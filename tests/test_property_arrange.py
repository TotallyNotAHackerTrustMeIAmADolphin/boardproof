"""set_property_positions(arrange=...) (GitHub issue 221): Reference and Value of many symbols by a rule, with real kicad-cli.

Vertical parts get the Reference on the left and the Value on the right, horizontal parts the Reference above and
the Value below. Positions are cross-checked against the raw S-expression of the written file and the raw
library geometry (Device:R: body rectangle x +-1.016, y +-2.54; Device:LED lies on its side), and the absence of
text_text overlaps against find_overlaps itself.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import SExp
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_editing, write_layer
from boardproof import sheet_property_arrange as spa
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


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
    """R1 (50.8, 50.8) upright with a long value; D1 (76.2, 50.8) turned 90 degrees (vertical); R2 (101.6, 50.8) turned 90 degrees
    (horizontal); R3 (127, 50.8) mirrored; C1 (152.4, 50.8) upright. The Reference and Value of every part start
    on one point, so each pair overlaps."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "100k 1%"},
        {"lib_id": "Device:LED", "x": 76.2, "y": 50.8, "ref": "D1", "value": "LED"},
        {"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R2", "value": "1k"},
        {"lib_id": "Device:R", "x": 127.0, "y": 50.8, "ref": "R3", "value": "22"},
        {"lib_id": "Device:C", "x": 152.4, "y": 50.8, "ref": "C1", "value": "10uF"},
    ])["status"] == "ok"
    # Device:LED lies on its side in the library: turned 90 degrees it is vertical, a resistor turned 90 degrees is horizontal
    assert tools["rotate_items"](str(sch), [{"references": ["R2", "D1"], "by": 90}])["ok"]
    assert tools["mirror_items"](str(sch), [{"references": ["R3"], "axis": "x"}])["ok"]
    items = [{"pattern": "*", "properties": {"Reference": {"offset": [0, -7]}, "Value": {"offset": [0, -7]}}}]
    assert tools["set_property_positions"](str(sch), items)["ok"]
    return sch


def _prop(sch: Path, ref: str, name: str) -> tuple[float, float, float, list[str]]:
    """(x, y, angle, justify words) read from the raw S-expression of the file."""
    root = write_layer.load(sch)
    node = next(s for s in root.find_children("symbol") if sheet_editing._ref(s) == ref)
    prop = sheet_editing._property(node, name)
    x, y, a = sheet_editing._at(prop)
    return x, y, a, spa._justify_words(prop)


def _text_text(tools, sch: Path) -> list:
    result = tools["find_overlaps"](str(sch), ["text_text"])
    return result["findings"]


def test_the_fixture_starts_with_overlapping_texts(sheet, tools):
    assert len(_text_text(tools, sheet)) >= 4


def test_vertical_parts_get_the_reference_left_and_the_value_right_and_no_text_text_overlap_remains(sheet, tools):
    result = tools["set_property_positions"](str(sheet), arrange={})
    assert result["ok"] and result["written"] is True, result
    # Device:R: body x +-1.016, so the box edge sits at 49.784 / 51.816 and the gap is 1.27
    assert _prop(sheet, "R1", "Reference") == (48.514, 50.8, 0.0, ["right"])
    assert _prop(sheet, "R1", "Value") == (53.086, 50.8, 0.0, ["left"])
    d1_ref, d1_val = _prop(sheet, "D1", "Reference"), _prop(sheet, "D1", "Value")
    assert d1_ref[0] < 76.2 < d1_val[0]  # drawn left and right; stored words are swapped on a symbol turned 90 degrees (below)
    assert d1_ref[3] == ["left"] and d1_val[3] == ["right"]
    assert _text_text(tools, sheet) == []
    assert result["text_text_overlaps"]["after"] == 0 and result["text_text_overlaps"]["before"] >= 4
    assert result["text_text_overlaps"]["listed"] == []


def test_horizontal_parts_get_the_reference_above_and_the_value_below(sheet, tools):
    tools["set_property_positions"](str(sheet), arrange={})
    ref, val = _prop(sheet, "R2", "Reference"), _prop(sheet, "R2", "Value")
    assert ref[1] < 50.8 < val[1] and ref[3] == ["top"] and val[3] == ["bottom"]  # stored swapped: KiCad draws a 90 degree turn flipped
    assert ref[0] == val[0] == 101.6 and ref[2] == val[2] == 90.0  # stored relative to a symbol turned 90 degrees: 90 draws upright
    # the turned resistor is 5.08 wide in x (pin to pin 7.62 with the pins) and 2.032 high: above and below its box
    assert ref[1] == pytest.approx(50.8 - 1.016 - 1.27, abs=1e-3) and val[1] == pytest.approx(50.8 + 1.016 + 1.27, abs=1e-3)


def test_a_mirrored_part_is_judged_as_drawn(sheet, tools):
    tools["set_property_positions"](str(sheet), arrange={})
    assert _prop(sheet, "R3", "Reference") == (124.714, 50.8, 0.0, ["right"])
    assert _prop(sheet, "R3", "Value") == (129.286, 50.8, 0.0, ["left"])


def test_a_selection_moves_only_the_selected_symbols_and_gap_is_honoured(sheet, tools):
    before = _prop(sheet, "C1", "Value")
    result = tools["set_property_positions"](str(sheet), arrange={"selector": {"references": ["R1"]}, "gap": 2.54})
    assert result["ok"] and result["changed_references"] == ["R1"] and result["count"] == 2, result
    assert _prop(sheet, "R1", "Reference")[0] == pytest.approx(49.784 - 2.54)
    assert _prop(sheet, "C1", "Value") == before


def test_a_hidden_text_and_a_pinned_text_are_never_moved(sheet, tools):
    root = write_layer.load(sheet)
    syms = {sheet_editing._ref(s): s for s in root.find_children("symbol") if s.find_children("lib_id")}
    sheet_editing._property(syms["R1"], "Value").find_children("effects")[0].children.append(SExp.list("hide", write_layer.symbol("yes")))
    sheet_editing._property(syms["R3"], "Value").children.append(SExp.list("do_not_autoplace"))
    sheet_editing._property(syms["D1"], "Reference").children.append(SExp.list("do_not_autoplace", write_layer.symbol("yes")))
    sheet_editing._property(syms["C1"], "Value").children.append(SExp.list("do_not_autoplace", write_layer.symbol("no")))  # not pinned
    write_layer.save(root, sheet)
    before = {(r, n): _prop(sheet, r, n) for r, n in (("R1", "Value"), ("D1", "Reference"), ("R3", "Value"))}
    result = tools["set_property_positions"](str(sheet), arrange={})
    assert result["ok"], result
    for key, value in before.items():
        assert _prop(sheet, *key) == value, key
    reasons = {(s["reference"], s["property"]): s["reason"] for s in result["skipped"]}
    assert reasons == {("R1", "Value"): "hidden", ("R3", "Value"): "locked", ("D1", "Reference"): "locked"}, reasons
    assert _prop(sheet, "R1", "Reference")[3] == ["right"]  # the visible text of R1 was still arranged
    assert _prop(sheet, "D1", "Value")[3] == ["right"]  # D1 is turned 90 degrees: the stored word is swapped
    assert _prop(sheet, "C1", "Value")[3] == ["left"]  # do_not_autoplace no is not a pin (an upright C is vertical)


def test_a_second_call_changes_nothing_and_writes_nothing(sheet, tools):
    assert tools["set_property_positions"](str(sheet), arrange={})["written"] is True
    once = sheet.read_bytes()
    again = tools["set_property_positions"](str(sheet), arrange={})
    assert again["ok"] and again["count"] == 0 and again["written"] is False, again
    assert sheet.read_bytes() == once


def test_dry_run_lists_what_would_move_and_writes_nothing(sheet, tools):
    before = sheet.read_bytes()
    result = tools["set_property_positions"](str(sheet), arrange={}, dry_run=True)
    assert result["ok"] and result["count"] == 10 and result["written"] is False, result
    assert {e["property"] for e in result["would_update"]} == {"Reference", "Value"}
    assert result["would_update"][0]["was"]["at"] and result["would_update"][0]["orientation"] in ("vertical", "horizontal")
    assert sheet.read_bytes() == before


def test_fields_autoplaced_is_removed_from_each_moved_symbol_and_only_from_those(sheet, tools):
    root = write_layer.load(sheet)
    for s in root.find_children("symbol"):
        if s.find_children("lib_id"):
            s.children.insert(5, SExp.list("fields_autoplaced", write_layer.symbol("yes")))
    write_layer.save(root, sheet)
    assert tools["set_property_positions"](str(sheet), arrange={"selector": {"references": ["R1", "D1"]}})["ok"]
    left = {sheet_editing._ref(s) for s in write_layer.load(sheet).find_children("symbol") if s.find_children("fields_autoplaced")}
    assert left == {"R2", "R3", "C1"}


def test_neighbours_closer_than_the_text_is_wide_are_reported_not_hidden(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "close"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k 1% thick film 0603"},
        {"lib_id": "Device:R", "x": 60.96, "y": 50.8, "ref": "R2", "value": "4.7k"},
    ])["status"] == "ok"
    result = tools["set_property_positions"](str(sch), arrange={})
    assert result["ok"]
    overlaps = result["text_text_overlaps"]
    assert overlaps["after"] >= 1 and overlaps["involving_selection"] == len(overlaps["listed"]) >= 1
    assert overlaps["after"] == len(_text_text(tools, sch))  # the same measure as find_overlaps


@pytest.mark.parametrize("kwargs", [
    {"arrange": {}, "items": [{"references": ["R1"], "properties": {"Value": {"angle": 90}}}]},
    {"arrange": {"nope": 1}},
    {"arrange": {"gap": -1}},
    {"arrange": {"gap": True}},
    {"arrange": {"gap": "wide"}},
    {"arrange": {"selector": {}}},
    {"arrange": {"selector": {"footprint": "x"}}},
    {"arrange": []},
    {},
])
def test_odd_arguments_are_refused_and_nothing_is_written(sheet, tools, kwargs):
    before = sheet.read_bytes()
    result = tools["set_property_positions"](str(sheet), **kwargs)
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert sheet.read_bytes() == before


def test_a_selector_that_matches_nothing_is_refused(sheet, tools):
    before = sheet.read_bytes()
    result = tools["set_property_positions"](str(sheet), arrange={"selector": {"references": ["R99"]}})
    assert result["ok"] is False and result["code"] == "not_found", result
    assert sheet.read_bytes() == before


def test_power_symbols_are_left_alone(sheet, tools):
    assert tools["add_labels"](str(sheet), [{"kind": "power", "text": "power:GND", "x": 50.8, "y": 70.0}])["status"] == "ok"
    result = tools["set_property_positions"](str(sheet), arrange={})
    assert result["ok"] and not [e for e in result["updated"] if e["reference"].startswith("#")]
    assert not [s for s in result["skipped"] if s["reference"].startswith("#")]


def test_a_corrupted_write_is_refused(sheet, tools, monkeypatch):
    real = write_layer.load
    before = sheet.read_bytes()

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = tools["set_property_positions"](str(sheet), arrange={})
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert sheet.read_bytes() == before


def test_the_tool_text_pins_the_rule():
    text = " ".join(_tools()["set_property_positions"].__doc__.split())
    for sentence in (
        "arrange (instead of items; giving both is refused)",
        "A symbol taller than wide is vertical: the Reference goes to the left of it, right-justified, the Value to the right, left-justified",
        "Any other symbol is horizontal: the Reference above it, bottom-justified, the Value below it, top-justified",
        "A hidden text and a locked text are never moved",
        "a call that changes nothing writes nothing",
        "text_text_overlaps {before, after, involving_selection, listed}",
        "so the stored angle is 90 on a symbol turned 90 or 270 degrees (0 otherwise)",
        "the stored justify words may be the opposite ones; the texts are drawn upright",
        "find_overlaps places its text boxes the same way (issue 223), so its text_text count for the sheet agrees with these figures",
        "(which copy KiCad draws is not measured)",
    ):
        assert sentence in text, sentence


# --------------------------------------------------------------------------- rendering (issue 221 review, B1)

_TURNS = [(f"{by}{axis or ''}", by or None, axis) for by in (0, 90, 180, 270) for axis in (None, "x", "y")]  # 12 parts


def _rendered_texts(sch: Path, out: Path) -> dict[str, tuple[list[str], float, float, float]]:
    """{text: ([rotate(...) transforms of its ancestors], x, y, width)} for every <text> of the svg kicad-cli draws
    (anchor middle, y is the baseline, 0.635 below the centre of a 1.27 mm text; no ancestor but a rotate moves it)."""
    import xml.etree.ElementTree as ET

    kicad_cli.export_svg(sch, out)
    svg = next(out.glob("*.svg"))
    tree = ET.parse(svg)
    parent = {c: p for p in tree.iter() for c in p}
    found: dict[str, tuple[list[str], float, float, float]] = {}
    for el in tree.iter():
        if el.tag.endswith("text") and (el.text or "").strip():
            chain, cur = [el.get("transform") or ""], el
            while cur in parent:
                cur = parent[cur]
                chain.append(cur.get("transform") or "")
            found[el.text.strip()] = ([t for t in chain if "rotate(" in t], float(el.get("x")), float(el.get("y")) - 0.635, float(el.get("textLength")))
    return found


@pytest.mark.parametrize("lib_id", ["Device:R", "Device:LED"])
def test_the_moved_texts_are_drawn_upright_on_every_turn_and_mirror(tmp_path, tools, lib_id):
    made = project_toolset._project_create(str(tmp_path / "turn"), "demo")
    sch = Path(made["schematic_path"])
    specs = [{"lib_id": lib_id, "x": 30.0 + 30.0 * i, "y": 60.0, "ref": f"X{i}", "value": f"VAL{i}"} for i in range(len(_TURNS))]
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"
    for i, (_name, by, axis) in enumerate(_TURNS):
        if by:
            assert tools["rotate_items"](str(sch), [{"references": [f"X{i}"], "by": by}])["ok"]
        if axis:
            assert tools["mirror_items"](str(sch), [{"references": [f"X{i}"], "axis": axis}])["ok"]
    result = tools["set_property_positions"](str(sch), arrange={}, detail=True)
    assert result["ok"], result
    drawn = _rendered_texts(sch, tmp_path / "svg")
    orientation = {e["reference"]: e["orientation"] for e in result["updated"]}
    for i, (name, _by, _axis) in enumerate(_TURNS):
        sx, sy = 30.0 + 30.0 * i, 60.0
        ref, val = drawn[f"X{i}"], drawn[f"VAL{i}"]
        where = (lib_id, name)
        assert ref[0] == [] and val[0] == [], (where, "not upright", ref[0], val[0])
        if orientation[f"X{i}"] == "vertical":  # the drawn Reference ends left of the body, the Value starts right of it
            assert ref[1] + ref[3] / 2 < sx - 1.0 and val[1] - val[3] / 2 > sx + 1.0, (where, ref, val)
        else:  # the drawn Reference lies wholly above the body, the Value wholly below it
            assert ref[2] + 0.635 < sy - 1.0 and val[2] - 0.635 > sy + 1.0, (where, ref, val)
    # the overlap figure uses the drawn orientation: the estimated box centres sit where kicad-cli drew the texts
    from boardproof import sheet_overlaps

    for t in sheet_overlaps._read(write_layer.load(sch))[1]:
        if t.kind == "property" and t.text in drawn:
            box = t.box  # find_overlaps' own box, the drawn one (issue 223)
            _rot, dx, dy, w = drawn[t.text]
            slack = 0.3 + abs(w - (box[2] - box[0])) / 2  # the estimate is shorter than the stroke font: only the side is checked
            assert abs((box[0] + box[2]) / 2 - dx) < slack and abs((box[1] + box[3]) / 2 - dy) < slack, (lib_id, t.text, box, drawn[t.text])


def test_the_schematic_skill_names_the_arrange_mode_in_items_9_and_10():
    skill = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "kicad-schematic" / "SKILL.md"
    lines = skill.read_text(encoding="utf-8").splitlines()
    nine = next(ln for ln in lines if ln.startswith("9. "))
    ten = next(ln for ln in lines if ln.startswith("10. "))
    for phrase in ("`set_property_positions` with `arrange` instead of `items`", "the whole sheet without a selector",
                   "the Reference on the left and the Value on the right", "the Reference above and the Value below, drawn upright",
                   "hidden and `do_not_autoplace` texts are skipped"):
        assert phrase in nine, phrase
    assert "arrange them with `set_property_positions` and `arrange`, item 9" in ten
    assert "check them with `set_property_positions`" not in ten
