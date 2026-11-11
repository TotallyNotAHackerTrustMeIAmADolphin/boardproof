"""Free text objects on a sheet (GitHub issue 145).

``set_text`` (issue 114) changes the text of an existing label or free text object; nothing created
a free text. ``add_text`` does: annotations beside pins, notes and the like, which must not be
labels because a label names a net. A free text joins nothing, so the call proves that instead of
assuming it:

- the netlist of the project, read by kicad-cli from scratch copies before and after, must be
  unchanged (the pipeline of ``sheet_editing.run``, ``connectivity_changed`` otherwise; there is
  no override), and kicad-cli's ERC census of the project must be unchanged too (a violation count
  that moves refuses the call as ``write_not_verified``, check ``erc_unchanged``);
- the staged check of the pipeline sees that the only new nodes are the texts the call meant to
  add and that every other node is byte-equal;
- the write goes through ``write_layer.save``, whose verify reloads the temporary sibling and
  compares it with the serialised tree before ``os.replace``.

The node is built from the raw KiCad 10 shape ``(text "..." (exclude_from_sim no) (at x y a)
(effects (font (size s s)) [(justify ..)]) (uuid ".."))``; ``exclude_from_sim`` is left out for a
sheet older than the version that introduced it (20231120). A text that holds a line break, a tab
or a Unicode line separator is refused (the same refusal ``set_text`` gives a label; free text is
one line here, and ``set_text`` can still make it several). Nothing is read from ``kicad_tools``'
convenience properties.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import sheet_authoring, sheet_editing as se, write_layer
from boardproof.sheet_authoring import SheetAuthoringError

_ITEM_KEYS = {"x", "y", "text", "rotation", "size", "justify"}
_ROTATIONS = (0, 90)  # measured: kicad-cli reads 180 as 0 and 270 as 90, so only what KiCad keeps is written
MIN_SIZE, MAX_SIZE = 0.1, 50.0
DEFAULT_SIZE = 1.27
_EXCLUDE_FROM_SIM_SINCE = 20231120  # the schematic file version that writes (exclude_from_sim ..) in a text
# a line break in any form: LF, CR, VT, FF, the C1 NEL and the Unicode line and paragraph separators, and a tab
_BREAKS = frozenset("\t\r\n\x0b\x0c\x1c\x1d\x1e\x1f\x85\u2028\u2029")
# nodes KiCad writes after the drawing objects; a new text goes in front of the first of them
_TAIL = ("sheet_instances", "symbol_instances", "embedded_fonts", "embedded_files")


def _check_items(items: Any) -> list[dict]:
    if not isinstance(items, list) or not items:
        raise SheetAuthoringError("invalid_spec", "items must be a non-empty list of {x, y, text [, rotation, size, justify]}")
    seen: set[tuple] = set()
    for i, item in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(item, dict) or set(item) - _ITEM_KEYS:
            raise SheetAuthoringError("invalid_spec", f"{where} must be an object with keys from {sorted(_ITEM_KEYS)}, got {item!r}")
        for key in ("x", "y", "text"):
            if key not in item:
                raise SheetAuthoringError("invalid_spec", f"{where} needs {key!r}")
        for key in ("x", "y"):
            if not se.is_mm(item[key]):
                raise SheetAuthoringError("invalid_spec", f"{where}.{key} must be a finite number of mm within {se.MAX_MM:g}, got {item[key]!r}")
        text = item["text"]
        if not isinstance(text, str):
            raise SheetAuthoringError("invalid_spec", f"{where}.text must be a string, got {text!r}")
        if not text.strip():
            raise SheetAuthoringError("empty_text", f"{where}.text is empty; a text object cannot be given no text; nothing was written")
        if any(ch in _BREAKS for ch in text):
            raise SheetAuthoringError(
                "invalid_spec", f"{where}.text holds a line break, tab or line separator; add_text writes one line without tabs "
                "(set_text can give the created text several lines afterwards); nothing was written")
        try:
            write_layer.check_text(text)
        except write_layer.InvalidStringError as exc:
            raise SheetAuthoringError("invalid_spec", f"{where}.text cannot be written: {exc}; nothing was written") from exc
        rotation = item.get("rotation", 0)
        if isinstance(rotation, bool) or rotation not in _ROTATIONS:
            raise SheetAuthoringError("invalid_spec", f"{where}.rotation must be one of {list(_ROTATIONS)}, got {rotation!r}")
        size = item.get("size", DEFAULT_SIZE)
        if not se.is_mm(size) or not MIN_SIZE <= size <= MAX_SIZE:
            raise SheetAuthoringError("invalid_spec", f"{where}.size must be a number of mm from {MIN_SIZE} to {MAX_SIZE}, got {size!r}")
        if "justify" in item:
            se._justify_words(item["justify"], where)
        key = (text, float(item["x"]), float(item["y"]), rotation, float(size), " ".join(se._justify_words(item.get("justify", ""), where)))
        if key in seen:
            raise SheetAuthoringError("invalid_spec", f"{where} repeats an earlier item exactly (same text, position, rotation, size and justification); nothing was written")
        seen.add(key)
    return items


def text_node(sheet_version: int, text: str, x: float, y: float, rotation: int, size: float, justify: list[str]) -> SExp:
    """A free text object as KiCad 10 writes one (see the module docstring)."""
    effects = SExp.list("effects", SExp.list("font", SExp.list("size", write_layer.number(size), write_layer.number(size))))
    if justify:
        effects.children.append(SExp.list("justify", *(write_layer.symbol(w) for w in justify)))
    node = SExp.list("text", write_layer.string(text))
    if sheet_version >= _EXCLUDE_FROM_SIM_SINCE:
        node.children.append(SExp.list("exclude_from_sim", write_layer.symbol("no")))
    node.children.append(SExp.list("at", write_layer.number(se._r(x)), write_layer.number(se._r(y)), write_layer.number(rotation)))
    node.children.append(effects)
    node.children.append(sheet_authoring._uuid())
    return node


def _sheet_version(root: SExp) -> int:
    versions = root.find_children("version")
    return (versions[0].get_int(0) or 0) if versions else 0


def _insert(root: SExp, node: SExp) -> None:
    tail = [k for k, c in enumerate(root.children) if not c.is_atom and c.name in _TAIL]
    root.children.insert(tail[0] if tail else len(root.children), node)


def _erc_unchanged(sheet: se.Sheet, new: SExp):
    """The self-check: kicad-cli's ERC census of the project is the same with the texts as without."""

    def check(_before: dict, _after: dict) -> None:
        old: Counter = sheet.erc_for(None)
        now: Counter = sheet.erc_for(new)
        if old != now:
            moved = {f"{t} ({s})": [old.get((t, s), 0), now.get((t, s), 0)]
                     for (t, s) in sorted(set(old) | set(now)) if old.get((t, s), 0) != now.get((t, s), 0)}
            raise SheetAuthoringError(
                "write_not_verified", f"check erc_unchanged: kicad-cli's ERC of the project changed with the new text (before, after: {moved}); nothing was written",
                check="erc_unchanged", changed=moved)

    return check


def add_text(path: str, items: Any, root_sch: str | None, *, snap: Any, grid: Any, dry_run: bool) -> dict[str, Any]:
    """Add free text objects to the sheet at ``path`` (see the tool text)."""

    def mutate(sheet: se.Sheet, root: SExp) -> se.Outcome:
        specs = _check_items(items)
        se._check_move_args(snap, grid, False)
        version = _sheet_version(root)
        outcome = se.Outcome()
        added: list[dict[str, Any]] = []
        warnings: list[dict[str, Any]] = []
        for item in specs:
            want = (float(item["x"]), float(item["y"]))
            got = (se._snap(want[0], float(grid)), se._snap(want[1], float(grid))) if snap else (se._r(want[0]), se._r(want[1]))
            size = float(item.get("size", DEFAULT_SIZE))
            words = se._justify_words(item["justify"], "justify") if "justify" in item else []
            try:
                node = text_node(version, item["text"], got[0], got[1], item.get("rotation", 0), size, words)
            except write_layer.InvalidStringError as exc:
                raise SheetAuthoringError("invalid_spec", f"text {item['text']!r} cannot be written: {exc}; nothing was written") from exc
            _insert(root, node)
            uid = se._uuid(node)
            outcome.added.add(uid)
            added.append({"uuid": uid, "text": item["text"], "at": se._round_pt(*got), "rotation": item.get("rotation", 0), "size": size,
                          "justify": " ".join(words), "asked": se._round_pt(*want),
                          "snapped": se._round_pt(*got) if snap and not se._same(want, got) else None})
            if item["text"] != item["text"].strip():
                warnings.append({"code": "text_whitespace", "uuid": uid, "text": item["text"]})
        outcome.post_check = _erc_unchanged(sheet, root)
        outcome.report = {"added": added, "count": len(added), "grid": float(grid) if snap else None,
                          "note": "free text joins nothing: the netlist and the ERC census of the project were compared before and after on scratch copies and are unchanged"}
        if warnings:
            outcome.report["warnings"] = warnings
        return outcome

    result = se.run(path, root_sch, mutate, allow_connectivity_change=False, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_add"] = result.pop("added")
    return result
