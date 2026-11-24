# Clearance and creepage for mains nets (230 V side): what the standards say, what KiCad 10 can check

Research ticket 126. No code change. Tags: **VERIFIED** (fetched or measured, quoted), **DOCUMENTED**
(publisher page read), **ASSUMED** (secondary or recalled, not read in a primary source; the line says what
would verify it). Date of the work: 2026-11-24. Tool measured: `kicad-cli` 10.0.7.

## 1. Answer in short

1. **The product must never state a compliance result.** The distances a mains board needs depend on facts
   the board file does not contain: rated impulse voltage (overvoltage category and supply), working voltage,
   pollution degree, material group (CTI of the real laminate and of any coating), insulation class (basic,
   supplementary, reinforced), altitude, and which product standard applies. Only a person with the standard
   can fix them. The product can report measured distances against a table and edition that the user names.
2. **KiCad 10.0.7 can measure creepage and clearance between nets by itself**, along the copper surface of
   each copper layer and around Edge.Cuts slots (measured, section 4). It needs a custom-rule file for
   creepage (not settable in net classes); clearance per net class needs only the project file.
3. **KiCad cannot check** through-air distances across component bodies, coating or solder-mask credit,
   creepage or clearance between layers, inside components, material group, pollution degree or altitude
   (section 5).
4. **The standards' tables are paid documents.** Their figures are not reproduced here as facts; the few
   example figures in section 3 are ASSUMED and must be read from the user's own licensed copy before use.
5. **Recommendation:** document now; build a propose/confirm tool that writes the rules from numbers the
   user types and a table name the user names (option B in section 7). Embed no standard table in the product.

## 2. Terms and licence checks (read before use)

- **KiCad (`kicad-cli`, source).** VERIFIED (file header of `pcbnew/drc/drc_test_provider_creepage.cpp`,
  tag 10.0.7): "This program is free software; you can redistribute it and/or modify it under the terms of
  the GNU General Public License as published by the Free Software Foundation; either version 2 of the
  License, or (at your option) any later version." No clause limits automated or commercial use of the
  output. As everywhere in this project, `kicad-cli` is called as a subprocess (decision 6); nothing is
  imported or copied from it. KiCad documentation: VERIFIED "You may distribute it and/or modify it under
  the terms of either the GNU General Public License ... version 3 or later, or the Creative Commons
  Attribution License ... version 3.0 or later."
- **IEC 60664-1 preview (publisher VDE, edition 3.1 2025-05).** DOCUMENTED: the preview PDF carries the
  contents list only (19 pages), no table figures. Reading it is fine; it grants no reproduction right.
- **A circulated IEC committee draft (CD 2018 of 60664-1 ed. 3, found on a trade-association site).**
  VERIFIED licence line: "It is permitted to download this electronic file, to make a copy and to print out
  the content for the sole purpose of preparing National Committee positions. You may not copy or "mirror"
  the file or printed version of the document, or any part of it, for any other purpose without permission
  in writing from IEC." It is marked "PRIVATE CIRCULATION". **It is not used as a source here and nothing
  from it is reproduced.** It was opened once; the reader of this file should know that and not cite it.
- **IEC and IPC standards** are copyrighted paid documents. No clause allowing a product to embed their
  tables was found; ASSUMED that embedding or redistributing a table needs a licence from IEC or IPC. What
  would verify it: the IEC webstore terms of use and a written answer from IEC or IPC.
- **Vendor and blog pages** (a semiconductor vendor seminar paper, PCB vendor blogs) are secondary. They are
  used only where marked, as ASSUMED.

## 3. Which standard table gives which figure

### 3.1 What fixes the figure (inputs)

For a 230 V single-phase supply (nominal 230/400 V system) the inputs are: overvoltage category (OVC I to
IV), working voltage (rms, and peak), pollution degree (1 to 4), material group by CTI, insulation class
(functional, basic, supplementary, reinforced), altitude (correction above 2000 m) and the board material
type (printed wiring material has its own creepage columns).

- Material groups by CTI. DOCUMENTED (vendor seminar paper SLUP419, "Table 1. Material groups based on the
  CTI"): group I is CTI 600 and above, II is 400 to below 600, IIIa 175 to below 400, IIIb 100 to below 175 (or
  not specified). The same paper: "Most FR4 materials used in PCB fabrication are rated for material group
  IIIa." Secondary; what verifies it is the CTI column of the laminate's datasheet and the standard's
  definition (IEC 60664-1 clause 4.8.1.x, ASSUMED clause number).
- Pollution degree 2 (non-conductive pollution, occasional condensation) is the usual choice for indoor
  equipment; the choice (and a coating or enclosure that lowers it) is a system decision. Not checkable from a
  board file.

### 3.2 Table map (which table, by standard)

| Standard | Impulse voltage from the supply | Clearance | Creepage | Status |
|---|---|---|---|---|
| IEC 60664-1 ed. 3.1 (2025-05) | Table F.1, "Rated impulse withstand voltage for equipment energized directly from the mains supply" | Table F.2, "Clearances to withstand transient overvoltages" | Table F.5, "Creepage distances to avoid failure due to tracking" | Table titles and numbers VERIFIED from the publisher preview contents list. Figures not readable there. |
| IEC 62368-1 ed. 3.0 (2018-10) | Table 12, "Mains transient voltages" | Table 14, "Minimum clearances using required withstand voltage" (also Table 10 for voltages up to 30 kHz, Table 11 above 30 kHz) | Table 17, "Minimum creepage distances for basic insulation and supplementary ..." | Titles VERIFIED from the publisher (redline) preview contents; clause 5.4.2 clearances, 5.4.3 creepage distances, Annex O measurement of creepage and clearances. Figures not readable. |
| IEC 61010-1 ed. 3.0 (2010-06) | Table 4, "Clearances and creepage distances for mains circuits of overvoltage category II up to 300 V"; Annex K for category III and IV (Tables K.2 to K.4) | same Table 4 | same Table 4; Table 7 for secondary circuits | Titles VERIFIED from the publisher preview contents. Figures not readable. |
| IPC-2221 (B and C) | none | Table 6-1, "Electrical conductor spacing" (columns B1 to B4, A5 to A7) | none (a design spacing table, no tracking or CTI concept) | Not a product-safety standard. Figures ASSUMED (see 3.3). |

Edition drift matters: the 60664-1 creepage table is F.5 in edition 3.x (VERIFIED, contents list); older
editions numbered it F.4 (ASSUMED, from vendor papers). Table numbers in product standards change between
editions too (the "Table 15 14" renumbering seen in a redline of 62368-1 shows 14 in edition 3). Always name
the edition with the table.

### 3.3 Example figures for one case (all ASSUMED)

Case: 230 V rms single-phase mains, overvoltage category II, pollution degree 2, material group IIIa,
basic insulation, altitude up to 2000 m, printed wiring board.

| Quantity | Figure | Table | Tag | What would verify it |
|---|---|---|---|---|
| Required impulse withstand voltage, 230/400 V, OVC II | 2500 V | 60664-1 F.1 | ASSUMED | Read Table F.1 in the licensed standard. |
| Clearance, 2500 V, inhomogeneous field (case A), pollution degree 2 | 1.5 mm | 60664-1 F.2 | ASSUMED | Table F.2 in the licensed standard. |
| Creepage, 250 V row (230 V is read at the next higher row), pollution degree 2, material group III | 2.5 mm | 60664-1 F.5 | ASSUMED | Table F.5 in the licensed standard. |
| Same, material group I and II | 1.25 mm and 1.8 mm | 60664-1 F.5 | ASSUMED | same |
| Reinforced insulation | clearance from the next higher impulse step (OVC II: 4000 V, 3.0 mm); creepage twice the basic value | 60664-1 5.4 (clause numbers) | ASSUMED | clauses on reinforced insulation in the licensed standard |
| IPC-2221 external uncoated conductors (column B2), 301 to 500 V (325 V peak of 230 V rms) | 2.5 mm | Table 6-1 | ASSUMED (several vendor pages agree; one source disagrees on column mapping, and IPC-2221C relabels B4) | IPC-2221 Table 6-1 in the licensed edition |

Observations that follow from the table structure rather than from the figures (VERIFIED from the table
titles, ASSUMED for their content): creepage can never be below clearance, so the larger figure rules; the
lower the CTI and the higher the pollution degree, the longer the creepage; clearance and creepage are read
from different tables with different inputs. The product standards (62368-1, 61010-1) add rules of their own
(for example other transient values and insulation classes), so the same board can need different figures
under different standards. A claim "230 V needs 2.5 mm" is wrong without all the inputs.

## 4. What KiCad 10 checks, and measurements

### 4.1 Documentation and source (primary)

- DOCUMENTED, VERIFIED quotes from the KiCad 10 PCB editor manual (DRC violations): "Creepage violation: This
  violation occurs when the creepage distance between two copper items with different nets is smaller than
  the configured creepage for those nets. ... Creepage distances can be configured using a creepage
  constraint in custom rules." Custom rules list: "creepage min: Specifies the creepage between copper
  objects of different nets." Example in the manual: `(rule "minimum creepage distance for high voltage nets"
  (condition "A.hasNetclass('HV')") (constraint creepage (min 5mm)))`. Constraint list: "Creepage distance:
  Minimum surface creepage distance between conductors at different potentials."
- Clearance. VERIFIED quote: "clearance min: Specifies the electrical clearance between copper objects of
  different nets. (See physical_clearance if you wish to specify clearance between objects regardless of
  net.)" and "physical_clearance min: Checks the clearance between two objects, regardless of their nets.
  ... Physical clearance is only checked between objects on the same layer, except for objects on Edge.Cuts,
  which are treated as if they are on all layers."
- Source, tag 10.0.7 (VERIFIED; the file is identical to the 10.0 branch head of 2026-11-24), `pcbnew/drc/
  drc_test_provider_creepage.cpp`:
  - For every pair of nets and every copper layer, a creepage graph is solved:
    `for( auto it = layers.copper_layers_begin(); it != layers.copper_layers_end(); ++it )`. Each layer is
    treated alone: the check is on-surface per layer. The pair loop is quadratic in the number of nets
    (cost on large boards not measured).
  - Obstacles: Edge.Cuts items of the board and of footprints, and NPTH holes ("Oblong slot: add the two
    semicircular end caps and two straight sides. The slot outline is the border that creepage paths must
    not cross."). The outline polygon has NPTH holes subtracted so that no path runs through a slot
    (comment cites KiCad issue 24286).
  - Groove handling is behind an advanced setting: `if( ADVANCED_CFG::GetCfg().m_EnableCreepageSlot )
    graph.m_minGrooveWidth = ...; else graph.m_minGrooveWidth = 0;`. So by default there is no minimum slot
    width: a 0.1 mm saw cut is treated like a wide slot (source reading, not measured). Standards give a
    minimum groove width below which a slot does not lengthen the creepage path (ASSUMED, 60664-1 Annex
    figures; verify in the licensed standard). The fab's own minimum slot width is a separate question.
  - Violation condition: `if( creepageValue <= 0 || distance - creepageValue >= 0 ) return 1;` (no
    violation at or above the minimum).
  - A rule with a condition is evaluated per path; "Placeholders at the origin never satisfy
    intersectsArea()" (a rule that depends on an area may be missed in the graph sizing).
- No material, CTI, pollution degree, coating or voltage appears in the source: the constraint is a plain
  millimetre minimum chosen by the rule author.

### 4.2 Measurement on a scratch board

Board: a copy of the project's golden board fixture in a scratch directory outside the repository. Through the
project's write layer (`write_layer.load`, `append`, `save`; no text edit) two single-pad footprints were added
and two nets `MAINS_L` and `MAINS_N` declared. Raw tokens read back from the saved file: pads `(at 130 121)`
and `(at 140 121)` in the footprint frame, each `(size 1 1)`, both on F.Cu; so the facing pad edges are
x = 130.5 and x = 139.5: **straight edge-to-edge distance 9.0 mm**. Slots were added with the project's
`propose_keepouts_and_slots` / `confirm_keepouts_and_slots` (gr_line and gr_arc items on Edge.Cuts). Raw slot
tokens for the first case: lines `(start 134.5 119) (end 134.5 122)` and `(start 135.5 122) (end 135.5 119)`,
arcs `(start 134.5 122) (mid 135 122.5) (end 135.5 122)` and `(start 135.5 119) (mid 135 118.5) (end 134.5
119)`: a 1.0 mm wide round-ended slot, x = 134.5 to 135.5, y = 118.5 to 122.5, between the pads.

Rule files (test-local `.kicad_dru` in the scratch directory, the one tolerated exception; the project file
for net classes was written with `write_layer.save_project`):

```
(version 1)
(rule "mains_creepage"
  (constraint creepage (min 20mm))
  (condition "A.NetName == 'MAINS_L' && B.NetName == 'MAINS_N'"))
```

and the same with `(constraint clearance (min 20mm))`. The 20 mm minimum is deliberately far above the true
distance so that KiCad prints the actual value. Command (each run in its own directory with the board,
project and rule file sharing one base name):

```
timeout 120 kicad-cli pcb drc --format json --severity-all --output out.json b.kicad_pcb
```

Control: the same board with no rule file reports only `lib_footprint_issues` (the scratch footprint is not in
a library); no clearance or creepage item.

Results (VERIFIED, quoted from the `description` field of the JSON; the output language followed the machine
locale, German here, English glosses in brackets):

| Board | Rule | Reported actual |
|---|---|---|
| no slot | creepage | `Kriechstrecken-Verstoß (Regel "mains_creepage" Kriechstrecke 20,0000 mm; tatsächlich 9,0000 mm)` [creepage violation, actual 9.0000 mm] |
| no slot | clearance | `Freiraum-Verstoß (Regel "mains_clearance" Freiraum 20,0000 mm; tatsächlich 9,0000 mm)` [actual 9.0000 mm] |
| round slot 1.0 wide, y 118.5 to 122.5 | creepage | actual 9.2213 mm |
| same slot | clearance | actual 9.0000 mm (the slot does not change a clearance) |
| square-ended slot 1.0 wide, y 119 to 122 | creepage | actual 9.0623 mm |
| round slot 1.0 wide, y 112.5 to 123.5 (long) | creepage | actual 9.8613 mm |
| round slot 3.0 wide, y 117.5 to 123.5 | creepage | actual 9.8890 mm |

Cross-check of the creepage figures against plain geometry computed by hand from the raw coordinates
(shortest path from the pad corner around the slot end; tangent lines plus arc): round 1.0 slot 9.22 mm
(measured 9.2213), square slot 2 x sqrt(4^2 + 0.5^2) + 1 = 9.0623 mm (measured 9.0623), long slot 9.86 mm
(measured 9.8613), 3.0 wide 9.885 mm (measured 9.8890; 0.004 mm difference is the arc approximation).
**So KiCad's creepage is the exact shortest surface path around the slot geometry, and a slot only lengthens
it a little unless it is long compared with the pad distance.** In this example a 4 mm slot between pads 9 mm
apart bought 0.22 mm.

Net class and layer behaviour (VERIFIED):

- A net class "Mains" (clearance 9.5 mm, patterns `MAINS_*`) in the project file with no rule file gives
  `Freiraum-Verstoß (Netzklasse "Mains" Freiraum 9,5000 mm; tatsächlich 9,0000 mm)` between the two pads: net
  class clearance needs no rule file.
- Rules written with `A.NetClass == 'Mains' && B.NetClass != 'Mains'` and with both `== 'Mains'` (creepage,
  min 20 mm) matched: the L to N pair (actual 9.0000 mm) and every Mains pad against other nets' pads
  (22 pairs listed, from 12.1485 mm to 18.7149 mm). Conditions on net class and on net name both work.
- With the neutral pad moved to B.Cu (same x and y), **neither the creepage rule nor the clearance rule
  reported anything**: a pad on the front layer and a pad on the back layer are not checked against each
  other (per-layer checks, as the source says). The 1.6 mm through the board is solid insulation, a different
  property.

Reproducibility: the scratch board is built by about forty lines of write-layer calls; the build script and
the boards were kept outside the repository. Not measured: creepage between a track or a zone fill and a pad
(the source includes the track and zone headers; no measurement was made), boards with inner layers, curved
outlines, time on a large board.

### 4.3 Report quirks to know before building anything on the output

- VERIFIED: the "actual" distance exists only inside the localised description text, with a decimal comma in
  the German run, and only for violations. A read-only distance report built on `kicad-cli` therefore needs a
  rule minimum above every distance of interest (the graph is sized by the largest minimum, so a huge value
  costs time; not measured) and a locale-proof parser, or must force the language (not tried).
- The JSON items carry the two pads and their positions, not the path.

## 5. What cannot be checked

| Need | KiCad 10 | Tag |
|---|---|---|
| Surface path between copper of two nets on one layer, around Edge.Cuts slots and NPTH holes | yes, with a creepage rule | VERIFIED (4.2) |
| Straight-line copper-to-copper distance between nets on one layer | yes, `clearance` (net class or rule) | VERIFIED (4.2) |
| Needs a rule file | creepage always; conditions on pairs of net classes; physical clearance. Net class clearance does not | VERIFIED / DOCUMENTED |
| Layer to layer (front pad to back pad, or to an inner layer) | no, per layer only | VERIFIED (4.2, source loop) |
| Through-air distance across a component body, from a lead to a metal part of another package, or to a mounted heatsink or enclosure | no: only copper and Edge.Cuts are obstacles, 3D bodies are not | DOCUMENTED (source: obstacles are Edge.Cuts and NPTH holes; creepage between copper only) |
| Creepage along a component package (lead to lead over the plastic) | no | DOCUMENTED, same reason; pad-to-pad on the board only |
| Inside components, optocoupler or transformer isolation rating | no (datasheet property) | ASSUMED, no model in KiCad |
| Coating, conformal coating, solder mask credit, potting, pollution degree 1 or 2 by enclosure | no | DOCUMENTED (no such input in the source or in the constraint list) |
| Material group, CTI, working voltage, overvoltage category, altitude, insulation class | no; the user translates them into millimetres | DOCUMENTED |
| Which nets are line, neutral or secondary | only through names or net classes the user assigns | VERIFIED |
| Minimum groove width that makes a slot count | off by default (advanced setting) | DOCUMENTED from source; behaviour not measured |
| Solid insulation (board thickness, test voltage), electric strength, dielectric test | no | ASSUMED |

## 6. Hard limits for the product

- Never write "compliant", "passes IEC ...", "safe", or a green mark for a mains board. A rule file with
  correct numbers does not make a board compliant: the DRC says "no violation of the rule the user wrote".
- Always print: the table and edition the numbers came from (supplied by the user), the inputs (working
  voltage, pollution degree, material group, insulation class, altitude) as the user entered them, the
  measured distance, and the list of what was not checked (section 5).
- A missing creepage rule must not look like a pass: a board with mains net classes and no creepage rule is
  "creepage not checked" (the same principle as `report_clearance_coverage`, which today lists creepage only
  as an uncovered constraint; see ARCHITECTURE on issue 134).
- The DRC counts as a gate input only for the rules it ran. `verify_board` PASS means zero error-level DRC
  with the rules present; it does not make the mains distances right.

## 7. Options (one line each)

- **A. Document only.** Add a short how-to (skill text and this file): name the table, write the rule by hand.
  Cheapest, no new risk, but it leaves the hand calculation in place and a missing rule goes unnoticed.
- **B. Rule writer (recommended build).** A propose/confirm tool that writes a creepage and a clearance rule per
  net-class pair into the board's `.kicad_dru` and the net classes into the project file, from **numbers the
  user types** together with the **table name, edition and inputs**; it embeds no standard table; it records
  them in a comment in the rule file; the result says "rules installed, not compliance". The next DRC (and
  `verify_board`) then reports violations by KiCad itself.
- **C. Read-only distance report.** Per net pair, the measured minimum clearance and creepage next to the
  user's named threshold, from a temporary rule run; strongest for review but needs the locale-proof parse
  (4.3). Park until B exists; it can reuse B's inputs.
- **D. Built-in tables (not recommended).** Ship IEC or IPC figures with a table picker: licence unclear, edition
  drift, and it invites the claim the product must not make. Rejected.

## 8. Draft text for the follow-up build ticket

**Title:** Mains net rules: propose/confirm writer of creepage and clearance rules from user-given distances.

**Scope:** New propose/confirm pair on the `verification` or `placement` toolset (decide in the ticket). Input:
`classes` (net class name, net patterns), `pairs` (class A, class B, `clearance_mm`, `creepage_mm`),
`source` (free text naming standard, edition, table, row, working voltage, pollution degree, material group,
insulation class, altitude: required, stored, echoed). Output: one board revision with net classes in the
project file and rules in the `.kicad_dru` through the write layer (issue 131 mechanics: no kicad-cli parse
check exists for `.kicad_dru`, so the tool re-runs `kicad-cli pcb drc` and checks that a probe pair reports
the expected rule name). **No built-in table. No word "compliant" in any output; the result carries
`compliance: not_assessed`, the `source` echo and the "not checked" list of section 5.**

**Acceptance:** (1) A test rebuilds the section 4.2 boards and asserts creepage 9.0000 and 9.2213 mm and the
clearance 9.0000 mm from the `kicad-cli` description (the locale pinned or the parser proof against German and
English). (2) A pad on the other copper layer is not reported (pinned as a limit). (3) A board with Mains
classes and no creepage rule shows "creepage not checked" in `report_clearance_coverage`. (4) The tool text
and the `kicad-verification` skill carry the non-compliance sentence; a sweep test fails on "compliant",
"safe" or "passes IEC". (5) Targeted tests, adversarial review, no batch gate (not a routing path).

**Open before building:** measure track and zone to pad creepage, an inner layer, a slot below 1 mm and the
advanced groove setting; time on a board with 100 or more nets; decide how `source` is validated (free text,
never a lookup).

## 9. Open points

1. All millimetre figures from the standards (section 3.3) are ASSUMED. The user must read them in their own
   licensed copy of the named edition. The IEC webstore terms on embedding tables were not read.
2. KiCad measurements are on 10.0.7 with a synthetic two-pad board; not measured: tracks and zone fills as
   creepage endpoints, inner layers, curved outlines, groove-width setting, run time on large boards.
3. The localised report text (4.3) is a hazard for any parser; the way to force English was not tried.
4. A committee draft was opened for a consistency check of the author's recollection (section 2). It must not
   be cited or reproduced; the figures here stand or fall by the licensed standard.
5. Whether 62368-1 and 61010-1 give different numbers for the example case was not established (titles only).

## 10. Sources

- KiCad 10 PCB editor manual (docs.kicad.org, 10.0, pcbnew), DRC, custom rules and constraints sections.
- KiCad source, tag 10.0.7, `pcbnew/drc/drc_test_provider_creepage.cpp`.
- IEC 60664-1 ed. 3.1 (2025-05) publisher preview (contents and table list).
- IEC 62368-1 ed. 3.0 (2018-10) redline, publisher preview (contents and table list).
- IEC 61010-1 ed. 3.0 (2010-06), publisher preview (contents and table list).
- Texas Instruments power supply design seminar paper SLUP419 (material groups by CTI; secondary).
- Several PCB vendor and EDA vendor pages on IPC-2221 Table 6-1 (secondary).
