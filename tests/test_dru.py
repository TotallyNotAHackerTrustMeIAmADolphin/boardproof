"""The rules-file module (src/boardproof/dru.py; GitHub issue 131): tokenizer defects, rule parsing, the
condition evaluator, the managed block and the KiCad load probe against real ``kicad-cli``."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from boardproof import dru, kicad_cli
from tests import _pair_fixture as fx

NC = dru.NetCtx


def test_parse_reads_rules_conditions_and_units():
    text = (
        "(version 1)\n# a comment (with parens)\n"
        '(rule "Sense gap"\n\t(condition "A.NetClass == \'Sense\'")\n\t(constraint clearance (min 0.5mm))\n\t(severity warning))\n'
        '(rule "in" (constraint clearance (min 0.02in) (max 1mm)))\n'
    )
    rules, version, problems = dru.parse(text)
    assert version == 1 and problems == []
    assert [r.name for r in rules] == ["Sense gap", "in"]
    assert rules[0].condition == "A.NetClass == 'Sense'" and rules[0].severity == "warning"
    assert rules[0].clearance().min_mm == 0.5
    assert rules[1].clearance().min_mm == pytest.approx(0.508) and rules[1].clearance().max_mm == 1.0


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        ('(rule "r" (constraint clearance (min 0.5mm)))\n', "no (version N)"),
        ('(version 1)\n(rule "r" (constraint clearance (min 0.5)))\n', "without a unit"),
        ('(version 1)\n(rule "r" (constraint clearance (min 0.5mm))\n', "never closed"),
        ('(version 1)\n(rule "r" (constraint clearance (min 0.5mm))))\n', "closing parenthesis"),
        ('(version 1)\n(rule "r\n', "never closed"),
        ("(version 1)\n(rule (constraint clearance (min 1mm)))\n", "without a name"),
    ],
)
def test_defects_that_make_kicad_drop_the_file_are_reported(text, needle):
    _rules, _version, problems = dru.parse(text)
    assert any(needle in p for p in problems), problems


A, B = NC("n1", ("Mains",)), NC("n2", ("Sense", "Default"))


@pytest.mark.parametrize(
    ("condition", "expected"),
    [
        (None, True),
        ("A.NetClass == 'Mains' && B.NetClass == 'Sense'", True),
        ("A.NetClass == 'Sense' && B.NetClass == 'Mains'", True),  # symmetric: matches() tries both orders
        ("A.NetClass == 'Mains' && B.NetClass == 'Mains'", False),
        ("A.NetClass == 'Sense'", True),  # a constituent of B's class list
        ("B.NetClass == 'Sense,Default'", True),  # the whole effective name
        ("B.NetClass == 'Default,Sense'", False),  # another order is another name (measured)
        ("A.hasNetclass('Mains') && !B.hasNetclass('Mains')", True),
        ("A.NetName == 'n1' || A.NetName == 'zz'", True),
        ("(A.NetClass == 'x' || A.NetClass == 'Mains') && B.NetName != 'n2'", False),
        ("A.NetClass != 'Mains'", True),  # A=n2 in the swapped order
        ("A.NetClass != 'Mains' && B.NetClass != 'Mains'", False),
    ],
)
def test_condition_evaluator(condition, expected):
    assert dru.matches(condition, A, B) is expected


@pytest.mark.parametrize(
    "condition",
    ["A.Type == 'Track'", "A.NetClass == 'Mains' && A.Layer == 'F.Cu'", "A.NetClass == 'Ma*'", "A.insideCourtyard('J1')", "A.NetClass ===", "((("],
)
def test_condition_that_depends_on_what_is_not_modelled_is_unknown(condition):
    assert dru.matches(condition, A, B) is None


def test_unknown_does_not_override_a_definite_false():
    assert dru.matches("A.NetClass == 'zzz' && A.Type == 'Track'", A, B) is False


def test_block_round_trip_keeps_the_rest_byte_for_byte():
    original = '(version 1)\r\n# mine\r\n(rule "keep" (constraint clearance (min 1mm)))\r\n'
    pairs = {("Mains", "Sense"): 0.6, ("Mains", "Mains"): 1.25}
    written = dru.with_pairs(original, pairs)
    assert "\r\n" in written and "\n" not in written.replace("\r\n", "")
    start, end, found, problems = dru.split_block(written)
    assert found == pairs and problems == []
    assert dru.outside_block(written).rstrip() == original.rstrip()
    rules, version, parse_problems = dru.parse(written)
    assert version == 1 and parse_problems == []
    assert [r.managed for r in rules] == [False, True, True]
    assert dru.with_pairs(written, {}) == original  # removing every pair restores the original text
    assert dru.with_pairs(written, {("Mains", "Sense"): 0.7}).count("(rule ") == 2  # one managed + the hand rule


def test_missing_file_becomes_version_plus_block():
    text = dru.with_pairs(None, {("A", "B"): 0.5})
    assert text.startswith("(version 1)\n") and dru.parse(text)[2] == []


def test_hand_edited_block_is_refused_not_overwritten():
    written = dru.with_pairs("(version 1)\n", {("A", "B"): 0.5})
    tampered = written.replace("0.5mm", "0.1mm).(")
    with pytest.raises(ValueError, match="changed by hand"):
        dru.with_pairs(tampered, {("A", "B"): 0.9})
    extra = written.replace(dru.BLOCK_END, '(rule "mine" (constraint clearance (min 9mm)))\n' + dru.BLOCK_END)
    assert dru.split_block(extra)[3]
    broken_markers = written + dru.BLOCK_BEGIN + "\n"
    assert dru.split_block(broken_markers)[3]


@pytest.mark.parametrize("name", ["", " x", "a'b", 'a"b', "a\\b", "a\nb", "a,b", "x" * 201, None, 5])
def test_class_names_a_condition_cannot_carry_are_refused(name):
    assert dru.class_name_problem(name)


def test_class_names_with_spaces_and_unicode_are_fine():
    assert dru.class_name_problem("Mains 230 V") is None and dru.class_name_problem("Netzä") is None


@pytest.mark.parametrize(("value", "ok"), [(0.5, True), (0.2501, True), (0.25001, False), (0.00001, False), (0, False)])
def test_representable_clearances(value, ok):
    assert dru.representable(value) is ok


def test_format_mm_has_no_exponent_and_round_trips():
    for value in (0.1, 0.25, 1.0, 12.3456, 0.0001):
        assert float(dru.format_mm(value)[:-2]) == value and "e" not in dru.format_mm(value)


# --- KiCad itself (kicad-cli 10) -----------------------------------------------------------------


def _probe(board: Path, text: str) -> dict:
    return dru.load_probe(board, text, kicad_cli.run_drc)


def test_probe_says_loaded_for_a_good_file_and_not_loaded_for_each_defect(tmp_path):
    board = fx.build_pair_board(tmp_path, classes=False)
    good = '(version 1)\n(rule "ok" (condition "A.NetClass == \'x\'") (constraint clearance (min 0.5mm)))\n'
    assert _probe(board, good)["loaded"] is True
    defects = {
        "unit": '(version 1)\n(rule "r" (constraint clearance (min 0.5)))\n',
        "paren": '(version 1)\n(rule "r" (constraint clearance (min 0.5mm))\n',
        "constraint": '(version 1)\n(rule "r" (constraint clearence (min 0.5mm)))\n',
        "condition": '(version 1)\n(rule "r" (condition "A.NetClass ==== ") (constraint clearance (min 0.5mm)))\n',
        "function": '(version 1)\n(rule "r" (condition "A.foo(\'x\')") (constraint clearance (min 0.5mm)))\n',
        "version": '(rule "r" (constraint clearance (min 0.5mm)))\n',
    }
    for label, text in defects.items():
        assert _probe(board, text)["loaded"] is False, label


def test_probe_cannot_conclude_on_a_board_without_any_target(tmp_path):
    board = fx.build_pair_board(tmp_path, tracks=False, classes=False)
    assert re.search(r"\(drill\b", board.read_text(encoding="utf-8"))  # the golden board has through holes
    assert _probe(board, "(version 1)\n")["loaded"] is True


# --- review round 1 (issue 131) -------------------------------------------------------------------


def test_netclass_and_netname_compare_without_regard_to_case_but_hasnetclass_does_not():
    mains, sense = NC("VCC", ("Mains",)), NC("GND", ("Sense", "Default"))
    assert dru.matches("A.NetClass == 'mains' && B.NetClass == 'SENSE'", mains, sense) is True  # measured: kicad-cli 10.0.7
    assert dru.matches("B.NetClass == 'sense,default'", mains, sense) is True
    assert dru.matches("A.NetName == 'vcc'", mains, sense) is True
    assert dru.matches("A.hasNetclass('mains')", mains, sense) is False
    assert dru.matches("A.hasNetclass('Mains')", mains, sense) is True


@pytest.mark.parametrize("name", ["Ma*", "a?b", "*", "?"])
def test_wildcards_in_class_names_are_refused(name):
    assert "wildcard" in dru.class_name_problem(name)


def test_a_space_before_the_unit_is_a_length_not_a_defect():
    rules, _version, problems = dru.parse('(version 1)\n(rule "r" (constraint clearance (min 0.9 mm) (max 2 mm)))\n')
    assert problems == [] and rules[0].clearance().min_mm == 0.9 and rules[0].clearance().max_mm == 2.0
    _r, _v, bad = dru.parse('(version 1)\n(rule "r" (constraint clearance (min 0.9)))\n')
    assert any("without a unit" in p for p in bad)


@pytest.mark.parametrize(
    "original", ["(version 1)\n", "(version 1)\n\n", '(version 1)\n(rule "a" (constraint clearance (min 1mm)))\n\n\n']
)
def test_removing_the_block_restores_the_text_exactly(original):
    assert dru.with_pairs(dru.with_pairs(original, {("A", "B"): 0.5}), {}) == original


def test_a_file_without_a_final_line_break_gains_exactly_that_one():
    original = "(version 1)"  # the only text change a pair round trip can make: the final line break
    assert dru.with_pairs(dru.with_pairs(original, {("A", "B"): 0.5}), {}) == original + "\n"


def test_kicad_loads_a_space_before_the_unit(tmp_path):
    board = fx.build_pair_board(tmp_path, classes=False)
    text = '(version 1)\n(rule "r" (condition "A.NetClass == \'x\'") (constraint clearance (min 0.9 mm)))\n'
    assert dru.parse(text)[2] == [] and _probe(board, text)["loaded"] is True
