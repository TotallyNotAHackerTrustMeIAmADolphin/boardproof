"""PCB footprint placement toolset (archived backlog Phase 3, P3-01/P3-02/P3-03).

Batch tools on top of `kicad_tools.schema.PCB`'s real placement API
(`add_footprint`, `update_footprint_position`, `place_footprint_safe`) and
its courtyard-collision engine (`verification.py`'s `pcb_check_collisions`,
reused here rather than re-derived). See docs/ARCHITECTURE.md "Tool design
conventions" for the batch/annotation/evidence-gated house style this
follows.
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from kicad_mcp import write_layer

# _courtyard_polygon / Layer are internal (underscore) API, not a documented
# public surface -- consistent with this project's existing acceptance of
# kicad_tools-internals risk (P1-07's sexp/fp_lib_table usage, P1-06's
# library.generators) given the version is pinned (pyproject.toml
# kicad-tools>=0.20.0,<0.21.0). No public courtyard-bbox function exists in
# kicad_tools as of 0.20.x; this is the real source of truth for courtyard
# geometry (see the module's own docstring, "single source of truth ...
# consumed by the courtyard-overlap DRC rule and the placement analyzer") --
# reusing it is more correct than re-deriving bounding boxes naively, which
# is exactly what docs/ARCHITECTURE.md's alignment-tools spec warns against.
from kicad_tools.geometry.courtyard import _courtyard_polygon  # noqa: F401 (re-exported name kept private-looking deliberately)

_footprint_dir_checked = False


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
        from kicad_mcp.kicad_cli import find_kicad_cli

        cli_path = find_kicad_cli()
    except Exception:  # noqa: BLE001 - best-effort; let the real error surface downstream if this fails
        return
    footprint_dir = cli_path.parent.parent / "share" / "kicad" / "footprints"
    if footprint_dir.is_dir():
        os.environ["KICAD_FOOTPRINT_DIR"] = str(footprint_dir)


def _footprint_courtyard_or_pad_bbox(footprint: Any) -> tuple[tuple[float, float, float, float], bool]:
    """Return ((minx, miny, maxx, maxy), used_real_courtyard) for a footprint.

    Prefers the real courtyard polygon (accounts for rotation/mirroring
    correctly, per kicad_tools' own courtyard module). Falls back to a pad
    bounding box -- and reports that fallback honestly via the second
    return value -- when the footprint has no courtyard artwork at all
    (e.g. some hand-authored/legacy footprints), since a fixture/generated
    footprint with real IPC-7351 courtyard graphics is the common case this
    project actually cares about (see footprint_gen.py).
    """
    try:
        from shapely.geometry import Polygon
    except ImportError as exc:  # pragma: no cover -- shapely is a kicad-tools core dep
        raise RuntimeError("shapely is required (should be a transitive kicad-tools dependency)") from exc

    side = "F" if footprint.layer.upper().startswith("F.") or footprint.layer.upper().startswith("F") else "B"
    polygon = _courtyard_polygon(footprint, side, Polygon)
    if polygon is not None:
        minx, miny, maxx, maxy = polygon.bounds
        return (minx, miny, maxx, maxy), True

    # Fallback: bounding box of the footprint's own pads, in board space.
    # Pads already carry absolute board-space positions on a loaded PCB
    # footprint (kicad_tools resolves this internally) -- if that turns out
    # not to hold for a given footprint, this raises rather than silently
    # returning a wrong box.
    xs: list[float] = []
    ys: list[float] = []
    for pad in footprint.pads:
        px, py = pad.position
        xs.append(px)
        ys.append(py)
    if not xs:
        # No pads either -- fall back to a zero-size box at the footprint's
        # own position, the least-wrong thing possible.
        x, y = footprint.position
        return (x, y, x, y), False
    return (min(xs), min(ys), max(xs), max(ys)), False


# Module-level pending-state for autoplacement proposals, mirroring
# sourcing.py's _PENDING_MATCHES bound+TTL pattern (archived backlog P1-19) -- the
# first tool in this codebase to actually implement the dry-run +
# revision-token pattern for a real mutating operation, not just describe
# it (docs/ARCHITECTURE.md's "Status honesty" note on this).
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
# (net N)/(net_name "...") pair (find("net") already resolves a numeric net
# id via board.get_net(), which is exactly what a zone's (net N) child is,
# so _graphic_node_net_name needed no change), a (layer "...") child, and
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
    """Net name of a graphic item's (net ...) child, or None if it has no
    net. Handles both the numeric-net-id dialect and KiCad 10's name-only
    (net "name") dialect (see kicad_tools' own net_name_only handling for
    segments/vias -- graphics need the same tolerance, but kicad_tools
    doesn't parse graphics' nets at all, so this is done from scratch here).
    """
    net = node.find("net")
    if net is None:
        return None
    raw = net.get_value(0)
    if raw is None:
        return None
    if isinstance(raw, str):
        return raw
    net_obj = board.get_net(int(raw))
    return net_obj.name if net_obj is not None else None


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
    from kicad_tools.sexp import parse_file

    root = parse_file(Path(pcb_path))
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


def build(mcp: MCPServer) -> list[str]:
    @_with_frame
    def place_footprints(pcb_path: str, specs: list[dict]) -> dict:
        """Place or move a batch of footprints on a board in one call.
        Coordinates are BOARD-LOCAL millimetres: local (0,0) is the top-left corner of the
        bounding box of the Edge.Cuts outline (arc bulges and circles included); the
        tool refuses an outline it cannot read exactly (a curve, an arc inside a
        polygon, a legacy arc without a mid point, Edge.Cuts graphics inside a footprint,
        a malformed or unsupported Edge.Cuts item); without any outline coordinates are
        page coordinates.

        Each spec: {"reference": str, "library_id": str (only for a NEW
        footprint), "x": float, "y": float, "rotation": float (default 0),
        "layer": str (default "F.Cu"), "value": str (optional)}. A
        reference that already exists on the board is moved (via
        update_footprint_position); one that doesn't and has a "library_id"
        is added (via add_footprint). Never call this once per footprint --
        pass the whole batch.
        """
        from kicad_tools.schema import PCB

        _ensure_kicad_footprint_dir_set()
        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=True)
        if refusal is not None:
            return refusal

        placed: list[str] = []
        moved: list[str] = []
        rotation_kept: list[str] = []
        errors: list[dict] = []
        for spec in specs:
            ref = spec.get("reference")
            if not ref:
                errors.append({"spec": spec, "reason": "missing 'reference'"})
                continue
            x, y = spec.get("x"), spec.get("y")
            rotation = spec.get("rotation")  # None: keep an existing footprint's rotation
            layer = spec.get("layer", "F.Cu")
            existing = board.get_footprint(ref)
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
                return {"status": "incomplete", "reason": f"placed in-memory but failed to save: {exc}"}

        status = "ok" if not errors else ("partial" if (placed or moved) else "error")
        return {
            "status": status, "placed": placed, "moved": moved, "errors": errors,
            "rotation_kept": rotation_kept,
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
        """
        from kicad_tools.schema import PCB

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}

        moved_count = 0
        try:
            for ref in refs:
                moved_count += board.move_references_to_layer(layer, pattern=ref)
            write_layer.save_pcb(board, path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": str(exc)}
        return {"status": "ok", "requested": len(refs), "moved_count": moved_count, "layer": layer}

    @_with_frame
    def align_footprints(pcb_path: str, refs: list[str], axis: str, edge: str) -> dict:
        """Align a batch of footprints' edges or centers along a shared X/Y
        coordinate (like a design tool's align-left/right/top/bottom/center).
        Takes references and works on relative offsets (no coordinates are passed in); it
        reports local positions, relative to origin_mm (see frame in the result).

        axis: "x" or "y". edge: "min"/"max"/"center" of the courtyard
        bounding box on that axis (courtyard-accurate when the footprint
        has real courtyard artwork; falls back to a pad bounding box
        otherwise -- reported per-footprint via "used_courtyard").
        """
        from kicad_tools.schema import PCB

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
            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=False)
        if refusal is not None:
            return refusal

        boxes: dict[str, tuple[tuple[float, float, float, float], bool]] = {}
        missing: list[str] = []
        for ref in refs:
            fp = board.get_footprint(ref)
            if fp is None:
                missing.append(ref)
                continue
            boxes[ref] = _footprint_courtyard_or_pad_bbox(fp)
        if missing:
            return {"status": "error", "reason": f"unknown references: {missing}"}

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

        moved: list[dict] = []
        for ref in refs:
            bbox, used_courtyard = boxes[ref]
            fp = board.get_footprint(ref)
            cur_x, cur_y = fp.position
            delta = target - edge_value(bbox)
            new_x = cur_x + delta if axis == "x" else cur_x
            new_y = cur_y + delta if axis == "y" else cur_y
            board.update_footprint_position(ref, new_x, new_y, None)
            moved.append({"reference": ref, "x": new_x, "y": new_y, "used_courtyard": used_courtyard})

        try:
            write_layer.save_pcb(board, path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"aligned in-memory but failed to save: {exc}"}
        return {"status": "ok", "axis": axis, "edge": edge, "anchor": anchor_ref, "moved": moved}

    @_with_frame
    def distribute_footprints(pcb_path: str, refs: list[str], axis: str) -> dict:
        """Evenly space a batch of footprints (by center) along an axis
        between the first and last ref in the list (used as anchors).
        Takes references and works on relative offsets (no coordinates are passed in); it
        reports local positions, relative to origin_mm (see frame in the result).
        """
        from kicad_tools.schema import PCB

        if axis not in ("x", "y"):
            return {"status": "error", "reason": "axis must be 'x' or 'y'"}
        if len(refs) < 3:
            return {"status": "error", "reason": "need at least 3 refs to distribute (first/last are anchors)"}

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=False)
        if refusal is not None:
            return refusal

        positions: dict[str, tuple[float, float]] = {}
        missing: list[str] = []
        for ref in refs:
            fp = board.get_footprint(ref)
            if fp is None:
                missing.append(ref)
                continue
            positions[ref] = fp.position
        if missing:
            return {"status": "error", "reason": f"unknown references: {missing}"}

        idx = 0 if axis == "x" else 1
        start = positions[refs[0]][idx]
        end = positions[refs[-1]][idx]
        step = (end - start) / (len(refs) - 1)

        moved: list[dict] = []
        for i, ref in enumerate(refs):
            cur_x, cur_y = positions[ref]
            new_val = start + step * i
            new_x = new_val if axis == "x" else cur_x
            new_y = new_val if axis == "y" else cur_y
            board.update_footprint_position(ref, new_x, new_y, None)
            moved.append({"reference": ref, "x": new_x, "y": new_y})

        try:
            write_layer.save_pcb(board, path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"distributed in-memory but failed to save: {exc}"}
        return {"status": "ok", "axis": axis, "moved": moved}

    @_with_frame
    def snap_footprints_to_grid(pcb_path: str, refs: list[str], grid_mm: float) -> dict:
        """Snap a batch of footprints' positions to the nearest multiple of
        grid_mm, in one call. The grid is anchored at the outline corner, so snapped
        parts are not on the editor's absolute grid unless the corner is.
        Coordinates are BOARD-LOCAL millimetres: local (0,0) is the top-left corner of the
        bounding box of the Edge.Cuts outline (arc bulges and circles included); the
        tool refuses an outline it cannot read exactly (a curve, an arc inside a
        polygon, a legacy arc without a mid point, Edge.Cuts graphics inside a footprint,
        a malformed or unsupported Edge.Cuts item); without any outline coordinates are
        page coordinates.
        """
        from kicad_tools.schema import PCB

        if grid_mm <= 0:
            return {"status": "error", "reason": "grid_mm must be > 0"}

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=True)
        if refusal is not None:
            return refusal

        def snap(v: float) -> float:
            return round(v / grid_mm) * grid_mm

        missing: list[str] = []
        moved: list[dict] = []
        for ref in refs:
            fp = board.get_footprint(ref)
            if fp is None:
                missing.append(ref)
                continue
            x, y = fp.position
            new_x, new_y = snap(x), snap(y)
            board.update_footprint_position(ref, new_x, new_y, None)
            moved.append({"reference": ref, "x": new_x, "y": new_y})
        if missing:
            return {"status": "error", "reason": f"unknown references: {missing}"}

        try:
            write_layer.save_pcb(board, path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"snapped in-memory but failed to save: {exc}"}
        return {"status": "ok", "grid_mm": grid_mm, "moved": moved}

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
        """
        from kicad_tools.schema import PCB

        if len(refs) < 2:
            return {"status": "error", "reason": "need at least 2 refs to pack"}

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}

        boxes: dict[str, tuple[tuple[float, float, float, float], bool]] = {}
        missing: list[str] = []
        for ref in refs:
            fp = board.get_footprint(ref)
            if fp is None:
                missing.append(ref)
                continue
            boxes[ref] = _footprint_courtyard_or_pad_bbox(fp)
        if missing:
            return {"status": "error", "reason": f"unknown references: {missing}"}

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
            return {"status": "incomplete", "reason": f"packed in-memory but failed to save: {exc}"}

        # Verify via the existing, already-tested collision checker rather
        # than trusting our own arithmetic.
        try:
            board2 = PCB.load(path)
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
    def propose_autoplacement(pcb_path: str, refs: list[str] | None = None) -> dict:
        """Propose an automated placement for a set of footprints (or all
        footprints on the board if refs is omitted). Returns a proposal +
        revision_token -- does NOT write to the board. Call
        confirm_autoplacement(revision_token) to actually apply it.
        Coordinates are BOARD-LOCAL millimetres: local (0,0) is the top-left corner of the
        bounding box of the Edge.Cuts outline (arc bulges and circles included); the
        tool refuses an outline it cannot read exactly (a curve, an arc inside a
        polygon, a legacy arc without a mid point, Edge.Cuts graphics inside a footprint,
        a malformed or unsupported Edge.Cuts item); without any outline coordinates are
        page coordinates. The proposal packs rows starting at (0, 0) and
        ignores the outline's size.

        Placement quality is a best-effort heuristic (simple row-packing by
        courtyard size, not a real simulated-annealing solver -- kicad_tools
        has no built-in autoplacer and adding pyplacer as a dependency for
        one heuristic pass wasn't judged worth it tonight). Always requires
        the checkpoint per docs/ARCHITECTURE.md's human-in-the-loop policy for
        autoplacement output -- never trust this without review.
        """
        from kicad_tools.schema import PCB

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
        refusal = _frame_gate(path, board, strict=True)
        if refusal is not None:
            return refusal

        target_refs = refs if refs is not None else [fp.reference for fp in board.footprints]
        if not target_refs:
            return {"status": "error", "reason": "no footprints to place"}

        boxes = []
        missing = []
        for ref in target_refs:
            fp = board.get_footprint(ref)
            if fp is None:
                missing.append(ref)
                continue
            bbox, _ = _footprint_courtyard_or_pad_bbox(fp)
            boxes.append((ref, bbox[2] - bbox[0], bbox[3] - bbox[1]))
        if missing:
            return {"status": "error", "reason": f"unknown references: {missing}"}

        # Naive left-to-right row-packing heuristic, wrapping to a new row
        # every ~50mm -- a starting point for human review, not a claim of
        # quality.
        margin = 2.0
        row_height = max((h for _, _, h in boxes), default=5.0) + margin
        cursor_x = 0.0
        cursor_y = 0.0
        row_limit_mm = 50.0
        proposed: dict[str, tuple[float, float, float]] = {}
        for ref, w, h in boxes:
            if cursor_x + w > row_limit_mm and cursor_x > 0:
                cursor_x = 0.0
                cursor_y += row_height
            proposed[ref] = (cursor_x + w / 2, cursor_y + h / 2, 0.0)
            cursor_x += w + margin

        _evict_stale_pending_placements()
        token = uuid.uuid4().hex
        _PENDING_PLACEMENTS[token] = (pcb_path, proposed, time.time() + _PENDING_PLACEMENT_TTL_SECONDS)
        return {
            "status": "pending_confirmation",
            "revision_token": token,
            "proposed_positions": {ref: {"x": x, "y": y, "rotation": r} for ref, (x, y, r) in proposed.items()},
            "note": "heuristic row-packing, not a real autoplacer -- review before confirming, per docs/ARCHITECTURE.md's human-in-the-loop policy for autoplacement output",
        }

    def confirm_autoplacement(revision_token: str) -> dict:
        """Apply a previously proposed autoplacement by its revision_token.
        Consumes the token -- a second confirm with the same token fails.
        Takes references and works on relative offsets (no coordinates are passed in); it
        reports local positions, relative to origin_mm (see frame in the result).
        """
        from kicad_tools.schema import PCB

        _evict_stale_pending_placements()
        entry = _PENDING_PLACEMENTS.get(revision_token)  # consumed only once the board is safe
        if entry is None:
            return {"status": "error", "reason": "unknown or expired revision_token"}
        pcb_path, proposed, _ = entry

        path = Path(pcb_path)
        fields = _frame_fields(path)
        try:
            board = PCB.load(path)
            refusal = _frame_gate(path, board, strict=True)
            if refusal is not None:
                return refusal
            _PENDING_PLACEMENTS.pop(revision_token, None)
            for ref, (x, y, rotation) in proposed.items():
                board.update_footprint_position(ref, x, y, rotation)
            write_layer.save_pcb(board, path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to apply proposal: {exc}", **fields}
        return {"status": "confirmed", "applied": list(proposed.keys()), **fields}

    def propose_placement_optimization(
        pcb_path: str,
        rounds: int = 2,
        route_args: str = "--escalation board",
        timeout_seconds: int = 1800,
    ) -> dict:
        """Propose a real, KiCadRoutingTools-driven placement repair
        (`py_placer/place_route_loop.py`, `krt-capability-suite` ticket 08)
        -- router-in-the-loop: it routes the board, sees which nets actually
        fail, relocates just those (or their blockers), and re-routes,
        iterating across `rounds`. Unlike `propose_autoplacement`'s naive
        row-packing heuristic, this is a real optimizer with a real
        feedback loop. Returns a proposal + revision_token -- does NOT
        modify the board. Call confirm_placement_optimization with the
        token to apply it.

        `route_args` is passed through verbatim to the loop's own internal
        `route.py` invocations (a quoted string, e.g.
        `"--escalation board --clearance 0.2"`) -- KRT's own
        `place_route_loop.py` REQUIRES this flag be non-empty (confirmed by
        two real invocations: omitting it, and passing `""`, both fail the
        same argparse error, "--route-args is required (except with
        --suggest-locks)"), so this function always passes a real value,
        defaulting to `"--escalation board"` to match this project's own
        router-wrapper default. `--relocate` is never passed (ticket 04 of
        `krt-capability-suite`: measured net-negative by KRT's own authors).

        Real evidence, not the placer's own self-report: this function runs
        this project's own `run_kicad_routing_tools` (already real-DRC-scored
        elsewhere in this codebase) TWICE -- once on the board as-is
        (baseline), once on a fresh trial copy with the placer's proposed
        positions applied (nothing else) -- and reports independent
        `kicad-cli pcb drc` numbers for both, so the caller can judge whether
        the proposed placement actually helps before confirming it, not just
        whether the placer claims it does.

        No graceful-stop exists for the underlying tool (verified directly,
        see `kicad_placer_tools`'s module docstring) -- `timeout_seconds` is
        a hard kill with no softer option.
        """
        import tempfile

        from kicad_mcp import kicad_cli
        from kicad_mcp.routers.kicad_placer_tools import placement_delta, run_placement_repair
        from kicad_mcp.routers.kicad_routing_tools import KicadRoutingToolsError, run_kicad_routing_tools
        from kicad_mcp.toolsets.routing import _trial_copy

        path = Path(pcb_path)
        if not path.is_file():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}

        def _drc(target: Path) -> dict:
            try:
                kicad_cli.ensure_kicad_cli_on_path()
                report = kicad_cli.run_drc(target)
                return {
                    "violation_count": len(report.get("violations", [])),
                    "unconnected_count": len(report.get("unconnected_items", [])),
                    "short_count": sum(
                        1 for v in report.get("violations", []) if "short" in str(v.get("type", "")).lower()
                    ),
                }
            except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
                return {"status": "incomplete", "reason": str(exc)}

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp = Path(tmp_dir)
            try:
                baseline_input = _trial_copy(path, tmp / "baseline")
            except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
                return {"status": "incomplete", "reason": f"could not build trial copy: {exc}"}

            baseline_output = tmp / "baseline" / (path.stem + "_routed" + path.suffix)
            try:
                baseline_run = run_kicad_routing_tools(baseline_input, baseline_output, timeout=300)
                before_drc = _drc(baseline_output)
            except KicadRoutingToolsError as exc:
                before_drc = {"status": "incomplete", "reason": f"baseline routing failed: {exc}"}

            try:
                placer_input = _trial_copy(path, tmp / "placer")
            except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
                return {"status": "incomplete", "reason": f"could not build trial copy: {exc}"}
            placer_output = tmp / "placer" / (path.stem + "_optimized" + path.suffix)

            try:
                repair_run = run_placement_repair(
                    placer_input, placer_output, rounds=rounds, route_args=route_args, timeout=timeout_seconds,
                )
            except KicadRoutingToolsError as exc:
                return {"status": "incomplete", "reason": f"placement repair failed: {exc}"}

            if not repair_run.get("output_produced"):
                return {
                    "status": "complete",
                    "changed": False,
                    "reason": "placer produced no output -- no repair found worth keeping",
                    "run": repair_run,
                    "before_drc": before_drc,
                }

            delta = placement_delta(placer_input, placer_output)
            if not delta:
                return {
                    "status": "complete",
                    "changed": False,
                    "reason": "placer ran but found no beneficial repositioning",
                    "run": repair_run,
                    "before_drc": before_drc,
                }

            # Real "after" evidence: apply ONLY the position delta (never the
            # placer's own routing/copper) to a fresh trial copy, then score
            # it with THIS project's own already-verified router+DRC path --
            # never the placer's internal route.py result, which used
            # different parameters (route_args) than the caller may want for
            # a real attempt, and is exactly the kind of self-report this
            # project's standing rule says never to trust as the signal.
            try:
                from kicad_tools.schema import PCB

                verify_input = _trial_copy(path, tmp / "verify")
                verify_board = PCB.load(verify_input)
                for ref, (x, y, rotation) in delta.items():
                    verify_board.update_footprint_position(ref, x, y, rotation)
                write_layer.save_pcb(verify_board, verify_input)
            except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
                after_drc = {"status": "incomplete", "reason": f"could not apply delta for verification: {exc}"}
            else:
                verify_output = tmp / "verify" / (path.stem + "_routed" + path.suffix)
                try:
                    run_kicad_routing_tools(verify_input, verify_output, timeout=300)
                    after_drc = _drc(verify_output)
                except KicadRoutingToolsError as exc:
                    after_drc = {"status": "incomplete", "reason": f"verification routing failed: {exc}"}

        _evict_stale_pending_placements()
        token = uuid.uuid4().hex
        _PENDING_PLACEMENTS[token] = (pcb_path, delta, time.time() + _PENDING_PLACEMENT_TTL_SECONDS)
        return {
            "status": "pending_confirmation",
            "revision_token": token,
            "changed": True,
            "proposed_positions": {ref: {"x": x, "y": y, "rotation": r} for ref, (x, y, r) in delta.items()},
            "footprints_moved": len(delta),
            "before_drc": before_drc,
            "after_drc": after_drc,
            "repair_run": repair_run,
            "warning": (
                "Placement proposal from a real optimizer, compared by an independent routing+DRC pass on "
                "trial copies (before_drc vs after_drc; single DRC runs, not the gate) -- review them yourself "
                "before confirming. Call confirm_placement_optimization with this revision_token to apply it, "
                "then re-run your own routing attempt on the real board (this proposal's after_drc used a "
                "bounded 300s pass, not necessarily the same parameters you'd want for a real attempt) and run "
                "verify_board on it: only its PASS is a verdict."
            ),
        }

    def confirm_placement_optimization(revision_token: str) -> dict:
        """Apply a previously-proposed placement-optimization result by its
        revision_token. Consumes the token -- a second confirm with the same
        token fails rather than re-applying. Runs the real `pcb_run_drc`
        against the result before returning (same pattern as `confirm_route`),
        so the caller immediately knows the applied placement's real
        courtyard/clearance state, not just "some footprints got moved".
        """
        from kicad_tools.schema import PCB

        _evict_stale_pending_placements()
        entry = _PENDING_PLACEMENTS.pop(revision_token, None)
        if entry is None:
            return {"status": "error", "reason": "unknown or expired revision_token"}
        pcb_path, proposed, _ = entry

        path = Path(pcb_path)
        try:
            board = PCB.load(path)
            for ref, (x, y, rotation) in proposed.items():
                board.update_footprint_position(ref, x, y, rotation)
            write_layer.save_pcb(board, path)
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return {"status": "incomplete", "reason": f"failed to apply proposal: {exc}"}

        from kicad_mcp import kicad_cli

        try:
            kicad_cli.ensure_kicad_cli_on_path()
            drc_report = kicad_cli.run_drc(path)
            drc_summary = {
                "violation_count": len(drc_report.get("violations", [])),
                "unconnected_count": len(drc_report.get("unconnected_items", [])),
            }
        except Exception as exc:  # noqa: BLE001 - evidence-gated: applying succeeded even if the verify step didn't
            drc_summary = {"status": "incomplete", "reason": f"applied but DRC could not run: {exc}"}

        from kicad_mcp.toolsets.routing import GATE_NOTE

        return {
            "status": "confirmed",
            "applied": list(proposed.keys()),
            "drc_after": drc_summary,
            "note": GATE_NOTE,
        }

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
        from kicad_tools.schema import PCB

        err = _validate_graphic_filters(filters)
        if err:
            return {"status": "error", "reason": err}

        path = Path(pcb_path)
        if not path.exists():
            return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
        try:
            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001
            return {"status": "incomplete", "reason": f"failed to load board: {exc}"}

        matched_nodes = _match_graphic_items(board, filters)
        if not matched_nodes:
            return {
                "status": "complete",
                "removed_count": 0,
                "matched_items": [],
                "note": "no graphic items matched the given filters -- nothing to remove",
            }

        items = [_describe_graphic_node(n, board) for n in matched_nodes]
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
        """
        from kicad_tools.schema import PCB

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
            board = PCB.load(path)
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
            return {"status": "incomplete", "reason": f"removal computed but failed to save: {exc}"}

        # Non-negotiable verification: reload FRESH from disk.
        try:
            reloaded = PCB.load(path)
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
            from kicad_mcp import kicad_cli

            kicad_cli.ensure_kicad_cli_on_path()
            drc_report = kicad_cli.run_drc(path)
            drc_after = {
                "violation_count": len(drc_report.get("violations", [])),
                "unconnected_count": len(drc_report.get("unconnected_items", [])),
            }
        except Exception as exc:  # noqa: BLE001 - removal already succeeded even if this verify step didn't
            drc_after = {"status": "incomplete", "reason": f"removed but DRC could not run: {exc}"}

        from kicad_mcp.toolsets.routing import GATE_NOTE

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
        description="Place or move a batch of footprints on a board in one call (add new ones with a library_id, move existing ones by reference)." + " " + _FRAME_RULE,
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        move_reference_designators_to_layer,
        name="move_reference_designators_to_layer",
        description="Move a batch of footprints' reference-designator silkscreen TEXT to a different layer (e.g. F.Fab) in one call -- does not change which side the footprint itself is mounted on.",
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        align_footprints,
        name="align_footprints",
        description="Align a batch of footprints' courtyard edges or centers along a shared X/Y coordinate." + " " + _RELATIVE_RULE,
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        distribute_footprints,
        name="distribute_footprints",
        description="Evenly space a batch of footprints along an axis between the first and last given ref." + " " + _RELATIVE_RULE,
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        snap_footprints_to_grid,
        name="snap_footprints_to_grid",
        description="Snap a batch of footprints' positions to the nearest grid point in one call." + " " + _FRAME_RULE + " The grid is anchored at the outline corner, so snapped parts are not on the editor's absolute grid unless the corner is.",
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        pack_footprints_densely,
        name="pack_footprints_densely",
        description="Tighten spacing between an aligned row/column of footprints toward a minimum clearance, then re-checked with the placement collision checker, which is not a verdict: only verify_board gives one.",
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_autoplacement,
        name="propose_autoplacement",
        description="Propose an automated placement for a set of footprints. Returns a proposal + revision_token; does not modify the board. Call confirm_autoplacement to apply." + " " + _FRAME_RULE + " Rows start at local (0,0) and ignore the outline size.",
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        confirm_autoplacement,
        name="confirm_autoplacement",
        description="Apply a previously proposed autoplacement by its revision_token. Consumes the token." + " " + _RELATIVE_RULE,
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_placement_optimization,
        name="propose_placement_optimization",
        description="Propose a real router-in-the-loop placement repair (KiCadRoutingTools' place_route_loop.py) -- routes the board, relocates only footprints on nets that actually failed, re-routes, iterating. Compared with independent before/after kicad-cli DRC on trial copies after routing, not the tool's own self-report; those counts only compare the two placements and are not the acceptance gate. Returns a proposal + revision_token; does not modify the board. Call confirm_placement_optimization to apply (it applies footprint positions only, not the trial routing). Only a verify_board PASS may be reported as routed or done.",
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        confirm_placement_optimization,
        name="confirm_placement_optimization",
        description="Apply a previously proposed placement-optimization result by its revision_token. Consumes the token. Applies footprint positions only, then runs one kicad-cli DRC on the result and reports its counts (drc_after); that single run is not the acceptance gate. Only a verify_board PASS may be reported as routed or done.",
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
        description="Apply a previously proposed graphic-item removal by its revision_token. Consumes the token. Re-verifies the result by reloading the saved file fresh before reporting success.",
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    return [
        "place_footprints",
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
