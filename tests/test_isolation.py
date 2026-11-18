"""Keepout zones and slots (src/boardproof/isolation.py, GitHub issue 132).

The oracle is kicad-cli 10.0.7: DRC for a track inside a keepout and for the outline, the gerber
export for the slot. Fixtures are the golden board (outline (123.5, 85) to (173.5, 125); C1 at
(133.5, 93), D1 (163.5, 103), J1 (128.5, 105), J2 (163.5, 117), R1 (163.5, 95), U1 (148.5, 105))
plus copper added through the write layer's tree API. Positions are literals; everything read back
is read from the raw tokens of the saved file.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from boardproof import _self_check, edge_loops, isolation, kicad_cli, write_layer
from tests import _kicad_faults as faults
from tests._self_check_helpers import corrupt_writes, footprint_reference, in_revision_tmp, sexp_edit, top_nodes
from tests.test_edge_loops import rect_lines

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"

CLEAN = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}

MAINS = {"name": "MAINS", "rect": {"x": 138, "y": 95, "width": 20, "height": 5}, "layers": ["F.Cu"]}
SLOT = {"x1": 140, "y1": 112, "x2": 156, "y2": 112, "width_mm": 1.5, "ends": "round"}
SQUARE = {"x1": 169, "y1": 100, "x2": 169, "y2": 110, "width_mm": 1.0, "ends": "square"}


@pytest.fixture(autouse=True)
def _needs_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def seg(a, b, layer="F.Cu", net=1, width=0.25):
    return SExp.list("segment", SExp.list("start", *a), SExp.list("end", *b), SExp.list("width", width),
                     SExp.list("layer", layer), SExp.list("net", net), SExp.list("uuid", str(uuid.uuid4())))


def via(at, net=1):
    return SExp.list("via", SExp.list("at", *at), SExp.list("size", 0.6), SExp.list("drill", 0.3),
                     SExp.list("layers", "F.Cu", "B.Cu"), SExp.list("net", net), SExp.list("uuid", str(uuid.uuid4())))


def pour(layer="B.Cu"):
    pts = SExp.list("pts")
    for x, y in [(123.5, 85), (173.5, 85), (173.5, 125), (123.5, 125)]:
        pts.append(SExp.list("xy", x, y))
    return SExp.list("zone", SExp.list("net", 1), SExp.list("net_name", "GND"), SExp.list("layer", layer),
                     SExp.list("uuid", str(uuid.uuid4())), SExp.list("hatch", "edge", 0.5),
                     SExp.list("connect_pads", SExp.list("clearance", 0.3)), SExp.list("min_thickness", 0.25),
                     SExp.list("fill", "yes", SExp.list("thermal_gap", 0.5), SExp.list("thermal_bridge_width", 0.5)),
                     SExp.list("polygon", pts))


def build(tmp_path: Path, nodes=(), name="proj") -> Path:
    shutil.copytree(GOLDEN_DIR, tmp_path / name)
    pcb = tmp_path / name / GOLDEN_NAME
    if nodes:
        root = write_layer.load(pcb)
        for node in nodes:
            root.append(node)
        write_layer.save(root, pcb)
    return pcb


def digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def propose(pcb: Path, tmp_path: Path, **kw) -> dict:
    return isolation.propose_keepouts_and_slots(pcb, output_dir=tmp_path / "out", **kw)


def apply(pcb: Path, tmp_path: Path, drc=None, **kw) -> dict:
    proposal = propose(pcb, tmp_path, **kw)
    assert proposal["status"] == "proposed", proposal
    return isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=drc)


def drc_report(pcb: Path, tmp_path: Path) -> dict:
    out = tmp_path / f"drc-{pcb.parent.name}.json"
    subprocess.run([str(kicad_cli.find_kicad_cli()), "pcb", "drc", "--format", "json", "--severity-all", "--output", str(out), str(pcb)],
                   capture_output=True, text=True, check=True)
    return json.loads(out.read_text(encoding="utf-8"))


def types(report: dict) -> list[str]:
    return sorted(v["type"] for v in report["violations"])


# --------------------------------------------------------------------------- the dry run


def test_propose_writes_nothing_and_lists_what_it_would_add(tmp_path):
    pcb = build(tmp_path)
    before = digest(tmp_path)
    out = propose(pcb, tmp_path, keepouts=[MAINS], slots=[SLOT, SQUARE])
    assert out["status"] == "proposed" and out["revision_token"]
    assert digest(tmp_path) == before
    assert not Path(out["output_pcb"]).exists() and not (tmp_path / "out").exists()
    assert [k["name"] for k in out["keepouts_add"]] == ["MAINS"]
    assert out["keepouts_add"][0]["layers"] == ["F.Cu"]
    assert out["keepouts_add"][0]["rules"] == {"tracks": "not_allowed", "vias": "not_allowed", "pads": "allowed",
                                               "copperpour": "not_allowed", "footprints": "allowed"}
    assert [s["ends"] for s in out["slots_add"]] == ["round", "square"]
    assert out["keepouts_after"] == 1 and out["slots_after"] == 2


def test_nothing_asked_lists_the_boards_keepouts_and_slots(tmp_path):
    pcb = build(tmp_path, rect_lines(138, 108, 158, 110) + [isolation._keepout_node(  # a keepout written by the module's own node builder
        isolation._norm_keepout(MAINS, ["F.Cu", "B.Cu"])[0])])
    out = isolation.propose_keepouts_and_slots(pcb)
    assert out["status"] == "nothing_to_do" and out["revision_token"] is None
    assert [k["name"] for k in out["existing_keepouts"]["items"]] == ["MAINS"]
    assert [s["id"] for s in out["existing_slots"]["items"]] == ["rect-138-108-0"]


def test_the_same_request_gives_the_same_ids_so_a_replan_equals_the_proposal(tmp_path):
    pcb = build(tmp_path)
    a = isolation.propose_keepouts_and_slots(pcb, keepouts=[MAINS], slots=[SLOT], output_dir=tmp_path / "o1")
    b = isolation.propose_keepouts_and_slots(pcb, keepouts=[MAINS], slots=[SLOT], output_dir=tmp_path / "o2")
    assert a["slots_add"][0]["id"] == b["slots_add"][0]["id"]
    assert a["keepouts_add"][0]["uuid"] == b["keepouts_add"][0]["uuid"]
    assert a["revision_token"] != b["revision_token"]


# --------------------------------------------------------------------------- acceptance: kicad-cli is the oracle


def test_confirm_writes_a_revision_kicad_accepts_and_leaves_the_source_alone(tmp_path):
    pcb = build(tmp_path)
    before = digest(pcb.parent)
    built = apply(pcb, tmp_path, keepouts=[MAINS], slots=[SLOT, SQUARE])
    assert built["status"] == "built", built
    assert digest(pcb.parent) == before
    revision = Path(built["output_pcb"])
    assert revision != pcb and revision.is_file()
    root = write_layer.load(revision)
    outline = edge_loops.classify(root)
    assert outline.problems == () and len(outline.slots) == 2
    assert outline.outer.box == (123.5, 85.0, 173.5, 125.0)
    stadium = next(s for s in outline.slots if s.kinds[1] == "gr_arc")
    x0, y0, x1, y1 = stadium.box
    assert (round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)) == (139.25, 111.25, 156.75, 112.75)
    zones = [z for z in top_nodes(root, "zone") if z.find("keepout") is not None]
    assert len(zones) == 1 and zones[0].find("name").values[0] == "MAINS"
    assert [str(v) for v in zones[0].find("layers").values] == ["F.Cu"]
    assert types(drc_report(revision, tmp_path)) == types(drc_report(pcb, tmp_path))  # the source's own findings, nothing new
    assert built["drc_watched"]["invalid_outline"] == {"before": 0, "after": 0}


def test_acceptance_a_track_inside_the_keepout_is_reported_by_kicad_cli_on_its_layer_only(tmp_path):
    revision = Path(apply(build(tmp_path), tmp_path, keepouts=[MAINS])["output_pcb"])
    root = write_layer.load(revision)
    root.append(seg((140, 97), (150, 97)))  # F.Cu, inside MAINS
    root.append(seg((140, 98), (150, 98), layer="B.Cu"))  # B.Cu: the keepout lists F.Cu only
    root.append(via((155, 97)))
    write_layer.save(root, revision)
    report = drc_report(revision, tmp_path)
    flagged = [v for v in report["violations"] if v["type"] == "items_not_allowed"]
    assert len(flagged) == 2  # the F.Cu track and the via
    descriptions = " | ".join(i["description"] for v in flagged for i in v["items"])
    assert "F.Cu" in descriptions and "B.Cu," not in descriptions.replace("F.Cu - B.Cu", "")


def test_acceptance_the_gerber_export_contains_the_slot(tmp_path):
    pcb = build(tmp_path)
    revision = Path(apply(pcb, tmp_path, slots=[SLOT])["output_pcb"])

    def gerber(board: Path) -> str:
        out = tmp_path / f"gb-{board.parent.name}"
        out.mkdir()
        subprocess.run([str(kicad_cli.find_kicad_cli()), "pcb", "export", "gerbers", "--layers", "Edge.Cuts", "--output", str(out) + "/", str(board)],
                       capture_output=True, text=True, check=True)
        return next(out.glob("*Edge_Cuts.gm1")).read_text(encoding="utf-8")

    plain, slotted = gerber(pcb), gerber(revision)
    assert "Y-112750000" not in plain
    # the long sides of the 1.5 mm stadium centred on y = 112 run at y = 111.25 and 112.75; the caps are G75 arcs
    assert "X140000000Y-112750000" in slotted and "X156000000Y-112750000" in slotted
    assert "X156000000Y-111250000" in slotted
    assert slotted.count("G75*") == 2  # two arc caps
    assert "X123500000Y-85000000" in slotted  # the outer outline is still drawn


def test_acceptance_the_specctra_export_hands_the_router_the_slot_and_the_keepout(tmp_path):
    from boardproof.toolsets import routing

    revision = Path(apply(build(tmp_path), tmp_path, keepouts=[MAINS], slots=[SQUARE])["output_pcb"])
    dsn = tmp_path / "out.dsn"
    result = routing.run_kicad_specctra("export", revision, dsn)
    assert result.get("ok") is True, result
    text = dsn.read_text(encoding="utf-8", errors="replace")
    assert text.count("(keepout") == 2
    assert "(keepout \"\" (polygon F.Cu 0  138000 -95000" in text  # the rule area, on F.Cu only
    assert "(polygon signal 0" in text  # the slot, on every signal layer


def test_a_copper_pour_is_cut_back_from_slots_and_keepouts_by_kicad(tmp_path):
    revision = Path(apply(build(tmp_path, [pour()]), tmp_path, keepouts=[{**MAINS, "layers": ["B.Cu"]}], slots=[SLOT])["output_pcb"])
    proposal = propose(build(tmp_path, [pour()], name="proj2"), tmp_path / "x", keepouts=[{**MAINS, "layers": ["B.Cu"]}], slots=[SLOT])
    assert proposal["zone_overlaps"]["count"] == 2 and "pour_note" in proposal  # named, with the refill hint
    subprocess.run([str(kicad_cli.find_kicad_cli()), "pcb", "drc", "--refill-zones", "--save-board", "--format", "json", "--output",
                    str(tmp_path / "r.json"), str(revision)], capture_output=True, text=True, check=True)
    zone = next(z for z in top_nodes(write_layer.load(revision), "zone") if z.find("keepout") is None)
    fills = [[(float(c.values[0]), float(c.values[1])) for c in f.find("pts").children if c.name == "xy"] for f in zone.children if f.name == "filled_polygon"]
    assert fills
    for box in ((138, 95, 158, 100), (140, 111.25, 156, 112.75)):  # the keepout, and the straight part of the stadium
        inside = [p for pts in fills for p in pts if box[0] + 0.01 < p[0] < box[2] - 0.01 and box[1] + 0.01 < p[1] < box[3] - 0.01]
        assert inside == [], f"copper was left inside {box}"


# --------------------------------------------------------------------------- the outline checks of this project


def test_a_slot_does_not_break_the_rectangular_outline_checks_and_parts_are_kept_out_of_it(tmp_path):
    from mcp.server.mcpserver import MCPServer

    from boardproof import placement_anchor
    from boardproof.toolsets import placement as placement_toolset

    revision = Path(apply(build(tmp_path), tmp_path, slots=[SQUARE, SLOT])["output_pcb"])
    outline, problem = placement_anchor.outline_rect(write_layer.load(revision))
    assert problem is None and outline.box == (123.5, 85.0, 173.5, 125.0)
    assert sorted(outline.cutouts) == [(139.25, 111.25, 156.75, 112.75), (168.5, 100.0, 169.5, 110.0)]

    mcp = MCPServer("t")
    placement_toolset.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    # R1 to the right edge at the height of the SQUARE slot: the courtyard would overlap the slot
    refused = tools["place_footprints_at_edge"](str(revision), [{"reference": "R1", "edge": "right", "offset_mm": 4.5, "along_mm": 25.0}])
    assert refused["status"] == "error" and refused["code"] == "in_cutout", refused
    ok = tools["place_footprints_at_edge"](str(revision), [{"reference": "R1", "edge": "right", "offset_mm": 1.0, "along_mm": 5.0}])
    assert ok["status"] != "error" or ok.get("code") != "outline_not_rectangular", ok
    assert ok["status"] == "ok", ok


def test_autoplacement_packs_around_a_slot_not_over_it(tmp_path):
    from mcp.server.mcpserver import MCPServer

    from boardproof import placement_anchor
    from boardproof.toolsets import placement as placement_toolset

    mcp = MCPServer("t")
    placement_toolset.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]

    def boxes(board: Path) -> dict[str, tuple]:
        proposal = tools["propose_autoplacement"](str(board), ["C1", "R1", "D1"])
        assert proposal["status"] == "pending_confirmation", proposal
        root = write_layer.load(board)
        outline, _ = placement_anchor.outline_rect(root)
        parts = {p.reference: p for p in placement_anchor.read_parts(root)}
        return {
            ref: placement_anchor.shift(parts[ref].box, pos["x"] + outline.box[0] - parts[ref].at[0], pos["y"] + outline.box[1] - parts[ref].at[1])
            for ref, pos in proposal["proposed_positions"].items()
        }

    barrier = {"x1": 125, "y1": 88, "x2": 140, "y2": 88, "width_mm": 4.0, "ends": "square"}  # the top-left corner, where first-fit starts
    slot_box = (125.0, 86.0, 140.0, 90.0)
    plain = boxes(build(tmp_path, name="plain"))
    assert any(placement_anchor.overlap(b, slot_box) for b in plain.values())  # without the slot the parts would sit there
    revision = Path(apply(build(tmp_path), tmp_path, slots=[barrier])["output_pcb"])
    slotted = boxes(revision)
    assert not any(placement_anchor.overlap(b, slot_box) for b in slotted.values())


def test_propose_board_outline_still_refuses_a_board_with_slots_and_names_them(tmp_path):
    from boardproof import board_outline

    revision = Path(apply(build(tmp_path), tmp_path, slots=[SLOT])["output_pcb"])
    refused = board_outline.propose_board_outline(revision, {"x": 100.0, "y": 70.0, "width": 100.0, "height": 70.0}, output_dir=tmp_path / "o")
    assert refused["status"] == "refused" and "cutouts" in refused  # documented: replacing the outline would delete the slot
    assert {c["uuid"] for c in refused["cutouts"]} >= {edge_loops.classify(write_layer.load(revision)).slots[0].id}


# --------------------------------------------------------------------------- refusals


def test_a_slot_over_pads_is_refused_although_kicad_drc_would_not_notice(tmp_path):
    pcb = build(tmp_path)
    over_u1 = {"x1": 146, "y1": 105, "x2": 151, "y2": 105, "width_mm": 2.0, "ends": "square"}
    out = propose(pcb, tmp_path, slots=[over_u1])
    assert out["status"] == "refused" and out["revision_token"] is None
    kinds = {c["kind"] for c in out["conflicts"]["items"]}
    assert {"pad", "courtyard"} <= kinds
    assert any(c["item"].startswith("U1") for c in out["conflicts"]["items"])


def test_a_slot_over_a_track_or_via_inside_it_is_refused_the_oracle_does_not_flag_them(tmp_path):
    pcb = build(tmp_path, [seg((142, 112), (150, 112)), via((153, 112))])  # wholly inside the slot
    out = propose(pcb, tmp_path, slots=[SLOT])
    kinds = sorted(c["kind"] for c in out["conflicts"]["items"])
    assert out["status"] == "refused" and kinds == ["track", "via"], out
    # and the oracle really is blind to them: with the slot written by hand, DRC has no edge error
    blind = build(tmp_path, [seg((142, 112), (150, 112))] + rect_lines(140, 111.25, 156, 112.75), name="blind")
    assert "copper_edge_clearance" not in types(drc_report(blind, tmp_path))


def test_a_slot_too_close_to_copper_is_refused_by_the_margin_and_passes_with_a_smaller_one(tmp_path):
    pcb = build(tmp_path, [seg((140, 109.3), (156, 109.3))])  # 0.3 mm above the slot's upper edge (111.25 - 109.3 - 0.125 = 1.8 mm?)
    near = {"x1": 140, "y1": 110, "x2": 156, "y2": 110, "width_mm": 1.0, "ends": "square"}  # edge at 109.5: 0.075 mm from the track's edge
    assert propose(pcb, tmp_path, slots=[near])["status"] == "refused"
    assert propose(pcb, tmp_path, slots=[near], copper_margin_mm=0.0)["status"] == "proposed"


@pytest.mark.parametrize(
    "slot",
    [
        {"x1": 123.7, "y1": 100, "x2": 140, "y2": 100, "width_mm": 1.0, "ends": "square"},  # 0.2 mm web to the left edge
        {"x1": 124.2, "y1": 100, "x2": 140, "y2": 100, "width_mm": 1.0, "ends": "round"},  # round cap 0.2 mm from it
        {"x1": 120, "y1": 100, "x2": 140, "y2": 100, "width_mm": 1.0, "ends": "square"},  # crosses the outline
        {"x1": 150, "y1": 80, "x2": 160, "y2": 80, "width_mm": 1.0, "ends": "square"},  # wholly outside
    ],
)
def test_a_slot_that_reaches_the_outline_or_lies_outside_it_is_refused(tmp_path, slot):
    out = propose(build(tmp_path), tmp_path, slots=[slot])
    assert out["status"] == "refused"
    assert out["conflicts"]["items"][0]["kind"] == "outline"
    assert "propose_board_outline" in out["conflicts"]["items"][0]["reason"]


def test_slots_that_touch_nest_or_come_closer_than_the_margin_are_refused(tmp_path):
    pcb = build(tmp_path, rect_lines(138, 108, 158, 110))
    for slot in (
        {"x1": 140, "y1": 109, "x2": 150, "y2": 109, "width_mm": 0.6, "ends": "square"},  # inside the existing slot
        {"x1": 140, "y1": 110.2, "x2": 150, "y2": 110.2, "width_mm": 0.6, "ends": "square"},  # 0.0 mm
        {"x1": 140, "y1": 110.5, "x2": 150, "y2": 110.5, "width_mm": 0.6, "ends": "square"},  # 0.2 mm
    ):
        out = propose(pcb, tmp_path, slots=[slot])
        assert out["status"] == "refused" and out["conflicts"]["items"][0]["kind"] == "slot", slot
    two_new = propose(build(tmp_path, name="p2"), tmp_path, slots=[SLOT, {**SLOT, "x1": 142, "x2": 150}])
    assert two_new["status"] == "refused" and two_new["conflicts"]["items"][0]["reason"].endswith("another new slot")


def test_a_keepout_over_items_its_rules_forbid_is_refused_and_over_allowed_ones_is_not(tmp_path):
    pcb = build(tmp_path, [seg((140, 97), (150, 97)), via((155, 97)), seg((141, 98), (149, 98), layer="B.Cu")])
    out = propose(pcb, tmp_path, keepouts=[MAINS])
    assert out["status"] == "refused"
    assert sorted(c["kind"] for c in out["conflicts"]["items"]) == ["track", "via"]  # not the B.Cu track: MAINS lists F.Cu
    only_pour = propose(pcb, tmp_path, keepouts=[{**MAINS, "rules": {"tracks": "allowed", "vias": "allowed"}}])
    assert only_pour["status"] == "proposed"  # copperpour alone forbids only pours: the track and via stay legal
    assert propose(pcb, tmp_path, keepouts=[{**MAINS, "layers": ["B.Cu"]}])["conflicts"]["items"][0]["layer"] == "B.Cu"
    pads_only = {"name": "NOPADS", "rect": {"x": 143, "y": 100, "width": 11, "height": 10}, "layers": ["F.Cu"], "rules": {"tracks": "allowed", "vias": "allowed", "copperpour": "allowed", "pads": "not_allowed"}}
    out = propose(pcb, tmp_path, keepouts=[pads_only])
    assert out["status"] == "refused" and {c["kind"] for c in out["conflicts"]["items"]} == {"pad"}
    fps = {**pads_only, "name": "NOFP", "rules": {"tracks": "allowed", "vias": "allowed", "copperpour": "allowed", "footprints": "not_allowed"}}
    out = propose(pcb, tmp_path, keepouts=[fps])
    assert {c["kind"] for c in out["conflicts"]["items"]} == {"footprint"} and out["conflicts"]["items"][0]["item"] == "U1"
    fps_back = {**fps, "layers": ["B.Cu"]}
    assert propose(pcb, tmp_path, keepouts=[fps_back])["status"] == "proposed"  # U1 is on the front


def test_there_is_no_override_in_the_tool_signature():
    import inspect

    for fn in (isolation.propose_keepouts_and_slots, isolation.confirm_keepouts_and_slots):
        names = set(inspect.signature(fn).parameters)
        assert not {"force", "allow_overlap", "allow_conflicts", "override"} & names


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"keepouts": [{"rect": {"x": 1, "y": 1, "width": 2, "height": 2}}]}, "name"),
        ({"keepouts": [{"name": "A\nB", "rect": {"x": 1, "y": 1, "width": 2, "height": 2}}]}, "name"),
        ({"keepouts": [{"name": "A", "rect": {"x": 1, "y": 1, "width": 2, "height": 2}, "polygon": [[0, 0], [1, 0], [1, 1]]}]}, "exactly one"),
        ({"keepouts": [{"name": "A"}]}, "exactly one"),
        ({"keepouts": [{"name": "A", "rect": {"x": 1, "y": 1, "width": 0, "height": 2}}]}, "positive"),
        ({"keepouts": [{"name": "A", "polygon": [[0, 0], [1, 1], [1, 0], [0, 1]]}]}, "crosses itself"),
        ({"keepouts": [{"name": "A", "polygon": [[0, 0], [1, 0]]}]}, "3 to"),
        ({"keepouts": [{"name": "A", "rect": {"x": 1, "y": 1, "width": 2, "height": 2}, "layers": ["F.SilkS"]}]}, "not copper layers"),
        ({"keepouts": [{"name": "A", "rect": {"x": 1, "y": 1, "width": 2, "height": 2}, "layers": []}]}, "layers must be"),
        ({"keepouts": [{"name": "A", "rect": {"x": 1, "y": 1, "width": 2, "height": 2}, "rules": {"tracks": "maybe"}}]}, "rules must map"),
        ({"keepouts": [{"name": "A", "rect": {"x": 1, "y": 1, "width": 2, "height": 2}, "rules": {"tracks": "allowed", "vias": "allowed", "copperpour": "allowed"}}]}, "forbids nothing"),
        ({"keepouts": [{"name": "A", "rect": {"x": 1, "y": 1, "width": 2, "height": 2}, "extra": 1}]}, "unknown key"),
        ({"keepouts": [{"name": "A", "rect": {"x": float("nan"), "y": 1, "width": 2, "height": 2}}]}, "numeric"),
        ({"keepouts": [MAINS, MAINS]}, "declared twice"),
        ({"slots": [{**SLOT, "width_mm": 0.2}]}, "at least"),
        ({"slots": [{**SLOT, "ends": "pointed"}]}, "ends must be"),
        ({"slots": [{**SLOT, "x2": 140.01}]}, "closer than 0.1"),
        ({"slots": [{**SLOT, "y1": "112"}]}, "y1"),
        ({"slots": [{**SLOT, "y1": True}]}, "y1"),
        ({"slots": [{**SLOT, "zzz": 1}]}, "unknown key"),
        ({"slots": ["slot"]}, "object"),
        ({"keepouts": "MAINS"}, "list"),
        ({"remove_keepouts": ["NOPE"]}, "0 keepout zones"),
        ({"remove_slots": ["no-such-id"]}, "no slot has the id"),
        ({"edge_margin_mm": -1, "slots": [SLOT]}, "edge_margin_mm"),
        ({"copper_margin_mm": float("inf"), "slots": [SLOT]}, "copper_margin_mm"),
    ],
)
def test_bad_input_is_refused_with_a_reason_and_no_token(tmp_path, kw, needle):
    pcb = build(tmp_path)
    before = digest(tmp_path)
    out = propose(pcb, tmp_path, **kw)
    assert out["status"] == "refused" and out["revision_token"] is None, out
    assert needle in out["reason"], out["reason"]
    assert digest(tmp_path) == before


def test_slots_are_not_touched_on_a_board_whose_outline_is_not_understood(tmp_path):
    pcb = build(tmp_path, rect_lines(138, 108, 158, 110)[:3])  # an open chain
    out = propose(pcb, tmp_path, slots=[SLOT])
    assert out["status"] == "refused" and "open chain" in out["reason"]
    assert propose(pcb, tmp_path, keepouts=[MAINS])["status"] == "proposed"  # keepouts do not depend on the outline


# --------------------------------------------------------------------------- removal


def test_a_slot_and_a_keepout_are_removed_by_id_and_name_and_a_name_can_be_reused(tmp_path):
    first = apply(build(tmp_path), tmp_path, keepouts=[MAINS], slots=[SLOT, SQUARE])
    assert first["status"] == "built"
    rev1 = Path(first["output_pcb"])
    listing = isolation.propose_keepouts_and_slots(rev1)
    ids = {s["box"][0]: s["id"] for s in listing["existing_slots"]["items"]}
    second = isolation.propose_keepouts_and_slots(
        rev1, remove_slots=[ids[139.25]], remove_keepouts=["MAINS"], keepouts=[{**MAINS, "layers": ["B.Cu"]}], output_dir=tmp_path / "out2"
    )
    assert second["status"] == "proposed", second
    assert second["slots_after"] == 1 and second["keepouts_after"] == 1
    done = isolation.confirm_keepouts_and_slots(rev1, second["revision_token"])
    assert done["status"] == "built", done
    root = write_layer.load(Path(done["output_pcb"]))
    outline = edge_loops.classify(root)
    assert [s.box for s in outline.slots] == [(168.5, 100.0, 169.5, 110.0)]
    (zone,) = [z for z in top_nodes(root, "zone") if z.find("keepout") is not None]
    assert [str(v) for v in zone.find("layers").values] == ["B.Cu"]
    assert not any(n.find("uuid") is not None and str(n.find("uuid").values[0]) == ids[139.25] for n in root.children if not n.is_atom)


def test_removing_a_name_that_two_keepouts_share_is_refused(tmp_path):
    twin = isolation._keepout_node(isolation._norm_keepout(MAINS, ["F.Cu", "B.Cu"])[0])
    other = isolation._keepout_node({**isolation._norm_keepout(MAINS, ["F.Cu", "B.Cu"])[0], "uuid": str(uuid.uuid4())})
    out = propose(build(tmp_path, [twin, other]), tmp_path, remove_keepouts=["MAINS"])
    assert out["status"] == "refused" and "2 keepout zones are named" in out["reason"]


# --------------------------------------------------------------------------- checkpoint behaviour


def test_a_changed_board_voids_the_token(tmp_path):
    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])
    faults.drift(pcb)
    out = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"])
    assert out["status"] == "error" and "changed since the proposal" in out["reason"]
    assert isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"])["reason"].startswith("unknown or expired")
    assert not (tmp_path / "out").exists()


def test_a_replayed_token_and_a_wrong_path_are_refused(tmp_path):
    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])
    other = build(tmp_path, name="other")
    assert "does not match" in isolation.confirm_keepouts_and_slots(other, proposal["revision_token"])["reason"]
    assert isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"])["status"] == "built"
    assert isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"])["reason"].startswith("unknown or expired")


def test_an_existing_output_directory_is_refused_at_proposal_and_inside_the_project_too(tmp_path):
    pcb = build(tmp_path)
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "x").write_text("x")
    assert propose(pcb, tmp_path, slots=[SLOT])["status"] == "error"
    inside = isolation.propose_keepouts_and_slots(pcb, slots=[SLOT], output_dir=pcb.parent / "sub")
    assert inside["status"] == "error" and "inside the source project" in inside["reason"]


def test_confirm_replans_and_keeps_the_token_when_a_new_item_would_now_conflict_is_covered_by_drift(tmp_path):
    # a track added after the proposal changes the project hash: the token is spent by the drift guard
    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])
    root = write_layer.load(pcb)
    root.append(seg((142, 112), (150, 112)))
    write_layer.save(root, pcb)
    out = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"])
    assert out["status"] == "error" and not (tmp_path / "out").exists()


def test_a_lock_file_refuses_the_confirm_and_keeps_the_token(tmp_path):
    from boardproof import kicad_lock

    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])
    lock = pcb.parent / f"~{pcb.name}.lck"
    assert kicad_lock.is_lock_name(lock.name)
    lock.write_text("{}")
    out = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"])
    assert out["ok"] is False and out["code"] == "file_open_in_kicad", out
    assert not (tmp_path / "out").exists()
    lock.unlink()
    assert isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"])["status"] == "built"


# --------------------------------------------------------------------------- the self-checks fire


def _planned_slot_nodes(root):
    ids = set(isolation._norm_slot(SLOT, 0)[0]["uuids"])
    return [n for n in root.children if not n.is_atom and n.find("uuid") is not None and str(n.find("uuid").values[0]) in ids]


def _drop_a_slot_line(root):
    root.remove(next(n for n in _planned_slot_nodes(root) if n.name == "gr_line"))


def _bend_a_cap(root):
    arc_node = next(n for n in _planned_slot_nodes(root) if n.name == "gr_arc")
    mid = arc_node.find("mid")
    mid.set_value(0, float(mid.values[0]) + 0.5)


def _move_the_slot(root):
    for n in _planned_slot_nodes(root):
        for tag in ("start", "mid", "end"):
            child = n.find(tag)
            if child is not None:
                child.set_value(1, float(child.values[1]) + 0.5)


def _drop_the_keepout(root):
    root.remove(next(z for z in top_nodes(root, "zone") if z.find("keepout") is not None))


def _flip_a_rule(root):
    zone = next(z for z in top_nodes(root, "zone") if z.find("keepout") is not None)
    zone.find("keepout").find("tracks").set_value(0, "allowed")


def _widen_the_layers(root):
    zone = next(z for z in top_nodes(root, "zone") if z.find("keepout") is not None)
    zone.find("layers").append(SExp.atom("B.Cu"))


def _shift_a_keepout_vertex(root):
    zone = next(z for z in top_nodes(root, "zone") if z.find("keepout") is not None)
    xy = zone.find("polygon").find("pts").children[0]
    xy.set_value(0, float(xy.values[0]) + 0.01)


def _drop_an_unrelated_footprint(root):
    root.remove(next(f for f in top_nodes(root, "footprint") if footprint_reference(f) != "U1"))


def _rename_the_keepout(root):
    zone = next(z for z in top_nodes(root, "zone") if z.find("keepout") is not None)
    zone.find("name").set_value(0, "OTHER")


SELF_CHECK_CASES = [
    pytest.param(_drop_a_slot_line, "Edge.Cuts after save+reload", id="slot-line-dropped"),
    pytest.param(_bend_a_cap, "planned shape", id="cap-bent"),
    pytest.param(_move_the_slot, "planned shape", id="slot-moved"),
    pytest.param(_drop_the_keepout, "keepout zones after reload", id="keepout-dropped"),
    pytest.param(_flip_a_rule, "keepout MAINS after reload", id="rule-flipped"),
    pytest.param(_widen_the_layers, "keepout MAINS after reload", id="layers-widened"),
    pytest.param(_shift_a_keepout_vertex, "keepout MAINS after reload", id="vertex-shifted"),
    pytest.param(_rename_the_keepout, "keepout zones after reload", id="keepout-renamed"),
]


@pytest.mark.parametrize("alone", [False, True], ids=["whole-file-compare", "structural-checks-alone"])
@pytest.mark.parametrize(("corrupt", "named"), SELF_CHECK_CASES)
def test_the_self_check_fires_on_a_corrupted_write(tmp_path, monkeypatch, corrupt, named, alone):
    """With ``alone`` the whole-file compare is switched off, so the structural checks (slot ids and
    shapes, keepout names, layers, rules, polygon) must catch the fault by themselves."""
    pcb = build(tmp_path)
    before = digest(tmp_path)
    proposal = propose(pcb, tmp_path, keepouts=[MAINS], slots=[SLOT])
    assert proposal["status"] == "proposed", proposal
    handle = corrupt_writes(monkeypatch, write_layer, "save", sexp_edit(corrupt), when=in_revision_tmp(tmp_path / "out"))
    if alone:
        monkeypatch.setattr(_self_check, "reload_differences", lambda **_k: {"missing": [], "unexpected": [], "tolerated": []})
    failed = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=lambda p, **_k: CLEAN)
    assert len(handle.hits) == 1
    assert failed["status"] == "error" and failed["ok"] is False and failed["code"] == "write_not_verified", failed
    assert failed["reason"].startswith("CRITICAL:")
    if alone:
        assert named in failed["reason"], failed["reason"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]  # no revision, no temporary left
    assert digest(tmp_path) == before
    handle.disarm()
    assert isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=lambda p, **_k: CLEAN)["status"] == "built"


def test_the_whole_file_compare_alone_catches_an_unrelated_node_lost(tmp_path, monkeypatch):
    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])
    handle = corrupt_writes(monkeypatch, write_layer, "save", sexp_edit(_drop_an_unrelated_footprint), when=in_revision_tmp(tmp_path / "out"))
    failed = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=lambda p, **_k: CLEAN)
    assert len(handle.hits) == 1 and failed["code"] == "write_not_verified"
    assert "footprint:" in json.dumps(failed["mismatches"])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


# --------------------------------------------------------------------------- the DRC guard


def _drc_that_grows(kind: str):
    def fake(path, **_k):
        grown = ".tmp-" in str(path)
        return {"violations": [{"type": kind, "severity": "error", "items": []}] if grown else [], "unconnected_items": []}

    return fake


@pytest.mark.parametrize("kind", ["invalid_outline", "malformed_outline", "copper_edge_clearance", "items_not_allowed"])
def test_a_revision_that_adds_an_outline_or_keepout_error_is_not_produced(tmp_path, kind):
    pcb = build(tmp_path)
    before = digest(tmp_path)
    proposal = propose(pcb, tmp_path, keepouts=[MAINS], slots=[SLOT])
    failed = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=_drc_that_grows(kind))
    assert failed["status"] == "error" and kind in failed["reason"] and failed["drc_growth"][kind] == {"before": 0, "after": 1}
    assert not (tmp_path / "out").exists() and digest(tmp_path) == before
    assert isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=lambda p, **_k: CLEAN)["status"] == "built"  # the token is kept


def test_findings_the_source_already_had_do_not_block_the_revision(tmp_path):
    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])
    old = {"violations": [{"type": "copper_edge_clearance", "severity": "error", "items": []}], "unconnected_items": []}
    done = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=lambda p, **_k: old)
    assert done["status"] == "built" and done["drc_watched"]["copper_edge_clearance"] == {"before": 1, "after": 1}


def test_a_drc_that_cannot_run_gives_no_revision(tmp_path):
    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])

    def broken(path, **_k):
        raise RuntimeError("kicad-cli crashed")

    out = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=broken)
    assert out["status"] == "incomplete" and "did not run" in out["reason"] and not (tmp_path / "out").exists()


def test_removals_alone_do_not_need_a_drc_run(tmp_path):
    first = apply(build(tmp_path), tmp_path, slots=[SLOT])
    rev = Path(first["output_pcb"])
    slot_id = isolation.propose_keepouts_and_slots(rev)["existing_slots"]["items"][0]["id"]
    proposal = isolation.propose_keepouts_and_slots(rev, remove_slots=[slot_id], output_dir=tmp_path / "o3")

    def must_not_run(path, **_k):
        raise AssertionError("no DRC is needed to take a slot away")

    done = isolation.confirm_keepouts_and_slots(rev, proposal["revision_token"], drc=must_not_run)
    assert done["status"] == "built" and edge_loops.classify(write_layer.load(Path(done["output_pcb"]))).slots == ()


def test_an_arc_in_the_chain_is_kept_as_an_arc_and_the_node_order_is_the_chain_order(tmp_path):
    revision = Path(apply(build(tmp_path), tmp_path, slots=[SLOT])["output_pcb"])
    (slot,) = edge_loops.classify(write_layer.load(revision)).slots
    assert slot.kinds == ("gr_line", "gr_arc", "gr_line", "gr_arc")
    assert slot.id == isolation._norm_slot(SLOT, 0)[0]["uuids"][0]


# --------------------------------------------------------------------------- the text an LLM reads


def test_the_skill_and_the_tool_descriptions_say_what_the_pair_does_not_check():
    import asyncio

    from mcp.client.client import Client

    from boardproof.server import build_server

    skill = " ".join((Path(__file__).parents[1] / ".claude" / "skills" / "kicad-pcb" / "SKILL.md").read_text(encoding="utf-8").split())
    for phrase in (
        "kicad-cli DRC does not see a track, via, pad or part that lies wholly inside a slot",
        "there is no override",
        "propose_board_outline",
        "The fab's own minimum slot width is not checked",
    ):
        assert phrase in skill, phrase

    async def descriptions():
        async with Client(build_server(), mode="auto") as client:
            return {t.name: t.description for t in (await client.list_tools()).tools}

    text = asyncio.run(descriptions())
    propose = " ".join(text["propose_keepouts_and_slots"].split())
    assert "kicad-cli DRC does not report items that lie wholly inside a slot" in propose
    assert "There is no override" in propose
    assert "Freerouting routes around a copperpour-only keepout as a full keepout" in propose
    confirm = " ".join(text["confirm_keepouts_and_slots"].split())
    assert "Removing a keepout or a slot is a loosening" in confirm
    assert "NEW board revision" in confirm and "verify_board" in confirm


# --------------------------------------------------------------------------- mutant-driven additions


def test_a_wide_track_and_a_big_via_count_by_their_copper_not_their_centre_line(tmp_path):
    slot = {"x1": 140, "y1": 110, "x2": 156, "y2": 110, "width_mm": 1.0, "ends": "square"}  # edges at y = 109.5 and 110.5
    # centre line 0.6 mm above the slot's edge: clear with a 0.2 mm track, 0.3 mm of clearance left; a 1.6 mm track leaves none
    thin = build(tmp_path, [seg((141, 108.9), (155, 108.9), width=0.2)], name="thin")
    wide = build(tmp_path, [seg((141, 108.9), (155, 108.9), width=1.6)], name="wide")
    assert propose(thin, tmp_path / "a", slots=[slot], copper_margin_mm=0.2)["status"] == "proposed"
    assert propose(wide, tmp_path / "b", slots=[slot], copper_margin_mm=0.2)["status"] == "refused"
    # a via of radius 0.3 centred 0.45 mm from the edge is 0.15 mm away: inside a 0.2 mm margin, outside a 0.1 mm one
    with_via = build(tmp_path, [via((148, 109.05))], name="v")
    assert propose(with_via, tmp_path / "c", slots=[slot], copper_margin_mm=0.2)["status"] == "refused"
    assert propose(with_via, tmp_path / "d", slots=[slot], copper_margin_mm=0.1)["status"] == "proposed"


def _change_a_slot_uuid(root):
    node = next(n for n in _planned_slot_nodes(root) if n.name == "gr_line")
    node.find("uuid").set_value(0, "00000000-0000-0000-0000-000000000001")


def _shift_the_outer_outline(root):
    """All four outer lines move together: still one closed outline, no longer the source's."""
    planned = {id(n) for n in _planned_slot_nodes(root)}
    for node in root.children:
        if not node.is_atom and node.name == "gr_line" and id(node) not in planned and node.find("layer").values[0] == "Edge.Cuts":
            for tag in ("start", "end"):
                child = node.find(tag)
                child.set_value(0, float(child.values[0]) + 0.02)


@pytest.mark.parametrize(("corrupt", "named"), [
    pytest.param(_change_a_slot_uuid, "slots after reload", id="slot-id"),
    pytest.param(_shift_the_outer_outline, "outer outline", id="outer-outline"),
])
def test_the_slot_set_and_the_outer_outline_checks_fire_alone(tmp_path, monkeypatch, corrupt, named):
    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])
    handle = corrupt_writes(monkeypatch, write_layer, "save", sexp_edit(corrupt), when=in_revision_tmp(tmp_path / "out"))
    monkeypatch.setattr(_self_check, "reload_differences", lambda **_k: {"missing": [], "unexpected": [], "tolerated": []})
    failed = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=lambda p, **_k: CLEAN)
    assert len(handle.hits) == 1 and failed["code"] == "write_not_verified" and named in failed["reason"], failed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


def test_a_plan_that_differs_at_confirm_is_refused_and_the_token_is_kept(tmp_path, monkeypatch):
    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])
    real = isolation._plan

    def changed(*args, **kwargs):
        plan, refusal = real(*args, **kwargs)
        return {**plan, "slots_after": plan["slots_after"] + 1}, refusal

    monkeypatch.setattr(isolation, "_plan", changed)
    out = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"])
    assert out["status"] == "error" and "different result" in out["reason"] and "token is kept" in out["note"]
    monkeypatch.setattr(isolation, "_plan", real)
    assert isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"])["status"] == "built"


def test_confirm_autoplacement_refuses_a_part_that_a_slot_now_covers(tmp_path):
    from mcp.server.mcpserver import MCPServer

    from boardproof import placement_anchor
    from boardproof.toolsets import placement as placement_toolset

    pcb = build(tmp_path)
    mcp = MCPServer("t")
    placement_toolset.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    proposal = tools["propose_autoplacement"](str(pcb), ["C1"])
    assert proposal["status"] == "pending_confirmation", proposal
    root = write_layer.load(pcb)
    outline, _ = placement_anchor.outline_rect(root)
    pos = proposal["proposed_positions"]["C1"]
    x, y = pos["x"] + outline.box[0], pos["y"] + outline.box[1]
    for node in rect_lines(x - 0.5, y - 0.5, x + 0.5, y + 0.5):  # a slot over the proposed spot, made after the proposal
        root.append(node)
    write_layer.save(root, pcb)
    out = tools["confirm_autoplacement"](proposal["revision_token"])
    assert out["status"] == "error" and "slot of the outline" in out["reason"], out


def test_a_round_hole_inside_a_rectangular_outline_is_a_cutout_not_a_non_rectangular_outline(tmp_path):
    from boardproof import placement_anchor

    circle = SExp.list("gr_circle", SExp.list("center", 148, 115), SExp.list("end", 149, 115),
                       SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default")), SExp.list("fill", "none"),
                       SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", str(uuid.uuid4())))
    outline, problem = placement_anchor.outline_rect(write_layer.load(build(tmp_path, [circle])))
    assert problem is None and outline.box == (123.5, 85.0, 173.5, 125.0)
    assert len(outline.cutouts) == 1
    x0, y0, x1, y1 = outline.cutouts[0]
    assert (round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)) == (147.0, 114.0, 149.0, 116.0)
