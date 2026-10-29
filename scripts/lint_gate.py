"""Lint gate: ruff findings against a shrink-only baseline (build ticket 45).

    python scripts/lint_gate.py            # check; exit 0 only when nothing grew or shrank
    python scripts/lint_gate.py --shrink   # lower the baseline to the current counts (never raises)

The baseline (``scripts/ruff-baseline.json``) freezes the findings that existed when ruff was
adopted, as a count per (file, rule code), so line moves do not matter. The check fails when:

- a (file, code) pair has more findings than frozen (a new finding; all findings of that pair are
  printed, because a count cannot tell which one is new);
- a pair has fewer findings than frozen (fixed: run ``--shrink`` and commit the smaller baseline,
  the same ratchet rule as ``tests/ratchets.toml``);
- ruff is missing, is not the version pinned in ``pyproject.toml``'s dev extra, times out, exits
  non-zero, or its output cannot be read (unknown is never clean);
- ``[tool.ruff]`` differs from ``EXPECTED_RUFF_CONFIG`` below (no ignores, excludes or
  per-file-ignores without a reviewed change to this file).

Ruff runs with ``--config pyproject.toml`` (a nested ``ruff.toml`` cannot change the rules) and
``--ignore-noqa`` (a ``# noqa`` comment cannot hide a finding).

Ruff runs as ``$RUFF`` when set (a path to the ruff executable), otherwise as
``<this interpreter> -m ruff``. It lints the whole tree with the ``[tool.ruff]`` settings of
``pyproject.toml``, from the repository root this script lives in.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tomllib
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "ruff-baseline.json"
PYPROJECT = ROOT / "pyproject.toml"

Key = tuple[str, str]

# The reviewed [tool.ruff] table of pyproject.toml. The gate refuses any other table, so ignores,
# excludes or per-file-ignores cannot be added without changing this line (and so a review).
EXPECTED_RUFF_CONFIG = {
    "target-version": "py311",
    "extend-exclude": ["third_party"],  # git submodules: other projects' code (publish-prep ticket 03b)
    "lint": {"select": ["E4", "E7", "E9", "F", "B"]},
}


def pinned_ruff_version(pyproject: Path = PYPROJECT) -> str:
    """The exact ``ruff==X`` pin of the dev extra; raises when there is none."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    for req in data["project"]["optional-dependencies"]["dev"]:
        m = re.fullmatch(r"\s*ruff\s*==\s*([0-9][^\s;,]*)\s*", req)
        if m:
            return m.group(1)
    raise ValueError("pyproject.toml's dev extra has no exact ruff==<version> pin")


def ruff_command() -> list[str]:
    exe = os.environ.get("RUFF")
    return [exe] if exe else [sys.executable, "-m", "ruff"]


def count_findings(findings: list[dict], root: Path = ROOT) -> tuple[Counter, dict[Key, list[str]]]:
    """Counts per (posix path relative to root, code) and a printable line per finding."""
    counts: Counter = Counter()
    lines: dict[Key, list[str]] = {}
    for f in findings:
        path = Path(f["filename"])
        try:
            rel = path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel = path.as_posix()
        code = f.get("code") or "syntax-error"
        key = (rel, code)
        counts[key] += 1
        loc = f.get("location") or {}
        lines.setdefault(key, []).append(
            f"{rel}:{loc.get('row', '?')}:{loc.get('column', '?')}: {code} {f.get('message', '')}"
        )
    return counts, lines


def flatten(baseline: dict) -> Counter:
    return Counter({(p, c): n for p, codes in baseline["findings"].items() for c, n in codes.items()})


def nest(counts: Counter) -> dict:
    out: dict[str, dict[str, int]] = {}
    for (p, c), n in sorted(counts.items()):
        if n > 0:
            out.setdefault(p, {})[c] = n
    return out


def compare(frozen: Counter, current: Counter) -> tuple[list[Key], list[Key]]:
    """(grown, shrunk) keys, each sorted."""
    keys = set(frozen) | set(current)
    grown = sorted(k for k in keys if current.get(k, 0) > frozen.get(k, 0))
    shrunk = sorted(k for k in keys if current.get(k, 0) < frozen.get(k, 0))
    return grown, shrunk


def shrink(frozen: Counter, current: Counter) -> Counter:
    """The baseline lowered to the current counts; never raised, never given a new key."""
    return Counter({k: min(n, current.get(k, 0)) for k, n in frozen.items() if min(n, current.get(k, 0)) > 0})


def check_config(pyproject: Path) -> str | None:
    """None when ``[tool.ruff]`` is exactly the reviewed configuration, else the reason."""
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    actual = data.get("tool", {}).get("ruff")
    if actual != EXPECTED_RUFF_CONFIG:
        return (
            f"[tool.ruff] in {pyproject.name} is {actual!r}, not the reviewed {EXPECTED_RUFF_CONFIG!r};"
            " a change to the rules, ignores or excludes goes through a ticket and a review, and"
            " then updates EXPECTED_RUFF_CONFIG in scripts/lint_gate.py"
        )
    return None


def run_ruff(root: Path, ruff_cmd: list[str], pyproject: Path, timeout: float) -> tuple[str, list[dict]]:
    """(version, findings). Raises RuntimeError when ruff cannot be run or read."""
    try:
        ver = subprocess.run(ruff_cmd + ["--version"], cwd=root, capture_output=True, text=True, timeout=120)
    except OSError as exc:
        raise RuntimeError(f"ruff cannot be started ({' '.join(ruff_cmd)}): {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ruff --version timed out ({' '.join(ruff_cmd)})") from exc
    m = re.search(r"ruff (\S+)", ver.stdout)
    if ver.returncode != 0 or not m:
        raise RuntimeError(
            f"ruff is not installed for {' '.join(ruff_cmd)} (rc {ver.returncode}): "
            f"{(ver.stderr or ver.stdout).strip()[:300]}. Install the pinned version into that"
            f" interpreter: <python> -m pip install ruff=={pinned_ruff_version(pyproject)}"
        )
    # --config: the reviewed pyproject.toml settings for every file, so a nested ruff.toml or
    # .ruff.toml cannot switch rules off. --ignore-noqa: a line or file-level noqa comment
    # cannot hide a finding from the baseline.
    cmd = ruff_cmd + ["check", "--no-cache", "--config", str(pyproject), "--ignore-noqa",
                      "--output-format", "json", "--exit-zero", "."]
    try:
        proc = subprocess.run(cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"ruff check timed out after {timeout:.0f} s") from exc
    if proc.returncode != 0:
        raise RuntimeError(f"ruff check failed (rc {proc.returncode}): {proc.stderr.strip()[:500]}")
    try:
        findings = json.loads(proc.stdout)
    except ValueError as exc:
        raise RuntimeError(f"ruff output is not JSON: {exc}") from exc
    if not isinstance(findings, list):
        raise RuntimeError("ruff JSON output is not a list of findings")
    return m.group(1), findings


def main(argv: list[str] | None = None, *, root: Path = ROOT, ruff_cmd: list[str] | None = None,
         timeout: float = 600) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--shrink", action="store_true", help="lower the baseline to the current counts")
    ap.add_argument("--init", action="store_true", help="write the first baseline (refused if one exists)")
    args = ap.parse_args(argv)
    baseline_path = root / "scripts" / "ruff-baseline.json"
    pyproject = root / "pyproject.toml"

    try:
        pin = pinned_ruff_version(pyproject)
        reason = check_config(pyproject)
        if reason:
            print(f"lint: FAILED: {reason}")
            return 1
        version, findings = run_ruff(root, ruff_cmd or ruff_command(), pyproject, timeout)
    except (RuntimeError, ValueError, KeyError, OSError) as exc:
        print(f"lint: FAILED: {exc}")
        return 1
    if version != pin:
        print(f"lint: FAILED: ruff {version} runs, but pyproject.toml pins ruff=={pin}")
        return 1
    current, lines = count_findings(findings, root)

    if args.init:
        if baseline_path.exists():
            print(f"lint: FAILED: {baseline_path.name} exists; --init only writes the first baseline")
            return 1
        _write(baseline_path, current, version)
        print(f"lint: baseline written: {sum(current.values())} findings in {len(current)} (file, code) pairs")
        return 0

    try:
        frozen = flatten(json.loads(baseline_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        print(f"lint: FAILED: baseline {baseline_path} unreadable: {exc}")
        return 1

    if args.shrink:
        lowered = shrink(frozen, current)
        _write(baseline_path, lowered, version)
        print(f"lint: baseline lowered from {sum(frozen.values())} to {sum(lowered.values())} findings")
        frozen = lowered

    grown, shrunk = compare(frozen, current)
    for key in grown:
        print(f"lint: NEW {key[1]} in {key[0]}: {current[key]} now, {frozen.get(key, 0)} frozen")
        for line in lines.get(key, []):
            print(f"  {line}")
    for key in shrunk:
        print(
            f"lint: FIXED {key[1]} in {key[0]}: {current.get(key, 0)} now, {frozen[key]} frozen;"
            " lower the baseline with: python scripts/lint_gate.py --shrink"
        )
    if grown or shrunk:
        print(f"lint: FAILED ({len(grown)} grown, {len(shrunk)} shrunk; ruff {version})")
        return 1
    print(f"lint: OK (ruff {version}; {sum(frozen.values())} findings frozen in {baseline_path.name})")
    return 0


def _write(path: Path, counts: Counter, version: str) -> None:
    doc = {
        "_comment": (
            "Shrink-only ruff baseline (build ticket 45): findings per file and rule code, counted"
            " with --ignore-noqa, that existed when ruff was adopted. Never raise a count or add an"
            " entry; fix new findings. Lower it with: python scripts/lint_gate.py --shrink"
        ),
        "ruff": version,
        "findings": nest(counts),
    }
    path.write_text(json.dumps(doc, indent=2, sort_keys=False) + "\n", encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.exit(main())
