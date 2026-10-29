# Overview: ten minutes with boardproof

This page is for a reader who has never seen the repository. It says what the project is, how a tool call works, what counts as proof that a board is finished, and where to read next. [docs/ARCHITECTURE.md](ARCHITECTURE.md) is the decision record behind it; this page is the current picture.

## Purpose and honest status

boardproof is a Python MCP server (code in `src/kicad_mcp/`) that lets an AI agent do KiCad 10 design work: part sourcing, schematic construction, placement, routing, verification, SPICE simulation and manufacturing export. It works on the project files and on `kicad-cli`; no KiCad GUI has to run.

The project exists because an earlier run on a real board claimed "routed" and was wrong. Since then the rule is that an agent's word, a router's score and a library's convenience property are never evidence. Evidence is the output of `kicad-cli`, read in full.

As of 2026-10-29 no board has passed the acceptance gate as a plain PASS. The first official batches under protocol 6 gave Freerouting 5 of 5 `PASS (necked)` on rung 1 and KiCadRoutingTools 0 of 5. The rung-1 board changed afterwards (protocols 7 and 8) and has no official batch under the current protocol. Rung 2 cannot pass for any router because its input already carries 6 silkscreen errors. The product promise is therefore tiered: the **Copilot tier** (you own power routing and zones) is the default, and the **Autonomous tier** is earned one board class at a time. [README.md](../README.md) has the numbers and [docs/ROADMAP.md](ROADMAP.md) the ladder.

## Map of `src/kicad_mcp/`

| Path | What it is |
|---|---|
| `server.py` | Builds the MCP server, registers the always-loaded tools (gate and checkpointed pairs) and the lazy toolsets |
| `toolsets/` | The lazy toolsets: `project`, `verification`, `sourcing`, `diagnostics`, `library`, `schematic`, `placement`, `manufacturing`, `routing`, `copilot`, `simulation`; `registry.py` loads and unloads them |
| `verify_board.py`, `gate_state.py` | The acceptance gate and the session store behind the `gate` field every board-state tool returns |
| `write_layer.py`, `_self_check.py` | The only way a KiCad file is written; reload-and-compare for writing tools |
| `checkpoint.py`, `kicad_lock.py` | Propose and confirm with a revision token and a drift guard; refusal to write a file KiCad has open |
| `raw_geometry.py`, `sheet_tree.py` | Readers that take positions and sheet structure from raw S-expressions |
| `input_prep.py`, `pour_step.py`, `board_outline.py`, `board_rules.py`, `silk_minimums.py`, `schematic_import.py` | The checkpointed steps that prepare a board for routing |
| `routers/` | Wrappers around KiCadRoutingTools and its placer, and KiCad's own Specctra export and import for Freerouting |
| `rung_runner.py`, `rung_backends.py`, `autonomy_run.py`, `autonomy_protocol.json` | The rung runner: one measured attempt of one rung on one backend, and the frozen protocol it follows |
| `sourcing/`, `manufacturing_profiles/`, `ipc/` | Part search and assets, fab capability profiles, the live-IPC adapter. `sourcing/data/jlcpcb_basic_index.json` is the JLCPCB part-tier index; the library it was built from is the optional submodule `third_party/JLCPCB-Kicad-Library` (see the README) |
| `schematic_readability.py` | A read-only schematic readability checker, run as `python -m kicad_mcp.schematic_readability` |

The server starts with 19 tools and the remaining 65 sit in ten toolsets that load on demand (84 in total).

## Life of a tool call

**Lazy toolsets.** At start only the `project` toolset and the always-loaded tools exist. An agent calls `list_toolsets`, then `load_toolset("routing")`, and the server announces the changed tool list. This keeps the list an agent reads small.

**Tools take collections.** `place_footprints`, `add_wires`, `assign_footprints` and the others take lists or patterns, not one item per call. This is a project requirement.

**Read-only calls** read the files, or `kicad-cli` output, and return structured results. Every tool that returns board state also carries a `gate` field with the last gate verdict for that exact file and whether it is stale.

**Mutations that matter use a checkpoint.** A `propose_*` tool changes nothing. It returns a dry-run description and a `revision_token`. The matching `confirm_*` applies exactly that proposal. For the checkpointed pairs built since the pivot (`checkpoint.py`, five modules use it), the token contains a hash of the project's files, so a confirm is refused if someone, usually a person in KiCad, changed the files in between. There is no override. The general **drift guard**, which would check one project-wide hash before every write and refuse with `drift_detected`, is designed (docs/ARCHITECTURE.md decision 12) but not built yet (build ticket 40), and the guard that refuses to write a file KiCad has open (`kicad_lock.py`, refusal code `file_open_in_kicad`) exists as a module but is not wired into the tools yet (build ticket 41).

**Writes are verified before they replace.** That is the rule for every writing tool, and 15 of the 28 writing tools prove it by a reload-and-compare, 5 check by weaker means and 8 have no self-check yet (counted in `tests/ratchets.toml` on 2026-10-29, where the lists may only shrink). A writing tool saves a new revision to a temporary sibling file, the **self-check** reloads that file and compares it with the intended change, and only a copy that passed is moved over the original with `os.replace`. On a mismatch the original stays untouched and the tool returns `ok: false` with `write_not_verified`. The writes themselves go through the write layer, which owns what KiCad silently depends on: string escaping, number precision and pad angles. The move onto it is nearly complete: 3 high-level saves (a generated footprint, the empty schematic of a new project, one annotation fixer) and a list of raw writes are still on shrink-only ratchet lists. A passed self-check means only "the write landed".

**A mutating tool's own result is never the verdict.** It says what it did. Whether the board is good is the gate's business.

## The gate: `verify_board`

`verify_board` is always loaded. It returns one of three verdicts:

- `PASS`: zero unconnected items on every net, zero error-level DRC violations, every zone filled and verified, and the schematic and board agree.
- `FAIL`: the board is demonstrably bad. The complete per-net list of unconnected items and the complete list of violations come with it.
- `INCOMPLETE`: the evidence is missing (a `kicad-cli` failure, a missing or unannotated schematic, a rules deviation from the pinned baseline). It never counts as a pass.

How it earns that: it runs `kicad-cli pcb drc` three times on fresh copies, each with the zones refilled, and takes the union of the errors, so a flaky run cannot hide one. It grades against the board's own rules, pinned beforehand with `pin_rule_baseline`, so a router that rewrites the rules to look clean is caught. A netlist guard compares the component count of the schematic's sheet tree with what `kicad-cli` exports, because KiCad silently drops a sub-sheet it cannot read. There are no waivers.

The full contract is in [docs/TESTING.md](TESTING.md) and docs/ARCHITECTURE.md decision 14.

## External tools at arm's length

KiCadRoutingTools, Freerouting, ngspice, KiKit and easyeda2kicad all run as subprocesses with an explicit argument list, never a shell string. Each is found through an environment variable, then `PATH`, then the usual install locations. A missing tool produces `status: incomplete` with install instructions. The copyleft tools (Freerouting, GPLv3, and easyeda2kicad, AGPL-3.0) are never imported. Their output is treated as a proposal: it is graded by `kicad-cli`, and a router's own score is never trusted. [THIRD_PARTY.md](../THIRD_PARTY.md) lists every tool and licence.

## The ladder and what has been measured

The **acceptance ladder** is an ordered set of reference boards. A pipeline climbs it by getting a `verify_board` PASS on each. The first rung that fails marks the autonomous ceiling.

1. **Rung 1:** a USB-C ATtiny1616 breakout, built through the tools from a specification.
2. **Rung 2:** KiCad's `pic_programmer` demo with its tracks removed and its placement kept (about 30 nets, 247 pads).
3. **Rung 3:** the openMPPT board (68 multi-pad nets, 7 zones, a 20 A class), frozen until rungs 1 and 2 pass.

Each measurement follows a frozen protocol: a fixed plan of at most five attempts per backend, the same input for every attempt, results reported per backend and never pooled, and only a clean committed tree counts as official. Under protocol 6, Freerouting 2.5.0 reached `PASS (necked)` in 5 of 5 rung-1 attempts, and KiCadRoutingTools 0.23.1 passed 0 of 5. A `PASS (necked)` is a gate PASS with counted, forced necks at pad entries; it is always labelled and is a weaker statement than a plain PASS. The owner's decision on a neck below the pad limit is still open, and the 5 of 5 depends on it: the first segment of each neck is 0.15 mm wide where the pad would allow 0.2 mm, so under a stricter reading all five attempts would FAIL. Rung 2 was measured under protocol 5 (no protocol-6 batch has run there) and cannot pass for any router until its input's 6 silkscreen errors are dealt with. The exact numbers and the open build tickets are in [docs/ROADMAP.md](ROADMAP.md) and [docs/TESTING.md](TESTING.md).

## Tests and the batch gate

The suite has around 4,490 tests (collected on 2026-10-29). Their oracle is a file KiCad itself wrote, with reference values from `kicad-cli` made in the same run; a library is never its own oracle. Tools that mutate files are tested by reloading the file, and the rule is that every self-check has a test that feeds it a corrupted write and shows it fires. That is not yet true for all of them: `FROZEN_WITHOUT_PROVEN_SELF_CHECK` in `tests/test_ratchets.py` lists the 14 writing tools (counted 2026-10-29) with no proven self-check, among them one reload-and-compare tool (`panelize_board`) whose check no test has yet shown to fire; the list may only shrink. A set of **ratchets** (`tests/test_ratchets.py`) holds counts of known bad patterns in the source that may only go down.

There is no hosted CI. The test gate is local: `python scripts/batch_gate.py` runs the whole suite and the linter on a clean committed tree and ends with one line, `BATCH GATE <sha>: GREEN` or `RED`. It takes about 45 minutes. A **known red** is a test listed in `scripts/known-red.txt`; any other failure blocks a merge.

## Where to read next

- [docs/CONTEXT.md](CONTEXT.md): the glossary. Terms such as board class, checkpoint, drift and ratchet are defined there.
- [docs/ARCHITECTURE.md](ARCHITECTURE.md): the decision record and, at the end, the known facts and gotchas with their evidence. It is long; start with "Decisions".
- [docs/ROADMAP.md](ROADMAP.md): the Copilot tier, the frozen openMPPT state, the router-choice rule, carried-over defects, non-goals and deferred domains. The ladder itself (rungs, status, requirements) is in docs/TESTING.md.
- [docs/TESTING.md](TESTING.md): the gate's contract, the acceptance ladder (rungs, status, requirements) and the test suite.
- [CONTRIBUTING.md](../CONTRIBUTING.md): how to propose a change.
- [CLAUDE.md](../CLAUDE.md): the rules AI agents follow when they work in this repository.
