"""Edge.Cuts as one outer loop plus slots (src/boardproof/edge_loops.py, GitHub issue 132).

Boards are the golden board (outline (123.5, 85) to (173.5, 125), a 50 x 40 rectangle of four
``gr_line``) with Edge.Cuts items added through the write layer's tree API and saved with
``write_layer.save``. Every expectation is a literal read off the coordinates written here.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from boardproof import edge_loops, write_layer

GOLDEN_DIR = Path(__file__).parent / "fixtures" / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"


def _stroke():
    return SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default"))


def line(a, b, ident=None):
    return SExp.list("gr_line", SExp.list("start", *a), SExp.list("end", *b), _stroke(),
                     SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", ident or str(uuid.uuid4())))


def arc(a, m, b, ident=None):
    return SExp.list("gr_arc", SExp.list("start", *a), SExp.list("mid", *m), SExp.list("end", *b), _stroke(),
                     SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", ident or str(uuid.uuid4())))


def rect_lines(x0, y0, x1, y1):
    c = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return [line(c[i], c[(i + 1) % 4], f"rect-{x0}-{y0}-{i}") for i in range(4)]


def board_with(tmp_path: Path, nodes) -> Path:
    shutil.copytree(GOLDEN_DIR, tmp_path / "proj")
    pcb = tmp_path / "proj" / GOLDEN_NAME
    root = write_layer.load(pcb)
    for node in nodes:
        root.append(node)
    write_layer.save(root, pcb)
    return pcb


def classify(pcb: Path):
    return edge_loops.classify(write_layer.load(pcb))


def test_the_plain_golden_outline_is_one_outer_loop_and_no_slot(tmp_path):
    out = classify(board_with(tmp_path, []))
    assert out.problems == ()
    assert out.slots == ()
    assert out.outer is not None and out.outer.box == (123.5, 85.0, 173.5, 125.0)
    assert out.outer.kinds == ("gr_line",) * 4


def test_a_closed_rectangle_of_lines_inside_is_a_slot_named_by_its_first_uuid(tmp_path):
    out = classify(board_with(tmp_path, rect_lines(138, 108, 158, 110)))
    assert out.problems == ()
    (slot,) = out.slots
    assert slot.box == (138.0, 108.0, 158.0, 110.0)
    assert slot.id == "rect-138-108-0"
    assert out.outer.box == (123.5, 85.0, 173.5, 125.0)  # the slot never changes the outer outline


def test_a_stadium_of_lines_and_arcs_is_one_slot_with_its_arc_bulges_in_the_box(tmp_path):
    nodes = [line((138, 114), (158, 114), "s0"), arc((158, 114), (159, 115), (158, 116), "s1"),
             line((158, 116), (138, 116), "s2"), arc((138, 116), (137, 115), (138, 114), "s3")]
    (slot,) = classify(board_with(tmp_path, nodes)).slots
    assert slot.kinds == ("gr_line", "gr_arc", "gr_line", "gr_arc")
    assert slot.id == "s0"
    x0, y0, x1, y1 = slot.box
    assert (round(x0, 3), round(y0, 3), round(x1, 3), round(y1, 3)) == (137.0, 114.0, 159.0, 116.0)


def test_a_chain_written_in_scrambled_order_and_direction_is_still_one_loop(tmp_path):
    a, b, c, d = (138, 108), (158, 108), (158, 110), (138, 110)
    nodes = [line(c, b, "n1"), line(a, d, "n3"), line(a, b, "n0"), line(d, c, "n2")]  # two reversed
    (slot,) = classify(board_with(tmp_path, nodes)).slots
    assert slot.box == (138.0, 108.0, 158.0, 110.0)
    assert len(slot.nodes) == 4


def test_gr_rect_and_gr_circle_inside_are_slots(tmp_path):
    rect = SExp.list("gr_rect", SExp.list("start", 138, 108), SExp.list("end", 158, 110), _stroke(),
                     SExp.list("fill", "none"), SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", "r1"))
    circle = SExp.list("gr_circle", SExp.list("center", 148, 115), SExp.list("end", 149, 115), _stroke(),
                       SExp.list("fill", "none"), SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", "c1"))
    out = classify(board_with(tmp_path, [rect, circle]))
    assert out.problems == ()
    assert sorted(s.id for s in out.slots) == ["c1", "r1"]


def test_a_loop_that_touches_the_outer_outline_is_a_problem_not_a_slot(tmp_path):
    out = classify(board_with(tmp_path, rect_lines(123.5, 108, 130, 110)))  # shares the left edge
    assert out.slots == ()
    assert any("not strictly inside" in p for p in out.problems)


def test_a_loop_that_crosses_the_outer_outline_is_a_problem(tmp_path):
    out = classify(board_with(tmp_path, rect_lines(120, 108, 130, 110)))
    assert out.slots == ()
    assert any("not strictly inside" in p for p in out.problems)


def test_an_open_chain_is_a_problem_naming_it(tmp_path):
    out = classify(board_with(tmp_path, rect_lines(138, 108, 158, 110)[:3]))
    assert out.slots == ()
    assert any("open chain" in p for p in out.problems)


def test_an_island_inside_a_slot_is_a_problem(tmp_path):
    nodes = rect_lines(138, 100, 158, 120) + rect_lines(145, 105, 150, 110)
    out = classify(board_with(tmp_path, nodes))
    assert any("touch, cross or nest" in p for p in out.problems)


def test_two_separate_slots_are_two_slots(tmp_path):
    out = classify(board_with(tmp_path, rect_lines(138, 108, 158, 110) + rect_lines(138, 112, 158, 114)))
    assert out.problems == ()
    assert sorted(s.box for s in out.slots) == [(138.0, 108.0, 158.0, 110.0), (138.0, 112.0, 158.0, 114.0)]


def test_a_second_loop_outside_the_largest_is_a_problem(tmp_path):
    out = classify(board_with(tmp_path, rect_lines(180, 85, 190, 95)))
    assert any("not strictly inside" in p for p in out.problems)


def test_a_collinear_arc_is_reported_not_guessed(tmp_path):
    out = classify(board_with(tmp_path, [arc((140, 110), (145, 110), (150, 110), "bad")]))
    assert any("collinear arc" in p for p in out.problems)


# --------------------------------------------------------------------------- the 2-D helpers


@pytest.mark.parametrize(
    ("a", "b", "c", "d", "expected"),
    [
        ((0, 0), (2, 2), (0, 2), (2, 0), True),  # proper crossing
        ((0, 0), (1, 0), (1, 0), (2, 0), True),  # touching at an end
        ((0, 0), (1, 0), (2, 0), (3, 0), False),  # collinear, apart
        ((0, 0), (2, 0), (1, 0), (3, 0), True),  # collinear overlap
        ((0, 0), (1, 1), (2, 2), (3, 3), False),
        ((0, 0), (1, 0), (0, 1), (1, 1), False),  # parallel
    ],
)
def test_segments_intersect(a, b, c, d, expected):
    assert edge_loops.segments_intersect(a, b, c, d) is expected


def test_shape_distance_counts_containment_unless_asked_not_to():
    big = [(0, 0), (10, 0), (10, 10), (0, 10)]
    small = [(4, 4), (6, 4), (6, 6), (4, 6)]
    assert edge_loops.ring_distance(small, big) == 0.0  # inside: no gap
    assert edge_loops.boundary_distance(small, big) == pytest.approx(4.0)  # the gap to the contour
    assert edge_loops.boundary_distance(small, [(20, 4), (22, 4), (22, 6), (20, 6)]) == pytest.approx(14.0)
    assert edge_loops.shape_distance(small, True, [(5, 5)], False) == 0.0  # a point inside a ring
    assert edge_loops.shape_distance(small, True, [(8, 5)], False) == pytest.approx(2.0)


def test_ring_is_simple_rejects_a_bow_tie_and_a_repeated_point():
    assert edge_loops.ring_is_simple([(0, 0), (4, 0), (4, 4), (0, 4)])
    assert not edge_loops.ring_is_simple([(0, 0), (4, 4), (4, 0), (0, 4)])  # bow tie
    assert not edge_loops.ring_is_simple([(0, 0), (4, 0), (4, 0), (0, 4)])
    assert not edge_loops.ring_is_simple([(0, 0), (4, 0)])


def test_arc_points_follow_the_side_the_mid_point_names():
    pts = edge_loops.arc_points((158, 114), (159, 115), (158, 116))  # the right cap of a stadium
    assert pts[0] == (158, 114) and pts[-1] == (158, 116)
    assert max(p[0] for p in pts) == pytest.approx(159.0, abs=1e-3)
    assert edge_loops.arc_points((0, 0), (1, 0), (2, 0)) is None
