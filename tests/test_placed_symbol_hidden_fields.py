"""A placed symbol hides the fields its library symbol hides (GitHub issue 190).

KiCad 10 writes ``(hide yes)`` as a direct child of ``property`` (every Description and ki_* field of its stock
libraries; docs/research-kicad-symbol-placement.md, section 5); KiCad 9 and older write it inside ``(effects ..)``.
The tool read only the second form, so a stock part was placed with visible Description, ki_keywords and
ki_fp_filters. Fixtures: the stock Device library and a project library made through the write layer on ``tmp_path``.
The raw tree is read here without the code under test; kicad-cli's SVG is the witness of what KiCad draws.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_editing, write_layer
from boardproof.toolsets import library as library_toolset
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

HIDDEN_BY_KICAD = ("Footprint", "Datasheet", "Description", "ki_keywords", "ki_fp_filters")


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


def _raw_hidden(prop) -> bool:
    """Independent of the code under test: a direct (hide yes) or one inside (effects)."""
    for kid in prop.children:
        if not kid.is_atom and kid.name == "hide":
            return not kid.children or kid.children[0].value == "yes"
    for eff in prop.find_children("effects"):
        for kid in eff.children:
            if kid.is_atom and kid.value == "hide":
                return True
            if not kid.is_atom and kid.name == "hide":
                return not kid.children or kid.children[0].value == "yes"
    return False


def _placed(sch: Path, ref: str) -> dict[str, bool]:
    for c in write_layer.load(sch).children:
        if not c.is_atom and c.name == "symbol" and c.find_children("lib_id") and sheet_editing._ref(c) == ref:
            return {p.get_string(0): _raw_hidden(p) for p in c.find_children("property")}
    raise AssertionError(ref)


def _prop(name: str, value: str, form: str) -> str:
    head = f'(property "{name}" "{value}" (at 0 0 0) '
    if form == "direct":
        return head + '(hide yes) (show_name no) (do_not_autoplace no) (effects (font (size 1.27 1.27))))'
    if form == "effects":
        return head + '(effects (font (size 1.27 1.27)) (hide yes)))'
    if form == "effects_no":
        return head + '(effects (font (size 1.27 1.27)) (hide no)))'
    return head + "(effects (font (size 1.27 1.27))))"  # shown


def _lib(form: str) -> str:
    fields = " ".join([_prop("Footprint", "Diode_SMD:D_SOD-123", form), _prop("Datasheet", "~", form),
                       _prop("Description", "A diode", form), _prop("ki_keywords", "diode schottky", form),
                       _prop("ki_fp_filters", "D*", form), _prop("Shown", "x", "shown")])
    body = ('(symbol "Part_0_1" (rectangle (start -1 -2) (end 1 2) (stroke (width 0.2) (type default)) (fill (type none)))) '
            '(symbol "Part_1_1" (pin passive line (at 0 3.81 270) (length 1.27) (name "A" (effects (font (size 1.27 1.27)))) '
            '(number "1" (effects (font (size 1.27 1.27))))) (pin passive line (at 0 -3.81 90) (length 1.27) '
            '(name "B" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27))))))')
    return ('(kicad_symbol_lib (version 20251024) (generator "test") (generator_version "10.0") '
            '(symbol "Part" (in_bom yes) (on_board yes) ' + _prop("Reference", "D", "shown") + " " + _prop("Value", "Part", "shown")
            + " " + fields + " " + body + "))")


def _project(tmp_path: Path, form: str) -> Path:
    made = project_toolset._project_create(str(tmp_path / f"proj_{form}"), "demo")
    sch = Path(made["schematic_path"])
    lib = tmp_path / f"proj_{form}" / "lib" / "parts.kicad_sym"
    lib.parent.mkdir(parents=True, exist_ok=True)
    write_layer.save(parse_string(_lib(form)), lib)
    reg = _tools(library_toolset)["register_library_asset"](str(tmp_path / f"proj_{form}"), symbol_lib_path=str(lib))
    assert reg["symbol"]["status"] == "ok", reg
    return sch


def test_a_stock_part_is_placed_with_the_fields_hidden_that_kicad_hides(tmp_path):
    sch = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    assert _tools()["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    placed = _placed(sch, "R1")
    for name in HIDDEN_BY_KICAD:
        assert placed[name] is True, (name, placed)
    assert placed["Reference"] is False and placed["Value"] is False


def test_kicad_draws_none_of_the_hidden_texts(tmp_path):
    """The witness: the SVG of the sheet holds the reference and value, and not the Description."""
    sch = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    assert _tools()["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    out = tmp_path / "svg"
    out.mkdir()
    import subprocess

    subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "export", "svg", "-o", str(out), str(sch)], check=True, capture_output=True)
    text = "".join(f.read_text(encoding="utf-8", errors="replace") for f in out.glob("*.svg"))
    assert "10k" in text and "R1" in text
    assert "Resistor" not in text and "resistor" not in text and "R_*" not in text


@pytest.mark.parametrize("form", ["direct", "effects"])
def test_add_symbols_keeps_the_library_hidden_state_in_either_file_form(tmp_path, form):
    sch = _project(tmp_path, form)
    assert _tools()["add_symbols"](str(sch), [{"lib_id": "parts:Part", "x": 50.8, "y": 50.8, "ref": "D1"}])["status"] == "ok"
    placed = _placed(sch, "D1")
    assert all(placed[n] for n in HIDDEN_BY_KICAD), placed
    assert placed["Shown"] is False  # a field the library shows stays shown


def test_replace_symbols_with_library_fields_keeps_the_hidden_state(tmp_path):
    sch = _project(tmp_path, "direct")
    tools = _tools()
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "D1"}])["status"] == "ok"
    result = tools["replace_symbols"](str(sch), [{"reference": "D1", "lib_id": "parts:Part", "pin_map": {"1": "1", "2": "2"}, "library_fields": True}])
    assert result["status"] == "ok", result
    placed = _placed(sch, "D1")
    assert all(placed[n] for n in HIDDEN_BY_KICAD), placed
    assert placed["Shown"] is False


def test_the_properties_map_unhides_a_field_that_is_hidden_in_the_direct_form(tmp_path):
    """_set_hidden removes the direct form too, else a field asked to be shown stays hidden."""
    sch = _project(tmp_path, "direct")
    spec = {"lib_id": "parts:Part", "x": 50.8, "y": 50.8, "ref": "D1", "properties": {"Description": {"value": "d", "hidden": False}}}
    assert _tools()["add_symbols"](str(sch), [spec])["status"] == "ok"
    placed = _placed(sch, "D1")
    assert placed["Description"] is False and placed["ki_keywords"] is True


def test_library_hidden_reads_the_three_forms_and_no():
    def prop(tail: str):
        return parse_string(f'(property "X" "v" (at 0 0 0) {tail})')

    assert sheet_editing.library_hidden(prop("(hide yes) (effects (font (size 1 1)))"))
    assert sheet_editing.library_hidden(prop("(effects (font (size 1 1)) (hide yes))"))
    assert sheet_editing.library_hidden(prop("(effects (font (size 1 1)) hide)"))
    assert not sheet_editing.library_hidden(prop("(hide no) (effects (font (size 1 1)))"))
    assert not sheet_editing.library_hidden(prop("(effects (font (size 1 1)))"))


def test_set_properties_shows_a_field_a_kicad_10_sheet_holds_in_the_direct_form(tmp_path):
    """A sheet saved by KiCad 10 holds (hide yes) directly under the property; asking for hidden false must remove it."""
    sch = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    tools = _tools()
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    root = write_layer.load(sch)
    node = next(c for c in root.children if not c.is_atom and c.name == "symbol" and c.find_children("lib_id") and sheet_editing._ref(c) == "R1")
    prop = next(p for p in node.find_children("property") if p.get_string(0) == "Description")
    eff = prop.find_children("effects")[0]
    eff.children[:] = [k for k in eff.children if k.is_atom or k.name != "hide"]
    prop.children.insert(3, write_layer.SExp.list("hide", write_layer.symbol("yes")))
    write_layer.save(root, sch)
    assert _placed(sch, "R1")["Description"] is True
    result = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Description": {"value": "Resistor", "hidden": False}}}], standalone=True)
    assert result["status"] == "ok", result
    assert _placed(sch, "R1")["Description"] is False


def test_add_symbols_leaves_a_library_field_shown_that_says_hide_no_inside_effects(tmp_path):
    sch = _project(tmp_path, "effects_no")
    assert _tools()["add_symbols"](str(sch), [{"lib_id": "parts:Part", "x": 50.8, "y": 50.8, "ref": "D1"}])["status"] == "ok"
    placed = _placed(sch, "D1")
    assert not any(placed[n] for n in HIDDEN_BY_KICAD), placed


def test_library_hidden_hide_no_inside_effects_is_shown_and_a_direct_hide_no_wins_over_effects():
    shown = parse_string('(property "X" "v" (at 0 0 0) (effects (font (size 1 1)) (hide no)))')
    assert not sheet_editing.library_hidden(shown)
    both = parse_string('(property "X" "v" (at 0 0 0) (hide no) (effects (font (size 1 1)) (hide yes)))')
    assert not sheet_editing.library_hidden(both)  # the direct form is read first and decides


def test_set_properties_shows_a_field_with_a_bare_hide_inside_effects(tmp_path):
    """KiCad 6 to 8 sheets hold a bare hide in effects; hidden false must remove it."""
    sch = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    tools = _tools()
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    root = write_layer.load(sch)
    node = next(c for c in root.children if not c.is_atom and c.name == "symbol" and c.find_children("lib_id") and sheet_editing._ref(c) == "R1")
    prop = next(p for p in node.find_children("property") if p.get_string(0) == "Description")
    eff = prop.find_children("effects")[0]
    eff.children[:] = [k for k in eff.children if k.is_atom or k.name != "hide"]
    eff.children.append(write_layer.SExp.atom("hide"))
    write_layer.save(root, sch)
    assert _placed(sch, "R1")["Description"] is True
    result = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Description": {"value": "Resistor", "hidden": False}}}], standalone=True)
    assert result["status"] == "ok", result
    assert _placed(sch, "R1")["Description"] is False
