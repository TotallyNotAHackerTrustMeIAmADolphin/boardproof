"""The MCP surface of the pair clearance rules (GitHub issue 131): tool text pins, schemas and a call through the server.

The tools expose no test seam (``drc`` is never a parameter; tests/test_layout_tools.py scans server.py for it).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer

from boardproof import board_rules
from boardproof.server import build_server
from boardproof.toolsets import verification
from tests import _pair_fixture as fx


async def _tools() -> dict:
    async with Client(build_server(), mode="auto") as client:
        return {t.name: t for t in (await client.list_tools()).tools}


@pytest.mark.asyncio
async def test_propose_board_rules_text_and_schema_name_the_pair_rules() -> None:
    tools = await _tools()
    propose, confirm = tools["propose_board_rules"], tools["confirm_board_rules"]
    props = propose.input_schema["properties"]
    assert "pair_clearances" in props and "remove_pair_clearances" in props
    assert not {"drc", "_clock", "revision_token"} & set(props)
    text = propose.description
    for phrase in (
        "pair_clearances: [{a, b, clearance}]",
        "managed block at the end of the board's .kicad_dru",
        "hand-written rules are never touched",
        "REPLACES the class clearance and the board minimum",
        "loosening",
        "silently ignores a whole rules file",
        "refuses (the source stays untouched)",
        "report_clearance_coverage",
        "Freerouting does not read the .kicad_dru itself",
        "the plain two-class pair rules of the managed block are passed to Freerouting as class_class rules",
        "applied_pair_rules and pairs_not_applied",
        "unrouted nets may follow",
        "the kicad_routing_tools backend and hand-written rules never reach a router",
        "kicad-cli DRC flags that copper afterwards",
        "never the router's own report",
        "measured on one 5-net board with Freerouting 2.5.0: the copper the router laid then kept the pair clearance, except pad-to-pad at fixed input geometry, at the price of more unrouted nets",
    ):
        assert phrase in text, phrase
    assert ".kicad_dru included" in confirm.description


@pytest.mark.asyncio
async def test_report_clearance_coverage_is_a_read_only_verification_tool() -> None:
    server = MCPServer("t")
    assert "report_clearance_coverage" in verification.build(server)
    tool = next(t for t in await server.list_tools() if t.name == "report_clearance_coverage")
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    assert set(tool.input_schema["properties"]) == {"pcb_path", "check_loading", "offset", "limit"}
    assert tool.input_schema["required"] == ["pcb_path"]
    text = " ".join(tool.description.split())
    for phrase in (
        "which clearance rule holds EVERY pair of nets apart",
        "the LAST matching",
        "REPLACES the net class clearance and the board minimum",
        "drops EVERY rule of a rules file that has one defect",
        "silently",
        "rules_file_loaded_by_kicad",
        "may_be_overridden_by",
        "is not a verdict on the board (verify_board is)",
        "Freerouting does not see the .kicad_dru itself",
        "the plain two-class pair rules of the managed block are passed to Freerouting as class_class rules",
        "the kicad_routing_tools backend and hand-written rules never reach a router",
        "never the router's own report",
        "measured on one 5-net board: the copper the router laid then kept the pair clearance, except pad-to-pad at fixed input geometry, at the price of more unrouted nets",
    ):
        assert phrase in text, phrase


@pytest.mark.asyncio
async def test_a_call_through_the_server_lists_the_pairs(tmp_path: Path) -> None:
    board = fx.build_pair_board(tmp_path)
    async with Client(build_server(), mode="auto") as client:
        await client.call_tool("load_toolset", {"name": "verification"})
        result = await client.call_tool(
            "report_clearance_coverage", {"pcb_path": str(board), "check_loading": False, "limit": 10}
        )
    report = json.loads(result.content[0].text)
    total = len(board_rules._board_net_names(board))
    assert report["net_pairs"]["total"] == total * (total - 1) // 2
    assert report["net_pairs"]["returned"] == 10 and report["net_pairs"]["next_offset"] == 10
    assert report["status"] == "complete" and report["rules_file"] is None
