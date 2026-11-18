"""Pairwise clearance rules between net classes, proposed and confirmed through board_rules (GitHub issue 131).

Real ``kicad-cli`` (10.0.7) DRC on a small board: two tracks 0.25 mm apart on the nets VCC (class Mains) and GND
(class Sense). Fixtures come from ``tests/_pair_fixture.py`` (built through the project's tools); a hand-written
rules file is made through ``dru.with_pairs`` and the write layer, never by raw text.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from boardproof import board_rules, dru, kicad_cli, write_layer
from tests import _pair_fixture as fx

HAND = (
    '(version 1)\n# hand-written, must survive\n'
    '(rule "Sense gap"\n\t(condition "A.NetClass == \'Sense\' && A.Type == \'Track\'")\n\t(constraint clearance (min 0.2mm)))\n'
)


def digest(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def put_rules(board: Path, text: str) -> None:
    """A hand-written rules file for a fixture: through the write layer (test fixtures never write raw KiCad text)."""
    write_layer.save_bytes(text.encode("utf-8"), board.with_suffix(".kicad_dru"), verify=lambda _p: None)


@pytest.fixture
def board(tmp_path: Path) -> Path:
    return fx.build_pair_board(tmp_path)


def probe_drc(path, **_kwargs):
    """A stand-in DRC that fires the load probe the way KiCad does once the file loaded (tests of the
    checkpoint's own logic; the real runs are the kicad-cli tests)."""
    text = Path(path).with_suffix(".kicad_dru").read_text(encoding="utf-8") if Path(path).with_suffix(".kicad_dru").exists() else ""
    loaded = dru.parse(text)[2] == []
    violations = [{"type": "track_width", "description": 'Rule "boardproof_probe_track"', "items": []}] if loaded else []
    return {**fx.EMPTY_REPORT, "violations": violations}


def apply(board: Path, tmp_path: Path, name: str = "rev", drc=None, **request) -> dict:
    proposal = board_rules.propose_board_rules(board, output_dir=tmp_path / name, **request)
    assert proposal["status"] == "proposed", proposal
    return board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=drc or probe_drc)


def violations_of_pair(pcb: Path) -> list[dict]:
    return fx.pair_violations(kicad_cli.run_drc(pcb))


PAIR = [{"a": "Mains", "b": "Sense", "clearance": 0.6}]


# --- the proposal writes nothing ---------------------------------------------------------------


def test_proposal_lists_the_rule_and_writes_nothing(board: Path, tmp_path: Path) -> None:
    before = digest(board.parent)
    result = board_rules.propose_board_rules(board, pair_clearances=PAIR, output_dir=tmp_path / "rev")
    assert result["status"] == "proposed" and result["revision_token"]
    assert digest(board.parent) == before and not (tmp_path / "rev").exists()
    assert result["pair_clearances"]["before"] == []
    assert result["pair_clearances"]["after"] == [
        {"a": "Mains", "b": "Sense", "clearance_mm": 0.6, "rule": "boardproof pair clearance: Mains / Sense"}
    ]
    assert result["pair_clearances"]["rules_file_exists"] is False
    [change] = [c for c in result["changes"] if c["where"].startswith("pair_clearance")]
    assert change["before"] is None and change["after"] == 0.6 and change["net_class_floor"] == 0.2
    assert result["loosened"] == []


def test_order_of_a_and_b_does_not_matter(board: Path) -> None:
    one = board_rules.propose_board_rules(board, pair_clearances=[{"a": "Sense", "b": "Mains", "clearance": 0.6}])
    assert one["pair_clearances"]["after"][0]["rule"] == "boardproof pair clearance: Mains / Sense"


# --- the confirm writes the rule and KiCad enforces it ---------------------------------------------


def test_confirm_writes_the_rules_file_and_kicad_reports_the_violating_pair(board: Path, tmp_path: Path) -> None:
    assert violations_of_pair(board) == []  # 0.25 mm apart, classes ask for 0.2: clean before
    before = digest(board.parent)
    result = apply(board, tmp_path, pair_clearances=PAIR, drc=kicad_cli.run_drc)
    assert result["status"] == "applied", result
    assert digest(board.parent) == before  # the source is untouched
    out = Path(result["output_pcb"])
    written = out.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert dru.split_block(written)[2] == {("Mains", "Sense"): 0.6}
    assert result["rules_file_loaded_by_kicad"] is True
    found = violations_of_pair(out)
    assert len(found) == 1 and "boardproof pair clearance: Mains / Sense" in found[0]["description"]
    assert "0,6000" in found[0]["description"] or "0.6000" in found[0]["description"]
    assert found[0]["severity"] == "error"
    assert result["baseline_changed"] is True  # the rules file is part of the pinned baseline


def test_a_pair_that_is_far_enough_apart_is_not_reported(board: Path, tmp_path: Path) -> None:
    result = apply(board, tmp_path, pair_clearances=[{"a": "Mains", "b": "Sense", "clearance": 0.25}], drc=kicad_cli.run_drc)
    assert result["status"] == "applied", result
    assert violations_of_pair(Path(result["output_pcb"])) == []


def test_hand_written_rules_survive_byte_for_byte_and_new_rule_comes_last(board: Path, tmp_path: Path) -> None:
    put_rules(board, HAND)
    result = apply(board, tmp_path, pair_clearances=PAIR, drc=kicad_cli.run_drc)
    assert result["status"] == "applied", result
    written = Path(result["output_pcb"]).with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert written.startswith(HAND)
    names = [r.name for r in dru.parse(written)[0]]
    assert names == ["Sense gap", "boardproof pair clearance: Mains / Sense"]
    assert result["custom_rules"][0]["rule"] == "Sense gap"  # the hand-written rule is shown
    assert len(violations_of_pair(Path(result["output_pcb"]))) == 1


def test_crlf_rules_file_keeps_its_line_endings(board: Path, tmp_path: Path) -> None:
    put_rules(board, HAND.replace("\n", "\r\n"))
    result = apply(board, tmp_path, pair_clearances=PAIR, drc=kicad_cli.run_drc)
    raw = Path(result["output_pcb"]).with_suffix(".kicad_dru").read_bytes()
    assert raw.count(b"\n") == raw.count(b"\r\n") and raw.startswith(HAND.replace("\n", "\r\n").encode())


def test_update_and_remove_a_managed_pair(board: Path, tmp_path: Path) -> None:
    first = apply(board, tmp_path, "one", pair_clearances=PAIR + [{"a": "Mains", "b": "Mains", "clearance": 1.0}])
    assert first["status"] == "applied", first
    rev1 = Path(first["output_pcb"])
    second = apply(
        rev1, tmp_path, "two", pair_clearances=[{"a": "Sense", "b": "Mains", "clearance": 0.8}],
        remove_pair_clearances=[{"a": "Mains", "b": "Mains"}], allow_loosen=True,  # removing a rule relaxes (1.0 -> 0.2)
    )
    assert second["status"] == "applied", second
    rev2 = Path(second["output_pcb"])
    assert dru.split_block(rev2.with_suffix(".kicad_dru").read_text(encoding="utf-8"))[2] == {("Mains", "Sense"): 0.8}
    third = apply(rev2, tmp_path, "three", remove_pair_clearances=[{"a": "Mains", "b": "Sense"}], allow_loosen=True)
    assert third["status"] == "applied", third
    text = Path(third["output_pcb"]).with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert dru.BLOCK_BEGIN not in text and text.strip() == "(version 1)"
    assert violations_of_pair(Path(third["output_pcb"])) == []


def test_pairs_and_a_new_class_in_one_request(board: Path, tmp_path: Path) -> None:
    result = apply(
        board, tmp_path,
        netclasses=[{"name": "Mains HV", "clearance": 0.3, "nets": ["+3V3"] if False else []}],
        pair_clearances=[{"a": "Mains HV", "b": "Sense", "clearance": 0.9}],
    )
    assert result["status"] == "applied", result
    written = Path(result["output_pcb"]).with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert dru.split_block(written)[2] == {("Mains HV", "Sense"): 0.9}


# --- refusals ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("request_", "needle"),
    [
        ({"pair_clearances": [{"a": "Mains", "b": "Nope", "clearance": 0.5}]}, "not in the project"),
        ({"pair_clearances": [{"a": "Mains", "b": "Se'nse", "clearance": 0.5}]}, "quote"),
        ({"pair_clearances": [{"a": "Mains", "b": "Sense", "clearance": 0}]}, "clearance must be"),
        ({"pair_clearances": [{"a": "Mains", "b": "Sense", "clearance": 0.123456}]}, "four decimals"),
        ({"pair_clearances": [{"a": "Mains", "b": "Sense", "clearance": float("nan")}]}, "clearance must be"),
        ({"pair_clearances": [{"a": "Mains", "b": "Sense", "clearance": True}]}, "clearance must be"),
        ({"pair_clearances": [{"a": "Mains", "b": "Sense"}]}, "clearance must be"),
        ({"pair_clearances": [{"a": "Mains", "b": "Sense", "clearance": 1, "x": 1}]}, "entries are"),
        ({"pair_clearances": ["Mains"]}, "entries are"),
        ({"pair_clearances": PAIR + [{"a": "Sense", "b": "Mains", "clearance": 0.7}]}, "listed twice"),
        ({"pair_clearances": PAIR, "remove_pair_clearances": [{"a": "Sense", "b": "Mains"}]}, "both set and removed"),
        ({"remove_pair_clearances": [{"a": "Mains", "b": "Sense"}]}, "no managed pair clearance to remove"),
        ({"pair_clearances": []}, "nothing requested"),
    ],
)
def test_bad_requests_are_refused_without_a_token(board: Path, request_, needle) -> None:
    result = board_rules.propose_board_rules(board, **request_)
    assert result["status"] == "error" and needle in result["reason"], result
    assert result.get("revision_token") is None


def test_a_rule_below_what_the_classes_demand_is_a_loosening(board: Path, tmp_path: Path) -> None:
    low = [{"a": "Mains", "b": "Sense", "clearance": 0.1}]  # classes ask for 0.2
    result = board_rules.propose_board_rules(board, pair_clearances=low, output_dir=tmp_path / "rev")
    assert result["status"] == "refused" and result["revision_token"] is None
    [loose] = [x for x in result["loosened"] if x["kind"] == "pair_clearance"]
    assert loose["before"] == 0.2 and loose["after"] == 0.1
    allowed = apply(board, tmp_path, pair_clearances=low, allow_loosen=True)
    assert allowed["status"] == "applied" and allowed["loosened"][0]["after"] == 0.1


def test_lowering_a_managed_pair_is_a_loosening(board: Path, tmp_path: Path) -> None:
    rev = Path(apply(board, tmp_path, "one", pair_clearances=PAIR)["output_pcb"])
    result = board_rules.propose_board_rules(rev, pair_clearances=[{"a": "Mains", "b": "Sense", "clearance": 0.4}])
    assert result["status"] == "refused" and result["loosened"][0]["before"] == 0.6


def test_hand_edited_block_is_never_overwritten(board: Path, tmp_path: Path) -> None:
    rev = Path(apply(board, tmp_path, "one", pair_clearances=PAIR)["output_pcb"])
    dru_file = rev.with_suffix(".kicad_dru")
    text = dru_file.read_text(encoding="utf-8").replace("0.6mm", "0.1mm) (min 0.1mm")
    put_rules(rev, text)
    result = board_rules.propose_board_rules(rev, pair_clearances=[{"a": "Mains", "b": "Sense", "clearance": 0.9}])
    assert result["status"] == "error" and "changed by hand" in result["reason"]


def test_hand_written_clearance_rule_after_the_block_is_warned_about(board: Path, tmp_path: Path) -> None:
    rev = Path(apply(board, tmp_path, "one", pair_clearances=PAIR)["output_pcb"])
    text = rev.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    put_rules(rev, text + '(rule "late" (constraint clearance (min 0.3mm)))\n')
    result = board_rules.propose_board_rules(rev, pair_clearances=[{"a": "Mains", "b": "Sense", "clearance": 0.9}])
    assert result["status"] == "proposed"
    assert any("late" in w and "come after the managed block" in w for w in result["warnings"])


# --- the self-check: a malformed rule never reaches the revision --------------------------------------


def test_existing_rules_file_that_kicad_drops_fails_the_self_check_and_nothing_is_written(board: Path, tmp_path: Path) -> None:
    put_rules(board, '(version 1)\n(rule "broken" (constraint clearance (min 0.5)))\n')  # no unit: KiCad drops the file
    before = digest(board.parent)
    proposal = board_rules.propose_board_rules(board, pair_clearances=PAIR, output_dir=tmp_path / "rev")
    assert proposal["status"] == "proposed"
    result = board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=kicad_cli.run_drc)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert "without a unit" in json.dumps(result)
    assert not (tmp_path / "rev").exists() and digest(board.parent) == before
    # the token stays usable once the rules file is fixed... it does not: the source changed, so it is stale
    assert board_rules.propose_board_rules(board, pair_clearances=PAIR)["status"] == "proposed"


def test_kicad_not_loading_the_written_file_fails_the_self_check(board: Path, tmp_path: Path, monkeypatch) -> None:
    """A defect our own parser cannot see (here an unknown function KiCad rejects) is caught by the probe."""
    real = dru.pair_condition
    monkeypatch.setattr(dru, "pair_condition", lambda a, b: real(a, b).replace("A.NetClass", "A.NetKlass("))
    # the managed block no longer reads back as a canonical block: the reload comparison fires first
    proposal = board_rules.propose_board_rules(board, pair_clearances=PAIR, output_dir=tmp_path / "rev")
    result = board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=kicad_cli.run_drc)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert not (tmp_path / "rev").exists()


def test_wrong_value_on_disk_fails_the_reload_comparison(board: Path, tmp_path: Path, monkeypatch) -> None:
    real = dru.with_pairs
    monkeypatch.setattr(dru, "with_pairs", lambda text, pairs: real(text, {k: v / 2 for k, v in pairs.items()}))
    before = digest(board.parent)
    proposal = board_rules.propose_board_rules(board, pair_clearances=PAIR, output_dir=tmp_path / "rev")
    result = board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=probe_drc)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert result["mismatches"]["items"][0]["item"] == "pair Mains / Sense"
    assert not (tmp_path / "rev").exists() and digest(board.parent) == before


def test_changed_hand_written_text_fails_the_reload_comparison(board: Path, tmp_path: Path, monkeypatch) -> None:
    put_rules(board, HAND)
    real = dru.with_pairs
    monkeypatch.setattr(dru, "with_pairs", lambda text, pairs: real(text.replace("must survive", "altered"), pairs))
    proposal = board_rules.propose_board_rules(board, pair_clearances=PAIR, output_dir=tmp_path / "rev")
    result = board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=probe_drc)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert not (tmp_path / "rev").exists()


def test_probe_that_does_not_fire_fails_the_self_check(board: Path, tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(board, pair_clearances=PAIR, output_dir=tmp_path / "rev")
    result = board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=fx.fake_drc)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert "does not load" in result["reason"] or "does not load" in json.dumps(result)
    assert not (tmp_path / "rev").exists()


def test_a_source_that_changed_since_the_proposal_is_refused(board: Path, tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(board, pair_clearances=PAIR, output_dir=tmp_path / "rev")
    put_rules(board, HAND)
    result = board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=probe_drc)
    assert result["status"] == "error" and "changed since the proposal" in result["reason"]


def test_a_request_without_pairs_does_not_touch_the_rules_file(board: Path, tmp_path: Path) -> None:
    put_rules(board, HAND)
    result = apply(board, tmp_path, min_clearance=0.15)
    assert result["status"] == "applied"
    assert Path(result["output_pcb"]).with_suffix(".kicad_dru").read_bytes() == HAND.encode().replace(b"\n", b"\n")
    assert "pair_clearances" not in result


# --- review round 1 (issue 131) -------------------------------------------------------------------

HAND_2MM = (
    "(version 1)\n"
    '(rule "mains vs sense 2mm"\n\t(condition "A.NetClass == \'Mains\' && B.NetClass == \'Sense\'")\n\t(constraint clearance (min 2mm)))\n'
)


def test_a_pair_rule_below_a_hand_written_rule_is_a_loosening(board: Path, tmp_path: Path) -> None:
    put_rules(board, HAND_2MM)
    low = [{"a": "Mains", "b": "Sense", "clearance": 0.2}]
    result = board_rules.propose_board_rules(board, pair_clearances=low, output_dir=tmp_path / "rev")
    assert result["status"] == "refused" and result["revision_token"] is None
    [loose] = [x for x in result["loosened"] if x["kind"] == "pair_clearance"]
    assert loose["before"] == 2.0 and loose["after"] == 0.2 and loose["before_source"] == "hand-written rule"
    allowed = apply(board, tmp_path, pair_clearances=low, allow_loosen=True)
    assert allowed["status"] == "applied"
    # a value above the hand-written rule is no loosening
    assert board_rules.propose_board_rules(board, pair_clearances=[{"a": "Mains", "b": "Sense", "clearance": 2.5}])["status"] == "proposed"


def test_a_hand_rule_for_other_classes_does_not_count(board: Path) -> None:
    put_rules(board, HAND_2MM.replace("'Sense'", "'Other'"))
    low = [{"a": "Mains", "b": "Sense", "clearance": 0.3}]
    assert board_rules.propose_board_rules(board, pair_clearances=low)["status"] == "proposed"


def test_a_hand_rule_that_cannot_be_evaluated_gives_a_warning(board: Path) -> None:
    put_rules(
        board,
        "(version 1)\n"
        '(rule "tracks" (condition "A.NetClass == \'Mains\' && A.Type == \'Track\'") (constraint clearance (min 2mm)))\n'
        '(rule "by net" (condition "A.NetName == \'VCC\'") (constraint clearance (min 2mm)))\n',
    )
    result = board_rules.propose_board_rules(board, pair_clearances=PAIR)
    assert result["status"] == "proposed"
    [warning] = [w for w in result["warnings"] if "cannot evaluate" in w]
    assert "tracks" in warning and "by net" in warning


def test_removing_a_pair_rule_that_raised_the_clearance_is_a_loosening_without_allow_loosen(board: Path, tmp_path: Path) -> None:
    rev = Path(apply(board, tmp_path, "one", pair_clearances=PAIR)["output_pcb"])
    result = board_rules.propose_board_rules(rev, remove_pair_clearances=[{"a": "Mains", "b": "Sense"}])
    assert result["status"] == "refused" and result["revision_token"] is None
    [loose] = [x for x in result["loosened"] if x["kind"] == "pair_clearance"]
    assert loose["before"] == 0.6 and loose["after"] == 0.2


def test_floor_is_the_larger_of_the_old_and_the_new_class_clearance(board: Path) -> None:
    raised = board_rules.propose_board_rules(
        board,
        netclasses=[{"name": "Mains", "clearance": 0.5}],
        pair_clearances=[{"a": "Mains", "b": "Sense", "clearance": 0.4}],
    )
    assert raised["status"] == "refused"  # the class is raised to 0.5 in the same request: 0.4 would relax it
    [loose] = [x for x in raised["loosened"] if x.get("kind") == "pair_clearance"]
    assert loose["before"] == 0.5
    # the class is lowered in the same request: the OLD value (0.2, Sense) still counts
    lowered = board_rules.propose_board_rules(
        board,
        netclasses=[{"name": "Sense", "clearance": 0.1}],
        pair_clearances=[{"a": "Mains", "b": "Sense", "clearance": 0.15}],
    )
    assert [x for x in lowered["loosened"] if x.get("kind") == "pair_clearance"][0]["before"] == 0.2


@pytest.mark.parametrize(
    ("request_", "needle"),
    [
        ({"pair_clearances": [{"a": "Ma*", "b": "Sense", "clearance": 0.5}]}, "wildcard"),
        ({"pair_clearances": [{"a": "Mains", "b": "mains", "clearance": 0.5}]}, "differ only in case"),
        ({"pair_clearances": [{"a": "MAINS", "b": "Sense", "clearance": 0.5}]}, "differs only in case"),
    ],
)
def test_wildcard_and_case_colliding_class_names_are_refused(board: Path, request_, needle) -> None:
    result = board_rules.propose_board_rules(board, **request_)
    assert result["status"] == "error" and needle in result["reason"], result


def test_a_hand_file_with_a_space_before_the_unit_is_not_falsely_refused(board: Path, tmp_path: Path) -> None:
    put_rules(
        board,
        '(version 1)\n(rule "spaced" (condition "A.NetClass == \'Mains\'") (constraint clearance (min 0.3 mm)))\n',
    )
    result = apply(board, tmp_path, pair_clearances=PAIR, drc=kicad_cli.run_drc)
    assert result["status"] == "applied" and result["rules_file_loaded_by_kicad"] is True


def test_the_probe_overrules_a_parser_defect_that_kicad_does_not_have(board: Path, tmp_path: Path, monkeypatch) -> None:
    put_rules(board, HAND)
    real = dru.parse
    monkeypatch.setattr(dru, "parse", lambda text: (real(text)[0], 1, ["a defect only the parser sees"]))

    def kicad_loads_it(path, **_k):
        return {**fx.EMPTY_REPORT, "violations": [{"type": "track_width", "description": "boardproof_probe_track", "items": []}]}

    result = apply(board, tmp_path, pair_clearances=PAIR, drc=kicad_loads_it)
    assert result["status"] == "applied"  # KiCad (the probe) loaded the file: the parser does not get the last word


def test_a_probe_that_cannot_conclude_leaves_a_visible_warning(board: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dru, "load_probe", lambda *a, **k: {"loaded": None, "reason": "the board has no track, via or hole"})
    result = apply(board, tmp_path, pair_clearances=PAIR)
    assert result["status"] == "applied" and result["rules_file_loaded_by_kicad"] is None
    [warning] = [w for w in result["warnings"] if "could not confirm" in w]
    assert "NOT proven" in warning and "no track, via or hole" in warning


def test_a_probe_that_cannot_conclude_still_refuses_a_defect_the_parser_sees(board: Path, tmp_path: Path, monkeypatch) -> None:
    put_rules(board, '(version 1)\n(rule "broken" (constraint clearance (min 0.5)))\n')
    monkeypatch.setattr(dru, "load_probe", lambda *a, **k: {"loaded": None, "reason": "no target"})
    proposal = board_rules.propose_board_rules(board, pair_clearances=PAIR, output_dir=tmp_path / "rev")
    result = board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=probe_drc)
    assert result["ok"] is False and result["code"] == "write_not_verified" and not (tmp_path / "rev").exists()
