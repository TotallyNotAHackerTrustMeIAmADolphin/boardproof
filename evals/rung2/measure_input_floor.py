"""Rung-2 input floor measurement (build ticket 07).

Measures, on scratch copies only, what error-level findings the stripped and
prepared ``pic_programmer`` input has BEFORE any routing, whether KiCadRoutingTools'
silkscreen label beautifier clears them, and writes the decision:

    evals/rung2/input_floor_result.json   every step with its complete gate result
    evals/rung2/input_fail_floor.json     the manifest declaration (complete list)

Usage (from the repo root, with the project venv):
    .venv/Scripts/python.exe evals/rung2/measure_input_floor.py <pic_programmer_dir> <work_dir>

``<pic_programmer_dir>`` is KiCad's ``demos/pic_programmer`` from the source the current
protocol pins (``source(protocol_version)``): KiCad master ``6a26ce9`` up to protocol 3,
where the committed ``input_floor_result.json`` / ``input_fail_floor.json`` were measured
(build ticket 07), and the KiCad 10.0.x release demo from protocol 4 on (build ticket 51;
the install's ``share/kicad/demos/pic_programmer``). It is only read. Nothing is written
into it or into openMPPT. The committed records are not rewritten: their ``source`` block
is the protocol-3 one, with ``attribution_confirmed: false`` as it was then.
No rule, severity or exclusion is ever changed here: the same pinned baseline
grades every step.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

_SOURCE_BASE = {
    "project": "KiCad",
    "path": "demos/pic_programmer",
    "file_version": 20260206,
    "licence": "CC BY-SA 4.0 (KiCad's licence for its demo projects)",
    "attribution_confirmed": True,
    "attribution_note": (
        "CC BY-SA 4.0 (KiCad's licence for its demo projects; the demo has no licence file of its own), "
        "decided in paradigm-review-2 ticket 15; source, licence and changes in "
        "THIRD_PARTY.md (section Attribution: pic_programmer). Modified copies carry that attribution."
    ),
}


def source(protocol_version: str) -> dict:
    """The rung-2 source the given protocol pins: KiCad master 6a26ce9 up to protocol 3,
    the KiCad 10.0.x release demo from protocol 4 on (build ticket 51)."""
    if str(protocol_version) in ("1", "2", "3"):
        return {**_SOURCE_BASE, "commit": "6a26ce9df3067b2233b415ba2d4cbc3164272ccc"}
    return {**_SOURCE_BASE, "release": "KiCad 10.0.x (demos/pic_programmer identical in tags 10.0.0 to 10.0.7)"}

BEAUTIFIER_TIMEOUT_S = 300


# --- pure record builders (tested) -------------------------------------------


def _sig(v: dict) -> tuple:
    items = sorted(
        (i.get("description", ""), round(i.get("pos", {}).get("x", 0.0), 3), round(i.get("pos", {}).get("y", 0.0), 3))
        for i in v.get("items", [])
    )
    return (v.get("type"), tuple(items))


def diff_gate_results(before: dict, after: dict) -> dict:
    """Which error-level violations the step cleared, created, or left, by signature
    (type + item descriptions + rounded positions; runtime uuids are not identity)."""
    b = {_sig(v): v for v in before["violations"]}
    a = {_sig(v): v for v in after["violations"]}
    return {
        "cleared": [b[k] for k in b if k not in a],
        "created": [a[k] for k in a if k not in b],
        "persisting": [a[k] for k in a if k in b],
    }


def floor_declaration(graded: dict, *, source: dict, reason: str) -> dict:
    """The manifest's ``input_fail_floor``: complete list, never a waiver."""
    return {
        "input_fail_floor": bool(graded["violations"]),
        "rung": 2,
        "source": source,
        "reason": reason,
        "violations": list(graded["violations"]),
        "violation_types": dict(Counter(v.get("type") for v in graded["violations"])),
        "unconnected_nets_before_routing": len(graded["unconnected"]),
        "waived": [],
        "rules_changed": False,
        "report_label": "rung 2 (input_fail_floor: errors present before routing; not attributable to any router)"
        if graded["violations"]
        else "rung 2",
    }


def choose_floor(prepared: dict, beautified: dict) -> tuple[str, dict, str]:
    """``(board, graded, reason)``. The beautified board is adopted only when it
    reaches zero errors (a deterministic, recorded input step). Otherwise the floor
    is the prepared board's: a fix attempt that adds errors is never the floor."""
    if not beautified["violations"]:
        if not prepared["violations"]:
            return "prepared", prepared, "floor is zero after input preparation alone"
        return "beautified", beautified, "beautifier step reached zero errors: deterministic recorded input step"
    if len(beautified["violations"]) < len(prepared["violations"]):
        return "beautified", beautified, "beautifier reduced but did not clear the floor"
    return "prepared", prepared, (
        f"beautifier did not clear the floor ({len(prepared['violations'])} errors before, "
        f"{len(beautified['violations'])} after): the unmodified prepared board is the floor"
    )


def summarize(graded: dict) -> dict:
    un = Counter(u["net"] for u in graded["unconnected"])
    return {
        "verdict": graded["verdict"],
        "unstable": graded["unstable"],
        "list_complete": graded["list_complete"],
        "incomplete_reasons": graded["incomplete_reasons"],
        "runs": graded["runs"],
        "violation_count": len(graded["violations"]),
        "violation_types": dict(Counter(v.get("type") for v in graded["violations"])),
        "violations": graded["violations"],
        "warning_types": dict(Counter(v.get("type") for v in graded["warnings"])),
        "warnings": graded["warnings"],
        "unconnected_edges": len(graded["unconnected"]),
        "unconnected_per_net": dict(sorted(un.items())),
        "unconnected": graded["unconnected"],
    }


# --- measurement --------------------------------------------------------------


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)).replace("\\", "/"): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def _raw_counts(pcb: Path) -> dict:
    from kicad_tools.sexp import parse_file

    root = parse_file(str(pcb))
    wanted = ("footprint", "segment", "via", "zone", "gr_text", "gr_text_box")
    counts = Counter(c.name for c in root.children)
    return {k: counts.get(k, 0) for k in wanted}


def strip_tracks(src_pcb: Path, dst_pcb: Path) -> dict:
    """Segments, arcs, vias and zones removed exactly as ``stage_input.py`` stages the pinned
    rung-2 input: through the write layer, nothing else changed (build ticket 67; until then
    ``PCB.load`` + ``strip_traces``, which also added a net table)."""
    import stage_input  # the sibling script: this directory is sys.path[0] when this script runs

    if Path(src_pcb).resolve() != Path(dst_pcb).resolve():
        shutil.copyfile(src_pcb, dst_pcb)
    removed = stage_input.strip_tracks(Path(dst_pcb))
    return {"removed": removed, "before": _raw_counts(src_pcb), "after": _raw_counts(dst_pcb)}


def _step(name: str, graded: dict, **extra) -> dict:
    return {"name": name, "gate": summarize(graded), **extra}


def run_beautifier(krt_dir: Path, python: Path, src_pcb: Path, out_pcb: Path, report: Path) -> dict:
    cmd = [str(python), str(krt_dir / "py_placer" / "beautify_labels.py"), str(src_pcb), str(out_pcb),
           "--report-json", str(report)]
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=BEAUTIFIER_TIMEOUT_S, cwd=str(krt_dir))
        rc, out, err, timed_out = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        rc, out, err, timed_out = None, exc.stdout or "", exc.stderr or "", True
    return {
        "command": [Path(c).name if i < 2 else c for i, c in enumerate(cmd)],
        "krt_version": (krt_dir / "VERSION").read_text().strip(),
        "returncode": rc,
        "timed_out": timed_out,
        "seconds": round(time.time() - started, 1),
        "stdout_tail": out[-2000:],
        "stderr_tail": err[-2000:],
    }


def measure(fixture_dir: Path, work: Path, krt_dir: Path, python: Path) -> dict:
    from kicad_mcp import input_prep, rung_runner
    from kicad_mcp import verify_board as gate

    pinned_source = source(rung_runner.load_protocol()["protocol_version"])
    fixture_before = _tree_hashes(fixture_dir)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    stripped_dir = work / "stripped"
    shutil.copytree(fixture_dir, stripped_dir)
    pcb = stripped_dir / "pic_programmer.kicad_pcb"
    strip = strip_tracks(fixture_dir / "pic_programmer.kicad_pcb", pcb)

    proposal = input_prep.propose_input_preparation(pcb, work / "prepared")
    if proposal["status"] != "proposed":
        raise RuntimeError(f"input_prep did not propose: {proposal}")
    confirmed = input_prep.confirm_input_preparation(pcb, proposal["revision_token"], parity="required")
    if confirmed["status"] != "prepared":
        raise RuntimeError(f"input_prep did not prepare: {confirmed}")
    prepared_pcb = Path(confirmed["output_pcb"])
    baseline = confirmed["baseline"]  # pinned BEFORE any fix attempt

    # Fresh complete gate result of the prepared board (the floor), same pinned rules.
    floor = gate.verify_board(prepared_pcb, parity="required", baseline=baseline)

    steps = [_step("stripped_prepared_baseline", floor)]
    # raw copper-text facts of the prepared board
    text_check = input_prep.check_router_text_moves(prepared_pcb)
    prep_hashes = _tree_hashes(prepared_pcb.parent)

    beaut_in = work / "beaut_in"
    beaut_out = work / "beaut_out"
    shutil.copytree(prepared_pcb.parent, beaut_in)
    shutil.copytree(prepared_pcb.parent, beaut_out)
    rep = work / "beautifier_report.json"
    run = run_beautifier(krt_dir, python, beaut_in / pcb.name, beaut_out / pcb.name, rep)
    after_hashes = _tree_hashes(beaut_out)
    after = gate.verify_board(beaut_out / pcb.name, parity="required", baseline=baseline)
    steps.append(
        _step(
            "beautifier_after",
            after,
            beautifier=run,
            files_changed=sorted(k for k in after_hashes if prep_hashes.get(k) != after_hashes[k]),
            raw_counts_before=_raw_counts(prepared_pcb),
            raw_counts_after=_raw_counts(beaut_out / pcb.name),
            label_report=json.loads(rep.read_text(encoding="utf-8")) if rep.is_file() else None,
        )
    )
    diff = diff_gate_results(floor, after)

    decision_source, floor_graded, why = choose_floor(floor, after)
    declaration = floor_declaration(floor_graded, source=pinned_source, reason=why)
    declaration["floor_board"] = decision_source

    fixture_after = _tree_hashes(fixture_dir)
    return {
        "source": pinned_source,
        "fixture_untouched": fixture_before == fixture_after,
        "fixture_pcb_sha256": fixture_before["pic_programmer.kicad_pcb"],
        "strip": strip,
        "input_prep": {
            "moved": confirmed["moved"],
            "unhandled": confirmed["unhandled"],
            "router_text_check_ok": text_check["ok"],
            "prepared_pcb_sha256": _sha(prepared_pcb),
        },
        "baseline_rules": baseline,
        "steps": steps,
        "beautifier_diff": diff,
        "decision": {
            "input_fail_floor": declaration["input_fail_floor"],
            "floor_board": decision_source,
            "reason": why,
            "floor_violations": declaration["violations"],
            "rules_changed": False,
            "re_pin_needed": decision_source == "beautified" and not declaration["input_fail_floor"],
        },
        "declaration": declaration,
    }


def main(argv: list[str]) -> int:
    from kicad_mcp.routers import kicad_routing_tools as krt

    fixture, work = Path(argv[1]), Path(argv[2])
    krt_dir = krt.find_kicad_routing_tools()
    python = krt.find_kicad_routing_tools_python()
    result = measure(fixture, work, Path(krt_dir), Path(python))
    out = Path(__file__).parent
    (out / "input_floor_result.json").write_text(json.dumps(result, indent=1, default=str), encoding="utf-8")
    (out / "input_fail_floor.json").write_text(json.dumps(result["declaration"], indent=1, default=str), encoding="utf-8")
    d = result["decision"]
    print("input_fail_floor:", d["input_fail_floor"], "|", d["reason"], "| violations:", len(d["floor_violations"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
