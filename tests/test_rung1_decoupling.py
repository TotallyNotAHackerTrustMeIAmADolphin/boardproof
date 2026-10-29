"""Build ticket 114: rung 1 places C3 (U1's decoupling capacitor) with its +3V3 pad next to U1's +3V3 pin.

The owner's decision (2026-10-29): C3's 3V3 connection goes directly next to the MCU's 3V3 pin.
The position comes from a stated geometric rule evaluated on raw pad and courtyard geometry
(``evals/rung1/build_rung1_layout.plan_decoupling_capacitor``), not from typed numbers, and it was
fixed before any routing result of the new input existed. These tests pin the RULE on the committed
board (``evals/rung1/layout_run/07_poured``, read from its raw ``(at ...)`` and ``(size ...)`` nodes)
and show with mutants that each part of the rule can fail.

The rule (all numbers computed by the script from the raw geometry):
  1. C3's +3V3 pad is the pad nearest U1; its nearest edge is within ``MAX_PAD_GAP_MM`` (1.5 mm) of
     U1 pin 4's pad edge, and the courtyards keep the board's minimum clearance (0.127 mm) apart;
  2. C3's +3V3 pad does not shadow the straight escape of the neighbouring pad above pin 4 (pin 3,
     GND): its top edge is at least half the minimum track width plus the Power clearance (0.2 mm) below
     pin 3's centre line;
  3. pin 4's centre line runs through C3's +3V3 pad, so the two connect with a straight track.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from kicad_mcp import raw_geometry, write_layer

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "evals" / "rung1" / "build_rung1_layout.py"
STAGES = ROOT / "evals" / "rung1" / "layout_run"
BOARD = STAGES / "07_poured" / "rung1_usbc_attiny1616.kicad_pcb"
OUTLINED = STAGES / "02_outline" / "rung1_usbc_attiny1616.kicad_pcb"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("build_rung1_layout_114", DRIVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_the_committed_board_satisfies_the_rule(mod):
    assert mod.decoupling_findings(write_layer.load(BOARD)) == []


def test_the_rule_numbers_on_the_committed_board(mod):
    """The raw numbers of the committed placement (page mm, read from the raw nodes)."""
    facts = mod.decoupling_facts(write_layer.load(BOARD))
    assert facts["c3_at"] == (149.293, 108.239, 180.0)
    assert facts["u1_pin4_edge_to_c3_pad1_edge_mm"] == pytest.approx(0.632, abs=1e-3)
    assert facts["u1_pin4_edge_to_c3_pad1_edge_mm"] <= mod.MAX_PAD_GAP_MM
    assert facts["c3_pad1_top_minus_pin3_axis_mm"] >= 0.2635 - 1e-9
    assert facts["courtyard_gap_to_u1_mm"] == pytest.approx(0.127, abs=1e-3)


def test_the_script_derives_the_same_placement_from_the_unplaced_board(mod):
    """No typed C3 numbers: the plan is computed from the imported board's pad and courtyard nodes."""
    plan = mod.plan_decoupling_capacitor(write_layer.load(OUTLINED), mod.LAYOUT)
    committed = {f.reference: f for f in raw_geometry.read_footprints(write_layer.load(BOARD))}["C3"]
    assert (round(mod.OX + plan["x"], 3), round(mod.OY + plan["y"], 3), plan["rotation"]) == (
        committed.at_mm[0], committed.at_mm[1], committed.angle_deg)
    assert "C3" not in mod.LAYOUT  # the cap is derived, never typed


def test_other_parts_keep_their_positions(mod):
    """Only C3 moved: the other 12 parts are where ticket 02a left them (board-local mm)."""
    old = {"J1": (10, 18.0), "F1": (10, 13), "D1": (10, 8.5), "R1": (17.6, 15.8), "R2": (17.6, 18.6),
           "C1": (17.6, 12.5), "U2": (21, 8), "C2": (27, 8), "U1": (24.5, 17.5),
           "R3": (21.5, 13.5), "D2": (29.6, 16.2), "J2": (30.6, 7.5)}
    assert mod.LAYOUT == old


def _mutated(mod, ref, *, at=None, angle=None):
    root = write_layer.load(BOARD)
    fp = next(f for f in root.children if f.name == "footprint" and write_layer_ref(f) == ref)
    node = fp.find("at")
    if at is not None:
        write_layer.set_number(node, 0, at[0])
        write_layer.set_number(node, 1, at[1])
    if angle is not None:
        if len(node.values) < 3:
            node.append(write_layer.number(angle))
        else:
            write_layer.set_number(node, 2, angle)
    return root


def write_layer_ref(fp):
    for c in fp.children:
        if c.name == "property" and c.values and c.values[0] == "Reference":
            return str(c.values[1])
    return ""


def test_mutant_the_old_position_fails_the_distance_rule(mod):
    findings = mod.decoupling_findings(_mutated(mod, "C3", at=(153.0, 111.2), angle=0.0))
    assert any("pad gap" in f for f in findings), findings


def test_mutant_the_pad_pointing_away_fails(mod):
    """C3 at the new place but with its GND pad toward U1 (rotation 0 instead of 180)."""
    findings = mod.decoupling_findings(_mutated(mod, "C3", angle=0.0))
    assert any("nearest" in f for f in findings), findings
    assert any("pad gap" in f for f in findings), findings


def test_mutant_the_pad_shadowing_pin_3_fails(mod):
    """C3 moved up so that its +3V3 pad sits on pin 3's escape line."""
    findings = mod.decoupling_findings(_mutated(mod, "C3", at=(149.293, 107.9)))
    assert any("shadows" in f for f in findings), findings


def test_mutant_off_the_pin_axis_fails(mod):
    findings = mod.decoupling_findings(_mutated(mod, "C3", at=(149.293, 109.5)))
    assert any("straight" in f for f in findings), findings


def test_mutant_courtyard_overlap_with_u1_fails(mod):
    findings = mod.decoupling_findings(_mutated(mod, "C3", at=(150.2, 108.239)))
    assert any("courtyard gap to U1" in f for f in findings), findings
    assert any("below the Power clearance" in f for f in findings), findings  # the pads touch


def test_mutant_courtyard_gap_below_the_board_clearance_fails(mod):
    """0.077 mm closer than the rule: courtyards still apart (0.05 mm) but under the 0.127 mm clearance."""
    findings = mod.decoupling_findings(_mutated(mod, "C3", at=(149.37, 108.239)))
    assert [f for f in findings if "courtyard gap to U1" in f], findings
    assert not [f for f in findings if "overlaps" in f], findings


def test_mutant_courtyard_overlap_with_another_part_fails(mod):
    """C3 moved onto R2 (and out of the pad-gap rule too): the overlap names the part."""
    findings = mod.decoupling_findings(_mutated(mod, "C3", at=(148.0, 108.239)))
    assert any("overlaps the courtyard of R2" in f for f in findings), findings


def test_mutant_too_far_away_fails(mod):
    findings = mod.decoupling_findings(_mutated(mod, "C3", at=(146.0, 108.239)))
    assert any("pad gap" in f for f in findings), findings


def test_other_supply_capacitors_are_reported_not_moved(mod):
    """C1 and C2 belong to U2 (SOT-89 on the left of the package, 3 pads in a column): by the same
    pad-gap measure they are 3.5 and 6.1 mm away. Ticket 114 reports and keeps them (owner decision)."""
    table = mod.supply_capacitor_distances(write_layer.load(BOARD))
    assert table["C1"]["ic"] == "U2" and table["C1"]["gap_mm"] == pytest.approx(3.5, abs=0.1)
    assert table["C2"]["ic"] == "U2" and table["C2"]["gap_mm"] == pytest.approx(6.1, abs=0.1)
    assert table["C3"]["ic"] == "U1" and table["C3"]["gap_mm"] <= mod.MAX_PAD_GAP_MM
