"""Tests for the KiCadRoutingTools routing backend (archived backlog P4-05).

Unit tests need no KRT install: discovery, the dependency probe's
actionable failure, `JSON_SUMMARY` parsing against text copied from real
KRT v0.22.0 logs, the fab-overrides derivation, argument construction, and
the delta diff/round-trip through kicad_tools on a board routed via
kicad_tools' own API.

The end-to-end class runs the real subprocess on the real golden-path
board and carries the `needs_krt` marker (build ticket 53) -- set
KICAD_ROUTING_TOOLS_PATH (a checkout containing py_router/route.py, with
`python build_router.py` run once) and KICAD_ROUTING_TOOLS_PYTHON (an
interpreter with numpy/scipy/shapely/Pillow) -- and skips with the probe's
own reason otherwise, or fails when KICAD_ROUTING_TOOLS_REQUIRED is set (the
gate run). It never fakes a route.
"""

from __future__ import annotations

import json
import shutil
import threading
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import write_layer
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


def _fake_checkout(root: Path) -> Path:
    (root / "py_router").mkdir(parents=True)
    (root / "py_router" / "route.py").write_text("# stub\n", encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_find_via_env_override(tmp_path, monkeypatch):
    checkout = _fake_checkout(tmp_path / "krt")
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    assert krt.find_kicad_routing_tools() == checkout


def test_env_override_must_be_the_checkout_dir_not_route_py(tmp_path, monkeypatch):
    checkout = _fake_checkout(tmp_path / "krt")
    monkeypatch.setenv(krt.ENV_PATH, str(checkout / "py_router"))
    with pytest.raises(krt.KicadRoutingToolsError, match="py_router/route.py"):
        krt.find_kicad_routing_tools()


def test_find_via_platform_glob_prefers_newest_kicad_version(tmp_path, monkeypatch):
    monkeypatch.delenv(krt.ENV_PATH, raising=False)
    old = _fake_checkout(tmp_path / "KiCad" / "9.0" / "3rdparty" / "plugins" / "com_github_drandyhaas_kicadroutingtools")
    new = _fake_checkout(tmp_path / "KiCad" / "10.0" / "3rdparty" / "plugins" / "com_github_drandyhaas_kicadroutingtools")
    monkeypatch.setattr(
        krt, "_PLUGIN_GLOBS", {krt.sys.platform: [str(tmp_path / "KiCad" / "*" / "3rdparty" / "plugins" / "*" / "py_router" / "route.py")]}
    )
    assert krt.find_kicad_routing_tools() == new
    assert old != new


def test_missing_install_is_actionable(monkeypatch):
    monkeypatch.delenv(krt.ENV_PATH, raising=False)
    monkeypatch.setattr(krt, "_PLUGIN_GLOBS", {})
    with pytest.raises(krt.KicadRoutingToolsError, match="KICAD_ROUTING_TOOLS_PATH"):
        krt.find_kicad_routing_tools()
    result = krt.check_availability()
    assert result["status"] == "incomplete"
    assert "drandyhaas/KiCadRoutingTools" in result["reason"]


def test_python_override_default_is_this_interpreter(monkeypatch):
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    assert krt.find_kicad_routing_tools_python() == Path(krt.sys.executable)
    monkeypatch.setenv(krt.ENV_PYTHON, "/nope/python")
    with pytest.raises(krt.KicadRoutingToolsError):
        krt.find_kicad_routing_tools_python()


def test_availability_probe_names_missing_binary_and_install_command(tmp_path, monkeypatch):
    """A checkout without the prebuilt grid_router binary must come back as
    an actionable incomplete naming `build_router.py`, never raise."""
    checkout = _fake_checkout(tmp_path / "krt")
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    result = krt.check_availability()
    assert result["status"] == "incomplete"
    assert "grid_router" in result["reason"]
    assert any("build_router.py" in hint for hint in result["install_hints"])
    assert result["checkout"] == str(checkout)
    # Any missing pip packages in this interpreter are named with a pip line too.
    for pkg in result["missing_packages"]:
        assert any(pkg in hint and "pip install" in hint for hint in result["install_hints"])


# ---------------------------------------------------------------------------
# Log parsing -- text copied from real KRT v0.22.0 runs on the golden board
# (2026-09-11 evaluation). The `narrowed` lists are trimmed; every field and
# line shape is as emitted.
# ---------------------------------------------------------------------------

_BOARD_POLICY_LOG = r"""
Rescue pass: 1 recovered, 0 improved, 2 unchanged (1.9s)
JSON_SUMMARY: {"routed_single": ["Net-(D1-K)", "Net-(U1-PA1)", "GND"], "failed_single": ["Net-(J2-Pin_2)"], "open_single": [], "failed_multipoint": [{"net_name": "VCC", "failed_pads": [{"component_ref": "U1", "pad_number": "?", "x": 147.05, "y": 105.4}]}], "multipoint_nets": 2, "multipoint_pads_connected": 9, "multipoint_pads_total": 10, "successful": 3, "failed": 2, "total_time": 0.47, "total_iterations": 20895, "total_vias": 2, "cleanup_disconnected": [], "min_clearance_used": 0.1818, "design_rules": {"escalation_policy": "board", "fab_tier": "auto", "board_floors": {"track_width": 0.2, "via_diameter": 0.5, "via_drill": 0.3, "hole_to_hole": 0.25, "annular": 0.1}, "count": 27, "nets": [1, 3, 4, 5], "narrowed": [{"net": 4, "net_name": null, "kind": "track_width", "requested": 0.25, "delivered": 0.2, "site": "terminal neck", "count": 1}, {"net": 5, "net_name": null, "kind": "via_diameter", "requested": 0.6, "delivered": 0.5, "site": "via-in-pad clamp (floor)", "count": 1}, {"net": 1, "net_name": "GND", "kind": "track_width", "requested": 0.25, "delivered": 0.2, "site": "net rescue", "count": 1}], "min_delivered": {"track_width": 0.2, "via_diameter": 0.5}, "fab_tier_escalations": 9, "unsupported_rules": []}, "rescue": {"attempted": 3, "recovered": ["GND"], "improved": [], "unchanged": ["Net-(J2-Pin_2)", "VCC"], "pads_reconnected": 1, "time": 1.88}, "terminal_escalations": {"Net-(J2-Pin_2)": "unrecovered", "VCC": "unrecovered"}, "oracle_check": "disabled", "blockers": [{"net": "Net-(J2-Pin_2)", "stage": "single_ended", "blocked_by": [{"net": "Net-(U1-PA1)", "blocked_count": 5, "unique_cells": 5, "track_cells": 5, "via_cells": 0, "near_target_cells": 5, "near_source_cells": 0}]}], "boxed_in": [{"verdict": "boxed_in_static", "iterations": 5001, "geometry": {"grid_step": 0.1, "clearance": 0.2, "track_width": 0.25, "via_diameter": null}, "net": "Net-(J2-Pin_2)"}], "pad_pairs_connected": 9, "pad_pairs_total": 11, "pad_pairs_open": [{"net": "Net-(J2-Pin_2)", "pairs_connected": 0, "pairs_total": 1, "outcome": "open", "open_subtype": "unrouted"}, {"net": "VCC", "pairs_connected": 2, "pairs_total": 3, "outcome": "open", "open_subtype": "partial"}], "scope": "run", "env_knobs": {}}

Writing output to golden_esc_board.kicad_pcb...
Successfully wrote golden_esc_board.kicad_pcb
  In-run DRC floors (#650): lowered 1 value(s) in golden_esc_board.kicad_pro to the routed floors so the in-run audit grades what ships
      rules.min_hole_clearance: 0.25 -> 0.2 mm

Final reconciliation: retrying 2 incomplete/custody net(s) against the finished board: Net-(J2-Pin_2), VCC
JSON_SUMMARY: {"routed_single": [], "failed_single": ["VCC"], "open_single": [], "failed_multipoint": [], "successful": 0, "failed": 1, "total_time": 0.14, "total_iterations": 5001, "total_vias": 0, "min_clearance_used": 0.1635, "design_rules": {"escalation_policy": "board", "fab_tier": "auto", "board_floors": {"track_width": 0.2}, "count": 0, "nets": [], "narrowed": [], "min_delivered": {}, "fab_tier_escalations": 0, "unsupported_rules": []}, "scope": "reconciliation-subset", "env_knobs": {}}
Note: the JSON_SUMMARY above covers only the reconciliation subset (it carries scope="reconciliation-subset"); the run's full tally is the earlier JSON_SUMMARY, the one with scope="run", plus these recoveries. Never scrape the LAST JSON_SUMMARY of a route log -- count them, or read the scope.
  Design rules [--escalation board, --fab-tier auto]: 52 feature(s) on 4 net(s) delivered below the requested size (smallest track width 0.2 mm, smallest via diameter 0.5 mm); 18 fab-tier escalation(s) to advanced. Details: JSON_SUMMARY design_rules.
JSON_SUMMARY_MIN: {"escalations": 70, "failed": 1, "failed_single": ["VCC"], "main_loop_time_s": 0.87, "min_clearance_used": 0.1635, "multipoint_deficit": 0, "open_single": [], "pad_pairs_open": {"count": 1, "nets": ["VCC"]}, "routed": 0, "scope": "merged", "terminal_restores_broken": [], "vias": 5}
"""

_FAB_POLICY_TAIL = r"""
  DRC settings: wrote 9 value(s) to ../krt_trial/golden_routed.kicad_pro to match the routed floors (close+reopen in KiCad if it is open). WRITES, not changes: a key the project never declared counts here, and one logical value is written once per net class:
      rules.min_hole_clearance: 0.2 -> 0.127 mm
      rules.min_copper_edge_clearance: 0.0 -> 0.2 mm (fab-edge pin)
      rules.min_track_width: 0.2 -> 0.127 mm
      rules.min_via_diameter: 0.5 -> 0.25 mm
      rules.min_through_hole_diameter: 0.3 -> 0.15 mm
      rules.min_via_drill: None -> 0.15 mm
      rules.min_via_annular_width: 0.1 -> 0.05 mm
      net_class[Default].clearance: 0.2 -> 0.127 mm
      kicad_routing_tools.fab_floor_origin: recorded
PROJECT_WRITES_JSON: {"project": "../krt_trial/golden_routed.kicad_pro", "writes": ["rules.min_hole_clearance: 0.2 -> 0.127 mm", "rules.min_track_width: 0.2 -> 0.127 mm", "net_class[Default].clearance: 0.2 -> 0.127 mm", "kicad_routing_tools.fab_floor_origin: recorded"]}
  FAB FLOOR RELAXED -- the output project declares a smaller minimum than the board originally did:
    track width: 0.2 -> 0.127 mm; 24 of 82 object(s) on this board are below the ORIGINAL 0.2mm
"""


def test_parse_route_log_picks_the_scope_run_summary_not_the_last_one():
    parsed = krt.parse_route_log(_BOARD_POLICY_LOG)
    assert parsed["source"] == "stdout:scope=run"
    assert parsed["summary_count_on_stdout"] == 2
    # From the whole-run summary, not the reconciliation subset:
    assert parsed["failed_nets"] == ["Net-(J2-Pin_2)", "VCC"]  # failed_single + failed_multipoint net names
    assert parsed["router_reported_nets"] == ["Net-(D1-K)", "Net-(U1-PA1)", "GND"]
    assert parsed["boxed_in_nets"] == ["Net-(J2-Pin_2)"]
    assert parsed["min_clearance_used_mm"] == 0.1818
    assert parsed["narrowed_min_delivered_mm"] == {"track_width": 0.2, "via_diameter": 0.5}
    assert parsed["board_floors_seen_by_router"]["track_width"] == 0.2
    assert parsed["escalation_policy"] == "board"
    assert parsed["rescue"]["recovered"] == ["GND"]
    # JSON_SUMMARY_MIN is the merged tally (min clearance of the whole run incl. reconciliation).
    assert parsed["summary_min"]["min_clearance_used"] == 0.1635
    # The project rewrite that happens even under --no-fix-drc-settings is surfaced.
    assert parsed["project_rule_writes"] == [{"key": "rules.min_hole_clearance", "from": "0.25", "to": "0.2"}]
    assert parsed["fab_floor_relaxed"] is False


def test_parse_route_log_prefers_json_out_file_when_given():
    merged = {"routed_single": ["A"], "failed_single": [], "min_clearance_used": 0.2, "scope": "merged", "design_rules": {}}
    parsed = krt.parse_route_log(_BOARD_POLICY_LOG, json.dumps(merged))
    assert parsed["source"] == "json_out"
    assert parsed["router_reported_nets"] == ["A"]
    assert parsed["failed_nets"] == []
    assert parsed["min_clearance_used_mm"] == 0.2


def test_parse_route_log_surfaces_every_project_write_under_fab_policy():
    parsed = krt.parse_route_log(_FAB_POLICY_TAIL)
    assert parsed["fab_floor_relaxed"] is True
    assert "net_class[Default].clearance: 0.2 -> 0.127 mm" in parsed["project_writes"]
    keys = [w["key"] for w in parsed["project_rule_writes"]]
    assert "rules.min_track_width" in keys and "net_class[Default].clearance" in keys
    assert parsed["source"] == "none"  # no JSON_SUMMARY in this excerpt -- reported, not invented


def test_parse_route_log_on_empty_output_is_harmless():
    parsed = krt.parse_route_log("")
    assert parsed["failed_nets"] == [] and parsed["source"] == "none"


def test_descents_below_floor_flags_clearance_from_self_report():
    parsed = krt.parse_route_log(_BOARD_POLICY_LOG)
    floors = {"clearance": 0.2, "track_width": 0.2, "via_diameter": 0.5}
    descents = krt.descents_below_floor(parsed, floors)
    assert descents == [{"rule": "clearance", "board_floor_mm": 0.2, "delivered_mm": 0.1818}]
    assert krt.descents_below_floor(parsed, {"clearance": 0.18}) == []


def test_delivered_rules_overlay_lowers_only_what_the_router_went_below():
    board = {"effective_rules": {"clearance_mm": {"value_mm": 0.2, "source": "board_netclass:Default", "candidates_mm": {}},
                                 "track_width_mm": {"value_mm": 0.25, "source": "board_netclass:Default", "candidates_mm": {}}}}
    log = {"min_clearance_used_mm": 0.127, "narrowed_min_delivered_mm": {"track_width": 0.127, "via_diameter": 0.25}}
    overlay = krt.delivered_rules_overlay(board, log)
    assert overlay["effective_rules"]["clearance_mm"]["value_mm"] == 0.127
    assert overlay["effective_rules"]["clearance_mm"]["source"] == "kicad_routing_tools:delivered_copper"
    assert overlay["effective_rules"]["track_width_mm"]["value_mm"] == 0.127
    assert overlay["effective_rules"]["via_diameter_mm"]["value_mm"] == 0.25  # absent on the board, added from delivery
    assert board["effective_rules"]["clearance_mm"]["value_mm"] == 0.2  # input untouched


# ---------------------------------------------------------------------------
# Floors, overrides file, argument construction
# ---------------------------------------------------------------------------


def test_board_floor_overrides_from_the_real_golden_project():
    report = krt.board_floor_overrides(GOLDEN_PCB)
    floors = report["floors"]
    # Clearance comes from the netclass (rules.min_clearance is 0.0 = unset in
    # KiCad's default state -- exactly the gap KRT falls into), the rest from
    # the project's explicit minimums.
    assert floors["clearance"] == 0.2 and report["sources"]["clearance"] == "board_netclass:Default"
    assert floors["track_width"] == 0.2 and report["sources"]["track_width"] == "board_project_rules:min_track_width"
    assert floors["via_diameter"] == 0.5
    assert floors["via_drill"] == 0.3
    assert floors["hole_to_hole"] == 0.25
    assert "board_edge" in report["omitted"]  # 0.0 = unset -> left to KRT, reported


def test_write_fab_overrides_is_plain_key_value_text(tmp_path):
    path = krt.write_fab_overrides({"clearance": 0.2, "track_width": 0.2}, tmp_path / "ov.txt")
    text = path.read_text(encoding="utf-8")
    assert "clearance = 0.2\n" in text and "track_width = 0.2\n" in text


def test_build_route_args_board_policy_pins_floor_and_never_fixes_drc_settings(tmp_path):
    args = krt.build_route_args("in.kicad_pcb", "out.kicad_pcb", escalation="board", clearance_mm=0.2,
                                fab_overrides=tmp_path / "ov.txt", grid_step_mm=0.05, max_iterations=800000,
                                json_out=tmp_path / "s.json")
    assert args[:2] == ["in.kicad_pcb", "out.kicad_pcb"]
    assert "--no-fix-drc-settings" in args
    assert args[args.index("--escalation") + 1] == "board"
    assert args[args.index("--clearance") + 1] == "0.2"
    assert args[args.index("--fab-overrides") + 1] == str(tmp_path / "ov.txt")
    assert args[args.index("--grid-step") + 1] == "0.05"
    assert args[args.index("--max-iterations") + 1] == "800000"
    assert "--json-out" in args


def test_build_route_args_rejects_unknown_escalation():
    with pytest.raises(ValueError):
        krt.build_route_args("a", "b", escalation="yolo")


# ---------------------------------------------------------------------------
# Delta diff / round-trip (no KRT needed: the "routed" board is produced
# through kicad_tools' own API, the only sanctioned way to mutate a board).
# ---------------------------------------------------------------------------


def test_route_delta_finds_exactly_the_added_copper_and_round_trips(tmp_path):
    from kicad_tools.schema import PCB

    original = _golden_copy(tmp_path / "orig")
    routed = _golden_copy(tmp_path / "routed")
    board = PCB.load(routed)
    board.add_trace((10.0, 8.0), (14.0, 8.0), width=0.25, layer="F.Cu", net="GND")
    board.add_trace((14.0, 8.0), (14.0, 12.0), width=0.2, layer="B.Cu", net="GND")
    board.add_via(14.0, 8.0, size=0.5, drill=0.3, net="GND")
    write_layer.save_pcb(board, routed)

    delta = krt.route_delta(original, routed)
    assert delta["removed_segment_count"] == 0 and delta["removed_via_count"] == 0
    assert len(delta["trace_specs"]) == 2
    assert len(delta["via_specs"]) == 1
    via = delta["via_specs"][0]
    assert (round(via["x"], 3), round(via["y"], 3)) == (14.0, 8.0)  # board-local, same frame add_via took
    assert via["size_mm"] == 0.5 and via["drill_mm"] == 0.3 and via["net"] == "GND"
    widths = sorted(t["width_mm"] for t in delta["trace_specs"])
    assert widths == [0.2, 0.25]

    # Round trip: applying the delta to a fresh copy reproduces the routed board.
    target = _golden_copy(tmp_path / "target")
    routing_toolset._apply_specs(target, delta["trace_specs"], delta["via_specs"])
    reloaded = PCB.load(target)
    assert len(reloaded.segments) == 2 and len(reloaded.vias) == 1
    again = krt.route_delta(target, routed)
    assert again["trace_specs"] == [] and again["via_specs"] == []


def test_route_delta_on_identical_boards_is_empty(tmp_path):
    original = _golden_copy(tmp_path / "a")
    delta = krt.route_delta(original, original)
    assert delta["trace_specs"] == [] and delta["via_specs"] == []


# ---------------------------------------------------------------------------
# _trial_copy sibling-file coverage (archived backlog P4-09: a bare .kicad_pcb
# trial copy silently loses per-netclass rules/library resolution)
# ---------------------------------------------------------------------------


def test_trial_copy_carries_every_sibling_krt_or_kicad_cli_can_read(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    pcb = src_dir / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")
    siblings = {
        "board.kicad_pro": "{}",
        "board.kicad_prl": "{}",
        "board.kicad_dru": "(version 1)",
        "fp-lib-table": "(fp_lib_table)",
        "sym-lib-table": "(sym_lib_table)",
    }
    for name, content in siblings.items():
        (src_dir / name).write_text(content, encoding="utf-8")

    trial = routing_toolset._trial_copy(pcb, tmp_path / "dest")

    assert trial.read_text(encoding="utf-8") == "(kicad_pcb)"
    for name, content in siblings.items():
        copied = trial.parent / name
        assert copied.is_file(), f"{name} was not copied into the trial dir"
        assert copied.read_text(encoding="utf-8") == content


def test_trial_copy_tolerates_missing_siblings(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    pcb = src_dir / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)", encoding="utf-8")

    trial = routing_toolset._trial_copy(pcb, tmp_path / "dest")

    assert trial.is_file()
    for name in ("board.kicad_pro", "board.kicad_prl", "board.kicad_dru", "fp-lib-table", "sym-lib-table"):
        assert not (trial.parent / name).exists()


def test_trial_copy_on_the_real_golden_board_carries_its_real_siblings(tmp_path):
    # Real project directory, not synthetic siblings like the test above --
    # confirms the fix against an actual fixture, and that adding
    # .kicad_dru/lib-table handling didn't break the pre-existing
    # .kicad_pro/.kicad_prl copy this fixture already exercises.
    trial = routing_toolset._trial_copy(GOLDEN_PCB, tmp_path / "dest")
    for suffix in (".kicad_pro", ".kicad_prl"):
        sibling = GOLDEN_PCB.with_suffix(suffix)
        assert sibling.is_file(), f"fixture assumption broken: {sibling} should exist"
        assert trial.with_suffix(suffix).read_bytes() == sibling.read_bytes()


# ---------------------------------------------------------------------------
# run_kicad_routing_tools streams stdout/stderr to real files instead of
# buffering them via subprocess.run(capture_output=True) (ticket 08:
# on a hard board, a single attempt can run 17-90+ minutes with zero
# visibility until it exits; a live log file is watchable mid-run and
# survives a timeout kill, where the old buffered approach lost it).
# No real KRT needed -- a tiny stand-in `route.py` prints known text.
# ---------------------------------------------------------------------------

_FAKE_ROUTE_SUCCESS = """\
import sys
from pathlib import Path
sys.stdout.write("bus corridor planning: fake progress line 1\\n")
sys.stdout.flush()
sys.stderr.write("fake warning on stderr\\n")
sys.stderr.flush()
Path(sys.argv[2]).write_text("(kicad_pcb fake-routed)\\n", encoding="utf-8")
sys.stdout.write("FAKE_ROUTE_DONE\\n")
"""

_FAKE_ROUTE_HANGS = """\
import sys, time
sys.stdout.write("fake route starting, about to hang\\n")
sys.stdout.flush()
time.sleep(30)
"""


def _fake_route_checkout(root: Path, script_body: str) -> Path:
    (root / "py_router").mkdir(parents=True)
    (root / "py_router" / "route.py").write_text(script_body, encoding="utf-8")
    return root


def test_run_kicad_routing_tools_streams_log_to_disk_on_success(tmp_path, monkeypatch):
    checkout = _fake_route_checkout(tmp_path / "krt", _FAKE_ROUTE_SUCCESS)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)

    input_pcb = tmp_path / "board.kicad_pcb"
    input_pcb.write_text("(kicad_pcb)", encoding="utf-8")
    output_pcb = tmp_path / "out" / "board.kicad_pcb"

    result = krt.run_kicad_routing_tools(input_pcb, output_pcb, timeout=30)

    assert result["exit_code"] == 0
    stdout_log = Path(result["stdout_log"])
    stderr_log = Path(result["stderr_log"])
    assert stdout_log.is_file() and stderr_log.is_file()
    assert stdout_log.name == "board.krt_route.log"
    assert "bus corridor planning: fake progress line 1" in stdout_log.read_text(encoding="utf-8")
    assert "FAKE_ROUTE_DONE" in stdout_log.read_text(encoding="utf-8")
    assert "fake warning on stderr" in stderr_log.read_text(encoding="utf-8")
    assert result["stderr_tail"].strip() == "fake warning on stderr"
    # The live-file text still flows into the same parsed `log` downstream
    # code (_diagnose_krt_routing et al.) already reads -- not a silent
    # behavior change for existing consumers.
    assert result["log"] is not None


def test_run_kicad_routing_tools_timeout_preserves_log_and_raises(tmp_path, monkeypatch):
    checkout = _fake_route_checkout(tmp_path / "krt", _FAKE_ROUTE_HANGS)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)

    input_pcb = tmp_path / "board.kicad_pcb"
    input_pcb.write_text("(kicad_pcb)", encoding="utf-8")
    output_pcb = tmp_path / "out" / "board.kicad_pcb"

    with pytest.raises(krt.KicadRoutingToolsError, match="timed out"):
        krt.run_kicad_routing_tools(input_pcb, output_pcb, timeout=1)

    # Unlike the old capture_output=True call (which could lose everything
    # buffered up to the kill), the file-backed log survives the timeout
    # and the partial output it did write is preserved on disk.
    stdout_log = output_pcb.with_name("board.krt_route.log")
    assert stdout_log.is_file()
    assert "fake route starting, about to hang" in stdout_log.read_text(encoding="utf-8")
    assert not output_pcb.is_file()  # the fake script never got to write it


# ---------------------------------------------------------------------------
# Graceful stop instead of a hard kill (ticket 09): request_graceful_stop()
# touches a file KRT's real route.py checks before starting each next net --
# confirmed for real against actual KRT + tests/fixtures/simple_led.kicad_pcb
# (2026-09-13): it printed "CHECKPOINT STOP (KICAD_STOP_FILE): ... exists;
# writing partial board", exited 0, and the output loaded cleanly via
# kicad_tools.schema.PCB.load -- a real, valid, non-corrupted partial board.
# These tests cover the mechanism with a fake stand-in `route.py` that
# checks the same env var the real one does, no real KRT install needed.
# ---------------------------------------------------------------------------

_FAKE_ROUTE_CHECKS_STOP = """\
import os, sys, time
from pathlib import Path
stop_file = os.environ.get("KICAD_STOP_FILE")
for i in range(20):
    sys.stdout.write(f"fake net {i} starting\\n")
    sys.stdout.flush()
    if stop_file and Path(stop_file).is_file():
        sys.stdout.write(f"CHECKPOINT STOP (KICAD_STOP_FILE): {stop_file} exists; writing partial board\\n")
        Path(sys.argv[2]).write_text("(kicad_pcb fake-partial)\\n", encoding="utf-8")
        sys.stdout.flush()
        sys.exit(0)
    time.sleep(0.2)
Path(sys.argv[2]).write_text("(kicad_pcb fake-full)\\n", encoding="utf-8")
sys.stdout.write("FAKE_ROUTE_DONE\\n")
"""

_FAKE_ROUTE_IGNORES_STOP_AND_HANGS = """\
import sys, time
sys.stdout.write("fake route ignoring any stop request, hanging\\n")
sys.stdout.flush()
time.sleep(30)
"""


def test_stop_file_for_and_request_graceful_stop_path_naming(tmp_path):
    output_pcb = tmp_path / "out" / "myboard.kicad_pcb"
    expected = tmp_path / "out" / "myboard.krt_stop_request"
    assert krt.stop_file_for(output_pcb) == expected

    assert not expected.exists()
    path = krt.request_graceful_stop(output_pcb, reason="because")
    assert path == expected
    assert expected.is_file()
    assert "because" in expected.read_text(encoding="utf-8")


def test_run_kicad_routing_tools_honors_pre_existing_stop_file(tmp_path, monkeypatch):
    checkout = _fake_route_checkout(tmp_path / "krt", _FAKE_ROUTE_CHECKS_STOP)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)

    input_pcb = tmp_path / "board.kicad_pcb"
    input_pcb.write_text("(kicad_pcb)", encoding="utf-8")
    output_pcb = tmp_path / "out" / "board.kicad_pcb"

    # Same scenario verified for real against actual KRT (see module
    # docstring above) -- request the stop before the run even starts.
    krt.request_graceful_stop(output_pcb, reason="test")

    result = krt.run_kicad_routing_tools(input_pcb, output_pcb, timeout=30)

    assert result["exit_code"] == 0
    assert result["graceful_stop_honored"] is True
    assert output_pcb.read_text(encoding="utf-8") == "(kicad_pcb fake-partial)\n"
    assert result["stop_file"] == str(krt.stop_file_for(output_pcb))


def test_run_kicad_routing_tools_honors_mid_run_stop_request(tmp_path, monkeypatch):
    checkout = _fake_route_checkout(tmp_path / "krt", _FAKE_ROUTE_CHECKS_STOP)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)

    input_pcb = tmp_path / "board.kicad_pcb"
    input_pcb.write_text("(kicad_pcb)", encoding="utf-8")
    output_pcb = tmp_path / "out" / "board.kicad_pcb"

    # A real concurrent request while the run is genuinely in flight (via a
    # background thread), not a shortcut of pre-touching before it starts --
    # the scenario an actual operator watching a live run would trigger.
    timer = threading.Timer(
        0.5, krt.request_graceful_stop, args=(output_pcb,), kwargs={"reason": "mid-run test"}
    )
    timer.start()
    try:
        result = krt.run_kicad_routing_tools(input_pcb, output_pcb, timeout=30)
    finally:
        timer.cancel()

    assert result["exit_code"] == 0
    assert result["graceful_stop_honored"] is True
    assert output_pcb.read_text(encoding="utf-8") == "(kicad_pcb fake-partial)\n"
    # Stopped early -- never reached the fake loop's natural completion.
    assert "FAKE_ROUTE_DONE" not in Path(result["stdout_log"]).read_text(encoding="utf-8")


def test_run_kicad_routing_tools_does_not_clear_a_leftover_stop_file(tmp_path, monkeypatch):
    # Documented, deliberate behavior (see run_kicad_routing_tools'
    # docstring): a stop file left at the same output path from an earlier
    # call is honored by a later one too, same as a freshly-requested stop
    # made just before starting -- the two are indistinguishable by design,
    # so this project relies on its own trial-copy discipline (a fresh temp
    # directory per attempt) rather than auto-clearing, which would also
    # break the "start already resigned to stop" pre-existing-file case
    # covered above.
    checkout = _fake_route_checkout(tmp_path / "krt", _FAKE_ROUTE_CHECKS_STOP)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)

    input_pcb = tmp_path / "board.kicad_pcb"
    input_pcb.write_text("(kicad_pcb)", encoding="utf-8")
    output_pcb = tmp_path / "out" / "board.kicad_pcb"

    leftover = krt.stop_file_for(output_pcb)
    leftover.parent.mkdir(parents=True, exist_ok=True)
    leftover.write_text("leftover from an earlier call at this same path\n", encoding="utf-8")

    result = krt.run_kicad_routing_tools(input_pcb, output_pcb, timeout=30)

    assert result["exit_code"] == 0
    assert result["graceful_stop_honored"] is True
    assert output_pcb.read_text(encoding="utf-8") == "(kicad_pcb fake-partial)\n"


def test_run_kicad_routing_tools_hard_kill_backstop_when_stop_request_ignored(tmp_path, monkeypatch):
    checkout = _fake_route_checkout(tmp_path / "krt", _FAKE_ROUTE_IGNORES_STOP_AND_HANGS)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)

    input_pcb = tmp_path / "board.kicad_pcb"
    input_pcb.write_text("(kicad_pcb)", encoding="utf-8")
    output_pcb = tmp_path / "out" / "board.kicad_pcb"

    krt.request_graceful_stop(output_pcb, reason="ignored on purpose")

    # The hard-kill path still fires as the backstop even though a graceful
    # stop was requested -- a script that never checks/honors the file (or
    # gets stuck before its next checkpoint) doesn't hang forever.
    with pytest.raises(krt.KicadRoutingToolsError, match="timed out"):
        krt.run_kicad_routing_tools(input_pcb, output_pcb, timeout=1)


# ---------------------------------------------------------------------------
# Real-time progress from the live log (krt-capability-suite ticket 10):
# parse_krt_progress()/krt_run_progress() give a caller deciding whether to
# request_graceful_stop() a real "which net, how far into its MST edges"
# signal instead of waiting blind -- ticket 06 found a stop request can sit
# 10+ minutes mid-way through one large multi-pad net, since KRT only
# checks its stop file between nets. Regex behavior verified against a
# REAL KRT run against tests/fixtures/krt_stress_congestion.kicad_pcb
# (2026-09-14), not just `--help`-reading: an earlier draft required the
# edge line's "(K failed)" qualifier unconditionally, which silently
# matched zero edge lines on any net before its first in-net failure (real
# KRT omits the qualifier entirely until K>0, exactly like the net-level
# line already does) -- caught by that real run and fixed before landing.
# ---------------------------------------------------------------------------


def test_parse_krt_progress_tracks_current_net_and_edge():
    # Net-level qualifier is itself conditional in real KRT output
    # ("[1/5] Routing X (id=1)" with no failures yet, "[2/5 (1 failed)]
    # Routing Y (id=2)" once one has) -- both forms covered here.
    log_text = "\n".join([
        "[1/5] Routing SIG1 (id=1)",
        "[1/8] Routing MST edge: pad 1 -> pad 2 (length=5.08mm) target=(1,1)",
        "[2/8] Routing MST edge: pad 2 -> pad 3 (length=5.08mm) target=(1,1)",
        "[2/5 (1 failed)] Routing GND (id=2)",
        "[1/248] Routing MST edge: pad 5 -> pad 6 (length=1.00mm) target=(1,1)",
        "[134/248] (53 failed) Routing MST edge: pad 4 -> pad 14 (length=13.66mm) target=(121.76, 96.82)",
    ])

    progress = krt.parse_krt_progress(log_text)

    assert progress["net"] == {"net_index": 2, "net_total": 5, "net_name": "GND"}
    assert progress["edge"] == {"edge_index": 134, "edge_total": 248, "edges_failed": 53}


def test_parse_krt_progress_edge_resets_when_a_new_net_starts():
    # A leftover edge count from the PREVIOUS net must not survive into a
    # net whose own log hasn't produced any edge lines yet (e.g. a
    # single-pad net, or one just started).
    log_text = "\n".join([
        "[1/2] Routing SIG1 (id=1)",
        "[1/8] Routing MST edge: pad 1 -> pad 2 (length=5.08mm) target=(1,1)",
        "[2/2] Routing SIG2 (id=2)",
    ])

    progress = krt.parse_krt_progress(log_text)

    assert progress["net"] == {"net_index": 2, "net_total": 2, "net_name": "SIG2"}
    assert progress["edge"] is None


def test_parse_krt_progress_on_real_captured_krt_log():
    # Real log captured from an actual KRT run against
    # tests/fixtures/krt_stress_congestion.kicad_pcb (2026-09-14), not a
    # hand-typed approximation -- exercises the exact conditional
    # "(K failed)" formatting real KRT emits on both net and edge lines.
    log_text = "\n".join([
        "[1/7] Routing SIG1 (id=1)",
        "[2/7 (1 failed)] Routing SIG2 (id=2)",
        "[3/7 (2 failed)] Routing SIG3 (id=3)",
        "[4/7 (3 failed)] Routing SIG4 (id=4)",
        "[5/7 (4 failed)] Routing SIG5 (id=5)",
        "[6/7 (5 failed)] Routing GND (id=7)",
        "[7/7 (5 failed)] Routing VDD (id=6)",
        "[1/8] Routing MST edge: pad 1 -> pad 2 (length=5.08mm) target=(145.50, 114.20)",
        "[2/8] Routing MST edge: pad 2 -> pad 3 (length=5.08mm) target=(145.50, 119.28)",
        "[1/1] Routing SIG5 (id=5)",
    ])

    progress = krt.parse_krt_progress(log_text)

    # Final line in the real log is a single-net retry pass for the one
    # net that failed on the first full pass -- it logged no MST-edge
    # lines before this captured excerpt ends, same as a real KRT retry
    # on a small remaining net can look.
    assert progress["net"] == {"net_index": 1, "net_total": 1, "net_name": "SIG5"}
    assert progress["edge"] is None


def test_krt_run_progress_reports_not_found_before_a_run_starts(tmp_path):
    output_pcb = tmp_path / "out" / "board.kicad_pcb"

    progress = krt.krt_run_progress(output_pcb)

    assert progress == {"log_found": False, "net": None, "edge": None}


def test_krt_run_progress_reads_the_live_log_file(tmp_path):
    output_pcb = tmp_path / "out" / "board.kicad_pcb"
    log_path = krt.stdout_log_for(output_pcb)
    log_path.parent.mkdir(parents=True)
    log_path.write_text(
        "[1/3] Routing A (id=1)\n[1/5] Routing MST edge: pad 1 -> pad 2 (length=1mm) target=(0,0)\n",
        encoding="utf-8",
    )

    progress = krt.krt_run_progress(output_pcb)

    assert progress["log_found"] is True
    assert progress["net"] == {"net_index": 1, "net_total": 3, "net_name": "A"}
    assert progress["edge"] == {"edge_index": 1, "edge_total": 5, "edges_failed": 0}


def test_krt_run_progress_matches_stop_file_naming_convention(tmp_path):
    # stdout_log_for()/stop_file_for() must derive from the same output_pcb
    # argument alone, same convention, so an external caller (another
    # session, a person) can find both without new plumbing.
    output_pcb = tmp_path / "out" / "board.kicad_pcb"

    assert krt.stdout_log_for(output_pcb).name == "board.krt_route.log"
    assert krt.stop_file_for(output_pcb).name == "board.krt_stop_request"
    assert krt.stdout_log_for(output_pcb).parent == krt.stop_file_for(output_pcb).parent


def test_run_kicad_routing_tools_progress_is_readable_mid_run_via_the_same_log_path(tmp_path, monkeypatch):
    # krt_run_progress() must point at the SAME file run_kicad_routing_tools()
    # actually streams to -- not a parallel/duplicate path convention.
    checkout = _fake_route_checkout(tmp_path / "krt", _FAKE_ROUTE_SUCCESS)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)

    input_pcb = tmp_path / "board.kicad_pcb"
    input_pcb.write_text("(kicad_pcb)", encoding="utf-8")
    output_pcb = tmp_path / "out" / "board.kicad_pcb"

    result = krt.run_kicad_routing_tools(input_pcb, output_pcb, timeout=30)

    assert Path(result["stdout_log"]) == krt.stdout_log_for(output_pcb)
    # The fake script's output has no real "Routing ..." lines, so no
    # progress is extracted -- confirms this doesn't crash/misparse
    # arbitrary log text, just reports nothing found.
    progress = krt.krt_run_progress(output_pcb)
    assert progress["log_found"] is True
    assert progress["net"] is None
    assert progress["edge"] is None


# ---------------------------------------------------------------------------
# Toolset integration (no KRT present)
# ---------------------------------------------------------------------------


def _tools() -> dict:
    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_no_new_tools_only_a_backend_parameter():
    """P4-05's principle: KRT is a `backend=` choice on the shared
    propose/confirm pair, not a parallel `propose_route_with_kicad_routing_tools`
    tool family. `diagnose_placement_congestion` (ticket 11) is a genuinely
    separate diagnostic capability, not a routing-backend variant, so its
    addition doesn't violate what this test guards."""
    mcp = MCPServer("test")
    names = routing_toolset.build(mcp)
    assert set(names) == {
        "add_traces",
        "get_unrouted_nets",
        "propose_route_with_freerouting",
        "confirm_route",
        "propose_route_with_retry",
        "diagnose_placement_congestion",
    }


def test_propose_with_unknown_backend_or_escalation_is_incomplete(tmp_path):
    pcb = _golden_copy(tmp_path)
    tools = _tools()
    assert tools["propose_route_with_retry"](str(pcb), backend="bogus")["status"] == "incomplete"
    assert tools["propose_route_with_retry"](str(pcb), backend="kicad_routing_tools", escalation="yolo")["status"] == "incomplete"


def test_propose_with_krt_missing_is_actionable_incomplete_not_a_crash(tmp_path, monkeypatch):
    monkeypatch.delenv(krt.ENV_PATH, raising=False)
    monkeypatch.setattr(krt, "_PLUGIN_GLOBS", {})
    pcb = _golden_copy(tmp_path)
    result = _tools()["propose_route_with_retry"](str(pcb), backend="kicad_routing_tools")
    assert result["status"] == "incomplete"
    assert "KICAD_ROUTING_TOOLS_PATH" in result["reason"]
    assert result["router_availability"]["status"] == "incomplete"
    # And the board was not touched.
    from kicad_tools.schema import PCB

    assert PCB.load(pcb).routing_status()["segments"] == 0


def test_plan_next_krt_attempt_uses_finer_grid_then_more_iterations_then_stops():
    categories = [{"category": "unrouted_net", "routing_adjustable": True}]
    log = {"failed_nets": ["VCC", "X"], "boxed_in_nets": ["VCC"]}
    first = routing_toolset._plan_next_krt_attempt(categories, log, [{"label": "baseline", "plan": {}}])
    assert first["label"] == "finer_grid" and first["grid_step_mm"] == 0.05
    second = routing_toolset._plan_next_krt_attempt(
        categories, log, [{"label": "baseline", "plan": {}}, {"label": "finer_grid", "plan": first}]
    )
    assert second["label"] == "more_iterations" and second["grid_step_mm"] == 0.05  # carries the finer grid forward
    third = routing_toolset._plan_next_krt_attempt(
        categories, log, [{"label": "baseline", "plan": {}}, {"label": "finer_grid", "plan": first}, {"label": "more_iterations", "plan": second}]
    )
    assert third is None
    # A boxed-in-only failure never gets a "more iterations" retry -- KRT says it provably can't help.
    boxed_only = {"failed_nets": ["VCC"], "boxed_in_nets": ["VCC"]}
    assert routing_toolset._plan_next_krt_attempt(categories, boxed_only, [{"label": "baseline", "plan": {}}, {"label": "finer_grid", "plan": first}]) is None
    # Nothing to retry for when only design-rule conflicts remain.
    assert routing_toolset._plan_next_krt_attempt([{"category": "trace_narrower_than_board_minimum", "routing_adjustable": False}], log, []) is None


def test_diagnose_krt_routing_adds_relaxed_rules_category_and_self_report():
    drc = {"violations": [{"type": "clearance", "description": "x"}]}
    log = {"failed_nets": [], "boxed_in_nets": [], "min_clearance_used_mm": 0.127, "narrowed_min_delivered_mm": {"track_width": 0.127}}
    floors = {"clearance": 0.2, "track_width": 0.2}
    diagnosis = routing_toolset._diagnose_krt_routing(drc, {"unrouted_pads": []}, {}, log, floors, "fab")
    by_cat = {c["category"]: c for c in diagnosis["categories"]}
    assert by_cat["router_relaxed_board_rules"]["count"] == 2
    assert by_cat["router_relaxed_board_rules"]["routing_adjustable"] is False  # fab = opted in
    assert by_cat["clearance_violation"]["routing_adjustable"] is False
    assert diagnosis["kicad_routing_tools_self_report"]["min_clearance_used_mm"] == 0.127


# ---------------------------------------------------------------------------
# Real end-to-end, gated on a real install.
# ---------------------------------------------------------------------------

@pytest.mark.needs_krt  # skip with the probe's reason, or fail in the gate run (build ticket 53)
class TestRealKicadRoutingTools:
    """Runs the real KRT subprocess on the real golden-path board through the
    finished tool: propose (trial copies only) -> confirm (delta applied via
    kicad_tools) -> real kicad-cli DRC against the board's ORIGINAL rules.

    Measured while building P4-05 (KRT v0.22.0, escalation='board' with the
    board's own rules pinned as a hard floor): baseline 0 violations / 3
    unconnected / 0 shorts, finer-grid retry 0 / 2 / 0, 113 traces + 2
    vias. The strict assertions are the ones that matter (0 shorts, floor
    respected, board untouched until confirm, confirm == trial); the
    unconnected count is reported, not pinned, because the VQFN's 0.4 mm
    pitch genuinely cannot take the board's 0.2/0.2 rules (docs/ARCHITECTURE.md
    meta-finding) and how many pads KRT reaches within the rules wobbles.
    """

    def test_propose_confirm_under_board_policy(self, tmp_path):
        pcb = _golden_copy(tmp_path)
        tools = _tools()

        result = tools["propose_route_with_retry"](str(pcb), max_attempts=3, backend="kicad_routing_tools", escalation="board")
        assert result["status"] == "pending_confirmation", result
        assert result["backend"] == "kicad_routing_tools"
        assert result["trace_count"] > 0
        assert result["clearance_policy"]["hard_floor_pinned"] is True
        assert result["clearance_policy"]["clearance_mm_passed"] == 0.2

        best_drc = result["best_attempt_drc_summary"]
        assert best_drc["short_count"] == 0, result["attempts"]
        # The whole point of pinning the floor: no rule-breaking copper.
        assert best_drc["violation_count"] == 0, result["attempts"]
        for attempt in result["attempts"]:
            assert attempt.get("error") is None, attempt
            assert attempt["router_log"]["min_clearance_used_mm"] >= 0.2 - 1e-9
            assert attempt["delta"]["removed_segment_count"] == 0
            # KRT rewrote the trial's sibling project even with --no-fix-drc-settings;
            # it was discarded, and the fact is disclosed rather than hidden.
            assert attempt["router_rewrote_project_file"] is True
        print(
            f"KRT board-policy real result: {best_drc['violation_count']} violations / "
            f"{best_drc['unconnected_count']} unconnected / {best_drc['short_count']} shorts, "
            f"{result['trace_count']} traces + {result['via_count']} vias, best attempt {result['best_attempt']} of {result['attempts_tried']}"
        )

        from kicad_tools.schema import PCB

        untouched = PCB.load(pcb).routing_status()
        assert untouched["segments"] == 0 and untouched["vias"] == 0

        # KRT narrows this board's 0.25 mm tracks to 0.2 mm and its 0.6 mm vias to 0.5 mm
        # (exit 3), on v0.22.0 and v0.23.1 alike (build ticket 33). Since below-size
        # delivery is a failure (2508e6e) the proposal must say so, and a plain confirm
        # must refuse without touching the board; with the snapshot current there is no
        # version flag beside it.
        assert result["router_flags"] == ["strict_sizes_violated"], result["router_flags"]
        assert result["requires_acknowledgement"] is True
        assert result["delivered_below_requested"], result
        refused = tools["confirm_route"](str(pcb), result["revision_token"])
        assert refused["status"] == "refused", refused
        assert refused["router_flags"] == ["strict_sizes_violated"]
        unchanged = PCB.load(pcb).routing_status()
        assert unchanged["segments"] == 0 and unchanged["vias"] == 0

        confirmed = tools["confirm_route"](str(pcb), result["revision_token"], acknowledge_router_flags=True)
        assert confirmed["status"] == "confirmed", confirmed
        assert confirmed["traces_applied"] == result["trace_count"]
        assert confirmed["vias_applied"] == result["via_count"]
        assert confirmed["drc_after"]["unconnected_count"] == best_drc["unconnected_count"]
        assert confirmed["drc_after"]["violation_count"] == 0
        after = PCB.load(pcb).routing_status()
        assert after["segments"] == result["trace_count"] and after["vias"] == result["via_count"]
        # The original project file is still the original (never KRT's rewrite).
        original_rules = json.loads(GOLDEN_PCB.with_suffix(".kicad_pro").read_text(encoding="utf-8"))["board"]["design_settings"]["rules"]
        applied_rules = json.loads(pcb.with_suffix(".kicad_pro").read_text(encoding="utf-8"))["board"]["design_settings"]["rules"]
        assert applied_rules == original_rules

    def test_fab_policy_is_graded_for_manufacturability(self, tmp_path):
        pcb = _golden_copy(tmp_path)
        tools = _tools()
        result = tools["propose_route_with_retry"](str(pcb), max_attempts=1, backend="kicad_routing_tools", escalation="fab")
        assert result["status"] == "pending_confirmation", result
        assert result["clearance_policy"]["hard_floor_pinned"] is False
        assert result["best_attempt_drc_summary"]["short_count"] == 0
        manufacturability = result["manufacturability"]
        assert manufacturability["status"] in ("complete", "partial"), manufacturability
        # KRT's fab policy relaxes the board's floors; the delivered overlay must show it.
        assert manufacturability["delivered_overlay_mm"]["clearance_mm"] < 0.2
        assert manufacturability["meets_fab_floor"] is not True
        assert "manufacturability" in result["warning"]
        categories = {c["category"] for c in result["best_attempt_diagnosis"]["categories"]}
        assert "router_relaxed_board_rules" in categories
        print(
            f"KRT fab-policy real result: {result['best_attempt_drc_summary']['violation_count']} violations / "
            f"{result['best_attempt_drc_summary']['unconnected_count']} unconnected against the ORIGINAL rules; "
            f"meets_fab_floor={manufacturability['meets_fab_floor']}"
        )
