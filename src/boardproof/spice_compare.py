"""compare_spice_netlist: does a SPICE netlist connect the same pins as the schematic? (issue 195)

Read-only and topology only. It reads kicad-cli's netlist of the project (the same netlist path as
``sheet_tree.netlist_components``) and a SPICE netlist as text (an LTspice ``.net``/``.cir`` export;
no LTspice and no ngspice is run), maps simulation parts to schematic references, and compares the
two as partitions of pins: net and node NAMES differ between the tools (KiCad ``VCC``, LTspice
``N001``), so a consistent bijection between simulation nodes and schematic nets is searched and
every pin that breaks it is reported. It never says a circuit is right: models, values (unless asked),
parameters, sources and directives are not compared, and ``not_compared`` says so in every result.

Decisions (issue 195): one tool in the verification toolset; identity mapping by reference designator
(case-insensitive, as SPICE names are) unless ``mapping.names`` says otherwise; a part whose pin order
this tool cannot know (diodes, transistors, sources, subcircuits ...) is listed under ``uncompared_parts``
until ``mapping.pin_map`` gives the pin numbers, never guessed; ``topology_matches`` is true only when
nothing was left uncompared.
"""

from __future__ import annotations

import math
import re
import tempfile
from pathlib import Path
from typing import Any
from boardproof import write_layer

# Element letter -> number of nodes after the name (X: variable, K and others: not read).
_NODE_COUNT = {
    "R": 2, "C": 2, "L": 2, "D": 2, "V": 2, "I": 2, "B": 2, "W": 2, "F": 2, "H": 2,
    "E": 4, "G": 4, "Q": 3, "J": 3, "Z": 3, "M": 4, "S": 4, "T": 4,
}
# Elements whose SPICE pin order is the schematic's pin order 1, 2 for the standard passive symbols.
_POSITIONAL_DEFAULT = {"R", "C", "L"}
_GROUND = "0"
_NOT_READ_NOTE = {"Q": "a fourth (substrate) node is not read"}

NOT_COMPARED = (
    "device models and .model/.subckt definitions (only the connections of the instances are compared)",
    "the contents of .include and .lib files (elements in an included file are not read)",
    "lines after .end (element lines are listed, not read; other lines after .end are ignored)",
    "element parameters (m=, tc=, w/l, area, ic=, ...)",
    "source definitions and values (V, I, B sources, PULSE/SIN/PWL)",
    "analysis and other directives (.tran, .ac, .param, .meas, .step, ...)",
    "component values, unless compare_values is true (and then only R, C and L)",
    "KiCad simulation fields (Sim.Device, Sim.Pins, Sim.Params) and the exclude-from-simulation flag",
    "coupling statements (K), and the schematic's own power symbols and net names",
    "that the circuit works: topology only, never a verdict on the circuit",
)


class SpiceCompareError(Exception):
    """A refusal; ``code`` is machine-readable."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(message)
        self.code = code
        self.details = details


# --------------------------------------------------------------------------- SPICE side


def _logical_lines(text: str, skip: int = 0) -> list[tuple[int, str]]:
    """Source lines with '+' continuations joined and comments dropped: (first line number, text).
    ``skip`` is the number of the title line, which is not read."""
    out: list[tuple[int, str]] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or number == skip or line.startswith(("*", "$")):
            continue
        for marker in (";", " $"):
            cut = line.find(marker)
            if cut >= 0:
                line = line[:cut].rstrip()
        if not line:
            continue
        if line.startswith("+") and out:
            out[-1] = (out[-1][0], out[-1][1] + " " + line[1:].strip())
        else:
            out.append((number, line))
    return out


def _node(name: str) -> str:
    low = name.casefold()
    return _GROUND if low in ("0", "gnd") else low


def parse_spice(text: str) -> dict[str, Any]:
    """Parts and bookkeeping of a SPICE netlist (no analysis is run)."""
    if "\x00" in text:
        raise SpiceCompareError("invalid_netlist", "the SPICE netlist holds a NUL character: not a text netlist")
    title = None
    title_line = 0
    # SPICE: the first line is the title. A comment or directive first line (LTspice starts with a comment) is no title.
    for number, raw in enumerate(text.splitlines(), start=1):
        if raw.strip():
            if not raw.strip().startswith(("*", ".", ";")):
                title, title_line = raw.strip(), number
            break
    lines = _logical_lines(text, title_line)
    parts: dict[str, dict[str, Any]] = {}
    directives: list[str] = []
    subckts: list[str] = []
    skipped: list[dict[str, Any]] = []
    in_subckt = 0
    ended = False
    for number, line in lines:
        tokens = line.split()
        head = tokens[0]
        if ended:
            # a simulator stops reading at .end: nothing after it is part of the circuit, and it is listed, not compared
            if not head.startswith("."):
                skipped.append({"line": number, "name": head, "reason": "after .end: a simulator does not read it", "code": "content_after_end"})
            continue
        if head.startswith("."):
            word = head.casefold()
            if word == ".end" and in_subckt == 0:
                ended = True
                if len(tokens) > 1:  # a + continuation line was joined into the .end line
                    skipped.append({"line": number, "name": tokens[1], "reason": "continuation after .end: a simulator does not read it", "code": "content_after_end"})
            if word == ".subckt":
                in_subckt += 1
                if len(tokens) > 1:
                    subckts.append(tokens[1])
            elif word == ".ends":
                if in_subckt == 0:
                    raise SpiceCompareError("invalid_netlist", f".ends on line {number} closes no .subckt: the netlist is not read")
                in_subckt -= 1
            elif in_subckt == 0:
                directives.append(word)
            continue
        if in_subckt:
            continue  # elements inside a subcircuit definition are the model, not the circuit
        kind = head[0].upper()
        name = head
        if kind == "K":
            skipped.append({"line": number, "name": name, "reason": "coupling statement, not read"})
            continue
        if kind == "X":
            body = [t for t in tokens[1:] if not t.casefold().startswith("params:")]
            plain = [t for t in body if "=" not in t]
            if len(plain) < 2:
                skipped.append({"line": number, "name": name, "reason": "subcircuit instance without nodes and a subcircuit name"})
                continue
            nodes, model, rest = plain[:-1], plain[-1], [t for t in body if "=" in t]
        elif kind in _NODE_COUNT:
            count = _NODE_COUNT[kind]
            if len(tokens) < 1 + count:
                skipped.append({"line": number, "name": name, "reason": f"fewer than {count} nodes"})
                continue
            nodes, rest = tokens[1 : 1 + count], tokens[1 + count :]
            model = None
        else:
            skipped.append({"line": number, "name": name, "reason": f"element letter {head[0]!r} is not read"})
            continue
        key = name.casefold()
        if key in parts:
            raise SpiceCompareError("duplicate_sim_name", f"the simulation names {name} twice (names are case-insensitive)", name=name)
        value = None
        if kind in _POSITIONAL_DEFAULT and rest:
            value = rest[0].split("=", 1)[1] if rest[0].casefold().startswith(kind.casefold() + "=") else rest[0]
        parts[key] = {
            "name": name, "kind": kind, "nodes": [_node(n) for n in nodes], "raw_nodes": nodes,
            "model": model, "value": value, "line": number,
        }
    if in_subckt > 0:
        raise SpiceCompareError("invalid_netlist", f"{in_subckt} .subckt without .ends: every line after it would be hidden, so the netlist is not read")
    return {"title": title, "parts": parts, "directives": directives, "subckt_definitions": subckts, "skipped": skipped}


# --------------------------------------------------------------------------- KiCad side


def read_kicad_netlist(netlist_file: Path) -> dict[str, Any]:
    """Parts ``{ref: {value, lib_id}}`` and ``pin_net`` ``{(ref, pin): net name}`` of a kicadsexpr netlist, read raw."""

    root = write_layer.load(netlist_file)

    def text(node: Any, tag: str) -> str:
        found = node.find_children(tag)
        return (found[0].get_string(0) or "") if found else ""

    comps: dict[str, dict[str, str]] = {}
    for block in root.find_children("components"):
        for comp in block.find_children("comp"):
            lib = comp.find_children("libsource")
            lib_id = f"{text(lib[0], 'lib')}:{text(lib[0], 'part')}" if lib else ""
            comps[text(comp, "ref")] = {"value": text(comp, "value"), "lib_id": lib_id}
    pin_net: dict[tuple[str, str], str] = {}
    for block in root.find_children("nets"):
        for net in block.find_children("net"):
            name = text(net, "name")
            for node in net.find_children("node"):
                pin_net[(text(node, "ref"), text(node, "pin"))] = name
    return {"parts": comps, "pin_net": pin_net}


# --------------------------------------------------------------------------- values

_SI = {"t": 1e12, "g": 1e9, "k": 1e3, "m": 1e-3, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "n": 1e-9, "p": 1e-12, "f": 1e-15}
_NUM = re.compile(r"^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-zµμΩ]*)$", re.IGNORECASE)
_RKM = re.compile(r"^(\d+)([a-zµμ])(\d+)$", re.IGNORECASE)


def parse_value(text: str, *, spice: bool) -> float | None:
    """A number with an SI suffix as a float, or None when it is not one this reader is sure of.
    SPICE: f is femto, m and M are milli, meg is mega. Schematic text: a lone F is the unit farad, a lone M is
    ambiguous (None), MΩ is mega; units after a suffix are ignored; 4k7 style is read."""
    s = text.strip().replace(" ", "").replace(" ", "")
    if not s or s.startswith("{") or "=" in s:
        return None
    rkm = _RKM.match(s)
    if rkm and rkm.group(2).casefold() in set(_SI) | {"r"}:
        mult = 1.0 if rkm.group(2).casefold() == "r" else _SI[rkm.group(2).casefold()]
        return float(f"{rkm.group(1)}.{rkm.group(3)}") * mult
    match = _NUM.match(s)
    if not match:
        return None
    number, suffix = float(match.group(1)), match.group(2)
    low = suffix.casefold()
    if low == "":
        return number
    if low.startswith("meg"):
        return number * 1e6
    if low.startswith("mil"):
        return number * 25.4e-6
    if not spice:
        if suffix == "M":
            return None
        if suffix.startswith("M") and ("Ω" in suffix or "ohm" in low):
            return number * 1e6
        if low in ("f", "h", "v", "a", "r", "ω", "ohm", "ohms"):
            return number
    first = low[0]
    if first in _SI:
        return number * _SI[first]
    return number if all(ch in "rvahsz" for ch in low) or "ohm" in low or "Ω" in suffix else None


# --------------------------------------------------------------------------- comparison


def _validate_mapping(mapping: Any) -> tuple[dict[str, str], list[str], dict[str, dict[int, str | None]]]:
    if mapping is None:
        return {}, [], {}
    if not isinstance(mapping, dict):
        raise SpiceCompareError("invalid_spec", "mapping must be an object with the optional keys names, ignore, pin_map")
    unknown = sorted(set(mapping) - {"names", "ignore", "pin_map"})
    if unknown:
        raise SpiceCompareError("invalid_spec", f"unknown mapping key(s) {unknown}; allowed: names, ignore, pin_map")
    names = mapping.get("names", {})
    if not isinstance(names, dict) or not all(isinstance(k, str) and isinstance(v, str) and k and v for k, v in names.items()):
        raise SpiceCompareError("invalid_spec", "mapping.names must be {simulation name: schematic reference} with non-empty text")
    ignore = mapping.get("ignore", [])
    if not isinstance(ignore, list) or not all(isinstance(v, str) and v for v in ignore):
        raise SpiceCompareError("invalid_spec", "mapping.ignore must be a list of names (simulation names and/or schematic references)")
    raw_pins = mapping.get("pin_map", {})
    if not isinstance(raw_pins, dict):
        raise SpiceCompareError("invalid_spec", "mapping.pin_map must be {simulation name: {pin index (1-based): schematic pin number}}")
    pins: dict[str, dict[int, str | None]] = {}
    for sim, table in raw_pins.items():
        if not isinstance(sim, str) or not isinstance(table, dict) or not table:
            raise SpiceCompareError("invalid_spec", f"mapping.pin_map[{sim!r}] must be a non-empty object {{pin index: pin number or null}}")
        entry: dict[int, str | None] = {}
        for index, number in table.items():
            if isinstance(index, bool):
                raise SpiceCompareError("invalid_spec", f"pin_map[{sim!r}] has a bad pin index {index!r}")
            try:
                position = int(index)
            except (TypeError, ValueError):
                raise SpiceCompareError("invalid_spec", f"pin_map[{sim!r}] has a bad pin index {index!r} (1-based integers)") from None
            if position in entry or position < 1:
                raise SpiceCompareError("invalid_spec", f"pin_map[{sim!r}] has a bad or repeated pin index {index!r}")
            if number is not None and (isinstance(number, bool) or not isinstance(number, (str, int)) or str(number) == ""):
                raise SpiceCompareError("invalid_spec", f"pin_map[{sim!r}][{index!r}] must be a pin number (text) or null")
            entry[position] = None if number is None else str(number)
        pins[sim] = entry
    return dict(names), list(ignore), pins


def compare(
    kicad: dict[str, Any], spice: dict[str, Any], mapping: Any = None, compare_values: bool = False,
) -> dict[str, Any]:
    """The comparison itself, on parsed inputs (see the module docstring)."""
    if not isinstance(compare_values, bool):
        raise SpiceCompareError("invalid_spec", "compare_values must be true or false")
    names, ignore, pin_maps = _validate_mapping(mapping)
    sim_parts: dict[str, dict[str, Any]] = spice["parts"]
    k_parts: dict[str, dict[str, str]] = kicad["parts"]
    pin_net: dict[tuple[str, str], str] = kicad["pin_net"]

    # An entry for a part the simulation lacks is not a refusal (one table serves several revisions of a simulation):
    # it is listed, so a typo shows, and the part it was meant for is extra or uncompared anyway.
    mapping_unused = sorted(
        [f"names.{k}" for k in names if k.casefold() not in sim_parts] + [f"pin_map.{k}" for k in pin_maps if k.casefold() not in sim_parts])
    ref_by_fold: dict[str, list[str]] = {}
    for ref in k_parts:
        ref_by_fold.setdefault(ref.casefold(), []).append(ref)
    name_fold = {k.casefold(): v for k, v in names.items()}
    if len(name_fold) != len(names):
        raise SpiceCompareError("invalid_spec", "mapping.names repeats a simulation name (names are case-insensitive)")
    pin_fold = {k.casefold(): v for k, v in pin_maps.items()}
    ignore_fold = {i.casefold() for i in ignore}

    def refs_of(sim_key: str) -> list[str]:
        wanted = name_fold.get(sim_key, sim_parts[sim_key]["name"])
        found = k_parts.keys() & {wanted} or set(ref_by_fold.get(wanted.casefold(), []))
        return sorted(found)

    ignored: list[dict[str, str]] = []
    matched_ignore: set[str] = set()
    extra: list[dict[str, Any]] = []
    uncompared: list[dict[str, Any]] = []
    comparable: list[tuple[str, str]] = []  # (sim key, reference)
    used_refs: dict[str, str] = {}
    for key, part in sim_parts.items():
        if key in ignore_fold:
            matched_ignore.add(key)
            ignored.append({"name": part["name"], "side": "simulation"})
            continue
        refs = refs_of(key)
        if not refs:
            extra.append({"sim_name": part["name"], "kind": part["kind"], "nodes": part["raw_nodes"],
                          **({"mapped_to": name_fold[key]} if key in name_fold else {}),
                          "reason": "mapped reference is not in the schematic" if key in name_fold else "no schematic part with this reference"})
            continue
        if len(refs) > 1:
            raise SpiceCompareError("ambiguous_reference", f"{part['name']} matches several schematic references {refs}: use mapping.names", candidates=refs)
        ref = refs[0]
        if ref in used_refs:
            raise SpiceCompareError("invalid_spec", f"simulation parts {used_refs[ref]} and {part['name']} map to the same reference {ref}")
        used_refs[ref] = part["name"]
        if key not in pin_fold and part["kind"] not in _POSITIONAL_DEFAULT:
            uncompared.append({"sim_name": part["name"], "reference": ref, "reason": "no_pin_map",
                               "detail": f"a {part['kind']} element's pin order is not the schematic's pin numbering; give mapping.pin_map[{part['name']!r}]"})
            continue
        comparable.append((key, ref))

    missing = []
    for ref, info in sorted(k_parts.items()):
        if ref in used_refs:
            continue
        if ref.casefold() in ignore_fold:
            matched_ignore.add(ref.casefold())
            ignored.append({"name": ref, "side": "schematic"})
            continue
        missing.append({"reference": ref, "value": info["value"], "lib_id": info["lib_id"]})
    ignore_unmatched = sorted(i for i in ignore if i.casefold() not in matched_ignore)

    # pin pairs (sim node, kicad net) of every compared pin
    pairs: list[dict[str, Any]] = []
    uncompared_pins: list[dict[str, Any]] = []
    only_schematic: dict[str, list[str]] = {}
    for key, ref in comparable:
        part = sim_parts[key]
        table = pin_fold.get(key)
        covered: set[str] = set()
        for index, node in enumerate(part["nodes"], start=1):
            if table is None:
                pin = str(index)
            elif index in table:
                pin = table[index]
            else:
                uncompared_pins.append({"sim_name": part["name"], "reference": ref, "sim_pin": index, "reason": "pin not in mapping.pin_map"})
                continue
            if pin is None:
                continue  # deliberately mapped to nothing
            covered.add(pin)
            pairs.append({"sim": part["name"], "ref": ref, "sim_pin": index, "kicad_pin": pin, "node": node,
                          "raw_node": part["raw_nodes"][index - 1], "net": pin_net.get((ref, pin)), "default_pins": table is None})
        if table is not None:
            bad = sorted(i for i in table if i > len(part["nodes"]))
            if bad:
                raise SpiceCompareError("invalid_spec", f"pin_map[{part['name']!r}] has pin index {bad} but the element has {len(part['nodes'])} nodes")
        extra_pins = sorted({p for (r, p), net in pin_net.items() if r == ref and p not in covered and not net.startswith("unconnected-")})
        if extra_pins:
            only_schematic[ref] = extra_pins

    # votes -> a bijection between nodes and nets, then every pin that breaks it
    votes: dict[tuple[str, str], int] = {}
    for p in pairs:
        if p["net"] is not None:
            votes[(p["node"], p["net"])] = votes.get((p["node"], p["net"]), 0) + 1

    def weight(pair: tuple[str, str]) -> int:
        return votes[pair] * 4 + (1 if pair[0].casefold() == pair[1].casefold() else 0)

    ranked = sorted(votes, key=lambda pr: (-weight(pr), pr[0], pr[1]))
    node_to_net: dict[str, str] = {}
    net_to_node: dict[str, str] = {}
    ambiguous = False
    for pair in ranked:
        node, net = pair
        if node in node_to_net or net in net_to_node:
            continue
        node_to_net[node], net_to_node[net] = net, node
        if any(o != pair and (o[0] == node or o[1] == net) and weight(o) == weight(pair) for o in ranked):
            ambiguous = True

    per_part: dict[str, dict[str, Any]] = {}
    for key, ref in comparable:
        per_part[ref] = {"sim_name": sim_parts[key]["name"], "reference": ref, "connections_match": True, "pins": []}
    for p in pairs:
        entry = {"sim_pin": p["sim_pin"], "kicad_pin": p["kicad_pin"], "sim_node": p["raw_node"], "kicad_net": p["net"]}
        if p["net"] is None:
            # kicad-cli lists every pin of a part, an unconnected one in a net named unconnected-(...): a pin number
            # that is in no net does not exist on the part, so it never counts as a match
            entry["matches"], entry["problem"] = False, "kicad_pin_not_in_schematic"
        elif node_to_net.get(p["node"]) == p["net"]:
            entry["matches"] = True
        else:
            entry["matches"], entry["problem"] = False, "net_differs"
            entry["sim_node_corresponds_to_net"] = node_to_net.get(p["node"])
            entry["schematic_net_corresponds_to_sim_node"] = net_to_node.get(p["net"])
        if not entry["matches"]:
            per_part[p["ref"]]["connections_match"] = False
            if p["default_pins"] and entry.get("problem") == "net_differs":
                entry["hint"] = (
                    "pins taken in node order (1, 2); a part drawn the other way round in the simulation can show as a mismatch on this "
                    "part or on a neighbouring one (see node_map_ambiguous), a false alarm for a symmetric part: give a pin_map to say which pin is which")
        per_part[p["ref"]]["pins"].append(entry)
    for ref, rows in only_schematic.items():
        per_part[ref]["schematic_pins_not_in_simulation"] = rows

    value_differences: list[dict[str, Any]] = []
    values_not_compared: list[dict[str, str]] = []
    if compare_values:
        for key, ref in comparable:
            part = sim_parts[key]
            if part["kind"] not in _POSITIONAL_DEFAULT:
                values_not_compared.append({"sim_name": part["name"], "reason": f"{part['kind']} element: only R, C and L values are compared"})
                continue
            sim_text, kicad_text = part["value"] or "", k_parts[ref]["value"]
            a, b = parse_value(sim_text, spice=True), parse_value(kicad_text, spice=False)
            if a is None or b is None:
                values_not_compared.append({"sim_name": part["name"], "reason": f"value not readable as a number: simulation {sim_text!r}, schematic {kicad_text!r}"})
            elif not math.isclose(a, b, rel_tol=1e-6, abs_tol=0.0):
                value_differences.append({"sim_name": part["name"], "reference": ref, "simulation_value": sim_text, "schematic_value": kicad_text,
                                          "simulation_number": a, "schematic_number": b})

    parts_out = [per_part[ref] for _k, ref in comparable]
    mismatched = [p["reference"] for p in parts_out if not p["connections_match"]]
    unread = [{"sim_name": i["name"], "reference": None, "reason": i.get("code", "element_not_read"), "detail": i["reason"], "line": i["line"]} for i in spice["skipped"]]
    uncompared = uncompared + unread
    topology = len(parts_out) > 0 and not (mismatched or missing or extra or uncompared or uncompared_pins or only_schematic)
    ground_net = node_to_net.get(_GROUND)
    node_map = [{"sim_node": n, "kicad_net": node_to_net[n]} for n in sorted(node_to_net)]
    result: dict[str, Any] = {
        "status": "ok",
        "topology_matches": topology,
        "topology_note": "true only when at least one part was compared, every element line before .end was read and compared (element lines after .end are listed, not read; other lines after .end are ignored), nothing is missing, extra or differently connected, and no connected schematic pin of a compared part is absent from the simulation; ignored parts also drop their pins from net membership; it is not a verdict on the circuit",
        "parts_compared": len(parts_out),
        "mismatched_parts": mismatched,
        "parts": parts_out,
        "missing_parts": missing,
        "extra_parts": extra,
        "uncompared_parts": uncompared,
        "uncompared_pins": uncompared_pins,
        "ignored": ignored,
        "ignore_unmatched": ignore_unmatched,
        "mapping_unused": mapping_unused,
        "node_map": node_map,
        "ground_net": ground_net,
        "node_map_ambiguous": ambiguous,
        "values_compared": compare_values,
        "not_compared": list(NOT_COMPARED),
        "simulation": {
            "title_line": spice["title"],
            "elements_read": len(sim_parts),
            "directives": len(spice["directives"]),
            "subckt_definitions": list(spice["subckt_definitions"]),
            "skipped_lines": spice["skipped"],
        },
    }
    if not parts_out:
        result["nothing_compared"] = "no part was compared (the simulation or the schematic is empty, or everything was ignored or uncompared), so topology_matches is false"
    first_tokens = (spice["title"] or "").split()
    if len(first_tokens) >= 3 and first_tokens[0][0].upper() in set(_NODE_COUNT) | {"X"}:
        result["title_note"] = "the first line is read as the SPICE title, not as an element, although it looks like one; a part on it is missing from the simulation side; if this line is a title, ignore this note"
    if only_schematic:
        result["schematic_pins_not_in_simulation"] = {r: pins for r, pins in sorted(only_schematic.items())}
    if ambiguous:
        result["node_map_note"] = (
            "two or more node-to-net pairings had equal support, so a mismatch may be attributed to another part than the one "
            "that differs; read the whole pin list")
    if compare_values:
        result["value_differences"] = value_differences
        result["values_not_compared"] = values_not_compared
    return result


# --------------------------------------------------------------------------- entry point


def compare_spice_netlist(
    sch_path: str, spice_netlist: str, root_sch: str | None = None, mapping: Any = None, compare_values: bool = False,
) -> dict[str, Any]:
    """Read-only: ``kicad-cli``'s netlist of the project against a SPICE netlist text file (see the module docstring)."""
    from boardproof import kicad_cli, sheet_authoring, sheet_tree
    from boardproof.sheet_authoring import SheetAuthoringError

    try:
        file = Path(sch_path)
        if not file.is_file():
            raise SheetAuthoringError("file_not_found", f"sheet not found: {file}")
        sim_file = Path(spice_netlist)
        if sim_file.suffix.casefold() == ".asc":
            raise SpiceCompareError(
                "unsupported_input", "an LTspice .asc schematic is not read: export its netlist from LTspice (View > SPICE Netlist) and pass that .net or .cir file")
        if not sim_file.is_file():
            raise SpiceCompareError("file_not_found", f"SPICE netlist not found: {sim_file}")
        try:
            text = sim_file.read_text(encoding="utf-8-sig")
        except UnicodeDecodeError:
            try:
                text = sim_file.read_text(encoding="utf-16")  # LTspice can write UTF-16
            except UnicodeError:
                raise SpiceCompareError("invalid_netlist", "the SPICE netlist is neither UTF-8 nor UTF-16 text") from None
        _validate_mapping(mapping)
        spice = parse_spice(text)
        root_file = sheet_authoring.find_root(file, root_sch)
        if not root_file.is_file():
            raise SheetAuthoringError("root_not_found", f"root sheet not found: {root_file}")
        tree = sheet_tree.walk(root_file)
        if not tree.files or tree.problems:
            raise SheetAuthoringError(
                "sheet_tree_incomplete", "the project's sheet tree does not load whole, so the netlist cannot stand for it: " + "; ".join(tree.problems),
                **tree.report())
        with tempfile.TemporaryDirectory(prefix="boardproof-spice-compare-") as tmp:
            try:
                out = kicad_cli.export_netlist(root_file, Path(tmp) / "netlist.net")
            except kicad_cli.KicadCliError as exc:
                raise SpiceCompareError("netlist_failed", f"kicad-cli could not export the netlist: {exc}") from None
            kicad = read_kicad_netlist(out)
        problems = sheet_tree.netlist_disagreement(tree, kicad["parts"])
        if problems:
            raise SpiceCompareError("netlist_not_trustworthy", "kicad-cli's netlist cannot stand for the tree: " + "; ".join(problems))
        return compare(kicad, spice, mapping, compare_values)
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)
    except SpiceCompareError as exc:
        return {"status": "incomplete", "ok": False, "code": exc.code, "reason": str(exc), **exc.details}
