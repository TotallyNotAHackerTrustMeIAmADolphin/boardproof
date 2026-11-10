"""Align and distribute the symbols or labels of a schematic sheet (GitHub issue 90).

``align_items`` and ``distribute_items`` compute a position change for every item of every group and
hand the list to the move machinery of issue 84 (``sheet_editing.move_pipeline``): the property texts
of a moved symbol move with it, wire ends, junctions, labels and no-connect markers that sit exactly
on a moved pin (or label) follow, the staged check proves nothing else changed, kicad-cli's netlist
before and after is compared (``connectivity_changed`` unless allowed), and the write goes through the
write layer under its path lock, with ``dry_run`` and the reload check.

What this module adds, and the facts the tool texts state:

- **Item boxes.** A symbol's box is computed from the raw geometry of its library symbol in the sheet
  file (``lib_symbols``): the graphics of the placed unit and body style (rectangle, polyline, bezier
  control points, circle, arc with its axis extremes) and the pins that are not hidden, transformed
  with the raw ``(at x y angle)`` and ``(mirror ...)`` of the placed symbol (``sheet_editing.place``,
  measured against kicad-cli's netlist). The box is therefore the symbol's graphics plus its pin
  connection points and pin lines. It is NOT KiCad's graphics-only body box
  (``LIB_SYMBOL::GetBodyBoundingBox`` with ``aIncludePins`` false, eeschema/lib_symbol.cpp), which
  skips pins; it corresponds to ``aIncludePins`` true for the pin lines, without the pin name and
  number texts that KiCad's pin box adds. Like KiCad's, it skips fields and hidden pins. Texts and
  text boxes inside the library symbol and the stroke width are not counted either (centre lines).
  Bezier curves contribute their control points, so a box around a curved part can be slightly too
  large. Only the box "pins" exists; a graphics-only box is a follow-up. No ``kicad_tools`` position
  property is read.
  A label is a point: its anchor, the connection point, because the text extent is not in the file.
- **Grid.** The new origin of a moved item is rounded to ``grid`` on the moved axis (``snap``, 1.27 mm
  by default), because a symbol off the grid has its pins off the grid and KiCad does not connect
  across the gap. Items of different size can then not all match the target edge exactly; every item
  reports its ``residual`` (edge after minus target) and every group its ``max_residual``.
- **Order.** ``first`` and ``last`` mean the order of the list the caller gave, so the lists name
  items one by one (reference, uuid or label text, each picking exactly one node); ``min`` and ``max``
  mean the smallest and the largest edge coordinate; a number is an anchor coordinate in mm.
"""

from __future__ import annotations

import math
from typing import Any

from kicad_tools.sexp import SExp

from kicad_mcp import sheet_editing as se
from kicad_mcp.raw_geometry import arc_extent
from kicad_mcp.sheet_authoring import SheetAuthoringError

BOXES = ("pins",)
EDGES = {"left": "x", "right": "x", "hcenter": "x", "top": "y", "bottom": "y", "vcenter": "y"}
LIST_KEYS = {"symbol": ("references", "uuids"), "label": ("texts", "uuids")}

Box = tuple[float, float, float, float]


# --------------------------------------------------------------------------- boxes


def _hidden(pin: SExp) -> bool:
    for child in pin.children:
        if child.is_atom and str(child.value) == "hide":
            return True
        if not child.is_atom and child.name == "hide":
            return not (child.children and str(child.children[0].value) == "no")
    return False


def _geometry_points(sub: SExp) -> list[tuple[float, float]]:
    """Library-frame points (Y up) whose bounding box is that of the graphics and visible pins of one sub-symbol."""
    pts: list[tuple[float, float]] = []

    def xy(node: SExp, tag: str) -> tuple[float, float] | None:
        found = node.find_children(tag)
        return (found[0].get_float(0), found[0].get_float(1)) if found else None

    for item in sub.children:
        if item.is_atom:
            continue
        if item.name == "rectangle":
            a, b = xy(item, "start"), xy(item, "end")
            pts += [p for p in (a, b) if p]
        elif item.name in ("polyline", "bezier"):
            holder = item.find_children("pts")
            if holder:
                pts += [(p.get_float(0), p.get_float(1)) for p in holder[0].find_children("xy")]
        elif item.name == "circle":
            c, r = xy(item, "center"), item.find_children("radius")
            if c and r:
                rad = r[0].get_float(0)
                pts += [(c[0] - rad, c[1] - rad), (c[0] + rad, c[1] + rad)]
        elif item.name == "arc":
            a, m, b = xy(item, "start"), xy(item, "mid"), xy(item, "end")
            if a and m and b:
                pts += arc_extent(a, m, b) or [a, m, b]
        elif item.name == "pin" and not _hidden(item):
            at = item.find_children("at")
            if not at:
                continue
            px, py = at[0].get_float(0), at[0].get_float(1)
            pts.append((px, py))
            angle = math.radians(at[0].get_float(2) if len(at[0].children) > 2 else 0.0)
            length = item.find_children("length")
            reach = length[0].get_float(0) if length else 0.0
            pts.append((px + reach * math.cos(angle), py + reach * math.sin(angle)))
    return pts


def symbol_box(root: SExp, symbol: SExp, box: str = "pins") -> Box | None:
    """``(x0, y0, x1, y1)`` on the sheet (Y down) of one placed symbol, from the raw ``(at ...)`` of the
    symbol and the raw geometry of its library symbol; None when the sheet holds no geometry for it."""
    lib = se._lib_symbol(root, symbol)
    if lib is None:
        return None
    sx, sy, rot = se._at(symbol)
    mirror = se._mirror(symbol)
    unit = (symbol.find_children("unit")[0].get_int(0) if symbol.find_children("unit") else 1) or 1
    style = (symbol.find_children("body_style")[0].get_int(0) if symbol.find_children("body_style") else 1) or 1
    points: list[tuple[float, float]] = []
    for sub in [lib, *se._kids(lib, "symbol")]:
        if sub is not lib:
            parts = (sub.get_string(0) or "").rsplit("_", 2)
            if len(parts) < 3 or not (parts[1].isdigit() and parts[2].isdigit()):
                continue
            if int(parts[1]) not in (0, unit) or int(parts[2]) not in (0, style):
                continue
        points += _geometry_points(sub)
    if not points:
        return None
    # rectangles, circles and arcs are given as corner or extreme points: transform every point, take the box
    placed = [se.place(px, py, rot, mirror) for px, py in points]
    xs, ys = [sx + p[0] for p in placed], [sy + p[1] for p in placed]
    return (min(xs), min(ys), max(xs), max(ys))


# --------------------------------------------------------------------------- items


class _Item:
    def __init__(self, node: SExp, kind: str, box: Box) -> None:
        self.node, self.kind, self.box = node, kind, box
        self.origin = se._at(node)[:2]
        self.uuid = se._uuid(node)
        self.name = se._ref(node) if kind == "symbol" else (node.get_string(0) or "")

    def lo(self, axis: str) -> float:
        return self.box[0] if axis == "x" else self.box[1]

    def hi(self, axis: str) -> float:
        return self.box[2] if axis == "x" else self.box[3]

    def centre(self, axis: str) -> float:
        return (self.lo(axis) + self.hi(axis)) / 2

    def edge(self, edge: str) -> float:
        axis = EDGES[edge]
        return {"left": self.lo, "top": self.lo, "right": self.hi, "bottom": self.hi}.get(edge, lambda a: self.centre(a))(axis)


def _group_items(sheet: se.Sheet, group: Any, where: str, box: str, allowed: set[str], seen: set[int]) -> list[_Item]:
    if not isinstance(group, dict):
        raise SheetAuthoringError("invalid_spec", f"{where} must be an object")
    kind = group.get("kind")
    if not isinstance(kind, str) or kind not in LIST_KEYS:
        raise SheetAuthoringError("invalid_spec", f"{where}.kind must be 'symbol' or 'label', got {kind!r}")
    unknown = set(group) - allowed - set(LIST_KEYS[kind])
    if unknown:
        raise SheetAuthoringError("invalid_spec", f"{where} has keys {sorted(unknown)} that this tool does not take")
    given = [k for k in LIST_KEYS[kind] if k in group]
    if len(given) != 1:
        raise SheetAuthoringError("invalid_spec", f"{where} needs exactly one of {list(LIST_KEYS[kind])} (a list naming the items one by one)")
    key = given[0]
    names = group[key]
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise SheetAuthoringError("invalid_spec", f"{where}.{key} must be a list of non-empty strings")
    if len(set(names)) != len(names):
        raise SheetAuthoringError("invalid_spec", f"{where}.{key} names an item twice")
    items = []
    for name in names:
        nodes = se.select(sheet, kind, [{key: [name]}])
        if len(nodes) != 1:
            raise SheetAuthoringError(
                "invalid_spec", f"{where}: {name!r} picks {len(nodes)} {kind} nodes; every entry must pick exactly one (use uuids for several units or equal texts)")
        node = nodes[0]
        if id(node) in seen:
            raise SheetAuthoringError("invalid_spec", f"{where}: {name!r} is already in another group of this call")
        seen.add(id(node))
        if kind == "symbol":
            found = symbol_box(sheet.root, node, box)
            if found is None:
                raise SheetAuthoringError("no_symbol_geometry", f"{where}: the sheet holds no library geometry for {se._ref(node)}; nothing was written")
        else:
            x, y = se._at(node)[:2]
            found = (x, y, x, y)
        items.append(_Item(node, kind, found))
    return items


def _check_common(groups: Any, box: Any, minimum: int) -> None:
    if box not in BOXES:
        raise SheetAuthoringError("invalid_spec", f"box must be 'pins' (the only box there is: the symbol's graphics plus its visible pins' connection points and lines), got {box!r}")
    if not isinstance(groups, list) or not groups:
        raise SheetAuthoringError("invalid_spec", "groups must be a non-empty list of objects")


def _finish(item: _Item, delta: float, axis: str, snap: bool, grid: float) -> tuple[float, float, bool, float]:
    """The planned move of one item along ``axis`` by about ``delta``: ``(dx, dy, snapped, shift)``."""
    if abs(delta) <= se.TOL:
        return 0.0, 0.0, False, 0.0
    old = item.origin[0] if axis == "x" else item.origin[1]
    wanted = old + delta
    got = se._snap(wanted, grid) if snap else se._r(wanted)
    shift = got - old
    if abs(shift) <= se.TOL / 10:
        return 0.0, 0.0, False, 0.0
    snapped = snap and abs(got - wanted) > se.TOL / 10
    return (shift, 0.0, snapped, shift) if axis == "x" else (0.0, shift, snapped, shift)


def _record(item: _Item, dx: float, dy: float, snapped: bool, axis: str, delta: float) -> dict:
    ox, oy = item.origin
    want = (ox + (delta if axis == "x" else 0.0), oy + (delta if axis == "y" else 0.0))
    return {"asked": se._round_pt(*want), "snapped": se._round_pt(ox + dx, oy + dy) if snapped else None}


# --------------------------------------------------------------------------- planning


def _plan_group_moves(sheet: se.Sheet, op: str, groups: Any, box: str, snap: bool, grid: float):
    """``(planned, extra_report)`` for ``align`` or ``distribute``; refuses before anything changes."""
    _check_common(groups, box, 2)
    allowed = {"kind", "edge", "reference"} if op == "align" else {"kind", "axis", "spacing"}
    seen: set[int] = set()
    planned: list = []
    reports: list[dict] = []
    kinds = set()
    for g, group in enumerate(groups):
        where = f"groups[{g}]"
        items = _group_items(sheet, group, where, box, allowed, seen)
        kinds.add(group["kind"])
        if len(kinds) > 1:
            raise SheetAuthoringError("invalid_spec", "one call handles one kind of item (symbols or labels); split the groups over two calls")
        if op == "align":
            rep, moves = _plan_align(items, group, where, snap, grid)
        else:
            rep, moves = _plan_distribute(items, group, where, snap, grid)
        for item, dx, dy, sn, axis, delta in moves:
            if abs(dx) > se.TOL / 10 or abs(dy) > se.TOL / 10:
                planned.append((item.node, dx, dy, _record(item, dx, dy, sn, axis, delta)))
        reports.append(rep)
    snapped_note = (f"new origins are rounded to the {grid} mm grid on the moved axis; residual is the distance of an item's edge from its target after that rounding"
                    if snap else "snap is off: new origins are exact (rounded to 0.1 micrometre)")
    return planned, {"groups": reports, "kind": sorted(kinds)[0], "box": box, "note": snapped_note,
                     "box_note": "symbol boxes come from the raw library geometry of the sheet file (graphics and visible pins; pin texts and stroke width not included); a label is its anchor point"}


def _plan_align(items: list[_Item], group: dict, where: str, snap: bool, grid: float):
    if len(items) < 2:
        raise SheetAuthoringError("invalid_spec", f"{where} needs at least two items to align")
    edge = group.get("edge")
    if not isinstance(edge, str) or edge not in EDGES:
        raise SheetAuthoringError("invalid_spec", f"{where}.edge must be one of {sorted(EDGES)}, got {edge!r}")
    ref = group.get("reference", "first")
    axis = EDGES[edge]
    values = [i.edge(edge) for i in items]
    if ref == "first":
        target = values[0]
    elif ref == "last":
        target = values[-1]
    elif ref == "min":
        target = min(values)
    elif ref == "max":
        target = max(values)
    elif se.is_mm(ref):
        target = float(ref)
    else:
        raise SheetAuthoringError("invalid_spec", f"{where}.reference must be 'first', 'last', 'min', 'max' or a finite coordinate in mm (at most {se.MAX_MM:g}), got {ref!r}")
    moves, rows = [], []
    for item, value in zip(items, values, strict=True):
        dx, dy, sn, shift = _finish(item, target - value, axis, snap, grid)
        moves.append((item, dx, dy, sn, axis, target - value))
        after = value + shift
        rows.append(_row(item, value, after, after - target, abs(shift) > se.TOL / 10, sn))
    worst = max(abs(r["residual"]) for r in rows)
    return {"kind": group["kind"], "edge": edge, "reference": ref, "axis": axis, "target": se._r(target), "items": rows,
            "max_residual": se._r(worst), "aligned": worst <= se.TOL}, moves


def _row(item: _Item, before: float, after: float, residual: float, moved: bool, snapped: bool) -> dict:
    key = "reference" if item.kind == "symbol" else "text"
    return {key: item.name, "uuid": item.uuid, "edge_before": se._r(before), "edge_after": se._r(after),
            "residual": se._r(residual), "moved": moved, "snapped": bool(snapped and moved)}


def _plan_distribute(items: list[_Item], group: dict, where: str, snap: bool, grid: float):
    if len(items) < 3:
        raise SheetAuthoringError("invalid_spec", f"{where} needs at least three items to distribute (two have nothing between them)")
    axis = group.get("axis")
    if axis not in ("x", "y"):
        raise SheetAuthoringError("invalid_spec", f"{where}.axis must be 'x' or 'y', got {axis!r}")
    spacing = group.get("spacing", "gaps")
    pitch = None
    if spacing in ("gaps", "centres"):
        pass
    elif se.is_mm(spacing) and spacing > 0:
        pitch = float(spacing)
    else:
        raise SheetAuthoringError("invalid_spec", f"{where}.spacing must be 'gaps', 'centres' or a positive, finite pitch in mm (at most {se.MAX_MM:g}), got {spacing!r}")
    ordered = sorted(items, key=lambda i: (i.centre(axis), i.uuid))
    n = len(ordered)
    first, last = ordered[0], ordered[-1]
    deltas: list[float] = [0.0] * n
    if spacing == "gaps":
        free = last.hi(axis) - first.lo(axis) - sum(i.hi(axis) - i.lo(axis) for i in ordered)
        gap = free / (n - 1)
        cursor = first.hi(axis)
        for k in range(1, n - 1):
            lo = cursor + gap
            deltas[k] = lo - ordered[k].lo(axis)
            cursor = lo + (ordered[k].hi(axis) - ordered[k].lo(axis))
        nominal = gap
    else:
        step = pitch if pitch is not None else (last.centre(axis) - first.centre(axis)) / (n - 1)
        for k in range(1, n if pitch is not None else n - 1):
            deltas[k] = first.centre(axis) + k * step - ordered[k].centre(axis)
        nominal = step
    moves, rows, shifts = [], [], []
    for item, delta in zip(ordered, deltas, strict=True):
        dx, dy, sn, shift = _finish(item, delta, axis, snap, grid)
        moves.append((item, dx, dy, sn, axis, delta))
        shifts.append(shift)
        key = "reference" if item.kind == "symbol" else "text"
        rows.append({key: item.name, "uuid": item.uuid, "centre_before": se._r(item.centre(axis)),
                     "centre_after": se._r(item.centre(axis) + shift), "moved": abs(shift) > se.TOL / 10, "snapped": bool(sn)})
    los = [i.lo(axis) + s for i, s in zip(ordered, shifts, strict=True)]
    his = [i.hi(axis) + s for i, s in zip(ordered, shifts, strict=True)]
    gaps = [los[k + 1] - his[k] for k in range(n - 1)]
    pitches = [(los[k + 1] + his[k + 1]) / 2 - (los[k] + his[k]) / 2 for k in range(n - 1)]
    measured = gaps if spacing == "gaps" else pitches
    worst = max(abs(m - nominal) for m in measured)
    overlaps = [[ordered[k].name, ordered[k + 1].name] for k in range(n - 1) if gaps[k] < -se.TOL]
    return {"kind": group["kind"], "axis": axis, "spacing": spacing if pitch is None else "pitch", "pitch": pitch,
            "order": [i.name for i in ordered], "items": rows, "gaps_after": [se._r(g) for g in gaps],
            "centre_pitches_after": [se._r(p) for p in pitches], "nominal": se._r(nominal),
            "max_residual": se._r(worst), "even": worst <= se.TOL, "overlapping_after": overlaps}, moves


# --------------------------------------------------------------------------- entry points


def _run(op: str, path: str, groups: Any, root_sch: str | None, *, box: Any, move_wires: bool, snap: bool, grid: Any,
         allow_connectivity_change: bool, dry_run: bool) -> dict[str, Any]:
    kind = "symbol"
    if isinstance(groups, list) and groups and isinstance(groups[0], dict) and groups[0].get("kind") == "label":
        kind = "label"

    def planner(sheet: se.Sheet):
        return _plan_group_moves(sheet, op, groups, box, bool(snap), float(grid))

    return se.move_pipeline(kind, planner, path, root_sch, move_wires=move_wires, snap=snap, grid=grid,
                            allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)


def align(path: str, groups: list[dict], root_sch: str | None, *, box: str = "pins", move_wires: bool = True, snap: bool = True,
          grid: float = se.GRID, allow_connectivity_change: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Align the edges or centres of the items of each group (see the module docstring)."""
    return _run("align", path, groups, root_sch, box=box, move_wires=move_wires, snap=snap, grid=grid,
                allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)


def distribute(path: str, groups: list[dict], root_sch: str | None, *, box: str = "pins", move_wires: bool = True, snap: bool = True,
               grid: float = se.GRID, allow_connectivity_change: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Distribute the items of each group evenly along an axis (see the module docstring)."""
    return _run("distribute", path, groups, root_sch, box=box, move_wires=move_wires, snap=snap, grid=grid,
                allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)
