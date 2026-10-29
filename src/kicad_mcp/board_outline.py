"""Board outline and mounting holes with copper keepouts, as a checkpointed pair.

Contract: build ticket 19.

``propose_board_outline`` (dry run, writes nothing, returns a revision token) and
``confirm_board_outline`` (builds a NEW board revision in a temporary sibling directory,
reloads it fresh from disk, verifies, runs ``kicad-cli pcb drc`` for outline errors,
renames it into place). The source board is never touched. The checkpoint mechanics
come from the shared checkpoint helper (``kicad_mcp.checkpoint``).

Coordinates, the origin trap (CLAUDE.md hard rule 2):
``kicad_tools`` reports and accepts positions relative to the MIN CORNER OF THE OLD
Edge.Cuts outline (``PCB._board_origin``), and ``add_footprint`` silently adds that
offset. This module therefore never trusts those properties: every position is written
into, and read back from, the raw ``(at ...)`` / ``(start ...)`` nodes in file (page)
coordinates. Declared positions are in one of two named frames:

* ``origin="page"`` (default): file coordinates, exactly what the raw nodes hold.
* ``origin="aux_axis"``: relative to the board's ``(setup (aux_axis_origin x y))``, the
  user-set drill/place origin. Refused when the board has none.

Outline: one rectangle, optionally with rounded corners (``gr_line`` edges plus
``gr_arc`` corners). Every top-level Edge.Cuts graphic of the source is replaced;
footprint-embedded Edge.Cuts shapes are untouched. Footprints outside the new outline
are reported, never moved.

Mounting hole: a library footprint at the declared position plus a rule-area (keepout)
zone on every copper layer that forbids tracks, vias and copper pour within
``keepout_radius_mm`` (a 32-gon circumscribing that circle, so it is never undersized).
"""

from __future__ import annotations

import copy
import math
import os
import re
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from kicad_mcp import _self_check, checkpoint, input_prep, write_layer
from kicad_mcp import verify_board as gate

for _name in ("_top_level", "_parse"):
    if not hasattr(input_prep, _name):  # private helpers reused on purpose
        raise ImportError(f"board_outline reuses kicad_mcp.input_prep.{_name}, which no longer exists")

_EDGE_LAYER = "Edge.Cuts"
_EDGE_KINDS = ("gr_line", "gr_rect", "gr_arc", "gr_circle", "gr_poly", "gr_curve")
_OUTLINE_VIOLATIONS = {"invalid_outline", "malformed_outline"}
_STROKE_WIDTH = 0.1
_KEEPOUT_SIDES = 32
_EPS = 1e-6

_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())  # token -> {"out_dir", "plan_args", "plan", "cache"}
_DRIFT = "the board changed since the proposal was made: re-run propose_board_outline"

_POUR_NOTE = (
    "copper pours listed under zone_overlaps are carved by the new copperpour keepouts, but their "
    "stored fill is stale: the pour needs refilling (kicad-cli refill / the pour step) before any "
    "pour or DRC figure for this revision means anything"
)


# --- small raw-node readers -------------------------------------------------------


def _xy(node: Any, tag: str) -> tuple[float, float] | None:
    child = node.find(tag)
    if child is None or len(child.values) < 2:
        return None
    return float(child.values[0]), float(child.values[1])


def _layer(node: Any) -> str:
    child = node.find("layer")
    return str(child.values[0]) if child is not None and child.values else ""


def _fp_reference(fp: Any) -> str:
    for child in fp.children:
        if child.name == "property" and len(child.values) >= 2 and str(child.values[0]) == "Reference":
            return str(child.values[1])
    for child in fp.children:
        if child.name == "fp_text" and len(child.values) >= 2 and str(child.values[0]) == "reference":
            return str(child.values[1])
    return ""


def _aux_origin(root: Any) -> tuple[float, float] | None:
    setup = root.find("setup")
    return _xy(setup, "aux_axis_origin") if setup is not None else None


def _copper_layers(root: Any) -> list[str]:
    layers = root.find("layers")
    found = [
        str(c.values[0]) if c.values else str(c.name)
        for c in (layers.children if layers is not None else [])
    ]
    cu = [name for name in found if name.endswith(".Cu")]
    return cu or ["F.Cu", "B.Cu"]


# --- footprint library --------------------------------------------------------------


_ENV_DIR = re.compile(r"^KICAD\d*_FOOTPRINT_DIR$")
_VAR = re.compile(r"\$\{([^}]+)\}")


def _footprint_dirs() -> list[Path]:
    """Directories that contain ``<Library>.pretty`` folders: every ``KICAD*_FOOTPRINT_DIR``
    env var, then the installed KiCad's share directory."""
    dirs: list[Path] = []
    for name in sorted(os.environ):
        if _ENV_DIR.match(name) and os.environ[name]:
            dirs.append(Path(os.environ[name]))
    try:
        from kicad_mcp.kicad_cli import find_kicad_cli

        dirs.append(find_kicad_cli().parent.parent / "share" / "kicad" / "footprints")
    except Exception:  # noqa: BLE001
        pass
    return dirs


def _expand(uri: str, project_dir: Path | None) -> Path:
    def sub(match: re.Match) -> str:
        key = match.group(1)
        if key == "KIPRJMOD" and project_dir is not None:
            return str(project_dir)
        if key in os.environ:
            return os.environ[key]
        if _ENV_DIR.match(key):  # KiCad's own footprint dir variable, unset outside the KiCad GUI
            dirs = _footprint_dirs()
            if dirs:
                return str(dirs[-1])
        return match.group(0)

    return Path(_VAR.sub(sub, uri))


def _table_libraries(table: Path, project_dir: Path | None, depth: int = 0) -> dict[str, Path]:
    """``nickname -> directory`` from an fp-lib-table (nested ``Table`` entries one level deep)."""
    found: dict[str, Path] = {}
    if depth > 2 or not table.is_file():
        return found
    try:
        root = input_prep._parse(table)
    except Exception:  # noqa: BLE001
        return found
    for lib in root.children:
        if lib.name != "lib":
            continue
        name, kind, uri = lib.find("name"), lib.find("type"), lib.find("uri")
        if name is None or uri is None or not name.values or not uri.values:
            continue
        target = _expand(str(uri.values[0]), project_dir)
        if kind is not None and kind.values and str(kind.values[0]) == "Table":
            for nick, path in _table_libraries(target, project_dir, depth + 1).items():
                found.setdefault(nick, path)
        else:
            found.setdefault(str(name.values[0]), target)
    return found


_TABLE_CACHE: dict[tuple, dict[str, Path]] = {}


def _cached_table(table: Path, project_dir: Path | None) -> dict[str, Path]:
    """``_table_libraries`` keyed by file, mtime and project (the standard table is large)."""
    try:
        stamp = table.stat().st_mtime_ns if table.is_file() else None
    except OSError:
        stamp = None
    key = (str(table), stamp, str(project_dir), tuple(sorted(k for k in os.environ if _ENV_DIR.match(k))))
    if key not in _TABLE_CACHE:
        _TABLE_CACHE[key] = _table_libraries(table, project_dir)
    return _TABLE_CACHE[key]


def _library_tables(project_dir: Path | None) -> list[Path]:
    tables: list[Path] = []
    if project_dir is not None:
        tables.append(project_dir / "fp-lib-table")
    appdata = os.environ.get("APPDATA")
    config = Path(appdata) / "kicad" if appdata else Path.home() / ".config" / "kicad"
    if config.is_dir():
        tables += sorted(config.glob("*/fp-lib-table"), reverse=True)
    return tables


def _safe_part(part: str) -> bool:
    return bool(part) and part not in (".", "..") and not any(sep in part for sep in ("/", "\\", ":"))


def _footprint_file(footprint: str, project_dir: Path | None = None) -> Path | None:
    """The ``.kicad_mod`` for ``Nickname:Name`` (or an existing file path), else None.

    KiCad semantics: the first fp-lib-table (project, then user) that HAS the nickname decides;
    a miss in that table's directory is a miss, with no fall-through to other libraries. Only
    nicknames that no table declares fall back to ``<Nickname>.pretty`` under every
    ``KICAD*_FOOTPRINT_DIR`` and the installed KiCad footprint directory. The nickname and
    the footprint name must be plain names (no path separators, no ``..``)."""
    direct = Path(footprint)
    if direct.suffix == ".kicad_mod" and direct.is_file():
        return direct
    if ":" not in footprint:
        return None
    library, name = footprint.split(":", 1)
    if not (_safe_part(library) and _safe_part(name)):
        return None
    for table in _library_tables(project_dir):
        directory = _cached_table(table, project_dir).get(library)
        if directory is not None and directory.is_dir():  # an unresolvable entry declares nothing
            candidate = directory / f"{name}.kicad_mod"
            return candidate if candidate.is_file() else None
    for base in _footprint_dirs():
        for folder in (f"{library}.pretty", library):
            candidate = base / folder / f"{name}.kicad_mod"
            if candidate.is_file():
                return candidate
    return None


def _hole_extent_radius(mod_path: Path) -> float:
    """Largest drill or pad radius of a library footprint, from its raw nodes."""
    root = input_prep._parse(mod_path)
    radius = 0.0
    for pad in root.children:
        if pad.name != "pad":
            continue
        for tag in ("drill", "size"):
            node = pad.find(tag)
            if node is None:
                continue
            nums = [float(v) for v in node.values if _is_number(v)]
            if nums:
                radius = max(radius, max(nums) / 2)
    return radius


def _is_number(value: Any) -> bool:
    """A real, finite int/float. Booleans, strings, NaN and infinities are not numbers."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


# --- geometry -----------------------------------------------------------------------


def _inside(rect: dict, corner: float, x: float, y: float, margin: float = 0.0) -> bool:
    """Is the disc of radius ``margin`` at (x, y) inside the (rounded) rectangle?

    The disc fits when its centre lies in the outline shrunk by ``margin``. Shrinking a
    rectangle with corner radius ``corner`` gives the rectangle inset by ``margin`` with corner
    radius ``max(corner - margin, 0)`` around the SAME corner centres: once the margin reaches
    the corner radius, the inset rectangle's corners are sharp and the straight-edge test above
    is the whole answer (build ticket 52: ``hypot + margin <= corner`` refused such a disc
    anywhere, since ``dx = dy = 0`` away from the corners still left ``margin > corner``)."""
    x0, y0 = rect["x"], rect["y"]
    x1, y1 = x0 + rect["width"], y0 + rect["height"]
    if x < x0 + margin - _EPS or x > x1 - margin + _EPS or y < y0 + margin - _EPS or y > y1 - margin + _EPS:
        return False
    if corner <= 0:
        return True
    dx = max(x0 + corner - x, 0.0, x - (x1 - corner))  # distance past the nearest corner centre
    dy = max(y0 + corner - y, 0.0, y - (y1 - corner))
    return math.hypot(dx, dy) <= max(corner - margin, 0.0) + _EPS


def _footprint_points(fp: Any) -> list[tuple[float, float]]:
    """The footprint origin plus the bounding-square corners of every pad, in file coordinates."""
    at = fp.find("at")
    if at is None or len(at.values) < 2:
        return []
    ox, oy = float(at.values[0]), float(at.values[1])
    angle = math.radians(float(at.values[2])) if len(at.values) > 2 and _is_number(at.values[2]) else 0.0
    cos, sin = math.cos(angle), math.sin(angle)
    points = [(ox, oy)]
    for pad in fp.children:
        if pad.name != "pad":
            continue
        local = _xy(pad, "at")
        size = pad.find("size")
        if local is None or size is None or not size.values:
            continue
        half = max(float(v) for v in size.values[:2]) / 2
        # KiCad's y axis points down: positive angles rotate counter-clockwise on screen.
        px = ox + local[0] * cos + local[1] * sin
        py = oy - local[0] * sin + local[1] * cos
        points += [(px + sx * half, py + sy * half) for sx in (-1, 1) for sy in (-1, 1)]
    return points


def _outside_footprints(root: Any, rect: dict, corner: float, skip: set[str]) -> list[dict]:
    outside: list[dict] = []
    for fp in root.children:
        if fp.name != "footprint":
            continue
        ref = _fp_reference(fp)
        if ref in skip:
            continue
        points = _footprint_points(fp)
        bad = [p for p in points if not _inside(rect, corner, p[0], p[1])]
        if bad:
            outside.append(
                {
                    "reference": ref,
                    "at": list(points[0]),
                    "origin_outside": not _inside(rect, corner, points[0][0], points[0][1]),
                    "points_outside": len(bad),
                }
            )
    return outside


# --- outline / keepout nodes ----------------------------------------------------------


def _stroke() -> Any:
    from kicad_tools.sexp import SExp

    return SExp.list("stroke", SExp.list("width", _STROKE_WIDTH), SExp.list("type", "default"))


def _outline_nodes(rect: dict, corner: float) -> list[Any]:
    from kicad_tools.sexp import SExp

    x0, y0 = rect["x"], rect["y"]
    x1, y1 = x0 + rect["width"], y0 + rect["height"]
    c = corner

    def line(a: tuple[float, float], b: tuple[float, float]) -> Any:
        if abs(a[0] - b[0]) < 1e-9 and abs(a[1] - b[1]) < 1e-9:
            return None  # corner_radius == half the short side leaves a zero-length edge
        return SExp.list(
            "gr_line",
            SExp.list("start", *a),
            SExp.list("end", *b),
            _stroke(),
            SExp.list("layer", _EDGE_LAYER),
            SExp.list("uuid", str(uuid.uuid4())),
        )

    def arc(start: tuple[float, float], mid: tuple[float, float], end: tuple[float, float]) -> Any:
        return SExp.list(
            "gr_arc",
            SExp.list("start", *start),
            SExp.list("mid", *mid),
            SExp.list("end", *end),
            _stroke(),
            SExp.list("layer", _EDGE_LAYER),
            SExp.list("uuid", str(uuid.uuid4())),
        )

    def r(v: float) -> float:
        return round(v, 6)

    if c <= 0:
        corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        return [line(corners[i], corners[(i + 1) % 4]) for i in range(4)]

    d = c * (1 - math.cos(math.pi / 4))  # mid point offset from the sharp corner
    nodes = [
        line((r(x0 + c), y0), (r(x1 - c), y0)),
        arc((r(x1 - c), y0), (r(x1 - d), r(y0 + d)), (x1, r(y0 + c))),
        line((x1, r(y0 + c)), (x1, r(y1 - c))),
        arc((x1, r(y1 - c)), (r(x1 - d), r(y1 - d)), (r(x1 - c), y1)),
        line((r(x1 - c), y1), (r(x0 + c), y1)),
        arc((r(x0 + c), y1), (r(x0 + d), r(y1 - d)), (x0, r(y1 - c))),
        line((x0, r(y1 - c)), (x0, r(y0 + c))),
        arc((x0, r(y0 + c)), (r(x0 + d), r(y0 + d)), (r(x0 + c), y0)),
    ]
    return [n for n in nodes if n is not None]


def _keepout_vertex_radius(radius: float) -> float:
    return radius / math.cos(math.pi / _KEEPOUT_SIDES)


def _keepout_node(name: str, cx: float, cy: float, radius: float, layers: list[str]) -> Any:
    from kicad_tools.sexp import SExp

    vr = _keepout_vertex_radius(radius)
    pts = SExp.list("pts")
    for i in range(_KEEPOUT_SIDES):
        a = 2 * math.pi * i / _KEEPOUT_SIDES
        pts.append(SExp.list("xy", round(cx + vr * math.cos(a), 6), round(cy + vr * math.sin(a), 6)))
    return SExp.list(
        "zone",
        SExp.list("net", 0),
        SExp.list("net_name", ""),
        SExp.list("layers", *layers),
        SExp.list("uuid", str(uuid.uuid4())),
        SExp.list("name", name),
        SExp.list("hatch", "edge", 0.5),
        SExp.list("connect_pads", SExp.list("clearance", 0)),
        SExp.list("min_thickness", 0.25),
        SExp.list(
            "keepout",
            SExp.list("tracks", "not_allowed"),
            SExp.list("vias", "not_allowed"),
            SExp.list("pads", "allowed"),
            SExp.list("copperpour", "not_allowed"),
            SExp.list("footprints", "allowed"),
        ),
        SExp.list("fill", SExp.list("thermal_gap", 0.5), SExp.list("thermal_bridge_width", 0.5)),
        SExp.list("polygon", pts),
    )


# --- cutouts and overlaps -------------------------------------------------------------


def _item_points(node: Any) -> list[tuple[float, float]]:
    pts = [p for p in (_xy(node, t) for t in ("start", "mid", "end", "center")) if p is not None]
    poly = node.find("pts")
    if poly is not None:
        pts += [
            (float(c.values[0]), float(c.values[1]))
            for c in poly.children
            if c.name == "xy" and len(c.values) >= 2
        ]
    return pts


def _uuid(node: Any) -> str:
    child = node.find("uuid")
    return str(child.values[0]) if child is not None and child.values else ""


_SNAP = 1e-4


def _edge_cuts_loops(root: Any) -> dict | None:
    """Group the top-level Edge.Cuts items into closed-loop candidates. Endpoints closer than
    ``_SNAP`` are the same point (grid hashing, so a rounding boundary cannot split them).
    Returns None when there is at most one loop; otherwise the outer loop is the one with the
    largest bounding box (an explicit rule), the rest are reported."""
    items = [n for n in root.children if n.name in _EDGE_KINDS and _layer(n) == _EDGE_LAYER]
    parent = list(range(len(items)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cell = 1e-3
    grid: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
    for i, node in enumerate(items):
        if node.name not in ("gr_line", "gr_arc"):
            continue
        for tag in ("start", "end"):
            pt = _xy(node, tag)
            if pt is None:
                continue
            gx, gy = int(math.floor(pt[0] / cell)), int(math.floor(pt[1] / cell))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for qx, qy, j in grid.get((gx + dx, gy + dy), ()):
                        if abs(qx - pt[0]) <= _SNAP and abs(qy - pt[1]) <= _SNAP:
                            parent[find(i)] = find(j)
            grid.setdefault((gx, gy), []).append((pt[0], pt[1], i))
    loops: dict[int, list[int]] = {}
    for i in range(len(items)):
        loops.setdefault(find(i), []).append(i)
    if len(loops) < 2:
        return None

    def bbox(members: list[int]) -> tuple[float, float, float, float]:
        pts = [p for i in members for p in _item_points(items[i])]
        if not pts:
            return (0.0, 0.0, 0.0, 0.0)
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs), min(ys), max(xs), max(ys))

    def area(box: tuple[float, float, float, float]) -> float:
        return (box[2] - box[0]) * (box[3] - box[1])

    members_list = list(loops.values())
    boxes = [bbox(m) for m in members_list]
    outer_index = max(range(len(members_list)), key=lambda k: area(boxes[k]))
    ob = boxes[outer_index]
    ambiguous = False
    for k, box in enumerate(boxes):
        if k == outer_index:
            continue
        nested = box[0] >= ob[0] - _SNAP and box[1] >= ob[1] - _SNAP and box[2] <= ob[2] + _SNAP and box[3] <= ob[3] + _SNAP
        if not nested or abs(area(box) - area(ob)) <= _SNAP:
            ambiguous = True
    cutouts = [
        {
            "kind": items[i].name,
            "uuid": _uuid(items[i]),
            "start": list(_xy(items[i], "start") or _xy(items[i], "center") or []),
            "end": list(_xy(items[i], "end") or []),
        }
        for k, members in enumerate(members_list)
        if k != outer_index
        for i in members
    ]
    return {"loops": len(members_list), "cutouts": cutouts, "ambiguous": ambiguous}


def _point_segment(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _point_rect(p: tuple[float, float], box: tuple[float, float, float, float]) -> float:
    x0, y0, x1, y1 = box
    return math.hypot(max(x0 - p[0], 0.0, p[0] - x1), max(y0 - p[1], 0.0, p[1] - y1))


def _point_in_ring(p: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    inside = False
    for i in range(len(ring)):
        (x1, y1), (x2, y2) = ring[i], ring[(i + 1) % len(ring)]
        if (y1 > p[1]) != (y2 > p[1]) and p[0] < (x2 - x1) * (p[1] - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _fp_transform(fp: Any) -> Any:
    at = fp.find("at")
    ox, oy = float(at.values[0]), float(at.values[1])
    angle = math.radians(float(at.values[2])) if len(at.values) > 2 and _is_number(at.values[2]) else 0.0
    cos, sin = math.cos(angle), math.sin(angle)
    return lambda lx, ly: (ox + lx * cos + ly * sin, oy - lx * sin + ly * cos)


def _courtyard_box(fp: Any) -> tuple[float, float, float, float] | None:
    if fp.find("at") is None:
        return None
    to_abs = _fp_transform(fp)
    pts: list[tuple[float, float]] = []
    for node in fp.children:
        if (node.name or "").startswith("fp_") and _layer(node) in ("F.CrtYd", "B.CrtYd"):
            pts += [to_abs(*p) for p in _item_points(node)]
    if not pts:
        return None
    xs, ys = [p[0] for p in pts], [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _pad_boxes(fp: Any) -> list[tuple[float, float, float, float]]:
    if fp.find("at") is None:
        return []
    to_abs = _fp_transform(fp)
    boxes = []
    for pad in fp.children:
        if pad.name != "pad":
            continue
        local, size = _xy(pad, "at"), pad.find("size")
        if local is None or size is None or not size.values:
            continue
        half = max(float(v) for v in size.values[:2]) / 2
        cx, cy = to_abs(*local)
        boxes.append((cx - half, cy - half, cx + half, cy + half))
    return boxes


def _zone_ring(node: Any) -> list[tuple[float, float]]:
    poly = node.find("polygon")
    pts = poly.find("pts") if poly is not None else None
    if pts is None:
        return []
    return [(float(x.values[0]), float(x.values[1])) for x in pts.children if x.name == "xy"]


def _footprint_geometry(root: Any) -> list[dict]:
    """Pad and courtyard boxes of every footprint, computed once per board."""
    return [
        {"reference": _fp_reference(fp), "pads": _pad_boxes(fp), "courtyard": _courtyard_box(fp)}
        for fp in root.children
        if fp.name == "footprint"
    ]


def _arc_polyline(
    a: tuple[float, float], m: tuple[float, float], b: tuple[float, float], segments: int = 24
) -> list[tuple[float, float]]:
    """Points along the circular arc a -> m -> b (straight chords when the three are collinear)."""
    d = 2 * (a[0] * (m[1] - b[1]) + m[0] * (b[1] - a[1]) + b[0] * (a[1] - m[1]))
    if abs(d) < 1e-12:
        return [a, m, b]
    ux = ((a[0] ** 2 + a[1] ** 2) * (m[1] - b[1]) + (m[0] ** 2 + m[1] ** 2) * (b[1] - a[1]) + (b[0] ** 2 + b[1] ** 2) * (a[1] - m[1])) / d
    uy = ((a[0] ** 2 + a[1] ** 2) * (b[0] - m[0]) + (m[0] ** 2 + m[1] ** 2) * (a[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (m[0] - a[0])) / d
    radius = math.hypot(a[0] - ux, a[1] - uy)
    ang = lambda p: math.atan2(p[1] - uy, p[0] - ux)  # noqa: E731
    start, mid, end = ang(a), ang(m), ang(b)
    two_pi = 2 * math.pi
    to_mid = (mid - start) % two_pi
    to_end = (end - start) % two_pi
    sweep = to_end if to_mid <= to_end else to_end - two_pi  # go the way that passes through m
    return [
        (ux + radius * math.cos(start + sweep * k / segments), uy + radius * math.sin(start + sweep * k / segments))
        for k in range(segments + 1)
    ]


def _overlaps(root: Any, holes: list[dict], geometry: list[dict] | None = None) -> tuple[list[dict], list[dict]]:
    """``(hard, pours)``. ``hard``: existing items a hole's keepout (its circumscribing polygon)
    touches and that stay a conflict: pads, courtyards, tracks, arcs, vias, rule-area zones, other
    holes. ``pours``: copper-pour zones under a keepout, informational (the keepout carves them)."""
    hard: list[dict] = []
    pours: list[dict] = []
    if not holes:
        return hard, pours
    geometry = geometry if geometry is not None else _footprint_geometry(root)
    for hole in holes:
        c = (hole["at"][0], hole["at"][1])
        vr = _keepout_vertex_radius(hole["keepout_radius_mm"])

        def add(kind: str, item: str, **extra: Any) -> None:
            hard.append({"hole": hole["reference"], "kind": kind, "item": item, **extra})

        for fp in geometry:
            via = None
            if any(_point_rect(c, box) <= vr for box in fp["pads"]):
                via = "pad"
            elif fp["courtyard"] is not None and _point_rect(c, fp["courtyard"]) <= vr:
                via = "courtyard"
            if via:
                add("footprint", fp["reference"], via=via)
        for node in root.children:
            if node.name == "via":
                at, size = _xy(node, "at"), node.find("size")
                if at and size is not None and size.values:
                    if math.hypot(c[0] - at[0], c[1] - at[1]) <= vr + float(size.values[0]) / 2:
                        add("via", _uuid(node))
            elif node.name == "zone":
                ring = _zone_ring(node)
                if not ring:
                    continue
                touches = _point_in_ring(c, ring) or any(
                    _point_segment(c, ring[i], ring[(i + 1) % len(ring)]) <= vr for i in range(len(ring))
                )
                if not touches:
                    continue
                name = node.find("name")
                label = str(name.values[0]) if name is not None and name.values else ""
                if node.find("keepout") is not None:
                    add("zone", _uuid(node), name=label, keepout=True)
                else:
                    net = node.find("net_name")
                    pours.append(
                        {
                            "hole": hole["reference"],
                            "kind": "pour",
                            "item": _uuid(node),
                            "name": label,
                            "net": str(net.values[0]) if net is not None and net.values else "",
                            "layer": _layer(node),
                        }
                    )
            elif node.name in ("segment", "arc"):
                a, b = _xy(node, "start"), _xy(node, "end")
                mid = _xy(node, "mid")
                if not (a and b):
                    continue
                poly = _arc_polyline(a, mid, b) if mid else [a, b]
                width_node = node.find("width")
                half = float(width_node.values[0]) / 2 if width_node is not None and width_node.values else 0.0
                if any(_point_segment(c, poly[k], poly[k + 1]) <= vr + half for k in range(len(poly) - 1)):
                    add("track", _uuid(node), layer=_layer(node))
    for i, hole in enumerate(holes):
        for other in holes[:i]:
            gap = math.hypot(hole["at"][0] - other["at"][0], hole["at"][1] - other["at"][1])
            need = _keepout_vertex_radius(hole["keepout_radius_mm"]) + _keepout_vertex_radius(
                other["keepout_radius_mm"]
            )
            if gap < need:
                hard.append({"hole": hole["reference"], "kind": "mounting_hole", "item": other["reference"]})
    return hard, pours


# --- planning -------------------------------------------------------------------------


def _refused(reason: str, **extra: Any) -> dict:
    return {"status": "refused", "reason": reason, "revision_token": None, **extra}


def _validate_outline(outline: Any) -> tuple[dict | None, float, str | None]:
    if not isinstance(outline, dict):
        return None, 0.0, "outline must be an object with x, y, width, height"
    for key in ("x", "y", "width", "height"):
        if not _is_number(outline.get(key)):
            return None, 0.0, f"outline.{key} is required and must be a number"
    rect = {k: float(outline[k]) for k in ("x", "y", "width", "height")}
    if rect["width"] <= 0 or rect["height"] <= 0:
        return None, 0.0, "outline width and height must be positive"
    corner = outline.get("corner_radius", 0.0)
    if not _is_number(corner) or float(corner) < 0:
        return None, 0.0, "outline.corner_radius must be a number >= 0"
    corner = float(corner)
    if corner > min(rect["width"], rect["height"]) / 2 + _EPS:
        return None, 0.0, "outline.corner_radius exceeds half of the shorter side"
    return rect, corner, None


def _plan(
    pcb_path: Path,
    outline: Any,
    mounting_holes: Any,
    origin: str,
    allow_overlap: bool = False,
    cache: dict | None = None,
) -> tuple[dict | None, dict | None]:
    """``(plan, None)`` or ``(None, refusal)``. Reads the board only. ``cache`` (a dict the caller
    keeps for one source revision) holds the per-footprint pad/courtyard boxes, so they are
    computed once per propose and reused at confirm."""
    rect, corner, problem = _validate_outline(outline)
    if problem:
        return None, _refused(problem)
    if origin not in ("page", "aux_axis"):
        return None, _refused("origin must be 'page' or 'aux_axis'")
    try:
        root = input_prep._parse(pcb_path)
    except Exception as exc:  # noqa: BLE001
        return None, {"status": "incomplete", "reason": f"failed to parse board: {exc}", "revision_token": None}

    if mounting_holes is not None and not isinstance(mounting_holes, list):
        return None, _refused("mounting_holes must be a list of objects")
    loops = _edge_cuts_loops(root)
    if loops is not None:
        n, cutouts = loops["loops"], loops["cutouts"]
        if loops["ambiguous"]:
            reason = (
                f"loops found: {n}; the existing Edge.Cuts is ambiguous: the largest loop by bbox does "
                f"not enclose the other {len(cutouts)} item(s), so the board outline cannot be told apart "
                "from a second shape. Nothing was changed; remove the stray Edge.Cuts item(s) listed under "
                "cutouts (or the whole stray loop) first"
            )
        else:
            reason = (
                f"loops found: {n}; the largest by bbox is treated as the outer outline; the following "
                f"{len(cutouts)} item(s) are inside it and would be deleted as internal cutouts or slots "
                "(preserving cutouts is not supported). If the largest loop is not the board outline, "
                "remove that decorative item first"
            )
        return None, _refused(reason, cutouts=cutouts, outline_ambiguous=loops["ambiguous"])
    offset = (0.0, 0.0)
    if origin == "aux_axis":
        aux = _aux_origin(root)
        if aux is None:
            return None, _refused("origin 'aux_axis' needs (setup (aux_axis_origin ...)) and the board has none")
        offset = aux
    absolute = {"x": rect["x"] + offset[0], "y": rect["y"] + offset[1], "width": rect["width"], "height": rect["height"]}

    existing_refs = {_fp_reference(fp) for fp in root.children if fp.name == "footprint"}
    previous_keepouts = {
        str(z.find("name").values[0])
        for z in root.children
        if z.name == "zone" and z.find("keepout") is not None and z.find("name") is not None and z.find("name").values
    }
    holes: list[dict] = []
    refused: list[dict] = []
    seen: set[str] = set()
    for spec in mounting_holes or []:
        ref = str(spec.get("reference", "")) if isinstance(spec, dict) else ""
        if not ref:
            return None, _refused("every mounting hole needs a 'reference'")
        why = None
        mod = None
        if f"KEEPOUT_{ref}" in previous_keepouts:
            why = (
                f"reference {ref} is already a mounting hole from a previous run (keepout zone "
                f"KEEPOUT_{ref}); holes and keepouts cannot be redeclared on an output revision: "
                "re-run from the original board, or declare only new references"
            )
        elif ref in existing_refs or ref in seen:
            why = f"reference {ref} is already used on the board or declared twice"
        elif not (_is_number(spec.get("x")) and _is_number(spec.get("y"))):
            why = f"hole {ref}: x and y are required numbers"
        elif not _is_number(spec.get("keepout_radius_mm")) or float(spec["keepout_radius_mm"]) <= 0:
            why = f"hole {ref}: keepout_radius_mm is required and must be positive"
        else:
            fp_name = str(spec.get("footprint") or "MountingHole:MountingHole_3.2mm_M3")
            mod = _footprint_file(fp_name, pcb_path.parent)
            if mod is None:
                why = f"hole {ref}: footprint {fp_name} not found in the KiCad footprint libraries"
        seen.add(ref)
        if why is None:
            radius = float(spec["keepout_radius_mm"])
            needed = _hole_extent_radius(mod)
            x, y = float(spec["x"]) + offset[0], float(spec["y"]) + offset[1]
            if radius + _EPS < needed:
                why = f"hole {ref}: keepout radius {radius} mm is smaller than the hole itself ({needed} mm)"
            elif not _inside(absolute, corner, x, y, _keepout_vertex_radius(radius)):
                why = f"hole {ref}: hole plus keepout of radius {radius} mm is not inside the new outline"
            if why is None:
                holes.append(
                    {
                        "reference": ref,
                        "footprint": fp_name,
                        "footprint_file": str(mod),
                        "at": [x, y],
                        "keepout_radius_mm": radius,
                    }
                )
                continue
            refused.append({"reference": ref, "at": [x, y], "keepout_radius_mm": radius, "reason": why})
            continue
        refused.append({"reference": ref, "reason": why})
    if refused:
        names = ", ".join(r["reference"] for r in refused)
        detail = "; ".join(r["reason"] for r in refused)
        return None, _refused(f"mounting hole(s) refused: {names}. {detail}", refused_holes=refused)

    replaced = Counter()
    for node in root.children:
        if node.name in _EDGE_KINDS and _layer(node) == _EDGE_LAYER:
            replaced[node.name] += 1
    hole_refs = {h["reference"] for h in holes}
    outside = _outside_footprints(root, absolute, corner, hole_refs)
    geometry = None
    if holes:
        if cache is not None:
            if "geometry" not in cache:
                cache["geometry"] = _footprint_geometry(root)
            geometry = cache["geometry"]
        else:
            geometry = _footprint_geometry(root)
    overlaps, zone_overlaps = _overlaps(root, holes, geometry)
    if overlaps and not allow_overlap:
        names = sorted({o["hole"] for o in overlaps})
        listing = "; ".join(f"{o['hole']} overlaps {o['kind']} {o['item']}" for o in overlaps[:20])
        return None, _refused(
            f"mounting hole(s) {', '.join(names)} overlap existing items: {listing}"
            + (" ..." if len(overlaps) > 20 else "")
            + ". Move the hole(s) or pass allow_overlap=true to proceed",
            overlaps=overlaps,
        )
    return (
        {
            "rect": absolute,
            "corner_radius": corner,
            "holes": holes,
            "replaces": dict(replaced),
            "outside_outline": outside,
            "overlaps": overlaps,
            "zone_overlaps": zone_overlaps,
            "origin": {"frame": origin, "offset": [offset[0], offset[1]]},
        },
        None,
    )


# --- checkpoint -------------------------------------------------------------------------


def _default_output_dir(pcb_path: Path) -> Path:
    resolved = pcb_path.resolve()
    return resolved.parent.parent / f"{resolved.parent.name}_outlined"


def propose_board_outline(
    pcb_path: str | Path,
    outline: dict,
    mounting_holes: list[dict] | None = None,
    origin: str = "page",
    output_dir: str | Path | None = None,
    allow_overlap: bool = False,
) -> dict:
    """Dry run: validate the outline and every mounting hole, list what would be replaced and
    which footprints fall outside the new outline; write nothing. Returns a revision token.

    ``outline``: ``{x, y, width, height, corner_radius?}`` in the ``origin`` frame.
    ``mounting_holes``: ``[{reference, x, y, keepout_radius_mm, footprint?}]`` (default
    footprint ``MountingHole:MountingHole_3.2mm_M3``). A hole whose keepout touches an existing
    footprint (pads, courtyard), track, via, zone or another hole is refused with the items named
    unless ``allow_overlap`` is true; an outline that would delete internal Edge.Cuts cutouts is
    always refused. Copper pours under a keepout are listed as ``zone_overlaps`` (not refused).

    Re-applying: calling this on an output revision with only a new ``outline`` (no holes) works;
    holes and keepouts from a previous run cannot be redeclared there (a reference that is
    already a hole is refused naming its ``KEEPOUT_`` zone): start again from the original board.
    """
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}", "revision_token": None}
    plan_args = copy.deepcopy((outline, mounting_holes, origin, bool(allow_overlap)))
    cache: dict = {}
    plan, refusal = _plan(src, *plan_args, cache=cache)
    if refusal is not None:
        return refusal
    out_dir = Path(output_dir) if output_dir else _default_output_dir(src)
    problem = checkpoint.output_dir_problem(src, out_dir, what="the prepared board")
    if problem:
        return {"status": "error", "reason": problem, "revision_token": None}

    try:
        token = _PENDING.issue(
            src, {"out_dir": str(out_dir), "plan_args": plan_args, "plan": copy.deepcopy(plan), "cache": cache}
        )
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc), "revision_token": None}
    return {
        "status": "proposed",
        "revision_token": token,
        "output_pcb": str(out_dir / src.name),
        "outline": {**plan["rect"], "corner_radius": plan["corner_radius"]},
        "origin": plan["origin"],
        "replaces": plan["replaces"],
        "holes": plan["holes"],
        "outside_outline": plan["outside_outline"],
        "overlaps": plan["overlaps"],
        "zone_overlaps": plan["zone_overlaps"],
        **({"pour_note": _POUR_NOTE} if plan["zone_overlaps"] else {}),
        "note": (
            "creates a new board revision; the original is untouched. Footprints listed under "
            "outside_outline are reported, not moved. Call confirm_board_outline with this "
            "revision_token to apply."
        ),
    }


def _edge_summary(root: Any) -> list[tuple]:
    out = []
    for node in root.children:
        if node.name in _EDGE_KINDS and _layer(node) == _EDGE_LAYER:
            out.append(
                (
                    node.name,
                    _xy(node, "start"),
                    _xy(node, "mid"),
                    _xy(node, "end"),
                )
            )
    return out


def _chain_closed(items: list[tuple]) -> bool:
    ends: Counter = Counter()
    for kind, start, _mid, end in items:
        if kind not in ("gr_line", "gr_arc") or start is None or end is None:
            return False
        for p in (start, end):
            ends[(round(p[0], 4), round(p[1], 4))] += 1
    return bool(ends) and all(count == 2 for count in ends.values())


def _copy_project(src_pcb: Path, dest: Path) -> Path:
    """Copy the project directory with the checkpoint helper (size cap, no VCS/venv, no backups,
    no other boards) and also skip earlier ``*_outlined`` / ``*_prepared`` outputs."""
    name = src_pcb.parent.name
    return checkpoint.copy_project(
        src_pcb, dest, outputs=(f"{name}_outlined", f"{name}_prepared"), max_bytes=gate._MAX_COPY_BYTES
    )


def _write(src: Path, tmp: Path, plan: dict) -> dict:
    """Copy the project into ``tmp``, add the holes and keepouts, replace the Edge.Cuts outline
    and save through the write layer. Returns what ``_verify`` compares against."""
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    tmp_pcb = _copy_project(src, tmp)
    board = PCB.load(tmp_pcb)
    root = board._sexp
    before_counts = Counter(c.name for c in root.children if c.name)
    copper = _copper_layers(root)
    hole_footprints: list[Any] = []  # the nodes this build writes, compared with the reload below

    # Holes first: add_footprint_from_file offsets by the OLD outline's min corner, so
    # the raw (at ...) is rewritten to the declared file coordinates straight away.
    for hole in plan["holes"]:
        count = sum(1 for c in root.children if c.name == "footprint")
        board.add_footprint_from_file(
            hole["footprint_file"], hole["reference"], 0.0, 0.0, rotation=0.0, layer="F.Cu",
            value=hole["footprint"].split(":")[-1],
        )
        footprints = [c for c in root.children if c.name == "footprint"]
        if len(footprints) != count + 1:
            raise RuntimeError(f"adding {hole['reference']} did not add exactly one footprint")
        # The hole exists only on the board; without this kicad-cli --schematic-parity
        # reports "Extra footprint" for it.
        attr = footprints[-1].find("attr")
        if attr is None:
            footprints[-1].append(SExp.list("attr", "board_only"))
        elif "board_only" not in [str(v) for v in attr.values]:
            attr.append(SExp.atom("board_only"))
        at = footprints[-1].find("at")
        at.set_value(0, hole["at"][0])
        at.set_value(1, hole["at"][1])
        hole_footprints.append(footprints[-1])

    for node in [c for c in root.children if c.name in _EDGE_KINDS and _layer(c) == _EDGE_LAYER]:
        root.remove(node)
    outline = _outline_nodes(plan["rect"], plan["corner_radius"])
    keepouts_written = [
        _keepout_node(
            f"KEEPOUT_{hole['reference']}", hole["at"][0], hole["at"][1], hole["keepout_radius_mm"], copper
        )
        for hole in plan["holes"]
    ]
    for node in outline + keepouts_written:
        root.append(node)
    write_layer.save_pcb(board, tmp_pcb)
    return {
        "pcb": tmp_pcb,
        "root": root,
        "before_counts": before_counts,
        "written": hole_footprints + outline + keepouts_written,
    }


def _verify(src: Path, state: dict, plan: dict, drc: Any) -> dict:
    """Verify from a fresh parse of the saved file, never the in-memory tree; then kicad-cli
    DRC for outline errors."""
    tmp_pcb: Path = state["pcb"]
    name = tmp_pcb.name
    try:
        fresh = input_prep._parse(tmp_pcb)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: revision failed to re-parse: {exc}",
            [checkpoint.mismatch(name, "board", "a board that parses", str(exc))],
        ) from exc
    edge = _edge_summary(fresh)
    expected_edge = len(_outline_nodes(plan["rect"], plan["corner_radius"]))
    if len(edge) != expected_edge or not _chain_closed(edge):
        raise checkpoint.NotVerified(
            "CRITICAL: Edge.Cuts after save+reload is not the expected closed outline",
            [
                checkpoint.mismatch(
                    name, "Edge.Cuts outline", f"a closed chain of {expected_edge} items",
                    f"{len(edge)} items, closed: {_chain_closed(edge)}",
                )
            ],
        )
    xs = [p[0] for _k, s, _m, e in edge for p in (s, e)]
    ys = [p[1] for _k, s, _m, e in edge for p in (s, e)]
    r = plan["rect"]
    w = _self_check.as_written  # the declared value as the write layer writes it (full precision)
    extent = {"min_x": min(xs), "max_x": max(xs), "min_y": min(ys), "max_y": max(ys)}
    declared = {
        "min_x": w(r["x"]), "max_x": w(r["x"] + r["width"]), "min_y": w(r["y"]), "max_y": w(r["y"] + r["height"]),
    }
    off = [key for key in declared if abs(extent[key] - declared[key]) > 1e-4]
    if off:
        raise checkpoint.NotVerified(
            "CRITICAL: outline extent after reload differs from the declared rectangle",
            [checkpoint.mismatch(name, f"outline {key}", declared[key], extent[key]) for key in off],
        )
    by_ref = {_fp_reference(fp): fp for fp in fresh.children if fp.name == "footprint"}
    for hole in plan["holes"]:
        fp = by_ref.get(hole["reference"])
        at = _xy(fp, "at") if fp is not None else None
        if at is None or abs(at[0] - w(hole["at"][0])) > 1e-4 or abs(at[1] - w(hole["at"][1])) > 1e-4:
            raise checkpoint.NotVerified(
                f"CRITICAL: raw (at ...) of {hole['reference']} after reload is {at}, not {hole['at']}",
                [checkpoint.mismatch(name, f"footprint:{hole['reference']} (at ...)", hole["at"], at)],
            )
    keepouts = [c for c in fresh.children if c.name == "zone" and c.find("keepout") is not None]
    if len(keepouts) < len(plan["holes"]):
        raise checkpoint.NotVerified(
            "CRITICAL: keepout zones missing after reload",
            [checkpoint.mismatch(name, "keepout zones", f"at least {len(plan['holes'])}", len(keepouts))],
        )
    after_counts = Counter(c.name for c in fresh.children if c.name)
    expect = Counter(state["before_counts"])
    for kind, n in plan["replaces"].items():
        expect[kind] -= n
    for node in _outline_nodes(plan["rect"], plan["corner_radius"]):
        expect[node.name] += 1
    expect["footprint"] += len(plan["holes"])
    expect["zone"] += len(plan["holes"])
    if +after_counts != +expect:
        raise checkpoint.NotVerified(
            "CRITICAL: top-level node counts changed unexpectedly",
            [
                checkpoint.mismatch(name, f"top-level {tag} count", (+expect).get(tag, 0), after_counts.get(tag, 0))
                for tag in sorted(set(+expect) | set(after_counts))
                if (+expect).get(tag, 0) != after_counts.get(tag, 0)
            ],
            expected={k: v for k, v in expect.items() if v}, after=dict(after_counts),
        )
    # The summaries above name the common faults; this compares the whole file, exactly: the
    # reload must be what was saved, and the source minus its Edge.Cuts plus what this build wrote.
    try:
        source = input_prep._parse(src)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.Failed(
            "error", f"CRITICAL: the source could not be re-parsed for the write check: {exc}"
        ) from exc
    write_check = _self_check.reload_differences(
        source=source,
        removed=[c for c in source.children if c.name in _EDGE_KINDS and _layer(c) == _EDGE_LAYER],
        written=state["written"],
        memory=state["root"],
        reloaded=fresh,
    )
    if write_check["missing"] or write_check["unexpected"]:
        raise checkpoint.NotVerified(
            "CRITICAL: the reloaded revision is not the intended write: " + _self_check.describe(write_check),
            checkpoint.reload_mismatches(name, write_check),
            write_check=_self_check.bounded(write_check),
        )

    outline_drc = _outline_drc(tmp_pcb, drc)
    if outline_drc["status"] == "errors":
        raise checkpoint.Failed("error", "kicad-cli reports an outline error on the revision", outline_drc=outline_drc)
    if outline_drc["status"] != "clean":
        raise checkpoint.Failed(
            "incomplete",
            "the outline could not be checked, so no revision was produced: kicad-cli DRC did not run "
            f"({outline_drc.get('reason')})",
            outline_drc=outline_drc,
        )
    return {"outline_drc": outline_drc, "write_check": _self_check.bounded(write_check)}


def _outline_drc(pcb: Path, drc: Any) -> dict:
    try:
        if drc is None:
            from kicad_mcp.kicad_cli import run_drc

            report = run_drc(pcb)
        else:
            report = drc(pcb)
    except Exception as exc:  # noqa: BLE001
        return {"status": "not_run", "reason": str(exc)}
    found = [v for v in report.get("violations", []) if v.get("type") in _OUTLINE_VIOLATIONS]
    return {"status": "errors" if found else "clean", "violations": found}


def confirm_board_outline(pcb_path: str | Path, revision_token: str, *, drc: Any = None) -> dict:
    """Apply a proposal: build the revision, verify it from a fresh reload, run kicad-cli DRC for
    outline errors, move it into place. ``drc`` replaces the kicad-cli call (tests only). The token
    is consumed only by success or by detected source drift."""
    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    out_dir = Path(entry["out_dir"])
    problem = checkpoint.output_dir_problem(src, out_dir, what="the prepared board")
    if problem:
        return {"status": "error", "reason": problem}
    plan, refusal = _plan(src, *copy.deepcopy(entry["plan_args"]), cache=entry["cache"])
    if refusal is not None:
        return {**refusal, "status": "error", "note": "the revision_token is kept"}
    if plan != entry["plan"]:
        return {
            "status": "error",
            "reason": "re-planning the same arguments now gives a different result than the proposal "
            "(library or board inputs changed): re-run propose_board_outline",
            "note": "the revision_token is kept",
        }

    built = checkpoint.build_revision(
        out_dir,
        write=lambda tmp: _write(src, tmp, plan),
        verify=lambda tmp, state: _verify(src, state, plan, drc),
        success="built",
        before_verified="outline build failed before a revision was verified",
    )
    if built["status"] != "built":
        return built
    _PENDING.spend(revision_token)
    return {
        "status": "built",
        "output_pcb": str(out_dir / src.name),
        "outline": {**plan["rect"], "corner_radius": plan["corner_radius"]},
        "origin": plan["origin"],
        "replaced": plan["replaces"],
        "holes": plan["holes"],
        "outside_outline": plan["outside_outline"],
        "overlaps": plan["overlaps"],
        "zone_overlaps": plan["zone_overlaps"],
        **({"pour_note": _POUR_NOTE} if plan["zone_overlaps"] else {}),
        "outline_drc": built["outline_drc"],
        "write_check": built["write_check"],
        "note": (
            "outline errors only; run verify_board for the verdict. Footprints in outside_outline "
            "were not moved."
        ),
    }
