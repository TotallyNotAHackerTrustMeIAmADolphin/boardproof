"""add_labels kind "power" writes the same instance data as add_symbols with a power lib_id (GitHub issue 121).

Reproduction: ``add_labels`` of ``{"kind": "power", "text": "power:GND"}`` wrote the symbol's
``(instances (project "" (path "/<uuid>" (reference "#PWR604") ..)))`` with an empty project name and a
reference without leading zeros, while ``add_symbols`` with the same lib_id wrote the project's name and
``#PWR0nnn``. Both are compared from the raw ``(instances)`` block (the project name, the path, the
reference, the unit) apart from the uuid. Fixtures are built through the tools on ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_authoring, write_layer
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset

PIN = [{"name": "A", "direction": "input"}]


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def _tools():
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _power_instances(sheet: Path) -> list[dict]:
    """The raw (instances) data of every power symbol (reference starting with #) on the sheet."""
    found = []
    for symbol in [c for c in write_layer.load(sheet).children if not c.is_atom and c.name == "symbol"]:
        ref = [p for p in symbol.find_children("property") if p.get_string(0) == "Reference"][0].get_string(1)
        if not ref.startswith("#"):
            continue
        for inst in symbol.find_children("instances"):
            for proj in inst.find_children("project"):
                for path in proj.find_children("path"):
                    found.append({
                        "project": proj.get_string(0),
                        "path": path.get_string(0),
                        "reference": path.find("reference").get_string(0),
                        "unit": path.find("unit").get_string(0) if path.find("unit") is not None else None,
                        "property_reference": ref,
                    })
    return found


def _fresh(tmp_path, name):
    made = project_toolset._project_create(str(tmp_path / name), "demo")
    return Path(made["schematic_path"])


def _sheet_uuid(sheet: Path) -> str:
    return write_layer.load(sheet).find_children("uuid")[0].get_string(0)


def test_power_label_instances_equal_add_symbols(tmp_path):
    tools = _tools()
    a, b = _fresh(tmp_path, "a"), _fresh(tmp_path, "b")
    assert tools["add_symbols"](str(a), [{"lib_id": "power:GND", "x": 50.8, "y": 50.8, "ref": "#PWR01"}])["status"] == "ok"
    got = tools["add_labels"](str(b), [{"kind": "power", "text": "power:GND", "x": 50.8, "y": 50.8}])
    assert got["status"] == "ok", got
    via_symbols, via_labels = _power_instances(a), _power_instances(b)
    assert len(via_symbols) == len(via_labels) == 1
    s, lab = via_symbols[0], via_labels[0]
    assert lab["project"] == s["project"]  # the root sheet keeps what add_symbols writes there
    assert lab["path"] == f"/{_sheet_uuid(b)}" and s["path"] == f"/{_sheet_uuid(a)}"
    assert lab["unit"] == s["unit"]
    assert lab["reference"] == lab["property_reference"] == "#PWR01"


def test_power_label_references_have_leading_zeros_and_are_unique(tmp_path):
    tools = _tools()
    sheet = _fresh(tmp_path, "c")
    assert tools["add_labels"](str(sheet), [
        {"kind": "power", "text": "power:GND", "x": 50.8, "y": 50.8},
        {"kind": "power", "text": "power:+3V3", "x": 76.2, "y": 50.8}])["status"] == "ok"
    assert tools["add_symbols"](str(sheet), [{"lib_id": "power:GND", "x": 101.6, "y": 50.8, "ref": "#PWR03"}])["status"] == "ok"
    refs = [i["reference"] for i in _power_instances(sheet)]
    assert refs[:2] == ["#PWR01", "#PWR02"], refs
    assert len({i["project"] for i in _power_instances(sheet)}) == 1


def test_power_label_reference_is_new_tree_wide_and_keeps_leading_zero(tmp_path):
    tools = _tools()
    root = _fresh(tmp_path, "e")
    assert sheet_authoring.create_sheets(str(root), [{"file": "a.kicad_sch"}])["status"] == "ok"
    sub = root.parent / "a.kicad_sch"
    assert sheet_authoring.add_sheet_symbols(
        str(root), [{"file": "a.kicad_sch", "name": "A", "x": 101.6, "y": 50.8, "pins": PIN}])["status"] == "ok"
    assert tools["add_symbols"](str(root), [{"lib_id": "power:GND", "x": 50.8, "y": 50.8, "ref": "#PWR0603"}])["status"] == "ok"
    assert tools["add_labels"](str(sub), [{"kind": "power", "text": "power:GND", "x": 50.8, "y": 50.8}])["status"] == "ok"
    (inst,) = _power_instances(sub)
    assert inst["reference"] == "#PWR0604", inst


def test_power_label_on_attached_sub_sheet_gets_the_use_path(tmp_path):
    tools = _tools()
    root = _fresh(tmp_path, "d")
    assert sheet_authoring.create_sheets(str(root), [{"file": "a.kicad_sch"}])["status"] == "ok"
    sub = root.parent / "a.kicad_sch"
    assert sheet_authoring.add_sheet_symbols(
        str(root), [{"file": "a.kicad_sch", "name": "A", "x": 101.6, "y": 50.8, "pins": PIN}])["status"] == "ok"
    assert tools["add_labels"](str(sub), [{"kind": "power", "text": "power:GND", "x": 50.8, "y": 50.8}])["status"] == "ok"
    (inst,) = _power_instances(sub)
    assert inst["project"] == "demo"
    assert inst["path"] != f"/{_sheet_uuid(sub)}"
    assert inst["reference"].startswith("#PWR0")
