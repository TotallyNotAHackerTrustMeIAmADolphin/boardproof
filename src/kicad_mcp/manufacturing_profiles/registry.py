"""Lookup for local manufacturer fab-capability profiles.

Evidence-gated by construction: an unknown manufacturer name raises
:class:`~kicad_mcp.manufacturing_profiles.models.UnknownManufacturerError`
naming every profile that *is* available. There is deliberately no
"generic"/"default" fallback profile — a silent fallback is exactly the
failure mode archived backlog P1-18 describes ("clean against numbers nobody
actually chose").

Adding a second fab is additive: write a module like ``jlcpcb.py`` and add
its profile to ``_PROFILES``. Nothing here is JLCPCB-specific.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .jlcpcb import JLCPCB_PROFILE
from .models import (
    CANONICAL_FROM_FAB_FIELD,
    FabDesignRules,
    ManufacturerProfile,
    UnknownManufacturerError,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from kicad_tools.manufacturers.base import DesignRules

_PROFILES: dict[str, ManufacturerProfile] = {
    JLCPCB_PROFILE.id: JLCPCB_PROFILE,
}

_ALIASES: dict[str, str] = {
    alias: profile.id for profile in _PROFILES.values() for alias in profile.aliases
}


def list_manufacturer_ids() -> list[str]:
    """Canonical profile ids, sorted."""
    return sorted(_PROFILES)


def list_accepted_names() -> list[str]:
    """Every name :func:`get_profile` accepts — canonical ids plus aliases."""
    return sorted(set(_PROFILES) | set(_ALIASES))


def list_profiles() -> list[ManufacturerProfile]:
    return [_PROFILES[k] for k in sorted(_PROFILES)]


def get_profile(manufacturer: str) -> ManufacturerProfile:
    """Resolve a manufacturer name (or alias) to its local profile.

    Raises:
        UnknownManufacturerError: always, for anything not in the registry —
            never a silent fallback to generic defaults.
    """
    key = (manufacturer or "").strip().lower().replace(" ", "")
    key = _ALIASES.get(key, key)
    profile = _PROFILES.get(key)
    if profile is None:
        raise UnknownManufacturerError(
            manufacturer, list_manufacturer_ids(), list_accepted_names()
        )
    return profile


def get_design_rules(
    manufacturer: str, layer_count: int = 2, copper_weight_oz: float = 1.0
) -> FabDesignRules:
    """Published capability set for one fab/layer-count/copper-weight combination."""
    return get_profile(manufacturer).get_rules(layer_count, copper_weight_oz)


def to_kicad_tools_design_rules(
    manufacturer: str, layer_count: int = 2, copper_weight_oz: float = 1.0
) -> "DesignRules":
    """Bridge a local profile into ``kicad_tools``' own ``DesignRules`` dataclass.

    Provided so callers that need ``kicad_tools``' downstream machinery
    (``manufacturers.dru_generator.generate_dru``,
    ``manufacturers.project_generator.build_project_rules``) can drive it
    from *our* verified numbers rather than their stale bundled YAML — see
    the discrepancy table in ``jlcpcb.py``'s docstring.

    Their dataclass has no representation for "not published", so any field
    we could not confirm falls back to *their* dataclass default. That is a
    real fidelity loss, which is exactly why it is confined to this one
    explicit conversion function instead of being the profile shape itself.
    """
    from kicad_tools.manufacturers.base import DesignRules

    rules = get_design_rules(manufacturer, layer_count, copper_weight_oz)
    mapped: dict[str, float] = {}
    # name-for-name where their field means the same thing as ours
    pairs = {
        "min_trace_width_mm": rules.min_trace_width_mm,
        "min_clearance_mm": rules.min_trace_spacing_mm,
        "min_via_drill_mm": rules.min_via_drill_mm,
        "min_via_diameter_mm": rules.min_via_diameter_mm,
        # kicad_tools' own DesignRules.min_annular_ring_mm is documented
        # under its "# Via constraints" section (it feeds KiCad's via
        # annular-width project setting when generating a .kicad_pro/.dru),
        # so it must be sourced from our VIA-derived field, not our PTH
        # field — same via-vs-PTH distinction as archived backlog P1-23.
        "min_annular_ring_mm": rules.min_via_annular_ring_mm,
        "min_hole_diameter_mm": rules.min_pth_drill_mm,
        "max_hole_diameter_mm": rules.max_pth_drill_mm,
        "min_copper_to_edge_mm": rules.min_copper_to_edge_mm,
        "min_hole_to_hole_mm": rules.min_pad_hole_to_hole_mm,
        "min_silkscreen_width_mm": rules.min_silkscreen_width_mm,
        "min_silkscreen_height_mm": rules.min_silkscreen_text_height_mm,
        "min_solder_mask_dam_mm": rules.min_solder_mask_dam_mm,
        "min_pad_size_mm": rules.min_pad_size_mm,
        "max_board_width_mm": rules.max_board_width_mm,
        "max_board_height_mm": rules.max_board_height_mm,
        "outer_copper_oz": rules.copper_weight_oz,
    }
    for key, value in pairs.items():
        if value is not None:
            mapped[key] = value
    # DesignRules' first five fields are required positional-or-keyword with
    # no defaults; every one of them is confirmed for every config we ship,
    # but fail loudly rather than constructing a half-populated object if a
    # future profile leaves one unconfirmed.
    required = (
        "min_trace_width_mm",
        "min_clearance_mm",
        "min_via_drill_mm",
        "min_via_diameter_mm",
        "min_annular_ring_mm",
    )
    missing = [k for k in required if k not in mapped]
    if missing:
        raise ValueError(
            f"cannot build kicad_tools DesignRules for {manufacturer} "
            f"{layer_count}-layer {copper_weight_oz}oz: unconfirmed required fields {missing}"
        )
    return DesignRules(**mapped)


__all__ = [
    "CANONICAL_FROM_FAB_FIELD",
    "get_design_rules",
    "get_profile",
    "list_accepted_names",
    "list_manufacturer_ids",
    "list_profiles",
    "to_kicad_tools_design_rules",
]
