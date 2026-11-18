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

from boardproof import _self_check, raw_geometry, write_layer
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
    """The board outline as an axis-aligned rectangle, page frame."""

    box: Box

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
    box: Box  # courtyard (or pad) box, page frame
    box_source: str  # "courtyard" or "pads"

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


def _pad_points(pads: tuple[raw_geometry.PadGeometry, ...]) -> list[Point]:
    out: list[Point] = []
    for pad in pads:
        w, h = pad.size_mm
        for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            out.append(raw_geometry.place(pad.at_mm, pad.angle_deg, (sx * w / 2, sy * h / 2)))
    return out


def _box(points: list[Point]) -> Box:
    return (min(p[0] for p in points), min(p[1] for p in points), max(p[0] for p in points), max(p[1] for p in points))


def read_parts(root: Any) -> list[Part]:
    """Every footprint, in file order, with its raw box. Raises ``RawGeometryError`` naming a
    footprint with no courtyard and no pad, or a token that is not a number."""
    geometry = raw_geometry.read_footprints(root)
    nodes = [c for c in root.children if not c.is_atom and c.name == "footprint"]
    parts = []
    for node, geo in zip(nodes, geometry, strict=True):
        points = _courtyard_points(node, geo.at_mm, geo.angle_deg)
        source = "courtyard"
        if not points:
            points, source = _pad_points(geo.pads), "pads"
        if not points:
            raise RawGeometryError(f"footprint {geo.reference or geo.lib_id or '?'} has no courtyard and no pad to measure")
        parts.append(Part(
            geo.reference, node, geo.at_mm, geo.angle_deg, is_locked(node), geo.side,
            any(p.kind == "thru_hole" for p in geo.pads), _box(points), source,
        ))
    return parts


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
    kinds = [item.kind for item in outline.items]
    if kinds == ["rect"]:
        return Outline((x0, y0, x1, y1)), None
    if kinds == ["line"] * 4:
        sides = set()
        for item in outline.items:
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
            return Outline((x0, y0, x1, y1)), None
    return None, (
        "the outline is not one axis-aligned rectangle (one gr_rect, or four closing gr_line); "
        f"its items are {sorted(set(kinds))}"
    )


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
) -> dict:
    """Load ``path`` twice (the source and a copy to edit), call ``edit(root, parts)`` (it changes
    footprint nodes in place and returns them), save through the write layer, and on the temporary
    sibling refuse unless (a) every top-level node other than the changed footprints equals the
    source and the changed ones equal what was saved, and (b) ``check(reloaded_root, parts)``, the
    tool's own raw re-measurement of the intended facts, returns no complaint. Returns
    ``{"changed": n}``. Raises ``NotVerified`` (the file is untouched) or a write-layer error."""
    source = write_layer.load(path)
    root = write_layer.load(path)
    parts = read_parts(root)
    changed = edit(root, parts)
    if not changed:
        return {"changed": 0}
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
    return {"changed": len(changed)}


def raw_at(root: Any, reference: str) -> Point | None:
    """The page ``(at x y)`` of the footprint with ``reference`` in ``root``, read raw."""
    for part in read_parts(root):
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
        after = {p.reference: p.locked for p in read_parts(reloaded)}
        return [f"{ref} is {'locked' if after.get(ref) else 'unlocked'}, intended {'locked' if want else 'unlocked'}"
                for ref, want in wanted.items() if after.get(ref) != want]

    return save_footprint_edits(path, edit, check)


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
        after = {p.reference: p for p in read_parts(reloaded)}
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

    return save_footprint_edits(path, edit, check)


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


def plan_edge_placement(parts: list[Part], rect: Box, specs: list[dict]) -> tuple[dict[str, tuple[float, float]], dict[str, dict], list[dict]]:
    """``(new_at, info, problems)``: for every valid spec the new page ``(x, y)`` of the footprint
    and its facts; each problem is ``{"reference", "code", "reason"}``. All or nothing: the caller
    writes only when ``problems`` is empty."""
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
