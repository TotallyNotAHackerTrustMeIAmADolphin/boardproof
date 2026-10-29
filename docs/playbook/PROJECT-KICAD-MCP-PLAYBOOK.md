# Kicad-MCP: project playbook (fork)

Fork of `AGENT-WORKFLOW-PLAYBOOK.md` in this folder (the version of 2026-10-11 with Core, A1-A12,
856 lines). It cites that file by section and copies nothing: the general file wins on general
rules, this file on project values. Except: P6 and P11 override A1, the resources table's
background-jobs row and the sub-agent template for the batch gate, which runs in the background
(build ticket 66). `CLAUDE.md` (hard rules) wins over both.

## Section 0: P1-P17

> **Project rule P1: Human-only steps and owner approvals.** Logins and credentials; opening a
> pull request on another project or commenting on another person's thread (issues may be filed
> without a per-issue review: owner, 2026-10-19; build ticket 48); any change to the frozen openMPPT
> board; providing a Freerouting jar (GPLv3, never downloaded automatically); spending (the owner
> will not pay for GitHub, 2026-10-19). Licence attribution of the `pic_programmer` copies is
> recorded in `.scratch/pivot-ladder/runs/ATTRIBUTION-pic_programmer.md` (CC BY-SA 4.0); demos read
> from the install at test time are never vendored. **Committing and pushing are pre-authorised**
> (owner, 2026-10-19, ticket 13 amendment Q8): commit after every closed ticket and push right
> away, without asking, with the full suite run first (P11). Every commit on `main` and every
> research branch is pushed. Local squash merges of build-ticket branches after a CLEAN review and a
> green local suite are pre-authorised too (P12).

> **Project rule P2: What no agent may touch by hand.** `.kicad_sch`, `.kicad_pcb`, `.kicad_sym`,
> `.kicad_mod`, `.kicad_pro`, `.kicad_dru` (CLAUDE.md hard rule 1; the one tolerated exception is a
> test-local `.kicad_dru` under `tmp_path`). The openMPPT repo
> `<home>\Documents\PlatformIO\MPPT` (a path on the other machine; not present on
> another machine), branch `hardware/PCB_v1.3`, frozen until ladder rungs 1 and 2 pass.
> No force pushes, no deleting `main`, no other force operations.

> **Project rule P3: Issue tracker, CLI, labels.** GitHub issues of this repository, written so that
> they may become public (no real name, no machine path, no candid remark about a person or another
> project). The convention is `docs/WORKFLOW.md`: a map is one issue labelled `map` with a task
> list; a ticket is an issue labelled by type (`build`, `research`, `decision`, `task`) plus
> `critical` (needs an adversarial review), `parked`, `needs-owner`; `Blocked by #n` lines; status
> is open or closed; the Answer and every review verdict (reviewed sha, verdict, BLOCKS and
> FOLLOW-UP lists, delta rounds, gate verdict) are comments. Agents read a ticket with
> `python scripts/issues.py show <n>` and record with `python scripts/issues.py comment <n>
> --body-file <file>`; the script refuses a text that hits `scripts/check_publishable.py`. The
> review loop, what a finding blocks and the stop rule stay as they are. Old ticket ids (build
> ticket N, P1-xx..P5-xx, M-0x) appear in code comments and docs as plain references; an issue
> migrated from the old planning folders says "formerly build ticket N" in its first line; the
> The old backlog ids resolve through the maintainers' archive (commit `3380b34`).

> **Project rule P4: Deploy window, command, verification.** None; nothing runs in production.
> "Release" means a commit or local squash merge on `main` (P1, P12). Sections 6 (deploy parts) and
> 7 (deploy parts) do not apply.

> **Project rule P5: Parallelism, builder model, turn cap.** Up to about 5 builders on disjoint
> `Files:` lists; sub-agents are the `builder`, `reviewer` and `researcher` definitions in
> `.claude/agents/` (A1/A2/A11 adapted to this project's contracts, build ticket 45), resumed with
> `SendMessage` for fixes; `general-purpose` only for ops work. The definitions set no `model`, so
> every agent inherits the coordinator's model and the reviewer is at least as strong as the
> builder (same family: shared blind spots are the risk; the mutation tests are the check that does
> not depend on judgement). Build tickets: one
> local branch per ticket in its own hand-made worktree (section 4; section 8 items 1 and 8), but
> branched from the current **local** `main`, not `origin/main`: `git worktree add --lock --reason
> "<agent> #<n>" -b ticket-<n> <scratch>/work/wt-<n> main`. Never `isolation: "worktree"`: it bases
> on `origin/main` (checked 2026-10-19), not on the local `main` that the coordinator merges into
> (`paradigm-review-2` ticket 13, amendment Q7).
> Planning and research tickets (maps, research notes, decisions; no code) live only in issues and
> their comments; a research findings file that belongs in the repository is committed to `main`
> by the coordinator. **Worktrees need no special handling for planning records any more**: there
> is no `.scratch/` to link or copy, because the planning lives in the issues. Tickets that share
> files run in sequence, even when neither blocks the other. Every sub-agent runs its own tests before it reports.

> **Project rule P6: Test, lint, environment.** `.venv/Scripts/python.exe -m pytest -q <paths> 2>&1 |
> tail -15`, never `pytest.exe` (collection fails on `from tests. ...` imports; build ticket 33);
> full suite adds `-m "not network"`, and the gate run sets `KICAD_DEMOS_REQUIRED=1` and `KICAD_ROUTING_TOOLS_REQUIRED=1` (P11).
> Targeted test runs in the foreground, with timeouts; the batch gate (P11) runs in the background
> (`.claude/agents/builder.md`, Tests). **In a worktree, test the worktree's own code** (and drop `-q`, which hides
> the `rootdir:` line): run from the worktree
> root with the shared interpreter by absolute path, `<main checkout>/.venv/Scripts/python.exe`,
> and `PYTHONPATH=<worktree>/src`. The result counts
> only if `kicad_mcp.__file__` lies inside the worktree **and** pytest's `rootdir:` line names the
> worktree; otherwise the shared `.venv`'s editable install tested the main checkout.
> `scripts/batch_gate.py` (P11) does both checks itself. **Lint:** `ruff`, pinned exactly in the
> dev extra (`ruff==0.17.1`), run by `scripts/lint_gate.py` with `--config pyproject.toml` (a
> nested `ruff.toml` cannot change the rules) and `--ignore-noqa` (a noqa comment cannot hide a
> finding), against the shrink-only baseline `scripts/ruff-baseline.json` (153 findings frozen on
> 2026-10-20 on the tree merged with `main`, counted per file and rule code). `[tool.ruff]` must
> equal `EXPECTED_RUFF_CONFIG` in `scripts/lint_gate.py`, so adding ignores, excludes or
> per-file-ignores needs a reviewed change there. A new finding is fixed, never baselined; a fixed
> one is removed with `--shrink`. The shared `.venv` needs the pinned ruff (`.venv/Scripts/python.exe -m pip
> install ruff==0.17.1`); without it the lint gate, and so the batch gate, is RED. Scratch is the
> session scratchpad. Bash tool: no long heredocs with apostrophes; write files with the Write tool.

> **Project rule P7: Risk classes.**
> - **critical:** `verify_board.py`, `gate_state.py`, `kicad_cli.py` (gate inputs), `rung_runner.py`,
>   `rung_backends.py`, `autonomy_run.py`, `autonomy_protocol.json`, `input_prep.py`,
>   `schematic_import.py`, `board_rules.py`, `board_outline.py`, `silk_minimums.py`, `pour_step.py`,
>   `routers/kicad_routing_tools.py`, the tool registrations in `server.py`, anything that mutates a
>   `.kicad_*` file, and text an LLM reads at runtime (tool descriptions, `.claude/skills/**`,
>   `CLAUDE.md`, `docs/CONTEXT.md`).
> - **core:** other `src/kicad_mcp/` code and tests. **config/ops:** `pyproject.toml`, `evals/`,
>   fixtures. **docs:** `docs/ARCHITECTURE.md`, `docs/ROADMAP.md`, `README.md`, `docs/**`.
> - Critical-class text (`CLAUDE.md`, `docs/CONTEXT.md`, `.claude/skills/**`, tool descriptions) needs a
>   CLEAN adversarial review before it is committed, even when a planning ticket changes it.

> **Project rule P8: Enforcement table.** Tooling: the gate (`verify_board`) is the only source of a
> "routed/done" claim; every board-state output carries `gate` and a sweep test fails on any other
> "routed"/"done"; no test seam (`drc`, `router`, `fill`, `connectivity_check`, `footprint_dirs`,
> `netlist_exporter`, `_clock`, `**kwargs`) is reachable from the MCP surface (AST source-scan
> guard); an unofficial run (dirty tree, injected seam, unpinned rung, edited plan) can never fill
> the official slot. Instruction only: no hand-edits of `.kicad_*`, the kicad_tools geometry
> cross-check rule (moving into code: ratchets, build ticket 29; raw-token geometry readers, build
> ticket 43), openMPPT frozen. Tooling decided 2026-10-19, not built yet: every writing tool's
> self-check (build tickets 22, 30-32), the drift guard (40), the KiCad lock-file guard (41).
> **`main` integrity: instruction only + hook** (section 8 item 0): no paid GitHub (owner,
> 2026-10-19), so there is no hosted CI and no server-side branch protection. Two local hooks
> (build ticket 45), and normal pushes stay allowed (P1):
> - **pre-push guard (every push; authoritative for pushes, coordinator decision 2026-10-20):**
>   `scripts/git-hooks/pre-push`, installed into the common git dir (`cp scripts/git-hooks/pre-push
>   "$(git rev-parse --git-common-dir)/hooks/"`), sees the real ref updates and refuses every
>   non-fast-forward update of any ref and deleting `main`, whatever started the push. Git does not
>   show a mirror push's deletions to the hook (measured, git 2.55), so it also refuses pushes to a
>   remote with `remote.<name>.mirror` and pushes from a repository without a local `main`.
>   Installing or refreshing it is a merge step (P12); the batch gate prints whether it is
>   installed and current.
> - **`git-guardrails` (agent sessions):** a Claude Code PreToolUse hook,
>   `scripts/git_guardrails.py`, registered in `.claude/settings.json` for every tool that runs a
>   shell command (`Bash|PowerShell|Monitor`). A command parser is never a complete shell parser:
>   for pushes it is best effort (it blocks the force and delete forms it can see and allows
>   arguments it cannot resolve, so `git push origin "$BRANCH"` works). For local deletion of
>   `main` (`git branch -d/-D/-m/-M`, `git update-ref -d` or `--stdin` on `main` or `HEAD`,
>   removing or writing `.git`, its refs or `refs/heads/main`; `main` compared case-insensitively)
>   it is the only guard, and there anything it cannot resolve is refused. It refuses switching
>   the pre-push guard off in plain sight (`push --no-verify`, `core.hooksPath` in any form,
>   touching `hooks/pre-push` or `.git/hooks`), and deleting or force-updating through `gh`.
>   Unreadable hook input is blocked. Its module docstring lists what it cannot see. It is not the
>   plugin's `git-guardrails-claude-code`, which blocks every push.
>
> **Threat model (coordinator decision, 2026-10-20, build ticket 45 fix round 3).** The guards
> defend against accidental or careless destructive commands by an agent, including a frustrated
> agent that tries to make a hook "shut up" in plain sight. They do not defend against deliberate
> obfuscation: an agent that hides a force flag inside `$(printf ...)` or deletes a ref from a
> Python one-liner breaks the hard rules on purpose, and no command parser can stop that. So every
> literal, plausible form is blocked, unknown local delete targets are refused, and obfuscated
> option injection through unresolved push words is not chased (the pre-push guard still refuses
> the resulting non-fast-forward update or deletion of `main`, unless it was switched off by such
> an obfuscated command).
>
> Merges are local squash merges by the coordinator after a CLEAN review and a green local
> suite (P12). A rule broken once becomes a test or a hook.

> **Project rule P9: Domain invariants and irreversible paths.** The safety path is false claims
> about a board. No board is called routed or done without a `verify_board` PASS (zero unconnected
> items per net, zero error-level DRC, zones filled and verified, all figures from `kicad-cli`).
> Until a PASS exists, report the complete per-net unconnected list and the complete violation list.
> Samples, counts, "0 shorts", a router self-report and `kicad_tools` properties are never
> evidence. Position and geometry facts are stated only after reading the raw S-expression. A
> measurement is reported as a measurement, including a failure.

> **Project rule P10: Resource thresholds.** Dispatch freely under 95% of the window; stop at 95%
> weekly. A job past about 3x its expected duration is stopped and escalated (see incident 2).
> `kicad-cli` and router jobs run under `timeout`.

> **Project rule P11: Full-suite command, known reds.** The test gate is the batch script
> (general A9, build ticket 45): `python <checkout or worktree>/scripts/batch_gate.py`, run by the
> builder at the end of its ticket and by the coordinator before a merge, each on a committed,
> clean tree with no agent editing it (otherwise the number is not a
> verdict). There is no hosted CI: this local run is the test gate, permanently (owner,
> 2026-10-19). When the full suite is required at all is set by the owner rule of 2026-10-29 in CLAUDE.md and docs/TESTING.md ("Test suite"): not after a change that only moves files or edits docs or comments. It runs `<shared .venv>/python.exe -m pytest -m "not network" -rfEs -p
> no:cacheprovider` (no `-q`) from the tree's root with `PYTHONPATH=<tree>/src` and
> `KICAD_DEMOS_REQUIRED=1` and `KICAD_ROUTING_TOOLS_REQUIRED=1` under a 7650 s timeout (about 3x the slowest measured suite with only other gates running, P10; build tickets 70 and 88), checks `kicad_mcp.__file__`, the
> `rootdir:` line and `configfile: pyproject.toml` (P6), prints the collected count and every new
> failure by id, re-runs each new failure once alone (a pass is a flaky candidate and still RED),
> runs the lint gate (P6), and ends with `BATCH GATE <sha7>: GREEN` or `RED (<reasons>)`; logs go
> to the system temp directory's `kicad-mcp-batch-gate/`. It takes 8-43 minutes (460-870 s of
> pytest on 2026-10-20 with one gate at a time, up to 2545 s on 2026-10-21 with several gates in parallel and only other gates known to be running; the 5331 s run c1c8f1b of that day is excluded from the timeout's basis: a reviewer's targeted tests ran at the same time and the machine was in standby for 3770 s of it; it exited 0 and ended GREEN under the 4200 s timeout then in force, because Popen.wait(timeout) does not count standby time on Windows; the gate now enforces the timeout on the wall clock; the
> gate prints its expected duration and worst case, about 168 min, at its start), so it runs in the
> background (`.claude/agents/builder.md`, Tests). For the batch gate, the suite's timeout is about 3x
> the slowest measured run with only other gates known to be running and every other step has a fixed bound, so the worst case the gate
> prints at its start is its stop point. It is RED when
> `KICAD_ROUTING_TOOLS_PATH` is unset or not a directory, when nothing passed, when the deselected
> count differs from the number of `network`-marked tests, when the tests that ran do not account
> for every selected test, and on any skip that **`scripts/known-skips.toml`** does not allow (test
> file, reason prefix, maximum count, why; this machine's 12 skips on 2026-10-20); a module-level
> skip is always RED. It is also RED when `[tool.pytest.ini_options]` differs from
> `EXPECTED_PYTEST_CONFIG` in the script, or when a tracked `tests/test_*.py` file is missing from
> a full `--collect-only` run. Adding a skip entry, raising a maximum or changing the pytest
> configuration needs a ticket and a review, like a known red.
> **Known reds are only the entries of `scripts/known-red.txt`**, each naming its owning ticket (an
> entry without one turns the gate RED). Adding an entry needs its own ticket and an adversarial
> review; any failure not on the list blocks the merge or commit. Entries match by test id only: a
> known-red test that starts failing for a different reason still counts as known, so read its
> failure text in the log. Last result 2026-10-19 on `main`
> after build tickets 29, 31 and 33 (coordinator's run): see the ticket 33 merge commit. The file
> started with one entry, owned by build ticket 46:
> `test_routing_toolset.py::TestRealRetryLoopOnGoldenPathBoard::test_propose_route_with_retry_on_golden_path_board`
> (our `kicad_tools` DSN exported U1's custom pads as their 0.143 mm anchor, so Freerouting's track
> overlapped the real copper by about 0.024 mm; build ticket 33). Build ticket 33 fixed the other four;
> build ticket 46 (KiCad's own DSN export) made it pass and removed the entry, so the list is empty.

> **Project rule P12: Branch, PR, merge conventions.** Everything is local (`paradigm-review-2`
> ticket 13, amendment Q7); there are no PRs and no `land.sh`. Build tickets: a `ticket-<n>` branch
> in its own worktree (P5); builder, then adversarial review until CLEAN; then the full suite in the
> worktree against its own `src` (P6). The reviewed commit is pinned: before
> `git merge --squash ticket-<n>`, `git rev-parse ticket-<n>` must equal the reviewed sha recorded
> in the ticket; otherwise the delta is reviewed first. The coordinator then squash-merges into
> local `main` (one commit) only when the review is CLEAN and no test fails outside P11's known-red
> list; a branch without CLEAN is never merged. After a merge that changes
> `scripts/git-hooks/pre-push` (and once after build ticket 45), the coordinator installs it into
> the common git dir: `cp scripts/git-hooks/pre-push "$(git rev-parse --git-common-dir)/hooks/"`;
> the batch gate reports `pre-push guard: installed` only when the installed copy matches. The verdict, the reviewed commit and the review text
> go into the ticket. Planning tickets commit directly on `main`, but a change to critical-class
> text (P7) still needs a CLEAN adversarial review first. Push right away after each commit (P1).
> Trailers from the session's attribution block.

> **Project rule P13: Rules, glossary, memory.** `CLAUDE.md`, `docs/CONTEXT.md` (the glossary),
> `docs/ARCHITECTURE.md` ("Known facts and gotchas"), `docs/ROADMAP.md`, `docs/TESTING.md`, the memory directory.
> A finding is recorded immediately: docs/ARCHITECTURE.md, the Answer
> (an issue comment on the ticket), one line in the map issue.

> **Project rule P14: Alert duties.** None (no running system). Session start: newest hand-off
> file and `git status`.

> **Project rule P15: Owner zone, channel, decision list.** The owner writes German; docs and code
> are English. One question at a time, in German, with a recommendation. "Autonomous" means two
> debating agents, then the main session decides and records it in the ticket. Committing is
> pre-authorised (P1). Settled 2026-10-19: the git identity, committing the tree, the Freerouting jar
> on this machine (2.5.0, sha matches the release), and no paid GitHub (no hosted CI, everything
> local; P8, P11, P12). Decisions waiting for the owner: whether the 6 silkscreen overlaps of rung 2 may
> be addressed by a placement/silkscreen step. Decided by the coordinator at the owner's request
> (2026-10-19, `paradigm-review-2` ticket 15): rung 1 keeps its board and is pinned (build ticket
> 51); `pic_programmer` is attributed; its pad count is documented in docs/TESTING.md; the gate's
> netlist-count guard is build ticket 50. Upstream issues are authorised without per-issue review
> (P1).

> **Project rule P16: Plugin version; extra skills.** mattpocock-skills pinned to the installed
> version (verify the table in section 3 against its frontmatter at the next update). Extra:
> `kicad-verification`, `kicad-pcb`, `kicad-schematic`, `kicad-sourcing`, `kicad-manufacture`
> (project skills over the MCP toolsets), `mcp-builder` for tool schemas. The plugin's
> `git-guardrails-claude-code` is not used: it blocks every push, and pushes are expected here
> (P1). Its place is taken by this project's own hook (P8, build ticket 45). The `grilling` opt-out line in the global
> `CLAUDE.md` is not written yet.

> **Project rule P17: Human-only skill needed while the owner is away.** Default: by hand from the
> templates, flagged as hand-written, never blocking.

## Project values (section 4 and A1)

- **Every builder prompt adds:** the ticket text and its acceptance items; the `Files:` it owns;
  "never hand-edit `.kicad_*`; cross-check any position or geometry against the raw S-expression";
  "no routed or done claim without a `verify_board` PASS"; the checkpoint pattern (propose dry run,
  revision token, confirm, new revision, temp dir plus rename, source untouched, no seam on the MCP
  surface; reference implementation `input_prep.py`, later the shared helper of build ticket 22);
  the self-check (verify before replace, `write_not_verified`) and the drift guard (`drift_detected`,
  `file_open_in_kicad`) once built (docs/ARCHITECTURE.md decisions 4 and 12); every mutation through the
  write layer (decision 1; until build ticket 42 lands, its interim rule); the `ToolsetBuilder` and `SourcingProvider` contracts where relevant;
  targeted tests in the foreground and the batch gate in the background (`.claude/agents/builder.md`,
  Tests, which with this fork overrides the generic A1 and sub-agent template); one genuine line of
  encouragement.
- **Every reviewer prompt adds:** scratch-copy-only mutation testing (copy src, tests,
  pyproject.toml, fixtures into the scratchpad, `PYTHONPATH=<copy>/src`, verify `kicad_mcp.__file__`
  points into the copy before trusting a result; never modify the working tree), real `kicad-cli`
  where the claim is about KiCad behaviour, and the findings the reference module needed. The
  verdict, the reviewed commit and the review text go into the ticket. Each BLOCKS finding becomes
  a failing test before its fix; a FOLLOW-UP's test is written in its own ticket.
- **Metrics:** review rounds and the CLEAN verdict per ticket only. No token or wall-clock tracking.

## Project-only rules

- **Tools, not scripts (CLAUDE.md "Batch tools"):** operations on collections, as a checkpointed
  propose/confirm pair; when no tool exists the work builds the tool through the project's write
  layer on `kicad_tools`' low-level API (CLAUDE.md rule 1; until build ticket 42 lands, the
  interim rule in docs/ARCHITECTURE.md decision 1) instead of a one-off script or a hand
  edit.
- **What a finding blocks (owner, 2026-10-21; incident 6):** a review classes every finding
  BLOCKS or FOLLOW-UP. CLEAN means no BLOCKS finding; it does not mean no finding at all.
  - **BLOCKS:**
    - a BLOCKS finding from an earlier round that is not closed; a FOLLOW-UP never blocks;
    - an acceptance item not met: not built, or wrong for an input the item names or the ticket's
      tests cover. An input the code refuses with a true reason meets the item unless the item
      requires that input to succeed. Shrinking a claim never closes one. Only the coordinator,
      with the owner, moves an item to another ticket, and records the move in the ticket;
    - a regression the ticket's commits cause: behaviour that was right at the base or at the
      previous reviewed head and is wrong now, wherever it shows;
    - a fail-open path in code the ticket added or changed: an input it does not refuse that
      yields pass, ok or success instead of incomplete or refused (incident 3). Refusing that input
      with a true reason closes the finding; handling it fully is a FOLLOW-UP. An evasion form of a
      static scan or ratchet (FOLLOW-UP list) is not a fail-open path; a runtime guard has no such
      exemption;
    - a false claim in the lines the ticket wrote or changed, or in a verdict or value its code
      returns. Shrinking the claim to what the code proves closes it as well as widening the code
      does;
    - a guard or acceptance test that stays green when the key line it guards is broken, or an
      existing test weakened;
    - a breach of a CLAUDE.md hard rule or of a contract in the STANDARDS list of
      `.claude/agents/reviewer.md`.

    A second fail-open or wrong value of the same family makes the design itself the finding (a
    denylist, a special case per input), and only refusing everything not proven closes it (build
    ticket 67, round 3).
  - **FOLLOW-UP:**
    - an edge case outside the ticket's acceptance items that is not a fail-open path in its code;
    - a defect in code the delta did not touch, unless the delta causes it;
    - an over-refusal whose reason is true;
    - evasion of a static scan or ratchet: a form only an author aiming past the scan writes (a
      name built from strings, `getattr` on a computed string, `exec`, dunder or frame access).
      Forms ordinary code uses (aliases, lazy, local or class-body imports, wrappers, re-exports)
      are not evasion. The scan's docstring says it does not defend against evasion and names
      these classes; until it does, the claim is false and BLOCKS. A form in a named class is a
      FOLLOW-UP.
  - A delta review judges the delta and the findings it answers. It opens no new axis of attack on
    unchanged code; what it finds there is a FOLLOW-UP unless it is a regression the delta causes.
  - A finding that fits both lists BLOCKS, and so does one the reviewer and the coordinator class
    differently.
  - Builders fix BLOCKS findings. A FOLLOW-UP they fix anyway is new code under review. At the
    merge the coordinator files the FOLLOW-UPs as build tickets (one per module is fine), links
    them from the review record, and their failing tests are written there.
  - "No merge without CLEAN" and the stop rule below stay as they are. Findings from rounds before
    2026-10-21 are classed by the ticket's next review.
- **A stop rule for deep review loops (section 5):** after the third delta review of one module,
  no reviewer is dispatched again without the owner. The module's claims shrink to what it proves
  (per-item `proven | unproven`, a `proves` field), and the authority stays with the gate. The
  coordinator checks the latest diff against the open BLOCKS findings and escalates to the owner.
  The owner may grant one more fix round, or cut the open findings into a FOLLOW-UP ticket (never
  a hard-rule breach); either way one more delta review judges what is left and gives the verdict.
  See incidents 2 and 6. The stop rule never permits a merge without a CLEAN verdict: it
  escalates, it does not wave through.
- **Measurement protocol:** the first OFFICIAL scored batch per (rung, backend, protocol sha) is the
  reported one; unofficial batches are shown beside it and never fill the slot; no re-run to improve
  an outcome; Freerouting reported UNAVAILABLE rather than invented.

## Evidence (incidents, 2026-10-11)

1. **Confident wrong claims from convenience properties** (history in CLAUDE.md rule 2); again
   during rung 1: `PCB.add_footprint` silently adds the old Edge.Cuts corner to positions; the pour
   step first read `Zone.polygon` page-relative.
2. **The pour step ran about 100 minutes and four review rounds** (expected: well under an hour):
   each round found a real false accept (list-based isolation, description-based subtraction, a
   through anchor bridging layers). Cause: `kicad-cli` lists nothing for a lone pad plus a zone.
   Escalated by the stop rule above.
3. **Reviews found fail-open holes in every module** (11 of 11 critical modules were not clean at
   first review; typical: missing information treated as "fine"). The checklist that works:
   unknown, absent, unparsed or unreachable means incomplete, never pass; read every fake.
4. **Test and tooling facts that bit more than once:** `kicad_tools` serialises floats at 6
   significant digits; `kicad-cli` descriptions are localised (German here) so parse structure,
   not words; `PCB.save` re-serialises whole files; scratch directories are shared between agents
   (one reviewer's `rm -rf` of `copy` hit another's files; each agent now uses its own prefix).
5. **A builder reported "not RUN" instead of faking a measurement** (ticket 10): the right outcome
   when tools were missing; it produced the gap list that became tickets 17-20.
6. **Three tickets reached the stop rule in one night** (2026-10-21; build tickets 36, 67 and 74).
   Nearly every delta review opened a new class of edge case rather than finding an earlier finding
   still open:
   - 36: pattern syntax, then ASCII escapes and anchoring;
   - 67: aliases, then star imports and dynamic access;
   - 74: variable paths, then wrappers in the helper, then aliases, then a re-export through an
     exempt file.

   The final blockers were real: a false refusal reason, ordinary lazy imports, and a wrapper that
   escapes an exemption. The rounds before them, though, mixed blockers with FOLLOW-UP work. That
   led to the rule "What a finding blocks".

## Open bootstrap items (section 12)

Built by build ticket 45 (`paradigm-review-2` ticket 13): the `.claude/agents/` definitions
(P5), the git guardrail and the pre-push guard (P8), `ruff` with a shrink-only baseline (P6), and
the batch gate with its known-red file (P11). There is no hosted CI (owner, 2026-10-19). Still
open: installing the pinned `ruff` into the shared `.venv` and the pre-push guard into the common
git dir (coordinator, once, after the merge); the `grilling` opt-out line. No `land.sh`: merges
are local (P12). The shared helper for the checkpoint boilerplate copied across
`input_prep.py`, `schematic_import.py`, `board_rules.py`, `board_outline.py` and `silk_minimums.py`
is build ticket 22, unblocked since 2026-10-19; it carries the self-check and the drift-guard token.
