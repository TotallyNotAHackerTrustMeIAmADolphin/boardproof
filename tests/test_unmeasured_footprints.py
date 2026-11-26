"""Issue 258: a footprint with neither courtyard nor pad (a logo) must not stop a move of other footprints.

The logo is a copy of R1 on a copy of simple_led with its pads and courtyard graphics removed through the
write layer (tmp_path only). It is listed as not measured and is absent from the box checks. Moving it stays refused
for write_positions, write_lock_state and write_poses; place_footprints (which does not use write_positions) still
moves it.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import group_edit, placement_anchor, raw_geometry, write_layer
from boardproof.toolsets import placement as placement_toolset
from tests.test_board_groups import FIXTURES, _fp_uuids, _group
from tests.test_group_edit import _ats, _fp

PAIR = "dddddddd-0000-4000-8000-000000000001"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    dest = tmp_path / "led"
    dest.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy(FIXTURES / f"simple_led{suffix}", dest / f"simple_led{suffix}")
    pcb = dest / "simple_led.kicad_pcb"
    ids = _fp_uuids(pcb)
    root = write_layer.load(pcb)
    logo = write_layer.parse_string(write_layer.serialise(_fp(root, "R1")))
    logo.children[:] = [
        c for c in logo.children
        if c.is_atom or not (c.name == "pad" or (c.name or "").startswith("fp_") and c.find("layer") is not None
                             and "CrtYd" in str(c.find("layer").values[0]))
    ]
    for c in logo.children:
        if not c.is_atom and c.name == "uuid":
            c.children[0] = write_layer.string(str(uuid.uuid4()))
        if not c.is_atom and c.name in ("property", "fp_text") and str(c.children[0].value).lower() == "reference":
            c.children[1] = write_layer.string("LOGO1")
    root.append(logo)
    root.append(_group("pair", PAIR, [ids["R1"], ids["D1"]]))
    write_layer.save(root, pcb)
    assert not placement_anchor.read_parts(write_layer.load(pcb), {"R1"}) == []
    with pytest.raises(raw_geometry.RawGeometryError):
        placement_anchor.read_parts(write_layer.load(pcb), {"LOGO1"})
    return pcb


def _tools() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_move_group_succeeds_and_names_the_logo(board: Path, tmp_path: Path) -> None:
    before = _ats(board)
    found = group_edit.propose_move_group(board, ["pair"], 1.0, 2.0, tmp_path / "out")
    assert found["status"] == "proposed" and found["not_measured"] == ["LOGO1"], found
    result = group_edit.confirm_move_group(board, found["revision_token"])
    assert result["status"] == "moved", result
    after = _ats(Path(result["output_pcb"]))
    assert after["R1"][:2] == [before["R1"][0] + 1.0, before["R1"][1] + 2.0]
    assert after["LOGO1"] == before["LOGO1"]


def test_a_group_member_without_shape_is_refused_not_moved(board: Path, tmp_path: Path) -> None:
    root = write_layer.load(board)
    root.append(_group("logos", "dddddddd-0000-4000-8000-000000000009", [raw_geometry.direct_uuid(_fp(root, "LOGO1"))]))
    write_layer.save(root, board)
    found = group_edit.propose_move_group(board, ["logos"], 1.0, 0.0, tmp_path / "out")
    assert found["status"] == "refused" and "LOGO1" in found["refused"][0]["reason"], found


def test_write_positions_moves_others_reports_the_logo_and_refuses_to_move_it(board: Path) -> None:
    result = placement_anchor.write_positions(board, {"R1": (50.0, 50.0)})
    assert result == {"changed": 1, "not_measured": ["LOGO1"]}
    assert _ats(board)["R1"][:2] == [50.0, 50.0]
    before = board.read_bytes()
    with pytest.raises(raw_geometry.RawGeometryError, match="LOGO1"):
        placement_anchor.write_positions(board, {"LOGO1": (1.0, 1.0)})
    assert board.read_bytes() == before


def test_place_footprints_moves_a_measured_one(board: Path) -> None:
    out = _tools()["place_footprints"](str(board), [{"reference": "R1", "x": 20.0, "y": 20.0}])
    assert out["status"] == "ok" or out.get("status") != "error", out
    assert placement_anchor.read_parts(write_layer.load(board), {"R1"})


def test_autoplacement_lists_the_logo_and_ignores_it_in_the_collision_check(board: Path) -> None:
    tools = _tools()
    proposal = tools["propose_autoplacement"](str(board), ["R1"])
    assert proposal["status"] == "pending_confirmation" and proposal["not_measured"] == ["LOGO1"], proposal
    done = tools["confirm_autoplacement"](proposal["revision_token"])
    assert done["status"] == "confirmed" and done["not_measured"] == ["LOGO1"], done
    refused = tools["propose_autoplacement"](str(board), ["LOGO1"])
    assert refused["code"] == "footprint_unmeasurable", refused


def test_a_lock_of_the_logo_is_refused_and_writes_nothing(board: Path) -> None:
    before = board.read_bytes()
    with pytest.raises(raw_geometry.RawGeometryError, match="LOGO1"):
        placement_anchor.write_lock_state(board, {"LOGO1": True})
    out = _tools()["lock_footprints"](str(board), ["LOGO1"])
    assert out["status"] == "incomplete" and "LOGO1" in out["reason"], out
    assert board.read_bytes() == before
    assert placement_anchor.write_lock_state(board, {"R1": True}) == {"changed": 1, "not_measured": ["LOGO1"]}


def test_the_edge_tool_lists_the_logo(board: Path) -> None:
    out = _tools()["place_footprints_at_edge"](str(board), [{"reference": "R1", "edge": "left", "offset_mm": 1.0}])
    assert out["status"] == "ok" and out["not_measured"] == ["LOGO1"], out


def test_a_logo_without_reference_is_named_by_its_lib_id_never_by_a_bare_id(board: Path) -> None:
    root = write_layer.load(board)
    for c in _fp(root, "LOGO1").children:
        if not c.is_atom and c.name in ("property", "fp_text") and str(c.children[0].value).lower() == "reference":
            c.children[1] = write_layer.string("")
    write_layer.save(root, board)
    names: list[str] = []
    placement_anchor.read_parts(write_layer.load(board), unmeasured=names)
    assert len(names) == 1 and names[0].startswith("lib_id "), names


def test_the_not_measured_text_is_in_the_tool_descriptions() -> None:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    sentence = "is listed under not_measured and left out of the collision check; that check says nothing about it."
    for name in ("place_footprints_at_edge", "propose_autoplacement", "confirm_autoplacement"):
        text = tools[name].description
        assert text.count(sentence) == 1 and "neither courtyard nor pad" in text, name
    for word in ("routed", "done", "complete", "finished", "clean", "verified", "ready", "connected", "passes"):
        assert word not in sentence.split()
