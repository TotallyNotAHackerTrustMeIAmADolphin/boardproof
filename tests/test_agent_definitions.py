"""Build ticket 45: the agent definitions in `.claude/agents/` carry this project's contracts.

The definitions are text an LLM reads at runtime (critical class, fork P7). This guard keeps the
contracts in them: losing one of these anchors needs a deliberate edit of this test.
"""

from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pytest

OLD_PLANNING_DIR = "." + "scratch"  # built, so this file names no quoted path
AGENTS = Path(__file__).resolve().parents[1] / ".claude" / "agents"

REQUIRED = {
    "builder": [
        "kicad_mcp.__file__",  # the worktree test check (fork P6)
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
        # build ticket 66: targeted runs stay in the foreground; the gate (about 23 minutes, beyond
        # the Bash tool's 600 s limit) runs in the background and is waited for
        "Targeted test runs: from the worktree root, in the foreground under `timeout`, never as a background command",  # noqa: E501
        "the batch gate, `python <worktree>/scripts/batch_gate.py`, on a clean committed tree",
        "the harness notifies you when it exits",
        "no polling, no `sleep`",
        "no second run",
        "never edit the worktree while it runs (no file edit, commit, checkout or merge)",
        "a run with edits during it is void; run it again on the new head",
        # review round 1, finding 1: precedence over the generic playbook (A1, resources, template)
        "this file and the project fork override the generic playbook's rules on background commands",  # noqa: E501
        "(A1, the resources table's background-jobs row, the sub-agent template)",
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
        "git -C",
        "archive",
        "kicad_mcp.__file__",
        "rootdir:",
        "Mutation",
        "write_not_verified",
        "tests/ratchets.toml",
        "verify_board",
        "lint_gate.py",
        # review round 1: the lint gate runs ruff from the interpreter that starts it
        '.venv/Scripts/python.exe" <export>/scripts/lint_gate.py',
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
    text = path.read_text(encoding="utf-8")
    assert text.startswith("---\n") or text.startswith("---\r\n"), f"{path.name}: no frontmatter"
    _, head, body = text.split("---", 2)
    meta: dict[str, str] = {}
    for line in head.strip().splitlines():
        key, sep, value = line.partition(":")
        assert sep, f"{path.name}: frontmatter line without a colon: {line!r}"
        meta[key.strip()] = value.strip()
    return meta, body


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
def test_definition_exists_and_carries_its_contracts(name):
    meta, body = _parse(AGENTS / f"{name}.md")
    assert meta["name"] == name
    assert meta["description"]
    assert int(meta["maxTurns"]) > 0
    flat = _norm(body)
    missing = [anchor for anchor in REQUIRED[name] if _norm(anchor) not in flat]
    assert not missing, f"{name}.md lost its contracts: {missing}"


@pytest.mark.parametrize("name", sorted(REQUIRED))
def test_no_builtin_worktree_isolation(name):
    # Worktrees are made by hand from local main (fork P5); isolation: worktree bases on origin/main.
    meta, _ = _parse(AGENTS / f"{name}.md")
    assert "isolation" not in meta


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


def test_targeted_runs_stay_in_the_foreground():
    _, body = _parse(AGENTS / "builder.md")
    targeted = [b for b in _top_level_bullets(body) if b.startswith("- Targeted test runs:")]
    assert len(targeted) == 1
    assert "in the foreground under `timeout`, never as a background command" in targeted[0]
    assert "run_in_background" not in targeted[0]


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


# Review round 1, finding 1: the project fork agrees with builder.md on background runs.
FORK = Path(__file__).resolve().parents[1] / "docs" / "playbook" / "PROJECT-KICAD-MCP-PLAYBOOK.md"


def _fork() -> str:
    # The fork's rules are Markdown block quotes; drop the `> ` markers before normalising.
    lines = FORK.read_text(encoding="utf-8").splitlines()
    return _norm("\n".join(line[1:] if line.startswith(">") else line for line in lines))


@pytest.mark.parametrize(
    "anchor",
    [
        # P6
        "Targeted test runs in the foreground, with timeouts; the batch gate (P11) runs in the background",  # noqa: E501
        # P11
        "run by the builder at the end of its ticket and by the coordinator before a merge",
        "so it runs in the background (`.claude/agents/builder.md`, Tests)",
        # the builder dispatch template
        "targeted tests in the foreground and the batch gate in the background",
    ],
)
def test_fork_playbook_agrees_on_background_gate(anchor):
    assert _norm(anchor) in _fork()


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
    ],
)
def test_fork_playbook_carries_the_finding_rule(anchor):
    assert _norm(anchor) in _fork()


@pytest.mark.parametrize(
    "stale",
    [
        # superseded by "What a finding blocks"; a FOLLOW-UP's test is written in its own ticket
        "Each finding becomes a failing test before its fix",
        "a finding from an earlier round that is not closed",
    ],
)
def test_fork_playbook_drops_the_unclassed_finding_rule(stale):
    assert _norm(stale) not in _fork()


def test_claude_md_points_to_the_finding_rule():
    text = _norm((FORK.parents[2] / "CLAUDE.md").read_text(encoding="utf-8"))
    assert _norm("CLEAN means no BLOCKS finding") in text
    assert _norm("A BLOCKS finding becomes a failing test before its fix") in text
    assert _norm("FOLLOW-UPs go into build tickets at the merge") in text
    assert _norm("stop after the third delta review and escalate") in text
    assert _norm("The stop rule never permits a merge without CLEAN") in text
    assert _norm("Each finding becomes a failing test before its fix") not in text


def test_fork_header_names_the_background_gate_exception():
    # Fix round 2, finding 1: both headers let the general file win on general rules, so A1
    # (and its "rerun in the foreground" nudge) would still win without this exception.
    header = _fork().split("## Section 0", 1)[0]
    assert _norm(
        "Except: P6 and P11 override A1, the resources table's background-jobs row and the "
        "sub-agent template for the batch gate, which runs in the background (build ticket 66)."
    ) in header


GENERIC = FORK.with_name("AGENT-WORKFLOW-PLAYBOOK.md")


def test_generic_header_points_to_the_fork_exception():
    lines = GENERIC.read_text(encoding="utf-8").splitlines()
    header = _norm("\n".join(line.lstrip("> ") for line in lines[:8]))
    assert (
        "This file wins on general rules, the fork on project values, except where the fork's "
        "header names an exception." in header
    )


@pytest.mark.parametrize(
    "stale",
    [
        "Foreground, with timeouts.",  # P6 before ticket 66
        "the coordinator starts it in the background",  # P11 before ticket 66
        "foreground tests; one genuine line",  # the dispatch template before ticket 66
    ],
)
def test_fork_playbook_drops_the_foreground_only_gate(stale):
    assert _norm(stale) not in _fork()


def test_builder_cannot_start_agents():
    meta, _ = _parse(AGENTS / "builder.md")
    assert "Agent" in meta["disallowedTools"]


def test_agents_read_and_record_tickets_as_issues():
    """Publish-prep ticket 03a: a ticket is a GitHub issue; no agent definition points at .scratch."""
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
    for path in sorted(AGENTS.glob("*.md")):
        assert OLD_PLANNING_DIR not in path.read_text(encoding="utf-8"), path.name
