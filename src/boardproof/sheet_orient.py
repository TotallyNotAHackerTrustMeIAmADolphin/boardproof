"""Rotate and mirror placed symbols of a schematic sheet in batches (GitHub issue 93).

``rotate_items`` and ``mirror_items`` change the orientation of existing symbols in place: the symbol
keeps its origin (the raw ``(at x y ...)``), its uuid, reference, unit and instance data; only the
stored rotation angle and the ``(mirror ...)`` node change, and what hangs on the symbol is handled
here rather than left behind:

- **Orientation.** The stored form is ``(at x y angle)`` plus an optional ``(mirror x|y)``; the pin
  positions follow ``sheet_editing.place`` (rotate, then mirror; measured against kicad-cli's netlist
  for four rotations and both mirrors, tests/test_sheet_editing.py). Rotation counts counter-clockwise
  on the screen (library Y points up, the sheet's Y down; a pin at the top of a symbol goes to its left
  for ``by`` 90). ``mirror_items`` takes the axis named as the stored node names it: "x" swaps top and
  bottom, "y" swaps left and right. A mirror and a rotation of 180 degrees can be stored in two ways
  ((mirror x, angle a) and (mirror y, angle a + 180) are the same picture); the tool keeps the form the
  symbol already has. The new form is found by transforming the symbol's axes with the same ``place``
  and is then proved: the pins of the written tree must sit where the old pin offsets, transformed on
  the sheet, say (``write_not_verified``, check ``orientation``, nothing written).
- **Pins follow.** Wire ends, junctions, labels and no-connect markers that sat exactly on a pin
  follow THAT pin to its new place (each pin has its own offset, unlike a move). A wire whose end
  moved and is no longer horizontal or vertical is listed under ``slanted``. kicad-cli's netlist of the
  project before and after is compared (``connectivity_changed`` unless allowed): a pin that lands on
  the end of another wire, on another pin or on a label changes nets and is refused. A pin that lands
  in the middle of a foreign wire is neither connected by kicad-cli nor refused. Because the netlist is
  blind to wires laid over each other, a drag that runs a wire over a pin, label, marker, junction or
  wire end that is not its own (a 180 degree turn of a two-pin part wired on both sides) is refused
  by the tool itself as ``drag_crosses_connection``.
- **Property texts** (Reference, Value, ...) keep their offset from the origin transformed with the
  symbol (the text stays where it was relative to the part), their own angle and justification are
  not changed; ``property_texts: "keep"`` leaves them where they are. KiCad's own rotate command was
  not run (no GUI here), so this is the tool's rule, listed per text.
- **Grid.** The origin does not move, so nothing is rounded; pins that end up off the grid are listed
  (``off_grid_pins``, grid 1.27 mm by default) because KiCad connects by exact position only.
"""

from __future__ import annotations

import math
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import sheet_editing as se
from boardproof import write_layer
from boardproof.sheet_authoring import SheetAuthoringError

ROTATIONS = (0, 90, 180, 270)
MIRRORS = ("", "x", "y")
BY = (90, 180, 270)
PROPERTY_TEXTS = ("follow", "keep")
ITEM_KEYS = {"by", "to", "axis"}
Vec = tuple[float, float]


# --------------------------------------------------------------------------- orientation maths


def _rotate_ccw(phi: int):
    c, s = round(math.cos(math.radians(phi))), round(math.sin(math.radians(phi)))

    def op(x: float, y: float) -> Vec:
        return (x * c + y * s, -x * s + y * c)

    return op


def _flip(axis: str):
    def op(x: float, y: float) -> Vec:
        return (x, -y) if axis == "x" else (-x, y)

    return op


def _basis(rot: int, mirror: str) -> tuple[Vec, Vec]:
    """Where the library x axis and y axis point on the sheet for this stored orientation."""
    return se.place(1.0, 0.0, rot, mirror), se.place(0.0, 1.0, rot, mirror)


def _close(a: tuple[Vec, Vec], b: tuple[Vec, Vec]) -> bool:
    return all(abs(p[0] - q[0]) < 1e-9 and abs(p[1] - q[1]) < 1e-9 for u, v in zip(a, b, strict=True) for p, q in [(u, v)])


def orientation_after(rot: int, mirror: str, op, preferred_mirror: str) -> tuple[int, str]:
    """The stored ``(angle, mirror)`` whose axes are those of ``(rot, mirror)`` after the visual change ``op``."""
    target = tuple(op(*v) for v in _basis(rot, mirror))
    found = [(r, m) for r in ROTATIONS for m in MIRRORS if _close(_basis(r, m), target)]  # type: ignore[arg-type]
    if not found:  # cannot happen for a rotation by a multiple of 90 or a flip
        raise SheetAuthoringError("write_not_verified", "no stored orientation matches the requested change; nothing was written")
    found.sort(key=lambda rm: (rm[1] != preferred_mirror, rm[0]))
    return found[0]


def _stored_rotation(node: SExp, who: str) -> int:
    angle = se._at(node)[2]
    if angle % 90 != 0:
        raise SheetAuthoringError("unusual_orientation", f"{who} is placed at {angle:g} degrees; only multiples of 90 are handled; nothing was written")
    mirror = se._mirror(node)
    if mirror not in MIRRORS:
        raise SheetAuthoringError("unusual_orientation", f"{who} has (mirror {mirror}); only x and y are handled; nothing was written")
    return int(angle) % 360


def _set_orientation(node: SExp, rot: int, mirror: str) -> None:
    at = node.find_children("at")[0]
    write_layer.set_number(at, 2, rot)  # index 2 may be one past the end: set_number appends then
    found = node.find_children("mirror")
    if mirror == "":
        for m in found:
            node.children.remove(m)
    elif found:
        write_layer.set_symbol(found[0], 0, mirror)
    else:
        node.children.insert(node.children.index(at) + 1, write_layer.SExp.list("mirror", write_layer.symbol(mirror)))


# --------------------------------------------------------------------------- checks


def _check_args(items: Any, move_wires: Any, property_texts: Any, grid: Any) -> list[dict]:
    if not isinstance(move_wires, bool):
        raise SheetAuthoringError("invalid_spec", "move_wires must be true or false")
    if property_texts not in PROPERTY_TEXTS:
        raise SheetAuthoringError("invalid_spec", f"property_texts must be 'follow' or 'keep', got {property_texts!r}")
    if not se.is_mm(grid) or grid <= 0:
        raise SheetAuthoringError("invalid_spec", f"grid must be a positive, finite number of mm (at most {se.MAX_MM:g}), got {grid!r}")
    if not isinstance(items, list) or not items:
        raise SheetAuthoringError("invalid_spec", "items must be a non-empty list of objects with symbol selector keys and by, to or axis")
    out = []
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise SheetAuthoringError("invalid_spec", f"items[{i}] must be an object")
        spec = {k: v for k, v in item.items() if k not in ITEM_KEYS}
        unknown = set(spec) - se._SELECTOR_KEYS["symbol"]
        if unknown:
            raise SheetAuthoringError("invalid_spec", f"items[{i}] has keys {sorted(unknown)} that this tool does not take")
        if not spec:
            raise SheetAuthoringError("invalid_spec", f"items[{i}] needs a symbol selector (references, uuids, pattern, region or lib_id)")
        out.append(item)
    return out


def _whole_number(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value != int(value):
        return None
    return int(value)


# --------------------------------------------------------------------------- wires that would run over a connection


def _points(sheet: se.Sheet) -> list[tuple[Vec, str]]:
    """Every place something other than a plain wire body connects: pins, labels, markers, junctions, sheet pins
    and wire ends (the owner is the wire's uuid, so a wire's own ends are skipped)."""
    pts: list[tuple[Vec, str]] = [(p, "") for p in se._connectors(sheet)]
    for wire in sheet.wires:
        ends = se._wire_ends(wire)
        pts += [(ends[0], se._uuid(wire)), (ends[-1], se._uuid(wire))]
    return pts


def _crossings(sheet: se.Sheet, wires: list[SExp], points: list[tuple[Vec, str]]) -> set[tuple[str, tuple[float, float]]]:
    """``(wire uuid, point)`` where a point that is not an end of the wire lies on the wire's run."""
    found = set()
    for wire in wires:
        ends = se._wire_ends(wire)
        a, b = ends[0], ends[-1]
        for point, owner in points:
            if owner == se._uuid(wire) or se._same(point, a) or se._same(point, b):
                continue
            if se._segment_contains(a, b, point):
                found.add((se._uuid(wire), se._key(*point)))
    return found


# --------------------------------------------------------------------------- the shared pipeline


def _pin_rows(old: list[dict], new: list[dict], grid: float) -> tuple[list[dict], list[list]]:
    rows, off = [], []
    for a, b in zip(old, new, strict=True):
        rows.append({"number": a["number"], "name": a["name"], "was": [a["x"], a["y"]], "now": [b["x"], b["y"]]})
        steps = (b["x"] / grid, b["y"] / grid)
        if any(abs(s - round(s)) * grid > se.TOL for s in steps):
            off.append([a["number"], b["x"], b["y"]])
    return rows, off


def _run(op_name: str, path: str, items: Any, root_sch: str | None, *, key: str, plan, move_wires: bool, property_texts: str,
         grid: Any, allow_connectivity_change: bool, dry_run: bool) -> dict[str, Any]:
    """``plan(sheet, items)`` returns ``[(node, new_rot, new_mirror, op, record)]`` (``op`` the visual change of offsets)."""

    def mutate(sheet: se.Sheet, root: SExp) -> se.Outcome:
        checked = _check_args(items, move_wires, property_texts, grid)
        planned = plan(sheet, checked)
        outcome = se.Outcome(dangling_before=se.dangling(sheet), no_change=not any(p[5] for p in planned))
        anchors: dict[tuple[float, float], tuple[float, float]] = {}
        points_before = _points(sheet)
        crossings_before = _crossings(sheet, sheet.wires, points_before) if move_wires else set()
        listed: list[dict] = []
        off_grid_all: list[dict] = []
        for node, new_rot, new_mirror, op, record, changes in planned:
            ox, oy, old_rot = se._at(node)
            old_mirror = se._mirror(node)
            old_pins = se.pin_positions(root, node)
            if se._lib_symbol(root, node) is None:
                raise SheetAuthoringError("no_symbol_geometry", f"the sheet holds no library symbol for {se._ref(node)}; its pins and wires cannot be followed, so nothing was written")
            entry: dict[str, Any] = {"reference": se._ref(node), "uuid": se._uuid(node), "unit": se._unit(node), "at": se._round_pt(ox, oy),
                                     "before": {"rotation": int(old_rot) % 360, "mirror": old_mirror},
                                     "after": {"rotation": new_rot, "mirror": new_mirror}, "changed": changes, **record}
            if changes:
                outcome.changed.add(se._uuid(node))
                _set_orientation(node, new_rot, new_mirror)
                texts = []
                for prop in se._kids(node, "property"):
                    px, py, pa = se._at(prop)
                    if property_texts == "follow":
                        nx, ny = op(px - ox, py - oy)
                        se._set_at(prop, ox + nx, oy + ny)
                    nxt = se._at(prop)
                    texts.append({"name": prop.get_string(0), "was": se._round_pt(px, py), "now": se._round_pt(nxt[0], nxt[1])})
                entry["property_texts"] = texts
            new_pins = se.pin_positions(root, node)
            expected = [(se._r(ox + op(p["x"] - ox, p["y"] - oy)[0]), se._r(oy + op(p["x"] - ox, p["y"] - oy)[1])) for p in old_pins]
            got = [(p["x"], p["y"]) for p in new_pins]
            if len(got) != len(expected) or any(abs(a[0] - b[0]) > se.TOL or abs(a[1] - b[1]) > se.TOL for a, b in zip(got, expected, strict=True)):
                raise SheetAuthoringError("write_not_verified", f"check orientation: the pins of {se._ref(node)} are not where the change puts them "
                                          f"(expected {expected[:3]}, got {got[:3]}); nothing was written", check="orientation")
            rows, off = _pin_rows(old_pins, new_pins, float(grid))
            entry["pins"] = rows
            if off:
                entry["off_grid_pins"] = off
                off_grid_all.append({"reference": se._ref(node), "pins": off})
            for a, b in zip(old_pins, new_pins, strict=True):
                if abs(a["x"] - b["x"]) <= se.TOL / 10 and abs(a["y"] - b["y"]) <= se.TOL / 10:
                    continue
                k = se._key(a["x"], a["y"])
                if k in anchors and not (abs(anchors[k][0] - b["x"]) <= se.TOL and abs(anchors[k][1] - b["y"]) <= se.TOL):
                    raise SheetAuthoringError(
                        "ambiguous_drag", f"two pins sit at {[a['x'], a['y']]} and move to different places, so the wire ends there cannot follow both; "
                        "change them in separate calls or with move_wires false")
                anchors[k] = (b["x"], b["y"])
            listed.append(entry)
        followed: dict[str, list] = {"wires": [], "junctions": [], "labels": [], "no_connects": []}
        slanted: list[str] = []
        if move_wires and anchors:
            for wire in sheet.wires:
                ends = se._wire_ends(wire)
                new_ends = list(ends)
                for idx in (0, len(ends) - 1):
                    target = anchors.get(se._key(*ends[idx]))
                    if target is not None:
                        new_ends[idx] = target
                if new_ends != ends:
                    outcome.changed.add(se._uuid(wire))
                    for idx in (0, len(ends) - 1):
                        if new_ends[idx] != ends[idx]:
                            se._set_wire_end(wire, idx, *new_ends[idx])
                    followed["wires"].append({"uuid": se._uuid(wire), "was": [se._round_pt(*e) for e in ends], "now": [se._round_pt(*e) for e in new_ends]})
                    if len(new_ends) >= 2 and abs(new_ends[0][0] - new_ends[-1][0]) > se.TOL and abs(new_ends[0][1] - new_ends[-1][1]) > se.TOL:
                        slanted.append(se._uuid(wire))
            for group, nodes in (("junctions", sheet.junctions), ("labels", sheet.labels), ("no_connects", sheet.no_connects)):
                for node in nodes:
                    x, y, _a = se._at(node)
                    target = anchors.get(se._key(x, y))
                    if target is not None:
                        se._set_at(node, *target)
                        outcome.changed.add(se._uuid(node))
                        followed[group].append({"uuid": se._uuid(node), "was": se._round_pt(x, y), "now": se._round_pt(*target)})
        if move_wires and followed["wires"]:
            dragged = {w["uuid"] for w in followed["wires"]}
            fresh = sorted(_crossings(sheet, [w for w in sheet.wires if se._uuid(w) in dragged], _points(sheet)) - crossings_before)
            if fresh:
                raise SheetAuthoringError(
                    "drag_crosses_connection", f"following the pins would run {len(fresh)} wire(s) over a pin, label, marker, junction or wire end that is not theirs "
                    f"(first: wire {fresh[0][0]} over {list(fresh[0][1])}); nothing was written. Turn the part in another way, move the other item first, or use move_wires false.",
                    crossings=[{"wire": u, "point": list(p)} for u, p in fresh[:se.MAX_LISTED]])
        after = se.dangling(sheet)
        created = {k: [u for u in v if u not in set(outcome.dangling_before.get(k, []))] for k, v in after.items()}
        outcome.report = {
            key: listed, "count": len(listed), "unchanged": [e["reference"] for e in listed if not e["changed"]],
            "move_wires": move_wires, "property_texts": property_texts, "followed": followed, "slanted": slanted,
            "dangling_created": created, "grid": float(grid), "off_grid_pins": off_grid_all,
            "move_wires_note": ("wire ends, junctions, labels and no-connect markers that sat exactly on a pin moved with that pin, so wires can end up slanted (listed)"
                                if move_wires else "move_wires is false: nothing attached was moved; wires that ended on a pin now end where the pin was"),
            "grid_note": f"origins do not move; pins whose new position is off the {float(grid)} mm grid are listed under off_grid_pins",
            "property_texts_note": ("property texts kept their offset from the origin, transformed with the symbol; their own angle and justification are unchanged (set_property_positions adjusts them)"
                                    if property_texts == "follow" else "property texts were left where they are"),
        }
        return outcome

    result = se.run(path, root_sch, mutate, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result[f"would_{op_name}"] = result.pop(key)
    return result


def _pick(sheet: se.Sheet, items: list[dict], allowed: set[str]) -> list[tuple[dict, list[SExp]]]:
    seen: set[int] = set()
    out = []
    for i, item in enumerate(items):
        nodes = se.select(sheet, "symbol", [{k: v for k, v in item.items() if k not in allowed}])
        for node in nodes:
            if id(node) in seen:
                raise SheetAuthoringError("invalid_spec", f"items[{i}] picks a symbol that an earlier item already picks ({se._uuid(node)})")
            seen.add(id(node))
        out.append((item, nodes))
    return out


# --------------------------------------------------------------------------- entry points


def rotate(path: str, items: list[dict], root_sch: str | None, *, move_wires: bool = True, property_texts: str = "follow",
           grid: float = se.GRID, allow_connectivity_change: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Rotate the symbols the items pick by ``by`` (90, 180, 270 counter-clockwise) or to the stored angle ``to``."""

    def plan(sheet: se.Sheet, checked: list[dict]):
        planned = []
        for i, (item, nodes) in enumerate(_pick(sheet, checked, {"by", "to"})):
            by, to = item.get("by"), item.get("to")
            if (by is None) == (to is None):
                raise SheetAuthoringError("invalid_spec", f"items[{i}] needs exactly one of 'by' (90, 180 or 270, counter-clockwise on the screen) or 'to' (stored angle 0, 90, 180 or 270)")
            if by is not None and _whole_number(by) not in BY:
                raise SheetAuthoringError("invalid_spec", f"items[{i}].by must be 90, 180 or 270 (counter-clockwise), got {by!r}")
            if to is not None and _whole_number(to) not in ROTATIONS:
                raise SheetAuthoringError("invalid_spec", f"items[{i}].to must be 0, 90, 180 or 270, got {to!r}")
            for node in nodes:
                rot, mirror = _stored_rotation(node, se._ref(node)), se._mirror(node)
                if by is not None:
                    phi = int(by)
                    op = _rotate_ccw(phi)
                    new_rot, new_mirror = orientation_after(rot, mirror, op, mirror)
                    record = {"by": phi}
                else:
                    new_rot, new_mirror = int(to), mirror
                    # the visual rotation that takes the old axes to the new ones (one of the four always does: same handedness)
                    op = next(_rotate_ccw(p) for p in ROTATIONS if _close(tuple(_rotate_ccw(p)(*v) for v in _basis(rot, mirror)), _basis(new_rot, new_mirror)))
                    record = {"to": int(to)}
                planned.append((node, new_rot, new_mirror, op, record, (new_rot, new_mirror) != (rot, mirror)))
        return planned

    return _run("rotate", path, items, root_sch, key="rotated", plan=plan, move_wires=move_wires, property_texts=property_texts, grid=grid,
                allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)


def mirror(path: str, items: list[dict], root_sch: str | None, *, move_wires: bool = True, property_texts: str = "follow",
           grid: float = se.GRID, allow_connectivity_change: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Mirror the symbols the items pick about the axis ``axis`` ("x": top and bottom swap; "y": left and right swap)."""

    def plan(sheet: se.Sheet, checked: list[dict]):
        planned = []
        for i, (item, nodes) in enumerate(_pick(sheet, checked, {"axis"})):
            axis = item.get("axis")
            if axis not in ("x", "y"):
                raise SheetAuthoringError("invalid_spec", f"items[{i}].axis must be 'x' (top and bottom swap) or 'y' (left and right swap), got {axis!r}")
            op = _flip(axis)
            for node in nodes:
                rot, old = _stored_rotation(node, se._ref(node)), se._mirror(node)
                new_rot, new_mirror = orientation_after(rot, old, op, old or axis)
                planned.append((node, new_rot, new_mirror, op, {"axis": axis}, True))
        return planned

    return _run("mirror", path, items, root_sch, key="mirrored", plan=plan, move_wires=move_wires, property_texts=property_texts, grid=grid,
                allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)
