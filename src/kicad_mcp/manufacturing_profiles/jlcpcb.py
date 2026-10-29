"""JLCPCB fab-capability profile — verified against JLCPCB's own page.

SOURCE OF RECORD
----------------
URL:      https://jlcpcb.com/capabilities/pcb-capabilities
Fetched:  2026-09-04
Verified: every number below was read off that page during two independent
          extraction passes over the same fetch, and the two passes agreed.
          Values the page does **not** state are ``None`` with a
          ``NOT STATED`` note in ``field_notes`` — never a guess, never a
          recalled-from-memory number. See archived backlog P1-21 for why this
          project treats an honest gap as worth more than a confident
          wrong number.

Scope: rigid FR-4 PCB fabrication capabilities. Assembly (PCBA) limits,
pricing, lead times and the LCSC parts library are deliberately out of
scope here — they are not design *rules*, and pricing/stock is exactly the
live data this static profile must not pretend to carry.

WHY THIS FILE EXISTS AT ALL (finding, 2026-09-04)
-------------------------------------------------
``kicad_tools.manufacturers`` already ships a JLCPCB profile
(``manufacturers/data/jlcpcb.yaml``, "Last verified: 2026-01-16"), so this
package is *not* filling an empty hole. It exists because that data
disagrees materially with what JLCPCB publishes today, always in the
conservative direction, and carries no per-field provenance:

    field                     kicad_tools     JLCPCB page (2026-09-04)
    min trace width  (2L 1oz) 0.127 mm        0.10 mm
    min spacing      (2L 1oz) 0.127 mm        0.10 mm
    min trace width  (4L 1oz) 0.1016 mm       0.09 mm
    min via drill             0.30 mm         0.15 mm
    min via diameter          0.60 mm         0.25 mm
    min annular ring (2L)     0.15 mm         0.18 mm  <-- LOOSER upstream
    min copper to edge        0.30 mm         0.20 mm
    min hole-to-hole          0.50 mm         0.45 mm (pad) / 0.20 mm (via)
    min silkscreen-to-pad     (absent)        0.15 mm
    max board size            400 x 500 mm    670 x 600 mm (2L)

Most of those are safe-but-stale (a design that clears kicad_tools' floor
clears JLCPCB's), but ``min_annular_ring`` for 2-layer is the dangerous
direction: upstream's 0.15 mm is **below** JLCPCB's published 0.18 mm
absolute minimum, so a board could pass an upstream-rules check and still be
below the fab's floor. That single field is the reason this profile is
authoritative here rather than a thin wrapper over upstream.
``to_kicad_tools_design_rules()`` in ``registry.py`` bridges back to their
dataclass for callers that need it (e.g. their ``.kicad_dru`` generator).
"""

from __future__ import annotations

from .models import FabDesignRules, ManufacturerProfile

_URL = "https://jlcpcb.com/capabilities/pcb-capabilities"


def _not_stated(*fields: str) -> dict[str, str]:
    return {f: f"NOT STATED on {_URL} (fetched 2026-09-04) - recorded as None, not guessed" for f in fields}


# Values shared by every configuration below because JLCPCB publishes them
# once, not per copper weight / layer count. Repeating them per-config with
# a note beats silently inheriting them.
_SHARED_NOTES = {
    "min_via_hole_to_hole_mm": 'verbatim: "Via Hole-to-Hole Spacing: 0.2mm"',
    "min_pad_hole_to_hole_mm": 'verbatim: "Pad Hole-to-Hole Spacing: 0.45mm"',
    "min_copper_to_edge_mm": 'verbatim: "Copper clearance from routed board edges: >=0.2 mm"',
    "min_copper_to_vcut_edge_mm": 'verbatim: "Copper clearance from V-cut board edges: >=0.4 mm"',
    "min_silkscreen_width_mm": 'verbatim: "Min silkscreen line width: >=0.15mm"',
    "min_silkscreen_text_height_mm": 'verbatim: "Min silkscreen text height: 40 mil (1.0mm)"',
    "min_silkscreen_to_pad_mm": 'verbatim: "Pad To Silkscreen: 0.15mm"',
    "min_solder_mask_to_trace_mm": (
        'verbatim: "at least 0.09 mm clearance between soldermask openings and neighboring traces"'
    ),
    "min_npth_drill_mm": 'verbatim: "Min non-plated holes: 0.50mm"',
    "min_board_width_mm": 'verbatim: "Min board size (FR4/Rogers/PTFE): 3 x 3 mm"',
    "min_board_height_mm": 'verbatim: "Min board size (FR4/Rogers/PTFE): 3 x 3 mm"',
}

# Shared numeric values for the fields above.
_SHARED = dict(
    min_via_hole_to_hole_mm=0.2,
    min_pad_hole_to_hole_mm=0.45,
    min_copper_to_edge_mm=0.2,
    min_copper_to_vcut_edge_mm=0.4,
    min_silkscreen_width_mm=0.15,
    min_silkscreen_text_height_mm=1.0,
    min_silkscreen_to_pad_mm=0.15,
    min_solder_mask_to_trace_mm=0.09,
    min_npth_drill_mm=0.5,
    min_board_width_mm=3.0,
    min_board_height_mm=3.0,
    # NOT published anywhere on the capabilities page. A minimum SMD pad
    # dimension is an IPC/land-pattern concern, not something JLCPCB states
    # as a fab limit -- so it stays None rather than inheriting the 0.25mm
    # that kicad_tools' YAML carries with no cited source.
    min_pad_size_mm=None,
)


def _notes(**extra: str) -> dict[str, str]:
    merged = dict(_SHARED_NOTES)
    merged.update(extra)
    return merged


def _derived_via_annular_ring_note(diameter_mm: float, drill_mm: float) -> str:
    value = (diameter_mm - drill_mm) / 2
    return (
        f"DERIVED, not verbatim: JLCPCB publishes no separate via-specific annular-ring "
        f"minimum (re-checked {_URL} on 2026-09-13, archived backlog P1-23) — only PTH annular "
        f"ring (a different, unrelated number for component through-hole pads, see "
        f"min_annular_ring_mm) and via diameter/drill. This is the geometric floor those "
        f"two verbatim numbers imply: (min_via_diameter_mm {diameter_mm}mm - "
        f"min_via_drill_mm {drill_mm}mm) / 2 = {value}mm."
    )


# --------------------------------------------------------------------------
# 1 layer (single-sided). JLCPCB states distinctly looser drill/via limits
# here than for 2-layer -- and single-sided boards are NPTH-only.
# --------------------------------------------------------------------------
_1L_1OZ = FabDesignRules(
    config_id="1layer_1oz",
    description="1-layer FR-4, 1 oz outer copper",
    copper_weight_oz=1.0,
    min_trace_width_mm=0.10,
    min_trace_spacing_mm=0.10,
    min_via_diameter_mm=0.5,
    min_via_drill_mm=0.3,
    min_pth_drill_mm=0.3,
    max_pth_drill_mm=6.3,
    min_solder_mask_dam_mm=0.10,
    max_board_width_mm=606.0,
    max_board_height_mm=510.0,
    **_SHARED,
    field_notes=_notes(
        min_trace_width_mm='verbatim: "Min. track width and spacing (1 oz): 0.10 / 0.10 mm (4 / 4 mil)" - 1- and 2-layer row',
        min_trace_spacing_mm='verbatim: "Min. track width and spacing (1 oz): 0.10 / 0.10 mm (4 / 4 mil)" - 1- and 2-layer row',
        min_via_diameter_mm='verbatim: 1-layer (NPTH only) "0.3 mm hole / 0.5 mm diameter"',
        min_via_drill_mm='verbatim: 1-layer (NPTH only) "0.3 mm hole / 0.5 mm diameter"',
        min_pth_drill_mm='verbatim: "PTH drill diameter range: 0.3 - 6.3 mm (1-layer)"',
        max_pth_drill_mm='verbatim: "PTH drill diameter range: 0.3 - 6.3 mm (1-layer)"',
        min_solder_mask_dam_mm='verbatim: "Min solder mask bridge (1oz): 0.10 mm"',
        max_board_width_mm='verbatim: "Max board size (FR4, 1-layer): 606 x 510 mm"',
        max_board_height_mm='verbatim: "Max board size (FR4, 1-layer): 606 x 510 mm"',
        min_via_annular_ring_mm=(
            f"NOT STATED on {_URL} (fetched 2026-09-04) - recorded as None, not guessed. "
            "N/A by construction, not just unpublished: 1-layer boards are single-sided "
            "(NPTH only per the page's own via row) - a plated via with an annular ring "
            "is a 2+-layer concept, so this stays unset rather than derived from the "
            "NPTH hole/diameter numbers above."
        ),
        **_not_stated("min_annular_ring_mm", "recommended_annular_ring_mm", "min_pad_size_mm"),
    ),
)

# --------------------------------------------------------------------------
# 2 layer
# --------------------------------------------------------------------------
_2L_1OZ = FabDesignRules(
    config_id="2layer_1oz",
    description="2-layer FR-4, 1 oz outer copper",
    copper_weight_oz=1.0,
    min_trace_width_mm=0.10,
    min_trace_spacing_mm=0.10,
    min_via_diameter_mm=0.25,
    min_via_drill_mm=0.15,
    # ABSOLUTE minimum, not the recommended value. Using the recommendation
    # as the floor would falsely reject manufacturable boards; using the
    # absolute minimum as a *target* would be poor practice -- hence both
    # are carried, and the comparison uses the absolute minimum.
    min_annular_ring_mm=0.18,
    recommended_annular_ring_mm=0.25,
    min_via_annular_ring_mm=0.05,
    min_pth_drill_mm=0.15,
    max_pth_drill_mm=6.3,
    min_solder_mask_dam_mm=0.10,
    max_board_width_mm=670.0,
    max_board_height_mm=600.0,
    **_SHARED,
    field_notes=_notes(
        min_trace_width_mm='verbatim: "Min. track width and spacing (1 oz): 0.10 / 0.10 mm (4 / 4 mil)" - 1- and 2-layer row',
        min_trace_spacing_mm='verbatim: "Min. track width and spacing (1 oz): 0.10 / 0.10 mm (4 / 4 mil)" - 1- and 2-layer row',
        min_via_diameter_mm='verbatim: "Min. Via hole size/diameter: 0.15mm / 0.25mm" - 2-layer row',
        min_via_drill_mm='verbatim: "Min. Via hole size/diameter: 0.15mm / 0.25mm" - 2-layer row',
        min_annular_ring_mm='verbatim: "PTH annular ring (2-layer, 1oz): Recommended 0.25 mm; absolute minimum 0.18 mm"',
        recommended_annular_ring_mm='verbatim: "PTH annular ring (2-layer, 1oz): Recommended 0.25 mm; absolute minimum 0.18 mm"',
        min_via_annular_ring_mm=_derived_via_annular_ring_note(0.25, 0.15),
        min_pth_drill_mm='verbatim: "PTH drill diameter range: 0.15 - 6.3 mm (2+ layers)"',
        max_pth_drill_mm='verbatim: "PTH drill diameter range: 0.15 - 6.3 mm (2+ layers)"',
        min_solder_mask_dam_mm='verbatim: "Min solder mask bridge (1oz): 0.10 mm"',
        max_board_width_mm=(
            'verbatim: "Max board size (FR4, 2-layer): 670 x 600 mm"; the page also lists '
            "1020 x 600 mm for a separate large-format process - the smaller, universally "
            "available figure is used as the capability limit"
        ),
        max_board_height_mm='verbatim: "Max board size (FR4, 2-layer): 670 x 600 mm"',
        **_not_stated("min_pad_size_mm"),
    ),
)

_2L_2OZ = FabDesignRules(
    config_id="2layer_2oz",
    description="2-layer FR-4, 2 oz outer copper",
    copper_weight_oz=2.0,
    min_trace_width_mm=0.16,
    min_trace_spacing_mm=0.16,
    min_via_diameter_mm=0.25,
    min_via_drill_mm=0.15,
    min_annular_ring_mm=0.254,
    min_via_annular_ring_mm=0.05,
    min_pth_drill_mm=0.15,
    max_pth_drill_mm=6.3,
    max_board_width_mm=670.0,
    max_board_height_mm=600.0,
    **_SHARED,
    field_notes=_notes(
        min_trace_width_mm='verbatim: "Min. track width and spacing (2 oz): 0.16 / 0.16 mm (6.5 / 6.5 mil)" - 1- and 2-layer row',
        min_trace_spacing_mm='verbatim: "Min. track width and spacing (2 oz): 0.16 / 0.16 mm (6.5 / 6.5 mil)" - 1- and 2-layer row',
        min_via_diameter_mm=(
            'verbatim: "Min. Via hole size/diameter: 0.15mm / 0.25mm" - the via row is not '
            "broken out by copper weight, so the same figure applies at 2 oz"
        ),
        min_via_drill_mm=(
            'verbatim: "Min. Via hole size/diameter: 0.15mm / 0.25mm" - the via row is not '
            "broken out by copper weight, so the same figure applies at 2 oz"
        ),
        min_annular_ring_mm='verbatim: "PTH annular ring (2-layer, 2 oz): 0.254 mm or above"',
        min_via_annular_ring_mm=_derived_via_annular_ring_note(0.25, 0.15),
        min_pth_drill_mm='verbatim: "PTH drill diameter range: 0.15 - 6.3 mm (2+ layers)"',
        max_pth_drill_mm='verbatim: "PTH drill diameter range: 0.15 - 6.3 mm (2+ layers)"',
        max_board_width_mm='verbatim: "Max board size (FR4, 2-layer): 670 x 600 mm"',
        max_board_height_mm='verbatim: "Max board size (FR4, 2-layer): 670 x 600 mm"',
        **_not_stated(
            # The 0.10mm solder-mask bridge figure is published for 1 oz
            # only; the page states no 2 oz equivalent.
            "min_solder_mask_dam_mm",
            "recommended_annular_ring_mm",
            "min_pad_size_mm",
        ),
    ),
)

# --------------------------------------------------------------------------
# Multilayer (4+). JLCPCB publishes one "multilayer" column, not one per
# layer count -- 4-layer and 6-layer share the same trace/via/annular
# limits. Only the maximum board size differs (4-layer vs 6+), so those are
# split into two configs that are otherwise identical.
# --------------------------------------------------------------------------
_MULTI_1OZ_COMMON = dict(
    copper_weight_oz=1.0,
    min_trace_width_mm=0.09,
    min_trace_spacing_mm=0.09,
    min_via_diameter_mm=0.25,
    min_via_drill_mm=0.15,
    min_annular_ring_mm=0.15,
    recommended_annular_ring_mm=0.20,
    min_via_annular_ring_mm=0.05,
    min_pth_drill_mm=0.15,
    max_pth_drill_mm=6.3,
    min_solder_mask_dam_mm=0.10,
)

_MULTI_1OZ_NOTES = dict(
    min_trace_width_mm='verbatim: "Multilayer: 0.09 / 0.09 mm" (1 oz)',
    min_trace_spacing_mm='verbatim: "Multilayer: 0.09 / 0.09 mm" (1 oz)',
    min_via_diameter_mm='verbatim: "Multilayer: 0.15mm hole / 0.25 mm diameter"',
    min_via_drill_mm='verbatim: "Multilayer: 0.15mm hole / 0.25 mm diameter"',
    min_annular_ring_mm='verbatim: "PTH annular ring (multilayer, 1oz): Recommended 0.20 mm; absolute minimum 0.15 mm"',
    recommended_annular_ring_mm='verbatim: "PTH annular ring (multilayer, 1oz): Recommended 0.20 mm; minimum 0.15 mm"',
    min_via_annular_ring_mm=_derived_via_annular_ring_note(0.25, 0.15),
    min_pth_drill_mm='verbatim: "PTH drill diameter range: 0.15 - 6.3 mm (2+ layers)"',
    max_pth_drill_mm='verbatim: "PTH drill diameter range: 0.15 - 6.3 mm (2+ layers)"',
    min_solder_mask_dam_mm='verbatim: "Min solder mask bridge (1oz): 0.10 mm"',
    **_not_stated("min_pad_size_mm"),
)

_4L_1OZ = FabDesignRules(
    config_id="4layer_1oz",
    description="4-layer FR-4, 1 oz outer copper",
    max_board_width_mm=663.0,
    max_board_height_mm=593.0,
    **_MULTI_1OZ_COMMON,
    **_SHARED,
    field_notes=_notes(
        **_MULTI_1OZ_NOTES,
        max_board_width_mm='verbatim: "Max board size (FR4, 4-layer): 663 x 593 mm"',
        max_board_height_mm='verbatim: "Max board size (FR4, 4-layer): 663 x 593 mm"',
    ),
)

_6L_1OZ = FabDesignRules(
    config_id="6layer_plus_1oz",
    description="6-or-more-layer FR-4, 1 oz outer copper",
    max_board_width_mm=656.0,
    max_board_height_mm=586.0,
    **_MULTI_1OZ_COMMON,
    **_SHARED,
    field_notes=_notes(
        **_MULTI_1OZ_NOTES,
        max_board_width_mm='verbatim: "Max board size (FR4, 6+ layers): 656 x 586 mm"',
        max_board_height_mm='verbatim: "Max board size (FR4, 6+ layers): 656 x 586 mm"',
    ),
)

_MULTI_2OZ_COMMON = dict(
    copper_weight_oz=2.0,
    min_trace_width_mm=0.15,
    min_trace_spacing_mm=0.15,
    min_via_diameter_mm=0.25,
    min_via_drill_mm=0.15,
    min_annular_ring_mm=0.254,
    min_via_annular_ring_mm=0.05,
    min_pth_drill_mm=0.15,
    max_pth_drill_mm=6.3,
)

_MULTI_2OZ_NOTES = dict(
    min_trace_width_mm='verbatim: "Multilayer: 0.15 / 0.15 mm" (2 oz)',
    min_trace_spacing_mm='verbatim: "Multilayer: 0.15 / 0.15 mm" (2 oz)',
    min_via_diameter_mm=(
        'verbatim: "Min. Via hole size/diameter: 0.15mm / 0.25mm" - the via row is not '
        "broken out by copper weight"
    ),
    min_via_drill_mm=(
        'verbatim: "Min. Via hole size/diameter: 0.15mm / 0.25mm" - the via row is not '
        "broken out by copper weight"
    ),
    min_annular_ring_mm='verbatim: "PTH annular ring (multilayer, 2 oz): 0.254 mm or above"',
    min_via_annular_ring_mm=_derived_via_annular_ring_note(0.25, 0.15),
    min_pth_drill_mm='verbatim: "PTH drill diameter range: 0.15 - 6.3 mm (2+ layers)"',
    max_pth_drill_mm='verbatim: "PTH drill diameter range: 0.15 - 6.3 mm (2+ layers)"',
    **_not_stated("min_solder_mask_dam_mm", "recommended_annular_ring_mm", "min_pad_size_mm"),
)

_4L_2OZ = FabDesignRules(
    config_id="4layer_2oz",
    description="4-layer FR-4, 2 oz outer copper",
    max_board_width_mm=663.0,
    max_board_height_mm=593.0,
    **_MULTI_2OZ_COMMON,
    **_SHARED,
    field_notes=_notes(
        **_MULTI_2OZ_NOTES,
        max_board_width_mm='verbatim: "Max board size (FR4, 4-layer): 663 x 593 mm"',
        max_board_height_mm='verbatim: "Max board size (FR4, 4-layer): 663 x 593 mm"',
    ),
)

_6L_2OZ = FabDesignRules(
    config_id="6layer_plus_2oz",
    description="6-or-more-layer FR-4, 2 oz outer copper",
    max_board_width_mm=656.0,
    max_board_height_mm=586.0,
    **_MULTI_2OZ_COMMON,
    **_SHARED,
    field_notes=_notes(
        **_MULTI_2OZ_NOTES,
        max_board_width_mm='verbatim: "Max board size (FR4, 6+ layers): 656 x 586 mm"',
        max_board_height_mm='verbatim: "Max board size (FR4, 6+ layers): 656 x 586 mm"',
    ),
)


JLCPCB_PROFILE = ManufacturerProfile(
    id="jlcpcb",
    display_name="JLCPCB",
    website="https://jlcpcb.com",
    source_url=_URL,
    source_fetched_date="2026-09-04",
    aliases=("jlc", "jlc-pcb", "jlcpcb.com"),
    supported_layer_count_min=1,
    supported_layer_count_max=32,
    supported_layer_counts_note=(
        'The page states "1-32 Layers" as a RANGE and does not enumerate which '
        "individual counts are orderable, so no per-count support list is claimed "
        "here. Published design rules exist for three tiers only: 1-layer, 2-layer, "
        "and 'multilayer' (4 and above share one rule column; only max board size "
        "differs between 4-layer and 6+)."
    ),
    configs={
        r.config_id: r
        for r in (_1L_1OZ, _2L_1OZ, _2L_2OZ, _4L_1OZ, _4L_2OZ, _6L_1OZ, _6L_2OZ)
    },
    # (min_layers, max_layers, copper_oz, config_id) - first match wins.
    config_selectors=(
        (1, 1, 1.0, "1layer_1oz"),
        (2, 2, 1.0, "2layer_1oz"),
        (2, 2, 2.0, "2layer_2oz"),
        (4, 5, 1.0, "4layer_1oz"),
        (4, 5, 2.0, "4layer_2oz"),
        (6, 32, 1.0, "6layer_plus_1oz"),
        (6, 32, 2.0, "6layer_plus_2oz"),
    ),
    notes=(
        "Rigid FR-4 only. Flex/rigid-flex, aluminium and Rogers stackups have "
        "different limits and are not covered by this profile.",
        "min_annular_ring_mm is JLCPCB's stated ABSOLUTE minimum, not their "
        "recommended value; recommended_annular_ring_mm carries the recommendation.",
        "JLCPCB publishes two distinct hole-to-hole numbers: 0.2 mm via-to-via and "
        "0.45 mm pad-to-pad. This project's canonical hole_to_hole_mm maps to the "
        "pad figure, which is what footprint placement checks measure.",
        "Copper weights other than 1 oz and 2 oz (the page lists 2.5 oz and above "
        "for 2-layer) are not modelled -- ask for them and they will be added from "
        "the same page rather than interpolated.",
        "3-layer boards are not offered as a rule tier; layer_count=3 resolves to "
        "no configuration rather than silently borrowing 2- or 4-layer rules.",
    ),
)
