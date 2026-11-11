"""Find the connection points of a sheet that are off the grid and move them onto it (GitHub issue 95, tool ``snap_to_grid``).

KiCad joins things at exact coordinates, so a pin, wire end, junction, label or no-connect marker a
fraction of a millimetre off the grid is hard to wire to, and ERC reports it (``endpoint_off_grid``)
without offering a fix. This tool lists every such point of one sheet and moves them to the grid in one
call, on the move machinery of issue 84 (``sheet_editing.run``: lock check, staged check, kicad-cli
netlist of scratch copies before and after, write layer with reload verify, ``dry_run``).

What it does, and the facts the tool text states:

- **Grid.** ``grid`` is 1.27 mm (50 mil) by default and the origin is the sheet origin (0, 0), as in the
  other move tools. A coordinate is off grid when it is more than ``sheet_editing.TOL`` (1 micrometre)
  from a multiple of ``grid``; the snapped value is the nearest multiple.
- **What is examined** (``kinds``): ``symbol`` (every pin of every placed symbol, power symbols included,
  hidden pins too; positions from the raw ``(at ...)`` maths of ``sheet_editing.pin_positions``),
  ``wire`` (each end), ``label`` (local, global and hierarchical: the anchor), ``junction`` and
  ``no_connect``. Free text, property texts and hierarchical sheet symbols are not connection points the
  tool snaps: a sheet symbol with an off-grid origin or pin is listed under ``unsnappable``.
- **A symbol moves as a whole.** Its shift is the one that puts a pin on the grid, tried pin by pin until
  one puts every pin on the grid; when none does (the library's pin spacing is not a multiple of the
  grid) the symbol stays and is listed under ``unsnappable``. Its property texts move with it. A wire end,
  junction, label or marker that sat exactly on a moved pin follows by the same shift, so the connection
  stays; every other off-grid point is rounded on its own, and two points at one place go to one place.
- **Connectivity.** kicad-cli's netlist of the project is compared before and after on scratch copies;
  a snap that splits or joins a net, strands a pin or renames a net is refused with
  ``connectivity_changed`` and nothing is written (there is no override: narrow ``kinds`` or move the
  parts first).
- **Geometry the netlist cannot see.** A wire laid over a pin joins nothing and a wire end that lands on one joins it,
  and neither shows as a netlist difference of a wire over a pin. After the snap the sheet is checked for wires over a pin,
  label, marker, junction or wire end that are not theirs and for wire ends touching more than before
  (``sheet_orient._crossings``, the guard of rotate_items); either is refused as ``snap_creates_connection``.
  A wire that was horizontal or vertical and no longer is, is listed under ``slanted``.
- **Self-check.** After the change the sheet is scanned again: every point the call meant to snap must be
  on the grid and every other must be where it was, else ``write_not_verified`` and nothing is written.
"""

from __future__ import annotations

from typing import Any

from kicad_tools.sexp import SExp

from boardproof import sheet_editing as se
from boardproof import sheet_orient
from boardproof.sheet_authoring import SheetAuthoringError

KINDS = ("symbol", "wire", "label", "junction", "no_connect")


def _check_kinds(value: Any) -> list[str]:
    if value is None:
        return list(KINDS)
    if not isinstance(value, list) or not value or not all(isinstance(k, str) for k in value) or len(set(value)) != len(value):
        raise SheetAuthoringError("invalid_spec", f"kinds must be a non-empty list of distinct names from {list(KINDS)}")
    bad = [k for k in value if k not in KINDS]
    if bad:
        raise SheetAuthoringError("invalid_spec", f"unknown kinds {bad}; the kinds are {list(KINDS)}")
    return [k for k in KINDS if k in value]


def _snap_value(value: float, grid: float) -> float:
    return se._snap(value, grid)


def _off(value: float, grid: float) -> bool:
    return abs(value - round(value / grid) * grid) > se.TOL


def _point_off(x: float, y: float, grid: float) -> bool:
    return _off(x, grid) or _off(y, grid)


def _points(sheet: se.Sheet, kinds: list[str]) -> list[tuple[str, SExp, tuple[float, float]]]:
    """Every examined connection point of the sheet that is not a symbol pin: (kind, node, (x, y))."""
    out: list[tuple[str, SExp, tuple[float, float]]] = []
    if "label" in kinds:
        out += [("label", n, se._at(n)[:2]) for n in sheet.labels]
    if "junction" in kinds:
        out += [("junction", n, se._at(n)[:2]) for n in sheet.junctions]
    if "no_connect" in kinds:
        out += [("no_connect", n, se._at(n)[:2]) for n in sheet.no_connects]
    return out


def _scan(root: SExp, kinds: list[str], grid: float) -> set[str]:
    """The uuids of the examined nodes that have an off-grid point (symbols by any pin, wires by any end)."""
    sheet = se.Sheet(root)
    bad: set[str] = set()
    if "symbol" in kinds:
        for node in sheet.symbols:
            if node.find_children("lib_id") and any(_point_off(p["x"], p["y"], grid) for p in se.pin_positions(root, node)):
                bad.add(se._uuid(node))
    if "wire" in kinds:
        for node in sheet.wires:
            if any(_point_off(x, y, grid) for x, y in se._wire_ends(node)):
                bad.add(se._uuid(node))
    for _kind, node, (x, y) in _points(sheet, kinds):
        if _point_off(x, y, grid):
            bad.add(se._uuid(node))
    return bad


def _contacts(sheet: se.Sheet) -> dict[tuple[str, int], int]:
    """Per wire end (wire uuid, 0 or 1): how many other connection points (pins, labels, markers, junctions, sheet
    pins, ends of other wires) sit exactly on it. The netlist sees these joins."""
    out: dict[tuple[str, int], int] = {}
    pts = sheet_orient._points(sheet)
    for wire in sheet.wires:
        ends = se._wire_ends(wire)
        if len(ends) < 2:
            continue
        for idx, end in enumerate((ends[0], ends[-1])):
            out[(se._uuid(wire), idx)] = sum(1 for point, owner in pts if owner != se._uuid(wire) and se._same(point, end))
    return out


def _wire_geometry(sheet: se.Sheet) -> tuple[set, dict]:
    """What the netlist cannot see: wires laid over a pin, label, marker, junction or wire end that is not theirs,
    and the contacts at the wire ends (see ``_contacts``)."""
    return sheet_orient._crossings(sheet, sheet.wires, sheet_orient._points(sheet)), _contacts(sheet)


def _fresh_crossings(before: set, after: set, move: Any) -> list:
    """The (wire, point) crossings after the snap that are not a before-crossing carried to where its point went
    (``move(x, y)`` gives the new place of a point). A wire over pin A before and over pin B after is new."""
    expected = [(u, move(*p)) for u, p in before]
    return sorted((u, p) for u, p in after if not any(u == eu and se._same(p, ep) for eu, ep in expected))


def _sheet_symbols_off(sheet: se.Sheet, grid: float) -> list[dict[str, Any]]:
    out = []
    for node in sheet.sheets:
        pts = [se._at(node)[:2], *[se._at(p)[:2] for p in se._kids(node, "pin")]]
        if any(_point_off(x, y, grid) for x, y in pts):
            name = next((p.get_string(1) for p in se._kids(node, "property") if p.get_string(0) in ("Sheetname", "Sheet name")), "")
            out.append({"kind": "sheet", "uuid": se._uuid(node), "name": name, "at": se._round_pt(*se._at(node)[:2]),
                        "reason": "a hierarchical sheet symbol is not snapped by this tool (its pins sit on its border): "
                                  "move it with move_sheet_pins or recreate it on the grid"})
    return out


def snap_to_grid(path: str, root_sch: str | None = None, *, kinds: Any = None, grid: Any = se.GRID, dry_run: bool = False) -> dict[str, Any]:
    """Move the off-grid connection points of one sheet onto the grid (see the module docstring)."""

    def mutate(sheet: se.Sheet, root: SExp) -> se.Outcome:
        chosen = _check_kinds(kinds)
        if not se.is_mm(grid) or grid <= 0:
            raise SheetAuthoringError("invalid_spec", f"grid must be a positive, finite number of mm (at most {se.MAX_MM:g}), got {grid!r}")
        g = float(grid)
        outcome = se.Outcome()
        crossings_before, contacts_before = _wire_geometry(sheet)
        slanted: list[str] = []
        listed: list[dict[str, Any]] = []
        unsnappable: list[dict[str, Any]] = _sheet_symbols_off(sheet, g)
        anchors: dict[tuple[float, float], tuple[float, float]] = {}
        stuck: set[str] = {u["uuid"] for u in unsnappable}
        on_grid = {k: 0 for k in chosen}
        found = {k: 0 for k in chosen}

        if "symbol" in chosen:
            for node in sheet.symbols:
                if not node.find_children("lib_id"):
                    continue
                pins = se.pin_positions(root, node)
                if not pins or not any(_point_off(p["x"], p["y"], g) for p in pins):
                    on_grid["symbol"] += 1
                    continue
                found["symbol"] += 1
                shift = None
                for p in pins:
                    cand = (_snap_value(p["x"], g) - p["x"], _snap_value(p["y"], g) - p["y"])
                    if not any(_point_off(q["x"] + cand[0], q["y"] + cand[1], g) for q in pins):
                        shift = cand
                        break
                if shift is None:
                    stuck.add(se._uuid(node))
                    unsnappable.append({"kind": "symbol", "reference": se._ref(node), "uuid": se._uuid(node), "at": se._round_pt(*se._at(node)[:2]),
                                        "pins": [{"number": p["number"], "x": p["x"], "y": p["y"]} for p in pins if _point_off(p["x"], p["y"], g)],
                                        "reason": "no single shift puts every pin on the grid (the library's pin spacing is not a multiple of the grid); "
                                                  "the symbol was not moved"})
                    for p in pins:
                        anchors[se._key(p["x"], p["y"])] = (0.0, 0.0)  # what sits on its pins stays on them
                    continue
                for p in pins:
                    key = se._key(p["x"], p["y"])
                    anchors[key] = shift
                x, y, _a = se._at(node)
                se._set_at(node, x + shift[0], y + shift[1])
                for prop in se._kids(node, "property"):
                    px, py, _pa = se._at(prop)
                    se._set_at(prop, px + shift[0], py + shift[1])
                outcome.changed.add(se._uuid(node))
                listed.append({"kind": "symbol", "reference": se._ref(node), "uuid": se._uuid(node), "from": se._round_pt(x, y),
                               "to": se._round_pt(x + shift[0], y + shift[1]), "shift": se._round_pt(*shift), "pins": se.pin_positions(root, node)})

        cache: dict[tuple[float, float], tuple[float, float]] = {}

        def target(x: float, y: float) -> tuple[float, float] | None:
            """Where a connection point at (x, y) goes, or None when it stays."""
            key = se._key(x, y)
            if key in anchors:
                return (x + anchors[key][0], y + anchors[key][1])
            if not _point_off(x, y, g):
                return None
            return cache.setdefault(key, (_snap_value(x, g), _snap_value(y, g)))

        def pinned(x: float, y: float) -> bool:
            """An off-grid point on a pin of a symbol that stays (its anchor is a zero shift)."""
            return anchors.get(se._key(x, y)) == (0.0, 0.0) and _point_off(x, y, g)

        def hold(kind: str, node: SExp, at: Any, why: str) -> None:
            stuck.add(se._uuid(node))
            unsnappable.append({"kind": kind, "uuid": se._uuid(node), "at": at, "reason": why})

        held_why = "it sits on a pin of a symbol that could not be moved onto the grid (see the symbol's entry), so it stays on that pin"
        if "wire" in chosen:
            for node in sheet.wires:
                ends = se._wire_ends(node)
                if len(ends) < 2:
                    continue
                if not any(_point_off(x, y, g) for x, y in ends) and not any(se._key(x, y) in anchors for x, y in ends):
                    on_grid["wire"] += 1
                    continue
                new = [target(x, y) or (x, y) for x, y in ends]
                if any(pinned(x, y) for x, y in new):
                    hold("wire", node, [se._round_pt(*e) for e in ends], held_why)
                if new == ends:
                    if se._uuid(node) not in stuck:
                        on_grid["wire"] += 1
                    continue
                found["wire"] += 1
                if se._same(new[0], new[-1]) and not se._same(ends[0], ends[-1]):
                    raise SheetAuthoringError("degenerate_wire", f"snapping would shrink the wire {se._uuid(node)} at {[se._round_pt(*e) for e in ends]} to a point; "
                                              "delete it first (delete_wires); nothing was written")
                for idx in (0, len(ends) - 1):
                    if new[idx] != ends[idx]:
                        se._set_wire_end(node, idx, *new[idx])
                if abs(ends[0][0] - ends[-1][0]) <= se.TOL or abs(ends[0][1] - ends[-1][1]) <= se.TOL:
                    if abs(new[0][0] - new[-1][0]) > se.TOL and abs(new[0][1] - new[-1][1]) > se.TOL:
                        slanted.append(se._uuid(node))
                outcome.changed.add(se._uuid(node))
                listed.append({"kind": "wire", "uuid": se._uuid(node), "was": [se._round_pt(*e) for e in ends], "now": [se._round_pt(*e) for e in new]})

        for kind, node, (x, y) in _points(sheet, chosen):
            got = target(x, y)
            if got is not None and pinned(*got):
                hold(kind, node, se._round_pt(x, y), held_why)
                continue
            if got is None or (abs(got[0] - x) <= se.TOL / 10 and abs(got[1] - y) <= se.TOL / 10):
                on_grid[kind] += 1
                continue
            found[kind] += 1
            se._set_at(node, *got)
            outcome.changed.add(se._uuid(node))
            entry = {"kind": kind, "uuid": se._uuid(node), "from": se._round_pt(x, y), "to": se._round_pt(*got)}
            if kind == "label":
                entry["text"] = node.get_string(0)
            listed.append(entry)

        # Self-check (issue 95): what the call meant to snap is on the grid now, and nothing else is off the grid
        # that was not before. A symbol, wire or point that is off the grid after the change and is not listed as
        # unsnappable means the write did not do what the plan said.
        bad_after = _scan(root, chosen, g)
        stray = sorted(bad_after - stuck)
        if stray:
            raise SheetAuthoringError("write_not_verified", f"check on_grid_after: {len(stray)} item(s) are still off the {g} mm grid after the change "
                                      f"(first: {stray[0]}); nothing was written", check="on_grid_after", items=stray[:se.MAX_LISTED])
        # What the netlist cannot see (issue 95 review): a wire laid over a pin or another connection point joins nothing
        # but looks wired, and a wire end that now sits exactly on something joins it. Both are new geometry the snap
        # made, so the call is refused rather than leaving either behind.
        after = se.Sheet(root)
        crossings_after, contacts_after = _wire_geometry(after)
        fresh = _fresh_crossings(crossings_before, crossings_after, lambda x, y: target(x, y) or (x, y))
        touching = sorted(k for k, n in contacts_after.items() if n > contacts_before.get(k, 0))
        if fresh or touching:
            raise SheetAuthoringError(
                "snap_creates_connection",
                f"the snap would lay {len(fresh)} wire(s) over a pin or other connection point and put {len(touching)} wire end(s) exactly on one "
                "(a wire over a pin joins nothing but looks wired; a wire end on a pin joins it); the netlist comparison cannot see the first kind. "
                "Nothing was written. Move the wire or the part first, or narrow kinds.",
                crossings=[{"wire": u, "point": list(p)} for u, p in fresh[:se.MAX_LISTED]],
                touching=[{"wire": u, "end": i} for u, i in touching[:se.MAX_LISTED]])
        outcome.no_change = not listed
        outcome.report = {
            "grid": g, "kinds": chosen, "count": len(listed), "moved": listed, "found_off_grid": found, "already_on_grid": on_grid,
            "unsnappable": unsnappable, "slanted": slanted,
            "note": f"the grid is {g} mm with its origin at the sheet origin; a point is off the grid when it is more than {se.TOL} mm from a multiple of it. "
                    "A symbol moves as a whole with its property texts, and what sat exactly on a moved pin follows; every other point is rounded on its own. "
                    "Free text, property texts and hierarchical sheet symbols are not snapped.",
        }
        return outcome

    result = se.run(path, root_sch, mutate, allow_connectivity_change=False, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_move"] = result.pop("moved")
    return result
