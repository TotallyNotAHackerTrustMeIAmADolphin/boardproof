"""Tests for the LCSC scraping provider (archived backlog P1-04).

Per docs/TESTING.md, the default `pytest` run must not depend on live
scraping. The failure-path fixture below is REAL, captured live on
2026-09-02 -- LCSC's `wmsc.lcsc.com/product/search` endpoint returns this
exact body to a plain httpx client even with realistic
headers/session-cookies, which is strong evidence of Akamai bot-walling
non-browser clients (see the module docstring in
kicad_mcp/sourcing/providers/lcsc.py for the full finding). The
success-path fixture is NOT verified against a real response (none was
obtainable) and is marked as such -- it exists to pin down the intended
parsing behavior once a real payload is captured.
"""

from __future__ import annotations

import httpx
import pytest

from kicad_mcp.sourcing.providers.lcsc import LCSCProvider, LCSCSearchError

# Captured live 2026-09-02 against https://wmsc.lcsc.com/product/search --
# real, verified response body (see module docstring for the request that
# produced it).
REAL_CAPTURED_FAILURE_RESPONSE = {
    "code": 500,
    "msg": "Something went wrong on our end. Please try again or contact support.",
    "result": None,
    "ok": False,
}

# UNVERIFIED best-effort success shape -- see module docstring. Field names
# inferred from public knowledge of LCSC's JSON, not confirmed live.
ASSUMED_SUCCESS_RESPONSE = {
    "code": 200,
    "result": {
        "productList": [
            {
                "productCode": "C123456",
                "productModel": "ATTINY1616-MFR",
                "brandNameEn": "Microchip",
                "stockNumber": 4200,
                "productPriceList": [
                    {"ladder": 1, "usdPrice": 1.23},
                    {"ladder": 100, "usdPrice": 0.98},
                ],
                "pdfUrl": "https://example.com/attiny1616.pdf",
            }
        ]
    },
    "ok": True,
}


def _mock_get(response_json: dict):
    def fake_get(url, *, params=None, headers=None, timeout=None, follow_redirects=None):
        return httpx.Response(200, json=response_json, request=httpx.Request("GET", url))

    return fake_get


def test_parses_assumed_success_shape(monkeypatch):
    monkeypatch.setattr("kicad_mcp.sourcing.providers.lcsc.httpx.get", _mock_get(ASSUMED_SUCCESS_RESPONSE))
    provider = LCSCProvider(min_request_interval_s=0)
    results = provider.search("ATtiny1616")
    assert len(results) == 1
    part = results[0]
    assert part.manufacturer == "Microchip"
    assert part.mpn == "ATTINY1616-MFR"
    assert part.lcsc_id == "C123456"
    assert part.source == "scrape"
    assert part.provider == "lcsc"
    assert part.stock_qty == 4200
    assert len(part.price_breaks) == 2


def test_real_captured_failure_response_raises_clear_error(monkeypatch):
    """Regression test pinned to the ACTUAL response LCSC's endpoint gives
    a non-browser client -- see REAL_CAPTURED_FAILURE_RESPONSE. Confirms
    the provider fails loudly (LCSCSearchError, caught upstream by
    ProviderRegistry.search_all) rather than silently returning an empty
    list that would look like "no matching parts" instead of "blocked".
    """
    monkeypatch.setattr("kicad_mcp.sourcing.providers.lcsc.httpx.get", _mock_get(REAL_CAPTURED_FAILURE_RESPONSE))
    provider = LCSCProvider(min_request_interval_s=0)
    with pytest.raises(LCSCSearchError):
        provider.search("ATtiny1616")


def test_empty_product_list_returns_empty_not_error(monkeypatch):
    monkeypatch.setattr(
        "kicad_mcp.sourcing.providers.lcsc.httpx.get",
        _mock_get({"code": 200, "result": {"productList": []}, "ok": True}),
    )
    provider = LCSCProvider(min_request_interval_s=0)
    assert provider.search("no such part anywhere") == []


def test_rate_limit_sleeps_between_calls(monkeypatch):
    sleep_calls: list[float] = []
    monkeypatch.setattr("kicad_mcp.sourcing.providers.lcsc.httpx.get", _mock_get({"code": 200, "result": {}, "ok": True}))
    monkeypatch.setattr("kicad_mcp.sourcing.providers.lcsc.time.sleep", lambda s: sleep_calls.append(s))
    provider = LCSCProvider(min_request_interval_s=5.0)
    provider.search("a")
    provider.search("b")
    assert sleep_calls, "second call within the rate-limit window should have slept"


def test_registers_into_provider_registry():
    import kicad_mcp.sourcing.providers as providers_pkg  # noqa: F401 -- import triggers registration
    from kicad_mcp.sourcing import PROVIDER_REGISTRY

    assert any(p.name == "lcsc" for p in PROVIDER_REGISTRY.providers())


@pytest.mark.skip(
    reason=(
        "Hits the real, live LCSC endpoint -- excluded from the default run per docs/TESTING.md "
        "(flaky/rate-limited/bot-walled by nature). Remove the skip and run manually to "
        "re-verify against reality; as of 2026-09-02 this endpoint returns HTTP 500 JSON to "
        "plain httpx clients (see module docstring) so this test is currently expected to fail "
        "until a browser-capable fetch path exists."
    )
)
def test_live_lcsc_search_manual():
    provider = LCSCProvider()
    results = provider.search("ATtiny1616")
    assert results
