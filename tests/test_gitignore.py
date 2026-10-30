"""The repository's ``.gitignore`` (publish-prep ticket 03a, item 4).

Three kinds of checks, all against throw-away git repositories or ``git ls-files`` (tracked files
only; nothing here scans the checkout's directory tree, so untracked files around it never matter):

- no tracked file is ignored (``git ls-files -ci --exclude-standard`` is empty), except the old
  ``.scratch/`` planning tree, which is still tracked until the migration ticket (03) leaves it
  out of the new history;
- every pattern class ignores a planted file of its kind, and the fixtures, the rung-1 project
  (its ``.kicad_prl`` is part of the pinned project hash), our own ``Rung1_USBC`` library, the
  tracked ``.claude`` parts and the future submodule path are NOT ignored;
- every pattern has a case, and removing the patterns of a case un-ignores its planted file (the
  mutant: a dropped pattern turns the case red).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GITIGNORE = ROOT / ".gitignore"

# Tracked today, ignored by design: leaves with the migration (publish-prep ticket 03). Shrink only.
# (spelled with a variable: tests/test_scratch_absent_skips.py searches for quoted scratch paths)
OLD_NOTES = "." + "scratch"
TRACKED_BUT_IGNORED_PREFIXES: tuple[str, ...] = ()  # public export: the old planning tree is not tracked

# (planted path, the pattern lines that cover it: all of them are removed in the mutant)
IGNORED_CASES: list[tuple[str, tuple[str, ...]]] = [
    ("src/__pycache__/a.txt", ("__pycache__/",)),
    ("src/a.pyc", ("*.pyc",)),
    ("src/a.pyo", ("*.pyo",)),
    (".pytest_cache/v/x", (".pytest_cache/",)),
    (".ruff_cache/x", (".ruff_cache/",)),
    (".mypy_cache/x", (".mypy_cache/",)),
    (".coverage", (".coverage",)),
    (".coverage.host.1", (".coverage.*",)),
    ("htmlcov/index.html", ("htmlcov/",)),
    ("src/x.egg-info/PKG-INFO", ("*.egg-info/",)),
    ("build/lib/x.py", ("build/",)),
    ("dist/x.whl", ("dist/",)),
    ("~board.kicad_pcb.lck", ("~*.lck",)),
    ("proj/.history/a/x", (".history/",)),
    ("proj/proj-backups/x.zip", ("*-backups/",)),
    ("proj/_autosave-board.kicad_pcb", ("_autosave-*",)),
    ("proj/fp-info-cache", ("fp-info-cache",)),
    ("proj/board.kicad_pcb.bak", ("*.bak",)),
    ("userproj/board.kicad_prl", ("*.kicad_prl",)),
    (".claude/settings.local.json", (".claude/settings.local.json", ".claude/*")),
    (".claude/worktrees/x/a", (".claude/worktrees/", ".claude/*")),
    (".claude/scheduled_tasks.lock", (".claude/scheduled_tasks.lock", ".claude/*.lock", ".claude/*")),
    (".claude/skills/some-global-skill/notes.txt", (".claude/skills/*",)),
    (".idea/x", (".idea/",)),
    (".vscode/x", (".vscode/",)),
    ("src/.a.swp", ("*.swp",)),
    ("src/.a.swo", ("*.swo",)),
    ("src/a.py~", ("*~",)),
    (".DS_Store", (".DS_Store",)),
    ("Thumbs.db", ("Thumbs.db",)),
    ("desktop.ini", ("desktop.ini",)),
    (".env", (".env*",)),
    (".env.local", (".env*",)),
    ("keys/a.pem", ("*.pem",)),
    ("keys/a.key", ("*.key",)),
    ("keys/a.p12", ("*.p12",)),
    ("keys/a.pfx", ("*.pfx",)),
    ("credentials.json", ("credentials*.json",)),
    ("credentials-prod.txt", ("credentials*.txt",)),
    ("credentials.yaml", ("credentials*.yaml",)),
    ("credentials.yml", ("credentials*.yml",)),
    ("home/id_rsa", ("id_rsa",)),
    ("home/id_ed25519", ("id_ed25519",)),
    (".venv-probe/x", (".venv*/",)),
    (".venv/x", (".venv/", ".venv*/")),
    ("runs/1/index.json", ("/runs/",)),
    ("a1b2c3d.pytest.out", ("*.pytest.out",)),
    ("batch-gate-1.log", ("batch-gate-*.log",)),
    (OLD_NOTES + "/publish-prep/map.md", ("/.scratch/",)),
    (".tools/KiCadRoutingTools-0.23.1/README.md", ("/.tools/",)),
    ("tools/JLCPCB-Kicad-Library/x/y.kicad_sym", ("JLCPCB-Kicad-Library/",)),
    ("com_github_x/readme.md", ("com_github_*/",)),
    ("fetched/C12345.3dshapes/x.step", ("C[0-9]*.3dshapes/",)),
    ("fetched/C12345.kicad_sym", ("**/C[0-9]*.kicad_sym",)),
    ("fetched/C12345.pretty/x.kicad_mod", ("**/C[0-9]*.pretty/",)),
    ("proj/lib/C12345/x.txt", ("**/lib/C[0-9]*/",)),
    ("publish-forbidden.txt", ("publish-forbidden*",)),
    ("publish-forbidden-private.txt", ("publish-forbidden*",)),
]

# Paths that must stay visible to git.
TRACKED_CASES = [
    "evals/rung1/project/rung1_usbc_attiny1616.kicad_prl",
    "evals/rung1/layout_run/03_placed/rung1_usbc_attiny1616.kicad_prl",
    "tests/fixtures/simple_led.kicad_prl",
    "tests/fixtures/golden_attiny1616_board/golden_attiny1616_board.kicad_prl",
    "evals/rung1/project/lib/Rung1_USBC/Rung1_USBC.kicad_sym",
    "evals/rung1/project/lib/Rung1_USBC/Rung1_USBC.pretty/USB_C_Receptacle_Shouhan_TYPE-C-6P.kicad_mod",
    "evals/rung1/assets/Rung1_USBC/Rung1_USBC.pretty/USB_C_Receptacle_Shouhan_TYPE-C-6P.kicad_mod",
    ".claude/agents/builder.md",
    ".claude/settings.json",
    ".claude/skills/kicad-pcb/SKILL.md",
    ".claude/skills/kicad-simulation/SKILL.md",
    "third_party/JLCPCB-Kicad-Library/JLCPCB/C1.kicad_sym",
    "third_party/JLCPCB-Kicad-Library/README.md",
    "third_party/JLCPCB-Kicad-Library/JLCPCB/C1.pretty/C1.kicad_mod",
    "third_party/JLCPCB-Kicad-Library/JLCPCB/C1.3dshapes/C1.step",
    "third_party/JLCPCB-Kicad-Library/lib/C1/C1.txt",
    ".gitmodules",
    "src/kicad_mcp/sourcing/jlcpcb_basic.py",
    "evals/ladder_results.json",
    "tests/fixtures/zone_obstacle/zone_obstacle.kicad_pro",
]

# Future legitimate files the broad old patterns would have swallowed: (path, old pattern, the
# lines that replaced it). Restoring the old pattern in a mutant must make the file ignored.
LEGIT_CASES = [
    ("src/kicad_mcp/runs/store.py", "runs/", ("/runs/",)),
    ("tests/runs/a.json", "runs/", ("/runs/",)),
    ("evals/rung1/project/lib/Rung1_USBC/Rung1_USBC.3dshapes/part.step", "*.3dshapes/", ("C[0-9]*.3dshapes/",)),
    ("src/kicad_mcp/credentials.py", "credentials*",
     ("credentials*.json", "credentials*.txt", "credentials*.yaml", "credentials*.yml")),
]


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    if shutil.which("git") is None:
        pytest.fail("git is not on PATH; the .gitignore cannot be tested")
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=60, check=check)


def _is_ignored(tmp_path: Path, lines: list[str], rel: str) -> bool:
    repo = tmp_path / "r"
    if not repo.exists():
        _git(tmp_path, "init", "-q", str(repo))
    (repo / ".gitignore").write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    target = repo / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("x\n", encoding="utf-8")
    r = _git(repo, "check-ignore", "-q", "--no-index", rel, check=False)
    assert r.returncode in (0, 1), r.stderr
    return r.returncode == 0


def _lines() -> list[str]:
    return GITIGNORE.read_text(encoding="utf-8").splitlines()


def _patterns() -> list[str]:
    return [ln for ln in _lines() if ln.strip() and not ln.startswith("#")]


def test_no_tracked_file_is_ignored():
    out = _git(ROOT, "ls-files", "-ci", "--exclude-standard").stdout.splitlines()
    offenders = [p for p in out if not p.startswith(TRACKED_BUT_IGNORED_PREFIXES)]
    assert offenders == [], f"tracked files that .gitignore ignores: {offenders[:10]}"


def test_tracked_but_ignored_allowance_is_still_needed_or_removed():
    # The allowance for the old planning tree must not outlive it: once nothing under it is
    # tracked, the prefix has to leave TRACKED_BUT_IGNORED_PREFIXES.
    for prefix in TRACKED_BUT_IGNORED_PREFIXES:
        assert _git(ROOT, "ls-files", "--", prefix).stdout.strip(), f"{prefix} is not tracked any more: drop the allowance"


@pytest.mark.parametrize("rel,covering", IGNORED_CASES, ids=[c[0] for c in IGNORED_CASES])
def test_pattern_class_ignores_its_planted_file(tmp_path, rel, covering):
    assert all(line in _patterns() for line in covering), f"{covering} not in .gitignore"
    assert _is_ignored(tmp_path, _lines(), rel)


@pytest.mark.parametrize("rel,covering", IGNORED_CASES, ids=[c[0] for c in IGNORED_CASES])
def test_mutant_dropping_the_patterns_unignores_the_planted_file(tmp_path, rel, covering):
    mutant = [ln for ln in _lines() if ln not in covering]
    assert len(mutant) < len(_lines())
    assert not _is_ignored(tmp_path, mutant, rel)


@pytest.mark.parametrize("rel", TRACKED_CASES)
def test_fixtures_ours_and_tracked_claude_parts_are_not_ignored(tmp_path, rel):
    assert not _is_ignored(tmp_path, _lines(), rel)


def test_kicad_prl_negation_is_what_keeps_the_fixtures(tmp_path):
    # Mutant: without the negations the rung-1 project's .kicad_prl would be ignored.
    mutant = [ln for ln in _lines() if not ln.startswith("!evals/") and not ln.startswith("!tests/fixtures/")]
    assert len(mutant) == len(_lines()) - 2
    assert _is_ignored(tmp_path, mutant, "evals/rung1/project/rung1_usbc_attiny1616.kicad_prl")
    assert _is_ignored(tmp_path, mutant, "tests/fixtures/simple_led.kicad_prl")


def test_submodule_negation_is_what_keeps_the_submodule_path(tmp_path):
    mutant = [ln for ln in _lines() if ln != "!/third_party/JLCPCB-Kicad-Library/"]
    assert len(mutant) == len(_lines()) - 1
    assert _is_ignored(tmp_path, mutant, "third_party/JLCPCB-Kicad-Library/README.md")


def test_every_ignore_pattern_has_a_planted_case():
    covered = {p for _, covering in IGNORED_CASES for p in covering}
    uncovered = [p for p in _patterns() if not p.startswith("!") and p not in covered]
    assert uncovered == [], f"patterns without a planted-file case in IGNORED_CASES: {uncovered}"


def test_submodule_contents_negation_is_what_keeps_the_librarys_own_parts(tmp_path):
    mutant = [ln for ln in _lines() if ln != "!/third_party/JLCPCB-Kicad-Library/**"]
    assert len(mutant) == len(_lines()) - 1
    assert _is_ignored(tmp_path, mutant, "third_party/JLCPCB-Kicad-Library/JLCPCB/C1.kicad_sym")


@pytest.mark.parametrize("rel,old,new", LEGIT_CASES, ids=[c[0] for c in LEGIT_CASES])
def test_legitimate_files_are_not_swallowed_by_a_broad_pattern(tmp_path, rel, old, new):
    lines = _lines()
    assert all(n in lines for n in new)
    assert not _is_ignored(tmp_path, lines, rel)
    # Mutant: the old broad pattern back in place of the narrow ones turns the case red.
    mutant = [ln for ln in lines if ln not in new] + [old]
    assert _is_ignored(tmp_path, mutant, rel)
