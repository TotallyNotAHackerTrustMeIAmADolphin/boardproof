# KiCad 10: symbol placement, instance data, netlist field choice, Update Symbol, save rules

Research note. Question: what does the KiCad 10 source do when a symbol is placed from a library, how
does the netlist exporter read fields, what does "Change/Update Symbols" do with fields, and which
save rules matter for a writer. Scope: reading and one measurement round; no code changed.

## Sources, licence, evidence tags

- Source: KiCad `10.0` branch, head commit `86a7a3f4146f6a1630877865b3e9af4d5a2f5de2` (committer date
  2026-11-05), fetched with `git clone --depth 1 --branch 10.0 https://gitlab.com/kicad/code/kicad.git`.
  Every path below is relative to that repository; line numbers are those of the head commit. Link form:
  `https://gitlab.com/kicad/code/kicad/-/blob/86a7a3f4146f6a1630877865b3e9af4d5a2f5de2/<path>#L<from>-L<to>`.
- Tag `10.0.7` (the installed `kicad-cli` reports `10.0.7`) was fetched too. `git diff 10.0.7 HEAD` over the
  files cited here (`sch_symbol.cpp`, `sch_field.cpp`, `netlist_exporter_xml.cpp`,
  `netlist_exporter_base.cpp`, `dialog_change_symbols.cpp`, `sch_io_kicad_sexpr.cpp`,
  `sch_io_kicad_sexpr_parser.cpp`) shows changes only in `sch_symbol.cpp` and `sch_field.cpp`, and those
  are trace-macro renames plus an instance-index refactor of `RemoveInstance`. All logic cited is the
  same at `10.0.7`. VERIFIED (diff output read).
- Licence: `LICENSE` is the GNU GPL version 3; `LICENSE.README` says "The majority of KiCad's source code
  is developed and distributed under the terms of the GPLv3 or later". No clause limiting automated
  reading or commercial use of the behaviour was found in the repository. The GitLab terms of service were
  not read (ASSUMED fine for an anonymous `git clone` of a public repository). This note quotes only short
  fragments and paraphrases; no KiCad code is copied or imported, in line with docs/ARCHITECTURE.md decision 6.
- Tags: **VERIFIED** = read in the source named and quoted or closely paraphrased. **MEASURED** = ran the
  real `kicad-cli 10.0.7` on a scratch copy of a stock demo (no file edited by hand). **INFERRED** = my
  reading of what the code does, not run.

## Verdict on the five known facts

| # | Known fact | Verdict |
|---|-----------|---------|
| 1 | Netlist reads the lowest unit's property copy on one sheet | **Partly confirmed, with a hole.** For multi-unit parts the exporter scavenges across all units of the same reference (all sheets), preferring a lower unit but accepting the first non-empty value. It is not "the lowest unit's copy", and the result can depend on item iteration order (3.2). Single-unit parts read their own field. |
| 2 | Edge whitespace is trimmed for Value/Datasheet/Description/Footprint | **Confirmed** (also Reference). Trimming is in `SCH_FIELD::SetText` for every mandatory field, so it applies on load and on every edit. User fields are not trimmed. Variant overrides are stored untrimmed. |
| 3 | `"~"` becomes empty | **Contradicted for current files.** Since file version 20250318 "~ no longer means empty text". Only files older than that get `"~"` mapped to empty on load. The XML exporter itself writes `"~"` for an empty Value. |
| 4 | `${REFERENCE}` expands | **Confirmed.** Expands to the sheet-path reference including the unit letter for multi-unit parts (`U1A`). Name match is case-insensitive. A field whose whole text is `${ITSELF}` is left unresolved. |
| 5 | A lowercase `footprint` field folds into the standard field | **Confirmed** on load (symbols and library symbols). It folds into `Footprint` and is saved back as `Footprint`. Only mandatory names fold; user-field names are case-sensitive. |

## 1. Placing a symbol: `SCH_SYMBOL(LIB_SYMBOL, ...)` and field copy

### 1.1 Call path (VERIFIED)

- `eeschema/tools/sch_drawing_tools.cpp:445-448`: `symbol = new SCH_SYMBOL( *libSymbol,
  &m_frame->GetCurrentSheet(), sel, cursorPos, &m_frame->Schematic() ); addSymbol( symbol ); annotate();`
  (tool function `SCH_DRAWING_TOOLS::PlaceSymbol`). Then lines 455-459: if
  `m_AutoplaceFields.enable`, `symbol->AutoplaceFields( nullptr, AUTOPLACE_AUTO )`; at click
  (lines 467-470) the same with the screen. The setting defaults to `true`
  (`eeschema/eeschema_settings.cpp:327-328`, `autoplace_fields.enable`, default `true`).
- The second constructor (`sch_symbol.cpp:118-130`) delegates to the first, then applies fields modified
  in the chooser (`aSel.Fields`): Reference through `SetRef( aSheet, value )`, others through
  `field->SetText( value )`.

### 1.2 The constructor (`eeschema/sch_symbol.cpp:81-115`, VERIFIED)

1. `Init( aPosition )` (lines 178-204) creates exactly the five mandatory fields, in order Reference,
   Value, Footprint, Datasheet, Description, all with empty text, all placed at the symbol position, all
   visible (`SCH_FIELD` ctor, `sch_field.cpp:147`: `SetVisible( true )`). Default prefix `U`,
   `m_isInNetlist = true`.
2. Sets `m_unit = aUnit`, `m_bodyStyle`, `m_lib_id`.
3. `part = aSymbol.Flatten(); SetLibSymbol( part.release() )`: the instance keeps its own flattened copy of
   the library symbol (derived symbols are resolved here). This is the copy that goes into the
   `lib_symbols` cache on save.
4. `UpdateFields( aSheet, true /*style*/, false /*update ref*/, false /*update other*/, true /*reset ref*/,
   true /*reset other*/ )` (lines 98-102).
5. `m_prefix = GetRefDesPrefix( m_part->GetReferenceField().GetText() )`; if `aSheet`,
   `SetRef( aSheet, GetRefDesUnannotated( m_prefix ) )` (lines 104-107), which is the `R?` reference.
6. Inherits the four exclude flags (sim, BOM, board, pos files) from the flattened library symbol
   (lines 111-114). DNP is not inherited (stays default false).

### 1.3 `UpdateFields` as the constructor calls it (`sch_symbol.cpp:1414-1477`, VERIFIED)

For each library field (`m_part->GetFields`):

- Mandatory library field: target is the symbol's field with the same id. Non-mandatory: target is looked up by
  `GetCanonicalName()`; if absent, a new `FIELD_T::USER` field is added, `ImportValues( *libField )` and
  `SetTextPos( m_pos + libField->GetTextPos() )` (lines 1434-1441).
- Always: `schField->SetPrivate( libField->IsPrivate() )`.
- `aUpdateStyle` true: `schField->ImportValues( *libField )` plus `SetTextPos( m_pos + libField->GetTextPos() )`.
  `ImportValues` (`sch_field.cpp:373-379`) copies text attributes (`SetAttributes`: font, size, justification,
  angle, bold, italic), **visibility** (`SetVisible`), show-name and can-autoplace. So **hidden state is copied
  from the library field**, and the field offset is the library offset added to the symbol position.
- Reference with a path: `aResetRef` true gives `SetRef( aPath, m_part->GetField( REFERENCE )->GetText() )`,
  that is the library reference text such as `R` or `U` becomes the instance reference, then the constructor
  overrides it with `R?` (step 5).
- Value: `SetValueFieldText( UnescapeString( libField->GetText() ) )`, always, regardless of the flags.
- Datasheet: with `aResetOtherFields` the text is `GetDatasheet()`, the library symbol's datasheet text.
- Every other field (Footprint, Description, user fields): with `aResetOtherFields || aUpdateOtherFields` the
  text is copied from the library field. So at placement **Footprint, Description and all user fields are
  copied from the library symbol**, including empty ones.
- Text is stored through `SCH_FIELD::SetText`, so mandatory fields are whitespace-trimmed (section 3.4).

### 1.4 Field positions, rotation and mirror (VERIFIED, one INFERRED)

- At construction the transform is the identity, so a field is at `symbol position + library offset` with the
  library angle (`sch_symbol.cpp:1448-1449`). The library offset is not rotated, because nothing is rotated yet.
- `SCH_SYMBOL::Rotate` (`sch_symbol.cpp:2862-2876`) rotates the symbol position about the centre and moves each
  field by the same delta as the symbol; it does not rotate the field's own offset. `MirrorHorizontally`/
  `MirrorVertically` (2826-2860) do the same (translate by the symbol's move). The per-field rotation is the
  separate `SCH_FIELD::Rotate` (`sch_field.cpp:835-880`), which toggles horizontal/vertical text angle and
  justification.
- The tidy position comes from `AutoplaceFields` (`eeschema/autoplace_fields.cpp:768-782`,
  `AUTOPLACER::DoAutoplace`), which skips fields that are not visible or have `CanAutoplace()` false
  (lines 154 and 202) and uses `GetTransform()` (line 121) to choose the text angle. It sets
  `m_fieldsAutoplaced` to `AUTOPLACE_AUTO`, which the writer saves as `(fields_autoplaced yes)`.
- INFERRED: a writer that computes field positions itself should treat the position as an absolute
  schematic coordinate (what `(at x y angle)` in `property` holds), not as an offset; and the offsets of an
  already rotated symbol cannot be derived from the library offset by a plain rotation, because KiCad itself
  does not do that (it autoplaces).

### 1.5 `${...}` variables at placement (VERIFIED)

Text is copied raw. Nothing is expanded at placement; `${...}` stays in the field text and is resolved when
displayed or exported (`SCH_FIELD::GetShownText`, `sch_field.cpp:240-259`: `if( HasTextVars() ... ) text =
ResolveText( text, aPath, aDepth )`). Symbol-level tokens are resolved in `SCH_SYMBOL::ResolveTextVar`
(`sch_symbol.cpp:1852-2090`): field names (matched with `IsSameAs( fieldName, false )`, so case-insensitive,
lines 1948-1975), `REFERENCE` gives `GetRef( aPath, true )` (with unit), `UNIT`, `SHORT_REFERENCE`
(`GetRef( aPath, false )`), `FOOTPRINT_LIBRARY`, `FOOTPRINT_NAME`, `SYMBOL_LIBRARY`, `SYMBOL_NAME`,
`SYMBOL_DESCRIPTION`, `SYMBOL_KEYWORDS`, `EXCLUDE_FROM_*`, `DNP`, `OP...`.
A template field name that the symbol does not have resolves to the empty string (lines 1992-2003).

## 2. The instance block

### 2.1 Creation (VERIFIED)

- `SetRef( aSheet, "R?" )` (`sch_symbol.cpp:708-731`): looks up `m_instancePathIndex[ sheet->Path() ]`; if
  none, `AddHierarchicalReference( path, ref, m_unit )` (610-644) which creates a `SCH_SYMBOL_INSTANCE`
  with `m_Path`, `m_Reference`, `m_Unit`; the first instance also sets the Reference field text and `m_unit`.
  `SetRef` also updates `m_prefix` (default `U` if empty) and `m_isInNetlist = !ref.StartsWith( "#" )`.
- `annotate()` in the placement tool (`sch_drawing_tools.cpp:223-252`) then runs
  `ReannotateByOptions` and `UpdateAnnotation` only if `m_AnnotatePanel.automatic` or the symbol is
  "always annotate" (power symbols); otherwise the reference stays `R?`.
- Unannotated reference text is `GetRefDesUnannotated( prefix )` = `GetRefDesPrefix( x ) + "?"`
  (`common/refdes_utils.cpp:35-51`); the prefix is the string with trailing digits and `?` removed.
- `GetRef` (`sch_symbol.cpp:647-689`): instance reference for the sheet path; if empty, the Reference field
  text; if still empty, `prefix + "?"`. With `aIncludeUnit` and a multi-unit library symbol the unit letter is
  appended (`SubReference( unit )`). `IsAnnotated` (839-850) is true only if the instance reference is
  non-empty and its last character is not `?`.
- Unit: the instance unit is `m_unit` at the time of `SetRef`; per-sheet unit is `GetUnitSelection( sheet )`
  (912-922), which falls back to the top-level `m_unit` when the path has no instance.

### 2.2 What is written (`eeschema/sch_io/kicad_sexpr/sch_io_kicad_sexpr.cpp:680-1006`, VERIFIED)

- Top level: `(lib_id ..) (at x y angle) [(mirror x|y)] (unit N) (body_style N) (exclude_from_sim ..)
  (in_bom ..) (on_board ..) (in_pos_files ..) (dnp ..) [(fields_autoplaced yes)] (uuid ..)`, then the
  `property` blocks, then `pin` blocks, then `instances`. The top-level `(unit N)` and the Reference
  property are always the **ordinal** instance (first sheet path), "to prevent file churn" (lines 737-762,
  796-801); `ordinalInstance.m_Reference` starts as `aSymbol->GetPrefix()`.
- `(instances (project "<name>" (path "/<sheet uuid>[/..]" (reference "R?") (unit 1) [(variant ..)])))`
  (lines 848-1003). One `project` per top-level sheet group, `path` entries sorted by path. Instances whose
  path no longer exists in the hierarchy are dropped on save when they belong to this project
  (lines 906-915). The project name is the real project name for the current project
  (`m_schematic->Project().GetProjectName()`, line 939).
- Instance `value` and `footprint` are no longer written (file version 20221126, "Remove value and footprint
  from instance data", `eeschema/sch_file_versions.h`), but the parser still reads them (4.1).
- MEASURED: stock demo `interf_u` after `kicad-cli sch upgrade --force`:
  `(instances (project "interf_u" (path "/092cfb52-b6f1-4355-8e6c-0f835c9fcaf7" (reference "BUS1") (unit 1))))`.
- Multi-unit parts: each unit is its own `(symbol ..)` with its own `(unit N)`, `uuid`, properties and its own
  `instances` entry carrying the same reference and `(unit N)`. A unit's pins are the pins of that unit only
  (INFERRED from `SCH_SYMBOL::UpdatePins` and the netlist unit collection; not measured).

## 3. Netlist export: how field values are picked

### 3.1 Entry points (VERIFIED)

`kicad/cli/command_sch_export_netlist.cpp` registers `--format` with `kicadsexpr` (default), `kicadxml`,
`cadstar`, `orcadpcb2`, `spice`, `spicemodel`, `pads`, `allegro`. `eeschema/eeschema_jobs_handler.cpp`
(`JobExportNetlist`, lines 425-470) warns on annotation errors and duplicate sheet names (it does not
stop), then builds `NETLIST_EXPORTER_KICAD` (kicadsexpr) or `NETLIST_EXPORTER_XML` (kicadxml).
`NETLIST_EXPORTER_KICAD : public NETLIST_EXPORTER_XML` (`netlist_exporter_kicad.h:37`) and calls the same
`makeRoot( GNL_ALL | GNL_OPT_KICAD )`, so both formats share `addSymbolFields`. `NETLIST_EXPORTER_XML`
sets `m_resolveTextVars( true )` in its constructor (`netlist_exporter_xml.h:66`), so text variables are
**expanded** in both formats.

### 3.2 `addSymbolFields` (`eeschema/netlist_exporters/netlist_exporter_xml.cpp:111-255`, VERIFIED)

Which symbol object is exported (`makeSymbols`, 258-330):
- For each sheet path in the hierarchy, symbols are put in an ordered set keyed by `GetRef( &sheet, false )`
  (no unit). When two units share a reference the one with the **smaller UUID** is kept as the exported
  representative and the others go to `extra_units` (lines 285-306). So the representative is the
  lowest-UUID unit, not the lowest unit number.
- `findNextSymbol` (`netlist_exporter_base.cpp:82-120`) skips references starting with `#` and, for
  multi-unit library symbols, skips a reference already seen (`m_referencesAlreadyFound`).
- A symbol is skipped for the board netlist when its sheet or itself is excluded from board (lines 317-321).

Single-unit library symbol (`GetUnitCount() == 1`, lines 195-225): Value = `aSymbol->GetValue( resolve,
&aSheet, false )`, Footprint = `GetFootprintFieldText( resolve, &aSheet, false )`, Datasheet and Description
from the field `GetShownText( &aSheet, false )`; every non-mandatory, non-private field is exported with
`GetShownText` (private fields are skipped). The field values are those of the one `SCH_SYMBOL` object. A sheet
used twice is one screen, hence one `SCH_SYMBOL` object, so the **Value/Footprint/user fields are identical
for both sheet instances**; only the Reference (instance path) differs, and text variables like
`${REFERENCE}` are resolved per sheet path. Per-instance differences need a design variant
(section 3.6).

Multi-unit library symbol (lines 122-194): comment in the source, quoted: "this block finds the unit with
the lowest number having a non blank field value and records it ... the user is best off setting fields
into only the first unit. But this scavenger algorithm will find any non blank fields in all units and use
the first non-blank field for each unique field name."
Mechanics:
1. `minUnit` starts as the unit of the representative on `aSheet`.
2. Loop over **every sheet path** and every symbol on its screen; keep those whose `GetRef( &sheet )` equals
   the reference case-insensitively (`CmpNoCase`).
3. For Value, Footprint, Datasheet, Description: `if( !candidate.IsEmpty() && ( unit < minUnit ||
   value.IsEmpty() ) ) value = candidate;`
4. For each non-mandatory, non-private field name: taken when `unit < minUnit || fields.count( name ) == 0`
   (no emptiness test: the first-seen copy wins even if blank, a lower unit overrides it).
5. `minUnit = std::min( unit, minUnit )` after every visited symbol, including ones with empty candidates.
6. `unit` is read as `symbol2->GetUnitSelection( &aSheet )` (the caller's sheet path, not `&sheet`), so for a
   unit living on a different sheet path the instance lookup misses and the top-level `(unit N)` is used.
- INFERRED consequence of 3 and 5: the result is "lowest unit with a non-empty value" only when the visit order
  is by ascending unit or when no earlier-visited unit had an empty value. Example: representative is unit 1
  with an empty Value, unit 3 (value C) is visited before unit 2 (value B): `minUnit` is already 1, so unit 2
  fails `2 < 1`, and C wins. Iteration order is the screen item order (`Items().OfType( SCH_SYMBOL_T )`),
  which this note did not establish as unit-sorted. Not measured. A writer should therefore put the field on
  every unit identically, or at least never leave Value empty on a unit that might be the representative.
- In the multi-unit branch non-mandatory fields are resolved with `GetShownText( &aSheet, false )` of the
  symbol2 field (line 185), not with its own sheet path.
- Unannotated parts: `R?` is a plain reference string, so all unannotated multi-unit symbols with the same
  prefix collapse into one reference in this scavenging (INFERRED from the `CmpNoCase` match and
  `m_referencesAlreadyFound`; `kicad-cli` only warns about annotation errors).

### 3.3 What is written (VERIFIED)

- `value` is `UnescapeString( value )`, or `"~"` when empty (lines 232-235): "value field always written in
  netlist". `footprint`, `datasheet`, `description` are written only when non-empty (237-244).
- `fields` list: the exported non-mandatory fields, then `Footprint`, `Datasheet`, `Description` entries
  (lines 227-229, written even when empty) with `name` attribute.
- Per-component `<property name=.. value=..>` entries for non-mandatory fields (365-379), then the sheet's
  fields (381-391), then `exclude_from_bom`, `exclude_from_board`, `exclude_from_pos_files`, `dnp`
  flags (393-415).

### 3.4 Whitespace trimming (VERIFIED)

`SCH_FIELD::SetText` (`sch_field.cpp:1051-1062`): "Mandatory fields should not have leading or trailing
whitespace. `if( IsMandatory() ) EDA_TEXT::SetText( aText.Strip( wxString::both ) );`". `IsMandatory`
(`sch_field.cpp:1421`) is true for Reference, Value, Footprint, Datasheet, Description (and the sheet and label
mandatory ids). The parser reaches it: `parseSchField` ends in `field->SetText( value )`
(`sch_io_kicad_sexpr_parser.cpp:2385`). Value set via instance data goes through `SetValueFieldText`
(`sch_symbol.cpp:1266-1272`) which calls `GetField( VALUE )->SetText`, also trimmed. Not trimmed: user fields,
variant overrides (`SetFieldText` stores `aFieldText` raw into `m_Variants[..].m_Fields`,
`sch_symbol.cpp:761-779`; the 3-argument `SCH_FIELD::SetText` computes a stripped `tmp` but passes `aText`,
`sch_field.cpp:1072-1083`, a latent inconsistency), and text inside the lib cache is trimmed at its own load.
`wxString::Strip( both )` strips the wx whitespace set (ASSUMED space, tab, CR, LF from wxWidgets
documentation, not re-read).

### 3.5 `"~"` (VERIFIED, MEASURED)

- Parser: `if( m_requiredVersion < 20250318 && CurStr() == "~" ) value = wxEmptyString;`
  (`parseSchField`, lines 2330-2333; same guard for instance and default-instance `value`/`footprint`,
  lines 3292, 3303, 3375, 3386). `eeschema/sch_file_versions.h` lines 56 and 127: "20250318 // ~ no longer
  means empty text"; current schematic version is 20260306 (line 138).
- So in a file at version 20250318 or later (all files KiCad 10 writes) `"~"` is a literal one-character
  text and the exporter will output `~` as the datasheet/footprint/description/user value (nothing in
  `addSymbolFields` maps it). Only the XML `value` element uses `"~"` as its own empty marker, on output.
- MEASURED: demo `interf_u` is version 20250114 and contains `(property "Datasheet" "~"`. Command:
  `kicad-cli sch export netlist --format kicadxml -o interf_u.xml interf_u.kicad_sch` on a scratch copy gave
  24 `<comp ` and 0 `<datasheet>` elements (the old `"~"` became empty). After
  `kicad-cli sch upgrade --force interf_u.kicad_sch` (output "Schaltplandatei erfolgreich im neuesten Format
  gespeichert"), the file is `(version 20260306)`, has 0 `(property "Datasheet" "~"` and 69
  `(property "Datasheet" ""` lines. Not measured: a version-20260306 file that itself contains `"~"` (it
  needs a file edit; see open points).

### 3.6 Variants (VERIFIED, briefly)

With a design variant selected (`kicad-cli ... --variant`), `GetValue`/`GetFieldText` read
`instance.m_Variants[name].m_Fields[field]` before the base field (`sch_symbol.cpp:1230-1263`, 788-836).
The exporter also emits a `<variants>` block per component (`netlist_exporter_xml.cpp:~420-520`).

## 4. Change Symbol / Update Symbol from Library

### 4.1 Dialog path (`eeschema/dialogs/dialog_change_symbols.cpp`, VERIFIED)

Both "Change Symbols" and "Update Symbols from Library" are `DIALOG_CHANGE_SYMBOLS::processSymbols`
(573-837). Fields are **not ignored**: per field in the "update fields" list (`m_updateFields`, built from the
check list, lines 442-456) it acts as follows.

- Defaults at dialog creation (lines 116-125): `m_removeExtraBox` false, `m_resetEmptyFields` false,
  `m_resetFieldText` **true**, `m_resetCustomPower` false, and for **Update** mode `m_resetFieldVisibilities`,
  `m_resetFieldEffects`, `m_resetFieldPositions`, `m_resetAttributes`, `m_resetPinTextVisibility`,
  `m_resetAlternatePin` **false**; for **Change** mode these are **true**. In the field list Reference and
  Value are checked only if the saved settings `change_symbols.update_references` / `update_values` are true
  (both default `false`, `eeschema_settings.cpp:636-639`; dialog lines 89-113); **Footprint, Datasheet and
  Description are checked by default**; user fields found on the new library symbol are appended and
  checked when all were checked (lines 398-404).
- Per checked field that exists on the new library symbol (lines 696-757): text is overwritten
  by the library text when the library text is non-empty and `m_resetFieldText`, or when it is empty and
  `m_resetEmptyFields`. Special cases: Reference keeps its number and takes the new prefix (lines 710-726);
  Value is set through `SetValueFieldText( UnescapeString( .. ) )` and for power symbols only when
  `m_resetCustomPower` is set (727-731); other fields get `field.SetText( libField->GetText() )`.
  Visibility, sizes/styles and position reset only if their boxes are on.
- Fields not in the new library symbol: removed only with "Remove fields if not in new symbol"
  (`removeExtras`, lines 758-761, non-mandatory only). User fields in the new library symbol and not on the
  placed symbol are added with library text, attributes, visibility and position (764-785).
- Consequence for Footprint (VERIFIED by reading, not run): on a default **Update from Library**, a non-empty
  library Footprint **overwrites** the placed symbol's Footprint; an empty library Footprint leaves the placed
  one alone (unless "Reset fields if empty" is on). In **Change Symbol**, the same, plus positions/visibility
  are reset.
- The lib id and the flattened library copy are always replaced (lines 654-660);
  excluded-from flags only when `m_resetAttributes` (662-670); alternate pins cleared when
  `m_resetAlternatePin` or the alternate no longer exists (807-826); `SetSchSymbolLibraryName( "" )`
  (line 796). Unit count too low for the symbol's unit: refused with "new symbol has too few units"
  (617-623). Instances/references are kept (`SetRef` per instance path, 714-725).

### 4.2 Other two paths (VERIFIED)

- `SCH_EDIT_FRAME::SaveSymbolToSchematic` (`sch_edit_frame.cpp:2242-2300`, symbol-editor "Update symbol in
  schematic"): for all units of the reference, `SetLibSymbol( aSymbol.Flatten() )` then `UpdateFields(
  &GetCurrentSheet(), true, true, true, false, false )` (style, ref, other fields all updated from the library
  field: Footprint and others **overwritten**, reference overwritten with the library text too).
- `DIALOG_EDIT_SYMBOLS_LIBID` (`dialogs/dialog_edit_symbols_libid.cpp:755-780`, "Edit Symbol Library
  Identifiers"): `SetLibId`, `SetLibSymbol( flattened )`; value changed only if it equalled the old symbol
  name; with the "update fields" checkbox `UpdateFields( nullptr, false, false, false, false, true )`
  (resets non-reference fields, Footprint included, no style).
- Opening a schematic: `SCH_SCREEN::UpdateSymbolLinks` (`sch_screen.cpp:717-790`) only relinks the
  library symbol (cache from `lib_symbols`, else library) and does not touch placed field text.

## 5. Save and load rules that matter for a writer

All VERIFIED from `sch_io_kicad_sexpr.cpp` (writer) and `sch_io_kicad_sexpr_parser.cpp` (reader).

1. **Hide.** The writer prints `(hide yes)` as a direct child of `property`, only when the field is
   not visible (`saveField`, lines 1029-1030: `if( !aField->IsVisible() ) KICAD_FORMAT::FormatBool( m_out,
   "hide", true )`; `FormatBool` prints `(key yes|no)`, `common/io/kicad/kicad_io_utils.cpp:35-38`).
   MEASURED in the upgraded demo: `(property "Datasheet" "" (at 132.08 99.06 0) (hide yes) (show_name no)
   (do_not_autoplace no) (effects (font (size 1.524 1.524))))`. `EDA_TEXT::Format` does not write hide
   (`common/eda_text.cpp` `(effects` block has font/justify only).
   Reader: directly under `property`, `parseSchField` does `case T_hide: field->SetVisible( !parseBool() );
   NeedRIGHT();` (line 2410), and `parseBool` accepts only `yes` or `no` (lines 130-142). So `(hide)` and a
   bare `hide` as a `property` child fail to load; `(hide yes)` and `(hide no)` work. Inside `(effects ..)` the
   reader uses `parseMaybeAbsentBool( true )` (line 894), which accepts bare `hide`, `(hide)` and `(hide yes)`
   (documented in the source comment at line 145 as `"hide", "hide)", "(hide yes)"`).
   The direct `property`-child children accepted are `id` (legacy, ignored), `at`, `hide`, `effects`,
   `show_name`, `do_not_autoplace`; anything else is a parse error ("Expecting id, at, hide, show_name,
   do_not_autoplace or effects", line 2435). `show_name` and `do_not_autoplace` use `parseMaybeAbsentBool( true )`.
2. **Property shape.** `(property ["private"] "<Name>" "<value>" (at x y angle) [(hide yes)] (show_name yes|no)
   (do_not_autoplace yes|no) [(effects ...)])`; `(effects ..)` is only written when formatting is not the
   default (`IsDefaultFormatting`, text height other than the default; lines 1036-1040). Names are written
   canonical (`GetCanonicalName`); `private` is a bare token before the name (parser lines 2292-2296).
3. **Field order.** The writer sorts by `GetOrdinal()` (`sch_symbol.cpp:1340-1358`, used at
   `sch_io_kicad_sexpr.cpp:788-789`); `GetOrdinal()` is the id for mandatory fields and `m_ordinal` for user
   fields (`sch_field.h:135-138`), so output order is Reference, Value, Footprint, Datasheet, Description, then
   user fields in the order they were created (on load, in file order). File order of mandatory fields on
   input is irrelevant. MEASURED: the upgraded demo shows Reference, Value, Footprint, Datasheet, Description.
4. **Name folding and duplicates.** On load a symbol property whose name equals a mandatory name
   case-insensitively (`CmpNoCase`) gets the mandatory id (`parseSchField` 2340-2347; library symbols the same,
   parser ~1085-1090). `GetName()` of a mandatory field returns the canonical name (`sch_field.cpp:1135-1146`),
   so `footprint` is saved as `Footprint`. User-field lookup is exact and case-sensitive
   (`sch_field.h:401-417`: `field.GetName() == aFieldName`), so `MPN` and `mpn` are two fields. A second
   mandatory property overwrites the first (`*existing = *field`, parser 3552-3553); a second non-mandatory
   with the same name is renamed `<name>_1` .. `<name>_9` (3533-3550) (the 10th duplicate overwrites).
   Legacy `Sim.Enable` / `Spice_Netlist_Enabled` properties are dropped and turned into `exclude_from_sim`
   (3511-3524).
5. **Missing field on load.** `parseSchematicSymbol` builds the symbol with the default constructor, which
   already holds the five mandatory fields with empty text, at (0,0), visible (`Init`, 178-204). A missing
   `Footprint`/`Datasheet`/`Description`/`Value` property is therefore an empty field, not an error. On the next
   save the field is written with `(at 0 0 0)` and without `hide` (INFERRED from `Init` + `saveField`; not
   run). In the netlist: empty Value gives `<value>~</value>`, empty Footprint gives no `<footprint>` element
   (3.3). A missing Reference property gives `GetRef` the `prefix + "?"` fallback (2.1). A symbol with no
   `(instances ..)` falls back to the Reference field text and `(unit N)` (2.1; the "version 1 schematic"
   fallback comment in `GetRef`).
6. **Instances on load.** Each `(path ..)` becomes a `SCH_SYMBOL_INSTANCE` through
   `AddHierarchicalReference` (parser 3345-3497). Inside a `path`, `value` and `footprint` children are still
   read and applied to the symbol's single Value/Footprint field (last one read wins; they are **not** kept per
   path), so a writer should not emit them. Unknown children raise "Expecting reference, unit, value,
   footprint, or variant".
7. **Format version.** Write `(version 20260306)` for KiCad 10 files (`sch_file_versions.h:138`); with
   `< 20250318` `"~"` is read as empty; with `< 20260306` the variant `in_bom` meaning differs (parser 3431).

## Open points

- Multi-unit field choice (3.2) is source reading only. The iteration-order hole is INFERRED. A real
  measurement needs a multi-unit scratch schematic with differing field values per unit, built through this
  project's write layer (hand edits are forbidden), then `kicad-cli sch export netlist --format kicadxml` and a
  check of which `<value>` or `<field>` appears; also a unit on a second sheet path to test the
  `GetUnitSelection( &aSheet )` quirk. Suggested follow-up ticket.
- A version-20260306 file with `"~"` in `Datasheet`/`Footprint` (to prove 3.5 for the current format) and a
  lowercase `footprint` property (to prove 5.4 by the tool) were not measured, for the same reason.
- Line numbers are for branch head `86a7a3f`; the logic is unchanged at tag `10.0.7`, but numbers in
  `sch_symbol.cpp` / `sch_field.cpp` differ slightly there.
- GitLab terms of service were not read (only the repository licence).
