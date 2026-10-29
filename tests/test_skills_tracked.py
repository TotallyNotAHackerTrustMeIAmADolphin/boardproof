"""A skill the project authors must be tracked by git (build ticket 02 review: kicad-simulation
was gitignored by ``.claude/skills/*`` and missing from the commit)."""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _tracked() -> set[str]:
    out = subprocess.run(["git", "ls-files", ".claude/skills"], cwd=REPO, capture_output=True, text=True, check=True)
    return set(out.stdout.split())


def test_every_skill_on_disk_is_tracked():
    tracked = _tracked()
    missing = [
        d.name for d in sorted((REPO / ".claude" / "skills").iterdir())
        if d.is_dir() and f".claude/skills/{d.name}/SKILL.md" not in tracked
    ]
    assert not missing, f"untracked skills (git add -f, and un-ignore them in .gitignore): {missing}"


def test_every_skill_cited_by_source_or_tickets_is_tracked():
    tracked = _tracked()
    cited = set()
    for root in ("src", "tests", "docs/ARCHITECTURE.md", "docs/ROADMAP.md"):
        base = REPO / root
        files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file() and p.suffix in {".py", ".md", ".toml"}]
        for p in files:
            if p.name == Path(__file__).name:
                continue
            cited.update(re.findall(r"\.claude/skills/([\w-]+)/SKILL\.md", p.read_text(encoding="utf-8", errors="ignore")))
    missing = sorted(s for s in cited if f".claude/skills/{s}/SKILL.md" not in tracked)
    assert not missing, f"cited but untracked skills: {missing}"
