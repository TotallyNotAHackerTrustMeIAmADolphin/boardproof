"""sheet_extents and page_size_mm (GitHub issue 197): the reusable geometry behind set_paper.

The page sizes are the values measured with kicad-cli 10.0.7 (SVG export of each paper form; the
measurement is repeated in test_set_paper.py against the real binary). The extents are read from the raw
S-expressions; sheets are built through the tools on ``tmp_path`` and nodes the tools cannot make are
parsed in memory only (no KiCad file is edited as text).
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string
from mcp.server.mcpserver import MCPServer

from boardproof import sheet_extents as ex
from boardproof import sheet_replicate, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


def _paper(text: str):
    return parse_string(text)


# --------------------------------------------------------------------------- page_size_mm


@pytest.mark.parametrize("token, size", [
    ('(paper "A4")', (297.0, 210.0)),
    ('(paper "A4" portrait)', (210.0, 297.0)),
    ('(paper "A3" portrait)', (297.0, 420.0)),
    ('(paper "A5")', (210.0, 148.0)),
    ('(paper "A2")', (594.0, 420.0)),
    ('(paper "A1")', (841.0, 594.0)),
    ('(paper "A0")', (1189.0, 841.0)),
    ('(paper "A")', (279.4, 215.9)),
    ('(paper "B")', (431.8, 279.4)),
    ('(paper "C")', (558.8, 431.8)),
    ('(paper "D")', (863.6, 558.8)),
    ('(paper "E")', (1117.6, 863.6)),
    ('(paper "USLetter")', (279.4, 215.9)),
    ('(paper "USLegal")', (355.6, 215.9)),
    ('(paper "USLedger")', (431.8, 279.4)),
    ('(paper "USLegal" portrait)', (215.9, 355.6)),
    ('(paper "User" 300 200)', (300.0, 200.0)),
    ('(paper "User" 210.007 229.997)', (210.007, 229.997)),
    ('(paper "User" 300 200 portrait)', (200.0, 300.0)),   # KiCad reads the flag as a swap (measured)
    ('(paper "a4")', (297.0, 210.0)),                       # KiCad reads any letter case (measured)
    ('(paper "User" 10 5000)', (25.4, 3048.0)),             # KiCad clamps each side (measured)
])
def test_page_size_follows_the_sizes_kicad_prints(token, size):
    got = ex.page_size_mm(_paper(token))
    assert got == pytest.approx(size)


@pytest.mark.parametrize("token", ['(paper "A6")', '(paper "Letter")', '(paper "User")', '(paper "User" 1)', '(paper)', '(paper 4)',
                                   '(paper "User" "a" "b")', '(paper "A4" landscape)', '(paper "A4" 300 200)', '(paper "A4" portrait portrait)',
                                   '(paper "A4" portrait extra)', '(paper "User" 1 2 3)', '(paper "User" 300 200 landscape)'])
def test_page_size_is_none_for_a_token_kicad_cannot_load(token):
    assert ex.page_size_mm(_paper(token)) is None


def test_page_size_none_for_no_node_and_for_a_root_with_two_papers():
    assert ex.page_size_mm(None) is None
    root = parse_string('(kicad_sch (paper "A4") (paper "A3"))')
    assert ex.page_size_mm(root) is None
    assert ex.page_size_mm(parse_string('(kicad_sch (paper "A3"))')) == (420.0, 297.0)


def test_the_table_agrees_with_the_one_sheet_replicate_already_uses():
    for name, size in sheet_replicate._PAPER.items():
        assert ex.PAPER_MM[name] == size
    assert set(ex.PAPER_MM) - set(sheet_replicate._PAPER) == {"USLetter", "USLegal", "USLedger"}


def test_canonical_name():
    assert ex.canonical_name("a4") == "A4" and ex.canonical_name("uslegal") == "USLegal"
    assert ex.canonical_name("Letter") is None and ex.canonical_name(4) is None  # type: ignore[arg-type]


# --------------------------------------------------------------------------- sheet_extents


def _tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def sheet(tmp_path_factory) -> Path:
    base = tmp_path_factory.mktemp("ex197")
    tools = _tools()
    made = project_toolset._project_create(str(base / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    assert tools["add_wires"](str(sch), [{"p1": [50.8, 46.99], "p2": [50.8, 40.64], "route": "direct", "snap": False}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 63.5, "y": 40.64, "text": "NETA", "snap": False}])["status"] == "ok"
    return sch


def _by_kind(entries):
    out: dict[str, list] = {}
    for e in entries:
        out.setdefault(e["kind"], []).append(e)
    return out


def test_a_placed_symbol_is_measured_from_its_raw_at_body_and_pins(sheet):
    root = write_layer.load(sheet)
    entries = _by_kind(ex.sheet_extents(root))
    (r1,) = entries["symbol"]
    assert r1["label"] == "R1" and r1["measured"] == "symbol_body_and_pins"
    raw = next(c for c in root.children if not c.is_atom and c.name == "symbol").find_children("at")[0]
    assert (raw.get_float(0), raw.get_float(1)) == (50.8, 50.8)
    # Device:R: pin ends at +-3.81 from the origin, body 1.016 wide: the box is symmetric around the raw (at)
    assert r1["x1"] < 50.8 < r1["x2"] and r1["y1"] < 50.8 < r1["y2"]
    assert r1["x1"] == pytest.approx(50.8 - 1.016, abs=0.5) and r1["y1"] == pytest.approx(50.8 - 3.81, abs=0.01) and r1["y2"] == pytest.approx(50.8 + 3.81, abs=0.01)
    assert (r1["x"], r1["y"]) == (r1["x1"], r1["y1"])


def test_wires_and_labels_are_measured_at_their_raw_coordinates(sheet):
    entries = _by_kind(ex.sheet_extents(write_layer.load(sheet)))
    (wire,) = entries["wire"]
    assert (wire["x1"], wire["y1"], wire["x2"], wire["y2"]) == (50.8, 40.64, 50.8, 46.99) and wire["measured"] == "points"
    (label,) = entries["label"]
    assert label["label"] == "NETA" and (label["x1"], label["y1"], label["x2"], label["y2"]) == (63.5, 40.64, 63.5, 40.64) and label["measured"] == "origin"


def test_the_quiet_nodes_are_no_objects(sheet):
    kinds = {e["kind"] for e in ex.sheet_extents(write_layer.load(sheet))}
    assert kinds == {"symbol", "wire", "label"}


EXTRA = """(kicad_sch
 (paper "A4")
 (junction (at 10 20) (diameter 0) (color 0 0 0 0) (uuid "j1"))
 (no_connect (at 11 21) (uuid "n1"))
 (text "note" (exclude_from_sim no) (at 12 22 0) (uuid "t1"))
 (text_box "box" (exclude_from_sim no) (at 100 100 0) (size 20 10) (uuid "tb1"))
 (rectangle (start 5 6) (end 30 40) (uuid "r1"))
 (polyline (pts (xy 1 2) (xy 300 3) (xy 4 5)) (uuid "p1"))
 (circle (center 100 100) (radius 10) (uuid "c1"))
 (arc (start 0 10) (mid 10 0) (end 20 10) (uuid "a1"))
 (bus (pts (xy 7 8) (xy 70 8)) (uuid "b1"))
 (bus_entry (at 80 80) (size 2.54 2.54) (uuid "be1"))
 (sheet (at 150 30) (size 40 20) (property "Sheetname" "Power") (uuid "s1"))
 (global_label "VCC" (at 9 9 0) (uuid "g1"))
 (hierarchical_label "H" (at 8 8 0) (uuid "h1"))
 (image (at 30 30) (uuid "i1"))
 (group "g" (uuid "gr") (members "a"))
)"""


def test_every_drawing_kind_has_a_box_from_its_raw_tokens():
    entries = {e["uuid"]: e for e in ex.sheet_extents(parse_string(EXTRA))}
    def box(u):
        return entries[u]["x1"], entries[u]["y1"], entries[u]["x2"], entries[u]["y2"]

    assert box("j1") == (10, 20, 10, 20) and entries["j1"]["measured"] == "origin"
    assert box("n1") == (11, 21, 11, 21)
    assert box("t1") == (12, 22, 12, 22) and entries["t1"]["label"] == "note"
    assert box("tb1") == (100, 100, 120, 110)
    assert box("r1") == (5, 6, 30, 40)
    assert box("p1") == (1, 2, 300, 5)
    assert box("c1") == (90, 90, 110, 110)
    assert box("b1") == (7, 8, 70, 8)
    assert box("be1") == (80, 80, 82.54, 82.54)
    assert box("s1") == (150, 30, 190, 50) and entries["s1"]["label"] == "Power"
    assert box("g1") == (9, 9, 9, 9) and box("h1") == (8, 8, 8, 8) and box("i1") == (30, 30, 30, 30)
    assert "gr" not in entries   # a group is no drawing object


def test_an_arc_box_includes_the_extreme_it_sweeps_through_not_just_three_points():
    entries = {e["uuid"]: e for e in ex.sheet_extents(parse_string(EXTRA))}
    a = entries["a1"]
    # the circle through (0,10) (10,0) (20,10) has centre (10,10), radius 10: the top (y=0) is the mid point;
    # the arc is the upper half, so its left, right and top extremes are the three points
    assert (a["x1"], a["y1"], a["x2"], a["y2"]) == (0, 0, 20, 10)
    swept = parse_string('(kicad_sch (arc (start 0 10) (mid 10 20) (end 20 10) (uuid "a2")))')
    box = ex.sheet_extents(swept)[0]
    assert box["y2"] == pytest.approx(20) and box["y1"] == pytest.approx(10)
    # three quarters of the circle (centre (10,10), radius 10) from (20,10) over (0,10) to (10,20): the three
    # points span y 10..20, the arc sweeps through the top (y = 0) as well
    big = parse_string('(kicad_sch (arc (start 20 10) (mid 0 10) (end 10 20) (uuid "a3")))')
    box = ex.sheet_extents(big)[0]
    assert (box["x1"], box["y1"], box["x2"], box["y2"]) == pytest.approx((0, 0, 20, 20))


def test_an_object_that_cannot_be_measured_is_returned_without_a_box_never_dropped():
    root = parse_string('(kicad_sch (table (column_count 1)) (wire (uuid "w1")) (junction (uuid "j")) (arc (start 0 0) (uuid "a")) (symbol (uuid "s")))')
    entries = ex.sheet_extents(root)
    assert [e["kind"] for e in entries] == ["table", "wire", "junction", "arc", "symbol"]
    assert all(e["measured"] == "none" and e["x1"] is None for e in entries)


def test_a_non_numeric_coordinate_is_not_measured():
    root = parse_string('(kicad_sch (junction (at "a" "b") (uuid "j")))')
    assert ex.sheet_extents(root)[0]["measured"] == "none"


def test_union_box_and_outside_page():
    entries = ex.sheet_extents(parse_string(EXTRA))
    assert ex.union_box(entries) == (0, 0, 300, 110) or ex.union_box(entries)[2] == 300
    assert ex.union_box([]) is None
    outside = ex.outside_page(entries, (297.0, 210.0))
    assert [e["uuid"] for e in outside] == ["p1"]            # only the polyline reaches x = 300
    assert ex.outside_page(entries, (420.0, 297.0)) == []
    inside_edge = [{"x1": 0.0, "y1": 0.0, "x2": 297.0, "y2": 210.0}]
    assert ex.outside_page(inside_edge, (297.0, 210.0)) == []   # touching the page edge is inside
    assert len(ex.outside_page([{"x1": -0.01, "y1": 0.0, "x2": 1.0, "y2": 1.0}], (297.0, 210.0))) == 1
    assert len(ex.outside_page([{"x1": 0.0, "y1": 0.0, "x2": 297.01, "y2": 1.0}], (297.0, 210.0))) == 1
    assert len(ex.outside_page([{"x1": 0.0, "y1": 0.0, "x2": 1.0, "y2": 210.01}], (297.0, 210.0))) == 1
    assert len(ex.outside_page([{"x1": 0.0, "y1": -0.01, "x2": 1.0, "y2": 1.0}], (297.0, 210.0))) == 1
    assert ex.outside_page([{"x1": None, "y1": None, "x2": None, "y2": None}], (1.0, 1.0)) == []


def test_boxes_are_rounded_and_finite():
    root = parse_string('(kicad_sch (junction (at 1.123456789 2.5) (uuid "j")))')
    e = ex.sheet_extents(root)[0]
    assert e["x1"] == 1.1235 and math.isfinite(e["y1"])


HIDDEN_PIN = """(kicad_sch
 (lib_symbols (symbol "X:Y" (symbol "Y_0_1" (rectangle (start -1 1) (end 1 -1)))
   (symbol "Y_1_1" (pin passive line (at 0 10 270) (length 2.54) hide (name "~") (number "1")))))
 (symbol (lib_id "X:Y") (at 50 50 0) (unit 1) (uuid "s1") (property "Reference" "U1" (at 0 0 0)))
)"""


def test_a_hidden_pin_far_from_the_body_still_counts_toward_the_symbol_box():
    """symbol_box leaves hidden pins out; a hidden power pin is still a connection point on the sheet."""
    (entry,) = ex.sheet_extents(parse_string(HIDDEN_PIN))
    assert entry["label"] == "U1" and entry["x1"] < 50 < entry["x2"]
    assert (entry["x1"], entry["y1"], entry["x2"], entry["y2"]) == pytest.approx((49.0, 40.0, 51.0, 51.0))   # the hidden pin end is 10 mm above the origin (Y down)


def test_a_non_finite_coordinate_is_not_measured():
    root = parse_string('(kicad_sch (junction (at 1e999 2) (uuid "j")) (junction (at -1e999 2) (uuid "k")))')
    assert [e["measured"] for e in ex.sheet_extents(root)] == ["none", "none"]


HIDDEN_PIN_RIGHT = """(kicad_sch
 (lib_symbols (symbol "X:Y" (symbol "Y_0_1" (rectangle (start -1 1) (end 1 -1)))
   (symbol "Y_1_1" (pin passive line (at 10 0 180) (length 2.54) hide (name "~") (number "1"))
                   (pin passive line (at -1 0 0) (length 0) (name "A") (number "2")))))
 (symbol (lib_id "X:Y") (at 50 50 0) (unit 1) (uuid "s1") (property "Reference" "U1" (at 0 0 0)))
)"""


def test_a_hidden_pin_end_outside_the_body_makes_the_symbol_not_fit_a_smaller_page():
    entries = ex.sheet_extents(parse_string(HIDDEN_PIN_RIGHT))
    (entry,) = entries
    assert entry["x2"] == pytest.approx(60.0)          # the hidden pin end, 10 mm right of the origin; the body ends at 51
    assert [e["uuid"] for e in ex.outside_page(entries, (55.0, 100.0))] == ["s1"]
    assert ex.outside_page(entries, (60.0, 100.0)) == []


def test_a_bezier_is_measured_by_its_control_hull_and_the_text_says_so():
    root = parse_string('(kicad_sch (bezier (pts (xy 0 50) (xy 10 50) (xy 20 70) (xy 30 50)) (uuid "z")))')
    (entry,) = ex.sheet_extents(root)
    assert (entry["x1"], entry["y1"], entry["x2"], entry["y2"]) == (0, 50, 30, 70)      # the curve itself stays below y 62.9
    assert any("bezier" in n and "control points" in n and "too large" in n for n in ex.NOT_MEASURED)
