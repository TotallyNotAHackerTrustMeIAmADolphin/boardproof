"""Local parts index: a read-only reader for a jlcparts-format SQLite cache (issue 199, decision ticket 53).

The index is the ``cache.sqlite3`` of the community project jlcparts (yaqwsx/jlcparts), downloaded locally
by the user; nothing is vendored and this module never touches the network. It is opened read-only and
immutable, never written, and every SQL value is a bound parameter (the only text put into SQL is a fixed
column list chosen by this module, never a caller's string).

Schema read (primary-source check of the jlcparts code, docs/ARCHITECTURE.md "Known facts and gotchas",
"jlcparts cache format"): tables ``components`` (lcsc INTEGER without the C, category_id, mfr, package,
manufacturer_id, basic, preferred, description, datasheet, stock, price JSON text, last_update, extra JSON
text, jlc_extra JSON text), ``categories`` (id, category, subcategory) and ``manufacturers`` (id, name).
Required columns are checked on open; ``preferred``, ``extra`` and ``jlc_extra`` are optional (older caches
lack some), a part without them is read as not preferred / no attributes.

Not verified against real data (the 250 MB file is never downloaded in this repository): the exact text of
attribute values (``"10k\\u03a9"``-style with a unit suffix is assumed from the LCSC/JLCPCB parameter
names), the currency of ``price`` and the unit of ``last_update`` (taken as unix seconds). Every answer
says so where it matters.
"""

from __future__ import annotations

import json
import math
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path, PureWindowsPath
from typing import Any

from pydantic import ValidationError

from boardproof.sourcing.models import PartResult, PriceBreak

ENV_VAR = "BOARDPROOF_PARTS_INDEX"
STALE_AFTER_DAYS = 7
MAX_CANDIDATES = 200_000
DEFAULT_LIMIT = 20
MAX_LIMIT = 40
CHARACTER_LIMIT = 25_000
PROVIDER_NAME = "jlcparts_index"

TIERS = ("basic", "preferred", "extended")
SORT_FIELDS = ("stock", "price", "lcsc")
SORT_ORDERS = ("asc", "desc")
ATTRIBUTE_OPS = ("eq", "ne", "lt", "le", "gt", "ge", "contains")
FILTER_NAMES = (
    "category", "subcategory", "attributes", "package", "manufacturer", "min_stock", "max_price",
    "price_quantity", "tiers", "text", "sort_by", "sort_order", "limit", "offset", "index_path",
)

HOW_TO_GET = (
    "Download the jlcparts cache yourself (the jlcparts project publishes it as cache.zip with split "
    "parts cache.z01, cache.z02, ...; extract cache.sqlite3 with 7-Zip) and point BOARDPROOF_PARTS_INDEX "
    "at the extracted file, or pass index_path. Nothing is shipped with this server; a download tool is "
    "a separate, later addition."
)

_REQUIRED = {
    "components": ("lcsc", "category_id", "mfr", "package", "manufacturer_id", "basic", "description",
                   "datasheet", "stock", "price", "last_update"),
    "categories": ("id", "category", "subcategory"),
    "manufacturers": ("id", "name"),
}


class IndexRefusal(Exception):
    """A refusal with a stable code; becomes ``{"ok": False, "status": "refused", "reason": code, ...}``."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def as_result(self) -> dict:
        return {"ok": False, "status": "refused", "reason": self.code, "message": self.message, **self.extra}


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- quantities with SI prefixes and units

_PREFIX = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_OHM = chr(0x3A9)  # Greek capital omega
_UNITS = frozenset({_OHM, "F", "V", "A", "H", "W", "Hz", "s", "%"})
_NUMBER = re.compile(r"^\s*([+\-\u00b1]?)\s*((?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+\-]?\d+)?)\s*(.*?)\s*$")


def _normalise_suffix(rest: str) -> str:
    rest = re.sub(r"\s+", "", rest)
    rest = rest.replace(chr(0xB5), "u").replace(chr(0x3BC), "u").replace(chr(0x2126), _OHM)
    return re.sub(r"(?i)ohms?", _OHM, rest)


def parse_quantity(text: Any) -> tuple[float, str] | None:
    """``"10k<ohm>"`` -> ``(10000.0, "<ohm>")``, ``"100nF"`` -> ``(1e-07, "F")``, ``"+-1%"`` -> ``(1.0, "%")``.

    A bare number has unit ``""``. Anything else (``"X7R"``, ``"-55~125"``) is ``None``: not a quantity.
    A leading plus-minus sign is dropped (a tolerance is read as its size). A lone ``m`` or ``M`` is a prefix,
    ``m`` milli and ``M`` mega (SI is case sensitive); a lone unit letter (``F``, ``V``, ``A``, ``H``,
    ``W``) is a unit, never a prefix.
    """
    if isinstance(text, bool):
        return None
    if isinstance(text, (int, float)):
        number = _finite(text)
        return None if number is None else (number, "")
    if not isinstance(text, str):
        return None
    match = _NUMBER.match(text)
    if not match:
        return None
    sign, number, rest = match.groups()
    rest = _normalise_suffix(rest)
    if rest == "":
        multiplier, unit = 1.0, ""
    elif rest in _UNITS:
        multiplier, unit = 1.0, rest
    elif rest[0] in _PREFIX and (len(rest) == 1 or rest[1:] in _UNITS):
        multiplier, unit = _PREFIX[rest[0]], rest[1:]
    else:
        return None
    try:
        base = Decimal(number)
        if base != 0 and abs(base.adjusted()) > 300:
            return None
        exact = base * Decimal(repr(multiplier))
        value = float(exact)
    except (InvalidOperation, ValueError, OverflowError, ArithmeticError):
        return None
    if sign == "-":
        value = -value
    return (value, unit) if math.isfinite(value) else None


def _finite(value: Any) -> float | None:
    """``value`` as a finite float, or ``None`` (an int too large for a float, infinity and NaN included)."""
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _units_compatible(filter_unit: str, value_unit: str) -> bool:
    return filter_unit == "" or filter_unit == value_unit


def match_attribute(value: str, op: str, wanted: Any) -> bool:
    """Does one attribute value (text from the index) satisfy ``op wanted``? Numeric where both sides read as
    quantities with compatible units (a wanted value without unit accepts any unit), else text."""
    have_q = parse_quantity(value)
    want_q = parse_quantity(wanted)
    if op in ("lt", "le", "gt", "ge"):
        if have_q is None or want_q is None or not _units_compatible(want_q[1], have_q[1]):
            return False
        a, b = have_q[0], want_q[0]
        close = math.isclose(a, b, rel_tol=1e-9)
        if op == "lt":
            return a < b and not close
        if op == "gt":
            return a > b and not close
        if op == "le":
            return a < b or close
        return a > b or close
    if op == "contains":
        return str(wanted).strip().casefold() in value.casefold()
    if have_q is not None and want_q is not None:
        equal = _units_compatible(want_q[1], have_q[1]) and math.isclose(have_q[0], want_q[0], rel_tol=1e-9, abs_tol=0.0)
    else:
        equal = value.strip().casefold() == str(wanted).strip().casefold()
    return equal if op == "eq" else not equal


# ---------------------------------------------------------------- opening the index

def _network_by_text(given: str) -> bool:
    """UNC and NT device forms by their text, whatever mix of ``/`` and backslash the caller used.

    Windows treats forward and back slashes alike, so a path that starts with one of each is UNC too.
    """
    unified = given.replace("/", "\\")
    return unified.startswith("\\\\") or unified.startswith("\\??\\")


def _network_by_drive(given: str) -> bool:
    """The same by what pathlib calls the drive (a second, independent reading); no file-system call."""
    return PureWindowsPath(given).drive.startswith("\\\\") or Path(given).drive.startswith("\\\\")


def _is_network_path(given: str) -> bool:
    return _network_by_text(given) or _network_by_drive(given)


def resolve_index_path(index_path: str | None) -> Path:
    given = index_path if index_path not in (None, "") else os.environ.get(ENV_VAR, "")
    if not given:
        raise IndexRefusal(
            "index_not_configured",
            f"No parts index is configured: pass index_path or set {ENV_VAR}. {HOW_TO_GET}",
            how_to_get=HOW_TO_GET, env_var=ENV_VAR,
        )
    if _is_network_path(given):
        raise IndexRefusal(
            "index_path_refused",
            "A network (UNC) path is refused: the index must be a local file, and the server does not touch "
            "a network share on a caller's say-so. Copy the file to a local disk.",
            env_var=ENV_VAR,
        )
    path = Path(given)
    if not path.is_file():
        raise IndexRefusal(
            "index_not_found",
            f"The parts index file {given!r} does not exist (or is not a file). {HOW_TO_GET}",
            how_to_get=HOW_TO_GET, env_var=ENV_VAR,
        )
    return path


def open_index(index_path: str | None) -> tuple[sqlite3.Connection, dict]:
    """Open the index read-only and immutable; returns ``(connection, columns)``. Never writes."""
    path = resolve_index_path(index_path)
    uri = path.resolve().as_uri() + "?mode=ro&immutable=1"
    try:
        conn = sqlite3.connect(uri, uri=True)
        conn.execute("PRAGMA query_only = ON")
        conn.row_factory = sqlite3.Row
        columns = {}
        for table in ("components", "categories", "manufacturers"):
            columns[table] = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error as exc:
        raise IndexRefusal("index_unreadable", f"{path.name} is not a readable SQLite database: {exc}") from exc
    missing = {t: [c for c in need if c not in columns[t]] for t, need in _REQUIRED.items()}
    missing = {t: cols for t, cols in missing.items() if cols}
    if missing:
        conn.close()
        raise IndexRefusal(
            "index_schema_unsupported",
            f"{path.name} does not have the jlcparts cache tables and columns this reader needs "
            "(the compact cache-v2 format is not supported); missing: "
            + "; ".join(f"{t}: {', '.join(c)}" for t, c in missing.items()),
            missing=missing,
        )
    return conn, columns


def index_info(conn: sqlite3.Connection, path_name: str) -> dict:
    row = conn.execute("SELECT COUNT(*) AS n, MAX(last_update) AS newest FROM components").fetchone()
    info: dict[str, Any] = {"file": path_name, "part_count": row["n"], "stale_after_days": STALE_AFTER_DAYS}
    newest = row["newest"]
    stamp = None
    try:
        if newest and int(newest) > 0:
            stamp = datetime.fromtimestamp(int(newest), tz=timezone.utc)
    except (ValueError, TypeError, OverflowError, OSError):
        stamp = None
    if stamp is None:
        info.update(index_date=None, index_age_days=None, index_stale=True,
                    date_note="The index records no update time for any part, so its age is unknown; treat it as old.")
        return info
    age_days = (_now() - stamp).total_seconds() / 86400
    info.update(
        index_date=stamp.date().isoformat(),
        index_age_days=round(age_days, 1),
        index_stale=age_days > STALE_AFTER_DAYS,
        date_note="index_date is the newest per-part update time (last_update, read as unix seconds) in the file; "
                  "an individual part can be older.",
    )
    if info["index_stale"]:
        info["stale_note"] = (
            f"The index is {round(age_days)} days old (more than {STALE_AFTER_DAYS}); stock and price may have "
            "changed. Download a newer cache before relying on them."
        )
    return info


# ---------------------------------------------------------------- reading parts

def _json(text: Any) -> Any:
    if not isinstance(text, str) or not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _attribute_dict(blob: Any) -> dict[str, str]:
    parsed = _json(blob)
    attrs = parsed.get("attributes") if isinstance(parsed, dict) else None
    out: dict[str, str] = {}
    if isinstance(attrs, dict):
        for name, value in attrs.items():
            if value is None or isinstance(value, (dict, list)):
                continue
            text = str(value).strip()
            if text not in ("", "-"):
                out[str(name)] = text
    return out


def merged_attributes(extra: Any, jlc_extra: Any) -> dict[str, str]:
    """Attribute name -> text from ``extra.attributes``, overlaid by ``jlc_extra.attributes`` (the jlcparts
    rule: the JLCPCB value wins; an empty or ``-`` value never overwrites). A part with neither: ``{}``."""
    attrs = _attribute_dict(extra)
    attrs.update(_attribute_dict(jlc_extra))
    return attrs


def _tiers_of(price: Any) -> list[tuple[int, float]]:
    parsed = _json(price)
    tiers: list[tuple[int, float]] = []
    if isinstance(parsed, list):
        for entry in parsed:
            if isinstance(entry, dict):
                q, p = entry.get("qFrom"), entry.get("price")
                if isinstance(q, (int, float)) and not isinstance(q, bool) and isinstance(p, (int, float)) \
                        and not isinstance(p, bool):
                    q_ok, p_ok = _finite(q), _finite(p)
                    if q_ok is not None and p_ok is not None and q_ok > 0 and p_ok >= 0:
                        tiers.append((int(q_ok), p_ok))
    return sorted(tiers)


def unit_price(price: Any, quantity: int) -> float | None:
    """Unit price at ``quantity`` (the tier with the largest qFrom not above it; below the first tier, the
    first tier). ``None`` when the part has no readable price."""
    tiers = _tiers_of(price)
    if not tiers:
        return None
    chosen = tiers[0]
    for tier in tiers:
        if tier[0] <= quantity:
            chosen = tier
    return chosen[1]


def tier_name(basic: Any, preferred: Any) -> str:
    """jlcparts' own rule: preferred wins over basic; neither is Extended."""
    if preferred:
        return "preferred"
    return "basic" if basic else "extended"


def _like(token: str) -> str:
    return "%" + token.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _select_columns(columns: dict) -> str:
    comp = columns["components"]
    return ", ".join([
        "c.lcsc AS lcsc", "c.mfr AS mfr", "c.package AS package", "c.basic AS basic",
        "c.preferred AS preferred" if "preferred" in comp else "0 AS preferred",
        "c.description AS description", "c.datasheet AS datasheet", "c.stock AS stock", "c.price AS price",
        "c.last_update AS last_update",
        "c.extra AS extra" if "extra" in comp else "NULL AS extra",
        "c.jlc_extra AS jlc_extra" if "jlc_extra" in comp else "NULL AS jlc_extra",
        "k.category AS category", "k.subcategory AS subcategory", "m.name AS manufacturer",
    ])


# The scan is read in LCSC order; the Python sort below does not rely on it (a test reverses it).
_SCAN_ORDER = " ORDER BY c.lcsc"
_FROM = (" FROM components c JOIN categories k ON k.id = c.category_id "
         "LEFT JOIN manufacturers m ON m.id = c.manufacturer_id")


def _resolve_categories(conn: sqlite3.Connection, category: str | None, subcategory: str | None) -> list[int] | None:
    if category in (None, ""):
        if subcategory not in (None, ""):
            raise IndexRefusal("category_required", "subcategory needs category as well (subcategory names repeat across categories).")
        return None
    rows = [r for r in conn.execute("SELECT id, category, subcategory FROM categories")
            if isinstance(r["category"], str) and isinstance(r["subcategory"], str)]
    in_category = [r for r in rows if r["category"].casefold() == category.strip().casefold()]
    if not in_category:
        raise IndexRefusal(
            "unknown_category", f"category {category!r} is not in the index.",
            valid_categories=sorted({r["category"] for r in rows}),
        )
    if subcategory in (None, ""):
        return [r["id"] for r in in_category]
    chosen = [r for r in in_category if r["subcategory"].casefold() == subcategory.strip().casefold()]
    if not chosen:
        raise IndexRefusal(
            "unknown_subcategory", f"subcategory {subcategory!r} is not in category {in_category[0]['category']!r}.",
            valid_subcategories=sorted(r["subcategory"] for r in in_category),
        )
    return [r["id"] for r in chosen]


def _in_clause(ids: list[int]) -> tuple[str, list[int]]:
    return "(" + ",".join("?" * len(ids)) + ")", list(ids)


def _scan_attributes(conn: sqlite3.Connection, columns: dict, category_ids: list[int]) -> list[dict[str, str]]:
    marks, params = _in_clause(category_ids)
    count = conn.execute(f"SELECT COUNT(*) FROM components c WHERE c.category_id IN {marks}", params).fetchone()[0]
    if count > MAX_CANDIDATES:
        raise IndexRefusal(
            "too_many_candidates",
            f"{count} parts are in that scope (limit {MAX_CANDIDATES}); name a subcategory.", candidates=count,
        )
    comp = columns["components"]
    extra = "c.extra" if "extra" in comp else "NULL"
    jlc = "c.jlc_extra" if "jlc_extra" in comp else "NULL"
    cursor = conn.execute(f"SELECT {extra}, {jlc} FROM components c WHERE c.category_id IN {marks}", params)
    return [merged_attributes(row[0], row[1]) for row in cursor]


def _unit_of(values: list[str]) -> tuple[str | None, list[float], str | None]:
    """(unit or None, the numbers read, why the unit is None)."""
    parsed = [q for q in (parse_quantity(v) for v in values) if q is not None]
    units = {u for _, u in parsed}
    numbers = [n for n, _ in parsed]
    if not parsed:
        return None, numbers, "no value reads as a quantity (a number with an optional SI prefix and unit)"
    if len(units) > 1:
        return None, numbers, f"the numeric values carry different units: {sorted(u or '(none)' for u in units)}"
    unit = next(iter(units))
    if not unit:
        return None, numbers, "the numeric values have no unit"
    return unit, numbers, None


def list_filters(index_path: str | None, category: str | None = None, subcategory: str | None = None,
                 attributes: list[str] | None = None, max_values: int = 10) -> dict:
    if not isinstance(max_values, int) or isinstance(max_values, bool) or not 1 <= max_values <= 200:
        raise IndexRefusal("invalid_argument", "max_values must be an integer from 1 to 200.")
    if attributes is not None and (not isinstance(attributes, list) or not all(isinstance(a, str) for a in attributes)):
        raise IndexRefusal("invalid_argument", "attributes must be a list of attribute names.")
    conn, columns = open_index(index_path)
    try:
        path = resolve_index_path(index_path)
        result: dict[str, Any] = {"ok": True, "status": "ok", "index": index_info(conn, path.name),
                                  "filter_names": list(FILTER_NAMES), "tiers": list(TIERS),
                                  "attribute_ops": list(ATTRIBUTE_OPS), "sort_fields": list(SORT_FIELDS)}
        ids = _resolve_categories(conn, category, subcategory)
        if ids is None:
            rows = conn.execute(
                "SELECT k.category AS category, COUNT(DISTINCT k.id) AS subcategories, COUNT(c.lcsc) AS parts "
                "FROM categories k LEFT JOIN components c ON c.category_id = k.id "
                "WHERE typeof(k.category) = 'text' AND typeof(k.subcategory) = 'text' "
                "GROUP BY k.category ORDER BY k.category"
            ).fetchall()
            result["categories"] = [dict(r) for r in rows]
            skipped = conn.execute(
                "SELECT COUNT(DISTINCT k.id) AS categories, COUNT(c.lcsc) AS parts FROM categories k "
                "LEFT JOIN components c ON c.category_id = k.id "
                "WHERE typeof(k.category) != 'text' OR typeof(k.subcategory) != 'text'"
            ).fetchone()
            if skipped["categories"]:
                result["skipped_categories"] = {
                    "categories": skipped["categories"], "parts": skipped["parts"],
                    "note": "Category rows whose name is not text cannot be listed or selected; their parts are in no list here.",
                }
            result["next_step"] = "Call again with category (and subcategory) to list that scope's attribute names and values."
            return result
        marks, params = _in_clause(ids)
        subs = conn.execute(
            f"SELECT k.category AS category, k.subcategory AS name, COUNT(c.lcsc) AS parts FROM categories k "
            f"LEFT JOIN components c ON c.category_id = k.id WHERE k.id IN {marks} GROUP BY k.id ORDER BY k.subcategory", params
        ).fetchall()
        result["category"] = subs[0]["category"] if subs else category
        result["subcategories"] = [{"name": r["name"], "parts": r["parts"]} for r in subs]
        scanned = _scan_attributes(conn, columns, ids)
        counters: dict[str, Counter] = {}
        for attrs in scanned:
            for name, value in attrs.items():
                counters.setdefault(name, Counter())[value] += 1
        result["parts_in_scope"] = len(scanned)
        result["parts_without_attributes"] = sum(1 for a in scanned if not a)
        if attributes:
            unknown = [a for a in attributes if a not in counters]
            if unknown:
                raise IndexRefusal("unknown_attribute", f"attribute name(s) not in this scope: {unknown}.",
                                   unknown=unknown, valid_attributes=sorted(counters))
            names = list(attributes)
        else:
            names = sorted(counters, key=lambda n: (-sum(counters[n].values()), n))[:60]
            if len(counters) > 60:
                result["attribute_names_truncated"] = {"shown": 60, "total": len(counters),
                                                       "note": "Pass attributes=[names] for the others."}
        entries = []
        for name in names:
            counter = counters[name]
            unit, numbers, unit_note = _unit_of(list(counter))
            non_numeric = [v for v in counter if parse_quantity(v) is None]
            entry: dict[str, Any] = {
                "name": name, "parts_with_value": sum(counter.values()), "distinct_values": len(counter),
                "unit": unit, "numeric_values": len(counter) - len(non_numeric),
                "non_numeric_values": len(non_numeric), "non_numeric_parts": sum(counter[v] for v in non_numeric),
            }
            if unit_note:
                entry["unit_note"] = unit_note
            if numbers:
                entry["numeric_range"] = [min(numbers), max(numbers)]
                if non_numeric:
                    entry["range_note"] = "numeric_range is taken from the numeric values only; the non-numeric ones are not in it."
            entry["_all"] = counter.most_common()
            entries.append(entry)
        shown = max_values
        while True:
            out = []
            for entry in entries:
                item = {k: v for k, v in entry.items() if k != "_all"}
                item["values"] = [{"value": v, "count": c} for v, c in entry["_all"][:shown]]
                item["values_truncated"] = entry["distinct_values"] > shown
                out.append(item)
            result["attributes"] = out
            if len(json.dumps(result, ensure_ascii=False)) <= CHARACTER_LIMIT or shown <= 1:
                break
            shown = max(1, shown // 2)
        if shown < max_values:
            result["values_cut_to"] = shown
            result["note"] = "Values were cut to stay under the response budget; pass attributes=[names] for full lists."
        return result
    finally:
        conn.close()


def _not_a_usable_number(value: Any) -> bool:
    """True for a number (or a text that starts with one) that does not fit a float: 10**400, 1e9999999, a float inf or NaN.

    A text such as "inf" or "NaN" does not start with a number and stays a text value."""
    if isinstance(value, str):
        match = _NUMBER.match(value)
        if not match:
            return False  # text such as "inf" or "NaN" is only text
        try:
            return not math.isfinite(float(match.group(2)))
        except ValueError:
            return False
    return _finite(value) is None


def _validate_attribute_filters(raw: Any) -> list[dict]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise IndexRefusal("invalid_filter", "attributes must be a list of {name, op, value} objects.")
    filters = []
    for entry in raw:
        if not isinstance(entry, dict) or set(entry) - {"name", "op", "value"} or "name" not in entry or "value" not in entry:
            raise IndexRefusal("invalid_filter", f"each attribute filter is {{name, op (optional), value}}; got {entry!r}.")
        op = entry.get("op", "eq")
        if op not in ATTRIBUTE_OPS:
            raise IndexRefusal("invalid_filter", f"op {op!r} is not valid.", valid_ops=list(ATTRIBUTE_OPS))
        value = entry["value"]
        if isinstance(value, bool) or not isinstance(value, (str, int, float)) or (isinstance(value, str) and not value.strip()):
            raise IndexRefusal("invalid_filter", f"value of attribute filter {entry['name']!r} must be a number or non-empty text.")
        if _not_a_usable_number(value):
            raise IndexRefusal("invalid_filter", f"value of attribute filter {entry['name']!r} is a number too large to compare "
                                                 "(or infinity or NaN).")
        if op in ("lt", "le", "gt", "ge") and parse_quantity(value) is None:
            raise IndexRefusal("invalid_filter", f"op {op!r} needs a number with an optional SI prefix and unit (10k, 100nF, 50V); got {value!r}.")
        filters.append({"name": str(entry["name"]), "op": op, "value": value})
    return filters


def _int_arg(name: str, value: Any, low: int, high: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < low or (high is not None and value > high):
        bound = f" from {low} to {high}" if high is not None else f" of at least {low}"
        raise IndexRefusal("invalid_argument", f"{name} must be an integer{bound}.")
    return value


def filter_parts(
    index_path: str | None, *, category: str | None = None, subcategory: str | None = None,
    attributes: list[dict] | None = None, package: str | None = None, manufacturer: str | None = None,
    min_stock: int | None = None, max_price: float | None = None, price_quantity: int = 1,
    tiers: list[str] | None = None, text: str | None = None, sort_by: str = "stock",
    sort_order: str | None = None, limit: int = DEFAULT_LIMIT, offset: int = 0,
) -> dict:
    limit = _int_arg("limit", limit, 1, MAX_LIMIT)
    offset = _int_arg("offset", offset, 0)
    price_quantity = _int_arg("price_quantity", price_quantity, 1)
    if min_stock is not None:
        min_stock = _int_arg("min_stock", min_stock, 0)
    if max_price is not None and (isinstance(max_price, bool) or not isinstance(max_price, (int, float)) or max_price < 0
                                  or not math.isfinite(max_price)):
        raise IndexRefusal("invalid_argument", "max_price must be a number of at least 0.")
    if sort_by not in SORT_FIELDS:
        raise IndexRefusal("invalid_argument", f"sort_by {sort_by!r} is not valid.", valid_sort_fields=list(SORT_FIELDS))
    if sort_order is None:
        sort_order = "asc" if sort_by in ("price", "lcsc") else "desc"
    if sort_order not in SORT_ORDERS:
        raise IndexRefusal("invalid_argument", f"sort_order {sort_order!r} is not valid.", valid_sort_orders=list(SORT_ORDERS))
    if tiers is not None:
        bad = [t for t in tiers if t not in TIERS] if isinstance(tiers, list) else [tiers]
        if bad or not tiers:
            raise IndexRefusal("invalid_argument", f"tiers must be a non-empty list drawn from {list(TIERS)}.", valid_tiers=list(TIERS))
    attr_filters = _validate_attribute_filters(attributes)
    tokens = text.split() if isinstance(text, str) else []
    if text is not None and (not isinstance(text, str) or not tokens):
        raise IndexRefusal("invalid_argument", "text must be non-empty text.")
    if len(tokens) > 8:
        raise IndexRefusal("invalid_argument", "text takes at most 8 words.")
    for label, value in (("package", package), ("manufacturer", manufacturer)):
        if value is not None and (not isinstance(value, str) or not value.strip()):
            raise IndexRefusal("invalid_argument", f"{label} must be non-empty text.")

    conn, columns = open_index(index_path)
    try:
        path = resolve_index_path(index_path)
        info = index_info(conn, path.name)
        ids = _resolve_categories(conn, category, subcategory)
        if attr_filters:
            if ids is None:
                raise IndexRefusal("category_required",
                                   "attribute filters need category (attribute names differ per category; call list_part_filters first).")
            seen = set()
            for attrs in _scan_attributes(conn, columns, ids):
                seen.update(attrs)
            unknown = sorted({f["name"] for f in attr_filters} - seen)
            if unknown:
                raise IndexRefusal("unknown_attribute", f"attribute name(s) not in this scope: {unknown}.",
                                   unknown=unknown, valid_attributes=sorted(seen))
        where, params = [], []
        if ids is not None:
            marks, p = _in_clause(ids)
            where.append(f"c.category_id IN {marks}")
            params += p
        if package is not None:
            where.append("lower(c.package) = lower(?)")
            params.append(package.strip())
        if manufacturer is not None:
            where.append("lower(m.name) = lower(?)")
            params.append(manufacturer.strip())
        if min_stock is not None:
            where.append("c.stock >= ?")
            params.append(min_stock)
        if tiers is not None:
            pref = "c.preferred" if "preferred" in columns["components"] else "0"
            clauses = {"preferred": f"{pref} = 1", "basic": f"({pref} = 0 AND c.basic = 1)",
                       "extended": f"({pref} = 0 AND c.basic = 0)"}
            where.append("(" + " OR ".join(clauses[t] for t in dict.fromkeys(tiers)) + ")")
        for token in tokens:
            where.append("(c.mfr LIKE ? ESCAPE '\\' OR c.description LIKE ? ESCAPE '\\' OR m.name LIKE ? ESCAPE '\\' "
                         "OR ('C' || c.lcsc) LIKE ? ESCAPE '\\')")
            params += [_like(token)] * 4
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        count = conn.execute("SELECT COUNT(*)" + _FROM + where_sql, params).fetchone()[0]
        if count > MAX_CANDIDATES:
            raise IndexRefusal("too_many_candidates",
                               f"{count} parts pass the index-side filters (limit {MAX_CANDIDATES}); narrow category, "
                               "subcategory, package, manufacturer or text.", candidates=count)
        need_attrs = bool(attr_filters)
        extra = "c.extra" if need_attrs and "extra" in columns["components"] else "NULL"
        jlc = "c.jlc_extra" if need_attrs and "jlc_extra" in columns["components"] else "NULL"
        cursor = conn.execute(f"SELECT c.lcsc AS lcsc, c.stock AS stock, c.price AS price, {extra} AS extra, {jlc} AS jlc"
                              + _FROM + where_sql + _SCAN_ORDER, params)
        matches: list[tuple[Any, int]] = []
        excluded_no_price = 0
        skipped_unreadable = 0
        missing_attribute: Counter = Counter()
        for row in cursor:
            stock = row["stock"]
            if stock is not None and (isinstance(stock, bool) or not isinstance(stock, int)):
                skipped_unreadable += 1
                continue
            price_now = unit_price(row["price"], price_quantity) if (max_price is not None or sort_by == "price") else None
            if max_price is not None:
                if price_now is None:
                    excluded_no_price += 1
                    continue
                if price_now > max_price:
                    continue
            if need_attrs:
                attrs = merged_attributes(row["extra"], row["jlc"])
                ok = True
                for f in attr_filters:
                    value = attrs.get(f["name"])
                    if value is None:
                        missing_attribute[f["name"]] += 1
                        ok = False
                        break
                    if not match_attribute(value, f["op"], f["value"]):
                        ok = False
                        break
                if not ok:
                    continue
            if sort_by == "price":
                key = price_now
            elif sort_by == "stock":
                key = row["stock"]
            else:
                key = row["lcsc"]
            matches.append((key, row["lcsc"]))
        # Parts without a price sort last in either order; ties break by LCSC number ascending.
        has_key = [m for m in matches if m[0] is not None]
        no_key = sorted(m[1] for m in matches if m[0] is None)
        has_key.sort(key=lambda m: m[1])
        has_key.sort(key=lambda m: m[0], reverse=(sort_order == "desc"))
        ordered = [m[1] for m in has_key] + no_key
        total = len(ordered)
        page_ids = ordered[offset: offset + limit]
        items = _fetch_items(conn, columns, page_ids)
        result: dict[str, Any] = {
            "ok": True, "status": "ok" if items else "no_results", "index": info,
            "total_count": total, "count": len(items), "offset": offset, "limit": limit,
            "sort_by": sort_by, "sort_order": sort_order, "items": items,
        }
        end = offset + len(items)
        while len(json.dumps(result, ensure_ascii=False)) > CHARACTER_LIMIT and len(result["items"]) > 1:
            result["items"].pop()
            result["truncated"] = True
            end -= 1
        result["count"] = len(result["items"])
        result["has_more"] = end < total
        result["next_offset"] = end if end < total else None
        if result.get("truncated"):
            result["truncation_message"] = (f"Items were cut to stay under {CHARACTER_LIMIT} characters; "
                                            "continue at next_offset, or lower limit.")
        if skipped_unreadable:
            result["skipped_unreadable_rows"] = skipped_unreadable
        unreadable_items = sum(1 for i in items if "unreadable" in i)
        if unreadable_items:
            result["unreadable_rows"] = unreadable_items
        if excluded_no_price:
            result["excluded_without_price"] = excluded_no_price
        if missing_attribute:
            result["excluded_without_attribute"] = dict(missing_attribute)
        notes = ["Prices are the index's own price tiers; the currency is not recorded in the index (assumed USD)."]
        if any("part" in i and i["attributes"] is None for i in items):
            notes.append("Parts with attributes null have no parameter data in the index: attribute filters cannot match them.")
        result["notes"] = notes
        if not items and total == 0:
            result["hints"] = _value_hints(conn, package, manufacturer)
        return result
    finally:
        conn.close()


def _value_hints(conn: sqlite3.Connection, package: str | None, manufacturer: str | None) -> dict:
    hints: dict[str, Any] = {}
    if package:
        rows = conn.execute("SELECT DISTINCT package FROM components WHERE package LIKE ? ESCAPE '\\' ORDER BY package LIMIT 10",
                            [_like(package.strip())]).fetchall()
        hints["packages_containing"] = [r[0] for r in rows]
    if manufacturer:
        rows = conn.execute("SELECT name FROM manufacturers WHERE name LIKE ? ESCAPE '\\' ORDER BY name LIMIT 10",
                            [_like(manufacturer.strip())]).fetchall()
        hints["manufacturers_containing"] = [r[0] for r in rows]
    return hints


def _fetch_items(conn: sqlite3.Connection, columns: dict, lcsc_ids: list[int]) -> list[dict]:
    if not lcsc_ids:
        return []
    marks, params = _in_clause(lcsc_ids)
    rows = {r["lcsc"]: r for r in conn.execute(f"SELECT {_select_columns(columns)}{_FROM} WHERE c.lcsc IN {marks}", params)}
    items = []
    for lcsc in lcsc_ids:
        row = rows.get(lcsc)
        if row is None:
            continue
        tier = tier_name(row["basic"], row["preferred"])
        tiers = _tiers_of(row["price"])
        attrs = merged_attributes(row["extra"], row["jlc_extra"])
        try:
            part = PartResult(
                manufacturer=row["manufacturer"] or "", mpn=row["mfr"], provider=PROVIDER_NAME, source="scrape",
                distributor="LCSC", stock_qty=row["stock"],
                price_breaks=[PriceBreak(quantity=q, price_usd=p) for q, p in tiers],
                datasheet_url=row["datasheet"] or None, lcsc_id=f"C{lcsc}",
                jlcpcb_basic=(tier == "basic"), jlcpcb_class=tier,
            )
        except (ValidationError, TypeError, ValueError) as exc:
            items.append({"lcsc_id": f"C{lcsc}", "unreadable": "this row of the index does not hold a readable part "
                                                              f"({type(exc).__name__}); it is listed so the page keeps its place"})
            continue
        item: dict[str, Any] = {
            "part": part.model_dump(mode="json"), "package": row["package"], "category": row["category"],
            "subcategory": row["subcategory"], "description": row["description"],
            "attributes": attrs or None,
        }
        if not attrs:
            item["attributes_note"] = "No parameter data for this part in the index."
        items.append(item)
    return items
