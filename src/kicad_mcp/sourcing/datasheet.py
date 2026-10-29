"""Datasheet fetch + inspection (archived backlog P1-08).

Downloads a candidate part's datasheet PDF and extracts structured
content (pin tables, absolute maximum ratings, package dimensions) so
component selection can verify a part actually matches spec instead of
trusting a parametric-search match alone -- see docs/ARCHITECTURE.md
"Component sourcing". Fully automated (no human checkpoint here); an
inconclusive extraction just reports itself as such, evidence-gated,
the same shape toolsets/verification.py already uses -- the ambiguous
-match checkpoint that reacts to that is a separate ticket (P1-09).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import httpx
import pdfplumber

_PIN_TABLE_HEADER_RE = re.compile(r"\bpin\b", re.IGNORECASE)
_RATINGS_HEADER_RE = re.compile(r"absolute maximum rating|maximum rating", re.IGNORECASE)
_DIMENSION_HEADER_RE = re.compile(r"package|dimension|land pattern|footprint", re.IGNORECASE)

# datasheet_url comes from third-party provider data (PartResult.datasheet_url,
# populated by scraped/API sourcing results) -- treat it as untrusted input.
# Security fix (2026-09-02, from an independent review that verified
# https://x/a/..\..\evil.pdf produced a filename that escaped output_dir):
# cap response size and only accept http(s) URLs.
_MAX_DATASHEET_BYTES = 50 * 1024 * 1024  # 50MB -- generous for a datasheet PDF


def fetch_datasheet(url: str, output_dir: str | Path) -> dict[str, Any]:
    """Download a datasheet PDF to output_dir. Evidence-gated: a bad URL,
    timeout, or non-PDF response is reported explicitly, never raised
    unhandled and never written to disk as if it were a real PDF.
    """
    output_dir = Path(output_dir)
    if not url.lower().startswith(("http://", "https://")):
        return {"status": "incomplete", "reason": f"unsupported URL scheme: {url!r}"}

    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {"status": "incomplete", "reason": f"could not create output_dir: {exc}"}

    try:
        with httpx.stream(
            "GET",
            url,
            timeout=30.0,
            follow_redirects=True,
            headers={"User-Agent": "kicad-mcp/0.1 (+datasheet-fetch)"},
        ) as response:
            if response.status_code != 200:
                return {
                    "status": "incomplete",
                    "reason": f"HTTP {response.status_code} fetching datasheet",
                    "url": url,
                }

            content_type = response.headers.get("content-type", "")
            # Path(...).name strips any directory components _filename_from_url
            # might still contain -- defense in depth even though
            # _filename_from_url already sanitizes, so dest can never land
            # outside output_dir regardless of how the filename was derived.
            filename = Path(_filename_from_url(url)).name
            dest = output_dir / filename

            first_chunk = True
            magic_ok = True
            bytes_written = 0
            with dest.open("wb") as fh:
                for chunk in response.iter_bytes():
                    if first_chunk:
                        magic_ok = chunk.startswith(b"%PDF-")
                        first_chunk = False
                    bytes_written += len(chunk)
                    if bytes_written > _MAX_DATASHEET_BYTES:
                        fh.close()
                        dest.unlink(missing_ok=True)
                        return {
                            "status": "incomplete",
                            "reason": f"response exceeded {_MAX_DATASHEET_BYTES} byte cap, aborted",
                            "url": url,
                        }
                    fh.write(chunk)

            if not magic_ok:
                dest.unlink(missing_ok=True)
                return {
                    "status": "incomplete",
                    "reason": (
                        f"response was not a PDF (content-type={content_type!r}); "
                        "likely a bot-block/redirect page rather than the real document"
                    ),
                    "url": url,
                }

            return {
                "status": "ok",
                "path": str(dest),
                "bytes": dest.stat().st_size,
                "content_type": content_type,
            }
    except httpx.RequestError as exc:
        return {"status": "incomplete", "reason": f"request failed: {exc}", "url": url}


def inspect_datasheet(pdf_path: str | Path, query: str | None = None) -> dict[str, Any]:
    """Extract structured content from a datasheet PDF: pin tables,
    absolute-maximum-ratings tables, and package/dimension tables where
    present. `query` (optional) is matched case-insensitively against
    extracted table/section text to filter which sections are returned,
    for a targeted look (e.g. query="VDD" to find just the supply-voltage
    rating) rather than dumping every table.
    """
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        return {"status": "incomplete", "reason": f"file not found: {pdf_path}"}

    try:
        pin_tables: list[dict] = []
        ratings_tables: list[dict] = []
        dimension_tables: list[dict] = []
        text_chars = 0

        with pdfplumber.open(pdf_path) as pdf:
            page_count = len(pdf.pages)
            for page_index, page in enumerate(pdf.pages):
                page_text = page.extract_text() or ""
                text_chars += len(page_text)

                for table in page.find_tables():
                    rows = table.extract()
                    if not rows or not any(any(cell for cell in row) for row in rows):
                        continue
                    header_context = _table_header_context(page_text, table)
                    entry = {"page": page_index + 1, "rows": rows}
                    if _PIN_TABLE_HEADER_RE.search(header_context):
                        pin_tables.append(entry)
                    if _RATINGS_HEADER_RE.search(header_context):
                        ratings_tables.append(entry)
                    if _DIMENSION_HEADER_RE.search(header_context):
                        dimension_tables.append(entry)

        if text_chars == 0:
            return {
                "status": "incomplete",
                "reason": "no extractable text (scanned/image-only PDF)",
                "page_count": page_count,
            }

        if query:
            q = query.lower()
            pin_tables = [t for t in pin_tables if _table_contains(t, q)]
            ratings_tables = [t for t in ratings_tables if _table_contains(t, q)]
            dimension_tables = [t for t in dimension_tables if _table_contains(t, q)]

        found_anything = bool(pin_tables or ratings_tables or dimension_tables)
        return {
            "status": "ok" if found_anything else "no_structured_tables_found",
            "page_count": page_count,
            "pin_tables": pin_tables,
            "absolute_max_ratings_tables": ratings_tables,
            "package_dimension_tables": dimension_tables,
        }
    except Exception as exc:  # noqa: BLE001 -- pdfplumber can raise on malformed PDFs; report, don't crash
        return {"status": "incomplete", "reason": f"extraction failed: {exc}"}


def _filename_from_url(url: str) -> str:
    # url is untrusted (third-party provider data) -- normalize backslashes
    # to forward slashes first so a Windows-style traversal sequence
    # ("..\\..\\evil.pdf") doesn't survive the split, then take only the
    # last path segment.
    name = url.rsplit("/", 1)[-1].split("?", 1)[0]
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = Path(name).name  # belt-and-suspenders: strip anything Path still considers a directory component
    if not name or name in {".", ".."}:
        name = "datasheet"
    if not name.lower().endswith(".pdf"):
        name = name + ".pdf"
    return name


def _table_header_context(page_text: str, table) -> str:
    """A small window of page text above the table's position, used to
    classify what kind of table this is (pin table vs. ratings vs.
    dimensions) by nearby heading text rather than by cell contents alone.
    """
    return page_text[:400]


def _table_contains(table_entry: dict, needle: str) -> bool:
    for row in table_entry["rows"]:
        for cell in row:
            if cell and needle in str(cell).lower():
                return True
    return False
