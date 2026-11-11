"""Add, remove and move the sheet pins of sheet symbols that already exist (GitHub issue 89).

``add_sheet_symbols`` (issue 75) only takes sheet files no sheet symbol references yet, so a pin
could not be added later to a sheet symbol that is already on the parent sheet. Three batch tools
change the pins of existing sheet symbols in place:

- ``add_sheet_pins``: append sheet pins to a sheet symbol, and create (or reuse) the hierarchical
  label of the same name and direction in the child sheet;
- ``remove_sheet_pins``: remove sheet pins; the child's hierarchical label stays unless
  ``remove_child_labels`` is true;
- ``move_sheet_pins``: move a pin along its edge or to another side.

Decision 1 (docs/ARCHITECTURE.md): nothing here edits KiCad text. The parent (and, for an added or
removed label, the child) is loaded with ``write_layer.load`` into ``kicad_tools``' low-level
``SExp`` tree, changed in memory with typed atoms, and saved with ``write_layer.save`` (kicad-cli
parse check, reload-and-compare, ``os.replace``). Decision 4, the order of work for every call:

1. select the sheet symbols (a name or uuid that matches nothing refuses the whole call) and plan
   every pin: side and position (snapped to the 1.27 mm grid, strictly inside the edge, never on
   another pin), direction, the child label (reused when name and direction agree, a different
   direction is ``label_direction_mismatch``);
2. change copies of the trees in memory;
3. staged check: every top-level node the call did not mean to touch is unchanged, on the parent
   and on the child (the child's symbols and their instance data included); the sheet symbol
   differs only in its pins;
4. the changed files are saved into a scratch copy of the project: the sheet tree must be whole, the
   sheet pins and hierarchical labels may disagree only where the call explains it, kicad-cli's ERC
   may show no violation type the project did not already show (besides the unconnected sheet pins
   and labels the call itself makes), and kicad-cli's netlist before and after is compared net by
   net (``sheet_editing.compare_nets``);
5. a pin that is wired (a wire end, label, junction, symbol pin or marker sits exactly on its point)
   cannot be removed or moved, and a new position may not land on one, unless
   ``allow_connectivity_change`` is true: ``connectivity_changed``, nothing written, the changes listed;
6. only then the real write: the children first, then the parent; a parent save that fails restores
   the children.

Two more tools compare and reconcile the pins with the child's hierarchical labels (issue 110):

- ``compare_sheet_pins`` (read-only, no lock, no scratch copy): per sheet symbol the pins without a
  label, the labels without a pin and the pins whose direction differs from their label's. kicad-cli's
  ERC reports the missing names (``hier_label_mismatch``) but not a different direction;
- ``sync_sheet_pins``: creates the missing pins from the child's labels (name and direction of the
  label), through the same plan and the same checks as ``add_sheet_pins``. It never creates or
  removes a hierarchical label, never changes a direction and never removes a pin: which side is right
  is the caller's call, so those differences come back as ``unresolved``.

A wire is not dragged along with a moved pin: the pin leaves the wire's end, which is a
connectivity change. Move the wire (``delete_wires`` and ``add_wires``) after moving the pin.

Sheet pins have no reference and no pin number, so ``get_symbol_pins`` and ``connect_pins`` cannot
address them. The result lists each pin's absolute position on the parent sheet: draw to it with
``add_wires`` (a point within 0.01 mm of a sheet pin lands exactly on it).
"""

from __future__ import annotations

import copy
import fnmatch
import shutil
import tempfile
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import sheet_authoring, sheet_editing as se, sheet_tree, write_layer
from boardproof.sheet_authoring import SheetAuthoringError

GRID = sheet_authoring.GRID
SIDES = sheet_authoring.SIDES
PIN_DIRECTIONS = ("input", "output", "bidirectional", "tri_state", "passive")
DEFAULT_SIDE = {"input": "left", "output": "right", "bidirectional": "left", "tri_state": "left", "passive": "left"}
_SIDE_BY_ANGLE = {180: "left", 0: "right", 90: "top", 270: "bottom"}
_DANGLING_ERC = frozenset({"unconnected_wire_endpoint", "isolated_pin_label", "label_dangling", "pin_not_connected", "hier_label_mismatch"})
_TAIL = ("sheet_instances", "embedded_fonts", "symbol_instances")
MAX_LISTED = se.MAX_LISTED


# --------------------------------------------------------------------------- small helpers


def _kids(node: SExp, tag: str) -> list[SExp]:
    return [c for c in node.children if not c.is_atom and c.name == tag]


def _text(node: SExp, index: int = 0) -> str:
    return node.get_string(index) or ""


def _specs(value: Any, what: str) -> list[dict]:
    if not isinstance(value, list) or not value or not all(isinstance(v, dict) for v in value):
        raise SheetAuthoringError("invalid_spec", f"{what} must be a non-empty list of objects")
    return value


def _only_keys(spec: dict, allowed: set[str], where: str) -> None:
    if set(spec) - allowed:
        raise SheetAuthoringError("invalid_spec", f"{where} may hold only {sorted(allowed)}, got {sorted(spec)}")


def _find_sheet(root: SExp, spec: dict, where: str) -> SExp:
    """The sheet symbol the spec names: ``sheet`` (its name, or its uuid) or ``uuid``."""
    sheets = _kids(root, "sheet")
    name, uid = spec.get("sheet"), spec.get("uuid")
    if (name is None) == (uid is None):
        raise SheetAuthoringError("invalid_spec", f"{where} needs exactly one of 'sheet' (the sheet name) and 'uuid'")
    wanted = name if name is not None else uid
    if not isinstance(wanted, str) or not wanted:
        raise SheetAuthoringError("invalid_spec", f"{where}: the sheet selector must be a non-empty string, got {wanted!r}")
    hits = [s for s in sheets if sheet_tree.sheet_name_property(s) == wanted] if name is not None else []
    if len(hits) > 1:
        raise SheetAuthoringError(
            "invalid_spec",
            f"{where}: {len(hits)} sheet symbols on this sheet are named {wanted!r}; name the one to change by its 'uuid'. Nothing was written",
            sheets=[{"name": sheet_tree.sheet_name_property(s), "uuid": se._uuid(s)} for s in hits])
    if not hits:
        hits = [s for s in sheets if se._uuid(s) == wanted]
    if not hits:
        raise SheetAuthoringError(
            "sheet_not_found", f"{where}: no sheet symbol named or numbered {wanted!r} on this sheet; nothing was written",
            sheets=[{"name": sheet_tree.sheet_name_property(s), "uuid": se._uuid(s)} for s in sheets])
    return hits[0]


@dataclass
class Geometry:
    x: float
    y: float
    w: float
    h: float

    def origin_length(self, side: str) -> tuple[float, float]:
        return (self.y, self.h) if side in ("left", "right") else (self.x, self.w)


def _geometry(sheet: SExp) -> Geometry:
    at, size = sheet.find_children("at"), sheet.find_children("size")
    if not at or not size:
        raise SheetAuthoringError("invalid_spec", f"sheet symbol {se._uuid(sheet)} has no (at ...) or (size ...)")
    return Geometry(at[0].get_float(0), at[0].get_float(1), size[0].get_float(0), size[0].get_float(1))


def _pin_state(sheet: SExp, geo: Geometry) -> list[dict[str, Any]]:
    """Every pin of the sheet symbol from its raw ``(at x y angle)``: side by angle, offset along the edge."""
    out = []
    for node in _kids(sheet, "pin"):
        x, y, angle = se._at(node)
        side = _SIDE_BY_ANGLE.get(int(round(angle)) % 360)
        offset = None
        if side is not None:
            origin, _length = geo.origin_length(side)
            offset = (y - origin) if side in ("left", "right") else (x - origin)
        out.append({"node": node, "name": _text(node), "direction": _text(node, 1), "side": side, "offset": offset,
                    "x": x, "y": y, "uuid": se._uuid(node)})
    return out


def _pos(geo: Geometry, side: str, offset: float) -> list[float]:
    plan = {"x": geo.x, "y": geo.y, "width": geo.w, "height": geo.h}
    return list(sheet_authoring._pin_position(plan, {"side": side, "offset": offset}))


def _place(geo: Geometry, taken: list[dict], side: str, offset: Any, where: str) -> tuple[float, dict[str, Any]]:
    """The offset (mm along the edge, snapped so the pin sits on the 1.27 mm grid of the page) for a
    pin on ``side``. ``offset`` None picks the next free 2.54 mm step. Raises when it falls off the
    edge or onto another pin."""
    origin, length = geo.origin_length(side)
    here = [p["offset"] for p in taken if p["side"] == side and p["offset"] is not None]
    info: dict[str, Any] = {}
    if offset is None:
        k = round((origin + 2 * GRID) / GRID)
        if (k * GRID) < origin + 2 * GRID - 1e-6:
            k += 1
        while k * GRID - origin < length - 1e-6:
            candidate = round(k * GRID - origin, 4)
            if all(abs(candidate - o) >= 2 * GRID - 1e-6 for o in here):
                return candidate, info
            k += 2
        raise SheetAuthoringError("no_room_on_edge", f"{where}: the {side} edge ({length} mm) has no free 2.54 mm step left; give an offset or another side")
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        raise SheetAuthoringError("invalid_spec", f"{where}.offset must be a number, got {offset!r}")
    snapped_abs = round((origin + offset) / GRID) * GRID
    value = round(snapped_abs - origin, 4)
    if abs(value - offset) > 1e-6:
        info = {"asked_offset": offset, "snapped": True}
    if not 0 < value < length:
        raise SheetAuthoringError(
            "pin_off_edge", f"{where}: offset {offset} (snapped {value}) is not strictly inside the {side} edge (0 to {length} mm)")
    clash = [p for p in taken if p["side"] == side and p["offset"] is not None and abs(p["offset"] - value) < 1e-3]
    if clash:
        raise SheetAuthoringError("pin_position_taken", f"{where}: pin {clash[0]['name']!r} already sits at offset {value} on the {side} edge")
    return value, info


def _justify(node: SExp, side: str) -> None:
    effects = node.find_children("effects")
    if not effects:
        node.append(sheet_authoring._effects(sheet_authoring._JUSTIFY[side]))
        return
    eff = effects[0]
    eff.children[:] = [c for c in eff.children if c.is_atom or c.name != "justify"]
    eff.append(SExp.list("justify", write_layer.symbol(sheet_authoring._JUSTIFY[side])))


def _attached(sheet: se.Sheet, point: tuple[float, float], skip: Any = None) -> list[dict[str, Any]]:
    """What sits exactly on ``point``: a wire end, a label, junction or marker, a symbol pin, another sheet pin.
    (A wire that only runs over the point joins nothing, measured in issue 86.)"""
    found: list[dict[str, Any]] = []
    for wire in sheet.wires:
        ends = se._wire_ends(wire)
        if ends and (se._same(ends[0], point) or se._same(ends[-1], point)):
            found.append({"kind": "wire", "uuid": se._uuid(wire)})
    for node in (*sheet.labels, *sheet.junctions, *sheet.no_connects):
        x, y, _ = se._at(node)
        if se._same((x, y), point):
            entry = {"kind": node.name, "uuid": se._uuid(node)}
            if node.name in se.LABEL_TAGS:
                entry["text"] = _text(node)
            found.append(entry)
    for sym in sheet.symbols:
        for pin in se.pin_positions(sheet.root, sym):
            if se._same((pin["x"], pin["y"]), point):
                found.append({"kind": "symbol_pin", "reference": se._ref(sym), "pin": pin["number"]})
    for sh in sheet.sheets:
        for pin in _kids(sh, "pin"):
            if pin is not skip and se._same(se._at(pin)[:2], point):
                found.append({"kind": "sheet_pin", "name": _text(pin), "uuid": se._uuid(pin)})
    return found


def _insert_pin(sheet: SExp, pin: SExp) -> None:
    index = next((i for i, c in enumerate(sheet.children) if not c.is_atom and c.name == "instances"), len(sheet.children))
    sheet.children.insert(index, pin)


def _insert_label(child: SExp, label: SExp) -> None:
    index = next((i for i, c in enumerate(child.children) if not c.is_atom and c.name in _TAIL), len(child.children))
    child.children.insert(index, label)


def _labels_named(child: SExp, name: str) -> list[SExp]:
    return [c for c in _kids(child, "hierarchical_label") if _text(c) == name]


def _shape(label: SExp) -> str:
    shape = label.find_children("shape")
    return _text(shape[0]) if shape else ""


# --------------------------------------------------------------------------- the run


@dataclass
class Plan:
    parent: SExp
    children: dict[Path, SExp] = field(default_factory=dict)
    touched: dict[str, SExp] = field(default_factory=dict)           # sheet uuid -> its original (copied) node
    sheet_files: dict[str, Path] = field(default_factory=dict)       # sheet uuid -> resolved child file
    report: list[dict[str, Any]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)      # connectivity changes seen by geometry
    added_pins: dict[str, set[str]] = field(default_factory=dict)    # sheet uuid -> new pin uuids
    removed_pins: dict[str, set[str]] = field(default_factory=dict)
    moved_pins: dict[str, set[str]] = field(default_factory=dict)
    added_labels: dict[Path, set[str]] = field(default_factory=dict)
    removed_labels: dict[Path, set[str]] = field(default_factory=dict)
    explained: set[str] = field(default_factory=set)                 # interface mismatches the call explains
    allowed_erc: Counter = field(default_factory=Counter)


def _child_file(tree: sheet_tree.SheetTree, parent_dir: Path, sheet: SExp, where: str) -> tuple[Path, str]:
    rel = sheet_tree.sheet_file_property(sheet)[0]
    path = (parent_dir / (rel or "")).resolve()
    if not rel or path not in {f.resolve() for f in tree.files}:
        raise SheetAuthoringError("sheet_file_missing", f"{where}: the sheet file {rel!r} is not part of the project's sheet tree", **tree.report())
    return path, rel


def _shared(tree: sheet_tree.SheetTree, child: Path, where: str, rel: str) -> None:
    if sum(1 for file, _p in tree.uses if file == child) > 1:
        raise SheetAuthoringError(
            "sheet_file_shared",
            f"{where}: {rel} is used by more than one sheet symbol, so a hierarchical label added or removed in it would "
            "disagree with the other sheet symbols; change all uses in one project by hand in KiCad. Nothing was written")


class _Session:
    """The trees of one call: the parent copy, the child copies by file, the plan being filled."""

    def __init__(self, tree: sheet_tree.SheetTree, parent_key: Path, parent_new: SExp) -> None:
        self.tree = tree
        self.parent_key = parent_key
        self.plan = Plan(parent=parent_new)
        self.parent_sheet = se.Sheet(parent_new)
        self.seen: set[str] = set()

    def sheet(self, spec: dict, where: str) -> tuple[SExp, Path, str, Geometry]:
        node = _find_sheet(self.plan.parent, spec, where)
        uid = se._uuid(node)
        if uid in self.seen:
            raise SheetAuthoringError("invalid_spec", f"{where}: sheet symbol {sheet_tree.sheet_name_property(node)!r} is named twice in this call")
        self.seen.add(uid)
        file, rel = _child_file(self.tree, self.parent_key.parent, node, where)
        self.plan.touched[uid] = copy.deepcopy(node)
        self.plan.sheet_files[uid] = file
        return node, file, rel, _geometry(node)

    def child(self, file: Path) -> SExp:
        if file not in self.plan.children:
            self.plan.children[file] = write_layer.load(file)
        return self.plan.children[file]


def _plan_add(session: _Session, specs: list[dict]) -> None:
    plan = session.plan
    for i, spec in enumerate(specs):
        where = f"specs[{i}]"
        _only_keys(spec, {"sheet", "uuid", "pins"}, where)
        pins = spec.get("pins")
        if not isinstance(pins, list) or not pins:
            raise SheetAuthoringError("invalid_spec", f"{where}.pins must be a non-empty list of {{name, direction, side?, offset?}}")
        node, file, rel, geo = session.sheet(spec, where)
        sheet_name = sheet_tree.sheet_name_property(node)
        uid = se._uuid(node)
        state = _pin_state(node, geo)
        taken = list(state)
        names = {p["name"] for p in state}
        child = None
        ys = None
        entries = []
        for j, pin in enumerate(pins):
            here = f"{where}.pins[{j}]"
            if not isinstance(pin, dict):
                raise SheetAuthoringError("invalid_spec", f"{here} must be {{name, direction, side?, offset?}}, got {pin!r}")
            _only_keys(pin, {"name", "direction", "side", "offset"}, here)
            name = sheet_authoring._name(pin.get("name"), f"{here}.name")
            direction = pin.get("direction")
            if direction not in PIN_DIRECTIONS:
                raise SheetAuthoringError("invalid_spec", f"{here}.direction must be one of {list(PIN_DIRECTIONS)}, got {direction!r}")
            if name in names:
                raise SheetAuthoringError("duplicate_pin_name", f"{here}: sheet symbol {sheet_name!r} already has a pin named {name!r}; nothing was written")
            side = pin.get("side") or DEFAULT_SIDE[direction]
            if side not in SIDES:
                raise SheetAuthoringError("invalid_spec", f"{here}.side must be one of {list(SIDES)}, got {side!r}")
            offset, info = _place(geo, taken, side, pin.get("offset"), here)
            # the child label: reuse one of the same name and direction, refuse another direction
            if child is None:
                child = session.child(file)
                heights = [c.find_children("at")[0].get_float(1) or 0.0 for c in _kids(child, "hierarchical_label") if c.find_children("at")]
                ys = max(heights) + sheet_authoring.LABEL_STEP if heights else sheet_authoring.LABEL_Y
            have = _labels_named(child, name)
            wrong = [_shape(c) for c in have if _shape(c) != direction]
            if wrong:
                raise SheetAuthoringError(
                    "label_direction_mismatch",
                    f"{here}: {rel} already has hierarchical label {name!r} with direction {wrong[0]!r}, not {direction!r}; nothing was written")
            if have:
                label_info = {"state": "reused", "at": [round(v, 4) for v in se._at(have[0])[:2]]}
            else:
                _shared(session.tree, file, here, rel)  # only a label that would change makes a twice-used child a problem
                label = sheet_authoring.label_node(name, direction, side, sheet_authoring.LABEL_X, round(ys, 4))
                _insert_label(child, label)
                plan.added_labels.setdefault(file, set()).add(se._uuid(label))
                label_info = {"state": "created", "at": [sheet_authoring.LABEL_X, round(ys, 4)]}
                ys += sheet_authoring.LABEL_STEP
                plan.allowed_erc["label_dangling"] += 1
            px, py = _pos(geo, side, offset)
            node_pin = SExp.list(
                "pin", write_layer.string(name), write_layer.symbol(direction), sheet_authoring._at(px, py, sheet_authoring._PIN_ANGLE[side]),
                sheet_authoring._uuid(), sheet_authoring._effects(sheet_authoring._JUSTIFY[side]))
            _insert_pin(node, node_pin)
            plan.added_pins.setdefault(uid, set()).add(se._uuid(node_pin))
            plan.allowed_erc["pin_not_connected"] += 1
            taken.append({"name": name, "side": side, "offset": offset})
            names.add(name)
            landed = _attached(session.parent_sheet, (px, py), skip=node_pin)
            if landed:
                plan.changes.append({"type": "pin_lands_on", "sheet": sheet_name, "pin": name, "at": [px, py], "attached": landed})
            entries.append({"name": name, "direction": direction, "side": side, "offset": offset, "at": [px, py],
                            "child_label": label_info, **info})
        plan.report.append({"sheet": sheet_name, "uuid": uid, "file": rel, "pins": entries})


def _plan_remove(session: _Session, specs: list[dict], remove_child_labels: bool) -> None:
    plan = session.plan
    if not isinstance(remove_child_labels, bool):
        raise SheetAuthoringError("invalid_spec", "remove_child_labels must be true or false")
    for i, spec in enumerate(specs):
        where = f"specs[{i}]"
        _only_keys(spec, {"sheet", "uuid", "pins", "pattern"}, where)
        node, file, rel, geo = session.sheet(spec, where)
        sheet_name = sheet_tree.sheet_name_property(node)
        uid = se._uuid(node)
        names, pattern = spec.get("pins"), spec.get("pattern")
        if names is None and pattern is None:
            raise SheetAuthoringError("invalid_spec", f"{where} needs 'pins' (exact pin names) or 'pattern' (a shell pattern on the names)")
        if names is not None and (not isinstance(names, list) or not names or not all(isinstance(n, str) for n in names)):
            raise SheetAuthoringError("invalid_spec", f"{where}.pins must be a non-empty list of pin names")
        if pattern is not None and (not isinstance(pattern, str) or not pattern):
            raise SheetAuthoringError("invalid_spec", f"{where}.pattern must be a non-empty string")
        state = _pin_state(node, geo)
        have = {p["name"] for p in state}
        missing = [n for n in (names or []) if n not in have]
        if missing:
            raise SheetAuthoringError("pin_not_found", f"{where}: sheet symbol {sheet_name!r} has no pin named {missing}; nothing was written",
                                      pins=sorted(have))
        picked = [p for p in state if p["name"] in (names or []) or (pattern is not None and fnmatch.fnmatchcase(p["name"], pattern))]
        if not picked:
            raise SheetAuthoringError("pin_not_found", f"{where}: the pattern {pattern!r} matches no pin of {sheet_name!r}; nothing was written", pins=sorted(have))
        child = None
        if remove_child_labels:
            child = session.child(file)
        entries = []
        doomed = {id(p["node"]) for p in picked}
        for p in picked:
            attached = _attached(session.parent_sheet, (p["x"], p["y"]), skip=p["node"])
            if attached:
                plan.changes.append({"type": "sheet_pin_detached", "sheet": sheet_name, "pin": p["name"], "at": [se._r(p["x"]), se._r(p["y"])],
                                     "attached": attached})
            label_state = "left"
            if remove_child_labels:
                labels = _labels_named(child, p["name"])
                if labels:
                    _shared(session.tree, file, where, rel)
                if not labels:
                    label_state = "absent"
                for label in labels:
                    lx, ly, _ = se._at(label)
                    cattached = _attached(se.Sheet(child), (lx, ly), skip=label)
                    cattached = [a for a in cattached if a.get("uuid") != se._uuid(label)]
                    if cattached:
                        plan.changes.append({"type": "child_label_detached", "sheet": sheet_name, "label": p["name"], "at": [se._r(lx), se._r(ly)],
                                             "attached": cattached})
                    child.children[:] = [c for c in child.children if c is not label]
                    plan.removed_labels.setdefault(file, set()).add(se._uuid(label))
                    label_state = "removed"
            else:
                plan.explained.add(f"{rel}: hierarchical label {p['name']!r} has no pin")
                plan.allowed_erc["hier_label_mismatch"] += 2
                plan.allowed_erc["label_dangling"] += 1
            plan.removed_pins.setdefault(uid, set()).add(p["uuid"])
            entries.append({"name": p["name"], "direction": p["direction"], "side": p["side"], "offset": None if p["offset"] is None else se._r(p["offset"]),
                            "at": [se._r(p["x"]), se._r(p["y"])], "attached": attached, "child_label": label_state})
        node.children[:] = [c for c in node.children if id(c) not in doomed]
        plan.report.append({"sheet": sheet_name, "uuid": uid, "file": rel, "pins": entries, "pins_left": len(state) - len(picked)})


def _plan_move(session: _Session, specs: list[dict]) -> None:
    plan = session.plan
    for i, spec in enumerate(specs):
        where = f"specs[{i}]"
        _only_keys(spec, {"sheet", "uuid", "pins"}, where)
        pins = spec.get("pins")
        if not isinstance(pins, list) or not pins:
            raise SheetAuthoringError("invalid_spec", f"{where}.pins must be a non-empty list of {{name, side?, offset?}}")
        node, file, rel, geo = session.sheet(spec, where)
        sheet_name = sheet_tree.sheet_name_property(node)
        uid = se._uuid(node)
        state = {p["name"]: p for p in _pin_state(node, geo)}
        asked = []
        for j, pin in enumerate(pins):
            here = f"{where}.pins[{j}]"
            if not isinstance(pin, dict):
                raise SheetAuthoringError("invalid_spec", f"{here} must be {{name, side?, offset?}}, got {pin!r}")
            _only_keys(pin, {"name", "side", "offset"}, here)
            name = pin.get("name")
            if name not in state:
                raise SheetAuthoringError("pin_not_found", f"{here}: sheet symbol {sheet_name!r} has no pin named {name!r}; nothing was written", pins=sorted(state))
            if pin.get("side") is None and pin.get("offset") is None:
                raise SheetAuthoringError("invalid_spec", f"{here} needs 'side' or 'offset' (or both)")
            if pin.get("side") is not None and pin["side"] not in SIDES:
                raise SheetAuthoringError("invalid_spec", f"{here}.side must be one of {list(SIDES)}, got {pin['side']!r}")
            if any(a[0] == name for a in asked):
                raise SheetAuthoringError("invalid_spec", f"{here}: pin {name!r} is moved twice in this call")
            asked.append((name, pin, here))
        moving = {a[0] for a in asked}
        taken = [p for n, p in state.items() if n not in moving]
        entries = []
        for name, pin, here in asked:
            old = state[name]
            side = pin.get("side") or old["side"]
            if side is None:
                raise SheetAuthoringError("invalid_spec", f"{here}: pin {name!r} does not sit on an edge of its sheet symbol; give 'side' and 'offset'")
            offset_arg = pin.get("offset")
            if offset_arg is None and side == old["side"]:
                raise SheetAuthoringError("invalid_spec", f"{here}: pin {name!r} is already on the {side} edge; give 'offset' or another 'side'")
            offset, info = _place(geo, taken, side, offset_arg, here)
            px, py = _pos(geo, side, offset)
            target = old["node"]
            at = target.find_children("at")[0]
            write_layer.set_number(at, 0, px)
            write_layer.set_number(at, 1, py)
            write_layer.set_number(at, 2, sheet_authoring._PIN_ANGLE[side])
            _justify(target, side)
            taken.append({"name": name, "side": side, "offset": offset})
            attached_old = _attached(session.parent_sheet, (old["x"], old["y"]), skip=target)
            if attached_old:
                plan.changes.append({"type": "sheet_pin_detached", "sheet": sheet_name, "pin": name, "at": [se._r(old["x"]), se._r(old["y"])],
                                     "attached": attached_old})
            landed = _attached(session.parent_sheet, (px, py), skip=target)
            if landed:
                plan.changes.append({"type": "pin_lands_on", "sheet": sheet_name, "pin": name, "at": [px, py], "attached": landed})
            plan.moved_pins.setdefault(uid, set()).add(old["uuid"])
            entries.append({"name": name, "direction": old["direction"], "from": {"side": old["side"], "at": [se._r(old["x"]), se._r(old["y"])]},
                            "side": side, "offset": offset, "at": [px, py], "attached_before": attached_old, **info})
        plan.report.append({"sheet": sheet_name, "uuid": uid, "file": rel, "pins": entries})


# --------------------------------------------------------------------------- checks


def _counter(root: SExp) -> Counter:
    return Counter(write_layer.serialise(c, compact=True) for c in root.children)


def _strip_pins(sheet: SExp) -> str:
    stripped = copy.deepcopy(sheet)
    stripped.children[:] = [c for c in stripped.children if c.is_atom or c.name != "pin"]
    return write_layer.serialise(stripped, compact=True)


def _staged_check(plan: Plan, original_parent: SExp, originals: dict[Path, SExp]) -> None:
    """Everything the call did not mean to change is unchanged. Raises ``write_not_verified`` (nothing written yet)."""
    problems: list[str] = []
    new_sheets = {se._uuid(s): s for s in _kids(plan.parent, "sheet")}
    swap_old, swap_new = Counter(), Counter()
    for uid, before in plan.touched.items():
        after = new_sheets.get(uid)
        if after is None:
            problems.append(f"sheet symbol {uid} is gone")
            continue
        if _strip_pins(before) != _strip_pins(after):
            problems.append(f"sheet symbol {uid} changed beyond its pins")
        old_pins = {se._uuid(p): write_layer.serialise(p, compact=True) for p in _kids(before, "pin")}
        new_pins = {se._uuid(p): write_layer.serialise(p, compact=True) for p in _kids(after, "pin")}
        added, removed, moved = plan.added_pins.get(uid, set()), plan.removed_pins.get(uid, set()), plan.moved_pins.get(uid, set())
        if set(new_pins) - set(old_pins) != added:
            problems.append(f"sheet symbol {uid}: new pins {sorted(set(new_pins) - set(old_pins))} differ from the planned {sorted(added)}")
        if set(old_pins) - set(new_pins) != removed:
            problems.append(f"sheet symbol {uid}: removed pins {sorted(set(old_pins) - set(new_pins))} differ from the planned {sorted(removed)}")
        for pid, text in old_pins.items():
            if pid in new_pins and pid not in moved and new_pins[pid] != text:
                problems.append(f"pin {pid} changed but was not selected")
        for pid in moved:
            if pid in new_pins and (_pin_identity(old_pins[pid]) != _pin_identity(new_pins[pid])):
                problems.append(f"pin {pid} changed beyond its position")
        swap_old[write_layer.serialise(before, compact=True)] += 1
        swap_new[write_layer.serialise(after, compact=True)] += 1
    cur_old = _counter(original_parent)
    cur_new = _counter(plan.parent)
    if (cur_old - swap_old) != (cur_new - swap_new):
        problems.append("a top-level node of the parent sheet other than the selected sheet symbols changed")
    for file, child in plan.children.items():
        old, new = _counter(originals[file]), _counter(child)
        gone, fresh = old - new, new - old
        want_gone = plan.removed_labels.get(file, set())
        want_new = plan.added_labels.get(file, set())
        gone_ids = {_uuid_of_text(t) for t in gone.elements()}
        fresh_ids = {_uuid_of_text(t) for t in fresh.elements()}
        if gone_ids != want_gone or fresh_ids != want_new:
            problems.append(f"{file.name}: the child sheet differs from the original beyond the planned hierarchical labels")
        if any(not _is_label(t) for t in [*gone.elements(), *fresh.elements()]):
            problems.append(f"{file.name}: something other than a hierarchical label changed in the child sheet")
    if problems:
        raise SheetAuthoringError("write_not_verified", "the edited sheets differ from the originals beyond the selection: "
                                  + "; ".join(problems[:10]) + "; nothing was written", problems=problems[:MAX_LISTED])


def _pin_identity(text: str) -> str:
    """A serialised pin without its position and justification: name, direction, uuid."""
    return text.split("(at ")[0]


def _uuid_of_text(text: str) -> str:
    marker = "(uuid "
    start = text.rfind(marker)
    if start < 0:
        return ""
    return text[start + len(marker):].split(")")[0].strip().strip('"')


def _is_label(text: str) -> bool:
    return text.startswith("(hierarchical_label ")


def _staged_verify(plan: Plan, tree: sheet_tree.SheetTree, parent_key: Path) -> dict[str, Any]:
    """Save the changed files into a scratch copy and measure it against a scratch copy of the original."""
    base = tree.project_dir
    before_dir = Path(tempfile.mkdtemp(prefix="boardproof-sheet-pins-before-"))
    staged_dir = Path(tempfile.mkdtemp(prefix="boardproof-sheet-pins-staged-"))
    try:
        before_root = sheet_authoring._copy_project(tree, [], before_dir)
        staged_root = sheet_authoring._copy_project(tree, [], staged_dir)
        for file, child in plan.children.items():
            sheet_authoring._save(child, staged_dir / file.relative_to(base))
        sheet_authoring._save(plan.parent, staged_dir / parent_key.relative_to(base))
        staged_tree = sheet_tree.walk(staged_root)
        if staged_tree.problems or staged_tree.unreferenced:
            raise SheetAuthoringError("write_not_verified", "the sheet tree is not whole after the change: "
                                      + "; ".join(staged_tree.problems or [f"{r} is unreferenced" for r in staged_tree.unreferenced]), **staged_tree.report())
        parent_rel = parent_key.relative_to(base)
        before_mismatch = _mismatches(before_dir, parent_rel)
        after_mismatch = _mismatches(staged_dir, parent_rel)
        fresh = [m for m in after_mismatch if m not in before_mismatch]
        unexplained = [m for m in fresh if m not in plan.explained]
        if unexplained:
            raise SheetAuthoringError("write_not_verified", "sheet pins and hierarchical labels disagree after the change: "
                                      + "; ".join(unexplained) + "; nothing was written", interface_problems=unexplained)
        before_erc = sheet_authoring.erc_census(before_root)
        after_erc = sheet_authoring.erc_census(staged_root)
        new = {f"{t}/{s}": n - before_erc[(t, s)] for (t, s), n in after_erc.items() if n > before_erc[(t, s)]}
        nets_before = se.nets_from(before_root)
        nets_after = se.nets_from(staged_root)
        return {"erc_new_violations": new, "erc_baseline_types": {t for (t, _s) in before_erc}, "nets_before": nets_before, "nets_after": nets_after}
    finally:
        shutil.rmtree(before_dir, ignore_errors=True)
        shutil.rmtree(staged_dir, ignore_errors=True)


def _mismatches(base: Path, parent_rel: Path) -> list[str]:
    """Where the sheet symbols of the parent (at ``parent_rel`` under ``base``) and the hierarchical labels of their files disagree."""
    parent_path = base / parent_rel
    parent = write_layer.load(parent_path)
    out: list[str] = []
    for sheet in _kids(parent, "sheet"):
        rel = sheet_tree.sheet_file_property(sheet)[0]
        child_path = parent_path.parent / (rel or "")
        if rel and child_path.is_file():
            out += sheet_authoring.interface_mismatches(parent, write_layer.load(child_path), rel)
    return out


# --------------------------------------------------------------------------- compare and sync (issue 110)


def _selection(value: Any) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list) or not value or not all(isinstance(v, str) and v for v in value):
        raise SheetAuthoringError("invalid_spec", "sheets must be omitted or a non-empty list of sheet names (or uuids)")
    return value


def _chosen(parent: SExp, selection: list[str] | None) -> list[SExp]:
    """The sheet symbols the selection names, in the order of the file (all of them without a selection)."""
    if selection is None:
        return _kids(parent, "sheet")
    picked = [_find_sheet(parent, {"sheet": wanted}, f"sheets[{i}]") for i, wanted in enumerate(selection)]
    ids = {se._uuid(s) for s in picked}
    return [s for s in _kids(parent, "sheet") if se._uuid(s) in ids]


def _compare_one(tree: sheet_tree.SheetTree, parent_dir: Path, sheet: SExp) -> dict[str, Any]:
    """Pins of ``sheet`` against the hierarchical labels of its child file, from the raw ``(at ...)`` and ``(shape ...)``."""
    where = f"sheet {sheet_tree.sheet_name_property(sheet)!r}"
    file, rel = _child_file(tree, parent_dir, sheet, where)
    child = write_layer.load(file)
    pins = [(p, _text(p), _text(p, 1)) for p in _kids(sheet, "pin")]
    labels: dict[str, list[SExp]] = {}
    for label in _kids(child, "hierarchical_label"):
        labels.setdefault(_text(label), []).append(label)
    pin_names = {name for _p, name, _d in pins}
    entry: dict[str, Any] = {
        "sheet": sheet_tree.sheet_name_property(sheet), "uuid": se._uuid(sheet), "file": rel,
        "missing_pins": [], "missing_labels": [], "direction_mismatches": [], "duplicate_labels": [], "pins_in_sync": [],
    }
    for name, nodes in labels.items():
        directions = [_shape(n) for n in nodes]
        if len(nodes) > 1:
            entry["duplicate_labels"].append({"name": name, "directions": directions, "count": len(nodes)})
        if name not in pin_names:
            x, y, _a = se._at(nodes[0])
            entry["missing_pins"].append({"name": name, "direction": directions[0], "label_at": [round(x, 4), round(y, 4)]})
    for node, name, direction in pins:
        x, y, _a = se._at(node)
        have = labels.get(name)
        if not have:
            entry["missing_labels"].append({"name": name, "pin_direction": direction, "pin_at": [round(x, 4), round(y, 4)]})
        elif _shape(have[0]) != direction:
            entry["direction_mismatches"].append({"name": name, "pin_direction": direction, "label_direction": _shape(have[0])})
        else:
            entry["pins_in_sync"].append(name)
    entry["differences"] = sum(len(entry[k]) for k in ("missing_pins", "missing_labels", "direction_mismatches", "duplicate_labels"))
    entry["in_sync"] = entry["differences"] == 0
    return entry


def _compare(tree: sheet_tree.SheetTree, parent_key: Path, selection: list[str] | None) -> list[dict[str, Any]]:
    parent = write_layer.load(parent_key)
    if parent.name != "kicad_sch":
        raise SheetAuthoringError("invalid_spec", f"{parent_key.name} is not a schematic sheet")
    return [_compare_one(tree, parent_key.parent, s) for s in _chosen(parent, selection)]


def _tree_of(parent_sch: str, root_sch: str | None) -> tuple[Path, sheet_tree.SheetTree]:
    parent = Path(parent_sch)
    if not parent.is_file():
        raise SheetAuthoringError("file_not_found", f"parent sheet not found: {parent}")
    root = sheet_authoring.find_root(parent, root_sch)
    tree = sheet_authoring._require_tree(root)
    parent_key = parent.resolve()
    if parent_key not in {f.resolve() for f in tree.files}:
        raise SheetAuthoringError("parent_not_in_tree", f"{parent.name} is not reached from the root sheet {root.name}", **tree.report())
    return parent_key, tree


def _unresolved(sheets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {"missing_labels": [], "direction_mismatches": [], "duplicate_labels": [], "unsupported_directions": []}
    for entry in sheets:
        for key in ("missing_labels", "direction_mismatches", "duplicate_labels"):
            out[key] += [{"sheet": entry["sheet"], **item} for item in entry[key]]
        out["unsupported_directions"] += [{"sheet": entry["sheet"], "name": m["name"], "label_direction": m["direction"]}
                                          for m in entry["missing_pins"] if m["direction"] not in PIN_DIRECTIONS]
    return out


def compare_sheet_pins(parent_sch: str, sheets: list[str] | None = None, root_sch: str | None = None) -> dict[str, Any]:
    """Read-only: the pins of the sheet symbols of ``parent_sch`` against the hierarchical labels of their child sheets."""
    try:
        selection = _selection(sheets)
        parent_key, tree = _tree_of(parent_sch, root_sch)
        entries = _compare(tree, parent_key, selection)
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)
    return {
        "status": "ok", "ok": True, "written": False, "sheets": entries, "count": len(entries),
        "differences": sum(e["differences"] for e in entries), "sheets_in_sync": sum(1 for e in entries if e["in_sync"]),
    }


def sync_sheet_pins(parent_sch: str, sheets: list[str] | None = None, root_sch: str | None = None, *, allow_connectivity_change: bool = False,
                    dry_run: bool = False) -> dict[str, Any]:
    return _pipeline("sync", parent_sch, sheets, root_sch, allow=allow_connectivity_change, dry_run=dry_run)


# --------------------------------------------------------------------------- the entry points


def _pipeline(op: str, parent_sch: str, specs: Any, root_sch: str | None, *, allow: Any, dry_run: Any, remove_child_labels: bool = False) -> dict[str, Any]:
    try:
        return _run(op, Path(parent_sch), specs, root_sch, allow, dry_run, remove_child_labels)
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)
    except write_layer.WriteLayerError as exc:
        if (locked := write_layer.as_lock_refusal(exc)) is not None:
            return locked
        return {"status": "incomplete", "ok": False, "code": exc.code, "reason": str(exc), **exc.details}


def _run(op: str, parent: Path, specs: Any, root_sch: str | None, allow: Any, dry_run: Any, remove_child_labels: bool) -> dict[str, Any]:
    if not isinstance(allow, bool) or not isinstance(dry_run, bool):
        raise SheetAuthoringError("invalid_spec", "allow_connectivity_change and dry_run must be true or false")
    specs = _selection(specs) if op == "sync" else _specs(specs, "specs")
    if not parent.is_file():
        raise SheetAuthoringError("file_not_found", f"parent sheet not found: {parent}")
    if not dry_run:
        write_layer.check_writable(parent)
    root = sheet_authoring.find_root(parent, root_sch)
    tree = sheet_authoring._require_tree(root)
    parent_key = parent.resolve()
    if parent_key not in {f.resolve() for f in tree.files}:
        raise SheetAuthoringError("parent_not_in_tree", f"{parent.name} is not reached from the root sheet {root.name}", **tree.report())
    # the parent and the child files of its sheet symbols are read and (maybe) written below: one lock over all of them, so a
    # concurrent add_labels on a child or a second call on the same parent cannot be overwritten (issue 87's lock; the tool
    # wrapper takes the parent already). Hierarchy order only: a call never waits for a file above its own parent.
    with write_layer.path_lock([parent_key, *_child_files(tree, parent_key)]):
        return _locked(op, parent, parent_key, tree, specs, allow, dry_run, remove_child_labels)


def _child_files(tree: sheet_tree.SheetTree, parent_key: Path) -> list[Path]:
    try:
        node = write_layer.load(parent_key)
    except Exception:  # noqa: BLE001 - the locked part reports an unreadable parent
        return []
    files: list[Path] = []
    for sheet in _kids(node, "sheet"):
        try:
            files.append(_child_file(tree, parent_key.parent, sheet, "sheet")[0])
        except SheetAuthoringError:
            continue
    return files


def _locked(op: str, parent: Path, parent_key: Path, tree: sheet_tree.SheetTree, specs: Any, allow: bool, dry_run: bool,
            remove_child_labels: bool) -> dict[str, Any]:
    original_parent = write_layer.load(parent_key)
    if original_parent.name != "kicad_sch":
        raise SheetAuthoringError("invalid_spec", f"{parent.name} is not a schematic sheet")
    unresolved = None
    if op == "sync":
        # read under the lock: the labels the pins are made from cannot change before the write
        found = _compare(tree, parent_key, specs)
        unresolved = _unresolved(found)
        def makeable(entry: dict[str, Any]) -> list[dict[str, Any]]:
            # a label name that stands twice with two directions has no one direction to give the pin: unresolved
            clash = {d["name"] for d in entry["duplicate_labels"] if len(set(d["directions"])) > 1}
            return [m for m in entry["missing_pins"] if m["direction"] in PIN_DIRECTIONS and m["name"] not in clash]

        specs = [{"uuid": e["uuid"], "pins": [{"name": m["name"], "direction": m["direction"]} for m in makeable(e)]}
                 for e in found if makeable(e)]
        if not specs:
            skipped = any(len(makeable(e)) < len(e["missing_pins"]) for e in found)
            return {"status": "ok", "ok": True, "written": False, "dry_run": dry_run, "added": [], "count": 0, "unresolved": unresolved,
                    "differences_left": sum(len(v) for v in unresolved.values()), "child_files_written": [],
                    "note": ("no pin can be created from the labels of the selected sheets; the labels that were skipped are listed under unresolved"
                             if skipped else "every hierarchical label of the selected sheets already has a pin; nothing to create")}
        op = "add"
    session = _Session(tree, parent_key, write_layer.load(parent_key))
    if op == "add":
        _plan_add(session, specs)
    elif op == "remove":
        _plan_remove(session, specs, remove_child_labels)
    else:
        _plan_move(session, specs)
    plan = session.plan
    originals = {file: write_layer.load(file) for file in plan.children}
    if not dry_run:
        write_layer.check_writable(parent_key, *plan.children)
    _staged_check(plan, original_parent, originals)

    proof = _staged_verify(plan, tree, parent_key)
    changes = list(plan.changes)
    changes += se.compare_nets(proof["nets_before"], proof["nets_after"])
    connectivity = {"nets_before": len(proof["nets_before"]), "nets_after": len(proof["nets_after"]), "changed": bool(changes),
                    "changes": se._bounded(changes)}
    # A wire end, label or pin left dangling by a change that moves connectivity is what the connectivity gate reports; the
    # dangling kinds are tolerated then. Anything else new, or any new violation when connectivity does not change, is refused.
    tolerated = _DANGLING_ERC if changes else frozenset()
    excess = {k: n for k, n in proof["erc_new_violations"].items()
              if k.split("/")[0] not in tolerated and n > plan.allowed_erc.get(k.split("/")[0], 0)}
    if excess and not (changes and not allow):
        raise SheetAuthoringError(
            "write_not_verified",
            f"kicad-cli ERC of the result shows more violations than the project had before plus what this change explains: "
            f"in excess {excess} (new in total {proof['erc_new_violations']}; explained at most {dict(plan.allowed_erc)}); nothing was written",
            erc_new_violations=proof["erc_new_violations"])
    key = {"add": "added", "remove": "removed", "move": "moved"}[op]
    result: dict[str, Any] = {
        "status": "ok", "ok": True, "written": False, "dry_run": dry_run, key: plan.report,
        "count": sum(len(r["pins"]) for r in plan.report), "connectivity": connectivity,
        "erc_new_violations": proof["erc_new_violations"],
        "child_files_written": sorted(tree.rel(f) for f in plan.children if plan.added_labels.get(f) or plan.removed_labels.get(f)),
    }
    if unresolved is not None:
        result["unresolved"] = unresolved
        result["differences_left"] = sum(len(v) for v in unresolved.values())
    if op == "remove":
        result["child_labels"] = ("removed with the pins" if remove_child_labels else
                                  "left in the child sheets; an unmatched hierarchical label is an ERC error (hier_label_mismatch) until it is removed or its pin is added again")
    if changes and not allow:
        raise SheetAuthoringError(
            "connectivity_changed",
            f"the change would alter connectivity ({len(changes)} change(s), first: {changes[0]}); nothing was written. "
            "Check the list, then repeat with allow_connectivity_change true if it is meant.", connectivity=connectivity, **{key: plan.report})
    if dry_run:
        return result
    written: list[Path] = []
    try:
        for file, child in plan.children.items():
            if plan.added_labels.get(file) or plan.removed_labels.get(file):
                write_layer.save(child, file, verify=se._expected_verify(child))
                written.append(file)
        write_layer.save(plan.parent, parent_key, verify=se._expected_verify(plan.parent))
    except BaseException:
        for file in written:  # the parent did not change, so no child may keep a change nothing answers to
            try:
                write_layer.save(originals[file], file)
            except Exception:  # noqa: BLE001 - the original failure is the one to report
                pass
        raise
    if write_layer.serialise(write_layer.load(parent_key), compact=True) != write_layer.serialise(plan.parent, compact=True):
        raise SheetAuthoringError("write_not_verified", f"{parent.name} does not read back as the edited tree")
    result["written"] = True
    return result


def add_sheet_pins(parent_sch: str, specs: list[dict], root_sch: str | None = None, *, allow_connectivity_change: bool = False,
                   dry_run: bool = False) -> dict[str, Any]:
    return _pipeline("add", parent_sch, specs, root_sch, allow=allow_connectivity_change, dry_run=dry_run)


def remove_sheet_pins(parent_sch: str, specs: list[dict], root_sch: str | None = None, *, remove_child_labels: bool = False,
                      allow_connectivity_change: bool = False, dry_run: bool = False) -> dict[str, Any]:
    return _pipeline("remove", parent_sch, specs, root_sch, allow=allow_connectivity_change, dry_run=dry_run, remove_child_labels=remove_child_labels)


def move_sheet_pins(parent_sch: str, specs: list[dict], root_sch: str | None = None, *, allow_connectivity_change: bool = False,
                    dry_run: bool = False) -> dict[str, Any]:
    return _pipeline("move", parent_sch, specs, root_sch, allow=allow_connectivity_change, dry_run=dry_run)
