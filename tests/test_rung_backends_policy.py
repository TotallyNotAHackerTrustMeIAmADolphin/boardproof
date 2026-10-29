"""Delta review of ticket 06 at the adapters: timeouts by exception type, router time
cross-checked against measured wall time."""

from __future__ import annotations

import time
import types
from pathlib import Path

import pytest

from kicad_mcp import rung_backends
from kicad_mcp.routers import kicad_routing_tools as krt_tools
from kicad_mcp.toolsets import routing as routing_toolset

from test_rung_backends import KRT_BASELINE, stress_board  # noqa: F401
from test_rung_backends_review import FakeRouter, _record, _run, fake  # noqa: F401
from test_router_timeout_classes import _FakePopen


def test_the_timeout_subclass_is_a_timeout_with_the_exceptions_elapsed(stress_board, tmp_path, fake):  # noqa: F811
    error = krt_tools.KicadRoutingToolsTimeout("killed")
    error.timeout, error.elapsed_s = 7, 7.4
    fake(error=error)
    outcome = _run(stress_board, tmp_path)
    assert (outcome["status"], outcome["cause"]) == ("timeout", "router-timeout")
    assert outcome["router_elapsed_s"] == 7.4


def test_a_plain_error_with_the_old_prefix_is_no_longer_a_timeout(stress_board, tmp_path, fake):  # noqa: F811
    fake(error=krt_tools.KicadRoutingToolsError("KiCadRoutingTools timed out after 7s (process killed)."))
    outcome = _run(stress_board, tmp_path)
    assert outcome["status"] == "failure" and outcome["cause"] == "router-failure"


def test_a_freerouting_timeout_subclass_maps_to_timeout(stress_board, tmp_path, monkeypatch):  # noqa: F811
    def hang(dsn, ses, **kwargs):
        error = routing_toolset.FreeroutingTimeout("killed")
        error.timeout, error.elapsed_s = 7, 7.1
        raise error

    monkeypatch.setattr(routing_toolset, "run_freerouting", hang)
    monkeypatch.setattr(rung_backends, "_freerouting_ready", lambda: None)
    (tmp_path / "a").mkdir()
    params = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None}
    outcome = rung_backends.freerouting_backend()(stress_board, params, 7.0, tmp_path / "a" / "router.log")
    assert (outcome["status"], outcome["cause"]) == ("timeout", "router-timeout")
    assert outcome["router_elapsed_s"] == 7.1


def test_a_freerouting_error_with_the_old_prefix_is_a_failure(stress_board, tmp_path, monkeypatch):  # noqa: F811
    def boom(dsn, ses, **kwargs):
        raise routing_toolset.FreeroutingError("Freerouting timed out after 7s: look-alike")

    monkeypatch.setattr(routing_toolset, "run_freerouting", boom)
    monkeypatch.setattr(rung_backends, "_freerouting_ready", lambda: None)
    (tmp_path / "a").mkdir()
    params = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None}
    outcome = rung_backends.freerouting_backend()(stress_board, params, 7.0, tmp_path / "a" / "router.log")
    assert outcome["status"] == "failure"


def test_reported_router_time_is_cross_checked_against_measured_wall_time(stress_board, tmp_path, monkeypatch):  # noqa: F811
    """The router (or its wrapper) says 0.001 s; the call really took about 0.4 s.
    The cap is charged with the larger of the two.

    Build ticket 54: the call's real duration is measured inside the fake router on
    ``time.monotonic``, the clock the adapter's cross-check reads. The adapter's interval
    encloses this one on the same clock, so its figure can never be smaller. A fixed
    ``>= 0.4`` floor flaked: on Python 3.12 / Windows ``time.monotonic`` is GetTickCount64
    (15.625 ms ticks) and read 0.39 s around ``time.sleep(0.4)`` in 15 of 40 runs."""
    inner: dict = {}

    def slow(board, routed, **kwargs):
        import shutil

        started = time.monotonic()
        time.sleep(0.4)
        Path(routed).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(board, routed)
        inner["elapsed"] = time.monotonic() - started
        return _record(0, elapsed_s=0.001)

    monkeypatch.setattr(krt_tools, "run_kicad_routing_tools", slow)
    outcome = _run(stress_board, tmp_path)
    assert outcome["status"] == "ok"
    assert inner["elapsed"] > 0.2  # the call really was slow, far above the reported 0.001 s
    assert outcome["router_elapsed_s"] >= inner["elapsed"]


def test_the_krt_wrapper_times_a_killed_run_on_a_clock_that_cannot_step(tmp_path, monkeypatch):
    """Build ticket 54: on a timeout the adapter charges the cap with the wrapper's own
    ``elapsed_s`` (no cross-check on that path), so the wrapper must not time the run on
    the adjustable wall clock. Here the wall clock is stepped back 3 s (an NTP correction)
    while the router runs for 5.7 s against ``timeout=5``; the charged time must be the
    5.7 s. A wall-clock wrapper reports 2.7 s, which the runner's sanity bound
    (0 <= reported <= its own monotonic measurement) accepts, so the cap would be
    under-charged by 3 s. Build ticket 57: the run is deliberately longer than the timeout,
    so a wrapper that charges the configured 5 s instead of the measured time fails too."""
    clocks = {"wall": 1_000_000.0, "mono": 100.0}
    monkeypatch.setattr(krt_tools, "time", types.SimpleNamespace(
        time=lambda: clocks["wall"], monotonic=lambda: clocks["mono"], sleep=lambda s: None,
    ))

    class HangingRouter(_FakePopen):
        stepped = False

        def wait(self, timeout=None):
            if not self.stepped:  # the first wait is the run; _reap waits again after the kill
                self.stepped = True
                clocks["wall"] += 5.7 - 3.0  # 5.7 s pass, but the wall clock is stepped back 3 s
                clocks["mono"] += 5.7  # the router really ran for 5.7 s (killed after the 5 s cap)
            return super().wait(timeout)

    pcb = tmp_path / "in" / "b.kicad_pcb"
    pcb.parent.mkdir()
    pcb.write_text("(kicad_pcb)")
    monkeypatch.setattr(krt_tools, "find_kicad_routing_tools", lambda: tmp_path)
    monkeypatch.setattr(krt_tools, "find_kicad_routing_tools_python", lambda: Path("python"))
    monkeypatch.setattr(krt_tools.subprocess, "Popen", HangingRouter("routing...\n", None))
    with pytest.raises(krt_tools.KicadRoutingToolsTimeout) as caught:
        krt_tools.run_router_script(
            "route.py", [], output_pcb=tmp_path / "out" / "b.kicad_pcb", original_pcb=pcb, timeout=5
        )
    assert caught.value.timeout == 5
    assert caught.value.elapsed_s == pytest.approx(5.7, abs=1e-9)
