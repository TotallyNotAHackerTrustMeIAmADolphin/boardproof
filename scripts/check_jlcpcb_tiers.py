"""Read-only cross-check of the vendored JLCPCB part-tier index against a parts CSV (build ticket 42).

    python scripts/check_jlcpcb_tiers.py (--csv FILE | --fetch [--url URL]) [--index FILE] [--sample N]

The vendored index (``src/boardproof/sourcing/data/jlcpcb_basic_index.json``) is pinned to one commit of
the CDFER/JLCPCB-Kicad-Library, whose daily generator is broken (issue 41). Its Basic and Preferred
tiers therefore age. The CDFER/jlcpcb-parts-database file ``jlcpcb-components-basic-preferred.csv``
is refreshed daily and carries the current tiers. This script compares the two and reports:

* tier changes: a part in both whose tier differs (CSV tier: ``library_type == base`` is basic (the library_type values base/expand/extended are
assumed, not verified live; the default URL was not fetched live either),
  otherwise ``preferred == 1`` is preferred, otherwise extended);
* index parts absent from the CSV (the CSV lists only Basic and Preferred parts, so an absent Basic or
  Preferred part has dropped to Extended or left the catalogue; an absent Extended part is expected and
  only counted);
* CSV parts missing from the index (candidates to add, which needs a new library commit for the
  symbol and footprint names the CSV does not carry).

Output is counts plus lists capped at ``--sample`` entries (default 20), sorted by LCSC number. Stock
and price are read from neither source nor printed. Nothing is written: not the index, not the CSV
(a fetched CSV lives in memory only). Vendoring any CSV content stays blocked until the terms of the
data sources have been read (issue 42).

CSV rules: UTF-8 with or without a BOM, CRLF or LF, quoted fields with commas, any column order.
Required columns ``lcsc``, ``library_type`` and ``preferred``. Refused, never guessed: a missing or
duplicated required column, a row with a different field count, an empty or duplicate LCSC number, a
``library_type`` outside base/expand/extended, a ``preferred`` outside 0/1, NUL bytes, a CSV without
rows.

``--fetch`` takes only an https URL without credentials on an allow-listed host (also for every
redirect), reads at most 64 MB, times out after 60 s per socket operation (not a total deadline), and sends no cookies or headers beyond the
defaults. The default URL is the file named above on the ``main`` branch.

Exit codes: 0 a report was printed (differences are not an error); 2 an input is unreadable or refused.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INDEX = ROOT / "src" / "boardproof" / "sourcing" / "data" / "jlcpcb_basic_index.json"
DEFAULT_URL = "https://raw.githubusercontent.com/CDFER/jlcpcb-parts-database/main/jlcpcb-components-basic-preferred.csv"
ALLOWED_HOSTS = frozenset({"raw.githubusercontent.com", "github.com", "objects.githubusercontent.com"})
MAX_BYTES = 64 * 1024 * 1024
TIMEOUT_S = 60
DEFAULT_SAMPLE = 20
REQUIRED = ("lcsc", "library_type", "preferred")
_LIBRARY_TYPES = {"base", "expand", "extended"}
_LCSC = re.compile(r"^C?(\d{1,12})$", re.IGNORECASE)
_TIERS = ("basic", "preferred", "extended")


class InputError(Exception):
    """An input is unreadable or refused; main() turns it into exit code 2."""


def normalize_lcsc(raw: str) -> str | None:
    match = _LCSC.match(raw.strip())
    return f"C{int(match.group(1))}" if match else None


def parse_csv(data: bytes) -> dict[str, str]:
    """LCSC number -> tier for every row of the CSV bytes; raises InputError on anything malformed."""
    if b"\x00" in data:
        raise InputError("the CSV holds NUL bytes")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InputError(f"the CSV is not UTF-8: {exc}") from None
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        header = next(reader, None)
        if header is None:
            raise InputError("the CSV is empty")
        names = [h.strip().lower() for h in header]
        index: dict[str, int] = {}
        for name in REQUIRED:
            count = names.count(name)
            if count != 1:
                raise InputError(f"the CSV needs exactly one column {name!r}, found {count}")
            index[name] = names.index(name)
        tiers: dict[str, str] = {}
        for number, row in enumerate(reader, start=2):
            if not row:
                continue  # a blank line (a trailing newline) carries nothing
            if len(row) != len(names):
                raise InputError(f"CSV line {number} has {len(row)} fields, the header has {len(names)}")
            lcsc = normalize_lcsc(row[index["lcsc"]])
            if lcsc is None:
                raise InputError(f"CSV line {number}: {row[index['lcsc']][:40]!r} is not an LCSC number")
            if lcsc in tiers:
                raise InputError(f"CSV line {number}: LCSC number {lcsc} appears twice")
            library_type = row[index["library_type"]].strip().lower()
            if library_type not in _LIBRARY_TYPES:
                raise InputError(f"CSV line {number}: library_type {library_type[:20]!r} is not base, expand or extended")
            preferred = row[index["preferred"]].strip()
            if preferred not in ("0", "1"):
                raise InputError(f"CSV line {number}: preferred {preferred[:20]!r} is not 0 or 1")
            tiers[lcsc] = "basic" if library_type == "base" else "preferred" if preferred == "1" else "extended"
    except csv.Error as exc:
        raise InputError(f"the CSV is malformed: {exc}") from None
    if not tiers:
        raise InputError("the CSV has a header but no rows")
    return tiers


def load_index(path: Path) -> dict[str, str]:
    """LCSC number -> tier of the committed index."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        parts = payload["parts"]
        tiers: dict[str, str] = {}
        for part in parts:
            lcsc = normalize_lcsc(part["lcsc"])
            tier = part["tier"]
            if lcsc is None or tier not in _TIERS:
                raise ValueError(f"bad entry {str(part)[:80]}")
            if lcsc in tiers:
                raise ValueError(f"LCSC number {lcsc} appears twice")
            tiers[lcsc] = tier
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise InputError(f"the index {path} is unreadable: {type(exc).__name__}: {exc}") from None
    if not tiers:
        raise InputError(f"the index {path} holds no parts")
    return tiers


def compare(index: dict[str, str], csv_tiers: dict[str, str]) -> dict:
    def order(key: str) -> int:
        return int(key[1:])

    changes = sorted(((k, index[k], csv_tiers[k]) for k in index.keys() & csv_tiers.keys() if index[k] != csv_tiers[k]),
                     key=lambda c: order(c[0]))
    absent = sorted((k for k in index.keys() - csv_tiers.keys()), key=order)
    missing = sorted((k for k in csv_tiers.keys() - index.keys()), key=order)
    return {
        "index_total": len(index),
        "csv_total": len(csv_tiers),
        "tier_changes": changes,
        "absent_from_csv": [(k, index[k]) for k in absent],
        "missing_from_index": [(k, csv_tiers[k]) for k in missing],
    }


def format_report(result: dict, sample: int) -> str:
    lines = [f"index parts: {result['index_total']}, CSV parts: {result['csv_total']}"]

    def listing(items, fmt):
        for item in items[:sample]:
            lines.append("    " + fmt(item))
        if len(items) > sample:
            lines.append(f"    ... and {len(items) - sample} more")

    changes = result["tier_changes"]
    lines.append(f"tier changes: {len(changes)}")
    for (a, b), n in sorted(Counter((a, b) for _, a, b in changes).items()):
        lines.append(f"  {a} -> {b}: {n}")
    listing(changes, lambda c: f"{c[0]}: {c[1]} -> {c[2]}")

    absent = result["absent_from_csv"]
    stale = [a for a in absent if a[1] != "extended"]
    lines.append(f"index parts absent from the CSV: {len(absent)} (basic or preferred in the index: {len(stale)}; "
                 f"extended, as expected: {len(absent) - len(stale)})")
    listing(stale, lambda a: f"{a[0]} ({a[1]} in the index)")

    missing = result["missing_from_index"]
    lines.append(f"CSV parts missing from the index: {len(missing)}")
    for tier, n in sorted(Counter(t for _, t in missing).items()):
        lines.append(f"  {tier}: {n}")
    listing(missing, lambda m: f"{m[0]} ({m[1]} in the CSV)")
    lines.append("Nothing was written. Stock and price were not read.")
    return "\n".join(lines)


def check_url(url: str) -> str | None:
    """None when ``url`` may be fetched: https, an allow-listed host, no credentials, no port, no fragment."""
    if not url or any(c.isspace() or ord(c) < 32 for c in url):
        return "the URL holds whitespace or control characters"
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return "the URL does not parse"
    if parts.scheme != "https":
        return "only https URLs are fetched"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "a URL with credentials is refused"
    if port is not None:
        return "a URL with a port is refused"
    if (parts.hostname or "") not in ALLOWED_HOSTS:
        return f"host {parts.hostname!r} is not allow-listed ({', '.join(sorted(ALLOWED_HOSTS))})"
    return None


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = check_url(newurl)
        if problem:
            raise urllib.error.URLError(f"redirect refused: {problem}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_csv(url: str, *, opener=None, max_bytes: int = MAX_BYTES, timeout: int = TIMEOUT_S) -> bytes:
    """The bytes behind ``url``, held in memory. ``opener`` exists so tests need no network."""
    problem = check_url(url)
    if problem:
        raise InputError(f"cannot fetch: {problem}")
    if opener is None:
        opener = urllib.request.build_opener(_CheckedRedirect)
    try:
        with opener.open(urllib.request.Request(url), timeout=timeout) as response:
            data = response.read(max_bytes + 1)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise InputError(f"fetching the CSV failed: {type(exc).__name__}: {exc}") from None
    if len(data) > max_bytes:
        raise InputError(f"the CSV is larger than {max_bytes} bytes; refused")
    return data


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", type=Path, help="a local CSV file")
    source.add_argument("--fetch", action="store_true", help="fetch the upstream CSV over https (kept in memory)")
    parser.add_argument("--url", default=None, help="with --fetch: another allow-listed https URL")
    parser.add_argument("--index", type=Path, default=DEFAULT_INDEX, help="the tier index to check")
    parser.add_argument("--sample", type=int, default=DEFAULT_SAMPLE, help="entries per list (default 20)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.csv is not None and args.url is not None:
        print("check_jlcpcb_tiers: --url only goes with --fetch", file=sys.stderr)
        return 2
    if args.sample < 0:
        print("check_jlcpcb_tiers: --sample must not be negative", file=sys.stderr)
        return 2
    try:
        if args.csv is not None:
            try:
                data = args.csv.read_bytes()
            except OSError as exc:
                raise InputError(f"cannot read {args.csv}: {exc}") from None
            if len(data) > MAX_BYTES:
                raise InputError(f"the CSV is larger than {MAX_BYTES} bytes; refused")
        else:
            data = fetch_csv(args.url or DEFAULT_URL)
        csv_tiers = parse_csv(data)
        index = load_index(args.index)
    except InputError as exc:
        print(f"check_jlcpcb_tiers: {exc}", file=sys.stderr)
        return 2
    print(format_report(compare(index, csv_tiers), args.sample))
    return 0


if __name__ == "__main__":
    sys.exit(main())
