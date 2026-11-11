"""render_region: one rectangular area of a schematic sheet as a PNG at a stated resolution (GitHub issue 98).

kicad-cli has no PNG export for schematics, so the sheet is exported to PDF (on a scratch copy of its sheet tree:
kicad-cli writes beside its input) and the area is cropped and rasterised by ``png_render.render_pdf_regions`` in a
child process, the isolation the whole-page PNGs of ``render_schematic_diagnostic`` use (issue 104).

The coordinate mapping, measured with kicad-cli 10.0.7 and proven in ``tests/test_render_region.py`` against the raw
wire coordinates of a sheet: the PDF page is the paper (A4: 841.896 x 595.296 points), its origin is the top-left
corner of the paper, x grows to the right and y downwards like the schematic's own coordinates, and one schematic
millimetre is ``72 / 25.4`` points. A wire from (50, 50) to (100, 50) mm is drawn from (141.73, 141.73) to
(283.46, 141.73) points. So a region in sheet millimetres is the crop box ``(x1, y1, x2, y2) * 72 / 25.4`` and needs no
other offset (the drawing sheet frame is part of the page, not a shift of the coordinates).
"""

from __future__ import annotations

import math
import re
import struct
import tempfile
from pathlib import Path
from typing import Any

from boardproof import kicad_cli, png_render, sheet_authoring, sheet_tree
from boardproof.sheet_authoring import SheetAuthoringError

POINTS_PER_MM = 72.0 / 25.4
DPI_MIN, DPI_MAX = 50, 600
MAX_PIXELS = 30_000_000
_MEDIABOX = re.compile(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]")


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SheetAuthoringError("invalid_spec", f"{name} must be a finite number of millimetres; got {value!r}", **{name: value})
    return float(value)


def _page_size_mm(pdf: Path) -> tuple[float, float] | None:
    """The first page's size in mm from its MediaBox, or None when the PDF does not state it in the plain form."""
    match = _MEDIABOX.search(pdf.read_bytes()[:2_000_000])
    if match is None:
        return None
    x0, y0, x1, y1 = (float(g) for g in match.groups())
    return (x1 - x0) / POINTS_PER_MM, (y1 - y0) / POINTS_PER_MM


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from the IHDR chunk of a PNG."""
    return struct.unpack(">II", data[16:24])  # type: ignore[return-value]


def _name(stem: str, box: tuple[float, float, float, float], dpi: int) -> str:
    return f"{stem}-region-{'-'.join(f'{v:g}' for v in box)}-{dpi}dpi.png"


def render_region(sch_path: str, x1: Any, y1: Any, x2: Any, y2: Any, output_dir: str, dpi: Any = 300) -> dict[str, Any]:
    """Read-only for the project: the area ``(x1, y1)`` to ``(x2, y2)`` mm of the sheet file as a PNG in ``output_dir``."""
    try:
        box = (_number(x1, "x1"), _number(y1, "y1"), _number(x2, "x2"), _number(y2, "y2"))
        if box[2] <= box[0] or box[3] <= box[1]:
            raise SheetAuthoringError("invalid_spec", f"the region must have x2 > x1 and y2 > y1; got {list(box)}", region=list(box))
        if isinstance(dpi, bool) or not isinstance(dpi, int) or not DPI_MIN <= dpi <= DPI_MAX:
            raise SheetAuthoringError("invalid_spec", f"dpi must be an integer from {DPI_MIN} to {DPI_MAX}; got {dpi!r}", dpi=dpi)
        file = Path(sch_path)
        if not file.is_file() or file.suffix != ".kicad_sch":
            raise SheetAuthoringError("file_not_found", f"sheet not found: {file}")
        pixels = (box[2] - box[0]) / 25.4 * dpi * (box[3] - box[1]) / 25.4 * dpi
        if not math.isfinite(pixels) or pixels > MAX_PIXELS:
            size = "an unbounded number of" if not math.isfinite(pixels) else f"about {pixels / 1e6:.0f} million"
            raise SheetAuthoringError(
                "region_too_large", f"the region would be {size} pixels at {dpi} dpi; the limit is {MAX_PIXELS // 10**6} million: take a smaller region or a lower dpi",
                pixels=int(pixels) if math.isfinite(pixels) else None, limit=MAX_PIXELS)
        tree = sheet_tree.walk(file)
        if not tree.files:
            raise SheetAuthoringError("sheet_tree_incomplete", f"{file.name} cannot be loaded: " + "; ".join(tree.problems), **tree.report())
    except SheetAuthoringError as exc:
        return {**sheet_authoring.refusal(exc), "authoritative": False}

    with tempfile.TemporaryDirectory(prefix="boardproof-render-region-") as tmp:
        try:
            root = sheet_authoring._copy_project(tree, [], Path(tmp) / "p")  # kicad-cli writes beside its input
            pdf = kicad_cli.export_sch_pdf(root, Path(tmp) / "sheet.pdf")
        except kicad_cli.KicadCliError as exc:
            return {"status": "incomplete", "ok": False, "authoritative": False, "reason": str(exc)}
        page = _page_size_mm(pdf)
        if page is None:  # fail closed: without the page size "inside the page" cannot be checked
            return {"status": "incomplete", "ok": False, "code": "page_size_unreadable", "authoritative": False,
                    "reason": "the page size of kicad-cli's PDF could not be read, so the region cannot be checked against the page; nothing was rendered"}
        if box[0] < 0 or box[1] < 0 or box[2] > page[0] + 1e-6 or box[3] > page[1] + 1e-6:
            return {"status": "incomplete", "ok": False, "code": "region_outside_page", "authoritative": False,
                    "reason": f"the region {list(box)} mm is not inside the page, which is {page[0]:.1f} x {page[1]:.1f} mm (origin top-left)",
                    "page_size_mm": [round(page[0], 1), round(page[1], 1)]}
        crop = tuple(v * POINTS_PER_MM for v in box)
        rendered = png_render.render_pdf_regions(pdf, [(1, crop)], dpi)  # type: ignore[arg-type]
    data = rendered.images[0]
    if data is None:
        return {"status": "incomplete", "ok": False, "code": "png_render_failed", "authoritative": False,
                "reason": f"png_render_failed: {rendered.failures.get(0, 'no PNG was produced')}"}
    out_dir = Path(output_dir)
    target = out_dir / _name(file.stem, box, dpi)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except OSError as exc:  # an output_dir that is a file, a read-only folder, a name too long
        return {"status": "incomplete", "ok": False, "code": "output_dir_unusable", "authoritative": False,
                "reason": f"the PNG could not be written to {out_dir}: {exc}"}
    width, height = png_size(data)
    return {
        "status": "ok", "ok": True, "authoritative": False, "png": str(target), "width_px": width, "height_px": height,
        "region_mm": list(box), "dpi": dpi, "sheet": file.name, "page": 1,
        "page_size_mm": [round(page[0], 1), round(page[1], 1)],
        "sheet_problems": list(tree.problems),
        "note": "A human-style visual check only, never evidence. The region is in the sheet's own millimetres (origin at the top-left corner "
                "of the page, y downwards, as in the .kicad_sch). Only verify_board gives a verdict on the board.",
    }
