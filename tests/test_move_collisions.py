"""Issue 264 slice 1: place_footprints, propose/confirm_move_group and pack_footprints_densely report the
courtyard collisions in which a footprint they moved takes part: a measurement, never a pass claim."""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import group_edit, placement_anchor, write_layer
from boardproof.toolsets import placement as placement_toolset
from tests import test_group_edit, test_placement_toolset, test_unmeasured_footprints
from tests.test_placement_toolset import _build_and_get_fns


@pytest.fixture
def chip_footprint(tmp_path) -> Path:
    return test_placement_toolset.chip_footprint.__wrapped__(tmp_path)


@pytest.fixture
def board_with_three_chips(tmp_path, chip_footprint) -> str:
    return test_placement_toolset.board_with_three_chips.__wrapped__(tmp_path, chip_footprint)


@pytest.fixture
def group_board(tmp_path) -> Path:
    return test_group_edit.board.__wrapped__(tmp_path)


@pytest.fixture
def logo_board(tmp_path) -> Path:
    return test_unmeasured_footprints.board.__wrapped__(tmp_path)


def _tools() -> dict:
    return _build_and_get_fns(MCPServer("test"))


def _pairs(result: dict) -> list[list[str]]:
    return [c["between"] for c in result["collisions"]]


def _stack_board(tmp_path: Path, chip_footprint: Path, n: int) -> str:
    """n chips at one spot (they collide with each other) and a mover M1 elsewhere."""
    from kicad_tools.schema import PCB

    board = PCB.create(width=100.0, height=100.0)
    for i in range(n):
        board.add_footprint_from_file(chip_footprint, f"S{i}", 10.0, 10.0)
    board.add_footprint_from_file(chip_footprint, "M1", 60.0, 60.0)
    path = tmp_path / "stack.kicad_pcb"
    write_layer.save_pcb(board, path)
    return str(path)


# --- place_footprints -----------------------------------------------------------------------------


def test_place_footprints_reports_a_move_into_a_collision(board_with_three_chips):
    result = _tools()["place_footprints"](board_with_three_chips, [{"reference": "R2", "x": 10.0, "y": 10.0}])
    assert result["status"] == "ok", result
    assert result["collision_check"] == "measured"
    assert _pairs(result) == [["R1", "R2"]] and result["collisions_count"] == 1
    assert result["collisions"][0]["existed_before"] is False


def test_place_footprints_with_no_collision_says_zero_measured_not_ok(board_with_three_chips):
    result = _tools()["place_footprints"](board_with_three_chips, [{"reference": "R2", "x": 80.0, "y": 80.0}])
    assert result["collision_check"] == "measured" and result["collisions_count"] == 0 and result["collisions"] == []


def test_only_collisions_of_a_moved_footprint_are_listed(tmp_path, chip_footprint):
    path = _stack_board(tmp_path, chip_footprint, 3)  # S0, S1, S2 collide with each other
    result = _tools()["place_footprints"](path, [{"reference": "M1", "x": 80.0, "y": 80.0}])
    assert result["collisions_count"] == 0, result


def test_a_collision_that_existed_before_is_marked(tmp_path, chip_footprint):
    path = _stack_board(tmp_path, chip_footprint, 1)
    # S0 moves 0.1 mm: it overlapped nothing; then M1 moves onto it, then S0 moves again while still overlapping
    tools = _tools()
    tools["place_footprints"](path, [{"reference": "M1", "x": 10.0, "y": 10.0}])
    result = tools["place_footprints"](path, [{"reference": "M1", "x": 10.1, "y": 10.0}])
    assert result["collisions"][0]["existed_before"] is True, result


def test_a_new_footprint_that_lands_on_another_is_reported(tmp_path, chip_footprint, board_with_three_chips):
    result = _tools()["place_footprints"](
        board_with_three_chips, [{"reference": "R9", "library_id": str(chip_footprint), "x": 10.0, "y": 10.0}])
    assert _pairs(result) == [["R1", "R9"]], result


def test_the_list_is_cut_at_20_and_detail_lists_all(tmp_path, chip_footprint):
    path = _stack_board(tmp_path, chip_footprint, 21)
    spec = [{"reference": "M1", "x": 10.0, "y": 10.0}]
    short = _tools()["place_footprints"](path, spec)
    assert short["collisions_count"] == 21 and len(short["collisions"]) == 20 and short["collisions_truncated"] is True
    full = _tools()["place_footprints"](path, [{"reference": "M1", "x": 10.0, "y": 10.0, "rotation": 0}], detail=True)
    assert len(full["collisions"]) == 21 and "collisions_truncated" not in full


def test_a_partial_answer_keeps_every_collision(tmp_path, chip_footprint):
    path = _stack_board(tmp_path, chip_footprint, 21)
    result = _tools()["place_footprints"](path, [{"reference": "M1", "x": 10.0, "y": 10.0}, {"x": 1, "y": 1}])
    assert result["status"] == "partial" and len(result["collisions"]) == 21, result


def test_an_unmeasured_footprint_is_named_never_read_as_clear(logo_board):
    result = _tools()["place_footprints"](str(logo_board), [{"reference": "R1", "x": 150.0, "y": 90.0}])
    assert result["collisions_not_measured"] == ["LOGO1"], result


def test_a_check_that_cannot_run_says_not_measured_and_has_no_count(board_with_three_chips, monkeypatch):
    monkeypatch.setattr(placement_anchor, "read_parts", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    result = _tools()["place_footprints"](board_with_three_chips, [{"reference": "R2", "x": 10.0, "y": 10.0}])
    assert result["status"] == "ok" and result["collision_check"] == "not_measured", result
    assert "collisions_count" not in result and "collisions" not in result and "boom" in result["collision_reason"]


# --- pack_footprints_densely ----------------------------------------------------------------------


def test_pack_reports_the_collisions_it_causes(board_with_three_chips):
    tools = _tools()
    tools["align_footprints"](board_with_three_chips, ["R1", "R2", "R3"], "y", "center")
    result = tools["pack_footprints_densely"](board_with_three_chips, ["R1", "R2", "R3"], min_clearance_mm=-0.5)
    assert result["collision_check"] == "measured" and result["collisions_count"] >= 1, result
    ok = tools["pack_footprints_densely"](board_with_three_chips, ["R1", "R2", "R3"], min_clearance_mm=0.5)
    assert ok["collisions_count"] == 0 and ok["collision_check"] == "measured"


# --- move_group -----------------------------------------------------------------------------------


def test_move_group_propose_and_confirm_report_the_collision(group_board, tmp_path):
    # R1 (148.5, 85.5) moves 7.5 mm left onto J1 (141, 87.5)
    found = group_edit.propose_move_group(group_board, ["pair"], -7.5, 0.0, tmp_path / "out")
    assert found["status"] == "proposed" and found["collision_check"] == "measured", found
    assert _pairs(found) == [["J1", "R1"]] and found["collisions_count"] == 1
    assert found["collisions"][0]["existed_before"] is False
    done = group_edit.confirm_move_group(group_board, found["revision_token"])
    assert done["status"] == "moved" and _pairs(done) == [["J1", "R1"]], done
    assert done["collision_check"] == "measured"


def test_move_group_without_a_collision_counts_zero(group_board, tmp_path):
    found = group_edit.propose_move_group(group_board, ["pair"], 0.0, 10.0, tmp_path / "out")
    assert found["collisions_count"] == 0 and found["collisions"] == []


def test_move_group_names_what_it_could_not_measure(logo_board, tmp_path):
    found = group_edit.propose_move_group(logo_board, ["pair"], 1.0, 2.0, tmp_path / "out")
    assert found["collisions_not_measured"] == ["LOGO1"], found


def test_a_refused_move_group_proposal_carries_no_collision_fields(group_board, tmp_path):
    found = group_edit.propose_move_group(group_board, ["outer"], 1.0, 0.0, tmp_path / "out")
    assert found["status"] == "refused" and "collisions" not in found


# --- review follow-ups: the cut, the full lists of decision answers ---------------------------------


def _crowded(pcb: Path, n: int = 21) -> Path:
    """n copies of J1 stacked on J1, so a group member moved onto J1 collides with n + 1 footprints."""
    import uuid

    root = write_layer.load(pcb)
    j1 = test_group_edit._fp(root, "J1")
    for i in range(n):
        copy = write_layer.parse_string(write_layer.serialise(j1))
        for c in copy.children:
            if not c.is_atom and c.name == "uuid":
                c.children[0] = write_layer.string(str(uuid.uuid4()))
            if not c.is_atom and c.name in ("property", "fp_text") and str(c.children[0].value).lower() == "reference":
                c.children[1] = write_layer.string(f"X{i}")
        root.append(copy)
    write_layer.save(root, pcb)
    return pcb


def test_a_refused_group_keeps_every_collision_of_the_proposal(group_board, tmp_path):
    _crowded(group_board)
    found = group_edit.propose_move_group(group_board, ["pair", "outer"], -7.5, 0.0, tmp_path / "out")
    assert found["status"] == "proposed" and found["refused"], found
    assert found["collisions_count"] == 22 and len(found["collisions"]) == 22 and "collisions_truncated" not in found
    clean = group_edit.propose_move_group(group_board, ["pair"], -7.5, 0.0, tmp_path / "out2")
    assert len(clean["collisions"]) == 20 and clean["collisions_truncated"] is True


def test_confirm_move_group_cuts_at_20_and_detail_lists_all(group_board, tmp_path):
    _crowded(group_board)
    a = group_edit.propose_move_group(group_board, ["pair"], -7.5, 0.0, tmp_path / "a")
    short = group_edit.confirm_move_group(group_board, a["revision_token"])
    assert short["collisions_count"] == 22 and len(short["collisions"]) == 20 and short["collisions_truncated"] is True
    b = group_edit.propose_move_group(group_board, ["pair"], -7.5, 0.0, tmp_path / "b")
    full = group_edit.confirm_move_group(group_board, b["revision_token"], detail=True)
    assert len(full["collisions"]) == 22 and "collisions_truncated" not in full


def _packed_row(tmp_path: Path, chip_footprint: Path) -> str:
    from kicad_tools.schema import PCB

    board = PCB.create(width=100.0, height=100.0)
    for i in range(22):
        board.add_footprint_from_file(chip_footprint, f"R{i + 1}", 10.0 + 3.0 * i, 10.0)
    path = tmp_path / "row.kicad_pcb"
    write_layer.save_pcb(board, path)
    return str(path)


def test_an_unclean_pack_lists_every_collision(tmp_path, chip_footprint):
    path = _packed_row(tmp_path, chip_footprint)
    refs = [f"R{i + 1}" for i in range(22)]
    result = _tools()["pack_footprints_densely"](path, refs, min_clearance_mm=-0.9)
    assert result["status"] == "partial" and result["collisions_count"] > 20, result
    assert len(result["collisions"]) == result["collisions_count"] and "collisions_truncated" not in result


def test_a_pack_whose_verification_failed_lists_every_collision(tmp_path, chip_footprint, monkeypatch):
    path = _packed_row(tmp_path, chip_footprint)
    refs = [f"R{i + 1}" for i in range(22)]
    real, calls = write_layer.load_pcb, []

    def second_fails(p):
        calls.append(p)
        if len(calls) > 1:
            raise RuntimeError("no verification")
        return real(p)

    monkeypatch.setattr(write_layer, "load_pcb", second_fails)
    result = _tools()["pack_footprints_densely"](path, refs, min_clearance_mm=-0.9)
    assert "verification failed" in result["reason"] and result["collisions_count"] > 20, result
    assert len(result["collisions"]) == result["collisions_count"]


@pytest.mark.parametrize(("n", "cut"), [(0, False), (20, False), (21, True)])
def test_the_cut_boundary(n, cut):
    report = {"collision_check": "measured", "collisions": [{"between": [f"A{i}", "B"], "existed_before": False} for i in range(n)]}
    out = placement_anchor.collision_fields(report, False)
    assert out["collisions_count"] == n and len(out["collisions"]) == min(n, 20) and out.get("collisions_truncated", False) is cut
    assert len(placement_anchor.collision_fields(report, True)["collisions"]) == n


def test_move_group_confirm_detail_lists_all(group_board, tmp_path):
    found = group_edit.propose_move_group(group_board, ["pair"], -7.5, 0.0, tmp_path / "out")
    done = group_edit.confirm_move_group(group_board, found["revision_token"], detail=True)
    assert done["collisions_count"] == 1 and len(done["collisions"]) == 1


# --- the helper and the wording -------------------------------------------------------------------


def test_the_report_text_makes_no_claim():
    text = placement_anchor.COLLISION_NOTE.lower()
    for word in ("routed", "done", "complete", "finished", "clean", "verified", "ready", "connected", "passes"):
        assert word not in text


@pytest.mark.parametrize("tool", ["place_footprints", "pack_footprints_densely", "propose_move_group", "confirm_move_group"])
def test_the_registered_descriptions_name_the_fields(tool):
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    text = {t.name: t.description for t in mcp._tool_manager.list_tools()}[tool]  # type: ignore[attr-defined]
    assert placement_anchor.COLLISION_NOTE in text and "collisions_count" in text and "detail=true" in text
    for word in ("routed", "done", "complete", "finished", "clean", "verified", "ready", "connected", "passes"):
        assert word not in placement_anchor.COLLISION_NOTE.lower().split()
