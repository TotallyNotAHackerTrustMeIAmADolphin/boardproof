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
import unicodedata
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
# row it accepts becomes a pin), while this predicate only decides whether a later-page table
# looks like pin data, where a false positive costs a "may continue" and a false negative
# lets foreign pins hide behind a verified table. Cells are NFKC-normalised first, so full-width
# digits and letters read as ASCII.
_PIN_LIKE_ID_PART = r"[A-Z]{0,3}\d{1,5}[A-Z]?"
_PIN_LIKE_ID_RE = re.compile(
    rf"^(EP|{_PIN_LIKE_ID_PART}(?:[.,/\-]\s?{_PIN_LIKE_ID_PART})*\.?)$", re.IGNORECASE
)
_PIN_LIKE_HEADER_RE = re.compile(
    r"^(pins?(\s*(no\.?|number|#|name))?|pin\s*/\s*pad|no\.?|number|pad|terminals?)$", re.IGNORECASE
)
_LOOKAHEAD_PAGES = 3  # pages after the table that are searched for a pin-like table


def _is_id(cell: str) -> bool:
    return bool(_PIN_LIKE_ID_RE.match(cell) or _PIN_ID_RE.match(cell))


def _is_header(cell: str) -> bool:
    return bool(_PIN_LIKE_HEADER_RE.match(cell) or _PIN_COL_RE.match(cell))


def _table_is_pin_like(rows: list[list[Any]], rotated: bool = False) -> bool:
    """True when any row of a table has a pin-column header cell or starts with a pin-like
    identifier (the first non-blank cell, so a blank leading cell does not hide it), or when
    one column holds a pin-like identifier in at least two rows (the name-first, headerless
    shape). Every row is examined, so a header on row four is found.

    On a rotated page pdfplumber hands the grid back transposed, with rows and character order
    reversed (checked at 90, 180 and 270 degrees), so none of those shapes can be relied on:
    there any cell, or its reversal, that is a pin-like identifier or pin header counts."""
    grid = [[unicodedata.normalize("NFKC", _clean(c)) for c in row] for row in rows]
    if rotated:
        return any(_is_id(c) or _is_header(c) or _is_id(c[::-1]) or _is_header(c[::-1]) for row in grid for c in row if c)
    for cells in grid:
        if any(_is_header(c) for c in cells):
            return True
        first = next((c for c in cells if c), "")
        if first and _is_id(first):
            return True
    width = max((len(r) for r in grid), default=0)
    return any(sum(1 for r in grid if ci < len(r) and r[ci] and _is_id(r[ci])) >= 2 for ci in range(width))


def _may_continue(pdf: Any, page_index: int) -> bool:
    """True when ANY table on any of the next _LOOKAHEAD_PAGES pages is pin-like, whatever
    heading it sits under.

    Fail closed (issue #39, rounds 4 and 5; issue #46): no attempt is made to tell a
    continuation of the first table from the table of another package or part, because any
    such heading heuristic can fail open. Every pin-like table counts, not only the first one,
    every row of each is examined (see _table_is_pin_like), and a blank or figure page between
    two parts does not hide a following pin table. A table is pin-like when one of its rows has
    a pin-column header cell or starts with a pin-like identifier, or a column holds such
    identifiers in two rows. A rotated page is read with the looser rotated rule. The first
    table stands only when those pages have no table, only tables of empty cells, or only
    tables of other shapes.

    Known limits, not handled (issue #46): a pin table more than _LOOKAHEAD_PAGES pages after the
    first one is not seen; a pin table drawn as an image or as text without ruled cells is not
    seen (the same blindness as the main extraction); on a rotated page a table that
    pdfplumber does not find stays unseen. Landscape pages (wider than tall, no rotation flag)
    are read like portrait ones and are tested."""
    for nxt in range(page_index + 1, min(page_index + 1 + _LOOKAHEAD_PAGES, len(pdf.pages))):
        page = pdf.pages[nxt]
        rotated = bool(page.rotation % 360)
        for table in page.find_tables():
            rows = table.extract()
            if rows and _table_is_pin_like(rows, rotated):
                return True
    return False


def _write_png_without_replacing(out: Path, stem: str, data: bytes) -> Path:
    """Write the PNG as <stem>.png, or <stem>_2.png, <stem>_3.png ... when a file of another
    content already has the name: an existing file is never replaced (issue #46). A file with
    the same bytes is reused, so repeated calls leave no copies."""
    n = 1
    while True:
        dest = out / (f"{stem}.png" if n == 1 else f"{stem}_{n}.png")
        try:
            with dest.open("xb") as fh:
                fh.write(data)
            return dest
        except (FileExistsError, PermissionError, IsADirectoryError):
            if not dest.exists():
                raise  # a real permission problem, not a taken name
            if dest.is_file() and dest.read_bytes() == data:
                return dest
            n += 1


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
                dest = _write_png_without_replacing(out, f"{pdf_path.stem}_p{page_no}", _png_bytes(area))
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
