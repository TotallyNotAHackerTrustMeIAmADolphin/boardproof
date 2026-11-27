"""Issue 272: the readability check boxes a global or hierarchical label as find_overlaps does (issue 225: shape and text as kicad-cli
sch export svg draws them; the svg comparison itself is tests/test_find_overlaps_pin_text.py). Same fixture: 2 kinds x 5 shapes x 4 angles,
justify left and right stored, and font size 1.27 and 2.54."""

from __future__ import annotations

import pytest

from boardproof import schematic_readability as readability
from boardproof import sheet_overlaps
from tests import test_find_overlaps_pin_text as p

label_sheet = p.label_sheet  # the module-scoped fixture of the issue 225 test, used here unchanged


@pytest.mark.parametrize("side", ["left", "right", "big"])
@pytest.mark.parametrize("kind", ["global", "hier"])
@pytest.mark.parametrize("shape", p.SHAPES)
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_readability_and_find_overlaps_box_a_shaped_label_alike(label_sheet, side, kind, shape, angle):
    files, meta, _tmp = label_sheet
    _sch, root = files[side]
    name = next(m[0] for m in meta if m[1:4] == (kind, shape, angle))
    want = next(t.box for t in sheet_overlaps._read(root)[1] if t.text == name)
    got = next(t.box for t in readability._read_sheet(root, "x").texts if t.kind == "label" and t.text == name)
    assert got == pytest.approx(want)


@pytest.mark.parametrize("side", ["left", "right", "big"])
@pytest.mark.parametrize("kind", ["global", "hier"])
@pytest.mark.parametrize("shape", p.SHAPES)
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_the_readability_box_of_a_shaped_label_is_its_shape_and_text_as_drawn(label_sheet, side, kind, shape, angle):
    """The svg assertion of tests/test_find_overlaps_pin_text.py, with the readability box as the source."""
    files, meta, tmp = label_sheet
    sch, root = files[side]
    name, _k, _s, _a, x, y = next(m for m in meta if m[1:4] == (kind, shape, angle))
    out = tmp / f"svg_{side}"
    svg = next(out.glob("*.svg")) if list(out.glob("*.svg")) else p._export(sch, out)
    size = 2.54 if side == "big" else 1.27
    drawn = p._drawn_union(svg, name, (x, y), size)
    got = next(t.box for t in readability._read_sheet(root, "x").texts if t.kind == "label" and t.text == name)
    horizontal = abs(drawn[2] - drawn[0]) > abs(drawn[3] - drawn[1])
    near, far, cross0, cross1 = (0, 2, 1, 3) if horizontal else (1, 3, 0, 2)
    slack = 0.28 * size
    assert abs(got[cross0] - drawn[cross0]) < 0.1 * size and abs(got[cross1] - drawn[cross1]) < 0.1 * size, (got, drawn)
    at_anchor = (x, y)[0 if horizontal else 1]
    if abs(drawn[near] - at_anchor) < 0.01:
        assert abs(got[near] - at_anchor) < 0.01 and abs(got[far] - drawn[far]) < slack, (got, drawn)
    else:
        assert abs(got[far] - at_anchor) < 0.01 and abs(got[near] - drawn[near]) < slack, (got, drawn)
