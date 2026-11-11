"""Group related ERC findings (issue 127), without touching the flat list.

Input is the raw `kicad-cli sch erc --format json` document, the only place that still holds each
item's uuid. Nothing here reads a KiCad file or writes anything; it is a pure function of that JSON.

The grouping is a PARTITION of the findings, so counts always add up:
``sum(group count) + ungrouped_count == violation_count``. A finding is in at most one group; one that
matches no rule is in none and is listed under ``ungrouped_indexes`` (never dropped). The rules run in
this order and a finding taken by an earlier rule is not offered to a later one:

1. ``same_item``: findings that name the same item uuid (a label, pin or sheet pin) form one group
   (connected components over shared uuids). Measured with KiCad 10.0.7: two labels tied to one
   wire give one ``multiple_net_names`` and two ``isolated_pin_label`` findings that name the same
   label uuid, so they are one cause.
2. ``same_label_text``: findings of one type (``isolated_pin_label``, ``label_dangling``) whose first
   item is a label with the same quoted text. Measured: a local label ``ONLY`` alone on a pin on the
   root sheet and another on a sub-sheet give two ``isolated_pin_label`` findings with different uuids
   and the same quoted text (kicad-cli files both under the root sheet path). Local labels of different sheets never join, so they are separate
   findings with one shared name; the note says so and claims no cause beyond the name.
3. ``same_symbol_pins``: ``pin_not_connected`` findings on the same sheet whose item reads
   ``<word> <reference> <word> <pin> [<electrical type>, <graphic style>]`` and carries the same
   reference. Measured: a resistor with both pins open gives two findings with different pin uuids,
   so the uuid cannot link them; the reference does. Only this shape is read (the words are
   localised by the KiCad language, the bracket suffix and the token positions are not).

Every group is {rule, key, count, severity, type, types, sheets, violation_indexes, note}; the indexes
point into the unchanged ``violations`` list. Groups are ordered by their first index.
"""

from __future__ import annotations

import re
from typing import Any

# Rule 2 applies to these types only (the types the rule was measured on).
LABEL_TEXT_TYPES = ("isolated_pin_label", "label_dangling")

RULES = ("same_item", "same_label_text", "same_symbol_pins")

_QUOTED = re.compile(r'^[^"]*"([^"]+)"[^"]*$')  # exactly one quoted text, else not read
_PIN_ITEM = re.compile(r"^\S+ (?P<ref>\S+) \S+ (?P<pin>\S+) \[[^\]]*,[^\]]*\]$")
_SEVERITY_ORDER = {"error": 3, "warning": 2, "exclusion": 1, "ignore": 0}


def flatten(raw: Any) -> list[dict] | None:
    """The findings of a raw ERC document in the order kicad_tools lists them, or None if unreadable."""
    if not isinstance(raw, dict) or not isinstance(raw.get("sheets"), list):
        return None
    out: list[dict] = []
    for sheet in raw["sheets"]:
        if not isinstance(sheet, dict):
            return None
        path = sheet.get("path", "")
        for item in sheet.get("violations", []) or []:
            if not isinstance(item, dict):
                return None
            out.append({
                "type": item.get("type", "unknown"),
                "severity": item.get("severity", "error"),
                "description": item.get("description", ""),
                "sheet": path,
                "items": [i for i in (item.get("items") or []) if isinstance(i, dict)],
            })
    return out


def _uuids(finding: dict) -> list[str]:
    return [str(i["uuid"]) for i in finding["items"] if i.get("uuid")]


def _first_item_text(finding: dict) -> str:
    items = finding["items"]
    return str(items[0].get("description", "")) if items else ""


def _top_severity(findings: list[dict]) -> str:
    return max((f["severity"] for f in findings), key=lambda s: _SEVERITY_ORDER.get(s, -1))


def _group(rule: str, key: str, indexes: list[int], findings: list[dict], note: str) -> dict:
    members = [findings[i] for i in indexes]
    types = sorted({m["type"] for m in members})
    return {
        "rule": rule,
        "key": key,
        "count": len(indexes),
        "severity": _top_severity(members),
        "type": types[0] if len(types) == 1 else "mixed",
        "types": types,
        "sheets": sorted({m["sheet"] for m in members}),
        "violation_indexes": indexes,
        "note": note,
    }


def _same_item(findings: list[dict], free: set[int]) -> list[dict]:
    parent: dict[int, int] = {i: i for i in free}

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    owner: dict[str, int] = {}
    shared_uuids: set[str] = set()
    for i in sorted(free):
        for u in _uuids(findings[i]):
            if u in owner:
                if owner[u] != i:
                    shared_uuids.add(u)
                parent[find(i)] = find(owner[u])
            else:
                owner[u] = i
    shared_by_root: dict[int, list[str]] = {}
    for u in sorted(shared_uuids):
        shared_by_root.setdefault(find(owner[u]), []).append(u)
    comps: dict[int, list[int]] = {}
    for i in sorted(free):
        comps.setdefault(find(i), []).append(i)
    groups = []
    for root, members in comps.items():
        if len(members) < 2:
            continue
        shared = shared_by_root.get(root, [])
        label = _quoted(_first_item_text(findings[members[0]]))
        what = f" (item {label!r})" if label else ""
        note = (
            f"{len(members)} findings name the same item uuid{what}: they are reported on one and the same "
            f"schematic item or on items tied together by it"
        )
        groups.append(_group("same_item", shared[0] if shared else "", members, findings, note))
        free.difference_update(members)
    return groups


def _quoted(text: str) -> str:
    found = _QUOTED.match(text)
    return found.group(1) if found else ""


def _same_label_text(findings: list[dict], free: set[int]) -> list[dict]:
    buckets: dict[tuple[str, str], list[int]] = {}
    for i in sorted(free):
        f = findings[i]
        if f["type"] in LABEL_TEXT_TYPES:
            text = _quoted(_first_item_text(f))
            if text:
                buckets.setdefault((f["type"], text), []).append(i)
    groups = []
    for (ftype, text), members in buckets.items():
        if len(members) < 2:
            continue
        sheets = sorted({findings[i]["sheet"] for i in members})
        note = (
            f"{len(members)} {ftype} findings carry the label text {text!r} reported under {len(sheets)} sheet path(s) "
            f"({', '.join(sheets)}); local labels of different sheets never join, so check whether the "
            f"counterpart of this name is missing"
        )
        groups.append(_group("same_label_text", f"{ftype}:{text}", members, findings, note))
        free.difference_update(members)
    return groups


def _same_symbol_pins(findings: list[dict], free: set[int]) -> list[dict]:
    buckets: dict[tuple[str, str], list[tuple[int, str]]] = {}
    for i in sorted(free):
        f = findings[i]
        if f["type"] != "pin_not_connected" or len(f["items"]) != 1:
            continue
        m = _PIN_ITEM.match(_first_item_text(f))
        if m:
            buckets.setdefault((f["sheet"], m["ref"]), []).append((i, m["pin"]))
    groups = []
    for (sheet, ref), members in buckets.items():
        if len(members) < 2:
            continue
        indexes = [i for i, _ in members]
        pins = ", ".join(p for _, p in members)
        note = f"symbol {ref} on sheet {sheet or '/'}: {len(members)} unconnected pins ({pins})"
        groups.append(_group("same_symbol_pins", f"{sheet}:{ref}", indexes, findings, note))
        free.difference_update(indexes)
    return groups


def group_findings(findings: list[dict]) -> dict:
    """Partition `findings` (from `flatten`) by the rules above."""
    free = set(range(len(findings)))
    groups = _same_item(findings, free) + _same_label_text(findings, free) + _same_symbol_pins(findings, free)
    groups.sort(key=lambda g: g["violation_indexes"][0])
    return {
        "status": "ok",
        "rules": list(RULES),
        "groups": groups,
        "grouped_count": len(findings) - len(free),
        "ungrouped_count": len(free),
        "ungrouped_indexes": sorted(free),
    }


def grouping_for(raw: Any, listed: list[dict]) -> dict:
    """Grouping of the raw document, refused (no groups) unless it lines up with the listed violations.

    `listed` is the flat list the tool returns (dicts with type_str, description, sheet). A mismatch in
    count, type, description or sheet at any index means the indexes would point at the wrong finding.
    """
    findings = flatten(raw)
    if findings is None:
        return unavailable("the raw ERC report could not be read", len(listed))
    if len(findings) != len(listed):
        return unavailable(f"the raw report holds {len(findings)} findings, the list {len(listed)}", len(listed))
    for i, (f, v) in enumerate(zip(findings, listed, strict=True)):
        if (f["type"], f["description"], f["sheet"]) != (v.get("type_str"), v.get("description"), v.get("sheet")):
            return unavailable(f"finding {i} of the raw report is not finding {i} of the list", len(listed))
    return group_findings(findings)


def unavailable(reason: str, total: int) -> dict:
    """No groups; every one of the `total` findings is listed as ungrouped, so the counts still add up."""
    return {"status": "unavailable", "reason": reason, "rules": list(RULES), "groups": [],
            "grouped_count": 0, "ungrouped_count": total, "ungrouped_indexes": list(range(total))}
