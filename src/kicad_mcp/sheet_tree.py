"""Walk a KiCad schematic's sheet tree from its root sheet (build ticket 26).

The rule every schematic reader holds: start at the root sheet and follow each
``(sheet ...)`` node's sheet-file property, resolved relative to the sheet that holds
it (as KiCad does), never a directory glob and never the process working directory.
mixelpixx read every ``.kicad_sch`` under the project directory, so ``.history/`` and
backup copies overwrote live nets on a real board (their #400). Here
``kicad_tools.schema.hierarchy.build_hierarchy`` (0.20.0 and 0.23.0) resolves a sub-sheet
against the working directory first, so a same-named stray copy there is read as the
live sheet: that is how ``export_manufacturing_package``'s BOM read a stray (ticket 26).

KiCad 7 and later write the property as ``Sheetfile``; KiCad 6 wrote ``Sheet file``.
Both are followed. A ``.kicad_sch`` under the root's directory that the tree does not
reach is listed in ``unreferenced`` and is never read: readers report it instead.

The walk fails closed: a missing, unparseable, cyclic or outside-the-project sheet, or a
sheet node without a file property, is a problem the caller must surface, never a sheet
silently left out.
"""

from __future__ import annotations

import os
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SHEET_FILE_KEYS = ("Sheetfile", "Sheet file")  # KiCad 7+ / KiCad 6 spelling
SHEET_NAME_KEYS = ("Sheetname", "Sheet name")
_LEGACY_FILE_KEY = "Sheet file"
_SCAN_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


@dataclass(frozen=True)
class SheetTree:
    """What a walk from ``root`` found. Paths in ``files`` are resolved; each appears once."""

    root: Path
    files: tuple[Path, ...]
    legacy_files: tuple[Path, ...]
    problems: tuple[str, ...]
    unreferenced: tuple[str, ...]
    uses: tuple[tuple[Path, str], ...] = ()

    @property
    def project_dir(self) -> Path:
        return self.root.resolve().parent

    def rel(self, path: Path) -> str:
        """``path`` relative to the root's directory (POSIX), or as given when outside it."""
        try:
            return Path(path).resolve().relative_to(self.project_dir).as_posix()
        except ValueError:
            return Path(path).as_posix()

    def report(self) -> dict[str, Any]:
        """The fields every reader adds to its output."""
        return {
            "sheets": [self.rel(f) for f in self.files],
            "unreferenced_sheets": list(self.unreferenced),
            "sheet_problems": list(self.problems),
        }


def _atom(node: Any, index: int = 0) -> str:
    return node.get_string(index) or ""


def sheet_file_property(sheet: Any) -> tuple[str | None, str | None]:
    """``(file name, key)`` of a parsed ``(sheet ...)`` node; ``(None, None)`` without one."""
    for prop in sheet.find_children("property"):
        key = _atom(prop, 0)
        if key in SHEET_FILE_KEYS:
            return _atom(prop, 1), key
    return None, None


def sheet_name_property(sheet: Any) -> str | None:
    for prop in sheet.find_children("property"):
        if _atom(prop, 0) in SHEET_NAME_KEYS:
            return _atom(prop, 1)
    return None


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.relative_to(directory)
    except ValueError:
        return False
    return True


def _parse(path: Path) -> Any:
    from kicad_tools.sexp.parser import parse_file

    root = parse_file(str(path))
    if getattr(root, "tag", None) != "kicad_sch":
        raise ValueError("root node is not kicad_sch")
    return root


def walk(root: str | Path) -> SheetTree:
    """Walk the sheet tree from ``root``. Never raises for a bad project: see ``problems``.

    Each use of a sheet is visited, so ``uses`` holds one ``(file, instance path)`` per sheet
    instance: the root's ``/<root uuid>``, then ``<parent path>/<sheet node uuid>``, the paths
    KiCad keys symbol instance data on. Each file is parsed once."""
    root = Path(root)
    project_dir = root.resolve().parent
    files: list[Path] = []
    legacy: list[Path] = []
    problems: list[str] = []
    uses: list[tuple[Path, str]] = []
    parsed_files: dict[Path, Any] = {}  # None: missing or unparseable (already reported)

    def label(path: Path) -> str:
        try:
            return path.resolve().relative_to(project_dir).as_posix()
        except ValueError:
            return path.as_posix()

    def load(sheet: Path, key: Path, via_legacy: bool) -> Any:
        if key in parsed_files:
            return parsed_files[key]
        parsed = None
        if not sheet.is_file():
            problems.append(f"sheet file missing: {label(sheet)}")
        else:
            try:
                parsed = _parse(sheet)
            except Exception as exc:  # noqa: BLE001 - any parse failure fails closed
                problems.append(
                    f"sheet {label(sheet)} could not be parsed as a kicad_sch ({type(exc).__name__}: {exc})"
                )
        parsed_files[key] = parsed
        if parsed is not None:
            files.append(key)
            if via_legacy:
                legacy.append(key)
        return parsed

    def visit(sheet: Path, ancestors: tuple[Path, ...], via_legacy: bool, path: str | None) -> None:
        key = sheet.resolve()
        if key in ancestors:
            problems.append(f"sheet hierarchy cycle through {label(sheet)}")
            return
        parsed = load(sheet, key, via_legacy)
        if parsed is None:
            return
        if path is None:  # the root: its own uuid starts every instance path
            path = "/" + _uuid(parsed) if _uuid(parsed) else ""
        uses.append((key, path))
        for node in parsed.find_children("sheet"):
            name, prop_key = sheet_file_property(node)
            if not name:
                sheet_name = sheet_name_property(node) or "<unnamed>"
                problems.append(f"sheet '{sheet_name}' in {label(sheet)} has no sheet file property")
                continue
            normalized = name.replace("\\", "/")
            target = sheet.parent / normalized
            if normalized.startswith("/") or (len(normalized) > 1 and normalized[1] == ":") or not _inside(
                target.resolve(), project_dir
            ):
                problems.append(f"sheet file outside the project: {name} (in {label(sheet)})")
                continue
            visit(target, (*ancestors, key), prop_key == _LEGACY_FILE_KEY, f"{path}/{_uuid(node)}")

    visit(root, (), False, None)
    reached = set(files)
    unreferenced: list[str] = []
    if project_dir.is_dir():
        for current, dirs, names in os.walk(project_dir):
            dirs[:] = sorted(d for d in dirs if d not in _SCAN_SKIP_DIRS)
            for name in names:
                if not name.endswith(".kicad_sch"):
                    continue
                path = (Path(current) / name).resolve()
                if path not in reached:
                    unreferenced.append(label(path))
    return SheetTree(
        root=root,
        files=tuple(files),
        legacy_files=tuple(legacy),
        problems=tuple(dict.fromkeys(problems)),  # a sheet used twice repeats its problems
        unreferenced=tuple(sorted(unreferenced)),
        uses=tuple(uses),
    )


def _uuid(node: Any) -> str:
    found = node.find_children("uuid")
    return _atom(found[0], 0) if found else ""


def netlist_components(root: str | Path) -> list[dict[str, Any]]:
    """Components of ``kicad-cli sch export netlist`` of the root sheet, read from the raw
    S-expression: ``ref``, ``value``, ``footprint``, ``datasheet``, ``description``,
    ``lib_id``, ``fields`` (name -> value) and ``properties`` (name -> value; KiCad marks
    ``dnp``, ``exclude_from_bom`` and ``exclude_from_board`` this way). kicad-cli walks the
    tree itself, from the root, one component per placed instance."""
    import tempfile

    from kicad_tools.sexp.parser import parse_file

    from kicad_mcp import kicad_cli

    with tempfile.TemporaryDirectory(prefix="kicad-mcp-netlist-") as tmp:
        out = kicad_cli.export_netlist(root, Path(tmp) / "netlist.net")
        parsed = parse_file(str(out))

    def text(node: Any, tag: str) -> str:
        found = node.find_children(tag)
        return _atom(found[0], 0) if found else ""

    comps: list[dict[str, Any]] = []
    for block in parsed.find_children("components"):
        for comp in block.find_children("comp"):
            fields: dict[str, str] = {}
            for group in comp.find_children("fields"):
                for field in group.find_children("field"):
                    # (field (name "LCSC") "C123"): the value is the atom after the name node
                    atoms = [v for v in field.values if isinstance(v, (str, int, float))]
                    fields[text(field, "name")] = str(atoms[0]) if atoms else ""
            properties = {text(p, "name"): text(p, "value") for p in comp.find_children("property")}
            lib = comp.find_children("libsource")
            lib_id = f"{text(lib[0], 'lib')}:{text(lib[0], 'part')}" if lib else ""
            comps.append(
                {
                    "ref": text(comp, "ref"),
                    "value": text(comp, "value"),
                    "footprint": text(comp, "footprint"),
                    "datasheet": text(comp, "datasheet"),
                    "description": text(comp, "description"),
                    "lib_id": lib_id,
                    "fields": fields,
                    "properties": properties,
                }
            )
    return comps


def netlist_disagreement(tree: SheetTree, netlist_refs: Iterable[str]) -> list[str]:
    """Why kicad-cli's netlist cannot stand for the tree's placed parts, or ``[]``.

    kicad-cli 10's netlist keeps one component per reference and drops any other part under
    the same reference with exit 0 (build ticket 68). So first, over every placed symbol (power
    and off-board symbols too), it refuses these, each with its own message:

    - an instance path, sheet use path, symbol uuid or ``symbol_instances`` key that
      ``path_key`` cannot prove kicad-cli reads as an id vector (build ticket 73: kicad-cli
      reads paths as case-insensitive id vectors with empty segments dropped, so paths are
      compared by ``path_key``, and a path it does not know is refused);
    - a symbol with more than one instance entry at its own sheet use's path, of any project
      (``live_entries``: kicad-cli takes the last entry, the reader would take another; build
      ticket 73);
    - a symbol whose instance data names no reference for its sheet use (``stale_only``:
      kicad-cli then takes a stale or foreign entry's reference, which can name another
      existing part; sonde xilinx's U1 unit 2 with a stale entry naming R14 became a second
      R14 in DIP-14 and the resistor R14 was dropped);
    - a reference that names more than one part (``reference_conflicts``: a unit repeats, or
      the units differ in Value, Footprint or unit count; build tickets 68 and 73).

    Then it compares references: the placed (non-``#``, on-board) ones the netlist lacks, the
    netlist's ones no sheet places, and any the netlist lists twice. kicad-cli leaves a sheet it cannot read out with exit 0
    (docs/ARCHITECTURE.md, the silent sub-sheet drop), so a reader built on its netlist checks it
    against the files."""
    netlist = list(netlist_refs)
    records = placed_instances(tree)
    problems = []
    unproven = [f"{r['ref']} uuid {r['uuid']} in {r['sheet']}: {r['unproven_paths']}" for r in records if r["unproven_paths"]]
    unproven += legacy_table_problems(tree)
    if unproven:
        problems.append(f"{UNPROVEN_PATH}: " + "; ".join(unproven))
    doubled = [
        f"{r['ref']} uuid {r['uuid']} in {r['sheet']} (sheet use {r['path'] or '/'}; entries {r['live_entries']})"
        for r in records
        if r["live_entries"]
    ]
    if doubled:
        problems.append(f"{DUPLICATE_ENTRIES}: " + "; ".join(doubled))
    stale = [
        f"{r['ref']} uuid {r['uuid']} in {r['sheet']} (sheet use {r['path'] or '/'})" for r in records if r["stale_only"] == "yes"
    ]
    if stale:
        problems.append(f"{STALE_ONLY}: " + "; ".join(stale))
    conflicts = reference_conflicts(records)
    if conflicts:
        problems.append("a reference names more than one placed symbol: " + "; ".join(conflicts))
    placed = {r["ref"] for r in records if not r["ref"].startswith("#") and r["on_board"] == "yes"}
    listed = set(netlist)
    if placed - listed:
        problems.append("placed in the sheets but missing from kicad-cli's netlist: " + ", ".join(sorted(placed - listed)))
    if listed - placed:
        problems.append(
            "in kicad-cli's netlist but not placed in any sheet of the tree: " + ", ".join(sorted(listed - placed))
        )
    twice = sorted(ref for ref in listed if netlist.count(ref) > 1)
    if twice:
        problems.append("listed more than once in kicad-cli's netlist: " + ", ".join(twice))
    return problems


DUPLICATE_ENTRIES = (
    "more than one instance entry at the symbol's own sheet use, so the reference is not proven "
    "(kicad-cli takes the last one, whatever project it belongs to); annotate in KiCad"
)
UNPROVEN_PATH = (
    "an instance path or symbol_instances key whose reading by kicad-cli is not proven (it reads canonical "
    "and 32-digit ids, either one in braces, and 8-digit timestamp ids, in any case, between any number of "
    "slashes; no other spelling is known to be read, and which of two keys for one path wins is not known), "
    "so the reference is not proven; annotate in KiCad"
)
STALE_ONLY = "instance data does not match the sheet hierarchy; annotate in KiCad"


def reference_conflicts(records: Iterable[dict[str, str]]) -> list[str]:
    """Each reference of ``placed_instances`` records that names more than one part, with the
    rules that fired, or ``[]``. The units of one multi-unit symbol share a reference and are
    one part, whatever their lib ids (sonde xilinx's U1 units 1-4 carry four rescued lib ids).
    Two parts are (build ticket 68's rule): the same unit placed twice, or units whose Value,
    Footprint or embedded lib symbol's unit count differ (KiCad keeps these equal across the
    units of one part; a resistor annotated as a free unit of a 74LS125 repeats no unit, and
    kicad-cli's netlist then keeps the resistor and drops the gate package without an error).
    A unit count that cannot be read under a reference with more than one record fails closed
    ("unit count unreadable"). Power symbols (``#``) are skipped. Each entry reads
    ``<ref> (<rules>): <ref> unit <u> value '<v>' footprint '<fp>' <lib id> (<n> units) in
    <sheet> at <sheet use> uuid <uuid> and ...``."""
    by_ref: dict[str, list[dict[str, str]]] = {}
    for record in records:
        if not record["ref"].startswith("#"):
            by_ref.setdefault(record["ref"], []).append(record)
    problems = []
    for ref in sorted(by_ref):
        recs = by_ref[ref]
        units = Counter(r["unit"] for r in recs)
        rules = [f"unit {u} placed {n} times" for u, n in sorted(units.items()) if n > 1]
        if len({r["value"] for r in recs}) > 1:
            rules.append("different values")
        if len({r["footprint"] for r in recs}) > 1:
            rules.append("different footprints")
        if len({r["unit_count"] for r in recs if r["unit_count"]}) > 1:
            rules.append("different unit counts")
        if len(recs) > 1 and any(not r["unit_count"] for r in recs):
            rules.append("unit count unreadable")
        if rules:
            where = " and ".join(
                f"{r['ref']} unit {r['unit']} value {r['value']!r} footprint {r['footprint']!r} {r['lib_id']} "
                f"({_units_text(r['unit_count'])}) in {r['sheet']} at {r['path'] or '/'} uuid {r['uuid']}"
                for r in recs
            )
            problems.append(f"{ref} ({', '.join(rules)}): {where}")
    return problems


def _units_text(count: str) -> str:
    if not count:
        return "unit count unknown"
    return "1 unit" if count == "1" else f"{count} units"


def placed_instances(
    tree: SheetTree, project: str | None = None, *, on_board_only: bool = False
) -> list[dict[str, str]]:
    """One record per placed symbol unit per sheet use: ``ref``, ``unit``, ``lib_id``,
    ``sheet``, ``path`` (the sheet instance path), ``uuid`` (the placed symbol's own; the
    uses of a reused sheet share it, and with it the symbol's ``Footprint`` property),
    ``value`` and ``footprint`` (its ``Value`` and ``Footprint`` properties), ``dnp``
    (``"yes"`` when the symbol is marked ``(dnp yes)``, else ``"no"``) and ``unit_count``
    (the number of units of the symbol's embedded lib symbol, ``""`` when it cannot be read:
    see ``_unit_counts``), ``on_board`` (``"no"`` for a symbol marked ``(on_board no)``, else
    ``"yes"``) and ``stale_only`` (``"yes"`` when the symbol has ``(instances ...)`` entries,
    of any project, but none at this use's path: its reference is then not defined for this
    hierarchy and kicad-cli may take the stale entry's) and ``live_entries`` (``""``, or, when
    the symbol has more than one entry at this use's path, in any project, the entries as
    ``project 'P' reference 'R'`` joined by ``; ``) and ``unproven_paths`` (``""``, or the
    symbol's instance paths, its sheet use's path or its uuid that ``path_key`` cannot prove
    kicad-cli reads as it does, joined by ``; ``). Power symbols (``#``) are included.

    Paths are compared the way kicad-cli reads them, by ``path_key`` (an id vector: case
    folded, empty segments dropped, the project name ignored), never as strings: an entry
    spelled ``/5ADE2176-...`` or ``5ade2176-.../`` is the same path as ``/5ade2176-...``.
    A path ``path_key`` rejects matches no use, is named in ``unproven_paths`` and is refused
    by ``netlist_disagreement``.

    The reference is KiCad's for that use when the symbol has exactly one
    ``(instances ... (path P (reference R)))`` entry whose path is the use's instance path
    from ``walk``. kicad-cli 10 takes the LAST entry at a path, whatever project it belongs
    to (build ticket 73, measured), so no choice among several is trusted: with more than one
    entry at the use's path the record takes this project's first, but ``live_entries`` names
    them and ``netlist_disagreement`` refuses the symbol. With no entry at the path: the
    root's KiCad 6 ``(symbol_instances ...)`` entry; then the symbol's own ``Reference``
    property. A stale entry (a path no use has) is not counted when the symbol also has an
    entry at the use's path. Without one, the record falls back as above, but kicad-cli may
    read the stale or other project's entry instead (build ticket 68: its netlist then takes
    that entry's reference), so a reader that needs KiCad's reference compares with
    kicad-cli's netlist (``netlist_disagreement``). ``on_board_only`` leaves
    out symbols marked ``(on_board no)``, which kicad-cli's netlist leaves out too
    (cm5_minima demo)."""
    project = project or tree.root.stem
    parsed: dict[Path, Any] = {}
    counts: dict[Path, dict[str, int]] = {}

    def sheet_of(path: Path) -> Any:
        if path not in parsed:
            parsed[path] = _parse(path)
            counts[path] = _unit_counts(parsed[path])
        return parsed[path]

    root_key = path_key(tree.uses[0][1]) if tree.uses else ()  # None: unproven, refused through unproven_paths
    legacy_table, _ = _read_legacy_table(tree.uses[0][0]) if tree.uses else ({}, [])

    out: list[dict[str, str]] = []
    for sheet, use_path in tree.uses:
        for symbol in sheet_of(sheet).find_children("symbol"):
            on_board = not any(_atom(n, 0) == "no" for n in symbol.find_children("on_board"))
            if on_board_only and not on_board:
                continue
            own_unit = symbol.find_children("unit")
            unit = _atom(own_unit[0], 0) if own_unit else "1"
            all_entries = [
                (_atom(proj, 0), entry)
                for instances in symbol.find_children("instances")
                for proj in instances.find_children("project")
                for entry in proj.find_children("path")
            ]
            use_key = path_key(use_path)
            unproven = [f"instance path {_atom(e, 0)!r}" for _, e in all_entries if path_key(_atom(e, 0)) is None]
            if use_key is None:
                unproven.append(f"sheet use path {use_path!r}")
            # kicad-cli compares paths as id vectors (path_key), not as strings, and ignores the project name
            entries = [(proj, e) for proj, e in all_entries if use_key is not None and path_key(_atom(e, 0)) == use_key]
            entries.sort(key=lambda e: e[0] != project)  # this project's entry first (see the docstring)
            symbol_key = path_key("/" + _uuid(symbol))
            legacy_key = (
                use_key[len(root_key) :] + symbol_key
                if None not in (use_key, symbol_key, root_key)
                else None
            )
            if legacy_key is None and not all_entries:
                unproven.append(f"symbol uuid {_uuid(symbol)!r}")
            if entries:
                entry = entries[0][1]
                refs = entry.find_children("reference")
                units = entry.find_children("unit")
                ref = _atom(refs[0], 0) if refs else ""
                unit = _atom(units[0], 0) if units else unit
            elif legacy_key is not None and legacy_key in legacy_table:
                ref, unit = legacy_table[legacy_key]
            else:
                ref = next((_atom(p, 1) for p in symbol.find_children("property") if _atom(p, 0) == "Reference"), "")
            lib = symbol.find_children("lib_id")
            names = symbol.find_children("lib_name") or lib
            count = counts[sheet].get(_atom(names[0], 0)) if names else None
            out.append(
                {
                    "ref": ref,
                    "unit": unit,
                    "lib_id": _atom(lib[0], 0) if lib else "",
                    "sheet": tree.rel(sheet),
                    "path": use_path,
                    "uuid": _uuid(symbol),
                    "value": _property(symbol, "Value"),
                    "footprint": _property(symbol, "Footprint"),
                    "dnp": "yes" if any(_atom(n, 0) == "yes" for n in symbol.find_children("dnp")) else "no",
                    "unit_count": str(count) if count else "",
                    "on_board": "yes" if on_board else "no",
                    "stale_only": "yes" if all_entries and not entries else "no",
                    "unproven_paths": "; ".join(unproven),
                    "live_entries": (
                        "; ".join(
                            f"project {proj!r} reference "
                            f"{(_atom(e.find_children('reference')[0], 0) if e.find_children('reference') else '')!r}"
                            for proj, e in entries
                        )
                        if len(entries) > 1
                        else ""
                    ),
                }
            )
    return out


_CANONICAL_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PLAIN_ID = re.compile(r"[0-9a-f]{32}")
_TIMESTAMP_ID = re.compile(r"[0-9a-f]{8}")


def path_key(path: str) -> tuple[str, ...] | None:
    """KiCad's reading of an instance path, or ``None`` when it is not proven.

    kicad-cli 10 reads ``(instances ... (path P))`` and the keys of ``(symbol_instances)`` as a
    vector of ids, not as a string (build ticket 73, measured against its netlist): case does
    not matter, empty segments (a leading, doubled or trailing ``/``, a missing leading ``/``)
    are dropped, the project name is ignored, and each segment is an id in one of the forms
    ``8-4-4-4-12`` hex digits, 32 hex digits, either of those in braces, or the 8-digit legacy
    timestamp (``1234abcd`` is ``00000000-0000-0000-0000-00001234abcd``). A segment of any
    other shape is not proven to be read as an id, so the result is ``None`` and the callers
    refuse the symbol. Of those shapes only these were tried (ticket 73's Answer, the table of
    fix round 2), and each was read as no live path: a space or tab, ``urn:uuid:``, a garbage
    prefix or suffix, 35 or 37 digits, wrong hyphens, 36 ``z``, plain text, an empty path.
    A braced timestamp and every other shape were not tried. Build ticket 95 measured two more
    things. A segment that is not hex (``root``, ``zz``, 36 ``x``) is matched by nothing, not
    even the identical string as the sheet's own uuid: a sheet ``(uuid "root")`` with an
    instance path ``/root`` is a stale entry, so the key stays ``None`` and no string "ids" are
    compared by text. And a timestamp is 1 to 8 hex digits, zero-padded (``abc`` is
    ``00000abc``; 9 to 31 digits are read as no id); this function still accepts only 8,
    which only refuses more. The result is the tuple of canonical lowercase ids; two
    paths are KiCad's same path exactly when their tuples are equal."""
    out: list[str] = []
    for raw in path.split("/"):
        if not raw:
            continue
        seg = raw.lower()
        if seg.startswith("{") and seg.endswith("}") and (
            _CANONICAL_ID.fullmatch(seg[1:-1]) or _PLAIN_ID.fullmatch(seg[1:-1])
        ):
            seg = seg[1:-1]
        if _CANONICAL_ID.fullmatch(seg):
            out.append(seg)
        elif _PLAIN_ID.fullmatch(seg):
            out.append(f"{seg[:8]}-{seg[8:12]}-{seg[12:16]}-{seg[16:20]}-{seg[20:]}")
        elif _TIMESTAMP_ID.fullmatch(seg):
            out.append("00000000-0000-0000-0000-0000" + seg)
        else:
            return None
    return tuple(out)


def _read_legacy_table(root_sheet: Path) -> tuple[dict[tuple[str, ...], tuple[str, str]], list[str]]:
    """The root's KiCad 6 ``(symbol_instances)`` table as ``path_key -> (reference, unit)``, and
    the keys kicad-cli's reading of which is not proven: a key ``path_key`` rejects, and two
    keys that are the same path (which one wins is not measured)."""
    table: dict[tuple[str, ...], tuple[str, str]] = {}
    problems: list[str] = []
    for block in _parse(root_sheet).find_children("symbol_instances"):
        for entry in block.find_children("path"):
            raw = _atom(entry, 0)
            key = path_key(raw)
            if key is None:
                problems.append(f"symbol_instances path {raw!r}")
                continue
            if key in table:
                problems.append(f"symbol_instances path {raw!r} (a second entry for the same path)")
            ref = entry.find_children("reference")
            unit = entry.find_children("unit")
            table[key] = (_atom(ref[0], 0) if ref else "", _atom(unit[0], 0) if unit else "1")
    return table, problems


def legacy_table_problems(tree: SheetTree) -> list[str]:
    """The root's ``(symbol_instances)`` keys whose reading by kicad-cli is not proven (see
    ``path_key`` and ``_read_legacy_table``); ``[]`` for a tree without such a table."""
    return _read_legacy_table(tree.uses[0][0])[1] if tree.uses else []


_LIB_UNIT = re.compile(r"_(\d+)_(\d+)$")  # a lib symbol's unit body: <name>_<unit>_<body style>


def _unit_counts(sheet: Any) -> dict[str, int]:
    """``name -> unit count`` of each embedded lib symbol of a parsed sheet, read from the raw
    ``(lib_symbols (symbol "<name>" (symbol "<base>_<unit>_<style>" ...)))``. A placed symbol
    names its entry by ``lib_name``, else ``lib_id``. Unit 0 holds what all units share; a lib
    symbol with no other unit body has one unit. The rule of build ticket 68's import scan."""
    out: dict[str, int] = {}
    for table in sheet.find_children("lib_symbols"):
        for lib in table.find_children("symbol"):
            units = {
                int(m.group(1)) for body in lib.find_children("symbol") if (m := _LIB_UNIT.search(_atom(body, 0)))
            }
            out[_atom(lib, 0)] = max(units - {0}, default=1)
    return out


def _property(symbol: Any, name: str) -> str:
    return next((_atom(p, 1) for p in symbol.find_children("property") if _atom(p, 0) == name), "")


def placed_references(
    tree: SheetTree, project: str | None = None, *, on_board_only: bool = False
) -> list[tuple[str, str]]:
    """``(reference, sheet)`` of every record of ``placed_instances``."""
    return [(r["ref"], r["sheet"]) for r in placed_instances(tree, project, on_board_only=on_board_only)]
