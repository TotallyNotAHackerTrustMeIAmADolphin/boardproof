"""The clearance coverage report (src/boardproof/clearance_coverage.py; GitHub issue 131).

What KiCad 10.0.7 does, measured with ``kicad-cli pcb drc`` (docs/ARCHITECTURE.md "Pair clearance rules"), and what
the report must say about it: every net pair with the rule that applies, the last matching rule winning, a rule
replacing the class clearance, and a rules file KiCad drops (one defect) never claimed to apply.
"""

from __future__ import annotations

import itertools
import shutil
from pathlib import Path

import pytest

from boardproof import board_rules, clearance_coverage, kicad_cli, write_layer
from tests import _pair_fixture as fx
from tests.test_pair_clearance_rules import HAND, probe_drc, put_rules


@pytest.fixture
def board(tmp_path: Path) -> Path:
    return fx.build_pair_board(tmp_path)


def coverage(board: Path, **kw) -> dict:
    kw.setdefault("check_loading", False)
    kw.setdefault("limit", clearance_coverage.MAX_LIMIT)
    return clearance_coverage.report_clearance_coverage(board, **kw)


def pair(report: dict, a: str, b: str) -> dict:
    [item] = [i for i in report["net_pairs"]["items"] if {i["a"], i["b"]} == {a, b}]
    return item


def test_every_net_pair_is_named_exactly_once_with_a_rule_or_class_source(board: Path) -> None:
    report = coverage(board)
    nets = sorted(board_rules._board_net_names(board))
    expected = {frozenset(p) for p in itertools.combinations(nets, 2)}
    items = report["net_pairs"]["items"]
    assert report["net_pairs"]["total"] == len(expected) == len(items) and report["net_pairs"]["all_listed"] is True
    assert {frozenset((i["a"], i["b"])) for i in items} == expected
    assert report["status"] == "complete" and report["rules_file"] is None
    assert {i["source"] for i in items} <= {"netclass", "board_minimum"}
    vcc_gnd = pair(report, "VCC", "GND")
    assert (vcc_gnd["a_class"], vcc_gnd["b_class"]) in {("Mains", "Sense"), ("Sense", "Mains")}
    assert vcc_gnd["clearance_mm"] == 0.2 and vcc_gnd["source"] == "netclass"
    assert sum(g["net_pairs"] for g in report["class_pairs"]) == len(expected)  # the class summary is complete too
    by_class = {g["name"]: g["nets"] for g in report["classes"]}
    assert by_class["Mains"] == ["VCC"] and by_class["Sense"] == ["GND"]


def test_paging_walks_the_whole_list_without_gaps(board: Path) -> None:
    seen: list[tuple[str, str]] = []
    offset = 0
    while offset is not None:
        page = coverage(board, offset=offset, limit=50)["net_pairs"]
        assert page["returned"] <= 50
        seen += [(i["a"], i["b"]) for i in page["items"]]
        offset = page["next_offset"]
    assert len(seen) == len(set(seen)) == coverage(board)["net_pairs"]["total"]


@pytest.mark.parametrize("kw", [{"offset": -1}, {"limit": 0}, {"limit": 10**6}, {"offset": True}, {"limit": "5"}])
def test_bad_paging_is_refused(board: Path, kw) -> None:
    assert coverage(board, **kw)["status"] == "error"


def test_missing_board_or_project_is_an_error(tmp_path: Path, board: Path) -> None:
    assert coverage(tmp_path / "nope.kicad_pcb")["status"] == "error"
    lone = tmp_path / "lone.kicad_pcb"
    shutil.copyfile(board, lone)
    assert "no project file" in coverage(lone)["reason"]


def test_a_confirmed_pair_rule_is_reported_and_agrees_with_kicad(board: Path, tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(
        board, pair_clearances=[{"a": "Mains", "b": "Sense", "clearance": 0.6}], output_dir=tmp_path / "rev"
    )
    done = board_rules.confirm_board_rules(board, proposal["revision_token"], parity="required", drc=kicad_cli.run_drc)
    out = Path(done["output_pcb"])
    report = coverage(out, check_loading=True)
    assert report["rules_file_loaded_by_kicad"] is True and report["status"] == "complete"
    item = pair(report, "VCC", "GND")
    assert item["source"] == "rule" and item["rule"] == "boardproof pair clearance: Mains / Sense"
    assert item["clearance_mm"] == 0.6 and item["managed_by_boardproof"] is True and item["checked"] is True
    # KiCad reports exactly this pair, naming the same rule and value
    [violation] = fx.pair_violations(kicad_cli.run_drc(out))
    assert item["rule"] in violation["description"]
    # pairs the rule does not name keep their class value
    assert pair(report, "VCC", "+3V3" if False else "Net-(D1-K)")["source"] in {"netclass", "board_minimum"}
    [rule] = report["rules"]
    assert rule["managed_by_boardproof"] is True and rule["constraints"] == [{"kind": "clearance", "min_mm": 0.6}]


def test_a_rule_replaces_the_class_value_even_downwards(board: Path, tmp_path: Path) -> None:
    result = board_rules.propose_board_rules(
        board, pair_clearances=[{"a": "Mains", "b": "Sense", "clearance": 0.1}], allow_loosen=True, output_dir=tmp_path / "r"
    )
    done = board_rules.confirm_board_rules(board, result["revision_token"], parity="required", drc=probe_drc)
    report = coverage(Path(done["output_pcb"]))
    assert pair(report, "VCC", "GND")["clearance_mm"] == 0.1  # measured: 0.25 mm gaps pass where the class asks 0.8


def test_last_matching_rule_wins_and_ignore_severity_means_not_checked(board: Path) -> None:
    put_rules(
        board,
        '(version 1)\n'
        '(rule "first" (condition "A.NetClass == \'Mains\' && B.NetClass == \'Sense\'") (constraint clearance (min 0.9mm)))\n'
        '(rule "second" (condition "A.NetClass == \'Mains\' && B.NetClass == \'Sense\'") (constraint clearance (min 0.4mm)))\n',
    )
    assert pair(coverage(board), "VCC", "GND")["rule"] == "second"
    put_rules(
        board,
        '(version 1)\n'
        '(rule "quiet" (severity ignore) (condition "A.NetClass == \'Mains\' && B.NetClass == \'Sense\'") (constraint clearance (min 0.4mm)))\n',
    )
    item = pair(coverage(board), "VCC", "GND")
    assert item["rule"] == "quiet" and item["checked"] is False


def test_rule_with_unmodelled_condition_is_never_guessed(board: Path) -> None:
    put_rules(
        board,
        '(version 1)\n(rule "tracks only" (condition "A.NetClass == \'Mains\' && A.Type == \'Track\'") (constraint clearance (min 0.9mm)))\n',
    )
    report = coverage(board)
    item = pair(report, "VCC", "GND")
    assert item["source"] == "netclass" and item["may_be_overridden_by"] == ["tracks only"]
    assert report["status"] == "partial" and any("tracks only" in r for r in report["partial_reasons"])
    assert "may_be_overridden_by" not in pair(report, "GND", "Net-(D1-K)")  # the rule cannot touch this pair


def test_a_layer_limited_rule_is_not_claimed_for_the_whole_pair(board: Path) -> None:
    put_rules(
        board,
        '(version 1)\n(rule "outer" (layer "outer") (condition "A.NetClass == \'Mains\'") (constraint clearance (min 0.9mm)))\n',
    )
    assert pair(coverage(board), "VCC", "GND")["may_be_overridden_by"] == ["outer"]


def test_a_rules_file_with_a_defect_is_reported_as_ignored_by_kicad(board: Path) -> None:
    put_rules(
        board,
        '(version 1)\n(rule "broken" (condition "A.NetClass == \'Mains\' && B.NetClass == \'Sense\'") (constraint clearance (min 0.9)))\n',
    )
    report = coverage(board)  # no kicad-cli needed: the unit-less length is seen by the parser
    assert report["rules_ignored_by_kicad"] is True and report["status"] == "partial"
    assert any("without a unit" in p for p in report["rules_file_problems"])
    assert pair(report, "VCC", "GND")["source"] == "netclass"  # what applies WITHOUT the file
    assert [r["name"] for r in report["rules"]] == ["broken"]  # but the file is still shown


def test_kicad_itself_decides_whether_the_file_loads(board: Path) -> None:
    put_rules(  # no defect a parser sees: the constraint name is unknown to KiCad
        board,
        '(version 1)\n(rule "typo" (condition "A.NetClass == \'Mains\'") (constraint clearence (min 0.9mm)))\n'
        '(rule "real" (condition "A.NetClass == \'Mains\' && B.NetClass == \'Sense\'") (constraint clearance (min 0.9mm)))\n',
    )
    report = coverage(board, check_loading=True)
    assert report["rules_file_loaded_by_kicad"] is False and report["rules_ignored_by_kicad"] is True
    assert pair(report, "VCC", "GND")["source"] == "netclass"
    put_rules(board, HAND)
    assert coverage(board, check_loading=True)["rules_file_loaded_by_kicad"] is True


def test_probe_that_cannot_run_is_reported_not_hidden(board: Path) -> None:
    put_rules(board, HAND)

    def broken(*_a, **_k):
        raise kicad_cli.KicadCliError("no kicad-cli here")

    report = clearance_coverage.report_clearance_coverage(board, check_loading=True, drc=broken, limit=5)
    assert report["rules_file_loaded_by_kicad"] is None and report["status"] == "partial"
    assert "could not run" in report["rules_file_probe"]


def test_the_report_never_writes(board: Path) -> None:
    import hashlib

    def tree() -> dict:
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in board.parent.iterdir() if p.is_file()}

    put_rules(board, HAND)
    before = tree()
    clearance_coverage.report_clearance_coverage(board, check_loading=True, drc=probe_drc, limit=5)
    assert tree() == before
    assert write_layer  # imported for the put_rules fixture path


# --- review round 1 (issue 131) -------------------------------------------------------------------


def test_a_lowercase_hand_rule_wins_like_in_kicad(board: Path) -> None:
    put_rules(
        board,
        '(version 1)\n(rule "lower" (condition "A.NetClass == \'mains\' && B.NetClass == \'SENSE\'") (constraint clearance (min 0.7mm)))\n',
    )
    item = pair(coverage(board), "VCC", "GND")
    assert item["rule"] == "lower" and item["clearance_mm"] == 0.7  # measured: == ignores case for NetClass


def test_hasnetclass_stays_case_sensitive(board: Path) -> None:
    put_rules(
        board,
        '(version 1)\n(rule "lower" (condition "A.hasNetclass(\'mains\') && B.hasNetclass(\'sense\')") (constraint clearance (min 0.7mm)))\n',
    )
    assert pair(coverage(board), "VCC", "GND")["source"] == "netclass"


def test_a_later_definite_rule_clears_the_earlier_unknown_one(board: Path) -> None:
    put_rules(
        board,
        "(version 1)\n"
        '(rule "unknown first" (condition "A.NetClass == \'Mains\' && A.Type == \'Track\'") (constraint clearance (min 0.9mm)))\n'
        '(rule "definite later" (condition "A.NetClass == \'Mains\' && B.NetClass == \'Sense\'") (constraint clearance (min 0.4mm)))\n',
    )
    item = pair(coverage(board), "VCC", "GND")
    assert item["rule"] == "definite later" and "may_be_overridden_by" not in item  # the later rule wins over the earlier unknown
    put_rules(
        board,
        "(version 1)\n"
        '(rule "definite first" (condition "A.NetClass == \'Mains\' && B.NetClass == \'Sense\'") (constraint clearance (min 0.4mm)))\n'
        '(rule "unknown later" (condition "A.NetClass == \'Mains\' && A.Type == \'Track\'") (constraint clearance (min 0.9mm)))\n',
    )
    assert pair(coverage(board), "VCC", "GND")["may_be_overridden_by"] == ["unknown later"]


def test_the_probe_decides_over_the_parser_when_it_ran(board: Path) -> None:
    put_rules(
        board,
        '(version 1)\n(rule "odd" (condition "A.NetClass == \'Mains\' && B.NetClass == \'Sense\'") (constraint clearance (min 0.9)))\n',
    )

    def kicad_loads_it(path, **_k):
        return {**fx.EMPTY_REPORT, "violations": [{"type": "track_width", "description": "boardproof_probe_track", "items": []}]}

    report = clearance_coverage.report_clearance_coverage(board, check_loading=True, drc=kicad_loads_it, limit=2000)
    assert report["rules_file_loaded_by_kicad"] is True and report["rules_ignored_by_kicad"] is False
    assert pair(report, "VCC", "GND")["rule"] == "odd"  # what KiCad loaded is what applies
    unchecked = coverage(board)  # without the probe the parser's defect stands
    assert unchecked["rules_ignored_by_kicad"] is True
