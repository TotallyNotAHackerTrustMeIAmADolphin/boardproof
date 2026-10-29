"""Build ticket 109, limit 4 of the owner's decision (ticket 108): a necked result is counted and
labelled "PASS (necked)", never a plain PASS; a plain PASS stays plain.

The runner reads the shared under-width check's verdicts (``rung_backends.classify_necks``) from the
attempt's facts. An item without a ``neck_verdict`` is a policy hit like before protocol 6.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string

from kicad_mcp import autonomy_run, rung_backends, rung_runner, write_layer

from test_rung_runner import make_run as _make_run

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def workspace(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        shutil.copy(FIXTURES / f"krt_stress_congestion{suffix}", src)
    return src


def make_run(workspace, tmp_path, **kw):
    return _make_run(workspace, tmp_path, board=workspace / "krt_stress_congestion.kicad_pcb",
                     rule_files=[workspace / "krt_stress_congestion.kicad_pro"], **kw)


NECKED = {"net": "+3V3", "kind": "track", "dimension": "width", "delivered_mm": 0.15, "required_mm": 0.4,
          "neck_verdict": "necked", "neck_reason": "forced_neck", "neck_pad": "U1.4", "length_mm": 0.6972,
          "neck_distance_mm": 0.3, "start": [151.55, 107.9], "end": [150.8528, 107.9]}
FAILED = {**NECKED, "neck_verdict": "fail", "neck_reason": "neck_longer_than_bound", "delivered_mm": 0.127}


def _backend_with(facts):
    def backend(board, params, timeout_s, log):
        return {"status": "ok", "cause": None, "facts": facts}

    return backend


def _index(tmp_path):
    return [json.loads(x) for x in (tmp_path / "runs" / "index.jsonl").read_text().splitlines()]


def test_a_necked_pass_is_counted_and_labelled_and_is_not_a_policy_hit(workspace, tmp_path):
    report, _ = make_run(workspace, tmp_path, backend=_backend_with(
        {"delivered_below_requested": [NECKED, {**NECKED, "length_mm": 0.4659}]}))
    attempt = report["attempts"][0]
    assert attempt["status"] == "PASS" and attempt["cause"] is None
    assert attempt["status_label"] == "PASS (necked)"
    assert attempt["necked_count"] == 2
    assert attempt["necked_length_mm"] == pytest.approx(0.6972 + 0.4659)
    assert attempt["delivered_below_requested_policy_hit"] is False
    assert attempt["delivered_below_requested_count"] == 2  # the raw under-width list is kept whole
    assert report["attempts_passed"] == 5 and report["attempts_passed_necked"] == 5
    assert report["autonomous_pass"] is True
    assert report["autonomous_pass_necked"] is True
    assert report["autonomous_pass_label"] == "PASS (necked)"
    line = _index(tmp_path)[-1]
    assert (line["autonomous_pass_necked"], line["passed_necked"]) == (True, 5)
    summary = rung_runner.rung_result(tmp_path / "runs", 2, "krt")["summary"]
    assert "PASS (necked)" in summary and "not a plain PASS" in summary


def test_a_necked_item_next_to_a_failing_item_is_still_a_fail(workspace, tmp_path):
    report, _ = make_run(workspace, tmp_path, backend=_backend_with(
        {"delivered_below_requested": [NECKED, FAILED]}))
    attempt = report["attempts"][0]
    assert (attempt["status"], attempt["cause"]) == ("FAIL", "delivered-below-requested")
    assert attempt["status_label"] == "FAIL"
    assert report["attempts_passed"] == 0 and report["autonomous_pass"] is False
    assert report["autonomous_pass_label"] is None


def test_a_plain_pass_stays_plain(workspace, tmp_path):
    report, _ = make_run(workspace, tmp_path, backend=_backend_with({"delivered_below_requested": []}))
    attempt = report["attempts"][0]
    assert attempt["status"] == "PASS" and attempt["status_label"] == "PASS" and attempt["necked_count"] == 0
    assert report["autonomous_pass"] is True and report["autonomous_pass_necked"] is False
    assert report["autonomous_pass_label"] == "PASS"
    assert "PASS (necked)" not in rung_runner.rung_result(tmp_path / "runs", 2, "krt")["summary"]


def test_an_item_without_a_verdict_is_a_policy_hit_as_before(workspace, tmp_path):
    bare = {k: v for k, v in NECKED.items() if not k.startswith("neck")}
    report, _ = make_run(workspace, tmp_path, backend=_backend_with({"delivered_below_requested": [bare]}))
    assert report["attempts"][0]["status"] == "FAIL"


def test_a_krt_strict_sizes_violation_is_not_forgiven_by_a_neck(workspace, tmp_path):
    report, _ = make_run(workspace, tmp_path, backend=_backend_with(
        {"delivered_below_requested": [NECKED], "strict_sizes_violated": True}))
    assert report["attempts"][0]["status"] == "FAIL"


def test_the_best_attempt_prefers_a_plain_pass_over_a_necked_one():
    gate = {"unconnected": [], "violations": []}
    ran = [{"index": 0, "status": "PASS", "necked_count": 3, "gate": gate},
           {"index": 1, "status": "PASS", "necked_count": 0, "gate": gate}]
    assert rung_runner._best_attempt(ran) == 1
    assert rung_runner._best_attempt([ran[0]]) == 0


def test_the_autonomy_run_summary_carries_the_label(workspace, tmp_path):
    report, _ = make_run(workspace, tmp_path, backend=_backend_with({"delivered_below_requested": [NECKED]}))
    summary = autonomy_run._summary({"status": "DONE", "report": report,
                                     "result": {"summary": "x"}}, 2, "krt")
    assert summary["autonomous_pass_label"] == "PASS (necked)" and summary["autonomous_pass_necked"] is True
    assert summary["attempt_status_labels"][0] == "PASS (necked)"
    line = autonomy_run._under_width_line(NECKED)
    assert "forced neck at U1.4" in line
    assert "forced neck" not in autonomy_run._under_width_line(FAILED)  # a fail line is unchanged


# --- end to end through the shared check, on the rung-1 board ------------------------------------

RUNG1 = Path(__file__).resolve().parents[1] / "evals" / "rung1" / "layout_run" / "07_poured"
PASS_GATE = {"verdict": "PASS", "unconnected": [], "violations": [], "warnings": [],
             "unstable": False, "incomplete_reasons": []}


def _router_adding(segments):
    def backend(board, params, timeout_s, log):
        root = write_layer.load(board)
        for (sx, sy), (ex, ey), width in segments:
            root.children.append(parse_string(
                f'(segment (start {sx} {sy}) (end {ex} {ey}) (width {width}) (layer "F.Cu") (net 1) '
                f'(uuid "00000000-0000-4000-8000-{len(root.children):012d}"))'))
        write_layer.save(root, board)
        return {"status": "ok", "cause": None, "facts": {}}

    return backend


def _run_rung1(tmp_path, segments):
    source = tmp_path / "input"
    shutil.copytree(RUNG1, source)
    name = "rung1_usbc_attiny1616"
    return rung_runner.run_rung(
        rung="neck-e2e", backend_name="freerouting", board=source / f"{name}.kicad_pcb",
        rule_files=[source / f"{name}.kicad_pro"],
        plan=rung_runner.load_protocol()["backends"]["freerouting"]["plan"][:1],
        backend=rung_backends.with_under_width_check(_router_adding(segments)),
        gate=lambda board: dict(PASS_GATE), baseline={"rules": "pinned"}, tool_versions={},
        runs_root=tmp_path / "runs", repo_state=lambda: {"commit": "abc123", "dirty": False},
        protocol=rung_runner.load_protocol(), invariants=lambda board: {},
    )


def test_end_to_end_the_pass_board_neck_is_a_pass_necked_and_a_longer_neck_is_a_fail(tmp_path):
    ok = _run_rung1(tmp_path / "a", [((151.55, 107.9), (150.8528, 107.9), 0.15),
                                     ((150.8528, 107.9), (150.3869, 107.9), 0.2)])
    attempt = ok["attempts"][0]
    assert (attempt["status"], attempt["status_label"]) == ("PASS", "PASS (necked)")
    assert attempt["necked_count"] == 2
    assert attempt["necked_length_mm"] == pytest.approx(0.6972 + 0.4659)
    facts = attempt["router_facts"]["under_width_check"]
    assert (facts["under_width"], facts["failing"], facts["necked"]) == (2, 0, 2)
    assert facts["necked_length_mm"] == attempt["necked_length_mm"]

    too_long = _run_rung1(tmp_path / "b", [((151.55, 107.9), (149.05, 107.9), 0.2)])
    assert too_long["attempts"][0]["status"] == "FAIL"
    assert too_long["attempts"][0]["router_facts"]["delivered_below_requested"][0]["neck_reason"] == (
        "neck_longer_than_bound")


def test_the_cli_summary_never_shows_a_necked_pass_as_a_plain_pass(workspace, tmp_path):
    """Review FOLLOW-UP C: a consumer reading only the status list or the passed counts sees the difference."""
    report, _ = make_run(workspace, tmp_path, backend=_backend_with({"delivered_below_requested": [NECKED]}))
    outcome = {"status": "RAN", "report": report,
               "result": {"summary": "x"}}
    summary = autonomy_run._summary(outcome, "fixture", "krt")
    assert set(summary["attempt_statuses"]) == {"PASS (necked)"}
    assert "PASS" not in summary["attempt_statuses"]
    n = summary["attempts_passed"]
    assert n >= 1 and (summary["attempts_passed_necked"], summary["attempts_passed_plain"]) == (n, 0)
