"""Edge anchoring, locked footprints and the inside-the-outline guarantee, on raw tokens
(GitHub issue 133).

Every position here is read from the board's own S-expression tokens (a footprint's ``(at x y
angle)``, the courtyard items it holds, its pads' ``(at)``/``(size)``, the Edge.Cuts items) through
``raw_geometry``, never from a ``kicad_tools`` convenience property (CLAUDE.md hard rule 2). All
boxes are in the page frame (y down); the tools report board-local millimetres, local = page minus
the Edge.Cuts bounding-box corner (``outline_rect(...).origin``).

A footprint is *locked* when it carries ``(locked yes)`` (KiCad 8 and later; the older bare
``locked`` atom is read too). The tools in this module and the placement tools that call it never
move a locked footprint.

Writes go through the write layer's low-level API only: ``write_layer.set_number`` on the
footprint's ``(at)`` and ``write_layer.symbol`` for the ``locked`` token, saved with
``write_layer.save`` whose ``verify`` reloads the temporary sibling and refuses (``NotVerified``)
unless every other node is unchanged and every changed footprint holds exactly the intended token.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from boardproof import _self_check, edge_loops, output_detail, raw_geometry, write_layer
from boardproof.raw_geometry import Point, RawGeometryError

Box = tuple[float, float, float, float]  # (min x, min y, max x, max y), page mm, y down

EDGES = ("left", "right", "top", "bottom")
_EPS = 1e-6


class NotVerified(Exception):
    """The reloaded file is not the intended change; ``mismatches`` name what differs."""

    def __init__(self, reason: str, mismatches: list[str]) -> None:
        super().__init__(reason)
        self.reason = reason
        self.mismatches = mismatches


@dataclass(frozen=True)
class Outline:
    """The board outline as an axis-aligned rectangle, page frame. ``cutouts`` are the bounding
    boxes of the slots (closed Edge.Cuts loops inside it, issue 132): material is missing there."""

    box: Box
    cutouts: tuple[Box, ...] = ()

    @property
    def origin(self) -> Point:
        return (self.box[0], self.box[1])


@dataclass
class Part:
    """One footprint as read from its raw tokens."""

    reference: str
    node: Any
    at: Point  # page frame, as stored
    angle: float  # as stored
    locked: bool
    side: str  # "front" or "back"
    through_hole: bool
    box: Box  # hull of the courtyard and the pads (the pads alone without a courtyard), page frame
    box_source: str  # "courtyard" (it has one, the pads are added) or "pads"
    pads: tuple[raw_geometry.PadGeometry, ...] = ()  # raw pads, page frame (the box source where no courtyard exists)

    def sides(self) -> set[str]:
        return {"front", "back"} if self.through_hole else {self.side}


# --------------------------------------------------------------------------- reading


def is_locked(footprint: Any) -> bool:
    """``(locked yes)`` (or ``(locked)``) or the older bare ``locked`` atom on a footprint node."""
    for child in footprint.children:
        if child.is_atom:
            if child.value == "locked":
                return True
        elif child.name == "locked":
            return not child.values or str(child.values[0]) != "no"
    return False


def locked_references(root: Any) -> set[str]:
    """References of the locked footprints (read raw; needs no courtyard or pad)."""
    return {
        raw_geometry._reference(n)
        for n in root.children
        if not n.is_atom and n.name == "footprint" and is_locked(n)
    }


def _num(value: Any) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise RawGeometryError(f"{value!r} is not a finite number")
    return number


def _local_points(node: Any) -> list[Point]:
    """The footprint-local points of one ``fp_*`` courtyard item (its extent is taken after
    the move to the page, so an arc keeps its exact box)."""
    name = node.name
    if name in ("fp_line", "fp_rect"):
        pts = [node.find("start"), node.find("end")]
    elif name == "fp_circle":
        pts = [node.find("center"), node.find("end")]
    elif name == "fp_arc":
        pts = [node.find("start"), node.find("mid"), node.find("end")]
    elif name == "fp_poly":
        poly = node.find("pts")
        pts = [xy for xy in (poly.children if poly is not None else []) if not xy.is_atom and xy.name == "xy"]
    else:
        return []
    out = []
    for p in pts:
        if p is None or len(p.values) < 2:
            raise RawGeometryError(f"({name} ...) has a point without x and y")
        out.append((_num(p.values[0]), _num(p.values[1])))
    return out


def _courtyard_points(fp: Any, at: Point, angle: float) -> list[Point]:
    out: list[Point] = []
    for node in fp.children:
        if node.is_atom or not (node.name or "").startswith("fp_"):
            continue
        layer = node.find("layer")
        if layer is None or not layer.values or layer.values[0] not in ("F.CrtYd", "B.CrtYd"):
            continue
        local = _local_points(node)
        if not local:
            continue
        page = [raw_geometry.place(at, angle, p) for p in local]
        if node.name == "fp_circle":
            cx, cy = page[0]
            r = math.dist(local[0], local[1])
            out += [(cx - r, cy - r), (cx + r, cy + r)]
        elif node.name == "fp_arc":
            extent = raw_geometry.arc_extent(*page)
            out += extent if extent is not None else page
        else:
            out += page
    return out


def _pad_corners(pad: raw_geometry.PadGeometry) -> list[Point]:
    """Pad-local corners of the pad's anchor box and, for a custom pad, its primitives' box (issue 233)."""
    w, h = pad.size_mm
    boxes = [(-w / 2, -h / 2, w / 2, h / 2)] + ([pad.custom_box] if pad.custom_box else [])
    return [c for x0, y0, x1, y1 in boxes for c in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]


def _pad_points(pads: tuple[raw_geometry.PadGeometry, ...]) -> list[Point]:
    out: list[Point] = []
    for pad in pads:
        out += [raw_geometry.place(pad.at_mm, pad.angle_deg, c) for c in _pad_corners(pad)]
    return out


def _box(points: list[Point]) -> Box:
    return (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))


def read_parts(root: Any, only: set[str] | None = None, unmeasured: list[str] | None = None) -> list[Part]:
    """Every footprint (or only those whose reference is in ``only``), in file order, with its raw
    box. Raises ``RawGeometryError`` naming a measured footprint with no courtyard and no pad, or a
    token that is not a number; a footprint left out by ``only`` is not measured. With a list passed
    as ``unmeasured`` a footprint with no courtyard and no pad is left out of the result and its name
    appended to that list instead (it is absent from every box check, never a pass of one)."""
    geometry = raw_geometry.read_footprints(root, only)
    nodes = [c for c in root.children if not c.is_atom and c.name == "footprint"]
    if only is not None:
        nodes = [n for n in nodes if raw_geometry._reference(n) in only]
    parts = []
    for node, geo in zip(nodes, geometry, strict=True):
        # the hull of the courtyard and the pads: an adopted footprint can have pads outside its courtyard (issue 228)
        points = _courtyard_points(node, geo.at_mm, geo.angle_deg)
        source = "courtyard" if points else "pads"
        points = points + _pad_points(geo.pads)
        if not points:
            name = geo.reference or f"lib_id {geo.lib_id or '?'}"  # never a bare id that could equal a reference
            if unmeasured is None:
                raise RawGeometryError(f"footprint {name} has no courtyard and no pad to measure")
            unmeasured.append(name)
            continue
        parts.append(Part(
            geo.reference, node, geo.at_mm, geo.angle_deg, is_locked(node), geo.side,
            any(p.kind == "thru_hole" for p in geo.pads), _box(points), source, geo.pads,
        ))
    return parts


def box_at(part: Part, at: Point, angle: float) -> Box:
    """The part's box if its footprint stood at page ``at`` with ``angle``, from the same raw
    courtyard items and pads as ``read_parts`` (their hull). Pad angles are stored
    board-absolute, so a pad turns with the footprint by the difference of the angles."""
    out: list[Point] = _courtyard_points(part.node, at, angle) if part.box_source == "courtyard" else []
    cos_sin = raw_geometry._cos_sin(part.angle)  # inverse of place(): page offset -> footprint-local
    for pad in part.pads:
        dx, dy = pad.at_mm[0] - part.at[0], pad.at_mm[1] - part.at[1]
        local = (dx * cos_sin[0] - dy * cos_sin[1], dx * cos_sin[1] + dy * cos_sin[0])
        centre = raw_geometry.place(at, angle, local)
        out += [raw_geometry.place(centre, pad.angle_deg + (angle - part.angle), c) for c in _pad_corners(pad)]
    return _box(out)


def outline_rect(root: Any) -> tuple[Outline | None, str | None]:
    """``(outline, problem)``. ``outline`` is the Edge.Cuts rectangle when the board's outline is
    exactly one axis-aligned rectangle (one ``gr_rect``, or four axis-aligned ``gr_line`` that close
    it); ``None`` with ``problem`` None when there is no outline at all; ``None`` with a reason
    otherwise (a polygon, an arc or circle, a footprint on Edge.Cuts, a rectangle turned off the axes,
    an open or doubled chain)."""
    outline = raw_geometry.read_board_outline(root)
    if outline.bbox_mm is None and not outline.items and not outline.problems:
        return None, None
    if outline.problems:
        return None, "Edge.Cuts is not read exactly: " + "; ".join(outline.problems)
    if any(item.footprint_uuid is not None for item in outline.items):
        return None, "a footprint carries Edge.Cuts items"
    x0, y0, x1, y1 = outline.bbox_mm  # type: ignore[misc]
    items = list(outline.items)
    cutouts: tuple[Box, ...] = ()
    if len(items) > 1:
        # Slots (issue 132): closed loops strictly inside the outer loop. The rectangle test is made on
        # the outer loop's own items; the slots are reported as cutouts, never as part of the outline.
        parts = edge_loops.classify(root)
        if parts.outer is not None and parts.slots and not parts.problems and all(parts.outer.uuids):
            keep = set(parts.outer.uuids)
            outer_items = [item for item in items if item.uuid in keep]
            if len(outer_items) == len(parts.outer.uuids):
                items = outer_items
                x0, y0, x1, y1 = parts.outer.box
                cutouts = tuple(slot.box for slot in parts.slots)
    found = _rect_cutouts(items, (x0, y0, x1, y1))
    if found is not None:
        return Outline((x0, y0, x1, y1), tuple(found) + cutouts), None
    return None, (
        "the outline is not one axis-aligned rectangle (one gr_rect, or four closing gr_line, "
        "optionally with each corner replaced by a quarter-circle gr_arc); "
        f"its items are {sorted({item.kind for item in items})}"
    )


def _rect_cutouts(items: list[Any], box: Box) -> list[Box] | None:
    """The loop's own items (``gr_rect``, four closing ``gr_line``, or such a rectangle with quarter-arc
    corners) read as an axis-aligned rectangle with box ``box``: the boxes of the material its corner
    arcs cut away (none for a plain rectangle); None when it is not one."""
    x0, y0, x1, y1 = box
    kinds = [item.kind for item in items]
    if kinds == ["rect"]:
        return []
    if kinds == ["line"] * 4:
        sides = set()
        for item in items:
            (ax, ay), (bx, by) = item.points
            if abs(ax - bx) < _EPS and abs(min(ay, by) - y0) < _EPS and abs(max(ay, by) - y1) < _EPS:
                if abs(ax - x0) < _EPS:
                    sides.add("left")
                elif abs(ax - x1) < _EPS:
                    sides.add("right")
            elif abs(ay - by) < _EPS and abs(min(ax, bx) - x0) < _EPS and abs(max(ax, bx) - x1) < _EPS:
                if abs(ay - y0) < _EPS:
                    sides.add("top")
                elif abs(ay - y1) < _EPS:
                    sides.add("bottom")
        if sides == set(EDGES):
            return []
    if "arc" in kinds:
        return _rounded_corner_cutouts(items, box)
    return None


def _strips(radius: float) -> int:
    """Strips per corner: the cut-out is covered from outside, never under-covered, and a strip
    reaches at most about 0.4 mm past the arc (for a radius up to 32 mm)."""
    return min(64, max(8, math.ceil(2 * radius)))


_ARC_TOL = 1e-4  # mm; board_outline rounds the arc points to 6 decimals


def _circle_centre(a: Point, m: Point, b: Point) -> Point | None:
    (x1, y1), (x2, y2), (x3, y3) = a, m, b
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-12:
        return None
    s1, s2, s3 = x1 * x1 + y1 * y1, x2 * x2 + y2 * y2, x3 * x3 + y3 * y3
    return ((s1 * (y2 - y3) + s2 * (y3 - y1) + s3 * (y1 - y2)) / d,
            (s1 * (x3 - x2) + s2 * (x1 - x3) + s3 * (x2 - x1)) / d)


def _rounded_corner_cutouts(items: list[Any], box: Box) -> list[Box] | None:
    """The outer loop is an axis-aligned rectangle whose corners are replaced by quarter arcs:
    ``gr_line`` items on the box sides and at most one ``gr_arc`` per corner, each read from its raw
    ``(start)``, ``(mid)``, ``(end)``. The loop is already known to be one closed chain, so lines on
    the sides plus one corner arc each make exactly the rounded rectangle. Returns the material the
    arcs cut away, as boxes that cover it (strips of the square between the arc and the corner), or
    None when anything else is in the loop: a circle, a half or three-quarter arc, an arc not at a
    corner, an arc turned inward, a radius over half the shorter side, a line off the sides."""
    x0, y0, x1, y1 = box
    half = min(x1 - x0, y1 - y0) / 2
    seen: set[tuple[int, int]] = set()
    cutouts: list[Box] = []
    for item in items:
        if item.kind == "line":
            (ax, ay), (bx, by) = item.points
            on_x = abs(ax - bx) < _EPS and min(abs(ax - x0), abs(ax - x1)) < _EPS
            on_y = abs(ay - by) < _EPS and min(abs(ay - y0), abs(ay - y1)) < _EPS
            if not (on_x or on_y):
                return None
            continue
        if item.kind != "arc":
            return None
        a, m, b = item.points
        centre = _circle_centre(a, m, b)
        if centre is None:
            return None
        cx, cy = centre
        radius = math.dist(centre, a)
        if radius <= _ARC_TOL or radius > half + _ARC_TOL:
            return None
        # a quarter arc: its end points lie on the horizontal and the vertical through the centre,
        # its mid point in the quadrant they span, the corner they span is a corner of the box
        horizontal = [p for p in (a, b) if abs(p[1] - cy) < _ARC_TOL and abs(abs(p[0] - cx) - radius) < _ARC_TOL]
        vertical = [p for p in (a, b) if abs(p[0] - cx) < _ARC_TOL and abs(abs(p[1] - cy) - radius) < _ARC_TOL]
        if len(horizontal) != 1 or len(vertical) != 1 or abs(math.dist(centre, m) - radius) > _ARC_TOL:
            return None
        sx = 1 if horizontal[0][0] > cx else -1
        sy = 1 if vertical[0][1] > cy else -1
        if (m[0] - cx) * sx <= _ARC_TOL or (m[1] - cy) * sy <= _ARC_TOL:
            return None
        corner = (cx + sx * radius, cy + sy * radius)
        if min(abs(corner[0] - x0), abs(corner[0] - x1)) > _ARC_TOL or min(abs(corner[1] - y0), abs(corner[1] - y1)) > _ARC_TOL:
            return None
        if (sx, sy) in seen:
            return None
        seen.add((sx, sy))
        n = _strips(radius)
        for k in range(n):
            # strip edges at equal steps of the angle, dense where the arc is steep
            u0, u1 = radius * math.sin(math.pi / 2 * k / n), radius * math.sin(math.pi / 2 * (k + 1) / n)
            v0 = radius * math.cos(math.pi / 2 * (k + 1) / n)  # the arc's lowest point over the strip
            xa, xb = cx + sx * u0, cx + sx * u1
            ya, yb = cy + sy * v0, cy + sy * radius
            cutouts.append((min(xa, xb), min(ya, yb), max(xa, xb), max(ya, yb)))
    return cutouts


class RoundedCornerUnreadable(Exception):
    """The outer loop has an arc that is not a quarter-circle corner of a rectangle."""


class OutlineNotRectangular(Exception):
    """The outer loop is lines only and not one axis-aligned rectangle."""


def _is_stadium(items: list[Any], box: Box) -> bool:
    """Exactly two convex semicircle arcs, each with the box's shorter side as diameter, on opposite
    ends of the box (a circle when the box is square), joined by two lines (none for a circle). Read
    from the raw (start) (mid) (end): the centre is the chord's mid point and sits where the box puts
    it, the arc's mid point lies outward from the box centre along the axis, perpendicular to the chord."""
    arcs = [i for i in items if i.kind == "arc"]
    lines = sum(i.kind == "line" for i in items)
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    d = min(w, h)
    square = abs(w - h) < 1e-3
    if len(arcs) != 2 or lines != (0 if square else 2):
        return False
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    outward = []
    for arc in arcs:
        a, m, b = arc.points
        centre = _circle_centre(a, m, b)
        if centre is None or abs(2 * math.dist(centre, a) - d) > 1e-3:
            return False
        if math.dist(centre, ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)) > 1e-3:
            return False
        u = ((m[0] - centre[0]) / (d / 2), (m[1] - centre[1]) / (d / 2))
        if min(abs(u[0]), abs(u[1])) > 1e-3:  # |u| = 1 by the fitted circle, one component 0
            return False  # the mid point is not straight out along an axis
        if abs((b[0] - a[0]) * u[0] + (b[1] - a[1]) * u[1]) > 1e-3:
            return False  # the chord is not perpendicular to it
        if square:
            want = (cx, cy)
        elif w > h:
            want = (x0 + d / 2, cy) if u[0] < 0 else (x1 - d / 2, cy)
        else:
            want = (cx, y0 + d / 2) if u[1] < 0 else (cx, y1 - d / 2)
        if (not square and abs(u[0] if w > h else u[1]) < 0.5) or math.dist(centre, want) > 1e-3:
            return False
        outward.append(u)
    return abs(outward[0][0] + outward[1][0]) < 1e-3 and abs(outward[0][1] + outward[1][1]) < 1e-3


def _is_plain_rectangle_poly(item: Any) -> bool:
    """A ``gr_poly`` of four axis-aligned corners (a closing repeat of the first point allowed)."""
    pts = list(item.contour.items) if item.contour is not None else []
    if len(pts) == 5 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) != 4 or not all(isinstance(p, tuple) for p in pts):
        return False
    xs, ys = sorted({round(p[0], 6) for p in pts}), sorted({round(p[1], 6) for p in pts})
    return len(xs) == 2 and len(ys) == 2 and len({(round(p[0], 6), round(p[1], 6)) for p in pts}) == 4 and all(
        abs(pts[k][0] - pts[(k + 1) % 4][0]) < _EPS or abs(pts[k][1] - pts[(k + 1) % 4][1]) < _EPS for k in range(4))


def _items_box(items: list[Any]) -> Box | None:
    points = [p for i in items for p in (raw_geometry.extent(i) or ())]
    return _box(points) if points else None


def _outer_slot_boxes(items: list[Any]) -> tuple[Box, ...] | None:
    """The cut-away boxes of an outer loop given by its own items when it is understood (a rectangle
    with or without quarter-arc corners, a circle, a stadium, an axis-aligned rectangular ``gr_poly``);
    None when it is not. Raises ``RoundedCornerUnreadable`` for lines and arcs and
    ``OutlineNotRectangular`` for anything else that is not understood."""
    box = _items_box(items)
    if box is None:
        raise OutlineNotRectangular("the outer loop cannot be measured")
    found = _rect_cutouts(items, box)
    if found is not None:
        return tuple(found)
    if len(items) == 1 and items[0].kind == "circle":
        return ()
    if len(items) == 1 and items[0].kind == "poly" and _is_plain_rectangle_poly(items[0]):
        return ()
    line_arc = all(i.kind in ("line", "arc") for i in items)
    if line_arc and _is_stadium(items, box):
        return ()
    if line_arc and any(i.kind == "arc" for i in items):
        raise RoundedCornerUnreadable(
            "the outer loop has arcs the tool cannot read as quarter-arc corners of a rectangle "
            "(an arc off a quarter circle, a corner built from several arcs, a bite or notch in a side)")
    raise OutlineNotRectangular(
        "the outer loop is not one axis-aligned rectangle (lines only, a polygon or another shape: an L shape, for instance)")


class EdgeCutsUnreadable(Exception):
    """Edge.Cuts has items the tool cannot read as one outer loop with clean slots."""


def cutout_boxes(root: Any) -> tuple[Box, ...]:
    """The boxes of the material the outline is missing: a rectangle's rounded corners and the
    slots. Fail closed: a board with Edge.Cuts whose outer loop is not understood (a rectangle, quarter-arc
    rectangle, circle, stadium or axis-aligned rectangular ``gr_poly``), or whose Edge.Cuts has any other
    problem (a stray or open item, a slot that touches, a missing uuid cannot hide it), raises; only a board
    with no Edge.Cuts at all returns ``()``."""
    outline, _ = outline_rect(root)
    if outline is not None:
        return outline.cutouts
    read = raw_geometry.read_board_outline(root)
    if not read.items and not read.problems:
        return ()  # no Edge.Cuts at all: nothing to measure against, as before
    if read.problems:
        raise EdgeCutsUnreadable("Edge.Cuts is not read exactly: " + "; ".join(read.problems))
    if any(i.footprint_uuid is not None for i in read.items):
        raise EdgeCutsUnreadable("a footprint carries Edge.Cuts items")
    parts = edge_loops.classify(root)
    # the gr_ items in file order are read.items one to one (no problems, no footprint items), so the
    # outer loop's nodes pick their items by position, never by uuid
    nodes = [n for n in root.children if not n.is_atom and n.name in edge_loops.EDGE_KINDS
             and n.find("layer") is not None and n.find("layer").values[:1] == ["Edge.Cuts"]]
    if parts.outer is None or len(nodes) != len(read.items):
        raise EdgeCutsUnreadable("Edge.Cuts has no outer loop that can be read: " + "; ".join(parts.problems))
    position = {id(n): k for k, n in enumerate(nodes)}
    if any(id(n) not in position for n in parts.outer.nodes):
        raise EdgeCutsUnreadable("the outer loop of Edge.Cuts cannot be matched to its items")
    cuts = _outer_slot_boxes([read.items[position[id(n)]] for n in parts.outer.nodes])
    if parts.problems:
        raise EdgeCutsUnreadable("Edge.Cuts has problems besides the outer loop: " + "; ".join(parts.problems))
    return tuple(cuts or ()) + tuple(slot.box for slot in parts.slots)


def overlap(a: Box, b: Box) -> bool:
    """Whether two boxes share area (touching edges do not)."""
    return a[0] < b[2] - _EPS and b[0] < a[2] - _EPS and a[1] < b[3] - _EPS and b[1] < a[3] - _EPS


def inside(box: Box, rect: Box) -> bool:
    return box[0] >= rect[0] - _EPS and box[1] >= rect[1] - _EPS and box[2] <= rect[2] + _EPS and box[3] <= rect[3] + _EPS


def shift(box: Box, dx: float, dy: float) -> Box:
    return (box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy)


def collisions(moved: dict[str, Box], parts: list[Part]) -> list[dict]:
    """Courtyard overlaps involving a part in ``moved`` (reference -> its new box), the other
    parts at their current boxes; a front part and a back part meet only through a through-hole
    one. Each pair is listed once."""
    by_ref = {p.reference: p for p in parts}
    out, seen = [], set()
    for ref, box in moved.items():
        mine = by_ref[ref].sides()
        for other in parts:
            if other.reference == ref or not (mine & other.sides()):
                continue
            other_box = moved.get(other.reference, other.box)
            key = tuple(sorted((ref, other.reference)))
            if key in seen or not overlap(box, other_box):
                continue
            seen.add(key)
            out.append({"between": list(key)})
    return out


COLLISION_NOTE = (
    "collisions lists the courtyard overlaps (hull of courtyard and pads) in which a footprint this call moved takes part, "
    "existed_before marking one that was there before the call; it is a measurement of those footprints only, and a count of 0 says "
    "only that none was measured. A footprint with neither courtyard nor pad is named under collisions_not_measured and is "
    "absent from the check; collision_check not_measured means the check could not run and no count is given."
)


def moved_collisions(before: list[Part], after: list[Part], moved: set[str]) -> list[dict]:
    """Courtyard overlaps in which a footprint of ``moved`` takes part, in ``after``; each with
    ``existed_before``: the pair also overlapped in ``before`` (a footprint new in ``after`` has no before)."""
    now = collisions({p.reference: p.box for p in after if p.reference in moved}, after)
    was = {tuple(c["between"]) for c in collisions({p.reference: p.box for p in before if p.reference in moved}, before)}
    return [{"between": c["between"], "existed_before": tuple(c["between"]) in was} for c in now]


def collision_report(before_root: Any, after_parts: Callable[[list[str]], list[Part]] | Any, moved: set[str]) -> dict:
    """The full collision fields of a call that moved ``moved`` (references), read raw: ``before_root`` is
    the board before and ``after_parts`` the board after (a parsed root) or a callable giving its parts.
    Never raises: a board that cannot be read gives ``collision_check: not_measured`` and a reason."""
    blind: list[str] = []
    try:
        before = read_parts(before_root, unmeasured=[])
        after = after_parts(blind) if callable(after_parts) else read_parts(after_parts, unmeasured=blind)
        found = moved_collisions(before, after, moved)
    except Exception as exc:  # noqa: BLE001 - unknown never reads as no collision
        return {"collision_check": "not_measured", "collision_reason": f"the collision check could not run: {exc}"}
    return {"collision_check": "measured", "collisions": found, **({"collisions_not_measured": sorted(blind)} if blind else {})}


def collision_fields(report: dict, detail: bool) -> dict:
    """``report`` as answer fields: the first 20 collisions with ``collisions_count`` unless ``detail`` (pass
    ``True`` where the collisions are decision data: a partial or refused answer)."""
    out = {k: v for k, v in report.items() if k != "collisions"}
    if "collisions" in report:
        out.update(output_detail.listing("collisions", report["collisions"], detail))
    return out


# --------------------------------------------------------------------------- writing


def _set_at(node: Any, x: float, y: float) -> None:
    at = node.find("at")
    write_layer.set_number(at, 0, x)
    write_layer.set_number(at, 1, y)


def set_locked(node: Any, locked: bool) -> None:
    """Make ``node`` (a footprint) locked or not: ``(locked yes)`` right after the library id, as
    KiCad writes it; an unlocked footprint carries no ``locked`` token."""
    for child in list(node.children):
        if child.is_atom:
            if child.value == "locked":
                node.children.remove(child)
        elif child.name == "locked":
            node.children.remove(child)
    if locked:
        token = write_layer.SExp.list("locked", write_layer.symbol("yes"))
        node.children.insert(1, token)


def _same_except(source: Any, memory: Any, reloaded: Any, changed_before: list[Any], changed_after: list[Any]) -> None:
    differences = _self_check.reload_differences(
        source=source, removed=changed_before, written=changed_after, memory=memory, reloaded=reloaded
    )
    if differences["missing"] or differences["unexpected"]:
        raise NotVerified(
            "the reloaded file is not the intended change: " + _self_check.describe(differences),
            differences["missing"] + differences["unexpected"],
        )


def save_footprint_edits(
    path: Path,
    edit: Callable[[Any, list[Part]], list[Any]],
    check: Callable[[Any, list[Part]], list[str]],
    needed: frozenset[str] = frozenset(),
) -> dict:
    """Load ``path`` twice (the source and a copy to edit), call ``edit(root, parts)`` (it changes
    footprint nodes in place and returns them), save through the write layer, and on the temporary
    sibling refuse unless (a) every top-level node other than the changed footprints equals the
    source and the changed ones equal what was saved, and (b) ``check(reloaded_root, parts)``, the
    tool's own raw re-measurement of the intended facts, returns no complaint. Returns
    ``{"changed": n}``, plus ``"not_measured"`` (names) when footprints with no courtyard and no pad
    stand on the board: they are left out of ``parts`` and of the checks, and the "every other node
    equals the source" proof still covers them. A footprint in ``needed`` with no shape is refused
    (``RawGeometryError``). Raises ``NotVerified`` (the file is untouched) or a write-layer error."""
    source = write_layer.load(path)
    root = write_layer.load(path)
    skipped: list[str] = []
    parts = read_parts(root, unmeasured=skipped)
    if bad := sorted(needed & set(skipped)):
        raise RawGeometryError(f"footprint(s) {bad} have no courtyard and no pad to measure: not changed")
    extra = {"not_measured": sorted(skipped)} if skipped else {}
    changed = edit(root, parts)
    if not changed:
        return {"changed": 0, **extra}
    before = [n for n in source.children if not n.is_atom and n.name == "footprint"]
    index = {id(n): i for i, n in enumerate(c for c in root.children if not c.is_atom and c.name == "footprint")}
    removed = [before[index[id(n)]] for n in changed]

    def verify(temp: Path) -> None:
        reloaded = write_layer.load(temp)
        _same_except(source, root, reloaded, removed, changed)
        complaints = check(reloaded, parts)
        if complaints:
            raise NotVerified("the reloaded file does not hold the intended placement: " + "; ".join(complaints[:5]), complaints)

    write_layer.save(root, path, verify=verify)
    return {"changed": len(changed), **extra}


def raw_at(root: Any, reference: str) -> Point | None:
    """The page ``(at x y)`` of the footprint with ``reference`` in ``root``, read raw."""
    for part in read_parts(root, unmeasured=[]):
        if part.reference == reference:
            return part.at
    return None


def write_lock_state(path: Path, wanted: dict[str, bool]) -> dict:
    """Set the lock of each footprint in ``wanted`` (reference -> locked) and prove it on the
    reload: those footprints hold exactly that state, nothing else on the board changed."""
    def edit(root: Any, parts: list[Part]) -> list[Any]:
        nodes = []
        for part in parts:
            if part.reference in wanted and part.locked != wanted[part.reference]:
                set_locked(part.node, wanted[part.reference])
                nodes.append(part.node)
        return nodes

    def check(reloaded: Any, parts: list[Part]) -> list[str]:
        after = {p.reference: p.locked for p in read_parts(reloaded, unmeasured=[])}
        return [f"{ref} is {'locked' if after.get(ref) else 'unlocked'}, intended {'locked' if want else 'unlocked'}"
                for ref, want in wanted.items() if after.get(ref) != want]

    return save_footprint_edits(path, edit, check, frozenset(wanted))


def write_positions(
    path: Path, targets: dict[str, Point], extra: Callable[[Any, list[Part]], list[str]] | None = None
) -> dict:
    """Set the page ``(at x y)`` of each footprint in ``targets`` (reference -> (x, y)); its angle,
    pads and texts stay as they are. The reload must hold exactly those positions, the same angles
    and the same locked state, and every other footprint where it was."""
    def edit(root: Any, parts: list[Part]) -> list[Any]:
        nodes = []
        for part in parts:
            if part.reference in targets and (abs(targets[part.reference][0] - part.at[0]) > 1e-9
                                              or abs(targets[part.reference][1] - part.at[1]) > 1e-9):
                _set_at(part.node, *targets[part.reference])
                nodes.append(part.node)
        return nodes

    def check(reloaded: Any, parts: list[Part]) -> list[str]:
        complaints = []
        after = {p.reference: p for p in read_parts(reloaded, unmeasured=[])}
        for part in parts:
            now = after.get(part.reference)
            if now is None:
                complaints.append(f"{part.reference} is gone")
                continue
            want = targets.get(part.reference, part.at)
            if abs(now.at[0] - want[0]) > 1e-6 or abs(now.at[1] - want[1]) > 1e-6:
                complaints.append(f"{part.reference} is at {list(now.at)}, intended {list(want)}")
            if now.angle != part.angle or now.locked != part.locked:
                complaints.append(f"{part.reference} changed its angle or lock")
        return complaints + (extra(reloaded, parts) if extra is not None else [])

    return save_footprint_edits(path, edit, check, frozenset(targets))


class PoseRefused(Exception):
    """``write_poses`` refused before writing anything; ``problems`` lists every reason."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _wrap(angle: float) -> float:
    """``angle`` folded into (-180, 180], rounded so a turn by a non-quarter angle stays tidy."""
    a = round(math.fmod(angle, 360.0), 9) + 0.0
    return a - 360.0 if a > 180.0 else (a + 360.0 if a <= -180.0 else a)


def _angle_nodes(fp: Any) -> list[tuple[Any, int]]:
    """Every token of footprint ``fp`` that stores a board-absolute angle, as ``(node, index)``: a
    pad's, a text's or a property's ``(at x y angle)`` (index 2) and a text box's ``(angle a)``
    (index 0). The footprint's own ``(at)`` is not among them. Graphics (fp_line, fp_arc, ...),
    pad shapes and the 3D model carry no angle and stay in the footprint's own frame."""
    out: list[tuple[Any, int]] = []
    for child in fp.children:
        if child.is_atom:
            continue
        if child.name in ("pad", "fp_text", "property"):
            if child.find("at") is not None:
                out.append((child.find("at"), 2))
        elif child.name == "fp_text_box" and child.find("angle") is not None:
            out.append((child.find("angle"), 0))
    return out


def _angle_of(node: Any, index: int) -> float:
    return _num(node.values[index]) if len(node.values) > index else 0.0


def _frame(fp: Any) -> tuple[Any, list[float]]:
    """What a pose write must leave alone: the footprint as nested tuples without its own ``(at)``
    and without every absolute-angle atom, and each such angle relative to the footprint's own."""
    own = _angle_of(fp.find("at"), 2)
    tokens = _angle_nodes(fp)
    skip = {id(n): i for n, i in tokens}

    def strip(node: Any) -> Any:
        if node.is_atom:
            return ("atom", node.value)
        if node is fp.find("at"):
            return ("own-at",)
        drop = skip.get(id(node))
        # an (at x y angle) keeps x, y (children before index 2: a missing angle is not a change);
        # an (angle a) keeps nothing
        return (node.name, tuple(strip(c) for i, c in enumerate(node.children) if drop is None or i < drop))

    return strip(fp), [(_angle_of(n, i) - own) % 360.0 for n, i in tokens]


def _same_angle(a: float, b: float) -> bool:
    d = (a - b) % 360.0
    return min(d, 360.0 - d) < 1e-6


def write_poses(path: Path, targets: dict[str, tuple[float, float, float]]) -> dict:
    """Set the page ``(at x y angle)`` of each footprint in ``targets`` (reference -> (x, y, angle)).

    The footprint turns by ``delta = new angle - old angle``. KiCad stores pad, text and property
    angles board-absolute, so every one of them (and a text box's ``(angle)``) moves by the same
    ``delta`` in the file's own sense, on F.Cu and on B.Cu alike; pad and graphic coordinates are
    footprint-local and stay. A locked, unknown, ambiguous or non-finite target is refused with
    ``PoseRefused`` before anything is written. The reload must hold: every other node unchanged;
    each target at its pose; each pad's page position equal to the old offset from the footprint
    origin turned by ``delta`` (hand-computed, hard rule 2); every pad, text and graphic otherwise
    exactly as before (their angles relative to the footprint unchanged)."""
    frames: dict[str, tuple[Any, list[float]]] = {}

    def edit(root: Any, parts: list[Part]) -> list[Any]:
        problems = []
        for ref, pose in targets.items():
            if len(pose) != 3 or not all(isinstance(v, (int, float)) and math.isfinite(v) for v in pose):
                problems.append(f"{ref}: the pose {pose!r} is not three finite numbers")
            hits = [p for p in parts if p.reference == ref]
            if len(hits) != 1:
                problems.append(f"{ref}: {'no footprint' if not hits else 'more than one footprint'} has this reference")
            elif hits[0].locked:
                problems.append(f"{ref}: the footprint is locked")
        if problems:
            raise PoseRefused(problems)
        nodes = []
        for part in parts:
            if part.reference not in targets:
                continue
            x, y, angle = targets[part.reference]
            turns = not _same_angle(angle, part.angle)
            if not turns and abs(x - part.at[0]) <= 1e-9 and abs(y - part.at[1]) <= 1e-9:
                continue
            frames[part.reference] = _frame(part.node)  # before the edit
            if turns:
                for node, index in _angle_nodes(part.node):
                    write_layer.set_number(node, index, _wrap(_angle_of(node, index) + angle - part.angle))
            at = part.node.find("at")
            write_layer.set_number(at, 0, x)
            write_layer.set_number(at, 1, y)
            if turns:
                write_layer.set_number(at, 2, _wrap(angle))
            nodes.append(part.node)
        return nodes

    def check(reloaded: Any, parts: list[Part]) -> list[str]:
        complaints = []
        after = {p.reference: p for p in read_parts(reloaded, unmeasured=[])}
        for part in parts:
            if part.reference not in frames:
                continue
            now = after.get(part.reference)
            if now is None:
                complaints.append(f"{part.reference} is gone")
                continue
            x, y, angle = targets[part.reference]
            if abs(now.at[0] - x) > 1e-6 or abs(now.at[1] - y) > 1e-6 or not _same_angle(now.angle, angle):
                complaints.append(f"{part.reference} is at {list(now.at)} angle {now.angle}, intended {[x, y, angle]}")
            if now.locked != part.locked:
                complaints.append(f"{part.reference} changed its lock")
            if len(now.pads) != len(part.pads):
                complaints.append(f"{part.reference} changed its pad count")
                continue
            for old, new in zip(part.pads, now.pads, strict=True):
                # the old offset from the origin, turned by delta, from the new origin
                offset = (old.at_mm[0] - part.at[0], old.at_mm[1] - part.at[1])
                want = raw_geometry.place((x, y), angle - part.angle, offset)
                if math.dist(want, new.at_mm) > 1e-6 or math.dist(old.local_mm, new.local_mm) > 1e-9:
                    complaints.append(f"{part.reference} pad {old.number} is at {list(new.at_mm)}, expected {list(want)}")
            node = next(n for n in reloaded.children
                        if not n.is_atom and n.name == "footprint" and raw_geometry._reference(n) == part.reference)
            new_shape, new_rel = _frame(node)
            old_shape, old_rel = frames[part.reference]
            if old_shape != new_shape or len(old_rel) != len(new_rel) or not all(map(_same_angle, old_rel, new_rel)):
                complaints.append(f"{part.reference}: something other than the pose changed inside the footprint")
        return complaints

    return save_footprint_edits(path, edit, check, frozenset(targets))


# --------------------------------------------------------------------------- edge anchoring


def edge_target(box: Box, rect: Box, edge: str, offset: float, along: float | None) -> tuple[float, float]:
    """The shift ``(dx, dy)`` that puts the courtyard ``box`` ``offset`` mm from the ``edge`` of
    ``rect`` and its centre ``along`` mm from the rect's top (left and right edges) or left corner
    (top and bottom edges); ``None``: the middle of the edge."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    if edge == "left":
        want_x = rect[0] + offset + (box[2] - box[0]) / 2
        want_y = (rect[1] + along) if along is not None else (rect[1] + rect[3]) / 2
    elif edge == "right":
        want_x = rect[2] - offset - (box[2] - box[0]) / 2
        want_y = (rect[1] + along) if along is not None else (rect[1] + rect[3]) / 2
    elif edge == "top":
        want_y = rect[1] + offset + (box[3] - box[1]) / 2
        want_x = (rect[0] + along) if along is not None else (rect[0] + rect[2]) / 2
    else:
        want_y = rect[3] - offset - (box[3] - box[1]) / 2
        want_x = (rect[0] + along) if along is not None else (rect[0] + rect[2]) / 2
    return want_x - cx, want_y - cy


def gap_to_edge(box: Box, rect: Box, edge: str) -> float:
    """Distance from the courtyard box to ``edge`` of the rectangle."""
    return {"left": box[0] - rect[0], "right": rect[2] - box[2], "top": box[1] - rect[1], "bottom": rect[3] - box[3]}[edge]


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def plan_edge_placement(
    parts: list[Part], rect: Box, specs: list[dict], cutouts: tuple[Box, ...] = ()
) -> tuple[dict[str, tuple[float, float]], dict[str, dict], list[dict]]:
    """``(new_at, info, problems)``: for every valid spec the new page ``(x, y)`` of the footprint
    and its facts; each problem is ``{"reference", "code", "reason"}``. All or nothing: the caller
    writes only when ``problems`` is empty. ``cutouts`` are the slot boxes of the outline
    (``Outline.cutouts``): a courtyard that would overlap one is the problem ``in_cutout``."""
    by_ref: dict[str, list[Part]] = {}
    for p in parts:
        by_ref.setdefault(p.reference, []).append(p)
    problems: list[dict] = []
    new_at: dict[str, tuple[float, float]] = {}
    boxes: dict[str, Box] = {}
    info: dict[str, dict] = {}
    seen: set[str] = set()

    def bad(ref: Any, code: str, reason: str) -> None:
        problems.append({"reference": ref, "code": code, "reason": reason})

    for spec in specs:
        ref = spec.get("reference") if isinstance(spec, dict) else None
        if not isinstance(ref, str) or not ref:
            bad(ref, "invalid_spec", "each spec needs a 'reference' string")
            continue
        if ref in seen:
            bad(ref, "duplicate_reference", "a reference appears in two specs")
            continue
        seen.add(ref)
        edge, offset, along = spec.get("edge"), spec.get("offset_mm", 0.0), spec.get("along_mm")
        if edge not in EDGES:
            bad(ref, "invalid_spec", f"edge must be one of {list(EDGES)}, got {edge!r}")
            continue
        if not _finite(offset) or offset < 0:
            bad(ref, "invalid_spec", f"offset_mm must be a finite number >= 0, got {offset!r}")
            continue
        if along is not None and not _finite(along):
            bad(ref, "invalid_spec", f"along_mm must be a finite number, got {along!r}")
            continue
        found = by_ref.get(ref, [])
        if not found:
            bad(ref, "unknown_reference", "no footprint with this reference on the board")
            continue
        if len(found) > 1:
            bad(ref, "ambiguous_reference", f"{len(found)} footprints carry this reference")
            continue
        part = found[0]
        if part.locked:
            bad(ref, "locked_footprint", "the footprint is locked; unlock it with lock_footprints (locked=false) first")
            continue
        dx, dy = edge_target(part.box, rect, edge, float(offset), None if along is None else float(along))
        dx, dy = round(dx, 6), round(dy, 6)
        box = shift(part.box, dx, dy)
        if not inside(box, rect):
            bad(ref, "outside_outline", f"at edge {edge}, offset {offset} and along {along} its courtyard "
                                        f"{[round(v, 6) for v in box]} leaves the outline {[round(v, 6) for v in rect]}")
            continue
        if any(overlap(box, cut) for cut in cutouts):
            bad(ref, "in_cutout", f"its courtyard {[round(v, 6) for v in box]} would overlap a slot of the outline")
            continue
        new_at[ref] = (round(part.at[0] + dx, 6), round(part.at[1] + dy, 6))
        boxes[ref] = box
        info[ref] = {"edge": edge, "offset_mm": float(offset), "box": box, "box_source": part.box_source}
    if not problems:
        for pair in collisions(boxes, parts):
            a, b = pair["between"]
            bad(a, "collision", f"its courtyard at the new position overlaps {b}'s")
    return new_at, info, problems


# --------------------------------------------------------------------------- packing inside a region


def pack(
    sizes: list[tuple[str, float, float]],
    region: Box,
    blocked: list[tuple[Box, set[str]]],
    sides: dict[str, set[str]],
    spacing: float,
) -> tuple[dict[str, tuple[float, float]], list[str]]:
    """First-fit packing of boxes of ``(reference, width, height)`` into ``region``, in the given
    order: each box takes the lowest, then leftmost, position (a corner of the region or of a box
    already placed or blocked, plus ``spacing``) where it lies inside the region and keeps
    ``spacing`` mm from every blocked box and every box placed before it that shares a board side.
    Returns ``({reference: (centre x, centre y)}, [references that fit nowhere])``; a box that fits
    nowhere is left out and the rest are still placed."""
    placed: list[tuple[Box, set[str]]] = []
    centres: dict[str, tuple[float, float]] = {}
    unplaced: list[str] = []
    for ref, w, h in sizes:
        mine = sides[ref]
        others = [(b, s) for b, s in blocked + placed if s & mine]
        xs = sorted({region[0], *(b[2] + spacing for b, _ in others)})
        ys = sorted({region[1], *(b[3] + spacing for b, _ in others)})
        found = None
        for y in ys:
            for x in xs:
                box = (x, y, x + w, y + h)
                if not inside(box, region):
                    continue
                grown = (box[0] - spacing, box[1] - spacing, box[2] + spacing, box[3] + spacing)
                if any(overlap(grown, b) for b, _ in others):
                    continue
                found = box
                break
            if found is not None:
                break
        if found is None:
            unplaced.append(ref)
            continue
        placed.append((found, mine))
        centres[ref] = ((found[0] + found[2]) / 2, (found[1] + found[3]) / 2)
    return centres, unplaced
