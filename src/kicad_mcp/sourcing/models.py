"""Common part-result shape shared by every sourcing provider.

See docs/ARCHITECTURE.md "Component sourcing": one shape for Nexar, Digi-Key,
LCSC-scrape, etc. so the sourcing toolset can merge/dedupe across
providers instead of exposing one tool per distributor.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PriceBreak(BaseModel):
    model_config = ConfigDict(extra="forbid")

    quantity: int = Field(gt=0)
    price_usd: float = Field(ge=0)


class PartResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    manufacturer: str
    mpn: str
    provider: str
    """Which provider produced this result, e.g. "nexar", "digikey", "lcsc"."""

    source: Literal["api", "scrape"] = "api"
    """API-backed results are higher-trust than scraped ones -- surfaced
    explicitly so downstream tools/humans can weight confidence."""

    distributor: str | None = None
    stock_qty: int | None = None
    price_breaks: list[PriceBreak] = Field(default_factory=list)
    datasheet_url: str | None = None
    lcsc_id: str | None = None
    """LCSC part number, when known -- the key easyeda2kicad.py needs for
    automated asset retrieval (see archived backlog P1-05)."""

    jlcpcb_basic: bool | None = None
    """Is this a JLCPCB **Basic** part? (archived backlog P1-22.)

    Three-valued on purpose, and the three values are NOT interchangeable:

    - ``True``  -- confirmed Basic: JLCPCB keeps it loaded on the pick-and-place
      machines, so it never attracts a per-part feeder loading fee.
    - ``False`` -- confirmed **not** Basic. Note this covers *both* Preferred
      Extended (fee-exempt for Economic PCBA) and plain Extended ($3/part), so
      ``False`` alone does not mean "this will cost you a setup fee" -- read
      ``jlcpcb_class`` for that.
    - ``None``  -- **not looked up**. No claim is being made either way.

    Never collapse ``None`` into ``False``: an agent has to be able to tell "I
    checked and it is not Basic" from "nobody ever checked".
    """

    jlcpcb_class: Literal["basic", "preferred", "extended"] | None = None
    """JLCPCB's actual part tier, or ``None`` when not looked up.

    Added during P1-22 because the ticket's assumed two-way Basic/Extended
    split is out of date. Verified 2026-09-04 against JLCPCB's own PCB
    Assembly FAQ (https://jlcpcb.com/help/article/pcb-assembly-faqs), which
    documents three tiers: Basic ("no labor cost for feeder setup"),
    Preferred Extended ("exempt from the Feeders Loading fee for Economic PCB
    assembly"), and Extended ("$3 per extended component"). A bool cannot
    express that -- ``jlcpcb_basic=False`` on a Preferred part would read as
    "this costs a setup fee", which is wrong for Economic PCBA -- so the tier
    travels alongside it rather than replacing it.

    Populated by ``kicad_mcp.sourcing.jlcpcb_basic``.
    """

    def identity_key(self) -> tuple[str, str]:
        """Case-insensitive (manufacturer, mpn) key used to dedupe results
        that multiple providers return for the same real-world part."""
        return (self.manufacturer.strip().lower(), self.mpn.strip().lower())
