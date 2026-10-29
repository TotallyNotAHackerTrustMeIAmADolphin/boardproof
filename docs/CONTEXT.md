# Kicad-MCP

An MCP server that automates PCB design work in KiCad, in two tiers of autonomy depending on how complex the board is.

## Language

**Board class**:
A band of board complexity (net count, pad count, presence of power zones and wide-current nets) for which the project makes a defined autonomy promise.
_Avoid_: difficulty level, board size

**Acceptance gate**:
The single machine-checked verdict (PASS, FAIL or INCOMPLETE, plus the complete unconnected-item and violation list) that a board must pass before it may be called routed or done: zero unconnected items, zero error-level DRC violations, all zones filled and verified, figures taken from `kicad-cli` output.
_Avoid_: "routed" or "done" claimed from a sample, a category count, or a convenience property

**Acceptance ladder**:
The ordered set of reference boards a pipeline must pass the acceptance gate on, in sequence: the USB-C ATtiny1616 breakout, then a mid-size signal-only 2-layer board (about 30 nets), then openMPPT. The rung where the pipeline first fails measures the autonomous ceiling.
_Avoid_: golden path (it names only the first rung)

**Router backend**:
A routing/placement engine the pipeline can hand a board to, behind a thin adapter. KiCadRoutingTools and Freerouting run as equals, with no fixed lead; the acceptance gate, not the backend, decides which result wins on each board.
_Avoid_: autorouter (too narrow: backends also place, fan out and verify); lead and cross-check (retired 2026-10-19 until a fair measurement ranks the backends)

**Autonomous tier**:
The board classes where the agent alone is promised to deliver a board that passes the acceptance gate.
_Avoid_: autopilot (as a promise; "autopilot" remains the headless adapter mode)

**Copilot tier**:
The board classes above the autonomous ceiling, where a human owns power routing and zone topology and the agent assists with schematic, placement, signal routing and verification.
_Avoid_: manual mode

**Autonomous ceiling**:
The most complex board class that has passed the acceptance gate without human routing; it is set by the first rung of the acceptance ladder that fails.
_Avoid_: complexity limit (as an estimate; the ceiling is only ever measured)

**Rung**:
One reference board on the acceptance ladder, standing for one board class.
_Avoid_: milestone, phase

**Adapter mode**:
How a tool reaches the design: live through a running KiCad instance, or headless through files and `kicad-cli`. It is independent of the tier. "Autopilot" may still name the headless mode.
_Avoid_: copilot mode (copilot names a tier)

**Checkpoint**:
A mutation split into a proposal, which changes nothing and returns a revision token, and a confirmation that applies exactly that proposal. The confirmation is refused if the project's files drifted in between.
_Avoid_: approval step, review gate (the acceptance gate is a different thing)

**Write layer**:
The project's own thin layer through which every change to a KiCad file is made. A library parses and serialises the file; the write layer owns what KiCad silently depends on (string escaping, number precision, pad angles) and the save itself. Decided 2026-10-19 and built as `kicad_mcp.write_layer` (build ticket 42); the move of the remaining tools onto it is nearly complete, and shrink-only ratchet lists name the rest.
_Avoid_: writer (KRT and `kicad_tools` each have their own), the high-level models (they are for reads and generators only)

**Self-check**:
A writing tool's own check that its write landed: it reloads what it wrote, compares it with the intended change, and fails with `write_not_verified` on a mismatch. A passed self-check means only "the write landed", never that a board is routed or done.
_Avoid_: verification or verdict (those are the acceptance gate's), `incomplete` (the gate's word for missing evidence)

**Verify before replace**:
The order of every write: the change goes to a temporary copy beside the original, the copy is self-checked, and only a copy that passed replaces the original. A failed check leaves the original untouched and no copy behind.
_Avoid_: backup-then-write (it leaves stray copies that can be read as live files)

**Ratchet**:
A test that holds a count of known offenders that may only go down: a list that may shrink but never grow, or a plain ban where the count is already zero. Fixing an offender means removing it from the list.
_Avoid_: lint rule (a ratchet tolerates the offenders it lists)

**Drift**:
A difference between a project's files on disk and the state the agent last saw, usually a human's edit in KiCad between tool calls. It is measured against the last-known composite hash: one hash over the project's `.kicad_pcb`, `.kicad_pro`, `.kicad_dru` and `.kicad_sch` files, changed only by a self-checked write or an explicit reload. A writing tool is to refuse on drift (`drift_detected`) and ask for a reload; there is no override. Today only the checkpoint confirms enforce it; the general guard is build ticket 40.
_Avoid_: conflict, stale cache

**Oracle**:
What a test compares a result against. For file-format and geometry tests it is a file KiCad itself wrote, with reference values from `kicad-cli`, never a file built by the library under test.
_Avoid_: fixture (any test input; it is an oracle only if KiCad wrote it), golden file (the "golden board" is a pipeline fixture, not an oracle)

## Words for reading the repository's records

**Ticket** (also **build ticket N**):
One unit of work with a written acceptance list. A build ticket changes code, tests or docs and is numbered (for example "build ticket 42" is the write layer); other kinds are research and planning tickets, which produce findings. Code, tests and docs cite tickets by number as a plain reference; the records themselves are maintained outside the public repository, so a ticket id may not be resolvable in a public copy; the surrounding text states the fact in words.
_Avoid_: issue (the word for a request in a hosted tracker), task

**Map** and **Wayfinder**:
A map is the plan for an effort too big for one session: its destination, the decisions so far and the list of tickets. Wayfinder is the planning method and the agent skill that charts maps and tickets.
_Avoid_: roadmap (that is docs/ROADMAP.md, the acceptance ladder)

**Decision N**:
A numbered entry in the "Decisions" section of docs/ARCHITECTURE.md, for example "decision 4" for the checkpoint mechanism and self-check.
_Avoid_: ADR, RFC

**Protocol N**:
The version of the frozen plan by which a ladder rung is measured (`src/kicad_mcp/autonomy_protocol.json`): attempts, levers, policies and what counts as official. Results are comparable only within one version. The current one is protocol 6.
_Avoid_: test plan, benchmark

**Official batch**:
A measurement of one rung on one backend under the current protocol from a clean committed tree, with the unchanged input. Anything else is unofficial and says so.
_Avoid_: run (any execution), benchmark

**KRT**:
KiCadRoutingTools, a third-party router and placer (MIT) that this project runs as a subprocess. See THIRD_PARTY.md.
_Avoid_: the router (there are two backends)

**INCOMPLETE**:
The acceptance gate's verdict when the evidence is missing: a `kicad-cli` failure, a missing or unannotated schematic, a rules deviation from the pinned baseline, a netlist guard mismatch. It never counts as a pass. `FAIL` takes precedence over it.
_Avoid_: partial pass, warning

**`PASS (necked)`**:
A label the rung runner gives an attempt since protocol 6 (build ticket 109). The board passed the gate, but it carries counted, forced necks: tracks narrower than their net class at a pad entry where the pad geometry leaves no room for the class width. It is a weaker statement than a plain `PASS` and is always labelled. Whether a neck counts as forced is the owner's open decision (build ticket 111): the first segment of each neck on rung 1 is 0.15 mm wide where the pad would allow 0.2 mm, and under a stricter reading the five Freerouting passes would all FAIL. `verify_board` itself returns only `PASS`, `FAIL` or `INCOMPLETE`.
_Avoid_: "PASS" for such a board without the label

**Pour step**:
The checkpointed pair `propose_pour_step` and `confirm_pour_step` that adds a plane zone (for example a GND pour) through a candidate board that the acceptance gate has graded. It is not a general zone-editing tool.
_Avoid_: zone tool

**Frozen**:
Not to be touched until its stated condition holds. The openMPPT board is frozen until rungs 1 and 2 pass. A protocol, a rung input and a manifest are frozen before a measurement so that the board cannot be changed to fit a result.
_Avoid_: archived, deprecated

**Batch gate**:
`scripts/batch_gate.py`: runs the full test suite and the linter on a clean committed tree and ends with one line, `BATCH GATE <sha>: GREEN` or `RED`. It is the project's test gate, because there is no hosted CI. Do not confuse it with the acceptance gate, which judges a board.
_Avoid_: CI, acceptance gate

**Known red**:
A test that is allowed to fail because it is listed by id in `scripts/known-red.txt`. Adding one needs its own ticket and a review; every other failure blocks a merge. Allowed skips are listed in `scripts/known-skips.toml`.
_Avoid_: flaky test, xfail

**Owner**:
The person who decides the scope and the rules of this project. "The owner's decision" in the docs means a scope or rule call that only that person makes; where one is still open, the text says so.
_Avoid_: user, customer
