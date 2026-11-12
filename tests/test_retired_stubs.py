"""The retired redirect stubs BACKLOG.md and RESEARCH.md are gone and nothing tracked cites them as files.

Old ticket ids (P1-xx to P5-xx, M-0x) are cited as "archived backlog" in code and docs; the maintainers'
archive resolves them. A reference to the removed files would be a dead pointer in a public copy.

Every tracked text file is scanned, whatever its extension (issue 9: a list of extensions missed `.sh` and
`.ps1`). Binary files (a NUL in the first 8 KiB) are skipped. A git checkout is needed, as in
``tests/test_docs_layout.py``.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from tests._hermetic_git import run_git

ROOT = Path(__file__).resolve().parents[1]
_STUB_NAME = re.compile(r"\b(BACKLOG|RESEARCH)\.md\b")
_THIS = "tests/test_retired_stubs.py"
_NUL = b"\x00"


def _tracked_files() -> list[str]:
    if shutil.which("git") is None:
        pytest.skip("git is not available")
    out = run_git("ls-files", "-z", cwd=ROOT).stdout.decode()
    return [p for p in out.split("\x00") if p and p != _THIS and not p.startswith(".scratch/")]


def _text_of(path: Path) -> str | None:
    """The text of a file, or None when it is binary or cannot be read."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if _NUL in data[:8192]:
        return None
    return data.decode("utf-8", errors="replace")


def stub_references(files: list[str], root: Path) -> list[str]:
    """``file:line`` of every line in the given files (any extension, none included) that names a stub."""
    offenders: list[str] = []
    for rel in files:
        text = _text_of(root / rel)
        if text is None:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            if _STUB_NAME.search(line):
                offenders.append(f"{rel}:{number}")
    return offenders


def test_stub_files_are_removed() -> None:
    assert not (ROOT / "BACKLOG.md").exists()
    assert not (ROOT / "RESEARCH.md").exists()


def test_no_tracked_file_names_a_stub() -> None:
    assert stub_references(_tracked_files(), ROOT) == []


@pytest.mark.parametrize("name", ["hook.sh", "run.ps1", "Makefile", "x.cfg", "noext", "tool.bat", "a.ini"])
def test_a_reference_in_any_kind_of_text_file_is_found(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("echo ok\nsee BACKLOG.md for more\n", encoding="utf-8")
    assert stub_references([name], tmp_path) == [f"{name}:2"]


def test_a_binary_file_is_skipped_and_a_near_miss_is_clean(tmp_path: Path) -> None:
    (tmp_path / "b.bin").write_bytes(b"\x00\x01BACKLOG.md")
    (tmp_path / "t.sh").write_text("echo RESEARCH.mdx is not a stub\n", encoding="utf-8")
    assert stub_references(["b.bin", "t.sh"], tmp_path) == []
