"""check_connectivity against kicad-cli (GitHub issue 109), with real kicad-cli.

The downstream project replaced two connector placeholders by an 8-pin part, marked pins 4 to 7 with
no-connect markers, and got ERC 0 violations and a clean netlist. ``check_connectivity`` still
reported the four marked pins of each connector as unconnected, power symbols of its project library
as ``missing_from_netlist`` and ``possible_floating_wire`` at wire ends that sit on pins.

Reproduction (before the fix, on the sheet below): ``unconnected_pins`` held the marked C1.2 and
R4.2, the pin-to-pin touch R1.2/R3.1 and the label-on-pin C2 pins; ``connection_issues`` held a
floating wire at every wire end on a pin or sheet pin; ``missing_from_netlist`` held the renamed
ground symbol. The sheet is built only through the tools; the one raw edit renames the library of a
power symbol, as a project library does (``power:GND`` -> ``mylib:GND``), through the low-level
S-expression API.

Each case also has a verdict from kicad-cli: ``test_sheet_has_no_erc_error_for_the_clean_cases``
shows ERC and the netlist agree with the tool's reading, so the tool is what was wrong, not the sheet.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import connectivity_check, kicad_cli, sheet_editing, write_layer
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


@pytest.fixture(scope="module")
def tools() -> dict:
    return _tools()


def _wire(p1, p2):
    return {"p1": list(p1), "p2": list(p2), "route": "direct", "snap": False}


def _label(text, x, y, kind="label"):
    return {"kind": kind, "x": x, "y": y, "text": text, "snap": False}


def _pin(tools, sch: Path, ref: str, number: str) -> tuple[float, float]:
    """A pin position from the tool that reads the raw (at ...) nodes of the symbol and of the pin."""
    got = tools["get_symbol_pins"](str(sch), [ref])
    for pin in got["symbols"][0]["pins"]:
        if pin["number"] == number:
            return pin["x"], pin["y"]
    raise AssertionError(f"{ref}.{number} not found")


def _rename_power_library(sch: Path, old: str, new: str) -> None:
    """Move the library symbol ``old`` to another library name, as a project library holds its power
    symbols; the embedded symbol keeps its (power) flag."""
    root = write_layer.load(sch)
    for node in root.children:
        if node.is_atom:
            continue
        if node.name == "symbol" and node.find_children("lib_id") and node.find_children("lib_id")[0].get_string(0) == old:
            node.find_children("lib_id")[0].children[0] = write_layer.string(new)
        if node.name == "lib_symbols":
            for entry in node.children:
                if not entry.is_atom and entry.name == "symbol" and entry.get_string(0) == old:
                    entry.children[0] = write_layer.string(new)
    write_layer.save(root, sch)


@pytest.fixture(scope="module")
def sheet(tmp_path_factory, tools) -> Path:
    tmp_path = tmp_path_factory.mktemp("cc")
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["create_sheets"](str(sch), [{"file": "child.kicad_sch"}])["status"] == "ok"
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"},
        {"lib_id": "Device:R", "x": 50.8, "y": 58.42, "ref": "R3", "value": "10k"},  # R3.1 sits on R1.2
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"},
        {"lib_id": "Device:C", "x": 101.6, "y": 50.8, "ref": "C1", "value": "1u"},
        {"lib_id": "Device:C", "x": 127.0, "y": 50.8, "ref": "C2", "value": "1u"},
        {"lib_id": "Device:R", "x": 152.4, "y": 50.8, "ref": "R4", "value": "1k"},
        {"lib_id": "power:GND", "x": 76.2, "y": 71.12, "ref": "#PWR01", "value": "GND"},
    ])["status"] == "ok"
    r2_top, r2_bottom = _pin(tools, sch, "R2", "1"), _pin(tools, sch, "R2", "2")
    c1_bottom = _pin(tools, sch, "C1", "2")
    c2_top, c2_bottom = _pin(tools, sch, "C2", "1"), _pin(tools, sch, "C2", "2")
    r4_top, r4_bottom = _pin(tools, sch, "R4", "1"), _pin(tools, sch, "R4", "2")
    gnd = _pin(tools, sch, "#PWR01", "1")
    wires = [
        # R2.1: a wire end on a pin; the far end carries a label
        _wire(r2_top, (r2_top[0], 40.64)), _wire((r2_top[0], 40.64), (88.9, 40.64)),
        # R2.2 down to the ground symbol, the label GNDX on the stub
        _wire(r2_bottom, (r2_bottom[0], 62.23)), _wire((r2_bottom[0], 62.23), gnd),
        # a free wire: both ends touch nothing
        _wire((152.4, 30.48), (165.1, 30.48)),
        # a T without a junction: the end of the second wire lies on the middle of the first
        _wire((170.18, 71.12), (190.5, 71.12)), _wire((180.34, 60.96), (180.34, 71.12)),
        # a wire passing over R4.1 without ending there
        _wire((139.7, r4_top[1]), (165.1, r4_top[1])),
    ]
    assert tools["add_wires"](str(sch), wires)["status"] == "ok"
    assert tools["add_labels"](str(sch), [
        _label("TOPNET", 88.9, 40.64),
        _label("GNDX", r2_bottom[0], 62.23),
        _label("CONNA", *c2_top), _label("CONNB", *c2_bottom),  # labels straight on the pins, no wire
        _label("L1", 170.18, 71.12), _label("L2", 190.5, 71.12), _label("L3", 180.34, 60.96),
        _label("P1", 139.7, r4_top[1]), _label("P2", 165.1, r4_top[1]),
        {"kind": "no_connect", "x": c1_bottom[0], "y": c1_bottom[1], "snap": False},
        {"kind": "no_connect", "x": r4_bottom[0], "y": r4_bottom[1], "snap": False},
    ])["status"] == "ok"
    # the sheet symbol, a wire end on its sheet pin
    placed = tools["add_sheet_symbols"](str(sch), [
        {"file": "child.kicad_sch", "name": "Child", "x": 203.2, "y": 40.64, "width": 25.4, "height": 25.4,
         "pins": [{"name": "SIG", "direction": "bidirectional", "side": "left"}]}])
    assert placed["status"] == "ok", placed
    sheet_pin = sheet_editing._at(next(p for s in write_layer.load(sch).children if not s.is_atom and s.name == "sheet"
                                       for p in s.find_children("pin")))[:2]
    assert tools["add_wires"](str(sch), [_wire((190.5, sheet_pin[1] + 2.54), sheet_pin)])["status"] == "ok"
    assert tools["add_labels"](str(sch), [_label("TOCHILD", 190.5, sheet_pin[1] + 2.54)])["status"] == "ok"
    _rename_power_library(sch, "power:GND", "mylib:GND")
    return sch


@pytest.fixture(scope="module")
def result(sheet, tools, tmp_path_factory) -> dict:
    copy = tmp_path_factory.mktemp("run") / "proj"
    shutil.copytree(sheet.parent, copy)  # the netlist cross-check writes beside its input
    got = tools["check_connectivity"](str(copy / sheet.name))
    assert got["status"] == "ok", got
    return got


EXPECTED_OPEN = {("R1", "1"), ("R3", "2"), ("C1", "1"), ("R4", "1")}


def _pins(result: dict, key="unconnected_pins") -> set[tuple[str, str]]:
    return {(p["reference"], p["pin_number"]) for p in result[key]}


def _issue_points(result: dict) -> set[tuple[float, float]]:
    return {(round(i["position"][0], 2), round(i["position"][1], 2)) for i in result["connection_issues"]}


# --------------------------------------------------------------------------- ground truth


def test_kicad_cli_agrees_with_the_clean_cases(sheet, tmp_path):
    """The sheet is right where the tool said it was not: ERC has no pin_not_connected for the marked,
    touching or labelled pins, no dangling wire at a pin, and the netlist lacks the power symbol."""
    copy = tmp_path / "p"
    shutil.copytree(sheet.parent, copy)
    out = tmp_path / "erc.json"
    cli = kicad_cli.find_kicad_cli()
    subprocess.run([str(cli), "sch", "erc", "--format", "json", "-o", str(out), str(copy / sheet.name)],
                   check=True, capture_output=True)
    report = json.loads(out.read_text(encoding="utf-8"))
    flagged: set[tuple[str, str]] = set()
    for sh in report["sheets"]:
        for violation in sh["violations"]:
            if violation["type"] == "pin_not_connected":
                for item in violation["items"]:
                    found = re.search(r"Symbol (\S+) Pin (\S+)", item["description"])
                    assert found, item
                    flagged.add((found.group(1), found.group(2)))
    # ERC flags exactly the pins that are really open, among them R4.1 (a wire over it joins nothing);
    # it does not flag the marked C1.2 and R4.2, the touching R1.2 and R3.1, nor the labelled C2 pins.
    assert flagged == EXPECTED_OPEN
    nets = sheet_editing.nets_from(copy / sheet.name)
    members = {ref for nodes in nets.values() for ref, _pin in nodes}
    assert not any(ref.startswith("#PWR") for ref in members)
    joined = next(nodes for nodes in nets.values() if ("R1", "2") in nodes)
    assert ("R3", "1") in joined  # a pin on a pin is one net


# --------------------------------------------------------------------------- symptom 1: no-connect markers


def test_pins_with_a_no_connect_marker_are_not_unconnected(result):
    assert ("C1", "2") not in _pins(result)
    assert ("R4", "2") not in _pins(result)
    listed = {(p["reference"], p["pin_number"]): p["via"] for p in result["no_connect_pins"]}
    assert listed == {("C1", "2"): "no_connect_marker", ("R4", "2"): "no_connect_marker"}
    assert result["no_connect_count"] == 2


def test_the_unconnected_pins_are_exactly_the_ones_erc_flags(result):
    assert _pins(result) == EXPECTED_OPEN
    assert result["unconnected_count"] == len(EXPECTED_OPEN)


def test_pins_touching_a_pin_or_a_label_are_connected(result):
    found = _pins(result)
    assert ("R1", "2") not in found and ("R3", "1") not in found  # pin on pin
    assert ("C2", "1") not in found and ("C2", "2") not in found  # label on the pin


def test_a_pin_on_the_middle_of_a_wire_is_unconnected(result):
    """The wire runs over R4.1 and ends at labels either side: it joins nothing (issue 86 measured it;
    ERC flags the pin in test_kicad_cli_agrees_with_the_clean_cases). kicad_tools called it connected."""
    assert ("R4", "1") in _pins(result)


def test_a_marker_far_from_a_pin_hides_nothing(sheet, tools, tmp_path):
    """The marker is matched by position: one moved off the pin leaves the pin a finding."""
    copy = tmp_path / "p"
    shutil.copytree(sheet.parent, copy)
    root = write_layer.load(copy / sheet.name)
    for node in root.children:
        if not node.is_atom and node.name == "no_connect":
            at = node.find_children("at")[0]
            at.children[1] = write_layer.SExp.atom(float(at.children[1].value) + 1.27)
    write_layer.save(root, copy / sheet.name)
    got = tools["check_connectivity"](str(copy / sheet.name))
    assert {("C1", "2"), ("R4", "2")} <= _pins(got)
    assert got["no_connect_count"] == 0


def test_pin_type_no_connect_is_left_out_but_listed():
    from kicad_tools.sexp import parse_string

    root = parse_string(
        '(kicad_sch (version 20250114) (lib_symbols (symbol "L:U" (pin no_connect line (at 0 0 0) (length 2.54)'
        ' (name "NC") (number "1")))) (symbol (lib_id "L:U") (at 10 10 0) (unit 1)'
        ' (property "Reference" "U1") (property "Value" "x")))'
    )
    found, marked = connectivity_check.unconnected_pins(root, "", [])
    assert found == []
    assert [(m["reference"], m["via"]) for m in marked] == [("U1", "pin_type_no_connect")]


# --------------------------------------------------------------------------- symptom 2: power symbols


def test_a_project_library_power_symbol_is_not_missing_from_the_netlist(result):
    assert not [m for m in result["missing_from_netlist"] if m["reference"].startswith("#")], result["missing_from_netlist"]


def test_a_part_missing_from_the_netlist_is_still_reported(tmp_path):
    from kicad_tools.sexp import parse_string

    root = parse_string(
        '(kicad_sch (lib_symbols (symbol "L:G" (power) (pin power_in line (at 0 0 0) (length 0) (name "G") (number "1")))'
        ' (symbol "L:U" (pin passive line (at 0 0 0) (length 2.54) (name "A") (number "1"))))'
        ' (symbol (lib_id "L:G") (at 1 1 0) (property "Reference" "#PWR01"))'
        ' (symbol (lib_id "L:U") (at 5 5 0) (property "Reference" "U1")))'
    )
    assert connectivity_check.power_symbol_references(root) == {"#PWR01"}


# --------------------------------------------------------------------------- symptom 3: wire ends


def test_wire_ends_on_pins_and_sheet_pins_are_not_floating(result, sheet, tools):
    points = _issue_points(result)
    assert (88.9, 40.64) not in points  # label
    r2 = _pin(tools, sheet, "R2", "1")
    assert (round(r2[0], 2), round(r2[1], 2)) not in points  # pin
    sheet_node = next(s for s in write_layer.load(sheet).children if not s.is_atom and s.name == "sheet")
    sx, sy, _ = sheet_editing._at(sheet_node.find_children("pin")[0])
    assert (round(sx, 2), round(sy, 2)) not in points  # sheet pin
    assert (round(r2[0], 2), 62.23) not in points  # two wires meet


def test_a_free_wire_is_reported_with_its_reason(result):
    free = [i for i in result["connection_issues"] if i["position"][1] == 30.48]
    assert len(free) == 2
    assert all(connectivity_check.FLOATING_NOTE in i["description"] for i in free)


def test_a_wire_end_on_the_middle_of_a_wire_is_reported_and_says_why(result):
    t = [i for i in result["connection_issues"] if (round(i["position"][0], 2), round(i["position"][1], 2)) == (180.34, 71.12)]
    assert len(t) == 1
    assert connectivity_check.BODY_NOTE in t[0]["description"]
    assert "junction" in t[0]["description"]


def test_the_issue_list_holds_only_the_real_floating_ends(result):
    assert _issue_points(result) == {(152.4, 30.48), (165.1, 30.48), (180.34, 71.12)}


# --------------------------------------------------------------------------- what else attaches a pin


def _one_pin_sheet(extra: str):
    from kicad_tools.sexp import parse_string

    return parse_string(
        '(kicad_sch (version 20250114) (lib_symbols (symbol "L:U" (pin passive line (at 0 0 0) (length 2.54)'
        ' (name "A") (number "1")))) (symbol (lib_id "L:U") (at 10 10 0) (unit 1)'
        f' (property "Reference" "U1") (property "Value" "x")) {extra})'
    )


@pytest.mark.parametrize(
    "extra",
    [
        '(sheet (at 10 10) (size 5 5) (pin "SIG" input (at 10 10 180)))',  # a sheet pin on the pin
        "(junction (at 10 10)) (wire (pts (xy 0 10) (xy 20 10)))",  # a wire passes over the pin, a junction joins it
        '(label "NET" (at 10 10 0))',
        "(wire (pts (xy 10 10) (xy 20 10)))",
    ],
    ids=["sheet_pin", "junction", "label", "wire_end"],
)
def test_each_kind_of_attachment_connects_a_pin(extra):
    found, marked = connectivity_check.unconnected_pins(_one_pin_sheet(extra), "", [])
    assert found == [] and marked == []


def test_a_pin_with_nothing_on_it_is_unconnected():
    found, _marked = connectivity_check.unconnected_pins(_one_pin_sheet(""), "", [])
    assert [(p["reference"], p["pin_number"]) for p in found] == [("U1", "1")]


def test_a_wire_end_on_a_no_connect_marker_is_not_floating():
    from kicad_tools.sexp import parse_string

    wire = "(wire (pts (xy 10 10) (xy 20 10)))"
    marked = parse_string(f"(kicad_sch {wire} (no_connect (at 10 10)) (no_connect (at 20 10)))")
    one = parse_string(f"(kicad_sch {wire} (no_connect (at 10 10)))")
    bare = parse_string(f"(kicad_sch {wire})")
    assert connectivity_check.refine_issues(marked, [], "") == []
    assert [i.position for i in connectivity_check.refine_issues(one, [], "")] == [(20.0, 10.0)]
    assert len(connectivity_check.refine_issues(bare, [], "")) == 2


def test_a_symbol_without_an_embedded_library_symbol_keeps_the_kicad_tools_record():
    from types import SimpleNamespace

    from kicad_tools.sexp import parse_string

    root = parse_string('(kicad_sch (lib_symbols) (symbol (lib_id "L:U") (at 10 20 0) (property "Reference" "U9")))')
    record = SimpleNamespace(reference="U9", pin_number="3", pin_name="", pin_type="", symbol_value="", lib_id="L:U",
                             sheet="")
    found, _ = connectivity_check.unconnected_pins(root, "", [record])
    assert [(p["reference"], p["pin_number"], p["position"]) for p in found] == [("U9", "3", [10.0, 20.0])]


def test_a_lone_junction_on_a_pin_connects_nothing():
    """kicad-cli's ERC reports pin_not_connected for a junction with no wire at it."""
    found, _ = connectivity_check.unconnected_pins(_one_pin_sheet("(junction (at 10 10))"), "", [])
    assert [(p["reference"], p["pin_number"]) for p in found] == [("U1", "1")]


def test_a_wire_over_a_pin_without_a_junction_is_unconnected():
    found, _ = connectivity_check.unconnected_pins(_one_pin_sheet("(wire (pts (xy 0 10) (xy 20 10)))"), "", [])
    assert [(p["reference"], p["pin_number"]) for p in found] == [("U1", "1")]


def test_a_wire_end_on_a_lone_junction_is_not_floating():
    from kicad_tools.sexp import parse_string

    root = parse_string("(kicad_sch (wire (pts (xy 10 10) (xy 20 10))) (junction (at 10 10)) (junction (at 20 10)))")
    assert connectivity_check.refine_issues(root, [], "") == []
    bare = parse_string("(kicad_sch (wire (pts (xy 10 10) (xy 20 10))) (junction (at 10 10)))")
    assert [i.position for i in connectivity_check.refine_issues(bare, [], "")] == [(20.0, 10.0)]


def _open_pin_sheet(lib_id: str, extra: str = ""):
    from kicad_tools.sexp import parse_string

    return parse_string(
        f'(kicad_sch (lib_symbols (symbol "{lib_id}" (pin passive line (at 0 0 0) (length 2.54)'
        f' (name "A") (number "1")))) (symbol (lib_id "{lib_id}") (at 10 10 0) {extra}'
        ' (property "Reference" "U1") (property "Value" "x")))'
    )


def test_a_power_library_symbol_with_open_pins_is_not_listed():
    found, marked = connectivity_check.unconnected_pins(_open_pin_sheet("power:GND"), "", [])
    assert found == [] and marked == []
    found, _ = connectivity_check.unconnected_pins(_open_pin_sheet("L:GND"), "", [])
    assert len(found) == 1


def test_a_do_not_place_symbol_with_open_pins_is_not_listed():
    found, marked = connectivity_check.unconnected_pins(_open_pin_sheet("L:U", "(dnp yes)"), "", [])
    assert found == [] and marked == []
    found, _ = connectivity_check.unconnected_pins(_open_pin_sheet("L:U", "(dnp no)"), "", [])
    assert len(found) == 1
