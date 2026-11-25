"""A board the write layer writes keeps KiCad 10's layout and adds nothing (GitHub issue 226).

Oracle: the installed pic_programmer demo (KiCad 10, version 20260206) is KiCad's own text.
"""

from __future__ import annotations

import difflib
from pathlib import Path

from kicad_tools.sexp import parse_string

from boardproof import write_layer as wl
from tests.test_write_layer import _copy_demo


def _lf(path: Path) -> str:
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def _changed(a: str, b: str) -> int:
    diff = difflib.unified_diff(a.split("\n"), b.split("\n"), lineterm="", n=0)
    return sum(1 for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---"))


def _demo(tmp_path: Path) -> Path:
    return _copy_demo("pic_programmer/pic_programmer.kicad_pcb", tmp_path)


def test_noop_write_is_byte_for_byte(tmp_path: Path) -> None:
    pcb = _demo(tmp_path)
    before = _lf(pcb)
    wl.save_pcb(wl.load_pcb(pcb), pcb)
    assert _lf(pcb) == before


def test_no_net_declarations_added_to_a_v10_board(tmp_path: Path) -> None:
    pcb = _demo(tmp_path)
    wl.save_pcb(wl.load_pcb(pcb), pcb)
    root = wl.load(pcb)
    assert not [c for c in root.children if not c.is_atom and c.name == "net"]


def test_one_footprint_move_changes_only_its_lines(tmp_path: Path) -> None:
    pcb = _demo(tmp_path)
    before = _lf(pcb)
    board = wl.load_pcb(pcb)
    fp = next(c for c in board._sexp.children if not c.is_atom and c.name == "footprint")
    at = next(c for c in fp.children if not c.is_atom and c.name == "at")
    wl.set_number(at, 0, at.values[0] + 1.5)
    wl.save_pcb(board, pcb)
    assert _changed(before, _lf(pcb)) == 2


def test_older_format_board_still_writes_with_its_declarations(tmp_path: Path) -> None:
    pcb = _copy_demo("ecc83/ecc83-pp.kicad_pcb", tmp_path)
    board = wl.load_pcb(pcb)
    wl.save_pcb(board, pcb)
    root = wl.load(pcb)
    assert [c for c in root.children if not c.is_atom and c.name == "net"]


# --------------------------------------------------------------------------- long atom lists


def _lines(text: str) -> list[str]:
    return wl.serialise(parse_string(text)).split("\n")


def test_pins_list_wraps_at_72_characters_and_not_at_71() -> None:
    # the line before the next atom holds 71 / 72 characters (a tab counts one): measured with kicad-cli 10.0.7
    for before, wraps in ((71, False), (72, True)):
        first = '"' + "a" * (before - 9) + '"'
        lines = _lines(f'(kicad_pcb (pins {first} "Z"))')
        if wraps:
            assert len(lines[1]) == before and lines[2:] == ['\t\t"Z"', "\t)", ")"]
        else:
            assert len(lines) == 3 and len(lines[1]) == before + 5 and lines[1].endswith('"Z")')


def test_group_members_wrap_two_uuids_to_a_line() -> None:
    ids = [f'"{i}' + "0" * 35 + '"' for i in range(1, 6)]
    for n, rows in ((1, 1), (2, 1), (3, 2), (5, 3)):
        lines = _lines(f"(kicad_pcb (members {' '.join(ids[:n])}))")
        assert len(lines) == (3 if n < 3 else 2 + rows + 1), (n, lines)
        if n >= 3:
            assert lines[1].count('"') == 4 and lines[-2] == "\t)"


def test_the_legacy_board_layout_is_the_one_before_issue_226(tmp_path: Path) -> None:
    """A board whose bytes are pinned (the staged rung-2 input, ``evals/rung2/stage_input.py``) is saved with
    ``legacy_board_layout=True``: the old one-tab layout with short lists on one line, whatever the new layout is."""
    pcb = _demo(tmp_path)
    root = wl.load(pcb)
    wl.save(root, pcb, legacy_board_layout=True)
    text = _lf(pcb)
    assert "\t(general (thickness 1.6) (legacy_teardrops no))\n" in text
    assert text == wl.serialise(wl.load(pcb), legacy_board_layout=True) + "\n"
    assert wl.serialise(root) != wl.serialise(root, legacy_board_layout=True)
