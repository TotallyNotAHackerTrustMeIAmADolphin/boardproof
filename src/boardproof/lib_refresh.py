"""Bring the library symbols embedded in a sheet up to date with the library (GitHub issue 115, tool ``refresh_lib_symbols``).

The gap, measured with kicad-cli 10 on a project library changed through the write layer
(docs/ARCHITECTURE.md, known facts):

- the copy of a library symbol that a sheet embeds in ``lib_symbols`` stays as it was when the library
  symbol changes; ``add_symbols`` reads the sheet's embedded copy for a ``lib_id`` it already holds
  (``seed_symbol_defs`` skips a cached one), so a symbol placed afterwards gets the OLD pin layout;
- KiCad's ERC reports one ``lib_symbol_mismatch`` warning per placed symbol whose embedded copy differs
  from the library.

``refresh_lib_symbols`` replaces the embedded copy of each STALE chosen ``lib_id`` by the library's current
symbol, the one ``add_symbols`` would embed (``sheet_editing._load_library_entry``). Stale means: kicad-cli's ERC
names a placed symbol of it in a ``lib_symbol_mismatch`` finding, or its pin census differs from the library's.
Text is never compared: a sheet KiCad wrote embeds copies in KiCad's newer format (``in_pos_files``,
``duplicate_pin_numbers_are_jumpers``, ``show_name``, an empty pin name), the library file of the pic_programmer
demo is older, and the two differ as text while KiCad sees no mismatch (measured). The copy this tool writes has
the form of the library file, like the one ``add_symbols`` writes. Nothing else is touched: placed symbols, their instance data, wires and labels keep their bytes. It runs on the pipeline of
every sheet writer (``sheet_editing.run``: lock check, staged check, kicad-cli netlist and ERC of scratch
copies before and after, write layer with reload verify, ``dry_run``).

What it refuses, and what it reports:

- a placed pin number the new symbol no longer has (``pins_removed``, listed per symbol, no override: the
  placed symbol would carry a pin that does not exist), and a placed unit the new symbol lacks
  (``unit_missing``);
- a netlist that changes (``connectivity_changed``: a wired pin that moved, a new pin on a wire end,
  a new unconnected pin) unless ``allow_connectivity_change``;
- an ERC census with MORE violations of any type than before (``erc_changed``) unless
  ``allow_connectivity_change``; fewer is the point (the ``lib_symbol_mismatch`` warnings go) and is listed;
- a refreshed copy whose pins differ from the pins of the library file read raw (``write_not_verified``).

Pins that moved, were renamed or retyped, and pins the new symbol adds, are listed per symbol. Placed
symbols that use a ``lib_name`` variant of a library symbol are listed and left alone.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import kicad_cli, project_check, sheet_authoring, symbol_libs, write_layer
from boardproof import sheet_editing as se
from boardproof.sheet_authoring import SheetAuthoringError

MAX_LISTED = se.MAX_LISTED


def _check_lib_ids(lib_ids: Any) -> list[str] | None:
    if lib_ids is None:
        return None
    if not isinstance(lib_ids, list) or not lib_ids:
        raise SheetAuthoringError("invalid_spec", "lib_ids must be a non-empty list of Library:Symbol names, or omitted for every library symbol the sheet embeds")
    for lib_id in lib_ids:
        if not isinstance(lib_id, str) or not lib_id.strip() or lib_id != lib_id.strip() or any(c in lib_id for c in "\r\n\t  "):
            raise SheetAuthoringError("invalid_spec", f"lib_ids holds {lib_id!r}: each must be a non-empty one-line Library:Symbol name without edge whitespace")
        nick, _, name = lib_id.partition(":")
        if not nick or not name:
            raise SheetAuthoringError("invalid_spec", f"lib_ids holds {lib_id!r}, which has no Library:Symbol form")
    if len(set(lib_ids)) != len(lib_ids):
        raise SheetAuthoringError("invalid_spec", "lib_ids names the same library symbol twice")
    return list(lib_ids)


def _pin_census(symbol: SExp) -> Counter:
    """Every pin of a library symbol (all its units and body styles) as (unit, style, number, name, type, x, y, angle),
    read from the raw nodes."""
    census: Counter = Counter()
    for sub in se._kids(symbol, "symbol"):
        parts = (sub.get_string(0) or "").rsplit("_", 2)
        unit, style = (parts[1], parts[2]) if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit() else ("?", "?")
        for pin in se._kids(sub, "pin"):
            at = pin.find_children("at")
            name, number = pin.find_children("name"), pin.find_children("number")
            census[(
                unit, style, (number[0].get_string(0) if number else "") or "~", (name[0].get_string(0) if name else "") or "~",
                str(pin.children[0].value) if pin.children and pin.children[0].is_atom else "",
                round(at[0].get_float(0), 4) if at else None, round(at[0].get_float(1), 4) if at else None,
                round(at[0].get_float(2), 4) if at and len(at[0].children) > 2 else 0.0,
            )] += 1
    return census


def _library_file(sheet_path: Path, lib_id: str) -> Path | None:
    nick = lib_id.split(":", 1)[0]
    resolution = symbol_libs.resolve_symbol_libraries(sheet_path, [lib_id])
    if nick in resolution.files:
        return resolution.files[nick]
    for folder in symbol_libs.kicad_symbol_dirs():
        if (folder / f"{nick}.kicad_sym").is_file():
            return folder / f"{nick}.kicad_sym"
    return None


def _raw_library_census(sheet_path: Path, lib_id: str) -> Counter | None:
    """The pins of the library symbol read raw from its file; None when the symbol extends another (the embedded copy is
    flattened, so the two are not comparable pin for pin here)."""
    file = _library_file(sheet_path, lib_id)
    if file is None:
        raise SheetAuthoringError("write_not_verified", f"check embedded_pins_equal_library: the library file of {lib_id!r} cannot be found again; nothing was written", check="embedded_pins_equal_library")
    root = write_layer.load(file)
    name = lib_id.split(":", 1)[1]
    found = [s for s in se._kids(root, "symbol") if s.get_string(0) == name]
    if len(found) != 1:
        raise SheetAuthoringError("write_not_verified", f"check embedded_pins_equal_library: the library file has {len(found)} symbols named {name!r}; nothing was written", check="embedded_pins_equal_library")
    if found[0].find_children("extends"):
        return None
    return _pin_census(found[0])


def _load(path: Path, lib_id: str, explicit: bool) -> tuple[SExp | None, str | None]:
    """The library's current embeddable node of ``lib_id`` and no reason, or None and why it cannot be had (an explicit
    request refuses instead)."""
    try:
        nodes = se._load_library_entry(path, lib_id)
    except SheetAuthoringError as exc:
        if explicit:
            raise
        return None, str(exc)
    entry = [n for n in nodes if n.get_string(0) == lib_id]
    if len(entry) != 1 or len(nodes) != 1:
        reason = f"the library holds {len(entry)} symbol(s) named {lib_id!r} and {len(nodes)} to embed"
        if explicit:
            raise SheetAuthoringError("lib_unresolved", f"{lib_id}: {reason}; nothing was written", lib_id=lib_id)
        return None, reason
    return entry[0], None


def _pin_row(pin: dict) -> dict:
    return {"pin": pin["number"], "name": pin["name"], "type": pin.get("type", ""), "at": [pin["x"], pin["y"]]}


def _erc(sheet: se.Sheet, sheet_file: Path, replacement: SExp | None) -> tuple[Counter, list[str], set[str]]:
    """kicad-cli's ERC census of the project from a scratch copy that also holds the project-local symbol libraries (without
    them ERC cannot compare an embedded copy with its library and shows no lib_symbol_mismatch: measured), with
    ``replacement`` in the place of this sheet. Returns the census, what could not be copied and the uuids kicad-cli names in its lib_symbol_mismatch findings."""
    tree = sheet.tree
    with tempfile.TemporaryDirectory(prefix="boardproof-refresh-erc-") as tmp:
        scratch = Path(tmp) / "p"
        copied = sheet_authoring._copy_project(tree, [], scratch)
        problems = project_check._copy_project_libraries(tree.project_dir, scratch)
        if replacement is not None:
            sheet_authoring._save(replacement, scratch / sheet_file.resolve().relative_to(tree.project_dir))
        from boardproof import kicad_cli

        report = kicad_cli.run_erc(copied)
        census: Counter = Counter()
        stale: set[str] = set()
        for sh in report.get("sheets", []):
            for v in sh.get("violations", []):
                census[(v.get("type", "?"), v.get("severity", "?"))] += 1
                if v.get("type") == "lib_symbol_mismatch":
                    stale |= {str(i["uuid"]) for i in v.get("items", []) if i.get("uuid")}
        return census, problems, stale


def refresh(path: str, lib_ids: Any, root_sch: str | None, *, allow_connectivity_change: bool, dry_run: bool) -> dict[str, Any]:
    """Refresh the embedded library symbols of one sheet file (the module doc says what and why)."""
    sheet_file = Path(path)

    def mutate(sheet: se.Sheet, root: SExp) -> se.Outcome:
        wanted = _check_lib_ids(lib_ids)
        table = next(iter(se._kids(root, "lib_symbols")), None)
        entries = se._kids(table, "symbol") if table is not None else []
        names = [e.get_string(0) or "" for e in entries]
        if len(set(names)) != len(names):
            raise SheetAuthoringError("invalid_spec", f"the sheet embeds a library symbol twice ({sorted(n for n in set(names) if names.count(n) > 1)}); nothing was written")
        skipped: list[dict] = []
        if wanted is None:
            chosen = [n for n in names if ":" in n]
            skipped += [{"library_symbol": n, "reason": "not a Library:Symbol name (a lib_name variant or a symbol of this sheet only)"} for n in names if ":" not in n]
        else:
            missing = [n for n in wanted if n not in names]
            if missing:
                raise SheetAuthoringError("not_found", f"the sheet embeds no library symbol named {missing}; nothing was written (it embeds {sorted(names)[:MAX_LISTED]})", not_found=missing)
            chosen = list(wanted)

        placed = se._placed(sheet)
        outcome = se.Outcome()
        erc_memo: dict[str, Any] = {}

        def erc_state() -> tuple[Counter, list[str], set[str]]:
            if "v" not in erc_memo:
                erc_memo["v"] = _erc(sheet, sheet_file, None)
            return erc_memo["v"]

        try:  # kicad-cli's own judgement of which embedded copies differ from the library. If it cannot run, the pin census alone says what is stale; a refresh that finds something still needs the ERC comparison below and ends in kicad_cli_failed, nothing written
            stale_uuids = erc_state()[2]
            erc_read = True
        except kicad_cli.KicadCliError:
            stale_uuids, erc_read = set(), False
        stale_names = {s.find_children("lib_id")[0].get_string(0) for s in placed
                       if se._uuid(s) in stale_uuids and not s.find_children("lib_name")}
        refreshed: list[dict] = []
        unchanged: list[str] = []
        removed_pins: list[dict] = []
        extras: list[dict] = []
        lib_name_users: list[dict] = []
        expected: dict[str, Counter | None] = {}
        for name in chosen:
            new_entry, why = _load(sheet_file, name, wanted is not None)
            if new_entry is None:
                skipped.append({"library_symbol": name, "reason": why})
                continue
            old_entry = next(e for e in entries if e.get_string(0) == name)
            users = [s for s in placed if se._lib_name_of(s) == name and s.find_children("lib_id")[0].get_string(0) == name]
            variants = [s for s in placed if s.find_children("lib_id")[0].get_string(0) == name and s.find_children("lib_name")]
            lib_name_users += [{"library_symbol": name, "reference": se._ref(s), "uuid": se._uuid(s), "lib_name": se._lib_name_of(s)} for s in variants]
            reasons = (["erc_lib_symbol_mismatch"] if name in stale_names else []) + (["pins"] if _pin_census(old_entry) != _pin_census(new_entry) else [])
            if not reasons:
                unchanged.append(name)
                continue
            before = {se._uuid(s): se.pin_positions(root, s, with_type=True) for s in users}
            table.children[table.children.index(old_entry)] = new_entry
            outcome.lib_changed.add(name)
            expected[name] = _raw_library_census(sheet_file, name)
            per_symbol: list[dict] = []
            for node in users:
                who = f"{se._ref(node)} ({se._uuid(node)})"
                unit = se._unit_of(node)
                units = se._library_units(new_entry)
                if unit not in units - {0} and not (unit == 1 and not units - {0}):
                    raise SheetAuthoringError("unit_missing", f"{who} is unit {unit} but the library's {name} has units {sorted(units - {0})}; nothing was written", lib_id=name)
                old_pins = before[se._uuid(node)]
                new_pins = se.pin_positions(root, node, with_type=True)
                pairs, gone = se._pair_pins(old_pins, new_pins, {}, who)
                for pin in gone:
                    removed_pins.append({"library_symbol": name, "reference": se._ref(node), "uuid": se._uuid(node), **_pin_row(pin),
                                         "wired": bool(se._attached(sheet, (pin["x"], pin["y"]), node))})
                moved, renamed = [], []
                for old, new in pairs:
                    if not se._same((old["x"], old["y"]), (new["x"], new["y"])):
                        found = se._attached(sheet, (old["x"], old["y"]), node)
                        moved.append({"pin": new["number"], "name": new["name"], "was": [old["x"], old["y"]], "now": [new["x"], new["y"]], "wired": bool(found)})
                        if found:
                            extras.append({"type": "pin_moved", "reference": se._ref(node), "pin": new["number"], "was": [old["x"], old["y"]],
                                           "now": [new["x"], new["y"]], "attached": found[:5],
                                           "note": "the wires stay where they are; the new pin is not on them"})
                    if (old["name"], old.get("type")) != (new["name"], new.get("type")):
                        renamed.append({"pin": new["number"], "was": {"name": old["name"], "type": old.get("type", "")}, "now": {"name": new["name"], "type": new.get("type", "")}})
                paired_new = {id(n) for _o, n in pairs}
                added = []
                for pin in new_pins:
                    if id(pin) in paired_new:
                        continue
                    found = se._attached(sheet, (pin["x"], pin["y"]), node)
                    added.append({**_pin_row(pin), "wired": bool(found)})
                    if found:
                        extras.append({"type": "new_pin_attached", "reference": se._ref(node), "pin": pin["number"], "at": [pin["x"], pin["y"]], "attached": found[:5]})
                per_symbol.append({"reference": se._ref(node), "uuid": se._uuid(node), "unit": unit, "pins_moved": moved,
                                   "pins_changed": renamed, "pins_added": added})
            refreshed.append({"library_symbol": name, "found_by": reasons, "placed_symbols": len(users), "symbols": per_symbol})
        if removed_pins:
            raise SheetAuthoringError(
                "pins_removed", f"{len(removed_pins)} placed pin(s) no longer exist in the library symbol "
                f"(first: {removed_pins[0]['reference']}.{removed_pins[0]['pin']} of {removed_pins[0]['library_symbol']}); nothing was written. "
                "Replace those symbols with replace_symbols (pin_map) or delete them, or leave the library symbol out of lib_ids.",
                removed_pins=removed_pins[:MAX_LISTED], count=len(removed_pins))

        # the refreshed copy must carry the pins of the library file read raw
        for name, census in expected.items():
            if census is None:
                continue
            now = next(e for e in se._kids(table, "symbol") if e.get_string(0) == name)
            if _pin_census(now) != census:
                raise SheetAuthoringError(
                    "write_not_verified", f"check embedded_pins_equal_library: the refreshed copy of {name} has other pins than the library file; nothing was written",
                    check="embedded_pins_equal_library", subject=name)

        outcome.report = {
            "refreshed": refreshed, "unchanged": unchanged, "skipped": skipped, "lib_name_symbols_left": lib_name_users[:MAX_LISTED],
            "count": len(refreshed), "erc_mismatch_read": erc_read,
            "note": "only the embedded copies in this sheet file changed; placed symbols, wires, labels and instance data are untouched; "
                    "other sheet files keep their own copies (call again per sheet)"}
        if not refreshed:
            outcome.no_change = True
            outcome.check_nets = False
            outcome.report["note"] = "every embedded library symbol already equals the library (or could not be read: see skipped); nothing to write"
            return outcome
        outcome.compare = lambda before, after, removed: [*extras, *se.compare_nets(before, after, removed)]
        erc_before, problems_before, _stale = erc_state()
        erc_after, _problems_after, _stale_after = _erc(sheet, sheet_file, root)
        grew = {f"{t}/{sev}": erc_after[(t, sev)] - erc_before[(t, sev)] for (t, sev) in erc_after if erc_after[(t, sev)] > erc_before[(t, sev)]}
        gone_erc = {f"{t}/{sev}": erc_before[(t, sev)] - erc_after[(t, sev)] for (t, sev) in erc_before if erc_before[(t, sev)] > erc_after[(t, sev)]}
        outcome.report["erc"] = {"library_problems": problems_before, "total_before": sum(erc_before.values()), "total_after": sum(erc_after.values()), "increased": grew, "decreased": gone_erc,
                                 "lib_symbol_mismatch_before": sum(n for (t, _s), n in erc_before.items() if t == "lib_symbol_mismatch"),
                                 "lib_symbol_mismatch_after": sum(n for (t, _s), n in erc_after.items() if t == "lib_symbol_mismatch")}
        if grew and not allow_connectivity_change:
            raise SheetAuthoringError(
                "erc_changed", f"the refresh would add ERC violations ({grew}); nothing was written. Check them (a new pin that is not connected is one), "
                "then repeat with allow_connectivity_change true if that is meant.", erc=outcome.report["erc"])
        return outcome

    result = se.run(path, root_sch, mutate, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_refresh"] = result.pop("refreshed")
    return result
