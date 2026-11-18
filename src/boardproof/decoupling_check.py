"""Decoupling placement check: a capacitor on a supply net sits next to an IC supply pin (GitHub issue 26).

Read-only and batch: one call looks at every capacitor of the board. The measure is the one build ticket 114 used
for the rung-1 board (``evals/rung1/build_rung1_layout.py``, ``MAX_PAD_GAP_MM``): the distance between the edge of
the capacitor's supply pad and the edge of the IC's supply pad, both read from the raw ``(at)``, ``(size)`` and
pad angle tokens (``raw_geometry``; the angle is stored board-absolute). Nothing here is a verdict on layout
quality, a route, an inductance or a datasheet rule: it checks one geometric statement, and it says which nets
it treated as supply nets and why, so nothing is guessed silently.

How nets are classified (``classification`` in the result):
  * ground net: the last path segment of the net name matches GND, AGND, DGND, PGND ..., VSS ...;
  * supply net: the caller named it in ``power_nets`` (basis ``caller``), or (unless ``use_name_patterns`` is false)
    its last path segment matches one of ``POWER_NAME_PATTERNS`` (basis ``name_pattern: <regex>``): a voltage
    such as +3V3, +5V, 12V, +1V8, or VCC, VDD, VBUS, VBAT, VIN with an optional suffix;
  * any other net that a capacitor pad and an IC pad share is listed under ``unclassified_shared_nets`` and NOT
    checked: pass it in ``power_nets`` to check it.
An IC is a footprint whose reference matches ``U<digits>`` and a capacitor one matching ``C<digits>`` (the caller
can name both sets). A supply pin is any pad of an IC on a supply net; the board file carries no pin type, so
this is by net, never by what the datasheet says the pin is.
"""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Any

from boardproof import raw_geometry, write_layer

# The rung-1 placement rule of build ticket 114: the same bound, the same pad-edge measure. A test pins the two
# values equal (tests/test_decoupling_check.py).
MAX_PAD_GAP_MM = 1.5
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000

POWER_NAME_PATTERNS = (
    r"^[+-]?\d+(\.\d+)?V\d*(_\w+)?$",
    r"^[ADP]?V(CC|DD|BUS|BAT|BATT|IN)\w*$",
)
GROUND_NAME_PATTERN = r"^([ADPCS]?GND\w*|VSS\w*|0V)$"
_IC_REF = re.compile(r"^U\d+\w*$")
_CAP_REF = re.compile(r"^C\d+\w*$")

NOT_CHECKED = [
    "This is not a verdict on layout quality and it is not verify_board: a board can pass this check and be a bad "
    "layout, and the reverse.",
    "Only one geometric statement is checked per capacitor and supply pin: the pad-edge gap and the board side. Not "
    "checked: the route or its inductance, vias, return paths, the capacitor value, how many capacitors a pin needs, "
    "which capacitor serves which pin beyond the nearest, datasheet placement rules.",
    "Supply nets are found by name (or named by the caller), supply pins are any pad of a U<digits> footprint on such a "
    "net: the board file has no pin type. Check classification.unclassified_shared_nets for nets that were skipped.",
    "Only rect pads are measured exactly; roundrect, oval, circle and custom pads are measured by their bounding rectangle (pad_boxes: bounding), which "
    "can read a smaller gap than the copper has.",
]


def _error(reason: str, **extra: Any) -> dict:
    return {"status": "error", "reason": reason, **extra}


# --- geometry: exact gap between two rectangles (axis-aligned or turned by any angle) ------------------------------


def _rect(centre: tuple[float, float], size: tuple[float, float], angle_deg: float) -> list[tuple[float, float]]:
    """The four corners, page frame (y down), of a pad: its raw size turned by its board-absolute angle."""
    w, h = size
    return [raw_geometry.place(centre, angle_deg, (sx * w / 2, sy * h / 2)) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]


def _cross(o, a, b) -> float:
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _inside(p, poly) -> bool:
    signs = [_cross(poly[i], poly[(i + 1) % 4], p) for i in range(4)]
    return all(s >= -1e-12 for s in signs) or all(s <= 1e-12 for s in signs)


def _segments_cross(a, b, c, d) -> bool:
    d1, d2 = _cross(a, b, c), _cross(a, b, d)
    d3, d4 = _cross(c, d, a), _cross(c, d, b)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and 0 not in (d1, d2, d3, d4)


def _closest_on_segment(p, a, b) -> tuple[float, float]:
    ax, ay = a
    bx, by = b
    vx, vy = bx - ax, by - ay
    length2 = vx * vx + vy * vy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - ax) * vx + (p[1] - ay) * vy) / length2))
    return ax + t * vx, ay + t * vy


def _gap(a: list[tuple[float, float]], b: list[tuple[float, float]]) -> tuple[float, float, float]:
    """(distance, |dx|, |dy|) between the closest points of two convex quadrilaterals; zero when they touch or overlap."""
    if any(_inside(p, b) for p in a) or any(_inside(p, a) for p in b):
        return 0.0, 0.0, 0.0
    for i in range(4):
        for j in range(4):
            if _segments_cross(a[i], a[(i + 1) % 4], b[j], b[(j + 1) % 4]):
                return 0.0, 0.0, 0.0
    best = (math.inf, 0.0, 0.0)
    for poly, other in ((a, b), (b, a)):
        for p in poly:
            for j in range(4):
                q = _closest_on_segment(p, other[j], other[(j + 1) % 4])
                d = math.hypot(p[0] - q[0], p[1] - q[1])
                if d < best[0] - 1e-12:
                    best = (d, abs(p[0] - q[0]), abs(p[1] - q[1]))
    return best


def _octilinear(dx: float, dy: float) -> float:
    """The shortest run that uses horizontal, vertical and 45-degree segments over (dx, dy)."""
    return max(dx, dy) + (math.sqrt(2) - 1) * min(dx, dy)


# --- reading the board ---------------------------------------------------------------------------------------------


def _pad_net(pad_node: Any) -> str:
    net = pad_node.find("net")
    return str(net.values[-1]) if net is not None and net.values else ""


def _pin_function(pad_node: Any) -> str | None:
    node = pad_node.find("pinfunction")
    return str(node.values[0]) if node is not None and node.values else None


def _last_segment(net: str) -> str:
    return net.rsplit("/", 1)[-1]


def _classify(net: str, caller: set[str], patterns: bool) -> tuple[str, str]:
    """("ground" | "power" | "other", basis)."""
    if net in caller:
        return "power", "caller"
    name = _last_segment(net)
    if re.match(GROUND_NAME_PATTERN, name, re.IGNORECASE):
        return "ground", f"name_pattern: {GROUND_NAME_PATTERN}"
    if patterns:
        for pattern in POWER_NAME_PATTERNS:
            if re.match(pattern, name, re.IGNORECASE):
                return "power", f"name_pattern: {pattern}"
    return "other", ""


def _valid_float(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def check_decoupling_placement(
    pcb_path: str | Path,
    *,
    max_pad_gap_mm: float = MAX_PAD_GAP_MM,
    power_nets: list[str] | None = None,
    use_name_patterns: bool = True,
    capacitors: list[str] | None = None,
    ics: list[str] | None = None,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
) -> dict:
    src = Path(pcb_path)
    if not src.is_file():
        return _error(f"file not found: {pcb_path}")
    if not _valid_float(max_pad_gap_mm):
        return _error("max_pad_gap_mm must be a finite number of at least 0")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _error("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        return _error(f"limit must be an integer in [1, {MAX_LIMIT}]")
    for name, value in (("power_nets", power_nets), ("capacitors", capacitors), ("ics", ics)):
        if value is not None and (not isinstance(value, list) or not all(isinstance(v, str) for v in value)):
            return _error(f"{name} must be a list of strings")
    try:
        root = write_layer.load(src)
        footprints = raw_geometry.read_footprints(root)
    except Exception as exc:  # noqa: BLE001 - the message names the file or token
        return _error(f"could not read the board: {exc}")
    nodes = [c for c in root.children if c.name == "footprint"]
    caller_power = set(power_nets or [])

    Pads = list[tuple[raw_geometry.PadGeometry, str, Any]]
    all_fps: list[tuple[raw_geometry.FootprintGeometry, Pads, Pads]] = []  # footprint, usable pads, unnumbered pads
    for fp, node in zip(footprints, nodes, strict=True):
        pad_nodes = [c for c in node.children if c.name == "pad"]
        both = [(p, _pad_net(n), n) for p, n in zip(fp.pads, pad_nodes, strict=True) if p.kind != "np_thru_hole"]
        all_fps.append((fp, [t for t in both if t[0].number], [t for t in both if not t[0].number]))

    count: dict[str, int] = {}
    for fp, _pads, _un in all_fps:
        count[fp.reference] = count.get(fp.reference, 0) + 1
    by_ref = {fp.reference: (fp, pads) for fp, pads, _un in all_fps if fp.reference and count[fp.reference] == 1}

    def selected(ref: str, rule: Any, given: list[str] | None) -> bool:
        return ref in given if given is not None else bool(rule.match(ref))

    unknown = sorted({r for r in (capacitors or []) + (ics or []) if r not in count})
    cap_refs = [r for r in by_ref if selected(r, _CAP_REF, capacitors)]
    ic_refs = [r for r in by_ref if selected(r, _IC_REF, ics)]

    kinds: dict[str, tuple[str, str]] = {}
    for _fp, pads, unnumbered in all_fps:
        for _p, net, _n in pads + unnumbered:
            if net and net not in kinds:
                kinds[net] = _classify(net, caller_power, use_name_patterns)
    power = {n: b for n, (k, b) in kinds.items() if k == "power"}
    ground = sorted(n for n, (k, _b) in kinds.items() if k == "ground")

    skipped: list[dict] = []
    duplicated: set[str] = set()
    for ref, n in sorted(count.items()):
        if ref and n > 1 and (selected(ref, _CAP_REF, capacitors) or selected(ref, _IC_REF, ics)):
            duplicated.add(ref)
            uuids = [fp.uuid for fp, _p, _u in all_fps if fp.reference == ref]
            skipped.append({"reference": ref, "reason": "duplicate_reference", "count": n, "uuids": uuids})
    for fp, pads, unnumbered in all_fps:
        if not fp.reference:
            on_supply = sorted({net for _p, net, _n in pads + unnumbered if net in power})
            if on_supply:
                skipped.append({"reference": "", "reason": "empty_reference", "uuid": fp.uuid, "nets": on_supply})
        elif fp.reference in cap_refs or fp.reference in ic_refs:
            bad = sorted({net for _p, net, _n in unnumbered if net in power})
            if bad:
                skipped.append({"reference": fp.reference, "reason": "unnumbered_pad_on_supply_net", "nets": bad})
    excluded = sorted(
        {
            fp.reference
            for fp, pads, unnumbered in all_fps
            if fp.reference and fp.reference not in cap_refs and fp.reference not in ic_refs
            and fp.reference not in duplicated
            and any(net in power for _p, net, _n in pads + unnumbered)
        }
    )

    ic_pins: dict[str, list[tuple[str, Any, Any, str | None]]] = {}
    for ref in ic_refs:
        fp, pads = by_ref[ref]
        for p, net, n in pads:
            if net in power:
                ic_pins.setdefault(net, []).append((ref, fp, p, _pin_function(n)))

    shared: dict[str, dict[str, set[str]]] = {}
    for ref in cap_refs:
        for _p, net, _n in by_ref[ref][1]:
            if net and kinds[net][0] == "other":
                shared.setdefault(net, {"capacitors": set(), "ics": set()})["capacitors"].add(ref)
    for ref in ic_refs:
        for _p, net, _n in by_ref[ref][1]:
            if net in shared:
                shared[net]["ics"].add(ref)
    unclassified = [
        {"net": n, "capacitors": sorted(v["capacitors"]), "ics": sorted(v["ics"])}
        for n, v in sorted(shared.items())
        if v["capacitors"] and v["ics"]
    ]

    rows: list[dict] = []
    for ref in cap_refs:
        fp, pads = by_ref[ref]
        supply = [(p, net) for p, net, _n in pads if net in power]
        if not supply:
            continue
        nets = {net for _p, net, _n in pads if net}
        if len(pads) == 2 and len(nets) == 1:
            skipped.append({"reference": ref, "reason": "both_pads_on_one_net", "net": next(iter(nets))})
            continue
        for cpad, net in supply:  # each supply pad and its net is judged on its own
            cbox = _rect(cpad.at_mm, cpad.size_mm, cpad.angle_deg)
            pairs = []
            for ic_ref, ic_fp, ipad, function in ic_pins.get(net, []):
                gap, dx, dy = _gap(cbox, _rect(ipad.at_mm, ipad.size_mm, ipad.angle_deg))
                gap = round(gap, 4)  # the figure shown is the figure compared with the bound
                both_sides = cpad.kind == "thru_hole" or ipad.kind == "thru_hole"
                bounding = cpad.shape != "rect" or ipad.shape != "rect"
                pairs.append({
                    "net": net,
                    "ic": ic_ref,
                    "pin": ipad.number,
                    "pin_function": function,
                    "capacitor_pad": cpad.number,
                    "gap_mm": gap,
                    "path_estimate_mm": round(_octilinear(dx, dy), 4),
                    "manhattan_mm": round(dx + dy, 4),
                    "centre_distance_mm": round(math.hypot(cpad.at_mm[0] - ipad.at_mm[0], cpad.at_mm[1] - ipad.at_mm[1]), 4),
                    "same_side": both_sides or fp.side == ic_fp.side,
                    "capacitor_side": fp.side,
                    "ic_side": ic_fp.side,
                    "over_threshold": gap > max_pad_gap_mm,
                    "pad_boxes": "bounding" if bounding else "exact",
                })
            if not pairs:
                skipped.append({"reference": ref, "reason": "no_supply_pin_on_net", "nets": [net]})
                continue
            pairs.sort(key=lambda p: (p["gap_mm"], p["ic"], p["pin"]))
            same = [p for p in pairs if p["same_side"]]
            nearest = (same or pairs)[0]
            reasons = []
            if not same:
                reasons.append("different_side")
            if nearest["gap_mm"] > max_pad_gap_mm:
                reasons.append("too_far")
            status = "different_side" if not same else "too_far" if reasons else "ok"
            rows.append({
                "capacitor": ref,
                "supply_pad": cpad.number,
                "net": net,
                "side": fp.side,
                "status": status,
                "reasons": reasons,
                "nearest": nearest,
                "pairs_total": len(pairs),
                "pairs": pairs,
            })

    rows.sort(key=lambda r: (r["status"] == "ok", -r["nearest"]["gap_mm"], r["capacitor"], r["supply_pad"]))
    flagged = sum(1 for r in rows if r["status"] != "ok")
    gaps = []
    if not rows:
        gaps.append("nothing_checked")
    if skipped:
        gaps.append("skipped")
    if unclassified:
        gaps.append("unclassified_shared_nets")
    if excluded:
        gaps.append("excluded_footprints_on_supply_nets")
    page = rows[offset: offset + limit]
    end = offset + len(page)
    return {
        "status": "ok" if rows else "nothing_checked",
        "threshold_mm": max_pad_gap_mm,
        "measure": "gap between the capacitor's supply pad edge and the IC supply pad edge, raw pad (at)/(size)/angle",
        "verdict_is_layout_quality": False,
        "summary": {
            "checked": len(rows),
            "flagged": flagged,
            "ok": len(rows) - flagged,
            "skipped": len(skipped),
            # true only when something was checked and every checked supply pad met the bound and the side rule; it
            # says nothing about what was skipped or not classified: read coverage_complete beside it
            "all_checked_within_bound": bool(rows) and flagged == 0,
            # false whenever nothing was checked, something was skipped, a shared net was not classified, or a footprint on a
            # supply net was left out by the reference rule (nothing on a supply net may be left unjudged)
            "coverage_complete": not gaps,
            "coverage_gaps": gaps,
        },
        "classification": {
            "power_nets": [{"net": n, "basis": b} for n, b in sorted(power.items())],
            "ground_nets": ground,
            "unclassified_shared_nets": unclassified,
            "capacitors": sorted(cap_refs),
            "ics": sorted(ic_refs),
            "unknown_references": unknown,
            "excluded_by_reference_rule_on_supply_nets": excluded,
            "supply_pin_rule": "any pad of an IC footprint on a supply net (the board file carries no pin type)",
            "use_name_patterns": use_name_patterns,
            "name_patterns": list(POWER_NAME_PATTERNS) if use_name_patterns else [],
        },
        "skipped": skipped,
        "capacitors": {
            "total": len(rows),
            "offset": offset,
            "returned": len(page),
            "all_listed": end >= len(rows),
            "next_offset": end if end < len(rows) else None,
            "items": page,
        },
        "not_checked": NOT_CHECKED,
    }
