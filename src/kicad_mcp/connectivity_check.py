"""What ``check_connectivity`` reports, read from the raw sheet (GitHub issue 109).

``kicad_tools``' ``analyze_schematic`` builds a wire graph and nothing else. Measured on a sheet that
kicad-cli 10.0.7 accepts with 0 ERC violations and a clean netlist, it gave these false findings:

* a pin with a no-connect marker on it (or an electrical type ``no_connect``) was an unconnected pin;
* a pin that touches another symbol's pin, a label or a sheet pin directly, without a wire, was an
  unconnected pin;
* a wire end that lands on a symbol pin or a sheet pin was a ``possible_floating_wire``;
* an invisible ``power_in`` pin of an ordinary symbol, which KiCad joins to the net of its pin name
  without a wire, was an unconnected pin (issue 118);
* a symbol of a project library whose lib_id is not ``power:...`` but whose library symbol is a power
  symbol (``(power)``), such as the ground and supply symbols and ``PWR_FLAG`` of a project library,
  was ``missing_from_netlist`` although kicad-cli never lists power symbols in a netlist.

It also missed one real defect: a pin that only lies on the middle of a wire (no wire end, no junction)
counted as connected, but joins nothing (measured for issue 86).

The functions here read the raw nodes only (``sheet_editing.pin_positions`` and the ``(at ...)`` of
the sheet's items), never ``kicad_tools``' position properties. Two points match when they lie within
``sheet_editing.TOL`` (1 micrometre) of each other, because KiCad connects by exact internal units.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from kicad_tools.sexp import SExp

from kicad_mcp import sheet_editing as se

FLOATING_NOTE = (
    "no symbol pin, sheet pin, label, junction, no-connect marker or second wire end lies on this wire "
    "end, so it joins nothing"
)
BODY_NOTE = (
    "this wire end lies on the middle of another wire without a junction; KiCad joins nothing there "
    "(measured with kicad-cli 10.0.7), so add a junction (add_junctions) or end that wire here"
)


def _placed(root: SExp) -> list[SExp]:
    return [s for s in se._kids(root, "symbol") if s.find_children("lib_id")]


def is_power(root: SExp, symbol: SExp) -> bool:
    """True when the embedded library symbol of ``symbol`` carries the ``(power)`` flag."""
    lib = se._lib_symbol(root, symbol)
    return lib is not None and bool(lib.find_children("power"))


def _local_power(root: SExp, symbol: SExp) -> bool:
    """True when the library symbol is a ``(power local)`` symbol, which joins nets of its own sheet only."""
    lib = se._lib_symbol(root, symbol)
    flags = lib.find_children("power") if lib is not None else []
    return bool(flags) and any(c.is_atom and str(c.value) == "local" for c in flags[0].children)


def _dnp(symbol: SExp) -> bool:
    found = symbol.find_children("dnp")
    return bool(found) and bool(found[0].children) and str(found[0].children[0].value) == "yes"


def _value(symbol: SExp) -> str:
    prop = se._property(symbol, "Value")
    return (prop.get_string(1) or "") if prop is not None else ""


def _any(point: tuple[float, float], points: list) -> bool:
    return any(se._same(point, p) for p in points)


def _hidden_power_in(pin: dict) -> bool:
    """True for an invisible ``power_in`` pin with a name KiCad can use as a net name."""
    return pin.get("type") == "power_in" and bool(pin.get("hidden")) and pin["name"] not in ("", "~")


def _labels(root: SExp) -> list[SExp]:
    return se.Sheet(root).labels


class Points:
    """Everything that can sit on a point of one sheet, read from the raw nodes."""

    def __init__(self, root: SExp) -> None:
        self.root = root
        sheet = se.Sheet(root)
        self.wires: list[tuple[tuple[float, float], tuple[float, float]]] = []
        for wire in sheet.wires:
            ends = se._wire_ends(wire)
            if len(ends) >= 2:
                self.wires.append((ends[0], ends[-1]))
        self.wire_ends = [end for pair in self.wires for end in pair]
        self.pins: list[tuple[str, str, float, float]] = []  # (reference, number, x, y)
        for symbol in _placed(root):
            for pin in se.pin_positions(root, symbol):
                self.pins.append((se._ref(symbol), pin["number"], pin["x"], pin["y"]))
        self.labels = [se._at(n)[:2] for n in sheet.labels]
        self.junctions = [se._at(n)[:2] for n in sheet.junctions]
        self.no_connects = [se._at(n)[:2] for n in sheet.no_connects]
        self.sheet_pins = [se._at(p)[:2] for s in sheet.sheets for p in se._kids(s, "pin")]

    def pin_attached(self, point: tuple[float, float], reference: str, number: str) -> bool:
        """True when a wire end, another pin, a label, a sheet pin or a junction with a wire passing over it lies on ``point``."""
        if any(_any(point, group) for group in (self.wire_ends, self.labels, self.sheet_pins)):
            return True
        if _any(point, self.junctions) and self.on_wire_body(point):
            return True  # a junction joins a wire passing over the pin; alone it joins nothing (ERC: pin_not_connected)
        return any(se._same(point, (x, y)) for r, n, x, y in self.pins if (r, n) != (reference, number))

    def wire_end_attached(self, point: tuple[float, float], own: tuple[int, int]) -> bool:
        """True when a pin, a sheet pin, a label, a junction, a no-connect marker or the end of a
        second wire lies on ``point``. ``own`` is (wire index, end index) of the wire end under test,
        which does not count."""
        if any(se._same(point, (x, y)) for _r, _n, x, y in self.pins):
            return True
        if any(_any(point, group) for group in (self.sheet_pins, self.labels, self.junctions, self.no_connects)):
            return True
        return any(
            (i, j) != own and se._same(point, end) for i, pair in enumerate(self.wires) for j, end in enumerate(pair)
        )

    def global_names(self, sheet_name: str) -> Counter:
        """How many members each global power net name has on this sheet (issue 118).

        KiCad joins an invisible ``power_in`` pin of an ordinary symbol to the net of the same name,
        without a wire (``SCH_PIN::IsGlobalPower`` and ``CONNECTION_GRAPH::generateGlobalPowerPinSubGraphs``
        in eeschema/sch_pin.cpp and eeschema/connection_graph.cpp). Members, counted on one sheet because
        kicad-cli 10.0.7 ERC counts one sheet at a time: such a pin (its shown pin name is the net name),
        wired or not, a power symbol of that Value whose pin has something on it, and a label of that
        text on the sheet, plain or global (a global label on another sheet is no member).
        Two choices not measured: a pin with a no-connect marker is no member, and a ``(power local)``
        symbol is no member."""
        names: Counter = Counter()
        for symbol in _placed(self.root):
            power = is_power(self.root, symbol)
            if power and _local_power(self.root, symbol):
                continue
            reference = se._ref(symbol)
            for pin in se.pin_positions(self.root, symbol, with_type=True, with_hidden=True):
                point = (pin["x"], pin["y"])
                if power:
                    name = _value(symbol)
                    if name and self.pin_attached(point, reference, pin["number"]):
                        names[name] += 1
                elif _hidden_power_in(pin):
                    if not _any(point, self.no_connects):
                        names[pin["name"]] += 1
        for node in _labels(self.root):
            if node.name in ("global_label", "label"):
                text = node.get_string(0) or ""
                if text:
                    names[text] += 1
        return names

    def on_wire_body(self, point: tuple[float, float]) -> bool:
        """True when ``point`` lies on a wire without being one of its ends."""
        return any(
            not se._same(point, a) and not se._same(point, b) and se._segment_contains(a, b, point)
            for a, b in self.wires
        )


def unconnected_pins(root: SExp, sheet_name: str, fallback: list[Any]) -> tuple[list[dict], list[dict]]:
    """The unconnected pins of one sheet and the pins left out because they are marked no-connect.

    Skips what ``analyze_schematic`` skips by default: ``power:`` symbols and symbols marked do not
    place. A pin counts as connected when a wire end, another symbol's pin, a label, a sheet pin or a
    junction with a wire passing over it lies on it, or when it is an invisible ``power_in`` pin whose
    global power net name has another member on the same sheet (``Points.global_names``, issue 118;
    kicad-cli's ERC counts the members of the sheet only, even though the netlist joins the sheets). It is left out of the findings (and listed apart, with ``via``) when a
    no-connect marker lies on it, or its electrical type is ``no_connect`` and nothing is attached
    (KiCad's ERC accepts both). A symbol whose library symbol is not embedded in the sheet cannot be
    read here, so the ``kicad_tools`` records for it (``fallback``) stand."""
    points = Points(root)
    global_names = points.global_names(sheet_name)
    found: list[dict] = []
    marked: list[dict] = []
    for symbol in _placed(root):
        lib_id = symbol.find_children("lib_id")[0].get_string(0) or ""
        if lib_id.startswith("power:") or _dnp(symbol):
            continue
        reference = se._ref(symbol)
        if se._lib_symbol(root, symbol) is None:
            found += [_as_dict(r, se._at(symbol)[:2]) for r in fallback if r.reference == reference and r.sheet == sheet_name]
            continue
        for pin in se.pin_positions(root, symbol, with_type=True, with_hidden=True):
            point = (pin["x"], pin["y"])
            record = {
                "reference": reference, "pin_number": pin["number"], "pin_name": pin["name"],
                "pin_type": pin["type"], "symbol_value": _value(symbol), "lib_id": lib_id,
                "position": [pin["x"], pin["y"]], "sheet": sheet_name,
            }
            if _any(point, points.no_connects):
                marked.append({**record, "via": "no_connect_marker"})
            elif points.pin_attached(point, reference, pin["number"]):
                continue
            elif _hidden_power_in(pin) and global_names[pin["name"]] >= 2:
                continue  # joined by name to another member of its global power net (issue 118)
            elif pin["type"] == "no_connect":
                marked.append({**record, "via": "pin_type_no_connect"})
            else:
                found.append(record)
    return found, marked


def _as_dict(record: Any, position: tuple[float, float]) -> dict:
    return {
        "reference": record.reference, "pin_number": record.pin_number, "pin_name": record.pin_name,
        "pin_type": record.pin_type, "symbol_value": record.symbol_value, "lib_id": record.lib_id,
        "position": list(position), "sheet": record.sheet,
    }


def refine_issues(root: SExp, issues: list[Any], sheet_name: str) -> list[Any]:
    """The issues of one sheet with the ``possible_floating_wire`` ones read again from the raw wires:
    one for every wire end that has no pin, sheet pin, label, junction, no-connect marker or second wire
    end on it, with a text that says why it is a finding. Other issue types (stacked symbols) stand."""
    from kicad_tools.cli.sch_find_unconnected import ConnectionIssue

    points = Points(root)
    kept = [i for i in issues if i.type != "possible_floating_wire"]
    for index, pair in enumerate(points.wires):
        for end_index, point in enumerate(pair):
            if points.wire_end_attached(point, (index, end_index)):
                continue
            note = BODY_NOTE if points.on_wire_body(point) else FLOATING_NOTE
            kept.append(
                ConnectionIssue(
                    type="possible_floating_wire",
                    description=f"Wire endpoint is not connected: {note}",
                    position=point,
                    sheet=sheet_name,
                )
            )
    return kept


def power_symbol_references(root: SExp) -> set[str]:
    """References of the placed symbols whose library symbol is a power symbol; kicad-cli never
    lists them in a netlist."""
    return {se._ref(s) for s in _placed(root) if is_power(root, s)}
