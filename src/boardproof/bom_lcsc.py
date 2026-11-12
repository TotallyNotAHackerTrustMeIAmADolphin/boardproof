"""check_bom_against_lcsc: one read-only call that checks the parts of a project against LCSC data (issue 123).

Per placed part (one line per reference; a multi-unit part is one line): the LCSC number as the
schematic holds it, the JLCPCB stage (Basic / Preferred Extended / Extended / unknown) from the vendored
local index, the stock where a registered provider supplies it, and whether the placed footprint fits the
package of the part.

Where the data comes from (measured, see the issue's Answer):

* **Stage and package: the local index only** (``sourcing/data/jlcpcb_basic_index.json``, 889 parts, no
  network). The package of a part is the library footprint the index records for it (``PCM_JLCPCB:C_0402``,
  ``PCM_JLCPCB:SOIC-8_L5.0-...``), reduced to a package token by ``package_token``. A part that is not in the
  index has NO package offline: its fit is ``unknown``, never a pass. No provider in this repository returns a
  package (``PartResult`` has no such field and the LCSC scrape is behind a bot wall), and the placed footprint
  name says nothing about the part, so it is not used as the part's package.
* **Stock: a provider, only when ``include_stock`` is true** (network, one search per distinct LCSC number, at
  most ``max_stock_lookups``). Without it, or when no provider answers, stock is ``unknown`` with the reason;
  it is never guessed and never zero by default.
* **Placed footprint**: the Footprint property of the schematic symbol, or, when ``pcb_path`` is given, the
  footprint of the same reference on the board (read from the raw ``(footprint "<lib_id>" ...)`` token).

Fit rule: both sides must reduce to a known package token (an imperial passive size such as 0402 for R, C, L,
D, LED and FB footprints, or a family with its pin count such as SOIC-8, SOT-23-5, SMA); equal tokens are
``match``, different tokens are ``mismatch`` and an ERROR line, anything else is ``unknown`` with the reason.
The check reports. It does not say that a BOM can be built.
"""

from __future__ import annotations

import re
from typing import Any

from boardproof import field_check, sheet_authoring
from boardproof.sheet_authoring import SheetAuthoringError
from boardproof.sourcing import jlcpcb_basic as jlc

LCSC_RE = re.compile(r"^C[0-9]+$")
DEFAULT_MAX_STOCK_LOOKUPS = 50
HARD_MAX_STOCK_LOOKUPS = 200
_STOP_AFTER_FAILURES = 3

TIER_LABELS = {"basic": "Basic", "preferred": "Preferred Extended", "extended": "Extended", None: "unknown"}

_SIZES = "01005|0201|0402|0603|0805|1008|1206|1210|1812|2010|2512"
_PASSIVE = re.compile(rf"^(?:R|C|L|D|LED|FB|F|Fuse|CP|NTC|RV)_(?:SMD_)?({_SIZES})(?![0-9A-Za-z])", re.IGNORECASE)
_SO = re.compile(r"^(SOIC|SSOP|TSSOP|MSOP|SOP)-(\d+)([^_]*)(?:_(.*))?$", re.IGNORECASE)
_WIDTH_INDEX = re.compile(r"(?:^|[-_])W(\d+(?:\.\d+)?)(?![0-9])")
_STANDARD_WIDTHS = (3.0, 4.4, 5.3, 6.1)
_PITCH = re.compile(r"(?:^|[-_])P(\d+(?:\.\d+)?)(?:mm)?(?![0-9])")
_WIDTH_KICAD = re.compile(r"^(\d+(?:\.\d+)?)x\d+(?:\.\d+)?mm")
_SOT23 = re.compile(r"^SOT-23(?:-(\d+)L?)?(?![0-9A-Za-z])", re.IGNORECASE)
_SOT = re.compile(r"^SOT-(323|223|89)(?:-(\d+)L?)?(?![0-9A-Za-z])", re.IGNORECASE)
_SOT363 = re.compile(r"^SOT-363(?![0-9A-Za-z])", re.IGNORECASE)
_SOD = re.compile(r"^SOD-(123|323|523)(?:FL|F)?(?![0-9A-Za-z])", re.IGNORECASE)
_SM = re.compile(r"^(SMA|SMB|SMC)(?![0-9A-Za-z-])|^DO-214(AC|AA|AB)(?![0-9A-Za-z])", re.IGNORECASE)
_DO214 = {"AC": "SMA", "AA": "SMB", "AB": "SMC"}

PACKAGE_RULES_NOTE = (
    "What is compared: a package family with its pin count, or an imperial passive size. Recognised: imperial passive "
    "sizes (01005 to 2512) on R, C, L, D, LED, FB and fuse footprints; SOIC and SOP as one family (narrow 3.9 mm body "
    "only) with a pin count and pitch; SSOP, TSSOP and MSOP with a pin count, body width and pitch; SOT-23 (3 pins when unnumbered), "
    "SOT-323, SOT-363, SOT-223, SOT-89, SOD-123, SOD-323, SOD-523 (the F and FL forms count as the plain one); SMA, "
    "SMB and SMC (DO-214AC, AA and AB) on diode footprints only. Width variants (W suffix, wide bodies), exposed-pad "
    "variants (-nEP), universal footprints for two diode sizes and connector packages such as an SMA coax connector are not compared: their fit is unknown. "
    "SSOP, TSSOP and MSOP are compared only for the body widths 3.0, 4.4, 5.3 and 6.1 mm (within 0.03) and only inside one family: "
    "the same pin count in two of the three families is unknown. Any other package is also unknown, never a match. "
    "Fit compares package size and family only: it does not tell R, C, L, D and LED apart (a resistor part on an LED_0603 footprint is a match)."
)


def _body_width(rest: str | None) -> float | None:
    if not rest:
        return None
    if m := _WIDTH_INDEX.search(rest):
        return float(m.group(1))
    if m := _WIDTH_KICAD.match(rest):
        return float(m.group(1))
    return None


def package_token(name: Any) -> str | None:
    """The package token of a footprint name (library prefix ignored), or None when it is not one of the recognised packages."""
    if not isinstance(name, str):
        return None
    bare = name.rpartition(":")[2].strip()
    if not bare:
        return None
    if m := _PASSIVE.match(bare):
        return m.group(1)
    diode = bare[:2].upper() == "D_"
    rest = bare[2:] if bare[:2].upper() in ("D_", "Q_") else bare
    if m := _SO.match(rest):
        family, pins, suffix, tail = m.groups()
        width = _body_width(tail)
        pitch = _PITCH.search(tail or "")
        if suffix or width is None or pitch is None:  # a W suffix, -nEP or anything else after the pin count: not the plain package
            return None
        if family.upper() in ("SOIC", "SOP"):
            return f"SOIC-{pins}-p{float(pitch.group(1)):g}" if 3.8 <= width <= 4.1 else None
        standard = next((w for w in _STANDARD_WIDTHS if abs(width - w) <= 0.03), None)
        if standard is None:  # not a standard body width: never rounded onto one
            return None
        return f"{family.upper()}-{pins}-w{standard:g}-p{float(pitch.group(1)):g}"
    if m := _SOT23.match(rest):
        return f"SOT-23-{m.group(1) or '3'}"
    if m := _SOT.match(rest):
        return f"SOT-{m.group(1)}-{m.group(2) or '3'}"
    if _SOT363.match(rest):
        return "SOT-363"
    if m := _SOD.match(rest):
        return f"SOD-{m.group(1)}"
    if diode and (m := _SM.match(rest)):
        return m.group(1).upper() if m.group(1) else _DO214[m.group(2).upper()]
    return None


_SO_FAMILY = re.compile(r"^(SSOP|TSSOP|MSOP)-(\d+)-")


def _cross_family(a: str, b: str) -> bool:
    """SSOP, TSSOP or MSOP on both sides, different families, the same pin count."""
    ma, mb = _SO_FAMILY.match(a), _SO_FAMILY.match(b)
    return bool(ma and mb and ma.group(1) != mb.group(1) and ma.group(2) == mb.group(2))


def footprint_fit(expected_footprint: str | None, placed_footprint: str | None) -> dict[str, Any]:
    """Compare the package of the part (from its index footprint) with the placed footprint: match, mismatch or unknown, with the reason."""
    expected = package_token(expected_footprint)
    placed = package_token(placed_footprint)
    out: dict[str, Any] = {"part_package": expected, "placed_package": placed}
    if expected is None and placed is None:
        out.update(fit="unknown", reason="neither the part's package nor the placed footprint is a recognised package")
    elif expected is None:
        out.update(fit="unknown", reason="the part's package is not known offline (not in the local index, or not a recognised package)")
    elif placed is None:
        out.update(fit="unknown", reason=f"the placed footprint {placed_footprint!r} is not a recognised package; the part is {expected}")
    elif expected != placed and _cross_family(expected, placed):
        out.update(fit="unknown", reason=f"the part is {expected} and the placed footprint is {placed}: SSOP, TSSOP and MSOP of one pin count "
                                         "can share a land pattern or differ in pads, so the family is not decided")
    elif expected == placed:
        out.update(fit="match", reason=f"part {expected}, placed footprint {placed}")
    else:
        out.update(fit="mismatch", reason=f"the part is {expected} but the placed footprint {placed_footprint!r} is {placed}")
    return out


def _lcsc_state(props: dict[str, str]) -> tuple[str | None, str, list[str] | None]:
    """(number, status, case variants): status is ok, missing, blank, placeholder or malformed."""
    raw = props.get("LCSC")
    if raw is None:
        variants = sorted(p for p in props if p != "LCSC" and p.lower() == "lcsc")
        return None, "missing", variants or None
    value = raw.strip()
    if value == "":
        return None, "blank", None
    if value == field_check.PLACEHOLDER:
        return None, "placeholder", None
    if not LCSC_RE.match(value):
        return value, "malformed", None
    return value, "ok", None


def _pcb_footprints(pcb_path: str) -> dict[str, list[str]]:
    """Every footprint library id of the board by reference, in file order (a reference used twice keeps both)."""
    from boardproof import raw_geometry

    out: dict[str, list[str]] = {}
    for fp in raw_geometry.read_footprints(pcb_path):
        if fp.reference:
            out.setdefault(fp.reference, []).append(fp.lib_id)
    return out


def _stock_for(numbers: list[str], max_lookups: int, registry: Any) -> dict[str, dict[str, Any]]:
    """One provider search per distinct LCSC number (the cap and a stop after repeated total failures are reported per number)."""
    out: dict[str, dict[str, Any]] = {}
    providers = registry.providers()
    failures = 0
    for index, number in enumerate(numbers):
        if not providers:
            out[number] = {"status": "unknown", "qty": None, "source": None, "reason": "no sourcing provider is registered"}
            continue
        if index >= max_lookups:
            out[number] = {"status": "not_queried", "qty": None, "source": None,
                           "reason": f"over max_stock_lookups ({max_lookups}); raise it or call again with fewer references"}
            continue
        if failures >= _STOP_AFTER_FAILURES:
            out[number] = {"status": "not_queried", "qty": None, "source": None,
                           "reason": f"stopped after {_STOP_AFTER_FAILURES} searches in a row where every provider failed"}
            continue
        try:
            results, errors = registry.search_all(number, limit=5)
        except Exception as exc:  # noqa: BLE001 -- report the gap, never raise out of a read-only check
            results, errors = [], [{"provider": "?", "error": str(exc)}]
        hits = [r for r in results if (r.lcsc_id or "").strip().upper() == number.upper()]
        stocked = [r for r in hits if r.stock_qty is not None]
        if stocked:
            best = stocked[0]
            failures = 0
            out[number] = {"status": "known", "qty": int(best.stock_qty), "source": best.provider, "source_kind": best.source}
        elif errors and len(errors) >= len(providers):
            failures += 1
            out[number] = {"status": "unknown", "qty": None, "source": None, "provider_errors": errors,
                           "reason": "every provider failed; this says nothing about the stock"}
        else:
            failures = 0
            out[number] = {"status": "unknown", "qty": None, "source": None,
                           "reason": "no provider returned a stock figure for this number"
                                     + (" (some providers failed)" if errors else ""),
                           **({"provider_errors": errors} if errors else {})}
    return out


def check_bom(
    sch_path: str, references: Any = None, root_sch: str | None = None, pcb_path: str | None = None,
    include_stock: bool = False, max_stock_lookups: int = DEFAULT_MAX_STOCK_LOOKUPS,
    *, index: Any = None, registry: Any = None,
) -> dict[str, Any]:
    """Read-only: one line per placed part of the project (or of ``references``) checked against LCSC data."""
    try:
        wanted = field_check._names(references, "references")
        if not isinstance(include_stock, bool):
            raise SheetAuthoringError("invalid_spec", "include_stock must be true or false")
        if isinstance(max_stock_lookups, bool) or not isinstance(max_stock_lookups, int) or not 0 <= max_stock_lookups <= HARD_MAX_STOCK_LOOKUPS:
            raise SheetAuthoringError("invalid_spec", f"max_stock_lookups must be an integer from 0 to {HARD_MAX_STOCK_LOOKUPS}")
        board: dict[str, list[str]] | None = None
        if pcb_path is not None:
            try:
                board = _pcb_footprints(pcb_path)
            except Exception as exc:  # noqa: BLE001 -- an unreadable board is a refusal, not a silent fall back to the schematic
                raise SheetAuthoringError("pcb_unreadable", f"could not read the footprints of {pcb_path}: {exc}") from exc
        root_file, tree = field_check._resolve(sch_path, root_sch)

        want = set(wanted or ())
        found: set[str] = set()
        skipped: dict[str, list[dict[str, Any]]] = {reason: [] for reason in field_check._SKIP_ORDER}
        by_ref: dict[str, dict[str, Any]] = {}
        placed = 0
        for rel, rows in field_check._walk(tree):
            for sym in rows:
                placed += 1
                if not sym.lib_id:
                    for ref in sym.refs:
                        if wanted is None or ref in want:
                            found.add(ref)
                            skipped["no_lib_id"].append({"reference": ref, "uuid": sym.uuid, "unit": sym.unit, "lib_id": "", "sheet": rel})
                    continue
                why = field_check._skip_reason(sym.parsed, sym.node, [*sym.refs, sym.own])
                props = field_check._props(sym.node)
                for ref in sym.refs:  # a sheet file used more than once: one line per instance reference
                    if wanted is not None and ref not in want:
                        continue
                    found.add(ref)
                    head = {"reference": ref, "uuid": sym.uuid, "unit": sym.unit, "lib_id": sym.lib_id, "sheet": rel}
                    if why is not None:
                        skipped[why].append(head)
                        continue
                    row = {**head, "value": props.get("Value", ""), "props": props}
                    if ref in by_ref:  # another unit of a multi-unit part: one line, the units' fields compared
                        first = by_ref[ref]
                        diff = [n for n in ("LCSC", "Footprint") if first["props"].get(n, "") != props.get(n, "")]
                        if diff:
                            first.setdefault("units_disagree", {})[sym.unit] = {n: props.get(n) for n in diff}
                        first["units"].append(sym.unit)
                    else:
                        row["units"] = [sym.unit]
                        by_ref[ref] = row

        lines = [by_ref[k] for k in sorted(by_ref, key=lambda r: (by_ref[r]["sheet"], r))]
        numbers = sorted({n for line in lines if (n := _lcsc_state(line["props"])[0]) and LCSC_RE.match(n)})
        tiers = jlc.classify_parts(numbers, index=index) if numbers else {"status": "ok", "results": []}
        index_unavailable = tiers.get("status") == "unavailable"
        entry_by_number: dict[str, dict] = {}
        for r in tiers.get("results", []):
            if r.get("matched"):
                entry_by_number[r["identifier"].upper()] = r
        stock: dict[str, dict[str, Any]] = {}
        if include_stock and numbers:
            from boardproof.sourcing import PROVIDER_REGISTRY

            stock = _stock_for(numbers, max_stock_lookups, registry if registry is not None else PROVIDER_REGISTRY)
        needed: dict[str, int] = {}
        for line in lines:
            n = _lcsc_state(line["props"])[0]
            if n:
                needed[n] = needed.get(n, 0) + 1

        out_lines: list[dict[str, Any]] = []
        for line in lines:
            props = line.pop("props")
            number, state, variants = _lcsc_state(props)
            findings: list[dict[str, str]] = []
            sch_fp = props.get("Footprint", "")
            copies = board.get(line["reference"]) if board is not None else None
            ambiguous = bool(copies) and len(set(copies)) > 1
            pcb_fp = copies[0] if copies and not ambiguous else None
            if copies and len(copies) > 1:
                findings.append({"severity": "warning", "code": "duplicate_board_reference",
                                 "message": f"the board has {len(copies)} footprints with this reference ({sorted(set(copies))})"
                                            + ("; they differ, so the fit is not decided" if ambiguous else "")})
            placed_fp = "" if ambiguous else pcb_fp if pcb_fp is not None else sch_fp.strip()
            out: dict[str, Any] = {k: line[k] for k in ("reference", "uuid", "sheet", "lib_id", "value", "units")}
            out["lcsc"] = number
            out["lcsc_status"] = state
            if state != "ok":
                msg = {"missing": "the symbol has no LCSC property", "blank": "the LCSC property is blank",
                       "placeholder": "the LCSC property is the placeholder ~",
                       "malformed": f"the LCSC property {number!r} is not of the form C<digits>"}[state]
                if variants:
                    msg += f" (a property named {variants} exists; names are case sensitive)"
                findings.append({"severity": "warning", "code": f"lcsc_{state}", "message": msg})
            entry = entry_by_number.get(number.upper()) if number and state == "ok" else None
            tier = entry["jlcpcb_class"] if entry else None
            out["stage"] = TIER_LABELS[tier]
            out["tier"] = tier
            out["stage_source"] = "local_index" if entry else None
            if state == "ok" and entry is None:
                out["stage_reason"] = (tiers.get("reason") if index_unavailable else
                                       "not in the local JLCPCB index (Basic and Preferred parts plus a few Extended): the stage is unknown, not Extended")
                findings.append({"severity": "info", "code": "stage_unknown", "message": out["stage_reason"]})
            if number and state == "ok":
                s = stock.get(number) or {
                    "status": "not_queried" if not include_stock else "unknown", "qty": None, "source": None,
                    "reason": "include_stock is false: no network call was made" if not include_stock else "not looked up"}
                out["stock"] = s
                if s["status"] == "known" and s["qty"] == 0:
                    findings.append({"severity": "warning", "code": "out_of_stock", "message": f"{number} has stock 0 at {s['source']}"})
                elif s["status"] == "known" and s["qty"] < needed.get(number, 1):
                    findings.append({"severity": "warning", "code": "stock_below_one_board",
                                     "message": f"{number} has stock {s['qty']} at {s['source']}, {needed[number]} are placed on one board"})
            else:
                out["stock"] = {"status": "unknown", "qty": None, "source": None, "reason": "no usable LCSC number on this line"}
            out["placed_footprint"] = placed_fp
            out["placed_footprint_source"] = "pcb" if board is not None and copies else "schematic"
            if board is not None and not copies:
                findings.append({"severity": "warning", "code": "not_on_board", "message": "no footprint with this reference on the board; the schematic Footprint property was used"})
            if board is not None and pcb_fp is not None and sch_fp.strip() and pcb_fp.split(":", 1)[-1] != sch_fp.strip().split(":", 1)[-1]:
                findings.append({"severity": "warning", "code": "schematic_pcb_footprint_differs",
                                 "message": f"the schematic Footprint is {sch_fp.strip()!r} but the board has {pcb_fp!r}; the board's is checked"})
            if not placed_fp and not ambiguous:
                findings.append({"severity": "warning", "code": "footprint_missing", "message": "no footprint is assigned"})
            fit = footprint_fit(entry.get("footprint") if entry else None, placed_fp)
            if not entry:
                fit["part_package_source"] = None
            else:
                fit["part_package_source"] = "local_index"
                fit["index_footprint"] = entry.get("footprint")
            out["footprint_fit"] = fit
            if fit["fit"] == "mismatch":
                findings.append({"severity": "error", "code": "package_footprint_mismatch", "message": fit["reason"]})
            if "units_disagree" in line:
                findings.append({"severity": "warning", "code": "units_disagree",
                                 "message": f"other units carry different LCSC or Footprint values: {line['units_disagree']}; the first unit is used"})
            out["findings"] = findings
            out["severity"] = "error" if any(f["severity"] == "error" for f in findings) else \
                "warning" if any(f["severity"] == "warning" for f in findings) else "info" if findings else "ok"
            out_lines.append(out)

        for rows in skipped.values():
            rows.sort(key=lambda s: (s["sheet"], s["reference"], s["unit"], s["uuid"]))
        errors = [ln["reference"] for ln in out_lines if ln["severity"] == "error"]
        def count(sev: str) -> int:
            return sum(1 for ln in out_lines for f in ln["findings"] if f["severity"] == sev)
        loaded = index if index is not None else jlc.load_index()
        info = jlc.index_info(loaded) if loaded is not None else None
        return {
            "status": "ok", "ok": True, "written": False, "root": tree.rel(root_file),
            "selected_by": "references" if wanted is not None else "all",
            "placed_symbols": placed, "lines": out_lines, "line_count": len(out_lines),
            "has_errors": bool(errors), "error_count": count("error"), "error_references": errors,
            "warning_count": count("warning"),
            "stage_summary": {label: sum(1 for ln in out_lines if ln["stage"] == label) for label in TIER_LABELS.values()},
            "fit_summary": {k: sum(1 for ln in out_lines if ln["footprint_fit"]["fit"] == k) for k in ("match", "mismatch", "unknown")},
            "not_found": sorted(want - found), "all_found": want <= found,
            "skipped": skipped, "skipped_counts": {reason: len(rows) for reason, rows in skipped.items()},
            "data": {
                "stage_and_package": "local index only, no network" + ("" if info else " (the index could not be loaded: every stage is unknown)"),
                "index": info,
                "stock": ("looked up through the registered sourcing providers (network), at most max_stock_lookups distinct LCSC numbers"
                          if include_stock else "not queried: include_stock is false, so no network call was made and every stock is unknown"),
                "without_network": "stage and package fit still work; stock is unknown; nothing is cached on disk, so a failed provider is not answered from an older result",
                "placed_footprint": "the board's footprint of the reference" if board is not None else "the Footprint property of the schematic symbol (pass pcb_path to check the board's footprint)",
                "package_rules": PACKAGE_RULES_NOTE,
            },
            "note": "This reports. A line with no error is not a statement that the part is right or that the BOM can be built; unknown means no data, not a pass.",
            **{k: v for k, v in tree.report().items() if k != "sheets"},
        }
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)

