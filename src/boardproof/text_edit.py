"""Move and delete free text objects of a sheet (GitHub issue 198).

``add_text`` (issue 145) creates a free ``(text ..)`` and ``set_text`` (issue 114) changes its words;
nothing moved or removed one. A peer found eight notes lying below the A4 page after the sheets were
shrunk from A3 and had no way to bring them back. ``move_text`` and ``delete_text`` do that, on the
free texts and the text boxes of a sheet:

- **Which objects.** ``(text ..)`` and ``(text_box ..)`` (a note in a frame, ``(at x y a) (size w h)``)
  are both selected by the same keys: ``texts`` (exact), ``uuids``, ``pattern`` (shell pattern on the
  text), ``region`` (the object's ``(at ..)`` anchor inside the rectangle, for a text box the anchor
  too, not the whole frame) and ``kind`` (``text`` or ``text_box``). Labels are not texts: they name
  nets and have ``move_labels`` and ``delete_labels``.
- **What moves.** Only the ``(at x y a)`` of the object: x and y, and with ``rotation`` the angle. A
  text box keeps its ``(size ..)`` (the box grows right and down from the anchor), so the whole frame
  moves. Measured with kicad-cli 10.0.7 (``sch upgrade`` on a sheet with both kinds at 0, 90, 180, 270,
  45 and -90): a text keeps 0 and 90 and KiCad reads 180 as 0 and 270 and -90 as 90 (as ``add_text``
  found), a text box keeps every angle it is given. So a text may be turned to 0 or 90 and a text
  box to 0, 90, 180 or 270; anything else is refused.
- **Nothing else changes.** Free text joins nothing, but the call proves it as ``add_text`` does: the
  netlist of the project is read from scratch copies before and after (``connectivity_changed``, no
  override) and kicad-cli's ERC census must be the same (``write_not_verified``, check
  ``erc_unchanged``); the staged check of ``sheet_editing.run`` sees that only the selected nodes
  changed or are gone; the write is ``write_layer.save`` whose verify reloads the temporary sibling.
- **Out-of-page note.** A moved text that still lies outside the sheet's paper is listed under
  ``outside_page_after`` (anchor only: ``sheet_extents`` measures a text by its anchor, a text box by
  its corners). Nothing is refused for it.

Positions are read from the raw ``(at ..)`` tokens only.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import add_text, sheet_editing as se, sheet_extents, write_layer
from boardproof.sheet_authoring import SheetAuthoringError

TAGS = ("text", "text_box")
_SELECTOR_KEYS = {"texts", "uuids", "pattern", "region", "kind"}
_TEXT_ROTATIONS = (0, 90)             # measured: kicad-cli reads 180 as 0 and 270 as 90 for a free text
_BOX_ROTATIONS = (0, 90, 180, 270)    # measured: a text box keeps the angle it is given


def _nodes(root: SExp) -> list[SExp]:
    return [c for c in root.children if not c.is_atom and c.name in TAGS]


def _check_selector(sel: Any, where: str) -> dict:
    if not isinstance(sel, dict) or not sel or set(sel) - _SELECTOR_KEYS:
        raise SheetAuthoringError("invalid_spec", f"{where} must be a non-empty object with selector keys from {sorted(_SELECTOR_KEYS)}, got {sel!r}")
    for key in ("texts", "uuids"):
        if key in sel and (not isinstance(sel[key], list) or not sel[key] or not all(isinstance(v, str) for v in sel[key])):
            raise SheetAuthoringError("invalid_spec", f"{where}.{key} must be a non-empty list of strings, got {sel[key]!r}")
    if "pattern" in sel and not (isinstance(sel["pattern"], str) and sel["pattern"]):
        raise SheetAuthoringError("invalid_spec", f"{where}.pattern must be a non-empty string")
    if "kind" in sel and sel["kind"] not in TAGS:
        raise SheetAuthoringError("invalid_spec", f"{where}.kind must be one of {list(TAGS)}, got {sel['kind']!r}")
    return sel


def _pick(root: SExp, sel: dict, where: str) -> list[SExp]:
    """The texts and text boxes one selector picks (all keys must match). ``not_found`` for a listed text or uuid
    that no object of the sheet matches, ``selector_matched_nothing`` for a selector that picks nothing."""
    region = se._region(sel["region"], where) if "region" in sel else None
    hit: Counter = Counter()
    picked: list[SExp] = []
    for node in _nodes(root):
        text, uid = node.get_string(0) or "", se._uuid(node)
        if "kind" in sel and node.name != sel["kind"]:
            continue
        if "texts" in sel and text not in sel["texts"]:
            continue
        if "uuids" in sel and uid not in sel["uuids"]:
            continue
        if "pattern" in sel and not fnmatch.fnmatchcase(text, sel["pattern"]):
            continue
        if region is not None:
            x, y, _a = se._at(node)
            if not se._inside(region, x, y):
                continue
        picked.append(node)
        hit[("texts", text)] += 1
        hit[("uuids", uid)] += 1
    missing = [f"{key[:-1]} {v!r}" for key in ("texts", "uuids") if key in sel for v in sel[key] if hit[(key, v)] == 0]
    if missing:
        raise SheetAuthoringError("not_found", f"not found on this sheet: {', '.join(missing)}; nothing was written", not_found=missing)
    if not picked:
        raise SheetAuthoringError("selector_matched_nothing", f"{where} ({sel!r}) selects no text or text box; nothing was written")
    return picked


def _describe(node: SExp) -> dict[str, Any]:
    x, y, a = se._at(node)
    return {"uuid": se._uuid(node), "kind": node.name, "text": node.get_string(0), "at": se._round_pt(x, y), "rotation": a}


def _outside_note(root: SExp, uuids: set[str]) -> dict[str, Any]:
    """Which of the objects with these uuids lie outside the sheet's paper now (anchor only; see the module text)."""
    papers = root.find_children("paper")
    page = sheet_extents.page_size_mm(papers[0]) if papers else None
    if page is None:
        return {"page_mm": None, "outside_page_after": [], "note": "the paper size of the sheet could not be read, so the page check was not done"}
    out = [e for e in sheet_extents.outside_page(sheet_extents.sheet_extents(root), page) if e["uuid"] in uuids]
    return {"page_mm": [page[0], page[1]],
            "outside_page_after": [{k: e[k] for k in ("kind", "uuid", "label", "x1", "y1", "x2", "y2")} for e in out[: se.MAX_LISTED]],
            "outside_page_after_count": len(out)}


# --------------------------------------------------------------------------- move


def _plan(moves: Any, root: SExp, snap: bool, grid: float) -> list[tuple[SExp, float, float, int | None, dict]]:
    if not isinstance(moves, list) or not moves:
        raise SheetAuthoringError("invalid_spec", "moves must be a non-empty list of {selector keys..., to | offset [, rotation]}")
    planned: list[tuple[SExp, float, float, int | None, dict]] = []
    seen: set[int] = set()
    for i, move in enumerate(moves):
        where = f"moves[{i}]"
        if not isinstance(move, dict):
            raise SheetAuthoringError("invalid_spec", f"{where} must be an object")
        spec = dict(move)
        to, offset, rotation = spec.pop("to", None), spec.pop("offset", None), spec.pop("rotation", None)
        if to is not None and offset is not None:
            raise SheetAuthoringError("invalid_spec", f"{where} has both 'to' and 'offset'; give one")
        if to is None and offset is None and rotation is None:
            raise SheetAuthoringError("invalid_spec", f"{where} needs 'to' (absolute [x, y]), 'offset' (relative [dx, dy]) or 'rotation'")
        for name, val in (("to", to), ("offset", offset)):
            if val is not None and not (isinstance(val, (list, tuple)) and len(val) == 2 and all(se.is_mm(v) for v in val)):
                raise SheetAuthoringError("invalid_spec", f"{where}.{name} must be [x, y] in mm (finite, within {se.MAX_MM:g}), got {val!r}")
        if rotation is not None and (isinstance(rotation, bool) or rotation not in _BOX_ROTATIONS):
            raise SheetAuthoringError("invalid_spec", f"{where}.rotation must be one of {list(_BOX_ROTATIONS)}, got {rotation!r}")
        nodes = _pick(root, _check_selector(spec, where), where)
        if to is not None and len(nodes) != 1:
            raise SheetAuthoringError("invalid_spec", f"{where}: 'to' needs a selector that picks exactly one text, it picked {len(nodes)}; use 'offset'")
        for node in nodes:
            if id(node) in seen:
                raise SheetAuthoringError("invalid_spec", f"{where} picks a text that an earlier move already moves ({se._uuid(node)})")
            seen.add(id(node))
            if rotation is not None and node.name == "text" and rotation not in _TEXT_ROTATIONS:
                raise SheetAuthoringError(
                    "invalid_spec", f"{where}.rotation {rotation} is not kept by KiCad for a free text ({se._uuid(node)}; it reads 180 as 0 and 270 as 90): "
                    f"use {list(_TEXT_ROTATIONS)}, or select text boxes only (kind text_box)")
            x, y, _a = se._at(node)
            if to is None and offset is None:
                want = got = (x, y)        # a turn in place: never snapped
            else:
                want = (float(to[0]), float(to[1])) if to is not None else (x + float(offset[0]), y + float(offset[1]))
                got = (se._snap(want[0], grid), se._snap(want[1], grid)) if snap else (se._r(want[0]), se._r(want[1]))
            record = {"asked": se._round_pt(*want), "snapped": se._round_pt(*got) if snap and not se._same(want, got) else None}
            planned.append((node, got[0], got[1], rotation, record))
    return planned


def move_text(path: str, moves: Any, root_sch: str | None, *, snap: Any, grid: Any, dry_run: bool) -> dict[str, Any]:
    """Move or turn free texts and text boxes of the sheet at ``path`` (see the tool text)."""

    def mutate(sheet: se.Sheet, root: SExp) -> se.Outcome:
        se._check_move_args(snap, grid, False)
        planned = _plan(moves, root, snap, float(grid))
        outcome = se.Outcome(no_change=not planned)
        listed = []
        for node, nx, ny, rotation, record in planned:
            x, y, a = se._at(node)
            entry = {**_describe(node), "from": se._round_pt(x, y), "to": se._round_pt(nx, ny), **record}
            entry.pop("at")
            entry["rotation_from"] = a
            if rotation is not None:
                at = node.find_children("at")[0]
                if len(at.children) > 2:
                    write_layer.set_number(at, 2, rotation)
                else:
                    at.children.append(write_layer.number(rotation))
            entry["rotation"] = rotation if rotation is not None else a
            if abs(nx - x) > se.TOL / 10 or abs(ny - y) > se.TOL / 10:
                se._set_at(node, nx, ny)
            outcome.changed.add(se._uuid(node))
            listed.append(entry)
        outcome.post_check = add_text._erc_unchanged(sheet, root)
        outcome.report = {
            "moved": listed, "count": len(listed), "grid": float(grid) if snap else None,
            "snapped": [e["uuid"] for e in listed if e.get("snapped")],
            **_outside_note(root, {e["uuid"] for e in listed}),
            "note": "free text joins nothing: the netlist and the ERC census of the project were compared before and after on scratch copies and are unchanged",
        }
        return outcome

    result = se.run(path, root_sch, mutate, allow_connectivity_change=False, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_move"] = result.pop("moved")
    return result


# --------------------------------------------------------------------------- delete


def _leave_groups(root: SExp, gone: set[str], outcome: se.Outcome) -> tuple[list[dict[str, Any]], list[str]]:
    """Drop the deleted uuids from every ``(group .. (members ..))`` and remove a group left with no member. Measured
    (kicad-cli 10.0.7, sch upgrade): KiCad drops a dangling member silently on its next save and removes a group with none left."""
    changed: list[dict[str, Any]] = []
    removed: list[str] = []
    for group in [c for c in root.children if not c.is_atom and c.name == "group"]:
        holders = group.find_children("members")
        members = holders[0] if holders else None
        if members is None:
            continue
        hit = [m for m in members.children if str(m.value) in gone]
        if not hit:
            continue
        members.children[:] = [m for m in members.children if str(m.value) not in gone]
        gid = se._uuid(group)
        if members.children:
            outcome.changed.add(gid)
            changed.append({"group": gid, "members": [str(m.value) for m in hit]})
        else:
            se._remove(root, [group])
            outcome.deleted.add(gid)
            removed.append(gid)
    return changed, removed


def delete_text(path: str, selectors: Any, root_sch: str | None, *, dry_run: bool) -> dict[str, Any]:
    """Delete free texts and text boxes of the sheet at ``path`` (see the tool text)."""

    def mutate(sheet: se.Sheet, root: SExp) -> se.Outcome:
        if not isinstance(selectors, list) or not selectors:
            raise SheetAuthoringError("invalid_spec", "selectors must be a non-empty list of selector objects")
        picked: list[SExp] = []
        for i, sel in enumerate(selectors):
            where = f"selectors[{i}]"
            picked += [n for n in _pick(root, _check_selector(sel, where), where) if all(n is not p for p in picked)]
        listed = [_describe(n) for n in picked]
        outcome = se.Outcome(deleted={se._uuid(n) for n in picked})
        se._remove(root, picked)
        group_changes, groups_removed = _leave_groups(root, {se._uuid(n) for n in picked}, outcome)
        outcome.post_check = add_text._erc_unchanged(sheet, root)
        outcome.report = {
            "deleted": listed, "count": len(listed), "group_members_removed": group_changes, "groups_removed": groups_removed,
            "note": "free text joins nothing: the netlist and the ERC census of the project were compared before and after on scratch copies and are unchanged",
        }
        return outcome

    result = se.run(path, root_sch, mutate, allow_connectivity_change=False, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_delete"] = result.pop("deleted")
    return result

