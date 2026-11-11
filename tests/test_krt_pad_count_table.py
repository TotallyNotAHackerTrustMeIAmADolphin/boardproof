"""The pad-count reproduction table in docs/ARCHITECTURE.md, checked by a test (issue 54).

The table (KRT checkout pinned to commit `c64c0db2`) was measured by hand in issue 1: per rung-1
board, the raw `(pad ...)` count per footprint against the count KRT's `parse_kicad_pcb` returns.
Here the raw columns and the parsed column of the pinned commit are measured again and compared
with the table's cells, so a doc edit or a parser regression fails a test. The KRT 0.23.1 column
cannot be measured without that checkout and is not tested.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from boardproof import raw_geometry, write_layer
from boardproof.routers import kicad_routing_tools as krt_tools

ROOT = Path(__file__).resolve().parents[1]
CURRENT_DIR = "evals/rung1/layout_run/07_poured"
OLD_DIR = "tests/fixtures/uw/r1krt/in"
CURRENT = ROOT / CURRENT_DIR / "rung1_usbc_attiny1616.kicad_pcb"
OLD_STAGE = ROOT / OLD_DIR / "rung1_usbc_attiny1616.kicad_pcb"
BOARDS = ((CURRENT_DIR, CURRENT), (OLD_DIR, OLD_STAGE))
ARCHITECTURE = ROOT / "docs/ARCHITECTURE.md"
needs_krt = pytest.mark.needs_krt

_ROW = re.compile(r"^\s*\|[^|]*`(?P<path>[^`]+)`[^|]*\|(?P<cells>(?:[^|]*\|){6})\s*$")


def _doc_rows() -> dict[str, list[int]]:
    """Rows of the 'Pad count per footprint' table, keyed by the board path in backticks; the six
    cells as ints (a bold `**0**` cell included): J1 raw, J1 parsed 0.23.1, J1 parsed c64c0db2,
    total raw, total parsed 0.23.1, total parsed c64c0db2."""
    rows = {}
    for line in ARCHITECTURE.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if m:
            cells = [c.strip().strip("*") for c in m["cells"].strip("|").split("|")]
            rows[m["path"]] = [int(c) for c in cells]
    return rows


def _bare_pad_names(pcb: Path) -> list[str]:
    """Footprint references with a pad whose name the raw file writes unquoted."""
    root = write_layer.load(pcb)
    bare = []
    for fp in root.children:
        if fp.is_atom or fp.name != "footprint":
            continue
        for pad in fp.children:
            if pad.is_atom or pad.name != "pad":
                continue
            if not write_layer.atom_text(pad.children[0]).startswith('"'):
                bare.append(raw_geometry._reference(fp))
    return bare


def test_the_doc_table_has_one_row_per_rung1_board():
    assert set(_doc_rows()) == {CURRENT_DIR, OLD_DIR}


def test_the_raw_columns_of_the_doc_table_match_the_boards():
    rows = _doc_rows()
    for board, pcb in BOARDS:
        raw = krt_tools.raw_pad_counts(pcb)
        j1_raw, _, _, total_raw, _, _ = rows[board]
        assert max(raw["J1"]) == j1_raw, board
        assert sum(sum(v) for v in raw.values()) == total_raw, board


def test_bare_pad_names_are_only_in_the_old_stage_and_only_in_j1():
    assert _bare_pad_names(CURRENT) == []
    old = _bare_pad_names(OLD_STAGE)
    assert len(old) == 10 and set(old) == {"J1"}


@needs_krt
def test_the_pinned_commit_reads_every_pad_on_both_boards_as_the_doc_table_says(krt_checkout):
    assert krt_tools.router_commit(krt_checkout) == krt_tools.load_cli_snapshot()["router_commit"]
    rows = _doc_rows()
    for board, pcb in BOARDS:
        raw = krt_tools.raw_pad_counts(pcb)
        parsed = krt_tools.parsed_pad_counts(pcb)
        _, _, j1_parsed, _, _, total_parsed = rows[board]
        assert max(parsed["J1"]) == j1_parsed, board
        assert sum(sum(v) for v in parsed.values()) == total_parsed, board
        assert parsed == raw, board  # every footprint, not only J1
        assert krt_tools.krt_parse_gap(raw, parsed) == []


def test_the_table_columns_name_the_pinned_commit():
    """Issue 64: the pin moved to v0.23.8 (c64c0db2); the table's values were re-measured unchanged."""
    header = next(
        line for line in ARCHITECTURE.read_text(encoding="utf-8").splitlines() if line.lstrip().startswith("| Board |")
    )
    assert "KRT c64c0db2" in header and "299ad570" not in header
    assert "total parsed, c64c0db2" in header.lower() or "Total parsed, c64c0db2" in header
