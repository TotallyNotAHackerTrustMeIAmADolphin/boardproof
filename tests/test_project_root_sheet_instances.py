"""Issue 211: a new project writes the root (sheet_instances (path ...)) as "/", the form KiCad writes.

kicad_tools writes "/<root sheet uuid>" there; KiCad 10.0.7 loads both, the KiCad 11 nightly rejects
the uuid form ("malformed root sheet instance path"). Facts are read from the saved file's S-expression
through the write layer; nothing here edits KiCad text.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def _root_paths(sch: Path) -> list[str]:
    root = write_layer.load(sch)
    out = []
    for si in (c for c in root.children if not c.is_atom and c.name == "sheet_instances"):
        for p in (c for c in si.children if not c.is_atom and c.name == "path"):
            out.append(p.children[0].value)
    return out


def _tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_new_project_root_sheet_instance_path_is_slash(tmp_path):
    made = project_toolset._project_create(str(tmp_path / "p"), "demo")
    sch = Path(made["schematic_path"])
    assert _root_paths(sch) == ["/"]
    # still a quoted string, with the page child kept
    text = sch.read_text(encoding="utf-8")
    assert '(path "/"' in text and '(page "1")' in text


def test_slash_survives_an_edit_round_trip_of_the_schematic_toolset(tmp_path):
    made = project_toolset._project_create(str(tmp_path / "p"), "demo")
    sch = Path(made["schematic_path"])
    tools = _tools()
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 50.8, "y": 40.64, "text": "NETA", "snap": False}])["status"] == "ok"
    assert _root_paths(sch) == ["/"]


def test_kicad_cli_erc_loads_the_new_project_with_no_violations(tmp_path):
    made = project_toolset._project_create(str(tmp_path / "p"), "demo")
    report = kicad_cli.run_erc(made["schematic_path"])
    assert report is not None
    violations = [v for s in report.get("sheets", []) for v in s.get("violations", [])]
    assert violations == [], json.dumps(violations)[:500]


def test_overwrite_also_writes_slash(tmp_path):
    project_toolset._project_create(str(tmp_path / "p"), "demo")
    made = project_toolset._project_create(str(tmp_path / "p"), "demo", overwrite=True)
    assert _root_paths(Path(made["schematic_path"])) == ["/"]


def test_the_guard_refuses_a_tree_without_exactly_one_root_path(tmp_path):
    from kicad_tools.schematic.models.schematic import Schematic

    good = Schematic(title="t", project_name="t").to_sexp()
    none = write_layer.parse_string(good)
    none.children[:] = [c for c in none.children if c.is_atom or c.name != "sheet_instances"]
    with pytest.raises(write_layer.WriteLayerError) as zero:
        project_toolset._set_root_sheet_instance_path(none)
    assert zero.value.code == "root_sheet_instance_missing"
    assert "found 0" in str(zero.value)

    two = write_layer.parse_string(good)
    si = next(c for c in two.children if not c.is_atom and c.name == "sheet_instances")
    si.children.append(si.children[0])
    with pytest.raises(write_layer.WriteLayerError) as many:
        project_toolset._set_root_sheet_instance_path(two)
    assert many.value.code == "root_sheet_instance_missing"
    assert "found 2" in str(many.value)


def test_a_refused_schematic_leaves_no_project_file_behind(tmp_path, monkeypatch):
    def broken(root):
        raise write_layer.WriteLayerError("root_sheet_instance_missing", "expected one, found 0; nothing was written")

    monkeypatch.setattr(project_toolset, "_set_root_sheet_instance_path", broken)
    with pytest.raises(write_layer.WriteLayerError):
        project_toolset._project_create(str(tmp_path / "p"), "demo")
    assert not list((tmp_path / "p").glob("demo.*"))
