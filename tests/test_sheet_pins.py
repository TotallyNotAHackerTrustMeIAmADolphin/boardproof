"""add_sheet_pins / remove_sheet_pins / move_sheet_pins (GitHub issue 89), with real kicad-cli.

The downstream project attached a child sheet with add_sheet_symbols and could then not add a pin to
the sheet symbol that existed. Reproduction (before the tools): add_sheet_symbols on the same file
refuses with ``sheet_already_referenced``; no tool changed the pins of a sheet symbol.

Fixture, built only through the tools: a root sheet with R1 and two sheet symbols made by
add_sheet_symbols, then emptied by remove_sheet_pins (the pin-less correction case):

* ``Connectors`` (child connectors.kicad_sch: R10 wired to the hierarchical labels PT_A and PT_B,
  both bidirectional, so the labels exist and the sheet symbol has no pins), the concrete need (a);
* ``MCU`` (child mcu.kicad_sch: R20 and no label at all), the concrete need (b): SER, OE_N, RCLK and
  SRCLK as outputs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_authoring, sheet_editing, sheet_pins, sheet_tree, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def _tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def tools() -> dict:
    return _tools()


def _wire(p1, p2):
    return {"p1": list(p1), "p2": list(p2), "route": "direct", "snap": False}


@pytest.fixture
def root(tmp_path, tools) -> Path:
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    folder = sch.parent
    assert tools["create_sheets"](str(sch), [{"file": "connectors.kicad_sch"}, {"file": "mcu.kicad_sch"}])["status"] == "ok"
    conn, mcu = str(folder / "connectors.kicad_sch"), str(folder / "mcu.kicad_sch")
    assert tools["add_symbols"](conn, [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R10", "value": "1k"}])["status"] == "ok"
    assert tools["add_labels"](conn, [
        {"kind": "hier", "x": 38.1, "y": 46.99, "text": "PT_A", "shape": "bidirectional"},
        {"kind": "hier", "x": 38.1, "y": 54.61, "text": "PT_B", "shape": "bidirectional"}])["status"] == "ok"
    assert tools["add_wires"](conn, [_wire((38.1, 46.99), (50.8, 46.99)), _wire((38.1, 54.61), (50.8, 54.61))])["status"] == "ok"
    assert tools["add_symbols"](mcu, [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R20", "value": "1k"}])["status"] == "ok"
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 63.5, "y": 60.96, "ref": "R1", "value": "10k"}])["status"] == "ok"
    placed = tools["add_sheet_symbols"](str(sch), [
        {"file": "connectors.kicad_sch", "name": "Connectors", "x": 101.6, "y": 50.8, "width": 30.48, "height": 38.1,
         "pins": [{"name": "PT_A", "direction": "bidirectional"}, {"name": "PT_B", "direction": "bidirectional"}]},
        {"file": "mcu.kicad_sch", "name": "MCU", "x": 101.6, "y": 101.6, "width": 30.48, "height": 38.1,
         "pins": [{"name": "X", "direction": "input"}]}])
    assert placed["status"] == "ok", placed
    # the pin-less correction case: Connectors keeps its labels in the child (they are left), MCU loses its placeholder label too
    emptied = tools["remove_sheet_pins"](str(sch), [{"sheet": "Connectors", "pins": ["PT_A", "PT_B"]}])
    assert emptied["ok"] is True and emptied["removed"][0]["pins"][0]["child_label"] == "left", emptied
    emptied = tools["remove_sheet_pins"](str(sch), [{"sheet": "MCU", "pins": ["X"]}], remove_child_labels=True)
    assert emptied["ok"] is True and emptied["removed"][0]["pins"][0]["child_label"] == "removed", emptied
    return sch


def _nodes(path: Path, tag: str):
    return [c for c in write_layer.load(path).children if not c.is_atom and c.name == tag]


def _sheet(sch: Path, name: str):
    return next(s for s in _nodes(sch, "sheet") if sheet_tree.sheet_name_property(s) == name)


def _pins(sch: Path, name: str) -> dict[str, tuple]:
    out = {}
    for pin in _sheet(sch, name).find_children("pin"):
        x, y, a = sheet_editing._at(pin)
        out[pin.get_string(0)] = (pin.children[1].value, round(x, 4), round(y, 4), a)
    return out


def _labels(path: Path) -> dict[str, str]:
    return {c.get_string(0): c.find_children("shape")[0].children[0].value for c in _nodes(path, "hierarchical_label")}


def _bytes(folder: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(folder.glob("*.kicad_*"))}


def _without_labels(path: Path) -> list[str]:
    return [write_layer.serialise(c, compact=True) for c in write_layer.load(path).children
            if c.is_atom or c.name != "hierarchical_label"]


def _erc_types(sch: Path, tmp_path: Path) -> dict[str, int]:
    import shutil

    copy = tmp_path / "erc"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(sch.parent, copy)
    counts: dict[str, int] = {}
    for (kind, _sev), n in sheet_authoring.erc_census(copy / sch.name).items():
        counts[kind] = counts.get(kind, 0) + n
    return counts


# --------------------------------------------------------------------------- the concrete needs


def test_the_fixture_starts_with_pin_less_sheet_symbols_and_labels_in_the_child(root):
    assert _pins(root, "Connectors") == {} and _pins(root, "MCU") == {}
    assert _labels(root.parent / "connectors.kicad_sch") == {"PT_A": "bidirectional", "PT_B": "bidirectional"}
    assert _labels(root.parent / "mcu.kicad_sch") == {}


def test_need_a_two_bidirectional_pins_reuse_the_labels_the_child_already_has(root, tools, tmp_path):
    child = root.parent / "connectors.kicad_sch"
    child_before = _bytes(root.parent)["connectors.kicad_sch"]
    result = tools["add_sheet_pins"](str(root), [{"sheet": "Connectors", "pins": [
        {"name": "PT_A", "direction": "bidirectional"}, {"name": "PT_B", "direction": "bidirectional"}]}])
    assert result["ok"] is True and result["written"] is True, result
    pins = result["added"][0]["pins"]
    assert [(p["name"], p["side"], p["at"], p["child_label"]["state"]) for p in pins] == [
        ("PT_A", "left", [101.6, 53.34], "reused"), ("PT_B", "left", [101.6, 55.88], "reused")]
    assert _pins(root, "Connectors") == {"PT_A": ("bidirectional", 101.6, 53.34, 180), "PT_B": ("bidirectional", 101.6, 55.88, 180)}
    assert _bytes(root.parent)["connectors.kicad_sch"] == child_before  # nothing was written to the child
    assert result["child_files_written"] == []
    assert sheet_authoring.interface_mismatches(write_layer.load(root), write_layer.load(child), "connectors.kicad_sch") == []
    assert sheet_tree.walk(root).problems == ()
    assert result["connectivity"]["changed"] is False


def test_need_b_four_outputs_create_four_labels_and_leave_the_child_symbols_and_instance_data_alone(root, tools):
    child = root.parent / "mcu.kicad_sch"
    before = _without_labels(child)
    instances = [write_layer.serialise(s.find_children("instances")[0], compact=True) for s in _nodes(child, "symbol") if s.find_children("instances")]
    assert instances
    result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": n, "direction": "output"} for n in ("SER", "OE_N", "RCLK", "SRCLK")]}])
    assert result["ok"] is True and result["written"] is True, result
    pins = result["added"][0]["pins"]
    assert [p["name"] for p in pins] == ["SER", "OE_N", "RCLK", "SRCLK"]
    assert all(p["side"] == "right" and p["child_label"]["state"] == "created" for p in pins)
    assert [p["at"] for p in pins] == [[132.08, 104.14], [132.08, 106.68], [132.08, 109.22], [132.08, 111.76]]
    assert _labels(child) == {n: "output" for n in ("SER", "OE_N", "RCLK", "SRCLK")}
    assert result["child_files_written"] == ["mcu.kicad_sch"]
    assert _without_labels(child) == before  # every other node, symbols and their instance data included, is byte-identical
    assert [write_layer.serialise(s.find_children("instances")[0], compact=True) for s in _nodes(child, "symbol") if s.find_children("instances")] == instances
    # the labels are really there for kicad-cli: no pin without a label and no label without a pin
    assert sheet_authoring.interface_mismatches(write_layer.load(root), write_layer.load(child), "mcu.kicad_sch") == []
    assert sheet_tree.netlist_disagreement(sheet_tree.walk(root), [c["ref"] for c in sheet_tree.netlist_components(root)]) == []


def test_the_listed_pin_position_is_the_point_add_wires_must_reach(root, tools):
    """The reported ``at`` of a new sheet pin, wired to R1 with add_wires, puts R1.1 and R10.1 (in the
    child, behind the reused label) on one net in kicad-cli's netlist."""
    result = tools["add_sheet_pins"](str(root), [{"sheet": "Connectors", "pins": [{"name": "PT_A", "direction": "bidirectional"}]}])
    (pin,) = result["added"][0]["pins"]
    x, y = pin["at"]
    r1 = tools["get_symbol_pins"](str(root), ["R1"])["symbols"][0]
    r1_pin1 = next(p for p in r1["pins"] if p["number"] == "1")
    px, py = r1_pin1["x"], r1_pin1["y"]
    assert tools["add_wires"](str(root), [_wire((px, py), (px, y)), _wire((px, y), (x, y))])["status"] == "ok"
    with_net = [nodes for nodes in sheet_editing.nets_from(root).values() if ("R1", "1") in nodes]
    assert with_net and ("R10", "1") in with_net[0], with_net


# --------------------------------------------------------------------------- refusals leave the files alone


@pytest.mark.parametrize("spec,code", [
    ({"sheet": "Connectors", "pins": [{"name": "PT_A", "direction": "input"}]}, "label_direction_mismatch"),
    ({"sheet": "Nope", "pins": [{"name": "Q", "direction": "input"}]}, "sheet_not_found"),
    ({"uuid": "00000000-0000-0000-0000-000000000000", "pins": [{"name": "Q", "direction": "input"}]}, "sheet_not_found"),
    ({"sheet": "Connectors", "pins": [{"name": "Q", "direction": "input", "side": "middle"}]}, "invalid_spec"),
    ({"sheet": "Connectors", "pins": [{"name": "Q", "direction": "sideways"}]}, "invalid_spec"),
    ({"sheet": "Connectors", "pins": [{"name": "Q", "direction": "input", "offset": 0}]}, "pin_off_edge"),
    ({"sheet": "Connectors", "pins": [{"name": "Q", "direction": "input", "offset": 38.1}]}, "pin_off_edge"),
    ({"sheet": "Connectors", "pins": [{"name": "Q", "direction": "input", "offset": 99}]}, "pin_off_edge"),
    ({"sheet": "Connectors", "pins": [{"name": "Q", "direction": "input", "offset": "x"}]}, "invalid_spec"),
    ({"sheet": "Connectors", "pins": [{"name": "Q\nR", "direction": "input"}]}, "invalid_spec"),
    ({"sheet": "Connectors", "uuid": "x", "pins": [{"name": "Q", "direction": "input"}]}, "invalid_spec"),
    ({"sheet": "Connectors", "pins": []}, "invalid_spec"),
    ({"sheet": "Connectors", "pins": [{"name": "Q", "direction": "input", "extra": 1}]}, "invalid_spec"),
])
def test_add_refusals_write_nothing(root, tools, spec, code):
    before = _bytes(root.parent)
    result = tools["add_sheet_pins"](str(root), [spec])
    assert result["ok"] is False and result["code"] == code, result
    assert _bytes(root.parent) == before


def test_a_pin_name_the_sheet_symbol_has_is_a_duplicate_and_a_second_pin_cannot_take_a_pins_spot(root, tools):
    assert tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "SER", "direction": "output", "offset": 5.08}]}])["ok"] is True
    before = _bytes(root.parent)
    again = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "SER", "direction": "output"}]}])
    assert again["code"] == "duplicate_pin_name", again
    twice = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "direction": "output"}, {"name": "A", "direction": "output"}]}])
    assert twice["code"] == "duplicate_pin_name", twice
    taken = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "B", "direction": "output", "offset": 5.08}]}])
    assert taken["code"] == "pin_position_taken", taken
    assert _bytes(root.parent) == before


def test_an_offset_is_snapped_to_the_grid_and_the_result_says_so(root, tools):
    result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "direction": "tri_state", "side": "top", "offset": 6.0}]}])
    assert result["ok"] is True, result
    (pin,) = result["added"][0]["pins"]
    assert pin["snapped"] is True and pin["asked_offset"] == 6.0 and pin["offset"] == 6.35
    assert pin["at"] == [107.95, 101.6]
    assert _pins(root, "MCU")["A"] == ("tri_state", 107.95, 101.6, 90)
    assert _labels(root.parent / "mcu.kicad_sch")["A"] == "tri_state"


def test_sides_and_directions_are_accepted_and_angles_follow_the_side(root, tools):
    specs = [{"name": "L", "direction": "passive", "side": "left"}, {"name": "R", "direction": "input", "side": "right"},
             {"name": "T", "direction": "input", "side": "top"}, {"name": "B", "direction": "bidirectional", "side": "bottom"}]
    result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": specs}])
    assert result["ok"] is True, result
    got = _pins(root, "MCU")
    assert [got[n][3] for n in "LRTB"] == [180, 0, 90, 270]
    assert got["L"][1] == 101.6 and got["R"][1] == 132.08 and got["T"][2] == 101.6 and got["B"][2] == 139.7


def test_the_sheet_can_be_named_by_uuid_and_dry_run_writes_nothing(root, tools):
    uid = write_layer.serialise(_sheet(root, "MCU").find_children("uuid")[0], compact=True).split('"')[1]
    before = _bytes(root.parent)
    result = tools["add_sheet_pins"](str(root), [{"uuid": uid, "pins": [{"name": "Q", "direction": "output"}]}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and result["dry_run"] is True, result
    assert result["added"][0]["pins"][0]["child_label"]["state"] == "created"
    assert _bytes(root.parent) == before


def test_a_new_pin_that_lands_on_a_wire_end_is_a_connectivity_change(root, tools):
    assert tools["add_wires"](str(root), [_wire((101.6, 53.34), (88.9, 53.34))])["status"] == "ok"
    before = _bytes(root.parent)
    result = tools["add_sheet_pins"](str(root), [{"sheet": "Connectors", "pins": [{"name": "PT_A", "direction": "bidirectional"}]}])
    assert result["code"] == "connectivity_changed", result
    assert result["connectivity"]["changes"]["items"][0]["type"] == "pin_lands_on"
    assert _bytes(root.parent) == before
    allowed = tools["add_sheet_pins"](str(root), [{"sheet": "Connectors", "pins": [{"name": "PT_A", "direction": "bidirectional"}]}],
                                      allow_connectivity_change=True)
    assert allowed["ok"] is True and allowed["written"] is True, allowed


# --------------------------------------------------------------------------- remove


@pytest.fixture
def wired(root, tools) -> Path:
    """Connectors with PT_A and PT_B, PT_A wired to R1.1 in the root: the net reaches R10.1 in the child."""
    assert tools["add_sheet_pins"](str(root), [{"sheet": "Connectors", "pins": [
        {"name": "PT_A", "direction": "bidirectional"}, {"name": "PT_B", "direction": "bidirectional"}]}])["ok"] is True
    assert tools["add_wires"](str(root), [_wire((63.5, 57.15), (63.5, 53.34)), _wire((63.5, 53.34), (101.6, 53.34))])["status"] == "ok"
    assert any({("R1", "1"), ("R10", "1")} <= nodes for nodes in sheet_editing.nets_from(root).values())
    return root


def test_removing_a_pin_with_a_wire_attached_is_a_connectivity_change(wired, tools):
    before = _bytes(wired.parent)
    result = tools["remove_sheet_pins"](str(wired), [{"sheet": "Connectors", "pins": ["PT_A"]}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    kinds = {c["type"] for c in result["connectivity"]["changes"]["items"]}
    assert "sheet_pin_detached" in kinds and "split" in kinds, kinds  # the geometry check and the netlist comparison both see it
    assert _bytes(wired.parent) == before
    allowed = tools["remove_sheet_pins"](str(wired), [{"sheet": "Connectors", "pins": ["PT_A"]}], allow_connectivity_change=True)
    assert allowed["ok"] is True and allowed["written"] is True, allowed
    assert set(_pins(wired, "Connectors")) == {"PT_B"}


def test_the_geometry_check_and_the_netlist_comparison_each_refuse_on_their_own(wired, tools, monkeypatch):
    spec = [{"sheet": "Connectors", "pins": ["PT_A"]}]
    monkeypatch.setattr(sheet_pins, "_attached", lambda *a, **k: [])
    only_netlist = tools["remove_sheet_pins"](str(wired), spec)
    assert only_netlist["code"] == "connectivity_changed", only_netlist
    monkeypatch.undo()
    monkeypatch.setattr(sheet_editing, "compare_nets", lambda *a, **k: [])
    only_geometry = tools["remove_sheet_pins"](str(wired), spec)
    assert only_geometry["code"] == "connectivity_changed", only_geometry
    monkeypatch.setattr(sheet_pins, "_attached", lambda *a, **k: [])
    both_off = tools["remove_sheet_pins"](str(wired), spec)
    # with both connectivity guards off, the ERC census is a third layer: the wire now ends on nothing
    assert both_off["code"] == "write_not_verified" and "unconnected_wire_endpoint" in both_off["reason"], both_off
    monkeypatch.undo()  # the guards are back on for the call that is meant
    allowed = tools["remove_sheet_pins"](str(wired), spec, allow_connectivity_change=True)  # meant: it goes through
    assert allowed["ok"] is True and allowed["written"] is True, allowed


def test_the_child_label_is_left_unless_asked_and_the_result_says_which(wired, tools, tmp_path):
    child = wired.parent / "connectors.kicad_sch"
    result = tools["remove_sheet_pins"](str(wired), [{"sheet": "Connectors", "pins": ["PT_B"]}])
    assert result["ok"] is True and result["written"] is True, result
    assert result["removed"][0]["pins"][0]["child_label"] == "left" and "left in the child" in result["child_labels"]
    assert set(_labels(child)) == {"PT_A", "PT_B"} and set(_pins(wired, "Connectors")) == {"PT_A"}
    assert _erc_types(wired, tmp_path).get("hier_label_mismatch", 0) >= 1  # the label without a pin is what ERC reports
    # add_sheet_pins takes the label up again
    again = tools["add_sheet_pins"](str(wired), [{"sheet": "Connectors", "pins": [{"name": "PT_B", "direction": "bidirectional"}]}])
    assert again["added"][0]["pins"][0]["child_label"]["state"] == "reused"


def test_remove_child_labels_removes_the_label_and_a_wired_label_is_a_connectivity_change(wired, tools):
    child = wired.parent / "connectors.kicad_sch"
    gone = tools["remove_sheet_pins"](str(wired), [{"sheet": "Connectors", "pins": ["PT_B"]}], remove_child_labels=True, allow_connectivity_change=True)
    assert gone["ok"] is True and gone["removed"][0]["pins"][0]["child_label"] == "removed", gone
    assert set(_labels(child)) == {"PT_A"}
    before = _bytes(wired.parent)
    refused = tools["remove_sheet_pins"](str(wired), [{"sheet": "Connectors", "pins": ["PT_A"]}], remove_child_labels=True)
    assert refused["code"] == "connectivity_changed", refused
    assert _bytes(wired.parent) == before


def test_remove_by_pattern_to_a_pin_less_symbol_and_a_miss_refuses(root, tools):
    assert tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": n, "direction": "output"} for n in ("D0", "D1", "CLK")]}])["ok"]
    before = _bytes(root.parent)
    assert tools["remove_sheet_pins"](str(root), [{"sheet": "MCU", "pins": ["NOPE"]}])["code"] == "pin_not_found"
    assert tools["remove_sheet_pins"](str(root), [{"sheet": "MCU", "pattern": "Z*"}])["code"] == "pin_not_found"
    assert tools["remove_sheet_pins"](str(root), [{"sheet": "MCU"}])["code"] == "invalid_spec"
    assert _bytes(root.parent) == before
    result = tools["remove_sheet_pins"](str(root), [{"sheet": "MCU", "pattern": "D*", "pins": ["CLK"]}], remove_child_labels=True)
    assert result["ok"] is True and result["removed"][0]["pins_left"] == 0, result
    assert _pins(root, "MCU") == {} and _labels(root.parent / "mcu.kicad_sch") == {}


# --------------------------------------------------------------------------- move


def test_move_along_the_edge_and_to_another_side(root, tools):
    assert tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "direction": "output"}, {"name": "B", "direction": "input"}]}])["ok"]
    child_before = _bytes(root.parent)["mcu.kicad_sch"]
    result = tools["move_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "offset": 15.24}, {"name": "B", "side": "bottom", "offset": 7.62}]}])
    assert result["ok"] is True and result["written"] is True, result
    a, b = result["moved"][0]["pins"]
    assert a["from"]["at"] == [132.08, 104.14] and a["at"] == [132.08, 116.84]
    assert b["from"]["side"] == "left" and b["at"] == [109.22, 139.7]
    assert _pins(root, "MCU") == {"A": ("output", 132.08, 116.84, 0), "B": ("input", 109.22, 139.7, 270)}
    assert _bytes(root.parent)["mcu.kicad_sch"] == child_before  # move never touches the child
    assert tools["move_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "side": "top"}]}])["ok"] is True
    assert _pins(root, "MCU")["A"][3] == 90


def test_move_refusals_write_nothing(root, tools):
    assert tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "direction": "output"}, {"name": "B", "direction": "output"}]}])["ok"]
    before = _bytes(root.parent)
    for spec, code in [
        ({"sheet": "MCU", "pins": [{"name": "A", "offset": 99}]}, "pin_off_edge"),
        ({"sheet": "MCU", "pins": [{"name": "A", "offset": 5.08}]}, "pin_position_taken"),
        ({"sheet": "MCU", "pins": [{"name": "A"}]}, "invalid_spec"),
        ({"sheet": "MCU", "pins": [{"name": "A", "side": "middle"}]}, "invalid_spec"),
        ({"sheet": "MCU", "pins": [{"name": "A", "side": "right"}]}, "invalid_spec"),
        ({"sheet": "MCU", "pins": [{"name": "Z", "offset": 2.54}]}, "pin_not_found"),
        ({"sheet": "Nope", "pins": [{"name": "A", "offset": 2.54}]}, "sheet_not_found"),
    ]:
        result = tools["move_sheet_pins"](str(root), [spec])
        assert result["ok"] is False and result["code"] == code, (spec, result)
    assert _bytes(root.parent) == before


def test_moving_a_wired_pin_is_a_connectivity_change_and_the_wire_is_not_dragged(wired, tools):
    before = _bytes(wired.parent)
    result = tools["move_sheet_pins"](str(wired), [{"sheet": "Connectors", "pins": [{"name": "PT_A", "offset": 15.24}]}])
    assert result["code"] == "connectivity_changed", result
    assert "sheet_pin_detached" in {c["type"] for c in result["connectivity"]["changes"]["items"]}
    assert _bytes(wired.parent) == before
    allowed = tools["move_sheet_pins"](str(wired), [{"sheet": "Connectors", "pins": [{"name": "PT_A", "offset": 15.24}]}], allow_connectivity_change=True)
    assert allowed["ok"] is True and _pins(wired, "Connectors")["PT_A"][2] == 66.04
    assert not any({("R1", "1"), ("R10", "1")} <= nodes for nodes in sheet_editing.nets_from(wired).values())


def test_move_dry_run_reports_and_writes_nothing(root, tools):
    assert tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "direction": "output"}]}])["ok"]
    before = _bytes(root.parent)
    result = tools["move_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "side": "left"}]}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and result["moved"][0]["pins"][0]["side"] == "left"
    assert _bytes(root.parent) == before


# --------------------------------------------------------------------------- self-checks and guards


def test_the_reload_check_fires_on_a_corrupted_write_of_the_child_or_the_parent(root, tools, monkeypatch):
    spec = [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}]
    real = write_layer.load
    before = _bytes(root.parent)

    def corrupt_only(marker):
        def load(path):
            node = real(path)
            if ".tmp" in Path(path).name and marker in Path(path).name:
                node.children.pop()
            return node
        return load

    for marker in ("mcu", "demo"):  # the child is written first; then the parent, with the child restored if it fails
        monkeypatch.setattr(write_layer, "load", corrupt_only(marker))
        result = tools["add_sheet_pins"](str(root), spec)
        monkeypatch.undo()
        assert result["ok"] is False and result["code"] == "write_not_verified", (marker, result)
        assert _bytes(root.parent) == before, marker
        assert not [p for p in root.parent.iterdir() if p.name.endswith(".tmp")]


def test_a_remove_and_a_move_corrupted_write_is_refused(root, tools, monkeypatch):
    assert tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "direction": "output"}]}])["ok"]
    real = write_layer.load
    before = _bytes(root.parent)

    def corrupt(path):
        node = real(path)
        if ".tmp" in Path(path).name:
            node.children.pop()
        return node

    monkeypatch.setattr(write_layer, "load", corrupt)
    for result in (tools["remove_sheet_pins"](str(root), [{"sheet": "MCU", "pins": ["A"]}], remove_child_labels=True),
                   tools["move_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "A", "offset": 10.16}]}])):
        assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(root.parent) == before


def test_the_staged_check_fires_when_the_change_touches_more_than_it_selected(root, tools, monkeypatch):
    real = sheet_pins._insert_pin

    def greedy(sheet, pin):
        real(sheet, pin)
        sheet.children[:] = [c for c in sheet.children if c.is_atom or c.name != "dnp"]  # an unrelated token of the sheet symbol

    monkeypatch.setattr(sheet_pins, "_insert_pin", greedy)
    before = _bytes(root.parent)
    result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "beyond its pins" in result["reason"], result
    assert _bytes(root.parent) == before


def test_the_staged_check_fires_when_the_child_gains_a_node_that_is_not_a_label(root, tools, monkeypatch):
    real = sheet_pins._insert_label

    def sneaky(child, label):
        real(child, label)
        child.children.insert(0, write_layer.SExp.list("text", write_layer.string("x")))

    monkeypatch.setattr(sheet_pins, "_insert_label", sneaky)
    before = _bytes(root.parent)
    result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert _bytes(root.parent) == before


def test_the_erc_check_fires_on_a_new_violation_type(root, tools, monkeypatch):
    real = sheet_authoring.erc_census
    calls = []

    def census(path):
        counts = real(path)
        calls.append(path)
        if len(calls) == 2:  # the staged copy
            counts[("power_pin_not_driven", "error")] += 1
        return counts

    monkeypatch.setattr(sheet_authoring, "erc_census", census)
    before = _bytes(root.parent)
    result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "power_pin_not_driven" in result["reason"], result
    assert _bytes(root.parent) == before


def test_the_interface_check_fires_when_a_label_goes_missing(root, tools, monkeypatch):
    monkeypatch.setattr(sheet_pins, "_insert_label", lambda child, label: None)
    before = _bytes(root.parent)
    result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}])
    assert result["ok"] is False and result["code"] in ("write_not_verified",), result
    assert _bytes(root.parent) == before


def test_a_lock_on_the_parent_or_the_child_refuses_and_the_guard_is_what_does_it(root, tools, monkeypatch):
    from tests.test_write_guard import OTHER_HOST

    spec = [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}]
    child = root.parent / "mcu.kicad_sch"
    for target in (root, child):
        lock = target.with_name(f"~{target.name}.lck")
        lock.write_bytes(OTHER_HOST)
        before = _bytes(root.parent)
        result = tools["add_sheet_pins"](str(root), spec)
        assert result["ok"] is False and result["code"] == "file_open_in_kicad", (target.name, result)
        assert _bytes(root.parent) == before
        lock.unlink()
    # mutation check: with check_writable removed, the same call writes beside a lock
    lock = root.with_name(f"~{root.name}.lck")
    lock.write_bytes(OTHER_HOST)
    monkeypatch.setattr(write_layer, "check_writable", lambda *a, **k: None)
    result = tools["add_sheet_pins"](str(root), spec)
    monkeypatch.undo()
    assert result["ok"] is True and result["written"] is True, result


def _add_twin(root: Path, source: str, name: str, uuid: str, y: float) -> None:
    """A second sheet symbol of the same child file, written through the layer on a copy of the sheet symbol node."""
    import copy

    parent = write_layer.load(root)
    node = next(c for c in parent.children if not c.is_atom and c.name == "sheet" and sheet_tree.sheet_name_property(c) == source)
    twin = copy.deepcopy(node)
    for prop in twin.find_children("property"):
        if prop.get_string(0) == "Sheetname":
            write_layer.set_string(prop, 1, name)
    write_layer.set_number(twin.find_children("at")[0], 1, y)
    for uid in twin.find_children("uuid"):
        write_layer.set_string(uid, 0, uuid)
    parent.children.insert(parent.children.index(node) + 1, twin)
    write_layer.save(parent, root)


TWIN_UUID = "11111111-1111-4111-8111-111111111111"


def test_a_child_used_twice_is_refused_when_a_label_would_change(root, tools):
    _add_twin(root, "MCU", "MCU2", TWIN_UUID, 160.02)
    before = _bytes(root.parent)
    result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}])
    assert result["ok"] is False and result["code"] == "sheet_file_shared", result
    assert _bytes(root.parent) == before


def test_a_child_used_twice_is_fine_when_every_label_is_only_reused(root, tools):
    _add_twin(root, "Connectors", "Connectors2", TWIN_UUID, 160.02)
    result = tools["add_sheet_pins"](str(root), [{"sheet": "Connectors", "pins": [{"name": "PT_A", "direction": "bidirectional"}]}],
                                     allow_connectivity_change=True)  # the new pin joins R10 through the label it reuses
    assert result["ok"] is True and result["written"] is True, result
    assert result["child_files_written"] == [], result


def test_two_sheet_symbols_with_one_name_are_ambiguous_and_the_uuid_picks_one(root, tools):
    _add_twin(root, "Connectors", "Connectors", TWIN_UUID, 160.02)
    before = _bytes(root.parent)
    spec = {"pins": [{"name": "PT_A", "direction": "bidirectional"}]}
    result = tools["add_sheet_pins"](str(root), [{"sheet": "Connectors", **spec}])
    assert result["ok"] is False and result["code"] == "invalid_spec" and "uuid" in result["reason"], result
    assert len(result["sheets"]) == 2
    assert _bytes(root.parent) == before
    picked = tools["add_sheet_pins"](str(root), [{"uuid": TWIN_UUID, **spec}])
    assert picked["ok"] is True and picked["added"][0]["uuid"] == TWIN_UUID, picked


# --------------------------------------------------------------------------- the rest of the refusals


def test_a_mixed_remove_list_with_one_unknown_pin_refuses_and_removes_nothing(wired, tools):
    before = _bytes(wired.parent)
    result = tools["remove_sheet_pins"](str(wired), [{"sheet": "Connectors", "pins": ["PT_A", "NOPE"]}], allow_connectivity_change=True)
    assert result["ok"] is False and result["code"] == "pin_not_found", result
    assert _bytes(wired.parent) == before
    assert set(_pins(wired, "Connectors")) == {"PT_A", "PT_B"}


def test_a_sheet_named_twice_or_a_pin_moved_twice_in_one_call_refuses(root, tools):
    before = _bytes(root.parent)
    named_twice = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]},
                                                      {"sheet": "MCU", "pins": [{"name": "R", "direction": "output"}]}])
    assert named_twice["code"] == "invalid_spec" and "twice" in named_twice["reason"], named_twice
    assert _bytes(root.parent) == before
    assert tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}])["ok"]
    after_add = _bytes(root.parent)
    twice = tools["move_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "offset": 5.08}, {"name": "Q", "offset": 10.16}]}])
    assert twice["code"] == "invalid_spec" and "twice" in twice["reason"], twice
    assert _bytes(root.parent) == after_add


def test_the_interface_check_names_what_it_cannot_explain(root, tools, monkeypatch):
    monkeypatch.setattr(sheet_pins, "_insert_label", lambda child, label: None)
    monkeypatch.setattr(sheet_pins, "_staged_check", lambda *a, **k: None)  # the interface diff is the layer under test
    result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}])
    assert result["code"] == "write_not_verified" and result["interface_problems"], result


def test_a_failing_parent_save_puts_the_child_back_byte_for_byte(root, tools, monkeypatch):
    before = _bytes(root.parent)
    real = write_layer.save
    seen: list[str] = []

    def save(node, path, *a, **k):
        if Path(path).resolve().parent == root.resolve().parent:
            seen.append(Path(path).name)
        if Path(path).resolve() == root.resolve():
            raise OSError("disk full")
        return real(node, path, *a, **k)

    monkeypatch.setattr(write_layer, "save", save)
    with pytest.raises(OSError, match="disk full"):
        tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}])
    monkeypatch.undo()
    assert seen == ["mcu.kicad_sch", "demo.kicad_sch", "mcu.kicad_sch"], seen  # child written, parent refused, child written back
    assert _bytes(root.parent) == before


# --------------------------------------------------------------------------- the write lock (issue 87)


def _wrapped() -> dict:
    from boardproof import gate_state

    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    state = gate_state.GateState()
    return {t.name: gate_state.attach_gate_field(t.fn, t.annotations, state) for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _together(*calls) -> list:
    import threading

    barrier = threading.Barrier(len(calls))
    results: list = [None] * len(calls)

    def run(i, call):
        barrier.wait()
        try:
            results[i] = call()
        except BaseException as exc:  # noqa: BLE001 - the test reports it
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    return results


def test_concurrent_add_sheet_pins_on_two_sheets_both_land(root):
    tools = _wrapped()
    a, b = _together(
        lambda: tools["add_sheet_pins"](str(root), [{"sheet": "Connectors", "pins": [{"name": "PT_A", "direction": "bidirectional"}]}]),
        lambda: tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}]))
    assert not isinstance(a, BaseException) and not isinstance(b, BaseException), (a, b)
    assert a["ok"] is True and b["ok"] is True, (a, b)
    assert set(_pins(root, "Connectors")) == {"PT_A"} and set(_pins(root, "MCU")) == {"Q"}, "a sheet pin was lost (lost update)"


def test_add_sheet_pins_and_add_labels_on_the_child_do_not_lose_each_other(root):
    tools = _wrapped()
    child = root.parent / "mcu.kicad_sch"
    pins, label = _together(
        lambda: tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}]),
        lambda: tools["add_labels"](str(child), [{"kind": "label", "x": 76.2, "y": 76.2, "text": "LOCAL_NET", "snap": False}]))
    assert not isinstance(pins, BaseException) and not isinstance(label, BaseException), (pins, label)
    assert pins["ok"] is True and label["status"] == "ok", (pins, label)
    text = child.read_text(encoding="utf-8")
    assert '"LOCAL_NET"' in text and '"Q"' in text, "the label or the hierarchical label was lost (lost update)"


@pytest.mark.parametrize("held", ["parent", "child"])
def test_a_held_file_makes_add_sheet_pins_never_start_through_the_wrapper(root, monkeypatch, held):
    import threading

    tools = _wrapped()
    target = root if held == "parent" else root.parent / "mcu.kicad_sch"
    monkeypatch.setattr(write_layer, "LOCK_TIMEOUT_S", 0.3)
    before = _bytes(root.parent)
    taken, release = threading.Event(), threading.Event()

    def holder() -> None:
        with write_layer.path_lock([target]):
            taken.set()
            release.wait(30)

    t = threading.Thread(target=holder)
    t.start()
    assert taken.wait(5)
    try:
        result = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [{"name": "Q", "direction": "output"}]}])
    finally:
        release.set()
        t.join(10)
    assert result["ok"] is False and result["code"] == "file_busy", result
    assert _bytes(root.parent) == before


# --------------------------------------------------------------------------- the text an LLM reads


def test_the_tools_are_registered_mutating_with_the_pinned_texts():
    mcp = MCPServer("t")
    names = schematic_toolset.build(mcp)
    listed = {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    for name in ("add_sheet_pins", "remove_sheet_pins", "move_sheet_pins"):
        assert name in names and listed[name].annotations.read_only_hint is False
        text = " ".join((listed[name].description or "").split())
        assert write_layer.LOCK_SENTENCE in text and "connectivity_changed" in text and "dry_run" in text, name
    add = " ".join(listed["add_sheet_pins"].description.split())
    for phrase in ("tri_state", "label_direction_mismatch", "duplicate_pin_name", "get_symbol_pins and connect_pins cannot address a sheet pin",
                   "draw to the listed point with add_wires", "reused"):
        assert phrase in add, phrase
    assert "LEFT as it is, unless remove_child_labels is true" in " ".join(listed["remove_sheet_pins"].description.split())
    assert "A wire is NOT dragged along" in " ".join(listed["move_sheet_pins"].description.split())
