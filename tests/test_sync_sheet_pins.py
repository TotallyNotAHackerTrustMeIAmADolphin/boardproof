"""compare_sheet_pins / sync_sheet_pins (GitHub issue 110) and the write lock of create_sheets and add_sheet_symbols.

Reproduction of the need (issue 89 left it open): a child sheet carries hierarchical labels, the
sheet symbol that uses it has no pins for them (or the other way round, or the direction differs),
and no tool listed that or made the pins in one call. kicad-cli's ERC reports a missing name
(``hier_label_mismatch``) but not a different direction (``sheet_authoring.interface_mismatches``).

Fixture (``root`` of tests/test_sheet_pins.py, built only through the tools): Connectors (child holds
the labels PT_A and PT_B, bidirectional, the sheet symbol has no pins) and MCU (no label, no pin).
``drifted`` adds to MCU: SER (output, in sync), CLK (pin input, label output: a direction mismatch),
OLD (a pin whose label was deleted) and EXTRA (a label, input, without a pin).

Lock gap (reproduction before the fix, through the tool wrapper): ``create_sheets`` names its file as
``root_sch``, which is not in ``gate_state._MUTATION_ARGS``, so two calls creating one file both
returned ok; ``add_sheet_symbols`` locked only ``parent_sch``, so an ``add_labels`` on the child it
rewrites was overwritten (lost update).
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from boardproof import sheet_authoring, sheet_pins, write_layer
from tests import test_sheet_pins as _base
from tests.test_sheet_pins import _bytes, _labels, _pins

# the fixtures of tests/test_sheet_pins.py, bound by name (an import of a fixture would redefine it in every parameter list)
root = _base.root
tools = _base.tools
_need_kicad_cli = _base._need_kicad_cli


def _wrapped() -> dict:
    """The schematic tools registered the way the server does (``server._gate_every_toolset_tool``: the annotations
    dict reaches ``attach_gate_field``, so a read-only tool is not serialised)."""
    from mcp.server.mcpserver import MCPServer

    from boardproof import gate_state, server
    from boardproof.toolsets import schematic as schematic_toolset

    mcp = MCPServer("test")
    server._gate_every_toolset_tool(mcp, gate_state.GateState())
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def drifted(root, tools) -> Path:
    mcu = root.parent / "mcu.kicad_sch"
    added = tools["add_sheet_pins"](str(root), [{"sheet": "MCU", "pins": [
        {"name": "SER", "direction": "output"}, {"name": "CLK", "direction": "input"}, {"name": "OLD", "direction": "bidirectional"}]}])
    assert added["ok"] is True, added
    gone = tools["delete_labels"](str(mcu), [{"texts": ["CLK", "OLD"], "kind": "hier"}], allow_connectivity_change=True)
    assert gone["status"] == "ok", gone
    made = tools["add_labels"](str(mcu), [
        {"kind": "hier", "x": 25.4, "y": 30.48, "text": "CLK", "shape": "output", "snap": False},
        {"kind": "hier", "x": 25.4, "y": 38.1, "text": "EXTRA", "shape": "input", "snap": False}])
    assert made["status"] == "ok", made
    return root


def _by_sheet(result: dict) -> dict[str, dict]:
    return {s["sheet"]: s for s in result["sheets"]}


# --------------------------------------------------------------------------- compare_sheet_pins (report only)


def test_compare_lists_every_difference_with_direction(drifted, tools):
    result = tools["compare_sheet_pins"](str(drifted))
    assert result["ok"] is True and result["written"] is False, result
    sheets = _by_sheet(result)
    conn, mcu = sheets["Connectors"], sheets["MCU"]
    assert [(m["name"], m["direction"]) for m in conn["missing_pins"]] == [("PT_A", "bidirectional"), ("PT_B", "bidirectional")]
    assert conn["missing_labels"] == [] and conn["direction_mismatches"] == [] and conn["in_sync"] is False
    assert [(m["name"], m["direction"]) for m in mcu["missing_pins"]] == [("EXTRA", "input")]
    assert [(m["name"], m["pin_direction"]) for m in mcu["missing_labels"]] == [("OLD", "bidirectional")]
    assert mcu["direction_mismatches"] == [{"name": "CLK", "pin_direction": "input", "label_direction": "output"}]
    assert mcu["pins_in_sync"] == ["SER"]
    assert result["differences"] == 5 and result["sheets_in_sync"] == 0


def test_compare_reports_a_label_position_a_pin_position_and_selects_by_name_or_uuid(drifted, tools):
    result = tools["compare_sheet_pins"](str(drifted), ["MCU"])
    assert [s["sheet"] for s in result["sheets"]] == ["MCU"]
    mcu = result["sheets"][0]
    assert mcu["missing_pins"][0]["label_at"] and len(mcu["missing_pins"][0]["label_at"]) == 2
    assert mcu["missing_labels"][0]["pin_at"] and len(mcu["missing_labels"][0]["pin_at"]) == 2
    by_uuid = tools["compare_sheet_pins"](str(drifted), [mcu["uuid"]])
    assert [s["sheet"] for s in by_uuid["sheets"]] == ["MCU"]
    refused = tools["compare_sheet_pins"](str(drifted), ["Nope"])
    assert refused["ok"] is False and refused["code"] == "sheet_not_found", refused


def test_compare_writes_nothing_and_takes_no_lock(drifted, monkeypatch):
    tools = _wrapped()
    before = _bytes(drifted.parent)
    monkeypatch.setattr(write_layer, "LOCK_TIMEOUT_S", 0.3)
    taken, release = threading.Event(), threading.Event()

    def holder() -> None:
        with write_layer.path_lock([drifted, drifted.parent / "mcu.kicad_sch"]):
            taken.set()
            release.wait(30)

    t = threading.Thread(target=holder)
    t.start()
    assert taken.wait(5)
    try:
        result = tools["compare_sheet_pins"](str(drifted))
    finally:
        release.set()
        t.join(10)
    assert result["ok"] is True and result["differences"] == 5, result
    assert _bytes(drifted.parent) == before


def test_compare_of_a_synced_project_reports_in_sync(root, tools):
    assert tools["sync_sheet_pins"](str(root))["ok"] is True
    result = tools["compare_sheet_pins"](str(root))
    assert result["differences"] == 0 and result["sheets_in_sync"] == 2, result
    assert all(s["in_sync"] for s in result["sheets"])


def test_compare_refuses_a_parent_outside_the_tree_and_a_missing_file(root, tools, tmp_path):
    assert tools["compare_sheet_pins"](str(tmp_path / "nope.kicad_sch"))["code"] == "file_not_found"
    assert tools["compare_sheet_pins"](str(root), [])["code"] == "invalid_spec"
    assert tools["compare_sheet_pins"](str(root), "MCU")["code"] == "invalid_spec"


# --------------------------------------------------------------------------- sync_sheet_pins


def test_sync_creates_the_missing_pins_from_the_labels_with_their_direction(drifted, tools):
    child = drifted.parent / "connectors.kicad_sch"
    child_before = child.read_bytes()
    result = tools["sync_sheet_pins"](str(drifted))
    assert result["ok"] is True and result["written"] is True and result["count"] == 3, result
    assert _pins(drifted, "Connectors").keys() == {"PT_A", "PT_B"}
    assert {n: p[0] for n, p in _pins(drifted, "Connectors").items()} == {"PT_A": "bidirectional", "PT_B": "bidirectional"}
    assert _pins(drifted, "MCU").keys() == {"SER", "CLK", "OLD", "EXTRA"}
    assert _pins(drifted, "MCU")["EXTRA"][0] == "input"
    assert child.read_bytes() == child_before, "a child file was written although every label existed"
    assert all(p["child_label"]["state"] == "reused" for entry in result["added"] for p in entry["pins"])
    unresolved = result["unresolved"]
    assert [(m["sheet"], m["name"], m["pin_direction"]) for m in unresolved["missing_labels"]] == [("MCU", "OLD", "bidirectional")]
    assert len(unresolved["missing_labels"][0]["pin_at"]) == 2
    assert unresolved["direction_mismatches"] == [{"sheet": "MCU", "name": "CLK", "pin_direction": "input", "label_direction": "output"}]
    assert _labels(drifted.parent / "mcu.kicad_sch")["CLK"] == "output" and "OLD" not in _labels(drifted.parent / "mcu.kicad_sch")


def test_sync_second_run_changes_nothing(root, tools):
    first = tools["sync_sheet_pins"](str(root))
    assert first["written"] is True
    before = _bytes(root.parent)
    second = tools["sync_sheet_pins"](str(root))
    assert second["ok"] is True and second["written"] is False and second["count"] == 0, second
    assert _bytes(root.parent) == before


def test_sync_selection_and_dry_run_and_unknown_sheet(root, tools):
    before = _bytes(root.parent)
    dry = tools["sync_sheet_pins"](str(root), ["Connectors"], dry_run=True)
    assert dry["ok"] is True and dry["written"] is False and dry["dry_run"] is True and dry["count"] == 2, dry
    assert _bytes(root.parent) == before
    refused = tools["sync_sheet_pins"](str(root), ["Nope"])
    assert refused["ok"] is False and refused["code"] == "sheet_not_found" and _bytes(root.parent) == before
    only = tools["sync_sheet_pins"](str(root), ["Connectors"])
    assert only["written"] is True and set(_pins(root, "Connectors")) == {"PT_A", "PT_B"}


def test_sync_pins_land_apart_on_the_edge(root, tools):
    tools["sync_sheet_pins"](str(root))
    pins = _pins(root, "Connectors")
    assert len({(p[1], p[2]) for p in pins.values()}) == 2


def test_sync_keeps_the_netlist_and_the_erc_baseline(root, tools, tmp_path):
    from tests.test_sheet_pins import _erc_types

    before = _erc_types(root, tmp_path)
    assert tools["sync_sheet_pins"](str(root))["ok"] is True
    after = _erc_types(root, tmp_path)
    assert after.get("hier_label_mismatch", 0) <= before.get("hier_label_mismatch", 0), (before, after)


def test_the_reload_check_fires_on_a_corrupted_sync_write(root, tools, monkeypatch):
    real = write_layer.load
    before = _bytes(root.parent)

    def corrupt(path):
        node = real(path)
        # only the temporary sibling of the real parent (not the scratch copies of the staged checks): the verify of the real save
        if ".tmp" in Path(path).name and Path(path).resolve().parent == root.resolve().parent:
            node.children.pop()
        return node

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = tools["sync_sheet_pins"](str(root))
    monkeypatch.undo()
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert _bytes(root.parent) == before
    assert not [p for p in root.parent.iterdir() if p.name.endswith(".tmp")]


def test_the_staged_check_fires_when_sync_touches_more_than_it_selected(root, tools, monkeypatch):
    before = _bytes(root.parent)
    real = sheet_pins._plan_add

    def greedy(session, specs):
        real(session, specs)
        session.plan.parent.children.pop()  # a top-level node of the parent vanishes

    monkeypatch.setattr(sheet_pins, "_plan_add", greedy)
    result = tools["sync_sheet_pins"](str(root))
    monkeypatch.undo()
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert _bytes(root.parent) == before


def test_sync_through_the_wrapper_waits_for_a_writer_of_the_child(root):
    tools = _wrapped()
    child = root.parent / "connectors.kicad_sch"
    entered = threading.Event()
    real_verify = sheet_pins._staged_verify

    def slow_verify(*a, **k):
        entered.set()
        time.sleep(0.5)
        return real_verify(*a, **k)

    mp = pytest.MonkeyPatch()
    mp.setattr(sheet_pins, "_staged_verify", slow_verify)
    results: dict[str, dict] = {}
    ended: dict[str, float] = {}

    def sync() -> None:
        results["sync"] = tools["sync_sheet_pins"](str(root), ["Connectors"])
        ended["sync"] = time.monotonic()

    a = threading.Thread(target=sync)
    a.start()
    try:
        assert entered.wait(60)
        results["label"] = tools["add_labels"](str(child), [{"kind": "label", "x": 76.2, "y": 76.2, "text": "LOCAL_NET", "snap": False}])
        ended["label"] = time.monotonic()
    finally:
        a.join(120)
        mp.undo()
    assert results["sync"]["ok"] is True and results["label"]["status"] == "ok", results
    assert ended["label"] >= ended["sync"], "add_labels on the child ran while sync_sheet_pins held the sheet (no lock over the child)"
    assert "LOCAL_NET" in child.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- the lock gap of issue 75's tools


def _project(tmp_path, tools):
    from boardproof.toolsets import project as project_toolset

    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    return Path(made["schematic_path"])


@pytest.mark.parametrize("second_root", ["same", "other"])
def test_two_create_sheets_for_one_file_through_the_wrapper_do_not_both_succeed(tmp_path, _need_kicad_cli, second_root):
    import shutil

    tools = _wrapped()
    sch = _project(tmp_path, tools)
    other = sch
    if second_root == "other":  # a second root sheet in the same folder (a copy made by the test): the lock must be on the file created
        other = sch.parent / "other.kicad_sch"
        shutil.copyfile(sch, other)
    entered = threading.Event()
    real_check = write_layer.check_writable
    first = {"name": None}

    def slow_check(*paths):
        me = threading.current_thread().name
        if first["name"] is None:
            first["name"] = me
        if me == first["name"]:
            entered.set()
            time.sleep(1.5)
        return real_check(*paths)

    mp = pytest.MonkeyPatch()
    mp.setattr(write_layer, "check_writable", slow_check)
    results: dict[str, dict] = {}
    a = threading.Thread(name="creator-a", target=lambda: results.__setitem__("a", tools["create_sheets"](str(sch), [{"file": "n.kicad_sch"}])))
    b = threading.Thread(name="creator-b", target=lambda: results.__setitem__("b", tools["create_sheets"](str(other), [{"file": "n.kicad_sch"}])))
    try:
        a.start()
        assert entered.wait(30)
        b.start()
    finally:
        a.join(120)
        b.join(120)
        mp.undo()
    assert results["a"]["status"] == "ok" and results["b"].get("code") == "sheet_exists", results


def test_add_sheet_symbols_and_add_labels_on_its_child_do_not_lose_each_other(tmp_path, _need_kicad_cli):
    tools = _wrapped()
    sch = _project(tmp_path, tools)
    assert tools["create_sheets"](str(sch), [{"file": "x.kicad_sch"}])["status"] == "ok"
    child = sch.parent / "x.kicad_sch"
    entered = threading.Event()
    real_verify = sheet_authoring._verify_staged

    def slow_verify(*a, **k):
        entered.set()
        time.sleep(0.5)
        return real_verify(*a, **k)

    mp = pytest.MonkeyPatch()
    mp.setattr(sheet_authoring, "_verify_staged", slow_verify)
    spec = {"file": "x.kicad_sch", "name": "X", "x": 101.6, "y": 50.8, "height": 25.4, "pins": [{"name": "SER", "direction": "input"}]}
    results: dict[str, dict] = {}
    a = threading.Thread(target=lambda: results.__setitem__("sheet", tools["add_sheet_symbols"](str(sch), [spec])))
    a.start()
    try:
        assert entered.wait(60)
        results["label"] = tools["add_labels"](str(child), [{"kind": "label", "x": 76.2, "y": 76.2, "text": "LOCAL_NET", "snap": False}])
    finally:
        a.join(120)
        mp.undo()
    assert results["sheet"]["status"] == "ok", results
    text = child.read_text(encoding="utf-8")
    assert '"SER"' in text and '"LOCAL_NET"' in text, "one write was lost (lost update): " + str(results)


# --------------------------------------------------------------------------- review of issue 110


def test_a_nested_add_sheet_symbols_and_a_call_on_the_root_do_not_wait_for_each_other(tmp_path, _need_kicad_cli, monkeypatch):
    """Lock order (B1): a call never waits for a file above its own parent. sync_sheet_pins on the root holds the root and then
    the parent P; add_sheet_symbols on P (root_sch the root) used to hold P and then ask for the root: both waited until the timeout."""
    tools = _wrapped()
    sch = _project(tmp_path, tools)
    assert tools["create_sheets"](str(sch), [{"file": "p.kicad_sch"}, {"file": "r.kicad_sch"}])["status"] == "ok"
    spec = {"file": "p.kicad_sch", "name": "P", "x": 101.6, "y": 50.8, "height": 25.4, "pins": [{"name": "S", "direction": "input"}]}
    assert tools["add_sheet_symbols"](str(sch), [spec])["status"] == "ok"
    p_sheet = sch.parent / "p.kicad_sch"
    nested = dict(spec, file="r.kicad_sch", name="R")
    monkeypatch.setattr(write_layer, "LOCK_TIMEOUT_S", 45.0)
    entered = threading.Event()
    real_find = sheet_authoring.find_root

    def slow_find(path, root_sch):
        if threading.current_thread().name == "adder":
            entered.set()
            time.sleep(3)  # the wrapper holds P; the root call below takes the root and then waits for P
        return real_find(path, root_sch)

    monkeypatch.setattr(sheet_authoring, "find_root", slow_find)
    results: dict[str, dict] = {}
    ended: dict[str, float] = {}

    def adder() -> None:
        results["a"] = tools["add_sheet_symbols"](str(p_sheet), [nested], str(sch))
        ended["a"] = time.monotonic()

    def syncer() -> None:
        results["b"] = tools["sync_sheet_pins"](str(sch))
        ended["b"] = time.monotonic()

    start = time.monotonic()
    a = threading.Thread(name="adder", target=adder)
    b = threading.Thread(name="syncer", target=syncer)
    a.start()
    assert entered.wait(30)
    b.start()
    a.join(200)
    b.join(200)
    monkeypatch.undo()
    assert results["a"].get("status") == "ok" and results["b"].get("ok") is True, results
    assert ended["a"] - start < 40 and ended["b"] - start < 40, ("a call waited for the lock timeout", ended, start)


def test_allow_connectivity_change_of_sync_is_honoured_and_refused_by_default(root, tools):
    dry = tools["sync_sheet_pins"](str(root), ["Connectors"], dry_run=True)
    pin = next(p for p in dry["added"][0]["pins"] if p["name"] == "PT_A")
    x, y = pin["at"]
    wired = tools["add_wires"](str(root), [{"p1": [x, y], "p2": [x - 12.7, y], "route": "direct", "snap": False}])
    assert wired["status"] == "ok", wired
    before = _bytes(root.parent)
    refused = tools["sync_sheet_pins"](str(root), ["Connectors"])
    assert refused["ok"] is False and refused["code"] == "connectivity_changed", refused
    assert _bytes(root.parent) == before
    allowed = tools["sync_sheet_pins"](str(root), ["Connectors"], allow_connectivity_change=True)
    assert allowed["ok"] is True and allowed["written"] is True, allowed
    assert set(_pins(root, "Connectors")) == {"PT_A", "PT_B"}


def _add_duplicate_label(root, tools):
    child = root.parent / "connectors.kicad_sch"
    made = tools["add_labels"](str(child), [
        {"kind": "hier", "x": 38.1, "y": 62.23, "text": "DUP", "shape": "input", "snap": False},
        {"kind": "hier", "x": 38.1, "y": 64.77, "text": "DUP", "shape": "output", "snap": False}])
    assert made["status"] == "ok", made


def test_compare_reports_a_label_twice_in_the_child(root, tools):
    _add_duplicate_label(root, tools)
    entry = _by_sheet(tools["compare_sheet_pins"](str(root)))["Connectors"]
    assert entry["duplicate_labels"] == [{"name": "DUP", "directions": ["input", "output"], "count": 2}], entry
    assert entry["in_sync"] is False


def test_sync_skips_a_label_twice_with_two_directions_and_still_creates_the_other_pins(root, tools):
    _add_duplicate_label(root, tools)
    result = tools["sync_sheet_pins"](str(root), ["Connectors"])
    assert result["ok"] is True and result["written"] is True and result["count"] == 2, result
    assert set(_pins(root, "Connectors")) == {"PT_A", "PT_B"}, "a pin for the label with two directions was made or the others were not"
    assert [d["name"] for d in result["unresolved"]["duplicate_labels"]] == ["DUP"], result["unresolved"]
    assert result["differences_left"] >= 1


@pytest.mark.parametrize("dry_run", [False, True])
def test_the_no_op_note_does_not_claim_all_labels_have_a_pin_when_one_was_skipped(root, tools, dry_run):
    assert tools["sync_sheet_pins"](str(root))["ok"] is True
    child = root.parent / "connectors.kicad_sch"
    made = tools["add_labels"](str(child), [{"kind": "hier", "x": 38.1, "y": 70.0, "text": "ODD", "shape": "input", "snap": False}])
    assert made["status"] == "ok", made
    node = write_layer.load(child)
    label = next(c for c in node.children if not c.is_atom and c.name == "hierarchical_label" and c.get_string(0) == "ODD")
    label.find_children("shape")[0].children[0] = write_layer.symbol("dot")  # a shape no sheet pin has
    write_layer.save(node, child)
    before = _bytes(root.parent)
    result = tools["sync_sheet_pins"](str(root), dry_run=dry_run)
    assert result["ok"] is True and result["count"] == 0 and result["written"] is False and result["dry_run"] is dry_run, result
    assert [u["name"] for u in result["unresolved"]["unsupported_directions"]] == ["ODD"], result["unresolved"]
    assert "already has a pin" not in result["note"], result["note"]
    assert _bytes(root.parent) == before


# --------------------------------------------------------------------------- the text an LLM reads


def test_the_tools_are_registered_with_the_pinned_texts():
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import schematic as schematic_toolset

    mcp = MCPServer("t")
    names = schematic_toolset.build(mcp)
    listed = {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert "compare_sheet_pins" in names and "sync_sheet_pins" in names
    assert listed["compare_sheet_pins"].annotations.read_only_hint is True
    assert listed["sync_sheet_pins"].annotations.read_only_hint is False
    cmp_text = " ".join(listed["compare_sheet_pins"].description.split())
    for phrase in ("missing_pins", "missing_labels", "direction_mismatches", "writes nothing"):
        assert phrase in cmp_text, phrase
    sync_text = " ".join(listed["sync_sheet_pins"].description.split())
    for phrase in (write_layer.LOCK_SENTENCE, "connectivity_changed", "dry_run", "unresolved", "never creates or removes a hierarchical label"):
        assert phrase in sync_text, phrase
