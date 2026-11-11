"""replace_symbols (GitHub issue 112), with real kicad-cli.

No tool replaced a placed symbol by another lib_id (placeholder to final part): the symbol had to be
deleted and placed again, which loses its uuid, its wires and its instance data. These tests build a
small sheet through the tools, make a project library through kicad_tools' S-expression model
(``parse_string`` and ``write_layer.save`` on ``tmp_path``; no KiCad file is edited as text), replace
symbols in place and read the result back from the file and from kicad-cli's netlist.

The library pins are written here, so the expected sheet points are worked out by hand from the raw
library ``(at ...)`` (rotation 0, no mirror: sheet x = origin x + pin x, sheet y = origin y - pin y).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_authoring, sheet_editing, write_layer
from boardproof.toolsets import library as library_toolset
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


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


# Pins 1 and 2 sit where Device:R's pins sit: (0, 3.81) and (0, -3.81).
_P1, _P2 = _pin("1", "A", 0, 3.81, 270), _pin("2", "B", 0, -3.81, 90)
_LIB = "(kicad_symbol_lib (version 20231120) (generator \"test\") (generator_version \"8.0\") " + " ".join([
    _symbol("Quad", [[_P1, _P2, _pin("3", "C", 5.08, 0, 180), _pin("4", "D", -5.08, 0, 0)]]),
    _symbol("J8", [[_P1, _P2, _pin("3", "C", 5.08, 2.54, 180), _pin("4", "D", 5.08, -2.54, 180), _pin("5", "E", -5.08, 2.54, 0),
                    _pin("6", "F", -5.08, -2.54, 0), _pin("7", "G", 5.08, 5.08, 180), _pin("8", "H", 5.08, -5.08, 180)]]),
    _symbol("OnlyOne", [[_P1]]),
    _symbol("Shifted", [[_pin("1", "A", 0, 5.08, 270), _pin("2", "B", 0, -5.08, 90)]]),
    _symbol("Dual", [[_P1, _P2], [_pin("3", "C", 0, 3.81, 270), _pin("4", "D", 0, -3.81, 90)]]),
    _symbol("Bare", [[_pin("~", "X", 0, 3.81, 270), _pin("~", "Y", 0, -3.81, 90)]]),
]) + ")"


def _wire(p1, p2):
    return {"p1": list(p1), "p2": list(p2), "route": "direct", "snap": False}


@pytest.fixture
def sheet(tmp_path) -> Path:
    """R1 (50.8, 50.8), R2 (76.2, 50.8) (both Device:R) and C1 (101.6, 50.8). Pins: R1.1 (50.8, 46.99), R1.2 (50.8, 54.61),
    R2.1 (76.2, 46.99), R2.2 (76.2, 54.61). Net NETA joins R1.1 and R2.1 (three wire segments, label on the top one);
    GNDX (two labels) joins R1.2 and R2.2 through two stubs. A project library ``parts`` is registered."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    lib = tmp_path / "proj" / "lib" / "parts.kicad_sym"
    lib.parent.mkdir(parents=True, exist_ok=True)
    write_layer.save(parse_string(_LIB), lib)
    registered = _tools(library_toolset)["register_library_asset"](str(tmp_path / "proj"), symbol_lib_path=str(lib))
    assert registered["symbol"]["status"] == "ok", registered
    tools = _tools()
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"},
        {"lib_id": "Device:C", "x": 101.6, "y": 50.8, "ref": "C1", "value": "1u"},
    ])["status"] == "ok"
    assert tools["add_wires"](str(sch), [
        _wire((50.8, 46.99), (50.8, 40.64)), _wire((50.8, 40.64), (76.2, 40.64)), _wire((76.2, 40.64), (76.2, 46.99)),
        _wire((50.8, 54.61), (50.8, 59.69)), _wire((76.2, 54.61), (76.2, 59.69)),
    ])["status"] == "ok"
    assert tools["add_labels"](str(sch), [
        {"kind": "label", "x": 63.5, "y": 40.64, "text": "NETA", "snap": False},
        {"kind": "label", "x": 50.8, "y": 59.69, "text": "GNDX", "snap": False},
        {"kind": "label", "x": 76.2, "y": 59.69, "text": "GNDX", "snap": False},
    ])["status"] == "ok"
    return sch


def _root(sch: Path):
    return write_layer.load(sch)


def _nodes(sch: Path, tag: str):
    return [c for c in _root(sch).children if not c.is_atom and c.name == tag]


def _symbol_node(sch: Path, ref: str):
    return next(s for s in _nodes(sch, "symbol") if sheet_editing._ref(s) == ref)


def _lib_names(sch: Path) -> list[str]:
    table = _nodes(sch, "lib_symbols")[0]
    return [e.get_string(0) for e in table.children if not e.is_atom and e.name == "symbol"]


def _nets(sch: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    return sheet_editing.read_nets(tree, sch, None)


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _text(node) -> str:
    return write_layer.serialise(node, compact=True)


def _replace(sch: Path, replacements: list[dict], **kwargs) -> dict:
    return _tools()["replace_symbols"](str(sch), replacements, **kwargs)


def _named_nets(sch: Path) -> dict:
    return {k: v for k, v in _nets(sch).items() if k in ("/NETA", "/GNDX")}


# --------------------------------------------------------------------------- the acceptance cases


def test_the_fixture_has_the_nets_the_tests_assume(sheet):
    assert _named_nets(sheet) == {"/NETA": frozenset({("R1", "1"), ("R2", "1")}), "/GNDX": frozenset({("R1", "2"), ("R2", "2")})}


def test_a_two_pin_placeholder_becomes_a_four_pin_part_with_everything_else_unchanged(sheet):
    old = _symbol_node(sheet, "R1")
    old_uuid, old_at, old_instances = sheet_editing._uuid(old), _text(old.find_children("at")[0]), _text(old.find_children("instances")[0])
    old_props = [_text(p) for p in old.children if not p.is_atom and p.name == "property"]
    others = [_text(c) for c in _root(sheet).children if not c.is_atom and c.name not in ("symbol", "lib_symbols")]
    other_symbols = [_text(s) for s in _nodes(sheet, "symbol") if sheet_editing._ref(s) != "R1"]
    nets_before = _named_nets(sheet)

    result = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}])

    assert result["ok"] is True and result["written"] is True, result
    new = _symbol_node(sheet, "R1")
    assert new.find_children("lib_id")[0].get_string(0) == "parts:Quad"
    assert sheet_editing._uuid(new) == old_uuid and _text(new.find_children("at")[0]) == old_at
    assert _text(new.find_children("instances")[0]) == old_instances
    assert [_text(p) for p in new.children if not p.is_atom and p.name == "property"] == old_props
    assert [c.get_string(0) for c in new.children if not c.is_atom and c.name == "pin"] == ["1", "2", "3", "4"]
    assert [_text(c) for c in _root(sheet).children if not c.is_atom and c.name not in ("symbol", "lib_symbols")] == others
    assert [_text(s) for s in _nodes(sheet, "symbol") if sheet_editing._ref(s) != "R1"] == other_symbols
    assert _named_nets(sheet) == nets_before
    assert "parts:Quad" in _lib_names(sheet) and "Device:R" in _lib_names(sheet)  # R2 still uses Device:R
    entry = result["replaced"][0]
    assert entry["library_symbol"] == "added" and result["kept_library_symbols"][0]["library_symbol"] == "Device:R"
    # the new pin points, worked out by hand from the library's raw (at 0 3.81 270) etc. for a symbol at (50.8, 50.8)
    points = {p["number"]: (p["x"], p["y"]) for p in entry["pins"]}
    assert points == {"1": (50.8, 46.99), "2": (50.8, 54.61), "3": (55.88, 50.8), "4": (45.72, 50.8)}
    again = _tools()["get_symbol_pins"](str(sheet), ["R1"])["symbols"][0]
    assert {p["number"]: (p["x"], p["y"]) for p in again["pins"]} == points


def test_a_two_pin_placeholder_becomes_an_rj45_style_eight_pin_part_with_the_extra_pins_unwired(sheet):
    uuid = sheet_editing._uuid(_symbol_node(sheet, "R1"))
    result = _replace(sheet, [{"uuid": uuid, "lib_id": "parts:J8"}])
    assert result["ok"] is True and result["written"] is True, result
    assert len(result["replaced"][0]["pins"]) == 8 and result["replaced"][0]["unmapped_old_pins"] == []
    assert _named_nets(sheet) == {"/NETA": frozenset({("R1", "1"), ("R2", "1")}), "/GNDX": frozenset({("R1", "2"), ("R2", "2")})}
    nets = _nets(sheet)
    for pin in "345678":  # each extra pin sits alone on an automatic net
        alone = [nodes for nodes in nets.values() if ("R1", pin) in nodes]
        assert alone == [frozenset({("R1", pin)})], (pin, alone)


def test_the_old_library_symbol_is_dropped_only_when_no_other_symbol_uses_it(sheet):
    kept = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}], dry_run=True)
    assert kept["dropped_library_symbols"] == [] and "Device:R" in [k["library_symbol"] for k in kept["kept_library_symbols"]]
    result = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}, {"reference": "R2", "lib_id": "parts:Quad"}])
    assert result["ok"] is True and result["dropped_library_symbols"] == ["Device:R"], result
    assert "Device:R" not in _lib_names(sheet) and "Device:C" in _lib_names(sheet) and _lib_names(sheet).count("parts:Quad") == 1
    assert _named_nets(sheet)["/NETA"] == frozenset({("R1", "1"), ("R2", "1")})


def test_a_standard_library_symbol_is_read_from_kicads_folder(sheet):
    result = _replace(sheet, [{"reference": "R1", "lib_id": "Device:R_US"}])
    assert result["ok"] is True and result["replaced"][0]["library_symbol"] == "added", result
    assert "Device:R_US" in _lib_names(sheet)
    assert _named_nets(sheet)["/NETA"] == frozenset({("R1", "1"), ("R2", "1")})


def test_dry_run_writes_nothing_and_says_what_it_would_do(sheet):
    before = _bytes(sheet)
    result = _replace(sheet, [{"reference": "R1", "lib_id": "parts:J8"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and "would_replace" in result and "replaced" not in result, result
    assert len(result["would_replace"][0]["pins"]) == 8
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- connectivity


def test_a_wired_pin_with_no_counterpart_is_refused_as_pin_unmapped_and_allowed_on_request(sheet):
    before = _bytes(sheet)
    refused = _replace(sheet, [{"reference": "R1", "lib_id": "parts:OnlyOne"}])
    assert refused["ok"] is False and refused["code"] == "pin_unmapped", refused
    assert refused["unmapped"][0]["pin"] == "2" and refused["unmapped"][0]["at"] == [50.8, 54.61]
    assert _bytes(sheet) == before
    allowed = _replace(sheet, [{"reference": "R1", "lib_id": "parts:OnlyOne"}], allow_connectivity_change=True)
    assert allowed["ok"] is True and allowed["written"] is True, allowed
    assert allowed["unmapped_wired_pins"][0]["pin"] == "2" and allowed["connectivity"]["changed"] is True
    assert _symbol_node(sheet, "R1").find_children("lib_id")[0].get_string(0) == "parts:OnlyOne"


def test_a_wired_pin_whose_counterpart_lies_elsewhere_is_connectivity_changed_with_both_positions(sheet):
    before = _bytes(sheet)
    refused = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Shifted"}])
    assert refused["ok"] is False and refused["code"] == "connectivity_changed", refused
    moved = [c for c in refused["connectivity"]["changes"]["items"] if c["type"] == "pin_moved"]
    assert {(c["old_pin"], tuple(c["was"]), tuple(c["now"])) for c in moved} == {
        ("1", (50.8, 46.99), (50.8, 45.72)), ("2", (50.8, 54.61), (50.8, 55.88))}, moved
    assert _bytes(sheet) == before


def test_a_new_pin_that_lands_on_a_wire_end_is_connectivity_changed(sheet):
    assert _tools()["add_wires"](str(sheet), [_wire((55.88, 50.8), (60.96, 50.8))])["status"] == "ok"
    before = _bytes(sheet)
    refused = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}])
    assert refused["ok"] is False and refused["code"] == "connectivity_changed", refused
    landed = [c for c in refused["connectivity"]["changes"]["items"] if c["type"] == "new_pin_attached"]
    assert [(c["pin"], c["at"]) for c in landed] == [("3", [55.88, 50.8])], refused["connectivity"]
    assert _bytes(sheet) == before


def test_an_explicit_pin_map_pairs_pins_whose_numbers_differ(sheet):
    refused = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad", "pin_map": {"1": "3", "2": "4"}}])
    assert refused["code"] == "connectivity_changed"
    assert {(c["old_pin"], c["new_pin"]) for c in refused["connectivity"]["changes"]["items"] if c["type"] == "pin_moved"} == {("1", "3"), ("2", "4")}
    ok = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad", "pin_map": {"1": "1", "2": "2"}}], dry_run=True)
    assert ok["ok"] is True and [(m["old_pin"], m["new_pin"]) for m in ok["would_replace"][0]["pin_map"]] == [("1", "1"), ("2", "2")]


def test_pins_without_numbers_are_paired_by_name():
    old = [{"number": "~", "name": "X", "x": 0, "y": 0}, {"number": "~", "name": "Y", "x": 0, "y": 1}]
    new = [{"number": "~", "name": "Y", "x": 0, "y": 1}, {"number": "~", "name": "X", "x": 0, "y": 0}, {"number": "~", "name": "Z", "x": 0, "y": 2}]
    pairs, unmapped = sheet_editing._pair_pins(old, new, {}, "T")
    assert [(o["name"], n["name"]) for o, n in pairs] == [("X", "X"), ("Y", "Y")] and unmapped == []


# --------------------------------------------------------------------------- refusals write nothing


@pytest.mark.parametrize("spec, code", [
    ({"reference": "R9", "lib_id": "parts:Quad"}, "not_found"),
    ({"reference": "R1", "lib_id": "nolib:Quad"}, "lib_unresolved"),
    ({"reference": "R1", "lib_id": "parts:Missing"}, "lib_unresolved"),
    ({"reference": "R1", "lib_id": "Device:R"}, "same_lib_id"),
    ({"reference": "R1", "lib_id": "parts:Quad", "pin_map": {"7": "1"}}, "invalid_pin_map"),
    ({"reference": "R1", "lib_id": "parts:Quad", "pin_map": {"1": "9"}}, "invalid_pin_map"),
    ({"reference": "R1", "lib_id": "parts:Quad", "pin_map": {"1": "3", "2": "3"}}, "invalid_pin_map"),
    ({"reference": "R1", "uuid": "x", "lib_id": "parts:Quad"}, "invalid_spec"),
    ({"reference": "R1", "lib_id": "Quad"}, "invalid_spec"),
    ({"reference": "R1", "lib_id": "parts:Quad", "colour": "red"}, "invalid_spec"),
])
def test_a_refused_spec_writes_nothing(sheet, spec, code):
    before = _bytes(sheet)
    result = _replace(sheet, [spec])
    assert result["ok"] is False and result["code"] == code, result
    assert _bytes(sheet) == before


def test_one_bad_entry_refuses_the_whole_batch(sheet):
    before = _bytes(sheet)
    result = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}, {"reference": "R9", "lib_id": "parts:Quad"}])
    assert result["ok"] is False and result["code"] == "not_found"
    assert _bytes(sheet) == before
    assert _replace(sheet, [])["code"] == "invalid_spec"
    twice = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}, {"reference": "R1", "lib_id": "parts:J8"}])
    assert twice["code"] == "invalid_spec" and _bytes(sheet) == before


def test_a_part_with_several_units_placed_and_a_missing_unit_are_refused(sheet):
    tools = _tools()
    assert tools["add_symbols"](str(sheet), [
        {"lib_id": "parts:Dual", "x": 130, "y": 80, "ref": "U1", "unit": 1},
        {"lib_id": "parts:Dual", "x": 150, "y": 80, "ref": "U1", "unit": 2},
        {"lib_id": "parts:Dual", "x": 170, "y": 80, "ref": "U2", "unit": 2},
    ])["status"] == "ok"
    before = _bytes(sheet)
    several = _replace(sheet, [{"reference": "U1", "lib_id": "parts:Quad"}])
    assert several["code"] in ("multi_unit_symbol", "ambiguous_reference"), several
    uuid = next(sheet_editing._uuid(s) for s in _nodes(sheet, "symbol") if sheet_editing._ref(s) == "U1" and sheet_editing._unit_of(s) == 2)
    assert _replace(sheet, [{"uuid": uuid, "lib_id": "parts:Quad"}])["code"] == "multi_unit_symbol"
    assert _replace(sheet, [{"reference": "U2", "lib_id": "parts:Quad"}])["code"] == "unit_missing"
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- the self-checks fire


def test_the_reload_check_fires_on_a_corrupted_write(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sheet) == before


def test_the_symbol_check_fires_when_the_swap_changes_more_than_the_library_and_pins(sheet, monkeypatch):
    real = sheet_editing._swap_symbol_library
    before = _bytes(sheet)

    def greedy(node, lib_id, numbers, uuids):
        real(node, lib_id, numbers, uuids)
        sheet_editing._set_at(node, 1.27, 1.27)

    monkeypatch.setattr(sheet_editing, "_swap_symbol_library", greedy)
    result = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "symbol_children_unchanged", result
    assert _bytes(sheet) == before


def test_the_staged_check_fires_when_another_library_symbol_changes(sheet, monkeypatch):
    real = sheet_editing._uses
    before = _bytes(sheet)

    def greedy(sh, name):
        table = [c for c in sh.root.children if not c.is_atom and c.name == "lib_symbols"][0]
        entry = next(e for e in table.children if not e.is_atom and e.name == "symbol" and e.get_string(0) == "Device:C")
        entry.children.pop()
        return real(sh, name)

    monkeypatch.setattr(sheet_editing, "_uses", greedy)
    result = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "Device:C changed" in result["reason"], result
    assert _bytes(sheet) == before


def test_a_library_symbol_still_in_use_is_not_dropped_even_if_the_use_check_is_blind(sheet, monkeypatch):
    """Mutation guard: with the use check blinded the old entry is dropped while R2 still needs it, and the write is refused."""
    monkeypatch.setattr(sheet_editing, "_uses", lambda sh, name: [])
    before = _bytes(sheet)
    result = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}])
    assert result["ok"] is False, result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- review round 1


def _tree(*names: str):
    body = " ".join(f'(symbol "{n}" (x 1))' for n in names)
    return parse_string(f"(kicad_sch (lib_symbols {body}))")


def _staged(old, new, **kw):
    try:
        sheet_editing._staged_check(old, new, sheet_editing.Outcome(**kw))
    except sheet_authoring.SheetAuthoringError as exc:
        return exc
    return None


def test_the_library_table_checks_of_the_staged_check_each_fire():
    gone = _staged(_tree("A", "B"), _tree("A"), lib_added={"C"})
    assert gone is not None and gone.code == "write_not_verified" and "B is gone" in str(gone)
    new = _staged(_tree("A"), _tree("A", "C"), lib_removed={"Z"})
    assert new is not None and "C is new" in str(new)
    kept = _staged(_tree("A", "B"), _tree("A", "B"), lib_removed={"B"})
    assert kept is not None and "B should be dropped but is still there" in str(kept)
    missing = _staged(_tree("A"), _tree("A"), lib_added={"C"})
    assert missing is not None and "C should have been added but is missing" in str(missing)
    changed = _staged(_tree("A", "B"), parse_string('(kicad_sch (lib_symbols (symbol "A" (x 1)) (symbol "B" (x 2))))'), lib_added={"C"})
    assert changed is not None and "B changed" in str(changed)
    dup = _staged(_tree("A"), _tree("A", "A"), lib_added={"C"})
    assert dup is not None and "A changed" in str(dup)
    assert _staged(_tree("A", "B"), _tree("A", "C"), lib_added={"C"}, lib_removed={"B"}) is None


def test_without_library_expectations_the_library_table_is_compared_as_one_plain_node():
    assert _staged(_tree("A"), _tree("A")) is None
    for new in (_tree("A", "C"), _tree()):
        err = _staged(_tree("A"), new)
        assert err is not None and "a node without a uuid" in str(err)


def test_a_reference_shared_by_two_symbols_is_ambiguous_and_a_uuid_picks_one(sheet):
    assert _tools()["add_symbols"](str(sheet), [
        {"lib_id": "Device:R", "x": 130, "y": 100, "ref": "R?", "value": "1k"},
        {"lib_id": "Device:R", "x": 150, "y": 100, "ref": "R?", "value": "2k"},
    ])["status"] == "ok"
    before = _bytes(sheet)
    refused = _replace(sheet, [{"reference": "R?", "lib_id": "parts:Quad"}])
    assert refused["ok"] is False and refused["code"] == "ambiguous_reference", refused
    assert _bytes(sheet) == before
    uuids = [sheet_editing._uuid(s) for s in _nodes(sheet, "symbol") if sheet_editing._ref(s) == "R?"]
    assert len(uuids) == 2
    result = _replace(sheet, [{"uuid": uuids[1], "lib_id": "parts:Quad"}])
    assert result["ok"] is True, result
    libs = {sheet_editing._uuid(s): s.find_children("lib_id")[0].get_string(0) for s in _nodes(sheet, "symbol")}
    assert libs[uuids[0]] == "Device:R" and libs[uuids[1]] == "parts:Quad"


def _with_lib_name(sheet):
    """R1 and R2 name their embedded library symbol through (lib_name "Device:R_1"), the entry renamed to match (low-level tree, tests only)."""
    root = write_layer.load(sheet)
    table = next(c for c in root.children if not c.is_atom and c.name == "lib_symbols")
    entry = next(e for e in table.children if not e.is_atom and e.name == "symbol" and e.get_string(0) == "Device:R")
    write_layer.set_string(entry, 0, "Device:R_1")
    for sub in entry.children:  # KiCad wants the unit names to start with the entry name
        if not sub.is_atom and sub.name == "symbol":
            write_layer.set_string(sub, 0, "R_1_" + sub.get_string(0)[2:])
    for sym in root.children:
        if not sym.is_atom and sym.name == "symbol" and sheet_editing._ref(sym) in ("R1", "R2"):
            lib_id = sym.find_children("lib_id")[0]
            sym.children.insert(sym.children.index(lib_id) + 1, write_layer.SExp.list("lib_name", write_layer.string("Device:R_1")))
    write_layer.save(root, sheet)


def test_a_lib_name_is_removed_and_its_entry_kept_until_the_last_user_is_replaced(sheet):
    _with_lib_name(sheet)
    assert _named_nets(sheet)["/NETA"] == frozenset({("R1", "1"), ("R2", "1")})
    first = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}])
    assert first["ok"] is True, first
    assert not _symbol_node(sheet, "R1").find_children("lib_name") and _symbol_node(sheet, "R2").find_children("lib_name")
    assert [k["library_symbol"] for k in first["kept_library_symbols"]] == ["Device:R_1"] and first["dropped_library_symbols"] == []
    second = _replace(sheet, [{"reference": "R2", "lib_id": "parts:Quad"}])
    assert second["ok"] is True and second["dropped_library_symbols"] == ["Device:R_1"], second
    assert "Device:R_1" not in _lib_names(sheet) and not _symbol_node(sheet, "R2").find_children("lib_name")


def _pin_uuids(sheet, ref):
    return {p.get_string(0): sheet_editing._uuid(p) for p in _symbol_node(sheet, ref).children if not p.is_atom and p.name == "pin"}


def test_a_mapped_pin_keeps_its_uuid_and_an_extra_pin_gets_a_fresh_one(sheet):
    old = _pin_uuids(sheet, "R1")
    assert _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}])["ok"] is True
    new = _pin_uuids(sheet, "R1")
    assert [new["1"], new["2"]] == [old["1"], old["2"]]
    assert len({new["3"], new["4"], *old.values()}) == 4
    old2 = _pin_uuids(sheet, "R2")
    assert _replace(sheet, [{"reference": "R2", "lib_id": "parts:Quad", "pin_map": {"1": "2", "2": "1"}}])["code"] == "connectivity_changed"
    assert _pin_uuids(sheet, "R2") == old2


def test_two_symbols_replaced_in_one_call(sheet):
    result = _replace(sheet, [{"reference": "R1", "lib_id": "parts:J8"}, {"reference": "R2", "lib_id": "parts:Quad"}])
    assert result["ok"] is True and result["dropped_library_symbols"] == ["Device:R"], result
    assert sorted(n for n in _lib_names(sheet) if n.startswith("parts:")) == ["parts:J8", "parts:Quad"]
    assert _named_nets(sheet)["/NETA"] == frozenset({("R1", "1"), ("R2", "1")})


# ---- _attached: one case per kind of thing that can sit on a pin point

_OLD_PIN2 = (130.0, 103.81)  # R3 (Device:R) at (130, 100): pin 1 (130, 96.19), pin 2 (130, 103.81)
_NEW_PIN3 = (135.08, 100.0)  # parts:Quad pin 3 at library (5.08, 0)


def _place_r3(sheet):
    assert _tools()["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": 130, "y": 100, "ref": "R3", "value": "1k"}])["status"] == "ok"


def _no_connect(sheet, point):
    root = write_layer.load(sheet)
    at = write_layer.SExp.list("at", write_layer.number(point[0]), write_layer.number(point[1]))
    uid = write_layer.SExp.list("uuid", write_layer.string("0b1c2d3e-0000-4000-8000-000000000001"))
    root.children.append(write_layer.SExp.list("no_connect", at, uid))
    write_layer.save(root, sheet)


def _sheet_pin_point(sheet):
    tools = _tools()
    assert tools["create_sheets"](str(sheet), [{"file": "child.kicad_sch"}])["status"] == "ok"
    spec = {"file": "child.kicad_sch", "name": "Child", "x": 160.02, "y": 120.65, "height": 25.4, "pins": [{"name": "SIG", "direction": "input"}]}
    assert tools["add_sheet_symbols"](str(sheet), [spec])["status"] == "ok"
    sheet_node = next(c for c in _root(sheet).children if not c.is_atom and c.name == "sheet")
    at = sheet_node.find_children("pin")[0].find_children("at")[0]
    return (at.get_float(0), at.get_float(1))


def _decorate(sheet, kind, point):
    tools = _tools()
    x, y = point
    if kind == "label":
        assert tools["add_labels"](str(sheet), [{"kind": "label", "x": x, "y": y, "text": "ONPIN", "snap": False}])["status"] == "ok"
    elif kind == "junction":
        assert tools["add_junctions"](str(sheet), [[x, y]])["status"] == "ok"
    elif kind == "wire_first_end":
        assert tools["add_wires"](str(sheet), [_wire((x, y), (x + 5.08, y))])["status"] == "ok"
    elif kind == "wire_last_end":
        assert tools["add_wires"](str(sheet), [_wire((x + 5.08, y), (x, y))])["status"] == "ok"
        ends = sheet_editing._wire_ends(_nodes(sheet, "wire")[-1])
        assert sheet_editing._same(ends[-1], point) and not sheet_editing._same(ends[0], point), ends
    elif kind == "no_connect":
        _no_connect(sheet, point)
    elif kind == "other_pin":
        # a Device:R turned 90 degrees has its pins at x +- 3.81, so one of them lies on the point
        assert tools["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": x - 3.81, "y": y, "ref": "R4", "value": "1k", "rotation": 90}])["status"] == "ok"
        assert any(sheet_editing._same((p["x"], p["y"]), point) for p in sheet_editing.pin_positions(_root(sheet), _symbol_node(sheet, "R4")))
    else:
        raise AssertionError(kind)


@pytest.mark.parametrize("kind", ["label", "junction", "wire_first_end", "wire_last_end", "no_connect"])
def test_an_unmapped_old_pin_with_anything_on_it_is_pin_unmapped(sheet, kind):
    _place_r3(sheet)
    _decorate(sheet, kind, _OLD_PIN2)
    before = _bytes(sheet)
    result = _replace(sheet, [{"reference": "R3", "lib_id": "parts:OnlyOne"}])
    assert result["ok"] is False and result["code"] == "pin_unmapped", (kind, result)
    assert result["unmapped"][0]["reference"] == "R3" and result["unmapped"][0]["pin"] == "2"
    assert _bytes(sheet) == before


def test_an_unmapped_old_pin_with_another_symbols_pin_on_it_is_pin_unmapped(sheet):
    _place_r3(sheet)
    assert _tools()["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": 130, "y": 107.62, "ref": "R4", "value": "1k"}])["status"] == "ok"
    result = _replace(sheet, [{"reference": "R3", "lib_id": "parts:OnlyOne"}])
    assert result["code"] == "pin_unmapped" and "pin R4.1" in result["unmapped"][0]["attached"], result


@pytest.mark.parametrize("kind", ["label", "junction", "wire_first_end", "wire_last_end", "no_connect", "other_pin", "sheet_pin"])
def test_a_new_pin_landing_on_anything_is_new_pin_attached(sheet, kind):
    _place_r3(sheet)
    if kind == "sheet_pin":
        point = _sheet_pin_point(sheet)
        # move R3 so that its Quad pin 3 (library 5.08, 0) lies on the sheet pin
        assert _tools()["move_symbols"](str(sheet), [{"references": ["R3"], "to": [point[0] - 5.08, point[1]]}], snap=False)["ok"] is True
    else:
        _decorate(sheet, kind, _NEW_PIN3)
    before = _bytes(sheet)
    result = _replace(sheet, [{"reference": "R3", "lib_id": "parts:Quad"}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", (kind, result)
    landed = [c for c in result["connectivity"]["changes"]["items"] if c["type"] == "new_pin_attached"]
    assert [c["pin"] for c in landed] == ["3"], (kind, result["connectivity"])
    assert _bytes(sheet) == before


@pytest.mark.parametrize("renamed", ['"Odd_A"', '"Odd_1"', '"Other_1_1"'])
def test_a_library_block_not_named_name_unit_style_is_refused_with_the_expected_form(sheet, renamed):
    """Issue 192: KiCad 10 loads a library symbol only when its blocks are named <name>_<unit>_<style> (measured: kicad-cli
    refuses the library and every sheet that embeds it). The refusal names the expected form, not a generic parse failure.
    The library is written with kicad_tools' serialiser, without the write layer's load check, which refuses it too."""
    from tests._kicad_faults import write_unchecked

    bad = _symbol("Odd", [[_P1, _P2]]).replace('"Odd_1_1"', renamed)
    lib = sheet.parent / "lib" / "parts.kicad_sym"
    write_unchecked(parse_string(_LIB[:-1] + " " + bad + ")"), lib)
    before = _bytes(sheet)
    out = _replace(sheet, [{"reference": "R1", "lib_id": "parts:Odd"}])
    assert out["ok"] is False and out["code"] == "unit_block_name_invalid", out
    assert "Odd_<unit>_<style>" in out["reason"] and renamed.strip(chr(34)) in out["reason"], out
    assert _bytes(sheet) == before


def test_a_well_named_library_block_is_not_refused_by_the_name_check(sheet):
    assert _replace(sheet, [{"reference": "R1", "lib_id": "parts:Quad"}], dry_run=True)["ok"] is True
