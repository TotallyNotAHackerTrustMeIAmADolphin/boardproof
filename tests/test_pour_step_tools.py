"""Server-level tests for the pour-step tools (ticket 15).

Seam: the MCP call path (``build_server`` + ``Client``). The router, the
kicad-cli grading, the refill and the connectivity runs are replaced ONLY by
monkeypatching ``pour_step.confirm_pour_step`` inside this module with a wrapper
that adds the test seams: the tools themselves expose and forward none.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from mcp.client.client import Client

from kicad_mcp import pour_step
from kicad_mcp import verify_board as gate
from kicad_mcp.server import build_server
from test_pour_step import FULL, _seam_offenders, build_board, fake_drc, fake_fill, make_fake_router

SEAMS = {"drc", "router", "fill", "connectivity_check"}


@pytest.fixture
def board(tmp_path: Path) -> Path:
    return build_board(tmp_path / "proj")


@pytest.fixture
def forwarded(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[dict]:
    """Wrap the step so the test seams are added HERE; returns the kwargs the server passed."""
    seen: list[dict] = []
    original = pour_step.confirm_pour_step

    def seamed(pcb_path, revision_token, **kwargs):
        seen.append(dict(kwargs))
        return original(
            pcb_path, revision_token, **kwargs,
            router=make_fake_router(FULL), drc=fake_drc(),
            fill=fake_fill(tmp_path), connectivity_check=False,
        )

    monkeypatch.setattr(pour_step, "confirm_pour_step", seamed)
    return seen


@pytest.fixture
def baseline(board: Path) -> dict:
    return gate.snapshot_rules(board, drc=fake_drc())


async def _call(client: Client, tool: str, **kwargs) -> dict:
    result = await client.call_tool(tool, kwargs)
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def _digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*")) if p.is_file()
    }


CONFIRM = dict(parity="not_applicable", parity_reason="fixture board has no schematic")


@pytest.mark.asyncio
async def test_both_tools_are_listed_with_no_seam_parameter() -> None:
    async with Client(build_server(), mode="auto") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    propose, confirm = tools["propose_pour_step"], tools["confirm_pour_step"]
    assert propose.annotations.read_only_hint is True
    assert propose.annotations.idempotent_hint is True
    assert confirm.annotations.destructive_hint is True
    assert confirm.annotations.read_only_hint is False
    assert set(confirm.input_schema["properties"]) == {
        "pcb_path", "revision_token", "parity", "parity_reason", "baseline",
    }
    assert "baseline" in confirm.input_schema["required"]
    for tool in (propose, confirm):
        assert not SEAMS & set(tool.input_schema["properties"])
        assert "kwargs" not in " ".join(tool.input_schema["properties"])


@pytest.mark.asyncio
async def test_descriptions_state_what_poured_proves_and_the_cost() -> None:
    async with Client(build_server(), mode="auto") as client:
        tools = {t.name: t.description for t in (await client.list_tools()).tools}

    text = tools["confirm_pour_step"].replace("NOT", "not")
    for needle in (
        "poured", "does not", "connectivity", "proven", "unproven", "verify_board",
        "pin_rule_baseline", "propose_input_preparation", "40", "12",
    ):
        assert needle in text, needle
    assert "verify_board" in tools["propose_pour_step"]
    assert "propose_input_preparation" in tools["propose_pour_step"]


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_carries_the_gate(board: Path, tmp_path: Path) -> None:
    before = _digest(tmp_path)
    async with Client(build_server(), mode="auto") as client:
        out = await _call(
            client, "propose_pour_step", pcb_path=str(board), pours={"GND": "B.Cu"}
        )

    assert out["status"] == "proposed" and out["revision_token"]
    assert out["gate"]["verdict"] == "NOT_RUN" and out["gate"]["stale"] is True
    assert _digest(tmp_path) == before
    assert not Path(out["output_pcb"]).exists()


@pytest.mark.asyncio
async def test_confirm_pours_and_stales_source_and_output_and_refuses_a_replay(
    board: Path, baseline: dict, forwarded: list[dict]
) -> None:
    async with Client(build_server(), mode="auto") as client:
        proposal = await _call(
            client, "propose_pour_step", pcb_path=str(board), pours={"GND": "B.Cu"}
        )
        args = dict(
            pcb_path=str(board), revision_token=proposal["revision_token"],
            baseline=baseline, **CONFIRM,
        )
        out = await _call(client, "confirm_pour_step", **args)
        replay = await _call(client, "confirm_pour_step", **args)

    assert out["status"] == "poured", out
    prepared = Path(out["output_pcb"])
    assert prepared.is_file()
    assert out["pcb_path"] == out["output_pcb"] and out["source_pcb"] == str(board)
    assert set(out["gates"]) == {str(board), str(prepared)}
    assert all(g["stale"] is True for g in out["gates"].values())
    assert out["gates"][str(prepared)]["verdict"] == "NOT_RUN"
    assert replay["status"] == "error"
    assert "revision_token" in replay["reason"]


@pytest.mark.asyncio
async def test_confirm_without_a_pinned_baseline_is_refused_and_writes_nothing(
    board: Path, forwarded: list[dict], tmp_path: Path
) -> None:
    async with Client(build_server(), mode="auto") as client:
        proposal = await _call(
            client, "propose_pour_step", pcb_path=str(board), pours={"GND": "B.Cu"}
        )
        before = _digest(tmp_path)
        out = await _call(
            client, "confirm_pour_step", pcb_path=str(board),
            revision_token=proposal["revision_token"], baseline={}, **CONFIRM,
        )

    assert out["status"] == "incomplete"
    assert "pinned" in out["reason"]
    assert _digest(tmp_path) == before


@pytest.mark.asyncio
async def test_the_server_forwards_no_seam_even_when_a_caller_supplies_one(
    board: Path, baseline: dict, forwarded: list[dict]
) -> None:
    async with Client(build_server(), mode="auto") as client:
        proposal = await _call(
            client, "propose_pour_step", pcb_path=str(board), pours={"GND": "B.Cu"}
        )
        try:
            await _call(
                client, "confirm_pour_step", pcb_path=str(board),
                revision_token=proposal["revision_token"], baseline=baseline,
                drc="evil", router="evil", fill="evil", connectivity_check=False, **CONFIRM,
            )
        except Exception:  # noqa: BLE001  (a schema error is also a refusal)
            pass

    for kwargs in forwarded:
        assert set(kwargs) <= {"parity", "parity_reason", "baseline"}


def test_the_source_scan_guard_has_a_real_call_to_scan_and_passes() -> None:
    server = Path(__file__).parent.parent / "src" / "kicad_mcp" / "server.py"
    source = server.read_text(encoding="utf-8")
    assert "confirm_pour_step(" in source and "propose_pour_step(" in source
    assert _seam_offenders(source, "server.py") == []
