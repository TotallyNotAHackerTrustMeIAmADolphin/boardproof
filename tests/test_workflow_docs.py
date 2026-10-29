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
HARD_RULES_SHA256 = "a7f91ac7b37308cbb91e4ac58ec87368b4699af97a724e0c7b6324106f006084"


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


def test_agents_and_playbook_do_not_send_readers_to_scratch():
    for rel in (".claude/agents/builder.md", ".claude/agents/reviewer.md",
                ".claude/agents/researcher.md"):
        assert OLD_PLANNING_DIR not in _text(rel), rel
    fork = _text("docs/playbook/PROJECT-KICAD-MCP-PLAYBOOK.md")
    assert "scripts/issues.py show <n>" in fork
    assert "Worktrees need no special handling for planning records any more" in fork
    assert "Files, not GitHub" not in fork
    assert "tickets in `.scratch/" not in fork
    generic = _text("docs/playbook/AGENT-WORKFLOW-PLAYBOOK.md")
    assert "Record the\n  Answer as an issue comment" in generic
