"""No-connect markers by pin (GitHub issue 113).

``add_labels`` could place a no-connect marker by point only, and nothing deleted one. This module
holds ``add_no_connects``: a batch of selectors (symbol references or uuids, plus the pin numbers
or ``"unconnected"``) that puts a marker on each chosen pin point. The delete side is
``sheet_editing.delete("no_connect", ...)`` (selectors ``uuids``, ``region``, ``points`` and
``references`` plus ``pins``).

Decisions (docs/ARCHITECTURE.md decision 4, the same pipeline as the other sheet-editing tools):

- Pin points are the ``sheet_editing.pin_positions`` maths on the raw ``(at ...)`` nodes. Nothing
  is read from ``kicad_tools``' convenience properties.
- What counts as unconnected comes from kicad-cli's netlist of the project, not from geometry: a
  pin is open when it is the only node of an automatically named ``unconnected-(..)`` net. A wire
  that runs over a pin without ending there joins nothing (measured for issue 86), so such a pin
  is open and gets its marker; a pin with a wire stub or a label is not open and is refused as
  ``pin_connected`` when it was named explicitly.
- ``"unconnected"`` (also the default when no selector is given, which means every placed symbol
  of this sheet) leaves out hidden pins (a hidden power pin is connected by name, ERC does not
  report it), pins of the electrical type ``no_connect`` (the library already says so) and the
  power symbols and flags whose reference starts with ``#``. An explicitly named hidden pin is
  refused as ``pin_hidden``.
- A pin that already has a marker on its point is skipped and listed.
- Self-check before the write: kicad-cli's ERC of the project, from scratch copies, must show
  exactly as many fewer ``pin_not_connected`` as pins were marked (pins of the type
  ``no_connect`` excluded) and no violation type may grow; otherwise ``write_not_verified``
  and nothing is written. The netlist comparison of ``sheet_editing.run`` holds as for every
  sheet edit: a marker changes no net.
"""

from __future__ import annotations

import fnmatch
from collections import Counter
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import sheet_authoring, sheet_editing as se, write_layer
from boardproof.sheet_authoring import SheetAuthoringError

_SELECTOR_KEYS = {"references", "uuids", "pins"}
_SILENCING_TYPES = {"power_in", "input"}


def marker_node(x: float, y: float) -> SExp:
    """A no-connect marker as KiCad writes one: ``(no_connect (at x y) (uuid ..))``."""
    return SExp.list("no_connect", SExp.list("at", write_layer.number(se._r(x)), write_layer.number(se._r(y))), sheet_authoring._uuid())


def _is_open(net_name: str, nodes: frozenset) -> bool:
    return len(nodes) == 1 and (net_name.startswith("unconnected-(") or "/unconnected-(" in net_name)


def _parse(selectors: Any) -> list[dict[str, Any]]:
    if selectors is None:
        return [{"references": ["*"], "pins": "unconnected", "_implicit": True}]
    if not isinstance(selectors, list) or not selectors:
        raise SheetAuthoringError("invalid_spec", "selectors must be a non-empty list of objects, or omitted for every symbol of the sheet")
    out = []
    for i, sel in enumerate(selectors):
        if not isinstance(sel, dict) or not sel or set(sel) - _SELECTOR_KEYS or not (sel.get("references") or sel.get("uuids")):
            raise SheetAuthoringError(
                "invalid_spec", f"selectors[{i}] must be an object with references and/or uuids (non-empty lists) and optionally pins, keys from {sorted(_SELECTOR_KEYS)}, got {sel!r}")
        for key in ("references", "uuids"):
            if key in sel and not (isinstance(sel[key], list) and sel[key] and all(isinstance(v, str) and v for v in sel[key])):
                raise SheetAuthoringError("invalid_spec", f"selectors[{i}].{key} must be a non-empty list of non-empty strings")
        pins = sel.get("pins", "unconnected")
        if pins != "unconnected" and not (isinstance(pins, list) and pins and all(isinstance(v, str) and v for v in pins)):
            raise SheetAuthoringError("invalid_spec", f"selectors[{i}].pins must be a non-empty list of pin numbers (strings) or \"unconnected\"")
        out.append({**sel, "pins": pins})
    return out


def add_no_connects(path: str, selectors: Any, root_sch: str | None, *, allow_connectivity_change: bool, dry_run: bool) -> dict[str, Any]:
    """Put a no-connect marker on each pin the ``selectors`` pick (see the tool text)."""

    def mutate(sheet: se.Sheet, root: SExp) -> se.Outcome:
        parsed = _parse(selectors)
        placed = [s for s in sheet.symbols if s.find_children("lib_id")]
        net_of = {node: (name, nodes) for name, nodes in sheet.nets_before().items() for node in nodes}
        marked = [se._at(n)[:2] for n in sheet.no_connects]
        chosen: dict[tuple[str, str], dict[str, Any]] = {}
        missing: list[str] = []
        connected: list[str] = []
        hidden: list[str] = []
        power: list[str] = []
        left_connected = 0
        for i, sel in enumerate(parsed):
            nodes = [s for s in placed if se._uuid(s) in sel.get("uuids", []) or any(fnmatch.fnmatchcase(se._ref(s), r) for r in sel.get("references", []))]
            if not sel.get("_implicit"):
                missing += [f"reference {r!r}" for r in sel.get("references", []) if not any(fnmatch.fnmatchcase(se._ref(s), r) for s in placed)]
                missing += [f"uuid {u!r}" for u in sel.get("uuids", []) if not any(se._uuid(s) == u for s in placed)]
            seen_numbers: set[str] = set()
            for node in nodes:
                ref = se._ref(node)
                for pin in se.pin_positions(root, node, with_type=True, with_hidden=True):
                    number = pin["number"]
                    if sel["pins"] == "unconnected":
                        if pin["type"] == "no_connect" or ref.startswith("#"):  # hidden pins are not skipped here: KiCad ties hidden pins of one name into a shared net, which the netlist test below does not list as open
                            continue
                    elif number not in sel["pins"]:
                        continue
                    seen_numbers.add(number)
                    if (ref, number) in chosen:
                        continue
                    explicit = sel["pins"] != "unconnected"
                    if explicit and ref.startswith("#"):
                        power.append(f"{ref}.{number}")
                        continue
                    if explicit and pin["hidden"]:
                        hidden.append(f"{ref}.{number}")
                        continue
                    if (ref, number) not in net_of:
                        raise SheetAuthoringError(
                            "pin_not_in_netlist", f"selectors[{i}]: pin {ref}.{number} is not in kicad-cli's netlist of the project (an unannotated reference, or a sheet used more than once "
                            "whose per-use references differ from the sheet's own Reference property); whether it is open could not be checked, so no marker was added")
                    point = (pin["x"], pin["y"])
                    entry = {"reference": ref, "pin": number, "name": pin["name"], "type": pin["type"], "at": se._round_pt(*point), "point": point}
                    if any(se._same(point, m) for m in marked):
                        entry["reason"] = "a no-connect marker already sits on this pin"
                        chosen[(ref, number)] = {**entry, "skip": True}
                        continue
                    name, net_nodes = net_of[(ref, number)]
                    if not _is_open(name, net_nodes):
                        if explicit:
                            connected.append(f"{ref}.{number} (net {name!r}, {len(net_nodes)} pin(s))")
                        else:
                            left_connected += 1
                        continue
                    chosen[(ref, number)] = entry
            if sel["pins"] != "unconnected":
                missing += [f"pin {n!r} of selectors[{i}]" for n in sel["pins"] if n not in seen_numbers]
        if missing:
            raise SheetAuthoringError("not_found", f"not found on this sheet: {', '.join(missing)}; nothing was written", not_found=missing)
        if power:
            raise SheetAuthoringError(
                "power_symbol_pin", f"a power symbol's pin is its net, not an open pin: {', '.join(power)}; nothing was written. Wire it, or delete the symbol.", pins=power)
        if connected:
            raise SheetAuthoringError(
                "pin_connected", f"a no-connect marker belongs on an open pin; these are wired, labelled or tied to another pin (including a hidden pin of the same name): {', '.join(connected)}; nothing was written. "
                "Delete the wire or label first if the pin is meant to stay open.", pins=connected)
        if hidden:
            raise SheetAuthoringError("pin_hidden", f"these pins are hidden in the library symbol and need no marker: {', '.join(hidden)}; nothing was written", pins=hidden)

        added: list[dict[str, Any]] = []
        skipped = []
        points: list[tuple[tuple[float, float], str]] = []
        outcome = se.Outcome()
        for entry in chosen.values():
            point = entry.pop("point")
            if entry.pop("skip", False):
                skipped.append(entry)
                continue
            twin = next((u for p, u in points if se._same(point, p)), None)
            if twin is not None:  # two pins on one point: one marker serves both
                entry["uuid"] = twin
                entry["marker"] = "shared with another pin on the same point"
                added.append(entry)
                continue
            node = marker_node(*point)
            root.children.append(node)
            outcome.added.add(se._uuid(node))
            points.append((point, se._uuid(node)))
            entry["uuid"] = se._uuid(node)
            added.append(entry)
        listed = added
        silenced = [f"{e['reference']}.{e['pin']} ({e['type']})" for e in added if e["type"] in _SILENCING_TYPES]
        expected_drop = sum(1 for e in added if e["type"] != "no_connect")
        outcome.no_change = not added
        outcome.check_nets = bool(added)
        outcome.post_check = _erc_check(sheet, root, expected_drop) if added else None
        outcome.report = {
            "added": listed, "count": len(listed), "already_marked": skipped,
            "silenced_other_erc": {"count": len(silenced), "pins": silenced,
                                   "note": "a marker also silences the ERC types pin_not_driven (input pins) and power_pin_not_driven (power input pins) for that pin; check that these pins are meant to stay open"},
            "connected_pins_left_alone": left_connected,
            "note": ("a marker was added only on open pins (kicad-cli's netlist: the only pin on an unconnected-(..) net, so not wired, labelled or tied to another pin, "
                     "including a hidden pin of the same name); pins of the type no_connect and power symbols (#-references) are left out of \"unconnected\""),
        }
        if not added:
            outcome.report["note_nothing"] = "no open pin without a marker was found; nothing was written"
        return outcome

    result = se.run(path, root_sch, mutate, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)
    if result.get("ok") and dry_run:
        result["would_add"] = result.pop("added")
    return result


def _erc_check(sheet: se.Sheet, new: SExp, expected_drop: int):
    """The self-check: ERC reports ``expected_drop`` fewer ``pin_not_connected`` and no type grew."""

    def check(_before: dict, _after: dict) -> None:
        before: Counter = sheet.erc_for(None)
        after: Counter = sheet.erc_for(new)

        def of(census: Counter, kind: str) -> int:
            return sum(n for (t, _s), n in census.items() if t == kind)

        drop = of(before, "pin_not_connected") - of(after, "pin_not_connected")
        grown = {t: of(after, t) - of(before, t) for t in {t for (t, _s) in after} if of(after, t) > of(before, t)}
        if drop != expected_drop or grown:
            raise SheetAuthoringError(
                "write_not_verified",
                f"check erc_pin_not_connected: kicad-cli's ERC of the edited sheet shows {drop} fewer pin_not_connected, {expected_drop} were expected, and "
                f"violation types that grew: {grown or 'none'}; nothing was written",
                check="erc_pin_not_connected", expected_drop=expected_drop, erc_drop=drop, erc_grown=grown)

    return check
