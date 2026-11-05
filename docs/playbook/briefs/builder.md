# Standing brief: builder (sheet writing tools and other build tickets)

The coordinator pastes this page into the dispatch and fills the placeholders. CLAUDE.md and the
agent definition `.claude/agents/builder.md` are the source of truth for every rule they state; this
page repeats the ones named below word for word and adds the checklist. The checklist lives in the
playbook fork, section "Fail-open checklist for sheet writing tools".

**Fill in:** ticket `<TICKET>`; branch `ticket-<TICKET>`; worktree `<WORKTREE>`; acceptance text:
`python scripts/issues.py show <TICKET>`; merge instruction: `<MERGE INSTRUCTION>` (default: merge
local `main` into the ticket branch as below, never into `main`, no push).

## Work

- Work only in `<WORKTREE>` on branch `ticket-<TICKET>`. Read the acceptance text first; verify what
  is already on `main` before you build.
- Order before hand-back: (1) finish the fixes, (2) merge local `main` into your ticket branch (`git merge main`, no rebase, no force; this is allowed and counts as the prompt's merge instruction, merging into `main` stays forbidden), (3) rerun the targeted tests on the merged tree, (4) the batch gate where one applies, on a clean tree, (5) hand back the head sha, which is the gated sha.
  If `main` moves after the hand-back the coordinator decides whether a re-merge is needed.
- Use private log and scratch file names that contain your ticket number (for example
  `t<TICKET>-pytest-a.log`), and keep log paths and undo patches OUTSIDE every worktree (the scratchpad directory the session names, or a `*.log` file in the directory that holds the ticket worktrees), never in the tree: a dirty tree voids the gate's clean-tree check.
  Two agents once collided on one log name.
- Never kill a process you did not start; if a foreign process blocks you (lock, port), report it to the coordinator.
  Never touch another ticket's worktree.

## Fail-open checklist (probe each item with kicad-cli before hand-back)

If your tool writes a sheet, answer each item with a test, not with a sentence. Details and the
reasons are in the playbook fork section.

1. multi-unit part (for example LM324 units 1 to 5): edit by reference and by each unit's uuid; across sheets KiCad's pick of a multi-unit part's properties is undetermined (only on one sheet it is the lowest placed unit), so do not pin "lowest" across sheets;
2. same reference on another sheet of the tree, and a sheet used from a subfolder, with and
   without root_sch; lock order: a call never waits for a file above its own parent (the rule in `sheet_pins._run`);
3. hidden pins and power symbols (a hidden power_in pin tied by name is not open);
4. lib_name symbols;
5. every self-check key (reference, unit, uuid) and every guard has a test that goes red when that line is broken (run the mutation, report it);
6. a no-op or refusal writes nothing (file bytes compared before and after);
7. the result must not say ok when the netlist or ERC did not follow: read back what kicad-cli reports. A mismatch returns `ok: false` with `write_not_verified`.
8. no-connect markers and local power symbols change which pins count as members of a net;
9. label scope: local, global and child-sheet labels, and the hierarchy;
10. text input classes: line breaks, Unicode separators, edge whitespace in names;
11. a refusal reason or tool text that is false for a case the tool handles;
12. a spec guard that now refuses forms the base accepted;
13. an unreadable tree file or an undiscoverable root must refuse or note, never silently fall back to one sheet;
14. a tool that deletes has a self-check, with a corrupted-write test.

A second fail-open of the same family means refuse everything not proven. Do not add a denylist or
a special case.

## Test runs

- Targeted runs follow the coordinator decision of 2026-11-05, pending owner confirmation in issue 148 (the foreground limit is 10 minutes and a run killed by it prints no summary line).
- Run tests as SMALL SEPARATE runs (each aimed at under about 20 minutes), in the background (`run_in_background`) with their own log, waiting for the completion notification (no sleep loops), each printing its own summary line. A run killed by a timeout prints none.
- Use `PYTHONPATH=<WORKTREE>/src`, the shared interpreter by absolute path, `KICAD_ROUTING_TOOLS_PATH`
  as the dispatch says. A result counts only if `kicad_mcp.__file__` lies inside the worktree and pytest's `rootdir:` line names it; report both.
- Test in the worktree itself: a `git archive` export has no `.git` and fails
  `test_docs_layout` and `test_skills_tracked`.
- While a batch gate runs, start no more than 2 other runs (the gate's timing basis, fork P11, assumes few).
- Run `scripts/lint_gate.py` on the whole tree with the shared interpreter.
- The full suite and the batch gate keep their own rules (builder definition, Tests).

## Hand-back

- Report ONCE, then stop. If the hand-back tool fails, retry the same tool once, then state the report as the final message; do not invent another channel.
- The report names: head sha (after merging main), files changed, each run with its summary line,
  what was already on `main`, decisions taken, anything not done and which ticket should own it.
- Commit with the trailer lines from the dispatch. Do not amend or rebase after a review started.
