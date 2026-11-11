"""check_connectivity and hidden power_in pins of ordinary symbols (GitHub issue 118).

KiCad joins an invisible ``power_in`` pin of an ordinary symbol to the net of the same name, without
a wire (``SCH_PIN::IsGlobalPower`` is a ``power_in`` pin that is not visible;
``CONNECTION_GRAPH::generateGlobalPowerPinSubGraphs`` gives it the net name of its shown pin name,
"legacy invisible + power-in pins on non-power symbols", and skips a pin that is wired up: "ERC will
warn about this: user has wired up an invisible pin"; eeschema/connection_graph.cpp and
eeschema/sch_pin.cpp of the KiCad source, read 2026-11-05).

Reproduction (before the fix): a sheet with two 74LS00 power units (unit 5, pins GND and VCC, hidden
through the low-level S-expression API because no standard library symbol of KiCad 10.0 has a hidden
``power_in`` pin outside the power library) and a ground symbol wired to a resistor. kicad-cli 10.0.7
ERC reports no ``pin_not_connected`` for the hidden pins and the netlist joins U1.7, U2.7 and R1.2 in
GND; ``check_connectivity`` listed all four hidden pins as unconnected.

Measured with kicad-cli (the cases of ``test_the_reader_agrees_with_erc``): a hidden pin counts as
connected when its name-net has another member besides itself: another hidden pin of that name, a
power symbol of that value whose pin is attached, or a label of that text. A power symbol with
nothing on its pin, or a lone hidden pin, joins nothing and ERC flags the pin.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp, parse_string
from mcp.server.mcpserver import MCPServer

from boardproof import connectivity_check, kicad_cli, sheet_editing, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

# --------------------------------------------------------------------------- raw reader, no kicad-cli


def _sheet(extra: str = "", *, hide: str = "(hide yes)", second: bool = False, name: str = "VCC", ptype: str = "power_in",
          power_value: str | None = None, power_local: bool = False, wired_power: bool = True):
    """A sheet with U1 (one pin at the symbol origin) and optionally U2 with the same pin, a power
    symbol of ``power_value`` and ``extra`` raw items."""
    lib = (
        f'(symbol "L:U" (pin {ptype} line (at 0 0 0) (length 2.54) {hide} (name "{name}") (number "1")))'
        ' (symbol "power:P" (power' + (" local" if power_local else "") + ') (pin power_in line (at 0 0 0) (length 0)'
        ' (name "P") (number "1")))'
    )
    parts = [f'(lib_symbols {lib})', '(symbol (lib_id "L:U") (at 10 10 0) (property "Reference" "U1") (property "Value" "x"))']
    if second:
        parts.append('(symbol (lib_id "L:U") (at 10 30 0) (property "Reference" "U2") (property "Value" "x"))')
    if power_value is not None:
        parts.append(f'(symbol (lib_id "power:P") (at 50 50 0) (property "Reference" "#PWR1") (property "Value" "{power_value}"))')
        if wired_power:
            parts.append("(wire (pts (xy 50 50) (xy 50 60)))")
    parts.append(extra)
    return parse_string("(kicad_sch (version 20250114) " + " ".join(parts) + ")")


def _open(root, **kw) -> set[tuple[str, str]]:
    found, marked = connectivity_check.unconnected_pins(root, "", [], **kw)
    assert marked == []
    return {(p["reference"], p["pin_number"]) for p in found}


def test_a_lone_hidden_power_pin_is_unconnected():
    assert _open(_sheet()) == {("U1", "1")}


def test_two_hidden_power_pins_of_one_name_join_each_other():
    assert _open(_sheet(second=True)) == set()


def test_hidden_power_pins_of_different_names_do_not_join():
    other = parse_string(
        '(kicad_sch (lib_symbols (symbol "L:U" (pin power_in line (at 0 0 0) (length 2.54) (hide yes) (name "A") (number "1")))'
        ' (symbol "L:V" (pin power_in line (at 0 0 0) (length 2.54) (hide yes) (name "B") (number "1"))))'
        ' (symbol (lib_id "L:U") (at 10 10 0) (property "Reference" "U1") (property "Value" "x"))'
        ' (symbol (lib_id "L:V") (at 10 30 0) (property "Reference" "U2") (property "Value" "x")))'
    )
    assert _open(other) == {("U1", "1"), ("U2", "1")}


def test_an_attached_power_symbol_of_that_value_joins_the_pin():
    assert _open(_sheet(power_value="VCC")) == set()


def test_a_power_symbol_with_nothing_on_its_pin_joins_nothing():
    assert _open(_sheet(power_value="VCC", wired_power=False)) == {("U1", "1")}


def test_a_power_symbol_of_another_value_joins_nothing():
    assert _open(_sheet(power_value="GND")) == {("U1", "1")}


def test_a_local_power_symbol_does_not_join_a_global_power_pin():
    assert _open(_sheet(power_value="VCC", power_local=True)) == {("U1", "1")}


def test_a_label_of_that_text_on_the_same_sheet_joins_the_pin():
    assert _open(_sheet('(label "VCC" (at 90 90 0))')) == set()
    assert _open(_sheet('(label "OTHER" (at 90 90 0))')) == {("U1", "1")}


def test_a_label_joins_the_pin_on_a_child_sheet_too():
    root = _sheet('(label "VCC" (at 90 90 0))')
    found, _ = connectivity_check.unconnected_pins(root, "child.kicad_sch", [])
    assert found == []


def test_a_visible_power_pin_does_not_join_by_name():
    assert _open(_sheet(hide="", second=True)) == {("U1", "1"), ("U2", "1")}


def test_a_hidden_pin_that_is_not_power_in_does_not_join_by_name():
    assert _open(_sheet(ptype="passive", second=True)) == {("U1", "1"), ("U2", "1")}


def test_a_pin_with_a_no_connect_marker_is_no_member_of_the_net():
    """A design choice, not measured: the marked pin says "left open", so it gives its name-net no partner."""
    found, marked = connectivity_check.unconnected_pins(_sheet("(no_connect (at 10 30))", second=True), "", [])
    assert [(p["reference"], p["pin_number"]) for p in found] == [("U1", "1")]
    assert [(p["reference"], p["via"]) for p in marked] == [("U2", "no_connect_marker")]


def test_a_nameless_hidden_pin_joins_nothing():
    for name in ("~", ""):
        assert _open(_sheet(second=True, name=name)) == {("U1", "1"), ("U2", "1")}


def test_hide_no_and_the_bare_hide_atom():
    assert _open(_sheet(hide="(hide no)", second=True)) == {("U1", "1"), ("U2", "1")}
    assert _open(_sheet(hide="hide", second=True)) == set()


def test_a_wired_hidden_pin_is_connected_and_still_joins_its_name_net():
    """Measured with kicad-cli 10.0.7: ERC accepts the unwired U2 pin when U1's pin of that name carries
    a wire (see test_the_reader_agrees_with_erc_across_sheets), although KiCad's source comment says a
    wired invisible pin is skipped."""
    assert _open(_sheet("(wire (pts (xy 10 10) (xy 20 10)))")) == set()
    assert _open(_sheet("(wire (pts (xy 10 10) (xy 20 10)))", second=True)) == set()


def test_a_hidden_pin_with_a_no_connect_marker_is_listed_as_marked():
    found, marked = connectivity_check.unconnected_pins(_sheet("(no_connect (at 10 10))"), "", [])
    assert found == [] and [m["via"] for m in marked] == ["no_connect_marker"]


def test_global_names_counts_the_members_of_one_sheet():
    one = connectivity_check.Points(_sheet(power_value="VCC")).global_names("")
    assert one["VCC"] == 2  # the hidden pin and the attached power symbol
    two = connectivity_check.Points(_sheet(second=True, extra='(global_label "VCC" (at 90 90 0))')).global_names("child.kicad_sch")
    assert two["VCC"] == 3  # two hidden pins and a global label
    # a plain label counts on a child sheet as on the root (ERC, measured)
    local = connectivity_check.Points(_sheet('(label "VCC" (at 1 1 0))')).global_names("child.kicad_sch")
    assert local["VCC"] == 2


# --------------------------------------------------------------------------- against kicad-cli


@pytest.fixture
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture(scope="module")
def tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _wire(p1, p2):
    return {"p1": list(p1), "p2": list(p2), "route": "direct", "snap": False}


def _hide_power_pins(sch: Path, lib_id: str) -> None:
    """Make the power_in pins of the embedded library symbol hidden (low-level S-expression API)."""
    root = write_layer.load(sch)
    for table in root.find_children("lib_symbols"):
        for lib in table.find_children("symbol"):
            if lib.get_string(0) != lib_id:
                continue
            for unit in lib.find_children("symbol"):
                for pin in unit.find_children("pin"):
                    if pin.children[0].value == "power_in":
                        pin.children.append(SExp.list("hide", write_layer.symbol("yes")))
    write_layer.save(root, sch)


def _gate(unit: int, ref: str, x: float) -> dict:
    return {"lib_id": "74xx:74LS00", "x": x, "y": 60, "ref": ref, "value": "74LS00", "unit": unit}


def _build(tmp_path: Path, tools: dict, specs: list[dict], after=None) -> Path:
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"
    _hide_power_pins(sch, "74xx:74LS00")
    if after is not None:
        after(sch)
    return sch


def _pin(tools, sch: Path, ref: str, number: str) -> list[float]:
    for pin in tools["get_symbol_pins"](str(sch), [ref])["symbols"][0]["pins"]:
        if pin["number"] == number:
            return [pin["x"], pin["y"]]
    raise AssertionError(f"{ref}.{number}")


def _erc_open(sch: Path, tmp_path: Path) -> set[tuple[str, str]]:
    out = tmp_path / "erc.json"
    subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "erc", "--format", "json", "-o", str(out), str(sch)],
                   check=True, capture_output=True)
    flagged: set[tuple[str, str]] = set()
    for sh in json.loads(out.read_text(encoding="utf-8"))["sheets"]:
        for violation in sh["violations"]:
            if violation["type"] == "pin_not_connected":
                for item in violation["items"]:
                    if not item["description"].startswith("Symbol "):
                        continue  # the unused sheet pin of the child sheet symbol
                    found = re.search(r"Symbol (\S+) (?:\S+ )?Pin (\S+)", item["description"])
                    assert found, item
                    flagged.add((found.group(1), found.group(2)))
    # the power symbols' own pins are flagged by ERC and skipped by check_connectivity (issue 109)
    return {pin for pin in flagged if not pin[0].startswith("#PWR")}


def _tool_open(tools: dict, sch: Path, tmp_path: Path) -> set[tuple[str, str]]:
    copy = tmp_path / "copy"
    shutil.copytree(sch.parent, copy)  # the netlist cross-check writes beside its input
    got = tools["check_connectivity"](str(copy / sch.name))
    assert got["status"] == "ok", got
    return {(p["reference"], p["pin_number"]) for p in got["unconnected_pins"]}


@pytest.mark.parametrize("case", ["pair_with_ground_symbol", "lone", "label"])
def test_the_reader_agrees_with_erc(case, tools, tmp_path, _need_kicad_cli):
    specs = {
        "pair_with_ground_symbol": [_gate(5, "U1", 60), _gate(5, "U2", 100), {"lib_id": "Device:R", "x": 140, "y": 100, "ref": "R1", "value": "1k"},
                                    {"lib_id": "power:GND", "x": 140, "y": 130, "ref": "#PWR01", "value": "GND"}],
        "lone": [_gate(5, "U1", 60)],
        "label": [_gate(5, "U1", 60)],
    }[case]

    def after(sch: Path) -> None:
        if case == "pair_with_ground_symbol":
            r2, gnd = _pin(tools, sch, "R1", "2"), _pin(tools, sch, "#PWR01", "1")
            assert tools["add_wires"](str(sch), [_wire(r2, gnd)])["status"] == "ok"
            assert tools["add_labels"](str(sch), [{"kind": "no_connect", "x": _pin(tools, sch, "R1", "1")[0],
                                                   "y": _pin(tools, sch, "R1", "1")[1], "snap": False}])["status"] == "ok"
        if case == "label":
            assert tools["add_labels"](str(sch), [{"kind": "label", "x": 120, "y": 120, "text": "GND", "snap": False}])["status"] == "ok"

    sch = _build(tmp_path, tools, specs, after)
    erc = _erc_open(sch, tmp_path)
    mine = _tool_open(tools, sch, tmp_path)
    assert mine == erc, (mine, erc)
    expected = {
        "pair_with_ground_symbol": set(),
        "lone": {("U1", "7"), ("U1", "14")},
        "label": {("U1", "14")},
    }[case]
    assert erc == expected  # the sheets say what the unit tests above rely on


def test_the_members_are_counted_per_sheet_as_erc_does(tools, tmp_path, _need_kicad_cli):
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    root = Path(made["schematic_path"])
    assert tools["create_sheets"](str(root), [{"file": "child.kicad_sch"}])["status"] == "ok"
    child = root.parent / "child.kicad_sch"
    assert tools["add_symbols"](str(root), [_gate(5, "U1", 60)])["status"] == "ok"
    placed = tools["add_sheet_symbols"](str(root), [{"file": "child.kicad_sch", "name": "Child", "x": 152.4, "y": 38.1,
                                                   "width": 25.4, "height": 25.4,
                                                   "pins": [{"name": "SIG", "direction": "input", "side": "left"}]}])
    assert placed["status"] == "ok", placed
    assert tools["add_symbols"](str(child), [_gate(5, "U2", 60)])["status"] == "ok"
    for path in (root, child):
        _hide_power_pins(path, "74xx:74LS00")
    erc = _erc_open(root, tmp_path)
    mine = _tool_open(tools, root, tmp_path)
    assert mine == erc, (mine, erc)
    # ERC counts the members of one sheet: each lone hidden pin is flagged, although the netlist
    # joins U1.7 and U2.7 in GND across the two sheets.
    assert erc == {("U1", "7"), ("U1", "14"), ("U2", "7"), ("U2", "14")}
    assert sheet_editing.nets_from(root)["GND"] >= {("U1", "7"), ("U2", "7")}


def _label_item(text: str) -> dict:
    kind, _, text = text.rpartition(":")  # "global:GND" is a global label, "GND" a plain one
    return {"kind": kind or "label", "x": 120, "y": 120, "text": text, "snap": False}


def _stub_from(tools: dict, sch: Path) -> None:
    pin = _pin(tools, sch, "U1", "7")
    assert tools["add_wires"](str(sch), [_wire(pin, [pin[0], pin[1] + 5.08])])["status"] == "ok"


def _to_resistor(tools: dict, sch: Path) -> None:
    pin, target = _pin(tools, sch, "U1", "7"), _pin(tools, sch, "R1", "1")
    assert tools["add_wires"](str(sch), [_wire(pin, [pin[0], target[1]]), _wire([pin[0], target[1]], target)])["status"] == "ok"


_RESISTOR = {"lib_id": "Device:R", "x": 140, "y": 100, "ref": "R1", "value": "1k"}
# name: (root symbols, child symbols or None, extra on the root, extra on the child, what ERC flags, measured with kicad-cli 10.0.7)
_ACROSS = {
    "wired_stub": ([_gate(5, "U1", 60), _gate(5, "U2", 100)], None, "stub", None, set()),
    "wired_to_resistor": ([_gate(5, "U1", 60), _gate(5, "U2", 100), _RESISTOR], None, "to_r", None, {("R1", "2")}),
    "child_label_child_pins": ([_gate(5, "U1", 60)], [_gate(5, "U2", 60)], None, "GND", {("U1", "7"), ("U1", "14"), ("U2", "14")}),
    "child_label_lone_child_pin": ([], [_gate(5, "U2", 60)], None, "GND", {("U2", "14")}),
    "root_label_lone_child_pin": ([], [_gate(5, "U2", 60)], "GND", None, {("U2", "7"), ("U2", "14")}),
    "global_label_same_sheet": ([_gate(5, "U1", 60)], None, "global:GND", None, {("U1", "14")}),
    "global_label_child_vs_root_pin": ([_gate(5, "U1", 60)], [], None, "global:GND", {("U1", "7"), ("U1", "14")}),
    "child_label_lone_root_pin": ([_gate(5, "U1", 60)], [], None, "GND", {("U1", "7"), ("U1", "14")}),
}


@pytest.mark.parametrize("case", list(_ACROSS))
def test_the_reader_agrees_with_erc_across_sheets(case, tools, tmp_path, _need_kicad_cli):
    root_symbols, child_symbols, root_extra, child_extra, expected = _ACROSS[case]
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    root = Path(made["schematic_path"])
    child = root.parent / "child.kicad_sch"
    if child_symbols is not None:
        assert tools["create_sheets"](str(root), [{"file": "child.kicad_sch"}])["status"] == "ok"
    if root_symbols:
        assert tools["add_symbols"](str(root), root_symbols)["status"] == "ok"
    if child_symbols is not None:
        placed = tools["add_sheet_symbols"](str(root), [{"file": "child.kicad_sch", "name": "Child", "x": 152.4, "y": 38.1,
                                                       "width": 25.4, "height": 25.4,
                                                       "pins": [{"name": "SIG", "direction": "input", "side": "left"}]}])
        assert placed["status"] == "ok", placed
        if child_symbols:
            assert tools["add_symbols"](str(child), child_symbols)["status"] == "ok"
    for path in (root, child):
        if path.exists():
            _hide_power_pins(path, "74xx:74LS00")
    if root_extra == "stub":
        _stub_from(tools, root)
    elif root_extra == "to_r":
        _to_resistor(tools, root)
    elif root_extra:
        assert tools["add_labels"](str(root), [_label_item(root_extra)])["status"] == "ok"
    if child_extra:
        assert tools["add_labels"](str(child), [_label_item(child_extra)])["status"] == "ok"
    erc = _erc_open(root, tmp_path)
    mine = _tool_open(tools, root, tmp_path)
    assert erc == expected, erc
    assert mine == erc, (mine, erc)
