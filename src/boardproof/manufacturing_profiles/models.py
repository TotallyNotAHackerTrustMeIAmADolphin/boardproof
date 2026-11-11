"""Data shapes for local manufacturer fab-capability profiles.

Deliberately *not* a re-export of ``kicad_tools.manufacturers.DesignRules``
(see the package docstring in ``__init__.py`` for the full finding): that
dataclass has no per-field provenance and no way to say "this fab does not
publish this number", both of which this ticket requires. This module keeps
its own shape and offers an explicit converter instead, so a value can never
silently look verified when it isn't.

Every numeric field is ``float | None``. ``None`` means **the manufacturer
does not publish this number on the cited source page** — never "we guessed
zero" and never "we forgot". Any field that is ``None`` also appears in
``unconfirmed_fields`` and carries a note in ``field_notes`` saying so.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

# Every numeric capability field on FabDesignRules, in declaration order.
# Derived at import time from the dataclass itself so it can't drift.
_NON_NUMERIC_FIELDS = frozenset(
    {"config_id", "description", "copper_weight_oz", "field_notes"}
)


class UnknownManufacturerError(LookupError):
    """Raised when a manufacturer name has no local profile.

    Carries ``available`` so callers can turn it into an evidence-gated
    tool error that names what *is* known, rather than silently falling
    back to generic defaults.
    """

    def __init__(
        self, requested: str, available: list[str], accepted_names: list[str] | None = None
    ) -> None:
        self.requested = requested
        self.available = available
        self.accepted_names = accepted_names or list(available)
        extra = ""
        if self.accepted_names != available:
            extra = f" (accepted spellings: {', '.join(self.accepted_names)})"
        super().__init__(
            f"no local design-rule profile for manufacturer {requested!r}. "
            f"Available: {', '.join(available)}{extra}"
        )


class UnsupportedConfigurationError(LookupError):
    """Raised when a profile exists but has no rules for the asked-for config."""

    def __init__(self, manufacturer: str, layer_count: int, copper_weight_oz: float, available: list[str]) -> None:
        self.manufacturer = manufacturer
        self.layer_count = layer_count
        self.copper_weight_oz = copper_weight_oz
        self.available = available
        super().__init__(
            f"{manufacturer} has no published rule set for layer_count={layer_count}, "
            f"copper_weight_oz={copper_weight_oz}. Available configurations: "
            f"{', '.join(available)}"
        )


@dataclass(frozen=True)
class FabDesignRules:
    """One published capability set for a fab, at a given layer/copper config.

    All lengths in millimetres. ``None`` == not published on the cited
    source (see module docstring) — treat as "unknown", never as "no limit".
    """

    config_id: str
    description: str
    copper_weight_oz: float

    # --- Traces -------------------------------------------------------
    min_trace_width_mm: float | None = None
    min_trace_spacing_mm: float | None = None

    # --- Vias / holes -------------------------------------------------
    min_via_diameter_mm: float | None = None
    min_via_drill_mm: float | None = None
    # PTH (component through-hole pad) annular ring, as published under
    # JLCPCB's "PTH annular ring" rows. Do NOT compare a board's *via*
    # annular-width rule against this — see min_via_annular_ring_mm below
    # and archived backlog P1-23 for the real bug this distinction fixes.
    min_annular_ring_mm: float | None = None
    recommended_annular_ring_mm: float | None = None
    # Via annular ring. JLCPCB publishes no separate via-specific annular
    # ring minimum (confirmed 2026-09-13, re-fetching the same source page) —
    # this is a DERIVED value, (min_via_diameter_mm - min_via_drill_mm) / 2,
    # not a verbatim quote. field_notes says so explicitly wherever set.
    min_via_annular_ring_mm: float | None = None
    min_pth_drill_mm: float | None = None
    max_pth_drill_mm: float | None = None
    min_npth_drill_mm: float | None = None
    min_via_hole_to_hole_mm: float | None = None
    min_pad_hole_to_hole_mm: float | None = None

    # --- Board edge ---------------------------------------------------
    min_copper_to_edge_mm: float | None = None
    min_copper_to_vcut_edge_mm: float | None = None

    # --- Silkscreen ---------------------------------------------------
    min_silkscreen_width_mm: float | None = None
    min_silkscreen_text_height_mm: float | None = None
    min_silkscreen_to_pad_mm: float | None = None

    # --- Solder mask --------------------------------------------------
    min_solder_mask_dam_mm: float | None = None
    min_solder_mask_to_trace_mm: float | None = None

    # --- Pads ---------------------------------------------------------
    min_pad_size_mm: float | None = None

    # --- Board outline ------------------------------------------------
    max_board_width_mm: float | None = None
    max_board_height_mm: float | None = None
    min_board_width_mm: float | None = None
    min_board_height_mm: float | None = None

    # Per-field provenance: field name -> verbatim quote from the cited
    # source page, or an explicit "NOT STATED on <url>" note.
    field_notes: dict[str, str] = field(default_factory=dict)

    @property
    def numeric_fields(self) -> list[str]:
        return [f.name for f in dataclasses.fields(self) if f.name not in _NON_NUMERIC_FIELDS]

    @property
    def values_mm(self) -> dict[str, float | None]:
        return {name: getattr(self, name) for name in self.numeric_fields}

    @property
    def unconfirmed_fields(self) -> list[str]:
        """Fields the cited source did not publish (value is ``None``)."""
        return [name for name, value in self.values_mm.items() if value is None]

    def to_dict(self) -> dict:
        return {
            "config_id": self.config_id,
            "description": self.description,
            "copper_weight_oz": self.copper_weight_oz,
            "values_mm": self.values_mm,
            "unconfirmed_fields": self.unconfirmed_fields,
            "field_notes": dict(self.field_notes),
        }


# Maps this project's canonical design-rule keys (the ones the verification
# tools actually resolve and pass to the geometry checkers) onto the
# FabDesignRules field that expresses the same constraint. Kept in one place
# so `pcb_check_placement`, the manufacturability comparison and the board
# rule reader can never disagree about which fab number means what.
#
# Note `hole_to_hole_mm` maps to the *pad* hole-to-hole value, not the via
# one: the placement checker measures drilled pad holes on footprints, and
# KiCad's own `min_hole_to_hole` project setting is a single board-wide
# value with no via/pad split of its own, so the stricter (pad) figure is
# used as the single conservative floor. Fabs publish these as two
# different numbers (JLCPCB: 0.45mm pad vs 0.2mm via) — the via-specific
# number is still available on `FabDesignRules.min_via_hole_to_hole_mm` for
# a caller that wants it directly; it just isn't blended into this table
# because there is no unambiguous via-specific board-side source for it.
#
# `annular_ring_mm`/`via_annular_ring_mm` is the real split archived backlog P1-23
# found missing: a board's *via* annular-width rule (KiCad's
# `min_via_annular_width` project setting, or a `.kicad_dru`
# `annular_width` constraint — both unambiguously via-only by name) must
# never be compared against JLCPCB's *PTH* annular-ring floor, because vias
# and plated through-holes have different, unrelated minimums (and JLCPCB
# doesn't even publish a via-specific number — see
# `min_via_annular_ring_mm`'s docstring in `FabDesignRules`). Those board
# rules now feed `via_annular_ring_mm` instead. `annular_ring_mm` (PTH)
# currently has no board-side source at all — KiCad has no single project
# setting for "PTH annular ring" the way it does for vias — so it will
# honestly report `board_rule_unspecified` rather than being populated
# from an unrelated via number, which is the actual bug fix.
CANONICAL_FROM_FAB_FIELD: dict[str, str] = {
    "clearance_mm": "min_trace_spacing_mm",
    "track_width_mm": "min_trace_width_mm",
    "via_diameter_mm": "min_via_diameter_mm",
    "via_drill_mm": "min_via_drill_mm",
    "annular_ring_mm": "min_annular_ring_mm",
    "via_annular_ring_mm": "min_via_annular_ring_mm",
    "hole_to_hole_mm": "min_pad_hole_to_hole_mm",
    "edge_clearance_mm": "min_copper_to_edge_mm",
    "silk_clearance_mm": "min_silkscreen_to_pad_mm",
    "text_height_mm": "min_silkscreen_text_height_mm",
    "text_thickness_mm": "min_silkscreen_width_mm",
}


@dataclass(frozen=True)
class ManufacturerProfile:
    """A fab's published capabilities, as a local static snapshot.

    Static local data by design (explicit project requirement): these are
    published, largely-static capability specs, not live pricing/stock, so
    they are not fetched per call. ``source_url``/``source_fetched_date``
    record exactly what was read and when, so staleness is visible rather
    than assumed away.
    """

    id: str
    display_name: str
    website: str
    source_url: str
    source_fetched_date: str  # ISO date the source_url was actually read
    configs: dict[str, FabDesignRules]
    # The page may state a *range* rather than an enumeration; both are
    # recorded so nothing has to be inferred by the caller.
    supported_layer_count_min: int
    supported_layer_count_max: int
    supported_layer_counts_note: str
    aliases: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    # config_id resolver: (layer_count_predicate, copper_oz) -> config_id.
    # Stored as a plain tuple of rules rather than code so a second fab can
    # be added by adding data, not by subclassing anything.
    config_selectors: tuple[tuple[int, int, float, str], ...] = ()

    def get_rules(self, layer_count: int = 2, copper_weight_oz: float = 1.0) -> FabDesignRules:
        """Resolve the published rule set for a layer count / copper weight.

        Raises:
            UnsupportedConfigurationError: if the fab publishes nothing for
                this configuration. Never returns a nearby-but-different
                rule set silently.
        """
        if not (self.supported_layer_count_min <= layer_count <= self.supported_layer_count_max):
            raise UnsupportedConfigurationError(
                self.display_name, layer_count, copper_weight_oz, sorted(self.configs)
            )
        for low, high, oz, config_id in self.config_selectors:
            if low <= layer_count <= high and abs(oz - copper_weight_oz) < 1e-9:
                return self.configs[config_id]
        raise UnsupportedConfigurationError(
            self.display_name, layer_count, copper_weight_oz, sorted(self.configs)
        )

    def canonical_floor(self, layer_count: int = 2, copper_weight_oz: float = 1.0) -> dict[str, float | None]:
        """This fab's capability floor, keyed by this project's canonical rule names."""
        rules = self.get_rules(layer_count, copper_weight_oz)
        return {
            canonical: getattr(rules, fab_field)
            for canonical, fab_field in CANONICAL_FROM_FAB_FIELD.items()
        }

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "display_name": self.display_name,
            "website": self.website,
            "source_url": self.source_url,
            "source_fetched_date": self.source_fetched_date,
            "aliases": list(self.aliases),
            "supported_layer_count_min": self.supported_layer_count_min,
            "supported_layer_count_max": self.supported_layer_count_max,
            "supported_layer_counts_note": self.supported_layer_counts_note,
            "available_configs": sorted(self.configs),
            "notes": list(self.notes),
        }
