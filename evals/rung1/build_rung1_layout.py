"""Rung-1 layout flow (ticket 10), every step through the server's own tools.

Copies evals/rung1/project (never touched) into evals/rung1/layout_run/ and runs, via the MCP
client against build_server():

  import_from_schematic -> board_outline (+ mounting holes) -> placement (place_footprints)
  -> board_rules -> input_preparation -> silkscreen_minimums -> pin_rule_baseline -> pour step

Each confirm writes a new revision directory; the chain stops at the first refusal and the
record says which call was blocked and what would have been needed. Refusal flags
(allow_loosen / allow_incoherent / allow_overlap) are never set by this script.

Run:  .venv/Scripts/python.exe evals/rung1/build_rung1_layout.py          (flow only)
      .venv/Scripts/python.exe evals/rung1/build_rung1_layout.py --measure (then autonomy_run)
Writes evals/rung1/layout_run_record.json.  Measurement is run once; do not re-run to improve it.
"""
from __future__ import annotations

import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SOURCE = HERE / "project"
RUN = HERE / "layout_run"
NAME = "rung1_usbc_attiny1616"
RECORD = HERE / "layout_run_record.json"

# Page-frame origin of the outline (the blank board's own Edge.Cuts corner); all layout
# values below are board-local mm, added to this origin.
OX, OY = 128.5, 90.0
W, H = 34.0, 23.0
HOLE_INSET, HOLE_KEEPOUT = 2.7, 2.0
HOLES = [("H1", HOLE_INSET, HOLE_INSET), ("H2", W - HOLE_INSET, HOLE_INSET),
         ("H3", HOLE_INSET, H - HOLE_INSET), ("H4", W - HOLE_INSET, H - HOLE_INSET)]
# Board-local footprint origins, rotation 0. J1's opening (+y side) faces the bottom edge.
# C3 (U1's decoupling capacitor) is not in this table: build ticket 114 derives it from raw pad and
# courtyard geometry (plan_decoupling_capacitor below), so no position of it is typed by hand.
LAYOUT = {"J1": (10, 18.0), "F1": (10, 13), "D1": (10, 8.5), "R1": (17.6, 15.8), "R2": (17.6, 18.6),
          "C1": (17.6, 12.5), "U2": (21, 8), "C2": (27, 8), "U1": (24.5, 17.5),
          "R3": (21.5, 13.5), "D2": (29.6, 16.2), "J2": (30.6, 7.5)}
RULES = dict(
    min_clearance=0.127, min_track_width=0.127, via_diameter=0.45, min_through_hole_diameter=0.2,
    netclasses=[
        {"name": "Default", "clearance": 0.127, "track_width": 0.127, "via_diameter": 0.45, "via_drill": 0.2},
        {"name": "Power", "clearance": 0.2, "track_width": 0.4, "via_diameter": 0.6, "via_drill": 0.3,
         "priority": 0, "nets": ["VBUS", "/VBUS_F", "+3V3"]},
    ],
)


# --- Decoupling capacitor placement (build ticket 114) -------------------------------------------------
# Owner decision, 2026-10-29: C3's +3V3 pad goes directly next to U1's +3V3 pin (pin 4, left side of the
# VQFN). The position is the result of the rule below, evaluated on raw pad and courtyard nodes. It was
# fixed before any routing result of the new input existed, applies to both routers alike, and is not to be
# re-tuned after a measurement (a change of it is a protocol change).
#   1. C3's +3V3 pad faces U1 (rotation 180) and C3's courtyard keeps the board's minimum clearance from
#      U1's courtyard: that sets x. The gap between the two pad edges follows from the pad and courtyard
#      geometry and must be at most MAX_PAD_GAP_MM.
#   2. C3's +3V3 pad does not shadow the straight escape of pin 3 (GND, the pad above pin 4): its top edge
#      lies half the minimum track width plus the Power class clearance below pin 3's centre line: sets y.
#   3. Pin 4's centre line runs through that pad, so the two connect with a straight track.
# Rounding is toward the safe side (x away from U1, y away from pin 3) at 0.001 mm.
MAX_PAD_GAP_MM = 1.5
POWER_CLEARANCE_MM = next(c["clearance"] for c in RULES["netclasses"] if c["name"] == "Power")
COURTYARD_GAP_MM = RULES["min_clearance"]
DECOUPLING = {"cap": "C3", "ic": "U1", "ic_pad": "4", "neighbour_pad": "3", "cap_pad": "1", "net": "+3V3"}
# the supply capacitors of the board and the IC and net each serves (see supply_capacitor_distances)
SUPPLY_CAPS = {"C1": ("U2", "/VBUS_F"), "C2": ("U2", "+3V3"), "C3": ("U1", "+3V3")}


def _geo():
    from kicad_mcp import raw_geometry

    return raw_geometry


def _ref_of(fp) -> str:
    for c in fp.children:
        if c.name == "property" and c.values and c.values[0] == "Reference":
            return str(c.values[1])
    return ""


def _footprint_nodes(root) -> dict:
    return {_ref_of(f): f for f in root.children if f.name == "footprint"}


def _pad_nets(fp) -> dict[str, str]:
    """Pad number -> net name from each pad's raw (net ...) node (its last value is the name)."""
    out = {}
    for pad in fp.children:
        if pad.name == "pad" and pad.values:
            net = pad.find("net")
            out[str(pad.values[0])] = str(net.values[-1]) if net is not None and net.values else ""
    return out


def _courtyard_local(fp) -> tuple[float, float, float, float]:
    """The footprint-local bounding box of the front courtyard graphics (raw start/end/xy values)."""
    xs, ys = [], []
    for node in fp.children:
        if not (node.name or "").startswith("fp_"):
            continue
        layer = node.find("layer")
        if layer is None or not layer.values or str(layer.values[0]) != "F.CrtYd":
            continue
        for tag in ("start", "mid", "end", "center"):
            p = node.find(tag)
            if p is not None and len(p.values) >= 2:
                xs.append(float(p.values[0]))
                ys.append(float(p.values[1]))
        pts = node.find("pts")
        if pts is not None:
            for c in pts.children:
                if c.name == "xy" and len(c.values) >= 2:
                    xs.append(float(c.values[0]))
                    ys.append(float(c.values[1]))
    if not xs:
        raise ValueError(f"{_ref_of(fp)}: no F.CrtYd graphics")
    return min(xs), min(ys), max(xs), max(ys)


def _page_box(local_box, origin, angle):
    place = _geo().place
    pts = [place(origin, angle, (x, y)) for x in (local_box[0], local_box[2]) for y in (local_box[1], local_box[3])]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _pad_box(pad) -> tuple[float, float, float, float]:
    """Axis-aligned page box of a rectangular pad from its raw size and board-absolute angle."""
    w, h = pad.size_mm
    quarter = round(pad.angle_deg) % 360
    if abs(pad.angle_deg - round(pad.angle_deg)) > 1e-6 or quarter % 90:
        raise ValueError(f"pad {pad.number}: angle {pad.angle_deg} is not a multiple of 90")
    if quarter in (90, 270):
        w, h = h, w
    return pad.at_mm[0] - w / 2, pad.at_mm[1] - h / 2, pad.at_mm[0] + w / 2, pad.at_mm[1] + h / 2


def _box_gap(a, b) -> float:
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return (dx * dx + dy * dy) ** 0.5


def _floor3(v: float) -> float:
    return ((v * 1000 + 1e-6) // 1) / 1000


def _ceil3(v: float) -> float:
    return -((-(v * 1000 - 1e-6)) // 1) / 1000


def plan_decoupling_capacitor(root, layout: dict) -> dict:
    """C3's board-local position and rotation from the raw geometry of the imported board ``root``.

    ``layout`` holds U1's board-local origin. Raises ValueError when the geometry is not the case the
    rule is written for (pin 4 on the left of an unrotated U1, C3's pad 1 on the left of an unrotated C3)."""
    d = DECOUPLING
    fps = {f.reference: f for f in _geo().read_footprints(root)}
    nodes = _footprint_nodes(root)
    ic, cap = fps[d["ic"]], fps[d["cap"]]
    if ic.angle_deg != 0.0 or cap.angle_deg != 0.0:
        raise ValueError("the rule is written for footprints imported unrotated")
    pin = next(p for p in ic.pads if p.number == d["ic_pad"])
    nb = next(p for p in ic.pads if p.number == d["neighbour_pad"])
    cpad = next(p for p in cap.pads if p.number == d["cap_pad"])
    if not (pin.local_mm[0] < 0 and cpad.local_mm[0] < 0 and cpad.local_mm[1] == 0.0):
        raise ValueError("the rule is written for pin 4 on the left side and a cap pad 1 on its left")
    ic_origin = (OX + layout[d["ic"]][0], OY + layout[d["ic"]][1])
    ic_court = _page_box(_courtyard_local(nodes[d["ic"]]), ic_origin, 0.0)
    cap_local = _courtyard_local(nodes[d["cap"]])
    cx = _floor3(ic_court[0] - COURTYARD_GAP_MM + cap_local[0])  # rotation 180: the courtyard's right reach is -min_x
    axis_clear = RULES["min_track_width"] / 2 + POWER_CLEARANCE_MM
    cy = _ceil3(ic_origin[1] + nb.local_mm[1] + axis_clear + cpad.size_mm[1] / 2)
    return {"x": round(cx - OX, 3), "y": round(cy - OY, 3), "rotation": 180.0,
            "rule": {"max_pad_gap_mm": MAX_PAD_GAP_MM, "courtyard_gap_mm": COURTYARD_GAP_MM,
                     "pin3_axis_clearance_mm": axis_clear}}


def decoupling_facts(root) -> dict:
    """The measured numbers of the rule on a board (raw nodes, page mm)."""
    d = DECOUPLING
    fps = {f.reference: f for f in _geo().read_footprints(root)}
    nodes = _footprint_nodes(root)
    ic, cap = fps[d["ic"]], fps[d["cap"]]
    pin = next(p for p in ic.pads if p.number == d["ic_pad"])
    nb = next(p for p in ic.pads if p.number == d["neighbour_pad"])
    ep = next(p for p in ic.pads if p.number == "21")
    nets = _pad_nets(nodes[d["cap"]])
    plus = next(p for p in cap.pads if nets.get(p.number) == d["net"])
    gnd = next(p for p in cap.pads if p is not plus)
    pbox, cbox, gbox = _pad_box(pin), _pad_box(plus), _pad_box(gnd)
    ic_court = _page_box(_courtyard_local(nodes[d["ic"]]), ic.at_mm, ic.angle_deg)
    cap_court = _page_box(_courtyard_local(nodes[d["cap"]]), cap.at_mm, cap.angle_deg)
    return {
        "c3_at": (cap.at_mm[0], cap.at_mm[1], cap.angle_deg),
        "u1_pin4_edge_to_c3_pad1_edge_mm": round(_box_gap(pbox, cbox), 4),
        "c3_pad1_top_minus_pin3_axis_mm": round(cbox[1] - nb.at_mm[1], 4),
        "pin3_axis_y": nb.at_mm[1],
        "pin4_y": pin.at_mm[1],
        "c3_pad1_y_range": (cbox[1], cbox[3]),
        "courtyard_gap_to_u1_mm": round(max(ic_court[0] - cap_court[2], cap_court[0] - ic_court[2],
                                             ic_court[1] - cap_court[3], cap_court[1] - ic_court[3]), 4),
        "plus_pad_is_nearer": _box_gap(pbox, cbox) < _box_gap(pbox, gbox),
        "c3_gnd_pad_to_u1_pin3_edge_mm": round(_box_gap(gbox, _pad_box(nb)), 4),
        "c3_gnd_pad_to_u1_exposed_pad_edge_mm": round(_box_gap(gbox, _pad_box(ep)), 4),
    }


def decoupling_findings(root) -> list[str]:
    """Every way the board breaks the rule (empty when it holds)."""
    f = decoupling_facts(root)
    out = []
    gap = f["u1_pin4_edge_to_c3_pad1_edge_mm"]
    if gap > MAX_PAD_GAP_MM:
        out.append(f"pad gap {gap} mm between U1 pin 4 and C3's +3V3 pad exceeds {MAX_PAD_GAP_MM} mm")
    if gap < POWER_CLEARANCE_MM - 1e-9:
        out.append(f"pad gap {gap} mm is below the Power clearance {POWER_CLEARANCE_MM} mm")
    if not f["plus_pad_is_nearer"]:
        out.append("C3's +3V3 pad is not the pad nearest U1 pin 4")
    axis_clear = RULES["min_track_width"] / 2 + POWER_CLEARANCE_MM
    top, bottom = f["c3_pad1_y_range"]
    if top < f["pin3_axis_y"] + axis_clear - 1e-6 and bottom > f["pin3_axis_y"] - axis_clear:
        out.append("C3's +3V3 pad shadows the straight escape of U1 pin 3")
    if not top <= f["pin4_y"] <= bottom:
        out.append("U1 pin 4's centre line misses C3's +3V3 pad: no straight track")
    if f["courtyard_gap_to_u1_mm"] < COURTYARD_GAP_MM - 1e-3:
        out.append(f"courtyard gap to U1 {f['courtyard_gap_to_u1_mm']} mm is below {COURTYARD_GAP_MM} mm")
    nodes = _footprint_nodes(root)
    fps = {g.reference: g for g in _geo().read_footprints(root)}
    me = DECOUPLING["cap"]
    cc = _page_box(_courtyard_local(nodes[me]), fps[me].at_mm, fps[me].angle_deg)
    for ref, node in nodes.items():
        if ref == me or ref.startswith("H"):
            continue
        o = _page_box(_courtyard_local(node), fps[ref].at_mm, fps[ref].angle_deg)
        if cc[0] < o[2] and o[0] < cc[2] and cc[1] < o[3] and o[1] < cc[3]:
            out.append(f"C3's courtyard overlaps the courtyard of {ref}")
    return out


def supply_capacitor_distances(root) -> dict:
    """Per supply capacitor: the IC it serves and the smallest pad-edge gap to a pad of the same net."""
    fps = {f.reference: f for f in _geo().read_footprints(root)}
    nodes = _footprint_nodes(root)
    out = {}
    for ref, (ic, net) in SUPPLY_CAPS.items():
        cnets, inets = _pad_nets(nodes[ref]), _pad_nets(nodes[ic])
        gaps = [_box_gap(_pad_box(cp), _pad_box(ip))
                for cp in fps[ref].pads if cnets.get(cp.number) == net
                for ip in fps[ic].pads if inets.get(ip.number) == net]
        out[ref] = {"ic": ic, "net": net, "gap_mm": round(min(gaps), 3)}
    return out


def raw_positions(board: Path) -> dict[str, tuple[float, float]]:
    """Footprint reference -> (x, y) read from the raw top-level (at ...) of each footprint."""
    import re

    text = board.read_text(encoding="utf-8")
    out = {}
    for blk in re.split(r'\n\t\(footprint "', text)[1:]:
        at = re.search(r"\n\t\t\(at ([-\d.]+) ([-\d.]+)", blk)
        ref = re.search(r'\(property "Reference" "([^"]+)"|\(fp_text reference "([^"]+)"', blk)
        if at and ref:
            out[ref.group(1) or ref.group(2)] = (round(float(at.group(1)), 3), round(float(at.group(2)), 3))
    return out


# Owner approval, 2026-10-11 (asked and answered in the session): allow_loosen=true is approved
# for EXACTLY these loosened items and nothing else. allow_incoherent / allow_overlap are NOT
# approved and are never passed. (where, key, before, after)
APPROVED_LOOSENED = {
    ("constraints", "min_track_width", 0.2, 0.127),
    ("constraints", "min_via_diameter", 0.5, 0.45),
    ("constraints", "min_through_hole_diameter", 0.3, 0.2),
    ("netclass:Default", "clearance", 0.2, 0.127),
    # Same item, same target. kicad-tools 0.24.0's blank project starts the Default class at 0.15 mm
    # (0.23.x: 0.2 mm), so the same loosening to 0.127 mm now reads 0.15 -> 0.127, a smaller step
    # than the approved one. Added by publish-prep ticket 02a and reported to the owner.
    ("netclass:Default", "clearance", 0.15, 0.127),
    ("netclass:Default", "track_width", 0.25, 0.127),
    ("netclass:Default", "via_diameter", 0.6, 0.45),
    ("netclass:Default", "via_drill", 0.3, 0.2),
}


def _loosened_key(item: dict) -> tuple:
    return (item.get("where"), item.get("key"), item.get("before"), item.get("after"))


async def _call(client, tool: str, **kw) -> dict:
    r = await client.call_tool(tool, kw)
    if r.structured_content is not None:
        return r.structured_content
    return json.loads(r.content[0].text)


def _short(d: dict) -> dict:
    """Keep the record readable: drop bulky net maps but keep every status and refusal."""
    drop = {"nets", "footprints", "baseline", "pads"}
    return {k: v for k, v in d.items() if k not in drop}


class Chain:
    def __init__(self) -> None:
        self.steps: list[dict] = []
        self.blocked: dict | None = None

    def add(self, step: str, tool: str, ok: bool, out: dict, **extra) -> bool:
        self.steps.append({"step": step, "tool": tool, "ok": ok, **extra, "result": _short(out)})
        if not ok and self.blocked is None:
            self.blocked = {"step": step, "tool": tool, "status": out.get("status"),
                            "reason": out.get("reason"), "result": _short(out)}
        return ok


async def flow() -> dict:
    from mcp.client.client import Client

    from kicad_mcp import raw_geometry, write_layer
    from kicad_mcp.server import build_server

    shutil.rmtree(RUN, ignore_errors=True)
    shutil.copytree(SOURCE, RUN / "src")
    ch = Chain()
    board = RUN / "src" / f"{NAME}.kicad_pcb"
    paths: dict[str, str] = {"source_copy": str(board)}
    baseline: dict = {}

    async def pair(c, step, prop, conf, outdir, pargs, cargs, good):
        nonlocal board
        p = await _call(c, prop, pcb_path=str(board), output_dir=str(RUN / outdir), **pargs)
        if p.get("status") == "nothing_to_do":  # a valid outcome (e.g. no copper text): no revision
            ch.add(step, prop, True, p, args=pargs, note="nothing_to_do: board unchanged")
            return {"status": "nothing_to_do"}
        if p.get("status") != "proposed" or not p.get("revision_token"):
            ch.add(step, prop, False, p, args=pargs)
            return None
        o = await _call(c, conf, pcb_path=str(board), revision_token=p["revision_token"], **cargs)
        ok = o.get("status") in good
        ch.add(step, f"{prop} / {conf}", ok, o, args=pargs, proposal=_short(p))
        if ok:
            board = Path(o.get("output_pcb") or o["pcb_path"])
            paths[step] = str(board)
        return o

    async with Client(build_server(), mode="auto") as c:
        o = await pair(c, "import_from_schematic", "propose_schematic_import", "confirm_schematic_import",
                       "01_import", {}, {}, {"imported"})
        if o:
            ox, oy = OX, OY
            o = await pair(c, "board_outline", "propose_board_outline", "confirm_board_outline", "02_outline",
                           dict(outline={"x": ox, "y": oy, "width": W, "height": H},
                                mounting_holes=[{"reference": r, "x": ox + x, "y": oy + y,
                                                 "keepout_radius_mm": HOLE_KEEPOUT,
                                                 "footprint": "MountingHole:MountingHole_2.2mm_M2"}
                                                for r, x, y in HOLES]),
                           {}, {"built"})
        if o:
            # placement toolset is lazy-loaded; autoplacement is proposed (never confirmed:
            # its row-packing ignores the outline) and recorded, then place_footprints places.
            await _call(c, "load_toolset", name="placement")
            ap = await _call(c, "propose_autoplacement", pcb_path=str(board))
            ch.add("autoplacement_proposal_only", "propose_autoplacement", ap.get("status") == "pending_confirmation",
                   {"status": ap.get("status"), "proposed_positions": ap.get("proposed_positions"),
                    "note": "not confirmed: heuristic rows start at (0,0) and ignore the outline"})
            # copy the revision first: place_footprints edits in place
            placed_dir = RUN / "03_placed"
            shutil.copytree(board.parent, placed_dir)
            pboard = placed_dir / board.name
            # place_footprints takes BOARD-LOCAL mm (it adds the board origin itself; measured:
            # passing page coordinates double-offsets). The result is checked against the raw
            # (at ...) nodes in page coordinates, never against the tool's return.
            # C3 is derived from the raw geometry of the outlined board (ticket 114), never typed
            c3 = plan_decoupling_capacitor(write_layer.load(board), LAYOUT)
            layout = {**LAYOUT, DECOUPLING["cap"]: (c3["x"], c3["y"])}
            rotation = {DECOUPLING["cap"]: c3["rotation"]}
            specs = [{"reference": r, "x": x, "y": y, "rotation": rotation.get(r, 0.0)} for r, (x, y) in layout.items()]
            pr = await _call(c, "place_footprints", pcb_path=str(pboard), specs=specs)
            raw = raw_positions(pboard)
            want = {r: (round(OX + x, 3), round(OY + y, 3)) for r, (x, y) in layout.items()}
            wrong = {r: {"raw_at": raw.get(r), "want": w} for r, w in want.items() if raw.get(r) != w}
            raw_angles = {g.reference: g.angle_deg for g in raw_geometry.read_footprints(pboard)}
            wrong_angle = {r: {"raw_angle": raw_angles.get(r), "want": rotation.get(r, 0.0)}
                           for r in layout if raw_angles.get(r) != rotation.get(r, 0.0)}
            findings = decoupling_findings(write_layer.load(pboard))
            ok = (pr.get("status") == "ok" and sorted(pr.get("moved", [])) == sorted(layout)
                  and not wrong and not wrong_angle and not findings)
            pr = {**pr, "raw_position_mismatches": wrong, "raw_rotation_mismatches": wrong_angle,
                  "decoupling_rule_findings": findings, "decoupling_facts": decoupling_facts(write_layer.load(pboard)),
                  "supply_capacitor_distances": supply_capacitor_distances(write_layer.load(pboard))}
            ch.add("placement", "place_footprints", ok, pr, args={"layout_board_local_mm": layout, "rotation_deg": rotation,
                                                                  "c3_plan": c3})
            if ok:
                board = pboard
                paths["placement"] = str(board)
            else:
                o = None
        if o:
            # Probe WITHOUT the flag: a refusal lists every loosened item. The flag is set only
            # when that list is a subset of the owner-approved list; otherwise the chain stops.
            probe = await _call(c, "propose_board_rules", pcb_path=str(board), output_dir=str(RUN / "04_rules"), **RULES)
            loosened = {_loosened_key(i) for i in probe.get("loosened", [])}
            extra_items = sorted(map(str, loosened - APPROVED_LOOSENED))
            approval = {"approved_by": "owner, 2026-10-11, in session", "approved_list": sorted(map(str, APPROVED_LOOSENED)),
                        "loosened_seen": sorted(map(str, loosened)), "outside_approval": extra_items,
                        "incoherent": probe.get("incoherent"), "incoherent_existing": probe.get("incoherent_existing")}
            if probe.get("status") == "proposed" and probe.get("revision_token"):
                flag = False  # nothing loosens: no flag needed
                p = probe
            elif probe.get("status") == "refused" and loosened and not extra_items and not probe.get("incoherent"):
                flag = True
                p = await _call(c, "propose_board_rules", pcb_path=str(board), output_dir=str(RUN / "04_rules"),
                                allow_loosen=True, **RULES)
            else:
                ch.add("board_rules", "propose_board_rules", False, probe, approval=approval)
                p = None
            if p is not None:
                approval["allow_loosen_passed"] = flag
                if p.get("status") != "proposed" or not p.get("revision_token"):
                    ch.add("board_rules", "propose_board_rules", False, p, approval=approval)
                    o = None
                else:
                    o = await _call(c, "confirm_board_rules", pcb_path=str(board),
                                    revision_token=p["revision_token"], parity="required")
                    ok = o.get("status") == "applied"
                    ch.add("board_rules", "propose_board_rules / confirm_board_rules", ok, o, args=RULES,
                           proposal=_short(p), approval=approval)
                    if ok:
                        board = Path(o.get("output_pcb") or o["pcb_path"])
                        paths["board_rules"] = str(board)
                        baseline = o.get("baseline", {})
                    else:
                        o = None
            else:
                o = None
        if o:
            o = await pair(c, "input_preparation", "propose_input_preparation", "confirm_input_preparation",
                           "05_prepared", {}, {"parity": "required"}, {"prepared"})
            if o:
                baseline = o.get("baseline", baseline)
        if o is None and any(s["step"] == "input_preparation" and not s["ok"] for s in ch.steps):
            pass
        if ch.blocked is None:
            o = await pair(c, "silkscreen_minimums", "propose_silkscreen_minimums", "confirm_silkscreen_minimums",
                           "06_silk", {}, {}, {"applied"})
        if ch.blocked is None:
            pin = await _call(c, "pin_rule_baseline", pcb_path=str(board))
            baseline = pin.get("baseline", pin)
            ch.add("pin_rule_baseline", "pin_rule_baseline", bool(baseline), pin)
        if ch.blocked is None:
            o = await pair(c, "pour_step", "propose_pour_step", "confirm_pour_step", "07_poured",
                           {"pours": {"GND": "B.Cu"}},
                           {"parity": "required", "baseline": baseline}, {"poured"})
    return {"steps": ch.steps, "blocked": ch.blocked, "paths": paths, "final_board": str(board)}


def measure(board: str) -> dict:
    out = {}
    for backend in ("krt", "freerouting"):
        t0 = time.time()
        cp = subprocess.run(
            [sys.executable, "-m", "kicad_mcp.autonomy_run", "--rung", "1", "--backend", backend,
             "--board", board, "--parity", "required"],
            capture_output=True, text=True, timeout=3 * 1800 + 600,
        )
        out[backend] = {"exit_code": cp.returncode, "wall_s": round(time.time() - t0, 1),
                        "stdout": cp.stdout[-20000:], "stderr": cp.stderr[-4000:]}
    return out


if __name__ == "__main__":
    rec = asyncio.run(flow())
    rec["origin_page_mm"] = [OX, OY]
    if "--measure" in sys.argv and rec["blocked"] is None:
        rec["measurement"] = measure(rec["final_board"])
    text = json.dumps(rec, indent=1, default=str)
    text = text.replace(json.dumps(str(HERE.parent.parent))[1:-1], "<repo>")  # no machine path in the record
    text = re.sub(r"[A-Za-z]:(?:\\\\|/)Users(?:\\\\|/)[^\\/\"]+", "<home>", text)  # any other home path (temp dirs)
    RECORD.write_text(text, encoding="utf-8")
    print(json.dumps({"blocked": rec["blocked"], "paths": rec["paths"],
                      "steps": [(s["step"], s["ok"]) for s in rec["steps"]]}, indent=1, default=str))
