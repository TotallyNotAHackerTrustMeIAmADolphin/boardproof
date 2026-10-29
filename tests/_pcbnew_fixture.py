"""Run by KiCad's bundled Python, never by the test interpreter: build build ticket 43's
padstack and zone-hole board from a demo copy with KiCad's own ``pcbnew`` module, and print
which items it changed as JSON on stdout. No demo board carries a ``(padstack ...)`` token or
a zone hole, so KiCad itself writes them here; nothing is written as text.

Usage: ``<kicad>/bin/python.exe -I _pcbnew_fixture.py <in.kicad_pcb> <out.kicad_pcb>``, on a
board with at least four copper layers, front through-hole pads, back-side pads and a zone
(KiCad's ``video`` demo). The caller sets ``KICAD_CONFIG_HOME`` to a throwaway directory; wx
logging goes to stderr before ``pcbnew`` loads (builder brief rule 8).

What it makes, every size in mm:

- ``custom``: a front through-hole pad in padstack mode custom, In1.Cu a 0.9 x 0.8 rect and
  B.Cu a 3 x 2 oval (KiCad then writes every inner layer and B.Cu).
- ``front_inner_back``: a second front through-hole pad, inner layers a 1.1 x 0.7 roundrect
  and B.Cu a 2.5 circle.
- ``back_side``: a pad of a back-side footprint in mode front_inner_back, F.Cu a 2.2 x 1.1
  rect and B.Cu (the copper it sits on) a 4 x 3 oval.
- ``remove_unused``: a third front through-hole pad with remove_unused_layers and
  keep_end_layers set (KiCad writes ``(remove_unused_layers yes) (keep_end_layers yes)``).
- ``zone_connected``: a further front through-hole pad with remove_unused_layers set and
  keep_end_layers not, inside an inner-layer zone of its net; the zones are filled, so
  KiCad writes the layer in ``(zone_layer_connections ...)``.
- ``zone``: two square holes (2 mm and 1 mm) inside one zone's outline.
"""
import json
import sys

pcbnew = None  # imported by main(), only after wx logging is sent to stderr


def _load_pcbnew():
    global pcbnew
    import wx

    wx.Log.SetActiveTarget(wx.LogStderr())
    import pcbnew


def mm(value):
    return pcbnew.FromMM(value)


def size(w, h):
    return pcbnew.VECTOR2I(mm(w), mm(h))


def main(src, dst):
    _load_pcbnew()
    board = pcbnew.LoadBoard(src)
    if board.GetCopperLayerCount() < 4:
        raise SystemExit(f"{src}: needs four copper layers, has {board.GetCopperLayerCount()}")
    front_th = [p for fp in board.GetFootprints() if not fp.IsFlipped() for p in fp.Pads()
                if p.GetAttribute() == pcbnew.PAD_ATTRIB_PTH]
    back = [p for fp in board.GetFootprints() if fp.IsFlipped() for p in fp.Pads()]
    custom, fib, removed, back_pad = front_th[0], front_th[1], front_th[2], back[0]

    custom.Padstack().SetMode(pcbnew.PADSTACK.MODE_CUSTOM)
    custom.SetShape(pcbnew.In1_Cu, pcbnew.PAD_SHAPE_RECTANGLE)
    custom.SetSize(pcbnew.In1_Cu, size(0.9, 0.8))
    custom.SetShape(pcbnew.B_Cu, pcbnew.PAD_SHAPE_OVAL)
    custom.SetSize(pcbnew.B_Cu, size(3, 2))

    fib.Padstack().SetMode(pcbnew.PADSTACK.MODE_FRONT_INNER_BACK)
    fib.SetShape(pcbnew.In1_Cu, pcbnew.PAD_SHAPE_ROUNDRECT)
    fib.SetSize(pcbnew.In1_Cu, size(1.1, 0.7))
    fib.SetShape(pcbnew.B_Cu, pcbnew.PAD_SHAPE_CIRCLE)
    fib.SetSize(pcbnew.B_Cu, size(2.5, 2.5))

    removed.SetRemoveUnconnected(True)
    removed.SetKeepTopBottom(True)

    back_pad.Padstack().SetMode(pcbnew.PADSTACK.MODE_FRONT_INNER_BACK)
    back_pad.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_RECTANGLE)
    back_pad.SetSize(pcbnew.F_Cu, size(2.2, 1.1))
    back_pad.SetShape(pcbnew.B_Cu, pcbnew.PAD_SHAPE_OVAL)
    back_pad.SetSize(pcbnew.B_Cu, size(4, 3))

    holed = None
    for zone in board.Zones():
        outline = zone.Outline()
        c = zone.GetBoundingBox().GetCenter()
        squares = [(c.x, c.y, 1.0), (c.x + mm(4), c.y, 0.5)]
        if outline.OutlineCount() == 1 and outline.HoleCount(0) == 0 and all(
            outline.Contains(pcbnew.VECTOR2I(x + mm(dx * h), y + mm(dy * h)))
            for x, y, h in squares for dx in (-1, 1) for dy in (-1, 1)
        ):
            for hole, (x, y, h) in enumerate(squares):
                outline.NewHole(0)
                for dx, dy in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
                    outline.Append(x + mm(dx * h), y + mm(dy * h), 0, hole)  # polygon 0, this hole
            holed = zone
            break
    if holed is None:
        raise SystemExit(f"{src}: no single-outline zone holds the two holes")

    # A further remove_unused_layers pad that a zone of its net reaches on an inner layer:
    # KiCad's zone filler marks that layer, and the save writes (zone_layer_connections ...).
    def inner_zone_holds(pad):
        return any(not z.GetIsRuleArea() and z.GetNetCode() == pad.GetNetCode()
                   and any(layer not in (pcbnew.F_Cu, pcbnew.B_Cu) for layer in z.GetLayerSet().Seq())
                   and z.Outline().Contains(pad.GetPosition()) for z in board.Zones())

    zoned = next((p for p in front_th[3:] if inner_zone_holds(p)), None)
    if zoned is None:
        raise SystemExit(f"{src}: no front through-hole pad sits in an inner zone of its net")
    zoned.SetRemoveUnconnected(True)
    zoned.SetKeepTopBottom(False)
    if not pcbnew.ZONE_FILLER(board).Fill(board.Zones()):
        raise SystemExit(f"{src}: the zone fill did not complete")
    pcbnew.SaveBoard(dst, board)
    json.dump({
        "custom": custom.m_Uuid.AsString(),
        "front_inner_back": fib.m_Uuid.AsString(),
        "back_side": back_pad.m_Uuid.AsString(),
        "remove_unused": removed.m_Uuid.AsString(),
        "zone_connected": zoned.m_Uuid.AsString(),
        "zone": holed.m_Uuid.AsString(),
    }, sys.stdout)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
