"""Adoption compares the symbol's pin numbers with the footprint's pad numbers (issue 156).

The comparison reads the raw ``(pin ...)`` and ``(pad ...)`` nodes. Real parts come from the KiCad
stock libraries next to ``kicad-cli`` (skipped when they are not installed, a failure under
``KICAD_DEMOS_REQUIRED``); the cases the stock libraries do not hold are built through the write
layer. Mutation checks of every guard are listed in the issue's Answer.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from boardproof import part_adoption as pa
from boardproof import write_layer
from tests import _kicad_demos as demos
from tests import test_part_adoption as base
from tests.test_part_adoption import _part_assets, _propose, needs_cli

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def project(tmp_path: Path) -> Path:
    return _board(tmp_path)


def _board(tmp_path: Path) -> Path:
    from kicad_tools.schema import PCB

    board = PCB.create(width=40.0, height=30.0, title="adopt")
    path = tmp_path / "proj" / "adopt.kicad_pcb"
    path.parent.mkdir()
    write_layer.save_pcb(board, path)
    return path


# --------------------------------------------------------------------------- stock parts


def _share() -> Path:
    from boardproof.kicad_cli import find_kicad_cli

    try:
        share = demos.share_dir_next_to(Path(find_kicad_cli()), "symbols").parent
    except Exception as exc:  # noqa: BLE001
        demos.missing(str(exc), what="KiCad stock libraries")
    if not (share / "symbols").is_dir() or not (share / "footprints").is_dir():
        demos.missing(f"no symbols or footprints next to {share}", what="KiCad stock libraries")
    return share


def _stock_symbol(lib: str, name: str) -> SExp:
    root = write_layer.load(_share() / "symbols" / f"{lib}.kicad_sym")
    return next(c for c in root.children if not c.is_atom and c.name == "symbol" and c.get_string(0) == name)


def _stock_footprint(lib: str, name: str) -> SExp:
    return write_layer.load(_share() / "footprints" / f"{lib}.pretty" / f"{name}.kicad_mod")


MICRO_B = ("Connector", "USB_B_Micro")  # pins 1 to 5 and SH (Shield)


def test_a_matching_stock_part_is_a_match() -> None:
    result = pa._pin_pad_comparison(
        _stock_symbol("Device", "R"), _stock_footprint("Resistor_SMD", "R_0402_1005Metric")
    )
    assert result["verdict"] == "match"
    assert result["pins_without_pad"] == result["pads_without_pin"] == result["mechanical_pads"] == []
    assert [m["number"] for m in result["matched"]] == ["1", "2"]


def test_mounting_holes_are_listed_as_mechanical_not_as_errors() -> None:
    result = pa._pin_pad_comparison(
        _stock_symbol(*MICRO_B), _stock_footprint("Connector_USB", "USB_Micro-B_Technik_TWP-4002D-H3")
    )
    assert result["verdict"] == "match_with_mechanical_pads", result
    assert result["mechanical_pads"] == [
        {"number": "", "types": ["np_thru_hole"], "count": 2, "kind": "mounting_hole"}
    ]
    assert result["pins_without_pad"] == result["pads_without_pin"] == []


def test_stacked_pads_and_the_shield_pin_of_a_stock_part_match() -> None:
    # six pad numbers, the shield pad SH is stacked over several pads: still one number
    result = pa._pin_pad_comparison(
        _stock_symbol(*MICRO_B), _stock_footprint("Connector_USB", "USB_Micro-B_Molex-105017-0001")
    )
    assert result["verdict"] == "match", result
    assert [m["name"] for m in result["matched"]][-1] == "Shield"


def test_a_stock_footprint_with_pads_the_symbol_has_no_pin_for_is_a_mismatch() -> None:
    # measured on the stock libraries: this footprint numbers its shield pads "6" AND "SH"; the
    # symbol's shield pin is "SH", so the two pads numbered 6 have no pin
    result = pa._pin_pad_comparison(
        _stock_symbol(*MICRO_B),
        _stock_footprint("Connector_USB", "USB_Micro-B_Amphenol_10103594-0001LF_Horizontal"),
    )
    assert result["verdict"] == "mismatch"
    assert result["pads_without_pin"] == [{"number": "6", "types": ["smd"], "count": 2}]


# --------------------------------------------------------------------------- built cases


def _sym_with(pins: list[str]) -> SExp:
    return SExp.list(
        "symbol", write_layer.string("X"),
        SExp.list("symbol", write_layer.string("X_1_1"), *[base._pin(n) for n in pins]),
    )  # fmt: skip


def _fp_with(pads: list[tuple[str, str]]) -> SExp:
    return SExp.list(
        "footprint", write_layer.string("X"),
        *[
            SExp.list("pad", write_layer.string(n), write_layer.symbol(t), write_layer.symbol("rect"),
                      SExp.list("at", 0, 0), SExp.list("size", 1, 1))
            for n, t in pads
        ],
    )  # fmt: skip


def test_a_symbol_pin_without_a_pad_is_a_mismatch_with_name_and_alternates() -> None:
    pin = base._pin("3")
    pin.append(SExp.list("alternate", write_layer.string("RX"), write_layer.symbol("input"), write_layer.symbol("line")))
    sym = SExp.list("symbol", write_layer.string("X"), SExp.list("symbol", write_layer.string("X_1_1"), base._pin("1"), pin))
    result = pa._pin_pad_comparison(sym, _fp_with([("1", "smd")]))
    assert result["verdict"] == "mismatch"
    assert result["pins_without_pad"] == [
        {"number": "3", "name": "~", "alternates": ["RX"], "reason": "no pad of this number"}
    ]


def test_a_pin_whose_only_pad_is_a_non_plated_hole_is_a_pin_without_a_pad() -> None:
    result = pa._pin_pad_comparison(_sym_with(["1", "2"]), _fp_with([("1", "smd"), ("2", "np_thru_hole")]))
    assert result["verdict"] == "mismatch"
    assert [p["number"] for p in result["pins_without_pad"]] == ["2"]
    assert result["pins_without_pad"][0]["reason"] == "the only pad of this number is a non-plated hole"


def test_a_numbered_pad_without_a_pin_is_a_mismatch() -> None:
    result = pa._pin_pad_comparison(_sym_with(["1", "2"]), _fp_with([("1", "smd"), ("2", "smd"), ("3", "smd")]))
    assert result["verdict"] == "mismatch"
    assert result["pads_without_pin"] == [{"number": "3", "types": ["smd"], "count": 1}]


@pytest.mark.parametrize("number", ["SH", "sh", "SHIELD", "MH1", "Mount", "SHELL_2"])
def test_a_shield_or_mounting_name_is_mechanical(number: str) -> None:
    result = pa._pin_pad_comparison(_sym_with(["1"]), _fp_with([("1", "smd"), (number, "thru_hole")]))
    assert result["verdict"] == "match_with_mechanical_pads", result
    assert result["mechanical_pads"] == [
        {"number": number, "types": ["thru_hole"], "count": 1, "kind": "shield_or_mounting_pad"}
    ]


@pytest.mark.parametrize("number", ["S1", "7", "EP", "GND", "A1"])
def test_other_unmatched_pad_names_are_never_assumed_mechanical(number: str) -> None:
    result = pa._pin_pad_comparison(_sym_with(["1"]), _fp_with([("1", "smd"), (number, "smd")]))
    assert result["verdict"] == "mismatch", result
    assert [p["number"] for p in result["pads_without_pin"]] == [number]


def test_stacked_pads_and_pins_with_the_same_number_are_one_number() -> None:
    result = pa._pin_pad_comparison(_sym_with(["1", "1", "2"]), _fp_with([("1", "smd"), ("1", "smd"), ("2", "smd")]))
    assert result["verdict"] == "match", result
    assert [(m["number"], m["pads"]) for m in result["matched"]] == [("1", 2), ("2", 1)]


def test_an_unnumbered_copper_pad_is_mechanical_and_a_pin_numbered_empty_is_unmatched() -> None:
    result = pa._pin_pad_comparison(_sym_with(["1"]), _fp_with([("1", "smd"), ("", "smd")]))
    assert result["mechanical_pads"][0]["kind"] == "unnumbered_pad"
    result = pa._pin_pad_comparison(_sym_with(["1", ""]), _fp_with([("1", "smd"), ("", "smd")]))
    assert result["verdict"] == "mismatch"
    assert [p["number"] for p in result["pins_without_pad"]] == [""]


# --------------------------------------------------------------------------- propose and confirm


def _edit(path: Path, change) -> None:
    root = write_layer.load(path)
    change(root)
    path.unlink()
    write_layer.save(root, path)


def _add_pin(part: dict, number: str) -> None:
    def change(root: SExp) -> None:
        symbol = next(c for c in root.children if not c.is_atom and c.name == "symbol")
        next(c for c in symbol.children if not c.is_atom and c.name == "symbol").append(base._pin(number))

    _edit(Path(part["symbol_path"]), change)


def _add_pad(part: dict, number: str, kind: str = "smd") -> None:
    footprint = next(Path(part["footprint_dir"]).glob("*.kicad_mod"))
    _edit(
        footprint,
        lambda root: root.append(
            SExp.list("pad", write_layer.string(number), write_layer.symbol(kind), write_layer.symbol("rect"),
                      SExp.list("at", 3, 0), SExp.list("size", 1, 1))
        ),
    )  # fmt: skip


def test_a_matching_part_proposes_with_a_match_verdict(project: Path, tmp_path: Path) -> None:
    result = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    assert result["status"] == "proposed", result
    assert result["pin_pad_verdict"] == "match"
    assert result["parts"][0]["pin_pad_check"]["verdict"] == "match"
    assert "PIN/PAD MISMATCH" not in result["note"]


def test_a_symbol_with_an_extra_pin_blocks_the_proposal(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")
    _add_pin(part, "3")
    result = _propose(project, [part], tmp_path / "rev")
    assert result["status"] == "blocked"
    assert result["revision_token"] is None
    assert any("pins without a copper pad" in p and "3" in p for p in result["parts"][0]["problems"]), result
    assert result["parts"][0]["pin_pad_check"]["pins_without_pad"][0]["number"] == "3"


def test_every_pin_without_a_pad_is_named_not_five(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")
    for number in "34567890":
        _add_pin(part, number)
    result = _propose(project, [part], tmp_path / "rev")
    problem = next(p for p in result["parts"][0]["problems"] if "pins without a copper pad" in p)
    assert all(f"{n} (" in problem for n in "34567890"), problem


def test_a_footprint_with_an_extra_pad_is_proposed_with_the_mismatch_listed(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")
    _add_pad(part, "3")
    result = _propose(project, [part], tmp_path / "rev")
    assert result["status"] == "proposed", result
    assert result["revision_token"]
    assert result["pin_pad_verdict"] == "mismatch"
    assert "PIN/PAD MISMATCH: C100" in result["note"]
    assert result["parts"][0]["pin_pad_check"]["pads_without_pin"] == [{"number": "3", "types": ["smd"], "count": 1}]


def test_a_shield_pad_is_listed_as_mechanical_and_not_as_a_mismatch(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")
    _add_pad(part, "SH", "thru_hole")
    result = _propose(project, [part], tmp_path / "rev")
    assert result["status"] == "proposed", result
    assert result["pin_pad_verdict"] == "match_with_mechanical_pads"
    assert result["parts"][0]["pin_pad_check"]["mechanical_pads"][0]["kind"] == "shield_or_mounting_pad"
    assert "PIN/PAD MISMATCH" not in result["note"]


def test_the_batch_verdict_is_the_worst_part(project: Path, tmp_path: Path) -> None:
    odd = _part_assets(tmp_path, "C200")
    _add_pad(odd, "9")
    result = _propose(project, [_part_assets(tmp_path, "C100"), odd], tmp_path / "rev")
    assert result["pin_pad_verdict"] == "mismatch"
    assert "C200" in result["note"] and "C100" not in result["note"].split("PIN/PAD MISMATCH:")[1].split(" have")[0]


@needs_cli
def test_confirm_reports_a_pad_without_a_pin_as_a_warning_not_silently(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")
    _add_pad(part, "3")
    proposed = _propose(project, [part], tmp_path / "rev")
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "adopted", result
    assert result["pin_pad_verdict"] == "mismatch"
    check = next(c for c in result["parts"][0]["checks"] if c["check"] == "pin_pad_numbers")
    assert check["status"] == "warn" and "3" in check["detail"]
    assert "PIN/PAD WARNING: C100" in result["note"]


@needs_cli
def test_confirm_of_a_matching_part_passes_the_pin_pad_check(project: Path, tmp_path: Path) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "adopted", result
    check = next(c for c in result["parts"][0]["checks"] if c["check"] == "pin_pad_numbers")
    assert check["status"] == "pass"
    assert result["pin_pad_verdict"] == "match"
    assert "PIN/PAD" not in result["note"]


@needs_cli
def test_confirm_fails_when_the_saved_files_have_a_pin_without_a_pad(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposed = _propose(project, [_part_assets(tmp_path, "C100")], tmp_path / "rev")
    real = pa._pin_pad_comparison

    def lying(symbol: SExp, footprint: SExp) -> dict:  # the proposal saw a match; the saved files do not
        result = real(symbol, footprint)
        return {**result, "pins_without_pad": [{"number": "9", "name": "~", "alternates": [], "reason": "x"}]}

    calls = {"n": 0}

    def after_propose(symbol: SExp, footprint: SExp) -> dict:
        calls["n"] += 1
        return lying(symbol, footprint)

    monkeypatch.setattr(pa, "_pin_pad_comparison", after_propose)
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    assert any(p["check"] == "pin_pad_numbers" for p in result["problems"])
    assert not (tmp_path / "rev").exists()
    assert calls["n"] >= 1


# --------------------------------------------------------------------------- tool text


def _strings(name: str) -> list[str]:
    tree = ast.parse((REPO / "src" / "boardproof" / "toolsets" / name).read_text(encoding="utf-8"))
    return [" ".join(n.value.split()) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _having(name: str, *needles: str) -> list[str]:
    return [s for s in _strings(name) if all(n in s for n in needles)]


def test_the_propose_description_says_what_is_compared() -> None:
    assert _having(
        "library.py",
        "Dry run of adopting a LIST of parts",
        "pin_pad_check compares the symbol's pin NUMBERS with the footprint's pad NUMBERS",
        "a symbol pin without a copper pad rejects the part",
        "pads_without_pin",
        "pin_pad_verdict",
        "mechanical_pads",
        "alternate pin names are shown",
    )


def test_the_confirm_description_names_the_pin_pad_check() -> None:
    assert _having(
        "library.py",
        "Apply a propose_adopt_parts proposal",
        "pin_pad_numbers (symbol pin numbers against footprint pad numbers",
        "a pad without a pin is status warn",
    )


def test_the_skill_tells_the_agent_to_show_the_list_before_confirming() -> None:
    skill = " ".join((REPO / ".claude" / "skills" / "kicad-sourcing" / "SKILL.md").read_text(encoding="utf-8").split())
    assert "Show the user every entry of `pin_pad_check.pads_without_pin`" in skill
    assert "before `confirm_adopt_parts`" in skill
    assert "report `pin_pad_verdict: mismatch` after it" in skill
