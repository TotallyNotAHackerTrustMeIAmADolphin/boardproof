"""Run by KiCad's bundled Python, never by the test interpreter: load a board with KiCad's
own ``pcbnew`` module and print what KiCad itself made of it, as JSON on stdout (build ticket
43's oracle for pad angles and sizes, zone outlines and Edge.Cuts shapes, which no
``kicad-cli`` export carries exactly).

Usage: ``<kicad>/bin/python.exe -I _pcbnew_dump.py <board.kicad_pcb>``. The caller sets
``KICAD_CONFIG_HOME`` to a throwaway directory. wx logging goes to stderr before ``pcbnew``
loads, so a font-substitution message cannot open a modal box (builder brief rule 8).

All lengths are KiCad's internal nanometres, exactly as ``pcbnew`` returns them; angles are
degrees. Nothing is written except stdout.
"""
import json
import sys

pcbnew = None  # imported by main(), only after wx logging is sent to stderr


def _load_pcbnew():
    global pcbnew
    import wx

    wx.Log.SetActiveTarget(wx.LogStderr())
    import pcbnew


def xy(v):
    return [int(v.x), int(v.y)]


def shape_record(shape, owner):
    kind = shape.GetShapeStr() if hasattr(shape, "GetShapeStr") else str(shape.GetShape())
    record = {
        "owner": owner,
        "uuid": shape.m_Uuid.AsString(),
        "shape": int(shape.GetShape()),
        "kind": kind,
        "width": int(shape.GetWidth()),
        "start": xy(shape.GetStart()),
        "end": xy(shape.GetEnd()),
    }
    s = shape.GetShape()
    if s == pcbnew.SHAPE_T_ARC:
        record["mid"] = xy(shape.GetArcMid())
        record["center"] = xy(shape.GetCenter())
    elif s == pcbnew.SHAPE_T_CIRCLE:
        record["center"] = xy(shape.GetCenter())
        record["radius"] = int(shape.GetRadius())
    elif s == pcbnew.SHAPE_T_POLY:
        record["points"] = [xy(p) for p in shape.GetPolyPoints()]
        poly = shape.GetPolyShape()
        record["contour"] = contour(poly.Outline(0)) if poly.OutlineCount() else []
    elif s == pcbnew.SHAPE_T_BEZIER:
        record["controls"] = [xy(shape.GetBezierC1()), xy(shape.GetBezierC2())]
    box = shape.GetBoundingBox()
    record["bbox"] = [int(box.GetLeft()), int(box.GetTop()), int(box.GetRight()), int(box.GetBottom())]
    return record


def contour(chain):
    """A closed chain as KiCad's file writes it: ["xy", p] per plain point and
    ["arc", start, mid, end] per arc (an arc's own approximation points are not listed).
    ``ArcIndex`` returns an unwrapped ssize_t in this binding, so arcs are found by their
    start point."""
    arcs = [chain.Arc(i) for i in range(chain.ArcCount())]
    items = []
    for k in range(chain.PointCount()):
        point = chain.CPoint(k)
        if not chain.IsPtOnArc(k):
            items.append(["xy", xy(point)])
        elif chain.IsArcStart(k):
            for arc in arcs:
                if arc.GetP0() == point:
                    items.append(["arc", xy(arc.GetP0()), xy(arc.GetArcMid()), xy(arc.GetP1())])
                    break
            else:
                items.append(["unmatched_arc_start", xy(point)])
    return items


def zone_record(zone, owner):
    outline = zone.Outline()
    polygons = []
    for i in range(outline.OutlineCount()):
        polygons.append({
            "outline": contour(outline.Outline(i)),
            "holes": [contour(outline.Hole(i, h)) for h in range(outline.HoleCount(i))],
        })
    return {
        "owner": owner,
        "uuid": zone.m_Uuid.AsString(),
        "name": zone.GetZoneName(),
        "net": zone.GetNetname(),
        "keepout": bool(zone.GetIsRuleArea()),
        "layers": [pcbnew.BOARD.GetStandardLayerName(layer) for layer in zone.GetLayerSet().Seq()],
        "polygons": polygons,
    }


def main(path):
    _load_pcbnew()
    board = pcbnew.LoadBoard(path)
    board.BuildConnectivity()  # FlashLayer asks it where a remove_unused_layers pad connects
    settings = board.GetDesignSettings()
    out = {
        "version": pcbnew.Version(),
        "aux_origin": xy(settings.GetAuxOrigin()),
        "grid_origin": xy(settings.GetGridOrigin()),
        "footprints": [],
        "zones": [zone_record(z, None) for z in board.Zones()],
        "edge_shapes": [],
    }
    edge = pcbnew.Edge_Cuts
    for drawing in board.GetDrawings():
        if drawing.GetLayer() == edge and isinstance(drawing, pcbnew.PCB_SHAPE):
            out["edge_shapes"].append(shape_record(drawing, None))
    copper = [(layer, pcbnew.BOARD.GetStandardLayerName(layer)) for layer in board.GetEnabledLayers().CuStack()]
    shapes = {
        pcbnew.PAD_SHAPE_CIRCLE: "circle", pcbnew.PAD_SHAPE_RECTANGLE: "rect", pcbnew.PAD_SHAPE_OVAL: "oval",
        pcbnew.PAD_SHAPE_TRAPEZOID: "trapezoid", pcbnew.PAD_SHAPE_ROUNDRECT: "roundrect",
        pcbnew.PAD_SHAPE_CHAMFERED_RECT: "chamfered_rect", pcbnew.PAD_SHAPE_CUSTOM: "custom",
    }
    modes = {
        pcbnew.PADSTACK.MODE_NORMAL: "normal", pcbnew.PADSTACK.MODE_FRONT_INNER_BACK: "front_inner_back",
        pcbnew.PADSTACK.MODE_CUSTOM: "custom",
    }
    out["copper_layers"] = [name for _, name in copper]
    for fp in board.GetFootprints():
        ref = fp.GetReference()
        uuid = fp.m_Uuid.AsString()
        pads = []
        for pad in fp.Pads():
            pads.append({
                "number": pad.GetNumber(),
                "uuid": pad.m_Uuid.AsString(),
                "at": xy(pad.GetPosition()),
                "angle": pad.GetOrientation().AsDegrees(),
                "fp_relative_angle": pad.GetFPRelativeOrientation().AsDegrees(),
                "size": xy(pad.GetSize(pcbnew.F_Cu)),  # the pad's own (size): KiCad's F.Cu entry
                "mode": modes[pad.Padstack().Mode()],
                # Every copper layer of the board, by its canonical name: [name, shape, size,
                # on, flashed]. GetShape/GetSize answer any layer, whether the pad is on it or
                # not; IsOnLayer is its (layers ...) membership and FlashLayer whether KiCad puts
                # copper there (remove_unused_layers and np_thru_hole holes included).
                "copper": [[name, shapes[pad.GetShape(layer)], xy(pad.GetSize(layer)),
                            bool(pad.IsOnLayer(layer)), bool(pad.FlashLayer(layer))] for layer, name in copper],
            })
        out["footprints"].append({
            "reference": ref,
            "uuid": uuid,
            "at": xy(fp.GetPosition()),
            "angle": fp.GetOrientation().AsDegrees(),
            "back": bool(fp.IsFlipped()),
            "pads": pads,
        })
        for item in fp.GraphicalItems():
            if item.GetLayer() == edge and isinstance(item, pcbnew.PCB_SHAPE):
                out["edge_shapes"].append(shape_record(item, uuid))
        for zone in fp.Zones():
            out["zones"].append(zone_record(zone, uuid))
    json.dump(out, sys.stdout)


if __name__ == "__main__":
    main(sys.argv[1])
