"""kicad_tools facts that docs/ARCHITECTURE.md states per version (build ticket 71).

Each test pins what the installed ``kicad_tools`` does, keyed by its version, so an upgrade that
changes a fact turns a test red instead of leaving the known facts stale. Boards are copies of
KiCad's demos (``kicad_demo``); facts are read back from the raw S-expression through
``kicad_mcp.write_layer.load`` (hard rule 2), never from the library's properties.

None of this licenses a high-level save in ``src/``: the write layer stays the only save path
(docs/ARCHITECTURE.md decision 1), whatever these tests show ``PCB.save`` doing.
"""

from __future__ import annotations

import sys
from pathlib import Path

import kicad_tools
import pytest

from kicad_mcp import write_layer as wl
from tests import _kicad_faults as faults

VERSION = tuple(int(p) for p in kicad_tools.__version__.split(".")[:2])


def _board(kicad_demo) -> Path:
    return kicad_demo("pic_programmer") / "pic_programmer.kicad_pcb"


def _raw_footprint(root, ref: str):
    for fp in root.find_all("footprint"):
        if any(
            p.get_string(0) == "Reference" and p.get_string(1) == ref
            for p in fp.find_all("property")
        ):
            return fp
    raise KeyError(ref)


def _raw_pad_size(board: Path, ref: str, index: int) -> list:
    pad = _raw_footprint(wl.load(board), ref).find_all("pad")[index]
    return [c.value for c in pad.find_child("size").children]


def test_pad_size_assignment_reaches_the_saved_tree_from_0_24(kicad_demo) -> None:
    """``Pad.__setattr__`` mirrored only ``rotation`` up to 0.23.0, so ``pad.size = ...`` was
    lost on save (#7479). 0.24.0 writes size, drill and shape through to the tree (PR #7499).
    Saved here through the write layer, so the test shows the tree, not ``PCB.save``."""
    from kicad_tools.schema import PCB

    board = _board(kicad_demo)
    assert _raw_pad_size(board, "C3", 0) == [1.6, 1.6]  # the premise, from the raw file
    pcb = PCB.load(board)
    pad = next(f for f in pcb.footprints if f.reference == "C3").pads[0]
    pad.size = (2.345, 1.234)
    wl.save_pcb(pcb, board)

    expected = [2.345, 1.234] if VERSION >= (0, 24) else [1.6, 1.6]
    assert _raw_pad_size(board, "C3", 0) == expected


def test_pcb_save_refuses_an_edit_it_would_drop_and_writes_nothing(kicad_demo) -> None:
    """0.24.0's ``PCB.save`` raises ``ValueError`` naming each ``ref/pad.field`` it cannot
    persist (here ``Pad.number``) and writes nothing (PR #7499, #7524). Before 0.24 it dropped
    the edit without a word, or (0.22, 0.23 on Windows) failed with ``EBADF`` (#7478)."""
    from kicad_tools.schema import PCB

    board = _board(kicad_demo)
    before = board.read_bytes()
    pcb = PCB.load(board)
    next(f for f in pcb.footprints if f.reference == "C3").pads[0].number = "99"

    if VERSION >= (0, 24):
        with pytest.raises(ValueError, match=r"C3/pad 1\.number"):
            pcb.save(board)
        assert board.read_bytes() == before
    elif sys.platform == "win32":
        with pytest.raises(OSError) as raised:
            pcb.save(board)
        assert raised.value.errno == 9
        assert board.read_bytes() == before
    else:
        pcb.save(board)  # the edit is dropped without a word
        assert (
            _raw_footprint(wl.load(board), "C3").find_all("pad")[0].get_string(0) == "1"
        )


@pytest.mark.skipif(
    sys.platform != "win32", reason="line endings differ by platform only on Windows"
)
def test_pcb_save_writes_lf_on_windows(kicad_demo) -> None:
    """``atomic_write`` (0.22.0+) writes bytes, so ``PCB.save`` writes LF on Windows, unlike
    KiCad (CRLF) and the write layer (the platform's line ending). Build ticket 51's count of
    CRLF in a ``PCB.save`` output was taken on 0.20.x, before ``atomic_write``. Compare boards
    by content sha. 0.22.0 and 0.23.0 fail on Windows before the rename (#7478) and leave the
    written bytes in ``<board>.tmp``, which is LF as well."""
    from kicad_tools.schema import PCB

    board = _board(kicad_demo)
    assert b"\r\n" in board.read_bytes()  # the Windows install ships CRLF demos
    if VERSION >= (0, 24):
        PCB.load(board).save(board)
        data = board.read_bytes()
    else:
        with pytest.raises(OSError):
            PCB.load(board).save(board)
        (leftover,) = [p for p in board.parent.iterdir() if p.name.endswith(".tmp")]
        data = leftover.read_bytes()
    assert b"\r\n" not in data and data.count(b"\n") > 1000


def test_dsn_export_to_a_path_writes_a_revision_sidecar_beside_the_board_from_0_24(
    kicad_demo, tmp_path: Path
) -> None:
    """0.24.0's ``KiCadToDSNExporter.export(path)`` also writes ``<board>.dsn-revision.json``
    next to the BOARD (not the DSN), for its SES importer's stale-board check (#6915).
    ``export()`` without a path returns the text and writes nothing. A tool that exports from
    the user's board with a path therefore writes into the user's project directory."""
    from kicad_tools.export.dsn import KiCadToDSNExporter

    board = _board(kicad_demo)
    out = tmp_path / "elsewhere"
    project_before = sorted(p.name for p in board.parent.iterdir())

    text = KiCadToDSNExporter(board).export()
    assert text.lstrip().startswith("(pcb")
    assert sorted(p.name for p in board.parent.iterdir()) == project_before

    KiCadToDSNExporter(board).export(out / "board.dsn")
    added = sorted(set(p.name for p in board.parent.iterdir()) - set(project_before))
    assert added == (["pic_programmer.dsn-revision.json"] if VERSION >= (0, 24) else [])
    assert sorted(p.name for p in out.iterdir()) == ["board.dsn"]


def test_carriage_returns_in_strings_by_version(tmp_path: Path) -> None:
    r"""Up to 0.24 ``SExp.to_string`` wrote ``\r`` raw and ``parse_file`` read a raw CR as LF.
    0.25.0 escapes it (``\r``, the way KiCad does) and reads a raw CR as it is (its file reads
    keep newlines). The write layer's own serialiser and ``load`` give the same result on both,
    so none of this changes a byte the layer writes (build ticket 6)."""
    from kicad_tools.sexp import Document, SExp, parse_file, parse_string

    node = SExp.list("x", "A\rB")
    raw_cr = tmp_path / "raw.kicad_sym"
    doc = Document(parse_string("(kicad_symbol_lib (version 20251024))"), raw_cr)
    doc.root.children.append(node)
    doc.save(raw_cr)  # kicad_tools' own writer builds the fixture
    # the raw-CR file (0.20 to 0.24 wrote it, KiCad tolerates it) is made on every version
    faults.rewrite_bytes(raw_cr, lambda data: data.replace(b'"A\\rB"', b'"A\rB"'))
    assert b'"A\rB"' in raw_cr.read_bytes()

    if VERSION >= (0, 25):
        assert node.to_string(compact=True) == '(x "A\\rB")'
        assert parse_file(raw_cr).children[1].children[0].value == "A\rB"
    else:
        assert node.to_string(compact=True) == '(x "A\rB")'
        assert parse_file(raw_cr).children[1].children[0].value == "A\nB"

    assert wl.serialise(node, compact=True) == '(x "A\\rB")'
    assert wl.load(raw_cr).children[1].children[0].value == "A\rB"
    assert parse_string('(x "A\\rB")').children[0].value == "A\rB"
