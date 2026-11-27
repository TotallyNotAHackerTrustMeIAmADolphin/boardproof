"""Issue 269: move_symbols wire_mode "elbow" and "stub", add_wires auto_junction.
Fixture and helpers are those of test_sheet_editing (R2 at (76.2, 50.8), pins at y 46.99 and 54.61, vertical stubs)."""
from __future__ import annotations

from boardproof import sheet_editing
from tests import test_sheet_editing as base
from tests.test_sheet_editing import _bytes, _nets, _nodes, _wire

sheet = base.sheet   # the fixtures of test_sheet_editing, by name
tools = base.tools
_need_kicad_cli = base._need_kicad_cli


def _move(sheet, offset, mode, ref="R2", move_wires=True):
    return sheet_editing.move("symbol", str(sheet), [{"references": [ref], "offset": offset}], None, move_wires=move_wires, snap=True,
                              grid=1.27, allow_connectivity_change=False, dry_run=False, wire_mode=mode)


def _segments(sch):
    return {tuple(sorted(tuple(round(v, 3) for v in (xy.get_float(0), xy.get_float(1))) for xy in w.find_children("pts")[0].find_children("xy")))
            for w in _nodes(sch, "wire")}


def test_elbow_replaces_the_slanted_wire_by_a_right_angle_jog_and_keeps_the_nets(sheet):
    before = _nets(sheet)
    result = _move(sheet, [2.54, 0], "elbow")
    assert result["ok"] is True and result["slanted"] == [], result
    assert len(result["elbows"]) == 2   # the two stubs at R2's pins
    for w in _segments(sheet):
        assert w[0][0] == w[1][0] or w[0][1] == w[1][1], w   # every wire horizontal or vertical
    assert _nets(sheet) == before


def test_drag_mode_still_slants_and_lists_no_elbows(sheet):
    result = _move(sheet, [2.54, 0], "drag")
    assert len(result["slanted"]) >= 1 and result["elbows"] == [] and result["stubs"] == []


def test_stub_keeps_the_old_wires_and_draws_a_wire_from_each_old_pin_to_the_moved_pin(sheet):
    before = _nets(sheet)
    result = _move(sheet, [2.54, 0], "stub")
    assert result["ok"] is True and result["slanted"] == [], result
    assert {tuple(s["from"]) for s in result["stubs"]} == {(76.2, 46.99), (76.2, 54.61)}
    assert {tuple(s["to"]) for s in result["stubs"]} == {(78.74, 46.99), (78.74, 54.61)}
    segs = _segments(sheet)
    assert ((76.2, 46.99), (78.74, 46.99)) in segs and ((76.2, 54.61), (78.74, 54.61)) in segs
    assert _nets(sheet) == before


def test_stub_refuses_a_diagonal_move_and_writes_nothing(sheet):
    before = _bytes(sheet)
    result = _move(sheet, [2.54, 2.54], "stub")
    assert result["ok"] is False and result["code"] == "invalid_spec" and "axis" in result["reason"], result
    assert _bytes(sheet) == before


def test_unknown_wire_mode_or_a_mode_without_move_wires_refuses(sheet):
    before = _bytes(sheet)
    assert _move(sheet, [2.54, 0], "wiggle")["code"] == "invalid_spec"
    assert _move(sheet, [2.54, 0], "stub", move_wires=False)["code"] == "invalid_spec"
    assert _bytes(sheet) == before


def test_stub_along_the_wire_is_a_straight_stub(sheet):
    result = _move(sheet, [0, 2.54], "stub", ref="R1")
    assert result["ok"] is True and result["stubs"]
    for s in result["stubs"]:
        assert s["from"][0] == s["to"][0]


def test_add_wires_auto_junction_adds_one_where_an_end_lands_on_the_middle_of_a_wire(sheet, tools):
    n = len(_nodes(sheet, "junction"))
    off = tools["add_wires"](str(sheet), [_wire((63.5, 40.64), (63.5, 30.48))])
    assert off["status"] == "ok" and len(_nodes(sheet, "junction")) == n and "junctions_added" not in off
    away = tools["add_wires"](str(sheet), [_wire((88.9, 40.64), (88.9, 30.48))], auto_junction=True)
    assert away["junctions_added"] == []   # off every wire: none
    on = tools["add_wires"](str(sheet), [_wire((57.15, 40.64), (57.15, 30.48))], auto_junction=True)
    assert on["junctions_added"] == [[57.15, 40.64]] and len(_nodes(sheet, "junction")) == n + 1
    again = tools["add_wires"](str(sheet), [_wire((57.15, 40.64), (57.15, 25.4))], auto_junction=True)
    assert again["junctions_added"] == [] and len(_nodes(sheet, "junction")) == n + 1   # one is there already


def test_add_wires_auto_junction_must_be_a_bool(sheet, tools):
    before = _bytes(sheet)
    assert tools["add_wires"](str(sheet), [_wire((63.5, 40.64), (63.5, 30.48))], auto_junction="yes")["status"] == "incomplete"
    assert _bytes(sheet) == before
