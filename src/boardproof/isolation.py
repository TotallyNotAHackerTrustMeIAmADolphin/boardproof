"""Keepout zones and slots (Edge.Cuts cut-outs) for isolation, as a checkpointed pair.

Issue 132. ``propose_keepouts_and_slots`` (dry run: writes nothing, returns a revision token) and
``confirm_keepouts_and_slots`` (builds a NEW board revision in a temporary sibling directory,
reloads it fresh from disk, compares the whole file, runs ``kicad-cli pcb drc`` and refuses the
revision if the change made the outline or the keepouts report new errors, then moves it into
place). The source board is never touched. The checkpoint mechanics are the shared helper's
(``boardproof.checkpoint``), the pair follows ``board_outline``.

What is measured, with kicad-cli 10.0.7 (docs/ARCHITECTURE.md, "Keepout zones and slots"):

* a closed loop inside the outer outline is a cut-out: DRC accepts it, the gerber export draws it,
  KiCad's Specctra export hands the router a keepout on every signal layer for it, and a copper
  pour is pulled back from it. A loop that touches or crosses the outline is ``invalid_outline``;
* DRC does NOT flag a track, via, pad or footprint that lies entirely inside a slot (only copper
  touching a slot edge gets ``copper_edge_clearance``). This module therefore looks for such
  items itself and refuses a slot that would cut any of them;
* a rule area (zone with ``(keepout ...)``) reports ``items_not_allowed`` for each track, via, pad
  or footprint its rules forbid, only on the layers it lists; a copper pour is carved back on a
  refill when ``copperpour`` is ``not_allowed``.

Slots: ``{x1, y1, x2, y2, width_mm, ends}``, a centre line from (x1, y1) to (x2, y2) and a width.
``ends: "round"`` is a stadium whose semicircular caps are centred on both end points (the shape a
milling bit cuts: overall length is the centre-line length plus ``width_mm``); ``"square"`` is the
rectangle from the first end point to the second. Built from ``gr_line`` and ``gr_arc`` items on
Edge.Cuts, with fixed uuids so that a re-plan equals the proposal.

Keepouts: ``{name, rect | polygon, layers, rules}``; rules default to tracks, vias and copperpour
``not_allowed``, pads and footprints ``allowed``. Coordinates are page millimetres.

Nothing here trusts a ``kicad_tools`` position or geometry property: every position read is the raw
``(at ...)``, ``(start ...)``, ``(size ...)`` or ``(pts ...)`` token (CLAUDE.md hard rule 2).
"""

from __future__ import annotations

import copy
import math
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any

from boardproof import _self_check, board_outline, checkpoint, edge_loops, input_prep, raw_geometry, write_layer

for _name in ("_fp_transform", "_courtyard_box", "_copper_layers", "_zone_ring", "_fp_reference", "_copy_project"):
    if not hasattr(board_outline, _name):  # private helpers reused on purpose
        raise ImportError(f"isolation reuses boardproof.board_outline.{_name}, which no longer exists")

RULES = ("tracks", "vias", "pads", "copperpour", "footprints")
DEFAULT_RULES = {"tracks": "not_allowed", "vias": "not_allowed", "pads": "allowed", "copperpour": "not_allowed", "footprints": "allowed"}
ENDS = ("round", "square")
MIN_SLOT_WIDTH_MM = 0.5  # a guard against degenerate shapes, NOT a fab minimum (check the fab's own slot width)
DEFAULT_MARGIN_MM = 0.5
MAX_PER_LIST = 200
MAX_POLYGON_POINTS = 200
MAX_NAME = 64
_LISTED = 50
_STROKE_WIDTH = 0.1
SAMPLING_TOL_MM = edge_loops.SAMPLING_TOL_MM  # curves are polylines whose chords lie up to this far inside them
_COORD_LIMIT = 1e5
_WATCHED = ("invalid_outline", "malformed_outline", "copper_edge_clearance", "items_not_allowed")
_NAME_FORBIDDEN = '"' + chr(92)  # a double quote and a backslash
_NS = uuid.UUID("0b7f6d1e-5c0e-4b51-9a60-132132132132")

_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())
_DRIFT = "the board changed since the proposal was made: re-run propose_keepouts_and_slots"

_POUR_NOTE = (
    "copper pours listed under zone_overlaps are cut back by the new slots or keepouts, but their stored "
    "fill is stale: the pour needs refilling (kicad-cli refill / the pour step) before any pour or DRC "
    "figure for this revision means anything"
)


def _refused(reason: str, **extra: Any) -> dict:
    return {"status": "refused", "reason": reason, "revision_token": None, **extra}


def _num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and abs(value) <= _COORD_LIMIT


def _r(value: float) -> float:
    return round(float(value), 6)


def _bounded(items: list[dict]) -> dict:
    return {"count": len(items), "items": items[:_LISTED]}


# --------------------------------------------------------------------------- reading the board


def _names(node: Any, tag: str) -> list[str]:
    child = node.find(tag)
    return [str(v) for v in child.values] if child is not None else []


def _zone_layers(zone: Any) -> list[str]:
    return _names(zone, "layers") + _names(zone, "layer")


def _existing_keepouts(root: Any) -> list[dict]:
    out = []
    for node in root.children:
        if node.is_atom or node.name != "zone" or node.find("keepout") is None:
            continue
        name = node.find("name")
        keep = node.find("keepout")
        rules = {r: (str(keep.find(r).values[0]) if keep.find(r) is not None and keep.find(r).values else "allowed") for r in RULES}
        out.append(
            {
                "name": str(name.values[0]) if name is not None and name.values else "",
                "uuid": board_outline._uuid(node),
                "layers": _zone_layers(node),
                "rules": rules,
                "polygon": [list(p) for p in board_outline._zone_ring(node)],
            }
        )
    return out


class _Copper:
    """The existing items a slot or keepout can hit, read once per board from raw tokens."""

    def __init__(self, root: Any) -> None:
        self.layers = board_outline._copper_layers(root)
        self._net_names = raw_geometry.net_table(root)
        self.tracks: list[dict] = []  # points (polyline), half, layer, uuid
        self.vias: list[dict] = []  # center, radius, layers, uuid
        self.pads: list[dict] = []  # ring, layers, reference, number
        self.courtyards: list[dict] = []  # ring, reference, layer (the footprint's side)
        self.pours: list[dict] = []  # ring, layers, name, net, uuid
        self._read(root)

    def _expand(self, names: list[str]) -> set[str]:
        out: set[str] = set()
        for n in names:
            if n == "*.Cu":
                out |= set(self.layers)
            elif n.endswith(".Cu"):
                out.add(n)
        return out

    def _read(self, root: Any) -> None:
        for node in root.children:
            if node.is_atom:
                continue
            if node.name in ("segment", "arc"):
                a, b, mid = board_outline._xy(node, "start"), board_outline._xy(node, "end"), board_outline._xy(node, "mid")
                if not (a and b):
                    continue
                pts = (edge_loops.arc_points(a, mid, b) if mid else None) or [a, b]
                width = node.find("width")
                half = float(width.values[0]) / 2 if width is not None and width.values else 0.0
                self.tracks.append({"points": pts, "half": half, "layer": board_outline._layer(node), "uuid": board_outline._uuid(node)})
            elif node.name == "via":
                at, size = board_outline._xy(node, "at"), node.find("size")
                if not (at and size is not None and size.values):
                    continue
                layers = self._expand(_names(node, "layers"))
                vtype = _names(node, "type")
                if vtype and vtype[0] in ("blind", "micro", "blind_buried") and len(layers) == 2:
                    lo, hi = sorted(self.layers.index(x) for x in layers if x in self.layers)
                    layers = set(self.layers[lo : hi + 1])
                elif not vtype or vtype[0] not in ("blind", "micro", "blind_buried"):
                    layers = set(self.layers)
                self.vias.append({"center": at, "radius": float(size.values[0]) / 2, "layers": layers, "uuid": board_outline._uuid(node)})
            elif node.name == "zone" and node.find("keepout") is None:
                ring = board_outline._zone_ring(node)
                if ring:
                    name = node.find("name")
                    self.pours.append(
                        {
                            "ring": ring,
                            "layers": set(_zone_layers(node)),
                            "name": str(name.values[0]) if name is not None and name.values else "",
                            "net": raw_geometry.net_name(node, self._net_names),
                            "uuid": board_outline._uuid(node),
                        }
                    )
            elif node.name == "footprint" and node.find("at") is not None:
                self._footprint(node)

    def _footprint(self, fp: Any) -> None:
        ref = board_outline._fp_reference(fp)
        to_abs = board_outline._fp_transform(fp)
        side = board_outline._layer(fp) or "F.Cu"
        for pad in fp.children:
            if pad.is_atom or pad.name != "pad":
                continue
            local, size = board_outline._xy(pad, "at"), pad.find("size")
            if local is None or size is None or not size.values:
                continue
            w, h = (float(v) for v in size.values[:2])
            half = math.hypot(w, h) / 2  # the circle round the pad: a square that holds it turned by any angle
            cx, cy = to_abs(*local)
            number = str(pad.values[0]) if pad.values else ""
            self.pads.append(
                {
                    "ring": edge_loops.box_ring((cx - half, cy - half, cx + half, cy + half)),
                    "layers": self._expand(_names(pad, "layers")),
                    "reference": ref,
                    "number": number,
                }
            )
        box = board_outline._courtyard_box(fp)
        if box is not None:
            self.courtyards.append({"ring": edge_loops.box_ring(box), "reference": ref, "layer": side})


# --------------------------------------------------------------------------- shapes of the new items


def _slot_geometry(spec: dict) -> tuple[list[tuple[str, tuple[tuple[float, float], ...]]], list[tuple[float, float]]]:
    """``(pieces, ring)``: the Edge.Cuts pieces (``("line", (a, b))`` or ``("arc", (a, mid, b))``) in
    chain order, and the contour as a polyline for the geometry checks. Coordinates are rounded to
    the 6 decimals that are written."""
    p1, p2 = (spec["x1"], spec["y1"]), (spec["x2"], spec["y2"])
    w = spec["width_mm"]
    r = w / 2
    length = math.dist(p1, p2)
    d = ((p2[0] - p1[0]) / length, (p2[1] - p1[1]) / length)
    n = (-d[1], d[0])

    def at(p: tuple[float, float], u: tuple[float, float], k: float) -> tuple[float, float]:
        return (_r(p[0] + u[0] * k), _r(p[1] + u[1] * k))

    a1, a2 = at(p1, n, r), at(p2, n, r)
    b2, b1 = at(p2, n, -r), at(p1, n, -r)
    if spec["ends"] == "square":
        pieces = [("line", (a1, a2)), ("line", (a2, b2)), ("line", (b2, b1)), ("line", (b1, a1))]
    else:
        pieces = [
            ("line", (a1, a2)),
            ("arc", (a2, at(p2, d, r), b2)),
            ("line", (b2, b1)),
            ("arc", (b1, at(p1, d, -r), a1)),
        ]
    ring: list[tuple[float, float]] = []
    for kind, pts in pieces:
        seq = list(pts) if kind == "line" else (edge_loops.arc_points(*pts) or list(pts))
        ring.extend(seq[:-1])
    return pieces, ring


def _stroke() -> Any:
    from kicad_tools.sexp import SExp

    return SExp.list("stroke", SExp.list("width", _STROKE_WIDTH), SExp.list("type", "default"))


def _slot_nodes(pieces: list, uuids: list[str]) -> list[Any]:
    from kicad_tools.sexp import SExp

    nodes = []
    for (kind, pts), ident in zip(pieces, uuids, strict=True):
        if kind == "line":
            nodes.append(
                SExp.list("gr_line", SExp.list("start", *pts[0]), SExp.list("end", *pts[1]), _stroke(),
                          SExp.list("layer", write_layer.string("Edge.Cuts")), SExp.list("uuid", write_layer.string(ident)))
            )
        else:
            nodes.append(
                SExp.list("gr_arc", SExp.list("start", *pts[0]), SExp.list("mid", *pts[1]), SExp.list("end", *pts[2]), _stroke(),
                          SExp.list("layer", write_layer.string("Edge.Cuts")), SExp.list("uuid", write_layer.string(ident)))
            )
    return nodes


def _keepout_node(spec: dict) -> Any:
    from kicad_tools.sexp import SExp

    pts = SExp.list("pts")
    for x, y in spec["polygon"]:
        pts.append(SExp.list("xy", x, y))
    rules = spec["rules"]
    return SExp.list(
        "zone",
        SExp.list("net", 0),
        SExp.list("net_name", write_layer.string("")),
        SExp.list("layers", *[write_layer.string(layer) for layer in spec["layers"]]),
        SExp.list("uuid", write_layer.string(spec["uuid"])),
        SExp.list("name", write_layer.string(spec["name"])),
        SExp.list("hatch", "edge", 0.5),
        SExp.list("connect_pads", SExp.list("clearance", 0)),
        SExp.list("min_thickness", 0.25),
        SExp.list("keepout", *[SExp.list(rule, rules[rule]) for rule in RULES]),
        SExp.list("fill", SExp.list("thermal_gap", 0.5), SExp.list("thermal_bridge_width", 0.5)),
        SExp.list("polygon", pts),
    )


# --------------------------------------------------------------------------- validation


def _check_name(name: Any) -> str | None:
    if not isinstance(name, str) or not name.strip():
        return "name must be a non-empty string"
    if len(name) > MAX_NAME:
        return f"name is longer than {MAX_NAME} characters"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F or ch in _NAME_FORBIDDEN for ch in name):
        return "name holds a control character (a line break included), a quote or a backslash"
    try:
        write_layer.check_text(name)
    except Exception as exc:  # noqa: BLE001
        return f"name is not a writable text: {exc}"
    return None


def _norm_keepout(spec: Any, board_layers: list[str]) -> tuple[dict | None, str | None]:
    if not isinstance(spec, dict):
        return None, "each keepout must be an object"
    problem = _check_name(spec.get("name"))
    if problem:
        return None, problem
    name = spec["name"]
    unknown = set(spec) - {"name", "rect", "polygon", "layers", "rules"}
    if unknown:
        return None, f"keepout {name}: unknown key(s) {sorted(unknown)}"
    if ("rect" in spec) == ("polygon" in spec):
        return None, f"keepout {name}: give exactly one of rect and polygon"
    if "rect" in spec:
        rect = spec["rect"]
        if not isinstance(rect, dict) or not all(_num(rect.get(k)) for k in ("x", "y", "width", "height")):
            return None, f"keepout {name}: rect needs numeric x, y, width, height"
        if rect["width"] <= 0 or rect["height"] <= 0:
            return None, f"keepout {name}: rect width and height must be positive"
        x, y, w, h = (float(rect[k]) for k in ("x", "y", "width", "height"))
        ring = [(_r(x), _r(y)), (_r(x + w), _r(y)), (_r(x + w), _r(y + h)), (_r(x), _r(y + h))]
    else:
        poly = spec["polygon"]
        if not isinstance(poly, list) or not 3 <= len(poly) <= MAX_POLYGON_POINTS:
            return None, f"keepout {name}: polygon needs 3 to {MAX_POLYGON_POINTS} points"
        if not all(isinstance(p, (list, tuple)) and len(p) == 2 and _num(p[0]) and _num(p[1]) for p in poly):
            return None, f"keepout {name}: every polygon point must be [x, y] with finite numbers"
        ring = [(_r(p[0]), _r(p[1])) for p in poly]
    if not edge_loops.ring_is_simple(ring) or abs(edge_loops.ring_area(ring)) < 1e-6:
        return None, f"keepout {name}: the polygon crosses itself, repeats a point or has no area"
    layers = spec.get("layers", "all")
    if layers == "all":
        chosen = list(board_layers)
    elif isinstance(layers, list) and layers and all(isinstance(x, str) for x in layers):
        bad = [x for x in layers if x not in board_layers]
        if bad:
            return None, f"keepout {name}: layer(s) {bad} are not copper layers of this board {board_layers}"
        chosen = [x for x in board_layers if x in layers]  # board order, no repeats
    else:
        return None, f"keepout {name}: layers must be \"all\" or a non-empty list of copper layer names"
    rules = dict(DEFAULT_RULES)
    given = spec.get("rules", {})
    if not isinstance(given, dict) or set(given) - set(RULES) or any(v not in ("allowed", "not_allowed") for v in given.values()):
        return None, f"keepout {name}: rules must map {list(RULES)} to \"allowed\" or \"not_allowed\""
    rules.update(given)
    if "not_allowed" not in rules.values():
        return None, f"keepout {name}: every rule is allowed, so the keepout forbids nothing"
    ident = str(uuid.uuid5(_NS, f"keepout:{name}:{ring}:{chosen}:{sorted(rules.items())}"))
    return {"name": name, "polygon": [list(p) for p in ring], "layers": chosen, "rules": rules, "uuid": ident}, None


def _norm_slot(spec: Any, index: int) -> tuple[dict | None, str | None]:
    where = f"slot {index}"
    if not isinstance(spec, dict):
        return None, f"{where}: each slot must be an object"
    unknown = set(spec) - {"x1", "y1", "x2", "y2", "width_mm", "ends"}
    if unknown:
        return None, f"{where}: unknown key(s) {sorted(unknown)}"
    for key in ("x1", "y1", "x2", "y2", "width_mm"):
        if not _num(spec.get(key)):
            return None, f"{where}: {key} is required and must be a finite number"
    if spec["width_mm"] < MIN_SLOT_WIDTH_MM:
        return None, f"{where}: width_mm must be at least {MIN_SLOT_WIDTH_MM} mm"
    ends = spec.get("ends", "round")
    if ends not in ENDS:
        return None, f"{where}: ends must be one of {list(ENDS)}"
    p1, p2 = (float(spec["x1"]), float(spec["y1"])), (float(spec["x2"]), float(spec["y2"]))
    if math.dist(p1, p2) < 0.1:
        return None, f"{where}: the two end points are closer than 0.1 mm"
    norm = {"x1": _r(p1[0]), "y1": _r(p1[1]), "x2": _r(p2[0]), "y2": _r(p2[1]), "width_mm": _r(spec["width_mm"]), "ends": ends}
    pieces, ring = _slot_geometry(norm)
    norm["uuids"] = [str(uuid.uuid5(_NS, f"slot:{norm}:{k}")) for k in range(len(pieces))]
    return norm, None


# --------------------------------------------------------------------------- conflicts


def _hit(gap: float, margin: float) -> bool:
    """A gap (distance minus the item's own extent) that closes the margin. A gap of 0 or less (the
    shapes overlap or one holds the other) is a hit at ANY margin, margin 0 included; the sampling
    tolerance of the curves is part of every margin."""
    return gap <= 0.0 or gap < margin + SAMPLING_TOL_MM


def _too_close(a: list[tuple[float, float]], b: list[tuple[float, float]], margin: float) -> bool:
    """Two slot contours that nest, touch, cross or come closer than ``margin``."""
    return edge_loops.ring_distance(a, b) == 0.0 or edge_loops.boundary_distance(a, b) < max(margin, edge_loops.SNAP * 2) + SAMPLING_TOL_MM


def _slot_conflicts(label: str, ring: list[tuple[float, float]], copper: _Copper, margin: float) -> list[dict]:
    """Existing items within ``margin`` of the slot's contour or inside it. A slot passes through
    every layer, so every layer counts."""
    found: list[dict] = []

    def add(kind: str, item: str, **extra: Any) -> None:
        found.append({"slot": label, "kind": kind, "item": item, **extra})

    for t in copper.tracks:
        if _hit(edge_loops.shape_distance(ring, True, t["points"], False) - t["half"], margin):
            add("track", t["uuid"], layer=t["layer"])
    for v in copper.vias:
        if _hit(edge_loops.shape_distance(ring, True, [v["center"]], False) - v["radius"], margin):
            add("via", v["uuid"])
    for p in copper.pads:
        if _hit(edge_loops.ring_distance(ring, p["ring"]), margin):
            add("pad", f"{p['reference']}.{p['number']}")
    for c in copper.courtyards:
        if _hit(edge_loops.ring_distance(ring, c["ring"]), margin):
            add("courtyard", c["reference"])
    return found


def _pour_overlaps(label: str, ring: list[tuple[float, float]], copper: _Copper, layers: set[str] | None = None) -> list[dict]:
    return [
        {"for": label, "kind": "pour", "item": z["uuid"], "name": z["name"], "net": z["net"], "layers": sorted(z["layers"])}
        for z in copper.pours
        if (layers is None or z["layers"] & layers) and edge_loops.ring_distance(ring, z["ring"]) == 0.0
    ]


def _keepout_conflicts(spec: dict, copper: _Copper) -> list[dict]:
    ring = [tuple(p) for p in spec["polygon"]]
    layers = set(spec["layers"])
    rules = spec["rules"]
    found: list[dict] = []

    def add(kind: str, item: str, **extra: Any) -> None:
        found.append({"keepout": spec["name"], "kind": kind, "item": item, **extra})

    if rules["tracks"] == "not_allowed":
        for t in copper.tracks:
            if t["layer"] in layers and edge_loops.shape_distance(ring, True, t["points"], False) - t["half"] <= SAMPLING_TOL_MM:
                add("track", t["uuid"], layer=t["layer"])
    if rules["vias"] == "not_allowed":
        for v in copper.vias:
            if v["layers"] & layers and edge_loops.shape_distance(ring, True, [v["center"]], False) - v["radius"] <= SAMPLING_TOL_MM:
                add("via", v["uuid"])
    if rules["pads"] == "not_allowed":
        for p in copper.pads:
            if p["layers"] & layers and edge_loops.ring_distance(ring, p["ring"]) == 0.0:
                add("pad", f"{p['reference']}.{p['number']}")
    if rules["footprints"] == "not_allowed":
        for c in copper.courtyards:
            if c["layer"] in layers and edge_loops.ring_distance(ring, c["ring"]) == 0.0:
                add("footprint", c["reference"])
    return found


# --------------------------------------------------------------------------- the plan


def _plan(
    pcb_path: Path,
    keepouts: Any,
    slots: Any,
    remove_keepouts: Any,
    remove_slots: Any,
    edge_margin_mm: Any,
    copper_margin_mm: Any,
) -> tuple[dict | None, dict | None]:
    """``(plan, None)`` or ``(None, refusal)``. Reads the board only."""
    for label, value in (("keepouts", keepouts), ("slots", slots), ("remove_keepouts", remove_keepouts), ("remove_slots", remove_slots)):
        if value is not None and (not isinstance(value, list) or len(value) > MAX_PER_LIST):
            return None, _refused(f"{label} must be a list of at most {MAX_PER_LIST} entries")
    for label, value in (("edge_margin_mm", edge_margin_mm), ("copper_margin_mm", copper_margin_mm)):
        if not _num(value) or value < 0:
            return None, _refused(f"{label} must be a number >= 0")
    keepouts, slots = keepouts or [], slots or []
    remove_keepouts, remove_slots = remove_keepouts or [], remove_slots or []
    if not all(isinstance(x, str) and x for x in remove_keepouts + remove_slots):
        return None, _refused("remove_keepouts and remove_slots are lists of names and slot ids (strings)")
    try:
        root = input_prep._parse(pcb_path)
    except Exception as exc:  # noqa: BLE001
        return None, {"status": "incomplete", "reason": f"failed to parse board: {exc}", "revision_token": None}

    copper = _Copper(root)
    existing_keepouts = _existing_keepouts(root)
    outline = edge_loops.classify(root)
    existing_slots = [{"id": s.id, "kinds": list(s.kinds), "box": [_r(v) for v in s.box]} for s in outline.slots]

    # removals first: a name freed here may be used again by an addition of the same call
    removed_k: list[dict] = []
    for name in dict.fromkeys(remove_keepouts):
        hits = [k for k in existing_keepouts if k["name"] == name]
        if len(hits) != 1:
            return None, _refused(
                f"remove_keepouts: {len(hits)} keepout zones are named {name!r} (exactly one is needed)",
                existing_keepouts=_bounded(existing_keepouts),
            )
        if not hits[0]["uuid"]:
            return None, _refused(f"remove_keepouts: the keepout {name!r} is without a uuid, so it cannot be told from other nodes without one: it is not removed")
        removed_k.append({"name": name, "uuid": hits[0]["uuid"], "layers": hits[0]["layers"]})
    removed_s: list[dict] = []
    for ident in dict.fromkeys(remove_slots):
        hits = [s for s in outline.slots if s.id == ident]
        if len(hits) != 1 or not ident:
            return None, _refused(f"remove_slots: no slot has the id {ident!r}", existing_slots=_bounded(existing_slots))
        if not all(hits[0].uuids):
            return None, _refused(f"remove_slots: slot {ident} has an item without a uuid (a KiCad 7 file), so it cannot be told from other nodes without one: it is not removed")
        removed_s.append({"id": ident, "kinds": list(hits[0].kinds), "box": [_r(v) for v in hits[0].box], "uuids": list(hits[0].uuids)})
    if (slots or removed_s) and (outline.problems or outline.outer is None):
        return None, _refused(
            "the Edge.Cuts outline is not one closed outer outline with closed slots inside it, so no slot is added or "
            "removed: " + "; ".join(outline.problems),
            outline_problems=list(outline.problems),
        )

    # additions
    kept_names = {k["name"] for k in existing_keepouts if k["name"] not in {r["name"] for r in removed_k}}
    added_k: list[dict] = []
    refused: list[str] = []
    for spec in keepouts:
        norm, problem = _norm_keepout(spec, copper.layers)
        if problem:
            refused.append(problem)
        elif norm["name"] in kept_names or any(norm["name"] == a["name"] for a in added_k):
            refused.append(f"keepout {norm['name']}: a keepout zone of that name exists or is declared twice")
        else:
            added_k.append(norm)
    added_s: list[dict] = []
    rings: list[list[tuple[float, float]]] = []
    for i, spec in enumerate(slots):
        norm, problem = _norm_slot(spec, i)
        if problem:
            refused.append(problem)
            continue
        added_s.append(norm)
        rings.append(_slot_geometry(norm)[1])
    if refused:
        return None, _refused("; ".join(refused), problems=refused)

    conflicts: list[dict] = []
    zone_overlaps: list[dict] = []
    removed_slot_ids = {r["id"] for r in removed_s}
    for i, ring in enumerate(rings):
        label = f"slot {i}"
        assert outline.outer is not None
        outer = list(outline.outer.points)
        if not all(edge_loops.point_in_ring(p, outer) for p in ring) or edge_loops.boundary_distance(ring, outer) < max(edge_margin_mm, edge_loops.SNAP * 2):
            conflicts.append({"slot": label, "kind": "outline", "item": "outer outline", "reason": f"closer than {edge_margin_mm} mm to the outer outline or outside it (a slot that reaches the edge changes the outline: use propose_board_outline)"})
        for other in outline.slots:
            if other.id not in removed_slot_ids and _too_close(ring, list(other.points), edge_margin_mm):
                conflicts.append({"slot": label, "kind": "slot", "item": other.id, "reason": f"closer than {edge_margin_mm} mm to an existing slot"})
        for j in range(i):
            if _too_close(ring, rings[j], edge_margin_mm):
                conflicts.append({"slot": label, "kind": "slot", "item": f"slot {j}", "reason": f"closer than {edge_margin_mm} mm to another new slot"})
        conflicts += _slot_conflicts(label, ring, copper, copper_margin_mm)
        zone_overlaps += _pour_overlaps(label, ring, copper)
    for spec in added_k:
        conflicts += _keepout_conflicts(spec, copper)
        if spec["rules"]["copperpour"] == "not_allowed":
            zone_overlaps += _pour_overlaps(spec["name"], [tuple(p) for p in spec["polygon"]], copper, set(spec["layers"]))
    if conflicts:
        return None, _refused(
            f"{len(conflicts)} existing item(s) or neighbours conflict with what was asked: nothing is proposed. "
            "Move or remove them (or change the slot or keepout) first; there is no override",
            conflicts=_bounded(conflicts),
        )
    return (
        {
            "keepouts_add": added_k,
            "keepouts_remove": removed_k,
            "slots_add": added_s,
            "slots_remove": removed_s,
            "zone_overlaps": zone_overlaps,
            "margins": {"edge_margin_mm": edge_margin_mm, "copper_margin_mm": copper_margin_mm},
            "keepouts_after": len(existing_keepouts) - len(removed_k) + len(added_k),
            "slots_after": len(existing_slots) - len(removed_s) + len(added_s),
        },
        None,
    )


def _changes(plan: dict) -> int:
    return len(plan["keepouts_add"]) + len(plan["keepouts_remove"]) + len(plan["slots_add"]) + len(plan["slots_remove"])


# --------------------------------------------------------------------------- checkpoint


def _default_output_dir(pcb_path: Path) -> Path:
    resolved = pcb_path.resolve()
    return resolved.parent.parent / f"{resolved.parent.name}_isolated"


def propose_keepouts_and_slots(
    pcb_path: str | Path,
    keepouts: list[dict] | None = None,
    slots: list[dict] | None = None,
    remove_keepouts: list[str] | None = None,
    remove_slots: list[str] | None = None,
    output_dir: str | Path | None = None,
    edge_margin_mm: float = DEFAULT_MARGIN_MM,
    copper_margin_mm: float = DEFAULT_MARGIN_MM,
) -> dict:
    """Dry run: validate every keepout and slot, find the existing items they would hit, list what
    would be added and removed; write nothing; return a revision token. With nothing asked it only
    lists the board's keepouts and slots (``nothing_to_do``, no token)."""
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}", "revision_token": None}
    plan_args = copy.deepcopy((keepouts, slots, remove_keepouts, remove_slots, edge_margin_mm, copper_margin_mm))
    plan, refusal = _plan(src, *plan_args)
    if refusal is not None:
        return refusal
    assert plan is not None
    if _changes(plan) == 0:
        root = input_prep._parse(src)
        outline = edge_loops.classify(root)
        return {
            "status": "nothing_to_do",
            "reason": "no keepout or slot to add or remove was given",
            "revision_token": None,
            "existing_keepouts": _bounded(_existing_keepouts(root)),
            "existing_slots": _bounded([{"id": s.id, "kinds": list(s.kinds), "box": [_r(v) for v in s.box]} for s in outline.slots]),
            "outline_problems": list(outline.problems),
        }
    out_dir = Path(output_dir) if output_dir else _default_output_dir(src)
    problem = checkpoint.output_dir_problem(src, out_dir, what="the isolated board")
    if problem:
        return {"status": "error", "reason": problem, "revision_token": None}
    try:
        token = _PENDING.issue(src, {"out_dir": str(out_dir), "plan_args": plan_args, "plan": copy.deepcopy(plan)})
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc), "revision_token": None}
    return {
        "status": "proposed",
        "revision_token": token,
        "output_pcb": str(out_dir / src.name),
        **_summary(plan),
        "note": (
            "creates a new board revision; the original is untouched. A slot is missing material, a keepout is a rule: "
            "neither is routed or checked by this tool beyond the outline and keepout errors of kicad-cli; "
            "run verify_board for the verdict. Call confirm_keepouts_and_slots with this revision_token to apply."
        ),
    }


def _summary(plan: dict) -> dict:
    return {
        "keepouts_add": plan["keepouts_add"],
        "keepouts_remove": plan["keepouts_remove"],
        "slots_add": [{k: v for k, v in s.items() if k != "uuids"} | {"id": s["uuids"][0]} for s in plan["slots_add"]],
        "slots_remove": [{k: v for k, v in s.items() if k != "uuids"} for s in plan["slots_remove"]],
        "zone_overlaps": _bounded(plan["zone_overlaps"]),
        **({"pour_note": _POUR_NOTE} if plan["zone_overlaps"] else {}),
        "keepouts_after": plan["keepouts_after"],
        "slots_after": plan["slots_after"],
        "margins": plan["margins"],
    }


def _write(src: Path, tmp: Path, plan: dict) -> dict:
    """Copy the project into ``tmp``, remove and add the nodes and save through the write layer.
    Returns what ``_verify`` compares against."""
    tmp_pcb = board_outline._copy_project(src, tmp)
    root = write_layer.load(tmp_pcb)
    drop_uuids = {r["uuid"] for r in plan["keepouts_remove"]} | {u for r in plan["slots_remove"] for u in r["uuids"]}
    for node in [c for c in root.children if not c.is_atom and c.name in ("zone", "gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly") and board_outline._uuid(c) in drop_uuids]:
        root.remove(node)
    written: list[Any] = []
    for spec in plan["slots_add"]:
        pieces, _ring = _slot_geometry(spec)
        written += _slot_nodes(pieces, spec["uuids"])
    written += [_keepout_node(spec) for spec in plan["keepouts_add"]]
    for node in written:
        root.append(node)
    write_layer.save(root, tmp_pcb)
    return {"pcb": tmp_pcb, "root": root, "written": written}


def _drc_counts(pcb: Path, drc: Any) -> tuple[Counter, dict | None]:
    try:
        if drc is None:
            from boardproof.kicad_cli import run_drc

            report = run_drc(pcb)
        else:
            report = drc(pcb)
    except Exception as exc:  # noqa: BLE001
        return Counter(), {"status": "not_run", "reason": str(exc)}
    return Counter(v.get("type") for v in report.get("violations", []) if v.get("type") in _WATCHED), None


def _verify(src: Path, state: dict, plan: dict, drc: Any) -> dict:
    """From a fresh parse of the saved file, never the in-memory tree: the whole file against the
    intended change, the slots and keepouts against the plan, then kicad-cli DRC."""
    tmp_pcb: Path = state["pcb"]
    name = tmp_pcb.name
    try:
        fresh = input_prep._parse(tmp_pcb)
        source = input_prep._parse(src)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: revision failed to re-parse: {exc}", [checkpoint.mismatch(name, "board", "a board that parses", str(exc))]
        ) from exc
    drop_uuids = {r["uuid"] for r in plan["keepouts_remove"]} | {u for r in plan["slots_remove"] for u in r["uuids"]}
    removed = [c for c in source.children if not c.is_atom and board_outline._uuid(c) in drop_uuids and c.name in ("zone", "gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly")]
    write_check = _self_check.reload_differences(
        source=source, removed=removed, written=state["written"], memory=state["root"], reloaded=fresh
    )
    if write_check["missing"] or write_check["unexpected"]:
        raise checkpoint.NotVerified(
            "CRITICAL: the reloaded revision is not the intended write: " + _self_check.describe(write_check),
            checkpoint.reload_mismatches(name, write_check),
            write_check=_self_check.bounded(write_check),
        )

    # slots: the outline is still one outer loop with closed slots, outer unchanged, slot set as planned
    before_outline, after_outline = edge_loops.classify(source), edge_loops.classify(fresh)
    if after_outline.problems or after_outline.outer is None:
        raise checkpoint.NotVerified(
            "CRITICAL: Edge.Cuts after save+reload is not one closed outline with closed slots",
            [checkpoint.mismatch(name, "Edge.Cuts", "one outer loop with closed slots", "; ".join(after_outline.problems) or "no outer loop")],
        )
    if before_outline.outer is not None and (
        any(abs(a - b) > 1e-4 for a, b in zip(before_outline.outer.box, after_outline.outer.box, strict=True))
        or before_outline.outer.id != after_outline.outer.id
    ):
        raise checkpoint.NotVerified(
            "CRITICAL: the outer outline changed", [checkpoint.mismatch(name, "outer outline", list(before_outline.outer.box), list(after_outline.outer.box))]
        )
    want_ids = {s.id for s in before_outline.slots} - {r["id"] for r in plan["slots_remove"]} | {s["uuids"][0] for s in plan["slots_add"]}
    have = {s.id: s for s in after_outline.slots}
    if set(have) != want_ids:
        raise checkpoint.NotVerified(
            "CRITICAL: the slots after reload are not the planned ones",
            [checkpoint.mismatch(name, "slot ids", sorted(want_ids), sorted(have))],
        )
    for spec in plan["slots_add"]:
        pieces, ring = _slot_geometry(spec)
        got = have[spec["uuids"][0]]
        want_box = edge_loops.bbox(ring)
        if len(got.nodes) != len(pieces) or any(abs(a - b) > 1e-3 for a, b in zip(want_box, got.box, strict=True)) or abs(abs(edge_loops.ring_area(list(got.points))) - abs(edge_loops.ring_area(ring))) > 1e-2:
            raise checkpoint.NotVerified(
                f"CRITICAL: slot {spec['uuids'][0]} after reload is not the planned shape",
                [checkpoint.mismatch(name, f"slot {spec['uuids'][0]}", {"box": list(want_box), "items": len(pieces)}, {"box": list(got.box), "items": len(got.nodes)})],
            )

    # keepouts: names, layers, rules and polygon as planned
    after_k = {k["name"]: k for k in _existing_keepouts(fresh)}
    before_names = [k["name"] for k in _existing_keepouts(source)]
    want_names = sorted((set(before_names) - {r["name"] for r in plan["keepouts_remove"]}) | {k["name"] for k in plan["keepouts_add"]})
    if sorted(after_k) != want_names:
        raise checkpoint.NotVerified(
            "CRITICAL: the keepout zones after reload are not the planned ones",
            [checkpoint.mismatch(name, "keepout zones", want_names, sorted(after_k))],
        )
    for spec in plan["keepouts_add"]:
        got = after_k[spec["name"]]
        poly_off = len(got["polygon"]) != len(spec["polygon"]) or any(
            abs(a - _self_check.as_written(b)) > 1e-4 for gp, sp in zip(got["polygon"], spec["polygon"], strict=True) for a, b in zip(gp, sp, strict=True)
        )
        if got["layers"] != spec["layers"] or got["rules"] != spec["rules"] or poly_off:
            raise checkpoint.NotVerified(
                f"CRITICAL: keepout {spec['name']} after reload is not the planned one",
                [checkpoint.mismatch(name, f"keepout {spec['name']}", {"layers": spec["layers"], "rules": spec["rules"], "polygon": spec["polygon"]}, got)],
            )

    # kicad-cli: the change must not add outline or keepout errors
    watch = {}
    if plan["slots_add"] or plan["keepouts_add"]:
        before, bad = _drc_counts(src, drc)
        after, bad_after = _drc_counts(tmp_pcb, drc)
        if bad or bad_after:
            raise checkpoint.Failed(
                "incomplete",
                "the revision could not be checked, so none was produced: kicad-cli DRC did not run "
                f"({(bad or bad_after or {}).get('reason')})",
            )
        grown = {t: {"before": before.get(t, 0), "after": after[t]} for t in _WATCHED if after.get(t, 0) > before.get(t, 0)}
        if grown:
            raise checkpoint.Failed(
                "error",
                "kicad-cli reports new violations on the revision, so none was produced: "
                + ", ".join(f"{t} {v['before']} -> {v['after']}" for t, v in grown.items())
                + " (for copper_edge_clearance try a larger copper_margin_mm)",
                drc_growth=grown,
            )
        watch = {t: {"before": before.get(t, 0), "after": after.get(t, 0)} for t in _WATCHED}
    return {"drc_watched": watch, "write_check": _self_check.bounded(write_check)}


def confirm_keepouts_and_slots(pcb_path: str | Path, revision_token: str, *, drc: Any = None) -> dict:
    """Apply a proposal: build the revision, verify it from a fresh reload, run kicad-cli DRC, move
    it into place. ``drc`` replaces the kicad-cli call (tests only). The token is consumed only by
    success or by detected source drift."""
    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    out_dir = Path(entry["out_dir"])
    problem = checkpoint.output_dir_problem(src, out_dir, what="the isolated board")
    if problem:
        return {"status": "error", "reason": problem}
    plan, refusal = _plan(src, *copy.deepcopy(entry["plan_args"]))
    if refusal is not None:
        return {**refusal, "status": "error", "note": "the revision_token is kept"}
    if plan != entry["plan"]:
        return {
            "status": "error",
            "reason": "re-planning the same arguments now gives a different result than the proposal: re-run propose_keepouts_and_slots",
            "note": "the revision_token is kept",
        }
    built = checkpoint.build_revision(
        out_dir,
        source_pcb=src,
        write=lambda tmp: _write(src, tmp, plan),
        verify=lambda tmp, state: _verify(src, state, plan, drc),
        success="built",
        before_verified="keepout and slot build failed before a revision was verified",
    )
    if built["status"] != "built":
        return built
    _PENDING.spend(revision_token)
    return {
        "status": "built",
        "output_pcb": str(out_dir / src.name),
        **_summary(plan),
        "drc_watched": built["drc_watched"],
        "write_check": built["write_check"],
        "note": "outline and keepout errors of kicad-cli only; run verify_board for the verdict.",
    }
