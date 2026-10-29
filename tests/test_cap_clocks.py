"""Build ticket 57: every duration charged to the time cap is timed on a clock that cannot step.

Each test stands a ``SteppedClocks`` in for one module's ``time``: while the router "runs",
the monotonic clock moves forward by what the run really took and the wall clock
(``time.time``) is stepped back meanwhile, as an NTP correction would do. The charged figure
must be the monotonic one. Reverting the timer under test to ``time.time()`` makes exactly
its test fail; the reproduction (all five reverted, suite green) and the per-timer revert
runs are recorded in build ticket 57.

A reverted timer under-charges here (8.5 s for a 12.5 s run), which the runner's sanity bound
(``0 <= reported <= its own measurement``) would accept: nothing downstream catches it.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

from kicad_mcp import rung_backends, rung_runner
from kicad_mcp.toolsets import routing as routing_toolset
from kicad_mcp.routers import kicad_routing_tools as krt_tools

from test_rung_backends import stress_board  # noqa: F401
from test_rung_backends_review import FakeRouter, _run
from test_rung_runner import PASS, fail_result, make_run, project  # noqa: F401

RAN_S = 12.5  # what the router really took, on the monotonic clock
STEPPED_BACK_S = 4.0  # the wall clock is corrected back meanwhile, so it reads 8.5 s
FREEROUTING_PARAMS = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None}


class SteppedClocks:
    """Stands in for a module's ``time``. ``time()`` is the adjustable wall clock,
    ``monotonic()`` the one that cannot step. Anything else is the real ``time``."""

    def __init__(self) -> None:
        self.wall, self.mono = 1_800_000_000.0, 5_000.0

    def time(self) -> float:
        return self.wall

    def monotonic(self) -> float:
        return self.mono

    def run(self, seconds: float = RAN_S, stepped_back: float = STEPPED_BACK_S) -> None:
        self.mono += seconds
        self.wall += seconds - stepped_back

    def __getattr__(self, name: str):
        return getattr(time, name)


@pytest.fixture
def clocks() -> SteppedClocks:
    return SteppedClocks()


# --- rung_backends: the adapters ------------------------------------------------------------


def test_the_krt_adapter_cross_checks_the_wrapper_on_the_monotonic_clock(
        stress_board, tmp_path, monkeypatch, clocks):  # noqa: F811
    """``krt_backend`` charges max(wrapper's elapsed_s, its own measurement around the call).
    The wrapper under-reports 0.5 s; the adapter's own measurement must be the 12.5 s."""
    router = FakeRouter(sabotage=False, elapsed_s=0.5)

    def call(board, routed, **kwargs):
        record = router(board, routed, **kwargs)
        clocks.run()
        return record

    monkeypatch.setattr(krt_tools, "run_kicad_routing_tools", call)
    monkeypatch.setattr(rung_backends, "time", clocks)
    outcome = _run(stress_board, tmp_path)
    assert outcome["status"] == "ok"
    assert outcome["router_elapsed_s"] == RAN_S


def test_the_freerouting_adapter_times_the_router_on_the_monotonic_clock(
        stress_board, tmp_path, monkeypatch, clocks):  # noqa: F811
    """``freerouting_backend`` charges its own measurement around ``run_freerouting``."""

    def route(dsn, ses, **kwargs):
        clocks.run()
        Path(ses).write_text("(session)", encoding="utf-8")
        return {"freerouting": "self report"}

    monkeypatch.setattr(rung_backends, "_freerouting_ready", lambda: None)
    monkeypatch.setattr(routing_toolset, "export_dsn_with_kicad",
                        lambda board, work: {"dsn_path": str(work), "exporter": "stub"})
    monkeypatch.setattr(routing_toolset, "run_freerouting", route)
    monkeypatch.setattr(routing_toolset, "import_ses_with_kicad", lambda board, ses, work: {
        "trace_specs": [], "via_specs": [], "expected_copper": [], "removed_copper": 0, "import": {}})
    monkeypatch.setattr(routing_toolset, "_apply_specs", lambda board, traces, vias, **kw: {})
    monkeypatch.setattr(rung_backends, "time", clocks)
    (tmp_path / "a").mkdir()
    outcome = rung_backends.freerouting_backend()(
        stress_board, dict(FREEROUTING_PARAMS), 60.0, tmp_path / "a" / "router.log")
    assert outcome["status"] == "ok", outcome
    assert outcome["router_elapsed_s"] == RAN_S


# --- toolsets/routing.py: Freerouting's own timeout figure ----------------------------------


def test_a_killed_freerouting_run_reports_its_monotonic_elapsed_not_the_timeout(
        tmp_path, monkeypatch, clocks):
    """On a timeout the adapter charges the cap with ``FreeroutingTimeout.elapsed_s`` as is,
    so ``run_freerouting`` must time the run on the monotonic clock. The run is killed after
    5.7 s against ``timeout=5``, so charging the configured timeout fails too."""
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)

    def hang(args, **kwargs):
        clocks.run(5.7, 3.0)
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(routing_toolset.subprocess, "run", hang)
    monkeypatch.setattr(routing_toolset, "time", clocks)
    with pytest.raises(routing_toolset.FreeroutingTimeout) as caught:
        routing_toolset.run_freerouting(tmp_path / "a.dsn", tmp_path / "a.ses", timeout=5)
    assert caught.value.timeout == 5
    assert caught.value.elapsed_s == pytest.approx(5.7, abs=1e-9)


# --- rung_runner: the default clock ---------------------------------------------------------


def _one_attempt_run(project, tmp_path, backend):  # noqa: F811
    """``run_rung`` with NO clock injected: the runner's default clock is under test."""
    return rung_runner.run_rung(
        rung=2, backend_name="krt", board=project / "board.kicad_pcb",
        rule_files=[project / "board.kicad_dru"], plan=[{"lever": "grid", "parameters": {}}],
        backend=backend, gate=lambda board: dict(PASS), baseline={"rules": "pinned"},
        tool_versions={"kicad-cli": "9.0.0"}, runs_root=tmp_path / "runs",
        repo_state=lambda: {"commit": "abc123", "dirty": False},
        protocol=rung_runner.load_protocol(), invariants=lambda board: {},
    )


def test_run_rung_times_attempts_and_the_budget_on_the_monotonic_clock_by_default(
        project, tmp_path, monkeypatch, clocks):  # noqa: F811
    def backend(board, params, timeout_s, log):
        clocks.run()
        return {"status": "ok"}  # no router_elapsed_s: the runner's own measurement is charged

    monkeypatch.setattr(rung_runner, "time", clocks)
    report = _one_attempt_run(project, tmp_path, backend)
    attempt = report["attempts"][0]
    assert attempt["router_wall_s"] == RAN_S
    assert attempt["backend_wall_s"] == RAN_S
    assert report["total_wall_s"] == RAN_S


def test_rerun_attempt_times_the_rerun_on_the_monotonic_clock_by_default(
        project, tmp_path, monkeypatch, clocks):  # noqa: F811
    report, _ = make_run(project, tmp_path, gate=lambda board: fail_result())

    def backend(board, params, timeout_s, log):
        clocks.run()
        return {"status": "ok"}

    monkeypatch.setattr(rung_runner, "time", clocks)
    updated = rung_runner.rerun_attempt(report["run_dir"], 2, backend=backend, gate=lambda board: dict(PASS))
    assert updated["unplanned_reruns"][0]["router_wall_s"] == RAN_S
