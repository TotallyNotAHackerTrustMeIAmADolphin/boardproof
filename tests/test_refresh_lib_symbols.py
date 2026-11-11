"""refresh_lib_symbols (GitHub issue 115), with real kicad-cli.

The measured gap: a library symbol changed through the write layer leaves the copy a sheet embeds as it was,
``add_symbols`` places a new symbol with the OLD copy, and KiCad's ERC reports ``lib_symbol_mismatch`` per placed
symbol. The template project is built once through the tools (project library ``parts`` registered through
``register_library_asset``, written with kicad_tools' S-expression model on ``tmp_path``; no KiCad file is edited as
text); every test works on its own copy and changes the library through the write layer again.

Library pins sit where Device:R's sit: sheet x = origin x + pin x, sheet y = origin y - pin y (rotation 0).
Sheet: U1 (50.8, 50.8) and U2 (76.2, 50.8) of parts:Part, W1 (101.6, 50.8) of parts:Other and R1 of Device:R.
Pin 1 of U1 and U2 is wired (net NETA); pin 2 of both is free.
"""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp, parse_string
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, lib_refresh, sheet_authoring, sheet_editing, write_layer
from boardproof.toolsets import library as library_toolset
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


@pytest.fixture(autouse=True)
def _kicad_cli_calls_fail_fast(monkeypatch):
    """A stuck kicad-cli call fails this test after two minutes (the default limit is fifteen), never hangs the file."""
    monkeypatch.setattr(kicad_cli, "_TIMEOUT_SECONDS", 120)


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def _tools(module=schematic_toolset) -> dict:
    mcp = MCPServer("test")
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _pin(number: str, name: str, x: float, y: float, angle: int) -> str:
    return (f'(pin passive line (at {x} {y} {angle}) (length 1.27) (name "{name}" (effects (font (size 1.27 1.27)))) '
            f'(number "{number}" (effects (font (size 1.27 1.27)))))')


def _symbol(name: str, units: list[list[str]]) -> str:
    body = "".join(f'(symbol "{name}_{i}_1" {" ".join(pins)})' for i, pins in enumerate(units, start=1))
    return (f'(symbol "{name}" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes) '
            '(property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27)))) '
            f'(property "Value" "{name}" (at 0 0 0) (effects (font (size 1.27 1.27)))) '
            '(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide)) '
            '(property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide)) '
            f'(symbol "{name}_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254) (type default)) (fill (type none)))) '
            f"{body})")


P1, P2 = _pin("1", "A", 0, 3.81, 270), _pin("2", "B", 0, -3.81, 90)
OTHER = _symbol("Other", [[_pin("1", "X", 0, 3.81, 270), _pin("2", "Y", 0, -3.81, 90)]])
DUAL = _symbol("Dual", [[P1, P2], [_pin("3", "C", 0, 3.81, 270), _pin("4", "D", 0, -3.81, 90)]])


def _lib_text(*symbols: str) -> str:
    return '(kicad_symbol_lib (version 20231120) (generator "test") (generator_version "8.0") ' + " ".join(symbols) + ")"


def _part(*pins: str) -> str:
    return _symbol("Part", [list(pins)])


def _set_library(project: Path, *symbols: str) -> None:
    """Change the project library through the write layer (kicad_tools' S-expression model)."""
    write_layer.save(parse_string(_lib_text(*symbols)), project / "lib" / "parts.kicad_sym")


def _wire(p1, p2):
    return {"p1": list(p1), "p2": list(p2), "route": "direct", "snap": False}


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("refresh_template")
    made = project_toolset._project_create(str(root / "proj"), "demo")
    sch = Path(made["schematic_path"])
    lib = root / "proj" / "lib" / "parts.kicad_sym"
    lib.parent.mkdir(parents=True, exist_ok=True)
    write_layer.save(parse_string(_lib_text(_part(P1, P2), OTHER, DUAL)), lib)
    registered = _tools(library_toolset)["register_library_asset"](str(root / "proj"), symbol_lib_path=str(lib))
    assert registered["symbol"]["status"] == "ok", registered
    tools = _tools()
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "parts:Part", "x": 50.8, "y": 50.8, "ref": "U1"},
        {"lib_id": "parts:Part", "x": 76.2, "y": 50.8, "ref": "U2"},
        {"lib_id": "parts:Other", "x": 101.6, "y": 50.8, "ref": "W1"},
        {"lib_id": "parts:Dual", "x": 127.0, "y": 50.8, "ref": "D1", "unit": 2},
        {"lib_id": "Device:R", "x": 152.4, "y": 50.8, "ref": "R1", "value": "10k"},
    ])["status"] == "ok"
    assert tools["add_wires"](str(sch), [
        _wire((50.8, 46.99), (50.8, 40.64)), _wire((50.8, 40.64), (76.2, 40.64)), _wire((76.2, 40.64), (76.2, 46.99)),
    ])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 63.5, "y": 40.64, "text": "NETA", "snap": False}])["status"] == "ok"
    return sch


@pytest.fixture
def sheet(template, tmp_path) -> Path:
    shutil.copytree(template.parent, tmp_path / "proj")
    return tmp_path / "proj" / template.name


def _project(sch: Path) -> Path:
    return sch.parent


def _refresh(sch: Path, **kw) -> dict:
    return _tools()["refresh_lib_symbols"](str(sch), **kw)


def _bytes(sch: Path) -> dict:
    return {str(p.relative_to(sch.parent)): p.read_bytes() for p in sch.parent.rglob("*") if p.is_file() and ".kicad_prl" not in p.name}


def _root(sch: Path) -> SExp:
    return write_layer.load(sch)


def _kids(node: SExp, tag: str) -> list[SExp]:
    return [c for c in node.children if not c.is_atom and c.name == tag]


def _embedded(sch: Path, name: str) -> SExp:
    return next(e for e in _kids(_kids(_root(sch), "lib_symbols")[0], "symbol") if e.get_string(0) == name)


def _symbol_node(sch: Path, ref: str) -> SExp:
    return next(s for s in _kids(_root(sch), "symbol") if sheet_editing._ref(s) == ref)


def _library_node(sch: Path, name: str) -> SExp:
    return next(s for s in _kids(write_layer.load(_project(sch) / "lib" / "parts.kicad_sym"), "symbol") if s.get_string(0) == name)


def _pins(sch: Path, ref: str) -> dict[str, tuple[float, float]]:
    out = _tools()["get_symbol_pins"](str(sch), [ref])["symbols"]
    return {p["number"]: (p["x"], p["y"]) for s in out for p in s["pins"]}


def _mismatches(sch: Path) -> int:
    report = kicad_cli.run_erc(sch)
    return sum(1 for s in report.get("sheets", []) for v in s.get("violations", []) if v.get("type") == "lib_symbol_mismatch")


def _text(node: SExp) -> str:
    return write_layer.serialise(node, compact=True)


# --------------------------------------------------------------------------- the measured gap


def test_the_gap_a_library_change_leaves_the_embedded_copy_stale_and_add_symbols_places_with_it(sheet):
    """The first step of issue 115 (measured, docs/ARCHITECTURE.md): the copy stays, a new symbol gets the old layout, ERC warns."""
    old = _text(_embedded(sheet, "parts:Part"))
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    assert _text(_embedded(sheet, "parts:Part")) == old
    assert _tools()["add_symbols"](str(sheet), [{"lib_id": "parts:Part", "x": 50.8, "y": 90.0, "ref": "U3"}])["status"] == "ok"
    assert _text(_embedded(sheet, "parts:Part")) == old
    assert _pins(sheet, "U3")["2"] == (50.8, 93.81)  # the old layout: 3.81 below the origin, not 5.08
    assert _mismatches(sheet) == 3


# --------------------------------------------------------------------------- refresh


def test_nothing_changed_nothing_written_and_every_symbol_listed_unchanged(sheet):
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is True and out["written"] is False and out["count"] == 0, out
    assert set(out["unchanged"]) == {"parts:Part", "parts:Other", "parts:Dual", "Device:R"}
    assert _bytes(sheet) == before


def test_a_moved_free_pin_is_refreshed_the_copy_equals_the_library_and_the_rest_is_untouched(sheet):
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    placed_before = {r: _text(_symbol_node(sheet, r)) for r in ("U1", "U2", "W1", "D1", "R1")}
    other_before = {n: _text(_embedded(sheet, n)) for n in ("parts:Other", "parts:Dual", "Device:R")}
    assert _mismatches(sheet) == 2
    out = _refresh(sheet)
    assert out["ok"] is True and out["written"] is True and out["count"] == 1, out
    assert out["connectivity"]["changed"] is False
    assert out["erc"]["lib_symbol_mismatch_before"] == 2 and out["erc"]["lib_symbol_mismatch_after"] == 0
    assert out["erc"]["increased"] == {}
    entry = out["refreshed"][0]
    assert entry["library_symbol"] == "parts:Part" and entry["placed_symbols"] == 2
    assert {s["reference"] for s in entry["symbols"]} == {"U1", "U2"}
    moved = entry["symbols"][0]["pins_moved"]
    assert [m["pin"] for m in moved] == ["2"] and moved[0]["wired"] is False
    # the embedded copy equals the library symbol read raw (name carries the library prefix, the rest is the library's)
    raw = _library_node(sheet, "Part")
    embedded = _embedded(sheet, "parts:Part")
    assert lib_refresh._pin_census(embedded) == lib_refresh._pin_census(raw)
    assert _text(embedded).replace('"parts:Part"', '"Part"', 1) == _text(raw)
    # nothing else changed: placed symbols with their pins and instance data, the other embedded copies
    assert {r: _text(_symbol_node(sheet, r)) for r in placed_before} == placed_before
    assert {n: _text(_embedded(sheet, n)) for n in other_before} == other_before
    assert _mismatches(sheet) == 0
    assert _pins(sheet, "U1")["2"] == (50.8, 55.88)


def test_a_symbol_placed_after_the_refresh_gets_the_new_layout(sheet):
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    assert _refresh(sheet)["ok"] is True
    assert _tools()["add_symbols"](str(sheet), [{"lib_id": "parts:Part", "x": 50.8, "y": 90.0, "ref": "U3"}])["status"] == "ok"
    assert _pins(sheet, "U3")["2"] == (50.8, 95.08)


def test_lib_ids_picks_the_symbols_and_leaves_the_others_stale(sheet):
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)),
                 _symbol("Other", [[_pin("1", "X", 0, 3.81, 270), _pin("2", "Y", 0, -5.08, 90)]]), DUAL)
    out = _refresh(sheet, lib_ids=["parts:Other"])
    assert out["ok"] is True and [r["library_symbol"] for r in out["refreshed"]] == ["parts:Other"], out
    assert _pins(sheet, "W1")["2"] == (101.6, 55.88)
    assert _pins(sheet, "U1")["2"] == (50.8, 54.61)  # parts:Part was not named: still the old copy


def test_dry_run_computes_everything_and_writes_nothing(sheet):
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    before = _bytes(sheet)
    out = _refresh(sheet, dry_run=True)
    assert out["ok"] is True and out["written"] is False and "refreshed" not in out, out
    assert out["would_refresh"][0]["library_symbol"] == "parts:Part" and out["erc"]["lib_symbol_mismatch_after"] == 0
    assert _bytes(sheet) == before


def test_a_pin_that_gains_a_name_or_type_is_listed_as_changed(sheet):
    _set_library(_project(sheet), _part(P1, "(pin input line (at 0 -3.81 90) (length 1.27) (name \"RENAMED\" (effects (font (size 1.27 1.27)))) "
                                            "(number \"2\" (effects (font (size 1.27 1.27)))))"), OTHER, DUAL)
    out = _refresh(sheet, dry_run=True, allow_connectivity_change=True)  # an input pin nobody drives is a new ERC finding
    assert out["ok"] is True, out
    changed = out["would_refresh"][0]["symbols"][0]["pins_changed"]
    assert changed == [{"pin": "2", "was": {"name": "B", "type": "passive"}, "now": {"name": "RENAMED", "type": "input"}}]


def test_a_symbol_with_a_lib_name_variant_is_listed_and_left_alone(sheet):
    """KiCad keeps a second variant of one lib_id as (lib_name ...); it is not this tool's to rewrite."""
    root = _root(sheet)
    table = _kids(root, "lib_symbols")[0]
    text = _text(_embedded(sheet, "parts:Part")).replace('"parts:Part"', '"Part_1"', 1)
    variant = parse_string(text.replace('"Part_1_1"', '"Part_1_1_1"').replace('"Part_0_1"', '"Part_1_0_1"'))
    table.children.append(variant)
    u1 = next(s for s in _kids(root, "symbol") if sheet_editing._ref(s) == "U1")
    lib_id = u1.find_children("lib_id")[0]
    u1.children.insert(u1.children.index(lib_id) + 1, SExp.list("lib_name", write_layer.string("Part_1")))
    write_layer.save(root, sheet)
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    u1_before, variant_before = _text(_symbol_node(sheet, "U1")), _text(_embedded(sheet, "Part_1"))
    out = _refresh(sheet)
    assert out["ok"] is True, out
    assert [(u["reference"], u["lib_name"]) for u in out["lib_name_symbols_left"]] == [("U1", "Part_1")]
    assert [s["reference"] for s in out["refreshed"][0]["symbols"]] == ["U2"]
    assert [s["reason"] for s in out["skipped"] if s["library_symbol"] == "Part_1"] == [
        "not a Library:Symbol name (a lib_name variant or a symbol of this sheet only)"]
    assert _text(_symbol_node(sheet, "U1")) == u1_before and _text(_embedded(sheet, "Part_1")) == variant_before


# --------------------------------------------------------------------------- refusals


def test_a_placed_pin_that_no_longer_exists_refuses_the_call_and_lists_it(sheet):
    _set_library(_project(sheet), _part(P1), OTHER, DUAL)
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "pins_removed", out
    assert [(p["reference"], p["pin"], p["library_symbol"]) for p in out["removed_pins"]] == [("U1", "2", "parts:Part"), ("U2", "2", "parts:Part")]
    assert all(p["wired"] is False for p in out["removed_pins"]) and out["count"] == 2
    assert _bytes(sheet) == before


def test_a_removed_pin_that_has_a_wire_says_so(sheet):
    _set_library(_project(sheet), _part(_pin("2", "B", 0, -3.81, 90)), OTHER, DUAL)  # pin 1, the wired one, is gone
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "pins_removed", out
    assert [(p["reference"], p["pin"], p["wired"]) for p in out["removed_pins"]] == [("U1", "1", True), ("U2", "1", True)]


def test_naming_other_lib_ids_still_refreshes_when_one_symbol_lost_a_pin(sheet):
    _set_library(_project(sheet), _part(P1), _symbol("Other", [[_pin("1", "X", 0, 3.81, 270), _pin("2", "Y", 0, -5.08, 90)]]), DUAL)
    assert _refresh(sheet, dry_run=True)["code"] == "pins_removed"
    out = _refresh(sheet, lib_ids=["parts:Other"])
    assert out["ok"] is True and out["written"] is True, out
    assert _pins(sheet, "W1")["2"] == (101.6, 55.88)


def test_a_unit_the_new_symbol_lacks_is_refused(sheet):
    _set_library(_project(sheet), _part(P1, P2), OTHER, _symbol("Dual", [[P1, P2]]))  # D1 is unit 2
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "unit_missing", out
    assert _bytes(sheet) == before


def test_a_wired_pin_that_moved_is_refused_by_the_netlist_and_the_erc_comparison(sheet):
    _set_library(_project(sheet), _part(_pin("1", "A", 0, 5.08, 270), P2), OTHER, DUAL)
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "erc_changed", out
    assert out["erc"]["increased"] and _bytes(sheet) == before
    out = _refresh(sheet, allow_connectivity_change=True, dry_run=True)
    assert out["ok"] is True, out
    moved = out["would_refresh"][0]["symbols"][0]["pins_moved"]
    assert moved == [{"pin": "1", "name": "A", "was": [50.8, 46.99], "now": [50.8, 45.72], "wired": True}]
    assert out["connectivity"]["changed"] is True


def test_the_netlist_comparison_alone_refuses_a_wired_pin_that_moved(sheet, monkeypatch):
    """With the ERC census held equal the netlist comparison still stops it (each guard has its own test)."""
    monkeypatch.setattr(lib_refresh, "_erc", lambda *a: (Counter(), [], set()))
    _set_library(_project(sheet), _part(_pin("1", "A", 0, 5.08, 270), P2), OTHER, DUAL)
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "connectivity_changed", out
    assert _bytes(sheet) == before
    out = _refresh(sheet, allow_connectivity_change=True)
    assert out["ok"] is True and out["written"] is True and out["connectivity"]["changed"] is True, out


def test_a_new_pin_is_listed_and_a_new_unconnected_pin_is_an_erc_increase(sheet):
    _set_library(_project(sheet), _part(P1, P2, _pin("3", "C", 5.08, 0, 180)), OTHER, DUAL)
    before = _bytes(sheet)
    out = _refresh(sheet, dry_run=True, allow_connectivity_change=True)
    assert out["ok"] is True, out
    added = out["would_refresh"][0]["symbols"][0]["pins_added"]
    assert [(p["pin"], p["wired"]) for p in added] == [("3", False)]
    refused = _refresh(sheet)
    assert refused["ok"] is False and refused["code"] in ("erc_changed", "connectivity_changed"), refused
    assert _bytes(sheet) == before


@pytest.mark.parametrize("lib_ids", [[], "parts:Part", [""], ["parts"], ["parts:"], [":Part"], ["parts:Part", "parts:Part"], [3], [" parts:Part"], ["parts:Pa\nrt"]])
def test_a_bad_lib_ids_is_refused_and_writes_nothing(sheet, lib_ids):
    before = _bytes(sheet)
    out = _refresh(sheet, lib_ids=lib_ids)
    assert out["ok"] is False and out["code"] == "invalid_spec", out
    assert _bytes(sheet) == before


def test_a_lib_id_the_sheet_does_not_embed_is_refused(sheet):
    before = _bytes(sheet)
    out = _refresh(sheet, lib_ids=["parts:Missing"])
    assert out["ok"] is False and out["code"] == "not_found" and out["not_found"] == ["parts:Missing"], out
    assert _bytes(sheet) == before


def test_a_library_that_lost_the_symbol_refuses_an_explicit_request_and_is_skipped_for_all(sheet):
    _set_library(_project(sheet), OTHER, DUAL)
    before = _bytes(sheet)
    out = _refresh(sheet, lib_ids=["parts:Part"])
    assert out["ok"] is False and out["code"] == "lib_unresolved", out
    out = _refresh(sheet)
    assert out["ok"] is True and out["written"] is False, out
    assert [s["library_symbol"] for s in out["skipped"]] == ["parts:Part"] and "parts:Part" not in out["unchanged"]
    assert _bytes(sheet) == before


def test_a_missing_sheet_and_a_non_schematic_are_refused(sheet, tmp_path):
    assert _refresh(tmp_path / "nope.kicad_sch")["code"] == "file_not_found"
    out = _refresh(sheet.parent / "sym-lib-table")
    assert out["ok"] is False and out["code"] in ("invalid_spec", "sheet_tree_incomplete", "parse_error"), out


# --------------------------------------------------------------------------- self-checks


def test_a_corrupted_refresh_is_caught_by_the_pin_check(sheet, monkeypatch):
    """The copy the library read hands over is not what the library file holds: the raw pin comparison fires."""
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    real = lib_refresh._load

    def corrupt(path, lib_id, explicit):
        entry, why = real(path, lib_id, explicit)
        if entry is not None:
            pin = next(p for sub in _kids(entry, "symbol") for p in _kids(sub, "pin") if _kids(p, "number")[0].get_string(0) == "2")
            write_layer.set_number(pin.find_children("at")[0], 1, -2.54)
        return entry, why

    monkeypatch.setattr(lib_refresh, "_load", corrupt)
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "write_not_verified" and out["check"] == "embedded_pins_equal_library", out
    monkeypatch.undo()
    assert _bytes(sheet) == before


def test_a_corrupted_write_is_refused_and_leaves_the_sheet_alone(sheet, monkeypatch):
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "write_not_verified", out
    monkeypatch.undo()
    assert _bytes(sheet) == before
    assert not [p for p in sheet.parent.iterdir() if p.name.endswith(".tmp")]


def _outcome(**kw) -> sheet_editing.Outcome:
    return sheet_editing.Outcome(**kw)


def test_the_staged_check_allows_only_the_entry_named_in_lib_changed(sheet):
    original, new = _root(sheet), _root(sheet)
    table = _kids(new, "lib_symbols")[0]
    for name in ("parts:Part", "parts:Other"):
        entry = next(e for e in _kids(table, "symbol") if e.get_string(0) == name)
        entry.children.append(SExp.list("extra_marker"))
    sheet_editing._staged_check(original, new, _outcome(lib_changed={"parts:Part", "parts:Other"}))
    with pytest.raises(sheet_authoring.SheetAuthoringError) as err:
        sheet_editing._staged_check(original, new, _outcome(lib_changed={"parts:Part"}))
    assert err.value.code == "write_not_verified" and "parts:Other" in str(err.value)
    with pytest.raises(sheet_authoring.SheetAuthoringError):
        sheet_editing._staged_check(original, new, _outcome())


def test_the_refresh_touching_another_node_is_caught_by_the_staged_check(sheet, monkeypatch):
    """The call stays inside the lib_symbols table: a stray change to a placed symbol refuses it."""
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    real_check = sheet_editing._staged_check

    def stray(original, new, outcome):
        write_layer.set_string(next(s for s in _kids(new, "symbol") if sheet_editing._ref(s) == "R1").find_children("property")[1], 1, "STRAY")
        return real_check(original, new, outcome)

    monkeypatch.setattr(sheet_editing, "_staged_check", stray)
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "write_not_verified", out
    monkeypatch.undo()
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- tool text


def test_the_tool_text_names_the_refusals_the_override_and_dry_run():
    doc = _tools()["refresh_lib_symbols"].__doc__
    for phrase in ("pins_removed", "unit_missing", "connectivity_changed", "erc_changed", "lib_symbol_mismatch",
                   "allow_connectivity_change", "dry_run", "would_refresh", "file_open_in_kicad", "lib_name_symbols_left", "one sheet file per"):
        assert phrase in doc, phrase


def test_a_new_pin_written_with_the_override_loads_and_shows_as_not_connected(sheet):
    """The placed symbols keep their old pin list (instance data is not touched); KiCad still reads the new pin from the copy."""
    _set_library(_project(sheet), _part(P1, P2, _pin("3", "C", 5.08, 0, 180)), OTHER, DUAL)

    def census() -> Counter:
        report = kicad_cli.run_erc(sheet)
        return Counter(v["type"] for s in report.get("sheets", []) for v in s.get("violations", []))

    before = census()
    out = _refresh(sheet, allow_connectivity_change=True)
    assert out["ok"] is True and out["written"] is True, out
    after = census()
    assert after["lib_symbol_mismatch"] == 0 and before["lib_symbol_mismatch"] == 2
    assert after["pin_not_connected"] == before["pin_not_connected"] + 2
    assert _pins(sheet, "U1")["3"] == (55.88, 50.8)
    assert [p.get_string(0) for p in _kids(_symbol_node(sheet, "U1"), "pin")] == ["1", "2"]


def test_the_netlist_changes_name_the_moved_pin(sheet, monkeypatch):
    monkeypatch.setattr(lib_refresh, "_erc", lambda *a: (Counter(), [], set()))
    _set_library(_project(sheet), _part(_pin("1", "A", 0, 5.08, 270), P2), OTHER, DUAL)
    out = _refresh(sheet, allow_connectivity_change=True, dry_run=True)
    items = out["connectivity"]["changes"]["items"]
    assert [i for i in items if i.get("type") == "pin_moved"] == [
        {"type": "pin_moved", "reference": r, "pin": "1", "was": was, "now": now, "attached": a, "note": "the wires stay where they are; the new pin is not on them"}
        for r, was, now, a in (("U1", [50.8, 46.99], [50.8, 45.72], items[0]["attached"]), ("U2", [76.2, 46.99], [76.2, 45.72], items[1]["attached"]))]


def test_the_erc_report_lists_what_dropped(sheet):
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    out = _refresh(sheet, dry_run=True)
    assert out["erc"]["decreased"] == {"lib_symbol_mismatch/warning": 2} and out["erc"]["total_before"] == out["erc"]["total_after"] + 2


def test_the_same_symbol_embedded_twice_is_refused(sheet):
    root = _root(sheet)
    table = _kids(root, "lib_symbols")[0]
    table.children.append(parse_string(_text(_embedded(sheet, "parts:Other"))))
    write_layer.save(root, sheet)
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "invalid_spec" and "twice" in out["reason"], out
    assert _bytes(sheet) == before


@pytest.mark.parametrize("nodes", [[], ["two"]])
def test_a_library_that_hands_over_no_symbol_or_two_is_refused_or_skipped(sheet, monkeypatch, nodes):
    real = sheet_editing._load_library_entry
    monkeypatch.setattr(sheet_editing, "_load_library_entry", lambda path, lib_id: [] if not nodes else [real(path, lib_id)[0], real(path, "parts:Other")[0]])
    before = _bytes(sheet)
    out = _refresh(sheet, lib_ids=["parts:Part"])
    assert out["ok"] is False and out["code"] == "lib_unresolved", out
    out = _refresh(sheet)
    assert out["ok"] is True and out["written"] is False and len(out["skipped"]) == 4, out
    assert _bytes(sheet) == before


def test_a_symbol_that_extends_another_is_embedded_flattened_and_refreshed(sheet):
    child = ('(symbol "Child" (extends "Part") (property "Reference" "U" (at 0 0 0) (effects (font (size 1.27 1.27)))) '
             '(property "Value" "Child" (at 0 0 0) (effects (font (size 1.27 1.27)))) '
             '(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide)) '
             '(property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide)))')
    _set_library(_project(sheet), _part(P1, P2), OTHER, DUAL, child)
    assert _tools()["add_symbols"](str(sheet), [{"lib_id": "parts:Child", "x": 50.8, "y": 90.0, "ref": "C1"}])["status"] == "ok"
    assert [e for e in _kids(_embedded(sheet, "parts:Child"), "extends")] == []
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL, child)
    out = _refresh(sheet, lib_ids=["parts:Child"])
    assert out["ok"] is True and out["written"] is True, out
    assert _pins(sheet, "C1")["2"] == (50.8, 95.08)
    assert [e for e in _kids(_embedded(sheet, "parts:Child"), "extends")] == []


# --------------------------------------------------------------------------- review round: KiCad-authored sheets, fidelity, unit 0


def test_a_kicad_authored_demo_with_an_untouched_library_has_nothing_to_refresh(kicad_demo):
    """The embedded copies of a sheet KiCad wrote differ from kicad_tools' loader output (attributes dropped, empty pin
    names turned into ~), yet KiCad sees them as equal to the library: nothing is stale, nothing is written."""
    sch = kicad_demo("pic_programmer") / "pic_programmer.kicad_sch"
    before = _bytes(sch)
    for dry in (True, False):
        out = _refresh(sch, dry_run=dry)
        assert out["ok"] is True and out["count"] == 0 and out["written"] is False, out
        assert out["unchanged"] and not out.get("would_refresh") and not out.get("refreshed"), out
        assert out["erc_mismatch_read"] is True
        assert _bytes(sch) == before
    named = _refresh(sch, lib_ids=["pic_programmer:C", "pic_programmer:D"])
    assert named["ok"] is True and named["count"] == 0 and named["unchanged"] == ["pic_programmer:C", "pic_programmer:D"], named
    assert _bytes(sch) == before


def _zero_symbol(y: float) -> str:
    """Both pins live in Zero_0_1 (no unit of its own), as in KiCad's pic_programmer:JUMPER."""
    return ('(symbol "Zero" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes) '
            '(property "Reference" "J" (at 0 0 0) (effects (font (size 1.27 1.27)))) (property "Value" "Zero" (at 0 0 0) (effects (font (size 1.27 1.27)))) '
            '(property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide)) (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide)) '
            f'(symbol "Zero_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254) (type default)) (fill (type none))) {P1} '
            f'{_pin("2", "B", 0, y, 90)}))')


def test_a_symbol_whose_pins_all_sit_in_unit_zero_is_not_refused_as_unit_missing(sheet):
    _set_library(_project(sheet), _part(P1, P2), OTHER, DUAL, _zero_symbol(-3.81))
    assert _tools()["add_symbols"](str(sheet), [{"lib_id": "parts:Zero", "x": 50.8, "y": 90.0, "ref": "J1"}])["status"] == "ok"
    _set_library(_project(sheet), _part(P1, P2), OTHER, DUAL, _zero_symbol(-5.08))
    out = _refresh(sheet, lib_ids=["parts:Zero"])
    assert out["ok"] is True and out["written"] is True, out
    assert _pins(sheet, "J1")["2"] == (50.8, 95.08)


def test_replace_symbols_takes_a_target_whose_pins_all_sit_in_unit_zero(sheet):
    _set_library(_project(sheet), _part(P1, P2), OTHER, DUAL, _zero_symbol(-3.81))
    out = _tools()["replace_symbols"](str(sheet), [{"reference": "W1", "lib_id": "parts:Zero"}])
    assert out["ok"] is True and out["count"] == 1, out


def test_the_embedded_copy_equals_the_library_node_except_for_its_name(sheet):
    _set_library(_project(sheet), _part(P1, _pin("2", "", 0, -5.08, 90)), OTHER, DUAL)
    assert _refresh(sheet)["ok"] is True
    assert _text(_embedded(sheet, "parts:Part")).replace('"parts:Part"', '"Part"', 1) == _text(_library_node(sheet, "Part"))


def test_a_change_with_the_same_pins_is_found_through_kicad_clis_own_mismatch_finding(sheet):
    """A bigger body, same pins: only ERC's lib_symbol_mismatch says the copy is stale."""
    bigger = _part(P1, P2).replace("(start -1.016 -2.54) (end 1.016 2.54)", "(start -2.032 -2.54) (end 2.032 2.54)")
    _set_library(_project(sheet), bigger, OTHER, DUAL)
    out = _refresh(sheet)
    assert out["ok"] is True and out["written"] is True and out["count"] == 1, out
    assert out["refreshed"][0]["found_by"] == ["erc_lib_symbol_mismatch"]
    assert _text(_embedded(sheet, "parts:Part")).replace('"parts:Part"', '"Part"', 1) == _text(_library_node(sheet, "Part"))
    assert _mismatches(sheet) == 0


def test_the_pins_decide_when_kicad_cli_names_nothing(sheet, monkeypatch):
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    real = lib_refresh._erc
    monkeypatch.setattr(lib_refresh, "_erc", lambda *a: (lambda c, p, s: (c, p, set()))(*real(*a)))
    out = _refresh(sheet, dry_run=True)
    assert out["ok"] is True and out["would_refresh"][0]["found_by"] == ["pins"], out


def _with_lib_name_variant(sheet: Path, body_from: str, body_to: str) -> None:
    """U1 uses a lib_name variant "Part_1" of parts:Part whose body differs from the library's (written through the write layer)."""
    root = _root(sheet)
    text = _text(_embedded(sheet, "parts:Part")).replace('"parts:Part"', '"Part_1"', 1).replace(body_from, body_to)
    _kids(root, "lib_symbols")[0].children.append(parse_string(text.replace('"Part_1_1"', '"Part_1_1_1"').replace('"Part_0_1"', '"Part_1_0_1"')))
    u1 = next(s for s in _kids(root, "symbol") if sheet_editing._ref(s) == "U1")
    lib_id = u1.find_children("lib_id")[0]
    u1.children.insert(u1.children.index(lib_id) + 1, SExp.list("lib_name", write_layer.string("Part_1")))
    write_layer.save(root, sheet)


def test_a_stale_lib_name_variant_does_not_mark_its_base_library_symbol_stale(sheet):
    """ERC names only U1 (its variant differs from the library); parts:Part, used by U2 and equal to the library, stays unchanged."""
    _with_lib_name_variant(sheet, "(start -1.016 -2.54) (end 1.016 2.54)", "(start -2.032 -2.54) (end 2.032 2.54)")
    assert _mismatches(sheet) >= 1
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is True and out["written"] is False and out["count"] == 0, out
    assert "parts:Part" in out["unchanged"] and not out["refreshed"]
    assert [u["reference"] for u in out["lib_name_symbols_left"]] == ["U1"]
    assert _bytes(sheet) == before


def test_a_kicad_cli_that_fails_ends_in_kicad_cli_failed_and_writes_nothing(sheet, monkeypatch):
    """A real kicad-cli failure (the ERC call is made with a flag it rejects): a refresh that needs the ERC comparison is refused."""
    _set_library(_project(sheet), _part(P1, _pin("2", "B", 0, -5.08, 90)), OTHER, DUAL)
    real = kicad_cli._run

    def failing(args, timeout=None):
        if args[:2] == ["sch", "erc"]:
            args = [*args[:2], "--no-such-flag", *args[2:]]
        return real(args) if timeout is None else real(args, timeout)

    monkeypatch.setattr(kicad_cli, "_run", failing)
    before = _bytes(sheet)
    out = _refresh(sheet)
    assert out["ok"] is False and out["code"] == "kicad_cli_failed" and out["written"] is False, out
    monkeypatch.undo()
    assert _bytes(sheet) == before


def test_a_kicad_cli_that_fails_leaves_a_sheet_with_nothing_to_refresh_unchanged_and_says_it_did_not_read_ercs_mismatches(sheet, monkeypatch):
    real = kicad_cli._run

    def failing(args, timeout=None):
        if args[:2] == ["sch", "erc"]:
            args = [*args[:2], "--no-such-flag", *args[2:]]
        return real(args) if timeout is None else real(args, timeout)

    monkeypatch.setattr(kicad_cli, "_run", failing)
    out = _refresh(sheet)
    assert out["ok"] is True and out["written"] is False and out["count"] == 0 and out["erc_mismatch_read"] is False, out
    assert "parts:Part" in out["unchanged"]


@pytest.mark.parametrize("renamed", ['"Part_A"', '"Part_1"', '"Other_1_1"'])
def test_a_library_block_not_named_name_unit_style_is_refused_with_the_expected_form(sheet, renamed):
    """Issue 192: KiCad 10 loads a library symbol only when its blocks are named <name>_<unit>_<style>. The library file is
    written without the write layer's load check (kicad_tools' serialiser) because the write layer, like KiCad, refuses it."""
    from tests._kicad_faults import write_unchecked

    bad = _part(P1, P2).replace('"Part_1_1"', renamed)
    write_unchecked(parse_string(_lib_text(bad, OTHER)), _project(sheet) / "lib" / "parts.kicad_sym")
    before = _bytes(sheet)
    out = _refresh(sheet, lib_ids=["parts:Part"])
    assert out["ok"] is False and out["code"] == "unit_block_name_invalid", out
    assert "Part_<unit>_<style>" in out["reason"] and renamed.strip(chr(34)) in out["reason"], out
    assert _bytes(sheet) == before
