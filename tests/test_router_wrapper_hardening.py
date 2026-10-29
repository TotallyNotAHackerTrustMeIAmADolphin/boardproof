"""Ticket 03 review round 1: hardening of the KiCadRoutingTools wrapper.

Findings 1 (flag smuggling via values), 2 (pour detection), 4 (restore on
every exit path) plus the low items and mutant gaps. Companion to
test_router_cli_snapshot.py.
"""

from __future__ import annotations

import json
import math
import subprocess
import time
from pathlib import Path

import pytest

from kicad_mcp.routers import kicad_routing_tools as krt

# ---- finding 1: managed flags cannot be smuggled through VALUES ----------


@pytest.mark.parametrize(
    "value",
    [["1", "--overwrite", "--nets", "X"], "--overwrite", ["--nets"], "-n", ["1", "-O"], "--escalation=fab", ""],
)
def test_advanced_flag_values_cannot_smuggle_flags(value):
    with pytest.raises(ValueError, match="value"):
        krt.build_route_args("a", "b", advanced_flags={"--component": ["U1", *(value if isinstance(value, list) else [value])]})
    if not isinstance(value, list):
        with pytest.raises(ValueError, match="value"):
            krt.build_route_args("a", "b", advanced_flags={"--via-cost": value})


def test_list_value_for_a_single_value_flag_that_hides_a_flag_is_rejected():
    with pytest.raises(ValueError):
        krt.build_route_args("a", "b", advanced_flags={"--via-cost": ["1", "--overwrite", "--nets", "X"]})


def test_plain_negative_number_is_a_legitimate_value():
    args = krt.build_route_args("a", "b", advanced_flags={"--via-cost": -1})
    assert args[args.index("--via-cost") + 1] == "-1"


@pytest.mark.parametrize(
    "flags",
    [
        {"--via-cost": True},            # needs a value
        {"--bus": 5},                    # bare switch takes none
        {"--via-cost": ["1", "2"]},      # exactly one
        {"--component": []},             # at least one
        {"--via-cost": {"a": 1}},        # type
        {"--component": [["1"]]},        # nested
        {"--via-cost": float("nan")},    # not finite
        {"--via-cost": float("inf")},
        {"--component": [True]},         # bool inside a list
    ],
)
def test_advanced_flag_type_and_arity_are_validated(flags):
    with pytest.raises(ValueError):
        krt.build_route_args("a", "b", advanced_flags=flags)


def test_advanced_flag_multi_value_and_false_switch_ok():
    args = krt.build_route_args("a", "b", advanced_flags={"--component": ["U1", "U2"], "--bus": False})
    assert args[args.index("--component") + 1:args.index("--component") + 3] == ["U1", "U2"]
    assert "--bus" not in args


def test_snapshot_records_argument_arity():
    arity = krt.load_cli_snapshot()["modules"]["route.py"]["arity"]
    assert arity["--via-cost"] == "1" and arity["--bus"] == "0" and arity["--component"] == "+"


def test_parse_usage_arity_on_synthetic_usage():
    text = ("noise\nusage: route.py [-h] [--a] [--b B] [--c C [C ...]] [--d [D ...]]\n"
            "                [--e {x,y}] [--f |\n                --g]\n\nBody")
    assert krt.parse_usage_arity(text) == {"--a": "0", "--b": "1", "--c": "+", "--d": "*", "--e": "1",
                                           "--f": "0", "--g": "0"}


# ---- finding 2: pour detection --------------------------------------------


def _pcb(tmp_path, zones: str, copper=('"F.Cu"', '"B.Cu"')) -> Path:
    layers = " ".join(f"({i} {name} signal)" for i, name in enumerate(copper))
    p = tmp_path / "z.kicad_pcb"
    p.write_text(f'(kicad_pcb (version 20240108)\n (layers {layers} (44 "Edge.Cuts" user))\n{zones}\n)\n', encoding="utf-8")
    return p


def test_keepout_with_a_real_net_is_not_a_pour(tmp_path):
    pcb = _pcb(tmp_path, '(zone (net 3) (net_name "GND") (layer "F.Cu") (keepout (tracks not_allowed)))')
    assert krt.board_pour_info(pcb)["has_pour"] is False


def test_net_zero_zone_is_not_a_pour(tmp_path):
    pcb = _pcb(tmp_path, '(zone (net 0) (net_name "") (layer "F.Cu") (fill yes))')
    assert krt.board_pour_info(pcb)["has_pour"] is False


def test_footprint_level_zone_is_not_a_pour(tmp_path):
    pcb = _pcb(tmp_path, '(footprint "X" (zone (net 1) (layer "F.Cu") (fill yes)))')
    assert krt.board_pour_info(pcb)["has_pour"] is False


def test_named_net_zone_is_a_pour_and_layers_form_lists_all(tmp_path):
    pcb = _pcb(tmp_path, '(zone (net "GND") (layers "F.Cu" "B.Cu") (fill yes))')
    info = krt.board_pour_info(pcb)
    assert info["has_pour"] and info["pour_layers"] == ["F.Cu", "B.Cu"]


@pytest.mark.parametrize("token,expected", [("F&B.Cu", ["F.Cu", "B.Cu"]), ("*.Cu", ["F.Cu", "In1.Cu", "B.Cu"])])
def test_wildcard_layer_tokens_expand_to_the_boards_copper(tmp_path, token, expected):
    pcb = _pcb(tmp_path, f'(zone (net 1) (layer "{token}") (fill yes))', copper=('"F.Cu"', '"In1.Cu"', '"B.Cu"'))
    assert krt.board_pour_info(pcb)["pour_layers"] == expected


def test_zone_on_a_layer_that_is_not_copper_of_this_board_is_not_a_pour(tmp_path):
    pcb = _pcb(tmp_path, '(zone (net 1) (layer "In5.Cu") (fill yes))')
    assert krt.board_pour_info(pcb)["has_pour"] is False


def test_unquoted_layer_tokens_are_read(tmp_path):
    pcb = _pcb(tmp_path, "(zone (net 1) (layers F.Cu B.Cu) (fill yes))", copper=("F.Cu", "B.Cu"))
    assert krt.board_pour_info(pcb)["pour_layers"] == ["F.Cu", "B.Cu"]


def test_a_paren_in_a_quoted_name_does_not_unbalance_the_scan(tmp_path):
    pcb = _pcb(tmp_path, '(zone (net 1) (net_name "A)(B") (layer "B.Cu") (fill yes))')
    assert krt.board_pour_info(pcb)["pour_layers"] == ["B.Cu"]


# ---- mutant gaps: emitted-flag coverage ------------------------------------


def test_emittable_flags_cover_every_flag_the_wrapper_adds():
    flags = krt.emittable_flags()
    assert {"--layers", "--layer-costs", "--strict-sizes", "--no-fix-drc-settings", "--json-out", "--escalation",
            "--clearance", "--fab-overrides", "--grid-step", "--max-iterations", "--nets"} <= flags


def test_snapshot_check_names_layer_costs_when_dropped():
    broken = json.loads(json.dumps(krt.load_cli_snapshot()))
    broken["modules"]["route.py"]["flags"].remove("--layer-costs")
    assert [m["flag"] for m in krt.check_emitted_flags(broken)] == ["--layer-costs"]


def test_snapshot_file_is_present_and_parses_without_a_router():
    snap = krt.load_cli_snapshot()
    assert snap["snapshot_schema"] == 1 and snap["router_version"]
    assert set(krt.ROUTER_SCRIPTS) <= set(snap["modules"])
    assert "--strict-sizes" in snap["modules"]["route.py"]["flags"]


# ---- subprocess-level behaviour (fake router scripts) ----------------------

_EXIT3 = """\
import sys
from pathlib import Path
Path(sys.argv[2]).write_text("(kicad_pcb routed)\\n", encoding="utf-8")
sys.exit(3)
"""

_NARROWED_ONLY = """\
import sys, json
from pathlib import Path
Path(sys.argv[2]).write_text("(kicad_pcb routed)\\n", encoding="utf-8")
out = sys.argv[sys.argv.index("--json-out") + 1]
Path(out).write_text(json.dumps({"design_rules": {"narrowed": [
    {"net_name": "SDA", "kind": "track_width", "requested": 0.3, "delivered": 0.2}]}}), encoding="utf-8")
"""

_CLOBBER_THEN_HANG = """\
import sys, time
from pathlib import Path
Path(sys.argv[2]).parent.mkdir(parents=True, exist_ok=True)
Path(sys.argv[2]).with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
Path(sys.argv[1]).with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
time.sleep(60)
"""

_CLOBBER_NO_OUTPUT = """\
import sys
from pathlib import Path
Path(sys.argv[2]).parent.mkdir(parents=True, exist_ok=True)
Path(sys.argv[2]).with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
Path(sys.argv[1]).with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
"""

_CREATE_PROJECT_ONLY = """\
import sys
from pathlib import Path
Path(sys.argv[2]).write_text("(kicad_pcb routed)\\n", encoding="utf-8")
Path(sys.argv[2]).with_suffix(".kicad_pro").write_text('{"router_made": true}', encoding="utf-8")
"""

_NOOP_ROUTED = """\
import sys
from pathlib import Path
Path(sys.argv[2]).write_text("(kicad_pcb routed)\\n", encoding="utf-8")
"""


def _setup(tmp_path, monkeypatch, body: str, *, with_project=True):
    checkout = tmp_path / "krt"
    (checkout / "py_router").mkdir(parents=True)
    (checkout / "py_router" / "route.py").write_text(body, encoding="utf-8")
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    src = tmp_path / "in" / "b.kicad_pcb"
    src.parent.mkdir()
    src.write_text("(kicad_pcb)", encoding="utf-8")
    original = '{"meta": {"original": true}}'
    if with_project:
        src.with_suffix(".kicad_pro").write_text(original, encoding="utf-8")
    return src, tmp_path / "out" / "b.kicad_pcb", original


def test_exit_code_3_is_reported_as_strict_sizes_violation_not_raised(tmp_path, monkeypatch):
    src, out, _ = _setup(tmp_path, monkeypatch, _EXIT3)
    result = krt.run_kicad_routing_tools(src, out, timeout=30)
    assert result["exit_code"] == 3
    assert result["strict_sizes_violated"] is True


def test_delivered_below_requested_alone_sets_strict_sizes_violated(tmp_path, monkeypatch):
    src, out, _ = _setup(tmp_path, monkeypatch, _NARROWED_ONLY)
    result = krt.run_kicad_routing_tools(src, out, timeout=30)
    assert result["exit_code"] == 0
    assert result["strict_sizes_violated"] is True
    assert result["delivered_below_requested"][0]["net_name"] == "SDA"


def test_timeout_raises_but_project_is_restored_and_disclosed(tmp_path, monkeypatch):
    src, out, original = _setup(tmp_path, monkeypatch, _CLOBBER_THEN_HANG)
    with pytest.raises(krt.KicadRoutingToolsError) as info:
        krt.run_kicad_routing_tools(src, out, timeout=4)
    assert out.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original
    assert src.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original
    assert info.value.router_overwrote_project is True
    assert info.value.project_restored is True


def test_missing_output_raises_but_project_is_restored_and_disclosed(tmp_path, monkeypatch):
    src, out, original = _setup(tmp_path, monkeypatch, _CLOBBER_NO_OUTPUT)
    with pytest.raises(krt.KicadRoutingToolsError) as info:
        krt.run_kicad_routing_tools(src, out, timeout=30)
    assert out.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original
    assert src.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original
    assert info.value.router_overwrote_project is True


def test_keyboard_interrupt_terminates_the_child_and_restores_the_project(tmp_path, monkeypatch):
    src, out, original = _setup(tmp_path, monkeypatch, _CLOBBER_THEN_HANG)
    started: list = []
    real_popen = subprocess.Popen

    class InterruptingPopen(real_popen):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            started.append(self)
            self._first = True

        def wait(self, timeout=None):
            if self._first:
                self._first = False
                time.sleep(2.5)  # let the script clobber the project
                raise KeyboardInterrupt
            return super().wait(timeout)

    monkeypatch.setattr(krt.subprocess, "Popen", InterruptingPopen)
    with pytest.raises(KeyboardInterrupt):
        krt.run_kicad_routing_tools(src, out, timeout=60)
    assert started and started[0].poll() is not None, "child must be terminated"
    assert out.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original
    assert src.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original


def test_router_created_project_with_no_original_is_removed_and_disclosed(tmp_path, monkeypatch):
    src, out, _ = _setup(tmp_path, monkeypatch, _CREATE_PROJECT_ONLY, with_project=False)
    result = krt.run_kicad_routing_tools(src, out, timeout=30)
    assert result["router_overwrote_project"] is True
    assert result["project_removed_router_created"] is True
    assert not out.with_suffix(".kicad_pro").exists()


def test_identical_input_project_is_not_rewritten(tmp_path, monkeypatch):
    src, out, _ = _setup(tmp_path, monkeypatch, _NOOP_ROUTED)
    project = src.with_suffix(".kicad_pro")
    before = project.stat().st_mtime_ns
    time.sleep(0.05)
    result = krt.run_kicad_routing_tools(src, out, timeout=30)
    assert project.stat().st_mtime_ns == before
    assert result["router_overwrote_project"] is False


@pytest.mark.parametrize("cost", [0.5, 1001.0, float("nan"), float("inf"), -1.0])
def test_pour_layer_cost_is_validated(cost):
    assert not (math.isfinite(cost) and 1.0 <= cost <= 1000.0)
    with pytest.raises(ValueError, match="pour_layer_cost"):
        krt.build_route_args("a", "b", pour_layer_cost=cost)


def test_missing_snapshot_fails_before_the_router_runs(tmp_path, monkeypatch):
    src, out, _ = _setup(tmp_path, monkeypatch, _NOOP_ROUTED)
    monkeypatch.setattr(krt, "SNAPSHOT_PATH", tmp_path / "no_such_snapshot.json")
    with pytest.raises(krt.KicadRoutingToolsError, match="snapshot"):
        krt.run_kicad_routing_tools(src, out, timeout=30)
    assert not out.exists(), "router must not have run"
