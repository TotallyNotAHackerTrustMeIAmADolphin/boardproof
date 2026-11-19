## Fail-open checklist for sheet writing tools

Several of the schematic tickets of 2026-11-05 (issues 111, 118, 120 and others) needed extra
review rounds for one family of defects: a writing tool reports success while KiCad ignores or
overrides the edit. The builder probes each item below with real `kicad-cli` on a `tmp_path` copy
built through `kicad_tools` before hand-back, and the reviewer checks the same list. "Probe" means:
build the case, run the tool, run `kicad-cli sch export netlist` and `kicad-cli sch erc` on the
result, and compare with what the tool returned. A test pins what `kicad-cli` reports, never what
the builder guessed.

1. **Multi-unit part.** Use a part with several units (for example an LM324, units 1 to 5). Edit
   it by reference and by each unit's own uuid. The test shows the edit reaches exactly the
   addressed unit, a key such as mirror or position is not collapsed across units because they
   share a reference, and a field KiCad reads from one unit only (Value, LCSC) is checked in the
   netlist, not in the file. Across sheets KiCad's pick of a multi-unit part's properties is
   undetermined (only on one sheet it is the lowest placed unit, docs/ARCHITECTURE.md, build
   ticket 50 entry), so a test must not pin "lowest" across sheets.
2. **Same reference on another sheet, and a sheet in a subfolder.** Put the same reference (or
   another unit of the part) on a second sheet of the tree, and open a child sheet that lives in a
   subfolder, each with and without `root_sch`. The test shows the tool finds the root (or refuses
   with a true reason) and resolves the reference across the whole tree. Lock order: a call never
   waits for a file above its own parent (it locks the parent and the child files of its sheet
   symbols, in hierarchy order; the rule is the comment at the lock in `sheet_pins._run`). A test
   shows a call on a child does not wait for a lock held on its parent's parent.
3. **Hidden pins and power symbols.** A hidden `power_in` pin is tied by name and is not open;
   a power symbol sets a net by its value. The test shows the tool's open/unconnected verdict
   equals what ERC and the netlist say for these pins.
4. **`lib_name` symbols.** A placed symbol whose `lib_name` differs from its `lib_id` (a local
   copy in `lib_symbols`). The test shows the tool finds its pins and library data through
   `lib_name`, or refuses.
5. **Every self-check key and guard has a red test.** Each key of a self-check (reference, unit,
   uuid) and each guard line (drift, connectivity, refusal) has a test that goes red when that
   one line is broken. Run the mutation yourself (delete or invert the line, see the test fail,
   restore) and report it. A guard without such a test counts as absent. A tool that deletes has a
   self-check too (item 14).
6. **No-op and refusal write nothing.** For a no-op request and for every refusal, the test
   compares the file bytes (and the mtime where a lock matters) before and after.
7. **Do not say ok when KiCad did not follow.** After the write, read back what `kicad-cli`
   reports (netlist, ERC, the field or position in the exported data) and return `ok` only if it
   matches the request. A mismatch returns `ok: false` with `write_not_verified`. Anything the
   tool cannot read back is refused or returned with its real code (`write_not_verified`, `connectivity_changed`), never `ok`.
8. **Net members.** No-connect markers and local power symbols change which pins count as members
   of a net (issue 143). The test shows the tool's net membership equals the netlist with such a
   marker or symbol present.
9. **Label scope.** Local, global and child-sheet (hierarchical) labels connect differently and
   across the hierarchy (issue 114, label renames). The test shows a label of each scope on the right sheet and
   across a sheet pin, against the netlist.
10. **Text input classes.** Line breaks, Unicode separators and leading or trailing whitespace in
    names and texts (issue 144). The test feeds each class and shows it is refused or kept as
    KiCad reads it back.
11. **Reason and tool text are true.** A refusal reason or a sentence of the tool text that is
    false for a case the tool does handle is a finding (issue 140). The test or review reads each
    reason against a case that reaches it.
12. **Spec guard versus base.** A spec guard that now refuses a form the base accepted is a
    regression unless the ticket says so (issue 146). Run the base's accepted forms against the new
    guard.
13. **Unreadable tree.** An unreadable tree file or an undiscoverable root refuses or notes it,
    and never silently falls back to one sheet (issues 141, 120).
14. **Delete has a self-check.** A tool that deletes verifies the result (the object is gone, the
    rest is unchanged, the netlist follows) before it replaces the file, and has a corrupted-write
    test.

**Second fail-open of the same family:** the design is the finding. A denylist or a special case
per input is replaced by refusing everything not proven (this section extends the rule in
`docs/dispatch/review-rules.md`, "What a finding blocks"; build ticket 67, round 3).


Load: targeted runs raise the machine load, and the batch gate's timing basis (CLAUDE.md P11)
assumes few other runs. While a gate runs, start no more than 2 other runs.

The dispatch templates are `docs/dispatch/briefs/builder.md` and `docs/dispatch/briefs/reviewer.md`;
the coordinator pastes and fills them in. The rule for the reviewer's worktree is in
`docs/dispatch/reviewer.md` and repeated word for word in the reviewer template.
`tests/test_playbook_briefs.py` checks that the templates exist and carry these items and the
hand-back rules.
