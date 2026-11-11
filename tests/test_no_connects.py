"""add_no_connects and delete_no_connects (GitHub issue 113), with real kicad-cli.

A connector with two wired pins and six open ones is built through the tools; kicad-cli's ERC must
show pin_not_connected for the open pins before and none for the marked ones after, and the netlist
must be the same before and after. Pin points are never taken from a convenience property: the
markers are checked against the raw ``(at ...)`` of the sheet and, above all, against ERC, which
only stops reporting a pin when the marker sits exactly on it. Each guard has a mutation check.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from boardproof import kicad_cli, no_connects, sheet_authoring, sheet_editing, write_layer
from boardproof.toolsets import project as project_toolset
from tests.test_sheet_editing import _bytes, _nets, _nodes, _tools


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture
def tools() -> dict:
    return _tools()


def _erc(sch: Path, tmp_path: Path, kind: str = "pin_not_connected") -> int:
    copy = tmp_path / f"erc{len(list(tmp_path.glob('erc*')))}"
    shutil.copytree(sch.parent, copy)
    census = sheet_authoring.erc_census(copy / sch.name)
    return sum(n for (t, _s), n in census.items() if t == kind)


def _markers(sch: Path) -> list[tuple[float, float]]:
    return sorted(sheet_editing._at(n)[:2] for n in _nodes(sch, "no_connect"))


@pytest.fixture
def board(tmp_path, tools) -> Path:
    """J1 (8-pin connector) with pins 1 and 2 wired to labelled stubs; pins 3 to 8 open."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "nc")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Connector_Generic:Conn_01x08", "x": 50.8, "y": 63.5, "ref": "J1", "value": "Conn"},
    ])["status"] == "ok"
    pins = _pin_points(sch, tools, "J1")
    for number, net in (("1", "NETA"), ("2", "NETB")):
        x, y = pins[number]
        assert tools["add_wires"](str(sch), [{"p1": [x, y], "p2": [x - 5.08, y], "route": "direct", "snap": False}])["status"] == "ok"
        assert tools["add_labels"](str(sch), [{"kind": "label", "x": x - 5.08, "y": y, "text": net, "snap": False}])["status"] == "ok"
    return sch


def _pin_points(sch: Path, tools, ref: str) -> dict[str, tuple[float, float]]:
    entry = tools["get_symbol_pins"](str(sch), [ref])["symbols"][0]
    return {p["number"]: (p["x"], p["y"]) for p in entry["pins"]}


# --------------------------------------------------------------------------- add


def test_fixture_has_open_pins_in_erc(board, tmp_path):
    assert _erc(board, tmp_path) == 6


def test_add_unconnected_leaves_no_pin_not_connected_and_changes_no_net(board, tools, tmp_path):
    before_nets = _nets(board)
    pins = _pin_points(board, tools, "J1")
    result = tools["add_no_connects"](str(board), [{"references": ["J1"]}])
    assert result["ok"] is True and result["written"] is True and result["count"] == 6, result
    assert result["connectivity"]["changed"] is False
    assert [e["pin"] for e in result["added"]] == ["3", "4", "5", "6", "7", "8"]
    # the raw (at ...) of each marker is the pin point
    assert _markers(board) == sorted(pins[str(n)] for n in range(3, 9))
    assert _erc(board, tmp_path) == 0
    assert _nets(board) == before_nets


def test_add_with_no_selector_marks_every_open_pin_of_the_sheet_but_no_power_pin(board, tools, tmp_path):
    result = tools["add_no_connects"](str(board))
    assert result["ok"] is True and result["count"] == 6, result
    assert {e["reference"] for e in result["added"]} == {"J1"}
    assert _erc(board, tmp_path) == 0


def test_dry_run_writes_nothing_and_reports_would_add(board, tools):
    before = _bytes(board)
    result = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["3", "4"]}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and len(result["would_add"]) == 2, result
    assert _bytes(board) == before


def test_explicit_pins_mark_only_those(board, tools, tmp_path):
    result = tools["add_no_connects"](str(board), [{"references": ["J*"], "pins": ["3", "8"]}])
    assert result["ok"] is True and result["count"] == 2, result
    pins = _pin_points(board, tools, "J1")
    assert _markers(board) == sorted([pins["3"], pins["8"]])
    assert _erc(board, tmp_path) == 4


def test_a_second_call_skips_existing_markers_and_writes_nothing(board, tools):
    assert tools["add_no_connects"](str(board), [{"references": ["J1"]}])["ok"] is True
    before = _bytes(board)
    result = tools["add_no_connects"](str(board), [{"references": ["J1"]}])
    assert result["ok"] is True and result["count"] == 0 and result["written"] is False, result
    assert _bytes(board) == before
    explicit = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["3"]}])
    assert explicit["count"] == 0 and [e["pin"] for e in explicit["already_marked"]] == ["3"], explicit


def test_a_wired_pin_is_refused_as_pin_connected_and_nothing_is_written(board, tools):
    before = _bytes(board)
    result = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["3", "1"]}])
    assert result["ok"] is False and result["code"] == "pin_connected", result
    assert any(p.startswith("J1.1") for p in result["pins"])
    assert _bytes(board) == before


def test_a_pin_with_only_a_label_is_connected(board, tools):
    pins = _pin_points(board, tools, "J1")
    assert tools["add_labels"](str(board), [{"kind": "label", "x": pins["4"][0], "y": pins["4"][1], "text": "LBL", "snap": False}])["status"] == "ok"
    before = _bytes(board)
    result = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["4"]}])
    assert result["ok"] is False and result["code"] == "pin_connected", result
    assert _bytes(board) == before
    unconnected = tools["add_no_connects"](str(board), [{"references": ["J1"]}])
    assert unconnected["ok"] is True and unconnected["count"] == 5 and unconnected["connected_pins_left_alone"] >= 1, unconnected


def test_a_wire_that_passes_over_a_pin_does_not_connect_it(board, tools, tmp_path):
    pins = _pin_points(board, tools, "J1")
    x, y = pins["5"]
    assert tools["add_wires"](str(board), [{"p1": [x - 5.08, y], "p2": [x + 5.08, y], "route": "direct", "snap": False}])["status"] == "ok"
    result = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["5"]}])
    assert result["ok"] is True and result["count"] == 1, result
    assert result["connectivity"]["changed"] is False


def test_a_floating_power_symbol_is_not_marked_and_not_nameable(board, tools, tmp_path):
    assert tools["add_symbols"](str(board), [{"lib_id": "power:GND", "x": 127, "y": 76.2, "ref": "#PWR01", "value": "GND"}])["status"] == "ok"
    result = tools["add_no_connects"](str(board))
    assert result["ok"] is True and result["count"] == 6 and {e["reference"] for e in result["added"]} == {"J1"}, result
    assert _erc(board, tmp_path) == 1  # the floating power symbol is a wiring error, not an open pin
    before = _bytes(board)
    named = tools["add_no_connects"](str(board), [{"references": ["#PWR01"], "pins": ["1"]}])
    assert named["ok"] is False and named["code"] == "power_symbol_pin", named
    assert _bytes(board) == before


def test_hidden_pin_is_left_out_of_unconnected_and_refused_when_named(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "hid"), "hid")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "74xx:74ALVC164245", "x": 100.0, "y": 100.0, "ref": "U2", "value": "x"}])["status"] == "ok"
    pins = tools["get_symbol_pins"](str(sch), ["U2"])["symbols"][0]["pins"]
    hidden = {p["number"] for p in pins if p["type"] == "passive"}
    assert "10" in hidden
    result = tools["add_no_connects"](str(sch), [{"references": ["U2"]}], dry_run=True)
    assert result["ok"] is True, result
    marked = {e["pin"] for e in result["would_add"]}
    assert "10" not in marked and "15" not in marked and "1" in marked
    before = _bytes(sch)
    named = tools["add_no_connects"](str(sch), [{"references": ["U2"], "pins": ["10"]}])
    assert named["ok"] is False and named["code"] == "pin_hidden", named
    assert _bytes(sch) == before


@pytest.mark.parametrize("selectors", [
    [{"references": ["J9"]}],
    [{"references": ["J1"], "pins": ["3", "99"]}],
    [{"uuids": ["nope"]}],
])
def test_a_missing_reference_pin_or_uuid_refuses_the_whole_call(board, tools, selectors):
    before = _bytes(board)
    result = tools["add_no_connects"](str(board), selectors)
    assert result["ok"] is False and result["code"] == "not_found", result
    assert _bytes(board) == before


@pytest.mark.parametrize("selectors", [[], [{}], [{"pins": ["1"]}], [{"references": ["J1"], "colour": 1}], [{"references": ["J1"], "pins": []}], "unconnected"])
def test_bad_selectors_are_invalid_spec(board, tools, selectors):
    before = _bytes(board)
    result = tools["add_no_connects"](str(board), selectors)
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _bytes(board) == before


def test_rotated_and_mirrored_connectors_get_markers_on_their_pins(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "rot"), "rot")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Connector_Generic:Conn_01x04", "x": 50.8, "y": 50.8, "ref": "J1", "value": "c", "rotation": 90},
        {"lib_id": "Connector_Generic:Conn_01x04", "x": 101.6, "y": 50.8, "ref": "J2", "value": "c", "rotation": 270},
        {"lib_id": "Connector_Generic:Conn_01x04", "x": 152.4, "y": 50.8, "ref": "J3", "value": "c", "rotation": 180},
    ])["status"] == "ok"
    assert _erc(sch, tmp_path) == 12
    result = tools["add_no_connects"](str(sch), [{"references": ["J*"]}])
    assert result["ok"] is True and result["count"] == 12, result
    assert _erc(sch, tmp_path) == 0


def test_the_erc_self_check_fires_on_a_marker_that_misses_its_pin(board, tools, monkeypatch):
    real = no_connects.marker_node
    monkeypatch.setattr(no_connects, "marker_node", lambda x, y: real(x + 2.54, y + 2.54))
    before = _bytes(board)
    result = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["3"]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert result.get("check") == "erc_pin_not_connected", result
    monkeypatch.undo()
    assert _bytes(board) == before


def test_add_no_connects_corrupted_write_is_refused(board, tools, monkeypatch):
    real = write_layer.load
    before = _bytes(board)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = tools["add_no_connects"](str(board), [{"references": ["J1"]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(board) == before


def test_the_staged_check_fires_when_the_tool_adds_a_node_it_did_not_report(board, tools, monkeypatch):
    real = sheet_editing.Outcome

    class Sink(set):
        def add(self, item):
            return None

    class Quiet(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.added = Sink()

    monkeypatch.setattr(sheet_editing, "Outcome", Quiet)
    before = _bytes(board)
    result = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["3"]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(board) == before


def test_other_nodes_and_instance_data_are_untouched(board, tools):
    before = {n: write_layer.serialise(c, compact=True) for n in ("symbol", "wire", "label") for c in _nodes(board, n)}
    assert tools["add_no_connects"](str(board), [{"references": ["J1"]}])["ok"] is True
    after = {n: write_layer.serialise(c, compact=True) for n in ("symbol", "wire", "label") for c in _nodes(board, n)}
    assert after == before


# --------------------------------------------------------------------------- delete


def _marked(board, tools) -> None:
    assert tools["add_no_connects"](str(board), [{"references": ["J1"]}])["count"] == 6


def test_delete_by_reference_and_pin_restores_the_errors(board, tools, tmp_path):
    _marked(board, tools)
    nets = _nets(board)
    result = tools["delete_no_connects"](str(board), [{"references": ["J1"], "pins": ["3", "4"]}])
    assert result["ok"] is True and result["count"] == 2 and result["written"] is True, result
    assert len(_markers(board)) == 4
    assert _erc(board, tmp_path) == 2
    assert _nets(board) == nets
    assert result["connectivity"]["changed"] is False


def test_delete_by_uuid_point_and_region(board, tools, tmp_path):
    _marked(board, tools)
    nodes = _nodes(board, "no_connect")
    first = sheet_editing._uuid(nodes[0])
    second_at = sheet_editing._at(nodes[1])[:2]
    third_at = sheet_editing._at(nodes[2])[:2]
    result = tools["delete_no_connects"](str(board), [
        {"uuids": [first]}, {"points": [list(second_at)]},
        {"region": [third_at[0] - 0.1, third_at[1] - 0.1, third_at[0] + 0.1, third_at[1] + 0.1]},
    ])
    assert result["ok"] is True and result["count"] == 3, result
    assert len(_markers(board)) == 3
    assert _erc(board, tmp_path) == 3
    everything = tools["delete_no_connects"](str(board), [{"region": [0, 0, 400, 400]}])
    assert everything["count"] == 3
    assert _erc(board, tmp_path) == 6


def test_delete_dry_run_writes_nothing(board, tools):
    _marked(board, tools)
    before = _bytes(board)
    result = tools["delete_no_connects"](str(board), [{"region": [0, 0, 400, 400]}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and len(result["would_delete"]) == 6, result
    assert _bytes(board) == before


@pytest.mark.parametrize("selectors", [
    [{"uuids": ["nope"]}],
    [{"points": [[1.0, 1.0]]}],
    [{"references": ["J1"], "pins": ["1"]}],      # a wired pin has no marker
    [{"references": ["J9"], "pins": ["3"]}],
    [{"references": ["J1"], "pins": ["99"]}],
    [{"region": [0, 0, 1, 1]}],
])
def test_delete_refuses_a_selector_that_finds_nothing_and_writes_nothing(board, tools, selectors):
    _marked(board, tools)
    before = _bytes(board)
    result = tools["delete_no_connects"](str(board), selectors)
    assert result["ok"] is False and result["code"] in ("not_found", "selector_matched_nothing"), result
    assert _bytes(board) == before


def test_delete_requires_references_and_pins_together(board, tools):
    _marked(board, tools)
    before = _bytes(board)
    result = tools["delete_no_connects"](str(board), [{"references": ["J1"]}])
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _bytes(board) == before


def test_delete_no_connects_corrupted_write_is_refused(board, tools, monkeypatch):
    _marked(board, tools)
    real = write_layer.load
    before = _bytes(board)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = tools["delete_no_connects"](str(board), [{"region": [0, 0, 400, 400]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(board) == before


def test_delete_labels_still_refuses_the_no_connect_kind(board, tools):
    result = tools["delete_labels"](str(board), [{"kind": "no_connect"}])
    assert result["ok"] is False and result["code"] == "invalid_spec", result


def test_a_stray_marker_not_on_a_pin_can_be_deleted_by_point(board, tools):
    assert tools["add_labels"](str(board), [{"kind": "no_connect", "x": 200.66, "y": 120.65}])["status"] == "ok"
    assert [200.66, 120.65] in [list(m) for m in _markers(board)]
    result = tools["delete_no_connects"](str(board), [{"points": [[200.66, 120.65]]}])
    assert result["ok"] is True and result["count"] == 1, result
    assert _markers(board) == []


def test_both_tools_are_registered_as_writing_tools():
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import schematic as schematic_toolset

    mcp = MCPServer("test")
    names = schematic_toolset.build(mcp)
    for name in ("add_no_connects", "delete_no_connects"):
        tool = next(t for t in mcp._tool_manager.list_tools() if t.name == name)  # type: ignore[attr-defined]
        assert name in names and tool.annotations.read_only_hint is False
        assert "file_open_in_kicad" in tool.description and "connectivity_changed" in tool.description


def test_an_unannotated_reference_is_refused_as_pin_not_in_netlist(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "unann"), "unann")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "Connector_Generic:Conn_01x02", "x": 50.8, "y": 50.8, "ref": "J?", "value": "c"}])["status"] == "ok"
    before = _bytes(sch)
    result = tools["add_no_connects"](str(sch), [{"references": ["J?"]}])
    assert result["ok"] is False and result["code"] == "pin_not_in_netlist", result
    assert _bytes(sch) == before


def test_delete_refuses_a_batch_when_one_listed_pin_has_no_marker(board, tools):
    assert tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["3"]}])["ok"] is True
    before = _bytes(board)
    result = tools["delete_no_connects"](str(board), [{"references": ["J1"], "pins": ["3", "4"]}])
    assert result["ok"] is False and result["code"] == "not_found", result
    assert any("pin J1.4" in m or "J1.4" in m for m in result["not_found"]), result
    assert _bytes(board) == before


# ------------------------------------------------------------------ review fixes


def test_the_erc_self_check_fires_when_a_violation_type_grows_although_the_drop_is_right(board, tools, monkeypatch):
    """B1: the drop of pin_not_connected is correct, but another violation type gained one."""
    from collections import Counter

    real_init = sheet_editing.Sheet.__init__

    def init(self, root, nets_before=None, erc_for=None):
        real_init(self, root, nets_before, erc_for)
        inner = self.erc_for

        def grown(tree):
            census = Counter(inner(tree))
            if tree is not None:
                census[("label_dangling", "error")] += 1
            return census

        self.erc_for = grown

    monkeypatch.setattr(sheet_editing.Sheet, "__init__", init)
    before = _bytes(board)
    result = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["3"]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert result["check"] == "erc_pin_not_connected" and result["erc_drop"] == result["expected_drop"] == 1, result
    assert result["erc_grown"] == {"label_dangling": 1}, result
    monkeypatch.undo()
    assert _bytes(board) == before


def test_a_uuid_selector_marks_exactly_the_pins_of_that_symbol(board, tools):
    """B2: the uuids selector, with the pin list."""
    j1 = [se_uuid for se_uuid in (sheet_editing._uuid(n) for n in _nodes(board, "symbol") if sheet_editing._ref(n) == "J1")]
    assert len(j1) == 1
    pins = _pin_points(board, tools, "J1")
    result = tools["add_no_connects"](str(board), [{"uuids": j1, "pins": ["3"]}])
    assert result["ok"] is True and result["count"] == 1 and result["added"][0]["pin"] == "3", result
    assert _markers(board) == [pins["3"]]


def _slb(tmp_path, tools) -> Path:
    made = project_toolset._project_create(str(tmp_path / "slb"), "slb")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "Interface:SLB9660xT", "x": 100.0, "y": 100.0, "ref": "U1", "value": "x"}])["status"] == "ok"
    return sch


def test_a_named_visible_no_connect_type_pin_gets_a_marker_without_an_erc_drop(tmp_path, tools):
    """B3: KiCad does not report a pin of the type no_connect, so the marker drops nothing (expected_drop 0)."""
    sch = _slb(tmp_path, tools)
    before_erc = _erc(sch, tmp_path)
    result = tools["add_no_connects"](str(sch), [{"references": ["U1"], "pins": ["9"]}])
    assert result["ok"] is True and result["count"] == 1, result
    assert result["added"][0]["type"] == "no_connect" and len(_markers(sch)) == 1
    assert _erc(sch, tmp_path) == before_erc  # measured: no change
    # in the default mode the no_connect-type pins are left out, so nothing is added for them
    again = tools["add_no_connects"](str(sch), [{"references": ["U1"]}], dry_run=True)
    assert "9" not in {e["pin"] for e in again["would_add"]}, again


def test_a_hidden_pin_is_not_skipped_in_the_default_path_when_its_net_is_open(board, tools, tmp_path, monkeypatch):
    """B4: hidden pins tied by name sit in a shared net and are not open; an isolated one with an open net is marked."""
    real = sheet_editing.pin_positions

    def stub(root, symbol, **kw):
        pins = real(root, symbol, **kw)
        for p in pins:
            if p["number"] == "3":
                p["hidden"] = True
        return pins

    monkeypatch.setattr(sheet_editing, "pin_positions", stub)
    dry = tools["add_no_connects"](str(board), [{"references": ["J1"]}], dry_run=True)
    assert dry["ok"] is True and "3" in {e["pin"] for e in dry["would_add"]}, dry
    named = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["3"]}], dry_run=True)
    assert named["ok"] is False and named["code"] == "pin_hidden", named


def test_marking_input_and_power_in_pins_reports_the_other_erc_types_it_silences(tmp_path, tools):
    """F1: the entries keep their pin type and the report counts the pins whose pin_not_driven / power_pin_not_driven went too."""
    sch = _slb(tmp_path, tools)
    before_driven = _erc(sch, tmp_path, "pin_not_driven")
    result = tools["add_no_connects"](str(sch), [{"references": ["U1"], "pins": ["5", "7"]}])
    assert result["ok"] is True and result["count"] == 2, result
    assert {e["pin"]: e["type"] for e in result["added"]} == {"5": "power_in", "7": "input"}
    assert result["silenced_other_erc"]["count"] == 2 and sorted(result["silenced_other_erc"]["pins"]) == ["U1.5 (power_in)", "U1.7 (input)"]
    assert _erc(sch, tmp_path, "pin_not_driven") < before_driven  # the text is true: it did silence one
    plain = tools["add_no_connects"](str(sch), [{"references": ["U1"], "pins": ["9"]}])
    assert plain["silenced_other_erc"]["count"] == 0, plain


def test_two_pins_on_one_point_get_one_marker_and_both_entries_carry_its_uuid(board, tools, monkeypatch):
    """F4: the shared-point branch."""
    real = sheet_editing.pin_positions

    def stub(root, symbol, **kw):
        pins = real(root, symbol, **kw)
        by = {p["number"]: p for p in pins}
        if "3" in by and "4" in by:
            by["4"]["x"], by["4"]["y"] = by["3"]["x"], by["3"]["y"]
        return pins

    monkeypatch.setattr(sheet_editing, "pin_positions", stub)
    monkeypatch.setattr(no_connects, "_erc_check", lambda *a, **k: None)
    result = tools["add_no_connects"](str(board), [{"references": ["J1"], "pins": ["3", "4"]}], dry_run=True)
    assert result["ok"] is True and len(result["would_add"]) == 2, result
    first, second = result["would_add"]
    assert first["uuid"] and first["uuid"] == second["uuid"] and "shared" in second["marker"] and "marker" not in first, result
