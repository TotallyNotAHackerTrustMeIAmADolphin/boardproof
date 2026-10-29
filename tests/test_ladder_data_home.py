"""Publish-prep ticket 03a part B: the ladder's run data has a local, git-ignored home and a committed
sanitised history; no test reads ``.scratch`` any more.

The history file's own rules (no machine path, read as history by ``rung_result``) are tested in
``test_rung_runner_official_slot.py``; the converter is tested here.
"""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from kicad_mcp import rung_runner

ROOT = Path(__file__).resolve().parents[1]
BS = chr(92)  # a backslash, built so no escape in this file can be misread
DRIVE_PATH = r"[A-Za-z]:[/\x5c]"  # a drive path, slash or backslash
_spec = importlib.util.spec_from_file_location("export_ladder_results", ROOT / "scripts" / "export_ladder_results.py")
export = importlib.util.module_from_spec(_spec)
sys.modules["export_ladder_results"] = export  # the converter's dataclass-free code needs no more
_spec.loader.exec_module(export)

# Test files that name a ".scratch" path but never read the real folder (tmp_path trees, a forbidden
# link target, a comparison). A new test that reads the folder must not be added here.
NAMES_SCRATCH_WITHOUT_READING = {
    "test_ladder_data_home.py": "this file",
    "test_autonomy_run_review.py": "asserts a runs root named like the old one is a foreign root",
    "test_check_publishable.py": "plants a .scratch folder in tmp_path for the --exclude test",
    "test_rung_runner_policy.py": "builds its own .scratch/runs under a tmp repo",
    "test_public_docs_links.py": "names .scratch only as the forbidden link target; its cases live in tmp_path",
}
_READ = re.compile(r"""(?:ROOT|REPO|repo_root)\s*/\s*["']\.scratch["']|["']\.scratch/pivot|requires_scratch""")


def test_no_test_reads_the_scratch_folder():
    offenders = [p.name for p in sorted((ROOT / "tests").glob("test_*.py"))
                 if p.name not in NAMES_SCRATCH_WITHOUT_READING and _READ.search(p.read_text(encoding="utf-8"))]
    assert offenders == []
    assert not (ROOT / "tests" / "_scratch.py").exists()


def test_the_default_runs_root_is_the_local_runs_folder_and_the_history_file_is_committed_beside_the_evals():
    from kicad_mcp import autonomy_run

    assert autonomy_run.DEFAULT_RUNS_ROOT == ROOT / "runs"
    assert Path(rung_runner.HISTORY_FILE) == ROOT / "evals" / "ladder_results.json"
    assert Path(rung_runner.HISTORY_FILE).is_file()


def test_the_sanitiser_removes_windows_and_posix_machine_paths_including_spaces():
    dirty = {
        "a": "runs root " + BS.join(["C:", "Users", "Jane Doe", "Documents", "x", "runs"]) + " is not the folder",
        "b": ["see " + "/".join(["", "ho" + "me", "jane", "work", "runs", "1"]) + " and D:/data/run.json now",
              {"c": "/".join(["", "Users", "jane", "x"])}],
        "d": 3,
    }
    clean = json.dumps(export.sanitise(dirty))
    assert re.search(DRIVE_PATH, clean) is None
    for needle in ("Jane", "jane", "Users", "/home/"):
        assert needle not in clean
    assert export.sanitise(dirty)["a"] == "runs root <path> is not the folder"
    assert export.sanitise(dirty)["d"] == 3


def _fake_runs(tmp_path: Path) -> Path:
    runs = tmp_path / "runs"
    stamp = "20261028T230531Z"
    run_dir = runs / "1" / "krt" / stamp
    run_dir.mkdir(parents=True)
    machine = BS.join(["C:", "Users", "Jane Doe", "wt", ".scratch", "runs", "1", "krt", stamp])
    gate = {"verdict": "FAIL", "list_complete": True, "incomplete_reasons": [], "unstable": False, "warnings": [],
            "unconnected": [{"net": "N1", "items": [{"description": "pad", "pos": {"x": 1, "y": 2}, "uuid": "u1"}]}],
            "violations": [{"description": "clearance", "items": []}]}
    report = {"attempts": [{"index": 0, "lever": "l", "parameters": {"p": machine}, "status": "FAIL",
                            "cause": "gate", "gate": gate, "necked_count": 0}],
              "best_attempt": 0, "tool_versions": {"kicad_cli": "10.0.7", "where": machine}}
    (run_dir / "report.json").write_text(json.dumps(report), encoding="utf-8")
    row = {"kind": "batch", "rung": 1, "backend": "krt", "date": "2026-10-28T23:03:56+00:00",
           "protocol_version": "6", "protocol_sha256": "ab", "run_dir": machine, "passed": 0, "run": 1,
           "planned": 1, "unofficial": False, "unofficial_reasons": ["root " + machine + " odd"]}
    refused = {"kind": "refused", "rung": 1, "backend": "krt", "date": "2026-10-28T22:00:00+00:00",
               "protocol_version": "6", "protocol_sha256": "ab", "run_dir": machine + "x", "reason": "no"}
    (runs / "index.jsonl").write_text(json.dumps(refused) + "\n" + json.dumps(row) + "\n", encoding="utf-8")
    return runs


def test_the_converter_keeps_event_order_ids_and_the_best_attempts_complete_lists_without_paths(tmp_path):
    data = export.export(_fake_runs(tmp_path))
    assert [r["kind"] for r in data["rows"]] == ["refused", "batch"]
    batch_id = "history/1/krt/20261028T230531Z"
    assert data["rows"][1]["run_dir"] == batch_id
    detail = data["batches"][batch_id]
    assert detail["best_attempt"] == 0
    assert detail["best_attempt_gate"]["unconnected"][0]["net"] == "N1"
    assert detail["best_attempt_gate"]["violations"][0]["description"] == "clearance"
    assert detail["attempts"][0]["unconnected_count"] == 1 and detail["attempts"][0]["gate_verdict"] == "FAIL"
    text = json.dumps(data)
    assert re.search(DRIVE_PATH, text) is None and "Jane" not in text and ".scratch" not in text


def test_the_converter_refuses_to_write_when_a_path_survives(tmp_path, monkeypatch):
    runs = _fake_runs(tmp_path)
    monkeypatch.setattr(export, "sanitise", lambda value: value)  # a broken sanitiser
    out = tmp_path / "out.json"
    assert export.main([str(runs), "--out", str(out)]) == 1
    assert not out.exists()


def test_a_missing_or_corrupted_history_file_is_an_error_never_an_empty_history(tmp_path, monkeypatch):
    for content in (None, "not json", json.dumps({"schema": 2, "rows": []}), json.dumps({"schema": 1, "rows": 5})):
        bad = tmp_path / "bad.json"
        bad.unlink(missing_ok=True)
        if content is not None:
            bad.write_text(content, encoding="utf-8")
        monkeypatch.setattr(rung_runner, "HISTORY_FILE", bad)
        with pytest.raises(RuntimeError, match="unreadable"):
            rung_runner.rung_result(tmp_path / "runs", 1, "krt")


def test_the_committed_file_is_what_the_converter_would_write_for_its_own_rows():
    """The shipped file has the converter's shape: schema, rows with history ids, a detail per scored batch."""
    data = json.loads(Path(rung_runner.HISTORY_FILE).read_text(encoding="utf-8"))
    assert data["schema"] == export.SCHEMA
    batch_rows = [r for r in data["rows"] if r["kind"] == "batch"]
    assert batch_rows and {r["run_dir"] for r in batch_rows} == set(data["batches"])
    best = data["batches"]["history/1/freerouting/20261028T230531Z"]
    assert best["best_attempt_gate"]["verdict"] in ("PASS", "FAIL", "INCOMPLETE")
