# Roadmap

Rewritten for the 2026-10-11 pivot. The old Phases 0-6 measured progress by "the tools exist". All of them were marked done, yet no board was ever finished. Progress is now measured only by climbing the **acceptance ladder**. A rung is passed when its board gets a `verify_board` PASS (docs/TESTING.md). Day-to-day work is charted in the maintainers' planning records (Wayfinder maps and tickets, kept outside the public repository; docs/CONTEXT.md explains the terms).

## Step 0: build the gate (done)

`verify_board`: one machine-checked verdict (`PASS`, `FAIL` or `INCOMPLETE`) plus the complete per-net unconnected list and the complete violation list, all taken from `kicad-cli`. Contract: docs/TESTING.md and docs/ARCHITECTURE.md decision 14. Built in `src/kicad_mcp/verify_board.py` and always loaded, with `pin_rule_baseline`. It has not yet returned a plain PASS on any rung; the only PASSes so far are the five necked rung-1 boards of protocol 6 (docs/TESTING.md). Only a PASS may call a board routed or done.

## The ladder

The ladder's rungs, status and requirements live in docs/TESTING.md, "Acceptance ladder". The first rung that fails defines the autonomous ceiling.

**Router choice:** for each rung, run both backends as equals and let the gate pick (no fixed lead until a fair re-measurement ranks them, `paradigm-review-2` tickets 05 and 17; build ticket 47 ran it under protocol 5 and could not rank them, 0 of 20 attempts passing; the protocol-6 rung-1 batches (docs/TESTING.md, "Acceptance ladder") are the first separation). A backend that is never the one passing on any rung does not earn more integration work. The KRT feature inventory (a checklist of the KRT v0.23.0 tools and flags, kept in the maintainers' research records; docs/ARCHITECTURE.md decision 3 lists the features most easily overlooked) decides which KRT features get exercised on which rung. New KRT feature integration is paused until the fair re-measurement (build ticket 47); the goal that every KRT capability is used resumes once KRT is the router that passes on a rung (`paradigm-review-2` ticket 05).

## Copilot tier (default)

The human owns power routing and zone topology. The agent does schematic, placement, signal routing (with power and GND excluded) and verification, and runs `verify_board` after each human or agent step. Prerequisite that is not built yet:
- File-drift detection for file mode (P1-19), because the human edits in KiCad between tool calls. The checkpoint pairs built since the pivot refuse a confirm when their source changed after the proposal, but no tool guards against drift in general.

Built: the gate (`verify_board`) and a pour step (`propose_pour_step`/`confirm_pour_step`, build tickets 04 and 15) that carries a gate-graded plane zone for autonomous boards with a GND pour; rung 1 uses it. A general zone-editing tool (the rest of P3-07) is not built; the human does the zones on copilot boards.

A copilot-mode openMPPT ticket may follow now that `verify_board` exists. It is not scheduled.

## openMPPT: frozen state (for whoever resumes it)

The board is in the separate MPPT hardware repository (not part of this one), branch `hardware/PCB_v1.3`, tip `8f8556a`.
- **Copper on the board:** 954 segments, 111 vias and 7 zones. 61 non-power nets went through KRT, with the 7 power and GND nets excluded and then poured. The board is **not routed**.
- **Unconnected nets:** a complete DRC breakdown found 13 unconnected items on nets that should have been routed (`SCK`, `MISO`, `MOSI`, `SCL`, `SDA`, `TX`, `LED`, `USB_DP`, plus near-miss gaps). With the zone polygons confirmed correctly sized, `GND` still shows 113 unconnected pads (unexplained). Two stitching-via attempts each made it worse.
- **Shorts:** routing the 8 missing nets alone works, but applied on top of the current board it creates 35 shorts. Reverted.
- **Mounting holes:** these were never off the board; that claim came from a wrong `kicad_tools` position. Moving them to the user-confirmed corner targets collides with C18, C20, VOUT1 and VIN1. Reverted.
- **Further reading:** `hardware/REVIEW_FINDINGS.md` in the MPPT repository; the earlier attempt's records are with the maintainers.

## Carried-over defects (fix when a rung needs them, sooner if they lie silently)

| ID @3380b34 | Defect | Why it matters for the ladder |
|---|---|---|
| ~~P3-08~~ | ~~`place_footprints` resets rotation when a spec omits it~~ Fixed in `576d35f` | Silent corruption during placement |
| P3-11 | `align_footprints` reports `ok` without moving anything | Silent no-op |
| P3-13 | Audit `src/` for `Pad.size`/`drill` writes that `kicad_tools` silently drops | Silent lost write |
| P4-11 | `kicad_tools` origin/coordinate-frame bugs: one root cause or several? | Every geometry claim depends on it |
| P2-07 | Schematic tools corrupt hierarchical projects | Rung 2 or 3 if the board is hierarchical |
| P3-07 / P3-12 | No general zone tool (the pour step covers plane pours); no track/via removal tool | Zone edits beyond a plane pour; any re-route |
| P4-08 | Run the first round of routing attempts in parallel (bounded by CPU count) | Wall-clock time per rung attempt |
| P1-25 | Batch `get_jlcpcb_part_details` plus a voltage/current rating check | Sourcing correctness (openMPPT had 0603 resistors at 78 V on a 75 V rating) |
| P1-02 / P1-03 | Nexar / Digi-Key providers | Blocked on credentials only the user can obtain |

## Non-goals

- **Beating autorouting or autoplacement algorithms.** Our job is integration, verification and an honest promise, not a routing breakthrough.
- **Claiming autonomy above the measured ceiling,** and an autonomous promise for power stages, zones or wide-current nets until a rung proves it.
- **A circuit-description DSL** of the atopile, circuit-synth or Zener kind. We operate on real KiCad projects.
- **Cloud routers** (Quilter, DeepPCB) as a default. They upload the design. They are an opt-in at most.
- **Parallelising KRT's own Rust A\* core,** which is serial by architecture (P4-08).
- **Automatically placing or paying for fab orders.**
- **An auto-fix loop for ERC violations** (P2-04). It would create false confidence.

## Deferred domains (each needs its own map when picked up)

Differential pairs and controlled impedance; thermal; EMI and return paths; mechanical fit and enclosures; creepage for high voltage (`kicad_tools.creepage` is unused); test points and ICT; assembly variants; design diffing; Linux and macOS support (everything so far is Windows-only); packaging and distribution.
