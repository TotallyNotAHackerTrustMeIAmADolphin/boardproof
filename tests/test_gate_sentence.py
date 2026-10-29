"""Decision ticket 8: the status sentence in CLAUDE.md after the necked passes, and the docs that repeat it."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OLD = "has not yet produced a board that passes the acceptance gate"
NEW = (
    "**Rung 1 has boards with `PASS (necked)` from the gate (official batches under protocol 6: Freerouting 5 of 5; "
    "a PASS with forced necks, counted and labelled, never a plain PASS). No plain PASS exists yet, and the rung-1 "
    "board changed afterwards (protocols 7 and 8) and has no official batch under the current protocol.**"
)


def _text(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")


def test_claude_md_has_the_new_sentence_and_not_the_old():
    text = _text("CLAUDE.md")
    assert NEW in text
    assert OLD not in text
    assert "One earlier run on openMPPT claimed \"routed\" and was wrong." in text
    assert "Autonomous tier** is earned one board class at a time" in text
    assert "The **Copilot tier** is the default" in text


def test_other_docs_do_not_contradict_it():
    for rel in ("README.md", "docs/OVERVIEW.md", "docs/ROADMAP.md", "docs/TESTING.md"):
        text = _text(rel)
        assert OLD not in text, rel
        assert "plain PASS" in text and "PASS (necked)" in text, rel
    for rel in ("README.md", "docs/OVERVIEW.md"):
        text = _text(rel)
        assert "protocols 7 and 8" in text and "no official batch" in text, rel
