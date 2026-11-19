"""Every reader that opens a board or schematic directly fails closed on a newer file (issue 213).

Issue 208 guarded ``write_layer.load`` and the raw geometry readers. A KiCad 11 nightly writes
footprint placement as ``(transform ...)``; a reader that opened the file through
``kicad_tools``' ``PCB.load`` or ``parse_file`` instead returned wrong positions without an error.
Here the committed rung-1 board and schematic get a version token above
``write_layer.NEWEST_MEASURED_VERSION`` (the typed setter, written with the fault helper, as in
tests/test_file_version_guard.py) and each reader is called on the copy: it must refuse with the
named ``unsupported_file_version``, either by raising it or in the tool result text, never return a
normal result and never a crash text.
"""
from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import board_rules, board_view, input_prep, rung_backends, schematic_import, sheet_tree, write_layer
from boardproof.routers import kicad_placer_tools
from boardproof.toolsets import placement as placement_toolset
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import routing as routing_toolset
from boardproof.toolsets import verification as verification_toolset
from tests.test_file_version_guard import BOARD, NEWEST, RUNG1, SHEET, with_version

RUNG1_PRO = RUNG1 / "rung1_usbc_attiny1616.kicad_pro"

CODE = "unsupported_file_version"


def _tools(*modules) -> dict:
    mcp = MCPServer("t")
    for module in modules:
        module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def tools() -> dict:
    return _tools(verification_toolset, routing_toolset, placement_toolset, project_toolset)


@pytest.fixture
def newer_board(tmp_path: Path) -> Path:
    return with_version(BOARD, tmp_path / "newer.kicad_pcb", NEWEST["kicad_pcb"] + 1)


@pytest.fixture
def newer_sheet(tmp_path: Path) -> Path:
    return with_version(SHEET, tmp_path / "newer.kicad_sch", NEWEST["kicad_sch"] + 1)


# name -> callable(board, sheet, tools, tmp_path)
BOARD_READERS = {
    "pcb_check_placement": lambda b, s, t, d: t["pcb_check_placement"](str(b)),
    "get_unrouted_nets": lambda b, s, t, d: t["get_unrouted_nets"](str(b)),
    "place_footprints": lambda b, s, t, d: t["place_footprints"](str(b), [{"reference": "R1", "x": 1.0, "y": 1.0}]),
    "kicad_placer_tools._raw_poses": lambda b, s, t, d: kicad_placer_tools._raw_poses(b),
    "input_prep.check_router_text_moves": lambda b, s, t, d: input_prep.check_router_text_moves(b),
    "input_prep.copper_texts": lambda b, s, t, d: input_prep.copper_texts(b),
    "input_prep.propose_input_preparation": lambda b, s, t, d: input_prep.propose_input_preparation(b, d.with_name(d.name + "_out")),
    "board_rules._board_net_names": lambda b, s, t, d: board_rules._board_net_names(b),
    "board_view.read_board_view": lambda b, s, t, d: board_view.read_board_view(b),
    "rung_backends.board_invariants": lambda b, s, t, d: rung_backends.board_invariants(b),
    "rung_backends.pour_zones": lambda b, s, t, d: rung_backends.pour_zones(b),
    "rung_backends._copper_texts": lambda b, s, t, d: rung_backends._copper_texts(b),
    "rung_backends.under_width_items": lambda b, s, t, d: rung_backends.under_width_items(b, Counter(), {}),
    "rung_backends.classify_necks": lambda b, s, t, d: rung_backends.classify_necks(b, [], {}),
    "schematic_import.propose_schematic_import": lambda b, s, t, d: schematic_import.propose_schematic_import(b, SHEET, d.with_name(d.name + "_out")),
    "placement._read_outline": lambda b, s, t, d: placement_toolset._read_outline(b),
    "placement._read_grid_origin": lambda b, s, t, d: placement_toolset._read_grid_origin(b),
    "project_open": lambda b, s, t, d: t["project_open"](str(b.parent / "x.kicad_pro")),
}
SHEET_READERS = {
    "schematic_import._parse": lambda s: schematic_import._parse(s),
    "sheet_tree._parse": lambda s: sheet_tree._parse(s),
    "sheet_tree.walk": lambda s: sheet_tree.walk(s),
}


def _refused(call) -> None:
    try:
        result = call()
    except write_layer.WriteLayerError as exc:
        assert exc.code == CODE, exc
        return
    text = json.dumps(result, default=str)
    assert CODE in text, f"returned a normal result instead of refusing: {text[:300]}"
    if isinstance(result, dict):
        assert result.get("status") not in ("complete", "ok", "proposed", "opened", "nothing_to_do"), text[:300]
        assert result.get("passed") is not True, text[:300]


@pytest.mark.parametrize("name", [n for n in BOARD_READERS if n != "project_open"])
def test_a_direct_board_reader_refuses_a_newer_board(name, newer_board, newer_sheet, tools, tmp_path) -> None:
    _refused(lambda: BOARD_READERS[name](newer_board, newer_sheet, tools, tmp_path))


@pytest.mark.parametrize("name", list(SHEET_READERS))
def test_a_direct_schematic_reader_refuses_a_newer_schematic(name, newer_sheet) -> None:
    _refused(lambda: SHEET_READERS[name](newer_sheet))


def test_project_open_refuses_a_newer_board_and_a_newer_schematic(tools, tmp_path) -> None:
    for kind, source, suffix in (("kicad_pcb", BOARD, ".kicad_pcb"), ("kicad_sch", SHEET, ".kicad_sch")):
        project = tmp_path / kind
        project.mkdir()
        shutil.copy(RUNG1_PRO, project / "p.kicad_pro")
        with_version(source, project / f"p{suffix}", NEWEST[kind] + 1)
        _refused(lambda project=project: tools["project_open"](str(project / "p.kicad_pro")))


def test_the_older_and_the_newest_measured_boards_still_read(tools, tmp_path) -> None:
    board = with_version(BOARD, tmp_path / "ok.kicad_pcb", NEWEST["kicad_pcb"])
    assert CODE not in json.dumps(tools["get_unrouted_nets"](str(board)), default=str)
    assert CODE not in json.dumps(tools["pcb_check_placement"](str(board)), default=str)
    assert board_rules._board_net_names(board)
    assert kicad_placer_tools._raw_poses(board)[0]


def test_place_footprints_names_the_newer_version_in_its_answer(tools, newer_board) -> None:
    result = tools["place_footprints"](str(newer_board), [{"reference": "R1", "x": 1.0, "y": 1.0}])
    assert result["status"] != "complete" and CODE in result["reason"], result
    assert str(NEWEST["kicad_pcb"] + 1) in result["reason"]


def test_place_footprints_passes_the_write_layer_code_through(tools, monkeypatch) -> None:
    """The lock-state read refuses with the write layer's own code, not ``lock_state_unreadable``."""
    def refuse(path, *a, **k):
        raise write_layer.UnsupportedFileVersionError(CODE, "newer", file=str(path))

    monkeypatch.setattr(write_layer, "load", refuse)
    result = tools["place_footprints"](str(BOARD), [{"reference": "R1", "x": 1.0, "y": 1.0}])
    assert result["status"] == "error" and result["code"] == CODE, result
    assert "nothing was moved" in result["reason"]


def test_an_unreadable_lock_state_that_is_not_a_write_layer_error_keeps_its_code(tools, tmp_path, monkeypatch) -> None:
    def boom(*a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(write_layer, "load", boom)
    result = tools["place_footprints"](str(BOARD), [{"reference": "R1", "x": 1.0, "y": 1.0}])
    assert result["status"] == "error" and result["code"] == "lock_state_unreadable", result


def test_the_model_readers_check_the_version_without_a_second_full_parse(monkeypatch, tmp_path) -> None:
    """``load_pcb`` and ``load_model`` read the version from the top-level blocks (the split of
    board_view), not from a parse of the whole file that the model then parses again (+9 s on a
    71 MB board). Only small version fragments go through ``write_layer.parse_string``."""
    from kicad_tools.schema import Schematic

    sizes: list[int] = []
    real = write_layer.parse_string

    def spy(text, *a, **k):
        sizes.append(len(text))
        return real(text, *a, **k)

    monkeypatch.setattr(write_layer, "parse_string", spy)
    for loader, source in ((write_layer.load_pcb, BOARD), (lambda p: write_layer.load_model(Schematic, p), SHEET)):
        sizes.clear()
        loader(source)
        assert sizes and max(sizes) < 200, ("a full parse before the model's own", sizes)
    newer = with_version(BOARD, tmp_path / "n.kicad_pcb", NEWEST["kicad_pcb"] + 1)
    with pytest.raises(write_layer.UnsupportedFileVersionError):
        write_layer.load_pcb(newer)


def test_verify_boards_annotation_reader_names_a_newer_schematic_as_a_problem(newer_sheet) -> None:
    """The annotation check of verify_board reads every sheet; a newer one is a named problem,
    so the check cannot come back as annotated."""
    from boardproof import verify_board

    problems = verify_board._annotation_problems(newer_sheet)
    assert problems and all(CODE in p for p in problems), problems
    assert str(NEWEST["kicad_sch"] + 1) in problems[0]


# --------------------------------------------------------------------------- duplicate versions on the text path


def _two_versions(source: Path, dest: Path) -> Path:
    """The committed file with an older first (version) and a second, much newer one."""
    root = write_layer.load(source)
    write_layer.set_number(next(c for c in root.children if not c.is_atom and c.name == "version"), 0, 20260101)
    root.append(write_layer.parse_string("(version 99999999)"))
    from tests import _kicad_faults as faults

    return faults.write_unchecked(root, dest)


def test_a_second_version_token_cannot_hide_behind_an_older_first_one(tmp_path) -> None:
    """Issue 208's rule on the cheap text path: every top-level (version) block counts."""
    from kicad_tools.schema import Schematic

    board = _two_versions(BOARD, tmp_path / "two.kicad_pcb")
    sheet = _two_versions(SHEET, tmp_path / "two.kicad_sch")
    with pytest.raises(write_layer.UnsupportedFileVersionError):
        write_layer.load_pcb(board)
    with pytest.raises(write_layer.UnsupportedFileVersionError):
        write_layer.load_model(Schematic, sheet)
    with pytest.raises(write_layer.UnsupportedFileVersionError):
        board_view.read_board_view(board)
    with pytest.raises(write_layer.UnsupportedFileVersionError):
        write_layer.load(board)


def _outcome(call) -> tuple:
    try:
        call()
    except write_layer.UnsupportedFileVersionError as exc:
        return ("refused", exc.details.get("version_found"))
    except Exception as exc:  # noqa: BLE001 - a parse failure of either path
        return ("error", type(exc).__name__)
    return ("accepted",)


TEXT_CASES = {
    "older": "(kicad_pcb (version 20240108) (generator x))",
    "newest measured": f"(kicad_pcb (version {NEWEST['kicad_pcb']}))",
    "newer": f"(kicad_pcb (version {NEWEST['kicad_pcb'] + 1}))",
    "missing": "(kicad_pcb (generator x))",
    "version only inside a footprint": '(kicad_pcb (generator x) (footprint "L:F" (version 99999999)))',
    "non numeric": '(kicad_pcb (version "soon"))',
    "bare word": "(kicad_pcb (version soon))",
    "duplicate, old first": "(kicad_pcb (version 20260101) (version 99999999))",
    "duplicate, new first": "(kicad_pcb (version 99999999) (version 20260101))",
    "duplicate, both old": "(kicad_pcb (version 20260101) (version 20250101))",
    "quoted text holding a version": '(kicad_pcb (version 20260101) (gr_text "(version 99999999)"))',
    "property holding a version": '(kicad_pcb (version 20260101) (footprint "L:F" (property "x" "(version 99999999)")))',
    "string with a paren": '(kicad_pcb (version 20260101) (gr_text "a ) b ( c"))',
    "bom": "\ufeff(kicad_pcb (version 99999999))",
    "bom, older": "\ufeff(kicad_pcb (version 20240108))",
    "crlf": "(kicad_pcb\r\n  (version 99999999)\r\n  (generator x)\r\n)",
    "crlf, older": "(kicad_pcb\r\n  (version 20240108)\r\n)",
    "schematic newer": f"(kicad_sch (version {NEWEST['kicad_sch'] + 1}))",
    "schematic duplicate": "(kicad_sch (version 20250101) (version 99999999))",
    "footprint root": '(footprint "X" (version 99999999) (generator "t"))',
    "symbol library root": "(kicad_symbol_lib (version 99999999))",
    "no version float": "(kicad_pcb (version 20260206.0))",
    "empty version": "(kicad_pcb (version))",
    "unbalanced": "(kicad_pcb (version 20240108)",
}


@pytest.mark.parametrize("name", list(TEXT_CASES))
def test_the_text_check_agrees_with_the_tree_check(name) -> None:
    text = TEXT_CASES[name]
    text_path = _outcome(lambda: write_layer.check_text_version(text))
    tree_path = _outcome(lambda: write_layer.check_file_version(write_layer.parse_string(text)))
    assert text_path == tree_path, (name, text_path, tree_path)


def test_the_text_cases_cover_both_kinds_of_answer() -> None:
    """Calibration: the differential cases are not all refusals or all acceptances."""
    kinds = {_outcome(lambda t=t: write_layer.check_text_version(t))[0] for t in TEXT_CASES.values()}
    assert {"refused", "accepted"} <= kinds
