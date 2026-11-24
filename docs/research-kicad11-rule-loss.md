# Jetson demo: why the KiCad 11 nightly lost 357 custom-rule clearance errors (issue 210)

Research note for issue 210 (follow-up of issue 203, `docs/research-kicad11-nightly.md`). Nothing in the
repository was changed except this file. Tags: VERIFIED (run or fetched and quoted here), DOCUMENTED
(publisher page read), ASSUMED (not measured).

## Answer

**Cause not found. Narrowed to the zone fill, not to the rule.**

1. VERIFIED: the 357 errors are an artefact of the zone fill stored in the demo file. On 10.0.7 the same
   rule gives 357 clearance errors on the stored fill and **0** after `--refill-zones`. A rule-free run gives
   0 as well. The rule text and the NetClass wildcard are not what changed the count.
2. VERIFIED: the wildcard `==` on NetClass works on 10.0.7 (exact name, `*CSI*`, `90Ohm*` and `*diff*` give the
   same hit sets).
3. VERIFIED (source): the code that matches `A.NetClass` in rule expressions is textually unchanged between
   10.0.7 and the nightly's commit. Only the zone-clearance test path and the net-class lookup around it
   changed, and none of the changes I read explains a loss.
4. **The nightly could not be run**: it is no longer on this machine (searched below) and the task forbids
   downloading. So the nightly figure of 0 was not reproduced, no second nightly exists, and the mechanism is
   open. The one run that decides it is named under "Open points".

Confidence: high that the 357 depend on the stored fill and not on the NetClass wildcard (measured). Low on
why the nightly differs (no measurement on the nightly). Consequence for the project: a rule result on a
board with stored fills is not comparable across KiCad versions unless the zones are refilled first; the
`verify_board` gate already needs a fill (hard rule 3: all zones filled and verified), so this is a reason to
refill before any cross-version comparison, not evidence that rule evaluation broke.

## Setup (VERIFIED)

- KiCad 10.0.7 `kicad-cli` (the installed release). Demo "jetson-agx-thor-baseboard" copied to a scratch
  folder outside the repository (88.7 MB board). The `.kicad_pcb`, `.kicad_pro` and the schematics were never
  edited. The board file is version 20241229 (written by KiCad 9).
- The demo's own `.kicad_dru` was the only file varied, and only in the scratch copy: it was overwritten with
  one-rule variants (the project's tolerated exception for a test-local `.kicad_dru`, CLAUDE.md P2), then
  restored; its hash equals the installed demo's (`a376ee183d715...` for both).
- Every run: `kicad-cli pcb drc --severity-all --format json -o <out> <board>` under a 600 s subprocess
  timeout (about 25 s each; one 900 s `timeout` for the first). Figures are counts of `type == "clearance"` in
  the JSON.
- The demo's rule, verbatim from its `.kicad_dru`:

```
(rule "(hs_zone_clearance"
	(condition "(A.NetClass == '*diff*')&&(B.Type == 'Zone')&&(A.Layer == B.Layer)&&(A.NetName != '*USB*')")
	(constraint clearance (min 1mm))
)
```

  Note that the issue text quotes only the first two clauses; the file has four.

## Step 1: reproduction on 10.0.7 (VERIFIED)

```
$ kicad-cli pcb drc --severity-all --format json -o drc_orig.json jetson-agx-thor-baseboard.kicad_pcb
578 Verstoesse gefunden          (578 violations)
Unverbundene Elemente (0) gefunden
```

JSON: `kicad_version` "10.0.7"; by type: clearance 357, lib_footprint_issues 199, silk_edge_clearance 10,
silk_over_copper 5, courtyards_overlap 3, pth_inside_courtyard 2, npth_inside_courtyard 2. All 357 clearance
items are "rule (hs_zone_clearance, 1.0 mm, actual 0.10 to 0.87 mm" between a track (348), or pad (9), and a zone
(357 zones, e.g. "Zone [+3V3] on In2.Cu"). Violating nets: 41, all in classes 90Ohm-diff_CSI (308 items),
85Ohm-diff_DP (25), 85Ohm-diff_PCIE (14), 85Ohm-diff_SFI (1).

The project file stores net classes as lists (KiCad 10 format), for example
`"/CSI/CAM0.CSI0_CLK+": ["90Ohm-diff_CSI"]`, 391 explicit assignments, `netclass_patterns` empty, one class per
net. 40 nets are assigned `95Ohm-diff_CSI`, which has no entry in `classes` (an implicit class in KiCad 10);
none of them is among the violators.

## Step 2: bisection on 10.0.7 (VERIFIED, all runs on the stored fill unless marked)

| Variant (condition of a one-rule `.kicad_dru`, constraint `clearance (min 1mm)`) | clearance errors |
|---|---|
| demo rule file as shipped | 357 |
| demo rule file, `--refill-zones` | **0** (total 222 violations) |
| only the demo rule, alone in the file | 357 |
| same, `--refill-zones` | **0** |
| no rule file content (`(version 1)` only) | 0 (total 221) |
| no rule, `--refill-zones` | 0 (total 222) |
| `A.NetClass=='*diff*' && B.Type=='Zone'` (no Layer, no NetName clause) | 499 |
| `B.Type=='Zone' && A.Layer==B.Layer` (no NetClass at all) | 499 |
| `A.NetClass=='90Ohm-diff_CSI'` (exact name) + Zone + Layer + NetName | 308 |
| `A.NetClass=='*CSI*'` (wildcard) + Zone + Layer + NetName | 308 |
| `A.NetClass=='90Ohm*'` (prefix wildcard) + Zone + Layer + NetName | 308 |
| `A.hasExactNetclass('90Ohm-diff_CSI')` + Zone + Layer + NetName | 0 |
| `A.NetClass!='Default'` + Zone + Layer + NetName | 0 |
| `A.NetClass!='Default' && B.Type=='Zone'` | 0 |
| `!(A.NetClass=='Default') && B.Type=='Zone'` | 0 |
| `A.NetClass!='*diff*' && B.Type=='Zone'` | 499 |

Reading (the first rows are measurements, the interpretation is mine):

- The count depends on the fill (357 against 0 on refill) and on nothing else in the rule that I varied. The
  stored fill reaches within 0.10 mm of tracks that the rule says need 1 mm; a fill made under this rule is
  pulled back (ARCHITECTURE: "a zone fill is pulled back instead", measured earlier in issue 131) and gives no
  violation. So the demo's stored fill was not made under this rule (or not under a rule that applied to these
  pairs); 10.0.7 reports it, the nightly does not (ASSUMED reason: see below).
- NetClass wildcard `==`, prefix wildcard and exact name all select the same tracks. The wildcard is not the
  cause.
- **Unexplained, measured on 10.0.7 only:** `A.NetClass != ...` and `!(A.NetClass == ...)` and
  `A.hasExactNetclass(...)` conditions produce 0, and `!= '*diff*'` produces the same 499 as `== '*diff*'`. I
  expected `!= 'Default'` to match the 308 CSI tracks. The rule file loads without a message (221 violations
  total, same as no rule). I did not find the reason (no source read for `hasExactNetclass`). Whatever it is,
  it means a negated NetClass condition cannot be trusted on 10.0.7 either, which matters for the rule
  coverage evaluator (issue 131) and is a separate open point.

## Step 3: source (gitlab.com/kicad/code/kicad, blobless clone read with git)

Tags `10.0.7` (`93a8a4827b`) and the nightly's commit `3f3d8fbe5e` (2026-11-17, the build measured in issue
203) are both in the clone; the merge base is `0feeca2a80`. All below VERIFIED by `git show`/`git diff`.

Unchanged from 10.0.7 to master (no behavioural difference):

- `pcbnew/pcbexpr_evaluator.cpp`, class `PCBEXPR_NETCLASS_VALUE`, methods `EqualTo` and `NotEqualTo`: identical
  text. It tests every constituent class name with `WildCompareString` when the literal is a wildcard, then
  falls back to `VALUE::EqualTo` on the effective class name.
- `common/libeval_compiler/libeval_compiler.cpp`, `VALUE::EqualTo`: only the right-hand literal's
  `m_stringIsWildcard` flag selects `WildCompareString`. Same in 10.0.7 and master. A string literal is
  flagged a wildcard when it contains `?` or `*` (`TREE_NODE`/UOP push at line 1081 in 10.0.7, 1152 in master).
  Commit `432a8ee4cd` (2026-04-29, per-thread copy of string literals) also copies the wildcard flag.
- `pcbnew/board_connected_item.cpp`, `GetEffectiveNetClass` and `GetNetClassName`: identical.
- `pcbnew/pcbnew_jobs_handler.cpp`, `JobExportDrc`: zones are refilled only when `drcJob->m_refillZones` is
  set, in 10.0.7 and master alike.
- `pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr_parser.cpp`: `T_filled_polygon` parsing of stored fills and
  `zone->SetNeedRefill( false )` after load are unchanged in effect.

Changed since 10.0.7 and inside the nightly (candidates; none shown to be the cause, all ASSUMED as cause):

- Zone selection for clearance tests: `DRC_TEST_PROVIDER_COPPER_CLEARANCE::collectZonesNear` and
  `drc_zone_index.h` (`DRC_ZONE_INDEX`), introduced by `1057e9b0be` (2026-09-20, "Reuse prepared copper shapes
  in clearance checks"). It queries zones inside the item box inflated by `m_board->m_DRCMaxClearance`. In
  10.0.7 the loop was over every zone in `m_DRCCopperZones` with the same worst-case box check inside
  `testItemAgainstZone`. If `m_DRCMaxClearance` (built in `drc_cache_generator.cpp`) did not include a
  conditional rule's 1 mm in the nightly, such a zone would not be tested. I did not read the max-clearance
  builder's diff: this is the first thing to check.
- `GetCopperZoneRTree( zone )` replaces `m_CopperZoneRTreeCache[ aZone ]` (`f83bdc86b7`, 2026-10-16, "Eliminate
  last users of bare item cache map").
- Rule evaluation: `1f48cad694` (2026-06-21, per-item memo and expression-level cache of predicates, "violation
  counts are unchanged" per its message), `57cee3a9a2` (2026-07-23, `RequiresPairItems` from the compiled
  ucode instead of searching "B." in the text), `ff52f0d289` (2026-09-07, rejects unavailable B references in
  single-item rules), `52b43fb8fc`/`3d4c0bd28a` (2026-11-14/15, property strings that look like numbers become
  numbers).
- Net classes: `33cdb12c4a` (2026-10-15, composite constituents kept alive), `c8eab111b6` (project settings
  reload), `917b5bdb88` (stale assignments dropped on save), `53f65001bd`/`eeb7a9b64b` (implicit netclass rules).
  `a7f4196c58` (2026-11-19, plain-name net class lookup table) is **not** in the nightly (checked with
  `git merge-base --is-ancestor`), so it cannot explain the nightly's 0.

Not found: any commit message or code that discards or ignores a stored zone fill on load, or that makes
`kicad-cli pcb drc` refill zones by itself.

## Step 4: second nightly (not possible)

Searched the installed programs, the user profile, `ProgramData` and the scratch folders for any other
`kicad-cli.exe`: only the 10.0.7 install exists. The extracted build 6738 from issue 203 is gone, and the task
forbade downloading. So: **no nightly was run, none was available, and the nightly figure of 0 is as reported in
issue 203, not re-measured.**

## Open points

1. Run the nightly on a scratch copy with the stored fill and with `--refill-zones`. Predictions to test:
   (a) nightly, stored fill: 0 means it loads or tests the fill differently (check `collectZonesNear` and the
   max-clearance builder next); 357 means issue 203's difference came from something else (library or file
   path, a changed rule file read). (b) nightly with refill: expected 0.
2. Read the diff of `drc_cache_generator.cpp` (maximum clearance including conditional rules) and
   `DRC_ENGINE::EvalRules` for pair rules with `B.Type`.
3. The negated NetClass forms and `hasExactNetclass` returning nothing on 10.0.7 (above) are not explained.
4. The demo file is a KiCad 9 board; whether the same holds for a board saved by 10.0.x was not tested.

## Proposed sentence for ARCHITECTURE known facts

> A custom-rule clearance result on a board with stored zone fills is a fact about that fill: the jetson demo
> gives 357 `hs_zone_clearance` errors on KiCad 10.0.7 from the stored fill and 0 after `--refill-zones`, with
> the same rule, and the nightly's reported 0 was not reproduced (issue 210, cause open); refill before
> comparing rule results across versions, and do not trust `NetClass !=` or `hasExactNetclass` conditions
> (measured as matching nothing on 10.0.7).
