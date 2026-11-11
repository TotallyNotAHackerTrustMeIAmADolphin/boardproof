# Research 70: why the suite takes 47 minutes, and what can be sped up or run in parallel

Question: issue 70. Date: 2026-11-04. Method: per-test timing file of one gate run on `68fa741`
(START/END lines, 4770 tests), the test and source code, three single-test measurements, KiCad
10.0.7 `kicad-cli` timings on scratch copies, and the pytest-xdist documentation and source.
Evidence tags: MEASURED (run here), CODE (read in this repo), VERIFIED (fetched and quoted),
ASSUMED. No repo file other than this one was touched; no suite was run.

## Answer in short

1. The 47 minutes are serial run time, with no idle gaps: wall time from first START to last END is
   2812 s and the summed test time is 2809 s (MEASURED from the timing file). The run was disturbed
   by memory pressure, so absolute numbers are inflated; the shares are more reliable than the
   seconds.
2. The cost is concentrated: the slowest 100 tests (2 percent) take 1245 s (44 percent), the slowest
   500 take 77 percent. 4136 tests under one second sum to 494 s (18 percent).
3. Two different things dominate. (a) The pure-Python S-expression parse of large KiCad schematic
   trees, repeated for the same files many times inside one tool call (not `kicad-cli`). (b) Real
   `kicad-cli` runs repeated on unchanged boards.
4. The suite can run in parallel in principle: no fixed ports, no fixed temp paths, no shared
   writable state was found (grep, not a proof). Perfect balance by file gives 1404 s on 2 workers
   and 702 s on 4 (lower bounds from the timing file, which already includes contention). The gate
   cannot take that as is: it needs a new dependency (owner decision) and changes to its output
   parsing.

## 1. What takes the time

Per-file totals (MEASURED, timing file, `python -I`): test_pour_step 297 s (79 tests),
test_bom_guard_unit_takeover 262 (106), test_verify_board_netlist_guard 179 (30),
test_sheet_tree_readers 129 (77), test_raw_geometry 116 (84), test_import_duplicate_scan 107 (22),
test_gate_field 106 (46), test_rung2_batch 105 (98), test_board_rules 98 (65),
test_autonomy_run_review 83 (22). Ten of 127 files = 1486 s = 53 percent.

Unit costs MEASURED here, `kicad-cli` 10.0.7, scratch copies, machine loaded by a running gate:

| Command | Board | Time |
|---|---|---|
| `kicad-cli version` (process start) | n/a | 0.35 s |
| `pcb drc --format json --severity-all --refill-zones` | simple_led fixture | 2.24 s (twice: 2.243, 2.249) |
| `sch export netlist --format kicadsexpr` | vme-wren (36 sheet files, 1507 parts) | 3.3 and 3.4 s |
| `sch upgrade --force` of one sheet | vme-wren | 0.55 to 0.57 s |
| `pcb export pos` | vme-wren | 17.6 s |
| `pcb drc ... --refill-zones` | vme-wren | 207 s (3 min 27 s, loaded machine) |

Counting hooks (a pytest plugin outside the repo that wraps `subprocess.run`, one pytest process at
a time) gave these per-test facts.

**test_import_duplicate_scan::test_units_of_one_part_on_several_sheets_match_the_netlist (46.4 s in
the file, 49.6 s alone).** MEASURED: exactly one subprocess call, `kicad-cli sch export netlist`,
3.6 s. The other 46 s are Python. A cProfile run (85.6 s with profiling overhead) shows:
`kicad_tools/sexp/parser.py:1758 parse_file` called 296 times, cumulative 61.2 s (71 percent);
`sheet_tree._parse` 183 calls, 23.5 s; `schematic_import._parse` 41 calls, 23.2 s;
`verify_board._annotation_problems` 21.7 s; `_stale_instance_problems` 16.7 s;
`sheet_tree.placed_instances` 2 calls, 15.6 s; `sheet_tree.walk` 3 calls. About 36 sheet files are
parsed about 8 times each. CODE: nothing caches the parse. `sheet_tree._parse` (`sheet_tree.py` line
96) and `schematic_import._parse` (line 89) call `parse_file` directly; `walk` keeps a per-call dict
(`parsed_files`, `sheet_tree.py` line 120), so each walk parses once, but a tool call makes several
walks plus its own parses. The only `lru_cache` in `src/boardproof` is `board_rules.py` line 287.

**test_verify_board_netlist_guard::test_a_healthy_demo_still_passes[vme-wren] (64.3 s).** CODE: the
DRC is replaced by `_FakeDrc` (`_clean_gate`, lines 44 to 66), so no DRC runs; the guard's real
work is one netlist export (3.4 s MEASURED) plus `kicad-cli sch upgrade` of every sub-sheet
(`verify_board._sheet_load_problems`, `verify_board.py` lines 616 to 640; 35 sheets x 0.56 s is
about 20 s MEASURED unit cost) plus the same repeated parses as above. About 24 s is `kicad-cli`; the
rest is Python parse and the demo copy (ASSUMED split, not profiled). The sibling
`..._with_one_dnp_unit_still_passes[vme-wren...]` (36.8 s) and `test_every_sub_sheet_is_load_checked_down_the_tree`
(32.6 s) run the same guard on a fresh copy of the same board.

**test_bom_guard_unit_takeover (262 s, 106 tests, 51 of them over 2 s).** MEASURED on one test
(`..._refused[exact-padded-name]`, 8.1 s alone, 4.1 s in subprocesses): 7 `kicad-cli` calls:
`pcb drc json` 1.9 s, 2 x `sch export netlist` 0.8 s, `pcb export gerbers` 0.4 s, `pcb export drill`
0.4 s, `sch upgrade` 0.4 s, `version` 0.3 s. Each test calls `export_manufacturing_package` on a
fresh copy of `sonde xilinx` (CODE: `kicad_demo("sonde xilinx")` 12 times in the file, then
`manufacturing.export_manufacturing_package`) and changes only the schematic. The board is
byte-identical in every test, so its DRC, gerber and drill export repeat with identical inputs
(about 2.7 s of 4.1 s). The rest of the per-test time is Python (import, parse).

**test_pour_step (297 s).** CODE: `real_refill_confirm` (line 954) uses a fake router but "the REAL
kicad-cli refill and connectivity"; `pour_step.MAX_CONNECTIVITY_RUNS = 40` (`pour_step.py` line 99)
and the connectivity loop runs `kicad-cli` once per group until the cap (`pour_step.py` line 1167).
`test_ten_isolated_pads_are_all_named_within_the_run_cap` (57.5 s) asserts `runs <= MAX_CONNECTIVITY_RUNS`
on ten isolated pads; with one 2.24 s DRC per run that is the order of the cost (INFERRED; the run
count was not measured because the test is at the 60 s limit). 18 of 79 tests are over 2 s; the top
five are 142 s. The time is inherent to "real refill" tests, not repetition.

**test_rung1_layout (56 s, 6 tests).** CODE: `flow_result` is a module-scoped fixture that runs the
whole layout flow once (`asyncio.run(mod.flow())`, lines 31 to 46). The first test
(`test_source_project_is_never_touched`, 55.7 s) is charged the fixture; the other five are cheap.
So the 56 s are one real flow, not 56 s of one assertion.

**test_raw_geometry (116 s).** CODE: the `demo_board` fixture is session-scoped and caches one
`BoardOracle` per board (lines 30 to 52), so each export is already done once per session. The
three vme-wren tests (33.2, 32.6, 19.1 s) each pay one oracle export (`pcb export pos` is 17.6 s
MEASURED on vme-wren, `ipcd356`, a pcbnew run); this is already a shared-once design.

**test_gate_field (106 s), test_board_rules (98 s).** CODE: tests call the real `pcb_run_drc` or
`run_drc` on private copies of simple_led (2.24 s MEASURED per DRC). `test_real_drc_*` in
test_board_rules take 15.9, 14.9 and 13.6 s (several DRC runs per test; counts not measured).
Fixture board is the same bytes in each test, but the rules each test writes differ, so DRC inputs
differ.

**test_rung2_batch (105 s), test_sheet_tree_readers (129 s), test_autonomy_run_review (83 s).**
Names and code suggest AST scans of `evals/` per mutant (6.5 s tests) in test_rung2_batch,
`export_manufacturing_package` and netlist oracle on `complex_hierarchy` in test_sheet_tree_readers
(21.6 s for the cm5_minima board), and a full measure run on a pinned fixture per test in
test_autonomy_run_review (median 3.5 s). Not profiled (ASSUMED).

Other: `vme-wren`-named tests sum 187 s (10 tests); the four sheet-tree files (bom_guard,
netlist_guard, sheet_tree_readers, duplicate_scan) sum 678 s, 24 percent of the suite.

## 2. Work that repeats with identical inputs, and what a cache could hide

| Repetition | Evidence | Key | What it could hide |
|---|---|---|---|
| Parse of the same sheet file inside one tool call | 296 parses of ~36 files in one call (MEASURED) | within one call, the resolved path: no cross-call state | A parse that must see a mid-call edit (the write layer mutates then re-reads). Scope the memo to a read-only pass or key on (path, mtime_ns, size, sha256). A shared parse tree must not be mutated by a caller. |
| DRC, gerber and drill export of the unchanged `sonde xilinx` board in each bom_guard test | 7 `kicad-cli` calls per test, ~2.7 s repeatable (MEASURED) | sha256 of the `.kicad_pcb` and `.kicad_pro` and `.kicad_dru`, full argv, `kicad-cli version` output, output file list replayed | A change in how the product invokes `kicad-cli` is caught only if the argv is in the key. Non-determinism: the gate runs DRC three times and takes the union because runs can differ (`verify_board.GATE_RUNS = 3`); a cache would return one answer and defeat the tests of that instability. Replay has to restore output files exactly. |
| `kicad_demo(...)` copies of the same demo | 33 tests with the fixture, sum ~1 s (MEASURED from the timing file) | n/a | Not worth caching. |
| `sch upgrade --force` of each sheet, plus netlist export, for each healthy-demo test | 3 tests on vme-wren, 20 s + 3.4 s each (MEASURED unit cost) | sha256 of every sheet file, version | The guard tests exist to prove a real `kicad-cli` verdict on real files; a cache keyed on sha is sound but the three tests then prove less (they would share the one verdict). Not recommended. |
| Test-level monkeypatch of `parse_file` returning a shared tree | not built | sha256 of the bytes | It would hide a regression in the parser or in the production code's parse use, and shared trees get mutated by write tests. Rejected. |

The only cache that is a clean win and that helps users (the tool takes 80 s on a large project) is
the first row, in production code. A `kicad-cli` result cache in `tests/conftest.py` is possible
but gives about 2.7 s x 106 = 290 s at most for the bom_guard file, with the replay and
non-determinism risks above.

## 3. Parallel runs

pytest-xdist is not installed (`pip list`: pytest 9.2.0, pytest-asyncio 1.4.1, no xdist) and is not
in `pyproject.toml` `[project.optional-dependencies] dev` (`pytest>=8.0`, `pytest-asyncio>=0.24`,
`ruff==0.17.1`). A new dependency is an owner decision.
Licence: MIT (copyright held by the project and its contributors); no clause limits automated or
commercial use (VERIFIED, fetched from the project's LICENSE file on GitHub).

xdist facts (VERIFIED from the xdist docs and `dsession.py`): `--dist load` sends tests to any free
worker in no guaranteed order; `--dist loadfile` "Tests are grouped by their containing file. Groups
are distributed to available workers as whole units"; `loadscope` groups by module or class. "each
worker process will perform its own collection and execute a subset of all tests", so a
session-scoped fixture can run once per worker, and xdist documents a `FileLock` pattern for once-only
work. The terminal header for xdist is built as `f"{total_workers} {workers_noun} [{tests_collected}
{tests_noun}]"` and a crash prints `worker 'gw0' crashed while running '<id>'` and replaces the worker.

**Shared state found (CODE, grep over `tests/` and `src/boardproof/`; not exhaustive):**
- Per-test private paths: fixtures use `tmp_path`, `tmp_path_factory`, `kicad_demo` copies, and an
  autouse fixture sets `BOARDPROOF_BACKUP_DIR` to a tmp dir (`tests/conftest.py` lines 73 to 77).
  `tmp_path_factory` gives each xdist worker its own base temp directory (ASSUMED; standard pytest
  behaviour, not re-verified).
- The KiCad install is shared but read-only; `demo_install_guard` hashes the whole demos tree at the
  first `kicad_demos_dir` setup and checks it at teardown (`conftest.py` lines 80 to 105). Under
  xdist this runs once per worker (it is session-scoped); the cost is the hash read per worker and it
  is safe, since every worker only reads.
- The KRT checkout is only read (`krt_checkout`, `needs_krt` marker, 70 uses); its availability probe
  is cached per process (`tests/_krt.py` header).
- `os.chdir` changes: `monkeypatch.chdir` in `test_freerouting_sandbox.py` (lines 96, 150, 295),
  undone by the monkeypatch; per process, so no cross-worker effect.
- No fixed ports. IPC tests use an unreachable socket for non-live tests (`test_copilot_toolset.py`
  line 55); the live ones skip without KiCad. The `DEFAULT_RUNS_ROOT` tests redirect it to `tmp_path`
  with `monkeypatch`.
- Module-level state: `board_rules.py` `lru_cache` is per process. Session fixtures in
  `test_raw_geometry.py` (`demo_board`) are per worker; with `loadfile` the whole file lands on one
  worker, so the share-once design survives.
- Order dependence: none found by grep; not proven. A `-p no:randomly`-style order check was not run.
- Real writes into the repo tree: none found in the test sources; the gate's own tree recheck
  (HEAD and `git status`) would catch one.

**What the gate contract needs (CODE, `scripts/batch_gate.py`):**
- It requires a `collected N items` line (`_COLLECTED`, line 169; `collected_line`; RED "no collected
  line", line 516) and compares deselected and `ran` counts to it (lines 511 to 524). With xdist the
  header is `N workers [M items]` (VERIFIED above), so the gate must also read that form. Skip
  accounting (`-rs`, `known-skips.toml`) and the id-based known-red match (`known-red.txt` is empty
  today: 0 entries) work from the final short summary, which xdist keeps (ASSUMED; check on a trial
  run).
- Timeout and expected duration: `SUITE_MEASURED_S = 2545`, `SUITE_TIMEOUT_S = 7650` (lines 105 to
  106) and the printed worst case would need to be re-based; a parallel run is safe on the wall
  clock because the gate enforces it on the wall clock.
- The per-test START/END timing file is written by a wrapper not in the repo; with workers it must
  write one file per worker or lock (not in the gate; ASSUMED).
- The expected-config check `EXPECTED_PYTEST_CONFIG` forbids `addopts` (lines 147 to 160), so
  `-n`/`--dist` must go on the gate's command line (line 490), not in `pyproject.toml`.
- Reruns of new failures (lines 576 to 590) run a single id alone, so they stay serial.

**Achievable wall time from the timing file (MEASURED, greedy longest-first packing of whole files):**

| Workers | Wall time lower bound | Longest file | Note |
|---|---|---|---|
| 1 | 2809 s | 297 s | today |
| 2 | 1404 s (23 min) | 297 s | balanced exactly |
| 4 | 702 s (12 min) | 297 s | balanced exactly |
| 8 | 351 s | 297 s | first limited by the longest file (test_pour_step) |

These are sums of times measured under contention. With real parallel `kicad-cli` and a Python parse
on each worker, CPU and memory contention will cost something (not measured; the machine is short on
memory and `kicad-cli` DRC on vme-wren took 207 s under load). Memory per worker (a pytest process
plus a `kicad-cli` and a router JVM or KRT) was not measured: ASSUMED about 0.5 to 1.5 GB per worker,
so 4 workers fit in 32 GB when no other gate runs, but not next to several parallel gates.
`--dist loadfile` is needed because module and session fixtures (`flow_result`, `demo_board`) would
otherwise be rebuilt on each worker that gets one of their tests.

Without a new dependency: split the gate into N `pytest` subprocesses by file list (stdlib only). It
duplicates what xdist does and the gate would have to merge N outputs (collected counts, skip lines,
failure ids, deselected count, rootdir) and check that the shards partition the collected files
exactly. More custom gate code than adding xdist.

## 4. Ranked list

| # | Change | Expected saving (from the timing data) | Risk | Effort | Owner decision? |
|---|---|---|---|---|---|
| 1 | Parse each sheet once per tool call in production code (`sheet_tree`, `schematic_import`, `verify_board`: a per-call memo keyed on resolved path plus mtime_ns, size) | MEASURED on one test: parse is 71 percent of 85.6 s profiled; a once-per-file parse removes about 7 of 8 parses, about 30 of 50 s on that test. The four sheet-tree files sum 678 s; ASSUMED 40 to 50 percent of it is parse, so about 250 to 350 s (not measured per file). Also makes the product faster on real projects. | Low to medium: a shared SExp tree must not be mutated by a caller; the write layer re-reads after writing. Needs a test with a mid-call edit. Product code, needs adversarial review and the full suite. | Medium | No (a build ticket; not weakening any test) |
| 2 | pytest-xdist with `--dist loadfile` on 2 workers, then 4, in the gate | 1405 s saved at 2 workers, 2107 s at 4 (lower-bound wall 1404 and 702 s) | Medium: contention, memory, worker crashes, gate parsing of the `workers [N items]` header, timing file per worker. Needs a trial run to look for order dependence. | Medium to high (gate change, a critical-class file) | Yes: a new dev dependency (MIT), and a change of the gate |
| 3 | Order the whole files longest-first and start the 56 s `flow_result` and the 297 s pour file first | 0 s serially; keeps the 4-worker bound reachable | None | Low | No (only meaningful with #2) |
| 4 | `kicad-cli` result replay for bom_guard's unchanged board (sha, argv, version) in `tests/` only | at most 106 x 2.7 s = 290 s, ASSUMED 150 to 250 s after Python overhead | Medium: hides non-determinism and argv drift unless both are in the key; replay of output files | High | Yes (it weakens the "real tool each time" rule for 106 tests) |
| 5 | Replace the three `vme-wren` healthy-demo variants by shares of one gate run (sha-keyed) | up to 70 s of the 187 s | Weakens: the three tests probe different guard branches on the same big board | Low | Yes (coverage trade) |
| 6 | Cut `MAX_CONNECTIVITY_RUNS` in the ten-pads test by monkeypatch (keep one real cap test) | up to ~40 s of 57 s | It tests the real cap; shrinking it changes what is proven | Low | Yes (weakens a test) |

## Recommended first three steps

1. **Memoize the sheet parse per tool call** (#1). Highest saving per risk, no owner decision, helps
   users too, and shrinks the load on a parallel run. Start by writing the failing test (a mid-call
   edit must still be seen), then the memo, then re-measure the same single test:
   `test_units_of_one_part_on_several_sheets_match_the_netlist` (49.6 s now) as the benchmark.
2. **Ask the owner for the xdist decision** (#2) and, if yes, trial `-n 2 --dist loadfile` on a
   quiet machine once the gate that is running has finished: record collected line, skip lines,
   exit code and the lock behaviour of the timing file. Make the gate accept the
   `N workers [M items]` header only after a trial shows the exact text.
3. **Run the heavy files first** (#3) once #2 exists, and record the real two-worker and
   four-worker wall times in this file, replacing the lower bounds above.

## Open points and what needs an owner decision

- Owner: add `pytest-xdist` (MIT) as a dev dependency and let the gate run in parallel (also how
  many workers when several builders' gates run at once; memory was not measured).
- Owner: whether a test-level replay cache of `kicad-cli` output is acceptable at all (#4), and
  whether the three `vme-wren` tests (#5) may share a verdict.
- Not measured: the per-file split between parse and `kicad-cli` outside the two profiled tests;
  the run count of the ten-pads test; real parallel scaling and memory per worker; the actual
  `workers [N items]` header on this setup. The timing numbers come from a run disturbed by memory
  pressure, so seconds are inflated and the speed-ups are lower bounds relative to that run.
- The grep for shared state is not a proof of independence; only a trial parallel run shows
  order or collision problems.

## Corrections and trial results (2026-11-05)

**Correction to section 1 and ranked item 1 (parse each sheet once per call).** The 296 parse_file calls were measured before issue 71 added the explicit per-call memo; on main the benchmark test makes 41 calls for 39 distinct files. cProfile roughly doubles the cost of the pure-Python S-expression parser, so the 71 percent share of sheet parsing was inflated. Unprofiled, the 41 parses cost about 20 s, of which 12.7 s is ONE parse of the 71 MB board file of the vme-wren fixture (`schematic_import._plan`), about 3 s are the 36 sheets, and the netlist import file (10 MB) is parsed twice at 1.4 to 1.8 s. A shared per-call parse scope (built as ticket 149, sha256 of the bytes as key) removes the remaining duplicate sheet parses of the gate (73 to 37 parse calls in the healthy-demo test, 8.7 s to 4.5 s) but cannot reach the "under 20 s" goal: it was parked, the real hotspot is the parse of very large board files.

**Trial of pytest-xdist (ranked item 2), measured, kicad-cli 10.0.7, 16 cores, other agents running at the same time.** Group of 11 files, 460 tests: serial 606 s; `-n 4 --dist loadfile` 399 s; `-n 8 --dist loadfile` 452 s (no gain); `-n 4 --dist load` 295 s (2.05x). Six slow files, 241 tests: serial 1264 s, `-n 4 --dist loadfile` 470 s (2.7x). One parallel-only failure (a warning with a lone surrogate aborted the session; fixed in ticket 152). A `-n 4` run needs about 4 to 7 GB; CPU was 73 to 89 percent in every run (shared with the other agents), disk 4 to 10 percent busy. Hypothesis rejected: antivirus or sync slowing the runs. Stage 1 (dev dependency and documentation) is ticket 152; stage 2 (the batch gate under `-n 4`: command line, re-based timeouts, per-worker timing file) is a separate ticket.
