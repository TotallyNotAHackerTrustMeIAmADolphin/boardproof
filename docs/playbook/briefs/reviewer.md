# Standing brief: reviewer (adversarial review of one branch head)

The coordinator pastes this page into the dispatch and fills the placeholders. CLAUDE.md and the
agent definition `.claude/agents/reviewer.md` are the source of truth for every rule they state;
this page repeats the ones named below word for word and adds the checklist. The checklist lives in
the playbook fork, section "Fail-open checklist for sheet writing tools".

**Fill in:** ticket `<TICKET>`; branch `ticket-<TICKET>`; builder worktree `<WORKTREE>` (read-only
for you); head sha to review `<SHA>`; acceptance text: `python scripts/issues.py show <TICKET>`;
merge instruction: `<MERGE INSTRUCTION>` (default: you merge nothing and change nothing in the
builder's worktree or the main checkout).

## Setup

- Review the full sha your prompt names in your OWN detached git worktree of that sha, never in the builder's worktree and never in a `git archive` export (an export has no `.git` and fails `test_docs_layout` and `test_skills_tracked` falsely):
  `git worktree add --detach <worktrees dir>/rev-<ticket>-<sha7> <sha>`; remove it at the end. `<worktrees dir>` is the directory that holds the ticket worktrees.
  If `git worktree add` fails, report it to the coordinator, or use a `git init` copy with `git add -A` and a committed baseline, labelled as such in your report; never silently an export without `.git`.
  `git worktree add` and `git worktree remove` write only metadata under `.git`.
- Mutations happen in that private worktree only: restore with `git checkout -- .` after each. Never mutate a worktree while a batch gate runs on it.
  Never mutate while ANY run on that worktree is in progress; restore only after its completion notification.
- Judge the sha you were given. If `main` moved since, that is not a finding unless it conflicts.
- A delta review: the builder merged `main` before hand-back, so limit the delta to the ticket's own paths (`git diff <reviewed> <head> -- <ticket paths>`, or the commits since the reviewed sha authored for the ticket). Report a merge conflict; never resolve it.
- While a batch gate runs, start no more than 2 other runs (the gate's timing basis, fork P11, assumes few).
- Use private log and scratch file names that contain your ticket number and `rev` (for example
  `rev-<TICKET>-a.log`), and keep log paths and undo patches OUTSIDE every worktree (the scratchpad directory the session names, or a `*.log` file in the directory that holds the ticket worktrees), never in the tree: a dirty tree voids the gate's clean-tree check.
  Two reviewers once collided on one log name.
- Never kill a process you did not start; if a foreign process blocks you (lock, port), report it to the coordinator.

## Fail-open checklist (probe each item with kicad-cli; a missing probe is a finding)

The reasons are in the playbook fork section.

1. multi-unit part (for example LM324 units 1 to 5): edit by reference and by each unit's uuid; across sheets KiCad's pick of a multi-unit part's properties is undetermined (only on one sheet it is the lowest placed unit), so a test must not pin "lowest" across sheets;
2. same reference on another sheet of the tree, and a sheet used from a subfolder, with and
   without root_sch; lock order: a call never waits for a file above its own parent (the rule in `sheet_pins._run`);
3. hidden pins and power symbols (a hidden power_in pin tied by name is not open);
4. lib_name symbols;
5. every self-check key (reference, unit, uuid) and every guard has a test that goes red when that line is broken (run the mutation yourself);
6. a no-op or refusal writes nothing (file bytes compared before and after);
7. the result must not say ok when the netlist or ERC did not follow: read back what kicad-cli reports. A mismatch returns `ok: false` with `write_not_verified`.
8. no-connect markers and local power symbols change which pins count as members of a net;
9. label scope: local, global and child-sheet labels, and the hierarchy;
10. text input classes: line breaks, Unicode separators, edge whitespace in names;
11. a refusal reason or tool text that is false for a case the tool handles;
12. a spec guard that now refuses forms the base accepted;
13. an unreadable tree file or an undiscoverable root must refuse or note, never silently fall back to one sheet;
14. a tool that deletes has a self-check, with a corrupted-write test.

Also check that no test pins a guess that kicad-cli contradicts. A second fail-open of the same
family means refuse everything not proven. The design is then the finding.

## Test runs

- Run tests as SMALL SEPARATE runs (each aimed at under about 20 minutes), in the background (`run_in_background`) with their own log, waiting for the completion notification (no sleep loops), each printing its own summary line.
- Use `PYTHONPATH=<review worktree>/src` and the shared interpreter by absolute path. A result counts only if `kicad_mcp.__file__` lies inside the worktree and pytest's `rootdir:` line names it; report both.
- Run the ticket's targeted tests, `tests/test_ratchets.py` and `scripts/lint_gate.py`.

## Hand-back

- Class every finding BLOCKS or FOLLOW-UP (playbook fork, "What a finding blocks"). The verdict is
  CLEAN only with no BLOCKS finding.
- Report ONCE, then stop. If the hand-back tool fails, retry the same tool once, then state the report as the final message; do not invent another channel.
- The report names: the reviewed sha, the verdict, each finding with its class and evidence, each
  mutation you ran and its result, each run with its summary line. Then remove your review worktree.
