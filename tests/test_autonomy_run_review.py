"""Review round for ticket 06 at the CLI wiring: every argument the CLI hands the runner is
pinned by a test that goes red if it is dropped, and the "official" label cannot be reached
through a free-form rung, a foreign runs root or a missing refusal."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp import autonomy_run, rung_backends, rung_runner

from test_input_prep import _build_fixture, GOLDEN_DIR, GOLDEN_NAME

_EMPTY = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}
OK_INFO = {"available": True, "reason": None, "versions": {}}


def _drc(path, **_kwargs):
    return _EMPTY


def _ok_backend(board, params, timeout_s, log):
    return {"status": "ok", "cause": None}


@pytest.fixture
def board(tmp_path):
    return _build_fixture(tmp_path / "proj")


def _measure(board, tmp_path, **kw):
    kwargs = dict(rung="fixture", backend_name="krt", board=board, runs_root=tmp_path / "runs",
                  parity="required", drc=_drc, availability=OK_INFO, backend=_ok_backend)
    kwargs.update(kw)
    return autonomy_run.run_measurement(**kwargs)


def _reasons(outcome):
    return outcome["report"]["unofficial_reasons"]


# --- arguments the CLI hands the runner ----------------------------------------------------------


def test_invariants_are_wired_so_a_router_that_moves_a_footprint_cannot_pass(board, tmp_path):
    def mover(working, params, timeout_s, log):
        from kicad_tools.schema import PCB

        pcb = PCB.load(working)
        footprint = next(c for c in pcb._sexp.children if c.name == "footprint")
        at = footprint.find("at")
        at.set_value(0, float(at.values[0]) + 2.0)
        write_layer.save_pcb(pcb, working)
        return {"status": "ok", "cause": None}

    report = _measure(board, tmp_path, backend=mover)["report"]
    assert [a["status"] for a in report["attempts"]] == ["FAIL"] * 5
    assert {a["cause"] for a in report["attempts"]} == {"invariant-violated"}
    assert not any("no invariants check" in r for r in report["unofficial_reasons"])


def test_the_gate_is_bound_to_the_pinned_baseline(board, tmp_path):
    report = _measure(board, tmp_path)["report"]
    # A gate built with baseline=None answers INCOMPLETE ("rules not pinned") for a clean board.
    assert [a["status"] for a in report["attempts"]] == ["PASS"] * 5
    manifest = json.loads((Path(report["run_dir"]) / "manifest.json").read_text())
    assert manifest["baseline"]  # the pinned rules are frozen in the manifest


def test_budget_and_cap_come_from_the_protocol_not_from_defaults_in_the_caller(board, tmp_path):
    report = _measure(board, tmp_path)["report"]
    protocol = rung_runner.load_protocol()
    assert report["budget_s"] == protocol["budget_s"] == 1800
    manifest = json.loads((Path(report["run_dir"]) / "manifest.json").read_text())
    assert manifest["attempt_timeout_s"] == protocol["attempt_timeout_s"] == 300


def test_a_router_version_mismatch_makes_the_batch_unofficial(board, tmp_path):
    info = {**OK_INFO, "version_check": {"matches": False, "message": "router version 0.23.0 differs"}}
    outcome = _measure(board, tmp_path, availability=info)
    assert any("0.23.0" in r for r in _reasons(outcome))
    assert outcome["report"]["autonomous_pass"] is False


def test_the_availability_probe_runs_once_per_measurement(board, tmp_path, monkeypatch):
    calls = []

    def probe(name):
        calls.append(name)
        return {"backend": name, "available": True, "reason": None, "versions": {}}

    monkeypatch.setattr(rung_backends, "availability", probe)
    monkeypatch.setattr(rung_backends, "krt_availability", lambda: probe("krt"))
    _measure(board, tmp_path, availability=None)
    assert calls == ["krt"]


# --- "official" cannot be reached through the rung label or the runs root --------------------------


@pytest.mark.parametrize("bad", ["../x", "a/b", "a\\b", "", "..", "x" * 80])
def test_a_rung_label_that_is_not_a_plain_name_is_refused_before_anything_is_written(board, tmp_path, bad):
    outcome = _measure(board, tmp_path, rung=bad)
    assert outcome["status"] == "SETUP_FAILED"
    assert "rung" in outcome["reason"]
    assert not list((tmp_path / "runs").rglob("manifest.json"))


def test_the_default_runs_root_is_anchored_to_the_repository_not_the_cwd():
    root = Path(rung_runner.__file__).resolve().parents[2]
    assert autonomy_run.DEFAULT_RUNS_ROOT == root / "runs"
    assert autonomy_run.DEFAULT_RUNS_ROOT.is_absolute()


def test_a_foreign_runs_root_makes_the_batch_unofficial(board, tmp_path, monkeypatch):
    assert any("runs root" in r for r in _reasons(_measure(board, tmp_path)))
    monkeypatch.setattr(autonomy_run, "DEFAULT_RUNS_ROOT", tmp_path / "runs")
    assert not any("runs root" in r for r in _reasons(_measure(board, tmp_path)))


def test_a_batch_in_the_default_runs_root_is_official_and_any_other_root_is_not(board, tmp_path, monkeypatch):
    """The repo-default root is the local, git-ignored ``runs/`` folder (the committed history file
    carries the earlier protocols): a batch run into it is judged official on this point, a batch run
    into any other root, a sibling or a path that only looks like it, is unofficial."""
    default = tmp_path / "repo" / "runs"
    monkeypatch.setattr(autonomy_run, "DEFAULT_RUNS_ROOT", default)
    assert not any("runs root" in r for r in _reasons(_measure(board, tmp_path, runs_root=default)))
    assert not any("runs root" in r for r in _reasons(
        _measure(board, tmp_path, runs_root=tmp_path / "repo" / "x" / ".." / "runs")))
    for other in (tmp_path / "repo" / "runs2", tmp_path / "repo" / ".scratch" / "pivot-ladder" / "runs",
                  tmp_path / "elsewhere" / "runs"):
        assert any("runs root" in r for r in _reasons(_measure(board, tmp_path, runs_root=other))), other


def test_a_rung_without_a_pinned_fixture_is_unofficial(board, tmp_path):
    rungs = rung_runner.load_protocol()["rungs"]
    assert rungs["2"]["board_sha256"] == (  # protocol 5 (build ticket 67)
        "30fa9aecaece938cc535c72ffbe23158bae145fa49484e4fe0bf8d43438ee3aa")
    assert rungs["2"]["project_sha256"] == (
        "4a0326e79d763ee8be59bf5c012d4e3838599609101788c50ddd3f31db9a12d6")
    unpinned = rung_runner.load_protocol()
    unpinned["rungs"]["1"]["board_sha256"] = unpinned["rungs"]["1"]["project_sha256"] = None
    outcome = _measure(board, tmp_path, rung=1, protocol=unpinned)
    assert any("rung fixture not pinned" in r for r in _reasons(outcome))
    other = _measure(board, tmp_path, rung=2, runs_root=tmp_path / "runs2")  # pinned, but not this board
    assert any("does not match the pinned rung fixture" in r for r in _reasons(other))
    pinned = _measure(board, tmp_path, rung=1, runs_root=tmp_path / "runs3")  # rung 1 is pinned now
    assert not any("rung fixture not pinned" in r for r in _reasons(pinned))
    assert any("does not match the pinned rung fixture" in r for r in _reasons(pinned))


def test_an_unknown_rung_label_is_unofficial(board, tmp_path):
    assert any("not in the protocol" in r for r in _reasons(_measure(board, tmp_path, rung="fixture")))


def test_a_board_that_is_not_the_pinned_rung_fixture_is_unofficial(board, tmp_path):
    protocol = rung_runner.load_protocol()
    protocol["rungs"]["2"]["board_sha256"] = "0" * 64
    protocol["rungs"]["2"]["project_sha256"] = autonomy_run.project_sha(board)
    outcome = _measure(board, tmp_path, rung=2, protocol=protocol)
    assert any("does not match the pinned rung fixture" in r for r in _reasons(outcome))


def test_the_pinned_rung_fixture_is_accepted(board, tmp_path):
    protocol = rung_runner.load_protocol()
    protocol["rungs"]["2"]["board_sha256"] = autonomy_run.board_sha(board)
    protocol["rungs"]["2"]["project_sha256"] = autonomy_run.project_sha(board)
    outcome = _measure(board, tmp_path, rung=2, protocol=protocol)
    assert not any("rung fixture" in r for r in _reasons(outcome))


# --- (6) a refusal is a setup status, not five failed attempts ---------------------------------------


def test_a_board_with_a_filled_pour_is_refused_not_scored(tmp_path):
    project = tmp_path / "zone"
    shutil.copytree(Path(__file__).parent / "fixtures" / "zone_obstacle", project)
    outcome = _measure(project / "zone_obstacle.kicad_pcb", tmp_path, parity="not_applicable",
                       parity_reason="fixture has no schematic")
    assert outcome["status"] == "REFUSED"
    report = outcome["report"]
    assert (report["attempts_run"], report["attempts_planned"]) == (0, 5)
    assert not list((tmp_path / "runs").rglob("manifest.json"))
    lines = [json.loads(x) for x in (tmp_path / "runs" / "index.jsonl").read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["refused"]
    assert autonomy_run._exit_code(outcome) == autonomy_run.EXIT_REFUSED


# --- (5) the input floor is visible everywhere -------------------------------------------------------------

_VIOLATION = {
    "type": "clearance", "severity": "error", "description": "Clearance violation",
    "items": [{"uuid": "u1", "description": "Track", "pos": {"x": 1.0, "y": 2.0}}],
}


def test_a_failing_input_floor_labels_report_summary_and_index(board, tmp_path):
    def drc(path, **_kwargs):
        return {**_EMPTY, "violations": [_VIOLATION]}

    outcome = _measure(board, tmp_path, drc=drc)
    report = outcome["report"]
    assert report["input_fail_floor"] is True
    floor = report["input_floor"]
    assert floor["grading_status"] == "FAIL" and floor["certified"] is False
    assert floor["error_count"] == 1
    assert "unhandled" in floor and "router_text_check_ok" in floor
    summary = autonomy_run._summary(outcome, "fixture", "krt")
    assert summary["input_fail_floor"] is True
    assert summary["input_floor"]["error_count"] == 1
    assert "input_fail_floor" in summary["rung_result"]


def test_the_nothing_to_do_branch_carries_the_same_floor_fields(tmp_path):
    project = tmp_path / "golden"
    shutil.copytree(GOLDEN_DIR, project)
    outcome = _measure(project / GOLDEN_NAME, tmp_path)
    floor = outcome["report"]["input_floor"]
    assert outcome["report"]["setup"]["input_preparation"]["status"] == "nothing_to_do"
    assert {"grading_status", "certified", "unhandled", "router_text_check_ok", "error_count"} <= set(floor)
    assert floor["certified"] is False


def test_two_measurements_in_the_same_second_get_distinct_prepared_directories(board, tmp_path, monkeypatch):
    from datetime import datetime as real

    class Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return real(2026, 10, 11, 12, 0, 0, tzinfo=tz)

    monkeypatch.setattr(autonomy_run, "datetime", Frozen)
    first = _measure(board, tmp_path)
    second = _measure(board, tmp_path)
    assert first["status"] == "RAN" and second["status"] == "RAN", second
    prepared = [p for p in (tmp_path / "runs" / "_prepared").iterdir()]
    assert len(prepared) == 2
