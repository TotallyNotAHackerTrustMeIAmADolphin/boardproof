"""add_symbols failed with write_not_verified on a sheet that holds stale instance data (GitHub issue 107).

Reproduction (a downstream two-sheet design, KiCad 10.0.7): ``add_symbols`` of one resistor on an
attached sub-sheet refused with "7 node(s) of <sheet> would not survive the write: symbol:<uuid>, ...".
Cause, read from the raw S-expressions of that sheet: eight of its symbols carried
``(instances (project .. (path "/<the sheet's own uuid>" ..)))`` although the sheet is used below the root
(placed while the sheet was alone, or copied in by hand). ``add_symbols`` re-keys instance data from the
sheet's own uuid to the use path(s) (issue 79) and did so for the symbols that were already there, so
the "tools only add" token check (build ticket 38) saw those symbols changed and refused. The fix re-keys
only the symbols the call adds, as the tool text says; the refusals name check, subjects, expected and actual.

Fixtures are built through the tools and the write layer on ``tmp_path`` (never a raw text edit).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_authoring, sheet_tree, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

PIN = [{"name": "A", "direction": "input"}]


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def _tools():
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _spec(file: str, name: str, y: float) -> dict:
    return {"file": file, "name": name, "x": 101.6, "y": y, "pins": PIN}


def _symbols(sheet: Path) -> list:
    return [c for c in write_layer.load(sheet).children if not c.is_atom and c.name == "symbol"]


def _instance_paths(sheet: Path) -> dict[str, list[str]]:
    found = {}
    for symbol in _symbols(sheet):
        ref = [p for p in symbol.find_children("property") if p.get_string(0) == "Reference"][0].get_string(1)
        found[ref] = [path.get_string(0) for inst in symbol.find_children("instances")
                      for proj in inst.find_children("project") for path in proj.find_children("path")]
    return found


@pytest.fixture
def stale_tree(tmp_path) -> tuple[Path, Path, dict]:
    """A root and two attached sub-sheets; ``a.kicad_sch`` holds R1, R2 whose instance data is keyed on
    the sheet's own uuid again (what the downstream sheet had). Returns (root, sub, own-path entries)."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    root = Path(made["schematic_path"])
    assert sheet_authoring.create_sheets(str(root), [{"file": "a.kicad_sch"}, {"file": "b.kicad_sch"}])["status"] == "ok"
    sub = root.parent / "a.kicad_sch"
    placed = _tools()["add_symbols"](str(sub), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "2k"}])
    assert placed["status"] == "ok", placed
    for file, name, y in (("a.kicad_sch", "A", 50.8), ("b.kicad_sch", "B", 101.6)):
        assert sheet_authoring.add_sheet_symbols(str(root), [_spec(file, name, y)])["status"] == "ok"
    own = "/" + write_layer.load(sub).find_children("uuid")[0].get_string(0)
    tree = sheet_to = write_layer.load(sub)
    for symbol in [c for c in tree.children if not c.is_atom and c.name == "symbol"]:
        for path in [p for i in symbol.find_children("instances") for pr in i.find_children("project")
                     for p in pr.find_children("path")]:
            write_layer.set_string(path, 0, own)
    write_layer.save(sheet_to, sub)
    assert _instance_paths(sub) == {"R1": [own], "R2": [own]}
    return root, sub, {"own": own}


def test_add_symbols_on_an_attached_sheet_with_stale_instance_data_adds_and_leaves_old_symbols_unchanged(stale_tree):
    root, sub, facts = stale_tree
    before = {s.find("uuid").get_string(0): write_layer.serialise(s) for s in _symbols(sub)}
    assert len(before) == 2

    result = _tools()["add_symbols"](
        str(sub), [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R3", "value": "3k"}])

    assert result["status"] == "ok", result
    assert result["stale_instance_symbols"] == ["R1", "R2"]  # F1: the old symbols are not re-keyed, and said so
    after = {s.find("uuid").get_string(0): write_layer.serialise(s) for s in _symbols(sub)}
    assert all(after[uuid] == text for uuid, text in before.items())  # the old symbols are byte-for-byte what they were
    (use,) = [p for f, p in sheet_tree.walk(root).uses if f == sub.resolve() and p != facts["own"]]
    found = _instance_paths(sub)
    assert found["R3"] == [use]  # the new symbol is keyed on the use path
    assert found["R1"] == found["R2"] == [facts["own"]]


def test_the_token_refusal_names_the_check_the_symbols_and_what_was_read_back(stale_tree, monkeypatch):
    """Whatever changes an old symbol during the write, the refusal says which check, which symbol
    (reference and uuid), what was expected and what was read back."""
    root, sub, facts = stale_tree
    uuid = _symbols(sub)[0].find("uuid").get_string(0)
    original = schematic_toolset._schematic_text

    def corrupted(sch):
        return original(sch).replace(facts["own"], "/00000000-0000-4000-8000-000000000107", 1)

    monkeypatch.setattr(schematic_toolset, "_schematic_text", corrupted)
    before = sub.read_bytes()

    result = _tools()["add_symbols"](
        str(sub), [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R3", "value": "3k"}])

    assert result["ok"] is False and result["error"] == "write_not_verified", result
    (item,) = result["mismatches"]["items"]
    assert item["check"] == "top_level_nodes_unchanged"
    assert item["file"] == "a.kicad_sch"
    assert item["subject"].startswith("symbol R1") and uuid in item["subject"]
    assert "unchanged" in item["expected"]
    assert "changed in place" in item["actual"] and "instances" in item["actual"]
    assert facts["own"] in item["actual"] or "00000000-0000-4000-8000-000000000107" in item["actual"]
    for word in ("R1", "instances", "changed in place"):
        assert word in result["reason"]
    assert sub.read_bytes() == before


def test_a_removed_node_is_reported_as_absent(stale_tree, monkeypatch):
    root, sub, _ = stale_tree
    uuid = _symbols(sub)[1].find("uuid").get_string(0)
    original = schematic_toolset._schematic_text

    def corrupted(sch):
        return original(sch).replace(f'(uuid "{uuid}")', '(uuid "00000000-0000-4000-8000-000000000108")', 1)

    monkeypatch.setattr(schematic_toolset, "_schematic_text", corrupted)
    result = _tools()["add_symbols"](
        str(sub), [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R3", "value": "3k"}])
    assert result["error"] == "write_not_verified"
    (item,) = result["mismatches"]["items"]
    assert item["subject"].startswith("symbol R2") and uuid in item["subject"]
    assert "absent" in item["actual"] or "changed in place" in item["actual"]


def test_the_reload_check_of_the_sheet_tools_names_the_node_it_lost(tmp_path, monkeypatch):
    """create_sheets' reload self-check, fed a write that lost the last node, names the check, the
    node and the direction (expected as written, not read back)."""
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    root = Path(made["schematic_path"])
    real = write_layer.load

    def corrupt(path):
        loaded = real(path)
        if ".tmp" in Path(path).name:
            loaded.children.pop()
        return loaded

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = sheet_authoring.create_sheets(str(root), [{"file": "a.kicad_sch"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert result["check"] == "reload_equals_serialised"
    assert "reload_equals_serialised" in result["reason"] and "not read back" in result["reason"]
    assert "a.kicad_sch" in result["reason"]


def test_add_symbols_lists_no_stale_symbols_on_a_clean_sheet(tmp_path):
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    root = Path(made["schematic_path"])
    result = _tools()["add_symbols"](str(root), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"}])
    assert result["status"] == "ok" and result["stale_instance_symbols"] == []


def test_a_twice_used_sheet_gives_the_new_symbol_two_paths_and_leaves_stale_old_symbols_identical(stale_tree):
    from tests.test_sheet_authoring import _use_a_second_time

    root, sub, facts = stale_tree
    _use_a_second_time(root, "A2")
    uses = [p for f, p in sheet_tree.walk(root).uses if f == sub.resolve() and p != facts["own"]]
    assert len(uses) == 2
    before = {s.find("uuid").get_string(0): write_layer.serialise(s) for s in _symbols(sub)}

    result = _tools()["add_symbols"](
        str(sub), [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R3", "value": "3k"}])

    assert result["status"] == "ok", result
    assert result["stale_instance_symbols"] == ["R1", "R2"]
    after = {s.find("uuid").get_string(0): write_layer.serialise(s) for s in _symbols(sub)}
    assert all(after[u] == t for u, t in before.items())
    found = _instance_paths(sub)
    assert sorted(found["R3"]) == sorted(uses)
    assert found["R1"] == found["R2"] == [facts["own"]]


# ---- the sheet_editing reload checks (B1): a write that does not read back names check and node


@pytest.fixture
def labelled_sheet(tmp_path) -> Path:
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    root = Path(made["schematic_path"])
    placed = _tools()["add_labels"](str(root), [{"kind": "label", "text": "NETX", "x": 50.8, "y": 50.8}])
    assert placed["status"] == "ok", placed
    return root


def _delete_label(sheet: Path) -> dict:
    return _tools()["delete_labels"](str(sheet), [{"texts": ["NETX"]}], allow_connectivity_change=True)


def test_sheet_editing_verify_callback_names_the_check_and_the_node(labelled_sheet, monkeypatch):
    """`_expected_verify`: the temporary file read back is not the tree that was serialised."""
    real = write_layer.load

    def corrupt(path):
        loaded = real(path)
        # the temporary sibling of the real sheet only: the netlist check saves scratch copies elsewhere
        if Path(path).parent == labelled_sheet.parent and ".tmp" in Path(path).name and loaded.children:
            loaded.children.pop()
        return loaded

    monkeypatch.setattr(write_layer, "load", corrupt)
    before = labelled_sheet.read_bytes()
    result = _delete_label(labelled_sheet)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert result["check"] == "reload_equals_serialised"
    assert "did not reload as the edited tree" in result["reason"] and "not read back" in result["reason"]
    assert labelled_sheet.read_bytes() == before


def test_sheet_editing_final_reload_names_the_check_and_the_node(labelled_sheet, monkeypatch):
    """`_run`'s own read-back of the replaced file (the temporary file verified fine)."""
    real_load, real_save, armed = write_layer.load, write_layer.save, []

    def save(root, path, **kw):
        out = real_save(root, path, **kw)
        if Path(path) == labelled_sheet:  # not the scratch copies of the connectivity check
            armed.append(True)
        return out

    def corrupt(path):
        loaded = real_load(path)
        if armed and Path(path) == labelled_sheet and loaded.children:
            loaded.children.pop()
        return loaded

    monkeypatch.setattr(write_layer, "save", save)
    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _delete_label(labelled_sheet)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert result["check"] == "reload_equals_serialised"
    assert "does not read back as the edited tree" in result["reason"] and "not read back" in result["reason"]


def test_only_the_symbols_still_on_the_own_uuid_path_are_listed_as_stale(stale_tree):
    root, sub, facts = stale_tree
    (use,) = [p for f, p in sheet_tree.walk(root).uses if f == sub.resolve() and p != facts["own"]]
    tree = write_layer.load(sub)
    r2 = [s for s in tree.children if not s.is_atom and s.name == "symbol"
          and [q for q in s.find_children("property") if q.get_string(0) == "Reference"][0].get_string(1) == "R2"][0]
    for path in [p for i in r2.find_children("instances") for pr in i.find_children("project") for p in pr.find_children("path")]:
        write_layer.set_string(path, 0, use)  # R2 is already keyed on the use path
    write_layer.save(tree, sub)
    assert _instance_paths(sub) == {"R1": [facts["own"]], "R2": [use]}

    result = _tools()["add_symbols"](
        str(sub), [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R3", "value": "3k"}])

    assert result["status"] == "ok", result
    assert result["stale_instance_symbols"] == ["R1"]


def test_an_unreadable_sheet_tree_lists_nothing_as_stale(stale_tree):
    root, sub, _ = stale_tree
    old = {s.find("uuid").get_string(0) for s in _symbols(sub)}
    assert schematic_toolset._stale_instance_refs(sub, old) == ["R1", "R2"]
    (root.parent / "b.kicad_sch").unlink()  # the root names a sheet file that is gone: the tree has problems
    assert sheet_tree.walk(root).problems
    assert schematic_toolset._stale_instance_refs(sub, old) == []
