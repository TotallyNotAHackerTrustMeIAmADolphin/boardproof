"""Unit tests for the sourcing toolset's search_parts/list_sourcing_providers,
calling the registered functions directly (see docs/TESTING.md level 1), plus a
protocol-level check (level 2) that the toolset actually loads via the
running server -- registered as a fake provider so this doesn't depend on
any real network call or credentials.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp.sourcing import PROVIDER_REGISTRY, PartResult
from kicad_mcp.sourcing.registry import _dedupe
from kicad_mcp.toolsets import sourcing as sourcing_toolset


class _FakeProvider:
    name = "fake"

    def __init__(self, results: list[PartResult]) -> None:
        self._results = results

    def search(self, query: str, *, limit: int = 20) -> list[PartResult]:
        return self._results


class _FailingProvider:
    name = "flaky"

    def search(self, query: str, *, limit: int = 20) -> list[PartResult]:
        raise RuntimeError("provider unreachable")


@pytest.fixture(autouse=True)
def _clean_registry():
    # PROVIDER_REGISTRY is a module-level singleton -- isolate each test.
    original = dict(PROVIDER_REGISTRY._providers)
    PROVIDER_REGISTRY._providers.clear()
    yield
    PROVIDER_REGISTRY._providers.clear()
    PROVIDER_REGISTRY._providers.update(original)


@pytest.fixture(autouse=True)
def _clean_pending_matches():
    # sourcing_toolset._PENDING_MATCHES is module-level state -- isolate each test.
    sourcing_toolset._PENDING_MATCHES.clear()
    yield
    sourcing_toolset._PENDING_MATCHES.clear()


def _part(mpn: str, **kwargs) -> PartResult:
    return PartResult(manufacturer="Acme", mpn=mpn, provider=kwargs.pop("provider", "fake"), **kwargs)


def test_build_registers_expected_tools():
    mcp = MCPServer("test")
    names = sourcing_toolset.build(mcp)
    assert set(names) == {
        "search_parts",
        "list_sourcing_providers",
        "select_component",
        "propose_component_match",
        "confirm_component_match",
        "lookup_jlcpcb_part_tiers",
        "jlcpcb_library_status",
        "resolve_jlcpcb_assets",
        "fetch_datasheet",
        "inspect_datasheet",
        "fetch_part_assets",
        "generate_footprint",
        "list_package_types",
    }


def test_search_parts_merges_across_providers():
    PROVIDER_REGISTRY.register(_FakeProvider([_part("R1"), _part("R2")]))
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["search_parts"]("resistor")
    assert result["status"] == "ok"
    assert result["count"] == 2
    assert result["total_count"] == 2
    assert result["has_more"] is False


def test_search_parts_dedupes_same_manufacturer_and_mpn():
    PROVIDER_REGISTRY.register(_FakeProvider([_part("R1", datasheet_url=None)]))
    PROVIDER_REGISTRY.register(
        type("Other", (), {"name": "other", "search": lambda self, q, limit=20: [_part("R1", provider="other", datasheet_url="http://x/ds.pdf")]})()
    )
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["search_parts"]("resistor")
    assert result["count"] == 1
    assert result["items"][0]["datasheet_url"] == "http://x/ds.pdf"


def test_search_parts_paginates():
    PROVIDER_REGISTRY.register(_FakeProvider([_part(f"R{i}") for i in range(5)]))
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    page1 = tools["search_parts"]("resistor", limit=2, offset=0)
    assert page1["count"] == 2
    assert page1["has_more"] is True
    assert page1["next_offset"] == 2

    page2 = tools["search_parts"]("resistor", limit=2, offset=2)
    assert page2["count"] == 2
    assert page2["has_more"] is True


def test_search_parts_reports_provider_errors_without_crashing():
    PROVIDER_REGISTRY.register(_FakeProvider([_part("R1")]))
    PROVIDER_REGISTRY.register(_FailingProvider())
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["search_parts"]("resistor")
    assert result["status"] == "partial"
    assert result["count"] == 1
    assert any(e["provider"] == "flaky" for e in result["provider_errors"])


def test_search_parts_rejects_nonpositive_limit():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["search_parts"]("resistor", limit=0)
    assert result["status"] == "error"


def test_list_sourcing_providers():
    PROVIDER_REGISTRY.register(_FakeProvider([]))
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["list_sourcing_providers"]()
    assert result["count"] == 1
    assert result["providers"] == ["fake"]


def test_dedupe_merges_missing_fields_without_dropping_existing():
    a = _part("R1", lcsc_id="C1")
    b = _part("R1", provider="other")
    merged = _dedupe([a, b])
    assert len(merged) == 1
    assert merged[0].lcsc_id == "C1"


def test_propose_then_confirm_happy_path():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    candidates = [{"mpn": "R1"}, {"mpn": "R2"}]

    proposal = tools["propose_component_match"](candidates)
    assert proposal["status"] == "pending_confirmation"
    assert proposal["candidates"] == candidates
    token = proposal["revision_token"]

    result = tools["confirm_component_match"](token, 1)
    assert result["status"] == "confirmed"
    assert result["chosen"] == {"mpn": "R2"}


def test_confirm_with_out_of_range_index_keeps_proposal_pending():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    token = tools["propose_component_match"]([{"mpn": "R1"}])["revision_token"]

    bad = tools["confirm_component_match"](token, 5)
    assert bad["status"] == "error"

    # proposal should still be usable after an out-of-range attempt
    good = tools["confirm_component_match"](token, 0)
    assert good["status"] == "confirmed"


def test_confirm_with_unknown_token():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["confirm_component_match"]("does-not-exist", 0)
    assert result["status"] == "error"
    assert "revision_token" in result["reason"]


def test_confirming_same_token_twice_fails_second_time():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    token = tools["propose_component_match"]([{"mpn": "R1"}, {"mpn": "R2"}])["revision_token"]

    first = tools["confirm_component_match"](token, 0)
    assert first["status"] == "confirmed"

    second = tools["confirm_component_match"](token, 0)
    assert second["status"] == "error"


def test_propose_rejects_empty_candidates():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["propose_component_match"]([])
    assert result["status"] == "error"


def test_is_match_ambiguous_single_candidate_is_not_ambiguous():
    assert sourcing_toolset.is_match_ambiguous([_part("R1")]) is False


def test_is_match_ambiguous_true_on_incomplete_datasheet_inspection():
    assert sourcing_toolset.is_match_ambiguous(
        [_part("R1")], datasheet_inspection={"status": "incomplete", "reason": "no text"}
    ) is True


def test_is_match_ambiguous_true_for_multiple_distinct_parts_with_no_verification():
    # Fixed 2026-09-02 (archived backlog P1-16): stock quantity is not a real
    # disambiguating signal -- two different parts are ambiguous regardless
    # of which one happens to have more stock, until one is verified.
    a = _part("R1", stock_qty=10000)
    b = _part("R2", stock_qty=5)
    assert sourcing_toolset.is_match_ambiguous([a, b]) is True


def test_is_match_ambiguous_false_for_duplicate_listings_of_the_same_part():
    # Same (manufacturer, mpn) from two providers is not "ambiguous" -- that's
    # just search_parts' own dedup working as intended, not a real choice.
    a = _part("R1", provider="fake")
    b = _part("R1", provider="other")
    assert sourcing_toolset.is_match_ambiguous([a, b]) is False


def test_is_match_ambiguous_false_when_datasheet_verified_despite_multiple_candidates():
    a = _part("R1")
    b = _part("R2")
    verified = {"status": "ok", "pin_tables": [{"page": 1, "rows": []}]}
    assert sourcing_toolset.is_match_ambiguous([a, b], datasheet_inspection=verified) is False


def test_select_component_returns_directly_when_unambiguous():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    candidate = _part("R1").model_dump(mode="json")
    result = tools["select_component"]([candidate])
    assert result["status"] == "selected"
    assert result["chosen"] == candidate


def test_select_component_routes_through_checkpoint_when_ambiguous():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    candidates = [_part("R1").model_dump(mode="json"), _part("R2").model_dump(mode="json")]
    result = tools["select_component"](candidates)
    assert result["status"] == "pending_confirmation"
    assert "revision_token" in result

    confirmed = tools["confirm_component_match"](result["revision_token"], 1)
    assert confirmed["status"] == "confirmed"
    assert confirmed["chosen"]["mpn"] == "R2"


def test_select_component_rejects_malformed_candidates():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["select_component"]([{"not": "a part result"}])
    assert result["status"] == "error"


def test_select_component_rejects_empty_candidates():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["select_component"]([])
    assert result["status"] == "error"


# --- JLCPCB Basic-vs-Extended cost tiebreak (archived backlog P1-22) -------------
#
# The rule under test: cost is a tiebreaker between electrically-equivalent
# candidates, NEVER a reason to pick an electrically-worse part. These tests
# exist mostly to pin down where the cost signal is *not* allowed to act --
# regressing toward "cheapest wins" would repeat the mistake P1-16 removed.


def test_cost_preference_is_decisive_only_with_one_fee_exempt_and_rest_extended():
    basic = _part("R1", jlcpcb_class="basic", jlcpcb_basic=True)
    extended = _part("R2", jlcpcb_class="extended", jlcpcb_basic=False)
    pref = sourcing_toolset.jlcpcb_cost_preference([basic, extended])
    assert pref["decisive"] is True
    assert pref["chosen_index"] == 0
    assert "feeder loading fee" in pref["reason"]


def test_cost_preference_prefers_a_preferred_part_over_an_extended_one():
    # Preferred Extended is fee-exempt for Economic PCBA even though it is
    # not "Basic" -- jlcpcb_basic is False here and that must not disqualify it.
    preferred = _part("R1", jlcpcb_class="preferred", jlcpcb_basic=False)
    extended = _part("R2", jlcpcb_class="extended", jlcpcb_basic=False)
    pref = sourcing_toolset.jlcpcb_cost_preference([preferred, extended])
    assert pref["decisive"] is True
    assert pref["chosen_index"] == 0


def test_cost_preference_is_not_decisive_when_any_tier_is_unknown():
    basic = _part("R1", jlcpcb_class="basic", jlcpcb_basic=True)
    unknown = _part("R2")  # never looked up
    pref = sourcing_toolset.jlcpcb_cost_preference([basic, unknown])
    assert pref["decisive"] is False
    assert "unknown JLCPCB tier" in pref["reason"]


def test_cost_preference_is_not_decisive_when_two_candidates_are_fee_exempt():
    a = _part("R1", jlcpcb_class="basic", jlcpcb_basic=True)
    b = _part("R2", jlcpcb_class="preferred", jlcpcb_basic=False)
    pref = sourcing_toolset.jlcpcb_cost_preference([a, b])
    assert pref["decisive"] is False
    assert "no single fee-exempt candidate" in pref["reason"]


def test_cost_preference_ignores_duplicate_listings_of_the_same_part():
    a = _part("R1", jlcpcb_class="basic", jlcpcb_basic=True, provider="fake")
    dup = _part("R1", jlcpcb_class="basic", jlcpcb_basic=True, provider="other")
    pref = sourcing_toolset.jlcpcb_cost_preference([a, dup])
    assert pref["decisive"] is False
    assert pref["reason"] == "only one distinct candidate"


def test_basic_over_extended_resolves_an_otherwise_ambiguous_match():
    basic = _part("R1", jlcpcb_class="basic", jlcpcb_basic=True)
    extended = _part("R2", jlcpcb_class="extended", jlcpcb_basic=False)
    assert sourcing_toolset.is_match_ambiguous([basic, extended]) is False


def test_cost_does_not_resolve_ambiguity_when_a_tier_is_unknown():
    basic = _part("R1", jlcpcb_class="basic", jlcpcb_basic=True)
    unknown = _part("R2")
    assert sourcing_toolset.is_match_ambiguous([basic, unknown]) is True


def test_cost_never_overrides_an_inconclusive_datasheet_inspection():
    # Rule 1 beats rule 4: if we cannot tell whether a candidate even fits,
    # a price advantage is irrelevant.
    basic = _part("R1", jlcpcb_class="basic", jlcpcb_basic=True)
    extended = _part("R2", jlcpcb_class="extended", jlcpcb_basic=False)
    assert (
        sourcing_toolset.is_match_ambiguous(
            [basic, extended], datasheet_inspection={"status": "incomplete", "reason": "scanned pdf"}
        )
        is True
    )


def test_cost_never_displaces_a_datasheet_verified_leader():
    """The load-bearing guard: the verified leader is the *expensive* part
    here, and it must still win. Cost is a tiebreaker, not a veto on
    electrical evidence."""
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    verified_extended = _part("EXPENSIVE", jlcpcb_class="extended", jlcpcb_basic=False).model_dump(mode="json")
    cheap_basic = _part("CHEAP", jlcpcb_class="basic", jlcpcb_basic=True).model_dump(mode="json")
    result = tools["select_component"](
        [verified_extended, cheap_basic],
        datasheet_inspection={"status": "ok", "pin_tables": [{"page": 1, "rows": []}]},
    )
    assert result["status"] == "selected"
    assert result["chosen"]["mpn"] == "EXPENSIVE"
    assert result["chosen_index"] == 0
    assert result["selection_basis"] == "datasheet_verified"


def test_select_component_reports_why_a_cost_tiebreak_won():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    extended = _part("R_EXT", jlcpcb_class="extended", jlcpcb_basic=False).model_dump(mode="json")
    basic = _part("R_BASIC", jlcpcb_class="basic", jlcpcb_basic=True).model_dump(mode="json")
    # Deliberately not first in the list -- the cost winner must actually be
    # returned, not just candidates[0].
    result = tools["select_component"]([extended, basic])
    assert result["status"] == "selected"
    assert result["chosen"]["mpn"] == "R_BASIC"
    assert result["chosen_index"] == 1
    assert result["selection_basis"] == "jlcpcb_cost_tiebreak"
    assert "feeder loading fee" in result["reason"]
    assert result["jlcpcb_class"] == "basic"


def test_select_component_still_checkpoints_when_cost_cannot_decide():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    a = _part("R1", jlcpcb_class="basic", jlcpcb_basic=True).model_dump(mode="json")
    b = _part("R2", jlcpcb_class="basic", jlcpcb_basic=True).model_dump(mode="json")
    result = tools["select_component"]([a, b])
    assert result["status"] == "pending_confirmation"
    assert "reason" in result
    assert result["jlcpcb_cost_preference"]["decisive"] is False


def test_select_component_annotates_unknown_tiers_from_the_local_index():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    # C7955 (LM393DR2G) is Basic upstream; C2040 (RP2040) is Extended.
    basic = PartResult(manufacturer="onsemi", mpn="LM393DR2G", provider="fake", lcsc_id="C7955").model_dump(mode="json")
    extended = PartResult(manufacturer="RPi", mpn="RP2040", provider="fake", lcsc_id="C2040").model_dump(mode="json")
    result = tools["select_component"]([extended, basic])
    assert result["status"] == "selected"
    assert result["chosen"]["mpn"] == "LM393DR2G"
    assert result["selection_basis"] == "jlcpcb_cost_tiebreak"
    assert result["jlcpcb_lookup"]["status"] == "ok"


def test_select_component_annotation_can_be_switched_off():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    basic = PartResult(manufacturer="onsemi", mpn="LM393DR2G", provider="fake", lcsc_id="C7955").model_dump(mode="json")
    extended = PartResult(manufacturer="RPi", mpn="RP2040", provider="fake", lcsc_id="C2040").model_dump(mode="json")
    result = tools["select_component"]([extended, basic], annotate_jlcpcb=False)
    assert result["status"] == "pending_confirmation"


def test_search_parts_annotates_results_with_jlcpcb_tier():
    PROVIDER_REGISTRY.register(
        _FakeProvider([PartResult(manufacturer="onsemi", mpn="LM393DR2G", provider="fake", lcsc_id="C7955")])
    )
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["search_parts"]("comparator")
    assert result["items"][0]["jlcpcb_basic"] is True
    assert result["items"][0]["jlcpcb_class"] == "basic"
    assert result["jlcpcb_lookup"]["status"] == "ok"


def test_search_parts_leaves_unindexed_parts_unknown_not_extended():
    PROVIDER_REGISTRY.register(_FakeProvider([_part("NOT-A-REAL-PART")]))
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["search_parts"]("whatever")
    assert result["items"][0]["jlcpcb_basic"] is None
    assert result["items"][0]["jlcpcb_class"] is None
    # A gap in JLCPCB coverage is not a failure of the search itself.
    assert result["status"] == "ok"
    assert result["jlcpcb_lookup"]["status"] == "partial"


def test_lookup_jlcpcb_part_tiers_tool_is_batch():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["lookup_jlcpcb_part_tiers"](["C7955", "C2040", "C999999999"])
    assert result["count"] == 3
    assert result["status"] == "partial"
    assert result["summary"]["unknown"] == 1


def test_jlcpcb_library_status_tool_reports_index_availability():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    status = tools["jlcpcb_library_status"]()
    assert status["index_available"] is True
    assert status["license"] == "MIT"


def test_resolve_jlcpcb_assets_tool_is_batch_and_never_raises():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, sourcing_toolset)
    result = tools["resolve_jlcpcb_assets"](["C7955", "C999999999"])
    assert result["count"] == 2
    assert {r["source"] for r in result["results"]} <= {"jlcpcb_library", "easyeda2kicad"}


def _build_and_get_fns(mcp: MCPServer, module) -> dict:
    """Build the toolset onto a fresh server and return {tool_name: plain_fn},
    pulled from the tool manager's registered Tool objects (.name/.fn), so
    tests can call the underlying function directly (docs/TESTING.md level 1)
    without going through the async protocol layer.
    """
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
