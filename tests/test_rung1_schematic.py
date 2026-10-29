"""Rung-1 schematic + BOM acceptance (ticket 09). Seam: the artifacts that
evals/rung1/build_rung1.py leaves on disk, checked independently."""
import csv
import json
import re
from pathlib import Path

import pytest

R1 = Path(__file__).parent.parent / "evals" / "rung1"
SCH = R1 / "project" / "rung1_usbc_attiny1616.kicad_sch"


def _need(p):
    if not p.exists():
        pytest.fail(f"missing artifact {p} (run evals/rung1/build_rung1.py)")
    return p


def test_selection_log_follows_tier_preference():
    log = json.loads(_need(R1 / "selection_log.json").read_text(encoding="utf-8"))
    assert log["parts"]
    for p in log["parts"]:
        assert p["reason"] and p["lcsc"].startswith("C")
        if p["tier"] == "extended":
            assert p["category"] != "passive_rc" and p.get("extended_exception"), p["lcsc"]


def test_bom_complete_and_matches_schematic():
    rows = list(csv.DictReader(_need(R1 / "bom.csv").open(encoding="utf-8")))
    text = _need(SCH).read_text(encoding="utf-8")
    refs = set(re.findall(r'\(property "Reference" "([A-Z]+\d+)"', text))
    assert refs and {r["Designator"] for r in rows} == refs
    for r in rows:
        assert r["Footprint"] and r["LCSC Part #"].startswith("C")


def test_annotated_no_question_marks():
    text = _need(SCH).read_text(encoding="utf-8")
    assert not re.findall(r'\(property "Reference" "[A-Z#]+\?"', text)


def test_erc_complete_list_recorded_and_clean_or_explained():
    erc = json.loads(_need(R1 / "erc_report.json").read_text(encoding="utf-8"))
    assert isinstance(erc["violations"], list)
    assert erc["unexplained"] == 0


def test_spice_reported_non_gating():
    s = json.loads(_need(R1 / "spice_report.json").read_text(encoding="utf-8"))
    assert s["gating"] is False and s["status"] in ("ok", "incomplete")


def test_netlist_matches_intended_topology():
    nets = json.loads(_need(R1 / "nets.json").read_text(encoding="utf-8"))
    expect = {
        "VBUS": ["F1.1", "J1.A9", "J1.B9"],
        "VBUS_F": ["C1.1", "D1.1", "F1.2", "U2.2"],
        "CC1": ["J1.A5", "R1.1"],
        "CC2": ["J1.B5", "R2.1"],
        "UPDI": ["J2.2", "U1.19"],
        "LED_CTL": ["R3.1", "U1.8"],
        "LED_A": ["D2.2", "R3.2"],
        "+3V3": ["C2.1", "C3.1", "J2.1", "U1.4", "U2.3"],
    }
    for name, pins in expect.items():
        assert nets[name] == sorted(pins), name
    assert {"J1.7", "J1.A12", "J1.B12", "U1.3", "U1.21", "U2.1", "J2.3"} <= set(nets["GND"])
