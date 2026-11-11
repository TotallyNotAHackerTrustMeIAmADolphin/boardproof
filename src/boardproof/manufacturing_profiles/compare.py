"""Compare a board's own rules against a fab's published capability floor.

The precedence this whole package is built around, stated once:

* **The board's own rules are authoritative for what KiCad checks.** A DRC
  run measures the design against its netclasses, its project constraints
  and its ``.kicad_dru`` — nothing else.
* **A manufacturer profile is a capability FLOOR, not a rule set.** It says
  what the fab can physically produce. A design can be perfectly DRC-clean
  against its own rules and still be unmanufacturable at a given fab,
  because its own rules were looser than the fab's process.

So the two are never merged into one verdict. ``compare_to_profile`` reports
them side by side, and every number in every result carries the source it
came from — an agent must never be unable to tell whether ``0.2`` was the
designer's netclass, the fab's floor, or a built-in fallback.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .board_rules import BUILTIN_DEFAULTS, read_board_design_rules
from .models import CANONICAL_FROM_FAB_FIELD
from .registry import get_profile

# Human-readable meaning of each verdict, echoed into results so an agent
# doesn't have to infer semantics from a bare enum string.
VERDICTS: dict[str, str] = {
    "ok": "the board's rule is at or above the fab's published minimum",
    "below_fab_floor": (
        "the board's rule is BELOW the fab's published minimum - DRC-clean against "
        "the board's own rules would still not be manufacturable here"
    ),
    "board_rule_unspecified": (
        "the board declares no rule for this constraint, so it cannot be compared - "
        "this is an unverified gap, not a pass"
    ),
    "not_published_by_fab": (
        "the fab does not publish this number on the cited source page, so no floor "
        "exists to compare against"
    ),
}


def resolve_check_rules(
    explicit: dict[str, float | None],
    board_result: dict | None,
    fab_floor: dict[str, float | None] | None,
    *,
    manufacturer_label: str | None = None,
) -> dict[str, dict]:
    """Resolve the rule values a geometry check should actually run with.

    Precedence, highest first:

    1. an explicit argument the caller passed (caller intent always wins),
    2. the board's own effective rule (authoritative for what KiCad checks),
    3. the manufacturer profile's floor (used only where the board is silent),
    4. this project's built-in fallback literal.

    Returns ``{rule_name: {"value_mm": float, "source": str}}`` — the source
    label is mandatory, never omitted.
    """
    board_effective = (board_result or {}).get("effective_rules") or {}
    resolved: dict[str, dict] = {}
    for name, explicit_value in explicit.items():
        if explicit_value is not None:
            resolved[name] = {"value_mm": float(explicit_value), "source": "explicit_argument"}
            continue
        board_rule = board_effective.get(name)
        if board_rule and board_rule.get("value_mm") is not None:
            resolved[name] = {
                "value_mm": float(board_rule["value_mm"]),
                "source": board_rule.get("source", "board"),
            }
            continue
        fab_value = (fab_floor or {}).get(name)
        if fab_value is not None:
            resolved[name] = {
                "value_mm": float(fab_value),
                "source": f"manufacturer_profile:{manufacturer_label or '?'}",
            }
            continue
        default = BUILTIN_DEFAULTS.get(name)
        if default is not None:
            resolved[name] = {"value_mm": float(default), "source": "builtin_default"}
    return resolved


def compare_to_profile(
    pcb_path: str | Path,
    manufacturer: str,
    layer_count: int = 2,
    copper_weight_oz: float = 1.0,
    board_result: dict | None = None,
) -> dict:
    """Check one board's effective rules against a fab's capability floor.

    Raises nothing for a bad manufacturer — that is the caller's job to turn
    into an evidence-gated tool error; this function propagates
    :class:`UnknownManufacturerError` so it cannot be swallowed silently.
    """
    profile = get_profile(manufacturer)
    fab_rules = profile.get_rules(layer_count, copper_weight_oz)
    floor = profile.canonical_floor(layer_count, copper_weight_oz)

    board = board_result if board_result is not None else read_board_design_rules(pcb_path)
    if board.get("status") == "incomplete":
        return {
            "status": "incomplete",
            "reason": board.get("reason", "board design rules could not be read"),
            "pcb_path": str(pcb_path),
        }
    board_effective = board.get("effective_rules") or {}

    findings: list[dict] = []
    for canonical, fab_field in CANONICAL_FROM_FAB_FIELD.items():
        fab_value = floor.get(canonical)
        board_rule = board_effective.get(canonical) or {}
        board_value = board_rule.get("value_mm")
        if fab_value is None:
            verdict = "not_published_by_fab"
        elif board_value is None:
            verdict = "board_rule_unspecified"
        elif board_value < fab_value - 1e-9:
            verdict = "below_fab_floor"
        else:
            verdict = "ok"
        findings.append(
            {
                "rule": canonical,
                "verdict": verdict,
                "board_value_mm": board_value,
                "board_value_source": board_rule.get("source"),
                "fab_floor_mm": fab_value,
                "fab_floor_source": f"{profile.display_name} {fab_rules.config_id} "
                f"({profile.source_url}, fetched {profile.source_fetched_date})",
                "fab_field": fab_field,
                "fab_field_note": fab_rules.field_notes.get(fab_field),
            }
        )

    below = [f for f in findings if f["verdict"] == "below_fab_floor"]
    unspecified = [f for f in findings if f["verdict"] == "board_rule_unspecified"]
    unpublished = [f for f in findings if f["verdict"] == "not_published_by_fab"]

    # Evidence-gated: only claim "manufacturable" when every comparable rule
    # was actually comparable. Anything unknown downgrades to incomplete.
    if below:
        manufacturable = False
        status = "complete"
    elif unspecified or board.get("status") != "complete":
        manufacturable = None
        status = "partial"
    else:
        manufacturable = True
        status = "complete"

    result: dict[str, Any] = {
        "status": status,
        "pcb_path": str(pcb_path),
        "manufacturer": profile.id,
        "manufacturer_display_name": profile.display_name,
        "layer_count": layer_count,
        "copper_weight_oz": copper_weight_oz,
        "fab_config_id": fab_rules.config_id,
        "source_url": profile.source_url,
        "source_fetched_date": profile.source_fetched_date,
        "meets_fab_floor": manufacturable,
        "precedence": (
            "The board's own rules are authoritative for what KiCad's DRC checks. "
            "The manufacturer profile is a capability floor only: a design can be "
            "DRC-clean against its own rules and still be unmanufacturable at this fab."
        ),
        "verdict_meanings": dict(VERDICTS),
        "below_fab_floor_count": len(below),
        "below_fab_floor": below,
        "unspecified_count": len(unspecified),
        "not_published_by_fab_count": len(unpublished),
        "findings": findings,
        "board_rules_status": board.get("status"),
        "board_rules_warnings": board.get("warnings", []),
    }
    if status != "complete":
        reasons = []
        if unspecified:
            reasons.append(
                f"{len(unspecified)} rule(s) not declared by the board, so they could "
                "not be compared against the fab floor"
            )
        if board.get("status") != "complete":
            reasons.extend(board.get("warnings", []) or ["board rule sources incomplete"])
        result["reason"] = "; ".join(reasons)
    return result


__all__ = ["VERDICTS", "compare_to_profile", "resolve_check_rules"]
