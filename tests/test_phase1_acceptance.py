"""Phase 1 acceptance test (archived backlog P1-10).

One integration test chaining the real Phase 1 sourcing pipeline against
a real part, proving the tools compose end-to-end and not just in
isolation -- the same class of gap the 2026-09-02 independent review
found in P1-01 (search_parts had zero registered providers on the real
server despite every individual piece's own tests passing).

Nexar/Digi-Key are still credential-blocked (P1-02/P1-03) and LCSC-scrape
is bot-walled (P1-04), so this doesn't literally call search_parts for a
live result -- it starts from a real, known LCSC part (C2040 / RP2040,
the same one used in tests/test_asset_fetch.py and easyeda2kicad's own
--help example) standing in for "the candidate search_parts would have
returned", then chains every REAL subsequent step for real: fetch its
real datasheet, extract structured content from it, fetch its real KiCad
assets, register them into a real project with portability enforced, and
audit the result. A second, network-free test proves the ambiguous-match
checkpoint (select_component -> propose/confirm) is reachable end-to-end,
separately from archived backlog P1-16's own unit tests of the pieces in
isolation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.sourcing.asset_fetch import fetch_part_assets
from kicad_mcp.sourcing.datasheet import fetch_datasheet, inspect_datasheet
from kicad_mcp.toolsets.library import build as build_library_toolset
from kicad_mcp.toolsets.project import build as build_project_toolset
from kicad_mcp.toolsets.sourcing import build as build_sourcing_toolset
from mcp.server.mcpserver import MCPServer

# RP2040's real, stable, official datasheet URL (Raspberry Pi's own CDN,
# not a distributor mirror -- avoids the bot-walling that made the
# ATtiny1616 datasheet unfetchable per archived backlog P1-08's own notes).
_RP2040_DATASHEET_URL = "https://datasheets.raspberrypi.com/rp2040/rp2040-datasheet.pdf"
_RP2040_LCSC_ID = "C2040"


def _tools(mcp: MCPServer, build_fn) -> dict:
    build_fn(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.mark.network
def test_phase1_pipeline_search_result_to_portable_registered_library(tmp_path: Path):
    """search_parts candidate (simulated) -> real datasheet verification ->
    real asset retrieval -> real portable library registration -> audit."""

    # --- Stage 0: the part a real search_parts call would have surfaced. ---
    # (Nexar/Digi-Key credential-blocked, LCSC-scrape bot-walled -- see
    # module docstring. This dict is exactly the shape search_parts'
    # "items" entries have, i.e. a PartResult.model_dump(mode="json").)
    candidate = {
        "manufacturer": "Raspberry Pi",
        "mpn": "RP2040",
        "provider": "lcsc",
        "source": "api",
        "distributor": "LCSC",
        "stock_qty": 50000,
        "price_breaks": [],
        "datasheet_url": _RP2040_DATASHEET_URL,
        "lcsc_id": _RP2040_LCSC_ID,
    }

    # --- Stage 1: verify the candidate against its real datasheet. ---
    datasheets_dir = tmp_path / "datasheets"
    fetch_result = fetch_datasheet(candidate["datasheet_url"], datasheets_dir)
    assert fetch_result["status"] == "ok", fetch_result
    pdf_path = Path(fetch_result["path"])
    assert pdf_path.is_file()
    assert fetch_result["bytes"] > 0

    inspection = inspect_datasheet(pdf_path)
    assert inspection["status"] == "ok", inspection
    assert inspection["page_count"] > 0
    # RP2040's real datasheet has real pin-mux and package-dimension
    # tables -- assert something structured was actually extracted, not
    # just "didn't crash".
    assert inspection["pin_tables"] or inspection["package_dimension_tables"], (
        "expected at least one structured table from a real, large, "
        "text-based datasheet -- extraction may have silently regressed"
    )

    # --- Stage 2: candidate verified -- create the project it belongs in. ---
    mcp = MCPServer("phase1-acceptance")
    project_tools = _tools(mcp, build_project_toolset)
    project_dir = tmp_path / "rp2040_breakout"
    created = project_tools["project_create"](str(project_dir), "rp2040_breakout")
    assert created["status"] == "created", created
    assert Path(created["pcb_path"]).is_file()
    assert Path(created["schematic_path"]).is_file()

    # --- Stage 3: fetch real KiCad assets, directly under the project dir --
    # (so registration -- which refuses assets living outside project_dir,
    # per docs/ARCHITECTURE.md's portability rule -- can succeed without a
    # separate copy step).
    assets_dir = project_dir / "libraries"
    assets = fetch_part_assets(candidate["lcsc_id"], assets_dir)
    assert assets["status"] == "ok", assets
    assert Path(assets["symbol_path"]).is_file()
    assert Path(assets["footprint_dir"]).is_dir()
    assert any(Path(assets["footprint_dir"]).glob("*.kicad_mod"))

    # --- Stage 4: register into the project's library tables, portably. ---
    mcp2 = MCPServer("phase1-acceptance-library")
    library_tools = _tools(mcp2, build_library_toolset)
    registration = library_tools["register_library_asset"](
        project_dir=str(project_dir),
        symbol_lib_path=assets["symbol_path"],
        footprint_lib_path=assets["footprint_dir"],
        nickname="RP2040",
    )
    assert registration["status"] == "ok", registration
    assert registration["symbol"]["uri"].startswith("${KIPRJMOD}/libraries/")
    assert registration["footprint"]["uri"].startswith("${KIPRJMOD}/libraries/")

    # --- Stage 5: the whole point of the portability work -- audit it. ---
    portability = library_tools["check_project_portability"](str(project_dir))
    assert portability["status"] == "ok", portability
    assert portability["violation_count"] == 0
    assert len(portability["tables_checked"]) == 2  # sym-lib-table and fp-lib-table both written

    # --- Stage 6: registering the exact same asset again should dedupe, --
    # not create a second entry (archived backlog P1-07's dedup guarantee,
    # exercised here as part of a real pipeline run rather than in
    # isolation).
    reregistration = library_tools["register_library_asset"](
        project_dir=str(project_dir),
        symbol_lib_path=assets["symbol_path"],
        footprint_lib_path=assets["footprint_dir"],
    )
    assert reregistration["status"] == "already_present", reregistration


def test_phase1_ambiguous_match_checkpoint_is_reachable_end_to_end():
    """select_component actually routes an ambiguous set of search_parts-
    shaped candidates through propose/confirm and back to a real choice --
    proving archived backlog P1-16's reachability fix works as a real flow, not
    just via its own unit tests calling propose/confirm directly."""
    mcp = MCPServer("phase1-acceptance-sourcing")
    tools = _tools(mcp, build_sourcing_toolset)

    def part(mpn: str, **kw) -> dict:
        return {"manufacturer": "Acme", "mpn": mpn, "provider": "lcsc", "source": "api", **kw}

    # Unambiguous: a single distinct part -- selected immediately, no
    # checkpoint round-trip needed.
    unambiguous = tools["select_component"]([part("R1")])
    assert unambiguous["status"] == "selected"
    assert unambiguous["chosen"]["mpn"] == "R1"

    # Ambiguous: two distinct real candidates search_parts might plausibly
    # return for a loose query, no datasheet verification yet -- must
    # route through the checkpoint rather than silently guessing.
    candidates = [part("STM32F103C8T6"), part("STM32F103CBT6")]
    proposal = tools["select_component"](candidates)
    assert proposal["status"] == "pending_confirmation"
    token = proposal["revision_token"]
    assert token

    confirmed = tools["confirm_component_match"](token, 1)
    assert confirmed["status"] == "confirmed"
    assert confirmed["chosen"]["mpn"] == "STM32F103CBT6"

    # The token is consumed -- a stale re-confirm must fail, not silently
    # re-apply (docs/ARCHITECTURE.md's dry-run + revision-token guarantee).
    stale = tools["confirm_component_match"](token, 0)
    assert stale["status"] == "error"

    # Ambiguity resolved by a verified datasheet inspection instead --
    # select_component should return the leading candidate directly
    # without a checkpoint round-trip.
    verified = tools["select_component"](
        [part("STM32F103C8T6"), part("STM32F103CBT6")],
        datasheet_inspection={"status": "ok", "pin_tables": [{"page": 1, "rows": [["1", "VDD"]]}]},
    )
    assert verified["status"] == "selected"
