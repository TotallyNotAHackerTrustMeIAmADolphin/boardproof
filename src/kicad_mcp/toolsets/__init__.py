"""Toolset registry: lazy-loading groups of MCP tools.

See registry.ToolsetRegistry for the mechanism, modeled on the pattern
described in docs/ARCHITECTURE.md ("Toolset lazy-loading") — only a small
starter kit of tools is registered with the MCP server at startup; the
rest register themselves on demand via load_toolset, so the tool-schema
token cost scales with what's actually in use, not the full tool count.
"""

from kicad_mcp.toolsets.registry import ToolsetRegistry

__all__ = ["ToolsetRegistry"]
