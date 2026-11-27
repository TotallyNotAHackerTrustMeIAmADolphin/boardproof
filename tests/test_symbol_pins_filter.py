"""get_symbol_pins: the with_connection filter, the compact mode and the short default (GitHub issue 268).

A pin is "attached" when a wire end, a wire span, a junction or a label (local, global, hierarchical)
sits on its point; the geometry is the raw ``(at ...)`` maths of ``sheet_editing.pin_positions``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import sheet_editing, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


def _tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def tools() -> dict:
    return _tools()


def _wire(p1, p2):
    return {"p1": list(p1), "p2": list(p2), "route": "direct", "snap": False}


@pytest.fixture
def sch(tmp_path, tools) -> Path:
    """R1 (50.8,50.8): pin 1 a wire END, pin 2 a label. R2 (76.2,50.8): pin 1 a junction, pin 2 nothing.
    R3 (101.6,50.8): a wire runs OVER pin 1 (span, no end there), pin 2 nothing. R4 turned 90 and mirrored
    about y (127,50.8): pin 1 a wire end. GND power symbol (152.4,76.2): its hidden pin a wire end.
    U1 LM324 units 1 and 2: unit 2 pin 7 (output) a global label, nothing on unit 1."""
    made = project_toolset._project_create(str(tmp_path / "p"), "p")
    path = Path(made["schematic_path"])
    assert tools["add_symbols"](str(path), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"},
        {"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R3", "value": "1k"},
        {"lib_id": "Device:R", "x": 127.0, "y": 50.8, "ref": "R4", "value": "1k", "rotation": 90},
        {"lib_id": "power:GND", "x": 152.4, "y": 76.2, "ref": "#PWR01", "value": "GND"},
        {"lib_id": "Amplifier_Operational:LM324", "x": 50.8, "y": 101.6, "ref": "U1", "value": "LM324", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM324", "x": 101.6, "y": 101.6, "ref": "U1", "value": "LM324", "unit": 2},
    ])["status"] == "ok"
    root = write_layer.load(path)
    r4 = next(c for c in root.children if not c.is_atom and c.name == "symbol" and sheet_editing._ref(c) == "R4")
    at = r4.find_children("at")[0]
    r4.children.insert(r4.children.index(at) + 1, write_layer.SExp.list("mirror", write_layer.symbol("y")))
    write_layer.save(root, path)
    pins = {(e["reference"], e["unit"], p["number"]): (p["x"], p["y"])
            for e in tools["get_symbol_pins"](str(path), ["*"])["symbols"] for p in e["pins"]}
    assert tools["add_wires"](str(path), [
        _wire((pins["R1", 1, "1"][0], pins["R1", 1, "1"][1]), (50.8, 40.64)),  # end on R1.1
        _wire((101.6, 40.64), (101.6, 53.0)),  # runs over R3.1 (46.99), ends short of R3.2 (54.61)
        _wire(pins["R4", 1, "1"], (pins["R4", 1, "1"][0], pins["R4", 1, "1"][1] - 5.08)),
        _wire(pins["#PWR01", 1, "1"], (152.4, 66.04)),
    ])["status"] == "ok"
    assert tools["add_labels"](str(path), [
        {"kind": "label", "x": pins["R1", 1, "2"][0], "y": pins["R1", 1, "2"][1], "text": "NETX", "snap": False},
        {"kind": "global", "x": pins["U1", 2, "7"][0], "y": pins["U1", 2, "7"][1], "text": "OUT", "snap": False},
    ])["status"] == "ok"
    assert tools["add_junctions"](str(path), [list(pins["R2", 1, "1"])])["status"] == "ok"
    return path


def _attached(got) -> dict:
    return {(e["reference"], e["unit"], p["number"]): p["attached"] for e in got["symbols"] for p in e["pins"]}


def test_with_connection_lists_exactly_the_pins_with_a_wire_junction_or_label_on_them(sch, tools):
    got = tools["get_symbol_pins"](str(sch), ["*"], with_connection=True)
    assert got["ok"] is True, got
    assert _attached(got) == {
        ("R1", 1, "1"): ["wire"], ("R1", 1, "2"): ["label"], ("R2", 1, "1"): ["junction"],
        ("R3", 1, "1"): ["wire"],  # a wire span over the pin counts, no end needed
        ("R4", 1, "1"): ["wire"], ("#PWR01", 1, "1"): ["wire"], ("U1", 2, "7"): ["label"],
    }
    assert "not_measured" not in got


def test_without_the_filter_every_pin_is_listed_and_carries_no_attached_field(sch, tools):
    got = tools["get_symbol_pins"](str(sch), ["R2"])
    assert [p["number"] for p in got["symbols"][0]["pins"]] == ["1", "2"]
    assert all("attached" not in p for p in got["symbols"][0]["pins"])


def test_a_turned_and_mirrored_part_has_its_pin_where_the_raw_library_pin_says(sch, tools):
    root = write_layer.load(sch)
    lib = next(c for c in root.children if not c.is_atom and c.name == "lib_symbols")
    r = next(c for c in lib.children if not c.is_atom and c.get_string(0) == "Device:R")
    raw = {}
    for sub in r.children:
        if not sub.is_atom and sub.name == "symbol":
            for pin in sub.find_children("pin"):
                at = pin.find_children("at")[0]
                raw[pin.find_children("number")[0].get_string(0)] = (at.get_float(0), at.get_float(1))
    assert raw["1"] == (0.0, 3.81)
    # turned 90 counter-clockwise: (0, 3.81) -> (-3.81, 0); mirrored about y: x flips -> (3.81, 0); sheet Y is down but y is 0
    got = tools["get_symbol_pins"](str(sch), ["R4"])["symbols"][0]
    assert {p["number"]: (p["x"], p["y"]) for p in got["pins"]} == {"1": (127.0 + 3.81, 50.8), "2": (127.0 - 3.81, 50.8)}


def test_compact_gives_reference_number_x_y_and_nothing_else(sch, tools):
    got = tools["get_symbol_pins"](str(sch), ["R1", "R2"], compact=True)
    assert got["pins"] == [
        {"reference": "R1", "number": "1", "x": 50.8, "y": 46.99}, {"reference": "R1", "number": "2", "x": 50.8, "y": 54.61},
        {"reference": "R2", "number": "1", "x": 76.2, "y": 46.99}, {"reference": "R2", "number": "2", "x": 76.2, "y": 54.61},
    ]
    assert got["pins_count"] == 4 and "symbols" not in got
    both = tools["get_symbol_pins"](str(sch), ["R*"], compact=True, with_connection=True)
    assert [(p["reference"], p["number"]) for p in both["pins"]] == [("R1", "1"), ("R1", "2"), ("R2", "1"), ("R3", "1"), ("R4", "1")]


@pytest.fixture
def big(tmp_path, tools) -> Path:
    made = project_toolset._project_create(str(tmp_path / "big"), "big")
    path = Path(made["schematic_path"])
    specs = [{"lib_id": "Device:R", "x": 12.7 + 12.7 * (i % 10), "y": 25.4 + 15.24 * (i // 10), "ref": f"R{i + 1}", "value": "1k"}
             for i in range(30)]
    assert tools["add_symbols"](str(path), specs)["status"] == "ok"
    return path


def test_a_pattern_selection_is_cut_to_20_without_detail_and_complete_with_it(big, tools):
    short = tools["get_symbol_pins"](str(big), ["R*"])
    assert len(short["symbols"]) == 20 and short["count"] == 30 and short["symbols_truncated"] is True and "detail=true" in short["note"]
    full = tools["get_symbol_pins"](str(big), ["R*"], detail=True)
    assert len(full["symbols"]) == 30 and "symbols_truncated" not in full
    compact = tools["get_symbol_pins"](str(big), ["R*"], compact=True)
    assert len(compact["pins"]) == 20 and compact["pins_count"] == 60 and compact["pins_truncated"] is True
    assert len(tools["get_symbol_pins"](str(big), ["R*"], compact=True, detail=True)["pins"]) == 60


def test_symbols_asked_for_by_name_or_uuid_stay_full_next_to_a_cut_pattern(big, tools):
    names = [f"R{i}" for i in range(25, 31)]  # sorted last, beyond the first 20 of a pattern
    got = tools["get_symbol_pins"](str(big), names)
    assert got["count"] == 6 and len(got["symbols"]) == 6 and "symbols_truncated" not in got
    mixed = tools["get_symbol_pins"](str(big), ["R30", "R*"])
    refs = [e["reference"] for e in mixed["symbols"]]
    assert "R30" in refs and len(refs) == 21 and mixed["count"] == 30 and mixed["symbols_truncated"] is True


def test_the_filter_applies_before_the_cut(big, tools):
    got = tools["get_symbol_pins"](str(big), ["R*"], with_connection=True)
    assert got["symbols"] == [] and got["count"] == 0 and "symbols_truncated" not in got


def test_a_symbol_without_its_library_symbol_is_reported_not_measured_never_as_unattached(sch, tools):
    root = write_layer.load(sch)
    table = next(c for c in root.children if not c.is_atom and c.name == "lib_symbols")
    table.children[:] = [c for c in table.children if c.is_atom or c.get_string(0) != "Device:R"]
    write_layer.save(root, sch)
    got = tools["get_symbol_pins"](str(sch), ["R*"], with_connection=True)
    assert sorted(m["reference"] for m in got["not_measured"]["symbols"]) == ["R1", "R2", "R3", "R4"]
    assert got["symbols"] == []
    c = tools["get_symbol_pins"](str(sch), ["R*"], with_connection=True, compact=True)
    assert len(c["not_measured"]["symbols"]) == 4 and c["pins"] == []


def test_an_unreadable_wire_makes_unattached_pins_not_measured(sch, tools):
    root = write_layer.load(sch)
    wire = next(c for c in root.children if not c.is_atom and c.name == "wire")
    wire.children[:] = [c for c in wire.children if c.is_atom or c.name != "pts"]
    write_layer.save(root, sch)
    got = tools["get_symbol_pins"](str(sch), ["R2"], with_connection=True)
    assert got["not_measured"]["unreadable_items"] == 1
    assert {p["number"]: p["attached"] for p in got["symbols"][0]["pins"]} == {"1": ["junction"], "2": "not_measured"}


def test_the_description_and_the_skill_name_the_new_parameters_without_claim_words():
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "get_symbol_pins")  # type: ignore[attr-defined]
    text = tool.description
    for phrase in ("with_connection", "compact", "detail=true", "first 20", "wire, junction or label"):
        assert phrase in text, phrase
    skill = Path(__file__).resolve().parents[1].joinpath(".claude/skills/kicad-schematic/SKILL.md").read_text(encoding="utf-8")
    assert "with_connection" in skill and "compact" in skill
    schema = json.dumps(tool.parameters)
    assert "with_connection" in schema and "compact" in schema and "detail" in schema


# --------------------------------------------------------------------------- pin on pin and no-connect (issue 268 review)


def _server() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _two_ends(tmp_path, tools, *, hide_c_pins=False, lib_b="Device:R") -> Path:
    """R1 (50.8,50.8) pin 2 at (50.8,54.61) and B (50.8,58.42) pin 1 at the same point, no wire. B is R2 or a C."""
    path = Path(project_toolset._project_create(str(tmp_path / "pp"), "pp")["schematic_path"])
    ref = "R2" if lib_b == "Device:R" else "C1"
    assert tools["add_symbols"](str(path), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
        {"lib_id": lib_b, "x": 50.8, "y": 58.42, "ref": ref, "value": "1k"},
    ])["status"] == "ok"
    if hide_c_pins:
        root = write_layer.load(path)
        lib = next(c for c in root.children if not c.is_atom and c.name == "lib_symbols")
        cap = next(c for c in lib.children if not c.is_atom and c.get_string(0) == "Device:C")
        for sub in cap.children:
            if not sub.is_atom and sub.name == "symbol":
                for pin in sub.find_children("pin"):
                    pin.children.append(write_layer.SExp.list("hide", write_layer.symbol("yes")))
        write_layer.save(root, path)
    return path


def test_two_parts_end_to_end_attach_through_kind_pin(tmp_path, tools):
    path = _two_ends(tmp_path, tools)
    got = tools["get_symbol_pins"](str(path), ["*"], with_connection=True)
    assert _attached(got) == {("R1", 1, "2"): ["pin"], ("R2", 1, "1"): ["pin"]}


def test_a_hidden_pin_of_the_other_part_is_not_counted_like_pin_on_pin_does(tmp_path, tools):
    path = _two_ends(tmp_path, tools, hide_c_pins=True, lib_b="Device:C")
    got = tools["get_symbol_pins"](str(path), ["*"], with_connection=True)
    # R1.2 sees only a hidden pin of C1 (not read); C1.1 is hidden itself but R1.2 is visible, so it attaches
    assert _attached(got) == {("C1", 1, "1"): ["pin"]}


def test_a_power_symbol_sitting_on_a_pin_attaches_it(tmp_path, tools):
    path = Path(project_toolset._project_create(str(tmp_path / "pw"), "pw")["schematic_path"])
    assert tools["add_symbols"](str(path), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
        {"lib_id": "power:GND", "x": 50.8, "y": 54.61, "ref": "#PWR01", "value": "GND"},
    ])["status"] == "ok"
    got = tools["get_symbol_pins"](str(path), ["*"], with_connection=True)
    assert _attached(got)[("R1", 1, "2")] == ["pin"]


def test_a_power_symbol_with_a_hidden_pin_on_a_pin_still_attaches_it(tmp_path, tools):
    path = Path(project_toolset._project_create(str(tmp_path / "pwh"), "pwh")["schematic_path"])
    assert tools["add_symbols"](str(path), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
        {"lib_id": "power:GND", "x": 50.8, "y": 54.61, "ref": "#PWR01", "value": "GND"},
    ])["status"] == "ok"
    root = write_layer.load(path)
    lib = next(c for c in root.children if not c.is_atom and c.name == "lib_symbols")
    gnd = next(c for c in lib.children if not c.is_atom and c.get_string(0) == "power:GND")
    for sub in gnd.children:
        if not sub.is_atom and sub.name == "symbol":
            for pin in sub.find_children("pin"):
                if not any((not c.is_atom and c.name == "hide") or (c.is_atom and str(c.value) == "hide") for c in pin.children):
                    pin.children.append(write_layer.SExp.list("hide", write_layer.symbol("yes")))
    write_layer.save(root, path)
    pins = sheet_editing.pin_positions(write_layer.load(path), next(c for c in write_layer.load(path).children if not c.is_atom and c.name == "symbol" and c.find_children("lib_id") and sheet_editing._ref(c) == "#PWR01"), with_hidden=True)
    assert pins and all(p["hidden"] for p in pins)  # the power pin carries the hide flag
    assert _attached(tools["get_symbol_pins"](str(path), ["R1"], with_connection=True))[("R1", 1, "2")] == ["pin"]


def test_two_pins_of_one_part_on_one_point_do_not_attach_each_other(tmp_path, tools):
    path = Path(project_toolset._project_create(str(tmp_path / "same"), "same")["schematic_path"])
    assert tools["add_symbols"](str(path), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"}])["status"] == "ok"
    root = write_layer.load(path)
    lib = next(c for c in root.children if not c.is_atom and c.name == "lib_symbols")
    r = next(c for c in lib.children if not c.is_atom and c.get_string(0) == "Device:R")
    for sub in r.children:
        if not sub.is_atom and sub.name == "symbol":
            for pin in sub.find_children("pin"):
                at = pin.find_children("at")[0]
                at.children[0].value, at.children[1].value = 0, 3.81  # every pin on one point
    write_layer.save(root, path)
    pts = {(p["x"], p["y"]) for p in tools["get_symbol_pins"](str(path), ["R1"])["symbols"][0]["pins"]}
    assert len(pts) == 1
    assert tools["get_symbol_pins"](str(path), ["*"], with_connection=True)["symbols"] == []


def test_a_no_connect_marker_is_not_a_connection_so_the_pin_reads_unattached(sch, tools):
    before = _attached(tools["get_symbol_pins"](str(sch), ["*"], with_connection=True))
    done = tools["add_no_connects"](str(sch), [{"references": ["R2"], "pins": ["2"]}])
    assert done["ok"] is True and done["count"] == 1, done
    after = _attached(tools["get_symbol_pins"](str(sch), ["*"], with_connection=True))
    assert ("R2", 1, "2") not in after and after == before


def test_pins_total_and_the_new_kind_are_documented(sch, tools):
    entry = tools["get_symbol_pins"](str(sch), ["R2"], with_connection=True)["symbols"][0]
    assert entry["pins_total"] == 2 and entry["pin_count"] == 1
    text = _server()["get_symbol_pins"].description
    for phrase in ("pins_total", "no-connect", "pin of another part"):
        assert phrase in text, phrase
    skill = Path(__file__).resolve().parents[1].joinpath(".claude/skills/kicad-schematic/SKILL.md").read_text(encoding="utf-8")
    assert "pins_total" in skill and "`pin`" in skill
