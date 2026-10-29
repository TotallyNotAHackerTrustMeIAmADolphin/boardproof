"""Rung runner: one honest measurement of one rung on one backend.

Protocol: pivot-ladder ticket 6. The
runner writes a frozen manifest first, runs the planned attempts from the
identical input, grades each with the gate and writes a complete report plus
append-only index lines. Anything touched by hand relabels the run "assisted",
detected by hash and log comparison, never by self-report.

Two seams are injected so the protocol is provable without a router:

* ``backend(board, parameters, timeout_s, log) -> {"status": "ok" | "timeout" |
  "failure", "cause": str | None}`` mutates ``board`` (a fresh copy) in place.
  Ticket 06 supplies the real KiCadRoutingTools and Freerouting adapters.
* ``gate(board) -> verify_board result``; ticket 06 binds
  ``verify_board.verify_board`` with the parity declaration and pinned baseline.
  Whatever it returns is validated here before it can count as a pass.

Index lines (``runs_root/index.jsonl``, append-only): ``started`` right after the
manifest is frozen (so a killed run stays visible as a crashed, failed batch),
``batch`` when the report is complete, ``rerun`` for an unplanned re-run.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROTOCOL_FILE = Path(__file__).with_name("autonomy_protocol.json")
# The committed, sanitised history of the batches already measured (protocols 2 onward), written
# once by scripts/export_ladder_results.py. The run directories themselves stay local in the
# git-ignored ``runs/`` folder; this file is how a fresh checkout still knows which official slots
# are taken.
HISTORY_FILE = Path(__file__).resolve().parents[2] / "evals" / "ladder_results.json"


def protocol_sha256(path: Path | None = None) -> str:
    """The protocol file's content sha (CRLF read as LF, see ``content_sha256``), so a
    Windows (autocrlf) and a Linux checkout of the same commit key the same batch slot."""
    return content_sha256(Path(path or PROTOCOL_FILE))


def content_sha256(path: Path) -> str:
    """sha256 of a text file's CONTENT: every CRLF pair is read as LF; nothing else is
    normalised (a lone CR, a space, any other byte still changes the sha). Protocol 4 pins
    rungs and the protocol with it (build ticket 51): KiCad reads tokens, not layout
    (docs/ARCHITECTURE.md, ticket 03), and git autocrlf, the Windows installer and Linux packages
    hand out the same content with different line endings. Raises ``OSError``."""
    return content_sha256_bytes(Path(path).read_bytes())


def content_sha256_bytes(data: bytes) -> str:
    """``content_sha256`` of bytes already read: only CRLF pairs become LF."""
    return _sha256_bytes(data.replace(b"\r\n", b"\n"))


def load_protocol(path: Path | None = None) -> dict:
    """The committed protocol: version, N, budget, per-attempt cap and the per-backend
    lever tables and attempt plans. Malformed means refuse, never a silent default."""
    try:
        data = json.loads(Path(path or PROTOCOL_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"protocol file unreadable: {exc}") from exc
    problems = []
    if not isinstance(data.get("policies"), dict) or data["policies"].get("delivered_below_requested") != "fail":
        problems.append("policies.delivered_below_requested must be 'fail'")
    neck = data["policies"].get("necked_pad_entry") if isinstance(data.get("policies"), dict) else None
    bound = neck.get("max_length_beyond_pad_mm") if isinstance(neck, dict) else None
    if (not isinstance(neck, dict) or neck.get("label") != "PASS (necked)"
            or isinstance(bound, bool) or not isinstance(bound, (int, float)) or not 0 < bound < 100):
        problems.append("policies.necked_pad_entry needs label 'PASS (necked)' and a positive "
                        "max_length_beyond_pad_mm (build ticket 109)")
    for key, kind in (("protocol_version", str), ("attempts", int), ("budget_s", (int, float)),
                      ("attempt_timeout_s", (int, float)), ("backends", dict)):
        if not isinstance(data.get(key), kind) or isinstance(data.get(key), bool):
            problems.append(f"{key!r} missing or wrong type")
    if not problems:
        for name, spec in data["backends"].items():
            levers, plan = spec.get("levers"), spec.get("plan")
            if not isinstance(levers, dict) or not isinstance(plan, list):
                problems.append(f"backend {name!r} needs levers and plan")
                continue
            if len(plan) != data["attempts"]:
                problems.append(f"backend {name!r} plan has {len(plan)} entries, not {data['attempts']}")
            for entry in plan:
                problems.extend(f"backend {name!r}: {p}" for p in plan_problems(entry, levers))
    if problems:
        raise ValueError("protocol file invalid: " + "; ".join(problems))
    return data


def plan_problems(entry: Any, vocabulary: dict) -> list[str]:
    """Why a plan entry is not made of pre-declared discrete lever values (empty = ok)."""
    if not isinstance(entry, dict) or not isinstance(entry.get("lever"), str):
        return ["plan entry needs a 'lever' name"]
    params = entry.get("parameters")
    if not isinstance(params, dict):
        return [f"plan entry {entry.get('lever')!r} needs a 'parameters' object"]
    problems = []
    for key, value in params.items():
        if key not in vocabulary:
            problems.append(f"{entry['lever']}: {key!r} is not a pre-declared lever")
        elif value not in vocabulary[key]:
            problems.append(f"{entry['lever']}: {key}={value!r} is not one of {vocabulary[key]}")
    return problems


_PROTOCOL = load_protocol()
PROTOCOL_VERSION = _PROTOCOL["protocol_version"]
# A run that deviates from the protocol file is "unofficial": reported, never an
# autonomous pass.
EXPECTED_ATTEMPTS = _PROTOCOL["attempts"]
DEFAULT_BUDGET_S = float(_PROTOCOL["budget_s"])
DEFAULT_ATTEMPT_TIMEOUT_S = float(_PROTOCOL["attempt_timeout_s"])
# Test-only switch (tests/conftest.py): when False, injected seams do not mark a run
# unofficial, so the runner's own fake-seam tests can observe an official run.
SEAMS_ENFORCED = True
CAUSE_TAGS = (
    "delivered-below-requested",
    "router-timeout", "router-failure", "budget-exceeded", "rules-conflict", "input-floor",
    "invariant-violated", "input-refused",
)

# The label of a PASS that carries forced necks (policies.necked_pad_entry.label, build ticket 109).
NECKED_LABEL = _PROTOCOL["policies"]["necked_pad_entry"]["label"]

Backend = Callable[[Path, dict, float, Path], dict]
Gate = Callable[[Path], dict]

_VERDICTS = ("PASS", "FAIL", "INCOMPLETE")
_STATUS_RANK = {"PASS": 0, "FAIL": 1, "TIMEOUT": 2, "INCOMPLETE": 3}
# A NEW file in an attempt copy matching this set is something the gate's graded copy
# would read (rules, project settings, schematic, libraries, lib tables). The set
# mirrors ``gate_state.project_sha``'s walk (``_INPUT_SUFFIXES`` / ``_INPUT_NAMES``) plus
# anything inside a ``*.pretty`` directory; it is imported when available so the two
# cannot drift, with a copy as fallback. Matching is case-insensitive (NTFS is).
try:
    from kicad_mcp.gate_state import _INPUT_NAMES, _INPUT_SUFFIXES
except ImportError:  # pragma: no cover - copy of gate_state's sets
    _INPUT_SUFFIXES = {
        ".kicad_sch", ".kicad_dru", ".kicad_pro", ".kicad_sym", ".kicad_mod", ".kicad_prl",
    }
    _INPUT_NAMES = {"fp-lib-table", "sym-lib-table"}


def _gate_reads(rel: str) -> bool:
    """True if a file at this relative path is one the gate's graded copy could read."""
    path = Path(rel)
    name = path.name.lower()
    return (
        path.suffix.lower() in {x.lower() for x in _INPUT_SUFFIXES}
        or name in {x.lower() for x in _INPUT_NAMES}
        or any(part.lower().endswith(".pretty") for part in path.parts[:-1])
    )


# --- hashing and small file helpers ----------------------------------------------


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(json.dumps(value, sort_keys=True, default=str).encode())


def _tree_hashes(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root``, keyed by posix relative path."""
    out: dict[str, str | None] = {}
    try:
        paths = sorted(root.rglob("*"))
    except OSError:
        return out
    for p in paths:
        try:
            if p.is_file():
                out[p.relative_to(root).as_posix()] = _sha256_file(p)
        except OSError:
            out[p.relative_to(root).as_posix()] = None  # unreadable counts as changed
    return out


def _safe_sha(path: Path) -> str | None:
    try:
        return _sha256_file(path)
    except OSError:
        return None


def _write_json(path: Path, value: Any) -> None:
    """Write via a temp file and rename so a kill never leaves half a report."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _append_line(path: Path, value: dict) -> None:
    """One ``O_APPEND`` write per line, so concurrent runs cannot interleave."""
    data = (json.dumps(value, sort_keys=True) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o644)
    try:
        os.write(fd, data)
    finally:
        os.close(fd)


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def _git_repo_state(repo_dir: Path | None = None, exclude: Path | None = None) -> dict:
    """Commit and dirty flag for the WHOLE repository (its top level, not just the package
    directory ``repo_dir`` lives in). ``exclude`` (the runs root) is left out of the dirty
    check, so the runner's own manifests, frozen copies and prepared inputs never make it
    dirty. A repository that cannot be queried reads as dirty."""
    cwd = Path(repo_dir) if repo_dir else Path(__file__).resolve().parent
    try:
        top = Path(subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], cwd=cwd, capture_output=True, text=True,
            timeout=30, check=True,
        ).stdout.strip())
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=top, capture_output=True, text=True, timeout=30
        ).stdout.strip()
        args = ["git", "status", "--porcelain", "--", ":/"]
        if exclude is not None:
            try:
                rel = Path(os.path.abspath(exclude)).relative_to(top.resolve()).as_posix()
                args.append(f":(top,exclude){rel}")
            except ValueError:
                pass  # runs root outside the repository: nothing to exclude
        status = subprocess.run(args, cwd=top, capture_output=True, text=True, timeout=30).stdout
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": True}
    return {"commit": commit or None, "dirty": bool(status.strip()) or not commit}


def _new_run_dir(runs_root: Path, rung: Any, backend_name: str, now: datetime) -> Path:
    """Create a unique run directory; ``mkdir`` is the arbiter, so two runs racing
    for the same second get different directories."""
    base = runs_root / str(rung) / backend_name
    base.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%dT%H%M%SZ")
    n = 0
    while True:
        candidate = base / (stamp if n == 0 else f"{stamp}-{n}")
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            n += 1


# --- the gate result is untrusted until validated ----------------------------------


def _incomplete(reasons: list[str], source: Any = None) -> dict:
    lists = source if isinstance(source, dict) else {}

    def as_list(key: str) -> list:
        value = lists.get(key)
        return value if isinstance(value, list) else []

    return {
        "verdict": "INCOMPLETE",
        "unconnected": as_list("unconnected"),
        "violations": as_list("violations"),
        "warnings": as_list("warnings"),
        "unstable": bool(lists.get("unstable", False)),
        "incomplete_reasons": [*[str(r) for r in as_list("incomplete_reasons")], *reasons],
        "notes": as_list("notes"),  # informational: carried, never read for the verdict
    }


def _validate_gate_result(result: Any) -> dict:
    """Return the gate result unchanged if it is well formed and self-consistent,
    otherwise an INCOMPLETE one saying why. A PASS must carry no evidence of failure."""
    if not isinstance(result, dict):
        return _incomplete([f"gate returned {type(result).__name__}, not a result dict"])
    problems = []
    if result.get("verdict") not in _VERDICTS:
        problems.append(f"gate verdict {result.get('verdict')!r} is not PASS/FAIL/INCOMPLETE")
    for key in ("unconnected", "violations", "incomplete_reasons"):
        if not isinstance(result.get(key), list):
            problems.append(f"gate result field {key!r} missing or not a list")
    if not isinstance(result.get("warnings"), list):
        problems.append("gate result field 'warnings' missing or not a list")
    if not isinstance(result.get("unstable"), bool):
        problems.append("gate result field 'unstable' missing or not a bool")
    if not problems and isinstance(result.get("unconnected"), list):
        for entry in result["unconnected"]:
            if not (isinstance(entry, dict) and "net" in entry
                    and isinstance(entry.get("items", []), list)):
                problems.append("gate unconnected entry is not {net, items}")
                break
    if not problems and result["verdict"] == "PASS":
        for key in ("unconnected", "violations", "incomplete_reasons"):
            if result[key]:
                problems.append(f"gate said PASS but {key!r} is not empty")
        if result["unstable"]:
            problems.append("gate said PASS but the result was unstable")
    if problems:
        out = _incomplete(problems, result)
        out["rejected_verdict"] = result.get("verdict")
        return out
    return result


def _grade(gate: Gate, board: Path) -> dict:
    """The gate's validated result; a crashing gate is INCOMPLETE, never a pass."""
    try:
        raw = gate(board)
    except Exception as exc:  # noqa: BLE001 - any gate crash must be recorded, not raised
        return _incomplete([f"gate raised {type(exc).__name__}: {exc}"])
    return _validate_gate_result(raw)


# --- best attempt -------------------------------------------------------------------


def _best_attempt(ran: list[dict]) -> int | None:
    """Coded rule: PASS first (a plain PASS before a PASS (necked)), then fewest unconnected,
    fewest errors, lowest index.

    TIMEOUT and INCOMPLETE rank after FAIL, so empty lists can never win.
    """
    if not ran:
        return None

    def key(a: dict) -> tuple:
        unconnected = sum(max(1, len(n.get("items", []))) for n in a["gate"]["unconnected"])
        return (_STATUS_RANK.get(a["status"], 9), bool(a.get("necked_count")) and a["status"] == "PASS",
                unconnected, len(a["gate"]["violations"]), a["index"])

    return min(ran, key=key)["index"]


# --- attempt log (hash-and-log comparison) --------------------------------------------


def _log_attempt(run_dir: Path, entry: dict) -> None:
    _append_line(run_dir / "attempt_log.jsonl", entry)


def _log_findings(run_dir: Path, planned: int, executed: dict[int, dict]) -> list[str]:
    """The log must hold exactly one entry per executed attempt, matching what was
    executed, and nothing else. Missing, duplicated, edited or extra entries are findings."""
    path = run_dir / "attempt_log.jsonl"
    findings: list[str] = []
    try:
        entries = _read_lines(path)
    except (OSError, ValueError):
        return ["attempt log is unreadable"]
    by_index: dict[int, list[dict]] = {}
    for entry in entries:
        by_index.setdefault(entry.get("index"), []).append(entry)
    for index, group in by_index.items():
        if index not in executed or not isinstance(index, int) or not 0 <= index < planned:
            findings.append(f"attempt log holds an unplanned attempt (index {index})")
        elif len(group) > 1:
            findings.append(f"attempt log holds a repeated attempt (unplanned, index {index})")
    for index, expected in executed.items():
        group = by_index.get(index)
        if not group:
            findings.append(f"attempt log is missing the entry for attempt {index}")
        elif (group[0].get("lever"), group[0].get("parameters")) != (
            expected["lever"], expected["parameters"]
        ):
            findings.append(f"attempt log entry for attempt {index} differs from what ran")
    return findings


# --- one attempt: shared by the planned loop and by rerun_attempt -----------------------


def _attempt_core(
    *,
    backend: Backend,
    gate: Gate,
    board: Path,
    params: dict,
    timeout_s: float,
    log: Path,
    clock: Callable[[], float],
    inspect: Callable[[], list[str]] | None = None,
    over_budget: Callable[[], bool] | None = None,
    invariants: Callable[[], list[str]] | None = None,
) -> dict:
    detail = None
    r0 = clock()
    try:
        outcome = backend(board, dict(params), timeout_s, log)
        if not isinstance(outcome, dict):
            raise TypeError(f"backend returned {type(outcome).__name__}, not a dict")
    except Exception as exc:  # noqa: BLE001 - a router crash is a failed attempt, not a dead run
        outcome = {"status": "failure", "cause": "router-failure"}
        detail = f"{type(exc).__name__}: {exc}"
    r1 = clock()
    backend_wall = r1 - r0
    reported = outcome.get("router_elapsed_s")
    # The cap is charged against the router's own time when the adapter reports it;
    # loading, diffing and applying copper is adapter overhead, still in the budget.
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and 0 <= reported <= backend_wall:
        router_wall = float(reported)
    else:
        router_wall = backend_wall
    inspected = inspect() if inspect else []
    if board.exists():
        gate_result = _grade(gate, board)
    else:
        detail = detail or "output board missing after the backend ran"
        gate_result = _incomplete([detail])
        outcome = {**outcome, "cause": outcome.get("cause") or "router-failure"}
    r2 = clock()

    router_status = outcome.get("status", "ok")
    status, cause = gate_result["verdict"], outcome.get("cause")
    if router_status == "timeout" or router_wall > timeout_s:
        # The cap is enforced here too: a router that ignored it is not a pass.
        status, cause = "TIMEOUT", cause or "router-timeout"
    elif router_status != "ok":
        status = "FAIL" if status == "PASS" else status
        cause = cause or "router-failure"
    if status != "TIMEOUT" and over_budget is not None and over_budget():
        status, cause = "TIMEOUT", "budget-exceeded"
    violated: list[str] = []
    if invariants is not None and board.exists() and status != "TIMEOUT":
        try:
            violated = list(invariants())
        except Exception as exc:  # noqa: BLE001 - unknown invariants are not evidence of none
            status, cause = "INCOMPLETE", "invariant-violated"
            detail = f"invariants could not be computed: {type(exc).__name__}: {exc}"
        else:
            if violated:
                status = "INCOMPLETE" if status == "INCOMPLETE" else "FAIL"
                cause = "invariant-violated"
                detail = (detail + "; " if detail else "") + (
                    "attempt changed what must stay fixed: " + ", ".join(violated)
                )
    facts = outcome.get("facts")
    facts_d = facts if isinstance(facts, dict) else {}
    below = facts_d.get("delivered_below_requested")
    below_n = len(below) if isinstance(below, list) else 0
    # Protocol 6 (build ticket 109): an item the shared check classed as a forced neck is counted and
    # labelled, not a policy hit; every other item (no verdict included) is one.
    necked = [i for i in below if isinstance(i, dict) and i.get("neck_verdict") == "necked"] if below_n else []
    necked_length = 0.0
    for item in necked:
        length = item.get("length_mm")
        necked_length += float(length) if isinstance(length, (int, float)) and not isinstance(length, bool) else 0.0
    policy_hit = below_n - len(necked) > 0 or bool(facts_d.get("strict_sizes_violated"))
    if policy_hit and status in ("PASS", "FAIL"):
        # Policy (protocol file): graded by the gate as usual, but never a pass.
        if status == "PASS" or cause is None:
            cause = "delivered-below-requested"
        status = "FAIL"
    removed_seg = facts_d.get("removed_segment_count")
    removed_via = facts_d.get("removed_via_count")
    removed = None
    if (isinstance(removed_seg, int) and removed_seg > 0) or (isinstance(removed_via, int) and removed_via > 0):
        removed = {"segments": removed_seg or 0, "vias": removed_via or 0}
    return {
        "delivered_below_requested_count": below_n,
        "delivered_below_requested_policy_hit": policy_hit,
        "necked_count": len(necked),
        "necked_length_mm": necked_length,
        "status_label": NECKED_LABEL if status == "PASS" and necked else status,
        "removed_copper": removed,
        "status": status, "cause": cause, "detail": detail, "gate": gate_result,
        "router_wall_s": router_wall, "backend_wall_s": backend_wall,
        "adapter_overhead_s": backend_wall - router_wall,
        "verify_wall_s": r2 - r1, "inspection": inspected,
        "invariant_violations": violated,
        # Recorded for the audit trail only; status and ranking never read it.
        "router_facts": facts if isinstance(facts, dict) else {},
    }


def _floor_label(setup: dict | None) -> tuple[dict | None, bool, list[str]]:
    """The input floor as reported, and whether it labels the rung ``input_fail_floor``:
    anything other than a clean PASS grading, or copper text the router may still move."""
    floor = (setup or {}).get("input_floor")
    if not isinstance(floor, dict):
        return None, False, []
    reasons = []
    status = floor.get("grading_status", floor.get("verdict"))
    if status != "PASS":
        reasons.append(f"input grades {status}")
    if floor.get("error_count"):
        reasons.append(f"{floor['error_count']} error-level violation(s) before routing")
    if floor.get("unconnected_nets"):
        reasons.append(f"{floor['unconnected_nets']} net(s) unconnected before routing")
    if floor.get("unhandled"):
        reasons.append("copper text the router may relocate remains unhandled")
    if floor.get("router_text_check_ok") is False and not floor.get("unhandled"):
        reasons.append("router text check is not ok")
    return floor, bool(reasons), reasons


def _changed(before: dict, after: dict) -> list[str]:
    """Names whose fingerprint differs. A ``{"kind": "superset", "items": [...]}`` value
    means "everything in the input must still be there"; additions are the router's."""
    out = []
    for key in set(before) | set(after):
        b, a = before.get(key), after.get(key)
        if isinstance(b, dict) and b.get("kind") == "superset":
            have = set(a.get("items", [])) if isinstance(a, dict) and a.get("kind") == "superset" else set()
            if not set(b.get("items", [])) <= have:
                out.append(key)
        elif b != a:
            out.append(key)
    return sorted(out)


# --- the run ----------------------------------------------------------------------------


def _unofficial_reasons(
    repo: dict, plan_len: int, budget_s: float, attempt_cap: float,
    plan: list[dict] | None = None, committed_plan: list[dict] | None = None,
    protocol_given: bool = True, protocol_matches: bool = True, invariants_given: bool = True,
    version_matches: bool = True, seam_reasons: list[str] | None = None,
) -> list[str]:
    reasons = list(seam_reasons or [])
    if not protocol_given:
        reasons.append("protocol was not supplied by the caller")
    elif not protocol_matches:
        reasons.append("the protocol supplied differs from the committed protocol file")
    if not version_matches:
        reasons.append("protocol_version label differs from the committed protocol file")
    if not invariants_given:
        reasons.append("no invariants check was supplied: placement, nets and setup were not compared")
    if committed_plan is None or plan != committed_plan:
        reasons.append("plan differs from the committed plan in the protocol file for this backend")
    if repo.get("dirty", True):
        reasons.append("repository tree is dirty or its commit is unknown")
    if plan_len != EXPECTED_ATTEMPTS:
        reasons.append(f"plan has {plan_len} attempts, protocol expects {EXPECTED_ATTEMPTS}")
    if budget_s != DEFAULT_BUDGET_S:
        reasons.append(f"budget {budget_s} s differs from protocol {DEFAULT_BUDGET_S} s")
    if attempt_cap != DEFAULT_ATTEMPT_TIMEOUT_S:
        reasons.append(
            f"attempt cap {attempt_cap} s differs from protocol {DEFAULT_ATTEMPT_TIMEOUT_S} s"
        )
    return reasons


def run_rung(
    *,
    rung: Any,
    backend_name: str,
    board: Path,
    rule_files: list[Path],
    plan: list[dict],
    backend: Backend,
    gate: Gate,
    baseline: dict,
    tool_versions: dict,
    runs_root: Path,
    budget_s: float = DEFAULT_BUDGET_S,
    attempt_timeout_s: float = DEFAULT_ATTEMPT_TIMEOUT_S,
    repo_state: Callable[[], dict] | None = None,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
    protocol_version: str = PROTOCOL_VERSION,
    protocol: dict | None = None,
    extra_unofficial_reasons: list[str] | None = None,
    setup: dict | None = None,
    invariants: Callable[[Path], dict] | None = None,
    preflight: Callable[[Path], str | None] | None = None,
    repo_dir: Path | None = None,
) -> dict:
    """Run one batch and return its report (also written to ``report.json``).

    ``invariants(board) -> {name: fingerprint}`` is computed on the input before the
    manifest and on every attempt's output; an attempt whose fingerprints differ (the
    router moved a footprint, rewrote the net table or the board setup) cannot pass.
    ``protocol`` is the loaded protocol file; official needs it supplied and equal to the
    committed file, and the plan is compared with the COMMITTED table for ``backend_name``,
    never the injected one. ``preflight(board)`` returns a reason to refuse the input
    before anything is frozen: such a batch is REFUSED, unscored and never pooled. ``setup`` is
    recorded verbatim in the manifest (input preparation and input floor, run once
    before the baseline is pinned, never inside an attempt).
    """
    clock_injected = clock is not None
    clock = clock or time.monotonic
    seam_reasons: list[str] = []
    if SEAMS_ENFORCED:
        from kicad_mcp import rung_backends  # late: rung_backends imports this module

        for name, injected in (("repo_state", repo_state is not None), ("clock", clock_injected),
                               ("now", now is not None)):
            if injected:
                seam_reasons.append(f"seam injected: {name}")
        if preflight is None:
            seam_reasons.append("seam: preflight not supplied, the input was not checked for refusal")
        elif preflight is not rung_backends.input_refusal:
            seam_reasons.append("seam injected: preflight")
    committed = load_protocol()
    protocol_given = protocol is not None
    protocol_matches = protocol_given and protocol == committed
    protocol_sha = protocol_sha256() if (protocol_matches or not protocol_given) else _sha256_json(protocol)
    committed_plan = (committed.get("backends", {}).get(backend_name) or {}).get("plan")
    board, rule_files, runs_root = Path(board), [Path(p) for p in rule_files], Path(runs_root)
    started_at = (now or (lambda: datetime.now(UTC)))()
    if preflight is not None:
        try:
            refusal = preflight(board)
        except Exception as exc:  # noqa: BLE001 - an input that cannot be inspected is refused
            refusal = f"input could not be inspected: {type(exc).__name__}: {exc}"
        if refusal:
            return _run_not_scored(
                "refused", rung=rung, backend_name=backend_name, runs_root=runs_root,
                reason=str(refusal), extra={"setup": setup or {}}, plan=plan, now=now,
                protocol_version=protocol_version, protocol_sha=protocol_sha,
                board_sha256=_safe_sha(board),
            )
    # Repository state is captured before this runner creates a single file.
    repo = (repo_state or (lambda: _git_repo_state(repo_dir, runs_root)))()
    frozen_invariants = invariants(board) if invariants is not None else None
    run_dir = _new_run_dir(runs_root, rung, backend_name, started_at)

    frozen_plan = json.loads(json.dumps(plan))
    plan_sha = _sha256_json(frozen_plan)
    frozen_baseline = json.loads(json.dumps(baseline))
    baseline_sha = _sha256_json(frozen_baseline)

    # Frozen copy of the whole project directory; every attempt copies from it.
    frozen_dir = run_dir / "frozen"
    runs_resolved = runs_root.resolve()

    def _ignore(directory: str, names: list[str]) -> list[str]:
        return [n for n in names if (Path(directory) / n).resolve() == runs_resolved]

    shutil.copytree(board.parent, frozen_dir, ignore=_ignore)
    frozen_files = _tree_hashes(frozen_dir)
    watched = {p.name: p for p in [board, *rule_files]}
    input_hashes = {name: frozen_files.get(name) for name in watched}

    manifest = {
        "protocol_version": protocol_version,
        "rung": rung,
        "backend": backend_name,
        "created_utc": started_at.isoformat(),
        "input": {
            "board": board.name,
            "board_sha256": frozen_files[board.name],
            "rule_files": {p.name: frozen_files.get(p.name) for p in rule_files},
        },
        "frozen_files": frozen_files,
        "baseline": frozen_baseline,
        "baseline_sha256": baseline_sha,
        "protocol_sha256": protocol_sha,
        "setup": setup or {},
        "invariants": frozen_invariants,
        "tool_versions": tool_versions,
        "repo": repo,
        "plan": frozen_plan,
        "plan_sha256": plan_sha,
        "attempts_planned": len(frozen_plan),
        "budget_s": budget_s,
        "attempt_timeout_s": attempt_timeout_s,
    }
    unofficial_reasons = _unofficial_reasons(
        manifest["repo"], len(frozen_plan), budget_s, attempt_timeout_s, frozen_plan, committed_plan,
        protocol_given=protocol_given, protocol_matches=protocol_matches,
        invariants_given=invariants is not None,
        version_matches=protocol_version == committed["protocol_version"],
        seam_reasons=seam_reasons,
    ) + list(extra_unofficial_reasons or [])
    manifest["unofficial_reasons"] = unofficial_reasons  # lets an orphaned run dir be labelled
    manifest_path = run_dir / "manifest.json"
    _write_json(manifest_path, manifest)
    manifest_path.chmod(stat.S_IREAD)
    manifest_sha = _sha256_file(manifest_path)

    # Visible from here on, even if the process is killed before the report exists.
    _append_line(runs_root / "index.jsonl", {
        "kind": "started", "rung": rung, "backend": backend_name,
        "date": started_at.isoformat(), "protocol_version": protocol_version,
        "manifest_sha": manifest_sha, "run_dir": str(run_dir), "plan_sha256": plan_sha,
        "protocol_sha256": protocol_sha,
        "board_sha256": frozen_files[board.name], "planned": len(frozen_plan),
        "input_fail_floor": _floor_label(setup)[1],
        "unofficial": bool(unofficial_reasons), "unofficial_reasons": unofficial_reasons,
    })

    reasons: list[str] = []
    executed: dict[int, dict] = {}

    def record(findings: list[str]) -> list[str]:
        fresh = [f for f in findings if f not in reasons]
        reasons.extend(fresh)
        return fresh

    def interventions() -> list[str]:
        """Mechanical comparison with the manifest: hashes, plan, baseline, log."""
        found = []
        for name, path in watched.items():
            try:
                changed = _sha256_file(path) != input_hashes[name]
            except OSError:
                changed = True
            if changed:
                found.append(f"input or rule file changed after the manifest: {name}")
        try:
            now_files = _tree_hashes(frozen_dir)
        except OSError:
            now_files = {}
        for rel in sorted(set(frozen_files) | set(now_files)):
            if frozen_files.get(rel) != now_files.get(rel):
                found.append(f"frozen copy changed after the manifest: {rel}")
        if _sha256_json(plan) != plan_sha:
            found.append("plan changed after the manifest")
        if _sha256_json(baseline) != baseline_sha:
            found.append("baseline changed after the manifest")
        try:
            if _sha256_file(manifest_path) != manifest_sha:
                found.append("manifest edited after it was frozen")
        except OSError:
            found.append("manifest missing after it was frozen")
        found.extend(_log_findings(run_dir, len(frozen_plan), executed))
        return found

    attempts: list[dict] = []
    t0 = clock()
    for index, entry in enumerate(frozen_plan):
        base = {
            "index": index,
            "lever": entry.get("lever"),
            "parameters": entry.get("parameters", {}),
        }
        remaining = budget_s - (clock() - t0)
        if remaining <= 0:
            attempts.append(
                {**base, "status": "NOT_RUN", "router_wall_s": None, "verify_wall_s": None,
                 "backend_wall_s": None, "adapter_overhead_s": None,
                 "gate": None, "cause": None, "detail": None, "human_interventions": [],
                 "invariant_violations": [], "router_facts": {},
                 "delivered_below_requested_count": 0, "delivered_below_requested_policy_hit": False,
                 "necked_count": 0, "necked_length_mm": 0.0, "status_label": "NOT_RUN",
                 "removed_copper": None,
                 "input_sha256": None, "output_board_sha256": None}
            )
            continue
        attempt_dir = run_dir / "attempts" / f"{index:02d}"
        attempt_dir.mkdir(parents=True)
        working = attempt_dir / "project"
        shutil.copytree(frozen_dir, working)
        working_board = working / board.name
        start_sha = _sha256_file(working_board)
        pre = []
        if start_sha != frozen_files[board.name]:
            pre.append(f"attempt {index} did not start from the frozen board")
        pre.extend(f for f in interventions() if f.startswith("frozen copy"))

        def inspect(working=working) -> list[str]:
            """After the backend, before grading: every manifest file except the board
            must be untouched in this attempt's copy, and no new rule files may appear."""
            found = []
            now_files = _tree_hashes(working)
            for rel, sha in frozen_files.items():
                if rel != board.name and now_files.get(rel) != sha:
                    found.append(f"attempt copy changed a project file: {rel}")
            for rel in now_files:
                if rel not in frozen_files and _gate_reads(rel):
                    found.append(f"attempt copy added a file the gate reads: {rel}")
            return found

        core = _attempt_core(
            backend=backend, gate=gate, board=working_board, params=base["parameters"],
            timeout_s=min(attempt_timeout_s, remaining), log=attempt_dir / "router.log",
            clock=clock, inspect=inspect,
            over_budget=lambda: budget_s - (clock() - t0) < 0,
            invariants=(
                (lambda working_board=working_board: _changed(
                    frozen_invariants, invariants(working_board)))
                if invariants is not None else None
            ),
        )
        _write_json(attempt_dir / "gate.json", core["gate"])
        executed[index] = {"lever": base["lever"], "parameters": base["parameters"]}
        _log_attempt(run_dir, {**base})
        fresh = record([*pre, *core["inspection"], *interventions()])
        attempts.append(
            {**base, "router_wall_s": core["router_wall_s"],
             "backend_wall_s": core["backend_wall_s"],
             "adapter_overhead_s": core["adapter_overhead_s"],
             "verify_wall_s": core["verify_wall_s"], "status": core["status"],
             "gate": core["gate"], "cause": core["cause"], "detail": core["detail"],
             "invariant_violations": core["invariant_violations"],
             "router_facts": core["router_facts"],
             "delivered_below_requested_count": core["delivered_below_requested_count"],
             "delivered_below_requested_policy_hit": core["delivered_below_requested_policy_hit"],
             "necked_count": core["necked_count"], "necked_length_mm": core["necked_length_mm"],
             "status_label": core["status_label"],
             "removed_copper": core["removed_copper"],
             "human_interventions": fresh,
             "input_sha256": start_sha, "output_board_sha256": _safe_sha(working_board)}
        )

    record(interventions())
    ran = [a for a in attempts if a["status"] != "NOT_RUN"]
    passed = sum(1 for a in ran if a["status"] == "PASS")
    passed_necked = sum(1 for a in ran if a["status"] == "PASS" and a.get("necked_count"))
    unofficial = bool(unofficial_reasons)
    floor, fail_floor, floor_reasons = _floor_label(setup)
    report = {
        "protocol_version": protocol_version,
        "protocol_sha256": protocol_sha,
        "manifest_sha": manifest_sha,
        "run_dir": str(run_dir),
        "rung": rung,
        "backend": backend_name,
        "input_board_sha256": manifest["input"]["board_sha256"],
        "plan_sha256": plan_sha,
        "tool_versions": tool_versions,
        "budget_s": budget_s,
        "total_wall_s": clock() - t0,
        "attempts_planned": len(frozen_plan),
        "attempts_run": len(ran),
        "attempts_passed": passed,
        "autonomous_pass": passed >= 1 and not reasons and not unofficial,
        # Protocol 6: true when every passing attempt carries forced necks, i.e. the pass is a
        # "PASS (necked)" and no attempt passed plain. A plain PASS stays plain.
        "attempts_passed_necked": passed_necked,
        "autonomous_pass_necked": passed >= 1 and passed_necked == passed and not reasons and not unofficial,
        "autonomous_pass_label": (None if not (passed >= 1 and not reasons and not unofficial) else
                                  NECKED_LABEL if passed_necked == passed else "PASS"),
        "assisted": bool(reasons),
        "assisted_reasons": reasons,
        "unofficial": unofficial,
        "unofficial_reasons": unofficial_reasons,
        "best_attempt": _best_attempt(ran),
        "setup": setup or {},
        "input_floor": floor,
        "input_fail_floor": fail_floor,
        "input_floor_reasons": floor_reasons,
        "attempts_delivered_below_requested": sum(
            1 for a in ran if a.get("delivered_below_requested_policy_hit")),
        "attempts_with_removed_copper": sum(1 for a in ran if a.get("removed_copper")),
        "attempts": attempts,
    }
    # Index first: a crash between the two leaves an index line and no report, which
    # is visible, never a report the index does not know about.
    _append_line(runs_root / "index.jsonl", _index_line(report, started_at, "batch"))
    _write_json(run_dir / "report.json", report)
    return report


# --- the append-only index ------------------------------------------------------------------


def _index_line(report: dict, when: datetime, kind: str) -> dict:
    return {
        "kind": kind,
        "rung": report["rung"],
        "backend": report["backend"],
        "date": when.isoformat(),
        "protocol_version": report["protocol_version"],
        "manifest_sha": report["manifest_sha"],
        "run_dir": report["run_dir"],
        "plan_sha256": report["plan_sha256"],
        "board_sha256": report["input_board_sha256"],
        "passed": report["attempts_passed"],
        "run": report["attempts_run"],
        "planned": report["attempts_planned"],
        "autonomous_pass": report["autonomous_pass"],
        "autonomous_pass_necked": report.get("autonomous_pass_necked", False),
        "passed_necked": report.get("attempts_passed_necked", 0),
        "assisted": report["assisted"],
        "unofficial": report["unofficial"],
        "unofficial_reasons": report.get("unofficial_reasons", []),
        "protocol_sha256": report.get("protocol_sha256"),
        "input_fail_floor": report.get("input_fail_floor", False),
        "delivered_below_requested_attempts": report.get("attempts_delivered_below_requested", 0),
        "removed_copper_attempts": report.get("attempts_with_removed_copper", 0),
        "crashed": False,
    }


def _run_not_scored(
    kind: str,
    *,
    rung: Any,
    backend_name: str,
    runs_root: Path,
    reason: str,
    plan: list[dict],
    extra: dict | None = None,
    now: Callable[[], datetime] | None = None,
    protocol_version: str = PROTOCOL_VERSION,
    protocol_sha: str | None = None,
    board_sha256: str | None = None,
) -> dict:
    """A batch that never started attempts: ``unavailable`` (tool missing) or ``refused``
    (setup refused the input). No attempt happened, so none is reported failed; it is never
    pooled across backends and never scored, so it cannot lock in or dilute the first
    scored batch of a real run."""
    runs_root = Path(runs_root)
    started_at = (now or (lambda: datetime.now(UTC)))()
    run_dir = _new_run_dir(runs_root, rung, backend_name, started_at)
    status = kind.upper()
    report = {
        "status": status,
        "protocol_version": protocol_version,
        "protocol_sha256": protocol_sha or protocol_sha256(),
        "rung": rung,
        "backend": backend_name,
        "run_dir": str(run_dir),
        "reason": reason,
        "attempts_planned": len(plan),
        "attempts_run": 0,
        "attempts_passed": 0,
        "autonomous_pass": False,
        "assisted": False,
        "unofficial": True,
        "attempts": [],
        "board_sha256": board_sha256,
        **(extra or {}),
    }
    _append_line(runs_root / "index.jsonl", {
        "kind": kind, "rung": rung, "backend": backend_name, "board_sha256": board_sha256,
        "date": started_at.isoformat(), "protocol_version": protocol_version,
        "protocol_sha256": report["protocol_sha256"],
        "run_dir": str(run_dir), "reason": reason, "planned": len(plan),
    })
    _write_json(run_dir / "report.json", report)
    return report


def record_setup_failed(
    *, rung: Any, backend_name: str, runs_root: Path, reason: str, board_sha256: str | None = None,
    protocol_version: str = PROTOCOL_VERSION, protocol_sha: str | None = None,
    now: Callable[[], datetime] | None = None,
) -> None:
    """Setup could not complete before any batch existed: one index line, no run directory."""
    _append_line(Path(runs_root) / "index.jsonl", {
        "kind": "setup_failed", "rung": rung, "backend": backend_name,
        "date": (now or (lambda: datetime.now(UTC)))().isoformat(),
        "protocol_version": protocol_version, "protocol_sha256": protocol_sha or protocol_sha256(),
        "reason": reason, "board_sha256": board_sha256,
    })


def run_unavailable(
    *,
    rung: Any,
    backend_name: str,
    runs_root: Path,
    reason: str,
    availability: dict,
    plan: list[dict],
    now: Callable[[], datetime] | None = None,
    protocol_version: str = PROTOCOL_VERSION,
) -> dict:
    """The backend's tool is missing: UNAVAILABLE for the whole batch."""
    return _run_not_scored(
        "unavailable", rung=rung, backend_name=backend_name, runs_root=runs_root, reason=reason,
        plan=plan, extra={"availability": availability}, now=now, protocol_version=protocol_version,
    )


def _norm(path: Any) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _parse_utc(stamp: Any) -> datetime | None:
    """An ISO stamp with a time zone, or None (missing, unparsable or naive)."""
    try:
        parsed = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _strictly_later(stamp: Any, anchor: Any) -> bool:
    """True only when both stamps are readable and ``stamp`` is strictly later in time.
    Anything unreadable is not orderable, which every caller treats conservatively."""
    a, b = _parse_utc(stamp), _parse_utc(anchor)
    return a is not None and b is not None and a > b


_EARLIEST = datetime.min.replace(tzinfo=UTC)


def _time_key(stamp: Any) -> tuple:
    """Sort key by time: unreadable stamps first (the conservative place), then by instant."""
    parsed = _parse_utc(stamp)
    return (0, _EARLIEST) if parsed is None else (1, parsed)


def _read_history() -> list[dict]:
    """The index rows of the committed history file, in event order. A history that cannot be
    read is an error, never an empty history: reading it as empty would reopen every official
    slot that was already used."""
    try:
        data = json.loads(Path(HISTORY_FILE).read_text(encoding="utf-8"))
        rows = data["rows"]
        if data["schema"] != 1 or not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ValueError("unsupported shape")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"ladder history {HISTORY_FILE} is unreadable: {exc}") from exc
    return rows


def _read_index(runs_root: Path) -> list[dict]:
    """The committed history first (those events came earlier), then the live index of the
    runs root. The position of a row is its place in this list."""
    return [*_read_history(), *_read_lines(Path(runs_root) / "index.jsonl")]


def rung_result(
    runs_root: Path, rung: Any, backend_name: str, protocol_version: str = PROTOCOL_VERSION,
    protocol_sha: str | None = None,
) -> dict:
    """The reported result for one rung and one backend on one protocol version.

    The first OFFICIAL scored batch is the result; every other batch is listed beside it
    so re-running until lucky is visible. "First" is by index position (the batch's first
    line in ``index.jsonl``), never by date, so a clock that steps back between two batches
    cannot report the later one. An orphan (manifest, no index line) has no position: it is
    ordered after the first indexed official batch only if its manifest date is strictly
    later in time; otherwise (or if either stamp is unreadable) the orphan, a crashed fail,
    is reported, and ``reported_chosen_by`` and the summary say so. A batch is scored if it ran at least one attempt
    or crashed (a ``started`` line with no ``batch`` line counts as a failed, crashed
    batch). An unofficial batch (dirty tree, injected seam, foreign runs root, unpinned
    rung, ...) is history: listed under ``unofficial_batches`` with its reasons, never the
    reported batch, never counted in ``batches_with_pass``, and it does not lock the slot.
    A crashed batch counts as official only when its ``started`` line said so. Only
    official, unassisted passes count as passes. A later unplanned re-run of a batch
    relabels that batch assisted.

    Refusals before the first scored batch are those whose index line comes before that
    batch's first index line (its ``started`` line). ``index.jsonl`` is append-only, so line
    order is the order of events; ``date`` stamps are reported, never compared, because two
    can share one clock tick or a clock can step back. Only an orphan has no index line: for
    it the refusals are ordered against its manifest's ``created_utc`` by time, ties and
    unreadable stamps counted as before; if the orphan displaced an indexed official batch,
    the refusals before that batch's first line count too. ``refusals_ordered_by`` says which.
    """
    lines = _read_index(runs_root)
    protocol_sha = protocol_sha or protocol_sha256()

    def same_rung(line: dict) -> bool:
        return (line["rung"] == rung and line["backend"] == backend_name
                and line["protocol_version"] == protocol_version)

    def mine(line: dict) -> bool:
        # Pooled by the protocol file's content, not only its version label.
        return same_rung(line) and line.get("protocol_sha256") == protocol_sha

    different_sha = {l["run_dir"] for l in lines
                     if same_rung(l) and l["kind"] in ("batch", "started")
                     and l.get("protocol_sha256") != protocol_sha}

    reran = {l["run_dir"] for l in lines if l["kind"] == "rerun" and mine(l)}
    unavailable = [l for l in lines if l["kind"] == "unavailable" and mine(l)]
    refused_at = [(pos, l) for pos, l in enumerate(lines) if l["kind"] == "refused" and mine(l)]
    refused = [l for _, l in refused_at]
    setup_failed = [l for l in lines if l["kind"] == "setup_failed" and mine(l)]
    batches: dict[str, dict] = {}
    first_line: dict[str, int] = {}  # run_dir -> position of the batch's first index line
    for pos, line in enumerate(lines):
        if not mine(line) or line["kind"] in ("rerun", "unavailable", "refused", "setup_failed"):
            continue
        key = line["run_dir"]
        first_line.setdefault(key, pos)
        if line["kind"] == "started":
            batches[key] = {
                **line, "passed": 0, "run": 0, "autonomous_pass": False, "assisted": False,
                "unofficial": bool(line.get("unofficial", True)), "crashed": True,
            }
        else:
            batches[key] = {**batches.get(key, {}), **line, "crashed": False}
    # Run directories with a manifest but no index line (index lost, or the process
    # died between the manifest freeze and the started line) are orphans: crashed.
    indexed = {_norm(k) for k in batches}
    runs_dir = Path(runs_root) / str(rung) / backend_name
    for manifest_path in sorted(runs_dir.glob("*/manifest.json")):
        run_dir = manifest_path.parent
        if _norm(run_dir) in indexed:
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            manifest = {}
        if manifest.get("protocol_version", protocol_version) != protocol_version:
            continue
        if manifest.get("protocol_sha256") != protocol_sha:
            different_sha.add(str(run_dir))
            continue
        batches[str(run_dir)] = {
            "kind": "orphan", "rung": rung, "backend": backend_name,
            "date": manifest.get("created_utc", ""), "protocol_version": protocol_version,
            "manifest_sha": _safe_sha(manifest_path), "run_dir": str(run_dir),
            "plan_sha256": manifest.get("plan_sha256"),
            "board_sha256": manifest.get("input", {}).get("board_sha256"),
            "passed": 0, "run": 0, "planned": manifest.get("attempts_planned"),
            "autonomous_pass": False, "assisted": False,
            "unofficial": bool(manifest.get("unofficial_reasons", True)),
            "unofficial_reasons": manifest.get("unofficial_reasons", []),
            "crashed": True, "orphan": True,
        }
    reran_norm = {_norm(r) for r in reran}
    ordered = []
    # Indexed batches in index order (their first line); orphans have no position and
    # follow, by manifest time (unreadable stamps first). Dates never order two indexed
    # batches.
    for _key, b in sorted(batches.items(), key=lambda kv: (
            kv[0] not in first_line, first_line.get(kv[0], 0), _time_key(kv[1].get("date")), kv[0])):
        if _norm(b["run_dir"]) in reran_norm:
            b = {**b, "assisted": True, "autonomous_pass": False}
        ordered.append(b)

    scored = [b for b in ordered if b["crashed"] or b["run"] >= 1]
    official = [b for b in scored if not b["unofficial"]]
    unofficial_batches = [
        {**b, "unofficial_reasons": b.get("unofficial_reasons") or ["unofficial (reasons not recorded)"]}
        for b in scored if b["unofficial"]
    ]
    first_indexed = next((b for b in official if not b.get("orphan")), None)
    official_orphans = [b for b in official if b.get("orphan")]
    if first_indexed is None:
        reported = official_orphans[0] if official_orphans else None
        reported_chosen_by = "orphan_only" if reported else "no_official_batch"
    else:
        # An orphan is placed after the first indexed official batch only when its manifest
        # date is strictly later. Otherwise (same or earlier stamp, a clock that may have
        # stepped back, an unreadable stamp) nothing can order the two, and the orphan, a
        # crashed fail, is reported: the conservative reading.
        # The anchor is the first indexed OFFICIAL batch; an unofficial batch never is.
        # Orphans are sorted by time with unreadable stamps first, so if any orphan cannot
        # be ordered after the anchor, the earliest one cannot: it is the one reported.
        anchor = first_indexed.get("date")
        earliest = official_orphans[0] if official_orphans else None
        if earliest is not None and not _strictly_later(earliest.get("date"), anchor):
            reported = earliest
            reported_chosen_by = ("anchor_unreadable" if _parse_utc(anchor) is None
                                  else "orphan_not_orderable")
        else:
            reported, reported_chosen_by = first_indexed, "index_position"
    others = [b for b in ordered if b is not reported]
    with_pass = sum(1 for b in official if b["autonomous_pass"])
    # History under another protocol version or content: shown, never pooled with this one.
    other_protocol = [
        l for l in lines
        if l["rung"] == rung and l["backend"] == backend_name and l["kind"] == "batch"
        and l.get("protocol_sha256") != protocol_sha
    ]
    if official:
        with_necked = sum(1 for b in official if b["autonomous_pass"] and b.get("autonomous_pass_necked"))
        summary = f"passed in {with_pass} of {len(official)} official batches"
        if with_necked:
            summary += (f" ({with_necked} of them only as {NECKED_LABEL}: forced necks at pad entries within the "
                        "protocol's length bound, counted, not a plain PASS)")
    elif unofficial_batches:
        parts = [f"{b['passed']}/{b['planned']}{', input_fail_floor' if b.get('input_fail_floor') else ''} "
                 f"({'; '.join(b['unofficial_reasons'])})" for b in unofficial_batches]
        summary = ("no official batch yet; unofficial batches (never counted): " + " | ".join(parts))
    else:
        summary = "no official batch yet"
    if not scored and unavailable:
        summary = f"UNAVAILABLE: {unavailable[-1]['reason']} (no attempt ran; not a failed attempt)"
    elif not scored and refused:
        summary = f"REFUSED: {refused[-1]['reason']} (setup refused the input; not scored, no attempt ran)"
    if reported:
        labels = [name for name, flag in (
            ("assisted", reported["assisted"]),
            ("unofficial", reported["unofficial"] and not reported["crashed"]),
            ("crashed", reported["crashed"]),
            ("orphan", reported.get("orphan", False)),
        ) if flag]
        summary += f"; reported batch: {reported['passed']}/{reported['planned']}"
        if reported.get("passed_necked"):
            summary += f", {reported['passed_necked']} of those {NECKED_LABEL}"
            if reported.get("autonomous_pass_necked"):
                summary += f" (every pass is necked: the batch result is {NECKED_LABEL}, not a plain PASS)"
        if reported.get("input_fail_floor"):
            summary += ", input_fail_floor"
        if reported.get("delivered_below_requested_attempts"):
            summary += (f", {reported['delivered_below_requested_attempts']} attempt(s) delivered below "
                        "requested size (graded, never counted as passes)")
        if reported.get("removed_copper_attempts"):
            summary += f", {reported['removed_copper_attempts']} attempt(s) removed existing copper"
        summary += "".join(f", {l}" for l in labels)
        if reported_chosen_by == "orphan_not_orderable":
            summary += ("; the orphan cannot be ordered against the first indexed official batch "
                        "(its manifest date is not strictly later), so it is reported "
                        "conservatively as a crashed fail")
        elif reported_chosen_by == "anchor_unreadable":
            summary += ("; the first indexed official batch's date is unreadable, so no orphan can "
                        "be ordered against it; the orphan is reported conservatively as a "
                        "crashed fail")
    if official:
        status = "SCORED"
    elif scored:
        status = "NO_OFFICIAL_BATCH"
    else:
        status = "UNAVAILABLE" if unavailable else ("REFUSED" if refused else "NOT_RUN")
    if official and unofficial_batches:
        summary += f"; {len(unofficial_batches)} unofficial batch(es) beside it, not counted"
    if reported is None:
        refusals_ordered_by, before = "no_scored_batch", refused
    elif reported["run_dir"] in first_line:
        refusals_ordered_by = "index_position"
        cut = first_line[reported["run_dir"]]
        before = [r for pos, r in refused_at if pos < cut]
    else:
        # An orphan has no index line, hence no position; its manifest stamp is the only
        # anchor left. A tie or an unreadable stamp is ambiguous, so it is shown as before
        # rather than hidden. When the orphan displaced an indexed official batch, every
        # refusal before that batch's first line was before a scored batch too: the union.
        orphan_date = reported.get("date")
        if first_indexed is not None:
            refusals_ordered_by = "index_position_or_date_orphan"
            cut = first_line[first_indexed["run_dir"]]
        else:
            refusals_ordered_by, cut = "date_orphan_fallback", -1
        before = [r for pos, r in refused_at
                  if pos < cut or not _strictly_later(r.get("date"), orphan_date)]
    if before:
        summary += f"; {len(before)} refusal(s) before the first scored batch"
        if refusals_ordered_by == "date_orphan_fallback":
            summary += " (ordered by date: the reported batch is an orphan with no index line)"
        elif refusals_ordered_by == "index_position_or_date_orphan":
            summary += (" (by index position before the first indexed official batch, or by date "
                        "against the reported orphan, which has no index line)")
    if setup_failed:
        summary += f"; {len(setup_failed)} setup failure(s)"
    return {
        "status": status,
        "unavailable_batches": unavailable,
        "refused_batches": refused,
        "refusals_before_first_scored_batch": len(before),
        "refusals_ordered_by": refusals_ordered_by,
        "setup_failed_batches": setup_failed,
        "different_protocol_sha_batches": len(different_sha),
        "rung": rung,
        "backend": backend_name,
        "protocol_version": protocol_version,
        "reported": reported,
        "reported_chosen_by": reported_chosen_by,
        "other_batches": others,
        "batches": len(ordered),
        "batches_with_pass": with_pass,
        "unofficial_batches": unofficial_batches,
        "other_protocol_batches": other_protocol,
        "summary": summary,
    }


def rung_results(
    runs_root: Path, rung: Any, protocol_version: str = PROTOCOL_VERSION
) -> dict[str, dict]:
    """One result per backend. Backends are never pooled into a single figure."""
    backends = {l["backend"] for l in _read_index(runs_root) if l["rung"] == rung}
    rung_dir = Path(runs_root) / str(rung)
    if rung_dir.is_dir():
        backends |= {d.name for d in rung_dir.iterdir() if d.is_dir()}
    backends = sorted(backends)
    return {b: rung_result(runs_root, rung, b, protocol_version) for b in backends}


def rerun_attempt(
    run_dir: Path | str,
    index: int,
    *,
    backend: Backend,
    gate: Gate,
    attempt_timeout_s: float = DEFAULT_ATTEMPT_TIMEOUT_S,
    clock: Callable[[], float] | None = None,
    now: Callable[[], datetime] | None = None,
) -> dict:
    """Re-run one attempt after the batch finished. Allowed, but the batch becomes
    "assisted": the attempt is not in the plan a second time. The scored counts are
    untouched and the re-run is listed beside them."""
    clock = clock or time.monotonic
    run_dir = Path(run_dir)
    manifest_path = run_dir / "manifest.json"
    report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["plan"][index]
    findings = [f"unplanned attempt re-run (index {index})"]
    if _sha256_file(manifest_path) != report["manifest_sha"]:
        findings.append("manifest edited after it was frozen")

    frozen_now = _tree_hashes(run_dir / "frozen")
    for rel in sorted(set(manifest["frozen_files"]) | set(frozen_now)):
        if manifest["frozen_files"].get(rel) != frozen_now.get(rel):
            findings.append(f"frozen copy changed after the manifest: {rel}")

    n = len(report.get("unplanned_reruns", []))
    attempt_dir = run_dir / "attempts" / f"rerun-{n:02d}-{index:02d}"
    attempt_dir.mkdir(parents=True)
    working = attempt_dir / "project"
    shutil.copytree(run_dir / "frozen", working)
    core = _attempt_core(
        backend=backend, gate=gate, board=working / manifest["input"]["board"],
        params=entry.get("parameters", {}), timeout_s=attempt_timeout_s,
        log=attempt_dir / "router.log", clock=clock,
    )
    _write_json(attempt_dir / "gate.json", core["gate"])
    _log_attempt(run_dir, {"index": index, "lever": entry.get("lever"),
                           "parameters": entry.get("parameters", {}), "rerun": True})
    report.setdefault("unplanned_reruns", []).append(
        {"index": index, "lever": entry.get("lever"), "parameters": entry.get("parameters", {}),
         "router_wall_s": core["router_wall_s"], "verify_wall_s": core["verify_wall_s"],
         "status": core["status"], "gate": core["gate"], "cause": core["cause"],
         "detail": core["detail"], "invariant_violations": core["invariant_violations"],
         "router_facts": core["router_facts"],
         "delivered_below_requested_count": core["delivered_below_requested_count"],
         "delivered_below_requested_policy_hit": core["delivered_below_requested_policy_hit"],
         "necked_count": core["necked_count"], "necked_length_mm": core["necked_length_mm"],
         "status_label": core["status_label"],
         "removed_copper": core["removed_copper"]})
    for finding in [*findings, *core["inspection"]]:
        if finding not in report["assisted_reasons"]:
            report["assisted_reasons"].append(finding)
    report["assisted"] = True
    report["autonomous_pass"] = False
    report["autonomous_pass_necked"] = False
    report["autonomous_pass_label"] = None
    # Index line first, then the report (see run_rung).
    _append_line(run_dir.parents[2] / "index.jsonl",
                 _index_line(report, (now or (lambda: datetime.now(UTC)))(), "rerun"))
    _write_json(run_dir / "report.json", report)
    return report
