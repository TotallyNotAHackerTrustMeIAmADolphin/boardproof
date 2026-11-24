"""Follow-ups of issue 214 (GitHub issue 219): the Edge.Cuts chaining gap against KiCad's tolerance,
the guards of ``placement_anchor.cutout_boxes`` pinned directly (through ``place_footprints`` the frame
check shadows them), the ARCHITECTURE sentences on well-formed slots, and a ``gr_curve`` outer loop.

Boards are the golden board (outline (123.5, 85) to (173.5, 125)) with Edge.Cuts items replaced or added
through the write layer's tree API on a ``tmp_path`` copy; geometry is read off the raw tokens.
"""

from __future__ import annotations

import dataclasses
import json
import re
import subprocess
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp, parse_string
from mcp.server.mcpserver import MCPServer

from boardproof import edge_loops, kicad_cli, placement_anchor, raw_geometry, write_layer
from boardproof.toolsets import placement as placement_toolset
from tests.test_placement_rounded_outline import BOX, _golden, _line, _replace_outline

X0, Y0, X1, Y1 = BOX
CIRCLE = ('(gr_circle (center 150 105) (end 170 105) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") '
          '(uuid "33333333-3333-3333-3333-333333333333"))')
SLOT_CIRCLE = ('(gr_circle (center 150 105) (end 152 105) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") '
               '(uuid "44444444-4444-4444-4444-444444444444"))')
CURVE = ('(gr_curve (pts (xy 123.5 85) (xy 150 80) (xy 160 90) (xy 173.5 85)) (stroke (width 0.1) (type default)) '
         '(layer "Edge.Cuts") (uuid "22222222-2222-2222-2222-222222222222"))')


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _rect(gap: float = 0.0, dy: float = 0.0) -> list[SExp]:
    """The golden rectangle of four lines whose first line ends ``gap`` mm short of the second line's start
    (and ``dy`` mm off it in y: a diagonal gap)."""
    return [_line((X0, Y0), (X1 - gap, Y0 + dy)), _line((X1, Y0), (X1, Y1)), _line((X1, Y1), (X0, Y1)), _line((X0, Y1), (X0, Y0))]


def _board(tmp_path: Path, name: str, nodes: list[SExp]) -> Path:
    return _replace_outline(_golden(tmp_path, name), nodes)


def _cutouts(pcb: Path):
    return placement_anchor.cutout_boxes(write_layer.load(pcb))


# ---- 1. the chaining gap -------------------------------------------------------------------------------


def test_a_corner_gap_of_5e_minus_4_mm_is_refused_with_its_size_not_as_a_kicad_error(tmp_path, tools):
    pcb = _board(tmp_path, "gap", _rect(5e-4))
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 10.0, "y": 5.0}])
    assert result["status"] == "error" and result["code"] == "slots_unreadable", result
    reason = result["reason"]
    assert "0.0005 mm from another free end" in reason and "within 0.0001 mm" in reason, reason
    assert "KiCad reports invalid_outline" not in reason
    assert "a gap above that is invalid_outline" in reason
    assert pcb.read_bytes() == before


def test_a_gap_within_snap_still_closes_and_a_far_open_end_names_its_distance(tmp_path):
    closed = edge_loops.classify(write_layer.load(_board(tmp_path, "closed", _rect(edge_loops.SNAP / 2))))
    assert closed.problems == () and closed.outer is not None
    nodes = _rect()[:3]  # the left side is missing: the free ends are 40 mm apart
    reason = edge_loops.classify(write_layer.load(_board(tmp_path, "open", nodes))).problems[0]
    assert "nearest free end is 40 mm from another free end" in reason, reason


def test_each_open_chain_reports_the_nearest_of_its_own_free_ends(tmp_path):
    nodes = _rect(5e-4) + [_line((130, 90), (140, 100))]  # a gap of 5e-4 mm at a corner, and a stray line of length 14.14 mm
    problems = edge_loops.classify(write_layer.load(_board(tmp_path, "two", nodes))).problems
    assert sum("is 0.0005 mm from another free end" in p for p in problems) >= 1, problems
    assert sum("is 14.1 mm from another free end" in p for p in problems) == 1, problems
    assert not any("is 40 mm" in p or "is 50 mm" in p for p in problems), problems


KICAD_SENTENCE = "a gap above that is invalid_outline"
GAP_RE = re.compile(r"its nearest free end is ([0-9.e-]+) mm from another free end")


def _claims_are_true(problem: str) -> None:
    """A gap figure and the KiCad sentence appear together, only for a real gap above SNAP."""
    m = GAP_RE.search(problem)
    assert (m is not None) == (KICAD_SENTENCE in problem), problem
    if m:
        assert float(m.group(1)) > edge_loops.SNAP, problem
    else:
        assert "join other open chains" not in problem and "0 mm" not in problem, problem


def _open_chain_problems(tmp_path, name, nodes):
    problems = edge_loops.classify(write_layer.load(_board(tmp_path, name, nodes))).problems
    assert problems, "expected an open chain"
    return problems


@pytest.mark.parametrize("name,extra", [
    ("spur", [_line((X1, Y0), (X1 + 5, Y0 + 5))]),
    ("duplicate", [_line((X0, Y0), (X1, Y0))]),
    ("duplicate_reversed", [_line((X1, Y0), (X0, Y0))]),
    ("zero_length", [_line((130, 90), (130, 90))]),
])
def test_an_open_chain_without_a_real_gap_claims_no_gap_and_no_kicad_rule(tmp_path, name, extra):
    for problem in _open_chain_problems(tmp_path, name, _rect() + extra):
        _claims_are_true(problem)
        assert "open chain" in problem


def test_every_problem_line_of_vme_wren_makes_only_true_claims(kicad_demo):
    pcb = next(kicad_demo("vme-wren").glob("vme-wren.kicad_pcb"))
    problems = edge_loops.classify(write_layer.load(pcb)).problems
    if not problems:
        pytest.skip("this vme-wren has a closed Edge.Cuts outline within SNAP")
    for problem in problems:
        _claims_are_true(problem)
    assert any(GAP_RE.search(p) and "0.000904 mm" in p for p in problems), problems


def test_a_closed_outline_never_computes_the_free_ends(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(edge_loops, "_free_ends", lambda pieces: calls.append(1) or [])
    assert edge_loops.classify(write_layer.load(_board(tmp_path, "closed", _rect()))).problems == ()
    assert calls == []
    _open_chain_problems(tmp_path, "open", _rect(5e-4))
    assert calls, "an open chain asks for the free ends"


def test_the_free_ends_are_computed_once_however_many_chains_are_open(tmp_path, monkeypatch):
    calls = []
    real = edge_loops._free_ends
    monkeypatch.setattr(edge_loops, "_free_ends", lambda pieces: calls.append(1) or real(pieces))
    strays = [_line((130 + i, 90), (130 + i, 91)) for i in range(5)]
    problems = _open_chain_problems(tmp_path, "many", _rect() + strays)
    assert len(problems) == 5 and len(calls) == 1


def _drc_has_invalid_outline(pcb: Path) -> bool:
    out = pcb.with_suffix(".drc.json")
    subprocess.run([str(kicad_cli.find_kicad_cli()), "pcb", "drc", "--format", "json", "-o", str(out), str(pcb)],
                   capture_output=True, timeout=180, check=False)
    return any(v["type"] == "invalid_outline" for v in json.loads(out.read_text(encoding="utf-8"))["violations"])


@pytest.mark.parametrize("gap,dy,expected", [(5e-4, 0, False), (9.9e-3, 0, False), (1.01e-2, 0, True), (5e-2, 0, True),
                                             (7e-3, 7e-3, False), (8e-3, 8e-3, True)])
def test_kicad_closes_a_corner_gap_up_to_0_01_mm_and_reports_invalid_outline_above(tmp_path, gap, dy, expected):
    """Measured with kicad-cli 10.0.7 on 2026-11-24: 1e-4 to 9.9e-3 mm along x no invalid_outline, 1.01e-2 mm and up yes;
    a diagonal gap is judged by its Euclidean length: (7e-3, 7e-3) = 9.9e-3 mm closes, (8e-3, 8e-3) = 1.13e-2 mm does not."""
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not found")
    pcb = _board(tmp_path, "drc", _rect(gap, dy))
    assert _drc_has_invalid_outline(pcb) is expected
    # the tool stays stricter than KiCad: every gap above SNAP is refused, however KiCad rates it
    assert edge_loops.classify(write_layer.load(pcb)).outer is None


def test_the_vme_wren_gap_is_refused_with_its_size(kicad_demo, tools):
    pcb = next(kicad_demo("vme-wren").glob("vme-wren.kicad_pcb"))
    root = write_layer.load(pcb)
    problems = edge_loops.classify(root).problems
    if not problems:
        pytest.skip("this vme-wren has a closed Edge.Cuts outline within SNAP")
    ref = raw_geometry.read_footprints(root)[0].reference
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": ref, "x": 10.0, "y": 5.0}])
    assert result["status"] == "error" and result["code"] == "slots_unreadable", result
    assert re.search(r"nearest free end is [0-9.e-]+ mm from another free end", result["reason"]), result["reason"]
    assert "KiCad reports invalid_outline" not in result["reason"]
    assert pcb.read_bytes() == before


# ---- 2. the guards of cutout_boxes, called directly ----------------------------------------------------


def _add(pcb: Path, text: str) -> Path:
    root = write_layer.load(pcb)
    root.append(parse_string(text))
    write_layer.save(root, pcb)
    return pcb


def test_cutout_boxes_guard_on_read_problems_a_stray_text(tmp_path):
    pcb = _add(_golden(tmp_path), '(gr_text "EDGE" (at 150 90 0) (layer "Edge.Cuts") (effects (font (size 1 1) (thickness 0.15))))')
    with pytest.raises(placement_anchor.EdgeCutsUnreadable, match="is not read exactly: gr_text on Edge.Cuts"):
        _cutouts(pcb)


def test_cutout_boxes_guard_on_a_footprint_carrying_edge_cuts(tmp_path):
    pcb = _golden(tmp_path)
    root = write_layer.load(pcb)
    fp = next(n for n in root.children if not n.is_atom and n.name == "footprint")
    fp.append(parse_string('(fp_line (start 0 0) (end 1 1) (stroke (width 0.1) (type default)) (layer "Edge.Cuts"))'))
    write_layer.save(root, pcb)
    with pytest.raises(placement_anchor.EdgeCutsUnreadable, match="a footprint carries Edge.Cuts items"):
        _cutouts(pcb)


def test_cutout_boxes_guard_on_an_open_outline_with_no_outer_loop(tmp_path):
    pcb = _board(tmp_path, "open", _rect()[:3])
    with pytest.raises(placement_anchor.EdgeCutsUnreadable, match="no outer loop that can be read: an open chain"):
        _cutouts(pcb)


def test_cutout_boxes_guard_on_problems_besides_the_outer_loop(tmp_path):
    pcb = _board(tmp_path, "stray", _rect() + [_line((130, 90), (140, 90))])  # a closed outer loop plus an open stray line
    with pytest.raises(placement_anchor.EdgeCutsUnreadable, match="problems besides the outer loop: an open chain of gr_line"):
        _cutouts(pcb)
    touching = _board(tmp_path, "touch", _rect() + [_line((X0, Y0 + 5), (X0 + 5, Y0 + 5)), _line((X0 + 5, Y0 + 5), (X0 + 5, Y0 + 10)),
                                                    _line((X0 + 5, Y0 + 10), (X0, Y0 + 10)), _line((X0, Y0 + 10), (X0, Y0 + 5))])
    with pytest.raises(placement_anchor.EdgeCutsUnreadable, match="problems besides the outer loop: Edge.Cuts loop .* touches"):
        _cutouts(touching)


def test_cutout_boxes_guard_on_a_count_mismatch_between_items_and_nodes(tmp_path, monkeypatch):
    pcb = _add(_add(_board(tmp_path, "count", []), CIRCLE), SLOT_CIRCLE)
    real = raw_geometry.read_board_outline

    def one_item_short(root):
        read = real(root)
        return dataclasses.replace(read, items=read.items[:-1], problems=())

    monkeypatch.setattr(raw_geometry, "read_board_outline", one_item_short)
    with pytest.raises(placement_anchor.EdgeCutsUnreadable, match="no outer loop that can be read"):
        _cutouts(pcb)


def test_cutout_boxes_guard_on_an_outer_loop_that_cannot_be_matched_to_its_items(tmp_path, monkeypatch):
    pcb = _add(_board(tmp_path, "match", []), CIRCLE)
    real = edge_loops.classify

    def foreign_outer(root):
        parts = real(root)
        stranger = parse_string('(gr_line (start 0 0) (end 1 1) (layer "Edge.Cuts"))')
        return dataclasses.replace(parts, outer=dataclasses.replace(parts.outer, nodes=(stranger,)))

    monkeypatch.setattr(edge_loops, "classify", foreign_outer)
    with pytest.raises(placement_anchor.EdgeCutsUnreadable, match="cannot be matched to its items"):
        _cutouts(pcb)


def test_cutout_boxes_still_returns_the_slot_of_a_well_formed_board(tmp_path):
    pcb = _board(tmp_path, "slot", _rect() + [_line((130, 90), (140, 90)), _line((140, 90), (140, 100)),
                                              _line((140, 100), (130, 100)), _line((130, 100), (130, 90))])
    assert _cutouts(pcb) == ((130.0, 90.0, 140.0, 100.0),)


# ---- 3. the ARCHITECTURE sentences on well-formed slots ------------------------------------------------


def test_architecture_states_what_well_formed_slots_are_and_that_they_are_not_refused():
    arch = (Path(__file__).parents[1] / "docs" / ("ARCHITECTURE" + ".md")).read_text(encoding="utf-8")
    assert arch.count("well-formed slots") == 2
    assert "circles, rectangular polygons, stadiums and well-formed slots (separate inner loops) are not refused" in arch
    assert "a circle, a stadium or a rectangular `gr_poly` with well-formed slots (`placement_anchor.cutout_boxes`)" in arch


# ---- 4. a gr_curve outer loop ---------------------------------------------------------------------------


@pytest.mark.parametrize("nodes", [[], _rect()[1:]])
def test_a_gr_curve_outer_loop_is_refused_by_name(tmp_path, tools, nodes):
    """The curve alone, or the curve as the missing first side of a rectangle: never read, so never an outline."""
    pcb = _board(tmp_path, "curve", nodes)
    _add(pcb, CURVE)
    with pytest.raises(placement_anchor.EdgeCutsUnreadable, match=r"gr_curve 22222222-2222-2222-2222-222222222222 is not read"):
        _cutouts(pcb)
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 10.0, "y": 5.0}])
    # the frame check refuses first (no code), naming the curve; cutout_boxes above is the second wall
    assert result["status"] == "error" and "1 gr_curve" in result["reason"] and "nothing was moved" in result["reason"], result
    assert pcb.read_bytes() == before


def test_architecture_records_the_measured_chaining_tolerance():
    arch = (Path(__file__).parents[1] / "docs" / ("ARCHITECTURE" + ".md")).read_text(encoding="utf-8")
    assert "reports no `invalid_outline` for gaps of 1e-4, 2e-4, 5e-4, 9e-4, 1e-3, 2e-3, 5e-3, 9.9e-3 and 1e-2 mm and reports it from 1.01e-2 mm up" in arch
    assert "KiCad's default chaining tolerance is 0.01 mm. `edge_loops.SNAP` stays 1e-4 mm" in arch
    assert "KiCad judges the Euclidean length of the gap" in arch and "the tool's `close()` is a box of SNAP per axis" in arch
    assert edge_loops.SNAP == 1e-4
