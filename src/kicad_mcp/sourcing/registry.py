"""Provider registry for component sourcing.

Distinct from kicad_mcp.toolsets.registry.ToolsetRegistry (which manages
MCP tool visibility) -- this manages which sourcing providers (Nexar,
Digi-Key, LCSC-scrape, ...) a search_parts call fans out to. Providers
register themselves into the shared PROVIDER_REGISTRY singleton
(kicad_mcp.sourcing.PROVIDER_REGISTRY) at import time.
"""

from __future__ import annotations

from typing import Protocol

from kicad_mcp.sourcing.models import PartResult


class SourcingProvider(Protocol):
    name: str

    def search(self, query: str, *, limit: int = 20) -> list[PartResult]:
        """Return candidate parts for a free-text or parametric query.

        Implementations should raise on a genuine failure (network error,
        auth failure, ...) -- ProviderRegistry.search_all catches and
        isolates per-provider failures so one bad provider doesn't take
        down the whole search, per docs/ARCHITECTURE.md's evidence-gated
        verdicts principle: a partial result set is reported as partial,
        not silently presented as complete.
        """
        ...


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, SourcingProvider] = {}

    def register(self, provider: SourcingProvider) -> None:
        self._providers[provider.name] = provider

    def providers(self) -> list[SourcingProvider]:
        return list(self._providers.values())

    def search_all(self, query: str, *, limit: int = 20) -> tuple[list[PartResult], list[dict]]:
        """Fan a query out across every registered provider.

        Returns (merged_deduped_results, provider_errors) -- provider_errors
        is a list of {"provider": name, "error": str} for any provider that
        raised, so the caller can report a partial result honestly instead
        of silently dropping a failed provider's contribution.
        """
        results: list[PartResult] = []
        errors: list[dict] = []
        for provider in self._providers.values():
            try:
                results.extend(provider.search(query, limit=limit))
            except Exception as exc:  # noqa: BLE001 -- isolate provider failures deliberately
                errors.append({"provider": provider.name, "error": str(exc)})
        return _dedupe(results), errors


def _dedupe(results: list[PartResult]) -> list[PartResult]:
    """Merge duplicate (manufacturer, mpn) hits across providers.

    Keeps the first-seen result as the base and merges in any price
    breaks / stock info a later duplicate has that the first one lacked,
    rather than just dropping the duplicate outright.
    """
    merged: dict[tuple[str, str], PartResult] = {}
    for result in results:
        key = result.identity_key()
        if key not in merged:
            merged[key] = result
            continue
        existing = merged[key]
        if not existing.datasheet_url and result.datasheet_url:
            existing = existing.model_copy(update={"datasheet_url": result.datasheet_url})
        if not existing.lcsc_id and result.lcsc_id:
            existing = existing.model_copy(update={"lcsc_id": result.lcsc_id})
        if not existing.price_breaks and result.price_breaks:
            existing = existing.model_copy(update={"price_breaks": result.price_breaks})
        merged[key] = existing
    return list(merged.values())
