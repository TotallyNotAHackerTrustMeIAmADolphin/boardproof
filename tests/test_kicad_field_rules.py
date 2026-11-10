"""KiCad field rules measured with kicad-cli (GitHub issue 167; source reading in docs/research-kicad-symbol-placement.md).

Every test builds a scratch project through the tools (project_create, add_symbols, create_sheets,
add_sheet_symbols) and then sets the one state no tool can write (a version number, a literal "~", a lowercase
property name, a bare ``hide``) through the project's write layer on kicad_tools' S-expression API
(``write_layer.load``, ``set_string``/``set_number``/``set_symbol``, ``write_layer.save``). No KiCad file is
edited as text. The result is read from ``kicad-cli sch export netlist --format kicadxml``. These tests pin what
KiCad 10 does; the tools' own handling of the same states is left to their own tests.
"""

from __future__ import annotations

import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from kicad_mcp import kicad_cli, write_layer
from kicad_mcp.toolsets import project as project_toolset
from tests import _kicad_faults as faults
from tests import test_sheet_pins as _base

_need_kicad_cli = _base._need_kicad_cli  # autouse fixture: skips without kicad-cli
_tools = _base._tools
root = _base.root  # root sheet with the Connectors and MCU children

LM324 = "Amplifier_Operational:LM324"


@pytest.fixture
def tools() -> dict:
    return _tools()


@pytest.fixture
def proj(tmp_path, tools) -> Path:
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    return Path(made["schematic_path"])


def _props(sym) -> dict:
    return {p.get_string(0): p for p in sym.find_children("property")}


def _placed(tree, ref: str, unit: int | None = None) -> list:
    out = []
    for s in tree.find_children("symbol"):
        if not s.find_children("lib_id"):
            continue
        if _props(s)["Reference"].get_string(1) == ref and (unit is None or s.find_children("unit")[0].get_int(0) == unit):
            out.append(s)
    return out


def _edit(path: Path, change) -> None:
    tree = write_layer.load(path)
    change(tree)
    write_layer.save(tree, path)


def _set_version(path: Path, version: int) -> None:
    _edit(path, lambda t: write_layer.set_number(t.find_children("version")[0], 0, version))


def _set(path: Path, ref: str, name: str, value: str, unit: int | None = None) -> None:
    def change(tree):
        for s in _placed(tree, ref, unit):
            write_layer.set_string(_props(s)[name], 1, value)
    _edit(path, change)


def netlist(sch: Path, tmp: Path, tag: str = "n") -> tuple[dict, str]:
    """kicad-cli kicadxml netlist of ``sch`` as {ref: {"value", "footprint", "datasheet", "fields", "unit_count"}} plus stderr."""
    out = tmp / f"{tag}.xml"
    proc = kicad_cli._run(["sch", "export", "netlist", "--format", "kicadxml", "--output", str(out), str(sch)])
    assert proc.returncode == 0, proc.stderr + proc.stdout
    comps = {}
    for c in ET.parse(out).getroot().iter("comp"):
        def text(tag, c=c):
            return c.findtext(tag) if c.find(tag) is not None else None
        comps[c.get("ref")] = {
            "value": text("value"), "footprint": text("footprint"), "datasheet": text("datasheet"),
            "fields": {f.get("name"): f.text for f in c.iter("field")},
            "properties": {p.get("name"): p.get("value") for p in c.iter("property")},
        }
    return comps, proc.stdout + proc.stderr


def _add(tools, sch: Path, specs: list[dict]) -> None:
    res = tools["add_symbols"](str(sch), specs)
    assert res["status"] == "ok", res


def _r(ref="R1", **kw) -> dict:
    return {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": ref, "value": "1k", **kw}


# ---- fact 1: "~" depends on the file version ------------------------------------------------------------

def test_fact1_tilde_in_a_current_version_file_is_literal_text_and_in_an_older_file_it_is_empty(proj, tools, tmp_path):
    _add(tools, proj, [_r("R1"), _r("R2", x=76.2)])
    for ref in ("R1", "R2"):
        _set(proj, ref, "Datasheet", "~")
        _set(proj, ref, "Footprint", "~")
    old = tmp_path / "old.kicad_sch"
    shutil.copy(proj, old)
    _set_version(proj, 20260306)
    _set_version(old, 20250114)
    new_nl, _ = netlist(proj, tmp_path, "new")
    old_nl, _ = netlist(old, tmp_path, "old")
    assert new_nl["R1"]["datasheet"] == "~" and new_nl["R1"]["footprint"] == "~"
    assert old_nl["R1"]["datasheet"] is None and old_nl["R1"]["footprint"] is None
    assert new_nl["R1"]["fields"].get("Datasheet") == "~"
    assert old_nl["R1"]["fields"].get("Datasheet") in (None, "")


def test_fact1_the_boundary_is_20250318(proj, tools, tmp_path):
    _add(tools, proj, [_r("R1")])
    _set(proj, "R1", "Datasheet", "~")
    seen = {}
    for version in (20250317, 20250318):
        _set_version(proj, version)
        nl, _ = netlist(proj, tmp_path, str(version))
        seen[version] = nl["R1"]["datasheet"]
    assert seen == {20250317: None, 20250318: "~"}


def test_fact1_what_the_tools_write_today_is_a_file_version_where_tilde_means_empty(proj, tools, tmp_path):
    """add_symbols writes into a file whose version is the tools' own (read here, not assumed); since issue 162 the
    instance takes the library's Datasheet (empty for Device:R), so "~" is set through the write layer, as a
    tool written by an older version of the product (or kicad_tools) would have left it."""
    _add(tools, proj, [_r("R1")])
    tree = write_layer.load(proj)
    version = tree.find_children("version")[0].get_int(0)
    assert _props(_placed(tree, "R1")[0])["Datasheet"].get_string(1) == ""
    _set(proj, "R1", "Datasheet", "~")
    nl, _ = netlist(proj, tmp_path)
    assert (version < 20250318) == (nl["R1"]["datasheet"] is None), (version, nl["R1"])


# ---- fact 2: multi-unit field choice -------------------------------------------------------------------

def _units(tools, sch, values: dict[int, str], order=None):
    specs = []
    for unit in (order or sorted(values)):
        specs.append({"lib_id": LM324, "x": 40.0 + 50 * unit, "y": 50.8, "ref": "U1", "value": values[unit] or "x", "unit": unit})
    _add(tools, sch, specs)


def _field_per_unit(sch: Path, name: str, per_unit: dict[int, str]) -> None:
    for unit, value in per_unit.items():
        _set(sch, "U1", name, value, unit)


def _file_order(sch: Path) -> list[int]:
    return [s.find_children("unit")[0].get_int(0) for s in _placed(write_layer.load(sch), "U1")]


def test_fact2_unit1_empty_and_unit2_filled_gives_the_unit2_value(proj, tools, tmp_path):
    _units(tools, proj, {1: "x", 2: "x"})
    _field_per_unit(proj, "Footprint", {1: "", 2: "Pkg:B"})
    _field_per_unit(proj, "Datasheet", {1: "", 2: "http://b"})
    nl, _ = netlist(proj, tmp_path)
    assert nl["U1"]["footprint"] == "Pkg:B" and nl["U1"]["datasheet"] == "http://b"


def test_fact2_unit1_filled_and_unit2_filled_gives_the_unit1_value(proj, tools, tmp_path):
    _units(tools, proj, {1: "x", 2: "x"})
    _field_per_unit(proj, "Footprint", {1: "Pkg:A", 2: "Pkg:B"})
    nl, _ = netlist(proj, tmp_path)
    assert nl["U1"]["footprint"] == "Pkg:A"


def test_fact2_unit1_filled_and_unit2_empty_keeps_the_unit1_value(proj, tools, tmp_path):
    _units(tools, proj, {1: "x", 2: "x"})
    _field_per_unit(proj, "Footprint", {1: "Pkg:A", 2: ""})
    nl, _ = netlist(proj, tmp_path)
    assert nl["U1"]["footprint"] == "Pkg:A"


def test_fact2_value_follows_the_same_rule_as_footprint(proj, tools, tmp_path):
    _units(tools, proj, {1: "x", 2: "x"})
    _set(proj, "U1", "Value", "", 1)
    _set(proj, "U1", "Value", "B", 2)
    nl, _ = netlist(proj, tmp_path)
    assert nl["U1"]["value"] == "B"


def test_fact2_file_order_with_unit3_before_unit2_and_an_empty_unit1_footprint(proj, tools, tmp_path):
    """The INFERRED hole of the research: representative unit 1 empty, unit 3 visited before unit 2."""
    _units(tools, proj, {1: "x", 2: "x", 3: "x"}, order=[1, 3, 2])
    assert _file_order(proj) == [1, 3, 2]
    _field_per_unit(proj, "Footprint", {1: "", 2: "Pkg:B", 3: "Pkg:C"})
    nl, _ = netlist(proj, tmp_path)
    assert nl["U1"]["footprint"] == "Pkg:C"  # unit 2 loses: minUnit is already 1 when it is visited



@pytest.mark.parametrize("order,expected", [
    ([1, 2, 3], "Pkg:B"), ([1, 3, 2], "Pkg:C"), ([2, 1, 3], "Pkg:B"),
    ([2, 3, 1], "Pkg:C"), ([3, 1, 2], "Pkg:C"), ([3, 2, 1], "Pkg:B"),
])
def test_fact2_the_winner_depends_on_the_order_of_the_units_in_the_file_when_the_first_is_empty(proj, tools, tmp_path, order, expected):
    """Footprint empty on unit 1, B on unit 2, C on unit 3: the 'lowest unit' reading is only true for some file orders."""
    _units(tools, proj, {1: "x", 2: "x", 3: "x"}, order=order)
    assert _file_order(proj) == order
    _field_per_unit(proj, "Footprint", {1: "", 2: "Pkg:B", 3: "Pkg:C"})
    nl, _ = netlist(proj, tmp_path)
    assert nl["U1"]["footprint"] == expected


# ---- fact 3: a lowercase property name ------------------------------------------------------------------

def test_fact3_a_lowercase_footprint_property_is_read_as_the_footprint_field(proj, tools, tmp_path):
    _add(tools, proj, [_r("R1")])
    _set_version(proj, 20260306)
    _set(proj, "R1", "Footprint", "Pkg:X")
    def rename(tree):
        write_layer.set_string(_props(_placed(tree, "R1")[0])["Footprint"], 0, "footprint")
    _edit(proj, rename)
    assert "footprint" in _props(_placed(write_layer.load(proj), "R1")[0])
    nl, _ = netlist(proj, tmp_path)
    assert nl["R1"]["footprint"] == "Pkg:X"
    assert nl["R1"]["fields"]["Footprint"] == "Pkg:X"


def test_fact3_a_user_field_in_the_wrong_case_stays_a_different_field(proj, tools, tmp_path):
    _add(tools, proj, [_r("R1")])
    _set_version(proj, 20260306)
    def add_field(tree):
        sym = _placed(tree, "R1")[0]
        prop = _props(sym)["Datasheet"]
        clone = write_layer.SExp.list("property", "Lcsc", "C1")
        for child in prop.children[2:]:
            clone.append(child)
        sym.append(clone)
    _edit(proj, add_field)
    nl, _ = netlist(proj, tmp_path)
    assert "LCSC" not in nl["R1"]["fields"] and "LCSC" not in nl["R1"]["properties"]
    assert nl["R1"]["fields"].get("Lcsc") == "C1"


# ---- fact 4: hide as a direct child of property ---------------------------------------------------------

def _hide_variant(proj: Path, tmp: Path, name: str, mutate) -> subprocess.CompletedProcess:
    """A scratch copy of ``proj`` (version 20260306) with the Footprint property's hide changed by ``mutate``;
    the copy is serialised from the object model (it may be unreadable for KiCad, so it does not go through save)."""
    tree = write_layer.load(proj)
    prop = _props(_placed(tree, "R1")[0])["Footprint"]
    for hide in prop.find_children("effects")[0].find_children("hide"):
        prop.find_children("effects")[0].remove(hide)
    mutate(prop)
    out = tmp / f"{name}.kicad_sch"
    faults.write_unchecked(tree, out)
    return kicad_cli._run(["sch", "export", "netlist", "--format", "kicadxml", "--output", str(tmp / f"{name}.xml"), str(out)])


@pytest.mark.parametrize("name,mutate,loads", [
    ("hide_yes_direct", lambda p: p.append(write_layer.SExp.list("hide", "yes")), True),
    ("hide_bare_direct", lambda p: p.append(write_layer.SExp.atom("hide")), False),
    ("hide_empty_list_direct", lambda p: p.append(write_layer.SExp.list("hide")), False),
    ("hide_bare_in_effects", lambda p: p.find_children("effects")[0].append(write_layer.SExp.atom("hide")), True),
])
def test_fact4_property_hide_must_be_hide_yes_and_a_bare_hide_there_fails_to_load(proj, tools, tmp_path, name, mutate, loads):
    _add(tools, proj, [_r("R1")])
    _set_version(proj, 20260306)
    proc = _hide_variant(proj, tmp_path, name, mutate)
    assert (proc.returncode == 0) is loads, (proc.returncode, proc.stdout, proc.stderr)


def test_fact4_the_write_layer_refuses_a_save_with_a_bare_property_hide(proj, tools):
    """The save-time parse check catches the form that KiCad cannot load (so a writer cannot leave it behind)."""
    _add(tools, proj, [_r("R1")])
    before = proj.read_bytes()
    tree = write_layer.load(proj)
    prop = _props(_placed(tree, "R1")[0])["Footprint"]
    prop.append(write_layer.SExp.atom("hide"))
    with pytest.raises(write_layer.WriteLayerError):
        write_layer.save(tree, proj)
    assert proj.read_bytes() == before


# ---- fact 2, second part: the units on two sheets -------------------------------------------------------

@pytest.mark.parametrize("root_unit,child_unit,root_fp,child_fp,expected", [
    (1, 2, "", "Pkg:child", "Pkg:child"),
    (1, 2, "Pkg:root", "Pkg:child", "Pkg:root"),
    (2, 1, "Pkg:root", "Pkg:child", "Pkg:child"),
    (2, 1, "Pkg:root", "", "Pkg:root"),
])
def test_fact2_one_reference_on_two_sheets_takes_the_lowest_unit_with_a_non_empty_footprint(
        tools, root, tmp_path, root_unit, child_unit, root_fp, child_fp, expected):
    child = root.parent / "connectors.kicad_sch"
    _add(tools, root, [{"lib_id": LM324, "x": 40.0, "y": 150.0, "ref": "U1", "value": "x", "unit": root_unit}])
    _add(tools, child, [{"lib_id": LM324, "x": 140.0, "y": 150.0, "ref": "U1", "value": "x", "unit": child_unit}])
    _set(root, "U1", "Footprint", root_fp)
    _set(child, "U1", "Footprint", child_fp)
    nl, _ = netlist(root, tmp_path)
    assert nl["U1"]["footprint"] == expected
