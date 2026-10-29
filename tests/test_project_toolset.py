from pathlib import Path

from mcp.server.mcpserver import MCPServer

from kicad_mcp.toolsets import project as project_toolset


def _build_tools():
    mcp = MCPServer("test")
    names = project_toolset.build(mcp)
    return mcp, names


def test_build_registers_expected_tools():
    mcp, names = _build_tools()
    assert names == ["project_create", "project_open"]


def test_project_create_writes_all_three_files(tmp_path: Path):
    result = project_toolset._project_create(str(tmp_path), "demo")

    assert result["status"] == "created"
    assert Path(result["project_path"]).is_file()
    assert Path(result["schematic_path"]).is_file()
    assert Path(result["pcb_path"]).is_file()

    assert Path(result["project_path"]).name == "demo.kicad_pro"
    assert Path(result["schematic_path"]).name == "demo.kicad_sch"
    assert Path(result["pcb_path"]).name == "demo.kicad_pcb"


def test_project_open_reads_back_created_project(tmp_path: Path):
    created = project_toolset._project_create(str(tmp_path), "demo2")

    opened = project_toolset._project_open(str(tmp_path))

    assert opened["status"] == "opened"
    assert opened["name"] == "demo2"
    assert opened["project_path"] == created["project_path"]
    assert opened["schematic_path"] == created["schematic_path"]
    assert opened["pcb_path"] == created["pcb_path"]
    assert opened["symbol_count"] == 0
    assert opened["footprint_count"] == 0
    # kicad-tools' blank PCB always has the implicit net 0 ("no net")
    assert opened["net_count"] == 1


def test_project_open_accepts_direct_pro_path(tmp_path: Path):
    created = project_toolset._project_create(str(tmp_path), "demo3")

    opened = project_toolset._project_open(created["project_path"])

    assert opened["status"] == "opened"
    assert opened["name"] == "demo3"


def test_project_open_missing_pro_file_reports_error(tmp_path: Path):
    result = project_toolset._project_open(str(tmp_path))
    assert "error" in result


def test_project_create_refuses_to_overwrite_by_default(tmp_path: Path):
    first = project_toolset._project_create(str(tmp_path), "demo4")
    project_path = Path(first["project_path"])
    schematic_path = Path(first["schematic_path"])
    pcb_path = Path(first["pcb_path"])
    original_mtimes = {p: p.stat().st_mtime_ns for p in (project_path, schematic_path, pcb_path)}

    second = project_toolset._project_create(str(tmp_path), "demo4", width_mm=200.0)

    assert second["status"] == "error"
    assert "overwrite" in second["reason"]
    for path, mtime in original_mtimes.items():
        assert path.stat().st_mtime_ns == mtime, f"{path} was touched despite the refusal"


def test_project_create_overwrite_true_replaces_existing_project(tmp_path: Path):
    first = project_toolset._project_create(str(tmp_path), "demo5", width_mm=100.0)

    second = project_toolset._project_create(str(tmp_path), "demo5", width_mm=250.0, overwrite=True)

    assert second["status"] == "created"
    assert second["project_path"] == first["project_path"]
    opened = project_toolset._project_open(str(tmp_path))
    assert opened["status"] == "opened"
