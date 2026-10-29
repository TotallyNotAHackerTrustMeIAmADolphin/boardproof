"""The sanctioned fault-injection helper for tests (build ticket 74, ``tests/_kicad_faults.py``).

Each helper must change exactly the one thing it names, and nothing else, or a test that uses
it proves a fault it did not isolate. The oracle is the write layer's own save of the same tree
(``write_layer.save``, parse-checked by ``kicad-cli``) and its reader.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from kicad_mcp import write_layer as wl
from tests import _kicad_faults as faults

FIXTURES = Path(__file__).parent / "fixtures"


def _board(tmp_path: Path) -> Path:
    src = tmp_path / "board.kicad_pcb"
    shutil.copyfile(FIXTURES / "simple_led.kicad_pcb", src)
    return src


def _first(root, name: str):
    for node in root.find_all(name):
        return node
    raise AssertionError(f"no ({name} ...) in the tree")


def test_write_unchecked_writes_the_bytes_the_layer_saves(tmp_path: Path) -> None:
    src = _board(tmp_path)
    saved = wl.save(wl.load(src), tmp_path / "saved.kicad_pcb")
    unchecked = faults.write_unchecked(wl.load(src), tmp_path / "unchecked.kicad_pcb")
    assert unchecked.read_bytes() == saved.read_bytes()


def test_raw_atom_lands_verbatim_and_nothing_else_changes(tmp_path: Path) -> None:
    src = _board(tmp_path)
    saved = wl.save(wl.load(src), tmp_path / "saved.kicad_pcb").read_bytes()
    root = wl.load(src)
    net = _first(root, "net")  # (net 0 "") in a KiCad 10 board: the first net's name
    name = net.children[-1].value
    assert saved.count(wl.quote(name).encode()) >= 1
    net.children[-1] = faults.raw_atom('"line\nbreak"')
    written = faults.write_unchecked(root, tmp_path / "fault.kicad_pcb").read_bytes()
    head, _, tail = saved.partition(f"(net {net.children[0].value} {wl.quote(name)})".encode())
    assert tail, "the first net is written on one line"
    assert written == head + f'(net {net.children[0].value} "line\nbreak")'.encode() + tail
    assert b'"line\r\nbreak"' not in written  # the raw LF is not turned into the file's CRLF


def test_write_unchecked_writes_what_the_layer_refuses(tmp_path: Path) -> None:
    root = wl.load(_board(tmp_path))
    root.append(SExp.list("kicad_mcp_unknown_token"))
    with pytest.raises(wl.WriteLayerError):
        wl.save(root, tmp_path / "refused.kicad_pcb")
    assert not (tmp_path / "refused.kicad_pcb").exists()
    written = faults.write_unchecked(root, tmp_path / "fault.kicad_pcb")
    assert b"(kicad_mcp_unknown_token)" in written.read_bytes()


def test_drift_changes_the_bytes_and_keeps_the_tokens(tmp_path: Path) -> None:
    src = _board(tmp_path)
    before_bytes, before_tree = src.read_bytes(), wl.serialise(wl.load(src))
    faults.drift(src)
    assert src.read_bytes() != before_bytes
    assert src.read_bytes().startswith(before_bytes)
    assert wl.serialise(wl.load(src)) == before_tree


def test_rewrite_bytes_writes_exactly_the_changed_bytes(tmp_path: Path) -> None:
    src = _board(tmp_path)
    before = src.read_bytes()
    seen: list[bytes] = []

    def change(data: bytes) -> bytes:
        seen.append(data)
        return data.replace(b"(", b"(\r", 1)

    assert faults.rewrite_bytes(src, change) == src
    assert seen == [before]  # the change sees the file's bytes, unconverted
    assert src.read_bytes() == before.replace(b"(", b"(\r", 1)  # a lone CR stays a lone CR
