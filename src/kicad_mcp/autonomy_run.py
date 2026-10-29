"""CLI for one honest rung measurement.

    python -m kicad_mcp.autonomy_run --rung 2 --backend krt --board <path/to/board.kicad_pcb> \\
        --parity required --runs-root runs

Protocol: pivot-ladder ticket 6; constants and
attempt plans live in ``autonomy_protocol.json``. Setup order, never inside an attempt:

1. refuse an unavailable backend for the whole batch (nothing is touched or frozen);
2. input preparation (copper-layer text to silkscreen) through ``input_prep``,
   dry run then confirm, which re-pins the baseline and reports the input floor;
3. otherwise pin the baseline on the board as it is;
4. hand over to ``rung_runner.run_rung``, which freezes the manifest and runs the plan.

Exit codes: 0 a batch ran (whatever its verdict: this is a measurement, read the report),
2 setup could not complete, 3 backend unavailable, 4 input refused (not scored).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import secrets
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kicad_mcp import input_prep, rung_backends, rung_runner, verify_board
from kicad_mcp.gate_state import _scan

# Anchored to the repository, not the working directory: a run from anywhere else is not
# the repository's local runs folder (git-ignored; the committed history of earlier protocols is
# evals/ladder_results.json) and is labelled unofficial.
DEFAULT_RUNS_ROOT = Path(__file__).resolve().parents[2] / "runs"
EXIT_OK, EXIT_SETUP, EXIT_UNAVAILABLE, EXIT_REFUSED = 0, 2, 3, 4
_RUNG_LABEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,31}")


def board_sha(board: str | Path) -> str | None:
    """The rung pin of the board file: its content sha (``rung_runner.content_sha256``, CRLF
    read as LF, nothing else normalised), or None when it cannot be read."""
    try:
        return rung_runner.content_sha256(Path(board))
    except OSError:
        return None


def project_sha(board: str | Path) -> str | None:
    """The rung pin of the whole graded project: one sha over the board and every file the
    gate reads, walked, ordered and laid out exactly like gate_state's own fingerprint
    (``_scan``; ``name\\0sha\\0`` per file), with each file's content sha in place of its raw
    byte sha. For a project whose files are all LF the two are equal (tested). None when the
    project cannot be fingerprinted (too large, unreadable input), as in gate_state.

    gate_state's freshness fingerprint stays on raw bytes: only the pins are normalised."""
    pcb = Path(board)
    board_pin = board_sha(pcb)
    if board_pin is None:
        return None
    scan = _scan(pcb.parent)
    if scan.error is not None:
        return None
    digest = hashlib.sha256()
    digest.update(f"{pcb.name}\0{board_pin}\0".encode("utf-8"))
    for relative, full, _info in scan.inputs:
        try:
            file_pin = rung_runner.content_sha256(full)
        except OSError:
            return None
        digest.update(f"{relative}\0{file_pin}\0".encode("utf-8"))
    return digest.hexdigest()


def _setup_failed(reason: str, **extra: Any) -> dict:
    return {"status": "SETUP_FAILED", "reason": reason, **extra}


def _fail_setup(runs_root: Path, rung: Any, backend_name: str, protocol: dict, reason: str,
                board: Path | None = None, **extra: Any) -> dict:
    """A setup failure is visible in the index too (kind setup_failed), not only returned."""
    sha = None
    if board is not None:
        try:
            sha = rung_runner._sha256_file(Path(board))
        except OSError:
            sha = None
    rung_runner.record_setup_failed(
        rung=rung, backend_name=backend_name, runs_root=runs_root, reason=reason, board_sha256=sha,
        protocol_version=protocol["protocol_version"],
    )
    return _setup_failed(reason, **extra)


def _prepare_input(
    board: Path, *, rung: Any, backend_name: str, runs_root: Path, parity: str,
    parity_reason: str | None, drc: Callable | None,
) -> tuple[Path, dict, dict] | dict:
    """``(board_to_measure, baseline, setup)`` or a ``SETUP_FAILED`` dict."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = runs_root / "_prepared" / f"{rung}-{backend_name}-{stamp}-{secrets.token_hex(3)}"
    proposal = input_prep.propose_input_preparation(board, out_dir)
    status = proposal.get("status")
    source_bytes = board.read_bytes()  # one read: the raw sha and the board pin of the same bytes
    setup: dict[str, Any] = {
        "source_board": str(board),
        "source_board_sha256": rung_runner._sha256_bytes(source_bytes),
        "source_board_pin_sha256": rung_runner.content_sha256_bytes(source_bytes),
        "baseline_pinned_by": "autonomy_run setup, before the manifest, once",
    }
    if status == "proposed":
        confirmed = input_prep.confirm_input_preparation(
            board, proposal["revision_token"], parity=parity, parity_reason=parity_reason, drc=drc,
        )
        if confirmed.get("status") != "prepared":
            return _setup_failed(
                f"input preparation did not complete: {confirmed.get('reason') or confirmed.get('status')}",
                input_preparation=confirmed,
            )
        setup["input_preparation"] = {
            "status": "prepared",
            "moved": confirmed["moved"],
            "unhandled": confirmed["unhandled"],
            "output_pcb": confirmed["output_pcb"],
        }
        setup["input_floor"] = {
            **confirmed["input_floor"],
            "unhandled": confirmed["unhandled"],
            "router_text_check_ok": not confirmed["unhandled"],
        }
        return Path(confirmed["output_pcb"]), confirmed["baseline"], setup
    if status != "nothing_to_do":
        return _setup_failed(
            f"input preparation could not be proposed: {proposal.get('reason') or status}",
            input_preparation=proposal,
        )
    try:
        baseline = verify_board.snapshot_rules(board, drc=drc)
        floor = verify_board.verify_board(
            board, parity=parity, parity_reason=parity_reason, baseline=baseline, drc=drc
        )
    except Exception as exc:  # noqa: BLE001 - e.g. kicad-cli missing
        return _setup_failed(f"could not pin the baseline: {type(exc).__name__}: {exc}")
    setup["input_preparation"] = {
        "status": "nothing_to_do", "moved": [], "unhandled": proposal.get("unhandled", []),
    }
    setup["input_floor"] = {
        "grading_status": floor["verdict"],
        "certified": False,
        "violations": floor["violations"],
        "unconnected": floor["unconnected"],
        "error_count": len(floor["violations"]),
        "unconnected_nets": len(floor["unconnected"]),
        "unstable": floor["unstable"],
        "list_complete": floor["list_complete"],
        "incomplete_reasons": floor["incomplete_reasons"],
        "notes": floor.get("notes", []),  # informational: never change the verdict
        "unhandled": proposal.get("unhandled", []),
        "router_text_check_ok": not proposal.get("unhandled"),
    }
    return board, baseline, setup


def _rung_reasons(rung: Any, source_sha: str | None, source_project_sha: str | None, protocol: dict) -> list[str]:
    """A rung label is bound to a source: official needs the protocol to pin that rung's
    board AND its whole graded project (every file the gate reads) by sha256, and both to
    match, so a matching board with a relaxed ``.kicad_dru`` beside it is unofficial.
    ``source_sha`` and ``source_project_sha`` are the source's pins (``board_sha``,
    ``project_sha``: content shas, CRLF read as LF), never raw byte shas."""
    entry = (protocol.get("rungs") or {}).get(str(rung))
    if entry is None:
        return [f"rung {rung!r} is not in the protocol's rung table"]
    board_pin, project_pin = entry.get("board_sha256"), entry.get("project_sha256")
    if not board_pin or not project_pin:
        return [f"rung fixture not pinned: rung {rung} needs both board_sha256 and project_sha256 "
                "in the protocol file"]
    reasons = []
    if board_pin != source_sha:
        reasons.append(f"source board does not match the pinned rung fixture for rung {rung}")
    if source_project_sha is None or project_pin != source_project_sha:
        reasons.append(
            f"source project (rules, project settings, libraries) does not match the pinned rung "
            f"fixture for rung {rung}"
        )
    return reasons


def parse_expect_pours(values: list[str] | None) -> set[tuple[str, str]] | None:
    """``--expect-pour NET:LAYER`` (repeatable) -> {(net, layer)}; None when not given."""
    if not values:
        return None
    out = set()
    for value in values:
        net, sep, layer = value.rpartition(":")
        if not sep or not net or not layer:
            raise ValueError(f"--expect-pour wants NET:LAYER, got {value!r}")
        out.add((net, layer))
    return out


def _exit_code(outcome: dict) -> int:
    return {"RAN": EXIT_OK, "UNAVAILABLE": EXIT_UNAVAILABLE, "REFUSED": EXIT_REFUSED}.get(
        outcome["status"], EXIT_SETUP
    )


def run_measurement(
    *,
    rung: Any,
    backend_name: str,
    board: Path,
    runs_root: Path,
    parity: str,
    parity_reason: str | None = None,
    rule_files: list[Path] | None = None,
    backend: rung_runner.Backend | None = None,
    availability: dict | None = None,
    drc: Callable | None = None,
    protocol: dict | None = None,
    preflight: Callable[[Path], str | None] | None = None,
    expected_pours: set[tuple[str, str]] | None = None,
    **run_kwargs: Any,
) -> dict:
    """One batch for one rung on one backend. ``backend``, ``availability``, ``drc`` and
    ``run_kwargs`` (clock, repo_state, budget, ...) are test seams; the CLI passes none."""
    board, runs_root = Path(board), Path(runs_root)
    protocol = protocol if protocol is not None else rung_runner.load_protocol()
    if not _RUNG_LABEL.fullmatch(str(rung)):
        return _fail_setup(runs_root, rung, backend_name, protocol,
                           f"rung label {str(rung)!r} must be a plain name (letters, digits, _ or -)", board)
    spec = (protocol.get("backends") or {}).get(backend_name)
    if spec is None:
        return _fail_setup(runs_root, rung, backend_name, protocol,
                           f"unknown backend {backend_name!r}; the protocol declares "
                           f"{sorted(protocol['backends'])}", board)
    plan = spec["plan"]
    if not board.is_file():
        return _fail_setup(runs_root, rung, backend_name, protocol, f"board not found: {board}", board)

    # One availability probe per measurement; its result is passed along everywhere.
    info = availability if availability is not None else rung_backends.availability(backend_name)
    if not info.get("available"):
        report = rung_runner.run_unavailable(
            rung=rung, backend_name=backend_name, runs_root=runs_root,
            reason=str(info.get("reason")), availability=info, plan=plan,
            protocol_version=protocol["protocol_version"],
        )
        return {"status": "UNAVAILABLE", "report": report, "reason": report["reason"]}

    prepared = _prepare_input(
        board, rung=rung, backend_name=backend_name, runs_root=runs_root, parity=parity,
        parity_reason=parity_reason, drc=drc,
    )
    if isinstance(prepared, dict):
        rung_runner.record_setup_failed(
            rung=rung, backend_name=backend_name, runs_root=runs_root, reason=prepared["reason"],
            board_sha256=rung_runner._safe_sha(board), protocol_version=protocol["protocol_version"],
        )
        return prepared
    measured, baseline, setup = prepared
    # The rung pins of the source (content shas, CRLF read as LF). The board pin was taken
    # in _prepare_input from the same read as the raw source_board_sha256.
    setup["source_project_pin_sha256"] = project_sha(board)
    setup["measured_board_sha256"] = rung_runner._safe_sha(measured)
    zones: list[dict] = []
    setup["pour_zones_error"] = None
    try:  # pours in the measured input; outline-only ones are allowed (see rung_backends)
        zones = rung_backends.pour_zones(measured)
    except Exception as exc:  # noqa: BLE001 - recorded, never fatal: the preflight decides refusal
        setup["pour_zones_error"] = f"{type(exc).__name__}: {exc}"
    setup["pour_zones"] = zones
    setup["pour_provenance"] = "structural: not verified as pour-step output"
    setup["warnings"] = rung_backends.pour_warnings(zones, expected_pours)

    def gate(candidate: Path) -> dict:
        return verify_board.verify_board(
            candidate, parity=parity, parity_reason=parity_reason, baseline=baseline, drc=drc
        )

    rules = rule_files if rule_files is not None else [
        p for p in (measured.with_suffix(".kicad_pro"), measured.with_suffix(".kicad_dru")) if p.is_file()
    ]
    versions = rung_backends.tool_versions(backend_name, info) if availability is None else {
        "injected_availability": info.get("versions") or {}
    }
    extra = rung_backends.unofficial_reasons(info)
    if rung_runner.SEAMS_ENFORCED:
        for name, injected in (("drc", drc is not None), ("backend", backend is not None),
                               ("availability", availability is not None),
                               ("rule_files", rule_files is not None),
                               ("preflight", preflight is not None)):
            if injected:
                extra.append(f"seam injected: {name}")
    if Path(runs_root).resolve() != Path(DEFAULT_RUNS_ROOT).resolve():
        extra.append(f"runs root {runs_root} is not the repository's local runs folder")
    extra.extend(_rung_reasons(
        rung, setup["source_board_pin_sha256"], setup["source_project_pin_sha256"], protocol))
    report = rung_runner.run_rung(
        rung=rung, backend_name=backend_name, board=measured, rule_files=rules, plan=plan,
        backend=backend or rung_backends.make_backend(backend_name), gate=gate, baseline=baseline,
        tool_versions=versions, runs_root=runs_root,
        budget_s=float(protocol["budget_s"]), attempt_timeout_s=float(protocol["attempt_timeout_s"]),
        protocol_version=protocol["protocol_version"], protocol=protocol,
        extra_unofficial_reasons=extra, setup=setup,
        invariants=rung_backends.board_invariants,
        preflight=preflight if preflight is not None else rung_backends.input_refusal,
        **run_kwargs,
    )
    if report.get("status") == "REFUSED":
        return {"status": "REFUSED", "report": report, "reason": report["reason"],
                "result": rung_runner.rung_result(runs_root, rung, backend_name,
                                                  protocol["protocol_version"])}
    return {
        "status": "RAN",
        "report": report,
        "result": rung_runner.rung_result(
            runs_root, rung, backend_name, protocol["protocol_version"]
        ),
    }


def _under_width_line(item: Any) -> str:
    if not isinstance(item, dict):
        return repr(item)
    delivered, required = item.get("delivered_mm"), item.get("required_mm")
    if not isinstance(delivered, (int, float)) or not isinstance(required, (int, float)):
        return repr(item)
    verdict = item.get("neck_verdict")
    tail = ""
    if verdict == "necked":
        tail = f" [forced neck at {item.get('neck_pad')}, {item.get('neck_distance_mm'):.3g} mm from the pad edge]"
    return (f"{item.get('net')} {item.get('layer')} {item.get('kind')} {item.get('dimension')} "
            f"{delivered:g} mm < {required:g} mm ({item.get('required_from')}){tail}")


def _under_width_lines(report: dict) -> list[dict]:
    """Every under-width item of every attempt, one line each (net, layer, delivered vs
    required), from the shared check's facts (``rung_backends.with_under_width_check``,
    build ticket 36). The full records, with raw positions, stay in ``report.json`` under
    each attempt's ``router_facts.delivered_below_requested``. Attempts without items are
    left out."""
    out = []
    for attempt in report.get("attempts") or []:
        facts = attempt.get("router_facts")
        items = facts.get("delivered_below_requested") if isinstance(facts, dict) else None
        if isinstance(items, list) and items:
            out.append({"attempt": attempt.get("index"), "items": [_under_width_line(i) for i in items]})
    return out


def _summary(outcome: dict, rung: Any, backend_name: str) -> dict:
    report = outcome.get("report") or {}
    base = {"status": outcome["status"], "rung": rung, "backend": backend_name}
    if outcome["status"] == "SETUP_FAILED":
        return {**base, "reason": outcome["reason"]}
    if outcome["status"] in ("UNAVAILABLE", "REFUSED"):
        return {**base, "reason": outcome["reason"], "attempts_planned": report.get("attempts_planned"),
                "attempts_run": 0, "run_dir": report.get("run_dir")}
    return {
        **base,
        "run_dir": report["run_dir"],
        "attempts_passed": report["attempts_passed"],
        "attempts_run": report["attempts_run"],
        "attempts_planned": report["attempts_planned"],
        "autonomous_pass": report["autonomous_pass"],
        # Protocol 6: "PASS (necked)" when every passing attempt carries forced necks, "PASS" when one
        # passed plain, None without an autonomous pass. The boolean above is never the whole story.
        "autonomous_pass_label": report.get("autonomous_pass_label"),
        "autonomous_pass_necked": report.get("autonomous_pass_necked", False),
        "attempts_passed_necked": report.get("attempts_passed_necked", 0),
        # "attempts_passed" counts plain and necked passes together; these two never merge.
        "attempts_passed_plain": report["attempts_passed"] - report.get("attempts_passed_necked", 0),
        "assisted": report["assisted"],
        "unofficial": report["unofficial"],
        "unofficial_reasons": report["unofficial_reasons"],
        "input_floor": report.get("input_floor"),
        "input_fail_floor": report.get("input_fail_floor", False),
        "input_floor_reasons": report.get("input_floor_reasons", []),
        "attempts_delivered_below_requested": report.get("attempts_delivered_below_requested", 0),
        "under_width": _under_width_lines(report),
        "attempts_with_removed_copper": report.get("attempts_with_removed_copper", 0),
        "best_attempt": report["best_attempt"],
        "total_wall_s": report["total_wall_s"],
        # A necked pass reads "PASS (necked)" here too, never a plain "PASS" (protocol 6).
        "attempt_statuses": [a.get("status_label", a["status"]) for a in report["attempts"]],
        "attempt_status_labels": [a.get("status_label", a["status"]) for a in report["attempts"]],
        "rung_result": outcome["result"]["summary"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m kicad_mcp.autonomy_run", description=__doc__.split("\n\n")[0])
    parser.add_argument("--rung", required=True, help="rung label, e.g. 1 or 2")
    parser.add_argument("--backend", required=True, choices=["krt", "freerouting"])
    parser.add_argument("--board", required=True, type=Path)
    parser.add_argument("--parity", required=True, choices=list(verify_board.PARITY_MODES))
    parser.add_argument("--parity-reason", default=None)
    parser.add_argument("--runs-root", type=Path, default=DEFAULT_RUNS_ROOT)
    parser.add_argument("--expect-pour", action="append", metavar="NET:LAYER",
                        help="a pour the pour step was asked for (repeatable); any other pour "
                             "in the input is listed under setup.warnings")
    args = parser.parse_args(argv)
    # ASCII decimals only: str.isdigit() is True for superscripts that int() rejects.
    rung: Any = int(args.rung) if args.rung.isascii() and args.rung.isdecimal() else args.rung
    try:
        expected = parse_expect_pours(args.expect_pour)
    except ValueError as exc:
        parser.error(str(exc))
    outcome = run_measurement(
        rung=rung, backend_name=args.backend, board=args.board, runs_root=args.runs_root,
        parity=args.parity, parity_reason=args.parity_reason, expected_pours=expected,
    )
    print(json.dumps(_summary(outcome, rung, args.backend), indent=2, sort_keys=True))
    return _exit_code(outcome)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
