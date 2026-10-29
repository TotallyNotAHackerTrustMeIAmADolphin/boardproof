"""The acceptance gate: the only thing allowed to call a board routed or done.

Contract: pivot-ladder ticket 3 and
docs/ARCHITECTURE.md decision 14. Every figure comes from ``kicad-cli`` output,
never from ``kicad_tools`` properties or a router's own score. The rule
settings DRC grades against are read from the project files only to detect
tampering (a downgrade leaves no trace in the report itself).
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, get_args

from kicad_mcp import kicad_cli

Parity = Literal["required", "not_applicable"]
PARITY_MODES: tuple[str, ...] = get_args(Parity)

# The gate always runs kicad-cli this many times (decision 14); a caller cannot lower it.
GATE_RUNS = 3

# KiCad stops reporting at these counts per violation code, silently
# (drc_engine.cpp): a count exactly at the cap means "at least this many".
_CAP_DEFAULT = 199
_CAP_UNCONNECTED_AND_CLEARANCE = 499

_MAX_COPY_BYTES = 300 * 1024 * 1024
_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}

# One level of nested brackets is part of the name (a bus net such as DATA[0]).
_NET_TOKEN = re.compile(r"\[((?:[^\[\]]|\[[^\]]*\])+)\]")
_DRU_SEVERITY = re.compile(r"\(\s*severity\s+\w+\s*\)")

_Drc = Callable[..., dict]


# --- the graded copy ---------------------------------------------------------


def _directory_size(root: Path) -> int:
    return sum(f.stat().st_size for f in root.rglob("*") if f.is_file())


def _copy_project(pcb_path: Path, dest: Path) -> Path:
    """Copy the board's whole project directory (sheets, libraries, rules, lib
    tables) so ``kicad-cli`` sees what it would see in place, and the original is
    never touched. Other boards in the same directory are left out."""
    source = pcb_path.parent
    if _directory_size(source) > _MAX_COPY_BYTES:
        raise ValueError(f"project directory {source} is too large to copy for grading")

    def _ignore(directory: str, names: list[str]) -> list[str]:
        skipped = [n for n in names if n in _SKIP_DIRS or n.endswith("-backups")]
        if Path(directory) == source:
            skipped += [n for n in names if n.endswith(".kicad_pcb") and n != pcb_path.name]
        return skipped

    shutil.copytree(source, dest, ignore=_ignore, dirs_exist_ok=True)
    return dest / pcb_path.name


@contextmanager
def _graded_copy(pcb_path: Path) -> Iterator[Path]:
    with tempfile.TemporaryDirectory() as tmp:
        yield _copy_project(pcb_path, Path(tmp))


# --- reading a report --------------------------------------------------------


def _net_of(entry: dict) -> str | None:
    """The net of an unconnected-items entry, from the bracketed token."""
    for item in entry.get("items", []):
        match = _NET_TOKEN.search(item.get("description", ""))
        if match:
            return match.group(1)
    return None


def _positions(entry: dict) -> tuple:
    """Rounded, sorted item positions of an entry.

    Item uuids are NOT usable as identity: pads without a ``(uuid)`` in the file
    get a fresh runtime uuid on every kicad-cli run (observed on the golden
    board), so the same violation would look new each time.
    """
    return tuple(
        sorted(
            (round(i.get("pos", {}).get("x", 0.0), 3), round(i.get("pos", {}).get("y", 0.0), 3))
            for i in entry.get("items", [])
        )
    )


def _signature(violation: dict) -> tuple:
    """Identity of a violation across runs: type + item descriptions and positions."""
    items = sorted(
        (
            item.get("description", ""),
            round(item.get("pos", {}).get("x", 0.0), 3),
            round(item.get("pos", {}).get("y", 0.0), 3),
        )
        for item in violation.get("items", [])
    )
    return (violation.get("type"), tuple(items))


def _severity_class(violation: dict) -> str:
    """``"error"`` or ``"warning"``.

    A marker KiCad reports as excluded keeps its underlying severity when it has
    one; if only the string ``exclusion`` is present the underlying severity is
    unknown and counts as an error (the gate has no waivers). Anything else
    unrecognised also counts as an error and is surfaced separately.
    """
    severity = violation.get("severity")
    if severity in ("error", "warning"):
        return severity
    return "error"


def _classify(report: dict) -> tuple[dict[tuple, dict], dict[tuple, dict], set[str]]:
    """Errors and warnings of one report (violations and schematic parity),
    de-duplicated by signature, plus any severity string we do not recognise."""
    errors: dict[tuple, dict] = {}
    warnings: dict[tuple, dict] = {}
    unknown: set[str] = set()
    for violation in [*report.get("violations", []), *report.get("schematic_parity", [])]:
        severity = violation.get("severity")
        if severity not in ("error", "warning", "exclusion"):
            unknown.add(str(severity))
        target = errors if _severity_class(violation) == "error" else warnings
        target[_signature(violation)] = violation
    return errors, warnings, unknown


def _unconnected(report: dict) -> dict[tuple, dict]:
    """Unconnected entries keyed by (net, positions). An entry whose net could not be
    parsed gets an ordinal so distinct entries never collapse into one."""
    found: dict[tuple, dict] = {}
    for ordinal, entry in enumerate(report.get("unconnected_items", [])):
        net = _net_of(entry)
        found[(net, _positions(entry), ordinal if net is None else 0)] = entry
    return found


def _union(per_run: list[dict[tuple, dict]]) -> dict[tuple, dict]:
    merged: dict[tuple, dict] = {}
    for found in per_run:
        merged.update(found)
    return merged


def _truncated_codes(report: dict) -> list[str]:
    counts: dict[str, int] = {}
    for violation in report.get("violations", []):
        code = violation.get("type", "?")
        counts[code] = counts.get(code, 0) + 1
    counts["unconnected_items"] = len(report.get("unconnected_items", []))
    hit = []
    for code, count in counts.items():
        cap = (
            _CAP_UNCONNECTED_AND_CLEARANCE
            if code in ("clearance", "unconnected_items")
            else _CAP_DEFAULT
        )
        if count == cap:
            hit.append(code)
    return hit


# --- the rules the board is graded against ------------------------------------


def _strip_colors(value: Any) -> Any:
    """Drop cosmetic colour keys so a recolour is not read as a rules change."""
    if isinstance(value, dict):
        return {k: _strip_colors(v) for k, v in value.items() if not k.endswith("_color")}
    if isinstance(value, list):
        return [_strip_colors(v) for v in value]
    return value


def _current_rules(pcb_path: Path, ignored_checks: list[str]) -> dict[str, Any]:
    """The rule settings the gate pins and compares: everything that can change
    what DRC reports without leaving a trace in the report itself."""
    rules: dict[str, Any] = {
        "rule_severities": {},
        "drc_exclusions": [],
        "constraints": {},
        "net_settings": {},
        "dru_severity_clauses": [],
        "dru_sha256": None,
        "ignored_checks": sorted(ignored_checks),
    }
    project = pcb_path.with_suffix(".kicad_pro")
    if project.is_file():
        data = json.loads(project.read_text(encoding="utf-8"))
        design = data.get("board", {}).get("design_settings", {})
        rules["rule_severities"] = dict(sorted(design.get("rule_severities", {}).items()))
        rules["drc_exclusions"] = sorted(map(str, design.get("drc_exclusions", [])))
        rules["constraints"] = design.get("rules", {})
        rules["net_settings"] = _strip_colors(data.get("net_settings", {}))
    dru = pcb_path.with_suffix(".kicad_dru")
    if dru.is_file():
        text = re.sub(r"\s+", " ", dru.read_text(encoding="utf-8")).strip()
        rules["dru_sha256"] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        rules["dru_severity_clauses"] = sorted(
            re.sub(r"\s+", " ", match.group(0)) for match in _DRU_SEVERITY.finditer(text)
        )
    return rules


def _ignored_keys(report: dict) -> list[str]:
    return [c.get("key", "?") for c in report.get("ignored_checks", [])]


def snapshot_rules(pcb_path: str | Path, *, drc: _Drc | None = None) -> dict:
    """Pin the rule settings a board is graded against (one DRC run for ``ignored_checks``)."""
    pcb_path = Path(pcb_path)
    run_drc = drc or kicad_cli.run_drc
    with _graded_copy(pcb_path) as board:
        report = run_drc(board, refill_zones=True, schematic_parity=False)
    return _current_rules(pcb_path, _ignored_keys(report))


# --- schematic parity preconditions -------------------------------------------


_SHEET_FILE_KEYS = ("Sheetfile", "Sheet file")  # KiCad 7+ / KiCad 6 spelling
_SHEET_NAME_KEYS = ("Sheetname", "Sheet name")
_NO_REFERENCE = "<no reference>"


def _node_atom(node: Any, index: int = 0) -> str:
    return node.get_string(index) or ""


def _property_value(node: Any, keys: tuple[str, ...]) -> str | None:
    for prop in node.find_children("property"):
        if _node_atom(prop, 0) in keys:
            return _node_atom(prop, 1)
    return None


def _read_sheet(root: Any, project: str) -> dict:
    """What one parsed sheet says about annotation and hierarchy.

    Only direct children of the root are read: ``lib_symbols`` keeps ``?`` references
    by design and is never looked at. Symbols marked ``(on_board no)`` are deliberately
    still counted (conservative: an unannotated one is still a defect to the schematic).

    Returns ``references`` (resolved placed references), ``deferred`` (symbols with no
    instance data of THIS project: ``(uuid, property references)``, resolved later
    against KiCad-6 ``symbol_instances``), ``legacy`` (uuid -> references from
    ``symbol_instances``), ``sub_sheets`` (relative file names) and ``problems``.
    """
    references: list[str] = []
    deferred: list[tuple[str, list[str]]] = []
    for symbol in root.find_children("symbol"):
        property_refs = [
            _node_atom(prop, 1)
            for prop in symbol.find_children("property")
            if _node_atom(prop, 0) == "Reference"
        ]
        own: list[str] = []
        for instances in symbol.find_children("instances"):
            for proj in instances.find_children("project"):
                if _node_atom(proj, 0) != project:
                    continue  # another project's data says nothing about this board
                found = [
                    _node_atom(ref, 0)
                    for path in proj.find_children("path")
                    for ref in path.find_children("reference")
                ]
                own.extend(found or [""])
        if own:
            references.extend(own)  # KiCad uses the instance reference over the property
        else:
            uuid_nodes = symbol.find_children("uuid")
            uuid = _node_atom(uuid_nodes[0], 0) if uuid_nodes else ""
            deferred.append((uuid, property_refs))
    legacy: dict[str, list[str]] = {}
    for table in root.find_children("symbol_instances"):
        for path in table.find_children("path"):
            tail = _node_atom(path, 0).rsplit("/", 1)[-1]
            for ref in path.find_children("reference"):
                legacy.setdefault(tail, []).append(_node_atom(ref, 0))
    sub_sheets: list[str] = []
    problems: list[str] = []
    for sheet in root.find_children("sheet"):
        filename = _property_value(sheet, _SHEET_FILE_KEYS)
        if filename:
            sub_sheets.append(filename)
            continue
        name = _property_value(sheet, _SHEET_NAME_KEYS) or "<unnamed>"
        at = sheet.find_children("at")
        where = f" at ({at[0].get_float(0)}, {at[0].get_float(1)})" if at else ""
        problems.append(
            f"sheet '{name}'{where} has no sheet file property: its sub-hierarchy "
            "cannot be verified"
        )
    return {
        "references": references,
        "deferred": deferred,
        "legacy": legacy,
        "sub_sheets": sub_sheets,
        "problems": problems,
    }


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _annotation_problems(root_sheet: Path) -> list[str]:
    """Reasons the schematic hierarchy is not provably fully annotated (fail closed).

    Reads placed symbol instances from the parsed root sheet and every sub-sheet
    reached through ``(sheet ... (property "Sheetfile" ...))``, each resolved relative
    to the sheet that contains it. A ``?`` anywhere in a placed reference is
    unannotated; ``#``-prefixed references (power symbols and flags) do not count.
    A missing, unparseable, outside-the-project or cyclic sheet is a reason, never a
    pass. Measured on kicad-cli (2026-10-11): parity still runs for numberless, ``R0`` and
    ``#PWR?`` references, so only ``?`` is treated as unannotated. A symbol whose instance
    entries miss its sheet use is a reason too (``_stale_instance_problems``, build ticket 86).
    """
    from kicad_tools.sexp.parser import parse_file

    project = root_sheet.stem
    project_dir = root_sheet.parent.resolve()
    problems: list[str] = []
    unannotated: list[str] = []
    legacy: dict[str, list[str]] = {}
    deferred: list[tuple[str, str, list[str]]] = []
    done: set[Path] = set()

    def display(sheet: Path) -> str:
        try:
            return sheet.resolve().relative_to(project_dir).as_posix()
        except ValueError:
            return sheet.as_posix()

    def visit(sheet: Path, ancestors: tuple[Path, ...]) -> None:
        key = sheet.resolve()
        label = display(sheet)
        if key in ancestors:
            problems.append(f"sheet hierarchy cycle through {label}: hierarchy cannot be verified")
            return
        if key in done:
            return
        done.add(key)
        if not sheet.is_file():
            problems.append(f"sub-sheet file missing: {label}: annotation cannot be verified")
            return
        try:
            root = parse_file(str(sheet))
            if getattr(root, "tag", None) != "kicad_sch":
                raise ValueError("root node is not kicad_sch")
            info = _read_sheet(root, project)
        except Exception as exc:  # noqa: BLE001 - any parse failure must fail closed
            problems.append(
                f"schematic {label} could not be parsed as a kicad_sch "
                f"({type(exc).__name__}: {exc}): annotation cannot be verified"
            )
            return
        problems.extend(f"{label}: {p}" for p in info["problems"])
        for ref in info["references"]:
            _record(label, ref)
        for uuid, refs in info["deferred"]:
            deferred.append((label, uuid, refs))
        for tail, refs in info["legacy"].items():
            legacy.setdefault(tail, []).extend(refs)
        for name in info["sub_sheets"]:
            normalized = name.replace("\\", "/")
            if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
                problems.append(f"sheet file outside the project: {name} (in {label})")
                continue
            target = sheet.parent / normalized
            if not _inside(target.resolve(), project_dir):
                problems.append(f"sheet file outside the project: {name} (in {label})")
                continue
            visit(target, (*ancestors, key))

    def _record(label: str, ref: str) -> None:
        if ref.startswith("#"):
            return  # power symbols and flags
        if ref == "" or "?" in ref:
            unannotated.append(f"{label}:{ref or _NO_REFERENCE}")

    visit(root_sheet, ())
    for label, uuid, property_refs in deferred:
        # No instance data for this project: a KiCad-6 symbol_instances entry (keyed
        # by the symbol uuid) wins, otherwise the Reference property is all there is.
        for ref in (legacy.get(uuid) if uuid else None) or property_refs or [""]:
            _record(label, ref)
    if unannotated:
        problems.append(
            "schematic not fully annotated: kicad-cli would skip parity silently "
            f"({', '.join(sorted(set(unannotated)))})"
        )
    problems.extend(_stale_instance_problems(root_sheet))
    return problems


_STALE = "stale instance path"


def _stale_instance_problems(root_sheet: Path) -> list[str]:
    """Placed symbols whose instance data does not match the sheet hierarchy (build ticket 86).

    KiCad 7 and later key a symbol's reference on its sheet use: the ``(instances (project P
    (path ...)))`` entry whose path is the use's instance path (``sheet_tree.walk``: the root's
    ``/<root uuid>``, then the sheet uuids). A symbol that has entries, of any project, but none
    at a use's path is reported for that use: kicad-cli 10.0.7 then takes its reference from a
    stale or foreign entry, not from this hierarchy (build ticket 68, docs/ARCHITECTURE.md known
    facts), and its netlist can still agree in count. The rule of
    ``schematic_import._stale_only_entries`` (0 of 11166 measured sheet uses in 83 roots); a
    stale entry beside a live one is not reported. Symbols without entries (KiCad 6 sheets) are
    not affected. Sheets the walk cannot reach are ``_annotation_problems``' own reasons; this
    check never raises: a failure is a reason."""
    from kicad_tools.sexp.parser import parse_file

    from kicad_mcp import sheet_tree

    try:
        tree = sheet_tree.walk(root_sheet)
        parsed: dict[Path, Any] = {}
        found: list[str] = []
        for sheet, use_path in tree.uses:
            if sheet not in parsed:
                parsed[sheet] = parse_file(str(sheet))
            for symbol in parsed[sheet].find_children("symbol"):
                entries = [
                    (_node_atom(proj, 0), _node_atom(path, 0), _reference_of(path))
                    for instances in symbol.find_children("instances")
                    for proj in instances.find_children("project")
                    for path in proj.find_children("path")
                ]
                if not entries or any(path == use_path for _, path, _ in entries):
                    continue
                reference = _property_value(symbol, ("Reference",)) or _NO_REFERENCE
                listed = ", ".join(
                    f"{path} {ref}" + ("" if proj == root_sheet.stem else f" (project {proj})")
                    for proj, path, ref in entries
                )
                uuids = symbol.find_children("uuid")
                uuid = _node_atom(uuids[0], 0) if uuids else ""
                found.append(
                    f"{reference} uuid {uuid} in {tree.rel(sheet)} (sheet use {use_path or '/'}; "
                    f"instance paths {listed})"
                )
    except Exception as exc:  # noqa: BLE001 - fail closed, never raise
        return [f"{_STALE} check could not run ({type(exc).__name__}: {exc}): annotation cannot be verified"]
    if not found:
        return []
    rootless = "; the root sheet has no uuid, so no instance path can name it" if not tree.uses[0][1] else ""
    return [
        f"{_STALE} (kicad-cli then takes the reference from a stale or foreign entry, not from this "
        f"hierarchy, so parity may compare against the wrong part{rootless}): "
        # the wording of schematic_import._STALE_ONLY: the import reuses this check
        "instance data does not match the sheet hierarchy; annotate in KiCad: " + "; ".join(dict.fromkeys(found))
    ]


def _reference_of(path_node: Any) -> str:
    refs = path_node.find_children("reference")
    return _node_atom(refs[0], 0) if refs else ""


def _check_parity(pcb_path: Path, parity: str, reason: str | None) -> tuple[bool, list[str]]:
    """``(run_parity, problems)``: whether kicad-cli should run schematic parity, plus
    reasons the parity claim is not evidence. Parity is run unless the caller
    legitimately declared it not applicable."""
    problems: list[str] = []
    schematic = pcb_path.with_suffix(".kicad_sch")
    if parity not in PARITY_MODES:
        return True, [f"parity must be one of {PARITY_MODES}, got {parity!r}"]
    if parity == "required":
        if not schematic.is_file():
            problems.append("schematic missing: kicad-cli would skip parity silently and exit 0")
        else:
            problems.extend(_annotation_problems(schematic))
        return True, problems
    if not (reason or "").strip():
        return True, ["parity 'not_applicable' needs a parity_reason"]
    if schematic.is_file():
        return True, ["schematic exists: the 'not_applicable' parity claim is rejected"]
    return False, problems


# --- the netlist count guard (build ticket 50) ---------------------------------


_GUARD = "netlist count guard"


def _counted(ref: str, dnp: bool) -> bool:
    """Power symbols and flags (``#``) and DNP parts are not counted."""
    return not ref.startswith("#") and not dnp


def _units(units: set[str]) -> str:
    ordered = sorted(units, key=lambda u: (not u.isdigit(), int(u) if u.isdigit() else 0, u))
    return ("unit " if len(ordered) == 1 else "units ") + ", ".join(ordered)


def _netlist_count_problems(root_sheet: Path, notes: list[str] | None = None) -> list[str]:
    """Why kicad-cli's netlist of ``root_sheet`` cannot stand for the sheet tree, or ``[]``.

    KiCad leaves a sub-sheet it cannot read out of the design with exit 0 (a raw line break
    in a quoted string: ``complex_hierarchy`` 68 to 10 components), so schematic parity would
    compare the board with a truncated netlist. The count from the files: the sheet tree
    walked from the root (``sheet_tree.walk``, never a glob), one component per reference
    over every sheet use (a sheet used N times counts N times; the units of a multi-unit
    part count once), power symbols, DNP symbols and symbols excluded from the board
    (``on_board no``, which kicad-cli's netlist leaves out) not counted. The count from
    kicad-cli: the netlist's components without ``#`` references and without the ``dnp``
    property. Then the per-instance comparison of ``sheet_tree.netlist_disagreement``
    (duplicates, both set directions). Anything that stops the count is a reason too.
    """
    from kicad_mcp import sheet_tree

    try:
        tree = sheet_tree.walk(root_sheet)
        if tree.problems:
            return [
                f"{_GUARD}: the sheet tree cannot be counted ({'; '.join(tree.problems)}): "
                "the netlist parity compares against cannot be verified"
            ]
        records = sheet_tree.placed_instances(tree, on_board_only=True)
        components = sheet_tree.netlist_components(root_sheet)
        disagreement = sheet_tree.netlist_disagreement(tree, [c["ref"] for c in components])
    except Exception as exc:  # noqa: BLE001 - the guard fails closed, never raises
        return [
            f"{_GUARD} could not run ({type(exc).__name__}: {exc}): "
            "the netlist parity compares against cannot be verified"
        ]

    # Every on-board, non-power reference with the sheet uses that place it; the DNP ones
    # apart. DNP parts are not counted, but kicad-cli lists them (with a ``dnp`` property),
    # so a DNP part the netlist lost is compared on its own. A multi-unit part whose units
    # disagree on DNP takes whichever state the netlist gives it: KiCad 10.0.7's netlist takes
    # one unit's flag, and which one is not determined (the lowest unit's when all units share
    # one sheet; across sheets neither the lowest nor a fixed one: vme-wren IC14, jetson U30).
    # Its presence is still checked; the disagreement is a note, not a reason.
    where: dict[str, set[tuple[str, str]]] = {}
    unit_dnp: dict[str, dict[bool, set[str]]] = {}
    for record in records:
        ref = record["ref"]
        if ref.startswith("#"):
            continue
        where.setdefault(ref, set()).add((record["sheet"], record["path"]))
        unit_dnp.setdefault(ref, {True: set(), False: set()})[record["dnp"] == "yes"].add(record["unit"])
    listed = [c["ref"] for c in components if _counted(c["ref"], "dnp" in c["properties"])]
    listed_dnp = {c["ref"] for c in components if not c["ref"].startswith("#") and "dnp" in c["properties"]}
    mixed = sorted(ref for ref, units in unit_dnp.items() if units[True] and units[False])
    placed_dnp = {ref for ref, units in unit_dnp.items() if units[True] and not units[False]}
    placed_dnp |= set(mixed) & listed_dnp
    placed = {ref for ref in where if _counted(ref, ref in placed_dnp)}
    listed_refs = {c["ref"] for c in components}
    for ref in mixed:
        state = "DNP" if ref in listed_dnp else "not DNP" if ref in listed_refs else "not listed"
        if notes is not None:
            notes.append(
                f"{_GUARD}: the units of {ref} disagree on DNP (DNP: {_units(unit_dnp[ref][True])}; "
                f"not DNP: {_units(unit_dnp[ref][False])}); KiCad's netlist takes one unit's flag "
                f"(here: {state}), so the units should agree"
            )

    problems: list[str] = []
    listed_set = set(listed)
    if len(placed) != len(listed) or placed != listed_set or placed_dnp != listed_dnp:
        in_netlist = listed_set | listed_dnp
        uses: dict[tuple[str, str], list[str]] = {}
        for ref, sheet_uses in where.items():
            for use in sheet_uses:
                uses.setdefault(use, []).append(ref)
        missing = []
        for (sheet, path), refs in sorted(uses.items()):
            gone = sorted(ref for ref in refs if ref not in in_netlist)
            if gone:
                missing.append(
                    f"{sheet} at {path or '/'}: {len(gone)} of {len(refs)} missing ({', '.join(gone)})"
                )
        extra = sorted(in_netlist - set(where))
        detail = [f"missing from the netlist: {'; '.join(missing)}"] if missing else []
        if extra:
            detail.append(f"in the netlist but placed in no sheet: {', '.join(extra)}")
        problems.append(
            f"{_GUARD}: kicad-cli's netlist has {len(listed)} component(s), the sheet tree "
            f"places {len(placed)} (power, DNP and off-board symbols not counted); DNP parts: "
            f"the netlist has {len(listed_dnp)}, the sheet tree places {len(placed_dnp)}; KiCad "
            "leaves a sub-sheet it cannot read out with exit 0, so parity may have compared "
            "against a truncated netlist" + (": " + "; ".join(detail) if detail else "")
        )
    problems.extend(f"{_GUARD}: {p}" for p in disagreement)
    problems.extend(_sheet_load_problems(tree))
    return problems


def _sheet_load_problems(tree: Any) -> list[str]:
    """Sub-sheets ``kicad-cli sch upgrade`` cannot load on their own.

    A truncated sub-sheet can keep its parts (the fault after the last symbol) and so the
    count, while wires, labels or sheets after the fault are gone. KiCad reports the fault
    only when the sheet itself is the file loaded (``sch upgrade`` of the root says "saved"
    with exit 0; paradigm-review-2 ticket 03), so each sub-sheet is loaded on its own.
    ``sch upgrade`` rewrites only the file it is given, in place: this runs on the graded
    copy, after the DRC runs and the netlist export. The root is not loaded here: a root
    that does not load already fails the netlist export (exit 3)."""
    root = tree.root.resolve()
    problems: list[str] = []
    for sheet in tree.files:
        if sheet == root:
            continue
        label = tree.rel(sheet)
        try:
            proc = kicad_cli.upgrade_schematic(sheet)
        except Exception as exc:  # noqa: BLE001 - fail closed
            problems.append(
                f"{_GUARD}: sheet {label} could not be load-checked ({type(exc).__name__}: {exc})"
            )
            continue
        if proc.returncode != 0:
            output = " ".join(f"{proc.stdout} {proc.stderr}".split())
            problems.append(
                f"{_GUARD}: sheet {label} does not load on its own (kicad-cli sch upgrade exited "
                f"{proc.returncode}: {output}); KiCad keeps only what it parsed before the fault, "
                "with exit 0, so parity may have compared against a truncated sheet"
            )
    return problems


# --- the gate ----------------------------------------------------------------


def _incomplete_result(
    parity: str, parity_reason: str | None, reasons: list[str], notes: list[str] | None = None
) -> dict:
    return {
        "verdict": "INCOMPLETE",
        "unconnected": [],
        "violations": [],
        "warnings": [],
        "unstable": False,
        "runs": 0,
        "list_complete": False,
        "incomplete_reasons": reasons,
        "notes": notes or [],
        "parity": {"status": parity, "reason": parity_reason},
        "baseline_snapshot": None,
    }


def verify_board(
    pcb_path: str | Path,
    *,
    parity: str,
    parity_reason: str | None = None,
    baseline: dict | None = None,
    drc: _Drc | None = None,
) -> dict[str, Any]:
    """Grade a board against the acceptance gate.

    ``parity`` is mandatory: ``"required"`` or ``"not_applicable"`` (the latter needs
    ``parity_reason`` and is rejected when a schematic exists). ``drc`` replaces the
    ``kicad-cli`` subprocess boundary (tests only). Anything that stops a check from
    running yields ``INCOMPLETE``, never an exception and never a pass; evidence
    already collected still counts (``FAIL`` takes precedence over ``INCOMPLETE``).
    ``notes`` are informational (for example, the units of one part disagree on DNP): never
    a reason, they never change the verdict.
    """
    pcb_path = Path(pcb_path)
    run_drc = drc or kicad_cli.run_drc
    check_parity, incomplete_reasons = _check_parity(pcb_path, parity, parity_reason)
    notes: list[str] = []  # informational: never a reason, never changes the verdict

    reports: list[dict] = []
    try:
        with _graded_copy(pcb_path) as board:
            for _ in range(GATE_RUNS):
                try:
                    report = run_drc(board, refill_zones=True, schematic_parity=check_parity)
                    if "unconnected_items" not in report:
                        raise ValueError("the DRC report has no 'unconnected_items' (not read as clean)")
                    reports.append(report)
                except (kicad_cli.KicadCliError, OSError, ValueError) as exc:
                    incomplete_reasons.append(f"kicad-cli could not complete a run: {exc}")
                    break
            if reports and parity == "required" and pcb_path.with_suffix(".kicad_sch").is_file():
                # On the graded copy: the files the parity runs read. A failure is a reason,
                # never an exception that would discard the evidence of the runs.
                try:
                    incomplete_reasons.extend(_netlist_count_problems(board.with_suffix(".kicad_sch"), notes))
                except Exception as exc:  # noqa: BLE001 - fail closed
                    incomplete_reasons.append(f"{_GUARD} could not run ({type(exc).__name__}: {exc})")
        if not reports:
            return _incomplete_result(parity, parity_reason, incomplete_reasons, notes)
        current = _current_rules(pcb_path, _ignored_keys(reports[0]))
    except (kicad_cli.KicadCliError, OSError, ValueError) as exc:
        return _incomplete_result(
            parity, parity_reason, [*incomplete_reasons, f"kicad-cli could not complete: {exc}"], notes
        )

    classified = [_classify(report) for report in reports]
    errors_by_run = [errors for errors, _, _ in classified]
    unconnected_by_run = [_unconnected(report) for report in reports]
    errors = _union(errors_by_run)
    warnings = _union([warnings for _, warnings, _ in classified])
    unconnected_entries = _union(unconnected_by_run)

    key_sets = [set(e) | set(u) for e, u in zip(errors_by_run, unconnected_by_run)]
    unstable = any(keys != key_sets[0] for keys in key_sets)

    unknown = sorted({s for _, _, unknown_set in classified for s in unknown_set})
    if unknown:
        incomplete_reasons.append(f"unrecognised severity string(s) counted as errors: {unknown}")
    unparsed = sum(1 for (net, _, _) in unconnected_entries if net is None)
    if unparsed:
        incomplete_reasons.append(
            f"net could not be determined for {unparsed} unconnected item(s): "
            "resolve by position against the raw board"
        )
    truncated = sorted({code for report in reports for code in _truncated_codes(report)})
    for code in truncated:
        incomplete_reasons.append(f"report truncated at the KiCad cap for '{code}'")
    if baseline is None:
        incomplete_reasons.append(
            "rules not pinned: a PASS cannot be certified against unpinned rules; "
            "call pin_rule_baseline before the work and pass its result as baseline "
            "(the snapshot is in baseline_snapshot)"
        )
    else:
        changed = sorted(key for key in current if current[key] != baseline.get(key))
        if changed:
            incomplete_reasons.append(f"rules changed since pinning: {', '.join(changed)}")

    # FAIL (demonstrably bad board) takes precedence over INCOMPLETE (missing evidence).
    if unconnected_entries or errors:
        verdict = "FAIL"
    elif incomplete_reasons:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "unconnected": [
            {"net": net, "items": entry.get("items", [])}
            for (net, _, _), entry in unconnected_entries.items()
        ],
        "violations": list(errors.values()),
        "warnings": list(warnings.values()),
        "unstable": unstable,
        "runs": len(reports),
        "list_complete": not truncated,
        "incomplete_reasons": incomplete_reasons,
        "notes": notes,
        "parity": {"status": parity, "reason": parity_reason},
        "baseline_snapshot": current,
    }
