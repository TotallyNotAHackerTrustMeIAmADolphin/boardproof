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
import struct
import zlib
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


def inspect_datasheet(
    pdf_path: str | Path,
    query: str | None = None,
    package: str | None = None,
    image_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Extract structured content from a datasheet PDF: pin tables,
    absolute-maximum-ratings tables, and package/dimension tables where
    present. `query` (optional) is matched case-insensitively against
    extracted table/section text to filter which sections are returned,
    for a targeted look (e.g. query="VDD" to find just the supply-voltage
    rating) rather than dumping every table.

    `package` (optional, e.g. "SOT-23") adds a `pin_numbering` entry: the
    pin number -> name table for that package variant, only when a table
    with recognised Pin/Name headers sits directly under a heading naming
    that package. Pin diagrams are usually images, so otherwise the entry
    says `machine_extracted: false` with an explicit reason and, when
    `image_dir` is given, PNG crops of the candidate pages for a reviewer;
    a pin name or number is never inferred (issue #39).
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
        result: dict[str, Any] = {
            "status": "ok" if found_anything else "no_structured_tables_found",
            "page_count": page_count,
            "pin_tables": pin_tables,
            "absolute_max_ratings_tables": ratings_tables,
            "package_dimension_tables": dimension_tables,
        }
        if package is not None:
            result["pin_numbering"] = _package_pin_numbering(pdf_path, package, image_dir)
        return result
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


_PIN_COL_RE = re.compile(r"^(pin(\s*(no\.?|number|#))?|no\.?|number|pad)$", re.IGNORECASE)
_NAME_COL_RE = re.compile(r"^(pin\s*)?(name|symbol|terminal)$", re.IGNORECASE)
_FUNC_COL_RE = re.compile(r"^(function|description|type|i/o)$", re.IGNORECASE)
_PIN_ID_RE = re.compile(r"^(\d{1,3}|[A-Z]{1,2}\d{1,2})$")
_MAX_IMAGE_PAGES = 4


_PNG_SIGNATURE = bytes.fromhex("89504e470d0a1a0a")
_PNG_FILTER_NONE = bytes([0])


def _png_bytes(area: Any) -> bytes:
    """PNG of a pdfplumber page or crop, encoded here (zlib + struct) from the
    rendered RGB pixels, so no image-library save call is needed."""
    img = area.to_image(resolution=150).original.convert("RGB")
    width, height = img.size
    raw = img.tobytes()
    stride = width * 3
    scan = b"".join(_PNG_FILTER_NONE + raw[y * stride : (y + 1) * stride] for y in range(height))

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    return (
        _PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(scan, 6))
        + chunk(b"IEND", b"")
    )


def _clean(cell: object) -> str:
    return " ".join(str(cell or "").split())


def _package_regex(package: str) -> re.Pattern[str]:
    parts = [re.escape(p) for p in re.split(r"[\s\-_]+", package.strip()) if p]
    body = r"[-\s_]?".join(parts)
    # "SOT-23" must not match the heading of "SOT-23-5" or "SOT-236"
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9]|-\d)", re.IGNORECASE)


_HEADING_WORDS = frozenset(
    "package pin pins configuration configurations description descriptions assignment assignments "
    "function functions diagram table top view pinout definition definitions no".split()
)


def _heading_names_only(line: str, pkg_re: re.Pattern[str]) -> bool:
    """True when the heading line names the requested package and nothing else
    that matters: no second package, and only pin-table vocabulary around it
    ("Not for SOT-23 use:" and "SOT-23, SOT-89 pin configuration" are refused).
    """
    match = pkg_re.search(line)
    if match is None:
        return False
    rest = line[: match.start()] + " " + line[match.end() :]
    # the requested name must not be the tail of a longer package token ("23" in "SOT-23")
    before = line[: match.start()]
    if before and re.search(r"[A-Za-z]{2,}[-\s]?$", before):
        return False
    words = re.findall(r"[A-Za-z]+", rest)
    return all(w.lower() in _HEADING_WORDS for w in words)


def _parse_pin_rows(rows: list[list[Any]]) -> list[dict[str, str]] | None:
    """Pins from a table whose header names a pin column and a name column;
    None when the header is not recognised or any data row is not a clean
    single pin identifier with a name (no row is ever repaired or guessed).
    """
    if len(rows) < 2:
        return None
    header = [_clean(c) for c in rows[0]]
    pin_i = next((i for i, h in enumerate(header) if _PIN_COL_RE.match(h)), None)
    name_i = next((i for i, h in enumerate(header) if _NAME_COL_RE.match(h)), None)
    if pin_i is None or name_i is None or pin_i == name_i:
        return None
    func_i = next((i for i, h in enumerate(header) if _FUNC_COL_RE.match(h)), None)
    pins: list[dict[str, str]] = []
    for row in rows[1:]:
        cells = [_clean(c) for c in row]
        if not any(cells):
            continue
        if max(pin_i, name_i) >= len(cells):
            return None
        pin, name = cells[pin_i], cells[name_i]
        if not _PIN_ID_RE.match(pin) or not name:
            return None
        if not all(str(row[i] or "").isprintable() and "(cid:" not in str(row[i] or "") for i in (pin_i, name_i)):
            return None  # a newline or control character inside a pin or name cell
        entry = {"pin": pin, "name": name}
        if func_i is not None and func_i < len(cells):
            entry["function"] = cells[func_i]
        pins.append(entry)
    seen = [p["pin"] for p in pins]
    if not pins or len(set(seen)) != len(seen):
        return None
    if all(p.isdigit() for p in seen) and sorted(int(p) for p in seen) != list(range(1, len(seen) + 1)):
        return None  # a gap (or a table that does not start at 1) is a truncated or partial list
    return pins


def _large_images(page: Any) -> bool:
    """True when the embedded images together cover at least a quarter of the page;
    a small logo or icon must not make the crop hide the page's real pin drawing."""
    if not page.images:
        return False
    x0 = min(i["x0"] for i in page.images)
    x1 = max(i["x1"] for i in page.images)
    top = min(i["top"] for i in page.images)
    bottom = max(i["bottom"] for i in page.images)
    return (x1 - x0) * (bottom - top) >= 0.25 * page.width * page.height


# Deliberately wider than the parser's _PIN_ID_RE / _PIN_COL_RE: the parser must stay strict (a
# row it accepts becomes a pin), while this predicate only decides whether a next-page table
# looks like pin data, where a false positive costs a "may continue" and a false negative
# lets foreign pins hide behind a verified table.
_PIN_LIKE_ID_RE = re.compile(r"^(\d{1,4}\.?|\d{1,3}[A-Z]|[A-Z]{1,2}\d{1,2}[A-Z]?|EP)$", re.IGNORECASE)
_PIN_LIKE_HEADER_RE = re.compile(
    r"^(pins?(\s*(no\.?|number|#|name))?|pin\s*/\s*pad|no\.?|number|pad|terminals?)$", re.IGNORECASE
)
_PIN_LIKE_ROWS = 3  # a title row or two may sit above the header


def _table_is_pin_like(rows: list[list[Any]]) -> bool:
    """True when one of the first rows of a table has a pin-column header cell or starts with a
    pin-like identifier (the first non-blank cell, so a blank leading cell does not hide it)."""
    for row in rows[:_PIN_LIKE_ROWS]:
        cells = [_clean(c) for c in row]
        if any(_PIN_LIKE_HEADER_RE.match(c) or _PIN_COL_RE.match(c) for c in cells):
            return True
        first = next((c for c in cells if c), "")
        if first and (_PIN_LIKE_ID_RE.match(first) or _PIN_ID_RE.match(first)):
            return True
    return False


def _may_continue(pdf: Any, page_index: int) -> bool:
    """True when ANY table on the next page is pin-like, whatever heading it sits under.

    Fail closed (issue #39, rounds 4 and 5): no attempt is made to tell a continuation of the
    first table from the table of another package or part, because any such heading
    heuristic can fail open. Every pin-like table on the next page counts, not only the first
    one, and for each table the first rows are examined (see _table_is_pin_like), so an
    ordering table above the pin table or a title row above its header does not hide it. A
    table is pin-like when one of its first rows has a pin-column header cell or starts with a
    pin-like identifier. The first table stands only when the next page has no table, only
    tables of empty cells, or only tables of other shapes.

    Known limits, not handled: only the page directly after the table is looked at, so a blank
    page between two parts (F2) hides a following pin table; a rotated page (F3) may yield
    tables whose cells are read in another order or not found. Both still give true."""
    if page_index + 1 >= len(pdf.pages):
        return False
    for table in pdf.pages[page_index + 1].find_tables():
        rows = table.extract()
        if rows and _table_is_pin_like(rows):
            return True
    return False


def _package_pin_numbering(pdf_path: Path, package: str, image_dir: str | Path | None) -> dict[str, Any]:
    """The pin table of one package variant, or an explicit not-extracted
    verdict plus page images. Only a table with a recognised Pin/Name header
    whose nearest line above names the package counts; anything else is
    reported as NOT machine-extracted so a gate refuses it (issue #39).
    """
    if not re.search(r"[A-Za-z0-9]", package or ""):
        return {
            "package": package,
            "machine_extracted": False,
            "pins": [],
            "page_images": [],
            "reason": "pin numbering NOT machine-extracted: the package name is blank or has no letters or digits",
        }
    pkg_re = _package_regex(package)
    candidates: list[tuple[int, list[dict[str, str]]]] = []
    continued = False
    mention_pages: list[int] = []
    with pdfplumber.open(pdf_path) as pdf:
        for page_index, page in enumerate(pdf.pages):
            page_text = page.extract_text() or ""
            if pkg_re.search(page_text):
                mention_pages.append(page_index + 1)
            for table in page.find_tables():
                pins = _parse_pin_rows(table.extract())
                if pins is None:
                    continue
                top = table.bbox[1]
                above = page.crop((0, 0, page.width, max(top, 1))).extract_text() or ""
                lines = [ln for ln in above.splitlines() if ln.strip()]
                # only the line directly above the table counts as its heading
                if lines and _heading_names_only(lines[-1], pkg_re):
                    candidates.append((page_index + 1, pins))
                    if _may_continue(pdf, page_index):
                        continued = True

        base: dict[str, Any] = {"package": package}
        distinct = {tuple(tuple(p.items()) for p in pins) for _, pins in candidates}
        if len(distinct) == 1 and not continued:
            page_no, pins = candidates[0]
            return {**base, "machine_extracted": True, "page": page_no, "pins": pins, "page_images": []}

        if continued:
            reason = (
                f"pin numbering for {package} is NOT machine-extracted: the table may continue on the "
                "next page (a pin-like table follows it; no attempt is made to tell a continuation from another package)"
            )
        elif len(distinct) > 1:
            reason = (
                f"pin numbering for {package} is NOT machine-extracted: "
                f"{len(distinct)} different pin tables sit under headings naming it"
            )
        elif mention_pages:
            reason = (
                f"pin numbering for {package} is NOT machine-extracted: the package is named on "
                f"page(s) {mention_pages} but no Pin/Name table sits directly under a heading naming it "
                "(pin diagrams are usually images)"
            )
        else:
            reason = f"pin numbering for {package} is NOT machine-extracted: no page text names the package"
        shown = mention_pages[:_MAX_IMAGE_PAGES] or [1]
        images: list[dict[str, Any]] = []
        if image_dir is not None:
            out = Path(image_dir)
            out.mkdir(parents=True, exist_ok=True)
            for page_no in shown:
                page = pdf.pages[page_no - 1]
                if _large_images(page):
                    bbox = (
                        max(min(i["x0"] for i in page.images), 0),
                        max(min(i["top"] for i in page.images), 0),
                        min(max(i["x1"] for i in page.images), page.width),
                        min(max(i["bottom"] for i in page.images), page.height),
                    )
                    area, crop = page.crop(bbox), "images"
                else:
                    area, crop = page, "full_page"
                dest = out / f"{pdf_path.stem}_p{page_no}.png"
                dest.write_bytes(_png_bytes(area))
                images.append(
                    {
                        "page": page_no,
                        "path": str(dest),
                        "crop": crop,
                        "package_named_on_page": page_no in mention_pages,
                    }
                )
        else:
            reason += "; pass image_dir to get rendered page images"
        return {**base, "machine_extracted": False, "pins": [], "reason": reason, "page_images": images}
