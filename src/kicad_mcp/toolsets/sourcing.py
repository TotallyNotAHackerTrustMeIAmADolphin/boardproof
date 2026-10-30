"""Component sourcing toolset: search_parts fans out across every
registered provider (Nexar, Digi-Key, LCSC-scrape, ...) and returns one
merged, deduped, paginated list -- see docs/ARCHITECTURE.md "Component
sourcing" and archived backlog P1-01.

Candidate selection is also cost-aware (archived backlog P1-22): JLCPCB charges a
per-part feeder loading fee for Extended parts, so a fee-exempt part is
preferred when a design has a real choice. That signal is strictly
subordinate to electrical fit -- see `is_match_ambiguous` for the exact
ordering.

Also implements the ambiguous-match human checkpoint (archived backlog P1-09):
propose_component_match/confirm_component_match follow the dry-run +
revision-token pattern from docs/ARCHITECTURE.md's "Human-in-the-loop" section
-- a proposal is computed and returned with a token, and only takes
effect once that exact token is re-submitted, so a stale or duplicate
confirmation can't silently re-apply.
"""

from __future__ import annotations

import time
import uuid
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator

from kicad_mcp.sourcing import PROVIDER_REGISTRY, PartResult
from kicad_mcp.sourcing import jlcpcb_basic as jlc
from kicad_mcp.sourcing.asset_fetch import fetch_part_assets as _fetch_part_assets
from kicad_mcp.sourcing.datasheet import fetch_datasheet as _fetch_datasheet
from kicad_mcp.sourcing.datasheet import inspect_datasheet as _inspect_datasheet
from kicad_mcp.sourcing.footprint_gen import generate_footprint as _generate_footprint
from kicad_mcp.sourcing.footprint_gen import list_package_types as _list_package_types

# Importing this package registers every available provider (LCSCProvider,
# etc.) into PROVIDER_REGISTRY as a side effect of import -- without this
# import, search_parts silently has zero providers and always returns
# no_results, no matter how the providers themselves are implemented.
# (Found by an independent review, 2026-09-02: verified live that
# build_server() -> PROVIDER_REGISTRY.providers() == [] without this line.)
import kicad_mcp.sourcing.providers  # noqa: F401

# Per the mcp-builder guidance folded into docs/ARCHITECTURE.md: cap response
# size explicitly rather than either truncating silently or blowing the
# caller's context budget.
CHARACTER_LIMIT = 25_000

# Module-level state for pending match proposals, keyed by revision token.
# Phase 1 scope: in-memory is fine, no persistence layer needed yet -- a
# proposal that outlives the server process isn't a requirement here.
# Bounded + TTL'd (fixed 2026-09-02 per archived backlog P1-19, independent
# review: this was an unbounded process-global dict with no eviction --
# a long-running server that never got every proposal confirmed would
# leak memory indefinitely). Each entry is (candidates, expires_at).
_PENDING_MATCHES: dict[str, tuple[list[dict], float]] = {}
_PENDING_MATCH_TTL_SECONDS = 60 * 60  # 1 hour -- generous for a human/agent to confirm
_PENDING_MATCH_MAX_ENTRIES = 1000


PackageType = Literal["chip", "dip", "pin_header", "qfn", "qfp", "soic", "sot"]

# The keyword arguments each kicad_tools generator takes (kicad_tools 0.24.0,
# ``kicad_tools.library.generators.create_<type>``), the required ones first.
# test_generate_footprint_mcp.py checks this table against the generators' real
# signatures, so a kicad_tools upgrade that changes one fails a test, not a call.
PACKAGE_PARAMETERS: dict[str, tuple[str, ...]] = {
    "chip": ("size", "prefix", "metric", "name"),
    "dip": ("pins", "pitch", "row_spacing", "pad_diameter", "drill", "name"),
    "pin_header": ("pins", "rows", "pitch", "pad_diameter", "drill", "name"),
    "qfn": ("pins", "pitch", "body_size", "pad_width", "pad_height", "exposed_pad", "name"),
    "qfp": (
        "pins", "pitch", "body_size", "body_width", "body_length", "pad_width", "pad_height",
        "pins_x", "pins_y", "name",
    ),
    "soic": ("pins", "pitch", "body_width", "body_length", "pad_width", "pad_height", "name"),
    "sot": ("variant", "pins", "pitch", "body_width", "body_length", "name"),
}
REQUIRED_PACKAGE_PARAMETERS: dict[str, tuple[str, ...]] = {
    "chip": ("size",), "dip": ("pins",), "pin_header": ("pins",), "qfn": ("pins",),
    "qfp": ("pins",), "soic": ("pins",), "sot": (),
}

_Mm = Annotated[float, Field(gt=0)]


class FootprintParams(BaseModel):
    """The generator parameters of ``generate_footprint``. A name not listed here is
    refused by name (``extra="forbid"``); a listed one the package type does not take
    is refused by the tool. Lengths are millimetres."""

    model_config = ConfigDict(extra="forbid")

    # Every default or example stated below is checked against kicad_tools 0.24.0 by
    # test_generate_footprint_mcp.py (STATED and the size/variant lists).
    size: str | None = Field(
        None, description="chip: imperial size code, one of 0201, 0402, 0603, 0805, 1206, 1210, 1812, 2010, 2512."
    )
    prefix: str | None = Field(None, description="chip: reference prefix for the generated name, e.g. 'R', 'C', 'L'.")
    metric: bool | None = Field(
        None,
        description="chip: generated name without the imperial code, e.g. 'C_1005Metric' instead of "
        "'C_0402_1005Metric'. Refused together with name.",
    )
    variant: str | None = Field(
        None,
        description="sot: standard variant, one of SOT-23, SOT-23-5, SOT-23-6, SOT-223, SOT-89. It fixes pins, "
        "pitch, body_width and body_length, which are refused with it.",
    )
    pins: int | None = Field(
        None,
        ge=1,
        description="Pin count; required for dip, pin_header, qfn, qfp, soic and for sot without variant. "
        "qfn: without the exposed pad. sot without variant: 3 only (no other custom layout).",
    )
    rows: int | None = Field(None, ge=1, le=2, description="pin_header: 1 or 2 rows (default 1).")
    pitch: _Mm | None = Field(
        None,
        description="Pin pitch, mm. If omitted, the generator's default for the package and pin count "
        "(pin_header 2.54). sot: required without variant.",
    )
    body_size: _Mm | None = Field(
        None,
        description="qfn, qfp: body edge of a square package, mm. If omitted: qfn 4.0 mm; qfp the standard body "
        "for the pin count, 10.0 mm when there is none. qfp: refused together with body_width or body_length.",
    )
    body_width: _Mm | None = Field(
        None,
        description="qfp, soic, sot: body width, mm. If omitted, the generator's default for the pin count. "
        "sot: required without variant.",
    )
    body_length: _Mm | None = Field(
        None,
        description="qfp, soic, sot: body length, mm. If omitted: qfp equal to body_width; soic the generator's "
        "default for the pin count. sot: required without variant.",
    )
    pad_width: _Mm | None = Field(
        None, description="qfn, qfp, soic: pad length along the pin, mm. If omitted, the generator's default."
    )
    pad_height: _Mm | None = Field(
        None, description="qfn, qfp, soic: pad size across the pin, mm. If omitted, the generator's default."
    )
    pins_x: int | None = Field(
        None, ge=1, description="qfp: pins per horizontal side of a rectangular package; give pins_y with it."
    )
    pins_y: int | None = Field(
        None, ge=1, description="qfp: pins per vertical side of a rectangular package; give pins_x with it."
    )
    exposed_pad: _Mm | None = Field(
        None,
        description="qfn: edge of the centre thermal pad, mm. If omitted, the standard pad for pins and body_size "
        "where one exists (e.g. 16 pins on 3.0 mm: 1.7 mm), none when body_size is omitted or has no standard.",
    )
    row_spacing: _Mm | None = Field(
        None, description="dip: row distance, mm. If omitted, 7.62 mm up to 22 pins and 15.24 mm above."
    )
    pad_diameter: _Mm | None = Field(
        None, description="dip, pin_header: through-hole pad diameter, mm. If omitted: dip 1.6 mm, pin_header 1.7 mm."
    )
    drill: _Mm | None = Field(
        None, description="dip, pin_header: drill diameter, mm. If omitted: dip 0.8 mm, pin_header 1.0 mm."
    )
    name: str | None = Field(
        None, description="Footprint name, one line, no '\"' or '\\'; generated from the parameters if omitted."
    )


# Combinations where the kicad_tools 0.24.0 generator uses one parameter and silently drops
# others (read from create_sot, create_qfp, create_chip; build ticket 69 review round 1):
# package_type -> {parameter that wins: parameters it drops}.
OVERRIDING_PARAMETERS: dict[str, dict[str, tuple[str, ...]]] = {
    "chip": {"name": ("metric",)},
    "qfp": {"body_size": ("body_width", "body_length")},
    "sot": {"variant": ("pins", "pitch", "body_width", "body_length")},
}
# create_sot without variant raises unless all four custom dimensions are given.
_SOT_CUSTOM = ("pins", "pitch", "body_width", "body_length")


def _footprint_kwargs(package_type: str, params: FootprintParams) -> dict | str:
    """The generator keyword arguments, or why the parameters do not fit the package type."""
    given = params.model_dump(exclude_none=True)
    foreign = sorted(set(given) - set(PACKAGE_PARAMETERS[package_type]))
    if foreign:
        return (
            f"package_type '{package_type}' does not take {', '.join(foreign)}; "
            f"it takes {', '.join(PACKAGE_PARAMETERS[package_type])}"
        )
    missing = [name for name in REQUIRED_PACKAGE_PARAMETERS[package_type] if name not in given]
    if missing:
        return f"package_type '{package_type}' needs {', '.join(missing)}"
    for winner, losers in OVERRIDING_PARAMETERS.get(package_type, {}).items():
        dropped = sorted(name for name in losers if name in given)
        if winner in given and dropped:
            return (
                f"with {winner}, {package_type} ignores {', '.join(dropped)}: give {winner} alone or "
                f"leave it out"
            )
    if package_type == "sot" and "variant" not in given:
        absent = sorted(name for name in _SOT_CUSTOM if name not in given)
        if absent:
            return f"without variant, sot needs {', '.join(absent)} (or give variant alone)"
    return given


def _evict_stale_pending_matches() -> None:
    now = time.time()
    expired = [token for token, (_, expires_at) in _PENDING_MATCHES.items() if expires_at <= now]
    for token in expired:
        del _PENDING_MATCHES[token]
    if len(_PENDING_MATCHES) > _PENDING_MATCH_MAX_ENTRIES:
        # Evict oldest-expiring entries first once over the cap, rather than
        # growing unbounded under sustained load with no TTL expiry yet.
        oldest_first = sorted(_PENDING_MATCHES.items(), key=lambda kv: kv[1][1])
        overflow = len(_PENDING_MATCHES) - _PENDING_MATCH_MAX_ENTRIES
        for token, _ in oldest_first[:overflow]:
            del _PENDING_MATCHES[token]


def jlcpcb_cost_preference(candidates: list[PartResult]) -> dict:
    """Is there a *decisive* JLCPCB-cost reason to prefer one candidate?

    JLCPCB's per-part feeder loading fee applies to Extended parts only
    (Basic: never; Preferred Extended: exempt on Economic PCBA) -- see
    `kicad_mcp.sourcing.jlcpcb_basic` for the verified tier definitions. So
    when the electrically-plausible candidates differ only in tier, picking
    the fee-exempt one is free money.

    This is deliberately conservative, and only calls the choice decisive
    when **exactly one** distinct candidate is confirmed fee-exempt and
    **every** other distinct candidate is confirmed Extended. If any
    candidate's tier is unknown (`jlcpcb_class is None` -- never looked up,
    or not in the local index), cost proves nothing and the answer is "not
    decisive": an unknown part is not evidence of a cheaper part, and this
    must not become a back door to picking whichever candidate happened to
    get annotated.

    Returns a dict with `decisive`, `chosen_index` (index into the ORIGINAL
    `candidates` list), `reason`, and the per-candidate `tiers` that led to
    it, so the reasoning is auditable rather than a bare verdict.
    """
    tiers = [
        {
            "index": i,
            "mpn": c.mpn,
            "manufacturer": c.manufacturer,
            "jlcpcb_class": c.jlcpcb_class,
            "jlcpcb_basic": c.jlcpcb_basic,
        }
        for i, c in enumerate(candidates)
    ]
    # Collapse duplicate listings of the same real part (the same dedup
    # notion is_match_ambiguous uses) -- two providers listing one part is
    # not a choice between parts.
    first_by_identity: dict[tuple[str, str], int] = {}
    for i, c in enumerate(candidates):
        first_by_identity.setdefault(c.identity_key(), i)
    distinct_indices = sorted(first_by_identity.values())

    if len(distinct_indices) <= 1:
        return {"decisive": False, "chosen_index": None, "reason": "only one distinct candidate", "tiers": tiers}

    unknown = [i for i in distinct_indices if candidates[i].jlcpcb_class is None]
    if unknown:
        return {
            "decisive": False,
            "chosen_index": None,
            "reason": (
                f"{len(unknown)} of {len(distinct_indices)} distinct candidates have an unknown "
                "JLCPCB tier; an unknown tier is not evidence of a cheaper part, so cost cannot "
                "break this tie. Run lookup_jlcpcb_part_tiers on the candidates first."
            ),
            "tiers": tiers,
        }

    fee_exempt = [i for i in distinct_indices if candidates[i].jlcpcb_class in jlc.NO_SETUP_FEE_TIERS]
    if len(fee_exempt) != 1:
        return {
            "decisive": False,
            "chosen_index": None,
            "reason": (
                "no single fee-exempt candidate: "
                f"{len(fee_exempt)} of {len(distinct_indices)} distinct candidates are "
                "JLCPCB Basic/Preferred, so cost does not single one out"
            ),
            "tiers": tiers,
        }

    chosen = fee_exempt[0]
    others = [i for i in distinct_indices if i != chosen]
    return {
        "decisive": True,
        "chosen_index": chosen,
        "reason": (
            f"candidate {chosen} ({candidates[chosen].mpn}) is JLCPCB "
            f"{candidates[chosen].jlcpcb_class}, which carries no per-part feeder loading fee; "
            f"the other {len(others)} distinct candidate(s) are confirmed Extended "
            "($3/part setup). Cost breaks the tie only because no electrical evidence "
            "distinguished them."
        ),
        "tiers": tiers,
    }


def is_match_ambiguous(candidates: list[PartResult], datasheet_inspection: dict | None = None) -> bool:
    """True when there's no single clear best match among candidates, or
    when a datasheet-inspection result was inconclusive.

    **Ordering (electrical fit first, cost only as a tiebreaker)** -- the
    rules below are applied in this exact order, and cost never gets to
    overrule anything above it (archived backlog P1-22, guarding the lesson of
    P1-16: a cheap-wins heuristic that ignores whether the part is *right*
    is the same class of mistake as the stock-quantity heuristic that was
    removed):

    1. An inconclusive datasheet inspection -> ambiguous, full stop. Cost is
       irrelevant when we cannot tell whether a candidate even fits.
    2. One distinct candidate -> unambiguous.
    3. A datasheet inspection with status "ok" verified the leading
       candidate -> unambiguous, and that leader stands. Cost is NOT allowed
       to reorder past a datasheet-verified choice; a cheaper part that was
       never verified is not a better part.
    4. Only if none of the above resolved it: a decisive JLCPCB cost
       preference (exactly one confirmed fee-exempt candidate, all others
       confirmed Extended) -> unambiguous.
    5. Otherwise -> ambiguous, route to the human/agent checkpoint.

    Fixed 2026-09-02 per archived backlog P1-16 (independent review): the
    original heuristic treated "one candidate has more stock than the
    others" as a clear winner, which is electrically meaningless -- stock
    quantity says nothing about whether a part is the right match. This
    version instead follows the sequence `.claude/skills/kicad-sourcing`
    already documents as correct: multiple *distinct* (manufacturer, mpn)
    candidates are ambiguous by default, UNLESS a datasheet_inspection with
    status "ok" is supplied -- meaning the leading candidate has actually
    been verified against its real datasheet, per the skill's step 3, in
    which case other search hits for different parts don't make the
    already-verified choice ambiguous. Duplicate listings of the *same*
    (manufacturer, mpn) across providers/distributors are never ambiguous
    on their own (that's just search_parts' own dedup working as intended).
    """
    if datasheet_inspection is not None and datasheet_inspection.get("status") == "incomplete":
        return True
    distinct = {c.identity_key() for c in candidates}
    if len(distinct) <= 1:
        return False
    if datasheet_inspection is not None and datasheet_inspection.get("status") == "ok":
        return False
    if jlcpcb_cost_preference(candidates)["decisive"]:
        return False
    return True


_FOOTPRINT_TOP_LEVEL = ("package_type", "output_path", "params")


def _refuse_extra_arguments(mcp: MCPServer, tool_name: str, allowed: tuple[str, ...]) -> None:
    """Make one registered tool refuse, by name, any argument its signature does not
    name. The SDK's argument model ignores extras, so a caller's top-level ``prefix`` was
    dropped and the call came back ok (build ticket 69, fix round 2). Scoped to one tool:
    a server-wide ``extra="forbid"`` is a separate ticket and would use this same seam,
    the tool's ``fn_metadata.arg_model`` (the SDK re-reads it on every call).
    """
    tool = mcp._tool_manager.get_tool(tool_name)  # noqa: SLF001 - the SDK has no public hook
    if tool is None:
        raise RuntimeError(f"{tool_name} is not registered")

    def _check(_cls: type, data: object) -> object:
        if isinstance(data, dict):
            extra = sorted(str(k) for k in data if k not in allowed)
            if extra:
                raise ValueError(
                    f"unknown argument(s) {', '.join(extra)}: {tool_name} takes only {', '.join(allowed)}; "
                    "generator parameters belong inside params; nothing was written"
                )
        return data

    strict = create_model(
        tool.fn_metadata.arg_model.__name__,
        __base__=tool.fn_metadata.arg_model,
        __config__=ConfigDict(extra="forbid"),
        __validators__={"_refuse_extras": model_validator(mode="before")(classmethod(_check))},
    )
    tool.fn_metadata.arg_model = strict


_SEARCH_FALLBACK = {
    "tool": "lookup_jlcpcb_part_tiers",
    "note": (
        "Fall back to the local JLCPCB tier index (lookup_jlcpcb_part_tiers, jlcpcb_library_status) "
        "for Basic/Preferred/Extended and stock facts, and retry search_parts later."
    ),
}


def build(mcp: MCPServer) -> list[str]:
    def search_parts(query: str, limit: int = 20, offset: int = 0, annotate_jlcpcb: bool = True) -> dict:
        """Search every registered component-sourcing provider for parts
        matching a query, merged and deduped by (manufacturer, mpn) into
        one paginated list -- never call this once per distributor.

        Results are stamped with JLCPCB's part tier (`jlcpcb_basic` /
        `jlcpcb_class`) from a local index, so the caller can see which
        candidates avoid JLCPCB's per-part feeder loading fee without a
        second round-trip. A part missing from the index gets a null tier
        (unknown), never a false "Extended". Pass annotate_jlcpcb=False to
        skip it.
        """
        if limit <= 0:
            return {"status": "error", "reason": "limit must be > 0"}

        try:
            all_results, provider_errors = PROVIDER_REGISTRY.search_all(query, limit=limit + offset)
        except Exception as exc:  # noqa: BLE001 -- evidence-gated: report, don't crash the tool call
            return {"status": "incomplete", "reason": str(exc)}

        total_count = len(all_results)
        page = all_results[offset : offset + limit]
        jlcpcb_report: dict | None = None
        if annotate_jlcpcb and page:
            page, jlcpcb_report = jlc.annotate_part_results(page)
        items = [r.model_dump(mode="json") for r in page]

        response: dict = {
            "status": "ok" if page else "no_results",
            "total_count": total_count,
            "count": len(page),
            "offset": offset,
            "items": items,
            "has_more": offset + len(page) < total_count,
            "next_offset": offset + len(page) if offset + len(page) < total_count else None,
        }
        provider_count = len(PROVIDER_REGISTRY.providers())
        if provider_errors or provider_count == 0:
            response["fallback"] = dict(_SEARCH_FALLBACK)
        if provider_errors:
            # A provider failure is never a "no match": report the gap.
            response["provider_errors"] = provider_errors
            if len(provider_errors) >= provider_count and not page:
                response["status"] = "provider_error"
                response["reason"] = (
                    "Every provider failed, so zero items is not evidence the part does not exist. "
                    + _SEARCH_FALLBACK["note"]
                )
            else:
                response["status"] = "partial"
        elif provider_count == 0:
            response["status"] = "provider_error"
            response["reason"] = (
                "No sourcing provider is registered, so zero items is not evidence the part "
                "does not exist. " + _SEARCH_FALLBACK["note"]
            )
        if jlcpcb_report is not None:
            # Surfaced, not folded into `status`: an unindexed part is a gap
            # in JLCPCB tier coverage, not a failure of the search itself.
            response["jlcpcb_lookup"] = jlcpcb_report

        serialized_len = len(str(response))
        if serialized_len > CHARACTER_LIMIT:
            half = max(1, len(response["items"]) // 2)
            response["items"] = response["items"][:half]
            response["count"] = len(response["items"])
            response["truncated"] = True
            response["truncation_message"] = (
                f"Response truncated to {half} items to stay under the "
                f"{CHARACTER_LIMIT}-character budget. Use 'offset' to page "
                "through the rest, or narrow 'query'."
            )
        return response

    def list_sourcing_providers() -> dict:
        """List which component-sourcing providers are currently registered."""
        providers = PROVIDER_REGISTRY.providers()
        return {"count": len(providers), "providers": [p.name for p in providers]}

    def select_component(
        candidates: list[dict],
        datasheet_inspection: dict | None = None,
        annotate_jlcpcb: bool = True,
    ) -> dict:
        """Pick one component from a set of candidates, routing through the
        human/agent confirmation checkpoint automatically when the match is
        ambiguous rather than requiring the caller to separately remember
        to check is_match_ambiguous and call propose_component_match itself.

        `candidates` should be part-result-shaped dicts (as returned in
        search_parts' "items"). `datasheet_inspection` is the optional
        result of inspect_datasheet on the leading candidate -- pass it in
        once verified, per the sequence .claude/skills/kicad-sourcing
        documents, and an unambiguous single distinct candidate will be
        returned directly without waiting for a datasheet check.

        Selection order is electrical fit first, cost strictly second: a
        datasheet-verified leader is never displaced by a cheaper candidate.
        JLCPCB Basic/Preferred parts are preferred over Extended ones only
        when nothing electrical distinguished the candidates -- see
        `is_match_ambiguous` for the full ordering and `jlcpcb_cost_preference`
        for when cost counts as decisive. Set `annotate_jlcpcb=False` to skip
        the tier lookup entirely.

        Returns either {"status": "selected", "chosen": <candidate>,
        "chosen_index": i, "reason": <why this one won>} (unambiguous --
        nothing further needed) or
        {"status": "pending_confirmation", "revision_token": ..., "candidates": [...]}
        (ambiguous -- call confirm_component_match with the token once a
        choice is made, per docs/ARCHITECTURE.md's dry-run + revision-token
        pattern).
        """
        if not candidates:
            return {"status": "error", "reason": "candidates must be a non-empty list"}
        try:
            parsed = [PartResult(**c) for c in candidates]
        except Exception as exc:  # noqa: BLE001 -- evidence-gated: malformed input is reported, not raised
            return {"status": "error", "reason": f"candidates must be part-result-shaped: {exc}"}

        jlcpcb_report: dict | None = None
        if annotate_jlcpcb and any(c.jlcpcb_class is None for c in parsed):
            parsed, jlcpcb_report = jlc.annotate_part_results(parsed)

        if is_match_ambiguous(parsed, datasheet_inspection):
            proposal = propose_component_match(candidates)
            proposal["reason"] = (
                "more than one distinct plausible part, and neither a verified datasheet "
                "inspection nor a decisive JLCPCB cost difference singled one out"
            )
            # Always attached, not only when a lookup ran: the caller needs to
            # see *why* cost failed to decide (unknown tiers? two equally cheap
            # candidates?) to know whether looking tiers up would even help.
            proposal["jlcpcb_cost_preference"] = jlcpcb_cost_preference(parsed)
            if jlcpcb_report is not None:
                proposal["jlcpcb_lookup"] = jlcpcb_report
            return proposal

        # Unambiguous. Work out *which* one won and on what evidence -- the
        # electrical leader (candidates[0]) unless cost was the only thing
        # that resolved it.
        distinct = {c.identity_key() for c in parsed}
        chosen_index = 0
        if len(distinct) <= 1:
            selection_basis = "single_distinct_candidate"
            reason = "only one distinct part among the candidates"
        elif datasheet_inspection is not None and datasheet_inspection.get("status") == "ok":
            selection_basis = "datasheet_verified"
            reason = (
                "the leading candidate was verified against its datasheet; cost is not allowed "
                "to displace a verified electrical match"
            )
        else:
            preference = jlcpcb_cost_preference(parsed)
            chosen_index = preference["chosen_index"] or 0
            selection_basis = "jlcpcb_cost_tiebreak"
            reason = preference["reason"]

        response: dict = {
            "status": "selected",
            "chosen": candidates[chosen_index],
            "chosen_index": chosen_index,
            "selection_basis": selection_basis,
            "reason": reason,
        }
        if jlcpcb_report is not None:
            response["jlcpcb_lookup"] = jlcpcb_report
        if selection_basis != "single_distinct_candidate":
            response["jlcpcb_class"] = parsed[chosen_index].jlcpcb_class
        return response

    def lookup_jlcpcb_part_tiers(identifiers: list[str]) -> dict:
        """Look up JLCPCB's part tier (Basic / Preferred Extended / Extended)
        for many parts at once, from local data with no network call.

        Pass a list of LCSC part numbers and/or MPNs -- never loop this one
        part at a time. Extended parts cost a per-part feeder loading fee
        ($3); Basic and Preferred Extended do not. A part that isn't in the
        local index comes back with a null tier meaning "unknown", which is
        NOT the same as "Extended". Every result carries `index` (date,
        commit, total_parts); the shipped index records a commit but no date,
        and says so. Each unknown part has a `tier_meaning` sentence naming
        the index it is missing from.
        """
        return jlc.classify_parts(identifiers)

    def jlcpcb_library_status() -> dict:
        """Report what JLCPCB part data is available locally: the vendored
        tier index (always shipped) and, separately, whether a local checkout
        of CDFER/JLCPCB-Kicad-Library is present to supply ready
        symbol/footprint/3D assets. Use this to explain a degraded answer
        rather than guessing why a lookup came back unknown.
        """
        return jlc.library_status()

    def resolve_jlcpcb_assets(identifiers: list[str]) -> dict:
        """For many parts at once, say whether ready KiCad assets are already
        available locally from JLCPCB-Kicad-Library (maintained,
        KLC-conformant, includes STEP models, no network round-trip) or
        whether to fall back to the live easyeda2kicad conversion path.

        Fetches nothing itself. Each result is either
        source "jlcpcb_library" with concrete local paths, or source
        "easyeda2kicad" with the lcsc_id to pass to fetch_part_assets.
        """
        return jlc.resolve_assets(identifiers)

    def propose_component_match(candidates: list[dict]) -> dict:
        """Propose a set of plausible part matches for human/agent
        confirmation -- does not commit anything. Returns a revision_token
        that must be passed to confirm_component_match to actually select
        one, per docs/ARCHITECTURE.md's dry-run + revision-token pattern.
        """
        if not candidates:
            return {"status": "error", "reason": "candidates must be a non-empty list"}
        _evict_stale_pending_matches()
        token = uuid.uuid4().hex
        _PENDING_MATCHES[token] = (candidates, time.time() + _PENDING_MATCH_TTL_SECONDS)
        return {"status": "pending_confirmation", "revision_token": token, "candidates": candidates}

    def confirm_component_match(revision_token: str, chosen_index: int) -> dict:
        """Confirm a specific candidate from a prior propose_component_match
        call. Consumes the token -- a second confirm with the same token
        fails rather than silently re-succeeding, so a stale confirmation
        can't re-apply a decision that's already been acted on. A proposal
        left unconfirmed for more than an hour expires the same way.
        """
        _evict_stale_pending_matches()
        entry = _PENDING_MATCHES.pop(revision_token, None)
        if entry is None:
            return {"status": "error", "reason": "unknown or expired revision_token"}
        candidates, expires_at = entry
        if not (0 <= chosen_index < len(candidates)):
            # Put it back -- an out-of-range index is a caller mistake, not
            # a decision to discard the pending proposal.
            _PENDING_MATCHES[revision_token] = (candidates, expires_at)
            return {"status": "error", "reason": f"chosen_index out of range (0..{len(candidates) - 1})"}
        return {"status": "confirmed", "chosen": candidates[chosen_index]}

    def fetch_datasheet(url: str, output_dir: str) -> dict:
        """Download a candidate part's datasheet PDF to output_dir.
        Evidence-gated: a bad URL, timeout, or non-PDF response is reported
        explicitly (status: incomplete), never written to disk as if it
        were a real PDF and never raised unhandled.
        """
        return _fetch_datasheet(url, output_dir)

    def inspect_datasheet(pdf_path: str, query: str | None = None) -> dict:
        """Extract structured content (pin tables, absolute maximum
        ratings, package dimensions) from a fetched datasheet PDF, so a
        sourcing decision can be verified against the real part spec
        instead of trusting a parametric-search match alone. Call this
        before confirming a component match -- see the kicad-sourcing
        skill. A scanned/image-only PDF with no extractable text reports
        status: incomplete rather than a false pass.
        """
        return _inspect_datasheet(pdf_path, query)

    def fetch_part_assets(lcsc_id: str, output_dir: str, overwrite: bool = True) -> dict:
        """Retrieve a ready KiCad symbol/footprint/3D model for an
        LCSC-numbered part via easyeda2kicad, into output_dir. Prefer
        resolve_jlcpcb_assets first for JLCPCB Basic/Preferred parts --
        it needs no network round-trip when a local asset already covers
        the part. Evidence-gated: no LCSC match or a conversion failure
        is reported explicitly, not returned as a silent partial success.
        """
        return _fetch_part_assets(lcsc_id, output_dir, overwrite=overwrite)

    def generate_footprint(package_type: PackageType, output_path: str, params: FootprintParams) -> dict:
        """Generate a parametric, IPC-7351-style footprint (e.g. from
        pitch/pad-count/body-size) for a part with no ready asset from
        fetch_part_assets/resolve_jlcpcb_assets -- a fallback, not the
        preferred path, when a maintained ready asset exists. ``params``
        holds the generator's parameters; one the package type does not
        take is refused by name, before anything is written.
        """
        kwargs = _footprint_kwargs(package_type, params)
        if isinstance(kwargs, str):
            return {"status": "invalid_params", "reason": kwargs + "; nothing was written"}
        return _generate_footprint(package_type, output_path, **kwargs)

    def list_package_types() -> dict:
        """List the package_type values generate_footprint supports, with the
        params each one takes."""
        types = _list_package_types()
        return {
            "count": len(types),
            "package_types": types,
            "parameters": {
                t: {"required": list(REQUIRED_PACKAGE_PARAMETERS[t]), "accepted": list(PACKAGE_PARAMETERS[t])}
                for t in types
            },
        }

    mcp.add_tool(
        search_parts,
        name="search_parts",
        description=(
            "Search all registered component-sourcing providers (Nexar/Octopart, "
            "Digi-Key, LCSC, ...) for parts matching a query, merged and deduped "
            "by manufacturer+MPN into one paginated list. Supports 'limit'/'offset' "
            "pagination -- never loop this one part at a time. status is ok, "
            "no_results (every provider answered and nothing matched), partial "
            "(at least one provider failed; see provider_errors) or provider_error "
            "(every provider failed or none is registered: zero items says nothing "
            "about whether the part exists). On partial or provider_error the result "
            "names a fallback (lookup_jlcpcb_part_tiers, the local JLCPCB tier index)."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    mcp.add_tool(
        list_sourcing_providers,
        name="list_sourcing_providers",
        description="List which component-sourcing providers are currently registered and available to search_parts.",
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        select_component,
        name="select_component",
        description=(
            "Pick one component from a set of search_parts candidates. Returns the "
            "choice directly if unambiguous, or routes through the "
            "propose_component_match/confirm_component_match checkpoint automatically "
            "if there's more than one distinct plausible part and nothing resolved it. "
            "Resolution order is electrical fit first (a datasheet-verified leader is never "
            "displaced), then JLCPCB cost as a tiebreaker: a Basic/Preferred part is preferred "
            "over an Extended one only when the candidates were otherwise indistinguishable. "
            "Returns chosen_index, selection_basis and reason so the choice is auditable. "
            "Prefer this over calling propose_component_match yourself."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,  # ambiguous path mints a fresh pending proposal each call
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        propose_component_match,
        name="propose_component_match",
        description=(
            "Propose a set of plausible part candidates for human/agent confirmation when "
            "a component match is ambiguous (see is_match_ambiguous). Returns a "
            "revision_token -- nothing is selected until confirm_component_match is called "
            "with that exact token."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": False,  # each call mints a fresh token/pending entry
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        confirm_component_match,
        name="confirm_component_match",
        description=(
            "Confirm one candidate from a prior propose_component_match call by index. "
            "Consumes the revision_token -- a second confirm with the same token fails "
            "rather than re-applying the decision."
        ),
        annotations={
            "readOnlyHint": False,  # mutates the pending-proposal state
            "destructiveHint": False,
            "idempotentHint": False,  # explicitly NOT idempotent: same token+args fails the 2nd time by design
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        lookup_jlcpcb_part_tiers,
        name="lookup_jlcpcb_part_tiers",
        description=(
            "Batch-look-up JLCPCB's part tier (Basic / Preferred Extended / Extended) for a list "
            "of LCSC part numbers and/or MPNs, from a local index with no network call. Extended "
            "parts cost a per-part feeder loading fee; Basic and Preferred Extended do not. Use "
            "this to make a BOM cost-aware -- pass every part at once, never one per call. A null "
            "tier means 'not in the local index / unknown', not 'Extended'. The result carries "
            "`index` (date, commit, total_parts; the date is null with a date_note when the index "
            "records none) and each unknown part a `tier_meaning` ('not in the index ..., N parts'); "
            "print that, never a guessed tier."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        jlcpcb_library_status,
        name="jlcpcb_library_status",
        description=(
            "Report which JLCPCB data is available locally: the vendored part-tier index, and "
            "separately whether a local checkout of CDFER/JLCPCB-Kicad-Library is present for "
            "ready symbol/footprint/3D assets. Call this to explain why a tier or asset lookup "
            "came back unavailable instead of guessing."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        resolve_jlcpcb_assets,
        name="resolve_jlcpcb_assets",
        description=(
            "For a list of parts at once, report whether ready KiCad assets (symbol, footprint, "
            "STEP model) are already available locally from JLCPCB-Kicad-Library -- preferred, "
            "since they are maintained and need no network round-trip -- or whether to fall back "
            "to the live easyeda2kicad conversion path. Fetches nothing itself."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        fetch_datasheet,
        name="fetch_datasheet",
        description="Download a candidate part's datasheet PDF to output_dir. Evidence-gated: a bad URL/timeout/non-PDF response is reported explicitly, never written to disk as a fake PDF.",
        annotations={
            "readOnlyHint": False,  # writes a file to output_dir
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,  # hits a third-party URL
        },
    )
    mcp.add_tool(
        inspect_datasheet,
        name="inspect_datasheet",
        description="Extract structured content (pin tables, absolute max ratings, package dimensions) from a fetched datasheet PDF, to verify a candidate part against its real spec before confirming a match. Call before select_component/confirm_component_match, per the kicad-sourcing skill.",
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        fetch_part_assets,
        name="fetch_part_assets",
        description="Retrieve a ready KiCad symbol/footprint/3D model for an LCSC-numbered part via easyeda2kicad. Prefer resolve_jlcpcb_assets first for JLCPCB Basic/Preferred parts -- no network round-trip needed when a local asset already covers the part.",
        annotations={
            "readOnlyHint": False,  # writes assets to output_dir
            "destructiveHint": False,
            "idempotentHint": False,  # overwrite=True (default) replaces existing files
            "openWorldHint": True,  # shells out to easyeda2kicad, which hits the network
        },
    )
    mcp.add_tool(
        generate_footprint,
        name="generate_footprint",
        description=(
            "Generate a parametric IPC-7351-style footprint (.kicad_mod at output_path) for a part with no "
            "ready asset from fetch_part_assets/resolve_jlcpcb_assets. package_type picks the generator; "
            'params holds its parameters, lengths in mm, e.g. chip: {"size": "0603"}, soic: {"pins": 8}, '
            'sot: {"variant": "SOT-23"}. list_package_types names the required and accepted params of each '
            "package_type. Every generator parameter goes inside params. A name inside params that is unknown, "
            "that the package_type does not take, or that the generator would ignore next to another, is "
            "refused by name and nothing is written; an argument outside params other than package_type and "
            "output_path is refused by name too, with nothing written."
        ),
        annotations={
            "readOnlyHint": False,  # writes output_path
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    _refuse_extra_arguments(mcp, "generate_footprint", _FOOTPRINT_TOP_LEVEL)
    mcp.add_tool(
        list_package_types,
        name="list_package_types",
        description=(
            "List the package_type values generate_footprint supports, with the required and accepted "
            "params of each."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    return [
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
    ]
