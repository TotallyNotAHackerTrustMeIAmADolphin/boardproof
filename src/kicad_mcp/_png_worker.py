"""Subprocess entry: rasterise a PDF to one PNG per page (build ticket 104).

Run as ``python -I <this file> <pdf> <out_dir> <stem> <dpi>``. It imports only the standard
library and pdfplumber, never the server, so a native crash of PDFium (pypdfium2) kills this
process and nothing else. It prints the written PNG paths as one JSON list on stdout.
"""

import json
import sys


def main(argv: list[str]) -> int:
    pdf, out_dir, stem, dpi = argv[1], argv[2], argv[3], int(argv[4])
    import os

    import pdfplumber

    out: list[str] = []
    with pdfplumber.open(pdf) as doc:
        for i, page in enumerate(doc.pages, start=1):
            png = os.path.join(out_dir, f"{stem}-page{i}.png")
            page.to_image(resolution=dpi).save(png)
            out.append(png)
    sys.stdout.write(json.dumps(out))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
