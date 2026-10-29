"""Read a board's OWN design rules — netclasses, project constraints, `.kicad_dru`.

This is the concrete fix for archived backlog P1-18: before this module, every
verification tool checked geometry against hardcoded literals
(``clearance_mm=0.2`` etc.), so a "clean" result was clean against numbers
nobody involved in the design had chosen.

Three real sources, all of which KiCad itself consults:

1. ``<board>.kicad_pro`` → ``board.design_settings.rules`` — KiCad's
   *built-in* minimum constraints (``min_clearance``, ``min_track_width``,
   ``min_via_diameter``, ...).
2. ``<board>.kicad_pro`` → ``net_settings.classes`` — the netclasses. KiCad's
   ``clearance`` DRC test measures against the *applied netclass* clearance,
   not against ``min_clearance``, so this is usually the number that
   actually decides pass/fail.
3. ``<board>.kicad_dru`` — custom rules. KiCad applies the **most
   restrictive** of built-in and custom rules, which is the resolution this
   module reproduces.

Parsing note (ticket asked us to check before writing a parser): we write
no parser. ``.kicad_pro`` is JSON and is loaded via
``kicad_tools.core.project_file.load_project``; ``.kicad_dru`` is loaded via
``kicad_tools.core.sexp_file.load_design_rules``, which returns a real
``SExp`` tree. Everything below is tree traversal, never text matching.

Deliberate non-use of an available helper: ``kicad_tools.core.project_file``
also exposes ``get_netclass_definitions()``/``get_net_settings()``, but both
*fabricate and insert* a stock KiCad ``Default`` netclass (clearance 0.2,
track 0.25, via 0.6/0.3) when the project declares none. That invented data
is indistinguishable from a real board declaration once returned, which is
precisely the false-evidence failure this module exists to prevent — so
netclasses are read straight off the loaded dict instead, and "the project
declares no netclasses" stays visible as an empty list.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

# KiCad's own fallback minimums, used only when no board source states a
# value. These are the literals the verification tools used to hardcode
# unconditionally; they are still the last resort, but now they are labelled
# as such in every result instead of masquerading as the design's rules.
BUILTIN_DEFAULTS: dict[str, float] = {
    "clearance_mm": 0.2,
    "courtyard_margin_mm": 0.25,
    "edge_clearance_mm": 0.3,
    "hole_to_hole_mm": 0.5,
}

# `.kicad_pro` -> board.design_settings.rules keys, mapped to canonical names.
#
# `min_via_annular_width` maps to `via_annular_ring_mm`, NOT the generic
# `annular_ring_mm` bucket: it is unambiguously a via-only KiCad setting, and
# comparing it against JLCPCB's *PTH* annular-ring floor (a different,
# unrelated number for component through-hole pads) is exactly the real bug
# archived backlog P1-23 found and fixed. See CANONICAL_FROM_FAB_FIELD's docstring
# in models.py for the full reasoning.
_PROJECT_RULE_KEYS: dict[str, str] = {
    "min_clearance": "clearance_mm",
    "min_track_width": "track_width_mm",
    "min_via_diameter": "via_diameter_mm",
    "min_via_hole": "via_drill_mm",
    "min_via_annular_width": "via_annular_ring_mm",
    "min_through_hole_diameter": "through_hole_diameter_mm",
    "min_hole_to_hole": "hole_to_hole_mm",
    "min_copper_edge_clearance": "edge_clearance_mm",
    "min_silk_clearance": "silk_clearance_mm",
    "min_text_height": "text_height_mm",
    "min_text_thickness": "text_thickness_mm",
}

# `.kicad_pro` -> net_settings.classes[*] keys, mapped to canonical names.
_NETCLASS_KEYS: dict[str, str] = {
    "clearance": "clearance_mm",
    "track_width": "track_width_mm",
    "via_diameter": "via_diameter_mm",
    "via_drill": "via_drill_mm",
}

# `.kicad_dru` constraint types, mapped to canonical names. Only the
# dimensional families are mapped; length/skew/via-count/disallow/assertion
# rules are still reported raw under `custom_rules`, they just don't
# contribute to a single-scalar geometry minimum.
#
# `annular_width` is KiCad's DRU constraint type for via annular width
# specifically (there is no separate PTH-annular-ring DRU constraint type),
# so it maps to `via_annular_ring_mm` — same reasoning as
# `min_via_annular_width` above.
#
# archived backlog P1-24 (found on a real board, openMPPT v1.3): `hole_to_hole` and
# `hole_clearance` are two distinct, real KiCad DRU constraint types —
# `hole_to_hole` is hole-to-hole (pad/via perimeter) spacing, `hole_clearance`
# is copper-to-hole clearance (e.g. "track to PTH hole perimeter"). This
# table previously mapped `hole_clearance` to `hole_to_hole_mm` and had no
# entry for the real `hole_to_hole` type at all — the exact rule-matching bug
# the ticket describes, reproduced not by matching on rule *name* (this
# module never did that) but by conflating two constraint *types* in this
# table. `hole_clearance` is deliberately left unmapped here (no `None`
# entry needed — a missing key already means "not mapped"): there is no
# existing single-scalar canonical bucket for "copper to hole clearance" in
# this project's rule set, and inventing one is out of this fix's scope.
# Unmapped constraint types are still visible, raw, under `custom_rules`.
_DRU_CONSTRAINT_KEYS: dict[str, str] = {
    "clearance": "clearance_mm",
    "track_width": "track_width_mm",
    "via_diameter": "via_diameter_mm",
    "hole_size": "via_drill_mm",
    "annular_width": "via_annular_ring_mm",
    "hole_to_hole": "hole_to_hole_mm",
    "edge_clearance": "edge_clearance_mm",
    "silk_clearance": "silk_clearance_mm",
    "courtyard_clearance": "courtyard_margin_mm",
    "text_height": "text_height_mm",
    "text_thickness": "text_thickness_mm",
}

_MM_PER_MIL = 0.0254
_MM_PER_INCH = 25.4


class BoardRulesError(RuntimeError):
    """Raised when a board's rule sources exist but could not be read."""


def _to_mm(raw: Any) -> float | None:
    """Normalise a KiCad rule value (``0.2``, ``"0.2mm"``, ``"8mil"``) to mm."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    for suffix, scale in (("mm", 1.0), ("mil", _MM_PER_MIL), ("in", _MM_PER_INCH)):
        if text.endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * scale
            except ValueError:
                return None
    try:
        return float(text)
    except ValueError:
        return None


def _positive(value: float | None) -> float | None:
    """Treat 0.0 / negative / missing as *unspecified*.

    KiCad writes ``0.0`` into ``design_settings.rules`` for constraints the
    user never set (the ``simple_led`` fixture in this repo has
    ``min_copper_edge_clearance: 0.0``). Taking that literally would silently
    disable a check, which is worse than falling back to a labelled default.
    """
    if value is None:
        return None
    return value if value > 0 else None


@dataclass(frozen=True)
class ResolvedRule:
    """One canonical rule, its value, and where that value actually came from."""

    name: str
    value_mm: float | None
    source: str
    candidates: dict[str, float]

    def to_dict(self) -> dict:
        return {
            "value_mm": self.value_mm,
            "source": self.source,
            "candidates_mm": dict(self.candidates),
        }


def find_rule_files(pcb_path: str | Path) -> tuple[Path | None, Path | None]:
    """Locate the sibling ``.kicad_pro`` / ``.kicad_dru`` for a board.

    KiCad itself resolves both by same-directory, same-basename convention
    (``kicad-cli pcb drc`` auto-loads ``<board>.kicad_pro``), so this
    mirrors that rather than inventing a search path.
    """
    board = Path(pcb_path)
    pro = board.with_suffix(".kicad_pro")
    dru = board.with_suffix(".kicad_dru")
    return (pro if pro.is_file() else None, dru if dru.is_file() else None)


def read_project_rules(project_path: str | Path) -> tuple[dict[str, Any], list[dict], dict[str, Any]]:
    """Read ``design_settings.rules`` and ``net_settings.classes`` from a `.kicad_pro`.

    Returns ``(raw_rules, netclasses, raw_defaults)``. Missing sections come
    back empty — never fabricated (see the module docstring).
    """
    from kicad_tools.core.project_file import load_project

    data = load_project(Path(project_path))
    if not isinstance(data, dict):
        raise BoardRulesError(f"{project_path} did not parse to a JSON object")
    design_settings = (data.get("board") or {}).get("design_settings") or {}
    raw_rules = design_settings.get("rules") or {}
    raw_defaults = design_settings.get("defaults") or {}
    netclasses = (data.get("net_settings") or {}).get("classes") or []
    if not isinstance(netclasses, list):
        netclasses = []
    return dict(raw_rules), [nc for nc in netclasses if isinstance(nc, dict)], dict(raw_defaults)


def read_custom_rules(dru_path: str | Path) -> list[dict]:
    """Parse a ``.kicad_dru`` into structured rules via ``kicad_tools``' S-expression API.

    No hand-rolled text parsing: ``load_design_rules`` returns a real
    ``SExp`` tree and everything here is tree traversal.
    """
    from kicad_tools.core.sexp_file import load_design_rules
    from kicad_tools.sexp import SExp

    tree = load_design_rules(Path(dru_path))
    rules: list[dict] = []
    for child in tree.values:
        if not isinstance(child, SExp) or child.tag != "rule":
            continue
        name = str(child.values[0]) if child.values and isinstance(child.values[0], str) else None
        condition = child.find_child("condition")
        layer = child.find_child("layer")
        for sub in child.values:
            if not isinstance(sub, SExp) or sub.tag != "constraint":
                continue
            ctype = (
                str(sub.values[0])
                if sub.values and not isinstance(sub.values[0], SExp)
                else None
            )
            bounds: dict[str, float | None] = {}
            for bound in sub.values:
                if isinstance(bound, SExp) and bound.tag in ("min", "max", "opt") and bound.values:
                    bounds[bound.tag] = _to_mm(bound.values[0])
            rules.append(
                {
                    "name": name,
                    "constraint_type": ctype,
                    "min_mm": bounds.get("min"),
                    "max_mm": bounds.get("max"),
                    "opt_mm": bounds.get("opt"),
                    "condition": (
                        str(condition.values[0]) if condition and condition.values else None
                    ),
                    "layer": str(layer.values[0]) if layer and layer.values else None,
                    "canonical_rule": _DRU_CONSTRAINT_KEYS.get(ctype or ""),
                }
            )
    return rules


def _netclass_contributions(netclasses: list[dict]) -> dict[str, tuple[float, str]]:
    """Tightest value per canonical rule across every declared netclass.

    The *minimum* across netclasses, not the maximum: it is the smallest
    geometry that may legally appear anywhere on the board, so using it as a
    single scalar for a geometry pre-check cannot produce false positives.
    A stricter netclass being under-checked is fine here — ``pcb_run_drc``
    remains the authoritative per-net check.
    """
    out: dict[str, tuple[float, str]] = {}
    for nc in netclasses:
        nc_name = str(nc.get("name", "?"))
        for key, canonical in _NETCLASS_KEYS.items():
            value = _positive(_to_mm(nc.get(key)))
            if value is None:
                continue
            current = out.get(canonical)
            if current is None or value < current[0]:
                out[canonical] = (value, f"board_netclass:{nc_name}")
    return out


def resolve_effective_rules(
    project_rules: dict[str, Any],
    netclasses: list[dict],
    custom_rules: list[dict],
) -> dict[str, ResolvedRule]:
    """Combine the three board sources into one canonical rule set.

    Resolution is **most-restrictive-wins**, matching KiCad's documented
    behaviour ("the most restrictive of the built-in and custom rules
    applies"). Values of ``0.0`` or missing are treated as *unspecified*
    (see :func:`_positive`), never as "no limit".
    """
    contributions: dict[str, dict[str, float]] = {}

    def add(canonical: str, source: str, value: float | None) -> None:
        value = _positive(value)
        if value is None:
            return
        contributions.setdefault(canonical, {})[source] = value

    for key, canonical in _PROJECT_RULE_KEYS.items():
        add(canonical, "board_project_rules", _to_mm(project_rules.get(key)))

    for canonical, (value, source) in _netclass_contributions(netclasses).items():
        add(canonical, source, value)

    for rule in custom_rules:
        canonical = rule.get("canonical_rule")
        if canonical and rule.get("min_mm") is not None:
            label = f"board_custom_rule:{rule.get('name') or rule.get('constraint_type')}"
            existing = contributions.get(canonical, {}).get(label)
            value = float(rule["min_mm"])
            # Several conditioned rules can share a name/type; keep the most
            # restrictive of them, consistent with KiCad's own resolution.
            if existing is None or value > existing:
                add(canonical, label, value)

    resolved: dict[str, ResolvedRule] = {}
    for canonical, candidates in contributions.items():
        source, value = max(candidates.items(), key=lambda kv: kv[1])
        resolved[canonical] = ResolvedRule(
            name=canonical, value_mm=value, source=source, candidates=candidates
        )
    return resolved


def read_board_design_rules(pcb_path: str | Path) -> dict:
    """Read a board's own effective design rules from its project files.

    Returns a dict with ``status`` ``complete``/``partial``/``incomplete``:
    ``partial`` when the board exists but declares no rule sources at all
    (no ``.kicad_pro``, no ``.kicad_dru``), so a caller can never mistake
    "nothing was configured" for "checked and fine".
    """
    board = Path(pcb_path)
    if not board.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}

    project_path, dru_path = find_rule_files(board)
    warnings: list[str] = []
    project_rules: dict[str, Any] = {}
    netclasses: list[dict] = []
    defaults: dict[str, Any] = {}
    custom_rules: list[dict] = []

    if project_path is not None:
        try:
            project_rules, netclasses, defaults = read_project_rules(project_path)
        except Exception as exc:  # noqa: BLE001 - evidence-gated: report, don't raise
            warnings.append(f"could not read {project_path.name}: {exc}")
    if dru_path is not None:
        try:
            custom_rules = read_custom_rules(dru_path)
        except Exception as exc:  # noqa: BLE001 - evidence-gated: report, don't raise
            warnings.append(f"could not read {dru_path.name}: {exc}")

    effective = resolve_effective_rules(project_rules, netclasses, custom_rules)

    # A missing `.kicad_dru` is normal -- most boards have no custom rules --
    # but a missing `.kicad_pro` means the netclasses and built-in constraints
    # are genuinely unknown, which must never read as "checked and fine".
    if project_path is None:
        warnings.append(
            "board declares no design-rule sources (no sibling .kicad_pro); netclasses "
            "and built-in constraints are unknown"
            if dru_path is None
            else "no sibling .kicad_pro: netclasses and built-in constraints are unknown "
            "(only the .kicad_dru custom rules could be read)"
        )
        status = "partial"
    elif warnings:
        status = "partial"
    else:
        status = "complete"

    result = {
        "status": status,
        "pcb_path": str(board),
        "project_file": str(project_path) if project_path else None,
        "custom_rules_file": str(dru_path) if dru_path else None,
        "netclass_count": len(netclasses),
        "netclasses": [
            {
                "name": nc.get("name"),
                # canonical, unit-suffixed names (docs/ARCHITECTURE.md tool
                # conventions) rather than KiCad's raw unsuffixed keys
                **{
                    canonical: _to_mm(nc.get(key))
                    for key, canonical in _NETCLASS_KEYS.items()
                    if nc.get(key) is not None
                },
            }
            for nc in netclasses
        ],
        "project_rules_raw": project_rules,
        "project_defaults_raw": defaults,
        "custom_rule_count": len(custom_rules),
        "custom_rules": custom_rules,
        "effective_rules": {name: r.to_dict() for name, r in effective.items()},
        "resolution": (
            "most-restrictive-wins across board_project_rules, board_netclass:* "
            "(tightest netclass) and board_custom_rule:*; 0.0 or absent means "
            "unspecified, not 'no limit'"
        ),
        "warnings": warnings,
    }
    if status != "complete":
        result["reason"] = "; ".join(warnings)
    return result


__all__ = [
    "BUILTIN_DEFAULTS",
    "BoardRulesError",
    "ResolvedRule",
    "find_rule_files",
    "read_board_design_rules",
    "read_custom_rules",
    "read_project_rules",
    "resolve_effective_rules",
]
