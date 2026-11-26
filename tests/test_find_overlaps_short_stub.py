"""find_overlaps, issue 265: short_stub_to_power (a pin whose wire to a power symbol is under 1.27 mm).

Fixtures are built with the schematic tools. The pin positions of the turned and mirrored transistor are plain arithmetic on the raw
library points of Device:Q_NPN ((-5.08 0), (2.54 5.08), (2.54 -5.08), y up in the library), not sheet_editing.pin_positions.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_editing, sheet_overlaps, write_layer
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


def _found(tools: dict, sch: Path) -> list[dict]:
    out = tools["find_overlaps"](str(sch), ["short_stub_to_power"])
    assert out["status"] == "ok", out
    return out["findings"]


def _gnd(tools: dict, sch: Path, at: tuple[float, float], ref: str = "#PWR01") -> None:
    assert tools["add_symbols"](str(sch), [{"lib_id": "power:GND", "x": at[0], "y": at[1], "ref": ref, "value": "GND"}])["status"] == "ok"


def _wire(tools: dict, sch: Path, *points: tuple[float, float]) -> None:
    specs = [{"p1": list(a), "p2": list(b), "route": "direct", "snap": False} for a, b in zip(points, points[1:], strict=False)]
    assert tools["add_wires"](str(sch), specs)["status"] == "ok"


RES = {"lib_id": "Device:R", "x": 100.0, "y": 50.0, "ref": "R1", "value": "1k"}
TOP = (100.0, 46.19)  # the raw (at 100 50) less the library pin offset 3.81


def _res(tools: dict, sch: Path) -> None:
    assert tools["add_symbols"](str(sch), [RES])["status"] == "ok"


@pytest.mark.parametrize("length,reported", [(0.0, True), (1.0, True), (1.26, True), (1.27, False), (1.28, False), (5.0, False)])
def test_threshold_is_one_grid_pitch(tmp_path, tools, length, reported):
    sch = _sheet(tmp_path)
    _res(tools, sch)
    end = (TOP[0], round(TOP[1] - length, 2))
    _gnd(tools, sch, end)
    if length:
        _wire(tools, sch, TOP, end)
    found = _found(tools, sch)
    assert bool(found) is reported, found
    if reported:
        assert found[0]["length"] == pytest.approx(length) and found[0]["pin"] == pytest.approx(list(TOP)) and found[0]["estimated"] is False
        assert [i["kind"] for i in found[0]["items"]] == ["symbol", "symbol"] and found[0]["items"][1]["reference"] == "#PWR01"
    assert sheet_overlaps.MIN_STUB_MM == 1.27


@pytest.mark.parametrize("reverse", [False, True])
def test_the_wire_may_be_drawn_from_either_end(tmp_path, tools, reverse):
    sch = _sheet(tmp_path)
    _res(tools, sch)
    end = (TOP[0], TOP[1] - 1.0)
    _gnd(tools, sch, end)
    _wire(tools, sch, *((end, TOP) if reverse else (TOP, end)))
    assert [f["length"] for f in _found(tools, sch)] == [pytest.approx(1.0)]


def test_wires_meeting_end_to_end_are_summed(tmp_path, tools):
    for parts, reported in (((0.6, 0.6), True), ((0.7, 0.7), False)):
        sch = _sheet(tmp_path / str(parts[0]))
        _res(tools, sch)
        mid = (TOP[0], round(TOP[1] - parts[0], 2))
        end = (TOP[0], round(mid[1] - parts[1], 2))
        _gnd(tools, sch, end)
        _wire(tools, sch, TOP, mid, end)
        assert bool(_found(tools, sch)) is reported


def test_a_bent_wire_is_measured_along_the_wire(tmp_path, tools):
    sch = _sheet(tmp_path)
    _res(tools, sch)
    mid = (TOP[0] + 0.7, TOP[1])
    end = (mid[0], mid[1] - 0.7)  # straight distance 0.99, wire length 1.4
    _gnd(tools, sch, end)
    _wire(tools, sch, TOP, mid, end)
    assert _found(tools, sch) == []


def test_a_pin_with_no_wire_to_a_power_symbol_and_a_lone_power_symbol_are_out_of_scope(tmp_path, tools):
    sch = _sheet(tmp_path)
    _res(tools, sch)
    _gnd(tools, sch, (TOP[0], TOP[1] - 0.5))  # a power symbol near the pin, no wire between
    _wire(tools, sch, (90.0, 30.0), (95.0, 30.0))
    assert _found(tools, sch) == []


def test_the_power_symbols_own_texts_and_power_pins_are_not_parts(tmp_path, tools):
    sch = _sheet(tmp_path)
    _gnd(tools, sch, (100.0, 50.0))
    _gnd(tools, sch, (100.0, 50.0), "#PWR02")  # two power symbols on one point have no part pin
    assert _found(tools, sch) == []


def _screen(point: tuple[float, float], by: int, axis: str | None) -> tuple[float, float]:
    """A library point (x, y up) as relative screen point: y flips, a turn is counter-clockwise on the screen, the mirror comes after."""
    x, y = point[0], -point[1]
    for _ in range(by // 90):
        x, y = y, -x
    if axis == "x":
        y = -y
    if axis == "y":
        x = -x
    return x, y


@pytest.mark.parametrize("by,axis", [(0, None), (90, None), (180, None), (270, None), (0, "x"), (0, "y"), (90, "x"), (90, "y")])
def test_the_base_pin_of_a_turned_and_mirrored_transistor_is_found_where_the_raw_arithmetic_puts_it(tmp_path, tools, by, axis):
    """The base lies at the library point (-5.08, 0), the collector and emitter at (2.54, +-5.08): no point symmetry, so a flipped
    turn direction or mirror axis moves the base to another place and the power symbol on the expected place meets no pin."""
    dx, dy = _screen((-5.08, 0.0), by, axis)
    base = (round(100.0 + dx, 2), round(50.0 + dy, 2))
    sch = _sheet(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:Q_NPN", "x": 100.0, "y": 50.0, "ref": "Q1", "value": "q"}])["status"] == "ok"
    if by:
        assert tools["rotate_items"](str(sch), [{"references": ["Q1"], "by": by}])["ok"]
    if axis:
        assert tools["mirror_items"](str(sch), [{"references": ["Q1"], "axis": axis}])["ok"]
    node = next(n for n in write_layer.load(sch).children if not n.is_atom and n.name == "symbol" and n.find_children("lib_id")
                and sheet_editing._ref(n) == "Q1")
    at = node.find_children("at")[0]
    mirror = node.find_children("mirror")  # the raw stored turn and mirror, so a sign flip inside rotate_items and the reader cannot cancel
    assert [at.get_float(0), at.get_float(1), at.get_float(2)] == [100.0, 50.0, float(by)]
    assert [m.get_string(0) for m in mirror] == ([axis] if axis else [])
    _gnd(tools, sch, base)
    found = _found(tools, sch)
    assert [f["pin"] for f in found] == [pytest.approx(list(base))], (by, axis, found)


@pytest.mark.parametrize("length", [0.0, 2.54])
def test_the_count_alone_decides_no_findings_of_a_full_check_project(tmp_path, tools, length):
    """A quiet part on the 1.27 mm grid: bottom pin no-connect, top pin on a ground (with a power flag, so the net is driven) at
    `length` mm of wire. Every check runs, so no_findings can be true; only the stub differs between the two runs."""
    sch = _sheet(tmp_path)
    assert tools["add_symbols"](str(sch), [{**RES, "x": 101.6, "y": 50.8}])["status"] == "ok"
    top = (101.6, 46.99)
    end = (top[0], round(top[1] - length, 2))
    _gnd(tools, sch, end)
    assert tools["add_symbols"](str(sch), [{"lib_id": "power:PWR_FLAG", "x": end[0], "y": end[1], "ref": "#FLG01", "value": "PWR_FLAG"}])["status"] == "ok"
    if length:
        _wire(tools, sch, top, end)
    assert tools["add_labels"](str(sch), [{"kind": "no_connect", "x": 101.6, "y": 54.61, "snap": False}])["status"] == "ok"
    res = tools["check_project"](str(sch))
    assert res["totals"]["short_stub_to_power"] == int(not length), res
    assert res["sheets"][0]["short_stub_to_power"]["count"] == int(not length) and "listed" not in res["sheets"][0]["short_stub_to_power"]
    assert res["no_findings"] is bool(length), res
