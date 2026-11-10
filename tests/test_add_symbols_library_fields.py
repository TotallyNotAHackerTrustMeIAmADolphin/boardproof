"""add_symbols and replace_symbols take over the fields of the library symbol, as KiCad does (GitHub issue 162).

A user project placed diodes, switches and an IC from a project library with ``add_symbols``: the instances had
an empty Footprint, Datasheet ``~`` and no LCSC although the library symbol carried all three; a symbol placed
by hand in KiCad copies every library field. Fixtures: a project library made through kicad_tools' S-expression
model and the write layer on ``tmp_path`` (no raw text edit); the instances are read from the raw sheet and from
kicad-cli's netlist.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_authoring, sheet_editing, write_layer
from kicad_mcp.toolsets import library as library_toolset
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset


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


def _prop(name: str, value: str, hidden: bool = False) -> str:
    return f'(property "{name}" "{value}" (at 0 0 0) (effects (font (size 1.27 1.27)){" (hide yes)" if hidden else ""}))'


def _symbol(name: str, props: list[str], pin_x: float = 0) -> str:
    return (f'(symbol "{name}" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes) '
            + _prop("Reference", "D") + " " + _prop("Value", name) + " " + " ".join(props) + " "
            + f'(symbol "{name}_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254) (type default)) (fill (type none)))) '
            + f'(symbol "{name}_1_1" (pin passive line (at {pin_x} 3.81 270) (length 1.27) (name "A" (effects (font (size 1.27 1.27)))) '
              '(number "1" (effects (font (size 1.27 1.27))))) '
              f'(pin passive line (at {pin_x} -3.81 90) (length 1.27) (name "B" (effects (font (size 1.27 1.27)))) '
              '(number "2" (effects (font (size 1.27 1.27))))))) ')


_FULL = [_prop("Footprint", "Diode_SMD:D_SOD-123", True), _prop("Datasheet", "https://example.com/d.pdf", True),
         _prop("Description", "Schottky diode"), _prop("LCSC", "C8598", True), _prop("MPN", "SS14")]
_LIB = ('(kicad_symbol_lib (version 20231120) (generator "test") (generator_version "8.0") '
        + _symbol("Full", _FULL) + _symbol("Bare", [_prop("Footprint", ""), _prop("Datasheet", "~")])
        + _symbol("Shown", [_prop("Footprint", "Diode_SMD:D_SMA"), _prop("Datasheet", "https://example.com/s.pdf")])
        + _symbol("Legacy", [_prop("Footprint", "Diode_SMD:D_SMB").replace("(hide yes)", "")[:-2] + " hide))", _prop("LCSC", "C7", True).replace("(hide yes)", "hide")])
        + _symbol("Valued", [_prop("Footprint", "Diode_SMD:D_SOD-123", True)]).replace('(property "Value" "Valued"', '(property "Value" "1N5819"') + ")")


@pytest.fixture
def sheet(tmp_path) -> Path:
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    lib = tmp_path / "proj" / "lib" / "parts.kicad_sym"
    lib.parent.mkdir(parents=True, exist_ok=True)
    write_layer.save(parse_string(_LIB), lib)
    registered = _tools(library_toolset)["register_library_asset"](str(tmp_path / "proj"), symbol_lib_path=str(lib))
    assert registered["symbol"]["status"] == "ok", registered
    return sch


def _node(sch: Path, ref: str):
    for c in write_layer.load(sch).children:
        if not c.is_atom and c.name == "symbol" and c.find_children("lib_id") and sheet_editing._ref(c) == ref:
            return c
    raise AssertionError(ref)


def _props(sch: Path, ref: str) -> dict[str, tuple[str, bool]]:
    return {p.get_string(0): (p.get_string(1), sheet_editing._hidden(p)) for p in sheet_editing._kids(_node(sch, ref), "property")}


def _netlist_fields(sch: Path, ref: str) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    records: dict = {}
    sheet_editing.read_nets(tree, sch, None, capture=records)
    comps = records["components"]
    return next(c for c in comps if c["ref"] == ref)


def _spec(ref="D1", lib="parts:Full", **extra) -> dict:
    return {"lib_id": lib, "x": 50.8, "y": 50.8, "ref": ref, **extra}


def test_add_symbols_copies_every_library_field_with_its_hidden_state(sheet):
    result = _tools()["add_symbols"](str(sheet), [_spec()])
    assert result["status"] == "ok", result
    props = _props(sheet, "D1")
    assert props["Footprint"] == ("Diode_SMD:D_SOD-123", True)
    assert props["Datasheet"] == ("https://example.com/d.pdf", True)
    assert props["Description"] == ("Schottky diode", False)
    assert props["LCSC"] == ("C8598", True)
    assert props["MPN"] == ("SS14", False)
    assert props["Reference"][0] == "D1"
    assert props["Value"][0] == "Full"


def test_the_netlist_after_the_call_carries_the_fields(sheet):
    assert _tools()["add_symbols"](str(sheet), [_spec()])["status"] == "ok"
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sheet, None))
    records: dict = {}
    sheet_editing.read_nets(tree, sheet, None, capture=records)
    comp = next(c for c in records["components"] if c["ref"] == "D1")
    assert comp["fields"] == {"Footprint": "Diode_SMD:D_SOD-123", "Datasheet": "https://example.com/d.pdf", "Description": "Schottky diode",
                              "LCSC": "C8598", "MPN": "SS14"}


def test_a_field_the_library_lacks_stays_absent_and_empty_library_fields_stay_empty(sheet):
    _assert_tilde_assumption(sheet)
    assert _tools()["add_symbols"](str(sheet), [_spec("D1", "parts:Bare"), _spec("D2", "parts:Full")])["status"] == "ok"
    bare = _props(sheet, "D1")
    assert "LCSC" not in bare and "MPN" not in bare and "Manufacturer" not in _props(sheet, "D2")
    assert bare["Footprint"][0] == "" and bare["Datasheet"][0] == "~"


def test_the_value_comes_from_the_library_unless_the_spec_gives_one(sheet):
    assert _tools()["add_symbols"](str(sheet), [_spec("D1", "parts:Valued"), _spec("D2", "parts:Valued", value="BAT54")])["status"] == "ok"
    assert _props(sheet, "D1")["Value"][0] == "1N5819"
    assert _props(sheet, "D2")["Value"][0] == "BAT54"


def test_the_explicit_footprint_stays_an_override(sheet):
    assert _tools()["add_symbols"](str(sheet), [_spec(footprint="Diode_SMD:D_SMA")])["status"] == "ok"
    props = _props(sheet, "D1")
    assert props["Footprint"][0] == "Diode_SMD:D_SMA"
    assert props["LCSC"] == ("C8598", True)  # the rest still comes from the library
    assert _netlist_fields(sheet, "D1")["fields"]["Footprint"] == "Diode_SMD:D_SMA"


def test_the_properties_map_overrides_library_fields_adds_new_ones_and_sets_hidden_state(sheet):
    result = _tools()["add_symbols"](str(sheet), [_spec(properties={
        "LCSC": "C1", "Description": {"value": "my diode", "hidden": True}, "Tolerance": "5%", "Shown": {"value": "x", "hidden": False}})])
    assert result["status"] == "ok", result
    props = _props(sheet, "D1")
    assert props["LCSC"] == ("C1", True)  # the library field's hidden state is kept when the override gives none
    assert props["Description"] == ("my diode", True)
    assert props["Tolerance"] == ("5%", True)  # a new field is hidden unless asked
    assert props["Shown"] == ("x", False)
    assert props["MPN"] == ("SS14", False)  # untouched library field
    fields = _netlist_fields(sheet, "D1")["fields"]
    assert fields["LCSC"] == "C1" and fields["Tolerance"] == "5%" and fields["Description"] == "my diode"


def test_a_stock_library_part_takes_its_description_and_datasheet(sheet):
    assert _tools()["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    props = _props(sheet, "R1")
    assert props["Description"][0] == "Resistor" and props["Value"][0] == "10k"
    assert _netlist_fields(sheet, "R1")["fields"]["Description"] == "Resistor"


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


@pytest.mark.parametrize("extra, text", [
    ({"properties": {"Reference": "X1"}}, "Reference"),
    ({"properties": {"Value": "X"}}, "Value"),
    ({"properties": {"uuid": "x"}}, "uuid"),
    ({"properties": {"": "x"}}, "property name"),
    ({"properties": {" LCSC": "x"}}, "white space"),
    ({"properties": {"LCSC": 5}}, "string"),
    ({"properties": {"LCSC": {"value": "x", "hidden": "yes"}}}, "hidden"),
    ({"properties": ["LCSC"]}, "object"),
    ({"properties": {"Footprint": "A:B"}, "footprint": "C:D"}, "twice"),
])
def test_a_bad_properties_map_refuses_and_writes_nothing(sheet, extra, text):
    before = _bytes(sheet)
    result = _tools()["add_symbols"](str(sheet), [_spec(**extra)])
    assert result["ok"] is False and result["code"] == "invalid_spec" and text in result["reason"], result
    assert _bytes(sheet) == before


def test_two_units_of_one_reference_must_ask_for_the_same_fields(sheet):
    before = _bytes(sheet)
    result = _tools()["add_symbols"](str(sheet), [_spec("U1", unit=1, properties={"LCSC": "C1"}), _spec("U1", unit=2, properties={"LCSC": "C2"})])
    assert result["ok"] is False and result["code"] == "invalid_spec" and "another unit" in result["reason"], result
    assert _bytes(sheet) == before


def test_the_field_check_fires_on_a_corrupted_write(sheet, monkeypatch):
    """The self-check: a write that drops or alters a field the netlist should carry is write_not_verified, sheet untouched."""
    from kicad_mcp.toolsets import schematic as sch_mod

    real = sch_mod._apply_library_fields

    def corrupt(root, overrides, old_uuids):
        expected = real(root, overrides, old_uuids)
        for node in root.children:
            if not node.is_atom and node.name == "symbol" and node.find_children("lib_id"):
                prop = sheet_editing._property(node, "LCSC")
                if prop is not None:
                    write_layer.set_string(prop, 1, "C999")
        return expected

    monkeypatch.setattr(sch_mod, "_apply_library_fields", corrupt)
    before = _bytes(sheet)
    result = _tools()["add_symbols"](str(sheet), [_spec()])
    assert result["ok"] is False and result["error"] == "write_not_verified", result
    assert "netlist_follows_fields" in str(result) and "LCSC" in str(result)
    assert _bytes(sheet) == before


# ---------------------------------------------------------------- wording (critical-class text)

_ROOT = Path(__file__).resolve().parents[1]


def _doc(name: str) -> str:
    return next(t.description for t in MCPServer_tools() if t.name == name)


def MCPServer_tools():
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    return mcp._tool_manager.list_tools()  # type: ignore[attr-defined]


def test_the_add_symbols_text_says_what_is_copied_and_what_overrides():
    text = _doc("add_symbols")
    for needle in ("takes over every", "Footprint, Datasheet, Description, LCSC", "hidden state", "lacks stays absent", "properties",
                   "netlist_follows_fields", "invalid_spec", "set_properties", "library_fields true"):
        assert needle in text, needle


def test_the_replace_symbols_text_names_the_library_fields_option_and_its_default():
    text = _doc("replace_symbols")
    for needle in ("library_fields (default false", "never touched", "keeps the symbol's own field", "library_fields_taken"):
        assert needle in text, needle


def test_the_schematic_skill_names_the_field_copy():
    skill = (_ROOT / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8")
    assert "takes over every field of the library symbol" in skill and "`properties` map" in skill and "`library_fields: true`" in skill


# ---------------------------------------------------------------- replace_symbols (same gap)


def _placed_r(sheet) -> None:
    assert _tools()["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "D1", "value": "keep"}])["status"] == "ok"
    assert _tools()["set_properties"](str(sheet), [{"reference": "D1", "set": {"Footprint": "Mine:Assigned"}}])["status"] == "ok"


def test_replace_symbols_keeps_the_field_texts_by_default(sheet):
    _placed_r(sheet)
    result = _tools()["replace_symbols"](str(sheet), [{"reference": "D1", "lib_id": "parts:Full"}])
    assert result.get("ok") is True, result
    props = _props(sheet, "D1")
    assert props["Footprint"][0] == "Mine:Assigned" and "LCSC" not in props and props["Value"][0] == "keep"
    assert result["replaced"][0]["library_fields_taken"] == {}


def test_replace_symbols_with_library_fields_adopts_every_field_and_the_netlist_shows_it(sheet):
    _placed_r(sheet)
    result = _tools()["replace_symbols"](str(sheet), [{"reference": "D1", "lib_id": "parts:Full", "library_fields": True}])
    assert result.get("ok") is True, result
    props = _props(sheet, "D1")
    assert props["Footprint"] == ("Diode_SMD:D_SOD-123", True) and props["LCSC"] == ("C8598", True) and props["MPN"] == ("SS14", False)
    assert props["Value"][0] == "keep" and props["Reference"][0] == "D1"  # Reference and Value are never taken
    assert result["replaced"][0]["library_fields_taken"]["LCSC"] == "C8598"
    assert _netlist_fields(sheet, "D1")["fields"]["LCSC"] == "C8598"


def test_replace_symbols_library_fields_dry_run_writes_nothing(sheet):
    _placed_r(sheet)
    before = _bytes(sheet)
    result = _tools()["replace_symbols"](str(sheet), [{"reference": "D1", "lib_id": "parts:Full", "library_fields": True}], dry_run=True)
    assert result.get("ok") is True and result["would_replace"][0]["library_fields_taken"]["LCSC"] == "C8598", result
    assert _bytes(sheet) == before


def test_replace_symbols_library_fields_must_be_a_boolean(sheet):
    _placed_r(sheet)
    before = _bytes(sheet)
    result = _tools()["replace_symbols"](str(sheet), [{"reference": "D1", "lib_id": "parts:Full", "library_fields": "yes"}])
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _bytes(sheet) == before


def test_replace_symbols_field_check_fires_on_a_corrupted_adoption(sheet, monkeypatch):
    _placed_r(sheet)
    real = sheet_editing.take_library_fields

    def corrupt(node, entry, skip=frozenset()):
        done = real(node, entry, skip)
        write_layer.set_string(sheet_editing._property(node, "LCSC"), 1, "C999")
        return done

    monkeypatch.setattr(sheet_editing, "take_library_fields", corrupt)
    before = _bytes(sheet)
    result = _tools()["replace_symbols"](str(sheet), [{"reference": "D1", "lib_id": "parts:Full", "library_fields": True}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "LCSC" in str(result), result
    assert _bytes(sheet) == before


def test_a_library_field_shown_in_the_library_is_shown_on_the_instance(sheet):
    # kicad_tools writes Footprint and Datasheet hidden; the library's state wins
    assert _tools()["add_symbols"](str(sheet), [_spec("D1", "parts:Shown")])["status"] == "ok"
    props = _props(sheet, "D1")
    assert props["Footprint"] == ("Diode_SMD:D_SMA", False) and props["Datasheet"] == ("https://example.com/s.pdf", False)


def test_a_library_field_hidden_with_the_older_bare_hide_is_hidden_on_the_instance(sheet):
    assert _tools()["add_symbols"](str(sheet), [_spec("D1", "parts:Legacy")])["status"] == "ok"
    props = _props(sheet, "D1")
    assert props["Footprint"] == ("Diode_SMD:D_SMB", True) and props["LCSC"] == ("C7", True)


def test_an_override_can_show_or_hide_a_field_the_instance_already_has(sheet):
    result = _tools()["add_symbols"](str(sheet), [_spec(properties={"Datasheet": {"value": "https://x/y.pdf", "hidden": False}})])
    assert result["status"] == "ok", result
    assert _props(sheet, "D1")["Datasheet"] == ("https://x/y.pdf", False)


def test_unannotated_duplicates_may_ask_for_different_fields(sheet):
    result = _tools()["add_symbols"](str(sheet), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R?", "properties": {"LCSC": "C1"}},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R?", "properties": {"LCSC": "C2"}}])
    assert result["status"] == "ok", result


def test_units_of_one_reference_from_two_libraries_are_not_compared_with_the_netlist(sheet):
    """KiCad reads one unit's copy of a part's fields; with two different library symbols under one reference
    which copy it reads is not ours to guess, so the field check is skipped for that reference."""
    result = _tools()["add_symbols"](str(sheet), [_spec("U1", "parts:Full", unit=1), _spec("U1", "parts:Bare", unit=2, x=101.6)])
    assert result["status"] == "ok", result
    assert _props(sheet, "U1")["LCSC"][0] == "C8598"


# ---------------------------------------------------------------- per placement, not per reference (review of issue 162)


def _by_ref_in_order(sch: Path, ref: str) -> list[dict]:
    return [{p.get_string(0): p.get_string(1) for p in sheet_editing._kids(n, "property")}
            for n in write_layer.load(sch).children
            if not n.is_atom and n.name == "symbol" and n.find_children("lib_id") and sheet_editing._ref(n) == ref]


def test_two_unannotated_specs_each_keep_their_own_property_overrides(sheet):
    result = _tools()["add_symbols"](str(sheet), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R?", "properties": {"LCSC": "C1"}},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R?", "properties": {"LCSC": "C2"}}])
    assert result["status"] == "ok", result
    assert [p["LCSC"] for p in _by_ref_in_order(sheet, "R?")] == ["C1", "C2"]


def test_an_explicit_footprint_of_one_unannotated_spec_is_not_lost_to_its_neighbour(sheet):
    result = _tools()["add_symbols"](str(sheet), [_spec("D?", footprint="A:B"), _spec("D?", x=76.2)])
    assert result["status"] == "ok", result
    assert [p["Footprint"] for p in _by_ref_in_order(sheet, "D?")] == ["A:B", "Diode_SMD:D_SOD-123"]


def test_the_field_check_compares_only_a_lone_tilde_as_empty(sheet, monkeypatch):
    from kicad_mcp.toolsets import schematic as sch_mod

    real = sch_mod._apply_library_fields

    def drop_the_tilde(root, overrides, old_uuids):
        expected = real(root, overrides, old_uuids)
        for node in root.children:
            if not node.is_atom and node.name == "symbol" and node.find_children("lib_id"):
                prop = sheet_editing._property(node, "LCSC")
                if prop is not None:
                    write_layer.set_string(prop, 1, "ab")
        return expected

    monkeypatch.setattr(sch_mod, "_apply_library_fields", drop_the_tilde)
    _assert_tilde_assumption(sheet)
    before = _bytes(sheet)
    result = _tools()["add_symbols"](str(sheet), [_spec(properties={"LCSC": "a~b"})])
    assert result["ok"] is False and result["error"] == "write_not_verified" and "LCSC" in str(result), result
    assert _bytes(sheet) == before


def test_a_further_unit_of_an_already_placed_reference_is_not_field_checked(sheet, monkeypatch):
    """Documented: the netlist keeps one copy of a part's fields, so a reference that was on the sheet before is skipped."""
    from kicad_mcp.toolsets import schematic as sch_mod

    assert _tools()["add_symbols"](str(sheet), [_spec("U1", unit=1)])["status"] == "ok"
    real = sch_mod._apply_library_fields

    def corrupt(root, overrides, old_uuids):
        expected = real(root, overrides, old_uuids)
        for node in root.children:
            if not node.is_atom and node.name == "symbol" and node.find_children("lib_id") and sheet_editing._unit(node) == 2:
                write_layer.set_string(sheet_editing._property(node, "LCSC"), 1, "C999")
        return expected

    monkeypatch.setattr(sch_mod, "_apply_library_fields", corrupt)
    assert _tools()["add_symbols"](str(sheet), [_spec("U1", unit=2, x=101.6)])["status"] == "ok"


def test_a_count_of_new_symbols_that_differs_from_the_specs_is_refused_and_writes_nothing(sheet, monkeypatch):
    from kicad_mcp.toolsets import schematic as sch_mod

    real = sch_mod._library_field_overrides
    monkeypatch.setattr(sch_mod, "_library_field_overrides", lambda specs: [*real(specs), *real(specs)])
    before = _bytes(sheet)
    result = _tools()["add_symbols"](str(sheet), [_spec()])
    assert result.get("status") == "incomplete" and "left unchanged" in str(result), result
    assert _bytes(sheet) == before


# ---------------------------------------------------------------- review round 2


def _sheet_version(sch: Path) -> int:
    return int(write_layer.load(sch).find_children("version")[0].get_float(0))


def _assert_tilde_assumption(sheet: Path) -> None:
    """A lone "~" in a file older than 20250318 is read as empty (KiCad 10 source research). The tests that expect the netlist to show a
    library "~" as empty rely on both the library file (20231120) and the sheet project_create writes (also 20231120, read from its
    header) being older than that."""
    assert int(parse_string(_LIB).find_children("version")[0].get_float(0)) < 20250318
    assert _sheet_version(sheet) < 20250318


def test_the_lone_tilde_assumption_is_stated_for_the_versions_in_use(sheet):
    _assert_tilde_assumption(sheet)


def test_unannotated_specs_each_keep_their_own_mirror(sheet):
    result = _tools()["add_symbols"](str(sheet), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R?", "mirror": "y"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R?"},
        {"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R?", "mirror": "x"}])
    assert result["status"] == "ok", result
    nodes = [n for n in write_layer.load(sheet).children if not n.is_atom and n.name == "symbol" and n.find_children("lib_id")]
    assert [n.find_children("mirror")[0].children[0].value if n.find_children("mirror") else "" for n in nodes] == ["y", "", "x"]


def test_two_specs_of_one_annotated_reference_and_unit_with_different_mirrors_are_still_refused(sheet):
    before = _bytes(sheet)
    result = _tools()["add_symbols"](str(sheet), [_spec("D1", mirror="y"), _spec("D1", x=76.2, mirror="x")])
    assert result["code"] == "invalid_spec" and "different mirror" in result["reason"], result
    assert _bytes(sheet) == before


def test_the_mirror_check_names_a_spec_whose_symbol_is_missing_from_the_text(sheet, monkeypatch):
    from kicad_mcp.toolsets import schematic as sch_mod

    def drop_new_symbols(root, mirrors, old_uuids):
        for node in sch_mod._new_symbol_nodes(root, old_uuids):
            root.children.remove(node)

    monkeypatch.setattr(sch_mod, "_apply_mirrors", drop_new_symbols)
    before = _bytes(sheet)
    result = _tools()["add_symbols"](str(sheet), [_spec("J1", mirror="y")])
    assert result["ok"] is False and result["error"] == "write_not_verified", result
    assert result["mismatches"]["items"][0]["subject"] == "spec 0" and result["mismatches"]["items"][0]["actual"] is None
    assert _bytes(sheet) == before
