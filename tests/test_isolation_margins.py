"""Review round 1 of issue 132: margins, sampling error, id-less items and surviving mutants.

Same fixtures and helpers as tests/test_isolation.py (golden board with C1 (133.5, 93), D1 (163.5, 103),
J1 (128.5, 105), J2 (163.5, 117), R1 (163.5, 95), U1 (148.5, 105)).
"""

from __future__ import annotations

import shutil
import uuid
from types import SimpleNamespace

import pytest
from kicad_tools.sexp import SExp

from boardproof import _self_check, isolation, write_layer
from tests._self_check_helpers import corrupt_writes, in_revision_tmp, sexp_edit
from tests.test_edge_loops import board_with, classify, line
from tests.test_isolation import CLEAN, MAINS, SLOT, _planned_slot_nodes, build, pour, propose, rect_lines, seg, via


@pytest.mark.parametrize("margin", [0.0, 0.5])
def test_a_slot_wholly_over_a_part_is_refused_at_any_margin_including_zero(tmp_path, margin):
    """Containment is a conflict whatever the margin: a distance of 0 is not '< 0'."""
    pcb = build(tmp_path)
    over_c1 = {"x1": 131.5, "y1": 93, "x2": 135.5, "y2": 93, "width_mm": 2.0, "ends": "square"}
    over_u1 = {"x1": 146, "y1": 105, "x2": 151, "y2": 105, "width_mm": 2.0, "ends": "square"}
    for slot, ref in ((over_c1, "C1"), (over_u1, "U1")):
        out = propose(pcb, tmp_path, slots=[slot], copper_margin_mm=margin)
        assert out["status"] == "refused", (ref, margin)
        kinds = {c["kind"] for c in out["conflicts"]["items"]}
        assert {"pad", "courtyard"} <= kinds and any(c["item"].startswith(ref) for c in out["conflicts"]["items"])


def test_a_track_or_via_wholly_inside_a_slot_is_refused_at_margin_zero_even_with_no_width(tmp_path):
    pcb = build(tmp_path, [seg((142, 112), (150, 112), width=0.0), via((153, 112))])
    out = propose(pcb, tmp_path, slots=[SLOT], copper_margin_mm=0.0)
    assert out["status"] == "refused" and sorted(c["kind"] for c in out["conflicts"]["items"]) == ["track", "via"]


def test_a_nested_slot_is_refused_at_edge_margin_zero(tmp_path):
    pcb = build(tmp_path, rect_lines(138, 108, 158, 110))
    inner = {"x1": 140, "y1": 109, "x2": 150, "y2": 109, "width_mm": 0.6, "ends": "square"}
    assert propose(pcb, tmp_path, slots=[inner], edge_margin_mm=0.0)["status"] == "refused"


def _circle(a, b, c):
    d = 2 * (a[0] * (b[1] - c[1]) + b[0] * (c[1] - a[1]) + c[0] * (a[1] - b[1]))
    ux = ((a[0] ** 2 + a[1] ** 2) * (b[1] - c[1]) + (b[0] ** 2 + b[1] ** 2) * (c[1] - a[1]) + (c[0] ** 2 + c[1] ** 2) * (a[1] - b[1])) / d
    uy = ((a[0] ** 2 + a[1] ** 2) * (c[0] - b[0]) + (b[0] ** 2 + b[1] ** 2) * (a[0] - c[0]) + (c[0] ** 2 + c[1] ** 2) * (b[0] - a[0])) / d
    return ux, uy, ((a[0] - ux) ** 2 + (a[1] - uy) ** 2) ** 0.5


def test_a_large_radius_track_arc_is_not_measured_through_a_coarse_chord(tmp_path):
    """A radius-50 arc: 7.5 degree chords would sit 0.1 mm inside the true arc."""
    arc_track = SExp.list("arc", SExp.list("start", 125, 90), SExp.list("mid", 148.5, 89.0), SExp.list("end", 172, 90),
                          SExp.list("width", 0.2), SExp.list("layer", "F.Cu"), SExp.list("net", 1), SExp.list("uuid", str(uuid.uuid4())))
    root = write_layer.load(build(tmp_path, [arc_track]))
    (track,) = isolation._Copper(root).tracks
    ux, uy, r = _circle((125.0, 90.0), (148.5, 89.0), (172.0, 90.0))
    mids = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(track["points"], track["points"][1:], strict=False)]
    assert max(r - ((x - ux) ** 2 + (y - uy) ** 2) ** 0.5 for x, y in mids) <= 0.006


def test_a_pad_is_a_square_that_covers_it_in_every_rotation(tmp_path):
    """hypot(w, h) / 2 around the centre holds a pad turned by any angle; side / 2 does not."""
    root = write_layer.load(build(tmp_path))
    copper = isolation._Copper(root)
    pad_node = next(p for fp in root.children if not fp.is_atom and fp.name == "footprint" for p in fp.children
                    if not p.is_atom and p.name == "pad" and str(p.values[0]) == "1" and fp.find("at").values[:2] == [148.5, 105.0])
    w, h = (float(v) for v in pad_node.find("size").values[:2])
    ring = next(p["ring"] for p in copper.pads if p["reference"] == "U1" and p["number"] == "1")
    xs = [p[0] for p in ring]
    assert max(xs) - min(xs) == pytest.approx((w * w + h * h) ** 0.5, abs=1e-6)


def test_the_sampling_tolerance_is_part_of_every_margin(tmp_path):
    """A track 0.003 mm beyond the margin is still refused (the sampled contour may be that far inside)."""
    slot = {"x1": 140, "y1": 110, "x2": 156, "y2": 110, "width_mm": 1.0, "ends": "square"}  # lower edge at y = 110.5
    near = build(tmp_path, [seg((141, 111.003), (155, 111.003), width=0.0)], name="n")
    far = build(tmp_path, [seg((141, 111.1), (155, 111.1), width=0.0)], name="f")
    assert propose(near, tmp_path / "a", slots=[slot])["status"] == "refused"
    assert propose(far, tmp_path / "b", slots=[slot])["status"] == "proposed"


def test_a_via_on_layers_the_keepout_does_not_cover_is_no_conflict():
    copper = SimpleNamespace(tracks=[], pads=[], courtyards=[], pours=[],
                             vias=[{"center": (5.0, 5.0), "radius": 0.3, "layers": {"B.Cu"}, "uuid": "v1"}])
    rect = {"x": 0, "y": 0, "width": 10, "height": 10}
    on_f = isolation._norm_keepout({"name": "K", "rect": rect, "layers": ["F.Cu"]}, ["F.Cu", "B.Cu"])[0]
    on_b = isolation._norm_keepout({"name": "K", "rect": rect, "layers": ["B.Cu"]}, ["F.Cu", "B.Cu"])[0]
    assert isolation._keepout_conflicts(on_f, copper) == []
    assert [c["kind"] for c in isolation._keepout_conflicts(on_b, copper)] == ["via"]


def test_a_pour_on_another_layer_is_not_listed_under_a_keepout(tmp_path):
    pcb = build(tmp_path, [pour("B.Cu")])
    on_f = propose(pcb, tmp_path / "a", keepouts=[MAINS])
    on_b = propose(pcb, tmp_path / "b", keepouts=[{**MAINS, "layers": ["B.Cu"]}])
    assert on_f["zone_overlaps"]["count"] == 0 and "pour_note" not in on_f
    assert on_b["zone_overlaps"]["count"] == 1


def test_a_keepout_name_that_exists_on_the_board_is_refused(tmp_path):
    existing = isolation._keepout_node(isolation._norm_keepout(MAINS, ["F.Cu", "B.Cu"])[0])
    out = propose(build(tmp_path, [existing]), tmp_path, keepouts=[{**MAINS, "rect": {"x": 140, "y": 101, "width": 5, "height": 2}}])
    assert out["status"] == "refused" and "exists or is declared twice" in out["reason"]


def test_a_chain_whose_ends_miss_by_less_than_the_snap_still_closes(tmp_path):
    a, b, c, d = (138, 108), (158, 108), (158, 110), (138, 110)
    near = [line(a, b, "n0"), line((158.00005, 108), c, "n1"), line((158, 110.00005), d, "n2"), line((138.00005, 110), a, "n3")]
    (slot,) = classify(board_with(tmp_path, near)).slots
    assert len(slot.nodes) == 4
    shutil.rmtree(tmp_path / "proj")
    far = [line(a, b, "m0"), line((158.0005, 108), c, "m1"), line(c, d, "m2"), line(d, a, "m3")]
    assert any("open chain" in p for p in classify(board_with(tmp_path, far)).problems)


def _change_an_outer_uuid(root):
    planned = {id(n) for n in _planned_slot_nodes(root)}
    node = next(n for n in root.children if not n.is_atom and n.name == "gr_line" and id(n) not in planned
                and n.find("layer").values[0] == "Edge.Cuts")
    node.find("uuid").set_value(0, "00000000-0000-0000-0000-0000000000aa")


def test_the_outer_outline_id_check_fires_alone(tmp_path, monkeypatch):
    pcb = build(tmp_path)
    proposal = propose(pcb, tmp_path, slots=[SLOT])
    handle = corrupt_writes(monkeypatch, write_layer, "save", sexp_edit(_change_an_outer_uuid), when=in_revision_tmp(tmp_path / "out"))
    monkeypatch.setattr(_self_check, "reload_differences", lambda **_k: {"missing": [], "unexpected": [], "tolerated": []})
    failed = isolation.confirm_keepouts_and_slots(pcb, proposal["revision_token"], drc=lambda p, **_k: CLEAN)
    assert len(handle.hits) == 1 and failed["code"] == "write_not_verified" and "outer outline" in failed["reason"], failed


def _without_uuid(node):
    node.remove(node.find("uuid"))
    return node


def test_an_item_without_a_uuid_is_never_removed_by_matching_other_uuid_less_nodes(tmp_path):
    """KiCad 7 files carry tstamp, not uuid: a slot or keepout with a missing uuid is refused for removal,
    so an empty id can never select the outer outline's own uuid-less lines."""
    nodes = rect_lines(138, 108, 158, 110)
    _without_uuid(nodes[2])
    pcb = build(tmp_path, nodes)
    (slot,) = isolation.propose_keepouts_and_slots(pcb)["existing_slots"]["items"]
    out = propose(pcb, tmp_path, remove_slots=[slot["id"]])
    assert out["status"] == "refused" and "without a uuid" in out["reason"]
    bare = _without_uuid(isolation._keepout_node(isolation._norm_keepout(MAINS, ["F.Cu", "B.Cu"])[0]))
    out = propose(build(tmp_path, [bare], name="k"), tmp_path / "k", remove_keepouts=["MAINS"])
    assert out["status"] == "refused" and "without a uuid" in out["reason"]


def test_an_existing_slot_that_crosses_itself_is_an_outline_problem(tmp_path):
    a, b, c, d = (138, 108), (158, 110), (158, 108), (138, 112)  # a bow tie (unequal lobes: its signed area is not 0)
    out = classify(board_with(tmp_path, [line(a, b, "b0"), line(b, c, "b1"), line(c, d, "b2"), line(d, a, "b3")]))
    assert out.slots == () and any("crosses itself" in p for p in out.problems)


def test_the_keepout_counts_copper_within_the_sampling_tolerance_outside_its_edge(tmp_path):
    top = 95.0  # MAINS (F.Cu) spans y 95 to 100
    for dy, expect in ((0.003, "refused"), (0.02, "proposed")):
        pcb = build(tmp_path, [seg((141, top - dy), (150, top - dy), width=0.0)], name=f"t{dy}")
        assert propose(pcb, tmp_path / f"a{dy}", keepouts=[MAINS])["status"] == expect, dy
        pcb = build(tmp_path, [via((148, top - 0.3 - dy))], name=f"v{dy}")
        assert propose(pcb, tmp_path / f"b{dy}", keepouts=[MAINS])["status"] == expect, dy


def test_two_slots_within_the_sampling_tolerance_of_each_other_are_too_close_at_margin_zero(tmp_path):
    pcb = build(tmp_path, rect_lines(138, 108, 158, 110))
    for gap, expect in ((0.003, "refused"), (0.02, "proposed")):
        slot = {"x1": 140, "y1": 110 + gap + 0.3, "x2": 150, "y2": 110 + gap + 0.3, "width_mm": 0.6, "ends": "square"}
        assert propose(pcb, tmp_path / f"s{gap}", slots=[slot], edge_margin_mm=0.0)["status"] == expect, gap


def test_a_circular_slot_is_sampled_within_the_tolerance(tmp_path):
    circle = SExp.list("gr_circle", SExp.list("center", 148, 112), SExp.list("end", 168, 112),
                       SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default")), SExp.list("fill", "none"),
                       SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", "big-circle"))
    from boardproof import edge_loops

    (slot,) = edge_loops.classify(board_with_circle(tmp_path, circle)).slots
    pts = slot.points
    mids = [((a[0] + b[0]) / 2, (a[1] + b[1]) / 2) for a, b in zip(pts, pts[1:] + pts[:1], strict=True)]
    assert max(20.0 - ((x - 148) ** 2 + (y - 112) ** 2) ** 0.5 for x, y in mids) <= 0.006


def board_with_circle(tmp_path, circle):
    # a radius-20 circle needs a bigger outer outline than the golden board: widen it by editing the tree
    pcb = build(tmp_path)
    root = write_layer.load(pcb)
    for n in root.children:
        if not n.is_atom and n.name == "gr_line":
            for tag in ("start", "end"):
                c = n.find(tag)
                c.set_value(0, 100.0 if float(c.values[0]) < 150 else 200.0)
                c.set_value(1, 60.0 if float(c.values[1]) < 100 else 160.0)
    root.append(circle)
    write_layer.save(root, pcb)
    return write_layer.load(pcb)
