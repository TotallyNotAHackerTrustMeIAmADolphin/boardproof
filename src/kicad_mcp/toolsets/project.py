"""Project scaffolding toolset: create/open KiCad projects.

Built entirely on top of `kicad-tools` (see docs/ARCHITECTURE.md — depend on it
rather than reimplement KiCad file I/O): `create_minimal_project` for the
`.kicad_pro` JSON project file (saved by the write layer's `save_project`), the high-level
`kicad_tools.schematic.models.schematic.Schematic` builder for a blank
schematic, and `kicad_tools.schema.PCB.create` for a blank board (saved by the write layer).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from kicad_mcp import write_layer


def _project_create(
    directory: str,
    name: str,
    width_mm: float = 100.0,
    height_mm: float = 100.0,
    layers: int = 2,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Scaffold a new KiCad project: a .kicad_pro, a blank schematic, and a blank PCB.

    Refuses to touch an existing project at the same path unless `overwrite`
    is explicitly True -- see archived backlog P1-13: this used to silently
    replace all three files (destroying any edits made since they were
    created), bypassing the dry-run/confirm pattern the rest of the
    architecture relies on for destructive operations.

    Args:
        directory: Directory to create the project in (created if missing).
        name: Project name — used as the base filename for all three files.
        width_mm: Initial PCB board outline width in millimeters.
        height_mm: Initial PCB board outline height in millimeters.
        layers: Number of copper layers on the initial PCB.
        overwrite: Must be True to replace an already-existing project at
            this path. Defaults to False so the common case (a genuinely
            new path) stays a single plain call while an existing project
            is protected by default.
    """
    from kicad_tools.core.project_file import create_minimal_project
    from kicad_tools.schema import PCB
    from kicad_tools.schematic.models.schematic import Schematic

    dir_path = Path(directory)
    dir_path.mkdir(parents=True, exist_ok=True)

    project_path = dir_path / f"{name}.kicad_pro"
    schematic_path = dir_path / f"{name}.kicad_sch"
    pcb_path = dir_path / f"{name}.kicad_pcb"

    existing = [p for p in (project_path, schematic_path, pcb_path) if p.exists()]
    if existing and not overwrite:
        return {
            "status": "error",
            "reason": (
                f"project already exists at {directory}/{name}.* "
                f"({', '.join(p.name for p in existing)}); pass overwrite=True to replace it"
            ),
        }

    project_data = create_minimal_project(name)
    write_layer.save_project(project_data, project_path)

    schematic = Schematic(title=name, project_name=name)
    schematic.write(schematic_path)

    pcb = PCB.create(width=width_mm, height=height_mm, layers=layers, title=name)
    write_layer.save_pcb(pcb, pcb_path)

    return {
        "status": "created",
        "name": name,
        "project_path": str(project_path),
        "schematic_path": str(schematic_path),
        "pcb_path": str(pcb_path),
    }


def _project_open(path: str) -> dict[str, Any]:
    """Open an existing KiCad project and summarize its schematic and PCB.

    Args:
        path: Path to a directory containing a .kicad_pro, or the .kicad_pro
            file itself.
    """
    from kicad_tools.schema import PCB
    from kicad_tools.schema import Schematic as SchematicFile

    input_path = Path(path)

    if input_path.is_dir():
        candidates = sorted(input_path.glob("*.kicad_pro"))
        if not candidates:
            return {"error": f"no .kicad_pro file found in directory: {path}"}
        project_path = candidates[0]
    elif input_path.suffix == ".kicad_pro":
        project_path = input_path
    else:
        return {"error": f"expected a directory or a .kicad_pro path, got: {path}"}

    if not project_path.exists():
        return {"error": f"project file does not exist: {project_path}"}

    name = project_path.stem
    schematic_path = project_path.with_suffix(".kicad_sch")
    pcb_path = project_path.with_suffix(".kicad_pcb")

    result: dict[str, Any] = {
        "status": "opened",
        "name": name,
        "project_path": str(project_path),
        "schematic_path": None,
        "pcb_path": None,
    }

    if schematic_path.exists():
        from kicad_mcp import sheet_tree

        # The whole sheet tree from the root, never the root file alone and never a glob
        # (build ticket 26): symbol_count counts every placed instance, power symbols included.
        SchematicFile.load(schematic_path)  # the root must load
        tree = sheet_tree.walk(schematic_path)
        placed = sheet_tree.placed_references(tree)
        result["schematic_path"] = str(schematic_path)
        result["symbol_count"] = len(placed)
        result["references"] = sorted({ref for ref, _ in placed if not ref.startswith("#")})
        result.update(tree.report())
        if tree.problems:  # a sheet the walk could not read: the summary is not the project's
            result["status"] = "incomplete"
            result["reason"] = "the sheet tree is incomplete: " + "; ".join(tree.problems)
    if pcb_path.exists():
        pcb = PCB.load(pcb_path)
        result["pcb_path"] = str(pcb_path)
        result["footprint_count"] = pcb.footprint_count
        result["net_count"] = pcb.net_count

    return result


def build(mcp: MCPServer) -> list[str]:
    """Register this toolset's tools onto `mcp`. Returns the registered tool names."""
    mcp.add_tool(
        _project_create,
        name="project_create",
        description="Scaffold a new KiCad project: .kicad_pro, blank .kicad_sch, blank .kicad_pcb.",
        annotations={
            # archived backlog P1-13: calling this again at an existing path now
            # refuses by default (returns status="error") instead of
            # silently overwriting -- destructiveHint stays True because
            # the tool *can* still replace an existing project when the
            # caller explicitly passes overwrite=True, which is exactly
            # what this hint exists to warn about. idempotentHint stays
            # True: repeated calls with the same arguments always leave
            # the same end state (either "created once, then refused" or,
            # with overwrite=True, "replaced with the same fresh scaffold
            # every time") -- it never leaves things in a different state
            # depending on how many times it's called.
            "readOnlyHint": False,
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        _project_open,
        name="project_open",
        description="Open an existing KiCad project and summarize its schematic/PCB contents.",
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    return ["project_create", "project_open"]
