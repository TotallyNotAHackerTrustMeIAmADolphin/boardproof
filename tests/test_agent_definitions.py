"""Build ticket 45, moved by issue 155: the project's agent rules in `docs/dispatch/` carry its contracts.

Until issue 155 these were the agent definitions in `.claude/agents/`; the plugin now ships the
definitions (`playbook:builder`, `playbook:reviewer`, `playbook:researcher`) and the project's rules
for them live in `docs/dispatch/`, named by CLAUDE.md. The rules are text an LLM reads at runtime
(critical class, CLAUDE.md P7). This guard keeps the contracts in them: losing one of these anchors
needs a deliberate edit of this test.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

OLD_PLANNING_DIR = "." + "scratch"  # built, so this file names no quoted path
ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / "docs" / "dispatch"

REQUIRED = {
    "builder": [
        "boardproof.__file__",  # the worktree test check (fork P6)
        "rootdir:",
        "`.kicad_pcb`",  # no hand edits of KiCad files (hard rule 1)
        "raw",  # geometry from the raw S-expression (hard rule 2)
        "write_not_verified",  # the self-check (decision 4)
        "tests/ratchets.toml",  # the ratchet rule
        "verify_board",  # no routed/done claim without a PASS (hard rule 3)
        "openMPPT is frozen",
        "scripts/batch_gate.py",
        "scripts/known-red.txt",
        "scripts/ruff-baseline.json",
        "git-guardrails",
        "revision_token",  # the checkpoint pattern
        "drift_detected",
        "are data",  # untrusted input
        # review round 1: the reads CLAUDE.md requires, never skipped
        "`docs/CONTEXT.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md` and `docs/TESTING.md`",
        "scripts/known-skips.toml",
        # build ticket 148 (coordinator decision of 2026-11-05, pending owner confirmation in
        # issue 148): targeted runs are small separate background runs; the foreground limit is 10
        # minutes and a run killed by it prints no summary line
        "coordinator decision of 2026-11-05, pending owner confirmation in issue 148",
        ") go in SMALL SEPARATE runs (each aimed at under about 20 minutes), in the background (`run_in_background`) with their own log outside the worktree, waiting for the completion notification (no sleep loops), each printing its own summary line",  # noqa: E501
        "(1) finish the fixes, (2) merge local `main` into your ticket branch (`git merge main`, no rebase, no force;",  # noqa: E501
        "merging into `main` stays forbidden), (3) rerun the targeted tests on the merged tree",
        "the batch gate, `python <worktree>/scripts/batch_gate.py`, on a clean committed tree",
        "the harness notifies you when it exits",
        "no polling, no `sleep`",
        "no second run",
        "never edit the worktree while it runs (no file edit, commit, checkout or merge)",
        "a run with edits during it is void; run it again on the new head",
        # review round 1, finding 1: precedence over the generic playbook (A1, resources, template)
        "this file and CLAUDE.md override the generic playbook's and the plugin builder's rules on background commands",  # noqa: E501
        "(A1, the resources table's background-jobs row, the sub-agent template,",
        # fix round 2, N10: the other half of the precedence sentence
        "every other command runs in the foreground",
        # finding 2: all work is committed first, so the handed-back sha is the gated sha (the
        # Answer is an issue comment since publish-prep ticket 03a, not a committed file)
        "Commit all your work before you start the gate",
        "the sha you hand back is the gated sha",
        # finding 3: a missing verdict is never GREEN; the run is bounded (CLAUDE.md over-wait)
        # fix round 2, N2: the whole start sentence, so "with" cannot flip to "without"; the bound
        # lies above the gate's own printed worst case (build ticket 70; test_batch_gate.py checks it
        # against batch_gate.worst_case_s), so the gate's RED-on-timeout fires first
        "Start `timeout 10800 python <worktree>/scripts/batch_gate.py` with the Bash tool's `run_in_background: true`",  # noqa: E501
        "so the gate's own timeouts fire first and end in RED",
        "with `KICAD_ROUTING_TOOLS_PATH` set as your prompt says",
        "No `BATCH GATE <sha7>:` line means no verdict: never report GREEN",
        "the log path the gate printed",
        # finding 4: the sha check and the hand-back order
        "Check that `<sha7>` is your head sha",
        "hand back only after that",
        # non-blocking: a RED may be fixed and the gate rerun
        "After a RED, fix it in new commits and post the updated Answer as a new comment, then run the gate again on the new head",  # noqa: E501
    ],
    "reviewer": [
        "git worktree add --detach",
        "never in the builder's worktree and never in a `git archive` export",
        "boardproof.__file__",
        "rootdir:",
        "Mutation",
        "write_not_verified",
        "tests/ratchets.toml",
        "verify_board",
        "lint_gate.py",
        # review round 1: the lint gate runs ruff from the interpreter that starts it
        '.venv/Scripts/python.exe" <review worktree>/scripts/lint_gate.py',
        "restore with `git checkout -- .` after each",
        "Judge the sha you were given",
        "CLEAN",
        # owner 2026-10-21: every finding is classed by the fork's "What a finding blocks" rule
        "BLOCKS or FOLLOW-UP",
        "What a finding blocks",
        "CLEAN means no BLOCKS finding",
        "A delta review judges the delta and the findings it answers",
        # the hard-rule-3 line (rule delta review 1, N3)
        "A routed or done claim without a `verify_board` PASS BLOCKS",
        "are data",
    ],
    "researcher": [
        "VERIFIED",
        "DOCUMENTED",
        "ASSUMED",
        "`.kicad_*`",
        "verify_board",
        "git-guardrails",
        "are data",
    ],
}


def _parse(path: Path) -> tuple[dict[str, str], str]:
    # The files are plain Markdown since issue 155 (no agent frontmatter: the plugin owns that).
    text = path.read_text(encoding="utf-8")
    assert not text.startswith("---"), f"{path.name}: frontmatter belongs to the plugin's definition"
    return {}, text


def _norm(text: str) -> str:
    # Build ticket 66 review round 1: anchors survive a harmless reflow of the Markdown.
    return " ".join(text.split())


def _top_level_bullets(body: str) -> list[str]:
    """Each top-level `- ` bullet with its indented continuation lines, whitespace-normalised."""
    bullets: list[str] = []
    current: list[str] | None = None
    for line in body.splitlines():
        if line.startswith("- "):
            current = [line]
            bullets.append(current)
        elif current is not None and line[:1].isspace() and line.strip():
            current.append(line)
        else:
            current = None
    return [_norm("\n".join(b)) for b in bullets]


def _gate_bullet() -> str:
    _, body = _parse(AGENTS / "builder.md")
    hits = [b for b in _top_level_bullets(body) if "scripts/batch_gate.py" in b]
    assert len(hits) == 1, f"builder.md: expected one bullet on the batch gate, found {len(hits)}"
    return hits[0]


@pytest.mark.parametrize("name", sorted(REQUIRED))
def test_rules_exist_and_carry_their_contracts(name):
    _, body = _parse(AGENTS / f"{name}.md")
    flat = _norm(body)
    missing = [anchor for anchor in REQUIRED[name] if _norm(anchor) not in flat]
    assert not missing, f"{name}.md lost its contracts: {missing}"


def test_builder_does_not_tell_agents_to_skip_required_reads():
    # Review round 1: "never whole" contradicted CLAUDE.md's required reading list.
    _, body = _parse(AGENTS / "builder.md")
    assert "never whole" not in body


@pytest.mark.parametrize(
    "stale",
    [
        "do not start it in the background",  # the pre-66 gate rule
        "timeout at its maximum",  # the gate never fits the foreground limit
    ],
)
def test_builder_does_not_keep_the_gate_in_the_foreground(stale):
    # Build ticket 66: the gate takes longer than the 600 s foreground limit, so the old rule left
    # builders without a verdict (build ticket 63 ran the suite in three parts).
    _, body = _parse(AGENTS / "builder.md")
    assert stale.lower() not in _norm(body).lower()


# Review round 1, finding 4, and fix round 2 (N1, N2): any wording that sends the gate back to
# the foreground.
_GATE = r"(it|the (batch )?gate)"
BACK_TO_FOREGROUND = re.compile(
    "|".join([
        rf"\b(do not|don't|never)\s+(start|run)\s+{_GATE}\s+in\s+the\s+background",
        rf"\b(run|start)\s+{_GATE}\s+in\s+the\s+foreground",
        rf"\b{_GATE}\s+(runs|starts)\s+in\s+the\s+foreground",
        r"\bwithout\s+(the\s+bash\s+tool's\s+)?`?run_in_background",
    ]),
    re.IGNORECASE,
)


@pytest.mark.parametrize(
    "text",
    [
        "Do not start it in the background.",  # M11
        "Never run the batch gate in the background.",  # M12
        "- Run the batch gate in the foreground under `timeout`.",  # N1
        "Start it without the Bash tool's `run_in_background: true`.",  # N2
        "The gate runs in the foreground.",
    ],
)
def test_back_to_foreground_pattern_catches_known_wordings(text):
    assert BACK_TO_FOREGROUND.search(text)


def test_builder_never_forbids_the_gate_in_the_background():
    _, body = _parse(AGENTS / "builder.md")
    assert not BACK_TO_FOREGROUND.search(_norm(body))


def test_gate_bullet_runs_in_the_background_and_never_in_the_foreground():
    bullet = _gate_bullet()
    assert "`run_in_background: true`" in bullet
    assert "foreground" not in bullet.lower()


def test_targeted_runs_are_small_separate_background_runs():
    _, body = _parse(AGENTS / "builder.md")
    targeted = [b for b in _top_level_bullets(body) if b.startswith("- Targeted test runs")]
    assert len(targeted) == 1
    assert "`run_in_background`" in targeted[0]
    assert "never as a background command" not in targeted[0]


def test_reviewer_does_not_use_an_export():
    _, body = _parse(AGENTS / "reviewer.md")
    assert "your own export" not in body
    assert "<export>" not in body


def test_anchors_survive_a_harmless_reflow():
    # Rewrapping the Markdown must keep every contract test green. The reflow must break raw
    # substring matching (otherwise this test proves nothing); normalising must repair it.
    _, body = _parse(AGENTS / "builder.md")
    reflowed = "\n  ".join(textwrap.wrap(body, width=40, break_long_words=False, break_on_hyphens=False))
    broken_raw = [anchor for anchor in REQUIRED["builder"] if anchor not in reflowed]
    assert len(broken_raw) >= 10, broken_raw
    flat = _norm(reflowed)
    assert [anchor for anchor in REQUIRED["builder"] if _norm(anchor) not in flat] == []
    assert not BACK_TO_FOREGROUND.search(flat)


# Review round 1, finding 1: CLAUDE.md, the rule files and the briefs agree on background runs.
CLAUDE = ROOT / "CLAUDE.md"
HISTORY = ROOT / "docs" / "HISTORY.md"
RULES = AGENTS / "review-rules.md"
BRIEFS = AGENTS / "briefs"


def _read(path: Path) -> str:
    # Markdown block quotes lose their `> ` markers before normalising.
    lines = path.read_text(encoding="utf-8").splitlines()
    return _norm("\n".join(line[1:] if line.startswith(">") else line for line in lines))


def _section(path: Path, heading: str) -> str:
    text = _read(path)
    assert heading in text, f"{path.name}: no section {heading!r}"
    return text.split(heading, 1)[1].split(" ## ", 1)[0]


@pytest.mark.parametrize(
    ("path", "anchor"),
    [
        # P6 (CLAUDE.md since issue 155)
        (CLAUDE, "Targeted runs: small separate background runs (each aimed at under about 20 minutes, own log outside the worktree, own summary line, wait for the notification;"),  # noqa: E501
        # P11 (docs/HISTORY.md keeps the pinned text)
        (HISTORY, "run by the builder at the end of its ticket and by the coordinator before a merge"),
        (HISTORY, "so it runs in the background (`docs/dispatch/builder.md`, Tests)"),
        (CLAUDE, "run by the builder at the end of its ticket on a committed, clean tree in the background (`docs/dispatch/builder.md`, Tests)"),  # noqa: E501
        # the builder dispatch template
        (BRIEFS / "builder.md", "targeted tests in small separate background runs and the batch gate in the background"),
    ],
)
def test_the_rules_agree_on_background_gate(path, anchor):
    assert _norm(anchor) in _read(path)


@pytest.mark.parametrize(
    "anchor",
    [
        # owner 2026-10-21: what a finding blocks; review of af12140, finding B8
        "CLEAN means no BLOCKS finding",
        "a FOLLOW-UP never blocks",
        "an acceptance item not met",
        "Shrinking a claim never closes one",
        "a fail-open path in code the ticket added or changed",
        "unless the delta causes it",
        "A finding that fits both lists BLOCKS",
        "one the reviewer and the coordinator class differently",
        "Forms ordinary code uses",
        "Each BLOCKS finding becomes a failing test before its fix",
        "never permits a merge without a CLEAN verdict",
        # rule delta review 1: N1, N2, N3 and follow-ups 4, 5
        "a breach of a CLAUDE.md hard rule",
        "a false claim in the lines the ticket wrote or changed",
        "a regression the ticket's commits cause",
        "a guard or acceptance test that stays green when the key line it guards is broken",
        "no reviewer is dispatched again without the owner",
        "Builders fix BLOCKS findings",
        "says it does not defend against evasion",
        "An evasion form of a static scan or ratchet (FOLLOW-UP list) is not a fail-open path",
        "An input the code refuses with a true reason meets the item",
        "never a hard-rule breach",
        # rule delta review 2: D1, D2 and follow-ups 1, 3, 4
        "not built, or wrong for an input",
        "until it does, the claim is false and BLOCKS",
        "A second fail-open or wrong value of the same family makes the design itself the finding",
        "stays green when the key line it guards is broken",
        "a runtime guard has no such exemption",
        # follow-up rule (owner, 2026-11-11): reviewer fixes land in the same ticket
        "in the same fix round as the BLOCKS findings",
        "30 lines or fewer without new logic",
        "A new ticket is opened only for new knowledge",
        "by scope decision (an exception to the FOLLOW-UP rule above",
        # issue 155: the contract list the rule names lives in the reviewer's rule file now
        "the STANDARDS list of `docs/dispatch/reviewer.md`",
    ],
)
def test_the_review_rules_carry_the_finding_rule(anchor):
    assert _norm(anchor) in _read(RULES)


@pytest.mark.parametrize(
    "stale",
    [
        # superseded by "What a finding blocks"; a FOLLOW-UP's test is written in the same fix round
        "Each finding becomes a failing test before its fix",
        "a finding from an earlier round that is not closed",
    ],
)
def test_the_review_rules_drop_the_unclassed_finding_rule(stale):
    assert _norm(stale) not in _read(RULES)


def test_claude_md_points_to_the_finding_rule():
    text = _norm(CLAUDE.read_text(encoding="utf-8"))
    assert _norm("CLEAN means no BLOCKS finding") in text
    assert _norm("A BLOCKS finding becomes a failing test before its fix") in text
    assert _norm("FOLLOW-UPs go into build tickets at the merge") not in text
    assert _norm("A FOLLOW-UP that is a pure fix is fixed in the same ticket before the merge") in text
    assert _norm("stop after the third delta review and escalate") in text
    assert _norm("The stop rule never permits a merge without CLEAN") in text
    assert _norm("Each finding becomes a failing test before its fix") not in text
    assert "`docs/dispatch/review-rules.md`" in text


def test_claude_md_overrides_name_targeted_runs_and_the_reviewer_worktree():
    header = _section(CLAUDE, "## Overrides of the playbook")
    assert _norm("and for targeted test runs, which go in small separate background runs") in header
    assert _norm("coordinator decision of 2026-11-05, pending owner confirmation in issue 148") in header
    assert _norm("This file also overrides A2 and every generic rule that has the reviewer work in a `git archive` export") in header  # noqa: E501
    assert _norm("a reviewer works in its own detached git worktree of the reviewed sha") in header


def test_reviewer_dispatch_rule_names_the_review_worktree():
    text = _read(BRIEFS / "reviewer.md")
    assert "scratch-copy-only" not in text
    bullet = text.split("## Every reviewer dispatch adds (coordinator)", 1)[1]
    assert _norm("Mutation testing in the reviewer's own review worktree") in bullet
    assert "`git checkout -- .`" in bullet
    # P6's example uses the background form, not the foreground pipe
    for path in (CLAUDE, HISTORY, *AGENTS.glob("**/*.md")):
        assert "pytest -q <paths> 2>&1 | tail -15" not in _read(path), path


def test_no_unrecorded_owner_decision_claim():
    # The targeted-run rule is a coordinator decision pending owner confirmation (issue 148).
    # CLAUDE.md legitimately quotes recorded owner decisions (2026-10-19 and others), as before issue 155.
    for path in AGENTS.glob("**/*.md"):
        assert "owner decision" not in _norm(path.read_text(encoding="utf-8")).lower(), path


def test_claude_md_names_the_background_gate_exception():
    # Fix round 2, finding 1: the general file would win on A1 (and its "rerun in the foreground"
    # nudge) without this exception.
    header = _section(CLAUDE, "## Overrides of the playbook")
    assert _norm(
        "Except: P6 and P11 override A1, the resources table's background-jobs row, the sub-agent "
        "template and the plugin builder's \"foreground under `timeout`, never a background command\" "
        "for the batch gate, which runs in the background (build ticket 66)"
    ) in header


@pytest.mark.parametrize(
    "stale",
    [
        "Foreground, with timeouts.",  # P6 before ticket 66
        "the coordinator starts it in the background",  # P11 before ticket 66
        "foreground tests; one genuine line",  # the dispatch template before ticket 66
    ],
)
def test_the_rules_drop_the_foreground_only_gate(stale):
    for path in (CLAUDE, RULES, *AGENTS.glob("*.md"), *BRIEFS.glob("*.md")):
        assert _norm(stale) not in _read(path), path


def test_builder_cannot_start_agents():
    # The plugin owns the definition (issue 155); pin its frontmatter when the submodule is checked out.
    plugin = ROOT / "docs" / "playbook" / "plugin" / "agents" / "builder.md"
    if not plugin.is_file():
        pytest.skip("docs/playbook submodule is not initialised (git submodule update --init): "
                    "the plugin's builder definition is not on disk")
    head = plugin.read_text(encoding="utf-8").split("---", 2)[1]
    disallowed = next(line for line in head.splitlines() if line.startswith("disallowedTools:"))
    assert "Agent" in disallowed


def test_agents_read_and_record_tickets_as_issues():
    """Publish-prep ticket 03a: a ticket is a GitHub issue; no rule file points at .scratch."""
    builder = _norm((AGENTS / "builder.md").read_text(encoding="utf-8"))
    for phrase in (
        "with `python scripts/issues.py show <n>`",
        "post it as a comment on the issue",
        "python scripts/issues.py comment <n> --body-file <file>",
    ):
        assert _norm(phrase) in builder, phrase
    reviewer = _norm((AGENTS / "reviewer.md").read_text(encoding="utf-8"))
    assert _norm("as a comment on the ticket's issue") in reviewer
    researcher = _norm((AGENTS / "researcher.md").read_text(encoding="utf-8"))
    assert _norm("python scripts/issues.py show <n>") in researcher
    for path in sorted(AGENTS.glob("**/*.md")):
        assert OLD_PLANNING_DIR not in path.read_text(encoding="utf-8"), path.name


def test_hand_made_worktrees_are_history_only():
    # Issue 155: the plugin builder makes the worktree from a fresh origin/main (CLAUDE.md P5).
    for line in __import__("subprocess").run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True,
                                             check=True).stdout.splitlines():
        if line in {"docs/HISTORY.md", "tests/test_agent_definitions.py"} or line.startswith("docs/playbook"):
            continue
        path = ROOT / line
        if path.suffix in {".md", ".py", ".toml", ".json"} and path.is_file():
            text = _norm(path.read_text(encoding="utf-8", errors="ignore"))
            assert "hand-made worktree" not in text, line


def test_claude_md_overrides_name_no_prs_and_the_dispatch_files():
    text = _read(CLAUDE)
    section = _section(CLAUDE, "## Overrides of the playbook")
    assert _norm("`gh pr create` and the \"report: PR number\" of the plugin's builder text are replaced by P12") in section
    assert "push your own `ticket-<n>`" in section and "head sha" in section
    assert _norm("`git merge origin/main`") in section and "quiet" in section
    assert "opus" not in text.split("- P5:", 1)[1].split("- P6:", 1)[0]
    assert "verifies the model of the first plugin dispatch" in text


def test_history_points_to_the_open_owner_decision_and_marks_the_settled_list_as_history():
    text = _read(HISTORY)
    assert "issue #7" in text and "`needs-owner`" in text
    assert "history only" in text
