"""Placement hull of custom pads and per-reference reading (issue 233, follow-up of 228)."""

from __future__ import annotations

import math
from pathlib import Path

import pytest
from kicad_tools.schema import PCB
from kicad_tools.sexp import SExp

from boardproof import placement_anchor, write_layer
from tests.test_courtyard_hull import _fns, _raw_xs, c1206


def _board(tmp_path: Path) -> str:
    fp = c1206(tmp_path / "C_1206.kicad_mod")
    board = PCB.create(width=100.0, height=100.0)
    for ref, x in (("C1", 10.0), ("C2", 20.0), ("C3", 40.0)):
        board.add_footprint_from_file(fp, ref, x, 10.0)
    path = tmp_path / "board.kicad_pcb"
    write_layer.save_pcb(board, path)
    return str(path)


def _footprint(root: SExp, ref: str) -> SExp:
    for fp in (c for c in root.children if not c.is_atom and c.name == "footprint"):
        if any(p.get_string(0) == "Reference" and p.get_string(1) == ref for p in fp.find_all("property")):
            return fp
    raise AssertionError(ref)


def _make_custom(path: str, ref: str, primitive: str) -> None:
    """Turn pad 2 of ``ref`` into a custom pad carrying ``primitive`` (a test-local board)."""
    root = write_layer.load(path)
    pad = _footprint(root, ref).find_all("pad")[1]
    pad.children[2] = write_layer.symbol("custom")
    pad.children.append(write_layer.parse_string(f"(primitives {primitive})"))
    write_layer.save(root, path)


POLY = "(gr_poly (pts (xy 0 0) (xy 3 0) (xy 3 1)) (width 0) (fill yes))"


def test_hull_covers_the_primitives_of_a_custom_pad(tmp_path: Path) -> None:
    path = _board(tmp_path)
    _make_custom(path, "C2", POLY)
    part = next(p for p in placement_anchor.read_parts(write_layer.load(path), {"C2"}))
    # pad 2 sits 1.585 right of the origin (page frame, as stored); its polygon reaches 3 mm further
    assert part.box[2] == pytest.approx(part.at[0] + 1.585 + 3.0, abs=1e-6)
    moved = placement_anchor.box_at(part, (30.0, 10.0), part.angle)
    assert moved[2] == pytest.approx(30 + 1.585 + 3.0, abs=1e-6)  # box_at takes a page position


def test_pack_spaces_by_the_custom_pad_reach(tmp_path: Path) -> None:
    path = _board(tmp_path)
    _make_custom(path, "C1", POLY)
    assert _fns()["pack_footprints_densely"](path, ["C1", "C2"], min_clearance_mm=0.2)["status"] in ("ok", "partial")
    xs = _raw_xs(path)
    # C1's reach to the right is 1.585 + 3.0 beyond its origin, C2's hull starts 2.335 left of its origin
    assert xs["C2"] - xs["C1"] == pytest.approx(1.585 + 3.0 + 2.335 + 0.2, abs=1e-3)


def test_an_unmeasurable_custom_pad_is_refused_for_its_own_reference(tmp_path: Path) -> None:
    path = _board(tmp_path)
    root = write_layer.load(path)  # in memory only: KiCad would not load this pad
    pad = _footprint(root, "C2").find_all("pad")[1]
    pad.children[2] = write_layer.symbol("custom")
    pad.children.append(write_layer.parse_string("(primitives (gr_text (at 0 0)))"))
    with pytest.raises(placement_anchor.RawGeometryError, match="not measured"):
        placement_anchor.read_parts(root, {"C2"})
    assert [p.reference for p in placement_anchor.read_parts(root, {"C1"})] == ["C1"]


def test_an_unmeasurable_footprint_does_not_refuse_unrelated_references(tmp_path: Path) -> None:
    path = _board(tmp_path)
    root = write_layer.load(path)
    for pad in _footprint(root, "C3").find_all("pad"):  # C3: a pad without (size) cannot be measured
        pad.children.remove(pad.find("size"))
    write_layer.save(root, path)
    parts = placement_anchor.read_parts(write_layer.load(path), {"C1", "C2"})
    assert [p.reference for p in parts] == ["C1", "C2"]
    assert _fns()["align_footprints"](path, ["C1", "C2"], "y", "min")["status"] == "ok"
    refused = _fns()["align_footprints"](path, ["C2", "C3"], "y", "min")
    assert refused["code"] == "footprint_unmeasurable"


def _rot(origin: tuple[float, float], angle_deg: float, local: tuple[float, float]) -> tuple[float, float]:
    """KiCad's placement on a y-down page, written out independently of raw_geometry."""
    a = math.radians(angle_deg)
    return (origin[0] + local[0] * math.cos(a) + local[1] * math.sin(a),
            origin[1] - local[0] * math.sin(a) + local[1] * math.cos(a))  # fmt: skip


@pytest.mark.parametrize(("layer", "fp_angle", "pad_angle", "new_angle"), [
    ("F.Cu", 30.0, 50.0, 90.0),
    ("B.Cu", 90.0, 90.0, 0.0),
    ("B.Cu", 90.0, 135.0, 30.0),
])  # fmt: skip
def test_box_at_turns_the_primitive_box_with_the_pad(layer: str, fp_angle: float, pad_angle: float, new_angle: float) -> None:
    text = (
        f'(kicad_pcb (version 20241229) (footprint "X" (layer "{layer}") (at 50 40 {fp_angle:g}) '
        '(property "Reference" "U1" (at 0 0) (layer "F.SilkS")) '
        f'(pad "1" smd custom (at 1.585 0 {pad_angle:g}) (size 1.5 1.73) (layers "{layer}") '
        f"(primitives {POLY}))))"
    )
    part = placement_anchor.read_parts(write_layer.parse_string(text), {"U1"})[0]
    new_at = (70.0, 20.0)
    got = placement_anchor.box_at(part, new_at, new_angle)
    centre = _rot(new_at, new_angle, (1.585, 0.0))  # the pad's footprint-local place, moved with the footprint
    turned = pad_angle + (new_angle - fp_angle)  # pad angles are board-absolute: they turn by the difference
    pts = [_rot(centre, turned, c) for box in ((-0.75, -0.865, 0.75, 0.865), (0.0, 0.0, 3.0, 1.0))
           for c in ((box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3]))]  # fmt: skip
    want = (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))
    assert got == pytest.approx(want, abs=1e-6)
