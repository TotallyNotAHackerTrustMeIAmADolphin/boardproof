"""Short default answers of align_footprints, distribute_footprints, snap_footprints_to_grid,
place_footprints_at_edge, the nets block of add_symbols and confirm_schematic_import (GitHub issue 261,
follow-up of 230): the first 20 items and counts by default, every item with ``detail=true``; what a caller
decides on (locked_skipped, not_measured, refusals, the existing-connectivity lists) is never behind it."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import output_detail, schematic_import, write_layer
from boardproof.sourcing.footprint_gen import generate_footprint
from boardproof.toolsets import placement as placement_toolset
from boardproof.toolsets import schematic as schematic_toolset

BOUNDARY = [(0, False), (20, False), (21, True)]
N = 150


@pytest.fixture
def chip(tmp_path) -> Path:
    result = generate_footprint("chip", str(tmp_path / "0402.kicad_mod"), size="0402")
    assert result["status"] == "ok", result
    return Path(result["path"])


@pytest.fixture
def big_board(tmp_path, chip) -> str:
    from kicad_tools.schema import PCB

    board = PCB.create(width=300.0, height=300.0)
    for i in range(N):
        board.add_footprint_from_file(chip, f"R{i + 1}", 10.0 + (i % 15) * 15.0, 10.0 + (i // 15) * 15.0)
    (tmp_path / "proj").mkdir()
    path = tmp_path / "proj" / "big.kicad_pcb"
    write_layer.save_pcb(board, path)
    return str(path)


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _refs(n: int) -> list[str]:
    return [f"R{i + 1}" for i in range(n)]


@pytest.mark.parametrize("tool,extra", [("align_footprints", {"axis": "x", "edge": "min"}),
                                       ("distribute_footprints", {"axis": "x"}),
                                       ("snap_footprints_to_grid", {"grid_mm": 0.5})])
@pytest.mark.parametrize("n", [3, 20, 21])
def test_moved_boundary_20_21(big_board, tools, tool, extra, n) -> None:
    refs = _refs(n)
    out = tools[tool](big_board, refs, **extra)
    assert out["status"] == "ok", out
    assert out["moved_count"] == n and len(out["moved"]) == min(n, 20) and out.get("moved_truncated", False) is (n > 20)
    full = tools[tool](big_board, refs, detail=True, **extra)
    assert len(full["moved"]) == n and "moved_truncated" not in full
    assert out["locked_skipped"] == full["locked_skipped"] == []


def test_zero_moved_has_a_count(big_board, tools) -> None:
    tools["lock_footprints"](big_board, _refs(3))
    out = tools["align_footprints"](big_board, _refs(3), axis="x", edge="min")
    assert out["moved"] == [] and out["moved_count"] == 0 and out["locked_skipped"] == _refs(3)


def test_locked_skipped_is_never_cut(big_board, tools) -> None:
    tools["lock_footprints"](big_board, _refs(30)[1:])
    out = tools["snap_footprints_to_grid"](big_board, _refs(30), grid_mm=0.5)
    assert out["locked_skipped"] == _refs(30)[1:] and len(out["locked_skipped"]) == 29


def test_edge_default_cut_and_detail(big_board, tools) -> None:
    specs = [{"reference": r, "edge": "top", "offset_mm": 1.0, "along_mm": 10.0 + 11.0 * i} for i, r in enumerate(_refs(25))]
    out = tools["place_footprints_at_edge"](big_board, specs)
    if out["status"] == "error" and out.get("code") == "no_outline":
        pytest.skip("fixture has no outline")
    assert out["status"] == "ok", out
    assert len(out["moved"]) == 20 and out["moved_count"] == 25 and out["moved_truncated"] is True
    full = tools["place_footprints_at_edge"](big_board, specs, detail=True)
    assert len(full["moved"]) == 25 and "moved_truncated" not in full


def test_registered_text_names_detail() -> None:
    mcp = MCPServer("t")
    placement_toolset.build(mcp)
    by = {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    for name in ("align_footprints", "distribute_footprints", "snap_footprints_to_grid", "place_footprints_at_edge"):
        assert "detail=true" in by[name].description and "moved_count" in by[name].description, name
        assert by[name].parameters["properties"]["detail"]["default"] is False


# --- add_symbols nets block ------------------------------------------------------------------------------------------


def _nets(n_created: int, n_extended: int = 0) -> dict:
    return {"nets_before": 0, "nets_after": n_created, "changed": True,
            "created": [{"net": f"N{i}", "pins": [f"R{i}.1"]} for i in range(n_created)],
            "merged": [{"nets": ["A", "B"], "now": "A", "pins": []}] * 30, "renamed": [{"net": "x", "now": "y", "pins": []}] * 25,
            "extended": [{"net": f"E{i}", "added_pins": ["U1.1"]} for i in range(n_extended)],
            "split": [], "removed": [], "truncated": False, "scope": "project"}


@pytest.mark.parametrize("n,cut", BOUNDARY)
def test_nets_created_cut_at_20_extended_full(n: int, cut: bool) -> None:
    out = schematic_toolset._nets_shown(_nets(n, n), False)
    assert len(out["created"]) == min(n, 20) and out["created_count"] == n and out.get("created_truncated", False) is cut
    assert len(out["extended"]) == n and "extended_count" not in out and "extended_truncated" not in out
    assert out["nets_after"] == n and out["scope"] == "project"


def test_nets_decision_lists_and_detail_stay_full() -> None:
    nets = _nets(30, 30)
    out = schematic_toolset._nets_shown(nets, False)
    assert len(out["extended"]) == 30 and len(out["merged"]) == 30 and len(out["renamed"]) == 25 and "merged_count" not in out and out["changed"] is True
    assert schematic_toolset._nets_shown(nets, True) is nets


# --- confirm_schematic_import ----------------------------------------------------------------------------------------


def _plan(n: int, **extra) -> dict:
    return {"footprints": [{"reference": f"R{i}", "library_id": "R:R", "source": "x" * 80, "staging_at": [i, 0], "file_sha256": "0" * 64} for i in range(n)],
            "already_present": [f"C{i}" for i in range(n)], "footprint_mismatches": [], "orphans": [], "renetted": [], "stale_nets": {},
            "staging": {"origin": [0, 0]}, "nets": {f"N{i}": [f"R{i}.1"] for i in range(n)}, **extra}


@pytest.mark.parametrize("n,cut", BOUNDARY)
def test_confirm_lists_cut_at_20(n: int, cut: bool) -> None:
    out = schematic_import._confirm_lists(_plan(n), False)
    assert len(out["footprints_added"]) == min(n, 20) and out["footprints_added_count"] == n
    assert out.get("footprints_added_truncated", False) is cut and out.get("nets_truncated", False) is cut
    assert len(out["nets"]) == min(n, 20) and out["net_count"] == n
    assert out["already_present_count"] == n and "already_present" not in out and "detail_note" in out


def test_confirm_detail_and_decision_lists() -> None:
    many = [{"reference": f"U{i}", "reason": "r"} for i in range(30)]
    plan = _plan(30, footprint_mismatches=many, orphans=_refs(30), renetted=many, stale_nets={f"S{i}": 1 for i in range(30)})
    out = schematic_import._confirm_lists(plan, False)
    for key in ("footprint_mismatches", "orphans", "renetted", "stale_nets_removed"):
        assert len(out[key]) == 30 and f"{key}_truncated" not in out
    full = schematic_import._confirm_lists(plan, True)
    assert len(full["footprints_added"]) == 30 and full["nets"] == plan["nets"] and full["already_present"] == plan["already_present"]
    assert "detail_note" not in full


def test_confirm_default_is_much_shorter_for_226() -> None:
    short = len(json.dumps(schematic_import._confirm_lists(_plan(226), False)))
    full = len(json.dumps(schematic_import._confirm_lists(_plan(226), True)))
    assert short < 6000 < full  # measured 4033 against about 36000 characters


def test_confirm_registered_text_names_detail() -> None:
    from boardproof.server import build_server

    tool = next(t for t in build_server()._tool_manager.list_tools() if t.name == "confirm_schematic_import")  # type: ignore[attr-defined]
    assert tool.parameters["properties"]["detail"]["default"] is False
    assert "detail=true" in tool.description and "always full" in tool.description
    assert output_detail.LIMIT == 20


# --- the registered confirm_schematic_import, end to end on the rung-1 project (13 footprints) -------------------------


def _registered_import(tmp_path: Path, **kw) -> dict:
    from boardproof.kicad_cli import find_kicad_cli
    from boardproof.server import build_server
    from tests._fixture_copy import copy_fixture

    try:
        find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    project = tmp_path / "proj"
    copy_fixture(Path(__file__).resolve().parents[1] / "evals" / "rung1" / "project", project)
    pcb = str(project / "rung1_usbc_attiny1616.kicad_pcb")
    tools = {t.name: t.fn for t in build_server()._tool_manager.list_tools()}  # type: ignore[attr-defined]
    proposal = tools["propose_schematic_import"](pcb, output_dir=str(tmp_path / "out"))
    assert proposal["status"] == "proposed", proposal
    return tools["confirm_schematic_import"](pcb, proposal["revision_token"], **kw)


@pytest.mark.parametrize("limit,cut", [(14, False), (13, False), (12, True)])
def test_registered_confirm_cut_boundary(tmp_path, monkeypatch, limit: int, cut: bool) -> None:
    monkeypatch.setattr(output_detail, "LIMIT", limit)
    out = _registered_import(tmp_path)
    assert out["status"] == "imported", out
    assert out["footprints_added_count"] == 13 and len(out["footprints_added"]) == min(13, limit)
    assert out.get("footprints_added_truncated", False) is cut
    assert len(out["nets"]) == min(out["net_count"], limit) and out["net_count"] > 12
    assert "already_present" not in out and "already_present_count" in out


def test_registered_confirm_detail_reaches_the_function(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(output_detail, "LIMIT", 5)
    out = _registered_import(tmp_path, detail=True)
    assert out["status"] == "imported", out
    assert len(out["footprints_added"]) == 13 and len(out["nets"]) == out["net_count"] > 5
    assert "footprints_added_truncated" not in out and "nets_truncated" not in out and "already_present" in out
