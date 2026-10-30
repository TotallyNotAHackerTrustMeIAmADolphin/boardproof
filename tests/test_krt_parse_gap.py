"""KRT_PARSE_GAP: a footprint whose pads KRT's parser read fewer of than the raw file holds (issue 2).

Background: with unquoted pad names an old KRT parser read 0 pads for J1, skipped the nets as
"already routed" and reported ``failed_nets []``. Current KRT reads them (issue 1), so this is a
guard against an old checkout or a future parser gap. It only reports; it never edits the board.
"""

from __future__ import annotations

import hashlib
import re
import shutil
from pathlib import Path

import pytest

from kicad_mcp import rung_backends, write_layer
from kicad_mcp.routers import kicad_routing_tools as krt_tools

FIXTURES = Path(__file__).parent / "fixtures"
needs_krt = pytest.mark.needs_krt
KRT_BASELINE = {"escalation": "board", "grid_step_mm": None, "max_iterations": None, "ordering": None}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def stress_board(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        shutil.copy(FIXTURES / f"krt_stress_congestion{suffix}", project)
    return project / "krt_stress_congestion.kicad_pcb"


@pytest.fixture
def bare_board(stress_board):
    """The stress board with every pad name rewritten bare (A1, A2, ...) through the write layer."""
    root = write_layer.load(stress_board)
    for fp in [c for c in root.children if not c.is_atom and c.name == "footprint"]:
        for n, pad in enumerate([c for c in fp.children if not c.is_atom and c.name == "pad"], 1):
            write_layer.set_symbol(pad, 0, f"A{n}")
    write_layer.save(root, stress_board)
    return stress_board


# --- the pure comparison ---------------------------------------------------------------


def test_a_parsed_count_below_the_raw_count_is_a_named_gap():
    gaps = krt_tools.krt_parse_gap({"J1": [4], "R1": [2]}, {"J1": [0], "R1": [2]})
    assert gaps == [{"code": "KRT_PARSE_GAP", "reference": "J1", "raw_pads": 4, "parsed_pads": 0}]


def test_a_footprint_krt_did_not_return_at_all_is_a_gap_of_zero_pads():
    gaps = krt_tools.krt_parse_gap({"J1": [4]}, {})
    assert gaps == [{"code": "KRT_PARSE_GAP", "reference": "J1", "raw_pads": 4, "parsed_pads": 0}]


def test_equal_counts_and_a_footprint_with_no_pads_are_no_gap():
    assert krt_tools.krt_parse_gap({"J1": [4], "H1": [0]}, {"J1": [4], "H1": [0]}) == []


def test_two_footprints_with_one_reference_are_compared_one_to_one():
    gaps = krt_tools.krt_parse_gap({"U1": [8, 2]}, {"U1": [8, 1]})
    assert gaps == [{"code": "KRT_PARSE_GAP", "reference": "U1", "raw_pads": 2, "parsed_pads": 1}]


# --- the raw count, from the file's own S-expressions -----------------------------------------


def test_raw_pad_counts_read_bare_and_quoted_pad_names_alike(stress_board, bare_board):
    quoted = FIXTURES / "krt_stress_congestion.kicad_pcb"
    counts_bare = krt_tools.raw_pad_counts(bare_board)
    counts_quoted = krt_tools.raw_pad_counts(quoted)
    assert counts_bare == counts_quoted and counts_bare
    # Cross-check against the raw text: every `(pad` opener is counted exactly once.
    text = bare_board.read_text(encoding="utf-8")
    assert sum(sum(v) for v in counts_bare.values()) == len(re.findall(r"\(pad\s", text))
    assert re.search(r'\(pad\s+A1\s', text) and not re.search(r'\(pad\s+"', text)  # the scratch copy really has bare names


# --- reproduction against the real parser (recorded in the issue's Answer) ---------------------


@needs_krt
def test_reproduction_raw_against_parsed_pad_counts_on_bare_names(bare_board):
    raw = krt_tools.raw_pad_counts(bare_board)
    parsed = krt_tools.parsed_pad_counts(bare_board)
    print("per footprint (raw, parsed):", {r: (raw[r], parsed.get(r)) for r in sorted(raw)})
    assert set(parsed) <= set(raw)
    # The guard agrees with a direct comparison, whatever the checkout's parser does.
    expect = [r for r in raw if sum(parsed.get(r, [])) < sum(raw[r])]
    assert sorted(g["reference"] for g in krt_tools.krt_parse_gap(raw, parsed)) == sorted(expect)


# --- the adapter reports the gap and never edits the board -----------------------------------------


@needs_krt
def test_the_krt_adapter_reports_a_parse_gap_instead_of_a_silent_skip(stress_board, tmp_path, monkeypatch):
    real = krt_tools.parsed_pad_counts(stress_board)
    victim = sorted(real)[0]
    assert real[victim] and sum(real[victim]) > 0
    monkeypatch.setattr(krt_tools, "parsed_pad_counts",
                        lambda pcb: {**krt_tools.raw_pad_counts(pcb), victim: [0]})
    (tmp_path / "a").mkdir()
    outcome = rung_backends.krt_backend()(stress_board, dict(KRT_BASELINE), 120.0, tmp_path / "a" / "router.log")
    gaps = outcome["facts"]["krt_parse_gap"]
    assert [g["reference"] for g in gaps] == [victim]
    assert gaps[0]["code"] == "KRT_PARSE_GAP" and gaps[0]["parsed_pads"] == 0


@needs_krt
def test_no_gap_on_a_board_krt_reads_in_full(stress_board, tmp_path):
    (tmp_path / "a").mkdir()
    outcome = rung_backends.krt_backend()(stress_board, dict(KRT_BASELINE), 120.0, tmp_path / "a" / "router.log")
    assert outcome["facts"]["krt_parse_gap"] == []
    assert outcome["facts"].get("krt_parse_gap_check_error") is None


@needs_krt
def test_a_check_that_cannot_run_is_reported_not_silent(stress_board, tmp_path, monkeypatch):
    def boom(pcb):
        raise krt_tools.KicadRoutingToolsError("probe exploded")

    monkeypatch.setattr(krt_tools, "parsed_pad_counts", boom)
    (tmp_path / "a").mkdir()
    outcome = rung_backends.krt_backend()(stress_board, dict(KRT_BASELINE), 120.0, tmp_path / "a" / "router.log")
    assert "probe exploded" in outcome["facts"]["krt_parse_gap_check_error"]
    assert outcome["status"] == "ok"  # report-only: the attempt still ran


def test_the_check_itself_never_edits_the_board(bare_board, monkeypatch):
    monkeypatch.setattr(krt_tools, "parsed_pad_counts", lambda pcb: {})  # every footprint a gap
    before = _sha(bare_board)
    krt_tools.raw_pad_counts(bare_board)
    facts = rung_backends.parse_gap_facts(bare_board)
    assert facts["krt_parse_gap"]  # it did report
    assert _sha(bare_board) == before
