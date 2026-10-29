"""Tests for the placement toolset (archived backlog Phase 3, P3-01/P3-02/P3-03).

Uses real generated IPC-7351 footprints (via footprint_gen.generate_footprint,
already tested elsewhere) placed onto a real PCB via kicad_tools.schema.PCB,
so alignment/distribution/packing exercise real courtyard geometry, not
synthetic bounding boxes.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import write_layer
from kicad_mcp.sourcing.footprint_gen import generate_footprint
from kicad_mcp.toolsets import placement as placement_toolset


def _build_and_get_fns(mcp: MCPServer) -> dict:
    placement_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def chip_footprint(tmp_path) -> Path:
    out = tmp_path / "0402.kicad_mod"
    result = generate_footprint("chip", str(out), size="0402")
    assert result["status"] == "ok", result
    return Path(result["path"])


@pytest.fixture
def board_with_three_chips(tmp_path, chip_footprint) -> str:
    from kicad_tools.schema import PCB

    board = PCB.create(width=100.0, height=100.0)
    board.add_footprint_from_file(chip_footprint, "R1", 10.0, 10.0)
    board.add_footprint_from_file(chip_footprint, "R2", 30.0, 20.0)
    board.add_footprint_from_file(chip_footprint, "R3", 50.0, 5.0)
    pcb_path = tmp_path / "board.kicad_pcb"
    write_layer.save_pcb(board, pcb_path)
    return str(pcb_path)


def test_build_registers_expected_tools():
    mcp = MCPServer("test")
    names = placement_toolset.build(mcp)
    assert set(names) == {
        "place_footprints",
        "move_reference_designators_to_layer",
        "align_footprints",
        "distribute_footprints",
        "snap_footprints_to_grid",
        "pack_footprints_densely",
        "propose_autoplacement",
        "confirm_autoplacement",
        "propose_placement_optimization",
        "confirm_placement_optimization",
        "propose_remove_graphic_items",
        "confirm_remove_graphic_items",
    }


def test_place_footprints_adds_and_moves(tmp_path, chip_footprint):
    from kicad_tools.schema import PCB

    board = PCB.create(width=100.0, height=100.0)
    pcb_path = tmp_path / "b.kicad_pcb"
    write_layer.save_pcb(board, pcb_path)

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    result = tools["place_footprints"](
        str(pcb_path),
        [{"reference": "R1", "library_id": str(chip_footprint), "x": 10.0, "y": 10.0}],
    )
    assert result["status"] == "ok", result
    assert result["placed"] == ["R1"]

    move_result = tools["place_footprints"](str(pcb_path), [{"reference": "R1", "x": 20.0, "y": 20.0}])
    assert move_result["status"] == "ok"
    assert move_result["moved"] == ["R1"]

    reloaded = PCB.load(pcb_path)
    assert reloaded.get_footprint("R1").position == (20.0, 20.0)


def test_place_footprints_missing_library_id_reports_error(tmp_path):
    from kicad_tools.schema import PCB

    board = PCB.create()
    pcb_path = tmp_path / "b.kicad_pcb"
    write_layer.save_pcb(board, pcb_path)

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["place_footprints"](str(pcb_path), [{"reference": "U1", "x": 0, "y": 0}])
    assert result["status"] == "error"
    assert result["errors"]


def test_align_footprints_min_edge_uses_real_courtyard(board_with_three_chips):
    from kicad_tools.schema import PCB

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    result = tools["align_footprints"](board_with_three_chips, ["R1", "R2", "R3"], "y", "min")
    assert result["status"] == "ok", result
    assert all(m["used_courtyard"] for m in result["moved"]), "0402 IPC footprint should have real courtyard graphics"

    board = PCB.load(board_with_three_chips)
    from kicad_tools.geometry.courtyard import _courtyard_polygon
    from shapely.geometry import Polygon

    min_ys = []
    for ref in ["R1", "R2", "R3"]:
        fp = board.get_footprint(ref)
        poly = _courtyard_polygon(fp, "F", Polygon)
        min_ys.append(poly.bounds[1])
    assert min_ys[0] == pytest.approx(min_ys[1], abs=1e-6)
    assert min_ys[1] == pytest.approx(min_ys[2], abs=1e-6)


def test_align_footprints_center_axis_x(board_with_three_chips):
    from kicad_tools.schema import PCB

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["align_footprints"](board_with_three_chips, ["R1", "R2", "R3"], "x", "center")
    assert result["status"] == "ok"

    board = PCB.load(board_with_three_chips)
    xs = [board.get_footprint(r).position[0] for r in ["R1", "R2", "R3"]]
    assert xs[0] == pytest.approx(xs[1], abs=1e-6)
    assert xs[1] == pytest.approx(xs[2], abs=1e-6)


def test_align_footprints_rejects_unknown_ref(board_with_three_chips):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["align_footprints"](board_with_three_chips, ["R1", "R99"], "x", "center")
    assert result["status"] == "error"


def test_distribute_footprints_evenly_spaces_centers(board_with_three_chips):
    from kicad_tools.schema import PCB

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["distribute_footprints"](board_with_three_chips, ["R1", "R2", "R3"], "x")
    assert result["status"] == "ok", result

    board = PCB.load(board_with_three_chips)
    xs = [board.get_footprint(r).position[0] for r in ["R1", "R2", "R3"]]
    gap1 = xs[1] - xs[0]
    gap2 = xs[2] - xs[1]
    assert gap1 == pytest.approx(gap2, abs=1e-6)


def test_snap_footprints_to_grid(board_with_three_chips):
    from kicad_tools.schema import PCB

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["snap_footprints_to_grid"](board_with_three_chips, ["R1", "R2", "R3"], 5.0)
    assert result["status"] == "ok", result

    board = PCB.load(board_with_three_chips)
    for ref in ["R1", "R2", "R3"]:
        x, y = board.get_footprint(ref).position
        assert x % 5.0 == pytest.approx(0.0, abs=1e-6)
        assert y % 5.0 == pytest.approx(0.0, abs=1e-6)


def test_snap_footprints_to_grid_rejects_nonpositive_grid(board_with_three_chips):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["snap_footprints_to_grid"](board_with_three_chips, ["R1"], 0.0)
    assert result["status"] == "error"


def test_pack_footprints_densely_produces_collision_free_result(board_with_three_chips):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    align_result = tools["align_footprints"](board_with_three_chips, ["R1", "R2", "R3"], "y", "center")
    assert align_result["status"] == "ok"

    result = tools["pack_footprints_densely"](board_with_three_chips, ["R1", "R2", "R3"], min_clearance_mm=0.2)
    assert result["status"] == "ok", result
    assert result["post_pack_violation_count"] == 0


def test_pack_footprints_densely_rejects_single_ref(board_with_three_chips):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["pack_footprints_densely"](board_with_three_chips, ["R1"])
    assert result["status"] == "error"


def test_move_reference_designators_to_layer(board_with_three_chips):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["move_reference_designators_to_layer"](board_with_three_chips, ["R1"], "F.Fab")
    assert result["status"] == "ok", result
    assert result["moved_count"] >= 1


def test_propose_then_confirm_autoplacement(board_with_three_chips):
    from kicad_tools.schema import PCB

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    proposal = tools["propose_autoplacement"](board_with_three_chips, ["R1", "R2", "R3"])
    assert proposal["status"] == "pending_confirmation", proposal
    token = proposal["revision_token"]
    proposed_positions = proposal["proposed_positions"]
    assert set(proposed_positions) == {"R1", "R2", "R3"}

    before = {r: PCB.load(board_with_three_chips).get_footprint(r).position for r in ["R1", "R2", "R3"]}

    confirm = tools["confirm_autoplacement"](token)
    assert confirm["status"] == "confirmed", confirm

    board = PCB.load(board_with_three_chips)
    for ref in ["R1", "R2", "R3"]:
        pos = board.get_footprint(ref).position
        assert pos != before[ref] or proposed_positions[ref]["x"] == before[ref][0]

    # A second confirm with the same token must fail -- consumed already.
    second = tools["confirm_autoplacement"](token)
    assert second["status"] == "error"


def test_propose_autoplacement_does_not_modify_board_until_confirmed(board_with_three_chips):
    from kicad_tools.schema import PCB

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    before = {r: PCB.load(board_with_three_chips).get_footprint(r).position for r in ["R1", "R2", "R3"]}
    tools["propose_autoplacement"](board_with_three_chips, ["R1", "R2", "R3"])
    after = {r: PCB.load(board_with_three_chips).get_footprint(r).position for r in ["R1", "R2", "R3"]}
    assert before == after


def test_confirm_autoplacement_unknown_token():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["confirm_autoplacement"]("does-not-exist")
    assert result["status"] == "error"


def test_confirm_placement_optimization_unknown_token():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["confirm_placement_optimization"]("does-not-exist")
    assert result["status"] == "error"


# ---------------------------------------------------------------------------
# propose_placement_optimization / confirm_placement_optimization: real
# KiCadRoutingTools placer (krt-capability-suite ticket 08), gated behind a
# real install (the needs_krt marker, build ticket 53) -- skips with the probe's
# own reason otherwise and fails in the gate run, never fakes a repair. Uses the real stress fixture (ticket 05 of the same map),
# NOT the golden-path board -- see that ticket for why it's too simple to
# exercise this at all.
# ---------------------------------------------------------------------------

STRESS_PCB = Path(__file__).parent / "fixtures" / "krt_stress_congestion.kicad_pcb"


@pytest.mark.needs_krt
class TestRealPlacementOptimizationOnStressFixture:
    def test_propose_reports_real_before_after_drc_evidence(self, tmp_path):
        import shutil

        board = tmp_path / STRESS_PCB.name
        shutil.copy(STRESS_PCB, board)
        for suffix in (".kicad_pro", ".kicad_prl"):
            sibling = STRESS_PCB.with_suffix(suffix)
            if sibling.is_file():
                shutil.copy(sibling, board.with_suffix(suffix))

        mcp = MCPServer("test")
        tools = _build_and_get_fns(mcp)

        result = tools["propose_placement_optimization"](str(board), rounds=2, timeout_seconds=120)

        assert result["status"] in ("pending_confirmation", "complete"), result
        assert "before_drc" in result
        if result["status"] == "pending_confirmation":
            assert "after_drc" in result
            assert result["footprints_moved"] == len(result["proposed_positions"])
            # The real board file must be untouched by propose -- only confirm mutates it.
            from kicad_tools.schema import PCB

            reloaded = PCB.load(board)
            original = PCB.load(STRESS_PCB)
            for fp in original.footprints:
                assert reloaded.get_footprint(fp.reference).position == fp.position
