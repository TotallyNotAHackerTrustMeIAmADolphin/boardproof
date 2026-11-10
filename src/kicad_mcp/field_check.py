"""check_symbol_fields and get_properties: read-only readers of the fields of placed symbols (issues 163 and 122).

get_properties (issue 122, bottom of this module) reads chosen properties of selected or all symbols over the same
hierarchy walk (``_walk``) and the same property reading (``_props``) as check_symbol_fields.

Original docstring of check_symbol_fields: a read-only census of the fields of placed symbols (issue 163).

Neither ``check_connectivity`` nor ERC reports an empty Footprint, a missing LCSC or a Datasheet of
"~", so a schematic can look finished and still produce an incomplete BOM. This module reads every
sheet of the project's tree (from the root sheet, never a directory glob) and lists, per sheet file,
each placed symbol that has a gap in one of the required fields.

It reads and writes nothing else: no scratch copy, no kicad-cli, no write lock. The only reads are
the parses of the sheet files (``sheet_tree.walk``).

A field is a gap when the symbol has no such property (``missing``), its value is empty (``empty``),
only white space (``blank``) or the KiCad placeholder ``~`` (``placeholder``). Names match exactly
(KiCad field names are case sensitive); a property that differs from the asked name only in case is
named in ``case_variant`` of the gap, because it is the usual reason a field looks missing.

Not checked, listed apart under ``skipped`` (complete, by reference and uuid), with the first
matching reason: ``power`` (the library symbol carries the ``(power)`` flag, or the reference starts
with ``#``), ``off_board`` (``(on_board no)``), ``dnp`` (``(dnp yes)``) and ``no_lib_id`` (a symbol node
with no ``lib_id``). ``placed_symbols`` is ``checked`` plus every skipped symbol.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Iterator
from typing import Any, NamedTuple

from kicad_mcp import connectivity_check, sheet_authoring, sheet_editing, sheet_tree
from kicad_mcp.sheet_authoring import SheetAuthoringError

DEFAULT_FIELDS = ("Footprint", "LCSC", "Datasheet")
PLACEHOLDER = "~"
_SKIP_ORDER = ("power", "off_board", "dnp", "no_lib_id")


def _fields(required: Any) -> list[str]:
    if required is None:
        return list(DEFAULT_FIELDS)
    if not isinstance(required, list) or not required:
        raise SheetAuthoringError("invalid_spec", "required_fields must be a non-empty list of field names, or omitted for " + ", ".join(DEFAULT_FIELDS))
    names = [sheet_editing._property_name(name, "required_fields") for name in required]
    if len(set(names)) != len(names):
        raise SheetAuthoringError("invalid_spec", "required_fields names a field twice")
    return names


def _problem(value: str | None) -> str | None:
    """Why ``value`` (None: no such property) is a gap, or None when it is a real value."""
    if value is None:
        return "missing"
    if value == "":
        return "empty"
    if value.strip() == "":
        return "blank"
    if value.strip() == PLACEHOLDER:
        return "placeholder"
    return None


def _skip_reason(root: Any, symbol: Any, refs: list[str]) -> str | None:
    if connectivity_check.is_power(root, symbol) or any(r.startswith("#") for r in refs):
        return "power"
    if any(sheet_tree._atom(n, 0) == "no" for n in symbol.find_children("on_board")):
        return "off_board"
    if connectivity_check._dnp(symbol):
        return "dnp"
    return None


class Placed(NamedTuple):
    """One symbol node of a sheet file, with what the project's sheet tree says about it (shared by the readers of this module)."""

    node: Any
    parsed: Any
    uuid: str
    lib_id: str  # "" when the node has no lib_id
    own: str  # the Reference text
    refs: list[str]  # instance references of every use of the sheet (else the Reference text)
    unit: str
    uses: int


def _resolve(sch_path: str, root_sch: str | None) -> tuple[Path, Any]:
    """The root sheet file and the readable tree for ``sch_path``; refuses a sheet the tree does not reach."""
    file = Path(sch_path)
    if not file.is_file():
        raise SheetAuthoringError("file_not_found", f"sheet not found: {file}")
    root_file = sheet_authoring.find_root(file, root_sch)
    tree = sheet_authoring._require_tree(root_file)
    if file.resolve() not in {f.resolve() for f in tree.files}:
        raise SheetAuthoringError("parent_not_in_tree", f"{file.name} is not reached from the root sheet {root_file.name}", **tree.report())
    return root_file, tree


def _walk(tree: Any) -> Iterator[tuple[str, list[Placed]]]:
    """Per sheet file of the tree (each once): its relative name and every symbol node on it."""
    by_symbol: dict[tuple[str, str], list[dict[str, str]]] = {}
    for rec in sheet_tree.placed_instances(tree):
        by_symbol.setdefault((rec["sheet"], rec["uuid"]), []).append(rec)
    for path in tree.files:
        rel = tree.rel(path)
        parsed = sheet_tree.parsed_sheet(tree, path)
        rows: list[Placed] = []
        for symbol in parsed.find_children("symbol"):
            uuid = sheet_tree._uuid(symbol)
            records = by_symbol.get((rel, uuid), [])
            own = sheet_tree._property(symbol, "Reference")
            lib = symbol.find_children("lib_id")
            rows.append(Placed(symbol, parsed, uuid, sheet_tree._atom(lib[0], 0) if lib else "", own, [r["ref"] for r in records] or [own],
                               records[0]["unit"] if records else "1", len(records) or 1))
        yield rel, rows


def _props(symbol: Any) -> dict[str, str]:
    """The properties of a symbol node by name, the first node of a name winning."""
    return {sheet_tree._atom(p, 0): sheet_tree._atom(p, 1) for p in reversed(symbol.find_children("property"))}


def check_symbol_fields(sch_path: str, required_fields: Any = None, root_sch: str | None = None) -> dict[str, Any]:
    """Read-only: every placed symbol of the project with a gap in ``required_fields``, per sheet file."""
    try:
        required = _fields(required_fields)
        root_file, tree = _resolve(sch_path, root_sch)
        sheets: list[dict[str, Any]] = []
        skipped: dict[str, list[dict[str, Any]]] = {reason: [] for reason in _SKIP_ORDER}
        placed = 0
        for rel, rows in _walk(tree):
            entry: dict[str, Any] = {"sheet": rel, "checked": 0, "symbols_with_gaps": 0, "symbols": []}
            for sym in rows:
                placed += 1
                symbol, parsed, uuid = sym.node, sym.parsed, sym.uuid
                if not symbol.find_children("lib_id"):  # not a placed part KiCad can read fields of: counted, never silently dropped
                    skipped["no_lib_id"].append({"reference": sym.own, "uuid": uuid, "unit": "", "lib_id": "", "sheet": rel})
                    continue
                refs = sym.refs
                head = {"reference": refs[0], "uuid": uuid, "unit": sym.unit, "lib_id": sym.lib_id, "sheet": rel}
                if (why := _skip_reason(parsed, symbol, [*refs, sym.own])) is not None:
                    skipped[why].append(head)
                    continue
                props = _props(symbol)
                entry["checked"] += 1
                gaps = []
                for name in required:
                    if (problem := _problem(props.get(name))) is None:
                        continue
                    gap: dict[str, Any] = {"field": name, "problem": problem, "value": props.get(name)}
                    if problem == "missing" and (variants := sorted(p for p in props if p != name and p.lower() == name.lower())):
                        gap["case_variant"] = variants
                    gaps.append(gap)
                if gaps:
                    entry["symbols_with_gaps"] += 1
                    entry["symbols"].append({**{k: v for k, v in head.items() if k != "sheet"}, "uses": sym.uses, "gaps": gaps})
            entry["symbols"].sort(key=lambda s: (s["reference"], s["unit"], s["uuid"]))
            sheets.append(entry)
        checked = sum(e["checked"] for e in sheets)
        with_gaps = sum(e["symbols_with_gaps"] for e in sheets)
        for rows in skipped.values():
            rows.sort(key=lambda s: (s["sheet"], s["reference"], s["unit"], s["uuid"]))
        return {
            "status": "ok", "ok": True, "written": False, "root": tree.rel(root_file), "required_fields": required,
            "placed_symbols": placed, "checked": checked, "symbols_with_gaps": with_gaps, "no_gaps": with_gaps == 0,
            "gap_count": sum(len(s["gaps"]) for e in sheets for s in e["symbols"]),
            "sheets": sheets,
            "skipped": skipped, "skipped_counts": {reason: len(rows) for reason, rows in skipped.items()},
            "skipped_note": "power symbols, off-board symbols and DNP symbols are not checked; each is listed under skipped by its first matching reason (power, off_board, dnp), and a symbol node with no lib_id is listed under no_lib_id",
            **{k: v for k, v in tree.report().items() if k != "sheets"},
        }
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)


DEFAULT_READ = ("Value", "Footprint", "Datasheet", "Description", "LCSC")


def _names(value: Any, what: str, *, allow_none: bool = True) -> list[str] | None:
    """A non-empty list of distinct strings (property names are validated like set_properties does), or None when omitted."""
    if value is None and allow_none:
        return None
    if not isinstance(value, list) or not value:
        raise SheetAuthoringError("invalid_spec", f"{what} must be a non-empty list of strings, or omitted")
    if what == "properties":
        names = [sheet_editing._property_name(n, what) for n in value]
    else:
        names = []
        for n in value:
            if not isinstance(n, str) or not n or n != n.strip():
                raise SheetAuthoringError("invalid_spec", f"{what} holds {n!r}: each entry must be a non-empty string without white space at the ends")
            names.append(n)
    if len(set(names)) != len(names):
        raise SheetAuthoringError("invalid_spec", f"{what} names an entry twice")
    return names


def get_properties(sch_path: str, references: Any = None, uuids: Any = None, properties: Any = None, root_sch: str | None = None) -> dict[str, Any]:
    """Read-only: the requested properties of the selected (default: all) placed symbols of the project, per sheet file (issue 122)."""
    try:
        refs_wanted = _names(references, "references")
        uuids_wanted = _names(uuids, "uuids")
        asked = _names(properties, "properties") or list(DEFAULT_READ)
        root_file, tree = _resolve(sch_path, root_sch)
        selecting = refs_wanted is not None or uuids_wanted is not None
        want_refs, want_uuids = set(refs_wanted or ()), {u.lower() for u in uuids_wanted or ()}
        found_refs: set[str] = set()
        found_uuids: set[str] = set()
        sheets: list[dict[str, Any]] = []
        placed = returned = 0
        absent_total = 0
        for rel, rows in _walk(tree):
            entry: dict[str, Any] = {"sheet": rel, "symbols": []}
            for sym in rows:
                placed += 1
                symbol = sym.node
                names = {*sym.refs, sym.own}
                if selecting:
                    hit_refs = want_refs & names
                    hit_uuid = sym.uuid.lower() in want_uuids
                    if not hit_refs and not hit_uuid:
                        continue
                    found_refs |= hit_refs
                    if hit_uuid:
                        found_uuids.add(sym.uuid.lower())
                props = _props(symbol)
                values = {name: props[name] for name in asked if name in props}
                absent = []
                for name in asked:
                    if name in props:
                        continue
                    row: dict[str, Any] = {"name": name}
                    if variants := sorted(p for p in props if p.lower() == name.lower()):
                        row["case_variant"] = variants
                    absent.append(row)
                flags = [flag for flag, on in (
                    ("power", connectivity_check.is_power(sym.parsed, symbol) or any(r.startswith("#") for r in names)),
                    ("off_board", any(sheet_tree._atom(n, 0) == "no" for n in symbol.find_children("on_board"))),
                    ("dnp", connectivity_check._dnp(symbol)), ("no_lib_id", not sym.lib_id)) if on]
                out: dict[str, Any] = {"reference": sym.refs[0], "uuid": sym.uuid, "unit": sym.unit, "lib_id": sym.lib_id,
                                       "properties": values, "absent": absent, "flags": flags, "uses": sym.uses}
                if sym.refs[0] != sym.own:
                    out["reference_text"] = sym.own
                if len(sym.refs) > 1:
                    out["use_references"] = sym.refs
                entry["symbols"].append(out)
                returned += 1
                absent_total += len(absent)
            entry["symbols"].sort(key=lambda s: (s["reference"], s["unit"], s["uuid"]))
            sheets.append(entry)
        not_found = {"references": sorted(want_refs - found_refs), "uuids": sorted(u for u in (uuids_wanted or ()) if u.lower() not in found_uuids)}
        return {
            "status": "ok", "ok": True, "written": False, "root": tree.rel(root_file), "properties_asked": asked,
            "placed_symbols": placed, "returned": returned, "absent_count": absent_total,
            "selected_by": "references and uuids" if refs_wanted is not None and uuids_wanted is not None else
                           "references" if refs_wanted is not None else "uuids" if uuids_wanted is not None else "all",
            "not_found": not_found, "all_found": not (not_found["references"] or not_found["uuids"]),
            "sheets": sheets,
            "note": "properties holds the requested names that exist (an empty value is present, as \"\"); absent lists the requested names the "
                    "symbol has no property of, with case_variant when one exists under a name that differs only in case; flags marks power, "
                    "off_board, dnp and no_lib_id symbols, which are returned too",
            **{k: v for k, v in tree.report().items() if k != "sheets"},
        }
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)
