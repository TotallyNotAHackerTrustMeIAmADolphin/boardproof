"""The routing proposals are read-only: they write nothing beside the user's board.

``kicad_tools`` 0.24.0's ``KiCadToDSNExporter.export(path)`` also writes
``<board>.dsn-revision.json`` beside the board (#6915), so a proposal that exported through it
wrote into the user's project even with no Freerouting jar configured (build ticket 71 review,
finding F1). The board's directory is a copy in ``tmp_path``; its full listing and every file's
bytes must be the same after the call, whatever the tool's status.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp.toolsets import routing as routing_toolset

FIXTURES = Path(__file__).parent / "fixtures"


def _project(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl", ".kicad_sch"):
        shutil.copy(FIXTURES / f"simple_led{suffix}", proj / f"simple_led{suffix}")
    return proj / "simple_led.kicad_pcb"


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


def _tools() -> dict:
    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def no_jar(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("FREEROUTING_JAR_PATH", raising=False)


def test_propose_route_with_freerouting_writes_nothing_beside_the_board(tmp_path: Path, no_jar: None) -> None:
    board = _project(tmp_path)
    before = _snapshot(board.parent)

    result = _tools()["propose_route_with_freerouting"](str(board))

    assert result["status"] != "ok"  # no jar: nothing was routed
    assert _snapshot(board.parent) == before


def test_propose_route_with_retry_writes_nothing_beside_the_board(tmp_path: Path, no_jar: None) -> None:
    board = _project(tmp_path)
    before = _snapshot(board.parent)

    result = _tools()["propose_route_with_retry"](str(board), max_attempts=1)

    assert result["status"] != "ok"  # no jar: nothing was routed
    assert _snapshot(board.parent) == before
