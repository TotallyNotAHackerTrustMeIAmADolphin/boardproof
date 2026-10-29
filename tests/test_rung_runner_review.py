"""Review round for ticket 06: bypasses of "official" and "first scored batch",
refusals, router-versus-adapter time, repository state and superset invariants."""

from __future__ import annotations

import json
import subprocess

import pytest

from kicad_mcp import rung_runner

from test_rung_runner import Clock, load, make_run, project  # noqa: F401


def _plan():
    return rung_runner.load_protocol()["backends"]["krt"]["plan"]


# --- (3d, 3e) "official" cannot be reached by omitting or injecting things --------------------


def test_omitting_invariants_makes_the_run_unofficial(project, tmp_path):  # noqa: F811
    report, _ = make_run(project, tmp_path, invariants=None)
    assert report["unofficial"] is True
    assert any("invariants" in r for r in report["unofficial_reasons"])
    assert report["autonomous_pass"] is False


def test_omitting_the_protocol_makes_the_run_unofficial(project, tmp_path):  # noqa: F811
    report, _ = make_run(project, tmp_path, protocol=None)
    assert report["unofficial"] is True
    assert any("protocol was not supplied" in r for r in report["unofficial_reasons"])


def test_an_injected_protocol_cannot_launder_a_custom_plan(project, tmp_path):  # noqa: F811
    protocol = rung_runner.load_protocol()
    custom = [{"lever": "grid", "parameters": {"grid_step_mm": 0.05}}] * 5
    protocol["backends"]["krt"]["plan"] = custom  # the injected table agrees with the plan...
    report, _ = make_run(project, tmp_path, plan=custom, protocol=protocol)
    assert report["unofficial"] is True  # ...but the committed file does not
    assert any("committed" in r for r in report["unofficial_reasons"])


def test_an_injected_budget_that_differs_from_the_committed_file_is_unofficial(project, tmp_path):  # noqa: F811
    protocol = rung_runner.load_protocol()
    protocol["budget_s"] = 99999
    report, _ = make_run(project, tmp_path, protocol=protocol)
    assert report["unofficial"] is True
    assert any("protocol" in r and "committed" in r for r in report["unofficial_reasons"])


# --- (3c) batches pool by protocol sha, not just the version label ----------------------------


def test_batches_under_a_different_protocol_sha_are_not_pooled(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs)
    other = rung_runner.load_protocol()
    other["budget_s"] = 1000  # a different table under the SAME version label
    make_run(project, tmp_path, runs_root=runs, protocol=other)
    committed = rung_runner.rung_result(runs, 2, "krt")
    assert committed["batches"] == 1
    assert committed["different_protocol_sha_batches"] == 1
    mine = rung_runner.rung_result(runs, 2, "krt", protocol_sha=rung_runner._sha256_json(other))
    assert mine["batches"] == 1


# --- (6) a setup refusal is not scored and not pooled ---------------------------------------------


def test_a_refused_input_is_not_a_scored_batch_and_runs_no_attempt(project, tmp_path):  # noqa: F811
    calls = []

    def backend(board, params, timeout_s, log):
        calls.append(1)
        return {"status": "ok"}

    runs = tmp_path / "runs"
    report, _ = make_run(project, tmp_path, runs_root=runs, backend=backend,
                         preflight=lambda board: "board has a filled pour")
    assert calls == []
    assert report["status"] == "REFUSED"
    assert (report["attempts_run"], report["attempts_passed"], report["attempts_planned"]) == (0, 0, 5)
    assert report["autonomous_pass"] is False
    assert not list(runs.rglob("manifest.json"))
    lines = [json.loads(x) for x in (runs / "index.jsonl").read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["refused"]
    assert lines[0]["reason"] == "board has a filled pour"

    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["status"] == "REFUSED" and result["reported"] is None and result["batches"] == 0
    assert "REFUSED" in result["summary"] and "filled pour" in result["summary"]

    good, _ = make_run(project, tmp_path, runs_root=runs)  # a later real batch is the first scored one
    assert rung_runner.rung_result(runs, 2, "krt")["reported"]["run_dir"] == good["run_dir"]


# --- (7) the router cap is charged against router time, not adapter overhead ------------------


def test_adapter_overhead_does_not_turn_a_fast_router_into_a_timeout(project, tmp_path):  # noqa: F811
    clock = Clock()

    def backend(board, params, timeout_s, log):
        clock.t += 400  # routing 10 s plus 390 s of loading and diffing
        return {"status": "ok", "router_elapsed_s": 10.0}

    report, _ = make_run(project, tmp_path, backend=backend, clock=clock, plan=_plan()[:1])
    attempt = report["attempts"][0]
    assert attempt["status"] == "PASS"
    assert attempt["router_wall_s"] == 10.0
    assert attempt["backend_wall_s"] == 400.0
    assert attempt["adapter_overhead_s"] == 390.0


def test_a_router_that_really_ran_past_the_cap_is_still_a_timeout(project, tmp_path):  # noqa: F811
    clock = Clock()

    def backend(board, params, timeout_s, log):
        clock.t += 310
        return {"status": "ok", "router_elapsed_s": 305.0}

    report, _ = make_run(project, tmp_path, backend=backend, clock=clock)
    assert report["attempts"][0]["status"] == "TIMEOUT"
    assert report["attempts"][0]["cause"] == "router-timeout"


# --- (4) the repository state ignores the runner's own files ------------------------------------


def _git(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / "tracked.txt").write_text("x\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


def test_the_runners_own_files_do_not_make_the_tree_dirty(repo, tmp_path, project):  # noqa: F811
    report, _ = make_run(project, tmp_path, runs_root=repo / "runs", repo_state=None, repo_dir=repo)
    manifest, _ = load(report)
    assert manifest["repo"]["dirty"] is False
    assert manifest["repo"]["commit"]
    assert not any("dirty" in r for r in report["unofficial_reasons"])


def test_a_real_edit_to_a_tracked_file_still_makes_the_tree_dirty(repo, tmp_path, project):  # noqa: F811
    (repo / "tracked.txt").write_text("changed\n")
    report, _ = make_run(project, tmp_path, runs_root=repo / "runs", repo_state=None, repo_dir=repo)
    assert any("dirty" in r for r in report["unofficial_reasons"])


# --- (8) invariants may be "must still contain" sets -----------------------------------------------


def test_a_superset_invariant_allows_additions_but_not_removals(project, tmp_path):  # noqa: F811
    def inv(board):
        return {"copper": {"kind": "superset", "items": board.read_text().splitlines()}}

    def adder(board, params, timeout_s, log):
        board.write_text(board.read_text() + "added segment\n")
        return {"status": "ok"}

    def remover(board, params, timeout_s, log):
        board.write_text("")
        return {"status": "ok"}

    added, _ = make_run(project, tmp_path, backend=adder, invariants=inv)
    assert [a["status"] for a in added["attempts"]] == ["PASS"] * 5
    removed, _ = make_run(project, tmp_path, backend=remover, invariants=inv)
    assert removed["attempts"][0]["status"] == "FAIL"
    assert removed["attempts"][0]["invariant_violations"] == ["copper"]


# --- (5) the input floor is visible and labels the rung ----------------------------------------------

_DIRTY = {"grading_status": "FAIL", "certified": False, "error_count": 2, "unconnected_nets": 11,
          "unhandled": [], "router_text_check_ok": True}
_CLEAN = {"grading_status": "PASS", "certified": False, "error_count": 0, "unconnected_nets": 0,
          "unhandled": [], "router_text_check_ok": True}


def test_a_dirty_input_floor_is_in_the_report_and_labels_the_rung(project, tmp_path):  # noqa: F811
    report, _ = make_run(project, tmp_path, setup={"input_floor": _DIRTY})
    assert report["input_floor"] == _DIRTY
    assert report["input_fail_floor"] is True
    lines = [json.loads(x) for x in (tmp_path / "runs" / "index.jsonl").read_text().splitlines()]
    assert lines[-1]["input_fail_floor"] is True
    assert "input_fail_floor" in rung_runner.rung_result(tmp_path / "runs", 2, "krt")["summary"]


def test_a_clean_input_floor_is_not_labelled(project, tmp_path):  # noqa: F811
    report, _ = make_run(project, tmp_path, setup={"input_floor": _CLEAN})
    assert report["input_fail_floor"] is False


def test_unhandled_copper_text_labels_the_floor_even_when_it_grades_clean(project, tmp_path):  # noqa: F811
    floor = {**_CLEAN, "unhandled": [{"kind": "gr_text_box"}], "router_text_check_ok": False}
    report, _ = make_run(project, tmp_path, setup={"input_floor": floor})
    assert report["input_fail_floor"] is True
