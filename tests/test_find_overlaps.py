"""find_overlaps (GitHub issue 95), read-only, with real kicad-cli for the one claim it makes about KiCad.

One shared project (built through the tools, off-grid on purpose where it matters) holds each case in its
own corner of the sheet. Boxes are cross-checked against the raw ``(at ...)`` and the raw library
rectangle and pins of the sheet file, never against a kicad_tools property.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_authoring, sheet_editing, sheet_overlaps, write_layer
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
def sheet(tmp_path_factory) -> Path:
    tools = _tools()
    made = project_toolset._project_create(str(tmp_path_factory.mktemp("overlaps") / "proj"), "demo")
    sch = Path(made["schematic_path"])

    def symbol(ref: str, x: float, y: float, lib: str = "Device:R") -> dict:
        return {"lib_id": lib, "x": x, "y": y, "ref": ref, "value": "1k"}

    assert tools["add_symbols"](str(sch), [
        symbol("R1", 50.8, 50.8), symbol("R2", 52.07, 50.8),        # bodies overlap
        symbol("R3", 101.6, 50.8), symbol("R4", 103.632, 50.8),     # boxes only touch
        symbol("R5", 152.4, 50.8), {"lib_id": "power:GND", "x": 152.4, "y": 50.8, "ref": "#PWR01"},  # power on a body
        symbol("R6", 50.8, 101.6), symbol("R7", 101.6, 101.6), symbol("R8", 152.4, 101.6),
        symbol("R9", 200.66, 50.8),
    ])["status"] == "ok"
    assert tools["add_wires"](str(sch), [
        {"p1": [45.72, 97.79], "p2": [55.88, 97.79], "route": "direct", "snap": False},     # through R6 pin 1
        {"p1": [96.52, 97.79], "p2": [106.68, 97.79], "route": "direct", "snap": False},    # through R7 pin 1, junction
        {"p1": [147.32, 97.79], "p2": [152.4, 97.79], "route": "direct", "snap": False},    # ends on R8 pin 1
        {"p1": [15.24, 129.54], "p2": [35.56, 129.54], "route": "direct", "snap": False},   # under label CROSS
        {"p1": [60.96, 152.4], "p2": [71.12, 152.4], "route": "direct", "snap": False},     # the wire label OKLBL sits on
        {"p1": [60.96, 160.02], "p2": [66.04, 157.48], "route": "direct", "snap": False},   # a slanted wire from label DIAG through its text
        {"p1": [22.0, 149.5], "p2": [22.0, 160.0], "route": "direct", "snap": False},       # clips label CLIP by 0.25 mm (below the threshold)
        {"p1": [42.0, 149.0], "p2": [42.0, 160.0], "route": "direct", "snap": False},       # clips label CLOP by 0.75 mm (above it)
        {"p1": [15.24, 138.73], "p2": [35.56, 138.73], "route": "direct", "snap": False},   # along the top edge of label EDGE only
    ])["status"] == "ok"
    assert tools["add_junctions"](str(sch), [[101.6, 97.79]])["status"] == "ok"
    labels = [("THRU", 45.72, 97.79), ("JOIN", 96.52, 97.79), ("END", 147.32, 97.79), ("NET_A", 30.48, 71.12),
              ("CROSS", 20.32, 130.0), ("OKLBL", 60.96, 152.4), ("ONPIN", 152.4, 54.61),
              ("DIAG", 60.96, 160.02), ("EDGE", 20.32, 140.0), ("CLIP", 20.32, 150.0), ("CLOP", 40.32, 150.0)]
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": x, "y": y, "text": t, "snap": False} for t, x, y in labels])["status"] == "ok"
    assert tools["add_text"](str(sch), [{"x": 30.99, "y": 70.9, "text": "NOTE_X"},
                                        {"x": 200.0, "y": 51.5, "text": "ONBODY"},
                                        {"x": 152.4, "y": 52.0, "text": "OVERGND"}], snap=False)["status"] == "ok"
    return sch


@pytest.fixture(scope="module")
def result(sheet) -> dict:
    out = _tools()["find_overlaps"](str(sheet))
    assert out["status"] == "ok", out
    return out


def _of(result: dict, kind: str) -> list[dict]:
    return [f for f in result["findings"] if f["kind"] == kind]


def _refs(finding: dict) -> set[str]:
    return {i.get("reference") or i.get("text") or i.get("name") or i["kind"] for i in finding["items"]}


def _node(sheet: Path, tag: str, ref: str):
    root = write_layer.load(sheet)
    return next(s for s in root.children if not s.is_atom and s.name == tag and sheet_editing._ref(s) == ref)


# --------------------------------------------------------------------------- symbols


def test_overlapping_bodies_are_found_and_the_box_is_the_raw_geometry(sheet, result):
    found = [f for f in _of(result, "symbol_symbol") if _refs(f) == {"R1", "R2"}]
    assert len(found) == 1
    box = next(i["box"] for i in found[0]["items"] if i.get("reference") == "R1")
    # raw: (at 50.8 50.8), library rectangle x +-1.016, y +-2.54, pins (0, +-3.81) of length 1.27
    at = sheet_editing._at(_node(sheet, "symbol", "R1"))
    assert box == pytest.approx([at[0] - 1.016, at[1] - 3.81, at[0] + 1.016, at[1] + 3.81])
    assert found[0]["overlap"][0] == pytest.approx(51.816 - 51.054)
    assert found[0]["estimated"] is False


def test_boxes_that_only_touch_and_a_power_symbol_on_a_body_are_not_overlaps(result):
    pairs = [_refs(f) for f in _of(result, "symbol_symbol")]
    assert {"R3", "R4"} not in pairs
    assert not [p for p in pairs if {"R5"} <= p], pairs  # the GND symbol sits on R5 by design
    assert pairs == [{"R1", "R2"}]


# --------------------------------------------------------------------------- wires through pins


def test_a_wire_running_over_a_pin_is_found_but_not_one_that_ends_there_or_has_a_junction(sheet, result):
    through = _of(result, "wire_through_pin")
    assert [_refs(f) - {"wire"} for f in through] == [{"R6"}]
    assert through[0]["pin"] == pytest.approx([50.8, 97.79])


def test_kicad_joins_a_pin_only_to_a_wire_end_or_a_junction(sheet):
    """The claim behind wire_through_pin, measured: the net named by a label at the far end of each wire."""
    tree = sheet_authoring._require_tree(sheet)
    nets = sheet_editing.read_nets(tree, sheet, None)
    # the wire (and its label THRU) holds no pin, and R6 pin 1 is alone on its own net
    assert not nets.get("/THRU")
    assert [members for members in nets.values() if ("R6", "1") in members] == [frozenset({("R6", "1")})]
    assert ("R7", "1") in nets["/JOIN"]
    assert ("R8", "1") in nets["/END"]


# --------------------------------------------------------------------------- texts


def test_texts_on_texts_and_on_bodies_and_wires_are_found_as_estimates(result):
    assert [f for f in _of(result, "text_text") if _refs(f) == {"NET_A", "NOTE_X"}]
    over = [f for f in _of(result, "text_symbol") if _refs(f) == {"ONBODY", "R9"}]
    assert len(over) == 1 and over[0]["estimated"] is True
    crossed = [f for f in _of(result, "text_wire") if "CROSS" in _refs(f)]
    assert len(crossed) == 1 and crossed[0]["estimated"] is True and crossed[0]["length_inside"] > 0.5


def test_a_text_over_a_power_symbol_is_not_reported_against_it(result):
    """The tool text says power symbols are not compared: OVERGND lies over the GND symbol on R5 (and so over R5)."""
    over = [f for f in _of(result, "text_symbol") if "OVERGND" in _refs(f)]
    assert over and all("#PWR01" not in _refs(f) for f in over)


def test_a_wire_clipping_a_text_box_counts_only_beyond_half_a_millimetre(result):
    crossed = {t for f in _of(result, "text_wire") for t in _refs(f)}
    assert "CLOP" in crossed and "CLIP" not in crossed
    clop = next(f for f in _of(result, "text_wire") if "CLOP" in _refs(f))
    assert 0.5 < clop["length_inside"] < 1.0


def test_a_label_is_not_reported_against_the_wire_it_sits_on_and_a_property_not_against_its_own_part(result):
    assert not [f for f in _of(result, "text_symbol") if "ONPIN" in _refs(f)]  # a label on a pin of the part
    assert not [i for f in result["findings"] for i in f["items"] if str(i.get("property", "")).startswith("ki_")]
    assert not [f for f in _of(result, "text_wire") if _refs(f) & {"OKLBL", "END", "THRU", "DIAG", "EDGE"}]
    for f in _of(result, "text_symbol"):
        for item in f["items"]:
            if item["kind"] == "property":
                owner = next(i for i in f["items"] if i["kind"] == "symbol")
                assert item["owner_uuid"] != owner["uuid"], f


# --------------------------------------------------------------------------- the tool


def test_the_result_is_counted_whole_and_nothing_is_written(sheet, result):
    before = sheet.read_bytes()
    out = _tools()["find_overlaps"](str(sheet), kinds=["symbol_symbol", "wire_through_pin"], max_findings=1)
    assert sheet.read_bytes() == before
    assert out["written"] is False and out["truncated"] is True and len(out["findings"]) == 1
    assert out["counts"] == {"symbol_symbol": 1, "wire_through_pin": 1} and out["found"] == 2
    assert result["found"] == sum(result["counts"].values()) and result["truncated"] is False


@pytest.mark.parametrize("args", [{"kinds": []}, {"kinds": ["nope"]}, {"kinds": ["text_text", "text_text"]}, {"kinds": "text_text"},
                                  {"max_findings": 0}, {"max_findings": True}, {"max_findings": 5001}])
def test_bad_arguments_are_refused(sheet, args):
    out = _tools()["find_overlaps"](str(sheet), **args)
    assert out["ok"] is False and out["code"] == "invalid_spec"


def test_a_missing_sheet_is_refused(tmp_path):
    assert _tools()["find_overlaps"](str(tmp_path / "none.kicad_sch"))["code"] == "file_not_found"


def test_hierarchical_sheet_symbols_are_boxes_from_the_raw_at_and_size(tmp_path):
    tools = _tools()
    sch = Path(project_toolset._project_create(str(tmp_path / "h"), "h")["schematic_path"])
    assert tools["create_sheets"](str(sch), [{"file": "a.kicad_sch"}, {"file": "b.kicad_sch"}])["status"] == "ok"
    pin = [{"name": "X", "direction": "input"}]
    assert tools["add_sheet_symbols"](str(sch), [
        {"file": "a.kicad_sch", "x": 50.8, "y": 50.8, "pins": pin, "name": "A"},
        {"file": "b.kicad_sch", "x": 60.96, "y": 53.34, "pins": pin, "name": "B"}])["status"] == "ok"
    out = tools["find_overlaps"](str(sch), kinds=["symbol_symbol"])
    assert out["status"] == "ok" and out["sheets_read"] == 3
    root = write_layer.load(sch)
    nodes = {sheet_editing._property(n, "Sheetname").get_string(1): n for n in root.children if not n.is_atom and n.name == "sheet"}
    a = nodes["A"]
    ax, ay, _ = sheet_editing._at(a)
    size = a.find_children("size")[0]
    box = next(i["box"] for f in out["findings"] for i in f["items"] if i.get("name") == "A")
    assert box == pytest.approx([ax, ay, ax + size.get_float(0), ay + size.get_float(1)])
    assert out["found"] == 1


def test_the_module_lists_exactly_the_kinds_the_tool_text_names():
    import inspect

    doc = inspect.getdoc(_tools()["find_overlaps"]) or ""
    for kind in sheet_overlaps.KINDS:
        assert kind in doc
