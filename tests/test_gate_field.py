"""Tests for the ``gate`` field every board-state tool output carries.

Seam S2 (the pivot-ladder spec): everything goes through the real
server's call path (``build_server`` + MCP ``Client``). The gate itself runs
real ``kicad-cli`` on a copy of a fixture board; expected values come from
the ticket and docs/TESTING.md, not from re-deriving them with the code under test.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import pytest
from mcp.client.client import Client

from kicad_mcp.server import build_server
from tests import _kicad_faults as faults

FIXTURES = Path(__file__).parent / "fixtures"
GATE_KEYS = {"verdict", "board_sha", "age_s", "stale"}


@pytest.fixture
def board(tmp_path: Path) -> Path:
    """A private copy of the simple_led project, so mutations never touch fixtures."""
    for src in FIXTURES.glob("simple_led.*"):
        shutil.copy(src, tmp_path / src.name)
    return tmp_path / "simple_led.kicad_pcb"


async def _call(client: Client, tool: str, **kwargs) -> dict:
    result = await client.call_tool(tool, kwargs)
    assert result.structured_content is not None or result.content
    if result.structured_content is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_board_state_output_without_a_verdict_says_so(board: Path) -> None:
    async with Client(build_server(), mode="auto") as client:
        await _call(client, "load_toolset", name="verification")
        out = await _call(client, "pcb_run_drc", pcb_path=str(board))

    assert out["gate"].keys() >= GATE_KEYS
    assert out["gate"]["verdict"] == "NOT_RUN"
    assert out["gate"]["stale"] is True
    assert out["gate"]["age_s"] is None


async def _grade(client: Client, board: Path) -> dict:
    baseline = await _call(client, "pin_rule_baseline", pcb_path=str(board))
    return await _call(
        client, "verify_board", pcb_path=str(board), parity="required", baseline=baseline
    )


async def _move_r1(client: Client, board: Path) -> dict:
    await _call(client, "load_toolset", name="placement")
    return await _call(
        client,
        "place_footprints",
        pcb_path=str(board),
        specs=[{"reference": "R1", "x": 31.0, "y": 22.0}],
    )


@pytest.mark.asyncio
async def test_recorded_verdict_is_shown_for_the_same_unchanged_file(board: Path) -> None:
    async with Client(build_server(), mode="auto") as client:
        await _call(client, "load_toolset", name="verification")
        graded = await _grade(client, board)
        out = await _call(client, "pcb_run_drc", pcb_path=str(board))

    # simple_led is unrouted: the gate's own verdict is FAIL, and that is what is echoed.
    assert graded["verdict"] == "FAIL"
    gate = out["gate"]
    assert gate["verdict"] == "FAIL"
    assert gate["stale"] is False
    assert gate["board_sha"] == graded["board_sha"] == gate["verdict_board_sha"]
    assert isinstance(gate["age_s"], float) and gate["age_s"] >= 0


@pytest.mark.asyncio
async def test_verdict_for_one_file_is_never_shown_for_another(board: Path, tmp_path: Path) -> None:
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    for src in FIXTURES.glob("simple_led.*"):
        shutil.copy(src, other_dir / src.name)
    other = other_dir / "simple_led.kicad_pcb"

    async with Client(build_server(), mode="auto") as client:
        await _call(client, "load_toolset", name="verification")
        await _grade(client, board)
        out = await _call(client, "pcb_run_drc", pcb_path=str(other))

    assert out["gate"]["verdict"] == "NOT_RUN"
    assert out["gate"]["stale"] is True


@pytest.mark.asyncio
async def test_mutation_through_any_tool_makes_the_next_output_stale(board: Path) -> None:
    async with Client(build_server(), mode="auto") as client:
        await _call(client, "load_toolset", name="verification")
        graded = await _grade(client, board)
        moved = await _move_r1(client, board)
        out = await _call(client, "pcb_run_drc", pcb_path=str(board))

    # The mutating tool's own output is already stale, and so is the next read.
    assert moved["gate"]["stale"] is True
    assert out["gate"]["verdict"] == "FAIL"  # the old verdict, honestly labelled
    assert out["gate"]["stale"] is True
    assert out["gate"]["board_sha"] != graded["board_sha"]
    assert out["gate"]["verdict_board_sha"] == graded["board_sha"]


@pytest.mark.asyncio
async def test_project_open_carries_the_gate_field(board: Path) -> None:
    async with Client(build_server(), mode="auto") as client:
        out = await _call(client, "project_open", path=str(board.parent))

    assert out["pcb_path"] == str(board)
    assert out["gate"]["verdict"] == "NOT_RUN"
    assert out["gate"]["board_sha"]


@pytest.mark.asyncio
async def test_confirm_tool_that_takes_only_a_token_still_carries_the_gate(board: Path) -> None:
    async with Client(build_server(), mode="auto") as client:
        await _call(client, "load_toolset", name="placement")
        proposal = await _call(client, "propose_autoplacement", pcb_path=str(board))
        assert proposal["gate"]["stale"] is True
        confirmed = await _call(
            client, "confirm_autoplacement", revision_token=proposal["revision_token"]
        )

    assert "gate" in confirmed
    assert confirmed["gate"]["stale"] is True


@pytest.mark.asyncio
async def test_drc_tool_returns_unconnected_items_and_says_it_is_not_the_gate(board: Path) -> None:
    async with Client(build_server(), mode="auto") as client:
        await _call(client, "load_toolset", name="verification")
        out = await _call(client, "pcb_run_drc", pcb_path=str(board))
        listing = await client.list_tools()

    assert out["unconnected_items"], "unrouted fixture must list its unconnected items"
    assert out["unconnected_count"] == len(out["unconnected_items"])
    assert "not the acceptance gate" in out["not_the_gate"]
    assert "verify_board" in out["not_the_gate"]
    description = next(t.description for t in listing.tools if t.name == "pcb_run_drc")
    assert "NOT the acceptance gate" in description
    assert "AUTHORITATIVE" not in description


@pytest.mark.asyncio
async def test_a_schematic_edit_makes_the_verdict_stale_even_if_board_bytes_match(
    board: Path,
) -> None:
    async with Client(build_server(), mode="auto") as client:
        await _call(client, "load_toolset", name="verification")
        await _call(client, "load_toolset", name="schematic")
        graded = await _grade(client, board)
        await _call(client, "fix_annotation", sch_path=str(board.with_suffix(".kicad_sch")))
        out = await _call(client, "pcb_run_drc", pcb_path=str(board))

    assert out["gate"]["board_sha"] == graded["board_sha"]
    assert out["gate"]["stale"] is True


# Tools not called by the sweep below, each with its reason. Everything else is
# called, with realistic arguments where the generic ones would stop at validation.
_SWEEP_SKIP = {
    "verify_board": "the gate itself: the only tool allowed to say it",
    "load_toolset": "meta tool, called by the test to load every toolset",
    "unload_toolset": "meta tool, no board state",
    "search_parts": "needs the network (LCSC bot wall)",
    "fetch_datasheet": "needs the network",
    "fetch_part_assets": "needs the network",
    "resolve_jlcpcb_assets": "needs the network",
    "lookup_jlcpcb_part_tiers": "needs the network",
    "jlcpcb_library_status": "reads/downloads the JLCPCB parts library cache",
}
# Realistic arguments for the tools most likely to emit a status. Board-path
# values are filled in by _sweep_arguments. No router jar is configured here, so
# the routing proposals take their honest "incomplete" path; that is still output.
_SWEEP_ARGUMENTS = {
    "propose_route_with_retry": {"max_attempts": 1, "timeout_seconds": 60},
    "propose_route_with_freerouting": {"max_passes": 1, "timeout_seconds": 60},
    "propose_placement_optimization": {"rounds": 1, "timeout_seconds": 60},
    "copilot_propose_moves": {"moves": [{"reference": "R1", "x_mm": 1.0, "y_mm": 1.0}]},
    "copilot_highlight": {"references": ["R1"]},
    "copilot_get_board_state": {"mode": "autopilot"},
    "copilot_get_selection": {"document": "pcb"},
    "copilot_connect": {"timeout_ms": 500},
    # Outputs go beside the board in a scratch dir; missing KiKit/ngspice just
    # yields the tool's honest "incomplete", which is still output to scan.
    "export_manufacturing_package": lambda board: {"output_dir": str(board.parent / "fab")},
    "panelize_board": lambda board: {
        "output_path": str(board.parent / "panel.kicad_pcb"), "rows": 1, "cols": 2},
    "export_spice_netlist": lambda board: {"output_path": str(board.parent / "out.cir")},
    "run_spice_simulation": lambda board: {
        "netlist_path": _write_netlist(board.parent / "rc.cir"),
        "analysis": {"type": "op", "probe": ["out"]},
    },
}


def _write_netlist(path: Path) -> str:
    path.write_text(
        "* divider\nV1 in 0 DC 1\nR1 in out 1k\nR2 out 0 1k\n.end\n", encoding="utf-8"
    )
    return str(path)


# A plain word boundary would not split routed_nets; treat "_" as a boundary so
# such keys are caught.
_STATUS_CLAIM = re.compile(r"(?<![A-Za-z])(routed|done)(?![A-Za-z])", re.IGNORECASE)


def _strings(node, *, skip_key: str = "gate"):
    """Every key and string value in a result, except under the gate field."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == skip_key:
                continue
            yield key
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)
    elif isinstance(node, str):
        yield node


def _sweep_arguments(tool, board: Path) -> dict:
    properties = tool.input_schema.get("properties", {})
    args: dict = {}
    for key in tool.input_schema.get("required", []):
        kind = properties[key].get("type")
        if key == "pcb_path":
            args[key] = str(board)
        elif key == "pcb_paths":
            args[key] = [str(board)]
        elif key == "sch_path":
            args[key] = str(board.with_suffix(".kicad_sch"))
        elif key == "project_dir":
            args[key] = str(board.parent)
        elif kind == "string":
            args[key] = str(board.parent / "x") if ("path" in key or "dir" in key) else "x"
        elif kind == "array":
            args[key] = []
        elif kind == "object":
            args[key] = {}
        elif kind in ("integer", "number"):
            args[key] = 1
    if tool.name in _SWEEP_ARGUMENTS:
        extra = _SWEEP_ARGUMENTS[tool.name]
        args.update(extra(board) if callable(extra) else extra)
        if "pcb_path" in properties:
            args["pcb_path"] = str(board)
    return args


@pytest.mark.asyncio
async def test_no_tool_output_but_the_gates_says_routed_or_done(board: Path) -> None:
    swept: list[str] = []
    async with Client(build_server(), mode="auto") as client:
        for name in [t["name"] for t in (await _call(client, "list_toolsets"))["result"]]:
            await _call(client, "load_toolset", name=name)
        tools = (await client.list_tools()).tools
        for tool in tools:
            if tool.name in _SWEEP_SKIP:
                continue
            try:
                result = await client.call_tool(tool.name, _sweep_arguments(tool, board))
            except Exception as exc:  # a protocol-level error is still output
                text = str(exc)
            else:
                text = json.dumps(result.structured_content) if result.structured_content is not None \
                    else "\n".join(getattr(c, "text", "") for c in result.content)
            swept.append(tool.name)
            for claim in _STATUS_CLAIM.findall("\n".join(_strings_of(text))):
                pytest.fail(f"{tool.name} output claims {claim!r}: {text[:200]}")

    registered = {t.name for t in tools}
    assert _SWEEP_SKIP.keys() <= registered, f"skip entries for unknown tools: {_SWEEP_SKIP.keys() - registered}"
    assert set(swept) == registered - _SWEEP_SKIP.keys(), "swept set must be exactly registered minus skipped"
    assert set(_SWEEP_ARGUMENTS) <= set(swept)
    assert {"pcb_run_drc", "get_unrouted_nets", "project_open", "add_traces"} <= set(swept)


def _strings_of(text: str):
    try:
        return list(_strings(json.loads(text)))
    except ValueError:
        return [text]


# --- review round: what the verdict is tied to --------------------------------
#
# Each staleness mechanism has a test the others cannot cover:
#   board bytes     -> test_external_edit_of_the_board_file_is_stale
#   graded inputs   -> test_rewriting_only_the_{rules,schematic}_file_is_stale
#   mutation counter-> test_a_schematic_edit_makes_the_verdict_stale_even_if_board_bytes_match
#                      (through a tool, no file hash can change: fix_annotation no-ops
#                       on the bytes) and the ancestry / unknown-mutation tests below.


@pytest.fixture
def pass_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace only the gate's grading with a PASS (the S1 boundary), so staleness
    is tested against a verdict that looks trustworthy."""
    from kicad_mcp import verify_board as gate

    def fake(pcb_path, **_kwargs):
        return {"verdict": "PASS", "unconnected": [], "violations": []}

    monkeypatch.setattr(gate, "verify_board", fake)


async def _pass_then_read(client: Client, board: Path) -> dict:
    await _call(client, "load_toolset", name="verification")
    graded = await _call(
        client, "verify_board", pcb_path=str(board), parity="not_applicable",
        parity_reason="test", baseline={},
    )
    assert graded["verdict"] == "PASS"
    out = await _call(client, "pcb_run_drc", pcb_path=str(board))
    assert out["gate"]["verdict"] == "PASS" and out["gate"]["stale"] is False
    return out


@pytest.mark.asyncio
async def test_external_edit_of_the_board_file_is_stale(board: Path, pass_verdict: None) -> None:
    async with Client(build_server(), mode="auto") as client:
        before = await _pass_then_read(client, board)
        faults.drift(board)  # an editor outside any tool
        out = await _call(client, "pcb_check_collisions", pcb_path=str(board))

    assert out["gate"]["verdict"] == "PASS"
    assert out["gate"]["stale"] is True
    assert out["gate"]["board_sha"] != before["gate"]["board_sha"]


@pytest.mark.asyncio
async def test_rewriting_only_the_rules_file_is_stale(board: Path, pass_verdict: None) -> None:
    rules = board.with_suffix(".kicad_dru")
    rules.write_text("(version 1)\n", encoding="utf-8")
    async with Client(build_server(), mode="auto") as client:
        before = await _pass_then_read(client, board)
        rules.write_text('(version 1)\n(rule "weaker" (condition "A.Type == \'via\'"))\n', encoding="utf-8")
        out = await _call(client, "pcb_check_collisions", pcb_path=str(board))

    assert out["gate"]["board_sha"] == before["gate"]["board_sha"]  # board bytes untouched
    assert out["gate"]["stale"] is True


@pytest.mark.asyncio
async def test_rewriting_only_the_schematic_file_is_stale(board: Path, pass_verdict: None) -> None:
    sch = board.with_suffix(".kicad_sch")
    async with Client(build_server(), mode="auto") as client:
        before = await _pass_then_read(client, board)
        faults.drift(sch)
        out = await _call(client, "pcb_check_collisions", pcb_path=str(board))

    assert out["gate"]["board_sha"] == before["gate"]["board_sha"]
    assert out["gate"]["stale"] is True


@pytest.mark.asyncio
async def test_other_boards_in_the_directory_do_not_make_a_verdict_stale(
    board: Path, pass_verdict: None
) -> None:
    # The gate grades a copy that leaves other .kicad_pcb files out, so they are not inputs.
    async with Client(build_server(), mode="auto") as client:
        await _pass_then_read(client, board)
        (board.parent / "sibling.KICAD_PCB").write_text("(kicad_pcb)\n", encoding="utf-8")
        out = await _call(client, "pcb_check_collisions", pcb_path=str(board))

    assert out["gate"]["stale"] is False


@pytest.mark.asyncio
async def test_project_dir_mutation_stales_nested_boards_and_not_lookalike_dirs(
    tmp_path: Path, pass_verdict: None
) -> None:
    nested = tmp_path / "proj.v2" / "hw"
    lookalike = tmp_path / "proj.v2-copy"
    for directory in (nested, lookalike):
        directory.mkdir(parents=True)
        for src in FIXTURES.glob("simple_led.*"):
            shutil.copy(src, directory / src.name)
    inner = nested / "simple_led.kicad_pcb"
    other = lookalike / "simple_led.kicad_pcb"

    async with Client(build_server(), mode="auto") as client:
        await _pass_then_read(client, inner)
        await _pass_then_read(client, other)
        await _call(client, "load_toolset", name="library")
        # The call may be refused; it is a mutating tool addressed at the project dir.
        await _call(
            client, "register_library_asset", project_dir=str(tmp_path / "proj.v2"),
            symbol_lib_path=str(tmp_path / "none.kicad_sym"), nickname="x",
        )
        stale_inner = await _call(client, "pcb_check_collisions", pcb_path=str(inner))
        fresh_other = await _call(client, "pcb_check_collisions", pcb_path=str(other))

    assert stale_inner["gate"]["stale"] is True
    assert fresh_other["gate"]["stale"] is False


@pytest.mark.asyncio
async def test_a_mutating_call_that_cannot_name_its_board_stales_every_verdict(
    board: Path, pass_verdict: None
) -> None:
    async with Client(build_server(), mode="auto") as client:
        await _pass_then_read(client, board)
        await _call(client, "load_toolset", name="copilot")
        await _call(client, "copilot_apply_proposal", revision_token="unknown-token")
        out = await _call(client, "pcb_check_collisions", pcb_path=str(board))

    assert out["gate"]["verdict"] == "PASS"
    assert out["gate"]["stale"] is True


def test_a_mutation_during_grading_is_not_forgotten(tmp_path: Path) -> None:
    from kicad_mcp.gate_state import GateState

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)\n", encoding="utf-8")
    state = GateState()
    snapshot = state.begin_grading(pcb)
    state.note_mutation(pcb)  # a tool ran while the gate was grading
    state.record_verdict(pcb, "PASS", snapshot)

    assert state.gate_field(pcb)["stale"] is True


def test_board_suffix_match_ignores_case(tmp_path: Path) -> None:
    from kicad_mcp.gate_state import attach_gate_field, GateState

    state = GateState()
    wrapped = attach_gate_field(lambda pcb_path: {"status": "ok"}, {"readOnlyHint": True}, state)
    out = wrapped(pcb_path=str(tmp_path / "BOARD.KICAD_PCB"))

    assert out["gate"]["verdict"] == "NOT_RUN"


@pytest.mark.asyncio
async def test_placement_and_drc_descriptions_point_to_the_gate() -> None:
    async with Client(build_server(), mode="auto") as client:
        await _call(client, "load_toolset", name="verification")
        tools = {t.name: t.description for t in (await client.list_tools()).tools}

    for name in ("pcb_check_placement", "pcb_check_collisions", "pcb_run_drc"):
        assert "verify_board" in tools[name], name
    assert "authoritative full check" not in tools["pcb_check_placement"]


def test_status_claim_pattern_catches_keys_with_underscores() -> None:
    assert _STATUS_CLAIM.search("routed_nets")
    assert _STATUS_CLAIM.search("board is DONE")
    assert not _STATUS_CLAIM.search("unrouted_nets")


# --- round 2: fingerprint cost, caps, symlinks, and every input file ------------


def _state_with_verdict(pcb: Path, verdict: str = "PASS"):
    from kicad_mcp.gate_state import GateState

    state = GateState()
    state.record_verdict(pcb, verdict, state.begin_grading(pcb))
    assert state.gate_field(pcb)["stale"] is False
    return state


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """simple_led plus one of every non-board input the gate's copy can read."""
    root = tmp_path / "proj"
    root.mkdir()
    for src in FIXTURES.glob("simple_led.*"):
        shutil.copy(src, root / src.name)
    (root / "simple_led.kicad_dru").write_text("(version 1)\n", encoding="utf-8")
    (root / "fp-lib-table").write_text("(fp_lib_table)\n", encoding="utf-8")
    (root / "sym-lib-table").write_text("(sym_lib_table)\n", encoding="utf-8")
    (root / "libs").mkdir()
    (root / "libs" / "x.kicad_sym").write_text("(kicad_symbol_lib)\n", encoding="utf-8")
    (root / "libs" / "x.pretty").mkdir()
    (root / "libs" / "x.pretty" / "y.kicad_mod").write_text("(footprint)\n", encoding="utf-8")
    (root / "sheets").mkdir()
    (root / "sheets" / "sub.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    return root / "simple_led.kicad_pcb"


@pytest.mark.parametrize(
    "relative",
    [
        "simple_led.kicad_pro", "simple_led.kicad_prl", "simple_led.kicad_dru",
        "fp-lib-table", "sym-lib-table", "libs/x.kicad_sym",
        "libs/x.pretty/y.kicad_mod", "sheets/sub.kicad_sch",
    ],
)
def test_rewriting_any_single_input_file_makes_the_verdict_stale(project: Path, relative: str) -> None:
    state = _state_with_verdict(project)
    target = project.parent / relative
    faults.rewrite_bytes(target, lambda data: data + b"\n; changed\n")

    assert state.gate_field(project)["stale"] is True


@pytest.mark.parametrize("skipped", [".git", "__pycache__", ".venv", "node_modules", "x-backups"])
def test_changes_inside_directories_the_gate_skips_do_not_make_a_verdict_stale(
    project: Path, skipped: str
) -> None:
    ignored = project.parent / skipped
    ignored.mkdir()
    (ignored / "a.kicad_sch").write_text("(kicad_sch)\n", encoding="utf-8")
    state = _state_with_verdict(project)
    (ignored / "a.kicad_sch").write_text("(kicad_sch) ; changed\n", encoding="utf-8")

    assert state.gate_field(project)["stale"] is False

    normal = project.parent / "normal"
    normal.mkdir()
    (normal / "a.kicad_sch").write_text("(kicad_sch) ; new sheet\n", encoding="utf-8")
    assert state.gate_field(project)["stale"] is True


def test_a_mutation_in_a_subsheet_directory_stales_the_board_above_it(project: Path) -> None:
    state = _state_with_verdict(project)
    state.note_mutation(project.parent / "sheets" / "sub.kicad_sch")

    assert state.gate_field(project)["stale"] is True


def test_unchanged_files_are_not_read_again_and_changed_ones_are(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_mcp import gate_state

    reads: list[str] = []
    real = gate_state._hash_file
    always = {"simple_led.kicad_pcb", "simple_led.kicad_dru", "simple_led.kicad_pro"}

    def counting(path: Path):
        if path.name not in always:  # these are re-read every call by design
            reads.append(path.name)
        return real(path)

    monkeypatch.setattr(gate_state, "_hash_file", counting)
    state = _state_with_verdict(project)
    first = len(reads)
    assert reads, "bulk files were read on the first call"

    state.gate_field(project)
    state.gate_field(project)
    assert len(reads) == first, "cache hit must not re-read"

    sheet = project.parent / "sheets" / "sub.kicad_sch"
    faults.drift(sheet)  # size changes
    assert state.gate_field(project)["stale"] is True
    assert reads[first:] == ["sub.kicad_sch"]

    stat = sheet.stat()
    os.utime(sheet, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))  # mtime only
    state.gate_field(project)
    assert reads[first:] == ["sub.kicad_sch", "sub.kicad_sch"]


def test_several_boards_in_one_directory_are_walked_once(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_mcp import gate_state

    second = project.parent / "second.kicad_pcb"
    shutil.copy(project, second)
    walks: list[str] = []
    real_walk = os.walk

    def counting_walk(top, *args, **kwargs):
        walks.append(str(top))
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(gate_state.os, "walk", counting_walk)
    fields = gate_state.GateState().gate_fields([project, second])

    assert set(fields) == {str(project), str(second)}
    assert len(walks) == 1


def test_project_over_the_size_cap_is_never_shown_fresh(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_mcp import gate_state

    state = _state_with_verdict(project)
    monkeypatch.setattr(gate_state, "_MAX_COPY_BYTES", 10)
    field = state.gate_field(project)

    assert field["project_sha"] is None
    assert field["stale"] is True
    assert "project too large to fingerprint: verdict cannot be shown fresh" in field["fingerprint_note"]


def test_project_over_the_file_count_cap_is_never_shown_fresh(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_mcp import gate_state

    state = _state_with_verdict(project)
    monkeypatch.setattr(gate_state, "_MAX_FILES", 2)
    field = state.gate_field(project)

    assert field["stale"] is True
    assert "project too large to fingerprint" in field["fingerprint_note"]


def test_an_unreadable_input_file_is_named_and_never_shown_fresh(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_mcp import gate_state

    state = _state_with_verdict(project)
    real = gate_state._hash_file

    def flaky(path: Path):
        return None if path.name == "x.kicad_sym" else real(path)

    monkeypatch.setattr(gate_state, "_hash_file", flaky)
    # a different mtime so the cache cannot answer for the unreadable file
    sym = project.parent / "libs" / "x.kicad_sym"
    os.utime(sym, ns=(sym.stat().st_atime_ns, sym.stat().st_mtime_ns + 9_000_000_000))
    field = state.gate_field(project)

    assert field["stale"] is True
    assert "x.kicad_sym" in field["fingerprint_note"]
    assert "unreadable" in field["fingerprint_note"]


def test_a_symlinked_library_directory_is_fingerprinted_and_cycles_terminate(project: Path) -> None:
    outside = project.parent.parent / "shared_libs"
    outside.mkdir()
    (outside / "z.kicad_sym").write_text("(kicad_symbol_lib)\n", encoding="utf-8")
    try:
        os.symlink(outside, project.parent / "linked", target_is_directory=True)
        os.symlink(project.parent, outside / "loop", target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"cannot create directory symlinks without privilege here: {exc}")

    state = _state_with_verdict(project)  # a cycle must not hang or raise
    (outside / "z.kicad_sym").write_text("(kicad_symbol_lib) ; changed\n", encoding="utf-8")

    assert state.gate_field(project)["stale"] is True


def test_token_memory_is_bounded() -> None:
    from kicad_mcp.gate_state import GateState

    state = GateState()
    for i in range(5000):
        state.remember_token(f"t{i}", ["a.kicad_pcb"])

    assert state.recall_token("t4999") == ["a.kicad_pcb"]
    assert state.recall_token("t0") == []
    assert state.token_count() <= 1024


def test_the_walk_follows_directory_symlinks_like_the_gates_copy(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Independent of symlink privilege: the gate's copytree(symlinks=False) follows
    # links, so the fingerprint walk must ask os.walk to follow them too.
    from kicad_mcp import gate_state

    seen: list[bool] = []
    real_walk = os.walk

    def spying_walk(top, *args, **kwargs):
        seen.append(kwargs.get("followlinks", False))
        return real_walk(top, *args, **kwargs)

    monkeypatch.setattr(gate_state.os, "walk", spying_walk)
    gate_state.GateState().gate_field(project)

    assert seen == [True]


def test_symlink_loops_are_pruned_on_the_way_down(monkeypatch: pytest.MonkeyPatch) -> None:
    from kicad_mcp import gate_state

    # /p/linked -> /shared ; /shared/loop -> /p  (a loop through two links)
    targets = {"/p": "/p", "/p/linked": "/shared", "/p/linked/loop": "/p", "/p/real": "/p/real"}
    monkeypatch.setattr(gate_state.os.path, "realpath", lambda p: targets.get(str(p).replace("\\", "/"), str(p)))
    chains = {"/p": frozenset({"/p"})}

    dirs = ["linked", "real"]
    gate_state._prune_cycles("/p", dirs, chains)
    assert dirs == ["linked", "real"]

    inner = os.path.join("/p", "linked")
    loop_dirs = ["loop"]
    gate_state._prune_cycles(inner, loop_dirs, chains)
    assert loop_dirs == [], "/shared/loop resolves back to /p, an ancestor on this path"


def _same_size_other_content(data: bytes) -> bytes:
    changed = bytearray(data)
    changed[-2] = (changed[-2] + 1) % 256  # same size, different content
    return bytes(changed)


@pytest.mark.parametrize("relative", ["simple_led.kicad_pcb", "simple_led.kicad_dru", "simple_led.kicad_pro"])
def test_same_size_edit_with_restored_mtime_is_caught_for_board_rules_and_project(
    project: Path, relative: str
) -> None:
    state = _state_with_verdict(project)
    target = project.parent / relative
    before = target.stat()
    faults.rewrite_bytes(target, _same_size_other_content)
    os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert target.stat().st_size == before.st_size
    assert target.stat().st_mtime_ns == before.st_mtime_ns

    assert state.gate_field(project)["stale"] is True


@pytest.mark.asyncio
async def test_toolset_descriptions_do_not_call_a_drc_run_authoritative() -> None:
    async with Client(build_server(), mode="auto") as client:
        listing = (await _call(client, "list_toolsets"))["result"]

    verification = next(t["description"] for t in listing if t["name"] == "verification")
    assert "NOT the acceptance gate" in verification
    assert "verify_board" in verification
    assert "Authoritative" not in verification
