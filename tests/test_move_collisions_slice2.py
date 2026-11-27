"""Issue 264 slice 2: align, distribute, snap, place_footprints_at_edge and the placement optimization report
the courtyard collisions in which a footprint they moved takes part: a measurement, never a pass claim."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import placement_anchor, write_layer
from tests import test_placement_toolset, test_unmeasured_footprints
from tests.test_placement_toolset import _build_and_get_fns


@pytest.fixture
def chip_footprint(tmp_path) -> Path:
    return test_placement_toolset.chip_footprint.__wrapped__(tmp_path)


@pytest.fixture
def logo_board(tmp_path) -> Path:
    return test_unmeasured_footprints.board.__wrapped__(tmp_path)


def _tools() -> dict:
    return _build_and_get_fns(MCPServer("test"))


def _board(tmp_path: Path, chip: Path, spots: dict[str, tuple[float, float]]) -> str:
    from kicad_tools.schema import PCB

    board = PCB.create(width=100.0, height=100.0)
    for ref, (x, y) in spots.items():
        board.add_footprint_from_file(chip, ref, x, y)
    path = tmp_path / "b.kicad_pcb"
    write_layer.save_pcb(board, path)
    return str(path)


def _pairs(result: dict) -> list[list[str]]:
    return [c["between"] for c in result["collisions"]]


def _nudged(path: str, ref: str) -> tuple[float, float, float]:
    """The raw page pose of ref moved 1 mm right (stays inside the outline)."""
    part = next(p for p in placement_anchor.read_parts(write_layer.load(Path(path))) if p.reference == ref)
    return (part.at[0] + 1.0, part.at[1], part.angle)


def _cannot_run(monkeypatch) -> None:
    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(placement_anchor, "moved_collisions", boom)


def _assert_not_measured(result: dict) -> None:
    assert result["collision_check"] == "not_measured" and "boom" in result["collision_reason"], result
    assert "collisions_count" not in result and "collisions" not in result


# --- align ----------------------------------------------------------------------------------------


def test_align_reports_the_collision_it_causes(tmp_path, chip_footprint):
    path = _board(tmp_path, chip_footprint, {"A": (10, 10), "M": (10, 20), "Z": (60, 60)})
    result = _tools()["align_footprints"](path, ["A", "M"], "y", "center")
    assert result["status"] == "ok" and result["collision_check"] == "measured", result
    assert _pairs(result) == [["A", "M"]] and result["collisions"][0]["existed_before"] is False


def test_align_only_lists_collisions_of_a_moved_footprint(tmp_path, chip_footprint):
    path = _board(tmp_path, chip_footprint, {"A": (10, 10), "M": (30, 10.4), "P": (60, 60), "Q": (60, 60)})
    result = _tools()["align_footprints"](path, ["A", "M"], "y", "center")
    assert result["collisions_count"] == 0 and result["collisions"] == [], result  # P and Q collide, neither moved


def test_align_cannot_run_says_not_measured(tmp_path, chip_footprint, monkeypatch):
    path = _board(tmp_path, chip_footprint, {"A": (10, 10), "M": (10, 20)})
    _cannot_run(monkeypatch)
    result = _tools()["align_footprints"](path, ["A", "M"], "y", "center")
    assert result["status"] == "ok"
    _assert_not_measured(result)


def test_align_cut_at_20_and_detail(tmp_path, chip_footprint):
    spots = {f"S{i}": (10.0, 10.0) for i in range(21)}
    spots["M"] = (10.0, 50.0)
    spots["A"] = (60.0, 10.0)
    path = _board(tmp_path, chip_footprint, spots)
    short = _tools()["align_footprints"](path, ["A", "M"], "y", "center")
    assert short["collisions_count"] == 21 and len(short["collisions"]) == 20 and short["collisions_truncated"] is True
    path2 = _board(tmp_path, chip_footprint, spots)
    full = _tools()["align_footprints"](path2, ["A", "M"], "y", "center", detail=True)
    assert len(full["collisions"]) == 21 and "collisions_truncated" not in full


# --- distribute -----------------------------------------------------------------------------------


def test_distribute_reports_the_collision_it_causes(tmp_path, chip_footprint):
    path = _board(tmp_path, chip_footprint, {"A": (10, 10), "M": (20, 10), "B": (40, 10), "D": (25, 10)})
    result = _tools()["distribute_footprints"](path, ["A", "M", "B"], "x")
    assert result["status"] == "ok" and _pairs(result) == [["D", "M"]], result


def test_distribute_cannot_run_says_not_measured(tmp_path, chip_footprint, monkeypatch):
    path = _board(tmp_path, chip_footprint, {"A": (10, 10), "M": (20, 10), "B": (40, 10), "D": (25, 10)})
    _cannot_run(monkeypatch)
    _assert_not_measured(_tools()["distribute_footprints"](path, ["A", "M", "B"], "x"))


# --- snap -----------------------------------------------------------------------------------------


def test_snap_reports_the_collision_it_causes(tmp_path, chip_footprint):
    path = _board(tmp_path, chip_footprint, {"M": (23, 20), "D": (20, 20)})
    result = _tools()["snap_footprints_to_grid"](path, ["M"], 10.0)
    assert result["status"] == "ok" and result["moved_count"] == 1, result
    assert _pairs(result) == [["D", "M"]] and result["collisions"][0]["existed_before"] is False


def test_snap_cannot_run_says_not_measured(tmp_path, chip_footprint, monkeypatch):
    path = _board(tmp_path, chip_footprint, {"M": (23, 20), "D": (20, 20)})
    _cannot_run(monkeypatch)
    _assert_not_measured(_tools()["snap_footprints_to_grid"](path, ["M"], 10.0))


# --- place_footprints_at_edge ---------------------------------------------------------------------


def test_edge_answer_carries_the_collision_fields_and_names_the_logo(logo_board):
    out = _tools()["place_footprints_at_edge"](str(logo_board), [{"reference": "R1", "edge": "left", "offset_mm": 1.0}])
    assert out["status"] == "ok" and out["collision_check"] == "measured", out
    assert out["collisions_count"] == 0 and out["collisions_not_measured"] == ["LOGO1"], out


def test_edge_cannot_run_says_not_measured(logo_board, monkeypatch):
    _cannot_run(monkeypatch)
    out = _tools()["place_footprints_at_edge"](str(logo_board), [{"reference": "R1", "edge": "left", "offset_mm": 1.0}])
    assert out["status"] == "ok"
    _assert_not_measured(out)


# --- placement optimization -----------------------------------------------------------------------


@pytest.fixture
def stub_placer(monkeypatch):
    from boardproof.routers import kicad_placer_tools as placer

    state = {"delta": {}}
    monkeypatch.setattr(placer, "placer_problem", lambda: None)
    monkeypatch.setattr(placer, "run_place_simple",
                        lambda trial, out, refs, seed=0, timeout=0: {"output_pcb": trial, "report": {}, "elapsed_s": 0, "router_version": "t"})
    monkeypatch.setattr(placer, "placement_delta", lambda trial, out: state["delta"])
    return state


def test_optimization_propose_and_confirm_carry_the_collision_fields(tmp_path, chip_footprint, stub_placer):
    path = _board(tmp_path, chip_footprint, {"M": (30, 30), "D": (60, 60)})
    tools = _tools()
    stub_placer["delta"] = {"M": _nudged(path, "M")}
    proposal = tools["propose_placement_optimization"](path)
    assert proposal["status"] == "pending_confirmation", proposal
    assert proposal["collision_check"] == "measured" and proposal["collisions_count"] == 0, proposal
    done = tools["confirm_placement_optimization"](proposal["revision_token"])
    assert done["status"] == "confirmed" and done["collision_check"] == "measured" and done["collisions_count"] == 0, done


def test_optimization_cannot_run_says_not_measured(tmp_path, chip_footprint, stub_placer, monkeypatch):
    path = _board(tmp_path, chip_footprint, {"M": (30, 30), "D": (60, 60)})
    tools = _tools()
    stub_placer["delta"] = {"M": _nudged(path, "M")}
    _cannot_run(monkeypatch)
    proposal = tools["propose_placement_optimization"](path)
    assert proposal["status"] == "pending_confirmation"
    _assert_not_measured(proposal)
    _assert_not_measured(tools["confirm_placement_optimization"](proposal["revision_token"]))


# --- existed_before and the written revision (review follow-ups) ----------------------------------


def test_align_marks_a_kept_overlap_as_existed_before(tmp_path, chip_footprint):
    path = _board(tmp_path, chip_footprint, {"A": (10, 10), "M": (10.5, 10)})  # overlap now, M stays on A's row
    result = _tools()["align_footprints"](path, ["A", "M"], "y", "center")
    assert _pairs(result) == [["A", "M"]] and result["collisions"][0]["existed_before"] is True, result


def test_distribute_marks_kept_and_new_overlaps(tmp_path, chip_footprint):
    kept = _board(tmp_path, chip_footprint, {"A": (10, 10), "M": (25.5, 10), "B": (40, 10), "D": (25, 10)})
    result = _tools()["distribute_footprints"](kept, ["A", "M", "B"], "x")
    assert _pairs(result) == [["D", "M"]] and result["collisions"][0]["existed_before"] is True, result
    new = _board(tmp_path, chip_footprint, {"A": (10, 10), "M": (20, 10), "B": (40, 10), "D": (25, 10)})
    assert _tools()["distribute_footprints"](new, ["A", "M", "B"], "x")["collisions"][0]["existed_before"] is False


def test_snap_marks_a_kept_overlap_as_existed_before(tmp_path, chip_footprint):
    path = _board(tmp_path, chip_footprint, {"M": (20.5, 20), "D": (20, 20)})
    result = _tools()["snap_footprints_to_grid"](path, ["M"], 10.0)
    assert _pairs(result) == [["D", "M"]] and result["collisions"][0]["existed_before"] is True, result


def test_confirm_lists_the_collisions_of_the_written_revision(tmp_path, chip_footprint, stub_placer, monkeypatch):
    path = _board(tmp_path, chip_footprint, {"M": (30, 30), "D": (60, 60)})
    tools = _tools()
    stub_placer["delta"] = {"M": _nudged(path, "M")}
    proposal = tools["propose_placement_optimization"](path)
    assert proposal["status"] == "pending_confirmation", proposal
    d = next(p for p in placement_anchor.read_parts(write_layer.load(Path(path))) if p.reference == "D")
    real = placement_anchor.write_poses
    # the written file differs from the proposed pose: M lands on D
    monkeypatch.setattr(placement_anchor, "write_poses", lambda p, targets: real(p, {r: (d.at[0], d.at[1], t[2]) for r, t in targets.items()}))
    done = tools["confirm_placement_optimization"](proposal["revision_token"])
    assert done["status"] == "confirmed", done
    assert _pairs(done) == [["D", "M"]] and done["collisions"][0]["existed_before"] is False, done
