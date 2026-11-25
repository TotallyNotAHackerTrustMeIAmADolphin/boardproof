"""The layout of a schematic sheet the write layer writes is KiCad's own (GitHub issue 194).

A peer project saw large git diffs between a sheet our tools wrote and the same sheet saved by
KiCad (it normalised with ``kicad-cli sch upgrade --force`` after every change). Measured with
kicad-cli 10.0.7 on the installed demo sheets: the token stream is identical, only the line
breaking differs, and KiCad's layout is a pure function of the tokens (``upgrade --force`` of our
file gives KiCad's file byte for byte, and of KiCad's file gives the same file again). Four rules
describe it on 40 demo sheets without a miss: a list holding no list is one line, whatever its
length; any other list is a head line, one line per child and a closing parenthesis; consecutive
``(xy ..)`` children of ``(pts ..)`` share a line until the line, one character per tab of its indent
included, holds 99 or more (measured at indent depths 3 and 6: breaks at 96 and 93 characters of text); the strings of an image's ``(data ..)`` take a line each.

The oracle is KiCad: a demo sheet is saved by ``kicad-cli sch upgrade --force`` on a scratch copy
and our serialiser, fed the tokens KiCad wrote, must give the same text. The unit cases pin each
rule on small trees so that a mutant of one rule shows red.
"""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string

from boardproof import sheet_editing
from boardproof import write_layer as wl
from tests.test_write_layer import _copy_demo, _upgrade

DEMO_SHEETS = [
    "complex_hierarchy/ampli_ht.kicad_sch",  # symbols, wires, labels, pin graphics
    "interf_u/interf_u.kicad_sch",  # an image: a (data ..) of many strings
    "cm5_minima/HDMI.kicad_sch",  # polylines with long (pts ..) runs
]


def _kicad_saved(rel: str, tmp_path: Path) -> Path:
    """A scratch copy of demo ``rel`` saved by KiCad (``upgrade --force``), the file KiCad writes."""
    sheet = _copy_demo(rel, tmp_path / "demo")
    proc = _upgrade(sheet, "sch")
    assert proc.returncode == 0, (rel, proc.returncode, proc.stdout, proc.stderr)
    return sheet


def _lf(path_or_text: Path | str) -> str:
    text = path_or_text.read_bytes().decode("utf-8") if isinstance(path_or_text, Path) else path_or_text
    return text.replace("\r\n", "\n")


def _changed_lines(a: str, b: str) -> int:
    diff = difflib.unified_diff(a.split("\n"), b.split("\n"), lineterm="", n=0)
    return sum(1 for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---"))


# --------------------------------------------------------------------------- the KiCad oracle


@pytest.mark.parametrize("rel", DEMO_SHEETS, ids=[Path(r).name for r in DEMO_SHEETS])
def test_our_text_of_kicads_own_tokens_is_kicads_own_file(rel: str, tmp_path: Path) -> None:
    sheet = _kicad_saved(rel, tmp_path)
    ours = wl.serialise(wl.load(sheet)) + "\n"
    assert ours == _lf(sheet)


def test_a_real_tool_edit_changes_only_the_edited_lines_against_kicads_file(tmp_path: Path) -> None:
    """The peer's complaint: before this change a one-label move on a KiCad-saved ampli_ht sheet
    differed from KiCad's file in 3150 lines out of 6537 (measured); now only the label's own."""
    sheet = _kicad_saved("complex_hierarchy/ampli_ht.kicad_sch", tmp_path)
    before = _lf(sheet)
    result = sheet_editing.move("label", str(sheet), [{"texts": ["Vpil_0_3,3V"], "offset": [1.27, 0]}], None,
                                move_wires=False, snap=False, grid=1.27, allow_connectivity_change=True, dry_run=False)
    assert result["ok"] is True, result
    after = _lf(sheet)
    assert after != before
    assert _changed_lines(before, after) <= 6


def test_long_atom_only_list_stays_on_one_line_as_kicad_writes_it(tmp_path: Path) -> None:
    """A 200-character title is one line in KiCad's own save (measured), not wrapped at 100."""
    sheet = _kicad_saved("complex_hierarchy/ampli_ht.kicad_sch", tmp_path)
    root = wl.load(sheet)
    block = next(c for c in root.children if c.name == "title_block")
    wl.set_string(next(c for c in block.children if c.name == "title"), 0, "T" * 200)
    wl.save(root, sheet)
    proc = _upgrade(sheet, "sch")
    assert proc.returncode == 0
    assert wl.serialise(wl.load(sheet)) + "\n" == _lf(sheet)
    assert f'(title "{"T" * 200}")' in _lf(sheet)


def _long_polylines(root, points: int) -> None:
    """Append a sheet-level polyline (``(xy`` at depth 3) and one in the first library symbol's
    unit (depth 6), each of ``points`` points of varied width, through the tree API."""
    widths = ["(xy 10.5555 10)", "(xy 10 10)", "(xy -123.456 78.9)", "(xy 7.62 -0.254)", "(xy 1 2)"]
    run = " ".join(widths[i % len(widths)] for i in range(points))
    root.append(parse_string(f'(polyline (pts {run}) (stroke (width 0) (type default)) (uuid "00000000-0000-4000-8000-000000000001"))'))
    lib = next(c for c in root.children if c.name == "lib_symbols")
    unit = next(c for c in next(c for c in lib.children if c.name == "symbol").children if c.name == "symbol")
    unit.append(parse_string(f"(polyline (pts {run}) (stroke (width 0) (type default)) (fill (type none)))"))


@pytest.mark.parametrize("points", [40, 41, 77])
def test_long_polylines_at_depth_3_and_6_are_laid_out_as_kicad_does(points: int, tmp_path: Path) -> None:
    """Review of issue 194: the wrap threshold depends on the indent (depth 3 broke later than
    depth 6 in KiCad's save while the first version ignored the indent)."""
    sheet = _kicad_saved("complex_hierarchy/ampli_ht.kicad_sch", tmp_path)
    root = wl.load(sheet)
    _long_polylines(root, points)
    wl.save(root, sheet)
    proc = _upgrade(sheet, "sch")  # KiCad orders the items its own way, so compare its save with ours of its tokens
    assert proc.returncode == 0
    ours = wl.serialise(wl.load(sheet)) + chr(10)
    assert ours == _lf(sheet)
    assert sum(1 for ln in ours.split(chr(10)) if ln.lstrip(chr(9)).startswith("(xy")) > 6  # the runs did wrap


# --------------------------------------------------------------------------- the rules, pinned


def _sch(body: str) -> str:
    return wl.serialise(parse_string(f"(kicad_sch {body})"))


def test_a_list_holding_a_list_is_broken_even_when_short() -> None:
    assert _sch("(effects (font (size 1 1)))") == "\n".join([
        "(kicad_sch", "\t(effects", "\t\t(font", "\t\t\t(size 1 1)", "\t\t)", "\t)", ")"])


def test_a_list_of_atoms_only_is_one_line_whatever_its_length() -> None:
    text = _sch('(title "' + "x" * 300 + '")')
    assert text.split("\n")[1] == '\t(title "' + "x" * 300 + '")'


def test_head_atoms_stay_on_the_head_line_and_trailing_atoms_take_a_line_each() -> None:
    assert _sch('(property "Ref" "R1" (at 0 0) hide)') == "\n".join([
        "(kicad_sch", '\t(property "Ref" "R1"', "\t\t(at 0 0)", "\t\thide", "\t)", ")"])


def test_an_empty_list_is_written_closed() -> None:
    assert _sch("(power)").split("\n")[1] == "\t(power)"


def _pts(items: list[str]) -> list[str]:
    return _sch("(polyline (pts " + " ".join(items) + "))").split("\n")[3:-3]


def _xy_lines(wrappers: int, items: list[str]) -> list[str]:
    """The ``(xy`` lines of ``(pts ..)`` nested in ``wrappers`` lists below a sheet, so that the
    points sit at indent depth ``wrappers + 3``."""
    opening = "(a " * wrappers + "(polyline (pts " + " ".join(items) + ")" + ")" * (wrappers + 1)
    return [ln for ln in _sch(opening).split(chr(10)) if "(xy" in ln]


SEVEN = ["(xy 10 10)"] * 7  # 76 characters


def test_pts_wrap_when_indent_plus_line_reaches_99_characters_at_depth_3() -> None:
    at_98 = _xy_lines(0, SEVEN + ["(xy 10 1234567890)", "(xy 1 1)"])  # 3 + 76 + 1 + 18 = 98: next joins
    assert len(at_98) == 1 and at_98[0].count("(xy") == 9
    at_99 = _xy_lines(0, SEVEN + ["(xy 10 12345678901)", "(xy 1 1)"])  # 3 + 76 + 1 + 19 = 99: break after it
    assert len(at_99) == 2 and len(at_99[0]) == 99 and at_99[1].strip() == "(xy 1 1)"


def test_pts_wrap_threshold_is_the_same_total_at_depth_6() -> None:
    at_98 = _xy_lines(3, SEVEN + ["(xy 10 1234567)", "(xy 1 1)"])  # 6 + 76 + 1 + 15 = 98
    assert len(at_98) == 1 and at_98[0].count("(xy") == 9
    at_99 = _xy_lines(3, SEVEN + ["(xy 10 12345678)", "(xy 1 1)"])  # 6 + 76 + 1 + 16 = 99
    assert len(at_99) == 2 and len(at_99[0]) == 99


def test_pts_wrap_counts_each_tab_of_the_indent_as_one_character() -> None:
    eight = ["(xy 10 10)"] * 8 + ["(xy 1 1)"]  # 87 characters, then one more point
    assert len(_xy_lines(8, eight)) == 1  # depth 11: 11 + 87 = 98, the point joins
    assert len(_xy_lines(9, eight)) == 2  # depth 12: 12 + 87 = 99, break after the eighth


def test_pts_other_than_xy_children_are_not_grouped() -> None:
    text = _sch("(pts (xy 1 1) (foo 2) (xy 3 3))")
    assert text.split("\n")[2:5] == ["\t\t(xy 1 1)", "\t\t(foo 2)", "\t\t(xy 3 3)"]


def test_xy_outside_pts_are_not_grouped() -> None:
    text = _sch("(foo (xy 1 1) (xy 2 2) (bar 1))")
    assert text.split("\n")[2:5] == ["\t\t(xy 1 1)", "\t\t(xy 2 2)", "\t\t(bar 1)"]


def test_image_data_strings_take_a_line_each_below_the_head() -> None:
    assert _sch('(image (data "AAA" "BBB" "CCC"))') == "\n".join([
        "(kicad_sch", "\t(image", '\t\t(data "AAA"', '\t\t\t"BBB"', '\t\t\t"CCC"', "\t\t)", "\t)", ")"])
    assert _sch('(image (data "AAA"))').split("\n")[2] == '\t\t(data "AAA")'


def test_the_tokens_do_not_change_with_the_layout() -> None:
    tree = parse_string('(kicad_sch (a (pts (xy 1 1) (xy 2 2)) "s" 1.5) (image (data "A" "B")) (power))')
    assert wl.serialise(parse_string(wl.serialise(tree)), compact=True) == wl.serialise(tree, compact=True)


@pytest.mark.parametrize("root", ["kicad_symbol_lib", "footprint", "fp_lib_table"])
def test_other_file_kinds_keep_their_layout(root: str) -> None:
    """Only a sheet takes the new layout: a board, a library and a table are written as before."""
    text = wl.serialise(parse_string(f"({root} (effects (font 1 1)) (pts (xy 1 1) (xy 2 2)))"))
    assert text.split("\n")[1] == "\t(effects (font 1 1))"
    assert text.split("\n")[2] == "\t(pts (xy 1 1) (xy 2 2))"


def test_a_board_takes_the_sheet_layout() -> None:
    """A board is laid out as KiCad lays it out (issue 226; the demo round trip is in test_board_layout)."""
    text = wl.serialise(parse_string("(kicad_pcb (effects (font 1 1)))"))
    assert text.split("\n")[2] == "\t\t(font 1 1)"


def test_a_sub_tree_of_a_sheet_alone_keeps_the_old_layout() -> None:
    """Callers compare subtrees as text before and after; they are not given a root's layout."""
    assert wl.serialise(parse_string("(symbol (effects (font 1 1)))")).split("\n")[1] == "\t(effects (font 1 1))"
