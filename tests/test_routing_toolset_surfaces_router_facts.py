"""Ticket 03 review round 1, finding 3: the routing toolset surfaces what the
router wrapper now knows (exit 3, delivered-below-requested, version check,
logged parameters). A fake `run_kicad_routing_tools` replaces the subprocess;
the diff, apply and real kicad-cli DRC downstream are real.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from kicad_mcp.routers import kicad_routing_tools as krt
from kicad_mcp.toolsets import routing as routing_toolset

GOLDEN_PCB = Path(__file__).parent / "fixtures" / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"


def _golden_copy(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    pcb = tmp_path / GOLDEN_PCB.name
    shutil.copy(GOLDEN_PCB, pcb)
    for suffix in (".kicad_pro", ".kicad_prl"):
        sibling = GOLDEN_PCB.with_suffix(suffix)
        if sibling.is_file():
            shutil.copy(sibling, pcb.with_suffix(suffix))
    return pcb


def _tools() -> dict:
    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _fake_runner(exit_code=0, narrowed=None, version_matches=True):
    def run(input_pcb, output_pcb, **kwargs):
        out = Path(output_pcb)
        out.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(input_pcb, out)
        summary = {"design_rules": {"count": len(narrowed or []), "narrowed": narrowed or []}}
        log = krt.parse_route_log("", json.dumps(summary))
        violated = exit_code == 3 or bool(log["delivered_below_requested"])
        return {
            "script": "route.py", "exit_code": exit_code, "elapsed_s": 0.1, "args": ["--strict-sizes"],
            "output_pcb": str(out), "project_file_written_by_router": False, "router_overwrote_project": False,
            "project_restored": True, "stderr_tail": "", "stdout_log": "", "stderr_log": "", "stop_file": "",
            "graceful_stop_honored": False, "log": log,
            "logged_parameters": {"strict_sizes": True, "layer_costs": None, "advanced_flags": {}},
            "router_version": "0.22.0",
            "version_check": {"matches": version_matches, "pinned": "0.22.0", "live": "0.22.0",
                              "message": "router version matches snapshot" if version_matches else "router version differs"},
            "strict_sizes_violated": violated, "delivered_below_requested": log["delivered_below_requested"],
        }

    return run


def _propose(tmp_path, monkeypatch, runner):
    monkeypatch.setattr(krt, "check_availability",
                        lambda **kw: {"status": "complete", "checkout": "c", "python": "p", "grid_router_version": "x"})
    monkeypatch.setattr(krt, "run_kicad_routing_tools", runner)
    pcb = _golden_copy(tmp_path)
    return _tools()["propose_route_with_retry"](str(pcb), max_attempts=1, backend="kicad_routing_tools",
                                                escalation="board")


def test_toolset_surfaces_exit_3_and_delivered_below_requested(tmp_path, monkeypatch):
    narrowed = [{"net_name": "SDA", "kind": "track_width", "requested": 0.3, "delivered": 0.2, "count": 1}]
    result = _propose(tmp_path, monkeypatch, _fake_runner(exit_code=3, narrowed=narrowed))
    assert result["status"] == "pending_confirmation", result
    assert result["strict_sizes_violated"] is True
    assert result["delivered_below_requested"] == [
        {"net_name": "SDA", "kind": "track_width", "requested_mm": 0.3, "delivered_mm": 0.2}]
    assert result["logged_parameters"]["strict_sizes"] is True
    assert result["version_check"]["matches"] is True
    assert result["attempts"][0]["router_exit_status"] == "delivered_below_requested_size"
    assert "delivered below the requested size" in result["warning"].lower()


def test_toolset_other_nonzero_exit_is_a_router_failure_not_success(tmp_path, monkeypatch):
    result = _propose(tmp_path, monkeypatch, _fake_runner(exit_code=1))
    assert result["attempts"][0]["router_exit_status"] == "router_exit_nonzero:1"
    assert result["strict_sizes_violated"] is False


def test_toolset_clean_run_says_so_and_version_mismatch_is_surfaced(tmp_path, monkeypatch):
    result = _propose(tmp_path, monkeypatch, _fake_runner(exit_code=0, version_matches=False))
    assert result["attempts"][0]["router_exit_status"] == "ok"
    assert result["strict_sizes_violated"] is False
    assert result["delivered_below_requested"] == []
    assert result["version_check"]["matches"] is False
    assert "version" in result["warning"].lower()


def _clobbering_runner(inner):
    """Wraps a fake runner so it really overwrites the .kicad_pro beside the
    trial input AND the routed output, as KRT does."""

    def run(input_pcb, output_pcb, **kwargs):
        result = inner(input_pcb, output_pcb, **kwargs)
        for pcb in (Path(input_pcb), Path(output_pcb)):
            pcb.with_suffix(".kicad_pro").write_text('{"clobbered_by_router": true}', encoding="utf-8")
        return result

    return run


def test_original_project_stays_byte_identical_when_the_router_clobbers_the_trial_copy(tmp_path, monkeypatch):
    pcb = _golden_copy(tmp_path / "board")
    before = pcb.with_suffix(".kicad_pro").read_bytes()
    monkeypatch.setattr(krt, "check_availability", lambda **kw: {"status": "complete"})
    monkeypatch.setattr(krt, "run_kicad_routing_tools", _clobbering_runner(_fake_runner()))
    result = _tools()["propose_route_with_retry"](str(pcb), max_attempts=1, backend="kicad_routing_tools",
                                                  escalation="board")
    assert result["status"] == "pending_confirmation"
    assert pcb.with_suffix(".kicad_pro").read_bytes() == before
    assert b"clobbered_by_router" not in pcb.with_suffix(".kicad_pro").read_bytes()


# ---- round 2, finding 1: a flagged proposal must not confirm silently ------


def test_selection_prefers_an_unflagged_attempt_on_a_tie_but_a_better_flagged_one_wins():
    rank = routing_toolset._attempt_rank
    assert rank((0, 3, 0), []) < rank((0, 3, 0), ["strict_sizes_violated"])
    assert rank((0, 2, 0), ["strict_sizes_violated"]) < rank((0, 3, 0), [])
    assert rank((0, 3, 0), ["router_exit_nonzero:1"]) > rank((0, 3, 0), [])


def test_router_flags_are_derived_from_the_attempt_record():
    flags = routing_toolset._router_flags
    assert flags({"strict_sizes_violated": False, "router_exit_status": "ok", "version_check": {"matches": True}}) == []
    assert flags({"strict_sizes_violated": True, "router_exit_status": "delivered_below_requested_size",
                  "version_check": {"matches": True}}) == ["strict_sizes_violated"]
    assert flags({"strict_sizes_violated": False, "router_exit_status": "router_exit_nonzero:1",
                  "version_check": {"matches": False}}) == ["router_exit_nonzero:1", "router_version_mismatch"]


def test_flagged_best_attempt_requires_acknowledgement_and_confirm_refuses(tmp_path, monkeypatch):
    narrowed = [{"net_name": "SDA", "kind": "track_width", "requested": 0.3, "delivered": 0.2, "count": 1}]
    monkeypatch.setattr(krt, "check_availability", lambda **kw: {"status": "complete"})
    monkeypatch.setattr(krt, "run_kicad_routing_tools", _fake_runner(exit_code=3, narrowed=narrowed))
    pcb = _golden_copy(tmp_path)
    tools = _tools()
    proposal = tools["propose_route_with_retry"](str(pcb), max_attempts=1, backend="kicad_routing_tools",
                                                 escalation="board")
    assert proposal["requires_acknowledgement"] is True
    assert "strict_sizes_violated" in proposal["router_flags"]

    before = pcb.read_bytes()
    refused = tools["confirm_route"](str(pcb), proposal["revision_token"])
    assert refused["status"] == "refused"
    assert "strict_sizes_violated" in refused["reason"]
    assert "acknowledge_router_flags" in refused["reason"]
    assert pcb.read_bytes() == before, "a refusal must not touch the board"

    confirmed = tools["confirm_route"](str(pcb), proposal["revision_token"], acknowledge_router_flags=True)
    assert confirmed["status"] == "confirmed", confirmed  # token stayed valid after the refusal
    assert confirmed["router_flags_acknowledged"] == proposal["router_flags"]


def test_clean_proposal_confirms_without_acknowledgement(tmp_path, monkeypatch):
    monkeypatch.setattr(krt, "check_availability", lambda **kw: {"status": "complete"})
    monkeypatch.setattr(krt, "run_kicad_routing_tools", _fake_runner(exit_code=0))
    pcb = _golden_copy(tmp_path)
    tools = _tools()
    proposal = tools["propose_route_with_retry"](str(pcb), max_attempts=1, backend="kicad_routing_tools",
                                                 escalation="board")
    assert proposal["requires_acknowledgement"] is False and proposal["router_flags"] == []
    assert tools["confirm_route"](str(pcb), proposal["revision_token"])["status"] == "confirmed"


def test_unflagged_retry_beats_a_flagged_first_attempt_on_equal_score(tmp_path, monkeypatch):
    clean, flagged = _fake_runner(exit_code=0), _fake_runner(exit_code=3)
    calls = {"n": 0}

    def alternating(input_pcb, output_pcb, **kw):
        calls["n"] += 1
        return (flagged if calls["n"] == 1 else clean)(input_pcb, output_pcb, **kw)

    monkeypatch.setattr(krt, "check_availability", lambda **kw: {"status": "complete"})
    monkeypatch.setattr(krt, "run_kicad_routing_tools", alternating)
    result = _tools()["propose_route_with_retry"](str(_golden_copy(tmp_path)), max_attempts=2,
                                                  backend="kicad_routing_tools", escalation="board")
    assert result["attempts_tried"] == 2, "test needs a second attempt to exist"
    assert result["best_attempt"] == 2
    assert result["requires_acknowledgement"] is False


# ---- round 2, finding 7: script-level fake with a parsing board ------------

_REAL_SHAPED_ROUTER = """\
import json, shutil, sys
from pathlib import Path
src, out = Path(sys.argv[1]), Path(sys.argv[2])
shutil.copy(src, out)
from kicad_tools.schema import PCB
from kicad_mcp import write_layer  # PCB.save fails on Windows from 0.22.0
board = PCB.load(out)
board.add_trace((100.0, 100.0), (101.5, 100.0), width=0.2, layer="F.Cu", net="GND")
write_layer.save_pcb(board, out)
summary = {"scope": "run", "routed_single": ["GND"], "failed_single": [], "total_vias": 0,
           "design_rules": {"escalation_policy": "board", "count": 1,
                            "narrowed": [{"net_name": "GND", "kind": "track_width", "requested": 0.3,
                                          "delivered": 0.2, "count": 1}]}}
print("JSON_SUMMARY: " + json.dumps(summary))
print("PROJECT_WRITES_JSON: " + json.dumps({"writes": [{"key": "rules.min_clearance", "from": "0.2", "to": "0.1"}]}))
print("  rules.min_clearance: 0.2 -> 0.1 mm")
out.with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
src.with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
sys.exit(3)
"""


def test_real_wrapper_with_a_script_level_fake_router_end_to_end(tmp_path, monkeypatch):
    checkout = tmp_path / "krt"
    (checkout / "py_router").mkdir(parents=True)
    (checkout / "py_router" / "route.py").write_text(_REAL_SHAPED_ROUTER, encoding="utf-8")
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    monkeypatch.setattr(krt, "check_availability", lambda **kw: {"status": "complete"})
    pcb = _golden_copy(tmp_path / "board")
    original_project = pcb.with_suffix(".kicad_pro").read_bytes()

    tools = _tools()
    result = tools["propose_route_with_retry"](str(pcb), max_attempts=1, backend="kicad_routing_tools",
                                               escalation="board")
    assert result["status"] == "pending_confirmation", result
    assert result["trace_count"] >= 1, "the diff/apply/DRC path must have seen real copper"
    attempt = result["attempts"][0]
    assert attempt["router_exit_status"] == "delivered_below_requested_size"
    assert result["strict_sizes_violated"] is True
    assert result["requires_acknowledgement"] is True
    assert result["delivered_below_requested"][0]["delivered_mm"] == 0.2
    assert attempt["router_log"]["project_rule_writes"][0]["key"] == "rules.min_clearance"
    assert attempt["router_rewrote_project_file"] is True
    assert pcb.with_suffix(".kicad_pro").read_bytes() == original_project
    assert tools["confirm_route"](str(pcb), result["revision_token"])["status"] == "refused"
