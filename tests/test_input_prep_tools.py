"""Server-level tests for the input-preparation tools (ticket 11).

Seam: the MCP call path (``build_server`` + ``Client``), as test_gate_field.py.
Only the gate's grading (``verify_board.verify_board`` / ``snapshot_rules``,
the kicad-cli boundary) is replaced; the preparation itself runs for real on a
fixture built through ``kicad_tools`` nodes from the golden board.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from mcp.client.client import Client

from kicad_mcp import write_layer
from kicad_mcp.server import build_server

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"


def _build_board(dest: Path, texts: list[tuple[str, str]]) -> Path:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    shutil.copytree(GOLDEN_DIR, dest)
    pcb = dest / GOLDEN_NAME
    board = PCB.load(pcb)
    for index, (text, layer) in enumerate(texts):
        node = SExp.list("gr_text", text)
        node.append(SExp.list("at", 20.0 + index, 20.0, 0))
        node.append(SExp.list("layer", layer))
        node.append(SExp.list("uuid", f"00000000-0000-4000-8000-0000000000{index:02d}"))
        node.append(SExp.list("effects", SExp.list("font", SExp.list("size", 1, 1), SExp.list("thickness", 0.15))))
        board._sexp.append(node)
    write_layer.save_pcb(board, pcb)
    return pcb


@pytest.fixture
def board(tmp_path: Path) -> Path:
    return _build_board(tmp_path / "proj", [("NOTE-TOP", "F.Cu"), ("NOTE-BOT", "B.Cu")])


@pytest.fixture(autouse=True)
def fake_gate(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """The kicad-cli boundary: a PASS verdict and an empty baseline. Returns the
    list of calls the gate received (path plus keyword arguments)."""
    from kicad_mcp import verify_board as gate

    calls: list[dict] = []

    def fake_verify(pcb_path, **kwargs):
        calls.append({"pcb_path": str(pcb_path), **kwargs})
        return {
            "verdict": "PASS", "unconnected": [], "violations": [], "unstable": False,
            "list_complete": True, "incomplete_reasons": [],
        }

    monkeypatch.setattr(gate, "verify_board", fake_verify)
    monkeypatch.setattr(gate, "snapshot_rules", lambda pcb_path, **_kw: {})
    return calls


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


@pytest.mark.asyncio
async def test_both_tools_are_listed_without_loading_a_toolset() -> None:
    async with Client(build_server(), mode="auto") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    assert {"propose_input_preparation", "confirm_input_preparation"} <= tools.keys()
    propose, confirm = tools["propose_input_preparation"], tools["confirm_input_preparation"]
    assert propose.annotations.read_only_hint is True
    assert propose.annotations.idempotent_hint is True
    assert confirm.annotations.destructive_hint is True
    assert confirm.annotations.read_only_hint is False
    assert set(confirm.input_schema["properties"]) == {
        "pcb_path", "revision_token", "parity", "parity_reason",
    }
    assert "drc" not in propose.input_schema["properties"]


@pytest.mark.asyncio
async def test_descriptions_say_once_before_the_pin_and_point_to_the_gate() -> None:
    async with Client(build_server(), mode="auto") as client:
        tools = {t.name: t.description for t in (await client.list_tools()).tools}

    for name in ("propose_input_preparation", "confirm_input_preparation"):
        text = tools[name]
        assert "once" in text and "before" in text and "pin_rule_baseline" in text
        assert "attempt" in text
        assert "unhandled" in text
        assert "verify_board" in text


@pytest.mark.asyncio
async def test_dry_run_writes_nothing_and_carries_the_gate(board: Path, tmp_path: Path) -> None:
    before = _digest(tmp_path)
    async with Client(build_server(), mode="auto") as client:
        out = await _call(client, "propose_input_preparation", pcb_path=str(board))

    assert out["status"] == "proposed"
    assert [m["text"] for m in out["moves"]] == ["NOTE-TOP", "NOTE-BOT"]
    assert out["gate"]["verdict"] == "NOT_RUN" and out["gate"]["stale"] is True
    assert _digest(tmp_path) == before
    assert not Path(out["output_pcb"]).exists()


@pytest.mark.asyncio
async def test_confirm_builds_the_revision_and_stales_source_and_output(board: Path) -> None:
    async with Client(build_server(), mode="auto") as client:
        graded = await _call(
            client, "verify_board", pcb_path=str(board), parity="required", baseline={}
        )
        assert graded["verdict"] == "PASS"
        proposal = await _call(client, "propose_input_preparation", pcb_path=str(board))
        assert proposal["gate"]["verdict"] == "PASS" and proposal["gate"]["stale"] is False

        out = await _call(
            client, "confirm_input_preparation", pcb_path=str(board),
            revision_token=proposal["revision_token"], parity="required",
        )

    assert out["status"] == "prepared"
    prepared = Path(out["output_pcb"])
    assert prepared.is_file() and prepared != board
    gates = out["gates"]
    assert set(gates) == {str(board), str(prepared)}
    assert gates[str(board)]["stale"] is True  # the mutation mark outlives an unchanged source file
    assert gates[str(prepared)]["stale"] is True
    assert gates[str(prepared)]["verdict"] == "NOT_RUN"  # input_floor is not a recorded verdict


@pytest.mark.asyncio
async def test_a_replayed_token_is_refused(board: Path) -> None:
    async with Client(build_server(), mode="auto") as client:
        proposal = await _call(client, "propose_input_preparation", pcb_path=str(board))
        args = dict(
            pcb_path=str(board), revision_token=proposal["revision_token"], parity="required"
        )
        first = await _call(client, "confirm_input_preparation", **args)
        second = await _call(client, "confirm_input_preparation", **args)

    assert first["status"] == "prepared"
    assert second["status"] == "error"
    assert "revision_token" in second["reason"]


@pytest.mark.asyncio
async def test_unhandled_copper_text_is_listed_and_floor_is_not_clean(tmp_path: Path) -> None:
    pcb = _build_board(tmp_path / "proj", [("NOTE-TOP", "F.Cu"), ("INNER", "In1.Cu")])
    async with Client(build_server(), mode="auto") as client:
        proposal = await _call(client, "propose_input_preparation", pcb_path=str(pcb))
        out = await _call(
            client, "confirm_input_preparation", pcb_path=str(pcb),
            revision_token=proposal["revision_token"], parity="required",
        )

    assert [u.get("text") for u in proposal["unhandled"]] == ["INNER"]
    assert [u.get("text") for u in out["unhandled"]] == ["INNER"]
    assert out["input_floor"]["router_text_check_ok"] is False


@pytest.mark.asyncio
async def test_input_floor_carries_no_verdict_and_says_it_is_not_certified(board: Path) -> None:
    async with Client(build_server(), mode="auto") as client:
        proposal = await _call(client, "propose_input_preparation", pcb_path=str(board))
        out = await _call(
            client, "confirm_input_preparation", pcb_path=str(board),
            revision_token=proposal["revision_token"], parity="required",
        )

    floor = out["input_floor"]
    assert "verdict" not in floor
    assert floor["grading_status"] == "PASS"  # the gate's own answer on the floor, not a verdict
    assert floor["certified"] is False


@pytest.mark.asyncio
async def test_parity_and_reason_reach_the_graded_call(board: Path, fake_gate: list[dict]) -> None:
    async with Client(build_server(), mode="auto") as client:
        proposal = await _call(client, "propose_input_preparation", pcb_path=str(board))
        out = await _call(
            client, "confirm_input_preparation", pcb_path=str(board),
            revision_token=proposal["revision_token"],
            parity="not_applicable", parity_reason="imported board, no schematic",
        )

    assert out["status"] == "prepared"
    assert len(fake_gate) == 1
    assert fake_gate[0]["parity"] == "not_applicable"
    assert fake_gate[0]["parity_reason"] == "imported board, no schematic"
    assert fake_gate[0]["pcb_path"] == out["output_pcb"]


@pytest.mark.asyncio
async def test_explicit_output_dir_is_used_by_propose_and_confirm(board: Path, tmp_path: Path) -> None:
    target = tmp_path / "my_chosen_dir"
    async with Client(build_server(), mode="auto") as client:
        proposal = await _call(
            client, "propose_input_preparation", pcb_path=str(board), output_dir=str(target)
        )
        assert Path(proposal["output_pcb"]) == target / board.name
        assert not target.exists()
        out = await _call(
            client, "confirm_input_preparation", pcb_path=str(board),
            revision_token=proposal["revision_token"], parity="required",
        )

    assert Path(out["output_pcb"]) == target / board.name
    assert (target / board.name).is_file()


@pytest.mark.asyncio
async def test_confirm_names_source_and_prepared_board_and_says_which_is_pcb_path(
    board: Path,
) -> None:
    async with Client(build_server(), mode="auto") as client:
        proposal = await _call(client, "propose_input_preparation", pcb_path=str(board))
        out = await _call(
            client, "confirm_input_preparation", pcb_path=str(board),
            revision_token=proposal["revision_token"], parity="required",
        )
        tools = {t.name: t.description for t in (await client.list_tools()).tools}

    assert out["source_pcb"] == str(board)
    assert out["pcb_path"] == out["output_pcb"] != out["source_pcb"]
    assert "output_pcb" in tools["confirm_input_preparation"]
    assert "pcb_path in the result is the prepared board" in tools["confirm_input_preparation"]
