"""check_symbol_fields (GitHub issue 163): empty Footprint, missing LCSC and empty or "~" Datasheet.

Reproduction of the need: neither ``check_connectivity`` nor ERC reports these, so a sheet that is
wired and ERC-clean still gives an incomplete BOM. Fixture (``root`` of tests/test_sheet_pins.py plus
the cases below, built only through the tools and the write layer): R1 on the root sheet (add_symbols
leaves Footprint empty and Datasheet "~"), R10 (Connectors sheet) complete, R20 (MCU sheet) without LCSC.
Root sheet extras: R30 (DNP), R31 (off board), #PWR01 (power), R32 ("Lcsc" in the wrong case), R33
(LCSC blank), R34 (all three fields real).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boardproof import field_check, kicad_cli, write_layer
from tests import test_sheet_pins as _base
from tests.test_sheet_pins import _bytes
from tests._fixture_copy import copy_fixture

root = _base.root
tools = _base.tools
_need_kicad_cli = _base._need_kicad_cli

OLD_MEANING = "KiCad reads ~ as empty (file version below 20250318)"
NEW_MEANING = "KiCad keeps ~ as literal text (file version 20250318 or later)"
FULL = {"Footprint": "Resistor_SMD:R_0603_1608Metric", "LCSC": "C25804", "Datasheet": "https://example.invalid/r.pdf"}


def _sym(path: Path, ref: str):
    tree = write_layer.load(path)
    for s in tree.find_children("symbol"):
        if s.find_children("lib_id") and s.find_children("property")[0].get_string(1) == ref:
            return tree, s
    raise AssertionError(ref)


def _uuid(path: Path, ref: str) -> str:
    return _sym(path, ref)[1].find_children("uuid")[0].get_string(0)


@pytest.fixture(scope="module")
def _template(tmp_path_factory) -> Path:
    return build_template(tmp_path_factory)


@pytest.fixture
def project(_template, tmp_path) -> Path:
    copy_fixture(_template.parent, tmp_path / "proj")
    return tmp_path / "proj" / _template.name


def build_template(tmp_path_factory) -> Path:
    """The fixture project built once by a module-scoped fixture of any test module (its tool calls run kicad-cli)."""
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    tools = _base._tools()
    return build_project(_base.build_root(tmp_path_factory.mktemp("template"), tools), tools)


def build_project(root, tools) -> Path:
    folder = root.parent
    def r(ref, x, y):
        return {"lib_id": "Device:R", "x": x, "y": y, "ref": ref, "value": "1k"}

    placed = tools["add_symbols"](str(root), [
        {**r("R30", 76.2, 60.96), "dnp": True}, r("R31", 88.9, 60.96), r("R32", 76.2, 76.2), r("R33", 88.9, 76.2), r("R34", 63.5, 76.2),
        {"lib_id": "power:GND", "x": 63.5, "y": 90, "ref": "#PWR01", "value": "GND"}])
    assert placed["status"] == "ok", placed
    done = tools["set_properties"](str(root), [
        {"reference": "R32", "set": {"Lcsc": "C1", "Footprint": FULL["Footprint"], "Datasheet": FULL["Datasheet"]}},
        {"reference": "R33", "set": {"LCSC": "   ", "Footprint": FULL["Footprint"], "Datasheet": FULL["Datasheet"]}},
        {"reference": "R34", "set": FULL}])
    assert done["status"] == "ok", done
    for ref, fields in (("R10", FULL), ("R20", {k: v for k, v in FULL.items() if k != "LCSC"})):
        child = folder / ("connectors.kicad_sch" if ref == "R10" else "mcu.kicad_sch")
        assert tools["set_properties"](str(child), [{"reference": ref, "set": fields}], root_sch=str(root))["status"] == "ok"
    tree, off = _sym(root, "R31")
    write_layer.set_symbol(off.find_children("on_board")[0], 0, "no")
    write_layer.save(tree, root)
    return root


def _gaps(result: dict) -> dict[str, dict[str, str]]:
    return {s["reference"]: {g["field"]: g["problem"] for g in s["gaps"]} for e in result["sheets"] for s in e["symbols"]}


def test_default_fields_over_the_whole_hierarchy_in_one_call(project, tools) -> None:
    result = tools["check_symbol_fields"](str(project))
    assert result["ok"] is True and result["written"] is False, result
    assert result["required_fields"] == ["Footprint", "LCSC", "Datasheet"]
    assert _gaps(result) == {
        "R1": {"Footprint": "empty", "LCSC": "missing", "Datasheet": "empty"},  # issue 162: add_symbols copies the library's empty Datasheet
        "R20": {"LCSC": "missing"},
        "R32": {"LCSC": "missing"},
        "R33": {"LCSC": "blank"},
    }
    assert result["no_gaps"] is False and result["symbols_with_gaps"] == 4 and result["gap_count"] == 6
    assert result["checked"] == 6 and result["placed_symbols"] == 9  # R1 R10 R20 R32 R33 R34 checked; R30 R31 #PWR01 skipped


def test_gaps_are_listed_per_sheet_by_reference_and_uuid(project, tools) -> None:
    result = tools["check_symbol_fields"](str(project))
    sheets = {e["sheet"]: e for e in result["sheets"]}
    assert set(sheets) == {project.name, "connectors.kicad_sch", "mcu.kicad_sch"}
    assert sheets["connectors.kicad_sch"]["checked"] == 1 and sheets["connectors.kicad_sch"]["symbols"] == []
    assert sheets["connectors.kicad_sch"]["symbols_with_gaps"] == 0
    mcu = sheets["mcu.kicad_sch"]
    assert [s["reference"] for s in mcu["symbols"]] == ["R20"] and mcu["symbols_with_gaps"] == 1
    assert mcu["symbols"][0]["uuid"] == _uuid(project.parent / "mcu.kicad_sch", "R20")
    assert mcu["symbols"][0]["lib_id"] == "Device:R" and mcu["symbols"][0]["unit"] == "1"
    top = {s["reference"]: s for s in sheets[project.name]["symbols"]}
    assert top["R1"]["uuid"] == _uuid(project, "R1")
    assert top["R1"]["gaps"][2] == {"field": "Datasheet", "problem": "empty", "value": ""}
    assert top["R1"]["gaps"][0] == {"field": "Footprint", "problem": "empty", "value": ""}
    assert top["R1"]["gaps"][1]["value"] is None


def test_a_child_sheet_given_with_the_root_reports_the_whole_project(project, tools) -> None:
    whole = tools["check_symbol_fields"](str(project))
    from_child = tools["check_symbol_fields"](str(project.parent / "mcu.kicad_sch"), root_sch=str(project))
    assert from_child == whole


def test_skipped_power_off_board_and_dnp_are_listed_apart_and_counted(project, tools) -> None:
    result = tools["check_symbol_fields"](str(project))
    assert result["skipped_counts"] == {"power": 1, "off_board": 1, "dnp": 1, "no_lib_id": 0}
    by = {k: [(s["reference"], s["uuid"]) for s in v] for k, v in result["skipped"].items()}
    assert by == {"no_lib_id": [], "power": [("#PWR01", _uuid(project, "#PWR01"))], "off_board": [("R31", _uuid(project, "R31"))], "dnp": [("R30", _uuid(project, "R30"))]}
    assert "R30" not in _gaps(result) and "R31" not in _gaps(result) and "#PWR01" not in _gaps(result)


def test_a_dnp_and_off_board_symbol_is_listed_once_under_off_board(project, tools) -> None:
    tree, sym = _sym(project, "R31")
    write_layer.set_symbol(sym.find_children("dnp")[0], 0, "yes")
    write_layer.save(tree, project)
    counts = tools["check_symbol_fields"](str(project))["skipped_counts"]
    assert counts == {"power": 1, "off_board": 1, "dnp": 1, "no_lib_id": 0}


def test_a_power_symbol_is_skipped_by_its_flag_not_only_by_the_hash(project, tools) -> None:
    tree, sym = _sym(project, "#PWR01")
    for prop in sym.find_children("property"):
        if prop.get_string(0) == "Reference":
            write_layer.set_string(prop, 1, "GNDX1")
    for inst in sym.find_children("instances"):
        for proj in inst.find_children("project"):
            for path in proj.find_children("path"):
                write_layer.set_string(path.find_children("reference")[0], 0, "GNDX1")
    write_layer.save(tree, project)
    assert tools["check_symbol_fields"](str(project))["skipped_counts"]["power"] == 1


def test_a_hash_reference_is_skipped_even_without_the_power_flag(project, tools) -> None:
    tree, sym = _sym(project, "R34")
    for prop in sym.find_children("property"):
        if prop.get_string(0) == "Reference":
            write_layer.set_string(prop, 1, "#FLG01")
    for inst in sym.find_children("instances"):
        for proj in inst.find_children("project"):
            for path in proj.find_children("path"):
                write_layer.set_string(path.find_children("reference")[0], 0, "#FLG01")
    write_layer.save(tree, project)
    assert tools["check_symbol_fields"](str(project))["skipped_counts"]["power"] == 2


def test_required_fields_are_configurable_and_exact(project, tools) -> None:
    only_fp = tools["check_symbol_fields"](str(project), ["Footprint"])
    assert _gaps(only_fp) == {"R1": {"Footprint": "empty"}}
    custom = tools["check_symbol_fields"](str(project), ["MPN", "Value"])
    assert set(_gaps(custom)) == {"R1", "R10", "R20", "R32", "R33", "R34"}
    assert all(set(g) == {"MPN"} and g["MPN"] == "missing" for g in _gaps(custom).values())
    clean = tools["check_symbol_fields"](str(project), ["Value"])
    assert clean["no_gaps"] is True and clean["symbols_with_gaps"] == 0 and clean["gap_count"] == 0
    assert all(e["symbols"] == [] for e in clean["sheets"])


def test_placed_symbols_is_checked_plus_everything_skipped(project, tools) -> None:
    result = tools["check_symbol_fields"](str(project))
    assert result["placed_symbols"] == result["checked"] + sum(result["skipped_counts"].values()) == 9


def test_a_symbol_without_lib_id_is_counted_and_listed_not_dropped(project, tools) -> None:
    uuid = _uuid(project, "R34")
    tree, sym = _sym(project, "R34")
    sym.children.remove(sym.find_children("lib_id")[0])
    write_layer.save(tree, project)
    result = tools["check_symbol_fields"](str(project))
    assert result["skipped_counts"]["no_lib_id"] == 1 and result["placed_symbols"] == 9 and result["checked"] == 5
    (row,) = result["skipped"]["no_lib_id"]
    assert row["uuid"] == uuid and row["lib_id"] == "" and row["sheet"] == project.name
    assert result["placed_symbols"] == result["checked"] + sum(result["skipped_counts"].values())
    assert "R34" not in _gaps(result)


def test_a_sheet_file_used_twice_is_listed_once_with_two_uses_and_the_first_use_reference(project, tools) -> None:
    import copy

    tree = write_layer.load(project)
    sheets = tree.find_children("sheet")
    mcu = next(s for s in sheets if any(p.get_string(0) == "Sheetname" and p.get_string(1) == "MCU" for p in s.find_children("property")))
    twin = copy.deepcopy(mcu)
    write_layer.set_string(twin.find_children("uuid")[0], 0, "11111111-2222-4333-8444-555555555555")
    for prop in twin.find_children("property"):
        if prop.get_string(0) == "Sheetname":
            write_layer.set_string(prop, 1, "MCU2")
    for prop in twin.find_children("at"):
        write_layer.set_number(prop, 1, 160.02)
    tree.children.insert(tree.children.index(mcu) + 1, twin)
    twin_path = "/" + tree.find_children("uuid")[0].get_string(0) + "/11111111-2222-4333-8444-555555555555"
    write_layer.save(tree, project)
    # the second use carries its own reference R21 in the symbol's instance data; the first use's R20 is what is reported
    child = project.parent / "mcu.kicad_sch"
    ctree, sym = _sym(child, "R20")
    entry = sym.find_children("instances")[0].find_children("project")[0].find_children("path")[0]
    second = copy.deepcopy(entry)
    write_layer.set_string(second, 0, twin_path)
    write_layer.set_string(second.find_children("reference")[0], 0, "R21")
    sym.find_children("instances")[0].find_children("project")[0].children.append(second)
    write_layer.save(ctree, child)
    result = tools["check_symbol_fields"](str(project))
    assert result["ok"] is True, result
    mcu_sheet = next(e for e in result["sheets"] if e["sheet"] == "mcu.kicad_sch")
    (row,) = mcu_sheet["symbols"]  # one file, listed once
    assert row["reference"] == "R20" and row["uses"] == 2 and mcu_sheet["checked"] == 1
    assert row["use_references"] == ["R20", "R21"]  # issue 166 item 1: the references of every use, so a netlist reference of a further use is findable
    assert all("use_references" not in r for e in result["sheets"] for r in e["symbols"] if r["uses"] == 1)
    assert result["placed_symbols"] == 9


def test_a_two_unit_part_is_checked_per_placed_unit(project, tools) -> None:
    placed = tools["add_symbols"](str(project), [
        {"lib_id": "Amplifier_Operational:LM324", "x": 150, "y": 60, "ref": "U9", "value": "LM324", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM324", "x": 150, "y": 90, "ref": "U9", "value": "LM324", "unit": 2}])
    assert placed["status"] == "ok", placed
    result = tools["check_symbol_fields"](str(project), ["LCSC"])
    rows = [s for e in result["sheets"] for s in e["symbols"] if s["reference"] == "U9"]
    assert sorted(r["unit"] for r in rows) == ["1", "2"] and len({r["uuid"] for r in rows}) == 2
    assert all(r["gaps"] == [{"field": "LCSC", "problem": "missing", "value": None}] for r in rows)
    assert result["checked"] == 8 and result["placed_symbols"] == 11


def test_a_tilde_datasheet_on_a_placed_symbol_is_reported_as_a_placeholder(project, tools) -> None:
    tree, node = _sym(project, "R34")
    write_layer.set_string(next(p for p in node.find_children("property") if p.get_string(0) == "Datasheet"), 1, "~")
    write_layer.save(tree, project)
    gaps = next(s for e in tools["check_symbol_fields"](str(project))["sheets"] for s in e["symbols"] if s["reference"] == "R34")["gaps"]
    assert gaps == [{"field": "Datasheet", "problem": "placeholder", "value": "~", "meaning": OLD_MEANING}]


def test_a_wrong_case_field_is_named_as_a_case_variant(project, tools) -> None:
    gap = next(s for e in tools["check_symbol_fields"](str(project))["sheets"] for s in e["symbols"] if s["reference"] == "R32")["gaps"][0]
    assert gap == {"field": "LCSC", "problem": "missing", "value": None, "case_variant": ["Lcsc"]}
    plain = next(s for e in tools["check_symbol_fields"](str(project))["sheets"] for s in e["symbols"] if s["reference"] == "R20")["gaps"][0]
    assert "case_variant" not in plain


def test_a_name_with_a_value_of_another_case_is_not_a_match(project, tools) -> None:
    # exact names: asking for "lcsc" is a gap on R34 (which has "LCSC") with the variant named
    gaps = _gaps(tools["check_symbol_fields"](str(project), ["lcsc"]))
    assert gaps["R34"] == {"lcsc": "missing"}


@pytest.mark.parametrize("value, problem", [("", "empty"), (" \t", "blank"), ("~", "placeholder"), (" ~ ", "placeholder"), ("C1", None), ("~x", None)])
def test_problem_classes(value: str, problem: str | None) -> None:
    assert field_check._problem(value) == problem
    assert field_check._problem(None) == "missing"


def test_the_first_property_of_a_name_decides(project, tools) -> None:
    tree, sym = _sym(project, "R34")
    twin = [p for p in sym.find_children("property") if p.get_string(0) == "LCSC"][0]
    from kicad_tools.sexp import SExp  # noqa: PLC0415 - fixture construction only

    dup = SExp.list("property", write_layer.string("LCSC"), write_layer.string(""))
    sym.children.insert(sym.children.index(twin) + 1, dup)
    write_layer.save(tree, project)
    assert "R34" not in _gaps(tools["check_symbol_fields"](str(project)))


@pytest.mark.parametrize("bad", [[], "LCSC", [""], ["LCSC", "LCSC"], [" LCSC"], [1], ["A\nB"]])
def test_invalid_required_fields_are_refused(project, tools, bad) -> None:
    result = tools["check_symbol_fields"](str(project), bad)
    assert result["ok"] is False and result["code"] == "invalid_spec", result


def test_missing_file_and_incomplete_tree_are_refused(project, tools) -> None:
    assert tools["check_symbol_fields"](str(project.parent / "nope.kicad_sch"))["code"] == "file_not_found"
    (project.parent / "mcu.kicad_sch").unlink()
    result = tools["check_symbol_fields"](str(project))
    assert result["ok"] is False and result["code"] == "sheet_tree_incomplete", result


def test_a_sheet_the_project_does_not_reach_is_refused(project, tools) -> None:
    stray = project.parent / "stray.kicad_sch"
    write_layer.save(write_layer.load(project.parent / "mcu.kicad_sch"), stray)
    result = tools["check_symbol_fields"](str(stray), root_sch=str(project))
    assert result["ok"] is False and result["code"] == "parent_not_in_tree", result


def test_it_writes_nothing(project, tools) -> None:
    before = _bytes(project.parent)
    stamps = {p: p.stat().st_mtime_ns for p in project.parent.iterdir()}
    for args in ((), (["Footprint", "MPN"],)):
        tools["check_symbol_fields"](str(project), *args)
    tools["check_symbol_fields"](str(project.parent / "mcu.kicad_sch"), root_sch=str(project))
    assert _bytes(project.parent) == before
    assert {p: p.stat().st_mtime_ns for p in project.parent.iterdir()} == stamps


def test_it_is_registered_read_only_and_takes_no_lock(project) -> None:
    from mcp.server.mcpserver import MCPServer

    from boardproof import gate_state, server
    from boardproof.toolsets import schematic as schematic_toolset

    mcp = MCPServer("test")
    server._gate_every_toolset_tool(mcp, gate_state.GateState())
    schematic_toolset.build(mcp)
    listed = {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    tool = listed["check_symbol_fields"]
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    assert tool.fn.__name__ == "check_symbol_fields"
    # a held write lock on the project does not block the read
    with write_layer.path_lock([project, project.parent / "mcu.kicad_sch"]):
        assert tool.fn(str(project))["ok"] is True


def test_the_tool_text_states_the_contract() -> None:
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import schematic as schematic_toolset

    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    text = " ".join(next(t for t in mcp._tool_manager.list_tools() if t.name == "check_symbol_fields").description.split())  # type: ignore[attr-defined]
    for needle in ("Read-only", "whole sheet tree", "Footprint, LCSC and Datasheet", "missing", "empty", "blank", "placeholder", "case_variant",
                   "Nothing is truncated", "skipped", "power", "off_board", "dnp", "set_properties", "no write lock", "no_gaps is not a verdict"):
        assert needle in text, needle


def test_the_skill_and_the_tool_text_use_the_same_words() -> None:
    import ast

    repo = Path(__file__).resolve().parents[1]
    skill = " ".join((repo / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8").split())
    tree = ast.parse((repo / "src" / "boardproof" / "toolsets" / "schematic.py").read_text(encoding="utf-8"))
    doc = " ".join(next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "check_symbol_fields").body[0].value.value.split())
    for phrase in ("`check_symbol_fields` (read-only, no lock;", "`missing`, `empty`, `blank`, `placeholder`", "`case_variant`", "listed apart under `skipped`",
                   "`set_properties`", "`no_gaps` is not a verdict on the board"):
        assert phrase in skill, phrase
    for phrase in ("(problem missing)", "case_variant", "listed apart under skipped", "set_properties", "no_gaps is not a verdict on the board"):
        assert phrase in doc, phrase


# ---- issues 166 and 169 (issue 184): file version, folded names, units, includes ----------------------------

def _set_tilde(project: Path, version: int | None = None) -> None:
    tree, node = _sym(project, "R34")
    write_layer.set_string(next(p for p in node.find_children("property") if p.get_string(0) == "Datasheet"), 1, "~")
    if version is not None:
        write_layer.set_number(tree.find_children("version")[0], 0, version)
    write_layer.save(tree, project)


def test_the_sheet_entry_states_the_file_version_and_the_placeholder_meaning_follows_it(project, tools) -> None:
    _set_tilde(project)
    first = tools["check_symbol_fields"](str(project))
    assert next(e for e in first["sheets"] if e["sheet"] == project.name)["file_version"] == 20231120
    _set_tilde(project, 20260306)
    result = tools["check_symbol_fields"](str(project))
    assert next(e for e in result["sheets"] if e["sheet"] == project.name)["file_version"] == 20260306
    gaps = next(s for e in result["sheets"] for s in e["symbols"] if s["reference"] == "R34")["gaps"]
    assert gaps == [{"field": "Datasheet", "problem": "placeholder", "value": "~", "meaning": NEW_MEANING}]


def test_a_placeholder_of_a_field_that_was_not_measured_carries_no_meaning(project, tools) -> None:
    tree, node = _sym(project, "R34")
    write_layer.set_string(next(p for p in node.find_children("property") if p.get_string(0) == "LCSC"), 1, "~")
    write_layer.save(tree, project)
    gaps = next(s for e in tools["check_symbol_fields"](str(project))["sheets"] for s in e["symbols"] if s["reference"] == "R34")["gaps"]
    assert gaps == [{"field": "LCSC", "problem": "placeholder", "value": "~"}]


def test_the_tilde_cause_of_an_edit_mismatch_depends_on_the_file_version() -> None:
    from boardproof import sheet_editing

    assert sheet_editing._cause("Datasheet", "~", "", 20231120) == "tilde"
    assert sheet_editing._cause("Datasheet", "~", "", 20260306) == "elsewhere"
    assert sheet_editing._cause("Datasheet", "~", "", 20250318) == "elsewhere"
    assert sheet_editing._cause("Datasheet", "~", None, 20250317) == "tilde"
    assert "20250318" in sheet_editing._CAUSES["tilde"]


def _rename(project: Path, ref: str, old: str, new: str, value: str | None = None) -> None:
    tree, node = _sym(project, ref)
    prop = next(p for p in node.find_children("property") if p.get_string(0) == old)
    write_layer.set_string(prop, 0, new)
    if value is not None:
        write_layer.set_string(prop, 1, value)
    write_layer.save(tree, project)


def test_a_lowercase_footprint_property_is_the_footprint_field_as_kicad_folds_it(project, tools) -> None:
    _rename(project, "R34", "Footprint", "footprint")
    result = tools["check_symbol_fields"](str(project))
    assert "R34" not in _gaps(result)  # present: the netlist carries it as Footprint
    entry = next(e for e in result["sheets"] if e["sheet"] == project.name)
    (folded,) = [f for f in entry["folded"] if f["reference"] == "R34"]
    assert folded["field"] == "Footprint" and folded["property"] == "footprint" and folded["value"] == FULL["Footprint"]
    assert result["folded_count"] == 1
    # an empty folded value is still a gap, named by the property it sits in
    _rename(project, "R34", "footprint", "footprint", "")
    gap = next(s for e in tools["check_symbol_fields"](str(project))["sheets"] for s in e["symbols"] if s["reference"] == "R34")["gaps"][0]
    assert gap == {"field": "Footprint", "problem": "empty", "value": "", "property": "footprint"}


def test_only_footprint_is_folded_other_wrong_case_names_stay_missing(project, tools) -> None:
    gaps = _gaps(tools["check_symbol_fields"](str(project), ["LCSC", "Footprint"]))
    assert gaps["R32"] == {"LCSC": "missing"}  # "Lcsc" is a different field to KiCad (measured)


def test_a_multi_unit_part_names_its_units_and_that_the_netlist_reads_one_copy(project, tools) -> None:
    placed = tools["add_symbols"](str(project), [
        {"lib_id": "Amplifier_Operational:LM324", "x": 150, "y": 60, "ref": "U9", "value": "LM324", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM324", "x": 150, "y": 90, "ref": "U9", "value": "LM324", "unit": 2}])
    assert placed["status"] == "ok", placed
    result = tools["check_symbol_fields"](str(project), ["LCSC"])
    rows = [s for e in result["sheets"] for s in e["symbols"] if s["reference"] == "U9"]
    assert [r["part_units"] for r in rows] == [["1", "2"], ["1", "2"]]
    assert all("part_units" not in s for e in result["sheets"] for s in e["symbols"] if s["reference"] != "U9")
    assert "order of the symbols in the file" in result["multi_unit_note"]


def test_dnp_and_off_board_symbols_are_checked_when_asked_and_marked(project, tools) -> None:
    result = tools["check_symbol_fields"](str(project), include=["dnp", "off_board"])
    assert result["skipped_counts"]["dnp"] == 0 and result["skipped_counts"]["off_board"] == 0 and result["skipped_counts"]["power"] == 1
    rows = {s["reference"]: s for e in result["sheets"] for s in e["symbols"]}
    assert rows["R30"]["flags"] == ["dnp"] and rows["R31"]["flags"] == ["off_board"]
    assert rows["R30"]["gaps"] and rows["R31"]["gaps"]
    assert result["checked"] == 8 and result["placed_symbols"] == 9
    only_dnp = tools["check_symbol_fields"](str(project), include=["dnp"])
    assert only_dnp["skipped_counts"]["dnp"] == 0 and only_dnp["skipped_counts"]["off_board"] == 1
    default = tools["check_symbol_fields"](str(project))
    assert default["skipped_counts"]["dnp"] == 1 and all("flags" not in s for e in default["sheets"] for s in e["symbols"])


@pytest.mark.parametrize("bad", [["power"], [], "dnp", ["dnp", "dnp"], ["nope"]])
def test_invalid_include_is_refused(project, tools, bad) -> None:
    result = tools["check_symbol_fields"](str(project), include=bad)
    assert result["ok"] is False and result["code"] == "invalid_spec", result


def test_the_tool_text_and_the_skill_state_the_file_version_fold_units_and_include() -> None:
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import schematic as schematic_toolset

    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    text = " ".join(next(t for t in mcp._tool_manager.list_tools() if t.name == "check_symbol_fields").description.split())  # type: ignore[attr-defined]
    for needle in ("file version 20250318", "file_version", "meaning", "folded_count", "property named footprint (lowercase)", "use_references",
                   "part_units", "multi_unit_note", "unit 1's copy when it holds a value", "include: a list drawn from dnp and off_board"):
        assert needle in text, needle
    repo = Path(__file__).resolve().parents[1]
    skill = " ".join((repo / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8").split())
    for phrase in ("`include` (a list of `dnp` and `off_board`)", "version 20250318", "`part_units`", "`folded`"):
        assert phrase in skill, phrase
    new = text[text.index("file version 20250318") - 60:]
    for word in ("routed", "done", "complete", "finished", "clean", "verified", "ready", "connected", "passes"):
        assert word not in new.lower().replace(".", " ").replace(",", " ").split(), word


# ---- review of 0475dac (issue 184): B1 include wording, B2 unknown version, B3 boundary, multi-unit sentence ----

@pytest.mark.parametrize(("version", "meaning"), [(20250317, OLD_MEANING), (20250318, NEW_MEANING)])
def test_the_placeholder_meaning_switches_exactly_at_20250318(project, tools, version, meaning) -> None:
    _set_tilde(project, version)
    gaps = next(s for e in tools["check_symbol_fields"](str(project))["sheets"] for s in e["symbols"] if s["reference"] == "R34")["gaps"]
    assert gaps == [{"field": "Datasheet", "problem": "placeholder", "value": "~", "meaning": meaning}]


def test_an_unknown_version_claims_nothing_about_the_tilde() -> None:
    from boardproof import sheet_editing

    assert sheet_editing._cause("Datasheet", "~", "", None) == "elsewhere"
    assert sheet_editing._cause("Datasheet", "~", "", 20231120) == "tilde"


def test_an_unparsable_version_is_an_unknown_version_not_a_crash(project) -> None:
    """In memory: the write layer refuses to save a sheet that kicad-cli cannot load, so the reader helpers are called on the parsed tree."""
    from boardproof import field_check, sheet_editing

    tree = write_layer.load(project)
    assert sheet_editing._root_version(tree) == field_check._file_version(tree) == 20231120
    write_layer.set_string(tree.find_children("version")[0], 0, "abc")
    assert sheet_editing._root_version(tree) is None and field_check._file_version(tree) is None
    tree.find_children("version")[0].children.clear()
    assert sheet_editing._root_version(tree) is None


def test_the_include_text_says_which_of_the_two_are_in_the_netlist() -> None:
    import inspect

    from mcp.server.mcpserver import MCPServer

    from boardproof import field_check
    from boardproof.toolsets import schematic as schematic_toolset

    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    text = " ".join(next(t for t in mcp._tool_manager.list_tools() if t.name == "check_symbol_fields").description.split())  # type: ignore[attr-defined]
    source = " ".join(inspect.getsource(field_check.check_symbol_fields).split())
    assert "a dnp symbol is in the netlist, an off_board symbol is not in the netlist" in text
    assert "components in the netlist" not in text and "components in the netlist" not in source
    assert "an off_board symbol is not in the netlist" in source
    for word in ("routed", "done", "complete", "finished", "clean", "verified", "ready", "connected", "passes"):
        assert word not in text[text.index("include: a list"):].lower().replace(".", " ").replace(",", " ").split(), word


def test_the_multi_unit_sentence_names_unit_1_and_the_empty_case(project, tools) -> None:
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import schematic as schematic_toolset

    note = tools["check_symbol_fields"](str(project))["multi_unit_note"]
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    for text in (note, " ".join(next(t for t in mcp._tool_manager.list_tools() if t.name == "get_properties").description.split())):  # type: ignore[attr-defined]
        assert "unit 1" in text and "empty" in text and "order of the symbols in the file" in text, text
