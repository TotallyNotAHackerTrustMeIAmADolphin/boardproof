"""Delta review of ticket 06: seams, the delivered-below-requested policy, removed copper,
refusal counting, crashed-batch labelling and the repository-root dirty check."""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kicad_mcp import rung_backends, rung_runner

from test_rung_runner import BOARD_SHA, make_run, project  # noqa: F401
from test_rung_runner_review import _CLEAN, _DIRTY, _git, repo  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"
PASS_GATE = {"verdict": "PASS", "unconnected": [], "violations": [], "warnings": [],
             "unstable": False, "incomplete_reasons": []}


def _index(tmp_path, runs="runs"):
    return [json.loads(x) for x in (tmp_path / runs / "index.jsonl").read_text().splitlines()]


def _backend_with(facts, status="ok"):
    def backend(board, params, timeout_s, log):
        return {"status": status, "cause": None, "facts": facts}

    return backend


# --- (1) seams ---------------------------------------------------------------------------------


def test_every_injected_runner_seam_is_named_in_the_unofficial_reasons(project, tmp_path, enforce_seams):  # noqa: F811
    report, _ = make_run(project, tmp_path, now=lambda: datetime(2026, 10, 11, tzinfo=UTC))
    reasons = report["unofficial_reasons"]
    for seam in ("repo_state", "clock", "now"):
        assert any(f"seam injected: {seam}" in r for r in reasons), (seam, reasons)
    assert report["autonomous_pass"] is False


def test_a_missing_preflight_and_a_foreign_preflight_are_unofficial(project, tmp_path, enforce_seams):  # noqa: F811
    none_report, _ = make_run(project, tmp_path, preflight=None)
    assert any("seam: preflight" in r for r in none_report["unofficial_reasons"])
    custom, _ = make_run(project, tmp_path, preflight=lambda board: None)
    assert any("seam injected: preflight" in r for r in custom["unofficial_reasons"])


def test_a_run_with_no_injected_seam_is_official(tmp_path, enforce_seams):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "t")
    (root / "a.txt").write_text("x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "i")
    src = tmp_path / "src"
    src.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        shutil.copy(FIXTURES / f"krt_stress_congestion{suffix}", src)
    report = rung_runner.run_rung(
        rung=2, backend_name="krt", board=src / "krt_stress_congestion.kicad_pcb",
        rule_files=[src / "krt_stress_congestion.kicad_pro"],
        plan=rung_runner.load_protocol()["backends"]["krt"]["plan"],
        backend=lambda b, p, t, log: {"status": "ok"}, gate=lambda b: dict(PASS_GATE),
        baseline={"r": 1}, tool_versions={}, runs_root=root / "runs",
        protocol=rung_runner.load_protocol(), invariants=rung_backends.board_invariants,
        preflight=rung_backends.input_refusal, repo_dir=root,
    )
    assert report["unofficial_reasons"] == []
    assert report["autonomous_pass"] is True


# --- (3) policy: delivered below the requested size never counts ------------------------------------

_BELOW = [{"net_name": "SIG1", "kind": "track_width", "requested_mm": 0.25, "delivered_mm": 0.2}]


def test_delivered_below_requested_is_graded_but_never_counts_as_a_pass(project, tmp_path):  # noqa: F811
    report, _ = make_run(project, tmp_path, backend=_backend_with({"delivered_below_requested": _BELOW}))
    attempt = report["attempts"][0]
    assert attempt["gate"]["verdict"] == "PASS"  # graded as usual
    assert attempt["status"] == "FAIL"
    assert attempt["cause"] == "delivered-below-requested"
    assert attempt["delivered_below_requested_count"] == 1
    assert (report["attempts_passed"], report["autonomous_pass"]) == (0, False)
    assert report["attempts_delivered_below_requested"] == 5
    line = _index(tmp_path)[-1]
    assert line["delivered_below_requested_attempts"] == 5 and line["passed"] == 0
    assert "delivered below requested" in rung_runner.rung_result(tmp_path / "runs", 2, "krt")["summary"]


def test_a_strict_sizes_violation_alone_is_the_same(project, tmp_path):  # noqa: F811
    report, _ = make_run(project, tmp_path, backend=_backend_with(
        {"strict_sizes_violated": True, "delivered_below_requested": []}))
    attempt = report["attempts"][0]
    assert (attempt["status"], attempt["cause"]) == ("FAIL", "delivered-below-requested")
    assert attempt["delivered_below_requested_count"] == 0
    assert report["attempts_delivered_below_requested"] == 5


def test_a_clean_router_report_still_passes(project, tmp_path):  # noqa: F811
    report, _ = make_run(project, tmp_path, backend=_backend_with(
        {"strict_sizes_violated": False, "delivered_below_requested": []}))
    assert report["attempts_passed"] == 5
    assert report["attempts_delivered_below_requested"] == 0


def test_the_policy_and_version_are_declared_in_the_protocol_file():
    protocol = rung_runner.load_protocol()
    assert protocol["policies"]["delivered_below_requested"] == "fail"
    assert protocol["protocol_version"] == "8"
    assert protocol["version_history"]["2"]
    assert "pinned" in protocol["version_history"]["3"]
    assert "build ticket 51" in protocol["version_history"]["4"]
    assert "build ticket 67" in protocol["version_history"]["5"]
    assert "build ticket 109" in protocol["version_history"]["6"]
    assert "ticket 02a" in protocol["version_history"]["7"]
    assert "build ticket 114" in protocol["version_history"]["8"]


# --- (5) removed copper is tagged ---------------------------------------------------------------------


def test_an_attempt_that_removed_existing_copper_is_tagged(project, tmp_path):  # noqa: F811
    report, _ = make_run(project, tmp_path, backend=_backend_with(
        {"removed_segment_count": 2, "removed_via_count": 1}))
    attempt = report["attempts"][0]
    assert attempt["removed_copper"] == {"segments": 2, "vias": 1}
    assert attempt["status"] == "PASS"  # the must-still-contain invariant decides, not this tag
    assert report["attempts_with_removed_copper"] == 5
    assert _index(tmp_path)[-1]["removed_copper_attempts"] == 5
    assert "removed existing copper" in rung_runner.rung_result(tmp_path / "runs", 2, "krt")["summary"]


def test_untouched_copper_carries_no_tag(project, tmp_path):  # noqa: F811
    report, _ = make_run(project, tmp_path, backend=_backend_with(
        {"removed_segment_count": 0, "removed_via_count": 0}))
    assert report["attempts"][0]["removed_copper"] is None
    assert report["attempts_with_removed_copper"] == 0


# --- (4) refusals are countable and carry the board hash -------------------------------------------------


_START = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)


def _refused_twice_scored_refused_once(project, tmp_path, now=None):  # noqa: F811
    """Two refusals, one scored batch, one refusal after it, in that index order."""
    runs = tmp_path / "runs"
    clock = {} if now is None else {"now": now}
    for _ in range(2):
        make_run(project, tmp_path, runs_root=runs, preflight=lambda board: "filled pour", **clock)
    make_run(project, tmp_path, runs_root=runs, **clock)
    make_run(project, tmp_path, runs_root=runs, preflight=lambda board: "filled pour", **clock)  # after: not counted
    assert [line["kind"] for line in _index(tmp_path)] == ["refused", "refused", "started", "batch", "refused"]
    return rung_runner.rung_result(runs, 2, "krt")


def test_refused_lines_carry_the_board_hash_and_are_counted_before_the_first_scored_batch(
        project, tmp_path):  # noqa: F811
    # The real wall clock: four runs often share one tick (Python 3.12 on Windows). Ordering
    # is by index position, so that no longer matters (build ticket 56; ticket 33 had
    # injected a ticking clock here to work around the date comparison).
    result = _refused_twice_scored_refused_once(project, tmp_path)
    assert [line["board_sha256"] for line in _index(tmp_path) if line["kind"] == "refused"] == [BOARD_SHA] * 3
    assert result["refusals_before_first_scored_batch"] == 2
    assert result["refusals_ordered_by"] == "index_position"
    assert "2 refusal(s) before the first scored batch" in result["summary"]


def test_a_refusal_in_the_same_clock_tick_as_the_batch_start_is_counted_by_its_index_position(
        project, tmp_path):  # noqa: F811
    # Reproduction of build ticket 56: every line carries the identical stamp. A strict `<`
    # on the dates counted 0 refusals here, a `<=` would count 3; the index order says 2.
    result = _refused_twice_scored_refused_once(project, tmp_path, now=lambda: _START)
    assert {line["date"] for line in _index(tmp_path)} == {_START.isoformat()}
    assert result["refusals_before_first_scored_batch"] == 2
    assert "2 refusal(s) before the first scored batch" in result["summary"]


def test_refusal_order_ignores_a_clock_that_runs_backwards(project, tmp_path):  # noqa: F811
    # Each reading is one minute earlier than the last (a clock step back): the refusals
    # before the batch carry LATER stamps than its start, the one after it an EARLIER stamp.
    # Dates would count only the wrong refusal; positions count the two that came first.
    ticks = iter(range(10_000))
    result = _refused_twice_scored_refused_once(
        project, tmp_path, now=lambda: _START - timedelta(minutes=next(ticks)))
    lines = _index(tmp_path)
    started = next(line for line in lines if line["kind"] == "started")
    refused = [line for line in lines if line["kind"] == "refused"]
    assert [r["date"] > started["date"] for r in refused] == [True, True, False]
    assert result["refusals_before_first_scored_batch"] == 2
    assert result["refused_batches"] == refused  # the stamps are still reported, unchanged


def test_a_refusal_between_a_batch_start_and_its_end_is_not_before_it(project, tmp_path):  # noqa: F811
    # A refusal from a concurrent process lands between the batch's started and batch lines:
    # the batch was already under way, so the refusal is not "before" it.
    runs = tmp_path / "runs"

    def backend(board, params, timeout_s, log):
        if not any(line["kind"] == "refused" for line in _index(tmp_path)):
            make_run(project, tmp_path, runs_root=runs, preflight=lambda b: "filled pour")
        return {"status": "ok"}

    make_run(project, tmp_path, runs_root=runs, backend=backend)
    assert [line["kind"] for line in _index(tmp_path)] == ["started", "refused", "batch"]
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["refusals_before_first_scored_batch"] == 0
    assert "refusal(s) before" not in result["summary"]


def test_with_no_scored_batch_every_refusal_is_before_it(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"
    for _ in range(2):
        make_run(project, tmp_path, runs_root=runs, preflight=lambda board: "filled pour")
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["status"] == "REFUSED"
    assert result["refusals_before_first_scored_batch"] == 2
    assert result["refusals_ordered_by"] == "no_scored_batch"


def test_an_orphan_reported_batch_has_no_index_position_so_the_fallback_is_named(
        project, tmp_path, monkeypatch):  # noqa: F811
    # A crash between the manifest freeze and the started line leaves an orphan: a run
    # directory with no index line, hence no position. Its refusals are ordered against the
    # manifest's created_utc instead, ties counted as before, and the result says so.
    runs = tmp_path / "runs"
    ticks = iter(range(10_000))

    def now():
        return _START + timedelta(seconds=next(ticks))

    make_run(project, tmp_path, runs_root=runs, preflight=lambda board: "filled pour", now=now)  # START+1

    class Kill(BaseException):
        pass

    real_append = rung_runner._append_line

    def append_or_kill(path, value):
        if value.get("kind") == "started":
            raise Kill
        real_append(path, value)

    monkeypatch.setattr(rung_runner, "_append_line", append_or_kill)
    with pytest.raises(Kill):
        make_run(project, tmp_path, runs_root=runs, now=now)  # orphan, created START+2
    monkeypatch.setattr(rung_runner, "_append_line", real_append)
    make_run(project, tmp_path, runs_root=runs, preflight=lambda board: "filled pour", now=now)  # START+4
    make_run(project, tmp_path, runs_root=runs, preflight=lambda board: "filled pour",
             now=lambda: _START + timedelta(seconds=2))  # the orphan's own stamp: a tie
    assert [line["kind"] for line in _index(tmp_path)] == ["refused", "refused", "refused"]

    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["orphan"] is True
    assert result["reported"]["date"] == (_START + timedelta(seconds=2)).isoformat()
    assert result["refusals_before_first_scored_batch"] == 2  # the earlier one and the tie
    assert result["refusals_ordered_by"] == "date_orphan_fallback"
    assert "ordered by date" in result["summary"]


# --- (7) crashed batch keeps its floor label; floor branch; repository root --------------------------------


def test_a_crashed_batch_index_line_carries_the_input_fail_floor(project, tmp_path):  # noqa: F811
    def dies(board, params, timeout_s, log):
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        make_run(project, tmp_path, backend=dies, setup={"input_floor": _DIRTY})
    result = rung_runner.rung_result(tmp_path / "runs", 2, "krt")
    assert result["reported"]["crashed"] is True
    assert result["reported"]["input_fail_floor"] is True
    assert "input_fail_floor" in result["summary"]


def test_a_failed_router_text_check_alone_labels_the_floor(project, tmp_path):  # noqa: F811
    floor = {**_CLEAN, "router_text_check_ok": False, "unhandled": []}
    report, _ = make_run(project, tmp_path, setup={"input_floor": floor})
    assert report["input_fail_floor"] is True
    assert "router text check is not ok" in report["input_floor_reasons"]


def test_the_dirty_check_covers_the_repository_root_not_just_the_package_dir(repo, tmp_path, project):  # noqa: F811
    pkg = repo / "src" / "pkg"
    pkg.mkdir(parents=True)
    (pkg / "m.py").write_text("x\n")
    (repo / "pyproject.toml").write_text("[project]\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "more")
    runs = repo / ".scratch" / "runs"
    runs.mkdir(parents=True)

    clean, _ = make_run(project, tmp_path, runs_root=runs, repo_state=None, repo_dir=pkg)
    assert not any("dirty" in r for r in clean["unofficial_reasons"])

    (repo / "pyproject.toml").write_text("[project]\nname = 'changed'\n")  # outside the package dir
    dirty, _ = make_run(project, tmp_path, runs_root=runs, repo_state=None, repo_dir=pkg)
    assert any("dirty" in r for r in dirty["unofficial_reasons"])


def test_untracked_files_under_the_runs_root_are_ignored(repo, tmp_path, project):  # noqa: F811
    runs = repo / ".scratch" / "pivot-ladder" / "runs"
    (runs / "2" / "krt").mkdir(parents=True)
    (runs / "2" / "krt" / "noise.json").write_text("{}")
    report, _ = make_run(project, tmp_path, runs_root=runs, repo_state=None, repo_dir=repo)
    assert not any("dirty" in r for r in report["unofficial_reasons"])
