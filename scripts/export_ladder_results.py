"""One-off converter: a ladder runs root -> the committed, sanitised ``evals/ladder_results.json``.

The ladder's run directories (manifest, report, attempts with whole project copies, machine paths)
stay local in the git-ignored ``runs/`` folder. What is committed is this file: the history of the
official and unofficial batches of the protocols already measured. ``rung_runner`` reads its rows as
history in addition to the live ``index.jsonl`` of the runs root (``rung_runner.HISTORY_FILE``).

Usage: ``python scripts/export_ladder_results.py <runs root> [--out evals/ladder_results.json]``.

Shape: ``rows`` are the index lines in their original order (the order is the event order the slot
logic relies on), with every machine path removed and ``run_dir`` replaced by the stable batch id
``history/<rung>/<backend>/<stamp>``. ``batches`` holds, per scored batch id, the report's facts:
tool versions, the per-attempt verdict and cause fields, and the complete per-net unconnected list
and violation list of the best attempt as recorded. Nothing here is recomputed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SCHEMA = 1
# A Windows drive path; directory names may hold spaces, the last segment ends at white space.
_WIN_PATH = re.compile(r'[A-Za-z]:[\\/](?:[^\\/:*?"<>|\r\n]+[\\/])*[^\\/:*?"<>|\s]*')
_POSIX_HOME = re.compile(r"/(?:Users|home)/[^\s\"']+")
_ATTEMPT_KEYS = ("index", "lever", "parameters", "status", "status_label", "cause", "detail",
                 "necked_count", "necked_length_mm", "delivered_below_requested_count",
                 "delivered_below_requested_policy_hit", "invariant_violations", "removed_copper")


def sanitise(value):
    """Every machine path in every string, recursively, becomes ``<path>``."""
    if isinstance(value, str):
        return _POSIX_HOME.sub("<path>", _WIN_PATH.sub("<path>", value))
    if isinstance(value, list):
        return [sanitise(v) for v in value]
    if isinstance(value, dict):
        return {k: sanitise(v) for k, v in value.items()}
    return value


def batch_id(rung, backend, run_dir) -> str:
    stamp = re.split(r"[\\/]", str(run_dir).rstrip("\\/"))[-1]
    return f"history/{rung}/{backend}/{stamp}"


def _row(line: dict) -> dict:
    out = sanitise(line)
    if "run_dir" in line:
        out["run_dir"] = batch_id(line["rung"], line["backend"], line["run_dir"])
    return out


def _gate_view(gate: dict | None) -> dict | None:
    if not isinstance(gate, dict):
        return None
    return sanitise({k: gate.get(k) for k in (
        "verdict", "list_complete", "incomplete_reasons", "unstable", "unconnected", "violations", "warnings")})


def _detail(report: dict) -> dict:
    attempts = []
    for a in report.get("attempts", []):
        view = {k: a[k] for k in _ATTEMPT_KEYS if k in a}
        gate = a.get("gate") or {}
        view["gate_verdict"] = gate.get("verdict")
        view["unconnected_count"] = len(gate.get("unconnected") or [])
        view["violation_count"] = len(gate.get("violations") or [])
        attempts.append(sanitise(view))
    best = report.get("best_attempt")
    best_gate = None
    if isinstance(best, int):
        for a in report.get("attempts", []):
            if a.get("index") == best:
                best_gate = _gate_view(a.get("gate"))
    return {
        "date_started": None,
        "tool_versions": sanitise(report.get("tool_versions", {})),
        "input_floor_reasons": sanitise(report.get("input_floor_reasons", [])),
        "attempts": attempts,
        "best_attempt": best,
        "best_attempt_gate": best_gate,
    }


def export(runs_root: Path) -> dict:
    runs_root = Path(runs_root)
    rows, batches = [], {}
    for text in (runs_root / "index.jsonl").read_text(encoding="utf-8").splitlines():
        if not text:
            continue
        line = json.loads(text)
        rows.append(_row(line))
        if line.get("kind") != "batch":
            continue
        report_path = runs_root / str(line["rung"]) / line["backend"] / re.split(
            r"[\\/]", str(line["run_dir"]).rstrip("\\/"))[-1] / "report.json"
        if report_path.is_file():
            detail = _detail(json.loads(report_path.read_text(encoding="utf-8")))
            detail["date_started"] = line.get("date")
            batches[rows[-1]["run_dir"]] = detail
    return {
        "schema": SCHEMA,
        "note": ("Sanitised history of the ladder's measured batches (protocols 2 onward). "
                 "rows = index lines in event order; batches = per-batch facts. "
                 "Written by scripts/export_ladder_results.py; the run directories are local only."),
        "rows": rows,
        "batches": batches,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs_root", type=Path)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "evals" / "ladder_results.json")
    args = parser.parse_args(argv)
    data = export(args.runs_root)
    text = json.dumps(data, indent=1, sort_keys=True) + "\n"
    if _WIN_PATH.search(text) or _POSIX_HOME.search(text):
        print("a machine path survived the sanitiser; nothing written", file=sys.stderr)
        return 1
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(f"{len(data['rows'])} rows, {len(data['batches'])} batches -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
