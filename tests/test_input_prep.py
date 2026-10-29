"""Tests for input preparation (src/kicad_mcp/input_prep.py).

Contract: build ticket 2.
Seams: ``check_router_text_moves``, ``propose_input_preparation`` and
``confirm_input_preparation``. Fixtures are built through ``kicad_tools`` nodes
from the golden board, never hand-typed files; expectations are literals from
the fixture build, not recomputed by the code under test.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from kicad_mcp import checkpoint, input_prep, verify_board
from kicad_mcp import write_layer
from kicad_mcp import verify_board as gate
from kicad_mcp.kicad_cli import run_drc
from tests import _kicad_faults as faults

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"

# (text, layer, x, y) added to the golden board; one on silkscreen must stay put.
_ADDED = [
    ("TOP-COPPER-NOTE", "F.Cu", 20.0, 20.0),
    ("BOTTOM-COPPER-NOTE", "B.Cu", 25.0, 25.0),
    ("ALREADY-SILK", "F.SilkS", 30.0, 30.0),
]

_FONT = "(effects (font (size 1 1) (thickness 0.15)))"


def _build_fixture(dest: Path, extra_nodes: tuple[str, ...] = (), routed: bool = False) -> Path:
    """Copy the golden project and add nodes through kicad_tools (SExp / parse_sexp)."""
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp, parse_string

    shutil.copytree(GOLDEN_DIR, dest)
    pcb_path = dest / GOLDEN_NAME
    board = PCB.load(pcb_path)
    for index, (text, layer, x, y) in enumerate(_ADDED):
        node = SExp.list("gr_text", text)
        node.append(SExp.list("at", x, y, 0))
        node.append(SExp.list("layer", layer))
        node.append(SExp.list("uuid", f"00000000-0000-4000-8000-0000000000{index:02d}"))
        node.append(SExp.list("effects", SExp.list("font", SExp.list("size", 1, 1), SExp.list("thickness", 0.15))))
        board._sexp.append(node)
    for text in extra_nodes:
        board._sexp.append(parse_string(text))
    if routed:
        for i in range(3):
            seg = SExp.list("segment")
            seg.append(SExp.list("start", 10 + i, 10))
            seg.append(SExp.list("end", 12 + i, 10))
            seg.append(SExp.list("width", 0.25))
            seg.append(SExp.list("layer", "F.Cu"))
            seg.append(SExp.list("net", 1))
            seg.append(SExp.list("uuid", f"11111111-0000-4000-8000-00000000000{i}"))
            board._sexp.append(seg)
        via = SExp.list("via")
        via.append(SExp.list("at", 15, 15))
        via.append(SExp.list("size", 0.8))
        via.append(SExp.list("drill", 0.4))
        via.append(SExp.list("layers", "F.Cu", "B.Cu"))
        via.append(SExp.list("net", 1))
        via.append(SExp.list("uuid", "22222222-0000-4000-8000-000000000000"))
        board._sexp.append(via)
        zone = SExp.list("zone")
        zone.append(SExp.list("net", 1))
        zone.append(SExp.list("net_name", "GND"))
        zone.append(SExp.list("layer", "B.Cu"))
        zone.append(SExp.list("uuid", "33333333-0000-4000-8000-000000000000"))
        board._sexp.append(zone)
    write_layer.save_pcb(board, pcb_path)
    return pcb_path


@pytest.fixture
def board(tmp_path: Path) -> Path:
    return _build_fixture(tmp_path / "proj")


_EMPTY = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _fake_drc(report: dict | None = None, calls: list | None = None):
    def run(path, **_kwargs):
        # The gate must be grading the PREPARED content, not the original.
        assert input_prep.copper_texts(path) == []
        if calls is not None:
            calls.append(Path(path))
        return report or _EMPTY

    return run


def _prepare(board: Path, tmp_path: Path, drc=None, name: str = "prepared") -> dict:
    proposal = input_prep.propose_input_preparation(board, tmp_path / name)
    assert proposal["status"] == "proposed"
    return input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=drc or _fake_drc()
    )


def _gr_texts(pcb: Path) -> list:
    from kicad_tools.sexp import parse_file

    return [n for n in parse_file(str(pcb)).children if n.name == "gr_text"]


def _layer_values(node) -> list[str]:
    return [str(v) for v in node.find("layer").values]


def _patch_save(monkeypatch, mutate) -> None:
    """Make the write layer's save_pcb apply ``mutate(sexp)`` to the tree first (simulates a corrupting writer)."""
    original = write_layer.save_pcb

    def save(pcb, path=None):
        mutate(pcb._sexp)
        return original(pcb, path)

    monkeypatch.setattr(write_layer, "save_pcb", save)


def _stray(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name != "proj")


# --- core behaviour ------------------------------------------------------------


def test_router_moves_text_check_fails_before_and_passes_after(board: Path, tmp_path: Path) -> None:
    before = input_prep.check_router_text_moves(board)
    assert before["ok"] is False
    assert sorted(t["text"] for t in before["copper_texts"]) == ["BOTTOM-COPPER-NOTE", "TOP-COPPER-NOTE"]

    result = _prepare(board, tmp_path)
    assert result["status"] == "prepared"
    assert input_prep.check_router_text_moves(result["output_pcb"])["ok"] is True


def test_result_lists_every_move_and_saved_file_reloads_with_new_layers(board: Path, tmp_path: Path) -> None:
    result = _prepare(board, tmp_path)

    moved = {m["text"]: (m["layer_before"], m["layer_after"]) for m in result["moved"]}
    assert moved == {
        "TOP-COPPER-NOTE": ("F.Cu", "F.SilkS"),
        "BOTTOM-COPPER-NOTE": ("B.Cu", "B.SilkS"),
    }
    layers = {str(n.values[0]): _layer_values(n)[0] for n in _gr_texts(Path(result["output_pcb"]))}
    assert layers["TOP-COPPER-NOTE"] == "F.SilkS"
    assert layers["BOTTOM-COPPER-NOTE"] == "B.SilkS"
    assert layers["ALREADY-SILK"] == "F.SilkS"


def test_original_untouched_and_prepared_is_new_revision(board: Path, tmp_path: Path) -> None:
    before = hashlib.sha256(board.read_bytes()).hexdigest()
    result = _prepare(board, tmp_path)
    assert hashlib.sha256(board.read_bytes()).hexdigest() == before
    assert Path(result["output_pcb"]).resolve() != board.resolve()
    assert Path(result["output_pcb"]).parent.joinpath("golden_attiny1616_board.kicad_pro").is_file()


def test_dry_run_writes_nothing(board: Path, tmp_path: Path) -> None:
    out = tmp_path / "prepared"
    proposal = input_prep.propose_input_preparation(board, out)
    assert proposal["status"] == "proposed"
    assert len(proposal["moves"]) == 2
    assert not out.exists()


def test_running_twice_changes_nothing(board: Path, tmp_path: Path) -> None:
    first = _prepare(board, tmp_path)
    prepared = Path(first["output_pcb"])
    digest = hashlib.sha256(prepared.read_bytes()).hexdigest()

    again = input_prep.propose_input_preparation(prepared, tmp_path / "second")
    assert again["status"] == "nothing_to_do"
    assert again["revision_token"] is None
    assert not (tmp_path / "second").exists()
    assert hashlib.sha256(prepared.read_bytes()).hexdigest() == digest


def test_gate_is_called_on_the_prepared_revision(board: Path, tmp_path: Path) -> None:
    calls: list[Path] = []
    result = _prepare(board, tmp_path, drc=_fake_drc(calls=calls))
    assert result["status"] == "prepared"
    assert len(calls) >= 4  # one snapshot run + the gate's three runs, all on prepared content


# --- token and path discipline ---------------------------------------------------


def test_token_is_single_use_even_when_output_dir_is_free_again(board: Path, tmp_path: Path) -> None:
    proposal = input_prep.propose_input_preparation(board, tmp_path / "p1")
    first = input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert first["status"] == "prepared"
    shutil.rmtree(tmp_path / "p1")  # the output dir is free again: only consumption can refuse
    replay = input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert replay["status"] == "error"
    assert not (tmp_path / "p1").exists()


def test_source_drift_after_proposal_is_rejected(board: Path, tmp_path: Path) -> None:
    drifted = input_prep.propose_input_preparation(board, tmp_path / "p2")
    faults.drift(board)
    result = input_prep.confirm_input_preparation(
        board, drifted["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["status"] == "error"
    assert not (tmp_path / "p2").exists()


def test_token_expires_by_injected_clock(board: Path, tmp_path: Path, monkeypatch) -> None:
    now = [1_000_000.0]
    monkeypatch.setattr(input_prep, "_clock", lambda: now[0])
    proposal = input_prep.propose_input_preparation(board, tmp_path / "p")
    now[0] += checkpoint.TTL_SECONDS + 1
    result = input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["status"] == "error"
    assert not (tmp_path / "p").exists()


def test_confirm_with_a_different_pcb_path_is_refused_and_token_survives(board: Path, tmp_path: Path) -> None:
    other = _build_fixture(tmp_path / "other")
    proposal = input_prep.propose_input_preparation(board, tmp_path / "p")
    wrong = input_prep.confirm_input_preparation(
        other, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert wrong["status"] == "error"
    assert not (tmp_path / "p").exists()
    right = input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert right["status"] == "prepared"


def test_equivalent_spellings_of_the_path_are_accepted(board: Path, tmp_path: Path) -> None:
    proposal = input_prep.propose_input_preparation(board, tmp_path / "p")
    respelled = board.parent / ".." / board.parent.name / board.name
    result = input_prep.confirm_input_preparation(
        respelled, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["status"] == "prepared"


def test_output_dir_must_be_new_revision(board: Path) -> None:
    proposal = input_prep.propose_input_preparation(board, board.parent)
    assert proposal["status"] == "error"
    assert "new revision" in proposal["reason"]


def test_output_dir_inside_source_tree_is_refused(board: Path) -> None:
    proposal = input_prep.propose_input_preparation(board, board.parent / "sub" / "prepared")
    assert proposal["status"] == "error"
    assert "inside" in proposal["reason"]
    assert not (board.parent / "sub").exists()


def test_non_empty_output_dir_is_refused_at_propose(board: Path, tmp_path: Path) -> None:
    out = tmp_path / "taken"
    out.mkdir()
    (out / "something.txt").write_text("x")
    proposal = input_prep.propose_input_preparation(board, out)
    assert proposal["status"] == "error"
    assert "not empty" in proposal["reason"]


# --- failure cleanup (M1) --------------------------------------------------------


def test_failed_save_leaves_no_half_built_revision_and_token_stays_usable(
    board: Path, tmp_path: Path, monkeypatch
) -> None:
    out = tmp_path / "prepared"
    proposal = input_prep.propose_input_preparation(board, out)
    with monkeypatch.context() as patch:

        def boom(pcb, path=None):
            raise OSError("disk full")

        patch.setattr(write_layer, "save_pcb", boom)
        failed = input_prep.confirm_input_preparation(
            board, proposal["revision_token"], parity="required", drc=_fake_drc()
        )
    assert failed["status"] == "incomplete"
    assert not out.exists()
    assert _stray(tmp_path) == []
    retry = input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert retry["status"] == "prepared"


def test_copper_remaining_after_reload_is_caught_and_cleaned(board: Path, tmp_path: Path, monkeypatch) -> None:
    def revert_one(sexp) -> None:  # a writer that leaves one text on copper
        for n in sexp.children:
            if n.name == "gr_text" and str(n.values[0]) == "TOP-COPPER-NOTE":
                n.find("layer").set_value(0, "F.Cu")

    _patch_save(monkeypatch, revert_one)
    result = _prepare(board, tmp_path)
    assert result["status"] == "error"
    assert "remain" in result["reason"]
    assert _stray(tmp_path) == []


def test_node_count_change_is_caught(board: Path, tmp_path: Path, monkeypatch) -> None:
    def drop_text(sexp) -> None:
        victim = [n for n in sexp.children if n.name == "gr_text" and str(n.values[0]) == "ALREADY-SILK"][0]
        sexp.remove(victim)

    _patch_save(monkeypatch, drop_text)
    result = _prepare(board, tmp_path)
    assert result["status"] == "error"
    assert "counts" in result["reason"]
    assert _stray(tmp_path) == []


def test_wrong_layer_after_reload_is_caught(board: Path, tmp_path: Path, monkeypatch) -> None:
    def wrong_layer(sexp) -> None:
        for n in sexp.children:
            if n.name == "gr_text" and str(n.values[0]) == "TOP-COPPER-NOTE":
                n.find("layer").set_value(0, "F.Fab")

    _patch_save(monkeypatch, wrong_layer)
    result = _prepare(board, tmp_path)
    assert result["status"] == "error"
    assert "layer" in result["reason"]
    assert _stray(tmp_path) == []


# --- duplicate / empty uuids (M2) -------------------------------------------------


def test_duplicate_and_empty_uuids_are_compared_per_position(tmp_path: Path) -> None:
    dup = "00000000-0000-4000-8000-0000000000aa"
    board = _build_fixture(
        tmp_path / "proj",
        (
            f'(gr_text "DUP-F" (at 40 40 0) (layer "F.Cu") (uuid "{dup}") {_FONT})',
            f'(gr_text "DUP-B" (at 41 40 0) (layer "B.Cu") (uuid "{dup}") {_FONT})',
            f'(gr_text "NO-UUID" (at 42 40 0) (layer "F.Cu") {_FONT})',
        ),
    )
    result = _prepare(board, tmp_path)
    assert result["status"] == "prepared"
    layers = {str(n.values[0]): _layer_values(n)[0] for n in _gr_texts(Path(result["output_pcb"]))}
    assert layers["DUP-F"] == "F.SilkS"
    assert layers["DUP-B"] == "B.SilkS"
    assert layers["NO-UUID"] == "F.SilkS"
    assert len(result["moved"]) == 5


def test_wrong_layer_is_caught_even_when_uuids_are_duplicated(tmp_path: Path, monkeypatch) -> None:
    dup = "00000000-0000-4000-8000-0000000000aa"
    board = _build_fixture(
        tmp_path / "proj",
        (
            f'(gr_text "DUP-F" (at 40 40 0) (layer "F.Cu") (uuid "{dup}") {_FONT})',
            f'(gr_text "DUP-B" (at 41 40 0) (layer "B.Cu") (uuid "{dup}") {_FONT})',
        ),
    )

    def swap(sexp) -> None:  # DUP-B lands on the front silkscreen: right uuid, wrong layer
        for n in sexp.children:
            if n.name == "gr_text" and str(n.values[0]) == "DUP-B":
                n.find("layer").set_value(0, "F.SilkS")

    _patch_save(monkeypatch, swap)
    result = _prepare(board, tmp_path)
    assert result["status"] == "error"


# --- unhandled copper items (L1/L2) -----------------------------------------------


def test_inner_layer_text_and_text_boxes_are_unhandled_and_keep_check_red(tmp_path: Path) -> None:
    board = _build_fixture(
        tmp_path / "proj",
        (
            f'(gr_text "INNER" (at 40 40 0) (layer "In1.Cu") (uuid "00000000-0000-4000-8000-0000000000b1") {_FONT})',
            '(gr_text_box "BOX" (start 45 40) (end 55 45) (layer "F.Cu") '
            '(uuid "00000000-0000-4000-8000-0000000000b2"))',
        ),
    )
    before = input_prep.check_router_text_moves(board)
    assert before["ok"] is False
    assert {u["text"] for u in before["unhandled"]} == {"INNER", "BOX"}

    result = _prepare(board, tmp_path)
    assert result["status"] == "prepared"
    kinds = {u["text"]: (u["kind"], u["layer"]) for u in result["unhandled"]}
    assert kinds == {"INNER": ("gr_text", "In1.Cu"), "BOX": ("gr_text_box", "F.Cu")}
    assert all(u["reason"] for u in result["unhandled"])
    assert result["input_floor"]["router_text_check_ok"] is False
    assert "verdict" not in result["input_floor"]  # only verify_board certifies (rule 3)
    assert result["input_floor"]["certified"] is False
    assert result["input_floor"]["grading_status"] in {"PASS", "FAIL", "INCOMPLETE"}
    # F.Cu / B.Cu text still moved; the unhandled items were not touched
    assert {m["text"] for m in result["moved"]} == {"TOP-COPPER-NOTE", "BOTTOM-COPPER-NOTE"}
    out = Path(result["output_pcb"])
    assert {str(n.values[0]): _layer_values(n)[0] for n in _gr_texts(out)}["INNER"] == "In1.Cu"
    after = input_prep.check_router_text_moves(out)
    assert after["ok"] is False
    assert {u["text"] for u in after["unhandled"]} == {"INNER", "BOX"}


def test_only_unhandled_items_is_nothing_to_do_but_not_clean(tmp_path: Path) -> None:
    board = _build_fixture(
        tmp_path / "proj",
        (f'(gr_text "INNER" (at 40 40 0) (layer "In2.Cu") (uuid "00000000-0000-4000-8000-0000000000b3") {_FONT})',),
    )
    first = _prepare(board, tmp_path)  # moves the two outer-layer texts first
    again = input_prep.propose_input_preparation(first["output_pcb"], tmp_path / "second")
    assert again["status"] == "nothing_to_do"
    assert [u["text"] for u in again["unhandled"]] == ["INNER"]


# --- preserved details and routed boards ------------------------------------------


def test_knockout_and_mirrored_back_text_keep_their_details(tmp_path: Path) -> None:
    board = _build_fixture(
        tmp_path / "proj",
        (
            '(gr_text "KO-MIRROR" (at 35 35 0) (layer "B.Cu" knockout) '
            '(uuid "00000000-0000-4000-8000-0000000000c1") '
            "(effects (font (size 1 1) (thickness 0.15)) (justify mirror)))",
        ),
    )
    result = _prepare(board, tmp_path)
    node = [n for n in _gr_texts(Path(result["output_pcb"])) if str(n.values[0]) == "KO-MIRROR"][0]
    assert _layer_values(node) == ["B.SilkS", "knockout"]
    assert [str(v) for v in node.find("justify").values] == ["mirror"]
    assert [float(v) for v in node.find("at").values[:2]] == [35.0, 35.0]


def test_routed_board_keeps_every_top_level_node_count(tmp_path: Path) -> None:
    from kicad_tools.sexp import parse_file

    board = _build_fixture(tmp_path / "proj", routed=True)

    def counts(path: Path) -> dict[str, int]:
        out: dict[str, int] = {}
        for n in parse_file(str(path)).children:
            if n.name:
                out[n.name] = out.get(n.name, 0) + 1
        return out

    before = counts(board)
    assert before["segment"] == 3 and before["via"] == 1 and before["zone"] == 1
    result = _prepare(board, tmp_path)
    assert result["status"] == "prepared"
    assert counts(Path(result["output_pcb"])) == before


# --- copy guards (L4) -------------------------------------------------------------


def test_oversized_project_is_refused_without_leftovers(board: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(gate, "_MAX_COPY_BYTES", 1)
    proposal = input_prep.propose_input_preparation(board, tmp_path / "prepared")
    result = input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["status"] in ("error", "incomplete")
    assert "too large" in result["reason"]
    assert _stray(tmp_path) == []


def test_previous_prepared_outputs_are_not_copied_and_source_gains_nothing(tmp_path: Path) -> None:
    board = _build_fixture(tmp_path / "proj")
    stale = board.parent / "proj_prepared"
    stale.mkdir()
    (stale / "junk.txt").write_text("x")
    names_before = sorted(p.name for p in board.parent.iterdir())

    result = _prepare(board, tmp_path)
    assert result["status"] == "prepared"
    assert not (Path(result["output_pcb"]).parent / "proj_prepared").exists()
    assert sorted(p.name for p in board.parent.iterdir()) == names_before


# --- real kicad-cli ---------------------------------------------------------------


def test_real_kicad_cli_floor_and_pinned_baseline(board: Path, tmp_path: Path) -> None:
    """Real kicad-cli: the golden board is unrouted, so its floor is its unconnected items."""
    result = _prepare(board, tmp_path, drc=run_drc)
    assert result["input_floor"]["unconnected_nets"] > 0
    regraded = verify_board.verify_board(result["output_pcb"], parity="required", baseline=result["baseline"])
    assert not any("rules changed" in r for r in regraded["incomplete_reasons"])


# --- round 2 ----------------------------------------------------------------------


def test_keyboard_interrupt_during_save_cleans_temp_and_keeps_token(
    board: Path, tmp_path: Path, monkeypatch
) -> None:
    out = tmp_path / "prepared"
    proposal = input_prep.propose_input_preparation(board, out)
    with monkeypatch.context() as patch:

        def interrupt(pcb, path=None):
            raise KeyboardInterrupt

        patch.setattr(write_layer, "save_pcb", interrupt)
        with pytest.raises(KeyboardInterrupt):
            input_prep.confirm_input_preparation(
                board, proposal["revision_token"], parity="required", drc=_fake_drc()
            )
    assert not out.exists()
    assert _stray(tmp_path) == []
    retry = input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert retry["status"] == "prepared"


def test_gate_failure_after_rename_reports_incomplete_with_the_revision(board: Path, tmp_path: Path) -> None:
    from kicad_mcp.kicad_cli import KicadCliError

    def missing_cli(path, **_kwargs):
        raise KicadCliError("kicad-cli not found")

    result = _prepare(board, tmp_path, drc=missing_cli)
    assert result["status"] == "incomplete"
    assert Path(result["output_pcb"]).is_file()
    assert input_prep.check_router_text_moves(result["output_pcb"])["ok"] is True
    assert "kicad-cli" in result["reason"]
    assert [m["text"] for m in result["moved"]]  # the moves are still reported


def test_only_unhandled_propose_carries_not_clean_flag_and_note(tmp_path: Path) -> None:
    board = _build_fixture(
        tmp_path / "proj",
        (f'(gr_text "INNER" (at 40 40 0) (layer "In2.Cu") (uuid "00000000-0000-4000-8000-0000000000b4") {_FONT})',),
    )
    first = _prepare(board, tmp_path)
    again = input_prep.propose_input_preparation(first["output_pcb"], tmp_path / "second")
    assert again["status"] == "nothing_to_do"
    assert again["router_text_check_ok"] is False
    assert "not a clean baseline" in again["note"]
    clean = _build_fixture(tmp_path / "proj2")
    done = _prepare(clean, tmp_path, name="prepared2")
    assert input_prep.propose_input_preparation(done["output_pcb"], tmp_path / "third")["router_text_check_ok"] is True


def test_unhandled_drift_during_preparation_is_caught(tmp_path: Path, monkeypatch) -> None:
    board = _build_fixture(
        tmp_path / "proj",
        (
            '(gr_text_box "BOX" (start 45 40) (end 55 45) (layer "F.Cu") '
            '(uuid "00000000-0000-4000-8000-0000000000b5"))',
        ),
    )

    def move_box(sexp) -> None:  # a writer that relocates the unhandled text box
        for n in sexp.children:
            if n.name == "gr_text_box":
                n.find("layer").set_value(0, "B.Cu")

    _patch_save(monkeypatch, move_box)
    result = _prepare(board, tmp_path)
    assert result["status"] == "error"
    assert "unhandled" in result["reason"]
    assert _stray(tmp_path) == []


def test_floor_note_states_when_it_is_not_a_clean_baseline(tmp_path: Path) -> None:
    board = _build_fixture(
        tmp_path / "proj",
        (f'(gr_text "INNER" (at 40 40 0) (layer "In1.Cu") (uuid "00000000-0000-4000-8000-0000000000b6") {_FONT})',),
    )
    dirty = _prepare(board, tmp_path)["input_floor"]
    assert "not a clean baseline" in dirty["note"]
    clean = _prepare(_build_fixture(tmp_path / "proj2"), tmp_path, name="prepared2")["input_floor"]
    assert "not a clean baseline" not in clean["note"]
    assert clean["router_text_check_ok"] is True


def test_only_this_modules_own_prepared_sibling_is_skipped(tmp_path: Path) -> None:
    board = _build_fixture(tmp_path / "proj")
    own = board.parent / "proj_prepared"
    own.mkdir()
    (own / "a.txt").write_text("x")
    user = board.parent / "my_prepared"  # a user's own directory: must be copied
    user.mkdir()
    (user / "keep.txt").write_text("y")
    result = _prepare(board, tmp_path)
    copied = Path(result["output_pcb"]).parent
    assert (copied / "my_prepared" / "keep.txt").is_file()
    assert not (copied / "proj_prepared").exists()


def test_created_parent_directories_are_removed_on_failure(board: Path, tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "deep" / "er" / "prepared"
    proposal = input_prep.propose_input_preparation(board, out)

    def boom(pcb, path=None):
        raise OSError("disk full")

    monkeypatch.setattr(write_layer, "save_pcb", boom)
    result = input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["status"] == "incomplete"
    assert not (tmp_path / "deep").exists()
