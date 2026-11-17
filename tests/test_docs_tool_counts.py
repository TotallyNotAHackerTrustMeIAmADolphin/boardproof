"""The tool counts the public docs state equal the live registry (issue 199 review, B1).

README, docs/OVERVIEW.md and docs/ARCHITECTURE.md each state how many tools the server starts with, how many
sit in lazy toolsets, the total and the per-toolset counts. They went stale twice; the numbers are now read from
the docs and compared with a running server (the starter tools, then each toolset loaded alone), so a new tool
fails here until the docs are updated. The test is wording-robust: it reads numbers next to the toolset names
and the phrases "starts with N tools", "N more" or "remaining N", and "N in total".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mcp.client import Client

from boardproof.server import build_server

REPO = Path(__file__).resolve().parents[1]
DOCS = ["README.md", "docs/OVERVIEW.md", "docs/ARCHITECTURE.md"]
NAME = r"(project|verification|sourcing|diagnostics|library|schematic|placement|manufacturing|routing|copilot|simulation)"


async def _live() -> tuple[int, dict[str, int]]:
    async with Client(build_server(), mode="auto") as client:
        start = {t.name for t in (await client.list_tools()).tools}
        listing = (await client.call_tool("list_toolsets", {})).structured_content["result"]
        counts: dict[str, int] = {}
        for entry in listing:
            name = entry["name"]
            before = {t.name for t in (await client.list_tools()).tools}
            await client.call_tool("load_toolset", {"name": name})
            after = {t.name for t in (await client.list_tools()).tools}
            counts[name] = len(after - before)
            await client.call_tool("unload_toolset", {"name": name})
    return len(start), counts


@pytest.mark.asyncio
@pytest.mark.parametrize("doc", DOCS)
async def test_docs_state_the_live_tool_counts(doc):
    start, per = await _live()
    lazy = {k: v for k, v in per.items() if v}
    text = " ".join((REPO / doc).read_text(encoding="utf-8").split())
    assert str(start) in re.findall(r"starts with (\d+) tools", text), f"{doc}: starter count is not {start}"
    more = [int(a or b) for a, b in re.findall(r"(\d+) more(?: tools)?|remaining (\d+)", text)]
    assert more and set(more) == {sum(lazy.values())}, f"{doc}: lazy total is not {sum(lazy.values())}: {more}"
    totals = {int(n) for n in re.findall(r"(\d+) in total", text)}
    assert totals == {start + sum(lazy.values())}, f"{doc}: total is not {start + sum(lazy.values())}: {totals}"
    if doc != "docs/OVERVIEW.md":
        stated: dict[str, set[int]] = {}
        for name, number in re.findall(rf"`{NAME}` \((\d+)(?: tools)?[):]", text):
            stated.setdefault(name, set()).add(int(number))
        assert stated == {k: {v} for k, v in lazy.items()}, f"{doc}: per-toolset counts differ from {lazy}"
    if doc == "docs/ARCHITECTURE.md":
        # the sentence about which toolsets are file-only counts the lazy toolsets other than copilot
        assert f"the other {len(lazy) - 1} lazy toolsets and the always-loaded `project` toolset are file-only" in text
