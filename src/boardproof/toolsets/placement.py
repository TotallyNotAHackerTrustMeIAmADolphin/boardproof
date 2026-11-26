"""PCB footprint placement toolset (archived backlog Phase 3, P3-01/P3-02/P3-03).

Batch tools on top of `kicad_tools.schema.PCB`'s real placement API
(`add_footprint`, `update_footprint_position`, `place_footprint_safe`) and
its courtyard-collision engine (`verification.py`'s `pcb_check_collisions`,
reused here rather than re-derived). See docs/ARCHITECTURE.md "Tool design
conventions" for the batch/annotation/evidence-gated house style this
follows.
"""

from __future__ import annotations

import logging
import math
import os
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from boardproof import output_detail, placement_anchor, raw_geometry, write_layer
from boardproof.group_edit import CREATE_GROUP_SENTENCE, MOVE_GROUP_SENTENCE
from boardproof.group_lock import GROUP_LOCK_SENTENCE


_footprint_dir_checked = False


def _named_revision(result: dict, source: str) -> dict:
    """The gate field marks the source and the new revision, as confirm_group_lock does."""
    output = result.get("output_pcb")
    return {**result, "pcb_path": output, "source_pcb": source} if isinstance(output, str) else result


def _ensure_kicad_footprint_dir_set() -> None:
    """Point kicad_tools at KiCad's standard footprint libraries via the
    KICAD_FOOTPRINT_DIR env var it already honors.

    Fourth occurrence tonight (2026-09-02) of the same underlying gap
    already fixed for schematic symbol search (schematic.py), ERC
    (verification.py), and gerber generation (manufacturing.py) --
    kicad_tools has no Windows default for *any* of its KiCad-asset search
    paths. Found integrating the golden-path board: `place_footprints`
    with a stock lib-id footprint (e.g. "Resistor_SMD:R_0603_1608Metric")
    failed with "KiCad footprint library path not found" until this was
    set. Same fix shape as the other three -- derive it from
    find_kicad_cli()'s already-working resolution. See docs/ARCHITECTURE.md's
    upstream-PR note: this is strong enough, recurring enough signal to
    contribute find_kicad_cli() upstream wholesale rather than patch a
    fifth call site later.
    """
    global _footprint_dir_checked
    if _footprint_dir_checked or os.environ.get("KICAD_FOOTPRINT_DIR"):
        return
    _footprint_dir_checked = True
    try:
        from boardproof.kicad_cli import find_kicad_cli

        cli_path = find_kicad_cli()
    except Exception:  # noqa: BLE001 - best-effort; let the real error surface downstream if this fails
        return
    footprint_dir = cli_path.parent.parent / "share" / "kicad" / "footprints"
    if footprint_dir.is_dir():
        os.environ["KICAD_FOOTPRINT_DIR"] = str(footprint_dir)


_MOVED_RULE = (
    "The answer lists the first 20 footprints under moved with moved_count (moved_truncated when more were moved); "
    "the optional detail=true lists every one. locked_skipped, not_measured and every refusal are never cut."
)


def _moved_lists(moved: list[dict], detail: bool) -> dict:
    """The footprints a call moved: the first 20 with ``moved_count`` without ``detail`` (issue 261)."""
    return output_detail.listing("moved", moved, detail)


def _hull_boxes(path: Path, refs: list[str]) -> tuple[dict[str, tuple[tuple[float, float, float, float], bool]], dict | None]:
    """({reference: (box, has_courtyard)}, refusal): the hull of each footprint's courtyard and
    pads, read from the raw tokens (issue 228: pads can reach outside the courtyard of an adopted
    footprint). A refusal dict when a reference is unknown or a footprint cannot be measured."""
    try:
        parts = {p.reference: p for p in placement_anchor.read_parts(write_layer.load_file(path), set(refs))}
    except placement_anchor.RawGeometryError as exc:
        return {}, {"status": "error", "code": "footprint_unmeasurable", "reason": f"{exc}: nothing was moved"}
    except Exception as exc:  # noqa: BLE001 - an unreadable board measures nothing
        return {}, {"status": "incomplete", "reason": f"the board's footprints cannot be read raw: {exc}: nothing was moved"}
    missing = [r for r in refs if r not in parts]
    if missing:
        return {}, {"status": "error", "reason": f"unknown references: {missing}"}
    return {r: (parts[r].box, parts[r].box_source == "courtyard") for r in refs}, None


# Module-level pending-state for autoplacement proposals, mirroring
# sourcing.py's _PENDING_MATCHES bound+TTL pattern (archived backlog P1-19) -- the
# first tool in this codebase to actually implement the dry-run +
# revision-token pattern for a real mutating operation, not just describe
# it (docs/ARCHITECTURE.md's "Status honesty" note on this).
_MAX_REPORTED_ILLEGAL = 50  # the placer's own 'illegal' list is passed on only up to this many entries, with the full count beside it
_PENDING_PLACEMENTS: dict[str, tuple[str, dict[str, tuple[float, float, float]], float]] = {}
_PENDING_PLACEMENT_TTL_SECONDS = 60 * 60
_PENDING_PLACEMENT_MAX_ENTRIES = 1000


def _evict_stale_pending_placements() -> None:
    now = time.time()
    expired = [t for t, (_, _, exp) in _PENDING_PLACEMENTS.items() if exp <= now]
    for t in expired:
        del _PENDING_PLACEMENTS[t]
    if len(_PENDING_PLACEMENTS) > _PENDING_PLACEMENT_MAX_ENTRIES:
        oldest_first = sorted(_PENDING_PLACEMENTS.items(), key=lambda kv: kv[1][2])
        overflow = len(_PENDING_PLACEMENTS) - _PENDING_PLACEMENT_MAX_ENTRIES
        for t, _ in oldest_first[:overflow]:
            del _PENDING_PLACEMENTS[t]


# ---------------------------------------------------------------------------
# P3-10: remove_graphic_items (propose/confirm) -- see archived backlog P3-10 and
# docs/ARCHITECTURE.md's "No kicad_tools API to remove graphic items" finding.
#
# M-03 extension: `zone` was added to the removable-shape set. A top-level
# `(zone ...)` node is structurally the same kind of node as a gr_rect/
# gr_poly for every helper below -- it has its own (uuid ...), an optional
# net child (any form KiCad writes, read by _graphic_node_net_name through the
# shared raw_geometry.net_name), a (layer "...") child, and
# its outline lives under (polygon (pts ...)) which SExp.find("pts") reaches
# fine since find() searches all descendants, not just immediate children.
# No new removal technique -- same live-_sexp-node-removal path P3-10 proved
# safe, just widening which tags qualify.
#
# kicad_tools has no public API to add OR remove a standalone board-level
# graphic item (gr_rect/gr_poly/gr_line/gr_circle/gr_arc/zone) -- the only thing
# close to it is the private S-expression removal pattern inside
# PCB.replace_outline()/PCB.remove_edge_contour(), which only handles
# Edge.Cuts contours. Per CLAUDE.md's "never hand-edit KiCad files" rule and
# this ticket's own instructions, this works the same way replace_outline
# does: mutate the LIVE parsed node (`board._sexp`, kicad_tools' own private
# root S-expression, the same attribute replace_outline itself reaches into)
# directly, never a dataclass round-trip -- see the SymbolInstance.to_sexp()
# finding in docs/ARCHITECTURE.md for why a rebuild-from-dataclass approach is
# exactly the failure mode to avoid here. kicad_tools' own BoardGraphic model
# doesn't even parse gr_poly or a graphic's (net ...) child (confirmed by
# reading kicad_tools/schema/pcb.py directly), so filtering by shape/net
# requires reading the raw sexp children ourselves rather than trusting the
# in-memory board.graphics list.
_GRAPHIC_SHAPES = frozenset({"gr_rect", "gr_poly", "gr_line", "gr_circle", "gr_arc", "zone"})
_GRAPHIC_FILTER_KEYS = frozenset({"layer", "net_name", "shape", "bbox"})

# pcb_path -> (target uuids, expiry). Mirrors _PENDING_PLACEMENTS' bound+TTL
# pattern (archived backlog P1-19).
_PENDING_GRAPHIC_REMOVALS: dict[str, tuple[str, list[str], float]] = {}
_PENDING_GRAPHIC_REMOVAL_TTL_SECONDS = 60 * 60
_PENDING_GRAPHIC_REMOVAL_MAX_ENTRIES = 1000


def _evict_stale_pending_graphic_removals() -> None:
    now = time.time()
    expired = [t for t, (_, _, exp) in _PENDING_GRAPHIC_REMOVALS.items() if exp <= now]
    for t in expired:
        del _PENDING_GRAPHIC_REMOVALS[t]
    if len(_PENDING_GRAPHIC_REMOVALS) > _PENDING_GRAPHIC_REMOVAL_MAX_ENTRIES:
        oldest_first = sorted(_PENDING_GRAPHIC_REMOVALS.items(), key=lambda kv: kv[1][2])
        overflow = len(_PENDING_GRAPHIC_REMOVALS) - _PENDING_GRAPHIC_REMOVAL_MAX_ENTRIES
        for t, _ in oldest_first[:overflow]:
            del _PENDING_GRAPHIC_REMOVALS[t]


def _graphic_node_layer(node: Any) -> str:
    layer = node.find("layer")
    return (layer.get_string(0) or "") if layer is not None else ""


def _graphic_node_net_name(node: Any, board: Any) -> str | None:
    """Net name of a graphic item's (net ...) child: None if it has no (net ...)
    child, "" for net 0 (KiCad's "no net"). Read from the raw S-expression by
    raw_geometry.net_name, so KiCad 10's (net "name") and the older (net N) and
    (net N "name") forms all work; an unknown form or an undeclared number
    raises raw_geometry.RawGeometryError.
    """
    if node.find("net") is None:
        return None
    return raw_geometry.net_name(node, raw_geometry.net_table(board._sexp))  # unknown form: RawGeometryError


def _graphic_node_uuid(node: Any) -> str:
    u = node.find("uuid")
    return (u.get_string(0) or "") if u is not None else ""


def _graphic_node_points(node: Any) -> list[tuple[float, float]]:
    points: list[tuple[float, float]] = []
    for attr in ("start", "end", "mid", "center"):
        child = node.find(attr)
        if child is not None:
            points.append((child.get_float(0) or 0.0, child.get_float(1) or 0.0))
    pts = node.find("pts")
    if pts is not None:
        for xy in pts.children:
            if xy.name == "xy":
                points.append((xy.get_float(0) or 0.0, xy.get_float(1) or 0.0))
    return points


def _graphic_node_bbox(node: Any) -> tuple[float, float, float, float] | None:
    """Approximate (minx, miny, maxx, maxy) bounding box in mm.

    Exact for gr_rect/gr_line/gr_poly. Exact for gr_circle (derived from
    center + radius, not just the two sexp points, which alone would only
    bound a corner of the circle). Approximate for gr_arc (start/mid/end
    points only, not full arc-sweep extrema) -- good enough for a
    region-of-interest filter, not claimed to be precise.
    """
    if node.tag == "gr_circle":
        center = node.find("center")
        end = node.find("end")
        if center is not None and end is not None:
            cx, cy = center.get_float(0) or 0.0, center.get_float(1) or 0.0
            ex, ey = end.get_float(0) or 0.0, end.get_float(1) or 0.0
            r = ((ex - cx) ** 2 + (ey - cy) ** 2) ** 0.5
            return (cx - r, cy - r, cx + r, cy + r)
    points = _graphic_node_points(node)
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _describe_graphic_node(node: Any, board: Any) -> dict:
    return {
        "shape": node.tag,
        "layer": _graphic_node_layer(node),
        "net_name": _graphic_node_net_name(node, board),
        "uuid": _graphic_node_uuid(node),
        "bbox": _graphic_node_bbox(node),
    }


def _iter_top_level_graphics(board: Any):
    for child in board._sexp.children:
        if not child.is_atom and child.tag in _GRAPHIC_SHAPES:
            yield child


def _top_level_tag_counts(board: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for child in board._sexp.children:
        if child.is_atom:
            continue
        tag = child.tag or ""
        counts[tag] = counts.get(tag, 0) + 1
    return counts


def _validate_graphic_filters(filters: list[dict]) -> str | None:
    if not filters:
        return "filters must be a non-empty list"
    for i, f in enumerate(filters):
        if not isinstance(f, dict):
            return f"filters[{i}] must be an object"
        unknown = set(f) - _GRAPHIC_FILTER_KEYS
        if unknown:
            return f"filters[{i}] has unknown keys: {sorted(unknown)} -- allowed: {sorted(_GRAPHIC_FILTER_KEYS)}"
        if all(f.get(k) is None for k in _GRAPHIC_FILTER_KEYS):
            return f"filters[{i}] specifies no criteria -- would match every graphic item on the board (including the board outline); refusing"
        shape = f.get("shape")
        if shape is not None and shape not in _GRAPHIC_SHAPES:
            return f"filters[{i}]: unknown shape {shape!r} -- must be one of {sorted(_GRAPHIC_SHAPES)}"
        bbox = f.get("bbox")
        if bbox is not None and (not isinstance(bbox, (list, tuple)) or len(bbox) != 4):
            return f"filters[{i}]: bbox must be [min_x, min_y, max_x, max_y]"
    return None


def _graphic_filter_matches(node: Any, board: Any, f: dict) -> bool:
    shape = f.get("shape")
    if shape is not None and node.tag != shape:
        return False
    layer = f.get("layer")
    if layer is not None and _graphic_node_layer(node) != layer:
        return False
    net_name = f.get("net_name")
    if net_name is not None and _graphic_node_net_name(node, board) != net_name:
        return False
    bbox = f.get("bbox")
    if bbox is not None:
        item_bbox = _graphic_node_bbox(node)
        if item_bbox is None:
            return False
        fx0, fy0, fx1, fy1 = bbox
        ix0, iy0, ix1, iy1 = item_bbox
        if ix1 < fx0 or ix0 > fx1 or iy1 < fy0 or iy0 > fy1:
            return False
    return True


def _match_graphic_items(board: Any, filters: list[dict]) -> list:
    """Live sexp nodes matching ANY filter (filters are OR'd; the criteria
    within one filter are AND'd), de-duplicated, in stable document order.
    """
    matched = []
    seen_ids: set[int] = set()
    for node in _iter_top_level_graphics(board):
        if id(node) in seen_ids:
            continue
        if any(_graphic_filter_matches(node, board, f) for f in filters):
            seen_ids.add(id(node))
            matched.append(node)
    return matched


_RULE_BBOX = "min corner of the Edge.Cuts bounding box (arc bulges and circles included)"
_RULE_NONE = "none: page origin (0,0)"
_REFUSAL = (
    "the Edge.Cuts outline cannot be read exactly: use propose_board_outline to "
    "normalise the outline"
)
# A footprint's Edge.Cuts cannot be normalised away by propose_board_outline, so it gets its
# own hint (build ticket 42 fix round 2). Measuring it is a follow-up ticket.
_FOOTPRINT_REFUSAL = (
    "the board-local frame cannot be stated exactly: Edge.Cuts graphics inside footprints "
    "are not measured yet"
)
_NOT_MEASURED_RULE = (
    "A footprint on the board with neither courtyard nor pad (a logo) is listed under not_measured and left out of the "
    "collision check; that check says nothing about it."
)
_FRAME_RULE = (
    "Coordinates are BOARD-LOCAL millimetres: local (0,0) is the top-left corner of the "
    "bounding box of the Edge.Cuts outline (arc bulges and circles included); the tool "
    "refuses an outline it cannot read exactly (a curve, an arc inside a polygon, a legacy "
    "arc without a mid point, Edge.Cuts graphics inside a footprint, a malformed or "
    "unsupported Edge.Cuts item); without any outline coordinates are page coordinates. "
    "Results carry "
    "origin_mm (the page point local (0,0) maps to), origin_rule and frame."
)
_RELATIVE_RULE = (
    "Takes references and works on relative offsets (no coordinates are passed in); it "
    "reports local positions, relative to the page point origin_mm (see frame in the result)."
)

_OUTLINE_TAGS = ("gr_rect", "gr_line", "gr_arc", "gr_circle", "gr_poly")


def _xy(node: Any, tag: str) -> tuple[float, float]:
    values = node.find(tag).values
    return float(values[0]), float(values[1])


def _arc_extent(start: tuple, mid: tuple, end: tuple) -> list[tuple[float, float]] | None:
    """Points whose bounding box is the start/mid/end arc's: the three points plus every
    axis extreme of its circle that the swept angle reaches. None for a collinear arc."""
    import math

    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None
    s1, s2, s3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    cx = (s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d
    cy = (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d
    radius = math.dist((cx, cy), start)
    tau = 2 * math.pi
    a_start, a_mid, a_end = (math.atan2(y - cy, x - cx) for x, y in (start, mid, end))
    span = (a_end - a_start) % tau
    if (a_mid - a_start) % tau <= span:
        low = a_start  # counter-clockwise from start through mid to end
    else:
        low, span = a_end, (a_start - a_end) % tau
    points = [start, mid, end]
    for k in range(4):
        angle = k * math.pi / 2
        if (angle - low) % tau <= span:
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _mm(value: float) -> float:
    """A coordinate rounded to 1e-9 mm (1e-3 nm, far below KiCad's 1 nm), so float noise
    from the arc arithmetic (80.29999999999998) is not reported; never ``-0.0``."""
    return round(float(value), 9) + 0.0


def _on_edge_cuts(node: Any) -> bool:
    if node.is_atom:
        return False
    layer = node.find("layer")
    return layer is not None and bool(layer.values) and layer.values[0] == "Edge.Cuts"


def _footprint_reference(footprint: Any) -> str:
    for child in footprint.children:
        if child.is_atom:
            continue
        if child.name == "property" and len(child.children) >= 2 and child.children[0].value == "Reference":
            return str(child.children[1].value)
        if child.name == "fp_text" and len(child.children) >= 2 and child.children[0].value == "reference":
            return str(child.children[1].value)
    lib_id = footprint.children[0].value if footprint.children and footprint.children[0].is_atom else "?"
    return f"(no reference, {lib_id})"


def _outline_frame(pcb_path: str | Path) -> tuple[dict, str | None]:
    """``(fields, problem)`` of ``_read_outline``."""
    fields, problem, _outline, _footprints = _read_outline(pcb_path)
    return fields, problem


def _read_outline(pcb_path: str | Path) -> tuple[dict, str | None, str | None, list[str]]:
    """The origin the library applies (kicad_tools 0.23.0: the min corner of the bounding box
    of every top-level Edge.Cuts ``gr_*`` item, arc bulges and circles included; (0, 0) when
    there is none or one it cannot read), computed independently from the RAW nodes.
    Returns ``({origin_mm, origin_rule, frame}, problem)``; ``problem`` is None only when the
    outline was read exactly (or there is no outline at all).

    Read exactly means this reader's box is KiCad's own Edge.Cuts box. It is not, and
    ``problem`` names the item, for: a ``gr_curve`` (the library samples it); a ``gr_poly``
    whose points hold an ``(arc ...)`` (neither reader includes its bulge); a legacy
    ``gr_arc`` without ``(mid ...)`` (the library reads it, this reader does not); and any
    footprint carrying ``fp_*`` items on Edge.Cuts (KiCad's outline includes them, neither
    reader does; build ticket 42 fix round 1, the RoyalBlue54L NFC-antenna demo). An item
    the library cannot read at all makes it use the page origin, and the origin here is
    (0, 0) to match.

    Also returns, for the refusal's hint, the problem without the footprint entries (None
    if there is none) and one ``"<reference> (<tags>)"`` per footprint carrying Edge.Cuts."""

    root = write_layer.load_file(Path(pcb_path))
    points: list[tuple[float, float]] = []
    unreadable: list[str] = []  # the library falls back to the page origin
    inexact: list[str] = []  # read, but the box here is not KiCad's exact one
    footprint_problems: list[str] = []
    footprints: list[str] = []
    curves = 0
    for node in root.children:
        if node.name == "footprint":
            on_edge = sorted({c.name for c in node.children if _on_edge_cuts(c)})
            if on_edge:
                reference = _footprint_reference(node)
                footprints.append(f"{reference} ({', '.join(on_edge)})")
                footprint_problems.append(
                    f"footprint {reference} with {', '.join(on_edge)} on Edge.Cuts, "
                    "which KiCad's outline includes and this reader does not"
                )
            continue
        if not (node.name or "").startswith("gr_") or not _on_edge_cuts(node):
            continue
        try:
            if node.name in ("gr_rect", "gr_line"):
                points += [_xy(node, "start"), _xy(node, "end")]
            elif node.name == "gr_arc":
                if node.find("mid") is None:
                    inexact.append(
                        "a gr_arc without (mid ...) (the legacy centre/angle form), whose extent "
                        "this reader does not compute"
                    )
                    continue
                arc = _arc_extent(_xy(node, "start"), _xy(node, "mid"), _xy(node, "end"))
                if arc is None:
                    unreadable.append("collinear gr_arc")
                    continue
                points += arc
            elif node.name == "gr_circle":
                import math

                center, rim = _xy(node, "center"), _xy(node, "end")
                r = math.dist(center, rim)
                points += [(center[0] - r, center[1] - r), (center[0] + r, center[1] + r)]
            elif node.name == "gr_poly":
                pts = node.find("pts")
                items = [c for c in (pts.children if pts else []) if not c.is_atom]
                chain = [(float(xy.values[0]), float(xy.values[1])) for xy in items if xy.name == "xy"]
                others = sorted({c.name for c in items if c.name != "xy"})
                if others:
                    inexact.append(
                        "a gr_poly with " + ", ".join(f"({o} ...)" for o in others)
                        + " among its points, whose extent this reader does not compute"
                    )
                elif len(chain) < 3:
                    unreadable.append("gr_poly with fewer than 3 points")
                    continue
                points += chain
            elif node.name == "gr_curve":
                curves += 1  # the library samples it; its extent is not exact here
                pts = node.find("pts")
                points += [(float(xy.values[0]), float(xy.values[1])) for xy in (pts.find_all("xy") if pts else [])]
            else:
                unreadable.append(node.name)
        except (AttributeError, IndexError, TypeError, ValueError):
            unreadable.append(f"malformed {node.name}")
    if curves:
        inexact.insert(0, f"{curves} gr_curve, whose extent the library only samples")

    problems = []
    if unreadable:
        origin, rule = (0.0, 0.0), _RULE_NONE
        problems.append(
            f"{', '.join(sorted(set(unreadable)))}, which the library cannot read as outline, "
            "so it uses the page origin"
        )
    elif points:
        origin, rule = (min(p[0] for p in points), min(p[1] for p in points)), _RULE_BBOX
    else:
        origin, rule = (0.0, 0.0), _RULE_NONE
    problems += inexact
    outline_problem = f"Edge.Cuts holds {'; '.join(problems)}" if problems else None
    problems = footprint_problems + problems
    problem = f"Edge.Cuts holds {'; '.join(problems)}" if problems else None
    x, y = _mm(origin[0]), _mm(origin[1])
    if problem is not None:
        frame = (
            f"NOT board-local: positions are relative to page point ({x:g}, {y:g}), which is "
            f"not an exactly known outline corner ({problem})"
        )
    elif rule == _RULE_NONE:
        frame = "page coordinates (no outline yet): local = page, origin (0, 0)"
    else:
        frame = (
            f"board-local (origin = Edge.Cuts bounding-box top-left = ({x:g}, {y:g}) in page mm; "
            f"rule: {rule})"
        )
    fields = {"origin_mm": [x, y], "origin_rule": rule, "frame": frame}
    return fields, problem, outline_problem, footprints


_SNAP_ANCHORS = ("outline", "absolute")


def _read_grid_origin(pcb_path: str | Path) -> tuple[float, float]:
    """The board's user grid origin, page millimetres, from the RAW ``(setup (grid_origin x y))``
    token; (0, 0) when the token is absent. A token that is not exactly two finite numbers
    raises ValueError (the caller refuses; nothing is guessed)."""
    import math


    setup = write_layer.load_file(Path(pcb_path)).find("setup")
    node = setup.find("grid_origin") if setup is not None else None
    if node is None:
        return (0.0, 0.0)
    try:
        values = [float(v) for v in node.values]
    except (TypeError, ValueError):
        values = []
    if len(values) != 2 or not all(math.isfinite(v) for v in values):
        raise ValueError("the board's (setup (grid_origin ...)) is not two finite numbers")
    return (values[0], values[1])


def _frame_fields(pcb_path: Any) -> dict:
    try:
        if isinstance(pcb_path, (str, Path)) and Path(pcb_path).is_file():
            return _outline_frame(pcb_path)[0]
    except Exception:  # noqa: BLE001  (an unreadable board is reported by the tool itself)
        pass
    return {}


def _frame_gate(pcb_path: str | Path, board: Any, *, strict: bool) -> dict | None:
    """Cross-check the raw origin against the one the loaded library applies (any
    disagreement is an error) and, when ``strict``, refuse an outline the raw reader could not
    read exactly. Returns an error result, or None to proceed."""
    fields, problem, outline_problem, footprints = _read_outline(pcb_path)
    library = tuple(_mm(v) for v in getattr(board, "_board_origin", (0.0, 0.0)))
    raw = tuple(_mm(v) for v in fields["origin_mm"])
    if any(abs(a - b) > 1e-6 for a, b in zip(raw, library)):
        return {
            "status": "error",
            "reason": (
                f"the raw Edge.Cuts origin {list(raw)} and the origin kicad_tools applies "
                f"{list(library)} disagree: nothing was moved"
                + (f" ({problem})" if problem is not None else "")
            ),
            **fields,
        }
    if strict and problem is not None:
        # Each kind of problem gets the hint that can fix it: an outline item can be
        # normalised by propose_board_outline, a footprint's Edge.Cuts cannot.
        parts = []
        if outline_problem is not None:
            parts.append(f"{_REFUSAL} ({outline_problem})")
        if footprints:
            parts.append(f"{_FOOTPRINT_REFUSAL} (footprint {', footprint '.join(footprints)})")
        return {"status": "error", "reason": "; ".join(parts) + "; nothing was moved", **fields}
    return None


def _with_frame(fn):
    """Add origin_mm / origin_rule / frame to every dict a tool returns (errors too)."""
    import functools
    import inspect

    signature = inspect.signature(fn)

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        result = fn(*args, **kwargs)
        if isinstance(result, dict) and "frame" not in result:
            try:
                path = signature.bind_partial(*args, **kwargs).arguments.get("pcb_path")
            except TypeError:
                path = None
            result = {**result, **_frame_fields(path)}
        return result

    return wrapper


_LOCKED_NOTE = (
    "A locked footprint (a raw (locked yes) token; lock_footprints sets and clears it) is never moved "
    "by this tool."
)


class _LockUnreadable(Exception):
    """The raw tokens could not be read, so no footprint can be said to be unlocked. ``code`` is
    ``lock_state_unreadable``, or the write layer's own code when it refused the file (for
    example ``unsupported_file_version``, GitHub issue 213)."""

    def __init__(self, message: str, code: str = "lock_state_unreadable") -> None:
        super().__init__(message)
        self.code = code


def _locked_references(path: Path) -> set[str]:
    """References of the locked footprints, read from the RAW tokens. Fails closed: when the
    file cannot be read the lock state is unknown and the tool refuses (``_fail_closed``)."""
    try:
        return placement_anchor.locked_references(write_layer.load(path))
    except write_layer.WriteLayerError as exc:
        raise _LockUnreadable(f"the lock state of {path.name} cannot be read ({exc})", exc.code) from exc
    except Exception as exc:  # noqa: BLE001
        raise _LockUnreadable(f"the lock state of {path.name} cannot be read ({exc})") from exc


def _fail_closed(fn):
    """A tool that reads locks answers ``lock_state_unreadable`` (nothing written) when it cannot."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except _LockUnreadable as exc:
            return {"status": "error", "code": exc.code,
                    "reason": f"{exc}: nothing was moved"}

    return wrapper


_LOG = logging.getLogger("boardproof.placement")


def _error_on_failure(fn):
    """A long tool never ends in silence: any unexpected exception becomes an error result (issue 240)."""
    import functools

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed; _LockUnreadable passes through
            if isinstance(exc, _LockUnreadable):
                raise
            _LOG.exception("%s failed", fn.__name__)
            return {"status": "error", "reason": f"{fn.__name__} failed: {type(exc).__name__}: {exc}; nothing was moved"}

    return wrapper


def _drc_counts(path: Path) -> dict:
    """One kicad-cli DRC run as counts, a measurement and not a verdict; a run that cannot happen says so."""
    from boardproof import kicad_cli

    try:
        kicad_cli.ensure_kicad_cli_on_path()
        report = kicad_cli.run_drc(path)
        violations = report.get("violations", [])
        return {
            "violation_count": len(violations),
            "error_count": sum(1 for v in violations if str(v.get("severity", "")).lower() == "error"),
            "unconnected_count": len(report.get("unconnected_items", [])),
        }
    except Exception as exc:  # noqa: BLE001 - a missing measurement is reported, never replaced by zero
        return {"status": "incomplete", "reason": f"DRC could not run: {exc}"}


class _SlotsUnreadable(Exception):
    """The outline's cut-outs could not be read, so no courtyard can be said to be clear of them."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code


def _slot_conflicts(path: Path, specs: list[dict], locked: set[str]) -> list[dict]:
    """One problem per spec that moves an existing, unlocked footprint so that its courtyard (its
    rotated pad boxes where it has none) overlaps a slot or a rounded corner of the outline, and
    one (``footprint_unmeasurable``) per such footprint that has neither courtyard nor pad. Only
    the footprints the specs name are measured. Read from the raw tokens; a board without
    cut-outs is not read further. Coordinates are board-local, as in the spec. A footprint added
    by ``library_id`` has no box before it is added and is not checked."""
    try:
        root = write_layer.load(path)
        cutouts = placement_anchor.cutout_boxes(root)
        if not cutouts:
            return []
        origin = _outline_frame(path)[0]["origin_mm"]
        named = {s.get("reference") for s in specs if isinstance(s, dict) and isinstance(s.get("reference"), str)}
        raw_refs = [g.reference for g in placement_anchor.raw_geometry.read_footprints(root)]
    except placement_anchor.RoundedCornerUnreadable as exc:
        raise _SlotsUnreadable("outline_arcs_unreadable", str(exc)) from exc
    except placement_anchor.OutlineNotRectangular as exc:
        raise _SlotsUnreadable("outline_not_rectangular", str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        raise _SlotsUnreadable("slots_unreadable", f"the slots of {path.name} cannot be read ({exc})") from exc
    by_ref: dict[str, list[placement_anchor.Part]] = {}
    problems = []
    for ref in sorted(named):
        if ref in locked or raw_refs.count(ref) != 1:
            continue
        try:
            for part in placement_anchor.read_parts(root, {ref}):
                by_ref.setdefault(part.reference, []).append(part)
        except placement_anchor.RawGeometryError as exc:
            problems.append({"reference": ref, "code": "footprint_unmeasurable",
                             "reason": f"{ref} has no courtyard and no pad to measure against the slots ({exc})"})
    for spec in specs:
        ref = spec.get("reference")
        found = by_ref.get(ref, []) if isinstance(ref, str) else []
        x, y, rotation = spec.get("x"), spec.get("y"), spec.get("rotation")
        if len(found) != 1 or ref in locked or not all(_is_number(v) for v in (x, y)):
            continue
        if rotation is not None and not _is_number(rotation):
            continue
        part = found[0]
        at = (float(x) + origin[0], float(y) + origin[1])
        box = placement_anchor.box_at(part, at, part.angle if rotation is None else float(rotation))
        if any(placement_anchor.overlap(box, cut) for cut in cutouts):
            problems.append({"spec": spec, "reference": ref, "code": "in_cutout",
                             "reason": f"its courtyard {[round(v, 6) for v in box]} would overlap a slot or a rounded corner of the outline"})
    return problems


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _not_verified(exc: placement_anchor.NotVerified) -> dict:
    return {
        "status": "error", "ok": False, "code": "write_not_verified", "reason": exc.reason,
        "mismatches": exc.mismatches[:20], "note": "the board file was left untouched",
    }


def build(mcp: MCPServer) -> list[str]:
    @_with_frame
    @_fail_closed
    def place_footprints(pcb_path: str, specs: list[dict], detail: bool = False) -> dict:
        """Place or move a batch of footprints on a board in one call.
        Coordinates are BOARD-LOCAL millimetres: local (0,0) is the top-left corner of the
        bounding box of the Edge.Cuts outline (arc bulges and circles included); the
        tool refuses an outline it cannot read exactly (a curve, an arc inside a
        polygon, a legacy arc without a mid point, Edge.Cuts graphics inside a footprint,
        a malformed or unsupported Edge.Cuts item); without any outline coordinates are
        page coordinates.

        A move of an existing, unlocked footprint whose courtyard (its rotated pad boxes where it
        has none) would overlap a slot or a rounded corner of the outline is refused as in_cutout
        for the whole call and nothing is written; a footprint that already overlaps a cut-out is refused for any move whose new courtyard still overlaps one; a footprint added by library_id is not checked. An outline whose arcs are not all quarter-circle corners of a rectangle is refused as outline_arcs_unreadable (that is: an outer loop of lines and arcs, or a gr_poly, that is neither a plain rectangle nor a rectangle with quarter-circle corner arcs is refused for the whole call, nothing written (outline_arcs_unreadable when it has any arc, such as a bite, a notch or a corner built from several arcs; outline_not_rectangular when it is lines only or a gr_poly, such as an L shape); circles, stadiums and well-formed slots are not refused; any other Edge.Cuts problem (a stray or open item, a slot that touches the outline or another slot, a duplicate outer loop, an item the tool cannot read) is refused as slots_unreadable; a board with no Edge.Cuts at all is not checked), a named footprint with no courtyard and no pad as footprint_unmeasurable, and unreadable cut-outs as slots_unreadable.

        Each spec: {"reference": str, "library_id": str (only for a NEW
        footprint), "x": float, "y": float, "rotation": float (default 0),
        "layer": str (default "F.Cu"), "value": str (optional)}. A
        reference that already exists on the board is moved (via
        update_footprint_position); one that doesn't and has a "library_id"
        is added (via add_footprint). Never call this once per footprint --
        pass the whole batch.

        The answer carries counts (placed_count, moved_count, rotation_kept_count) and every error;
        detail=true adds the lists placed, moved and rotation_kept, which a partial or error answer
        always carries.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """

        _ensure_kicad_footprint_dir_set()
        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=True)
        if refusal is not None:
            return refusal

        placed: list[str] = []
        moved: list[str] = []
        rotation_kept: list[str] = []
        errors: list[dict] = []
        locked = _locked_references(path)
        try:
            in_slot = _slot_conflicts(path, specs, locked)
        except _SlotsUnreadable as exc:
            return {"status": "error", "code": exc.code, "reason": f"{exc}: nothing was moved"}
        if in_slot:
            code = "in_cutout" if any(p["code"] == "in_cutout" for p in in_slot) else in_slot[0]["code"]
            return {"status": "error", "code": code, **output_detail.listing("errors", in_slot, True),  # per-item refusals decide the retry: never cut
                    "reason": "a courtyard would overlap a slot or a rounded corner of the outline, or cannot be measured: nothing was moved"}
        for spec in specs:
            ref = spec.get("reference")
            if not ref:
                errors.append({"spec": spec, "reason": "missing 'reference'"})
                continue
            x, y = spec.get("x"), spec.get("y")
            rotation = spec.get("rotation")  # None: keep an existing footprint's rotation
            layer = spec.get("layer", "F.Cu")
            existing = board.get_footprint(ref)
            if existing is not None and ref in locked:
                errors.append({"spec": spec, "code": "locked_footprint",
                               "reason": f"{ref} is locked and was not moved; unlock it with lock_footprints (locked=false) first"})
                continue
            try:
                if existing is not None:
                    board.update_footprint_position(ref, x, y, rotation)
                    moved.append(ref)
                    if rotation is None:
                        rotation_kept.append(ref)
                else:
                    library_id = spec.get("library_id")
                    if not library_id:
                        errors.append({"spec": spec, "reason": "new footprint needs 'library_id'"})
                        continue
                    # A library_id that's a real file path on disk (e.g. the
                    # output of footprint_gen.generate_footprint or
                    # asset_fetch.fetch_part_assets) loads that exact
                    # .kicad_mod directly; otherwise it's treated as a KiCad
                    # lib-id ("Library:Footprint") resolved against KiCad's
                    # own installed footprint libraries.
                    new_rotation = 0.0 if rotation is None else rotation
                    if Path(library_id).exists():
                        board.add_footprint_from_file(library_id, ref, x, y, rotation=new_rotation, layer=layer, value=spec.get("value", ""))
                    else:
                        board.add_footprint(library_id, ref, x, y, rotation=new_rotation, layer=layer, value=spec.get("value", ""))
                    placed.append(ref)
            except Exception as exc:  # noqa: BLE001
                errors.append({"spec": spec, "reason": str(exc)})

        if placed or moved:
            try:
                write_layer.save_pcb(board, path)
            except Exception as exc:  # noqa: BLE001
                if (refusal := write_layer.as_lock_refusal(exc)) is not None:
                    return refusal
                return {"status": "incomplete", "reason": f"placed in-memory but failed to save: {exc}"}

        status = "ok" if not errors else ("partial" if (placed or moved) else "error")
        # which specs succeeded and which failed decides the retry: a partial or error answer
        # lists every item; only the plain success is counted without detail
        full = detail or status != "ok"
        return {
            "status": status,
            **output_detail.counted("placed", placed, full),
            **output_detail.counted("moved", moved, full),
            **output_detail.listing("errors", errors, True),
            **output_detail.counted("rotation_kept", rotation_kept, full),
            **({} if full else {"detail_note": "placed, moved and rotation_kept are counted only; " + output_detail.DETAIL_NOTE}),
        }

    def move_reference_designators_to_layer(pcb_path: str, refs: list[str], layer: str) -> dict:
        """Move a batch of footprints' reference-designator SILKSCREEN TEXT
        to a different layer in one call (e.g. "F.Fab" so refs don't appear
        on manufactured silkscreen) -- this does NOT flip which side of the
        board a footprint is mounted on, only where its ref-designator text
        renders. Named precisely to avoid the archived backlog P1-14 mistake
        (a tool whose name implied more than it actually did): the only
        underlying kicad_tools method here is PCB.move_references_to_layer,
        which moves reference-text layer assignment only.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}

        moved_count = 0
        try:
            for ref in refs:
                moved_count += board.move_references_to_layer(layer, pattern=ref)
            write_layer.save_pcb(board, path)
        except Exception as exc:  # noqa: BLE001
            if (locked := write_layer.as_lock_refusal(exc)) is not None:
                return locked
            return {"status": "incomplete", "reason": str(exc)}
        return {"status": "ok", "requested": len(refs), "moved_count": moved_count, "layer": layer}

    @_with_frame
    @_fail_closed
    def align_footprints(pcb_path: str, refs: list[str], axis: str, edge: str, detail: bool = False) -> dict:
        """Align a batch of footprints' edges or centers along a shared X/Y
        coordinate (like a design tool's align-left/right/top/bottom/center).
        Takes references and works on relative offsets (no coordinates are passed in); it
        reports local positions, relative to origin_mm (see frame in the result).

        axis: "x" or "y". edge: "min"/"max"/"center" of the bounding box on that axis of the
        hull of the courtyard and the pads (read from the raw footprint tokens; the pads alone
        when the footprint has no courtyard, reported per footprint via "used_courtyard").

        The answer lists the first 20 footprints under moved with moved_count (and moved_truncated when more were
        moved); detail=true lists every one. locked_skipped is never cut.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """

        if axis not in ("x", "y"):
            return {"status": "error", "reason": "axis must be 'x' or 'y'"}
        if edge not in ("min", "max", "center"):
            return {"status": "error", "reason": "edge must be 'min', 'max', or 'center'"}
        if len(refs) < 2:
            return {"status": "error", "reason": "need at least 2 refs to align"}

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=False)
        if refusal is not None:
            return refusal

        boxes, refused_boxes = _hull_boxes(path, refs)
        if refused_boxes is not None:
            return refused_boxes

        axis_idx_min = 0 if axis == "x" else 1
        axis_idx_max = 2 if axis == "x" else 3

        def edge_value(bbox):
            if edge == "min":
                return bbox[axis_idx_min]
            if edge == "max":
                return bbox[axis_idx_max]
            return (bbox[axis_idx_min] + bbox[axis_idx_max]) / 2

        # Align to the first footprint's own edge value -- an arbitrary but
        # stable, deterministic anchor (matches how most design-tool
        # align-to-selection tools behave when no explicit anchor is given).
        anchor_ref = refs[0]
        target = edge_value(boxes[anchor_ref][0])

        locked = _locked_references(path)
        locked_skipped = [r for r in refs if r in locked]
        moved: list[dict] = []
        for ref in refs:
            if ref in locked:
                continue
            bbox, used_courtyard = boxes[ref]
            fp = board.get_footprint(ref)
            cur_x, cur_y = fp.position
            delta = target - edge_value(bbox)
            new_x = cur_x + delta if axis == "x" else cur_x
            new_y = cur_y + delta if axis == "y" else cur_y
            board.update_footprint_position(ref, new_x, new_y, None)
            moved.append({"reference": ref, "x": new_x, "y": new_y, "used_courtyard": used_courtyard})

        if moved:
            try:
                write_layer.save_pcb(board, path)
            except Exception as exc:  # noqa: BLE001
                if (refusal := write_layer.as_lock_refusal(exc)) is not None:
                    return refusal
                return {"status": "incomplete", "reason": f"aligned in-memory but failed to save: {exc}"}
        return {"status": "ok", "axis": axis, "edge": edge, "anchor": anchor_ref, **_moved_lists(moved, detail),
                "locked_skipped": locked_skipped}

    @_with_frame
    @_fail_closed
    def distribute_footprints(pcb_path: str, refs: list[str], axis: str, detail: bool = False) -> dict:
        """Evenly space a batch of footprints (by the centre of the hull of courtyard and pads,
        not the footprint origin) along an axis
        between the first and last ref in the list (used as anchors).
        Takes references and works on relative offsets (no coordinates are passed in); it
        reports local positions, relative to origin_mm (see frame in the result).

        The answer lists the first 20 footprints under moved with moved_count (and moved_truncated when more were
        moved); detail=true lists every one. locked_skipped is never cut.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """

        if axis not in ("x", "y"):
            return {"status": "error", "reason": "axis must be 'x' or 'y'"}
        if len(refs) < 3:
            return {"status": "error", "reason": "need at least 3 refs to distribute (first/last are anchors)"}

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=False)
        if refusal is not None:
            return refusal

        # centres of the hull of courtyard and pads, not the footprint origins (issue 228)
        boxes, refused_boxes = _hull_boxes(path, refs)
        if refused_boxes is not None:
            return refused_boxes
        positions: dict[str, tuple[float, float]] = {}
        for ref in refs:
            fp = board.get_footprint(ref)
            if fp is None:
                return {"status": "error", "reason": f"unknown references: {[ref]}"}
            positions[ref] = fp.position

        idx = 0 if axis == "x" else 1
        centre = {r: (boxes[r][0][idx] + boxes[r][0][idx + 2]) / 2 for r in refs}
        start = centre[refs[0]]
        end = centre[refs[-1]]
        step = (end - start) / (len(refs) - 1)

        locked = _locked_references(path)
        locked_skipped = [r for r in refs if r in locked]
        moved: list[dict] = []
        for i, ref in enumerate(refs):
            if ref in locked:
                continue
            cur_x, cur_y = positions[ref]
            new_val = cur_x + (start + step * i - centre[ref]) if axis == "x" else cur_y + (start + step * i - centre[ref])
            new_x = new_val if axis == "x" else cur_x
            new_y = new_val if axis == "y" else cur_y
            board.update_footprint_position(ref, new_x, new_y, None)
            moved.append({"reference": ref, "x": new_x, "y": new_y})

        if moved:
            try:
                write_layer.save_pcb(board, path)
            except Exception as exc:  # noqa: BLE001
                if (refusal := write_layer.as_lock_refusal(exc)) is not None:
                    return refusal
                return {"status": "incomplete", "reason": f"distributed in-memory but failed to save: {exc}"}
        return {"status": "ok", "axis": axis, **_moved_lists(moved, detail), "locked_skipped": locked_skipped}

    @_with_frame
    @_fail_closed
    def snap_footprints_to_grid(pcb_path: str, refs: list[str], grid_mm: float, anchor: str = "outline", detail: bool = False) -> dict:
        """Snap a batch of footprints' positions to the nearest multiple of
        grid_mm, in one call. The grid is anchored at the outline corner, so snapped
        parts are not on the editor's absolute grid unless the corner is.
        anchor chooses the grid (default "outline", the behaviour above): anchor="absolute"
        snaps the PAGE coordinate to a grid whose origin is the grid origin KiCad stores in
        the board (the raw (setup (grid_origin x y)) token, honoured; page (0, 0) when none); a malformed grid_origin refuses in that mode,
        and any other anchor value refuses, writing nothing. The result carries anchor and
        grid_anchor_mm (the page point of the grid's origin; local positions are reported either way).
        Coordinates are BOARD-LOCAL millimetres: local (0,0) is the top-left corner of the
        bounding box of the Edge.Cuts outline (arc bulges and circles included); the
        tool refuses an outline it cannot read exactly (a curve, an arc inside a
        polygon, a legacy arc without a mid point, Edge.Cuts graphics inside a footprint,
        a malformed or unsupported Edge.Cuts item); without any outline coordinates are
        page coordinates.

        The answer lists the first 20 footprints under moved with moved_count (and moved_truncated when more were
        moved); detail=true lists every one. locked_skipped is never cut.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """

        if grid_mm <= 0:
            return {"status": "error", "reason": "grid_mm must be > 0"}
        if anchor not in _SNAP_ANCHORS:
            return {
                "status": "error",
                "reason": f'anchor must be "outline" or "absolute", got {anchor!r}: nothing was moved',
            }

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=True)
        if refusal is not None:
            return refusal

        # page point of the grid's origin: the outline corner (local 0, 0) or the user grid origin
        page_origin = tuple(_read_outline(path)[0]["origin_mm"])
        grid_anchor = list(page_origin)
        if anchor == "absolute":
            try:
                grid_anchor = list(_read_grid_origin(path))
            except ValueError as exc:
                return {"status": "error", "reason": f"{exc}: nothing was moved"}

        def snap(v: float) -> float:
            return round(v / grid_mm) * grid_mm

        def snap_absolute(v: float, axis: int) -> float:
            # _frame_gate cross-checked page_origin against the origin the library applies
            page = v + page_origin[axis]
            snapped = grid_anchor[axis] + round((page - grid_anchor[axis]) / grid_mm) * grid_mm
            return round(snapped - page_origin[axis], 6)

        missing: list[str] = []
        moved: list[dict] = []
        locked = _locked_references(path)
        locked_skipped = [r for r in refs if r in locked]
        for ref in refs:
            fp = board.get_footprint(ref)
            if fp is None:
                missing.append(ref)
                continue
            if ref in locked:
                continue
            x, y = fp.position
            if anchor == "absolute":
                new_x, new_y = snap_absolute(x, 0), snap_absolute(y, 1)
            else:
                new_x, new_y = snap(x), snap(y)
            board.update_footprint_position(ref, new_x, new_y, None)
            moved.append({"reference": ref, "x": new_x, "y": new_y})
        if missing:
            return {"status": "error", "reason": f"unknown references: {missing}"}

        if moved:
            try:
                write_layer.save_pcb(board, path)
            except Exception as exc:  # noqa: BLE001
                if (refusal := write_layer.as_lock_refusal(exc)) is not None:
                    return refusal
                return {"status": "incomplete", "reason": f"snapped in-memory but failed to save: {exc}"}
        return {"status": "ok", "grid_mm": grid_mm, "anchor": anchor, "grid_anchor_mm": grid_anchor, **_moved_lists(moved, detail),
                "locked_skipped": locked_skipped}

    @_fail_closed
    def pack_footprints_densely(pcb_path: str, refs: list[str], min_clearance_mm: float = 0.2) -> dict:
        """Tighten spacing between an already-aligned row/column of
        footprints toward min_clearance_mm, without violating clearance.
        Verifies the result with pcb_check_collisions before returning
        (evidence-gated -- if the packed result still collides, that's
        reported, not silently accepted).

        Assumes refs are given in the order they should be packed along
        whichever axis they're already predominantly separated on (inferred
        from their current positions, not re-specified) -- pack along the
        axis with greater spread among the given footprints' centers.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """

        if len(refs) < 2:
            return {"status": "error", "reason": "need at least 2 refs to pack"}

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}

        locked = [r for r in refs if r in _locked_references(path)]
        if locked:
            return {"status": "error", "code": "locked_footprint", "locked": locked,
                    "reason": f"{locked} are locked and packing moves every footprint it is given; "
                              "unlock them with lock_footprints (locked=false) or leave them out: nothing was moved"}

        boxes, refused_boxes = _hull_boxes(path, refs)
        if refused_boxes is not None:
            return refused_boxes

        centers_x = [(boxes[r][0][0] + boxes[r][0][2]) / 2 for r in refs]
        centers_y = [(boxes[r][0][1] + boxes[r][0][3]) / 2 for r in refs]
        axis = "x" if (max(centers_x) - min(centers_x)) >= (max(centers_y) - min(centers_y)) else "y"
        ai_min, ai_max = (0, 2) if axis == "x" else (1, 3)

        ordered = sorted(refs, key=lambda r: (boxes[r][0][ai_min] + boxes[r][0][ai_max]) / 2)

        cursor = boxes[ordered[0]][0][ai_min]
        moved: list[dict] = []
        for ref in ordered:
            bbox = boxes[ref][0]
            size = bbox[ai_max] - bbox[ai_min]
            fp = board.get_footprint(ref)
            cur_x, cur_y = fp.position
            shift = cursor - bbox[ai_min]
            if axis == "x":
                new_x, new_y = cur_x + shift, cur_y
            else:
                new_x, new_y = cur_x, cur_y + shift
            board.update_footprint_position(ref, new_x, new_y, None)
            moved.append({"reference": ref, "x": new_x, "y": new_y})
            cursor += size + min_clearance_mm

        try:
            write_layer.save_pcb(board, path)
        except Exception as exc:  # noqa: BLE001
            if (locked := write_layer.as_lock_refusal(exc)) is not None:
                return locked
            return {"status": "incomplete", "reason": f"packed in-memory but failed to save: {exc}"}

        # Verify via the existing, already-tested collision checker rather
        # than trusting our own arithmetic.
        try:
            board2 = write_layer.load_pcb(path)
            collision_result = board2.run_drc(clearance=min_clearance_mm)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "partial",
                "reason": f"packed and saved but post-pack verification failed: {exc}",
                "axis": axis,
                "moved": moved,
            }
        violation_count = getattr(collision_result, "violation_count", None)
        if violation_count is None and hasattr(collision_result, "violations"):
            violation_count = len(collision_result.violations)
        clean = (violation_count or 0) == 0
        return {
            "status": "ok" if clean else "partial",
            "axis": axis,
            "moved": moved,
            "post_pack_violation_count": violation_count,
            "note": None if clean else "packed result still has DRC/placement violations at the requested clearance -- see post_pack_violation_count",
        }

    @_with_frame
    def lock_footprints(pcb_path: str, refs: list[str], locked: bool = True, detail: bool = False) -> dict:
        """Lock (locked=true, the default) or unlock (locked=false) a batch of footprints in one
        call. A locked footprint carries the raw token (locked yes), the one KiCad's editor shows
        as a lock and refuses to drag; place_footprints, align_footprints, distribute_footprints,
        snap_footprints_to_grid, pack_footprints_densely, place_footprints_at_edge,
        propose_autoplacement and propose_placement_optimization never move it, and the two confirm
        tools refuse a proposal for a footprint locked since. Nothing else on the board changes. A reference that is not on the
        board, or carried by two footprints, refuses the whole call and nothing is written.
        Locking an already locked footprint is not an error (it is counted in unchanged_count).
        The answer carries changed_count and unchanged_count; detail=true adds the lists changed and unchanged.

        The reloaded file must hold exactly the requested lock state on those footprints and
        every other node unchanged; otherwise write_not_verified and the board file is untouched.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        if not isinstance(locked, bool):
            return {"status": "error", "code": "invalid_argument", "reason": f"locked must be true or false, got {locked!r}"}
        if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r for r in refs):
            return {"status": "error", "code": "invalid_argument", "reason": "refs must be a non-empty list of reference strings"}
        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            root = write_layer.load(path)
            nodes = [c for c in root.children if not c.is_atom and c.name == "footprint"]
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        from boardproof import raw_geometry

        by_ref: dict[str, list[Any]] = {}
        for node in nodes:
            by_ref.setdefault(raw_geometry._reference(node), []).append(node)
        wanted = list(dict.fromkeys(refs))
        missing = [r for r in wanted if r not in by_ref]
        doubled = [r for r in wanted if len(by_ref.get(r, [])) > 1]
        if missing or doubled:
            return {"status": "error", "code": "unknown_reference" if missing else "ambiguous_reference",
                    "reason": (f"no footprint with the reference(s) {missing}" if missing else
                               f"several footprints carry the reference(s) {doubled}") + ": nothing was written"}
        changing = [r for r in wanted if placement_anchor.is_locked(by_ref[r][0]) != locked]
        unchanged = [r for r in wanted if r not in changing]
        if changing:
            try:
                placement_anchor.write_lock_state(path, {r: locked for r in changing})
            except placement_anchor.NotVerified as exc:
                return _not_verified(exc)
            except Exception as exc:  # noqa: BLE001
                if (refusal := write_layer.as_lock_refusal(exc)) is not None:
                    return refusal
                return {"status": "incomplete", "reason": f"failed to write the lock state: {exc}"}
        return {"status": "ok", "locked": locked, **output_detail.counted("changed", changing, detail),
                **output_detail.counted("unchanged", unchanged, detail)}

    def list_footprint_groups(pcb_path: str) -> dict:
        """List the KiCad groups of a board, read only. Per group: name, uuid, locked, member count,
        the references of its footprints (and which of them are locked), nested group uuids, the
        count of other members by kind, and member uuids that match nothing on the board
        (unresolved). Placement writes keep groups and members; a plain move of one footprint does
        not move the rest of its group."""
        from boardproof import board_groups

        return board_groups.list_groups(pcb_path)

    def propose_group_lock(pcb_path: str, groups: list[str], locked: bool = True, output_dir: str | None = None) -> dict:
        from boardproof import group_lock

        return group_lock.propose_group_lock(pcb_path, groups, locked, output_dir)

    def confirm_group_lock(pcb_path: str, revision_token: str) -> dict:
        from boardproof import group_lock

        result = group_lock.confirm_group_lock(pcb_path, revision_token)
        output = result.get("output_pcb")
        if isinstance(output, str):  # the gate field marks the source and the revision
            return {**result, "pcb_path": output, "source_pcb": pcb_path}
        return result

    def propose_move_group(pcb_path: str, groups: list[str], dx: float, dy: float, output_dir: str | None = None) -> dict:
        from boardproof import group_edit

        return group_edit.propose_move_group(pcb_path, groups, dx, dy, output_dir)

    def confirm_move_group(pcb_path: str, revision_token: str) -> dict:
        from boardproof import group_edit

        return _named_revision(group_edit.confirm_move_group(pcb_path, revision_token), pcb_path)

    def propose_create_group(pcb_path: str, name: str, members: list[str], output_dir: str | None = None) -> dict:
        from boardproof import group_edit

        return group_edit.propose_create_group(pcb_path, name, members, output_dir)

    def confirm_create_group(pcb_path: str, revision_token: str) -> dict:
        from boardproof import group_edit

        return _named_revision(group_edit.confirm_create_group(pcb_path, revision_token), pcb_path)

    @_with_frame
    def place_footprints_at_edge(pcb_path: str, specs: list[dict], detail: bool = False) -> dict:
        """Place a batch of existing footprints at an edge of the board outline, a given distance
        in from it (banana sockets, connectors, mounting parts). Never call this once per
        footprint -- pass the whole batch.

        Each spec: {"reference": str, "edge": "left"|"right"|"top"|"bottom", "offset_mm": float
        (default 0: the gap between the outline edge and the footprint's courtyard on that side),
        "along_mm": float (optional: where the courtyard centre sits along the edge, measured
        from the outline's top-left corner, so from the top on left and right, from the left
        on top and bottom; default the middle of the edge)}. The footprint keeps its rotation
        and layer; rotate it first with place_footprints. The box measured is the hull of the
        courtyard and the pads, read from the raw footprint tokens (the pads' box alone when it
        has no courtyard; reported as box_source).

        Needs an outline that is one axis-aligned rectangle (one gr_rect or four closing
        gr_line, each corner optionally replaced by a quarter-circle gr_arc), else
        outline_not_rectangular or no_outline. The rounded corners and the slots are missing
        material: a courtyard that would overlap one is refused as in_cutout. All or nothing: a spec that is
        invalid, names an unknown, ambiguous or locked footprint, would leave the outline, or
        would overlap another courtyard (collision) refuses the whole call and nothing is
        written; every problem is listed under problems. Coordinates are BOARD-LOCAL
        millimetres: local (0,0) is the top-left corner of the Edge.Cuts bounding box.

        A footprint on the board with neither courtyard nor pad (a logo) is listed under not_measured and left out of the collision check; that check says nothing about it.

        The result reports each footprint's position and gap read back from the reloaded file;
        the reload must hold exactly the intended positions and nothing else changed, else
        write_not_verified and the board file is untouched.

        The answer lists the first 20 footprints under moved with moved_count (and moved_truncated when more were
        moved); detail=true lists every one. locked_skipped is never cut.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        if not isinstance(specs, list) or not specs:
            return {"status": "error", "code": "invalid_argument", "reason": "specs must be a non-empty list"}
        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            root = write_layer.load(path)
            not_measured: list[str] = []
            parts = placement_anchor.read_parts(root, unmeasured=not_measured)
            outline, problem = placement_anchor.outline_rect(root)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "code": "unreadable_geometry", "reason": f"{exc}: nothing was moved"}
        if problem is not None:
            return {"status": "error", "code": "outline_not_rectangular", "reason": f"{problem}: nothing was moved"}
        if outline is None:
            return {"status": "error", "code": "no_outline",
                    "reason": "the board has no Edge.Cuts outline to anchor to (propose_board_outline makes one): nothing was moved"}
        new_at, info, problems = placement_anchor.plan_edge_placement(parts, outline.box, specs, outline.cutouts)
        if problems:
            return {"status": "error", "code": problems[0]["code"], "problems": problems,
                    "reason": f"{len(problems)} problem(s), nothing was moved"}

        def gaps(reloaded: Any, before: list[placement_anchor.Part]) -> list[str]:
            after = {p.reference: p for p in placement_anchor.read_parts(reloaded, unmeasured=[])}
            out = []
            for ref, facts in info.items():
                gap = placement_anchor.gap_to_edge(after[ref].box, outline.box, facts["edge"])
                if abs(gap - facts["offset_mm"]) > 1e-5:
                    out.append(f"{ref} is {gap:.6f} mm from the {facts['edge']} edge, intended {facts['offset_mm']}")
                if not placement_anchor.inside(after[ref].box, outline.box):
                    out.append(f"{ref} lies outside the outline")
            return out

        try:
            placement_anchor.write_positions(path, new_at, extra=gaps)
        except placement_anchor.NotVerified as exc:
            return _not_verified(exc)
        except Exception as exc:  # noqa: BLE001
            if (refusal := write_layer.as_lock_refusal(exc)) is not None:
                return refusal
            return {"status": "incomplete", "reason": f"failed to write the positions: {exc}"}
        try:
            reloaded = {p.reference: p for p in placement_anchor.read_parts(write_layer.load(path), unmeasured=[])}
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"written, but the file could not be read back: {exc}"}
        ox, oy = outline.origin
        moved = []
        for ref, facts in info.items():
            part = reloaded[ref]
            moved.append({
                "reference": ref, "x": round(part.at[0] - ox, 6), "y": round(part.at[1] - oy, 6),
                "edge": facts["edge"], "offset_mm": facts["offset_mm"],
                "gap_mm": round(placement_anchor.gap_to_edge(part.box, outline.box, facts["edge"]), 6),
                "courtyard_mm": [round(part.box[0] - ox, 6), round(part.box[1] - oy, 6),
                                 round(part.box[2] - ox, 6), round(part.box[3] - oy, 6)],
                "box_source": part.box_source,
            })
        return {"status": "ok", **_moved_lists(moved, detail), **({"not_measured": sorted(not_measured)} if not_measured else {})}

    @_with_frame
    def propose_autoplacement(
        pcb_path: str, refs: list[str] | None = None, edge_clearance_mm: float = 0.5, spacing_mm: float = 2.0
    ) -> dict:
        """Propose an automated placement for a set of footprints (or all
        footprints on the board if refs is omitted). Returns a proposal +
        revision_token -- does NOT write to the board. Call
        confirm_autoplacement(revision_token) to actually apply it.
        Coordinates are BOARD-LOCAL millimetres: local (0,0) is the top-left corner of the
        bounding box of the Edge.Cuts outline (arc bulges and circles included); the
        tool refuses an outline it cannot read exactly (a curve, an arc inside a
        polygon, a legacy arc without a mid point, Edge.Cuts graphics inside a footprint,
        a malformed or unsupported Edge.Cuts item); without any outline coordinates are
        page coordinates and the proposal packs rows starting at (0, 0), 50 mm wide.

        With an outline, the outline must be one axis-aligned rectangle (one gr_rect or four
        closing gr_line, each corner optionally replaced by a quarter-circle gr_arc; anything
        else is refused as outline_not_rectangular, nothing proposed; rounded corners and slots
        are obstacles):
        every proposed courtyard lies inside it, at least edge_clearance_mm (default 0.5) from
        its edge, and at least spacing_mm (default 2) from every other courtyard. Footprints
        that are not in refs stay where they are and are obstacles; a locked footprint is
        never proposed to move (it is listed in locked_skipped) and is an obstacle too. Parts
        that fit nowhere refuse the whole proposal as does_not_fit, naming them. A proposal
        keeps each footprint's rotation.

        Placement quality is a best-effort heuristic (first-fit row packing by courtyard
        size, lowest then leftmost free position, not a real simulated-annealing solver). Always requires
        the checkpoint per docs/ARCHITECTURE.md's human-in-the-loop policy for
        autoplacement output -- never trust this without review. A footprint on the board with neither courtyard nor pad (a logo) is listed under not_measured and left out of the collision check; that check says nothing about it. Naming such a footprint in refs is
        refused as footprint_unmeasurable.
        """

        for name, value in (("edge_clearance_mm", edge_clearance_mm), ("spacing_mm", spacing_mm)):
            if not placement_anchor._finite(value) or value < 0:
                return {"status": "error", "code": "invalid_argument", "reason": f"{name} must be a finite number >= 0, got {value!r}"}
        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=True)
        if refusal is not None:
            return refusal
        try:
            root = write_layer.load(path)
            not_measured = []
            parts = placement_anchor.read_parts(root, unmeasured=not_measured)
            outline, problem = placement_anchor.outline_rect(root)
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "code": "unreadable_geometry", "reason": f"{exc}: nothing was proposed"}
        if problem is not None:
            return {"status": "error", "code": "outline_not_rectangular", "reason": f"{problem}: nothing was proposed"}

        by_ref: dict[str, list[placement_anchor.Part]] = {}
        for part in parts:
            by_ref.setdefault(part.reference, []).append(part)
        target_refs = list(dict.fromkeys(refs)) if refs is not None else [p.reference for p in parts if p.reference]
        if not target_refs:
            return {"status": "error", "reason": "no footprints to place"}
        if named := [r for r in target_refs if r in not_measured]:
            return {"status": "error", "code": "footprint_unmeasurable", "not_measured": sorted(not_measured),
                    "reason": f"{named} have no courtyard and no pad to measure: nothing was proposed"}
        missing = [r for r in target_refs if r not in by_ref]
        if missing:
            return {"status": "error", "reason": f"unknown references: {missing}"}
        doubled = [r for r in target_refs if len(by_ref[r]) > 1]
        if doubled:
            return {"status": "error", "code": "ambiguous_reference",
                    "reason": f"several footprints carry the reference(s) {doubled}: nothing was proposed"}
        locked_skipped = [r for r in target_refs if by_ref[r][0].locked]
        movable = [r for r in target_refs if not by_ref[r][0].locked]
        if not movable:
            return {"status": "error", "code": "all_locked", "locked_skipped": locked_skipped,
                    "reason": "every requested footprint is locked: nothing to place"}

        origin = outline.origin if outline is not None else (0.0, 0.0)

        def local(box: placement_anchor.Box) -> placement_anchor.Box:
            return placement_anchor.shift(box, -origin[0], -origin[1])

        sizes = [(r, by_ref[r][0].box[2] - by_ref[r][0].box[0], by_ref[r][0].box[3] - by_ref[r][0].box[1]) for r in movable]
        if outline is not None:
            x0, y0, x1, y1 = local(outline.box)
            region = (x0 + edge_clearance_mm, y0 + edge_clearance_mm, x1 - edge_clearance_mm, y1 - edge_clearance_mm)
            if region[2] <= region[0] or region[3] <= region[1]:
                return {"status": "error", "code": "does_not_fit", "reason": "edge_clearance_mm leaves no room inside the outline"}
        else:
            region = (0.0, 0.0, max(50.0, *(w for _, w, _ in sizes)), float("inf"))
        moving = set(movable)
        blocked = [(local(p.box), p.sides()) for p in parts if p.reference not in moving]
        if outline is not None:  # slots (issue 132) are missing material on both sides of the board
            blocked += [(local(cut), {"front", "back"}) for cut in outline.cutouts]
        centres, unplaced = placement_anchor.pack(
            sizes, region, blocked, {r: by_ref[r][0].sides() for r in movable}, spacing_mm
        )
        if unplaced:
            return {
                "status": "error", "code": "does_not_fit", "unplaced": unplaced, "locked_skipped": locked_skipped,
                "reason": (f"{unplaced} fit nowhere inside {[round(v, 3) for v in region]} (local mm) with "
                           f"spacing_mm={spacing_mm} around the footprints that stay: nothing was proposed"),
            }

        proposed: dict[str, tuple[float, float, float]] = {}
        new_boxes: dict[str, placement_anchor.Box] = {}
        for ref in movable:
            part = by_ref[ref][0]
            bx, by = (part.box[0] + part.box[2]) / 2 - origin[0], (part.box[1] + part.box[3]) / 2 - origin[1]
            dx, dy = centres[ref][0] - bx, centres[ref][1] - by
            ax, ay = part.at[0] - origin[0] + dx, part.at[1] - origin[1] + dy
            proposed[ref] = (round(ax, 6), round(ay, 6), part.angle)
            new_boxes[ref] = placement_anchor.shift(part.box, round(dx, 6), round(dy, 6))
        # the proposal is measured again on its own boxes before it is offered
        if outline is not None and any(not placement_anchor.inside(local(b), region) for b in new_boxes.values()):
            return {"status": "error", "code": "internal_check_failed", "reason": "a proposed courtyard lies outside the region: nothing was proposed"}
        if placement_anchor.collisions(new_boxes, parts):
            return {"status": "error", "code": "internal_check_failed", "reason": "proposed courtyards overlap: nothing was proposed"}

        _evict_stale_pending_placements()
        token = uuid.uuid4().hex
        _PENDING_PLACEMENTS[token] = (pcb_path, proposed, time.time() + _PENDING_PLACEMENT_TTL_SECONDS)
        return {
            "status": "pending_confirmation",
            "revision_token": token,
            "proposed_positions": {ref: {"x": x, "y": y, "rotation": r} for ref, (x, y, r) in proposed.items()},
            "locked_skipped": locked_skipped,
            "not_measured": sorted(not_measured),
            "inside_outline": outline is not None,
            "region_mm": [round(v, 6) for v in region] if outline is not None else None,
            "note": "heuristic first-fit packing, not a real autoplacer -- review before confirming, per docs/ARCHITECTURE.md's human-in-the-loop policy for autoplacement output",
        }

    def confirm_autoplacement(revision_token: str) -> dict:
        """Apply a previously proposed autoplacement by its revision_token.
        Consumes the token -- a second confirm with the same token fails.
        Takes references and works on relative offsets (no coordinates are passed in); it
        reports local positions, relative to origin_mm (see frame in the result).

        Before writing it measures the proposal again against the board as it is now: a
        footprint that is locked since, has another rotation, no longer fits inside the
        outline or would overlap another courtyard refuses the call (proposal_stale or
        locked_footprint) and the token stays valid. Only each footprint's (at x y) is
        written; the reloaded file must hold exactly those positions and nothing else changed,
        else write_not_verified and the board is untouched.
        A footprint on the board with neither courtyard nor pad (a logo) is listed under not_measured and left out of the collision check; that check says nothing about it.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """

        _evict_stale_pending_placements()
        entry = _PENDING_PLACEMENTS.get(revision_token)  # consumed only once the board is safe
        if entry is None:
            return {"status": "error", "reason": "unknown or expired revision_token"}
        pcb_path, proposed, _ = entry

        path = Path(pcb_path)
        fields = _frame_fields(path)
        try:
            board = write_layer.load_pcb(path)
            refusal = _frame_gate(path, board, strict=True)
            if refusal is not None:
                return refusal
            root = write_layer.load(path)
            not_measured = []
            parts = placement_anchor.read_parts(root, unmeasured=not_measured)
            outline, problem = placement_anchor.outline_rect(root)
            if problem is not None:
                return {"status": "error", "code": "outline_not_rectangular", "reason": f"{problem}: nothing was moved", **fields}
            origin = outline.origin if outline is not None else (0.0, 0.0)
            by_ref = {p.reference: p for p in parts}
            problems: list[str] = []
            targets: dict[str, tuple[float, float]] = {}
            boxes: dict[str, placement_anchor.Box] = {}
            locked_now = False
            for ref, (x, y, rotation) in proposed.items():
                part = by_ref.get(ref)
                if part is None:
                    problems.append(f"{ref} is no longer on the board")
                    continue
                if part.locked:
                    locked_now = True
                    problems.append(f"{ref} is locked now")
                    continue
                if abs(part.angle - rotation) > 1e-9:
                    problems.append(f"{ref} has another rotation than at the proposal")
                    continue
                target = (x + origin[0], y + origin[1])
                targets[ref] = target
                boxes[ref] = placement_anchor.shift(part.box, target[0] - part.at[0], target[1] - part.at[1])
                if outline is not None and not placement_anchor.inside(boxes[ref], outline.box):
                    problems.append(f"{ref} would lie outside the outline")
                if outline is not None and any(placement_anchor.overlap(boxes[ref], cut) for cut in outline.cutouts):
                    problems.append(f"{ref} would overlap a slot of the outline")
            if not problems:
                overlaps = placement_anchor.collisions(boxes, parts)
                if overlaps:
                    problems.append("courtyards would overlap: " + str(overlaps[:5]))
            if problems:
                return {"status": "error", "code": "locked_footprint" if locked_now else "proposal_stale",
                        "reason": "the board changed since the proposal: " + "; ".join(problems) + "; nothing was moved, the token stays valid",
                        **fields}
            write_layer.check_writable(path)  # before the token is spent: closing KiCad makes it usable again
            _PENDING_PLACEMENTS.pop(revision_token, None)
            try:
                placement_anchor.write_positions(path, targets)
            except placement_anchor.NotVerified as exc:
                _PENDING_PLACEMENTS[revision_token] = entry
                return {**_not_verified(exc), **fields}
        except Exception as exc:  # noqa: BLE001
            if (locked := write_layer.as_lock_refusal(exc)) is not None:
                _PENDING_PLACEMENTS[revision_token] = entry  # nothing was written: the token stays valid
                return locked
            return {"status": "incomplete", "reason": f"failed to apply proposal: {exc}", **fields}
        return {"status": "confirmed", "applied": list(proposed.keys()), "not_measured": sorted(not_measured), **fields}

    @_fail_closed
    @_with_frame
    @_error_on_failure
    def propose_placement_optimization(pcb_path: str, seed: int = 0, timeout_seconds: int = 300) -> dict:
        """Propose new poses (position and angle) for the unlocked footprints from an external placer, the
        KiCadRoutingTools place_simple script, run as a subprocess on a throw-away copy of the board under a
        timeout with the given seed (the same board and seed give the same poses). Only the poses are read
        back, from the raw (at x y angle) tokens of the copy's output board; that board is never adopted and
        the board file is not changed. Locked footprints are never moved: they are left out and listed in
        locked_skipped. The placer's own claims of legality are not trusted: before offering a proposal the
        tool measures each proposed courtyard against the outline and the other courtyards and refuses the
        proposal, listing the problems, when one is outside the outline or overlaps another (try another seed).
        Returns a proposal with a revision_token (poses are BOARD-LOCAL millimetres, the frame of
        propose_autoplacement). Call confirm_placement_optimization to apply it, then run pcb_check_placement
        and pcb_run_drc on the board. Unavailable (status error, code placer_unavailable) when the script, the
        pinned KiCadRoutingTools version or numpy, scipy or shapely are missing.
        """
        import tempfile

        from boardproof.routers import kicad_placer_tools
        from boardproof.routers.kicad_routing_tools import KicadRoutingToolsError
        from boardproof.toolsets.routing import _trial_copy

        if isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**31:
            return {"status": "error", "code": "invalid_argument", "reason": f"seed must be an integer from 0 to 2147483647, got {seed!r}"}
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 10 <= timeout_seconds <= 1800:
            return {"status": "error", "code": "invalid_argument", "reason": f"timeout_seconds must be an integer from 10 to 1800, got {timeout_seconds!r}"}
        path = Path(pcb_path)
        if not path.is_file():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        locked_now = _locked_references(path)  # fail closed before the run if the lock state cannot be read
        if (problem := kicad_placer_tools.placer_problem()) is not None:
            return {"status": "error", "code": "placer_unavailable", "reason": problem}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        if (refusal := _frame_gate(path, board, strict=True)) is not None:
            return refusal
        root = write_layer.load(path)
        parts = placement_anchor.read_parts(root)
        outline, outline_problem = placement_anchor.outline_rect(root)
        if outline_problem is not None:
            return {"status": "error", "code": "outline_not_rectangular", "reason": f"{outline_problem}: nothing was proposed"}
        refs = [p.reference for p in parts]
        if not refs:
            return {"status": "error", "reason": "no footprints to place"}
        if len(set(refs)) != len(refs):
            return {"status": "error", "code": "ambiguous_reference",
                    "reason": "several footprints carry the same reference: nothing was proposed"}
        origin = outline.origin if outline is not None else (0.0, 0.0)
        by_ref = {p.reference: p for p in parts}

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            try:
                trial = _trial_copy(path, tmp / "in")
                run = kicad_placer_tools.run_place_simple(trial, tmp / "run", refs, seed=seed, timeout=timeout_seconds)
                delta = kicad_placer_tools.placement_delta(trial, run["output_pcb"])
            except (KicadRoutingToolsError, ValueError) as exc:
                return {"status": "error", "reason": f"place_simple failed: {exc}; nothing was proposed"}
            report = run.get("report") if isinstance(run.get("report"), dict) else {}
        elapsed = run.get("elapsed_s")
        locked_skipped = sorted(r for r in delta if r in locked_now)
        delta = {r: v for r, v in delta.items() if r not in locked_now}
        base = {"seed": seed, "locked_skipped": locked_skipped, "placer": {"script": "place_simple", "elapsed_s": elapsed}}
        if not delta:
            return {"status": "complete", "changed": False, "proposed_positions": {}, **base,
                    "reason": "the placer left every unlocked footprint where it is"
                              + (" (locked footprints were left out)" if locked_skipped else "")}
        new_boxes = {r: placement_anchor.box_at(by_ref[r], (x, y), a) for r, (x, y, a) in delta.items()}
        problems = [f"{r} would lie outside the outline" for r, b in new_boxes.items()
                    if outline is not None and not placement_anchor.inside(b, outline.box)]
        problems += [f"{r} would overlap a slot of the outline" for r, b in new_boxes.items()
                     if outline is not None and any(placement_anchor.overlap(b, cut) for cut in outline.cutouts)]
        problems += [f"courtyards of {c['between']} would overlap" for c in placement_anchor.collisions(new_boxes, parts)]
        if problems:
            return {"status": "error", "code": "placer_result_illegal", "problems": problems, **base,
                    "reason": "the placer's poses fail this tool's own measurement (its own legality claim is not trusted): "
                              + "; ".join(problems[:8]) + "; nothing was proposed, another seed may give other poses"}
        proposed = {r: (round(x - origin[0], 6), round(y - origin[1], 6), round(a, 6)) for r, (x, y, a) in delta.items()}
        _evict_stale_pending_placements()
        token = uuid.uuid4().hex
        _PENDING_PLACEMENTS[token] = (pcb_path, proposed, time.time() + _PENDING_PLACEMENT_TTL_SECONDS)
        reported_illegal = report.get("illegal") if isinstance(report.get("illegal"), list) else []
        return {
            "status": "pending_confirmation",
            "revision_token": token,
            "changed": True,
            "proposed_positions": {r: {"x": x, "y": y, "rotation": a} for r, (x, y, a) in proposed.items()},
            "previous_positions": {r: {"x": round(by_ref[r].at[0] - origin[0], 6), "y": round(by_ref[r].at[1] - origin[1], 6),
                                       "rotation": by_ref[r].angle} for r in proposed},
            "footprints_moved": len(proposed),
            "placer_reported_illegal": reported_illegal[:_MAX_REPORTED_ILLEGAL],
            "placer_reported_illegal_count": len(reported_illegal),
            **base,
            "warning": (
                "A proposal from an external placer (KiCadRoutingTools place_simple); its own legality claim is not "
                "trusted. This tool measured the courtyards against the outline and each other, which is not the "
                "acceptance gate. Call confirm_placement_optimization with this revision_token to apply it, then run "
                "pcb_check_placement and pcb_run_drc. Only a verify_board PASS is a verdict on the board."),
        }

    @_fail_closed
    def confirm_placement_optimization(revision_token: str) -> dict:
        """Apply a previously proposed placement optimization by its revision_token. Consumes the token. Measures
        the proposal again against the board as it is now (a footprint locked since, outside the outline,
        in a slot or overlapping another courtyard refuses the call, the token stays valid), writes each
        footprint's position and angle (pad, text and property angles turn with it) and checks the reloaded
        file, then reports kicad-cli DRC counts before and after as measurements (drc_before, drc_after).
        Those two runs are not the acceptance gate.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from boardproof.toolsets.routing import GATE_NOTE

        _evict_stale_pending_placements()
        entry = _PENDING_PLACEMENTS.get(revision_token)
        if entry is None:
            return {"status": "error", "reason": "unknown or expired revision_token"}
        pcb_path, proposed, _ = entry
        path = Path(pcb_path)
        fields = _frame_fields(path)
        try:
            board = write_layer.load_pcb(path)
            if (refusal := _frame_gate(path, board, strict=True)) is not None:
                return refusal
            root = write_layer.load(path)
            parts = placement_anchor.read_parts(root)
            outline, problem = placement_anchor.outline_rect(root)
            if problem is not None:
                return {"status": "error", "code": "outline_not_rectangular", "reason": f"{problem}: nothing was moved", **fields}
            origin = outline.origin if outline is not None else (0.0, 0.0)
            by_ref = {p.reference: p for p in parts}
            problems: list[str] = []
            targets: dict[str, tuple[float, float, float]] = {}
            boxes: dict[str, placement_anchor.Box] = {}
            locked_refs = False
            for ref, (x, y, rotation) in proposed.items():
                part = by_ref.get(ref)
                if part is None:
                    problems.append(f"{ref} is no longer on the board")
                elif part.locked:
                    locked_refs = True
                    problems.append(f"{ref} is locked now")
                else:
                    targets[ref] = (x + origin[0], y + origin[1], rotation)
                    boxes[ref] = placement_anchor.box_at(part, targets[ref][:2], rotation)
                    if outline is not None and not placement_anchor.inside(boxes[ref], outline.box):
                        problems.append(f"{ref} would lie outside the outline")
                    if outline is not None and any(placement_anchor.overlap(boxes[ref], cut) for cut in outline.cutouts):
                        problems.append(f"{ref} would overlap a slot of the outline")
            if not problems and (overlaps := placement_anchor.collisions(boxes, parts)):
                problems.append("courtyards would overlap: " + str(overlaps[:5]))
            if problems:
                return {"status": "error", "code": "locked_footprint" if locked_refs else "proposal_stale",
                        "reason": "the board changed since the proposal: " + "; ".join(problems) + "; nothing was moved, the token stays valid",
                        **fields}
            write_layer.check_writable(path)  # before the token is spent: closing KiCad makes it usable again
            drc_before = _drc_counts(path)
            _PENDING_PLACEMENTS.pop(revision_token, None)
            try:
                placement_anchor.write_poses(path, targets)
            except placement_anchor.PoseRefused as exc:
                _PENDING_PLACEMENTS[revision_token] = entry
                return {"status": "error", "code": "pose_refused", "problems": exc.problems, "reason": str(exc), **fields}
            except placement_anchor.NotVerified as exc:
                _PENDING_PLACEMENTS[revision_token] = entry
                return {**_not_verified(exc), **fields}
        except Exception as exc:  # noqa: BLE001
            if (locked := write_layer.as_lock_refusal(exc)) is not None:
                _PENDING_PLACEMENTS[revision_token] = entry  # nothing was written: the token stays valid
                return locked
            return {"status": "incomplete", "reason": f"failed to apply proposal: {exc}", **fields}
        return {"status": "confirmed", "applied": list(proposed.keys()), "drc_before": drc_before,
                "drc_after": _drc_counts(path), "note": GATE_NOTE, **fields}

    def propose_remove_graphic_items(pcb_path: str, filters: list[dict]) -> dict:
        """Propose removing a batch of board-level graphic items
        (gr_rect/gr_poly/gr_line/gr_circle/gr_arc/zone) matching any of the given
        filters, in one call. Read-only -- does NOT modify the board. Call
        confirm_remove_graphic_items(pcb_path, revision_token) to apply.

        Built for archived backlog P3-10: no kicad_tools API exists to remove a
        standalone graphic item, which has blocked replacing hand-drawn
        copper rectangles/polygons (an ad-hoc power-plane substitute some
        boards use) with real zones -- the old copper and the new zone end
        up coexisting, which is a real source of DRC clearance errors.

        Each filter in `filters` may combine any of:
          - "layer": exact layer name, e.g. "F.Cu"
          - "net_name": exact net name -- only meaningful for copper-layer
            graphics; a graphic with no net never matches a net_name filter
          - "shape": one of "gr_rect"/"gr_poly"/"gr_line"/"gr_circle"/"gr_arc"/"zone"
          - "bbox": [min_x, min_y, max_x, max_y] in mm -- matches items
            whose own bounding box intersects this region (exact for
            rect/line/poly/circle, approximate for arc)
        An item matches this call if it satisfies every criterion given in
        AT LEAST ONE filter (criteria AND within a filter, filters OR
        across the list). A filter with no criteria at all is refused --
        it would otherwise match every graphic item on the board, including
        the board outline.

        Destructive per docs/ARCHITECTURE.md's human-in-the-loop policy: nothing
        is removed until confirm_remove_graphic_items is called with the
        returned revision_token. A filter set that matches zero items is a
        legitimate outcome -- returns status "complete", removed_count 0 --
        not an error.
        """

        err = _validate_graphic_filters(filters)
        if err:
            return {"status": "error", "reason": err}

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}

        try:
            matched_nodes = _match_graphic_items(board, filters)
            items = [_describe_graphic_node(n, board) for n in matched_nodes]
        except raw_geometry.RawGeometryError as exc:
            return {"status": "incomplete", "reason": f"net of a graphic item unreadable: {exc}"}
        if not matched_nodes:
            return {
                "status": "complete",
                "removed_count": 0,
                "matched_items": [],
                "note": "no graphic items matched the given filters -- nothing to remove",
            }

        if any(not it["uuid"] for it in items):
            # Without a stable per-item identifier we cannot safely
            # re-locate the exact same node at confirm time if the file
            # changes in between -- refuse rather than risk removing the
            # wrong item (or the right item by lucky re-match).
            return {
                "status": "error",
                "reason": "one or more matched graphic items have no uuid -- cannot safely propose removal for them",
            }

        _evict_stale_pending_graphic_removals()
        token = uuid.uuid4().hex
        uuids = [it["uuid"] for it in items]
        _PENDING_GRAPHIC_REMOVALS[token] = (str(path), uuids, time.time() + _PENDING_GRAPHIC_REMOVAL_TTL_SECONDS)
        return {
            "status": "pending_confirmation",
            "revision_token": token,
            "matched_count": len(items),
            "matched_items": items,
            "note": (
                "destructive, irreversible file mutation -- review matched_items, then call "
                "confirm_remove_graphic_items with this revision_token to actually remove them."
            ),
        }

    def confirm_remove_graphic_items(pcb_path: str, revision_token: str) -> dict:
        """Apply a previously proposed graphic-item removal by its
        revision_token. Consumes the token -- a second confirm with the
        same token fails rather than re-applying.

        Verification is non-negotiable for an operation this destructive:
        after saving, the board is reloaded FRESH from disk (never the
        in-memory object this function just mutated and saved) and the
        removal is checked against that fresh parse -- every targeted item
        is confirmed gone, the removed-per-shape counts match exactly what
        was requested, and every OTHER top-level node type's count is
        unchanged (catches the file having been corrupted or a sibling
        node accidentally touched, not just "did anything get removed").
        Trusting an in-memory return value instead of the reloaded file is
        exactly the kicad_tools.schema.symbol.SymbolInstance.to_sexp()
        failure mode documented in docs/ARCHITECTURE.md; this tool re-verifies
        from disk specifically so it can't repeat that mistake.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """

        _evict_stale_pending_graphic_removals()
        entry = _PENDING_GRAPHIC_REMOVALS.pop(revision_token, None)
        if entry is None:
            return {"status": "error", "reason": "unknown or expired revision_token"}
        proposal_path, target_uuids, _expires_at = entry

        if str(Path(pcb_path)) != proposal_path:
            _PENDING_GRAPHIC_REMOVALS[revision_token] = entry  # put back -- caller mismatch, not a decision to discard
            return {"status": "error", "reason": "pcb_path does not match the path this proposal was made for"}

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board before removal: {exc}"}

        before_counts = _top_level_tag_counts(board)
        target_set = set(target_uuids)
        to_remove = [n for n in _iter_top_level_graphics(board) if _graphic_node_uuid(n) in target_set]
        found_uuids = {_graphic_node_uuid(n) for n in to_remove}
        missing = target_set - found_uuids
        if missing:
            return {
                "status": "error",
                "reason": (
                    f"{len(missing)} of the proposed graphic items are no longer present on the board "
                    "(it changed since the proposal was made) -- re-run propose_remove_graphic_items"
                ),
            }

        for node in to_remove:
            if not board._sexp.remove(node):
                return {
                    "status": "error",
                    "reason": "internal error: a matched graphic node could not be removed from the parsed tree -- board left untouched on disk",
                }

        try:
            write_layer.save_pcb(board, path)
        except Exception as exc:  # noqa: BLE001
            if (locked := write_layer.as_lock_refusal(exc)) is not None:
                _PENDING_GRAPHIC_REMOVALS[revision_token] = entry  # nothing was written: the token stays valid
                return locked
            return {"status": "incomplete", "reason": f"removal computed but failed to save: {exc}"}

        # Non-negotiable verification: reload FRESH from disk.
        try:
            reloaded = write_layer.load_pcb(path)
        except Exception as exc:  # noqa: BLE001
            return {
                "status": "error",
                "reason": f"CRITICAL: file saved but failed to re-parse afterward -- board may be corrupted: {exc}",
            }
        after_counts = _top_level_tag_counts(reloaded)

        still_present = {_graphic_node_uuid(n) for n in _iter_top_level_graphics(reloaded)} & target_set
        if still_present:
            return {
                "status": "error",
                "reason": f"CRITICAL: {len(still_present)} targeted graphic item(s) still present after save+reload -- removal did not take effect",
            }

        expected_after = dict(before_counts)
        for tag in _GRAPHIC_SHAPES:
            expected_after[tag] = before_counts.get(tag, 0) - sum(1 for n in to_remove if n.tag == tag)
        unexpected_diffs = {
            tag: {"before": before_counts.get(tag, 0), "after": after_counts.get(tag, 0)}
            for tag in set(before_counts) | set(after_counts)
            if after_counts.get(tag, 0) != expected_after.get(tag, before_counts.get(tag, 0))
        }
        if unexpected_diffs:
            return {
                "status": "error",
                "reason": f"CRITICAL: node counts changed beyond what was requested -- possible corruption: {unexpected_diffs}",
            }

        drc_after: dict[str, Any]
        try:
            from boardproof import kicad_cli

            kicad_cli.ensure_kicad_cli_on_path()
            drc_report = kicad_cli.run_drc(path)
            drc_after = {
                "violation_count": len(drc_report.get("violations", [])),
                "unconnected_count": len(drc_report.get("unconnected_items", [])),
            }
        except Exception as exc:  # noqa: BLE001 - removal already succeeded even if this verify step didn't
            drc_after = {"status": "incomplete", "reason": f"removed but DRC could not run: {exc}"}

        from boardproof.toolsets.routing import GATE_NOTE

        return {
            "status": "confirmed",
            "removed_count": len(to_remove),
            "removed_uuids": sorted(target_set),
            "drc_after": drc_after,
            "note": GATE_NOTE,
        }

    mcp.add_tool(
        place_footprints,
        name="place_footprints",
        description=("Place or move a batch of footprints on a board in one call (add new ones with a library_id, move existing ones by reference; a locked footprint is refused per spec; a move of an existing footprint whose courtyard would overlap a slot or a rounded corner of the outline refuses the whole call as in_cutout, nothing written; a footprint that already overlaps a cut-out is refused for any move whose new courtyard still overlaps one; also refused: outline_arcs_unreadable, slots_unreadable, footprint_unmeasurable; an outer loop of lines and arcs, or a gr_poly, that is neither a plain rectangle nor a rectangle with quarter-circle corner arcs is refused for the whole call, nothing written (outline_arcs_unreadable when it has any arc, such as a bite, a notch or a corner built from several arcs; outline_not_rectangular when it is lines only or a gr_poly, such as an L shape); circles, stadiums and well-formed slots are not refused; any other Edge.Cuts problem (a stray or open item, a slot that touches the outline or another slot, a duplicate outer loop, an item the tool cannot read) is refused as slots_unreadable; a board with no Edge.Cuts at all is not checked). The answer carries counts (placed_count, moved_count, rotation_kept_count) and every error; the optional detail=true adds the lists placed, moved and rotation_kept, which a partial or error answer always carries." + " " + _FRAME_RULE + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        place_footprints_at_edge,
        name="place_footprints_at_edge",
        description=("Place a batch of existing footprints at an edge of the board outline, offset_mm in from it (connectors, banana sockets), optionally along_mm along the edge; the box measured is the hull of the courtyard and the pads (a custom pad counts by its anchor size), from the raw footprint tokens, and the file is read back after the write. Needs an outline that is one axis-aligned rectangle, its corners optionally rounded by quarter arcs. All or nothing: an unknown or locked footprint, a courtyard that would leave the outline, overlap a slot or a rounded corner (in_cutout) or overlap another one refuses the whole call and writes nothing. Rotation and layer are kept." + " " + _NOT_MEASURED_RULE + " " + _FRAME_RULE + " " + _MOVED_RULE + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        lock_footprints,
        name="lock_footprints",
        description=("Lock or unlock a batch of footprints (the raw (locked yes) token KiCad shows as a lock). The placement tools never move a locked footprint: place_footprints, place_footprints_at_edge, align, distribute, snap, pack, propose_autoplacement and propose_placement_optimization skip or refuse it, and confirm_autoplacement and confirm_placement_optimization refuse a proposal for one locked since. Nothing else on the board changes. The answer carries changed_count and unchanged_count; the optional detail=true adds the lists changed and unchanged." + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        list_footprint_groups,
        name="list_footprint_groups",
        description="List the KiCad groups of a board with their members: footprint references (and the locked ones), nested groups, other members by kind, and member uuids that match nothing. Reads the raw file; writes nothing. A plain move of one footprint does not move its group.",
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_group_lock,
        name="propose_group_lock",
        description=(
            "Dry run: for a list of KiCad groups (names or uuids, see list_footprint_groups) list the member footprints whose "
            "lock flag would change and whether the group's own flag changes, for locked true (lock) or false (unlock). "
            "Writes nothing; returns a revision_token for confirm_group_lock. " + GROUP_LOCK_SENTENCE
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        confirm_group_lock,
        name="confirm_group_lock",
        description=(
            "Apply a propose_group_lock proposal onto a new board revision (output_pcb; the source stays untouched), reload it, "
            "check it and only then move it into place. " + GROUP_LOCK_SENTENCE + " A failed check returns ok false and leaves "
            "the source as it was. The revision_token is single use and is refused if the board changed since the proposal. "
            + write_layer.LOCK_SENTENCE
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_move_group,
        name="propose_move_group",
        description=(
            "Dry run: for a list of KiCad groups (names or uuids, see list_footprint_groups) list each member footprint with its "
            "old and new (at x y) for one offset dx, dy in mm. Writes nothing; returns a revision_token for confirm_move_group. "
            + MOVE_GROUP_SENTENCE
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        confirm_move_group,
        name="confirm_move_group",
        description=(
            "Apply a propose_move_group proposal onto a new board revision (output_pcb; the source stays untouched), reload it, "
            "check it and only then move it into place. " + MOVE_GROUP_SENTENCE + " A failed check returns ok false and leaves "
            "the source as it was. The revision_token is single use and is refused if the board changed since the proposal. "
            + write_layer.LOCK_SENTENCE
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_create_group,
        name="propose_create_group",
        description=(
            "Dry run: the new KiCad group that would be added for a name and a list of footprint references. Writes nothing; "
            "returns a revision_token for confirm_create_group. " + CREATE_GROUP_SENTENCE
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        confirm_create_group,
        name="confirm_create_group",
        description=(
            "Apply a propose_create_group proposal onto a new board revision (output_pcb; the source stays untouched), reload it, "
            "check it and only then move it into place. " + CREATE_GROUP_SENTENCE + " A failed check returns ok false and leaves "
            "the source as it was. The revision_token is single use and is refused if the board changed since the proposal. "
            + write_layer.LOCK_SENTENCE
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        move_reference_designators_to_layer,
        name="move_reference_designators_to_layer",
        description=("Move a batch of footprints' reference-designator silkscreen TEXT to a different layer (e.g. F.Fab) in one call -- does not change which side the footprint itself is mounted on." + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        align_footprints,
        name="align_footprints",
        description=("Align a batch of footprints' edges or centers (of the hull of courtyard and pads; a custom pad counts by its anchor size) along a shared X/Y coordinate; locked footprints stay (locked_skipped)." + " " + _RELATIVE_RULE + " " + _MOVED_RULE + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        distribute_footprints,
        name="distribute_footprints",
        description=("Evenly space a batch of footprints along an axis between the first and last given ref, by the centres of the hull of courtyard and pads (not the footprint origins; a custom pad counts by its anchor size); locked footprints stay (locked_skipped)." + " " + _RELATIVE_RULE + " " + _MOVED_RULE + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        snap_footprints_to_grid,
        name="snap_footprints_to_grid",
        description=("Snap a batch of footprints' positions to the nearest grid point in one call; locked footprints stay (locked_skipped)." + " " + _FRAME_RULE + " The grid is anchored at the outline corner, so snapped parts are not on the editor's absolute grid unless the corner is." + ' Optional anchor (default "outline", unchanged): anchor="absolute" snaps the page coordinate to a grid whose origin is the grid origin KiCad stores in the board (setup grid_origin, honoured; page (0, 0) when none); a malformed grid_origin or any other anchor value refuses and writes nothing. The result carries anchor and grid_anchor_mm.' + " " + _MOVED_RULE + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        pack_footprints_densely,
        name="pack_footprints_densely",
        description=("Tighten spacing between an aligned row/column of footprints toward a minimum clearance between the hulls of courtyard and pads (refuses locked footprints; a custom pad counts by its anchor size here), then re-checked with the placement collision checker, which is not a verdict: only verify_board gives one." + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_autoplacement,
        name="propose_autoplacement",
        description="Propose an automated placement for a set of footprints. Returns a proposal + revision_token; does not modify the board. Call confirm_autoplacement to apply." + " " + _FRAME_RULE + " With an outline (one axis-aligned rectangle, its corners optionally rounded by quarter arcs, else outline_not_rectangular; slots and rounded corners are obstacles) every proposed footprint box (the hull of its courtyard and pads; a custom pad counts by its anchor size) lies inside it, edge_clearance_mm (default 0.5) from its edge and spacing_mm (default 2) from other boxes; footprints not in refs and locked ones stay and are obstacles (locked_skipped); parts that fit nowhere refuse the proposal as does_not_fit. Without an outline rows start at local (0,0), 50 mm wide. Rotations are kept." + " " + _NOT_MEASURED_RULE + " Naming such a footprint in refs is refused as footprint_unmeasurable.",
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        confirm_autoplacement,
        name="confirm_autoplacement",
        description=("Apply a previously proposed autoplacement by its revision_token. Consumes the token. Measures the proposal again against the board as it is now (a footprint locked since, rotated, outside the outline or overlapping refuses with the token kept) and writes only each footprint's (at x y), which the reloaded file must hold exactly (else the write is refused and the board file is untouched)." + " " + _NOT_MEASURED_RULE + " " + _RELATIVE_RULE + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_placement_optimization,
        name="propose_placement_optimization",
        description="Propose new poses (position and angle, board-local mm) for the unlocked footprints from an external placer, KiCadRoutingTools place_simple, run as a subprocess on a throw-away copy under a timeout with a fixed seed (the same board and seed give the same poses). Only the poses are read back from the copy's output board, which is never adopted; the board file is not changed. A locked footprint is never moved: it is left out and listed in locked_skipped. The placer's own legality claim is not trusted: a proposal whose courtyards fall outside the outline or overlap each other is refused with the problems listed (another seed may differ). Returns a proposal with a revision_token. Call confirm_placement_optimization to apply it, then run pcb_check_placement and pcb_run_drc. Unavailable (code placer_unavailable) when place_simple, the pinned KiCadRoutingTools version, numpy, scipy or shapely are missing. Only a verify_board PASS is a verdict on a board.",
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        confirm_placement_optimization,
        name="confirm_placement_optimization",
        description=("Apply a previously proposed placement optimization by its revision_token. Consumes the token. Measures the proposal again against the board as it is now (a footprint locked since, outside the outline, in a slot or overlapping another courtyard refuses with the token kept), writes each footprint's position and angle (pad, text and property angles turn with it; the reloaded file must hold exactly that, else the write is refused and the board file is untouched), then reports kicad-cli DRC counts before and after (drc_before, drc_after) as measurements; those two runs are not the acceptance gate. Run pcb_check_placement afterwards. Only a verify_board PASS is a verdict on a board." + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_remove_graphic_items,
        name="propose_remove_graphic_items",
        description="Propose removing a batch of board-level graphic items (gr_rect/gr_poly/gr_line/gr_circle/gr_arc/zone) matching layer/net/shape/bbox filters. Returns a proposal + revision_token; does not modify the board. Call confirm_remove_graphic_items to apply.",
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        confirm_remove_graphic_items,
        name="confirm_remove_graphic_items",
        description=("Apply a previously proposed graphic-item removal by its revision_token. Consumes the token. Re-verifies the result by reloading the saved file fresh before reporting success." + " " + write_layer.LOCK_SENTENCE),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    return [
        "place_footprints",
        "place_footprints_at_edge",
        "lock_footprints",
        "list_footprint_groups",
        "propose_group_lock",
        "confirm_group_lock",
        "propose_move_group",
        "confirm_move_group",
        "propose_create_group",
        "confirm_create_group",
        "move_reference_designators_to_layer",
        "align_footprints",
        "distribute_footprints",
        "snap_footprints_to_grid",
        "pack_footprints_densely",
        "propose_autoplacement",
        "confirm_autoplacement",
        "propose_placement_optimization",
        "confirm_placement_optimization",
        "propose_remove_graphic_items",
        "confirm_remove_graphic_items",
    ]
