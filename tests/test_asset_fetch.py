"""Tests for fetch_part_assets (archived backlog P1-05).

The "real" tests hit the live EasyEDA API via the easyeda2kicad CLI --
deliberate per the ticket: this is a thin wrapper whose only real risk is
the actual conversion pipeline, which mocking would hide entirely. LCSC
C2040 (RP2040) is the tool's own canonical example part (used in its
--help text), chosen for stability.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.sourcing.asset_fetch import fetch_part_assets


def test_fetch_part_assets_rejects_malformed_lcsc_id(tmp_path):
    result = fetch_part_assets("not-an-lcsc-id", tmp_path)
    assert result["status"] == "error"
    assert "not a valid LCSC part number" in result["reason"]


def test_fetch_part_assets_rejects_path_traversal_attempt(tmp_path):
    # Security regression test (independent review, 2026-09-02): this used
    # to pass validation (only checked startswith("C")) and, because
    # `output_dir / lcsc_id` discards output_dir when the right side looks
    # absolute, would have written outside tmp_path entirely.
    result = fetch_part_assets("C:/Windows/Temp/evil", tmp_path)
    assert result["status"] == "error"
    assert "not a valid LCSC part number" in result["reason"]


@pytest.mark.network
def test_fetch_part_assets_real_part(tmp_path):
    result = fetch_part_assets("C2040", tmp_path)
    assert result["status"] == "ok", result
    assert result["symbol_path"] and Path(result["symbol_path"]).is_file()
    assert result["footprint_dir"] and Path(result["footprint_dir"]).is_dir()
    assert result["model_3d_dir"] and Path(result["model_3d_dir"]).is_dir()
    assert any(Path(result["footprint_dir"]).glob("*.kicad_mod"))


def test_fetch_part_assets_nonexistent_lcsc_id_is_error_not_exception(tmp_path):
    result = fetch_part_assets("C999999999", tmp_path)
    assert result["status"] == "error"
    assert result["lcsc_id"] == "C999999999"
