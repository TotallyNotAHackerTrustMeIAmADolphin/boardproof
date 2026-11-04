"""Wire a sheet by pin, not by coordinate (GitHub issue 86).

``connect_pins`` takes a batch of ``{from: {ref|uuid, pin}, to: {ref|uuid, pin}}`` connections. The
tool finds both pin points on the sheet (the ``sheet_editing.pin_positions`` maths on the raw
``(at ...)`` nodes: rotation, mirror, unit), plans an orthogonal path that leaves each pin along
its own direction, draws it as wire segments, and checks the result the way the other sheet-editing
tools do (decision 4, docs/ARCHITECTURE.md): the edit is made on a copy of the tree, a staged
check shows that only the new wires (and labels) were added, kicad-cli's netlist of the project is
read before and after from scratch copies, and only then is the sheet saved through
``write_layer.save``. Nothing here edits KiCad text.

Facts the tool text states:

- A pin point is never moved. It sits where the library puts it, on the 1.27 mm grid or not. The
  grid applies to the points the tool chooses itself (the dogleg of a Z, the line of a U, a
  ``via`` waypoint); the result lists every such coordinate it snapped (``snapped``) and every pin
  point that is off the grid (``off_grid_endpoints``), which stays exact.
- The path is checked against the other connectors of the sheet (pins, labels, junctions, no-connect
  markers, sheet pins) and against the other wires (existing, and drawn earlier in the same call):
  a route that passes over one, or touches a wire, is not drawn. Another route of the same pair is
  tried; when none is clear the call is refused (``no_clear_route``) and names what is in the way.
  Symbol bodies are not checked: render the sheet.
- Connectivity: every requested pair must end on one net (a wire that lands elsewhere is refused
  as ``write_not_verified``), and a net other than the requested ones must not merge, nor two
  differently named nets be joined, unless ``allow_connectivity_change`` is true.
"""

from __future__ import annotations

import math
from typing import Any

from kicad_tools.sexp import SExp

from kicad_mcp import sheet_authoring, sheet_editing as se, write_layer
from kicad_mcp.sheet_authoring import SheetAuthoringError

GRID = se.GRID
TOL = se.TOL
_ENDPOINT_KEYS = {"ref", "reference", "uuid", "pin", "unit"}
_CONNECTION_KEYS = {"from", "to", "via", "net_label"}
_MAX_BLOCKERS = 5
PIN_LANDING_TOL = 0.01  # mm: a point this close to a pin was meant for it; it lands exactly on the pin

Point = tuple[float, float]


# --------------------------------------------------------------------------- nodes


def wire_node(a: Point, b: Point) -> SExp:
    """A wire from ``a`` to ``b``, shaped as KiCad writes one."""
    return SExp.list(
        "wire",
        SExp.list("pts", SExp.list("xy", write_layer.number(se._r(a[0])), write_layer.number(se._r(a[1]))),
                  SExp.list("xy", write_layer.number(se._r(b[0])), write_layer.number(se._r(b[1])))),
        SExp.list("stroke", SExp.list("width", write_layer.number(0)), SExp.list("type", write_layer.symbol("default"))),
        sheet_authoring._uuid(),
    )


def junction_node(x: float, y: float) -> SExp:
    """A junction dot at ``(x, y)``, shaped as KiCad writes one."""
    return SExp.list("junction", SExp.list("at", write_layer.number(se._r(x)), write_layer.number(se._r(y))),
                     SExp.list("diameter", write_layer.number(0)),
                     SExp.list("color", write_layer.number(0), write_layer.number(0), write_layer.number(0), write_layer.number(0)),
                     sheet_authoring._uuid())


def label_node(text: str, x: float, y: float, angle: int) -> SExp:
    return SExp.list("label", write_layer.string(text), SExp.list("at", write_layer.number(se._r(x)), write_layer.number(se._r(y)), write_layer.number(angle)),
                     SExp.list("fields_autoplaced", write_layer.symbol("yes")), sheet_authoring._effects("left bottom"), sheet_authoring._uuid())


# --------------------------------------------------------------------------- grid and pin landing


def on_grid(value: float, grid: float = GRID) -> bool:
    return abs(value / grid - round(value / grid)) * grid < 1e-3


def snap_value(value: float, grid: float = GRID) -> float:
    return se._r(round(value / grid) * grid)


def pin_points(root: SExp) -> list[Point]:
    """Every pin point of every placed symbol of the sheet."""
    out: list[Point] = []
    for sym in se._kids(root, "symbol"):
        if sym.find_children("lib_id"):
            out += [(p["x"], p["y"]) for p in se.pin_positions(root, sym)]
    return out


def symbol_uuids(root: SExp) -> set[str]:
    """The uuid of every placed symbol of the sheet."""
    out: set[str] = set()
    for sym in se._kids(root, "symbol"):
        if sym.find_children("lib_id") and sym.find_children("uuid"):
            out.add(sym.find_children("uuid")[0].get_string(0))
    return out


def read_back(root: SExp, before: set[str], grid: float = GRID) -> list[dict[str, Any]]:
    """What the sheet now holds for every symbol (every unit) whose uuid is not in ``before``, read
    from the raw ``(at ...)`` of the symbol and of its pins: reference, uuid, lib_id, unit, at,
    rotation, pins ({number, name, x, y}) and the pin numbers that are off the ``grid``."""
    out: list[dict[str, Any]] = []
    for sym in se._kids(root, "symbol"):
        if not sym.find_children("lib_id") or not sym.find_children("uuid"):
            continue
        uid = sym.find_children("uuid")[0].get_string(0)
        if uid in before:
            continue
        x, y, rot = se._at(sym)
        unit = sym.find_children("unit")
        pins = [{"number": p["number"], "name": p["name"], "x": p["x"], "y": p["y"]} for p in se.pin_positions(root, sym)]
        out.append({
            "reference": se._ref(sym), "uuid": uid, "lib_id": sym.find_children("lib_id")[0].get_string(0),
            "unit": int(unit[0].get_float(0)) if unit else 1, "at": [x, y], "rotation": rot,
            "pins": pins, "pin_count": len(pins),
            "off_grid_pins": [p["number"] for p in pins if not (on_grid(p["x"], grid) and on_grid(p["y"], grid))],
        })
    return out


def connector_points(root: SExp) -> list[Point]:
    """Every point on the sheet where something is attached: the pins of the placed symbols, labels (all
    kinds), no-connect markers, junctions, sheet pins and the ends of the wires."""
    out: list[Point] = pin_points(root)
    for node in root.children:
        if node.is_atom:
            continue
        if node.name in se.LABEL_TAGS or node.name in ("no_connect", "junction"):
            out.append(se._at(node)[:2])
        elif node.name == "sheet":
            out += [se._at(pin)[:2] for pin in se._kids(node, "pin")]
        elif node.name == "wire":
            out += se._wire_ends(node)
    return out


def land(point: Point, targets: list[Point], grid: float, snap: bool) -> tuple[Point, str]:
    """Where a free ``point`` goes. With ``snap`` false nowhere: nothing is moved. With ``snap`` true: onto
    the target (a connector point, see ``connector_points``) it is within ``PIN_LANDING_TOL`` of, so that what
    sits on the sheet wins over the grid; else rounded to ``grid`` per coordinate. The second value says
    which: ``"pin"`` (landed on a target), ``"snapped"`` or ``"unchanged"``."""
    if not snap:
        return point, "unchanged"
    for target in targets:
        if abs(target[0] - point[0]) <= PIN_LANDING_TOL and abs(target[1] - point[1]) <= PIN_LANDING_TOL:
            return target, ("unchanged" if se._same(target, point) else "pin")
    got = (snap_value(point[0], grid), snap_value(point[1], grid))
    return got, ("unchanged" if abs(got[0] - point[0]) < 1e-9 and abs(got[1] - point[1]) < 1e-9 else "snapped")


# --------------------------------------------------------------------------- the connectors of the sheet


class World:
    """The things a new path must not touch: connector points and wires."""

    def __init__(self, sheet: se.Sheet) -> None:
        self.connectors: list[tuple[str, Point]] = []
        for sym in sheet.symbols:
            if not sym.find_children("lib_id"):
                continue
            ref = se._ref(sym)
            for pin in se.pin_positions(sheet.root, sym):
                self.connectors.append((f"pin {ref}.{pin['number']}", (pin["x"], pin["y"])))
        for node in sheet.labels:
            x, y, _a = se._at(node)
            self.connectors.append((f"{node.name} {node.get_string(0)!r}", (x, y)))
        for node in sheet.no_connects:
            x, y, _a = se._at(node)
            self.connectors.append(("no_connect marker", (x, y)))
        for node in sheet.junctions:
            x, y, _a = se._at(node)
            self.connectors.append(("junction", (x, y)))
        for node in sheet.sheets:
            for pin in se._kids(node, "pin"):
                x, y, _a = se._at(pin)
                self.connectors.append((f"sheet pin {pin.get_string(0)!r}", (x, y)))
        self.wires: list[tuple[str, Point, Point]] = []
        for wire in sheet.wires:
            ends = se._wire_ends(wire)
            for a, b in zip(ends, ends[1:], strict=False):
                self.wires.append((se._uuid(wire), a, b))

    def blockers(self, path: list[Point], p: Point, q: Point, extra: tuple[Point, ...] = ()) -> list[dict[str, Any]]:
        """What the segments of ``path`` touch besides the two pin points ``p`` and ``q`` (and the ``extra``
        points: where a trimmed path meets the wire it branches from)."""
        found: list[dict[str, Any]] = []

        def ends(pt: Point) -> bool:
            return se._same(pt, p) or se._same(pt, q) or any(se._same(pt, e) for e in extra)

        for a, b in zip(path, path[1:], strict=False):
            for what, pt in self.connectors:
                if not ends(pt) and se._segment_contains(a, b, pt):
                    found.append({"type": "connector", "what": what, "at": se._round_pt(*pt)})
            for pt, name in ((p, "its own start pin"), (q, "its own end pin")):
                if se._segment_contains(a, b, pt) and not (se._same(pt, a) or se._same(pt, b)):
                    found.append({"type": "own_pin", "what": name, "at": se._round_pt(*pt)})
            for uid, c, d in self.wires:
                if _overlap(a, b, c, d) > TOL:
                    found.append({"type": "wire_overlap", "what": f"wire {uid}", "at": se._round_pt(*c)})
                    continue
                for end in (c, d):
                    if not ends(end) and se._segment_contains(a, b, end):
                        found.append({"type": "wire_end_on_route", "what": f"wire {uid}", "at": se._round_pt(*end)})
                for corner in (a, b):
                    if not ends(corner) and se._segment_contains(c, d, corner):
                        found.append({"type": "route_corner_on_wire", "what": f"wire {uid}", "at": se._round_pt(*corner)})
        unique: list[dict[str, Any]] = []
        for item in found:
            if item not in unique:
                unique.append(item)
        return unique

    def crossed(self, path: list[Point]) -> list[str]:
        """Existing wires the path crosses without touching an end (KiCad does not connect those)."""
        out: list[str] = []
        for a, b in zip(path, path[1:], strict=False):
            for uid, c, d in self.wires:
                if uid in out:
                    continue
                if _crossing(a, b, c, d):
                    out.append(uid)
        return out


def _overlap(a: Point, b: Point, c: Point, d: Point) -> float:
    """Length of the shared stretch of two collinear axis-aligned segments (0 if not collinear)."""
    if abs(a[1] - b[1]) <= TOL and abs(c[1] - d[1]) <= TOL and abs(a[1] - c[1]) <= TOL:
        return min(max(a[0], b[0]), max(c[0], d[0])) - max(min(a[0], b[0]), min(c[0], d[0]))
    if abs(a[0] - b[0]) <= TOL and abs(c[0] - d[0]) <= TOL and abs(a[0] - c[0]) <= TOL:
        return min(max(a[1], b[1]), max(c[1], d[1])) - max(min(a[1], b[1]), min(c[1], d[1]))
    return 0.0


def _crossing(a: Point, b: Point, c: Point, d: Point) -> bool:
    horiz_ab, horiz_cd = abs(a[1] - b[1]) <= TOL, abs(c[1] - d[1]) <= TOL
    if horiz_ab == horiz_cd:
        return False
    (h0, h1), (v0, v1) = ((a, b), (c, d)) if horiz_ab else ((c, d), (a, b))
    x, y = v0[0], h0[1]
    return min(h0[0], h1[0]) + TOL < x < max(h0[0], h1[0]) - TOL and min(v0[1], v1[1]) + TOL < y < max(v0[1], v1[1]) - TOL


# --------------------------------------------------------------------------- path planning


def _dir(a: Point, b: Point) -> tuple[int, int] | None:
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) > TOL and abs(dy) <= TOL:
        return (1 if dx > 0 else -1, 0)
    if abs(dy) > TOL and abs(dx) <= TOL:
        return (0, 1 if dy > 0 else -1)
    return None


def _clean(points: list[Point]) -> list[Point]:
    """Drop repeated points and the middle point of two collinear legs."""
    out: list[Point] = []
    for pt in points:
        if not out or not se._same(out[-1], pt):
            out.append(pt)
    changed = True
    while changed and len(out) > 2:
        changed = False
        for i in range(1, len(out) - 1):
            if _dir(out[i - 1], out[i]) is not None and _dir(out[i - 1], out[i]) == _dir(out[i], out[i + 1]):
                del out[i]
                changed = True
                break
    return out


def _steps(raw: float, lo: float, hi: float, snap: bool, grid: float) -> list[float]:
    """Values between ``lo`` and ``hi`` (exclusive) for a free coordinate wanted near ``raw``: grid
    lines nearest first when ``snap``, else ``raw`` alone; ``raw`` itself when no grid line fits."""
    lo, hi = min(lo, hi), max(lo, hi)
    if not snap:
        return [se._r(raw)]
    base = round(raw / grid)
    values = [se._r((base + k) * grid) for k in sorted(range(-3, 4), key=lambda k: (abs(k), k))]
    values = [v for v in values if lo + TOL < v < hi - TOL]
    return values or [se._r(raw)]


def _beyond(extreme: float, sign: int, snap: bool, grid: float) -> list[float]:
    """Lines beyond ``extreme`` in direction ``sign`` for the back-run of a U: one to three grid steps out."""
    if not snap:
        return [se._r(extreme + sign * grid * k) for k in (1, 2, 3)]
    first = (math.floor(extreme / grid + 1e-9) + 1) if sign > 0 else (math.ceil(extreme / grid - 1e-9) - 1)
    return [se._r((first + sign * k) * grid) for k in range(3)]


def candidates(p: Point, q: Point, dp: tuple[int, int] | None, dq: tuple[int, int] | None, snap: bool, grid: float):
    """Orthogonal paths from ``p`` to ``q``, fewest segments first: straight, L (both corners), Z (the
    dogleg on the grid lines nearest the middle), U (both pins face the same way: out, across, back).
    Each item: (shape, points, free) where ``free`` lists the coordinates the tool chose and the
    value it wanted (``{axis, asked, now}``)."""
    px, py = p
    qx, qy = q
    aligned = abs(px - qx) <= TOL or abs(py - qy) <= TOL
    if aligned:
        yield "straight", [p, q], []
    else:
        yield "L", [p, (qx, py), q], []
        yield "L", [p, (px, qy), q], []
        raw_x, raw_y = (px + qx) / 2, (py + qy) / 2
        for mx in _steps(raw_x, px, qx, snap, grid):
            yield "Z", [p, (mx, py), (mx, qy), q], [{"axis": "x", "asked": se._r(raw_x), "now": mx}]
        for my in _steps(raw_y, py, qy, snap, grid):
            yield "Z", [p, (px, my), (qx, my), q], [{"axis": "y", "asked": se._r(raw_y), "now": my}]
    if dp is not None and dp == dq:
        if dp[1] == 0 and abs(py - qy) > TOL:
            extreme = max(px, qx) if dp[0] > 0 else min(px, qx)
            for line in _beyond(extreme, dp[0], snap, grid):
                yield "U", [p, (line, py), (line, qy), q], [{"axis": "x", "asked": se._r(extreme + dp[0] * grid), "now": line}]
        if dp[0] == 0 and abs(px - qx) > TOL:
            extreme = max(py, qy) if dp[1] > 0 else min(py, qy)
            for line in _beyond(extreme, dp[1], snap, grid):
                yield "U", [p, (px, line), (qx, line), q], [{"axis": "y", "asked": se._r(extreme + dp[1] * grid), "now": line}]


def _trim_start(points: list[Point], wires: list[tuple[str, Point, Point]]) -> tuple[list[Point], list[str]]:
    """Drop the part of the path's start that runs along wires already attached to the start point: a
    second connection from a pin branches off the stub (or the bus line) the first one drew instead of
    drawing it twice. Returns the rest of the path and the uuids of the wires it reuses."""
    pts = list(points)
    reused: list[str] = []
    while len(pts) >= 2:
        a, b = pts[0], pts[1]
        d = _dir(a, b)
        if d is None:
            break
        far = a
        progressed = True
        while progressed:
            progressed = False
            for uid, c, e in wires:
                if abs(c[0] - e[0]) > TOL and abs(c[1] - e[1]) > TOL:
                    continue
                if not se._segment_contains(c, e, far) or _dir(c, e) not in (d, (-d[0], -d[1])):
                    continue
                end = max((c, e), key=lambda t: (t[0] - far[0]) * d[0] + (t[1] - far[1]) * d[1])
                reach = (end[0] - far[0]) * d[0] + (end[1] - far[1]) * d[1]
                if reach > TOL:
                    limit = (b[0] - far[0]) * d[0] + (b[1] - far[1]) * d[1]
                    nxt = end if reach < limit - TOL else b
                    if uid not in reused:
                        reused.append(uid)
                    far, progressed = nxt, True
                    if se._same(far, b):
                        progressed = False
                    break
        if se._same(far, a):
            break
        if se._same(far, b):
            pts.pop(0)
            continue
        pts[0] = far
        break
    return pts, reused


def trim(points: list[Point], wires: list[tuple[str, Point, Point]]) -> tuple[list[Point], list[str]]:
    """``_trim_start`` from both ends; the second pass runs on the reversed path."""
    pts, first = _trim_start(points, wires)
    if len(pts) < 2:
        return pts, first
    back, last = _trim_start(pts[::-1], wires)
    return back[::-1], first + [u for u in last if u not in first]


def conforms(points: list[Point], dp: tuple[int, int] | None, dq: tuple[int, int] | None) -> bool:
    """The wire leaves ``p`` along its pin direction and arrives at ``q`` against its pin direction."""
    if len(points) < 2:
        return False
    first, last = _dir(points[0], points[1]), _dir(points[-2], points[-1])
    if first is None or last is None:
        return False
    return (dp is None or first == dp) and (dq is None or last == (-dq[0], -dq[1]))


def plan_route(world: World, p: Point, dp, q: Point, dq, snap: bool, grid: float, where: str, names: tuple[str, str]) -> dict[str, Any]:
    """The first candidate that leaves and enters the pins along their directions and touches nothing;
    refuses (``no_clear_route``) naming what is in the way."""
    rejected: list[str] = []
    blockers_seen: list[dict[str, Any]] = []
    for shape, points, free in candidates(p, q, dp, dq, snap, grid):
        points = _clean(points)
        if not conforms(points, dp, dq):
            continue
        drawn, reused = trim(points, world.wires)
        blockers = world.blockers(drawn, p, q, (drawn[0], drawn[-1])) if len(drawn) >= 2 else [{"type": "wire_overlap", "what": "wires already along the whole path", "at": se._round_pt(*p)}]
        if blockers:
            blockers_seen += [b for b in blockers if b not in blockers_seen]
            rejected.append(f"{shape} via {[se._round_pt(*pt) for pt in points[1:-1]]}: {blockers[0]['what']} at {blockers[0]['at']}")
            continue
        return {"shape": shape, "points": points, "drawn": drawn, "reused": reused, "free": [f for f in free if abs(f["asked"] - f["now"]) > 1e-9]}
    if blockers_seen:
        raise SheetAuthoringError(
            "no_clear_route",
            f"{where}: no orthogonal route from {names[0]} to {names[1]} that leaves and enters the pins along their directions "
            f"is clear; in the way: {'; '.join(rejected[:_MAX_BLOCKERS])}. Nothing was written. Pass 'via' waypoints to choose the path, "
            "or move the part in the way.",
            blockers=blockers_seen[:_MAX_BLOCKERS * 4])
    raise SheetAuthoringError(
        "no_route",
        f"{where}: no straight, L, Z or U route joins {names[0]} (leaves {dp}) and {names[1]} (leaves {dq}) with the wire leaving and entering "
        "along the pin directions (the pins face each other the wrong way round). Nothing was written. Pass 'via' waypoints to choose the path.")


def via_route(world: World, p: Point, q: Point, via: list[Point], snap: bool, grid: float, where: str, names: tuple[str, str]) -> dict[str, Any]:
    """The path through the caller's waypoints. A waypoint coordinate equal to one of the two pins' on the
    same axis is that pin's exact coordinate; any other is snapped to ``grid`` when ``snap``. Every leg must
    be horizontal or vertical."""
    free: list[dict[str, Any]] = []
    fixed_x = [p[0], q[0]]
    fixed_y = [p[1], q[1]]
    points = [p]
    for i, wp in enumerate(via):
        got = []
        for axis, value, fixed in (("x", wp[0], fixed_x), ("y", wp[1], fixed_y)):
            match = next((f for f in fixed if abs(f - value) <= PIN_LANDING_TOL), None)
            if match is not None:
                got.append(match)
                continue
            now = snap_value(value, grid) if snap else se._r(value)
            if abs(now - value) > 1e-9:
                free.append({"waypoint": i, "axis": axis, "asked": se._r(value), "now": now})
            got.append(now)
        points.append((got[0], got[1]))
    points.append(q)
    points = _clean(points)
    for a, b in zip(points, points[1:], strict=False):
        if _dir(a, b) is None:
            raise SheetAuthoringError(
                "via_not_orthogonal", f"{where}: the leg {se._round_pt(*a)} to {se._round_pt(*b)} of the path {names[0]} to {names[1]} is not horizontal or vertical "
                "(after snapping the waypoints); add a waypoint or correct one. Nothing was written.")
    drawn, reused = trim(points, world.wires)
    blockers = world.blockers(drawn, p, q, (drawn[0], drawn[-1])) if len(drawn) >= 2 else [{"type": "wire_overlap", "what": "wires already along the whole path", "at": se._round_pt(*p)}]
    if blockers:
        raise SheetAuthoringError(
            "no_clear_route", f"{where}: the path through your waypoints touches {blockers[0]['what']} at {blockers[0]['at']}"
            + (f" and {len(blockers) - 1} more" if len(blockers) > 1 else "") + ". Nothing was written.", blockers=blockers[:_MAX_BLOCKERS * 4])
    return {"shape": "via", "points": points, "drawn": drawn, "reused": reused, "free": free}


# --------------------------------------------------------------------------- endpoints


def parse_endpoint(spec: Any, where: str) -> dict[str, Any]:
    if isinstance(spec, str):
        ref, dot, pin = spec.partition(".")
        if not (ref and dot and pin):
            raise SheetAuthoringError("invalid_spec", f"{where}: a string endpoint must read 'REF.PIN' such as 'R1.2', got {spec!r}")
        return {"ref": ref, "pin": pin, "unit": None, "uuid": None}
    if not isinstance(spec, dict) or set(spec) - _ENDPOINT_KEYS:
        raise SheetAuthoringError("invalid_spec", f"{where} must be 'REF.PIN' or an object with keys from {sorted(_ENDPOINT_KEYS)}")
    ref = spec.get("ref", spec.get("reference"))
    uid = spec.get("uuid")
    pin = spec.get("pin")
    if isinstance(pin, int) and not isinstance(pin, bool):
        pin = str(pin)
    if (ref is None) == (uid is None) or not isinstance(pin, str) or not pin:
        raise SheetAuthoringError("invalid_spec", f"{where} needs exactly one of 'ref' and 'uuid', and a 'pin' (the pin number, or its name when that is unique)")
    for key, value in (("ref", ref), ("uuid", uid)):
        if value is not None and (not isinstance(value, str) or not value):
            raise SheetAuthoringError("invalid_spec", f"{where}.{key} must be a non-empty string")
    unit = spec.get("unit")
    if unit is not None and (isinstance(unit, bool) or not isinstance(unit, int) or unit < 1):
        raise SheetAuthoringError("invalid_spec", f"{where}.unit must be a positive integer")
    return {"ref": ref, "pin": pin, "unit": unit, "uuid": uid}


def resolve(sheet: se.Sheet, endpoint: dict[str, Any], where: str) -> dict[str, Any]:
    """The placed pin an endpoint names: symbol node, reference, unit, pin number and name, point, exit direction."""
    placed = [s for s in sheet.symbols if s.find_children("lib_id")]
    chosen = [s for s in placed if (se._uuid(s) == endpoint["uuid"] if endpoint["uuid"] else se._ref(s) == endpoint["ref"])]
    label = endpoint["uuid"] or endpoint["ref"]
    if not chosen:
        raise SheetAuthoringError("not_found", f"{where}: no placed symbol on this sheet has {'uuid' if endpoint['uuid'] else 'reference'} {label!r}", not_found=[label])
    hits: list[tuple[SExp, dict[str, Any], int]] = []
    listing: list[str] = []
    for sym in chosen:
        unit = (sym.find_children("unit")[0].get_int(0) if sym.find_children("unit") else 1) or 1
        if endpoint["unit"] is not None and unit != endpoint["unit"]:
            continue
        pins = se.pin_positions(sheet.root, sym, with_exit=True)
        listing += [f"{p['number']} ({p['name']}) unit {unit}" for p in pins]
        by_number = [p for p in pins if p["number"] == endpoint["pin"]]
        by_name = [p for p in pins if p["name"] == endpoint["pin"]] if not by_number else []
        hits += [(sym, p, unit) for p in (by_number or by_name)]
    if endpoint["unit"] is not None and not any(
            ((s.find_children("unit")[0].get_int(0) if s.find_children("unit") else 1) or 1) == endpoint["unit"] for s in chosen):
        raise SheetAuthoringError("pin_not_found", f"{where}: {label} has no placed unit {endpoint['unit']}", not_found=[f"{label} unit {endpoint['unit']}"])
    if not hits:
        raise SheetAuthoringError(
            "pin_not_found", f"{where}: {label} has no pin {endpoint['pin']!r} (number or name); its pins: {', '.join(listing[:40]) or 'none readable (no library symbol with pins on this sheet)'}",
            not_found=[f"{label}.{endpoint['pin']}"])
    distinct = {(se._key(p["x"], p["y"]), p["number"], u) for _s, p, u in hits}
    if len({d[0] for d in distinct}) > 1 or len({d[2] for d in distinct}) > 1:
        raise SheetAuthoringError(
            "pin_ambiguous", f"{where}: {label}.{endpoint['pin']} names {len(hits)} pins ("
            + ", ".join(f"unit {u} at {[p['x'], p['y']]}" for _s, p, u in hits[:6]) + "); say which with 'unit', or use the pin number / the symbol's uuid",
            candidates=[{"unit": u, "pin": p["number"], "name": p["name"], "at": [p["x"], p["y"]]} for _s, p, u in hits[:20]])
    sym, pin, unit = hits[0]
    ref = se._ref(sym)
    if ref.endswith("?"):
        raise SheetAuthoringError("unannotated_symbol", f"{where}: {ref} is not annotated; run fix_annotation first (the netlist check needs real references)")
    return {"symbol": sym, "reference": ref, "unit": unit, "number": pin["number"], "name": pin["name"], "point": (pin["x"], pin["y"]),
            "exit": tuple(pin["exit"]) if pin.get("exit") else None, "id": f"{ref}.{pin['number']}"}


# --------------------------------------------------------------------------- the tool


def _check_args(connections: Any, snap: Any, grid: Any) -> None:
    if not isinstance(connections, list) or not connections:
        raise SheetAuthoringError("invalid_spec", "connections must be a non-empty list of {from, to, via?, net_label?}")
    if not isinstance(snap, bool):
        raise SheetAuthoringError("invalid_spec", "snap must be true or false")
    if isinstance(grid, bool) or not isinstance(grid, (int, float)) or grid <= 0:
        raise SheetAuthoringError("invalid_spec", f"grid must be a positive number of mm, got {grid!r}")


def _parse_via(via: Any, where: str) -> list[Point]:
    if not isinstance(via, list) or not via or not all(
            isinstance(v, (list, tuple)) and len(v) == 2 and all(isinstance(c, (int, float)) and not isinstance(c, bool) for c in v) for v in via):
        raise SheetAuthoringError("invalid_spec", f"{where}.via must be a non-empty list of [x, y] waypoints in mm")
    return [(float(v[0]), float(v[1])) for v in via]


def _label_spot(points: list[Point], snap: bool, grid: float) -> tuple[Point, int]:
    """A point on the longest segment of the path, near its middle, on the grid when that stays on the segment."""
    a, b = max(zip(points, points[1:], strict=False), key=lambda s: math.hypot(s[1][0] - s[0][0], s[1][1] - s[0][1]))
    horizontal = abs(a[1] - b[1]) <= TOL
    lo, hi = (min(a[0], b[0]), max(a[0], b[0])) if horizontal else (min(a[1], b[1]), max(a[1], b[1]))
    mid = (lo + hi) / 2
    on = snap_value(mid, grid) if snap else mid
    if not lo + TOL < on < hi - TOL:
        on = mid
    on = se._r(on)
    return ((on, a[1]), 0) if horizontal else ((a[0], on), 90)


def connect_pins(path: str, connections: list[dict], root_sch: str | None, *, snap: bool, grid: float,
                 allow_connectivity_change: bool, dry_run: bool) -> dict[str, Any]:
    """Draw the wires of ``connections`` (see the tool text) and check connectivity."""

    def mutate(sheet: se.Sheet, root: SExp) -> se.Outcome:
        _check_args(connections, snap, grid)
        grid_mm = float(grid)
        parsed: list[tuple[int, dict, dict, dict, str | None]] = []
        for i, item in enumerate(connections):
            where = f"connections[{i}]"
            if not isinstance(item, dict) or set(item) - _CONNECTION_KEYS or "from" not in item or "to" not in item:
                raise SheetAuthoringError("invalid_spec", f"{where} must be an object with 'from' and 'to' and optionally 'via', 'net_label' (keys {sorted(_CONNECTION_KEYS)})")
            start = resolve(sheet, parse_endpoint(item["from"], f"{where}.from"), f"{where}.from")
            end = resolve(sheet, parse_endpoint(item["to"], f"{where}.to"), f"{where}.to")
            text = item.get("net_label")
            if text is not None and (not isinstance(text, str) or not text.strip()):
                raise SheetAuthoringError("invalid_spec", f"{where}.net_label must be a non-empty string")
            if start["id"] == end["id"] and start["unit"] == end["unit"]:
                raise SheetAuthoringError("invalid_spec", f"{where}: from and to are the same pin {start['id']}")
            if "via" in item:
                _parse_via(item["via"], where)
            parsed.append((i, item, start, end, text))
        world = World(sheet)
        pins_on_sheet = pin_points(root)
        net_of = {node: name for name, nodes in sheet.nets_before().items() for node in nodes}
        outcome = se.Outcome()
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        added_junctions: list[SExp] = []
        listed: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        made: list[tuple[tuple[str, str], tuple[str, str]]] = []
        for i, item, start, end, text in parsed:
            where = f"connections[{i}]"
            a_key, b_key = (start["reference"], start["number"]), (end["reference"], end["number"])
            for key, ep in ((a_key, start), (b_key, end)):
                if key not in net_of:
                    raise SheetAuthoringError(
                        "pin_not_in_netlist", f"{where}: pin {ep['id']} is not in kicad-cli's netlist of the project (a symbol that is not on the board, or a sheet used "
                        "more than once, whose per-use references differ from the sheet's own Reference property); the connection could not be checked, so it was not drawn")
            record: dict[str, Any] = {"index": i, "from": _describe(start), "to": _describe(end)}
            by_call = find(start["id"]) == find(end["id"])
            if by_call or net_of[a_key] == net_of[b_key]:
                entry = {**record, "reason": "already on one net" + (" (by an earlier connection of this call)" if by_call else ""), "net": net_of[a_key]}
                if text is not None:
                    entry["net_label_ignored"] = text
                skipped.append(entry)
                continue
            if "via" in item:
                plan = via_route(world, start["point"], end["point"], _parse_via(item["via"], where), snap, grid_mm, where, (start["id"], end["id"]))
            else:
                plan = plan_route(world, start["point"], start["exit"], end["point"], end["exit"], snap, grid_mm, where, (start["id"], end["id"]))
            drawn: list[Point] = plan["drawn"]
            crossing = world.crossed(drawn)
            wires = [wire_node(a, b) for a, b in zip(drawn, drawn[1:], strict=False)]
            for node in wires:
                root.children.append(node)
                outcome.added.add(se._uuid(node))
            record.update(shape=plan["shape"], path=[se._round_pt(*pt) for pt in plan["points"]], drawn=[se._round_pt(*pt) for pt in drawn],
                          segments=len(wires), wires=[se._uuid(n) for n in wires], reuses_wires=plan["reused"],
                          snapped=plan["free"], crosses_wires=crossing)
            record["off_grid_endpoints"] = [
                {"pin": ep["id"], "at": se._round_pt(*ep["point"])} for ep in (start, end)
                if not (on_grid(ep["point"][0], grid_mm) and on_grid(ep["point"][1], grid_mm))]
            if text is not None:
                spot, angle = _label_spot(drawn, snap, grid_mm)
                node = label_node(text.strip(), spot[0], spot[1], angle)
                root.children.append(node)
                outcome.added.add(se._uuid(node))
                record["net_label"] = {"text": text.strip(), "at": se._round_pt(*spot), "uuid": se._uuid(node)}
            # KiCad joins a wire end that lands on the middle of another wire only with a junction there
            # (measured: without one, kicad-cli's netlist keeps the nets apart), so the tool draws it.
            for pt in (drawn[0], drawn[-1]):
                inside = any(not (se._same(a, pt) or se._same(b, pt)) and se._segment_contains(a, b, pt) for _u, a, b in world.wires)
                have = any(se._same(pt, se._at(j)[:2]) for j in (*sheet.junctions, *added_junctions))
                if inside and not have:
                    node = junction_node(*pt)
                    root.children.append(node)
                    outcome.added.add(se._uuid(node))
                    added_junctions.append(node)
                    record.setdefault("junctions_added", []).append(se._round_pt(*pt))
            for a, b in zip(drawn, drawn[1:], strict=False):
                world.wires.append(("a wire drawn earlier in this call", a, b))
            parent[find(start["id"])] = find(end["id"])
            made.append((a_key, b_key))
            listed.append(record)
        junctions = _junction_candidates(listed, world, pins_on_sheet, sheet)
        outcome.no_change = not made
        outcome.compare = _compare(made, net_of)
        outcome.post_check = _post_check(made)
        outcome.check_nets = bool(made)
        outcome.report = {
            "connections": listed, "count": len(listed), "already_connected": skipped,
            "grid": grid_mm if snap else None,
            "grid_note": ("Pin points are never moved. Coordinates the tool chose itself (Z dogleg, U line, via waypoints) are on the grid"
                          if snap else "snap is false: the coordinates the tool chose itself are not rounded to a grid") + "; see snapped per connection and off_grid_endpoints.",
            "junction_candidates": [se._round_pt(*pt) for pt in junctions],
            "junction_note": "three or more wire ends or pins meet at (or a wire end meets the middle of a wire at) each listed point and no junction sits there; add_junctions draws the dot",
        }
        if not made:
            outcome.report["note"] = "every requested pair is already on one net; nothing was drawn"
        return outcome

    result = se.run(path, root_sch, mutate, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_connect"] = result.pop("connections")
    return result


def _junction_candidates(listed: list[dict[str, Any]], world: World, pins: list[Point], sheet: se.Sheet) -> list[Point]:
    """Points where a new wire starts or ends and three or more things meet (wire ends and pins count one
    each, a wire passing through counts two) with no junction there."""
    out: list[Point] = []
    for rec in listed:
        for pt in (tuple(rec["drawn"][0]), tuple(rec["drawn"][-1])):
            if any(se._same(pt, o) for o in out):
                continue
            count = sum(1 for e in pins if se._same(e, pt))
            for _uid, a, b in world.wires:
                if se._same(a, pt) or se._same(b, pt):
                    count += 1
                elif se._segment_contains(a, b, pt):
                    count += 2
            if count >= 3 and not any(se._same(pt, se._at(j)[:2]) for j in sheet.junctions):
                out.append(pt)
    return out


def _describe(ep: dict[str, Any]) -> dict[str, Any]:
    return {"reference": ep["reference"], "unit": ep["unit"], "pin": ep["number"], "name": ep["name"], "at": se._round_pt(*ep["point"]), "exit": list(ep["exit"]) if ep["exit"] else None}


def _post_check(made: list[tuple[tuple[str, str], tuple[str, str]]]):
    """The self-check: in kicad-cli's netlist of the edited sheet every requested pair is on one net."""
    def check(_before: dict[str, frozenset], after: dict[str, frozenset]) -> None:
        where = {node: name for name, nodes in after.items() for node in nodes}
        bad = [f"{a[0]}.{a[1]} and {b[0]}.{b[1]}" for a, b in made if where.get(a) is None or where.get(a) != where.get(b)]
        if bad:
            raise SheetAuthoringError(
                "write_not_verified", f"the netlist of the edited sheet does not put these requested pairs on one net: {'; '.join(bad[:10])}; nothing was written",
                pairs=bad[:se.MAX_LISTED])
    return check


def _compare(made: list[tuple[tuple[str, str], tuple[str, str]]], net_of: dict[tuple[str, str], str]):
    """The connectivity changes beyond the requested ones: a net that merged with another that no requested pair
    asked for, and two differently named nets joined (even when asked for)."""
    def compare(before: dict[str, frozenset], after: dict[str, frozenset], _removed: set[str]) -> list[dict]:
        parent: dict[str, str] = {}

        def find(x: str) -> str:
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for a, b in made:
            parent[find(net_of[a])] = find(net_of[b])
        changes: list[dict] = []
        groups: dict[str, set[str]] = {}
        for a, b in made:
            for key in (a, b):
                groups.setdefault(find(net_of[key]), set()).add(net_of[key])
        for names in groups.values():
            named = sorted(n for n in names if not se._is_auto(n))
            if len(named) > 1:
                changes.append({"type": "named_nets_joined", "nets": named, "note": "the requested connections join these differently named nets"})
        for name, nodes in after.items():
            were = {net_of[n] for n in nodes if n in net_of}
            if len({find(w) for w in were}) > 1:
                changes.append({"type": "merged", "nets": sorted(were), "now": name, "note": "no requested connection asked for this merge"})
        return changes
    return compare
