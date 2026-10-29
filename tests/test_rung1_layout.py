"""Ticket 10: the rung-1 layout flow, run through the server's own tools.

Seam: ``evals/rung1/build_rung1_layout.flow()`` (MCP client against build_server()). It must
leave evals/rung1/project byte-identical, produce one new revision directory per step, read
placements back from the raw (at ...) nodes, and stop at a refusal without setting any
allow_* flag itself. It never claims a board is routed: only the gate may.
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "evals" / "rung1" / "build_rung1_layout.py"
PROJECT = ROOT / "evals" / "rung1" / "project"


def _digest() -> dict[str, str]:
    return {
        str(p.relative_to(PROJECT)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(PROJECT.rglob("*")) if p.is_file()
    }


@pytest.fixture(scope="module")
def flow_result(tmp_path_factory):
    from kicad_mcp.kicad_cli import find_kicad_cli

    try:
        find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    if not (PROJECT / "rung1_usbc_attiny1616.kicad_sch").is_file():
        pytest.skip("rung-1 project not built")
    spec = importlib.util.spec_from_file_location("build_rung1_layout", DRIVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.RUN = tmp_path_factory.mktemp("layout_run") / "run"  # never clobber the measured run
    before = _digest()
    result = asyncio.run(mod.flow())
    return mod, before, _digest(), result


def test_source_project_is_never_touched(flow_result):
    _, before, after, _ = flow_result
    assert before == after


def test_steps_ran_through_the_named_tools_and_wrote_revisions(flow_result):
    _, _, _, result = flow_result
    ran = {s["step"]: s for s in result["steps"] if s["ok"]}
    assert "propose_schematic_import" in ran["import_from_schematic"]["tool"]
    assert "propose_board_outline" in ran["board_outline"]["tool"]
    assert ran["placement"]["tool"] == "place_footprints"
    # the import resolved all 13 schematic footprints (J1 from the project library) + 4 holes
    for step in ("import_from_schematic", "board_outline", "placement"):
        assert Path(result["paths"][step]).is_file(), step


def test_placement_is_confirmed_against_raw_positions(flow_result):
    mod, _, _, result = flow_result
    raw = mod.raw_positions(Path(result["paths"]["placement"]))
    step = next(s for s in result["steps"] if s["step"] == "placement")
    layout = step["args"]["layout_board_local_mm"]  # LAYOUT plus the derived C3 (ticket 114)
    assert set(layout) == set(mod.LAYOUT) | {"C3"}
    for ref, (x, y) in layout.items():
        assert raw[ref] == (round(mod.OX + x, 3), round(mod.OY + y, 3)), ref
    assert step["result"]["decoupling_rule_findings"] == []
    assert {"J1", "U1", "H1", "H4"} <= set(raw)


@pytest.mark.needs_krt  # blocked is None only once KRT's plane step has run (build ticket 53)
def test_rules_loosening_is_limited_to_the_owner_approved_list(flow_result):
    mod, _, _, result = flow_result
    assert result["blocked"] is None
    step = next(s for s in result["steps"] if s["step"] == "board_rules")
    approval = step["approval"]
    assert approval["allow_loosen_passed"] is True
    assert approval["outside_approval"] == []
    # 7 owner-approved items (2026-10-11) plus the same Default-clearance loosening read from the 0.15 mm that
    # kicad-tools 0.24.0's blank project starts at (publish-prep ticket 02a; a smaller step to the same 0.127).
    assert len(approval["approved_list"]) == 8
    assert "('netclass:Default', 'clearance', 0.15, 0.127)" in approval["approved_list"]
    assert approval["loosened_seen"] and set(approval["loosened_seen"]) <= set(approval["approved_list"])
    # the flags that are NOT approved are never passed by the driver
    src = DRIVER.read_text(encoding="utf-8")
    assert "allow_incoherent=" not in src and "allow_overlap=" not in src


@pytest.mark.needs_krt  # the pour step runs KRT's route_planes.py (build ticket 53)
def test_flow_reaches_the_pour_step_through_the_tools(flow_result):
    _, _, _, result = flow_result
    ran = {s["step"]: s for s in result["steps"]}
    assert ran["board_rules"]["ok"] and ran["pour_step"]["ok"]
    assert ran["pour_step"]["result"]["status"] == "poured"
    assert Path(result["final_board"]).is_file()


def test_record_never_says_routed(flow_result):
    _, _, _, result = flow_result
    text = json.dumps(result).lower()
    assert "routed" not in text.replace("not a routing", "")
