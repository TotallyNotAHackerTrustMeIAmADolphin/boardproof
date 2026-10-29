"""Tests for archived backlog P1-08 (datasheet fetch + inspection).

inspect_datasheet is tested against real, saved datasheet PDFs (see
tests/fixtures/datasheets/) rather than synthetic ones, per docs/TESTING.md
level 3 -- real vendor PDFs exercise real formatting quirks a hand-built
minimal PDF wouldn't. fetch_datasheet is tested against a live URL when
network access is available (set NETWORK_TESTS=1), skipped by default so
the fast test run doesn't depend on external connectivity.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from kicad_mcp.sourcing.datasheet import fetch_datasheet, inspect_datasheet

FIXTURES = Path(__file__).parent / "fixtures" / "datasheets"

NETWORK_TESTS = os.environ.get("NETWORK_TESTS") == "1"

# The vendor PDFs are third-party files and are left out of the public export (publish-prep ticket 03);
# the tests that read them skip there, and the export adds a matching entry to scripts/known-skips.toml.
requires_vendor_pdfs = pytest.mark.skipif(
    not (FIXTURES / "lm358_opamp.pdf").is_file(),
    reason="the vendor datasheet PDFs are third-party files and are not in the public export",
)


@requires_vendor_pdfs
def test_inspect_opamp_datasheet_finds_pin_and_ratings_tables():
    result = inspect_datasheet(FIXTURES / "lm358_opamp.pdf")
    assert result["status"] in ("ok", "no_structured_tables_found")
    assert result["page_count"] > 0
    # A real op-amp datasheet has both a pin-out table and an absolute
    # maximum ratings table -- if extraction can't find either, the
    # heuristics are too weak to be useful and that's a real failure.
    assert result["pin_tables"] or result["absolute_max_ratings_tables"]


@requires_vendor_pdfs
def test_inspect_ldo_datasheet_finds_something_structured():
    result = inspect_datasheet(FIXTURES / "ncv7691_ldo.pdf")
    assert result["page_count"] > 0
    assert result["status"] == "ok"
    assert result["pin_tables"] or result["absolute_max_ratings_tables"] or result["package_dimension_tables"]


@requires_vendor_pdfs
def test_inspect_diode_datasheet_finds_something_structured():
    result = inspect_datasheet(FIXTURES / "pmeg2005ej_diode.pdf")
    assert result["page_count"] > 0
    assert result["status"] == "ok"
    assert result["pin_tables"] or result["absolute_max_ratings_tables"] or result["package_dimension_tables"]


@requires_vendor_pdfs
def test_inspect_datasheet_query_filters_tables():
    unfiltered = inspect_datasheet(FIXTURES / "ncv7691_ldo.pdf")
    total_unfiltered = (
        len(unfiltered["pin_tables"])
        + len(unfiltered["absolute_max_ratings_tables"])
        + len(unfiltered["package_dimension_tables"])
    )
    filtered = inspect_datasheet(FIXTURES / "ncv7691_ldo.pdf", query="a-string-unlikely-to-appear-xyzzy")
    total_filtered = (
        len(filtered["pin_tables"])
        + len(filtered["absolute_max_ratings_tables"])
        + len(filtered["package_dimension_tables"])
    )
    assert total_filtered == 0
    assert total_unfiltered > 0


def test_inspect_datasheet_missing_file_is_incomplete_not_an_exception():
    result = inspect_datasheet(FIXTURES / "does_not_exist.pdf")
    assert result["status"] == "incomplete"
    assert "not found" in result["reason"]


def test_inspect_datasheet_non_pdf_file_reports_incomplete_not_exception(tmp_path):
    bogus = tmp_path / "not_a_pdf.pdf"
    bogus.write_text("this is not a pdf")
    result = inspect_datasheet(bogus)
    assert result["status"] == "incomplete"


def test_fetch_datasheet_rejects_non_pdf_response_without_writing_garbage(tmp_path):
    # A URL that returns HTML (bot-block/redirect page) should be reported
    # explicitly, not silently saved as a ".pdf" file full of HTML.
    result = fetch_datasheet("https://example.com/", tmp_path)
    if result["status"] == "incomplete" and "request failed" in result.get("reason", ""):
        pytest.skip("no network access in this sandbox")
    assert result["status"] == "incomplete"
    assert not any(tmp_path.iterdir())


@pytest.mark.skipif(not NETWORK_TESTS, reason="set NETWORK_TESTS=1 to run live-network fetch tests")
def test_fetch_datasheet_real_pdf(tmp_path):
    result = fetch_datasheet("https://www.ti.com/lit/ds/symlink/lm358.pdf", tmp_path)
    assert result["status"] == "ok"
    assert Path(result["path"]).exists()
    assert result["bytes"] > 1000
