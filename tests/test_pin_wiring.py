"""connect_pins, pins win over the grid, and the add_symbols read-back (GitHub issue 86), with real kicad-cli.

The downstream project laid out a sheet of 36 symbols and about 107 wires by raw coordinates and
saw the result only after rendering. These tests wire by pin and read the answer back from
kicad-cli's netlist: each requested pair must be on one net and nothing else merged, for rotated,
mirrored and multi-unit parts and for pins that sit off the 1.27 mm grid. Each guard has a mutation
check: with the guard replaced by a no-op the same input is written (or refused by the next guard),
which shows the test sees the guard.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp import kicad_cli, pin_wiring, sheet_editing, write_layer
from kicad_mcp.toolsets import project as project_toolset
from tests.test_sheet_editing import _bytes, _mirror_symbols, _nets, _nodes, _tools


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture
def tools() -> dict:
    return _tools()


def _project(tmp_path, tools, specs, name="pw") -> Path:
    made = project_toolset._project_create(str(tmp_path / name), name)
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"
    return sch


def _r(ref, x, y, rot=0, value="1k"):
    return {"lib_id": "Device:R", "x": x, "y": y, "ref": ref, "value": value, "rotation": rot}


def _groups(sch: Path, groups: list[list[str]]) -> None:
    """Every pin of a group ("R1.2") is on one net, and the groups sit on different nets."""
    nets = _nets(sch)
    where = {node: name for name, nodes in nets.items() for node in nodes}
    seen = {}
    for group in groups:
        names = {where[tuple(p.split(".", 1))] for p in group}
        assert len(names) == 1, (group, names)
        (name,) = names
        assert name not in seen, f"{group} shares net {name} with {seen[name]}"
        seen[name] = group


def _wire_ends(sch: Path) -> list[list[tuple[float, float]]]:
    return [sheet_editing._wire_ends(w) for w in _nodes(sch, "wire")]


# --------------------------------------------------------------------------- the main path


def test_connect_pins_puts_each_pair_on_one_net_for_rotated_mirrored_and_multi_unit_parts(tmp_path, tools):
    specs = [
        _r("R1", 50.8, 50.8), _r("R2", 76.2, 63.5), _r("R3", 101.6, 50.8, 90), _r("R4", 127, 40.64),
        _r("R5", 25.4, 101.6, 90), _r("R6", 50.8, 127, 180), _r("R7", 76.2, 101.6, 270), _r("R8", 101.6, 127),
        {"lib_id": "Amplifier_Operational:LM324", "x": 152.4, "y": 101.6, "ref": "U1", "value": "LM324", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM324", "x": 193.04, "y": 101.6, "ref": "U1", "value": "LM324", "unit": 2},
        _r("R9", 172.72, 116.84), _r("R10", 213.36, 116.84),
    ]
    sch = _project(tmp_path, tools, specs)
    _mirror_symbols(sch, {"R5": "y", "R7": "y", "R8": "x"})
    pairs = [("R1.2", "R2.1"), ("R3.2", "R4.2"), ("R5.1", "R6.2"), ("R7.2", "R8.2"), ("U1.1", "R9.1"), ("U1.7", "R10.1")]
    before = _bytes(sch)
    result = tools["connect_pins"](str(sch), [{"from": a, "to": b} for a, b in pairs])
    assert result["ok"] is True and result["written"] is True and result["count"] == len(pairs), result
    assert _bytes(sch) != before
    _groups(sch, [list(p) for p in pairs] + [["R1.1"], ["R3.1"], ["R4.1"], ["R2.2"], ["R5.2"], ["R6.1"], ["R7.1"], ["R8.1"], ["R9.2"], ["R10.2"]])
    assert result["connectivity"]["changed"] is False
    for rec in result["connections"]:
        assert len(rec["wires"]) == rec["segments"] == len(rec["path"]) - 1
        assert rec["path"][0] == rec["from"]["at"] and rec["path"][-1] == rec["to"]["at"]
    # the path is orthogonal and every wire segment sits in the file
    for ends in _wire_ends(sch):
        (x0, y0), (x1, y1) = ends[0], ends[-1]
        assert abs(x0 - x1) < 1e-6 or abs(y0 - y1) < 1e-6, ends


def test_the_result_names_the_shape_and_leaves_each_pin_along_its_own_direction(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8), _r("R3", 50.8, 76.2), _r("R4", 76.2, 88.9)])
    result = tools["connect_pins"](str(sch), [
        {"from": "R1.1", "to": "R2.1"},                 # both pins face up: a U
        {"from": "R3.2", "to": "R4.1"},                 # down then up into a pin that faces up: a Z
    ])
    assert result["ok"] is True, result
    u, z = result["connections"]
    assert u["shape"] == "U" and u["path"][0] == [50.8, 46.99] and u["path"][1][0] == 50.8 and u["path"][1][1] < 46.99, u
    assert z["shape"] == "Z" and z["path"][1][0] == 50.8 and z["path"][-2][0] == 76.2, z
    _groups(sch, [["R1.1", "R2.1"], ["R3.2", "R4.1"], ["R1.2"], ["R2.2"], ["R3.1"], ["R4.2"]])


def test_a_straight_l_and_the_string_and_object_forms_of_an_endpoint(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 50.8, 76.2), _r("R3", 101.6, 50.8, 90), _r("R4", 127, 40.64)])
    uuid = next(sheet_editing._uuid(s) for s in _nodes(sch, "symbol") if s.find_children("lib_id") and sheet_editing._ref(s) == "R2")
    result = tools["connect_pins"](str(sch), [
        {"from": "R1.2", "to": {"uuid": uuid, "pin": "1"}},
        {"from": {"ref": "R3", "pin": 2}, "to": {"reference": "R4", "pin": "2"}},
    ])
    assert result["ok"] is True, result
    assert [c["shape"] for c in result["connections"]] == ["straight", "L"]
    _groups(sch, [["R1.2", "R2.1"], ["R3.2", "R4.2"], ["R1.1"], ["R2.2"], ["R3.1"], ["R4.1"]])


# --------------------------------------------------------------------------- the grid


def test_pins_off_the_grid_keep_their_exact_points_and_the_result_says_so(tmp_path, tools):
    """R9 at (60, 120) has pins at (60, 116.19) and (60, 123.81), neither a multiple of 1.27 mm."""
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0), _r("R10", 88.9, 120.65)])
    result = tools["connect_pins"](str(sch), [{"from": "R9.1", "to": "R10.1"}])
    assert result["ok"] is True, result
    rec = result["connections"][0]
    assert rec["off_grid_endpoints"] == [{"pin": "R9.1", "at": [60.0, 116.19]}], rec
    ends = [e for ws in _wire_ends(sch) for e in ws]
    assert (60.0, 116.19) in ends and (88.9, 116.84) in ends, ends         # hand arithmetic: 120 - 3.81, 120.65 - 3.81
    assert rec["shape"] == "U" and rec["snapped"], rec                      # the back-run line is the tool's own: on the grid
    line = rec["path"][1][1]
    assert abs(line / 1.27 - round(line / 1.27)) < 1e-6 and rec["snapped"][0]["asked"] != rec["snapped"][0]["now"]
    _groups(sch, [["R9.1", "R10.1"], ["R9.2"], ["R10.2"]])


def test_snap_false_rounds_nothing_the_tool_chooses(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 80.0, 63.5)])
    result = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.1"}], snap=False)
    assert result["ok"] is True and result["grid"] is None, result
    assert result["connections"][0]["snapped"] == []
    _groups(sch, [["R1.2", "R2.1"], ["R1.1"], ["R2.2"]])


def test_via_waypoints_are_snapped_but_a_coordinate_equal_to_a_pin_stays_exact(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0), _r("R10", 88.9, 120.65)])
    # (60, 112.5) shares the pin's x, so x stays 60; y 112.5 is the tool's: snapped to the grid.
    result = tools["connect_pins"](str(sch), [{"from": "R9.1", "to": "R10.1", "via": [[60, 112.5], [88.9, 112.5]]}])
    assert result["ok"] is True, result
    rec = result["connections"][0]
    assert rec["shape"] == "via" and rec["path"][1][0] == 60.0 and rec["path"][1][1] == 113.03, rec
    assert {(s["axis"], s["asked"], s["now"]) for s in rec["snapped"]} == {("y", 112.5, 113.03)}
    _groups(sch, [["R9.1", "R10.1"], ["R9.2"], ["R10.2"]])


# --------------------------------------------------------------------------- refusals


def _two_named_nets(tmp_path, tools):
    """R1.2 on net NETA and R2.2 on net NETB (labels on short stubs); R3 free."""
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8), _r("R3", 101.6, 50.8)])
    assert tools["add_wires"](str(sch), [
        {"p1": [50.8, 54.61], "p2": [50.8, 59.69], "route": "direct", "snap": False},
        {"p1": [76.2, 54.61], "p2": [76.2, 59.69], "route": "direct", "snap": False}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [
        {"kind": "label", "x": 50.8, "y": 59.69, "text": "NETA", "snap": False},
        {"kind": "label", "x": 76.2, "y": 59.69, "text": "NETB", "snap": False}])["status"] == "ok"
    assert {"/NETA", "/NETB"} <= set(_nets(sch))
    return sch


def test_a_wire_that_would_join_two_named_nets_is_refused_and_writes_nothing(tmp_path, tools):
    sch = _two_named_nets(tmp_path, tools)
    before = _bytes(sch)
    result = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.2"}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert result["connectivity"]["changes"]["items"][0]["type"] == "named_nets_joined"
    assert result["connectivity"]["changes"]["items"][0]["nets"] == ["/NETA", "/NETB"]
    assert _bytes(sch) == before


def test_the_flag_allows_the_join_and_the_result_lists_it(tmp_path, tools):
    sch = _two_named_nets(tmp_path, tools)
    result = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.2"}], allow_connectivity_change=True)
    assert result["ok"] is True and result["written"] is True and result["connectivity"]["changed"] is True, result
    where = {n: name for name, nodes in _nets(sch).items() for n in nodes}
    assert where[("R1", "2")] == where[("R2", "2")]


def test_mutation_without_the_connectivity_guard_the_join_is_written(tmp_path, tools, monkeypatch):
    sch = _two_named_nets(tmp_path, tools)
    monkeypatch.setattr(pin_wiring, "_compare", lambda made, net_of: (lambda before, after, removed: []))
    result = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.2"}])
    assert result["ok"] is True and result["written"] is True, result            # the guard is what refused it above
    where = {n: name for name, nodes in _nets(sch).items() for n in nodes}
    assert where[("R1", "2")] == where[("R2", "2")]


def _blocked(tmp_path, tools):
    """R1.2 (50.8, 54.61) and R2.1 (50.8, 71.12 - 3.81 = 67.31) are aligned; R3's pin 1 at (50.8, 62.23) sits between them."""
    return _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R3", 50.8, 66.04, 0), _r("R2", 50.8, 71.12)])


def test_a_pin_in_the_way_is_named_and_the_call_is_refused(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 50.8, 87.63), _r("R3", 50.8, 69.85)])
    # R1.2 at (50.8, 54.61) to R2.1 at (50.8, 83.82): R3 stands between with pins at 66.04 and 73.66 on the same line
    before = _bytes(sch)
    result = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.1"}])
    assert result["ok"] is False and result["code"] == "no_clear_route", result
    assert "R3.1" in result["reason"] or "R3.2" in result["reason"]
    assert {b["what"] for b in result["blockers"]} >= {"pin R3.1", "pin R3.2"}
    assert _bytes(sch) == before


def test_mutation_without_the_route_check_the_wire_is_drawn_over_the_pin(tmp_path, tools, monkeypatch):
    """The route check is what refuses the pass-through. Measured with kicad-cli: a wire that runs over a pin or a
    label without ending there joins nothing, so the netlist cannot catch it; the sheet would show a wire over a pin
    that a later junction would join to the net."""
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 50.8, 87.63), _r("R3", 50.8, 69.85)])
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 50.8, "y": 66.04, "text": "MID", "snap": False}])["status"] == "ok"
    monkeypatch.setattr(pin_wiring.World, "blockers", lambda self, *a, **k: [])
    result = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.1"}])
    assert result["ok"] is True and result["written"] is True, result
    assert any(abs(ws[0][0] - 50.8) < 1e-6 and ws[0][1] < 66.04 < ws[-1][1] for ws in _wire_ends(sch)), _wire_ends(sch)
    assert _nets(sch)["/MID"] == frozenset({("R3", "1")})


def test_a_wire_in_the_way_is_refused_but_a_crossing_is_not(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 76.2)])
    assert tools["add_wires"](str(sch), [
        {"p1": [40.64, 60.96], "p2": [88.9, 60.96], "route": "direct", "snap": False},      # a free horizontal wire
    ])["status"] == "ok"
    ok = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.1", "via": [[50.8, 72.39], [76.2, 72.39]]}], dry_run=True)
    assert ok["ok"] is True and ok["would_connect"][0]["crosses_wires"], ok         # crosses the free wire at (50.8, 60.96)
    touch = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.1", "via": [[50.8, 60.96], [76.2, 60.96]]}])
    assert touch["ok"] is False and touch["code"] == "no_clear_route" and "wire" in touch["reason"], touch


def test_pins_facing_the_wrong_way_for_a_short_path_give_no_route_and_via_is_the_way_out(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R3", 101.6, 50.8, 90)])
    result = tools["connect_pins"](str(sch), [{"from": "R3.1", "to": "R1.1"}])
    assert result["ok"] is False and result["code"] == "no_route", result
    via = tools["connect_pins"](str(sch), [{"from": "R3.1", "to": "R1.1", "via": [[97.79, 41.91], [50.8, 41.91]]}])
    assert via["ok"] is True, via
    _groups(sch, [["R3.1", "R1.1"], ["R1.2"], ["R3.2"]])
    bad = tools["connect_pins"](str(sch), [{"from": "R3.2", "to": "R1.2", "via": [[110.49, 60.96]]}])
    assert bad["ok"] is False and bad["code"] == "via_not_orthogonal", bad


def test_pin_resolution_refusals(tmp_path, tools):
    specs = [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8),
             {"lib_id": "Amplifier_Operational:LM324", "x": 127, "y": 101.6, "ref": "U1", "value": "LM324", "unit": 1},
             {"lib_id": "Amplifier_Operational:LM324", "x": 167.64, "y": 101.6, "ref": "U1", "value": "LM324", "unit": 2},
             _r("R?", 25.4, 25.4)]
    sch = _project(tmp_path, tools, specs)
    before = _bytes(sch)

    def call(a, b, **kw):
        return tools["connect_pins"](str(sch), [{"from": a, "to": b, **kw}])

    assert call("R1.1", "R9.1")["code"] == "not_found"
    missing = call("R1.1", "R2.7")
    assert missing["code"] == "pin_not_found" and "1 (" in missing["reason"], missing
    ambiguous = call({"ref": "U1", "pin": "+"}, "R1.1")
    assert ambiguous["code"] == "pin_ambiguous" and {c["unit"] for c in ambiguous["candidates"]} == {1, 2}, ambiguous
    assert call({"ref": "U1", "pin": "+", "unit": 1}, "R1.1", via=[[0, 0]])["code"] != "pin_ambiguous"
    assert call({"ref": "U1", "pin": "+", "unit": 3}, "R1.1")["code"] == "pin_not_found"
    assert call("R1.1", "R1.1")["code"] == "invalid_spec"
    assert call("R1", "R2.1")["code"] == "invalid_spec"
    assert call({"ref": "R1", "uuid": "x", "pin": "1"}, "R2.1")["code"] == "invalid_spec"
    assert call("R1.1", "R2.1", color="red")["code"] == "invalid_spec"
    assert call("R?.1", "R2.1")["code"] == "unannotated_symbol"
    assert tools["connect_pins"](str(sch), [])["code"] == "invalid_spec"
    assert tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1"}], grid=0)["code"] == "invalid_spec"
    assert tools["connect_pins"]("/no/such.kicad_sch", [{"from": "R1.1", "to": "R2.1"}])["code"] == "file_not_found"
    assert _bytes(sch) == before


# --------------------------------------------------------------------------- the rest of the contract


def test_a_pair_already_on_one_net_is_not_drawn_again_and_nothing_is_written(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8)])
    first = tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1"}])
    assert first["ok"] is True and first["written"] is True
    before = _bytes(sch)
    again = tools["connect_pins"](str(sch), [{"from": "R2.1", "to": "R1.1"}])
    assert again["ok"] is True and again["written"] is False and again["count"] == 0, again
    assert again["already_connected"][0]["reason"] == "already on one net" and _bytes(sch) == before
    # a three-way join in one call: the third pair is implied by the first two
    sch2 = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8), _r("R3", 101.6, 50.8)], name="pw2")
    three = tools["connect_pins"](str(sch2), [{"from": "R1.1", "to": "R2.1"}, {"from": "R2.1", "to": "R3.1"}, {"from": "R1.1", "to": "R3.1"}])
    assert three["ok"] is True and three["count"] == 2 and "earlier connection" in three["already_connected"][0]["reason"], three
    _groups(sch2, [["R1.1", "R2.1", "R3.1"], ["R1.2"], ["R2.2"], ["R3.2"]])


def test_dry_run_plans_and_checks_but_writes_nothing(tmp_path, tools):
    sch = _two_named_nets(tmp_path, tools)
    before = _bytes(sch)
    plan = tools["connect_pins"](str(sch), [{"from": "R3.1", "to": "R1.1"}], dry_run=True)
    assert plan["ok"] is True and plan["written"] is False and plan["would_connect"][0]["wires"], plan
    assert _bytes(sch) == before
    refused = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.2"}], dry_run=True)
    assert refused["code"] == "connectivity_changed" and _bytes(sch) == before


def test_net_label_names_the_net(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8)])
    result = tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1", "net_label": "SIG"}])
    assert result["ok"] is True and result["connections"][0]["net_label"]["text"] == "SIG", result
    assert _nets(sch)["/SIG"] == frozenset({("R1", "1"), ("R2", "1")})
    # a label that names an existing net would merge it with the new wire
    clash = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.2", "net_label": "SIG"}])
    assert clash["code"] == "connectivity_changed" and any(c["type"] == "merged" for c in clash["connectivity"]["changes"]["items"]), clash


def test_junction_candidates_name_a_point_where_three_things_meet(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8), _r("R3", 101.6, 50.8)])
    result = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.2"}, {"from": "R1.2", "to": "R3.2"}])
    assert result["ok"] is True, result
    assert result["junction_candidates"] == [[76.2, 55.88]], result["junction_candidates"]


def test_the_netlist_self_check_fires_on_a_wire_that_misses_its_pin(tmp_path, tools, monkeypatch):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8)])
    real = pin_wiring.wire_node

    def off(a, b):
        return real(a, (b[0] + 0.254, b[1]))

    monkeypatch.setattr(pin_wiring, "wire_node", off)
    before = _bytes(sch)
    result = tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1"}], allow_connectivity_change=True)
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["pairs"], result
    monkeypatch.undo()
    assert _bytes(sch) == before


def test_connect_pins_corrupted_write_is_refused(tmp_path, tools, monkeypatch):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8)])
    real = write_layer.load
    before = _bytes(sch)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sch) == before


def test_the_staged_check_fires_when_the_tool_adds_a_node_it_did_not_report(tmp_path, tools, monkeypatch):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8)])
    real = sheet_editing.Outcome

    class Sink(set):
        def add(self, item):  # the tool reports nothing it added
            return None

    class Quiet(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            self.added = Sink()

    monkeypatch.setattr(sheet_editing, "Outcome", Quiet)
    before = _bytes(sch)
    result = tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "is new" in result["reason"], result
    monkeypatch.undo()
    assert _bytes(sch) == before


def test_connect_pins_is_registered_as_a_writing_tool():
    from mcp.server.mcpserver import MCPServer

    from kicad_mcp.toolsets import schematic as schematic_toolset

    mcp = MCPServer("test")
    names = schematic_toolset.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "connect_pins")  # type: ignore[attr-defined]
    assert "connect_pins" in names and tool.annotations.read_only_hint is False
    assert "file_open_in_kicad" in tool.description and "pin_ambiguous" in tool.description


# --------------------------------------------------------------------------- pins win over the grid in add_wires / add_labels


def test_add_wires_with_the_default_snap_lands_on_an_off_grid_pin(tmp_path, tools):
    """Before issue 86 the default snap moved a wire end that sat exactly on an off-grid pin off the pin."""
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0), _r("R10", 88.9, 120.65)])
    result = tools["add_wires"](str(sch), [{"p1": [60.0, 116.19], "p2": [88.9, 116.84]}])
    assert result["status"] == "ok", result
    ends = [e for ws in _wire_ends(sch) for e in ws]
    assert (60.0, 116.19) in ends and (88.9, 116.84) in ends, ends
    assert result["landed_on_pin"] == [] and result["snapped"] == []
    _groups(sch, [["R9.1", "R10.1"], ["R9.2"], ["R10.2"]])


def test_add_wires_snaps_a_free_point_and_a_point_near_a_pin_lands_on_the_pin(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0), _r("R10", 88.9, 120.65)])
    result = tools["add_wires"](str(sch), [{"p1": [60.004, 116.186], "p2": [88.3, 116.9], "route": "direct"},
                                          {"p1": [60.0, 123.81], "p2": [60.0, 130.0], "route": "direct"}])
    assert result["status"] == "ok", result
    assert result["landed_on_pin"][0] == {"spec": 0, "point": "p1", "asked": [60.004, 116.186], "now": [60.0, 116.19]}, result
    snapped = {(s["spec"], s["point"]): s for s in result["snapped"]}
    assert snapped[(0, "p2")]["now"] == [88.9, 116.84] or snapped[(0, "p2")]["now"][0] == 88.9, snapped
    assert (59.69, 129.54) in [e for ws in _wire_ends(sch) for e in ws]          # 130 rounds to 129.54 (102 * 1.27)
    assert result["grid"] == 1.27


def test_add_wires_snap_false_is_unchanged(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0)])
    result = tools["add_wires"](str(sch), [{"p1": [60.0, 116.19], "p2": [70.13, 116.19], "snap": False}])
    assert result["status"] == "ok" and result["snapped"] == [] and result["landed_on_pin"] == []
    assert (70.13, 116.19) in [e for ws in _wire_ends(sch) for e in ws]


def test_add_labels_with_the_default_snap_lands_on_an_off_grid_pin(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0)])
    result = tools["add_labels"](str(sch), [{"kind": "label", "x": 60.0, "y": 116.19, "text": "PINNET"},
                                           {"kind": "label", "x": 20.0, "y": 20.0, "text": "FREE"}])
    assert result["status"] == "ok", result
    assert result["snapped"] == [{"spec": 1, "asked": [20.0, 20.0], "now": [20.32, 20.32]}], result
    at = {n.get_string(0): sheet_editing._at(n)[:2] for n in _nodes(sch, "label")}
    assert at["PINNET"] == (60.0, 116.19) and at["FREE"] == (20.32, 20.32)
    assert _nets(sch)["/PINNET"] == frozenset({("R9", "1")})


# --------------------------------------------------------------------------- add_symbols read-back


def test_add_symbols_lists_every_placed_symbol_and_unit_with_its_pin_points(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "rb"), "rb")
    sch = Path(made["schematic_path"])
    specs = [_r("R1", 50.8, 50.8, 90), _r("R2", 60.0, 120.0),
             {"lib_id": "Amplifier_Operational:LM324", "x": 127, "y": 101.6, "ref": "U1", "value": "LM324", "unit": 1},
             {"lib_id": "Amplifier_Operational:LM324", "x": 167.64, "y": 101.6, "ref": "U1", "value": "LM324", "unit": 2},
             {"lib_id": "Amplifier_Operational:LM324", "x": 208.28, "y": 101.6, "ref": "U1", "value": "LM324", "unit": 5}]
    result = tools["add_symbols"](str(sch), specs)
    assert result["status"] == "ok" and result["placed"] == ["R1", "R2", "U1", "U1", "U1"], result
    assert result["count"] == 5 and len(result["symbols"]) == 5
    assert [(s["reference"], s["unit"]) for s in result["symbols"]] == [("R1", 1), ("R2", 1), ("U1", 1), ("U1", 2), ("U1", 5)]
    again = tools["get_symbol_pins"](str(sch), ["*"])
    want = {s["uuid"]: s for s in again["symbols"]}
    for got in result["symbols"]:
        ref = want[got["uuid"]]
        assert got["pins"] == [{k: p[k] for k in ("number", "name", "x", "y")} for p in ref["pins"]]
        assert got["at"] == ref["at"] and got["rotation"] == ref["rotation"] and got["pin_count"] == len(got["pins"])
    r2 = result["symbols"][1]
    assert r2["off_grid_pins"] == ["1", "2"] and result["symbols"][0]["off_grid_pins"] == []
    assert result["grid"] == 1.27 and "snap" in result["grid_note"]


def test_add_symbols_snap_true_rounds_the_origin_and_says_so(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "rb2"), "rb2")
    sch = Path(made["schematic_path"])
    result = tools["add_symbols"](str(sch), [{**_r("R1", 60.0, 120.0), "snap": True}, _r("R2", 60.0, 140.0)])
    assert result["status"] == "ok", result
    first, second = result["symbols"]
    assert first["at"] == [59.69, 119.38] and first["asked"] == [60.0, 120.0] and first["snapped"] is True, first
    assert second["at"] == [60.0, 140.0] and second["snapped"] is False and second["off_grid_pins"], second


# --------------------------------------------------------------------------- review round: asked per spec, snap false, connectors, wire guards


def test_add_symbols_asked_and_snapped_belong_to_their_own_spec_when_a_reference_repeats(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "rb3"), "rb3")
    sch = Path(made["schematic_path"])
    lm = "Amplifier_Operational:LM324"
    result = tools["add_symbols"](str(sch), [
        {"lib_id": lm, "x": 101.6, "y": 76.2, "ref": "U1", "value": "LM324", "unit": 1},
        {"lib_id": lm, "x": 101.6, "y": 114.3, "ref": "U1", "value": "LM324", "unit": 2, "snap": True},
        {"lib_id": lm, "x": 60.0, "y": 140.0, "ref": "U1", "value": "LM324", "unit": 3, "snap": True},
    ])
    assert result["status"] == "ok", result
    one, two, three = result["symbols"]
    assert (one["unit"], one["asked"], one["at"], one["snapped"]) == (1, [101.6, 76.2], [101.6, 76.2], False), one
    assert (two["unit"], two["asked"], two["snapped"]) == (2, [101.6, 114.3], False), two      # already on the grid
    assert (three["unit"], three["asked"], three["snapped"]) == (3, [60.0, 140.0], True), three
    assert three["at"] == [59.69, 139.7], three
    assert [s["ref"] for s in result["snapped"]] == ["U1"] and result["snapped"][0]["spec"] == 2, result["snapped"]


def test_snap_false_moves_nothing_even_next_to_a_pin(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0), _r("R10", 88.9, 120.65)])
    result = tools["add_wires"](str(sch), [{"p1": [60.008, 116.182], "p2": [70.0, 116.186], "route": "direct", "snap": False}])
    assert result["status"] == "ok" and result["snapped"] == [] and result["landed_on_pin"] == [], result
    # the file keeps 0.01 mm: (60.01, 116.18) is the asked point, 0.01 mm from the pin (60, 116.19), not the pin
    assert (60.01, 116.18) in [e for ws in _wire_ends(sch) for e in ws]
    labelled = tools["add_labels"](str(sch), [{"kind": "label", "x": 60.008, "y": 123.802, "text": "T", "snap": False}])
    assert labelled["snapped"] == [] and labelled["landed_on_pin"] == [], labelled
    assert [(round(n.find_children("at")[0].get_float(0), 3), round(n.find_children("at")[0].get_float(1), 3))
            for n in _nodes(sch, "label")] == [(60.01, 123.8)]


def test_a_free_point_far_from_every_connector_is_still_snapped_by_default(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0)])
    result = tools["add_wires"](str(sch), [{"p1": [150.0, 40.0], "p2": [160.0, 40.0], "route": "direct"}])
    assert result["status"] == "ok" and len(result["snapped"]) == 2 and result["landed_on_pin"] == [], result
    assert (149.86, 39.37) in [e for ws in _wire_ends(sch) for e in ws]


def test_the_default_snap_keeps_a_wire_end_on_an_off_grid_label_junction_or_wire_end(tmp_path, tools):
    """A point that sits on something already on the sheet stays there: the connection is the point."""
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0)])
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 100.5, "y": 50.2, "text": "LBL", "snap": False}])["status"] == "ok"
    assert tools["add_junctions"](str(sch), [[110.3, 60.1]])["status"] == "ok"
    assert tools["add_wires"](str(sch), [{"p1": [130.7, 30.3], "p2": [140.2, 30.3], "route": "direct", "snap": False}])["status"] == "ok"
    result = tools["add_wires"](str(sch), [
        {"p1": [100.5, 50.2], "p2": [100.5, 80.0], "route": "direct"},           # from the label
        {"p1": [110.3, 60.1], "p2": [120.0, 60.1], "route": "direct"},           # from the junction
        {"p1": [140.2, 30.3], "p2": [140.2, 20.0], "route": "direct"},           # from the end of the free wire
    ])
    assert result["status"] == "ok", result
    ends = [e for ws in _wire_ends(sch) for e in ws]
    for point in [(100.5, 50.2), (110.3, 60.1), (140.2, 30.3)]:
        assert point in ends, (point, ends)
    assert result["snapped"] and all(s["point"] == "p2" for s in result["snapped"]), result["snapped"]
    again = tools["add_labels"](str(sch), [{"kind": "label", "x": 110.3, "y": 60.1, "text": "J"}])
    assert again["snapped"] == [] and again["landed_on_pin"] == [], again


def test_connector_points_include_sheet_pins_and_every_other_kind_of_connector(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R9", 60.0, 120.0)])
    assert tools["create_sheets"](str(sch), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    made = tools["add_sheet_symbols"](str(sch), [{"file": "sub.kicad_sch", "x": 152.4, "y": 50.8, "pins": [{"name": "IN", "direction": "input"}]}])
    assert made["status"] == "ok", made
    assert tools["add_labels"](str(sch), [{"kind": "no_connect", "x": 20.3, "y": 20.3, "snap": False}])["status"] == "ok"
    root = write_layer.load(sch)
    sheet_pin = next(sheet_editing._at(p)[:2] for s in sheet_editing._kids(root, "sheet") for p in sheet_editing._kids(s, "pin"))
    points = pin_wiring.connector_points(root)
    assert sheet_pin in [tuple(p) for p in points] and (20.3, 20.3) in points and (60.0, 116.19) in points
    near = (sheet_pin[0] + 0.004, sheet_pin[1] - 0.004)
    assert pin_wiring.land(near, points, 1.27, True) == (sheet_pin, "pin")
    assert pin_wiring.land(near, points, 1.27, False) == (near, "unchanged")


def _u_pair(tmp_path, tools):
    """R1.1 and R2.1 both face up: the first route is a U whose line is y0; returns (sheet, path of that route)."""
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8)])
    plan = tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1"}], dry_run=True)
    assert plan["ok"] is True and plan["would_connect"][0]["shape"] == "U", plan
    return sch, plan["would_connect"][0]["path"]


def _path_again(tools, sch):
    again = tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1"}], dry_run=True)
    assert again["ok"] is True, again
    return again["would_connect"][0]["path"]


def test_an_existing_wire_lying_along_the_route_makes_the_tool_take_another_line(tmp_path, tools):
    sch, first = _u_pair(tmp_path, tools)
    y0 = first[1][1]
    assert tools["add_wires"](str(sch), [{"p1": [58.42, y0], "p2": [68.58, y0], "route": "direct", "snap": False}])["status"] == "ok"
    now = _path_again(tools, sch)
    assert now[1][1] != y0 and now != first, (now, first)


def test_an_existing_wire_ending_on_the_route_makes_the_tool_take_another_line(tmp_path, tools):
    sch, first = _u_pair(tmp_path, tools)
    y0 = first[1][1]
    assert tools["add_wires"](str(sch), [{"p1": [63.5, y0 - 5.08], "p2": [63.5, y0], "route": "direct", "snap": False}])["status"] == "ok"
    now = _path_again(tools, sch)
    assert now[1][1] != y0 and now != first, (now, first)


def test_a_route_corner_lying_on_an_existing_diagonal_wire_makes_the_tool_take_another_line(tmp_path, tools):
    sch, first = _u_pair(tmp_path, tools)
    cx, cy = first[2]
    assert tools["add_wires"](str(sch), [{"p1": [cx - 5.08, cy - 5.08], "p2": [cx + 5.08, cy + 5.08], "route": "direct", "snap": False}])["status"] == "ok"
    now = _path_again(tools, sch)
    assert now[2] != first[2], (now, first)


def test_connect_pins_refuses_while_kicad_holds_a_lock_file(tmp_path, tools):
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 76.2, 50.8)])
    lock = sch.with_name(f"~{sch.name}.lck")
    lock.write_bytes(b'{"hostname":"other","username":"x"}')
    before = _bytes(sch)
    result = tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1"}])
    assert result["ok"] is False and result["code"] == "file_open_in_kicad", result
    assert _bytes(sch) == {**before}


def test_the_description_names_the_side_by_side_case_where_via_is_the_way(tmp_path, tools):
    from mcp.server.mcpserver import MCPServer

    from kicad_mcp.toolsets import schematic as schematic_toolset

    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "connect_pins")  # type: ignore[attr-defined]
    assert "side by side" in tool.description and "no_route" in tool.description and "via" in tool.description


def test_an_existing_wire_running_through_both_pins_blocks_the_straight_route(tmp_path, tools):
    """Neither wire end lies on the route and both route ends are the pins, so only the overlap check sees it."""
    sch = _project(tmp_path, tools, [_r("R1", 50.8, 50.8), _r("R2", 50.8, 76.2)])
    clear = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.1"}], dry_run=True)
    assert clear["ok"] is True and clear["would_connect"][0]["shape"] == "straight", clear
    assert tools["add_wires"](str(sch), [{"p1": [50.8, 40.64], "p2": [50.8, 86.36], "route": "direct", "snap": False}])["status"] == "ok"
    blocked = tools["connect_pins"](str(sch), [{"from": "R1.2", "to": "R2.1"}], dry_run=True)
    assert not (blocked["ok"] and blocked["would_connect"][0]["shape"] == "straight"), blocked


def test_blockers_report_a_wire_lying_along_a_segment_with_both_ends_beyond_it():
    """Only the overlap check sees this: the wire's ends lie off the segment and the segment's corners are the exempt
    pin points. Planning trims a covering wire attached at a pin before it asks, so this is the direct check."""
    world = pin_wiring.World.__new__(pin_wiring.World)
    world.connectors = []
    world.wires = [("w1", (50.8, 40.64), (50.8, 86.36))]
    found = world.blockers([(50.8, 54.61), (50.8, 72.39)], (50.8, 54.61), (50.8, 72.39))
    assert [item["type"] for item in found] == ["wire_overlap"], found
