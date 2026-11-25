"""Issue 250: no test seam of courtyard_rewrite (``_clock``, ``drc``) is reachable from the MCP surface.

tests/test_layout_tools.py scans server.py only; the pair lives in the routing toolset, so the wrappers in
toolsets/library.py are scanned here, and the registered input schemas are read from a live server.
"""

from __future__ import annotations

import ast
import asyncio
from pathlib import Path

from mcp.client import Client

from boardproof.server import build_server

SEAMS = {"drc", "router", "fill", "connectivity_check", "footprint_dirs", "netlist_exporter", "_clock"}
LIBRARY = Path(__file__).resolve().parents[1] / "src" / "boardproof" / "toolsets" / "library.py"
TOOLS = ("propose_courtyard_rewrite", "confirm_courtyard_rewrite")


def _calls_into_courtyard_rewrite(tree: ast.AST) -> list[ast.Call]:
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name) and n.func.value.id == "courtyard_rewrite"
    ]


def test_the_wrappers_forward_no_seam_keyword() -> None:
    calls = _calls_into_courtyard_rewrite(ast.parse(LIBRARY.read_text(encoding="utf-8")))
    assert len(calls) == 2, "expected exactly the propose and the confirm wrapper to call courtyard_rewrite"
    for call in calls:
        for kw in call.keywords:
            assert kw.arg is not None, "**kwargs forwarded into courtyard_rewrite"
            assert kw.arg not in SEAMS, f"seam {kw.arg} forwarded into courtyard_rewrite"


def test_the_wrappers_take_no_seam_or_kwargs_parameter() -> None:
    tree = ast.parse(LIBRARY.read_text(encoding="utf-8"))
    wrappers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name in TOOLS]
    assert {w.name for w in wrappers} == set(TOOLS)
    for fn in wrappers:
        assert fn.args.kwarg is None and fn.args.vararg is None, fn.name
        assert not SEAMS & {a.arg for a in fn.args.args + fn.args.kwonlyargs}, fn.name


def test_the_registered_input_schemas_have_no_seam() -> None:
    async def schemas() -> dict[str, dict]:
        async with Client(build_server(), mode="auto") as client:
            await client.call_tool("load_toolset", {"name": "library"})
            return {t.name: t.input_schema for t in (await client.list_tools()).tools if t.name in TOOLS}

    found = asyncio.run(schemas())
    assert set(found) == set(TOOLS)
    for name, schema in found.items():
        assert not SEAMS & set(schema.get("properties", {})), name
        assert schema.get("additionalProperties") in (None, False), name


def test_the_tool_text_says_what_is_refused_and_what_stays_put() -> None:
    async def texts() -> dict[str, str]:
        async with Client(build_server(), mode="auto") as client:
            await client.call_tool("load_toolset", {"name": "library"})
            return {t.name: t.description or "" for t in (await client.list_tools()).tools if t.name in TOOLS}

    found = asyncio.run(texts())
    for text in found.values():
        assert "without moving" in text and "back side" in text
