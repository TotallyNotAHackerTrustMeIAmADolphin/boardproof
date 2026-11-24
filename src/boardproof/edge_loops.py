"""The board's Edge.Cuts as closed loops: one outer outline and the slots (cut-outs) inside it.

Issue 132. Everything here reads RAW tokens (``gr_line``, ``gr_arc`` with its ``mid`` point,
``gr_rect``, ``gr_circle``, ``gr_poly``, CLAUDE.md hard rule 2): no ``kicad_tools`` convenience
property. Measured with kicad-cli 10.0.7 (docs/ARCHITECTURE.md, "Slots and keepouts"): a closed
loop inside the outer outline is a cut-out and passes DRC; a loop that touches or crosses the
outer outline is ``invalid_outline`` ("self-intersecting"); an open chain is ``invalid_outline``
("not a closed shape"). The classification below therefore accepts exactly the shapes KiCad
accepts: ONE outer loop and any number of closed loops strictly inside it that touch neither it
nor each other.

The geometry helpers (``ring_distance``, ``point_in_ring`` ...) are small 2-D functions on point
lists; ``isolation`` uses them to find the existing items a new slot or keepout would hit.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

Point = tuple[float, float]

SNAP = 1e-4  # endpoints closer than this (mm) are the same point
SAMPLING_TOL_MM = 0.005  # every curve is sampled so that its chords lie at most this far inside it
_MAX_STEPS = 2000
EDGE_KINDS = ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_curve")


# --------------------------------------------------------------------------- 2-D geometry


def _cross(o: Point, a: Point, b: Point) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def segments_intersect(a: Point, b: Point, c: Point, d: Point) -> bool:
    """Whether the closed segments ab and cd share a point (touching and collinear overlap count)."""
    d1, d2, d3, d4 = _cross(c, d, a), _cross(c, d, b), _cross(a, b, c), _cross(a, b, d)
    if ((d1 > 0 > d2) or (d1 < 0 < d2)) and ((d3 > 0 > d4) or (d3 < 0 < d4)):
        return True

    def on(p: Point, q: Point, r: Point) -> bool:  # r on segment pq, given collinear
        return min(p[0], q[0]) - 1e-12 <= r[0] <= max(p[0], q[0]) + 1e-12 and min(p[1], q[1]) - 1e-12 <= r[1] <= max(p[1], q[1]) + 1e-12

    return (
        (abs(d1) < 1e-12 and on(c, d, a))
        or (abs(d2) < 1e-12 and on(c, d, b))
        or (abs(d3) < 1e-12 and on(a, b, c))
        or (abs(d4) < 1e-12 and on(a, b, d))
    )


def point_segment_distance(p: Point, a: Point, b: Point) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def point_in_ring(p: Point, ring: list[Point] | tuple[Point, ...]) -> bool:
    """Even-odd test; a point exactly on the boundary may answer either way (callers add margins)."""
    inside = False
    n = len(ring)
    for i in range(n):
        (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % n]
        if (y1 > p[1]) != (y2 > p[1]) and p[0] < (x2 - x1) * (p[1] - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _edges(points: list[Point] | tuple[Point, ...], closed: bool) -> list[tuple[Point, Point]]:
    n = len(points)
    if n == 1:
        return [(points[0], points[0])]
    return [(points[i], points[(i + 1) % n]) for i in range(n if closed else n - 1)]


def shape_distance(a: list[Point], a_closed: bool, b: list[Point], b_closed: bool, *, contain: bool = True) -> float:
    """Distance between two polylines or polygons: 0 when they cross or touch and, with ``contain``
    (the default), when one lies inside the other (a closed ring contains the other shape); else
    the smallest vertex-to-edge gap. ``contain=False`` measures boundary to boundary only (a slot
    inside the outline is that far from its edge)."""
    ea, eb = _edges(a, a_closed), _edges(b, b_closed)
    for p, q in ea:
        for r, s in eb:
            if segments_intersect(p, q, r, s):
                return 0.0
    if contain and a_closed and len(a) >= 3 and any(point_in_ring(p, a) for p in b):
        return 0.0
    if contain and b_closed and len(b) >= 3 and any(point_in_ring(p, b) for p in a):
        return 0.0
    best = math.inf
    for p in a:
        for r, s in eb:
            best = min(best, point_segment_distance(p, r, s))
    for p in b:
        for r, s in ea:
            best = min(best, point_segment_distance(p, r, s))
    return best


def ring_distance(a: list[Point], b: list[Point]) -> float:
    """Ring to ring, 0 when one contains the other."""
    return shape_distance(a, True, b, True)


def boundary_distance(a: list[Point], b: list[Point]) -> float:
    """Ring to ring, boundary to boundary: 0 only when the contours cross or touch."""
    return shape_distance(a, True, b, True, contain=False)


def ring_area(ring: list[Point] | tuple[Point, ...]) -> float:
    """Signed area (positive counter-clockwise on a y-up page; the sign is not used on its own)."""
    return sum(ring[i][0] * ring[(i + 1) % len(ring)][1] - ring[(i + 1) % len(ring)][0] * ring[i][1] for i in range(len(ring))) / 2


def ring_is_simple(ring: list[Point]) -> bool:
    """No two non-adjacent edges of the ring meet, and no vertex repeats."""
    n = len(ring)
    if n < 3 or len({(round(x, 9), round(y, 9)) for x, y in ring}) != n:
        return False
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue
            if segments_intersect(ring[i], ring[(i + 1) % n], ring[j], ring[(j + 1) % n]):
                return False
    return True


def bbox(points: list[Point] | tuple[Point, ...]) -> tuple[float, float, float, float]:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def box_ring(box: tuple[float, float, float, float]) -> list[Point]:
    return [(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])]


# --------------------------------------------------------------------------- raw readers


def _xy(node: Any, tag: str) -> Point | None:
    child = node.find(tag)
    if child is None or len(child.values) < 2:
        return None
    try:
        x, y = float(child.values[0]), float(child.values[1])
    except (TypeError, ValueError):
        return None
    return (x, y) if math.isfinite(x) and math.isfinite(y) else None


def _layer(node: Any) -> str:
    child = node.find("layer")
    return str(child.values[0]) if child is not None and child.values else ""


def _uuid(node: Any) -> str:
    child = node.find("uuid")
    return str(child.values[0]) if child is not None and child.values else ""


def _steps(radius: float, sweep: float) -> int:
    """Chords for an arc of ``sweep`` radians so that the sagitta stays within ``SAMPLING_TOL_MM``."""
    if radius <= SAMPLING_TOL_MM:
        return 2
    step = 2 * math.acos(1 - SAMPLING_TOL_MM / radius)
    return max(2, min(_MAX_STEPS, math.ceil(sweep / step)))


def arc_points(a: Point, m: Point, b: Point) -> list[Point] | None:
    """Points along the arc a -> m -> b, ends included; None when a, m, b are collinear."""
    d = 2 * (a[0] * (m[1] - b[1]) + m[0] * (b[1] - a[1]) + b[0] * (a[1] - m[1]))
    if abs(d) < 1e-12:
        return None
    ux = ((a[0] ** 2 + a[1] ** 2) * (m[1] - b[1]) + (m[0] ** 2 + m[1] ** 2) * (b[1] - a[1]) + (b[0] ** 2 + b[1] ** 2) * (a[1] - m[1])) / d
    uy = ((a[0] ** 2 + a[1] ** 2) * (b[0] - m[0]) + (m[0] ** 2 + m[1] ** 2) * (a[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (m[0] - a[0])) / d
    radius = math.hypot(a[0] - ux, a[1] - uy)
    start, mid, end = (math.atan2(p[1] - uy, p[0] - ux) for p in (a, m, b))
    tau = 2 * math.pi
    to_mid, to_end = (mid - start) % tau, (end - start) % tau
    sweep = to_end if to_mid <= to_end else to_end - tau  # the way that passes through m
    steps = _steps(radius, abs(sweep))
    pts = [(ux + radius * math.cos(start + sweep * k / steps), uy + radius * math.sin(start + sweep * k / steps)) for k in range(steps + 1)]
    pts[0], pts[-1] = a, b  # the stored end points exactly
    return pts


@dataclass(frozen=True)
class Loop:
    """One closed Edge.Cuts contour: its raw nodes, their uuids and the contour as a polyline."""

    nodes: tuple[Any, ...]
    uuids: tuple[str, ...]
    kinds: tuple[str, ...]
    points: tuple[Point, ...]  # the ring, first point not repeated

    @property
    def box(self) -> tuple[float, float, float, float]:
        return bbox(self.points)

    @property
    def id(self) -> str:
        """Stable name of the loop: the uuid of its first item in file order ('' when it has none)."""
        return self.uuids[0] if self.uuids else ""


@dataclass(frozen=True)
class Edges:
    """What the top-level Edge.Cuts items make: closed ``loops`` and ``problems`` (open chains,
    items this reader does not take). ``problems`` empty means every item sits in a closed loop."""

    loops: tuple[Loop, ...]
    problems: tuple[str, ...]


def _item(node: Any) -> tuple[list[Point] | None, bool, str | None]:
    """``(points, self_closed, problem)`` of one top-level Edge.Cuts item. A line or arc is an
    open piece (``self_closed`` False); a rect, circle or poly is a closed ring by itself."""
    what = f"{node.name} {_uuid(node) or '?'}"
    if node.name == "gr_line":
        a, b = _xy(node, "start"), _xy(node, "end")
        return ([a, b], False, None) if a and b else (None, False, f"{what} has no readable start and end")
    if node.name == "gr_arc":
        a, m, b = _xy(node, "start"), _xy(node, "mid"), _xy(node, "end")
        if not (a and m and b):
            return None, False, f"{what} has no (start ...) (mid ...) (end ...): a legacy arc is not read"
        pts = arc_points(a, m, b)
        return (pts, False, None) if pts else (None, False, f"{what} is a collinear arc")
    if node.name == "gr_rect":
        a, b = _xy(node, "start"), _xy(node, "end")
        if not (a and b):
            return None, True, f"{what} has no readable start and end"
        return [(a[0], a[1]), (b[0], a[1]), (b[0], b[1]), (a[0], b[1])], True, None
    if node.name == "gr_circle":
        c, e = _xy(node, "center"), _xy(node, "end")
        if not (c and e) or math.dist(c, e) <= 0:
            return None, True, f"{what} has no readable center and end"
        r = math.dist(c, e)
        steps = max(8, _steps(r, 2 * math.pi))
        return [(c[0] + r * math.cos(2 * math.pi * k / steps), c[1] + r * math.sin(2 * math.pi * k / steps)) for k in range(steps)], True, None
    if node.name == "gr_poly":
        pts_node = node.find("pts")
        pts = [(float(c.values[0]), float(c.values[1])) for c in (pts_node.children if pts_node is not None else []) if c.name == "xy" and len(c.values) >= 2]
        return (pts, True, None) if len(pts) >= 3 else (None, True, f"{what} has fewer than three points")
    return None, False, f"{what} is not read (only lines, arcs, rectangles, circles and polygons are)"


def _free_ends(pieces: list[tuple[Any, list[Point]]]) -> list[tuple[Point, int]]:
    """The ends (point, piece index) with no end of another piece within SNAP."""
    ends = [(pt, k) for k, (_, pts) in enumerate(pieces) for pt in (pts[0], pts[-1])]
    return [e for e in ends if not any(o[1] != e[1] and abs(e[0][0] - o[0][0]) <= SNAP and abs(e[0][1] - o[0][1]) <= SNAP for o in ends)]


def read_edges(root: Any) -> Edges:
    """The top-level Edge.Cuts items of a parsed board as closed loops. Footprint-embedded
    Edge.Cuts (``fp_*``) are not read here: ``footprint_edge_items`` names them."""
    loops: list[Loop] = []
    problems: list[str] = []
    pieces: list[tuple[Any, list[Point]]] = []
    for node in root.children:
        if node.is_atom or node.name not in EDGE_KINDS or _layer(node) != "Edge.Cuts":
            continue
        points, self_closed, problem = _item(node)
        if problem:
            problems.append(problem)
            continue
        if self_closed:
            ring = list(points)  # type: ignore[arg-type]
            if len(ring) >= 3 and ring_area(ring) != 0:
                loops.append(Loop((node,), (_uuid(node),), (node.name,), tuple(ring)))
            else:
                problems.append(f"{node.name} {_uuid(node) or '?'} has no area")
        else:
            pieces.append((node, list(points)))  # type: ignore[arg-type]

    # chain lines and arcs: an endpoint is shared when two ends are within SNAP
    used = [False] * len(pieces)
    free: list[tuple[Point, int]] | None = None

    def close(p: Point, q: Point) -> bool:
        return abs(p[0] - q[0]) <= SNAP and abs(p[1] - q[1]) <= SNAP

    for first in range(len(pieces)):
        if used[first]:
            continue
        used[first] = True
        chain = [first]
        ring: list[Point] = list(pieces[first][1])
        closed = False
        while True:
            end = ring[-1]
            if close(end, ring[0]) and len(chain) >= 1 and (len(ring) > 2 or len(chain) > 1):
                closed = True
                break
            nxt = None
            for k in range(len(pieces)):
                if used[k]:
                    continue
                pts = pieces[k][1]
                if close(pts[0], end):
                    nxt = (k, pts)
                    break
                if close(pts[-1], end):
                    nxt = (k, pts[::-1])
                    break
            if nxt is None:
                break
            used[nxt[0]] = True
            chain.append(nxt[0])
            ring.extend(nxt[1][1:])
        nodes = tuple(pieces[k][0] for k in chain)
        if closed:
            ring = ring[:-1]  # the closing point repeats the first
            if len(ring) >= 3 and ring_area(ring) != 0:
                loops.append(Loop(nodes, tuple(_uuid(n) for n in nodes), tuple(n.name for n in nodes), tuple(ring)))
            else:
                problems.append("a closed chain of Edge.Cuts items has no area")
        else:
            if free is None:  # on first need, once per call: it is quadratic and a closed outline never needs it
                free = _free_ends(pieces)
            mine = [e for e in free if e[1] in chain]
            gaps = [d for d in (math.dist(a[0], b[0]) for a in mine for b in free if b is not a) if d > SNAP]
            nodes_text = ", ".join(f"{n.name} {_uuid(n) or '?'}" for n in nodes)
            if gaps:
                problems.append(
                    f"an open chain of {nodes_text} does not close: its nearest free end is {min(gaps):.3g} mm from"
                    f" another free end, and the tool joins ends only within {SNAP:g} mm"
                    " (KiCad's own default is 0.01 mm: a gap above that is invalid_outline)")
            else:  # a stray, duplicate, spur or branching item: there is no gap to name
                problems.append(f"an open chain of {nodes_text} does not close (it has no pair of free ends to measure a gap between)")
    return Edges(tuple(loops), tuple(problems))


def footprint_edge_items(root: Any) -> list[str]:
    """References of footprints that carry ``fp_*`` items on Edge.Cuts (KiCad counts them into the outline)."""
    found = []
    for fp in root.children:
        if fp.is_atom or fp.name != "footprint":
            continue
        if any((not c.is_atom) and (c.name or "").startswith("fp_") and _layer(c) == "Edge.Cuts" for c in fp.children):
            ref = ""
            for c in fp.children:
                if c.name == "property" and len(c.values) >= 2 and str(c.values[0]) == "Reference":
                    ref = str(c.values[1])
            found.append(ref or _uuid(fp) or "?")
    return found


@dataclass(frozen=True)
class Outline:
    """``outer`` and its ``slots`` when the Edge.Cuts is exactly that; else ``outer`` may be None
    and ``problems`` says why the structure is not understood."""

    outer: Loop | None
    slots: tuple[Loop, ...]
    problems: tuple[str, ...]


def classify(root: Any) -> Outline:
    """One outer loop (the largest by area) and the closed loops strictly inside it that touch
    neither it nor each other: the structure KiCad accepts as a board with cut-outs. Anything
    else (an open chain, a second loop outside, a crossing, an island inside a slot, a footprint
    on Edge.Cuts) is a problem and no slot operation proceeds."""
    edges = read_edges(root)
    problems = list(edges.problems)
    for ref in footprint_edge_items(root):
        problems.append(f"footprint {ref} carries Edge.Cuts items")
    if not edges.loops:
        return Outline(None, (), tuple(problems or ["the board has no closed Edge.Cuts outline"]))
    ordered = sorted(edges.loops, key=lambda lp: abs(ring_area(list(lp.points))), reverse=True)
    outer, rest = ordered[0], ordered[1:]
    slots: list[Loop] = []
    for loop in rest:
        ring = list(loop.points)
        if boundary_distance(ring, list(outer.points)) <= SNAP or not all(point_in_ring(p, list(outer.points)) for p in ring):
            problems.append(f"Edge.Cuts loop {loop.id or loop.kinds} is not strictly inside the outer outline (it touches, crosses or lies outside it)")
            continue
        if not ring_is_simple(ring):
            problems.append(f"Edge.Cuts loop {loop.id or loop.kinds} crosses itself")
            continue
        slots.append(loop)
    for i, a in enumerate(slots):
        for b in slots[i + 1:]:
            if ring_distance(list(a.points), list(b.points)) <= SNAP:
                problems.append(f"Edge.Cuts loops {a.id} and {b.id} touch, cross or nest")
    if not ring_is_simple(list(outer.points)):
        problems.append("the outer outline crosses itself")
    return Outline(outer, tuple(slots), tuple(problems))
