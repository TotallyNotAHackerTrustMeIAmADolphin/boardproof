"""Issue 256: ``placement_anchor.write_poses`` sets a footprint's position AND rotation from raw tokens.

Every expectation is hand-computed from the raw ``(at ...)`` tokens of the file before the write
(pad and text coordinates are footprint-local and stay; pad, text and property angles are stored
board-absolute and move by the same delta as the footprint), never from a ``kicad_tools``
property and never through ``raw_geometry.place``. KiCad 10 boards (version 20260206) are a copy of
the rung-2 fixture; older-format boards are built through ``kicad_tools`` and saved through the
write layer.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from boardproof import placement_anchor as pa
from boardproof import write_layer
from boardproof.sourcing.footprint_gen import generate_footprint
from tests.test_placement_anchor import _board, _fp, _root

KICAD10 = Path(__file__).parent / "fixtures" / "uw" / "r2krt" / "in" / "pic_programmer.kicad_pcb"


@pytest.fixture
def chips(tmp_path) -> dict:
    out = {}
    for size in ("0805", "1206"):
        path = tmp_path / f"{size}.kicad_mod"
        assert generate_footprint("chip", str(path), size=size)["status"] == "ok"
        out[size] = path
    return out


@pytest.fixture
def pic(tmp_path) -> Path:
    out = tmp_path / "pic.kicad_pcb"
    shutil.copyfile(KICAD10, out)
    return out


def _at(node) -> list[float]:
    values = [float(v) for v in node.find("at").values]
    return values + [0.0] * (3 - len(values))


def _turn(origin, angle, local):
    """KiCad's rotation on a y-down page, counter-clockwise on screen, written out by hand."""
    c, s = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    return (origin[0] + local[0] * c + local[1] * s, origin[1] - local[0] * s + local[1] * c)


def _snapshot(path, ref):
    """Raw facts of one footprint: its (at), and for every pad its local (x, y) and angle, and the angle
    of every text and property."""
    fp = _fp(_root(path), ref)
    pads = [(_at(p)[:2], _at(p)[2]) for p in fp.children if not p.is_atom and p.name == "pad"]
    texts = [_at(t)[2] for t in fp.children if not t.is_atom and t.name in ("fp_text", "property") and t.find("at") is not None]
    return _at(fp), pads, texts


def _others(path, skip):
    """Every top-level node other than the footprints in ``skip``, as text."""
    from boardproof import _self_check

    root = _root(path)
    # top-level (net n "name") declarations are not compared: KiCad 10 files carry nets by name and the write
    # layer drops the table of a file that still had one, whoever writes it
    return [_self_check.node_text(c) for c in root.children
            if c.is_atom or c.name != "net" and (c.name != "footprint" or _ref(c) not in skip)]


def _ref(fp):
    return next(str(c.children[1].value) for c in fp.children if not c.is_atom and c.name == "property" and c.children[0].value == "Reference")


def _wrapped(a, b):
    d = (a - b) % 360.0
    return min(d, 360.0 - d) < 1e-6


def _check_pose(path, before, ref, pose):
    (bx, by, ba), pads0, texts0 = before
    x, y, angle = pose
    delta = angle - ba
    (ax, ay, aa), pads1, texts1 = _snapshot(path, ref)
    assert (ax, ay) == pytest.approx((x, y)) and _wrapped(aa, angle)
    assert len(pads1) == len(pads0)
    for (local0, pa0), (local1, pa1) in zip(pads0, pads1, strict=True):
        assert local1 == pytest.approx(local0)  # footprint-local coordinates stay
        assert _wrapped(pa1, pa0 + delta)  # the absolute angle moved by exactly delta
        # the page position: the old one turned by delta around the origin, from the new origin
        old_page = _turn((bx, by), ba, local0)
        expected = _turn((x, y), angle, local0)
        turned = _turn((x, y), delta, (old_page[0] - bx, old_page[1] - by))
        assert expected == pytest.approx(turned, abs=1e-6)
    for t0, t1 in zip(texts0, texts1, strict=True):
        assert _wrapped(t1, t0 + delta)


# --------------------------------------------------------------------------- KiCad 10


def test_kicad10_front_quarter_turn_moves_every_angle_and_nothing_else(pic):
    before = _snapshot(pic, "P1")
    assert before[0][2] == -90.0 and all(a == 270.0 for _, a in before[1])
    others = _others(pic, {"P1"})
    result = pa.write_poses(pic, {"P1": (90.0, 70.0, 0.0)})
    assert result == {"changed": 1}
    _check_pose(pic, before, "P1", (90.0, 70.0, 0.0))
    assert [a for _, a in _snapshot(pic, "P1")[1]] == [0.0, 0.0]
    assert _others(pic, {"P1"}) == others


def test_kicad10_back_footprint_with_custom_pads_quarter_turn(pic):
    before = _snapshot(pic, "JP1")
    assert before[0][2] == 0.0
    others = _others(pic, {"JP1"})
    pa.write_poses(pic, {"JP1": (150.0, 100.0, 90.0)})
    _check_pose(pic, before, "JP1", (150.0, 100.0, 90.0))
    assert [a for _, a in _snapshot(pic, "JP1")[1]] == [90.0, 90.0]
    assert _others(pic, {"JP1"}) == others


def test_kicad10_back_footprint_non_quarter_angle_two_footprints_at_once(pic):
    b_jp, b_p1 = _snapshot(pic, "JP1"), _snapshot(pic, "P1")
    pa.write_poses(pic, {"JP1": (150.0, 100.0, 33.5), "P1": (82.0, 61.0, 180.0)})
    _check_pose(pic, b_jp, "JP1", (150.0, 100.0, 33.5))
    _check_pose(pic, b_p1, "P1", (82.0, 61.0, 180.0))
    # one pad of JP1, by hand: local (lx, ly) -> (lx cos + ly sin, -lx sin + ly cos) around the new origin
    (lx, ly), _ = b_jp[1][0]
    c, s = math.cos(math.radians(33.5)), math.sin(math.radians(33.5))
    assert _turn((150.0, 100.0), 33.5, (lx, ly)) == pytest.approx((150.0 + lx * c + ly * s, 100.0 - lx * s + ly * c))
    after = pa.read_parts(_root(pic), {"JP1"})[0]
    assert after.pads[0].at_mm == pytest.approx(_turn((150.0, 100.0), 33.5, (lx, ly)), abs=1e-6)


def test_a_position_only_pose_touches_no_angle(pic):
    before = _snapshot(pic, "P1")
    pa.write_poses(pic, {"P1": (90.0, 70.0, -90.0)})
    after = _snapshot(pic, "P1")
    assert after[0] == [90.0, 70.0, -90.0] and after[1] == before[1] and after[2] == before[2]


def test_a_pose_equal_to_the_present_one_writes_nothing(pic):
    raw = pic.read_bytes()
    assert pa.write_poses(pic, {"P1": (81.8, 60.0, -90.0), "C1": (110.49, 78.867, 180.0)}) == {"changed": 0}
    assert pic.read_bytes() == raw
    assert pa.write_poses(pic, {"P1": (81.8, 60.0, 270.0)}) == {"changed": 0}  # 270 is -90


# --------------------------------------------------------------------------- older format


def test_older_format_front_and_back_quarter_and_non_quarter(tmp_path, chips):
    from kicad_tools.schema import PCB

    board = PCB.create(width=100.0, height=60.0)
    board.add_footprint_from_file(chips["0805"], "R1", 10.0, 10.0, rotation=0.0)
    board.add_footprint_from_file(chips["1206"], "R2", 30.0, 10.0, rotation=90.0, layer="B.Cu")
    board.add_footprint_from_file(chips["0805"], "R3", 50.0, 10.0, rotation=0.0)
    path = tmp_path / "old.kicad_pcb"
    write_layer.save_pcb(board, path)
    b1, b2, b3 = _snapshot(path, "R1"), _snapshot(path, "R2"), _snapshot(path, "R3")
    others = _others(path, {"R1", "R2"})
    pa.write_poses(path, {"R1": (20.0, 20.0, 90.0), "R2": (40.0, 20.0, 45.0)})
    _check_pose(path, b1, "R1", (20.0, 20.0, 90.0))
    _check_pose(path, b2, "R2", (40.0, 20.0, 45.0))
    assert _snapshot(path, "R3") == b3 and _others(path, {"R1", "R2"}) == others


def test_a_pad_without_an_angle_token_gets_one_when_the_footprint_turns(tmp_path, chips):
    path = _board(tmp_path, chips, [("R1", "0805", 10, 10)])
    pa.write_poses(path, {"R1": (10.0, 10.0, 90.0)})
    # chip pads (at x 0) of an unrotated footprint: local (-0.9, 0) goes to page (10, 10 + 0.9) by hand
    parts = pa.read_parts(_root(path))
    xs = sorted((round(p.at_mm[0], 6), round(p.at_mm[1], 6)) for p in parts[0].pads)
    assert [x for x, _ in xs] == [10.0, 10.0] and sorted(y for _, y in xs)[0] < 10.0 < sorted(y for _, y in xs)[1]
    assert {_wrapped(p.angle_deg, 90.0) for p in parts[0].pads} == {True}


# --------------------------------------------------------------------------- refusals


def _locked(pic):
    pa.write_lock_state(pic, {"P1": True})


def test_a_locked_footprint_is_refused_and_nothing_is_written(pic):
    _locked(pic)
    raw = pic.read_bytes()
    with pytest.raises(pa.PoseRefused) as e:
        pa.write_poses(pic, {"P1": (90.0, 70.0, 0.0), "JP1": (150.0, 100.0, 90.0)})
    assert e.value.problems == ["P1: the footprint is locked"]
    assert pic.read_bytes() == raw


@pytest.mark.parametrize("pose", [(float("nan"), 1.0, 0.0), (1.0, float("inf"), 0.0), (1.0, 1.0, float("nan")), (1.0, 1.0), ("1", 1.0, 0.0)])
def test_a_pose_that_is_not_three_finite_numbers_is_refused(pic, pose):
    raw = pic.read_bytes()
    with pytest.raises(pa.PoseRefused) as e:
        pa.write_poses(pic, {"P1": pose})
    assert "not three finite numbers" in e.value.problems[0]
    assert pic.read_bytes() == raw


def test_an_unknown_reference_is_refused_and_so_is_an_ambiguous_one(pic, tmp_path):
    raw = pic.read_bytes()
    with pytest.raises(pa.PoseRefused, match="no footprint"):
        pa.write_poses(pic, {"NOPE": (1.0, 1.0, 0.0), "P1": (1.0, 1.0, 0.0)})
    assert pic.read_bytes() == raw
    # two footprints with one reference: copy a footprint's reference onto another through the write layer
    root = write_layer.load(pic)
    fps = [c for c in root.children if not c.is_atom and c.name == "footprint"]
    for c in fps[0].children:
        if not c.is_atom and c.name == "property" and c.children[0].value == "Reference":
            write_layer.set_string(c, 1, _ref(fps[1]))
    twin = tmp_path / "twin.kicad_pcb"
    write_layer.save(root, twin)
    with pytest.raises(pa.PoseRefused, match="more than one"):
        pa.write_poses(twin, {_ref(fps[1]): (1.0, 1.0, 0.0)})


# --------------------------------------------------------------------------- the self-check


def test_the_reload_check_refuses_a_turn_that_leaves_the_pad_angles_behind(pic, monkeypatch):
    raw = pic.read_bytes()
    original = write_layer.set_number
    # the edit writes pad, text and property angles first and the footprint's own (at) last: drop the first ones
    own = []

    def skipping(node, index, value):
        own.append(index == 0)  # the footprint's x marks the end of the pad and text angles
        if index == 2 and not any(own):
            return None
        return original(node, index, value)

    monkeypatch.setattr(write_layer, "set_number", skipping)
    with pytest.raises(pa.NotVerified) as e:
        pa.write_poses(pic, {"P1": (90.0, 70.0, 0.0)})
    assert "something other than the pose" in " ".join(e.value.mismatches)
    assert pic.read_bytes() == raw


def test_the_reload_check_refuses_a_pad_that_moved_its_local_position(pic, monkeypatch):
    original = write_layer.set_number
    seen = {}

    def bad(node, index, value):
        if index == 2 and not seen:
            seen["x"] = True
            node.values[0]  # a pad or text (at x y angle): also shift its local x
            original(node, 0, float(node.values[0]) + 0.25)
        return original(node, index, value)

    monkeypatch.setattr(write_layer, "set_number", bad)
    with pytest.raises(pa.NotVerified):
        pa.write_poses(pic, {"P1": (90.0, 70.0, 0.0)})


def test_the_reload_check_refuses_a_wrong_footprint_position(pic, monkeypatch):
    original = write_layer.set_number

    def off(node, index, value):
        return original(node, index, value + 0.5 if index == 0 and len(node.values) == 3 and float(node.values[2]) == -90.0 else value)

    monkeypatch.setattr(write_layer, "set_number", off)
    with pytest.raises(pa.NotVerified, match="intended placement"):
        pa.write_poses(pic, {"P1": (90.0, 70.0, 0.0)})


def test_the_reload_check_refuses_another_footprint_changing(pic, monkeypatch):
    from tests.test_placement_anchor import _corrupt_write

    def edit(text):
        return text.replace('(version 20260206)', '(version 20260206)', 1).replace("(at 110.49 78.867 180)", "(at 110.5 78.867 180)", 1)

    _corrupt_write(monkeypatch, edit)
    with pytest.raises(pa.NotVerified):
        pa.write_poses(pic, {"P1": (90.0, 70.0, 0.0)})


# --------------------------------------------------------------------------- review follow-ups


@pytest.mark.parametrize(("angle", "wrapped"), [(270.0, -90.0), (180.0, 180.0), (-180.0, 180.0), (360.0, 0.0), (725.0, 5.0)])
def test_the_written_angle_is_wrapped_into_minus_180_to_180(pic, angle, wrapped):
    before = _snapshot(pic, "P1")
    pa.write_poses(pic, {"P1": (90.0, 70.0, angle)})
    after = _snapshot(pic, "P1")
    assert after[0][2] == pytest.approx(wrapped)
    if not _wrapped(angle, -90.0):  # a turn by a multiple of 360 leaves every token as it was
        assert all(-180.0 < a <= 180.0 for _, a in after[1])
    _check_pose(pic, before, "P1", (90.0, 70.0, angle))


def _edit_p1(pic, edit):
    root = write_layer.load(pic)
    edit(root, _fp(root, "P1"))
    write_layer.save(root, pic)


def test_a_text_box_angle_is_turned_by_the_same_delta(pic):
    # Whether KiCad stores a footprint text box's (angle) board-absolute is not established here; this pins
    # only that write_poses turns it by delta like the texts, so it stays relative to the footprint.
    from kicad_tools.sexp import SExp

    box = SExp.list("fp_text_box", write_layer.string("hi"), SExp.list("start", write_layer.number(0), write_layer.number(0)),
                    SExp.list("end", write_layer.number(1), write_layer.number(1)),
                    SExp.list("angle", write_layer.number(-90)), SExp.list("layer", write_layer.string("F.SilkS")))
    _edit_p1(pic, lambda root, fp: fp.append(box))
    pa.write_poses(pic, {"P1": (90.0, 70.0, 0.0)})
    angle = next(c for c in _fp(_root(pic), "P1").children if not c.is_atom and c.name == "fp_text_box").find("angle")
    assert float(angle.values[0]) == pytest.approx(0.0)


def test_a_footprint_with_the_bare_locked_token_is_refused(pic):
    _edit_p1(pic, lambda root, fp: fp.children.insert(1, write_layer.symbol("locked")))
    assert pa.is_locked(_fp(_root(pic), "P1"))
    raw = pic.read_bytes()
    with pytest.raises(pa.PoseRefused, match="locked"):
        pa.write_poses(pic, {"P1": (90.0, 70.0, 0.0)})
    assert pic.read_bytes() == raw


def test_a_group_member_is_posed_without_touching_the_group(pic):
    from kicad_tools.sexp import SExp

    from boardproof import _self_check, raw_geometry

    uid = raw_geometry.direct_uuid(_fp(_root(pic), "P1"))
    group = SExp.list("group", write_layer.string("g"), SExp.list("uuid", write_layer.string("bbbbbbbb-0000-4000-8000-000000000001")),
                      SExp.list("members", write_layer.string(uid)))
    _edit_p1(pic, lambda root, fp: root.append(group))

    def groups():
        return [_self_check.node_text(c) for c in _root(pic).children if not c.is_atom and c.name == "group"]

    before = groups()
    assert len(before) == 1
    pa.write_poses(pic, {"P1": (90.0, 70.0, 0.0)})
    assert groups() == before and _snapshot(pic, "P1")[0] == [90.0, 70.0, 0.0]
