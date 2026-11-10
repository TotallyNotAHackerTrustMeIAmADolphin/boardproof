"""align_items / distribute_items (GitHub issue 90), with real kicad-cli.

The downstream project tidied a sheet of ten identical branches on a pitch of 17.78 mm by computing
every coordinate by hand. These tests build a small sheet through the tools, align and distribute its
symbols and labels in place, and read the result back from the raw file and from kicad-cli's netlist.
Bounding boxes are cross-checked against the raw library geometry of the sheet's own file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import SExp
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_align, sheet_authoring, sheet_editing, write_layer
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


@pytest.fixture
def sheet(tmp_path, tools) -> Path:
    """R1 (50.8, 50.8), R2 (71.12, 60.96), R3 (101.6, 55.88), C1 (127, 50.8), all upright; every pin of
    the resistors carries a label on it (A1/B1, A2/B2, A3/B3), so a moved symbol must drag its labels."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"},
        {"lib_id": "Device:R", "x": 71.12, "y": 60.96, "ref": "R2", "value": "1k"},
        {"lib_id": "Device:R", "x": 101.6, "y": 55.88, "ref": "R3", "value": "2k"},
        {"lib_id": "Device:C", "x": 127.0, "y": 50.8, "ref": "C1", "value": "1u"},
    ])["status"] == "ok"
    labels = []
    for n, (x, y) in enumerate([(50.8, 50.8), (71.12, 60.96), (101.6, 55.88)], start=1):
        labels += [{"kind": "label", "x": x, "y": y - 3.81, "text": f"A{n}", "snap": False},
                   {"kind": "label", "x": x, "y": y + 3.81, "text": f"B{n}", "snap": False}]
    assert tools["add_labels"](str(sch), labels)["status"] == "ok"
    return sch


def _root(sch: Path):
    return write_layer.load(sch)


def _nodes(sch: Path, tag: str):
    return [c for c in _root(sch).children if not c.is_atom and c.name == tag]


def _symbol(sch: Path, ref: str):
    return next(s for s in _nodes(sch, "symbol") if sheet_editing._ref(s) == ref)


def _at(sch: Path, ref: str) -> tuple[float, float]:
    return sheet_editing._at(_symbol(sch, ref))[:2]


def _label_at(sch: Path, text: str) -> tuple[float, float]:
    return next(sheet_editing._at(n)[:2] for n in _nodes(sch, "label") if n.get_string(0) == text)


def _nets(sch: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    return sheet_editing.read_nets(tree, sch, None)


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _align(sch, groups, **kw):
    args = {"box": "pins", "move_wires": True, "snap": True, "grid": 1.27, "allow_connectivity_change": False, "dry_run": False}
    return sheet_align.align(str(sch), groups, None, **{**args, **kw})


def _distribute(sch, groups, **kw):
    args = {"box": "pins", "move_wires": True, "snap": True, "grid": 1.27, "allow_connectivity_change": False, "dry_run": False}
    return sheet_align.distribute(str(sch), groups, None, **{**args, **kw})


# --------------------------------------------------------------------------- bounding boxes


def test_the_symbol_box_is_the_raw_library_geometry_of_the_sheet_file(sheet):
    """Device:R in this very file: rectangle (-1.016, -2.54)..(1.016, 2.54), pins at (0, +-3.81) of length 1.27.
    Read from the raw lib_symbols node, not from a kicad_tools property."""
    root = _root(sheet)
    lib = next(e for t in root.children if not t.is_atom and t.name == "lib_symbols" for e in t.children
               if not e.is_atom and e.name == "symbol" and e.get_string(0) == "Device:R")
    rect = next(r for sub in lib.children if not sub.is_atom and sub.name == "symbol"
                for r in sub.children if not r.is_atom and r.name == "rectangle")
    start, end = rect.find_children("start")[0], rect.find_children("end")[0]
    assert sorted([start.get_float(0), end.get_float(0)]) == [-1.016, 1.016]
    assert sorted([start.get_float(1), end.get_float(1)]) == [-2.54, 2.54]
    r1 = _symbol(sheet, "R1")
    assert sheet_align.symbol_box(root, r1, "pins") == pytest.approx((49.784, 46.99, 51.816, 54.61))


def test_the_box_follows_rotation_and_agrees_with_the_pin_positions(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "rot"), "rot")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k", "rotation": 90}])["status"] == "ok"
    root = _root(sch)
    node = _symbol(sch, "R1")
    pins = sheet_editing.pin_positions(root, node)
    box = sheet_align.symbol_box(root, node, "pins")
    assert box == pytest.approx((min(p["x"] for p in pins), 50.8 - 1.016, max(p["x"] for p in pins), 50.8 + 1.016))
    assert box[2] - box[0] == pytest.approx(7.62)


# --------------------------------------------------------------------------- align


def test_align_top_to_the_first_moves_symbols_labels_and_texts_and_keeps_every_net(sheet):
    nets_before = _nets(sheet)
    ref_text_before = sheet_editing._at(sheet_editing._property(_symbol(sheet, "R3"), "Reference"))
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "R2", "R3"], "edge": "top", "reference": "first"}])
    assert result["ok"] is True and result["written"] is True, result
    assert _at(sheet, "R2") == (71.12, 50.8) and _at(sheet, "R3") == (101.6, 50.8) and _at(sheet, "R1") == (50.8, 50.8)
    assert _label_at(sheet, "A2") == (71.12, 46.99) and _label_at(sheet, "B3") == (101.6, 54.61)
    ref_text_after = sheet_editing._at(sheet_editing._property(_symbol(sheet, "R3"), "Reference"))
    assert ref_text_after[1] - ref_text_before[1] == pytest.approx(-5.08) and ref_text_after[0] == ref_text_before[0]
    assert _nets(sheet) == nets_before
    group = result["groups"][0]
    assert group["aligned"] is True and group["target"] == pytest.approx(46.99) and group["max_residual"] == 0
    assert result["connectivity"]["changed"] is False
    assert result["grid"] == 1.27


def test_align_last_and_extremes_and_numbers(sheet):
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "R3"], "edge": "left", "reference": "last"}])
    assert result["ok"] is True and _at(sheet, "R1")[0] == 101.6, result
    result = _align(sheet, [{"kind": "symbol", "references": ["R2", "R3"], "edge": "hcenter", "reference": 38.1}])
    assert result["ok"] is True and _at(sheet, "R2")[0] == 38.1 and _at(sheet, "R3")[0] == 38.1, result
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "R2"], "edge": "bottom", "reference": "max"}])
    assert result["ok"] is True and _at(sheet, "R1")[1] == 60.96, result


def test_the_grid_snap_is_stated_and_the_residual_reported(sheet):
    """C1's plates are wider than R1's body: their left edges cannot both be exact when the origin stays on the 1.27 grid."""
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "C1"], "edge": "left", "reference": "first"}])
    assert result["ok"] is True, result
    assert _at(sheet, "C1")[0] == pytest.approx(52.07)
    group = result["groups"][0]
    assert group["aligned"] is False and group["max_residual"] == pytest.approx(0.254)
    c1 = next(i for i in group["items"] if i["reference"] == "C1")
    assert c1["residual"] == pytest.approx(0.254) and c1["snapped"] is True
    assert "snapped" in result["note"] or "grid" in result["note"]
    # with snap off the edge is exact
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "C1"], "edge": "left", "reference": "first"}], snap=False)
    assert result["groups"][0]["aligned"] is True and _at(sheet, "C1")[0] == pytest.approx(51.816)


def test_align_labels_by_their_anchor(sheet):
    result = _align(sheet, [{"kind": "label", "texts": ["A1", "A3"], "edge": "left", "reference": "first"}], allow_connectivity_change=True)
    assert result["ok"] is True, result
    assert _label_at(sheet, "A3")[0] == 50.8
    # a label moved off its pin changes the net: the same call without the allowance is refused
    before = _bytes(sheet)
    refused = _align(sheet, [{"kind": "label", "texts": ["B1", "B3"], "edge": "left", "reference": "first"}])
    assert refused["ok"] is False and refused["code"] == "connectivity_changed", refused
    assert _bytes(sheet) == before


def test_a_dry_run_reports_and_writes_nothing(sheet):
    before = _bytes(sheet)
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and len(result["would_move"]) == 1, result
    assert _bytes(sheet) == before


def test_moving_without_the_attached_items_is_refused_by_the_netlist_comparison(sheet):
    before = _bytes(sheet)
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}], move_wires=False)
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert _bytes(sheet) == before


def test_nothing_to_move_writes_nothing(sheet):
    _align(sheet, [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}])
    before = _bytes(sheet)
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}])
    assert result["ok"] is True and result["written"] is False and result["count"] == 0, result
    assert _bytes(sheet) == before


@pytest.mark.parametrize("groups,code", [
    ([], "invalid_spec"),
    ([{"kind": "symbol", "references": ["R1"], "edge": "top"}], "invalid_spec"),                       # one item
    ([{"kind": "symbol", "references": ["R1", "R2"], "edge": "middle"}], "invalid_spec"),
    ([{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "second"}], "invalid_spec"),
    ([{"kind": "wire", "references": ["R1", "R2"], "edge": "top"}], "invalid_spec"),
    ([{"kind": "symbol", "references": ["R1", "R9"], "edge": "top"}], "not_found"),
    ([{"kind": "symbol", "references": ["R*", "C1"], "edge": "top"}], "not_found"),                    # references are exact, not patterns
    ([{"kind": "symbol", "references": ["R1", "R1"], "edge": "top"}], "invalid_spec"),                  # twice
    ([{"kind": "symbol", "references": ["R1", "R2"], "uuids": ["x"], "edge": "top"}], "invalid_spec"),
    ([{"kind": "symbol", "references": ["R1", "R2"], "edge": "top"}, {"kind": "symbol", "references": ["R2", "R3"], "edge": "left"}], "invalid_spec"),
    ([{"kind": "label", "texts": ["A1", "A9"], "edge": "left"}], "not_found"),
])
def test_bad_input_refuses_and_writes_nothing(sheet, groups, code):
    before = _bytes(sheet)
    result = _align(sheet, groups)
    assert result["ok"] is False and result["code"] == code, result
    assert _bytes(sheet) == before


def test_bad_options_refuse(sheet):
    g = [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top"}]
    for box in ("outline", "body"):
        refused = _align(sheet, g, box=box)
        assert refused["code"] == "invalid_spec" and "'pins'" in refused["reason"], refused
    assert _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}], box="body")["code"] == "invalid_spec"
    assert _align(sheet, g, grid=0)["code"] == "invalid_spec"
    assert _align(sheet, g, snap="yes")["code"] == "invalid_spec"


# --------------------------------------------------------------------------- distribute


def test_distribute_equal_gaps_moves_the_middle_item_and_its_attachments(sheet):
    result = _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}])
    assert result["ok"] is True, result
    assert _at(sheet, "R1")[0] == 50.8 and _at(sheet, "R3")[0] == 101.6 and _at(sheet, "R2")[0] == 76.2
    assert _label_at(sheet, "A2") == (76.2, 57.15) and _at(sheet, "R2")[1] == 60.96
    group = result["groups"][0]
    assert group["spacing"] == "gaps" and group["gaps_after"] == pytest.approx([23.368, 23.368])
    assert result["connectivity"]["changed"] is False


def test_distribute_on_a_pitch_keeps_the_first_and_orders_by_position(sheet):
    result = _distribute(sheet, [{"kind": "symbol", "references": ["R3", "R1", "R2"], "axis": "x", "spacing": 17.78}])
    assert result["ok"] is True, result
    assert [_at(sheet, r)[0] for r in ("R1", "R2", "R3")] == pytest.approx([50.8, 68.58, 86.36])
    group = result["groups"][0]
    assert group["order"] == ["R1", "R2", "R3"] and group["max_residual"] == 0


def test_distribute_centres_and_y_axis(sheet):
    result = _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "y", "spacing": 7.62}])
    assert result["ok"] is True, result
    assert result["groups"][0]["order"] == ["R1", "R3", "R2"]
    assert [_at(sheet, r)[1] for r in ("R1", "R3", "R2")] == pytest.approx([50.8, 58.42, 66.04])
    result = _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "y", "spacing": "centres"}])
    assert result["ok"] is True and result["groups"][0]["spacing"] == "centres", result
    assert _at(sheet, "R3")[1] == pytest.approx(58.42)   # already the midpoint of 50.8 and 66.04


def test_distribute_needs_three_items_and_a_known_axis_and_spacing(sheet):
    two = [{"kind": "symbol", "references": ["R1", "R2"], "axis": "x"}]
    assert _distribute(sheet, two)["code"] == "invalid_spec"
    three = {"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}
    assert _distribute(sheet, [{**three, "axis": "z"}])["code"] == "invalid_spec"
    assert _distribute(sheet, [{**three, "spacing": "tight"}])["code"] == "invalid_spec"
    assert _distribute(sheet, [{**three, "spacing": -1}])["code"] == "invalid_spec"
    assert _distribute(sheet, [{**three, "spacing": True}])["code"] == "invalid_spec"


def test_distribute_labels(sheet):
    result = _distribute(sheet, [{"kind": "label", "texts": ["A1", "A2", "A3"], "axis": "x"}], allow_connectivity_change=True)
    assert result["ok"] is True, result
    assert _label_at(sheet, "A2")[0] == pytest.approx(76.2)


# --------------------------------------------------------------------------- the guards


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


def test_align_items_corrupted_write_is_refused(sheet, monkeypatch):
    _corrupted_write_is_refused(sheet, monkeypatch, "align_items",
                                ([{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}],))


def test_distribute_items_corrupted_write_is_refused(sheet, monkeypatch):
    _corrupted_write_is_refused(sheet, monkeypatch, "distribute_items",
                                ([{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}],))


def test_the_connectivity_check_fires_when_the_comparison_is_blind(sheet, monkeypatch):
    """Mutation check of the guard: with the comparison disabled the unsafe align would go through."""
    groups = [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}]
    assert _align(sheet, groups, move_wires=False)["code"] == "connectivity_changed"
    monkeypatch.setattr(sheet_editing, "compare_nets", lambda *a, **k: [])
    assert _align(sheet, groups, move_wires=False)["ok"] is True
    monkeypatch.undo()


def test_the_staged_check_fires_when_an_unselected_node_moves(sheet, monkeypatch):
    """Mutation check: if the align also shifts a symbol it did not select, the staged check refuses."""
    original = sheet_align._plan_group_moves

    def greedy(*a, **k):
        planned, extra = original(*a, **k)
        root = a[0].root
        stray = next(n for n in sheet_editing._kids(root, "symbol") if sheet_editing._ref(n) == "C1")
        sheet_editing._set_at(stray, 200.0, 200.0)
        return planned, extra

    monkeypatch.setattr(sheet_align, "_plan_group_moves", greedy)
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()


def test_a_lock_file_refuses_both_tools_before_anything_is_computed(sheet, tools):
    lock = sheet.with_name(f"~{sheet.name}.lck")
    lock.write_bytes(b'{"hostname":"other","username":"x"}')
    before = _bytes(sheet)
    for name, groups in {"align_items": [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top"}],
                         "distribute_items": [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}]}.items():
        result = tools[name](str(sheet), groups)
        assert result["code"] == "file_open_in_kicad" and result["ok"] is False, (name, result)
    assert _bytes(sheet) == {**before, lock.name: lock.read_bytes()}


def test_the_tools_work_through_the_toolset(sheet, tools):
    result = tools["align_items"](str(sheet), [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}])
    assert result["ok"] is True and _at(sheet, "R2")[1] == 50.8, result
    result = tools["distribute_items"](str(sheet), [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}])
    assert result["ok"] is True, result


def test_a_hidden_pin_is_not_part_of_the_box(tmp_path, tools):
    """KiCad's body box skips hidden pins (eeschema/lib_symbol.cpp). The embedded power:GND pin has no hide flag in this
    file (measured: `(pin power_in line (at 0 0 270) (length 0) ...)`), so it counts; the library symbol is changed
    in memory only (nothing is saved) to a hidden pin 25.4 mm away, which must leave the box alone, in both spellings."""
    made = project_toolset._project_create(str(tmp_path / "pwr"), "pwr")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "power:GND", "x": 50.8, "y": 50.8, "ref": "#PWR01"}])["status"] == "ok"
    root = _root(sch)
    node = _symbol(sch, "#PWR01")
    base = sheet_align.symbol_box(root, node, "pins")
    lib = sheet_editing._lib_symbol(root, node)
    pins = [p for sub in sheet_editing._kids(lib, "symbol") for p in sheet_editing._kids(sub, "pin")]
    assert pins and not any(sheet_align._hidden(p) for p in pins)
    for pin in pins:
        write_layer.set_number(pin.find_children("at")[0], 1, 25.4)
    visible = sheet_align.symbol_box(root, node, "pins")
    assert visible[1] < base[1] or visible[3] > base[3]                     # a visible pin far away widens the box
    for spelling in (lambda: SExp.list("hide", write_layer.symbol("yes")), lambda: write_layer.symbol("hide")):
        for pin in pins:
            for child in list(pin.children):
                if (not child.is_atom and child.name == "hide") or (child.is_atom and str(child.value) == "hide"):
                    pin.children.remove(child)
            pin.children.append(spelling())
        assert all(sheet_align._hidden(p) for p in pins)
        assert sheet_align.symbol_box(root, node, "pins") == pytest.approx(base)


# --------------------------------------------------------------------------- review of issue 90: boxes of real library parts


def _parts(tmp_path, tools, specs) -> Path:
    made = project_toolset._project_create(str(tmp_path / "parts"), "parts")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"
    return sch


def _mirrored(node, word):
    """Put (mirror word) on a loaded placed symbol, in memory only."""
    at = node.find_children("at")[0]
    node.children.insert(node.children.index(at) + 1, SExp.list("mirror", write_layer.symbol(word)))


def _q_expected(root, node):
    """Expected box of Device:Q_NPN from independent pieces: the pin connection points (pin_positions, checked against the
    netlist) and the raw circle of the body (centre and radius read from the library node, placed with ``place``)."""
    lib = sheet_editing._lib_symbol(root, node)
    circle = next(c for sub in sheet_editing._kids(lib, "symbol") for c in sheet_editing._kids(sub, "circle"))
    centre = circle.find_children("center")[0]
    radius = circle.find_children("radius")[0].get_float(0)
    sx, sy, rot = sheet_editing._at(node)
    dx, dy = sheet_editing.place(centre.get_float(0), centre.get_float(1), rot, sheet_editing._mirror(node))
    pins = sheet_editing.pin_positions(root, node)
    xs = [p["x"] for p in pins] + [sx + dx - radius, sx + dx + radius]
    ys = [p["y"] for p in pins] + [sy + dy - radius, sy + dy + radius]
    return (min(xs), min(ys), max(xs), max(ys))


@pytest.mark.parametrize("rotation,mirror", [(90, "y"), (90, "x"), (0, "x"), (180, "y"), (270, "x"), (0, "")])
def test_the_box_of_a_rotated_and_mirrored_circle_part_follows_pins_and_the_raw_circle(tmp_path, tools, rotation, mirror):
    sch = _parts(tmp_path, tools, [{"lib_id": "Device:Q_NPN", "x": 50.8, "y": 50.8, "ref": "Q1", "value": "q", "rotation": rotation}])
    root = _root(sch)
    node = _symbol(sch, "Q1")
    if mirror:
        _mirrored(node, mirror)
    assert sheet_editing._mirror(node) == mirror
    assert sheet_align.symbol_box(root, node) == pytest.approx(_q_expected(root, node), abs=1e-4)


def test_the_box_of_a_mirrored_and_rotated_q_npn_matches_the_svg_ground_truth(tmp_path, tools):
    """Measured on kicad-cli's SVG export: rotation 90 and mirror y gives (-5.08, -4.089)..(5.08, 5.08) around the origin;
    mirror x flips the vertical extent, so a box that ignores the mirror fails the second assertion."""
    sch = _parts(tmp_path, tools, [{"lib_id": "Device:Q_NPN", "x": 50.8, "y": 50.8, "ref": "Q1", "value": "q", "rotation": 90},
                                   {"lib_id": "Device:Q_NPN", "x": 101.6, "y": 50.8, "ref": "Q2", "value": "q", "rotation": 90}])
    root = _root(sch)
    n1, n2 = _symbol(sch, "Q1"), _symbol(sch, "Q2")
    _mirrored(n1, "y")
    _mirrored(n2, "x")
    assert sheet_align.symbol_box(root, n1) == pytest.approx((45.72, 50.8 - 4.089, 55.88, 55.88), abs=1e-3)
    assert sheet_align.symbol_box(root, n2) == pytest.approx((96.52, 50.8 - 5.08, 106.68, 50.8 + 4.089), abs=1e-3)


def test_the_box_of_unit_2_of_a_multi_unit_part_uses_that_unit_only(tmp_path, tools):
    """LM358 unit 2 rotated 90 at (101.6, 101.6): box (96.52, 93.98)..(106.68, 109.22) (SVG ground truth)."""
    sch = _parts(tmp_path, tools, [
        {"lib_id": "Amplifier_Operational:LM358", "x": 101.6, "y": 101.6, "ref": "U1", "value": "x", "unit": 2, "rotation": 90},
        {"lib_id": "Amplifier_Operational:LM358", "x": 152.4, "y": 101.6, "ref": "U1", "value": "x", "unit": 3}])
    root = _root(sch)
    by_x = {round(sheet_editing._at(n)[0], 2): n for n in _nodes(sch, "symbol")}
    two, power = (sheet_align.symbol_box(root, by_x[x]) for x in (101.6, 152.4))
    assert two == pytest.approx((96.52, 93.98, 106.68, 109.22), abs=1e-3)
    assert (power[2] - power[0], power[3] - power[1]) != pytest.approx((two[2] - two[0], two[3] - two[1]), abs=1e-3)


def test_the_box_of_an_arc_part_includes_the_arc_bulge(tmp_path, tools):
    """Device:L is arcs bulging to x = +0.6323 of the origin: the axis extreme of the circle through start, mid and end,
    which none of the three points reaches."""
    sch = _parts(tmp_path, tools, [{"lib_id": "Device:L", "x": 152.4, "y": 50.8, "ref": "L1", "value": "1u"}])
    box = sheet_align.symbol_box(_root(sch), _symbol(sch, "L1"))
    assert box[2] - 152.4 == pytest.approx(0.6323, abs=1e-3)
    assert box[0] == pytest.approx(152.4, abs=1e-3)


def test_the_body_style_selects_the_drawing_and_the_pins(tmp_path, tools):
    """The placed symbol's (body_style 2) selects the *_2 sub-symbols. 74LS00 has the same extent in both styles, so the
    style-2 pin of the library node (in memory only) is moved 5.08 mm out: only a box that filters by body style sees it."""
    sch = _parts(tmp_path, tools, [{"lib_id": "74xx:74LS00", "x": 50.8, "y": 50.8, "ref": "U1", "value": "x", "unit": 1}])
    root = _root(sch)
    node = _symbol(sch, "U1")
    lib = sheet_editing._lib_symbol(root, node)
    style2 = next(sub for sub in sheet_editing._kids(lib, "symbol") if sub.get_string(0) == "74LS00_1_2")
    for pin in sheet_editing._kids(style2, "pin"):
        at = pin.find_children("at")[0]
        if at.get_float(0) < 0:
            write_layer.set_number(at, 0, -12.7)
    first = sheet_align.symbol_box(root, node)
    assert first[0] == pytest.approx(50.8 - 7.62, abs=1e-3)                    # style 1 (default): the style-2 pin is not seen
    at = node.find_children("at")[0]
    node.children.insert(node.children.index(at) + 1, SExp.list("body_style", 2))
    assert sheet_align.symbol_box(root, node)[0] == pytest.approx(50.8 - 12.7, abs=1e-3)   # style 2: it is


# --------------------------------------------------------------------------- review of issue 90: every edge and reference mode


def test_align_right_with_an_unequal_pair_and_reference_min(sheet):
    """C1's plates are 2.032 mm either side of its origin, R1's body 1.016: right edges 129.032 and 51.816."""
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "C1"], "edge": "right", "reference": "first"}])
    assert result["ok"] is True, result
    group = result["groups"][0]
    assert group["target"] == pytest.approx(51.816)
    assert _at(sheet, "C1")[0] == pytest.approx(49.53)                  # 51.816 - 2.032 = 49.784, rounded to the 1.27 grid
    assert group["max_residual"] == pytest.approx(0.254) and group["aligned"] is False
    c1 = next(i for i in group["items"] if i["reference"] == "C1")
    assert c1["edge_before"] == pytest.approx(129.032) and c1["edge_after"] == pytest.approx(51.562)
    result = _align(sheet, [{"kind": "symbol", "references": ["R2", "R3"], "edge": "right", "reference": "min"}], dry_run=True)
    assert result["groups"][0]["target"] == pytest.approx(72.136)   # R2's right edge is the smaller


def test_align_vcenter_to_the_minimum(sheet):
    result = _align(sheet, [{"kind": "symbol", "references": ["R2", "R3"], "edge": "vcenter", "reference": "min"}])
    assert result["ok"] is True, result
    assert _at(sheet, "R2")[1] == pytest.approx(55.88) and _at(sheet, "R3")[1] == pytest.approx(55.88)
    assert result["groups"][0]["target"] == pytest.approx(55.88)


# --------------------------------------------------------------------------- review of issue 90: distribute reports


def test_distribute_reports_overlapping_neighbours(sheet):
    """A pitch of 1.27 mm is narrower than a resistor body (2.032 mm): R1, R2, R3 overlap after the move."""
    result = _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x", "spacing": 1.27}],
                         allow_connectivity_change=True, dry_run=True)
    assert result["ok"] is True, result
    group = result["groups"][0]
    assert group["overlapping_after"] == [["R1", "R2"], ["R2", "R3"]]
    assert all(g < 0 for g in group["gaps_after"])


def test_distribute_says_when_the_grid_made_the_spacing_uneven(sheet):
    """R1, R2, C1 on x: the equal gap puts R2's origin at 88.392, which the 1.27 grid rounds to 88.9."""
    result = _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "C1"], "axis": "x"}], dry_run=True)
    assert result["ok"] is True, result
    group = result["groups"][0]
    assert group["even"] is False and group["max_residual"] > 0.1 and group["overlapping_after"] == []
    assert group["order"] == ["R1", "R2", "C1"]
    exact = _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "C1"], "axis": "x"}], dry_run=True, snap=False)
    assert exact["groups"][0]["even"] is True and exact["groups"][0]["max_residual"] == 0


# --------------------------------------------------------------------------- review of issue 90: numbers (F3)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), 1e9, -1e9])
def test_non_finite_and_absurd_numbers_are_invalid_spec(sheet, bad):
    before = _bytes(sheet)
    for snap in (True, False):
        r = _align(sheet, [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": bad}], snap=snap)
        assert r["ok"] is False and r["code"] == "invalid_spec", (bad, snap, r)
        r = _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x", "spacing": bad}], snap=snap)
        assert r["ok"] is False and r["code"] == "invalid_spec", (bad, snap, r)
    g = [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top"}]
    assert _align(sheet, g, grid=bad)["code"] == "invalid_spec"
    assert _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}], grid=bad)["code"] == "invalid_spec"
    assert sheet_editing.move("symbol", str(sheet), [{"references": ["R1"], "offset": [1.27, 0]}], None, move_wires=True,
                              snap=True, grid=bad, allow_connectivity_change=False, dry_run=False)["code"] == "invalid_spec"
    assert sheet_editing.move("symbol", str(sheet), [{"references": ["R1"], "offset": [bad, 0]}], None, move_wires=True,
                              snap=True, grid=1.27, allow_connectivity_change=False, dry_run=False)["code"] == "invalid_spec"
    assert sheet_editing.move("symbol", str(sheet), [{"references": ["R1"], "to": [0, bad]}], None, move_wires=True,
                              snap=False, grid=1.27, allow_connectivity_change=False, dry_run=False)["code"] == "invalid_spec"
    assert _bytes(sheet) == before


def test_the_tools_return_invalid_spec_for_nan_instead_of_raising(sheet, tools):
    nan = float("nan")
    for result in (tools["align_items"](str(sheet), [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": nan}]),
                   tools["distribute_items"](str(sheet), [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}], grid=nan),
                   tools["move_symbols"](str(sheet), [{"references": ["R1"], "offset": [1.27, 0]}], grid=float("inf"))):
        assert result["ok"] is False and result["code"] == "invalid_spec", result


def test_an_arc_extreme_that_is_none_of_its_three_points_is_in_the_box(tmp_path, tools):
    """An arc of radius 5 around the library origin from -60 to 60 degrees whose mid point is at 10 degrees (library node, in memory
    only): the circle's x extreme 5.0 lies between the points, the mid point only reaches 4.924."""
    sch = _parts(tmp_path, tools, [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"}])
    root = _root(sch)
    node = _symbol(sch, "R1")
    sub = sheet_editing._kids(sheet_editing._lib_symbol(root, node), "symbol")[0]
    sub.children.append(SExp.list("arc", SExp.list("start", 2.5, -4.330127), SExp.list("mid", 4.924039, 0.868241),
                                  SExp.list("end", 2.5, 4.330127)))
    box = sheet_align.symbol_box(root, node)
    assert box[2] == pytest.approx(50.8 + 5.0, abs=1e-3)
    assert box[1] == pytest.approx(50.8 - 4.330127, abs=1e-3) and box[3] == pytest.approx(50.8 + 4.330127, abs=1e-3)


# --------------------------------------------------------------------------- delta review of issue 90: B1 one kind per call


def _kinds_mix(op: str):
    sym = {"kind": "symbol", "references": ["R1", "R2", "R3"] if op == "distribute" else ["R1", "R2"],
           **({"axis": "x"} if op == "distribute" else {"edge": "top"})}
    lab = {"kind": "label", "texts": ["A1", "A2", "A3"] if op == "distribute" else ["A1", "A2"],
           **({"axis": "x"} if op == "distribute" else {"edge": "left"})}
    return sym, lab


@pytest.mark.parametrize("op", ["align", "distribute"])
@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("allow", [False, True])
def test_a_symbol_group_and_a_label_group_in_one_call_are_refused(sheet, op, reverse, allow):
    sym, lab = _kinds_mix(op)
    groups = [lab, sym] if reverse else [sym, lab]
    before = _bytes(sheet)
    run = _align if op == "align" else _distribute
    result = run(sheet, groups, allow_connectivity_change=allow)
    assert result["ok"] is False and result["code"] == "invalid_spec" and "one kind" in result["reason"], result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- delta review of issue 90: B2 exactly one node per entry


def test_an_entry_that_picks_several_units_is_refused_and_uuids_work(tmp_path, tools):
    sch = _parts(tmp_path, tools, [
        {"lib_id": "Amplifier_Operational:LM358", "x": 50.8, "y": 50.8, "ref": "U1", "value": "x", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM358", "x": 101.6, "y": 76.2, "ref": "U1", "value": "x", "unit": 2},
        {"lib_id": "Device:R", "x": 152.4, "y": 101.6, "ref": "R1", "value": "1k"}])
    before = _bytes(sch)
    result = _align(sch, [{"kind": "symbol", "references": ["U1", "R1"], "edge": "top"}])
    assert result["code"] == "invalid_spec" and "'U1' picks 2 symbol nodes" in result["reason"], result
    result = _distribute(sch, [{"kind": "symbol", "references": ["U1", "R1", "R1"], "axis": "x"}])
    assert result["code"] == "invalid_spec"
    assert _bytes(sch) == before
    uuids = [sheet_editing._uuid(n) for n in _nodes(sch, "symbol")]
    result = _align(sch, [{"kind": "symbol", "uuids": uuids[:2], "edge": "top", "reference": "first"}], dry_run=True)
    assert result["ok"] is True, result


def test_a_label_text_that_picks_several_labels_is_refused(sheet, tools):
    assert tools["add_labels"](str(sheet), [{"kind": "label", "x": 10.16, "y": 10.16, "text": "DUP", "snap": False},
                                            {"kind": "label", "x": 20.32, "y": 20.32, "text": "DUP", "snap": False}])["status"] == "ok"
    before = _bytes(sheet)
    result = _align(sheet, [{"kind": "label", "texts": ["DUP", "A1"], "edge": "left"}], allow_connectivity_change=True)
    assert result["ok"] is False and result["code"] == "invalid_spec" and "'DUP' picks 2 label nodes" in result["reason"], result
    result = _distribute(sheet, [{"kind": "label", "texts": ["A1", "DUP", "A3"], "axis": "x"}], allow_connectivity_change=True)
    assert result["ok"] is False and result["code"] == "invalid_spec" and "picks 2" in result["reason"], result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- delta review of issue 90: B3 labels with a wire on them


@pytest.fixture
def wired_labels(sheet, tools) -> Path:
    """The pins R1.1 (50.8, 46.99), R2.1 (71.12, 57.15), R3.1 (101.6, 52.07) each go up over a wire to a label on its top end."""
    for n, (x, y, top) in enumerate([(50.8, 46.99, 35.56), (71.12, 57.15, 38.1), (101.6, 52.07, 40.64)], start=1):
        assert tools["add_wires"](str(sheet), [{"p1": [x, y], "p2": [x, top], "route": "direct", "snap": False}])["status"] == "ok"
        assert tools["add_labels"](str(sheet), [{"kind": "label", "x": x, "y": top, "text": f"N{n}", "snap": False}])["status"] == "ok"
    return sheet


def _wire_with_end(sch: Path, point):
    return next(w for w in _nodes(sch, "wire") if any(sheet_editing._same(e, point) for e in sheet_editing._wire_ends(w)))


def test_aligned_labels_drag_the_wire_ends_on_them_and_keep_the_nets(wired_labels):
    nets_before = _nets(wired_labels)
    result = _align(wired_labels, [{"kind": "label", "texts": ["N1", "N2", "N3"], "edge": "top", "reference": "first"}])
    assert result["ok"] is True and result["written"] is True and result["kind"] == "label", result
    assert result["connectivity"]["changed"] is False
    assert [_label_at(wired_labels, f"N{n}")[1] for n in (1, 2, 3)] == [35.56] * 3
    assert len(result["followed"]["wires"]) == 2 and result["slanted"] == []
    for x in (71.12, 101.6):
        assert any(sheet_editing._same(e, (x, 35.56)) for e in sheet_editing._wire_ends(_wire_with_end(wired_labels, (x, 35.56))))
    assert _nets(wired_labels) == nets_before


def test_distributed_labels_drag_the_wire_ends_on_them_and_keep_the_nets(wired_labels):
    nets_before = _nets(wired_labels)
    result = _distribute(wired_labels, [{"kind": "label", "texts": ["N1", "N2", "N3"], "axis": "x"}])
    assert result["ok"] is True and result["written"] is True, result
    assert _label_at(wired_labels, "N2") == (76.2, 38.1)
    assert len(result["followed"]["wires"]) == 1 and len(result["slanted"]) == 1       # R2's wire now runs from its pin to the label
    assert any(sheet_editing._same(e, (76.2, 38.1)) for e in sheet_editing._wire_ends(_wire_with_end(wired_labels, (76.2, 38.1))))
    assert result["connectivity"]["changed"] is False and _nets(wired_labels) == nets_before


# --------------------------------------------------------------------------- delta review of issue 90: B4 the options through the tools

_ALIGN_TOP = [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}]
_DIST_X = [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}]


@pytest.mark.parametrize("tool,groups", [("align_items", _ALIGN_TOP), ("distribute_items", _DIST_X)])
def test_the_tools_forward_dry_run(sheet, tools, tool, groups):
    before = _bytes(sheet)
    result = tools[tool](str(sheet), groups, dry_run=True)
    assert result["ok"] is True and result["written"] is False and result["would_move"], result
    assert _bytes(sheet) == before


def test_align_items_forwards_snap_grid_box_and_the_connectivity_options(sheet, tools):
    exact = tools["align_items"](str(sheet), [{"kind": "symbol", "references": ["R1", "C1"], "edge": "left"}], snap=False, dry_run=True)
    assert exact["ok"] is True and exact["grid"] is None and exact["would_move"][0]["to"] == [51.816, 50.8], exact
    near = tools["align_items"](str(sheet), [{"kind": "symbol", "references": ["R2", "R3"], "edge": "hcenter", "reference": 39.0}],
                                grid=2.54, dry_run=True)
    assert near["grid"] == 2.54 and near["would_move"][0]["to"] == [38.1, 60.96], near
    default = tools["align_items"](str(sheet), [{"kind": "symbol", "references": ["R2", "R3"], "edge": "hcenter", "reference": 39.0}], dry_run=True)
    assert default["would_move"][0]["to"] == [39.37, 60.96], default
    assert tools["align_items"](str(sheet), _ALIGN_TOP, box="body")["code"] == "invalid_spec"
    before = _bytes(sheet)
    refused = tools["align_items"](str(sheet), _ALIGN_TOP, move_wires=False)
    assert refused["ok"] is False and refused["code"] == "connectivity_changed", refused
    assert _bytes(sheet) == before
    allowed = tools["align_items"](str(sheet), _ALIGN_TOP, move_wires=False, allow_connectivity_change=True)
    assert allowed["ok"] is True and allowed["written"] is True and allowed["move_wires"] is False, allowed
    assert _label_at(sheet, "A2") == (71.12, 57.15)         # the labels stayed where the pins were


def test_distribute_items_forwards_snap_grid_box_and_the_connectivity_options(sheet, tools):
    exact = tools["distribute_items"](str(sheet), [{"kind": "symbol", "references": ["R1", "R2", "C1"], "axis": "x"}], snap=False, dry_run=True)
    assert exact["ok"] is True and exact["grid"] is None and exact["would_move"][0]["to"] == [88.392, 60.96], exact
    pitch = [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x", "spacing": 17.0}]
    wide = tools["distribute_items"](str(sheet), pitch, grid=2.54, dry_run=True)
    assert wide["grid"] == 2.54 and wide["would_move"][0]["to"] == [68.58, 60.96], wide
    default = tools["distribute_items"](str(sheet), pitch, dry_run=True)
    assert default["would_move"][0]["to"] == [67.31, 60.96], default
    assert tools["distribute_items"](str(sheet), _DIST_X, box="body")["code"] == "invalid_spec"
    before = _bytes(sheet)
    refused = tools["distribute_items"](str(sheet), _DIST_X, move_wires=False)
    assert refused["ok"] is False and refused["code"] == "connectivity_changed", refused
    assert _bytes(sheet) == before
    allowed = tools["distribute_items"](str(sheet), _DIST_X, move_wires=False, allow_connectivity_change=True)
    assert allowed["ok"] is True and allowed["written"] is True and allowed["move_wires"] is False, allowed
    assert _label_at(sheet, "A2") == (71.12, 57.15)


@pytest.mark.parametrize("tool,groups", [("align_items", _ALIGN_TOP), ("distribute_items", _DIST_X)])
def test_the_tools_forward_root_sch(sheet, tools, tool, groups):
    """A root_sch that is no sheet refuses the call; dropped on the way, the project would be found from the folder instead."""
    before = _bytes(sheet)
    result = tools[tool](str(sheet), groups, root_sch=str(sheet.parent / "no_such_root.kicad_sch"))
    assert result["ok"] is False, result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- delta review of issue 90: F-a argument types

_JUNK = [[], ["top"], [1], {}, {"a": 1}, None, True, False, 0, 5, 1.5, "", "zzz"]


def _good_group(op, kind="symbol"):
    if kind == "label":
        return {"kind": "label", "texts": ["A1", "A2", "A3"], **({"axis": "x"} if op == "distribute" else {"edge": "left"})}
    return {"kind": "symbol", "references": ["R1", "R2", "R3"], **({"axis": "x"} if op == "distribute" else {"edge": "top"})}


@pytest.mark.parametrize("op", ["align", "distribute"])
@pytest.mark.parametrize("junk", _JUNK, ids=repr)
def test_no_argument_of_any_type_raises_instead_of_refusing(sheet, tools, op, junk):
    """Every group key and every option takes a junk value of every type: the call returns a refusal, it never raises. Values that
    are valid for a key (a coordinate for reference, a pitch for spacing) are skipped for it."""
    tool = tools[f"{op}_items"]
    before = _bytes(sheet)

    def refused(result, why):
        assert isinstance(result, dict) and result.get("ok") is False, (why, junk, result)
        assert result.get("code") in {"invalid_spec", "not_found"}, (why, junk, result)

    def group_with(**changes):
        group = _good_group(op)
        group.update(changes)
        return [group]

    keys = ["kind", "references", "uuids", "texts", "axis" if op == "distribute" else "edge"]
    if op == "align" and not (isinstance(junk, (int, float)) and not isinstance(junk, bool)):
        keys.append("reference")
    if op == "distribute" and not (isinstance(junk, (int, float)) and not isinstance(junk, bool) and junk > 0):
        keys.append("spacing")
    for key in keys:
        refused(tool(str(sheet), group_with(**{key: junk})), f"group.{key}")
    for entry in ([junk], [junk, "R2", "R3"]):
        if isinstance(junk, str) and junk:
            continue
        refused(tool(str(sheet), group_with(references=entry)), "references entry")
    refused(tool(str(sheet), junk), "groups")
    refused(tool(str(sheet), [junk]), "groups entry")
    refused(tool(str(sheet), [_good_group(op), junk]), "second group")
    for option in ("box", "move_wires", "snap", "allow_connectivity_change", "dry_run", "grid"):
        if option == "grid" and isinstance(junk, (int, float)) and not isinstance(junk, bool) and junk > 0:
            continue
        if option in ("move_wires", "snap", "allow_connectivity_change", "dry_run") and isinstance(junk, bool):
            continue
        refused(tool(str(sheet), [_good_group(op)], **{option: junk}), option)
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- delta review of issue 90: F-b pin lines and bezier curves


def test_the_end_of_a_pin_line_is_in_the_box(tmp_path, tools):
    """A pin whose line is 10 mm long runs from its connection point (0, 3.81) along 270 degrees to library y = -6.19: that end,
    reached by nothing else, is the bottom of the box (library node changed in memory only)."""
    sch = _parts(tmp_path, tools, [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"}])
    root = _root(sch)
    node = _symbol(sch, "R1")
    for sub in sheet_editing._kids(sheet_editing._lib_symbol(root, node), "symbol"):
        for pin in sheet_editing._kids(sub, "pin"):
            if pin.find_children("at")[0].get_float(1) > 0:
                write_layer.set_number(pin.find_children("length")[0], 0, 10.0)
    box = sheet_align.symbol_box(root, node)
    assert box[3] == pytest.approx(50.8 + 6.19, abs=1e-3) and box[1] == pytest.approx(50.8 - 3.81, abs=1e-3)


def test_a_bezier_counts_by_its_control_points(tmp_path, tools):
    """Documented: a bezier contributes its four control points, so the box can be slightly too large (library node in memory only)."""
    sch = _parts(tmp_path, tools, [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"}])
    root = _root(sch)
    node = _symbol(sch, "R1")
    sub = sheet_editing._kids(sheet_editing._lib_symbol(root, node), "symbol")[0]
    pts = SExp.list("pts", *(SExp.list("xy", x, y) for x, y in ((0.0, 0.0), (0.0, 10.0), (10.0, 10.0), (10.0, 0.0))))
    sub.children.append(SExp.list("bezier", pts))
    box = sheet_align.symbol_box(root, node)
    assert box[2] == pytest.approx(50.8 + 10.0, abs=1e-3) and box[1] == pytest.approx(50.8 - 10.0, abs=1e-3)


# --------------------------------------------------------------------------- delta review of issue 90: the two survivors of the mutation sweep


def test_an_item_named_twice_in_one_list_is_refused_by_name(sheet):
    before = _bytes(sheet)
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "R1"], "edge": "top"}])
    assert result["ok"] is False and result["code"] == "invalid_spec" and "names an item twice" in result["reason"], result
    result = _distribute(sheet, [{"kind": "label", "texts": ["A1", "A2", "A1"], "axis": "x"}])
    assert result["ok"] is False and result["code"] == "invalid_spec" and "names an item twice" in result["reason"], result
    assert _bytes(sheet) == before


def test_a_symbol_without_library_geometry_in_the_file_is_refused(sheet):
    """The embedded library symbol of Device:C is taken out of the sheet's lib_symbols (a fixture edit through the write layer):
    C1 has no geometry to compute a box from, so the call is refused rather than guessing."""
    root = write_layer.load(sheet)
    table = next(c for c in root.children if not c.is_atom and c.name == "lib_symbols")
    table.children[:] = [c for c in table.children if c.is_atom or c.get_string(0) != "Device:C"]
    write_layer.save(root, sheet)
    before = _bytes(sheet)
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "C1"], "edge": "top"}])
    assert result["ok"] is False and result["code"] == "no_symbol_geometry" and "C1" in result["reason"], result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- second delta review of issue 90: pinned guards


def test_align_bottom_of_a_rotated_part_uses_the_lower_edge(tmp_path, tools):
    """B-1. R (box 46.99..54.61 high) and C rotated 270 (plates run vertically, 57.968..62.032): 'bottom' with reference 'max'
    is the larger lower edge, 62.032, and R's lower pin tip is put there (a 'top' edge would give 57.968 instead)."""
    sch = _parts(tmp_path, tools, [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
                                   {"lib_id": "Device:C", "x": 101.6, "y": 60.0, "ref": "C1", "value": "1u", "rotation": 270}])
    result = _align(sch, [{"kind": "symbol", "references": ["R1", "C1"], "edge": "bottom", "reference": "max"}], snap=False, dry_run=True)
    assert result["ok"] is True, result
    group = result["groups"][0]
    assert group["target"] == pytest.approx(62.032, abs=1e-3)
    rows = {r["reference"]: r for r in group["items"]}
    assert rows["R1"]["edge_before"] == pytest.approx(54.61) and rows["C1"]["edge_before"] == pytest.approx(62.032, abs=1e-3)
    assert result["would_move"][0]["to"] == pytest.approx([50.8, 58.222], abs=1e-3)       # R's lowest pin tip: 58.222 + 3.81 = 62.032


def test_the_box_of_the_power_unit_and_of_unit_2_vs_unit_1(tmp_path, tools):
    """B-2. LM358 unit 3 (the power unit) unrotated at (152.4, 101.6): pins at x -2.54 and y +-7.62, nothing else, so the box is
    the line (149.86, 93.98)..(149.86, 109.22). Units 1 and 2 have the same drawing, so unit 2's pin is moved in the library node (in
    memory only): only a box that picks the placed unit's own sub-symbol sees it."""
    sch = _parts(tmp_path, tools, [
        {"lib_id": "Amplifier_Operational:LM358", "x": 152.4, "y": 101.6, "ref": "U1", "value": "x", "unit": 3},
        {"lib_id": "Amplifier_Operational:LM358", "x": 50.8, "y": 50.8, "ref": "U1", "value": "x", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM358", "x": 101.6, "y": 50.8, "ref": "U1", "value": "x", "unit": 2}])
    root = _root(sch)
    by_x = {round(sheet_editing._at(n)[0], 2): n for n in _nodes(sch, "symbol")}
    assert sheet_align.symbol_box(root, by_x[152.4]) == pytest.approx((149.86, 93.98, 149.86, 109.22), abs=1e-3)
    lib = sheet_editing._lib_symbol(root, by_x[50.8])
    unit2 = next(sub for sub in sheet_editing._kids(lib, "symbol") if sub.get_string(0) == "LM358_2_1")
    for pin in sheet_editing._kids(unit2, "pin"):
        if pin.find_children("at")[0].get_float(0) > 0:
            write_layer.set_number(pin.find_children("at")[0], 0, 12.7)
    assert sheet_align.symbol_box(root, by_x[50.8])[2] == pytest.approx(50.8 + 7.62, abs=1e-3)      # unit 1: its own pin at 7.62
    assert sheet_align.symbol_box(root, by_x[101.6])[2] == pytest.approx(101.6 + 12.7, abs=1e-3)    # unit 2: the moved pin


def test_an_item_that_lands_exactly_on_the_grid_is_not_reported_as_snapped(sheet):
    """B-3. R2 (y 60.96) and R3 (y 55.88) to the centre line 63.5: deltas 2.54 and 7.62 land on the 1.27 grid, nothing is rounded."""
    result = _align(sheet, [{"kind": "symbol", "references": ["R2", "R3"], "edge": "vcenter", "reference": 63.5}], dry_run=True)
    assert result["ok"] is True, result
    assert [r["snapped"] for r in result["groups"][0]["items"]] == [False, False] and result["snapped"] == []
    assert all(m["snapped"] is None for m in result["would_move"]) and result["groups"][0]["max_residual"] == 0
    # and one that has to be rounded is reported: 63.0 is not on the grid
    result = _align(sheet, [{"kind": "symbol", "references": ["R2", "R3"], "edge": "vcenter", "reference": 63.0}], dry_run=True)
    assert [r["snapped"] for r in result["groups"][0]["items"]] == [True, True] and len(result["snapped"]) == 2


def test_a_row_says_whether_the_item_moved(sheet):
    """B-4. R1 already sits on the target top edge: its row says moved False, R2's says True."""
    result = _align(sheet, [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top", "reference": "first"}], dry_run=True)
    rows = {r["reference"]: r for r in result["groups"][0]["items"]}
    assert rows["R1"]["moved"] is False and rows["R2"]["moved"] is True, rows
    result = _distribute(sheet, [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}], dry_run=True)
    rows = {r["reference"]: r for r in result["groups"][0]["items"]}
    assert rows["R1"]["moved"] is False and rows["R3"]["moved"] is False and rows["R2"]["moved"] is True, rows


def test_label_anchors_align_exactly_to_a_numeric_reference(sheet):
    """B-5. A label is its anchor point: left, right and hcenter to 90.0 all put the anchor at x = 90.0 (snap off), with no +-1 mm box."""
    for edge in ("left", "right", "hcenter"):
        result = _align(sheet, [{"kind": "label", "texts": ["A1", "A3"], "edge": edge, "reference": 90.0}], snap=False, dry_run=True,
                        allow_connectivity_change=True)
        assert result["ok"] is True, result
        assert [m["to"][0] for m in result["would_move"]] == [90.0, 90.0], (edge, result["would_move"])
    result = _distribute(sheet, [{"kind": "label", "texts": ["A1", "A2", "A3"], "axis": "x", "spacing": 20.0}], snap=False, dry_run=True,
                         allow_connectivity_change=True)
    assert [m["to"][0] for m in result["would_move"]] == [70.8, 90.8], result["would_move"]       # pitch from the first anchor (50.8)
    group = result["groups"][0]
    assert group["gaps_after"] == pytest.approx([20.0, 20.0]) and group["nominal"] == 20.0


def test_a_hide_no_flag_leaves_the_pin_in_the_box(tmp_path, tools):
    """B-6. (hide no) is no hide: a pin carrying it counts (library node changed in memory only)."""
    sch = _parts(tmp_path, tools, [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"}])
    root = _root(sch)
    node = _symbol(sch, "R1")
    pins = [p for sub in sheet_editing._kids(sheet_editing._lib_symbol(root, node), "symbol") for p in sheet_editing._kids(sub, "pin")]
    for pin in pins:
        write_layer.set_number(pin.find_children("at")[0], 1, 25.4 if pin.find_children("at")[0].get_float(1) > 0 else -25.4)
        pin.children.append(SExp.list("hide", write_layer.symbol("no")))
    assert not any(sheet_align._hidden(p) for p in pins)
    box = sheet_align.symbol_box(root, node)
    assert box[1] == pytest.approx(25.4, abs=1e-3) and box[3] == pytest.approx(76.2, abs=1e-3), box     # the far pins are counted
    for pin in pins:
        pin.children.pop()
        pin.children.append(SExp.list("hide", write_layer.symbol("yes")))
    assert sheet_align.symbol_box(root, node)[3] < 55.0                                              # hidden: they are not


def test_the_module_functions_have_safe_defaults(sheet):
    """Follow-up: called without options, align() and distribute() move the wiring along (move_wires True), snap to the 1.27 grid
    and refuse a connectivity change (allow_connectivity_change False)."""
    ok = sheet_align.align(str(sheet), [{"kind": "symbol", "references": ["R1", "R2"], "edge": "top"}], None)
    assert ok["ok"] is True and ok["move_wires"] is True and ok["written"] is True and ok["grid"] == 1.27, ok
    assert _label_at(sheet, "A2") == (71.12, 46.99)
    before = _bytes(sheet)
    refused = sheet_align.align(str(sheet), [{"kind": "label", "texts": ["B1", "B3"], "edge": "left"}], None)
    assert refused["ok"] is False and refused["code"] == "connectivity_changed", refused
    refused = sheet_align.distribute(str(sheet), [{"kind": "label", "texts": ["A1", "A2", "A3"], "axis": "x"}], None)
    assert refused["ok"] is False and refused["code"] == "connectivity_changed", refused
    assert _bytes(sheet) == before
    wired = sheet_align.distribute(str(sheet), [{"kind": "symbol", "references": ["R1", "R2", "R3"], "axis": "x"}], None)
    assert wired["ok"] is True and wired["move_wires"] is True and wired["grid"] == 1.27 and wired["written"] is True, wired
