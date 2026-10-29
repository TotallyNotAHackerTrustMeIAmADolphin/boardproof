"""Tests for the visual diagnostics toolset (archived backlog Phase 3.5).

Renders the real simple_led fixtures via the real kicad-cli install on this
machine (same pattern as tests/test_kicad_cli.py) -- no mocking, since the
whole point is confirming kicad-cli actually produces usable images.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from kicad_mcp.toolsets import diagnostics as diagnostics_toolset

FIXTURES = Path(__file__).parent / "fixtures"


def _build_and_get_fns(mcp: MCPServer, module) -> dict:
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_build_registers_expected_tools():
    mcp = MCPServer("test")
    names = diagnostics_toolset.build(mcp)
    assert set(names) == {"render_board_diagnostic", "render_schematic_diagnostic"}


def test_render_board_diagnostic_produces_svgs_and_is_non_authoritative(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_board_diagnostic"](str(FIXTURES / "simple_led.kicad_pcb"), str(tmp_path))
    assert result["status"] == "ok"
    assert result["authoritative"] is False
    assert "pcb_run_drc" in result["note"]
    assert result["images"]
    for img in result["images"]:
        assert Path(img).is_file()
        assert Path(img).suffix == ".svg"


def test_render_schematic_diagnostic_produces_svgs_and_is_non_authoritative(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_schematic_diagnostic"](str(FIXTURES / "simple_led.kicad_sch"), str(tmp_path))
    assert result["status"] == "ok"
    assert result["authoritative"] is False
    assert "sch_run_erc" in result["note"]
    assert result["images"]
    for img in result["images"]:
        assert Path(img).is_file()


def test_render_board_diagnostic_missing_file_is_incomplete_not_an_exception(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_board_diagnostic"](str(tmp_path / "does_not_exist.kicad_pcb"), str(tmp_path))
    assert result["status"] == "incomplete"
    assert result["authoritative"] is False


def test_render_schematic_diagnostic_missing_file_is_incomplete_not_an_exception(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_schematic_diagnostic"](str(tmp_path / "does_not_exist.kicad_sch"), str(tmp_path))
    assert result["status"] == "incomplete"
    assert result["authoritative"] is False
