"""Geometry readers on raw tokens (``kicad_mcp.raw_geometry``, build ticket 43), proven against
KiCad itself on KiCad-written demo boards (docs/TESTING.md layer 3).

The oracle for every assertion is KiCad, run in the same session on a copy of the demo board:
``kicad-cli pcb export pos`` and ``ipcd356`` for footprint and pad positions, and KiCad's own
``pcbnew`` module (KiCad's bundled Python) for pad angles and sizes, zone outlines and
Edge.Cuts shapes (``tests/_kicad_oracle.py``). Tolerance 0.01 mm (the ticket's), angles
0.001 degrees.
"""
from __future__ import annotations

import math
from collections import Counter

import pytest

from kicad_mcp import raw_geometry
from tests import _kicad_oracle as oracle

TOL_MM = 0.01
TOL_DEG = 1e-3
PADSTACK_BOARD = "padstack"  # tests/_pcbnew_fixture.py on a copy of video


@pytest.fixture(scope="session")
def kicad_python():
    return oracle.kicad_python()


@pytest.fixture(scope="session")
def demo_board(kicad_demos_dir, kicad_python, tmp_path_factory):
    """``demo_board("vme-wren")``: a session-cached ``BoardOracle`` over a scratch copy."""
    cache: dict[str, oracle.BoardOracle] = {}

    def get(name: str) -> oracle.BoardOracle:
        if name not in cache:
            scratch = tmp_path_factory.mktemp(f"oracle-{name}")
            made: dict = {}
            if name == PADSTACK_BOARD:  # video, given padstacks and zone holes by pcbnew
                source = oracle.copy_board(kicad_demos_dir, oracle.ALL_BOARDS["video"], scratch / "source")
                board = scratch / "padstack.kicad_pcb"
                made = oracle.pcbnew_fixture(kicad_python, source, board, scratch)
                made["user_name"] = _name_a_pads_copper_by_its_user_name(board)
            else:
                board = oracle.copy_board(kicad_demos_dir, oracle.ALL_BOARDS[name], scratch)
            cache[name] = oracle.BoardOracle(name, board, scratch, kicad_python, made)
        return cache[name]

    return get


def _name_a_pads_copper_by_its_user_name(board):
    """In the first front SMD pad whose ``(layers ...)`` starts with "F.Cu", put the board's
    user name for F.Cu ("top_copper" on video) in its place, through the low-level tree and
    ``write_layer.save`` (never as text). KiCad 10.0.7 reads that token as the Rescue layer,
    not as F.Cu (delta review 2); returns the pad's uuid."""
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    def kids(node, name):
        return [c for c in node.children if not c.is_atom and c.name == name]

    root = wl.load(board)
    (table,) = kids(root, "layers")
    user = next(str(r.values[2]) for r in table.children
                if not r.is_atom and r.values and r.values[0] == "F.Cu" and len(r.values) > 2)
    for fp in kids(root, "footprint"):
        for pad in kids(fp, "pad"):
            atoms = [str(c.value) for c in pad.children if c.is_atom]
            (layers,) = kids(pad, "layers")
            names = [str(v) for v in layers.values]
            if len(atoms) > 1 and atoms[1] == "smd" and names[:1] == ["F.Cu"]:
                pad.insert_before("layers", SExp.list("layers", *(wl.string(n) for n in [user, *names[1:]])))
                pad.remove(layers)
                wl.save(root, board)
                (uuid,) = kids(pad, "uuid")
                return str(uuid.values[0])
    raise AssertionError(f"{board}: no front SMD pad on F.Cu")


def _unique_by_reference(footprints):
    counts = Counter(fp.reference for fp in footprints)
    return {fp.reference: fp for fp in footprints if fp.reference and counts[fp.reference] == 1}


@pytest.mark.parametrize("name", sorted(oracle.POSITION_BOARDS))
def test_footprint_position_and_angle_match_the_kicad_cli_position_file(demo_board, name):
    board = demo_board(name)
    footprints = raw_geometry.read_footprints(board.root())
    by_ref = _unique_by_reference(footprints)
    rows = board.positions()
    compared = 0
    misses = []
    for row in rows:
        fp = by_ref.get(row["Ref"])
        if fp is None:
            continue
        compared += 1
        x, y = float(row["PosX"]), -float(row["PosY"])  # the position file's y axis points up
        off = math.dist((x, y), fp.at_mm)
        turn = oracle.angle_delta(float(row["Rot"]), fp.angle_deg)
        side = "back" if row["Side"] == "bottom" else "front"
        if off > TOL_MM or turn > TOL_DEG or side != fp.side:
            misses.append((row["Ref"], (x, y), fp.at_mm, row["Rot"], fp.angle_deg, side, fp.side))
    assert not misses, f"{len(misses)} of {compared} footprints differ from kicad-cli: {misses[:10]}"
    # A row left out is only one whose reference is empty or shared by several footprints.
    counts = Counter(fp.reference for fp in footprints)
    skipped = [row["Ref"] for row in rows if row["Ref"] not in by_ref]
    assert all(not ref or counts[ref] > 1 for ref in skipped), skipped
    assert compared > 0


@pytest.mark.parametrize("name", sorted(oracle.POSITION_BOARDS))
def test_origins_match_pcbnew_and_the_drill_origin_position_file(demo_board, name):
    board = demo_board(name)
    origins = raw_geometry.read_origins(board.root())
    kicad = board.pcbnew()
    assert origins.aux_axis_mm == pytest.approx(tuple(map(oracle.nm_to_mm, kicad["aux_origin"])), abs=1e-6)
    assert origins.grid_mm == pytest.approx(tuple(map(oracle.nm_to_mm, kicad["grid_origin"])), abs=1e-6)
    # kicad-cli's drill-origin position file is the page position minus the aux origin (y up).
    by_ref = _unique_by_reference(raw_geometry.read_footprints(board.root()))
    ax, ay = origins.aux_axis_mm
    misses = []
    for row in board.positions(drill_origin=True):
        fp = by_ref.get(row["Ref"])
        if fp is None:
            continue
        expected = (fp.at_mm[0] - ax, -(fp.at_mm[1] - ay))
        if math.dist(expected, (float(row["PosX"]), float(row["PosY"]))) > TOL_MM:
            misses.append((row["Ref"], expected, row["PosX"], row["PosY"]))
    assert not misses, misses[:10]


def test_the_position_boards_cover_a_non_zero_aux_origin(demo_board):
    with_aux = [n for n in sorted(oracle.POSITION_BOARDS)
                if raw_geometry.read_origins(demo_board(n).root()).aux_axis_mm != (0.0, 0.0)]
    assert {"pic_programmer", "vme-wren"} <= set(with_aux), with_aux


@pytest.mark.parametrize("name", sorted(oracle.POSITION_BOARDS))
def test_pad_positions_match_the_kicad_cli_ipc_d_356_export(demo_board, name):
    board = demo_board(name)
    ax, ay = raw_geometry.read_origins(board.root()).aux_axis_mm
    index: dict[tuple[str, str], list] = {}
    for fp in raw_geometry.read_footprints(board.root()):
        for pad in fp.pads:
            index.setdefault((fp.reference[:6], pad.number[:4]), []).append(pad)
    records = board.ipc_pads()
    misses = []
    for rec in records:
        expected = (rec.x_mm + ax, ay - rec.y_mm)  # IPC-D-356: y up, relative to the aux origin
        candidates = index.get((rec.reference, rec.pin), [])
        best = min((math.dist(expected, p.at_mm) for p in candidates), default=math.inf)
        if best > TOL_MM:
            misses.append((rec, expected, best))
    assert records
    assert not misses, f"{len(misses)} of {len(records)} IPC-D-356 pads unmatched: {misses[:10]}"


PAD_BOARDS = sorted({*oracle.POSITION_BOARDS, PADSTACK_BOARD})


@pytest.mark.parametrize("name", PAD_BOARDS)
def test_pad_position_angle_and_size_match_pcbnew(demo_board, name):
    board = demo_board(name)
    kicad = {fp["uuid"]: fp for fp in board.pcbnew()["footprints"]}
    footprints = raw_geometry.read_footprints(board.root())
    assert len(footprints) == len(kicad)
    misses, compared = [], 0
    for fp in footprints:
        theirs = kicad[fp.uuid]
        assert fp.side == ("back" if theirs["back"] else "front"), fp.reference
        assert len(fp.pads) == len(theirs["pads"]), fp.reference
        for pad, ref in zip(fp.pads, theirs["pads"], strict=True):
            compared += 1
            at = tuple(map(oracle.nm_to_mm, ref["at"]))
            size = tuple(map(oracle.nm_to_mm, ref["size"]))
            # KiCad clears the number of a pad on no copper layer (the padstack board's pad whose
            # F.Cu is given by its user name); the reader reports the number as stored.
            kicad_cleared = ref["number"] == "" and not any(c[3] for c in ref["copper"])
            if (
                (pad.number != ref["number"] and not kicad_cleared)
                or math.dist(at, pad.at_mm) > TOL_MM
                or oracle.angle_delta(ref["angle"], pad.angle_deg) > TOL_DEG
                or oracle.angle_delta(ref["fp_relative_angle"], pad.local_angle_deg) > TOL_DEG
                or max(abs(a - b) for a, b in zip(size, pad.size_mm, strict=True)) > TOL_MM
            ):
                misses.append((fp.reference, pad, ref))
    assert compared
    assert not misses, f"{len(misses)} of {compared} pads differ from pcbnew: {misses[:5]}"


def test_the_position_boards_cover_rotated_and_back_side_footprints(demo_board):
    angles, back_pads, rotated_pads = set(), 0, 0
    for name in sorted(oracle.POSITION_BOARDS):
        for fp in raw_geometry.read_footprints(demo_board(name).root()):
            angles.add(fp.angle_deg % 360)
            if fp.side == "back":
                back_pads += len(fp.pads)
            if fp.angle_deg % 90:
                rotated_pads += len(fp.pads)
    assert {0, 90, 180, 270, 45, 135, 225, 315} <= angles, sorted(angles)
    assert back_pads > 1000 and rotated_pads > 10, (back_pads, rotated_pads)


ZONE_BOARDS = sorted({*oracle.POSITION_BOARDS, "cm5_minima", "royalblue54L_feather", PADSTACK_BOARD})


def _contour_misses(ours, theirs):
    """Item-by-item difference between our contour and pcbnew's (``xy`` points and arcs as
    start, mid, end), or [] when they agree within TOL_MM."""
    if len(ours.items) != len(theirs):
        return [("item count", len(ours.items), len(theirs))]
    misses = []
    for mine, ref in zip(ours.items, theirs, strict=True):
        kind = "arc" if isinstance(mine, raw_geometry.Arc) else "xy"
        points = (mine.start, mine.mid, mine.end) if kind == "arc" else (mine,)
        expected = [tuple(map(oracle.nm_to_mm, p)) for p in ref[1:]]
        if kind != ref[0] or any(math.dist(a, b) > TOL_MM for a, b in zip(points, expected, strict=True)):
            misses.append((mine, ref))
    return misses


def _layers_agree(raw_layers, kicad_layers):
    if any("*" in layer or "&" in layer for layer in raw_layers):
        wildcard = {"*.Cu": lambda n: n.endswith(".Cu"), "F&B.Cu": lambda n: n in ("F.Cu", "B.Cu")}
        return all(any(wildcard.get(r, lambda n, r=r: n == r)(name) for r in raw_layers)
                   for name in kicad_layers)
    return set(raw_layers) == set(kicad_layers)


@pytest.mark.parametrize("name", ZONE_BOARDS)
def test_zone_outlines_match_pcbnew(demo_board, name):
    board = demo_board(name)
    kicad = {z["uuid"]: z for z in board.pcbnew()["zones"]}
    zones = raw_geometry.read_zones(board.root())
    assert {z.uuid for z in zones} == set(kicad)
    misses = []
    for zone in zones:
        ref = kicad[zone.uuid]
        assert len(ref["polygons"]) == 1, ref["polygons"]  # KiCad: first polygon outline, rest holes
        polygon = ref["polygons"][0]
        problems = _contour_misses(zone.outline, polygon["outline"])
        if len(zone.holes) != len(polygon["holes"]):
            problems.append(("hole count", len(zone.holes), len(polygon["holes"])))
        else:
            for hole, ref_hole in zip(zone.holes, polygon["holes"], strict=True):
                problems += _contour_misses(hole, ref_hole)
        if zone.footprint_uuid != ref["owner"] or zone.keepout != ref["keepout"]:
            problems.append(("owner/keepout", zone.footprint_uuid, zone.keepout, ref["owner"], ref["keepout"]))
        if not zone.keepout and zone.net != ref["net"]:
            problems.append(("net", zone.net, ref["net"]))
        if not _layers_agree(zone.layers, ref["layers"]):
            problems.append(("layers", zone.layers, ref["layers"]))
        if problems:
            misses.append((zone.uuid, problems[:3]))
    assert not misses, f"{len(misses)} of {len(zones)} zones differ from pcbnew: {misses[:5]}"


def test_the_zone_boards_cover_arcs_and_footprint_zones(demo_board):
    zones = raw_geometry.read_zones(demo_board("royalblue54L_feather").root())
    assert sum(1 for z in zones if any(isinstance(i, raw_geometry.Arc) for i in z.outline.items)) >= 2
    assert sum(1 for z in zones if z.footprint_uuid) == 2


_PCBNEW_SHAPES = {0: "line", 1: "rect", 2: "arc", 3: "circle", 4: "poly", 5: "curve"}


def _kicad_item_points(ref):
    kind = _PCBNEW_SHAPES[ref["shape"]]
    if kind in ("line", "rect"):
        pts = [ref["start"], ref["end"]]
    elif kind == "arc":
        pts = [ref["start"], ref["mid"], ref["end"]]
    elif kind == "circle":
        pts = [ref["center"], ref["end"]]
    elif kind == "curve":
        pts = [ref["start"], *ref["controls"], ref["end"]]
    else:
        pts = ref["points"]
    return kind, [tuple(map(oracle.nm_to_mm, p)) for p in pts]


def _outline_misses(outline, kicad_shapes):
    kicad = {s["uuid"]: s for s in kicad_shapes}
    assert {i.uuid for i in outline.items} == set(kicad)
    misses = []
    for item in outline.items:
        ref = kicad[item.uuid]
        kind, expected = _kicad_item_points(ref)
        if item.kind == "poly" and kind == "poly":  # compared token by token, arcs included
            off = bool(_contour_misses(item.contour, ref["contour"]))
        else:
            ours = list(item.points)
            off = len(ours) != len(expected) or any(
                math.dist(a, b) > TOL_MM for a, b in zip(ours, expected, strict=True))
        if (
            item.kind != kind
            or off
            or item.footprint_uuid != ref["owner"]
            or abs(item.width_mm - oracle.nm_to_mm(ref["width"])) > TOL_MM
        ):
            misses.append((item, ref))
    return misses


def _kicad_box(kicad_shapes):
    """The union of pcbnew's per-item bounding boxes, each shrunk by half its stroke width
    (pcbnew's box includes the stroke; ours is the centre line, KiCad's board edge)."""
    boxes = []
    for s in kicad_shapes:
        half = s["width"] / 2
        x0, y0, x1, y1 = s["bbox"]
        boxes.append((x0 + half, y0 + half, x1 - half, y1 - half))
    return tuple(oracle.nm_to_mm(v) for v in (
        min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)))


@pytest.mark.parametrize("name", sorted(oracle.ALL_BOARDS))
def test_board_outline_matches_pcbnew_item_by_item_and_in_extent(demo_board, name):
    board = demo_board(name)
    shapes = board.pcbnew()["edge_shapes"]
    outline = raw_geometry.read_board_outline(board.root())
    assert outline.problems == ()
    misses = _outline_misses(outline, shapes)
    assert not misses, f"{len(misses)} of {len(shapes)} Edge.Cuts items differ from pcbnew: {misses[:5]}"
    assert outline.bbox_mm == pytest.approx(_kicad_box(shapes), abs=TOL_MM)


def test_an_arc_corner_sets_the_outline_extent_on_cm5_minima(demo_board):
    """The `_outline_frame` bug class (paradigm-review-2 ticket 09). Its "0.11 mm" is how far
    the arc's mid point (75.903333, 83.485998) lies left of every line end; the arc itself
    sweeps on to x 75.05, KiCad's own outline box (pcbnew, stroke removed), 0.966 mm left."""
    outline = raw_geometry.read_board_outline(demo_board("cm5_minima").root())
    line_x = min(p[0] for i in outline.items if i.kind == "line" for p in i.points)
    file_x = min(p[0] for i in outline.items for p in i.points)
    assert line_x - file_x == pytest.approx(0.1127, abs=1e-3)
    assert outline.bbox_mm[:2] == pytest.approx((75.05, 20.25), abs=1e-4)
    assert line_x - outline.bbox_mm[0] == pytest.approx(0.966, abs=1e-3)


def test_footprint_edge_cuts_set_the_outline_extent_on_the_nfc_antenna(demo_board):
    """KiCad's outline includes J1's fp_* items on Edge.Cuts: top 66.560695 against 69.058195
    from the board's own gr_* items alone (docs/ARCHITECTURE.md, build ticket 42 fix round 1)."""
    outline = raw_geometry.read_board_outline(demo_board("nfc_antenna").root())
    board_only = [p for i in outline.items if i.footprint_uuid is None for p in raw_geometry.extent(i)]
    assert min(p[1] for p in board_only) == pytest.approx(69.058195, abs=1e-6)
    assert outline.bbox_mm[1] == pytest.approx(66.560695, abs=1e-6)


# --- Edge.Cuts kinds no demo board carries ------------------------------------------------
# Built on a copy of StickHub through the low-level tree and ``write_layer.save`` (its
# kicad-cli parse check included); pcbnew is the oracle. Footprint hosts: C36 and R7 are on
# the back side at 45 and -135 degrees; C32 is on the back at 90 degrees.


def _xy(tag, x, y):
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    return SExp.list(tag, wl.number(x), wl.number(y))


def _shape(prefix, kind, uid, *geometry):
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    return SExp.list(
        f"{prefix}_{kind}", *geometry,
        SExp.list("stroke", SExp.list("width", wl.number(0.1)), SExp.list("type", wl.symbol("default"))),
        SExp.list("layer", wl.string("Edge.Cuts")),
        SExp.list("uuid", wl.string(uid)),
    )


def _every_kind(prefix, n, dx=0.0, dy=0.0):
    """One of each Edge.Cuts shape kind, uuids ``…-<n><k>``; offset by (dx, dy)."""
    from kicad_tools.sexp import SExp

    def p(tag, x, y):
        return _xy(tag, x + dx, y + dy)

    def uid(k):
        return f"43000000-0000-4000-8000-0000000000{n}{k}"

    return [
        _shape(prefix, "line", uid(0), p("start", 0, 0), p("end", 3, 1)),
        _shape(prefix, "arc", uid(1), p("start", 0, 2), p("mid", 2, 4), p("end", 0, 6)),
        _shape(prefix, "circle", uid(2), p("center", 6, 3), p("end", 7.5, 3)),
        _shape(prefix, "rect", uid(3), p("start", 9, 0), p("end", 12, 5)),
        _shape(prefix, "poly", uid(4), SExp.list(
            "pts", p("xy", 14, 0),  # the arc's start is the next corner, as KiCad writes it
            SExp.list("arc", p("start", 17, 0), p("mid", 19.5, 2.5), p("end", 17, 5)),
            p("xy", 14, 5))),
        _shape(prefix, "curve", uid(5), SExp.list(
            "pts", p("xy", 21, 3), p("xy", 22, -3), p("xy", 25, 9), p("xy", 26, 3))),
    ]


@pytest.fixture(scope="module")
def synthetic_board(kicad_demos_dir, kicad_python, tmp_path_factory):
    from kicad_mcp import write_layer as wl

    scratch = tmp_path_factory.mktemp("oracle-synthetic")
    board = oracle.copy_board(kicad_demos_dir, oracle.ALL_BOARDS["stickhub"], scratch)
    root = wl.load(board)
    for item in _every_kind("gr", 1, dx=60.0, dy=20.0):  # page frame, beside the board
        root.append(item)
    hosts = {fp.reference: fp.uuid for fp in raw_geometry.read_footprints(board)}
    for n, ref in enumerate(("C36", "R7", "C32"), start=2):
        fp = next(c for c in root.children if not c.is_atom and c.name == "footprint"
                  and any(not u.is_atom and u.name == "uuid" and u.values[0] == hosts[ref]
                          for u in c.children))
        for item in _every_kind("fp", n):
            fp.append(item)
    wl.save(root, board)
    return oracle.BoardOracle("synthetic", board, scratch, kicad_python)


def test_every_edge_cuts_kind_in_turned_back_side_footprints_matches_pcbnew(synthetic_board):
    shapes = synthetic_board.pcbnew()["edge_shapes"]
    outline = raw_geometry.read_board_outline(synthetic_board.board)
    assert outline.problems == ()
    ours = Counter((i.footprint_reference, i.kind) for i in outline.items if i.uuid.startswith("43000000"))
    # A rectangle in a 45-degree footprint is no longer one: KiCad holds it as a polygon.
    assert ours == Counter(
        [(None, k) for k in ("line", "arc", "circle", "rect", "poly", "curve")]
        + [(ref, k) for ref in ("C36", "R7", "C32") for k in ("line", "arc", "circle", "poly", "curve")]
        + [("C36", "poly"), ("R7", "poly"), ("C32", "rect")]
    )
    misses = _outline_misses(outline, shapes)
    assert not misses, f"{len(misses)} of {len(shapes)} Edge.Cuts items differ from pcbnew: {misses[:5]}"
    assert outline.bbox_mm == pytest.approx(_kicad_box(shapes), abs=TOL_MM)
    for item in outline.items:  # and each item's own exact extent against pcbnew's box
        ref = next(s for s in shapes if s["uuid"] == item.uuid)
        pts = raw_geometry.extent(item)
        box = (min(q[0] for q in pts), min(q[1] for q in pts), max(q[0] for q in pts), max(q[1] for q in pts))
        assert box == pytest.approx(_kicad_box([ref]), abs=TOL_MM), (item.kind, item.footprint_reference)


# --- what the outline reader cannot measure is named, never guessed -----------------------


def _tree(*items):
    from kicad_tools.sexp import SExp

    return SExp.list("kicad_pcb", *items)


def test_an_outline_item_the_reader_cannot_measure_is_named_in_problems():
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    text = SExp.list("gr_text", wl.string("x"), _xy("at", 5, 5), SExp.list("layer", wl.string("Edge.Cuts")),
                     SExp.list("uuid", wl.string("t1")))
    legacy = _shape("gr", "arc", "a-legacy", _xy("start", 0, 0), _xy("end", 1, 0))
    collinear = _shape("gr", "arc", "a-flat", _xy("start", 0, 0), _xy("mid", 1, 0), _xy("end", 2, 0))
    line = _shape("gr", "line", "l1", _xy("start", 0, 0), _xy("end", 10, 4))
    silk = SExp.list("gr_line", _xy("start", -50, -50), _xy("end", 0, 0), SExp.list("layer", wl.string("F.SilkS")))
    outline = raw_geometry.read_board_outline(_tree(text, legacy, collinear, line, silk))
    assert [i.uuid for i in outline.items] == ["a-flat", "l1"]
    assert len(outline.problems) == 3
    assert "gr_text" in outline.problems[0]
    assert "a-legacy" in outline.problems[1] and "mid" in outline.problems[1]
    assert "a-flat" in outline.problems[2] and "collinear" in outline.problems[2]
    assert outline.bbox_mm == (0.0, 0.0, 10.0, 4.0)  # the silk line is not outline


def test_a_board_without_edge_cuts_has_no_outline_box():
    outline = raw_geometry.read_board_outline(_tree())
    assert outline == raw_geometry.BoardOutline((), None, ())


def test_a_malformed_coordinate_is_refused_not_read_as_zero():
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    bad = _shape("gr", "line", "l1", SExp.list("start", wl.number(0), wl.symbol("north")), _xy("end", 1, 1))
    outline = raw_geometry.read_board_outline(_tree(bad))
    assert outline.items == () and len(outline.problems) == 1 and "l1" in outline.problems[0]
    for token in ("nan", "inf", "-inf"):
        bad = _shape("gr", "line", "l2", SExp.list("start", wl.number(0), wl.string(token)), _xy("end", 1, 1))
        assert raw_geometry.read_board_outline(_tree(bad)).problems != (), token
    with pytest.raises(raw_geometry.RawGeometryError):
        raw_geometry.read_footprints(_tree(SExp.list("footprint", wl.string("lib:fp"), SExp.list("at", wl.number(1)))))


# --- KiCad 10 padstacks: per-layer pad copper -----------------------------------------------
# A pad's own (shape)/(size) is KiCad's F.Cu entry; (padstack (mode front_inner_back|custom)
# (layer "Inner"|"B.Cu"|"In<n>.Cu" (shape ...) (size ...))) holds the other copper layers.


def _file_shape(kicad_shape):
    # KiCad writes a chamfered rectangle as (shape roundrect) plus its chamfer tokens.
    return "roundrect" if kicad_shape == "chamfered_rect" else kicad_shape


def _undetermined(pad):
    """Pads whose flashed copper the file alone does not settle: copper_on may refuse a layer
    KiCad flashes (fail closed), never answer one it does not."""
    return pad.kind == "np_thru_hole" or pad.remove_unused_layers


@pytest.mark.parametrize("name", PAD_BOARDS)
def test_pad_copper_on_every_copper_layer_matches_pcbnew(demo_board, name):
    """Every board copper layer of every pad: where KiCad flashes copper, copper_on gives its
    shape and size (or, for an undetermined pad, refuses); where the pad is not on the layer,
    or KiCad flashes nothing there, copper_on refuses. pcbnew's GetSize(layer) answers any
    layer, so membership comes from IsOnLayer and FlashLayer (delta review 1)."""
    board = demo_board(name)
    dump = board.pcbnew()
    kicad = {fp["uuid"]: fp for fp in dump["footprints"]}
    misses, compared, refused = [], 0, 0
    for fp in raw_geometry.read_footprints(board.root()):
        for pad, ref in zip(fp.pads, kicad[fp.uuid]["pads"], strict=True):
            if pad.stack_mode != ref["mode"]:
                misses.append((fp.reference, pad.number, "mode", pad.stack_mode, ref["mode"]))
                continue
            for layer, shape, size, on, flashed in ref["copper"]:
                try:
                    mine = pad.copper_on(layer)
                except raw_geometry.RawGeometryError as exc:
                    refused += 1
                    if on and flashed and not _undetermined(pad):
                        misses.append((fp.reference, pad.number, layer, "refused", str(exc)))
                    continue
                compared += 1
                if not (on and flashed):
                    misses.append((fp.reference, pad.number, layer, "answered", on, flashed))
                    continue
                expected = tuple(map(oracle.nm_to_mm, size))
                if mine.shape != _file_shape(shape) or max(
                        abs(a - b) for a, b in zip(mine.size_mm, expected, strict=True)) > TOL_MM:
                    misses.append((fp.reference, pad.number, layer, mine, shape, expected))
    assert compared and refused
    assert not misses, f"{len(misses)} of {compared + refused} pad layers disagree with pcbnew: {misses[:5]}"


def test_the_padstack_board_covers_every_padstack_mode_and_zone_holes(demo_board):
    """Literal expectations from tests/_pcbnew_fixture.py's own specification."""
    board = demo_board(PADSTACK_BOARD)
    pads = {p.uuid: (fp, p) for fp in raw_geometry.read_footprints(board.root()) for p in fp.pads}

    def copper(pad, layer):
        found = pad.copper_on(layer)
        return found.shape, found.size_mm

    _, custom = pads[board.made["custom"]]
    assert custom.stack_mode == "custom"
    assert copper(custom, "In1.Cu") == ("rect", pytest.approx((0.9, 0.8)))
    assert copper(custom, "B.Cu") == ("oval", pytest.approx((3.0, 2.0)))
    assert copper(custom, "In2.Cu") == (custom.shape, pytest.approx(custom.size_mm))  # KiCad's default
    _, fib = pads[board.made["front_inner_back"]]
    assert fib.stack_mode == "front_inner_back"
    assert copper(fib, "In1.Cu") == copper(fib, "In2.Cu") == ("roundrect", pytest.approx((1.1, 0.7)))
    assert copper(fib, "B.Cu") == ("circle", pytest.approx((2.5, 2.5)))
    fp, back = pads[board.made["back_side"]]
    assert fp.side == "back" and back.stack_mode == "front_inner_back"
    # Its own (shape)/(size) is KiCad's F.Cu entry, but the SMD pad is not on F.Cu.
    assert (back.copper[0].shape, back.copper[0].size_mm) == ("rect", pytest.approx((2.2, 1.1)))
    assert (back.shape, back.size_mm) == ("rect", pytest.approx((2.2, 1.1)))
    with pytest.raises(raw_geometry.RawGeometryError, match="is not on 'F.Cu'"):
        back.copper_on("F.Cu")
    assert copper(back, "B.Cu") == ("oval", pytest.approx((4.0, 3.0)))  # the copper it sits on
    _, removed = pads[board.made["remove_unused"]]
    assert removed.remove_unused_layers and removed.keep_end_layers
    assert copper(removed, "F.Cu") == copper(removed, "B.Cu") == (removed.shape, removed.size_mm)
    for layer in ("In1.Cu", "In2.Cu"):
        with pytest.raises(raw_geometry.RawGeometryError, match="remove_unused_layers"):
            removed.copper_on(layer)
    # A removed pad a filled inner zone reaches: KiCad wrote that layer in zone_layer_connections.
    _, zoned = pads[board.made["zone_connected"]]
    assert zoned.remove_unused_layers and not zoned.keep_end_layers and zoned.zone_layer_connections
    for layer in zoned.zone_layer_connections:
        assert copper(zoned, layer) == (zoned.shape, zoned.size_mm)
    for layer in ("F.Cu", "B.Cu"):
        with pytest.raises(raw_geometry.RawGeometryError, match="remove_unused_layers"):
            zoned.copper_on(layer)
    # A pad whose (layers ...) names F.Cu by the board's user name: KiCad's Rescue layer.
    _, renamed = pads[board.made["user_name"]]
    assert renamed.layers[0] == "top_copper" and renamed.number == "1"
    kicad_pad = next(p for fp in board.pcbnew()["footprints"] for p in fp["pads"] if p["uuid"] == renamed.uuid)
    assert not any(on or flashed for _, _, _, on, flashed in kicad_pad["copper"])  # no copper at all
    assert kicad_pad["number"] == ""  # and KiCad clears its number, as for any pad off copper
    for layer in ("F.Cu", "In1.Cu", "B.Cu"):
        with pytest.raises(raw_geometry.RawGeometryError, match="'top_copper'.*Rescue"):
            renamed.copper_on(layer)
    zone = next(z for z in raw_geometry.read_zones(board.root()) if z.uuid == board.made["zone"])
    widths = sorted(max(p[0] for p in h.corners()) - min(p[0] for p in h.corners()) for h in zone.holes)
    assert widths == pytest.approx([1.0, 2.0])


BOARD_CU = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")  # the synthetic boards' copper, in stack order


def _layer_table(copper, users=None):
    """A board ``(layers ...)`` table as KiCad 10 writes it: F.Cu 0, B.Cu 2, In<n>.Cu 2 + 2n,
    each row ``(n "<canonical>" signal ["<user name>"])``, plus a technical layer, which is
    not copper. ``users`` maps a canonical name to its user name."""
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    def ordinal(name):
        return {"F.Cu": 0, "B.Cu": 2}.get(name) if not name.startswith("In") else 2 + 2 * int(name[2:-3])

    users = users or {}
    rows = [SExp.list(str(ordinal(n)), wl.string(n), wl.symbol("signal"),
                      *((wl.string(users[n]),) if n in users else ())) for n in copper]
    rows.append(SExp.list("1", wl.string("F.Mask"), wl.symbol("user")))
    return SExp.list("layers", *rows)


def _flag(name, *value):
    """``(remove_unused_layers yes)``, or the bare ``(remove_unused_layers)``."""
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    return SExp.list(name, *(wl.symbol(v) for v in value))


def _zone_connections(*layers):
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    return SExp.list("zone_layer_connections", *(wl.string(n) for n in layers))


def _pad_tree(*stack, shape="circle", kind="thru_hole", layers=("*.Cu", "*.Mask"), extra=(),
              copper=BOARD_CU, users=None):
    """A board with one footprint holding pad "7"; ``copper`` None leaves out the board's
    ``(layers ...)`` table, ``users`` gives its rows user names. ``extra`` nodes go into the
    pad after its ``(layers ...)``."""
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    pad = SExp.list("pad", wl.string("7"), wl.symbol(kind), wl.symbol(shape),
                    _xy("at", 1, 2), _xy("size", 1.6, 1.6),
                    SExp.list("layers", *(wl.string(n) for n in layers)), *extra)
    if stack:
        pad.append(SExp.list("padstack", *stack))
    table = (_layer_table(copper, users),) if copper is not None else ()
    return _tree(*table, SExp.list("footprint", wl.string("lib:fp"), _xy("at", 10, 20), pad))


def _stack_layer(name, shape="rect", size=(1.0, 0.5)):
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    parts = [SExp.list("shape", wl.symbol(shape))] if shape else []
    if size:
        parts.append(_xy("size", *size))
    return SExp.list("layer", wl.string(name), *parts)


def _mode(name):
    from kicad_mcp import write_layer as wl
    from kicad_tools.sexp import SExp

    return SExp.list("mode", wl.symbol(name))


def test_a_normal_pad_has_its_own_copper_on_every_layer_it_is_on():
    (fp,) = raw_geometry.read_footprints(_pad_tree())
    (pad,) = fp.pads
    assert pad.stack_mode == "normal"
    for layer in BOARD_CU:
        assert (pad.copper_on(layer).shape, pad.copper_on(layer).size_mm) == ("circle", (1.6, 1.6))


def _one_pad(**kwargs):
    (fp,) = raw_geometry.read_footprints(_pad_tree(**kwargs))
    (pad,) = fp.pads
    return pad


def _refused(pad, layer):
    try:
        pad.copper_on(layer)
    except raw_geometry.RawGeometryError as exc:
        assert "pad 7" in str(exc), exc  # every refusal names the pad
        return str(exc)
    return None


@pytest.mark.parametrize("kind, layers, on", [
    ("smd", ("F.Cu", "F.Paste", "F.Mask"), {"F.Cu"}),  # StickHub D4/1 (delta review 1)
    ("smd", ("B.Cu", "B.Mask", "B.Paste"), {"B.Cu"}),
    ("thru_hole", ("*.Cu", "*.Mask"), set(BOARD_CU)),
    ("thru_hole", ("F&B.Cu", "*.Mask"), {"F.Cu", "B.Cu"}),
    ("connect", ("F.Cu", "In2.Cu"), {"F.Cu", "In2.Cu"}),
], ids=["smd-front", "smd-back", "all-copper", "front-and-back", "named-inner"])
def test_copper_on_answers_only_the_copper_layers_the_pad_is_on(kind, layers, on):
    pad = _one_pad(kind=kind, layers=layers)
    for layer in BOARD_CU:
        if layer in on:
            assert pad.copper_on(layer) == pad.copper[0], layer
        else:
            assert "is not on" in (_refused(pad, layer) or "answered"), layer


def test_a_non_copper_layer_is_refused_for_a_normal_pad():
    pad = _one_pad(layers=("*.Cu", "*.Mask", "F.SilkS"))
    for layer in ("F.SilkS", "F.Mask", "Edge.Cuts", "Inner"):
        assert "not a copper layer" in (_refused(pad, layer) or "answered"), layer


def test_a_copper_layer_the_board_does_not_have_is_refused():
    pad = _one_pad()
    for layer in ("In3.Cu", "In30.Cu", "In99.Cu"):
        assert "board" in (_refused(pad, layer) or "answered"), layer
    no_table = _one_pad(copper=None)
    assert "(layers ...) table" in (_refused(no_table, "F.Cu") or "answered")


def test_a_pad_layer_given_by_its_user_name_is_not_copper():
    """KiCad 10.0.7 reads a board's user layer name in a pad's ``(layers ...)`` as the Rescue
    layer, not as that copper layer: pcbnew's IsOnLayer and FlashLayer are False for F.Cu and
    a re-save writes "Rescue" (delta review 2). So the token refuses every copper layer."""
    pad = _one_pad(kind="smd", layers=("top_copper", "F.Mask"), users={"F.Cu": "top_copper", "B.Cu": "bottom"})
    for layer in BOARD_CU:
        message = _refused(pad, layer) or "answered"
        assert "pad layer 'top_copper'" in message and "Rescue" in message, message
    canonical = _one_pad(kind="smd", layers=("F.Cu", "F.Mask"), users={"F.Cu": "top_copper"})
    assert canonical.copper_on("F.Cu") == canonical.copper[0]  # the canonical name still is F.Cu


@pytest.mark.parametrize("token", ["*In.Cu", "Inner1.Cu", "In3.Cu"])
def test_a_pad_layer_the_reader_cannot_resolve_is_refused_naming_it(token):
    pad = _one_pad(layers=("F.Cu", token))
    for layer in ("F.Cu", "In1.Cu"):  # even the layer it plainly names: its copper set is unknown
        message = _refused(pad, layer) or "answered"
        assert f"pad layer {token!r} is not resolved" in message, message


def test_remove_unused_layers_answers_only_the_layers_the_file_says_are_flashed():
    """``(remove_unused_layers yes)`` flashes copper only where a track, via or zone connects;
    the file states the zones (``zone_layer_connections``) and, with ``keep_end_layers``, the
    outer layers, but not the tracks: every other layer is undetermined and refused."""
    removed = _one_pad(extra=(_flag("remove_unused_layers", "yes"), _flag("keep_end_layers", "no"),
                              _zone_connections()))
    for layer in BOARD_CU:
        assert "remove_unused_layers" in (_refused(removed, layer) or "answered"), layer
    kept = _one_pad(extra=(_flag("remove_unused_layers", "yes"), _flag("keep_end_layers", "yes"),
                           _zone_connections("In2.Cu")))
    for layer in ("F.Cu", "In2.Cu", "B.Cu"):
        assert kept.copper_on(layer) == kept.copper[0], layer
    assert "remove_unused_layers" in (_refused(kept, "In1.Cu") or "answered")
    bare = _one_pad(extra=(_flag("remove_unused_layers"), _flag("keep_end_layers")))  # bare means yes
    assert bare.copper_on("F.Cu") == bare.copper[0]
    assert "remove_unused_layers" in (_refused(bare, "In1.Cu") or "answered")
    off = _one_pad(extra=(_flag("remove_unused_layers", "no"), _flag("keep_end_layers", "no")))
    assert all(off.copper_on(layer) == off.copper[0] for layer in BOARD_CU)


def test_a_malformed_remove_unused_layers_flag_is_refused_naming_the_pad():
    with pytest.raises(raw_geometry.RawGeometryError, match=r"pad 7.*remove_unused_layers"):
        _one_pad(extra=(_flag("remove_unused_layers", "maybe"),))


def test_an_np_thru_hole_pad_is_refused_on_copper():
    """KiCad flashes an np_thru_hole pad's copper only where the pad reaches past its hole;
    this reader does not resolve that, so it refuses rather than guess."""
    pad = _one_pad(kind="np_thru_hole")
    assert "np_thru_hole" in (_refused(pad, "F.Cu") or "answered")


def test_a_pad_with_two_padstacks_is_refused_naming_the_pad():
    from kicad_tools.sexp import SExp

    stack = SExp.list("padstack", _mode("custom"), _stack_layer("B.Cu"))
    twin = SExp.list("padstack", _mode("custom"), _stack_layer("B.Cu"))
    with pytest.raises(raw_geometry.RawGeometryError, match=r"pad 7 has 2 \(padstack"):
        raw_geometry.read_footprints(_pad_tree(extra=(stack, twin)))


@pytest.mark.parametrize("stack, why", [
    ((_mode("mirrored"),), "mode"),
    ((_mode("front_inner_back"), _stack_layer("In1.Cu"), _stack_layer("B.Cu")), "In1.Cu"),
    ((_mode("front_inner_back"), _stack_layer("Inner"), _stack_layer("Inner"), _stack_layer("B.Cu")), "twice"),
    ((_mode("custom"), _stack_layer("Inner"), _stack_layer("B.Cu")), "Inner"),
    ((_mode("custom"), _stack_layer("B.Cu", size=None)), "size"),
    ((_mode("custom"), _stack_layer("B.Cu", shape=None)), "shape"),
    ((_stack_layer("B.Cu"),), "mode"),
], ids=["unknown-mode", "fib-named-layer", "duplicate", "custom-inner", "no-size", "no-shape", "no-mode"])
def test_a_padstack_the_reader_cannot_interpret_is_refused_naming_the_pad(stack, why):
    with pytest.raises(raw_geometry.RawGeometryError) as caught:
        raw_geometry.read_footprints(_pad_tree(*stack))
    assert "pad 7" in str(caught.value) and why in str(caught.value)


def test_a_copper_layer_the_padstack_does_not_hold_is_refused():
    (fp,) = raw_geometry.read_footprints(_pad_tree(_mode("custom"), _stack_layer("B.Cu")))
    (pad,) = fp.pads
    assert pad.copper_on("B.Cu").size_mm == (1.0, 0.5)
    for layer in ("In1.Cu", "F.SilkS"):
        with pytest.raises(raw_geometry.RawGeometryError, match="pad 7"):
            pad.copper_on(layer)
    (fp,) = raw_geometry.read_footprints(_pad_tree(_mode("front_inner_back"), _stack_layer("B.Cu")))
    with pytest.raises(raw_geometry.RawGeometryError, match="Inner"):
        fp.pads[0].copper_on("In1.Cu")
