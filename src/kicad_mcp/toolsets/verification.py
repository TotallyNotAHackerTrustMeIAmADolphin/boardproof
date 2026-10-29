"""Verification toolset: DRC/ERC and geometric collision checks.

This is the core "geometry over vision" feedback surface described in
docs/ARCHITECTURE.md — structured, JSON-serializable results an agent can
reason over directly, never a rendered image the agent has to interpret.

Two distinct engines, used for different purposes -- kept explicit after
a mislabeling bug (archived backlog P1-14, independent review, 2026-09-02) where
a placement-only checker was exposed as `pcb_run_drc`, letting a clean
result be misread as a full DRC pass:
- ``pcb_run_drc``/``sch_run_erc`` — single runs of KiCad's own checks, via
  real ``kicad-cli`` (``kicad_mcp.kicad_cli``). Use these to list what is
  wrong; they are not the acceptance gate. Only ``verify_board`` decides
  whether a board passes.
- ``pcb_check_placement``/``pcb_check_collisions``/
  ``pcb_validate_proposed_placements`` — ``kicad_tools``' fast, pure-Python
  placement-geometry checker (pad clearance, courtyard, edge, hole-to-hole,
  silkscreen-over-pad, off-board). Useful as a quick check right after
  moving/placing footprints, NOT a substitute for `pcb_run_drc`.

Every tool here follows the "evidence-gated verdicts" principle: a check
that could not run to completion reports ``{"status": "incomplete", ...}``
rather than raising past the tool boundary or looking like a clean pass.

Design-rule sourcing (archived backlog P1-18 + P1-21, 2026-09-04)
-----------------------------------------------------------
The placement-geometry tools used to check against hardcoded literals
(``clearance_mm=0.2`` ...), so a clean result was clean against numbers
nobody involved in the design had chosen. They now resolve every rule with
an explicit, reported precedence:

    explicit argument > the board's own rules > manufacturer capability
    floor > this project's built-in fallback

and every returned number carries a ``source`` label. The precedence is not
arbitrary: **the board's own rules are authoritative for what KiCad checks**
(DRC measures the design against its netclasses / project constraints /
``.kicad_dru``), while **a manufacturer profile is a capability floor** —
what the fab can physically build. A design can be perfectly DRC-clean
against its own rules and still be unmanufacturable at a given fab, so the
two are reported side by side and never merged into one verdict. Passing
``manufacturer=`` also runs that floor comparison, surfaced as
``manufacturability`` alongside the check's own result.
"""

from __future__ import annotations

import dataclasses
import enum
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli
from kicad_mcp.kicad_cli import ensure_kicad_cli_on_path
from kicad_mcp.manufacturing_profiles import (
    UnknownManufacturerError,
    UnsupportedConfigurationError,
    compare_to_profile,
    get_profile,
    list_profiles,
    read_board_design_rules,
    resolve_check_rules,
)

# Collision-relevant DRC categories, per docs/ARCHITECTURE.md's
# `pcb_check_collisions()` spec (component-vs-component geometric
# overlap, not every DRC rule). Fixed 2026-09-02 per archived backlog P1-15
# (independent review): silkscreen_pad and off_board are real
# kicad_tools.placement.collision.ConflictType values that were silently
# missing here -- both are genuine component-vs-board/component-vs-silk
# collision categories, so they belong in this set. (A 7th real value,
# edge_connector_placement, is a single-footprint-vs-board-edge rule
# rather than a component-vs-component collision and is deliberately
# left out of this set -- it's closer to a placement constraint than a
# "did two things overlap" check.)
_COLLISION_TYPES = {
    "pad_clearance",
    "courtyard_overlap",
    "edge_clearance",
    "hole_to_hole",
    "silkscreen_pad",
    "off_board",
}


def _to_jsonable(value: Any) -> Any:
    """Recursively convert kicad_tools result objects into JSON-safe values.

    Handles dataclasses, enums, and simple x/y point-like objects
    generically rather than hardcoding every kicad_tools result shape,
    since that shape isn't part of any stable public contract.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "x") and hasattr(value, "y") and not isinstance(value, (str, bytes)):
        return {"x": value.x, "y": value.y}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _incomplete(reason: str, **extra: Any) -> dict:
    return {"status": "incomplete", "reason": reason, **extra}


_PRECEDENCE_NOTE = (
    "explicit argument > board's own rules (.kicad_pro netclasses/constraints, "
    ".kicad_dru) > manufacturer capability floor > built-in default. The board's "
    "own rules are authoritative for what KiCad's DRC checks; the manufacturer "
    "profile is only a capability floor -- a design can be DRC-clean against its "
    "own rules and still be unmanufacturable at that fab."
)


def _board_rules_summary(board: dict) -> dict:
    """Compact view of a board-rules read, for embedding in a check result."""
    return {
        "status": board.get("status"),
        "project_file": board.get("project_file"),
        "custom_rules_file": board.get("custom_rules_file"),
        "netclass_count": board.get("netclass_count"),
        "custom_rule_count": board.get("custom_rule_count"),
        "effective_rules": board.get("effective_rules"),
        "warnings": board.get("warnings", []),
    }


def _resolve_rules(
    pcb_path: Path,
    explicit: dict[str, float | None],
    manufacturer: str | None,
    layer_count: int,
    copper_weight_oz: float,
    use_board_rules: bool,
) -> dict:
    """Resolve the rule values a geometry check should run with, with sources.

    Returns either ``{"error": ...}`` (unknown manufacturer / unsupported
    configuration -- evidence-gated, never a silent fallback) or a dict with
    ``rules``, ``rules_applied``, ``board_design_rules`` and
    ``manufacturer_profile``.
    """
    board: dict = {}
    if use_board_rules:
        board = read_board_design_rules(pcb_path)

    fab_floor = None
    profile_info = None
    label = None
    if manufacturer:
        try:
            profile = get_profile(manufacturer)
            fab_floor = profile.canonical_floor(layer_count, copper_weight_oz)
        except (UnknownManufacturerError, UnsupportedConfigurationError) as exc:
            return {"error": str(exc)}
        label = profile.id
        profile_info = {
            "manufacturer": profile.id,
            "display_name": profile.display_name,
            "layer_count": layer_count,
            "copper_weight_oz": copper_weight_oz,
            "source_url": profile.source_url,
            "source_fetched_date": profile.source_fetched_date,
        }

    applied = resolve_check_rules(explicit, board, fab_floor, manufacturer_label=label)
    return {
        "rules": {name: entry["value_mm"] for name, entry in applied.items()},
        "rules_applied": applied,
        "rule_precedence": _PRECEDENCE_NOTE,
        "board_design_rules": _board_rules_summary(board) if board else None,
        "manufacturer_profile": profile_info,
    }


def _manufacturability(
    pcb_path: Path,
    manufacturer: str | None,
    layer_count: int,
    copper_weight_oz: float,
) -> dict | None:
    """Fab capability-floor audit for a board, or ``None`` when not requested."""
    if not manufacturer:
        return None
    try:
        return compare_to_profile(pcb_path, manufacturer, layer_count, copper_weight_oz)
    except (UnknownManufacturerError, UnsupportedConfigurationError) as exc:
        return {"status": "incomplete", "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
        return {"status": "incomplete", "reason": f"manufacturability check failed: {exc}"}


def build(mcp: MCPServer) -> list[str]:
    """Register the verification toolset. See toolsets/registry.py for the contract."""

    def pcb_run_drc(
        pcb_path: str,
        severity_all: bool = True,
        manufacturer: str | None = None,
        layer_count: int = 2,
        copper_weight_oz: float = 1.0,
    ) -> dict:
        """Run KiCad's real design rule check on a PCB (via `kicad-cli pcb drc`)
        once and return every violation it reports as structured data.

        A single DRC run: prefer this over asking a human/vision-model to
        look at a rendered image. It runs KiCad's own DRC engine (track
        width, annular ring, unconnected items, shorts, silk-over-pad,
        solder-mask bridge, differential-pair skew, impedance, ...), not
        just placement/courtyard geometry (for a narrower, faster
        placement-only check, see `pcb_check_placement`). It returns the
        unconnected items, but it is NOT the acceptance gate: one run, no
        zone refill, no stability check, no rule pinning, and a count of
        zero here proves nothing. Only `verify_board` may say a board
        passes; the `gate` field on this output shows its last verdict for
        this exact file.

        Fixed 2026-09-02 per archived backlog P1-14 (independent review): this
        tool used to call kicad_tools' PCB.run_drc(), which is actually a
        4-rule placement checker (pad clearance, courtyard, edge,
        hole-to-hole) despite the name — an agent reading a clean result
        from it would reasonably believe the board passed full DRC when
        only placement geometry had been checked. That checker is now
        `pcb_check_placement`; this tool wraps the real `kicad-cli pcb drc`.

        Design rules (archived backlog P1-18): KiCad runs this DRC against the
        BOARD'S OWN rules — its `.kicad_pro` netclasses and constraints plus
        any `.kicad_dru` — which this tool does not and must not change.
        Those rules are reported back under `design_rules` so a caller can
        see exactly what "clean" was clean against, instead of having to
        assume.

        `manufacturer` does NOT alter what KiCad checks. It adds a separate
        `manufacturability` audit comparing the board's effective rules
        against that fab's published capability floor, because the two are
        different claims: a board can be fully DRC-clean against its own
        rules and still be below a fab's process minimums. Relaxing or
        tightening the board's actual rules would mean rewriting its
        `.kicad_pro`, which is a mutation and deliberately out of scope for
        a read-only verification tool.
        """
        path = Path(pcb_path)
        if not path.is_file():
            return _incomplete(f"file not found: {pcb_path}")
        try:
            report = kicad_cli.run_drc(path, severity_all=severity_all)
        except Exception as exc:  # noqa: BLE001 - deliberately broad: evidence-gated verdict
            return _incomplete(f"DRC could not complete: {exc}")
        violations = report.get("violations", [])
        unconnected = report.get("unconnected_items", [])

        board = read_board_design_rules(path)
        manufacturability = _manufacturability(path, manufacturer, layer_count, copper_weight_oz)
        result = {
            "status": "complete",
            "kicad_version": report.get("kicad_version"),
            "violation_count": len(violations),
            "violations": violations,
            "unconnected_count": len(unconnected),
            "unconnected_items": unconnected,
            "schematic_parity": report.get("schematic_parity", []),
            "checked_against": (
                "the board's own rules, as loaded by kicad-cli from the sibling "
                ".kicad_pro / .kicad_dru -- see design_rules for what those are"
            ),
            "design_rules": _board_rules_summary(board),
            "rule_precedence": _PRECEDENCE_NOTE,
            "not_the_gate": (
                "This is a single DRC run and is not the acceptance gate. "
                "Call verify_board for the verdict; only its PASS certifies the board."
            ),
        }
        if manufacturability is not None:
            result["manufacturability"] = manufacturability
        return result

    def pcb_check_placement(
        pcb_path: str,
        clearance_mm: float | None = None,
        courtyard_margin_mm: float | None = None,
        edge_clearance_mm: float | None = None,
        hole_to_hole_mm: float | None = None,
        manufacturer: str | None = None,
        layer_count: int = 2,
        copper_weight_oz: float = 1.0,
        use_board_rules: bool = True,
    ) -> dict:
        """Check footprint placement geometry only (pad clearance, courtyard
        overlap, edge clearance, hole-to-hole) — NOT a full DRC.

        Narrower and faster than `pcb_run_drc`: useful as a quick check
        immediately after moving/placing footprints, before routing exists
        to check. Does not catch routing, connectivity, or manufacturing
        rule violations (unconnected nets, track width, annular ring,
        etc.) — use `pcb_run_drc` to list those. A clean result here says
        nothing about whether the board is finished: that verdict belongs
        to `verify_board` alone (see the `gate` field on this output).

        Rule sourcing (archived backlog P1-18): the four dimensions default to
        `None`, meaning "resolve them". Precedence is explicit argument >
        the board's own rules (`.kicad_pro` netclasses/constraints and
        `.kicad_dru`) > the `manufacturer` profile's capability floor >
        this project's built-in fallback. The value AND its source for
        every dimension come back in `rules_applied`, so an agent can
        always tell whether `0.2` was the designer's netclass, a fab
        minimum, or a fallback literal. A board value of `0.0` counts as
        unspecified (KiCad writes that for constraints never set), not as
        "no limit".

        Set `use_board_rules=False` to check against fab/built-in values
        only, ignoring what the design declares.
        """
        path = Path(pcb_path)
        if not path.is_file():
            return _incomplete(f"file not found: {pcb_path}")
        resolved = _resolve_rules(
            path,
            {
                "clearance_mm": clearance_mm,
                "courtyard_margin_mm": courtyard_margin_mm,
                "edge_clearance_mm": edge_clearance_mm,
                "hole_to_hole_mm": hole_to_hole_mm,
            },
            manufacturer,
            layer_count,
            copper_weight_oz,
            use_board_rules,
        )
        if "error" in resolved:
            return _incomplete(resolved["error"])
        rules = resolved["rules"]
        try:
            from kicad_tools.schema import PCB

            board = PCB.load(path)
            result = board.run_drc(
                clearance=rules["clearance_mm"],
                courtyard_margin=rules["courtyard_margin_mm"],
                edge_clearance=rules["edge_clearance_mm"],
                hole_to_hole=rules["hole_to_hole_mm"],
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad: evidence-gated verdict
            return _incomplete(f"placement check could not complete: {exc}")
        out = {
            "status": "complete",
            **_to_jsonable(result),
            "rules_applied": resolved["rules_applied"],
            "rule_precedence": resolved["rule_precedence"],
            "design_rules": resolved["board_design_rules"],
            "manufacturer_profile": resolved["manufacturer_profile"],
        }
        manufacturability = _manufacturability(path, manufacturer, layer_count, copper_weight_oz)
        if manufacturability is not None:
            out["manufacturability"] = manufacturability
        return out

    def sch_run_erc(sch_path: str) -> dict:
        """Run KiCad's electrical rule check (`kicad-cli sch erc`, all severities) on a
        schematic once and return every violation it reports as structured data.

        A single ERC run, NOT the acceptance gate: zero violations here says
        nothing about the board (the gate does not run ERC). Only
        `verify_board` gives a verdict, and only its PASS may be reported as
        routed or done.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        try:
            ensure_kicad_cli_on_path()  # kicad_tools' run_erc shells out to kicad-cli internally
            from kicad_tools.schema import Schematic

            report = Schematic.load(path).run_erc()
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"ERC could not complete: {exc}")
        return {"status": "complete", **_to_jsonable(report)}

    def pcb_check_collisions(
        pcb_path: str,
        clearance_mm: float | None = None,
        courtyard_margin_mm: float | None = None,
        manufacturer: str | None = None,
        layer_count: int = 2,
        copper_weight_oz: float = 1.0,
        use_board_rules: bool = True,
    ) -> dict:
        """Check every placed footprint for geometric overlap/clearance conflicts with its neighbors.

        A filtered view of `pcb_check_placement` (the placement-only
        geometry checker, NOT `pcb_run_drc`'s full DRC, and never a statement
        that the board passes: only `verify_board` says that) scoped to
        component-vs-component collision types (courtyard overlap, pad
        clearance, edge clearance, hole-to-hole, silkscreen-over-pad,
        off-board) — the direct answer to "did I just place two footprints
        on top of each other," returned as data (component pair, actual
        vs. required distance) instead of requiring image inspection.

        Clearance/courtyard values are resolved exactly as in
        `pcb_check_placement` (explicit > board's own rules > `manufacturer`
        capability floor > built-in default) and reported with their source
        in `rules_applied`.
        """
        path = Path(pcb_path)
        if not path.is_file():
            return _incomplete(f"file not found: {pcb_path}")
        resolved = _resolve_rules(
            path,
            {"clearance_mm": clearance_mm, "courtyard_margin_mm": courtyard_margin_mm},
            manufacturer,
            layer_count,
            copper_weight_oz,
            use_board_rules,
        )
        if "error" in resolved:
            return _incomplete(resolved["error"])
        rules = resolved["rules"]
        try:
            from kicad_tools.schema import PCB

            board = PCB.load(path)
            result = board.run_drc(
                clearance=rules["clearance_mm"], courtyard_margin=rules["courtyard_margin_mm"]
            )
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"collision check could not complete: {exc}")

        all_violations = _to_jsonable(result)["violations"]
        collisions = [v for v in all_violations if v["type"] in _COLLISION_TYPES]
        return {
            "status": "complete",
            "collision_count": len(collisions),
            "collisions": collisions,
            "rules_applied": resolved["rules_applied"],
            "rule_precedence": resolved["rule_precedence"],
        }

    def pcb_validate_proposed_placements(
        pcb_path: str,
        placements: dict[str, list[float]],
        clearance_mm: float | None = None,
        courtyard_margin_mm: float | None = None,
        manufacturer: str | None = None,
        layer_count: int = 2,
        copper_weight_oz: float = 1.0,
        use_board_rules: bool = True,
    ) -> dict:
        """Check a batch of proposed footprint placements for collisions before committing them.

        `placements` maps reference designator to [x_mm, y_mm, rotation_deg].
        Batch-oriented by design (docs/ARCHITECTURE.md: avoid one-item-per-call
        tools) — validate an entire placement plan in one call rather than
        looping a single-footprint collision check per component.

        Clearance/courtyard values are resolved exactly as in
        `pcb_check_placement` (explicit > board's own rules > `manufacturer`
        capability floor > built-in default) and reported with their source
        in `rules_applied`.

        A placement-geometry pre-check, never a statement that the board
        passes: only `verify_board` gives that verdict.
        """
        path = Path(pcb_path)
        if not path.is_file():
            return _incomplete(f"file not found: {pcb_path}")
        resolved = _resolve_rules(
            path,
            {"clearance_mm": clearance_mm, "courtyard_margin_mm": courtyard_margin_mm},
            manufacturer,
            layer_count,
            copper_weight_oz,
            use_board_rules,
        )
        if "error" in resolved:
            return _incomplete(resolved["error"])
        rules = resolved["rules"]
        try:
            from kicad_tools.schema import PCB

            board = PCB.load(path)
            typed_placements = {ref: (xyr[0], xyr[1], xyr[2]) for ref, xyr in placements.items()}
            result = board.validate_placements(
                typed_placements,
                clearance=rules["clearance_mm"],
                courtyard_margin=rules["courtyard_margin_mm"],
            )
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"placement validation could not complete: {exc}")
        return {
            "status": "complete",
            **_to_jsonable(result),
            "rules_applied": resolved["rules_applied"],
            "rule_precedence": resolved["rule_precedence"],
        }

    def get_manufacturer_design_rules(
        manufacturer: str, layer_count: int = 2, copper_weight_oz: float = 1.0
    ) -> dict:
        """Look up a PCB fab's published manufacturing capability limits.

        Answers "what is the smallest trace / via / annular ring / silkscreen
        this fab can actually make at this layer count?" — the numbers a
        design has to clear to be orderable, as opposed to the numbers the
        design happens to check itself against.

        Served from a local static snapshot of the fab's own published
        capabilities page (never fetched per call — these are stable specs,
        not live pricing), and every result carries `source_url` plus the
        date that page was actually read, so staleness is visible. Values
        the fab does not publish come back as `null` and are listed in
        `unconfirmed_fields` — an honest gap, never a plausible guess.

        Evidence-gated: an unrecognised manufacturer is an error naming
        every profile that IS available. There is no generic/default
        profile to silently fall back to.
        """
        try:
            profile = get_profile(manufacturer)
            rules = profile.get_rules(layer_count, copper_weight_oz)
        except (UnknownManufacturerError, UnsupportedConfigurationError) as exc:
            return _incomplete(str(exc), available=[p.id for p in list_profiles()])
        return {
            "status": "complete",
            **profile.to_dict(),
            "requested_layer_count": layer_count,
            "requested_copper_weight_oz": copper_weight_oz,
            "design_rules": rules.to_dict(),
            "canonical_floor_mm": profile.canonical_floor(layer_count, copper_weight_oz),
            "meaning": (
                "capability floor: the fab's published minimum. A design value BELOW "
                "one of these is not manufacturable here, even if the board's own DRC "
                "is clean. These are not the rules KiCad checks against -- see "
                "pcb_get_design_rules for those."
            ),
        }

    def list_manufacturer_profiles() -> dict:
        """List every PCB fab this server has a local design-rule profile for.

        Use before `get_manufacturer_design_rules` when the fab name is
        uncertain; the result includes accepted aliases and, for each
        profile, the source page and the date it was verified.
        """
        profiles = list_profiles()
        return {
            "status": "complete",
            "count": len(profiles),
            "manufacturers": [p.to_dict() for p in profiles],
        }

    def pcb_get_design_rules(pcb_paths: list[str]) -> dict:
        """Read the design rules each board ACTUALLY declares — netclasses,
        project constraints, and `.kicad_dru` custom rules.

        This is what KiCad's DRC measures a design against, so it is the
        answer to "clean according to whose numbers?". Batch: pass every
        board at once rather than looping a per-board call.

        Reports each rule's resolved value, the source it came from, and
        every competing candidate. Resolution is most-restrictive-wins
        across the three sources, matching KiCad's own behaviour; `0.0` or
        absent means unspecified, never "no limit". A board with no
        `.kicad_pro` and no `.kicad_dru` reports `partial`, so "nothing was
        configured" can never be misread as "checked and fine".
        """
        if not pcb_paths:
            return _incomplete("no pcb_paths given")
        boards = []
        for pcb_path in pcb_paths:
            try:
                boards.append(read_board_design_rules(pcb_path))
            except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
                boards.append(
                    {
                        "status": "incomplete",
                        "reason": f"could not read design rules: {exc}",
                        "pcb_path": pcb_path,
                    }
                )
        statuses = {b.get("status") for b in boards}
        return {
            "status": "complete" if statuses == {"complete"} else "partial",
            "count": len(boards),
            "boards": boards,
        }

    def pcb_check_manufacturability(
        pcb_paths: list[str],
        manufacturer: str,
        layer_count: int = 2,
        copper_weight_oz: float = 1.0,
    ) -> dict:
        """Check whether boards are below a fab's published capability floor.

        The question `pcb_run_drc` cannot answer: DRC only proves a design
        satisfies ITS OWN rules. If those rules were looser than the fab's
        process, a fully DRC-clean board is still unbuildable. This compares
        each board's effective rules against the fab's published minimums
        and reports, per rule, which side each number came from.

        Batch: pass every board at once. Evidence-gated per rule —
        `below_fab_floor` (a real problem), `ok`, `board_rule_unspecified`
        (the board declares nothing, so it could NOT be checked — an
        unverified gap, not a pass) and `not_published_by_fab` (the fab
        publishes no such number) are distinct verdicts, and any unverified
        rule downgrades the board's overall result to `partial` with
        `meets_fab_floor: null` rather than claiming a pass.

        Meeting the fab floor compares rules only and says nothing about the
        board's copper: only a `verify_board` PASS may be reported as routed
        or done.
        """
        if not pcb_paths:
            return _incomplete("no pcb_paths given")
        try:
            get_profile(manufacturer).get_rules(layer_count, copper_weight_oz)
        except (UnknownManufacturerError, UnsupportedConfigurationError) as exc:
            return _incomplete(str(exc), available=[p.id for p in list_profiles()])

        boards = [
            _manufacturability(Path(p), manufacturer, layer_count, copper_weight_oz) or {}
            for p in pcb_paths
        ]
        statuses = {b.get("status") for b in boards}
        failing = [b for b in boards if b.get("meets_fab_floor") is False]
        return {
            "status": "complete" if statuses == {"complete"} else "partial",
            "manufacturer": manufacturer,
            "layer_count": layer_count,
            "copper_weight_oz": copper_weight_oz,
            "count": len(boards),
            "boards_below_fab_floor": len(failing),
            "boards": boards,
        }

    # Every tool here only ever reads a file (.load()/.run_drc()/.run_erc()/
    # .validate_placements(), or kicad_cli.run_drc's own read-only subprocess
    # invocation) -- never .save() -- so all six are read-only, non-
    # destructive, and idempotent (same file + same params -> same result).
    # None of them make a network call or otherwise reach outside the local
    # filesystem/kicad-cli subprocess, so openWorldHint is False for all six
    # too, despite pcb_run_drc/sch_run_erc shelling out to kicad-cli --
    # that's a local process, not an external/open-world entity.
    _READ_ONLY_LOCAL = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    tools = [
        pcb_run_drc,
        pcb_check_placement,
        sch_run_erc,
        pcb_check_collisions,
        pcb_validate_proposed_placements,
        # archived backlog P1-18/P1-21: design-rule provenance. All four are
        # read-only too -- the profile lookups touch only local static data
        # (no network, hence openWorldHint stays False), and the board-rule
        # readers only parse the project's own files.
        get_manufacturer_design_rules,
        list_manufacturer_profiles,
        pcb_get_design_rules,
        pcb_check_manufacturability,
    ]
    for tool_fn in tools:
        mcp.add_tool(tool_fn, annotations=dict(_READ_ONLY_LOCAL))
    return [tool_fn.__name__ for tool_fn in tools]
