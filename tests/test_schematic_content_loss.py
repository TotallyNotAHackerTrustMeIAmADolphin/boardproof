"""The schematic tools must not delete content they do not model (build ticket 38).

kicad_tools 0.20.0's high-level ``Schematic`` builder rewrote a loaded sheet from its model and
dropped what the model does not hold (buses, bus entries, images, sub-sheet blocks, ``Description``
fields). 0.24.0 keeps the source of a sheet whose ``(version N)`` is at least its own format
(``KICAD_SCH_FORMAT_VERSION``, 20231120), but still regenerates an older sheet wholesale
(``io_mixin.load``, deliberately: kicad-tools #6057). The four builder tools (``add_symbols``,
``add_wires``, ``add_junctions``, ``add_labels``) refuse such a sheet before anything is written.

Every test works on a copy of a KiCad demo (``kicad_demo``, never vendored) and grades with
``kicad-cli`` output or raw S-expression tokens, never with a tool's return value alone. KiCad
files are written only through the tools under test (CLAUDE.md hard rule 1).
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path

import pytest
from kicad_tools.sexp import parse_file
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli
from kicad_mcp import write_layer as wl

# vme-wren's front-panel-io sheet is the one KiCad 7 file among KiCad 10.0's demos (format
# 20230819; the vme-wren root does not use it). Counted from its raw tokens, 2026-10-21.
OLD_DEMO, OLD_SHEET, OLD_VERSION = "vme-wren", "front-panel-io.kicad_sch", "20230819"
OLD_BUSES, OLD_BUS_ENTRIES, OLD_TEXT_BOXES = 34, 32, 1

HARMLESS = {
    "add_symbols": [{"lib_id": "Device:R", "x": 12.7, "y": 12.7, "ref": "R9038", "value": "1k"}],
    "add_wires": [{"p1": [12.7, 12.7], "p2": [17.78, 12.7], "route": "direct"}],
    "add_junctions": [[12.7, 12.7]],
    "add_labels": [{"kind": "label", "text": "T38", "x": 12.7, "y": 12.7}],
}
BUILDER_TOOLS = sorted(HARMLESS)


@pytest.fixture
def sch_tools() -> dict:
    from kicad_mcp.toolsets import schematic

    mcp = MCPServer("test")
    schematic.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


def _top_level(path: Path, name: str) -> int:
    return sum(1 for c in wl.load(path).children if not c.is_atom and c.name == name)


@pytest.mark.parametrize("tool", BUILDER_TOOLS)
def test_builder_tool_refuses_a_sheet_older_than_kicad_tools_format(tool: str, sch_tools: dict, kicad_demo) -> None:
    sheet = kicad_demo(OLD_DEMO) / OLD_SHEET
    assert str(wl.load(sheet).find("version").children[0].value) == OLD_VERSION
    assert (_top_level(sheet, "bus"), _top_level(sheet, "bus_entry")) == (OLD_BUSES, OLD_BUS_ENTRIES)
    before = sheet.read_bytes()

    result = sch_tools[tool](str(sheet), HARMLESS[tool])

    assert result["status"] != "ok"
    assert result["error"] == "old_sheet_format"
    assert result["version"] == int(OLD_VERSION)
    drop = result["would_drop"]
    assert (drop["bus"], drop["bus_entry"], drop["text_box"]) == (OLD_BUSES, OLD_BUS_ENTRIES, OLD_TEXT_BOXES)
    assert OLD_VERSION in result["reason"] and f"{OLD_BUSES} bus" in result["reason"]
    assert "nothing was written" in result["reason"]
    assert sheet.read_bytes() == before
    assert sorted(p.name for p in sheet.parent.iterdir() if p.name.startswith(sheet.name)) == [sheet.name]


def test_kicad_tools_still_regenerates_a_sheet_older_than_its_format(kicad_demo) -> None:
    """The fact the refusal rests on. When this fails, kicad_tools keeps old sheets' source and
    ``_old_format_refusal`` can go (recheck with the reproduction in build ticket 38)."""
    from kicad_tools.core.version import KICAD_SCH_FORMAT_VERSION
    from kicad_tools.schematic.models.schematic import Schematic
    from kicad_tools.sexp import parse_string

    sheet = kicad_demo(OLD_DEMO) / OLD_SHEET
    assert int(OLD_VERSION) < KICAD_SCH_FORMAT_VERSION == 20231120
    regenerated = parse_string(Schematic.load(sheet).to_sexp())

    assert str(regenerated.find("version").children[0].value) == str(KICAD_SCH_FORMAT_VERSION)
    names = [c.name for c in regenerated.children if not c.is_atom]
    assert (names.count("bus"), names.count("bus_entry"), names.count("text_box")) == (0, 0, 0)


# --------------------------------------------------------------------------- regression

# KiCad 10.0's demos, counted with kicad-cli 10.0 and from raw tokens, 2026-10-21 (ticket 38's
# reproduction table). Each demo carries the classes the 0.20.0 builder used to delete.
DEMOS = {
    "complex_hierarchy": {
        "root": "complex_hierarchy.kicad_sch",
        "netlist": (68, 52),
        "census": {"sheet": 2, "Description": 96, "version": ("20250114", "20250114")},
        "footprint": {"C104": "Capacitor_SMD:C_0805_2012Metric"},
    },
    "interf_u": {
        "root": "interf_u.kicad_sch",
        "netlist": (24, 173),
        "census": {"bus": 38, "bus_entry": 36, "image": 1, "Description": 70, "version": ("20250114",)},
        "footprint": {"C1": "Capacitor_SMD:C_0805_2012Metric"},
    },
}
CONTENT = ("sheet", "bus", "bus_entry", "image", "polyline", "text_box", "rectangle", "text")
# What each harmless call adds: a resistor with two unconnected pins is one component and two
# nets; a wire, junction or label away from every pin joins no net.
ADDED = {
    "add_symbols": {"symbol": 1, "Description": 1, "components": 1, "nets": 2},
    "add_wires": {"wire": 1},
    "add_junctions": {"junction": 1},
    "add_labels": {"label": 1},
    "assign_footprints": {},
    "fix_annotation": {},
}


def _netlist(root: Path) -> tuple[int, int]:
    """Components and nets of ``kicad-cli sch export netlist`` of ``root``."""
    with tempfile.TemporaryDirectory() as tmp:
        parsed = parse_file(str(kicad_cli.export_netlist(root, Path(tmp) / "census.net")))
    comps = sum(len(block.find_children("comp")) for block in parsed.find_children("components"))
    nets = sum(len(block.find_children("net")) for block in parsed.find_children("nets"))
    return comps, nets


def _census(project: Path, root: str) -> dict:
    """Raw-token counts over every sheet of ``project`` (top-level classes, ``Description``
    fields at any depth, each sheet's version) and the root's kicad-cli netlist counts."""
    counts: Counter = Counter()
    versions = []
    for sheet in sorted(project.glob("*.kicad_sch")):
        tree = wl.load(sheet)
        versions.append(str(tree.find("version").children[0].value))
        counts.update(c.name for c in tree.children if not c.is_atom)
        stack = list(tree.children)
        while stack:
            node = stack.pop()
            if node.is_atom:
                continue
            if node.name == "property" and str(node.children[0].value) == "Description":
                counts["Description"] += 1
            stack.extend(node.children)
    counts["components"], counts["nets"] = _netlist(project / root)
    return {**counts, "version": tuple(versions)}


def _top_level_nodes(project: Path) -> Counter:
    """Every top-level node of every sheet as compact text, except ``lib_symbols`` (whose
    children are counted one by one) and ``paper`` (``auto_size_paper`` may enlarge it)."""
    nodes: Counter = Counter()
    for sheet in sorted(project.glob("*.kicad_sch")):
        for child in wl.load(sheet).children:
            if child.is_atom or child.name == "paper":
                continue
            if child.name == "lib_symbols":
                nodes.update(f"{sheet.name} lib " + wl.serialise(s, compact=True) for s in child.children if not s.is_atom)
            else:
                nodes[f"{sheet.name} " + wl.serialise(child, compact=True)] += 1
    return nodes


def _reference_nodes(node, out: list) -> list:
    """Every ``(reference R)`` and ``(property "Reference" R)`` inside ``node``."""
    for child in node.children:
        if child.is_atom:
            continue
        if child.name == "reference" or (child.name == "property" and str(child.children[0].value) == "Reference"):
            out.append(child)
        _reference_nodes(child, out)
    return out


def _duplicate_power_reference(tools: dict, sheet: Path) -> None:
    """Give ``fix_annotation`` something to repair: two ``power:GND`` symbols added through
    ``add_labels``, the second given the first's reference through the write layer."""
    assert tools["add_labels"](str(sheet), [
        {"kind": "power", "text": "power:GND", "x": 12.7, "y": 12.7},
        {"kind": "power", "text": "power:GND", "x": 25.4, "y": 12.7},
    ])["status"] == "ok"
    root = wl.load(sheet)
    first, second = [
        c for c in root.children
        if not c.is_atom and c.name == "symbol" and c.find("lib_id") is not None
        and str(c.find("lib_id").children[0].value) == "power:GND"
    ][-2:]
    def value_at(node) -> int:  # (property "Reference" R ...) or (reference R)
        return 1 if node.name == "property" else 0

    reference = str(next(n.children[value_at(n)].value for n in _reference_nodes(first, [])))
    for node in _reference_nodes(second, []):
        wl.set_string(node, value_at(node), reference)
    wl.save(root, sheet)


def _call(tool: str, tools: dict, sheet: Path, demo: str) -> dict:
    if tool == "assign_footprints":
        return tools[tool](str(sheet), DEMOS[demo]["footprint"])
    if tool == "fix_annotation":
        return tools[tool](str(sheet))
    return tools[tool](str(sheet), HARMLESS[tool])


@pytest.mark.filterwarnings("ignore:Label 'T38'")
@pytest.mark.parametrize("demo", sorted(DEMOS))
@pytest.mark.parametrize("tool", sorted(ADDED))
def test_writing_tool_keeps_every_count_but_its_own_addition(tool: str, demo: str, sch_tools: dict, kicad_demo) -> None:
    project = kicad_demo(demo)
    root = DEMOS[demo]["root"]
    before = _census(project, root)
    assert (before["components"], before["nets"]) == DEMOS[demo]["netlist"]
    for name, n in DEMOS[demo]["census"].items():
        assert before.get(name, 0) == n, name
    if tool == "fix_annotation":  # the demos are annotated: give it one reference to repair
        _duplicate_power_reference(sch_tools, project / root)
        before = _census(project, root)
    before_nodes = _top_level_nodes(project)

    result = _call(tool, sch_tools, project / root, demo)

    assert result["status"] == "ok", result
    after = _census(project, root)
    for name in ("components", "nets", "Description", *CONTENT, "wire", "junction", "label", "symbol"):
        assert after.get(name, 0) == before.get(name, 0) + ADDED[tool].get(name, 0), name
    assert after["version"] == before["version"]
    lost = before_nodes - _top_level_nodes(project)
    if tool == "assign_footprints":  # the one symbol whose Footprint it set
        assert len(lost) == 1 and " (symbol " in next(iter(lost)), [text[:160] for text in lost]
    elif tool == "fix_annotation":  # the two power symbols it was given: their references and
        # their instances' project name, which kicad_tools' add_power writes as "project"
        assert result["changed"] is True
        assert sum(lost.values()) == 2, [text[:160] for text in lost]
        assert all(' (symbol (lib_id "power:GND")' in text for text in lost), [text[:160] for text in lost]
    else:
        assert not lost, [text[:160] for text in lost]


# --------------------------------------------------------------------------- self-check

def _corrupt_written_text(monkeypatch, old: str, new: str) -> None:
    """The text the builder tools write, with ``old`` replaced by ``new`` (still a sheet
    KiCad loads), as a faulty serialiser would produce it."""
    from kicad_mcp.toolsets import schematic

    real = schematic._schematic_text

    def corrupted(sch) -> str:
        text = real(sch)
        assert old in text
        return text.replace(old, new)

    monkeypatch.setattr(schematic, "_schematic_text", corrupted)


def _assert_not_verified(result: dict, project: Path, before: dict[str, bytes]) -> None:
    """Decision 4 and 9: ``ok: false``, ``write_not_verified``, the mismatches listed, every
    sheet byte-identical, no temporary file left."""
    assert result["ok"] is False and result["status"] == "error", result
    assert result["error"] == "write_not_verified"
    assert result["mismatches"]["count"] >= 1 and result["mismatches"]["items"]
    assert {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()} == before


def _bus_changed_on_write(tool: str, sch_tools: dict, kicad_demo, monkeypatch) -> None:
    """A write that changes one bus of interf_u (its uuid) is refused by the token check."""
    project = kicad_demo("interf_u")
    sheet = project / "interf_u.kicad_sch"
    bus = next(c for c in wl.load(sheet).children if not c.is_atom and c.name == "bus")
    uuid = str(bus.find("uuid").children[0].value)
    _corrupt_written_text(monkeypatch, f'(uuid "{uuid}")', '(uuid "00000000-0000-4000-8000-000000000038")')
    before = {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}

    result = sch_tools[tool](str(sheet), HARMLESS[tool])

    _assert_not_verified(result, project, before)
    assert f"bus:{uuid}" in result["mismatches"]["items"][0]["expected"]
    assert result["mismatches"]["items"][0]["file"] == "interf_u.kicad_sch"
    assert result["write_check"]["missing"]["count"] == 1


def test_add_symbols_self_check_fires_when_a_bus_changes(sch_tools, kicad_demo, monkeypatch) -> None:
    _bus_changed_on_write("add_symbols", sch_tools, kicad_demo, monkeypatch)


def test_add_wires_self_check_fires_when_a_bus_changes(sch_tools, kicad_demo, monkeypatch) -> None:
    _bus_changed_on_write("add_wires", sch_tools, kicad_demo, monkeypatch)


def test_add_junctions_self_check_fires_when_a_bus_changes(sch_tools, kicad_demo, monkeypatch) -> None:
    _bus_changed_on_write("add_junctions", sch_tools, kicad_demo, monkeypatch)


@pytest.mark.filterwarnings("ignore:Label 'T38'")
def test_add_labels_self_check_fires_when_a_bus_changes(sch_tools, kicad_demo, monkeypatch) -> None:
    _bus_changed_on_write("add_labels", sch_tools, kicad_demo, monkeypatch)


def test_add_symbols_netlist_check_fires_when_the_new_part_is_not_a_component(
    sch_tools, kicad_demo, monkeypatch
) -> None:
    """kicad-cli's netlist of the written sheet must hold every component it held before plus
    the parts added. A write that turns the new resistor into a power-style ``#`` reference
    (no source node changes, so only the netlist shows it) is refused."""
    project = kicad_demo("complex_hierarchy")
    sheet = project / "complex_hierarchy.kicad_sch"
    _corrupt_written_text(monkeypatch, '"R9038"', '"#R9038"')
    before = {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}

    result = sch_tools["add_symbols"](str(sheet), HARMLESS["add_symbols"])

    _assert_not_verified(result, project, before)
    check = result["netlist_check"]
    assert (check["before"]["components"], check["after"]["components"]) == (68, 68)
    assert check["expected_components"] == 69
    assert "R9038" in result["reason"]


def test_a_wire_joining_two_nets_passes_the_netlist_check(sch_tools, kicad_demo) -> None:
    """The check lets a write join nets (a new wire may do just that) and refuses only a net
    split or a lost pin: a wire from the end of one long horizontal wire of complex_hierarchy
    to the start of the next joins their two nets, and the write goes through."""
    project = kicad_demo("complex_hierarchy")
    sheet = project / "complex_hierarchy.kicad_sch"
    ends = []
    for wire in (c for c in wl.load(sheet).children if not c.is_atom and c.name == "wire"):
        (x1, y1), (x2, y2) = [
            (float(xy.children[0].value), float(xy.children[1].value)) for xy in wire.find("pts").children if not xy.is_atom
        ]
        if y1 == y2 and abs(x2 - x1) > 5:
            ends.append((x1, y1))
    before = _netlist(sheet)

    result = sch_tools["add_wires"](str(sheet), [{"p1": list(ends[0]), "p2": list(ends[1])}])

    assert result["status"] == "ok", result
    assert _netlist(sheet) == (before[0], before[1] - 1)


# ------------------------------------------------- the netlist check's own branches (round 1)

def test_add_symbols_with_a_duplicate_reference_is_refused_as_a_net_split(sch_tools, kicad_demo) -> None:
    """The net branch of the netlist check, through the real tool and kicad-cli: a new part
    that reuses C104 (already on complex_hierarchy) merges its pins into C104's, and the
    netlist after holds 54 nets where it held 52. Every net before must lie within one net
    after, so the write is refused and the sheet stays byte-identical."""
    project = kicad_demo("complex_hierarchy")
    sheet = project / "complex_hierarchy.kicad_sch"
    before = {p.name: p.read_bytes() for p in project.iterdir() if p.is_file()}
    parts = [{"lib_id": "Device:R", "x": 12.7, "y": 12.7, "ref": "C104", "value": "1k"}]

    result = sch_tools["add_symbols"](str(sheet), parts)

    _assert_not_verified(result, project, before)
    assert (result["netlist_check"]["before"]["nets"], result["netlist_check"]["after"]["nets"]) == (52, 54)
    assert {m["actual"] for m in result["mismatches"]["items"]} == {"split or pin lost"}
    assert all(m["expected"].startswith("one net holding ") for m in result["mismatches"]["items"])


def _verify_with_netlists(sheet: Path, monkeypatch, before: dict, after: dict, added: set[str]) -> dict:
    """Run ``_verify_schematic_write`` on ``sheet`` written unchanged (the token check passes),
    with kicad-cli's census of the sheet replaced by ``before`` and of the written file by
    ``after``. Returns the refusal as ``_not_verified_result`` shapes it."""
    from kicad_mcp.toolsets import schematic

    monkeypatch.setattr(schematic, "_netlist_census", lambda s, replacement=None: before if replacement is None else after)
    with pytest.raises(schematic.SchematicWriteNotVerified) as raised:
        schematic._verify_schematic_write(sheet, sheet, added)
    return schematic._not_verified_result(raised.value)


def _fake_census(refs: list[str], nets: list[set[tuple[str, str]]]) -> dict:
    return {"components": len(refs), "refs": refs, "nets": [frozenset(n) for n in nets]}


def test_netlist_check_refuses_a_net_that_split(kicad_demo, monkeypatch) -> None:
    sheet = kicad_demo("interf_u") / "interf_u.kicad_sch"
    before = _fake_census(["R1", "R2"], [{("R1", "1"), ("R2", "1")}])
    after = _fake_census(["R1", "R2"], [{("R1", "1")}, {("R2", "1")}])

    result = _verify_with_netlists(sheet, monkeypatch, before, after, set())

    assert [m["actual"] for m in result["mismatches"]["items"]] == ["split or pin lost"]
    assert result["mismatches"]["items"][0]["expected"] == "one net holding R1.1, R2.1"


def test_netlist_check_refuses_a_pin_that_left_every_net(kicad_demo, monkeypatch) -> None:
    sheet = kicad_demo("interf_u") / "interf_u.kicad_sch"
    before = _fake_census(["R1", "R2"], [{("R1", "1"), ("R2", "1")}])
    after = _fake_census(["R1", "R2"], [{("R1", "1")}])  # R2.1 is on no net after

    result = _verify_with_netlists(sheet, monkeypatch, before, after, set())

    assert [m["actual"] for m in result["mismatches"]["items"]] == ["split or pin lost"]


def test_netlist_check_refuses_a_lost_component_even_when_the_count_balances(kicad_demo, monkeypatch) -> None:
    """R2 vanishes while R3 (added) and a stray R4 appear: the count is the expected 3, so only
    the per-reference comparison can see the loss."""
    sheet = kicad_demo("interf_u") / "interf_u.kicad_sch"
    before = _fake_census(["R1", "R2"], [{("R1", "1")}])
    after = _fake_census(["R1", "R3", "R4"], [{("R1", "1")}])

    result = _verify_with_netlists(sheet, monkeypatch, before, after, {"R3"})

    assert result["netlist_check"]["expected_components"] == 3  # 2 before + R3
    assert [m["expected"] for m in result["mismatches"]["items"]] == ["components R2"]


def test_netlist_check_passes_a_net_join_and_a_new_part(kicad_demo, monkeypatch) -> None:
    """The refusals above are not blanket: joined nets and an added part pass."""
    from kicad_mcp.toolsets import schematic

    sheet = kicad_demo("interf_u") / "interf_u.kicad_sch"
    before = _fake_census(["R1", "R2"], [{("R1", "1")}, {("R2", "1")}])
    after = _fake_census(["R1", "R2", "R3"], [{("R1", "1"), ("R2", "1"), ("R3", "1")}])
    monkeypatch.setattr(schematic, "_netlist_census", lambda s, replacement=None: before if replacement is None else after)

    schematic._verify_schematic_write(sheet, sheet, {"R3"})
