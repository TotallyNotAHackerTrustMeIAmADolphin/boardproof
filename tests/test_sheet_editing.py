"""delete_* / move_* / set_property_positions (GitHub issue 84), with real kicad-cli.

The downstream project reworked a sheet of 36 symbols, about 107 wires and about 60 labels five
times in one day by resetting it to its skeleton, because the toolset could only add. These tests
build a small sheet through the tools (project_create, add_symbols, add_wires, add_labels,
add_junctions), change it in place, and read the result back from the file and from kicad-cli's
netlist. Pin positions are cross-checked against the netlist: a label placed exactly on a computed
pin position must put that pin on the label's net.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_authoring, sheet_editing, sheet_tree, write_layer
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset


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
def sheet(tmp_path, tools) -> Path:
    """R1 (50.8, 50.8), R2 (76.2, 50.8), C1 (101.6, 50.8), all upright. Pins: R1.1 (50.8, 46.99),
    R1.2 (50.8, 54.61), R2.1 (76.2, 46.99), R2.2 (76.2, 54.61). Net NETA joins R1.1 and R2.1 over a wire
    in three segments with the label NETA on the top segment; GNDX (two labels) joins R1.2 and R2.2
    through two stubs; C1 is placed and wired to nothing; one stray junction."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"},
        {"lib_id": "Device:C", "x": 101.6, "y": 50.8, "ref": "C1", "value": "1u"},
    ])["status"] == "ok"
    assert tools["add_wires"](str(sch), [
        _wire((50.8, 46.99), (50.8, 40.64)), _wire((50.8, 40.64), (76.2, 40.64)), _wire((76.2, 40.64), (76.2, 46.99)),
        _wire((50.8, 54.61), (50.8, 59.69)), _wire((76.2, 54.61), (76.2, 59.69)),
    ])["status"] == "ok"
    assert tools["add_labels"](str(sch), [
        {"kind": "label", "x": 63.5, "y": 40.64, "text": "NETA", "snap": False},
        {"kind": "label", "x": 50.8, "y": 59.69, "text": "GNDX", "snap": False},
        {"kind": "label", "x": 76.2, "y": 59.69, "text": "GNDX", "snap": False},
    ])["status"] == "ok"
    assert tools["add_junctions"](str(sch), [[120.65, 80.01]])["status"] == "ok"
    return sch


def _root(sch: Path):
    return write_layer.load(sch)


def _nodes(sch: Path, tag: str):
    return [c for c in _root(sch).children if not c.is_atom and c.name == tag]


def _symbol(sch: Path, ref: str):
    return next(s for s in _nodes(sch, "symbol") if sheet_editing._ref(s) == ref)


def _nets(sch: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    return sheet_editing.read_nets(tree, sch, None)


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _label_uuid(sch: Path, text: str, nth: int = 0) -> str:
    return [sheet_editing._uuid(n) for n in _nodes(sch, "label") if n.get_string(0) == text][nth]


# --------------------------------------------------------------------------- the fixture itself


def test_the_fixture_has_the_nets_the_tests_assume(sheet):
    nets = _nets(sheet)
    assert nets["/NETA"] == frozenset({("R1", "1"), ("R2", "1")})
    assert nets["/GNDX"] == frozenset({("R1", "2"), ("R2", "2")})
    assert len(_nodes(sheet, "wire")) == 5 and len(_nodes(sheet, "junction")) == 1


# --------------------------------------------------------------------------- pin positions


def test_pin_positions_agree_with_the_netlist_for_every_rotation_and_mirror(tmp_path, tools):
    """A label on a computed pin position puts that pin on the label's net, for four rotations and both mirrors."""
    made = project_toolset._project_create(str(tmp_path / "pins"), "pins")
    sch = Path(made["schematic_path"])
    variants = [(rot, mirror) for rot in (0, 90, 180, 270) for mirror in ("", "x", "y")]
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 25.4 + 25.4 * i, "y": 25.4 + 25.4 * (i % 3), "ref": f"R{i + 1}", "value": "1k", "rotation": rot}
        for i, (rot, _m) in enumerate(variants)])["status"] == "ok"
    root = write_layer.load(sch)
    for node, (_rot, mirror) in zip([c for c in root.children if not c.is_atom and c.name == "symbol"], variants, strict=True):
        if mirror:
            at = node.find_children("at")[0]
            node.children.insert(node.children.index(at) + 1, write_layer.SExp.list("mirror", write_layer.symbol(mirror)))
    write_layer.save(root, sch)
    labels = []
    for sym in [c for c in write_layer.load(sch).children if not c.is_atom and c.name == "symbol"]:
        for pin in sheet_editing.pin_positions(write_layer.load(sch), sym):
            labels.append({"kind": "label", "x": pin["x"], "y": pin["y"], "text": f"{sheet_editing._ref(sym)}_{pin['number']}", "snap": False})
    assert tools["add_labels"](str(sch), labels)["status"] == "ok"
    nets = _nets(sch)
    for lab in labels:
        ref, number = lab["text"].split("_")
        assert (ref, number) in nets.get("/" + lab["text"], frozenset()), f"{lab['text']} at {lab['x']},{lab['y']}: {sorted(nets)}"


# --------------------------------------------------------------------------- delete_labels


def test_delete_labels_by_text_changes_the_net_name_and_is_refused_without_the_flag(sheet):
    before = _bytes(sheet)
    result = sheet_editing.delete("label", str(sheet), [{"texts": ["NETA"]}], None, remove_dangling=False,
                                  allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert any(c["type"] == "renamed" and c["net"] == "/NETA" for c in result["connectivity"]["changes"]["items"])
    assert _bytes(sheet) == before


def test_delete_labels_with_the_flag_writes_and_lists_the_changed_nets(sheet):
    result = sheet_editing.delete("label", str(sheet), [{"texts": ["NETA"]}], None, remove_dangling=False,
                                  allow_connectivity_change=True, dry_run=False)
    assert result["ok"] is True and result["written"] is True and result["count"] == 1, result
    assert result["connectivity"]["changed"] is True
    assert "/NETA" not in _nets(sheet)
    assert [n.get_string(0) for n in _nodes(sheet, "label")] == ["GNDX", "GNDX"]


def test_delete_labels_by_pattern_and_region(sheet):
    result = sheet_editing.delete("label", str(sheet), [{"pattern": "GND*", "region": [70, 55, 80, 65]}], None,
                                  remove_dangling=False, allow_connectivity_change=True, dry_run=False)
    assert result["ok"] is True and result["count"] == 1 and result["deleted"][0]["at"] == [76.2, 59.69], result
    assert [n.get_string(0) for n in _nodes(sheet, "label")] == ["NETA", "GNDX"]
    assert _nets(sheet)["/GNDX"] == frozenset({("R1", "2")})


def test_a_redundant_label_can_go_without_changing_connectivity(sheet, tools):
    assert tools["add_labels"](str(sheet), [{"kind": "label", "x": 58.42, "y": 40.64, "text": "NETA", "snap": False}])["status"] == "ok"
    result = sheet_editing.delete("label", str(sheet), [{"uuids": [_label_uuid(sheet, "NETA", 1)]}], None,
                                  remove_dangling=False, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True, result
    assert result["connectivity"]["changed"] is False
    assert _nets(sheet)["/NETA"] == frozenset({("R1", "1"), ("R2", "1")})


def test_deleting_one_of_two_same_text_labels_splits_the_net_and_is_refused(sheet):
    result = sheet_editing.delete("label", str(sheet), [{"uuids": [_label_uuid(sheet, "GNDX", 1)]}], None,
                                  remove_dangling=False, allow_connectivity_change=False, dry_run=False)
    assert result["code"] == "connectivity_changed", result   # the two GNDX labels are what joins R1.2 and R2.2


def test_a_selector_that_matches_nothing_or_names_a_missing_item_refuses(sheet):
    before = _bytes(sheet)
    missing = sheet_editing.delete("label", str(sheet), [{"texts": ["NOPE"]}], None, remove_dangling=False,
                                   allow_connectivity_change=True, dry_run=False)
    assert missing["code"] == "not_found" and "NOPE" in str(missing["not_found"])
    nothing = sheet_editing.delete("label", str(sheet), [{"pattern": "ZZZ*"}], None, remove_dangling=False,
                                   allow_connectivity_change=True, dry_run=False)
    assert nothing["code"] == "selector_matched_nothing"
    bad = sheet_editing.delete("label", str(sheet), [{"colour": "red"}], None, remove_dangling=False,
                               allow_connectivity_change=True, dry_run=False)
    assert bad["code"] == "invalid_spec"
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- delete_wires / junctions


def test_delete_wires_that_would_orphan_a_pin_is_refused_and_leaves_the_file_untouched(sheet):
    before = _bytes(sheet)
    result = sheet_editing.delete("wire", str(sheet), [{"region": [75, 54, 77, 60]}], None, remove_dangling=False,
                                  allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    types = {c["type"] for c in result["connectivity"]["changes"]["items"]}
    assert {"split", "orphaned"} <= types, types
    assert _bytes(sheet) == before


def test_delete_wires_with_the_flag_reports_what_dangles_and_remove_dangling_removes_it(sheet):
    # the top bar of NETA: removing the middle segment leaves two stubs that end in the air
    middle = [sheet_editing._uuid(w) for w in _nodes(sheet, "wire") if sheet_editing._wire_ends(w) == [(50.8, 40.64), (76.2, 40.64)]]
    result = sheet_editing.delete("wire", str(sheet), [{"uuids": middle}], None, remove_dangling=False,
                                  allow_connectivity_change=True, dry_run=False)
    assert result["ok"] is True and len(result["dangling_after"]["wires"]) == 2, result
    assert len(_nodes(sheet, "wire")) == 4


def test_remove_dangling_removes_what_the_delete_left_hanging_and_not_what_was_there_before(sheet):
    middle = [sheet_editing._uuid(w) for w in _nodes(sheet, "wire") if sheet_editing._wire_ends(w) == [(50.8, 40.64), (76.2, 40.64)]]
    result = sheet_editing.delete("wire", str(sheet), [{"uuids": middle}], None, remove_dangling=True,
                                  allow_connectivity_change=True, dry_run=False)
    assert result["ok"] is True, result
    assert len(result["dangling_removed"]) == 2      # the two stubs the bar joined; the GNDX stubs end on pins and labels
    assert result["dangling_after"]["wires"] == [] and len(_nodes(sheet, "wire")) == 2
    # the NETA label that sat on the bar is dangling now, reported and kept
    assert len(result["dangling_after"]["labels"]) == 1 and [n.get_string(0) for n in _nodes(sheet, "label")].count("NETA") == 1


def test_delete_wires_by_net_label_text(sheet):
    result = sheet_editing.delete("wire", str(sheet), [{"net": "NETA"}], None, remove_dangling=False,
                                  allow_connectivity_change=True, dry_run=False)
    assert result["ok"] is True and result["count"] == 3, result
    assert len(_nodes(sheet, "wire")) == 2


def test_delete_a_stray_junction_changes_no_net(sheet):
    result = sheet_editing.delete("junction", str(sheet), [{"region": [100, 75, 125, 85]}], None, remove_dangling=False,
                                  allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True and result["count"] == 1 and result["connectivity"]["changed"] is False, result
    assert _nodes(sheet, "junction") == []


def test_dry_run_changes_nothing_but_says_what_would_happen(sheet):
    before = _bytes(sheet)
    result = sheet_editing.delete("label", str(sheet), [{"pattern": "G*"}], None, remove_dangling=False,
                                  allow_connectivity_change=True, dry_run=True)
    assert result["ok"] is True and result["written"] is False and len(result["would_delete"]) == 2, result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- delete_symbols


def test_delete_symbols_an_unwired_part_changes_no_net(sheet):
    result = sheet_editing.delete("symbol", str(sheet), [{"references": ["C1"]}], None, remove_dangling=False,
                                  allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True and result["deleted"][0]["reference"] == "C1", result
    assert [sheet_editing._ref(s) for s in _nodes(sheet, "symbol")] == ["R1", "R2"]


def test_delete_symbols_that_leaves_a_pin_alone_needs_the_flag(sheet):
    before = _bytes(sheet)
    refused = sheet_editing.delete("symbol", str(sheet), [{"pattern": "R2"}], None, remove_dangling=False,
                                   allow_connectivity_change=False, dry_run=False)
    assert refused["code"] == "connectivity_changed", refused
    assert any(c["type"] == "orphaned" for c in refused["connectivity"]["changes"]["items"])
    assert _bytes(sheet) == before
    done = sheet_editing.delete("symbol", str(sheet), [{"pattern": "R2"}], None, remove_dangling=True,
                                allow_connectivity_change=True, dry_run=False)
    assert done["ok"] is True and done["connectivity"]["changed"] is True, done
    assert [sheet_editing._ref(s) for s in _nodes(sheet, "symbol")] == ["R1", "C1"]
    assert len(done["dangling_removed"]) == 4   # R2's two stubs, then the bar and R1's top stub that ended on them


# --------------------------------------------------------------------------- move_symbols


def test_move_symbols_relative_drags_the_wire_ends_and_keeps_every_net(sheet):
    result = sheet_editing.move("symbol", str(sheet), [{"references": ["R2"], "offset": [5.08, 0]}], None, move_wires=True,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True and result["count"] == 1, result
    moved = result["moved"][0]
    assert moved["from"] == [76.2, 50.8] and moved["to"] == [81.28, 50.8]
    assert {(p["number"], p["x"], p["y"]) for p in moved["pins"]} == {("1", 81.28, 46.99), ("2", 81.28, 54.61)}
    assert result["connectivity"]["changed"] is False
    assert len(result["followed"]["wires"]) == 2 and result["followed"]["labels"] == []   # the GNDX label sits at the stub's far end
    nets = _nets(sheet)
    assert nets["/NETA"] == frozenset({("R1", "1"), ("R2", "1")})
    assert sheet_editing._at(_symbol(sheet, "R2"))[:2] == (81.28, 50.8)
    # the property texts moved with the symbol
    ref_prop = sheet_editing._property(_symbol(sheet, "R2"), "Reference")
    assert sheet_editing._at(ref_prop)[0] == 81.28


def test_move_symbols_without_wires_refuses_because_the_pins_lose_their_wire(sheet):
    before = _bytes(sheet)
    result = sheet_editing.move("symbol", str(sheet), [{"references": ["R2"], "offset": [5.08, 0]}], None, move_wires=False,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert _bytes(sheet) == before


def test_move_symbols_absolute_snaps_and_says_what_it_snapped(sheet):
    result = sheet_editing.move("symbol", str(sheet), [{"references": ["C1"], "to": [110.0, 52.0]}], None, move_wires=True,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True, result
    moved = result["moved"][0]
    assert moved["asked"] == [110.0, 52.0] and moved["snapped"] == [110.49, 52.07], moved
    assert result["snapped"] == [moved["uuid"]] and result["grid"] == 1.27
    assert sheet_editing._at(_symbol(sheet, "C1"))[:2] == (110.49, 52.07)


def test_move_symbols_snap_off_keeps_the_exact_position(sheet):
    result = sheet_editing.move("symbol", str(sheet), [{"references": ["C1"], "to": [110.0, 52.0]}], None, move_wires=True,
                                snap=False, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["moved"][0]["snapped"] is None and sheet_editing._at(_symbol(sheet, "C1"))[:2] == (110.0, 52.0)


def test_a_move_that_is_not_along_the_wire_slants_it_and_says_so(sheet):
    result = sheet_editing.move("symbol", str(sheet), [{"references": ["R2"], "offset": [0, 2.54]}], None, move_wires=True,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True, result
    assert result["slanted"] == []  # R2's stubs are vertical: moving along them stays straight
    result = sheet_editing.move("symbol", str(sheet), [{"references": ["R2"], "offset": [2.54, 0]}], None, move_wires=True,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True and len(result["slanted"]) >= 1, result


def test_moving_a_symbol_onto_another_pin_merges_nets_and_is_refused(sheet):
    result = sheet_editing.move("symbol", str(sheet), [{"references": ["C1"], "to": [50.8, 25.4]}], None, move_wires=True,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True  # lands in free space: no change
    result = sheet_editing.move("symbol", str(sheet), [{"references": ["C1"], "to": [50.8, 55.88]}], None, move_wires=True,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["code"] == "connectivity_changed", result


def test_to_needs_a_selector_that_picks_one_symbol(sheet):
    result = sheet_editing.move("symbol", str(sheet), [{"pattern": "R*", "to": [10, 10]}], None, move_wires=True,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["code"] == "invalid_spec" and "exactly one" in result["reason"]


# --------------------------------------------------------------------------- move_labels


def test_move_labels_along_the_wire_keeps_the_net(sheet):
    result = sheet_editing.move("label", str(sheet), [{"texts": ["NETA"], "offset": [-5.08, 0]}], None, move_wires=True,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True and result["moved"][0]["to"] == [58.42, 40.64], result
    assert _nets(sheet)["/NETA"] == frozenset({("R1", "1"), ("R2", "1")})


def test_move_labels_off_the_wire_renames_the_net_and_is_refused(sheet):
    result = sheet_editing.move("label", str(sheet), [{"texts": ["NETA"], "offset": [0, -10.16]}], None, move_wires=True,
                                snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)
    assert result["code"] == "connectivity_changed", result


# --------------------------------------------------------------------------- set_property_positions


def test_set_property_positions_moves_and_justifies_the_texts_and_lists_the_pins(sheet):
    result = sheet_editing.set_property_positions(str(sheet), [
        {"pattern": "R*", "properties": {"Reference": {"offset": [3.81, -1.27], "justify": "left"},
                                         "Value": {"offset": [3.81, 1.27], "angle": 90, "justify": "left bottom"}}},
    ], None, dry_run=False)
    assert result["ok"] is True and result["count"] == 4, result
    r1 = _symbol(sheet, "R1")
    ref = sheet_editing._property(r1, "Reference")
    assert sheet_editing._at(ref) == (54.61, 49.53, 0.0)
    val = sheet_editing._property(r1, "Value")
    assert sheet_editing._at(val) == (54.61, 52.07, 90.0)
    assert write_layer.serialise(val.find_children("effects")[0], compact=True).count("(justify left bottom)") == 1
    assert {p["number"] for p in result["updated"][0]["pins"]} == {"1", "2"}
    assert sheet_editing._at(sheet_editing._property(_symbol(sheet, "C1"), "Reference"))[:2] == (101.6, 45.72)  # not selected: untouched


def test_set_property_positions_absolute_and_clearing_justify(sheet):
    result = sheet_editing.set_property_positions(str(sheet), [
        {"references": ["C1"], "properties": {"Value": {"at": [120.0, 40.0], "justify": ""}}}], None, dry_run=False)
    assert result["ok"] is True, result
    val = sheet_editing._property(_symbol(sheet, "C1"), "Value")
    assert sheet_editing._at(val)[:2] == (120.0, 40.0)
    assert not val.find_children("effects")[0].find_children("justify")


def test_set_property_positions_refuses_a_missing_property_or_bad_justify(sheet):
    before = _bytes(sheet)
    a = sheet_editing.set_property_positions(str(sheet), [{"references": ["R1"], "properties": {"Nope": {"angle": 0}}}], None, dry_run=False)
    assert a["code"] == "property_not_found"
    b = sheet_editing.set_property_positions(str(sheet), [{"references": ["R1"], "properties": {"Value": {"justify": "left right"}}}], None, dry_run=False)
    assert b["code"] == "invalid_spec"
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- the registered tools


def test_the_registered_tools_run_and_move_wires_defaults_to_true(sheet, tools):
    result = tools["move_symbols"](str(sheet), [{"references": ["R2"], "offset": [0, 2.54]}])
    assert result["ok"] is True and result["move_wires"] is True, result
    assert tools["delete_junctions"](str(sheet), [{"region": [100, 75, 125, 85]}])["count"] == 1
    assert tools["delete_labels"](str(sheet), [{"texts": ["GNDX"]}])["code"] == "connectivity_changed"
    assert tools["delete_wires"](str(sheet), [{"net": "NETA"}], allow_connectivity_change=True)["count"] == 3
    assert tools["delete_symbols"](str(sheet), [{"references": ["C1"]}])["count"] == 1
    assert tools["move_labels"](str(sheet), [{"texts": ["GNDX"], "offset": [0, 1.27]}], allow_connectivity_change=True)["ok"] is True
    assert tools["set_property_positions"](str(sheet), [{"references": ["R1"], "properties": {"Reference": {"angle": 90}}}])["ok"] is True


# --------------------------------------------------------------------------- self-checks


def test_the_reload_check_fires_on_a_corrupted_write(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = sheet_editing.delete("junction", str(sheet), [{"region": [100, 75, 125, 85]}], None, remove_dangling=False,
                                  allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sheet) == before
    assert not [p for p in sheet.parent.iterdir() if p.name.endswith(".tmp")]


def _corrupted_write_is_refused(sheet, monkeypatch, tool, args):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _tools()[tool](str(sheet), *args)
    assert result["ok"] is False and result["code"] == "write_not_verified", (tool, result)
    monkeypatch.undo()
    assert _bytes(sheet) == before


def test_delete_symbols_corrupted_write_is_refused(sheet, monkeypatch):
    _corrupted_write_is_refused(sheet, monkeypatch, "delete_symbols", ([{"references": ["C1"]}],))


def test_delete_wires_corrupted_write_is_refused(sheet, monkeypatch):
    _corrupted_write_is_refused(sheet, monkeypatch, "delete_wires", ([{"net": "NETA"}], True))


def test_delete_labels_corrupted_write_is_refused(sheet, monkeypatch):
    _corrupted_write_is_refused(sheet, monkeypatch, "delete_labels", ([{"texts": ["NETA"]}], True))


def test_delete_junctions_corrupted_write_is_refused(sheet, monkeypatch):
    _corrupted_write_is_refused(sheet, monkeypatch, "delete_junctions", ([{"region": [100, 75, 125, 85]}],))


def test_move_symbols_corrupted_write_is_refused(sheet, monkeypatch):
    _corrupted_write_is_refused(sheet, monkeypatch, "move_symbols", ([{"references": ["C1"], "offset": [2.54, 0]}],))


def test_move_labels_corrupted_write_is_refused(sheet, monkeypatch):
    _corrupted_write_is_refused(sheet, monkeypatch, "move_labels", ([{"texts": ["NETA"], "offset": [-2.54, 0]}],))


def test_set_property_positions_corrupted_write_is_refused(sheet, monkeypatch):
    _corrupted_write_is_refused(sheet, monkeypatch, "set_property_positions", ([{"references": ["C1"], "properties": {"Value": {"angle": 90}}}],))


def test_the_staged_check_fires_when_the_mutation_touches_more_than_it_selected(sheet, monkeypatch):
    real = sheet_editing._remove

    def greedy(parent, doomed):
        extra = [c for c in parent.children if not c.is_atom and c.name == "wire"][:1]
        real(parent, [*doomed, *extra])

    monkeypatch.setattr(sheet_editing, "_remove", greedy)
    before = _bytes(sheet)
    result = sheet_editing.delete("junction", str(sheet), [{"region": [100, 75, 125, 85]}], None, remove_dangling=False,
                                  allow_connectivity_change=True, dry_run=False)
    assert result["ok"] is False and result["code"] == "write_not_verified" and "not selected" in result["reason"], result
    assert _bytes(sheet) == before


def test_the_connectivity_check_fires_when_the_comparison_is_blind(sheet, monkeypatch):
    """Mutation check of the guard: with the comparison disabled the unsafe delete would go through."""
    monkeypatch.setattr(sheet_editing, "compare_nets", lambda *a, **k: [])
    result = sheet_editing.delete("label", str(sheet), [{"texts": ["NETA"]}], None, remove_dangling=False,
                                  allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is True  # the guard is the comparison: this is what its absence looks like
    monkeypatch.undo()


def test_a_lock_file_refuses_every_tool_before_anything_is_computed(sheet, tools):
    lock = sheet.with_name(f"~{sheet.name}.lck")
    lock.write_bytes(b'{"hostname":"other","username":"x"}')
    before = _bytes(sheet)
    for name, args in {"delete_labels": [{"texts": ["NETA"]}], "delete_wires": [{"net": "NETA"}], "delete_symbols": [{"references": ["C1"]}],
                       "delete_junctions": [{"region": [0, 0, 200, 200]}], "move_symbols": [{"references": ["C1"], "offset": [1.27, 0]}],
                       "move_labels": [{"texts": ["NETA"], "offset": [1.27, 0]}],
                       "set_property_positions": [{"references": ["C1"], "properties": {"Value": {"angle": 90}}}]}.items():
        result = tools[name](str(sheet), args)
        assert result["code"] == "file_open_in_kicad" and result["ok"] is False, (name, result)
    assert _bytes(sheet) == {**before, lock.name: lock.read_bytes()}


# --------------------------------------------------------------------------- a sheet inside a tree


def test_positions_change_but_instance_data_is_untouched_on_an_attached_sheet(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "tree"), "tree")
    root = Path(made["schematic_path"])
    assert tools["create_sheets"](str(root), [{"file": "child.kicad_sch"}])["status"] == "ok"
    child = root.parent / "child.kicad_sch"
    assert tools["add_symbols"](str(child), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
                                             {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"}])["status"] == "ok"
    assert tools["add_wires"](str(child), [_wire((50.8, 54.61), (76.2, 46.99))])["status"] == "ok"
    attach = tools["add_sheet_symbols"](str(root), [{"file": "child.kicad_sch", "name": "Child", "x": 101.6, "y": 50.8,
                                                      "pins": [{"name": "A", "direction": "input"}]}])
    assert attach["status"] == "ok", attach
    instances_before = [write_layer.serialise(s.find_children("instances")[0], compact=True) for s in _nodes(child, "symbol")]

    result = tools["move_symbols"](str(child), [{"references": ["R2"], "offset": [2.54, 2.54]}], root_sch=str(root))
    assert result["ok"] is True and result["connectivity"]["changed"] is False, result
    assert sheet_editing._at(_symbol(child, "R2"))[:2] == (78.74, 53.34)
    assert [write_layer.serialise(s.find_children("instances")[0], compact=True) for s in _nodes(child, "symbol")] == instances_before
    tree = sheet_tree.walk(root)
    comps = sheet_tree.netlist_components(root)
    assert sheet_tree.netlist_disagreement(tree, [c["ref"] for c in comps]) == []
    # and the same call without root_sch finds the project from the folder
    assert tools["delete_labels"](str(child), [{"texts": ["X"]}])["code"] == "not_found"


# --------------------------------------------------------------------------- get_symbol_pins (issue 92)


def _mirror_symbols(sch: Path, mirrors: dict[str, str]) -> None:
    """Give the placed symbols named in ``mirrors`` (reference -> x or y) a (mirror ..) node, through the write layer."""
    root = write_layer.load(sch)
    for node in [c for c in root.children if not c.is_atom and c.name == "symbol" and c.find_children("lib_id")]:
        mirror = mirrors.get(sheet_editing._ref(node))
        if mirror:
            at = node.find_children("at")[0]
            node.children.insert(node.children.index(at) + 1, write_layer.SExp.list("mirror", write_layer.symbol(mirror)))
    write_layer.save(root, sch)


def test_get_symbol_pins_positions_agree_with_the_netlist_for_rotations_mirrors_units_and_extends(tmp_path, tools):
    """A label on each reported pin point puts that very pin on the label's net in kicad-cli's netlist.
    Covers Device:R in four rotations, mirrored R, a three-unit LM324 (units 1, 2 and the power unit 5),
    a BC547, a connector and Device:Filter_EMI_C (a symbol that extends another)."""
    made = project_toolset._project_create(str(tmp_path / "gsp"), "gsp")
    sch = Path(made["schematic_path"])
    specs = [{"lib_id": "Device:R", "x": 25.4 + 20.32 * i, "y": 25.4, "ref": f"R{i + 1}", "value": "1k", "rotation": rot}
             for i, rot in enumerate((0, 90, 180, 270))]
    specs += [{"lib_id": "Device:R", "x": 25.4 + 20.32 * i, "y": 50.8, "ref": f"R{i + 5}", "value": "1k", "rotation": rot}
              for i, rot in enumerate((0, 90, 180, 270))]
    specs += [{"lib_id": "Amplifier_Operational:LM324", "x": 30.48 + 40.64 * (u - 1), "y": 101.6, "ref": "U1", "value": "LM324", "unit": u}
              for u in (1, 2, 5)]
    specs += [{"lib_id": "Transistor_BJT:BC547", "x": 30.48, "y": 152.4, "ref": "Q1", "value": "BC547", "rotation": 90},
              {"lib_id": "Connector_Generic:Conn_01x04", "x": 76.2, "y": 152.4, "ref": "J1", "value": "Conn", "rotation": 180},
              {"lib_id": "Device:Filter_EMI_C", "x": 121.92, "y": 152.4, "ref": "FL1", "value": "EMI", "rotation": 270}]
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"
    _mirror_symbols(sch, {"R5": "x", "R6": "y", "R7": "x", "R8": "y", "Q1": "y", "FL1": "x"})

    got = tools["get_symbol_pins"](str(sch), ["*"])
    assert got["status"] == "ok" and got["ok"] is True, got
    by_ref = {}
    for entry in got["symbols"]:
        by_ref.setdefault(entry["reference"], []).append(entry)
    assert sorted(by_ref) == sorted({s["ref"] for s in specs})
    assert sorted(e["unit"] for e in by_ref["U1"]) == [1, 2, 5]
    labels, expected = [], {}
    for entry in got["symbols"]:
        for pin in entry["pins"]:
            text = f"{entry['reference']}_u{entry['unit']}_{pin['number']}"
            labels.append({"kind": "label", "x": pin["x"], "y": pin["y"], "text": text, "snap": False})
            expected[text] = (entry["reference"], pin["number"])
    assert len(labels) >= 8 * 2 + 3 + 3 + 3 + 4 + 2
    assert tools["add_labels"](str(sch), labels)["status"] == "ok"
    nets = _nets(sch)
    for text, node in expected.items():
        assert node in nets.get("/" + text, frozenset()), f"{text}: {sorted(n for n in nets if text in n)}"


def test_get_symbol_pins_reports_name_type_and_the_raw_library_facts(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "gsp2"), "gsp2")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
        {"lib_id": "Amplifier_Operational:LM324", "x": 101.6, "y": 50.8, "ref": "U1", "value": "LM324", "unit": 5},
        {"lib_id": "Transistor_BJT:BC547", "x": 152.4, "y": 50.8, "ref": "Q1", "value": "BC547"},
    ])["status"] == "ok"
    got = tools["get_symbol_pins"](str(sch), ["R1", "U*", "Q1"])
    pins = {e["reference"]: {p["number"]: p for p in e["pins"]} for e in got["symbols"]}
    assert pins["R1"]["1"]["name"] in ("", "~")  # the library's unnamed pin, as the sheet stores it
    assert {k: pins["R1"]["1"][k] for k in ("number", "type", "x", "y")} == {"number": "1", "type": "passive", "x": 50.8, "y": 46.99}
    assert pins["R1"]["2"]["y"] == 54.61
    assert {p["type"] for p in pins["U1"].values()} == {"power_in"}
    assert sorted(pins["U1"]) == ["11", "4"]
    assert {p["name"] for p in pins["Q1"].values()} == {"B", "C", "E"}
    assert {p["type"] for p in pins["Q1"].values()} == {"input", "passive"}
    assert {e["reference"]: e["at"] for e in got["symbols"]}["R1"] == [50.8, 50.8]


def test_get_symbol_pins_is_read_only_batch_and_refuses_a_missing_reference(sheet, tools):
    before = _bytes(sheet)
    both = tools["get_symbol_pins"](str(sheet), ["R1", "C1"])
    assert [e["reference"] for e in both["symbols"]] == ["C1", "R1"] and both["count"] == 2
    missing = tools["get_symbol_pins"](str(sheet), ["R1", "R99", "X*"])
    assert missing["ok"] is False and set(missing["not_found"]) == {"R99", "X*"}
    assert tools["get_symbol_pins"](str(sheet), [])["ok"] is False
    assert tools["get_symbol_pins"]("/no/such.kicad_sch", ["R1"])["ok"] is False
    assert _bytes(sheet) == before


def test_get_symbol_pins_is_registered_read_only():
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "get_symbol_pins")  # type: ignore[attr-defined]
    assert tool.annotations.read_only_hint is True


def test_get_symbol_pins_selects_by_uuid_even_for_two_symbols_with_one_reference(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "gsp3"), "gsp3")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R?", "value": "1k"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R?", "value": "2k"},
    ])["status"] == "ok"
    uuids = [sheet_editing._uuid(s) for s in _nodes(sch, "symbol") if s.find_children("lib_id")]
    assert len(set(uuids)) == 2
    one = tools["get_symbol_pins"](str(sch), [], uuids=[uuids[1]])
    assert one["ok"] is True and [e["uuid"] for e in one["symbols"]] == [uuids[1]]
    assert one["symbols"][0]["at"] == [76.2, 50.8]
    ored = tools["get_symbol_pins"](str(sch), ["R?"], uuids=[uuids[0]])
    assert ored["count"] == 2
    before = _bytes(sch)
    bad = tools["get_symbol_pins"](str(sch), [], uuids=[uuids[0], "no-such-uuid"])
    assert bad["ok"] is False and bad["not_found"] == ["no-such-uuid"] and _bytes(sch) == before
    assert tools["get_symbol_pins"](str(sch), [])["ok"] is False


def test_get_symbol_pins_without_the_embedded_library_symbol_gives_no_pins_and_a_warning(sheet, tools):
    root = write_layer.load(sheet)
    table = next(c for c in root.children if not c.is_atom and c.name == "lib_symbols")
    table.children[:] = [c for c in table.children if c.is_atom or c.get_string(0) != "Device:R"]
    write_layer.save(root, sheet)
    got = tools["get_symbol_pins"](str(sheet), ["R1", "C1"])
    by = {e["reference"]: e for e in got["symbols"]}
    assert by["R1"]["pins"] == [] and "warning" in by["R1"] and by["C1"]["pins"]
