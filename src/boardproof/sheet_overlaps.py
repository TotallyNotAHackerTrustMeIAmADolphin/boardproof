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
``pin_on_wire``
    A visible pin of a symbol or sheet symbol (not a power symbol) that lies on the interior of a wire, with or without a
    junction there (issue 262): the pin has no stub of its own, the rail runs straight through it. ``junction`` says whether a
    junction sits on the pin. With no junction the pair is also a ``wire_through_pin``. Pins on a wire END, wire stubs shorter
    than 1.27 mm and the pins of power symbols are not read.
``outside_page``
    A symbol, sheet symbol, label, free text or visible property text whose box reaches beyond the drawing
    frame (``FRAME_MARGIN_MM`` inside the page edge) of the sheet's own page, with how many mm over the
    frame and over the paper edge in each direction. The page is the raw ``(paper ...)`` token read by
    ``sheet_extents.page_size_mm``, origin at the page's top left corner. Wires, junctions and graphic
    shapes are not checked. A custom drawing sheet (``page_layout_descr_file`` of the project) or an
    unreadable ``.kicad_pro`` makes the frame unknown: only the paper edge is checked and a
    ``frame_unknown`` entry is listed in ``skipped``. A paper token that cannot be read gives a
    ``paper_unreadable`` entry, never a clean sheet. A sheet symbol is a box on its PARENT sheet.

``text_over_pin_text``
    A label, free text or visible property text whose box overlaps by more than ``TEXT_MM`` in both axes a
    visible pin number or pin name of a placed symbol (issue 225; boxes from ``_pin_text_boxes``: hidden
    pins, hidden numbers and names, empty and ``~`` names, names of a symbol whose offset is 0 and power
    symbols give none). A label on a pin whose text runs along the pin line over its number is one.

Global and hierarchical label boxes are their shape and text (``_shape_label_box``, issue 225).

Text boxes are ESTIMATES, because the stroke font's extent is not in the file: the same estimate as
``schematic_readability`` (the sum of a measured per-character advance after KiCad markup is resolved, a local label above its anchor) and
every finding that involves a text says ``estimated: true``. The box of a placed symbol's property text is put where
KiCad draws it (issue 223, ``sheet_property_arrange.drawn_box``, the rule measured in issue 221): the stored angle is
relative to the symbol, a turn of 90 or 180 degrees and a mirror swap the justify words as drawn. A near miss is not reported.
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
from boardproof.sheet_property_arrange import drawn_box

KINDS = ("symbol_symbol", "text_text", "text_symbol", "text_wire", "wire_through_pin", "outside_page",
         "label_over_own_symbol", "text_hidden_behind_symbol", "text_over_pin_text", "pin_on_wire")
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


# --------------------------------------------------------------------------- label shapes and pin texts (issue 225)

# Global and hierarchical labels, measured with kicad-cli 10.0.7 sch export svg (font size s = 1.27; every figure scales with s).
# Direction of the label from its anchor: the stored justify decides, the angle only by its axis (measured: left at 0, 90 and 270 and
# right at 180 as add_labels writes them, and right at all four: at 0 and 180 left runs right and right runs left; at 90 and 270
# left runs up and right runs down; left at 180 was not measured and follows the same rule). Global label: a frame 2 s high
# from the anchor to the text length plus a tail per shape; hierarchical label: a marker 1 s long and s high at the anchor, the text
# starts 1.15 s from the anchor and is s high.
_GLOBAL_TAIL = {"input": 2.242, "output": 2.242, "bidirectional": 3.354, "tri_state": 3.354, "passive": 1.131}  # mm at s = 1.27
_HIER_INSET = 1.15


def _label_direction(angle: float, justify: str) -> tuple[int, int]:
    a = round(angle) % 360
    if a in (0, 180):
        return (-1, 0) if justify == "right" else (1, 0)
    return (0, 1) if justify == "right" else (0, -1)


def _shape_label_box(node: Any, text: str, at: tuple[float, float, float], effects: Any) -> Box:
    """The box of a global or hierarchical label: its shape (frame or arrow marker) and its text, as KiCad draws them."""
    size = readability._font_size(effects)
    s = size[1]
    h, _v = readability._justify(effects)
    length = readability._text_width(text, size)
    if node.name == "global_label":
        shape = next((str(c.values[0]) for c in node.children if not c.is_atom and c.name == "shape" and c.values), "bidirectional")
        reach, half = length + _GLOBAL_TAIL.get(shape, _GLOBAL_TAIL["bidirectional"]) * s / 1.27, s
    else:
        reach, half = _HIER_INSET * s + length, s / 2
    dx, dy = _label_direction(at[2], h)
    x, y = at[0], at[1]
    corners = [(x, y - half), (x + dx * reach, y + half)] if dx else [(x - half, y), (x + half, y + dy * reach)]
    return (min(c[0] for c in corners), min(c[1] for c in corners), max(c[0] for c in corners), max(c[1] for c in corners))


_NUMBER_GAP_MM = 0.36  # measured: a pin number sits this far from its pin line (above a horizontal pin, left of a vertical one)
_DEFAULT_NAME_OFFSET = 0.508


def _flag(node: Any, name: str) -> bool:
    """The library symbol carries ``(name (hide yes))`` or ``(name hide)`` (also inside ``pin_names`` beside an offset)."""
    for c in se._kids(node, name):
        if any(a.is_atom and a.value == "hide" for a in c.children):
            return True
        if any(not a.is_atom and a.name == "hide" and (not a.values or str(a.values[0]) != "no") for a in c.children):
            return True
    return False


def _pin_text_boxes(root: Any, node: Any) -> list[tuple[str, str, str, Box]]:
    """``(what, pin number, text, box)`` of the visible pin numbers and pin names of one placed symbol, as KiCad draws them
    (issue 225, kicad-cli svg over turns and mirrors). A pin number is centred on the middle of its pin line, 0.36 mm above a
    horizontal pin and left of a vertical one; a pin name starts ``offset`` (the library's pin_names offset, 0.508 mm when it names
    none) past the inner end of the line and runs into the body. Hidden pins, hidden numbers and names, and the names of a symbol
    whose offset is 0 (drawn outside the body: not measured) give nothing."""
    lib = se._lib_symbol(root, node)
    if lib is None:
        return []
    sx, sy, rot = se._at(node)
    mirror = se._mirror(node)
    unit = (node.find_children("unit")[0].get_int(0) if node.find_children("unit") else 1) or 1
    style = (node.find_children("body_style")[0].get_int(0) if node.find_children("body_style") else 1) or 1
    show_numbers = not _flag(lib, "pin_numbers")
    names_node = se._kids(lib, "pin_names")
    offset = _DEFAULT_NAME_OFFSET
    if names_node and names_node[0].find_children("offset"):
        offset = names_node[0].find_children("offset")[0].get_float(0)
    show_names = not _flag(lib, "pin_names") and offset > 0
    out: list[tuple[str, str, str, Box]] = []
    for sub in [lib, *se._kids(lib, "symbol")]:
        if sub is not lib:
            parts = (sub.get_string(0) or "").rsplit("_", 2)
            if len(parts) < 3 or not (parts[1].isdigit() and parts[2].isdigit()):
                continue
            if int(parts[1]) not in (0, unit) or int(parts[2]) not in (0, style):
                continue
        for pin in se._kids(sub, "pin"):
            at = pin.find_children("at")
            length = pin.find_children("length")
            if not at or not length or se._pin_hidden(pin):
                continue
            px, py = at[0].get_float(0), at[0].get_float(1)
            angle = math.radians(at[0].get_float(2) if len(at[0].children) > 2 else 0.0)
            ln = length[0].get_float(0)
            ox, oy = se.place(px, py, rot, mirror)
            ix, iy = se.place(px + ln * math.cos(angle), py + ln * math.sin(angle), rot, mirror)
            ox, oy, ix, iy = sx + ox, sy + oy, sx + ix, sy + iy
            horizontal = abs(ix - ox) >= abs(iy - oy)
            sign = (1 if ix > ox else -1) if horizontal else (1 if iy > oy else -1)
            number = pin.find_children("number")
            for what, shown in (("number", show_numbers), ("name", show_names)):
                child = pin.find_children(what)
                text = (child[0].get_string(0) or "") if child else ""
                if not shown or text in ("", "~"):
                    continue
                size = readability._font_size(child[0].find("effects"))
                width, height = readability._text_width(text, size), size[1]
                if what == "number":
                    mx, my = (ox + ix) / 2, (oy + iy) / 2
                    if horizontal:
                        box = (mx - width / 2, oy - _NUMBER_GAP_MM - height, mx + width / 2, oy - _NUMBER_GAP_MM)
                    else:
                        box = (ox - _NUMBER_GAP_MM - height, my - width / 2, ox - _NUMBER_GAP_MM, my + width / 2)
                elif horizontal:
                    x0 = ix + offset if sign > 0 else ix - offset - width
                    box = (x0, iy - height / 2, x0 + width, iy + height / 2)
                else:
                    y0 = iy + offset if sign > 0 else iy - offset - width
                    box = (ix - height / 2, y0, ix + height / 2, y0 + width)
                out.append((what, (number[0].get_string(0) or "") if number else "", text, box))
    return out


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
        # a placed symbol's text is drawn with the symbol's turn and mirror applied (issue 223); a sheet symbol's is not turned
        box = drawn_box(owner, prop, text) if owner.name == "symbol" else readability._text_box(text, at, readability._font_size(effects), h, v)
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
        if node.name == "label":
            box = readability._text_box(text, (at[0], at[1], at[2] % 180.0), readability._font_size(effects), "left" if h == "center" else h, "center", above=True)
        else:  # issue 225: a global or hierarchical label is its shape and its text, which KiCad places by other rules
            box = _shape_label_box(node, text, at, effects)
        texts.append(_Text("label", node, None, node.name, text, (at[0], at[1]), box))
    for node in se._kids(root, "text"):
        text = node.get_string(0) or ""
        if not text.strip():
            continue
        at = se._at(node)
        effects = node.find("effects")
        h, v = readability._justify(effects)
        # a free text with no (justify) is drawn centred on its anchor (measured: kicad-cli svg draws it text-anchor middle), so centre stays centre
        box = readability._text_box(text, at, readability._font_size(effects), h, v)
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
            if text.owner is body.node or not readability._overlaps(text.box, body.box, TEXT_MM):
                continue
            items = [text.item(), body.item()]
            if body.power:  # issue 262: a text over a power symbol is a text_symbol; a label on its pin names that net and is not
                if not (text.kind == "label" and any(se._same(text.at, pin) for pin in body.pins)):
                    add("text_symbol", text.at, items, "move the text clear of the power symbol", True)
                continue
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
    for body in solid:
        if body.kind != "symbol":
            continue
        for what, number, pin_text, pbox in _pin_text_boxes(root, body.node):
            for text in texts:
                if readability._overlaps(text.box, pbox, TEXT_MM):
                    px, py = readability._penetration(text.box, pbox)
                    add("text_over_pin_text", text.at,
                        [text.item(), {"kind": "pin_text", "what": what, "pin": number, "text": pin_text, "reference": body.name, "uuid": body.uuid, "box": _box_dict(pbox)}],
                        "move the text clear of the pin " + what + " (move_labels, move_text or set_property_positions), or turn the label so it runs away from the part",
                        True, overlap=[se._r(px), se._r(py)])
    for body in solid:  # issue 262: a pin on the middle of a wire has no stub of its own, with or without a junction
        for pin in body.pins:
            on_junction = any(se._same(pin, j) for j in junctions)
            for node, a, b in wires:
                if _strictly_inside(a, b, pin):
                    add("pin_on_wire", pin, [body.item(), {"kind": "wire", "uuid": se._uuid(node), "points": [se._round_pt(*a), se._round_pt(*b)]}],
                        "give the pin a stub: end the rail's wire at a short wire from the pin (at least 1.27 mm), or move the part off the rail",
                        False, pin=se._round_pt(*pin), junction=on_junction)
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
                    "text boxes are estimates from the font size (a finding with estimated true), power symbols are not compared as bodies of symbol_symbol and text_hidden_behind_symbol (a text over one is a text_symbol)",
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
