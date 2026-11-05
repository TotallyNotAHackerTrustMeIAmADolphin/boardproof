"""Delete and move what a schematic sheet holds (GitHub issue 84).

The schematic toolset could only add. Reworking a sheet meant resetting it to its skeleton and
sending every coordinate again. This module holds the batch tools that change a sheet in place:

- ``delete_symbols`` / ``delete_wires`` / ``delete_labels`` / ``delete_junctions``
- ``move_symbols`` / ``move_labels``
- ``set_property_positions`` (position, angle and justification of a symbol's property texts)
- ``replace_symbols`` (issue 112: a placed symbol becomes another lib_id in place; the checks are at the function)

Decision 1 (docs/ARCHITECTURE.md): nothing here edits KiCad text. A sheet is loaded with
``write_layer.load`` into ``kicad_tools``' low-level ``SExp`` tree, changed by removing nodes and
by ``write_layer.set_number`` / typed atoms, and saved with ``write_layer.save`` (kicad-cli parse
check, reload-and-compare, ``os.replace``). Decision 4, the order of work for every call:

1. select the items (``select_*``; a selector that matches nothing, or a listed reference, text or
   uuid that does not exist, refuses the whole call);
2. change a copy of the tree in memory;
3. staged check: every node the call did not mean to touch is unchanged;
4. kicad-cli's netlist of the project before and after, from scratch copies, compared net by net
   (``compare_nets``): a split net, a merged net, a pin left with nothing else on its net, a
   renamed net or a vanished named net is a connectivity change; the call is refused with
   ``connectivity_changed`` (nothing written, the changes listed) unless
   ``allow_connectivity_change`` is true, and then the result lists them anyway;
5. only then the real write, with the reload check.

The facts the tool texts state, and docs/ARCHITECTURE.md records:

- a wire, junction or label a delete leaves dangling is reported (``dangling_after``, only what
  the call itself made dangling); ``remove_dangling`` removes dangling wires and junctions
  (repeatedly, until none the call made is left), never labels;
- a moved symbol drags the wire ends, junctions, labels and no-connect markers that sit exactly on
  its pins (``move_wires``, default true); a wire whose other end stays put becomes slanted if the
  move is not along it, and the result says so (``slanted``);
- every moved position is snapped to ``grid`` (1.27 mm by default; ``snap`` false turns it off) and
  the result lists what was snapped;
- positions only change: the instance data of a symbol, on the root sheet or on a sheet a parent
  uses (issue 79), is not touched.
"""

from __future__ import annotations

import fnmatch
import math
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp

from kicad_mcp import _self_check, sheet_authoring, sheet_tree, write_layer
from kicad_mcp.sheet_authoring import SheetAuthoringError

GRID = 1.27
TOL = 1e-3  # mm: KiCad connects by exact internal units (1 nm); a micrometre is far above any rounding of ours
LABEL_TAGS = ("label", "global_label", "hierarchical_label")
LABEL_KINDS = {"label": "label", "global": "global_label", "hier": "hierarchical_label"}
MAX_LISTED = 50


# --------------------------------------------------------------------------- small helpers


def _kids(node: SExp, tag: str) -> list[SExp]:
    return [c for c in node.children if not c.is_atom and c.name == tag]


def _uuid(node: SExp) -> str:
    found = node.find_children("uuid")
    return (found[0].get_string(0) or "") if found else ""


def _at(node: SExp) -> tuple[float, float, float]:
    at = node.find_children("at")[0]
    return at.get_float(0), at.get_float(1), (at.get_float(2) if len(at.children) > 2 else 0.0) or 0.0


def _set_at(node: SExp, x: float, y: float) -> None:
    at = node.find_children("at")[0]
    write_layer.set_number(at, 0, _r(x))
    write_layer.set_number(at, 1, _r(y))


def _r(value: float) -> float:
    """Round to a micrometre-and-a-tenth (KiCad stores nanometres); no float noise in the file."""
    out = round(value + 0.0, 4)
    return 0.0 if out == 0 else out


def _key(x: float, y: float) -> tuple[float, float]:
    return (round(x / TOL) * TOL, round(y / TOL) * TOL)


def _same(a: tuple[float, float], b: tuple[float, float]) -> bool:
    return abs(a[0] - b[0]) <= TOL and abs(a[1] - b[1]) <= TOL


def _wire_ends(wire: SExp) -> list[tuple[float, float]]:
    pts = wire.find_children("pts")[0]
    return [(xy.get_float(0), xy.get_float(1)) for xy in pts.find_children("xy")]


def _set_wire_end(wire: SExp, index: int, x: float, y: float) -> None:
    xy = wire.find_children("pts")[0].find_children("xy")[index]
    write_layer.set_number(xy, 0, _r(x))
    write_layer.set_number(xy, 1, _r(y))


def _property(symbol: SExp, name: str) -> SExp | None:
    return next((p for p in _kids(symbol, "property") if p.get_string(0) == name), None)


def _ref(symbol: SExp) -> str:
    prop = _property(symbol, "Reference")
    return (prop.get_string(1) or "") if prop is not None else ""


def _remove(parent: SExp, doomed: list[SExp]) -> None:
    ids = {id(n) for n in doomed}
    parent.children[:] = [c for c in parent.children if id(c) not in ids]


def _round_pt(x: float, y: float) -> list[float]:
    return [_r(x), _r(y)]


# --------------------------------------------------------------------------- pins


def _lib_symbol(root: SExp, symbol: SExp) -> SExp | None:
    lib = symbol.find_children("lib_name")
    name = lib[0].get_string(0) if lib else (symbol.find_children("lib_id")[0].get_string(0) if symbol.find_children("lib_id") else None)
    for table in _kids(root, "lib_symbols"):
        for entry in _kids(table, "symbol"):
            if entry.get_string(0) == name:
                return entry
    return None


def _mirror(symbol: SExp) -> str:
    found = symbol.find_children("mirror")
    return (found[0].children[0].value if found and found[0].children else "") or ""


def place(px: float, py: float, rotation: float, mirror: str) -> tuple[float, float]:
    """A library coordinate (Y up) as an offset on the sheet (Y down) for a symbol rotated
    ``rotation`` degrees counter-clockwise and then mirrored about the x or y axis (``mirror``).
    The order, rotate then mirror, was measured against kicad-cli's netlist for the four rotations
    and both mirrors (tests/test_sheet_editing.py)."""
    rad = math.radians(rotation)
    x = px * math.cos(rad) - py * math.sin(rad)
    y = px * math.sin(rad) + py * math.cos(rad)
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return x, -y


def pin_positions(root: SExp, symbol: SExp, *, with_type: bool = False, with_exit: bool = False) -> list[dict[str, Any]]:
    """Where the pins of one placed ``symbol`` (a unit of it) sit on the sheet, from the raw
    ``(at ...)`` of the symbol and of the pins in the embedded library symbol. Each entry:
    ``number``, ``name``, ``x``, ``y`` (and ``type``, the electrical type word of the pin, when
    ``with_type``; and ``exit``, when ``with_exit``: the unit vector ``[dx, dy]`` on the sheet
    (Y down) along which a wire leaves the pin, away from the symbol body, rounded to the nearest
    axis). The library angle of a pin is the direction in which its line runs from the connection
    point into the body, so the wire leaves the opposite way. An unreadable library symbol gives
    an empty list."""
    lib = _lib_symbol(root, symbol)
    if lib is None:
        return []
    sx, sy, rot = _at(symbol)
    mirror = _mirror(symbol)
    unit = (symbol.find_children("unit")[0].get_int(0) if symbol.find_children("unit") else 1) or 1
    style = (symbol.find_children("body_style")[0].get_int(0) if symbol.find_children("body_style") else 1) or 1
    out = []
    for sub in [lib, *_kids(lib, "symbol")]:
        if sub is not lib:
            parts = (sub.get_string(0) or "").rsplit("_", 2)
            if len(parts) < 3 or not (parts[1].isdigit() and parts[2].isdigit()):
                continue
            if int(parts[1]) not in (0, unit) or int(parts[2]) not in (0, style):
                continue
        for pin in _kids(sub, "pin"):
            at = pin.find_children("at")
            if not at:
                continue
            dx, dy = place(at[0].get_float(0), at[0].get_float(1), rot, mirror)
            name = pin.find_children("name")
            number = pin.find_children("number")
            entry = {"number": number[0].get_string(0) if number else "", "name": name[0].get_string(0) if name else "",
                     "x": _r(sx + dx), "y": _r(sy + dy)}
            if with_type:
                entry["type"] = str(pin.children[0].value) if pin.children and pin.children[0].is_atom else ""
            if with_exit:
                angle = math.radians(at[0].get_float(2) if len(at[0].children) > 2 else 0.0)
                ex, ey = place(-math.cos(angle), -math.sin(angle), rot, mirror)
                entry["exit"] = ([1 if ex > 0 else -1, 0] if abs(ex) >= abs(ey) else [0, 1 if ey > 0 else -1])
            out.append(entry)
    return out


def get_symbol_pins(path: str, references: Any = None, uuids: Any = None) -> dict[str, Any]:
    """Read-only (issue 92): the pins of every placed symbol whose Reference property matches one
    of ``references`` (shell patterns allowed, ``"*"`` for all on this sheet) or whose uuid is in
    ``uuids`` (selectors are ORed; at least one must be non-empty), one entry per placed symbol,
    so a multi-unit part gives one entry per unit placed. Positions are the ``pin_positions``
    maths on the raw ``(at ...)`` nodes; nothing is written and no lock is taken."""
    try:
        file = Path(path)
        if not file.is_file():
            raise SheetAuthoringError("file_not_found", f"sheet not found: {file}")
        references = [] if references is None else references
        uuids = [] if uuids is None else uuids
        for name, value in (("references", references), ("uuids", uuids)):
            if not isinstance(value, list) or not all(isinstance(r, str) and r for r in value):
                raise SheetAuthoringError("invalid_spec", f"{name} must be a list of non-empty strings")
        if not references and not uuids:
            raise SheetAuthoringError("invalid_spec", "give references (shell patterns allowed) or uuids, at least one non-empty")
        root = write_layer.load(file)
        if root.name != "kicad_sch":
            raise SheetAuthoringError("invalid_spec", f"{file.name} is not a schematic sheet")
        placed = [s for s in _kids(root, "symbol") if s.find_children("lib_id")]
        missing = [r for r in references if not any(fnmatch.fnmatchcase(_ref(s), r) for s in placed)]
        missing += [u for u in uuids if not any(_uuid(s) == u for s in placed)]
        if missing:
            raise SheetAuthoringError("not_found", f"no placed symbol on this sheet matches: {', '.join(map(repr, missing))}", not_found=missing)
        out = []
        for symbol in placed:
            if not (_uuid(symbol) in uuids or any(fnmatch.fnmatchcase(_ref(symbol), r) for r in references)):
                continue
            x, y, rot = _at(symbol)
            unit = (symbol.find_children("unit")[0].get_int(0) if symbol.find_children("unit") else 1) or 1
            lib_id = symbol.find_children("lib_id")[0].get_string(0)
            pins = pin_positions(root, symbol, with_type=True)
            out.append({"reference": _ref(symbol), "uuid": _uuid(symbol), "lib_id": lib_id, "unit": unit,
                        "at": _round_pt(x, y), "rotation": rot, "mirror": _mirror(symbol), "pins": pins,
                        "pin_count": len(pins), **({} if pins else {"warning": "the sheet holds no library symbol with pins for this lib_id"})})
        out.sort(key=lambda e: (e["reference"], e["unit"], e["uuid"]))
        return {"status": "ok", "ok": True, "count": len(out), "symbols": out}
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)


# --------------------------------------------------------------------------- the sheet's items


class Sheet:
    """Index of the items of a sheet's root node by kind."""

    def __init__(self, root: SExp, nets_before: Any = None) -> None:
        self.root = root
        self.nets_before = nets_before  # callable -> kicad-cli's netlist of the project before the change (memoised)
        self.symbols = _kids(root, "symbol")
        self.wires = _kids(root, "wire")
        self.junctions = _kids(root, "junction")
        self.labels = [c for c in root.children if not c.is_atom and c.name in LABEL_TAGS]
        self.no_connects = _kids(root, "no_connect")
        self.sheets = _kids(root, "sheet")


def _segment_contains(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> bool:
    (ax, ay), (bx, by), (px, py) = a, b, p
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    length = math.hypot(bx - ax, by - ay)
    if length < TOL:
        return _same(a, p)
    if abs(cross) / length > TOL:
        return False
    dot = (px - ax) * (bx - ax) + (py - ay) * (by - ay)
    return -TOL * length <= dot <= length * length + TOL * length


def _connectors(sheet: Sheet) -> list[tuple[float, float]]:
    """Points where something other than a wire sits: symbol pins, labels, no-connect markers,
    sheet pins and junctions."""
    pts: list[tuple[float, float]] = []
    for sym in sheet.symbols:
        pts += [(p["x"], p["y"]) for p in pin_positions(sheet.root, sym)]
    for node in (*sheet.labels, *sheet.no_connects, *sheet.junctions):
        x, y, _ = _at(node)
        pts.append((x, y))
    for node in sheet.sheets:
        for pin in _kids(node, "pin"):
            x, y, _ = _at(pin)
            pts.append((x, y))
    return pts


def dangling(sheet: Sheet) -> dict[str, list[str]]:
    """The uuids of the wires with an end that touches nothing (no pin, label, marker, junction,
    other wire end or wire body), of the junctions where fewer than three wire ends or pins meet,
    and of the labels that touch no wire and no pin."""
    wires = [(w, _wire_ends(w)) for w in sheet.wires]
    pins = [(p["x"], p["y"]) for sym in sheet.symbols for p in pin_positions(sheet.root, sym)]
    for node in sheet.sheets:
        pins += [(_at(pin)[0], _at(pin)[1]) for pin in _kids(node, "pin")]
    others = [(*_at(n)[:2],) for n in (*sheet.labels, *sheet.no_connects, *sheet.junctions)]

    def touching_wire(point, skip=None) -> bool:
        for w, ends in wires:
            if w is skip:
                continue
            if len(ends) >= 2 and _segment_contains(ends[0], ends[1], point):
                return True
        return False

    def touching_others(point) -> bool:
        return any(_same(point, o) for o in (*pins, *others))

    bad_wires = []
    for wire, ends in wires:
        for end in (ends[0], ends[-1]) if len(ends) >= 2 else ends:
            if not (touching_wire(end, skip=wire) or touching_others(end)):
                bad_wires.append(_uuid(wire))
                break
    bad_junctions = []
    for junction in sheet.junctions:
        x, y, _ = _at(junction)
        degree = sum(1 for p in pins if _same((x, y), p))
        for _w, ends in wires:
            if len(ends) < 2:
                continue
            if _same(ends[0], (x, y)) or _same(ends[-1], (x, y)):
                degree += 1
            elif _segment_contains(ends[0], ends[-1], (x, y)):
                degree += 2
        if degree < 3:
            bad_junctions.append(_uuid(junction))
    bad_labels = []
    for label in sheet.labels:
        x, y, _ = _at(label)
        if not (touching_wire((x, y)) or any(_same((x, y), p) for p in pins)):
            bad_labels.append(_uuid(label))
    return {"wires": bad_wires, "junctions": bad_junctions, "labels": bad_labels}


# --------------------------------------------------------------------------- selection

_SELECTOR_KEYS = {
    "symbol": {"references", "uuids", "pattern", "region", "lib_id"},
    "wire": {"uuids", "region", "net"},
    "label": {"texts", "uuids", "pattern", "region", "kind"},
    "junction": {"uuids", "region", "net", "points"},
}
_LIST_KEYS = {"references", "uuids", "texts", "points"}


def _region(value: Any, where: str) -> tuple[float, float, float, float]:
    ok = isinstance(value, (list, tuple)) and len(value) == 4 and all(
        isinstance(v, (int, float)) and not isinstance(v, bool) for v in value)
    if not ok:
        raise SheetAuthoringError("invalid_spec", f"{where}.region must be [x1, y1, x2, y2] in mm, got {value!r}")
    x1, y1, x2, y2 = (float(v) for v in value)
    return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def _inside(region, x: float, y: float) -> bool:
    return region[0] - TOL <= x <= region[2] + TOL and region[1] - TOL <= y <= region[3] + TOL


def _net_wires(sheet: Sheet, text: str) -> set[int]:
    """Indexes of the wires of this sheet joined, wire to wire, to a label whose text is ``text``."""
    wires = [(i, _wire_ends(w)) for i, w in enumerate(sheet.wires)]
    seeds = []
    for label in sheet.labels:
        if label.get_string(0) == text:
            x, y, _ = _at(label)
            seeds += [i for i, e in wires if len(e) >= 2 and _segment_contains(e[0], e[-1], (x, y))]
    found, todo = set(seeds), list(seeds)
    while todo:
        i = todo.pop()
        ends = wires[i][1]
        for j, other in wires:
            if j in found or len(other) < 2:
                continue
            if any(_segment_contains(other[0], other[-1], e) for e in (ends[0], ends[-1])) or any(
                    _segment_contains(ends[0], ends[-1], e) for e in (other[0], other[-1])):
                found.add(j)
                todo.append(j)
    return found


def _check_selectors(kind: str, selectors: Any) -> list[dict]:
    if not isinstance(selectors, list) or not selectors:
        raise SheetAuthoringError("invalid_spec", "selectors must be a non-empty list of selector objects")
    allowed = _SELECTOR_KEYS[kind]
    for i, sel in enumerate(selectors):
        if not isinstance(sel, dict) or not sel or set(sel) - allowed:
            raise SheetAuthoringError(
                "invalid_spec", f"selectors[{i}] must be a non-empty object with keys from {sorted(allowed)}, got {sel!r}")
        for key in _LIST_KEYS & set(sel):
            if not isinstance(sel[key], list) or not sel[key]:
                raise SheetAuthoringError("invalid_spec", f"selectors[{i}].{key} must be a non-empty list")
        if "pattern" in sel and not (isinstance(sel["pattern"], str) and sel["pattern"]):
            raise SheetAuthoringError("invalid_spec", f"selectors[{i}].pattern must be a non-empty string")
        if "kind" in sel and sel["kind"] not in LABEL_KINDS:
            raise SheetAuthoringError("invalid_spec", f"selectors[{i}].kind must be one of {sorted(LABEL_KINDS)}")
    return selectors


def select(sheet: Sheet, kind: str, selectors: Any) -> list[SExp]:
    """The nodes of ``kind`` ("symbol", "wire", "label" or "junction") the ``selectors`` pick.

    A selector is an object; all keys in it must match (AND), and the selectors are joined (OR).
    Keys: ``references`` (symbol references; every unit), ``texts`` (label texts, exact),
    ``uuids``, ``pattern`` (shell pattern on a reference or a label text, case sensitive),
    ``region`` ([x1, y1, x2, y2] in mm: a symbol's, label's or junction's own position inside it;
    a wire only when both ends are), ``lib_id``, ``kind`` (label, global or hier), ``net`` (the
    wires, or junctions on them, joined wire to wire to a label of that text on this sheet),
    ``points`` (junction positions). Raises ``not_found`` for a listed reference, text, uuid,
    net or point that matches nothing, and ``selector_matched_nothing`` for a selector that
    selects nothing."""
    selectors = _check_selectors(kind, selectors)
    nodes = {"symbol": sheet.symbols, "wire": sheet.wires, "label": sheet.labels, "junction": sheet.junctions}[kind]
    chosen: list[SExp] = []
    missing: list[str] = []
    for i, sel in enumerate(selectors):
        region = _region(sel["region"], f"selectors[{i}]") if "region" in sel else None
        net_wires = _net_wires(sheet, sel["net"]) if "net" in sel else None
        if net_wires is not None and not net_wires:
            missing.append(f"net {sel['net']!r}")
        net_ends = None
        if net_wires is not None:
            net_ends = [_wire_ends(sheet.wires[j]) for j in net_wires]
        hit: Counter = Counter()
        picked = []
        for idx, node in enumerate(nodes):
            if kind == "symbol":
                name = _ref(node)
                x, y, _a = _at(node)
                ids = {"references": name}
                if "lib_id" in sel and (node.find_children("lib_id")[0].get_string(0) if node.find_children("lib_id") else None) != sel["lib_id"]:
                    continue
            elif kind == "label":
                name = node.get_string(0) or ""
                x, y, _a = _at(node)
                ids = {"texts": name}
                if "kind" in sel and node.name != LABEL_KINDS[sel["kind"]]:
                    continue
            elif kind == "junction":
                name = ""
                x, y, _a = _at(node)
                ids = {}
            else:
                name = ""
                ends = _wire_ends(node)
                x = y = 0.0
                ids = {}
            if any(key in sel and ids[key] not in sel[key] for key in ids):
                continue
            if "uuids" in sel and _uuid(node) not in sel["uuids"]:
                continue
            if "pattern" in sel and not fnmatch.fnmatchcase(name, sel["pattern"]):
                continue
            if "points" in sel and not any(_same((x, y), (float(p[0]), float(p[1]))) for p in sel["points"]):
                continue
            if region is not None:
                if kind == "wire":
                    if not all(_inside(region, ex, ey) for ex, ey in ends):
                        continue
                elif not _inside(region, x, y):
                    continue
            if net_wires is not None:
                if kind == "wire":
                    if idx not in net_wires:
                        continue
                elif not any(len(e) >= 2 and _segment_contains(e[0], e[-1], (x, y)) for e in net_ends):
                    continue
            picked.append(node)
            for key in ids:
                if key in sel:
                    hit[ids[key]] += 1
            if "uuids" in sel:
                hit[_uuid(node)] += 1
        for key in ("references", "texts", "uuids"):
            if key in sel:
                missing += [f"{key[:-1]} {v!r}" for v in sel[key] if hit[v] == 0]
        if "points" in sel:
            missing += [f"point {p!r}" for p in sel["points"] if not any(
                _same(_at(n)[:2], (float(p[0]), float(p[1]))) for n in nodes)]
        if not picked and not missing:
            raise SheetAuthoringError("selector_matched_nothing", f"selectors[{i}] ({sel!r}) selects no {kind}; nothing was written")
        chosen += [n for n in picked if all(n is not c for c in chosen)]
    if missing:
        raise SheetAuthoringError(
            "not_found", f"not found on this sheet: {', '.join(missing)}; nothing was written", not_found=missing)
    return chosen


# --------------------------------------------------------------------------- nets


def _is_auto(name: str) -> bool:
    return name.startswith(("Net-(", "unconnected-(")) or "/Net-(" in name or "/unconnected-(" in name


def read_nets(tree: sheet_tree.SheetTree, sheet: Path, replacement: SExp | None) -> dict[str, frozenset]:
    """kicad-cli's netlist of the project, from a scratch copy of the tree in which ``sheet``
    holds ``replacement`` (when given): net name -> the ``(reference, pin)`` nodes on it,
    unannotated (``?``) references left out."""
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-sheet-edit-") as tmp:
        scratch = Path(tmp) / "p"
        root = sheet_authoring._copy_project(tree, [], scratch)
        if replacement is not None:
            sheet_authoring._save(replacement, scratch / sheet.resolve().relative_to(tree.project_dir))
        return nets_from(root)


def nets_from(root: Path) -> dict[str, frozenset]:
    """kicad-cli's netlist of the project whose root sheet is ``root`` (a scratch copy: the CLI
    writes beside it): net name -> the ``(reference, pin)`` nodes on it, unannotated references left out."""
    from kicad_tools.sexp.parser import parse_file

    from kicad_mcp import kicad_cli

    with tempfile.TemporaryDirectory(prefix="kicad-mcp-sheet-net-") as tmp:
        parsed = parse_file(str(kicad_cli.export_netlist(root, Path(tmp) / "check.net")))
    nets: dict[str, frozenset] = {}
    for block in parsed.find_children("nets"):
        for net in block.find_children("net"):
            name = net.find("name").children[0].value
            nodes = frozenset(
                (str(n.find("ref").children[0].value), str(n.find("pin").children[0].value))
                for n in net.find_children("node") if not str(n.find("ref").children[0].value).endswith("?"))
            nets[str(name)] = nodes
    return nets


def compare_nets(before: dict[str, frozenset], after: dict[str, frozenset], removed_refs: set[str] = frozenset()) -> list[dict]:
    """The connectivity changes between two netlists, ignoring the pins of ``removed_refs``
    (deleted symbols): ``split`` (pins of one net on several nets), ``merged`` (pins of several
    nets on one), ``orphaned`` (a pin that had company now has none), ``renamed`` (a named net,
    not an automatic ``Net-(..)``, under another name) and ``net_removed`` (a named net with no
    pin left)."""
    where_after = {node: name for name, nodes in after.items() for node in nodes}
    changes: list[dict] = []
    group_of: dict[str, set[str]] = {}
    for name, nodes in before.items():
        rest = {n for n in nodes if n[0] not in removed_refs}
        pins = sorted(f"{r}.{p}" for r, p in rest)
        if not rest:
            if not _is_auto(name):
                changes.append({"type": "net_removed", "net": name, "pins": []})
            continue
        landed = {where_after.get(n) for n in rest}
        if None in landed:
            changes.append({"type": "pin_lost", "net": name, "pins": pins})
            landed.discard(None)
        if len(landed) > 1:
            changes.append({"type": "split", "net": name, "pins": pins, "now_on": sorted(landed)})
        for n in rest:
            group_of.setdefault(where_after.get(n, ""), set()).add(name)
        if len(landed) == 1:
            (now,) = landed
            if now != name and not (_is_auto(name) and _is_auto(now)):
                changes.append({"type": "renamed", "net": name, "now": now, "pins": pins})
        if len(nodes) > 1 and len(rest) >= 1:
            for n in rest:
                if len(after.get(where_after.get(n, ""), ())) <= 1:
                    changes.append({"type": "orphaned", "net": name, "pins": [f"{n[0]}.{n[1]}"]})
    for now, names in group_of.items():
        if now and len(names) > 1:
            changes.append({"type": "merged", "nets": sorted(names), "now": now})
    return changes


def _bounded(changes: list[dict]) -> dict[str, Any]:
    return {"count": len(changes), "items": changes[:MAX_LISTED], "truncated": len(changes) > MAX_LISTED}


# --------------------------------------------------------------------------- the pipeline


@dataclass
class Outcome:
    """What one mutation of the in-memory tree did."""

    report: dict[str, Any] = field(default_factory=dict)
    removed_refs: set[str] = field(default_factory=set)   # references whose every unit is gone
    deleted: set[str] = field(default_factory=set)         # uuids of removed nodes
    changed: set[str] = field(default_factory=set)         # uuids of nodes changed in place
    dangling_before: dict[str, list[str]] = field(default_factory=dict)
    check_nets: bool = True
    added: set[str] = field(default_factory=set)           # uuids of nodes the call added on purpose
    compare: Any = None                                    # (before, after, removed_refs) -> changes; default compare_nets
    post_check: Any = None                                 # (before, after) -> None, raises to refuse (self-check)
    no_change: bool = False                                # nothing to write: return after the checks
    lib_added: set[str] = field(default_factory=set)       # names of lib_symbols entries the call added on purpose (issue 112)
    lib_removed: set[str] = field(default_factory=set)     # names of lib_symbols entries the call dropped on purpose


def _staged_check(original: SExp, new: SExp, outcome: Outcome) -> None:
    """Every top-level node the call did not mean to touch is unchanged; deleted nodes are gone;
    changed nodes are still there. Raises ``write_not_verified`` (nothing is written yet)."""
    track_lib = bool(outcome.lib_added or outcome.lib_removed)

    def table(root: SExp) -> tuple[dict[str, str], Counter, dict[str, list[str]]]:
        keyed: dict[str, str] = {}
        plain: Counter = Counter()
        libs: dict[str, list[str]] = {}
        for child in root.children:
            if child.is_atom:
                plain[write_layer.serialise(child, compact=True)] += 1
            elif track_lib and child.name == "lib_symbols":
                for entry in child.children:
                    if not entry.is_atom and entry.name == "symbol":
                        libs.setdefault(entry.get_string(0) or "", []).append(write_layer.serialise(entry, compact=True))
                    else:
                        plain[write_layer.serialise(entry, compact=True)] += 1
            elif _uuid(child) and child.name != "lib_symbols":
                keyed[_uuid(child)] = write_layer.serialise(child, compact=True)
            else:
                plain[write_layer.serialise(child, compact=True)] += 1
        return keyed, plain, libs

    old_keyed, old_plain, old_libs = table(original)
    new_keyed, new_plain, new_libs = table(new)
    problems = []
    if track_lib:
        for name in outcome.lib_removed:
            if name in new_libs:
                problems.append(f"library symbol {name} should be dropped but is still there")
        for name in outcome.lib_added:
            if name not in new_libs:
                problems.append(f"library symbol {name} should have been added but is missing")
        for name, text in old_libs.items():
            if name in outcome.lib_removed:
                continue
            if name not in new_libs:
                problems.append(f"library symbol {name} is gone but was not selected")
            elif new_libs[name] != text:
                problems.append(f"library symbol {name} changed")
        for name in new_libs.keys() - old_libs.keys():
            if name not in outcome.lib_added:
                problems.append(f"library symbol {name} is new")
    if old_plain != new_plain:
        problems.append("a node without a uuid (title block, library symbols, paper, ...) changed")
    for uid in outcome.deleted:
        if uid in new_keyed:
            problems.append(f"{uid} should be deleted but is still there")
    for uid, text in old_keyed.items():
        if uid in outcome.deleted:
            continue
        if uid not in new_keyed:
            problems.append(f"{uid} is gone but was not selected")
        elif uid not in outcome.changed and new_keyed[uid] != text:
            problems.append(f"{uid} changed but was not selected")
    for uid in new_keyed.keys() - old_keyed.keys():
        if uid not in outcome.added:
            problems.append(f"{uid} is new")
    for uid in outcome.added - new_keyed.keys():
        problems.append(f"{uid} should have been added but is missing")
    if problems:
        raise SheetAuthoringError(
            "write_not_verified", "the edited sheet differs from the original beyond the selection: "
            + "; ".join(problems[:10]) + "; nothing was written", problems=problems[:MAX_LISTED])


def _expected_verify(new: SExp):
    """The reload check of the real write: the file read back from the temporary sibling is the
    tree that was serialised."""
    expected = write_layer.serialise(new, compact=True)

    def verify(written: Path) -> None:
        reloaded = write_layer.load(written)
        if write_layer.serialise(reloaded, compact=True) != expected:
            raise SheetAuthoringError(
                "write_not_verified",
                f"check reload_equals_serialised: {written.name} did not reload as the edited tree; nothing was replaced: "
                + (_self_check.describe_tree_difference(new, reloaded) or "the node order or the header differs"),
                check="reload_equals_serialised")

    return verify


def _tree_for(path: Path, root_sch: str | None) -> tuple[sheet_tree.SheetTree, bool]:
    """The sheet tree to check connectivity in: the project's, or the sheet alone when it is no
    part of one (a new sheet no sheet symbol points at yet, or a folder without a project)."""
    try:
        root = sheet_authoring.find_root(path, root_sch)
    except SheetAuthoringError:
        if root_sch:
            raise
        root = path
    tree = sheet_authoring._require_tree(root)
    if path.resolve() in {f.resolve() for f in tree.files}:
        return tree, False
    if root_sch:
        raise SheetAuthoringError("parent_not_in_tree", f"{path.name} is not reached from the root sheet {Path(root_sch).name}", **tree.report())
    return sheet_authoring._require_tree(path), True


def run(path: str, root_sch: str | None, mutate, *, allow_connectivity_change: bool, dry_run: bool) -> dict[str, Any]:
    """The shared pipeline; ``mutate(Sheet, new_root)`` changes the tree and returns an ``Outcome``."""
    try:
        return _run(Path(path), root_sch, mutate, allow_connectivity_change, dry_run)
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)
    except write_layer.WriteLayerError as exc:
        if (locked := write_layer.as_lock_refusal(exc)) is not None:
            return locked
        return {"status": "incomplete", "ok": False, "code": exc.code, "reason": str(exc), **exc.details}


def _run(path: Path, root_sch: str | None, mutate, allow: bool, dry_run: bool) -> dict[str, Any]:
    if not path.is_file():
        raise SheetAuthoringError("file_not_found", f"sheet not found: {path}")
    if not isinstance(allow, bool) or not isinstance(dry_run, bool):
        raise SheetAuthoringError("invalid_spec", "allow_connectivity_change and dry_run must be true or false")
    if not dry_run:
        write_layer.check_writable(path)
    tree, standalone = _tree_for(path, root_sch)
    original = write_layer.load(path)
    new = write_layer.load(path)
    if original.name != "kicad_sch":
        raise SheetAuthoringError("invalid_spec", f"{path.name} is not a schematic sheet")
    memo: dict[str, dict[str, frozenset]] = {}

    def nets_before() -> dict[str, frozenset]:
        if "nets" not in memo:
            memo["nets"] = read_nets(tree, path, None)
        return memo["nets"]

    outcome: Outcome = mutate(Sheet(new, nets_before), new)
    _staged_check(original, new, outcome)

    result: dict[str, Any] = {"status": "ok", "ok": True, "written": False, **outcome.report}
    if standalone:
        result["standalone_sheet"] = True
    if outcome.check_nets:
        before = nets_before()
        after = read_nets(tree, path, new)
        if outcome.post_check is not None:
            outcome.post_check(before, after)
        changes = (outcome.compare or compare_nets)(before, after, outcome.removed_refs)
        result["connectivity"] = {"nets_before": len(before), "nets_after": len(after), "changed": bool(changes),
                                  "changes": _bounded(changes)}
        if changes and not allow:
            raise SheetAuthoringError(
                "connectivity_changed",
                f"the change would alter connectivity ({len(changes)} change(s), first: {changes[0]}); nothing was written. "
                "Check the list, then repeat with allow_connectivity_change true if it is meant.",
                connectivity=result["connectivity"], **{k: v for k, v in outcome.report.items() if k in ("would_delete", "would_move")})
    if dry_run:
        return result
    if outcome.no_change:
        return result
    write_layer.save(new, path, verify=_expected_verify(new))
    reloaded = write_layer.load(path)
    if write_layer.serialise(reloaded, compact=True) != write_layer.serialise(new, compact=True):
        raise SheetAuthoringError(
            "write_not_verified",
            f"check reload_equals_serialised: {path.name} does not read back as the edited tree: "
            + (_self_check.describe_tree_difference(new, reloaded) or "the node order or the header differs"),
            check="reload_equals_serialised")
    result["written"] = True
    return result


# --------------------------------------------------------------------------- deletes


def _dangling_report(before: dict[str, list[str]], sheet: Sheet) -> tuple[dict[str, list[str]], dict[str, int]]:
    now = dangling(sheet)
    new = {k: [u for u in now[k] if u not in before[k]] for k in now}
    return new, {k: len(before[k]) for k in before}


def _describe(kind: str, node: SExp) -> dict[str, Any]:
    uid = _uuid(node)
    if kind == "symbol":
        x, y, a = _at(node)
        return {"reference": _ref(node), "uuid": uid, "lib_id": node.find_children("lib_id")[0].get_string(0), "at": _round_pt(x, y)}
    if kind == "wire":
        return {"uuid": uid, "points": [_round_pt(*e) for e in _wire_ends(node)]}
    x, y, _a = _at(node)
    if kind == "label":
        return {"text": node.get_string(0), "kind": node.name, "uuid": uid, "at": _round_pt(x, y)}
    return {"uuid": uid, "at": _round_pt(x, y)}


def delete(kind: str, path: str, selectors: list[dict], root_sch: str | None, *, remove_dangling: bool,
           allow_connectivity_change: bool, dry_run: bool) -> dict[str, Any]:
    """Delete the ``kind`` items the ``selectors`` pick (see ``select``)."""

    def mutate(sheet: Sheet, root: SExp) -> Outcome:
        if not isinstance(remove_dangling, bool):
            raise SheetAuthoringError("invalid_spec", "remove_dangling must be true or false")
        picked = select(sheet, kind, selectors)
        before = dangling(sheet)
        listed = [_describe(kind, n) for n in picked]
        outcome = Outcome(deleted={_uuid(n) for n in picked}, dangling_before=before)
        if kind == "symbol":
            outcome.removed_refs = {_ref(n) for n in picked}
            left = {_ref(n) for n in sheet.symbols if all(n is not p for p in picked)}
            outcome.removed_refs -= left  # a unit of a reference still placed elsewhere keeps its pins
        _remove(root, picked)
        removed_extra: list[dict[str, Any]] = []
        now = Sheet(root)
        new_dangling, preexisting = _dangling_report(before, now)
        if remove_dangling:
            for _ in range(len(now.wires) + len(now.junctions) + 1):
                fresh, _ = _dangling_report(before, now)
                doomed = [n for n in (*now.wires, *now.junctions) if _uuid(n) in set(fresh["wires"] + fresh["junctions"])]
                if not doomed:
                    break
                removed_extra += [{"kind": n.name, **_describe(n.name, n)} for n in doomed]
                outcome.deleted |= {_uuid(n) for n in doomed}
                _remove(root, doomed)
                now = Sheet(root)
            new_dangling, preexisting = _dangling_report(before, now)
        plural = {"symbol": "symbols", "wire": "wires", "label": "labels", "junction": "junctions"}[kind]
        outcome.report = {
            "deleted": listed, "count": len(listed),
            "dangling_after": {k: v for k, v in new_dangling.items()},
            "dangling_removed": removed_extra,
            "dangling_before_untouched": preexisting,
            "dangling_note": ("dangling wires and junctions the delete made were removed; labels are never removed" if remove_dangling
                              else f"report only: pass remove_dangling true to remove dangling wires and junctions left by deleting {plural}"),
        }
        return outcome

    result = run(path, root_sch, mutate, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_delete"] = result.pop("deleted")
    return result


# --------------------------------------------------------------------------- moves


def _snap(value: float, grid: float) -> float:
    return _r(round(value / grid) * grid)


def _check_move_args(snap: Any, grid: Any, move_wires: Any) -> None:
    if not isinstance(snap, bool) or not isinstance(move_wires, bool):
        raise SheetAuthoringError("invalid_spec", "snap and move_wires must be true or false")
    if isinstance(grid, bool) or not isinstance(grid, (int, float)) or grid <= 0:
        raise SheetAuthoringError("invalid_spec", f"grid must be a positive number of mm, got {grid!r}")


def _plan_moves(kind: str, moves: Any, sheet: Sheet, snap: bool, grid: float) -> list[tuple[SExp, float, float, dict]]:
    """Per node: the node, its delta and a record of what was asked and snapped."""
    if not isinstance(moves, list) or not moves:
        raise SheetAuthoringError("invalid_spec", "moves must be a non-empty list of {selector keys..., to | offset}")
    planned: list[tuple[SExp, float, float, dict]] = []
    seen: set[int] = set()
    for i, move in enumerate(moves):
        if not isinstance(move, dict):
            raise SheetAuthoringError("invalid_spec", f"moves[{i}] must be an object")
        spec = dict(move)
        to, offset, angle = spec.pop("to", None), spec.pop("offset", None), spec.pop("angle", None)
        if (to is None) == (offset is None) and not (kind == "label" and angle is not None and to is None and offset is None):
            raise SheetAuthoringError("invalid_spec", f"moves[{i}] needs exactly one of 'to' (absolute [x, y]) or 'offset' (relative [dx, dy])")
        if angle is not None and (kind != "label" or angle not in (0, 90, 180, 270)):
            raise SheetAuthoringError("invalid_spec", f"moves[{i}].angle is for labels only and must be 0, 90, 180 or 270")
        for name, val in (("to", to), ("offset", offset)):
            if val is not None and not (isinstance(val, (list, tuple)) and len(val) == 2 and all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) for v in val)):
                raise SheetAuthoringError("invalid_spec", f"moves[{i}].{name} must be [x, y] in mm, got {val!r}")
        nodes = select(sheet, kind, [spec])
        if to is not None and len(nodes) != 1:
            raise SheetAuthoringError("invalid_spec", f"moves[{i}]: 'to' needs a selector that picks exactly one {kind}, it picked {len(nodes)}; use 'offset'")
        for node in nodes:
            if id(node) in seen:
                raise SheetAuthoringError("invalid_spec", f"moves[{i}] picks a {kind} that an earlier move already moves ({_uuid(node)})")
            seen.add(id(node))
            x, y, _a = _at(node)
            want = (float(to[0]), float(to[1])) if to is not None else (x + float(offset[0]), y + float(offset[1]))
            got = (_snap(want[0], grid), _snap(want[1], grid)) if snap else (_r(want[0]), _r(want[1]))
            record = {"asked": _round_pt(*want), "snapped": [_r(got[0]), _r(got[1])] if snap and not _same(want, got) else None}
            if angle is not None:
                record["angle"] = angle
            planned.append((node, got[0] - x, got[1] - y, record))
    return planned


def move(kind: str, path: str, moves: list[dict], root_sch: str | None, *, move_wires: bool, snap: bool, grid: float,
         allow_connectivity_change: bool, dry_run: bool) -> dict[str, Any]:
    """Move symbols or labels (``kind`` "symbol" or "label")."""

    def mutate(sheet: Sheet, root: SExp) -> Outcome:
        _check_move_args(snap, grid, move_wires)
        planned = _plan_moves(kind, moves, sheet, snap, float(grid))
        outcome = Outcome(dangling_before=dangling(sheet))
        anchors: dict[tuple[float, float], tuple[float, float]] = {}
        listed = []
        for node, dx, dy, record in planned:
            x, y, a = _at(node)
            moved_pins_before: list[dict] = pin_positions(root, node) if kind == "symbol" else []
            entry: dict[str, Any] = {"uuid": _uuid(node), "from": _round_pt(x, y), "to": _round_pt(x + dx, y + dy), **record}
            if kind == "symbol":
                entry["reference"] = _ref(node)
            else:
                entry["text"] = node.get_string(0)
            outcome.changed.add(_uuid(node))
            anchor_list = [(p["x"], p["y"]) for p in moved_pins_before] if kind == "symbol" else [(x, y)]
            for point in anchor_list:
                key = _key(*point)
                if key in anchors and not (abs(anchors[key][0] - dx) <= TOL and abs(anchors[key][1] - dy) <= TOL):
                    raise SheetAuthoringError(
                        "ambiguous_drag", f"two moved items sit at {list(point)} and move differently, so the wire ends there cannot follow both; "
                        "move them in separate calls or with move_wires false")
                anchors[key] = (dx, dy)
            if abs(dx) > TOL / 10 or abs(dy) > TOL / 10:
                _set_at(node, x + dx, y + dy)
            if "angle" in record:
                write_layer.set_number(node.find_children("at")[0], 2, record["angle"])
            if kind == "symbol":
                for prop in _kids(node, "property"):
                    px, py, _pa = _at(prop)
                    _set_at(prop, px + dx, py + dy)
                entry["pins"] = pin_positions(root, node)
            listed.append(entry)
        followed: dict[str, list] = {"wires": [], "junctions": [], "labels": [], "no_connects": []}
        slanted = []
        if move_wires and anchors:
            def delta_at(px: float, py: float):
                return anchors.get(_key(px, py))

            moved_ids = {_uuid(n) for n, *_ in planned}
            for wire in sheet.wires:
                ends = _wire_ends(wire)
                new_ends = list(ends)
                for idx in (0, len(ends) - 1):
                    d = delta_at(*ends[idx])
                    if d is not None and (abs(d[0]) > TOL / 10 or abs(d[1]) > TOL / 10):
                        new_ends[idx] = (ends[idx][0] + d[0], ends[idx][1] + d[1])
                if new_ends != ends:
                    outcome.changed.add(_uuid(wire))
                    for idx in (0, len(ends) - 1):
                        if new_ends[idx] != ends[idx]:
                            _set_wire_end(wire, idx, *new_ends[idx])
                    rec = {"uuid": _uuid(wire), "was": [_round_pt(*e) for e in ends], "now": [_round_pt(*e) for e in new_ends]}
                    followed["wires"].append(rec)
                    if len(new_ends) >= 2 and abs(new_ends[0][0] - new_ends[-1][0]) > TOL and abs(new_ends[0][1] - new_ends[-1][1]) > TOL:
                        slanted.append(_uuid(wire))
            for group, nodes in (("junctions", sheet.junctions), ("labels", sheet.labels), ("no_connects", sheet.no_connects)):
                for node in nodes:
                    if _uuid(node) in moved_ids:
                        continue
                    x, y, _a = _at(node)
                    d = delta_at(x, y)
                    if d is not None and (abs(d[0]) > TOL / 10 or abs(d[1]) > TOL / 10):
                        _set_at(node, x + d[0], y + d[1])
                        outcome.changed.add(_uuid(node))
                        followed[group].append({"uuid": _uuid(node), "was": _round_pt(x, y), "now": _round_pt(x + d[0], y + d[1])})
        outcome.report = {
            "moved": listed, "count": len(listed),
            "grid": float(grid) if snap else None,
            "snapped": [e["uuid"] for e in listed if e.get("snapped")],
            "move_wires": move_wires, "followed": followed, "slanted": slanted,
            "move_wires_note": ("wire ends, junctions, labels and no-connect markers that sat exactly on a moved pin (or on a moved label) moved with it"
                                if move_wires else "move_wires is false: nothing attached was moved; wires that ended on a moved pin now end where the pin was"),
        }
        return outcome

    result = run(path, root_sch, mutate, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_move"] = result.pop("moved")
    return result


# --------------------------------------------------------------------------- property texts

_JUSTIFY_WORDS = ("left", "right", "top", "bottom", "mirror")


def _justify_words(value: Any, where: str) -> list[str]:
    if not isinstance(value, str):
        raise SheetAuthoringError("invalid_spec", f"{where}.justify must be a string such as 'left bottom' ('' clears it)")
    words = value.split()
    if any(w not in _JUSTIFY_WORDS for w in words) or len(set(words)) != len(words):
        raise SheetAuthoringError("invalid_spec", f"{where}.justify words must be drawn once each from {list(_JUSTIFY_WORDS)}, got {value!r}")
    if "left" in words and "right" in words or "top" in words and "bottom" in words:
        raise SheetAuthoringError("invalid_spec", f"{where}.justify holds two opposite words: {value!r}")
    order = {"left": 0, "right": 0, "top": 1, "bottom": 1, "mirror": 2}
    return sorted(words, key=lambda w: order[w])


def set_property_positions(path: str, items: list[dict], root_sch: str | None, *, dry_run: bool) -> dict[str, Any]:
    """Set position, angle and justification of symbol property texts."""

    def mutate(sheet: Sheet, root: SExp) -> Outcome:
        if not isinstance(items, list) or not items:
            raise SheetAuthoringError("invalid_spec", "items must be a non-empty list of {selector keys..., properties: {...}}")
        outcome = Outcome(check_nets=False)
        listed, seen = [], set()
        for i, item in enumerate(items):
            if not isinstance(item, dict) or not isinstance(item.get("properties"), dict) or not item["properties"]:
                raise SheetAuthoringError("invalid_spec", f"items[{i}] needs 'properties': {{name: {{offset|at, angle, justify}}}}")
            spec = {k: v for k, v in item.items() if k != "properties"}
            nodes = select(sheet, "symbol", [spec])
            for name, change in item["properties"].items():
                where = f"items[{i}].properties[{name!r}]"
                if not isinstance(change, dict) or not change or set(change) - {"at", "offset", "angle", "justify"}:
                    raise SheetAuthoringError("invalid_spec", f"{where} must be an object with keys from ['at', 'offset', 'angle', 'justify']")
                if "at" in change and "offset" in change:
                    raise SheetAuthoringError("invalid_spec", f"{where} takes 'at' (absolute) or 'offset' (from the symbol's origin), not both")
                for key in ("at", "offset"):
                    if key in change and not (isinstance(change[key], (list, tuple)) and len(change[key]) == 2 and all(
                            isinstance(v, (int, float)) and not isinstance(v, bool) for v in change[key])):
                        raise SheetAuthoringError("invalid_spec", f"{where}.{key} must be [x, y] in mm")
                if "at" in change and len(nodes) != 1:
                    raise SheetAuthoringError("invalid_spec", f"{where}: 'at' needs a selector that picks one symbol node; use 'offset'")
                if "angle" in change and change["angle"] not in (0, 90, 180, 270):
                    raise SheetAuthoringError("invalid_spec", f"{where}.angle must be 0, 90, 180 or 270")
                words = _justify_words(change["justify"], where) if "justify" in change else None
                for node in nodes:
                    if (id(node), name) in seen:
                        raise SheetAuthoringError("invalid_spec", f"{where} is set twice for {_ref(node)}")
                    seen.add((id(node), name))
                    prop = _property(node, name)
                    if prop is None:
                        raise SheetAuthoringError(
                            "property_not_found", f"{_ref(node)} has no property {name!r} (it has {[p.get_string(0) for p in _kids(node, 'property')]}); nothing was written")
                    ox, oy, _a = _at(node)
                    _px, _py, pa = _at(prop)
                    if "at" in change:
                        _set_at(prop, float(change["at"][0]), float(change["at"][1]))
                    elif "offset" in change:
                        _set_at(prop, ox + float(change["offset"][0]), oy + float(change["offset"][1]))
                    if "angle" in change:
                        write_layer.set_number(prop.find_children("at")[0], 2, change["angle"])
                    if words is not None:
                        effects = prop.find_children("effects")[0]
                        _remove(effects, _kids(effects, "justify"))
                        if words:
                            font = [k for k, c in enumerate(effects.children) if not c.is_atom and c.name == "font"]
                            effects.children.insert((font[0] + 1) if font else 0, SExp.list("justify", *(write_layer.symbol(w) for w in words)))
                    outcome.changed.add(_uuid(node))
                    for auto in _kids(node, "fields_autoplaced"):
                        _remove(node, [auto])  # the texts are no longer where KiCad's auto-placement put them
                    px, py, pa = _at(prop)
                    justify = [w for j in _kids(prop.find_children("effects")[0], "justify") for w in (a.value for a in j.children)]
                    listed.append({"reference": _ref(node), "uuid": _uuid(node), "property": name, "at": _round_pt(px, py), "angle": pa,
                                   "justify": " ".join(justify), "symbol_at": _round_pt(ox, oy), "pins": pin_positions(root, node)})
        outcome.report = {"updated": listed, "count": len(listed),
                          "note": "pins are listed so the texts can be put clear of them; fields_autoplaced is removed from each edited symbol"}
        return outcome

    result = run(path, root_sch, mutate, allow_connectivity_change=False, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_update"] = result.pop("updated")
    return result


# --------------------------------------------------------------------------- replace_symbols (issue 112)

_NO_NUMBER = ("", "~")
_REPLACE_KEYS = {"reference", "uuid", "lib_id", "pin_map"}


def _placed(sheet: Sheet) -> list[SExp]:
    return [s for s in sheet.symbols if s.find_children("lib_id")]


def _lib_name_of(symbol: SExp) -> str:
    lib = symbol.find_children("lib_name")
    if lib:
        return lib[0].get_string(0) or ""
    return symbol.find_children("lib_id")[0].get_string(0) or ""


def _unit_of(symbol: SExp) -> int:
    return (symbol.find_children("unit")[0].get_int(0) if symbol.find_children("unit") else 1) or 1


def _library_units(entry: SExp) -> set[int]:
    """The unit numbers a library symbol draws (from its ``<name>_<unit>_<style>`` children; 0 is shared)."""
    units: set[int] = set()
    for sub in _kids(entry, "symbol"):
        parts = (sub.get_string(0) or "").rsplit("_", 2)
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            units.add(int(parts[1]))
    return units


def _load_library_entry(sheet_path: Path, lib_id: str) -> list[SExp]:
    """The embeddable ``(symbol "Lib:Name" ...)`` node of ``lib_id`` read from its library file (the
    project's tables first, then KiCad's own folder), the same source ``add_symbols`` seeds from."""
    from kicad_tools.schematic.models.symbol import SymbolDef

    from kicad_mcp import symbol_libs

    resolution = symbol_libs.resolve_symbol_libraries(sheet_path, [lib_id])
    if resolution.problems:
        raise SheetAuthoringError("lib_unresolved", f"cannot resolve {lib_id!r}: {resolution.reason}; nothing was written", lib_id=lib_id)
    nick = lib_id.split(":", 1)[0]
    folders = [resolution.files[nick].parent] if nick in resolution.files else symbol_libs.kicad_symbol_dirs()
    last: Exception | None = None
    for folder in folders:
        try:
            nodes = SymbolDef.from_library(lib_id, lib_paths=[folder]).to_sexp_nodes()
        except Exception as exc:  # noqa: BLE001 - reported as a refusal naming the library
            last = exc
            continue
        if nodes:
            return nodes
    raise SheetAuthoringError("lib_unresolved", f"cannot read {lib_id!r} from its library: {last}; nothing was written", lib_id=lib_id)


def _attached(sheet: Sheet, point: tuple[float, float], own: SExp) -> list[str]:
    """What sits exactly on ``point`` besides the pins of ``own``: wire ends, labels, junctions,
    no-connect markers, other symbols' pins and sheet pins. A wire that only runs over it joins nothing."""
    found = []
    for wire in sheet.wires:
        ends = _wire_ends(wire)
        if ends and (_same(ends[0], point) or _same(ends[-1], point)):
            found.append(f"wire {_uuid(wire)}")
    for group, nodes in (("label", sheet.labels), ("junction", sheet.junctions), ("no_connect", sheet.no_connects)):
        for node in nodes:
            x, y, _a = _at(node)
            if _same((x, y), point):
                found.append(f"{group} {_uuid(node)}")
    for sym in sheet.symbols:
        if sym is own or not sym.find_children("lib_id"):
            continue
        for pin in pin_positions(sheet.root, sym):
            if _same((pin["x"], pin["y"]), point):
                found.append(f"pin {_ref(sym)}.{pin['number']}")
    for node in sheet.sheets:
        for pin in _kids(node, "pin"):
            x, y, _a = _at(pin)
            if _same((x, y), point):
                found.append(f"sheet pin {pin.get_string(0)}")
    return found


def _pair_pins(old: list[dict], new: list[dict], pin_map: dict[str, str], who: str) -> tuple[list[tuple[dict, dict]], list[dict]]:
    """Pair the old pins with the new ones: the explicit ``pin_map`` (old number -> new number) first,
    then the same pin number, then, for a pin that has no number, the same pin name. Returns the pairs
    and the old pins with no counterpart."""
    old_numbers = {p["number"] for p in old}
    new_by_number: dict[str, dict] = {}
    for p in new:
        new_by_number.setdefault(p["number"], p)
    new_by_name: dict[str, dict] = {}
    for p in new:
        if p["number"] in _NO_NUMBER:
            new_by_name.setdefault(p["name"], p)
    bad_keys = sorted(k for k in pin_map if k not in old_numbers)
    if bad_keys:
        raise SheetAuthoringError("invalid_pin_map", f"{who}: pin_map names pins the old symbol does not have: {bad_keys} (it has {sorted(old_numbers)}); nothing was written")
    bad_values = sorted(v for v in pin_map.values() if v not in new_by_number)
    if bad_values:
        raise SheetAuthoringError("invalid_pin_map", f"{who}: pin_map names pins the new symbol does not have: {bad_values} (it has {sorted(new_by_number)}); nothing was written")
    if len(set(pin_map.values())) != len(pin_map):
        raise SheetAuthoringError("invalid_pin_map", f"{who}: pin_map sends two old pins to one new pin; nothing was written")
    pairs: list[tuple[dict, dict]] = []
    unmapped: list[dict] = []
    taken = set(pin_map.values())
    taken_ids: set[int] = set()
    for o in old:
        if o["number"] in pin_map:
            target = new_by_number[pin_map[o["number"]]]
        elif o["number"] not in _NO_NUMBER and o["number"] in new_by_number and o["number"] not in taken:
            target = new_by_number[o["number"]]
            taken.add(o["number"])
        elif o["number"] in _NO_NUMBER and o["name"] in new_by_name and id(new_by_name[o["name"]]) not in taken_ids:
            target = new_by_name[o["name"]]
            taken_ids.add(id(target))
        else:
            unmapped.append(o)
            continue
        pairs.append((o, target))
    return pairs, unmapped


def _check_replace_specs(replacements: Any) -> list[dict]:
    if not isinstance(replacements, list) or not replacements:
        raise SheetAuthoringError("invalid_spec", "replacements must be a non-empty list of {reference | uuid, lib_id, pin_map?} objects")
    for i, spec in enumerate(replacements):
        if not isinstance(spec, dict):
            raise SheetAuthoringError("invalid_spec", f"replacements[{i}] must be an object")
        unknown = sorted(set(spec) - _REPLACE_KEYS)
        if unknown:
            raise SheetAuthoringError("invalid_spec", f"replacements[{i}] has unknown keys {unknown}; allowed: {sorted(_REPLACE_KEYS)}")
        if ("reference" in spec) == ("uuid" in spec):
            raise SheetAuthoringError("invalid_spec", f"replacements[{i}] needs exactly one of reference and uuid")
        for key in ("reference", "uuid", "lib_id"):
            if key in spec and not (isinstance(spec[key], str) and spec[key]):
                raise SheetAuthoringError("invalid_spec", f"replacements[{i}].{key} must be a non-empty string")
        if "lib_id" not in spec or ":" not in spec["lib_id"]:
            raise SheetAuthoringError("invalid_spec", f"replacements[{i}].lib_id must be of the form Library:Symbol")
        pm = spec.get("pin_map", {})
        if not isinstance(pm, dict) or not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in pm.items()):
            raise SheetAuthoringError("invalid_spec", f"replacements[{i}].pin_map must map old pin numbers to new pin numbers (strings)")
    return replacements


def _uses(sheet: Sheet, name: str) -> list[str]:
    return [_ref(s) for s in _placed(sheet) if _lib_name_of(s) == name]


def _swap_symbol_library(node: SExp, lib_id: str, new_numbers: list[str], pin_uuids: dict[str, str]) -> None:
    """The only change to the placed symbol: its ``lib_id`` (and ``lib_name``, which would keep naming the old
    entry), and its ``(pin "n" (uuid ..))`` list, which follows the new library symbol's pins."""
    import uuid as _uuidlib

    write_layer.set_string(node.find_children("lib_id")[0], 0, lib_id)
    for stale in node.find_children("lib_name"):
        _remove(node, [stale])
    old_pins = _kids(node, "pin")
    if old_pins:
        anchor = node.children.index(old_pins[0])
    elif node.find_children("instances"):
        anchor = node.children.index(node.find_children("instances")[0])
    else:
        anchor = len(node.children)
    _remove(node, old_pins)
    fresh = [SExp.list("pin", write_layer.string(n), SExp.list("uuid", write_layer.string(pin_uuids.get(n) or str(_uuidlib.uuid4())))) for n in new_numbers]
    node.children[anchor:anchor] = fresh


def _swap_check(node: SExp, before: list[str], lib_id: str, who: str) -> None:
    """Self-check: every child of the placed symbol except ``lib_id``, ``lib_name`` and the pin list is byte-equal to before."""
    now = [write_layer.serialise(c, compact=True) for c in node.children if c.is_atom or c.name not in ("lib_id", "lib_name", "pin")]
    if now != before or node.find_children("lib_id")[0].get_string(0) != lib_id:
        raise SheetAuthoringError(
            "write_not_verified", f"check symbol_children_unchanged: {who}: the replaced symbol differs from the original beyond lib_id, lib_name and pins; nothing was written",
            check="symbol_children_unchanged", subject=who)


def replace_symbols(path: str, replacements: Any, root_sch: str | None, *, allow_connectivity_change: bool, dry_run: bool) -> dict[str, Any]:
    """Replace placed symbols by another ``lib_id`` in place (issue 112): same uuid, reference, position, rotation,
    mirror, unit, properties and instance data; the embedded library symbol is added (or reused) and the old one
    dropped when no other symbol uses it. Wires are never dragged."""

    def mutate(sheet: Sheet, root: SExp) -> Outcome:
        specs = _check_replace_specs(replacements)
        placed = _placed(sheet)
        table = next(iter(_kids(root, "lib_symbols")), None)
        if table is None:
            raise SheetAuthoringError("invalid_spec", "the sheet has no lib_symbols table")
        chosen: list[tuple[SExp, dict]] = []
        seen: set[str] = set()
        for i, spec in enumerate(specs):
            if "uuid" in spec:
                hits = [s for s in placed if _uuid(s) == spec["uuid"]]
            else:
                hits = [s for s in placed if _ref(s) == spec["reference"]]
            label = spec.get("uuid") or spec.get("reference")
            if not hits:
                raise SheetAuthoringError("not_found", f"replacements[{i}]: no placed symbol on this sheet matches {label!r}; nothing was written", not_found=[label])
            if len(hits) > 1:
                raise SheetAuthoringError("ambiguous_reference", f"replacements[{i}]: {label!r} matches {len(hits)} placed symbols; select one by uuid; nothing was written")
            node = hits[0]
            if _uuid(node) in seen:
                raise SheetAuthoringError("invalid_spec", f"replacements[{i}]: {_ref(node)} ({_uuid(node)}) is picked twice")
            seen.add(_uuid(node))
            others = [s for s in placed if s is not node and _ref(s) == _ref(node) and _unit_of(s) != _unit_of(node)]
            if others:
                raise SheetAuthoringError(
                    "multi_unit_symbol", f"{_ref(node)} has other units placed on this sheet (units {sorted(_unit_of(s) for s in others)}); "
                    "replace_symbols changes one placed unit per symbol, so a multi-unit part would be left half replaced; nothing was written")
            chosen.append((node, spec))

        outcome = Outcome()
        report: list[dict] = []
        extras: list[dict] = []
        unmapped_wired: list[dict] = []
        old_names: list[str] = []
        added: dict[str, str] = {}  # lib_id -> "added" | "reused"
        for node, spec in chosen:
            ref, uid, lib_id = _ref(node), _uuid(node), spec["lib_id"]
            who = f"{ref} ({uid})"
            old_lib_id = node.find_children("lib_id")[0].get_string(0)
            if lib_id == old_lib_id:
                raise SheetAuthoringError("same_lib_id", f"{who} already uses {lib_id}; nothing was written")
            old_pins = pin_positions(root, node, with_type=True)
            old_name = _lib_name_of(node)
            if lib_id not in added:
                if any(e.get_string(0) == lib_id for e in _kids(table, "symbol")):
                    added[lib_id] = "reused"
                else:
                    for entry in _load_library_entry(Path(path), lib_id):
                        table.children.append(entry)
                        outcome.lib_added.add(entry.get_string(0) or lib_id)
                    added[lib_id] = "added"
            entry = next((e for e in _kids(table, "symbol") if e.get_string(0) == lib_id), None)
            if entry is None:
                raise SheetAuthoringError("lib_unresolved", f"the library file of {lib_id!r} has no symbol of that name; nothing was written", lib_id=lib_id)
            unit = _unit_of(node)
            if unit not in _library_units(entry) and not (unit == 1 and not _library_units(entry)):
                raise SheetAuthoringError("unit_missing", f"{who} is unit {unit} but {lib_id} has units {sorted(_library_units(entry) - {0})}; nothing was written")
            snapshot = [write_layer.serialise(c, compact=True) for c in node.children if c.is_atom or c.name not in ("lib_id", "lib_name", "pin")]
            old_pin_uuids = {p.get_string(0): _uuid(p) for p in _kids(node, "pin")}
            # swap first with no pin list, then read the new pin points with the maths get_symbol_pins uses
            _swap_symbol_library(node, lib_id, [], {})
            new_pins = pin_positions(root, node, with_type=True)
            pairs, unmapped = _pair_pins(old_pins, new_pins, spec.get("pin_map", {}), who)
            keep_uuid = {n["number"]: old_pin_uuids[o["number"]] for o, n in pairs if old_pin_uuids.get(o["number"])}
            _swap_symbol_library(node, lib_id, [p["number"] for p in new_pins], keep_uuid)
            _swap_check(node, snapshot, lib_id, who)
            outcome.changed.add(uid)

            for o in unmapped:
                found = _attached(sheet, (o["x"], o["y"]), node)
                if found:
                    unmapped_wired.append({"reference": ref, "pin": o["number"], "name": o["name"], "at": [o["x"], o["y"]], "attached": found[:5]})
            pair_report = []
            for o, n in pairs:
                found = _attached(sheet, (o["x"], o["y"]), node)
                moved = not _same((o["x"], o["y"]), (n["x"], n["y"]))
                pair_report.append({"old_pin": o["number"], "new_pin": n["number"], "name": n["name"], "was": [o["x"], o["y"]], "now": [n["x"], n["y"]],
                                    "wired": bool(found), "moved": moved})
                if found and moved:
                    extras.append({"type": "pin_moved", "reference": ref, "old_pin": o["number"], "new_pin": n["number"],
                                   "was": [o["x"], o["y"]], "now": [n["x"], n["y"]], "attached": found[:5],
                                   "note": "the wires stay where they are; the new pin is not on them"})
            paired_new = {n["number"] for _o, n in pairs}
            for n in new_pins:
                if n["number"] in paired_new:
                    continue
                found = _attached(sheet, (n["x"], n["y"]), node)
                if found:
                    extras.append({"type": "new_pin_attached", "reference": ref, "pin": n["number"], "at": [n["x"], n["y"]], "attached": found[:5]})
            if old_name not in old_names:
                old_names.append(old_name)
            x, y, rot = _at(node)
            report.append({
                "reference": ref, "uuid": uid, "old_lib_id": old_lib_id, "lib_id": lib_id, "library_symbol": added[lib_id],
                "at": _round_pt(x, y), "rotation": rot, "mirror": _mirror(node), "unit": unit,
                "pin_map": pair_report,
                "unmapped_old_pins": [{"pin": o["number"], "name": o["name"], "at": [o["x"], o["y"]]} for o in unmapped],
                "pins": new_pins,
            })
        if unmapped_wired and not allow_connectivity_change:
            raise SheetAuthoringError(
                "pin_unmapped", f"{len(unmapped_wired)} wired pin(s) of the old symbol have no counterpart on the new one "
                f"(first: {unmapped_wired[0]['reference']}.{unmapped_wired[0]['pin']}); give a pin_map, or repeat with "
                "allow_connectivity_change true if dropping them is meant; nothing was written", unmapped=unmapped_wired[:MAX_LISTED])
        dropped, kept = [], []
        for name in old_names:
            if name in added:
                continue
            entry = next((e for e in _kids(table, "symbol") if e.get_string(0) == name), None)
            if entry is None:
                continue
            users = _uses(sheet, name)
            if users:
                kept.append({"library_symbol": name, "still_used_by": users[:MAX_LISTED]})
            else:
                _remove(table, [entry])
                outcome.lib_removed.add(name)
                dropped.append(name)
        outcome.report = {
            "replaced": report, "count": len(report), "dropped_library_symbols": dropped, "kept_library_symbols": kept,
            "unmapped_wired_pins": unmapped_wired,
            "note": "uuid, reference, position, rotation, mirror, unit, properties and instance data are unchanged; wires are not moved, "
                    "so a wired pin whose counterpart lies elsewhere is reported as connectivity_changed"}
        outcome.compare = lambda before, after, removed: [*extras, *compare_nets(before, after, removed)]
        return outcome

    result = run(path, root_sch, mutate, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_replace"] = result.pop("replaced")
    return result
