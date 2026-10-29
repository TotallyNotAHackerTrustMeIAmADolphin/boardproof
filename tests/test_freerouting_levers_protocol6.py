"""Build ticket 109: the Freerouting levers of protocol 6 (fanout off, neck width).

Ticket 107 measured them on a scratch copy of rung 1 (Freerouting 2.5.0):
``--router.fanout.enabled=false`` removes the unneeded 3/4 micro neck-down of the fanout stage;
``--router.neck_width_um=200`` gives the router a legal neck for the 0.2 mm pad entry of U1 pin 4
(127 re-applies the 3/4 neck: 0.0952 mm tracks, two ``track_width`` errors and a clearance error).

They reach Freerouting only through the wrapper's allow-list (``run_freerouting`` builds the
command line from a fixed map); nothing writes ``freerouting.json`` by hand.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kicad_mcp import rung_backends, rung_runner
from kicad_mcp.toolsets import routing as routing_toolset


def _run(tmp_path, monkeypatch, **kwargs):
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = list(args)
        Path(args[args.index("-do") + 1]).write_text("(session)")
        return subprocess.CompletedProcess(args, 0, b"final score: 1.0 (0 unrouted and 0 violations)", b"")

    monkeypatch.setattr(routing_toolset.subprocess, "run", fake_run)
    routing_toolset.run_freerouting(tmp_path / "a.dsn", tmp_path / "a.ses", timeout=4, **kwargs)
    return seen["args"]


def test_both_levers_reach_the_command_line_through_the_allow_list(tmp_path, monkeypatch):
    args = _run(tmp_path, monkeypatch, fanout_enabled=False, neck_width_um=200)
    assert "--router.fanout.enabled=false" in args
    assert "--router.neck_width_um=200" in args


def test_without_the_levers_the_command_line_has_no_router_option(tmp_path, monkeypatch):
    args = _run(tmp_path, monkeypatch)
    assert not [a for a in args if a.startswith("--router.")]


@pytest.mark.parametrize("kwargs", [
    {"fanout_enabled": True},       # the router's default: not a lever value
    {"fanout_enabled": "false"},
    {"fanout_enabled": 0},
    {"neck_width_um": 0},
    {"neck_width_um": -5},
    {"neck_width_um": True},
    {"neck_width_um": 200.5},
    {"neck_width_um": "200; rm"},
    {"neck_width_um": 100000},
])
def test_a_value_outside_the_allow_list_is_refused_before_any_process_starts(tmp_path, monkeypatch, kwargs):
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)
    started = []
    monkeypatch.setattr(routing_toolset.subprocess, "run", lambda *a, **k: started.append(a))
    with pytest.raises(routing_toolset.FreeroutingError):
        routing_toolset.run_freerouting(tmp_path / "a.dsn", tmp_path / "a.ses", timeout=4, **kwargs)
    assert started == []


def test_no_code_path_writes_freerouting_json():
    src = Path(routing_toolset.__file__).read_text(encoding="utf-8")
    assert "write_text" not in src.split("def freerouting_sandbox", 1)[1].split("def decode_jvm_output", 1)[0]


# --- the protocol's lever table ---------------------------------------------------------------


def test_protocol_6_declares_the_levers_with_their_measured_effect():
    protocol = rung_runner.load_protocol()
    fr = protocol["backends"]["freerouting"]
    assert fr["levers"]["fanout_enabled"] == [None, False]
    assert fr["levers"]["neck_width_um"] == [None, 200]
    notes = fr["lever_notes"]
    assert "0.0952" in notes["neck_width_um"] and "127" in notes["neck_width_um"]
    assert "200" in notes["neck_width_um"] and "pass" in notes["neck_width_um"].lower()
    assert "3/4" in notes["fanout_enabled"]
    for entry in fr["plan"]:  # the plan is made of declared lever values only
        assert rung_runner.plan_problems(entry, fr["levers"]) == []
        assert entry["parameters"]["fanout_enabled"] is False and entry["parameters"]["neck_width_um"] == 200


def test_the_backend_passes_the_levers_only_when_they_are_set(tmp_path, monkeypatch):
    seen = {}

    def fake_freerouting(dsn, ses, **kwargs):
        seen.update(kwargs)
        error = routing_toolset.FreeroutingTimeout("t")
        error.timeout, error.elapsed_s = 7, 7.0
        raise error

    monkeypatch.setattr(routing_toolset, "run_freerouting", fake_freerouting)
    monkeypatch.setattr(rung_backends, "_freerouting_ready", lambda: None)
    import shutil
    board = tmp_path / "b.kicad_pcb"
    shutil.copy(Path(__file__).parent / "fixtures" / "simple_led.kicad_pcb", board)
    (tmp_path / "a").mkdir()
    params = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None,
              "fanout_enabled": False, "neck_width_um": 200}
    rung_backends.freerouting_backend()(board, params, 7.0, tmp_path / "a" / "router.log")
    assert seen["fanout_enabled"] is False and seen["neck_width_um"] == 200
