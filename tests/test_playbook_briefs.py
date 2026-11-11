"""The standing brief templates exist and carry the fail-open checklist and the hand-back rules.

Drift guard for build ticket 148: the playbook fork section and both templates under
docs/playbook/briefs must keep the required sentences. The reviewer-worktree and merge-order
sentences must also match the agent definitions word for word (one source of truth).
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRIEFS = ROOT / "docs" / "playbook" / "briefs"
AGENTS = ROOT / ".claude" / "agents"
FORK = ROOT / "docs" / "playbook" / "PROJECT-KICAD-MCP-PLAYBOOK.md"
HEADING = "## Fail-open checklist for sheet writing tools"

# Checklist items 1-14 and the closing rule, as phrases that appear in both templates.
CHECKLIST = [
    "multi-unit part",
    "each unit's uuid",
    "KiCad's pick of a multi-unit part's properties is undetermined",
    "same reference on another sheet",
    "subfolder",
    "root_sch",
    "a call never waits for a file above its own parent (the rule in `sheet_pins._run`)",
    "hidden pins and power symbols",
    "lib_name symbols",
    "every self-check key",
    "goes red when that line is broken",
    "a no-op or refusal writes nothing",
    "file bytes compared",
    "the result must not say ok when the netlist or ERC did not follow: read back what kicad-cli reports. A mismatch returns `ok: false` with `write_not_verified`.",  # noqa: E501
    "no-connect markers and local power symbols change which pins count as members of a net",
    "label scope: local, global and child-sheet labels",
    "text input classes: line breaks, Unicode separators, edge whitespace in names",
    "a refusal reason or tool text that is false for a case the tool handles",
    "a spec guard that now refuses forms the base accepted",
    "an unreadable tree file or an undiscoverable root must refuse or note, never silently fall back to one sheet",  # noqa: E501
    "a tool that deletes has a self-check",
    "A second fail-open of the same family means refuse everything not proven.",
]

# Sentences both templates carry (whole sentences, so removing one goes red).
COMMON = [
    "Report ONCE, then stop.",
    "If the hand-back tool fails, retry the same tool once, then state the report as the final message; do not invent another channel.",  # noqa: E501
    "Use private log and scratch file names that contain your ticket number",
    "keep log paths and undo patches OUTSIDE every worktree (the scratchpad directory the session names, or a `*.log` file in the directory that holds the ticket worktrees), never in the tree: a dirty tree voids the gate's clean-tree check.",  # noqa: E501
    "Never kill a process you did not start; if a foreign process blocks you (lock, port), report it to the coordinator.",  # noqa: E501
    "Run tests as SMALL SEPARATE runs (each aimed at under about 20 minutes), in the background (`run_in_background`) with their own log, waiting for the completion notification (no sleep loops), each printing its own summary line.",  # noqa: E501
    "While a batch gate runs, start no more than 2 other runs",
    "A result counts only if `boardproof.__file__` lies inside the worktree and pytest's `rootdir:` line names it; report both.",  # noqa: E501
]

BUILDER_ONLY = [
    "(1) finish the fixes, (2) merge local `main` into your ticket branch (`git merge main`, no rebase, no force; this is allowed and counts as the prompt's merge instruction, merging into `main` stays forbidden), (3) rerun the targeted tests on the merged tree, (4) the batch gate where one applies, on a clean tree, (5) hand back the head sha, which is the gated sha.",  # noqa: E501
    "If `main` moves after the hand-back the coordinator decides whether a re-merge is needed.",
    "coordinator decision of 2026-11-05, pending owner confirmation in issue 148",
]

REVIEWER_ONLY = [
    "Review the full sha your prompt names in your OWN detached git worktree of that sha, never in the builder's worktree and never in a `git archive` export",  # noqa: E501
    "`git worktree add --detach <worktrees dir>/rev-<ticket>-<sha7> <sha>`; remove it at the end.",
    "restore with `git checkout -- .` after each. Never mutate a worktree while a batch gate runs on it.",
    "Judge the sha you were given. If `main` moved since, that is not a finding unless it conflicts.",
    "Never mutate while ANY run on that worktree is in progress; restore only after its completion notification.",
    "If `git worktree add` fails, report it to the coordinator, or use a `git init` copy with `git add -A` and a committed baseline, labelled as such in your report; never silently an export without `.git`.",  # noqa: E501
    "limit the delta to the ticket's own paths (`git diff <reviewed> <head> -- <ticket paths>`, or the commits since the reviewed sha authored for the ticket). Report a merge conflict; never resolve it.",  # noqa: E501
    "`git worktree add` and `git worktree remove` write only metadata under `.git`.",
]

PLACEHOLDERS = ["<TICKET>", "<WORKTREE>", "<MERGE INSTRUCTION>"]


def _flat(text: str) -> str:
    return " ".join(text.split())


def _read(path: Path) -> str:
    assert path.is_file(), f"{path} is missing"
    return _flat(path.read_text(encoding="utf-8"))


def _all_sentences() -> list[tuple[str, str]]:
    cases = [("builder.md", s) for s in CHECKLIST + COMMON + BUILDER_ONLY + PLACEHOLDERS]
    cases += [("reviewer.md", s) for s in CHECKLIST + COMMON + REVIEWER_ONLY + PLACEHOLDERS]
    return cases


@pytest.mark.parametrize(("name", "sentence"), _all_sentences())
def test_template_carries_sentence(name: str, sentence: str) -> None:
    assert sentence in _read(BRIEFS / name), f"{name} lacks: {sentence}"


def test_reviewer_worktree_rule_is_word_for_word_in_the_agent_definition() -> None:
    definition = _read(AGENTS / "reviewer.md")
    for sentence in REVIEWER_ONLY:
        assert sentence in definition, sentence


def test_builder_merge_order_is_word_for_word_in_the_agent_definition() -> None:
    definition = _read(AGENTS / "builder.md")
    assert BUILDER_ONLY[0].split(", (3)")[0] in definition
    assert BUILDER_ONLY[2] in definition


def test_templates_do_not_override_the_agent_definitions() -> None:
    for name in ["builder.md", "reviewer.md"]:
        text = _read(BRIEFS / name)
        assert "win over this page" not in text
        assert "overrides the export rule" not in text
        assert "do not merge, do not push" not in text.lower()


def test_playbook_fork_section_has_all_items() -> None:
    text = FORK.read_text(encoding="utf-8")
    assert HEADING in text
    section = _flat(text.split(HEADING, 1)[1].split("\n## ", 1)[0])
    for marker in [
        "**Multi-unit part.**",
        "**Same reference on another sheet, and a sheet in a subfolder.**",
        "**Hidden pins and power symbols.**",
        "**`lib_name` symbols.**",
        "**Every self-check key and guard has a red test.**",
        "**No-op and refusal write nothing.**",
        "**Do not say ok when KiCad did not follow.**",
        "**Net members.**",
        "**Label scope.**",
        "**Text input classes.**",
        "**Reason and tool text are true.**",
        "**Spec guard versus base.**",
        "**Unreadable tree.**",
        "**Delete has a self-check.**",
        "**Second fail-open of the same family:**",
        "refusing everything not proven",
        "docs/playbook/briefs/builder.md",
        "docs/playbook/briefs/reviewer.md",
        "a call never waits for a file above its own parent",
        'must not pin "lowest" across sheets',
    ]:
        assert marker in section, marker
    assert "Five of the six" not in section
