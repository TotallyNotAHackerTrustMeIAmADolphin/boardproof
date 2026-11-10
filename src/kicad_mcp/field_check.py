"""check_symbol_fields: a read-only census of the fields of placed symbols (issue 163).

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
from typing import Any

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


def check_symbol_fields(sch_path: str, required_fields: Any = None, root_sch: str | None = None) -> dict[str, Any]:
    """Read-only: every placed symbol of the project with a gap in ``required_fields``, per sheet file."""
    try:
        required = _fields(required_fields)
        file = Path(sch_path)
        if not file.is_file():
            raise SheetAuthoringError("file_not_found", f"sheet not found: {file}")
        root_file = sheet_authoring.find_root(file, root_sch)
        tree = sheet_authoring._require_tree(root_file)
        if file.resolve() not in {f.resolve() for f in tree.files}:
            raise SheetAuthoringError("parent_not_in_tree", f"{file.name} is not reached from the root sheet {root_file.name}", **tree.report())
        by_symbol: dict[tuple[str, str], list[dict[str, str]]] = {}
        for rec in sheet_tree.placed_instances(tree):
            by_symbol.setdefault((rec["sheet"], rec["uuid"]), []).append(rec)
        sheets: list[dict[str, Any]] = []
        skipped: dict[str, list[dict[str, Any]]] = {reason: [] for reason in _SKIP_ORDER}
        placed = 0
        for path in tree.files:
            rel = tree.rel(path)
            parsed = sheet_tree.parsed_sheet(tree, path)
            entry: dict[str, Any] = {"sheet": rel, "checked": 0, "symbols_with_gaps": 0, "symbols": []}
            for symbol in parsed.find_children("symbol"):
                placed += 1
                uuid = sheet_tree._uuid(symbol)
                if not symbol.find_children("lib_id"):  # not a placed part KiCad can read fields of: counted, never silently dropped
                    skipped["no_lib_id"].append({"reference": sheet_tree._property(symbol, "Reference"), "uuid": uuid, "unit": "", "lib_id": "", "sheet": rel})
                    continue
                records = by_symbol.get((rel, uuid), [])
                own = sheet_tree._property(symbol, "Reference")
                refs = [r["ref"] for r in records] or [own]
                unit = records[0]["unit"] if records else "1"
                head = {"reference": refs[0], "uuid": uuid, "unit": unit, "lib_id": sheet_tree._atom(symbol.find_children("lib_id")[0], 0), "sheet": rel}
                if (why := _skip_reason(parsed, symbol, [*refs, own])) is not None:
                    skipped[why].append(head)
                    continue
                props = {sheet_tree._atom(p, 0): sheet_tree._atom(p, 1) for p in reversed(symbol.find_children("property"))}
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
                    entry["symbols"].append({**{k: v for k, v in head.items() if k != "sheet"}, "uses": len(records) or 1, "gaps": gaps})
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
