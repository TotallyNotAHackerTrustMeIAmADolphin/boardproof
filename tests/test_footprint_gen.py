"""Tests for the P1-06 parametric footprint generation fallback.

Generates real footprints for a few common package types and validates
each one round-trips through kicad-tools' own S-expression parser +
Footprint model, per docs/TESTING.md ("real KiCad files, not synthetic
assertions") -- confirms KiCad's own tooling can actually parse what we
generated, not just that a file got written.
"""

from __future__ import annotations

from kicad_tools.schema import Footprint as SchemaFootprint
from kicad_tools.sexp.parser import parse_file

from kicad_mcp.sourcing.footprint_gen import generate_footprint, list_package_types


def _round_trip_validate(path):
    sexp = parse_file(path)
    footprint = SchemaFootprint.from_sexp(sexp)
    return footprint


def test_list_package_types_includes_chip_and_qfn():
    types = list_package_types()
    assert "chip" in types
    assert "qfn" in types


def test_generate_chip_0402(tmp_path):
    out = tmp_path / "R_0402.kicad_mod"
    result = generate_footprint("chip", out, size="0402", name="R_0402_1005Metric")
    assert result["status"] == "ok"
    assert result["pad_count"] == 2
    assert out.exists()

    footprint = _round_trip_validate(out)
    assert footprint is not None


def test_generate_chip_0603(tmp_path):
    out = tmp_path / "C_0603.kicad_mod"
    result = generate_footprint("chip", out, size="0603", name="C_0603_1608Metric")
    assert result["status"] == "ok"
    assert result["pad_count"] == 2
    _round_trip_validate(out)


def test_generate_qfn(tmp_path):
    out = tmp_path / "QFN-16.kicad_mod"
    result = generate_footprint(
        "qfn", out, pins=16, pitch=0.5, body_size=3.0, name="QFN-16_3x3mm_P0.5mm"
    )
    assert result["status"] == "ok"
    assert result["pad_count"] >= 16  # >= to allow for an exposed thermal pad
    _round_trip_validate(out)


def test_generate_soic(tmp_path):
    out = tmp_path / "SOIC-8.kicad_mod"
    result = generate_footprint("soic", out, pins=8, pitch=1.27, name="SOIC-8_3.9x4.9mm_P1.27mm")
    assert result["status"] == "ok"
    assert result["pad_count"] == 8
    _round_trip_validate(out)


def test_unsupported_package_type_is_evidence_gated(tmp_path):
    out = tmp_path / "bogus.kicad_mod"
    result = generate_footprint("not_a_real_package_type", out)
    assert result["status"] == "unsupported_package_type"
    assert "available" in result
    assert not out.exists()


def test_generation_failure_reports_incomplete_not_exception(tmp_path):
    out = tmp_path / "bad_chip.kicad_mod"
    # "0402" is a valid chip size; an unrecognized one should fail inside
    # the generator rather than us guessing at valid values -- exercises
    # the evidence-gated failure path without needing to fabricate a
    # generator-internal error.
    result = generate_footprint("chip", out, size="not_a_real_size")
    assert result["status"] == "incomplete"
    assert "reason" in result
