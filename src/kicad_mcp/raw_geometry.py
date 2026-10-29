"""Geometry readers on raw tokens: the sanctioned source for position and geometry facts on a
``.kicad_pcb`` (build ticket 43; CLAUDE.md hard rule 2, docs/ARCHITECTURE.md decision 1).

Every value here is evaluated from the board's own S-expression tokens (``(at ...)``,
``(size ...)``, ``(polygon ...)``, the Edge.Cuts ``gr_*``/``fp_*`` items), never from a
``kicad_tools`` convenience property: ``Footprint.position`` and ``Zone.polygon`` are
board-local (page minus the Edge.Cuts bounding-box corner) and ``Pad.position`` is
footprint-local, so each has been mistaken for a page position before (P4-11).

**Frame.** All positions are in the file's own frame, the one KiCad stores and pcbnew reports:
page millimetres, x to the right, **y down**, angles in degrees counter-clockwise as seen on
screen. To compare with ``kicad-cli`` exports, negate y (they are y up) and, for
``--use-drill-file-origin`` and IPC-D-356, subtract the aux origin first
(:func:`read_origins`).

**Footprints and pads.** A footprint's ``(at x y angle)`` is its page position and rotation.
A pad's ``(at x y angle)`` holds x, y in the footprint's own (unrotated) frame, and an angle
that is **board-absolute** (KiCad writes footprint rotation plus the pad's own rotation; an
omitted angle means 0 absolute, not 0 relative). The page position is
``fp + R(fp_angle) * (x, y)`` with ``R`` KiCad's screen-CCW rotation on a y-down axis:
``X = fx + x cos(a) + y sin(a)``, ``Y = fy - x sin(a) + y cos(a)``. Back-side footprints are
stored already mirrored, so the same formula holds. Proven against ``kicad-cli pcb export pos``,
``kicad-cli pcb export ipcd356`` and KiCad's ``pcbnew`` on KiCad's own demo boards
(``tests/test_raw_geometry.py``).

Readers take a path or an already parsed ``kicad_pcb`` root (``write_layer.load``).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Point = tuple[float, float]


class RawGeometryError(ValueError):
    """A token the reader needs is missing or malformed; the message names it."""


@dataclass(frozen=True)
class PadCopper:
    """One copper entry of a pad as stored: ``layer`` is "F.Cu" for the pad's own
    ``(shape)``/``(size)``, else the padstack's ``(layer ...)`` name ("Inner", "B.Cu",
    "In<n>.Cu").

    Not exposed (a note for ticket 44's callers): a roundrect's ``roundrect_rratio``, a
    chamfered rect's ``chamfer_ratio`` and ``(chamfer ...)`` corners (KiCad writes a chamfered
    rect as ``roundrect`` plus those tokens), a trapezoid's ``rect_delta``, a custom pad's
    primitives and the pad offset. ``shape`` and ``size_mm`` are the pad's bounding rectangle
    in its own frame, not its exact outline."""

    layer: str
    shape: str  # circle, rect, oval, trapezoid, roundrect, custom (a chamfered rect is roundrect)
    size_mm: Point  # in the pad's own frame (before its rotation)


_STACK_MODES = ("normal", "front_inner_back", "custom")


def _is_inner(layer: str) -> bool:
    return layer.startswith("In") and layer.endswith(".Cu") and layer[2:-3].isdigit() and int(layer[2:-3]) > 0


@dataclass(frozen=True)
class PadGeometry:
    number: str  # "" for an unnumbered pad (a mounting hole, an NPTH)
    uuid: str
    kind: str  # thru_hole, smd, connect, np_thru_hole
    shape: str  # the pad's own (F.Cu) shape: circle, rect, oval, trapezoid, roundrect, custom
    at_mm: Point  # PAGE frame, y down: the footprint's placement applied
    # The raw (at x y): the footprint's own frame, unrotated and, on a back-side footprint,
    # already mirrored (KiCad stores the flipped footprint).
    local_mm: Point
    angle_deg: float  # BOARD-ABSOLUTE, as stored (footprint rotation included)
    local_angle_deg: float  # relative to the footprint, in [0, 360)
    # The pad's own (size w h), in its own frame (before its rotation). This is KiCad's F.Cu
    # entry, also on a back-side footprint: with a padstack the copper on any other layer can
    # differ, so ask copper_on(layer).
    size_mm: Point
    stack_mode: str = "normal"  # normal, front_inner_back, custom: KiCad 10's (padstack (mode))
    copper: tuple[PadCopper, ...] = ()  # the F.Cu entry first, then the padstack's, as stored
    layers: tuple[str, ...] = ()  # the pad's (layers ...) as stored: "*.Cu", "F&B.Cu", names
    # The board's copper layers from its (layers ...) table, in stack order, as (canonical name,
    # user name or ""); None when the board has no table.
    board_copper: tuple[tuple[str, str], ...] | None = None
    remove_unused_layers: bool = False  # (remove_unused_layers yes); a bare token means yes
    keep_end_layers: bool = False  # (keep_end_layers yes): F.Cu and B.Cu stay flashed
    zone_layer_connections: tuple[str, ...] = ()  # layers a zone connects it on, as stored

    def copper_on(self, layer: str) -> PadCopper:
        """The pad's copper shape and size on the copper ``layer`` (a canonical name: "F.Cu",
        "In<n>.Cu", "B.Cu"), resolved as KiCad does by ``stack_mode``.

        Raises ``RawGeometryError`` naming the pad where the file does not put copper there,
        or does not say: a layer that is not copper or not on the board; a layer outside the
        pad's ``(layers ...)`` (wildcards ``*.Cu`` and ``F&B.Cu`` resolved against the board's
        table; only canonical names count: a board user layer name there is KiCad's Rescue
        layer, not copper, and it and any other token naming copper refuse every layer); an
        ``np_thru_hole`` pad (KiCad
        flashes it only where it reaches past its hole); a ``remove_unused_layers`` pad on a
        layer neither ``keep_end_layers`` nor ``zone_layer_connections`` keeps (KiCad flashes it
        only where a track or via connects, which the file does not state); a layer the
        padstack holds no entry for."""
        what = f"pad {self.number or '?'} ({self.uuid or 'no uuid'})"
        if layer != "F.Cu" and layer != "B.Cu" and not _is_inner(layer):
            raise RawGeometryError(f"{what}: {layer!r} is not a copper layer")
        if self.board_copper is None:
            raise RawGeometryError(f"{what}: the board has no (layers ...) table, so its copper layers are unknown")
        board = [name for name, _ in self.board_copper]
        if layer not in board:
            raise RawGeometryError(f"{what}: the board has no copper layer {layer!r} (it has {board})")
        if layer not in self._on_copper(what):
            raise RawGeometryError(f"{what} is not on {layer!r} (its layers: {list(self.layers)})")
        if self.kind == "np_thru_hole":
            raise RawGeometryError(
                f"{what} is np_thru_hole: KiCad flashes its copper only where the pad reaches past "
                "its hole, which this reader does not resolve")
        if self.remove_unused_layers and layer not in self.zone_layer_connections and not (
                self.keep_end_layers and layer in ("F.Cu", "B.Cu")):
            raise RawGeometryError(
                f"{what}: with (remove_unused_layers yes) KiCad flashes {layer!r} only where a track "
                "or via connects, which the file does not state")
        if self.stack_mode == "normal" or layer == "F.Cu":
            return self.copper[0]
        key = "Inner" if self.stack_mode == "front_inner_back" and _is_inner(layer) else layer
        for entry in self.copper[1:]:
            if entry.layer == key:
                return entry
        raise RawGeometryError(f"{what}: its {self.stack_mode} padstack holds no {key!r} entry")

    def _on_copper(self, what: str) -> set[str]:
        """The board copper layers the pad's ``(layers ...)`` puts it on."""
        board = self.board_copper or ()
        names = {name for name, _ in board}
        users = {user for _, user in board if user}
        on: set[str] = set()
        for token in self.layers:
            if token == "*.Cu":
                on |= names
            elif token == "F&B.Cu":
                on |= names & {"F.Cu", "B.Cu"}
            elif token in names:
                on.add(token)
            elif token in users:  # KiCad 10.0.7: not that copper layer (delta review 2)
                raise RawGeometryError(
                    f"{what}: pad layer {token!r} is a board user layer name; KiCad reads a user layer "
                    "name in a pad's layers as the Rescue layer, not as copper")
            elif token.endswith(".Cu"):  # "*In.Cu", a legacy "Inner1.Cu", a layer the board lacks
                raise RawGeometryError(
                    f"{what}: pad layer {token!r} is not resolved against the board's copper {sorted(names)}")
        return on


@dataclass(frozen=True)
class FootprintGeometry:
    reference: str  # "" when the footprint has none
    uuid: str
    lib_id: str
    side: str  # "front" or "back" (the footprint's (layer ...))
    at_mm: Point  # page frame, y down
    angle_deg: float  # as stored
    pads: tuple[PadGeometry, ...] = ()


def _root(board: Any) -> Any:
    if isinstance(board, (str, Path)):
        from kicad_mcp import write_layer

        board = write_layer.load(board)
    if getattr(board, "name", None) != "kicad_pcb":
        raise RawGeometryError(f"not a kicad_pcb root: {getattr(board, 'name', board)!r}")
    return board


def _num(value: Any, what: str) -> float:
    try:
        number = float(value) if not isinstance(value, bool) else math.nan
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number):  # nan or inf would poison every box built from it
        raise RawGeometryError(f"{what}: {value!r} is not a finite number")
    return number


def _children(node: Any, name: str) -> list[Any]:
    return [c for c in node.children if not c.is_atom and c.name == name]


def _at(node: Any, what: str) -> tuple[float, float, float]:
    at = node.find("at")
    if at is None or len(at.values) < 2:
        raise RawGeometryError(f"{what} has no (at x y)")
    values = at.values
    angle = _num(values[2], f"{what} (at) angle") if len(values) > 2 else 0.0
    return _num(values[0], f"{what} (at) x"), _num(values[1], f"{what} (at) y"), angle


def _reference(footprint: Any) -> str:
    for child in footprint.children:
        if child.is_atom or len(child.children) < 2:
            continue
        if child.name == "property" and child.children[0].value == "Reference":
            return str(child.children[1].value)
        if child.name == "fp_text" and child.children[0].value == "reference":
            return str(child.children[1].value)
    return ""


def _uuid(node: Any) -> str:
    uuid = node.find("uuid")
    return str(uuid.values[0]) if uuid is not None and uuid.values else ""


def _side(footprint: Any) -> str:
    layer = footprint.find("layer")
    return "back" if layer is not None and layer.values and layer.values[0] == "B.Cu" else "front"


def read_footprints(board: Any) -> list[FootprintGeometry]:
    """Every footprint on the board, in file order."""
    root = _root(board)
    copper = _board_copper(root)
    out = []
    for fp in _children(root, "footprint"):
        reference = _reference(fp)
        lib_id = str(fp.children[0].value) if fp.children and fp.children[0].is_atom else ""
        what = f"footprint {reference or lib_id or '?'}"
        x, y, angle = _at(fp, what)
        pads = tuple(_pad(pad, (x, y), angle, what, copper) for pad in _children(fp, "pad"))
        out.append(FootprintGeometry(reference, _uuid(fp), lib_id, _side(fp), (x, y), angle, pads))
    return out


def place(origin: Point, angle_deg: float, local: Point) -> Point:
    """A point in a footprint's own frame, placed on the page: KiCad's rotation on a y-down
    axis (counter-clockwise on screen) by the footprint's angle, then the translation."""
    cos, sin = _cos_sin(angle_deg)
    lx, ly = local
    return (origin[0] + lx * cos + ly * sin + 0.0, origin[1] - lx * sin + ly * cos + 0.0)


def _board_copper(root: Any) -> tuple[tuple[str, str], ...] | None:
    """The copper rows of the board's ``(layers (0 "F.Cu" signal ["user name"]) ...)`` table,
    in file order, as (canonical name, user name or ""); None without a table."""
    tables = _children(root, "layers")
    if not tables:
        return None
    rows = []
    for row in tables[0].children:
        values = [] if row.is_atom else row.values
        name = str(values[0]) if values else ""
        if name in ("F.Cu", "B.Cu") or _is_inner(name):
            rows.append((name, str(values[2]) if len(values) > 2 else ""))
    return tuple(rows)


def _yes_no(pad: Any, name: str, what: str) -> bool:
    """A pad flag such as ``(remove_unused_layers yes)``: absent is no, a bare token is yes
    (KiCad's parser reads it so)."""
    found = _children(pad, name)
    if not found:
        return False
    values = found[0].values
    if not values:
        return True
    if str(values[0]) not in ("yes", "no") or len(found) > 1:
        raise RawGeometryError(f"{what}: ({name} {' '.join(map(str, values))}) is not one yes or no")
    return str(values[0]) == "yes"


def _pad(pad: Any, fp_at: Point, fp_angle: float, owner: str,
         board_copper: tuple[tuple[str, str], ...] | None = None) -> PadGeometry:
    atoms = [c.value for c in pad.children if c.is_atom]
    number = str(atoms[0]) if atoms else ""
    what = f"{owner} pad {number or '?'}"
    lx, ly, angle = _at(pad, what)
    shape = str(atoms[2]) if len(atoms) > 2 else ""
    front = PadCopper("F.Cu", shape, _size(pad, what))
    mode, stack = _padstack(pad, what)
    return PadGeometry(
        number=number,
        uuid=_uuid(pad),
        kind=str(atoms[1]) if len(atoms) > 1 else "",
        shape=shape,
        at_mm=place(fp_at, fp_angle, (lx, ly)),
        local_mm=(lx, ly),
        angle_deg=angle,
        local_angle_deg=(angle - fp_angle) % 360.0 + 0.0,
        size_mm=front.size_mm,
        stack_mode=mode,
        copper=(front, *stack),
        layers=_strings(pad, "layers"),
        board_copper=board_copper,
        remove_unused_layers=_yes_no(pad, "remove_unused_layers", what),
        keep_end_layers=_yes_no(pad, "keep_end_layers", what),
        zone_layer_connections=_strings(pad, "zone_layer_connections"),
    )


def _strings(node: Any, name: str) -> tuple[str, ...]:
    found = _children(node, name)
    return tuple(str(v) for v in found[0].values) if found else ()


def _size(node: Any, what: str) -> Point:
    size = node.find("size")
    if size is None or len(size.values) < 2:
        raise RawGeometryError(f"{what} has no (size w h)")
    return (_num(size.values[0], f"{what} size"), _num(size.values[1], f"{what} size"))


def _padstack(pad: Any, what: str) -> tuple[str, tuple[PadCopper, ...]]:
    """KiCad 10's ``(padstack (mode m) (layer "<name>" (shape s) (size w h) ...) ...)``:
    front_inner_back holds "Inner" and "B.Cu", custom one entry per inner layer and "B.Cu".
    Any other form is refused, naming the pad: a guessed copper size is a wrong one."""
    stacks = _children(pad, "padstack")
    if not stacks:
        return "normal", ()
    if len(stacks) > 1:
        raise RawGeometryError(f"{what} has {len(stacks)} (padstack ...) nodes")
    node = stacks[0]
    mode_node = node.find("mode")
    mode = str(mode_node.values[0]) if mode_node is not None and mode_node.values else ""
    if mode not in _STACK_MODES:
        raise RawGeometryError(f"{what}: padstack mode {mode or '(none)'!r} is not one of {_STACK_MODES}")
    entries: list[PadCopper] = []
    for layer in _children(node, "layer"):
        name = str(layer.values[0]) if layer.values else ""
        allowed = (name in ("Inner", "B.Cu") if mode == "front_inner_back"
                   else mode == "custom" and (name == "B.Cu" or _is_inner(name)))
        if not allowed:
            raise RawGeometryError(f"{what}: a {mode} padstack cannot hold layer {name!r}")
        if any(e.layer == name for e in entries):
            raise RawGeometryError(f"{what}: padstack layer {name!r} is given twice")
        shape = layer.find("shape")
        if shape is None or not shape.values:
            raise RawGeometryError(f"{what}: padstack layer {name!r} has no (shape ...)")
        entries.append(PadCopper(name, str(shape.values[0]), _size(layer, f"{what} padstack layer {name!r}")))
    return mode, tuple(entries)


def _cos_sin(angle_deg: float) -> tuple[float, float]:
    """cos and sin of a KiCad angle, exact at multiples of 90 degrees."""
    quarter = angle_deg / 90.0
    if quarter == int(quarter):
        return ((1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0))[int(quarter) % 4]
    rad = math.radians(angle_deg)
    return math.cos(rad), math.sin(rad)


@dataclass(frozen=True)
class BoardOrigins:
    """The origins in ``(setup ...)``, page frame; (0, 0) when KiCad omitted the token."""

    aux_axis_mm: Point  # the drill/place origin: kicad-cli's --use-drill-file-origin, IPC-D-356
    grid_mm: Point


def read_origins(board: Any) -> BoardOrigins:
    root = _root(board)
    setup = root.find("setup")

    def point(name: str) -> Point:
        node = setup.find(name) if setup is not None else None
        if node is None:
            return (0.0, 0.0)
        if len(node.values) < 2:
            raise RawGeometryError(f"(setup ({name} ...)) needs x and y")
        return (_num(node.values[0], name) + 0.0, _num(node.values[1], name) + 0.0)

    return BoardOrigins(point("aux_axis_origin"), point("grid_origin"))


@dataclass(frozen=True)
class Arc:
    """A three-point arc as KiCad stores it (start, a point on the arc, end), page frame."""

    start: Point
    mid: Point
    end: Point


@dataclass(frozen=True)
class Contour:
    """One closed contour in file order: plain corner points and arcs, exactly as the
    ``(pts (xy ...) (arc (start ...) (mid ...) (end ...)))`` tokens hold them."""

    items: tuple[Point | Arc, ...]

    def corners(self) -> tuple[Point, ...]:
        """The contour's points with each arc reduced to its start and end (arc bulges are
        left out; use :func:`extent` for the exact bounding box)."""
        out: list[Point] = []
        for item in self.items:
            out += [item.start, item.end] if isinstance(item, Arc) else [item]
        return tuple(out)


@dataclass(frozen=True)
class ZoneGeometry:
    uuid: str
    name: str
    net: str  # "" for a rule area or a zone without a net
    layers: tuple[str, ...]  # as stored; may hold a wildcard such as "*.Cu" or "F&B.Cu"
    keepout: bool  # a rule area (keepout)
    outline: Contour  # KiCad reads the first (polygon ...) as the outline
    holes: tuple[Contour, ...]  # and every further one as a hole in it
    footprint_uuid: str | None = None  # set for a zone inside a footprint
    footprint_reference: str | None = None


def _point(node: Any, what: str) -> Point:
    if node is None or len(node.values) < 2:
        raise RawGeometryError(f"{what} needs x and y")
    return (_num(node.values[0], what) + 0.0, _num(node.values[1], what) + 0.0)


def _contour(pts: Any, what: str, to_page: Any = None) -> Contour:
    """A ``(pts ...)`` node. ``to_page`` maps a footprint-local point onto the page; zones are
    stored in page coordinates already, footprint polygons are not."""
    move = to_page or (lambda p: p)
    items: list[Point | Arc] = []
    for child in pts.children if pts is not None else []:
        if child.is_atom:
            continue
        if child.name == "xy":
            items.append(move(_point(child, f"{what} (xy)")))
        elif child.name == "arc":
            items.append(Arc(*(move(_point(child.find(k), f"{what} (arc {k})")) for k in ("start", "mid", "end"))))
        else:
            raise RawGeometryError(f"{what}: unknown point token ({child.name} ...)")
    if not items:
        raise RawGeometryError(f"{what} has no points")
    return Contour(tuple(items))


def _zone(node: Any, owner: Any) -> ZoneGeometry:
    uuid = _uuid(node)
    name_node = node.find("name")
    name = str(name_node.values[0]) if name_node is not None and name_node.values else ""
    what = f"zone {name or uuid or '?'}"
    net_name = node.find("net_name")
    net = node.find("net")
    if net_name is not None and net_name.values:
        net_text = str(net_name.values[0])
    elif net is not None and net.values and isinstance(net.values[0], str):
        net_text = net.values[0]
    else:
        net_text = ""
    layers: list[str] = []
    for tag in ("layer", "layers"):
        found = node.find(tag)
        if found is not None:
            layers += [str(v) for v in found.values]
    polygons = _children(node, "polygon")
    if not polygons:
        raise RawGeometryError(f"{what} has no (polygon ...)")
    contours = [_contour(p.find("pts"), what) for p in polygons]
    return ZoneGeometry(
        uuid=uuid, name=name, net=net_text, layers=tuple(layers),
        keepout=node.find("keepout") is not None,
        outline=contours[0], holes=tuple(contours[1:]),
        footprint_uuid=_uuid(owner) if owner is not None else None,
        footprint_reference=_reference(owner) if owner is not None else None,
    )


def read_zones(board: Any) -> list[ZoneGeometry]:
    """Every zone and rule area: the board's own in file order, then those inside footprints
    (stored in page coordinates, with the footprint's placement already applied)."""
    root = _root(board)
    zones = [_zone(z, None) for z in _children(root, "zone")]
    for fp in _children(root, "footprint"):
        zones += [_zone(z, fp) for z in _children(fp, "zone")]
    return zones


@dataclass(frozen=True)
class OutlineItem:
    """One Edge.Cuts shape, page frame. ``points`` by kind: line (start, end), arc (start, mid,
    end), circle (center, a point on the rim), rect (start, end), curve (start, control 1,
    control 2, end); a poly has its points in ``contour`` instead."""

    kind: str  # line, arc, circle, rect, poly, curve
    uuid: str
    points: tuple[Point, ...]
    width_mm: float  # the stroke; the board edge is the centre line
    contour: Contour | None = None
    footprint_uuid: str | None = None  # set for an fp_* item inside a footprint
    footprint_reference: str | None = None


@dataclass(frozen=True)
class BoardOutline:
    items: tuple[OutlineItem, ...]  # the board's gr_* items in file order, then footprints' fp_*
    bbox_mm: tuple[float, float, float, float] | None  # (x0, y0, x1, y1); None: no outline
    problems: tuple[str, ...]  # what kept the box from being exact; () when it is exact


_SHAPES = ("line", "arc", "circle", "rect", "poly", "curve")


def _on_edge_cuts(node: Any) -> bool:
    layer = node.find("layer")
    return layer is not None and bool(layer.values) and layer.values[0] == "Edge.Cuts"


def _width(node: Any) -> float:
    stroke = node.find("stroke")
    width = stroke.find("width") if stroke is not None else None
    if width is None:
        width = node.find("width")
    return _num(width.values[0], "stroke width") if width is not None and width.values else 0.0


def _outline_item(node: Any, prefix: str, owner: Any, fp_at: Point, fp_angle: float) -> OutlineItem:
    kind = node.name[len(prefix):]
    what = f"{node.name} {_uuid(node) or '?'}"

    def move(p: Point) -> Point:
        return place(fp_at, fp_angle, p) if owner is not None else p

    def pt(tag: str) -> Point:
        return move(_point(node.find(tag), f"{what} ({tag})"))

    contour = None
    if kind == "line":
        points: tuple[Point, ...] = (pt("start"), pt("end"))
    elif kind == "arc":
        if node.find("mid") is None:
            raise RawGeometryError(f"{what} has no (mid ...): the legacy centre/angle arc is not read")
        points = (pt("start"), pt("mid"), pt("end"))
    elif kind == "circle":
        points = (pt("center"), pt("end"))
    elif kind == "rect":
        start = _point(node.find("start"), f"{what} (start)")
        end = _point(node.find("end"), f"{what} (end)")
        if owner is not None and fp_angle % 90:
            # A rectangle turned off the axes is no longer a rectangle: its four corners.
            corners = [start, (end[0], start[1]), end, (start[0], end[1])]
            kind, points = "poly", ()
            contour = Contour(tuple(move(c) for c in corners))
        else:
            points = (move(start), move(end))
    elif kind == "curve":
        pts = _contour(node.find("pts"), what, move).items
        if len(pts) != 4 or any(isinstance(p, Arc) for p in pts):
            raise RawGeometryError(f"{what} needs exactly four (xy ...) points")
        points = tuple(p for p in pts if not isinstance(p, Arc))
    else:  # poly
        points = ()
        contour = _contour(node.find("pts"), what, move)
    return OutlineItem(
        kind=kind, uuid=_uuid(node), points=points, width_mm=_width(node), contour=contour,
        footprint_uuid=_uuid(owner) if owner is not None else None,
        footprint_reference=_reference(owner) if owner is not None else None,
    )


def read_board_outline(board: Any) -> BoardOutline:
    """Every Edge.Cuts shape: the board's ``gr_*`` items and, placed on the page, the ``fp_*``
    items of every footprint (KiCad's board outline includes both). ``bbox_mm`` is the exact
    centre-line extent: arc bulges, circles and curve extremes included. A ``gr_*``/``fp_*``
    item on Edge.Cuts this reader cannot measure (text, a legacy arc without a mid point, a
    collinear arc) is named in ``problems``, and then the box is not KiCad's. A ``dimension``
    on Edge.Cuts is skipped without a problem: KiCad leaves it out of the outline too."""
    root = _root(board)
    items: list[OutlineItem] = []
    problems: list[str] = []

    def take(node: Any, prefix: str, owner: Any = None, at: Point = (0.0, 0.0), angle: float = 0.0) -> None:
        if node.is_atom or not (node.name or "").startswith(prefix) or not _on_edge_cuts(node):
            return
        where = f" in footprint {_reference(owner) or _uuid(owner)}" if owner is not None else ""
        if node.name[len(prefix):] not in _SHAPES:
            problems.append(f"{node.name}{where} on Edge.Cuts is not an outline shape")
            return
        try:
            items.append(_outline_item(node, prefix, owner, at, angle))
        except RawGeometryError as exc:
            problems.append(f"{exc}{where}")

    for node in root.children:
        take(node, "gr_")
    for fp in _children(root, "footprint"):
        x, y, angle = _at(fp, f"footprint {_reference(fp) or '?'}")
        for node in fp.children:
            take(node, "fp_", fp, (x, y), angle)
    points: list[Point] = []
    for item in items:
        found = extent(item)
        if found is None:
            problems.append(f"collinear arc in {item.kind} {item.uuid or '?'}: KiCad cannot read it")
            continue
        points += found
    box = None
    if points:
        box = (min(p[0] for p in points), min(p[1] for p in points),
               max(p[0] for p in points), max(p[1] for p in points))
    return BoardOutline(tuple(items), box, tuple(problems))


def arc_extent(start: Point, mid: Point, end: Point) -> list[Point] | None:
    """Points whose bounding box is the arc's exactly: its three points plus every axis extreme
    of its circle that the arc sweeps through. None for a collinear (degenerate) arc."""
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
        low = a_start
    else:
        low, span = a_end, (a_start - a_end) % tau
    points = [start, mid, end]
    for k in range(4):
        angle = k * math.pi / 2
        if (angle - low) % tau <= span:
            points.append((cx + radius * math.cos(angle), cy + radius * math.sin(angle)))
    return points


def _bezier(p0: Point, p1: Point, p2: Point, p3: Point, t: float) -> Point:
    u = 1 - t
    a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
    return (a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0], a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1])


def _curve_extent(p0: Point, p1: Point, p2: Point, p3: Point) -> list[Point]:
    """The end points plus the cubic Bezier's axis extremes (roots of its derivative)."""
    out = [p0, p3]
    for axis in (0, 1):
        a0, a1, a2, a3 = p0[axis], p1[axis], p2[axis], p3[axis]
        # The derivative divided by 3 is qa t^2 + qb t + qc.
        qa = -a0 + 3 * a1 - 3 * a2 + a3
        qb = 2 * (a0 - 2 * a1 + a2)
        qc = a1 - a0
        roots: list[float] = []
        if abs(qa) < 1e-12:
            if abs(qb) > 1e-12:
                roots.append(-qc / qb)
        else:
            disc = qb * qb - 4 * qa * qc
            if disc >= 0:
                r = math.sqrt(disc)
                roots += [(-qb + r) / (2 * qa), (-qb - r) / (2 * qa)]
        out += [_bezier(p0, p1, p2, p3, t) for t in roots if 0 < t < 1]
    return out


def extent(item: OutlineItem) -> list[Point] | None:
    """Points whose bounding box is the item's exact centre-line box (None: a collinear arc)."""
    if item.kind in ("line", "rect"):
        return list(item.points)
    if item.kind == "arc":
        return arc_extent(*item.points)
    if item.kind == "circle":
        center, rim = item.points
        r = math.dist(center, rim)
        return [(center[0] - r, center[1] - r), (center[0] + r, center[1] + r)]
    if item.kind == "curve":
        return _curve_extent(*item.points)
    return contour_extent(item.contour)


def contour_extent(contour: Contour | None) -> list[Point] | None:
    """Points whose bounding box is the contour's exactly, arc bulges included (None: a
    collinear arc in it)."""
    out: list[Point] = []
    for part in contour.items if contour is not None else ():
        if isinstance(part, Arc):
            found = arc_extent(part.start, part.mid, part.end)
            if found is None:
                return None
            out += found
        else:
            out.append(part)
    return out
