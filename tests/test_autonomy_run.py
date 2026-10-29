"""The measurement CLI: setup order, availability and exit codes, at the S3 seam.

The kicad-cli boundary and the router are fakes here; the real router and gate run in
tests/test_rung_backends.py. Expected values are literals of the fixture build
(tests/test_input_prep.py adds two copper-layer texts and one silkscreen text).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kicad_mcp import autonomy_run, input_prep, rung_runner

from test_input_prep import _build_fixture

_EMPTY = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _drc(path, **_kwargs):
    return _EMPTY


@pytest.fixture
def board(tmp_path):
    return _build_fixture(tmp_path / "proj")


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_input_preparation_runs_once_before_the_baseline_pin_and_never_inside_an_attempt(
        board, tmp_path, monkeypatch):
    original_sha = _sha(board)
    order = []
    real_confirm = input_prep.confirm_input_preparation

    def spy_confirm(*a, **kw):
        order.append("prepare")
        return real_confirm(*a, **kw)

    monkeypatch.setattr(input_prep, "confirm_input_preparation", spy_confirm)
    boards_seen = []

    def backend(b, params, timeout_s, log):
        order.append("attempt")
        boards_seen.append(input_prep.copper_texts(b))
        return {"status": "ok", "cause": None}

    outcome = autonomy_run.run_measurement(
        rung="fixture", backend_name="krt", board=board, runs_root=tmp_path / "runs",
        parity="required", backend=backend, drc=_drc,
        availability={"available": True, "reason": None, "versions": {}},
    )

    assert order == ["prepare"] + ["attempt"] * 5  # exactly once, before any attempt
    assert boards_seen == [[]] * 5  # every attempt started from the prepared board
    assert _sha(board) == original_sha  # the original is untouched
    report = outcome["report"]
    manifest = json.loads((Path(report["run_dir"]) / "manifest.json").read_text())
    assert manifest["setup"]["input_preparation"]["status"] == "prepared"
    assert [m["text"] for m in manifest["setup"]["input_preparation"]["moved"]] == [
        "TOP-COPPER-NOTE", "BOTTOM-COPPER-NOTE",
    ]
    assert manifest["setup"]["input_floor"]["error_count"] == 0
    assert manifest["input"]["board_sha256"] != original_sha  # frozen the prepared revision
    assert manifest["setup"]["source_board"].endswith(board.name)
    assert manifest["setup"]["source_board_sha256"] == original_sha
    assert manifest["protocol_sha256"] == rung_runner.protocol_sha256()
    assert report["attempts_planned"] == 5 and report["attempts_run"] == 5


def test_the_plan_comes_from_the_protocol_file_for_the_chosen_backend(board, tmp_path):
    outcome = autonomy_run.run_measurement(
        rung="fixture", backend_name="krt", board=board, runs_root=tmp_path / "runs",
        parity="required", drc=_drc, availability={"available": True, "reason": None},
        backend=lambda b, p, t, log: {"status": "ok", "cause": None},
    )
    expected = rung_runner.load_protocol()["backends"]["krt"]["plan"]
    manifest = json.loads((Path(outcome["report"]["run_dir"]) / "manifest.json").read_text())
    assert manifest["plan"] == expected


def test_an_unavailable_backend_is_reported_for_the_whole_batch_and_touches_nothing(board, tmp_path):
    original = _sha(board)
    outcome = autonomy_run.run_measurement(
        rung="fixture", backend_name="freerouting", board=board, runs_root=tmp_path / "runs",
        parity="required", drc=_drc,
        availability={"available": False, "reason": "FREEROUTING_JAR_PATH is not set"},
    )
    assert outcome["status"] == "UNAVAILABLE"
    report = outcome["report"]
    assert report["status"] == "UNAVAILABLE" and report["attempts_run"] == 0
    assert report["attempts_planned"] == 5
    assert not list((tmp_path / "runs").rglob("manifest.json"))  # nothing was frozen or run
    assert _sha(board) == original
    assert not (tmp_path / "runs" / "_prepared").exists()  # input preparation did not run either


def test_main_exit_codes_and_json_summary(board, tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("FREEROUTING_JAR_PATH", raising=False)
    code = autonomy_run.main([
        "--rung", "fixture", "--backend", "freerouting", "--board", str(board),
        "--runs-root", str(tmp_path / "runs"), "--parity", "required",
    ])
    assert code == autonomy_run.EXIT_UNAVAILABLE == 3
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "UNAVAILABLE"
    assert summary["backend"] == "freerouting"
    assert "FREEROUTING_JAR_PATH" in summary["reason"]


def test_main_refuses_a_missing_board_without_writing_anything(tmp_path, capsys):
    code = autonomy_run.main([
        "--rung", "fixture", "--backend", "krt", "--board", str(tmp_path / "nope.kicad_pcb"),
        "--runs-root", str(tmp_path / "runs"), "--parity", "required",
    ])
    assert code == autonomy_run.EXIT_SETUP == 2
    assert not list((tmp_path / "runs").rglob("manifest.json"))  # only an index line, no run
