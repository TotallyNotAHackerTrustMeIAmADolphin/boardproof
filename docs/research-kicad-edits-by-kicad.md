# Letting KiCad itself edit a schematic: IPC API, existing headless paths, a new kicad-cli job, a patched binary

Research note for ticket 165. Question: KiCad already places a library symbol with all its fields,
updates fields from the library, writes the instance block and annotates. `kicad-cli` cannot be told
to do it. How can KiCad do the schematic edit itself? Scope: reading source and docs plus a few
commands on the installed tool; no code changed. The KiCad facts about placement are in
`docs/research-kicad-symbol-placement.md` and are not repeated here.

## Sources, licence, tags

- KiCad `10.0` branch, head `86a7a3f4146f6a1630877865b3e9af4d5a2f5de2` (2026-11-05, version string
  `10.0.8-unknown`; tag `10.0.8` exists, dated 2026-10-29). Cloned shallow from
  `https://gitlab.com/kicad/code/kicad.git`. Paths below are relative to that repository.
- KiCad `master` (version string `10.99.0-unknown`, i.e. the development line that becomes KiCad 11; no
  `11.x` tag exists on 2026-11-10), read file by file through the GitLab raw-file API at head
  `4b753b460125b7f8fe90f19877d16dd04d913108` (2026-11-09). Quoted as "master".
- `kicad-python` (module `kipy`), head `76b47d25a826a56a301660f5f572dda968c84d59` (2026-11-08),
  cloned from `https://gitlab.com/kicad/code/kicad-python.git`. `pyproject.toml` line 8: `license = "MIT"`;
  `LICENSE`: "Permission is hereby granted, free of charge, to any person obtaining a copy of this
  software". No clause limiting automated or commercial use.
- KiCad itself: GPLv3 (`LICENSE`; `LICENSE.README`: "a combined works as is will be governed under the
  terms of the GPLv3 (or later). This includes any binary distribution of the KiCad EDA suite by the
  KiCad project or any third party"). Consequence for us: only as a subprocess or over the IPC socket,
  never imported (docs/ARCHITECTURE.md decision 6). GitLab terms of service not read (anonymous reads
  of a public repository and the public API only).
- Tags: **VERIFIED** = source path and lines read (quoted or closely paraphrased). **MEASURED** = ran the
  installed `kicad-cli` 10.0.7 or a shell command. **INFERRED** = my reading, not run. **DOCUMENTED** =
  publisher page read.
- Not done: nothing was built or run on master (no KiCad 11 binary exists here), so every statement
  about master behaviour is VERIFIED from source only, never MEASURED.

## Answers in short

1. **IPC API in KiCad 10.0: no.** It cannot place a symbol, set a field or save a schematic, and it
   needs the running GUI with the server switched on. Details in section 1.
2. **Headless in KiCad 11 (master): yes, in source.** `kicad-cli api-server` exists on master, and a
   `PlaceSymbolFromLibrary` command calls the same `SCH_SYMBOL` constructor as the GUI. It is not in any
   released KiCad. Section 2.
3. **No existing 10.0 headless path edits a schematic.** `kicad-cli sch` has `erc`, `export`, `upgrade`
   only; `upgrade` re-saves and changes no content. Section 3.
4. **A new 10.0 job is feasible without the GUI** (the job handler already loads a `SCHEMATIC` with no
   frame), about 250 to 450 lines in about 8 files for "place symbol", more for "update from library"
   because that logic lives inside a dialog class. Section 4.
5. **Recommendation:** keep rebuilding with the netlist oracle now; track KiCad 11's headless IPC as the
   one realistic route; do not maintain a patched binary. Section 6.

## 1. IPC API in KiCad 10.0

- Server is off by default. `common/settings/common_settings.cpp:480-481`:
  `new PARAM<bool>( "api.enable_server", &m_Api.enable_server, false )`. VERIFIED. The server constructor
  returns at once when it is off ("Server: disabled by user preferences", `common/api/api_server.cpp:58-62`).
  VERIFIED.
- Only an editor frame registers a handler: `eeschema/sch_edit_frame.cpp:464-465`
  (`m_apiHandler = std::make_unique<API_HANDLER_SCH>( this ); Pgm().GetApiServer().RegisterHandler(...)`).
  `API_HANDLER_SCH` takes a `SCH_EDIT_FRAME*` (`eeschema/api/api_handler_sch.cpp:37-38`). So the schematic
  API exists only while the GUI schematic editor runs. VERIFIED.
- `kicad-cli` 10.0 has no server command. MEASURED:
  `kicad-cli api-server` prints `Failed to parse 'api-server', did you mean 'jobset'` and lists
  `{fp,jobset,pcb,sch,sym,version}`. The official page agrees: "The IPC API is an interface that can be
  used to remotely control a running instance of KiCad" (`dev-docs.kicad.org/en/apis-and-binding/ipc-api/`,
  DOCUMENTED).
- What the 10.0 schematic handler can do: `API_HANDLER_SCH` registers one command of its own,
  `GetOpenDocuments` (`api_handler_sch.cpp:40-41`); the shared editor base adds only BeginCommit,
  EndCommit, CreateItems, UpdateItems, DeleteItems and HitTest (`common/api/api_handler_editor.cpp:38-43`).
  VERIFIED. There is no save command for a schematic, so even a successful edit cannot reach the file.
- Item types: the schematic proto has only `Line`, `Text`, `LocalLabel`, `GlobalLabel`,
  `HierarchicalLabel`, `DirectiveLabel` (`api/proto/schematic/schematic_types.proto`, 75 lines).
  `SCH_SYMBOL` has no `Serialize`/`Deserialize` (grep over `eeschema/sch_symbol.h` and `.cpp`: 0 hits), and
  the base `SERIALIZABLE::Serialize` is `wxASSERT_MSG( false, "Serialize called on an object that doesn't
  implement it!" )` (`common/api/serializable.cpp:24-27`). `DeleteItems` is `// TODO`
  (`api_handler_sch.cpp:~245-250`). VERIFIED.
- `kicad-python` README says the same: "the IPC API requires communication with a running instance of
  KiCad. It is not possible to use `kicad-python` to manipulate KiCad design files without KiCad running."
  VERIFIED (`README.md`). Its schematic classes are labelled `versionadded:: 0.x.y (KiCad 11)`
  (`kipy/schematic.py:106`, `:617`). VERIFIED.

Answer to question 1: in 10.0 no schematic editing, GUI required, no headless mode. Removes 0 cause classes.

## 2. KiCad 11 (master): headless API server and symbol placement

All VERIFIED from master source; none MEASURED.

- `kicad/cli/command_api_server.cpp`: `COMMAND( "api-server" )`, description "Run the KiCad IPC API
  server in headless mode", optional argument "Optional path to a .kicad_pro, .kicad_pcb, or .kicad_sch
  file to pre-load", `--socket`. It constructs `KICAD_API_SERVER( false )` and loads the schematic face
  through the KIWAY.
- `kicad-python` README: "As of KiCad 11, it is possible to start a headless API server using
  `kicad-cli api-server`. `kicad-python` can do this for you automatically when creating a `KiCad`
  object." (`kipy/server.py:169` builds `[kicad_cli, "api-server", "--socket", path]`.)
- `api/proto/schematic/schematic_commands.proto` (master): `PlaceSymbolFromLibrary`, "Since: 11.0", with
  `lib_id`, `position`, optional `orientation`, optional `unit`, optional `reference`.
- `eeschema/api/api_handler_sch.cpp` (master) `handlePlaceSymbolFromLibrary` (lines 3206-3349):
  loads the library symbol through `PROJECT_SCH::SymbolLibAdapter( &project() )->LoadSymbol`, then
  `std::make_unique<SCH_SYMBOL>( *libSymbol, libId, &targetPath, unit, 0, position )` (the same
  constructor the GUI tool uses, so field copy from the library, flattened library copy and instance
  block are KiCad's own), `BaseRefFromUser` + `SetRef` when a reference is given, otherwise annotation via
  `AnnotateNewSymbol` only when `AlwaysAnnotate()` (power symbols) or, with a frame, the GUI preference;
  `AutoplaceFields` only `if( SCH_EDIT_FRAME* frame = this->frame() )` and the setting is on. In
  headless mode `frame()` is null, so fields are NOT autoplaced and a symbol without `reference` stays
  `R?`. Handler list on master: 40 `registerHandler` calls, including SaveDocument, SaveCopyOfDocument,
  SaveDocumentAs, GetItems, GetSchematicNetlist, GetSchematicHierarchy, StartErcTask, variants, and
  the job runners (export netlist, BOM, PDF, ...). There is no "update symbol from library" or "set field"
  command; field edits go through `UpdateItems` with a `SchematicSymbolInstance` (proto fields
  `reference_field`, `value_field`, `footprint_field`, `datasheet_field`, `description_field`).
- Caveat in the client docs: "Requires that libraries be loaded, which will not be the case in headless
  mode until `load_all_libraries` has been called" (`kipy/schematic.py`).
- Release state: master is `10.99.0`; no KiCad 11 release or release candidate tag exists on
  2026-11-10 (tags list ends at `10.0.8`). The release date is not stated in any source I read
  (ASSUMED: not before 2027).

## 3. Existing headless paths in 10.0

- `kicad-cli sch` (MEASURED, `kicad-cli sch --help` on 10.0.7): subcommands `erc`, `export`, `upgrade`.
  `kicad-cli sch upgrade --help`: "Upgrade the schematic file's format to the latest one", option
  `--force` "Forces the schematic file to be resaved regardless of versioning".
- `JobUpgrade` (`eeschema/eeschema_jobs_handler.cpp:1386-1431`) loads with `LoadSchematicFile` and
  writes with `SaveSchematicFile`; it changes no item. VERIFIED. Registered jobs of the schematic face:
  `bom`, `pythonbom`, `netlist`, `plot`, `symupgrade`, `symsvg`, `erc`, `upgrade` (`Register(` lines
  90-167). All read-only except the two upgrades. VERIFIED.
- Python scripting: the SWIG module exists for the PCB only. `grep -rn "%module" --include=*.i` finds
  `pcbnew/python/swig/pcbnew.i` and nothing in `eeschema/`. VERIFIED. (An upstream issue says the SWIG
  route was removed on master: issue 25847, title "kicad-cli and the API have no Specctra DSN export or
  SES import on master (the SWIG route was remove...", DOCUMENTED from the title.)
- Open upstream requests are for other edits, none for symbol placement or field update by CLI:
  issue 25665 "kicad-cli: create and edit design variants" (open; labels `10.0`, `feature-request`,
  `priority::wishlist`, `status::out-of-scope`; its text: "Master has variant commands in the IPC API,
  but the API needs a running editor"), issue 25728 "kicad-cli: add a command to Update PCB from
  Schematic" (open, `status::new`), issue 22955 "add kicad-cli command to upgrade a project" (open).
  DOCUMENTED (GitLab issue API, title/labels/description). I found no issue or merge request asking for
  a CLI job that places symbols or updates fields from the library. Searches used: "headless",
  "kicad-cli sch update", "kicad-cli schematic edit", "IPC API schematic", "api-server",
  "update symbols from library cli". The issue comments need a login (HTTP 401), so I could not read
  maintainer replies. Merge request 2713 "bring the schematic IPC surface to parity with pcbnew" (target
  master, closed unmerged 2026-08-02) and 2332 "interactive symbol placement through the schematic IPC
  API" (closed) are related history; their discussion was not read.

## 4. What a new 10.0 job would depend on, and its size

### 4.1 Classes

- The job handler needs no frame. `EESCHEMA_JOBS_HANDLER::getSchematic` (`eeschema_jobs_handler.cpp:
  175-213`) uses `Pgm().IsGUI()` to choose; in CLI mode it calls `EESCHEMA_HELPERS::LoadSchematic(
  schPath, true, false, &project )` and keeps a `SCHEMATIC*`. VERIFIED.
- Placement path in the GUI (`eeschema/tools/sch_drawing_tools.cpp:445-470`, from the earlier note) mixes
  model calls and frame calls. Model classes, no frame: `SCH_SYMBOL` constructor and `UpdateFields`
  (`sch_symbol.cpp:81-115`, `1414-1477`), `SCH_SYMBOL::SetRef`, `SCH_REFERENCE`, `SCH_REFERENCE_LIST::
  ReannotateByOptions` and `UpdateAnnotation` (`eeschema/sch_reference_list.cpp`), `SCH_SCREEN`,
  `SCH_SHEET_LIST`, `SYMBOL_LIBRARY_ADAPTER`. Frame-bound: the tool wrapper `SCH_DRAWING_TOOLS::
  PlaceSymbol`, `m_frame->eeconfig()` (the auto-annotate and autoplace preferences), the `SCH_COMMIT`
  undo stack, and the lambda in `sch_drawing_tools.cpp:~223-252` that calls the model methods above.
  VERIFIED for the call sites; INFERRED that the model calls work without a frame (master's headless
  handler does exactly that, section 2, which is the evidence).
- `AutoplaceFields` (`autoplace_fields.cpp`) is a model-level class (`AUTOPLACER`) taking a screen.
  INFERRED frame-free; master calls it only with a frame, so headless master leaves fields unplaced.
- "Update symbols from library" is a dialog method: `DIALOG_CHANGE_SYMBOLS::processSymbols`
  (`dialogs/dialog_change_symbols.cpp:573-837`, about 265 lines) reads wx widget state (`m_fieldsBox`,
  `m_removeExtraBox`, `m_reset*` booleans) and gets `SCH_EDIT_FRAME*` through `GetParent()`
  (lines 281, 432, 467, 508, 579). Reusing it in a job means moving that logic into a free function
  or class taking a plain option struct, then having the dialog call it. VERIFIED (call sites);
  the refactor size is INFERRED.
- Write-out: `pi->SaveSchematicFile` as `JobUpgrade` does. VERIFIED.

### 4.2 Size of a patch (INFERRED from the nearest existing job)

Reference: the upgrade job is `common/jobs/job_sch_upgrade.{h,cpp}` 35+27 lines, `kicad/cli/
command_sch_upgrade.{h,cpp}` 36+54, plus an entry in `common/CMakeLists.txt:168`,
`kicad/CMakeLists.txt:78` and the handler body (46 lines) with one `Register(` line. MEASURED by `wc -l`:
netlist export, a richer job, is 94+62+104+37 = 297 lines in the four job/command files.
Estimate for `kicad-cli sch place-symbol` (many symbols per call from a JSON spec, field copy by the
existing constructor, reference given or annotated, save): 4 new files (~200 lines), 4 edited files
(CMake x2, handler header and body, ~100 lines): about 300 lines in 8 files. Add "update from library"
by extracting `processSymbols` into a UI-free function: 300 to 400 lines moved or changed in 2 files,
plus tests (`qa/` has schematic fixtures; ~150 lines). Total estimate 450 to 850 lines, 10 to 14 files.
The estimate is not a measurement.

## 5. Distribution cost

- Binaries to replace (INFERRED from where the classes live): job classes sit in `common/jobs/` and go
  into `kicommon` (`common/CMakeLists.txt:168`), the handler into the schematic face, the command into
  `kicad-cli`. MEASURED on the installed Windows 10.0.7: `bin/kicad-cli.exe`, `bin/kicommon.dll` and
  `bin/_eeschema.dll` exist; the whole install is 4.6 GB (`du -sh`, 235 files in `bin/`). A patched build
  must replace the three together, built from the same source commit.
- Build: needs KiCad's full dependency set (`vcpkg.json` at the repo root lists them: wxWidgets, Boost,
  OpenCASCADE and others), ASSUMED hours of build time and tens of GB of disk; not measured here.
  Platforms: one build each for Windows, Linux, macOS if we ship all three.
- Version drift: every 10.0.x point release (10.0.7 on 2026-10-06, 10.0.8 on 2026-10-29: tag dates)
  needs a rebuild or a rebase of the patch, and `.kicad_sch` version numbers move with major
  versions (`SEXPR_SCHEMATIC_FILE_VERSION`, 20260306 on 10.0). Our users have the stock install; a
  patched `kicad-cli` is a second KiCad on the machine.
- Licence: GPLv3; shipping a modified binary is conveying it and obliges us to offer the source.
  Calling it as a subprocess keeps the rest of our code out of the GPL scope (decision 6).
- Upstream route: `dev-docs.kicad.org/en/rules-guidelines/release-policy/` (DOCUMENTED): "Features from
  the current development branch of KiCad can be back ported to the current stable branch", only after
  the feature's last commit is at least 60 days old and its last known issue at least 30 days old;
  bug fixes only on the current stable release. So a new job goes to master first, and reaches 10.x only
  by backport. `CONTRIBUTING.md` (VERIFIED): "Developing any larger change, such as a new feature, should
  be discussed on the developers mailing list before substantial work is done."
  Tool-Generated Content Policy (`.../tool-generated-content/`, DOCUMENTED): "There must be a human in
  the loop", disclose with an `Assisted-by: <tool name>` trailer, "Using AI tools to generate text for
  humans to read is not permitted", "Documentation and translations must be fully human-written", and
  "AI tools must not be used to fix issues labelled `starter`". An agent cannot file or discuss the
  change by itself; a human has to own it.
- Master already has the headless route (section 2), and a maintainer-labelled
  `status::out-of-scope` on the variants CLI request points the same way: upstream steers CLI-less
  editing to the IPC server. A separate `sch place-symbol` job on master is therefore unlikely to be
  accepted (INFERRED from 25665's labels; no maintainer comment could be read).

## 6. Recommendation and cause classes removed

Cause classes (my list, from tickets 120, 162, 163 and the placement note; each was a source of an
emulation or refusal rule in our write layer):

| Id | Class | Ticket |
|----|-------|--------|
| A | Library fields copied into the instance at placement (Footprint, Datasheet, Description, user fields, hidden state) | 162 |
| B | Field positions and autoplace | 162 |
| C | Instance block, `R?` reference, annotation | 162 |
| D | Whitespace trim of mandatory fields | 120 |
| E | `"~"` placeholder meaning | 120 |
| F | Text variables (`${REFERENCE}`) | 120 |
| G | Field-name case folding (`footprint` to `Footprint`) | 120 |
| H | Multi-unit and reused-sheet field choice ("not exactly one component") | 120, 163 |
| I | What the netlist shows versus the file (DNP, off-board, power skipped) | 163 |
| J | Update-from-library semantics (what overwrites what) | 162 (replace_symbols) |

"Removed" means: we no longer have to reproduce KiCad's rule, because KiCad's own code produces the
result; the netlist check stays in every option as the oracle.

| Option | Removes | Classes | Cost |
|--------|---------|---------|------|
| IPC API on 10.0 | 0 of 10 | none | impossible: GUI plus enabled server, no symbol item, no save (section 1) |
| Headless IPC on KiCad 11 (`kicad-cli api-server` + `PlaceSymbolFromLibrary` + `UpdateItems` + `SaveDocument`) | 4 of 10 | A, C; D and G by delegation (KiCad's `SetText`/loader applies them to the values we send) | needs KiCad 11 on the user's machine; B stays (headless does not autoplace); J has no command; E, F, H, I are properties of the netlist and stay |
| New 10.0 job (place + update, patched binary) | 6 of 10 | A, B, C, D, G, J (J only if `processSymbols` is extracted) | 450 to 850 lines, three rebuilt binaries per platform per point release |
| Upstream job | same 6 as the patched job | same | not before KiCad 11 and 60 days; unlikely accepted given the headless IPC server on master; needs a human owner and the mailing list |
| Keep rebuilding with the netlist check | 0 of 10 | none | what we run today; ticket 120's differential run: 378 cases, 353 ok and followed, 0 ok and not followed |

E, F, H and I are not removed by any option, because they describe how the netlist reads the file, not
how the file is written. They need the oracle regardless. The counts are my reading (INFERRED), not
measured.

**Recommendation.**
1. Now: keep rebuilding with the netlist check. It is the only option that works on the KiCad users
   have (10.0.x), and its false-accept rate on 378 cases is 0.
2. Plan a second backend behind the existing sourcing/write-layer seam for KiCad 11: spawn
   `kicad-cli api-server --socket <path>` as a subprocess and talk to it with `kicad-python` (MIT) over
   the socket (arm's length from the GPL). That would retire classes A, C, D, G for boards on KiCad 11.
   First step when a KiCad 11 release candidate exists: MEASURE the three calls (`PlaceSymbolFromLibrary`
   with a project library, `UpdateItems` on the fields, `SaveDocument`) on a scratch copy and compare
   the saved file and the `kicad-cli` netlist with what our writer produces for the same input.
3. Do not maintain a patched `kicad-cli`: the cost (three binaries, every platform, every point
   release) is higher than the 4 to 6 rules it retires, and the same capability arrives in KiCad 11.
4. Do not open an upstream request for a CLI placement job; at most comment on the headless-IPC route
   once a human decides to engage with the project under its tool-generated-content policy.

## Open points

- Nothing about KiCad 11 behaviour was run; the `PlaceSymbolFromLibrary` and headless claims are source
  reading of master at one commit and may change before release.
- Whether `UpdateItems` on a `SchematicSymbolInstance` (symbol fields, hidden state, user fields) works
  headless was not traced to the end (`ApplySymbolInstance`, `ValidateSymbolInstance`); only the
  placement command was read in full.
- Build time, disk and CI cost of a patched KiCad build are ASSUMED, not measured.
- Maintainer comments on issue 25665 and the closed merge requests 2713 and 2332 need a login or were
  not read; the "unlikely accepted" judgement rests on labels.
- The class counts depend on my list of ten classes; a different split gives different counts.
