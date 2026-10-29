"""Tests for the local manufacturer design-rule profiles and board-rule reader.

archived backlog P1-21 (fab-capability profiles) + P1-18 (stop checking designs
against hardcoded literals). The network-dependent test at the bottom
re-verifies the JLCPCB numbers against JLCPCB's live capabilities page and is
marked `network` so CI stays green offline.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp.manufacturing_profiles import (
    BUILTIN_DEFAULTS,
    CANONICAL_FROM_FAB_FIELD,
    JLCPCB_PROFILE,
    UnknownManufacturerError,
    UnsupportedConfigurationError,
    compare_to_profile,
    find_rule_files,
    get_design_rules,
    get_profile,
    list_manufacturer_ids,
    list_profiles,
    read_board_design_rules,
    read_custom_rules,
    resolve_check_rules,
    resolve_effective_rules,
    to_kicad_tools_design_rules,
)

FIXTURES = Path(__file__).parent / "fixtures"
PCB_FIXTURE = FIXTURES / "simple_led.kicad_pcb"
GOLDEN_PCB = FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"

JLCPCB_CAPABILITIES_URL = "https://jlcpcb.com/capabilities/pcb-capabilities"


# ---------------------------------------------------------------------------
# Registry: evidence gating
# ---------------------------------------------------------------------------


def test_registry_ships_jlcpcb() -> None:
    assert list_manufacturer_ids() == ["jlcpcb"]
    assert get_profile("jlcpcb") is JLCPCB_PROFILE


@pytest.mark.parametrize("alias", ["jlc", "JLCPCB", " jlcpcb ", "JLC-PCB", "jlcpcb.com"])
def test_aliases_and_normalisation_resolve(alias: str) -> None:
    assert get_profile(alias).id == "jlcpcb"


def test_unknown_manufacturer_is_an_error_naming_what_is_available() -> None:
    # The whole point of P1-21's evidence gate: never a silent fallback to
    # generic defaults for a fab we have no data for.
    with pytest.raises(UnknownManufacturerError) as excinfo:
        get_profile("pcbway")
    assert "jlcpcb" in str(excinfo.value)
    assert excinfo.value.available == ["jlcpcb"]


def test_no_generic_default_profile_exists() -> None:
    for name in ("", "generic", "default", "any", "unknown"):
        with pytest.raises(UnknownManufacturerError):
            get_profile(name)


def test_unsupported_configuration_is_an_error_not_a_nearby_ruleset() -> None:
    # 3-layer boards aren't a JLCPCB rule tier; silently handing back the
    # 2- or 4-layer set would be exactly the invented-number failure mode.
    with pytest.raises(UnsupportedConfigurationError):
        get_design_rules("jlcpcb", layer_count=3)
    # 2.5oz copper is listed on the page but not modelled here.
    with pytest.raises(UnsupportedConfigurationError):
        get_design_rules("jlcpcb", layer_count=2, copper_weight_oz=2.5)
    with pytest.raises(UnsupportedConfigurationError):
        get_design_rules("jlcpcb", layer_count=64)


# ---------------------------------------------------------------------------
# JLCPCB data: verified against https://jlcpcb.com/capabilities/pcb-capabilities
# (fetched 2026-09-04). These assertions are the regression guard on the
# numbers themselves -- if someone "tidies" a value, this fails.
# ---------------------------------------------------------------------------


def test_profile_records_its_source_and_fetch_date() -> None:
    assert JLCPCB_PROFILE.source_url == JLCPCB_CAPABILITIES_URL
    assert JLCPCB_PROFILE.source_fetched_date == "2026-09-04"


def test_jlcpcb_2layer_1oz_matches_published_capabilities() -> None:
    r = get_design_rules("jlcpcb", layer_count=2, copper_weight_oz=1.0)
    assert r.config_id == "2layer_1oz"
    assert r.min_trace_width_mm == 0.10
    assert r.min_trace_spacing_mm == 0.10
    assert r.min_via_diameter_mm == 0.25
    assert r.min_via_drill_mm == 0.15
    assert r.min_annular_ring_mm == 0.18  # absolute minimum, not the 0.25 recommendation
    assert r.recommended_annular_ring_mm == 0.25
    # DERIVED (not verbatim - JLCPCB publishes no via-specific annular-ring
    # minimum): (min_via_diameter_mm 0.25 - min_via_drill_mm 0.15) / 2.
    assert r.min_via_annular_ring_mm == 0.05
    assert "DERIVED" in r.field_notes["min_via_annular_ring_mm"]
    assert r.min_pth_drill_mm == 0.15
    assert r.max_pth_drill_mm == 6.3
    assert r.min_npth_drill_mm == 0.5
    assert r.min_via_hole_to_hole_mm == 0.2
    assert r.min_pad_hole_to_hole_mm == 0.45
    assert r.min_copper_to_edge_mm == 0.2
    assert r.min_copper_to_vcut_edge_mm == 0.4
    assert r.min_silkscreen_width_mm == 0.15
    assert r.min_silkscreen_text_height_mm == 1.0
    assert r.min_silkscreen_to_pad_mm == 0.15
    assert r.min_solder_mask_dam_mm == 0.10
    assert r.max_board_width_mm == 670.0
    assert r.max_board_height_mm == 600.0
    assert r.min_board_width_mm == 3.0


def test_jlcpcb_differentiates_by_layer_count_where_the_page_does() -> None:
    two = get_design_rules("jlcpcb", layer_count=2)
    four = get_design_rules("jlcpcb", layer_count=4)
    six = get_design_rules("jlcpcb", layer_count=6)
    # multilayer trace/spacing and annular ring are genuinely tighter
    assert two.min_trace_width_mm == 0.10 and four.min_trace_width_mm == 0.09
    assert two.min_trace_spacing_mm == 0.10 and four.min_trace_spacing_mm == 0.09
    assert two.min_annular_ring_mm == 0.18 and four.min_annular_ring_mm == 0.15
    # 4-layer and 6+ share the rule column but not the max board size
    assert four.min_trace_width_mm == six.min_trace_width_mm
    assert (four.max_board_width_mm, four.max_board_height_mm) == (663.0, 593.0)
    assert (six.max_board_width_mm, six.max_board_height_mm) == (656.0, 586.0)


def test_jlcpcb_differentiates_by_copper_weight() -> None:
    assert get_design_rules("jlcpcb", 2, 1.0).min_trace_width_mm == 0.10
    assert get_design_rules("jlcpcb", 2, 2.0).min_trace_width_mm == 0.16
    assert get_design_rules("jlcpcb", 4, 2.0).min_trace_width_mm == 0.15


def test_1layer_has_its_own_looser_drill_limits() -> None:
    one = get_design_rules("jlcpcb", layer_count=1)
    assert one.min_pth_drill_mm == 0.3  # vs 0.15 for 2+ layers
    assert one.min_via_diameter_mm == 0.5
    assert (one.max_board_width_mm, one.max_board_height_mm) == (606.0, 510.0)


def test_unconfirmed_values_are_none_and_declared_not_guessed() -> None:
    # The honest-gap requirement: JLCPCB publishes no minimum pad size, so
    # it must be None AND must say so, rather than inheriting a plausible
    # 0.25mm from somewhere.
    r = get_design_rules("jlcpcb", 2)
    assert r.min_pad_size_mm is None
    assert "min_pad_size_mm" in r.unconfirmed_fields
    assert "NOT STATED" in r.field_notes["min_pad_size_mm"]


def test_every_confirmed_value_carries_a_source_note() -> None:
    for layers, oz in ((1, 1.0), (2, 1.0), (2, 2.0), (4, 1.0), (4, 2.0), (6, 1.0), (6, 2.0)):
        r = get_design_rules("jlcpcb", layers, oz)
        for name, value in r.values_mm.items():
            note = r.field_notes.get(name)
            assert note, f"{r.config_id}.{name} has no provenance note"
            if value is None:
                assert "NOT STATED" in note, f"{r.config_id}.{name} is None without saying so"
            else:
                assert "verbatim" in note, f"{r.config_id}.{name}={value} has no quoted source"


def test_unconfirmed_fields_is_exactly_the_none_valued_fields() -> None:
    r = get_design_rules("jlcpcb", 2, 2.0)
    assert set(r.unconfirmed_fields) == {k for k, v in r.values_mm.items() if v is None}
    assert r.min_solder_mask_dam_mm is None  # page publishes the 0.10mm figure for 1oz only


def test_profile_is_json_serialisable_end_to_end() -> None:
    payload = {
        "profile": JLCPCB_PROFILE.to_dict(),
        "rules": get_design_rules("jlcpcb", 4).to_dict(),
    }
    assert json.loads(json.dumps(payload))["profile"]["source_url"] == JLCPCB_CAPABILITIES_URL


def test_canonical_floor_maps_pad_hole_to_hole_not_via() -> None:
    # JLCPCB publishes two different hole-to-hole numbers; the placement
    # checker measures footprint pad holes, so the pad figure is the one
    # the canonical name must resolve to.
    floor = JLCPCB_PROFILE.canonical_floor(2)
    assert floor["hole_to_hole_mm"] == 0.45
    assert get_design_rules("jlcpcb", 2).min_via_hole_to_hole_mm == 0.2
    assert set(floor) == set(CANONICAL_FROM_FAB_FIELD)


def test_canonical_floor_separates_via_annular_ring_from_pth() -> None:
    """archived backlog P1-23: a board's VIA annular-ring rule must never be graded
    against JLCPCB's PTH annular-ring floor -- they are different, unrelated
    numbers, and JLCPCB doesn't even publish a via-specific one (confirmed by
    re-fetching the page; see min_via_annular_ring_mm's field_notes)."""
    floor = JLCPCB_PROFILE.canonical_floor(2)
    assert floor["annular_ring_mm"] == 0.18  # PTH (component through-hole pad)
    assert floor["via_annular_ring_mm"] == 0.05  # via, DERIVED from diameter/drill
    assert floor["annular_ring_mm"] != floor["via_annular_ring_mm"]

    # A board's `.kicad_pro` `min_via_annular_width` is unambiguously a via
    # rule by name -- it must resolve to via_annular_ring_mm, not
    # annular_ring_mm (the real bug: comparing a 0.1mm via annular width
    # against the 0.18mm PTH floor would wrongly flag it as below-floor when
    # it's actually well above the real via floor of 0.05mm).
    resolved = resolve_effective_rules(
        project_rules={"min_via_annular_width": 0.1},
        netclasses=[],
        custom_rules=[],
    )
    assert "via_annular_ring_mm" in resolved
    assert resolved["via_annular_ring_mm"].value_mm == 0.1
    assert "annular_ring_mm" not in resolved

    compared = compare_to_profile(
        PCB_FIXTURE,
        "jlcpcb",
        layer_count=2,
        board_result={
            "status": "complete",
            "warnings": [],
            "effective_rules": {k: v.to_dict() for k, v in resolved.items()},
        },
    )
    via_finding = next(f for f in compared["findings"] if f["rule"] == "via_annular_ring_mm")
    assert via_finding["verdict"] == "ok"  # 0.1mm >= 0.05mm real via floor
    assert via_finding["board_value_mm"] == 0.1
    assert via_finding["fab_floor_mm"] == 0.05
    pth_finding = next(f for f in compared["findings"] if f["rule"] == "annular_ring_mm")
    assert pth_finding["verdict"] == "board_rule_unspecified"  # board never declared a PTH rule

    # A `.kicad_dru` `annular_width` constraint is also via-specific in
    # KiCad's own DRU vocabulary -- same fix applies there.
    dru_resolved = resolve_effective_rules(
        project_rules={},
        netclasses=[],
        custom_rules=[
            {
                "name": "via annular",
                "constraint_type": "annular_width",
                "min_mm": 0.12,
                "canonical_rule": "via_annular_ring_mm",
            }
        ],
    )
    assert dru_resolved["via_annular_ring_mm"].value_mm == 0.12
    assert "annular_ring_mm" not in dru_resolved

    # And the raw .kicad_dru parser itself must produce that canonical_rule
    # for an `annular_width` constraint type, not annular_ring_mm.
    from kicad_mcp.manufacturing_profiles.board_rules import _DRU_CONSTRAINT_KEYS

    assert _DRU_CONSTRAINT_KEYS["annular_width"] == "via_annular_ring_mm"


def test_bridge_to_kicad_tools_design_rules_uses_our_verified_numbers() -> None:
    # kicad_tools' own bundled JLCPCB YAML says 0.127mm trace / 0.3mm via
    # drill. The bridge must carry our verified numbers instead.
    bridged = to_kicad_tools_design_rules("jlcpcb", 2, 1.0)
    assert bridged.min_trace_width_mm == 0.10
    assert bridged.min_clearance_mm == 0.10
    assert bridged.min_via_drill_mm == 0.15

    # archived backlog P1-23: kicad_tools' own DesignRules.min_annular_ring_mm is
    # documented under its "# Via constraints" section (it feeds KiCad's via
    # annular-width project setting when generating a .kicad_pro/.dru), so
    # the bridge must source it from our VIA-derived field
    # (min_via_annular_ring_mm, 0.05mm - JLCPCB publishes no via-specific
    # annular-ring minimum, this is geometrically implied by the via
    # diameter/drill numbers), never our PTH field (min_annular_ring_mm,
    # 0.18mm - a different, unrelated number for component through-hole
    # pads). Before this fix the bridge silently fed the PTH number into a
    # via-context field.
    assert bridged.min_annular_ring_mm == 0.05

    from kicad_tools.manufacturers import get_profile as kt_get_profile

    upstream = kt_get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1.0)
    # Once compared in the correct (via) context, upstream's stale 0.15mm
    # figure is actually LOOSER than the real geometric via floor (0.05mm) -
    # the opposite relationship from when this field was (wrongly) compared
    # against the stricter PTH number. Both directions are worth knowing:
    # upstream's bundled data was never verified against JLCPCB's real via
    # geometry either.
    assert upstream.min_annular_ring_mm > bridged.min_annular_ring_mm, (
        "upstream kicad_tools' bundled via annular-ring figure is expected to be LOOSER "
        "than the real geometric via floor derived from JLCPCB's own via diameter/drill "
        "numbers -- if this ever stops being true, upstream has been corrected and this "
        "package's justification should be revisited"
    )


# ---------------------------------------------------------------------------
# Board's own rules (P1-18)
# ---------------------------------------------------------------------------


def test_reads_real_netclasses_from_the_fixture_project() -> None:
    out = read_board_design_rules(PCB_FIXTURE)
    assert out["status"] == "complete"
    assert out["project_file"].endswith("simple_led.kicad_pro")
    assert out["netclass_count"] == 1
    assert out["netclasses"][0]["name"] == "Default"
    # unit-suffixed canonical keys, per docs/ARCHITECTURE.md tool conventions
    assert out["netclasses"][0]["clearance_mm"] == 0.2
    effective = out["effective_rules"]
    assert effective["clearance_mm"]["value_mm"] == 0.2
    assert effective["clearance_mm"]["source"] == "board_netclass:Default"
    assert effective["via_drill_mm"]["value_mm"] == 0.3


def test_zero_valued_project_rules_are_unspecified_not_no_limit() -> None:
    # simple_led.kicad_pro carries `min_copper_edge_clearance: 0.0`, which is
    # KiCad's "never set" marker. Taking it literally would silently disable
    # the edge-clearance check.
    out = read_board_design_rules(PCB_FIXTURE)
    assert out["project_rules_raw"]["min_copper_edge_clearance"] == 0.0
    assert "edge_clearance_mm" not in out["effective_rules"]


def test_golden_board_rules_are_readable() -> None:
    out = read_board_design_rules(GOLDEN_PCB)
    assert out["status"] in ("complete", "partial")
    assert out["pcb_path"].endswith("golden_attiny1616_board.kicad_pcb")
    for rule in out["effective_rules"].values():
        assert rule["source"].startswith(("board_netclass:", "board_project_rules", "board_custom_rule:"))


def test_missing_board_is_incomplete_not_an_exception() -> None:
    out = read_board_design_rules(FIXTURES / "nope.kicad_pcb")
    assert out["status"] == "incomplete"
    assert "reason" in out


def test_board_with_no_rule_sources_reports_partial(tmp_path: Path) -> None:
    from kicad_tools.schema import PCB

    board = PCB.create(width=20, height=20)
    lonely = tmp_path / "lonely.kicad_pcb"
    write_layer.save_pcb(board, lonely)
    out = read_board_design_rules(lonely)
    assert out["status"] == "partial"
    assert "no design-rule sources" in out["reason"]
    assert out["effective_rules"] == {}


def test_find_rule_files_locates_siblings() -> None:
    pro, dru = find_rule_files(PCB_FIXTURE)
    assert pro is not None and pro.suffix == ".kicad_pro"
    assert dru is None  # fixture has no custom rules file


def test_reads_kicad_dru_custom_rules(tmp_path: Path) -> None:
    # Never hand-write a KiCad file: generate a real .kicad_dru through
    # kicad_tools' own generator, driven by our verified JLCPCB numbers.
    from kicad_tools.manufacturers.dru_generator import generate_dru
    from kicad_tools.schema import PCB

    board = PCB.create(width=30, height=30)
    pcb_path = tmp_path / "dru_board.kicad_pcb"
    write_layer.save_pcb(board, pcb_path)
    dru_path = pcb_path.with_suffix(".kicad_dru")
    dru_path.write_text(
        generate_dru(to_kicad_tools_design_rules("jlcpcb", 2, 1.0), manufacturer_name="JLCPCB"),
        encoding="utf-8",
    )

    parsed = read_custom_rules(dru_path)
    # Keyed on the minimum rules: from kicad_tools 0.24.0, generate_dru also writes a
    # "Max Hole Size" rule, a second hole_size constraint with only a max (build ticket 71).
    by_type = {r["constraint_type"]: r for r in parsed if r["min_mm"] is not None}
    assert by_type["track_width"]["min_mm"] == pytest.approx(0.10)
    assert by_type["clearance"]["min_mm"] == pytest.approx(0.10)
    assert by_type["hole_size"]["min_mm"] == pytest.approx(0.15)
    assert by_type["track_width"]["canonical_rule"] == "track_width_mm"
    assert by_type["track_width"]["condition"]  # conditions are preserved, not flattened away
    # The max-only rule is read too, not dropped: 0.24.0's "Max Hole Size" (max 6.3 mm).
    import kicad_tools

    max_only = [r for r in parsed if r["min_mm"] is None and r["constraint_type"] == "hole_size"]
    if tuple(int(p) for p in kicad_tools.__version__.split(".")[:2]) >= (0, 24):
        assert [r["max_mm"] for r in max_only] == [pytest.approx(6.3)]
    else:
        assert max_only == []

    out = read_board_design_rules(pcb_path)
    assert out["status"] == "partial"  # no .kicad_pro, but the .kicad_dru read fine
    assert out["custom_rules_file"] == str(dru_path)
    assert out["custom_rule_count"] == len(parsed)
    clearance = out["effective_rules"]["clearance_mm"]
    assert clearance["value_mm"] == pytest.approx(0.10)
    assert clearance["source"].startswith("board_custom_rule:")
    # a max-only hole_size rule (0.24.0's "Max Hole Size") never lowers the minimum drill
    assert out["effective_rules"]["via_drill_mm"]["value_mm"] == pytest.approx(0.15)


def test_most_restrictive_source_wins_and_all_candidates_are_reported() -> None:
    resolved = resolve_effective_rules(
        project_rules={"min_clearance": 0.15},
        netclasses=[{"name": "Default", "clearance": 0.2}, {"name": "HV", "clearance": 0.5}],
        custom_rules=[
            {
                "name": "tight",
                "constraint_type": "clearance",
                "min_mm": 0.3,
                "canonical_rule": "clearance_mm",
            }
        ],
    )
    rule = resolved["clearance_mm"]
    assert rule.value_mm == 0.3  # the custom rule is the most restrictive
    assert rule.source == "board_custom_rule:tight"
    # tightest netclass (0.2), not the loosest (0.5), is the netclass candidate
    assert rule.candidates == {
        "board_project_rules": 0.15,
        "board_netclass:Default": 0.2,
        "board_custom_rule:tight": 0.3,
    }


def test_dru_unit_suffixes_are_normalised_to_mm() -> None:
    resolved = resolve_effective_rules(
        project_rules={},
        netclasses=[],
        custom_rules=[
            {
                "name": "mils",
                "constraint_type": "clearance",
                "min_mm": 0.2032,  # 8 mil, as read back by read_custom_rules
                "canonical_rule": "clearance_mm",
            }
        ],
    )
    assert resolved["clearance_mm"].value_mm == pytest.approx(0.2032)


def test_hole_to_hole_and_hole_clearance_are_distinct_constraint_types() -> None:
    """archived backlog P1-24, found on the user's real openMPPT v1.3 board:
    `hole_clearance` (copper-to-hole clearance, e.g. "track to PTH hole
    perimeter") and `hole_to_hole` (hole-to-hole perimeter spacing) are two
    distinct, real KiCad DRU constraint types. Conflating them previously
    let a `hole_clearance` rule silently steal the `hole_to_hole_mm`
    canonical bucket while the board's genuine `hole_to_hole` rules were
    dropped entirely. The fixture is the real board's own `.kicad_dru`,
    which happens to carry both types, including two differently-conditioned
    `hole_to_hole` rules (different nets vs. same net)."""
    dru_path = FIXTURES / "openmppt_v1_3.kicad_dru"
    parsed = read_custom_rules(dru_path)
    by_type: dict[str, list[dict]] = {}
    for rule in parsed:
        by_type.setdefault(rule["constraint_type"], []).append(rule)

    # hole_clearance: 2 real rules (NPTH 0.254mm, PTH 0.33mm), both correctly
    # left unmapped -- this project's rule set has no single-scalar canonical
    # bucket for "copper to hole clearance" yet, and they must never resolve
    # to hole_to_hole_mm.
    assert len(by_type["hole_clearance"]) == 2
    assert all(r["canonical_rule"] is None for r in by_type["hole_clearance"])

    # hole_to_hole: 2 real rules (different nets 0.5mm, same net 0.15mm),
    # both correctly mapped to hole_to_hole_mm.
    assert len(by_type["hole_to_hole"]) == 2
    assert all(r["canonical_rule"] == "hole_to_hole_mm" for r in by_type["hole_to_hole"])
    hole_to_hole_values = sorted(r["min_mm"] for r in by_type["hole_to_hole"])
    assert hole_to_hole_values == pytest.approx([0.15, 0.5])

    resolved = resolve_effective_rules(project_rules={}, netclasses=[], custom_rules=parsed)
    rule = resolved["hole_to_hole_mm"]
    # The strictest genuine hole_to_hole rule wins ("different nets", 0.5mm)
    # -- never a hole_clearance value (0.254/0.33mm), which must not have
    # been a candidate for this bucket at all.
    assert rule.value_mm == pytest.approx(0.5)
    assert "hole to hole" in rule.source
    assert len(rule.candidates) == 2  # both real hole_to_hole rules, nothing else
    assert sorted(rule.candidates.values()) == pytest.approx([0.15, 0.5])


# ---------------------------------------------------------------------------
# Precedence + capability-floor comparison
# ---------------------------------------------------------------------------


def test_resolve_check_rules_precedence_order() -> None:
    board = {"effective_rules": {"clearance_mm": {"value_mm": 0.25, "source": "board_netclass:Default"}}}
    floor = {"clearance_mm": 0.1, "edge_clearance_mm": 0.2}

    explicit_wins = resolve_check_rules({"clearance_mm": 0.4}, board, floor, manufacturer_label="jlcpcb")
    assert explicit_wins["clearance_mm"] == {"value_mm": 0.4, "source": "explicit_argument"}

    board_wins = resolve_check_rules({"clearance_mm": None}, board, floor, manufacturer_label="jlcpcb")
    assert board_wins["clearance_mm"]["value_mm"] == 0.25
    assert board_wins["clearance_mm"]["source"] == "board_netclass:Default"

    fab_wins = resolve_check_rules({"edge_clearance_mm": None}, board, floor, manufacturer_label="jlcpcb")
    assert fab_wins["edge_clearance_mm"] == {
        "value_mm": 0.2,
        "source": "manufacturer_profile:jlcpcb",
    }

    fallback = resolve_check_rules({"hole_to_hole_mm": None}, board, None)
    assert fallback["hole_to_hole_mm"] == {
        "value_mm": BUILTIN_DEFAULTS["hole_to_hole_mm"],
        "source": "builtin_default",
    }


def test_compare_flags_a_design_below_the_fab_floor() -> None:
    # A board whose own rules are internally consistent but below JLCPCB's
    # published minimums: DRC-clean, still unmanufacturable. This is the
    # exact claim P1-21 exists to make checkable.
    below = {
        "status": "complete",
        "effective_rules": {
            canonical: {"value_mm": 0.01, "source": "board_netclass:Default"}
            for canonical in CANONICAL_FROM_FAB_FIELD
        },
    }
    out = compare_to_profile(PCB_FIXTURE, "jlcpcb", 2, 1.0, board_result=below)
    assert out["status"] == "complete"
    assert out["meets_fab_floor"] is False
    # every rule JLCPCB actually publishes should be flagged
    published = [c for c, f in JLCPCB_PROFILE.canonical_floor(2).items() if f is not None]
    assert out["below_fab_floor_count"] == len(published)
    for finding in out["below_fab_floor"]:
        assert finding["board_value_mm"] < finding["fab_floor_mm"]
        assert JLCPCB_CAPABILITIES_URL in finding["fab_floor_source"]


def test_compare_never_claims_a_pass_for_rules_the_board_never_declared() -> None:
    out = compare_to_profile(PCB_FIXTURE, "jlcpcb")
    assert out["status"] == "partial"
    assert out["meets_fab_floor"] is None  # not True -- unverified is not a pass
    assert out["unspecified_count"] > 0
    verdicts = {f["rule"]: f["verdict"] for f in out["findings"]}
    assert verdicts["clearance_mm"] == "ok"
    assert verdicts["edge_clearance_mm"] == "board_rule_unspecified"


def test_compare_reports_which_side_every_number_came_from() -> None:
    out = compare_to_profile(PCB_FIXTURE, "jlcpcb")
    for finding in out["findings"]:
        assert "fab_floor_source" in finding
        if finding["board_value_mm"] is not None:
            assert finding["board_value_source"], finding
    assert "capability floor" in out["precedence"]


def test_compare_propagates_unknown_manufacturer() -> None:
    with pytest.raises(UnknownManufacturerError):
        compare_to_profile(PCB_FIXTURE, "definitely-not-a-fab")


def test_compare_on_missing_board_is_incomplete() -> None:
    out = compare_to_profile(FIXTURES / "nope.kicad_pcb", "jlcpcb")
    assert out["status"] == "incomplete"
    assert "reason" in out


def test_list_profiles_exposes_provenance() -> None:
    for profile in list_profiles():
        d = profile.to_dict()
        assert d["source_url"].startswith("https://")
        assert d["source_fetched_date"]
        assert d["available_configs"]


# ---------------------------------------------------------------------------
# Live re-verification (network)
# ---------------------------------------------------------------------------


@pytest.mark.network
def test_jlcpcb_capabilities_page_still_publishes_our_numbers() -> None:
    """Re-verify the profile against JLCPCB's live page.

    Deliberately narrow: it asserts the key figures still appear verbatim on
    the page, so a silent capability change (or a dead URL) surfaces as a
    failing test rather than as a stale local file nobody re-reads. Marked
    `network` so `pytest -m "not network"` stays green offline.
    """
    import httpx

    response = httpx.get(
        JLCPCB_CAPABILITIES_URL,
        timeout=30.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; kicad-mcp profile verification)"},
    )
    response.raise_for_status()
    text = response.text.replace(" ", " ")
    # Figures that must still be findable somewhere on the page.
    for needle in ("0.15mm", "0.25mm", "0.45mm", "6.3"):
        assert needle in text, f"{needle!r} no longer appears on {JLCPCB_CAPABILITIES_URL}"
