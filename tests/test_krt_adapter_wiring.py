"""Issue 47: follow-ups from issue 2, with no KiCadRoutingTools install needed.

1. The real ``krt_backend`` adapter, driven through the real runner, carries the gate's unconnected
   net list into the attempt cause and detail, next to the parse gap fact (end to end wiring).
2. The pre-router pad parse is charged to the attempt: it is bounded by the attempt cap, it shrinks
   the router's own timeout, and its wall time counts in ``router_elapsed_s``.
"""

from __future__ import annotations

import pytest
from test_rung_runner import make_run
from test_rung_runner import project as shared_project

from kicad_mcp import rung_backends
from kicad_mcp.routers import kicad_routing_tools as krt_tools
from kicad_mcp.toolsets import routing as routing_toolset

_REGISTERED = (shared_project,)  # the import registers the fixture under this module's name

_NETS = ["/CC1", "GND", "GND", "VBUS"]
_FAIL_GATE = {
    "verdict": "FAIL",
    "unconnected": [{"net": n, "items": [f"pad {i}"]} for i, n in enumerate(_NETS)],
    "violations": [], "warnings": [], "unstable": False, "incomplete_reasons": [],
}
_GAP = [{"code": "KRT_PARSE_GAP", "reference": "J1", "raw_pads": 4, "parsed_pads": 0}]


class FakeTime:
    """Stands in for the ``time`` module of rung_backends; stubs advance it."""

    def __init__(self):
        self.now = 1000.0
        self.monotonic = lambda: self.now


@pytest.fixture
def stubbed_krt(monkeypatch):
    """Every KRT seam the adapter touches replaced; records what the adapter passed."""
    clock = FakeTime()
    seen: dict = {"parse_cost": 0.0, "router_cost": 5.0, "parse_timeout": None, "router_timeout": None}

    def parsed(pcb, timeout=120):
        seen["parse_timeout"] = timeout
        clock.now += seen["parse_cost"]
        return {"J1": [0]}

    def routed(board, out, **kw):
        seen["router_timeout"] = kw["timeout"]
        clock.now += seen["router_cost"]
        return {"exit_code": 0, "elapsed_s": 0.0, "log": {}, "delivered_below_requested": []}

    monkeypatch.setattr(rung_backends, "time", clock)
    monkeypatch.setattr(rung_backends, "_pour_problem", lambda board: None)
    monkeypatch.setattr(krt_tools, "raw_pad_counts", lambda pcb: {"J1": [4]})
    monkeypatch.setattr(krt_tools, "parsed_pad_counts", parsed)
    monkeypatch.setattr(krt_tools, "board_floor_overrides", lambda board: {"floors": {}})
    monkeypatch.setattr(krt_tools, "run_kicad_routing_tools", routed)
    monkeypatch.setattr(krt_tools, "route_delta", lambda a, b: {"trace_specs": [], "via_specs": []})
    monkeypatch.setattr(routing_toolset, "_apply_specs", lambda *a, **k: None)
    return seen


def test_adapter_wiring_carries_the_gate_net_list_into_the_attempt_cause(request, tmp_path, stubbed_krt):
    report, _ = make_run(request.getfixturevalue("shared_project"), tmp_path,
                         backend=rung_backends.krt_backend(), gate=lambda b: dict(_FAIL_GATE))
    attempt = report["attempts"][0]
    assert attempt["status"] == "FAIL"
    assert attempt["cause"] == "unconnected-nets"
    assert attempt["unconnected_nets"] == ["/CC1", "GND", "VBUS"]
    for text in ("/CC1", "GND", "VBUS", "KRT_PARSE_GAP", "J1"):
        assert text in attempt["detail"], (text, attempt["detail"])
    assert attempt["router_facts"]["krt_parse_gap"] == _GAP


def _run_adapter(tmp_path, timeout_s):
    (tmp_path / "a").mkdir()
    params = {"escalation": "board", "grid_step_mm": None, "max_iterations": None, "ordering": None}
    return rung_backends.krt_backend()(tmp_path / "board.kicad_pcb", params, timeout_s, tmp_path / "a" / "router.log")


def test_the_pad_parse_is_bounded_by_the_attempt_cap(tmp_path, stubbed_krt):
    _run_adapter(tmp_path, 30.0)
    assert stubbed_krt["parse_timeout"] == 30  # not the parser's own 120 s default


def test_the_pad_parse_time_shrinks_the_router_timeout_and_counts_in_the_charge(tmp_path, stubbed_krt):
    stubbed_krt["parse_cost"] = 40.0
    outcome = _run_adapter(tmp_path, 100.0)
    assert stubbed_krt["router_timeout"] == 60  # 100 s cap minus the 40 s the parse took
    assert outcome["router_elapsed_s"] == pytest.approx(45.0)  # 40 s parse + 5 s router


def test_a_parse_that_eats_the_whole_cap_is_a_timeout_and_the_router_never_starts(tmp_path, stubbed_krt):
    stubbed_krt["parse_cost"] = 100.0
    outcome = _run_adapter(tmp_path, 100.0)
    assert outcome["status"] == "timeout" and outcome["cause"] == "router-timeout"
    assert outcome["router_elapsed_s"] == pytest.approx(100.0)
    assert stubbed_krt["router_timeout"] is None  # the router was not started


def test_a_router_timeout_also_charges_the_parse_time(tmp_path, stubbed_krt, monkeypatch):
    stubbed_krt["parse_cost"] = 10.0

    def slow(board, out, **kw):
        exc = krt_tools.KicadRoutingToolsTimeout("router timed out")
        exc.timeout, exc.elapsed_s = kw["timeout"], 90.0
        raise exc

    monkeypatch.setattr(krt_tools, "run_kicad_routing_tools", slow)
    outcome = _run_adapter(tmp_path, 100.0)
    assert outcome["status"] == "timeout"
    assert outcome["router_elapsed_s"] == pytest.approx(100.0)  # 10 s parse + 90 s router
    assert outcome["facts"]["krt_parse_gap"] == _GAP  # the parse fact survives the timeout
