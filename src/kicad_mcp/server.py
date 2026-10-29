"""kicad-mcp server entrypoint.

Wires up the toolset registry (see toolsets/registry.py) and loads the
starter kit of always-available tools. Everything else registers itself
on demand via the `load_toolset` meta-tool, per docs/ARCHITECTURE.md's
"Toolset lazy-loading" convention.
"""

from __future__ import annotations

from typing import Literal

from mcp.server.mcpserver import Context, MCPServer

from kicad_mcp.gate_state import GateState, attach_gate_field
from kicad_mcp.toolsets.registry import ToolsetRegistry

# Toolsets always available at startup — kept deliberately small.
STARTER_TOOLSETS = ["project"]


def build_server() -> MCPServer:
    mcp = MCPServer("kicad-mcp")
    registry = ToolsetRegistry(mcp)
    gate_state = GateState()

    _register_all_toolsets(registry)

    @mcp.tool()
    def list_toolsets() -> list[dict]:
        """List available KiCad tool groups (schematic, pcb, sourcing, ...) and whether each is currently loaded."""
        return registry.list_toolsets()

    @mcp.tool()
    async def load_toolset(name: str, ctx: Context) -> dict:
        """Load a tool group's tools into this session, e.g. 'sourcing', 'schematic', 'pcb', 'verification'."""
        result = registry.load_toolset(name)
        if result.get("status") == "loaded":
            # add_tool/remove_tool don't auto-notify clients (MCP SDK v2.x has
            # no server-initiated push outside a handler) -- the caller must
            # explicitly publish the change or clients won't see new tools
            # until their next unrelated request.
            await ctx.notify_tools_changed()
        return result

    @mcp.tool()
    async def unload_toolset(name: str, ctx: Context) -> dict:
        """Unload a tool group's tools to free up the session's tool-schema budget."""
        result = registry.unload_toolset(name)
        if result.get("status") == "unloaded":
            await ctx.notify_tools_changed()
        return result

    # The acceptance gate is deliberately not a lazy toolset: an agent that never
    # loads "verification" must still be able to reach it (docs/ARCHITECTURE.md
    # decision 14). It is the only tool allowed to say a board is routed or done.
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def verify_board(
        pcb_path: str,
        parity: Literal["required", "not_applicable"],
        parity_reason: str | None = None,
        baseline: dict | None = None,
    ) -> dict:
        """The acceptance gate. Grades a board with real kicad-cli (three refilled runs, union of
        errors) and returns PASS, FAIL or INCOMPLETE plus the COMPLETE per-net unconnected list
        and the complete violation list. Only a PASS may be reported as routed or done.

        parity: "required" (a missing or unannotated schematic, or one with stale instance
        paths, is INCOMPLETE) or "not_applicable" (give parity_reason, e.g. an imported board
        with no schematic).
        baseline: the dict from pin_rule_baseline. Without it the verdict is INCOMPLETE,
        because rule weakening (.kicad_dru severity ignore, rule_severities) would go unseen.
        FAIL takes precedence over INCOMPLETE. incomplete_reasons say why a verdict is
        INCOMPLETE. notes are informational hints (e.g. the units of one part disagree on DNP):
        they are never reasons and never change the verdict, so a note neither doubts a PASS
        nor excuses a FAIL or INCOMPLETE."""
        from kicad_mcp import verify_board as gate

        snapshot = gate_state.begin_grading(pcb_path)  # before grading, never after
        result = gate.verify_board(
            pcb_path, parity=parity, parity_reason=parity_reason, baseline=baseline
        )
        gate_state.record_verdict(pcb_path, result["verdict"], snapshot)
        return {**result, "board_sha": snapshot.sha, "project_sha": snapshot.project_sha}

    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def pin_rule_baseline(pcb_path: str) -> dict:
        """Snapshot the rule settings verify_board grades against (rule severities, netclass
        limits, .kicad_dru severity clauses, ignored checks). Read-only: the caller stores the
        result and passes it as `baseline`. Pin before the work, not after it."""
        from kicad_mcp import verify_board as gate

        return gate.snapshot_rules(pcb_path)

    _gate_every_toolset_tool(mcp, gate_state)

    # Input preparation is always available (not a lazy toolset) and, unlike the gate
    # tools above, is registered AFTER the gate-field wrapper is installed so its
    # outputs carry ``gate``. The wrapper treats the non-read-only confirm as a
    # mutation of both ``pcb_path`` (source) and the returned ``pcb_path`` (the
    # prepared board), so verdicts for both are stale.
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def propose_input_preparation(pcb_path: str, output_dir: str | None = None) -> dict:
        """Dry run of input preparation: lists every copper-layer text (F.Cu/B.Cu gr_text) that
        would move to silkscreen, and returns a revision_token. Writes nothing.

        Run this step once per board, BEFORE pin_rule_baseline and before any routing; never
        inside a routing attempt. Copper text it cannot handle (inner-layer text, text boxes) is
        listed under `unhandled`, and the input floor is then NOT a clean baseline. With nothing
        to move there is no token (status nothing_to_do). This tool gives no verdict: only
        verify_board does.

        output_dir: new directory for the prepared project copy (default: sibling
        `<project>_prepared`); it must not be inside the source project or non-empty."""
        from kicad_mcp import input_prep

        return input_prep.propose_input_preparation(pcb_path, output_dir)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    def confirm_input_preparation(
        pcb_path: str,
        revision_token: str,
        parity: Literal["required", "not_applicable"],
        parity_reason: str | None = None,
    ) -> dict:
        """Apply a proposal from propose_input_preparation: writes a NEW prepared board revision
        (the source is untouched), re-pins the rule baseline on it and reports the `input_floor`
        (errors present before any routing; never waive them).

        Runs once per board, BEFORE the pin_rule_baseline you use for routing, never inside an
        attempt: use the returned `output_pcb` and `baseline` from here on. Copper text it cannot
        handle is listed under `unhandled`; the floor is then NOT a clean baseline. The
        revision_token is single use. input_floor carries `grading_status` and `certified: false`,
        not a verdict (the prepared board's gate stays NOT_RUN/stale): only verify_board gives a
        verdict. pcb_path in the result is the prepared board (same as output_pcb); the board you
        passed in is `source_pcb`.

        parity: "required" or "not_applicable" (give parity_reason), passed to the gate unchanged."""
        from kicad_mcp import input_prep

        result = input_prep.confirm_input_preparation(
            pcb_path, revision_token, parity=parity, parity_reason=parity_reason
        )
        output = result.get("output_pcb")
        if isinstance(output, str):
            # Lets the gate-field wrapper mark the prepared board as mutated and show its gate.
            result = {**result, "pcb_path": output, "source_pcb": pcb_path}
        return result

    # The pour step (planes first). Same registration rules as input preparation: after
    # the gate-field wrapper, always available. The step's test seams (drc, router, fill,
    # connectivity_check) are deliberately not parameters and never forwarded; a
    # source-scan test in tests/test_pour_step.py guards this file.
    @mcp.tool(
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        }
    )
    def propose_pour_step(
        pcb_path: str, pours: dict[str, str], output_dir: str | None = None
    ) -> dict:
        """Dry run of the pour step: validates the requested power planes and returns a
        revision_token. Writes nothing and runs no router.

        pours: {net name: copper layer}, one net per layer, e.g. {"GND": "B.Cu"}. A net that
        already has a pour on that layer is skipped. Run propose_input_preparation /
        confirm_input_preparation first (copper-layer text is refused by confirm), then
        pin_rule_baseline, then this step once, before routing signals. output_dir: a new
        directory (default sibling `<project>_poured`); not inside the source project.
        This tool gives no verdict: only verify_board does."""
        from kicad_mcp import pour_step

        return pour_step.propose_pour_step(pcb_path, pours=pours, output_dir=output_dir)

    @mcp.tool(
        annotations={
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": False,
            "openWorldHint": False,
        }
    )
    def confirm_pour_step(
        pcb_path: str,
        revision_token: str,
        parity: Literal["required", "not_applicable"],
        baseline: dict,
        parity_reason: str | None = None,
    ) -> dict:
        """Run the plane router on a scratch copy of the board, grade its candidate, and only if
        accepted write a NEW board revision with the zone (plus any vias/tracks the plane script
        added); the source is untouched. The revision_token is single use.

        baseline: the dict from pin_rule_baseline, REQUIRED (this step never pins silently; an
        empty baseline is refused as incomplete). Input preparation (propose_input_preparation) must have run first: a board
        with copper-layer text is refused.

        What status `poured` proves: the candidate was graded by verify_board's gate, the zone is
        filled (measured in a kicad-cli refilled copy; the file on disk holds the outline only)
        and no pad was found isolated by the checks listed in the result. It does NOT prove the
        board is connected or complete: each pad in `pads` carries `connectivity: proven|unproven`
        (unproven pads, e.g. waiting for a via, and every open signal remain). Only verify_board
        certifies a board.

        Cost per confirm: about 12 gate runs (kicad-cli DRC) plus connectivity runs, capped at 40
        in total; expect minutes, not seconds. Over the cap a pad is never accepted unproven.

        In the result, pcb_path is the new revision (same as output_pcb) and source_pcb is the
        board you passed in; both gates are stale until verify_board runs on the revision."""
        from kicad_mcp import pour_step

        result = pour_step.confirm_pour_step(
            pcb_path,
            revision_token,
            parity=parity,
            parity_reason=parity_reason,
            baseline=baseline or None,
        )
        output = result.get("output_pcb")
        if isinstance(output, str):
            result = {**result, "pcb_path": output, "source_pcb": pcb_path}
        return result

    # Layout checkpoint pairs (ticket 21): after the gate-field wrapper, always available.
    # The modules' test seams (drc, footprint_dirs, netlist_exporter, _clock) are never
    # parameters and never forwarded; tests/test_layout_tools.py scans this file for that.
    _register_layout_tools(mcp)

    for name in STARTER_TOOLSETS:
        registry.load_toolset(name)

    return mcp


_LAYOUT_ORDER = (
    "Order for a project: import_from_schematic -> board_outline -> board_rules -> "
    "input_preparation -> silkscreen_minimums -> pin_rule_baseline -> pour step -> routing. "
    "Every confirm writes a NEW board revision beside the source (the source is untouched) and "
    "returns it as output_pcb (also pcb_path; the board you passed in is source_pcb): continue "
    "from that path. Both gates are stale until verify_board runs; only verify_board certifies."
)
_FLAG_RULE = (
    "set only after a human has read the listed items; never set it to get past a refusal on "
    "your own"
)
_READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
_DESTRUCTIVE = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": False,
    "openWorldHint": False,
}


def _as_revision(result: dict, source: str) -> dict:
    """Name the prepared board as ``pcb_path`` so the gate-field wrapper marks both the
    source and the revision mutated and returns ``gates`` for both."""
    output = result.get("output_pcb")
    if isinstance(output, str):
        return {**result, "pcb_path": output, "source_pcb": source}
    return result


def _register_layout_tools(mcp: MCPServer) -> None:
    @mcp.tool(
        annotations=_READ_ONLY,
        description=(
            "Dry run of importing footprints and nets from the project's schematic into the "
            "board (Update PCB from Schematic): lists every footprint and net that would be "
            "added and where new footprints are staged; writes nothing and returns a "
            "revision_token. Any footprint that cannot be resolved through the project "
            "fp-lib-table / installed libraries blocks the proposal (status blocked, no token) "
            "and is named. Staged footprints are not placed: placement is a separate step. "
            "schematic_path defaults to the board's own .kicad_sch; staging_origin is [x, y] "
            "in page mm (default: right of the Edge.Cuts box). " + _LAYOUT_ORDER
        ),
    )
    def propose_schematic_import(
        pcb_path: str,
        schematic_path: str | None = None,
        output_dir: str | None = None,
        staging_origin: tuple[float, float] | None = None,
        columns: int = 5,
        spacing: float = 10.0,
    ) -> dict:
        from kicad_mcp import schematic_import

        return schematic_import.propose_schematic_import(
            pcb_path,
            schematic_path,
            output_dir,
            staging_origin=staging_origin,
            columns=columns,
            spacing=spacing,
        )

    @mcp.tool(
        annotations=_DESTRUCTIVE,
        description=(
            "Apply a propose_schematic_import proposal: writes a NEW board revision with the "
            "schematic's footprints and every pad net, verified by a fresh reload against the "
            "kicad-cli netlist; an unresolved footprint fails the whole confirm and nothing is "
            "written. The revision_token is single use. " + _LAYOUT_ORDER
        ),
    )
    def confirm_schematic_import(pcb_path: str, revision_token: str) -> dict:
        from kicad_mcp import schematic_import

        result = schematic_import.confirm_schematic_import(pcb_path, revision_token)
        return _as_revision(result, pcb_path)

    @mcp.tool(
        annotations=_READ_ONLY,
        description=(
            "Dry run of setting the board's design rules (minimum clearance, track width, via "
            "diameter, via annular width, through-hole diameter, and named net classes with "
            "their nets) in the project's .kicad_pro; lists every value that would change, "
            "every loosening and every incoherence; writes nothing and returns a "
            "revision_token. Lengths are mm. A key absent from the project counts as KiCad's "
            "built-in default, never as unset. netclasses: [{name, clearance?, track_width?, "
            "via_diameter?, via_drill?, priority?, nets?: [exact net names]}]. "
            "allow_loosen: lowering a rule is refused unless this is true; " + _FLAG_RULE + ". "
            "allow_incoherent: a class value below a board minimum is refused unless this is "
            "true; " + _FLAG_RULE + ". " + _LAYOUT_ORDER
        ),
    )
    def propose_board_rules(
        pcb_path: str,
        min_clearance: float | None = None,
        min_track_width: float | None = None,
        via_diameter: float | None = None,
        min_via_annular_width: float | None = None,
        min_through_hole_diameter: float | None = None,
        netclasses: list[dict] | None = None,
        allow_loosen: bool = False,
        allow_incoherent: bool = False,
        output_dir: str | None = None,
    ) -> dict:
        from kicad_mcp import board_rules

        return board_rules.propose_board_rules(
            pcb_path,
            min_clearance=min_clearance,
            min_track_width=min_track_width,
            via_diameter=via_diameter,
            min_via_annular_width=min_via_annular_width,
            min_through_hole_diameter=min_through_hole_diameter,
            netclasses=netclasses,
            allow_loosen=allow_loosen,
            allow_incoherent=allow_incoherent,
            output_dir=output_dir,
        )

    @mcp.tool(
        annotations=_DESTRUCTIVE,
        description=(
            "Apply a propose_board_rules proposal: writes a NEW project revision with the rules, "
            "verified by a fresh reload, then pins the rule baseline of that revision and grades "
            "it (the `grading` field is not a verdict). Any rule change makes every earlier "
            "baseline pin and verdict void: use the returned `baseline` and re-run verify_board "
            "on output_pcb. The revision_token is single use. parity: \"required\" or "
            "\"not_applicable\" (give parity_reason), passed to the gate unchanged. "
            + _LAYOUT_ORDER
        ),
    )
    def confirm_board_rules(
        pcb_path: str,
        revision_token: str,
        parity: Literal["required", "not_applicable"],
        parity_reason: str | None = None,
    ) -> dict:
        from kicad_mcp import board_rules

        result = board_rules.confirm_board_rules(
            pcb_path, revision_token, parity=parity, parity_reason=parity_reason
        )
        return _as_revision(result, pcb_path)

    @mcp.tool(
        annotations=_READ_ONLY,
        description=(
            "Dry run of replacing the board outline (one rectangle, optional rounded corners) "
            "and adding mounting holes with copper keepouts; lists what would be replaced, "
            "which footprints would fall outside the new outline (reported, never moved) and "
            "any hole that touches existing items; writes nothing and returns a revision_token. "
            "outline: {x, y, width, height, corner_radius?} in mm. mounting_holes: "
            "[{reference, x, y, keepout_radius_mm, footprint?}]. origin: \"page\" (file "
            "coordinates, default) or \"aux_axis\" (relative to the board's aux axis origin). "
            "allow_overlap: a hole whose keepout touches footprints, tracks, vias, zones or "
            "another hole is refused unless this is true; " + _FLAG_RULE + ". "
            + _LAYOUT_ORDER
        ),
    )
    def propose_board_outline(
        pcb_path: str,
        outline: dict,
        mounting_holes: list[dict] | None = None,
        origin: Literal["page", "aux_axis"] = "page",
        output_dir: str | None = None,
        allow_overlap: bool = False,
    ) -> dict:
        from kicad_mcp import board_outline

        return board_outline.propose_board_outline(
            pcb_path,
            outline,
            mounting_holes=mounting_holes,
            origin=origin,
            output_dir=output_dir,
            allow_overlap=allow_overlap,
        )

    @mcp.tool(
        annotations=_DESTRUCTIVE,
        description=(
            "Apply a propose_board_outline proposal: writes a NEW board revision with the "
            "outline and mounting holes, verified by a fresh reload and a kicad-cli outline "
            "check; copper pours under a new keepout need refilling before any pour or DRC "
            "figure means anything. The revision_token is single use. " + _LAYOUT_ORDER
        ),
    )
    def confirm_board_outline(pcb_path: str, revision_token: str) -> dict:
        from kicad_mcp import board_outline

        result = board_outline.confirm_board_outline(pcb_path, revision_token)
        return _as_revision(result, pcb_path)

    @mcp.tool(
        annotations=_READ_ONLY,
        description=(
            "Dry run of raising every silkscreen text below the fab minimums (height and "
            "stroke) on a NEW revision; lists each text with its before and after size, and "
            "any text it cannot handle under `unhandled`; writes nothing and returns a "
            "revision_token (none when there is nothing to raise). It never moves, hides or "
            "re-layers text: overlaps the larger text may create are for verify_board to "
            "report. min_height_mm default 1.0, min_thickness_mm default 0.15. "
            + _LAYOUT_ORDER
        ),
    )
    def propose_silkscreen_minimums(
        pcb_path: str,
        output_dir: str | None = None,
        min_height_mm: float = 1.0,
        min_thickness_mm: float = 0.15,
    ) -> dict:
        from kicad_mcp import silk_minimums

        return silk_minimums.propose_silkscreen_minimums(
            pcb_path, output_dir, min_height_mm, min_thickness_mm
        )

    @mcp.tool(
        annotations=_DESTRUCTIVE,
        description=(
            "Apply a propose_silkscreen_minimums proposal: writes a NEW board revision with the "
            "raised text, verified by a fresh reload against the plan. The revision_token is "
            "single use. " + _LAYOUT_ORDER
        ),
    )
    def confirm_silkscreen_minimums(pcb_path: str, revision_token: str) -> dict:
        from kicad_mcp import silk_minimums

        result = silk_minimums.confirm_silkscreen_minimums(pcb_path, revision_token)
        return _as_revision(result, pcb_path)


def _gate_every_toolset_tool(mcp: MCPServer, state: GateState) -> None:
    """Make every tool a toolset registers carry the ``gate`` field.

    Toolsets register through ``mcp.add_tool``; the instance's ``add_tool`` is
    wrapped so each tool is registered through ``attach_gate_field`` without
    any toolset knowing about it. The gate tools themselves are registered
    before this runs and so are never wrapped.
    """
    original = mcp.add_tool

    def add_tool(fn, *args, **kwargs):
        annotations = kwargs.get("annotations")
        return original(attach_gate_field(fn, annotations, state), *args, **kwargs)

    mcp.add_tool = add_tool  # type: ignore[method-assign]


def _register_all_toolsets(registry: ToolsetRegistry) -> None:
    from kicad_mcp.toolsets import project as project_toolset

    registry.register("project", "Project scaffolding: create/open KiCad projects", project_toolset.build)

    try:
        from kicad_mcp.toolsets import verification as verification_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "verification",
            "Real kicad-cli DRC/ERC runs (single runs, NOT the acceptance gate: only verify_board gives a verdict) plus fast placement-geometry/collision checks (kicad-tools-backed) -- structured output, no image rendering required",
            verification_toolset.build,
        )

    try:
        from kicad_mcp.toolsets import sourcing as sourcing_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "sourcing",
            "Component search across Nexar/Digi-Key/LCSC providers, merged and deduped into one paginated list",
            sourcing_toolset.build,
        )

    try:
        from kicad_mcp.toolsets import diagnostics as diagnostics_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "diagnostics",
            "Non-authoritative visual rendering of a board/schematic for human-style sanity checks -- use 'verification' to list problems and verify_board for the verdict",
            diagnostics_toolset.build,
        )

    try:
        from kicad_mcp.toolsets import library as library_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "library",
            "Register fetched/generated symbols and footprints into a project's local library tables (portable, project-relative paths only), dedup, minimal KLC checks",
            library_toolset.build,
        )

    try:
        from kicad_mcp.toolsets import schematic as schematic_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "schematic",
            "Symbol placement, wiring, labels, footprint assignment, annotation fixing, and connectivity checking on a schematic",
            schematic_toolset.build,
        )

    try:
        from kicad_mcp.toolsets import placement as placement_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "placement",
            "PCB footprint placement: batch place/move, courtyard-accurate alignment/distribution/grid-snap/dense-packing, and a checkpointed autoplacement proposal",
            placement_toolset.build,
        )

    try:
        from kicad_mcp.toolsets import manufacturing as manufacturing_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "manufacturing",
            "Manufacturing file export (gerbers, drill, BOM, CPL) for a target manufacturer, e.g. JLCPCB's BOM/CPL format with LCSC part numbers, and KiKit panelization -- exporting checks nothing: only verify_board gives a verdict",
            manufacturing_toolset.build,
        )

    try:
        from kicad_mcp.toolsets import routing as routing_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "routing",
            "Track/via placement, ratsnest queries, and a checkpointed Freerouting autorouting proposal (real local subprocess, review required before applying) -- no routing tool gives a verdict: only verify_board does",
            routing_toolset.build,
        )

    try:
        from kicad_mcp.toolsets import copilot as copilot_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "copilot",
            "Live copilot mode: connect to a running KiCad GUI over its IPC API, read live board/schematic state and the human's current selection, and apply small reviewed changes into their session -- fails closed rather than editing files behind an open editor",
            copilot_toolset.build,
        )

    try:
        from kicad_mcp.toolsets import simulation as simulation_toolset
    except ImportError:
        pass
    else:
        registry.register(
            "simulation",
            "SPICE netlist export (real kicad-cli) and headless ngspice simulation (DC/transient/AC), e.g. verifying an RC filter's cutoff frequency before layout",
            simulation_toolset.build,
        )


def main() -> None:
    server = build_server()
    server.run()


if __name__ == "__main__":
    main()
