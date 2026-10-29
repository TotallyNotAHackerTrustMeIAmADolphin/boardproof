"""Grade a schematic sheet against stated readability rules (build ticket 10, spice-simulation).

A read-only checker. It never writes a file. Every position, size and extent here is read from
the sheet's own S-expression tokens (``(at ...)``, ``(pts (xy ...))``, ``(size ...)``,
``(effects (font (size ...)))``, the ``lib_symbols`` graphics and pins), never from a
``kicad_tools`` convenience property (CLAUDE.md hard rule 2; the parse is the low-level
``SExp`` tree from ``write_layer.load``). Frame: sheet millimetres, x right, y down; library
symbols are y up and are placed as ``X = ax + x'``, ``Y = ay - y'`` after the symbol's rotation
and mirror (rotate first, then mirror, KiCad's ``SCH_SYMBOL::SetOrientation`` composition; no
checked-in sheet has a mirrored symbol at 90 or 270 degrees, so that one case is unproven).

The rules (``RULES``), each reported per offender with its sheet, a position and a fix:

``symbol_overlap``
    The bodies of two placed symbols (or sheet symbols) overlap by more than ``OVERLAP_MM`` in
    both axes. A body is the symbol's own graphics plus the inner end of each pin for the placed
    unit and body style, so pin stubs that meet at a shared wire end are not an overlap. Power
    symbols are skipped (a power symbol sits on a pin by design).
``text_overlap``
    Two visible texts (symbol properties, net labels, free text) overlap, or a visible property
    text lies inside the body of another symbol. Text boxes are *estimates*: width is
    ``characters * size * TEXT_ADVANCE`` (punctuation counts half; KiCad's stroke font has no
    fixed advance, and 0.6 keeps the hand-laid demo sheets' own Reference and Value pairs from
    reporting), height is ``size`` per line, and the Footprint, Datasheet and Description properties are not graded; a pair is
    reported only when the boxes overlap by more than ``TEXT_OVERLAP_MM`` in both axes, so a
    near miss is not reported.
``wire_not_orthogonal``
    A wire is neither horizontal nor vertical.
``wire_off_grid``
    A wire end is not on the grid (``grid_mm``, default 1.27 mm = 50 mil, origin at the sheet
    origin).
``net_unlabelled_between_blocks``
    A net joins pins of two or more different symbols (sheet symbols count), its pins span more
    than ``long_net_mm`` (Manhattan width plus height of their bounding box, default 50.8 mm)
    and it carries no label (local, global or hierarchical) and no power symbol. Such a net is
    "between blocks": it can only be followed by tracing wire. Connectivity here is wires,
    pins, labels and junctions by coordinates (a point on a wire segment is on that wire); it
    is a readability aid, not the netlist, which only ``kicad-cli`` exports.
``value_not_visible``
    A symbol's Value property is hidden or empty (power symbols and ``#`` references exempt).
``model_name_not_visible``
    A symbol carries a simulation model name (``Sim.Name`` or ``Spice_Model``) that is shown
    neither by that property nor by a visible Value with the same text.

Whether a sheet "passes" is only: no violation. The numbers are a baseline, not a target
(``docs/TESTING.md`` and the ticket): run ``python -m kicad_mcp.schematic_readability <root>`` to
print them for a project or a directory of projects.
"""
from __future__ import annotations

import math
import sys
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

Point = tuple[float, float]
Box = tuple[float, float, float, float]  # x0, y0, x1, y1 with x0 <= x1, y0 <= y1

RULES = (
    "symbol_overlap",
    "text_overlap",
    "wire_not_orthogonal",
    "wire_off_grid",
    "net_unlabelled_between_blocks",
    "value_not_visible",
    "model_name_not_visible",
)

DEFAULT_GRID_MM = 1.27
DEFAULT_LONG_NET_MM = 50.8
OVERLAP_MM = 0.1  # body boxes must overlap by more than this in both axes
TEXT_OVERLAP_MM = 0.5  # the same for text boxes (estimated, so more slack)
TEXT_ADVANCE = 0.6  # estimated character advance as a fraction of the font size
GRID_TOLERANCE_MM = 0.005
ORTHO_TOLERANCE_MM = 0.001
_POINT_KEY_MM = 0.01
_NARROW = ".,:;'|! "  # counted as half a character
_QUIET_PROPERTIES = ("Footprint", "Datasheet", "Description")  # shown rarely and tiny: not graded
_MODEL_NAME_PROPERTIES = ("Sim.Name", "Spice_Model")


class ReadabilityError(ValueError):
    """A token the checker needs is malformed; the message names it."""


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str  # what is wrong, where, and what to change
    sheet: str  # the sheet file's name
    at_mm: Point | None  # sheet frame, y down
    refs: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "rule": self.rule,
            "message": self.message,
            "sheet": self.sheet,
            "at_mm": list(self.at_mm) if self.at_mm is not None else None,
            "refs": list(self.refs),
        }


@dataclass(frozen=True)
class SheetReport:
    sheet: str
    violations: tuple[Violation, ...]
    symbols: int
    wires: int

    @property
    def passed(self) -> bool:
        return not self.violations

    def by_rule(self) -> dict[str, int]:
        counts = Counter(v.rule for v in self.violations)
        return {rule: counts.get(rule, 0) for rule in RULES}

    def as_dict(self) -> dict[str, Any]:
        return {
            "sheet": self.sheet,
            "passed": self.passed,
            "symbols": self.symbols,
            "wires": self.wires,
            "by_rule": self.by_rule(),
            "violations": [v.as_dict() for v in self.violations],
        }


@dataclass(frozen=True)
class ProjectReport:
    sheets: tuple[SheetReport, ...]
    problems: tuple[str, ...] = ()  # sheet-tree problems (a missing or unparseable sheet)

    @property
    def passed(self) -> bool:
        return not self.problems and all(s.passed for s in self.sheets)

    def by_rule(self) -> dict[str, int]:
        total: Counter[str] = Counter()
        for sheet in self.sheets:
            total.update(sheet.by_rule())
        return {rule: total.get(rule, 0) for rule in RULES}

    def as_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "problems": list(self.problems),
            "sheets": [s.as_dict() for s in self.sheets],
            "by_rule": self.by_rule(),
        }


# ---------------------------------------------------------------------------------------------
# Token readers
# ---------------------------------------------------------------------------------------------


def _is_node(value: Any) -> bool:
    return hasattr(value, "children") and hasattr(value, "name")


def _atoms(node: Any) -> list[Any]:
    """The atom values of ``node`` (its nested nodes left out), in order."""
    return [v for v in node.values if not _is_node(v)]


def _num(value: Any, what: str) -> float:
    try:
        number = float(value) if not isinstance(value, bool) else math.nan
    except (TypeError, ValueError):
        number = math.nan
    if not math.isfinite(number):
        raise ReadabilityError(f"{what}: {value!r} is not a finite number")
    return number


def _pair(node: Any, name: str, what: str) -> Point:
    child = node.find(name)
    values = _atoms(child) if child is not None else []
    if len(values) < 2:
        raise ReadabilityError(f"{what} has no ({name} x y)")
    return _num(values[0], f"{what} {name} x"), _num(values[1], f"{what} {name} y")


def _at(node: Any, what: str) -> tuple[float, float, float]:
    child = node.find("at")
    values = _atoms(child) if child is not None else []
    if len(values) < 2:
        raise ReadabilityError(f"{what} has no (at x y)")
    angle = _num(values[2], f"{what} angle") if len(values) > 2 else 0.0
    return _num(values[0], f"{what} x"), _num(values[1], f"{what} y"), angle


def _yes(node: Any, name: str, *, bare: bool = True) -> bool:
    """``(name yes)`` or a bare ``name`` atom directly in ``node`` (KiCad 6/7 wrote ``hide``)."""
    child = node.find(name)
    if child is not None:
        values = _atoms(child)
        return not values or str(values[0]) != "no"
    return bare and name in [v for v in _atoms(node) if isinstance(v, str)]


def _str(node: Any, index: int = 0) -> str:
    values = _atoms(node)
    return str(values[index]) if len(values) > index else ""


def _fmt(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") if value else "0"


def _at_text(point: Point) -> str:
    return f"({_fmt(point[0])}, {_fmt(point[1])})"


# ---------------------------------------------------------------------------------------------
# Geometry
# ---------------------------------------------------------------------------------------------


def _bbox(points: Iterable[Point]) -> Box | None:
    pts = list(points)
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _penetration(a: Box, b: Box) -> tuple[float, float]:
    """How far the boxes overlap in x and y (negative or zero: they do not)."""
    return min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])


def _overlaps(a: Box, b: Box, tolerance: float) -> bool:
    px, py = _penetration(a, b)
    return px > tolerance and py > tolerance


def _rotate(x: float, y: float, angle_deg: float) -> Point:
    """Rotate CCW in a y-up frame."""
    angle = math.radians(angle_deg)
    c, s = round(math.cos(angle), 12), round(math.sin(angle), 12)
    return x * c - y * s, x * s + y * c


def _place(local: Point, at: tuple[float, float], angle: float, mirror: str) -> Point:
    """A library (y up) point on the sheet (y down): rotate, then mirror, then flip y."""
    x, y = _rotate(local[0], local[1], angle)
    if mirror == "x":
        y = -y
    elif mirror == "y":
        x = -x
    return at[0] + x, at[1] - y


def _on_segment(point: Point, a: Point, b: Point, tolerance: float = _POINT_KEY_MM) -> bool:
    (px, py), (ax, ay), (bx, by) = point, a, b
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    if length2 == 0:
        return math.hypot(px - ax, py - ay) <= tolerance
    t = ((px - ax) * dx + (py - ay) * dy) / length2
    if t < -tolerance or t > 1 + tolerance:
        return False
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy)) <= tolerance


def _key(point: Point) -> tuple[int, int]:
    return round(point[0] / _POINT_KEY_MM), round(point[1] / _POINT_KEY_MM)


# ---------------------------------------------------------------------------------------------
# Library symbols
# ---------------------------------------------------------------------------------------------


@dataclass
class _LibShape:
    graphics: list[Point] = field(default_factory=list)  # y up, library frame
    pins: list[tuple[str, Point]] = field(default_factory=list)  # (number, connection point)
    power: bool = False


def _unit_style(name: str) -> tuple[int, int] | None:
    parts = name.rsplit("_", 2)
    if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
        return int(parts[1]), int(parts[2])
    return None


def _graphic_points(item: Any) -> list[Point]:
    name = item.name
    try:
        if name == "rectangle":
            return [_pair(item, "start", "rectangle"), _pair(item, "end", "rectangle")]
        if name in ("polyline", "bezier"):
            pts = item.find("pts")
            return [(_num(_atoms(xy)[0], "xy x"), _num(_atoms(xy)[1], "xy y"))
                    for xy in (pts.find_children("xy") if pts is not None else [])]
        if name == "circle":
            cx, cy = _pair(item, "center", "circle")
            radius = _num(_atoms(item.find("radius"))[0], "circle radius")
            return [(cx - radius, cy - radius), (cx + radius, cy + radius)]
        if name == "arc":
            return [_pair(item, n, "arc") for n in ("start", "mid", "end")]
    except (IndexError, AttributeError) as exc:
        raise ReadabilityError(f"malformed library {name}: {exc}") from exc
    return []


def _lib_shape(lib: Any, unit: int, style: int) -> _LibShape:
    shape = _LibShape(power=lib.find("power") is not None)
    parts = [lib]
    for sub in lib.find_children("symbol"):
        us = _unit_style(_str(sub))
        if us is None or (us[0] in (0, unit) and us[1] in (0, style)):
            parts.append(sub)
    for part in parts:
        for item in part.children:
            if item.is_atom:
                continue
            shape.graphics.extend(_graphic_points(item))
            if item.name == "pin":
                ax, ay, ang = _at(item, "library pin")
                length_node = item.find("length")
                length = _num(_atoms(length_node)[0], "pin length") if length_node is not None else 0.0
                number_node = item.find("number")
                number = _str(number_node) if number_node is not None else ""
                shape.pins.append((number, (ax, ay)))
                inner = _rotate(length, 0.0, ang)  # the pin points from its end toward the body
                shape.graphics.append((ax + inner[0], ay + inner[1]))
    return shape


# ---------------------------------------------------------------------------------------------
# Sheet model
# ---------------------------------------------------------------------------------------------


@dataclass
class _Text:
    kind: str  # "property", "label", "text"
    owner: str  # the symbol reference, or ""
    text: str
    box: Box
    at: Point
    name: str = ""  # the property name


@dataclass
class _Body:
    ref: str
    lib_id: str
    box: Box | None
    at: Point
    power: bool


def _font_size(effects: Any | None) -> Point:
    font = effects.find("font") if effects is not None else None
    size = font.find("size") if font is not None else None
    values = _atoms(size) if size is not None else []
    if len(values) >= 2:
        return _num(values[0], "font width"), _num(values[1], "font height")
    return 1.27, 1.27


def _justify(effects: Any | None) -> tuple[str, str]:
    horizontal, vertical = "center", "center"
    justify = effects.find("justify") if effects is not None else None
    for word in (_atoms(justify) if justify is not None else []):
        if word in ("left", "right"):
            horizontal = word
        elif word in ("top", "bottom"):
            vertical = word
    return horizontal, vertical


def _text_box(text: str, at: tuple[float, float, float], size: Point, horizontal: str, vertical: str,
              *, above: bool = False) -> Box:
    """The estimated box of ``text``: see the module doc. ``above`` puts it on the upper side of
    its anchor (a label sits above the wire it names)."""
    lines = text.split("\n") or [""]
    width = max(sum(0.5 if c in _NARROW else 1.0 for c in line) for line in lines) * size[0] * TEXT_ADVANCE
    height = size[1] * len(lines)
    x, y, angle = at
    angle %= 360.0
    if angle >= 180.0:  # KiCad draws text upright: turn it half a turn and swap the justification
        angle -= 180.0
        horizontal = {"left": "right", "right": "left"}.get(horizontal, horizontal)
    u0, u1 = {"left": (0.0, width), "right": (-width, 0.0)}.get(horizontal, (-width / 2, width / 2))
    if above:
        v0, v1 = -height, 0.0
    else:
        v0, v1 = {"top": (0.0, height), "bottom": (-height, 0.0)}.get(vertical, (-height / 2, height / 2))
    c, s = round(math.cos(math.radians(angle)), 12), round(math.sin(math.radians(angle)), 12)
    corners = [(x + u * c + v * s, y - u * s + v * c) for u in (u0, u1) for v in (v0, v1)]
    box = _bbox(corners)
    assert box is not None
    return box


@dataclass
class _Sheet:
    name: str
    bodies: list[_Body] = field(default_factory=list)
    texts: list[_Text] = field(default_factory=list)
    wires: list[tuple[Point, Point]] = field(default_factory=list)
    pins: list[tuple[str, str, Point, bool]] = field(default_factory=list)  # ref, number, at, power
    labels: list[Point] = field(default_factory=list)
    junctions: list[Point] = field(default_factory=list)
    value_problems: list[Violation] = field(default_factory=list)
    model_problems: list[Violation] = field(default_factory=list)


def _properties(symbol: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for prop in symbol.find_children("property"):
        values = _atoms(prop)
        if len(values) >= 1:
            out[str(values[0])] = prop
    return out


def _prop_visible(prop: Any) -> bool:
    effects = prop.find("effects")
    return not (_yes(prop, "hide", bare=False) or (effects is not None and _yes(effects, "hide")))


def _prop_text(prop: Any) -> str:
    values = _atoms(prop)
    return str(values[1]) if len(values) > 1 else ""


def _read_sheet(root: Any, name: str) -> _Sheet:
    if getattr(root, "name", None) != "kicad_sch":
        raise ReadabilityError(f"not a kicad_sch root: {getattr(root, 'name', root)!r}")
    sheet = _Sheet(name=name)
    libs: dict[str, Any] = {}
    lib_node = root.find("lib_symbols")
    for lib in (lib_node.find_children("symbol") if lib_node is not None else []):
        libs[_str(lib)] = lib
    shapes: dict[tuple[str, int, int], _LibShape] = {}

    for symbol in root.find_children("symbol"):
        lib_name = symbol.find("lib_name")
        lib_id_node = symbol.find("lib_id")
        lib_id = _str(lib_id_node) if lib_id_node is not None else ""
        key = _str(lib_name) if lib_name is not None else lib_id
        props = _properties(symbol)
        ref = _prop_text(props["Reference"]) if "Reference" in props else ""
        where = f"symbol {ref or lib_id}"
        at = _at(symbol, where)
        mirror_node = symbol.find("mirror")
        mirror = _str(mirror_node) if mirror_node is not None else ""
        unit_node, style_node = symbol.find("unit"), symbol.find("body_style")
        unit = int(_num(_atoms(unit_node)[0], f"{where} unit")) if unit_node is not None and _atoms(unit_node) else 1
        style = int(_num(_atoms(style_node)[0], f"{where} body_style")) if style_node is not None and _atoms(style_node) else 1
        lib = libs.get(key) or libs.get(lib_id)
        if lib is None:
            raise ReadabilityError(f"{where}: library symbol {key!r} is not in lib_symbols")
        shape = shapes.setdefault((key, unit, style), _lib_shape(lib, unit, style))
        power = shape.power or lib_id.startswith("power:") or ref.startswith("#")
        box = _bbox(_place(p, (at[0], at[1]), at[2], mirror) for p in shape.graphics)
        sheet.bodies.append(_Body(ref, lib_id, box, (at[0], at[1]), power))
        for number, local in shape.pins:
            sheet.pins.append((ref, number, _place(local, (at[0], at[1]), at[2], mirror), power))
        for pname, prop in props.items():
            if pname in _QUIET_PROPERTIES or not _prop_visible(prop) or not _prop_text(prop).strip():
                continue
            p_at = _at(prop, f"{where} property {pname}")
            effects = prop.find("effects")
            h, v = _justify(effects)
            sheet.texts.append(_Text("property", ref, _prop_text(prop),
                                     _text_box(_prop_text(prop), p_at, _font_size(effects), h, v),
                                     (p_at[0], p_at[1]), pname))
        if not power:
            _check_value(sheet, ref or lib_id, at, props)
            _check_model(sheet, ref or lib_id, at, props)

    for node in root.find_children("sheet"):
        _read_sheet_symbol(sheet, node)

    for wire in root.find_children("wire"):
        pts = wire.find("pts")
        xy = pts.find_children("xy") if pts is not None else []
        if len(xy) != 2:
            raise ReadabilityError("a wire without exactly two (xy x y) points")
        a = (_num(_atoms(xy[0])[0], "wire x"), _num(_atoms(xy[0])[1], "wire y"))
        b = (_num(_atoms(xy[1])[0], "wire x"), _num(_atoms(xy[1])[1], "wire y"))
        sheet.wires.append((a, b))
    for junction in root.find_children("junction"):
        sheet.junctions.append(_at(junction, "junction")[:2])
    for tag in ("label", "global_label", "hierarchical_label"):
        for label in root.find_children(tag):
            text = _str(label)
            l_at = _at(label, f"{tag} {text!r}")
            sheet.labels.append((l_at[0], l_at[1]))
            effects = label.find("effects")
            h, _v = _justify(effects)
            sheet.texts.append(_Text("label", "", text,
                                     _text_box(text, l_at, _font_size(effects), "left" if h == "center" else h,
                                               "center", above=True), (l_at[0], l_at[1]), tag))
    for free in root.find_children("text"):
        text = _str(free)
        if not text.strip():
            continue
        f_at = _at(free, f"text {text!r}")
        effects = free.find("effects")
        h, v = _justify(effects)
        sheet.texts.append(_Text("text", "", text,
                                 _text_box(text, f_at, _font_size(effects), "left" if h == "center" else h,
                                           "bottom" if v == "center" else v), (f_at[0], f_at[1]), "text"))
    return sheet


def _read_sheet_symbol(sheet: _Sheet, node: Any) -> None:
    at = _at(node, "sheet")
    size = _pair(node, "size", "sheet")
    props = _properties(node)
    name = _prop_text(props["Sheetname"]) if "Sheetname" in props else (
        _prop_text(props["Sheet name"]) if "Sheet name" in props else "")
    box = (at[0], at[1], at[0] + size[0], at[1] + size[1])
    sheet.bodies.append(_Body(name or "<sheet>", "sheet", box, (at[0], at[1]), False))
    for pin in node.find_children("pin"):
        p_at = _at(pin, f"sheet {name!r} pin")
        sheet.pins.append((name or "<sheet>", _str(pin), (p_at[0], p_at[1]), False))
    for pname, prop in props.items():
        if not _prop_visible(prop) or not _prop_text(prop).strip():
            continue
        p_at = _at(prop, f"sheet {name!r} property {pname}")
        effects = prop.find("effects")
        h, v = _justify(effects)
        sheet.texts.append(_Text("property", name, _prop_text(prop),
                                 _text_box(_prop_text(prop), p_at, _font_size(effects), h, v),
                                 (p_at[0], p_at[1]), pname))


def _check_value(sheet: _Sheet, ref: str, at: tuple[float, float, float], props: dict[str, Any]) -> None:
    where = (at[0], at[1])
    value = props.get("Value")
    if value is None or not _prop_text(value).strip():
        sheet.value_problems.append(Violation(
            "value_not_visible",
            f"{ref} at {_at_text(where)} has no Value text; set one (for example 10k or the part "
            f"name) and show it, so the sheet says what the part is",
            sheet.name, where, (ref,)))
    elif not _prop_visible(value):
        sheet.value_problems.append(Violation(
            "value_not_visible",
            f"{ref} at {_at_text(where)} hides its Value '{_prop_text(value)}'; show the Value "
            f"property (clear 'Hide' in the symbol's property list), so the sheet says what the part is",
            sheet.name, where, (ref,)))


def _check_model(sheet: _Sheet, ref: str, at: tuple[float, float, float], props: dict[str, Any]) -> None:
    where = (at[0], at[1])
    value = props.get("Value")
    value_shown = value is not None and _prop_visible(value)
    for pname in _MODEL_NAME_PROPERTIES:
        prop = props.get(pname)
        if prop is None or not _prop_text(prop).strip():
            continue
        model = _prop_text(prop)
        if _prop_visible(prop) or (value_shown and _prop_text(value).strip() == model.strip()):
            continue
        sheet.model_problems.append(Violation(
            "model_name_not_visible",
            f"{ref} at {_at_text(where)} uses simulation model '{model}' ({pname}) but shows it "
            f"nowhere; show the {pname} property, or make the Value text '{model}', so a reader "
            f"can tell which model the simulation runs",
            sheet.name, where, (ref,)))
        return


# ---------------------------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------------------------


def _rule_wires(sheet: _Sheet, grid: float) -> list[Violation]:
    out: list[Violation] = []
    for a, b in sheet.wires:
        dx, dy = b[0] - a[0], b[1] - a[1]
        if abs(dx) > ORTHO_TOLERANCE_MM and abs(dy) > ORTHO_TOLERANCE_MM:
            corner = (b[0], a[1])
            out.append(Violation(
                "wire_not_orthogonal",
                f"wire {_at_text(a)} to {_at_text(b)} is diagonal (dx {_fmt(dx)} mm, dy {_fmt(dy)} mm); "
                f"redraw it as horizontal and vertical segments, for example through {_at_text(corner)}",
                sheet.name, a))
        off = [p for p in (a, b)
               if any(abs(c / grid - round(c / grid)) * grid > GRID_TOLERANCE_MM for c in p)]
        if off:
            nearest = ", ".join(_at_text((round(p[0] / grid) * grid, round(p[1] / grid) * grid)) for p in off)
            out.append(Violation(
                "wire_off_grid",
                f"wire {_at_text(a)} to {_at_text(b)} has "
                f"{'an end' if len(off) == 1 else 'both ends'} off the {_fmt(grid)} mm grid: "
                f"{', '.join(_at_text(p) for p in off)}; move to {nearest}",
                sheet.name, off[0]))
    return out


def _rule_symbol_overlap(sheet: _Sheet) -> list[Violation]:
    out: list[Violation] = []
    bodies = [b for b in sheet.bodies if b.box is not None and not b.power]
    for i, first in enumerate(bodies):
        for second in bodies[i + 1:]:
            assert first.box is not None and second.box is not None
            if _overlaps(first.box, second.box, OVERLAP_MM):
                px, py = _penetration(first.box, second.box)
                out.append(Violation(
                    "symbol_overlap",
                    f"{first.ref or first.lib_id} and {second.ref or second.lib_id} overlap by "
                    f"{_fmt(px)} x {_fmt(py)} mm near {_at_text(second.at)}; move one of them "
                    f"apart (their bodies are {_fmt_box(first.box)} and {_fmt_box(second.box)})",
                    sheet.name, second.at, (first.ref, second.ref)))
    return out


def _fmt_box(box: Box) -> str:
    return f"[{_fmt(box[0])},{_fmt(box[1])} .. {_fmt(box[2])},{_fmt(box[3])}]"


def _label_of(text: _Text) -> str:
    where = text.owner
    if text.kind == "property":
        return f"{where} {text.name} '{text.text}'" if where else f"{text.name} '{text.text}'"
    if text.kind == "label":
        return f"label '{text.text}'"
    return f"text '{text.text[:24]}'"


def _rule_text_overlap(sheet: _Sheet) -> list[Violation]:
    out: list[Violation] = []
    texts = sheet.texts
    for i, first in enumerate(texts):
        for second in texts[i + 1:]:
            if _overlaps(first.box, second.box, TEXT_OVERLAP_MM):
                out.append(Violation(
                    "text_overlap",
                    f"{_label_of(first)} overlaps {_label_of(second)} near {_at_text(second.at)}; "
                    f"move one of the two texts (the boxes are estimated from the font size, so "
                    f"a near miss is not reported)",
                    sheet.name, second.at, tuple(r for r in (first.owner, second.owner) if r)))
    for text in texts:
        if text.kind != "property":
            continue
        for body in sheet.bodies:
            if body.box is None or body.power or body.ref == text.owner:
                continue
            if _overlaps(text.box, body.box, TEXT_OVERLAP_MM):
                out.append(Violation(
                    "text_overlap",
                    f"{_label_of(text)} lies on top of {body.ref or body.lib_id} near "
                    f"{_at_text(text.at)}; move the text clear of that symbol",
                    sheet.name, text.at, tuple(r for r in (text.owner, body.ref) if r)))
    return out


_CELL_MM = 5.08


class _Nets:
    """Union-find over wires and the points that touch them. A point on a wire segment (within
    ``_POINT_KEY_MM``) is on that wire; wires are bucketed in ``_CELL_MM`` cells so a big sheet
    is not quadratic."""

    def __init__(self, sheet: _Sheet) -> None:
        self.parent = list(range(len(sheet.wires)))
        self.sheet = sheet
        self.cells: dict[tuple[int, int], list[int]] = {}
        for index, (a, b) in enumerate(sheet.wires):
            x0, x1 = sorted((a[0], b[0]))
            y0, y1 = sorted((a[1], b[1]))
            for cx in range(math.floor((x0 - _POINT_KEY_MM) / _CELL_MM), math.floor((x1 + _POINT_KEY_MM) / _CELL_MM) + 1):
                for cy in range(math.floor((y0 - _POINT_KEY_MM) / _CELL_MM), math.floor((y1 + _POINT_KEY_MM) / _CELL_MM) + 1):
                    self.cells.setdefault((cx, cy), []).append(index)
        # every wire end, pin, label and junction joins each wire it lies on (a wire end on
        # another wire's body joins the two, as KiCad connects them)
        probes = [(p, index) for index, (a, b) in enumerate(sheet.wires) for p in (a, b)]
        probes += [(p, None) for p in [q[2] for q in sheet.pins] + sheet.labels + sheet.junctions]
        for point, own in probes:
            for index in self.wires_at(point):
                if own is not None:
                    self.union(own, index)
        by_point: dict[tuple[int, int], int] = {}
        for point, _own in probes:  # points at one coordinate are one node
            on = self.wires_at(point)
            if on:
                for index in on:
                    self.union(by_point.setdefault(_key(point), on[0]), index)

    def wires_at(self, p: Point) -> list[int]:
        cell = (math.floor(p[0] / _CELL_MM), math.floor(p[1] / _CELL_MM))
        return [i for i in self.cells.get(cell, ()) if _on_segment(p, *self.sheet.wires[i])]

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra

    def wire_at(self, p: Point) -> int | None:
        on = self.wires_at(p)
        return self.find(on[0]) if on else None


def _rule_unlabelled(sheet: _Sheet, long_net: float) -> list[Violation]:
    nets = _Nets(sheet)
    members: dict[int, list[tuple[str, str, Point, bool]]] = {}
    for pin in sheet.pins:
        net = nets.wire_at(pin[2])
        if net is not None:
            members.setdefault(net, []).append(pin)
    labelled = {n for p in sheet.labels if (n := nets.wire_at(p)) is not None}
    wires_of: dict[int, list[tuple[Point, Point]]] = {}
    for index, wire in enumerate(sheet.wires):
        wires_of.setdefault(nets.find(index), []).append(wire)
    out: list[Violation] = []
    for net, pins in members.items():
        real = [p for p in pins if not p[3]]
        if net in labelled or len(real) != len(pins) or len({p[0] for p in real}) < 2:
            continue
        box = _bbox(p[2] for p in real)
        assert box is not None
        span = (box[2] - box[0]) + (box[3] - box[1])
        if span <= long_net:
            continue
        longest = max(wires_of[net], key=lambda w: abs(w[1][0] - w[0][0]) + abs(w[1][1] - w[0][1]))
        mid = ((longest[0][0] + longest[1][0]) / 2, (longest[0][1] + longest[1][1]) / 2)
        names = [f"{p[0]}.{p[1]}" if p[1] else p[0] for p in sorted(real)]
        shown = ", ".join(names[:4]) + (f" (+{len(names) - 4} more)" if len(names) > 4 else "")
        out.append(Violation(
            "net_unlabelled_between_blocks",
            f"the net joining {shown} runs {_fmt(span)} mm between "
            f"{len({p[0] for p in real})} symbols with no label, global label, hierarchical label "
            f"or power symbol; add a net label on it (for example on its wire at {_at_text(mid)}) "
            f"so a reader does not have to follow the wire",
            sheet.name, mid, tuple(sorted({p[0] for p in real}))))
    return out


# ---------------------------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------------------------


def check_sheet(
    sheet: Any,
    *,
    name: str | None = None,
    grid_mm: float = DEFAULT_GRID_MM,
    long_net_mm: float = DEFAULT_LONG_NET_MM,
) -> SheetReport:
    """Grade one sheet: a path to a ``.kicad_sch`` or its parsed ``kicad_sch`` root
    (``write_layer.load``). Raises ``ReadabilityError`` for a token it cannot read."""
    if isinstance(sheet, (str, Path)):
        from kicad_mcp import write_layer

        label = name or Path(sheet).name
        root = write_layer.load(sheet)
    else:
        label, root = name or "<sheet>", sheet
    model = _read_sheet(root, label)
    violations = [
        *_rule_symbol_overlap(model),
        *_rule_text_overlap(model),
        *_rule_wires(model, grid_mm),
        *_rule_unlabelled(model, long_net_mm),
        *model.value_problems,
        *model.model_problems,
    ]
    violations.sort(key=lambda v: (RULES.index(v.rule), v.at_mm or (0.0, 0.0), v.message))
    return SheetReport(label, tuple(violations), len(model.bodies), len(model.wires))


def check_project(root: str | Path, **options: Any) -> ProjectReport:
    """Grade every sheet reached from the root sheet (``sheet_tree.walk``: never a glob)."""
    from kicad_mcp import sheet_tree

    tree = sheet_tree.walk(root)
    reports = [check_sheet(path, name=tree.rel(path), **options) for path in tree.files]
    return ProjectReport(tuple(reports), tree.problems)


def summarise(reports: Sequence[ProjectReport]) -> dict[str, Any]:
    """The baseline figures over projects: sheets, how many pass, violations per rule."""
    sheets = [s for r in reports for s in r.sheets]
    by_rule = Counter()
    failing = Counter()
    for s in sheets:
        for rule, count in s.by_rule().items():
            by_rule[rule] += count
            failing[rule] += 1 if count else 0
    return {
        "projects": len(reports),
        "sheets": len(sheets),
        "sheets_passing": sum(1 for s in sheets if s.passed),
        "violations_by_rule": {rule: by_rule.get(rule, 0) for rule in RULES},
        "sheets_failing_by_rule": {rule: failing.get(rule, 0) for rule in RULES},
    }


def main(argv: Sequence[str] | None = None) -> int:
    """``python -m kicad_mcp.schematic_readability <root.kicad_sch | directory> ...``: print the
    per-rule baseline (a directory is searched for ``*.kicad_sch`` roots, one per project: the
    sheet named like its ``.kicad_pro``)."""
    args = list(sys.argv[1:] if argv is None else argv)
    roots: list[Path] = []
    for arg in args:
        path = Path(arg)
        if path.is_dir():
            roots.extend(sorted(p.with_suffix(".kicad_sch") for p in path.rglob("*.kicad_pro")
                                if p.with_suffix(".kicad_sch").is_file()))
        else:
            roots.append(path)
    reports = [check_project(r) for r in roots]
    for root, report in zip(roots, reports, strict=True):
        for sheet in report.sheets:
            print(f"{root.parent.name}/{sheet.sheet}: {'PASS' if sheet.passed else 'FAIL'} {sheet.by_rule()}")
        for problem in report.problems:
            print(f"{root.parent.name}: sheet-tree problem: {problem}")
    summary = summarise(reports)
    print(f"{summary['sheets_passing']} of {summary['sheets']} sheets in {summary['projects']} projects pass")
    for rule in RULES:
        print(f"  {rule}: {summary['violations_by_rule'][rule]} violations on "
              f"{summary['sheets_failing_by_rule'][rule]} sheets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
