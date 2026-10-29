"""Tests for the copilot toolset (src/kicad_mcp/toolsets/copilot.py).

Runs tools through the real MCPServer.call_tool path (matches
test_verification_toolset.py's convention) so the registration contract
is exercised too, not just the bare functions.

Everything here that doesn't need a live KiCad connection runs always.
The `requires_live_kicad`-gated class at the bottom exercises the real
mutation round-trip (propose -> apply -> verify it landed, plus the
stale-proposal-rejection path) against an actual running KiCad -- it was
genuinely run this way during development (see tests/test_ipc.py's
module docstring and the Phase 6 report) and skips cleanly, with a clear
reason, wherever no live KiCad is reachable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp.ipc import adapter, connection, discovery
from kicad_mcp.toolsets import copilot as copilot_toolset
from tests.test_ipc import requires_live_kicad  # reuse the same live-KiCad gate

FIXTURES = Path(__file__).parent / "fixtures"
PCB_FIXTURE = FIXTURES / "simple_led.kicad_pcb"
SCH_FIXTURE = FIXTURES / "simple_led.kicad_sch"


@pytest.fixture
def mcp() -> MCPServer:
    server = MCPServer("test")
    copilot_toolset.build(server)
    return server


@pytest.fixture(autouse=True)
def _reset_state():
    connection.reset_connection()
    copilot_toolset._PROPOSALS.clear()
    yield
    connection.reset_connection()
    copilot_toolset._PROPOSALS.clear()


async def _call(mcp: MCPServer, name: str, **kwargs) -> dict:
    result = await mcp.call_tool(name, kwargs)
    return json.loads(result.content[0].text)


@pytest.fixture(autouse=True)
def _unreachable_socket(monkeypatch: pytest.MonkeyPatch):
    """Point every non-live test at a socket that can never answer.

    Keeps these tests honest and hermetic: they exercise the fail-closed
    path deterministically instead of depending on whatever KiCad state
    happens to be on the dev/CI machine.
    """
    monkeypatch.setenv(discovery.SOCKET_ENV_VAR, "ipc:///definitely/not/a/real/kicad/socket")


# ---------------------------------------------------------------------------
# Registration contract
# ---------------------------------------------------------------------------


def test_build_registers_expected_tools() -> None:
    mcp = MCPServer("test")
    names = copilot_toolset.build(mcp)
    assert set(names) == {
        "copilot_status",
        "copilot_connect",
        "copilot_disconnect",
        "copilot_get_board_state",
        "copilot_get_schematic_state",
        "copilot_get_selection",
        "copilot_highlight",
        "copilot_propose_moves",
        "copilot_apply_proposal",
    }


@pytest.mark.asyncio
async def test_toolset_survives_server_build() -> None:
    """The literal 'Done means' criterion from the brief, at the toolset level."""
    from kicad_mcp.server import build_server

    server = build_server()
    result = await server.call_tool("load_toolset", {"name": "copilot"})
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "loaded"
    assert "copilot_status" in payload["tools"]


# ---------------------------------------------------------------------------
# Fail-closed behavior -- no live KiCad reachable
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_copilot_status_is_actionable_when_not_connected(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_status")
    assert out["status"] == "not_connected"
    assert out["copilot_available"] is False
    assert out["autopilot_always_available"] is True
    assert "remedy" in out
    assert "Enable KiCad API" in out["remedy"]


@pytest.mark.asyncio
async def test_copilot_connect_never_raises_and_reports_reason(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_connect")
    assert out["status"] == "not_connected"
    assert out["reason_code"] == "kicad_not_running_or_api_disabled"


@pytest.mark.asyncio
async def test_copilot_disconnect_is_idempotent(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_disconnect")
    assert out["status"] == "disconnected"
    out2 = await _call(mcp, "copilot_disconnect")
    assert out2["status"] == "disconnected"
    assert out2["was_connected"] is False


@pytest.mark.asyncio
async def test_copilot_mode_never_falls_back_to_file(mcp: MCPServer) -> None:
    """The single most important negative test in this file.

    Even with a perfectly good pcb_path handed in, explicit copilot mode
    must refuse rather than quietly reading the file -- that silent
    fallback is exactly the data-loss scenario copilot mode exists to
    prevent.
    """
    out = await _call(
        mcp, "copilot_get_board_state", mode="copilot", pcb_path=str(PCB_FIXTURE)
    )
    assert out["status"] == "not_connected"
    assert out.get("source") != "file"
    assert out["fail_closed"] is True


@pytest.mark.asyncio
async def test_copilot_get_selection_requires_live_connection(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_get_selection")
    assert out["status"] == "not_connected"


@pytest.mark.asyncio
async def test_copilot_highlight_requires_live_connection(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_highlight", references=["C1"])
    assert out["status"] == "not_connected"


@pytest.mark.asyncio
async def test_copilot_propose_moves_requires_live_connection(mcp: MCPServer) -> None:
    out = await _call(
        mcp, "copilot_propose_moves", moves=[{"reference": "C1", "x_mm": 1.0, "y_mm": 1.0}]
    )
    assert out["status"] == "not_connected"


@pytest.mark.asyncio
async def test_copilot_apply_proposal_rejects_unknown_token(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_apply_proposal", revision_token="not-a-real-token")
    assert out["status"] == "error"
    assert "unknown or expired" in out["reason"]


# ---------------------------------------------------------------------------
# Mode-agnostic seam behaviour -- exercised via autopilot/auto against the file
# fixture, no live KiCad required. This is the "same tool, both modes" claim.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_board_state_autopilot_mode_reads_file(mcp: MCPServer) -> None:
    out = await _call(
        mcp,
        "copilot_get_board_state",
        mode="autopilot",
        pcb_path=str(PCB_FIXTURE),
        include=["counts", "footprints"],
    )
    assert out["status"] == "ok"
    assert out["mode"] == "autopilot"
    assert out["source"] == "file"
    assert out["counts"]["footprints"] > 0
    assert out["footprints"]["items"][0]["reference"]


@pytest.mark.asyncio
async def test_get_board_state_auto_mode_falls_back_and_says_so(mcp: MCPServer) -> None:
    out = await _call(
        mcp, "copilot_get_board_state", mode="auto", pcb_path=str(PCB_FIXTURE)
    )
    assert out["status"] == "ok"
    assert out["source"] == "file"
    assert out["fell_back_to_file"] is True
    assert out["fell_back_reason"]


@pytest.mark.asyncio
async def test_get_schematic_state_autopilot_mode_reads_file(mcp: MCPServer) -> None:
    out = await _call(
        mcp,
        "copilot_get_schematic_state",
        mode="autopilot",
        sch_path=str(SCH_FIXTURE),
        include=["counts", "symbols"],
    )
    assert out["status"] == "ok"
    assert out["source"] == "file"
    assert out["counts"]["symbols"] > 0


@pytest.mark.asyncio
async def test_get_board_state_autopilot_without_path_is_unsupported(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_get_board_state", mode="autopilot")
    assert out["status"] == "unsupported"
    assert "remedy" in out


# ---------------------------------------------------------------------------
# copilot_propose_moves validation (no live connection needed for these --
# input validation happens before the backend is even resolved)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_propose_moves_rejects_empty_batch(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_propose_moves", moves=[])
    assert out["status"] == "error"
    assert "must not be empty" in out["reason"]


@pytest.mark.asyncio
async def test_propose_moves_rejects_move_with_no_changes(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_propose_moves", moves=[{"reference": "C1"}])
    assert out["status"] == "error"
    assert "changes nothing" in out["reason"]


@pytest.mark.asyncio
async def test_propose_moves_rejects_half_a_position(mcp: MCPServer) -> None:
    out = await _call(
        mcp, "copilot_propose_moves", moves=[{"reference": "C1", "x_mm": 1.0}]
    )
    assert out["status"] == "error"
    assert "only one of x_mm/y_mm" in out["reason"]


@pytest.mark.asyncio
async def test_propose_moves_rejects_missing_reference_key(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_propose_moves", moves=[{"x_mm": 1.0, "y_mm": 1.0}])
    assert out["status"] == "error"
    assert "reference" in out["reason"]


# ---------------------------------------------------------------------------
# copilot_get_selection document validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_selection_rejects_bad_document(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_get_selection", document="layout")
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_highlight_rejects_bad_document(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_highlight", references=["C1"], document="layout")
    assert out["status"] == "error"


@pytest.mark.asyncio
async def test_highlight_rejects_empty_references(mcp: MCPServer) -> None:
    out = await _call(mcp, "copilot_highlight", references=[])
    assert out["status"] == "error"


# ---------------------------------------------------------------------------
# Truncation helper
# ---------------------------------------------------------------------------


def test_truncate_leaves_small_results_untouched() -> None:
    small = {"status": "ok", "footprints": {"items": [1, 2, 3]}}
    assert copilot_toolset._truncate(small) == small


def test_truncate_trims_oversized_list_sections() -> None:
    huge_items = [{"reference": f"C{i}", "note": "x" * 200} for i in range(500)]
    result = {
        "status": "ok",
        "footprints": {"items": huge_items, "count": len(huge_items), "has_more": False},
    }
    trimmed = copilot_toolset._truncate(result)
    assert trimmed["truncated"] is True
    assert len(trimmed["footprints"]["items"]) < len(huge_items)
    assert trimmed["footprints"]["has_more"] is True


# ---------------------------------------------------------------------------
# Real, live KiCad mutation round-trip -- skips cleanly when unavailable
# ---------------------------------------------------------------------------


@requires_live_kicad
class TestLiveCopilotMutation:
    """The real propose/apply/verify/stale-rejection round trip.

    Genuinely run against KiCad 10.0.5 pcbnew.exe during development on
    tests/fixtures/golden_attiny1616_board -- see the Phase 6 report for
    the full transcript. Restores every position it moves so the fixture
    (which is a real design under test elsewhere) is left as found.
    """

    GOLDEN_PCB = FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"

    def _live_mcp(self) -> MCPServer:
        server = MCPServer("test-live")
        copilot_toolset.build(server)
        return server

    @pytest.fixture(autouse=True)
    def _real_socket(self, monkeypatch: pytest.MonkeyPatch):
        # Live tests must NOT inherit the unreachable-socket override from
        # the module-level autouse fixture above.
        monkeypatch.delenv(discovery.SOCKET_ENV_VAR, raising=False)

    def test_propose_apply_verify_and_stale_rejection(self) -> None:
        choice = adapter.resolve_backend(adapter.MODE_COPILOT)
        assert choice.ok, choice.error
        backend = choice.backend

        before = backend.board_state(include=["footprints"], limit=None)
        row = next(r for r in before["footprints"]["items"] if r["reference"] == "C1")
        original = (row["x_mm"], row["y_mm"], row["rotation_degrees"])
        target = (original[0] + 2.0, original[1], original[2])

        proposal = copilot_toolset._copilot_propose_moves(
            moves=[{"reference": "C1", "x_mm": target[0], "y_mm": target[1]}],
            message="test_ipc live round-trip",
        )
        assert proposal["status"] == "proposed"
        token = proposal["revision_token"]

        applied = copilot_toolset._copilot_apply_proposal(token)
        assert applied["status"] == "applied"

        after = backend.board_state(include=["footprints"], limit=None)
        row_after = next(r for r in after["footprints"]["items"] if r["reference"] == "C1")
        assert row_after["x_mm"] == pytest.approx(target[0])

        # A second apply of the same (now-consumed) token must not silently
        # no-op as "applied" -- the token is gone.
        replay = copilot_toolset._copilot_apply_proposal(token)
        assert replay["status"] == "error"

        # Restore, then prove the stale-detection path with a fresh proposal.
        restore = copilot_toolset._copilot_propose_moves(
            moves=[{"reference": "C1", "x_mm": original[0], "y_mm": original[1]}],
            message="test_ipc restore",
        )
        restore_applied = copilot_toolset._copilot_apply_proposal(restore["revision_token"])
        assert restore_applied["status"] == "applied"

        stale_setup = copilot_toolset._copilot_propose_moves(
            moves=[{"reference": "C1", "x_mm": original[0] + 5.0, "y_mm": original[1]}],
            message="test_ipc stale setup",
        )
        # Someone else (the "human") moves it in the meantime.
        direct = copilot_toolset._copilot_propose_moves(
            moves=[{"reference": "C1", "x_mm": original[0] + 1.0, "y_mm": original[1]}],
            message="test_ipc simulated concurrent human edit",
        )
        copilot_toolset._copilot_apply_proposal(direct["revision_token"])

        stale_result = copilot_toolset._copilot_apply_proposal(stale_setup["revision_token"])
        assert stale_result["status"] == "stale"

        # Clean up back to the original position.
        cleanup = copilot_toolset._copilot_propose_moves(
            moves=[{"reference": "C1", "x_mm": original[0], "y_mm": original[1]}],
            message="test_ipc final cleanup",
        )
        copilot_toolset._copilot_apply_proposal(cleanup["revision_token"])
