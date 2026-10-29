"""Build rung 1's own USB-C receptacle symbol and footprint (publish-prep ticket 02a).

The two files are written here, through the write layer's low-level S-expression API
(`kicad_mcp.write_layer`: typed atoms, serialise, parse check with kicad-cli, read-back verify,
`os.replace`); no `.kicad_*` text is edited by hand and no third-party library file is read or
transformed. The part is the SHOU HAN (Shenzhen Shouhan Technology Co., Ltd.) "TYPE-C 6P"
receptacle (LCSC C456012 is its purchasing id; it stays in the BOM because it names the part).

Where the numbers come from:
 * The pad table PAD_TABLE is the rung-1 board contract (ticket 51: nobody changes the board to
   help a router). It equals, pad for pad, the pad table of the footprint that rung 1 was pinned
   with, which was recorded from the raw `(pad ...)` nodes before that footprint was removed.
 * The manufacturer's "recommended P.C.B. layout" (drawing 1006-FGBXXX, 2021-08-02) was read for
   the dimensions. Pad positions in x (+-0.5, +-1.5, +-2.7 mm contacts; +-4.32 mm shield tabs)
   and the 3.80 mm tab spacing agree with it. Three pad SIZES do not (drawing against contract):
   contact pad height 1.00 against 1.20 mm, shield pad 1.00 x 1.80 against 1.10 x 1.90 mm, and
   slot 0.60 x 1.40 against 0.60 x 1.30 mm. The contract is kept; the differences are the
   owner's to decide (see the ticket's Answer).
 * Body 8.94 x 6.80 mm (drawing), opening edge 2.60 mm beyond the lower shield tabs.

Run:  python evals/rung1/build_usbc_asset.py        (rewrites the two files under assets/Rung1_USBC)
"""
from __future__ import annotations

import sys
from pathlib import Path

from kicad_tools.sexp import SExp

from kicad_mcp import write_layer as wl

HERE = Path(__file__).resolve().parent
ASSET = HERE / "assets" / "Rung1_USBC"
LIB = "Rung1_USBC"
SYMBOL = "USB_C_Receptacle_Power_6P"
FOOTPRINT = "USB_C_Receptacle_Shouhan_TYPE-C-6P"
SYMBOL_FILE = ASSET / f"{LIB}.kicad_sym"
FOOTPRINT_FILE = ASSET / f"{LIB}.pretty" / f"{FOOTPRINT}.kicad_mod"

SOURCE_NOTE = (
    "USB Type-C receptacle, 6 pin power only (VBUS, GND, CC1, CC2), SMD contacts with four "
    "through-hole shield tabs. Source: dimensions of the manufacturer's drawing, SHOU HAN "
    "(Shenzhen Shouhan Technology Co., Ltd.) TYPE-C 6P, recommended P.C.B. layout; own drawing, "
    "no third-party library data. Pad table: rung-1 board contract (ticket 51)."
)

# (number, kind, x, y, width, height, drill) in mm, footprint origin = body centre, rotation 0.
# kind "smd": rectangular SMD pad; kind "tab": oval plated through-hole with an oval drill.
PAD_TABLE = [
    ("B12", "smd", -2.70, -2.03, 0.8, 1.2, None),
    ("B9", "smd", -1.50, -2.03, 0.7, 1.2, None),
    ("A5", "smd", -0.50, -2.03, 0.7, 1.2, None),
    ("B5", "smd", 0.50, -2.03, 0.7, 1.2, None),
    ("A9", "smd", 1.50, -2.03, 0.7, 1.2, None),
    ("A12", "smd", 2.70, -2.03, 0.8, 1.2, None),
    ("7", "tab", 4.32, 2.03, 1.1, 1.9, (0.6, 1.3)),
    ("7", "tab", -4.32, 2.03, 1.1, 1.9, (0.6, 1.3)),
    ("7", "tab", 4.32, -1.78, 1.1, 1.9, (0.6, 1.3)),
    ("7", "tab", -4.32, -1.78, 1.1, 1.9, (0.6, 1.3)),
]

# Body outline from the drawing: 8.94 wide, 6.80 deep, the opening edge at y = +4.63.
BODY = (-4.47, -2.18, 4.47, 4.63)
# Courtyard: the body width in x (8.94 mm) and, in y, the body and the contact pads (pad edge at
# -2.63 mm, plus 0.05). It does not reach the shield tabs (pad edge +-4.87 mm): the rung-1 mounting hole H3
# keeps its own courtyard 2.45 mm around its centre, 4.85 mm from this origin, and a wider courtyard here
# would add a courtyards_overlap error to the board (measured). Reported to the owner.
COURTYARD = (-4.47, -2.68, 4.47, 4.63)

# (number, name, type, x, y, orientation) - the symbol; pin positions are the schematic wiring
# of build_rung1.py. Types follow the convention of KiCad's stock USB-C receptacle symbols:
# VBUS, GND and the shield are passive connector contacts, CC1 and CC2 are bidirectional.
PIN_TABLE = [
    ("B12", "GND", "passive", -12.7, 6.35, 0),
    ("B9", "VBUS", "passive", -12.7, 3.81, 0),
    ("A5", "CC1", "bidirectional", -12.7, 1.27, 0),
    ("B5", "CC2", "bidirectional", -12.7, -1.27, 0),
    ("A9", "VBUS", "passive", -12.7, -3.81, 0),
    ("A12", "GND", "passive", -12.7, -6.35, 0),
    ("7", "SHIELD", "passive", 12.7, 1.27, 180),
]


# ------------------------------------------------------------------ typed node helpers

def S(v):  # a quoted string, made by the write layer's typed setter (its text check included)
    holder = SExp.list("s", "")
    wl.set_string(holder, 0, v)
    return holder.children[0]


def Y(v):  # a bare keyword
    return wl.symbol(v)


def F(v):  # a number
    return wl.number(v)


def node(tag: str, *kids) -> SExp:
    return SExp.list(tag, *kids)


def at(x, y, angle=None) -> SExp:
    return node("at", F(x), F(y)) if angle is None else node("at", F(x), F(y), F(angle))


def effects(size=1.0, thickness=0.15, hide=False) -> SExp:
    n = node("effects", node("font", node("size", F(size), F(size)), node("thickness", F(thickness))))
    if hide:
        n.children.append(node("hide", Y("yes")))
    return n


def stroke(width, kind="solid") -> SExp:
    return node("stroke", node("width", F(width)), node("type", Y(kind)))


def line(x1, y1, x2, y2, layer, width) -> SExp:
    return node("fp_line", node("start", F(x1), F(y1)), node("end", F(x2), F(y2)), stroke(width),
                node("layer", S(layer)))


def rect(box, layer, width) -> SExp:
    x1, y1, x2, y2 = box
    return node("fp_rect", node("start", F(x1), F(y1)), node("end", F(x2), F(y2)), stroke(width),
                node("fill", Y("no")), node("layer", S(layer)))


def text_prop(name, value, x, y, layer, hide=False, size=1.0) -> SExp:
    n = node("property", S(name), S(value), at(x, y, 0), node("layer", S(layer)))
    if hide:
        n.children.append(node("hide", Y("yes")))
    n.children.append(effects(size))
    return n


# ------------------------------------------------------------------ the footprint

def footprint_tree() -> SExp:
    fp = node("footprint", S(FOOTPRINT), node("version", F(20260206)), node("generator", S("kicad_mcp")),
              node("layer", S("F.Cu")), node("descr", S(SOURCE_NOTE)),
              node("tags", S("USB Type-C receptacle 6P power only SHOU HAN TYPE-C 6P")))
    fp.children.append(text_prop("Reference", "REF**", 0, -4.4, "F.SilkS"))
    fp.children.append(text_prop("Value", FOOTPRINT, 0, 6.4, "F.Fab"))
    fp.children.append(text_prop("Datasheet", "", 0, 0, "F.Fab", hide=True, size=1.27))
    fp.children.append(text_prop("Description", SOURCE_NOTE, 0, 0, "F.Fab", hide=True, size=1.27))
    fp.children.append(node("attr", Y("smd")))
    # Silkscreen: the side walls between the tab pads and the opening edge (clear of every pad).
    sx1, sy1, sx2, sy2 = BODY
    for sx in (sx1, sx2):
        fp.children.append(line(sx, -0.63, sx, 0.88, "F.SilkS", 0.15))
        fp.children.append(line(sx, 3.18, sx, sy2, "F.SilkS", 0.15))
    fp.children.append(line(sx1, sy2, sx2, sy2, "F.SilkS", 0.15))
    fp.children.append(rect(BODY, "F.Fab", 0.1))
    fp.children.append(rect(COURTYARD, "F.CrtYd", 0.05))
    fp.children.append(node("fp_text", Y("user"), S("${REFERENCE}"), at(0, 1.2, 0), node("layer", S("F.Fab")),
                            effects(1.0)))
    for number, kind, x, y, w, h, drill in PAD_TABLE:
        if kind == "smd":
            pad = node("pad", S(number), Y("smd"), Y("rect"), at(x, y), node("size", F(w), F(h)),
                       node("layers", S("F.Cu"), S("F.Mask"), S("F.Paste")))
        else:
            pad = node("pad", S(number), Y("thru_hole"), Y("oval"), at(x, y), node("size", F(w), F(h)),
                       node("drill", Y("oval"), F(drill[0]), F(drill[1])),
                       node("layers", S("*.Cu"), S("*.Mask")))
        fp.children.append(pad)
    return fp


# ------------------------------------------------------------------ the symbol

def symbol_tree() -> SExp:
    def prop(name, value, x, y, hide=False):
        n = node("property", S(name), S(value), at(x, y, 0), node("effects", node("font", node("size", F(1.27), F(1.27)))))
        if hide:
            n.children[-1].children.append(node("hide", Y("yes")))
        return n

    sym = node("symbol", S(SYMBOL), node("pin_names", node("offset", F(1.016))), node("exclude_from_sim", Y("no")),
               node("in_bom", Y("yes")), node("on_board", Y("yes")))
    sym.children.append(prop("Reference", "J", 0, 10.16))
    sym.children.append(prop("Value", "USB_C_PWR", 0, -10.16))
    sym.children.append(prop("Footprint", f"{LIB}:{FOOTPRINT}", 0, -12.7, hide=True))
    sym.children.append(prop("Datasheet", "", 0, -15.24, hide=True))
    sym.children.append(prop("Description", SOURCE_NOTE, 0, -17.78, hide=True))
    sym.children.append(prop("Manufacturer", "SHOU HAN", 0, -20.32, hide=True))
    sym.children.append(prop("MPN", "TYPE-C 6P", 0, -22.86, hide=True))
    body = node("symbol", S(f"{SYMBOL}_0_1"),
                node("rectangle", node("start", F(-7.62), F(8.89)), node("end", F(7.62), F(-8.89)),
                     stroke(0, "default"), node("fill", node("type", Y("background")))))
    sym.children.append(body)
    pins = node("symbol", S(f"{SYMBOL}_1_1"))
    for number, name, kind, x, y, angle in PIN_TABLE:
        pins.children.append(node(
            "pin", Y(kind), Y("line"), at(x, y, angle), node("length", F(5.08)),
            node("name", S(name), node("effects", node("font", node("size", F(1.27), F(1.27))))),
            node("number", S(number), node("effects", node("font", node("size", F(1.27), F(1.27)))))))
    sym.children.append(pins)
    sym.children.append(node("embedded_fonts", Y("no")))
    return node("kicad_symbol_lib", node("version", F(20251024)), node("generator", S("kicad_symbol_editor")),
                node("generator_version", S("10.0")), sym)


# ------------------------------------------------------------------ read-back check

class AssetMismatch(RuntimeError):
    pass


def read_pads(root: SExp) -> list[tuple]:
    """(number, type, shape, x, y, w, h, layers, drill) of every `(pad ...)` node, raw."""
    rows = []
    for pad in root.find_all("pad"):
        a = pad.get_atoms()
        at_ = [float(v) for v in pad.find_child("at").get_atoms()]
        size = [float(v) for v in pad.find_child("size").get_atoms()]
        layers = tuple(sorted(str(v) for v in pad.find_child("layers").get_atoms()))
        dr = pad.find_child("drill")
        drill = None if dr is None else tuple(float(v) for v in dr.get_atoms()[1:])
        rows.append((str(a[0]), str(a[1]), str(a[2]), at_[0], at_[1], size[0], size[1], layers, drill))
    return rows


def read_pins(root: SExp) -> list[tuple]:
    rows = []
    for pin in root.find_all("pin"):
        a = pin.get_atoms()
        at_ = [float(v) for v in pin.find_child("at").get_atoms()]
        name = pin.find_child("name").get_atoms()[0]
        number = pin.find_child("number").get_atoms()[0]
        rows.append((str(number), str(name), str(a[0]), at_[0], at_[1], at_[2]))
    return rows


def verify_footprint(path: Path) -> None:
    got = read_pads(wl.load(path))
    want = sorted(
        (n, "smd" if k == "smd" else "thru_hole", "rect" if k == "smd" else "oval", x, y, w, h,
         tuple(sorted(["F.Cu", "F.Mask", "F.Paste"] if k == "smd" else ["*.Cu", "*.Mask"])), d)
        for n, k, x, y, w, h, d in PAD_TABLE)
    if sorted(got) != want:
        raise AssetMismatch(f"the written footprint's pads differ from PAD_TABLE: {sorted(got)!r}")


def verify_symbol(path: Path) -> None:
    got = sorted(read_pins(wl.load(path)))
    want = sorted((n, nm, t, x, y, float(a)) for n, nm, t, x, y, a in PIN_TABLE)
    if got != want:
        raise AssetMismatch(f"the written symbol's pins differ from PIN_TABLE: {got!r}")


def main() -> int:
    FOOTPRINT_FILE.parent.mkdir(parents=True, exist_ok=True)
    wl.save(footprint_tree(), FOOTPRINT_FILE, verify=verify_footprint)
    wl.save(symbol_tree(), SYMBOL_FILE, verify=verify_symbol)
    print(f"wrote {SYMBOL_FILE.name} and {FOOTPRINT_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
