"""Issue 271: the readability check boxes a local label as find_overlaps does since issue 266 (both justify words, stored angle
modulo 180), compared with kicad-cli svg over the same 36 cases (4 angles x 9 justify forms); the SVG fixture is the one of
tests/test_find_overlaps_label_justify.py. Also pinned against the SVG: the baseline lies 0.349 mm inside the box edge (the text margin;
measured 29.6507 / 30.9207 / 30.2857 for anchors at y 30 and a 1.27 mm font; turned 90 or 270 the same on the page after the svg's
rotate(-90) about the text pivot) and the box is as long as textLength within 0.25 mm
(the documented constant of the width estimate, 0.21)."""

from __future__ import annotations

import pytest

from boardproof import schematic_readability as readability
from boardproof import write_layer
from tests import test_find_overlaps_label_justify as j

sheet = j.sheet  # the module-scoped fixture of the issue 266 test, used here unchanged

NAMES = [f"L{a}J{k}" for a in j.ANGLES for k in range(len(j.JUSTIFY))]
MARGIN = 0.349


def _boxes(sheet):
    sch = sheet[3]
    texts = readability._read_sheet(write_layer.load(sch), "x").texts
    return {t.text: t.box for t in texts if t.kind == "label"}


@pytest.mark.parametrize("name", NAMES)
def test_readability_and_find_overlaps_box_the_label_alike(sheet, name):
    assert _boxes(sheet)[name] == pytest.approx(sheet[2][name])


@pytest.mark.parametrize("name", NAMES)
def test_the_readability_box_has_the_drawn_length_and_margin(sheet, name):
    vertical, sx, sy, _anchor, length, pivot_y = sheet[1][name]
    box = _boxes(sheet)[name]
    along = (box[3] - box[1]) if vertical else (box[2] - box[0])
    assert 0.0 <= length - along <= 0.25
    if vertical:  # the svg frame is rotate(-90) about (sx, pivot_y): the baseline lies on the page at x = sx + (sy - pivot_y)
        assert sx + (sy - pivot_y) == pytest.approx(box[2] - MARGIN, abs=0.01)
    else:
        assert sy == pytest.approx(box[3] - MARGIN, abs=0.01)
