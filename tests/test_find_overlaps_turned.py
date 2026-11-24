"""find_overlaps places the Reference and Value boxes as KiCad draws them on turned and mirrored symbols (issue 223).

KiCad stores a field's angle relative to the symbol and draws the justify words swapped on 90 and 180 degree turns (a mirror
swaps one word more): the rule of issue 221 (``sheet_property_arrange.drawn_box``). The fixtures are built with the schematic tools only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_overlaps, write_layer
from boardproof import sheet_property_arrange as spa
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset
from tests.test_property_arrange import _TURNS, _rendered_texts


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _sheet(tmp_path: Path, tools: dict, turns: list[tuple[str, int | None, str | None]], lib_id: str = "Device:LED") -> Path:
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    specs = [{"lib_id": lib_id, "x": 30.0 + 30.0 * i, "y": 60.0, "ref": f"X{i}", "value": f"VAL{i}"} for i in range(len(turns))]
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"
    for i, (_name, by, axis) in enumerate(turns):
        if by:
            assert tools["rotate_items"](str(sch), [{"references": [f"X{i}"], "by": by}])["ok"]
        if axis:
            assert tools["mirror_items"](str(sch), [{"references": [f"X{i}"], "axis": axis}])["ok"]
    return sch


def _text_text(tools: dict, sch: Path) -> list:
    return tools["find_overlaps"](str(sch), ["text_text"])["findings"]


@pytest.mark.parametrize("by", [90, 270])
def test_reference_and_value_drawn_side_by_side_on_a_turned_part_are_not_a_text_text_finding(tmp_path, tools, by):
    sch = _sheet(tmp_path, tools, [(str(by), by, None)])
    # the shape of a real sheet: both texts stored at angle 90 on a part turned 90 or 270, drawn upright, 1.4 mm apart
    props = {"Reference": {"offset": [0, -1.4], "angle": 90}, "Value": {"offset": [0, 0], "angle": 90}}
    assert tools["set_property_positions"](str(sch), [{"pattern": "*", "properties": props}])["ok"]
    assert _text_text(tools, sch) == []


@pytest.mark.parametrize("by", [90, 270])
def test_reference_and_value_drawn_on_top_of_each_other_on_a_turned_part_are_one_finding(tmp_path, tools, by):
    sch = _sheet(tmp_path, tools, [(str(by), by, None)])
    same = {"offset": [0, -3], "angle": 90}
    assert tools["set_property_positions"](str(sch), [{"pattern": "*", "properties": {"Reference": same, "Value": same}}])["ok"]
    found = _text_text(tools, sch)
    assert len(found) == 1 and {i["property"] for i in found[0]["items"]} == {"Reference", "Value"}


@pytest.mark.parametrize("lib_id", ["Device:R", "Device:LED"])
def test_every_property_box_of_find_overlaps_agrees_with_the_svg_on_every_turn_and_mirror(tmp_path, tools, lib_id):
    """Side and extent of each visible property box, with the stored justify words KiCad's own files carry (left, right, top,
    bottom, none), against the text kicad-cli draws."""
    sch = _sheet(tmp_path, tools, _TURNS, lib_id)
    # give every text a different stored justify so each word and each turn is exercised, not only the arranged ones
    for i in range(len(_TURNS)):
        assert tools["set_property_positions"](str(sch), [{"references": [f"X{i}"], "properties": {
            "Reference": {"offset": [6, -6], "justify": "left top"}, "Value": {"offset": [-6, 6], "justify": "right bottom"}}}])["ok"]
    drawn = _rendered_texts(sch, tmp_path / "svg")
    boxes = {t.text: t.box for t in sheet_overlaps._read(write_layer.load(sch))[1] if t.kind == "property"}
    assert len(boxes) == 2 * len(_TURNS)
    for text, box in boxes.items():
        _rot, dx, dy, w = drawn[text]
        slack = 0.3 + abs(w - (box[2] - box[0])) / 2  # the estimate is shorter than the stroke font: the side is what is checked
        assert abs((box[0] + box[2]) / 2 - dx) < slack and abs((box[1] + box[3]) / 2 - dy) < slack, (lib_id, text, box, drawn[text])


def test_one_shared_rule_not_a_second_copy():
    src = Path(sheet_overlaps.__file__).read_text(encoding="utf-8")
    assert "_upright_flips" not in src and "_flip(" not in src
    assert spa.drawn_box is sheet_overlaps.drawn_box


def test_the_wording_of_tool_text_skill_and_architecture_is_pinned(tools):
    root = Path(__file__).resolve().parents[1]
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    text = next(t.description for t in mcp._tool_manager.list_tools() if t.name == "find_overlaps")  # type: ignore[attr-defined]
    text = " ".join(text.split())
    for phrase in ("placed as KiCad draws it: the symbol's turn and mirror applied to the field's stored angle and justify words",
                   "the two texts of a part turned 90 or 270 degrees that stand side by side are not a finding",
                   "the box of a hierarchical sheet symbol's text is not turned",
                   "so covered_fraction has the right shape there",
                   "Not measured: which unit copy of a multi-unit symbol KiCad draws"):
        assert phrase in text, phrase
    assert "Known limit: on a symbol turned 90 or 270" not in text and "which this check does not" not in text
    skill = (root / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8")
    assert "the Reference and Value boxes of a turned or mirrored part are placed as KiCad draws them" in skill
    arch = (root / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "`find_overlaps` places a placed symbol's property boxes as drawn (issue 223;" in arch
    assert "since issue 223 `find_overlaps` uses the same `drawn_box`" in arch
    assert "`find_overlaps` places a box by the stored angle alone" not in arch
