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
        ('(rule "r" (constraint clearance (min 0.5mm)))\n(version 1)\n', "no (version N)"),  # measured: load_probe False
        ('# note\n\n  (version 1)\n(rule "r" (constraint clearance (min 0.5)))\n', "without a unit"),
        ('(version 1)\n(rule "r" (constraint clearance (min 0.5mm))\n', "never closed"),
        ('(version 1)\n(rule "r" (constraint clearance (min 0.5mm))))\n', "closing parenthesis"),
        ('(version 1)\n(rule "r\n', "never closed"),
        ("(version 1)\n(rule (constraint clearance (min 1mm)))\n", "without a name"),
    ],
)
def test_defects_that_make_kicad_drop_the_file_are_reported(text, needle):
    _rules, _version, problems = dru.parse(text)
    assert any(needle in p for p in problems), problems


_C = "(constraint clearance (min 0.5mm))"
_V = "(version 1)\n"
_R = f'(rule "r" {_C})\n'

# Each form was probed with dru.load_probe on kicad-cli 10 (issue 251): loaded False, and no other defect names it.
_KICAD_REFUSES = {
    "top-level unknown item": _V + "(foo)\n" + _R,
    "top-level unknown item after a rule": _V + _R + "(foo)\n",
    "top-level bare atom": _V + "foo\n" + _R,
    "top-level string": _V + '"foo"\n' + _R,
    "top-level empty list": _V + "()\n" + _R,
    "bare atom in a rule": _V + f'(rule "r" foo {_C})\n',
    "string in a rule": _V + f'(rule "r" "foo" {_C})\n',
    "unknown head in a rule": _V + f'(rule "r" (foo bar) {_C})\n',
    "empty list in a rule": _V + f'(rule "r" () {_C})\n',
    "priority": _V + f'(rule "r" (priority 3) {_C})\n',
    "comments": _V + f'(rule "r" (comments "x") {_C})\n',
    "second name": _V + f'(rule "r" "s" {_C})\n',
    "upper-case rule": _V + f'(RULE "r" {_C})\n',
    "upper-case version": "(VERSION 1)\n" + _R,
    "upper-case constraint": _V + '(rule "r" (CONSTRAINT clearance (min 0.5mm)))\n',
    "upper-case condition": _V + f'(rule "r" (CONDITION "A.NetClass == \'x\'") {_C})\n',
    "upper-case min": _V + '(rule "r" (constraint clearance (MIN 0.5mm)))\n',
    "upper-case severity word": _V + f'(rule "r" (severity Warning) {_C})\n',
    "severity unknown": _V + f'(rule "r" (severity bogus) {_C})\n',
    "severity without value": _V + f'(rule "r" (severity) {_C})\n',
    "severity two values": _V + f'(rule "r" (severity warning error) {_C})\n',
    "layer without value": _V + f'(rule "r" (layer) {_C})\n',
    "layer two values": _V + f'(rule "r" (layer outer inner) {_C})\n',
    "layer keyword quoted": _V + f'(rule "r" (layer "outer") {_C})\n',
    "layer twice": _V + f'(rule "r" (layer outer) (layer inner) {_C})\n',
    "condition without value": _V + f'(rule "r" (condition) {_C})\n',
    "condition as bare atom": _V + f'(rule "r" (condition x) {_C})\n',
    "condition with two values": _V + f'(rule "r" (condition "A.NetClass == \'x\'" "y") {_C})\n',
    "constraint without type": _V + '(rule "r" (constraint))\n',
    "constraint with a number type": _V + '(rule "r" (constraint 5))\n',
    "constraint bare min": _V + '(rule "r" (constraint clearance min 0.5mm))\n',
    "skew option as a bare atom": _V + '(rule "r" (constraint skew (max 1mm) within_diff_pairs))\n',
    "length without a unit": _V + '(rule "r" (constraint length (max 50)))\n',
    "skew without a unit": _V + '(rule "r" (constraint skew (max 1)))\n',
    "diff_pair_uncoupled without a unit": _V + '(rule "r" (constraint diff_pair_uncoupled (max 2)))\n',
    "version with two numbers": "(version 1 2)\n" + _R,
    "version as string": '(version "1")\n' + _R,
    "later version with two numbers": _V + _R + "(version 1 2)\n",
    "later version without number": _V + _R + "(version)\n",
    "comment after code": _V + _R.rstrip() + " # hi\n",
    "comment after the version": "(version 1) # hi\n" + _R,
    "comment inside a rule after code": _V + '(rule "r" # hi\n' + _C + ")\n",
    "byte order mark": "﻿" + _V + _R,
}
# Probed loaded True: a form KiCad accepts must stay unflagged (no false "?" entry).
_KICAD_ACCEPTS = {
    "clean": _V + _R,
    "comment on its own line, top and in a rule": _V + "# hi\n" + '(rule "r"\n# hi\n' + _C + ")\n",
    "comment inside a constraint": _V + '(rule "r"\n(constraint clearance\n# x\n(min 0.5mm)))\n',
    "layers and severities": _V + f'(rule "r" (layer outer) (severity ignore) {_C})\n(rule "q" (layer "F.Cu") (severity exclusion) {_C})\n',
    "two severities": _V + f'(rule "r" (severity warning) (severity error) {_C})\n',
    "two constraints, min max opt": _V + '(rule "r" (constraint clearance (min 0.5mm) (max 1mm) (opt 1mm)) (constraint track_width (min 0.1mm)))\n',
    "constraint without a value": _V + '(rule "r" (constraint clearance))\n',
    "disallow": _V + '(rule "r" (constraint disallow track))\n',
    "rule without a constraint, or without anything": _V + '(rule "r" (condition "A.NetClass == \'x\'"))\n(rule "q")\n',
    "empty and bare-atom names": _V + f'(rule "" {_C})\n(rule r {_C})\n',
    "versions": "(version 2)\n" + _R + _V,
    "second version at the start": _V + _V + _R,
    **{
        f"constraint type {kind}": _V + f'(rule "r" (constraint {kind} {args}))\n'
        for kind, args in {
            **dict.fromkeys(
                ("clearance", "creepage", "hole_clearance", "hole_to_hole", "edge_clearance", "courtyard_clearance",
                 "silk_clearance", "physical_clearance", "physical_hole_clearance", "track_width", "annular_width",
                 "via_diameter", "hole_size", "text_height", "text_thickness", "thermal_relief_gap",
                 "thermal_spoke_width", "connection_width", "solder_mask_expansion", "solder_paste_abs_margin",
                 "diff_pair_gap"), "(min 0.5mm)"),
            "length": "(max 50mm)", "skew": "(max 1mm)", "via_count": "(max 2)", "diff_pair_uncoupled": "(max 2mm)",
            "bridged_mask": "", "min_resolved_spokes": "2", "zone_connection": "solid",
            "assertion": '"A.Length < 5mm"',
        }.items()
    },
    # KiCad source (drc_rule_parser.cpp, parseConstraint): the generic path takes (min|max|opt <expression>) items,
    # (within_diff_pairs) for skew only; track_angle reads a bare number as degrees; via_count, via_dangling and
    # bridged_mask are unitless. Each probed loaded True on kicad-cli 10.
    "skew with its option": _V + '(rule "r" (constraint skew (max 1mm) (within_diff_pairs)))\n',
    "track_angle min": _V + '(rule "r" (constraint track_angle (min 45)))\n',
    "track_angle max": _V + '(rule "r" (constraint track_angle (max 45)))\n',
    "via_dangling": _V + '(rule "r" (constraint via_dangling (max 1)))\n',
    "bridged_mask with a number": _V + '(rule "r" (constraint bridged_mask (min 1)))\n',
    "track_segment_length": _V + '(rule "r" (constraint track_segment_length (min 1mm)))\n',
    "solder_mask_sliver": _V + '(rule "r" (constraint solder_mask_sliver (min 0.1mm)))\n',
    "zone_connection none": _V + '(rule "r" (constraint zone_connection none))\n',
    "zone_connection thermal_reliefs": _V + '(rule "r" (constraint zone_connection thermal_reliefs))\n',
    "assertion with a bare atom": _V + '(rule "r" (constraint assertion x))\n',
    "disallow quoted item": _V + '(rule "r" (constraint disallow "track"))\n',
    "disallow every item": _V + '(rule "r" (constraint disallow track via micro_via buried_via pad zone text graphic hole footprint))\n',
    "escaped quote in a condition": _V + '(rule "r" (condition "A.NetName == \'x\\"y\'") ' + _C + ")\n",
    "version with a fraction": "(version 1.5)\n" + _R,
    "CRLF": (_V + _R).replace("\n", "\r\n"),
    "comments, blank lines and indentation before the version": "# note\n\n  (version 1)\n" + _R,
}


@pytest.mark.parametrize("text", list(_KICAD_REFUSES.values()), ids=list(_KICAD_REFUSES))
def test_file_shapes_kicad_ignores_as_a_whole_are_reported(text):
    assert dru.parse(text)[2], "KiCad drops this file (probed) and parse() names no defect"


@pytest.mark.parametrize("text", list(_KICAD_ACCEPTS.values()), ids=list(_KICAD_ACCEPTS))
def test_file_shapes_kicad_loads_are_not_reported(text):
    assert dru.parse(text)[2] == []


def test_probe_agrees_with_the_shape_lists(tmp_path):  # the ground truth behind both lists (kicad-cli 10)
    board = fx.build_pair_board(tmp_path, classes=False)
    for label, text in {**_KICAD_REFUSES, **_KICAD_ACCEPTS}.items():
        expected = label in _KICAD_ACCEPTS or text in _KICAD_ACCEPTS.values()
        assert dru.load_probe(board, text, kicad_cli.run_drc)["loaded"] is expected, label


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
    # comments and blank lines before (version N) are fine for KiCad and parse alike
    spaced = '# note\n\n  (version 1)\n(rule "r" (constraint clearance (min 0.5mm)))\n'
    assert _probe(board, spaced)["loaded"] is True and dru.parse(spaced)[2] == []
    defects = {
        "unit": '(version 1)\n(rule "r" (constraint clearance (min 0.5)))\n',
        "paren": '(version 1)\n(rule "r" (constraint clearance (min 0.5mm))\n',
        "constraint": '(version 1)\n(rule "r" (constraint clearence (min 0.5mm)))\n',
        "condition": '(version 1)\n(rule "r" (condition "A.NetClass ==== ") (constraint clearance (min 0.5mm)))\n',
        "function": '(version 1)\n(rule "r" (condition "A.foo(\'x\')") (constraint clearance (min 0.5mm)))\n',
        "version": '(rule "r" (constraint clearance (min 0.5mm)))\n',
        "version after a rule": '(rule "r" (constraint clearance (min 0.5mm)))\n(version 1)\n',
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


def test_a_byte_order_mark_is_reported_once_as_the_bare_atom_it_is():
    problems = dru.parse("\uFEFF" + _V + _R)[2]
    assert len(problems) == 1 and "neither (version N) nor (rule" in problems[0]
