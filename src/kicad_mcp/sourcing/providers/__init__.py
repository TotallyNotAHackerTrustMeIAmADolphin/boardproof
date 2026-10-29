"""Import this package to register every built-in sourcing provider.

Each provider module registers an instance into PROVIDER_REGISTRY at
import time (see kicad_mcp.sourcing.registry.SourcingProvider). Importing
kicad_mcp.sourcing.providers is therefore enough to make all of them
available to the sourcing toolset's search_parts -- wire this import in
wherever the server/toolset needs providers active (e.g. at the top of
toolsets/sourcing.py's build(), or in server.py's toolset registration --
left to whoever assembles the full sourcing toolset, since individual
provider tickets shouldn't need to touch that shared file).
"""

from kicad_mcp.sourcing import PROVIDER_REGISTRY
from kicad_mcp.sourcing.providers.lcsc import LCSCProvider

PROVIDER_REGISTRY.register(LCSCProvider())

__all__ = ["LCSCProvider"]
