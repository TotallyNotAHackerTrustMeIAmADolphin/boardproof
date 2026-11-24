# KiCadRoutingTools placement tools against the rung-1 decoupling rule (issue 27, part 1)

Research note. Question: where does KiCadRoutingTools' (KRT) placer put the decoupling capacitor C3 of the
rung-1 board, what else does it move, and how does that compare with the position chosen by the rung-1 rule
of build ticket 114 (`evals/rung1/build_rung1_layout.py`, `plan_decoupling_capacitor`)? Scope: reading, runs on
scratch copies, no code or board changed, nothing posted. Nothing here is a routed or done claim: no board in
this note has a `verify_board` PASS, and `kicad-cli pcb drc` reported 30 unconnected items on every board
(none is routed).

Tags: VERIFIED (run or fetched, output quoted), DOCUMENTED (publisher text read, not run), ASSUMED.

## Answer in four lines

1. `place_fanout_clearance.py` does nothing on this board: it only tidies capacitors near a BGA, and U1 is a
   VQFN (VERIFIED, below). It cannot place C3.
2. `place_simple.py` with C3 the only movable part puts C3 at (149.292, 107.900), rotation 180, on all 12 seeds
   and from both starting boards. The rule's position is (149.293, 108.239), rotation 180. They agree in x
   (0.001 mm), rotation and pad gap to U1 pin 4 (0.633 against 0.632 mm); they differ by 0.339 mm in y.
3. The y difference is the rule's clause 2: the rule keeps C3's +3V3 pad off the straight escape axis of
   pin 3 (GND). KRT centres the pad on pin 4's axis, so its pad top edge lies 0.075 mm above pin 3's centre
   line. By the stated rule the project's position is the better one; KRT's is a valid decoupling position
   that fails clause 2 (it was not asked to honour it). This is supporting evidence for clauses 1 and 3
   (placement next to pin 4, straight track), and a measured difference on clause 2.
4. Used as the skill uses it (every part but the connector and holes movable), `place_simple` moves all 12
   other parts by 1.3 to 21.8 mm and rotates most of them, so it cannot be the producer of a router-neutral
   input. Its C3 result there is not stable (three relative poses over 12 seeds).

Confidence: high on the numbers (raw S-expression, byte-identical reruns, independent reproduction of the
project's own 0.632 and 2.725 mm); medium on the comparison's meaning (one board, one IC, one decoupling pin).

## 1. Sources, version, licence

- VERIFIED. Fresh shallow clone of upstream main into an empty scratch directory:
  `git clone --depth 1 https://github.com/drandyhaas/KiCadRoutingTools.git up`; `git rev-parse HEAD` printed
  `61565f35053aeeb3a005fafc9f137f9c026e04df` (commit date 2026-11-22), `VERSION` 0.24.2. The local copy under
  `.tools/` (0.23.8, commit `c64c0db2`) was not used (it is older; read-only anyway).
- VERIFIED. The Rust extension was installed with the documented `python build_router.py` (downloads the
  release binary): "Installed prebuilt binary from release v0.24.2" (module version string `v0.24.1`). The
  placers import it (`import grid_router` in `placement/quench.py`); without it `check_floorplan --emit-intent`
  fails with `ModuleNotFoundError: No module named 'grid_router'`. It is a binary from the publisher's release
  page; a GPL-style arm's-length rule is not needed (see licence), but the binary was not audited (ASSUMED safe).
- VERIFIED. Licence, quoted from `LICENSE` and `README.md`: "MIT License ... Permission is hereby granted, free
  of charge, to any person obtaining a copy of this software ... to deal in the Software without restriction,
  including without limitation the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or
  sell". README "Support": "free and MIT-licensed, and it stays that way - there is no paid tier, no feature held
  back". No clause limits automated or commercial use. (The MIT licence does require the copyright notice in
  copies; a vendored copy would need it.)
- Interpreter: the project's shared `.venv` Python 3.12 (numpy, scipy, shapely, Pillow present). KiCad 10.0.7
  `kicad-cli` for the DRC run.

## 2. What the tools do (DOCUMENTED, from the source docstrings and `--help`)

`py_placer/place_fanout_clearance.py` (docstring): "It nudges decoupling caps near a BGA so their pads clear
every foreign-net fanout via by `clearance`, and pulls each cap pad toward the nearest same-net ball ... Run
this AFTER bga_fanout.py." Options include `--clearance`, `--max-displacement` (default 2.0), `--no-rotate`,
`--cap-prefix` (default `C,R,FB`), `--lock REF...`, `--max-passes`. Deterministic pass (no seed option). Side
effect (VERIFIED below): it also writes DRC settings into a `.kicad_pro` beside the output.
Note: the issue text and ARCHITECTURE describe it as "BGA and QFN"; the docstring and the code in
`placement/fanout_clearance.py` name only BGAs (`analyze_bga_grid`; the word "qfn" appears there only in
comments about sibling tools). VERIFIED by the run in section 4.

`py_placer/place_simple.py` (docstring): "Place a board by function, simply: groups, one objective, annealed."
Inputs: `--groups groups.json` (functional groups, "an agent's judgement" per `py_tools/check_groups.py`) and
`--intent intent.json` (made by `py_tools/check_floorplan.py --emit-intent`). Parts the board locks, or the
intent seats in `fixed_poses[]`, stay put; every other part "starts at a random place on the board". One cost
(signal and rail trees, crossings, claims, "decaps spread over their own group's supply pins", gap tree, halo
in front of fine-pitch pads, congestion, the intent's decap limits) is lowered by simulated annealing,
`MOVES_PER_PART = 500` per movable part "from `--seed`, so the same board, inputs and seed give the same
placement". Options: `--seed`, `--seeds LIST`, `--jobs`, weights (`--decap-weight` 2.0,
`--decap-limit-weight` 10.0 and so on), `--legalise-only`, `--repair-decaps`. So: stochastic in method, seeded
and reproducible per seed. It has no option "place only part X"; the only way to restrict it is to seat every
other part with `fixed_poses[]` (what run A below does) or to lock them in the board.

The `plan-pcb-placement` skill (`.claude/skills/plan-pcb-placement/SKILL.md`) is an agent procedure, not a
program: the agent writes `groups.json` ("Group the parts (your judgement)") and the intent, runs
`place_simple` over several seeds, routes each result quickly and keeps the best by routed score ("The routed
board judges a placement"). It therefore feeds router results back into placement, which is the opposite of
router-neutral. The groups and intent used below are mine (a judgement input, listed in section 3).

## 3. Method

Scratch area outside the repository; all inputs below are scratch copies.

- Board "after": `evals/rung1/layout_run/07_poured/` (C3 at 149.293, 108.239, angle 180; U1 at 153.0, 107.5;
  this is the board `tests/test_decoupling_check.py` uses as `BOARD`). Footprint list: C1 C2 C3 D1 D2 F1 J1 J2 R1 R2
  R3 U1 U2 and mounting holes H1 to H4.
- Board "before" (C3 as before build ticket 114): the same board with C3 at (153.0, 111.2), angle 0, the pre-114
  position quoted in ARCHITECTURE and in `tests/test_decoupling_check.py`. The repository has only one commit of
  these files (`git log` shows the initial commit), so the earlier board was rebuilt, not read from history:
  `write_layer.load` / `set_number` x3 / `write_layer.save` on the scratch copy (the project's write layer; no
  text edit). ASSUMED equal to the real pre-114 board in every other respect (the layout table in
  `build_rung1_layout.py` is unchanged for the other parts).
- Measurement: my own small S-expression reader (`raw.py` in the scratch area, not `kicad_tools`): per footprint
  the raw `(at x y angle)`, per pad `(at)`, `(size)`, net, with the pad angle read as board-absolute; pad edge gap
  = distance between two axis-aligned rectangles. It reproduces the project's figures: C3 pad 1 to U1 pin 4 gap
  0.632 mm on "after" and 2.725 mm on "before" (`check_decoupling_placement` / ARCHITECTURE: 0.632 and 2.725).
  Pads are `rect`, so the rectangle is exact.
- groups.json (mine): usb_in {J1 F1 D1 R1 R2}, ldo {U2 C1 C2}, mcu {U1 C3 J2}, led {R3 D2}. Intent: the tool's own
  `check_floorplan --emit-intent`, plus `fixed_poses[]` at the current pose of the seated parts (and J1 removed
  from `edge_connectors`, which KRT refuses together with a fixed pose).
  `check_groups.py` accepted both: `errors: 0`.
- Run A, "C3 only": every part but C3 seated; C3 free. Run B, "whole": only J1 and the four mounting holes seated
  (the skill's usual state: connector and holes locked), the other 12 parts free. Both: `--seeds 0-11 --jobs 4`,
  default weights, no decap limit, under `timeout 900`. Each run took 8 to 14 s.

## 4. Results

### 4.1 `place_fanout_clearance.py`: no-op (VERIFIED)

Command (both boards): `python -X utf8 py_placer/place_fanout_clearance.py <board> <out> -v` under `timeout 300`.
Output (identical text for both boards):

```
BGAs: (none)  fanout vias: 0  movable near-BGA caps: 0
No vias on the board - run this AFTER bga_fanout.py.
No caps moved; passing the board through unchanged.
```

My reader found C3 and every other footprint at the same `(at)` before and after, on both boards. The tool also
wrote a `.kicad_pro` beside the output ("DRC settings: wrote 4 value(s) ... rules.min_hole_clearance: None -> 0.2
mm; rules.min_copper_edge_clearance: 0.0 -> 0.2 mm (fab-edge pin)"): even a no-op run changes project rules.

### 4.2 `place_simple.py`, run A (only C3 movable), 12 seeds, both boards (VERIFIED)

Command: `python -X utf8 py_placer/place_simple.py <board> "<dir>/s{seed}.kicad_pcb" --groups groups.json
--intent intent_c3only.json --seeds 0-11 --jobs 4 --json "<dir>/s{seed}.json"`. Log: every seed "cost 154.3, 500
moves".

| start board | seeds | C3 result `(at x y angle)` | +3V3 pad edge to U1 pin 4 edge | footprints moved |
|---|---|---|---|---|
| after (rule position) | 0..11 | (149.292, 107.900, 180) in 11 seeds; (149.2923, 107.899, 180) in seed 0 | 0.633 mm (dx 0.633, dy 0) | only C3, by 0.339 mm |
| before (pre-114 position) | 0..11 | the same two values | 0.633 mm | only C3, by 4.964 mm and 180 degrees |

Spread over seeds: none beyond 0.001 mm. The result does not depend on the starting position of C3. A diff of the
KRT output against its input board shows exactly one changed line, the C3 footprint `(at 149.293 108.239 180)`
becoming `(at 149.292000 107.900000 180)`. Raw pad geometry of the KRT result: C3 pad 1 (+3V3) centre
(150.067, 107.900), size 0.9 x 0.95; U1 pin 4 (+3V3) centre (151.55, 107.900), size 0.8 x 0.2; dx 0.633, dy 0.
Pin 4's centre line runs through the pad, so the connecting track is straight. GND path: C3 pad 2 (GND) centre
(148.517, 107.9); nearest U1 GND pads: pin 3 at 2.183 mm edge gap, exposed pad 21 at 3.183 mm.

### 4.3 The rule's position, same measures (VERIFIED)

Rule board "after": C3 at (149.293, 108.239) angle 180; +3V3 pad centre (150.068, 108.239); gap to pin 4 0.632 mm
(dx 0.632, dy 0); GND pad (148.518, 108.239); nearest U1 GND pad pin 3 at 2.188 mm, pin 21 at 3.182 mm. Pad top
edge y 107.764; pin 3 centre line y 107.500; difference +0.264 mm, which is the rule's clause 2 value (half the
0.127 minimum track width plus the 0.2 Power clearance, 0.2635, rounded up). KRT: pad top edge y 107.425, i.e.
-0.075 mm from pin 3's centre line: the +3V3 pad (x 149.617 to 150.517) sits in the path of a straight leftward
GND escape of pin 3 (pad x 151.15 to 151.95, y 107.4 to 107.6). Whether that costs anything at routing time was
not measured (nothing was routed); it is the exact situation clause 2 was written to avoid.

### 4.4 `place_simple.py`, run B (whole board), 12 seeds (VERIFIED)

Same command with `intent_whole.json`. C3 relative to U1's frame (offset in U1's rotation, rotation difference):

- rule: (-3.707, +0.739), 180.
- from "after": 3 seeds at (-3.708, +0.40 to +0.48), 180 (the run-A pose); 4 seeds at (-2.958, about -0.3 to -0.6),
  90; 4 seeds at (-2.958, about +1.1 to +1.2), 270; 1 seed (1) at (-6.626, +0.332), 90 with a 4.304 mm gap.
- from "before": 4, 4 and 4 seeds in the same three families, no outlier.
- +3V3 pad to pin 4 gap: 0.632 to 0.640 mm in 23 of 24 runs, 4.304 mm in one. So the objective's decap term keeps
  C3 beside pin 4 in most seeds, but the pose family (and in one seed the pin) is seed-dependent.
- Everything else: all 12 free parts moved in every seed (1.3 to 21.8 mm; U1 itself 1.9 to 19.5 mm and
  rotated), most rotated by 90, 180 or 270. Not comparable with a rung-1 input whose layout is fixed by the
  project's table.
- Determinism (VERIFIED): seed 3 of run B (from "after") rerun as a single `--seed 3` run gives an output file
  with the same SHA-256 (`a09dc809...`) as the 12-seed batch's s3.

### 4.5 DRC of the placements (VERIFIED, a placement check only)

`kicad-cli pcb drc --format json --severity-all` (KiCad 10.0.7), each board: 30 unconnected items (nothing is
routed) and no error-level violation other than that; violations are warnings (silk_edge_clearance 2,
silk_overlap 1, silk_over_copper 2 on the rule board; the KRT run-A board adds one `lib_footprint_issues`
warning, which is not caused by its single changed line; ASSUMED to come from the sibling `.kicad_pro` the tool
wrote). The pre-114 board has 7 silk_over_copper warnings. DRC does not discriminate the rule's and KRT's C3
poses. This is not a `verify_board` result and says nothing about routability.

## 5. Comparison and limits

| | rule (build ticket 114) | KRT run A | KRT run B |
|---|---|---|---|
| C3 position | (149.293, 108.239), 180 | (149.292, 107.900), 180 | seed-dependent, three families |
| +3V3 pad to pin 4 | 0.632 mm | 0.633 mm | 0.632 to 0.640 mm (one seed 4.304) |
| straight track pad to pin 4 | yes | yes | not examined |
| clear of pin 3's straight escape | yes (+0.264 mm) | no (-0.075 mm) | not examined |
| GND pad to nearest U1 GND pad | 2.188 mm (pin 3) | 2.183 mm (pin 3) | not examined |
| other footprints moved | none | none (all seated by me) | all 12 |

Reading: positions agree within the pad-pitch scale (pad pitch 0.5 mm; difference 0.339 mm in one axis, 0.001 mm in
the other), so the project's choice of "directly next to pin 4, rotation 180" is independently reproduced by a
different method (an annealed decoupling-distance objective). It is supporting evidence for clauses 1 and 3. On
clause 2 the two differ and the project's position is better by the stated rule (KRT optimises distance and
routability proxies, not a 0.2635 mm escape clearance for a neighbouring GND pin on a net class it does not know
the owner meant). I make no claim that either position routes better.

Limits: one board, one capacitor, one IC (VQFN, 0.5 mm pitch); my groups and intent are a judgement input;
`place_simple` with a different groups file, `--decap-limit-weight` or an intent decap limit may behave
differently (not run); the "before" board was rebuilt through the write layer, not read from history; the
pad-edge measure treats pads as exact rectangles (true: all `rect`); the run-B poses at 90 and 270 were not
checked against clause 2; nothing was routed, so no routing or done claim; both KRT tools write a `.kicad_pro`
beside the output, which the comparison ignored.

## 6. What part 2 should and should not expose (recommendation)

- Constraints are met only in one form: a frozen, checked board. `place_simple` is seeded and byte-reproducible
  (section 4.4), but it needs a groups file and an intent, which are a judgement input, and it moves what it is
  not told to hold. The skill's loop routes candidates and keeps the best, which would couple the rung-1 input to
  a router. Do not expose the skill's route-and-select loop, and do not regenerate at measurement time.
- Do not expose `place_fanout_clearance` for QFN or non-BGA boards: it is a documented no-op there (section
  4.1), and it rewrites DRC settings in the project file even when it moves nothing.
- If part 2 exposes a KRT placement function at all, expose it as a propose/confirm tool that (a) takes explicit
  `refs` to move and seats every other footprint (run A's mode), (b) records seed, KRT sha and version, (c) writes
  only the `(at)` of the named parts through the write layer, never the tool's own output file or `.kicad_pro`,
  (d) is graded by `check_decoupling_placement` and by a clause-2 style check (escape-axis clearance of the
  neighbouring pin), which KRT's own result would fail here, and (e) refuses on any parse gap. Treat its output
  as a candidate for the rule to cross-check, not as the rule: on this board the rule's own geometry (clause 2)
  was the better position.
- The Rust binary download needed by `place_simple` and `check_floorplan` is an extra supply-chain step for a
  tool that a deterministic project rule already replaces for this case; arm's length (subprocess) is enough.

## Open points

- A second IC and a larger board (a BGA board for `place_fanout_clearance`, a multi-pin IC for run A) would test
  whether the run-A agreement generalises; not run.
- Whether the 0.075 mm clause-2 shortfall actually blocks a router (Freerouting or KRT's router) was not measured;
  doing so means routing, which is a separate, gated measurement.
- Run B's 90/270 poses were not examined for pin-3 shadowing or pad-to-pad clearance beyond DRC.
- The sibling `.kicad_pro` writes of both tools were not diffed in full.

## Commands (condensed)

```
git clone --depth 1 https://github.com/drandyhaas/KiCadRoutingTools.git up   # sha 61565f35053a..., VERSION 0.24.2
python build_router.py                                                         # release binary v0.24.2
python -X utf8 py_placer/place_fanout_clearance.py <board> <out> -v            # no-op, section 4.1
python -X utf8 py_tools/check_floorplan.py <board> --emit-intent intent.json
python -X utf8 py_tools/check_groups.py <board> --groups groups.json --intent intent_X.json   # errors 0
python -X utf8 py_placer/place_simple.py <board> "s{seed}.kicad_pcb" --groups groups.json \
    --intent intent_X.json --seeds 0-11 --jobs 4 --json "s{seed}.json"
kicad-cli pcb drc --format json --severity-all --units mm -o <report.json> <board>
```
