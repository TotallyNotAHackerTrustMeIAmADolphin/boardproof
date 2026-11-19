"""Find what lies on top of what on the sheets of a project (GitHub issue 95, tool ``find_overlaps``).

Read-only: it writes nothing, makes no scratch copy and takes no lock. Every sheet file of the project's
sheet tree is read once from its raw S-expression tree (``sheet_tree.parsed_sheet``); no ``kicad_tools``
position or geometry property is read. Frame: sheet millimetres, x right, y down.

The kinds of finding (``KINDS``), each with its two (or one) items, their uuids, a position and the
numbers behind it:

``symbol_symbol``
    The boxes of two placed symbols (or hierarchical sheet symbols) overlap by more than ``BODY_MM`` in
    both axes. A symbol's box is the one ``sheet_align.symbol_box`` computes from the raw library geometry
    in the sheet file and the raw ``(at ...)``/``(mirror ...)`` of the placed symbol: its graphics plus the
    connection points and lines of its visible pins. A sheet symbol's box is its raw ``(at x y)`` and
    ``(size w h)``. Power symbols (a ``(power)`` library symbol, a ``#`` reference) are skipped: they sit
    on a pin by design. Boxes that only touch (two pins meeting at one wire end) are not an overlap.
``text_text``
    Two visible texts (symbol properties other than Footprint, Datasheet, Description and the ``ki_``
    library fields, labels, free text) whose boxes overlap by more than ``TEXT_MM`` in both axes.
``text_symbol``
    A visible text over the box of a symbol it does not belong to. A label is not reported against a
    symbol that has a pin on the label's anchor (a label on a pin sits next to its part).
``text_wire``
    A visible text whose box a wire crosses (more than ``TEXT_WIRE_MM`` of the wire inside the box,
    shrunk by ``TEXT_INSET_MM`` on every side). A label is not reported against a wire that touches its
    anchor (the wire it names).
``wire_through_pin``
    A wire whose interior (not an end) runs over a pin of a symbol or a sheet symbol, with no junction
    on that point. KiCad joins a pin only to a wire END or a junction, so such a pin joins nothing: the
    pin looks wired and is not (measured with kicad-cli's netlist in tests/test_find_overlaps.py).

``label_over_own_symbol``
    A label whose anchor is on a visible pin of a symbol or sheet symbol S (the pin-attachment test of
    ``text_symbol``, which skips exactly these pairs) and whose estimated text box overlaps S's box by more
    than ``TEXT_MM`` in both axes: the text runs into its own part. The label box uses the label's raw
    stored justification and angle, and 180 is drawn as 0 and 270 as 90 (left runs right or up, right runs left or down); a label
    on a pin whose text runs away from the part is not reported.
``text_hidden_behind_symbol``
    A property text of another symbol (Reference, Value) or a free text whose box is partly or wholly
    behind the box of a symbol, DNP or not (``dnp`` in the finding, from the raw ``(dnp yes)``), with
    ``covered_fraction``: the part of the text box inside the symbol box. Own texts and labels are not
    read here (labels are ``label_over_own_symbol`` or ``text_symbol``). Every such pair is ALSO reported
    as ``text_symbol``; this kind adds the share and the DNP flag.
``outside_page``
    A symbol, sheet symbol, label, free text or visible property text whose box reaches beyond the drawing
    frame (``FRAME_MARGIN_MM`` inside the page edge) of the sheet's own page, with how many mm over the
    frame and over the paper edge in each direction. The page is the raw ``(paper ...)`` token read by
    ``sheet_extents.page_size_mm``, origin at the page's top left corner. Wires, junctions and graphic
    shapes are not checked. A custom drawing sheet (``page_layout_descr_file`` of the project) or an
    unreadable ``.kicad_pro`` makes the frame unknown: only the paper edge is checked and a
    ``frame_unknown`` entry is listed in ``skipped``. A paper token that cannot be read gives a
    ``paper_unreadable`` entry, never a clean sheet. A sheet symbol is a box on its PARENT sheet.

Text boxes are ESTIMATES, because the stroke font's extent is not in the file: the same estimate as
``schematic_readability`` (characters times font size times 0.6, a label above its anchor) and
every finding that involves a text says ``estimated: true``. A near miss is not reported.
This is a layout aid: no finding is a verdict on the schematic, and an empty list says nothing about ERC.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from boardproof import connectivity_check, field_check, sheet_align, sheet_authoring, sheet_editing as se, sheet_extents, sheet_tree
from boardproof import schematic_readability as readability
from boardproof.sheet_authoring import SheetAuthoringError

KINDS = ("symbol_symbol", "text_text", "text_symbol", "text_wire", "wire_through_pin", "outside_page",
         "label_over_own_symbol", "text_hidden_behind_symbol")
# The default drawing sheet's frame lies 10 mm inside the page on every side. Measured: the SVG that kicad-cli 10.0.7
# exports draws the outer frame line 10 mm from each edge of A4 landscape and A3 portrait
# (tests/test_find_overlaps_outside_page.py reads it back).
FRAME_MARGIN_MM = 10.0
BODY_MM = readability.OVERLAP_MM  # boxes must overlap by more than this in both axes
TEXT_MM = readability.TEXT_OVERLAP_MM  # the same for the (estimated) text boxes
TEXT_INSET_MM = 0.25
TEXT_WIRE_MM = 0.5
DEFAULT_MAX = 500
MAX_MAX = 5000

Box = tuple[float, float, float, float]


# --------------------------------------------------------------------------- reading one sheet


def _is_power(root: Any, node: Any) -> bool:
    lib = se._lib_symbol(root, node)
    return se._ref(node).startswith("#") or (lib is not None and bool(lib.find_children("power")))


def _box_dict(box: Box) -> list[float]:
    return [se._r(v) for v in box]


class _Body:
    def __init__(self, kind: str, node: Any, name: str, box: Box, pins: list[tuple[float, float]], power: bool) -> None:
        self.kind, self.node, self.name, self.box, self.pins, self.power = kind, node, name, box, pins, power
        self.uuid = se._uuid(node)

    def item(self) -> dict[str, Any]:
        out = {"kind": self.kind, "uuid": self.uuid, "box": _box_dict(self.box)}
        out["reference" if self.kind == "symbol" else "name"] = self.name
        return out


class _Text:
    def __init__(self, kind: str, node: Any, owner: Any, label: str, text: str, at: tuple[float, float], box: Box) -> None:
        self.kind, self.node, self.owner, self.label, self.text, self.at, self.box = kind, node, owner, label, text, at, box
        self.uuid = se._uuid(node)

    def item(self) -> dict[str, Any]:
        out: dict[str, Any] = {"kind": self.kind, "uuid": self.uuid, "text": self.text, "at": se._round_pt(*self.at), "box": _box_dict(self.box)}
        if self.kind == "property":
            out["owner_uuid"] = se._uuid(self.owner)
            out["property"] = self.label
        return out


def _text_of(prop: Any) -> str:
    return prop.get_string(1) or ""


def _property_texts(owner: Any, skip: tuple[str, ...]) -> list[_Text]:
    out = []
    for prop in se._kids(owner, "property"):
        name = prop.get_string(0) or ""
        text = _text_of(prop)
        if name in skip or name.startswith("ki_") or not text.strip() or not readability._prop_visible(prop) or not prop.find_children("at"):
            continue
        at = se._at(prop)
        effects = prop.find("effects")
        h, v = readability._justify(effects)
        box = readability._text_box(text, at, readability._font_size(effects), h, v)
        out.append(_Text("property", prop, owner, name, text, (at[0], at[1]), box))
    return out


def _read(root: Any) -> tuple[list[_Body], list[_Text], list[tuple[Any, tuple, tuple]], list[dict[str, Any]], list[tuple[float, float]]]:
    """Bodies, texts, wires (node, end, end), skipped symbols and junction points of one sheet."""
    bodies: list[_Body] = []
    texts: list[_Text] = []
    skipped: list[dict[str, Any]] = []
    for node in se._kids(root, "symbol"):
        if not node.find_children("lib_id"):
            continue
        ref = se._ref(node)
        power = _is_power(root, node)
        box = sheet_align.symbol_box(root, node)
        if box is None:
            skipped.append({"reference": ref, "uuid": se._uuid(node), "reason": "no_geometry"})
        else:
            pins = [(p["x"], p["y"]) for p in se.pin_positions(root, node, with_hidden=True) if not p["hidden"]]
            bodies.append(_Body("symbol", node, ref, box, pins, power))
        if not power:
            texts += _property_texts(node, readability._QUIET_PROPERTIES)
    for node in se._kids(root, "sheet"):
        if not (node.find_children("at") and node.find_children("size")):
            continue
        x, y, _a = se._at(node)
        size = node.find_children("size")[0]
        name = next((_text_of(p) for p in se._kids(node, "property") if p.get_string(0) in ("Sheetname", "Sheet name")), "")
        pins = [se._at(p)[:2] for p in se._kids(node, "pin")]
        bodies.append(_Body("sheet", node, name or "<sheet>", (x, y, x + size.get_float(0), y + size.get_float(1)), pins, False))
        texts += _property_texts(node, ())
    for node in se._kids(root, "label") + se._kids(root, "global_label") + se._kids(root, "hierarchical_label"):
        text = node.get_string(0) or ""
        at = se._at(node)
        effects = node.find("effects")
        h, _v = readability._justify(effects)
        # KiCad draws a label with the stored justify at the stored angle taken modulo 180 (180 as 0, 270 as 90), no half-turn flip
        # (measured with kicad-cli 10.0.7, tests/test_find_overlaps_label_hidden.py); _text_box flips from 180 on, so hand it angle % 180.
        box = readability._text_box(text, (at[0], at[1], at[2] % 180.0), readability._font_size(effects), "left" if h == "center" else h, "center", above=True)
        texts.append(_Text("label", node, None, node.name, text, (at[0], at[1]), box))
    for node in se._kids(root, "text"):
        text = node.get_string(0) or ""
        if not text.strip():
            continue
        at = se._at(node)
        effects = node.find("effects")
        h, v = readability._justify(effects)
        box = readability._text_box(text, at, readability._font_size(effects), "left" if h == "center" else h, "bottom" if v == "center" else v)
        texts.append(_Text("text", node, None, "text", text, (at[0], at[1]), box))
    wires = []
    for node in se._kids(root, "wire"):
        ends = se._wire_ends(node)
        if len(ends) >= 2:
            wires.append((node, ends[0], ends[-1]))
    junctions = [se._at(j)[:2] for j in se._kids(root, "junction")]
    return bodies, texts, wires, skipped, junctions


# --------------------------------------------------------------------------- geometry


def _shrink(box: Box, by: float) -> Box | None:
    out = (box[0] + by, box[1] + by, box[2] - by, box[3] - by)
    return out if out[0] < out[2] and out[1] < out[3] else None


def _length_inside(a: tuple[float, float], b: tuple[float, float], box: Box) -> float:
    """The length of the segment a-b inside ``box`` (Liang-Barsky clipping)."""
    t0, t1 = 0.0, 1.0
    for p, q in ((-(b[0] - a[0]), a[0] - box[0]), (b[0] - a[0], box[2] - a[0]),
                 (-(b[1] - a[1]), a[1] - box[1]), (b[1] - a[1], box[3] - a[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return 0.0
            continue
        r = q / p
        if p < 0:
            t0 = max(t0, r)
        else:
            t1 = min(t1, r)
        if t0 > t1:
            return 0.0
    return (t1 - t0) * math.hypot(b[0] - a[0], b[1] - a[1])


def _strictly_inside(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> bool:
    """``p`` lies on the segment a-b but is not one of its ends."""
    return se._segment_contains(a, b, p) and not se._same(a, p) and not se._same(b, p)


# --------------------------------------------------------------------------- the finder


def _frame_margin(root_file: Path) -> float | None:
    """The frame margin of the default drawing sheet unless the project names its own one; None
    (unknown) when it does, or when the project file cannot be read as that."""
    pro = root_file.with_suffix(".kicad_pro")
    if not pro.exists():
        return FRAME_MARGIN_MM
    try:
        named = json.loads(pro.read_text(encoding="utf-8"))["schematic"].get("page_layout_descr_file", "")
    except (OSError, ValueError, KeyError, AttributeError, TypeError):
        return None
    return FRAME_MARGIN_MM if named == "" else None


def _sides(box: Box, w: float, h: float, inset: float, tol: float) -> dict[str, float]:
    """How far ``box`` reaches past the rectangle ``inset`` inside the page, per side, for the sides over by more than ``tol``."""
    over = {"left": inset - box[0], "right": box[2] - (w - inset), "top": inset - box[1], "bottom": box[3] - (h - inset)}
    return {k: se._r(v) for k, v in over.items() if v > tol}


def _outside_page(root: Any, margin: float | None, bodies: list[_Body], texts: list[_Text]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(findings without the sheet name, skipped entries) of the outside_page kind for one sheet."""
    page = sheet_extents.page_size_mm(root)
    if page is None:
        return [], [{"kind": "outside_page", "reason": "paper_unreadable"}]
    skipped = [{"kind": "outside_page", "reason": "frame_unknown"}] if margin is None else []
    w, h = page
    out = []
    boxes = [(b.item(), b.box, (b.box[0], b.box[1]), BODY_MM, False) for b in bodies] + [(t.item(), t.box, t.at, TEXT_MM, True) for t in texts]
    for item, box, at, tol, estimated in boxes:
        over_paper = _sides(box, w, h, 0.0, tol)
        over_frame = _sides(box, w, h, margin, tol) if margin is not None else {}
        if over_paper or over_frame:
            out.append({"kind": "outside_page", "at": se._round_pt(*at), "items": [item], "estimated": estimated,
                        "fix": "move the item inside the frame (move_symbols, move_labels, move_text or set_property_positions), or set a larger paper with set_paper",
                        "page_mm": [w, h], "frame_margin_mm": margin, "over_frame_mm": over_frame, "over_paper_mm": over_paper})
    return out, skipped


def _findings(sheet: str, root: Any, margin: float | None, page_check: bool) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bodies, texts, wires, skipped, junctions = _read(root)
    found: list[dict[str, Any]] = []

    def add(kind: str, at: tuple[float, float], items: list[dict[str, Any]], fix: str, estimated: bool, **extra: Any) -> None:
        found.append({"kind": kind, "sheet": sheet, "at": se._round_pt(*at), "items": items, "estimated": estimated, "fix": fix, **extra})

    solid = [b for b in bodies if not b.power]
    for i, first in enumerate(solid):
        for second in solid[i + 1:]:
            if readability._overlaps(first.box, second.box, BODY_MM):
                px, py = readability._penetration(first.box, second.box)
                add("symbol_symbol", (max(first.box[0], second.box[0]), max(first.box[1], second.box[1])), [first.item(), second.item()],
                    "move one of the two symbols (move_symbols, or align_items and distribute_items for a group)", False,
                    overlap=[se._r(px), se._r(py)])
    for i, first in enumerate(texts):
        for second in texts[i + 1:]:
            if readability._overlaps(first.box, second.box, TEXT_MM):
                add("text_text", second.at, [first.item(), second.item()],
                    "move one of the two texts (set_property_positions for symbol texts, move_labels for labels)", True)
    for text in texts:
        for body in bodies:
            if body.power or text.owner is body.node or not readability._overlaps(text.box, body.box, TEXT_MM):
                continue
            items = [text.item(), body.item()]
            if text.kind == "label" and any(se._same(text.at, pin) for pin in body.pins):
                px, py = readability._penetration(text.box, body.box)
                add("label_over_own_symbol", text.at, items, "turn the label (move_labels) or move it to the far side of the pin so its text runs away from the part",
                    True, overlap=[se._r(px), se._r(py)])
                continue
            add("text_symbol", text.at, items, "move the text clear of the symbol", True)
            if text.kind != "label":
                area = (text.box[2] - text.box[0]) * (text.box[3] - text.box[1])
                px, py = readability._penetration(text.box, body.box)
                dnp = body.kind == "symbol" and connectivity_check._dnp(body.node)
                add("text_hidden_behind_symbol", text.at, items, "move the text clear of the symbol (set_property_positions for a symbol text, move_text for a free text)", True,
                    covered_fraction=se._r(min(px, text.box[2] - text.box[0]) * min(py, text.box[3] - text.box[1]) / area), dnp=dnp)
        inner = _shrink(text.box, TEXT_INSET_MM)
        if inner is None:
            continue
        for node, a, b in wires:
            if text.kind == "label" and se._segment_contains(a, b, text.at):
                continue
            inside = _length_inside(a, b, inner)
            if inside > TEXT_WIRE_MM:
                add("text_wire", text.at, [text.item(), {"kind": "wire", "uuid": se._uuid(node), "points": [se._round_pt(*a), se._round_pt(*b)]}],
                    "move the text off the wire", True, length_inside=se._r(inside))
    for body in bodies:
        for pin in body.pins:
            if any(se._same(pin, j) for j in junctions):
                continue
            for node, a, b in wires:
                if _strictly_inside(a, b, pin):
                    add("wire_through_pin", pin, [body.item(), {"kind": "wire", "uuid": se._uuid(node), "points": [se._round_pt(*a), se._round_pt(*b)]}],
                        "split the wire at the pin (delete_wires and add_wires with an end on the pin) or move it clear: "
                        "KiCad joins a pin only to a wire end or a junction", False, pin=se._round_pt(*pin))
    if page_check:
        page_found, page_skipped = _outside_page(root, margin, bodies, texts)
        found += [{"sheet": sheet, **f} for f in page_found]
        skipped += page_skipped
    return found, skipped


def find_overlaps(sch_path: str, kinds: Any = None, root_sch: str | None = None, max_findings: Any = DEFAULT_MAX) -> dict[str, Any]:
    """Read-only: the overlaps of every sheet file of the project (see the module docstring)."""
    try:
        chosen = _kinds(kinds)
        if not isinstance(max_findings, int) or isinstance(max_findings, bool) or not 1 <= max_findings <= MAX_MAX:
            raise SheetAuthoringError("invalid_spec", f"max_findings must be a whole number from 1 to {MAX_MAX}, got {max_findings!r}")
        root_file, tree = field_check._resolve(sch_path, root_sch)
        page_check = "outside_page" in chosen
        margin = _frame_margin(root_file) if page_check else None
        everything: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        for path in tree.files:
            rel = tree.rel(path)
            found, skip = _findings(rel, sheet_tree.parsed_sheet(tree, path), margin, page_check)
            everything += [f for f in found if f["kind"] in chosen]
            skipped += [{**s, "sheet": rel} for s in skip]
        order = {k: i for i, k in enumerate(KINDS)}
        everything.sort(key=lambda f: (f["sheet"], order[f["kind"]], f["at"], [i["uuid"] for i in f["items"]]))
        counts = {k: sum(1 for f in everything if f["kind"] == k) for k in KINDS if k in chosen}
        return {
            "status": "ok", "ok": True, "written": False, "root": tree.rel(root_file), "kinds": [k for k in KINDS if k in chosen],
            "sheets_read": len(tree.files), "found": len(everything), "counts": counts,
            "findings": everything[:max_findings], "truncated": len(everything) > max_findings,
            "skipped": skipped,
            "note": "a layout aid, not a verdict: symbol boxes are the raw library geometry of the sheet file (graphics plus visible pins), "
                    "text boxes are estimates from the font size (a finding with estimated true), power symbols are not compared",
            **{k: v for k, v in tree.report().items() if k != "sheets"},
        }
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)


def _kinds(value: Any) -> set[str]:
    if value is None:
        return set(KINDS)
    if not isinstance(value, list) or not value or not all(isinstance(k, str) for k in value) or len(set(value)) != len(value):
        raise SheetAuthoringError("invalid_spec", f"kinds must be a non-empty list of distinct names from {list(KINDS)}")
    bad = [k for k in value if k not in KINDS]
    if bad:
        raise SheetAuthoringError("invalid_spec", f"unknown kinds {bad}; the kinds are {list(KINDS)}")
    return set(value)
