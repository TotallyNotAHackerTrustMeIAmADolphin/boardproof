"""Issue 246 review, BLOCKS 2: no test seam of dangling_copper (``drc``) is reachable from the MCP surface.

tests/test_layout_tools.py scans server.py only; the pair lives in the routing toolset, so the wrappers in
toolsets/routing.py are scanned here, and the registered input schemas are read from a live server.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from mcp.client import Client

from boardproof.server import build_server

SEAMS = {"drc", "router", "fill", "connectivity_check", "footprint_dirs", "netlist_exporter", "_clock"}
ROUTING = Path(__file__).resolve().parents[1] / "src" / "boardproof" / "toolsets" / "routing.py"
TOOLS = ("propose_dangling_copper_removal", "confirm_dangling_copper_removal")


def _calls_into_dangling_copper(tree: ast.AST) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "_dangling_copper"
    ]


def test_the_wrappers_forward_no_seam_keyword() -> None:
    calls = _calls_into_dangling_copper(ast.parse(ROUTING.read_text(encoding="utf-8")))
    assert len(calls) == 2, "expected exactly the propose and the confirm wrapper to call dangling_copper"
    for call in calls:
        for kw in call.keywords:
            assert kw.arg is not None, "**kwargs forwarded into dangling_copper"
            assert kw.arg not in SEAMS, f"seam {kw.arg} forwarded into dangling_copper"


def test_the_wrappers_take_no_seam_or_kwargs_parameter() -> None:
    tree = ast.parse(ROUTING.read_text(encoding="utf-8"))
    wrappers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in TOOLS]
    assert {w.name for w in wrappers} == set(TOOLS)
    for fn in wrappers:
        assert fn.args.kwarg is None and fn.args.vararg is None, fn.name
        assert not SEAMS & {a.arg for a in fn.args.args + fn.args.kwonlyargs}, fn.name


def test_the_registered_input_schemas_have_no_seam() -> None:
    async def schemas() -> dict[str, dict]:
        async with Client(build_server(), mode="auto") as client:
            await client.call_tool("load_toolset", {"name": "routing"})
            return {t.name: t.input_schema for t in (await client.list_tools()).tools if t.name in TOOLS}

    found = asyncio.run(schemas())
    assert set(found) == set(TOOLS)
    for name, schema in found.items():
        assert not SEAMS & set(schema.get("properties", {})), name
        assert schema.get("additionalProperties") in (None, False), name


def test_the_tool_text_says_what_a_truncated_proposal_means() -> None:
    async def texts() -> dict[str, str]:
        async with Client(build_server(), mode="auto") as client:
            await client.call_tool("load_toolset", {"name": "routing"})
            return {t.name: t.description or "" for t in (await client.list_tools()).tools if t.name in TOOLS}

    found = asyncio.run(texts())
    assert "truncated true and the confirm refuses it" in found["propose_dangling_copper_removal"]
