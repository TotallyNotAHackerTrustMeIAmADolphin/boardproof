"""snap_to_grid (GitHub issue 95): off-grid connection points moved onto the grid, with real kicad-cli.

The template project is built once through the tools (off the grid on purpose: R1 sits 0.1 mm and 0.05 mm
off, with a wire, a label and a no-connect marker on its pins, a floating junction and label off the grid,
two parts on the grid). Every test works on its own copy. Positions are read back from the raw ``(at ...)``
and ``(pts ...)`` nodes and the pin maths on them, never from a kicad_tools property.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_authoring, sheet_editing, sheet_grid, write_layer
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset

GRID = 1.27


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
    tools = _tools()
    base = tmp_path_factory.mktemp("snap") / "proj"
    sch = Path(project_toolset._project_create(str(base), "demo")["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.9, "y": 50.75, "ref": "R1", "value": "10k"},
        {"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R2", "value": "1k"},
        {"lib_id": "Device:R", "x": 127.0, "y": 50.8, "ref": "R3", "value": "2k"},
    ])["status"] == "ok"
    assert tools["add_wires"](str(sch), [{"p1": [50.9, 46.94], "p2": [50.9, 40.7], "route": "direct", "snap": False}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 50.9, "y": 40.7, "text": "N1", "snap": False},
                                          {"kind": "label", "x": 86.4, "y": 20.3, "text": "FL", "snap": False}])["status"] == "ok"
    assert tools["add_no_connects"](str(sch), [{"references": ["R1"], "pins": ["2"]}])["status"] == "ok"
    assert tools["add_junctions"](str(sch), [[76.25, 50.85]])["status"] == "ok"
    return sch


@pytest.fixture
def sheet(template, tmp_path) -> Path:
    shutil.copytree(template.parent, tmp_path / "p")
    return tmp_path / "p" / template.name


def _root(sch: Path):
    return write_layer.load(sch)


def _kids(sch: Path, tag: str):
    return [c for c in _root(sch).children if not c.is_atom and c.name == tag]


def _symbol(sch: Path, ref: str):
    return next(s for s in _kids(sch, "symbol") if sheet_editing._ref(s) == ref)


def _nets(sch: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    return sheet_editing.read_nets(tree, sch, None)


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _snap(sch: Path, **kw) -> dict:
    return _tools()["snap_to_grid"](str(sch), **kw)


def _on_grid(v: float) -> bool:
    return abs(v - round(v / GRID) * GRID) <= 1e-3


def _off_grid_points(sch: Path) -> list:
    """Every connection point of the sheet that is off the grid, read from the raw nodes."""
    root, out = _root(sch), []
    for sym in _kids(sch, "symbol"):
        out += [(sheet_editing._ref(sym), p["number"], p["x"], p["y"]) for p in sheet_editing.pin_positions(root, sym)
                if not (_on_grid(p["x"]) and _on_grid(p["y"]))]
    for wire in _kids(sch, "wire"):
        out += [("wire", x, y) for x, y in sheet_editing._wire_ends(wire) if not (_on_grid(x) and _on_grid(y))]
    for tag in ("label", "junction", "no_connect"):
        out += [(tag, *sheet_editing._at(n)[:2]) for n in _kids(sch, tag) if not (_on_grid(sheet_editing._at(n)[0]) and _on_grid(sheet_editing._at(n)[1]))]
    return out


# --------------------------------------------------------------------------- detect and move


def test_the_template_is_off_the_grid_where_it_should_be(sheet):
    assert sheet_editing._at(_symbol(sheet, "R1"))[:2] == (50.9, 50.75)
    assert len(_off_grid_points(sheet)) == 8  # 2 pins, 2 wire ends, 2 labels (N1, FL), the junction, the marker


def test_dry_run_lists_what_would_move_and_writes_nothing(sheet):
    before = _bytes(sheet)
    out = _snap(sheet, dry_run=True)
    assert out["ok"] is True and out["written"] is False and _bytes(sheet) == before
    moved = {(e["kind"], e.get("reference") or e.get("text")): e for e in out["would_move"]}
    assert moved[("symbol", "R1")]["to"] == [50.8, 50.8] and moved[("symbol", "R1")]["shift"] == pytest.approx([-0.1, 0.05])
    assert out["found_off_grid"] == {"symbol": 1, "wire": 1, "label": 2, "junction": 1, "no_connect": 1}
    assert out["already_on_grid"]["symbol"] == 2 and out["grid"] == GRID and out["unsnappable"] == []


def test_the_snap_puts_every_point_on_the_grid_and_keeps_every_net(sheet):
    before_nets = _nets(sheet)
    out = _snap(sheet)
    assert out["ok"] is True and out["written"] is True, out
    assert _off_grid_points(sheet) == []
    r1 = _symbol(sheet, "R1")
    assert sheet_editing._at(r1)[:2] == (50.8, 50.8)
    assert _nets(sheet) == before_nets
    assert out["connectivity"]["changed"] is False
    # the wire end on the pin followed the pin, and the label on the far end followed the wire end (one place, one target)
    wire = _kids(sheet, "wire")[0]
    assert sorted(sheet_editing._wire_ends(wire)) == [(50.8, 40.64), (50.8, 46.99)]
    label = next(n for n in _kids(sheet, "label") if n.get_string(0) == "N1")
    assert sheet_editing._at(label)[:2] == (50.8, 40.64)
    marker = _kids(sheet, "no_connect")[0]
    assert sheet_editing._at(marker)[:2] == (50.8, 54.61)
    assert sheet_editing._at(_kids(sheet, "junction")[0])[:2] == (76.2, 50.8)


def test_the_property_texts_move_with_their_symbol(template, sheet):
    before = {p.get_string(0): sheet_editing._at(p)[:2] for p in sheet_editing._kids(_symbol(template, "R1"), "property")}
    _snap(sheet)
    after = {p.get_string(0): sheet_editing._at(p)[:2] for p in sheet_editing._kids(_symbol(sheet, "R1"), "property")}
    for name, (x, y) in before.items():
        assert after[name] == pytest.approx((x - 0.1, y + 0.05), abs=1e-3), name


def test_kicad_cli_reports_the_off_grid_points_before_and_not_after(sheet):
    """ERC is the reference the tool is the fix for: endpoint_off_grid before, none after."""
    def census(sch: Path):
        tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
        import tempfile

        with tempfile.TemporaryDirectory(prefix="t95-erc-") as tmp:
            copied = sheet_authoring._copy_project(tree, [], Path(tmp) / "p")
            return {kind: n for (kind, _sev), n in sheet_authoring.erc_census(copied).items()}

    assert census(sheet).get("endpoint_off_grid", 0) > 0
    assert _snap(sheet)["ok"] is True
    assert "endpoint_off_grid" not in census(sheet)


def test_a_sheet_with_nothing_off_the_grid_is_not_written(sheet):
    assert _snap(sheet)["ok"] is True
    before = _bytes(sheet)
    out = _snap(sheet)
    assert out["ok"] is True and out["count"] == 0 and out["written"] is False and _bytes(sheet) == before


def test_kinds_narrow_what_is_examined_and_moved(sheet):
    out = _snap(sheet, kinds=["junction"])
    assert out["ok"] is True and {e["kind"] for e in out["moved"]} == {"junction"} and out["kinds"] == ["junction"]
    assert sheet_editing._at(next(n for n in _kids(sheet, "label") if n.get_string(0) == "FL"))[:2] == (86.4, 20.3)
    assert sheet_editing._at(_symbol(sheet, "R1"))[:2] == (50.9, 50.75)
    assert sheet_editing._at(_kids(sheet, "junction")[0])[:2] == (76.2, 50.8)


# --------------------------------------------------------------------------- refusals


def test_a_snap_that_would_move_a_wire_end_off_its_pin_is_refused(sheet):
    """kinds without symbol: the wire end snaps away from the off-grid pin, the netlist comparison refuses."""
    before = _bytes(sheet)
    out = _snap(sheet, kinds=["wire"])
    assert out["ok"] is False and out["code"] == "connectivity_changed", out
    assert out["connectivity"]["changed"] is True and _bytes(sheet) == before
    assert "allow_connectivity_change" not in _tools()["snap_to_grid"].__code__.co_varnames


def test_a_symbol_whose_pins_cannot_all_reach_the_grid_stays_and_what_sits_on_it_stays(sheet):
    """On a 5.08 mm grid the 7.62 mm pin pitch of a resistor cannot land on it: R1 stays, its wire end stays on its pin."""
    out = _snap(sheet, grid=5.08, dry_run=True)
    assert out["ok"] is True, out
    kinds = {(u["kind"], u.get("reference")) for u in out["unsnappable"]}
    assert ("symbol", "R1") in kinds
    assert not [e for e in out["would_move"] if e.get("reference") == "R1"]
    wire = next(e for e in out["would_move"] if e["kind"] == "wire")
    assert [50.9, 46.94] in wire["now"]  # the end on the pin that stays is where it was
    assert any(u["kind"] == "wire" for u in out["unsnappable"])


def test_a_hierarchical_sheet_symbol_off_the_grid_is_listed_not_moved(tmp_path):
    tools = _tools()
    sch = Path(project_toolset._project_create(str(tmp_path / "h"), "h")["schematic_path"])
    assert tools["create_sheets"](str(sch), [{"file": "a.kicad_sch"}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(sch), [{"file": "a.kicad_sch", "x": 50.8, "y": 53.34, "pins": [{"name": "X", "direction": "input"}], "name": "A"}])["status"] == "ok"
    out = tools["snap_to_grid"](str(sch), grid=5.08, dry_run=True)
    assert out["ok"] is True and [u["kind"] for u in out["unsnappable"]] == ["sheet"] and out["unsnappable"][0]["name"] == "A"


def test_a_wire_the_snap_would_shrink_to_a_point_is_refused(sheet):
    assert _tools()["add_wires"](str(sheet), [{"p1": [30.5, 20.3], "p2": [30.4, 20.3], "route": "direct", "snap": False}])["status"] == "ok"
    before = _bytes(sheet)
    out = _snap(sheet)
    assert out["ok"] is False and out["code"] == "degenerate_wire" and _bytes(sheet) == before


@pytest.mark.parametrize("args", [{"kinds": []}, {"kinds": ["nope"]}, {"kinds": ["wire", "wire"]}, {"kinds": "wire"},
                                  {"grid": 0}, {"grid": -1.27}, {"grid": True}, {"grid": "1.27"}, {"grid": float("nan")}, {"grid": float("inf")}, {"grid": 1e9}])
def test_bad_arguments_are_refused_and_write_nothing(sheet, args):
    before = _bytes(sheet)
    out = _snap(sheet, **args)
    assert out["ok"] is False and out["code"] == "invalid_spec" and _bytes(sheet) == before


def test_a_missing_sheet_is_refused(tmp_path):
    assert _tools()["snap_to_grid"](str(tmp_path / "none.kicad_sch"))["code"] == "file_not_found"


def test_a_sheet_open_in_kicad_is_refused(sheet):
    sheet.with_name("~" + sheet.name + ".lck").write_text("{}")
    before = sheet.read_bytes()
    out = _snap(sheet)
    assert out["ok"] is False and out["code"] == "file_open_in_kicad" and sheet.read_bytes() == before


# --------------------------------------------------------------------------- the guards fire


def test_the_on_grid_scan_fires_when_a_point_is_snapped_to_the_wrong_place(sheet, monkeypatch):
    before = _bytes(sheet)
    monkeypatch.setattr(sheet_grid, "_snap_value", lambda value, grid: sheet_editing._snap(value, grid) + 0.1)
    out = _snap(sheet)
    assert out["ok"] is False and out["code"] == "write_not_verified" and out.get("check") == "on_grid_after", out
    assert _bytes(sheet) == before


def test_a_corrupted_write_is_refused_and_leaves_the_sheet_alone(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    out = _snap(sheet)
    assert out["ok"] is False and out["code"] == "write_not_verified", out
    monkeypatch.undo()
    assert _bytes(sheet) == before
    assert not [p for p in sheet.parent.iterdir() if p.name.endswith(".tmp")]


def test_the_staged_check_fires_when_the_call_touches_more_than_it_moved(sheet, monkeypatch):
    real = sheet_grid._scan

    def greedy(root, kinds, grid):
        symbol = next(c for c in root.children if not c.is_atom and c.name == "symbol" and sheet_editing._ref(c) == "R3")
        write_layer.set_string(symbol.find_children("property")[0], 1, "STRAY")
        return real(root, kinds, grid)

    monkeypatch.setattr(sheet_grid, "_scan", greedy)
    before = _bytes(sheet)
    out = _snap(sheet)
    assert out["ok"] is False and out["code"] == "write_not_verified", out
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- geometry the netlist cannot see (review of issue 95)


def test_a_snap_that_lays_a_wire_over_two_pins_is_refused(sheet):
    """A wire 0.06 mm beside R1 snaps onto its pins 1 and 2: the netlist shows no change (a wire over a pin joins nothing), the geometry check does."""
    assert _tools()["add_wires"](str(sheet), [{"p1": [50.74, 40.64], "p2": [50.74, 60.96], "route": "direct", "snap": False}])["status"] == "ok"
    before = _bytes(sheet)
    out = _snap(sheet)
    assert out["ok"] is False and out["code"] == "snap_creates_connection", out
    assert len(out["crossings"]) == 2 and _bytes(sheet) == before


def test_a_snap_that_puts_a_wire_end_exactly_on_a_lone_pin_is_refused(sheet):
    assert _tools()["add_wires"](str(sheet), [{"p1": [101.65, 46.99], "p2": [101.65, 40.64], "route": "direct", "snap": False}])["status"] == "ok"
    before = _bytes(sheet)
    out = _snap(sheet)
    assert out["ok"] is False and out["code"] == "snap_creates_connection" and out["touching"], out
    assert _bytes(sheet) == before


def test_a_vertical_wire_that_becomes_slanted_is_listed(sheet):
    out = _snap(sheet, grid=5.08, dry_run=True)
    wire = next(e for e in out["would_move"] if e["kind"] == "wire")
    assert out["slanted"] == [wire["uuid"]]
    assert _snap(sheet, dry_run=True)["slanted"] == []


# --------------------------------------------------------------------------- the tool text's other claims


def _add(sheet: Path, specs: list[dict]) -> None:
    assert _tools()["add_symbols"](str(sheet), specs)["status"] == "ok"


def test_power_rotated_mirrored_and_multi_unit_symbols_are_snapped(sheet):
    _add(sheet, [
        {"lib_id": "power:GND", "x": 80.05, "y": 90.1, "ref": "#PWR01"},
        {"lib_id": "Device:R", "x": 150.1, "y": 90.05, "ref": "R7", "value": "1k", "rotation": 90},
        {"lib_id": "Device:C", "x": 170.1, "y": 90.05, "ref": "C1", "value": "1u", "mirror": "x"},
        {"lib_id": "Amplifier_Operational:LM324", "x": 190.1, "y": 90.05, "ref": "U1", "value": "LM324", "unit": 1},
    ])
    assert _off_grid_points(sheet)
    out = _snap(sheet)
    assert out["ok"] is True and out["unsnappable"] == [], out
    assert _off_grid_points(sheet) == []
    assert sheet_editing._at(_symbol(sheet, "#PWR01"))[:2] == (80.01, 90.17)


def test_a_hidden_pin_counts(sheet):
    """Hide R1's pin 2 in the embedded library symbol (low level, tests only): on a 5.08 grid the symbol still cannot be moved,
    which it could if only the visible pin 1 counted."""
    root = _root(sheet)
    lib = next(e for t in root.children if not t.is_atom and t.name == "lib_symbols" for e in t.children
               if not e.is_atom and e.name == "symbol" and e.get_string(0) == "Device:R")
    pin2 = next(p for sub in lib.children if not sub.is_atom and sub.name == "symbol" for p in sub.children
                if not p.is_atom and p.name == "pin" and p.find_children("number")[0].get_string(0) == "2")
    pin2.children.append(write_layer.SExp.list("hide", write_layer.symbol("yes")))
    write_layer.save(root, sheet)
    assert sheet_editing._pin_hidden(pin2)
    out = _snap(sheet, grid=5.08, dry_run=True)
    assert ("symbol", "R1") in {(u["kind"], u.get("reference")) for u in out["unsnappable"]}, out


def test_a_non_default_grid_moves_to_that_grid(sheet):
    before_nets = _nets(sheet)
    out = _snap(sheet, grid=2.54)
    assert out["ok"] is True and out["grid"] == 2.54, out
    root = _root(sheet)
    for sym in _kids(sheet, "symbol"):
        for p in sheet_editing.pin_positions(root, sym):
            assert abs(p["x"] / 2.54 - round(p["x"] / 2.54)) * 2.54 <= 1e-3 and abs(p["y"] / 2.54 - round(p["y"] / 2.54)) * 2.54 <= 1e-3, p
    assert _nets(sheet) == before_nets


def test_a_sheet_a_parent_uses_is_snapped_with_and_without_root_sch(tmp_path):
    tools = _tools()
    root_sch = Path(project_toolset._project_create(str(tmp_path / "h"), "h")["schematic_path"])
    assert tools["create_sheets"](str(root_sch), [{"file": "a.kicad_sch"}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(root_sch), [{"file": "a.kicad_sch", "x": 50.8, "y": 50.8, "pins": [{"name": "X", "direction": "input"}], "name": "A"}])["status"] == "ok"
    child = root_sch.parent / "a.kicad_sch"
    assert tools["add_symbols"](str(child), [{"lib_id": "Device:R", "x": 50.9, "y": 50.75, "ref": "R1", "value": "1k"}])["status"] == "ok"
    out = tools["snap_to_grid"](str(child), dry_run=True)
    assert out["ok"] is True and out["count"] == 1, out
    out = tools["snap_to_grid"](str(child), root_sch=str(root_sch))
    assert out["ok"] is True and out["written"] is True, out
    assert sheet_editing._at(_symbol(child, "R1"))[:2] == (50.8, 50.8)


def test_a_wire_that_already_ran_over_a_pin_and_moves_with_it_is_not_a_new_crossing(sheet):
    assert _tools()["add_wires"](str(sheet), [{"p1": [45.72, 46.94], "p2": [55.88, 46.94], "route": "direct", "snap": False}])["status"] == "ok"
    out = _snap(sheet)
    assert out["ok"] is True and out["written"] is True, out
    assert _off_grid_points(sheet) == []


def test_a_wire_over_pin_a_before_and_over_pin_b_after_is_refused(sheet, monkeypatch):
    """Equal counts per wire hide a swap: the crossing check compares (wire, point) after mapping the points that moved."""
    wire = sheet_editing._uuid(_kids(sheet, "wire")[0])
    calls = iter([{(wire, (101.6, 46.99))}, {(wire, (101.6, 54.61))}])
    monkeypatch.setattr(sheet_grid.sheet_orient, "_crossings", lambda *a, **k: next(calls))
    before = _bytes(sheet)
    out = _snap(sheet)
    assert out["ok"] is False and out["code"] == "snap_creates_connection" and out["crossings"], out
    assert _bytes(sheet) == before


def test_a_crossing_that_moves_with_its_point_is_mapped_not_new():
    moved = {(101.6, 46.99): (101.6, 47.0)}
    assert sheet_grid._fresh_crossings({("w", (101.6, 46.99))}, {("w", (101.6, 47.0))}, lambda x, y: moved.get((x, y), (x, y))) == []
