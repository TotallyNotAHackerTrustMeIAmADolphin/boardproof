"""set_text (GitHub issue 114): change the text of existing labels and free text objects.

Fixtures are built through the tools and through ``write_layer`` on ``tmp_path`` copies; nothing
here edits KiCad text. Free text objects have no creating tool, so a test adds one to the loaded
tree and saves it through the write layer.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_authoring, sheet_editing, write_layer
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


def _wire(p1, p2):
    return {"p1": list(p1), "p2": list(p2), "route": "direct", "snap": False}


def _add_free_text(sch: Path, text: str, x: float, y: float, uuid: str) -> None:
    """A free text object, built from a label's effects, saved through the write layer."""
    root = write_layer.load(sch)
    effects = next(c for c in root.children if not c.is_atom and c.name == "label").find_children("effects")[0]
    root.children.append(SExp.list(
        "text", write_layer.string(text), SExp.list("exclude_from_sim", write_layer.symbol("no")),
        SExp.list("at", write_layer.number(x), write_layer.number(y), write_layer.number(0)),
        copy.deepcopy(effects), SExp.list("uuid", write_layer.string(uuid))))
    write_layer.save(root, sch)


@pytest.fixture
def sheet(tmp_path, tools) -> Path:
    """R1 and R2 joined by NETA (label on the top segment); GNDX (two labels) joins their other pins;
    a global label GLOB and a free text NOTE on the sheet."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"},
    ])["status"] == "ok"
    assert tools["add_wires"](str(sch), [
        _wire((50.8, 46.99), (50.8, 40.64)), _wire((50.8, 40.64), (76.2, 40.64)), _wire((76.2, 40.64), (76.2, 46.99)),
        _wire((50.8, 54.61), (50.8, 59.69)), _wire((76.2, 54.61), (76.2, 59.69)),
    ])["status"] == "ok"
    assert tools["add_labels"](str(sch), [
        {"kind": "label", "x": 63.5, "y": 40.64, "text": "NETA", "snap": False},
        {"kind": "label", "x": 50.8, "y": 59.69, "text": "GNDX", "snap": False},
        {"kind": "label", "x": 76.2, "y": 59.69, "text": "GNDX", "snap": False},
        {"kind": "global", "x": 120.65, "y": 30.48, "text": "GLOB", "snap": False},
    ])["status"] == "ok"
    _add_free_text(sch, "NOTE", 100.33, 90.17, "11111111-2222-3333-4444-555555555555")
    return sch


def _nodes(sch: Path, tag: str):
    return [c for c in write_layer.load(sch).children if not c.is_atom and c.name == tag]


def _uuid_of(sch: Path, tag: str, text: str, nth: int = 0) -> str:
    return [sheet_editing._uuid(n) for n in _nodes(sch, tag) if n.get_string(0) == text][nth]


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _nets(sch: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    return sheet_editing.read_nets(tree, sch, None)


def _serial(node) -> str:
    return write_layer.serialise(node, compact=True)


def _others(sch: Path, skip_uuid: str) -> list[str]:
    return [_serial(c) for c in write_layer.load(sch).children if c.is_atom or sheet_editing._uuid(c) != skip_uuid]


# --------------------------------------------------------------------------- free text


def test_a_free_text_change_keeps_everything_but_the_text(sheet, tools):
    uid = "11111111-2222-3333-4444-555555555555"
    before = _nodes(sheet, "text")[0]
    others = _others(sheet, uid)
    result = tools["set_text"](str(sheet), [{"kind": "text", "old_text": "NOTE", "text": "Hello\nWorld"}])
    assert result["ok"] is True and result["written"] is True, result
    assert result["changed"] == [{"uuid": uid, "kind": "text", "from": "NOTE", "to": "Hello\nWorld", "at": [100.33, 90.17]}]
    assert "connectivity" not in result  # a pure free-text change never reads a netlist
    after = _nodes(sheet, "text")[0]
    assert after.get_string(0) == "Hello\nWorld"
    assert [_serial(c) for c in after.children[1:]] == [_serial(c) for c in before.children[1:]]
    assert _others(sheet, uid) == others


def test_a_free_text_change_is_picked_by_uuid(sheet, tools):
    uid = "11111111-2222-3333-4444-555555555555"
    assert tools["set_text"](str(sheet), [{"uuid": uid, "text": "By uuid"}])["written"] is True
    assert _nodes(sheet, "text")[0].get_string(0) == "By uuid"


def test_dry_run_writes_nothing(sheet, tools):
    before = _bytes(sheet)
    result = tools["set_text"](str(sheet), [{"kind": "text", "old_text": "NOTE", "text": "X"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and result["would_change"][0]["to"] == "X", result
    assert _bytes(sheet) == before


def test_an_unchanged_text_writes_nothing(sheet, tools):
    before = _bytes(sheet)
    result = tools["set_text"](str(sheet), [{"kind": "text", "old_text": "NOTE", "text": "NOTE"}])
    assert result["ok"] is True and result["written"] is False and result["unchanged"] == 1, result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- labels


def test_renaming_a_net_label_is_refused_as_a_connectivity_change(sheet, tools):
    before = _bytes(sheet)
    result = tools["set_text"](str(sheet), [{"kind": "label", "old_text": "NETA", "text": "NETB"}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert result["connectivity"]["changed"] is True
    assert {"label_renamed"} <= {c["type"] for c in result["connectivity"]["changes"]["items"]}
    assert {"renamed"} <= {c["type"] for c in result["connectivity"]["changes"]["items"]}  # the netlist itself shows it
    assert _bytes(sheet) == before


def test_renaming_a_net_label_with_the_flag_keeps_uuid_position_and_effects(sheet, tools):
    uid = _uuid_of(sheet, "label", "NETA")
    before = _nodes(sheet, "label")[0]
    others = _others(sheet, uid)
    result = tools["set_text"](str(sheet), [{"uuid": uid, "text": "NETB"}], allow_connectivity_change=True)
    assert result["ok"] is True and result["written"] is True, result
    assert result["connectivity"]["changed"] is True
    after = next(n for n in _nodes(sheet, "label") if sheet_editing._uuid(n) == uid)
    assert after.get_string(0) == "NETB"
    assert [_serial(c) for c in after.children[1:]] == [_serial(c) for c in before.children[1:]]
    assert _others(sheet, uid) == others
    nets = _nets(sheet)
    assert "/NETB" in nets and "/NETA" not in nets


def test_a_global_label_is_renamed_and_its_shape_kept(sheet, tools):
    uid = _uuid_of(sheet, "global_label", "GLOB")
    before = _nodes(sheet, "global_label")[0]
    result = tools["set_text"](str(sheet), [{"kind": "global", "old_text": "GLOB", "text": "GLOB2"}], allow_connectivity_change=True)
    assert result["ok"] is True and result["written"] is True, result
    after = _nodes(sheet, "global_label")[0]
    assert sheet_editing._uuid(after) == uid and after.get_string(0) == "GLOB2"
    assert [_serial(c) for c in after.children[1:]] == [_serial(c) for c in before.children[1:]]


def test_a_point_tells_apart_two_labels_of_one_text(sheet, tools):
    result = tools["set_text"](str(sheet), [{"kind": "label", "old_text": "GNDX", "point": [76.2, 59.69], "text": "GNDY"}],
                               allow_connectivity_change=True, dry_run=True)
    assert result["ok"] is True and result["would_change"][0]["at"] == [76.2, 59.69], result


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize(("items", "code"), [
    ([{"kind": "label", "old_text": "NOPE", "text": "X"}], "not_found"),
    ([{"uuid": "00000000-0000-0000-0000-000000000000", "text": "X"}], "not_found"),
    ([{"kind": "label", "old_text": "GNDX", "point": [1, 1], "text": "X"}], "not_found"),
    ([{"kind": "global", "old_text": "NETA", "text": "X"}], "not_found"),
    ([{"kind": "label", "old_text": "GNDX", "text": "X"}], "ambiguous"),
    ([], "invalid_spec"),
    ("NETA", "invalid_spec"),
    ([{"kind": "label", "old_text": "NETA"}], "invalid_spec"),
    ([{"kind": "label", "text": "X"}], "invalid_spec"),
    ([{"kind": "wire", "old_text": "NETA", "text": "X"}], "invalid_spec"),
    ([{"kind": "label", "old_text": "NETA", "text": "X", "color": "red"}], "invalid_spec"),
    ([{"kind": "label", "old_text": "NETA", "text": 5}], "invalid_spec"),
    ([{"kind": "label", "old_text": "NETA", "text": "A\nB"}], "invalid_spec"),
    ([{"kind": "label", "old_text": "NETA", "text": "A\x01B"}], "invalid_spec"),
    ([{"kind": "label", "old_text": "NETA", "point": [1], "text": "X"}], "invalid_spec"),
    ([{"kind": "label", "old_text": "NETA", "text": "X"}, {"kind": "label", "old_text": "NETA", "text": "Y"}], "invalid_spec"),
    ([{"kind": "label", "old_text": "NETA", "text": ""}], "empty_text"),
    ([{"kind": "text", "old_text": "NOTE", "text": "   "}], "empty_text"),
])
def test_a_refusal_names_its_code_and_writes_nothing(sheet, tools, items, code):
    before = _bytes(sheet)
    result = tools["set_text"](str(sheet), items, allow_connectivity_change=True)
    assert result["ok"] is False and result["code"] == code, result
    assert _bytes(sheet) == before


@pytest.mark.parametrize("tag", ["label", "global_label"])
@pytest.mark.parametrize("text", ["A\nB", "A\rB", "A\tB"])
def test_a_line_break_or_tab_in_a_label_is_refused_when_picked_by_uuid_alone(sheet, tools, tag, text):
    before = _bytes(sheet)
    result = tools["set_text"](str(sheet), [{"uuid": _uuid_of(sheet, tag, "NETA" if tag == "label" else "GLOB"), "text": text}],
                               allow_connectivity_change=True)
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _bytes(sheet) == before


def test_a_tab_in_a_label_is_refused_by_kind_and_free_text_may_hold_one(sheet, tools):
    assert tools["set_text"](str(sheet), [{"kind": "label", "old_text": "NETA", "text": "A\tB"}],
                             allow_connectivity_change=True)["code"] == "invalid_spec"
    assert tools["set_text"](str(sheet), [{"kind": "text", "old_text": "NOTE", "text": "A\tB"}])["written"] is True


def test_leading_or_trailing_whitespace_in_a_label_is_a_listed_warning(sheet, tools):
    uid = _uuid_of(sheet, "label", "NETA")
    result = tools["set_text"](str(sheet), [{"uuid": uid, "text": " NETB "}], allow_connectivity_change=True, dry_run=True)
    assert result["ok"] is True, result
    assert {"code": "label_whitespace", "uuid": uid, "text": " NETB "} in result["warnings"], result
    clean = tools["set_text"](str(sheet), [{"uuid": uid, "text": "NETB"}], allow_connectivity_change=True, dry_run=True)
    assert "warnings" not in clean, clean
    free = tools["set_text"](str(sheet), [{"kind": "text", "old_text": "NOTE", "text": " padded "}], dry_run=True)
    assert "warnings" not in free, free


def test_uuid_with_a_kind_or_old_text_that_does_not_match_is_not_found(sheet, tools):
    label, note = _uuid_of(sheet, "label", "NETA"), "11111111-2222-3333-4444-555555555555"
    before = _bytes(sheet)
    for item in ({"uuid": label, "kind": "text", "text": "X"}, {"uuid": note, "kind": "label", "text": "X"},
                 {"uuid": label, "old_text": "WRONG", "text": "X"}, {"uuid": note, "old_text": "WRONG", "text": "X"}):
        result = tools["set_text"](str(sheet), [item], allow_connectivity_change=True)
        assert result["ok"] is False and result["code"] == "not_found", (item, result)
    assert _bytes(sheet) == before
    assert tools["set_text"](str(sheet), [{"uuid": label, "kind": "label", "old_text": "NETA", "text": "NETB"}],
                             allow_connectivity_change=True, dry_run=True)["ok"] is True


def test_a_batch_with_one_bad_item_writes_none_of_it(sheet, tools):
    before = _bytes(sheet)
    result = tools["set_text"](str(sheet), [{"kind": "text", "old_text": "NOTE", "text": "Fine"},
                                            {"kind": "label", "old_text": "NOPE", "text": "X"}])
    assert result["code"] == "not_found" and result["not_found"], result
    assert _bytes(sheet) == before


def test_ambiguity_lists_the_candidates_with_uuid_and_position(sheet, tools):
    result = tools["set_text"](str(sheet), [{"kind": "label", "old_text": "GNDX", "text": "X"}])
    assert sorted(c["at"] for c in result["candidates"]) == [[50.8, 59.69], [76.2, 59.69]], result


def test_a_missing_sheet_is_refused(tmp_path, tools):
    result = tools["set_text"](str(tmp_path / "none.kicad_sch"), [{"uuid": "x", "text": "y"}])
    assert result["ok"] is False and result["code"] == "file_not_found", result


# --------------------------------------------------------------------------- hierarchical labels


@pytest.fixture
def hier(tmp_path, tools):
    """A root with a Sensors sheet symbol holding the pin SER, and the child's hierarchical label SER."""
    made = project_toolset._project_create(str(tmp_path / "h"), "demo")
    root = Path(made["schematic_path"])
    assert tools["create_sheets"](str(root), [{"file": "sensors.kicad_sch"}])["status"] == "ok"
    spec = {"file": "sensors.kicad_sch", "name": "Sensors", "x": 101.6, "y": 50.8, "height": 25.4,
            "pins": [{"name": "SER", "direction": "input"}]}
    assert tools["add_sheet_symbols"](str(root), [spec])["status"] == "ok"
    return root, root.parent / "sensors.kicad_sch"


def test_a_hierarchical_label_rename_warns_about_the_parents_pin_and_leaves_it(hier, tools):
    root, child = hier
    root_before = root.read_bytes()
    result = tools["set_text"](str(child), [{"kind": "hier", "old_text": "SER", "text": "SDA"}],
                               allow_connectivity_change=True, root_sch=str(root))
    assert result["ok"] is True and result["written"] is True, result
    assert [w["code"] for w in result["warnings"]] == ["pin_name_mismatch"], result
    warning = result["warnings"][0]
    assert warning["pin"] == "SER" and warning["label"] == "SDA" and warning["sheet_name"] == "Sensors"
    assert _nodes(child, "hierarchical_label")[0].get_string(0) == "SDA"
    assert root.read_bytes() == root_before  # the sheet pin was not touched


def test_a_hierarchical_label_rename_to_the_pins_name_is_no_mismatch(hier, tools):
    root, child = hier
    assert tools["set_text"](str(child), [{"kind": "hier", "old_text": "SER", "text": "SDA"}],
                             allow_connectivity_change=True, root_sch=str(root))["written"] is True
    back = tools["set_text"](str(child), [{"kind": "hier", "old_text": "SDA", "text": "SER"}],
                             allow_connectivity_change=True, root_sch=str(root))
    assert back["ok"] is True and "warnings" not in back, back


def _rename(tools, root, child, uid_or_old, new, kind="hier"):
    item = {"uuid": uid_or_old, "text": new} if "-" in uid_or_old else {"kind": kind, "old_text": uid_or_old, "text": new}
    return tools["set_text"](str(child), [item], allow_connectivity_change=True, root_sch=str(root))


def test_no_warning_while_the_child_keeps_another_hierarchical_label_of_the_old_name(hier, tools):
    root, child = hier
    tree = write_layer.load(child)
    first = next(c for c in tree.children if not c.is_atom and c.name == "hierarchical_label")
    twin = copy.deepcopy(first)
    twin.find_children("uuid")[0].children[0] = write_layer.string("99999999-8888-7777-6666-555555555555")
    twin.find_children("at")[0].children[2] = write_layer.number(10)
    tree.children.append(twin)
    write_layer.save(tree, child)
    result = _rename(tools, root, child, sheet_editing._uuid(first), "SDA")
    assert result["ok"] is True and "warnings" not in result, result


def test_no_warning_when_the_new_name_is_a_pin_the_parent_already_has(hier, tools):
    root, child = hier
    assert tools["add_sheet_pins"](str(root), [{"sheet": "Sensors", "pins": [{"name": "OE_N", "direction": "output"}]}])["ok"] is True
    result = _rename(tools, root, child, "SER", "OE_N")
    assert result["ok"] is True and "warnings" not in result, result


def test_no_warning_when_the_parent_has_no_pin_of_the_old_name(hier, tools):
    root, child = hier
    assert tools["remove_sheet_pins"](str(root), [{"sheet": "Sensors", "pins": ["SER"]}])["ok"] is True
    result = _rename(tools, root, child, "SER", "SDA")
    assert result["ok"] is True and "warnings" not in result, result


def test_renaming_a_local_label_that_shares_a_pins_name_gives_no_warning(hier, tools):
    root, child = hier
    assert _rename(tools, root, child, "SER", "SDA")["warnings"]  # the hierarchical label is gone from the old name
    assert tools["add_labels"](str(child), [{"kind": "label", "x": 25.4, "y": 25.4, "text": "SER"}])["status"] == "ok"
    result = _rename(tools, root, child, "SER", "ZZ", kind="label")
    assert result["ok"] is True and "warnings" not in result, result


# --------------------------------------------------------------------------- self-checks


def test_a_corrupted_write_is_refused_and_leaves_the_sheet_alone(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _tools()["set_text"](str(sheet), [{"kind": "text", "old_text": "NOTE", "text": "X"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sheet) == before
    assert not [p for p in sheet.parent.iterdir() if p.name.endswith(".tmp")]


def test_the_staged_check_fires_when_the_change_touches_more_than_it_selected(sheet, monkeypatch):
    """A set_string that also renames a symbol's first property is caught before anything is written."""
    real_set = write_layer.set_string
    real_nodes = sheet_editing._text_nodes
    seen: dict = {}

    def nodes(s):
        seen["root"] = s.root
        return real_nodes(s)

    def greedy(node, index, value):
        real_set(node, index, value)
        symbol = next(c for c in seen["root"].children if not c.is_atom and c.name == "symbol")
        real_set(symbol.find_children("property")[0], 1, "STRAY")

    before = _bytes(sheet)
    monkeypatch.setattr(sheet_editing, "_text_nodes", nodes)
    monkeypatch.setattr(write_layer, "set_string", greedy)
    result = _tools()["set_text"](str(sheet), [{"kind": "text", "old_text": "NOTE", "text": "X"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert _bytes(sheet) == before


def test_the_connectivity_guard_fires_even_when_the_netlist_sees_no_rename(sheet, tools, monkeypatch):
    """A label rename is listed as label_renamed on its own, so a netlist that shows no change cannot let it through."""
    monkeypatch.setattr(sheet_editing, "compare_nets", lambda *a, **k: [])
    before = _bytes(sheet)
    result = tools["set_text"](str(sheet), [{"kind": "label", "old_text": "NETA", "text": "NETB"}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert [c["type"] for c in result["connectivity"]["changes"]["items"]] == ["label_renamed"]
    assert _bytes(sheet) == before
