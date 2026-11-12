"""Where the objects of a schematic sheet lie, and how big the page is (GitHub issue 197).

Two small, separate facts that ``set_paper`` needs and that other tools (a text mover, an
out-of-page check) can reuse:

- ``page_size_mm(paper)``: the width and height in mm of the sheet's ``(paper ...)`` token, as KiCad
  reads it. Measured with kicad-cli 10.0.7 (SVG export of each form, docs/ARCHITECTURE.md): the named
  sizes are A5 A4 A3 A2 A1 A0 and A B C D E and USLetter USLegal USLedger, all landscape; the word
  ``portrait`` after a named size swaps width and height; ``(paper "User" w h)`` is w by h, KiCad
  clamps each side to 25.4 .. 3048 mm and, when it writes the sheet again, drops a ``portrait`` flag
  after swapping the numbers. KiCad reads a name in any letter case ("a4" is A4) and writes it
  canonical.
- ``sheet_extents(root)``: one entry per drawing object that is a direct child of the sheet, read from
  the raw ``(at ..)``, ``(xy ..)``, ``(start ..)`` ... S-expressions (never from ``kicad_tools``
  convenience geometry), with its box in sheet coordinates (mm, Y down, origin at the page's top left
  corner).

What a box covers (``measured``): a placed symbol is its origin, its visible library graphics and
every pin end (``symbol_body_and_pins``; ``symbol_pins`` when the sheet holds no library geometry for
it; ``origin`` when it has neither); a wire or bus, its ends; a rectangle, polyline, bezier, circle
(centre and radius), arc (its axis extremes), sheet symbol and text box, their corners (a text box
rotation is not applied); a bus entry, its two ends; junctions, no-connect markers, labels, texts,
netclass flags and images are ``origin`` only.

NOT measured, and stated with every result by the callers: the extent of any text (a label, a note, the
Reference and Value fields of a symbol, a pin name or number) beyond its anchor point, the size of an
image, hidden or off-grid detail inside a library symbol that the sheet does not embed, and the drawing
sheet's own frame (the page border margin).

An object that cannot be measured (a kind this module does not know, or a known kind with a missing or
unreadable coordinate) is returned with ``measured == "none"`` and no box; a caller must treat that as
"cannot classify", never as "inside the page".
"""

from __future__ import annotations

import math
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import sheet_editing as se
from boardproof.raw_geometry import arc_extent
from boardproof.sheet_align import symbol_box

Box = tuple[float, float, float, float]

# KiCad's named paper sizes in mm, landscape (width, height). Measured: kicad-cli 10.0.7 SVG export.
PAPER_MM: dict[str, tuple[float, float]] = {
    "A5": (210.0, 148.0), "A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0), "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8), "D": (863.6, 558.8),
    "E": (1117.6, 863.6), "USLetter": (279.4, 215.9), "USLegal": (355.6, 215.9), "USLedger": (431.8, 279.4),
}
USER_MIN_MM = 25.4     # KiCad clamps a User page side to 1 inch ...
USER_MAX_MM = 3048.0   # ... .. 120 inches
_LOWER = {name.lower(): name for name in PAPER_MM}

# top-level nodes that are no drawing object
QUIET = frozenset({"version", "generator", "generator_version", "uuid", "paper", "title_block", "lib_symbols",
                   "sheet_instances", "symbol_instances", "embedded_fonts", "embedded_files", "bus_alias", "group"})
NOT_MEASURED = (
    "the extent of any text beyond its anchor point (labels, notes, symbol Reference and Value fields, pin names and numbers)",
    "the size of images",
    "the rotation of text boxes",
    "the drawing sheet's own border margin",
    "a bezier curve counts by its control points, so the box can be slightly too large",
)


def canonical_name(name: str) -> str | None:
    """KiCad's spelling of a named paper size (it reads any letter case), None for a name it does not know."""
    return _LOWER.get(name.lower()) if isinstance(name, str) else None


def page_size_mm(paper: SExp | None) -> tuple[float, float] | None:
    """Width and height in mm of a ``(paper ...)`` node, or of a ``kicad_sch`` root (its paper node);
    None when there is none or it cannot be read. A User size is returned as stored (KiCad rounds to a
    mil and clamps to 25.4 .. 3048 mm; a stored value outside that is clamped here the same way)."""
    if paper is None:
        return None
    if paper.name == "kicad_sch":
        found = paper.find_children("paper")
        if len(found) != 1:
            return None
        paper = found[0]
    atoms = [a.value for a in paper.children if a.is_atom]
    if not atoms or not isinstance(atoms[0], str):
        return None
    name = atoms[0]
    if name.lower() == "user":
        tail = atoms[3:]
        if len(atoms) < 3 or tail not in ([], ["portrait"]) or any(isinstance(a, bool) or not isinstance(a, (int, float)) for a in atoms[1:3]):
            return None
        w, h = (min(max(float(v), USER_MIN_MM), USER_MAX_MM) for v in atoms[1:3])
        return (h, w) if tail else (w, h)
    canon = canonical_name(name)
    if canon is None:
        return None
    if atoms[1:] not in ([], ["portrait"]):
        return None     # KiCad cannot load any other word or a number after a named size (measured)
    w, h = PAPER_MM[canon]
    return (h, w) if atoms[1:] else (w, h)


# --------------------------------------------------------------------------- the objects


def _box(points: list[tuple[float, float]]) -> Box | None:
    if not points or not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for p in points for v in p):
        return None     # a missing, non-numeric or non-finite coordinate: not measurable
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _xy(node: SExp, tag: str) -> tuple[float, float] | None:
    found = node.find_children(tag)
    if not found or len(found[0].children) < 2:
        return None
    return found[0].get_float(0), found[0].get_float(1)


def _pts(node: SExp) -> list[tuple[float, float]]:
    holder = node.find_children("pts")
    return [(p.get_float(0), p.get_float(1)) for p in holder[0].find_children("xy")] if holder else []


def _need(*values: Any) -> bool:
    return all(v is not None for v in values)


def _measure(root: SExp, node: SExp) -> tuple[Box | None, str]:
    """(box, how) of one top-level node; (None, "none") when it cannot be measured."""
    name = node.name
    if name == "symbol":
        if not node.find_children("lib_id"):
            return None, "none"
        x, y, _a = se._at(node)
        pins = [(p["x"], p["y"]) for p in se.pin_positions(root, node)]     # every pin, hidden ones too (with_hidden only adds a flag)
        body = symbol_box(root, node)
        points = [(x, y), *pins]
        if body is not None:
            points += [(body[0], body[1]), (body[2], body[3])]
            return _box(points), "symbol_body_and_pins"
        return _box(points), ("symbol_pins" if pins else "origin")
    if name in ("wire", "bus", "polyline", "bezier", "rule_area"):
        pts = _pts(node)
        return (_box(pts), "points") if pts else (None, "none")
    if name in ("rectangle", "text_box", "sheet"):
        if name == "rectangle":
            a, b = _xy(node, "start"), _xy(node, "end")
            return (_box([a, b]), "points") if _need(a, b) else (None, "none")
        at, size = _xy(node, "at"), _xy(node, "size")
        if not _need(at, size):
            return None, "none"
        return _box([at, (at[0] + size[0], at[1] + size[1])]), "points"
    if name == "bus_entry":
        at, size = _xy(node, "at"), _xy(node, "size")
        return (_box([at, (at[0] + size[0], at[1] + size[1])]), "points") if _need(at, size) else (None, "none")
    if name == "circle":
        c, r = _xy(node, "center"), node.find_children("radius")
        if not _need(c) or not r:
            return None, "none"
        rad = abs(r[0].get_float(0))
        if rad is None:
            return None, "none"
        return _box([(c[0] - rad, c[1] - rad), (c[0] + rad, c[1] + rad)]), "points"
    if name == "arc":
        a, m, b = _xy(node, "start"), _xy(node, "mid"), _xy(node, "end")
        if not _need(a, m, b):
            return None, "none"
        return _box(arc_extent(a, m, b) or [a, m, b]), "points"
    if name in ("junction", "no_connect", "label", "global_label", "hierarchical_label", "text", "netclass_flag", "image", "directive_label"):
        at = _xy(node, "at")
        return (_box([at]), "origin") if at else (None, "none")
    return None, "none"


def _label(node: SExp) -> str:
    if node.name == "symbol":
        return se._ref(node) or (node.find_children("lib_id")[0].get_string(0) or "")
    if node.name == "sheet":
        for prop in node.find_children("property"):
            if prop.get_string(0) in ("Sheetname", "Sheet name"):
                return prop.get_string(1) or ""
        return ""
    if node.name in ("label", "global_label", "hierarchical_label", "directive_label", "text", "text_box"):
        text = node.get_string(0) or ""
        return text if len(text) <= 60 else text[:57] + "..."
    return ""


def sheet_extents(root: SExp) -> list[dict[str, Any]]:
    """One entry per drawing object (a direct child of ``root`` that is not in QUIET), in file order:
    ``kind``, ``uuid``, ``label`` (reference, text or sheet name), ``x``, ``y`` (the top left corner of
    the box), ``x1``, ``y1``, ``x2``, ``y2`` (the box, mm, rounded to 4 decimals) and ``measured`` (see
    the module text). An object that cannot be measured has ``measured == "none"`` and no box."""
    out: list[dict[str, Any]] = []
    for node in root.children:
        if node.is_atom or node.name in QUIET:
            continue
        try:
            box, how = _measure(root, node)
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            box, how = None, "none"
        try:
            label = _label(node)
        except (TypeError, ValueError, IndexError, KeyError, AttributeError):
            label = ""
        entry: dict[str, Any] = {"kind": node.name, "uuid": se._uuid(node), "label": label}
        if box is None:
            entry.update(x=None, y=None, x1=None, y1=None, x2=None, y2=None, measured="none")
        else:
            x1, y1, x2, y2 = (round(v, 4) for v in box)
            entry.update(x=x1, y=y1, x1=x1, y1=y1, x2=x2, y2=y2, measured=how)
        out.append(entry)
    return out


def union_box(entries: list[dict[str, Any]]) -> Box | None:
    """The box around every measured entry, None when none was measured."""
    boxes = [e for e in entries if e.get("x1") is not None]
    if not boxes:
        return None
    return (min(e["x1"] for e in boxes), min(e["y1"] for e in boxes), max(e["x2"] for e in boxes), max(e["y2"] for e in boxes))


def outside_page(entries: list[dict[str, Any]], page: tuple[float, float], *, tol: float = se.TOL) -> list[dict[str, Any]]:
    """The measured entries whose box is not wholly inside the page [0, width] x [0, height]."""
    w, h = page
    return [e for e in entries if e.get("x1") is not None
            and (e["x1"] < -tol or e["y1"] < -tol or e["x2"] > w + tol or e["y2"] > h + tol)]
