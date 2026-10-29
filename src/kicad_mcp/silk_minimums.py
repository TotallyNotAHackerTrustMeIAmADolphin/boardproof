"""Silkscreen text at fab minimums: raise height and stroke, change nothing else.

Contract: build ticket 20.
Raises every silkscreen text below the declared minimum height / stroke width
(rung 1: 1.0 mm / 0.15 mm) onto a NEW board revision and lists every change.
It never moves, hides, unhides, re-layers or re-justifies text: overlaps the
larger text may create are for the acceptance gate to report.

Text kinds handled (all on ``F.SilkS`` / ``B.SilkS``): board-level ``gr_text`` and
``gr_text_box``, and footprint-owned ``property`` (Reference, Value, any other
field), ``fp_text`` and ``fp_text_box``. Hidden fields stay hidden; they are raised
too (so unhiding later cannot create a violation) and flagged ``hidden`` in the
report. ``(justify mirror)`` on B.SilkS is never touched.

KiCad facts (measured with kicad-cli, 2026-10-11): ``(size H W)`` -- the first value
is what ``text_height`` compares; the stroke that DRC grades is clamped to a quarter
of the narrower dimension, so a text 0.5 mm wide with ``(thickness 0.15)`` is graded
at 0.125 mm. A text is therefore raised when its height is below the minimum OR its
effective stroke is. Size is scaled uniformly (aspect ratio kept) by the smallest
factor that satisfies both; the stroke is then set to the minimum if lower.

Every value is read from the raw S-expression (``parse_file`` / ``PCB._sexp``
nodes), never from ``kicad_tools`` convenience properties (CLAUDE.md hard rule 2),
and written with ``node.set_value`` on parsed nodes (hard rule 1).

Reused, not duplicated: the raw-node readers from ``input_prep``; the revision
token, pending state, output-dir rules, project copy and the build-verify-move of
the new revision from the shared checkpoint helper (``kicad_mcp.checkpoint``).

Flow (dry-run plus revision-token checkpoint, docs/ARCHITECTURE.md decision 4):
``propose_silkscreen_minimums`` writes nothing and returns the planned changes and
a token; ``confirm_silkscreen_minimums`` builds the revision in a temporary sibling,
reloads it fresh from disk, verifies every text against the expectation, renames it
into place. A failed attempt leaves nothing behind and the token stays usable.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from pathlib import Path
from typing import Any

from kicad_mcp import checkpoint, write_layer
from kicad_mcp import input_prep as ip
from kicad_mcp import verify_board as gate

DEFAULT_MIN_HEIGHT_MM = 1.0
DEFAULT_MIN_THICKNESS_MM = 0.15

SILK_LAYERS = ("F.SilkS", "B.SilkS")
_BOARD_KINDS = ("gr_text", "gr_text_box")
_FOOTPRINT_KINDS = ("property", "fp_text", "fp_text_box")
_FOOTPRINT_NODES = ("footprint", "module")
_EPS = 1e-9
_LARGE_SCALE = 2.0

_REUSED_NAMES = (
    "_node_layer", "_node_text", "_node_uuid", "_node_at", "_parse", "_tag_counts",
)
for _name in _REUSED_NAMES:  # private helpers reused on purpose
    if not hasattr(ip, _name):
        raise ImportError(f"silk_minimums reuses kicad_mcp.input_prep.{_name}, which no longer exists")


def _fmt(value: float) -> float:
    """A value as kicad_tools serialises it (6 significant digits), so what is
    planned is exactly what a reload reads back."""
    return float(f"{value:.6g}")


_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())  # token -> {"out_dir", "min_h", "min_t"}

_DRIFT = "the board changed since the proposal was made: re-run propose_silkscreen_minimums"


# --- reading ---------------------------------------------------------------------


def _floats(node: Any) -> list[float] | None:
    if node is None:
        return None
    try:
        return [float(v) for v in node.values]
    except (TypeError, ValueError):
        return None


def _is_hidden(node: Any, effects: Any, content_values: int) -> bool:
    """Hidden flag as ``(hide yes)`` child or KiCad 6/7 bare ``hide`` atom. The leading
    ``content_values`` of the text node are its name/content (a text reading "hide"
    is not a hidden text), so only the atoms after them are checked."""
    for holder, skip in ((node, content_values), (effects, 0)):
        if holder is None:
            continue
        flag = holder.find("hide")
        if flag is not None and (not flag.values or str(flag.values[0]) in ("yes", "true")):
            return True
        if "hide" in [str(v) for v in holder.values[skip:]]:
            return True
    return False


def _owner_reference(fp: Any) -> str | None:
    for child in fp.children:
        if child.name == "property" and child.values and str(child.values[0]) == "Reference":
            return str(child.values[1]) if len(child.values) > 1 else ""
        if child.name == "fp_text" and child.values and str(child.values[0]) == "reference":
            return str(child.values[1]) if len(child.values) > 1 else ""
    return None


def _record(node: Any, kind: str, owner: str | None) -> tuple[dict, dict] | None:
    """``(record, handles)`` for a silkscreen text node, or None when it is not one."""
    layer = ip._node_layer(node)
    if layer not in SILK_LAYERS:
        return None
    effects = node.find("effects")
    font = effects.find("font") if effects is not None else None
    size = font.find("size") if font is not None else None
    thick = font.find("thickness") if font is not None else None
    size_values = _floats(size)
    thickness = _floats(thick)
    if kind == "property":
        field, text = str(node.values[0]), str(node.values[1]) if len(node.values) > 1 else ""
    elif kind == "fp_text":
        field = str(node.values[0]) if node.values else ""
        text = str(node.values[1]) if len(node.values) > 1 else ""
    else:
        field, text = kind, ip._node_text(node)
    justify = effects.find("justify") if effects is not None else None
    anchor = [str(v) for v in (justify.values if justify is not None else []) if str(v) != "mirror"]
    record = {
        "kind": kind,
        "owner": owner,
        "field": field,
        "text": text,
        "layer": layer,
        "uuid": ip._node_uuid(node),
        "at": ip._node_at(node),
        "hidden": _is_hidden(node, effects, 2 if kind in ("property", "fp_text") else 1),
        "mirrored": justify is not None and "mirror" in [str(v) for v in justify.values],
        "anchor_shift": ",".join(anchor) if anchor else None,
        "box_may_clip": kind in ("gr_text_box", "fp_text_box"),
        "render_cache": node.find("render_cache") is not None,
        "height": size_values[0] if size_values and len(size_values) >= 2 else None,
        "width": size_values[1] if size_values and len(size_values) >= 2 else None,
        "thickness": thickness[0] if thickness else None,
    }
    return record, {"node": node, "font": font, "size": size, "thickness": thick}


def _descendants(node: Any):
    for child in node.children:
        yield child
        yield from _descendants(child)


def _uncollected_silk_text(node: Any, owner: str | None) -> dict | None:
    """A node this module does not resize that nevertheless carries font data on a
    silkscreen layer (table, dimension, anything newer): listed, never silently missed."""
    parts = list(_descendants(node))
    layers = [str(n.values[0]) for n in (node, *parts) if n.name == "layer" and n.values]
    own = ip._node_layer(node)
    silk = [layer for layer in ([own] + layers) if layer in SILK_LAYERS]
    if not silk or not any(n.name == "font" for n in parts):
        return None
    text = ""
    for n in parts:
        if n.name in ("table_cell", "gr_text") and n.values:
            text = str(n.values[0])
            break
    else:
        text = ip._node_text(node) if node.values else ""
    return {
        "kind": node.name,
        "owner": owner,
        "field": node.name,
        "text": text,
        "layer": silk[0],
        "uuid": ip._node_uuid(node),
        "at": ip._node_at(node),
        "reason": (
            f"{node.name} carries text on a silkscreen layer and is not resized by this tool: "
            "check its font size and stroke by hand"
        ),
    }


def _collect_unhandled_kinds(root: Any) -> list[dict]:
    found: list[dict] = []
    for child in root.children:
        if child.name in _BOARD_KINDS:
            continue
        if child.name in _FOOTPRINT_NODES:
            owner = _owner_reference(child)
            for node in child.children:
                if node.name not in _FOOTPRINT_KINDS:
                    item = _uncollected_silk_text(node, owner)
                    if item:
                        found.append(item)
            continue
        item = _uncollected_silk_text(child, None)
        if item:
            found.append(item)
    return found


def _collect(root: Any) -> list[tuple[dict, dict]]:
    found: list[tuple[dict, dict]] = []
    for child in root.children:
        if child.name in _BOARD_KINDS:
            item = _record(child, child.name, None)
            if item:
                found.append(item)
        elif child.name in _FOOTPRINT_NODES:
            owner = _owner_reference(child)
            for node in child.children:
                if node.name in _FOOTPRINT_KINDS:
                    item = _record(node, node.name, owner)
                    if item:
                        found.append(item)
    return found


def scan_silk_texts(pcb_path: str | Path) -> list[dict]:
    """Every silkscreen text on the board with its raw size and stroke."""
    return [rec for rec, _ in _collect(ip._parse(pcb_path))]


# --- planning --------------------------------------------------------------------


def _effective_thickness(rec: dict) -> float:
    narrow = min(rec["height"], rec["width"])
    return min(rec["thickness"], narrow / 4.0)


def _plan(rec: dict, min_h: float, min_t: float) -> dict | None:
    """The after-state for a record, or None when it already meets the minimums."""
    h, w = rec["height"], rec["width"]
    below_height = h < min_h - _EPS
    below_stroke = _effective_thickness(rec) < min_t - _EPS
    if not (below_height or below_stroke):
        return None
    new_t = max(rec["thickness"], min_t)
    scale = max(1.0, min_h / h, 4.0 * new_t / min(h, w))
    return {
        "height": _fmt(h * scale),
        "width": _fmt(w * scale),
        "thickness": _fmt(new_t),
    }


def _unhandled_reason(rec: dict) -> str | None:
    if rec["height"] is None or rec["height"] <= 0 or rec["width"] <= 0:
        return "text size missing or not positive: not guessed"
    if rec["thickness"] is None or rec["thickness"] <= 0:
        return (
            "no explicit (thickness): KiCad takes the stroke from the board default text "
            "thickness, which this tool does not read; not assumed"
        )
    return None


def _change(rec: dict, index: int, after: dict) -> dict:
    scale = round(after["height"] / rec["height"], 6)
    return {
        **rec,
        "index": index,
        "before": {"height": rec["height"], "width": rec["width"], "thickness": rec["thickness"]},
        "after": after,
        "scale": scale,
        "large_scale": scale > _LARGE_SCALE + _EPS,
        "render_cache_removed": rec["render_cache"],
    }


def _summary(changes: list[dict]) -> dict:
    def names(flag: str) -> list[str]:
        return [c["text"] for c in changes if c[flag]]

    return {
        "changed": len(changes),
        "large_scale": names("large_scale"),
        "box_may_clip": names("box_may_clip"),
        "render_cache_removed": names("render_cache_removed"),
        "anchor_shift": [c["text"] for c in changes if c["anchor_shift"]],
    }


def _plan_all(root: Any, min_h: float, min_t: float) -> tuple[list[dict], list[dict]]:
    """``(changes, unhandled)`` for a parsed board, in scan order."""
    changes: list[dict] = []
    unhandled: list[dict] = []
    for index, (rec, _) in enumerate(_collect(root)):
        reason = _unhandled_reason(rec)
        if reason:
            unhandled.append({**rec, "index": index, "reason": reason})
            continue
        after = _plan(rec, min_h, min_t)
        if after is not None:
            changes.append(_change(rec, index, after))
    unhandled.extend(_collect_unhandled_kinds(root))
    return changes, unhandled


def _check_minimums(min_h: Any, min_t: Any) -> str | None:
    for name, value in (("min_height_mm", min_h), ("min_thickness_mm", min_t)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            return f"{name} must be a positive finite number, got {value!r}"
    return None


def propose_silkscreen_minimums(
    pcb_path: str | Path,
    output_dir: str | Path | None = None,
    min_height_mm: float = DEFAULT_MIN_HEIGHT_MM,
    min_thickness_mm: float = DEFAULT_MIN_THICKNESS_MM,
) -> dict:
    """Dry run. Lists every silkscreen text below the minimums with its before and
    after size and stroke; writes nothing. With nothing to raise there is no token."""
    bad = _check_minimums(min_height_mm, min_thickness_mm)
    if bad:
        return {"status": "error", "reason": bad}
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    try:
        changes, unhandled = _plan_all(ip._parse(src), min_height_mm, min_thickness_mm)
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"failed to parse board: {exc}"}
    base = {
        "minimums": {"height_mm": min_height_mm, "thickness_mm": min_thickness_mm},
        "changes": changes,
        "unhandled": unhandled,
        "summary": _summary(changes),
    }
    if not changes and unhandled:
        return {
            "status": "unhandled_only",
            **base,
            "revision_token": None,
            "note": (
                "no text this tool can raise, but silkscreen text remains that it cannot resize "
                "(see unhandled): sub-minimum text may remain on the board"
            ),
        }
    if not changes:
        return {"status": "nothing_to_do", **base, "revision_token": None}

    out_dir = Path(output_dir) if output_dir else _default_output_dir(src)
    problem = checkpoint.output_dir_problem(src, out_dir, what="the prepared board")
    if problem:
        return {"status": "error", "reason": problem}
    try:
        token = _PENDING.issue(
            src, {"out_dir": str(out_dir), "min_h": float(min_height_mm), "min_t": float(min_thickness_mm)}
        )
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        **base,
        "output_pcb": str(out_dir / src.name),
        "revision_token": token,
        "note": (
            "creates a new board revision; the original is untouched. Text is only resized, "
            "never moved: overlaps this creates are for the acceptance gate to report. " + _CAVEATS
            + " Call confirm_silkscreen_minimums with this revision_token to apply."
        ),
    }


_CAVEATS = (
    "Text grows around its anchor, so left/right/top/bottom-justified texts (anchor_shift) extend "
    "in one direction; text boxes (box_may_clip) are not resized and may clip the larger text; "
    "texts with scale above 2.0 (large_scale) are worth a visual check; stale render_cache "
    "(TrueType) is removed on changed texts and KiCad regenerates it."
)


def _default_output_dir(pcb_path: Path) -> Path:
    resolved = pcb_path.resolve()
    return resolved.parent.parent / f"{resolved.parent.name}_silk"


# --- building --------------------------------------------------------------------


def _build_revision(src: Path, out_dir: Path, min_h: float, min_t: float) -> dict:
    """Build, verify and move a revision into ``out_dir`` (the checkpoint helper's temporary
    sibling, reload and compare, then the move). Every failure leaves nothing behind."""
    return checkpoint.build_revision(
        out_dir,
        write=lambda tmp: _write(src, tmp, min_h, min_t),
        verify=lambda tmp, state: _verify(state, min_h, min_t),
        success="applied",
        before_verified="silkscreen raise failed before a revision was verified",
    )


def _snapshot(rec: dict) -> tuple:
    return (
        rec["kind"], rec["owner"], rec["field"], rec["text"], rec["layer"], rec["uuid"],
        tuple(rec["at"]), rec["hidden"], rec["mirrored"], rec["anchor_shift"], rec["render_cache"],
        rec["height"], rec["width"], rec["thickness"],
    )


_SNAPSHOT_FIELDS = (
    "kind", "owner", "field", "text", "layer", "uuid", "at", "hidden", "mirrored", "anchor_shift",
    "render_cache", "height", "width", "thickness",
)


def _unhandled_signature(root: Any, min_h: float, min_t: float) -> list[tuple]:
    return [(u["kind"], u["text"], u["layer"], u["uuid"]) for u in _plan_all(root, min_h, min_t)[1]]


def _write(src: Path, tmp: Path, min_h: float, min_t: float) -> dict:
    """Copy the project into ``tmp``, raise every planned text and save through the write
    layer. Returns what ``_verify`` compares against."""
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    tmp_pcb = checkpoint.copy_project(
        src, tmp, outputs=(f"{src.parent.name}_prepared",), max_bytes=gate._MAX_COPY_BYTES
    )
    board = PCB.load(tmp_pcb)
    before_counts = ip._tag_counts(board._sexp)
    unhandled_before = _unhandled_signature(board._sexp, min_h, min_t)
    items = _collect(board._sexp)
    expected: list[tuple] = []
    changed: list[dict] = []
    for index, (rec, handles) in enumerate(items):
        after_state = dict(rec)
        if _unhandled_reason(rec) is None:
            after = _plan(rec, min_h, min_t)
            if after is not None:
                size = handles["size"]
                size.set_value(0, after["height"])
                size.set_value(1, after["width"])
                if handles["thickness"] is not None:
                    handles["thickness"].set_value(0, after["thickness"])
                else:
                    handles["font"].append(SExp.list("thickness", after["thickness"]))
                cache = handles["node"].find("render_cache")
                while cache is not None:  # stale after a resize; KiCad regenerates it
                    handles["node"].remove(cache)
                    cache = handles["node"].find("render_cache")
                after_state.update(after)
                after_state["render_cache"] = False
                changed.append(_change(rec, index, after))
        expected.append(_snapshot(after_state))
    write_layer.save_pcb(board, tmp_pcb)
    return {
        "pcb": tmp_pcb,
        "before_counts": before_counts,
        "unhandled_before": unhandled_before,
        "expected": expected,
        "changed": changed,
    }


def _verify(state: dict, min_h: float, min_t: float) -> dict:
    """Non-negotiable: verify from a fresh parse of the saved file, never the in-memory tree."""
    tmp_pcb: Path = state["pcb"]
    name = tmp_pcb.name
    try:
        fresh = ip._parse(tmp_pcb)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: revision failed to re-parse: {exc}",
            [checkpoint.mismatch(name, "board", "a board that parses", str(exc))],
        ) from exc
    before_counts = state["before_counts"]
    after_counts = ip._tag_counts(fresh)
    if after_counts != before_counts:
        raise checkpoint.NotVerified(
            "CRITICAL: top-level node counts changed",
            [
                checkpoint.mismatch(name, f"top-level {tag} count", before_counts.get(tag, 0), after_counts.get(tag, 0))
                for tag in sorted(set(before_counts) | set(after_counts))
                if before_counts.get(tag, 0) != after_counts.get(tag, 0)
            ],
            before=dict(before_counts),
            after=dict(after_counts),
        )
    expected = state["expected"]
    actual = [_snapshot(rec) for rec, _ in _collect(fresh)]
    if actual != expected:
        bad = [i for i, (a, e) in enumerate(zip(actual, expected, strict=False)) if a != e]
        raise checkpoint.NotVerified(
            "CRITICAL: a silkscreen text differs from the expected result after save+reload "
            f"(compared by position; first differing index {bad[0] if bad else 'count'})",
            [
                checkpoint.mismatch(name, f"silkscreen text #{i} {field}", want, got)
                for i in bad
                for field, want, got in zip(_SNAPSHOT_FIELDS, expected[i], actual[i], strict=True)
                if want != got
            ]
            or [checkpoint.mismatch(name, "silkscreen text count", len(expected), len(actual))],
        )
    unhandled_after = _unhandled_signature(fresh, min_h, min_t)
    if unhandled_after != state["unhandled_before"]:
        raise checkpoint.NotVerified(
            "CRITICAL: the unhandled silkscreen items changed during the raise",
            [checkpoint.mismatch(name, "unhandled silkscreen items", state["unhandled_before"], unhandled_after)],
        )
    remaining, unhandled = _plan_all(fresh, min_h, min_t)
    if remaining:
        raise checkpoint.NotVerified(
            f"CRITICAL: {len(remaining)} silkscreen text(s) still below the minimums",
            [
                checkpoint.mismatch(name, f"silkscreen text {item.get('text')!r}", "at or above the minimums", item.get("before"))
                for item in remaining
            ],
        )
    return {"changed": state["changed"], "unhandled": unhandled}


def confirm_silkscreen_minimums(pcb_path: str | Path, revision_token: str) -> dict:
    """Apply a proposal: build the revision, verify it from a fresh reload, move it
    into place. The token is consumed only by a success or by detected source drift;
    every other failure leaves it usable."""
    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    out_dir, min_h, min_t = Path(entry["out_dir"]), entry["min_h"], entry["min_t"]
    problem = checkpoint.output_dir_problem(src, out_dir, what="the prepared board")
    if problem:
        return {"status": "error", "reason": problem}

    built = _build_revision(src, out_dir, min_h, min_t)
    if built["status"] != "applied":
        return built
    _PENDING.spend(revision_token)
    return {
        "status": "applied",
        "output_pcb": str(out_dir / src.name),
        "changed": built["changed"],
        "unhandled": built["unhandled"],
        "summary": _summary(built["changed"]),
        "minimums": {"height_mm": min_h, "thickness_mm": min_t},
        "note": (
            "text was resized only, never moved; run the acceptance gate (verify_board) on the "
            "revision to see any overlap or clearance findings this created. " + _CAVEATS
        ),
    }
