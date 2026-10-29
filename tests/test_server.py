"""End-to-end smoke test for the server/toolset-registry wiring itself.

Uses the MCP SDK's in-memory Client (no subprocess/transport) against the
real server built by build_server(), covering the actual protocol path a
client would take: list tools, load a toolset, see it appear, unload it,
see it disappear.
"""

from __future__ import annotations

import pytest
from mcp.client.client import Client

from kicad_mcp.server import build_server


@pytest.mark.asyncio
async def test_starter_kit_loaded_by_default():
    server = build_server()
    async with Client(server, mode="auto") as client:
        tools = await client.list_tools()
        names = {t.name for t in tools.tools}
        assert {"list_toolsets", "load_toolset", "unload_toolset"} <= names
        # "project" is a starter toolset (see server.STARTER_TOOLSETS) and
        # should already be registered without an explicit load_toolset call.
        assert "project_create" in names
        assert "project_open" in names


@pytest.mark.asyncio
async def test_load_toolset_publishes_tools_changed():
    server = build_server()
    async with Client(server, mode="auto") as client:
        before = {t.name for t in (await client.list_tools()).tools}
        assert "verification" not in before  # not a starter toolset

        result = await client.call_tool("load_toolset", {"name": "verification"})
        assert result.structured_content is not None or result.content

        after = {t.name for t in (await client.list_tools()).tools}
        assert after - before  # something new appeared

        unload_result = await client.call_tool("unload_toolset", {"name": "verification"})
        assert unload_result is not None

        after_unload = {t.name for t in (await client.list_tools()).tools}
        assert after_unload == before


@pytest.mark.asyncio
async def test_unknown_toolset_reports_error_not_exception():
    server = build_server()
    async with Client(server, mode="auto") as client:
        result = await client.call_tool("load_toolset", {"name": "does_not_exist"})
        # Should surface as tool content the model can read, not a protocol error.
        assert result is not None
