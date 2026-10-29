"""Tests for silkscreen text minimums (src/kicad_mcp/silk_minimums.py).

Contract: build ticket 20.
Seams: ``scan_silk_texts``, ``propose_silkscreen_minimums`` and
``confirm_silkscreen_minimums``. Fixtures are the golden board plus nodes added
through ``kicad_tools`` (never hand-typed files); expected values are literals
worked out by hand from the fixture numbers, not recomputed by the code under test.

KiCad facts used (measured with kicad-cli 2026-10-11): ``(size H W)`` -- the FIRST
value is what ``text_height`` compares; the effective stroke is clamped to a quarter
of the narrower dimension, so a 0.5 mm wide text with ``(thickness 0.15)`` is graded
at 0.125 mm.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from kicad_mcp import silk_minimums as sm
from kicad_mcp import write_layer
from tests import _kicad_faults as faults

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"

# test-local rule file: the one tolerated .kicad_dru, mirrors the board minimums
_DRU = (
    "(version 1)\n"
    '(rule "Silkscreen text" (layer "*SilkS") '
    "(condition \"A.Type == 'Text' || A.Type == 'Text Box'\") "
    "(constraint text_thickness (min 0.15mm)) (constraint text_height (min 1mm)))\n"
)

# (text, layer, height, width, thickness, x, mirror)
_BOARD_TEXTS = [
    ("SMALL-TOP", "F.SilkS", 0.8, 0.8, 0.10, 10, False),
    ("SMALL-BACK", "B.SilkS", 0.6, 0.9, 0.12, 20, True),
    ("FINE", "F.SilkS", 1.0, 1.0, 0.15, 30, False),
    ("THIN-ONLY", "F.SilkS", 1.2, 1.2, 0.10, 40, False),
    ("NARROW", "F.SilkS", 1.0, 0.5, 0.15, 50, False),
    ("COPPER-SMALL", "F.Cu", 0.5, 0.5, 0.05, 60, False),
    ("FAB-SMALL", "F.Fab", 0.5, 0.5, 0.05, 70, False),
    # height-only violation: effective stroke min(0.15, 0.9/4) = 0.15 is fine
    ("HEIGHT-ONLY", "F.SilkS", 0.9, 1.6, 0.15, 80, False),
    # H < W clamp direction: height and clamped stroke (0.5/4 = 0.125) both fail
    ("WIDE-LOW", "F.SilkS", 0.5, 1.5, 0.15, 90, False),
]


def _build(dest: Path, with_dru: bool = False) -> Path:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    shutil.copytree(GOLDEN_DIR, dest)
    pcb = dest / GOLDEN_NAME
    board = PCB.load(pcb)
    for i, (text, layer, h, w, t, x, mirror) in enumerate(_BOARD_TEXTS):
        node = SExp.list("gr_text", text)
        node.append(SExp.list("at", x, 20, 0))
        node.append(SExp.list("layer", layer))
        node.append(SExp.list("uuid", f"00000000-0000-4000-8000-0000000000{i:02d}"))
        eff = SExp.list("effects", SExp.list("font", SExp.list("size", h, w), SExp.list("thickness", t)))
        if mirror:
            eff.append(SExp.list("justify", "left", "mirror"))
        node.append(eff)
        board._sexp.append(node)
    # footprint C1: Reference (visible, silk) made small; the hidden KiLib property
    # on silk made small too; Value is on F.Fab and must stay as it is.
    c1 = next(
        n for n in board._sexp.children
        if n.name == "footprint" and any(
            p.name == "property" and p.values[:2] == ["Reference", "C1"] for p in n.children
        )
    )
    for prop in (p for p in c1.children if p.name == "property"):
        font = prop.find("effects").find("font")
        if prop.values[0] == "Reference":
            font.find("size").set_value(0, 0.7)
            font.find("size").set_value(1, 0.7)
            font.find("thickness").set_value(0, 0.1)
        elif prop.values[0] == "KiLib_Generator":
            font.find("size").set_value(0, 0.5)
            font.find("size").set_value(1, 0.5)
            font.find("thickness").set_value(0, 0.05)
        elif prop.values[0] == "Value":
            font.find("size").set_value(0, 0.5)
            font.find("size").set_value(1, 0.5)
    write_layer.save_pcb(board, pcb)
    if with_dru:
        (dest / GOLDEN_NAME.replace(".kicad_pcb", ".kicad_dru")).write_text(_DRU, encoding="utf-8")
    return pcb


@pytest.fixture
def board(tmp_path: Path) -> Path:
    return _build(tmp_path / "proj")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hash(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): _sha(p) for p in sorted(root.rglob("*")) if p.is_file()}


def _apply(board: Path, tmp_path: Path, name: str = "rev", **kw) -> dict:
    proposal = sm.propose_silkscreen_minimums(board, tmp_path / name, **kw)
    assert proposal["status"] == "proposed", proposal
    return sm.confirm_silkscreen_minimums(board, proposal["revision_token"])


def _by_text(records: list[dict], text: str) -> dict:
    return next(r for r in records if r["text"] == text)


def _sizes(pcb: Path) -> dict[tuple[str, str], tuple]:
    """(owner or '', text) -> (height, width, thickness|None, layer) from the raw file."""
    return {(r["owner"] or "", r["text"]): (r["height"], r["width"], r["thickness"], r["layer"])
            for r in sm.scan_silk_texts(pcb)}


# --- scan -----------------------------------------------------------------------


def test_scan_lists_silk_texts_of_every_kind_and_nothing_else(board: Path) -> None:
    recs = sm.scan_silk_texts(board)
    texts = {(r["owner"], r["kind"], r["text"]) for r in recs}
    assert ("C1", "property", "C1") in texts  # Reference property
    assert ("C1", "property", "C1") in texts
    assert (None, "gr_text", "SMALL-TOP") in texts
    assert not any(r["text"] in ("COPPER-SMALL", "FAB-SMALL", "100nF") for r in recs)
    assert all(r["layer"] in ("F.SilkS", "B.SilkS") for r in recs)
    hidden = [r for r in recs if r["hidden"]]
    assert hidden and all(r["field"] == "KiLib_Generator" for r in hidden if r["owner"] == "C1")
    assert _by_text(recs, "SMALL-BACK")["mirrored"] is True
    assert _by_text(recs, "SMALL-TOP")["height"] == 0.8


# --- core behaviour ----------------------------------------------------------------


def test_every_text_below_minimums_is_raised_and_listed_others_untouched(board: Path, tmp_path: Path) -> None:
    before = _sizes(board)
    result = _apply(board, tmp_path)
    assert result["status"] == "applied"
    after = _sizes(Path(result["output_pcb"]))
    changed = {(c["owner"] or "", c["text"]) for c in result["changed"]}

    # literals worked by hand (see module docstring for the clamp rule)
    assert after[("", "SMALL-TOP")][:3] == (1.0, 1.0, 0.15)  # 0.8 * 1.25
    assert after[("", "SMALL-BACK")][:3] == (1.0, 1.5, 0.15)  # aspect 0.6:0.9 kept
    assert after[("", "THIN-ONLY")][:3] == (1.2, 1.2, 0.15)  # only stroke raised
    assert after[("", "NARROW")][:3] == (1.2, 0.6, 0.15)  # 1.0:0.5 kept, stroke clamp cleared
    assert after[("C1", "C1")][:3] == (1.0, 1.0, 0.15)  # Reference property, 0.7 -> 1.0 (x1.428571)
    assert after[("", "HEIGHT-ONLY")][:3] == (1.0, 1.77778, 0.15)  # x1.111111, 6 significant digits as kicad_tools writes
    assert after[("", "WIDE-LOW")][:3] == (1.0, 3.0, 0.15)  # x2, H<W
    assert after[("", "FINE")] == before[("", "FINE")] and ("", "FINE") not in changed

    for key in after:
        if key not in changed:
            assert after[key] == before[key]
    assert ("", "SMALL-TOP") in changed and ("", "THIN-ONLY") in changed

    entry = _by_text(result["changed"], "SMALL-TOP")
    assert entry["before"] == {"height": 0.8, "width": 0.8, "thickness": 0.1}
    assert entry["after"] == {"height": 1.0, "width": 1.0, "thickness": 0.15}
    assert entry["layer"] == "F.SilkS"


def test_non_silk_layers_are_not_touched(board: Path, tmp_path: Path) -> None:
    from kicad_tools.sexp import parse_file

    result = _apply(board, tmp_path)
    root = parse_file(result["output_pcb"])
    small = {n.values[0]: n for n in root.children if n.name == "gr_text"}
    for name in ("COPPER-SMALL", "FAB-SMALL"):
        font = small[name].find("effects").find("font")
        assert [float(v) for v in font.find("size").values] == [0.5, 0.5]
        assert float(font.find("thickness").values[0]) == 0.05


def test_mirror_hidden_and_position_are_preserved(board: Path, tmp_path: Path) -> None:
    before = {(r["owner"], r["text"]): r for r in sm.scan_silk_texts(board)}
    result = _apply(board, tmp_path)
    after = {(r["owner"], r["text"]): r for r in sm.scan_silk_texts(result["output_pcb"])}
    assert set(before) == set(after)
    for key, rec in before.items():
        for field in ("layer", "mirrored", "hidden", "at", "uuid", "kind", "field"):
            assert after[key][field] == rec[field], (key, field)
    assert after[(None, "SMALL-BACK")]["mirrored"] is True
    hidden_after = [r for r in after.values() if r["hidden"]]
    assert hidden_after and all(r["height"] >= 1.0 for r in hidden_after)
    assert any(c["hidden"] for c in result["changed"])  # reported as hidden, still raised


def test_idempotent_second_run_has_nothing_to_do(board: Path, tmp_path: Path) -> None:
    result = _apply(board, tmp_path)
    again = sm.propose_silkscreen_minimums(result["output_pcb"], tmp_path / "rev2")
    assert again["status"] == "nothing_to_do"
    assert again["changes"] == [] and again["revision_token"] is None
    assert not (tmp_path / "rev2").exists()


def test_dry_run_writes_nothing_and_source_is_untouched(board: Path, tmp_path: Path) -> None:
    root = board.parent
    snapshot = _tree_hash(root)
    siblings = sorted(p.name for p in tmp_path.iterdir())
    proposal = sm.propose_silkscreen_minimums(board, tmp_path / "rev")
    assert proposal["status"] == "proposed"
    assert _tree_hash(root) == snapshot
    assert sorted(p.name for p in tmp_path.iterdir()) == siblings
    assert {c["text"] for c in proposal["changes"]} >= {"SMALL-TOP", "SMALL-BACK", "THIN-ONLY", "NARROW"}
    sm.confirm_silkscreen_minimums(board, proposal["revision_token"])
    assert _tree_hash(root) == snapshot  # source untouched even after confirm
    assert (tmp_path / "rev" / GOLDEN_NAME).is_file()


def test_nothing_else_changes_in_the_revision(board: Path, tmp_path: Path) -> None:
    from collections import Counter
    from kicad_tools.sexp import parse_file

    result = _apply(board, tmp_path)
    a, b = parse_file(str(board)), parse_file(result["output_pcb"])
    assert Counter(c.name for c in a.children) == Counter(c.name for c in b.children)
    fa = [c for c in a.children if c.name == "footprint"]
    fb = [c for c in b.children if c.name == "footprint"]
    assert [x.find("at").values for x in fa] == [x.find("at").values for x in fb]
    assert [len([c for c in x.children if c.name == "pad"]) for x in fa] == [
        len([c for c in x.children if c.name == "pad"]) for x in fb
    ]


# --- checkpoint ------------------------------------------------------------------


def test_token_is_single_use(board: Path, tmp_path: Path) -> None:
    proposal = sm.propose_silkscreen_minimums(board, tmp_path / "rev")
    assert sm.confirm_silkscreen_minimums(board, proposal["revision_token"])["status"] == "applied"
    again = sm.confirm_silkscreen_minimums(board, proposal["revision_token"])
    assert again["status"] == "error"


def test_unknown_token_and_wrong_path_are_refused(board: Path, tmp_path: Path) -> None:
    assert sm.confirm_silkscreen_minimums(board, "nope")["status"] == "error"
    proposal = sm.propose_silkscreen_minimums(board, tmp_path / "rev")
    other = _build(tmp_path / "other")
    assert sm.confirm_silkscreen_minimums(other, proposal["revision_token"])["status"] == "error"
    # the refused attempt did not consume the token
    assert sm.confirm_silkscreen_minimums(board, proposal["revision_token"])["status"] == "applied"


def test_source_drift_after_proposal_is_rejected(board: Path, tmp_path: Path) -> None:
    proposal = sm.propose_silkscreen_minimums(board, tmp_path / "rev")
    faults.drift(board)  # test-only drift, never a project edit
    result = sm.confirm_silkscreen_minimums(board, proposal["revision_token"])
    assert result["status"] == "error" and "changed" in result["reason"]
    assert not (tmp_path / "rev").exists()


def test_token_expires_by_injected_clock(board: Path, tmp_path: Path, monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(sm, "_clock", lambda: now[0])
    proposal = sm.propose_silkscreen_minimums(board, tmp_path / "rev")
    now[0] += 3601
    assert sm.confirm_silkscreen_minimums(board, proposal["revision_token"])["status"] == "error"


def test_output_dir_rules(board: Path, tmp_path: Path) -> None:
    assert sm.propose_silkscreen_minimums(board, board.parent)["status"] == "error"
    assert sm.propose_silkscreen_minimums(board, board.parent / "sub")["status"] == "error"
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    assert sm.propose_silkscreen_minimums(board, busy)["status"] == "error"


def test_failed_save_leaves_nothing_behind_and_token_stays_usable(board: Path, tmp_path: Path, monkeypatch) -> None:
    proposal = sm.propose_silkscreen_minimums(board, tmp_path / "deep" / "rev")
    original = write_layer.save_pcb

    def boom(pcb, path=None):
        raise OSError("disk full")

    monkeypatch.setattr(write_layer, "save_pcb", boom)
    failed = sm.confirm_silkscreen_minimums(board, proposal["revision_token"])
    assert failed["status"] == "incomplete"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]  # temp dir and 'deep' removed
    monkeypatch.setattr(write_layer, "save_pcb", original)
    assert sm.confirm_silkscreen_minimums(board, proposal["revision_token"])["status"] == "applied"


def test_corrupting_writer_is_caught_by_reload_verification(board: Path, tmp_path: Path, monkeypatch) -> None:
    original = write_layer.save_pcb

    def corrupt(pcb, path=None):  # simulate a writer that also drops a layer assignment
        for n in pcb._sexp.children:
            if n.name == "gr_text" and n.values and n.values[0] == "SMALL-TOP":
                n.find("layer").set_value(0, "F.Fab")
        return original(pcb, path)

    proposal = sm.propose_silkscreen_minimums(board, tmp_path / "rev")
    monkeypatch.setattr(write_layer, "save_pcb", corrupt)
    result = sm.confirm_silkscreen_minimums(board, proposal["revision_token"])
    assert result["status"] == "error" and "CRITICAL" in result["reason"]
    assert not (tmp_path / "rev").exists()
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


def test_declared_minimums_are_honoured_and_validated(board: Path, tmp_path: Path) -> None:
    result = _apply(board, tmp_path, min_height_mm=1.5, min_thickness_mm=0.2)
    after = _sizes(result["output_pcb"])
    assert after[("", "FINE")][0] >= 1.5 and after[("", "FINE")][2] >= 0.2
    for bad in (0, -1, float("nan")):
        assert sm.propose_silkscreen_minimums(board, tmp_path / "x", min_height_mm=bad)["status"] == "error"


def test_zero_size_text_is_reported_unhandled_not_guessed(tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    pcb = _build(tmp_path / "proj")
    b = PCB.load(pcb)
    n = SExp.list("gr_text", "ZERO")
    n.append(SExp.list("at", 5, 5, 0))
    n.append(SExp.list("layer", "F.SilkS"))
    n.append(SExp.list("effects", SExp.list("font", SExp.list("size", 0, 0), SExp.list("thickness", 0.1))))
    b._sexp.append(n)
    write_layer.save_pcb(b, pcb)
    proposal = sm.propose_silkscreen_minimums(pcb, tmp_path / "rev")
    assert [u["text"] for u in proposal["unhandled"]] == ["ZERO"]
    assert "ZERO" not in {c["text"] for c in proposal["changes"]}


# --- real kicad-cli ----------------------------------------------------------------


def test_real_kicad_cli_no_text_violation_after_and_violations_before(tmp_path: Path) -> None:
    from kicad_mcp.kicad_cli import run_drc

    pcb = _build(tmp_path / "proj", with_dru=True)

    def text_violations(path: Path) -> set[tuple[str, str]]:
        report = run_drc(path)
        found = set()
        for v in report["violations"]:
            if v["type"] in ("text_height", "text_thickness"):
                for item in v["items"]:
                    for name in (t[0] for t in _BOARD_TEXTS):
                        if f'"{name}"' in item["description"]:
                            found.add((v["type"], name))
        return found

    before = text_violations(pcb)
    assert ("text_height", "SMALL-TOP") in before
    assert ("text_thickness", "THIN-ONLY") in before  # height fine, stroke 0.10, no clamp
    assert ("text_thickness", "NARROW") in before  # H > W clamp
    assert ("text_height", "HEIGHT-ONLY") in before  # distinct: stroke is fine
    assert ("text_thickness", "HEIGHT-ONLY") not in before
    assert ("text_height", "WIDE-LOW") in before and ("text_thickness", "WIDE-LOW") in before  # H < W clamp
    assert ("text_height", "THIN-ONLY") not in before and ("text_height", "FINE") not in before

    result = _apply(pcb, tmp_path)
    assert result["status"] == "applied"
    out = Path(result["output_pcb"])
    assert (out.parent / GOLDEN_NAME.replace(".kicad_pcb", ".kicad_dru")).is_file()
    assert text_violations(out) == set()


# --- nodes the tool cannot safely resize, boxes, anchors, scale, cache ---------------


def _append(pcb: Path, *sources: str) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    b = PCB.load(pcb)
    for src in sources:
        b._sexp.append(parse_string(src))
    write_layer.save_pcb(b, pcb)


# A table KiCad 10 loads. The earlier form gave its table_cell (at ...) and (size ...), which
# KiCad refuses ("Erwarte locked, start, pts, ..."); the write layer's parse check caught it
# (build ticket 42).
_TABLE = (
    '(table (column_count 1) (locked no) (layer "F.SilkS") (uuid "aaaaaaaa-0000-4000-8000-000000000001") '
    "(border (external yes) (header yes) (stroke (width 0.1) (type solid))) "
    "(separators (rows yes) (cols yes) (stroke (width 0.1) (type solid))) "
    "(column_widths 10) (row_heights 5) "
    '(cells (table_cell "CELLTXT" (start 5 5) (end 15 10) (margins 0.5 0.5 0.5 0.5) (span 1 1) '
    '(layer "F.SilkS") (uuid "aaaaaaaa-0000-4000-8000-000000000004") '
    "(effects (font (size 0.5 0.5) (thickness 0.1)) (justify left top)))))"
)
_DIM = (
    '(dimension (type aligned) (layer "F.SilkS") (uuid "aaaaaaaa-0000-4000-8000-000000000002") '
    "(pts (xy 0 0) (xy 10 0)) (height 3) "
    '(gr_text "10.0 mm" (at 5 -1 0) (layer "F.SilkS") '
    "(effects (font (size 0.5 0.5) (thickness 0.1)))))"
)


def test_table_cell_and_dimension_are_listed_unhandled_not_silently_missed(tmp_path: Path) -> None:
    pcb = _build(tmp_path / "proj")
    _append(pcb, _TABLE, _DIM)
    proposal = sm.propose_silkscreen_minimums(pcb, tmp_path / "rev")
    kinds = {u["kind"]: u for u in proposal["unhandled"]}
    assert "table" in kinds and "dimension" in kinds
    assert kinds["table"]["text"] == "CELLTXT" and kinds["table"]["layer"] == "F.SilkS"
    assert "not resized" in kinds["dimension"]["reason"]
    assert "CELLTXT" not in {c["text"] for c in proposal["changes"]}
    result = sm.confirm_silkscreen_minimums(pcb, proposal["revision_token"])
    assert result["status"] == "applied"
    assert {u["kind"] for u in result["unhandled"]} >= {"table", "dimension"}


def test_footprint_level_unknown_silk_text_node_is_listed_unhandled(tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    pcb = _build(tmp_path / "proj")
    b = PCB.load(pcb)
    fp = next(n for n in b._sexp.children if n.name == "footprint")
    fp.append(
        parse_string('(fp_mystery "MYSTERY" (layer "B.SilkS") (effects (font (size 0.4 0.4) (thickness 0.1))))')
    )
    # An unknown token on purpose, so KiCad cannot load this board and the write layer's
    # parse check would refuse it: written with the layer's serialiser, unchecked.
    faults.write_unchecked(b._sexp, pcb)
    proposal = sm.propose_silkscreen_minimums(pcb, tmp_path / "rev")
    assert [u["kind"] for u in proposal["unhandled"]] == ["fp_mystery"]


def test_text_box_is_resized_font_only_and_flagged_may_clip(tmp_path: Path) -> None:
    from kicad_tools.sexp import parse_file

    pcb = _build(tmp_path / "proj")
    _append(
        pcb,
        '(gr_text_box "BOXED" (start 100 100) (end 120 110) (layer "F.SilkS") '
        '(uuid "bbbbbbbb-0000-4000-8000-000000000001") '
        "(effects (font (size 0.6 0.6) (thickness 0.15))))",
    )
    result = _apply(pcb, tmp_path)
    entry = _by_text(result["changed"], "BOXED")
    assert entry["box_may_clip"] is True
    assert _by_text(result["changed"], "SMALL-TOP")["box_may_clip"] is False
    assert "clip" in result["note"]
    box = next(n for n in parse_file(result["output_pcb"]).children if n.name == "gr_text_box")
    assert [float(v) for v in box.find("end").values[:2]] == [120.0, 110.0]  # box not resized
    assert [float(v) for v in box.find("effects").find("font").find("size").values] == [1.0, 1.0]


def test_non_center_justification_is_flagged_as_anchor_shift(tmp_path: Path) -> None:
    pcb = _build(tmp_path / "proj")
    _append(
        pcb,
        '(gr_text "LEFTY" (at 110 20 0) (layer "F.SilkS") (uuid "cccccccc-0000-4000-8000-000000000001") '
        "(effects (font (size 0.6 0.6) (thickness 0.15)) (justify left bottom)))",
    )
    result = _apply(pcb, tmp_path)
    assert _by_text(result["changed"], "LEFTY")["anchor_shift"] == "left,bottom"
    assert _by_text(result["changed"], "SMALL-BACK")["anchor_shift"] == "left"  # (justify left mirror): mirror itself is not an anchor
    assert _by_text(result["changed"], "SMALL-TOP")["anchor_shift"] is None
    assert "anchor" in result["note"]


def test_scale_above_two_is_reported_as_large_scale(tmp_path: Path) -> None:
    pcb = _build(tmp_path / "proj")
    _append(
        pcb,
        '(gr_text "BIG-SCALE" (at 120 20 0) (layer "F.SilkS") (uuid "dddddddd-0000-4000-8000-000000000001") '
        "(effects (font (size 1.5 0.25) (thickness 0.1))))",
    )
    result = _apply(pcb, tmp_path)
    big = _by_text(result["changed"], "BIG-SCALE")
    assert big["after"] == {"height": 3.6, "width": 0.6, "thickness": 0.15}
    assert big["scale"] == 2.4 and big["large_scale"] is True
    assert _by_text(result["changed"], "SMALL-TOP")["large_scale"] is False
    assert result["summary"]["large_scale"] == ["BIG-SCALE"]
    assert result["summary"]["changed"] == len(result["changed"])


def test_missing_thickness_is_unhandled_never_assumed(tmp_path: Path) -> None:
    from kicad_tools.sexp import parse_file

    pcb = _build(tmp_path / "proj")
    _append(
        pcb,
        '(gr_text "NO-THICK" (at 130 20 0) (layer "F.SilkS") (uuid "eeeeeeee-0000-4000-8000-000000000001") '
        "(effects (font (size 0.5 0.5))))",
    )
    result = _apply(pcb, tmp_path)
    assert [u["text"] for u in result["unhandled"]] == ["NO-THICK"]
    assert "thickness" in result["unhandled"][0]["reason"]
    node = next(
        n for n in parse_file(result["output_pcb"]).children if n.name == "gr_text" and n.values[0] == "NO-THICK"
    )
    font = node.find("effects").find("font")
    assert font.find("thickness") is None
    assert [float(v) for v in font.find("size").values] == [0.5, 0.5]


def test_stale_render_cache_is_removed_for_changed_texts_only(tmp_path: Path) -> None:
    from kicad_tools.sexp import parse_file

    cache = '(render_cache "{t}" 0 (polygon (pts (xy 0 0) (xy 1 0) (xy 1 1))))'
    pcb = _build(tmp_path / "proj")
    _append(
        pcb,
        '(gr_text "CACHED-SMALL" (at 140 20 0) (layer "F.SilkS") (uuid "ffffffff-0000-4000-8000-000000000001") '
        "(effects (font (size 0.6 0.6) (thickness 0.15))) " + cache.format(t="CACHED-SMALL") + ")",
        '(gr_text "CACHED-FINE" (at 150 20 0) (layer "F.SilkS") (uuid "ffffffff-0000-4000-8000-000000000002") '
        "(effects (font (size 1 1) (thickness 0.15))) " + cache.format(t="CACHED-FINE") + ")",
    )
    assert _by_text(sm.scan_silk_texts(pcb), "CACHED-SMALL")["render_cache"] is True
    result = _apply(pcb, tmp_path)
    assert _by_text(result["changed"], "CACHED-SMALL")["render_cache_removed"] is True
    assert "render_cache" in result["note"]
    nodes = {n.values[0]: n for n in parse_file(result["output_pcb"]).children if n.name == "gr_text"}
    assert nodes["CACHED-SMALL"].find("render_cache") is None
    assert nodes["CACHED-FINE"].find("render_cache") is not None  # untouched text keeps its cache


def test_reused_private_names_exist_in_input_prep() -> None:
    from kicad_mcp import input_prep

    assert all(hasattr(input_prep, n) for n in sm._REUSED_NAMES)


def test_only_unhandled_sub_minimum_text_is_not_nothing_to_do(tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    dest = tmp_path / "proj"
    shutil.copytree(GOLDEN_DIR, dest)
    pcb = dest / GOLDEN_NAME
    b = PCB.load(pcb)
    b._sexp.append(parse_string(
        '(gr_text "NO-THICK" (at 130 20 0) (layer "F.SilkS") (uuid "eeeeeeee-0000-4000-8000-000000000009") '
        "(effects (font (size 0.5 0.5))))"
    ))
    write_layer.save_pcb(b, pcb)
    proposal = sm.propose_silkscreen_minimums(pcb, tmp_path / "rev")
    assert proposal["status"] == "unhandled_only"
    assert proposal["revision_token"] is None and proposal["changes"] == []
    assert [u["text"] for u in proposal["unhandled"]] == ["NO-THICK"]
    assert "remain" in proposal["note"]


def test_text_content_hide_is_not_a_hidden_flag(tmp_path: Path) -> None:
    pcb = _build(tmp_path / "proj")
    _append(
        pcb,
        '(gr_text "hide" (at 160 20 0) (layer "F.SilkS") (uuid "99999999-0000-4000-8000-000000000001") '
        "(effects (font (size 1 1) (thickness 0.15))))",
    )
    assert _by_text(sm.scan_silk_texts(pcb), "hide")["hidden"] is False


def test_bare_hide_atom_kicad_6_7_form_is_hidden(tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    pcb = _build(tmp_path / "proj")
    b = PCB.load(pcb)
    fp = next(n for n in b._sexp.children if n.name == "footprint")
    fp.append(parse_string(
        '(fp_text user "OLDHIDDEN" (at 0 0) (layer "F.SilkS") hide '
        "(effects (font (size 0.5 0.5) (thickness 0.1))))"
    ))
    write_layer.save_pcb(b, pcb)
    rec = _by_text(sm.scan_silk_texts(pcb), "OLDHIDDEN")
    assert rec["hidden"] is True
    result = _apply(pcb, tmp_path)
    assert _by_text(result["changed"], "OLDHIDDEN")["hidden"] is True
    assert _by_text(sm.scan_silk_texts(result["output_pcb"]), "OLDHIDDEN")["hidden"] is True
