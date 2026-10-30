"""Local batch gate: the full suite plus lint, one GREEN/RED verdict (build ticket 45).

There is no hosted CI (owner, 2026-10-19); this script is the test gate (playbook A9, fork P6 and
P11). Run it from any directory; it always tests the checkout or worktree it lives in:

    python <worktree>/scripts/batch_gate.py

It runs ``<interpreter> -m pytest -m "not network" -rfEs -p no:cacheprovider`` from the worktree
root with ``PYTHONPATH=<worktree>/src``, ``KICAD_DEMOS_REQUIRED=1``, ``KICAD_ROUTING_TOOLS_REQUIRED=1`` (not ``NGSPICE_REQUIRED``: ngspice is optional, see below),
under a timeout (``SUITE_TIMEOUT_S``, about 3x the slowest measured suite ``SUITE_MEASURED_S``, with only other gates known to be running;
``--timeout`` overrides it; the timeout is measured on the wall clock), and then
``scripts/lint_gate.py``. The interpreter is ``--python``, else ``$KICAD_MCP_PYTHON``, else the
shared ``.venv`` of the main checkout (found through ``git rev-parse --git-common-dir``).

RED, never silence, when:
- the working tree is not clean;
- ``KICAD_ROUTING_TOOLS_PATH`` is unset or not a directory (the KRT tests would skip);
- ``kicad_mcp.__file__`` does not lie inside this worktree's ``src`` (the suite is then not run);
- pytest's ``rootdir:`` line is missing or names another directory, or its ``configfile:`` is not
  ``pyproject.toml``, or ``[tool.pytest.ini_options]`` differs from ``EXPECTED_PYTEST_CONFIG``
  (no ``addopts``, ``python_files`` or ``norecursedirs`` without a reviewed change here);
- a tracked ``tests/test_*.py`` file (``git ls-files``) is missing from a full
  ``--collect-only`` run (``collect_ignore`` in a ``conftest.py``, ``--ignore``);
- there is no ``collected`` line, or the deselected count differs from the number of tests marked
  ``network`` (counted by a separate ``--collect-only -m network`` run), so nothing else can be
  deselected;
- nothing passed, or the tests that ran (passed, failed, skipped, xfailed, xpassed) do not account
  for every selected test, which also makes a module-level skip
  (``pytest.skip(allow_module_level=True)``) RED: its file's tests are never counted;
- a skip that ``scripts/known-skips.toml`` does not allow (by test file and reason prefix, at most
  ``max`` times), or skip lines that do not add up to the summary's count;
- a timeout; a pytest exit code other than 0 and 1; exit 1 without failure ids or exit 0 with them;
  failure lines that do not add up to the summary's counts;
- any failure not in ``scripts/known-red.txt``, or a known-red entry without an owning ticket;
- lint (``scripts/lint_gate.py``);
- the standing publishability check (publish-prep ticket 03a): ``scripts/check_publishable.py`` over
  the TRACKED files (``git checkout-index``, never the working tree; ``.scratch/`` is not part of the
  published tree) with ``--forbidden-file`` from ``$BOARDPROOF_FORBIDDEN_FILE`` and
  ``--strict-archives``. RED when the variable is unset, empty or names no file, when the checker
  cannot run (an empty or invalid pattern file, a timeout, an export failure), on any hit beyond the
  reviewed ``PUBLISH_ALLOWED_HITS`` (the three KiCad-written fixtures, pinned by hit count), and on
  any unopened archive beyond ``PUBLISH_ALLOWED_ARCHIVES`` (the 8 datasheet PDFs and the gerber
  zip). The log names ``file:line: [pattern]`` only, never the matched text. ``--publishable-only``
  runs just this check (for a push) and prints ``PUBLISHABLE: OK`` or ``PUBLISHABLE: RED (...)``;
- HEAD or ``git status --porcelain --untracked-files=all`` at the end differ from the start (an
  edit, commit or checkout during the run), or that recheck cannot run (build ticket 70). Not seen
  by it: an edit undone before the end, and anything ``git status`` does not show (ignored paths,
  assume-unchanged or skip-worktree entries). Never edit the tree while the gate runs;
- ``--log-dir`` lies inside the tree (its files would change the status); nothing is run.
Each new failure is printed by id and re-run once alone; a pass there is reported as a flaky
candidate and is still RED. A known-red entry matches by test id only: a known-red test that fails
for a new reason still counts as known.

The verdict is the last line: ``BATCH GATE <sha7>: GREEN`` or ``BATCH GATE <sha7>: RED (...)``.
Exit code 0 for GREEN, 1 for RED (2 for a bad argument). The first lines give the expected duration
and the worst case (``worst_case_s``; builder.md's outer ``timeout`` lies above it). Full output:
the log printed at the start; each run's files are named ``<sha7>[-dirty]-<date>-<time>-<pid>``,
with ``-<n>`` added when that name is taken, so that gates of several worktrees can share the log
directory. No free name is RED. Each run starts by removing whole old runs from the log directory
(build ticket 79): a run older than 14 days, or beyond the newest 100 runs and older than a day
(``LOG_MAX_AGE_S``, ``LOG_KEEP_RUNS``, ``LOG_MIN_AGE_S``; age is the newest file's mtime). Only
file names the gate writes are touched, the current run's never, and a run whose ``.log`` cannot
be removed (on Windows: a gate still holds it open) keeps all its files. The cleanup never changes
the verdict: a file that cannot be removed is skipped and listed, and an error is reported.
Every start failure prints exactly one verdict line, ``BATCH GATE <unknown>: RED (...)`` (the sha7
instead of ``<unknown>`` once HEAD is known), and exits 1: a git failure (``could not read the tree
at the start: ...``, build ticket 79), a script that is not at the top of its git tree, no shared
interpreter, a log directory or log file that cannot be created, and ``pyproject.toml`` or the
known-red or known-skips file existing but unreadable (build ticket 88).
"""

from __future__ import annotations

import argparse
import math
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
KNOWN_RED = Path("scripts/known-red.txt")  # relative to the tree's root
KNOWN_SKIPS = Path("scripts/known-skips.toml")
PRE_PUSH = Path("scripts/git-hooks/pre-push")
COMMIT_MSG = Path("scripts/git-hooks/commit-msg")  # the identity guard (publish-prep ticket 03a)
# pytest's duration: 460-870 s on 2026-10-20 with one gate at a time; 1173-1358 s that day with the
# gates of parallel builders at once (dead735, ticket 70 review round 1). On 2026-10-21, with up to
# six gates at once and about 2500-2720 tests, the shared log directory holds exit-0 runs of
# 1506-1801 s; later that day, with more gates at once and about 2850 tests, 2240-2545 s; the
# slowest is 374c526, 2545 s (build ticket 88, review round 1). That is the basis: the slowest run
# with only other gates known to be running. The 5331 s run (c1c8f1b, 2026-10-21, ticket 36's
# gate) is excluded from the timeout's basis: a reviewer's targeted tests ran at the same time
# and the machine was in standby for 3770 s of it. It exited 0 and ended GREEN under the 4200 s timeout then in force,
# because Popen.wait(timeout) does not count standby time on Windows; default_runner now
# enforces the timeout on the wall clock. Update the basis from the slowest logged run when the
# suite grows; the timeout follows at about 3x (fork P10).
SUITE_MEASURED_S = 2545
SUITE_MEASURED_WHEN = "374c526 on 2026-10-21, several gates in parallel, only other gates known to be running"
SUITE_TIMEOUT_S = 7650
WAIT_SLICE_S = 5.0  # default_runner checks the wall-clock deadline this often
IMPORT_TIMEOUT_S = 120
COLLECT_TIMEOUT_S = 300
RERUN_TIMEOUT_S = 300  # one rerun of one new failure
RERUN_BUDGET_S = 600  # all reruns together; past it, the rest are not rerun (still RED)
MAX_RERUNS = 10
LINT_TIMEOUT_S = 210  # ruff takes seconds; the publishability budget below came out of the former 300 s
PUBLISH_BUDGET_S = 90  # the whole publishability step, export and checker; the worst case stays as it was
KILL_WAIT_S = 60  # killing a timed-out step: taskkill, then the wait for the process, half each
GIT_TIMEOUT_S = 60  # each git call
GIT_CALLS = 8  # in main(): toplevel, HEAD and status twice, default_python, pre-push, ls-files
OVERHEAD_S = 30  # import probe, two collections, lint and git, on a normal run

# The standing publishability check (publish-prep ticket 03a). The owner's private pattern file is
# never in the repository; its location comes from this environment variable.
FORBIDDEN_ENV = "BOARDPROOF_FORBIDDEN_FILE"
PUBLISH_CHECK = Path("scripts/check_publishable.py")  # relative to the tree's root
PUBLISH_EXCLUDE = (".scratch",)  # the old planning tree: git-ignored, left out of the new history
# Reviewed hits: the three KiCad-written fixtures record the machine that wrote them. Each entry is
# (number of hits, git blob id of the reviewed file). A different count, a file whose content
# changed since its review (so a new leak cannot replace a reviewed one unseen; the blob id reveals
# nothing) and any other file are RED. Publish-prep ticket 03 regenerates or drops the fixtures and
# shrinks this list.
PUBLISH_ALLOWED_HITS: dict[str, tuple[int, str]] = {}  # public export: the three KiCad-written fixtures were regenerated or dropped
# Reviewed archives the checker cannot open (compressed streams): five datasheets fetched for the
# rung-1 board, three test datasheets, and the golden board's gerber zip. A new one is RED.
PUBLISH_ALLOWED_ARCHIVES = (
    "tests/fixtures/golden_attiny1616_board/manufacturing/gerbers/gerbers.zip",
)  # public export: the datasheet PDFs are not in the tree; the gerber zip is scanned by the export


def worst_case_s(suite_timeout: float) -> float:
    """Longest a gate run can take: every step that can time out runs to its timeout and is then
    killed (import, two collections, suite, lint, the publishability step and the last rerun; the rerun budget covers the
    earlier reruns' kills), and every git call runs to its timeout. ``.claude/agents/builder.md``
    wraps the gate in a ``timeout`` above this figure, so the gate's own RED fires first."""
    steps = (IMPORT_TIMEOUT_S + 2 * COLLECT_TIMEOUT_S + suite_timeout + RERUN_BUDGET_S + LINT_TIMEOUT_S
             + PUBLISH_BUDGET_S)
    return steps + 6 * KILL_WAIT_S + GIT_CALLS * GIT_TIMEOUT_S

# The reviewed [tool.pytest.ini_options] of pyproject.toml. Options such as addopts (--ignore,
# -k, -x), python_files or norecursedirs change what the gate tests, so any change to this table
# needs a reviewed change here (delta review 1, finding 4).
EXPECTED_PYTEST_CONFIG = {
    "testpaths": ["tests"],
    "pythonpath": ["."],
    "markers": [
        "network: hits a real external network endpoint -- excluded from the default run"
        " (`pytest -m 'not network'`), run deliberately via `pytest -m network`",
    ],
    # an unknown marker (a typo of needs_krt) is an error, not a silent no-op (build ticket 53)
    "filterwarnings": ["error::pytest.PytestUnknownMarkWarning"],
}

# runner(cmd, env, cwd, timeout_s, out_path) -> exit code, or None on timeout. Output goes to out_path.
Runner = Callable[[list[str], dict, Path, float, Path], "int | None"]

_ID_LINE = re.compile(r"^(?:FAILED|ERROR) (\S+?\.py(?:::[^\s\[]+)*(?:\[.*?\])?)(?: - .*)?\s*$")
_SKIP_LINE = re.compile(r"^SKIPPED \[(\d+)\] (\S+?\.py)(?::\d+)?: (.*?)\s*$")
_SUMMARY_HEADER = re.compile(r"^=+ short test summary info =+\s*$")
_ROOTDIR = re.compile(r"^rootdir: (.+?)(?:, (?:configfile|testpaths|inifile): .*)?\s*$")
_CONFIGFILE = re.compile(r"(?:^|, )configfile: (\S+?)(?:,|\s*$)")
_COLLECTED = re.compile(r"^collected (\d+) items?\b.*$")
_COLLECT_ONLY = re.compile(r"^(\d+)(?:/(\d+))? tests? collected\b")
_FINAL = re.compile(r"^=+ (.*\bin [\d.]+s\b.*?) =+\s*$")
_TICKET = re.compile(r"#.*\bticket\s+\d+", re.I)


# --------------------------------------------------------------------------- parsing


def _summary_lines(output: str) -> list[str]:
    lines: list[str] = []
    in_summary = False
    for line in output.splitlines():
        if _SUMMARY_HEADER.match(line):
            in_summary = True
            continue
        if in_summary:
            lines.append(line)
    return lines


def failure_ids(output: str) -> list[str]:
    """One id per FAILED/ERROR line of the short test summary section, in order (not deduplicated)."""
    return [m.group(1) for m in map(_ID_LINE.match, _summary_lines(output)) if m]


def skip_counts(output: str) -> Counter:
    """Skips per (posix test file, reason) from the summary's ``SKIPPED [n] file:line: reason`` lines."""
    counts: Counter = Counter()
    for m in map(_SKIP_LINE.match, _summary_lines(output)):
        if m:
            counts[(m.group(2).replace("\\", "/"), m.group(3))] += int(m.group(1))
    return counts


def rootdir(output: str) -> str | None:
    for line in output.splitlines():
        m = _ROOTDIR.match(line)
        if m:
            return m.group(1).strip()
    return None


def configfile(output: str) -> str | None:
    for line in output.splitlines():
        if line.startswith(("rootdir: ", "configfile: ")):
            m = _CONFIGFILE.search(line)
            if m:
                return m.group(1)
    return None


def collected_line(output: str) -> str | None:
    for line in output.splitlines():
        if _COLLECTED.match(line):
            return line.strip()
    return None


def collected_counts(line: str) -> tuple[int, int, int]:
    """(collected, deselected, selected) from pytest's ``collected`` line."""
    total = int(re.match(r"collected (\d+)", line).group(1))
    m = re.search(r"(\d+) deselected", line)
    deselected = int(m.group(1)) if m else 0
    m = re.search(r"(\d+) selected", line)
    selected = int(m.group(1)) if m else total - deselected
    return total, deselected, selected


def final_summary(output: str) -> str | None:
    found = None
    for line in output.splitlines():
        m = _FINAL.match(line)
        if m:
            found = m.group(1)
    return found


def summary_count(summary: str, word: str) -> int:
    m = re.search(rf"(\d+) {word}\b", summary)
    return int(m.group(1)) if m else 0


@dataclass
class KnownRed:
    ids: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def read_known_red(text: str) -> KnownRed:
    """Ids, one per line, each followed by ``# ... ticket <n>: reason``. Blank lines and full-line
    comments are ignored. An entry without an owning ticket is a problem (it turns the gate RED)."""
    kr = KnownRed()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        test_id = line.split("#", 1)[0].strip()
        if not _TICKET.search(line):
            kr.problems.append(f"known-red line {n} names no owning ticket: {line}")
        if not re.fullmatch(r"\S+\.py(::\S+)*(\[.*\])?", test_id):
            kr.problems.append(f"known-red line {n} is not a test id: {test_id}")
            continue
        kr.ids.append(test_id)
    return kr


@dataclass
class KnownSkip:
    file: str
    reason: str
    max: int
    why: str


@dataclass
class KnownSkips:
    entries: list[KnownSkip] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def read_known_skips(text: str) -> KnownSkips:
    """``[[skip]]`` tables with ``file`` (posix test path), ``reason`` (a prefix of pytest's skip
    reason), ``max`` (at least 1) and ``why`` (non-empty)."""
    ks = KnownSkips()
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        ks.problems.append(f"known-skips file is not TOML: {exc}")
        return ks
    for n, entry in enumerate(data.get("skip", []), 1):
        try:
            skip = KnownSkip(str(entry["file"]), str(entry["reason"]), int(entry["max"]), str(entry["why"]))
        except (KeyError, TypeError, ValueError) as exc:
            ks.problems.append(f"known-skips entry {n} lacks file, reason, max or why: {exc}")
            continue
        if not skip.file.endswith(".py") or "\\" in skip.file:
            ks.problems.append(f"known-skips entry {n}: file must be a posix test path: {skip.file}")
        elif not skip.reason.strip() or skip.max < 1 or not skip.why.strip():
            ks.problems.append(f"known-skips entry {n}: empty reason or why, or max below 1")
        else:
            ks.entries.append(skip)
    return ks


def check_skips(counts: Counter, known: KnownSkips) -> tuple[list[str], list[str]]:
    """(unknown skip lines, skips over their maximum)."""
    used: Counter = Counter()
    unknown: list[str] = []
    for (file, reason), n in sorted(counts.items()):
        match = next((k for k in known.entries if k.file == file and reason.startswith(k.reason)), None)
        if match is None:
            unknown.append(f"{file}: {reason} ({n}x)")
        else:
            used[(match.file, match.reason)] += n
    over = [f"{k.file}: {k.reason} ({used[(k.file, k.reason)]}x, at most {k.max})"
            for k in known.entries if used[(k.file, k.reason)] > k.max]
    return unknown, over


def same_path(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


def inside(child: str | Path, parent: str | Path) -> bool:
    c = os.path.normcase(os.path.realpath(str(child)))
    p = os.path.normcase(os.path.realpath(str(parent)))
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


# --------------------------------------------------------------------------- gate


@dataclass
class Result:
    reasons: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    flaky: list[str] = field(default_factory=list)

    @property
    def green(self) -> bool:
        return not self.reasons


def run_gate(
    *,
    root: Path,
    sha: str,
    dirty: list[str],
    python: str,
    known_red_text: str | None,
    known_skips_text: str | None,
    tracked_tests: list[str] | None,
    pyproject_text: str | None,
    runner: Runner,
    log_dir: Path,
    say: Callable[[str], None],
    suite_timeout: float = SUITE_TIMEOUT_S,
    run_id: str | None = None,
    clock: Callable[[], float] = time.monotonic,
    publish_check: Callable[..., list[str]] | None = None,
) -> Result:
    """``run_id`` prefixes every output file in ``log_dir`` (default: the sha7)."""
    res = Result()
    s7 = run_id or sha[:7]
    env = dict(os.environ)
    if env.pop("PYTEST_ADDOPTS", None) is not None:
        say("PYTEST_ADDOPTS is set; ignored for this run")
    env["PYTHONPATH"] = str(root / "src")
    env["KICAD_DEMOS_REQUIRED"] = "1"
    env["KICAD_ROUTING_TOOLS_REQUIRED"] = "1"  # an absent KRT fails its tests, never skips (build ticket 53)
    say(f"env: KICAD_DEMOS_REQUIRED=1 KICAD_ROUTING_TOOLS_REQUIRED=1 PYTHONPATH={env['PYTHONPATH']}")
    krt = env.get("KICAD_ROUTING_TOOLS_PATH")
    say(f"env: KICAD_ROUTING_TOOLS_PATH={krt if krt else '(unset)'}")
    if not krt:
        res.reasons.append("KICAD_ROUTING_TOOLS_PATH unset (the KRT tests would skip)")
    elif not Path(krt).is_dir():
        res.reasons.append(f"KICAD_ROUTING_TOOLS_PATH is not a directory: {krt}")
    # ngspice is optional while the simulation map is parked (owner decision of 2026-10-30, issue 40):
    # absent, the live ngspice tests skip, and scripts/known-skips.toml allows exactly those skips,
    # counted and printed. Any other skip is still RED. NGSPICE_REQUIRED is no longer set here.
    ngspice = env.get("NGSPICE_PATH")
    if ngspice and Path(ngspice).is_file():
        say(f"env: NGSPICE_PATH={ngspice} (the live ngspice tests run)")
    else:
        say(f"env: NGSPICE_PATH={ngspice if ngspice else '(unset)'}"
            " (ngspice is optional, issue 40: the live ngspice tests skip, as the known skips count)")

    # The standing publishability check, on the tracked files (publish-prep ticket 03a).
    res.reasons += (publish_check or check_publishable_tracked)(root, python, say)

    if dirty:
        res.reasons.append(f"working tree not clean ({len(dirty)} paths)")
        say(f"working tree not clean, the verdict cannot be GREEN ({len(dirty)} paths):")
        for p in dirty:
            say(f"  {p}")

    # Known-red list and known skips.
    if known_red_text is None:
        res.reasons.append("known-red file missing")
        say(f"known-red file missing: {KNOWN_RED}")
        known = KnownRed()
    else:
        known = read_known_red(known_red_text)
        for p in known.problems:
            say(p)
        if known.problems:
            res.reasons.append("known-red file invalid")
    if known_skips_text is None:
        res.reasons.append("known-skips file missing")
        say(f"known-skips file missing: {KNOWN_SKIPS}")
        skips = KnownSkips()
    else:
        skips = read_known_skips(known_skips_text)
        for p in skips.problems:
            say(p)
        if skips.problems:
            res.reasons.append("known-skips file invalid")

    # The pytest configuration that decides what is collected must be the reviewed one.
    try:
        actual = tomllib.loads(pyproject_text or "").get("tool", {}).get("pytest", {}).get("ini_options")
    except tomllib.TOMLDecodeError as exc:
        actual = f"(unreadable: {exc})"
    if actual != EXPECTED_PYTEST_CONFIG:
        res.reasons.append("[tool.pytest.ini_options] differs from the reviewed EXPECTED_PYTEST_CONFIG")
        say(f"[tool.pytest.ini_options] is {actual!r}; the reviewed table is in scripts/batch_gate.py"
            " (EXPECTED_PYTEST_CONFIG): a change goes through a ticket and a review")

    # Which kicad_mcp does this interpreter import?
    probe_out = log_dir / f"{s7}.import.out"
    rc = runner([python, "-c", "import kicad_mcp; print(kicad_mcp.__file__)"], env, root, IMPORT_TIMEOUT_S,
                probe_out)
    probe = _read(probe_out).strip().splitlines()
    imported = probe[-1].strip() if probe else ""
    say(f"kicad_mcp.__file__: {imported or '(nothing printed)'}")
    if rc != 0 or not imported or not inside(imported, root / "src"):
        res.reasons.append("kicad_mcp is not imported from this worktree's src")
        say(f"RED: kicad_mcp must come from {root / 'src'} (rc {rc}); the suite was not run")
        return res

    # How many tests carry the network marker? Exactly those may be deselected.
    net_out = log_dir / f"{s7}.network.out"
    rc = runner([python, "-m", "pytest", "--collect-only", "-q", "-m", "network", "-p", "no:cacheprovider"],
                env, root, COLLECT_TIMEOUT_S, net_out)
    network = None
    for line in _read(net_out).splitlines():
        m = _COLLECT_ONLY.match(line.strip())
        if m:
            network = int(m.group(1))
    if rc not in (0, 5) or network is None:  # 5: no test carries the marker
        if rc == 5:
            network = 0
        else:
            res.reasons.append(f"network-marked tests could not be counted (rc {rc})")
    say(f"network-marked tests: {network if network is not None else '(unknown)'}")

    # Every tracked test file must be collected (collect_ignore in a conftest.py, --ignore and the
    # like would otherwise drop whole files without a trace). A file skipped at module level counts
    # as collected here; its skip is judged with the others below.
    all_out = log_dir / f"{s7}.collect.out"
    rc = runner([python, "-m", "pytest", "--collect-only", "-q", "-rs", "-p", "no:cacheprovider"],
                env, root, COLLECT_TIMEOUT_S, all_out)
    collected_text = _read(all_out)
    collected_files = {line.split("::", 1)[0].replace("\\", "/") for line in collected_text.splitlines()
                       if "::" in line and not line.startswith(("SKIPPED", "FAILED", "ERROR"))}
    collected_files |= {f for f, _ in skip_counts(collected_text)}
    if rc != 0 or not any(_COLLECT_ONLY.match(line.strip()) for line in collected_text.splitlines()):
        res.reasons.append(f"the full collection could not be read (rc {rc})")
    elif tracked_tests is None:
        res.reasons.append("tracked test files unknown (git ls-files failed)")
    else:
        missing = sorted(set(tracked_tests) - collected_files)
        say(f"tracked test files: {len(tracked_tests)}, collected files: {len(collected_files)}")
        if missing:
            res.reasons.append(f"{len(missing)} tracked test file(s) not collected")
            say("tracked test files that pytest did not collect:")
            for f in missing:
                say(f"  {f}")

    # The suite.
    cmd = [python, "-m", "pytest", "-m", "not network", "-rfEs", "-p", "no:cacheprovider"]
    out_path = log_dir / f"{s7}.pytest.out"
    say(f"pytest: {' '.join(cmd)} (timeout {int(suite_timeout)} s), output {out_path}")
    t0 = time.monotonic()
    rc = runner(cmd, env, root, suite_timeout, out_path)
    say(f"pytest finished in {time.monotonic() - t0:.0f} s with exit code {'TIMEOUT' if rc is None else rc}")
    out = _read(out_path)

    rd = rootdir(out)
    if rd is None:
        res.reasons.append("no rootdir line")
        say("RED: pytest printed no rootdir line")
    elif not same_path(rd, root):
        res.reasons.append("rootdir is not this worktree")
        say(f"RED: rootdir {rd} is not {root}")
    else:
        say(f"rootdir: {rd} (this worktree)")
    cf = configfile(out)
    say(f"configfile: {cf or '(none)'}")
    if cf != "pyproject.toml":
        res.reasons.append(f"configfile is {cf or 'missing'}, not pyproject.toml")
    coll = collected_line(out)
    say(f"collected: {coll or '(no collected line)'}")
    summary = final_summary(out)
    say(f"summary: {summary or '(no final summary line)'}")
    if coll is None:
        res.reasons.append("no collected line")
    else:
        total, deselected, selected = collected_counts(coll)
        if network is not None and deselected != network:
            res.reasons.append(f"{deselected} deselected, but {network} tests are marked network")
        if summary is not None and rc in (0, 1):
            ran = sum(summary_count(summary, w) for w in ("passed", "failed", "skipped", "xfailed", "xpassed"))
            errors = summary_count(summary, "errors?")
            if ran > selected or ran + errors < selected:
                res.reasons.append(f"{selected} tests selected, but {ran} ran ({errors} errors)")
    if summary is not None and summary_count(summary, "passed") == 0:
        res.reasons.append("nothing passed")

    # Skips.
    skipped = skip_counts(out)
    if summary is not None and sum(skipped.values()) != summary_count(summary, "skipped"):
        res.reasons.append(f"{sum(skipped.values())} skips in skip lines, summary says {summary_count(summary, 'skipped')}")
    unknown, over = check_skips(skipped, skips)
    say(f"skips ({sum(skipped.values())}):")
    for (file, reason), n in sorted(skipped.items()):
        say(f"  {n}x {file}: {reason}")
    if unknown:
        res.reasons.append(f"{len(unknown)} unknown skip reason(s)")
        say("unknown skips (not allowed by scripts/known-skips.toml):")
        for u in unknown:
            say(f"  {u}")
    if over:
        res.reasons.append(f"{len(over)} known skip(s) with more skips than allowed")
        for o in over:
            say(f"  over its maximum: {o}")

    ids = failure_ids(out)
    if rc is None:
        res.reasons.append("suite timed out")
    elif rc not in (0, 1):
        res.reasons.append(f"pytest exit code {rc}")
    elif rc == 1 and not ids:
        res.reasons.append("pytest exit code 1 without failure ids")
    elif rc == 0 and ids:
        res.reasons.append("pytest exit code 0 with failure ids")
    if rc in (0, 1):
        if summary is None:
            res.reasons.append("no final summary line")
        else:
            expected = summary_count(summary, "failed") + summary_count(summary, "errors?")
            if expected != len(ids):
                res.reasons.append(f"{len(ids)} failure lines, summary says {expected}")
                say(f"RED: {len(ids)} FAILED/ERROR lines parsed, but the summary counts {expected}")

    failing = sorted(set(ids))
    known_set = set(known.ids)
    res.new = [i for i in failing if i not in known_set]
    say(f"known-red ({len(known.ids)} entries; matched by id only):")
    for kid in known.ids:
        state = "failing (known)" if kid in failing else "did not fail this run (passed, skipped or gone)"
        say(f"  {kid}: {state}")
    say(f"new failures ({len(res.new)}):")
    for i in res.new:
        say(f"  {i}")
    if res.new:
        res.reasons.append(f"{len(res.new)} new failure(s)")
        if len(res.new) <= MAX_RERUNS:
            started = clock()
            for k, i in enumerate(res.new):
                left = RERUN_BUDGET_S - (clock() - started)
                if left < 1:
                    say(f"rerun budget of {RERUN_BUDGET_S} s used up: {len(res.new) - k} failure(s) not rerun")
                    break
                rr = runner([python, "-m", "pytest", "-p", "no:cacheprovider", i], env, root,
                            min(RERUN_TIMEOUT_S, left), log_dir / f"{s7}.rerun-{k}.out")
                if rr == 0:
                    res.flaky.append(i)
                    say(f"flaky candidate (passed alone on rerun, still RED): {i}")
        else:
            say(f"more than {MAX_RERUNS} new failures: no reruns")

    # Lint, on the same tree.
    lint_out = log_dir / f"{s7}.lint.out"
    rc = runner([python, str(root / "scripts" / "lint_gate.py")], env, root, LINT_TIMEOUT_S, lint_out)
    for line in _read(lint_out).splitlines():
        say(line)
    if rc != 0:
        res.reasons.append("lint")
    return res


_HIT_LINE = re.compile(r"^(?P<file>.*):(?P<line>\d+): \[(?P<name>[^\]]+)\] ")


def check_publishable_tracked(
    root: Path,
    python: str,
    say: Callable[[str], None],
    *,
    checker: Path | None = None,
    allowed_hits: dict[str, tuple[int, str]] | None = None,
    allowed_archives: tuple[str, ...] | None = None,
    budget: float = PUBLISH_BUDGET_S,
) -> list[str]:
    """Reasons for RED from ``scripts/check_publishable.py`` over the tracked files (empty: pass).

    The tracked files are exported from the index into a temporary directory, so untracked and
    ignored files around the checkout never matter. The matched text of a hit is never printed or
    logged (it is the owner's private pattern); only ``file:line: [pattern]``. The whole step,
    export and checker, stays within ``budget`` seconds."""
    hits_ok = PUBLISH_ALLOWED_HITS if allowed_hits is None else allowed_hits
    archives_ok = PUBLISH_ALLOWED_ARCHIVES if allowed_archives is None else allowed_archives
    checker = checker or root / PUBLISH_CHECK
    deadline = time.monotonic() + budget
    forbidden = os.environ.get(FORBIDDEN_ENV, "")
    if not forbidden.strip():
        say(f"publishability check: {FORBIDDEN_ENV} is unset or empty")
        return [f"{FORBIDDEN_ENV} unset (the publishability check cannot run)"]
    if not Path(forbidden).is_file():
        say(f"publishability check: {FORBIDDEN_ENV} does not name a file")
        return [f"{FORBIDDEN_ENV} is not a file (the publishability check cannot run)"]
    if not checker.is_file():
        say(f"publishability check: {checker} is missing")
        return ["the publishability checker script is missing"]

    def left() -> float:
        return max(1.0, deadline - time.monotonic())

    with tempfile.TemporaryDirectory(prefix="bp-publish-") as tmp:
        export = Path(tmp) / "tree"
        export.mkdir()
        try:
            subprocess.run(["git", "-C", str(root), "checkout-index", "-a", f"--prefix={export.as_posix()}/"],
                           capture_output=True, text=True, check=True, timeout=left())
            exported = sum(1 for p in export.rglob("*") if p.is_file())
            staged = subprocess.run(["git", "-C", str(root), "ls-files", "-s", "-z"], capture_output=True,
                                    text=True, encoding="utf-8", errors="replace", check=True, timeout=left()).stdout
            blobs = {}
            for entry in staged.split("\0"):
                meta, _, name = entry.partition("\t")
                if name:
                    blobs[name] = meta.split()[1]
        except (subprocess.SubprocessError, OSError) as exc:
            say(f"publishability check: the tracked files could not be exported: {type(exc).__name__}")
            return ["the tracked files could not be exported for the publishability check"]
        cmd = [python, str(checker), str(export), "--forbidden-file", forbidden, "--strict-archives"]
        cmd += [x for e in PUBLISH_EXCLUDE for x in ("--exclude", e)]
        cmd += [x for a in archives_ok for x in ("--allow-archive", a)]
        try:
            done = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                                  timeout=left())
        except subprocess.TimeoutExpired:
            say(f"publishability check: the checker timed out (budget {int(budget)} s)")
            return ["the publishability checker timed out"]
        except OSError as exc:
            say(f"publishability check: the checker could not start: {type(exc).__name__}")
            return ["the publishability checker could not run"]

    if done.returncode not in (0, 1, 3):
        say(f"publishability check: the checker could not run (exit {done.returncode})")
        return [f"the publishability checker could not run (exit {done.returncode})"]
    found: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        m = _HIT_LINE.match(line)
        if m:
            found.setdefault(m["file"].replace("\\", "/"), []).append(f"{m['file']}:{m['line']}: [{m['name']}]")
    refused = [ln.split(": ", 1)[1] for ln in done.stdout.splitlines() if ln.startswith("archive not allowed: ")]
    if done.returncode == 3 and not refused:
        say("publishability check: the checker refused an archive but no 'archive not allowed' line could be read")
        return ["the publishability checker output could not be read"]
    if done.returncode == 1 and not found:
        say("publishability check: the checker reported hits that could not be read from its output")
        return ["the publishability checker output could not be read"]

    reasons = []
    unexpected = 0
    for file, lines in sorted(found.items()):
        n_ok, blob_ok = hits_ok.get(file, (0, ""))
        changed = n_ok > 0 and blobs.get(file) != blob_ok
        excess = len(lines) if changed else len(lines) - n_ok
        if excess > 0:
            unexpected += excess
            say(f"publishability check: {len(lines)} hit(s) in {file} (reviewed: {n_ok}"
                + ("; the file changed since its review" if changed else "") + "):")
            for hit in lines:
                say(f"  {hit}")
    allowed_n = sum(min(len(v), hits_ok.get(k, (0, ""))[0]) for k, v in found.items())
    if unexpected:
        reasons.append(f"publishability: {unexpected} hit(s) outside the reviewed allow-list")
    if refused:
        say(f"publishability check: {len(refused)} unopened archive(s) outside the reviewed allow-list:")
        for a in refused:
            say(f"  {a}")
        reasons.append(f"publishability: {len(refused)} unopened archive(s) outside the reviewed allow-list")
    if not reasons:
        say(f"publishability check: no hits outside the reviewed allow-list ({allowed_n} reviewed hits,"
            f" {len(archives_ok)} reviewed archives allowed, {exported} tracked files exported)")
    return reasons


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def default_runner(cmd: list[str], env: dict, cwd: Path, timeout: float, out_path: Path) -> int | None:
    with open(out_path, "wb") as out:
        kwargs: dict = {}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            kwargs["start_new_session"] = True
        try:
            proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=out, stderr=subprocess.STDOUT,
                                    stdin=subprocess.DEVNULL, **kwargs)
        except OSError as exc:
            out.write(f"cannot start {cmd[0]}: {exc}\n".encode())
            return 127
        # The deadline is wall-clock time (time.time()), waited for in short slices. A single
        # proc.wait(timeout=...) is not enough: on Windows it does not count time spent in standby
        # or hibernation, and a run that slept for 3770 s outlived a 4200 s timeout (build ticket 88).
        deadline = time.time() + timeout
        while True:
            done = proc.poll()
            if done is not None:
                return done
            left = deadline - time.time()
            if left <= 0:
                _kill_tree(proc)
                return None
            try:
                return proc.wait(timeout=min(WAIT_SLICE_S, left))
            except subprocess.TimeoutExpired:
                continue


def _kill_tree(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True,
                           timeout=KILL_WAIT_S / 2)
        except subprocess.SubprocessError:
            pass  # proc.kill() below still ends the direct child
    else:
        import signal

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.wait(timeout=KILL_WAIT_S / 2)
    except subprocess.TimeoutExpired:
        proc.kill()


# --------------------------------------------------------------------------- main


class GitError(subprocess.SubprocessError):
    """A failed ``_git`` call; its text is one line naming the git command and why (build ticket 79)."""


class StartError(Exception):
    """A start failure other than git; its text is the reason in the RED verdict line (build ticket 88)."""


def _git(*args: str, root: Path | None = None, strip: bool = True) -> str:
    what = " ".join(("git", *args))
    try:
        out = subprocess.run(["git", "-C", str(root or ROOT), *args], capture_output=True, text=True,
                             check=True, timeout=GIT_TIMEOUT_S).stdout
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"{what} timed out after {GIT_TIMEOUT_S} s") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        first = next((line.strip() for line in stderr.splitlines() if line.strip()), "(no message)")
        raise GitError(f"{what} exited {exc.returncode}: {first}") from exc
    except OSError as exc:
        raise GitError(" ".join(f"{what} could not start: {exc}".split())) from exc
    return out.strip() if strip else out


def tree_state(root: Path) -> tuple[str, list[str]]:
    """(HEAD sha, porcelain status lines). Untracked files are listed one by one whatever
    ``status.showUntrackedFiles`` says; the output is not stripped, so the first line keeps its
    leading space (`` M`` and ``M `` stay apart). Raises on any git error."""
    sha = _git("rev-parse", "--verify", "HEAD", root=root)
    status = _git("status", "--porcelain", "--untracked-files=all", root=root, strip=False)
    return sha, [line for line in status.splitlines() if line.strip()]


def recheck_tree(root: Path, sha: str, dirty: list[str], say: Callable[[str], None]) -> list[str]:
    """Reasons for RED if HEAD or the porcelain status at the end differ from the start (build
    ticket 70). A recheck that cannot run is RED too: the gate never assumes the tree is unchanged."""
    try:
        end_sha, end_dirty = tree_state(root)
    except (subprocess.SubprocessError, OSError) as exc:
        say(f"RED: the tree could not be read again at the end: {exc}")
        return ["the tree could not be rechecked at the end (git failed)"]
    reasons = []
    if end_sha != sha:
        reasons.append(f"HEAD changed during the run ({sha[:7]} -> {end_sha[:7]})")
        say(f"RED: HEAD was {sha} at the start and is {end_sha} at the end")
    if end_dirty != dirty:
        changed = sorted(set(dirty) ^ set(end_dirty)) or end_dirty
        reasons.append(f"working tree changed during the run ({len(changed)} status lines: {changed[0].strip()}"
                       + (", ..." if len(changed) > 1 else "") + ")")
        say("RED: the working tree's status changed during the run; start vs end:")
        for line in sorted(set(dirty) - set(end_dirty)):
            say(f"  start only: {line}")
        for line in sorted(set(end_dirty) - set(dirty)):
            say(f"  end only:   {line}")
    if not reasons:
        say(f"tree recheck at the end: HEAD {end_sha[:7]}, status unchanged ({len(end_dirty)} lines)")
    return reasons


# --------------------------------------------------------------------------- log cleanup (build ticket 79)
# The gates of every worktree share one log directory, and each run adds about 6 files (about
# 0.3 MB): 399 files and 18 MB after 66 runs in two days (2026-10-21). Each run starts by removing
# whole old runs. A run's age is that of its newest file, by mtime (os.stat, not the directory
# listing's cached copy, which Windows does not update while a file is open for writing).
LOG_MAX_AGE_S = 14 * 24 * 3600  # a run older than this is removed
LOG_KEEP_RUNS = 100  # beyond the newest 100 runs, a run is removed once it is older than LOG_MIN_AGE_S
# A gate run lasts at most its printed worst case (about 168 min; builder.md's outer timeout 180
# min), so a run whose newest file is older than a day has ended. Every file a run writes is
# created during the run, so its mtime is no older than the run's start.
LOG_MIN_AGE_S = 24 * 3600
_LOG_FILE = re.compile(  # only files the gate names (fullmatch); anything else is never touched
    r"(?P<run>[0-9a-f]{7}(?:-dirty)?(?:-\d{8}-\d{6}-\d+(?:-\d+)?)?)"
    r"\.(?:log|(?:import|network|collect|pytest|lint|rerun-\d+)\.out)")


@dataclass
class Cleanup:
    removed_runs: int = 0
    removed_files: int = 0
    removed_bytes: int = 0
    kept_runs: int = 0
    skipped: list[str] = field(default_factory=list)  # files that could not be removed, and why


def prune_logs(log_dir: Path, current_run: str, now: float | None = None) -> Cleanup:
    """Remove old gate runs' files from ``log_dir``: a run older than ``LOG_MAX_AGE_S``, or beyond
    the newest ``LOG_KEEP_RUNS`` runs and older than ``LOG_MIN_AGE_S``. ``current_run``'s files are
    excluded by name. A run's ``.log`` goes first: a running gate holds its log open from before
    its first step to its verdict, and Windows refuses to remove an open file, so a held run is
    skipped whole. A file another gate removed first is no error; any other file that cannot be
    removed is skipped and reported."""
    now = time.time() if now is None else now
    runs: dict[str, list[tuple[Path, float, int]]] = {}
    with os.scandir(log_dir) as entries:
        for entry in entries:
            m = _LOG_FILE.fullmatch(entry.name)
            if m is None or m.group("run") == current_run:
                continue
            try:
                if not entry.is_file(follow_symlinks=False):
                    continue
                st = os.stat(entry.path, follow_symlinks=False)
            except OSError:
                continue  # gone already (another gate's cleanup) or unreadable: left alone
            runs.setdefault(m.group("run"), []).append((Path(entry.path), st.st_mtime, st.st_size))
    newest = {run: max(t for _, t, _ in files) for run, files in runs.items()}
    res = Cleanup()
    for rank, run in enumerate(sorted(runs, key=lambda r: (newest[r], r), reverse=True)):
        age = now - newest[run]
        if not (age > LOG_MAX_AGE_S or (rank >= LOG_KEEP_RUNS and age > LOG_MIN_AGE_S)):
            res.kept_runs += 1
            continue
        whole = True
        for path, _, size in sorted(runs[run], key=lambda f: not f[0].name.endswith(".log")):
            try:
                os.remove(path)
            except FileNotFoundError:
                continue  # another gate starting at the same time removed it first
            except OSError as exc:
                whole = False
                res.skipped.append(f"{path.name} ({type(exc).__name__}: {exc.strerror or exc})")
                if path.name.endswith(".log"):
                    break  # held: the run may still be going, so none of its files are touched
                continue
            res.removed_files += 1
            res.removed_bytes += size
        if whole:
            res.removed_runs += 1
        else:
            res.kept_runs += 1
    return res


def cleanup_logs(log_dir: Path, current_run: str, say: Callable[[str], None]) -> None:
    """``prune_logs`` that never fails the gate: any error is reported and the run goes on."""
    try:
        c = prune_logs(log_dir, current_run)
    except Exception as exc:  # housekeeping only: never a reason for RED, never a traceback
        say(f"log cleanup: failed and skipped ({type(exc).__name__}: {exc})")
        return
    say(f"log cleanup: removed {c.removed_runs} runs ({c.removed_files} files, {c.removed_bytes / 1e6:.1f} MB)"
        f" older than {LOG_MAX_AGE_S // 86400} days, or beyond the newest {LOG_KEEP_RUNS} and older than"
        f" {LOG_MIN_AGE_S // 3600} h; kept {c.kept_runs}; {len(c.skipped)} files could not be removed")
    for s in c.skipped[:10]:
        say(f"  not removed (held or locked): {s}")
    if len(c.skipped) > 10:
        say(f"  ... and {len(c.skipped) - 10} more")


def default_python(root: Path | None = None) -> str:
    env = os.environ.get("KICAD_MCP_PYTHON")
    if env:
        return env
    common = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir", root=root))
    main_checkout = common.parent
    for rel in (".venv/Scripts/python.exe", ".venv/bin/python"):
        if (main_checkout / rel).exists():
            return str(main_checkout / rel)
    raise StartError(f"no shared interpreter under {main_checkout}/.venv; pass --python")


def hook_states(root: Path | None = None) -> tuple[str, str]:
    """(pre-push guard, identity guard) as installed in the common git dir, from one git call: each
    is ``installed``, ``NOT INSTALLED (...)``, ``DIFFERS from ...`` or ``unknown``."""
    try:
        hooks = Path(_git("rev-parse", "--path-format=absolute", "--git-common-dir", root=root)) / "hooks"
    except (subprocess.SubprocessError, OSError):
        return "unknown", "unknown"

    def norm(data: bytes) -> bytes:
        return data.replace(b"\r\n", b"\n")

    def state(name: str, source: Path) -> str:
        hook = hooks / name
        if not hook.exists():
            return f"NOT INSTALLED (cp scripts/git-hooks/{name} \"$(git rev-parse --git-common-dir)/hooks/\")"
        try:
            same = norm(hook.read_bytes()) == norm(((root or ROOT) / source).read_bytes())
        except OSError:
            return "unknown"
        return "installed" if same else f"DIFFERS from scripts/git-hooks/{name}"

    return state("pre-push", PRE_PUSH), state("commit-msg", COMMIT_MSG)


def pre_push_state(root: Path | None = None) -> str:
    return hook_states(root)[0]


def _timeout(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text}") from None
    if not math.isfinite(value) or value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive finite number of seconds: {text}")
    return value


def expected_duration(suite_timeout: float) -> str:
    """The line a waiting agent reads to know when a run has hung (fork P11)."""
    worst = worst_case_s(suite_timeout)
    return (f"expected duration: about {round((SUITE_MEASURED_S + OVERHEAD_S) / 60)} min (pytest took up to"
            f" {SUITE_MEASURED_S} s, {SUITE_MEASURED_WHEN}); the suite times out at"
            f" {int(suite_timeout)} s and the gate ends at worst about {round(worst / 60)} min after its start."
            " A run still going after that has hung: stop it and escalate (fork playbook P11, docs/TESTING.md)")


def main(argv: list[str] | None = None, *, root: Path = ROOT, runner: Runner = default_runner) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--python", help="interpreter for pytest and lint (default: the shared .venv)")
    ap.add_argument("--timeout", type=_timeout, default=SUITE_TIMEOUT_S,
                    help=f"suite timeout in seconds (default {SUITE_TIMEOUT_S}, about 3x the slowest measured suite with only other gates known to be running; enforced on the wall clock)")
    ap.add_argument("--publishable-only", action="store_true",
                    help="run only the publishability check over the tracked files (for a push); prints"
                         " PUBLISHABLE: OK or PUBLISHABLE: RED (...) and exits 0 or 1")
    ap.add_argument("--log-dir", type=Path, default=Path(tempfile.gettempdir()) / "kicad-mcp-batch-gate")
    args = ap.parse_args(argv)

    if args.publishable_only:
        reasons = check_publishable_tracked(root, args.python or sys.executable, print)
        print("PUBLISHABLE: OK" if not reasons else f"PUBLISHABLE: RED ({'; '.join(reasons)})")
        return 0 if not reasons else 1

    # A git failure here still ends in a verdict line, never a traceback (build ticket 79).
    s7 = "<unknown>"
    try:
        toplevel = _git("rev-parse", "--show-toplevel", root=root)
        if not same_path(toplevel, root):
            print(f"BATCH GATE {s7}: RED (script at {root} is not the top of its git tree {toplevel})")
            return 1
        sha, dirty = tree_state(root)
        s7 = sha[:7] + ("-dirty" if dirty else "")
        python = args.python or default_python(root)
    except (subprocess.SubprocessError, OSError) as exc:
        print(f"BATCH GATE {s7}: RED (could not read the tree at the start: {exc})", flush=True)
        return 1
    except StartError as exc:
        print(f"BATCH GATE {s7}: RED ({exc})", flush=True)
        return 1
    if inside(args.log_dir, root):
        # its new files would change the status (or hide in an ignored path): never a verdict
        print(f"BATCH GATE {s7}: RED (--log-dir {args.log_dir} lies inside the tree; use a directory outside it)")
        return 1
    try:
        args.log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"BATCH GATE {s7}: RED (could not create the log directory {args.log_dir}: {exc})", flush=True)
        return 1
    # Gates of several worktrees share the log directory, so each run gets its own file names
    # (sha7, start time, pid); "x" refuses a name another run already holds (build ticket 70).
    stem = f"{s7}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    for n in range(1000):
        run_id = stem if n == 0 else f"{stem}-{n}"
        log_path = args.log_dir / f"{run_id}.log"
        try:
            log = open(log_path, "x", encoding="utf-8")
            break
        except FileExistsError:
            continue
        except OSError as exc:
            print(f"BATCH GATE {s7}: RED (could not create the log {log_path}: {exc})", flush=True)
            return 1
    else:
        print(f"BATCH GATE {s7}: RED (no free log name for {stem} in {args.log_dir})")
        return 1

    def say(msg: str) -> None:
        print(msg, flush=True)
        log.write(msg + "\n")
        log.flush()

    say(f"batch gate: worktree {root}, HEAD {sha}")
    say(f"interpreter: {python}")
    say(f"log: {log_path}")
    say(expected_duration(args.timeout))
    pre_push, identity = hook_states(root)
    say(f"pre-push guard: {pre_push}")
    say(f"identity guard (commit-msg hook): {identity}")
    cleanup_logs(args.log_dir, run_id, say)
    known_red, known_skips, pyproject = root / KNOWN_RED, root / KNOWN_SKIPS, root / "pyproject.toml"
    reading = pyproject
    try:  # a file that exists but cannot be read is a start failure, never a traceback (ticket 88)
        pyproject_text = pyproject.read_text(encoding="utf-8")
        reading = known_red
        known_text = known_red.read_text(encoding="utf-8") if known_red.exists() else None
        reading = known_skips
        skips_text = known_skips.read_text(encoding="utf-8") if known_skips.exists() else None
    except (OSError, UnicodeDecodeError) as exc:
        say(f"BATCH GATE {s7}: RED (could not read {reading}: {exc})")
        log.close()
        return 1
    try:
        tracked = [p for p in _git("ls-files", "--", "tests/test_*.py", root=root).splitlines() if p]
    except (subprocess.SubprocessError, OSError):
        tracked = None
    res = run_gate(root=root, sha=sha, dirty=dirty, python=python, known_red_text=known_text,
                   known_skips_text=skips_text, tracked_tests=tracked,
                   pyproject_text=pyproject_text,
                   runner=runner, log_dir=args.log_dir, say=say, suite_timeout=args.timeout, run_id=run_id)
    # The tree is read again last: an edit, commit or checkout during the run makes it void (ticket 70).
    res.reasons += recheck_tree(root, sha, dirty, say)
    verdict = "GREEN" if res.green else f"RED ({'; '.join(res.reasons)})"
    say(f"BATCH GATE {s7}: {verdict}")
    log.close()
    return 0 if res.green else 1


if __name__ == "__main__":
    sys.exit(main())
