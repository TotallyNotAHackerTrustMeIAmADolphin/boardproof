"""The coordinate frame of the placement tools (tickets 24 and its review).

kicad_tools reports and accepts footprint positions relative to an "origin" it
detects. Since 0.23.0 (build ticket 42) that is the min corner of the bounding box of
every top-level Edge.Cuts ``gr_*`` item, arc bulges and circles included, or (0, 0) when
there is none or one it cannot read (0.20.0 used the first ``gr_rect`` start, else the
min over ``gr_line`` endpoints, which is not the top-left corner of a reversed rect, a
polygon or a second rect). The tools read the origin from the RAW Edge.Cuts nodes with
their own arithmetic, cross-check it against the library, return it numerically, and
refuse to place or snap when the outline cannot be read exactly (a curve, an item the
library cannot read). Raw (at) nodes are always what the tests read back.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import write_layer
from kicad_mcp.toolsets import placement as placement_toolset

REPO = Path(__file__).resolve().parents[1]
FIXTURE = Path(__file__).parent / "fixtures" / "simple_led.kicad_pcb"
# simple_led: (gr_rect (start 136 77.5) (end 161 97.5)) on Edge.Cuts; R1 at raw (148.5 85.5).
RULE_BBOX = "min corner of the Edge.Cuts bounding box (arc bulges and circles included)"
RULE_NONE = "none: page origin (0,0)"
COORDINATE_TOOLS = [
    "place_footprints", "snap_footprints_to_grid", "propose_autoplacement",
]
RELATIVE_TOOLS = ["align_footprints", "distribute_footprints", "confirm_autoplacement"]
REFUSAL = "the Edge.Cuts outline cannot be read exactly: use propose_board_outline to normalise the outline"


@pytest.fixture
def fns() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def board(tmp_path: Path) -> Path:
    dest = tmp_path / "simple_led.kicad_pcb"
    shutil.copy(FIXTURE, dest)
    return dest


# -- fixtures built through kicad_tools nodes ------------------------------------------


def _edit(pcb: Path, mutate) -> None:
    from kicad_tools.schema import PCB

    board = PCB.load(pcb)
    mutate(board._sexp)
    write_layer.save_pcb(board, pcb)


def _drop_outline(root) -> None:
    for node in [n for n in root.children if n.name == "gr_rect"]:
        root.remove(node)


def _add_rect(root, start, end) -> None:
    from kicad_tools.sexp import parse_string

    root.append(parse_string(
        f'(gr_rect (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) '
        '(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))'
    ))


def reversed_rect(board: Path) -> None:
    def mutate(root):
        _drop_outline(root)
        _add_rect(root, (161, 97.5), (136, 77.5))

    _edit(board, mutate)


def two_rects_outline_second(board: Path) -> None:
    def mutate(root):
        _drop_outline(root)
        _add_rect(root, (150, 85), (155, 90))
        _add_rect(root, (136, 77.5), (161, 97.5))

    _edit(board, mutate)


def poly_outline(board: Path) -> None:
    from kicad_tools.sexp import parse_string

    def mutate(root):
        _drop_outline(root)
        root.append(parse_string(
            '(gr_poly (pts (xy 136 77.5) (xy 161 77.5) (xy 161 97.5) (xy 136 97.5)) '
            '(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))'
        ))

    _edit(board, mutate)


def no_outline(board: Path) -> None:
    _edit(board, _drop_outline)


def _raw_at(pcb: Path, ref: str) -> tuple[float, ...]:
    from kicad_tools.sexp import parse_file

    for fp in parse_file(pcb).find_all("footprint"):
        if any(
            t.values and t.values[0] == "reference" and t.values[1] == ref
            for t in fp.find_all("fp_text")
        ):
            return tuple(float(v) for v in fp.find("at").values)
    raise AssertionError(f"{ref} not found")


def _place(fns: dict, board: Path, **spec) -> dict:
    return fns["place_footprints"].fn(
        pcb_path=str(board), specs=[{"reference": "R1", "x": 10.0, "y": 5.0, **spec}]
    )


# -- descriptions -----------------------------------------------------------------------

RULE_TEXT = (
    "local (0,0) is the top-left corner of the bounding box of the Edge.Cuts outline (arc "
    "bulges and circles included); the tool refuses an outline it cannot read exactly (a "
    "curve, an arc inside a polygon, a legacy arc without a mid point, Edge.Cuts graphics "
    "inside a footprint, a malformed or unsupported Edge.Cuts item); without any outline "
    "coordinates are page coordinates"
)


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("name", COORDINATE_TOOLS)
def test_coordinate_taking_tools_state_the_rule_exactly(fns: dict, name: str) -> None:
    assert RULE_TEXT in _flat(fns[name].description)


@pytest.mark.parametrize("name", RELATIVE_TOOLS)
def test_relative_tools_do_not_tell_the_caller_to_pass_coordinates(fns: dict, name: str) -> None:
    text = _flat(fns[name].description)
    assert "never pass page" not in text
    assert "relative" in text and "local positions" in text


def test_snap_description_says_the_grid_is_anchored_at_the_outline_corner(fns: dict) -> None:
    text = _flat(fns["snap_footprints_to_grid"].description)
    assert "anchored at the outline corner" in text
    assert "not on the editor's absolute grid unless the corner is" in text


def test_the_skill_states_the_rule() -> None:
    text = _flat((REPO / ".claude" / "skills" / "kicad-pcb" / "SKILL.md").read_text(encoding="utf-8"))
    assert RULE_TEXT in text


# -- behaviour: raw file checked ----------------------------------------------------------


def test_one_rect_local_coordinates_land_at_outline_corner_plus_local(fns: dict, board: Path) -> None:
    result = _place(fns, board)
    assert result["status"] == "ok", result
    assert result["origin_mm"] == [136.0, 77.5]
    assert result["origin_rule"] == RULE_BBOX
    assert "136" in result["frame"] and "77.5" in result["frame"]
    assert _raw_at(board, "R1")[:2] == (146.0, 82.5)


@pytest.mark.parametrize(
    "build", [reversed_rect, two_rects_outline_second, poly_outline],
    ids=["reversed-rect", "two-rects", "poly-outline"],
)
def test_outlines_0_20_refused_are_read_at_their_top_left_corner(fns: dict, board: Path, build) -> None:
    """0.20.0's origin was not the top-left corner of these outlines, so they were refused.
    0.23.0's is, and the raw reader agrees: local coordinates land at corner + local."""
    build(board)
    result = _place(fns, board)
    assert result["status"] == "ok", result
    assert result["origin_mm"] == [136.0, 77.5]
    assert result["origin_rule"] == RULE_BBOX
    assert _raw_at(board, "R1")[:2] == (146.0, 82.5)


def curve_inside_the_outline(board: Path) -> None:
    from kicad_tools.sexp import parse_string

    def mutate(root):
        root.append(parse_string(
            '(gr_curve (pts (xy 140 80) (xy 145 85) (xy 150 82) (xy 155 90)) '
            '(stroke (width 0.1) (type default)) (layer "Edge.Cuts"))'
        ))

    _edit(board, mutate)


def text_on_edge_cuts(board: Path) -> None:
    from kicad_tools.sexp import parse_string

    def mutate(root):
        root.append(parse_string(
            '(gr_text "EDGE" (at 150 90 0) (layer "Edge.Cuts") '
            '(effects (font (size 1 1) (thickness 0.15))))'
        ))

    _edit(board, mutate)


@pytest.mark.parametrize(
    "build", [curve_inside_the_outline, text_on_edge_cuts], ids=["curve", "unsupported-item"]
)
def test_outlines_the_reader_cannot_read_exactly_are_refused_and_move_nothing(
    fns: dict, board: Path, build
) -> None:
    build(board)
    before = board.read_bytes()
    for name, kwargs in (
        ("place_footprints", {"specs": [{"reference": "R1", "x": 10.0, "y": 5.0}]}),
        ("snap_footprints_to_grid", {"refs": ["R1"], "grid_mm": 1.0}),
        ("propose_autoplacement", {"refs": ["R1"]}),
    ):
        result = fns[name].fn(pcb_path=str(board), **kwargs)
        assert result["status"] == "error", (name, result)
        assert REFUSAL in result["reason"]
        assert "origin_mm" in result and "origin_rule" in result and "frame" in result
    assert board.read_bytes() == before


def test_the_origin_numbers_reported_for_a_refused_board_match_the_librarys(
    fns: dict, board: Path
) -> None:
    text_on_edge_cuts(board)
    result = _place(fns, board)
    assert result["status"] == "error" and REFUSAL in result["reason"]
    assert result["origin_mm"] == [0.0, 0.0]  # the library cannot read the outline: page origin
    assert result["origin_rule"] == RULE_NONE


def test_an_arc_bulge_sets_the_origin_and_the_raw_reader_agrees(fns: dict, board: Path) -> None:
    """An Edge.Cuts arc whose leftmost point is neither its start, mid nor end: the origin is
    the circle's axis extreme, computed by the raw reader and by the library alike."""
    import math

    from kicad_tools.sexp import parse_string

    def mutate(root):
        root.append(parse_string(
            '(gr_arc (start 136 77.5) (mid 131 82) (end 136 97.5) '
            '(stroke (width 0.1) (type default)) (layer "Edge.Cuts"))'
        ))

    _edit(board, mutate)
    # circle through the three points: centre on y = 87.5
    cx = ((136**2 + 77.5**2) - (131**2 + 82**2) - 2 * 87.5 * (77.5 - 82)) / (2 * (136 - 131))
    left = cx - math.dist((cx, 87.5), (136, 77.5))
    result = _place(fns, board)
    assert result["status"] == "ok", result
    assert result["origin_mm"][0] == pytest.approx(left, abs=1e-9) and left < 131
    assert result["origin_mm"][1] == 77.5
    assert _raw_at(board, "R1")[:2] == pytest.approx((left + 10.0, 82.5), abs=1e-6)


def test_no_outline_at_all_places_in_page_coordinates(fns: dict, board: Path) -> None:
    no_outline(board)
    result = _place(fns, board)
    assert result["status"] == "ok", result
    assert result["origin_mm"] == [0.0, 0.0]
    assert result["origin_rule"] == RULE_NONE
    assert result["frame"].startswith("page coordinates (no outline yet)")
    assert _raw_at(board, "R1")[:2] == (10.0, 5.0)


def test_a_board_outline_made_by_propose_board_outline_is_accepted(
    fns: dict, tmp_path: Path
) -> None:
    from kicad_mcp import board_outline

    (tmp_path / "proj").mkdir()
    board = tmp_path / "proj" / "simple_led.kicad_pcb"
    shutil.copy(FIXTURE, board)

    empty = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}
    outline = {"x": 100.0, "y": 70.0, "width": 100.0, "height": 70.0, "corner_radius": 3.0}
    proposal = board_outline.propose_board_outline(board, outline, output_dir=tmp_path / "rev")
    assert proposal["status"] == "proposed", proposal
    built = board_outline.confirm_board_outline(
        board, proposal["revision_token"], drc=lambda p, **_k: empty
    )
    assert built["status"] == "built", built
    revision = Path(built["output_pcb"])
    result = _place(fns, revision)
    assert result["status"] == "ok", result
    assert result["origin_rule"] == RULE_BBOX
    assert result["origin_mm"] == [100.0, 70.0]
    assert _raw_at(revision, "R1")[:2] == (110.0, 75.0)


def test_disagreement_with_the_library_is_an_error_and_moves_nothing(
    fns: dict, board: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_tools.schema import PCB

    def wrong(self) -> None:
        self._board_origin = (1.0, 1.0)

    monkeypatch.setattr(PCB, "_detect_board_origin", wrong)
    before = board.read_bytes()
    result = _place(fns, board)
    assert result["status"] == "error"
    assert "disagree" in result["reason"]
    assert board.read_bytes() == before


# -- results of every coordinate-taking tool carry the computed frame ------------------------


def _assert_frame(result: dict) -> None:
    assert result["origin_mm"] == [136.0, 77.5], result
    assert result["origin_rule"] == RULE_BBOX
    assert "136" in result["frame"]


def test_every_tool_result_carries_origin_rule_and_frame(fns: dict, board: Path) -> None:
    _assert_frame(fns["snap_footprints_to_grid"].fn(pcb_path=str(board), refs=["R1"], grid_mm=1.0))
    _assert_frame(fns["align_footprints"].fn(pcb_path=str(board), refs=["R1", "D1"], axis="x", edge="min"))
    _assert_frame(
        fns["distribute_footprints"].fn(pcb_path=str(board), refs=["J1", "R1", "D1"], axis="x")
    )
    proposal = fns["propose_autoplacement"].fn(pcb_path=str(board), refs=["R1"])
    _assert_frame(proposal)
    _assert_frame(fns["confirm_autoplacement"].fn(revision_token=proposal["revision_token"]))


def test_error_and_incomplete_results_carry_the_frame_too(fns: dict, board: Path) -> None:
    for result in (
        fns["align_footprints"].fn(pcb_path=str(board), refs=["R1", "NOPE"], axis="x", edge="min"),
        fns["distribute_footprints"].fn(pcb_path=str(board), refs=["J1", "R1", "NOPE"], axis="x"),
        fns["snap_footprints_to_grid"].fn(pcb_path=str(board), refs=["NOPE"], grid_mm=1.0),
    ):
        assert result["status"] == "error"
        _assert_frame(result)


def test_align_and_distribute_report_the_frame_when_the_outline_is_unusual(
    fns: dict, board: Path
) -> None:
    text_on_edge_cuts(board)  # they work on relative positions, so they still run
    aligned = fns["align_footprints"].fn(pcb_path=str(board), refs=["R1", "D1"], axis="x", edge="min")
    assert aligned["status"] == "ok", aligned
    assert aligned["origin_mm"] == [0.0, 0.0]
    assert "NOT" in aligned["frame"]


# -- rotation -----------------------------------------------------------------------------


def test_moving_without_a_rotation_keeps_the_existing_rotation(fns: dict, board: Path) -> None:
    first = _place(fns, board, rotation=90.0)
    assert first["status"] == "ok"
    assert _raw_at(board, "R1") == (146.0, 82.5, 90.0)
    again = fns["place_footprints"].fn(
        pcb_path=str(board), specs=[{"reference": "R1", "x": 12.0, "y": 6.0}]
    )
    assert again["status"] == "ok", again
    assert _raw_at(board, "R1") == (148.0, 83.5, 90.0)


# -- fix round 1 of build ticket 42: outlines the reader cannot read exactly are refused -------
# The review found two outlines KiCad loads whose exact bounding box differs from what both
# this reader and kicad_tools computed, with no problem reported. Coordinator decision: fail
# closed and name the item.

COORDINATE_CALLS = (
    ("place_footprints", lambda ref: {"specs": [{"reference": ref, "x": 10.0, "y": 5.0}]}),
    ("snap_footprints_to_grid", lambda ref: {"refs": [ref], "grid_mm": 1.0}),
    ("propose_autoplacement", lambda ref: {"refs": [ref]}),
)


def _demo_board(kicad_demo, rel: str) -> Path:
    """A copy of the KiCad demo board ``rel``, through build ticket 34's ``kicad_demo`` fixture
    (copies the board's project directory; a missing demo skips, or fails under
    ``KICAD_DEMOS_REQUIRED=1``)."""
    path = Path(rel)
    return kicad_demo(path.parent.as_posix()) / path.name


FOOTPRINT_REFUSAL = (
    "the board-local frame cannot be stated exactly: Edge.Cuts graphics inside footprints "
    "are not measured yet"
)


def _assert_refused(
    fns: dict, board: Path, ref: str, *names: str, hints: tuple = (REFUSAL,), absent: tuple = ()
) -> None:
    before = board.read_bytes()
    for tool, kwargs in COORDINATE_CALLS:
        result = fns[tool].fn(pcb_path=str(board), **kwargs(ref))
        assert result["status"] == "error", (tool, result)
        for text in hints + names:
            assert text in result["reason"], (tool, text, result["reason"])
        for text in absent + ("the library cannot read",):
            assert text not in result["reason"], (tool, text, result["reason"])
        assert "NOT board-local" in result["frame"]
    assert board.read_bytes() == before


NFC = "royalblue54L_feather/nfc_antenna/RoyalBlue54L-NFC-Antenna/RoyalBlue54L-NFC-Antenna.kicad_pcb"
TINY_TAPEOUT = "tiny_tapeout/tinytapeout-demo.kicad_pcb"


def test_a_footprint_carrying_edge_cuts_graphics_is_refused_by_name(fns: dict, kicad_demo) -> None:
    """KiCad's RoyalBlue54L NFC-antenna demo: footprint J1 (raw at 151.44971 66.560695)
    carries fp_line and fp_arc on Edge.Cuts above every gr_* item. KiCad's exact outline
    top is 66.560695; the gr_* items alone, which both this reader and kicad_tools use, give
    69.058195. Was: placed at origin y 69.058195 with no problem. The refusal has its own
    hint (fix round 2): normalising the board outline cannot fix a footprint's Edge.Cuts."""
    board = _demo_board(kicad_demo, NFC)
    fields, problem = placement_toolset._outline_frame(board)
    assert problem is not None and "footprint J1" in problem and "Edge.Cuts" in problem
    assert fields["origin_mm"] == [139.94971, 69.058195]  # the gr_* box, which the library applies too
    _assert_refused(
        fns, board, "J1", "footprint J1", "fp_arc", "fp_line",
        hints=(FOOTPRINT_REFUSAL,), absent=("propose_board_outline",),
    )


def test_a_footprint_edge_cuts_inside_the_outline_names_the_footprint_not_propose_board_outline(
    fns: dict, kicad_demo
) -> None:
    """KiCad's tiny_tapeout demo: footprint J15's Edge.Cuts lies inside the outline, so the
    frame is in fact right, but it is not measured, so the board is refused (a safe
    superset). The hint names J15 and does not send the caller to propose_board_outline,
    which cannot change a footprint's Edge.Cuts."""
    board = _demo_board(kicad_demo, TINY_TAPEOUT)
    _assert_refused(
        fns, board, "J15", "footprint J15", hints=(FOOTPRINT_REFUSAL,), absent=("propose_board_outline",)
    )


def test_an_outline_problem_and_a_footprint_problem_give_both_hints(fns: dict, kicad_demo) -> None:
    """The NFC demo plus a gr_poly with an arc (built through the write layer): the poly needs
    propose_board_outline, footprint J1 cannot be fixed that way; the reason says both."""
    from kicad_tools.sexp import parse_string

    board = _demo_board(kicad_demo, NFC)

    def mutate(root):
        root.append(parse_string(
            '(gr_poly (pts (xy 150 80) (xy 160 80) (xy 160 90) (xy 150 90) '
            '(arc (start 150 90) (mid 145 85) (end 150 80))) '
            '(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))'
        ))

    _edit(board, mutate)
    _assert_refused(
        fns, board, "J1", "footprint J1", "gr_poly", "(arc ...)", hints=(REFUSAL, FOOTPRINT_REFUSAL)
    )


def poly_with_an_arc(board: Path) -> None:
    """A gr_poly whose points end with an (arc ...) bulging 10 mm left of every xy point
    (KiCad loads it: the save below is parse-checked)."""
    from kicad_tools.sexp import parse_string

    def mutate(root):
        _drop_outline(root)
        root.append(parse_string(
            '(gr_poly (pts (xy 136 77.5) (xy 161 77.5) (xy 161 97.5) (xy 136 97.5) '
            '(arc (start 136 97.5) (mid 126 87.5) (end 136 77.5))) '
            '(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))'
        ))

    _edit(board, mutate)


def test_a_polygon_with_an_arc_among_its_points_is_refused_by_name(fns: dict, board: Path) -> None:
    """KiCad's bounding box includes the arc's bulge (left edge 126 here); this reader and
    kicad_tools read only the xy points (136). Was: placed at origin x 136 with no problem."""
    poly_with_an_arc(board)
    fields, problem = placement_toolset._outline_frame(board)
    assert problem is not None and "gr_poly" in problem and "(arc ...)" in problem
    assert fields["origin_mm"] == [136.0, 77.5]
    _assert_refused(fns, board, "R1", "gr_poly", "(arc ...)")


def legacy_arc(board: Path) -> None:
    """A legacy gr_arc (start = centre, end = arc start, angle), on a board whose version
    header is KiCad 5's, so KiCad loads it (parse-checked save). KiCad reads it as the arc
    (start 136 97.5) (mid 126 87.5) (end 136 77.5) (its own upgrade writes that), and
    kicad_tools agrees: origin (126, 77.5)."""
    from kicad_tools.sexp import parse_string

    def mutate(root):
        write_layer.set_number(root.find("version"), 0, 20171130)
        root.append(parse_string(
            '(gr_arc (start 136 87.5) (end 136 77.5) (angle -180) (layer Edge.Cuts) (width 0.1))'
        ))

    _edit(board, mutate)


def test_a_legacy_arc_is_refused_with_a_true_reason(fns: dict, board: Path) -> None:
    """Was: "Edge.Cuts holds gr_arc without (mid ...), which the library cannot read as
    outline, so it uses the page origin", with origin_mm (0, 0), while the library read the
    arc and applied (126, 77.5)."""
    from kicad_tools.schema import PCB

    legacy_arc(board)
    assert tuple(PCB.load(board)._board_origin) == (126.0, 77.5)
    fields, problem = placement_toolset._outline_frame(board)
    assert problem is not None and "gr_arc without (mid ...)" in problem
    assert "cannot read" not in problem and "page origin" not in problem
    assert fields["origin_mm"] != [0.0, 0.0]
    before = board.read_bytes()
    for tool, kwargs in COORDINATE_CALLS:
        result = fns[tool].fn(pcb_path=str(board), **kwargs("R1"))
        assert result["status"] == "error", (tool, result)
        assert "gr_arc without (mid ...)" in result["reason"], (tool, result["reason"])
        assert "cannot read" not in result["reason"]
    assert board.read_bytes() == before


def test_the_origin_is_reported_without_float_noise(fns: dict, board: Path) -> None:
    """An arc whose leftmost point is 80.3 mm, computed through the three-point circle as
    80.29999999999998. The origin is rounded to 1e-9 mm."""
    from kicad_tools.sexp import parse_string

    def mutate(root):
        root.append(parse_string(
            '(gr_arc (start 90 100.3) (mid 80.3 110) (end 90 119.7) '
            '(stroke (width 0.1) (type default)) (layer "Edge.Cuts"))'
        ))

    _edit(board, mutate)
    result = _place(fns, board)
    assert result["status"] == "ok", result
    assert result["origin_mm"] == [80.3, 77.5]
    assert "(80.3, 77.5)" in result["frame"]


def test_the_library_origin_in_a_disagreement_is_rounded_too(
    fns: dict, board: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kicad_tools.schema import PCB

    def noisy(self) -> None:
        self._board_origin = (1.0000000000000002, 0.9999999999999998)

    monkeypatch.setattr(PCB, "_detect_board_origin", noisy)
    result = _place(fns, board)
    assert result["status"] == "error" and "disagree" in result["reason"]
    assert "[1.0, 1.0]" in result["reason"]
