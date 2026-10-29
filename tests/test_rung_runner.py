"""Rung runner, tested only at seam S3: manifest, report and index.

A fake backend and a fake gate boundary stand in for the router and
``verify_board``; every expected value is a literal from the protocol
(pivot-ladder ticket 6), not recomputed.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest

from kicad_mcp import rung_runner

BOARD_TEXT = "fake board, version 1\n"
RULES_TEXT = "fake rules, version 1\n"
# sha256 literals of the two fixture strings above, computed once with an external tool.
BOARD_SHA = hashlib.sha256(BOARD_TEXT.encode()).hexdigest()
RULES_SHA = hashlib.sha256(RULES_TEXT.encode()).hexdigest()

PASS = {"verdict": "PASS", "unconnected": [], "violations": [], "warnings": [],
        "unstable": False, "incomplete_reasons": []}


def fail_result(nets=("GND", "VCC"), violations=3):
    return {
        "verdict": "FAIL",
        "unconnected": [{"net": n, "items": [f"pad {n}-1", f"pad {n}-2"]} for n in nets],
        "violations": [{"type": "clearance", "description": f"v{i}"} for i in range(violations)],
        "warnings": [], "unstable": False, "incomplete_reasons": [],
    }


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


@pytest.fixture
def project(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "board.kicad_pcb").write_bytes(BOARD_TEXT.encode())
    (src / "board.kicad_dru").write_bytes(RULES_TEXT.encode())
    return src


def make_run(project, tmp_path, *, backend=None, gate=None, plan=None, clock=None, **kw):
    clock = clock or Clock()
    plan = plan if plan is not None else rung_runner.load_protocol()["backends"]["krt"]["plan"]

    def default_backend(board, params, timeout_s, log):
        clock.t += 10
        return {"status": "ok"}

    kwargs = dict(
        rung=2,
        backend_name="krt",
        board=project / "board.kicad_pcb",
        rule_files=[project / "board.kicad_dru"],
        plan=plan,
        backend=backend or default_backend,
        gate=gate or (lambda board: dict(PASS)),
        baseline={"rules": "pinned"},
        tool_versions={"kicad-cli": "9.0.0"},
        runs_root=tmp_path / "runs",
        repo_state=lambda: {"commit": "abc123", "dirty": False},
        clock=clock,
        protocol=rung_runner.load_protocol(),
        invariants=lambda board: {},
    )
    kwargs.update(kw)
    return rung_runner.run_rung(**kwargs), clock


def load(report):
    run_dir = Path(report["run_dir"])
    return (json.loads((run_dir / "manifest.json").read_text()),
            json.loads((run_dir / "report.json").read_text()))


def test_manifest_is_written_before_first_attempt_and_report_is_complete(project, tmp_path):
    seen = {}
    clock = Clock()

    def backend(board, params, timeout_s, log):
        clock.t += 10
        run_dir = log.parent.parent.parent
        if "manifest" not in seen:
            seen["manifest"] = json.loads((run_dir / "manifest.json").read_text())
            seen["report_exists"] = (run_dir / "report.json").exists()
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend, clock=clock)
    manifest, on_disk = load(report)

    assert seen["report_exists"] is False
    assert seen["manifest"] == manifest  # same content before and after the run
    assert manifest["protocol_version"] == rung_runner.PROTOCOL_VERSION
    assert manifest["input"]["board_sha256"] == BOARD_SHA
    assert manifest["input"]["rule_files"] == {"board.kicad_dru": RULES_SHA}
    assert manifest["baseline"] == {"rules": "pinned"}
    assert manifest["tool_versions"] == {"kicad-cli": "9.0.0"}
    assert manifest["repo"] == {"commit": "abc123", "dirty": False}
    assert manifest["attempts_planned"] == 5
    assert [a["lever"] for a in manifest["plan"]] == [
        "baseline", "finer-grid", "more-iterations", "ordering-inside-out",
        "finer-grid-more-iterations-inside-out",
    ]

    assert on_disk == report
    assert (report["attempts_passed"], report["attempts_run"], report["attempts_planned"]) == (5, 5, 5)
    assert report["autonomous_pass"] is True
    assert report["assisted"] is False
    assert report["manifest_sha"] == hashlib.sha256(
        (Path(report["run_dir"]) / "manifest.json").read_bytes()).hexdigest()
    first = report["attempts"][0]
    assert first["index"] == 0 and first["lever"] == "baseline"
    assert first["parameters"] == {
        "escalation": "board", "grid_step_mm": None, "max_iterations": None, "ordering": None,
    }
    assert first["status"] == "PASS"
    assert first["gate"]["verdict"] == "PASS"
    assert first["router_wall_s"] == 10.0


def test_every_attempt_starts_from_a_fresh_copy_of_the_frozen_input(project, tmp_path):
    starts = []

    def backend(board, params, timeout_s, log):
        starts.append(hashlib.sha256(board.read_bytes()).hexdigest())
        board.write_text("routed garbage from this attempt")
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend)
    assert starts == [BOARD_SHA] * 5
    assert report["assisted"] is False
    assert report["input_board_sha256"] == BOARD_SHA


def test_attempts_not_started_before_the_budget_expires_are_not_run_and_stay_in_the_denominator(
        project, tmp_path):
    clock = Clock()

    def backend(board, params, timeout_s, log):
        clock.t += 280
        return {"status": "ok"}

    def gate(board):
        clock.t += 400  # 680 s per attempt including grading
        return dict(PASS)

    report, _ = make_run(project, tmp_path, backend=backend, gate=gate, clock=clock)

    # Attempts start at 0, 680, 1360; the third finishes at 2040, past the 1800 s
    # budget (a failed attempt); then nothing more starts.
    assert (report["attempts_passed"], report["attempts_run"], report["attempts_planned"]) == (2, 3, 5)
    assert [a["status"] for a in report["attempts"]] == ["PASS", "PASS", "TIMEOUT"] + ["NOT_RUN"] * 2
    not_run = report["attempts"][3]
    assert not_run["index"] == 3 and not_run["lever"] == "ordering-inside-out" and not_run["gate"] is None
    assert report["budget_s"] == 1800
    assert report["total_wall_s"] == 2040


def test_a_router_timeout_is_a_failed_attempt_with_a_cause_and_a_complete_gate_result(
        project, tmp_path):
    def backend(board, params, timeout_s, log):
        return {"status": "timeout"}

    report, _ = make_run(project, tmp_path, backend=backend,
                         gate=lambda board: fail_result(),
                         plan=[{"lever": "grid", "parameters": {}}])
    attempt = report["attempts"][0]
    assert attempt["status"] == "TIMEOUT"
    assert attempt["cause"] == "router-timeout"
    assert attempt["gate"]["unconnected"][0]["items"] == ["pad GND-1", "pad GND-2"]
    assert report["attempts_run"] == 1 and report["attempts_passed"] == 0


def test_a_failing_rung_is_as_complete_as_a_passing_one(project, tmp_path):
    results = iter([fail_result(("A",), 1), fail_result(("A", "B", "C"), 4),
                    fail_result(("A", "B"), 2), fail_result(("B",), 9), fail_result(("A",), 1)])

    def backend(board, params, timeout_s, log):
        return {"status": "failure"} if params.get("grid_step_mm") == 0.05 else {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend, gate=lambda b: next(results))
    run_dir = Path(report["run_dir"])

    assert (report["attempts_passed"], report["attempts_run"], report["attempts_planned"]) == (0, 5, 5)
    assert report["autonomous_pass"] is False
    assert [a["status"] for a in report["attempts"]] == ["FAIL"] * 5
    assert report["attempts"][1]["cause"] == "router-failure"
    for a in report["attempts"]:
        assert a["gate"]["violations"] and a["gate"]["unconnected"]
        assert (run_dir / "attempts" / f"{a['index']:02d}" / "gate.json").exists()
    # Coded rule: PASS first, fewest unconnected, fewest errors, lowest index.
    # Attempts 0 and 4 tie at 2 unconnected items and 1 error; the lowest index wins.
    assert report["best_attempt"] == 0


def test_an_incomplete_attempt_never_wins_best_attempt_on_empty_lists(project, tmp_path):
    results = iter([
        {"verdict": "INCOMPLETE", "unconnected": [], "violations": [], "warnings": [],
         "unstable": False, "incomplete_reasons": ["kicad-cli timed out"]},
        fail_result(("A",), 5),
    ])
    report, _ = make_run(project, tmp_path, gate=lambda b: next(results),
                         plan=[{"lever": "a", "parameters": {}}, {"lever": "b", "parameters": {}}])
    assert [a["status"] for a in report["attempts"]] == ["INCOMPLETE", "FAIL"]
    assert report["best_attempt"] == 1


def _assisted_after(project, tmp_path, tamper, **kw):
    """Run five attempts; ``tamper(attempt_index)`` fires inside attempt 2 (index 1)."""
    calls = {"n": 0}

    def backend(board, params, timeout_s, log):
        calls["n"] += 1
        if calls["n"] == 2:
            tamper()
        return {"status": "ok"}

    return make_run(project, tmp_path, backend=backend, **kw)[0]


def test_editing_the_input_board_between_attempts_relabels_the_run_assisted(project, tmp_path):
    report = _assisted_after(
        project, tmp_path,
        lambda: (project / "board.kicad_pcb").write_bytes(b"hand-edited in the GUI\n"))
    assert report["assisted"] is True
    assert report["autonomous_pass"] is False  # a pass obtained this way is not autonomous
    assert any("board.kicad_pcb" in r for r in report["assisted_reasons"])
    assert report["attempts"][1]["human_interventions"]
    # Attempts still ran from the frozen copy and the pass count is still shown.
    assert report["attempts_passed"] == 5


def test_editing_a_rule_file_relabels_the_run_assisted(project, tmp_path):
    report = _assisted_after(
        project, tmp_path,
        lambda: (project / "board.kicad_dru").write_bytes(b"(severity ignore)\n"))
    assert report["assisted"] is True
    assert any("board.kicad_dru" in r for r in report["assisted_reasons"])


def test_changing_the_plan_after_the_manifest_relabels_the_run_assisted_and_runs_the_frozen_plan(
        project, tmp_path):
    plan = [{"lever": "grid", "parameters": {"grid_step": 0.1}} for _ in range(3)]
    used = []

    def backend(board, params, timeout_s, log):
        used.append(params["grid_step"])
        plan[2]["parameters"]["grid_step"] = 0.01  # someone tunes after seeing attempt 1
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend, plan=plan)
    assert report["assisted"] is True
    assert any("plan" in r for r in report["assisted_reasons"])
    assert used == [0.1, 0.1, 0.1]


def test_re_pinning_the_baseline_mid_run_relabels_the_run_assisted(project, tmp_path):
    baseline = {"rules": "pinned"}
    report = _assisted_after(project, tmp_path,
                             lambda: baseline.update(rules="loosened"), baseline=baseline)
    assert report["assisted"] is True
    assert any("baseline" in r for r in report["assisted_reasons"])


def test_editing_the_frozen_manifest_relabels_the_run_assisted(project, tmp_path):
    def tamper():
        manifest = next((tmp_path / "runs").rglob("manifest.json"))
        manifest.chmod(0o600)
        manifest.write_text(manifest.read_text().replace('"budget_s": 1800.0', '"budget_s": 9999'))

    report = _assisted_after(project, tmp_path, tamper)
    assert report["assisted"] is True
    assert any("manifest" in r for r in report["assisted_reasons"])


def test_an_untouched_run_is_not_assisted(project, tmp_path):
    report, _ = make_run(project, tmp_path)
    assert report["assisted"] is False and report["assisted_reasons"] == []
    assert report["unofficial"] is False


def test_a_dirty_repository_makes_the_run_unofficial_not_assisted(project, tmp_path):
    report, _ = make_run(project, tmp_path,
                         repo_state=lambda: {"commit": "abc123", "dirty": True})
    assert report["unofficial"] is True
    assert report["assisted"] is False
    # All five attempts passed, yet an unofficial run is not an autonomous pass.
    assert report["attempts_passed"] == 5
    assert report["autonomous_pass"] is False


def _index(tmp_path):
    path = tmp_path / "runs" / "index.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_every_invocation_appends_one_index_line_and_never_rewrites_earlier_ones(
        project, tmp_path):
    make_run(project, tmp_path)
    path = tmp_path / "runs" / "index.jsonl"
    first_bytes = path.read_bytes()
    assert [l["kind"] for l in _index(tmp_path)] == ["started", "batch"]

    make_run(project, tmp_path, gate=lambda b: fail_result())
    assert path.read_bytes().startswith(first_bytes)
    lines = [l for l in _index(tmp_path) if l["kind"] == "batch"]
    assert len(lines) == 2
    assert lines[0]["passed"] == 5 and lines[0]["planned"] == 5
    assert lines[1]["passed"] == 0 and lines[1]["planned"] == 5
    assert lines[0]["rung"] == 2 and lines[0]["backend"] == "krt"
    assert lines[0]["protocol_version"] == rung_runner.PROTOCOL_VERSION
    assert lines[0]["manifest_sha"] != lines[1]["manifest_sha"] or lines[0]["run_dir"] != lines[1]["run_dir"]


def gate_passing_first(n):
    """A gate that passes its first ``n`` calls and fails the rest."""
    calls = {"n": 0}

    def gate(board):
        calls["n"] += 1
        return dict(PASS) if calls["n"] <= n else fail_result()

    return gate


def test_the_reported_result_is_the_first_scored_batch_with_the_others_beside_it(
        project, tmp_path):
    # Distinct counts per batch (1, 3, 0, 5 of 5) so first, last, best and sum differ.
    for n in (1, 3, 0, 5):
        make_run(project, tmp_path, gate=gate_passing_first(n))

    result = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert result["reported"]["passed"] == 1
    assert result["batches"] == 4
    assert [b["passed"] for b in result["other_batches"]] == [3, 0, 5]
    assert result["batches_with_pass"] == 3
    assert result["summary"] == "passed in 3 of 4 official batches; reported batch: 1/5"


def test_a_batch_that_ran_no_attempt_is_not_scored(project, tmp_path):
    make_run(project, tmp_path, budget_s=0)           # nothing starts: run == 0
    make_run(project, tmp_path, gate=gate_passing_first(2))
    result = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert result["reported"]["passed"] == 2
    assert result["batches"] == 2
    assert result["summary"] == "passed in 1 of 1 official batches; reported batch: 2/5"


def test_backends_are_reported_separately_and_never_pooled(project, tmp_path):
    make_run(project, tmp_path, backend_name="krt", gate=lambda b: fail_result())
    make_run(project, tmp_path, backend_name="freerouting",
             plan=rung_runner.load_protocol()["backends"]["freerouting"]["plan"])

    results = rung_runner.rung_results(tmp_path / "runs", rung=2)
    assert set(results) == {"krt", "freerouting"}
    assert results["krt"]["reported"]["passed"] == 0
    assert results["freerouting"]["reported"]["passed"] == 5
    assert results["krt"]["batches"] == 1 and results["freerouting"]["batches"] == 1


def test_a_new_protocol_version_starts_a_fresh_count(project, tmp_path):
    make_run(project, tmp_path, gate=lambda b: fail_result(), protocol_version="1")
    make_run(project, tmp_path, protocol_version="2")
    v1 = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt",
                                 protocol_version="1")
    v2 = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt",
                                 protocol_version="2")
    # Neither label is the committed protocol version, so both are unofficial history; what
    # this pins is that the labels never pool.
    assert v1["batches"] == 1 and v1["unofficial_batches"][0]["passed"] == 0
    assert v2["batches"] == 1 and v2["unofficial_batches"][0]["passed"] == 5
    assert v1["reported"] is None and v2["reported"] is None


def test_re_running_an_unplanned_attempt_relabels_the_batch_assisted_and_is_indexed(
        project, tmp_path):
    report, clock = make_run(project, tmp_path, gate=lambda b: fail_result())
    assert report["assisted"] is False

    updated = rung_runner.rerun_attempt(
        report["run_dir"], 2,
        backend=lambda board, params, timeout_s, log: {"status": "ok"},
        gate=lambda b: dict(PASS), clock=clock)

    assert updated["assisted"] is True
    assert any("unplanned" in r for r in updated["assisted_reasons"])
    # The re-run does not change the scored count; it is listed beside it.
    assert (updated["attempts_passed"], updated["attempts_run"]) == (0, 5)
    assert updated["autonomous_pass"] is False
    assert updated["unplanned_reruns"][0]["index"] == 2
    assert updated["unplanned_reruns"][0]["status"] == "PASS"
    on_disk = json.loads((Path(report["run_dir"]) / "report.json").read_text())
    assert on_disk["assisted"] is True
    lines = _index(tmp_path)
    assert [l["kind"] for l in lines] == ["started", "batch", "rerun"]
    result = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert result["reported"]["assisted"] is True
    assert result["batches"] == 1


# --- review round: crash visibility, input tamper, gate validation, caps -------------


class Boom(BaseException):
    """Stands in for a kill: not an Exception, so nothing in the runner may swallow it."""


def test_a_crash_mid_run_leaves_a_started_index_line_counted_as_a_failed_batch(
        project, tmp_path):
    def backend(board, params, timeout_s, log):
        raise Boom

    with pytest.raises(Boom):
        make_run(project, tmp_path, backend=backend)
    lines = _index(tmp_path)
    assert [l["kind"] for l in lines] == ["started"]
    assert lines[0]["manifest_sha"] and lines[0]["run_dir"]

    result = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert result["batches"] == 1
    assert result["reported"]["crashed"] is True
    assert result["reported"]["passed"] == 0
    assert result["batches_with_pass"] == 0
    assert "crashed" in result["summary"]


def test_a_crashed_first_batch_stays_the_reported_result_over_a_later_lucky_one(
        project, tmp_path):
    def backend(board, params, timeout_s, log):
        raise Boom

    with pytest.raises(Boom):
        make_run(project, tmp_path, backend=backend)
    make_run(project, tmp_path)  # a clean 5/5 afterwards
    result = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert result["reported"]["crashed"] is True
    assert result["batches"] == 2 and result["batches_with_pass"] == 1


def test_a_backend_or_gate_exception_fails_the_attempt_and_the_run_completes(
        project, tmp_path):
    calls = {"n": 0}

    def backend(board, params, timeout_s, log):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("router segfault")
        return {"status": "ok"}

    def gate(board):
        if calls["n"] == 2:
            raise OSError("kicad-cli vanished")
        return dict(PASS)

    report, _ = make_run(project, tmp_path, backend=backend, gate=gate)
    statuses = [a["status"] for a in report["attempts"]]
    assert statuses == ["FAIL", "INCOMPLETE", "PASS", "PASS", "PASS"]
    assert report["attempts"][0]["cause"] == "router-failure"
    assert "router segfault" in report["attempts"][0]["detail"]
    assert "kicad-cli vanished" in " ".join(report["attempts"][1]["gate"]["incomplete_reasons"])
    assert report["attempts_run"] == 5 and report["attempts_passed"] == 3


def test_a_router_failure_never_turns_a_passing_gate_into_a_pass(project, tmp_path):
    report, _ = make_run(project, tmp_path,
                         backend=lambda b, p, t, l: {"status": "failure"},
                         plan=[{"lever": "grid", "parameters": {}}])
    assert report["attempts"][0]["status"] == "FAIL"
    assert report["attempts"][0]["cause"] == "router-failure"
    assert report["attempts_passed"] == 0


def test_the_gate_grades_each_attempts_own_working_copy(project, tmp_path):
    graded = []

    def gate(board):
        graded.append(board)
        return dict(PASS)

    report, _ = make_run(project, tmp_path, gate=gate)
    run_dir = Path(report["run_dir"])
    assert graded == [run_dir / "attempts" / f"{i:02d}" / "project" / "board.kicad_pcb"
                      for i in range(5)]


def test_attempts_start_from_the_frozen_copy_even_when_the_original_is_edited(
        project, tmp_path):
    starts = []

    def backend(board, params, timeout_s, log):
        starts.append(hashlib.sha256(board.read_bytes()).hexdigest())
        (project / "board.kicad_pcb").write_bytes(b"edited by hand\n")
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend)
    assert starts == [BOARD_SHA] * 5
    assert report["assisted"] is True


def test_every_file_of_the_project_is_hashed_into_the_manifest_and_each_attempt(
        project, tmp_path):
    (project / "board.kicad_pro").write_bytes(b"project settings\n")
    report, _ = make_run(project, tmp_path)
    manifest, _ = load(report)
    pro_sha = hashlib.sha256(b"project settings\n").hexdigest()
    assert manifest["frozen_files"] == {
        "board.kicad_pcb": BOARD_SHA, "board.kicad_dru": RULES_SHA, "board.kicad_pro": pro_sha}
    assert [a["input_sha256"] for a in report["attempts"]] == [BOARD_SHA] * 5


def test_a_backend_rewriting_an_unlisted_project_file_in_its_copy_is_assisted(
        project, tmp_path):
    (project / "board.kicad_pro").write_bytes(b"project settings\n")

    def backend(board, params, timeout_s, log):
        (board.parent / "board.kicad_pro").write_bytes(b"severities loosened\n")
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend)
    assert report["assisted"] is True
    assert any("board.kicad_pro" in r for r in report["assisted_reasons"])
    assert report["autonomous_pass"] is False


def test_tampering_with_the_frozen_copy_between_attempts_is_assisted(project, tmp_path):
    def tamper():
        frozen = next((tmp_path / "runs").rglob("frozen"))
        (frozen / "board.kicad_dru").write_bytes(b"(severity ignore)\n")

    report = _assisted_after(project, tmp_path, tamper)
    assert report["assisted"] is True
    assert any("frozen" in r and "board.kicad_dru" in r for r in report["assisted_reasons"])


@pytest.mark.parametrize("bad", [
    {**PASS, "violations": [{"type": "clearance"}]},
    {**PASS, "unconnected": [{"net": "GND", "items": ["pad"]}]},
    {**PASS, "incomplete_reasons": ["cap hit"]},
    {**PASS, "unstable": True},
    {"verdict": "PASS"},
    {**PASS, "verdict": "MAYBE"},
    {**PASS, "violations": "none"},
    "PASS",
])
def test_a_malformed_or_self_contradicting_gate_result_is_incomplete_never_a_pass(
        project, tmp_path, bad):
    report, _ = make_run(project, tmp_path, gate=lambda b: bad,
                         plan=[{"lever": "grid", "parameters": {}}])
    attempt = report["attempts"][0]
    assert attempt["status"] == "INCOMPLETE"
    assert attempt["gate"]["verdict"] == "INCOMPLETE"
    assert attempt["gate"]["incomplete_reasons"]
    assert report["attempts_passed"] == 0


def test_an_attempt_over_its_cap_is_a_timeout_even_if_the_backend_says_ok(
        project, tmp_path):
    clock = Clock()

    def backend(board, params, timeout_s, log):
        clock.t += 301  # the router ignored its cap
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend, clock=clock,
                         plan=[{"lever": "grid", "parameters": {}}])
    assert report["attempts"][0]["status"] == "TIMEOUT"
    assert report["attempts"][0]["cause"] == "router-timeout"
    assert report["attempts_passed"] == 0


def test_an_attempt_whose_gate_run_overruns_the_total_budget_is_a_timeout(
        project, tmp_path):
    clock = Clock()

    def backend(board, params, timeout_s, log):
        clock.t += 290
        return {"status": "ok"}

    def gate(board):
        clock.t += 1600  # grading is inside the budget too
        return dict(PASS)

    report, _ = make_run(project, tmp_path, backend=backend, gate=gate, clock=clock,
                         plan=[{"lever": "grid", "parameters": {}}])
    assert report["attempts"][0]["status"] == "TIMEOUT"
    assert report["attempts"][0]["cause"] == "budget-exceeded"


@pytest.mark.parametrize("kw", [
    {"plan": [{"lever": "grid", "parameters": {}}] * 3},
    {"budget_s": 600},
    {"attempt_timeout_s": 60},
])
def test_a_run_off_the_protocol_constants_is_unofficial_and_not_an_autonomous_pass(
        project, tmp_path, kw):
    report, _ = make_run(project, tmp_path, **kw)
    assert report["unofficial"] is True
    assert report["unofficial_reasons"]
    assert report["autonomous_pass"] is False
    assert report["attempts_passed"] >= 1


def test_index_lines_carry_autonomy_plan_and_board_hashes_and_the_summary_names_labels(
        project, tmp_path):
    make_run(project, tmp_path, repo_state=lambda: {"commit": "abc", "dirty": True})
    make_run(project, tmp_path)
    lines = [l for l in _index(tmp_path) if l["kind"] == "batch"]
    assert lines[0]["autonomous_pass"] is False and lines[0]["unofficial"] is True
    assert lines[1]["autonomous_pass"] is True
    assert lines[0]["board_sha256"] == BOARD_SHA
    assert lines[0]["plan_sha256"] == lines[1]["plan_sha256"]
    result = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert result["batches_with_pass"] == 1     # the unofficial pass does not count
    assert result["summary"] == (
        "passed in 1 of 1 official batches; reported batch: 5/5; 1 unofficial batch(es) beside it, not counted")
    assert result["reported"]["unofficial"] is False


def test_a_duplicated_log_entry_during_the_run_is_caught_in_that_attempt(project, tmp_path):
    def tamper():
        log = next((tmp_path / "runs").rglob("attempt_log.jsonl"))
        with log.open("a") as fh:
            fh.write(json.dumps({"index": 0, "lever": "grid", "parameters": {}}) + "\n")

    report = _assisted_after(project, tmp_path, tamper)
    assert report["assisted"] is True
    assert report["attempts"][1]["human_interventions"]


def test_a_wiped_attempt_log_is_assisted(project, tmp_path):
    report = _assisted_after(
        project, tmp_path,
        lambda: next((tmp_path / "runs").rglob("attempt_log.jsonl")).unlink())
    assert report["assisted"] is True
    assert any("log" in r for r in report["assisted_reasons"])


def test_an_edited_attempt_log_is_assisted(project, tmp_path):
    def tamper():
        log = next((tmp_path / "runs").rglob("attempt_log.jsonl"))
        log.write_text(log.read_text().replace('"baseline"', '"hand-picked"'))

    report = _assisted_after(project, tmp_path, tamper)
    assert report["assisted"] is True


def test_rerunning_after_the_manifest_was_edited_is_assisted_for_that_too(project, tmp_path):
    report, clock = make_run(project, tmp_path, gate=lambda b: fail_result())
    manifest = Path(report["run_dir"]) / "manifest.json"
    manifest.chmod(0o600)
    manifest.write_text(manifest.read_text().replace('"rung": 2', '"rung": 3'))
    updated = rung_runner.rerun_attempt(
        report["run_dir"], 0, backend=lambda b, p, t, l: {"status": "ok"},
        gate=lambda b: dict(PASS), clock=clock)
    assert any("manifest" in r for r in updated["assisted_reasons"])


def test_a_rerun_with_a_failing_router_is_a_fail_not_a_pass(project, tmp_path):
    report, clock = make_run(project, tmp_path, gate=lambda b: fail_result())
    updated = rung_runner.rerun_attempt(
        report["run_dir"], 0, backend=lambda b, p, t, l: {"status": "failure"},
        gate=lambda b: dict(PASS), clock=clock)
    assert updated["unplanned_reruns"][0]["status"] == "FAIL"
    assert updated["unplanned_reruns"][0]["cause"] == "router-failure"


# --- round 2: what the gate reads, vanished output, orphans, caps -------------------------


@pytest.mark.parametrize("new_file", [
    "new.kicad_dru", "new.kicad_pro", "new.kicad_prl", "extra.kicad_sch", "s.kicad_sym",
    "fp.kicad_mod", "UPPER.KICAD_DRU", "Mixed.Kicad_Pro", "fp-lib-table", "sym-lib-table",
    "FP-LIB-TABLE", "lib.pretty/part.txt", "sub/lib.pretty/a.kicad_mod",
])
def test_a_new_file_the_gate_would_read_in_an_attempt_copy_is_assisted_and_named(
        project, tmp_path, new_file):
    def backend(board, params, timeout_s, log):
        target = board.parent / new_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"planted by the backend\n")
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend)
    assert report["assisted"] is True
    assert report["autonomous_pass"] is False
    assert any(Path(new_file).name in r for r in report["assisted_reasons"])


@pytest.mark.parametrize("benign", [
    "router.log", "route_report.json", "board.kicad_pcb.lck", "board-backups/old.txt",
    "freerouting.dsn", "out/result.ses",
])
def test_router_output_files_in_an_attempt_copy_are_benign(project, tmp_path, benign):
    def backend(board, params, timeout_s, log):
        target = board.parent / benign
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"router output\n")
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend)
    assert report["assisted"] is False
    assert report["autonomous_pass"] is True


def test_a_backend_that_deletes_its_output_board_fails_that_attempt_not_the_batch(
        project, tmp_path):
    calls = {"n": 0}

    def backend(board, params, timeout_s, log):
        calls["n"] += 1
        if calls["n"] == 2:
            board.unlink()
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend)
    statuses = [a["status"] for a in report["attempts"]]
    assert statuses == ["PASS", "INCOMPLETE", "PASS", "PASS", "PASS"]
    assert "board" in report["attempts"][1]["detail"]
    assert report["attempts"][1]["output_board_sha256"] is None
    assert report["attempts_run"] == 5 and report["attempts_passed"] == 4


def test_a_backend_that_removes_a_whole_directory_of_its_copy_does_not_crash_the_batch(
        project, tmp_path):
    (project / "sub").mkdir()
    (project / "sub" / "a.kicad_sch").write_bytes(b"sheet\n")

    def backend(board, params, timeout_s, log):
        shutil.rmtree(board.parent / "sub")
        return {"status": "ok"}

    report, _ = make_run(project, tmp_path, backend=backend)
    assert report["assisted"] is True
    assert any("sub/a.kicad_sch" in r for r in report["assisted_reasons"])
    assert report["attempts_run"] == 5


def test_a_run_dir_missing_from_the_index_is_an_orphan_counted_as_a_crashed_batch(
        project, tmp_path):
    make_run(project, tmp_path)                       # a clean 5/5
    (tmp_path / "runs" / "index.jsonl").write_text("")  # its index lines are gone
    make_run(project, tmp_path, gate=gate_passing_first(3))
    result = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert result["batches"] == 2
    assert result["reported"]["orphan"] is True       # the older run is reported first
    assert result["reported"]["crashed"] is True and result["reported"]["passed"] == 0
    assert result["batches_with_pass"] == 1           # only the indexed one
    assert "orphan" in result["summary"]


def test_a_crash_between_the_manifest_freeze_and_the_started_line_leaves_an_orphan(
        project, tmp_path, monkeypatch):
    def boom(path, value):
        raise Boom

    monkeypatch.setattr(rung_runner, "_append_line", boom)
    with pytest.raises(Boom):
        make_run(project, tmp_path)
    monkeypatch.undo()
    assert not (tmp_path / "runs" / "index.jsonl").exists()

    result = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert result["batches"] == 1
    assert result["reported"]["orphan"] is True and result["reported"]["crashed"] is True
    assert result["reported"]["passed"] == 0
    assert rung_runner.rung_results(tmp_path / "runs", rung=2)["krt"]["batches"] == 1


@pytest.mark.parametrize("warnings", [None, "none", 3])
def test_a_gate_result_without_a_list_of_warnings_is_incomplete(project, tmp_path, warnings):
    report, _ = make_run(project, tmp_path, gate=lambda b: {**PASS, "warnings": warnings},
                         plan=[{"lever": "grid", "parameters": {}}])
    assert report["attempts"][0]["status"] == "INCOMPLETE"
    assert any("warnings" in r for r in report["attempts"][0]["gate"]["incomplete_reasons"])


def test_rerunning_a_clean_five_of_five_batch_flips_it_to_not_autonomous(project, tmp_path):
    report, clock = make_run(project, tmp_path)
    assert (report["attempts_passed"], report["autonomous_pass"]) == (5, True)
    before = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert before["reported"]["autonomous_pass"] is True

    updated = rung_runner.rerun_attempt(
        report["run_dir"], 4, backend=lambda b, p, t, l: {"status": "ok"},
        gate=lambda b: dict(PASS), clock=clock)
    assert updated["attempts_passed"] == 5            # the count is shown, not erased
    assert updated["autonomous_pass"] is False
    after = rung_runner.rung_result(tmp_path / "runs", rung=2, backend_name="krt")
    assert after["reported"]["autonomous_pass"] is False
    assert after["batches_with_pass"] == 0


def test_a_rerun_after_the_frozen_copy_was_edited_is_assisted_for_that_too(project, tmp_path):
    report, clock = make_run(project, tmp_path)
    (Path(report["run_dir"]) / "frozen" / "board.kicad_dru").write_bytes(b"(severity ignore)\n")
    updated = rung_runner.rerun_attempt(
        report["run_dir"], 0, backend=lambda b, p, t, l: {"status": "ok"},
        gate=lambda b: dict(PASS), clock=clock)
    assert any("frozen" in r and "board.kicad_dru" in r for r in updated["assisted_reasons"])


@pytest.mark.parametrize("entry", [
    {"index": 99, "lever": "grid", "parameters": {}},    # out of range
    {"index": 4, "lever": "grid", "parameters": {}},     # in range but not executed yet
    {"index": -1, "lever": "grid", "parameters": {}},
])
def test_an_unplanned_attempt_log_entry_is_assisted(project, tmp_path, entry):
    def tamper():
        log = next((tmp_path / "runs").rglob("attempt_log.jsonl"))
        with log.open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    report = _assisted_after(project, tmp_path, tamper)
    assert report["assisted"] is True
    assert any("unplanned" in r for r in report["assisted_reasons"])


def test_the_timeout_each_backend_call_receives_is_the_cap_clipped_to_the_budget_left(
        project, tmp_path):
    clock = Clock()
    received = []

    def backend(board, params, timeout_s, log):
        received.append(timeout_s)
        clock.t += 160
        return {"status": "ok"}

    def gate(board):
        clock.t += 400
        return dict(PASS)

    report, _ = make_run(project, tmp_path, backend=backend, gate=gate, clock=clock)
    # Attempts start at 0, 560, 1120, 1680: 120 s of the 1800 s budget remain at the last.
    assert received == [300, 300, 300, 120]
    assert [a["status"] for a in report["attempts"]] == ["PASS"] * 3 + ["TIMEOUT", "NOT_RUN"]
