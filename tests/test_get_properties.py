"""get_properties (GitHub issue 122): read chosen properties of selected or all placed symbols in one call.

Companion of set_properties and check_symbol_fields; it reuses the fixture of tests/test_check_symbol_fields.py
(R1 root sheet with empty Footprint and Datasheet, R10 Connectors sheet complete, R20 MCU sheet without LCSC;
R30 DNP, R31 off board, #PWR01 power, R32 "Lcsc" in the wrong case, R33 LCSC blank, R34 complete), built only
through the tools and the write layer.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from kicad_mcp import kicad_cli, write_layer
from tests import test_check_symbol_fields as _check
from tests import test_sheet_pins as _base
from tests.test_check_symbol_fields import FULL, _sym, _uuid
from tests.test_sheet_pins import _bytes


@pytest.fixture(scope="module")
def _template(tmp_path_factory) -> Path:
    """The fixture project of tests/test_check_symbol_fields.py, built once per module (its tool calls run kicad-cli); every test gets a copy."""
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    tools = _base._tools()
    built = _base.root._get_wrapped_function()(tmp_path_factory.mktemp("template"), tools)
    return _check.project._get_wrapped_function()(built, tools)


@pytest.fixture
def project(_template, tmp_path) -> Path:
    import shutil

    shutil.copytree(_template.parent, tmp_path / "proj")
    return tmp_path / "proj" / _template.name


@pytest.fixture
def tools() -> dict:
    return _base._tools()


def _rows(result: dict) -> dict[tuple[str, str], dict]:
    return {(e["sheet"], s["reference"]): s for e in result["sheets"] for s in e["symbols"]}


def _by_ref(result: dict) -> dict[str, dict]:
    return {ref: row for (_sheet, ref), row in _rows(result).items()}


def test_every_symbol_of_the_hierarchy_in_one_call_with_the_default_properties(project, tools) -> None:
    result = tools["get_properties"](str(project))
    assert result["ok"] is True and result["written"] is False, result
    assert result["properties_asked"] == ["Value", "Footprint", "Datasheet", "Description", "LCSC"]
    assert result["selected_by"] == "all" and result["all_found"] is True
    assert result["placed_symbols"] == result["returned"] == 9
    assert {e["sheet"] for e in result["sheets"]} == {project.name, "connectors.kicad_sch", "mcu.kicad_sch"}
    rows = _by_ref(result)
    assert set(rows) == {"R1", "R10", "R20", "R30", "R31", "R32", "R33", "R34", "#PWR01"}
    assert rows["R34"]["properties"] == {"Value": "1k", **FULL, "Description": rows["R34"]["properties"]["Description"]}
    assert rows["R34"]["absent"] == [] and rows["R34"]["uuid"] == _uuid(project, "R34")
    assert rows["R10"]["properties"]["LCSC"] == "C25804"
    assert result["absent_count"] == sum(len(r["absent"]) for r in rows.values())


def test_values_equal_the_raw_property_nodes(project, tools) -> None:
    """Cross-check against the raw (property ...) nodes of every sheet file, read through the write layer."""
    result = tools["get_properties"](str(project), properties=["Value", "Footprint", "Datasheet", "LCSC", "Lcsc", "Reference"])
    for entry in result["sheets"]:
        tree = write_layer.load(project.parent / entry["sheet"])
        raw = {s.find_children("uuid")[0].get_string(0): {p.get_string(0): p.get_string(1) for p in s.find_children("property")}
               for s in tree.find_children("symbol")}
        for row in entry["symbols"]:
            props = raw[row["uuid"]]
            assert row["properties"] == {k: props[k] for k in result["properties_asked"] if k in props}
            assert [a["name"] for a in row["absent"]] == [k for k in result["properties_asked"] if k not in props]


def test_missing_properties_are_listed_as_absent_and_an_empty_value_is_present(project, tools) -> None:
    rows = _by_ref(tools["get_properties"](str(project)))
    assert rows["R1"]["properties"]["Footprint"] == "" and rows["R1"]["properties"]["Datasheet"] == ""  # present, empty
    assert [a["name"] for a in rows["R1"]["absent"]] == ["LCSC"]
    assert rows["R33"]["properties"]["LCSC"] == "   "  # blank is returned as it is
    assert [a["name"] for a in rows["R20"]["absent"]] == ["LCSC"]
    assert "LCSC" not in rows["R20"]["properties"]


def test_a_wrong_case_name_is_a_case_variant_of_the_absent_one(project, tools) -> None:
    rows = _by_ref(tools["get_properties"](str(project), properties=["LCSC", "Lcsc"]))
    assert rows["R32"]["properties"] == {"Lcsc": "C1"} and rows["R32"]["absent"] == [{"name": "LCSC", "case_variant": ["Lcsc"]}]
    assert rows["R20"]["absent"] == [{"name": "LCSC"}, {"name": "Lcsc"}]
    assert rows["R34"]["absent"] == [{"name": "Lcsc", "case_variant": ["LCSC"]}]


def test_selection_by_references(project, tools) -> None:
    result = tools["get_properties"](str(project), references=["R20", "R34"], properties=["LCSC"])
    assert result["selected_by"] == "references" and result["returned"] == 2 and result["placed_symbols"] == 9
    assert _by_ref(result)["R34"]["properties"] == {"LCSC": "C25804"}
    assert result["all_found"] is True and result["not_found"] == {"references": [], "uuids": []}
    assert {e["sheet"]: len(e["symbols"]) for e in result["sheets"]} == {project.name: 1, "connectors.kicad_sch": 0, "mcu.kicad_sch": 1}


def test_selection_by_uuids_and_the_union_with_references(project, tools) -> None:
    uuid = _uuid(project, "R33")
    by_uuid = tools["get_properties"](str(project), uuids=[uuid.upper()])
    assert by_uuid["selected_by"] == "uuids" and list(_by_ref(by_uuid)) == ["R33"]
    both = tools["get_properties"](str(project), references=["R10"], uuids=[uuid])
    assert both["selected_by"] == "references and uuids" and sorted(_by_ref(both)) == ["R10", "R33"]


def test_what_matches_nothing_is_listed_as_not_found_not_dropped(project, tools) -> None:
    result = tools["get_properties"](str(project), references=["R34", "R99", "r1"], uuids=["00000000-0000-4000-8000-000000000000"])
    assert result["ok"] is True and result["returned"] == 1
    assert result["all_found"] is False
    assert result["not_found"] == {"references": ["R99", "r1"], "uuids": ["00000000-0000-4000-8000-000000000000"]}  # exact, case-sensitive


def test_dnp_off_board_and_power_symbols_are_returned_with_flags(project, tools) -> None:
    rows = _by_ref(tools["get_properties"](str(project)))
    assert rows["R30"]["flags"] == ["dnp"] and rows["R31"]["flags"] == ["off_board"] and rows["#PWR01"]["flags"] == ["power"]
    assert rows["R34"]["flags"] == []
    assert rows["#PWR01"]["properties"]["Value"] == "GND"


def test_a_symbol_without_lib_id_is_returned_with_its_flag(project, tools) -> None:
    tree, sym = _sym(project, "R34")
    sym.children.remove(sym.find_children("lib_id")[0])
    write_layer.save(tree, project)
    result = tools["get_properties"](str(project))
    row = _by_ref(result)["R34"]
    assert row["flags"] == ["no_lib_id"] and row["lib_id"] == "" and result["returned"] == 9


def test_custom_properties_including_reference_and_unknown_names(project, tools) -> None:
    rows = _by_ref(tools["get_properties"](str(project), references=["R34"], properties=["Reference", "MPN", "Value"]))
    assert rows["R34"]["properties"] == {"Reference": "R34", "Value": "1k"}
    assert rows["R34"]["absent"] == [{"name": "MPN"}]


def test_a_child_sheet_given_with_the_root_reports_the_whole_project(project, tools) -> None:
    whole = tools["get_properties"](str(project))
    assert tools["get_properties"](str(project.parent / "mcu.kicad_sch"), root_sch=str(project)) == whole


def test_the_first_property_of_a_name_decides(project, tools) -> None:
    from kicad_tools.sexp import SExp  # noqa: PLC0415 - fixture construction only

    tree, sym = _sym(project, "R34")
    twin = next(p for p in sym.find_children("property") if p.get_string(0) == "LCSC")
    sym.children.insert(sym.children.index(twin) + 1, SExp.list("property", write_layer.string("LCSC"), write_layer.string("OTHER")))
    write_layer.save(tree, project)
    assert _by_ref(tools["get_properties"](str(project), references=["R34"]))["R34"]["properties"]["LCSC"] == "C25804"


def test_a_two_unit_part_is_returned_per_placed_unit(project, tools) -> None:
    placed = tools["add_symbols"](str(project), [
        {"lib_id": "Amplifier_Operational:LM324", "x": 150, "y": 60, "ref": "U9", "value": "LM324", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM324", "x": 150, "y": 90, "ref": "U9", "value": "LM324", "unit": 2}])
    assert placed["status"] == "ok", placed
    result = tools["get_properties"](str(project), references=["U9"], properties=["Value"])
    rows = [s for e in result["sheets"] for s in e["symbols"]]
    assert sorted(r["unit"] for r in rows) == ["1", "2"] and len({r["uuid"] for r in rows}) == 2
    assert all(r["properties"] == {"Value": "LM324"} for r in rows) and result["returned"] == 2


def _use_mcu_twice(project: Path) -> None:
    """The MCU sheet used a second time; its R20 carries the reference R21 in that use (as in tests/test_check_symbol_fields.py)."""
    tree = write_layer.load(project)
    mcu = next(s for s in tree.find_children("sheet") if any(p.get_string(0) == "Sheetname" and p.get_string(1) == "MCU" for p in s.find_children("property")))
    twin = copy.deepcopy(mcu)
    write_layer.set_string(twin.find_children("uuid")[0], 0, "11111111-2222-4333-8444-555555555555")
    for prop in twin.find_children("property"):
        if prop.get_string(0) == "Sheetname":
            write_layer.set_string(prop, 1, "MCU2")
    for at in twin.find_children("at"):
        write_layer.set_number(at, 1, 160.02)
    tree.children.insert(tree.children.index(mcu) + 1, twin)
    twin_path = "/" + tree.find_children("uuid")[0].get_string(0) + "/11111111-2222-4333-8444-555555555555"
    write_layer.save(tree, project)
    child = project.parent / "mcu.kicad_sch"
    ctree, sym = _sym(child, "R20")
    project_node = sym.find_children("instances")[0].find_children("project")[0]
    second = copy.deepcopy(project_node.find_children("path")[0])
    write_layer.set_string(second, 0, twin_path)
    write_layer.set_string(second.find_children("reference")[0], 0, "R21")
    project_node.children.append(second)
    write_layer.save(ctree, child)


def test_a_sheet_file_used_twice_is_returned_once_and_selectable_by_either_use_reference(project, tools) -> None:
    _use_mcu_twice(project)
    result = tools["get_properties"](str(project))
    assert result["ok"] is True, result
    row = _rows(result)[("mcu.kicad_sch", "R20")]
    assert row["uses"] == 2 and row["use_references"] == ["R20", "R21"] and result["placed_symbols"] == result["returned"] == 9
    for ref in ("R20", "R21"):
        picked = tools["get_properties"](str(project), references=[ref])
        assert picked["all_found"] is True and picked["returned"] == 1, ref
    assert "use_references" not in _by_ref(result)["R34"]


def test_a_reference_text_that_differs_from_the_instance_reference_is_selectable_by_both(project, tools) -> None:
    tree, sym = _sym(project, "R34")
    for inst in sym.find_children("instances"):
        for path in inst.find_children("project")[0].find_children("path"):
            write_layer.set_string(path.find_children("reference")[0], 0, "R340")
    write_layer.save(tree, project)
    for ref in ("R340", "R34"):
        picked = tools["get_properties"](str(project), references=[ref])
        assert picked["returned"] == 1 and picked["all_found"] is True, ref
    row = _by_ref(tools["get_properties"](str(project), references=["R34"]))["R340"]
    assert row["reference_text"] == "R34"


@pytest.mark.parametrize("kwargs", [
    {"references": []}, {"references": "R1"}, {"references": [""]}, {"references": ["R1", "R1"]}, {"references": [" R1"]}, {"references": [1]},
    {"uuids": []}, {"uuids": ["a", "a"]}, {"properties": []}, {"properties": "LCSC"}, {"properties": ["LCSC", "LCSC"]}, {"properties": [" LCSC"]},
    {"properties": [""]}, {"properties": ["A\nB"]}, {"properties": [3]},
])
def test_invalid_selections_are_refused(project, tools, kwargs) -> None:
    result = tools["get_properties"](str(project), **kwargs)
    assert result["ok"] is False and result["code"] == "invalid_spec", result


def test_missing_file_incomplete_tree_and_unreached_sheet_are_refused(project, tools) -> None:
    assert tools["get_properties"](str(project.parent / "nope.kicad_sch"))["code"] == "file_not_found"
    stray = project.parent / "stray.kicad_sch"
    write_layer.save(write_layer.load(project.parent / "mcu.kicad_sch"), stray)
    result = tools["get_properties"](str(stray), root_sch=str(project))
    assert result["ok"] is False and result["code"] == "parent_not_in_tree", result
    stray.unlink()
    (project.parent / "mcu.kicad_sch").unlink()
    result = tools["get_properties"](str(project))
    assert result["ok"] is False and result["code"] == "sheet_tree_incomplete", result


def test_it_writes_nothing(project, tools) -> None:
    before = _bytes(project.parent)
    stamps = {p: p.stat().st_mtime_ns for p in project.parent.iterdir()}
    tools["get_properties"](str(project))
    tools["get_properties"](str(project), references=["R1"], properties=["MPN"])
    tools["get_properties"](str(project.parent / "mcu.kicad_sch"), root_sch=str(project))
    assert _bytes(project.parent) == before
    assert {p: p.stat().st_mtime_ns for p in project.parent.iterdir()} == stamps


def _listed():
    from mcp.server.mcpserver import MCPServer

    from kicad_mcp import gate_state, server
    from kicad_mcp.toolsets import schematic as schematic_toolset

    mcp = MCPServer("test")
    server._gate_every_toolset_tool(mcp, gate_state.GateState())
    schematic_toolset.build(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_it_is_registered_read_only_and_takes_no_lock(project) -> None:
    tool = _listed()["get_properties"]
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    assert tool.fn.__name__ == "get_properties"
    with write_layer.path_lock([project, project.parent / "mcu.kicad_sch"]):  # a held write lock does not block the read
        assert tool.fn(str(project))["ok"] is True


def test_the_tool_text_states_the_contract() -> None:
    text = " ".join(_listed()["get_properties"].description.split())
    for needle in ("Read-only", "whole sheet tree", "Value, Footprint, Datasheet, Description and LCSC", "absent", "case_variant", "Nothing is truncated",
                   "not_found", "all_found", "flags", "power, off_board, dnp", "returned too, not skipped", "set_properties", "check_symbol_fields",
                   "no write lock", "raw property nodes", "use_references", "per placed unit"):
        assert needle in text, needle


def test_the_skill_and_the_tool_text_use_the_same_words() -> None:
    repo = Path(__file__).resolve().parents[1]
    skill = " ".join((repo / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8").split())
    for phrase in ("`get_properties` (read-only, no lock;", "`references`, `uuids` and `properties`", "default Value, Footprint, Datasheet, Description and LCSC", "under `absent`"):
        assert phrase in skill, phrase
