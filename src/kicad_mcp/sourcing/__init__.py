"""Component sourcing: a common PartResult shape fed by multiple providers.

See docs/ARCHITECTURE.md "Component sourcing" and archived backlog P1-01. Providers
(Nexar, Digi-Key, LCSC-scrape, ...) register themselves into PROVIDER_REGISTRY
at import time; the `sourcing` MCP toolset (toolsets/sourcing.py) fans a
single search_parts call out across whatever's registered and merges/dedupes
the results, so the agent never has to call one tool per distributor.
"""

from kicad_mcp.sourcing.models import PartResult, PriceBreak
from kicad_mcp.sourcing.registry import ProviderRegistry, SourcingProvider

PROVIDER_REGISTRY = ProviderRegistry()

__all__ = ["PROVIDER_REGISTRY", "PartResult", "PriceBreak", "ProviderRegistry", "SourcingProvider"]
