"""Ticket 03 (pivot-ladder-build): router wrapper flags and the CLI-surface snapshot.

A router upgrade must fail a TEST instead of a RUN. The snapshot
(`src/kicad_mcp/routers/krt_cli_snapshot.json`) is captured from the real
installed router's `route.py --capabilities`; the checks below are one-way:
every flag the wrapper can emit must exist in the pinned router's surface.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from kicad_mcp.routers import kicad_routing_tools as krt

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_PCB = FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"
POUR_PCB = FIXTURES / "zone_obstacle" / "zone_obstacle.kicad_pcb"

_KEEPOUT_ONLY = """(kicad_pcb (version 20240108)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (zone (net 0) (net_name "") (layer "F.Cu") (keepout (tracks not_allowed) (vias not_allowed)))
  (footprint "X" (zone (net 1) (layers "F.Cu")))
)
"""


# ---------------------------------------------------------------------------
# Pour detection (raw S-expression, never a kicad_tools convenience property)
# ---------------------------------------------------------------------------


def test_pour_detected_on_a_board_with_a_real_zone():
    info = krt.board_pour_info(POUR_PCB)
    assert info["has_pour"] is True
    assert info["pour_layers"] == ["F.Cu"]
    assert info["copper_layers"] == ["F.Cu", "B.Cu"]


def test_no_pour_on_a_board_without_zones():
    assert krt.board_pour_info(GOLDEN_PCB)["has_pour"] is False


def test_keepout_and_footprint_level_zones_are_not_pours(tmp_path):
    pcb = tmp_path / "k.kicad_pcb"
    pcb.write_text(_KEEPOUT_ONLY, encoding="utf-8")
    assert krt.board_pour_info(pcb)["has_pour"] is False


# ---------------------------------------------------------------------------
# Wrapper flags
# ---------------------------------------------------------------------------


def test_layer_costs_present_with_a_pour_and_absent_without():
    with_pour = krt.build_route_args("a", "b", pour_info=krt.board_pour_info(POUR_PCB))
    assert "--layer-costs" in with_pour
    assert with_pour[with_pour.index("--layers") + 1:with_pour.index("--layers") + 3] == ["F.Cu", "B.Cu"]
    # pour layer is costlier than the signal layer; order matches --layers
    costs = with_pour[with_pour.index("--layer-costs") + 1:with_pour.index("--layer-costs") + 3]
    assert [float(c) for c in costs] == [4.0, 1.0]

    without = krt.build_route_args("a", "b", pour_info=krt.board_pour_info(GOLDEN_PCB))
    assert "--layer-costs" not in without and "--layers" not in without
    assert "--layer-costs" not in krt.build_route_args("a", "b")


def test_strict_sizes_is_always_emitted():
    assert "--strict-sizes" in krt.build_route_args("a", "b")
    assert "--strict-sizes" in krt.build_route_args("a", "b", escalation="fab", clearance_mm=0.2)


def test_advanced_flag_is_emitted_and_logged():
    args = krt.build_route_args("a", "b", advanced_flags={"--via-cost": 120, "--bus": True})
    assert args[args.index("--via-cost") + 1] == "120"
    assert "--bus" in args
    logged = krt.logged_parameters(advanced_flags={"--via-cost": 120, "--bus": True})
    assert logged["advanced_flags"] == {"--via-cost": 120, "--bus": True}
    assert logged["strict_sizes"] is True


def test_advanced_flag_unknown_to_the_pinned_router_is_rejected_naming_it():
    with pytest.raises(ValueError, match="--no-such-flag"):
        krt.build_route_args("a", "b", advanced_flags={"--no-such-flag": 1})


def test_wrapper_managed_flags_cannot_be_smuggled_in_as_advanced():
    for flag in ("--overwrite", "--escalation", "--nets", "--layer-costs"):
        with pytest.raises(ValueError, match=flag):
            krt.build_route_args("a", "b", advanced_flags={flag: 1})


def test_delivered_below_requested_is_reported_not_hidden():
    log = krt.parse_route_log(
        "",
        json.dumps({"design_rules": {"count": 1, "narrowed": [
            {"net": 3, "net_name": "SDA", "kind": "track_width", "requested": 0.3, "delivered": 0.2,
             "site": "x", "count": 1}]}}),
    )
    assert log["delivered_below_requested"] == [
        {"net_name": "SDA", "kind": "track_width", "requested_mm": 0.3, "delivered_mm": 0.2,
         "site": "x"}  # the ledger site is kept since build ticket 37
    ]
    assert krt.parse_route_log("", json.dumps({}))["delivered_below_requested"] == []


# ---------------------------------------------------------------------------
# Snapshot: version-keyed, one-way flag check, names the flag
# ---------------------------------------------------------------------------


def test_every_flag_the_wrapper_can_emit_is_in_the_pinned_snapshot():
    snapshot = krt.load_cli_snapshot()
    assert snapshot["router_version"], "snapshot must record the router version it was taken from"
    assert krt.check_emitted_flags(snapshot) == []


def test_snapshot_check_fails_naming_the_flag_when_the_router_drops_it():
    snapshot = krt.load_cli_snapshot()
    broken = json.loads(json.dumps(snapshot))
    broken["modules"]["route.py"]["flags"].remove("--strict-sizes")
    missing = krt.check_emitted_flags(broken)
    assert [m["flag"] for m in missing] == ["--strict-sizes"]
    assert missing[0]["module"] == "route.py"


def test_version_mismatch_is_reported_not_ignored():
    snapshot = {"router_version": "0.22.0"}
    ok = krt.compare_router_version(snapshot, "0.22.0")
    assert ok["matches"] is True
    bad = krt.compare_router_version(snapshot, "0.23.1")
    assert bad["matches"] is False
    assert "0.23.1" in bad["message"] and "0.22.0" in bad["message"]
    unknown = krt.compare_router_version(snapshot, None)
    assert unknown["matches"] is False and "unknown" in unknown["message"].lower()


def test_installed_router_still_matches_the_snapshot(krt_checkout):
    checkout = krt_checkout  # absent: skip with the reason, or fail in the gate run (build ticket 53)
    live = krt.capture_cli_snapshot(checkout, Path(sys.executable))
    pinned = krt.load_cli_snapshot()
    version = krt.compare_router_version(pinned, live["router_version"])
    assert version["matches"], version["message"]
    for module, entry in pinned["modules"].items():
        gone = sorted(set(entry["flags"]) - set(live["modules"].get(module, {}).get("flags", [])))
        assert not gone, f"{module} no longer offers flag(s) the snapshot pins: {gone}"
    pinned_arity = pinned["modules"]["route.py"]["arity"]
    live_arity = live["modules"]["route.py"]["arity"]
    changed = {f: (a, live_arity.get(f)) for f, a in pinned_arity.items() if live_arity.get(f) != a}
    assert not changed, f"route.py argument arity changed (pinned, live): {changed}"


# ---------------------------------------------------------------------------
# Original project file restored after EVERY router script the wrapper runs
# ---------------------------------------------------------------------------

_SCRIPT_CLOBBERS_PROJECT = """\
import sys
from pathlib import Path
out = Path(sys.argv[2])
out.write_text("(kicad_pcb routed)\\n", encoding="utf-8")
out.with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
Path(sys.argv[1]).with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
"""


def _checkout_with(root: Path, *scripts: str) -> Path:
    (root / "py_router").mkdir(parents=True)
    for name in scripts:
        (root / "py_router" / name).write_text(_SCRIPT_CLOBBERS_PROJECT, encoding="utf-8")
    return root


@pytest.mark.parametrize("script", ["route.py", "route_planes.py", "repair_planes.py"])
def test_original_project_is_restored_after_every_router_script(tmp_path, monkeypatch, script):
    checkout = _checkout_with(tmp_path / "krt", "route.py", script)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    src = tmp_path / "in" / "b.kicad_pcb"
    src.parent.mkdir()
    src.write_text("(kicad_pcb)", encoding="utf-8")
    original = '{"meta": {"original": true}}'
    src.with_suffix(".kicad_pro").write_text(original, encoding="utf-8")
    out = tmp_path / "out" / "b.kicad_pcb"

    if script == "route.py":
        result = krt.run_kicad_routing_tools(src, out, timeout=30)
    else:
        result = krt.run_router_script(script, [str(src), str(out)], output_pcb=out, original_pcb=src, timeout=30)

    assert out.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original
    assert src.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original
    assert result["project_restored"] is True
    assert result["router_overwrote_project"] is True  # disclosed, then undone


def test_run_router_script_refuses_scripts_outside_the_pinned_set(tmp_path):
    with pytest.raises(ValueError, match="evil.py"):
        krt.run_router_script("evil.py", [], output_pcb=tmp_path / "o.kicad_pcb", original_pcb=tmp_path / "i.kicad_pcb")


def test_run_result_carries_logged_parameters_and_version_check(tmp_path, monkeypatch):
    checkout = _checkout_with(tmp_path / "krt", "route.py")
    (checkout / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    src = tmp_path / "b.kicad_pcb"
    shutil.copy(POUR_PCB, src)
    result = krt.run_kicad_routing_tools(src, tmp_path / "o" / "b.kicad_pcb", timeout=30,
                                         advanced_flags={"--via-cost": 90})
    params = result["logged_parameters"]
    assert params["strict_sizes"] is True
    assert params["advanced_flags"] == {"--via-cost": 90}
    assert params["layer_costs"]["layers"] == ["F.Cu", "B.Cu"]
    assert result["router_version"] == "9.9.9"
    assert result["version_check"]["matches"] is False
    assert "--strict-sizes" in result["args"] and "--layer-costs" in result["args"]
    assert result["strict_sizes_violated"] is False
