"""Ticket 23: the pour-first flow is measurable. The preflight guard reads the raw file: a
zone with only an outline (no stored ``filled_polygon``) is allowed, a zone with stored fill
stays refused (P4-06), keepouts never count. Evidence for the policy was measured with the
real router and the real gate (see the ticket report)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp import rung_backends, rung_runner

from test_rung_backends import KRT_BASELINE, needs_krt  # noqa: F401

FIXTURES = Path(__file__).parent / "fixtures"
POURED = (Path(__file__).resolve().parents[1] / "evals" / "rung1" / "layout_run" / "07_poured")


def _zone_board(tmp_path: Path, name: str, strip_fill: bool) -> Path:
    from kicad_tools.schema import PCB

    project = tmp_path / name
    shutil.copytree(FIXTURES / "zone_obstacle", project)
    board = project / "zone_obstacle.kicad_pcb"
    if strip_fill:
        pcb = PCB.load(board)
        for zone in [c for c in pcb._sexp.children if c.name == "zone"]:
            for fill in zone.find_all("filled_polygon"):
                zone.remove(fill)
        write_layer.save_pcb(pcb, board)
    return board


def test_a_zone_with_stored_fill_stays_refused(tmp_path):
    board = _zone_board(tmp_path, "filled", strip_fill=False)
    assert "filled_polygon" in board.read_text()
    reason = rung_backends.input_refusal(board)
    assert reason and "stored fill" in reason and "P4-06" in reason


def test_an_outline_only_zone_is_allowed_even_though_it_is_a_fill_yes_pour(tmp_path):
    board = _zone_board(tmp_path, "outline", strip_fill=True)
    text = board.read_text()
    assert "filled_polygon" not in text
    assert "(fill yes" in text or "(fill" in text  # a pour, as the pour step writes it
    assert rung_backends.input_refusal(board) is None


def test_the_allowed_pour_zones_are_recorded_for_the_manifest(tmp_path):
    board = _zone_board(tmp_path, "outline", strip_fill=True)
    zones = rung_backends.pour_zones(board)
    assert [(z["net"], z["filled_in_file"]) for z in zones] == [("GND", False)]
    assert zones[0]["layer"]


@pytest.mark.skipif(not (POURED / "rung1_usbc_attiny1616.kicad_pcb").is_file(), reason="rung-1 poured revision absent")
def test_the_rung1_poured_revision_is_not_refused_but_records_its_pour(tmp_path):
    project = tmp_path / "poured"
    shutil.copytree(POURED, project)  # a copy: the evals tree is never edited
    board = project / "rung1_usbc_attiny1616.kicad_pcb"
    assert rung_backends.input_refusal(board) is None
    zones = rung_backends.pour_zones(board)
    assert [(z["net"], z["layer"], z["filled_in_file"]) for z in zones] == [("GND", "B.Cu", False)]


def test_a_keepout_never_counts_as_a_pour(tmp_path):
    from kicad_tools.sexp import parse_string
    from kicad_tools.schema import PCB

    board = _zone_board(tmp_path, "keepout", strip_fill=True)
    pcb = PCB.load(board)
    for zone in [c for c in pcb._sexp.children if c.name == "zone"]:
        pcb._sexp.remove(zone)
    pcb._sexp.append(parse_string(
        '(zone (net 0) (net_name "") (layer "F.Cu") (keepout (tracks not_allowed) (vias not_allowed)) '
        '(polygon (pts (xy 1 1) (xy 5 1) (xy 5 5))))'))
    write_layer.save_pcb(pcb, board)
    assert rung_backends.input_refusal(board) is None
    assert rung_backends.pour_zones(board) == []


@needs_krt
def test_real_router_and_real_gate_on_an_outline_only_pour_leave_no_violation(tmp_path):
    """The measured evidence for the allowance: route the outline-only GND pour board with
    KRT 0.22.0 and grade it with the real gate (which refills the zone): no shorts, no
    clearance errors, nothing unconnected."""
    from kicad_mcp import verify_board

    board = _zone_board(tmp_path, "real", strip_fill=True)
    baseline = verify_board.snapshot_rules(board)
    (tmp_path / "a").mkdir()
    outcome = rung_backends.krt_backend()(board, dict(KRT_BASELINE), 120.0, tmp_path / "a" / "router.log")
    assert outcome["status"] == "ok", outcome
    graded = verify_board.verify_board(
        board, parity="not_applicable", parity_reason="fixture has no schematic", baseline=baseline)
    assert graded["violations"] == []
    assert graded["unconnected"] == []
    assert graded["verdict"] == "PASS"
