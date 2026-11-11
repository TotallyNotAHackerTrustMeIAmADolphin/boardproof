"""Create sub-sheets and insert hierarchical sheet symbols (GitHub issue 75).

Two batch operations for the schematic toolset, kept out of ``toolsets/schematic.py``:

- ``create_sheets``: new empty ``.kicad_sch`` files inside an existing project, in the format
  KiCad 10 writes (``version 20260306``, measured: ``kicad-cli sch upgrade`` of a blank sheet).
  A new sheet is unreferenced until a sheet symbol points at it, so the result says so and the
  sheet-tree walk's other findings (a missing, unparseable or cyclic sheet) refuse the call.
- ``add_sheet_symbols``: one ``(sheet ...)`` node per spec in a parent sheet, referencing a
  sheet created above, with its sheet pins; and the matching ``(hierarchical_label ...)``
  nodes in the child, so both sides name the same pins in the same direction.

Decision 1 (docs/ARCHITECTURE.md): nothing here edits KiCad text. Nodes are built from typed
atoms (``write_layer.string/symbol/number``) on ``kicad_tools``' low-level ``SExp`` tree, and every
file is saved with ``write_layer.save`` (parse check by kicad-cli, a reload self-check, then
``os.replace``). Verification after the write (decision 4): the sheet-tree walk must be whole,
kicad-cli's ERC of the result may show no violation type that the project did not already show
(besides the dangling pins and labels the new interface has by construction), and kicad-cli's
netlist export must see every new sheet. A failed check restores the files this call changed.
"""

from __future__ import annotations

import copy
import shutil
import tempfile
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import _self_check, sheet_tree, write_layer

SCHEMATIC_VERSION = 20260306  # what KiCad 10.0 writes; measured with `kicad-cli sch upgrade`
GENERATOR = "eeschema"
GENERATOR_VERSION = "10.0"
GRID = 1.27  # KiCad's default connection grid, in mm
PIN_DIRECTIONS = ("input", "output", "bidirectional", "passive")
SIDES = ("left", "right", "top", "bottom")
DEFAULT_SIDE = {"input": "left", "output": "right", "bidirectional": "left", "passive": "left"}
# sheet-pin angle by border: how KiCad writes (at x y angle) for a pin on that edge
_PIN_ANGLE = {"left": 180, "right": 0, "top": 90, "bottom": 270}
DEFAULT_WIDTH = 30.48
LABEL_X = 25.4
LABEL_Y = 25.4
LABEL_STEP = 2.54
# ERC types the new interface has by construction (measured, kicad-cli 10.0.7): each sheet pin that
# nothing is wired to is one pin_not_connected, each hierarchical label of a newly referenced sheet that
# nothing is wired to is one label_dangling. More of them than that, or any other new type, fails the insert.
EXPECTED_DANGLING_ERC = frozenset({"pin_not_connected", "label_dangling"})


class SheetAuthoringError(RuntimeError):
    """A refusal; ``code`` is machine-readable, ``details`` is the evidence."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


def refusal(exc: SheetAuthoringError) -> dict[str, Any]:
    return {"status": "incomplete", "ok": False, "code": exc.code, "reason": str(exc), **exc.details}


# --------------------------------------------------------------------------- node building


def _n(name: str, *children: SExp) -> SExp:
    return SExp.list(name, *children)


def _s(value: str) -> SExp:
    return write_layer.string(value)


def _y(value: str) -> SExp:
    return write_layer.symbol(value)


def _num(value: int | float) -> SExp:
    return write_layer.number(value)


def _at(x: float, y: float, angle: int | float) -> SExp:
    return _n("at", _num(x), _num(y), _num(angle))


def _effects(justify: str | None = None) -> SExp:
    node = _n("effects", _n("font", _n("size", _num(1.27), _num(1.27))))
    if justify:
        node.append(_n("justify", *(_y(word) for word in justify.split())))
    return node


def _uuid() -> SExp:
    return _n("uuid", _s(str(uuid.uuid4())))


def empty_sheet(title: str | None = None) -> SExp:
    """A blank schematic root as KiCad 10 writes one (``kicad-cli sch upgrade`` of a blank sheet)."""
    root = _n(
        "kicad_sch",
        _n("version", _num(SCHEMATIC_VERSION)),
        _n("generator", _s(GENERATOR)),
        _n("generator_version", _s(GENERATOR_VERSION)),
        _uuid(),
        _n("paper", _s("A4")),
    )
    if title:
        root.append(_n("title_block", _n("title", _s(title))))
    root.append(_n("lib_symbols"))
    return root


# --------------------------------------------------------------------------- checks


def _grid_ok(value: float) -> bool:
    return abs(value / GRID - round(value / GRID)) < 1e-6


def _number(spec: dict, key: str, where: str, *, required: bool = True, default: float | None = None) -> float:
    if key not in spec or spec[key] is None:
        if required:
            raise SheetAuthoringError("invalid_spec", f"{where}: '{key}' is required")
        return default  # type: ignore[return-value]
    value = spec[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SheetAuthoringError("invalid_spec", f"{where}: '{key}' must be a number, not {value!r}")
    return float(value)


def _name(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SheetAuthoringError("invalid_spec", f"{where} must be a non-empty string without leading or trailing space, got {value!r}")
    if any(ord(c) < 0x20 for c in value) or len(value) > 255:
        raise SheetAuthoringError("invalid_spec", f"{where} holds a control character (a line break among them) or is over 255 characters: {value!r}")
    return value


_DEVICE_NAMES = frozenset({"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))})


def _degenerate(name: str) -> str | None:
    """Why a path ``name`` cannot be a sheet file on Windows or is an empty name, or None."""
    for part in name.split("/"):
        if part == "..":
            continue  # a way out of the project: refused as sheet_outside_project
        stem = part[: -len(".kicad_sch")] if part.endswith(".kicad_sch") else part
        if part.endswith(".kicad_sch") and not stem.strip(" ."):
            return f"{part!r} has an empty file name before .kicad_sch"
        if part in ("", ".") or part != part.rstrip(" ."):
            return f"{part!r} is not a usable file or folder name"
        if stem.split(".")[0].upper() in _DEVICE_NAMES:
            return f"{part!r} is a Windows device name"
    return None


def _sheet_path(parent_dir: Path, project_dir: Path, value: Any, where: str) -> Path:
    name = _name(value, where)
    if "\\" in name or name.startswith("/") or (len(name) > 1 and name[1] == ":") or not name.endswith(".kicad_sch"):
        raise SheetAuthoringError("invalid_spec", f"{where} must be a relative path with forward slashes ending in .kicad_sch, got {name!r}")
    if (why := _degenerate(name)) is not None:
        raise SheetAuthoringError("invalid_spec", f"{where}: {why}")
    target = (parent_dir / name).resolve()
    try:
        target.relative_to(project_dir)
    except ValueError:
        raise SheetAuthoringError("sheet_outside_project", f"{where} {name!r} is outside the project directory {project_dir}") from None
    return target


def _require_tree(root: Path) -> sheet_tree.SheetTree:
    if not root.is_file():
        raise SheetAuthoringError("file_not_found", f"root sheet not found: {root}")
    tree = sheet_tree.walk(root)
    if tree.problems:
        raise SheetAuthoringError(
            "sheet_tree_incomplete",
            f"the sheet tree from {root.name} is incomplete: " + "; ".join(tree.problems) + "; nothing was written",
            **tree.report(),
        )
    return tree


MAX_PARENTS_SEARCHED = 12


def discover_projects(path: Path) -> dict[str, list]:
    """Every project around ``path``: the ``.kicad_pro`` files in its folder and in the folders above it
    (at most ``MAX_PARENTS_SEARCHED`` levels), each with the root sheet of the same name, whose sheet tree is walked.

    ``contains``: ``(root sheet, tree)`` of the projects whose tree holds ``path``; ``excluded``: root sheets
    whose tree reads whole and does not hold it; ``unreadable``: ``"root sheet: why"`` for a tree that does
    not read whole (it may hold the sheet or not: nothing is proven about it)."""
    here = path.resolve()
    contains: list = []
    excluded: list = []
    unreadable: list = []
    for folder in [here.parent, *list(here.parent.parents)[:MAX_PARENTS_SEARCHED]]:
        for project in sorted(folder.glob("*.kicad_pro")):
            root = project.with_suffix(".kicad_sch")
            if not root.is_file():
                continue
            try:
                tree = sheet_tree.walk(root)
            except Exception as exc:  # noqa: BLE001 - whatever the walk could not read
                unreadable.append(f"{root}: {exc}")
                continue
            if tree.problems:
                unreadable.append(f"{root}: " + "; ".join(tree.problems))
            elif here in {f.resolve() for f in tree.files}:
                contains.append((root, tree))
            else:
                excluded.append(str(root))
    return {"contains": contains, "excluded": excluded, "unreadable": unreadable}


def find_root(path: Path, root_sch: str | None) -> Path:
    """The project's root sheet: ``root_sch`` if given, else ``path`` itself when a ``.kicad_pro``
    of the same name sits beside it, else the one ``.kicad_pro`` in its folder, else the one project
    in the folders above whose sheet tree holds ``path`` (a child sheet in a subfolder)."""
    if root_sch:
        return Path(root_sch)
    if path.with_suffix(".kicad_pro").is_file():
        return path
    projects = sorted(path.parent.glob("*.kicad_pro"))
    if len(projects) == 1 and projects[0].with_suffix(".kicad_sch").is_file():
        return projects[0].with_suffix(".kicad_sch")
    found = discover_projects(path)
    if len(found["contains"]) == 1:
        return found["contains"][0][0]
    named = [str(r) for r, _t in found["contains"]]
    raise SheetAuthoringError(
        "root_not_found", f"cannot tell which sheet is the project root for {path.name}; pass root_sch"
        + (f" (several projects hold it: {', '.join(named)})" if named else "")
        + (f" (projects that do not read whole, so nothing is proven about them: {'; '.join(found['unreadable'])})" if found["unreadable"] else ""),
        candidates=named, unreadable=found["unreadable"], excluded=found["excluded"])


def _expected_verify(root: SExp):
    expected = write_layer.serialise(root, compact=True)

    def verify(written: Path) -> None:
        reloaded = write_layer.load(written)
        if write_layer.serialise(reloaded, compact=True) != expected:
            raise SheetAuthoringError(
                "write_not_verified",
                f"check reload_equals_serialised: {written.name} did not reload as the tree that was serialised: "
                + (_self_check.describe_tree_difference(root, reloaded) or "the node order or the header differs"),
                check="reload_equals_serialised")

    return verify


def _save(root: SExp, path: Path) -> None:
    write_layer.save(root, path, verify=_expected_verify(root))


def _text(node: SExp, index: int = 0) -> str:
    return node.get_string(index) or ""


# --------------------------------------------------------------------------- create_sheets


def _spec_files(folder: Path, specs: Any) -> list[Path]:
    """The ``file`` of every spec as a path under ``folder``, for the lock only: a spec that is not valid is refused
    later, by the validation that needs the sheet tree."""
    if not isinstance(specs, list):
        return []
    out = []
    for spec in specs:
        name = spec.get("file") if isinstance(spec, dict) else None
        if isinstance(name, str) and name:
            out.append((folder / name).resolve())
    return out


def create_sheets(root_sch: str, specs: list[dict]) -> dict[str, Any]:
    """Create each ``{"file": "sub.kicad_sch", "title": optional}`` of ``specs`` as a new blank sheet."""
    try:
        root = Path(root_sch)
        # every file to create, for the whole call (issue 110: ``root_sch`` is not one of the tool wrapper's mutation
        # arguments, so two calls naming one new file both passed the "exists" check). The root is only read, so it is not
        # locked: a call never waits for a file above the one it writes (sheet_pins._run holds the root and then its children).
        with write_layer.path_lock(_spec_files(root.parent, specs)):
            return _create_sheets(root, specs)
    except SheetAuthoringError as exc:
        return refusal(exc)
    except write_layer.WriteLayerError as exc:
        if (locked := write_layer.as_lock_refusal(exc)) is not None:
            return locked
        return {"status": "incomplete", "ok": False, "code": exc.code, "reason": str(exc), **exc.details}


def _create_sheets(root: Path, specs: list[dict]) -> dict[str, Any]:
    if not isinstance(specs, list) or not specs:
        raise SheetAuthoringError("invalid_spec", "specs must be a non-empty list of {file, title?}")
    tree = _require_tree(root)
    targets: list[tuple[Path, str | None]] = []
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict) or set(spec) - {"file", "title"}:
            raise SheetAuthoringError("invalid_spec", f"specs[{i}] must be {{file, title?}}, got {spec!r}")
        target = _sheet_path(root.parent, tree.project_dir, spec.get("file"), f"specs[{i}].file")
        title = spec.get("title")
        if title is not None:
            title = _name(title, f"specs[{i}].title")
        if target.exists():
            raise SheetAuthoringError("sheet_exists", f"{tree.rel(target)} already exists; nothing was written", file=str(target))
        if not target.parent.is_dir():
            raise SheetAuthoringError("invalid_spec", f"the folder of specs[{i}].file does not exist: {target.parent}")
        if any(target == other for other, _ in targets):
            raise SheetAuthoringError("invalid_spec", f"specs[{i}].file {spec['file']!r} is listed twice")
        targets.append((target, title))
    write_layer.check_writable(root, *(t for t, _ in targets))
    created: list[Path] = []
    try:
        for target, title in targets:
            _save(empty_sheet(title or target.stem), target)
            created.append(target)
        after = sheet_tree.walk(root)
        if after.problems:
            raise SheetAuthoringError("write_not_verified", "the sheet tree is incomplete after the write: " + "; ".join(after.problems))
    except BaseException:
        for path in created:
            path.unlink(missing_ok=True)
        raise
    return {
        "status": "ok",
        "created": [tree.rel(t) for t, _ in targets],
        "count": len(targets),
        "awaiting_reference": [tree.rel(t) for t, _ in targets],
        "next": "add_sheet_symbols references each created sheet from a parent sheet; until then the sheet is unreferenced",
        **after.report(),
    }


# --------------------------------------------------------------------------- add_sheet_symbols


def _plan_pins(spec: dict, where: str, width: float, height: float) -> list[dict[str, Any]]:
    pins = spec.get("pins")
    if not isinstance(pins, list) or not pins:
        raise SheetAuthoringError("invalid_spec", f"{where}.pins must be a non-empty list of {{name, direction, side?, offset?}}")
    planned: list[dict[str, Any]] = []
    next_offset: dict[str, float] = {side: 2 * GRID for side in SIDES}
    seen: set[str] = set()
    taken: set[tuple[str, float]] = set()
    for j, pin in enumerate(pins):
        here = f"{where}.pins[{j}]"
        if not isinstance(pin, dict) or set(pin) - {"name", "direction", "side", "offset"}:
            raise SheetAuthoringError("invalid_spec", f"{here} must be {{name, direction, side?, offset?}}, got {pin!r}")
        name = _name(pin.get("name"), f"{here}.name")
        if name in seen:
            raise SheetAuthoringError("invalid_spec", f"{here}: pin name {name!r} is used twice in this sheet symbol")
        seen.add(name)
        direction = pin.get("direction")
        if direction not in PIN_DIRECTIONS:
            raise SheetAuthoringError("invalid_spec", f"{here}.direction must be one of {list(PIN_DIRECTIONS)}, got {direction!r}")
        side = pin.get("side") or DEFAULT_SIDE[direction]
        if side not in SIDES:
            raise SheetAuthoringError("invalid_spec", f"{here}.side must be one of {list(SIDES)}, got {side!r}")
        length = height if side in ("left", "right") else width
        if pin.get("offset") is None:
            offset = next_offset[side]
        else:
            offset = _number(pin, "offset", here)
        next_offset[side] = max(next_offset[side], offset + 2 * GRID)
        if not _grid_ok(offset) or not 0 < offset < length:
            raise SheetAuthoringError(
                "invalid_spec",
                f"{here}.offset {offset} must be a multiple of {GRID} mm strictly inside the {side} border (0 to {length} mm)",
            )
        if (side, round(offset, 6)) in taken:
            raise SheetAuthoringError("invalid_spec", f"{here}: another pin already sits at offset {offset} on the {side} border")
        taken.add((side, round(offset, 6)))
        planned.append({"name": name, "direction": direction, "side": side, "offset": offset})
    return planned


def _default_height(pins: list[dict]) -> float:
    left_right = [p["offset"] for p in pins if p["side"] in ("left", "right")]
    return max(4 * GRID, (max(left_right) + 2 * GRID) if left_right else 0.0)


def _geometry(where: str, label: str, value: float, *, positive: bool = False) -> None:
    if positive and value <= 0:
        raise SheetAuthoringError("invalid_spec", f"{where}.{label} {value} must be positive")
    if not _grid_ok(value):
        raise SheetAuthoringError("invalid_spec", f"{where}.{label} {value} must be a multiple of {GRID} mm (a pin off the grid cannot be wired)")


def _plan_sheet(spec: Any, i: int, parent_dir: Path, project_dir: Path) -> dict[str, Any]:
    where = f"specs[{i}]"
    allowed = {"file", "name", "x", "y", "width", "height", "pins"}
    if not isinstance(spec, dict) or set(spec) - allowed:
        raise SheetAuthoringError("invalid_spec", f"{where} must hold only {sorted(allowed)}, got {spec!r}")
    file = _sheet_path(parent_dir, project_dir, spec.get("file"), f"{where}.file")
    name = _name(spec.get("name") if spec.get("name") is not None else Path(file).stem, f"{where}.name")
    x, y = _number(spec, "x", where), _number(spec, "y", where)
    width = _number(spec, "width", where, required=False, default=DEFAULT_WIDTH)
    _geometry(where, "x", x), _geometry(where, "y", y), _geometry(where, "width", width, positive=True)
    if spec.get("height") is None:
        provisional = _plan_pins(spec, where, width, 1e6)  # offsets only; the borders are checked below
        height = round(_default_height(provisional) / GRID) * GRID
    else:
        height = _number(spec, "height", where)
    _geometry(where, "height", height, positive=True)
    return {"file": file, "name": name, "x": x, "y": y, "width": width, "height": height,
            "pins": _plan_pins(spec, where, width, height)}


def _pin_position(plan: dict, pin: dict) -> tuple[float, float]:
    x, y, w, h, o = plan["x"], plan["y"], plan["width"], plan["height"], pin["offset"]
    px, py = {"left": (x, y + o), "right": (x + w, y + o), "top": (x + o, y), "bottom": (x + o, y + h)}[pin["side"]]
    return round(px, 4), round(py, 4)  # KiCad stores nanometres; no float noise in the file


_JUSTIFY = {"left": "left", "right": "right", "top": "right", "bottom": "left"}


def sheet_node(plan: dict, rel_file: str, project: str, instance_pages: list[tuple[str, str]]) -> SExp:
    """The ``(sheet ...)`` node for ``plan``, as KiCad writes one (tokens known since KiCad 8, so
    a parent still in the KiCad 8 format reads it)."""
    x, y = plan["x"], plan["y"]
    font = _n("font", _n("size", _num(1.27), _num(1.27)))
    node = _n(
        "sheet",
        _n("at", _num(x), _num(y)),
        _n("size", _num(plan["width"]), _num(plan["height"])),
        _n("exclude_from_sim", _y("no")),
        _n("in_bom", _y("yes")),
        _n("on_board", _y("yes")),
        _n("dnp", _y("no")),
        _n("fields_autoplaced", _y("yes")),
        _n("stroke", _n("width", _num(0.1524)), _n("type", _y("solid"))),
        _n("fill", _n("color", _num(0), _num(0), _num(0), _num(0.0))),
        _uuid(),
        _n("property", _s("Sheetname"), _s(plan["name"]), _at(x, round(y - 0.7116, 4), 0),
           _n("effects", font, _n("justify", _y("left"), _y("bottom")))),
        _n("property", _s("Sheetfile"), _s(rel_file), _at(x, round(y + plan["height"] + 0.5884, 4), 0),
           _n("effects", font, _n("justify", _y("left"), _y("top")))),
    )
    for pin in plan["pins"]:
        px, py = _pin_position(plan, pin)
        node.append(_n("pin", _s(pin["name"]), _y(pin["direction"]), _at(px, py, _PIN_ANGLE[pin["side"]]),
                       _uuid(), _effects(_JUSTIFY[pin["side"]])))
    node.append(_n("instances", _n("project", _s(project),
                                   *(_n("path", _s(path), _n("page", _s(page))) for path, page in instance_pages))))
    return node


def label_node(name: str, direction: str, side: str, x: float, y: float) -> SExp:
    """The hierarchical label in the child that meets the sheet pin ``name``: same name, same direction."""
    facing_left = side != "right"
    return _n("hierarchical_label", _s(name), _n("shape", _y(direction)), _at(x, y, 180 if facing_left else 0),
              _effects("right" if facing_left else "left"), _uuid())


def _children_named(root: SExp, tag: str) -> list[SExp]:
    return [c for c in root.children if not c.is_atom and c.name == tag]


def _next_page(tree: sheet_tree.SheetTree) -> int:
    """One more than the highest numeric page any sheet instance in the project holds."""
    pages = [1]
    for sheet in tree.files:
        root = write_layer.load(sheet)
        for page in root.find_all("page"):
            if _text(page).isdigit():
                pages.append(int(_text(page)))
    return max(pages) + 1


def add_sheet_symbols(parent_sch: str, specs: list[dict], root_sch: str | None = None) -> dict[str, Any]:
    """Insert one sheet symbol per spec into ``parent_sch``; see the module docstring."""
    try:
        parent = Path(parent_sch)
        # the parent and the child files the call rewrites, for the whole call (issue 110: the wrapper holds only
        # ``parent_sch``, so an ``add_labels`` on a child was overwritten by the rewrite of its labels and instances). Not the
        # root: it is only read, and a call never waits for a file above its own parent (a call on the root holds the root and
        # then the parent, so holding the parent and asking for the root deadlocks until the lock timeout).
        with write_layer.path_lock([parent, *_spec_files(parent.parent, specs)]):
            return _add_sheet_symbols(parent, specs, root_sch)
    except SheetAuthoringError as exc:
        return refusal(exc)
    except write_layer.WriteLayerError as exc:
        if (locked := write_layer.as_lock_refusal(exc)) is not None:
            return locked
        return {"status": "incomplete", "ok": False, "code": exc.code, "reason": str(exc), **exc.details}


def _copy_project(tree: sheet_tree.SheetTree, extra: list[Path], scratch: Path) -> Path:
    """Copy the sheets of ``tree`` (and ``extra``), the project file and the library tables into
    ``scratch`` keeping their layout; returns the copy of the root. kicad-cli writes beside the
    root it is given, so every check runs on such a copy."""
    base = tree.project_dir
    for source in {*(f.resolve() for f in tree.files), *(e.resolve() for e in extra)}:
        dest = scratch / source.relative_to(base)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, dest)
    for sibling in (*base.glob("*.kicad_pro"), base / "sym-lib-table", base / "fp-lib-table"):
        if sibling.is_file():
            shutil.copyfile(sibling, scratch / sibling.name)
    return scratch / tree.root.resolve().relative_to(base)


def erc_census(root: Path) -> Counter:
    """Violations of kicad-cli's ERC (all severities) of the project at ``root``, as a count per
    ``(type, severity)``. ``root`` must be a scratch copy: the CLI writes beside it."""
    from boardproof import kicad_cli

    report = kicad_cli.run_erc(root)
    counts: Counter = Counter()
    for sheet in report.get("sheets", []):
        for violation in sheet.get("violations", []):
            counts[(violation.get("type", "?"), violation.get("severity", "?"))] += 1
    return counts


def netlist_sheet_names(root: Path) -> list[str]:
    """The sheet names kicad-cli's netlist export lists, from ``(design (sheet (name ...)))``."""
    from kicad_tools.sexp.parser import parse_file

    from boardproof import kicad_cli

    with tempfile.TemporaryDirectory(prefix="boardproof-sheet-net-") as tmp:
        parsed = parse_file(str(kicad_cli.export_netlist(root, Path(tmp) / "check.net")))
    return [_text(sheet.find_children("name")[0]) for design in parsed.find_children("design")
            for sheet in design.find_children("sheet") if sheet.find_children("name")]


def interface_mismatches(parent: SExp, child: SExp, sheet_file: str) -> list[str]:
    """Where the sheet symbol of ``sheet_file`` in ``parent`` and the hierarchical labels of ``child``
    disagree: a pin without a label, a label without a pin, or a different direction. kicad-cli's
    ERC reports a missing name (``hier_label_mismatch``) but not a different direction."""
    problems = []
    for node in parent.find_children("sheet"):
        if sheet_tree.sheet_file_property(node)[0] != sheet_file:
            continue
        pins = {_text(p): _text(p, 1) for p in node.find_children("pin")}
        labels = {_text(c): _text(c.find_children("shape")[0]) if c.find_children("shape") else "" for c in _children_named(child, "hierarchical_label")}
        problems += [f"{sheet_file}: pin {n!r} has no hierarchical label" for n in pins if n not in labels]
        problems += [f"{sheet_file}: hierarchical label {n!r} has no pin" for n in labels if n not in pins]
        problems += [f"{sheet_file}: {n!r} is {pins[n]} on the pin and {labels[n]} on the label" for n in pins if n in labels and pins[n] != labels[n]]
    return problems


def rewrite_child_instances(child: SExp, project: str, new_paths: list[str], skip_uuids: frozenset[str] = frozenset()) -> int:
    """Point the instance data of every symbol in ``child`` at the sheet use(s) ``new_paths``.

    A sheet written alone keys its symbols' ``(instances (project .. (path "/<its own uuid>" ..)))``
    on itself; once a sheet symbol references it, KiCad reads them at ``<parent path>/<sheet symbol
    uuid>`` under the project's name, and the project's own check (``sheet_tree.netlist_disagreement``)
    calls the old entries stale. Each entry at the child's own path becomes one entry per new path,
    with its reference, unit and other children kept; entries at any other path are left alone.
    Symbols whose uuid is in ``skip_uuids`` are left as they are (add_symbols re-keys only the symbols it
    adds, GitHub issue 107). Returns the number of symbols changed."""
    own = "/" + (_text(child.find_children("uuid")[0]) if child.find_children("uuid") else "")
    changed = 0
    for symbol in _children_named(child, "symbol"):
        if skip_uuids and symbol.find_children("uuid") and _text(symbol.find_children("uuid")[0]) in skip_uuids:
            continue
        for instances in symbol.find_children("instances"):
            moved = []
            for proj in instances.find_children("project"):
                for path in proj.find_children("path"):
                    if _text(path).lower() == own.lower():
                        moved.append(path)
                        proj.children.remove(path)
            if not moved:
                continue
            for proj in [p for p in instances.find_children("project") if not p.find_children("path")]:
                instances.children.remove(proj)
            target = next((p for p in instances.find_children("project") if _text(p) == project), None)
            if target is None:
                target = _n("project", _s(project))
                instances.append(target)
            for old in moved:
                for new in new_paths:
                    entry = _n("path", _s(new))
                    for extra in old.children[1:]:
                        entry.append(copy.deepcopy(extra))
                    target.append(entry)
            changed += 1
    return changed


def _verify_staged(staged_root: Path, before: Counter, names: list[str], referenced: list[str], allowed: dict[str, int],
                   disagreements_before: list[str] = ()) -> dict[str, Any]:
    """The checks on the scratch copy, before anything real is written. Raises ``write_not_verified``."""
    tree = sheet_tree.walk(staged_root)
    if tree.problems or any(rel in tree.unreferenced for rel in referenced):
        raise SheetAuthoringError(
            "write_not_verified",
            "the sheet tree is not whole after the insert: "
            + "; ".join(tree.problems or [f"{r} is still unreferenced" for r in referenced]),
            **tree.report(),
        )
    after = erc_census(staged_root)
    new = {f"{t}/{s}": n - before[(t, s)] for (t, s), n in after.items() if n > before[(t, s)]}
    unexpected = {k: n for k, n in new.items() if k.split("/")[0] not in EXPECTED_DANGLING_ERC or n > allowed.get(k.split("/")[0], 0)}
    if unexpected:
        expected = {k: v for k, v in allowed.items() if v}
        raise SheetAuthoringError(
            "write_not_verified",
            f"kicad-cli ERC of the result shows more violations than the project and the child sheets had before plus "
            f"the new interface explains: in excess {unexpected} (new in total {new}; the interface explains at most "
            f"{expected}); nothing was written",
            erc_new_violations=new,
        )
    components = sheet_tree.netlist_components(staged_root)
    disagreements = [m for m in sheet_tree.netlist_disagreement(tree, [c["ref"] for c in components]) if m not in disagreements_before]
    if disagreements:
        raise SheetAuthoringError(
            "write_not_verified",
            "the instance data of the symbols does not match the sheet hierarchy after the insert: "
            + "; ".join(disagreements) + "; nothing was written",
            instance_problems=disagreements,
        )
    seen = netlist_sheet_names(staged_root)
    missing = [n for n in names if not any(s.strip("/").split("/")[-1] == n for s in seen)]
    if missing:
        raise SheetAuthoringError(
            "write_not_verified",
            f"kicad-cli's netlist export does not list the sheet(s) {missing} (it lists {seen}); nothing was written",
        )
    return {"erc_new_violations": new, "netlist_sheets": seen}


def _add_sheet_symbols(parent: Path, specs: list[dict], root_sch: str | None) -> dict[str, Any]:
    if not isinstance(specs, list) or not specs:
        raise SheetAuthoringError("invalid_spec", "specs must be a non-empty list of sheet symbols")
    if not parent.is_file():
        raise SheetAuthoringError("file_not_found", f"parent sheet not found: {parent}")
    root = find_root(parent, root_sch)
    tree = _require_tree(root)
    parent_key = parent.resolve()
    if parent_key not in {f.resolve() for f in tree.files}:
        raise SheetAuthoringError("parent_not_in_tree", f"{parent.name} is not reached from the root sheet {root.name}", **tree.report())
    plans = [_plan_sheet(spec, i, parent_key.parent, tree.project_dir) for i, spec in enumerate(specs)]
    unreferenced = {(tree.project_dir / rel).resolve() for rel in tree.unreferenced}
    seen_files: set[Path] = set()
    parent_root = write_layer.load(parent_key)
    existing_names = {sheet_tree.sheet_name_property(s) for s in parent_root.find_children("sheet")}
    for i, plan in enumerate(plans):
        rel = tree.rel(plan["file"])
        if plan["file"] in seen_files:
            raise SheetAuthoringError("invalid_spec", f"specs[{i}].file is listed twice")
        seen_files.add(plan["file"])
        if not plan["file"].is_file():
            raise SheetAuthoringError("sheet_file_missing", f"specs[{i}].file {rel} does not exist; create it with create_sheets first")
        if plan["file"] not in unreferenced:
            raise SheetAuthoringError(
                "sheet_already_referenced", f"{rel} is already a sheet of this project (or is the parent itself); nothing was written"
            )
        if plan["name"] in existing_names or sum(p["name"] == plan["name"] for p in plans) > 1:
            raise SheetAuthoringError("invalid_spec", f"specs[{i}].name {plan['name']!r} is already used by a sheet in {parent.name}")
    write_layer.check_writable(parent_key, root, *(p["file"] for p in plans))

    project = root.stem
    page = _next_page(tree)
    uses = [path for file, path in tree.uses if file == parent_key]
    child_roots: dict[Path, SExp] = {}
    rewritten: dict[Path, int] = {}
    report = []
    for plan in plans:
        child = write_layer.load(plan["file"])
        have = {_text(label): label for label in _children_named(child, "hierarchical_label")}
        ys = [label.find_children("at")[0].get_float(1) or 0.0 for label in have.values() if label.find_children("at")]
        y = max(ys) + LABEL_STEP if ys else LABEL_Y
        added, reused = [], []
        for pin in plan["pins"]:
            old = have.get(pin["name"])
            if old is not None:
                shape = _text(old.find_children("shape")[0]) if old.find_children("shape") else ""
                if shape != pin["direction"]:
                    raise SheetAuthoringError(
                        "label_direction_mismatch",
                        f"{tree.rel(plan['file'])} already has hierarchical label {pin['name']!r} with direction {shape!r}, "
                        f"not {pin['direction']!r}; nothing was written",
                    )
                reused.append(pin["name"])
                continue
            child.append(label_node(pin["name"], pin["direction"], pin["side"], LABEL_X, round(y, 4)))
            y += LABEL_STEP
            added.append(pin["name"])
        child_roots[plan["file"]] = child
        report.append({
            "sheet": plan["name"], "file": tree.rel(plan["file"]), "pins": len(plan["pins"]),
            "labels_added": added, "labels_reused": reused,
            "pin_positions": {p["name"]: list(_pin_position(plan, p)) for p in plan["pins"]},
        })
        node = sheet_node(plan, plan["file"].relative_to(parent_key.parent).as_posix(), project, [(path, str(page)) for path in uses])
        page += 1
        sheet_uuid = _text(node.find_children("uuid")[0])
        rewritten[plan["file"]] = rewrite_child_instances(child, project, [f"{path}/{sheet_uuid}" for path in uses])
        report[-1]["symbols_reattached"] = rewritten[plan["file"]]
        tail = [i for i, c in enumerate(parent_root.children) if not c.is_atom and c.name in ("sheet_instances", "embedded_fonts", "symbol_instances")]
        parent_root.children.insert(tail[0] if tail else len(parent_root.children), node)

    base = tree.project_dir
    extra = [p["file"] for p in plans]
    before_dir = Path(tempfile.mkdtemp(prefix="boardproof-sheet-before-"))
    staged_dir = Path(tempfile.mkdtemp(prefix="boardproof-sheet-staged-"))
    try:
        before_root = _copy_project(tree, extra, before_dir)
        before = erc_census(before_root)
        for p in plans:  # what each child already shows on its own, unattached
            alone = before_dir / "alone" / p["file"].name
            alone.parent.mkdir(exist_ok=True)
            shutil.copyfile(p["file"], alone)
            before += erc_census(alone)
            alone.unlink()
        disagreements_before = sheet_tree.netlist_disagreement(
            sheet_tree.walk(before_root), [c["ref"] for c in sheet_tree.netlist_components(before_root)])
        staged_root = _copy_project(tree, extra, staged_dir)
        for file, child in child_roots.items():
            _save(child, staged_dir / file.relative_to(base))
        _save(parent_root, staged_dir / parent_key.relative_to(base))
        staged_parent = write_layer.load(staged_dir / parent_key.relative_to(base))
        mismatches = [m for p in plans for m in interface_mismatches(
            staged_parent, write_layer.load(staged_dir / p["file"].relative_to(base)), p["file"].relative_to(parent_key.parent).as_posix())]
        if mismatches:
            raise SheetAuthoringError("write_not_verified", "sheet pins and hierarchical labels disagree after the insert: " + "; ".join(mismatches))
        allowed = {"pin_not_connected": sum(len(p["pins"]) for p in plans), "label_dangling": sum(len(r["labels_added"]) for r in report)}
        proof = _verify_staged(staged_root, before, [p["name"] for p in plans], [tree.rel(p["file"]) for p in plans], allowed,
                               disagreements_before)
    finally:
        shutil.rmtree(before_dir, ignore_errors=True)
        shutil.rmtree(staged_dir, ignore_errors=True)

    originals = {file: write_layer.load(file) for file in child_roots}
    saved: list[Path] = []
    try:
        for file, child in child_roots.items():
            _save(child, file)
            saved.append(file)
        _save(parent_root, parent_key)
    except BaseException:
        for file in saved:  # the parent did not change, so the child must not keep labels nothing points at
            try:
                write_layer.save(originals[file], file)
            except Exception:  # noqa: BLE001 - the original failure is the one to report
                pass
        raise
    after = sheet_tree.walk(root)
    whole = not after.problems and not any(tree.rel(p["file"]) in after.unreferenced for p in plans)
    return {"status": "ok", "ok": True, "inserted": report, "count": len(plans),
            "verified": {"sheet_tree_whole": whole, **proof}, **after.report()}
