"""find_overlaps, issue 265 addition: pin_on_pin, the split shape, a short wire that ends on no rail, and the power flag.

Fixtures are built with the schematic tools; the part positions are raw (at) values.
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


def _wires(tools: dict, sch: Path, rails: list[tuple[list[float], list[float]]]) -> None:
    assert tools["add_wires"](str(sch), [{"p1": a, "p2": b, "route": "direct", "snap": False} for a, b in rails])["status"] == "ok"


def _add(tools: dict, sch: Path, specs: list[dict]) -> None:
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"


RES = {"lib_id": "Device:R", "x": 100.0, "y": 50.0, "ref": "R1", "value": "1k"}  # pins (100, 46.19) and (100, 53.81)
BOTTOM = (100.0, 53.81)
TOP = (100.0, 46.19)


@pytest.mark.parametrize("y2,reported", [(57.62, True), (57.63, False), (60.0, False)])
def test_pin_on_pin_needs_the_two_pins_on_one_point(tmp_path, tools, y2, reported):
    sch = _sheet(tmp_path)
    _add(tools, sch, [RES, {**RES, "ref": "R2", "y": y2}])
    found = _kind(tools, sch, "pin_on_pin")
    assert bool(found) is reported, found
    if reported:
        assert found[0]["pin"] == pytest.approx(list(BOTTOM)) and sorted(i["reference"] for i in found[0]["items"]) == ["R1", "R2"]
        assert found[0]["estimated"] is False


def test_one_part_alone_a_wire_on_the_point_and_a_power_symbol_are_no_pin_on_pin(tmp_path, tools):
    alone = _sheet(tmp_path / "a")
    _add(tools, alone, [RES])
    assert _kind(tools, alone, "pin_on_pin") == []
    wired = _sheet(tmp_path / "w")
    _add(tools, wired, [RES, {**RES, "ref": "R2", "y": 57.62}])
    _wires(tools, wired, [(list(BOTTOM), [110.0, BOTTOM[1]])])
    assert _kind(tools, wired, "pin_on_pin") == []
    power = _sheet(tmp_path / "p")
    _add(tools, power, [RES, {"lib_id": "power:GND", "x": BOTTOM[0], "y": BOTTOM[1], "ref": "#PWR01", "value": "GND"}])
    assert _kind(tools, power, "pin_on_pin") == [] and len(_kind(tools, power, "short_stub_to_power")) == 1


@pytest.mark.parametrize("y2", [57.62, 60.96])
def test_pin_on_pin_alone_decides_no_findings_of_a_full_check_project(tmp_path, tools, y2):
    """Two resistors on the 1.27 mm grid, every free pin carries a no-connect marker; only the second part's place differs."""
    sch = _sheet(tmp_path)
    y1 = 50.8
    _add(tools, sch, [{**RES, "x": 101.6, "y": y1}, {**RES, "ref": "R2", "x": 101.6, "y": y1 + (7.62 if y2 == 57.62 else 10.16)}])
    root = write_layer.load(sch)
    pins = [(round(p["x"], 2), round(p["y"], 2)) for n in root.children if not n.is_atom and n.name == "symbol" and n.find_children("lib_id")
            for p in sheet_editing.pin_positions(root, n, with_hidden=True)]
    on_pin = y2 == 57.62
    marks = sorted({q for q in pins if not on_pin or pins.count(q) == 1})
    assert tools["add_labels"](str(sch), [{"kind": "no_connect", "x": x, "y": y, "snap": False} for x, y in marks])["status"] == "ok"
    res = tools["check_project"](str(sch))
    assert res["totals"]["pin_on_pin"] == int(on_pin), res
    assert res["sheets"][0]["pin_on_pin"]["count"] == int(on_pin)
    assert res["no_findings"] is (not on_pin), res


def test_a_power_flag_is_not_a_power_symbol_for_the_stub(tmp_path, tools):
    """A flag on the pin (length 0) and the ground 10 mm away: no finding (a real project showed false positives for flags)."""
    sch = _sheet(tmp_path)
    _add(tools, sch, [RES, {"lib_id": "power:PWR_FLAG", "x": TOP[0], "y": TOP[1], "ref": "#FLG01", "value": "PWR_FLAG"},
                      {"lib_id": "power:GND", "x": TOP[0], "y": TOP[1] - 10.0, "ref": "#PWR01", "value": "GND"}])
    _wires(tools, sch, [(list(TOP), [TOP[0], TOP[1] - 10.0])])
    assert _kind(tools, sch, "short_stub_to_power") == []


def test_a_split_carries_its_shape(tmp_path, tools):
    """straight: two collinear ends; corner: two at an angle; branch: three or more. Every one is reported (the owner rule)."""
    left, right, up = (-10.0, 0.0), (10.0, 0.0), (0.0, -10.0)
    cases = {"straight": [left, right], "corner": [left, up], "branch": [left, right, up]}
    shapes = {}
    for name, arms in cases.items():
        sch = _sheet(tmp_path / name)
        _add(tools, sch, [RES])
        _wires(tools, sch, [(list(TOP), [round(TOP[0] + dx, 2), round(TOP[1] + dy, 2)]) for dx, dy in arms])
        found = _kind(tools, sch, "pin_on_wire")
        assert len(found) == 1 and found[0]["split"] is True, (name, found)
        shapes[name] = found[0]["shape"]
    assert shapes == {"straight": "straight", "corner": "corner", "branch": "branch"}


@pytest.mark.parametrize("end", ["dangling", "label", "pin"])
def test_a_short_wire_that_ends_on_no_rail_is_not_pin_on_wire(tmp_path, tools, end):
    """A stub of 1.0 mm that ends in the air, at a label or at another part's pin has no rail to meet: no finding."""
    sch = _sheet(tmp_path)
    _add(tools, sch, [RES] + ([{**RES, "ref": "R2", "y": 50.0 - 1.0 - 7.62}] if end == "pin" else []))  # R2's bottom pin is 1.0 above R1's top
    top = [TOP[0], round(TOP[1] - 1.0, 2)]
    _wires(tools, sch, [(list(TOP), top)])
    if end == "label":
        assert tools["add_labels"](str(sch), [{"kind": "label", "x": top[0], "y": top[1], "text": "NET", "snap": False}])["status"] == "ok"
    assert _kind(tools, sch, "pin_on_wire") == []


def test_three_parts_on_one_point_are_one_finding(tmp_path, tools):
    """Issue 270: one finding per point, listing every part there (it was one per pair: three findings for three parts)."""
    sch = _sheet(tmp_path)
    _add(tools, sch, [RES, {**RES, "ref": "R2", "y": 57.62}, {**RES, "ref": "R3", "y": 57.62}])
    found = _kind(tools, sch, "pin_on_pin")  # R2 and R3 also share their lower pin: a second point with two parts
    assert sorted(f["parts"] for f in found) == [2, 3], found
    top = next(f for f in found if f["parts"] == 3)
    assert sorted(i["reference"] for i in top["items"]) == ["R1", "R2", "R3"]


def test_two_pins_of_one_part_on_one_point_count_the_part_once(tmp_path, tools, monkeypatch):
    """Issue 270 review: R1 reads with its bottom pin twice (no library part has two coincident pins); alone it is no
    finding, with R2 on the point it is one finding with parts 2, not 3."""
    from boardproof import sheet_overlaps

    real = sheet_overlaps._read

    def doubled(root):
        out = real(root)
        for b in out[0]:
            if sheet_overlaps.se._ref(b.node) == "R1":
                b.pins = [*b.pins, *b.pins]
        return out

    monkeypatch.setattr(sheet_overlaps, "_read", doubled)
    alone = _sheet(tmp_path / "a")
    _add(tools, alone, [RES])
    assert _kind(tools, alone, "pin_on_pin") == []
    sch = _sheet(tmp_path / "b")
    _add(tools, sch, [RES, {**RES, "ref": "R2", "y": 57.62}])
    found = _kind(tools, sch, "pin_on_pin")
    assert [f["parts"] for f in found] == [1 + 1], found


def test_a_power_body_is_excluded_from_pin_on_pin(tmp_path, tools, monkeypatch):
    """Issue 270: a power symbol with a visible pin on a part's pin is no pin_on_pin (the symbol library hides power pins,
    so R2 stands in as a power symbol)."""
    from boardproof import sheet_overlaps

    sch = _sheet(tmp_path)
    _add(tools, sch, [RES, {**RES, "ref": "R2", "y": 57.62}])
    assert len(_kind(tools, sch, "pin_on_pin")) == 1
    real = sheet_overlaps._is_power
    monkeypatch.setattr(sheet_overlaps, "_is_power", lambda root, node: sheet_overlaps.se._ref(node) == "R2" or real(root, node))
    assert _kind(tools, sch, "pin_on_pin") == []
