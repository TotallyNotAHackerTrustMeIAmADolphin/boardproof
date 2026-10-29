"""Tests for the board outline and mounting-hole step (src/kicad_mcp/board_outline.py).

Contract: build ticket 19.
Seams: ``propose_board_outline`` and ``confirm_board_outline``. Fixtures are the golden
board copied to tmp (its Edge.Cuts rectangle starts at (123.5, 85), NOT at the page
origin, which is exactly the origin trap kicad_tools' convenience properties fall into).
Every fact is read back from the raw S-expression; expected values are literals.
"""

from __future__ import annotations

import hashlib
import math
import shutil
from pathlib import Path

import pytest

from kicad_mcp import board_outline
from kicad_mcp import write_layer
from tests import _kicad_faults as faults

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"

HOLE = "MountingHole:MountingHole_3.2mm_M3"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    shutil.copytree(GOLDEN_DIR, tmp_path / "proj")
    return tmp_path / "proj" / GOLDEN_NAME


def _root(pcb: Path):
    from kicad_tools.sexp import parse_file

    return parse_file(str(pcb))


def _edge(pcb: Path) -> list:
    return [
        n
        for n in _root(pcb).children
        if n.name.startswith("gr_") and n.find("layer") is not None and n.find("layer").values[0] == "Edge.Cuts"
    ]


def _xy(node, tag: str) -> tuple[float, float]:
    at = node.find(tag)
    return float(at.values[0]), float(at.values[1])


def _fp_at(pcb: Path, ref: str) -> tuple[float, float]:
    for fp in _root(pcb).children:
        if fp.name != "footprint":
            continue
        for prop in fp.children:
            if prop.name == "property" and prop.values and prop.values[0] == "Reference" and prop.values[1] == ref:
                return _xy(fp, "at")
    raise AssertionError(f"{ref} not in board")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hash(directory: Path) -> dict[str, str]:
    return {str(p.relative_to(directory)): _sha(p) for p in sorted(directory.rglob("*")) if p.is_file()}


def _no_outline_drc(path, **_kw):
    return {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _apply(board: Path, tmp_path: Path, outline, holes=None, origin="page", drc=_no_outline_drc, name="out"):
    proposal = board_outline.propose_board_outline(
        board, outline, mounting_holes=holes, origin=origin, output_dir=tmp_path / name
    )
    assert proposal["status"] == "proposed", proposal
    return proposal, board_outline.confirm_board_outline(board, proposal["revision_token"], drc=drc)


RECT = {"x": 100.0, "y": 70.0, "width": 100.0, "height": 70.0}


def _closed_chain(edge_nodes) -> bool:
    """Every endpoint is shared by exactly two Edge.Cuts items."""
    from collections import Counter

    ends: Counter = Counter()
    for n in edge_nodes:
        if n.name == "gr_line":
            pts = [_xy(n, "start"), _xy(n, "end")]
        elif n.name == "gr_arc":
            pts = [_xy(n, "start"), _xy(n, "end")]
        else:
            return False
        for p in pts:
            ends[(round(p[0], 4), round(p[1], 4))] += 1
    return bool(ends) and all(c == 2 for c in ends.values())


# --- dry run --------------------------------------------------------------------


def test_dry_run_writes_nothing_and_source_untouched(board: Path, tmp_path: Path) -> None:
    before = _tree_hash(board.parent)
    out = tmp_path / "out"
    proposal = board_outline.propose_board_outline(board, RECT, mounting_holes=None, output_dir=out)
    assert proposal["status"] == "proposed"
    assert proposal["revision_token"]
    assert not out.exists()
    assert _tree_hash(board.parent) == before
    assert proposal["replaces"]["gr_line"] == 4  # the golden board's four Edge.Cuts lines


# --- outline --------------------------------------------------------------------


def test_outline_replaces_edge_cuts_rectangle_and_source_stays(board: Path, tmp_path: Path) -> None:
    before = _tree_hash(board.parent)
    _, result = _apply(board, tmp_path, RECT)
    assert result["status"] == "built", result
    out_pcb = Path(result["output_pcb"])
    edge = _edge(out_pcb)
    assert len(edge) == 4 and all(n.name == "gr_line" for n in edge)
    xs = {p[0] for n in edge for p in (_xy(n, "start"), _xy(n, "end"))}
    ys = {p[1] for n in edge for p in (_xy(n, "start"), _xy(n, "end"))}
    assert xs == {100.0, 200.0} and ys == {70.0, 140.0}
    assert _closed_chain(edge)
    assert _tree_hash(board.parent) == before  # source untouched
    assert result["outline_drc"]["status"] == "clean"


def test_rounded_corners_use_arcs_and_close(board: Path, tmp_path: Path) -> None:
    _, result = _apply(board, tmp_path, {**RECT, "corner_radius": 3.0})
    assert result["status"] == "built", result
    edge = _edge(Path(result["output_pcb"]))
    assert sorted(n.name for n in edge) == ["gr_arc"] * 4 + ["gr_line"] * 4
    assert _closed_chain(edge)
    for arc in (n for n in edge if n.name == "gr_arc"):
        s, m, e = _xy(arc, "start"), _xy(arc, "mid"), _xy(arc, "end")
        # start, mid and end all lie 3 mm from one corner centre: chord from start to end is 3*sqrt(2)
        assert math.dist(s, e) == pytest.approx(3.0 * math.sqrt(2), abs=1e-3)
        assert math.dist(s, m) == pytest.approx(math.dist(m, e), abs=1e-3)


def test_closed_outline_has_no_outline_error_in_kicad_cli(board: Path, tmp_path: Path) -> None:
    """Real kicad-cli: no outline violation on the revision."""
    from kicad_mcp.kicad_cli import run_drc

    proposal = board_outline.propose_board_outline(
        board, {**RECT, "corner_radius": 2.0}, output_dir=tmp_path / "out"
    )
    result = board_outline.confirm_board_outline(board, proposal["revision_token"])  # real kicad-cli
    assert result["status"] == "built", result
    assert result["outline_drc"]["status"] == "clean"
    report = run_drc(result["output_pcb"])
    kinds = {v["type"] for v in report["violations"]}
    assert not kinds & {"malformed_outline", "invalid_outline"}


def test_unclosed_outline_is_detected_by_the_same_check(board: Path, tmp_path: Path) -> None:
    """The check is not vacuous: an outline DRC report naming an outline error fails the revision."""

    def bad_drc(path, **_kw):
        return {
            "violations": [{"type": "malformed_outline", "severity": "error", "description": "x", "items": []}],
            "unconnected_items": [],
            "schematic_parity": [],
            "ignored_checks": [],
        }

    proposal = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out")
    result = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=bad_drc)
    assert result["status"] == "error"
    assert not (tmp_path / "out").exists()


# --- mounting holes -------------------------------------------------------------


def test_holes_are_placed_at_declared_absolute_positions_not_origin_shifted(board: Path, tmp_path: Path) -> None:
    # The old outline starts at (123.5, 85): kicad_tools' add_footprint would add that offset.
    holes = [
        {"reference": "H1", "x": 105.0, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 3.0},
        {"reference": "H2", "x": 195.0, "y": 135.0, "footprint": HOLE, "keepout_radius_mm": 3.0},
    ]
    _, result = _apply(board, tmp_path, RECT, holes)
    assert result["status"] == "built", result
    out_pcb = Path(result["output_pcb"])
    assert _fp_at(out_pcb, "H1") == (105.0, 75.0)
    assert _fp_at(out_pcb, "H2") == (195.0, 135.0)
    assert [h["reference"] for h in result["holes"]] == ["H1", "H2"]
    assert result["holes"][0]["at"] == [105.0, 75.0]

    keepouts = [n for n in _root(out_pcb).children if n.name == "zone" and n.find("keepout") is not None]
    assert len(keepouts) == 2
    for zone in keepouts:
        flags = {c.name: c.values[0] for c in zone.find("keepout").children}
        assert flags["tracks"] == "not_allowed" and flags["vias"] == "not_allowed"
        assert flags["copperpour"] == "not_allowed"
        pts = [(float(p.values[0]), float(p.values[1])) for p in zone.find("polygon").find("pts").children if p.name == "xy"]
        cx = sum(p[0] for p in pts) / len(pts)
        cy = sum(p[1] for p in pts) / len(pts)
        assert (round(cx, 3), round(cy, 3)) in {(105.0, 75.0), (195.0, 135.0)}
        # every vertex at least the declared radius from the hole centre (never an undersized keepout)
        assert min(math.dist((cx, cy), p) for p in pts) >= 3.0 - 1e-6


def test_hole_outside_outline_is_refused_by_name(board: Path, tmp_path: Path) -> None:
    holes = [
        {"reference": "H1", "x": 105.0, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 3.0},
        {"reference": "H7", "x": 250.0, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 3.0},
    ]
    proposal = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert proposal["status"] == "refused"
    assert proposal["revision_token"] is None
    assert [h["reference"] for h in proposal["refused_holes"]] == ["H7"]
    assert "H7" in proposal["reason"]


def test_hole_whose_keepout_crosses_the_edge_is_refused(board: Path, tmp_path: Path) -> None:
    holes = [{"reference": "H1", "x": 101.0, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 3.0}]
    proposal = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert proposal["status"] == "refused"


def test_hole_in_rounded_corner_cut_is_refused(board: Path, tmp_path: Path) -> None:
    # Both clear the straight edges by more than the keepout (1.6 mm, the M3 drill radius, so the
    # keepout-smaller-than-hole refusal cannot answer first, as it did for a 1.0 mm keepout). H1's
    # centre lies in the 8 mm corner cut; H2's centre is on the board but its keepout crosses the arc.
    holes = [
        {"reference": "H1", "x": 101.7, "y": 71.7, "footprint": HOLE, "keepout_radius_mm": 1.6},
        {"reference": "H2", "x": 103.0, "y": 73.0, "footprint": HOLE, "keepout_radius_mm": 1.6},
    ]
    proposal = board_outline.propose_board_outline(
        board, {**RECT, "corner_radius": 8.0}, mounting_holes=holes, output_dir=tmp_path / "out"
    )
    assert proposal["status"] == "refused"
    assert [h["reference"] for h in proposal["refused_holes"]] == ["H1", "H2"]
    assert all("not inside the new outline" in h["reason"] for h in proposal["refused_holes"]), proposal


@pytest.mark.parametrize("corner", [0.0, 3.0])
def test_hole_well_inside_a_rounded_outline_is_proposed(board: Path, tmp_path: Path, corner: float) -> None:
    """Build ticket 52's reproduction: a 3 mm keepout exceeds a 3 mm corner radius (the 32-gon
    circumscribes the circle), and the hole 10 mm in from two edges was refused anywhere."""
    holes = [{"reference": "H1", "x": 110.0, "y": 80.0, "footprint": HOLE, "keepout_radius_mm": 3.0}]
    proposal = board_outline.propose_board_outline(
        board, {**RECT, "corner_radius": corner}, mounting_holes=holes, output_dir=tmp_path / "out"
    )
    assert proposal["status"] == "proposed", proposal


# The boundary grid: a 3 mm keepout is a 32-gon whose vertices lie 3 / cos(pi / 32) from the hole,
# so the disc that must fit is 3.0145157 mm; DELTA puts a hole just inside or just outside it.
# (Build ticket 72: this literal was 3.0145245, 8.8 nm too large; the tight grid needs it exact.)
KEEPOUT_VERTEX_R = 3.0145157
DELTA = 0.01
# Build ticket 72: at 10 um a corner check with up to 9 um of slack (_EPS -> 0.009, or the margin
# scaled by 0.998) still passed every grid hole. The 0.1 um grid kills any slack of 0.1 um or
# more; test_corner_check_has_no_slack_beyond_eps below probes 10 nm directly.
TIGHT_DELTA = 1e-4
CORNERS = {"TL": (100.0, 70.0, -1, -1), "TR": (200.0, 70.0, 1, -1), "BR": (200.0, 140.0, 1, 1), "BL": (100.0, 140.0, -1, 1)}


def test_boundary_grid_radius_is_the_keepout_vertex_radius() -> None:
    """The grid is only as tight as its radius: a 32-gon of apothem 3 mm has circumradius
    3 / cos(pi / 32) = 3.01451572 mm."""
    assert abs(KEEPOUT_VERTEX_R - 3.0 / math.cos(math.pi / 32)) < 1e-7


@pytest.mark.parametrize("margin", [0.0, 3.0])
@pytest.mark.parametrize("name", list(CORNERS))
def test_corner_check_has_no_slack_beyond_eps(name: str, margin: float) -> None:
    """Build ticket 72, review round 1: the disc of radius ``margin`` fits a corner of RECT with
    an 8 mm radius exactly while its centre is at most 8 - margin from the corner centre. On the
    diagonal, 10 nm (1e-5 mm, ten times _EPS) past that limit is outside and 10 nm short is inside;
    float error at 200 mm is about 3e-14 mm."""
    cx, cy, sx, sy = CORNERS[name]
    centre = (cx - sx * 8.0, cy - sy * 8.0)  # (108, 78) for TL
    for slack, fits in ((1e-5, False), (-1e-5, True)):
        reach = (8.0 - margin + slack) / math.sqrt(2)
        x, y = centre[0] + sx * reach, centre[1] + sy * reach
        assert board_outline._inside(RECT, 8.0, x, y, margin) is fits, (name, margin, slack)


def _boundary_holes(corner: float, inside: bool, delta: float = DELTA) -> list[dict]:
    """One hole per corner and one per straight-edge midpoint of RECT, each ``delta`` inside (or
    outside) the tightest position its 3 mm keepout allows."""
    step = -delta if inside else delta  # how far past the limit, towards the outline
    holes = []
    for name, (cx, cy, sx, sy) in CORNERS.items():
        if corner > KEEPOUT_VERTEX_R:  # the arc binds: on the diagonal from the corner centre
            reach = (corner - KEEPOUT_VERTEX_R + step) / math.sqrt(2)
            x, y = cx - sx * corner + sx * reach, cy - sy * corner + sy * reach
        else:  # the shrunk outline has sharp corners: both straight edges bind
            x, y = cx - sx * (KEEPOUT_VERTEX_R - step), cy - sy * (KEEPOUT_VERTEX_R - step)
        holes.append({"reference": f"H{name}", "x": x, "y": y})
    limit = KEEPOUT_VERTEX_R - step
    holes += [
        {"reference": "HT", "x": 150.0, "y": 70.0 + limit},
        {"reference": "HB", "x": 150.0, "y": 140.0 - limit},
        {"reference": "HL", "x": 100.0 + limit, "y": 105.0},
        {"reference": "HR", "x": 200.0 - limit, "y": 105.0},
    ]
    return [{**h, "footprint": HOLE, "keepout_radius_mm": 3.0} for h in holes]


@pytest.mark.parametrize("delta", [DELTA, TIGHT_DELTA])
@pytest.mark.parametrize("corner", [0.0, 1.0, 3.0, 8.0])
def test_holes_just_inside_every_corner_and_edge_are_proposed(
    board: Path, tmp_path: Path, corner: float, delta: float
) -> None:
    holes = _boundary_holes(corner, inside=True, delta=delta)
    proposal = board_outline.propose_board_outline(
        board, {**RECT, "corner_radius": corner}, mounting_holes=holes, output_dir=tmp_path / "out"
    )
    assert proposal["status"] == "proposed", proposal


@pytest.mark.parametrize("delta", [DELTA, TIGHT_DELTA])
@pytest.mark.parametrize("corner", [0.0, 1.0, 3.0, 8.0])
def test_holes_just_outside_every_corner_and_edge_are_each_refused(
    board: Path, tmp_path: Path, corner: float, delta: float
) -> None:
    holes = _boundary_holes(corner, inside=False, delta=delta)
    proposal = board_outline.propose_board_outline(
        board, {**RECT, "corner_radius": corner}, mounting_holes=holes, output_dir=tmp_path / "out"
    )
    assert proposal["status"] == "refused" and proposal["revision_token"] is None, proposal
    refused = proposal["refused_holes"]
    assert sorted(r["reference"] for r in refused) == ["HB", "HBL", "HBR", "HL", "HR", "HT", "HTL", "HTR"]
    assert all("not inside the new outline" in r["reason"] for r in refused), refused


def _arc_samples(start, mid, end, n: int = 256) -> list[tuple[float, float]]:
    """Points on the circle through start, mid and end, swept from start to end via mid."""
    (ax, ay), (bx, by), (cx, cy) = start, mid, end
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    ux = ((ax**2 + ay**2) * (by - cy) + (bx**2 + by**2) * (cy - ay) + (cx**2 + cy**2) * (ay - by)) / d
    uy = ((ax**2 + ay**2) * (cx - bx) + (bx**2 + by**2) * (ax - cx) + (cx**2 + cy**2) * (bx - ax)) / d
    r = math.dist((ux, uy), start)
    a0, am, a1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (start, mid, end))
    sweep = (a1 - a0) % (2 * math.pi)
    if (am - a0) % (2 * math.pi) > sweep:  # mid is not on the counter-clockwise sweep
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a0 + sweep * i / n), uy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def _raw_outline_hull(pcb: Path) -> list[tuple[float, float]]:
    """The convex hull of the raw Edge.Cuts gr_line ends and densely sampled gr_arc points.
    Chords lie inside a convex outline, so a point inside this hull is inside the outline."""
    pts: list[tuple[float, float]] = []
    for n in _edge(pcb):
        if n.name == "gr_line":
            pts += [_xy(n, "start"), _xy(n, "end")]
        else:
            assert n.name == "gr_arc", n.name
            pts += _arc_samples(_xy(n, "start"), _xy(n, "mid"), _xy(n, "end"))
    pts = sorted(set(pts))

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: list = []
    upper: list = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 0:
            lower.pop()
        lower.append(p)
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 0:
            upper.pop()
        upper.append(p)
    return lower[:-1] + upper[:-1]  # counter-clockwise


def _inset(hull, p) -> float:
    """Signed distance of p inside the convex hull (negative: outside)."""
    best = math.inf
    for a, b in zip(hull, hull[1:] + hull[:1], strict=True):
        length = math.dist(a, b)
        best = min(best, ((b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])) / length)
    return best


@pytest.mark.parametrize("corner", [0.0, 1.0, 3.0, 8.0])
def test_every_keepout_vertex_of_boundary_holes_lies_inside_the_written_outline(
    board: Path, tmp_path: Path, corner: float
) -> None:
    """Independent oracle (hard rule 2): the raw keepout (polygon ...) vertices of the tightest
    accepted holes, checked against the raw gr_line / gr_arc outline written to the revision."""
    _, result = _apply(board, tmp_path, {**RECT, "corner_radius": corner}, _boundary_holes(corner, inside=True))
    assert result["status"] == "built", result
    out_pcb = Path(result["output_pcb"])
    hull = _raw_outline_hull(out_pcb)
    keepouts = {
        str(z.find("name").values[0]): [
            (float(p.values[0]), float(p.values[1])) for p in z.find("polygon").find("pts").children if p.name == "xy"
        ]
        for z in _root(out_pcb).children
        if z.name == "zone" and z.find("keepout") is not None
    }
    assert sorted(keepouts) == [f"KEEPOUT_{r}" for r in ("HB", "HBL", "HBR", "HL", "HR", "HT", "HTL", "HTR")]
    for name, vertices in keepouts.items():
        assert len(vertices) == 32, name
        clearance = min(_inset(hull, v) for v in vertices)
        # inside the outline, and no more than about DELTA from it: the grid really probes the limit
        assert -1e-6 <= clearance <= DELTA + 2e-3, (name, clearance)


def test_duplicate_reference_and_unknown_footprint_are_refused(board: Path, tmp_path: Path) -> None:
    holes = [{"reference": "U1", "x": 150.0, "y": 100.0, "footprint": HOLE, "keepout_radius_mm": 3.0}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "o1")
    assert p["status"] == "refused" and "U1" in p["reason"]
    holes = [{"reference": "H1", "x": 150.0, "y": 100.0, "footprint": "MountingHole:Nope_9mm", "keepout_radius_mm": 3.0}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "o2")
    assert p["status"] == "refused" and "H1" in p["reason"]


def test_keepout_smaller_than_the_hole_is_refused(board: Path, tmp_path: Path) -> None:
    # M3 hole drill is 3.2 mm, radius 1.6; a 1.0 mm keepout would not even cover the drill.
    holes = [{"reference": "H1", "x": 150.0, "y": 100.0, "footprint": HOLE, "keepout_radius_mm": 1.0}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "o")
    assert p["status"] == "refused" and "H1" in p["reason"]


# --- footprints outside the new outline ------------------------------------------


def test_footprints_outside_new_outline_are_reported_not_moved(board: Path, tmp_path: Path) -> None:
    small = {"x": 130.0, "y": 90.0, "width": 20.0, "height": 20.0}
    before = {}
    for fp in _root(board).children:
        if fp.name == "footprint":
            ref = next(p.values[1] for p in fp.children if p.name == "property" and p.values[0] == "Reference")
            before[ref] = _xy(fp, "at")
    proposal, result = _apply(board, tmp_path, small)
    assert result["status"] == "built"
    outside = {o["reference"] for o in result["outside_outline"]}
    assert outside, "the 20x20 outline must leave footprints outside"
    assert {o["reference"] for o in proposal["outside_outline"]} == outside
    for ref, (x, y) in before.items():
        inside = 130.0 <= x <= 150.0 and 90.0 <= y <= 110.0
        if not inside:
            assert ref in outside, ref  # origin outside is always reported
    for ref in outside:
        assert _fp_at(Path(result["output_pcb"]), ref) == before[ref]  # not moved


@pytest.mark.parametrize(
    ("corner", "expected"),
    [
        (0.0, []),  # the same rectangle with sharp corners holds C1: only an arc puts it outside
        (3.0, [{"reference": "C1", "at": [133.5, 93.0], "origin_outside": False, "points_outside": 3}]),
        (8.0, [{"reference": "C1", "at": [133.5, 93.0], "origin_outside": True, "points_outside": 9}]),
    ],
)
def test_footprint_in_a_rounded_corner_cut_is_reported_outside(
    board: Path, tmp_path: Path, corner: float, expected: list
) -> None:
    """Build ticket 72: the footprint check calls the corner test at margin 0, and no other test
    reached that path inside a corner cut. C1 sits at raw (at 133.5 93) with pads (at +-0.48 0)
    (size 0.56 0.62): its origin lies 1.0 mm inside the outline's top and left edges (as (101, 71)
    on an outline at (100, 70)), its pad corners 0.21 mm or more, so every point is inside the
    rectangle. Under the 8 mm arc (centre (140.5, 100)) all nine points lie past it; the nearest,
    (134.29, 93.31), is 9.13 mm from the centre. Under the 3 mm arc (centre (135.5, 95)) only three
    pad corners do: (132.71, 92.69) at 3.622, (132.71, 93.31) at 3.262 and (133.33, 92.69) at
    3.169 mm, while the origin, at 2.828 mm, stays inside (review round 1: a check that skips small
    arcs, counts every point or always flags the origin passed the 8 mm case alone)."""
    outline = {"x": 132.5, "y": 92.0, "width": 100.0, "height": 70.0, "corner_radius": corner}
    proposal = board_outline.propose_board_outline(board, outline, output_dir=tmp_path / "out")
    assert proposal["status"] == "proposed", proposal
    assert [o for o in proposal["outside_outline"] if o["reference"] == "C1"] == expected


def _raw_footprint_points(pcb: Path, ref: str) -> list[tuple[float, float]]:
    """The footprint's origin and the bounding-square corners of its pads (half the larger
    ``size`` side, as the module documents), read straight from the raw ``(at)`` and ``(size)``
    nodes, never from kicad_tools' position properties (CLAUDE.md hard rule 2). Only unrotated
    footprints and pads are supported, which is all the corner tests below need."""
    for fp in _root(pcb).children:
        if fp.name != "footprint":
            continue
        refs = [p.values[1] for p in fp.children if p.name == "property" and p.values[:1] == ["Reference"]]
        if refs != [ref]:
            continue
        at = fp.find("at")
        assert [float(v) for v in at.values[2:]] in ([], [0.0]), f"{ref} is rotated: {at.values}"
        ox, oy = float(at.values[0]), float(at.values[1])
        points = [(ox, oy)]
        for pad in (c for c in fp.children if c.name == "pad"):
            pad_at, size = pad.find("at"), pad.find("size")
            assert [float(v) for v in pad_at.values[2:]] in ([], [0.0]), f"{ref} pad is rotated"
            half = max(float(size.values[0]), float(size.values[1])) / 2
            px, py = ox + float(pad_at.values[0]), oy + float(pad_at.values[1])
            points += [(px + sx * half, py + sy * half) for sx in (-1, 1) for sy in (-1, 1)]
        return points
    raise AssertionError(f"{ref} not in board")


def _past_top_left_arc(point: tuple[float, float], x0: float, y0: float, radius: float) -> bool:
    """Does ``point``, inside the rectangle's top and left edges, lie in the cut of a top-left
    corner arc of ``radius``: up and left of the arc centre and farther than ``radius`` from it?"""
    cx, cy = x0 + radius, y0 + radius
    return point[0] < cx and point[1] < cy and math.dist(point, (cx, cy)) > radius


# The small corner radii: 1 um to 2.9 mm, so a skip threshold anywhere in that range is caught.
SMALL_CORNERS = [0.001, 0.5, 1.0, 2.9]


@pytest.mark.parametrize(
    ("corner", "origin_outside", "points_outside"),
    [
        (0.001, False, 1),
        (0.5, False, 1),
        (1.0, False, 1),
        (2.9, True, 5),  # the origin 0.033 mm past the arc, four pad corners 0.19 to 0.79 mm
    ],
)
def test_footprint_in_a_corner_cut_smaller_than_3_mm_is_reported_outside(
    board: Path, tmp_path: Path, corner: float, origin_outside: bool, points_outside: int
) -> None:
    """Build ticket 84: after ticket 72 the mutant ``if corner <= 2.9: return True`` in
    ``_inside`` still passed, as no footprint sat in a corner cut under 3 mm at margin 0.
    C1's top-left pad corner, from its raw (at 133.5 93), pad (at -0.48 0) and (size 0.56 0.62),
    is (132.71, 92.69). The outline is placed so that this point sits a tenth of the corner
    radius inside both the top and the left edge (0.1 mm for the 1 mm corner): it then lies
    sqrt(2) * 0.9 = 1.273 radii from the arc centre, 0.273 radii past the arc (0.27 mm at 1 mm;
    0.27 um at 1 um, still about 270 times _EPS), while the sharp-cornered rectangle holds it. A
    skip threshold anywhere from 0.001 to 2.9 mm (``<= 0.9``, ``< 1.0``, ``<= 2.9``) fails one of
    the radii. ``points_outside`` and ``origin_outside`` are derived here from the raw nodes and
    must also match the literals worked out by hand."""
    points = _raw_footprint_points(board, "C1")
    corner_point = (min(p[0] for p in points), min(p[1] for p in points))
    assert corner_point == pytest.approx((132.71, 92.69), abs=1e-9)
    inset = corner / 10
    x0, y0 = corner_point[0] - inset, corner_point[1] - inset
    assert all(p[0] > x0 and p[1] > y0 for p in points)  # the sharp rectangle holds every point
    derived = [p for p in points if _past_top_left_arc(p, x0, y0, corner)]
    assert (len(derived), points[0] in derived) == (points_outside, origin_outside)

    outline = {"x": x0, "y": y0, "width": 100.0, "height": 70.0, "corner_radius": corner}
    proposal = board_outline.propose_board_outline(board, outline, output_dir=tmp_path / "out")
    assert proposal["status"] == "proposed", proposal
    assert [o for o in proposal["outside_outline"] if o["reference"] == "C1"] == [
        {"reference": "C1", "at": [133.5, 93.0], "origin_outside": origin_outside, "points_outside": len(derived)}
    ]


@pytest.mark.parametrize("corner", SMALL_CORNERS)
def test_footprint_just_inside_a_small_corner_arc_is_not_reported(board: Path, tmp_path: Path, corner: float) -> None:
    """The counterpart of the test above: with C1's top-left pad corner 0.35 radii inside both
    edges it lies sqrt(2) * 0.65 = 0.919 radii from the arc centre, inside the arc, and every other
    C1 point is farther in, so a check that cuts the whole corner square (or too large an arc)
    fails here."""
    points = _raw_footprint_points(board, "C1")
    corner_point = (min(p[0] for p in points), min(p[1] for p in points))
    inset = corner * 0.35
    x0, y0 = corner_point[0] - inset, corner_point[1] - inset
    assert not [p for p in points if _past_top_left_arc(p, x0, y0, corner)]

    outline = {"x": x0, "y": y0, "width": 100.0, "height": 70.0, "corner_radius": corner}
    proposal = board_outline.propose_board_outline(board, outline, output_dir=tmp_path / "out")
    assert proposal["status"] == "proposed", proposal
    assert [o for o in proposal["outside_outline"] if o["reference"] == "C1"] == []


# --- origin handling --------------------------------------------------------------


def _with_aux_origin(board: Path, x: float, y: float) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    pcb = PCB.load(board)
    setup = pcb._sexp.find("setup")
    old = setup.find("aux_axis_origin")
    if old is not None:
        setup.remove(old)
    setup.append(SExp.list("aux_axis_origin", x, y))
    write_layer.save_pcb(pcb, board)


def test_board_relative_frame_adds_aux_axis_origin(board: Path, tmp_path: Path) -> None:
    _with_aux_origin(board, 100.0, 50.0)
    outline = {"x": 0.0, "y": 0.0, "width": 60.0, "height": 40.0}
    holes = [{"reference": "H1", "x": 5.0, "y": 5.0, "footprint": HOLE, "keepout_radius_mm": 3.0}]
    proposal, result = _apply(board, tmp_path, outline, holes, origin="aux_axis")
    assert result["status"] == "built", result
    out_pcb = Path(result["output_pcb"])
    xs = {p[0] for n in _edge(out_pcb) for p in (_xy(n, "start"), _xy(n, "end"))}
    ys = {p[1] for n in _edge(out_pcb) for p in (_xy(n, "start"), _xy(n, "end"))}
    assert xs == {100.0, 160.0} and ys == {50.0, 90.0}  # file coordinates
    assert _fp_at(out_pcb, "H1") == (105.0, 55.0)
    assert result["origin"] == {"frame": "aux_axis", "offset": [100.0, 50.0]}


def test_page_frame_ignores_aux_axis_origin(board: Path, tmp_path: Path) -> None:
    _with_aux_origin(board, 100.0, 50.0)
    holes = [{"reference": "H1", "x": 110.0, "y": 80.0, "footprint": HOLE, "keepout_radius_mm": 3.0}]
    _, result = _apply(board, tmp_path, RECT, holes, origin="page")
    assert _fp_at(Path(result["output_pcb"]), "H1") == (110.0, 80.0)
    assert result["origin"]["frame"] == "page"


def test_aux_frame_without_aux_origin_is_refused(board: Path, tmp_path: Path) -> None:
    p = board_outline.propose_board_outline(board, RECT, origin="aux_axis", output_dir=tmp_path / "o")
    assert p["status"] == "refused" and "aux_axis_origin" in p["reason"]


# --- checkpoint behaviour ---------------------------------------------------------


def test_invalid_outline_dimensions_refused(board: Path, tmp_path: Path) -> None:
    for bad in (
        {**RECT, "width": 0},
        {**RECT, "height": -5},
        {**RECT, "corner_radius": 40.0},  # more than half the short side
        {**RECT, "corner_radius": -1},
        {"x": 1, "y": 1, "width": 10},
    ):
        p = board_outline.propose_board_outline(board, bad, output_dir=tmp_path / "o")
        assert p["status"] == "refused", bad


def test_token_single_use_and_source_drift_rejected(board: Path, tmp_path: Path) -> None:
    proposal = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out")
    first = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert first["status"] == "built"
    again = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert again["status"] == "error"

    other = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out2")
    faults.drift(board)  # drift (whitespace only)
    drifted = board_outline.confirm_board_outline(board, other["revision_token"], drc=_no_outline_drc)
    assert drifted["status"] == "error"
    assert not (tmp_path / "out2").exists()


def test_failure_leaves_nothing_behind_and_token_stays_usable(board: Path, tmp_path: Path, monkeypatch) -> None:
    proposal = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out")
    original = write_layer.save_pcb

    def boom(pcb, path=None):
        raise OSError("disk full")

    monkeypatch.setattr(write_layer, "save_pcb", boom)
    failed = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert failed["status"] == "incomplete"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]

    monkeypatch.setattr(write_layer, "save_pcb", original)
    ok = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert ok["status"] == "built"


def test_output_dir_inside_source_refused(board: Path) -> None:
    p = board_outline.propose_board_outline(board, RECT, output_dir=board.parent / "sub")
    assert p["status"] == "error"


def test_other_content_preserved(board: Path, tmp_path: Path) -> None:
    from collections import Counter

    _, result = _apply(board, tmp_path, RECT)
    count = lambda pcb: Counter(c.name for c in _root(pcb).children)  # noqa: E731
    after, before = count(Path(result["output_pcb"])), count(board)
    diff = {k: after[k] - before[k] for k in set(after) | set(before) if after[k] != before[k]}
    assert diff == {}  # 4 lines replaced by 4 lines
    assert after["footprint"] == before["footprint"]


# --- review round: cutouts, pill, overlaps, parity, fail-closed, keepout DRC ---------------


def _add_slot(board: Path) -> None:
    """An internal 5x2 mm slot as four Edge.Cuts gr_lines, added through kicad_tools nodes."""
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    pcb = PCB.load(board)
    pts = [(130, 100), (135, 100), (135, 102), (130, 102)]
    for i in range(4):
        a, b = pts[i], pts[(i + 1) % 4]
        pcb._sexp.append(
            SExp.list(
                "gr_line",
                SExp.list("start", *a),
                SExp.list("end", *b),
                SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default")),
                SExp.list("layer", "Edge.Cuts"),
                SExp.list("uuid", f"11111111-1111-4111-8111-11111111111{i}"),
            )
        )
    write_layer.save_pcb(pcb, board)


def test_internal_cutout_is_refused_by_name_not_silently_deleted(board: Path, tmp_path: Path) -> None:
    _add_slot(board)
    p = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out")
    assert p["status"] == "refused" and p["revision_token"] is None
    assert len(p["cutouts"]) == 4
    assert {c["uuid"] for c in p["cutouts"]} == {f"11111111-1111-4111-8111-11111111111{i}" for i in range(4)}
    assert "cutout" in p["reason"]
    assert not (tmp_path / "out").exists()


def test_corner_radius_half_short_side_builds_a_stadium(board: Path, tmp_path: Path) -> None:
    _, result = _apply(board, tmp_path, {**RECT, "corner_radius": 35.0}, drc=None)  # real kicad-cli DRC
    assert result["status"] == "built", result
    assert result["outline_drc"]["status"] == "clean"
    edge = _edge(Path(result["output_pcb"]))
    assert sorted(n.name for n in edge) == ["gr_arc"] * 4 + ["gr_line"] * 2  # no zero-length lines
    assert _closed_chain(edge)


def test_corner_radius_just_below_half_short_side_builds(board: Path, tmp_path: Path) -> None:
    _, result = _apply(board, tmp_path, {**RECT, "corner_radius": 34.9})
    assert result["status"] == "built", result
    assert len(_edge(Path(result["output_pcb"]))) == 8


def test_hole_over_footprints_is_refused_with_items_named_unless_allowed(board: Path, tmp_path: Path) -> None:
    # (150, 105) on the golden board sits on existing parts (reviewer repro).
    holes = [{"reference": "H1", "x": 150.0, "y": 105.0, "footprint": HOLE, "keepout_radius_mm": 3.0}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert p["status"] == "refused" and p["revision_token"] is None
    assert p["overlaps"], p
    assert all(o["hole"] == "H1" for o in p["overlaps"])
    assert all(o["kind"] in {"footprint", "track", "via", "zone", "mounting_hole"} for o in p["overlaps"])
    assert any(o["kind"] == "footprint" and o["item"] for o in p["overlaps"])
    assert "H1" in p["reason"]

    ok = board_outline.propose_board_outline(
        board, RECT, mounting_holes=holes, output_dir=tmp_path / "out", allow_overlap=True
    )
    assert ok["status"] == "proposed" and ok["overlaps"] == p["overlaps"]
    built = board_outline.confirm_board_outline(board, ok["revision_token"], drc=_no_outline_drc)
    assert built["status"] == "built" and built["overlaps"] == p["overlaps"]


def test_hole_overlap_with_track_and_via_is_found(board: Path, tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    pcb = PCB.load(board)
    seg = SExp.list(
        "segment", SExp.list("start", 111.0, 80.0), SExp.list("end", 119.0, 80.0), SExp.list("width", 0.25),
        SExp.list("layer", "F.Cu"), SExp.list("net", 0), SExp.list("uuid", "22222222-2222-4222-8222-222222222222"),
    )
    via = SExp.list(
        "via", SExp.list("at", 190.0, 130.0), SExp.list("size", 0.8), SExp.list("drill", 0.4),
        SExp.list("layers", "F.Cu", "B.Cu"), SExp.list("net", 0), SExp.list("uuid", "33333333-3333-4333-8333-333333333333"),
    )
    pcb._sexp.append(seg)
    pcb._sexp.append(via)
    write_layer.save_pcb(pcb, board)
    holes = [
        {"reference": "H1", "x": 115.0, "y": 82.0, "footprint": HOLE, "keepout_radius_mm": 3.0},  # track 2 mm away
        {"reference": "H2", "x": 190.0, "y": 127.0, "footprint": HOLE, "keepout_radius_mm": 3.0},  # via 3 mm away
        {"reference": "H3", "x": 105.0, "y": 135.0, "footprint": HOLE, "keepout_radius_mm": 3.0},  # clear
    ]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert p["status"] == "refused"
    kinds = {(o["hole"], o["kind"]) for o in p["overlaps"]}
    assert ("H1", "track") in kinds and ("H2", "via") in kinds
    assert not any(o["hole"] == "H3" for o in p["overlaps"])


def test_two_holes_overlapping_each_other_are_refused(board: Path, tmp_path: Path) -> None:
    holes = [
        {"reference": "H1", "x": 105.0, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 3.0},
        {"reference": "H2", "x": 108.0, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 3.0},
    ]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert p["status"] == "refused" and any(o["kind"] == "mounting_hole" for o in p["overlaps"])


HOLE_OK = [{"reference": "H1", "x": 105.0, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 3.0}]


def test_mounting_hole_is_board_only_so_schematic_parity_has_no_extra_footprint(board: Path, tmp_path: Path) -> None:
    from collections import Counter

    from kicad_mcp.kicad_cli import run_drc

    src = Counter(v["type"] for v in run_drc(board, schematic_parity=True).get("schematic_parity", []))
    _, result = _apply(board, tmp_path, RECT, HOLE_OK)
    out = Path(result["output_pcb"])
    h1 = [
        f for f in _root(out).children
        if f.name == "footprint"
        and any(p.name == "property" and p.values[0] == "Reference" and p.values[1] == "H1" for p in f.children)
    ][0]
    assert "board_only" in [str(v) for v in h1.find("attr").values]
    report = run_drc(out, schematic_parity=True)
    after = Counter(v["type"] for v in report.get("schematic_parity", []))
    assert after.get("extra_footprint", 0) == src.get("extra_footprint", 0), report.get("schematic_parity")


def test_outline_drc_that_cannot_run_fails_closed(board: Path, tmp_path: Path) -> None:
    def broken(path, **_kw):
        raise RuntimeError("kicad-cli not found")

    proposal = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out")
    result = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=broken)
    assert result["status"] == "incomplete" and "kicad-cli" in result["reason"]
    assert not (tmp_path / "out").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]
    ok = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert ok["status"] == "built"  # token still usable


def test_real_drc_flags_a_track_through_the_keepout(board: Path, tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    from kicad_mcp.kicad_cli import run_drc

    _, result = _apply(board, tmp_path, RECT, HOLE_OK)
    out = Path(result["output_pcb"])
    pcb = PCB.load(out)
    pcb._sexp.append(
        SExp.list(
            "segment", SExp.list("start", 102.0, 75.0), SExp.list("end", 108.0, 75.0), SExp.list("width", 0.25),
            SExp.list("layer", "F.Cu"), SExp.list("net", 0), SExp.list("uuid", "44444444-4444-4444-8444-444444444444"),
        )
    )
    write_layer.save_pcb(pcb, out)
    kinds = {v["type"] for v in run_drc(out)["violations"]}
    assert "items_not_allowed" in kinds


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), True, "10"])
def test_non_finite_bool_and_string_numbers_are_refused(board: Path, tmp_path: Path, bad) -> None:
    p = board_outline.propose_board_outline(board, {**RECT, "width": bad}, output_dir=tmp_path / "o")
    assert p["status"] == "refused"
    p = board_outline.propose_board_outline(board, {**RECT, "corner_radius": bad}, output_dir=tmp_path / "o")
    assert p["status"] == "refused"
    holes = [{"reference": "H1", "x": bad, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 3.0}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "o")
    assert p["status"] == "refused"


def test_proposal_is_a_snapshot_of_the_arguments(board: Path, tmp_path: Path) -> None:
    outline = dict(RECT)
    holes = [dict(h) for h in HOLE_OK]
    proposal = board_outline.propose_board_outline(board, outline, mounting_holes=holes, output_dir=tmp_path / "out")
    outline["width"] = 1.0  # caller mutates its objects after proposing
    holes[0]["x"] = 5000.0
    result = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert result["status"] == "built"
    assert _fp_at(Path(result["output_pcb"]), "H1") == (105.0, 75.0)


def test_replan_mismatch_at_confirm_is_an_error_and_token_kept(board: Path, tmp_path: Path, monkeypatch) -> None:
    proposal = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out")
    real = board_outline._plan

    def drifted(*a, **k):
        plan, refusal = real(*a, **k)
        return {**plan, "corner_radius": 1.0}, refusal

    monkeypatch.setattr(board_outline, "_plan", drifted)
    r = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert r["status"] == "error" and "proposal" in r["reason"]
    monkeypatch.setattr(board_outline, "_plan", real)
    ok = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert ok["status"] == "built"


def test_footprint_library_found_via_any_kicad_env_dir(tmp_path: Path, monkeypatch) -> None:
    import os

    lib = tmp_path / "libs"
    (lib / "Custom.pretty").mkdir(parents=True)
    shutil.copy(board_outline._footprint_file(HOLE), lib / "Custom.pretty" / "MyHole.kicad_mod")
    for name in list(os.environ):
        if name.startswith("KICAD") and name.endswith("_FOOTPRINT_DIR"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("KICAD99_FOOTPRINT_DIR", str(lib))
    assert board_outline._footprint_file("Custom:MyHole") is not None


def test_footprint_library_found_via_project_fp_lib_table_nickname(board: Path, tmp_path: Path) -> None:
    lib = board.parent / "holes_lib.pretty"
    lib.mkdir()
    shutil.copy(board_outline._footprint_file(HOLE), lib / "MyHole.kicad_mod")
    # a plain config file (not a design file), written for the test fixture
    (board.parent / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "Nick") (type "KiCad") '
        '(uri "${KIPRJMOD}/holes_lib.pretty") (options "") (descr "")))\n',
        encoding="utf-8",
    )
    assert board_outline._footprint_file("Nick:MyHole", board.parent) is not None
    holes = [{"reference": "H1", "x": 105.0, "y": 75.0, "footprint": "Nick:MyHole", "keepout_radius_mm": 3.0}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert p["status"] == "proposed", p


def test_earlier_outlined_revision_inside_source_is_not_copied(board: Path, tmp_path: Path) -> None:
    stale = board.parent / "proj_outlined"
    stale.mkdir()
    (stale / "old.txt").write_text("x")
    _, result = _apply(board, tmp_path, RECT)
    assert result["status"] == "built"
    assert not (Path(result["output_pcb"]).parent / "proj_outlined").exists()


# --- delta review: pours, re-apply, outer-loop rule, perf cache, arcs, tables ---------------


def _add_gnd_pour(board: Path) -> None:
    """A B.Cu GND copper pour covering the whole board, added through kicad_tools nodes."""
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    pcb = PCB.load(board)
    pcb._sexp.append(
        parse_string(
            '(zone (net 1) (net_name "GND") (layer "B.Cu") (uuid "55555555-5555-4555-8555-555555555555") '
            '(name "GNDPOUR") (hatch edge 0.5) (connect_pads (clearance 0.2)) (min_thickness 0.2) '
            '(fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5)) '
            '(polygon (pts (xy 90 60) (xy 210 60) (xy 210 150) (xy 90 150))))'
        )
    )
    write_layer.save_pcb(pcb, board)


def test_copper_pour_under_a_hole_is_informational_not_a_refusal(board: Path, tmp_path: Path) -> None:
    _add_gnd_pour(board)
    holes = [{"reference": "H1", "x": 105.0, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 4.0}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert p["status"] == "proposed", p
    assert p["overlaps"] == []
    assert [z["name"] for z in p["zone_overlaps"]] == ["GNDPOUR"] and p["zone_overlaps"][0]["hole"] == "H1"
    result = board_outline.confirm_board_outline(board, p["revision_token"], drc=_no_outline_drc)
    assert result["status"] == "built"
    assert result["zone_overlaps"] == p["zone_overlaps"]
    assert "refill" in result["pour_note"] and "stale" in result["pour_note"]


def test_pour_does_not_silence_a_hard_overlap(board: Path, tmp_path: Path) -> None:
    _add_gnd_pour(board)
    holes = [{"reference": "H1", "x": 150.0, "y": 105.0, "footprint": HOLE, "keepout_radius_mm": 3.0}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert p["status"] == "refused" and p["overlaps"]
    assert all(o["kind"] != "zone" for o in p["overlaps"])


def test_rule_area_zone_under_a_hole_is_a_hard_overlap(board: Path, tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    pcb = PCB.load(board)
    pcb._sexp.append(
        parse_string(
            '(zone (net 0) (net_name "") (layers "F.Cu") (uuid "66666666-6666-4666-8666-666666666666") '
            '(name "RULE") (hatch edge 0.5) (keepout (tracks not_allowed) (vias allowed) (pads allowed) '
            '(copperpour allowed) (footprints allowed)) '
            '(polygon (pts (xy 100 70) (xy 120 70) (xy 120 90) (xy 100 90))))'
        )
    )
    write_layer.save_pcb(pcb, board)
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=HOLE_OK, output_dir=tmp_path / "out")
    assert p["status"] == "refused" and any(o["kind"] == "zone" for o in p["overlaps"])


def test_reapplying_outline_only_to_an_output_revision_works(board: Path, tmp_path: Path) -> None:
    _, first = _apply(board, tmp_path, RECT, HOLE_OK, name="rev1")
    assert first["status"] == "built"
    rev1 = Path(first["output_pcb"])
    p = board_outline.propose_board_outline(
        rev1, {**RECT, "width": 90.0}, output_dir=tmp_path / "rev2"
    )
    assert p["status"] == "proposed", p
    second = board_outline.confirm_board_outline(rev1, p["revision_token"], drc=_no_outline_drc)
    assert second["status"] == "built"
    xs = {pt[0] for n in _edge(Path(second["output_pcb"])) for pt in (_xy(n, "start"), _xy(n, "end"))}
    assert xs == {100.0, 190.0}
    assert _fp_at(Path(second["output_pcb"]), "H1") == (105.0, 75.0)  # the hole stays


def test_redeclaring_a_hole_from_a_previous_run_names_the_earlier_keepout(board: Path, tmp_path: Path) -> None:
    _, first = _apply(board, tmp_path, RECT, HOLE_OK, name="rev1")
    rev1 = Path(first["output_pcb"])
    p = board_outline.propose_board_outline(rev1, RECT, mounting_holes=HOLE_OK, output_dir=tmp_path / "rev2")
    assert p["status"] == "refused"
    assert "KEEPOUT_H1" in p["reason"] and "previous run" in p["reason"]
    assert not p.get("overlaps")


def _add_loop(board: Path, pts, prefix: str) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    pcb = PCB.load(board)
    for i in range(len(pts)):
        a, b = pts[i], pts[(i + 1) % len(pts)]
        pcb._sexp.append(
            SExp.list(
                "gr_line",
                SExp.list("start", *a),
                SExp.list("end", *b),
                SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default")),
                SExp.list("layer", "Edge.Cuts"),
                SExp.list("uuid", f"{prefix}-0000-4000-8000-00000000000{i}"),
            )
        )
    write_layer.save_pcb(pcb, board)


def test_larger_decorative_loop_refusal_states_the_rule(board: Path, tmp_path: Path) -> None:
    _add_loop(board, [(90, 60), (210, 60), (210, 150), (90, 150)], "aaaaaaaa")  # encloses the real outline
    p = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out")
    assert p["status"] == "refused"
    assert "largest by bbox" in p["reason"] and "loops found: 2" in p["reason"]
    assert p["outline_ambiguous"] is False
    assert len(p["cutouts"]) == 4  # the real (inner) outline items, named


def test_disjoint_second_loop_is_reported_as_ambiguous(board: Path, tmp_path: Path) -> None:
    _add_loop(board, [(300, 300), (310, 300), (310, 310), (300, 310)], "bbbbbbbb")
    p = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out")
    assert p["status"] == "refused" and p["outline_ambiguous"] is True
    assert "ambiguous" in p["reason"]


def test_endpoints_straddling_a_rounding_boundary_still_form_one_loop(board: Path, tmp_path: Path) -> None:
    from kicad_tools.schema import PCB

    pcb = PCB.load(board)
    lines = [n for n in pcb._sexp.children if n.name == "gr_line" and n.find("layer").values[0] == "Edge.Cuts"]
    # the corner (173.5, 85) is shared by two lines: put the two endpoints either side of the
    # 0.0005 rounding boundary of a 3-decimal key, 2e-5 mm apart
    for node in lines:
        if abs(float(node.find("end").values[0]) - 173.5) < 1e-9 and abs(float(node.find("end").values[1]) - 85.0) < 1e-9:
            node.find("end").set_value(0, 173.50049)
        if abs(float(node.find("start").values[0]) - 173.5) < 1e-9 and abs(float(node.find("start").values[1]) - 85.0) < 1e-9:
            node.find("start").set_value(0, 173.50051)
    write_layer.save_pcb(pcb, board)
    p = board_outline.propose_board_outline(board, RECT, output_dir=tmp_path / "out")
    assert p["status"] == "proposed", p


def test_footprint_geometry_is_computed_once_per_footprint_and_reused_at_confirm(
    board: Path, tmp_path: Path, monkeypatch
) -> None:
    calls = {"pads": 0, "court": 0}
    real_pads, real_court = board_outline._pad_boxes, board_outline._courtyard_box

    def pads(fp):
        calls["pads"] += 1
        return real_pads(fp)

    def court(fp):
        calls["court"] += 1
        return real_court(fp)

    monkeypatch.setattr(board_outline, "_pad_boxes", pads)
    monkeypatch.setattr(board_outline, "_courtyard_box", court)
    n_fp = sum(1 for n in _root(board).children if n.name == "footprint")
    holes = [
        {"reference": f"H{i}", "x": 105.0 + 10 * i, "y": 75.0, "footprint": HOLE, "keepout_radius_mm": 3.0}
        for i in range(1, 6)
    ]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert p["status"] == "proposed", p
    assert calls["pads"] == n_fp and calls["court"] <= n_fp
    before = dict(calls)
    r = board_outline.confirm_board_outline(board, p["revision_token"], drc=_no_outline_drc)
    assert r["status"] == "built"
    assert calls == before  # reused


def test_large_sagitta_arc_track_overlap_is_found(board: Path, tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    pcb = PCB.load(board)
    pcb._sexp.append(
        SExp.list(
            "arc", SExp.list("start", 110.0, 100.0), SExp.list("mid", 120.0, 80.0), SExp.list("end", 130.0, 100.0),
            SExp.list("width", 0.25), SExp.list("layer", "F.Cu"), SExp.list("net", 0),
            SExp.list("uuid", "77777777-7777-4777-8777-777777777777"),
        )
    )
    write_layer.save_pcb(pcb, board)
    # circle through the 3 points: centre (120, 92.5), radius 12.5; this hole is 2.5 mm inside the
    # arc at 225 degrees, but 3.9 mm from either chord.
    holes = [{"reference": "H1", "x": 112.929, "y": 85.429, "footprint": HOLE, "keepout_radius_mm": 3.0}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert p["status"] == "refused" and any(o["kind"] == "track" for o in p["overlaps"]), p


def test_nickname_in_first_table_is_authoritative_no_fall_through(
    board: Path, tmp_path: Path, monkeypatch
) -> None:
    empty = board.parent / "empty.pretty"
    empty.mkdir()
    (board.parent / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "MountingHole") (type "KiCad") '
        '(uri "${KIPRJMOD}/empty.pretty") (options "") (descr "")))\n',
        encoding="utf-8",
    )
    assert board_outline._footprint_file(HOLE, board.parent) is None  # not found in the table's dir
    assert board_outline._footprint_file(HOLE) is not None  # without that table it resolves


def test_footprint_name_part_cannot_escape_the_library(board: Path) -> None:
    assert board_outline._footprint_file("MountingHole:../MountingHole.pretty/MountingHole_3.2mm_M3") is None
    assert board_outline._footprint_file("MountingHole:sub/MountingHole_3.2mm_M3") is None
    assert board_outline._footprint_file("..:MountingHole_3.2mm_M3") is None


def test_hole_overlapping_only_a_courtyard_is_found(board: Path, tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    pcb = PCB.load(board)
    pcb._sexp.append(
        parse_string(
            '(footprint "Test:Big" (layer "F.Cu") (uuid "88888888-8888-4888-8888-888888888888") (at 110 130) '
            '(property "Reference" "TP9" (at 0 0) (layer "F.SilkS")) '
            '(fp_rect (start -5 -5) (end 5 5) (stroke (width 0.05) (type default)) (fill none) (layer "F.CrtYd")) '
            '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))'
        )
    )
    write_layer.save_pcb(pcb, board)
    # 4 mm from the only pad (clear of it), inside the courtyard rectangle
    holes = [{"reference": "H1", "x": 114.0, "y": 130.0, "footprint": HOLE, "keepout_radius_mm": 1.7}]
    p = board_outline.propose_board_outline(board, RECT, mounting_holes=holes, output_dir=tmp_path / "out")
    assert p["status"] == "refused", p
    hit = [o for o in p["overlaps"] if o["item"] == "TP9"]
    assert hit and hit[0]["via"] == "courtyard"


def test_footprint_without_attr_gets_board_only(board: Path, tmp_path: Path) -> None:
    root = write_layer.load(board_outline._footprint_file(HOLE))
    attr = root.find("attr")
    assert attr is not None
    root.children[:] = [c for c in root.children if c is not attr]
    mod = tmp_path / "NoAttr.kicad_mod"
    write_layer.save(root, mod)  # the variant goes through the layer (build ticket 74)
    holes = [{"reference": "H1", "x": 105.0, "y": 75.0, "footprint": str(mod), "keepout_radius_mm": 3.0}]
    _, result = _apply(board, tmp_path, RECT, holes)
    assert result["status"] == "built", result
    h1 = [
        f for f in _root(Path(result["output_pcb"])).children
        if f.name == "footprint"
        and any(p.name == "property" and p.values[0] == "Reference" and p.values[1] == "H1" for p in f.children)
    ][0]
    assert h1.find("attr") is not None and "board_only" in [str(v) for v in h1.find("attr").values]


# --- the reload self-check fires on a corrupted write (build ticket 31) ----------------------
# Each case lets the real save run, then changes ONE compared value in the written temporary
# revision through kicad_tools' Document API (tests/_self_check_helpers.py). The self-check must
# refuse it by name, leave nothing behind and keep the token; with the corruption disarmed the
# same token must build, which proves the failure came from the corruption alone.

from tests._self_check_helpers import (  # noqa: E402
    corrupt_pcb_saves,
    footprint_reference,
    in_revision_tmp,
    on_layer,
    polygon_points,
    shift_xy,
    top_nodes,
)


def _edge_nodes(root):
    return [n for n in root.children if n.name in ("gr_line", "gr_arc") and on_layer("Edge.Cuts")(n)]


def _drop_first_edge_line(root):
    root.remove(next(n for n in _edge_nodes(root) if n.name == "gr_line"))


def _break_the_chain(root):
    shift_xy(next(n for n in _edge_nodes(root) if n.name == "gr_line"), "start", dy=0.5)


def _translate_the_outline(root):
    for node in _edge_nodes(root):
        for tag in ("start", "mid", "end"):
            if node.find(tag) is not None:
                shift_xy(node, tag, dx=1.0)


def _move_hole(root):
    shift_xy(next(f for f in top_nodes(root, "footprint") if footprint_reference(f) == "H1"), "at", dx=0.5)


def _strip_keepout_flag(root):
    zone = next(z for z in top_nodes(root, "zone") if z.find("keepout") is not None)
    zone.remove(zone.find("keepout"))


def _drop_an_unrelated_footprint(root):
    root.remove(next(f for f in top_nodes(root, "footprint") if footprint_reference(f) != "H1"))


@pytest.mark.parametrize(
    ("corrupt", "named"),
    [
        pytest.param(_drop_first_edge_line, "Edge.Cuts after save+reload", id="O1-edge-item-dropped"),
        pytest.param(_break_the_chain, "Edge.Cuts after save+reload", id="O1-chain-broken"),
        pytest.param(_translate_the_outline, "outline extent after reload", id="O2-extent"),
        pytest.param(_move_hole, "raw (at ...) of H1", id="O3-hole-position"),
        pytest.param(_strip_keepout_flag, "keepout zones missing", id="O4-keepout-count"),
        pytest.param(_drop_an_unrelated_footprint, "top-level node counts", id="O5-node-counts"),
    ],
)
def test_reload_self_check_fires_on_a_corrupted_write(board: Path, tmp_path: Path, monkeypatch, corrupt, named) -> None:
    before = _tree_hash(board.parent)
    proposal = board_outline.propose_board_outline(board, RECT, mounting_holes=HOLE_OK, output_dir=tmp_path / "out")
    assert proposal["status"] == "proposed", proposal
    token = proposal["revision_token"]
    saves = corrupt_pcb_saves(monkeypatch, corrupt, when=in_revision_tmp(tmp_path / "out"))

    failed = board_outline.confirm_board_outline(board, token, drc=_no_outline_drc)

    assert len(saves.hits) == 1  # the corruption really landed in the temporary revision
    assert failed["status"] == "error", failed
    assert failed["reason"].startswith("CRITICAL:") and named in failed["reason"], failed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]  # no revision, no temporary left
    assert _tree_hash(board.parent) == before
    saves.disarm()
    ok = board_outline.confirm_board_outline(board, token, drc=_no_outline_drc)
    assert ok["status"] == "built", ok  # same token: the corruption alone caused the failure
    assert _fp_at(Path(ok["output_pcb"]), "H1") == (105.0, 75.0)


def _bend_an_arc(root):
    shift_xy(next(n for n in _edge_nodes(root) if n.name == "gr_arc"), "mid", dx=0.5, dy=0.5)


def _move_the_keepout(root):
    zone = next(z for z in top_nodes(root, "zone") if z.find("keepout") is not None)
    for xy in polygon_points(zone):
        xy.set_value(0, float(xy.values[0]) + 2.0)


def _nudge_a_keepout_vertex(root):
    """2 um: just over the 1 um the check allows for kicad_tools' 6-significant-digit numbers."""
    zone = next(z for z in top_nodes(root, "zone") if z.find("keepout") is not None)
    xy = polygon_points(zone)[0]
    xy.set_value(0, float(xy.values[0]) + 0.002)


def _flip_the_hole_to_the_back(root):
    hole = next(f for f in top_nodes(root, "footprint") if footprint_reference(f) == "H1")
    hole.find("layer").set_value(0, "B.Cu")


@pytest.mark.parametrize(
    ("corrupt", "named"),
    [
        pytest.param(_bend_an_arc, "gr_arc", id="arc-mid"),
        pytest.param(_move_the_keepout, "KEEPOUT_H1", id="keepout-polygon"),
        pytest.param(_nudge_a_keepout_vertex, "KEEPOUT_H1", id="keepout-vertex-2um"),
        pytest.param(_flip_the_hole_to_the_back, "H1", id="hole-layer"),
    ],
)
def test_reload_self_check_compares_the_written_nodes_not_only_their_summary(
    board: Path, tmp_path: Path, monkeypatch, corrupt, named
) -> None:
    """Corruptions the summary checks (O1-O5) cannot see: the tool's own nodes must reload as written."""
    hole = [{**HOLE_OK[0], "x": 110.0, "y": 80.0}]  # clear of the rounded corner
    proposal = board_outline.propose_board_outline(
        board, {**RECT, "corner_radius": 5.0}, mounting_holes=hole, output_dir=tmp_path / "out"
    )
    assert proposal["status"] == "proposed", proposal
    token = proposal["revision_token"]
    saves = corrupt_pcb_saves(monkeypatch, corrupt, when=in_revision_tmp(tmp_path / "out"))

    failed = board_outline.confirm_board_outline(board, token, drc=_no_outline_drc)

    assert len(saves.hits) == 1
    assert failed["status"] == "error", failed
    assert failed["reason"].startswith("CRITICAL:") and named in failed["reason"], failed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]
    saves.disarm()
    assert board_outline.confirm_board_outline(board, token, drc=_no_outline_drc)["status"] == "built"


# --- review round (ticket 31): the whole file, exact text, numbers as written -------------------

_NOT_INTENDED = "CRITICAL: the reloaded revision is not the intended write"


def _move_c1(root):
    shift_xy(next(f for f in top_nodes(root, "footprint") if footprint_reference(f) == "C1"), "at", dx=5.0)


def _thicken_the_board(root):
    root.find("general").find("thickness").set_value(0, 2.0)


def _duplicate_the_hole_and_drop_u1(root):
    from kicad_tools.sexp import parse_string

    hole = next(f for f in top_nodes(root, "footprint") if footprint_reference(f) == "H1")
    root.remove(next(f for f in top_nodes(root, "footprint") if footprint_reference(f) == "U1"))
    root.append(parse_string(hole.to_string()))


def _first_arc(root):
    return next(n for n in _edge_nodes(root) if n.name == "gr_arc")


def _reissue_the_keepout_uuid(root):
    zone = next(z for z in top_nodes(root, "zone") if z.find("keepout") is not None)
    zone.find("uuid").set_value(0, "00000000-0000-4000-8000-00000000beef")


def _append_a_child_to_an_arc(root):
    from kicad_tools.sexp import SExp

    _first_arc(root).append(SExp.list("locked", "yes"))


def _rename_a_child_of_an_arc(root):
    _first_arc(root).find("stroke").name = "strokes"


def _replace_a_list_by_an_atom(root):
    from kicad_tools.sexp import SExp

    arc = _first_arc(root)
    stroke = arc.find("stroke")
    index = arc.children.index(stroke)
    arc.remove(stroke)
    arc.insert(index, SExp.atom("stroke"))


@pytest.mark.parametrize(
    ("corrupt", "named"),
    [
        pytest.param(_move_c1, "footprint:C1", id="untouched-footprint-moved"),
        pytest.param(_thicken_the_board, "(general", id="board-thickness"),
        pytest.param(_duplicate_the_hole_and_drop_u1, "footprint:U1", id="duplicate-written-drop-original"),
        pytest.param(_reissue_the_keepout_uuid, "zone:KEEPOUT_H1", id="written-uuid-reissued"),
        pytest.param(_append_a_child_to_an_arc, "gr_arc:", id="written-child-appended"),
        pytest.param(_rename_a_child_of_an_arc, "gr_arc:", id="written-child-renamed"),
        pytest.param(_replace_a_list_by_an_atom, "gr_arc:", id="written-list-becomes-atom"),
    ],
)
def test_reload_self_check_compares_the_whole_file_exactly(board: Path, tmp_path: Path, monkeypatch, corrupt, named) -> None:
    """Nodes the step did not author, and every shape of change to one it did, must reload as saved."""
    hole = [{**HOLE_OK[0], "x": 110.0, "y": 80.0}]
    proposal = board_outline.propose_board_outline(
        board, {**RECT, "corner_radius": 5.0}, mounting_holes=hole, output_dir=tmp_path / "out"
    )
    assert proposal["status"] == "proposed", proposal
    saves = corrupt_pcb_saves(monkeypatch, corrupt, when=in_revision_tmp(tmp_path / "out"))

    failed = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)

    assert len(saves.hits) == 1
    assert failed["status"] == "error", failed
    assert failed["reason"].startswith("CRITICAL:") and named in failed["reason"], failed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]
    saves.disarm()
    assert board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)["status"] == "built"


@pytest.mark.parametrize("x", [1000.0, 1500.0, -1100.0])
def test_a_correct_write_far_from_the_origin_passes_the_self_check(board: Path, tmp_path: Path, x: float) -> None:
    """New numbers are written as %.6g (1048.535534 as 1048.54): a correct write must still verify."""
    hole = [{**HOLE_OK[0], "x": x + 10.0, "y": 80.0}]
    proposal = board_outline.propose_board_outline(
        board, {**RECT, "x": x, "corner_radius": 5.0}, mounting_holes=hole, output_dir=tmp_path / "out"
    )
    assert proposal["status"] == "proposed", proposal
    result = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert result["status"] == "built", result
    assert _fp_at(Path(result["output_pcb"]), "H1") == (x + 10.0, 80.0)


def test_declared_values_are_written_with_full_precision(board: Path, tmp_path: Path) -> None:
    """The write layer writes 103.0125 and 110.01234 as declared (kicad_tools' own writer rounded
    them to 103.013 and 110.012 before build ticket 42), and the self-check compares them so."""
    hole = [{**HOLE_OK[0], "x": 110.01234, "y": 80.0}]
    proposal = board_outline.propose_board_outline(
        board, {**RECT, "x": 103.0125}, mounting_holes=hole, output_dir=tmp_path / "out"
    )
    assert proposal["status"] == "proposed", proposal
    result = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert result["status"] == "built", result
    out = Path(result["output_pcb"])
    assert _fp_at(out, "H1") == (110.01234, 80.0)
    assert min(p[0] for n in _edge(out) for p in (_xy(n, "start"), _xy(n, "end"))) == 103.0125


# --- review round 2 (ticket 31): the tolerated net table must still reload as saved ---------------


def _gnd_net(root):
    return next(n for n in top_nodes(root, "net") if len(n.values) > 1 and str(n.values[1]) == "GND")


def _rename_the_added_gnd_net(root):
    _gnd_net(root).set_value(1, "GNDX")


def _renumber_the_added_gnd_net(root):
    _gnd_net(root).set_value(0, 99)


def _drop_the_added_gnd_net(root):
    root.remove(_gnd_net(root))


@pytest.mark.parametrize(
    ("corrupt", "named"),
    [
        # count-neutral: only the reload-versus-saved half of the write check can see these
        pytest.param(_rename_the_added_gnd_net, 'present but not intended: (net 2 "GNDX")', id="added-net-renamed"),
        pytest.param(_renumber_the_added_gnd_net, 'present but not intended: (net 99 "GND")', id="added-net-renumbered"),
        # a dropped entry already changes the node counts
        pytest.param(_drop_the_added_gnd_net, "CRITICAL: top-level node counts changed", id="added-net-dropped"),
    ],
)
def test_on_a_kicad10_board_the_added_net_table_must_reload_as_saved(
    board: Path, tmp_path: Path, monkeypatch, corrupt, named
) -> None:
    """PCB.load adds a net table to a board without one; it is tolerated against the source, so
    only the reload-versus-saved half of the check can catch a corrupted entry."""
    from tests.test_pour_step import to_kicad10_nets

    to_kicad10_nets(board)
    assert top_nodes(_root(board), "net") == []  # the premise: no net table in the source
    proposal = board_outline.propose_board_outline(board, RECT, mounting_holes=HOLE_OK, output_dir=tmp_path / "out")
    assert proposal["status"] == "proposed", proposal
    saves = corrupt_pcb_saves(monkeypatch, corrupt, when=in_revision_tmp(tmp_path / "out"))

    failed = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)

    assert len(saves.hits) == 1
    assert failed["status"] == "error", failed
    assert failed["reason"].startswith("CRITICAL:") and named in failed["reason"], failed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]
    saves.disarm()
    ok = board_outline.confirm_board_outline(board, proposal["revision_token"], drc=_no_outline_drc)
    assert ok["status"] == "built", ok
    # 22 tolerated entries, bounded to the first 20 (decision 9: {count, items})
    tolerated = ok["write_check"]["tolerated"]
    assert tolerated["count"] == 22 and len(tolerated["items"]) == 20, tolerated
    assert ok["write_check"]["missing"] == {"count": 0, "items": []}
