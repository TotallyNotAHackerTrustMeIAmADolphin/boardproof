"""find_overlaps, issue 225: pin numbers and names as obstacles (the kind ``text_over_pin_text``) and the user's cases 1 to 3.

The box rules themselves are checked against the svg in tests/test_find_overlaps_pin_text.py; here every branch of the new kind has
a test that goes red under its mutant (the mutants are listed in the hand-back of issue 225).
"""

from __future__ import annotations

import math

import pytest

from boardproof import sheet_editing as se
from boardproof import sheet_overlaps, write_layer
from kicad_tools.sexp import SExp
from boardproof import kicad_cli
from tests.test_find_overlaps_pin_text import _export, _overlap, _project, _svg_texts, _tools


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


TURNS = [(0, None), (90, None), (180, None), (270, None), (0, "x"), (0, "y"), (90, "x"), (90, "y")]
LIBS = ["Device:R", "Connector_Generic:Conn_01x04", "Amplifier_Operational:LM358"]


@pytest.fixture(scope="module")
def pin_sheet(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("pins")
    tools = _tools()
    sch = _project(tmp)
    specs = [{"lib_id": lib, "x": 40.0 + 45.0 * i, "y": 40.0 + 50.0 * j, "ref": f"X{j}_{i}", "value": f"V{j}_{i}"}
             for i in range(len(TURNS)) for j, lib in enumerate(LIBS)]
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"
    for by in (90, 180, 270):  # one call per turn and per mirror: every call is verified with kicad-cli
        refs = [f"X{j}_{i}" for i, (b, _a) in enumerate(TURNS) for j in range(len(LIBS)) if b == by]
        assert tools["rotate_items"](str(sch), [{"references": refs, "by": by}])["ok"]
    for axis in ("x", "y"):
        refs = [f"X{j}_{i}" for i, (_b, a) in enumerate(TURNS) for j in range(len(LIBS)) if a == axis]
        assert tools["mirror_items"](str(sch), [{"references": refs, "axis": axis}])["ok"]
    return sch, write_layer.load(sch), _svg_texts(_export(sch, tmp / "svg"))


def _symbol(root, ref):
    return next(n for n in se._kids(root, "symbol") if se._ref(n) == ref)


def _fixture(tmp_path, lib="Amplifier_Operational:LM358"):
    tools = _tools()
    sch = _project(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": lib, "x": 100.0, "y": 100.0, "ref": "U1", "value": "X"}])["status"] == "ok"
    return tools, sch


def _kinds(tools, sch, kinds):
    return tools["find_overlaps"](str(sch), kinds)


@pytest.mark.parametrize("i", range(len(TURNS)))
@pytest.mark.parametrize("j", range(len(LIBS)))
def test_every_pin_text_box_lies_where_kicad_draws_the_text(pin_sheet, i, j):
    """Number and name boxes against the svg on eight turns and mirrors of a passive (numbers hidden), a connector (names
    hidden) and an operational amplifier (numbers and names inside): the centre of each box within 0.3 mm plus half the
    difference of the estimated and drawn length; nothing is predicted where nothing is drawn."""
    _sch, root, drawn = pin_sheet
    node = _symbol(root, f"X{j}_{i}")
    sx, sy, _r = se._at(node)
    boxes = sheet_overlaps._pin_text_boxes(root, node)
    near = [(t, b, length) for t, b, length in drawn if abs((b[0] + b[2]) / 2 - sx) < 14 and abs((b[1] + b[3]) / 2 - sy) < 14 and not t.startswith(("X", "V"))]
    whats = {w for w, *_ in boxes}
    if j == 0:
        assert boxes == [] and near == [], (boxes, near)          # Device:R hides its pin numbers and has no pin names
    elif j == 1:
        assert whats == {"number"} and len(boxes) == 4 and len(near) == 4   # the connector hides its pin names
    else:
        assert whats == {"number", "name"}
    assert len(boxes) == len(near), (boxes, near)
    for _what, _number, text, box in boxes:
        candidates = [(t, b, length) for t, b, length in near if t == text]
        assert candidates, (text, near)
        best = min(candidates, key=lambda c: math.hypot((c[1][0] + c[1][2]) / 2 - (box[0] + box[2]) / 2, (c[1][1] + c[1][3]) / 2 - (box[1] + box[3]) / 2))
        _t, b, length = best
        horizontal = (b[2] - b[0]) >= (b[3] - b[1])
        slack = 0.3 + abs(length - ((box[2] - box[0]) if horizontal else (box[3] - box[1]))) / 2
        along, across = (0, 1) if horizontal else (1, 0)   # the position across the text is exact; along it the length is an estimate
        assert abs((b[along] + b[along + 2]) / 2 - (box[along] + box[along + 2]) / 2) < slack + 0.1, (text, box, b)
        assert abs((b[across] + b[across + 2]) / 2 - (box[across] + box[across + 2]) / 2) < 0.15, (text, box, b)


def test_a_label_on_a_pin_whose_text_runs_along_the_pin_covers_its_number_and_is_reported(tmp_path):
    """The shape of the user's pin 25: the label sits on the pin end and its text runs over the pin number (drawn: they overlap)."""
    tools, sch = _fixture(tmp_path)
    # LM358 unit 1 pin 2 ('-', number 2): connection point (92.38, 102.54), the line runs right to 94.92, its number is centred on 93.65
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 92.38, "y": 102.54, "text": "S_ADC", "rotation": 0, "snap": False}])["status"] == "ok"
    drawn = {t: b for t, b, _l in _svg_texts(_export(sch, tmp_path / "svg"))}
    px, py = _overlap(drawn["S_ADC"], drawn["2"])
    assert px > 0.5 and py > 0.5                           # a real collision in the render
    found = _kinds(tools, sch, ["text_over_pin_text"])
    assert found["counts"] == {"text_over_pin_text": found["found"]}
    numbers = [g for g in found["findings"] if g["items"][1]["what"] == "number"]   # the text also runs over the name '-' further in
    assert len(numbers) == 1
    f = numbers[0]
    assert f["estimated"] is True and [i["kind"] for i in f["items"]] == ["label", "pin_text"]
    assert (f["items"][1]["what"], f["items"][1]["pin"], f["items"][1]["reference"]) == ("number", "2", "U1") and f["items"][0]["text"] == "S_ADC"
    assert f["overlap"][0] > 0.5 and f["overlap"][1] > 0.5


def test_a_label_on_a_pin_whose_text_runs_away_from_the_part_is_not_reported(tmp_path):
    tools, sch = _fixture(tmp_path)
    # pin 1 (output) at (107.62, 100): its line runs left into the body, a label with the stored justify left runs right, away from it
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 107.62, "y": 100.0, "text": "OUT", "rotation": 0, "snap": False}])["status"] == "ok"
    assert _kinds(tools, sch, ["text_over_pin_text"])["found"] == 0


def test_a_text_over_a_pin_name_inside_the_body_is_reported_as_a_name(tmp_path):
    tools, sch = _fixture(tmp_path)
    # the '-' of pin 2 is drawn from 2.67 mm right of the pin end (x 35.05 against 32.38 on the sheet of the measurement)
    assert tools["add_text"](str(sch), [{"x": 95.2, "y": 102.9, "text": "NOTE"}], snap=False)["status"] == "ok"
    names = [f for f in _kinds(tools, sch, ["text_over_pin_text"])["findings"] if f["items"][1]["what"] == "name"]
    assert names and names[0]["items"][1]["text"] == "-" and names[0]["items"][0]["kind"] == "text"


def test_a_property_text_over_a_pin_number_is_reported_too(tmp_path):
    tools, sch = _fixture(tmp_path)
    assert tools["set_property_positions"](str(sch), [{"references": ["U1"], "properties": {"Reference": {"at": [93.65, 101.5], "angle": 0}}}])["status"] == "ok"
    kinds = {(f["items"][0].get("property"), f["items"][1]["what"]) for f in _kinds(tools, sch, ["text_over_pin_text"])["findings"]}
    assert ("Reference", "number") in kinds


def test_hidden_pin_numbers_hidden_pin_names_and_hidden_pins_are_not_obstacles(tmp_path):
    # Device:R hides its numbers in the library: a label running along its pin covers nothing
    tools, sch = _fixture(tmp_path, "Device:R")
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 100.0, "y": 103.81, "text": "LONGNAME", "rotation": 90, "snap": False}])["status"] == "ok"
    assert _kinds(tools, sch, ["text_over_pin_text"])["found"] == 0
    # the connector hides its names: a text over the place where a name would be is not a finding
    tools, sch = _fixture(tmp_path / "c", "Connector_Generic:Conn_01x04")
    assert tools["add_text"](str(sch), [{"x": 100.0, "y": 97.0, "text": "NOTES"}], snap=False)["status"] == "ok"
    assert [f for f in _kinds(tools, sch, ["text_over_pin_text"])["findings"] if f["items"][1]["what"] == "name"] == []
    # a library pin that carries (hide yes) draws no text (set on the parsed tree, in memory only)
    tools, sch = _fixture(tmp_path / "d")
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 92.38, "y": 102.54, "text": "S_ADC", "rotation": 0, "snap": False}])["status"] == "ok"
    root = write_layer.load(sch)
    found, _ = sheet_overlaps._findings("demo.kicad_sch", root, 10.0, False)
    assert [f for f in found if f["kind"] == "text_over_pin_text"]
    lib = se._lib_symbol(root, _symbol(root, "U1"))
    for sub in se._kids(lib, "symbol"):
        for pin in se._kids(sub, "pin"):
            if pin.find_children("number")[0].get_string(0) in ("2", "6"):
                pin.children.append(SExp.list("hide", write_layer.symbol("yes")))
    found, _ = sheet_overlaps._findings("demo.kicad_sch", root, 10.0, False)
    assert [f for f in found if f["kind"] == "text_over_pin_text"] == []


def test_the_names_of_a_symbol_whose_pin_name_offset_is_zero_are_skipped_as_the_tool_text_says(tmp_path):
    _unused, sch = _fixture(tmp_path)
    root = write_layer.load(sch)
    node = _symbol(root, "U1")
    assert {w for w, *_ in sheet_overlaps._pin_text_boxes(root, node)} == {"number", "name"}
    lib = se._lib_symbol(root, node)
    offset = se._kids(lib, "pin_names")[0].find_children("offset")[0]   # set on the parsed tree, in memory only
    offset.children[0] = write_layer.number(0)
    assert {w for w, *_ in sheet_overlaps._pin_text_boxes(root, node)} == {"number"}


def test_markup_in_a_text_is_resolved_before_its_box_is_measured(tmp_path):
    """A label `VPP{slash}MCLR` is drawn as `VPP/MCLR` (10.18 mm at size 1.27) and `~{RST}` as `RST`: the box is that wide, not the
    17 mm of the stored characters (the first round of issue 225 counted the markup literally)."""
    tools, sch = _fixture(tmp_path, "Device:R")
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 150.0, "y": 150.0, "text": "VPP{slash}MCLR", "rotation": 0, "snap": False}])["status"] == "ok"
    box = next(t.box for t in sheet_overlaps._read(write_layer.load(sch))[1] if t.kind == "label")
    assert box[2] - box[0] == pytest.approx(10.18, abs=0.3)


def test_a_free_text_without_justify_is_centred_on_its_anchor_as_kicad_draws_it(tmp_path):
    """Real files hold free texts with no (justify): kicad-cli draws them centred (text-anchor middle), not from the anchor."""
    tools, sch = _fixture(tmp_path, "Device:R")
    assert tools["add_text"](str(sch), [{"x": 150.0, "y": 150.0, "text": "ABCD"}], snap=False)["status"] == "ok"
    root = write_layer.load(sch)
    node = next(n for n in root.children if not n.is_atom and n.name == "text")
    effects = node.find("effects")
    effects.children[:] = [c for c in effects.children if c.is_atom or c.name != "justify"]   # in memory only
    (tmp_path / "plain").mkdir()
    plain = tmp_path / "plain" / "plain.kicad_sch"
    write_layer.save(root, plain)
    drawn = next(b for t, b, _l in _svg_texts(_export(plain, tmp_path / "svg")) if t == "ABCD")
    box = next(t.box for t in sheet_overlaps._read(write_layer.load(plain))[1] if t.kind == "text")
    assert abs((drawn[0] + drawn[2]) / 2 - 150.0) < 0.3                       # drawn centred
    assert (box[0] + box[2]) / 2 == pytest.approx(150.0, abs=0.01)            # and the box is


def test_a_power_symbol_has_no_pin_texts(tmp_path):
    tools, sch = _fixture(tmp_path, "Device:R")
    assert tools["add_labels"](str(sch), [{"kind": "power", "x": 140.0, "y": 100.0, "text": "power:GND"}])["status"] == "ok"
    assert tools["add_text"](str(sch), [{"x": 139.5, "y": 102.0, "text": "AB"}], snap=False)["status"] == "ok"
    assert _kinds(tools, sch, ["text_over_pin_text"])["found"] == 0


def test_the_kind_counts_like_the_others_and_one_label_can_appear_under_two_kinds(tmp_path):
    tools, sch = _fixture(tmp_path)
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 92.38, "y": 102.54, "text": "S_ADC", "rotation": 0, "snap": False}])["status"] == "ok"
    both = tools["find_overlaps"](str(sch), ["label_over_own_symbol", "text_over_pin_text"])
    # the label is one item of three findings: the part, the pin number and the pin name
    assert both["counts"] == {"label_over_own_symbol": 1, "text_over_pin_text": 2} and both["found"] == 3
    cut = tools["find_overlaps"](str(sch), ["label_over_own_symbol", "text_over_pin_text"], max_findings=1)
    assert cut["found"] == 3 and cut["truncated"] is True and len(cut["findings"]) == 1
    assert tools["find_overlaps"](str(sch), ["text_over_pin_text"])["kinds"] == ["text_over_pin_text"]
    before = sch.read_bytes()
    tools["find_overlaps"](str(sch))
    assert sch.read_bytes() == before and "text_over_pin_text" in sheet_overlaps.KINDS


def test_hierarchical_labels_whose_drawn_text_covers_a_reference_are_reported(tmp_path):
    """The user's cases 1 and 2: a Reference with a long hierarchical label running over it from the right. Drawn, the label's
    marker and text start 1.15 text heights from its anchor and run left for the whole text; the old box (text only, 0.6 per
    character, no inset) ended 3 mm short."""
    tools = _tools()
    sch = _project(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 100.0, "y": 100.0, "ref": "R507", "value": "10k"}])["status"] == "ok"
    props = {"Reference": {"at": [100.0, 90.0], "angle": 0, "justify": "left"}}
    assert tools["set_property_positions"](str(sch), [{"references": ["R507"], "properties": props}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "hier", "x": 110.0, "y": 90.0, "text": "ENA_K2", "rotation": 180, "shape": "input", "snap": False}])["status"] == "ok"
    drawn = {t: b for t, b, _l in _svg_texts(_export(sch, tmp_path / "svg"))}
    px, py = _overlap(drawn["R507"], drawn["ENA_K2"])
    assert px > 0.5 and py > 0.5                           # a real collision in the render
    found = _kinds(tools, sch, ["text_text"])["findings"]
    assert len(found) == 1 and {i.get("text") for i in found[0]["items"]} == {"R507", "ENA_K2"}


def test_the_wording_of_tool_text_skill_and_architecture_is_pinned():
    from pathlib import Path

    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import schematic as schematic_toolset

    root = Path(__file__).resolve().parents[1]
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    text = " ".join(next(t.description for t in mcp._tool_manager.list_tools() if t.name == "find_overlaps").split())  # type: ignore[attr-defined]
    for phrase in ("text_over_pin_text (issue 225)", "a pin number is centred on the middle of its pin line, 0.36 mm above a horizontal pin",
                   "the names of a symbol whose pin_names offset is 0 (drawn outside the body) are not checked",
                   "A global or hierarchical label follows its stored justify by the axis of its angle (measured; add_labels",
                   "the width is the sum of a per-character advance measured with kicad-cli svg",
                   "a capital 0.76 to 1.14 (I only 0.48)", "a small letter 0.48 to 1.33 (i j l narrow, m wide)",
                   "any other character outside ASCII counts 1.0 (not measured)",
                   "Sheet pins of a hierarchical sheet symbol are not read as texts",
                   "two texts that only touch, as lines of text stacked with no gap, are not a finding",
                   "can be an item of this kind and of label_over_own_symbol or text_symbol"):
        assert phrase in text, phrase
    stale = ("times 0.6", "0.6 of the font size", "the stroke font is wider", "0.6 per character", "TEXT_ADVANCE")
    for tool in mcp._tool_manager.list_tools():  # type: ignore[attr-defined]   # a stale width statement in ANY tool text fails
        assert not any(s in tool.description for s in stale), tool.name
    for rel in (".claude/skills/kicad-schematic/SKILL.md", "src/boardproof/sheet_property_arrange.py", "src/boardproof/sheet_overlaps.py",
                "src/boardproof/schematic_readability.py"):
        body = (root / rel).read_text(encoding="utf-8")
        assert not any(s in body for s in ("times font size times 0.6", "TEXT_ADVANCE", "characters times font size times 0.6")), rel
    skill = (root / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8")
    line = next(ln for ln in skill.splitlines() if ln.startswith("11. "))
    assert "`text_over_pin_text` names a label, free text or visible property text whose box covers the pin number or pin name" in line
    arch = (root / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    assert "`find_overlaps`, issue 225 (`sheet_overlaps.py`, kicad-cli 10.0.7;" in arch
    assert "`schematic_readability._ADVANCE` is a per-character advance table" in arch
    assert "so a box is shorter than the drawn text" not in arch
