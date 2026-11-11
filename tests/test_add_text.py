"""add_text (GitHub issue 145): create free text objects without changing connectivity.

Fixtures are built through the tools on ``tmp_path`` copies; nothing here edits KiCad text. Raw
facts are read from the saved file's ``(at ...)``, ``(font (size ...))`` and text atoms.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import add_text, kicad_cli, sheet_authoring, sheet_editing, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

SExp = write_layer.SExp


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def _tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def tools() -> dict:
    return _tools()


@pytest.fixture
def sheet(tmp_path, tools) -> Path:
    """R1 with a wire stub and the net label NETA on it."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    assert tools["add_wires"](str(sch), [{"p1": [50.8, 46.99], "p2": [50.8, 40.64], "route": "direct", "snap": False}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 50.8, "y": 40.64, "text": "NETA", "snap": False}])["status"] == "ok"
    return sch


def _texts(sch: Path) -> list[SExp]:
    return [c for c in write_layer.load(sch).children if not c.is_atom and c.name == "text"]


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _serial(node) -> str:
    return write_layer.serialise(node, compact=True)


def _nets(sch: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    return sheet_editing.read_nets(tree, sch, None)


def _others(sch: Path, skip: set[str]) -> list[str]:
    return [_serial(c) for c in write_layer.load(sch).children if c.is_atom or sheet_editing._uuid(c) not in skip]


def _raw(node: SExp) -> dict:
    at = node.find_children("at")[0]
    size = node.find_children("effects")[0].find_children("font")[0].find_children("size")[0]
    justify = [a.value for j in node.find_children("effects")[0].find_children("justify") for a in j.children]
    return {"text": node.get_string(0), "at": [at.get_value(0), at.get_value(1), at.get_value(2)],
            "size": [size.get_value(0), size.get_value(1)], "justify": justify, "uuid": sheet_editing._uuid(node)}


# --------------------------------------------------------------------------- creating


def test_texts_created_in_one_call_have_the_asked_position_rotation_and_size(sheet, tools):
    nets = _nets(sheet)
    result = tools["add_text"](str(sheet), [
        {"x": 25.4, "y": 30.48, "text": "U501.24 VDD"},
        {"x": 101.6, "y": 76.2, "text": "side", "rotation": 90, "size": 2, "justify": "left bottom"},
    ])
    assert result["ok"] is True and result["written"] is True and result["count"] == 2, result
    first, second = _texts(sheet)
    assert _raw(first) == {"text": "U501.24 VDD", "at": [25.4, 30.48, 0], "size": [1.27, 1.27], "justify": [], "uuid": result["added"][0]["uuid"]}
    assert _raw(second) == {"text": "side", "at": [101.6, 76.2, 90], "size": [2, 2], "justify": ["left", "bottom"], "uuid": result["added"][1]["uuid"]}
    assert result["added"][1]["rotation"] == 90 and result["added"][1]["size"] == 2
    assert _nets(sheet) == nets  # a free text changes no net
    assert result["connectivity"]["changed"] is False
    # a KiCad 10 sheet gets the node as KiCad writes it
    assert [c.name for c in first.children if not c.is_atom] == ["exclude_from_sim", "at", "effects", "uuid"]


def test_the_new_texts_are_in_a_kicad_cli_render_and_nothing_else_changed(sheet, tools, tmp_path):
    before = _others(sheet, set())
    result = tools["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "RENDERPROBE"}])
    assert result["ok"] is True, result
    uid = result["added"][0]["uuid"]
    assert _others(sheet, {uid}) == before  # every other node is byte-equal
    kicad_cli.export_svg(sheet, tmp_path / "svg")
    svg = next((tmp_path / "svg").glob("*.svg")).read_text(encoding="utf-8")
    assert "RENDERPROBE" in svg, "the text is not in kicad-cli's render"


def test_kicad_cli_keeps_the_written_angles(sheet, tools, tmp_path):
    """Why only 0 and 90 are accepted: kicad-cli reads them back as written (it reads 180 and 270 as 0 and 90)."""
    tools["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "A0"}, {"x": 25.4, "y": 40.64, "text": "A90", "rotation": 90}])
    copy = tmp_path / "copy.kicad_sch"
    shutil.copy(sheet, copy)
    kicad_cli.upgrade_schematic(copy)
    angles = {n.get_string(0): _raw(n)["at"] for n in _texts(copy)}
    assert angles == {"A0": [25.4, 30.48, 0], "A90": [25.4, 40.64, 90]}


def test_the_text_goes_in_front_of_the_trailing_nodes(sheet, tools):
    tools["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "T"}])
    names = [c.name for c in write_layer.load(sheet).children if not c.is_atom]
    tail = [k for k, n in enumerate(names) if n in ("sheet_instances", "embedded_fonts")]
    assert names.index("text") < min(tail)


def test_snap_rounds_to_the_grid_by_default_and_not_when_off(sheet, tools):
    result = tools["add_text"](str(sheet), [{"x": 25.0, "y": 30.0, "text": "A"}])
    assert result["ok"] is True, result
    assert result["added"][0]["at"] == [25.4, 30.48] and result["added"][0]["snapped"] == [25.4, 30.48] and result["added"][0]["asked"] == [25.0, 30.0]
    assert _raw(_texts(sheet)[0])["at"][:2] == [25.4, 30.48]
    result = tools["add_text"](str(sheet), [{"x": 25.0, "y": 30.0, "text": "B"}], snap=False)
    assert result["added"][0]["at"] == [25.0, 30.0] and result["added"][0]["snapped"] is None
    assert _raw(_texts(sheet)[1])["at"][:2] == [25.0, 30.0]
    result = tools["add_text"](str(sheet), [{"x": 11.0, "y": 11.0, "text": "C"}], grid=2.54)
    assert result["added"][0]["at"] == [10.16, 10.16]


def test_a_dry_run_writes_nothing_and_lists_what_would_be_added(sheet, tools):
    before = _bytes(sheet)
    result = tools["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "T"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and result["would_add"][0]["text"] == "T" and "added" not in result, result
    assert _bytes(sheet) == before


def test_the_returned_uuid_works_with_set_text(sheet, tools):
    uid = tools["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "OLD"}])["added"][0]["uuid"]
    changed = tools["set_text"](str(sheet), [{"uuid": uid, "text": "NEW\nTWO"}])
    assert changed["ok"] is True and changed["written"] is True, changed
    node = _texts(sheet)[0]
    assert node.get_string(0) == "NEW\nTWO" and sheet_editing._uuid(node) == uid
    assert _raw(node)["at"] == [25.4, 30.48, 0] and _raw(node)["size"] == [1.27, 1.27]


def test_edge_whitespace_is_written_and_listed(sheet, tools):
    result = tools["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": " lead"}])
    assert result["ok"] is True and [w["code"] for w in result["warnings"]] == ["text_whitespace"], result
    assert _texts(sheet)[0].get_string(0) == " lead"


def test_a_sheet_a_parent_uses_gets_its_text_and_the_parent_is_untouched(tmp_path, tools):
    made = project_toolset._project_create(str(tmp_path / "h"), "demo")
    root = Path(made["schematic_path"])
    assert tools["create_sheets"](str(root), [{"file": "sensors.kicad_sch"}])["status"] == "ok"
    spec = {"file": "sensors.kicad_sch", "name": "Sensors", "x": 101.6, "y": 50.8, "height": 25.4, "pins": [{"name": "SER", "direction": "input"}]}
    assert tools["add_sheet_symbols"](str(root), [spec])["status"] == "ok"
    child = root.parent / "sensors.kicad_sch"
    root_before = root.read_bytes()
    result = tools["add_text"](str(child), [{"x": 25.4, "y": 30.48, "text": "child note"}], root_sch=str(root))
    assert result["ok"] is True and result["written"] is True, result
    assert _texts(child)[0].get_string(0) == "child note"
    assert root.read_bytes() == root_before


# --------------------------------------------------------------------------- refusals


_BAD = [
    ("line break", {"x": 1, "y": 1, "text": "a\nb"}, "invalid_spec"),
    ("carriage return", {"x": 1, "y": 1, "text": "a\rb"}, "invalid_spec"),
    ("tab", {"x": 1, "y": 1, "text": "a\tb"}, "invalid_spec"),
    ("line separator", {"x": 1, "y": 1, "text": "a b"}, "invalid_spec"),
    ("paragraph separator", {"x": 1, "y": 1, "text": "a b"}, "invalid_spec"),
    ("next line", {"x": 1, "y": 1, "text": "a\x85b"}, "invalid_spec"),
    ("control character", {"x": 1, "y": 1, "text": "a\x1bb"}, "invalid_spec"),
    ("lone surrogate", {"x": 1, "y": 1, "text": "a\ud800b"}, "invalid_spec"),
    ("empty", {"x": 1, "y": 1, "text": ""}, "empty_text"),
    ("blank", {"x": 1, "y": 1, "text": "   "}, "empty_text"),
    ("text not a string", {"x": 1, "y": 1, "text": 5}, "invalid_spec"),
    ("no text", {"x": 1, "y": 1}, "invalid_spec"),
    ("no x", {"y": 1, "text": "a"}, "invalid_spec"),
    ("x a bool", {"x": True, "y": 1, "text": "a"}, "invalid_spec"),
    ("x not finite", {"x": float("nan"), "y": 1, "text": "a"}, "invalid_spec"),
    ("y beyond the page", {"x": 1, "y": 1e9, "text": "a"}, "invalid_spec"),
    ("bad rotation", {"x": 1, "y": 1, "text": "a", "rotation": 45}, "invalid_spec"),
    ("rotation 180 (KiCad keeps 0)", {"x": 1, "y": 1, "text": "a", "rotation": 180}, "invalid_spec"),
    ("rotation 270 (KiCad keeps 90)", {"x": 1, "y": 1, "text": "a", "rotation": 270}, "invalid_spec"),
    ("rotation a bool", {"x": 1, "y": 1, "text": "a", "rotation": False}, "invalid_spec"),
    ("size zero", {"x": 1, "y": 1, "text": "a", "size": 0}, "invalid_spec"),
    ("size too big", {"x": 1, "y": 1, "text": "a", "size": 500}, "invalid_spec"),
    ("size a bool", {"x": 1, "y": 1, "text": "a", "size": True}, "invalid_spec"),
    ("bad justify word", {"x": 1, "y": 1, "text": "a", "justify": "middle"}, "invalid_spec"),
    ("opposite justify words", {"x": 1, "y": 1, "text": "a", "justify": "left right"}, "invalid_spec"),
    ("unknown key", {"x": 1, "y": 1, "text": "a", "color": "red"}, "invalid_spec"),
    ("not an object", "a", "invalid_spec"),
]


@pytest.mark.parametrize("bad", [b[1] for b in _BAD], ids=[b[0] for b in _BAD])
def test_a_bad_item_refuses_the_whole_call_and_writes_nothing(sheet, tools, bad):
    code = next(c for _n, item, c in _BAD if item is bad)
    before = _bytes(sheet)
    result = tools["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "GOOD"}, bad])
    assert result["ok"] is False and result["code"] == code, result
    assert _bytes(sheet) == before


@pytest.mark.parametrize("items", [[], None, "x", {"x": 1}], ids=["empty", "none", "string", "object"])
def test_items_must_be_a_non_empty_list(sheet, tools, items):
    before = _bytes(sheet)
    result = tools["add_text"](str(sheet), items)
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _bytes(sheet) == before


def test_two_identical_items_are_refused(sheet, tools):
    item = {"x": 25.4, "y": 30.48, "text": "T"}
    before = _bytes(sheet)
    result = tools["add_text"](str(sheet), [item, dict(item)])
    assert result["ok"] is False and result["code"] == "invalid_spec" and "repeats" in result["reason"], result
    assert _bytes(sheet) == before


@pytest.mark.parametrize("args", [{"snap": "yes"}, {"grid": 0}, {"grid": -1.27}, {"grid": float("inf")}, {"dry_run": "no"}], ids=str)
def test_bad_snap_grid_or_dry_run_is_refused(sheet, tools, args):
    before = _bytes(sheet)
    result = tools["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "T"}], **args)
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _bytes(sheet) == before


def test_a_missing_sheet_is_refused(tmp_path, tools):
    result = tools["add_text"](str(tmp_path / "none.kicad_sch"), [{"x": 1, "y": 1, "text": "T"}])
    assert result["ok"] is False and result["code"] == "file_not_found", result


# --------------------------------------------------------------------------- the node itself


def test_an_older_sheet_gets_no_exclude_from_sim():
    old = add_text.text_node(20230121, "T", 1, 2, 0, 1.27, [])
    new = add_text.text_node(20260101, "T", 1, 2, 0, 1.27, [])
    assert [c.name for c in old.children if not c.is_atom] == ["at", "effects", "uuid"]
    assert [c.name for c in new.children if not c.is_atom] == ["exclude_from_sim", "at", "effects", "uuid"]


# --------------------------------------------------------------------------- the guards fire


def test_a_corrupted_write_is_refused_and_leaves_the_sheet_alone(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _tools()["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "T"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sheet) == before
    assert not [p for p in sheet.parent.iterdir() if p.name.endswith(".tmp")]


def test_the_staged_check_fires_when_the_call_touches_more_than_it_added(sheet, monkeypatch):
    """A text_node that also renames the symbol's first property is caught before anything is written."""
    real = add_text._insert

    def greedy(root, node):
        real(root, node)
        symbol = next(c for c in root.children if not c.is_atom and c.name == "symbol")
        write_layer.set_string(symbol.find_children("property")[0], 1, "STRAY")

    before = _bytes(sheet)
    monkeypatch.setattr(add_text, "_insert", greedy)
    result = _tools()["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "T"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert _bytes(sheet) == before


def test_the_staged_check_fires_when_a_node_that_was_not_announced_appears(sheet, monkeypatch):
    real = add_text._insert

    def extra(root, node):
        real(root, node)
        real(root, add_text.text_node(20260101, "STOWAWAY", 5.08, 5.08, 0, 1.27, []))

    before = _bytes(sheet)
    monkeypatch.setattr(add_text, "_insert", extra)
    result = _tools()["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "T"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert _bytes(sheet) == before


def test_the_netlist_oracle_fires_when_the_new_object_is_a_label(sheet, monkeypatch):
    """The connectivity comparison is what proves a text joins nothing: with a label in its place it refuses."""
    real = add_text.text_node

    def label(version, text, x, y, rotation, size, justify):
        node = real(version, text, x, y, rotation, size, justify)
        return SExp.list("global_label", write_layer.string("SNEAKY"), SExp.list("shape", write_layer.symbol("input")),
                         SExp.list("at", write_layer.number(50.8), write_layer.number(46.99), write_layer.number(0)),
                         node.find_children("effects")[0], node.find_children("uuid")[0])

    before = _bytes(sheet)
    monkeypatch.setattr(add_text, "text_node", label)
    monkeypatch.setattr(add_text, "_erc_unchanged", lambda sheet, new: (lambda _b, _a: None))  # the ERC check runs first; this test is about the netlist
    result = _tools()["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "T"}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert _bytes(sheet) == before


def test_the_erc_check_also_catches_the_label(sheet, monkeypatch):
    """With the netlist comparison out of play, the ERC census still sees the label that replaced the text."""
    real = add_text.text_node

    def label(version, text, x, y, rotation, size, justify):
        node = real(version, text, x, y, rotation, size, justify)
        return SExp.list("global_label", write_layer.string("SNEAKY"), SExp.list("shape", write_layer.symbol("input")),
                         SExp.list("at", write_layer.number(50.8), write_layer.number(46.99), write_layer.number(0)),
                         node.find_children("effects")[0], node.find_children("uuid")[0])

    before = _bytes(sheet)
    monkeypatch.setattr(add_text, "text_node", label)
    monkeypatch.setattr(sheet_editing, "compare_nets", lambda *a, **k: [])
    result = _tools()["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "T"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "erc_unchanged", result
    assert _bytes(sheet) == before


def test_the_erc_check_fires_when_the_census_moves(sheet, monkeypatch):
    real = sheet_authoring.erc_census
    calls = {"n": 0}

    def moving(root):
        calls["n"] += 1
        census = real(root)
        if calls["n"] == 2:  # the second census is the one with the new text
            census[("invented", "error")] += 1
        return census

    before = _bytes(sheet)
    monkeypatch.setattr(sheet_authoring, "erc_census", moving)
    result = _tools()["add_text"](str(sheet), [{"x": 25.4, "y": 30.48, "text": "T"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "erc_unchanged", result
    assert "invented" in str(result["changed"])
    assert _bytes(sheet) == before


def test_there_is_no_connectivity_override(tools):
    import inspect

    assert "allow_connectivity_change" not in inspect.signature(tools["add_text"]).parameters
