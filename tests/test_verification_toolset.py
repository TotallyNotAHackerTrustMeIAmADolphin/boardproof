"""Tests for the verification toolset (src/kicad_mcp/toolsets/verification.py).

Runs tools through the real MCPServer.call_tool path (not the bare
functions) so the toolset registration contract from registry.py is
exercised too, not just the underlying logic.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import write_layer
from kicad_mcp.toolsets import verification

FIXTURES = Path(__file__).parent / "fixtures"
PCB_FIXTURE = FIXTURES / "simple_led.kicad_pcb"
SCH_FIXTURE = FIXTURES / "simple_led.kicad_sch"


@pytest.fixture
def mcp() -> MCPServer:
    server = MCPServer("test")
    verification.build(server)
    return server


async def _call(mcp: MCPServer, name: str, **kwargs) -> dict:
    result = await mcp.call_tool(name, kwargs)
    return json.loads(result.content[0].text)


def test_build_registers_expected_tools(mcp: MCPServer) -> None:
    fresh = MCPServer("fresh")
    registered = verification.build(fresh)
    assert registered == [
        "pcb_run_drc",
        "pcb_check_placement",
        "sch_run_erc",
        "pcb_check_collisions",
        "pcb_validate_proposed_placements",
        "get_manufacturer_design_rules",
        "list_manufacturer_profiles",
        "pcb_get_design_rules",
        "pcb_check_manufacturability",
    ]


@pytest.mark.asyncio
async def test_pcb_run_drc_returns_structured_violations(mcp: MCPServer) -> None:
    # Real kicad-cli DRC (archived backlog P1-14) -- the simple_led fixture is
    # unrouted, so it has real DRC findings (unconnected items) that the
    # old placement-only checker could never have caught.
    out = await _call(mcp, "pcb_run_drc", pcb_path=str(PCB_FIXTURE))
    assert out["status"] == "complete"
    assert out["violation_count"] == len(out["violations"])
    assert out["violations"], "fixture board is expected to have at least one DRC finding"
    violation = out["violations"][0]
    assert {"type", "severity", "description", "items"} <= violation.keys()
    assert out["unconnected_count"] == len(out["unconnected_items"])
    assert out["unconnected_items"], "unrouted fixture board is expected to have unconnected items"


@pytest.mark.asyncio
async def test_pcb_run_drc_missing_file_is_incomplete_not_an_exception(mcp: MCPServer) -> None:
    out = await _call(mcp, "pcb_run_drc", pcb_path=str(FIXTURES / "nope.kicad_pcb"))
    assert out["status"] == "incomplete"
    assert "reason" in out


@pytest.mark.asyncio
async def test_pcb_check_placement_returns_structured_violations(mcp: MCPServer) -> None:
    # This is the renamed old pcb_run_drc -- placement geometry only
    # (pad clearance/courtyard/edge/hole-to-hole), not a full DRC.
    out = await _call(mcp, "pcb_check_placement", pcb_path=str(PCB_FIXTURE))
    assert out["status"] == "complete"
    assert out["violation_count"] == len(out["violations"])
    assert out["violations"], "fixture board is expected to have at least one placement finding"
    violation = out["violations"][0]
    assert {"type", "severity", "description", "location", "components"} <= violation.keys()


@pytest.mark.asyncio
async def test_pcb_check_placement_missing_file_is_incomplete_not_an_exception(mcp: MCPServer) -> None:
    out = await _call(mcp, "pcb_check_placement", pcb_path=str(FIXTURES / "nope.kicad_pcb"))
    assert out["status"] == "incomplete"
    assert "reason" in out


@pytest.mark.asyncio
async def test_sch_run_erc_returns_structured_report(mcp: MCPServer) -> None:
    out = await _call(mcp, "sch_run_erc", sch_path=str(SCH_FIXTURE))
    assert out["status"] == "complete"
    assert "violations" in out
    assert isinstance(out["violations"], list)


@pytest.mark.asyncio
async def test_pcb_check_collisions_filters_to_collision_types(mcp: MCPServer) -> None:
    out = await _call(mcp, "pcb_check_collisions", pcb_path=str(PCB_FIXTURE))
    assert out["status"] == "complete"
    assert out["collision_count"] == len(out["collisions"])
    # Fixed 2026-09-02 per archived backlog P1-15 (independent review): silkscreen_pad
    # and off_board were real ConflictType values silently missing from this set.
    allowed = {
        "pad_clearance",
        "courtyard_overlap",
        "edge_clearance",
        "hole_to_hole",
        "silkscreen_pad",
        "off_board",
    }
    for c in out["collisions"]:
        assert c["type"] in allowed


@pytest.mark.asyncio
async def test_pcb_check_collisions_detects_a_real_overlap(mcp: MCPServer, tmp_path: Path) -> None:
    # Fixed 2026-09-02 per archived backlog P1-15 (independent review): the only
    # prior fixture had zero collisions, so this tool's core "did two things
    # overlap" behavior had never actually been exercised by a passing
    # assertion. Build a real board with two footprints placed on top of
    # each other and confirm a real, non-zero, correctly-shaped result.
    from kicad_tools.schema import PCB

    from kicad_mcp.sourcing.footprint_gen import generate_footprint

    fp_path = tmp_path / "R_0402.kicad_mod"
    gen = generate_footprint("chip", fp_path, size="0402", name="R_0402_1005Metric")
    assert gen["status"] == "ok", gen

    board = PCB.create(width=50, height=50)
    board.add_footprint_from_file(str(fp_path), "R1", 10.0, 10.0)
    board.add_footprint_from_file(str(fp_path), "R2", 10.05, 10.0)  # 0.05mm away -- guaranteed overlap
    overlap_pcb = tmp_path / "overlap.kicad_pcb"
    write_layer.save_pcb(board, overlap_pcb)

    out = await _call(mcp, "pcb_check_collisions", pcb_path=str(overlap_pcb))
    assert out["status"] == "complete"
    assert out["collision_count"] > 0
    assert out["collision_count"] == len(out["collisions"])
    types_found = {c["type"] for c in out["collisions"]}
    assert types_found & {"pad_clearance", "courtyard_overlap"}


@pytest.mark.asyncio
async def test_pcb_validate_proposed_placements_batch(mcp: MCPServer) -> None:
    # J1 is known (from test_pcb_check_placement) to trigger an
    # off-board-connector finding at its current position; re-proposing
    # that same position should surface as a rejected placement, exercising
    # the batch path.
    out = await _call(
        mcp,
        "pcb_validate_proposed_placements",
        pcb_path=str(PCB_FIXTURE),
        placements={"J1": [5.0, 10.0, 0.0]},
    )
    assert out["status"] == "complete"
    assert out["total_placements"] == 1


@pytest.mark.asyncio
async def test_pcb_check_collisions_missing_file_is_incomplete(mcp: MCPServer) -> None:
    out = await _call(mcp, "pcb_check_collisions", pcb_path=str(FIXTURES / "nope.kicad_pcb"))
    assert out["status"] == "incomplete"


# ---------------------------------------------------------------------------
# Design-rule provenance (archived backlog P1-18 + P1-21)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pcb_check_placement_reports_the_source_of_every_rule(mcp: MCPServer) -> None:
    # P1-18: a "clean" result used to be clean against hardcoded literals.
    # Now every dimension the check ran with names where it came from.
    out = await _call(mcp, "pcb_check_placement", pcb_path=str(PCB_FIXTURE))
    assert out["status"] == "complete"
    applied = out["rules_applied"]
    assert set(applied) == {
        "clearance_mm",
        "courtyard_margin_mm",
        "edge_clearance_mm",
        "hole_to_hole_mm",
    }
    for rule in applied.values():
        assert isinstance(rule["value_mm"], (int, float))
        assert rule["source"]
    # simple_led.kicad_pro really does declare a Default netclass at 0.2mm
    assert applied["clearance_mm"] == {"value_mm": 0.2, "source": "board_netclass:Default"}
    # KiCad has no courtyard-margin concept, so that one is honestly labelled
    assert applied["courtyard_margin_mm"]["source"] == "builtin_default"
    assert out["design_rules"]["project_file"].endswith("simple_led.kicad_pro")
    assert out["manufacturer_profile"] is None


@pytest.mark.asyncio
async def test_pcb_check_placement_explicit_argument_beats_board_rules(mcp: MCPServer) -> None:
    out = await _call(
        mcp, "pcb_check_placement", pcb_path=str(PCB_FIXTURE), clearance_mm=0.45
    )
    assert out["rules_applied"]["clearance_mm"] == {
        "value_mm": 0.45,
        "source": "explicit_argument",
    }


@pytest.mark.asyncio
async def test_pcb_check_placement_falls_back_to_fab_floor_only_where_board_is_silent(
    mcp: MCPServer,
) -> None:
    out = await _call(
        mcp, "pcb_check_placement", pcb_path=str(PCB_FIXTURE), manufacturer="jlcpcb"
    )
    assert out["status"] == "complete"
    applied = out["rules_applied"]
    # board declares a clearance -> board wins over the fab floor (0.1mm)
    assert applied["clearance_mm"]["source"] == "board_netclass:Default"
    # board declares no edge clearance -> JLCPCB's published 0.2mm is used
    assert applied["edge_clearance_mm"] == {
        "value_mm": 0.2,
        "source": "manufacturer_profile:jlcpcb",
    }
    assert applied["hole_to_hole_mm"]["value_mm"] == 0.45
    assert out["manufacturer_profile"]["source_url"].startswith("https://jlcpcb.com")
    assert out["manufacturability"]["manufacturer"] == "jlcpcb"


@pytest.mark.asyncio
async def test_use_board_rules_false_ignores_the_design(mcp: MCPServer) -> None:
    out = await _call(
        mcp,
        "pcb_check_placement",
        pcb_path=str(PCB_FIXTURE),
        manufacturer="jlcpcb",
        use_board_rules=False,
    )
    assert out["rules_applied"]["clearance_mm"] == {
        "value_mm": 0.1,
        "source": "manufacturer_profile:jlcpcb",
    }
    assert out["design_rules"] is None


@pytest.mark.asyncio
async def test_unknown_manufacturer_is_incomplete_and_names_what_exists(mcp: MCPServer) -> None:
    for tool, kwargs in (
        ("pcb_check_placement", {"pcb_path": str(PCB_FIXTURE)}),
        ("pcb_check_collisions", {"pcb_path": str(PCB_FIXTURE)}),
        ("pcb_validate_proposed_placements", {"pcb_path": str(PCB_FIXTURE), "placements": {}}),
        ("pcb_check_manufacturability", {"pcb_paths": [str(PCB_FIXTURE)]}),
    ):
        out = await _call(mcp, tool, manufacturer="acme-pcb", **kwargs)
        assert out["status"] == "incomplete", tool
        assert "jlcpcb" in out["reason"], tool


@pytest.mark.asyncio
async def test_pcb_run_drc_reports_what_it_checked_against(mcp: MCPServer) -> None:
    out = await _call(mcp, "pcb_run_drc", pcb_path=str(PCB_FIXTURE))
    assert out["status"] == "complete"
    assert out["design_rules"]["effective_rules"]["clearance_mm"]["value_mm"] == 0.2
    assert "capability floor" in out["rule_precedence"]
    assert "manufacturability" not in out


@pytest.mark.asyncio
async def test_pcb_run_drc_manufacturer_adds_a_floor_audit_without_changing_drc(
    mcp: MCPServer,
) -> None:
    plain = await _call(mcp, "pcb_run_drc", pcb_path=str(PCB_FIXTURE))
    with_mfr = await _call(mcp, "pcb_run_drc", pcb_path=str(PCB_FIXTURE), manufacturer="jlc")
    # The DRC result itself must be identical -- the profile is an audit, not
    # a rule override (overriding would mean rewriting the board's .kicad_pro).
    assert with_mfr["violation_count"] == plain["violation_count"]
    assert with_mfr["unconnected_count"] == plain["unconnected_count"]
    audit = with_mfr["manufacturability"]
    assert audit["manufacturer"] == "jlcpcb"
    assert audit["fab_config_id"] == "2layer_1oz"
    # unverified rules must not read as a pass
    assert audit["meets_fab_floor"] is None
    assert audit["status"] == "partial"


@pytest.mark.asyncio
async def test_get_manufacturer_design_rules(mcp: MCPServer) -> None:
    out = await _call(mcp, "get_manufacturer_design_rules", manufacturer="jlcpcb", layer_count=4)
    assert out["status"] == "complete"
    assert out["id"] == "jlcpcb"
    assert out["design_rules"]["config_id"] == "4layer_1oz"
    assert out["design_rules"]["values_mm"]["min_trace_width_mm"] == 0.09
    assert out["canonical_floor_mm"]["annular_ring_mm"] == 0.15
    assert out["source_url"] == "https://jlcpcb.com/capabilities/pcb-capabilities"
    assert out["source_fetched_date"] == "2026-09-04"
    # unconfirmed values are surfaced, not silently zero-filled
    assert "min_pad_size_mm" in out["design_rules"]["unconfirmed_fields"]
    assert out["design_rules"]["values_mm"]["min_pad_size_mm"] is None


@pytest.mark.asyncio
async def test_get_manufacturer_design_rules_rejects_unknown_fab(mcp: MCPServer) -> None:
    out = await _call(mcp, "get_manufacturer_design_rules", manufacturer="not-a-fab")
    assert out["status"] == "incomplete"
    assert out["available"] == ["jlcpcb"]
    assert "jlcpcb" in out["reason"]


@pytest.mark.asyncio
async def test_get_manufacturer_design_rules_rejects_unsupported_configuration(
    mcp: MCPServer,
) -> None:
    out = await _call(mcp, "get_manufacturer_design_rules", manufacturer="jlcpcb", layer_count=3)
    assert out["status"] == "incomplete"
    assert "3" in out["reason"]


@pytest.mark.asyncio
async def test_list_manufacturer_profiles(mcp: MCPServer) -> None:
    out = await _call(mcp, "list_manufacturer_profiles")
    assert out["status"] == "complete"
    assert out["count"] == len(out["manufacturers"]) == 1
    assert out["manufacturers"][0]["aliases"]


@pytest.mark.asyncio
async def test_pcb_get_design_rules_is_batch(mcp: MCPServer) -> None:
    out = await _call(
        mcp,
        "pcb_get_design_rules",
        pcb_paths=[str(PCB_FIXTURE), str(FIXTURES / "nope.kicad_pcb")],
    )
    assert out["count"] == 2
    assert out["status"] == "partial"  # one board couldn't be read
    assert out["boards"][0]["status"] == "complete"
    assert out["boards"][0]["netclasses"][0]["name"] == "Default"
    assert out["boards"][1]["status"] == "incomplete"


@pytest.mark.asyncio
async def test_pcb_get_design_rules_rejects_an_empty_batch(mcp: MCPServer) -> None:
    out = await _call(mcp, "pcb_get_design_rules", pcb_paths=[])
    assert out["status"] == "incomplete"


@pytest.mark.asyncio
async def test_pcb_check_manufacturability_is_batch_and_evidence_gated(mcp: MCPServer) -> None:
    out = await _call(
        mcp,
        "pcb_check_manufacturability",
        pcb_paths=[str(PCB_FIXTURE)],
        manufacturer="jlcpcb",
        layer_count=2,
    )
    assert out["count"] == 1
    assert out["manufacturer"] == "jlcpcb"
    board = out["boards"][0]
    assert board["below_fab_floor_count"] == 0
    assert board["meets_fab_floor"] is None  # unverified rules are not a pass
    findings = {f["rule"]: f for f in board["findings"]}
    assert findings["clearance_mm"]["verdict"] == "ok"
    assert findings["clearance_mm"]["board_value_source"] == "board_netclass:Default"
    assert "jlcpcb.com" in findings["clearance_mm"]["fab_floor_source"]
