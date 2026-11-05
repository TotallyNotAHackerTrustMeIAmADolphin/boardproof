"""Subprocess entry: render page regions of a datasheet PDF to PNG files (build ticket 105).

Run as ``python -I <this file> <pdf> <out_dir> <dpi> <regions_json>``. ``regions_json`` is a list of
``[page_number, bbox_or_null]`` (1-based page, bbox ``[x0, top, x1, bottom]`` in PDF points). It
imports only the standard library and pdfplumber, never the server, so a native crash of PDFium
(pypdfium2) kills this process and nothing else. Region ``k`` is written as ``region-<k>.png`` (a
whole PNG is written at once, and the caller accepts a file only when it ends with the PNG IEND
chunk, so a process killed mid-write leaves a file that is rejected, never a truncated image). It prints ``{"rendered": [k, ...], "errors": {"k": "reason"}}`` on
stdout; a Python-level failure of one region is an entry in ``errors`` and the loop goes on.
"""

import json
import os
import struct
import sys
import zlib

_PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")


def _png_bytes(img) -> bytes:
    """PNG of an RGB image, encoded here (zlib + struct), so no image-library save call is needed."""
    width, height = img.size
    raw = img.tobytes()
    stride = width * 3
    scan = b"".join(b"\x00" + raw[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        _PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scan, 6))
        + chunk(b"IEND", b"")
    )


def main(argv: list[str]) -> int:
    pdf_path, out_dir, dpi, regions = argv[1], argv[2], int(argv[3]), json.loads(argv[4])

    import pdfplumber

    rendered: list[int] = []
    errors: dict[str, str] = {}
    with pdfplumber.open(pdf_path) as doc:
        for k, (page_no, bbox) in enumerate(regions):
            try:
                area = doc.pages[page_no - 1]
                if bbox is not None:
                    area = area.crop(tuple(bbox))
                data = _png_bytes(area.to_image(resolution=dpi).original.convert("RGB"))
                with open(os.path.join(out_dir, f"region-{k}.png"), "wb") as fh:
                    fh.write(data)
                rendered.append(k)
            except Exception as exc:  # noqa: BLE001 -- one bad region must not hide the others
                errors[str(k)] = f"{type(exc).__name__}: {exc}"
    sys.stdout.write(json.dumps({"rendered": rendered, "errors": errors}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
