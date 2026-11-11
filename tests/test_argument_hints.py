"""A tool call with a missing or wrong argument gets a short hint with an example call (issue 128).

The MCP layer validates the arguments before the tool body runs and answers with pydantic's raw
text. One wrapper in ``build_server`` (``argument_hints.install``) rewrites that answer; what is
accepted does not change. The example is built from each tool's own input schema, so no list can go
stale; the registry-wide test builds the example's arguments and runs them through the tool's own
validation.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path

from pydantic import Field
from typing import Annotated
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer

from boardproof import argument_hints
from boardproof.server import build_server

SRC = Path(argument_hints.__file__)


def _call(server: MCPServer, tool: str, args: dict, load: str | None = None):
    async def go():
        async with Client(server, mode="auto") as client:
            if load:
                await client.call_tool("load_toolset", {"name": load})
            return await client.call_tool(tool, args)

    return asyncio.run(go())


def _text(result) -> str:
    return "\n".join(part.text for part in result.content)


def _toy() -> MCPServer:
    server = MCPServer("toy")
    argument_hints.install(server)

    @server.tool()
    def toy(sch_path: str, count: int, ratio: Annotated[float, Field(description="Ratio, line one.\nline two secret.")], flag: bool = False, mode: str = "a") -> dict:
        """Toy tool.

        Args:
            sch_path: Path to the .kicad_sch file to read.
                Second line that must not appear.
            count: How many.
        """
        return {"ok": True, "sch_path": sch_path, "count": count}

    return server


def test_a_missing_field_names_the_tool_the_field_and_shows_an_example():
    result = _call(_toy(), "toy", {"count": 1, "ratio": 1.0})
    text = _text(result)
    assert result.is_error
    assert "toy" in text and "sch_path" in text
    assert "Path to the .kicad_sch file to read" in text
    assert "Second line" not in text
    assert 'toy(sch_path="<' in text and "count=" in text
    assert "validation error" not in text and "pydantic" not in text


def test_every_missing_field_is_named_once_and_the_example_holds_all_required_fields():
    text = _text(_call(_toy(), "toy", {}))
    assert "sch_path" in text and "count" in text
    assert text.count("Example call") == 1
    assert "flag" not in text and "mode" not in text  # optional fields stay out of the example


def test_a_field_without_a_docstring_line_says_its_type():
    text = _text(_call(_toy(), "toy", {"sch_path": "x", "count": 1}))
    assert "ratio" in text and "Ratio, line one" in text and "secret" not in text


def test_a_field_with_only_a_type_says_its_type():
    server = MCPServer("toy3")
    argument_hints.install(server)

    @server.tool()
    def bare(width: float) -> dict:
        """Bare."""
        return {}

    text = _text(_call(server, "bare", {}))
    assert "`width` (number)" in text


def test_a_tool_error_raised_inside_keeps_its_text():
    from mcp.server.mcpserver.exceptions import ToolError

    server = MCPServer("toy4")
    argument_hints.install(server)

    @server.tool()
    def own(x: str) -> dict:
        """Own."""
        raise ToolError("own words")

    text = _text(_call(server, "own", {"x": "1"}))
    assert "own words" in text and "Example call" not in text


def test_a_wrong_type_keeps_the_raw_message_and_gains_the_example():
    text = _text(_call(_toy(), "toy", {"sch_path": 5, "count": 1, "ratio": 1.0}))
    assert "Input should be a valid string" in text
    assert "Example call" in text and "toy(" in text


def test_accepted_inputs_are_unchanged():
    server = _toy()
    ok = _call(server, "toy", {"sch_path": "a", "count": 2, "ratio": 1})
    assert not ok.is_error and json.loads(_text(ok))["count"] == 2
    extra = _call(server, "toy", {"sch_path": "a", "count": 2, "ratio": 1, "bogus": 1})
    assert not extra.is_error  # an unknown argument was ignored before and still is
    assert json.loads(_text(_call(server, "toy", {"sch_path": "a", "count": "3", "ratio": 1})))["count"] == 3


def test_a_tool_that_fails_inside_keeps_its_own_error():
    server = MCPServer("toy2")
    argument_hints.install(server)

    @server.tool()
    def boom(x: str) -> dict:
        """Boom."""
        raise ValueError("inner failure")

    result = _call(server, "boom", {"x": "1"})
    assert result.is_error
    assert "Example call" not in _text(result)


def test_an_unknown_tool_keeps_its_message():
    result = _call(_toy(), "no_such_tool", {})
    assert result.is_error and "Example call" not in _text(result)


def test_check_connectivity_through_the_real_server_gets_the_documented_example():
    result = _call(build_server(), "check_connectivity", {}, load="schematic")
    text = _text(result)
    assert result.is_error
    assert 'check_connectivity(sch_path="<' in text
    assert "validation error" not in text


def test_every_registered_tool_gets_an_example_that_passes_its_own_validation():
    server = build_server()

    async def load_all():
        async with Client(server, mode="auto") as client:
            for ts in (await client.call_tool("list_toolsets", {})).structured_content["result"]:
                await client.call_tool("load_toolset", {"name": ts["name"]})

    asyncio.run(load_all())
    tools = server._tool_manager.list_tools()
    assert len(tools) > 50
    bad = []
    for tool in tools:
        args = argument_hints.example_arguments(tool.parameters)
        try:
            tool.fn_metadata.validate_arguments(args)
        except Exception as exc:  # noqa: BLE001 - collect every failing tool
            bad.append((tool.name, args, str(exc)[:200]))
        text = argument_hints.example_call(tool.name, tool.parameters)
        assert text.startswith(tool.name + "(") and text.endswith(")")
        for name in tool.parameters.get("required", []):
            assert f"{name}=" in text
    assert not bad, bad


def test_a_missing_field_of_every_tool_is_answered_with_a_hint():
    server = build_server()

    async def run():
        async with Client(server, mode="auto") as client:
            for ts in (await client.call_tool("list_toolsets", {})).structured_content["result"]:
                await client.call_tool("load_toolset", {"name": ts["name"]})
            for tool in server._tool_manager.list_tools():
                required = tool.parameters.get("required", [])
                if not required:
                    continue
                res = await client.call_tool(tool.name, {})
                text = "\n".join(p.text for p in res.content)
                assert res.is_error, tool.name
                assert "Example call" in text and tool.name + "(" in text, tool.name
                assert all(name in text for name in required), tool.name

    asyncio.run(run())


def test_the_example_builder_handles_schema_shapes():
    schema = {
        "type": "object",
        "$defs": {"Pt": {"type": "object", "properties": {"x": {"type": "number"}}, "required": ["x"]}},
        "properties": {
            "a": {"anyOf": [{"type": "null"}, {"type": "integer", "minimum": 3}]},
            "b": {"enum": ["r", "s"]},
            "c": {"type": "array", "items": {"$ref": "#/$defs/Pt"}, "minItems": 1},
            "d": {"type": "string", "minLength": 4},
            "e": {"type": "boolean"},
            "f": {"type": "number", "exclusiveMinimum": 0},
            "g": {"type": "object"},
        },
        "required": ["a", "b", "c", "d", "e", "f", "g"],
    }
    args = argument_hints.example_arguments(schema)
    assert args["a"] == 3 and args["b"] == "r" and args["c"] == [{"x": 1}]
    assert len(args["d"]) >= 4 and args["e"] is False and args["f"] > 0 and args["g"] == {}
    text = argument_hints.example_call("t", schema)
    assert text.startswith("t(a=3, b=") and 'b="r"' in text and "e=False" in text


def test_the_formatter_never_raises_on_a_hostile_schema():
    for schema in ({}, {"required": ["x"]}, {"required": ["x"], "properties": {"x": {"$ref": "#/nope"}}},
                   {"required": ["x"], "properties": {"x": {"$ref": "#/$defs/A"}}, "$defs": {"A": {"$ref": "#/$defs/A"}}}):
        assert isinstance(argument_hints.example_call("t", schema), str)


def test_the_wrapper_adds_no_force_or_test_seam_and_does_not_touch_arguments():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    names = {n.arg for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) for n in node.args.args}
    assert not names & {"force", "_test", "seam"}
    src = SRC.read_text(encoding="utf-8")
    assert "validate_arguments" not in src  # the wrapper rewrites a message, it never validates itself
    assert "arguments[" not in src and "arguments.pop" not in src
    assert inspect.signature(argument_hints.install).parameters.keys() == {"mcp"}


def test_a_validation_error_raised_inside_a_tool_body_is_not_rewritten_as_a_hint():
    from pydantic import BaseModel

    class M(BaseModel):
        a: int

    server = MCPServer("toy5")
    argument_hints.install(server)

    @server.tool()
    def inner(x: str) -> dict:
        """Inner."""
        M.model_validate({})
        return {}

    text = _text(_call(server, "inner", {"x": "1"}))
    assert "Error executing tool inner" in text
    assert "Example call" not in text and "missing required" not in text


def test_a_failing_hint_builder_lets_the_original_error_through(monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("builder broke")

    monkeypatch.setattr(argument_hints, "example_call", broken)
    result = _call(_toy(), "toy", {})
    assert result.is_error
    assert "Example call" not in _text(result) and "Field required" in _text(result)


def test_a_missing_field_together_with_a_wrong_type_keeps_the_type_error():
    text = _text(_call(_toy(), "toy", {"sch_path": 5, "ratio": 1.0}))
    assert "Input should be a valid string" in text
    assert "count" in text and "Example call" in text


def test_a_description_is_cut_at_a_sentence_and_never_ends_mid_expression():
    short = argument_hints._short
    assert short('List of ``{"reference": "C1", "x_mm": 12.5, "y_mm": 30.0,') == ""
    assert short("Path to the file. More words follow here.") == "Path to the file. More words follow here"
    long = "First sentence here. " + "word " * 40
    assert short(long) == "First sentence here"
    assert short("x" * 150) == ""
    assert short("List of (a, b)") == "List of (a, b)"
    assert short("Unbalanced (paren") == ""


def test_a_docstring_entry_that_ends_mid_expression_falls_back_to_the_type():
    server = MCPServer("toy6")
    argument_hints.install(server)

    @server.tool()
    def moves_tool(moves: list[dict]) -> dict:
        """Moves.

        Args:
            moves: List of ``{"reference": "C1", "x_mm": 12.5,
                "y_mm": 30.0}`` items.
        """
        return {}

    text = _text(_call(server, "moves_tool", {}))
    assert "`moves` (array)" in text
