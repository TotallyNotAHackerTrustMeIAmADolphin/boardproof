"""LCSC scraping provider.

LCSC has no public search API (confirmed in archived research notes), so this reaches
its internal product-search endpoint directly with httpx rather than a
documented API contract. Every result is tagged source="scrape"
(kicad_mcp.sourcing.models.PartResult.source) so downstream tools/humans
can weight confidence lower than API-backed providers.

IMPORTANT FINDING (2026-09-02, verified live): LCSC's site sits behind
Akamai Bot Manager (the `ak_bmsc`/`bm_sv` cookies a plain visit receives
confirm this). A bare httpx client -- even one that first visits the
homepage/search page to pick up session cookies and sets a realistic
User-Agent/Referer/Origin -- consistently gets HTTP 200 responses whose
JSON body is `{"code": 500, "msg": "Something went wrong on our end...",
"result": null, "ok": false}` from the `wmsc.lcsc.com/product/search`
endpoint, rather than either a real result or an honest block page. This
looks like Akamai returning a sanitized "everything is fine, nothing to
see" error to non-browser clients rather than the real backend response.
Plain-HTTP scraping of LCSC search is therefore NOT reliably viable as
implemented -- a real fix likely needs a headless browser (Playwright)
to pass Akamai's fingerprinting, which is a materially bigger engineering
and maintenance commitment than a scrape provider was scoped for, and
raises ToS considerations worth a deliberate decision rather than a
silent default. Flagged back to the parent/archived backlog rather than solved
here (see this fork's final report).

The response-schema parsing below is therefore BEST-EFFORT / UNVERIFIED
against a real successful payload -- field names are inferred from public
knowledge of LCSC's JSON shape used by other open-source LCSC tooling
(productCode, productModel, brandNameEn, stockNumber, productPriceList,
pdfUrl), not confirmed against a live 200-with-data response, since none
was obtainable. Treat `_parse_result` as needing revalidation once a real
success payload is captured (e.g. after a Playwright-based fetch, or a
manually-captured browser HAR).
"""

from __future__ import annotations

import time

import httpx

from kicad_mcp.sourcing.models import PartResult

SEARCH_URL = "https://wmsc.lcsc.com/product/search"
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://www.lcsc.com/search",
    "Origin": "https://www.lcsc.com",
}


class LCSCSearchError(RuntimeError):
    """Raised when LCSC's endpoint doesn't return a parseable success payload.

    Caught by ProviderRegistry.search_all and surfaced as a provider_error
    in the sourcing toolset's response, per docs/ARCHITECTURE.md's evidence-gated
    principle -- a bot-walled scrape reports as a failure, never as an
    empty-but-technically-successful result.
    """


class LCSCProvider:
    name = "lcsc"

    def __init__(self, *, min_request_interval_s: float = 1.0) -> None:
        self._min_interval = min_request_interval_s
        self._last_request_at = 0.0

    def search(self, query: str, *, limit: int = 20) -> list[PartResult]:
        self._respect_rate_limit()
        response = httpx.get(
            SEARCH_URL,
            params={"keyword": query, "currentPage": 1, "pageSize": limit},
            headers=_DEFAULT_HEADERS,
            timeout=15,
            follow_redirects=True,
        )
        response.raise_for_status()
        payload = response.json()
        return self._parse_results(payload)

    def _respect_rate_limit(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_interval:
            time.sleep(self._min_interval - elapsed)
        self._last_request_at = time.monotonic()

    def _parse_results(self, payload: dict) -> list[PartResult]:
        code = payload.get("code")
        if code not in (200, 0):
            raise LCSCSearchError(
                f"LCSC search did not return success (code={code!r}, msg={payload.get('msg')!r}). "
                "This endpoint is currently unreliable behind Akamai bot protection for non-browser "
                "clients -- see the module docstring."
            )
        result = payload.get("result") or {}
        rows = result.get("productList") or result.get("dataList") or result.get("list") or []
        return [self._parse_row(row) for row in rows]

    @staticmethod
    def _parse_row(row: dict) -> PartResult:
        price_list = row.get("productPriceList") or []
        price_breaks = [
            {"quantity": pb.get("ladder", 1), "price_usd": pb.get("usdPrice") or pb.get("productPrice") or 0.0}
            for pb in price_list
            if pb.get("usdPrice") is not None or pb.get("productPrice") is not None
        ]
        return PartResult(
            manufacturer=row.get("brandNameEn") or row.get("brandName") or "Unknown",
            mpn=row.get("productModel") or row.get("productModelEn") or row.get("productCode", ""),
            provider="lcsc",
            source="scrape",
            distributor="LCSC",
            stock_qty=row.get("stockNumber") or row.get("stockCount"),
            price_breaks=price_breaks,
            datasheet_url=row.get("pdfUrl") or row.get("productPdfUrl"),
            lcsc_id=row.get("productCode"),
        )
