# Testing

There are two separate things here. The **acceptance gate** decides whether a board is done. The **test suite** decides whether the code works. A green test suite has never meant a finished board.

Terms such as ticket, protocol and known red are explained in docs/CONTEXT.md. Where a fact comes from a maintainers' ticket, this file states the fact and cites the ticket as a plain reference.

## Acceptance gate (`verify_board`)

The only way to call a board routed or done. Contract (decided 2026-10-11):

- **Output:** one verdict, `PASS` or `FAIL`, plus the **complete** per-net list of unconnected items and the **complete** list of violations. No sampling, no truncation, and no category counts in place of items. If the list does not fit, page it; never summarise it.
- **PASS requires all of:**
  - zero unconnected items on every net;
  - zero error-level DRC violations;
  - every zone filled, and verified as filled against the current copper.
- **Source of truth:** every figure comes from `kicad-cli` output (`pcb drc --format json --severity-all`), run against the board's **original** project rules. These are never evidence: `kicad_tools` convenience properties or its ratsnest, a router's self-reported score, a tool's own `status: ok`, or a project file a router rewrote.
- **No partial pass.** If any check could not run, the verdict is `FAIL` (or `INCOMPLETE`, which never counts as a pass).
- **Settled details** (docs/ARCHITECTURE.md decision 14 holds the full contract; built in `src/kicad_mcp/verify_board.py`):
  - it is always loaded, not part of a lazy toolset;
  - every run refills zones itself (`--refill-zones` on a fresh copy);
  - schematic parity is required unless declared not applicable with a reason, and a missing or unannotated schematic is `INCOMPLETE`;
  - run-to-run non-determinism is handled by three runs with union semantics: an error in any run is `FAIL`, and differing runs are reported as unstable;
  - error level means severity `error` under the project's own severities, pinned by a rules baseline; any deviation from the baseline is `INCOMPLETE`;
  - there are no waivers.
- **The netlist count guard** (build ticket 50) sits beside the annotation check, on the graded copy:
  - Why: KiCad leaves a sub-sheet it cannot read out of the design and `kicad-cli` still exits 0, so parity would compare the board with a truncated netlist (`complex_hierarchy`: 10 of 68 parts, and the gate passed).
  - What it does: it walks the sheet tree from the root and counts one component per reference over every sheet use (a sheet used N times counts N times, a multi-unit part once; power symbols, DNP symbols and symbols off the board are not counted). It compares that count and the references with `kicad-cli sch export netlist`, and loads every sub-sheet on its own with `kicad-cli sch upgrade`.
  - Effect: a mismatch, a sheet that does not load, or a guard that cannot run lowers a `PASS` to `INCOMPLETE` with the sheets and counts named. A `FAIL` stays `FAIL`, and the reason is still reported.

Why the gate is this strict:
- On 2026-09-14 openMPPT was declared "61/61 connected, unconnected = zone nets only", and both claims were false. Every check that day, including an independent re-check, read counts and samples, never the full list.
- KRT's `fab` policy makes DRC report a board as clean by rewriting the rules that DRC grades against.
- On the golden board, `pcb_run_drc` reports 0 violations while the board is completely unrouted (11 unconnected).

## Acceptance ladder

Progress is measured only by climbing this ladder: a rung is passed when its board gets a `verify_board` PASS. The first rung that fails defines the **autonomous ceiling**; boards above it get the Copilot tier promise. Status as of 2026-10-29, with the details under each rung below:

| Rung | Board | Status | What passing proves |
|---|---|---|---|
| 1 | USB-C ATtiny1616 breakout, 2-layer, v2 rules | **Not passed.** Official: protocol 5 KRT 0 of 5, Freerouting 0 of 5; protocol 6 KRT 0 of 5, Freerouting 5 of 5 `PASS (necked)`. No rung is claimed from a necked pass. The 5 of 5 depends on the owner's open decision on a neck below the pad limit (build ticket 111, item 3): the first segment of each neck is 0.15 mm wide where the pad would allow 0.2 mm, and under a stricter reading all five attempts would FAIL. | The whole pipeline (sourcing to schematic to placement to routing to gate to manufacturing export) works on a trivial board without a human. |
| 2 | Mid-size signal-only 2-layer board, about 30 nets | **Not passed, and cannot pass for any router** until the input's 6 `silk_overlap` errors are dealt with (build ticket 106). Official: protocol 5 KRT 0 of 5, Freerouting 0 of 5; no protocol-6 batch has run. | Autonomy holds in the band where PCBWorld shows open-source routers falling off (D3-B: KRT 0.20 Clean Pass, Freerouting 0.78). |
| 3 | openMPPT v1.3 (68 multi-pad nets, 488 pads, 7 zones, 20 A class) | **Frozen** until rungs 1 and 2 pass. | Expected to stay Copilot tier. A pass here would be a surprise and needs extra scrutiny. |

A rung result can be a "PASS (necked)" since protocol 6 (build ticket 109, the owner's decision of ticket 108): a weaker statement than a plain PASS, always labelled (below). The v1 rung-1 build (a fixture with a generic power header instead of USB-C and rules unroutable at the VQFN pitch) was replaced by the v2 board built through the tools (`evals/rung1/`, build tickets 9 and 10).

Board requirements:

**Rung 1: USB-C ATtiny1616 breakout, v2.** It must be built through the tool pipeline, not by hand. Requirements:
- **Real USB-C receptacle** (v1 swapped it for a generic power header).
- **Fab-realistic rules** from the JLCPCB profile: 0.127/0.127 mm track/clearance and a 0.45/0.20 mm via. Set them in both the netclass and the `.kicad_pro` constraints; KiCad leaves `min_clearance = 0.0` otherwise. v1's 0.2/0.2 rules cannot fit between the VQFN's 0.4 mm-pitch pads, and both routers proved it.
- **Power netclass** wider than signal.
- **Tight outline** that forces real packing.
- **Real mounting holes** with keepouts.
- **Silkscreen** at no less than JLCPCB's 1.0 mm text height and 0.15 mm stroke.
- **A real topology:** regulator, decoupling, LED and UPDI.
- **One SPICE-verified sub-circuit,** run on real ngspice.
- **GND pour:** it carries one on B.Cu, made by the pour step (build ticket 10).

**Rung 1's pinned input** (pinned in protocol 4, build ticket 51; unchanged in protocols 5 and 6; re-pinned in protocol 7 by publish-prep ticket 02a and in protocol 8 by build ticket 114, see below) is `evals/rung1/layout_run/07_poured/` as committed. Its board, USB-C footprint pads and 0.127 mm rules are **unchanged on purpose** (`paradigm-review-2` ticket 15): changing them so that a router passes would game the ladder. The pin only makes measurements official; it claims nothing about routability.

**Protocol 7 (publish-prep ticket 02a): rung 1's own USB-C asset.** The receptacle's symbol and footprint were an EasyEDA user-library export with no licence, which cannot be published. Rung 1 now uses this project's own pair, written by `evals/rung1/build_usbc_asset.py` through the write layer (no copied or transformed third-party file, no 3D model). The pad table of all 10 pads (number, position, size, shape, layers, drill) equals the old one within 0.001 mm and a test pins it; the outline, footprint positions, nets, GND pour and rules are unchanged, and the rebuilt unrouted stage gives the same `verify_board` result (FAIL, 26 unconnected items over the same 9 nets, 0 violations). The pins changed only because the hashed library, schematic and footprint-block content changed; the official slot of protocol 7 starts empty, and the official batches recorded on protocol 6 stay history. Where the manufacturer's drawing differs from the pad table (pad heights and shield-tab size), the table was kept (ticket 51) and the difference is the owner's decision: contact pads 1.20 mm high against 1.00 mm drawn, shield tabs 1.10 x 1.90 mm with a 0.6 x 1.3 mm slot against 1.00 x 1.80 mm and 0.6 x 1.4 mm.

**Protocol 8 (build ticket 114): rung 1's decoupling capacitor placed next to U1's +3V3 pin.** The owner's design observation of 2026-10-29, made before any official batch under protocols 7 and 8 and equal for both routers: C3 (U1's decoupling capacitor) sat 2.725 mm edge to edge from U1's +3V3 pin 4, and now sits at board position (149.293, 108.239) with rotation 180 (was (153.0, 111.2) with rotation 0), 0.632 mm from the pin's pad edge, with its +3V3 pad facing U1. The position is derived by `plan_decoupling_capacitor` in `evals/rung1/build_rung1_layout.py` from raw pad and courtyard geometry (rule in the script and `tests/test_rung1_decoupling.py`), was fixed before any routing result of the new input existed and is not to be re-tuned after a measurement. No other footprint moved; the unrouted `verify_board` result is the same (FAIL, 26 unconnected items over the same 9 nets with the same pad lists, 0 violations). C1 and C2 (U2's capacitors) are 3.55 mm and 6.10 mm from U2's pads and were kept: owner decision pending. The official slot of protocol 8 starts empty; protocol 7 has no official batch.

**Official measurements on rung 1** (five attempts per backend; the run directories stay local in the git-ignored `runs/` folder; the sanitised history of every measured batch is committed as `evals/ladder_results.json`):
- **Protocol 5** (2026-10-28, build ticket 47): KRT 0 of 5, Freerouting 0 of 5.
- **Protocol 6** (2026-10-28): KRT 0 of 5. Freerouting 5 of 5 `PASS (necked)`, each attempt with 3 forced necks on `+3V3` at U1 pad 4, 2.1476 mm in total. Versions: KiCad 10.0.7, KRT 0.23.1, Freerouting 2.5.0 on Java 25.
- KRT's 0 of 5 under protocol 6 has a parser cause: it reads zero pads of footprints with unquoted pad names, so it leaves their pads unconnected (build ticket 112, not fixed).
- The owner's decision on a neck below the pad limit is still open (build ticket 111, item 3); no rung is claimed from a necked pass.

**Earlier, unofficial findings** on the same input (before protocol 5):
- **KRT** left the USB-C pads unconnected in every attempt: `/CC1` (J1 A5 to R1.1), `/CC2` (J1 B5 to R2.1), VBUS (J1 B9 to J1 A9, F1.1 to J1 B9) and GND (J1 B12 and J1 A12 to J1 TH7), the same lists in all 5 attempts (`paradigm-review-2` ticket 04).
- **Freerouting through KiCad's own DSN** (one unofficial round trip, `paradigm-review-2` ticket 16) connected the USB-C pads, but the run still **FAILED**:
  - 4 unconnected items on `+3V3`, at U1 pad 4, C3 pad 1 and J2 pad 1 (J2 is the UPDI pin header, not the USB-C receptacle);
  - 2 GND `track_width` errors at 0.0952 mm (0.75 x 0.127 mm), both on F.Cu at U1 pad 3 (GND, a 0.4 mm-pitch VQFN pad at (151.55, 107.5)): (151.5139, 107.5361) to (151.55, 107.5), 0.051 mm long, and (150.877, 107.5361) to (151.5139, 107.5361), 0.637 mm long. They are the first two segments of the GND path from U1 pad 3, which continues at 0.127 mm through a via to C3 pad 2. Positions are from the raw `(segment ...)` nodes and match kicad-cli's DRC positions;
  - 2 `/VBUS_F` segments at 0.3 mm in the 0.4 mm Power class.
- So routability of the USB-C pads under the 0.127 mm rules was not established by those runs. Protocol 6 later showed it for Freerouting, with the necks described next.

**"PASS (necked)" (protocol 6, build ticket 109; the owner's decision of ticket 108, evidence in research ticket 107).** On rung 1, U1's pads are 0.8 x 0.2 mm at 0.4 mm pitch (gap 0.2 mm) and the Power class asks 0.4 mm track at 0.2 mm clearance: at U1 pin 4 a Power track can be at most `w <= s + 2(g - c) = 0.2 + 2(0.2 - 0.2) = 0.2` mm wide. KiCad's DRC does not flag a track narrower than its net class (net-class widths are defaults, not limits); only copper below `min_track_width` (0.127 mm) is a DRC error. The under-width policy therefore no longer fails a **forced neck**, and calls such a result **"PASS (necked)"**, never a plain PASS. A narrower-than-class track is a forced neck only if all four limits hold:
1. **The pad geometry forces it.** A pad of the same net and layer, entered by the track, leaves no room for the class width: the class width is above `s + 2(g - c)` for some neighbouring pad of another net beside it (`s` the pad width across the entry direction, `g` the gap to that neighbour, `c` the larger of the two nets' class clearances). Pad shapes come from the raw `(pad ...)` nodes (`(at)`, `(size)`, `(layers)`, `gr_poly` primitives of custom pads; a pad whose shape or footprint side is not read next to a contact pad stops the judgement).
2. **Its width is at or above the board minimum** (`min_track_width`).
3. **It lies within 2.0 mm of the pad edge**, measured along the net's straight tracks (full-width copper counts; the part inside the pad does not). The bound is `policies.necked_pad_entry.max_length_beyond_pad_mm` in the protocol file, derived from the Freerouting PASS board of ticket 107 (1.758 mm beyond the pad edge, measured again in build ticket 109).
4. **It is counted and labelled.** The attempt keeps status `PASS` with `status_label` "PASS (necked)", `necked_count` and `necked_length_mm`; the report carries `autonomous_pass_label` ("PASS (necked)" when every passing attempt is necked, "PASS" when one passed plain) and the rung result says so. `autonomous_pass` stays a boolean; read the label next to it. The best attempt prefers a plain PASS over a necked one.

Sub-minimum copper, unforced undercuts (the class width fits), necks beyond the bound, narrow vias and arcs, and open nets stay fails. Rung 1's board and rules are unchanged. The Freerouting plan of protocol 6 carries `--router.fanout.enabled=false` and `--router.neck_width_um=200` in every attempt (`fanout_enabled`, `neck_width_um` in the lever table, with the measured effect of each value in `lever_notes`): with the router's defaults it delivers the 0.0952 mm GND copper and leaves `+3V3` at U1 pin 4 open. A "PASS (necked)" is a weaker statement than a plain PASS.

The v1 fixture (`tests/fixtures/golden_attiny1616_board/`) stays as a regression fixture for the pipeline. **It is not a rung-1 candidate.**

**Rung 2: mid-size signal-only 2-layer board, about 30 nets.** No power-stage nets. Within PCBWorld's D3-B band (about 30-100 pads). **Chosen** (docs/ARCHITECTURE.md decision 13): KiCad's `demos/pic_programmer` (2 layers, 63 footprints, 34 nets with 2+ pads, one GND zone), with its tracks, vias and zone removed and its placement kept.
- **Where the input comes from.** Since protocol 4 (build ticket 51) the pinned source is the KiCad 10.0.x **release** demo, staged by `evals/rung2/stage_input.py`; protocol 3's pin came from KiCad master `6a26ce9` and no install reproduced it. Since protocol 5 (build ticket 67) the staging writes through the write layer: `write_layer.load` parses KiCad's file, the top-level `segment`, `arc`, `via` and `zone` nodes are removed and nothing else, and `write_layer.save` writes it with a read-back check. The staged board is KiCad's file minus those nodes (a test compares it with the installed demo), and its pins are the same under `kicad_tools` 0.23.0 and 0.24.0. Protocol 4 staged through `PCB.load` and `strip_traces` and wrote `kicad_tools`' serialiser text, which also carried a numeric net table KiCad 10's file does not have. `measure_input_floor.py` strips the same way.
- **Pins.** Rung pins hash file content with CRLF read as LF and nothing else normalised, so a Windows and a Linux copy of the same source give the same pin. The tests stage this input from the installed demo through the `kicad_demo` fixture (build ticket 34) and check both pins against it.
- **The scan of the eval scripts.** A ratchet in `tests/test_rung2_batch.py` scans the in-process Python of every script under `evals/` and fails closed:
  - every name used from a module outside a short safe stdlib list, `kicad_mcp` and the sibling scripts (so every `subprocess`, `os`, `shutil`, `kicad_tools`, `pcbnew` or other library call) must be a reviewed entry with its count;
  - dynamic imports and lookups (`__import__`, `importlib`, `getattr`, `eval`, star imports, `sys.modules`), imports in a class body or into a `global`/`nonlocal` name, and every `__`-prefixed or frame/traceback attribute are never allowed;
  - raw writes (reviewed JSON and CSV records only), model saves, copies and launches keep counted lists; launches are keyed by their argv with its names resolved and their `cwd=` value, and a launch keyword other than `capture_output`, `text`, `timeout` and `check` leaves it without a key.
  - The scan reads names and syntax: it catches accidents and plain idioms and is not a sandbox. Its blind spots are named in the test's header: among them, what a launched program writes (KRT's `beautify_labels.py`, launched by `measure_input_floor.py`, writes `beaut_out/pic_programmer.kicad_pcb` with KRT's own writer; reviewed: the output is measured, never pinned), the payload of a listed record write, deliberate obfuscation from objects that are not modules, values a caller passes into an argv, pathlib's `touch`/`unlink`/`chmod`, `rmtree` targets and `sys.path` changes before a sibling import.
  - One reviewed exception remains among the saves: `evals/rung1/build_rung1.py` writes the rung-1 schematic with `kicad_tools`' `Schematic.write`.
- **What it tests.** Its net count is inside the band; its 247 pads are above the band's pad range. Because it is a real open-source design with its placement kept, the rung tests routing only, and the tier claim says so.
- **Why it cannot pass.** The stripped and prepared input already carries 6 `silk_overlap` errors (`input_fail_floor`, build ticket 07), with 125 unconnected items over 34 nets before routing (re-measured on the protocol-5 staging, build ticket 67: the same complete lists as protocol 4's). No router can remove them. Official protocol-5 batches: KRT 0 of 5 and Freerouting 0 of 5; both connect all 34 nets and fail on those 6 errors plus under-width delivery. The ladder decision on this floor is build ticket 106.

**Rung 3: openMPPT.** Frozen (docs/ROADMAP.md).

**Recording results.** For each rung attempt, record: board commit, backend and flags, wall-clock time, and the full `verify_board` output. Record FAILs as well: the first failing rung *is* the measurement.

## Test suite

**When the full suite is required (owner decision 2026-10-29).** A change that only moves files, changes docs, or changes comments or docstrings gets a review plus targeted tests: the link test, the ratchets, the lint gate and every test file that names a changed path. The full suite is required for any change to behaviour, to tool text an LLM reads, or to pins or protocols, and once at each milestone (for example an export, which runs its own gate). Never run the full suite twice for one logical change. Edits to critical-class text (`CLAUDE.md`, `docs/CONTEXT.md`, `.claude/skills/**`, tool descriptions) are not "docs" for this purpose and need the full suite.

Around 4,490 tests as collected on 2026-10-29 (4,486 without the network-marked ones, 4,489 with them; `python -m pytest --collect-only -q -m "not network"` prints the current count). These layers test code, not boards:

1. **Unit tests per toolset module.** Mock the network boundary, never `kicad_tools`.
2. **Protocol tests** through the MCP SDK's in-memory `Client` (`tests/test_server.py`). This is the only layer that exercises lazy loading and `notify_tools_changed`. Every toolset needs at least one.
3. **Oracle and fixtures.** For file-format and geometry tests the oracle is a file KiCad itself wrote, with reference values produced by `kicad-cli` (netlist, position file, IPC-D-356, DRC JSON) in the same run, never stored.
   - KiCad's demos are read from the install (`KICAD_DEMOS_DIR` or next to `kicad-cli`). A missing demo skips in an ad-hoc local run with a stated reason and fails in the local gate run, which sets `KICAD_DEMOS_REQUIRED=1` (any value but empty or `0`).
   - Tests take them through the `kicad_demos_dir` and `kicad_demo` fixtures in `tests/conftest.py` (build ticket 34): `kicad_demo("pic_programmer")` returns a copy in `tmp_path`, and the install is only read. (Not run by this project: on Ubuntu, by KiCad's package layout, the demos come with `kicad-demos` (pulled in by `apt-get install kicad`) at `/usr/share/kicad/demos`, beside `/usr/bin/kicad-cli`).
   - KiCad's symbol libraries follow the same rule and flag (`KICAD_SYMBOL_DIR`, else `share/kicad/symbols` next to `kicad-cli`).
   - A session guard hashes the whole demos tree at `kicad_demos_dir`'s first setup, whether or not a test copies a demo, and fails teardown if any file in the install changed, appeared or vanished (build ticket 59).
   - Fixtures built through `kicad_tools`' object model (never hand-typed) are allowed only for cases no KiCad file covers and as inputs to behaviour tests, whose results are still checked against `kicad-cli`. Provenance is in `tests/fixtures/README.md`.
4. **Checkpoint tests:** a propose tool writes nothing until confirm. Tests assert that behaviour, not routing quality.
5. **Tests of mutating tools assert the reloaded file,** not the tool's return value (lesson from P3-11, an old defect id). The rule is that every self-check (`write_not_verified`, docs/ARCHITECTURE.md decision 4) also has a test that feeds it a corrupted write and shows that it fires; `FROZEN_WITHOUT_PROVEN_SELF_CHECK` in `tests/test_ratchets.py` lists the 14 writing tools that do not yet meet it (it may only shrink).
6. **Geometry edge cases:** 0°, 90°, 180° and 270°, front and back, against the courtyard module and the raw S-expression.
7. **Evals:** `evals/kicad_mcp_eval.xml` (`evals/README.md`).
8. **Ratchets:** `tests/test_ratchets.py` with its frozen lists in `tests/ratchets.toml` (build ticket 29): static `ast` scans over `src/kicad_mcp/`. A ban on pad attribute writes, and shrink-only lists of raw writes, commits (rename, replace, copy, `--save-board`, keyed on the exact call text), high-level saves, convenience-property reads on mutation paths, read-only tools that reach a write, and writing tools without a proven self-check. A new entry fails the test; a fixed entry must leave its list. Only top-level functions of `kicad_mcp.write_layer` (build ticket 42) are exempt from the save ratchet.

**Writing KiCad files in tests.** Building a test fixture through `kicad_tools`' object model under `tests/` is the one allowed use of the high-level models for writing (CLAUDE.md hard rule 1 and docs/ARCHITECTURE.md decision 1 otherwise keep them to reads and generators).

```
KICAD_DEMOS_REQUIRED=1 KICAD_ROUTING_TOOLS_REQUIRED=1 python -m pytest -m "not network"   # the gate's suite: a local run on this machine
python -m pytest -m network                                                              # live network, run deliberately
```

### The batch gate

- **The local run is the gate.** Hosted CI does not run: the owner will not pay for GitHub (2026-10-19), so the GitHub Actions workflow was disabled and removed on 2026-10-20 (its failed-run emails carried no signal). `python scripts/batch_gate.py` (build ticket 45) runs the full suite and the lint gate and gives one GREEN/RED verdict.
- **What it checks.** Skips against `scripts/known-skips.toml`, deselections against the network-marked count, collected test files against `git ls-files`, the pinned pytest and ruff configs, and whether the installed pre-push hook matches `scripts/git-hooks/pre-push` (it also reports whether the identity guard, `scripts/git-hooks/commit-msg`, matches its installed copy).
- **The standing publishability check.** The gate runs `scripts/check_publishable.py` over the tracked files only (exported from the index with `git checkout-index`, never the working tree; the old `.scratch/` tree is not part of the published tree), with `--forbidden-file` from the environment variable `BOARDPROOF_FORBIDDEN_FILE` and `--strict-archives` (publish-prep ticket 03a). It is RED when the variable is unset, empty or names no file, when the checker cannot run (an empty or invalid pattern file, a timeout, a failed export), on any hit beyond `PUBLISH_ALLOWED_HITS` in `scripts/batch_gate.py` (the three KiCad-written fixtures, pinned per file by hit count and by the reviewed git blob id, so a changed fixture is RED; publish-prep ticket 03 shrinks the list) and on any unopened archive beyond `PUBLISH_ALLOWED_ARCHIVES` (the 8 datasheet PDFs and the gerber zip). The log names `file:line: [pattern]` only, never the matched text. The owner's pattern file stays outside the repository; a wrapper that starts the gate must export the variable. `python scripts/batch_gate.py --publishable-only` runs just this check (about 10 s) and prints `PUBLISHABLE: OK` or `PUBLISHABLE: RED (...)`: run it before a push. The step has its own 90 s budget (`PUBLISH_BUDGET_S`), taken from the lint timeout so the worst case stays as printed.
- **The identity guard.** `scripts/git-hooks/commit-msg` refuses a commit whose author or committer e-mail is not the public alias's noreply address; there is no override. It covers `git commit` (also `--amend` and the commit after `merge --squash`) and merge commits; it does not see cherry-picks, rebases, `commit --no-verify`, `commit-tree` or fast-forward merges. Those are closed elsewhere: the git guardrail blocks `commit --no-verify` and `-n`, and the pre-push guard refuses a push that adds any commit whose author or committer e-mail is not the alias's noreply address. Install it like the pre-push guard: `cp scripts/git-hooks/commit-msg "$(git rev-parse --git-common-dir)/hooks/commit-msg"`. `tests/test_identity_hook.py` plants a wrong identity in a throw-away repository. `tests/test_gitignore.py` pins the `.gitignore` the same way (a planted file per pattern class, a mutant per pattern, and no tracked file ignored).
- **How long it takes.** It prints its expected duration (about 43 min) and its worst case (about 168 min, every step and git call at its timeout) at the start.
  - pytest alone took 460-870 s on 2026-10-20 with one gate at a time, and up to 2545 s with several builders' gates in parallel (`374c526` on 2026-10-21, a suite of about 2,850 tests then).
  - The 5331 s run `c1c8f1b` of that day is excluded from the timeout's basis: a reviewer's targeted tests ran at the same time and the machine was in standby for 3770 s of it. It exited 0 and ended GREEN under the 4200 s timeout then in force, because `Popen.wait(timeout)` does not count standby time on Windows; the gate now enforces the timeout on the wall clock.
  - The gate times the suite out at 7650 s (`SUITE_TIMEOUT_S`, about 3x the slowest measured run with only other gates running; `--timeout` overrides it and the printed worst case follows, and a timeout is RED). For the batch gate, the suite's timeout is about 3x the slowest measured run with only other gates known to be running and every other step has a fixed bound, so the worst case the gate prints at its start is its stop point: a waiting agent stops it then, not at 3x the expected duration (fork playbook P11). Builders wrap it in `timeout 10800`, above that worst case, so the gate's own RED fires first.
- **Do not touch the tree while it runs.** Run it in the background. At its end the gate rereads HEAD and `git status --porcelain --untracked-files=all`, and a change during the run (an edit, commit or checkout) is RED (build ticket 70). An edit undone before the end, an ignored path and an assume-unchanged or skip-worktree entry are invisible to that check.
- **Logs.** Each run's log files are named `<sha7>[-dirty]-<date>-<time>-<pid>[-<n>]` in the system temp directory's `kicad-mcp-batch-gate/`; a `--log-dir` inside the tree is RED. A log is no lasting evidence: copy any figure you cite from it (such as `374c526`'s duration) into the ticket.
  - Gates of every worktree share that directory, so each run starts by removing whole old runs from it (build ticket 79): a run older than 14 days, or beyond the newest 100 runs and older than a day (a run's age is its newest file's mtime; a gate run ends within its printed worst case).
  - It touches only file names the gate writes and never the current run's, and a run whose `.log` cannot be removed (on Windows: another gate still holds it open) keeps all its files; a file that cannot be removed is skipped and listed, and the cleanup never changes the verdict.
  - Any `.log` or `.out` file whose name starts with seven hex digits in the gate's naming pattern counts as the gate's own, so `--log-dir` must name a directory for gate logs only, never a general folder such as `%TEMP%`.
- **Start failures.** Every start failure prints exactly one `BATCH GATE <sha7|unknown>: RED (...)` line and exits 1: a git failure (`could not read the tree at the start: ...`), a script below the top of its git tree, no shared interpreter, a log directory or log file that cannot be created, and a `pyproject.toml`, known-red or known-skips file that exists but cannot be read (build ticket 88).
- **Use `python -m pytest`.** Since build ticket 33 the `pytest` console script collects too (`pythonpath = ["."]` in `pyproject.toml` puts the repo root on `sys.path` for the `from tests. ...` imports), but the documented commands stay on `python -m pytest`, which names the interpreter it runs.
- **In a worktree, test the worktree's own code.** The shared `.venv`'s editable install imports the main checkout's `src`. Run from the worktree root with the shared interpreter by absolute path, `<main checkout>/.venv/Scripts/python.exe`, and `PYTHONPATH=<worktree>/src`. The result counts only if `kicad_mcp.__file__` lies inside the worktree **and** pytest's `rootdir:` line names the worktree:

  ```
  cd <worktree>
  PYTHONPATH=<worktree>/src <main checkout>/.venv/Scripts/python.exe -c "import kicad_mcp; print(kicad_mcp.__file__)"
  KICAD_DEMOS_REQUIRED=1 KICAD_ROUTING_TOOLS_REQUIRED=1 PYTHONPATH=<worktree>/src <main checkout>/.venv/Scripts/python.exe -m pytest -m "not network"
  ```
- **Known reds** are only the entries of `scripts/known-red.txt`, matched by test id. Adding one needs its own ticket and an adversarial review. Any other failure blocks the merge or commit.

### Network and tool gating

- `@pytest.mark.network` tests are excluded by default. The datasheet live fetch needs `NETWORK_TESTS=1`. The LCSC live search is permanently skipped (bot wall).
- Tests that need Freerouting, KRT, KiKit or a running KiCad skip with a stated reason when the tool is absent. They never fake a result.
- **ngspice is optional while the SPICE simulation map is parked** (owner decision of 2026-10-30, issue 40; recorded on issue 32). The live simulation tests take the `ngspice_exe` fixture (`tests/_ngspice.py`), which finds `NGSPICE_PATH` (the pinned ngspice-47, `ngspice_con.exe` on Windows), then PATH. Present, they run. Absent, they skip with a stated reason that names the decision. The gate no longer sets `NGSPICE_REQUIRED` and is not RED for an unset or wrong `NGSPICE_PATH`; it prints which case it is in. The skip is not hidden: `scripts/known-skips.toml` allows exactly those skips (8 in `tests/test_simulation_toolset.py`, 1 in `tests/test_ngspice_required.py` for the version pin), each with a `max` and the ticket, so they are counted in the gate output, and a skip of any other reason is still RED. `NGSPICE_REQUIRED` (any value but empty or `0`) still turns the skip into a failure, for a machine that wants strict runs. `tests/test_ngspice_required.py` pins the version, fails any other test module that decides an ngspice skip itself, and checks that the allowed entries name the ticket and match the number of tests that take the fixture. When the simulation map resumes, restore the requirement: set `NGSPICE_REQUIRED=1` in the gate, remove the two entries, and bring back the unset-path check.
- **KRT** follows the `KICAD_DEMOS_REQUIRED` rule (build ticket 53):
  - A test that needs it carries the `needs_krt` marker, or takes the `krt_checkout` fixture when it only reads the checkout (both in `tests/conftest.py`, rule in `tests/_krt.py`).
  - When KRT is not usable (`check_availability()`: checkout from `KICAD_ROUTING_TOOLS_PATH` or the KiCad plugin directory, interpreter packages, the prebuilt `grid_router`), the test skips with the probe's reason in an ad-hoc run and fails in the local gate run, which sets `KICAD_ROUTING_TOOLS_REQUIRED=1` (any value but empty or `0`), so the gate cannot pass by skipping them.
  - An unknown marker is an error (`filterwarnings` in `pyproject.toml`), so a typo of `needs_krt` cannot drop the gating silently.
- **The KRT-skip ratchet** in `tests/test_krt_required.py` fails any other module in `tests/` that decides a KRT skip itself: a skip that names KRT, a probe at import time, or a probe of the real install in a function that does not control it.
  - Control means one of these: replacing the probe or a locator under it (`setattr`, `mock.patch`, `mock.patch.object`); setting `KICAD_ROUTING_TOOLS_PATH`, named by `setenv`'s first argument or `setitem(os.environ, <name>, ...)`; or deleting it and replacing `_PLUGIN_GLOBS` together. The locators stack as `availability` on `krt_availability` on `check_availability` on `find_kicad_routing_tools`, so replacing `krt_availability` does not control a call to `check_availability`.
  - Fixtures the test requests count when they are defined at module level in the same module or in `tests/conftest.py`. One imported from elsewhere, from a plugin or from a class is not resolved: that is a false positive, so move the control there.
  - The check is static, so these blind spots pass: a `setenv` to the real install, control under a condition, control in a helper that is never called, `monkeypatch.undo()` after the control, and a class-level fixture that shadows a module-level control fixture.
