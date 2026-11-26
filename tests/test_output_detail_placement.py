"""Short default answers of place_footprints, lock_footprints, propose_board_outline and
confirm_board_outline (GitHub issue 230, slice 1): counts and the first 20 items that need
attention by default, every item with ``detail=true``; the safety fields stay in both."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import board_outline, output_detail, write_layer
from boardproof.sourcing.footprint_gen import generate_footprint
from boardproof.toolsets import placement as placement_toolset

N = 150  # footprints on the fixture
DEFAULT_BOUND = 4000  # characters of json for a default answer on N footprints (old answers: > 20000)


def _NO_DRC(_path, **_kw):
    return {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


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


def _size(result: dict) -> int:
    return len(json.dumps(result))


def _specs(dx: float = 1.0) -> list[dict]:
    return [{"reference": f"R{i + 1}", "x": 10.0 + (i % 15) * 15.0 + dx, "y": 10.0 + (i // 15) * 15.0} for i in range(N)]


def _safety_place(result: dict) -> None:
    for key in ("status", "placed_count", "moved_count", "rotation_kept_count", "errors", "errors_count", "frame", "origin_mm"):
        assert key in result, f"safety field {key} missing from {sorted(result)}"


def _safety_outline(result: dict, *, proposal: bool) -> None:
    keys = ["status", "outline", "origin", "replaces" if proposal else "replaced", "holes", "note",
            "outside_outline", "outside_outline_count", "overlaps_count", "zone_overlaps_count"]
    keys += ["revision_token"] if proposal else ["outline_drc", "write_check"]
    for key in keys:
        assert key in result, f"safety field {key} missing from {sorted(result)}"


def test_place_footprints_default_is_short_and_detail_has_the_lists(big_board, tools) -> None:
    result = tools["place_footprints"](big_board, _specs())
    full = tools["place_footprints"](big_board, _specs(2.0), detail=True)
    assert result["status"] == "ok", result
    _safety_place(result)
    assert (result["moved_count"], result["placed_count"], result["errors_count"]) == (N, 0, 0)
    assert "moved" not in result and "rotation_kept" not in result
    assert _size(result) < DEFAULT_BOUND, _size(result)
    # the old fields, whole, under detail=true
    assert full["status"] == "ok" and full["placed"] == [] and full["errors"] == []
    assert full["moved"] == full["rotation_kept"] == [f"R{i + 1}" for i in range(N)]
    assert _size(full) > 3 * _size(result)


def test_place_footprints_partial_lists_which_specs_succeeded_without_detail(big_board, tools) -> None:
    specs = [{"reference": "R1", "x": 5.0, "y": 5.0}, {"reference": "X1", "x": 1.0, "y": 1.0}]  # X1: no library_id
    result = tools["place_footprints"](big_board, specs)
    assert result["status"] == "partial", result
    assert result["moved"] == ["R1"] and result["placed"] == [] and result["rotation_kept"] == ["R1"]
    assert [e["spec"]["reference"] for e in result["errors"]] == ["X1"] and result["errors_count"] == 1
    assert "detail_note" not in result


def test_place_footprints_errors_are_never_cut(big_board, tools) -> None:
    specs = [{"reference": f"X{i}", "x": 1.0, "y": 1.0} for i in range(N)]
    result = tools["place_footprints"](big_board, specs)
    assert result["status"] == "error" and len(result["errors"]) == result["errors_count"] == N


def test_mutant_hiding_the_lists_of_a_partial_answer_is_caught(big_board, tools, monkeypatch) -> None:
    real = output_detail.counted
    monkeypatch.setattr(output_detail, "counted", lambda name, items, detail: real(name, items, False))
    specs = [{"reference": "R1", "x": 5.0, "y": 5.0}, {"reference": "X1", "x": 1.0, "y": 1.0}]
    result = tools["place_footprints"](big_board, specs)
    assert result["status"] == "partial" and "moved" not in result  # what test_..._without_detail would fail on


@pytest.mark.parametrize("n", [0, 20, 21])
@pytest.mark.parametrize("detail", [False, True])
def test_listing_and_counted_at_the_limit(n, detail) -> None:
    items = list(range(n))
    got = output_detail.listing("x", items, detail)
    assert got["x_count"] == n
    assert got["x"] == (items if detail else items[:20])
    assert ("x_truncated" in got) == (n == 21 and not detail)  # exactly 20 is whole, 21 is cut
    counted = output_detail.counted("x", items, detail)
    assert counted["x_count"] == n and (counted.get("x") == items if detail else "x" not in counted)


def test_lock_footprints_default_counts_and_detail_lists(big_board, tools) -> None:
    refs = [f"R{i + 1}" for i in range(N)]
    result = tools["lock_footprints"](big_board, refs)
    assert result["status"] == "ok" and result["locked"] is True
    assert (result["changed_count"], result["unchanged_count"]) == (N, 0)
    assert "changed" not in result and "unchanged" not in result
    assert _size(result) < DEFAULT_BOUND, _size(result)
    full = tools["lock_footprints"](big_board, refs, detail=True)
    assert full["changed"] == [] and full["unchanged"] == refs and full["unchanged_count"] == N
    refused = tools["lock_footprints"](big_board, ["NOPE"])
    assert refused["code"] == "unknown_reference" and "NOPE" in refused["reason"]


def _outline_calls(big_board, tmp_path, detail):
    rect = {"x": 0.0, "y": 0.0, "width": 20.0, "height": 20.0}  # leaves almost every footprint outside
    proposal = board_outline.propose_board_outline(big_board, rect, output_dir=tmp_path / "out", detail=detail)
    built = board_outline.confirm_board_outline(big_board, proposal["revision_token"], drc=_NO_DRC, detail=detail)
    return proposal, built


def test_board_outline_default_is_short_and_detail_has_every_item(big_board, tmp_path) -> None:
    proposal, built = _outline_calls(big_board, tmp_path, False)
    assert proposal["status"] == "proposed" and built["status"] == "built", (proposal, built)
    _safety_outline(proposal, proposal=True)
    _safety_outline(built, proposal=False)
    for result in (proposal, built):
        assert result["outside_outline_count"] > output_detail.LIMIT
        assert len(result["outside_outline"]) == output_detail.LIMIT and result["outside_outline_truncated"] is True
        assert _size(result) < DEFAULT_BOUND, _size(result)
    full_p, full_b = _outline_calls(big_board, tmp_path / "again", True)
    for short, full in ((proposal, full_p), (built, full_b)):
        assert len(full["outside_outline"]) == full["outside_outline_count"] == short["outside_outline_count"]
        assert full["outside_outline"][: output_detail.LIMIT] == short["outside_outline"]
        assert _size(full) > 3 * _size(short)
        assert "outside_outline_truncated" not in full


def test_mutants_dropping_a_safety_field_from_the_default_are_caught(big_board, tools, tmp_path, monkeypatch) -> None:
    placed = tools["place_footprints"](big_board, _specs(3.0))
    proposal, built = _outline_calls(big_board, tmp_path, False)
    for key in ("status", "placed_count", "errors", "errors_count", "frame", "origin_mm"):
        with pytest.raises(AssertionError):
            _safety_place({k: v for k, v in placed.items() if k != key})
    for key in ("status", "revision_token", "outside_outline", "outside_outline_count", "holes", "note"):
        with pytest.raises(AssertionError):
            _safety_outline({k: v for k, v in proposal.items() if k != key}, proposal=True)
    for key in ("status", "outline_drc", "write_check", "outside_outline_count"):
        with pytest.raises(AssertionError):
            _safety_outline({k: v for k, v in built.items() if k != key}, proposal=False)
    # a mutated helper (the count dropped) is caught by the same checks on the real tools
    monkeypatch.setattr(output_detail, "counted", lambda *_a, **_k: {})
    with pytest.raises(AssertionError):
        _safety_place(tools["place_footprints"](big_board, _specs(4.0)))
    monkeypatch.setattr(output_detail, "listing", lambda name, items, detail: {name: []})
    with pytest.raises(AssertionError):
        _safety_place(tools["place_footprints"](big_board, _specs(5.0)))


def test_tool_text_names_detail() -> None:
    import asyncio

    async def texts() -> dict:
        from tests.test_description_gate_wording import _descriptions

        return await _descriptions()

    descriptions = asyncio.run(texts())
    for name in ("place_footprints", "lock_footprints", "propose_board_outline", "confirm_board_outline"):
        assert "detail=true" in descriptions[name] or "detail (default false)" in descriptions[name], name


def test_cutout_refusal_lists_every_item_without_detail(big_board, tools, tmp_path) -> None:
    rect = {"x": 0.0, "y": 0.0, "width": 300.0, "height": 300.0, "corner_radius": 60.0}
    proposal = board_outline.propose_board_outline(big_board, rect, output_dir=tmp_path / "round")
    built = board_outline.confirm_board_outline(big_board, proposal["revision_token"], drc=_NO_DRC)
    assert built["status"] == "built", built
    specs = [{"reference": f"R{i + 1}", "x": 1.0, "y": 1.0} for i in range(N)]  # every part into the rounded corner
    result = tools["place_footprints"](built["output_pcb"], specs)
    assert result["status"] == "error" and result["code"] == "in_cutout", result
    assert result["errors_count"] == len(result["errors"]) > output_detail.LIMIT
    assert "errors_truncated" not in result
