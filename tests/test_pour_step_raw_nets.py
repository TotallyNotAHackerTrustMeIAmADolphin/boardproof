"""pour_step._resolved_net and its callers read nets through the shared raw reader and fail closed
(build ticket 253, CLAUDE.md hard rule 2): an undeclared net number is never an empty name.
A node with no (net ...) child stays "no net"; (net 0) stays ""."""

from __future__ import annotations

from collections import Counter

import pytest
from kicad_tools.sexp import parse_string

from boardproof import pour_step, raw_geometry, rung_backends, write_layer

OLD_TABLE = '(net 0 "") (net 3 "GND")'


def _root(body: str, table: str = ""):
    return parse_string(f"(kicad_pcb (version 20260206) {table} {body})")


def _node(text: str):
    return parse_string(text)


def test_resolved_net_both_formats_and_no_net():
    table = {0: "", 3: "GND"}
    assert pour_step._resolved_net(_node('(segment (net "VCC"))'), {}) == "VCC"  # KiCad 10
    assert pour_step._resolved_net(_node("(segment (net 3))"), table) == "GND"  # older
    assert pour_step._resolved_net(_node("(segment (net 0))"), table) == ""
    assert pour_step._resolved_net(_node("(segment (net 0))"), {}) == ""
    assert pour_step._resolved_net(_node("(segment (width 1))"), table) is None  # no net node: no net
    assert pour_step._resolved_net(_node('(zone (net 3) (net_name "X"))'), {}) == "X"  # sibling name wins


@pytest.mark.parametrize("table", [{0: "", 3: "GND"}, {}])  # older table, and KiCad 10 (no table)
def test_resolved_net_undeclared_number_fails_closed(table):
    with pytest.raises(raw_geometry.RawGeometryError):
        pour_step._resolved_net(_node("(segment (net 7))"), table)


def test_net_names_undeclared_pad_net_fails_closed():
    root = _root('(footprint "F" (pad "1" smd rect (net 7)))', OLD_TABLE)
    with pytest.raises(raw_geometry.RawGeometryError):
        pour_step._net_names(root)


def test_net_names_kicad10_and_older_still_work():
    assert pour_step._net_names(_root('(footprint "F" (pad "1" smd rect (net "A"))) (via (net "B"))')) == {"A", "B"}
    assert pour_step._net_names(_root("(via (net 3))", OLD_TABLE)) == {"GND"}


def test_zone_info_undeclared_net_fails_closed():
    with pytest.raises(raw_geometry.RawGeometryError):
        pour_step._zone_info(_node("(zone (net 7) (layer F.Cu))"), {3: "GND"})


def test_pad_records_undeclared_net_fails_closed():
    root = _root('(footprint "F" (at 0 0) (pad "1" smd rect (at 0 0) (size 1 1) (layers F.Cu) (net 7)))', OLD_TABLE)
    with pytest.raises(raw_geometry.RawGeometryError):
        pour_step._pad_records(root, ["F.Cu", "B.Cu"])


def test_diff_boards_undeclared_net_is_a_problem_not_a_crash():
    base = _root("", OLD_TABLE)
    cand = _root('(via (at 1 1) (size 0.6) (drill 0.3) (layers F.Cu B.Cu) (net 7) (uuid "v1"))', OLD_TABLE)
    problems = pour_step._diff_boards(base, cand, {"GND"})["problems"]
    assert any("net id 7" in p for p in problems), problems


def _write(tmp_path, body: str, table: str):
    path = tmp_path / "b.kicad_pcb"
    write_layer.save(parse_string(f"(kicad_pcb (version 20241229) (generator pcbnew) {table} {body})"), path)
    return path


def test_pour_zones_undeclared_net_fails_closed(tmp_path):
    path = _write(tmp_path, '(zone (net 7) (layer "F.Cu") (uuid "z1"))', OLD_TABLE)
    with pytest.raises(raw_geometry.RawGeometryError):
        rung_backends.pour_zones(path)


def test_pour_zones_declared_net_unchanged(tmp_path):
    path = _write(tmp_path, '(zone (net 3) (layer "F.Cu") (uuid "z1"))', OLD_TABLE)
    assert [z["net"] for z in rung_backends.pour_zones(path)] == ["GND"]


def test_under_width_items_undeclared_net_refuses(tmp_path):
    path = _write(tmp_path, '(segment (start 0 0) (end 1 0) (width 0.1) (layer "F.Cu") (net 7) (uuid "s1"))', OLD_TABLE)
    with pytest.raises(rung_backends.UnderWidthCheckError):
        rung_backends.under_width_items(path, Counter(), {})
