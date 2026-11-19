"""Publish-prep ticket 03a, part C: the issue workflow is written down and CLAUDE.md keeps its rules.

CLAUDE.md is critical-class text. The hard-rules block is pinned by hash (taken from the text as
merged before this ticket), so a reword of rules 1 to 4 turns this test red; every other rule bullet
of the working convention is pinned by its opening words.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD_PLANNING_DIR = "." + "scratch"  # built, so this file names no quoted path
# Changed once on purpose (tidy, 2026-10-29): rule 3 names the testing doc by its path under docs/; nothing else.
HARD_RULES_SHA256 = "fb8db188fca2949d8b1de733849984e205a6a69190995f63f8ad84de66cd2244"


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_hard_rules_are_byte_identical():
    text = _text("CLAUDE.md")
    block = text[text.index("## Hard rules") : text.index("## Working convention")]
    assert hashlib.sha256(block.encode()).hexdigest() == HARD_RULES_SHA256


def test_every_other_working_rule_is_still_there():
    text = _text("CLAUDE.md")
    for opening in (
        "- **Work a map's frontier in parallel.**",
        "- **Every build ticket gets an adversarial review.**",
        "- **Record findings immediately**",
        "- **Commit after every closed ticket and push right away**",
        "- **A rule that was broken once becomes a test or a hook.**",
        "- **Batch tools, not one-item-per-call.**",
        "- **Ask the user only when truly blocked**",
        "- **Don't over-wait.**",
        "- Use the `mcp-builder` skill when defining tool schemas.",
        "The stop rule never permits a merge without CLEAN.",
        "stop after the third delta review and escalate",
        "Never force-push, never delete `main`.",
    ):
        assert opening in text, opening


def test_claude_md_points_to_the_issue_workflow_and_not_to_scratch():
    text = _text("CLAUDE.md")
    assert "`docs/WORKFLOW.md`" in text
    assert "scripts/issues.py" in text
    assert OLD_PLANNING_DIR not in text
    assert "Planning runs through Wayfinder" not in text


def test_workflow_doc_states_the_convention():
    text = _text("docs/WORKFLOW.md")
    for phrase in (
        "one issue labelled `map`",
        "`build`", "`research`", "`decision`", "`task`",
        "`critical`", "`parked`", "`needs-owner`",
        "`Blocked by #n`",
        "open or closed",
        "reviewed (full) sha",
        "BLOCKS",
        "FOLLOW-UP",
        "stop rule",
        "BOARDPROOF_FORBIDDEN_FILE",
        "BOARDPROOF_ALLOW_UNCHECKED",
        "--no-identity-check",
        "which is not a pass",
        "`BOARDPROOF_REPO`",
        "run `label-sync` first",
        "formerly build ticket",
    ):
        assert phrase in text, phrase
    assert OLD_PLANNING_DIR not in text


def test_agent_rules_and_claude_md_do_not_send_readers_to_scratch():
    # Issue 155: the agent rules moved to docs/dispatch, the project's answers into CLAUDE.md.
    rules = ("docs/dispatch/builder.md", "docs/dispatch/reviewer.md", "docs/dispatch/researcher.md",
             "docs/dispatch/review-rules.md", "docs/dispatch/briefs/builder.md",
             "docs/dispatch/briefs/reviewer.md")
    for rel in rules:
        assert OLD_PLANNING_DIR not in _text(rel), rel
    claude = _text("CLAUDE.md")
    assert "scripts/issues.py show <n>" in _text("docs/dispatch/builder.md")
    assert "Planning and research tickets need no worktree" in claude
    for text in (claude, *(_text(rel) for rel in rules)):
        assert "Files, not GitHub" not in text
        assert "tickets in `.scratch/" not in text
    # The Answer is an issue comment, not a file (the plugin's builder says it too; the project's
    # own rule is pinned here so a submodule bump cannot change it).
    builder = " ".join(_text("docs/dispatch/builder.md").split())
    assert "post it as a comment on the issue" in builder
    assert "Answer is an issue comment, not a file" in builder


def test_contributing_names_the_submodule_fetch():
    text = " ".join(_text("CONTRIBUTING.md").split())
    assert "`git submodule update --init` fetches `docs/playbook`" in text
