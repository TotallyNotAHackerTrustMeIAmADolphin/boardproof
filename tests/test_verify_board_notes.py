"""verify_board's informational ``notes`` reach every reader (build ticket 86).

Build ticket 50 added ``notes`` to the gate's result: hints such as a multi-unit part whose
units disagree on DNP. They never are a reason and never change the verdict. These tests
pin that the tool description and the docstring say so, and that every builder that
summarises a gate result carries ``notes`` through unchanged (the review of ticket 50 found
``pour_step._gate_summary`` and the input-floor and grading builders dropping them).

The gate's result is real (``verify_board`` with a fake DRC, or its own INCOMPLETE builder);
only a note is added to it, through the module attribute every builder calls, so each
builder is observed through its own public flow.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.client.client import Client

from kicad_mcp import autonomy_run, board_rules, input_prep, pour_step, rung_runner
from kicad_mcp import verify_board as gate
from kicad_mcp.server import build_server

from test_input_prep import GOLDEN_DIR, GOLDEN_NAME, _build_fixture

NOTE = "build ticket 86: an informational note that must reach the reader"
_EMPTY = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _drc(path, **_kwargs):
    return _EMPTY


@pytest.fixture
def noted_gate(monkeypatch):
    """The real gate, with one note added to every result it returns."""
    real = gate.verify_board

    def noted(*args, **kwargs):
        result = real(*args, **kwargs)
        return {**result, "notes": [*result["notes"], NOTE]}

    monkeypatch.setattr(gate, "verify_board", noted)


# --- the description and the docstring --------------------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_description_says_notes_never_change_the_verdict() -> None:
    async with Client(build_server(), mode="auto") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    description = " ".join(tools["verify_board"].description.split())

    assert "notes" in description
    assert "never change the verdict" in description


def test_the_docstring_says_notes_never_change_the_verdict() -> None:
    doc = " ".join((gate.verify_board.__doc__ or "").split())

    assert "notes" in doc
    assert "never change the verdict" in doc


# --- every gate-summary builder carries notes through unchanged --------------------------------


def test_the_pour_steps_gate_summary_carries_notes() -> None:
    result = gate._incomplete_result("required", None, ["a reason"], [NOTE])

    assert pour_step._gate_summary(result)["notes"] == [NOTE]


def test_the_pour_steps_gate_summary_tolerates_a_grade_without_notes() -> None:
    """Round-1 finding 3: the other builders read ``grade.get("notes", [])``; so does this one."""
    result = gate._incomplete_result("required", None, ["a reason"], [NOTE])
    del result["notes"]

    assert pour_step._gate_summary(result)["notes"] == []


def test_input_preparations_input_floor_carries_notes(noted_gate, tmp_path: Path) -> None:
    board = _build_fixture(tmp_path / "proj")
    proposal = input_prep.propose_input_preparation(board, tmp_path / "prepared")

    result = input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=_drc
    )

    assert result["status"] == "prepared", result
    assert result["input_floor"]["notes"][-1] == NOTE


def test_the_board_rules_grading_carries_notes(noted_gate, tmp_path: Path) -> None:
    shutil.copytree(GOLDEN_DIR, tmp_path / "proj")
    board = tmp_path / "proj" / GOLDEN_NAME
    proposal = board_rules.propose_board_rules(board, output_dir=tmp_path / "rules_rev", min_clearance=0.3)

    result = board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=_drc)

    assert result["status"] == "applied", result
    assert result["grading"]["notes"][-1] == NOTE


def test_the_autonomy_runs_input_floor_carries_notes(noted_gate, tmp_path: Path) -> None:
    """A board without copper text: the floor is graded by autonomy_run itself."""
    shutil.copytree(GOLDEN_DIR, tmp_path / "proj")
    board = tmp_path / "proj" / GOLDEN_NAME

    prepared = autonomy_run._prepare_input(
        board, rung="fixture", backend_name="krt", runs_root=tmp_path / "runs",
        parity="required", parity_reason=None, drc=_drc,
    )

    _, _, setup = prepared
    assert setup["input_preparation"]["status"] == "nothing_to_do"
    assert setup["input_floor"]["notes"][-1] == NOTE


def test_a_rejected_gate_result_keeps_its_notes() -> None:
    """The rung runner lowers a malformed result to INCOMPLETE; the notes stay."""
    malformed = {**gate._incomplete_result("required", None, [], [NOTE]), "verdict": "MAYBE"}

    graded = rung_runner._validate_gate_result(malformed)

    assert graded["verdict"] == "INCOMPLETE"
    assert graded["notes"] == [NOTE]


def test_a_well_formed_gate_result_keeps_its_notes_unchanged() -> None:
    result = gate._incomplete_result("required", None, ["a reason"], [NOTE])

    assert rung_runner._validate_gate_result(result)["notes"] == [NOTE]
