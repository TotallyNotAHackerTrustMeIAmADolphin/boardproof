"""set_properties (GitHub issue 120), with real kicad-cli.

A downstream project places symbols with add_symbols and then needs the sourcing fields on them
(LCSC, Datasheet, Description, Value, Footprint) without rebuilding the sheet. These tests build a
small sheet through the tools, set, add and remove instance properties in one call, and read the
result back from the raw file, from kicad-cli's netlist and from its ERC.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_authoring, sheet_editing, sheet_tree, write_layer
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def _tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def tools() -> dict:
    return _tools()


@pytest.fixture
def sheet(tmp_path, tools) -> Path:
    """R1 (50.8, 50.8) with a Footprint, R2 (76.2, 50.8), C1 (101.6, 50.8); R1.1 and R2.1 share a net label."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k", "footprint": "Resistor_SMD:R_0402_1005Metric"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"},
        {"lib_id": "Device:C", "x": 101.6, "y": 50.8, "ref": "C1", "value": "1u"},
    ])["status"] == "ok"
    assert tools["add_wires"](str(sch), [
        {"p1": [50.8, 46.99], "p2": [50.8, 40.64], "route": "direct", "snap": False},
        {"p1": [50.8, 40.64], "p2": [76.2, 40.64], "route": "direct", "snap": False},
        {"p1": [76.2, 40.64], "p2": [76.2, 46.99], "route": "direct", "snap": False},
    ])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 63.5, "y": 40.64, "text": "NETA", "snap": False}])["status"] == "ok"
    return sch


def _root(sch: Path):
    return write_layer.load(sch)


def _symbol(sch: Path, ref: str):
    return next(s for s in _root(sch).children if not s.is_atom and s.name == "symbol" and sheet_editing._ref(s) == ref)


def _props(sch: Path, ref: str) -> dict[str, str]:
    return {p.get_string(0): write_layer.serialise(p, compact=True) for p in sheet_editing._kids(_symbol(sch, ref), "property")}


def _names(sch: Path, ref: str) -> list[str]:
    return [p.get_string(0) for p in sheet_editing._kids(_symbol(sch, ref), "property")]


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _nets(sch: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    return sheet_editing.read_nets(tree, sch, None)


def _erc(sch: Path, tmp_path: Path) -> dict:
    copy = tmp_path / "erc"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(sch.parent, copy)
    return dict(sheet_authoring.erc_census(copy / sch.name))


def _is_hidden(sch: Path, ref: str, name: str) -> bool:
    prop = sheet_editing._property(_symbol(sch, ref), name)
    return bool(sheet_editing._kids(prop.find_children("effects")[0], "hide"))


# --------------------------------------------------------------------------- the acceptance test


def test_lcsc_on_three_instances_in_one_call(sheet, tools, tmp_path):
    nets, erc = _nets(sheet), _erc(sheet, tmp_path)
    result = tools["set_properties"](str(sheet), [
        {"reference": "R1", "set": {"LCSC": "C25744"}},
        {"reference": "R2", "set": {"LCSC": "C21190"}},
        {"reference": "C1", "set": {"LCSC": "C14663"}},
    ])
    assert result["ok"] is True and result["written"] is True and result["count"] == 3, result
    for ref, lcsc in (("R1", "C25744"), ("R2", "C21190"), ("C1", "C14663")):
        assert f'(property "LCSC" "{lcsc}"' in _props(sheet, ref)["LCSC"]
        assert _is_hidden(sheet, ref, "LCSC")
        # a new field sits at the symbol's own (at) and after the last existing property
        prop, node = sheet_editing._property(_symbol(sheet, ref), "LCSC"), _symbol(sheet, ref)
        assert sheet_editing._at(prop)[:2] == sheet_editing._at(node)[:2]
        assert _names(sheet, ref)[-1] == "LCSC"
    assert result["connectivity"]["changed"] is False
    assert _nets(sheet) == nets
    assert _erc(sheet, tmp_path) == erc


# --------------------------------------------------------------------------- set, add, remove


def test_changing_a_value_keeps_position_effects_and_order(sheet, tools):
    before = _props(sheet, "R1")
    order = _names(sheet, "R1")
    result = tools["set_properties"](str(sheet), [{"reference": "R1", "set": {"Value": "22k", "Footprint": "Resistor_SMD:R_0603_1608Metric"}}])
    assert result["ok"] is True, result
    after = _props(sheet, "R1")
    assert _names(sheet, "R1") == order
    assert after["Value"] == before["Value"].replace('"10k"', '"22k"')
    assert after["Footprint"] == before["Footprint"].replace("R_0402_1005Metric", "R_0603_1608Metric")
    assert after["Reference"] == before["Reference"] and after["Datasheet"] == before["Datasheet"]
    assert [p["action"] for p in result["updated"][0]["properties"]] == ["changed", "changed"]


def test_a_value_set_to_what_it_already_is_writes_nothing(sheet, tools):
    before = _bytes(sheet)
    result = tools["set_properties"](str(sheet), [{"reference": "R1", "set": {"Value": "10k"}}])
    assert result["ok"] is True and result["written"] is False and result["updated"][0]["properties"][0]["action"] == "unchanged"
    assert _bytes(sheet) == before


def test_the_hidden_flag_for_new_and_existing_properties(sheet, tools):
    r = tools["set_properties"](str(sheet), [{"reference": "R1", "set": {
        "Description": {"value": "pull-up", "hidden": False}, "MPN": {"value": "RC0402"}, "Value": {"value": "10k", "hidden": True}}}])
    assert r["ok"] is True, r
    assert not _is_hidden(sheet, "R1", "Description")  # a new field can be asked to show
    assert _is_hidden(sheet, "R1", "MPN")  # default for a new custom field
    assert _is_hidden(sheet, "R1", "Value")  # an existing one can be hidden
    again = tools["set_properties"](str(sheet), [{"reference": "R1", "set": {"Value": "47k"}}])
    assert again["ok"] is True and _is_hidden(sheet, "R1", "Value")  # no hidden given: the flag stays
    shown = tools["set_properties"](str(sheet), [{"reference": "R1", "set": {"Value": {"value": "47k", "hidden": False}}}])
    assert shown["ok"] is True and not _is_hidden(sheet, "R1", "Value")


def test_remove_and_set_in_one_item_and_selection_by_uuid(sheet, tools):
    uid = sheet_editing._uuid(_symbol(sheet, "R2"))
    assert tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C1", "Note": "x"}}])["ok"] is True
    result = tools["set_properties"](str(sheet), [{"uuid": uid, "set": {"Note": "y"}, "remove": ["LCSC"]}])
    assert result["ok"] is True and result["written"] is True, result
    assert "LCSC" not in _names(sheet, "R2") and 'Note" "y"' in _props(sheet, "R2")["Note"]
    assert {p["name"]: p["action"] for p in result["updated"][0]["properties"]} == {"Note": "changed", "LCSC": "removed"}


def test_other_nodes_and_instance_data_are_untouched(sheet, tools):
    others = {u: write_layer.serialise(c, compact=True) for c in _root(sheet).children if not c.is_atom
              for u in [sheet_editing._uuid(c)] if u and sheet_editing._ref(c) != "R1" or c.name != "symbol" and u}
    r1_instances = write_layer.serialise(sheet_editing._kids(_symbol(sheet, "R1"), "instances")[0], compact=True)
    assert tools["set_properties"](str(sheet), [{"reference": "R1", "set": {"LCSC": "C1"}, "remove": ["Datasheet"]}])["ok"] is True
    after = {u: write_layer.serialise(c, compact=True) for c in _root(sheet).children if not c.is_atom
             for u in [sheet_editing._uuid(c)] if u and sheet_editing._ref(c) != "R1" or c.name != "symbol" and u}
    assert after == others
    assert write_layer.serialise(sheet_editing._kids(_symbol(sheet, "R1"), "instances")[0], compact=True) == r1_instances


def test_dry_run_writes_nothing_and_says_what_would_happen(sheet, tools):
    before = _bytes(sheet)
    result = tools["set_properties"](str(sheet), [{"reference": "R1", "set": {"LCSC": "C1", "Value": "5k"}, "remove": ["Datasheet"]}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and "updated" not in result, result
    assert [p["action"] for p in result["would_update"][0]["properties"]] == ["added", "changed", "removed"]
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- refusals write nothing


@pytest.mark.parametrize("items, code", [
    ([{"reference": "R1", "set": {"Reference": "R9"}}], "reference_not_settable"),
    ([{"reference": "R1", "remove": ["Reference"]}], "property_not_removable"),
    ([{"reference": "R1", "remove": ["uuid"]}], "property_not_removable"),
    ([{"reference": "R1", "remove": ["Value"]}], "property_not_removable"),
    ([{"reference": "R1", "set": {" LCSC": "1"}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC ": "1"}}], "invalid_spec"),
    ([{"reference": "R1", "remove": ["Datasheet "]}], "invalid_spec"),
    ([{"reference": "R1", "remove": ["NoSuchField"]}], "property_not_found"),
    ([{"reference": "ZZ9", "set": {"LCSC": "1"}}], "not_found"),
    ([{"uuid": "00000000-0000-0000-0000-000000000000", "set": {"LCSC": "1"}}], "not_found"),
    ([{"reference": "R1", "set": {"LCSC": "1"}}, {"reference": "ZZ9", "set": {"LCSC": "1"}}], "not_found"),
    ([], "invalid_spec"),
    ([{"set": {"LCSC": "1"}}], "invalid_spec"),
    ([{"reference": "R1", "uuid": "x", "set": {"LCSC": "1"}}], "invalid_spec"),
    ([{"reference": "R1"}], "invalid_spec"),
    ([{"reference": "R1", "set": {}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": 5}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": True}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": None}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": {"hidden": True}}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": {"value": "1", "hidden": "yes"}}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": {"value": "1", "colour": "red"}}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"": "1"}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"A\nB": "1"}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": "a\x07b"}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"uuid": "1"}}], "invalid_spec"),
    ([{"reference": "R1", "remove": "LCSC"}], "invalid_spec"),
    ([{"reference": "R1", "remove": []}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": "1"}, "remove": ["LCSC"]}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": "1"}}, {"reference": "R1", "set": {"MPN": "1"}}], "invalid_spec"),
    ([{"reference": "R1", "set": {"LCSC": "1"}, "colour": "red"}], "invalid_spec"),
    (["R1"], "invalid_spec"),
])
def test_a_refusal_has_its_code_and_writes_nothing(sheet, tools, items, code):
    before = _bytes(sheet)
    result = tools["set_properties"](str(sheet), items)
    assert result["ok"] is False and result["code"] == code, result
    assert _bytes(sheet) == before


def test_a_reference_shared_by_several_nodes_is_ambiguous_and_the_uuid_picks_one(sheet, tools):
    root = write_layer.load(sheet)
    twin = write_layer.load(sheet)
    original = next(c for c in root.children if not c.is_atom and c.name == "symbol" and sheet_editing._ref(c) == "C1")
    copy = next(c for c in twin.children if not c.is_atom and c.name == "symbol" and sheet_editing._ref(c) == "C1")
    copy.find_children("uuid")[0].children[0] = write_layer.string("11111111-1111-1111-1111-111111111111")
    copy.find_children("at")[0].children[0] = write_layer.number(130.0)
    root.children.insert(root.children.index(original) + 1, copy)
    write_layer.save(root, sheet)
    before = _bytes(sheet)
    refused = tools["set_properties"](str(sheet), [{"reference": "C1", "set": {"LCSC": "1"}}])
    assert refused["ok"] is False and refused["code"] == "ambiguous_reference", refused
    assert "11111111-1111-1111-1111-111111111111" in str(refused) and _bytes(sheet) == before
    picked = tools["set_properties"](str(sheet), [{"uuid": "11111111-1111-1111-1111-111111111111", "set": {"LCSC": "1"}}], allow_connectivity_change=True)
    assert picked["ok"] is True and picked["count"] == 1, picked
    nodes = [c for c in _root(sheet).children if not c.is_atom and c.name == "symbol" and sheet_editing._ref(c) == "C1"]
    assert sorted(sheet_editing._property(n, "LCSC") is not None for n in nodes) == [False, True]


def test_a_power_symbols_value_names_its_net_so_changing_it_is_a_connectivity_change(sheet, tools, tmp_path):
    assert tools["add_symbols"](str(sheet), [{"lib_id": "power:GND", "x": 50.8, "y": 54.61, "ref": "#PWR01", "value": "GND"}])["status"] == "ok"
    before = _bytes(sheet)
    refused = tools["set_properties"](str(sheet), [{"reference": "#PWR01", "set": {"Value": "AGND"}}])
    assert refused["ok"] is False and refused["code"] == "connectivity_changed", refused
    assert refused["connectivity"]["changed"] is True and _bytes(sheet) == before
    allowed = tools["set_properties"](str(sheet), [{"reference": "#PWR01", "set": {"Value": "AGND"}}], allow_connectivity_change=True)
    assert allowed["ok"] is True and allowed["written"] is True and allowed["connectivity"]["changed"] is True, allowed
    assert any("AGND" in name for name in _nets(sheet))


# --------------------------------------------------------------------------- the guards fire


def test_set_properties_corrupted_write_is_refused(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _tools()["set_properties"](str(sheet), [{"reference": "C1", "set": {"LCSC": "C1"}}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sheet) == before


def test_the_staged_check_fires_when_a_property_edit_touches_an_unselected_symbol(sheet, monkeypatch):
    holder = {}
    real_run = sheet_editing.run

    def run(path, root_sch, mutate, **kw):
        def wrapped(sh, new):
            out = mutate(sh, new)
            c1 = next(s for s in sh.symbols if sheet_editing._ref(s) == "C1")
            write_layer.set_string(sheet_editing._property(c1, "Value"), 1, "tampered")
            holder["done"] = True
            return out

        return real_run(path, root_sch, wrapped, **kw)

    monkeypatch.setattr(sheet_editing, "run", run)
    before = _bytes(sheet)
    result = _tools()["set_properties"](str(sheet), [{"reference": "R1", "set": {"LCSC": "C1"}}])
    assert holder.get("done") and result["ok"] is False and result["code"] == "write_not_verified" and "not selected" in result["reason"], result
    assert _bytes(sheet) == before


def test_the_netlist_comparison_fires_when_a_property_edit_changes_connectivity(sheet, monkeypatch):
    """Mutation of the guard's input: a change that moves a pin off its net must be seen even though the edit is a property edit."""
    real_run = sheet_editing.run

    def run(path, root_sch, mutate, **kw):
        def wrapped(sh, new):
            out = mutate(sh, new)
            r1 = next(s for s in sh.symbols if sheet_editing._ref(s) == "R1")
            sheet_editing._set_at(r1, 90.0, 90.0)  # R1 leaves its wires
            return out

        return real_run(path, root_sch, wrapped, **kw)

    monkeypatch.setattr(sheet_editing, "run", run)
    result = _tools()["set_properties"](str(sheet), [{"reference": "R1", "set": {"LCSC": "C1"}}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", result


# --------------------------------------------------------------------------- multi-unit parts (review B1)


def _netlist_comp(sch: Path, ref: str, tmp_path: Path) -> tuple[str, dict[str, str]]:
    """kicad-cli netlist entry of ``ref``: (value, {field name: value})."""
    import re

    from kicad_tools.sexp.parser import parse_file

    out = kicad_cli.export_netlist(sch, tmp_path / "probe.net")
    parsed = parse_file(str(out))
    for comp_list in parsed.find_children("components"):
        for comp in comp_list.find_children("comp"):
            if comp.find_children("ref")[0].get_string(0) != ref:
                continue
            text = write_layer.serialise(comp, compact=True)
            value = re.search(r'\(value "([^"]*)"\)', text).group(1)
            fields = dict(re.findall(r'\(field \(name "([^"]*)"\) "([^"]*)"\)', text))
            return value, fields
    raise AssertionError(f"{ref} not in the netlist")


@pytest.fixture
def two_units(sheet, tools) -> Path:
    spec = {"lib_id": "Amplifier_Operational:LM324", "ref": "U1", "value": "LM324", "y": 90.17}
    assert tools["add_symbols"](str(sheet), [{**spec, "x": 50.8, "unit": 1}, {**spec, "x": 101.6, "unit": 2}])["status"] == "ok"
    return sheet


def _units(sch: Path, ref: str) -> list:
    nodes = [c for c in _root(sch).children if not c.is_atom and c.name == "symbol" and sheet_editing._ref(c) == ref]
    return sorted(nodes, key=lambda n: n.find_children("unit")[0].get_float(0))


def test_an_edit_through_one_unit_applies_to_every_unit_and_the_netlist_agrees(two_units, tools, tmp_path):
    first, second = _units(two_units, "U1")
    result = tools["set_properties"](str(two_units), [{"uuid": sheet_editing._uuid(second), "set": {"Value": "LM324X", "LCSC": "C7950"}}])
    assert result["ok"] is True and result["written"] is True, result
    assert {(u["uuid"], u["unit"]) for u in result["updated"]} == {(sheet_editing._uuid(first), 1), (sheet_editing._uuid(second), 2)}
    for node in _units(two_units, "U1"):
        assert sheet_editing._property(node, "Value").get_string(1) == "LM324X"
        assert sheet_editing._property(node, "LCSC").get_string(1) == "C7950"
    value, fields = _netlist_comp(two_units, "U1", tmp_path)
    assert value == "LM324X" and fields.get("LCSC") == "C7950"
    # a later edit through the other unit cannot leave the copies apart either
    again = tools["set_properties"](str(two_units), [{"uuid": sheet_editing._uuid(first), "set": {"LCSC": "C1"}}])
    assert again["ok"] is True and {u["unit"] for u in again["updated"]} == {1, 2}
    assert _netlist_comp(two_units, "U1", tmp_path)[1].get("LCSC") == "C1"


def test_a_reference_shared_by_the_units_of_one_part_edits_all_of_them(two_units, tools):
    result = tools["set_properties"](str(two_units), [{"reference": "U1", "set": {"LCSC": "C7950"}}])
    assert result["ok"] is True and result["count"] == 2, result
    assert all(sheet_editing._property(n, "LCSC") is not None for n in _units(two_units, "U1"))


def test_units_whose_copies_differ_are_brought_together_and_remove_needs_one_unit_to_have_it(two_units, tools):
    first = _units(two_units, "U1")[0]
    assert tools["set_properties"](str(two_units), [{"uuid": sheet_editing._uuid(first), "set": {"Note": "x"}}])["ok"] is True
    root = _root(two_units)
    second = next(n for n in root.children if not n.is_atom and n.name == "symbol" and sheet_editing._ref(n) == "U1"
                  and n.find_children("unit")[0].get_float(0) == 2)
    sheet_editing._remove(second, [sheet_editing._property(second, "Note")])  # a copy that has drifted apart
    write_layer.save(root, two_units)
    result = tools["set_properties"](str(two_units), [{"reference": "U1", "remove": ["Note"]}])
    assert result["ok"] is True, result
    assert sorted(r["properties"][0]["action"] for r in result["updated"]) == ["absent", "removed"]
    assert all(sheet_editing._property(n, "Note") is None for n in _units(two_units, "U1"))
    assert tools["set_properties"](str(two_units), [{"reference": "U1", "remove": ["Note"]}])["code"] == "property_not_found"


def test_two_items_for_two_units_of_one_part_are_one_symbol_selected_twice(two_units, tools):
    first, second = _units(two_units, "U1")
    result = tools["set_properties"](str(two_units), [{"uuid": sheet_editing._uuid(first), "set": {"A": "1"}},
                                                      {"uuid": sheet_editing._uuid(second), "set": {"B": "1"}}])
    assert result["ok"] is False and result["code"] == "invalid_spec", result


def test_two_separate_symbols_with_one_unit_number_stay_ambiguous_next_to_a_multi_unit_part(two_units, tools):
    """Two copies of unit 2 are not a multi-unit part: the reference alone cannot pick."""
    root = _root(two_units)
    second = _units(two_units, "U1")[1]
    copy = _root(two_units)
    twin = next(n for n in copy.children if not n.is_atom and n.name == "symbol" and sheet_editing._uuid(n) == sheet_editing._uuid(second))
    twin.find_children("uuid")[0].children[0] = write_layer.string("22222222-2222-2222-2222-222222222222")
    twin.find_children("at")[0].children[0] = write_layer.number(160.0)
    root.children.append(twin)
    write_layer.save(root, two_units)
    before = _bytes(two_units)
    result = tools["set_properties"](str(two_units), [{"reference": "U1", "set": {"LCSC": "1"}}])
    assert result["ok"] is False and result["code"] == "ambiguous_reference" and _bytes(two_units) == before, result


# --------------------------------------------------------------------------- review follow-ups


def test_root_sch_is_passed_through_to_the_netlist_comparison(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "tree"), "tree")
    root = Path(made["schematic_path"])
    assert tools["create_sheets"](str(root), [{"file": "child.kicad_sch"}])["status"] == "ok"
    child = root.parent / "child.kicad_sch"
    assert tools["add_symbols"](str(child), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
                                             {"lib_id": "power:GND", "x": 50.8, "y": 54.61, "ref": "#PWR01", "value": "GND"}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(root), [{"file": "child.kicad_sch", "name": "Child", "x": 101.6, "y": 50.8,
                                                   "pins": [{"name": "A", "direction": "input"}]}])["status"] == "ok"
    other = project_toolset._project_create(str(tmp_path / "other"), "other")
    wrong = tools["set_properties"](str(child), [{"reference": "#PWR01", "set": {"Value": "AGND"}}], root_sch=other["schematic_path"])
    assert wrong["ok"] is False and wrong["code"] == "parent_not_in_tree", wrong
    before = child.read_bytes()
    result = tools["set_properties"](str(child), [{"reference": "#PWR01", "set": {"Value": "AGND"}}], root_sch=str(root))
    assert result["ok"] is False and result["code"] == "connectivity_changed" and child.read_bytes() == before, result
    ok = tools["set_properties"](str(child), [{"reference": "R1", "set": {"LCSC": "C1"}}], root_sch=str(root))
    assert ok["ok"] is True and ok["connectivity"]["changed"] is False, ok


def test_a_name_listed_twice_in_remove_is_removed_once(sheet, tools):
    assert tools["set_properties"](str(sheet), [{"reference": "R1", "set": {"LCSC": "C1"}}])["ok"] is True
    result = tools["set_properties"](str(sheet), [{"reference": "R1", "remove": ["LCSC", "LCSC"]}])
    assert result["ok"] is True, result
    assert [(p["name"], p["action"]) for p in result["updated"][0]["properties"]] == [("LCSC", "removed")]
    assert "LCSC" not in _names(sheet, "R1")


def test_a_new_property_sits_before_the_pins_and_the_instance_data(sheet, tools):
    assert tools["set_properties"](str(sheet), [{"reference": "R1", "set": {"LCSC": "C1", "MPN": "x"}}])["ok"] is True
    kids = _symbol(sheet, "R1").children
    first_pin = min(i for i, c in enumerate(kids) if not c.is_atom and c.name in ("pin", "instances"))
    props = [i for i, c in enumerate(kids) if not c.is_atom and c.name == "property"]
    new = [i for i in props if kids[i].get_string(0) in ("LCSC", "MPN")]
    assert len(new) == 2 and max(props) < first_pin and set(new) == set(props[-2:])


def test_removing_the_footprint_is_allowed_but_the_result_says_what_it_does(sheet, tools):
    result = tools["set_properties"](str(sheet), [{"reference": "R1", "remove": ["Footprint"]}])
    assert result["ok"] is True and result["written"] is True and "Footprint" in result["note"], result
    assert "Footprint" not in _names(sheet, "R1")
    plain = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C1"}}])
    assert "Footprint" not in plain.get("note", "")


# --------------------------------------------------------------------------- delta review 2 (B1', B2, lib_name)

LM324 = {"lib_id": "Amplifier_Operational:LM324", "ref": "U1", "value": "LM324", "y": 50.8}


def _split_part(tmp_path: Path, tools: dict) -> tuple[Path, Path]:
    """LM324 U1: unit 1 on child.kicad_sch, unit 2 on the root sheet, the child attached to the root."""
    made = project_toolset._project_create(str(tmp_path / "split"), "split")
    root = Path(made["schematic_path"])
    assert tools["create_sheets"](str(root), [{"file": "child.kicad_sch"}])["status"] == "ok"
    child = root.parent / "child.kicad_sch"
    assert tools["add_symbols"](str(child), [{**LM324, "x": 50.8, "unit": 1}])["status"] == "ok"
    assert tools["add_symbols"](str(root), [{**LM324, "x": 50.8, "unit": 2}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(root), [{"file": "child.kicad_sch", "name": "Child", "x": 152.4, "y": 50.8,
                                                   "pins": [{"name": "A", "direction": "input"}]}])["status"] == "ok"
    return root, child


def test_a_part_with_units_on_two_sheets_is_refused_from_either_side(tmp_path, tools):
    root, child = _split_part(tmp_path, tools)
    for target, kw in ((child, {"root_sch": str(root)}), (root, {}), (child, {})):
        before = _bytes(root)
        result = tools["set_properties"](str(target), [{"reference": "U1", "set": {"Value": "X"}}], **kw)
        assert result["ok"] is False and result["code"] == "part_spans_sheets", (target.name, result)
        assert "child.kicad_sch" in str(result) or "split.kicad_sch" in str(result)
        assert result["other_units"] and {"sheet", "unit", "uuid"} <= set(result["other_units"][0])
        assert _bytes(root) == before
    uid = sheet_editing._uuid(_units(child, "U1")[0])
    by_uuid = tools["set_properties"](str(child), [{"uuid": uid, "set": {"Value": "X"}}], root_sch=str(root))
    assert by_uuid["code"] == "part_spans_sheets"  # a uuid does not make the other sheet's unit go away


def test_a_part_wholly_on_one_sheet_still_works_with_a_root_sheet_given(tmp_path, tools):
    root, child = _split_part(tmp_path, tools)
    assert tools["add_symbols"](str(child), [{**LM324, "ref": "U2", "x": 101.6, "unit": 1}, {**LM324, "ref": "U2", "x": 152.4, "unit": 2}])["status"] == "ok"
    result = tools["set_properties"](str(child), [{"reference": "U2", "set": {"LCSC": "C7950"}}], root_sch=str(root))
    assert result["ok"] is True and result["count"] == 2, result


def test_remove_looks_at_every_unit_not_only_the_first(two_units, tools):
    second = _units(two_units, "U1")[1]
    assert tools["set_properties"](str(two_units), [{"uuid": sheet_editing._uuid(second), "set": {"X": "1"}}])["ok"] is True
    root = _root(two_units)
    first = next(n for n in root.children if not n.is_atom and n.name == "symbol" and sheet_editing._ref(n) == "U1"
                 and n.find_children("unit")[0].get_float(0) == 1)
    sheet_editing._remove(first, [sheet_editing._property(first, "X")])  # only unit 2 has X
    write_layer.save(root, two_units)
    result = tools["set_properties"](str(two_units), [{"reference": "U1", "remove": ["X"]}])
    assert result["ok"] is True, result
    assert {u["unit"]: u["properties"][0]["action"] for u in result["updated"]} == {1: "absent", 2: "removed"}


def test_units_of_two_different_libraries_are_not_one_part(sheet, tools):
    assert tools["add_symbols"](str(sheet), [
        {**LM324, "x": 50.8, "y": 90.17, "unit": 1},
        {"lib_id": "Amplifier_Operational:LM358", "ref": "U1", "value": "LM358", "x": 101.6, "y": 90.17, "unit": 2}])["status"] == "ok"
    before = _bytes(sheet)
    result = tools["set_properties"](str(sheet), [{"reference": "U1", "set": {"LCSC": "1"}}])
    assert result["ok"] is False and result["code"] == "ambiguous_reference" and _bytes(sheet) == before, result


def test_a_unit_with_another_lib_name_is_not_treated_as_the_same_part(two_units, tools):
    """lib_name marks a local variant of the library symbol; its behaviour in the netlist was not probed, so it is not merged."""
    root = _root(two_units)
    second = next(n for n in root.children if not n.is_atom and n.name == "symbol" and sheet_editing._ref(n) == "U1"
                  and n.find_children("unit")[0].get_float(0) == 2)
    lib = second.find_children("lib_id")[0]
    second.children.insert(second.children.index(lib) + 1, write_layer_lib_name("LM324_1"))
    write_layer.save(root, two_units)
    before = _bytes(two_units)
    result = tools["set_properties"](str(two_units), [{"reference": "U1", "set": {"LCSC": "1"}}])
    assert result["ok"] is False and result["code"] == "ambiguous_reference" and _bytes(two_units) == before, result


def write_layer_lib_name(name: str):
    from kicad_tools.sexp import SExp

    return SExp.list("lib_name", write_layer.string(name))


# --------------------------------------------------------------------------- delta review 3 (B1'': the root is not always beside the sheet)


def _sub_project(tmp_path: Path, tools: dict, name: str = "deep") -> tuple[Path, Path]:
    """LM324 U1: unit 1 on the root, unit 2 on sub/child.kicad_sch (a subfolder), the child attached."""
    made = project_toolset._project_create(str(tmp_path / name), name)
    root = Path(made["schematic_path"])
    (root.parent / "sub").mkdir()
    assert tools["create_sheets"](str(root), [{"file": "sub/child.kicad_sch"}])["status"] == "ok"
    child = root.parent / "sub" / "child.kicad_sch"
    assert tools["add_symbols"](str(child), [{**LM324, "x": 50.8, "unit": 2}])["status"] == "ok"
    assert tools["add_symbols"](str(root), [{**LM324, "x": 50.8, "unit": 1}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(root), [{"file": "sub/child.kicad_sch", "name": "Child", "x": 152.4, "y": 50.8,
                                                   "pins": [{"name": "A", "direction": "input"}]}])["status"] == "ok"
    return root, child


def _all_bytes(folder: Path) -> dict:
    return {str(p.relative_to(folder)): p.read_bytes() for p in folder.rglob("*") if p.is_file()}


def test_a_child_in_a_subfolder_finds_its_root_above_and_the_part_check_applies(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    before = _all_bytes(root.parent)
    result = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "XVAL", "LCSC": "C999"}}])
    assert result["ok"] is False and result["code"] == "part_spans_sheets", result
    assert any(u["sheet"].endswith("deep.kicad_sch") for u in result["other_units"])
    assert _all_bytes(root.parent) == before
    # the same call with the root given is refused the same way, and a symbol that is alone on its sheet is edited
    assert tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}], root_sch=str(root))["code"] == "part_spans_sheets"
    assert tools["add_symbols"](str(child), [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R7", "value": "1k"}])["status"] == "ok"
    ok = tools["set_properties"](str(child), [{"reference": "R7", "set": {"LCSC": "C1"}}])
    assert ok["ok"] is True and "part_checked_standalone" not in ok, ok


def test_find_root_uses_the_upward_search_for_the_other_editing_tools_too(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    assert sheet_authoring.find_root(child, None) == root


def test_two_projects_in_a_folder_the_one_that_holds_the_sheet_is_the_root(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    other = project_toolset._project_create(str(tmp_path / "scratch"), "other")
    shutil.copy(str(Path(other["schematic_path"]).with_suffix(".kicad_pro")), root.parent / "other.kicad_pro")
    shutil.copy(other["schematic_path"], root.parent / "other.kicad_sch")
    result = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}])
    assert result["code"] == "part_spans_sheets", result


def test_two_projects_that_both_hold_the_sheet_are_refused_naming_both(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    shutil.copy(str(root.with_suffix(".kicad_pro")), root.parent / "other.kicad_pro")
    shutil.copy(root, root.parent / "other.kicad_sch")  # a second root sheet with the same sheet symbol: both trees hold the child
    before = _all_bytes(root.parent)
    result = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}])
    assert result["ok"] is False and result["code"] == "root_not_found", result
    assert "deep.kicad_sch" in str(result) and "other.kicad_sch" in str(result) and "root_sch" in result["reason"]
    assert _all_bytes(root.parent) == before


def test_a_project_that_cannot_be_read_whole_makes_the_check_refuse(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    other = project_toolset._project_create(str(tmp_path / "scratch"), "other")
    shutil.copy(str(Path(other["schematic_path"]).with_suffix(".kicad_pro")), root.parent / "other.kicad_pro")
    broken = root.parent / "other.kicad_sch"
    shutil.copy(other["schematic_path"], broken)
    assert tools["create_sheets"](str(broken), [{"file": "gone.kicad_sch"}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(broken), [{"file": "gone.kicad_sch", "name": "Gone", "x": 152.4, "y": 50.8,
                                                     "pins": [{"name": "A", "direction": "input"}]}])["status"] == "ok"
    (root.parent / "gone.kicad_sch").unlink()  # the tree of other.kicad_sch no longer reads whole
    before = _all_bytes(root.parent)
    result = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}])
    assert result["ok"] is False and result["code"] == "root_not_found" and "other.kicad_sch" in str(result), result
    assert _all_bytes(root.parent) == before


def test_a_sheet_with_no_project_anywhere_is_refused_unless_standalone_is_asked_for(tmp_path, two_units, tools):
    alone = tmp_path / "alone" / "lonely.kicad_sch"
    alone.parent.mkdir()
    shutil.copy(two_units, alone)
    before = _all_bytes(alone.parent)
    refused = tools["set_properties"](str(alone), [{"reference": "U1", "set": {"LCSC": "C1"}}])
    assert refused["ok"] is False and refused["code"] == "root_not_found", refused
    assert "standalone" in refused["reason"] and "root_sch" in refused["reason"] and "12" in refused["reason"], refused
    assert _all_bytes(alone.parent) == before
    result = tools["set_properties"](str(alone), [{"reference": "U1", "set": {"LCSC": "C1"}}], standalone=True)
    assert result["ok"] is True and result["part_checked_standalone"] is True, result
    assert "units on other sheets were not checked" in result["note"] and "part_checked_standalone" in result["note"]


def test_a_sheet_no_sheet_symbol_uses_yet_needs_the_opt_in_and_the_note_names_the_project(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "proj2"), "proj2")
    root = Path(made["schematic_path"])
    assert tools["create_sheets"](str(root), [{"file": "new.kicad_sch"}])["status"] == "ok"
    new = root.parent / "new.kicad_sch"
    assert tools["add_symbols"](str(new), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"}])["status"] == "ok"
    before = _all_bytes(root.parent)
    refused = tools["set_properties"](str(new), [{"reference": "R1", "set": {"LCSC": "C1"}}])
    assert refused["ok"] is False and refused["code"] == "root_not_found" and "proj2.kicad_sch" in refused["reason"], refused
    assert _all_bytes(root.parent) == before
    result = tools["set_properties"](str(new), [{"reference": "R1", "set": {"LCSC": "C1"}}], standalone=True)
    assert result["ok"] is True and result["part_checked_standalone"] is True and "proj2.kicad_sch" in result["note"], result


def _deep_project(tmp_path: Path, tools: dict, levels: int) -> tuple[Path, Path]:
    """LM324 U1: unit 1 on the root, unit 2 on a child ``levels`` folders below it, the child attached."""
    made = project_toolset._project_create(str(tmp_path / "far"), "far")
    root = Path(made["schematic_path"])
    rel = "/".join("abcdefghijklmnopqrstuvwxyz"[:levels]) + "/child.kicad_sch"
    (root.parent / Path(rel).parent).mkdir(parents=True)
    assert tools["create_sheets"](str(root), [{"file": rel}])["status"] == "ok"
    child = root.parent / rel
    assert tools["add_symbols"](str(child), [{**LM324, "x": 50.8, "unit": 2}])["status"] == "ok"
    assert tools["add_symbols"](str(root), [{**LM324, "x": 50.8, "unit": 1}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(root), [{"file": rel, "name": "Child", "x": 152.4, "y": 50.8,
                                                   "pins": [{"name": "A", "direction": "input"}]}])["status"] == "ok"
    return root, child


def test_a_child_more_than_twelve_levels_below_its_project_is_refused_without_the_opt_in(tmp_path, tools):
    root, child = _deep_project(tmp_path, tools, 14)
    before = _all_bytes(root.parent)
    refused = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "XVAL"}}])
    assert refused["ok"] is False and refused["code"] == "root_not_found", refused
    assert "12" in refused["reason"] and "standalone" in refused["reason"] and ".kicad_pro" in refused["reason"], refused
    assert _all_bytes(root.parent) == before
    assert tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "XVAL"}}], root_sch=str(root))["code"] == "part_spans_sheets"
    opted = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "XVAL"}}], standalone=True)
    assert opted["ok"] is True and opted["part_checked_standalone"] is True, opted
    assert "units on other sheets were not checked" in opted["note"] and "12 levels" in opted["note"], opted


def test_a_project_whose_kicad_pro_is_missing_is_not_provable_and_is_refused(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    root.with_suffix(".kicad_pro").unlink()  # KiCad still treats deep.kicad_sch as a project root
    before = _all_bytes(root.parent)
    refused = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "XVAL"}}])
    assert refused["ok"] is False and refused["code"] == "root_not_found" and ".kicad_pro" in refused["reason"], refused
    assert _all_bytes(root.parent) == before
    assert tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}], root_sch=str(root))["code"] == "part_spans_sheets"
    opted = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "XVAL"}}], standalone=True)
    assert opted["ok"] is True and opted["part_checked_standalone"] is True and "were not checked" in opted["note"], opted


def test_standalone_cannot_be_combined_with_a_root_sheet_and_must_be_a_boolean(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    before = _all_bytes(root.parent)
    both = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}], root_sch=str(root), standalone=True)
    assert both["code"] == "invalid_spec", both
    wrong = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}], standalone="yes")
    assert wrong["code"] == "invalid_spec", wrong
    assert _all_bytes(root.parent) == before


def test_the_opt_in_does_not_switch_off_the_check_when_one_project_holds_the_sheet(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    result = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}], standalone=True)
    assert result["ok"] is False and result["code"] == "part_spans_sheets", result


def test_the_project_is_looked_for_once_per_call_not_once_per_item(tmp_path, tools, monkeypatch):
    root, child = _sub_project(tmp_path, tools)
    assert tools["add_symbols"](str(child), [{"lib_id": "Device:R", "x": 101.6 + 12.7 * k, "y": 90.17, "ref": f"R{k + 1}", "value": "1k"} for k in range(6)])["status"] == "ok"
    calls, loads = [], []
    real_discover, real_load = sheet_authoring.discover_projects, write_layer.load

    def discover(path):
        calls.append(path)
        return real_discover(path)

    def load(path):
        loads.append(Path(path).name)
        return real_load(path)

    monkeypatch.setattr(sheet_authoring, "discover_projects", discover)
    monkeypatch.setattr(write_layer, "load", load)
    one = tools["set_properties"](str(child), [{"reference": "R1", "set": {"LCSC": "C0"}}])
    assert one["ok"] is True, one
    discovered, read = len(calls), loads.count("deep.kicad_sch")
    calls.clear(), loads.clear()
    items = [{"reference": f"R{k + 1}", "set": {"LCSC": f"D{k}"}} for k in range(6)]
    result = tools["set_properties"](str(child), items)
    assert result["ok"] is True and result["count"] == 6, result
    # six items cost what one item costs (the check once per call; the shared pipeline looks for the root once itself)
    assert 1 <= discovered <= 2 and len(calls) == discovered, (discovered, calls)
    assert loads.count("deep.kicad_sch") == read <= 1, (read, loads)  # the other sheet is read once, not once per item


def test_the_part_spans_sheets_reason_names_the_duplicate_reference_case(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    result = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}], root_sch=str(root))
    assert "duplicate reference" in result["reason"] and "fix_annotation" in result["reason"], result


# --------------------------------------------------------------------------- delta review 5: the netlist has to follow the edit


def _set_reference_property(sheet_file: Path, uid: str, text: str) -> None:
    """Make the Reference PROPERTY text of one symbol differ from its instance reference (through the write layer)."""
    root = write_layer.load(sheet_file)
    node = next(c for c in root.children if not c.is_atom and c.name == "symbol" and sheet_editing._uuid(c) == uid)
    write_layer.set_string(sheet_editing._property(node, "Reference"), 1, text)
    write_layer.save(root, sheet_file)


def _no_clear_check(monkeypatch):
    """Switch the clearer part_spans_sheets refusal off, so that only the netlist check stands in the way."""
    monkeypatch.setattr(sheet_editing, "_check_part_on_one_sheet", lambda *a, **k: None)


def test_a_property_text_that_differs_from_the_instance_reference_cannot_hide_the_other_unit(tmp_path, tools):
    root, child = _split_part(tmp_path, tools)
    _set_reference_property(child, sheet_editing._uuid(_units(child, "U1")[0]), "U9")
    before = _all_bytes(root.parent)
    first = tools["set_properties"](str(child), [{"reference": "U9", "set": {"Value": "STALE"}}], root_sch=str(root))
    assert first["ok"] is False and first["code"] == "part_spans_sheets", first  # the instance reference U1 is also on the root
    second = tools["set_properties"](str(root), [{"reference": "U1", "set": {"Value": "ROOTV"}}], root_sch=str(root))
    assert second["ok"] is False and second["code"] == "part_spans_sheets", second
    assert _all_bytes(root.parent) == before


def test_the_netlist_check_refuses_when_the_clear_check_is_blind(tmp_path, tools, monkeypatch):
    root, child = _split_part(tmp_path, tools)
    _set_reference_property(child, sheet_editing._uuid(_units(child, "U1")[0]), "U9")
    _no_clear_check(monkeypatch)
    before = _all_bytes(root.parent)
    stale = tools["set_properties"](str(child), [{"reference": "U9", "set": {"Value": "STALE"}}], root_sch=str(root))
    assert stale["ok"] is True, stale  # the unit of the child is the one KiCad reads: the netlist follows
    after_stale = _all_bytes(root.parent)
    refused = tools["set_properties"](str(root), [{"reference": "U1", "set": {"Value": "ROOTV"}}], root_sch=str(root))
    assert refused["ok"] is False and refused["code"] == "write_not_verified", refused
    assert refused["check"] == "netlist_follows_edit" and "U1" in refused["reason"] and "ROOTV" in refused["reason"] and "STALE" in refused["reason"], refused
    assert _all_bytes(root.parent) == after_stale and after_stale != before


def _reused_sheet(tmp_path: Path, tools: dict) -> tuple[Path, Path]:
    """child.kicad_sch holds LM324 unit 1 (property U1) and is used twice, as instance refs U1 and U2; the root holds unit 2 of U1 and of U2."""
    root, child = _split_part(tmp_path, tools)
    assert tools["add_symbols"](str(root), [{**LM324, "ref": "U2", "x": 101.6, "unit": 2}])["status"] == "ok"
    rroot = write_layer.load(root)
    sheet_node = next(c for c in rroot.children if not c.is_atom and c.name == "sheet")
    twin = write_layer.load(root)
    twin_sheet = next(c for c in twin.children if not c.is_atom and c.name == "sheet")
    new_uuid = "33333333-3333-3333-3333-333333333333"
    twin_sheet.find_children("uuid")[0].children[0] = write_layer.string(new_uuid)
    for prop in sheet_editing._kids(twin_sheet, "property"):
        if prop.get_string(0) == "Sheetname":
            write_layer.set_string(prop, 1, "Child2")
    at = twin_sheet.find_children("at")[0]
    write_layer.set_number(at, 1, at.get_float(1) + 40)
    rroot.children.insert(rroot.children.index(sheet_node) + 1, twin_sheet)
    write_layer.save(rroot, root)
    croot = write_layer.load(child)
    symbol = next(c for c in croot.children if not c.is_atom and c.name == "symbol")
    project = symbol.find_children("instances")[0].find_children("project")[0]
    first = project.find_children("path")[0]
    clone = write_layer.load(child)
    clone_symbol = next(c for c in clone.children if not c.is_atom and c.name == "symbol")
    second = clone_symbol.find_children("instances")[0].find_children("project")[0].find_children("path")[0]
    parent_path = first.get_string(0).rsplit("/", 1)[0]
    write_layer.set_string(second, 0, f"{parent_path}/{new_uuid}")
    write_layer.set_string(second.find_children("reference")[0], 0, "U2")
    project.children.append(second)
    write_layer.save(croot, child)
    return root, child


def test_a_sheet_used_twice_cannot_hide_the_other_unit_of_its_second_use(tmp_path, tools, monkeypatch):
    root, child = _reused_sheet(tmp_path, tools)
    own = [(r["ref"], r["sheet"]) for r in sheet_tree.placed_instances(sheet_tree.walk(root)) if r["sheet"].endswith("child.kicad_sch")]
    assert own == [("U1", "child.kicad_sch"), ("U2", "child.kicad_sch")], own
    before = _all_bytes(root.parent)
    refused = tools["set_properties"](str(root), [{"reference": "U2", "set": {"Value": "ROOTV"}}], root_sch=str(root))
    assert refused["ok"] is False and refused["code"] == "part_spans_sheets", refused
    assert _all_bytes(root.parent) == before
    _no_clear_check(monkeypatch)
    blind = tools["set_properties"](str(root), [{"reference": "U2", "set": {"Value": "ROOTV"}}], root_sch=str(root))
    assert blind["ok"] is False and blind["code"] == "write_not_verified" and blind["check"] == "netlist_follows_edit", blind
    assert _all_bytes(root.parent) == before


def test_the_netlist_check_passes_for_dnp_units_empty_and_multiline_and_custom_fields(sheet, tools, tmp_path):
    assert tools["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": 127.0, "y": 50.8, "ref": "R9", "value": "1k", "dnp": True}])["status"] == "ok"
    result = tools["set_properties"](str(sheet), [
        {"reference": "R9", "set": {"LCSC": "C1", "dnp": "x", "Empty": "", "Multi": "l1\nl2", "Spaced": "a  b ", "Description": "d", "Value": "2k"}},
        {"reference": "R1", "remove": ["Footprint"]}])
    assert result["ok"] is True and result["written"] is True, result
    value, fields = _netlist_comp(sheet, "R9", tmp_path)
    assert value == "2k" and fields["dnp"] == "x" and fields["Multi"].replace("\\n", "\n") == "l1\nl2" and fields.get("Empty", "") == "" and fields["Description"] == "d"
    assert _netlist_comp(sheet, "R1", tmp_path)[1].get("Footprint", "") == ""


def test_the_netlist_check_leaves_power_symbols_out_and_names_a_component_that_is_missing(sheet, tools, monkeypatch):
    assert tools["add_symbols"](str(sheet), [{"lib_id": "power:GND", "x": 50.8, "y": 54.61, "ref": "#PWR01", "value": "GND"}])["status"] == "ok"
    ok = tools["set_properties"](str(sheet), [{"reference": "#PWR01", "set": {"LCSC": "x"}}])
    assert ok["ok"] is True, ok  # power symbols are not in the netlist: nothing to compare
    real = sheet_editing._components_of
    monkeypatch.setattr(sheet_editing, "_components_of", lambda parsed: [c for c in real(parsed) if c["ref"] != "R2"])
    before = _bytes(sheet)
    refused = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C2"}}])
    assert refused["ok"] is False and refused["code"] == "write_not_verified" and "R2" in refused["reason"], refused
    assert _bytes(sheet) == before


def test_standalone_does_not_bypass_a_unique_holder_even_when_another_project_is_unreadable(tmp_path, tools):
    root, child = _sub_project(tmp_path, tools)
    other = project_toolset._project_create(str(tmp_path / "scratch"), "other")
    shutil.copy(str(Path(other["schematic_path"]).with_suffix(".kicad_pro")), root.parent / "other.kicad_pro")
    broken = root.parent / "other.kicad_sch"
    shutil.copy(other["schematic_path"], broken)
    assert tools["create_sheets"](str(broken), [{"file": "gone.kicad_sch"}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(broken), [{"file": "gone.kicad_sch", "name": "Gone", "x": 152.4, "y": 50.8,
                                                     "pins": [{"name": "A", "direction": "input"}]}])["status"] == "ok"
    (root.parent / "gone.kicad_sch").unlink()
    result = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}], standalone=True)
    assert result["ok"] is False and result["code"] == "part_spans_sheets", result
    without = tools["set_properties"](str(child), [{"reference": "U1", "set": {"Value": "X"}}])
    assert without["code"] == "root_not_found", without  # an unreadable project and no opt-in: still unprovable


def test_a_symbol_with_a_lib_name_that_has_no_library_symbol_is_not_in_the_netlist_and_is_refused(sheet, tools):
    """Probed with kicad-cli 10.0.7: such a symbol is left out of the netlist, so no edit of it can be followed."""
    from kicad_tools.sexp import SExp

    root = _root(sheet)
    node = next(n for n in root.children if not n.is_atom and n.name == "symbol" and sheet_editing._ref(n) == "R2")
    lib = node.find_children("lib_id")[0]
    node.children.insert(node.children.index(lib) + 1, SExp.list("lib_name", write_layer.string("R_variant")))
    write_layer.save(root, sheet)
    before = _bytes(sheet)
    result = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C2"}}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "R2: 0 components" in result["reason"], result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- review 6: every branch of the netlist check is pinned


def _patch_netlist(monkeypatch, change):
    """Let ``change(components)`` alter the components that the staged netlist export yields (what KiCad would have written)."""
    real = sheet_editing._components_of

    def patched(parsed):
        comps = real(parsed)
        out = change(comps)
        return comps if out is None else out

    monkeypatch.setattr(sheet_editing, "_components_of", patched)


def _refused_by_the_netlist(result, *needles):
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "netlist_follows_edit", result
    for needle in needles:
        assert needle in result["reason"], (needle, result["reason"])


def test_a_custom_field_that_the_netlist_lacks_is_refused(sheet, tools, monkeypatch):
    _patch_netlist(monkeypatch, lambda comps: [{**c, "fields": {k: v for k, v in c["fields"].items() if k != "LCSC"}} for c in comps])
    before = _bytes(sheet)
    result = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C2"}}])
    _refused_by_the_netlist(result, "R2", "LCSC", "C2", "no such field")
    assert _bytes(sheet) == before


def test_a_standard_field_that_the_netlist_carries_differently_is_refused(sheet, tools, monkeypatch):
    def change(comps):
        for c in comps:
            if c["ref"] == "R1":
                c["fields"]["Footprint"] = "Other:Elsewhere"
        return comps

    _patch_netlist(monkeypatch, change)
    before = _bytes(sheet)
    result = tools["set_properties"](str(sheet), [{"reference": "R1", "set": {"Footprint": "Resistor_SMD:R_0603_1608Metric"}}])
    _refused_by_the_netlist(result, "R1", "Footprint", "R_0603_1608Metric", "Other:Elsewhere")
    assert _bytes(sheet) == before


def test_a_value_that_the_netlist_carries_differently_is_refused(sheet, tools, monkeypatch):
    _patch_netlist(monkeypatch, lambda comps: [{**c, "value": "other"} if c["ref"] == "R2" else c for c in comps])
    result = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"Value": "4k7"}}])
    _refused_by_the_netlist(result, "R2", "Value", "4k7", "other")


def test_two_components_with_one_reference_are_refused_and_not_resolved_to_the_first(sheet, tools, monkeypatch):
    def change(comps):
        twin = next(c for c in comps if c["ref"] == "R2")
        return [*comps, {**twin, "fields": {**twin["fields"], "LCSC": "C2"}}]

    _patch_netlist(monkeypatch, change)
    before = _bytes(sheet)
    result = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C2"}}])  # the first copy lacks LCSC, the second has it
    _refused_by_the_netlist(result, "R2", "2 components")
    assert _bytes(sheet) == before


def test_a_dry_run_refuses_exactly_like_a_real_call(sheet, tools, monkeypatch):
    _patch_netlist(monkeypatch, lambda comps: [{**c, "fields": {k: v for k, v in c["fields"].items() if k != "LCSC"}} for c in comps])
    before = _bytes(sheet)
    real = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C2"}}])
    dry = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C2"}}], dry_run=True)
    _refused_by_the_netlist(real, "LCSC")
    _refused_by_the_netlist(dry, "LCSC")
    assert dry["reason"] == real["reason"] and _bytes(sheet) == before


def test_a_removed_custom_field_that_the_netlist_still_has_is_refused_even_when_empty(sheet, tools, monkeypatch):
    assert tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C2"}}])["ok"] is True
    for kept in ("C2", ""):
        def change(comps, kept=kept):
            for c in comps:
                if c["ref"] == "R2":
                    c["fields"]["LCSC"] = kept
            return comps

        with monkeypatch.context() as scoped:
            _patch_netlist(scoped, change)
            before = _bytes(sheet)
            result = tools["set_properties"](str(sheet), [{"reference": "R2", "remove": ["LCSC"]}])
            _refused_by_the_netlist(result, "R2", "LCSC", "removed", "netlist still has")
            assert _bytes(sheet) == before


def test_a_removed_standard_field_is_refused_while_the_netlist_has_a_value_and_accepted_when_empty(sheet, tools, monkeypatch):
    def change(comps):
        for c in comps:
            if c["ref"] == "R1":
                c["fields"]["Footprint"] = "Resistor_SMD:R_0402_1005Metric"
        return comps

    with monkeypatch.context() as scoped:
        _patch_netlist(scoped, change)
        refused = tools["set_properties"](str(sheet), [{"reference": "R1", "remove": ["Footprint"]}])
        _refused_by_the_netlist(refused, "R1", "Footprint", "netlist still has")
    accepted = tools["set_properties"](str(sheet), [{"reference": "R1", "remove": ["Footprint"]}])  # the real netlist keeps it as empty
    assert accepted["ok"] is True and accepted["written"] is True, accepted


def test_the_reason_names_the_cause_by_class(sheet, tools, monkeypatch):
    two = {}

    def change(comps):
        twin = next(c for c in comps if c["ref"] == "R2")
        return [*comps, dict(twin)]

    with monkeypatch.context() as scoped:
        _patch_netlist(scoped, change)
        two = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"LCSC": "C2"}}])
    assert "duplicate or unannotated reference" in two["reason"] and "copy elsewhere" not in two["reason"], two
    other = {}
    with monkeypatch.context() as scoped:
        _patch_netlist(scoped, lambda comps: [{**c, "value": "other"} if c["ref"] == "R2" else c for c in comps])
        other = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"Value": "4k7"}}])
    assert "copy elsewhere" in other["reason"], other  # the one case where nothing narrower is known
    spaced = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"Value": " 4k7 "}}])
    _refused_by_the_netlist(spaced, "white space")
    expanded = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"Value": "${REFERENCE}x"}}])
    _refused_by_the_netlist(expanded, "text variable")
    tilde = tools["set_properties"](str(sheet), [{"reference": "R2", "set": {"Datasheet": "~", "LCSC": "C2"}}])  # something else changes, so the netlist is looked at
    _refused_by_the_netlist(tilde, "Datasheet", "placeholder ~")
