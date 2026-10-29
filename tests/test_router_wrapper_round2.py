"""Ticket 03 review round 2: denied flags, net validation, number grammar,
snapshot arity completeness, restore only unlinks what the router created,
keepout-in-a-name survivor."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from kicad_mcp.routers import kicad_routing_tools as krt


# ---- 2: denied flags (each one its own case) --------------------------------


@pytest.mark.parametrize(
    "flag",
    ["--preview-png", "--schematic-dir", "--rip-existing-nets", "--force-reroute", "--relax-drc-severities"],
)
def test_path_taking_and_rip_force_flags_are_denied(flag):
    assert flag in krt.load_cli_snapshot()["modules"]["route.py"]["flags"], "flag must exist so it is a real denial"
    with pytest.raises(ValueError, match="denied"):
        krt.build_route_args("a", "b", advanced_flags={flag: "x"})
    with pytest.raises(ValueError, match="denied"):
        krt.build_route_args("a", "b", advanced_flags={flag: True})


@pytest.mark.parametrize(
    "lever,value",
    [("--fab-tier", "standard"), ("--track-width", 0.2), ("--via-size", 0.6), ("--via-drill", 0.3),
     ("--net-clearances", "{}"), ("--max-ripup", 3), ("--via-cost", 100)],
)
def test_performance_levers_stay_allowed(lever, value):
    assert lever in krt.build_route_args("a", "b", advanced_flags={lever: value})


# ---- 3: nets -----------------------------------------------------------------


@pytest.mark.parametrize("net", ["-x", "--overwrite", "-"])
def test_net_patterns_starting_with_dash_are_rejected_naming_the_net(net):
    with pytest.raises(ValueError, match="net pattern"):
        krt.build_route_args("a", "b", nets=("GND", net))


def test_normal_net_patterns_pass():
    args = krt.build_route_args("a", "b", nets=("GND", "Net-(U1-PA1)", "/SDA*"))
    assert args[args.index("--nets") + 1:args.index("--nets") + 4] == ["GND", "Net-(U1-PA1)", "/SDA*"]


# ---- 4: number grammar -------------------------------------------------------


@pytest.mark.parametrize("token", ["-1\n", "-1 ", "-١", "-1e3", "-.5", "-1."])
def test_only_plain_ascii_numbers_may_start_with_a_dash(token):
    with pytest.raises(ValueError):
        krt.build_route_args("a", "b", advanced_flags={"--via-cost": token})


@pytest.mark.parametrize("token", ["-1", "-0.5", "-12.25"])
def test_plain_negative_numbers_pass(token):
    assert token in krt.build_route_args("a", "b", advanced_flags={"--via-cost": token})


# ---- 5: arity completeness ---------------------------------------------------


def test_every_route_flag_in_the_committed_snapshot_has_an_arity():
    route = krt.load_cli_snapshot()["modules"]["route.py"]
    assert sorted(set(route["flags"]) - set(route["arity"])) == []
    assert set(route["arity"].values()) <= {"0", "1", "+", "*"}


def test_capture_fails_when_a_flag_has_no_arity(monkeypatch, krt_checkout):
    checkout = krt_checkout  # absent: skip with the reason, or fail in the gate run (build ticket 53)
    monkeypatch.setattr(krt, "parse_usage_arity", lambda text: {"--via-cost": "1"})
    with pytest.raises(krt.KicadRoutingToolsError, match="arity"):
        krt.capture_cli_snapshot(checkout, Path(sys.executable))


# ---- survivor: "(keepout" inside a quoted name ---------------------------------


def test_keepout_text_inside_a_quoted_zone_name_is_not_a_keepout(tmp_path):
    pcb = tmp_path / "z.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (layers (0 "F.Cu" signal) (31 "B.Cu" signal))\n'
        ' (zone (net 1) (net_name "(keepout") (layer "F.Cu") (fill yes))\n)\n',
        encoding="utf-8",
    )
    assert krt.board_pour_info(pcb)["pour_layers"] == ["F.Cu"]


# ---- 8: restore only unlinks what the router created ---------------------------

_NOOP = """\
import sys
from pathlib import Path
Path(sys.argv[2]).write_text("(kicad_pcb routed)\\n", encoding="utf-8")
"""

_TOUCH_OUTPUT_PROJECT = """\
import sys
from pathlib import Path
Path(sys.argv[2]).write_text("(kicad_pcb routed)\\n", encoding="utf-8")
Path(sys.argv[2]).with_suffix(".kicad_pro").write_text('{"router": "changed"}', encoding="utf-8")
"""


def _run(tmp_path, monkeypatch, body, preexisting_output_project: str | None):
    checkout = tmp_path / "krt"
    (checkout / "py_router").mkdir(parents=True)
    (checkout / "py_router" / "route.py").write_text(body, encoding="utf-8")
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    src = tmp_path / "in" / "b.kicad_pcb"
    src.parent.mkdir()
    src.write_text("(kicad_pcb)", encoding="utf-8")  # NO sibling .kicad_pro
    out = tmp_path / "out" / "b.kicad_pcb"
    out.parent.mkdir()
    if preexisting_output_project is not None:
        out.with_suffix(".kicad_pro").write_text(preexisting_output_project, encoding="utf-8")
    return krt.run_kicad_routing_tools(src, out, timeout=30), out


def test_preexisting_unchanged_output_project_is_never_unlinked(tmp_path, monkeypatch):
    result, out = _run(tmp_path, monkeypatch, _NOOP, '{"mine": true}')
    assert out.with_suffix(".kicad_pro").read_text(encoding="utf-8") == '{"mine": true}'
    assert result["project_removed_router_created"] is False
    assert result["router_overwrote_project"] is False


def test_preexisting_output_project_changed_by_router_is_disclosed_not_deleted(tmp_path, monkeypatch):
    result, out = _run(tmp_path, monkeypatch, _TOUCH_OUTPUT_PROJECT, '{"mine": true}')
    assert out.with_suffix(".kicad_pro").exists()
    assert result["router_overwrote_project"] is True
    assert result["project_removed_router_created"] is False
