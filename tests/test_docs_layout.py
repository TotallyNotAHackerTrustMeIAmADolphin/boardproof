"""The four reference documents live in docs/, and no tracked file names their old top-level paths.

Moved in build ticket 118 (tidy pass): ARCHITECTURE.md, CONTEXT.md, ROADMAP.md and TESTING.md went from
the repository root into docs/. A bare mention (no ``docs/`` before it) is a stale reference. Exempt:
a markdown link target inside docs/ itself (``](TESTING.md)`` is relative to docs/), the committed
run record ``evals/ladder_results.json`` (verbatim recorded tool output), tracked fixtures, ``.scratch/``
(planning records).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
NAMES = ("ARCHITECTURE", "CONTEXT", "ROADMAP", "TESTING")
BARE = re.compile(r"(?<![\w/.\-])(?:%s)\.md" % "|".join(NAMES))
LINK_TARGET = re.compile(r"\]\((?:%s)\.md" % "|".join(NAMES))
EXEMPT_PREFIXES = (".scratch/", "tests/fixtures/")
EXEMPT_FILES = {"evals/ladder_results.json", "tests/test_docs_layout.py"}


def _tracked() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    out = subprocess.run(["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout
    return [f for f in out.decode("utf-8").split("\0") if f]


def stale_references(files: list[str], root: Path) -> list[str]:
    problems = []
    for rel in files:
        if rel.startswith(EXEMPT_PREFIXES) or rel in EXEMPT_FILES:
            continue
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        in_docs = rel.startswith("docs/") and rel.count("/") == 1
        for n, line in enumerate(text.splitlines(), 1):
            for m in BARE.finditer(line):
                if in_docs and LINK_TARGET.match(line, m.start() - 2):
                    continue
                problems.append(f"{rel}:{n}: {m.group(0)}")
    return problems


def test_the_four_documents_moved_into_docs():
    for name in NAMES:
        assert not (ROOT / f"{name}.md").exists(), f"{name}.md is back at the top level"
        assert (ROOT / "docs" / f"{name}.md").is_file()
    for name in ("README.md", "CLAUDE.md", "CONTRIBUTING.md", "THIRD_PARTY.md", "LICENSE"):
        assert (ROOT / name).is_file(), f"{name} must stay at the top level"


def test_no_tracked_file_names_an_old_top_level_path():
    assert stale_references(_tracked(), ROOT) == []


def test_the_scan_catches_a_stale_reference_and_a_bare_link_target_outside_docs(tmp_path):
    (tmp_path / "docs").mkdir()
    (tmp_path / "a.md").write_text("see TESTING.md and [x](ROADMAP.md) and docs/TESTING.md\n", encoding="utf-8")
    (tmp_path / "docs" / "b.md").write_text("[x](TESTING.md) and (ARCHITECTURE.md decision 1)\n", encoding="utf-8")
    got = stale_references(["a.md", "docs/b.md"], tmp_path)
    assert got == ["a.md:1: TESTING.md", "a.md:1: ROADMAP.md", "docs/b.md:1: ARCHITECTURE.md"]


CARRIED_OVER_IDS = ("P3-11", "P3-13", "P4-11", "P2-07", "P3-07 / P3-12", "P4-08", "P1-25", "P1-02 / P1-03")


def test_carried_over_defects_and_deferred_domains_stay_in_the_roadmap_until_issues_exist():
    text = (ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    for ident in CARRIED_OVER_IDS:
        assert ident in text, f"{ident} is not in docs/ROADMAP.md"
    assert "## Carried-over defects" in text and "## Deferred domains" in text
    assert "tracked as GitHub issues" not in text


def test_the_roadmap_has_no_dangling_ladder_reference():
    text = (ROOT / "docs" / "ROADMAP.md").read_text(encoding="utf-8")
    assert "batches above" not in text
