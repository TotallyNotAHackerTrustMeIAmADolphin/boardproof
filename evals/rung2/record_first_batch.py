"""Condense a rung-2 batch report into evals/rung2/first_batch_record.json.

Usage: python evals/rung2/record_first_batch.py <report.json> <runner_summary.json> [<freerouting_summary.json>]
The full per-attempt gate results (complete unconnected and violation lists) stay in
the report; this record is the per-attempt table plus the labels. Measurement only.
"""
import json
import sys
from collections import Counter
from pathlib import Path


def record(report: dict, krt_summary: dict, freerouting: dict | None) -> dict:
    rows = []
    for a in report["attempts"]:
        g = a["gate"]
        rows.append({
            "index": a["index"], "lever": a["lever"], "parameters": a["parameters"], "status": a["status"],
            "cause": a["cause"], "router_wall_s": round(a["router_wall_s"], 1),
            "backend_wall_s": round(a["backend_wall_s"], 1), "verify_wall_s": round(a["verify_wall_s"], 1),
            "gate_verdict": g["verdict"], "unconnected_edges": len(g["unconnected"]),
            "unconnected_per_net": dict(sorted(Counter(u["net"] for u in g["unconnected"]).items())),
            "violation_count": len(g["violations"]),
            "violation_types": dict(Counter(v["type"] for v in g["violations"])),
            "list_complete": g["list_complete"], "unstable": g["unstable"],
        })
    return {
        "rung": report["rung"], "backend": report["backend"], "protocol_version": report["protocol_version"],
        "run_dir": report["run_dir"], "report": "report.json inside run_dir (complete lists per attempt)",
        "attempts_passed": report["attempts_passed"], "attempts_planned": report["attempts_planned"],
        "input_fail_floor": report["input_fail_floor"], "input_floor_reasons": report["input_floor_reasons"],
        "unofficial": report["unofficial"], "unofficial_reasons": report["unofficial_reasons"],
        "assisted": report["assisted"], "autonomous_pass": report["autonomous_pass"],
        "total_wall_s": round(report["total_wall_s"], 1), "tool_versions": report["tool_versions"],
        "attempts": rows, "freerouting": freerouting,
        "source": source(report["protocol_version"]),
    }


ATTRIBUTION = "THIRD_PARTY.md, section Attribution: pic_programmer (CC BY-SA 4.0; paradigm-review-2 ticket 15)"


def source(protocol_version: str) -> dict:
    """The rung-2 source a batch was pinned to: KiCad master 6a26ce9 up to protocol 3, the
    KiCad 10.0.x release demo from protocol 4 on (build ticket 51)."""
    base = {"project": "KiCad", "path": "demos/pic_programmer", "attribution_confirmed": True,
            "attribution": ATTRIBUTION}
    if str(protocol_version) in ("1", "2", "3"):
        return {**base, "commit": "6a26ce9df3067b2233b415ba2d4cbc3164272ccc"}
    return {**base, "release": "KiCad 10.0.x (demos/pic_programmer identical in tags 10.0.0 to 10.0.7)"}


if __name__ == "__main__":
    rep = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    fr = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8")) if len(sys.argv) > 3 else None
    out = Path(__file__).parent / "first_batch_record.json"
    out.write_text(json.dumps(record(rep, {}, fr), indent=1), encoding="utf-8")
    print(out)
