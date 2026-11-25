"""A footprint's courtyard must enclose every pad (GitHub issue 228).

Footprints adopted through easyeda2kicad can carry a courtyard of body size while the pads reach
outside it (measured on a peer board: a 1206 capacitor with a courtyard of 3.2 x 1.6 mm against
pads that reach 4.67 x 1.73 mm; packing by courtyard then gave shorting items and solder-mask
bridges in DRC with no courtyard overlap). Everything here reads the raw ``(at)``, ``(size)`` and
courtyard items of the footprint's own S-expression (CLAUDE.md hard rule 2) and changes it through
the write layer's low-level API only.

Shortcut: the courtyard is measured as the bounding box of its items, so an L-shaped courtyard is
treated as its box. A custom pad is measured by its anchor ``(size)`` and the points of its
primitives (an arc by its extreme points, plus half the primitive's line width); a primitive of
another kind, or a pad without ``(at)`` and ``(size)``, cannot be measured and ``check`` raises
``RawGeometryError`` (callers report it as unmeasurable, never as a pass).
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from boardproof import placement_anchor, raw_geometry, write_layer
from boardproof.raw_geometry import RawGeometryError

MARGIN_MM = 0.25  # KLC: 0.25 mm between the pads and the courtyard
GRID_MM = 0.01
LINE_WIDTH_MM = 0.05
_TOL = 1e-6
_COURTYARD_LAYERS = ("F.CrtYd", "B.CrtYd")

Box = list[float]  # [min x, min y, max x, max y], footprint-local mm


def _round(box: tuple[float, float, float, float]) -> Box:
    return [round(v, 6) + 0.0 for v in box]


def _primitive_box(node: Any) -> tuple[float, float, float, float]:
    """The pad-local box of one custom-pad primitive, with half its line width."""

    def xy(name: str) -> tuple[float, float]:
        child = node.find(name)
        if child is None or len(child.values) < 2:
            raise RawGeometryError(f"a ({node.name} ...) primitive has no ({name} x y)")
        return float(child.values[0]), float(child.values[1])

    kind = node.name
    if kind in ("gr_line", "gr_rect"):
        points = [xy("start"), xy("end")]
    elif kind == "gr_circle":
        (cx, cy), (ex, ey) = xy("center"), xy("end")
        r = math.hypot(ex - cx, ey - cy)
        points = [(cx - r, cy - r), (cx + r, cy + r)]
    elif kind == "gr_arc":
        points = raw_geometry.arc_extent(xy("start"), xy("mid"), xy("end")) or [xy("start"), xy("mid"), xy("end")]
    elif kind in ("gr_poly", "gr_curve", "gr_bezier"):
        pts = node.find("pts")
        points = [
            (float(c.values[0]), float(c.values[1]))
            for c in (pts.children if pts is not None else [])
            if not c.is_atom and c.name == "xy" and len(c.values) >= 2
        ]
        if not points:
            raise RawGeometryError(f"a ({kind} ...) primitive has no points")
    else:
        raise RawGeometryError(f"a custom pad primitive ({kind} ...) is not measured")
    width = node.find("width")
    half = abs(float(width.values[0])) / 2 if width is not None and width.values else 0.0
    return (
        min(p[0] for p in points) - half, min(p[1] for p in points) - half,
        max(p[0] for p in points) + half, max(p[1] for p in points) + half,
    )  # fmt: skip


def _pads(root: Any) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(pad number, box) of every pad, from the raw ``(at x y [angle])`` and ``(size w h)``; a pad
    turned by ``angle`` covers ``|w cos| + |h sin|`` by ``|w sin| + |h cos|``. A custom pad also
    covers its primitives. Raises ``RawGeometryError`` for a pad that cannot be measured."""
    out = []
    for pad in root.find_all("pad"):
        at, size = pad.find("at"), pad.find("size")
        if at is None or size is None or len(at.values) < 2 or len(size.values) < 2:
            raise RawGeometryError(f"a pad of {root.get_string(0)!r} has no readable (at) and (size)")
        x, y = float(at.values[0]), float(at.values[1])
        angle_deg = float(at.values[2]) if len(at.values) > 2 else 0.0
        angle = math.radians(angle_deg)
        w, h = abs(float(size.values[0])), abs(float(size.values[1]))
        hw = (w * abs(math.cos(angle)) + h * abs(math.sin(angle))) / 2
        hh = (w * abs(math.sin(angle)) + h * abs(math.cos(angle))) / 2
        box = (x - hw, y - hh, x + hw, y + hh)
        primitives = pad.find("primitives")
        if primitives is not None and "custom" in [getattr(c, "value", None) for c in pad.children[:4] if c.is_atom]:
            for prim in primitives.children:
                if prim.is_atom:
                    continue
                x0, y0, x1, y1 = _primitive_box(prim)
                corners = [raw_geometry.place((x, y), angle_deg, c) for c in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))]
                box = _hull([box, (min(c[0] for c in corners), min(c[1] for c in corners),
                                   max(c[0] for c in corners), max(c[1] for c in corners))])  # fmt: skip
        number = str(pad.get_string(0) or "")
        out.append((number or f"(unnumbered pad at {x:g}, {y:g})", box))
    return out


def _courtyard(root: Any) -> tuple[float, float, float, float] | None:
    points = placement_anchor._courtyard_points(root, (0.0, 0.0), 0.0)
    if not points:
        return None
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _hull(boxes: list[tuple[float, float, float, float]]) -> tuple[float, float, float, float]:
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def _snapped(box: tuple[float, float, float, float], margin: float) -> tuple[float, float, float, float]:
    """``box`` grown by ``margin`` and rounded outward to the 0.01 mm grid."""
    def down(v: float) -> float:
        return math.floor(round(v / GRID_MM, 6)) * GRID_MM

    def up(v: float) -> float:
        return math.ceil(round(v / GRID_MM, 6)) * GRID_MM

    return (round(down(box[0] - margin), 6), round(down(box[1] - margin), 6),
            round(up(box[2] + margin), 6), round(up(box[3] + margin), 6))  # fmt: skip


def check(root: Any, margin: float = MARGIN_MM) -> dict:
    """Does the courtyard enclose every pad? ``encloses`` is False without a courtyard when the
    footprint has pads. ``uncovered_pads`` lists the pad numbers; ``will_be_set_to`` is what ``fix``
    would write. Raises ``RawGeometryError`` on a token that is not a number."""
    pads = _pads(root)
    court = _courtyard(root)
    uncovered = [
        n for n, b in pads
        if court is None or b[0] < court[0] - _TOL or b[1] < court[1] - _TOL or b[2] > court[2] + _TOL or b[3] > court[3] + _TOL
    ]  # fmt: skip
    pads_box = _hull([b for _, b in pads]) if pads else None
    return {
        "encloses": not uncovered and (court is not None or not pads),
        "uncovered_pads": uncovered,
        "courtyard_box": None if court is None else _round(court),
        "pads_box": None if pads_box is None else _round(pads_box),
        "will_be_set_to": None if pads_box is None else _round(_hull([_snapped(pads_box, margin)] + ([court] if court else []))),
    }


def _crtyd_items(root: Any) -> list[Any]:
    out = []
    for node in root.children:
        if node.is_atom or not (node.name or "").startswith("fp_"):
            continue
        layer = node.find("layer")
        if layer is not None and layer.values and layer.values[0] in _COURTYARD_LAYERS:
            out.append(node)
    return out


def fix(root: Any, margin: float = MARGIN_MM) -> Box | None:
    """Replace the courtyard by one rectangle around the pads plus ``margin`` (on the 0.01 mm grid)
    and the old courtyard's box, so a body larger than the pads stays enclosed. Returns the box, or
    ``None`` and writes nothing for a footprint without pads or whose courtyard already encloses
    every pad (a good courtyard, a circle or a polygon too, is never rewritten)."""
    fit = check(root, margin)
    box = fit["will_be_set_to"]
    if box is None or fit["encloses"]:
        return None
    old = _crtyd_items(root)
    layer = (old[0].find("layer").values[0] if old else "F.CrtYd")
    for node in old:
        root.children.remove(node)
    x0, y0, x1, y1 = box
    n, s = write_layer.number, write_layer.string
    if root.name == "module":  # the old format has no fp_rect
        for a, b in (((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x1, y1), (x0, y1)), ((x0, y1), (x0, y0))):
            root.append(write_layer.SExp.list(
                "fp_line", write_layer.SExp.list("start", n(a[0]), n(a[1])), write_layer.SExp.list("end", n(b[0]), n(b[1])),
                write_layer.SExp.list("layer", write_layer.symbol(layer)), write_layer.SExp.list("width", n(LINE_WIDTH_MM)),
            ))  # fmt: skip
        return box
    root.append(write_layer.SExp.list(
        "fp_rect", write_layer.SExp.list("start", n(x0), n(y0)), write_layer.SExp.list("end", n(x1), n(y1)),
        write_layer.SExp.list("stroke", write_layer.SExp.list("width", n(LINE_WIDTH_MM)),
                              write_layer.SExp.list("type", write_layer.symbol("solid"))),
        write_layer.SExp.list("fill", write_layer.symbol("none")), write_layer.SExp.list("layer", s(layer)),
    ))  # fmt: skip
    return box


def describe(fit: dict) -> str:
    """One phrase for a failed ``check``: no courtyard at all, or the box that misses pads."""
    if fit["courtyard_box"] is None:
        return "the footprint has no courtyard"
    return f"the courtyard {fit['courtyard_box']} does not enclose pad(s) {', '.join(fit['uncovered_pads'])}"


def _library_problem(library_name: str, library_dir: str) -> str | None:
    text = str(library_dir).replace("\\", "/")
    if not text or text.startswith("/") or ":" in text or any(p in ("", ".", "..") for p in text.split("/")):
        return f"library_dir {library_dir!r} must be a folder below the project, without '..' or a drive"
    if not library_name or library_name in (".", "..") or any(c in str(library_name) for c in "/\\:"):
        return f"library_name {library_name!r} must be a plain library name"
    return None


def list_adopted(
    pcb_path: str | Path, library_name: str = "project_parts", library_dir: str = "lib", margin: float = MARGIN_MM
) -> dict:
    """Read-only: every footprint of the project's adopted library (``<library_dir>/<library_name>.pretty``
    beside the board) whose courtyard does not enclose every pad. Nothing is written."""
    if (problem := _library_problem(library_name, library_dir)) is not None:
        return {"status": "error", "reason": problem}
    base = Path(pcb_path).parent / Path(*library_dir.replace("\\", "/").split("/"))
    pretty = base / f"{library_name}.pretty"
    if not pretty.is_dir():
        return {"status": "incomplete", "reason": f"{pretty.name} is not a folder beside the board: nothing was checked"}
    violations, unreadable = [], []
    files = sorted(pretty.glob("*.kicad_mod"))
    for file in files:
        try:
            result = check(write_layer.load(file), margin)
        except Exception as exc:  # noqa: BLE001 - an unreadable footprint is reported, never counted clean
            unreadable.append({"footprint": file.stem, "reason": str(exc)})
            continue
        if not result["encloses"]:
            violations.append({"footprint": file.stem, "severity": "error", **result})
    return {
        "status": "ok" if not unreadable else "incomplete",
        "library": str(pretty),
        "checked": len(files),
        "violations": violations,
        "unreadable": unreadable,
        "note": "read-only: nothing was changed. No tool rewrites an already adopted footprint yet (adoption never overwrites); new adoptions set the courtyard to the pads plus margin.",
    }
