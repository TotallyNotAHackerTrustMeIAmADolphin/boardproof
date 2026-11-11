"""add_symbols and add_labels (power) never report a clean ok about instance data they could not prove (GitHub issue 187).

Reproduction before the fix: attach a sheet below a root, break the project's sheet tree (a sheet file the root names is
gone), run ``add_symbols``: the call said ``ok`` and wrote the new symbols keyed on the sheet's own uuid, which KiCad
treats as stale once a parent uses the sheet; ``_reattach_to_tree`` had returned the text unchanged without a word.

Design: where the sheet is part of a project whose tree cannot prove where it is used, the call is refused
(``unproven_instance_data``, the pattern of replicate_group and rename_ref) and nothing is written; where the own-uuid
path is the right one (root sheet, no project, a sheet no sheet symbol uses yet) the call writes and says so in
``instance_data``. Fixtures are built through the tools and the write layer on ``tmp_path``.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_authoring, sheet_tree, write_layer
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset

PIN = [{"name": "A", "direction": "input"}]
R = [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R3", "value": "3k"}]
GND = [{"kind": "power", "text": "power:GND", "x": 101.6, "y": 76.2}]
LABEL = [{"kind": "label", "text": "NET1", "x": 101.6, "y": 76.2}]


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


def _paths(sheet: Path, ref: str) -> list[str]:
    for symbol in write_layer.load(sheet).children:
        if symbol.is_atom or symbol.name != "symbol":
            continue
        if [p for p in symbol.find_children("property") if p.get_string(0) == "Reference"][0].get_string(1) == ref:
            return [p.get_string(0) for i in symbol.find_children("instances") for pr in i.find_children("project")
                    for p in pr.find_children("path")]
    raise AssertionError(f"{ref} not on {sheet.name}")


def _own(sheet: Path) -> str:
    return "/" + write_layer.load(sheet).find_children("uuid")[0].get_string(0)


@pytest.fixture
def project(tmp_path) -> tuple[Path, Path]:
    """A root with sub-sheets a and b, a attached, b not attached, no symbols."""
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    assert sheet_authoring.create_sheets(str(root), [{"file": "a.kicad_sch"}, {"file": "b.kicad_sch"}])["status"] == "ok"
    assert sheet_authoring.add_sheet_symbols(
        str(root), [{"file": "a.kicad_sch", "name": "A", "x": 101.6, "y": 50.8, "pins": PIN}])["status"] == "ok"
    return root, root.parent / "a.kicad_sch"


def _broken(project) -> tuple[Path, Path]:
    root, sub = project
    assert sheet_authoring.add_sheet_symbols(
        str(root), [{"file": "b.kicad_sch", "name": "B", "x": 101.6, "y": 101.6, "pins": PIN}])["status"] == "ok"
    (root.parent / "b.kicad_sch").unlink()  # the root names a sheet file that is gone
    assert sheet_tree.walk(root).problems
    return root, sub


# ---------------------------------------------------------------- branch: reattached

def test_an_attached_sheet_is_reattached_and_the_result_says_so(project):
    root, sub = project
    result = _tools()["add_symbols"](str(sub), R)
    assert result["status"] == "ok", result
    assert result["instance_data"]["reattached"] is True and result["instance_data"]["reason"] == "reattached"
    assert _paths(sub, "R3") != [_own(sub)]


def test_a_power_label_on_an_attached_sheet_is_reattached_and_says_so(project):
    root, sub = project
    result = _tools()["add_labels"](str(sub), GND)
    assert result["status"] == "ok", result
    assert result["instance_data"]["reattached"] is True


# ---------------------------------------------------------------- branch: tree with problems

def test_add_symbols_on_a_sheet_in_a_tree_with_problems_is_refused_and_writes_nothing(project):
    root, sub = _broken(project)
    before = sub.read_bytes()
    result = _tools()["add_symbols"](str(sub), R)
    assert result["ok"] is False and result["code"] == "unproven_instance_data", result
    assert result["instance_reason"] == "tree_problems"
    assert "nothing was written" in result["reason"]
    assert sub.read_bytes() == before


def test_a_power_label_on_a_sheet_in_a_tree_with_problems_is_refused(project):
    root, sub = _broken(project)
    before = sub.read_bytes()
    result = _tools()["add_labels"](str(sub), GND)
    assert result["ok"] is False and result["code"] == "unproven_instance_data", result
    assert sub.read_bytes() == before


def test_a_label_without_a_power_symbol_does_not_depend_on_the_tree(project):
    root, sub = _broken(project)
    result = _tools()["add_labels"](str(sub), LABEL)
    assert result["status"] == "ok", result
    assert result["instance_data"] is None  # no instance data written, none claimed


def test_wires_and_junctions_hold_no_instance_data_and_do_not_depend_on_the_tree(project):
    root, sub = _broken(project)
    assert _tools()["add_wires"](str(sub), [{"p1": [50.8, 50.8], "p2": [76.2, 50.8]}])["status"] == "ok"
    assert _tools()["add_junctions"](str(sub), [[76.2, 50.8]])["status"] == "ok"


def test_a_sheet_in_a_subfolder_whose_project_does_not_read_whole_is_refused(tmp_path):
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    (root.parent / "sub").mkdir()
    made = sheet_authoring.create_sheets(str(root), [{"file": "sub/a.kicad_sch"}, {"file": "b.kicad_sch"}])
    assert made["status"] == "ok", made
    sub = root.parent / "sub" / "a.kicad_sch"
    for spec in ({"file": "sub/a.kicad_sch", "name": "A", "x": 101.6, "y": 50.8},
                 {"file": "b.kicad_sch", "name": "B", "x": 101.6, "y": 101.6}):
        assert sheet_authoring.add_sheet_symbols(str(root), [{**spec, "pins": PIN}])["status"] == "ok"
    (root.parent / "b.kicad_sch").unlink()
    before = sub.read_bytes()
    result = _tools()["add_symbols"](str(sub), R)
    assert result["ok"] is False and result["code"] == "unproven_instance_data", result
    assert result["instance_reason"] == "tree_problems"
    assert sub.read_bytes() == before


# ---------------------------------------------------------------- branch: no project / not attached / root

def test_a_lone_sheet_with_no_project_is_written_with_an_explicit_note(project, tmp_path):
    root, sub = project
    lone = tmp_path / "lone" / "a.kicad_sch"
    lone.parent.mkdir()
    shutil.copy(sub, lone)
    result = _tools()["add_symbols"](str(lone), R)
    assert result["status"] == "ok", result
    assert result["instance_data"]["reattached"] is False and result["instance_data"]["reason"] == "no_project"
    assert "add_sheet_symbols" in result["instance_data"]["note"]
    assert _paths(lone, "R3") == [_own(lone)]


def test_a_sheet_no_sheet_symbol_uses_keeps_its_own_path_and_says_not_attached(project):
    root, sub = project
    unattached = root.parent / "b.kicad_sch"
    result = _tools()["add_symbols"](str(unattached), R)
    assert result["status"] == "ok", result
    assert result["instance_data"]["reattached"] is False and result["instance_data"]["reason"] == "not_attached"
    assert _paths(unattached, "R3") == [_own(unattached)]


def test_the_root_sheet_keeps_its_own_path_even_when_the_tree_has_problems(project):
    root, sub = _broken(project)
    result = _tools()["add_symbols"](str(root), R)
    assert result["status"] == "ok", result
    assert result["instance_data"]["reason"] == "root_sheet"


# ---------------------------------------------------------------- branch: failures of the lookup and the walk

def test_an_exception_in_the_walk_of_a_project_is_a_named_refusal_not_a_silent_ok(project, monkeypatch):
    root, sub = project

    def boom(*_a, **_k):
        raise RuntimeError("walk exploded")

    monkeypatch.setattr(sheet_tree, "walk", boom)
    before = sub.read_bytes()
    result = _tools()["add_symbols"](str(sub), R)
    assert result["ok"] is False and result["code"] == "unproven_instance_data", result
    assert result["instance_reason"] == "tree_problems" and "walk exploded" in result["reason"]
    assert sub.read_bytes() == before


def test_an_exception_in_the_project_lookup_is_a_named_refusal(project, monkeypatch):
    root, sub = project

    def boom(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(sheet_authoring, "discover_projects", boom)
    before = sub.read_bytes()
    result = _tools()["add_symbols"](str(sub), R)
    assert result["ok"] is False and result["instance_reason"] == "walk_failed" and "disk gone" in result["reason"], result
    assert sub.read_bytes() == before


def test_a_sheet_held_by_two_projects_is_refused_as_ambiguous(tmp_path):
    outer = Path(project_toolset._project_create(str(tmp_path / "outer"), "outer")["schematic_path"])
    inner = Path(project_toolset._project_create(str(tmp_path / "outer" / "inner"), "inner")["schematic_path"])
    shared = inner.parent / "s.kicad_sch"
    assert sheet_authoring.create_sheets(str(outer), [{"file": "inner/s.kicad_sch"}])["status"] == "ok"
    for root, name in ((outer, "inner/s.kicad_sch"), (inner, "s.kicad_sch")):
        done = sheet_authoring.add_sheet_symbols(str(root), [{"file": name, "name": "S", "x": 101.6, "y": 50.8, "pins": PIN}])
        assert done["status"] == "ok", done
    before = shared.read_bytes()
    result = _tools()["add_symbols"](str(shared), R)
    assert result["ok"] is False and result["code"] == "unproven_instance_data", result
    assert result["instance_reason"] == "root_not_found"
    assert shared.read_bytes() == before


def _sub_with_own_project(tmp_path) -> tuple[Path, Path]:
    """Root demo in the project folder, sub/a.kicad_sch attached to it, and sub/a.kicad_pro beside the sub-sheet (what
    KiCad leaves when the sub-sheet is opened on its own)."""
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    (root.parent / "sub").mkdir()
    assert sheet_authoring.create_sheets(str(root), [{"file": "sub/a.kicad_sch"}])["status"] == "ok"
    done = sheet_authoring.add_sheet_symbols(str(root), [{"file": "sub/a.kicad_sch", "name": "A", "x": 101.6, "y": 50.8, "pins": PIN}])
    assert done["status"] == "ok", done
    sub = root.parent / "sub" / "a.kicad_sch"
    shutil.copy(root.with_suffix(".kicad_pro"), sub.with_suffix(".kicad_pro"))
    return root, sub


def test_a_project_file_beside_an_attached_sub_sheet_does_not_hide_the_parent_project(tmp_path):
    root, sub = _sub_with_own_project(tmp_path)
    result = _tools()["add_symbols"](str(sub), R)
    assert result["status"] == "ok", result
    assert result["instance_data"]["reattached"] is True, result["instance_data"]
    (use,) = [u for f, u in sheet_tree.walk(root).uses if f == sub.resolve()]
    assert _paths(sub, "R3") == [use]


def test_a_project_file_beside_an_attached_sub_sheet_does_not_hide_the_parent_for_a_power_label(tmp_path):
    root, sub = _sub_with_own_project(tmp_path)
    result = _tools()["add_labels"](str(sub), GND)
    assert result["status"] == "ok", result
    assert result["instance_data"]["reattached"] is True, result["instance_data"]


def test_a_project_file_whose_root_sheet_is_missing_counts_as_no_project(project, tmp_path):
    root, sub = project
    lone = tmp_path / "lone" / "a.kicad_sch"
    lone.parent.mkdir()
    shutil.copy(sub, lone)
    shutil.copy(root.with_suffix(".kicad_pro"), lone.parent / "ghost.kicad_pro")  # no ghost.kicad_sch
    result = _tools()["add_symbols"](str(lone), R)
    assert result["status"] == "ok" and result["instance_data"]["reason"] == "no_project", result


def test_a_sheet_used_twice_gets_one_path_per_use_and_the_note_counts_them(project):
    root, sub = project
    from tests.test_sheet_authoring import _use_a_second_time  # a second sheet symbol of the file, through the write layer

    _use_a_second_time(root, "A2")
    uses = [u for f, u in sheet_tree.walk(root).uses if f == sub.resolve()]
    assert len(uses) == 2
    result = _tools()["add_symbols"](str(sub), R)
    assert result["status"] == "ok", result
    assert "2 use path(s)" in result["instance_data"]["note"]
    assert sorted(_paths(sub, "R3")) == sorted(uses)


def test_a_tree_that_holds_the_sheet_but_names_no_use_path_is_refused(project, monkeypatch):
    root, sub = project
    tree = sheet_tree.walk(root)
    assert sub.resolve() in {f.resolve() for f in tree.files}
    tree = dataclasses.replace(tree, uses=[(f, u) for f, u in tree.uses if f != sub.resolve()])  # the file is in the tree, no use of it is named
    real = sheet_authoring.discover_projects

    def fake(path):
        found = real(path)
        return {**found, "contains": [(root, tree)], "excluded": [], "unreadable": []}

    monkeypatch.setattr(sheet_authoring, "discover_projects", fake)
    before = sub.read_bytes()
    result = _tools()["add_symbols"](str(sub), R)
    assert result["ok"] is False and result["code"] == "unproven_instance_data", result
    assert result["instance_reason"] == "tree_problems" and "no use path" in result["reason"]
    assert sub.read_bytes() == before


def test_an_unrelated_project_with_an_unreadable_tree_above_a_lone_sheet_refuses(project, tmp_path):
    root, sub = project
    broken = tmp_path / "up"
    inner = broken / "x" / "y"
    inner.mkdir(parents=True)
    made = Path(project_toolset._project_create(str(broken / "other"), "other")["schematic_path"])
    shutil.copy(made.with_suffix(".kicad_pro"), broken / "up.kicad_pro")  # a project file above ...
    shutil.copy(made, broken / "up.kicad_sch")
    assert sheet_authoring.create_sheets(str(broken / "up.kicad_sch"), [{"file": "gone.kicad_sch"}])["status"] == "ok"
    assert sheet_authoring.add_sheet_symbols(
        str(broken / "up.kicad_sch"), [{"file": "gone.kicad_sch", "name": "G", "x": 101.6, "y": 50.8, "pins": PIN}])["status"] == "ok"
    (broken / "gone.kicad_sch").unlink()  # ... whose tree no longer reads
    lone = inner / "a.kicad_sch"
    shutil.copy(sub, lone)
    before = lone.read_bytes()
    result = _tools()["add_symbols"](str(lone), R)
    assert result["ok"] is False and result["instance_reason"] == "tree_problems", result
    assert lone.read_bytes() == before


def test_a_stray_project_file_at_a_mid_level_makes_two_parents_and_is_refused(tmp_path):
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    (root.parent / "mid").mkdir()
    mid = root.parent / "mid" / "b.kicad_sch"
    assert sheet_authoring.create_sheets(str(root), [{"file": "mid/b.kicad_sch"}])["status"] == "ok"
    assert sheet_authoring.add_sheet_symbols(str(root), [{"file": "mid/b.kicad_sch", "name": "B", "x": 101.6, "y": 50.8, "pins": PIN}])["status"] == "ok"
    assert sheet_authoring.create_sheets(str(mid), [{"file": "a.kicad_sch"}])["status"] == "ok"
    done = sheet_authoring.add_sheet_symbols(str(mid), [{"file": "a.kicad_sch", "name": "A", "x": 101.6, "y": 50.8, "pins": PIN}])
    assert done["status"] == "ok", done
    leaf = mid.parent / "a.kicad_sch"
    shutil.copy(root.with_suffix(".kicad_pro"), mid.with_suffix(".kicad_pro"))  # the stray b.kicad_pro
    before = leaf.read_bytes()
    result = _tools()["add_symbols"](str(leaf), R)
    assert result["ok"] is False and result["instance_reason"] == "root_not_found", result
    assert leaf.read_bytes() == before


def test_the_tool_texts_state_the_refusal_and_the_result_field():
    tools = _tools()
    for name in ("add_symbols", "add_labels"):
        doc = tools[name].__doc__
        assert "unproven_instance_data" in doc and "instance_data" in doc, name
    assert "unrelated project" in tools["add_symbols"].__doc__
    assert "no_project" in tools["add_symbols"].__doc__ and "not_attached" in tools["add_symbols"].__doc__
