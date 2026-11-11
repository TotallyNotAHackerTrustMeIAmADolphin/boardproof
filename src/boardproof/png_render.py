"""Rasterise a PDF to PNGs in a child process (build ticket 104).

pypdfium2/PDFium is not thread-safe and a native fault in it ends the whole process, and
FastMCP runs sync tools on worker threads. So the raster step runs in ``python -I`` on a script
inside this package (no import of the server, no code from the working directory), under its own
timeout and in its own temp directory, and one process-wide lock keeps parallel calls from
spawning parallel renders. A crash or a timeout becomes a :class:`PngRenderError` with a plain
reason; the caller turns it into a ``png_render_failed`` refusal.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

PNG_TIMEOUT_SECONDS = 120
_WORKER = Path(__file__).with_name("_png_worker.py")
_CROP_WORKER = Path(__file__).with_name("_png_crop_worker.py")
_PNG_IEND = bytes.fromhex("0000000049454e44ae426082")  # last chunk of every whole PNG
# One lock for every PDFium user in the process (also sourcing/datasheet.py's to_image).
PDFIUM_LOCK = threading.Lock()


class PngRenderError(RuntimeError):
    """The PNG renderer crashed, timed out or reported nothing usable."""


def render_pdf_pages(pdf: str | Path, output_dir: str | Path, stem: str, dpi: int) -> list[str]:
    """Render every page of ``pdf`` to ``<output_dir>/<stem>-page<N>.png``; return the paths."""
    output_dir = Path(output_dir)
    with PDFIUM_LOCK, tempfile.TemporaryDirectory(prefix="png_render_") as tmp:
        cmd = [sys.executable, "-I", str(_WORKER), str(pdf), tmp, stem, str(int(dpi))]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=PNG_TIMEOUT_SECONDS
            )
        except subprocess.TimeoutExpired as exc:
            raise PngRenderError(f"PNG renderer timed out after {PNG_TIMEOUT_SECONDS}s") from exc
        except OSError as exc:
            raise PngRenderError(f"PNG renderer could not start: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip().splitlines()[-3:]
            raise PngRenderError(
                f"PNG renderer exited with code {proc.returncode}" + (": " + " | ".join(tail) if tail else "")
            )
        try:
            names = [Path(p).name for p in json.loads(proc.stdout)]
        except (ValueError, TypeError) as exc:
            raise PngRenderError("PNG renderer printed no usable page list") from exc
        if not names or not all((Path(tmp) / n).is_file() for n in names):
            raise PngRenderError("PNG renderer wrote no PNG files")
        moved: list[Path] = []
        try:
            output_dir.mkdir(parents=True, exist_ok=True)
            for name in names:
                dest = output_dir / name
                shutil.move(str(Path(tmp) / name), str(dest))
                moved.append(dest)
        except OSError as exc:
            for done in moved:  # all or nothing: take back what already moved
                try:
                    done.unlink(missing_ok=True)
                except OSError:
                    pass  # the refusal below still names the original failure
            raise PngRenderError(f"PNG files could not be written to {output_dir}: {exc}") from exc
        return [str(p) for p in moved]


class RegionRender:
    """Result of :func:`render_pdf_regions`: ``images[k]`` is the PNG bytes of region ``k`` or None;
    ``failures`` maps a failed region's index to its reason (every None has one)."""

    def __init__(self, count: int) -> None:
        self.images: list[bytes | None] = [None] * count
        self.failures: dict[int, str] = {}


def render_pdf_regions(
    pdf: str | Path, regions: list[tuple[int, tuple[float, float, float, float] | None]], dpi: int
) -> RegionRender:
    """Render ``[(page_number, bbox_or_None), ...]`` of ``pdf`` to PNG bytes in a child process.

    Same isolation as :func:`render_pdf_pages` (``python -I``, timeout, process-wide lock). Never
    raises for a render problem: a crash, timeout or Python error of the child leaves the regions
    that were finished (a whole region file survives a later crash; a truncated one is rejected) and gives every other region
    a reason in ``failures``, so nothing is dropped silently."""
    out = RegionRender(len(regions))
    if not regions:
        return out
    with PDFIUM_LOCK, tempfile.TemporaryDirectory(prefix="png_render_") as tmp:
        payload = json.dumps([[p, list(b) if b is not None else None] for p, b in regions])
        cmd = [sys.executable, "-I", str(_CROP_WORKER), str(pdf), tmp, str(int(dpi)), payload]
        child_errors: dict[str, str] = {}
        fault: str | None = None
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, check=False, timeout=PNG_TIMEOUT_SECONDS
            )
            if proc.returncode != 0:
                tail = (proc.stderr or "").strip().splitlines()[-3:]
                fault = f"PNG renderer exited with code {proc.returncode}" + (
                    ": " + " | ".join(tail) if tail else ""
                )
            else:
                try:
                    child_errors = {str(k): str(v) for k, v in json.loads(proc.stdout)["errors"].items()}
                except (ValueError, TypeError, KeyError, AttributeError):
                    fault = "PNG renderer printed no usable result"
        except subprocess.TimeoutExpired:
            fault = f"PNG renderer timed out after {PNG_TIMEOUT_SECONDS}s"
        except OSError as exc:
            fault = f"PNG renderer could not start: {exc}"
        for k in range(len(regions)):
            png = Path(tmp) / f"region-{k}.png"
            try:
                data = png.read_bytes() if png.is_file() else b""
            except OSError:
                data = b""
            if data.startswith(b"\x89PNG") and data.endswith(_PNG_IEND):
                out.images[k] = data
            else:
                out.failures[k] = child_errors.get(str(k)) or fault or "PNG renderer wrote no PNG for this region"
    return out
