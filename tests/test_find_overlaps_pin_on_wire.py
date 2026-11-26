"""find_overlaps, issue 262: pin_on_wire, texts over power symbols, and the rotated-part text cases.

Every fixture is built with the schematic tools; the pin position is cross-checked against the raw sheet file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_editing, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


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


def _sheet(tmp_path: Path) -> Path:
    return Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])


def _kind(tools: dict, sch: Path, kind: str) -> list[dict]:
    out = tools["find_overlaps"](str(sch), [kind])
    assert out["status"] == "ok", out
    return out["findings"]


def _place(tools: dict, sch: Path, specs: list[dict]) -> None:
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"


def _wires(tools: dict, sch: Path, rails: list[tuple[list[float], list[float]]]) -> None:
    assert tools["add_wires"](str(sch), [{"p1": a, "p2": b, "route": "direct", "snap": False} for a, b in rails])["status"] == "ok"


def _top_pin(sch: Path) -> tuple[float, float]:
    root = write_layer.load(sch)
    node = next(s for s in root.children if not s.is_atom and s.name == "symbol" and sheet_editing._ref(s) == "C1")
    return next((p["x"], p["y"]) for p in sheet_editing.pin_positions(root, node, with_hidden=True) if p["y"] < 50.0)


CAP = {"lib_id": "Device:C", "x": 100.0, "y": 50.0, "ref": "C1", "value": "1u"}


@pytest.mark.parametrize("junction", [False, True])
def test_a_pin_in_the_middle_of_a_wire_is_pin_on_wire_with_or_without_a_junction(tmp_path, tools, junction):
    sch = _sheet(tmp_path)
    _place(tools, sch, [CAP])
    pin = _top_pin(sch)  # the raw pin position, read back from the sheet file
    _wires(tools, sch, [([90.0, pin[1]], [110.0, pin[1]])])
    if junction:
        assert tools["add_junctions"](str(sch), [list(pin)])["status"] == "ok"
    found = _kind(tools, sch, "pin_on_wire")
    assert len(found) == 1 and found[0]["junction"] is junction and found[0]["pin"] == pytest.approx(list(pin)) and found[0]["estimated"] is False
    assert [i["kind"] for i in found[0]["items"]] == ["symbol", "wire"]
    assert len(_kind(tools, sch, "wire_through_pin")) == (0 if junction else 1)


def test_a_pin_on_a_wire_end_or_a_wire_beside_it_is_not_pin_on_wire(tmp_path, tools):
    sch = _sheet(tmp_path)
    _place(tools, sch, [CAP])
    pin = _top_pin(sch)
    _wires(tools, sch, [([pin[0], pin[1]], [110.0, pin[1]]), ([90.0, 44.0], [110.0, 44.0])])
    assert _kind(tools, sch, "pin_on_wire") == []


def test_a_power_symbol_on_a_rail_is_not_pin_on_wire(tmp_path, tools):
    sch = _sheet(tmp_path)
    _place(tools, sch, [{"lib_id": "power:GND", "x": 100.0, "y": 50.0, "ref": "#PWR01", "value": "GND"}])
    _wires(tools, sch, [([90.0, 50.0], [110.0, 50.0])])
    assert _kind(tools, sch, "pin_on_wire") == []


def test_a_label_over_a_ground_symbol_is_a_text_symbol_but_the_label_on_its_pin_is_not(tmp_path, tools):
    sch = _sheet(tmp_path)
    _place(tools, sch, [{"lib_id": "power:GND", "x": 100.0, "y": 50.0, "ref": "#PWR01", "value": "GND"}])
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 100.0, "y": 52.5, "text": "ISENSE", "snap": False},
                                          {"kind": "label", "x": 100.0, "y": 50.0, "text": "ON_PIN", "snap": False}])["status"] == "ok"
    found = _kind(tools, sch, "text_symbol")
    assert [i["text"] for f in found for i in f["items"] if i["kind"] == "label"] == ["ISENSE"]
    assert any(i.get("reference") == "#PWR01" for i in found[0]["items"])


def _rotated_pair(tools: dict, sch: Path, value_offset: float) -> None:
    _place(tools, sch, [{"lib_id": "Device:R", "x": 100.0, "y": 50.0, "ref": "R1", "value": "1k"},
                        {"lib_id": "Device:R", "x": 100.0, "y": 60.0, "ref": "R2", "value": "2k"}])
    assert tools["rotate_items"](str(sch), [{"references": ["R1", "R2"], "by": 90}])["ok"]
    top = {"angle": 90, "justify": "left top"}
    assert tools["set_property_positions"](str(sch), [
        {"references": ["R1"], "properties": {"Value": {"offset": [0, value_offset], **top}}},
        {"references": ["R2"], "properties": {"Reference": {"offset": [0, 0], **top}}}])["ok"]


def test_value_text_of_a_turned_part_over_a_foreign_reference_is_a_text_text(tmp_path, tools):
    sch = _sheet(tmp_path)
    _rotated_pair(tools, sch, 10.0)  # R1's Value lands on R2's Reference at (100, 60), both stored at angle 90 with justify top
    found = _value_over_reference(tools, sch)
    assert len(found) == 1 and found[0]["estimated"] is True


def _value_over_reference(tools: dict, sch: Path) -> list[dict]:
    """The text_text findings between R1's Value ('1k') and R2's Reference ('R2') (each part's own texts are not the case)."""
    return [f for f in _kind(tools, sch, "text_text") if {i["text"] for i in f["items"]} == {"1k", "R2"}]


def test_the_same_texts_apart_are_no_text_text(tmp_path, tools):
    sch = _sheet(tmp_path)
    _rotated_pair(tools, sch, 0.0)
    assert _value_over_reference(tools, sch) == []


def test_a_label_inside_a_turned_resistor_body_is_a_text_symbol(tmp_path, tools):
    sch = _sheet(tmp_path)
    _place(tools, sch, [{"lib_id": "Device:R", "x": 100.0, "y": 50.0, "ref": "R1", "value": "1k"}])
    assert tools["rotate_items"](str(sch), [{"references": ["R1"], "by": 90}])["ok"]
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 100.0, "y": 50.0, "text": "PA8_EN", "rotation": 90, "snap": False}])["status"] == "ok"
    assert [i["text"] for f in _kind(tools, sch, "text_symbol") for i in f["items"] if i["kind"] == "label"] == ["PA8_EN"]


def test_a_label_over_a_pin_name_inside_a_body_is_a_text_over_pin_text(tmp_path, tools):
    sch = _sheet(tmp_path)
    _place(tools, sch, [{"lib_id": "Amplifier_Operational:LM358", "x": 100.0, "y": 100.0, "ref": "U1", "value": "x"}])
    assert tools["add_text"](str(sch), [{"x": 95.2, "y": 102.9, "text": "NOTE"}], snap=False)["status"] == "ok"  # on the name '-' of pin 2 (issue 225)
    assert any(i.get("what") == "name" for f in _kind(tools, sch, "text_over_pin_text") for i in f["items"])


def test_check_project_carries_only_the_pin_on_wire_count(tmp_path, tools):
    sch = _sheet(tmp_path)
    _place(tools, sch, [CAP])
    pin = _top_pin(sch)
    _wires(tools, sch, [([90.0, pin[1]], [110.0, pin[1]])])
    res = tools["check_project"](str(sch), checks=["connectivity"])
    assert res["totals"]["pin_on_wire"] == 1 and res["sheets"][0]["pin_on_wire"]["count"] == 1 and "listed" not in res["sheets"][0]["pin_on_wire"]
    assert res["no_findings"] is False


def _quiet_project(tools: dict, sch: Path, on_rail: bool) -> None:
    """A part whose pins carry no ERC or connectivity finding: the bottom pin has a no-connect marker and the top pin sits on a wire
    (with a junction, so it is connected) whose ends carry no-connect markers; the control has the wire start at the pin instead."""
    _place(tools, sch, [{**CAP, "x": 101.6, "y": 50.8}])  # on the 1.27 mm grid, so ERC has no off-grid warning
    pin = (101.6, 46.99)  # the raw (at 101.6 50.8) less the library pin offset 3.81
    left = [91.44, pin[1]] if on_rail else list(pin)
    _wires(tools, sch, [(left, [111.76, pin[1]])])
    marks = [{"kind": "no_connect", "x": x, "y": y, "snap": False} for x, y in ((pin[0], 54.61), tuple(left), (111.76, pin[1]))]
    assert tools["add_labels"](str(sch), marks)["status"] == "ok"
    if on_rail:
        assert tools["add_junctions"](str(sch), [list(pin)])["status"] == "ok"


@pytest.mark.parametrize("on_rail", [False, True])
def test_pin_on_wire_alone_decides_no_findings_of_a_full_check_project(tmp_path, tools, on_rail):
    sch = _sheet(tmp_path)
    _quiet_project(tools, sch, on_rail)
    res = tools["check_project"](str(sch))  # every check, so no_findings can be true
    assert res["totals"]["pin_on_wire"] == int(on_rail), res
    assert res["no_findings"] is (not on_rail), res


def _raw_at(sch: Path) -> list[float]:
    root = write_layer.load(sch)
    node = next(s for s in root.children if not s.is_atom and s.name == "symbol" and sheet_editing._ref(s) == "C1")
    at = node.find_children("at")[0]
    return [at.get_float(0), at.get_float(1), at.get_float(2)]


def _turned(tools: dict, sch: Path, by: int | None, axis: str | None) -> None:
    _place(tools, sch, [CAP])
    if by:
        assert tools["rotate_items"](str(sch), [{"references": ["C1"], "by": by}])["ok"]
    if axis:
        assert tools["mirror_items"](str(sch), [{"references": ["C1"], "axis": axis}])["ok"]


@pytest.mark.parametrize("by,axis,expected", [(90, None, [(96.19, 50.0), (103.81, 50.0)]), (270, None, [(96.19, 50.0), (103.81, 50.0)]),
                                              (None, "x", [(100.0, 46.19), (100.0, 53.81)]), (None, "y", [(100.0, 46.19), (100.0, 53.81)]),
                                              (90, "x", [(96.19, 50.0), (103.81, 50.0)])])
def test_pin_on_wire_on_a_turned_and_a_mirrored_part_lands_on_each_raw_pin(tmp_path, tools, by, axis, expected):
    """The pins of Device:C are the raw library points (0, +-3.81): after any turn or mirror about the raw (at 100 50) they are the
    two points checked here by plain arithmetic, not by sheet_editing.pin_positions. A rail through each of them is one finding at that pin."""
    for pin in expected:
        sch = _sheet(tmp_path / f"{pin[0]}_{pin[1]}")
        _turned(tools, sch, by, axis)
        assert _raw_at(sch)[:2] == [100.0, 50.0]
        horizontal = expected[0][1] == expected[1][1]  # the pins lie side by side, so the rail through one is vertical
        rail = ([pin[0], pin[1] - 10.0], [pin[0], pin[1] + 10.0]) if horizontal else ([pin[0] - 10.0, pin[1]], [pin[0] + 10.0, pin[1]])
        _wires(tools, sch, [rail])
        found = _kind(tools, sch, "pin_on_wire")
        assert [f["pin"] for f in found] == [pytest.approx(list(pin))]


def test_a_rail_split_at_the_pin_is_not_pin_on_wire(tmp_path, tools):
    sch = _sheet(tmp_path)
    _place(tools, sch, [CAP])
    pin = _top_pin(sch)
    _wires(tools, sch, [([90.0, pin[1]], list(pin)), (list(pin), [110.0, pin[1]])])
    assert _kind(tools, sch, "pin_on_wire") == []


def test_the_pin_test_is_within_a_micrometre():
    """sheet_editing.TOL is 0.001 mm. The tools write wires on 0.01 mm, so a rail 0.0005 mm off the pin cannot be built with them:
    the test the finder uses (``_strictly_inside``) is pinned directly, just inside and just outside the tolerance."""
    from boardproof import sheet_overlaps

    a, b = (90.0, 46.19), (110.0, 46.19)
    assert sheet_overlaps._strictly_inside(a, b, (100.0, 46.1905)) and sheet_overlaps._strictly_inside(a, b, (100.0, 46.1895))
    assert not sheet_overlaps._strictly_inside(a, b, (100.0, 46.1925)) and not sheet_overlaps._strictly_inside(a, b, (100.0, 46.18))
    assert not sheet_overlaps._strictly_inside(a, b, (90.0, 46.19)) and not sheet_overlaps._strictly_inside(a, b, (110.0005, 46.19))


@pytest.mark.parametrize("offset,reported", [(0.0, True), (0.01, False)])
def test_a_wire_a_hundredth_off_the_pin_is_not_pin_on_wire(tmp_path, tools, offset, reported):
    sch = _sheet(tmp_path)
    _place(tools, sch, [CAP])
    pin = _top_pin(sch)
    _wires(tools, sch, [([90.0, pin[1] + offset], [110.0, pin[1] + offset])])
    assert bool(_kind(tools, sch, "pin_on_wire")) is reported
