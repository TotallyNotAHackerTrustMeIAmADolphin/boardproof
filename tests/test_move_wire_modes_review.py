"""Issue 269, review findings F1 to F5: the corner of an elbow, the stub guard, the already slanted wire, the same-call junction,
dry_run and a turned and mirrored symbol. Fixtures are those of test_sheet_editing."""
from __future__ import annotations

from boardproof import sheet_editing, write_layer
from tests import test_sheet_editing as base
from tests.test_move_wire_modes import _move, _segments
from tests.test_sheet_editing import _bytes, _nets, _nodes, _wire

sheet = base.sheet
tools = base.tools
_need_kicad_cli = base._need_kicad_cli


def test_elbow_corner_keeps_the_fixed_ends_direction_vertical_and_horizontal(sheet, tools):
    # vertical wire (76.2, 59.69)-(76.2, 54.61): fixed end (76.2, 59.69), the pin end moves to (78.74, 54.61): corner (fixed.x, moved.y)
    # horizontal wire (91.44, 46.99)-(101.6, 46.99) on C1.1: the pin end moves to (101.6, 49.53): corner (moved.x, fixed.y)
    assert tools["add_wires"](str(sheet), [_wire((91.44, 46.99), (101.6, 46.99))])["status"] == "ok"
    vertical = _move(sheet, [2.54, 0], "elbow")
    assert vertical["ok"] is True, vertical
    assert sorted(e["corner"] for e in vertical["elbows"]) == [[76.2, 46.99], [76.2, 54.61]]
    horizontal = _move(sheet, [0, 2.54], "elbow", ref="C1")
    assert horizontal["ok"] is True and [e["corner"] for e in horizontal["elbows"]] == [[101.6, 46.99]], horizontal
    assert ((91.44, 46.99), (101.6, 46.99)) in _segments(sheet) and ((101.6, 46.99), (101.6, 49.53)) in _segments(sheet)


def test_stub_skips_a_pin_with_nothing_on_its_old_point(sheet):
    before = _nets(sheet)
    result = _move(sheet, [2.54, 0], "stub", ref="C1")   # C1 is wired to nothing
    assert result["ok"] is True and result["stubs"] == [], result
    assert _nets(sheet) == before


def test_elbow_leaves_a_wire_that_was_slanted_before_the_move(sheet, tools):
    assert tools["add_wires"](str(sheet), [_wire((101.6, 54.61), (111.76, 59.69))])["status"] == "ok"
    result = _move(sheet, [0, 2.54], "elbow", ref="C1")
    assert result["ok"] is True and result["elbows"] == [] and len(result["slanted"]) == 1, result


def test_auto_junction_sees_two_wires_of_the_same_call(sheet, tools):
    n = len(_nodes(sheet, "junction"))
    done = tools["add_wires"](str(sheet), [_wire((88.9, 25.4), (101.6, 25.4)), _wire((95.25, 25.4), (95.25, 15.24))], auto_junction=True)
    assert done["junctions_added"] == [[95.25, 25.4]] and len(_nodes(sheet, "junction")) == n + 1


def test_auto_junction_with_dry_run_lists_the_junction_and_writes_nothing(sheet, tools):
    before = _bytes(sheet)
    done = tools["add_wires"](str(sheet), [_wire((57.15, 40.64), (57.15, 30.48))], auto_junction=True, dry_run=True)
    assert done["junctions_added"] == [[57.15, 40.64]] and done["dry_run"] is True and done["written"] is False, done
    assert _bytes(sheet) == before


def test_stub_on_a_turned_and_mirrored_symbol_keeps_the_nets(sheet, tools):
    assert tools["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": 50.8, "y": 90.17, "ref": "R3", "value": "1k", "rotation": 90}])["status"] == "ok"
    root = write_layer.load(sheet)
    node = next(s for s in root.children if not s.is_atom and s.name == "symbol" and sheet_editing._ref(s) == "R3")
    at = node.find_children("at")[0]
    node.children.insert(node.children.index(at) + 1, write_layer.SExp.list("mirror", write_layer.symbol("y")))
    write_layer.save(root, sheet)
    root = write_layer.load(sheet)
    node = next(s for s in root.children if not s.is_atom and s.name == "symbol" and sheet_editing._ref(s) == "R3")
    wires, labels = [], []
    for p in sheet_editing.pin_positions(root, node):
        far = p["x"] + (5.08 if p["x"] > 50.8 else -5.08)
        wires.append(_wire((p["x"], p["y"]), (far, p["y"])))
        labels.append({"kind": "label", "x": far, "y": p["y"], "text": f"T{p['number']}", "snap": False})
    assert tools["add_wires"](str(sheet), wires)["status"] == "ok"
    assert tools["add_labels"](str(sheet), labels)["status"] == "ok"
    before = _nets(sheet)
    result = _move(sheet, [0, 2.54], "stub", ref="R3")
    assert result["ok"] is True and len(result["stubs"]) == 2, result
    assert all(s["from"][0] == s["to"][0] for s in result["stubs"])
    assert _nets(sheet) == before
