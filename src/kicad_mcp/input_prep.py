"""Input preparation: move copper-layer text to silkscreen once, before routing.

Contract: build ticket 2
and docs/ARCHITECTURE.md ("Text layers", measured 2026-10-11): KiCadRoutingTools'
writers silently move every copper-layer ``gr_text`` to silkscreen. An add-only
segment/via delta cannot carry that change, so tracks end up routed through
texts left on copper, and the resulting errors get blamed on the router. This
module does that move deterministically, once, through ``kicad_tools`` parsed
nodes (never raw text), onto a NEW board revision, then re-pins the rule
baseline and reports the input floor (the error-level findings that exist
before any routing).

Scope is deliberately narrow: only board-level ``gr_text`` on ``F.Cu`` and
``B.Cu`` is moved (``F.SilkS`` / ``B.SilkS``). Copper items whose router
behaviour is unmeasured (inner-layer text, ``gr_text_box``) are never touched
and never guessed at: they are listed under ``unhandled`` and keep
``check_router_text_moves`` red.

Every position/layer fact is read from the raw S-expression (``parse_file``),
never from ``GraphicText.layer`` / ``.position`` convenience properties
(CLAUDE.md hard rule 2).

Flow (dry-run plus revision-token checkpoint, docs/ARCHITECTURE.md decision 4):
``propose_input_preparation`` writes nothing and returns the planned moves and
a token; ``confirm_input_preparation`` builds the revision in a temporary
sibling directory, reloads it fresh from disk, verifies, renames it into place
and only then re-pins and grades it. A failed attempt leaves nothing behind and
the token stays usable.
"""

from __future__ import annotations

import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

from kicad_mcp import checkpoint, write_layer
from kicad_mcp import verify_board as gate

# Only these two have a measured, side-preserving silkscreen target.
_SILK_FOR = {"F.Cu": "F.SilkS", "B.Cu": "B.SilkS"}


_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())  # token -> {"out_dir": ...}

_DRIFT = "the board changed since the proposal was made: re-run propose_input_preparation"

for _name in ("_directory_size", "_MAX_COPY_BYTES"):  # private gate helpers reused on purpose
    if not hasattr(gate, _name):
        raise ImportError(
            f"input_prep reuses kicad_mcp.verify_board.{_name} for the copy size cap, which no longer exists"
        )


def silk_layer_for(copper_layer: str) -> str | None:
    """The silkscreen layer a copper-layer text moves to, or None when it is not handled."""
    return _SILK_FOR.get(copper_layer)


def _is_copper(layer: str) -> bool:
    return layer.endswith(".Cu")


def _norm(path: str | Path) -> str:
    """Comparable spelling of a path: absolute, resolved, case-normalised on Windows."""
    return os.path.normcase(str(Path(path).resolve()))


def _node_layer(node: Any) -> str:
    layer = node.find("layer")
    if layer is None or not layer.values:
        return ""
    return str(layer.values[0])


def _node_text(node: Any) -> str:
    return str(node.values[0]) if node.values else ""


def _node_uuid(node: Any) -> str:
    uuid = node.find("uuid")
    return str(uuid.values[0]) if uuid is not None and uuid.values else ""


def _node_at(node: Any) -> list[float]:
    at = node.find("at") or node.find("start")
    return [float(v) for v in at.values[:2]] if at is not None else []


def _top_level(root: Any, tag: str) -> list[Any]:
    # Direct children only: footprint-owned text is not board-level text.
    return [child for child in root.children if child.name == tag]


def _scan(root: Any) -> tuple[list[dict], list[dict]]:
    """``(movable, unhandled)`` copper items of a parsed board, from raw nodes."""
    movable: list[dict] = []
    for ordinal, node in enumerate(_top_level(root, "gr_text")):
        layer = _node_layer(node)
        if layer in _SILK_FOR:
            movable.append(
                {
                    "ordinal": ordinal,
                    "text": _node_text(node),
                    "uuid": _node_uuid(node),
                    "at": _node_at(node),
                    "layer_before": layer,
                    "layer_after": _SILK_FOR[layer],
                }
            )
    unhandled: list[dict] = []
    for kind in ("gr_text", "gr_text_box"):
        for node in _top_level(root, kind):
            layer = _node_layer(node)
            if not _is_copper(layer):
                continue
            if kind == "gr_text" and layer in _SILK_FOR:
                continue
            reason = (
                "text box on a copper layer: KRT behaviour for gr_text_box is unmeasured"
                if kind == "gr_text_box"
                else "inner-layer copper text: KRT behaviour for inner layers is unmeasured"
            )
            unhandled.append(
                {
                    "kind": kind,
                    "text": _node_text(node),
                    "uuid": _node_uuid(node),
                    "at": _node_at(node),
                    "layer": layer,
                    "reason": reason,
                }
            )
    return movable, unhandled


def _parse(pcb_path: str | Path) -> Any:
    from kicad_tools.sexp import parse_file

    return parse_file(str(pcb_path))


def copper_texts(pcb_path: str | Path) -> list[dict]:
    """Every board-level ``gr_text`` on F.Cu / B.Cu that would be moved, from the raw S-expression."""
    return _scan(_parse(pcb_path))[0]


def unhandled_copper_items(pcb_path: str | Path) -> list[dict]:
    """Copper-layer text items this module will not move (inner layers, text boxes)."""
    return _scan(_parse(pcb_path))[1]


def check_router_text_moves(pcb_path: str | Path) -> dict:
    """The "router moves text" check. ``ok`` is False while any copper-layer text
    remains that KRT's writers could relocate: movable ones (``copper_texts``) and
    ones we do not handle (``unhandled``), whose router behaviour is unmeasured."""
    movable, unhandled = _scan(_parse(pcb_path))
    return {"ok": not movable and not unhandled, "copper_texts": movable, "unhandled": unhandled}


def _tag_counts(root: Any) -> Counter:
    return Counter(child.name for child in root.children if child.name)


def _default_output_dir(pcb_path: Path) -> Path:
    resolved = pcb_path.resolve()
    return resolved.parent.parent / f"{resolved.parent.name}_prepared"


def _output_dir_problem(src: Path, out_dir: Path) -> str | None:
    return checkpoint.output_dir_problem(src, out_dir, what="the prepared board")


def propose_input_preparation(
    pcb_path: str | Path, output_dir: str | Path | None = None
) -> dict:
    """Dry run. Lists every copper-layer text that would move (and every copper
    text item left unhandled); writes nothing. With nothing to move there is no
    token and nothing to do (idempotence)."""
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    try:
        moves, unhandled = _scan(_parse(src))
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"failed to parse board: {exc}"}
    if not moves:
        result = {
            "status": "nothing_to_do",
            "moves": [],
            "unhandled": unhandled,
            "revision_token": None,
            "router_text_check_ok": not unhandled,
        }
        if unhandled:
            result["note"] = (
                "copper text the router may move remains unhandled; floor is not a clean baseline"
            )
        return result

    out_dir = Path(output_dir) if output_dir else _default_output_dir(src)
    problem = _output_dir_problem(src, out_dir)
    if problem:
        return {"status": "error", "reason": problem}

    try:
        token = _PENDING.issue(src, {"out_dir": str(out_dir)})
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        "moves": moves,
        "unhandled": unhandled,
        "output_pcb": str(out_dir / src.name),
        "revision_token": token,
        "note": (
            "creates a new board revision; the original is untouched. Call "
            "confirm_input_preparation with this revision_token to apply."
        ),
    }


def _copy_project(src_pcb: Path, dest: Path) -> Path:
    """Copy the project directory (same exclusions as the gate's graded copy, plus
    earlier ``*_prepared`` outputs) with the gate's size cap."""
    return checkpoint.copy_project(
        src_pcb, dest, outputs=(f"{src_pcb.parent.name}_prepared",), max_bytes=gate._MAX_COPY_BYTES
    )


def _write(src: Path, tmp: Path) -> dict:
    """Copy the project into ``tmp``, move every copper-layer text to its silkscreen layer and
    save through the write layer. Returns what ``_verify`` compares against."""
    from kicad_tools.schema import PCB

    tmp_pcb = _copy_project(src, tmp)
    board = PCB.load(tmp_pcb)
    nodes = _top_level(board._sexp, "gr_text")
    before_counts = _tag_counts(board._sexp)
    _, unhandled_before = _scan(board._sexp)
    expected: list[tuple[str, str]] = []  # per gr_text ordinal: (text, layer after)
    moved: list[dict] = []
    for ordinal, node in enumerate(nodes):
        layer = _node_layer(node)
        target = _SILK_FOR.get(layer)
        if target is not None:
            node.find("layer").set_value(0, target)
            moved.append(
                {
                    "ordinal": ordinal,
                    "text": _node_text(node),
                    "uuid": _node_uuid(node),
                    "at": _node_at(node),
                    "layer_before": layer,
                    "layer_after": target,
                }
            )
        expected.append((_node_text(node), target or layer))
    write_layer.save_pcb(board, tmp_pcb)
    return {
        "pcb": tmp_pcb,
        "before_counts": before_counts,
        "unhandled_before": unhandled_before,
        "expected": expected,
        "moved": moved,
    }


def _verify(tmp: Path, state: dict) -> dict:
    """Non-negotiable: verify from a fresh parse of the saved file, never the in-memory tree."""
    tmp_pcb: Path = state["pcb"]
    name = tmp_pcb.name
    try:
        fresh = _parse(tmp_pcb)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: prepared file failed to re-parse: {exc}",
            [checkpoint.mismatch(name, "board", "a board that parses", str(exc))],
        ) from exc
    movable_after, unhandled_after = _scan(fresh)
    if movable_after:
        raise checkpoint.NotVerified(
            f"CRITICAL: {len(movable_after)} copper-layer text(s) remain after save+reload",
            [
                checkpoint.mismatch(name, f"gr_text #{item['ordinal']} {item['text']!r}", item["layer_after"], item["layer_before"])
                for item in movable_after
            ],
        )
    before_counts = state["before_counts"]
    after_counts = _tag_counts(fresh)
    if after_counts != before_counts:
        raise checkpoint.NotVerified(
            "CRITICAL: top-level node counts changed during preparation",
            [
                checkpoint.mismatch(name, f"top-level {tag} count", before_counts.get(tag, 0), after_counts.get(tag, 0))
                for tag in sorted(set(before_counts) | set(after_counts))
                if before_counts.get(tag, 0) != after_counts.get(tag, 0)
            ],
            before=dict(before_counts),
            after=dict(after_counts),
        )
    expected = state["expected"]
    actual = [(_node_text(n), _node_layer(n)) for n in _top_level(fresh, "gr_text")]
    if actual != expected:
        raise checkpoint.NotVerified(
            "CRITICAL: a text is not on the expected layer after reload (compared by position)",
            [
                checkpoint.mismatch(name, f"gr_text #{ordinal}", list(want), list(got))
                for ordinal, (want, got) in enumerate(zip(expected, actual, strict=False))
                if want != got
            ]
            or [checkpoint.mismatch(name, "gr_text count", len(expected), len(actual))],
        )
    if unhandled_after != state["unhandled_before"]:
        raise checkpoint.NotVerified(
            "CRITICAL: unhandled copper items changed during preparation",
            [checkpoint.mismatch(name, "unhandled copper items", state["unhandled_before"], unhandled_after)],
        )
    return {"moved": state["moved"], "unhandled": unhandled_after}


def _build_revision(src: Path, out_dir: Path) -> dict:
    """Build, verify and move a prepared revision into ``out_dir`` (the checkpoint helper's
    temporary sibling, reload and compare, then the move). Returns a ``status: built`` dict, or
    a failure dict that leaves nothing behind."""
    return checkpoint.build_revision(
        out_dir,
        write=lambda tmp: _write(src, tmp),
        verify=_verify,
        success="built",
        before_verified="preparation failed before a revision was verified",
    )


def confirm_input_preparation(
    pcb_path: str | Path,
    revision_token: str,
    *,
    parity: str,
    parity_reason: str | None = None,
    drc: Any = None,
) -> dict:
    """Apply a proposal: build the prepared revision, verify it from a fresh
    reload, then re-pin the baseline and report the input floor.

    ``parity`` / ``parity_reason`` are passed to ``verify_board`` unchanged
    (the gate refuses to guess). ``drc`` replaces the kicad-cli boundary
    (tests only). The token is consumed only by a successful preparation or by
    detected source drift; every other failure leaves it usable.
    """
    from kicad_tools.schema import PCB

    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused

    out_dir = Path(entry["out_dir"])
    problem = _output_dir_problem(src, out_dir)
    if problem:
        return {"status": "error", "reason": problem}

    built = _build_revision(src, out_dir)
    if built["status"] != "built":
        return built
    _PENDING.spend(revision_token)
    out_pcb = out_dir / src.name
    moved = built["moved"]
    unhandled_after = built["unhandled"]

    try:
        baseline = gate.snapshot_rules(out_pcb, drc=drc)
        graded = gate.verify_board(
            out_pcb, parity=parity, parity_reason=parity_reason, baseline=baseline, drc=drc
        )
    except Exception as exc:  # noqa: BLE001  (e.g. kicad-cli missing): the revision exists, grading did not run
        return {
            "status": "incomplete",
            "reason": f"revision written and verified, but the baseline could not be pinned or graded: {exc}",
            "output_pcb": str(out_pcb),
            "moved": moved,
            "unhandled": unhandled_after,
            "router_text_check_ok": not unhandled_after,
        }
    return {
        "status": "prepared",
        "output_pcb": str(out_pcb),
        "moved": moved,
        "unhandled": unhandled_after,
        "baseline": baseline,
        "input_floor": {
            # Not a verdict: only a verify_board call through the server records one.
            "grading_status": graded["verdict"],
            "certified": False,
            "violations": graded["violations"],
            "unconnected": graded["unconnected"],
            "error_count": len(graded["violations"]),
            "unconnected_nets": len(graded["unconnected"]),
            "unstable": graded["unstable"],
            "list_complete": graded["list_complete"],
            "incomplete_reasons": graded["incomplete_reasons"],
            "notes": graded.get("notes", []),  # informational: never change the verdict
            "router_text_check_ok": not unhandled_after,
            "note": (
                "errors present BEFORE routing: never attribute these to the router, "
                "and never waive them to reach a PASS"
                + (
                    "; unhandled copper text items remain, so the router may still relocate them "
                    "and this floor is not a clean baseline"
                    if unhandled_after
                    else ""
                )
            ),
        },
    }
