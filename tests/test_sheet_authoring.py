"""create_sheets and add_sheet_symbols (GitHub issue 75).

Reproduction recorded with kicad-cli 10.0.7 on a project made by project_create and on the
committed simple_led project:

* a blank sheet written by the layer upgrades without change (version 20260306, the format
  ``kicad-cli sch upgrade`` writes);
* after the insert, ``kicad-cli sch erc`` shows, per sheet pin and per new hierarchical label, one
  ``pin_not_connected`` and one ``label_dangling`` (nothing is wired to the interface yet), and no
  ``hier_label_mismatch``; a renamed label does show ``hier_label_mismatch``;
* ``kicad-cli sch export netlist`` lists the sheet as ``/<Name>/``.
"""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_authoring, sheet_tree, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

FIXTURES = Path(__file__).parent / "fixtures"

PINS = [
    {"name": "SER", "direction": "input"},
    {"name": "SRCLK", "direction": "input"},
    {"name": "Q", "direction": "output"},
    {"name": "VCC", "direction": "passive", "side": "top", "offset": 5.08},
]


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture
def blank(tmp_path) -> Path:
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    assert made["status"] == "created", made
    return Path(made["schematic_path"])


@pytest.fixture
def led(tmp_path) -> Path:
    """The committed simple_led project (R1, D1, J1) as a folder."""
    dest = tmp_path / "led"
    dest.mkdir()
    for suffix in (".kicad_sch", ".kicad_pro"):
        shutil.copy(FIXTURES / f"simple_led{suffix}", dest / f"simple_led{suffix}")
    return dest / "simple_led.kicad_sch"


def _spec(**over):
    spec = {"file": "sensors.kicad_sch", "name": "Sensors", "x": 101.6, "y": 50.8, "pins": PINS}
    spec.update(over)
    return spec


def _tree(path: Path) -> list[str]:
    return [p.name for p in sorted(path.parent.iterdir())]


def _text(path: Path) -> str:
    return path.read_bytes().decode("utf-8")


def _node(parent, tag):
    return [c for c in parent.children if not c.is_atom and c.name == tag]


def _erc(root: Path, tmp_path: Path) -> Counter:
    copy = tmp_path / "erc_copy"
    shutil.rmtree(copy, ignore_errors=True)
    shutil.copytree(root.parent, copy)
    return sheet_authoring.erc_census(copy / root.name)


# --------------------------------------------------------------------------- create_sheets


def test_create_sheets_writes_a_blank_kicad_10_sheet(blank):
    result = sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch", "title": "Sensors"}])
    assert result["status"] == "ok", result
    assert result["created"] == ["sensors.kicad_sch"] and result["awaiting_reference"] == ["sensors.kicad_sch"]
    root = write_layer.load(blank.parent / "sensors.kicad_sch")
    assert root.name == "kicad_sch"
    assert root.find_children("version")[0].get_int(0) == 20260306
    assert root.find_children("generator")[0].get_string(0) == "eeschema"
    assert root.find_children("generator_version")[0].get_string(0) == "10.0"
    assert root.find_children("uuid") and root.find_children("lib_symbols")
    assert not [c for c in root.children if not c.is_atom and c.name in ("symbol", "wire", "sheet")]
    # KiCad itself loads it, and reads it as the same format (the layer's parse check ran too)
    copy = blank.parent / "scratch_copy.kicad_sch"
    shutil.copyfile(blank.parent / "sensors.kicad_sch", copy)
    assert kicad_cli.upgrade_schematic(copy).returncode == 0
    assert write_layer.load(copy).find_children("version")[0].get_int(0) == 20260306
    tree = sheet_tree.walk(blank)
    assert tree.problems == () and tree.unreferenced == ("scratch_copy.kicad_sch", "sensors.kicad_sch")


def test_create_sheets_is_a_batch_and_reports_the_tree(blank):
    result = sheet_authoring.create_sheets(str(blank), [{"file": "a.kicad_sch"}, {"file": "b.kicad_sch"}])
    assert result["status"] == "ok" and result["count"] == 2, result
    assert result["unreferenced_sheets"] == ["a.kicad_sch", "b.kicad_sch"] and result["sheet_problems"] == []


@pytest.mark.parametrize(
    ("spec", "code"),
    [
        ({"file": "demo.kicad_sch"}, "sheet_exists"),
        ({"file": "../outside.kicad_sch"}, "sheet_outside_project"),
        ({"file": "/abs.kicad_sch"}, "invalid_spec"),
        ({"file": "sub\\x.kicad_sch"}, "invalid_spec"),
        ({"file": "notasheet.txt"}, "invalid_spec"),
        ({"file": "nodir/x.kicad_sch"}, "invalid_spec"),
        ({"file": "x.kicad_sch", "title": "line\nbreak"}, "invalid_spec"),
        ({"file": "x.kicad_sch", "extra": 1}, "invalid_spec"),
    ],
)
def test_create_sheets_refuses_and_writes_nothing(blank, spec, code):
    before = _tree(blank)
    result = sheet_authoring.create_sheets(str(blank), [{"file": "ok.kicad_sch"}, spec])
    assert result["ok"] is False and result["code"] == code, result
    assert _tree(blank) == before, "the valid first spec must not be written either"


def test_create_sheets_refuses_a_broken_tree(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "sub.kicad_sch"}])
    assert sheet_authoring.add_sheet_symbols(str(blank), [_spec(file="sub.kicad_sch", name="Sub")])["status"] == "ok"
    (blank.parent / "sub.kicad_sch").unlink()
    result = sheet_authoring.create_sheets(str(blank), [{"file": "new.kicad_sch"}])
    assert result["code"] == "sheet_tree_incomplete" and "sheet file missing: sub.kicad_sch" in result["reason"]
    assert not (blank.parent / "new.kicad_sch").exists()


def test_create_sheets_removes_what_it_wrote_if_the_tree_is_not_whole_afterwards(blank, monkeypatch):
    real = sheet_tree.walk
    calls = []

    def walk(root):
        calls.append(root)
        tree = real(root)
        if len(calls) == 2:  # the check after the write
            return sheet_tree.SheetTree(tree.root, tree.files, tree.legacy_files, ("injected problem",), tree.unreferenced)
        return tree

    monkeypatch.setattr(sheet_authoring.sheet_tree, "walk", walk)
    result = sheet_authoring.create_sheets(str(blank), [{"file": "a.kicad_sch"}])
    assert result["code"] == "write_not_verified" and "injected problem" in result["reason"]
    assert not (blank.parent / "a.kicad_sch").exists()


def test_the_written_sheet_check_fires_on_a_corrupted_write(blank, monkeypatch):
    """The reload self-check (decision 4) sees a write that does not reload as the tree serialised."""
    real = write_layer.load

    def corrupt(path):
        root = real(path)
        if Path(path).name.endswith(".tmp") or ".tmp" in Path(path).name:
            root.children.pop()  # drop (lib_symbols)
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = sheet_authoring.create_sheets(str(blank), [{"file": "a.kicad_sch"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert not (blank.parent / "a.kicad_sch").exists()
    assert not [p for p in blank.parent.iterdir() if p.name.endswith(".tmp")]


# --------------------------------------------------------------------------- add_sheet_symbols


def _insert(sch: Path, specs=None, **kw):
    return sheet_authoring.add_sheet_symbols(str(sch), specs or [_spec()], **kw)


def test_insert_on_a_committed_project_keeps_the_tree_whole_and_the_netlist_intact(led, tmp_path):
    assert sheet_authoring.create_sheets(str(led), [{"file": "sensors.kicad_sch"}])["status"] == "ok"
    erc_before = _erc(led, tmp_path)
    before_nodes = [write_layer.serialise(c, compact=True) for c in write_layer.load(led).children]

    result = _insert(led)
    assert result["status"] == "ok" and result["ok"] is True, result
    assert result["verified"]["sheet_tree_whole"] is True
    assert result["sheets"] == ["simple_led.kicad_sch", "sensors.kicad_sch"] and result["unreferenced_sheets"] == []
    assert result["verified"]["netlist_sheets"] == ["/", "/Sensors/"]

    # nothing the parent had is gone or changed (only the sheet node is added)
    after_nodes = [write_layer.serialise(c, compact=True) for c in write_layer.load(led).children]
    assert [n for n in before_nodes if n not in after_nodes] == []
    added = [n for n in after_nodes if n not in before_nodes]
    assert len(added) == 1 and added[0].startswith("(sheet ")

    # kicad-cli on the real files: the sheet is seen, the original parts are still there
    erc_after = _erc(led, tmp_path)
    new = erc_after - erc_before
    assert new == Counter({("pin_not_connected", "error"): 4, ("label_dangling", "error"): 4}), new
    assert not [k for k in erc_after if k[0] in ("hier_label_mismatch", "duplicate_sheet_names")]
    census = sheet_authoring.netlist_sheet_names(led)
    assert "/Sensors/" in census
    refs = {c["ref"] for c in sheet_tree.netlist_components(led)}
    assert {"R1", "D1", "J1"} <= refs


def test_sheet_pins_sit_on_the_border_and_labels_agree(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    result = _insert(blank, [_spec(width=30.48, height=15.24)])
    assert result["status"] == "ok", result

    parent = write_layer.load(blank)
    (sheet,) = _node(parent, "sheet")
    # raw (at ...) and (size ...) of the S-expression, not a convenience property
    x, y = sheet.find_children("at")[0].get_float(0), sheet.find_children("at")[0].get_float(1)
    w, h = sheet.find_children("size")[0].get_float(0), sheet.find_children("size")[0].get_float(1)
    assert (x, y, w, h) == (101.6, 50.8, 30.48, 15.24)
    expected_angle = {"input": 180, "output": 0}
    pins = {p.get_string(0): p for p in sheet.find_children("pin")}
    assert set(pins) == {"SER", "SRCLK", "Q", "VCC"}
    for name, pin in pins.items():
        px, py, angle = (pin.find_children("at")[0].get_float(i) for i in range(3))
        direction = pin.children[1].value
        if name == "VCC":
            assert (py, angle, direction) == (y, 90, "passive") and x < px < x + w
        elif direction == "input":
            assert (px, angle) == (x, expected_angle["input"]) and y < py < y + h
        else:
            assert (px, angle) == (round(x + w, 4), expected_angle["output"]) and y < py < y + h
        assert round(px / 1.27, 6) == round(px / 1.27) or name == "VCC"
    assert result["inserted"][0]["pin_positions"]["SER"] == [101.6, 53.34]

    child = write_layer.load(blank.parent / "sensors.kicad_sch")
    labels = {c.get_string(0): c.find_children("shape")[0].get_string(0) for c in _node(child, "hierarchical_label")}
    assert labels == {"SER": "input", "SRCLK": "input", "Q": "output", "VCC": "passive"}
    assert sheet_authoring.interface_mismatches(parent, child, "sensors.kicad_sch") == []

    # the instance path is the parent's own, and the page follows the root's page 1
    inst = sheet.find_children("instances")[0].find_children("project")[0]
    assert inst.get_string(0) == "demo"
    path = inst.find_children("path")[0]
    assert path.get_string(0) == "/" + parent.find_children("uuid")[0].get_string(0)
    assert path.find_children("page")[0].get_string(0) == "2"


def test_a_second_insert_nests_under_the_first_sheet_with_its_instance_path(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "a.kicad_sch"}, {"file": "b.kicad_sch"}])
    assert _insert(blank, [_spec(file="a.kicad_sch", name="A")])["status"] == "ok"
    result = _insert(blank.parent / "a.kicad_sch", [_spec(file="b.kicad_sch", name="B", pins=[{"name": "X", "direction": "input"}])],
                     root_sch=str(blank))
    assert result["status"] == "ok", result
    assert result["sheets"] == ["demo.kicad_sch", "a.kicad_sch", "b.kicad_sch"]
    (sheet,) = _node(write_layer.load(blank.parent / "a.kicad_sch"), "sheet")
    root_uuid = write_layer.load(blank).find_children("uuid")[0].get_string(0)
    a_uuid = _node(write_layer.load(blank), "sheet")[0].find_children("uuid")[0].get_string(0)
    path = sheet.find_children("instances")[0].find_children("project")[0].find_children("path")[0]
    assert path.get_string(0) == f"/{root_uuid}/{a_uuid}"
    assert path.find_children("page")[0].get_string(0) == "3"


def test_a_label_the_child_already_has_is_reused_not_duplicated(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    from boardproof.toolsets import schematic as sch

    mcp = MCPServer("t")
    sch.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    made = tools["add_labels"](str(blank.parent / "sensors.kicad_sch"),
                               [{"kind": "hier", "x": 50.8, "y": 50.8, "text": "SER", "shape": "input", "snap": False}])
    assert made["status"] == "ok", made  # the created sheet is a sheet the other schematic tools can work on
    result = _insert(blank)
    assert result["status"] == "ok", result
    assert result["inserted"][0]["labels_reused"] == ["SER"]
    child = write_layer.load(blank.parent / "sensors.kicad_sch")
    assert [c.get_string(0) for c in _node(child, "hierarchical_label")].count("SER") == 1


@pytest.mark.parametrize(
    ("override", "code", "fragment"),
    [
        ({"file": "missing.kicad_sch"}, "sheet_file_missing", "create_sheets"),
        ({"file": "demo.kicad_sch"}, "sheet_already_referenced", "already a sheet"),
        ({"pins": []}, "invalid_spec", "pins"),
        ({"pins": [{"name": "A", "direction": "in"}]}, "invalid_spec", "direction"),
        ({"pins": [{"name": "A", "direction": "input"}, {"name": "A", "direction": "output"}]}, "invalid_spec", "twice"),
        ({"pins": [{"name": "A\nB", "direction": "input"}]}, "invalid_spec", "control character"),
        ({"pins": [{"name": "A", "direction": "input", "side": "middle"}]}, "invalid_spec", "side"),
        ({"pins": [{"name": "A", "direction": "input", "offset": 1.0}]}, "invalid_spec", "multiple of 1.27"),
        ({"height": 15.24, "pins": [{"name": "A", "direction": "input", "offset": 99.06}]}, "invalid_spec", "strictly inside"),
        ({"pins": [{"name": "A", "direction": "input", "offset": 2.54}, {"name": "B", "direction": "input", "offset": 2.54}]}, "invalid_spec", "already sits"),
        ({"x": 101.0}, "invalid_spec", "multiple of 1.27"),
        ({"width": -5}, "invalid_spec", "positive"),
        ({"name": ""}, "invalid_spec", "name"),
        ({"bogus": 1}, "invalid_spec", "specs[0]"),
    ],
)
def test_insert_refuses_a_bad_spec_and_changes_nothing(blank, override, code, fragment):
    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    snapshot = {p.name: p.read_bytes() for p in blank.parent.iterdir()}
    result = _insert(blank, [_spec(**override)])
    assert result["ok"] is False and result["code"] == code and fragment in result["reason"], result
    assert {p.name: p.read_bytes() for p in blank.parent.iterdir()} == snapshot


def test_insert_refuses_a_label_with_another_direction(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    tools["add_labels"](str(blank.parent / "sensors.kicad_sch"),
                        [{"kind": "hier", "x": 50.8, "y": 50.8, "text": "Q", "shape": "input", "snap": False}])
    snapshot = {p.name: p.read_bytes() for p in blank.parent.iterdir()}
    result = _insert(blank)
    assert result["code"] == "label_direction_mismatch", result
    assert {p.name: p.read_bytes() for p in blank.parent.iterdir()} == snapshot


def test_insert_refuses_a_second_sheet_with_the_same_name_or_file(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "a.kicad_sch"}, {"file": "b.kicad_sch"}])
    one = [{"name": "X", "direction": "input"}]
    same_name = _insert(blank, [_spec(file="a.kicad_sch", name="X", pins=one), _spec(file="b.kicad_sch", name="X", y=76.2, pins=one)])
    assert same_name["code"] == "invalid_spec" and "already used" in same_name["reason"]
    same_file = _insert(blank, [_spec(file="a.kicad_sch", name="X", pins=one), _spec(file="a.kicad_sch", name="Y", y=76.2, pins=one)])
    assert same_file["code"] == "invalid_spec" and "listed twice" in same_file["reason"]
    ok = _insert(blank, [_spec(file="a.kicad_sch", name="A", pins=one), _spec(file="b.kicad_sch", name="B", y=76.2, pins=one)])
    assert ok["status"] == "ok" and ok["count"] == 2, ok
    assert _insert(blank, [_spec(file="a.kicad_sch", name="A")])["code"] == "sheet_already_referenced"


def test_insert_refuses_a_parent_the_root_does_not_reach(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "a.kicad_sch"}, {"file": "b.kicad_sch"}])
    result = _insert(blank.parent / "a.kicad_sch", [_spec(file="b.kicad_sch", name="B")], root_sch=str(blank))
    assert result["code"] == "parent_not_in_tree", result


def test_default_height_and_sides_fit_the_pins(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    pins = [{"name": f"P{i}", "direction": "input"} for i in range(8)]
    assert _insert(blank, [_spec(pins=pins)])["status"] == "ok"
    (sheet,) = _node(write_layer.load(blank), "sheet")
    height = sheet.find_children("size")[0].get_float(1)
    last = max(p.find_children("at")[0].get_float(1) for p in sheet.find_children("pin"))
    assert last < 50.8 + height


# --------------------------------------------------------------------------- the self-checks fire


def _unchanged_after_refusal(blank, monkeypatch, **patches):
    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    for name, value in patches.items():
        monkeypatch.setattr(sheet_authoring, name, value)
    snapshot = {p.name: p.read_bytes() for p in blank.parent.iterdir()}
    result = _insert(blank)
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert {p.name: p.read_bytes() for p in blank.parent.iterdir()} == snapshot
    return result


def test_a_new_erc_violation_type_fails_the_insert(blank, monkeypatch):
    real = sheet_authoring.erc_census

    def erc(root):
        counts = real(root)
        if "staged" in str(root):
            counts[("hier_label_mismatch", "error")] += 1
        return counts

    result = _unchanged_after_refusal(blank, monkeypatch, erc_census=erc)
    assert "hier_label_mismatch" in result["reason"]


def test_more_dangling_violations_than_the_interface_explains_fail_the_insert(blank, monkeypatch):
    real = sheet_authoring.erc_census

    def erc(root):
        counts = real(root)
        if "staged" in str(root):
            counts[("pin_not_connected", "error")] += 1
        return counts

    result = _unchanged_after_refusal(blank, monkeypatch, erc_census=erc)
    assert "pin_not_connected" in result["reason"]
    assert "than the project and the child sheets had before" in result["reason"]  # does not blame the interface
    assert "the interface explains at most" in result["reason"]


def test_add_sheet_symbols_corrupted_write_fails_the_insert(blank, monkeypatch):
    real = write_layer.load

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name and root.find_children("sheet"):
            root.find_children("sheet")[0].children.pop()  # the written parent lost the sheet instances
        return root

    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    snapshot = {p.name: p.read_bytes() for p in blank.parent.iterdir()}
    monkeypatch.setattr(write_layer, "load", corrupt)
    result = sheet_authoring.add_sheet_symbols(str(blank), [_spec()])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert {p.name: p.read_bytes() for p in blank.parent.iterdir()} == snapshot


def test_a_netlist_that_does_not_see_the_sheet_fails_the_insert(blank, monkeypatch):
    result = _unchanged_after_refusal(blank, monkeypatch, netlist_sheet_names=lambda root: ["/"])
    assert "netlist" in result["reason"] and "Sensors" in result["reason"]


def test_pins_and_labels_that_disagree_fail_the_insert(blank, monkeypatch):
    real = sheet_authoring.label_node

    def wrong_direction(name, direction, side, x, y):
        return real(name, "output" if direction == "input" else direction, side, x, y)

    result = _unchanged_after_refusal(blank, monkeypatch, label_node=wrong_direction)
    assert "disagree" in result["reason"] and "SER" in result["reason"]


def test_interface_mismatches_names_each_kind_of_disagreement(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    assert _insert(blank)["status"] == "ok"
    parent = write_layer.load(blank)
    child = write_layer.load(blank.parent / "sensors.kicad_sch")
    labels = _node(child, "hierarchical_label")
    child.children.remove(labels[0])                      # SER loses its label
    write_layer.set_string(labels[1], 0, "EXTRA")         # SRCLK renamed: a pin without label and a label without pin
    write_layer.set_symbol(labels[2].find_children("shape")[0], 0, "input")   # Q: direction differs
    found = sheet_authoring.interface_mismatches(parent, child, "sensors.kicad_sch")
    assert any("pin 'SER' has no hierarchical label" in f for f in found)
    assert any("pin 'SRCLK' has no hierarchical label" in f for f in found)
    assert any("label 'EXTRA' has no pin" in f for f in found)
    assert any("'Q' is output on the pin and input on the label" in f for f in found)


def test_the_parent_save_failing_restores_the_child(blank, monkeypatch):
    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    child_path = blank.parent / "sensors.kicad_sch"
    child_before = write_layer.serialise(write_layer.load(child_path), compact=True)
    parent_before = blank.read_bytes()
    real = write_layer.save
    seen = []

    def save(root, path, **kw):
        if Path(path).resolve() == blank.resolve() and not seen:
            seen.append(path)
            raise OSError("disk full")
        return real(root, path, **kw)

    monkeypatch.setattr(write_layer, "save", save)
    with pytest.raises(OSError, match="disk full"):
        _insert(blank)
    assert blank.read_bytes() == parent_before
    assert write_layer.serialise(write_layer.load(child_path), compact=True) == child_before


# --------------------------------------------------------------------------- the tools


def _listing():
    mcp = MCPServer("t")
    names = schematic_toolset.build(mcp)
    return names, {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_both_tools_are_registered_mutating_and_the_text_an_llm_reads_is_pinned():
    names, tools = _listing()
    assert {"create_sheets", "add_sheet_symbols"} <= set(names)
    for name in ("create_sheets", "add_sheet_symbols"):
        text = " ".join((tools[name].description or "").split())
        assert write_layer.LOCK_SENTENCE in text
        assert "ok: false" in text and "nothing written" in text
    create = " ".join(tools["create_sheets"].description.split())
    for phrase in ("version 20260306", "awaiting_reference", "sheet_tree_incomplete", "sheet_exists", "sheet_outside_project",
                   "unreferenced until add_sheet_symbols"):
        assert phrase in create, phrase
    insert = " ".join(tools["add_sheet_symbols"].description.split())
    for phrase in ("input, output, bidirectional or passive", "left, right, top or bottom", "a multiple of 1.27",
                   "hierarchical label of the same name and direction", "pin_not_connected", "label_dangling",
                   "netlist export must list the sheet", "write_not_verified", "label_direction_mismatch",
                   "sheet_already_referenced", "sheet_file_missing", "symbols_reattached",
                   "rewritten to the project's name and the new sheet use's path", "keeping reference, unit and value",
                   "add_symbols on a sheet that is already attached", "one path per use of the sheet in the tree"):
        assert phrase in insert, phrase
    assert tools["create_sheets"].annotations.read_only_hint is False
    assert tools["add_sheet_symbols"].annotations.read_only_hint is False


def test_the_tools_return_the_results_of_the_module(blank):
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert tools["create_sheets"](str(blank), [{"file": "sensors.kicad_sch"}])["status"] == "ok"
    result = tools["add_sheet_symbols"](str(blank), [_spec()])
    assert result["status"] == "ok" and result["verified"]["sheet_tree_whole"] is True


# --------------------------------------------------------------------------- a child that already holds symbols


def _tools():
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _child_with_parts(blank) -> Path:
    tools = _tools()
    assert sheet_authoring.create_sheets(str(blank), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    sub = blank.parent / "sub.kicad_sch"
    placed = tools["add_symbols"](str(sub), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "1k"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "2k"}])
    assert placed["status"] == "ok", placed
    wired = tools["add_wires"](str(sub), [{"p1": [50.8, 54.61], "p2": [76.2, 46.99]}])
    assert wired["status"] == "ok", wired
    return sub


def _instance_paths(sheet: Path) -> dict[str, list[tuple[str, str]]]:
    found = {}
    for symbol in _node(write_layer.load(sheet), "symbol"):
        ref = [p for p in symbol.find_children("property") if p.get_string(0) == "Reference"][0].get_string(1)
        found[ref] = [(proj.get_string(0), path.get_string(0))
                      for inst in symbol.find_children("instances") for proj in inst.find_children("project")
                      for path in proj.find_children("path")]
    return found


def test_attaching_a_child_with_symbols_moves_their_instance_data_to_the_sheet_use(blank):
    sub = _child_with_parts(blank)
    stale = _instance_paths(sub)
    own = "/" + write_layer.load(sub).find_children("uuid")[0].get_string(0)
    assert all(p[0][1] == own for p in stale.values())

    result = sheet_authoring.add_sheet_symbols(
        str(blank), [_spec(file="sub.kicad_sch", name="Sub", pins=[{"name": "A", "direction": "input"}])])
    assert result["status"] == "ok", result
    assert result["inserted"][0]["symbols_reattached"] == 2

    tree = sheet_tree.walk(blank)
    (use,) = [path for file, path in tree.uses if file == sub.resolve() and path != own]
    now = _instance_paths(sub)
    assert set(now) == {"R1", "R2"}
    for ref, entries in now.items():
        assert entries == [("demo", use)], (ref, entries)
    refs = [c["ref"] for c in sheet_tree.netlist_components(blank)]
    assert sorted(refs) == ["R1", "R2"]
    assert sheet_tree.netlist_disagreement(tree, refs) == []


def test_a_child_symbol_whose_instance_data_cannot_be_rewritten_is_refused_naming_the_instance_problem(blank):
    sub = _child_with_parts(blank)
    root = write_layer.load(sub)
    symbol = _node(root, "symbol")[0]
    path = symbol.find_children("instances")[0].find_children("project")[0].find_children("path")[0]
    write_layer.set_string(path, 0, "/00000000-0000-4000-8000-000000000000")  # a foreign path: not the child's own
    write_layer.save(root, sub)
    snapshot = {p.name: p.read_bytes() for p in blank.parent.iterdir()}
    result = sheet_authoring.add_sheet_symbols(
        str(blank), [_spec(file="sub.kicad_sch", name="Sub", pins=[{"name": "A", "direction": "input"}])])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert "instance data of the symbols does not match the sheet hierarchy" in result["reason"]
    assert "ERC" not in result["reason"]
    assert {p.name: p.read_bytes() for p in blank.parent.iterdir()} == snapshot


def test_the_sheet_node_goes_before_sheet_instances_and_embedded_fonts(blank):
    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}, {"file": "second.kicad_sch"}])
    assert _insert(blank)["status"] == "ok"
    names = [c.name for c in write_layer.load(blank).children if not c.is_atom]
    assert names.index("sheet") < names.index("sheet_instances")
    root = write_layer.load(blank)
    root.append(SExp.list("embedded_fonts", write_layer.symbol("no")))
    write_layer.save(root, blank)
    assert _insert(blank, [_spec(file="second.kicad_sch", name="Second", y=101.6)])["status"] == "ok"
    names = [c.name for c in write_layer.load(blank).children if not c.is_atom]
    last_sheet = max(i for i, n in enumerate(names) if n == "sheet")
    assert last_sheet < names.index("sheet_instances") and last_sheet < names.index("embedded_fonts")


@pytest.mark.parametrize("which", ["child", "parent"])
def test_the_lock_check_refuses_before_anything_is_written(blank, which):
    from tests.test_write_guard import CODE, OTHER_HOST

    sheet_authoring.create_sheets(str(blank), [{"file": "sensors.kicad_sch"}])
    target = blank.parent / "sensors.kicad_sch" if which == "child" else blank
    lock = target.with_name(f"~{target.name}.lck")
    lock.write_bytes(OTHER_HOST)
    snapshot = {p.name: p.read_bytes() for p in blank.parent.iterdir()}
    result = _insert(blank)
    assert result["code"] == CODE and result["ok"] is False, result
    assert {p.name: p.read_bytes() for p in blank.parent.iterdir()} == snapshot  # children untouched, no temp file


@pytest.mark.parametrize("name", [".kicad_sch", " .kicad_sch", "NUL.kicad_sch", "con.kicad_sch", "COM1.kicad_sch",
                                  "lpt9.kicad_sch", "aux.x.kicad_sch", "sub/PRN.kicad_sch", "sub./x.kicad_sch"])
def test_degenerate_sheet_file_names_are_refused(blank, name):
    (blank.parent / "sub").mkdir(exist_ok=True)
    before = _tree(blank)
    result = sheet_authoring.create_sheets(str(blank), [{"file": name}])
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _tree(blank) == before
    result = _insert(blank, [_spec(file=name)])
    assert result["ok"] is False and result["code"] == "invalid_spec", result


# --------------------------------------------------------------------------- add_symbols on a sheet used in a tree (issue 79)


def _attach(blank: Path, name: str = "Sub") -> Path:
    """An empty sub-sheet made and attached below the root, before any symbol is placed in it."""
    assert sheet_authoring.create_sheets(str(blank), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    result = sheet_authoring.add_sheet_symbols(
        str(blank), [_spec(file="sub.kicad_sch", name=name, pins=[{"name": "A", "direction": "input"}])])
    assert result["status"] == "ok", result
    return blank.parent / "sub.kicad_sch"


def _use_a_second_time(root_sch: Path, name: str = "Sub2") -> None:
    """A second sheet symbol of sub.kicad_sch in the root, built through the write layer."""
    import copy
    import uuid as uuid_mod

    root = write_layer.load(root_sch)
    node = copy.deepcopy(_node(root, "sheet")[0])
    for item in node.find_children("uuid") + [u for p in node.find_children("pin") for u in p.find_children("uuid")]:
        write_layer.set_string(item, 0, str(uuid_mod.uuid4()))
    sheetname = [p for p in node.find_children("property") if p.get_string(0) == "Sheetname"][0]
    write_layer.set_string(sheetname, 1, name)
    at = node.find_children("at")[0]
    write_layer.set_number(at, 1, at.get_float(1) + 60)
    for prop in node.find_children("property"):
        pat = prop.find_children("at")[0]
        write_layer.set_number(pat, 0, pat.get_float(0) + 60)
    for pin in node.find_children("pin"):
        pat = pin.find_children("at")[0]
        write_layer.set_number(pat, 0, pat.get_float(0) + 60)
    path = node.find_children("instances")[0].find_children("project")[0].find_children("path")[0]
    write_layer.set_string(path, 0, path.get_string(0).rsplit("/", 1)[0] + "/" + node.find_children("uuid")[0].get_string(0))
    root.children.insert(root.children.index(_node(root, "sheet")[0]) + 1, node)
    write_layer.save(root, root_sch)


def _place(sheet: Path, refs=("R1", "R2")) -> dict:
    return _tools()["add_symbols"](str(sheet), [
        {"lib_id": "Device:R", "x": 50.8 + 25.4 * i, "y": 50.8, "ref": ref, "value": "1k"} for i, ref in enumerate(refs)])


def _agrees(root_sch: Path) -> list[str]:
    tree = sheet_tree.walk(root_sch)
    return sheet_tree.netlist_disagreement(tree, [c["ref"] for c in sheet_tree.netlist_components(root_sch)])


def test_add_symbols_on_a_sheet_used_once_writes_the_instance_path_of_that_use(blank):
    sub = _attach(blank)
    placed = _place(sub)
    assert placed["status"] == "ok", placed
    tree = sheet_tree.walk(blank)
    (use,) = [p for f, p in tree.uses if f == sub.resolve()]
    assert _instance_paths(sub) == {"R1": [("demo", use)], "R2": [("demo", use)]}
    assert _agrees(blank) == []


def test_add_symbols_on_a_sheet_used_twice_writes_one_instance_entry_per_use(blank):
    sub = _attach(blank)
    _use_a_second_time(blank)
    tree = sheet_tree.walk(blank)
    uses = [p for f, p in tree.uses if f == sub.resolve()]
    assert len(uses) == 2
    placed = _place(sub)
    assert placed["status"] == "ok", placed
    found = _instance_paths(sub)
    for ref in ("R1", "R2"):
        assert sorted(found[ref]) == sorted(("demo", u) for u in uses), (ref, found[ref])
    # no entry is stale; what the check still names is the one reference on both uses (annotation, fix_annotation)
    lines = _agrees(blank)
    assert lines, lines
    assert all("more than one" in line or "listed more than once" in line for line in lines), lines


def test_add_symbols_on_the_root_sheet_keeps_its_own_uuid_path(blank):
    placed = _place(blank)
    assert placed["status"] == "ok", placed
    own = "/" + write_layer.load(blank).find_children("uuid")[0].get_string(0)
    found = _instance_paths(blank)
    assert {ref: [p for _, p in entries] for ref, entries in found.items()} == {"R1": [own], "R2": [own]}
    assert _agrees(blank) == []


def test_add_symbols_on_a_sheet_not_yet_attached_keeps_its_own_uuid_path(blank):
    assert sheet_authoring.create_sheets(str(blank), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    sub = blank.parent / "sub.kicad_sch"
    placed = _place(sub)
    assert placed["status"] == "ok", placed
    own = "/" + write_layer.load(sub).find_children("uuid")[0].get_string(0)
    assert {ref: [p for _, p in entries] for ref, entries in _instance_paths(sub).items()} == {"R1": [own], "R2": [own]}
