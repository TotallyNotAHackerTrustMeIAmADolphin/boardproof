"""Server-level tests for the layout checkpoint tools (ticket 21): schematic import,
board rules, board outline and silkscreen minimums.

Seam: the MCP call path (``build_server`` + ``Client``). kicad-cli's DRC (board rules,
board outline) and the gate's grading are replaced ONLY by monkeypatching inside this
module; the tools expose and forward no test seam.
"""

from __future__ import annotations

import ast
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from mcp.client.client import Client

from kicad_mcp import board_outline, board_rules
from kicad_mcp import verify_board as gate
from kicad_mcp.server import build_server
from test_board_rules import _build as build_rules_board
from test_silk_minimums import _build as build_silk_board

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"
RUNG1 = Path(__file__).resolve().parents[1] / "evals" / "rung1"
RUNG1_NAME = "rung1_usbc_attiny1616"

TOOLS = [
    "propose_schematic_import", "confirm_schematic_import",
    "propose_board_rules", "confirm_board_rules",
    "propose_board_outline", "confirm_board_outline",
    "propose_silkscreen_minimums", "confirm_silkscreen_minimums",
]
SEAMS = {"drc", "footprint_dirs", "netlist_exporter", "_clock", "router", "fill"}
_EMPTY = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _kicad_cli_available() -> bool:
    try:
        from kicad_mcp.kicad_cli import find_kicad_cli

        find_kicad_cli()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.fixture(autouse=True)
def fake_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    """Seams added HERE only: the DRC of rules/outline, and the gate's grading."""
    for module, name in ((board_rules, "confirm_board_rules"), (board_outline, "confirm_board_outline")):
        original = getattr(module, name)

        def seamed(pcb_path, token, *args, _original=original, **kwargs):
            return _original(pcb_path, token, *args, **kwargs, drc=lambda p, **_k: _EMPTY)

        monkeypatch.setattr(module, name, seamed)

    def fake_verify(pcb_path, **_kwargs):
        return {
            "verdict": "PASS", "unconnected": [], "violations": [], "unstable": False,
            "list_complete": True, "incomplete_reasons": [],
        }

    monkeypatch.setattr(gate, "verify_board", fake_verify)


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


def _rules_case(tmp: Path):
    board = build_rules_board(tmp / "proj")
    args = dict(
        min_clearance=0.3, min_track_width=0.25,
        netclasses=[{"name": "Default", "via_diameter": 0.8, "via_drill": 0.4, "clearance": 0.3}],
    )
    return board, "propose_board_rules", args, "confirm_board_rules", {"parity": "required"}, "applied"


def _outline_case(tmp: Path):
    shutil.copytree(GOLDEN_DIR, tmp / "proj")
    args = dict(outline={"x": 100.0, "y": 70.0, "width": 100.0, "height": 70.0})
    return (tmp / "proj" / GOLDEN_NAME, "propose_board_outline", args,
            "confirm_board_outline", {}, "built")


def _silk_case(tmp: Path):
    board = build_silk_board(tmp / "proj")
    return board, "propose_silkscreen_minimums", {}, "confirm_silkscreen_minimums", {}, "applied"


def _import_case(tmp: Path):
    if not _kicad_cli_available():
        pytest.skip("kicad-cli not installed")
    shutil.copytree(RUNG1 / "project", tmp / "proj")
    return (tmp / "proj" / f"{RUNG1_NAME}.kicad_pcb", "propose_schematic_import", {},
            "confirm_schematic_import", {}, "imported")


CASES = {
    "rules": _rules_case, "outline": _outline_case, "silk": _silk_case, "import": _import_case,
}


@pytest.mark.asyncio
async def test_all_pairs_are_listed_with_hints_and_no_seam_parameter() -> None:
    async with Client(build_server(), mode="auto") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}

    for name in TOOLS:
        tool = tools[name]
        props = set(tool.input_schema["properties"])
        assert not SEAMS & props, name
        assert not any("kwargs" in p for p in props), name
        if name.startswith("propose_"):
            assert tool.annotations.read_only_hint is True, name
            assert tool.annotations.idempotent_hint is True, name
        else:
            assert tool.annotations.destructive_hint is True, name
            assert tool.annotations.read_only_hint is False, name
    assert set(tools["confirm_board_rules"].input_schema["properties"]) == {
        "pcb_path", "revision_token", "parity", "parity_reason",
    }
    assert {"allow_loosen", "allow_incoherent", "min_clearance", "netclasses", "output_dir"} <= set(
        tools["propose_board_rules"].input_schema["properties"]
    )
    assert {"allow_overlap", "mounting_holes", "origin", "outline"} <= set(
        tools["propose_board_outline"].input_schema["properties"]
    )


@pytest.mark.asyncio
async def test_descriptions_give_order_new_revision_flags_and_the_gate() -> None:
    async with Client(build_server(), mode="auto") as client:
        tools = {t.name: t.description for t in (await client.list_tools()).tools}

    order = "import_from_schematic -> board_outline -> board_rules -> input_preparation"
    for name in TOOLS:
        text = tools[name]
        assert order in text, name
        assert "silkscreen_minimums -> pin_rule_baseline -> pour step -> routing" in text, name
        assert "NEW" in text, name
        assert "verify_board" in text, name
    flag = "set only after a human has read the listed items"
    for flag_name, tool in (
        ("allow_loosen", "propose_board_rules"), ("allow_incoherent", "propose_board_rules"),
        ("allow_overlap", "propose_board_outline"),
    ):
        assert flag_name in tools[tool], flag_name
        assert flag in tools[tool]
        assert "never set it to get past a refusal on your own" in tools[tool]
    assert "baseline" in tools["confirm_board_rules"] and "void" in tools["confirm_board_rules"]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(CASES))
async def test_dry_run_writes_nothing_and_carries_the_gate(case: str, tmp_path: Path) -> None:
    board, propose, args, *_ = CASES[case](tmp_path)
    before = _digest(tmp_path)
    async with Client(build_server(), mode="auto") as client:
        out = await _call(client, propose, pcb_path=str(board), **args)

    assert out["status"] == "proposed", out
    assert out["revision_token"]
    assert out["gate"]["verdict"] == "NOT_RUN" and out["gate"]["stale"] is True
    assert _digest(tmp_path) == before
    assert not Path(out["output_pcb"]).exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", list(CASES))
async def test_confirm_stales_source_and_output_and_refuses_a_replay(
    case: str, tmp_path: Path
) -> None:
    board, propose, args, confirm, extra, done = CASES[case](tmp_path)
    async with Client(build_server(), mode="auto") as client:
        graded = await _call(
            client, "verify_board", pcb_path=str(board), parity="not_applicable",
            parity_reason="test", baseline={},
        )
        assert graded["verdict"] == "PASS"
        proposal = await _call(client, propose, pcb_path=str(board), **args)
        assert proposal["gate"]["verdict"] == "PASS" and proposal["gate"]["stale"] is False
        confirm_args = dict(pcb_path=str(board), revision_token=proposal["revision_token"], **extra)
        out = await _call(client, confirm, **confirm_args)
        replay = await _call(client, confirm, **confirm_args)

    assert out["status"] == done, out
    prepared = Path(out["output_pcb"])
    assert prepared.is_file() and prepared != board
    assert out["pcb_path"] == out["output_pcb"] and out["source_pcb"] == str(board)
    assert set(out["gates"]) == {str(board), str(prepared)}
    # the earlier PASS on the source is void even though the source bytes are unchanged
    assert out["gates"][str(board)]["stale"] is True
    assert out["gates"][str(prepared)]["stale"] is True
    assert out["gates"][str(prepared)]["verdict"] == "NOT_RUN"
    assert replay["status"] == "error"
    assert "revision_token" in replay["reason"]


@pytest.mark.asyncio
async def test_rules_confirm_voids_the_sources_verdict_and_reports_the_new_baseline(
    tmp_path: Path,
) -> None:
    board, propose, args, confirm, extra, _ = _rules_case(tmp_path)
    async with Client(build_server(), mode="auto") as client:
        baseline = await _call(client, "pin_rule_baseline", pcb_path=str(board))
        await _call(
            client, "verify_board", pcb_path=str(board), parity="not_applicable",
            parity_reason="test", baseline=baseline,
        )
        proposal = await _call(client, propose, pcb_path=str(board), **args)
        out = await _call(
            client, confirm, pcb_path=str(board), revision_token=proposal["revision_token"], **extra
        )
        source_now = await _call(client, "propose_board_rules", pcb_path=str(board), **args)

    assert out["baseline_changed"] is True
    assert out["baseline"] != baseline  # a new pin for the revision; the old one is void
    assert out["gates"][str(board)]["verdict"] == "PASS"  # recorded earlier ...
    assert out["gates"][str(board)]["stale"] is True  # ... and void now
    assert source_now["gate"]["stale"] is True


def _calls_in(source: str) -> list[ast.Call]:
    return [n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.Call)]


def test_source_scan_guard_no_registered_tool_forwards_a_seam() -> None:
    server = Path(__file__).parent.parent / "src" / "kicad_mcp" / "server.py"
    callees = set(TOOLS)
    seen: set[str] = set()
    offenders: list[str] = []
    for call in _calls_in(server.read_text(encoding="utf-8")):
        func = call.func
        if not isinstance(func, ast.Attribute) or func.attr not in callees:
            continue  # only calls into the modules (module.fn), not the registered wrappers
        seen.add(func.attr)
        for kw in call.keywords:
            if kw.arg is None or kw.arg in SEAMS:
                offenders.append(f"line {call.lineno} {func.attr} passes {kw.arg or '**kwargs'}")
    assert seen == callees  # the guard really scanned every call
    assert offenders == []


def test_the_seam_guard_would_catch_a_forwarding_call() -> None:
    (call,) = _calls_in("board_rules.confirm_board_rules(p, t, drc=drc)")
    assert any(kw.arg in SEAMS for kw in call.keywords)
