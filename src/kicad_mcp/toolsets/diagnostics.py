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

from kicad_mcp.kicad_cli import KicadCliError, export_sch_pdf, export_svg
from kicad_mcp.png_render import PngRenderError, render_pdf_pages

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


_PNG_DPI_MIN, _PNG_DPI_MAX = 50, 600


def _png_pages(sch_path: Path, output_dir: Path, dpi: int) -> list[str]:
    """Rasterise the schematic's PDF export (one PNG per sheet) at ``dpi``.

    The raster step runs in a child process (kicad_mcp.png_render): PDFium is not thread-safe
    and a native crash there must not take the server down (build ticket 104).
    """
    pdf = export_sch_pdf(sch_path, output_dir / f"{sch_path.stem}.pdf")
    return render_pdf_pages(pdf, output_dir, sch_path.stem, dpi)


def build(mcp: MCPServer) -> list[str]:
    def render_board_diagnostic(pcb_path: str, output_dir: str, layers: str | None = None) -> dict:
        """Render a .kicad_pcb to SVG for human/vision-assisted sanity checking.

        NOT a correctness check -- see pcb_run_drc / pcb_check_collisions for
        that. This is for catching things geometry checks can't, e.g.
        silkscreen legibility or an obviously-wrong-looking layout.
        """
        kwargs = {"layers": layers} if layers else {}
        return _render(pcb_path, output_dir, _PCB_NOTE, **kwargs)

    def render_schematic_diagnostic(sch_path: str, output_dir: str, png_dpi: int | None = None) -> dict:
        """Render a .kicad_sch to SVG for human/vision-assisted sanity checking.

        Optional png_dpi (50-600) also writes one PNG per sheet at that
        resolution (A4 at 200 dpi is about 2339 x 1654 px) under png_images.

        NOT a correctness check -- see sch_run_erc for that. This is for
        catching things ERC can't, e.g. wiring that's electrically valid but
        visually confusing.
        """
        if png_dpi is not None and not (_PNG_DPI_MIN <= png_dpi <= _PNG_DPI_MAX):
            return {
                "status": "incomplete",
                "reason": f"png_dpi must be between {_PNG_DPI_MIN} and {_PNG_DPI_MAX}, got {png_dpi}",
                "authoritative": False,
            }
        result = _render(sch_path, output_dir, _SCH_NOTE)
        if png_dpi is None or result["status"] != "ok":
            return result
        try:
            result["png_images"] = _png_pages(Path(sch_path), Path(output_dir), png_dpi)
        except KicadCliError as exc:
            return {"status": "incomplete", "reason": str(exc), "authoritative": False}
        except PngRenderError as exc:
            # The SVGs (and the PDF, where kicad-cli wrote it) stay returned.
            pdf = Path(output_dir) / f"{Path(sch_path).stem}.pdf"
            refusal = {
                "status": "incomplete",
                "refusal": "png_render_failed",
                "reason": f"png_render_failed: {exc}",
                "images": result["images"],
                "authoritative": False,
            }
            if pdf.is_file():
                refusal["pdf"] = str(pdf)
            return refusal
        return result

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
            "returns authoritative: false: an image is never evidence. The SVG is A4-sized "
            "(about 1123 x 794 px); pass png_dpi (50-600, e.g. 200) to also get one PNG per sheet "
            "at that resolution in png_images. sch_run_erc lists ERC "
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
