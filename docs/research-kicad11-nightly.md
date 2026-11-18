# KiCad 11 nightly: which workarounds and features it removes, measured

Research note for issue 203 (owner idea: look at KiCad 11 now, issue 168 is parked until a release
candidate). Nothing in the repository was changed. The repo has no `docs/research/` folder; this file
follows the existing `docs/research-*.md` naming. It is not yet listed in an index (none was named).

## Build measured (every result below is for this build only)

- **KiCad nightly `10.99.0-6738-g3f3d8fbe5e`**, Windows x86_64 full installer
  `kicad-nightly-10.99.0.6738.g3f3d8fbe5e-x86_64.exe`, 903.7 MB, sha256
  `376d11dddf4b2a2fc1bbf63da5c47290aab16dd6a2427ad1664f8269b0cccd94` (computed here).
  Source commit `3f3d8fbe5e89e2d0fc84c90ec13eb68fbc421323`, committed 2026-11-17; the binary says
  "Date: Nov 18 2026 06:35:25", wxWidgets 3.3.4, OCC 8.0.1, ngspice 47 (`kicad-cli version --format about`,
  MEASURED). Nightlies change daily; a second build must be measured before any build ticket.
- Source of the download: the official listing `downloads.kicad.org/kicad/windows/explore/nightlies`
  (fetched; it lists full and lite installers for x86_64 and arm64 per day and redirects to
  `r2-downloads.kicad.org`). VERIFIED. KiCad's help page "nightlies and rcs": "These builds may contain
  serious bugs including those that can cause loss of data" and nightlies "always have a minor version of
  99"; it sets no further terms and says to work on copies (DOCUMENTED, fetched). The downloads site's own
  terms were not read. KiCad is GPLv3: only as a subprocess or over the IPC socket (ARCHITECTURE decision 6).
- **Side by side without installing: yes.** The installer is an NSIS archive. I did not run it. I took the
  official 7-Zip 23.01 MSI from 7-zip.org, unpacked it with an administrative image extract
  (`msiexec /a`, which installs nothing) into a scratch folder, and extracted the nightly with that
  `7z.exe` (24280 files, 4.4 GB, 56 s). `kicad-cli.exe` then runs from the scratch folder with
  `KICAD_CONFIG_HOME` pointed at a scratch folder. The 10.0.7 install was never touched (its
  `kicad-cli version` still prints 10.0.7). 7-Zip's licence text was not read (ASSUMED fine for a
  scratch tool). Disk cost 4.4 GB extracted plus 0.9 GB download.
- Not in the extract: no `python.exe`, no `pcbnew.py`, no `Lib`. `bin/_pcbnew.dll` exists but has no
  `PyInit__pcbnew` export, while 10.0.7's `_pcbnew.dll` has it (byte search, MEASURED). Source:
  commit 65a442b1d2 (2026-03-22) "REMOVED: SWIG, wxPython, and Python integration", and eb515676c6 "PCM:
  Alert users about incompatible SWIG plugins". So `pcbnew` Python is gone in this nightly.

## How things were run

Scratch copies of KiCad's own demos (identical files in both installs, checked by hash for every demo
board), and a scratch copy of `evals/rung1/layout_run/07_poured` (read, never in place). The IPC client
was `kipy` built from `kicad-python` head `76b47d25a826a56a301660f5f572dda968c84d59` (version
`0.9.0.dev0`, MIT, 2026-11-08) with protobuf sources taken from the nightly's own commit, in a separate
virtual environment (PyPI has only kipy 0.8.0, which the project pins). Its version check was off
(`kicad_api_version` "unknown"). Raw file facts come from the saved text, not from `kicad_tools`.

## Q1 and Q2: headless API (the open point of issue 168)

All MEASURED on build 6738 against `kicad-cli api-server` started by `KiCad(headless=True, kicad_cli_path=...)`.

| Step | Result |
|---|---|
| `kicad-cli api-server` | exists (10.0.7: "Failed to parse 'api-server'"). Usage: `api-server [--socket PATH] [PROJECT_OR_FILE]`; listens on a named pipe `%TEMP%\kicad\api.sock` by default |
| `GetVersion` | `10.99.0 (10.99.0-6738-g3f3d8fbe5e)` |
| Pre-load by command-line file | did not open a document: `GetOpenDocuments` answered "no handler available" until `open_document(path, type)` was called |
| Open schematic (`open_document`) | works; `get_schematic().get_symbols()` returned 124 symbols of the pic_programmer demo |
| `load_all_libraries` / `wait_for_libraries` | works, project libraries `LLS_LOADED`. Global library table is empty with a clean config dir, so `Device:R` was "not found"; project-local `pic_programmer:R` placed fine |
| **Place symbol from library** (`place_symbol_from_library`) | **works**: `R900` placed with an explicit reference; a second call without reference gave `C?` (unannotated). Saved file has the symbol block with its `(instances (project ... (reference "R900")))`. One quirk: the first symbol was written with `(lib_id "R_1")` and `(lib_name "R_1")` (no library prefix); ERC reported 1 `lib_symbol_issues` |
| **Field edit** (`UpdateItems`) | **applies on the server** (value `10k`, footprint `Resistor_SMD:R_0603_1608Metric` read back and written to the file), but the kipy head client raises `AttributeError: type_url` while decoding the response (`editor.update_items` calls `unpack_any` on an already unpacked item). A client bug, reproduced on schematic and PCB; the edit is not reported as success |
| **Annotate** | **no API command and no CLI command.** The only annotation path is the `reference` argument of the place command. Proto search for annotate/autoplace found only `fields_autoplaced` flags. NOT SOLVED |
| **Autoplace** | none (same search) |
| **Save** (`sch.save()`, `b.save()`) | works; wrote all sub-sheets and the `.kicad_dru` |
| PCB: open, `get_footprints` 63, `get_nets` 112, `get_zones` 1, `get_stackup` | work |
| PCB: `place_footprint_from_library` (`LED_THT:LED_D5.0mm`) | works, board then had 64 footprints |
| PCB: move a footprint by `UpdateItems` | applied (C1 x 110.49 to 111.49 in the saved raw text) but same client decode error |
| PCB: `RefillZones` | returned without error (result not checked against the file) |
| PCB: `Get/SetCustomDesignRules` | works and writes the `.kicad_dru`, see below |
| PCB: `ImportSpecctraSession` | exists ("Since: 11.0"; skips locked tracks and vias by design); with a missing file it returns `session file '...' could not be opened`. **A real SES import was not run** (no Java or Freerouting on this machine at the time) |
| `kipy.KiCad()` headless `find_kicad_cli` | without a path hint it picks the newest `KiCad/*/bin/kicad-cli.exe` under Program Files; always pass the path |

Answer to 168's open point: **the headless API can do place, field edit, save and board edits in this
nightly; annotate and autoplace are missing; the unmodified kipy client cannot be used as is (no release
for KiCad 11 yet, and the response decode bug).**

## Q3: kicad-cli, file formats, rules

kicad-cli help trees compared (MEASURED, `--help` of 20 command paths). New in the nightly: `api-server`,
`import`, `project downgrade`, `mergetool`, `gerber`, `pcb diff|downgrade|export bom|idf|png|specctra|stackup`,
`sch diff|downgrade|import|export png`, `fp diff|downgrade`, `sym diff|downgrade`. Unchanged: `pcb drc`,
`sch erc`, `jobset run`, `upgrade` commands. **Not present:** zone fill, annotate, route, panelize,
`pcb import specctra session` (the SES import is only in the API), a job to edit anything. Job registry in
source lists no job for routing, annotation or panelization.

**File formats** (MEASURED from files saved by the nightly; version table VERIFIED from
`pcbnew/pcb_io/kicad_sexpr/pcb_io_kicad_sexpr.h`):

- `.kicad_pcb` version `20261031` (10.0.7: `20260206`), `.kicad_sch` `20261106` (10.0.7: `20260101`),
  `.kicad_dru` `(version 2)` (we write and read `(version 1)`). 10.0.7 refuses a nightly-saved board:
  "Um die Datei zu öffnen, muss KiCad auf Version 10.99 oder später aktualisiert werden (Dateiformat mit
  Datum 20261031 oder später)" and a nightly-saved schematic: "Konnte Schaltplan nicht laden". No way back
  except the new `pcb|sch downgrade` commands (not run).
- **Footprint placement is no longer `(at x y a)`.** Every footprint (64 of 64) is written as
  `(transform (translate x y) (rotate a) (scale 1 1))` (source: version 20260616 "Footprint affine transform:
  lib-frame storage and (transform) block"; children now in library frame, `FIRST_FP_AFFINE_TRANSFORM`).
  Pad `(at ...)` angle of C1 (rotation 180) stayed 180 in this file; whether pad angles are still
  board-absolute in general was not isolated.
- Other format steps since 10.0 (header comments): extruded 3D body, ellipse, net chains, copper thieving,
  pad simulation types, geometric constraints, grid items, via stitching, line ending shapes, microvia
  stacks, drill charts, footprint overall height, linked placement rule areas, live stackup tables,
  non-component pads.

**Our reader and writer on the nightly-saved board (MEASURED, project venv, `src/boardproof`):**
`write_layer.load` and `raw_geometry.read_footprints|read_zones|read_board_outline|read_origins` all
return "OK", but **`read_footprints` returns wrong positions for 63 of 63 footprints** (the nearest `(at)`
is the Reference property's: C1 gives (12.5, -4.37) instead of (111.49, 78.867)). It fails open, the
worst kind (hard rule 2). `kicad_tools` 0.24.0 also loads the file and gives C1.position (-73.66, -40.64),
also wrong. So any move to KiCad 11 files needs the raw reader to learn `(transform ...)` and to refuse
unknown file versions.

**Schematic written by our tools is rejected by the nightly CLI.** The rung-1 schematic (version 20231120,
root `(sheet_instances (path "/<root uuid>" (page "1")))`) makes the nightly `sch erc` stop with
"Schematic '...' has a malformed root sheet instance path" (10.0.7 loads it). Source:
`eeschema/eeschema_helpers.cpp` 150-165, commit cdf88eb7a2 "Validate headless schematic roots": any file
newer than 20221110 whose screen keeps a non-empty sheet-instance list is refused; KiCad itself writes
`(path "/")`. The demo schematics (KiCad-written) load. Which of our writers emit the UUID form was not
audited beyond the rung-1 files.

**DRC and ERC comparison** (MEASURED; `--severity-all`, JSON, same input files, hash-identical demos):

- pic_programmer: DRC identical (41, all `lib_footprint_issues`); ERC 44 vs 47 (3 new
  `wired_implicit_power` warnings).
- 17 demo boards run (batch): DRC identical on 5 (ecc83, interf_u, pic_programmer, royalblue54L_feather,
  stickhub). The others differ by check changes and by library resolution: `text_thickness` became
  `line_width` (cm5_minima 27, microwave 8); `lib_footprint_mismatch` vanished where the nightly's library
  folder is not the table's (video 129, kit-dev 47: environment, not engine); new `copper_outside_board`
  (multichannel +45); `solder_mask_bridge` (+3), `silk_over_copper` up, `zones_intersect` down. ERC gained
  `bus_to_bus_conflict` errors (cm5 6, video 5, vme-wren 16, royalblue 2) and `no_connect_connected`,
  `pin_to_pin` changes. **The jetson demo lost all 357 `clearance` errors raised by its own custom rule
  `(hs_zone_clearance` (condition `A.NetClass == '*diff*' && B.Type == 'Zone' ...`) on identical files;
  the cause was not found.** It touches our rule-coverage facts (wildcard `==` on NetClass) and must be
  investigated before trusting any rule result on KiCad 11.
- rung-1 board (07_poured copy, board file read as is by the nightly): DRC identical on 10.0.7 and the
  nightly, same violation set (2 `silk_edge_clearance`, 12 `lib_footprint_issues`, 1 `silk_overlap`, 2
  `silk_over_copper`) and the same 30 unconnected items, compared item by item. The nightly ERC could not
  run on the rung-1 schematic (above).
- Custom rules written through the API (creepage 5 mm, clearance 0.9 mm, skew, length) give **identical
  counts on 10.0.7 and the nightly** (199 creepage capped, 109 skew, 36 clearance, 32 length), and 10.0.7
  reads the `(version 2)` rules file. So creepage, length, skew and clearance rules are **already
  native in 10.0.7**, not new in 11.

**Specctra.** `kicad-cli pcb export specctra` exists, 2.1 s on the demo. Against the 10.0.7 SWIG
`ExportSpecctraDSN` output (MEASURED): same components (29), padstacks (24), keepouts (4); the nightly's
`(wiring)` marks all unlocked existing tracks `(type protect)` (locked: `fix`, source
`specctra_export.cpp` ~1665; 10.0.7 wrote `route`), exports the GND zone as a `protect` polygon with fill
windows instead of a `(plane ...)` (commit 31220febea), and pad-stack names differ (`..._250.000000_um`
vs `..._250.951000_um`). DSN size 298 KB vs 141 KB. Source also lists: export keeps two points per wire
path to avoid Freerouting bugs (854010b7ac), per-wire session import with undo (34083f08a8), keepout fixes
(fa5dd14f47, 2ea8fb04a2). **Whether Freerouting 2.5.0 routes this DSN equally was not run.**

**Keepouts and slots.** Source-only: commit 2ea8fb04a2 (2026-11-12) skips rule areas that allow tracks
and vias; the slot-to-keepout export was not re-measured. Our DRC-blind-slot fact (copper wholly inside a
slot) was not re-measured on the nightly.

**kicad_dru syntax.** Version 2 written by the API; constraint types in the API proto (source) include
creepage, physical clearance, hole aspect ratio, microvia stack depth, net-chain length/stub/return path,
via count, diff-pair gap, skew, length (`board_rules.proto`, VERIFIED). Which are new in 11 versus 10.0 was
not diffed beyond the four measured.

## Table 1: problem today -> in the nightly

| Problem today | Status | Evidence |
|---|---|---|
| Schematic edit by KiCad itself, headless (issue 165/168): place symbol with fields and instance block | **Solved in build 6738** (API) | placed `R900` and `C?`, saved, ERC reloaded it |
| Edit fields headless | **Partly**: the server applies it; the available client cannot read the reply | `type_url` error, value read back changed |
| Annotation | **Not solved** | no command in API proto or CLI |
| Autoplace of symbols | **Not solved** | none |
| SWIG `pcbnew` removal breaks the DSN/SES path | **Partly**: `pcb export specctra` and API `ImportSpecctraSession` exist; export semantics changed (protect, zone polygons); SES import never run | DSN diff, source |
| KiKit/`panelize_board` depends on `pcbnew` | **Worse, unsolved**: no `pcbnew.py` in the nightly, no native panelization | no `PyInit__pcbnew`, no job |
| Live IPC needs a GUI (kipy 0.8.0 schematic read broken) | **Solved headless in source and measured**, but needs a kipy built for KiCad 11 | reads and writes worked |
| `kicad-cli` cannot edit | **Not solved** (api-server is the edit path; it is a server, not a job) | help tree |
| Zone fill not available in `kicad-cli` | **Partly**: API `RefillZones` (headless synchronous); CLI none | proto, call returned |
| Our reader/writer on new files | **Breaks** | transform, 63/63 wrong positions, sheet-instance reject |
| Verification gate unchanged | **Unknown/risky**: DRC engine differs on the jetson custom rule and new checks | DRC table |
| Rule syntax | **Compatible** for our four probes, `.kicad_dru` version 2 | measured |
| Downgrade path | **New**: `pcb|sch|project downgrade` | help, not run |

## Table 2: our features (built or planned) against the nightly

"Native" columns say where the nightly has an equivalent. Only rows marked MEASURED were run; the rest
are source or help-text readings (SOURCE) or not examined (UNKNOWN). Our value everywhere remains: batch,
checkpointed, self-checked use by an LLM, and a verdict from `verify_board`.

| Feature (issue) | Nightly equivalent | Verdict |
|---|---|---|
| Placement (place, align, distribute, pack, snap, autoplacement; edge anchoring 133; locks) | API `InteractiveMoveItems`, `UpdateItems`, `PlaceFootprintFromLibrary`; GUI placement tools; footprint autoplacer only for fields. No batch autoplacement in API or CLI (SOURCE) | still needed; could use API instead of the write layer later |
| Keepouts and slots (132) | file/GUI exist already in 10.0; API creates items; export to Specctra fixed (SOURCE) | still needed; re-measure DRC blindness |
| Clearance rules and coverage report (131) | `.kicad_dru` v2 and API `Get/SetCustomDesignRules`; same DRC results on 4 probes (MEASURED); no coverage report found | still needed; jetson rule difference open |
| Creepage (134, planned) | `creepage` constraint, same result in 10.0.7 (MEASURED) | already native in 10.0.7; our tool = rule authoring plus check |
| Length matching, skew, diff pairs (planned) | native constraints length, skew, diff_pair_gap in 10.0.7 DRC (MEASURED for length and skew); interactive tuning and net-chain length/skew/return-path checks are new (SOURCE, commits 2026-07..11) | thinner: write rules, let KiCad judge |
| Net chains (new in 11) | file format `net chain`, DRC check for tuning-profile consistency (SOURCE) | none planned yet |
| Trace width from current (planned) | none found in source titles | still needed |
| Height / 3D check (planned) | `physical_clearance` rules in 10.0, footprint overall height property and extruded body (SOURCE commit 674533a618, format 20261017) | thinner once heights are in files |
| Decoupling check (126) | none | still needed |
| Manufacturer rules in `.kicad_dru` (planned) | native rules file and API setter | thinner |
| Board outline | API items; no CLI | still needed |
| Pour and zone steps | API `RefillZones`; no CLI | still needed |
| Silkscreen minimums | DRC `silk_*` checks, `line_width` replaces `text_thickness` | thinner; wording sweep needs the rename |
| Schematic editing (symbols, wires, labels, sheets) | API: place symbol, items create/update, sheet hierarchy; no wire-routing help | thinner for placement, still needed for the rest |
| Import (EasyEDA etc.) | `kicad-cli import`, `sch import` (LTspice, EasyEDA, Altium, Eagle, CADSTAR, PADS, OrCAD...) (help) | partly redundant, not run |
| Annotation | none headless | still needed |
| ERC/DRC wrappers | same commands, new checks | still needed; version-pin results |
| Routing adapters (Freerouting, KRT) | `export specctra`, API SES import; no native autorouter (commit titles: only "attempt finish" in the router) | still needed; re-validate DSN semantics |
| Manufacturing export | CLI exports plus `bom`, `stackup`, `idf`, `png` | thinner |
| Panelize (KiKit) | none native; `pcbnew` Python removed | at risk |
| SPICE/simulation | ngspice 47 bundled; simulator in GUI | UNKNOWN |

## Recommendation for issue 168

**Wait for the release candidate, but unpark a bounded preparation now.** The headless place-symbol and
save path is real and works (that was the open point), so 168's direction is sound. It is not safe to
build on yet: no released KiCad 11, no released kipy for it, a client decode bug on every `UpdateItems`,
no annotate command, a new file format (`transform`, `.kicad_dru` v2, version numbers) that makes our raw
reader return wrong positions without an error, a stricter schematic loader that rejects our rung-1
schematic, and an unexplained DRC difference on a custom rule. Concrete follow-ups that can start now
without changing the pinned 10.0.7: (a) make `raw_geometry` and the write layer refuse unknown file versions
and learn `(transform)`; (b) audit our schematic writers for the `(sheet_instances (path "/<uuid>"))`
form; (c) investigate the jetson rule difference; (d) re-measure on a second nightly and on the first RC.

## Cost notes

Packaging: 0.9 GB download, 4.4 GB extracted, no install, separate config via `KICAD_CONFIG_HOME`; a
kipy for KiCad 11 must be built from git with the matching proto files (protoc via `grpcio-tools`,
`protoletariat`, `zstandard`; head version 0.9.0.dev0 pins protobuf <6, nng via pynng 0.9). Version
pinning: nightlies are unversioned beyond the build number and the commit. Breakages: `kicad_tools` 0.24.0
gives wrong convenience positions on the new format (it also does on 10.0.7, rule 2); our `raw_geometry`
fails open on `(transform)`.

## Not measured

Freerouting on the new DSN and any real SES import; KiKit/panelize; zone fill result against the file;
keepout and slot DRC behaviour; `pcb downgrade` and `sch upgrade` round trips; the full rung-1 DRC JSON
diff and the `verify_board` gate itself on the nightly; the jetson rule cause; unconnected-item parity;
GUI features (never started); lite build; arm64; a second nightly.
