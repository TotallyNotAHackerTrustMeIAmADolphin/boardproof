"""Issue 267: justify of add_labels and move_labels is stored as given and read back from the raw node; the stored left and
right of a property text on a symbol turned by 90 degrees is swapped against how kicad-cli draws it.

Reproduction (main c47d871): add_labels with justify "right bottom" at rotation 180 stored "left bottom" (the key was not read);
move_labels had no justify key and its refusal named only selectors. Fixtures are built through the tools on tmp_path; the raw
(justify ..) is read with the write layer and the drawing from ``kicad-cli sch export svg``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_editing, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset
from tests.test_property_arrange import _rendered_texts


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture
def tools():
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _sheet(tmp_path) -> Path:
    return Path(project_toolset._project_create(str(tmp_path / "p"), "demo")["schematic_path"])


def _stored(sch: Path) -> dict[str, str]:
    """{label text: raw justify words} of every label, global and hierarchical label."""
    return {n.get_string(0): " ".join(sheet_editing._raw_justify(n)) for n in write_layer.load(sch).children
            if not n.is_atom and n.name in ("label", "global_label", "hierarchical_label")}


KINDS = [("label", "LA"), ("global", "GA"), ("hier", "HA")]


@pytest.mark.parametrize("kind,text", KINDS)
@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_add_labels_stores_the_justify_as_given(tmp_path, tools, kind, text, rotation):
    sch = _sheet(tmp_path)
    r = tools["add_labels"](str(sch), [{"kind": kind, "x": 50.8, "y": 50.8, "text": text, "rotation": rotation, "justify": "right bottom"}])
    assert r["status"] == "ok", r
    assert _stored(sch)[text] == "right bottom"


def test_add_labels_without_justify_keeps_the_default(tmp_path, tools):
    sch = _sheet(tmp_path)
    specs = [{"kind": k, "x": 50.8, "y": 50.8 + 2.54 * i, "text": t} for i, (k, t) in enumerate(KINDS)]
    assert tools["add_labels"](str(sch), specs)["status"] == "ok"
    assert _stored(sch) == {"LA": "left bottom", "GA": "left", "HA": "left"}
    default = _stored(sch)
    sch2 = Path(project_toolset._project_create(str(tmp_path / "q"), "demo")["schematic_path"])
    assert tools["add_labels"](str(sch2), [{"kind": "label", "x": 50.8, "y": 50.8, "text": "LA", "justify": "left bottom"}])["status"] == "ok"
    assert _stored(sch2)["LA"] == default["LA"]  # the asked default and the unasked default are the same node


def test_add_labels_pairs_each_justify_with_its_own_label(tmp_path, tools):
    sch = _sheet(tmp_path)
    specs = [{"kind": "label", "x": 50.8, "y": 50.8, "text": "A", "justify": "right top"},
             {"kind": "label", "x": 50.8, "y": 60.96, "text": "B"},
             {"kind": "label", "x": 50.8, "y": 71.12, "text": "C", "justify": "left"}]
    assert tools["add_labels"](str(sch), specs)["status"] == "ok"
    stored = _stored(sch)
    assert (stored["A"], stored["C"]) == ("right top", "left") and stored["B"] != "right top"


@pytest.mark.parametrize("bad", ["sideways", "left right", "top bottom", "left left", 3, None])
def test_add_labels_refuses_a_bad_justify_and_writes_nothing(tmp_path, tools, bad):
    sch = _sheet(tmp_path)
    before = sch.read_bytes()
    r = tools["add_labels"](str(sch), [{"kind": "label", "x": 50.8, "y": 50.8, "text": "A", "justify": bad}])
    assert r["status"] != "ok" and "justify" in str(r), r
    assert sch.read_bytes() == before


def test_add_labels_refuses_justify_on_a_power_symbol_or_no_connect(tmp_path, tools):
    sch = _sheet(tmp_path)
    before = sch.read_bytes()
    for spec in ({"kind": "no_connect", "x": 50.8, "y": 50.8, "justify": "left"}, {"kind": "power", "text": "power:GND", "x": 50.8, "y": 50.8, "justify": "left"}):
        r = tools["add_labels"](str(sch), [spec])
        assert r["status"] != "ok" and "justify" in str(r), r
    assert sch.read_bytes() == before


def test_add_labels_self_check_compares_the_justify(tmp_path, tools, monkeypatch):
    sch = _sheet(tmp_path)
    before = sch.read_bytes()
    monkeypatch.setattr(schematic_toolset, "_apply_label_justify", lambda *a, **k: None)  # a writer that drops the justify
    r = tools["add_labels"](str(sch), [{"kind": "label", "x": 50.8, "y": 50.8, "text": "A", "justify": "right bottom"}])
    assert r["status"] != "ok" and "label_justify" in str(r), r
    assert sch.read_bytes() == before


def test_add_labels_self_check_compares_the_text_of_the_paired_node(tmp_path, tools, monkeypatch):
    sch = _sheet(tmp_path)
    before = sch.read_bytes()
    real = schematic_toolset._apply_label_justify
    monkeypatch.setattr(schematic_toolset, "_apply_label_justify", lambda root, j, old: real(root, [(k, t, ["right", "top"], n) for k, t, _w, n in j], old))
    r = tools["add_labels"](str(sch), [{"kind": "label", "x": 50.8, "y": 50.8, "text": "A", "justify": "right bottom"}])
    assert r["status"] != "ok" and "label_justify" in str(r), r  # a writer that overwrites the words with another value
    assert sch.read_bytes() == before


# --------------------------------------------------------------------------- move_labels


def _with_label(tmp_path, tools, rotation=0, justify=None):
    sch = _sheet(tmp_path)
    spec = {"kind": "label", "x": 50.8, "y": 50.8, "text": "A", "rotation": rotation}
    if justify:
        spec["justify"] = justify
    assert tools["add_labels"](str(sch), [spec])["status"] == "ok"
    return sch


def test_move_labels_sets_the_justify_with_a_move_and_alone(tmp_path, tools):
    sch = _with_label(tmp_path, tools, 180)
    r = tools["move_labels"](str(sch), [{"texts": ["A"], "offset": [2.54, 0], "justify": "right bottom"}], allow_connectivity_change=True)
    assert r["ok"], r
    assert _stored(sch)["A"] == "right bottom"
    r = tools["move_labels"](str(sch), [{"texts": ["A"], "justify": "left top"}])
    assert r["ok"], r
    assert _stored(sch)["A"] == "left top"
    assert r["moved"][0]["from"] == r["moved"][0]["to"] and r["moved"][0]["justify"] == "left top"


def test_move_labels_without_justify_keeps_the_stored_one(tmp_path, tools):
    sch = _with_label(tmp_path, tools, 0, "right top")
    assert tools["move_labels"](str(sch), [{"texts": ["A"], "offset": [2.54, 0]}], allow_connectivity_change=True)["ok"]
    assert _stored(sch)["A"] == "right top"


def test_move_labels_refuses_a_bad_justify_and_writes_nothing(tmp_path, tools):
    sch = _with_label(tmp_path, tools)
    before = sch.read_bytes()
    r = tools["move_labels"](str(sch), [{"texts": ["A"], "justify": "left right"}])
    assert not r["ok"] and r["code"] == "invalid_spec", r
    assert sch.read_bytes() == before


def test_move_labels_self_check_compares_the_justify(tmp_path, tools, monkeypatch):
    sch = _with_label(tmp_path, tools, 0, "left bottom")
    before = sch.read_bytes()
    monkeypatch.setattr(sheet_editing, "_set_justify", lambda prop, words: None)  # a move that keeps the old justify
    r = tools["move_labels"](str(sch), [{"texts": ["A"], "justify": "right top"}])
    assert not r["ok"] and r["code"] == "write_not_verified" and "label_justify" in str(r), r
    assert sch.read_bytes() == before


def test_move_labels_self_check_rejects_overwritten_justify(tmp_path, tools, monkeypatch):
    sch = _with_label(tmp_path, tools, 0, "left bottom")
    before = sch.read_bytes()
    real = sheet_editing._set_justify
    monkeypatch.setattr(sheet_editing, "_set_justify", lambda prop, words: real(prop, ["left", "bottom"]))  # the default written over the ask
    r = tools["move_labels"](str(sch), [{"texts": ["A"], "justify": "right top"}])
    assert not r["ok"] and r["code"] == "write_not_verified", r
    assert sch.read_bytes() == before


def test_the_move_labels_refusal_names_the_accepted_keys_and_the_example_runs(tmp_path, tools):
    import json

    sch = _with_label(tmp_path, tools)
    r = tools["move_labels"](str(sch), [{"texts": ["A"]}])
    assert not r["ok"] and r["code"] == "invalid_spec"
    for word in ("'to'", "'offset'", "'angle'", "'justify'", "texts"):
        assert word in r["reason"], (word, r)
    # the example quoted in the refusal and in the registered description is a working call (the sheet has no VCC: use its own text)
    moves = json.loads(sheet_editing.MOVE_LABELS_EXAMPLE.replace("VCC", "A"))
    got = tools["move_labels"](str(sch), moves, allow_connectivity_change=True)
    assert got["ok"], got
    assert _stored(sch)["A"] == "right bottom"
    doc = " ".join(tools["move_labels"].__doc__.split())
    assert 'Example call, moves = [{"texts": ["VCC"], "offset": [0, 2.54], "angle": 180, "justify": "right bottom"}]' in doc
    assert json.loads(sheet_editing.MOVE_LABELS_EXAMPLE) == [{"texts": ["VCC"], "offset": [0, 2.54], "angle": 180, "justify": "right bottom"}]


def test_the_tool_text_names_justify():
    add = " ".join(_doc("add_labels").split())
    assert "justify (label, global and hier only" in add and "left bottom" in add


def _doc(name: str) -> str:
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    return next(t.fn.__doc__ for t in mcp._tool_manager.list_tools() if t.name == name)  # type: ignore[attr-defined]


# --------------------------------------------------------------------------- set_property_positions, proven against kicad-cli

TURNS = [(by, axis) for by in (0, 90, 180, 270) for axis in (None, "x", "y")]


def test_the_stored_justify_of_a_property_on_a_turned_symbol_against_the_drawing(tmp_path, tools):
    """A Reference stored "left" and a Value stored "right", each 10 mm beside the symbol, with the stored angle the tool's rule
    gives (90 on a symbol turned 90 or 270): kicad-cli draws which side of the anchor the text runs to?"""
    sch = _sheet(tmp_path)
    specs = [{"lib_id": "Device:R", "x": 30.0 + 30.0 * i, "y": 60.0, "ref": f"X{i}", "value": f"VAL{i}"} for i in range(len(TURNS))]
    assert tools["add_symbols"](str(sch), specs)["status"] == "ok"
    for i, (by, axis) in enumerate(TURNS):
        if by:
            assert tools["rotate_items"](str(sch), [{"references": [f"X{i}"], "by": by}])["ok"]
        if axis:
            assert tools["mirror_items"](str(sch), [{"references": [f"X{i}"], "axis": axis}])["ok"]
        angle = 90 if by in (90, 270) else 0
        change = {"offset": [0, -8], "angle": angle}
        r = tools["set_property_positions"](str(sch), [{"references": [f"X{i}"], "properties": {
            "Reference": {**change, "justify": "left"}, "Value": {"offset": [0, 8], "angle": angle, "justify": "right"}}}])
        assert r["ok"], r
    drawn = _rendered_texts(sch, tmp_path / "svg")
    measured = {}
    for i, (by, axis) in enumerate(TURNS):
        ax = 30.0 + 30.0 * i
        for text, stored in ((f"X{i}", "left"), (f"VAL{i}", "right")):
            rot, cx, cy, width = drawn[text]
            runs = "right" if cx > ax else "left"  # centre of the drawn text against its anchor column (angle 0 drawn)
            assert not rot, (by, axis, "drawn turned")
            natural = "right" if stored == "left" else "left"  # "left" = the text starts at the anchor and runs right
            assert measured.setdefault((by, axis), runs != natural) == (runs != natural), (by, axis)  # Reference and Value agree
    # measured with kicad-cli 10.0.7: the turns and mirrors (rotate_items by, mirror_items axis) whose stored left and right are swapped
    assert {k for k, swapped in measured.items() if swapped} == SWAPPED
    assert len(measured) == 12


SWAPPED = {(90, None), (90, "x"), (180, None), (180, "x"), (0, "y"), (270, "y")}


def test_the_tool_texts_say_which_turns_swap_the_stored_justify():
    text = " ".join(_doc("set_property_positions").split())
    for sentence in (
        '"left" in a stored justify is the text starting at the anchor and running right',
        "the stored words are drawn swapped (left runs left, right runs right) on a symbol turned 90 or 180 degrees, with or without "
        "mirror_items axis x, and on a symbol turned 0 or 270 degrees with axis y; every other turn and mirror draws them as stored",
        "(measured with kicad-cli sch export svg; the test is tests/test_label_justify.py)",
    ):
        assert sentence in text, sentence
    skill = (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8")
    assert "stored `left` and `right` justify of a property text is swapped against the drawing" in skill


# --------------------------------------------------------------------------- review of fafba9b


@pytest.mark.parametrize("move", [
    {"texts": ["A"], "offset": [1.27, 0], "rotate": 90},
    {"texts": ["A"], "to": [50.8, 55.88], "rotate": 90},
    {"uuid": "x", "to": [1, 1]},
    {"offset": [1, 0]},
])
def test_a_label_move_with_an_unknown_key_gets_the_same_hint(tmp_path, tools, move):
    sch = _with_label(tmp_path, tools)
    before = sch.read_bytes()
    r = tools["move_labels"](str(sch), [move])
    assert not r["ok"] and r["code"] == "invalid_spec", r
    assert "moves[0]" in r["reason"] and "selectors[0]" not in r["reason"], r
    for word in ("'to'", "'offset'", "'angle'", "'justify'", "texts", "example"):
        assert word in r["reason"], (word, r)
    assert sch.read_bytes() == before


@pytest.mark.parametrize("words", ["", "  "])
def test_an_empty_justify_is_refused_by_both_tools(tmp_path, tools, words):
    sch = _with_label(tmp_path, tools)
    before = sch.read_bytes()
    r = tools["add_labels"](str(sch), [{"kind": "label", "x": 60.96, "y": 60.96, "text": "B", "justify": words}])
    assert r["status"] != "ok" and "justify" in str(r), r
    r = tools["move_labels"](str(sch), [{"texts": ["A"], "justify": words}])
    assert not r["ok"] and r["code"] == "invalid_spec" and "justify" in r["reason"], r
    assert sch.read_bytes() == before


@pytest.mark.parametrize("words", ["right", "mirror", "top", "right bottom mirror"])
def test_the_documented_justify_forms_are_stored(tmp_path, tools, words):
    sch = _sheet(tmp_path)
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 50.8, "y": 50.8, "text": "A", "justify": words}])["status"] == "ok"
    assert _stored(sch)["A"] == " ".join(sheet_editing._justify_words(words, "x"))
    doc = " ".join(_doc("add_labels").split())
    assert "one or more of the words left, right, top, bottom, mirror" in doc and "never empty" in doc


# --------------------------------------------------------------------------- delta review of 0140706


def test_the_texts_say_the_words_are_stored_in_kicad_order():
    sentence = 'its words in KiCad order (left or right, then top or bottom, then mirror: "bottom right" is stored as "right bottom")'
    for name in ("add_labels", "move_labels"):
        text = " ".join(_doc(name).split())
        assert sentence in text and "written as given" not in text, name
    skill = (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8")
    assert "stored with its words in KiCad order (`left` or `right`, then `top` or `bottom`, then `mirror`: `bottom right` is stored as `right bottom`)" in skill
    assert "stored as given" not in skill


def test_a_non_string_label_justify_message_does_not_offer_the_empty_string(tmp_path, tools):
    sch = _with_label(tmp_path, tools)
    r = tools["move_labels"](str(sch), [{"texts": ["A"], "justify": 5}])
    assert not r["ok"] and r["code"] == "invalid_spec" and "justify" in r["reason"] and "clears" not in r["reason"], r
    r = tools["add_labels"](str(sch), [{"kind": "label", "x": 60.96, "y": 60.96, "text": "B", "justify": 5}])
    assert "justify" in str(r) and "clears" not in str(r), r
    with pytest.raises(sheet_editing.SheetAuthoringError, match="clears it"):  # property texts keep it: '' clears there
        sheet_editing._justify_words(5, "x")
