"""The retired redirect stubs BACKLOG.md and RESEARCH.md are gone and nothing tracked cites them as files.

Old ticket ids (P1-xx to P5-xx, M-0x) are cited as "archived backlog" in code and docs; the maintainers'
archive resolves them. A reference to the removed files would be a dead pointer in a public copy.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_STUB_NAME = re.compile(r"\b(BACKLOG|RESEARCH)\.md\b")
_THIS = "tests/test_retired_stubs.py"


def _tracked_text_files() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True
    ).stdout.decode()
    return [
        p
        for p in out.split("\0")
        if p
        and p != _THIS
        and not p.startswith(".scratch/")
        and p.endswith((".py", ".md", ".toml", ".txt", ".gitignore", ".json", ".yml", ".yaml"))
    ]


def test_stub_files_are_removed() -> None:
    assert not (ROOT / "BACKLOG.md").exists()
    assert not (ROOT / "RESEARCH.md").exists()


def test_no_tracked_file_names_a_stub() -> None:
    offenders: list[str] = []
    for rel in _tracked_text_files():
        path = ROOT / rel
        if not path.is_file():
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
            if _STUB_NAME.search(line):
                offenders.append(f"{rel}:{number}")
    assert offenders == []
