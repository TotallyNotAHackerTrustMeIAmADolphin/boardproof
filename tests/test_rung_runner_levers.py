"""Levers, invariants, router facts and backend availability at the runner seam (S3)."""

from __future__ import annotations

import json
from pathlib import Path

from kicad_mcp import rung_runner

from test_rung_runner import PASS, fail_result, load, make_run, project  # noqa: F401


def _invariants(board: Path) -> dict:
    # stand-in for "placement / nets / setup" fingerprints: a fake board is one line of text
    return {"placement": board.read_text().splitlines()[0]}


def test_an_attempt_that_changes_a_declared_invariant_cannot_pass(project, tmp_path):  # noqa: F811
    def backend(board, params, timeout_s, log):
        if params["grid_step_mm"] == 0.05:  # the "finer-grid" attempt moves a footprint
            board.write_text("moved footprint\n")
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend, invariants=_invariants)
    manifest, _ = load(report)
    assert manifest["invariants"] == {"placement": "fake board, version 1"}
    attempt = report["attempts"][1]
    assert attempt["status"] == "FAIL"
    assert attempt["cause"] == "invariant-violated"
    assert attempt["invariant_violations"] == ["placement"]
    assert attempt["gate"]["verdict"] == "PASS"  # the gate saw a clean board; still not a pass
    assert (report["attempts_passed"], report["attempts_run"]) == (3, 5)  # attempts 1 and 4 use grid 0.05
    assert report["assisted"] is False  # a misbehaving router is not a human intervention


def test_invariants_that_cannot_be_computed_are_a_failed_attempt_not_a_skip(project, tmp_path):  # noqa: F811
    calls = {"n": 0}

    def invariants(board):
        calls["n"] += 1
        if calls["n"] == 3:  # frozen, attempt 0, then attempt 1 explodes
            raise RuntimeError("parse error")
        return {"placement": "same"}

    report, _ = make_run(project, tmp_path, invariants=invariants)
    attempt = report["attempts"][1]
    assert attempt["status"] == "INCOMPLETE"
    assert attempt["cause"] == "invariant-violated"
    assert "parse error" in attempt["detail"]
    assert report["attempts_run"] == 5


def test_router_facts_are_recorded_but_the_routers_own_score_never_decides(project, tmp_path):  # noqa: F811
    def backend(board, params, timeout_s, log):
        return {"status": "ok",
                "facts": {"self_reported_unrouted": 0, "self_reported_score": 0.0, "exit_code": 0}}

    results = iter([fail_result(("A", "B"), 2), fail_result(("A",), 1)] + [fail_result(("A",), 5)] * 3)
    report, _ = make_run(project, tmp_path, backend=backend, gate=lambda b: next(results))
    first = report["attempts"][0]
    assert first["router_facts"]["self_reported_unrouted"] == 0
    assert first["status"] == "FAIL"  # router said zero unrouted; the gate said FAIL
    assert report["autonomous_pass"] is False
    assert report["best_attempt"] == 1  # chosen from the gate's lists only (1 net, 1 violation)


def test_unavailable_backend_is_unavailable_for_the_whole_batch_never_a_failed_attempt(tmp_path):
    report = rung_runner.run_unavailable(
        rung=2, backend_name="freerouting", runs_root=tmp_path / "runs",
        reason="FREEROUTING_JAR_PATH is not set", availability={"java": "found", "jar": None},
        plan=rung_runner.load_protocol()["backends"]["freerouting"]["plan"],
    )
    assert report["status"] == "UNAVAILABLE"
    assert report["attempts_planned"] == 5
    assert (report["attempts_run"], report["attempts_passed"]) == (0, 0)
    assert report["autonomous_pass"] is False
    assert report["attempts"] == []  # no attempt happened, so none is reported as failed
    on_disk = json.loads((Path(report["run_dir"]) / "report.json").read_text())
    assert on_disk == report

    result = rung_runner.rung_result(tmp_path / "runs", 2, "freerouting")
    assert result["status"] == "UNAVAILABLE"
    assert result["reported"] is None
    assert result["batches"] == 0
    assert "UNAVAILABLE" in result["summary"]
    assert "FREEROUTING_JAR_PATH" in result["summary"]


def test_an_unavailable_batch_does_not_lock_the_first_scored_batch(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"
    rung_runner.run_unavailable(rung=2, backend_name="krt", runs_root=runs, reason="no checkout",
                                availability={}, plan=[])
    report, _ = make_run(project, tmp_path, runs_root=runs)
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == report["run_dir"]
    assert result["status"] == "SCORED"
    assert len(result["unavailable_batches"]) == 1


def test_backends_stay_separate_when_one_is_unavailable(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs)
    rung_runner.run_unavailable(rung=2, backend_name="freerouting", runs_root=runs,
                                reason="no jar", availability={}, plan=[])
    results = rung_runner.rung_results(runs, 2)
    assert sorted(results) == ["freerouting", "krt"]
    assert results["krt"]["status"] == "SCORED"
    assert results["freerouting"]["status"] == "UNAVAILABLE"
