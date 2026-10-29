"""Tests for the P4-06 filled-zone routing guard (archived backlog P4-06).

Root cause, with evidence, lives in `toolsets/routing.py`'s module
docstring (findings #13-#15) and `routers/kicad_routing_tools.py`'s module
docstring (finding #4): neither the `kicad_routing_tools` backend nor the
`freerouting` (DSN/SES) backend treats a pre-existing FILLED COPPER ZONE as
a routing obstacle for a net other than the zone's own. Routing openMPPT
v1.3 (which has real pre-existing filled zones) through `kicad_routing_tools`
produced 12 real shorts; this project's own tools must refuse rather than
risk that again.

`tests/fixtures/zone_obstacle/zone_obstacle.kicad_pcb` is `simple_led`'s
board plus a real, `kicad_tools.zones.ZoneGenerator`-built GND pour
straddling the direct path between R1's and D1's LED_ANODE pads, filled for
real via `kicad-cli pcb drc --refill-zones --save-board` -- never a
hand-edited file (CLAUDE.md's guardrail). Routing LED_ANODE through KRT
under `--escalation board` was independently verified (2026-09-12, outside
this test suite) to land a track AND a via at 0.0mm actual clearance
against that zone -- a real DRC-confirmed short.

The guard tests below need no backend installed at all: the check runs
before either backend is dispatched, so `backend="freerouting"` (the
default) never touches Java/Freerouting. The one real end-to-end
reproduction carries the `needs_krt` marker, same as `test_routing_krt.py`'s
`TestRealKicadRoutingTools`: it skips with the probe's reason when
KiCadRoutingTools isn't set up, and fails when KICAD_ROUTING_TOOLS_REQUIRED
is set (the gate run; build ticket 53).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp.toolsets import routing as routing_toolset

NO_ZONE_PCB = Path(__file__).parent / "fixtures" / "simple_led.kicad_pcb"
ZONE_PCB = Path(__file__).parent / "fixtures" / "zone_obstacle" / "zone_obstacle.kicad_pcb"


def _tools() -> dict:
    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _copy_fixture(src: Path, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    pcb = dest_dir / src.name
    shutil.copy(src, pcb)
    for suffix in (".kicad_pro", ".kicad_prl"):
        sibling = src.with_suffix(suffix)
        if sibling.is_file():
            shutil.copy(sibling, pcb.with_suffix(suffix))
    return pcb


# ---------------------------------------------------------------------------
# _pre_existing_filled_zone_report -- pure detection, no I/O beyond the
# already-loaded `PCB` object.
# ---------------------------------------------------------------------------


@dataclass
class _FakeZone:
    net_name: str
    layer: str
    is_filled: bool = False
    filled_polygons: list | None = None
    keepout: object | None = None
    priority: int = 0


@dataclass
class _FakeBoard:
    zones: list


def test_report_none_when_board_has_no_zones():
    assert routing_toolset._pre_existing_filled_zone_report(_FakeBoard(zones=[])) is None


def test_report_none_for_keepout_only_zone():
    # A KiCad `(zone ... (keepout ...))` rule area -- both backends DO
    # already honor these; only copper-pour zones are the P4-06 gap.
    board = _FakeBoard(zones=[_FakeZone(net_name="", layer="F.Cu", is_filled=False, keepout=object())])
    assert routing_toolset._pre_existing_filled_zone_report(board) is None


def test_report_none_for_unfilled_zone_definition():
    # A zone was defined (e.g. queued via ZoneGenerator) but never actually
    # filled -- no real copper exists yet, so nothing to route across.
    board = _FakeBoard(zones=[_FakeZone(net_name="GND", layer="F.Cu", is_filled=False, filled_polygons=[])])
    assert routing_toolset._pre_existing_filled_zone_report(board) is None


def test_report_detects_filled_pour():
    board = _FakeBoard(
        zones=[
            _FakeZone(net_name="GND", layer="F.Cu", is_filled=True, filled_polygons=[[(0.0, 0.0)]], priority=1),
            _FakeZone(net_name="", layer="F.Cu", is_filled=False, keepout=object()),  # ignored
        ]
    )
    report = routing_toolset._pre_existing_filled_zone_report(board)
    assert report is not None
    assert report["filled_zone_count"] == 1
    assert report["filled_zones"] == [{"net": "GND", "layer": "F.Cu", "priority": 1}]


def test_report_on_the_real_zone_obstacle_fixture():
    from kicad_tools.schema import PCB

    board = PCB.load(ZONE_PCB)
    report = routing_toolset._pre_existing_filled_zone_report(board)
    assert report is not None
    assert report["filled_zone_count"] == 1
    assert report["filled_zones"][0]["net"] == "GND"
    assert report["filled_zones"][0]["layer"] == "F.Cu"


def test_report_none_on_the_plain_no_zone_fixture():
    from kicad_tools.schema import PCB

    board = PCB.load(NO_ZONE_PCB)
    assert routing_toolset._pre_existing_filled_zone_report(board) is None


# ---------------------------------------------------------------------------
# propose_route_with_retry: refuses by default, for BOTH backends, before
# either backend is ever dispatched (so this needs no Freerouting/KRT
# install at all -- the guard runs right after `PCB.load`).
# ---------------------------------------------------------------------------


def test_propose_route_refuses_with_pre_existing_filled_zone(tmp_path):
    pcb = _copy_fixture(ZONE_PCB, tmp_path)
    tools = _tools()

    result = tools["propose_route_with_retry"](str(pcb))  # default backend="freerouting"

    assert result["status"] == "incomplete", result
    assert "P4-06" in result["reason"]
    assert result["filled_zone_count"] == 1
    assert result["filled_zones"] == [{"net": "GND", "layer": "F.Cu", "priority": 1}]


def test_propose_route_refuses_for_kicad_routing_tools_backend_too(tmp_path):
    pcb = _copy_fixture(ZONE_PCB, tmp_path)
    tools = _tools()

    result = tools["propose_route_with_retry"](str(pcb), backend="kicad_routing_tools")

    assert result["status"] == "incomplete", result
    assert "P4-06" in result["reason"]
    assert result["filled_zone_count"] == 1


def _stub_out_freerouting_subprocess(monkeypatch) -> None:
    """Force a deterministic, dependency-free failure *past* DSN export
    (which needs only `kicad_tools`, already a hard dependency) so these
    guard tests never depend on whether this machine happens to have a
    real Freerouting jar configured (`test_routing_toolset.py`'s own
    end-to-end tests cover the real subprocess separately)."""

    def _stub(*args, **kwargs):
        raise routing_toolset.FreeroutingError("stubbed: no real Freerouting subprocess in this guard test")

    monkeypatch.setattr(routing_toolset, "run_freerouting", _stub)


def test_propose_route_does_not_refuse_without_zones(tmp_path, monkeypatch):
    # Guard-only check: must not trip on a board with no zones. Runs the
    # real freerouting path far enough (real DSN export) to prove the guard
    # let it through, then fails deterministically at the stubbed
    # subprocess step -- never at the P4-06 check.
    _stub_out_freerouting_subprocess(monkeypatch)
    pcb = _copy_fixture(NO_ZONE_PCB, tmp_path)
    tools = _tools()

    result = tools["propose_route_with_retry"](str(pcb))  # default backend="freerouting"

    assert result["status"] == "incomplete"
    assert "P4-06" not in result["reason"]
    assert result["reason"] == "no attempt produced a usable route"
    assert "stubbed" in result["attempts"][0]["error"]


def test_propose_route_acknowledged_lets_execution_proceed_past_the_guard(tmp_path, monkeypatch):
    # With the explicit opt-in, routing is allowed to proceed past the P4-06
    # guard -- proven the same dependency-free way: it reaches the real
    # DSN-export step and only then fails, at the stubbed subprocess, never
    # at the zone guard.
    _stub_out_freerouting_subprocess(monkeypatch)
    pcb = _copy_fixture(ZONE_PCB, tmp_path)
    tools = _tools()

    result = tools["propose_route_with_retry"](str(pcb), acknowledge_unmodeled_zone_risk=True)

    assert result["status"] == "incomplete"
    assert "P4-06" not in result["reason"]
    assert result["reason"] == "no attempt produced a usable route"
    assert "stubbed" in result["attempts"][0]["error"]


# ---------------------------------------------------------------------------
# Real end-to-end reproduction, gated on a real KiCadRoutingTools install
# (same pattern as test_routing_krt.py's TestRealKicadRoutingTools) --
# proves the guard's warning describes a real, not hypothetical, failure.
# ---------------------------------------------------------------------------

@pytest.mark.needs_krt  # skip with the probe's reason, or fail in the gate run (build ticket 53)
class TestRealZoneObstacleReproduction:
    def test_acknowledged_route_reproduces_a_real_short_against_the_zone(self, tmp_path):
        pcb = _copy_fixture(ZONE_PCB, tmp_path)
        tools = _tools()

        result = tools["propose_route_with_retry"](
            str(pcb),
            backend="kicad_routing_tools",
            escalation="board",
            acknowledge_unmodeled_zone_risk=True,
        )

        assert result["status"] == "pending_confirmation", result
        assert "zone_obstacle_warning" in result
        assert "P4-06" in result["zone_obstacle_warning"]

        # The real finding this guard exists for: KRT's own obstacle map
        # never blocked the GND pour, so its best attempt is expected to
        # short LED_ANODE straight onto it -- real kicad-cli DRC, not the
        # router's own log.
        best_drc = result["best_attempt_drc_summary"]
        print(f"zone-obstacle repro: {best_drc}")
        assert best_drc["violation_count"] > 0, (
            "expected a real DRC violation from routing across the pre-existing GND pour; "
            f"got a clean result -- either KRT has since started respecting zones (great, but "
            f"then this guard's default should be relaxed) or this fixture stopped reproducing it: {result}"
        )
