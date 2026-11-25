"""Zone and pad nets in toolsets/routing.py come from the raw S-expressions (build ticket 249,
CLAUDE.md hard rule 2), through raw_geometry.net_name / net_table, and fail closed."""

from __future__ import annotations

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
from kicad_tools.sexp import parse_string

from boardproof import raw_geometry, write_layer
from boardproof.toolsets import routing as routing_toolset

FIXTURES = Path(__file__).parent / "fixtures"
KICAD10 = FIXTURES / "zone_obstacle" / "zone_obstacle.kicad_pcb"  # (net "GND"), version 20260206
OLDER = FIXTURES / "simple_led.kicad_pcb"  # version 20240108

LOG = """
[1/3] Routing LED_ANODE (id=5)
  forward cell (100, 100, layer=0): ok, 8/8 neighbors blocked (margin=10)
    Blocking obstacles: GND(2 pad)  [x3]
"""


def _zone_board(zone_net: str, table: str = "", uuid: str = "z0"):
    root = parse_string(
        f'(kicad_pcb (version 20240108) {table} (zone {zone_net} (layer "F.Cu") (uuid "{uuid}")))'
    )
    zone = SimpleNamespace(uuid="z0", is_filled=True, keepout=None, layer="F.Cu", priority=0, filled_polygons=[1])
    return SimpleNamespace(zones=[zone]), root


def _report(board, root):
    return routing_toolset._pre_existing_filled_zone_report(board, root)


def test_zone_net_kicad10_name_only():
    board, root = _zone_board('(net "GND")')
    assert _report(board, root)["filled_zones"][0]["net"] == "GND"


def test_zone_net_older_number_resolved_by_net_table():
    board, root = _zone_board("(net 3)", table='(net 0 "") (net 3 "GND")')
    assert _report(board, root)["filled_zones"][0]["net"] == "GND"


@pytest.mark.parametrize("net", ["(net 7)", "(net 1 2 3)", "(net)"])
def test_zone_net_unknown_form_fails_closed(net):
    board, root = _zone_board(net, table='(net 3 "GND")')
    with pytest.raises(raw_geometry.RawGeometryError):
        _report(board, root)


def test_zone_missing_from_raw_board_fails_closed():
    board, root = _zone_board('(net "GND")', uuid="other")
    with pytest.raises(raw_geometry.RawGeometryError):
        _report(board, root)


def test_pad_nets_on_both_formats(tmp_path):
    from kicad_tools.schema import PCB

    for pcb in (KICAD10, OLDER):
        by_net = routing_toolset._absolute_footprint_positions_by_net(PCB.load(pcb), write_layer.load(pcb))
        assert {"GND", "LED_ANODE", "VCC"} <= set(by_net), pcb


def test_pad_net_unknown_form_fails_closed():
    board = SimpleNamespace(footprints=[SimpleNamespace(uuid="f0", pads=[], position=(0, 0), reference="R1")])
    root = parse_string('(kicad_pcb (footprint "X" (uuid "f0") (pad "1" smd rect (net 9))))')  # 9 undeclared
    with pytest.raises(raw_geometry.RawGeometryError):
        routing_toolset._absolute_footprint_positions_by_net(board, root)


def test_footprint_missing_from_raw_board_fails_closed():
    board = SimpleNamespace(footprints=[SimpleNamespace(uuid="gone", pads=[], position=(0, 0), reference="R1")])
    with pytest.raises(raw_geometry.RawGeometryError):
        routing_toolset._absolute_footprint_positions_by_net(board, parse_string("(kicad_pcb)"))


def test_congestion_reports_incomplete_when_a_pad_net_is_unreadable(tmp_path, monkeypatch):
    pcb = tmp_path / "board.kicad_pcb"
    shutil.copy(OLDER, pcb)

    def boom(*_a):
        raise raw_geometry.RawGeometryError("net node of an unknown form")

    monkeypatch.setattr(routing_toolset, "_absolute_footprint_positions_by_net", boom)
    result = routing_toolset.suggest_placement_fixes_for_congestion(str(pcb), LOG)
    assert result["status"] != "complete"
    assert "unknown form" in str(result)


# --- review of 249: boards whose footprints carry no uuid of their own ----------------------------

RUNG1 = FIXTURES / "uw" / "r1krt" / "in" / "rung1_usbc_attiny1616.kicad_pcb"


def test_congestion_on_a_board_with_footprints_without_own_uuid(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    shutil.copy(RUNG1, pcb)
    root = write_layer.load(pcb)
    assert any(fp.name == "footprint" and not routing_toolset._direct_uuid(fp) for fp in root.children)
    result = routing_toolset.suggest_placement_fixes_for_congestion(str(pcb), LOG.replace("LED_ANODE", "GND"))
    assert "could not read" not in str(result.get("reason", "")), result


def test_every_fixture_board_reads_its_pad_nets_like_the_model():
    from kicad_tools.schema import PCB

    checked = 0
    for pcb in sorted(FIXTURES.rglob("*.kicad_pcb")):
        try:
            board = PCB.load(pcb)
        except Exception:  # noqa: BLE001 - a fixture the model cannot load is not this test's subject
            continue
        by_net = routing_toolset._absolute_footprint_positions_by_net(board, write_layer.load(pcb))
        old: dict = {}
        for fp in board.footprints:
            for net in {p.net_name for p in fp.pads if getattr(p, "net_name", None)}:
                old.setdefault(net, []).append(fp.reference)
        assert {n: [r for r, _, _ in v] for n, v in by_net.items()} == old, pcb
        checked += 1
    assert checked >= 5


def test_footprint_uuid_mismatch_fails_closed():
    board = SimpleNamespace(footprints=[SimpleNamespace(uuid="a", pads=[], position=(0, 0), reference="R1")])
    root = parse_string('(kicad_pcb (footprint "X" (uuid "b") (pad "1" smd rect (net "N"))))')
    with pytest.raises(raw_geometry.RawGeometryError):
        routing_toolset._absolute_footprint_positions_by_net(board, root)


def test_footprint_count_mismatch_fails_closed():
    fps = [SimpleNamespace(uuid="", pads=[], position=(0, 0), reference=f"R{i}") for i in range(2)]
    root = parse_string('(kicad_pcb (footprint "X" (pad "1" smd rect (net "N"))))')
    with pytest.raises(raw_geometry.RawGeometryError):
        routing_toolset._absolute_footprint_positions_by_net(SimpleNamespace(footprints=fps), root)


def test_footprint_without_own_uuid_is_matched_by_order_not_a_pad_uuid():
    board = SimpleNamespace(footprints=[SimpleNamespace(uuid="", pads=[], position=(1, 2), reference="R1")])
    root = parse_string('(kicad_pcb (footprint "X" (pad "1" smd rect (net "N") (uuid "pad-uuid"))))')
    assert routing_toolset._absolute_footprint_positions_by_net(board, root) == {"N": [("R1", 1, 2)]}


def test_footprint_nested_zone_on_a_real_board_does_not_break_the_report(tmp_path):
    """kicad_tools' PCB.zones lists top-level zones only; a nested zone stays unread, as before."""
    from kicad_tools.schema import PCB

    root = write_layer.load(KICAD10)
    fp = next(c for c in root.children if not c.is_atom and c.name == "footprint")
    fp.append(parse_string(
        '(zone (net "") (net_name "") (layer "F.Cu") (uuid "4f1c2b3a-0000-4000-8000-000000000001") (name "k")'
        ' (hatch edge 0.5) (keepout (tracks not_allowed) (vias not_allowed) (pads allowed)'
        ' (copperpour not_allowed) (footprints allowed))'
        ' (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))'
    ))
    pcb = tmp_path / "nested.kicad_pcb"
    write_layer.save(root, pcb)
    report = routing_toolset._pre_existing_filled_zone_report(PCB.load(pcb), write_layer.load(pcb))
    assert report["filled_zones"] == [{"net": "GND", "layer": "F.Cu", "priority": 1}]


def test_footprint_reference_mismatch_fails_closed():
    board = SimpleNamespace(footprints=[SimpleNamespace(uuid="", pads=[], position=(0, 0), reference="R1")])
    root = parse_string('(kicad_pcb (footprint "X" (property "Reference" "R2") (pad "1" smd rect (net "N"))))')
    with pytest.raises(raw_geometry.RawGeometryError):
        routing_toolset._absolute_footprint_positions_by_net(board, root)


def test_zone_layer_mismatch_fails_closed():
    board, root = _zone_board('(net "GND")')
    board.zones[0].layer = "B.Cu"
    with pytest.raises(raw_geometry.RawGeometryError):
        _report(board, root)


def test_zone_count_mismatch_fails_closed():
    board, root = _zone_board('(net "GND")')
    board.zones.append(board.zones[0])
    with pytest.raises(raw_geometry.RawGeometryError):
        _report(board, root)


def test_propose_route_refuses_when_a_zone_net_is_unreadable(tmp_path, monkeypatch):
    from mcp.server.mcpserver import MCPServer

    pcb = tmp_path / "board.kicad_pcb"
    shutil.copy(KICAD10, pcb)

    def boom(*_a):
        raise raw_geometry.RawGeometryError("net node of an unknown form")

    monkeypatch.setattr(routing_toolset, "_pre_existing_filled_zone_report", boom)
    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    result = tools["propose_route_with_retry"](str(pcb))
    assert result["status"] == "incomplete"
    assert "could not read the board's zone nets" in result["reason"]
