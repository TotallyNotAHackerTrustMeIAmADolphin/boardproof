"""Rule-based arrangement of the Reference and Value texts of placed symbols (GitHub issue 221).

A mode of ``sheet_editing.set_property_positions`` (``arrange=``), not a tool of its own: it plans the new
``(at x y 0)`` and ``(justify ..)`` of each Reference and Value and hands them to the same pipeline (staged
check, write layer, reload verify). Frame: sheet millimetres, x right, y down.

The rule, per placed symbol (power symbols and ``#`` references are skipped):

* the symbol's box is ``sheet_align.symbol_box`` (raw library geometry plus the raw ``(at)`` and mirror, so a
  rotated or mirrored symbol is judged as drawn); a box taller than wide is *vertical*, otherwise *horizontal*;
* vertical: the Reference sits left of the box, right-justified (so its text ends ``gap`` mm from the box), the
  Value right of it, left-justified, both at the box's vertical centre; texts of any length grow away from the part;
* horizontal: the Reference sits above the box, bottom-justified at the horizontal centre, the Value below it,
  top-justified;
* the text is drawn upright: stored angle 0, or 90 on a symbol turned 90 or 270 degrees (KiCad takes the angle relative to
  the symbol), and the stored justify words are the opposite ones where KiCad draws the turn or mirror flipped
  (``_upright_flips``, measured with kicad-cli svg for every turn and mirror); the font and everything else stays;
* a hidden text, and a text pinned with ``(do_not_autoplace)`` (the only lock KiCad 10.0.7 has on a schematic field; a
  symbol cannot be locked), is never moved (listed in ``skipped`` with the reason ``hidden`` or ``locked``).

Text boxes are the estimate of ``sheet_overlaps`` (no font metrics in the file) placed as KiCad draws a symbol's texts
(``drawn_box``, the same placement ``find_overlaps`` uses); a multi-unit symbol is arranged per unit node (which copy KiCad draws is not measured). The result reports the
``text_text`` overlaps of the sheet before and after, and which remain that involve an arranged text.
"""

from __future__ import annotations

from typing import Any

from boardproof import schematic_readability as readability
from boardproof import sheet_editing as se
from boardproof.sheet_authoring import SheetAuthoringError

ARRANGED = ("Reference", "Value")
DEFAULT_GAP_MM = 1.27
SELECTOR_KEYS = {"references", "uuids", "pattern", "region", "lib_id"}
MAX_REMAINING = 20


def _pinned(prop: Any) -> bool:
    """The text carries ``(do_not_autoplace)`` or ``(do_not_autoplace yes)``: the owner pinned it. KiCad 10.0.7 has no
    locked flag on a schematic symbol or field (kicad-cli refuses ``(locked yes)`` on both, measured), so this is the
    only lock the file format has."""
    return any(not c.is_atom and c.name == "do_not_autoplace" and (not c.values or str(c.values[0]) != "no") for c in prop.children)


def _justify_words(prop: Any) -> list[str]:
    effects = prop.find_children("effects")
    return [str(a.value) for e in effects for j in se._kids(e, "justify") for a in j.children]


def check_spec(arrange: Any) -> tuple[dict | None, float]:
    """``(selector or None for the whole sheet, gap)`` of the ``arrange`` argument."""
    if not isinstance(arrange, dict) or set(arrange) - {"selector", "gap"}:
        raise SheetAuthoringError("invalid_spec", "arrange must be an object with optional keys 'selector' and 'gap', got " + repr(arrange))
    selector = arrange.get("selector")
    if selector is not None and (not isinstance(selector, dict) or not selector or set(selector) - SELECTOR_KEYS):
        raise SheetAuthoringError(
            "invalid_spec", f"arrange.selector must be a non-empty object with keys from {sorted(SELECTOR_KEYS)} (leave it out for the whole sheet)")
    gap = arrange.get("gap", DEFAULT_GAP_MM)
    if isinstance(gap, bool) or not se.is_mm(gap) or gap < 0:
        raise SheetAuthoringError("invalid_spec", f"arrange.gap must be a number of mm from 0 (finite, within {se.MAX_MM:g}), got {gap!r}")
    return selector, float(gap)


_OPPOSITE = {"left": "right", "right": "left", "top": "bottom", "bottom": "top"}


def _flip(words: list[str], horizontal: bool, vertical: bool) -> list[str]:
    return [_OPPOSITE[w] if w in _OPPOSITE and (w in ("left", "right") and horizontal or w in ("top", "bottom") and vertical) else w
            for w in words]


def _upright_flips(rotation: float, mirror: str) -> tuple[bool, bool]:
    """(horizontal, vertical): which justification words KiCad draws swapped for an upright text of a symbol turned
    ``rotation`` and mirrored ``mirror``. Measured with kicad-cli 10.0.7 sch export svg over every turn and mirror
    (stored angle 0 on 0 and 180 degrees, 90 on 90 and 270): both swap on 90 and 180 degrees, a mirror about y swaps
    the horizontal word and a mirror about x the vertical one."""
    base = round(rotation) % 360 in (90, 180)
    return base != (mirror == "y"), base != (mirror == "x")


def drawn_box(owner: Any, prop: Any, text: str) -> Any:
    """The box of a symbol's property text as KiCad draws it: the stored angle is relative to the symbol (stored plus
    the symbol's turn is the drawn angle), a text drawn at 180 or 270 degrees has its words swapped, and a mirror
    swaps the vertical (about x) or horizontal (about y) word, along the text for a text drawn vertical (measured with
    kicad-cli svg, see ``_upright_flips``). The size is the estimate of ``sheet_overlaps``. Shared: ``find_overlaps`` places
    every property text of a placed symbol with it (issue 223), so the two never disagree."""
    _sx, _sy, rot = se._at(owner)
    mirror = se._mirror(owner)
    px, py, angle = se._at(prop)
    total = round(angle + rot) % 360
    effects = prop.find("effects")
    size = readability._font_size(effects)
    words = [w for w in _justify_words(prop) if w in ("left", "right", "top", "bottom")]
    both = total in (180, 270)
    vertical_text = total in (90, 270)
    along, across = (mirror == ("x" if vertical_text else "y")), (mirror == ("y" if vertical_text else "x"))
    words = _flip(words, both != along, both != across)
    h = next((w for w in words if w in ("left", "right")), "center")
    v = next((w for w in words if w in ("top", "bottom")), "center")
    return readability._text_box(text, (px, py, 90.0 if vertical_text else 0.0), size, h, v)


def _text_overlaps(root: Any) -> list[tuple[Any, Any]]:
    from boardproof import sheet_overlaps as so

    texts = so._read(root)[1]  # its property boxes are already the drawn ones
    return [(a, b) for i, a in enumerate(texts) for b in texts[i + 1:] if readability._overlaps(a.box, b.box, so.TEXT_MM)]


def _pair(a: Any, b: Any) -> dict[str, Any]:
    return {"items": [a.item(), b.item()], "at": se._round_pt(*b.at)}


def arrange(sheet: Any, root: Any, selector: dict | None, gap: float, outcome: Any, detail: bool) -> tuple[list[dict], dict[str, Any]]:
    """Change the property nodes of the chosen symbols in place; ``(listed entries, report extras)``."""
    from boardproof import sheet_align

    placed = [s for s in sheet.symbols if s.find_children("lib_id")]
    nodes = se.select(sheet, "symbol", [selector]) if selector else placed
    before = len(_text_overlaps(root))
    listed: list[dict] = []
    skipped: list[dict] = []
    touched: set[int] = set()
    for node in nodes:
        ref = se._ref(node)
        lib = se._lib_symbol(root, node)
        if ref.startswith("#") or (lib is not None and lib.find_children("power")):
            continue  # power symbols sit on a pin by design; the selector may still have picked them

        def skip(prop: str, why: str, ref: str = ref, node: Any = node) -> None:
            skipped.append({"reference": ref, "uuid": se._uuid(node), "property": prop, "reason": why})

        box = sheet_align.symbol_box(root, node)
        if box is None:
            skip("Reference and Value", "no_geometry")
            continue
        vertical = (box[3] - box[1]) > (box[2] - box[0])
        # KiCad takes a field's stored angle as relative to a symbol turned 90 or 270 degrees: stored 90 draws upright there
        stored = 90 if round(se._at(node)[2]) % 180 == 90 else 0
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        if vertical:
            target = {"Reference": ((box[0] - gap, cy), ["right"]), "Value": ((box[2] + gap, cy), ["left"])}
        else:
            target = {"Reference": ((cx, box[1] - gap), ["bottom"]), "Value": ((cx, box[3] + gap), ["top"])}
        flips = _upright_flips(se._at(node)[2], se._mirror(node))
        for name in ARRANGED:
            prop = se._property(node, name)
            if prop is None or not prop.find_children("at"):
                skip(name, "no_property")
                continue
            if not readability._prop_visible(prop):
                skip(name, "hidden")
                continue
            if _pinned(prop):
                skip(name, "locked")
                continue
            (x, y), wanted = target[name]
            words = _flip(wanted, *flips)  # what to store so that KiCad draws the wanted side
            x, y = se._r(x), se._r(y)
            px, py, pa = se._at(prop)
            if abs(px - x) <= se.TOL / 10 and abs(py - y) <= se.TOL / 10 and pa % 360 == stored and _justify_words(prop) == words:
                continue  # already there: nothing to write
            was = {"at": se._round_pt(px, py), "angle": pa, "justify": " ".join(_justify_words(prop))}
            se._set_at(prop, x, y)
            se.write_layer.set_number(prop.find_children("at")[0], 2, stored)
            se._set_justify(prop, words)
            outcome.changed.add(se._uuid(node))
            if id(node) not in touched:
                touched.add(id(node))
                for auto in se._kids(node, "fields_autoplaced"):
                    se._remove(node, [auto])
            entry = {"reference": ref, "uuid": se._uuid(node), "property": name, "at": se._round_pt(x, y), "angle": stored,
                     "justify": " ".join(words), "orientation": "vertical" if vertical else "horizontal", "was": was}
            if detail:
                ox, oy, _a = se._at(node)
                entry["symbol_at"], entry["pins"] = se._round_pt(ox, oy), se.pin_positions(root, node)
            listed.append(entry)
    mine = {id(p) for n in nodes for p in n.children if not p.is_atom and p.name == "property"}
    after = _text_overlaps(root)
    remaining = [_pair(a, b) for a, b in after if id(a.node) in mine or id(b.node) in mine]
    extras = {
        "skipped": skipped, "gap": gap,
        "text_text_overlaps": {"before": before, "after": len(after), "involving_selection": len(remaining),
                               "listed": remaining[:MAX_REMAINING], "estimated": True,
                               "measure": "the estimated text boxes of find_overlaps (characters times font size times 0.6)"},
    }
    return listed, extras
