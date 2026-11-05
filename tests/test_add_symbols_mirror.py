"""add_symbols and move_symbols: mirror support, unknown spec keys refused (GitHub issue 111).

Found in the walk of issue 108: add_symbols silently ignored a ``mirror`` key (kicad_tools' ``add_symbol``
has no mirror parameter) and accepted any unknown key. KiCad 10.0.7 stores ``(mirror x|y)`` right after
``(at ..)`` (read from the sheets it saved); the placed pin is the library pin rotated, then mirrored
(measured against kicad-cli's netlist in tests/test_sheet_editing.py and here; the KiCad source was not read).
``(mirror y)`` negates the sheet x offset, ``(mirror x)`` the sheet y offset. The mirror belongs to one placed
unit, so it is keyed by (reference, unit).

Fixtures are built through the tools and the write layer on ``tmp_path`` (never a raw text edit).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_editing, write_layer
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset

CONN = "Connector_Generic:Conn_01x08"


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def sch(tmp_path) -> Path:
    return Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])


def _spec(ref="J1", x=100.33, y=60.96, **extra) -> dict:
    return {"lib_id": CONN, "x": x, "y": y, "ref": ref, **extra}


def _pins(result_symbol) -> dict:
    return {p["number"]: (p["x"], p["y"]) for p in result_symbol["pins"]}


def _node(sheet: Path, ref: str):
    for c in write_layer.load(sheet).children:
        if not c.is_atom and c.name == "symbol" and c.find_children("lib_id") and sheet_editing._ref(c) == ref:
            return c
    raise AssertionError(ref)


def test_mirror_y_puts_the_pins_on_the_other_side_and_keeps_the_pin_order(tools, sch):
    plain = tools["add_symbols"](str(sch), [_spec("J1")])
    mirrored = tools["add_symbols"](str(sch), [_spec("J2", x=150.0, mirror="y")])
    assert plain["status"] == mirrored["status"] == "ok", (plain, mirrored)
    a, b = plain["symbols"][0], mirrored["symbols"][0]
    pa, pb = _pins(a), _pins(b)
    assert len(pa) == len(pb) == 8
    for number, (x, y) in pa.items():
        # same row, mirrored about the symbol origin (J1 at x 100.33, J2 at x 150.0)
        assert pb[number][1] == pytest.approx(y)
        assert pb[number][0] - 150.0 == pytest.approx(-(x - 100.33), abs=1e-3)
    assert pa["1"][0] < 100.33 < 150.0 < pb["1"][0]  # the pins left of the origin are now right of it
    # pin 1 stays on the row it had: an end of the column (the connector's first pin), not swapped with pin 8
    assert (pb["1"][1] < pb["8"][1]) == (pa["1"][1] < pa["8"][1])
    assert b["mirror"] == "y"
    assert a["mirror"] == ""
    node = _node(sch, "J2")
    assert str(node.find_children("mirror")[0].children[0].value) == "y"
    # the read-back from get_symbol_pins agrees
    got = tools["get_symbol_pins"](str(sch), references=["J2"])["symbols"][0]
    assert got["mirror"] == "y"
    assert {p["number"]: (p["x"], p["y"]) for p in got["pins"]} == pb


def test_mirror_x_negates_the_sheet_y_offset(tools, sch):
    a = tools["add_symbols"](str(sch), [_spec("J1")])["symbols"][0]
    b = tools["add_symbols"](str(sch), [_spec("J2", x=150.0, mirror="x")])["symbols"][0]
    pa, pb = _pins(a), _pins(b)
    for number, (x, y) in pa.items():
        assert pb[number][0] - 150.0 == pytest.approx(x - 100.33, abs=1e-3)
        assert pb[number][1] - 60.96 == pytest.approx(-(y - 60.96), abs=1e-3)


def test_mirror_pins_agree_with_the_netlist_for_rotation_and_mirror(tools, sch):
    """A label on each computed pin point must put that pin on the label's net in kicad-cli's netlist."""
    variants = [(rot, m) for rot in (0, 90, 180, 270) for m in ("x", "y")]
    specs = [_spec(f"J{i + 1}", x=50.8 + 63.5 * (i % 4), y=60.96 + 63.5 * (i // 4), rotation=rot, mirror=m)
             for i, (rot, m) in enumerate(variants)]
    res = tools["add_symbols"](str(sch), specs)
    assert res["status"] == "ok", res
    labels = []
    for entry in res["symbols"]:
        for p in entry["pins"]:
            labels.append({"text": f"N_{entry['reference']}_{p['number']}", "x": p["x"], "y": p["y"], "kind": "label", "snap": False})
    added = tools["add_labels"](str(sch), labels)
    assert added["status"] in ("ok", "partial"), added
    nets = sheet_editing.nets_from(sch)
    for entry in res["symbols"]:
        for p in entry["pins"]:
            want = (entry["reference"], p["number"])
            net = [n for n, members in nets.items() if n.endswith(f"N_{entry['reference']}_{p['number']}")]
            assert net, (entry["reference"], p, list(nets)[:5])
            assert any(want in {(m[0], m[1]) for m in nets[n]} for n in net), (want, net)


def test_the_netlist_is_unchanged_by_placing_a_mirrored_symbol_next_to_existing_ones(tools, sch):
    assert tools["add_symbols"](str(sch), [_spec("J1")])["status"] == "ok"
    before = sheet_editing.nets_from(sch)
    assert tools["add_symbols"](str(sch), [_spec("J2", x=150.0, mirror="y")])["status"] == "ok"
    after = sheet_editing.nets_from(sch)
    assert not sheet_editing.compare_nets(before, after) or all(
        c.get("kind") != "removed" for c in sheet_editing.compare_nets(before, after))


def test_a_mirrored_symbol_drags_its_wires_when_moved(tools, sch):
    res = tools["add_symbols"](str(sch), [_spec("J1", mirror="y")])["symbols"][0]
    pins = _pins(res)
    for number in ("1", "2"):
        x, y = pins[number]
        assert tools["add_wires"](str(sch), [{"p1": [x, y], "p2": [x + 10.16, y], "route": "direct", "snap": False}])["status"] == "ok"
        assert tools["add_labels"](str(sch), [{"text": f"W{number}", "x": x + 10.16, "y": y, "kind": "label", "snap": False}])["status"] in ("ok", "partial")
    before = sheet_editing.nets_from(sch)
    moved = tools["move_symbols"](str(sch), [{"references": ["J1"], "offset": [25.4, 12.7]}])
    assert moved["status"] == "ok", moved
    assert len(moved["followed"]["wires"]) == 2
    new = _pins(moved["moved"][0])
    assert new["1"][0] == pytest.approx(pins["1"][0] + 25.4)
    assert new["1"][1] == pytest.approx(pins["1"][1] + 12.7)
    assert _node(sch, "J1").find_children("mirror"), "the mirror survives the move"
    assert not sheet_editing.compare_nets(before, sheet_editing.nets_from(sch))


@pytest.mark.parametrize("bad", ["z", "xy", 1, True, ["y"]])
def test_an_unknown_mirror_value_is_refused_with_nothing_written(tools, sch, bad):
    before = sch.read_bytes()
    res = tools["add_symbols"](str(sch), [_spec(mirror=bad)])
    assert res.get("ok") is False and res["code"] == "invalid_spec", res
    assert "mirror" in res["reason"]
    assert sch.read_bytes() == before


def test_an_unknown_spec_key_is_refused_with_nothing_written(tools, sch):
    before = sch.read_bytes()
    res = tools["add_symbols"](str(sch), [_spec("J1"), _spec("J2", x=150.0, mirrr="y")])
    assert res.get("ok") is False and res["code"] == "invalid_spec", res
    assert "mirrr" in res["reason"] and "specs[1]" in res["reason"]
    assert sch.read_bytes() == before


def test_a_missing_required_key_or_a_non_object_spec_is_refused_with_nothing_written(tools, sch):
    before = sch.read_bytes()
    for bad in ([{"lib_id": CONN, "x": 1, "y": 2}], ["J1"], [{**_spec(), "x": "1"}]):
        res = tools["add_symbols"](str(sch), bad)
        assert res.get("ok") is False and res["code"] == "invalid_spec", (bad, res)
    assert sch.read_bytes() == before


def test_every_documented_key_is_still_accepted(tools, sch):
    res = tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k", "rotation": 90,
                                           "footprint": "", "unit": 1, "in_bom": True, "dnp": False, "snap": True, "mirror": ""}])
    assert res["status"] == "ok", res
    assert res["symbols"][0]["mirror"] == ""


def test_adding_to_a_sheet_that_already_holds_a_mirrored_symbol_keeps_it(tools, sch):
    assert tools["add_symbols"](str(sch), [_spec("J1", mirror="y")])["status"] == "ok"
    res = tools["add_symbols"](str(sch), [_spec("J2", x=150.0)])
    assert res["status"] == "ok", res
    assert _node(sch, "J1").find_children("mirror")


def test_add_symbols_mirror_self_check_fires_when_the_mirror_is_not_written_and_leaves_the_sheet_unchanged(tools, sch, monkeypatch):
    """Mutation check of the mirror self-check: when the mirror does not reach the text, the write is refused."""
    from kicad_mcp.toolsets import schematic as module

    before = sch.read_bytes()
    monkeypatch.setattr(module, "_apply_mirrors", lambda root, mirrors, old: None)
    res = tools["add_symbols"](str(sch), [_spec("J1", mirror="y")])
    assert res.get("ok") is False and res.get("error") == "write_not_verified", res
    assert res["mismatches"]["items"][0]["subject"] == "J1 unit 1"
    assert sch.read_bytes() == before


LM = "Amplifier_Operational:LM324"


def _unit_spec(ref, unit, x, **extra) -> dict:
    return {"lib_id": LM, "x": x, "y": 101.6, "ref": ref, "value": "LM324", "unit": unit, **extra}


def _mirror_of(sheet: Path, ref: str, unit: int) -> str:
    for c in write_layer.load(sheet).children:
        if not c.is_atom and c.name == "symbol" and c.find_children("lib_id") and sheet_editing._ref(c) == ref:
            u = c.find_children("unit")
            if (int(u[0].get_float(0)) if u else 1) == unit:
                m = c.find_children("mirror")
                return str(m[0].children[0].value) if m else ""
    raise AssertionError((ref, unit))


def test_two_units_of_one_reference_keep_their_own_mirror(tools, sch):
    """B1: the mirror is per placed unit, not per reference (the last spec used to win for every unit)."""
    first = tools["add_symbols"](str(sch), [_unit_spec("U1", 1, 50.8, mirror="y"), _unit_spec("U1", 2, 101.6)])
    assert first["status"] == "ok", first
    assert (_mirror_of(sch, "U1", 1), _mirror_of(sch, "U1", 2)) == ("y", "")
    second = tools["add_symbols"](str(sch), [{**_unit_spec("U2", 1, 50.8), "y": 152.4}, {**_unit_spec("U2", 2, 101.6, mirror="x"), "y": 152.4}])
    assert second["status"] == "ok", second
    assert (_mirror_of(sch, "U2", 1), _mirror_of(sch, "U2", 2)) == ("", "x")
    assert [(e["unit"], e["mirror"]) for e in first["symbols"]] == [(1, "y"), (2, "")]
    assert [(e["unit"], e["mirror"]) for e in second["symbols"]] == [(1, ""), (2, "x")]


def test_a_unit_added_to_a_reference_already_on_the_sheet_leaves_the_old_unit_alone(tools, sch):
    """The old_uuids skip: unit 1 is already there (mirror y); adding unit 2 (none, then x) must not touch it."""
    assert tools["add_symbols"](str(sch), [_unit_spec("U3", 1, 50.8, mirror="y")])["status"] == "ok"
    assert tools["add_symbols"](str(sch), [_unit_spec("U3", 2, 101.6)])["status"] == "ok"
    assert (_mirror_of(sch, "U3", 1), _mirror_of(sch, "U3", 2)) == ("y", "")
    assert tools["add_symbols"](str(sch), [_unit_spec("U3", 3, 152.4, mirror="x")])["status"] == "ok"
    assert [_mirror_of(sch, "U3", u) for u in (1, 2, 3)] == ["y", "", "x"]
    assert tools["add_symbols"](str(sch), [_unit_spec("U4", 1, 50.8)])["status"] == "ok"
    assert tools["add_symbols"](str(sch), [_unit_spec("U4", 2, 101.6, mirror="y")])["status"] == "ok"
    assert (_mirror_of(sch, "U4", 1), _mirror_of(sch, "U4", 2)) == ("", "y")


def test_the_same_reference_and_unit_twice_with_different_mirrors_is_refused(tools, sch):
    before = sch.read_bytes()
    res = tools["add_symbols"](str(sch), [_unit_spec("U1", 1, 50.8, mirror="y"), _unit_spec("U1", 1, 101.6)])
    assert res.get("ok") is False and res["code"] == "invalid_spec", res
    assert sch.read_bytes() == before


def test_the_mirror_node_sits_right_after_the_at_node(tools, sch):
    assert tools["add_symbols"](str(sch), [_spec("J1", mirror="y")])["status"] == "ok"
    node = _node(sch, "J1")
    names = [c.name for c in node.children if not c.is_atom]
    assert names.index("mirror") == names.index("at") + 1, names


@pytest.mark.parametrize("key, bad", [
    ("x", float("nan")), ("y", float("inf")), ("x", float("-inf")),
    ("rotation", 45), ("rotation", "90"), ("rotation", True), ("rotation", float("nan")),
    ("unit", 0), ("unit", -1), ("unit", 1.5), ("unit", "1"), ("unit", True),
])
def test_non_finite_coordinates_and_bad_rotation_or_unit_are_refused_as_invalid_spec(tools, sch, key, bad):
    before = sch.read_bytes()
    res = tools["add_symbols"](str(sch), [_spec(**{key: bad})] if key not in ("x", "y") else [{**_spec(), key: bad}])
    assert res.get("ok") is False and res["code"] == "invalid_spec", res
    assert key in res["reason"]
    assert sch.read_bytes() == before


def test_apply_mirrors_never_touches_a_symbol_that_was_already_on_the_sheet(tools, sch):
    """The old_uuids skip, seen directly: an old symbol with the same (reference, unit) as a new one is left alone."""
    from kicad_mcp.toolsets import schematic as module

    assert tools["add_symbols"](str(sch), [_spec("J1")])["status"] == "ok"
    root = write_layer.load(sch)
    old = {_node_uuid for _node_uuid in [c.find_children("uuid")[0].get_string(0) for c in root.children
                                         if not c.is_atom and c.name == "symbol" and c.find_children("lib_id")]}
    module._apply_mirrors(root, {("J1", 1): "y"}, set(old))
    assert not [c for c in root.children if not c.is_atom and c.name == "symbol" and c.find_children("mirror")]
    module._apply_mirrors(root, {("J1", 1): "y"}, set())
    assert [c for c in root.children if not c.is_atom and c.name == "symbol" and c.find_children("mirror")]
