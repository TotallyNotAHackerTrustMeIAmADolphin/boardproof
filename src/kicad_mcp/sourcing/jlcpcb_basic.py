"""JLCPCB part-tier lookup: is this part Basic, Preferred, or Extended?

Why this exists (archived backlog P1-22): JLCPCB charges a per-part *feeder
loading* fee for Extended parts. Preferring a fee-exempt part when a design
has a real choice removes that cost from the BOM. This module answers the
tier question **from local data with no network round-trip**, so it is cheap
enough to call on every candidate of every search.

Three tiers, not two (verified 2026-09-04 against JLCPCB's own PCB Assembly
FAQ, https://jlcpcb.com/help/article/pcb-assembly-faqs -- the ticket was
groomed against a two-way Basic/Extended split, which is out of date):

- **Basic Components** -- already loaded on the pick-and-place machines,
  "no labor cost for feeder setup". Fee-exempt unconditionally.
- **Preferred Extended Parts** -- "previously classified as Basic, now
  categorized as Extended", but "exempt from the Feeders Loading fee for
  Economic PCB assembly". Fee-exempt *for Economic PCBA*; that qualifier is
  real and is carried through in `setup_fee_exempt_economic` rather than
  flattened into "free".
- **Extended Components** -- "$3 per extended component".

So `PartResult.jlcpcb_basic` (a bool, per the ticket's contract) cannot carry
the whole truth on its own: `False` on a Preferred part would read as "this
costs a setup fee", which is wrong. `PartResult.jlcpcb_class` carries the
actual tier alongside it. See models.py.

Data source
-----------
`https://github.com/CDFER/JLCPCB-Kicad-Library` -- **MIT licensed**
(re-confirmed 2026-09-04 by reading the repository's own LICENSE file, not
by trusting GitHub's license badge or an earlier note: "MIT License,
Copyright (c) 2023 Chris", full permission grant, standard MIT text).
Its `metadata.json` and README also both state MIT.

Its real structure (inspected before choosing how to depend on it, per the
ticket):

- `symbols/JLCPCB-*.kicad_sym` -- ~20 KiCad symbol libraries, 894 symbols
  total. Each symbol carries the tier as a plain KiCad property,
  `(property "Class" "Basic Component" ...)`, alongside `LCSC`, `Part`
  (the MPN), `Manufacturer`, `Footprint`, `Minimum Qty`, `Attrition Qty`,
  and volatile `Stock`/`Price` fields refreshed daily by a GitHub Action.
  **There is no separate machine-readable parts index** -- the symbol
  libraries *are* the index.
- `footprints/JLCPCB.pretty/*.kicad_mod` -- ~130 footprints.
- `3dmodels/JLCPCB.3dshapes/*.step` -- the bulk of the repo.
- `Archived-Symbols-Footprints/` -- superseded per-part files, ignored here.

Two facts here contradicted the ticket's grooming assumptions and changed
the design:

1. The library is **not** "the Basic components" -- it is Basic *and*
   Preferred ("focused on components from JLCPCB's basic and preferred
   parts list, ensuring no extra setup costs" -- its README). 350 Basic vs
   518 Preferred at the pinned commit. Membership therefore does **not**
   imply Basic; the `Class` property does.
2. `symbols/JLCPCB-Extended.kicad_sym` holds 21 hand-added symbols
   (RP2040, ESP32-*, CH340G, ...) that carry **no** `Class` property at
   all. Those are genuinely Extended parts the author added for
   convenience, so tier is taken from library membership there and tagged
   `tier_source: "library_membership"` so the weaker evidence is visible.

How we depend on it
-------------------
The repo is ~103 MB, almost entirely STEP models, so it is not vendored. Two
things are used instead:

- **Vendored, generated index** (`data/jlcpcb_basic_index.json`, ~300 KB):
  identity + tier + asset *names*, distilled from the symbol libraries by
  `build_index()` below. Deliberately excludes `Stock`/`Price`, which the
  upstream Action rewrites daily -- vendoring a stale price would be a
  silent wrong answer, and this project has no business quoting a price it
  cannot re-verify. Tier, MPN and footprint names move on a scale of
  months, so a checked-in snapshot is honest data, and it makes the common
  question answerable with zero setup and zero network.
- **The library itself, fetched on demand**, for the *assets* (symbol,
  footprint, STEP). It is a git submodule at `third_party/JLCPCB-Kicad-Library`
  (shallow, opt-in: a plain clone does not pull its 103 MB; fetch it with
  `git submodule update --init --depth 1 third_party/JLCPCB-Kicad-Library`),
  pinned to the commit the index was built from (publish-prep ticket 03b).
  Lookup order, first valid wins: an explicit argument, the
  `KICAD_MCP_JLCPCB_LIBRARY` environment variable, the submodule path (only
  when running from a checkout), KiCad's 3rd-party directory (PCM install).
  Present -> `resolve_assets()` hands back real KLC-conformant local files
  and the caller skips the live `easyeda2kicad` conversion entirely.
  Absent -> it says so explicitly and directs the caller to
  `sourcing.asset_fetch.fetch_part_assets`, which is left untouched and
  treated as the contract for that fallback. A candidate that was named but
  is unusable (a variable pointing nowhere, an empty or missing submodule
  directory) is reported in `library_notes`, with the fetch command, even
  when a later candidate is used.

Nothing here ever degrades silently: a missing index, a missing library, or
an unrecognised part all produce an explicit `status`/`reason`, and an
unknown tier is `None` ("never looked it up / not in the index"), never
`False` ("checked, and it is not Basic").
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from kicad_mcp.sourcing.models import PartResult

# --- tiers -----------------------------------------------------------------

TIER_BASIC = "basic"
TIER_PREFERRED = "preferred"
TIER_EXTENDED = "extended"

#: Tiers that carry no per-part feeder loading fee on Economic PCBA. Basic is
#: exempt unconditionally; Preferred is exempt "for Economic PCB assembly"
#: per JLCPCB's FAQ -- see `setup_fee_exempt_economic` vs `jlcpcb_basic`.
NO_SETUP_FEE_TIERS = frozenset({TIER_BASIC, TIER_PREFERRED})

#: Preference order for cost-based tie-breaking. Lower is better. An unknown
#: tier sorts *between* the fee-exempt tiers and confirmed Extended: we have
#: no evidence it is cheap, but also no evidence it is not, so it must never
#: rank above a part we have positively confirmed is fee-exempt.
_TIER_RANK = {TIER_BASIC: 0, TIER_PREFERRED: 1, None: 2, TIER_EXTENDED: 3}

_CLASS_PROPERTY_TO_TIER = {
    "basic component": TIER_BASIC,
    "basic": TIER_BASIC,
    "preferred component": TIER_PREFERRED,
    "preferred": TIER_PREFERRED,
    "preferred extended": TIER_PREFERRED,
    "preferred extended component": TIER_PREFERRED,
    "extended component": TIER_EXTENDED,
    "extended": TIER_EXTENDED,
}

# --- where the data lives --------------------------------------------------

JLCPCB_LIBRARY_ENV_VAR = "KICAD_MCP_JLCPCB_LIBRARY"
"""Environment variable pointing at a local clone or KiCad-PCM install of
CDFER/JLCPCB-Kicad-Library (the directory containing `symbols/`)."""

VENDORED_INDEX_PATH = Path(__file__).parent / "data" / "jlcpcb_basic_index.json"

UPSTREAM_REPO_URL = "https://github.com/CDFER/JLCPCB-Kicad-Library"

#: Where the git submodule lives, relative to the repository root.
SUBMODULE_RELATIVE_PATH = "third_party/JLCPCB-Kicad-Library"

#: The one-line command that fetches the submodule (shallow, opt-in).
SUBMODULE_FETCH_COMMAND = f"git submodule update --init --depth 1 {SUBMODULE_RELATIVE_PATH}"

#: KiCad's Plugin & Content Manager installs this library under a directory
#: named after its PCM identifier (`com.github.CDFER.JLCPCB-Kicad-Library`)
#: with dots replaced by underscores.
_PCM_DIR_NAME = "com_github_CDFER_JLCPCB-Kicad-Library"

_LCSC_ID_RE = re.compile(r"^C\d+$", re.IGNORECASE)

_MISSING_INDEX_REASON = (
    f"the vendored JLCPCB part-tier index is missing or unreadable at {VENDORED_INDEX_PATH}. "
    f"No tier can be reported. Regenerate it with build_index() against a clone of "
    f"{UPSTREAM_REPO_URL}, or set {JLCPCB_LIBRARY_ENV_VAR} to a local checkout and call "
    f"refresh_index()."
)


# --- index -----------------------------------------------------------------


class JLCPCBBasicIndex:
    """In-memory view of the vendored part-tier index.

    Lookup is by LCSC part number (`C7955`) or by MPN (`LM393DR2G`), both
    case-insensitively. MPN lookup can legitimately hit more than one entry
    (the same MPN listed under different manufacturers); when those entries
    disagree on tier the lookup reports the conflict rather than picking one.
    """

    def __init__(self, payload: dict) -> None:
        self.schema_version: int = int(payload.get("schema_version", 0))
        self.source: dict = dict(payload.get("source", {}))
        self.counts: dict = dict(payload.get("counts", {}))
        self.parts: list[dict] = list(payload.get("parts", []))
        self._by_lcsc: dict[str, dict] = {}
        self._by_mpn: dict[str, list[dict]] = {}
        for entry in self.parts:
            lcsc = (entry.get("lcsc") or "").strip().upper()
            if lcsc:
                self._by_lcsc.setdefault(lcsc, entry)
            mpn = (entry.get("mpn") or "").strip().lower()
            if mpn:
                self._by_mpn.setdefault(mpn, []).append(entry)

    def __len__(self) -> int:
        return len(self.parts)

    def lookup(self, identifier: str) -> tuple[str | None, list[dict]]:
        """Resolve one identifier. Returns (match_type, entries).

        `match_type` is "lcsc", "mpn", or None when nothing matched.
        """
        key = (identifier or "").strip()
        if not key:
            return None, []
        if _LCSC_ID_RE.match(key):
            entry = self._by_lcsc.get(key.upper())
            return ("lcsc", [entry]) if entry else (None, [])
        entry = self._by_lcsc.get(key.upper())
        if entry is not None:
            return "lcsc", [entry]
        entries = self._by_mpn.get(key.lower())
        if entries:
            return "mpn", list(entries)
        return None, []


_INDEX_CACHE: JLCPCBBasicIndex | None = None
_INDEX_LOAD_ERROR: str | None = None


def load_index(path: str | Path | None = None, *, refresh: bool = False) -> JLCPCBBasicIndex | None:
    """Load the vendored index, cached. Returns None (never raises) when the
    index file is missing or unreadable -- callers report that as an explicit
    degraded status, they do not fall back to guessing.
    """
    global _INDEX_CACHE, _INDEX_LOAD_ERROR
    if path is None and _INDEX_CACHE is not None and not refresh:
        return _INDEX_CACHE
    target = Path(path) if path is not None else VENDORED_INDEX_PATH
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
        index = JLCPCBBasicIndex(payload)
    except Exception as exc:  # noqa: BLE001 -- evidence-gated: degrade explicitly, never raise
        if path is None:
            _INDEX_CACHE = None
            _INDEX_LOAD_ERROR = f"{type(exc).__name__}: {exc}"
        return None
    if path is None:
        _INDEX_CACHE = index
        _INDEX_LOAD_ERROR = None
    return index


def _index_or_reason(index: JLCPCBBasicIndex | None) -> tuple[JLCPCBBasicIndex | None, str | None]:
    if index is not None:
        return index, None
    loaded = load_index()
    if loaded is not None:
        return loaded, None
    reason = _MISSING_INDEX_REASON
    if _INDEX_LOAD_ERROR:
        reason = f"{reason} (load error: {_INDEX_LOAD_ERROR})"
    return None, reason


# --- classification (batch) ------------------------------------------------


def _result_for(identifier: str, match_type: str | None, entries: Sequence[dict]) -> dict:
    base: dict[str, Any] = {
        "identifier": identifier,
        "matched": bool(entries),
        "match_type": match_type,
        "jlcpcb_basic": None,
        "jlcpcb_class": None,
        "setup_fee_exempt_economic": None,
    }
    if not entries:
        base["reason"] = (
            "not present in the JLCPCB-Kicad-Library index. That library covers JLCPCB's "
            "Basic and Preferred parts (plus a small hand-added Extended set), not the whole "
            "JLCPCB catalogue -- absence is 'unknown tier', NOT 'confirmed Extended'."
        )
        return base

    tiers = {e.get("tier") for e in entries}
    if len(tiers) > 1:
        base["reason"] = (
            f"identifier matched {len(entries)} index entries with conflicting tiers "
            f"({sorted(t for t in tiers if t)}) -- disambiguate by LCSC part number. "
            "Reporting unknown rather than picking one."
        )
        base["candidates"] = [
            {"lcsc": e.get("lcsc"), "mpn": e.get("mpn"), "manufacturer": e.get("manufacturer"), "tier": e.get("tier")}
            for e in entries
        ]
        return base

    entry = entries[0]
    tier = entry.get("tier")
    base.update(
        {
            "jlcpcb_class": tier,
            "jlcpcb_basic": tier == TIER_BASIC if tier else None,
            "setup_fee_exempt_economic": tier in NO_SETUP_FEE_TIERS if tier else None,
            "tier_source": entry.get("tier_source"),
            "lcsc_id": entry.get("lcsc"),
            "mpn": entry.get("mpn"),
            "manufacturer": entry.get("manufacturer"),
            "symbol_library": entry.get("symbol_lib"),
            "symbol_name": entry.get("symbol"),
            "footprint": entry.get("footprint"),
            "minimum_qty": entry.get("minimum_qty"),
            "attrition_qty": entry.get("attrition_qty"),
        }
    )
    if tier == TIER_PREFERRED:
        base["note"] = (
            "Preferred Extended: exempt from JLCPCB's per-part feeder loading fee for "
            "Economic PCBA, but still an Extended part (so jlcpcb_basic is False)."
        )
    elif tier == TIER_EXTENDED:
        base["note"] = "Extended: incurs JLCPCB's per-part feeder loading fee."
    return base


def index_info(idx: JLCPCBBasicIndex) -> dict:
    """Age and size of the index a lookup was answered from.

    The shipped index records its upstream commit (the pin the refresh script
    writes) and its part counts, but no date: ``date`` is then None and
    ``date_note`` says so. A date is shown only if ``source.commit_date`` holds one.
    """
    commit = idx.source.get("commit")
    raw_date = idx.source.get("commit_date")
    date = str(raw_date)[:10] if raw_date else None
    info: dict[str, Any] = {
        "date": date,
        "commit": commit,
        "total_parts": len(idx),
        "counts": dict(idx.counts),
    }
    if date is None:
        info["date_note"] = (
            "the index records its upstream commit but not a date (date not recorded); "
            "judge its age from the commit"
        )
    return info


def _unknown_tier_meaning(info: dict) -> str:
    when = f"of {info['date']}" if info["date"] else (
        f"(pinned commit {str(info['commit'])[:7]}, date not recorded)" if info["commit"]
        else "(commit and date not recorded)"
    )
    return (
        f"not in the index {when}, {info['total_parts']} parts. Tier unknown: this is not Extended, "
        "and a newer or wider catalogue may know the part."
    )


def classify_parts(identifiers: Sequence[str], *, index: JLCPCBBasicIndex | None = None) -> dict:
    """**Batch** tier lookup: many part identifiers in, one result set out.

    `identifiers` may mix LCSC part numbers (`"C7955"`) and MPNs
    (`"LM393DR2G"`). Never loop this one part at a time.

    Evidence-gated: `status` is "ok" when every identifier resolved,
    "partial" when some did not, "unavailable" when the index itself is
    missing (in which case nothing is claimed about any part), "error" on
    malformed input. An unresolved identifier comes back with
    `jlcpcb_basic: None` and a `reason` -- never `False`, which would assert
    the part is not Basic when we simply have no record of it.
    """
    if not isinstance(identifiers, (list, tuple)):
        return {"status": "error", "reason": "identifiers must be a list of part identifiers"}
    cleaned = [str(i).strip() for i in identifiers if str(i).strip()]
    if not cleaned:
        return {"status": "error", "reason": "identifiers must be a non-empty list of part identifiers"}

    idx, reason = _index_or_reason(index)
    if idx is None:
        return {
            "status": "unavailable",
            "reason": reason,
            "count": len(cleaned),
            "results": [
                {"identifier": i, "matched": False, "jlcpcb_basic": None, "jlcpcb_class": None, "reason": reason}
                for i in cleaned
            ],
        }

    results = [_result_for(i, *idx.lookup(i)) for i in cleaned]
    info = index_info(idx)
    summary = {TIER_BASIC: 0, TIER_PREFERRED: 0, TIER_EXTENDED: 0, "unknown": 0}
    for r in results:
        summary[r["jlcpcb_class"] or "unknown"] += 1
        if r["jlcpcb_class"] is None:
            r["tier_meaning"] = _unknown_tier_meaning(info)

    unresolved = summary["unknown"]
    response: dict[str, Any] = {
        "status": "ok" if unresolved == 0 else "partial",
        "count": len(results),
        "results": results,
        "summary": summary,
        "index_source": idx.source,
        "index": info,
    }
    if unresolved:
        response["reason"] = (
            f"{unresolved} of {len(results)} identifiers are not in the JLCPCB-Kicad-Library index "
            f"({info['total_parts']} parts, {'date ' + info['date'] if info['date'] else 'date not recorded'}); "
            "their tier is unknown (reported as null), not Extended."
        )
    return response


def annotate_part_results(
    parts: Sequence[PartResult], *, index: JLCPCBBasicIndex | None = None
) -> tuple[list[PartResult], dict]:
    """**Batch** enrichment: stamp `jlcpcb_basic`/`jlcpcb_class` onto a list of
    `PartResult`s in one pass.

    Returns (annotated_parts, lookup_report). When the index is unavailable
    the parts come back unchanged -- every `jlcpcb_basic` still `None`,
    meaning "not looked up" -- and the report says why.
    """
    idx, reason = _index_or_reason(index)
    if idx is None:
        return list(parts), {"status": "unavailable", "reason": reason, "count": len(parts)}

    annotated: list[PartResult] = []
    summary = {TIER_BASIC: 0, TIER_PREFERRED: 0, TIER_EXTENDED: 0, "unknown": 0}
    for part in parts:
        # Prefer the LCSC id when present: it is unambiguous, an MPN is not.
        match_type, entries = (None, [])
        if part.lcsc_id:
            match_type, entries = idx.lookup(part.lcsc_id)
        if not entries and part.mpn:
            match_type, entries = idx.lookup(part.mpn)
        outcome = _result_for(part.lcsc_id or part.mpn, match_type, entries)
        summary[outcome["jlcpcb_class"] or "unknown"] += 1
        annotated.append(
            part.model_copy(
                update={
                    "jlcpcb_basic": outcome["jlcpcb_basic"],
                    "jlcpcb_class": outcome["jlcpcb_class"],
                }
            )
        )
    unresolved = summary["unknown"]
    report = {
        "status": "ok" if unresolved == 0 else "partial",
        "count": len(annotated),
        "summary": summary,
        "index_source": idx.source,
    }
    if unresolved:
        report["reason"] = (
            f"{unresolved} of {len(annotated)} parts are not in the JLCPCB-Kicad-Library index; "
            "their tier is left null (unknown), not False."
        )
    return annotated, report


def tier_rank(tier: str | None) -> int:
    """Cost-preference rank for a tier. Lower is better; unknown sorts between
    the fee-exempt tiers and confirmed Extended.

    This is a **tiebreaker only** -- see `toolsets.sourcing.is_match_ambiguous`
    for where it is (and is not) allowed to influence a selection.
    """
    return _TIER_RANK.get(tier, _TIER_RANK[None])


# --- local library assets --------------------------------------------------


def _checkout_root() -> Path | None:
    """The repository root when this module runs from a source checkout, else None.

    A checkout is recognised by `.gitmodules` and `src/kicad_mcp` next to each other at
    the root; an installed package (site-packages) has neither, so the submodule path is
    not even considered there.
    """
    root = Path(__file__).resolve().parents[3]
    if (root / ".gitmodules").is_file() and (root / "src" / "kicad_mcp").is_dir():
        return root
    return None


def _home_dir() -> Path:
    """The user's home directory (a seam so tests can use an empty one)."""
    return Path.home()


def _library_sources(explicit: str | Path | None = None) -> list[tuple[str, Path]]:
    """Every place the library may live, in lookup order, labelled by kind:
    `explicit`, `env`, `submodule`, `pcm`."""
    sources: list[tuple[str, Path]] = []
    if explicit:
        sources.append(("explicit", Path(explicit)))
    env_value = os.environ.get(JLCPCB_LIBRARY_ENV_VAR)
    if env_value:
        sources.append(("env", Path(env_value)))
    checkout = _checkout_root()
    if checkout is not None:
        sources.append(("submodule", checkout / SUBMODULE_RELATIVE_PATH))
    home = _home_dir()
    for docs in (home / "Documents" / "KiCad", home / "KiCad", home / ".local" / "share" / "kicad"):
        if not docs.is_dir():
            continue
        for version_dir in sorted(docs.iterdir(), reverse=True):
            third_party = version_dir / "3rdparty"
            if third_party.is_dir():
                # PCM splits an installed library across symbols/<id>,
                # footprints/<id> and 3dmodels/<id>; `3rdparty` itself is the
                # root we normalise against.
                sources.append(("pcm", third_party))
    return sources


def _candidate_library_roots(explicit: str | Path | None = None) -> list[Path]:
    return [path for _, path in _library_sources(explicit)]


def _source_problem(kind: str, root: Path) -> str | None:
    """Why a named candidate is unusable, or None if it holds a recognisable library."""
    try:
        if root.is_dir() and _library_layout(root) is not None:
            return None
        is_dir = root.is_dir()
        is_empty = is_dir and not any(root.iterdir())
    except OSError as exc:
        return f"{kind} candidate {root} cannot be read ({type(exc).__name__})."
    if kind == "submodule":
        if not is_dir or is_empty:
            return (
                f"the JLCPCB library submodule at {root} is not initialised "
                f"({'the directory is empty' if is_dir else 'the directory is missing'}). "
                f"Fetch it with: {SUBMODULE_FETCH_COMMAND}"
            )
        return (
            f"the JLCPCB library submodule at {root} has files but no 'symbols/JLCPCB-*.kicad_sym'. "
            f"Re-fetch it with: {SUBMODULE_FETCH_COMMAND}"
        )
    if kind == "pcm":
        return None  # a KiCad 3rdparty directory without this library is the normal case
    if kind == "env":
        return (
            f"{JLCPCB_LIBRARY_ENV_VAR}={root} "
            f"{'is not a directory' if not is_dir else 'has no symbols/JLCPCB-*.kicad_sym'}; it was skipped."
        )
    return f"the {kind} library path {root} does not hold 'symbols/JLCPCB-*.kicad_sym'."


def library_notes(explicit: str | Path | None = None) -> list[str]:
    """Plain-language problems with candidates tried before the one that is used.

    A candidate that was named (explicit argument, environment variable, or the submodule
    of this checkout) but is unusable is never skipped silently: it is listed here, with
    the command that fixes it where there is one. Candidates after the first usable one
    are not listed, since they were never reached.
    """
    notes: list[str] = []
    for kind, root in _library_sources(explicit):
        problem = _source_problem(kind, root)
        if problem is None:
            if _safe_layout(root) is not None:
                break
            continue
        notes.append(problem)
    return notes


def _safe_layout(root: Path) -> dict | None:
    try:
        return _library_layout(root) if root.is_dir() else None
    except OSError:
        return None


def _library_layout(root: Path) -> dict | None:
    """Recognise either layout: a plain git clone (`symbols/`,
    `footprints/JLCPCB.pretty`, `3dmodels/JLCPCB.3dshapes`) or a KiCad PCM
    install (`symbols/<pcm-id>/`, ...). Returns None if neither is present.
    """
    clone_symbols = root / "symbols"
    if (clone_symbols / "JLCPCB-Analog.kicad_sym").is_file() or any(clone_symbols.glob("JLCPCB-*.kicad_sym")):
        return {
            "layout": "clone",
            "root": root,
            "symbols_dir": clone_symbols,
            "footprints_dir": root / "footprints" / "JLCPCB.pretty",
            "models_dir": root / "3dmodels" / "JLCPCB.3dshapes",
        }
    pcm_symbols = root / "symbols" / _PCM_DIR_NAME
    if pcm_symbols.is_dir() and any(pcm_symbols.glob("JLCPCB-*.kicad_sym")):
        return {
            "layout": "pcm",
            "root": root,
            "symbols_dir": pcm_symbols,
            "footprints_dir": root / "footprints" / _PCM_DIR_NAME / "JLCPCB.pretty",
            "models_dir": root / "3dmodels" / _PCM_DIR_NAME / "JLCPCB.3dshapes",
        }
    return None


def _model_path_from_footprint(footprint_path: Path, models_dir: Path) -> Path | None:
    """Read a `.kicad_mod` file's own `(model "...")` reference and resolve
    its filename against `models_dir`.

    Returns None if the footprint file cannot be parsed or has no model
    directive -- never raises, since a missing/malformed footprint just
    means "no 3D model resolvable here", not a hard error for the whole
    lookup.
    """
    if not footprint_path.is_file():
        return None
    try:
        from kicad_tools.sexp.parser import parse_file  # local import: keeps module import cheap

        doc = parse_file(str(footprint_path))
    except Exception:  # noqa: BLE001 -- degrade to "no model resolvable", don't crash the batch call
        return None
    model_nodes = doc.find_children("model")
    if not model_nodes:
        return None
    raw = str(model_nodes[0].get_first_atom() or "")
    if not raw:
        return None
    filename = raw.replace("\\", "/").rsplit("/", 1)[-1]
    return models_dir / filename if filename else None


def resolve_library_root(explicit: str | Path | None = None) -> dict | None:
    """Locate a local clone/PCM install of the library, or None if absent."""
    for root in _candidate_library_roots(explicit):
        try:
            if not root.is_dir():
                continue
            layout = _library_layout(root)
        except OSError:
            continue
        if layout:
            return layout
    return None


def library_status(explicit: str | Path | None = None) -> dict:
    """Report exactly what JLCPCB data is available locally, and what is not.

    Deliberately verbose about the degraded case: the index alone answers
    "is this Basic?", but only a real local library can supply ready symbol
    /footprint/3D assets, and a caller must be able to tell those apart
    instead of quietly getting the `easyeda2kicad` path every time.
    """
    idx, reason = _index_or_reason(None)
    layout = resolve_library_root(explicit)
    status: dict[str, Any] = {
        "status": "ok" if idx is not None else "degraded",
        "index_available": idx is not None,
        "index_path": str(VENDORED_INDEX_PATH),
        "library_available": layout is not None,
        "upstream": UPSTREAM_REPO_URL,
        "license": "MIT",
        "env_var": JLCPCB_LIBRARY_ENV_VAR,
        "submodule_path": SUBMODULE_RELATIVE_PATH,
        "fetch_command": SUBMODULE_FETCH_COMMAND,
    }
    notes = library_notes(explicit)
    if notes:
        status["library_notes"] = notes
    if idx is not None:
        status["index_source"] = idx.source
        status["index_counts"] = idx.counts
        status["indexed_parts"] = len(idx)
    else:
        status["reason"] = reason
    if layout is not None:
        status["library_root"] = str(layout["root"])
        status["library_layout"] = layout["layout"]
        status["symbols_dir"] = str(layout["symbols_dir"])
    else:
        status["library_reason"] = (
            f"no local checkout of {UPSTREAM_REPO_URL} found. Tier lookups still work from the "
            f"vendored index, but ready symbol/footprint/3D assets are not available locally, so "
            f"asset retrieval falls back to the live easyeda2kicad path "
            f"(sourcing.asset_fetch.fetch_part_assets). To enable the local path, fetch the "
            f"submodule with `{SUBMODULE_FETCH_COMMAND}`, or clone the repo (or install it via "
            f"KiCad's Plugin & Content Manager) and set {JLCPCB_LIBRARY_ENV_VAR} "
            f"to the directory containing 'symbols/'."
        )
        if idx is not None:
            status["status"] = "partial"
    return status


def resolve_assets(
    identifiers: Sequence[str],
    *,
    library_root: str | Path | None = None,
    index: JLCPCBBasicIndex | None = None,
) -> dict:
    """**Batch** asset resolution: for each part, say whether KLC-conformant
    assets are already available locally from JLCPCB-Kicad-Library, or
    whether the caller should fall back to the live conversion path.

    This never fetches or converts anything itself. Per part it returns
    either `source: "jlcpcb_library"` with concrete local paths, or
    `source: "easyeda2kicad"` with a `reason` and the `lcsc_id` to hand to
    `kicad_mcp.sourcing.asset_fetch.fetch_part_assets` -- whose behaviour is
    treated as the contract for that fallback and is not duplicated here.

    Preferring the library when it covers a part is deliberate: those assets
    are maintained, KLC-conformant, ship real STEP models, and need no
    network round-trip, whereas the easyeda2kicad path converts EasyEDA data
    live on every call.
    """
    if not isinstance(identifiers, (list, tuple)):
        return {"status": "error", "reason": "identifiers must be a list of part identifiers"}
    cleaned = [str(i).strip() for i in identifiers if str(i).strip()]
    if not cleaned:
        return {"status": "error", "reason": "identifiers must be a non-empty list of part identifiers"}

    idx, reason = _index_or_reason(index)
    layout = resolve_library_root(library_root)

    if idx is None:
        return {
            "status": "unavailable",
            "reason": reason,
            "count": len(cleaned),
            "results": [
                {
                    "identifier": i,
                    "source": "easyeda2kicad",
                    "reason": "JLCPCB tier index unavailable; cannot tell whether this part is covered locally.",
                    "lcsc_id": i if _LCSC_ID_RE.match(i) else None,
                }
                for i in cleaned
            ],
        }

    results: list[dict] = []
    from_library = 0
    for identifier in cleaned:
        match_type, entries = idx.lookup(identifier)
        entry = entries[0] if len(entries) == 1 else None
        lcsc = (entry or {}).get("lcsc") or (identifier if _LCSC_ID_RE.match(identifier) else None)
        if entry is None:
            results.append(
                {
                    "identifier": identifier,
                    "source": "easyeda2kicad",
                    "lcsc_id": lcsc,
                    "reason": (
                        "not uniquely covered by JLCPCB-Kicad-Library"
                        if entries
                        else "not present in JLCPCB-Kicad-Library"
                    )
                    + "; use sourcing.asset_fetch.fetch_part_assets(lcsc_id, output_dir).",
                }
            )
            continue
        if layout is None:
            results.append(
                {
                    "identifier": identifier,
                    "source": "easyeda2kicad",
                    "lcsc_id": lcsc,
                    "jlcpcb_class": entry.get("tier"),
                    "reason": (
                        "covered by JLCPCB-Kicad-Library, but no local checkout is present "
                        f"(run `{SUBMODULE_FETCH_COMMAND}` or set {JLCPCB_LIBRARY_ENV_VAR}); falling back to "
                        "sourcing.asset_fetch.fetch_part_assets(lcsc_id, output_dir)."
                    ),
                    "would_provide": {
                        "symbol_library": entry.get("symbol_lib"),
                        "symbol_name": entry.get("symbol"),
                        "footprint": entry.get("footprint"),
                    },
                }
            )
            continue

        symbol_path = layout["symbols_dir"] / f"{entry.get('symbol_lib')}.kicad_sym"
        footprint_name = (entry.get("footprint") or "").split(":")[-1]
        footprint_path = layout["footprints_dir"] / f"{footprint_name}.kicad_mod" if footprint_name else None
        # The 3D model filename does NOT always match the footprint name --
        # found while smoke-testing this against the real PCM install: the
        # QFN-56 footprint's model is "...EP3.2x3.2mm.step" while the
        # footprint itself is named "...EP3.2x3.2mm_ThermalVias". Guessing
        # the model filename from the footprint name silently produced a
        # false "missing 3D model". Read the footprint file's own `(model
        # "...")` reference instead -- via kicad_tools' parser, never by
        # hand-parsing the file text -- and resolve just its basename
        # against our own models_dir (the reference's directory portion
        # assumes a PCM install path regardless of which layout we actually
        # found it under, so only the filename is trustworthy).
        model_path = _model_path_from_footprint(footprint_path, layout["models_dir"]) if footprint_path else None
        have_symbol = symbol_path.is_file()
        have_footprint = bool(footprint_path and footprint_path.is_file())
        have_model = bool(model_path and model_path.is_file())
        if not have_symbol:
            results.append(
                {
                    "identifier": identifier,
                    "source": "easyeda2kicad",
                    "lcsc_id": lcsc,
                    "jlcpcb_class": entry.get("tier"),
                    "reason": (
                        f"index lists symbol library {entry.get('symbol_lib')!r} but {symbol_path} is "
                        "missing from the local checkout (stale or partial clone); falling back to "
                        "sourcing.asset_fetch.fetch_part_assets(lcsc_id, output_dir)."
                    ),
                }
            )
            continue
        from_library += 1
        result: dict[str, Any] = {
            "identifier": identifier,
            "source": "jlcpcb_library",
            "status": "ok" if (have_footprint and have_model) else "partial",
            "lcsc_id": lcsc,
            "match_type": match_type,
            "jlcpcb_class": entry.get("tier"),
            "symbol_library_path": str(symbol_path),
            "symbol_name": entry.get("symbol"),
            "footprint_path": str(footprint_path) if have_footprint else None,
            "footprint_name": footprint_name or None,
            "model_3d_path": str(model_path) if have_model else None,
            "license": "MIT (CDFER/JLCPCB-Kicad-Library)",
        }
        if not (have_footprint and have_model):
            missing = [n for n, ok in (("footprint", have_footprint), ("3d model", have_model)) if not ok]
            result["reason"] = f"local checkout has the symbol but not the {', '.join(missing)}"
        results.append(result)

    response: dict[str, Any] = {
        "status": "ok" if from_library == len(results) else ("partial" if from_library else "no_local_assets"),
        "count": len(results),
        "from_library": from_library,
        "fallback_to_easyeda2kicad": len(results) - from_library,
        "results": results,
        "library_available": layout is not None,
    }
    notes = library_notes(library_root)
    if notes:
        response["library_notes"] = notes
    if from_library != len(results):
        response["reason"] = (
            f"{len(results) - from_library} of {len(results)} parts have no local JLCPCB-Kicad-Library "
            "assets; use sourcing.asset_fetch.fetch_part_assets for those."
        )
    return response


# --- index generation ------------------------------------------------------


def _symbol_properties(symbol) -> dict[str, str]:  # noqa: ANN001 -- kicad_tools SExp
    props: dict[str, str] = {}
    for prop in symbol.find_children("property"):
        atoms = prop.get_atoms()
        if len(atoms) >= 2:
            props[str(atoms[0])] = str(atoms[1])
    return props


def _as_int(value: str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def build_index(library_root: str | Path, *, commit: str | None = None) -> dict:
    """Regenerate the part-tier index from a local checkout of
    CDFER/JLCPCB-Kicad-Library.

    Reads the library's `.kicad_sym` files through `kicad_tools`' own
    S-expression parser -- never by hand-parsing KiCad file text, per this
    project's standing guardrail. Read-only: nothing under `library_root` is
    modified.

    Deliberately drops the upstream `Stock`/`Price` properties: a GitHub
    Action rewrites those daily, so a checked-in snapshot of them would be
    wrong within a day and wrong silently. Tier, identity and asset names are
    what this index is for.
    """
    from kicad_tools.sexp.parser import parse_file  # local import: keeps module import cheap

    root = Path(library_root)
    layout = _library_layout(root) if root.is_dir() else None
    if layout is None:
        return {
            "status": "error",
            "reason": (
                f"{root} does not look like a checkout of {UPSTREAM_REPO_URL} "
                "(expected a 'symbols/' directory containing JLCPCB-*.kicad_sym files)"
            ),
        }

    parts: list[dict] = []
    skipped: list[dict] = []
    libraries: list[str] = []
    for sym_file in sorted(layout["symbols_dir"].glob("JLCPCB-*.kicad_sym")):
        lib_name = sym_file.stem
        libraries.append(lib_name)
        try:
            doc = parse_file(str(sym_file))
        except Exception as exc:  # noqa: BLE001 -- report, don't abort the whole build
            skipped.append({"library": lib_name, "reason": f"parse failed: {exc}"})
            continue
        for symbol in doc.find_children("symbol"):
            props = _symbol_properties(symbol)
            if not props:
                continue
            name = str(symbol.get_first_atom() or "")
            lcsc = (props.get("LCSC") or "").strip()
            raw_class = (props.get("Class") or "").strip().lower()
            tier = _CLASS_PROPERTY_TO_TIER.get(raw_class)
            tier_source = "symbol_property" if tier else None
            if tier is None and lcsc and lib_name.endswith("-Extended"):
                # The hand-added JLCPCB-Extended library carries no Class
                # property; its name is the only evidence of tier, which is
                # weaker, so record where the claim came from.
                tier = TIER_EXTENDED
                tier_source = "library_membership"
            if tier is None:
                # Manufacturing helpers (mouse bites, tooling holes, ...) are
                # not orderable parts -- not an error, just not indexable.
                skipped.append({"library": lib_name, "symbol": name, "reason": "no Class property and no LCSC id"})
                continue
            parts.append(
                {
                    "lcsc": lcsc.upper() or None,
                    "mpn": (props.get("Part") or props.get("Value") or "").strip() or None,
                    "manufacturer": (props.get("Manufacturer") or "").strip() or None,
                    "tier": tier,
                    "tier_source": tier_source,
                    "symbol_lib": lib_name,
                    "symbol": name,
                    "footprint": (props.get("Footprint") or "").strip() or None,
                    "minimum_qty": _as_int(props.get("Minimum Qty")),
                    "attrition_qty": _as_int(props.get("Attrition Qty")),
                }
            )

    counts = {TIER_BASIC: 0, TIER_PREFERRED: 0, TIER_EXTENDED: 0}
    for entry in parts:
        counts[entry["tier"]] += 1
    counts["total"] = len(parts)

    return {
        "status": "ok" if parts else "error",
        "reason": None if parts else f"no indexable symbols found under {layout['symbols_dir']}",
        "schema_version": 1,
        "source": {
            "repository": UPSTREAM_REPO_URL,
            "license": "MIT",
            "copyright": "Copyright (c) 2023 Chris (Chris Dirks)",
            "commit": commit,
            "symbol_libraries": libraries,
            "note": (
                "Derived from the library's own KiCad symbol properties. Volatile Stock/Price "
                "fields are deliberately excluded -- upstream refreshes them daily."
            ),
        },
        "counts": counts,
        "parts": parts,
        "skipped": skipped[:50],
        "skipped_count": len(skipped),
    }


def write_index(payload: dict, path: str | Path | None = None) -> dict:
    """Write a `build_index()` payload to disk as the vendored index.

    Evidence-gated: refuses to write a payload that did not build cleanly,
    so a parse failure can never quietly replace a good index with an empty
    one.
    """
    if payload.get("status") != "ok":
        return {"status": "error", "reason": f"refusing to write a non-ok index payload: {payload.get('reason')}"}
    target = Path(path) if path is not None else VENDORED_INDEX_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    serializable = {k: v for k, v in payload.items() if k not in {"status", "reason"}}
    target.write_text(json.dumps(serializable, indent=1, sort_keys=False, ensure_ascii=False), encoding="utf-8")
    load_index(refresh=True)
    return {"status": "ok", "path": str(target), "parts": payload["counts"]["total"], "counts": payload["counts"]}


def refresh_index(library_root: str | Path | None = None, *, commit: str | None = None) -> dict:
    """Rebuild the vendored index from a local checkout (env var or explicit)."""
    layout = resolve_library_root(library_root)
    if layout is None:
        return {
            "status": "unavailable",
            "reason": (
                f"no local checkout of {UPSTREAM_REPO_URL} found. Fetch the submodule with "
                f"`{SUBMODULE_FETCH_COMMAND}`, or clone it (or install it via KiCad's Plugin & "
                f"Content Manager) and set {JLCPCB_LIBRARY_ENV_VAR} to the directory containing "
                "'symbols/'."
            ),
            "library_notes": library_notes(library_root),
        }
    built = build_index(layout["root"], commit=commit)
    if built.get("status") != "ok":
        return built
    return write_index(built)


def iter_tiers() -> Iterable[str]:
    return (TIER_BASIC, TIER_PREFERRED, TIER_EXTENDED)
