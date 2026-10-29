"""Lazy-loading registry for MCP toolsets.

See docs/ARCHITECTURE.md "Tool design conventions" — toolsets are named groups
of related tools (schematic, pcb, sourcing, verification, ...). Only a
small starter kit is registered with the running MCPServer at startup;
the rest register themselves on demand via `load_toolset(name)`, so the
tool-schema token cost scales with what a session actually uses.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from mcp.server.mcpserver import MCPServer

# A toolset builder registers its tools onto the given server (typically
# via repeated `mcp.add_tool(fn, name=...)` calls) and returns the list of
# tool names it registered, so the registry can remove exactly those on
# unload.
ToolsetBuilder = Callable[[MCPServer], list[str]]


@dataclass
class _Toolset:
    name: str
    description: str
    build: ToolsetBuilder
    loaded: bool = False
    registered_tools: list[str] = field(default_factory=list)


class ToolsetRegistry:
    def __init__(self, mcp: MCPServer) -> None:
        self._mcp = mcp
        self._toolsets: dict[str, _Toolset] = {}

    def register(self, name: str, description: str, build: ToolsetBuilder) -> None:
        if name in self._toolsets:
            raise ValueError(f"toolset '{name}' already registered")
        self._toolsets[name] = _Toolset(name=name, description=description, build=build)

    def list_toolsets(self) -> list[dict]:
        return [
            {"name": ts.name, "description": ts.description, "loaded": ts.loaded}
            for ts in self._toolsets.values()
        ]

    def load_toolset(self, name: str) -> dict:
        ts = self._toolsets.get(name)
        if ts is None:
            return {"error": f"unknown toolset '{name}'", "available": sorted(self._toolsets)}
        if ts.loaded:
            return {"status": "already_loaded", "toolset": name, "tools": ts.registered_tools}
        ts.registered_tools = ts.build(self._mcp)
        ts.loaded = True
        return {"status": "loaded", "toolset": name, "tools": ts.registered_tools}

    def unload_toolset(self, name: str) -> dict:
        ts = self._toolsets.get(name)
        if ts is None:
            return {"error": f"unknown toolset '{name}'", "available": sorted(self._toolsets)}
        if not ts.loaded:
            return {"status": "not_loaded", "toolset": name}
        for tool_name in ts.registered_tools:
            self._mcp.remove_tool(tool_name)
        ts.registered_tools = []
        ts.loaded = False
        return {"status": "unloaded", "toolset": name}
