"""Visual diagnostic toolset -- archived backlog "Phase 3.5".

Explicitly a *secondary, non-authoritative* aid per docs/ARCHITECTURE.md's
"geometry over vision" principle. Rendering a board/schematic and looking
at it can catch things geometric checks structurally can't (silkscreen
legibility, overall layout sanity, visually-confusing-but-electrically-
valid wiring) -- but it is never a substitute for pcb_run_drc/sch_run_erc/
pcb_check_collisions, which is why every result here carries an explicit
authoritative: false field.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from kicad_mcp.kicad_cli import KicadCliError, export_svg

_PCB_NOTE = (
    "A human-style visual sanity check only, never evidence. pcb_run_drc and "
    "pcb_check_collisions list problems; only verify_board gives a verdict on the board."
)
_SCH_NOTE = (
    "A human-style visual sanity check only, never evidence. sch_run_erc lists ERC "
    "violations; only verify_board gives a verdict on the board."
)


def _render(input_path: str | Path, output_dir: str | Path, note: str, **extra_args: str) -> dict:
    input_path = Path(input_path)
    if not input_path.is_file():
        return {"status": "incomplete", "reason": f"file not found: {input_path}", "authoritative": False}
    try:
        out_dir = export_svg(input_path, output_dir, **extra_args)
    except KicadCliError as exc:
        return {"status": "incomplete", "reason": str(exc), "authoritative": False}
    images = sorted(str(p) for p in Path(out_dir).glob("*.svg"))
    if not images:
        return {
            "status": "incomplete",
            "reason": "kicad-cli reported success but no .svg files were found in the output directory",
            "authoritative": False,
        }
    return {"status": "ok", "images": images, "authoritative": False, "note": note}


def build(mcp: MCPServer) -> list[str]:
    def render_board_diagnostic(pcb_path: str, output_dir: str, layers: str | None = None) -> dict:
        """Render a .kicad_pcb to SVG for human/vision-assisted sanity checking.

        NOT a correctness check -- see pcb_run_drc / pcb_check_collisions for
        that. This is for catching things geometry checks can't, e.g.
        silkscreen legibility or an obviously-wrong-looking layout.
        """
        kwargs = {"layers": layers} if layers else {}
        return _render(pcb_path, output_dir, _PCB_NOTE, **kwargs)

    def render_schematic_diagnostic(sch_path: str, output_dir: str) -> dict:
        """Render a .kicad_sch to SVG for human/vision-assisted sanity checking.

        NOT a correctness check -- see sch_run_erc for that. This is for
        catching things ERC can't, e.g. wiring that's electrically valid but
        visually confusing.
        """
        return _render(sch_path, output_dir, _SCH_NOTE)

    mcp.add_tool(
        render_board_diagnostic,
        name="render_board_diagnostic",
        description=(
            "Render a PCB to SVG image(s) as a non-authoritative, human-style visual "
            "sanity check (silkscreen legibility, a layout that looks wrong). Always returns "
            "authoritative: false: an image is never evidence. pcb_run_drc and "
            "pcb_check_collisions list problems; neither is a verdict. Only verify_board gives "
            "one, and only its PASS may be reported as routed or done."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            # Re-running with the same output_dir overwrites the same SVG
            # filenames with equivalent content for an unchanged board, so
            # this is idempotent in the sense that matters (no accumulating
            # side effects) even though a literal new file write occurs.
            "idempotentHint": True,
            "openWorldHint": True,  # shells out to the external kicad-cli process
        },
    )
    mcp.add_tool(
        render_schematic_diagnostic,
        name="render_schematic_diagnostic",
        description=(
            "Render a schematic to SVG image(s) as a non-authoritative, human-style "
            "visual sanity check (wiring that is electrically valid but hard to read). Always "
            "returns authoritative: false: an image is never evidence. sch_run_erc lists ERC "
            "violations; it is not a verdict either. Only verify_board gives a verdict on the "
            "board."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    return ["render_board_diagnostic", "render_schematic_diagnostic"]
