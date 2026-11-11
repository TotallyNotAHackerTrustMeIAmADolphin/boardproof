"""rotate_items / mirror_items (GitHub issue 93), with real kicad-cli.

The downstream project rotated and mirrored symbols by editing the schematic text. These tests build one
small sheet through the tools (shared by the module, copied per test), turn its symbols in place and read
the result back from the raw file and from kicad-cli's netlist. Pin geometry is cross-checked against the
raw ``(at ...)`` of the library pins in the sheet's own file, never against a ``kicad_tools`` property.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_authoring, sheet_editing, sheet_orient, write_layer
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


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    """R1 (50.8, 50.8) with a wire up from its pin 1 to the label TOP and a junction plus a stub wire on the same
    point; its pin 2 touches pin 1 of R4 (50.8, 58.42) directly; R4's pin 2 has a wire down to the label BOT.
    R2 (101.6, 50.8): a no-connect marker on pin 1 and the label R2B on pin 2. R3 (152.4, 50.8): alone."""
    tools = _tools()
    base = tmp_path_factory.mktemp("orient")
    made = project_toolset._project_create(str(base / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"},
        {"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R2", "value": "1k"},
        {"lib_id": "Device:R", "x": 152.4, "y": 50.8, "ref": "R3", "value": "2k"},
        {"lib_id": "Device:R", "x": 50.8, "y": 58.42, "ref": "R4", "value": "4k"},
    ])["status"] == "ok"
    assert tools["add_wires"](str(sch), [
        {"p1": [50.8, 46.99], "p2": [50.8, 38.1], "route": "direct"},
        {"p1": [50.8, 46.99], "p2": [45.72, 46.99], "route": "direct"},
        {"p1": [50.8, 62.23], "p2": [50.8, 71.12], "route": "direct"},
    ])["status"] == "ok"
    assert tools["add_junctions"](str(sch), [[50.8, 46.99]])["status"] == "ok"
    assert tools["add_labels"](str(sch), [
        {"kind": "label", "x": 50.8, "y": 38.1, "text": "TOP", "snap": False},
        {"kind": "label", "x": 50.8, "y": 71.12, "text": "BOT", "snap": False},
        {"kind": "label", "x": 101.6, "y": 54.61, "text": "R2B", "snap": False},
    ])["status"] == "ok"
    assert tools["add_no_connects"](str(sch), [{"references": ["R2"], "pins": ["1"]}])["status"] == "ok"
    return sch.parent


@pytest.fixture
def sheet(template, tmp_path) -> Path:
    shutil.copytree(template, tmp_path / "p")
    return tmp_path / "p" / "demo.kicad_sch"


@pytest.fixture
def tools() -> dict:
    return _tools()


def _root(sch: Path):
    return write_layer.load(sch)


def _nodes(sch: Path, tag: str):
    return [c for c in _root(sch).children if not c.is_atom and c.name == tag]


def _symbol(sch: Path, ref: str):
    return next(s for s in _nodes(sch, "symbol") if sheet_editing._ref(s) == ref)


def _angle(sch: Path, ref: str) -> float:
    return sheet_editing._at(_symbol(sch, ref))[2]


def _mirror(sch: Path, ref: str) -> str:
    return sheet_editing._mirror(_symbol(sch, ref))


def _pins(sch: Path, ref: str) -> dict[str, tuple[float, float]]:
    root = _root(sch)
    return {p["number"]: (p["x"], p["y"]) for p in sheet_editing.pin_positions(root, _symbol(sch, ref))}


def _prop(sch: Path, ref: str, name: str) -> tuple[float, float, float]:
    return sheet_editing._at(sheet_editing._property(_symbol(sch, ref), name))


def _nets(sch: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    return sheet_editing.read_nets(tree, sch, None)


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _rotate(sch, items, **kw):
    return sheet_orient.rotate(str(sch), items, None, **kw)


def _mirror_items(sch, items, **kw):
    return sheet_orient.mirror(str(sch), items, None, **kw)


def _wire(sch: Path, ends: list[tuple[float, float]]):
    for w in _nodes(sch, "wire"):
        if sorted((round(a, 3), round(b, 3)) for a, b in sheet_editing._wire_ends(w)) == sorted(ends):
            return w
    return None


# --------------------------------------------------------------------------- the facts, read from the raw file


def test_the_library_pins_of_device_r_are_where_the_expected_positions_assume(sheet):
    """Raw (at ...) of the library pins: pin 1 at (0, 3.81) pointing down (angle 270), pin 2 at (0, -3.81) (angle 90).
    Library Y is up, so pin 1 is the top pin of an upright part; every expectation below rests on this."""
    root = _root(sheet)
    lib = next(e for t in root.children if not t.is_atom and t.name == "lib_symbols" for e in t.children
               if not e.is_atom and e.name == "symbol" and e.get_string(0) == "Device:R")
    pins = {p.find_children("number")[0].get_string(0): p.find_children("at")[0]
            for sub in lib.children if not sub.is_atom and sub.name == "symbol" for p in sub.children if not p.is_atom and p.name == "pin"}
    assert (pins["1"].get_float(0), pins["1"].get_float(1)) == (0.0, 3.81)
    assert (pins["2"].get_float(0), pins["2"].get_float(1)) == (0.0, -3.81)
    assert _pins(sheet, "R1") == {"1": (50.8, 46.99), "2": (50.8, 54.61)}


# --------------------------------------------------------------------------- rotate


def test_rotate_by_90_turns_counter_clockwise_and_every_net_stays(sheet):
    """Top pin goes to the left, bottom pin to the right; the netlist is the same because the label and the marker followed their pins."""
    nets_before = _nets(sheet)
    result = _rotate(sheet, [{"references": ["R2"], "by": 90}])
    assert result["ok"] is True and result["written"] is True, result
    assert _angle(sheet, "R2") == 90.0 and _mirror(sheet, "R2") == ""
    assert _pins(sheet, "R2") == {"1": (97.79, 50.8), "2": (105.41, 50.8)}
    assert result["rotated"][0]["before"] == {"rotation": 0, "mirror": ""} and result["rotated"][0]["after"] == {"rotation": 90, "mirror": ""}
    assert _nets(sheet) == nets_before and result["connectivity"]["changed"] is False


def test_label_and_marker_on_pins_follow_their_own_pin(sheet):
    result = _rotate(sheet, [{"references": ["R2"], "by": 90}])
    assert result["ok"] is True, result
    followed = result["followed"]
    assert [n["now"] for n in followed["no_connects"]] == [[97.79, 50.8]]
    assert [n["now"] for n in followed["labels"]] == [[105.41, 50.8]] and followed["wires"] == []
    assert sheet_editing._at(_nodes(sheet, "no_connect")[0])[:2] == (97.79, 50.8)
    label = next(n for n in _nodes(sheet, "label") if n.get_string(0) == "R2B")
    assert sheet_editing._at(label)[:2] == (105.41, 50.8)
    assert result["dangling_created"] == {"wires": [], "junctions": [], "labels": []}


def test_a_wire_end_on_a_rotated_pin_follows_the_pin_and_is_listed_slanted(sheet):
    _wire_up_r3(sheet)
    nets_before = _nets(sheet)
    result = _rotate(sheet, [{"references": ["R3"], "by": 90}])
    assert result["ok"] is True and result["written"] is True, result
    assert _pins(sheet, "R3") == {"1": (148.59, 50.8), "2": (156.21, 50.8)}
    assert _wire(sheet, [(152.4, 38.1), (148.59, 50.8)]) is not None
    assert _wire(sheet, [(156.21, 50.8), (152.4, 63.5)]) is not None
    assert len(result["slanted"]) == 2 and len(result["followed"]["wires"]) == 2
    assert _nets(sheet) == nets_before


def _wire_up_r3(sheet: Path) -> None:
    tools = _tools()
    assert tools["add_wires"](str(sheet), [{"p1": [152.4, 46.99], "p2": [152.4, 38.1], "route": "direct"},
                                           {"p1": [152.4, 54.61], "p2": [152.4, 63.5], "route": "direct"}])["status"] == "ok"
    assert tools["add_labels"](str(sheet), [{"kind": "label", "x": 152.4, "y": 38.1, "text": "UP", "snap": False},
                                            {"kind": "label", "x": 152.4, "y": 63.5, "text": "DN", "snap": False}])["status"] == "ok"


def test_a_pin_touching_an_unmoved_pin_is_refused_by_the_netlist_comparison(sheet):
    """R1's pin 2 meets R4's pin 1; turning R1 alone breaks that contact: connectivity_changed, bytes unchanged."""
    before = _bytes(sheet)
    result = _rotate(sheet, [{"references": ["R1"], "by": 90}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert _bytes(sheet) == before


def test_two_pins_at_one_point_that_move_differently_are_refused_as_ambiguous(sheet):
    before = _bytes(sheet)
    result = _rotate(sheet, [{"references": ["R1"], "by": 90}, {"references": ["R4"], "by": 270}])
    assert result["ok"] is False and result["code"] == "ambiguous_drag", result
    assert _bytes(sheet) == before


def test_a_half_turn_of_a_two_pin_part_wired_on_both_sides_is_refused_as_a_crossing(sheet):
    """The pins swap places over their wires: each wire would be dragged across the body onto the other pin's old place."""
    _wire_up_r3(sheet)
    before = _bytes(sheet)
    result = _rotate(sheet, [{"references": ["R3"], "by": 180}])
    assert result["ok"] is False and result["code"] == "drag_crosses_connection", result
    assert {c["point"] for c in map(lambda c: {"point": tuple(c["point"])}, result["crossings"])} >= {(152.4, 46.99), (152.4, 54.61)}
    assert _bytes(sheet) == before


def test_the_crossing_check_fires_when_it_is_blind(sheet, monkeypatch):
    """Mutant: without the check the half turn is written with two wires laid over each other (kicad-cli keeps the nets,
    which is why only a geometric check can refuse it)."""
    _wire_up_r3(sheet)
    monkeypatch.setattr(sheet_orient, "_crossings", lambda *a, **k: set())
    result = _rotate(sheet, [{"references": ["R3"], "by": 180}])
    assert result["ok"] is True and result["written"] is True, result


def test_without_move_wires_nothing_attached_moves_and_the_comparison_refuses(sheet):
    before = _bytes(sheet)
    result = _rotate(sheet, [{"references": ["R2"], "by": 90}], move_wires=False)
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert _bytes(sheet) == before


def test_allow_connectivity_change_writes_the_change_and_reports_it(sheet):
    result = _rotate(sheet, [{"references": ["R2"], "by": 90}], move_wires=False, allow_connectivity_change=True)
    assert result["ok"] is True and result["written"] is True and result["connectivity"]["changed"] is True, result
    marker = _nodes(sheet, "no_connect")[0]
    assert sheet_editing._at(marker)[:2] == (101.6, 46.99)  # not dragged
    assert len(result["dangling_created"]["labels"]) == 1  # R2B now touches no pin


def test_property_texts_follow_by_default_and_keep_leaves_them(sheet):
    ox, oy, _ = sheet_editing._at(_symbol(sheet, "R2"))
    rx, ry, ra = _prop(sheet, "R2", "Reference")
    result = _rotate(sheet, [{"references": ["R2"], "to": 90}])
    assert result["ok"] is True, result
    # counter-clockwise on the screen with Y down: an offset (dx, dy) becomes (dy, -dx); the text's own angle stays
    assert _prop(sheet, "R2", "Reference") == pytest.approx((ox + (ry - oy), oy - (rx - ox), ra))
    listed = result["rotated"][0]["property_texts"]
    assert {t["name"] for t in listed} >= {"Reference", "Value"}


def test_keep_leaves_property_texts_where_they_are(sheet):
    ref_before = _prop(sheet, "R2", "Reference")
    assert _rotate(sheet, [{"references": ["R2"], "by": 90}], property_texts="keep")["ok"] is True
    assert _prop(sheet, "R2", "Reference") == ref_before


def test_to_the_present_angle_changes_nothing_and_writes_nothing(sheet):
    before = _bytes(sheet)
    result = _rotate(sheet, [{"references": ["R2"], "to": 0}])
    assert result["ok"] is True and result["written"] is False and result["unchanged"] == ["R2"], result
    assert _bytes(sheet) == before


def test_dry_run_returns_the_plan_and_writes_nothing(sheet):
    before = _bytes(sheet)
    result = _rotate(sheet, [{"references": ["R2"], "by": 90}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and "would_rotate" in result and "rotated" not in result, result
    assert result["would_rotate"][0]["after"] == {"rotation": 90, "mirror": ""}
    assert _bytes(sheet) == before


def test_pins_that_end_off_the_grid_are_listed(sheet):
    tools = _tools()
    assert tools["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": 180.0, "y": 90.0, "ref": "R9", "value": "1k", "snap": False}])["status"] == "ok"
    result = _rotate(sheet, [{"references": ["R9"], "by": 90}], dry_run=True)
    assert result["ok"] is True and result["off_grid_pins"] and result["off_grid_pins"][0]["reference"] == "R9", result
    assert result["grid"] == 1.27


@pytest.mark.parametrize("items", [
    [], None, "R1", [{"references": ["R1"]}], [{"references": ["R1"], "by": 90, "to": 0}], [{"references": ["R1"], "by": 45}],
    [{"references": ["R1"], "by": True}], [{"references": ["R1"], "to": 360}], [{"by": 90}], [{"references": ["R1"], "by": 90, "bogus": 1}],
    [{"references": ["R1"], "axis": "x"}], [{"references": ["R2"], "by": 90}, {"references": ["R2"], "by": 90}],
    [{"references": ["R2"], "by": 90}, {"pattern": "R*", "by": 90}],
])
def test_a_bad_item_refuses_the_whole_call_and_writes_nothing(sheet, items):
    before = _bytes(sheet)
    result = _rotate(sheet, items)
    assert result["ok"] is False and result["code"] in ("invalid_spec", "unusual_orientation"), result
    assert _bytes(sheet) == before


def test_an_unknown_reference_is_refused_as_not_found(sheet):
    before = _bytes(sheet)
    result = _rotate(sheet, [{"references": ["R99"], "by": 90}])
    assert result["ok"] is False and result["code"] == "not_found", result
    assert _bytes(sheet) == before


def test_bad_options_are_refused(sheet):
    for kw in ({"move_wires": "yes"}, {"property_texts": "rotate"}, {"grid": 0}, {"grid": float("nan")}):
        result = _rotate(sheet, [{"references": ["R2"], "by": 90}], **kw)
        assert result["ok"] is False and result["code"] == "invalid_spec", (kw, result)


def test_an_orientation_the_tool_cannot_prove_is_refused(sheet):
    """KiCad itself will not load such a symbol from a file, so the guard is tested on the in-memory node."""
    node = _symbol(sheet, "R2")
    write_layer.set_number(node.find_children("at")[0], 2, 45)
    with pytest.raises(sheet_orient.SheetAuthoringError) as err:
        sheet_orient._stored_rotation(node, "R2")
    assert err.value.code == "unusual_orientation"
    write_layer.set_number(node.find_children("at")[0], 2, 90)
    node.children.insert(node.children.index(node.find_children("at")[0]) + 1, write_layer.SExp.list("mirror", write_layer.symbol("z")))
    with pytest.raises(sheet_orient.SheetAuthoringError) as err:
        sheet_orient._stored_rotation(node, "R2")
    assert err.value.code == "unusual_orientation"


# --------------------------------------------------------------------------- the guards, each with a mutant


def test_a_corrupted_write_is_refused_and_leaves_the_sheet_alone(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _tools()["rotate_items"](str(sheet), [{"references": ["R2"], "by": 90}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sheet) == before


def test_the_orientation_check_fires_when_the_angle_written_is_wrong(sheet, monkeypatch):
    """Mutant: the stored angle is off by a quarter turn; the pins are then not where the change puts them."""
    real = sheet_orient._set_orientation
    monkeypatch.setattr(sheet_orient, "_set_orientation", lambda node, rot, mirror: real(node, (rot + 90) % 360, mirror))
    before = _bytes(sheet)
    result = _tools()["rotate_items"](str(sheet), [{"references": ["R2"], "by": 90}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result.get("check") == "orientation", result
    assert _bytes(sheet) == before


def test_the_connectivity_check_fires_when_the_comparison_is_blind(sheet, monkeypatch):
    """Mutant: with the comparison disabled the unsafe turn (R1's contact with R4 broken) goes through."""
    assert _rotate(sheet, [{"references": ["R1"], "by": 90}])["code"] == "connectivity_changed"
    monkeypatch.setattr(sheet_editing, "compare_nets", lambda *a, **k: [])
    assert _rotate(sheet, [{"references": ["R1"], "by": 90}])["ok"] is True


def test_the_staged_check_fires_when_an_unselected_node_changes(sheet, monkeypatch):
    original = sheet_orient._set_orientation

    def greedy(node, rot, mirror):
        original(node, rot, mirror)
        stray = next(n for n in sheet_editing._kids(sheet_editing_root[0], "symbol") if sheet_editing._ref(n) == "R3")
        sheet_editing._set_at(stray, 200.0, 200.0)

    sheet_editing_root = []
    real_run = sheet_editing.run

    def capture(path, root_sch, mutate, **kw):
        def wrapped(index, root):
            sheet_editing_root[:] = [root]
            return mutate(index, root)
        return real_run(path, root_sch, wrapped, **kw)

    monkeypatch.setattr(sheet_editing, "run", capture)
    monkeypatch.setattr(sheet_orient, "_set_orientation", greedy)
    before = _bytes(sheet)
    result = _tools()["rotate_items"](str(sheet), [{"references": ["R2"], "by": 90}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- mirror


def test_mirror_x_swaps_top_and_bottom_and_the_marker_and_label_swap_with_their_pins(sheet):
    """Measured: (mirror x) puts pin 1 of an upright Device:R at the bottom. The marker (on pin 1) and the label (on pin 2)
    follow their own pins in one pass, so they trade places and the nets stay."""
    nets_before = _nets(sheet)
    result = _mirror_items(sheet, [{"references": ["R2"], "axis": "x"}])
    assert result["ok"] is True and result["written"] is True, result
    assert _mirror(sheet, "R2") == "x" and _angle(sheet, "R2") == 0.0
    assert _pins(sheet, "R2") == {"1": (101.6, 54.61), "2": (101.6, 46.99)}
    assert sheet_editing._at(_nodes(sheet, "no_connect")[0])[:2] == (101.6, 54.61)
    assert sheet_editing._at(next(n for n in _nodes(sheet, "label") if n.get_string(0) == "R2B"))[:2] == (101.6, 46.99)
    assert _nets(sheet) == nets_before and result["mirrored"][0]["after"] == {"rotation": 0, "mirror": "x"}
    # the mirror node sits right after (at ...), where KiCad writes it
    node = _symbol(sheet, "R2")
    at = node.find_children("at")[0]
    assert node.children[node.children.index(at) + 1].name == "mirror"


def test_mirror_y_of_an_upright_two_pin_part_moves_no_pin_but_mirrors_the_text_offsets(sheet):
    ox = sheet_editing._at(_symbol(sheet, "R2"))[0]
    rx, ry, ra = _prop(sheet, "R2", "Reference")
    result = _mirror_items(sheet, [{"references": ["R2"], "axis": "y"}])
    assert result["ok"] is True and result["written"] is True, result
    assert _mirror(sheet, "R2") == "y" and _pins(sheet, "R2") == {"1": (101.6, 46.99), "2": (101.6, 54.61)}
    assert result["followed"] == {"wires": [], "junctions": [], "labels": [], "no_connects": []}
    assert _prop(sheet, "R2", "Reference") == pytest.approx((ox - (rx - ox), ry, ra))


def test_mirroring_twice_about_the_same_axis_restores_the_symbol(sheet):
    before = {n: _prop(sheet, "R2", n) for n in ("Reference", "Value")}
    assert _mirror_items(sheet, [{"references": ["R2"], "axis": "x"}])["ok"] is True
    assert _mirror_items(sheet, [{"references": ["R2"], "axis": "x"}])["ok"] is True
    assert _mirror(sheet, "R2") == "" and not _symbol(sheet, "R2").find_children("mirror") and _angle(sheet, "R2") == 0.0
    assert {n: _prop(sheet, "R2", n) for n in before} == before
    assert _pins(sheet, "R2") == {"1": (101.6, 46.99), "2": (101.6, 54.61)}


def test_a_mirrored_part_turned_ends_up_where_kicad_reads_its_pins(sheet):
    """External oracle: labels at hand-derived pin places must name the right pins in kicad-cli's netlist.
    Upright R3, mirror x (pin 1 to the bottom), then 90 counter-clockwise (bottom goes to the right): pin 1 right, pin 2 left."""
    tools = _tools()
    assert _mirror_items(sheet, [{"references": ["R3"], "axis": "x"}])["ok"] is True
    assert _rotate(sheet, [{"references": ["R3"], "by": 90}])["ok"] is True
    assert (_angle(sheet, "R3"), _mirror(sheet, "R3")) == (270.0, "x")  # a mirrored part's angle runs the other way
    assert tools["add_labels"](str(sheet), [{"kind": "label", "x": 156.21, "y": 50.8, "text": "P1", "snap": False},
                                            {"kind": "label", "x": 148.59, "y": 50.8, "text": "P2", "snap": False}])["status"] == "ok"
    nets = _nets(sheet)
    assert nets["/P1"] == frozenset({("R3", "1")}) and nets["/P2"] == frozenset({("R3", "2")})


def test_to_on_a_mirrored_part_keeps_the_mirror_and_sets_the_stored_angle(sheet):
    assert _mirror_items(sheet, [{"references": ["R2"], "axis": "x"}])["ok"] is True
    result = _rotate(sheet, [{"references": ["R2"], "to": 90}])
    assert result["ok"] is True, result
    assert (_angle(sheet, "R2"), _mirror(sheet, "R2")) == (90.0, "x")
    # mirror x puts pin 1 at the bottom; stored 90 with mirror x (library turned 90 CCW, then flipped): pin 1 on the left
    assert _pins(sheet, "R2") == {"1": (97.79, 50.8), "2": (105.41, 50.8)}


def test_mirror_dry_run_and_bad_items(sheet):
    before = _bytes(sheet)
    result = _mirror_items(sheet, [{"references": ["R2"], "axis": "x"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and "would_mirror" in result and "mirrored" not in result
    for items in ([{"references": ["R2"]}], [{"references": ["R2"], "axis": "z"}], [{"references": ["R2"], "axis": "x", "by": 90}],
                  [{"axis": "x"}], [{"references": ["R2"], "axis": "x"}, {"references": ["R2"], "axis": "y"}], []):
        bad = _mirror_items(sheet, items)
        assert bad["ok"] is False and bad["code"] == "invalid_spec", (items, bad)
    assert _mirror_items(sheet, [{"references": ["R99"], "axis": "x"}])["code"] == "not_found"
    assert _bytes(sheet) == before


def test_mirror_corrupted_write_is_refused_and_the_orientation_check_fires(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _tools()["mirror_items"](str(sheet), [{"references": ["R2"], "axis": "x"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sheet) == before
    # mutant: the mirror node is never written
    monkeypatch.setattr(sheet_orient, "_set_orientation", lambda node, rot, mirror: None)
    result = _mirror_items(sheet, [{"references": ["R2"], "axis": "x"}])
    assert result["ok"] is False and result.get("check") == "orientation", result
    assert _bytes(sheet) == before


def test_a_multi_unit_reference_turns_every_unit_and_a_uuid_turns_one(tmp_path):
    tools = _tools()
    made = project_toolset._project_create(str(tmp_path / "mu"), "mu")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Amplifier_Operational:LM324", "x": 50.8, "y": 50.8, "ref": "U1", "value": "LM324", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM324", "x": 101.6, "y": 50.8, "ref": "U1", "value": "LM324", "unit": 2}])["status"] == "ok"
    units = _nodes(sch, "symbol")
    second = sheet_editing._uuid(units[1])
    one = _rotate(sch, [{"uuids": [second], "by": 90}])
    assert one["ok"] is True and one["count"] == 1 and one["rotated"][0]["unit"] == 2, one
    assert [sheet_editing._at(u)[2] for u in _nodes(sch, "symbol")] == [0.0, 90.0]
    both = _rotate(sch, [{"references": ["U1"], "by": 90}])
    assert both["ok"] is True and both["count"] == 2 and sorted(r["unit"] for r in both["rotated"]) == [1, 2], both
    assert [sheet_editing._at(u)[2] for u in _nodes(sch, "symbol")] == [90.0, 180.0]


def test_a_power_symbol_turns_in_place_and_its_wire_stays(tmp_path):
    tools = _tools()
    made = project_toolset._project_create(str(tmp_path / "pw"), "pw")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
                                           {"lib_id": "power:GND", "x": 50.8, "y": 63.5, "ref": "#PWR01", "value": "GND"}])["status"] == "ok"
    assert tools["add_wires"](str(sch), [{"p1": [50.8, 54.61], "p2": [50.8, 63.5], "route": "direct"}])["status"] == "ok"
    gnd = _symbol(sch, "#PWR01")
    pin = sheet_editing.pin_positions(_root(sch), gnd)[0]
    assert (pin["x"], pin["y"]) == (50.8, 63.5)  # the power pin sits on the origin of a GND symbol
    nets_before = _nets(sch)
    result = _rotate(sch, [{"references": ["#PWR01"], "by": 180}])
    assert result["ok"] is True and result["written"] is True and _angle(sch, "#PWR01") == 180.0, result
    assert _nets(sch) == nets_before and result["followed"]["wires"] == []  # the pin is at the origin: nothing to drag


# --------------------------------------------------------------------------- the orientation algebra (no kicad-cli)


@pytest.mark.parametrize("rot", sheet_orient.ROTATIONS)
@pytest.mark.parametrize("mirror", sheet_orient.MIRRORS)
def test_every_orientation_after_a_turn_or_a_flip_has_the_transformed_axes(rot, mirror):
    """For all twelve stored forms: the axes of the result are the old axes carried through the visual change."""
    ops = [(f"rot{p}", sheet_orient._rotate_ccw(p)) for p in sheet_orient.BY] + [("flip x", sheet_orient._flip("x")), ("flip y", sheet_orient._flip("y"))]
    for name, op in ops:
        new_rot, new_mirror = sheet_orient.orientation_after(rot, mirror, op, mirror)
        want = [op(*v) for v in sheet_orient._basis(rot, mirror)]
        got = sheet_orient._basis(new_rot, new_mirror)
        assert all(abs(a - b) < 1e-9 for w, g in zip(want, got, strict=True) for a, b in zip(w, g, strict=True)), (rot, mirror, name, new_rot, new_mirror)


def test_the_stored_form_that_is_kept_is_the_one_the_symbol_has():
    flip_x = sheet_orient._flip("x")
    assert sheet_orient.orientation_after(0, "", flip_x, "x") == (0, "x")
    assert sheet_orient.orientation_after(90, "y", sheet_orient._rotate_ccw(90), "y")[1] == "y"
    assert sheet_orient.orientation_after(0, "x", flip_x, "x") == (0, "")
    assert sheet_orient.orientation_after(0, "x", sheet_orient._flip("y"), "x") == (180, "")


# --------------------------------------------------------------------------- review round: end index, junction, pinless, crossings


def _r3_wires(sheet: Path, *, toward: bool, extra: str = "") -> None:
    """R3 (152.4, 50.8): a wire on its pin 1 going up to the label UP; ``toward`` draws it FROM the far end TO the pin
    (so the pin is the wire's last end, index 1)."""
    tools = _tools()
    p1, p2 = ([152.4, 30.48], [152.4, 46.99]) if toward else ([152.4, 46.99], [152.4, 30.48])
    assert tools["add_wires"](str(sheet), [{"p1": p1, "p2": p2, "route": "direct"}])["status"] == "ok"
    assert tools["add_labels"](str(sheet), [{"kind": "label", "x": 152.4, "y": 30.48, "text": "UP", "snap": False}])["status"] == "ok"


def test_a_wire_drawn_toward_the_pin_follows_it_with_its_last_end(sheet):
    _r3_wires(sheet, toward=True)
    wire = _wire(sheet, [(152.4, 30.48), (152.4, 46.99)])
    assert sheet_editing._wire_ends(wire)[-1] == (152.4, 46.99)  # raw: the pin is end index 1
    nets_before = _nets(sheet)
    result = _rotate(sheet, [{"references": ["R3"], "by": 90}])
    assert result["ok"] is True and result["written"] is True, result
    moved = _wire(sheet, [(152.4, 30.48), (148.59, 50.8)])
    assert moved is not None and sheet_editing._wire_ends(moved)[0] == (152.4, 30.48) and sheet_editing._wire_ends(moved)[-1] == (148.59, 50.8)
    assert _nets(sheet) == nets_before and len(result["slanted"]) == 1


def test_a_junction_on_a_pin_follows_it_with_its_wires(sheet):
    tools = _tools()
    _r3_wires(sheet, toward=False)
    assert tools["add_wires"](str(sheet), [{"p1": [152.4, 46.99], "p2": [157.48, 46.99], "route": "direct"}])["status"] == "ok"
    assert tools["add_junctions"](str(sheet), [[152.4, 46.99]])["status"] == "ok"
    nets_before = _nets(sheet)
    result = _rotate(sheet, [{"references": ["R3"], "by": 90}])
    assert result["ok"] is True and result["written"] is True, result
    assert [sheet_editing._at(j)[:2] for j in _nodes(sheet, "junction")] == [(50.8, 46.99), (148.59, 50.8)]  # the fixture's own stays
    assert [j["now"] for j in result["followed"]["junctions"]] == [[148.59, 50.8]] and len(result["followed"]["wires"]) == 2
    assert _nets(sheet) == nets_before


def test_a_symbol_without_pins_can_be_turned_and_one_without_a_library_symbol_is_refused(sheet):
    tools = _tools()
    assert tools["add_symbols"](str(sheet), [{"lib_id": "Mechanical:MountingHole", "x": 200.66, "y": 76.2, "ref": "H1", "value": "MH"}])["status"] == "ok"
    assert _pins(sheet, "H1") == {}
    result = _rotate(sheet, [{"references": ["H1"], "by": 90}])
    assert result["ok"] is True and result["written"] is True and _angle(sheet, "H1") == 90.0, result
    root = _root(sheet)
    lib = next(t for t in root.children if not t.is_atom and t.name == "lib_symbols")
    lib.children[:] = [e for e in lib.children if e.is_atom or e.get_string(0) != "Device:R"]
    write_layer.save(root, sheet)
    before = _bytes(sheet)
    refused = _rotate(sheet, [{"references": ["R2"], "by": 90}])
    assert refused["ok"] is False and refused["code"] == "no_symbol_geometry", refused
    assert _bytes(sheet) == before


def _add_nodes(sheet: Path, nodes: list) -> None:
    """Fixture only: put raw nodes on the sheet through the write layer (before the sheet_instances tail)."""
    root = _root(sheet)
    tail = next((i for i, c in enumerate(root.children) if not c.is_atom and c.name in ("sheet_instances", "embedded_fonts")), len(root.children))
    root.children[tail:tail] = nodes
    write_layer.save(root, sheet)


def _crossing_case(sheet: Path, probe: str) -> dict:
    """R3's pin 1 wire runs up to (151.41, 30.4); after a 90 degree turn it runs from (148.59, 50.8) to there, through
    (150.0, 40.6) (round numbers: a label is stored to 0.1 mm). Put exactly one kind of connection on that point."""
    tools = _tools()
    x, y = 150.0, 40.6
    _add_nodes(sheet, [write_layer.SExp.list(
        "wire", write_layer.SExp.list("pts", write_layer.SExp.list("xy", write_layer.number(152.4), write_layer.number(46.99)),
                                      write_layer.SExp.list("xy", write_layer.number(151.41), write_layer.number(30.4))),
        write_layer.SExp.list("stroke", write_layer.SExp.list("width", write_layer.number(0)), write_layer.SExp.list("type", write_layer.symbol("default"))),
        write_layer.SExp.list("uuid", write_layer.string("11111111-2222-3333-4444-666666666666")))])  # off the 1.27 grid, so not through add_wires
    if probe == "pin":  # pin 1 of a new part sits on the point (its pin 2 is off the run)
        assert tools["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": x, "y": y + 3.81, "ref": "R5", "value": "1k", "snap": False}])["status"] == "ok"
    elif probe == "label":
        assert tools["add_labels"](str(sheet), [{"kind": "label", "x": x, "y": y, "text": "PROBE", "snap": False}])["status"] == "ok"
    elif probe == "junction":
        _add_nodes(sheet, [write_layer.SExp.list("junction", write_layer.SExp.list("at", write_layer.number(x), write_layer.number(y)),
                                                 write_layer.SExp.list("diameter", write_layer.number(0)),
                                                 write_layer.SExp.list("uuid", write_layer.string("11111111-2222-3333-4444-777777777777")))])
    elif probe in ("wire_first_end", "wire_last_end"):  # a foreign wire whose first / last end lies on the point
        near, far = (x, y), (x, y - 5.0)
        a, b = (near, far) if probe == "wire_first_end" else (far, near)
        _add_nodes(sheet, [write_layer.SExp.list(
            "wire", write_layer.SExp.list("pts", write_layer.SExp.list("xy", write_layer.number(a[0]), write_layer.number(a[1])),
                                          write_layer.SExp.list("xy", write_layer.number(b[0]), write_layer.number(b[1]))),
            write_layer.SExp.list("stroke", write_layer.SExp.list("width", write_layer.number(0)), write_layer.SExp.list("type", write_layer.symbol("default"))),
            write_layer.SExp.list("uuid", write_layer.string("11111111-2222-3333-4444-888888888888")))])
    else:
        _add_nodes(sheet, [write_layer.SExp.list("no_connect", write_layer.SExp.list("at", write_layer.number(x), write_layer.number(y)),
                                                 write_layer.SExp.list("uuid", write_layer.string("11111111-2222-3333-4444-555555555555")))])
    return _rotate(sheet, [{"references": ["R3"], "by": 90}])


KINDS = ["pin", "label", "marker", "junction", "wire_first_end", "wire_last_end"]


@pytest.mark.parametrize("probe", KINDS)
def test_a_dragged_wire_over_only_a_pin_a_label_or_a_marker_is_refused(sheet, probe):
    result = _crossing_case(sheet, probe)
    assert result["ok"] is False and result["code"] == "drag_crosses_connection", (probe, result)
    assert [tuple(c["point"]) for c in result["crossings"]] == [(150.0, 40.6)]


@pytest.mark.parametrize("probe", KINDS)
def test_the_crossing_guard_sees_each_kind_of_point(sheet, probe, monkeypatch):
    """Mutant: the point list lacks that kind of connection, so the run goes through (or fails elsewhere)."""
    real = sheet_orient.se._connectors
    real_points = sheet_orient._points
    if probe.startswith("wire"):  # mutants of the wire-end entries: none kept / only each wire's first end kept
        def ends_dropped(sh):
            owned = [p for p in real_points(sh) if p[1]]
            keep = [p for p in real_points(sh) if not p[1]]
            return keep + (owned[::2] if probe == "wire_last_end" else [])

        monkeypatch.setattr(sheet_orient, "_points", ends_dropped)
        assert _crossing_case(sheet, probe).get("code") != "drag_crosses_connection"
        monkeypatch.undo()
        monkeypatch.setattr(sheet_orient, "_points", lambda sh: [p for p in real_points(sh) if not p[1]])
        assert _crossing_case(sheet, probe).get("code") != "drag_crosses_connection"
        return

    def without(sh):
        pts = real(sh)
        junctions = {sheet_editing._at(n)[:2] for n in sh.junctions}
        pins = {(p["x"], p["y"]) for s in sh.symbols for p in sheet_editing.pin_positions(sh.root, s)}
        labels = {sheet_editing._at(n)[:2] for n in sh.labels}
        markers = {sheet_editing._at(n)[:2] for n in sh.no_connects}
        drop = {"pin": pins, "label": labels, "marker": markers, "junction": junctions}[probe]
        return [p for p in pts if not any(sheet_editing._same(p, d) for d in drop)]

    monkeypatch.setattr(sheet_orient.se, "_connectors", without)
    result = _crossing_case(sheet, probe)
    assert result.get("code") != "drag_crosses_connection", (probe, result)


# --------------------------------------------------------------------------- what the text says about a pin landing on something


def _land(sheet: Path, kind: str) -> None:
    """R3's pin 1 lands on (148.59, 50.8) after a 90 degree turn (no wire is on R3)."""
    tools = _tools()
    x, y = 148.59, 50.8
    if kind == "wire_end":
        _add_nodes(sheet, [_wire_node(x, y, x, y - 10.16, "11111111-2222-3333-4444-000000000001")])
        assert tools["add_labels"](str(sheet), [{"kind": "label", "x": x, "y": y - 10.16, "text": "PW", "snap": False}])["status"] == "ok"
    elif kind == "mid_wire":
        _add_nodes(sheet, [_wire_node(x, y - 10.16, x, y + 10.16, "11111111-2222-3333-4444-000000000002")])
        assert tools["add_labels"](str(sheet), [{"kind": "label", "x": x, "y": y - 10.16, "text": "PW", "snap": False}])["status"] == "ok"
    else:
        assert tools["add_labels"](str(sheet), [{"kind": "label", "x": x, "y": y, "text": "PL", "snap": False}])["status"] == "ok"


def _wire_node(x1, y1, x2, y2, uuid):
    S = write_layer.SExp
    return S.list("wire", S.list("pts", S.list("xy", write_layer.number(x1), write_layer.number(y1)), S.list("xy", write_layer.number(x2), write_layer.number(y2))),
                  S.list("stroke", S.list("width", write_layer.number(0)), S.list("type", write_layer.symbol("default"))), S.list("uuid", write_layer.string(uuid)))


@pytest.mark.parametrize("kind", ["wire_end", "label"])
def test_a_pin_landing_on_a_wire_end_or_a_label_is_refused_as_connectivity_changed(sheet, kind):
    _land(sheet, kind)
    before = _bytes(sheet)
    result = _rotate(sheet, [{"references": ["R3"], "by": 90}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert _bytes(sheet) == before


def test_a_pin_landing_in_the_middle_of_a_foreign_wire_is_written_and_changes_no_net(sheet):
    _land(sheet, "mid_wire")
    result = _rotate(sheet, [{"references": ["R3"], "by": 90}])
    assert result["ok"] is True and result["written"] is True and result["connectivity"]["changed"] is False, result
    assert _pins(sheet, "R3")["1"] == (148.59, 50.8)
