"""Issue 244 review: the tool-level paths of the net readers. An unreadable net form ends a tool call
as ``incomplete`` (placement) or ``KicadSpecctraError`` (routing), never as a raw exception."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp
from mcp.server.mcpserver import MCPServer

from boardproof import raw_geometry, write_layer
from boardproof.toolsets import placement, routing

FIXTURES = Path(__file__).parent / "fixtures"
KICAD10 = FIXTURES / "zone_obstacle" / "zone_obstacle.kicad_pcb"
OLDER = FIXTURES / "graphics_removal_board.kicad_pcb"


@pytest.fixture
def unreadable_net_board(monkeypatch) -> str:
    """The KiCad 10 demo loaded with its zone net replaced by a form no reader knows (in memory: KiCad itself
    would refuse to save such a file)."""
    board = write_layer.load_pcb(KICAD10)
    zone = next(c for c in board._sexp.children if c.name == "zone")
    zone.remove(zone.find("net"))
    zone.insert(0, SExp.list("net", 1, 2, 3))
    monkeypatch.setattr(placement.write_layer, "load_pcb", lambda _p: board)
    return str(KICAD10)


def _propose(path: str, filters: list[dict]) -> dict:
    mcp = MCPServer("test")
    placement.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    return tools["propose_remove_graphic_items"](path, filters)


def test_propose_with_a_net_filter_on_an_unreadable_net_is_incomplete(unreadable_net_board) -> None:
    out = _propose(unreadable_net_board, [{"net_name": "GND"}])
    assert out["status"] == "incomplete" and "unreadable" in out["reason"], out


def test_propose_describing_a_zone_with_an_unreadable_net_is_incomplete(unreadable_net_board) -> None:
    out = _propose(unreadable_net_board, [{"shape": "zone"}])
    assert out["status"] == "incomplete" and "unreadable" in out["reason"], out


def test_graphic_net_zero_is_empty_and_no_net_is_none() -> None:
    board = write_layer.load_pcb(OLDER)
    line = next(n for n in placement._iter_top_level_graphics(board) if n.tag == "gr_line")
    assert placement._graphic_node_net_name(line, board) is None
    line.insert(0, SExp.list("net", 0))
    assert placement._graphic_node_net_name(line, board) == ""


# The next test stubs raw_copper_delta, so it proves the conversion to KicadSpecctraError in isolation.
def test_import_ses_with_an_unreadable_net_is_a_specctra_error(monkeypatch, tmp_path) -> None:
    snap = tmp_path / "snap.kicad_pcb"
    shutil.copy(KICAD10, snap)
    monkeypatch.setattr(routing, "run_kicad_specctra", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(routing, "_board_digest", lambda _p: "x")
    monkeypatch.setattr(routing.krt_backend, "route_delta", lambda *_a: {"trace_specs": [], "via_specs": []})

    def boom(*_a):
        raise raw_geometry.RawGeometryError("net node of an unknown form")

    monkeypatch.setattr(routing, "raw_copper_delta", boom)
    snapshot = {"source_pcb": str(snap), "snapshot_pcb": str(snap), "source_sha256": "x"}
    with pytest.raises(routing.KicadSpecctraError):
        routing.import_ses_with_kicad(snapshot, tmp_path / "a.ses", tmp_path / "work")


def test_apply_specs_with_an_unreadable_net_is_not_verified(monkeypatch, tmp_path) -> None:
    board = tmp_path / "board.kicad_pcb"
    shutil.copy(FIXTURES / "simple_led.kicad_pcb", board)

    def boom(*_a):
        raise raw_geometry.RawGeometryError("net node of an unknown form")

    monkeypatch.setattr(routing, "raw_copper_delta", boom)
    with pytest.raises(write_layer.WriteLayerError) as caught:
        routing._apply_specs(board, [], [], expected_copper=[])
    assert routing.WRITE_NOT_VERIFIED in str(caught.value), caught.value
