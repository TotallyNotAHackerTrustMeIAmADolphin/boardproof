"""Clearance coverage: which rule holds every pair of nets apart (GitHub issue 131).

Read-only. For a board it reads the project's net classes and board minimum and the custom rules
file (``.kicad_dru``, hand-written or written by ``confirm_board_rules``), asks KiCad whether it loads
that file at all (``dru.load_probe``: one defect makes KiCad drop every rule, silently), and then
resolves every unordered pair of nets the way KiCad does (measured on kicad-cli 10.0.7, see
``dru``): the LAST matching clearance rule of the file wins and replaces the class clearance and the
board minimum; with no matching rule the largest of the two net classes' clearances and the board
minimum applies. A rule whose condition depends on something this module does not model (an item
type, a layer, a wildcard) is never guessed: it is reported as ``may_be_overridden_by`` on every
pair it could touch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from boardproof import board_rules, dru, kicad_cli
from boardproof import verify_board as gate

MAX_NETS = 800
DEFAULT_LIMIT = 200
MAX_LIMIT = 2000

NOTES = [
    "Only clearance between copper of two different nets is covered: a rule with a clearance constraint is held between "
    "tracks, vias, pads and zone fills on a common layer (measured: track to track, pad to track and via to track are "
    "reported as violations; a zone fill is pulled back to the rule's value instead).",
    "A matching clearance rule REPLACES the class clearance and the board minimum, in either direction (measured): the "
    "value shown for a pair is the one KiCad enforces, not the largest of several.",
    "Other constraints (creepage, hole and edge clearance, track width, ...) are listed under rules but are not part of "
    "this matrix.",
]


def _error(reason: str, **extra: Any) -> dict:
    return {"status": "error", "reason": reason, **extra}


def _ctx_for(chains: dict[str, tuple[str, ...]], net: str) -> dru.NetCtx:
    return dru.NetCtx(net, chains[net])


def _resolve(
    rules: list[dru.Rule], a: dru.NetCtx, b: dru.NetCtx, base: tuple[float, str, str | None]
) -> dict:
    winner: dru.Rule | None = None
    unknown: list[dru.Rule] = []
    for rule in rules:
        clearance = rule.clearance()
        if clearance is None or clearance.min_mm is None:
            continue
        matched = dru.matches(rule.condition, a, b)
        if matched is not False and rule.layer:
            matched = None  # applies on some layers only: never claimed for the pair as a whole
        if matched is True:
            winner, unknown = rule, []
        elif matched is None:
            unknown.append(rule)
    if winner is not None:
        value = winner.clearance().min_mm  # type: ignore[union-attr]
        item = {
            "clearance_mm": round(value, 6),
            "source": "rule",
            "rule": winner.name,
            "managed_by_boardproof": winner.managed,
            "checked": winner.severity != "ignore",
            "severity": winner.severity or "error",
        }
    else:
        value, source, name = base
        item = {
            "clearance_mm": round(value, 6),
            "source": source,
            "rule": name,
            "managed_by_boardproof": False,
            "checked": True,
            "severity": "error",
        }
    if unknown:
        item["may_be_overridden_by"] = [r.name for r in unknown]
    return item


def report_clearance_coverage(
    pcb_path: str | Path,
    *,
    check_loading: bool = True,
    offset: int = 0,
    limit: int = DEFAULT_LIMIT,
    drc: Any = None,
) -> dict:
    src = Path(pcb_path)
    if not src.is_file():
        return _error(f"file not found: {pcb_path}")
    if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
        return _error("offset must be a non-negative integer")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        return _error(f"limit must be an integer in [1, {MAX_LIMIT}]")
    project = src.with_suffix(".kicad_pro")
    if not project.is_file():
        return _error(f"no project file next to the board ({project.name}): net classes live there")
    try:
        data = board_rules._read_project(project)
        nets = sorted(board_rules._board_net_names(src))
    except Exception as exc:  # noqa: BLE001
        return _error(f"could not read the board or its project: {exc}")
    if not nets:  # never "complete" over nothing: a net form the reader missed would look the same
        return _error("the board has no nets, so no pair of nets was checked")
    if len(nets) > MAX_NETS:
        return _error(f"{len(nets)} nets: more than the {MAX_NETS} this report resolves pair by pair", net_count=len(nets))
    chains: dict[str, tuple[str, ...]] = {}
    try:
        board_rules._check_patterns(data)
        for net in nets:
            chains[net] = tuple(board_rules._netclass_chain(data, net))
    except board_rules.UnsupportedNetclassPattern as exc:
        return board_rules._project_refusal(exc)

    text, problem = board_rules._rules_text(src)
    if problem:
        return _error(problem)
    rules: list[dru.Rule] = []
    own_problems: list[str] = []
    if text is not None:
        rules, _version, own_problems = dru.parse(text)
    loading: dict[str, Any] = {"loaded": None, "reason": "no rules file" if text is None else "not checked"}
    if text is not None and check_loading:
        try:
            loading = dru.load_probe(src, text, drc or kicad_cli.run_drc, max_bytes=gate._MAX_COPY_BYTES)
        except Exception as exc:  # noqa: BLE001  (kicad-cli missing, project too large, ...)
            loading = {"loaded": None, "reason": f"the load probe could not run: {exc}"}
    # the probe is authoritative: the parser's defects count only where KiCad could not be asked
    ignored_by_kicad = loading["loaded"] is False or (loading["loaded"] is None and bool(own_problems))
    effective_rules = [] if ignored_by_kicad else rules

    board_min = board_rules._constraint_before(data, "min_clearance")[0] or 0.0
    values: dict[str, tuple[float, str]] = {}
    for net in nets:
        value, source = board_rules._net_value(data, net, "clearance")
        values[net] = (value or 0.0, source or board_rules._DEFAULT_CLASS)

    ctx = {net: _ctx_for(chains, net) for net in nets}
    total = len(nets) * (len(nets) - 1) // 2
    page: list[dict] = []
    groups: dict[tuple, dict] = {}
    index = 0
    for i, a in enumerate(nets):
        for b in nets[i + 1 :]:
            va, ca = values[a]
            vb, cb = values[b]
            if board_min >= va and board_min >= vb:
                base = (board_min, "board_minimum", None)
            elif va >= vb:
                base = (va, "netclass", ca)
            else:
                base = (vb, "netclass", cb)
            item = _resolve(effective_rules, ctx[a], ctx[b], base)
            classes = tuple(sorted((ctx[a].effective, ctx[b].effective)))
            key = (classes, item["clearance_mm"], item["source"], item["rule"], item["checked"], tuple(item.get("may_be_overridden_by", ())))
            group = groups.setdefault(
                key,
                {
                    "a_class": classes[0],
                    "b_class": classes[1],
                    "clearance_mm": item["clearance_mm"],
                    "source": item["source"],
                    "rule": item["rule"],
                    "checked": item["checked"],
                    **({"may_be_overridden_by": list(key[5])} if key[5] else {}),
                    "net_pairs": 0,
                },
            )
            group["net_pairs"] += 1
            if offset <= index < offset + limit:
                page.append({"a": a, "b": b, "a_class": ctx[a].effective, "b_class": ctx[b].effective, **item})
            index += 1

    unevaluated = sorted(
        {
            name
            for entry in groups.values()
            for name in entry.get("may_be_overridden_by", ())
        }
    )
    partial_reasons: list[str] = []
    if ignored_by_kicad:
        partial_reasons.append(
            "KiCad ignores the rules file (every rule of it, silently): the matrix below is what applies WITHOUT it"
        )
    if loading["loaded"] is None and text is not None:
        partial_reasons.append(f"whether KiCad loads the rules file is not known: {loading['reason']}")
    if unevaluated:
        partial_reasons.append(f"rule(s) {unevaluated} have conditions this report cannot evaluate (item type, layer or wildcard)")
    return {
        "status": "partial" if partial_reasons else "complete",
        "partial_reasons": partial_reasons,
        "pcb_path": str(src),
        "rules_file": src.with_suffix(dru.DRU_SUFFIX).name if text is not None else None,
        "rules_file_loaded_by_kicad": loading["loaded"],
        "rules_file_probe": loading["reason"],
        "rules_file_problems": own_problems,
        "rules_ignored_by_kicad": ignored_by_kicad,
        "board_minimum_clearance_mm": board_min,
        "rules": [r.as_dict() for r in rules],
        "classes": [
            {"name": name, "nets": sorted(n for n in nets if ctx[n].effective == name)}
            for name in sorted({c.effective for c in ctx.values()})
        ],
        "class_pairs": sorted(
            groups.values(), key=lambda g: (g["a_class"], g["b_class"], g["clearance_mm"], str(g["rule"]))
        ),
        "net_pairs": {
            "total": total,
            "offset": offset,
            "returned": len(page),
            "all_listed": offset == 0 and len(page) == total,
            "next_offset": offset + len(page) if offset + len(page) < total else None,
            "items": page,
        },
        "notes": NOTES,
    }
