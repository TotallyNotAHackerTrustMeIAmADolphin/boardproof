"""The checks of the board writers on a KiCad 10 board, which is saved without net declarations (issue 226).

kicad_tools' loader synthesises ``(net N "name")`` nodes into the tree of a KiCad 10 board; ``save`` does not write
them. Every write check that compared the reload with the tree in memory counted them and refused the write
(``confirm_board_outline`` failed with ``write_not_verified`` on the pic_programmer demo). The fixtures of the
other tests are older formats and never showed it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string

from boardproof import _self_check, board_outline, input_prep, pour_step, write_layer
from boardproof import verify_board as gate
from tests.test_pour_step import make_fake_router
from tests.test_write_layer import _copy_demo

DEMO = "pic_programmer/pic_programmer.kicad_pcb"
RECT = {"x": 73.66, "y": 40.64, "width": 160.02, "height": 99.06}  # the demo's Edge.Cuts, read from its raw tokens
WHOLE_BOARD = [(74.0, 41.0), (233.0, 41.0), (233.0, 139.0), (74.0, 139.0)]


def _board(tmp_path: Path) -> Path:
    return _copy_demo(DEMO, tmp_path / "proj")


def test_confirm_board_outline_builds_a_revision_of_a_v10_board(tmp_path: Path) -> None:
    pcb = _board(tmp_path)
    proposal = board_outline.propose_board_outline(pcb, RECT, output_dir=tmp_path / "out")
    assert proposal["status"] == "proposed", proposal
    done = board_outline.confirm_board_outline(pcb, proposal["revision_token"])
    assert done["status"] == "built", done


def test_input_preparation_and_pour_step_confirm_on_a_v10_board(tmp_path: Path) -> None:
    pcb = _board(tmp_path)
    prep = input_prep.propose_input_preparation(pcb, output_dir=tmp_path / "prep")
    assert prep["status"] == "proposed", prep
    prepared = input_prep.confirm_input_preparation(
        pcb, prep["revision_token"], parity="not_applicable", parity_reason="scratch copy, parity not checked here"
    )
    assert prepared["status"] == "prepared", prepared
    board = Path(prepared["output_pcb"])
    board.with_suffix(".kicad_sch").unlink(missing_ok=True)  # parity 'not_applicable' is refused beside a schematic
    baseline = gate.snapshot_rules(board)
    proposal = pour_step.propose_pour_step(board, pours={"GND": "F.Cu"}, output_dir=tmp_path / "pour")
    assert proposal["status"] == "proposed", proposal
    router = make_fake_router(
        zones=[{"net_id": 0, "net": "GND", "net_text": '(net "GND")', "layer": "F.Cu", "polygon": WHOLE_BOARD}]
    )
    done = pour_step.confirm_pour_step(
        board, proposal["revision_token"], baseline=baseline, router=router,
        parity="not_applicable", parity_reason="scratch copy, no schematic", timeout=300,
    )
    assert done["status"] == "poured", done


def test_the_schematic_import_confirm_on_a_v10_board_is_the_kicad10_test() -> None:
    """``tests/test_schematic_import.py -k kicad10`` covers the import confirm (it failed with ``write_not_verified``
    before ``saved_tag_counts`` and the pad-name read); this only pins that the test exists."""
    text = Path(__file__).with_name("test_schematic_import.py").read_text(encoding="utf-8")
    assert "def test_kicad10_confirm_writes_a_board_kicad_loads_with_the_new_nets" in text


# --------------------------------------------------------------------------- the self-check on a v10 board


def _trees(tmp_path: Path):
    pcb = _board(tmp_path)
    memory = write_layer.load_pcb(pcb)._sexp  # carries the synthesised declarations
    reloaded = write_layer.load(pcb)  # as saved: none
    return memory, reloaded


def test_the_self_check_ignores_only_the_unsaved_declarations_of_a_v10_board(tmp_path: Path) -> None:
    memory, reloaded = _trees(tmp_path)
    assert any(write_layer.is_net_declaration(c) for c in memory.children)
    assert not any(write_layer.is_net_declaration(c) for c in reloaded.children)
    diff = _self_check.reload_differences(source=reloaded, removed=[], written=[], memory=memory, reloaded=reloaded)
    assert diff["missing"] == [] and diff["unexpected"] == []


def test_the_self_check_still_catches_a_dropped_footprint_on_a_v10_board(tmp_path: Path) -> None:
    memory, reloaded = _trees(tmp_path)
    reloaded.children.remove(next(c for c in reloaded.children if not c.is_atom and c.name == "footprint"))
    diff = _self_check.reload_differences(source=memory, removed=[], written=[], memory=memory, reloaded=reloaded)
    assert diff["missing"], diff


def test_the_self_check_keeps_declarations_of_an_older_board(tmp_path: Path) -> None:
    pcb = _copy_demo("ecc83/ecc83-pp.kicad_pcb", tmp_path / "old")
    reloaded = write_layer.load(pcb)
    memory = write_layer.load(pcb)
    reloaded.children.remove(next(c for c in reloaded.children if write_layer.is_net_declaration(c)))
    diff = _self_check.reload_differences(source=memory, removed=[], written=[], memory=memory, reloaded=reloaded)
    assert diff["missing"], diff


@pytest.mark.parametrize("version,dropped", [(20260206, True), (20260207, True), (20241229, False)])
def test_only_a_board_of_version_20260206_or_newer_drops_declarations(version: int, dropped: bool) -> None:
    root = parse_string(f'(kicad_pcb (version {version}) (net 1 "A") (footprint "F"))')
    counts = write_layer.saved_tag_counts(root)
    assert (counts["net"] == 0) is dropped and counts["footprint"] == 1
    assert write_layer.drops_net_declarations(root) is dropped
    assert not write_layer.drops_net_declarations(parse_string(f"(kicad_sch (version {version}))"))
