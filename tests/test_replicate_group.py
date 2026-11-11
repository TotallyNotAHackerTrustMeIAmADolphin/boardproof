"""replicate_group (GitHub issue 94): copy a group with its wires and labels n times on a grid.

Fixtures are built through the tools (and ``write_layer`` for the twin sheet use) on ``tmp_path`` copies of
one module-scoped template; nothing here edits KiCad text. Every position and reference below is read from
the saved file's raw ``(at ...)`` and instance nodes, the nets from kicad-cli's netlist.

The branch (a relay with two resistors, ten of them on a pitch of 17.78 mm is the ticket's example):
K1 Relay:G5Q-1A at (50.8, 50.8), pins 5 (45.72, 43.18), 3 (55.88, 43.18), 1 (45.72, 58.42), 2 (55.88, 58.42);
R1 at (45.72, 33.02) wired to pin 5 with the local label IN1 on its free pin; R2 at (55.88, 33.02) wired to
pin 3 with the global label VBUS on its free pin; pin 1 wired to the local label COIL1; pin 2 wired to a GND
symbol. The group region is x 43.18..58.42, y 26.67..66.04.
"""

from __future__ import annotations

import copy
import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_authoring, sheet_editing, sheet_replicate, write_layer
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset

SExp = write_layer.SExp
REGION = {"x1": 43.18, "y1": 26.67, "x2": 58.42, "y2": 66.04}
PITCH = {"dx": 17.78, "dy": 0}


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


def _fill(tools: dict, sch: Path) -> None:
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Relay:G5Q-1A", "x": 50.8, "y": 50.8, "ref": "K1", "value": "G5Q"},
        {"lib_id": "Device:R", "x": 45.72, "y": 33.02, "ref": "R1", "value": "1k"},
        {"lib_id": "Device:R", "x": 55.88, "y": 33.02, "ref": "R2", "value": "2k"},
        {"lib_id": "power:GND", "x": 55.88, "y": 63.5, "ref": "#PWR01", "value": "GND"}])["status"] == "ok"
    assert tools["add_wires"](str(sch), [
        {"p1": [45.72, 36.83], "p2": [45.72, 43.18], "route": "direct", "snap": False},
        {"p1": [55.88, 36.83], "p2": [55.88, 43.18], "route": "direct", "snap": False},
        {"p1": [45.72, 58.42], "p2": [45.72, 63.5], "route": "direct", "snap": False},
        {"p1": [55.88, 58.42], "p2": [55.88, 63.5], "route": "direct", "snap": False}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [
        {"kind": "label", "x": 45.72, "y": 29.21, "text": "IN1", "snap": False},
        {"kind": "global", "x": 55.88, "y": 29.21, "text": "VBUS", "shape": "input", "snap": False},
        {"kind": "label", "x": 45.72, "y": 63.5, "text": "COIL1", "snap": False}])["status"] == "ok"


@pytest.fixture(scope="module")
def template(tmp_path_factory, tools) -> Path:
    base = tmp_path_factory.mktemp("replicate")
    made = project_toolset._project_create(str(base / "proj"), "demo")
    sch = Path(made["schematic_path"])
    _fill(tools, sch)
    return sch


@pytest.fixture
def sheet(template, tmp_path) -> Path:
    dest = tmp_path / "proj"
    shutil.copytree(template.parent, dest)
    return dest / template.name


@pytest.fixture(scope="module")
def hier_template(tmp_path_factory, tools) -> Path:
    """The branch on the sheet ``sub.kicad_sch`` that the root sheet uses once."""
    base = tmp_path_factory.mktemp("replicate_hier")
    made = project_toolset._project_create(str(base / "proj"), "demo")
    root = Path(made["schematic_path"])
    assert tools["create_sheets"](str(root), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    spec = {"file": "sub.kicad_sch", "name": "Sub", "x": 152.4, "y": 50.8, "width": 25.4, "height": 25.4, "pins": [{"name": "SER", "direction": "input"}]}
    assert tools["add_sheet_symbols"](str(root), [spec])["status"] == "ok"
    _fill(tools, root.parent / "sub.kicad_sch")
    return root


@pytest.fixture
def hier(hier_template, tmp_path) -> Path:
    dest = tmp_path / "proj"
    shutil.copytree(hier_template.parent, dest)
    return dest / hier_template.name


def _tree(sch: Path):
    return sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))


def _nets(sch: Path) -> dict[str, list[str]]:
    return {k: sorted(f"{r}.{p}" for r, p in v) for k, v in sheet_editing.read_nets(_tree(sch), sch, None).items()}


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _kids(path: Path, tag: str) -> list[SExp]:
    return [c for c in write_layer.load(path).children if not c.is_atom and c.name == tag]


def _symbols(path: Path) -> dict[str, list[SExp]]:
    out: dict[str, list[SExp]] = {}
    for s in _kids(path, "symbol"):
        out.setdefault(sheet_editing._ref(s), []).append(s)
    return out


def _at(node: SExp) -> list[float]:
    return list(sheet_editing._at(node)[:2])


def _entries(symbol: SExp) -> list[tuple[str, str]]:
    return [(p.get_string(0), p.find_children("reference")[0].get_string(0))
            for i in symbol.find_children("instances") for pr in i.find_children("project") for p in pr.find_children("path")]


def _all_uuids(path: Path) -> list[str]:
    out = []

    def walk(node):
        for c in node.children:
            if c.is_atom:
                continue
            if c.name == "uuid" and c.children:
                out.append(c.get_string(0))
            walk(c)

    walk(write_layer.load(path))
    return out


def _run(tools, sch, **kw):
    args = {"region": REGION, "copies": 2, "pitch": PITCH}
    args.update(kw)
    return tools["replicate_group"](str(sch), **args)


# --------------------------------------------------------------------------- the ticket's example


def test_a_relay_with_two_resistors_copied_nine_times_makes_ten_branches(sheet, tools):
    before = _nets(sheet)
    uuids_before = set(_all_uuids(sheet))
    result = _run(tools, sheet, copies=9)
    assert result["ok"] is True and result["written"] is True, result
    symbols = _symbols(sheet)
    # references continue from the highest used per prefix: K2..K10, then R3, R4 for copy 1, R5, R6 for copy 2 ...
    assert sorted(r for r in symbols if r.startswith("K")) == sorted(f"K{i}" for i in range(1, 11))
    assert sorted(r for r in symbols if r.startswith("R")) == sorted(f"R{i}" for i in range(1, 21))
    for i in range(1, 10):
        (k,) = symbols[f"K{i + 1}"]
        assert _at(k) == [round(50.8 + 17.78 * i, 4), 50.8], i  # raw (at ..), shifted by i pitches
        (r1,), (r2,) = symbols[f"R{2 * i + 1}"], symbols[f"R{2 * i + 2}"]
        assert _at(r1) == [round(45.72 + 17.78 * i, 4), 33.02] and _at(r2) == [round(55.88 + 17.78 * i, 4), 33.02]
        # the Reference property and the instance entry agree, at the path of the original
        for sym, ref in ((k, f"K{i + 1}"), (r1, f"R{2 * i + 1}")):
            assert sheet_editing._ref(sym) == ref
            assert [e[1] for e in _entries(sym)] == [ref]
            assert _entries(sym)[0][0] == _entries(symbols["K1"][0])[0][0]
    assert result["created"]["copies"][0]["references"] == {"K1": "K2", "R1": "R3", "R2": "R4", "#PWR01": "#PWR02"}
    # labels count up from their own number; the global and the ground symbol are shared
    texts = sorted(c.get_string(0) for c in _kids(sheet, "label"))
    assert texts == sorted([f"IN{i}" for i in range(1, 11)] + [f"COIL{i}" for i in range(1, 11)])
    assert len(_kids(sheet, "global_label")) == 10 and {c.get_string(0) for c in _kids(sheet, "global_label")} == {"VBUS"}
    # the netlist: every old net unchanged, ten private nets per numbered label, the shared nets joined
    after = _nets(sheet)
    assert after["/IN7"] == ["R13.1"] and after["/COIL10"] == ["K10.1"] and after["/IN1"] == before["/IN1"]
    assert after["VBUS"] == sorted(f"R{2 * i}.1" for i in range(1, 11))
    assert after["GND"] == sorted(f"K{i}.2" for i in range(1, 11))
    assert result["netlist"]["checked"] is True and result["netlist"]["joined_existing_nets"] == ["GND", "VBUS"]
    # every uuid in the file is unique and the old ones are all still there
    ids = _all_uuids(sheet)
    assert len(ids) == len(set(ids)) and uuids_before <= set(ids)
    # a copy's wires and labels were shifted with the symbols (raw points)
    wire_ends = {tuple(sheet_editing._wire_ends(w)[0]) for w in _kids(sheet, "wire")}
    assert (round(45.72 + 17.78 * 9, 4), 36.83) in wire_ends
    assert {tuple(_at(c)) for c in _kids(sheet, "label") if c.get_string(0) == "IN10"} == {(round(45.72 + 17.78 * 9, 4), 29.21)}


def test_power_symbols_continue_the_power_numbering(sheet, tools):
    _run(tools, sheet, copies=2)
    refs = sorted(r for r in _symbols(sheet) if r.startswith("#"))
    assert refs == ["#PWR01", "#PWR02", "#PWR03"], refs


def test_a_dry_run_writes_nothing_and_lists_what_would_be_created(sheet, tools):
    before = _bytes(sheet)
    result = _run(tools, sheet, dry_run=True)
    assert result["ok"] is True and result["written"] is False and "created" not in result, result
    assert [c["references"]["K1"] for c in result["would_create"]["copies"]] == ["K2", "K3"]
    assert result["would_create"]["copies"][1]["labels"] == {"IN1": "IN3", "COIL1": "COIL3"}
    assert result["netlist"]["checked"] is True  # the dry run runs the netlist oracle too
    assert _bytes(sheet) == before


def test_references_continue_from_the_highest_used_anywhere_on_the_sheet(sheet, tools):
    assert tools["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": 150.0, "y": 150.0, "ref": "R7", "value": "9k"}])["status"] == "ok"
    result = _run(tools, sheet, copies=1)
    assert result["ok"] is True, result
    assert result["created"]["copies"][0]["references"] == {"K1": "K2", "R1": "R8", "R2": "R9", "#PWR01": "#PWR02"}


def test_label_numbering_keeps_the_leading_zeros(sheet, tools):
    assert tools["add_labels"](str(sheet), [{"kind": "label", "x": 48.26, "y": 63.5, "text": "S08", "snap": False}])["status"] == "ok"
    assert tools["add_wires"](str(sheet), [{"p1": [45.72, 63.5], "p2": [48.26, 63.5], "route": "direct", "snap": False}])["status"] == "ok"
    result = _run(tools, sheet, copies=2, numbered_labels=["S08"])
    assert result["ok"] is True, result
    assert result["created"]["copies"][0]["labels"] == {"S08": "S09"} and result["created"]["copies"][1]["labels"] == {"S08": "S10"}


# --------------------------------------------------------------------------- numbering rules


def test_an_empty_numbering_list_keeps_every_label_so_the_copies_share_the_net(sheet, tools):
    result = _run(tools, sheet, copies=2, numbered_labels=[])
    assert result["ok"] is True and result["created"]["copies"][0]["labels"] == {}, result
    nets = _nets(sheet)
    assert nets["/COIL1"] == ["K1.1", "K2.1", "K3.1"] and nets["/IN1"] == ["R1.1", "R3.1", "R5.1"]


def test_a_numbering_list_can_number_a_global_label(sheet, tools):
    result = _run(tools, sheet, copies=2, numbered_labels=["IN1"])
    assert result["ok"] is True, result
    nets = _nets(sheet)
    assert nets["/IN2"] == ["R3.1"] and nets["/COIL1"] == ["K1.1", "K2.1", "K3.1"]


def test_the_auto_rule_numbers_local_labels_only(sheet, tools):
    tools["add_labels"](str(sheet), [{"kind": "global", "x": 48.26, "y": 63.5, "text": "G7", "shape": "input", "snap": False}])
    tools["add_wires"](str(sheet), [{"p1": [45.72, 63.5], "p2": [48.26, 63.5], "route": "direct", "snap": False}])
    result = _run(tools, sheet, copies=1)
    assert result["ok"] is True and "G7" not in result["created"]["copies"][0]["labels"], result


@pytest.mark.parametrize("numbered, code", [(["NOPE"], "not_found"), (["VBUS"], "not_numbered_text"), ("every", "invalid_spec"),
                                            (["IN1", "IN1"], "invalid_spec"), ([1], "invalid_spec")])
def test_a_bad_numbering_list_refuses_with_nothing_written(sheet, tools, numbered, code):
    before = _bytes(sheet)
    result = _run(tools, sheet, numbered_labels=numbered)
    assert result["ok"] is False and result["code"] == code, result
    assert _bytes(sheet) == before


def test_a_counted_label_that_exists_already_refuses(sheet, tools):
    assert tools["add_labels"](str(sheet), [{"kind": "label", "x": 120.0, "y": 100.0, "text": "IN3", "snap": False}])["status"] == "ok"
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=2)
    assert result["ok"] is False and result["code"] == "label_exists" and result["would_be"] == "IN3", result
    assert _bytes(sheet) == before


def test_a_counted_global_label_that_exists_on_another_sheet_refuses(hier, tools):
    sub = hier.parent / "sub.kicad_sch"
    assert tools["add_labels"](str(hier), [{"kind": "global", "x": 20.32, "y": 20.32, "text": "G2", "shape": "input", "snap": False}])["status"] == "ok"
    assert tools["add_labels"](str(sub), [{"kind": "global", "x": 48.26, "y": 63.5, "text": "G1", "shape": "input", "snap": False}])["status"] == "ok"
    assert tools["add_wires"](str(sub), [{"p1": [45.72, 63.5], "p2": [48.26, 63.5], "route": "direct", "snap": False}])["status"] == "ok"
    before = _bytes(sub)
    result = _run(tools, sub, copies=1, numbered_labels=["G1"], root_sch=str(hier))
    assert result["ok"] is False and result["code"] == "label_exists" and result["would_be"] == "G2", result
    assert _bytes(sub) == before


# --------------------------------------------------------------------------- refusals


def test_a_pitch_off_the_grid_refuses(sheet, tools):
    before = _bytes(sheet)
    result = _run(tools, sheet, pitch={"dx": 17.0, "dy": 0})
    assert result["ok"] is False and result["code"] == "off_grid", result
    assert _bytes(sheet) == before
    assert _run(tools, sheet, pitch={"dx": 17.78, "dy": 0.5})["code"] == "off_grid"
    # a coarser grid accepts a multiple of it only
    assert _run(tools, sheet, pitch={"dx": 19.05, "dy": 0}, grid=2.54)["code"] == "off_grid"
    assert _run(tools, sheet, pitch={"dx": 20.32, "dy": 0}, grid=2.54, dry_run=True)["ok"] is True


def test_a_copy_that_would_overlap_an_existing_object_refuses(sheet, tools):
    # a free wire where copy 2 would land (x 78.74 .. 93.98)
    assert tools["add_wires"](str(sheet), [{"p1": [80.01, 40.64], "p2": [85.09, 40.64], "route": "direct", "snap": False}])["status"] == "ok"
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=2)
    assert result["ok"] is False and result["code"] == "overlaps_existing" and any("copy 2" in o for o in result["overlaps"]), result
    assert _bytes(sheet) == before
    assert _run(tools, sheet, copies=1, dry_run=True)["ok"] is True  # copy 1 alone is clear


def test_a_pitch_smaller_than_the_group_refuses(sheet, tools):
    before = _bytes(sheet)
    result = _run(tools, sheet, pitch={"dx": 12.7, "dy": 0})
    assert result["ok"] is False and result["code"] == "overlaps_existing" and "meets the original" in result["overlaps"][0], result
    assert _bytes(sheet) == before


def test_a_copy_off_the_page_refuses(sheet, tools):
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=20)  # A4 is 297 mm wide: 43.18 + 20 * 17.78 + 15.24 > 297 hits at copy 14
    assert result["ok"] is False and result["code"] == "outside_page", result
    assert _bytes(sheet) == before


def test_a_region_that_cuts_a_wire_refuses_and_names_it(sheet, tools):
    before = _bytes(sheet)
    result = _run(tools, sheet, region={**REGION, "y2": 40.0})  # cuts the wires from R1 and R2 down to the relay
    assert result["ok"] is False and result["code"] == "crosses_region" and any(c.startswith("wire") for c in result["crossing"]), result
    assert _bytes(sheet) == before


def test_a_symbol_with_pins_on_both_sides_of_the_boundary_refuses(sheet, tools):
    result = _run(tools, sheet, region={**REGION, "y2": 55.0})  # K1's pins 1 and 2 are at y 58.42
    assert result["ok"] is False and result["code"] == "crosses_region" and any("K1" in c for c in result["crossing"]), result


def test_a_hierarchical_label_in_the_region_refuses(sheet, tools):
    assert tools["add_labels"](str(sheet), [{"kind": "hier", "x": 48.26, "y": 63.5, "text": "H1", "shape": "input", "snap": False}])["status"] == "ok"
    before = _bytes(sheet)
    result = _run(tools, sheet)
    assert result["ok"] is False and result["code"] == "unsupported_in_region", result
    assert _bytes(sheet) == before


def test_an_empty_region_refuses(sheet, tools):
    result = _run(tools, sheet, region={"x1": 150, "y1": 150, "x2": 160, "y2": 160})
    assert result["ok"] is False and result["code"] == "empty_group", result


def test_an_unannotated_reference_refuses(sheet, tools):
    assert tools["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": 50.8, "y": 60.96, "ref": "R?", "value": "x"}])["status"] == "ok"
    before = _bytes(sheet)
    result = _run(tools, sheet)
    assert result["ok"] is False and result["code"] == "unannotated_reference", result
    assert _bytes(sheet) == before


def test_one_unit_of_a_multi_unit_part_inside_refuses(sheet, tools):
    assert tools["add_symbols"](str(sheet), [
        {"lib_id": "Amplifier_Operational:LM324", "x": 50.8, "y": 90.0, "ref": "U1", "value": "LM324", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM324", "x": 150.8, "y": 90.0, "ref": "U1", "value": "LM324", "unit": 2}])["status"] == "ok"
    big = {**REGION, "y2": 110.0}
    before = _bytes(sheet)
    result = _run(tools, sheet, region=big, copies=1)
    assert result["ok"] is False and result["code"] == "partial_part", result
    assert _bytes(sheet) == before


def test_a_whole_multi_unit_part_is_copied_with_one_reference(sheet, tools):
    assert tools["add_symbols"](str(sheet), [
        {"lib_id": "Amplifier_Operational:LM324", "x": 50.8, "y": 90.0, "ref": "U1", "value": "LM324", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM324", "x": 50.8, "y": 115.0, "ref": "U1", "value": "LM324", "unit": 2}])["status"] == "ok"
    result = _run(tools, sheet, region={**REGION, "y2": 130.0}, copies=1)
    assert result["ok"] is True, result
    units = sorted(int(s.find_children("unit")[0].get_int(0)) for s in _symbols(sheet)["U2"])
    assert units == [1, 2] and len(_symbols(sheet)["U1"]) == 2


_BAD_ARGS = [
    ("region missing key", {"region": {"x1": 0, "y1": 0, "x2": 5}}),
    ("region not an object", {"region": [0, 0, 5, 5]}),
    ("region no area", {"region": {"x1": 0, "y1": 0, "x2": 0, "y2": 5}}),
    ("region nan", {"region": {"x1": float("nan"), "y1": 0, "x2": 5, "y2": 5}}),
    ("region bool", {"region": {"x1": True, "y1": 0, "x2": 5, "y2": 5}}),
    ("copies zero", {"copies": 0}),
    ("copies a float", {"copies": 2.0}),
    ("copies a bool", {"copies": True}),
    ("copies too many", {"copies": 101}),
    ("pitch zero", {"pitch": {"dx": 0, "dy": 0}}),
    ("pitch missing", {"pitch": {"dx": 1.27}}),
    ("pitch not finite", {"pitch": {"dx": float("inf"), "dy": 0}}),
    ("grid zero", {"grid": 0}),
    ("dry_run not a bool", {"dry_run": "yes"}),
]


@pytest.mark.parametrize("name, kw", _BAD_ARGS, ids=[b[0] for b in _BAD_ARGS])
def test_bad_arguments_refuse_with_nothing_written(sheet, tools, name, kw):
    before = _bytes(sheet)
    result = _run(tools, sheet, **kw)
    assert result["ok"] is False and result["code"] in ("invalid_spec", "off_grid"), result
    assert _bytes(sheet) == before


def test_a_missing_sheet_refuses(tmp_path, tools):
    assert _run(tools, tmp_path / "nope.kicad_sch")["code"] == "file_not_found"


# --------------------------------------------------------------------------- what the copy keeps


def test_a_mirrored_rotated_symbol_keeps_its_orientation_and_library_fields(sheet, tools):
    assert tools["add_symbols"](str(sheet), [
        {"lib_id": "Device:R", "x": 50.8, "y": 76.2, "ref": "R9", "value": "4k7", "rotation": 90, "mirror": "y",
         "footprint": "Resistor_SMD:R_0603_1608Metric", "properties": {"LCSC": "C25804"}}])["status"] == "ok"
    result = _run(tools, sheet, region={**REGION, "y2": 80.0}, copies=1)
    assert result["ok"] is True, result
    src = _symbols(sheet)["R9"][0]
    new = _symbols(sheet)[result["created"]["copies"][0]["references"]["R9"]][0]
    for tag in ("mirror", "lib_id"):
        assert [c.get_string(0) for c in src.find_children(tag)] == [c.get_string(0) for c in new.find_children(tag)]
    assert sheet_editing._at(new)[2] == 90.0
    def props(s):
        return {p.get_string(0): p.get_string(1) for p in s.find_children("property")}

    assert {k: v for k, v in props(src).items() if k != "Reference"} == {k: v for k, v in props(new).items() if k != "Reference"}
    # the fields reach kicad-cli's netlist on the copy
    tree = _tree(sheet)
    capture: dict = {}
    sheet_editing.read_nets(tree, sheet, None, capture)
    comps = {c["ref"]: c for c in capture["components"]}
    assert comps[props(new)["Reference"]]["fields"] == comps["R9"]["fields"] and comps["R9"]["fields"]["LCSC"] == "C25804"


# --------------------------------------------------------------------------- hierarchy (issue 79)


def test_a_sheet_a_parent_uses_gets_instance_data_at_the_use_path(hier, tools):
    sub = hier.parent / "sub.kicad_sch"
    root_before = hier.read_bytes()
    src_entries = _entries(_symbols(sub)["K1"][0])
    assert len(src_entries) == 1 and src_entries[0][0].count("/") == 2, src_entries  # /<root uuid>/<sheet uuid>
    result = _run(tools, sub, copies=2, root_sch=str(hier))
    assert result["ok"] is True and result["written"] is True, result
    new = _symbols(sub)["K3"][0]
    assert _entries(new) == [(src_entries[0][0], "K3")]  # the same use path, the new reference
    assert hier.read_bytes() == root_before
    nets = _nets(hier)
    assert nets["/Sub/IN3"] == ["R5.1"] and nets["VBUS"] == ["R2.1", "R4.1", "R6.1"] and nets["GND"] == ["K1.2", "K2.2", "K3.2"]


def _twin(root: Path) -> str:
    """Use sub.kicad_sch a second time (sheet Sub2) and give every symbol a second entry with the references
    R3/R4/K2 style numbers kept clear of the group's: R11, R12, K7. Returns the twin's instance path."""
    top = write_layer.load(root)
    root_uuid = top.find_children("uuid")[0].get_string(0)
    sheet = next(c for c in top.children if not c.is_atom and c.name == "sheet")
    twin = copy.deepcopy(sheet)
    twin_uuid = "99999999-9999-4999-8999-999999999999"
    write_layer.set_string(twin.find_children("uuid")[0], 0, twin_uuid)
    for prop in twin.find_children("property"):
        if prop.get_string(0) == "Sheetname":
            write_layer.set_string(prop, 1, "Sub2")
        if prop.get_string(0) == "Sheetfile":
            pass
    at = twin.find_children("at")[0]
    write_layer.set_number(at, 1, 101.6)
    for n, pin in enumerate(twin.find_children("pin")):
        write_layer.set_string(pin.find_children("uuid")[0], 0, f"99999999-9999-4999-8999-99999999990{n}")
    top.children.insert(top.children.index(sheet) + 1, twin)
    sheet_uuid = sheet.find_children("uuid")[0].get_string(0)
    for inst in twin.find_children("instances"):
        for project in inst.find_children("project"):
            for entry in project.find_children("path"):
                write_layer.set_string(entry, 0, f"/{root_uuid}/{twin_uuid}")
    write_layer.save(top, root)
    sub = write_layer.load(root.parent / "sub.kicad_sch")
    other = {"K1": "K7", "R1": "R11", "R2": "R12", "#PWR01": "#PWR07"}
    for symbol in sub.children:
        if symbol.is_atom or symbol.name != "symbol":
            continue
        for inst in symbol.find_children("instances"):
            project = inst.find_children("project")[0]
            entry = next(e for e in project.find_children("path") if e.get_string(0) == f"/{root_uuid}/{sheet_uuid}")
            second = copy.deepcopy(entry)
            write_layer.set_string(second, 0, f"/{root_uuid}/{twin_uuid}")
            write_layer.set_string(second.find_children("reference")[0], 0, other[entry.find_children("reference")[0].get_string(0)])
            project.children.append(second)
    write_layer.save(sub, root.parent / "sub.kicad_sch")
    return f"/{root_uuid}/{twin_uuid}"


def test_a_sheet_used_twice_gets_one_new_reference_per_use(hier, tools):
    twin_path = _twin(hier)
    sub = hier.parent / "sub.kicad_sch"
    result = _run(tools, sub, copies=1, root_sch=str(hier))
    assert result["ok"] is True and result["written"] is True, result
    assert len(result["sheet_uses"]) == 2
    new = next(s for s in _kids(sub, "symbol") if sheet_editing._ref(s) not in ("K1", "R1", "R2", "#PWR01") and not sheet_editing._ref(s).startswith("#")
               and _entries(s)[0][1].startswith("K"))
    entries = dict(_entries(new))
    # the first use continues after K1 (highest K in the tree is K7: both uses are counted), each use has its own number
    assert sorted(entries.values()) == ["K8", "K9"] and twin_path in entries, entries
    nets = _nets(hier)
    assert len([n for n in nets if n.startswith("/Sub/IN")]) == 2 and len([n for n in nets if n.startswith("/Sub2/IN")]) == 2


def test_instance_data_that_is_not_proven_for_every_use_refuses(hier, tools):
    sub = hier.parent / "sub.kicad_sch"
    top = write_layer.load(sub)
    k1 = next(s for s in top.children if not s.is_atom and s.name == "symbol" and sheet_editing._ref(s) == "K1")
    k1.children[:] = [c for c in k1.children if c.is_atom or c.name != "instances"]
    write_layer.save(top, sub)
    before = _bytes(sub)
    result = _run(tools, sub, copies=1, root_sch=str(hier))
    assert result["ok"] is False and result["code"] == "unproven_instance_data", result
    assert _bytes(sub) == before


# --------------------------------------------------------------------------- the self-checks fire


def test_the_staged_check_fires_when_a_copy_is_written_without_its_wires(sheet, tools, monkeypatch):
    """Mutant: a corrupted write drops the wires of copy 2 (they are announced as added but not in the tree)."""
    real = sheet_replicate._insert
    state = {"n": 0}

    def drop_some_wires(root, node):
        if node.name == "wire":
            state["n"] += 1
            if state["n"] > 4:  # the second copy's wires
                return
        real(root, node)

    monkeypatch.setattr(sheet_replicate, "_insert", drop_some_wires)
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=2)
    assert result["ok"] is False and result["code"] in ("write_not_verified",), result
    assert before == _bytes(sheet)


def test_the_netlist_oracle_fires_when_a_copy_connects_differently_from_the_original(sheet, tools, monkeypatch):
    """Mutant: the wires of copy 2 are not moved (they stay on the original's). The file is a consistent drawing and
    every node is announced, only kicad-cli says the copy's pins are not wired like the original's."""
    real = sheet_replicate._shift

    def forget_wires_of_copy_two(node, dx, dy):
        if node.name == "wire" and abs(dx - 2 * 17.78) < 1e-6:
            return
        real(node, dx, dy)

    monkeypatch.setattr(sheet_replicate, "_shift", forget_wires_of_copy_two)
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=2)
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "netlist_follows_plan", result
    assert any("copy 2 connects differently" in p for p in result["problems"]), result["problems"]
    assert before == _bytes(sheet)


def test_the_netlist_oracle_fires_when_a_copy_lands_on_existing_objects(sheet, tools, monkeypatch):
    """Mutant: the overlap guards are bypassed (a pitch of 10.16 mm puts copy 1 on top of the original). The copy
    then joins the original's nets in kicad-cli, which is not the netlist before plus the copies."""
    monkeypatch.setattr(sheet_replicate, "_shift_box", lambda box, dx, dy: (1000.0, 1000.0, 1001.0, 1001.0))
    monkeypatch.setattr(sheet_replicate, "_page", lambda root: None)
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=1, pitch={"dx": 10.16, "dy": 0})
    assert result["ok"] is False and result["code"] in ("write_not_verified", "connectivity_changed"), result
    assert result.get("check", "netlist_follows_plan") == "netlist_follows_plan"
    assert before == _bytes(sheet)


def test_the_reference_check_fires_when_two_parts_get_the_same_reference(sheet, tools, monkeypatch):
    """Mutant: the counter does not count: every R gets R9. The netlist compares pins, so it cannot see two parts
    under one reference (the written file passes it); the reference check does."""
    real = sheet_replicate._render_ref
    monkeypatch.setattr(sheet_replicate, "_render_ref", lambda prefix, number, digits: real(prefix, 9, digits) if prefix == "R" else real(prefix, number, digits))
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=2)
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "references_unique", result
    assert before == _bytes(sheet)


def test_the_netlist_oracle_fires_when_a_copy_lands_on_the_original_net(sheet, tools, monkeypatch):
    """Mutant: the label of copy 2 keeps the source text although the plan numbered it. The file then joins the
    nets of copy 2 and the original (a count that runs into an existing net), which is not what the plan says."""
    original = sheet_replicate._plan_labels

    def keep_text_of_copy_two(*args, **kwargs):
        original(*args, **kwargs)
        args[-1].new_text[(2, "COIL1")] = "COIL1"

    monkeypatch.setattr(sheet_replicate, "_plan_labels", keep_text_of_copy_two)
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=2)
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "netlist_follows_plan", result
    assert before == _bytes(sheet)


def test_the_staged_check_fires_when_the_call_touches_an_old_node(sheet, tools, monkeypatch):
    """Mutant: the shift also moves the original wires (a clone that shares its children with the source)."""
    real = sheet_replicate._clone

    def shallow(node):
        new = real(node)
        if node.name == "wire":
            write_layer.set_number(node.find_children("pts")[0].find_children("xy")[0], 0, 99.0)  # touches the ORIGINAL
        return new

    monkeypatch.setattr(sheet_replicate, "_clone", shallow)
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=1)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert before == _bytes(sheet)


def test_a_reused_uuid_is_caught(sheet, tools, monkeypatch):
    """Mutant: no new uuids (the clone keeps the source's): the staged check sees a node that is not new."""
    monkeypatch.setattr(sheet_replicate, "_fresh_uuids", lambda node, used: None)
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=1)
    assert result["ok"] is False, result
    assert before == _bytes(sheet)


def test_the_component_check_fires_when_a_copy_does_not_carry_its_sources_fields(sheet, tools, monkeypatch):
    """Mutant: the copy's Value is changed. Pins and nets are as planned; only the component table differs."""
    real = sheet_replicate._copy_symbol

    def other_value(node, copy, plan):
        new, refs = real(node, copy, plan)
        prop = sheet_editing._property(new, "Value")
        if prop is not None and sheet_editing._ref(new).startswith("R"):
            write_layer.set_string(prop, 1, "CHANGED")
        return new, refs

    monkeypatch.setattr(sheet_replicate, "_copy_symbol", other_value)
    before = _bytes(sheet)
    result = _run(tools, sheet, copies=1)
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "netlist_follows_plan", result
    assert before == _bytes(sheet)


def test_a_local_power_symbol_in_the_group_refuses(sheet, tools, monkeypatch):
    monkeypatch.setattr(sheet_replicate, "_lib_power", lambda root, symbol: "local")
    before = _bytes(sheet)
    result = _run(tools, sheet)
    assert result["ok"] is False and result["code"] == "local_power_in_group", result
    assert _bytes(sheet) == before


def test_the_original_branch_is_untouched_by_the_copies(sheet, tools):
    """Every node that was on the sheet is byte-equal afterwards (the staged check, read here from the files)."""
    before = {sheet_editing._uuid(c): write_layer.serialise(c, compact=True) for c in write_layer.load(sheet).children if not c.is_atom and sheet_editing._uuid(c)}
    assert _run(tools, sheet, copies=3)["ok"] is True
    after = {sheet_editing._uuid(c): write_layer.serialise(c, compact=True) for c in write_layer.load(sheet).children if not c.is_atom and sheet_editing._uuid(c)}
    assert all(after[u] == text for u, text in before.items())
    assert len(after) == len(before) + 3 * 11  # per copy: 4 symbols, 4 wires, 2 local labels, 1 global label


# --------------------------------------------------------------------------- vertical and diagonal pitch, property texts


def _geometry(path: Path) -> dict:
    """Raw points of every symbol (anchor and each property text), wire, label and junction of the sheet."""
    syms = {sheet_editing._ref(s): (_at(s), {p.get_string(0): _at(p) for p in s.find_children("property")}) for s in _kids(path, "symbol")}
    wires = {tuple(map(tuple, sheet_editing._wire_ends(w))) for w in _kids(path, "wire")}
    labels = {(c.get_string(0), tuple(_at(c))) for c in _kids(path, "label") + _kids(path, "global_label")}
    junctions = {tuple(_at(j)) for j in _kids(path, "junction")}
    return {"syms": syms, "wires": wires, "labels": labels, "junctions": junctions}


@pytest.mark.parametrize("pitch", [{"dx": 0, "dy": 40.64}, {"dx": 17.78, "dy": 40.64}], ids=["vertical", "diagonal"])
def test_every_point_of_a_copy_moves_by_the_whole_pitch_property_texts_included(sheet, tools, pitch):
    assert tools["add_junctions"](str(sheet), [[45.72, 40.64]])["status"] == "ok"
    src = _geometry(sheet)
    result = _run(tools, sheet, copies=2, pitch=pitch)
    assert result["ok"] is True, result
    now = _geometry(sheet)
    def r(v):
        return round(v, 4)

    for i, entry in enumerate(result["created"]["copies"], start=1):
        ox, oy = i * pitch["dx"], i * pitch["dy"]
        for old, new in entry["references"].items():
            (at, props), (nat, nprops) = src["syms"][old], now["syms"][new]
            assert nat == [r(at[0] + ox), r(at[1] + oy)], (old, new)
            assert set(props) == set(nprops)
            for name, pat in props.items():  # the Reference, Value and other property texts move with the symbol
                assert nprops[name] == [r(pat[0] + ox), r(pat[1] + oy)], (new, name)
        for ends in src["wires"]:
            assert tuple((r(x + ox), r(y + oy)) for x, y in ends) in now["wires"], ends
        for text, (x, y) in src["labels"]:
            new_text = entry["labels"].get(text, text)
            assert (new_text, (r(x + ox), r(y + oy))) in now["labels"], (text, new_text)
        for x, y in src["junctions"]:
            assert (r(x + ox), r(y + oy)) in now["junctions"]


# --------------------------------------------------------------------------- numbering across sheets, shared local labels


def test_the_highest_reference_is_taken_from_every_sheet_of_the_tree(hier, tools):
    sub = hier.parent / "sub.kicad_sch"
    assert tools["add_symbols"](str(hier), [{"lib_id": "Device:R", "x": 20.32, "y": 100.0, "ref": "R7", "value": "9k"}])["status"] == "ok"
    result = _run(tools, sub, copies=1, root_sch=str(hier))
    assert result["ok"] is True, result
    assert result["created"]["copies"][0]["references"]["R1"] == "R8" and result["created"]["copies"][0]["references"]["R2"] == "R9"


def test_a_local_label_that_is_not_counted_is_reported_as_shared(sheet, tools):
    result = _run(tools, sheet, copies=1, numbered_labels=[], dry_run=True)
    assert sorted(w["label"] for w in result["warnings"]) == ["COIL1", "IN1"] and {w["code"] for w in result["warnings"]} == {"label_shared"}
    assert "warnings" not in _run(tools, sheet, copies=1, dry_run=True)  # counted labels: no warning
