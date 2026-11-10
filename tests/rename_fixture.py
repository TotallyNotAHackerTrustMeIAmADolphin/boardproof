"""The project the rename tests (GitHub issue 96) work on, built only through the tools and ``write_layer``.

Root sheet ``demo``: R1 (10k) with the power symbol VCC on its pin 1 and the local label SDA on its pin 2;
R2 with the global label GL on its pin 1; the sheet symbol ``Sub`` (file sub.kicad_sch) with the sheet pins
SDA and CLK, and a second local label SDA on the SDA pin. Sub: R10 (hierarchical label SDA on pin 1, local
label LOC on pin 2) and R3 (global label GL on pin 1, hierarchical label CLK on pin 2).

Nets in kicad-cli's netlist: ``VCC`` [R1.1], ``/SDA`` [R1.2, R10.1], ``GL`` [R2.1, R3.1], ``/Sub/LOC`` [R10.2],
``/Sub/CLK`` [R3.2].
"""

from __future__ import annotations

import shutil
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from kicad_mcp import sheet_authoring, sheet_editing, write_layer
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset


def tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def build(folder: Path) -> Path:
    """Make the project under ``folder``; returns the root sheet."""
    t = tools()
    made = project_toolset._project_create(str(folder / "proj"), "demo")
    root = Path(made["schematic_path"])
    assert t["create_sheets"](str(root), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    sub = root.parent / "sub.kicad_sch"
    assert t["add_symbols"](str(sub), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R10", "value": "1k"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R3", "value": "2k"}])["status"] == "ok"
    assert t["add_labels"](str(sub), [
        {"kind": "hier", "x": 50.8, "y": 46.99, "text": "SDA", "shape": "bidirectional", "snap": False},
        {"kind": "label", "x": 50.8, "y": 54.61, "text": "LOC", "snap": False},
        {"kind": "global", "x": 76.2, "y": 46.99, "text": "GL", "snap": False},
        {"kind": "hier", "x": 76.2, "y": 54.61, "text": "CLK", "shape": "output", "snap": False}])["status"] == "ok"
    assert t["add_symbols"](str(root), [
        {"lib_id": "Device:R", "x": 63.5, "y": 60.96, "ref": "R1", "value": "10k"},
        {"lib_id": "power:VCC", "x": 63.5, "y": 57.15, "ref": "#PWR01", "value": "VCC"},
        {"lib_id": "Device:R", "x": 63.5, "y": 101.6, "ref": "R2", "value": "4k7"}])["status"] == "ok"
    placed = t["add_sheet_symbols"](str(root), [
        {"file": "sub.kicad_sch", "name": "Sub", "x": 101.6, "y": 50.8, "width": 30.48, "height": 38.1,
         "pins": [{"name": "SDA", "direction": "bidirectional"}, {"name": "CLK", "direction": "output"}]}])
    assert placed["status"] == "ok", placed
    sheet = next(c for c in write_layer.load(root).children if not c.is_atom and c.name == "sheet")
    pin = next(p for p in sheet.find_children("pin") if p.get_string(0) == "SDA")
    px, py, _a = sheet_editing._at(pin)
    assert t["add_labels"](str(root), [
        {"kind": "label", "x": 63.5, "y": 64.77, "text": "SDA", "snap": False},
        {"kind": "label", "x": px, "y": py, "text": "SDA", "snap": False},
        {"kind": "global", "x": 63.5, "y": 97.79, "text": "GL", "snap": False}])["status"] == "ok"
    return root


def nets(root: Path) -> dict[str, list[str]]:
    tree = sheet_authoring._require_tree(root)
    return {k: sorted(f"{r}.{p}" for r, p in v) for k, v in sheet_editing.read_nets(tree, root, None).items()}


def copy_of(template_root: Path, dest: Path) -> Path:
    """A private copy of the template project (same relative layout, same uuids)."""
    shutil.copytree(template_root.parent, dest)
    return dest / template_root.name


def snapshot(root: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(root.parent.iterdir()) if p.is_file()}


def children(path: Path, tag: str):
    return [c for c in write_layer.load(path).children if not c.is_atom and c.name == tag]
