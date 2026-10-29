"""Delta review of ticket 06 at the CLI: injected seams, whole-project rung pin, setup
failures in the index, superscript-safe rung parsing, policy counts in the summary."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_mcp import autonomy_run, rung_runner

from test_autonomy_run_review import OK_INFO, _drc, _measure, _ok_backend, board  # noqa: F401
from test_input_prep import GOLDEN_DIR, GOLDEN_NAME


def _reasons(outcome):
    return outcome["report"]["unofficial_reasons"]


def test_each_injected_cli_seam_is_named(board, tmp_path, enforce_seams):  # noqa: F811
    reasons = _reasons(_measure(board, tmp_path))
    for seam in ("drc", "backend", "availability"):
        assert any(f"seam injected: {seam}" in r for r in reasons), (seam, reasons)


def test_an_injected_preflight_and_rule_files_are_named(board, tmp_path, enforce_seams):  # noqa: F811
    outcome = _measure(board, tmp_path, preflight=lambda b: None, rule_files=[board.with_suffix(".kicad_pro")])
    reasons = _reasons(outcome)
    assert any("seam injected: preflight" in r for r in reasons)
    assert any("seam injected: rule_files" in r for r in reasons)


def test_the_rung_pin_binds_the_whole_graded_project_not_just_the_pcb(tmp_path, enforce_seams):
    project = tmp_path / "golden"
    shutil.copytree(GOLDEN_DIR, project)
    pcb = project / GOLDEN_NAME
    protocol = rung_runner.load_protocol()
    protocol["rungs"]["2"]["board_sha256"] = autonomy_run.board_sha(pcb)
    protocol["rungs"]["2"]["project_sha256"] = autonomy_run.project_sha(pcb)

    ok = _measure(pcb, tmp_path, rung=2, protocol=protocol)
    assert not any("rung fixture" in r or "pinned rung" in r for r in _reasons(ok))

    # Same board bytes, a rules file added beside it (a relaxed .kicad_dru).
    (project / "relaxed.kicad_dru").write_text('(version 1)\n(rule "x" (severity ignore) (condition "A.Type == \'Track\'"))\n')
    assert autonomy_run.board_sha(pcb) == protocol["rungs"]["2"]["board_sha256"]
    bad = _measure(pcb, tmp_path, rung=2, protocol=protocol, runs_root=tmp_path / "runs2")
    assert any("does not match the pinned rung fixture" in r and "project" in r for r in _reasons(bad))


def test_a_rung_with_only_a_board_pin_is_not_pinned(board, tmp_path):  # noqa: F811
    protocol = rung_runner.load_protocol()
    protocol["rungs"]["2"]["board_sha256"] = rung_runner._sha256_file(board)
    protocol["rungs"]["2"]["project_sha256"] = None
    outcome = _measure(board, tmp_path, rung=2, protocol=protocol)
    assert any("rung fixture not pinned" in r for r in _reasons(outcome))


def test_setup_failures_are_written_to_the_index(board, tmp_path):  # noqa: F811
    outcome = _measure(board, tmp_path, rung="../x")
    assert outcome["status"] == "SETUP_FAILED"
    lines = [json.loads(x) for x in (tmp_path / "runs" / "index.jsonl").read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["setup_failed"]
    assert "rung" in lines[0]["reason"]
    assert lines[0]["backend"] == "krt" and lines[0]["protocol_sha256"]
    assert not list((tmp_path / "runs").rglob("manifest.json"))
    result = rung_runner.rung_result(tmp_path / "runs", "../x", "krt")
    assert result["batches"] == 0


def test_a_missing_board_is_a_setup_failure_line(tmp_path):
    code = autonomy_run.main([
        "--rung", "2", "--backend", "krt", "--board", str(tmp_path / "nope.kicad_pcb"),
        "--runs-root", str(tmp_path / "runs"), "--parity", "required",
    ])
    assert code == autonomy_run.EXIT_SETUP
    lines = [json.loads(x) for x in (tmp_path / "runs" / "index.jsonl").read_text().splitlines()]
    assert lines[0]["kind"] == "setup_failed" and "board not found" in lines[0]["reason"]


@pytest.mark.parametrize("label", ["²", "٣", "1²"])
def test_a_non_ascii_digit_rung_is_a_setup_failure_not_a_crash(board, tmp_path, label):  # noqa: F811
    code = autonomy_run.main([
        "--rung", label, "--backend", "krt", "--board", str(board),
        "--runs-root", str(tmp_path / "runs"), "--parity", "required",
    ])
    assert code == autonomy_run.EXIT_SETUP


def test_policy_counts_reach_the_cli_summary(board, tmp_path):  # noqa: F811
    def backend(b, params, timeout_s, log):
        return {"status": "ok", "cause": None, "facts": {
            "delivered_below_requested": [{"net_name": "N"}], "removed_segment_count": 1,
            "removed_via_count": 0}}

    outcome = _measure(board, tmp_path, backend=backend)
    summary = autonomy_run._summary(outcome, "fixture", "krt")
    assert summary["attempts_delivered_below_requested"] == 5
    assert summary["attempts_with_removed_copper"] == 5
    assert summary["attempts_passed"] == 0


def test_an_outline_only_pour_is_measured_and_recorded_in_the_manifest(tmp_path):
    from test_rung_backends_pour import _zone_board

    board = _zone_board(tmp_path, "poured", strip_fill=True)
    outcome = _measure(board, tmp_path, parity="not_applicable", parity_reason="fixture has no schematic")
    assert outcome["status"] == "RAN"
    manifest = json.loads((Path(outcome["report"]["run_dir"]) / "manifest.json").read_text())
    zones = manifest["setup"]["pour_zones"]
    assert [(z["net"], z["filled_in_file"]) for z in zones] == [("GND", False)]
