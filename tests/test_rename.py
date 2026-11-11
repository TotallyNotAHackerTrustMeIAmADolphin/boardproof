"""rename_net and rename_ref (GitHub issue 96), with real kicad-cli.

Reproduction before the tools: a net or a reference could only be renamed by editing the schematic text
(a label here, a sheet pin there, the instance data of every unit), and a half-applied edit leaves a net
split or a part that two sheets call by different names. No tool renamed a net or a reference across a sheet
tree.

The project (tests/rename_fixture.py) is built once per module through the tools; every test works on a copy.
Mutants, each shown red by a test named in its docstring: skipping the sheet pins, skipping the netlist check,
renaming only the Reference property, dropping the interface check, dropping the rollback, accepting an
existing name, dropping the net-rule check, dropping the shared-sheet refusal.
"""

from __future__ import annotations

import copy
import json
import types
from pathlib import Path

import pytest

from boardproof import kicad_cli, sheet_editing, sheet_rename, write_layer
from tests import rename_fixture as F

SExp = write_layer.SExp


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    return F.build(tmp_path_factory.mktemp("rename-template"))


@pytest.fixture
def root(template, tmp_path) -> Path:
    return F.copy_of(template, tmp_path / "work")


@pytest.fixture
def tools() -> dict:
    return F.tools()


def _texts(path: Path, tag: str) -> list[str]:
    return sorted(c.get_string(0) for c in F.children(path, tag))


def _sub(root: Path) -> Path:
    return root.parent / "sub.kicad_sch"


def _pins(root: Path) -> list[str]:
    sheet = F.children(root, "sheet")[0]
    return sorted(p.get_string(0) for p in sheet.find_children("pin"))


def _power_values(path: Path) -> list[str]:
    return sorted(sheet_editing._property(s, "Value").get_string(1) for s in F.children(path, "symbol")
                  if sheet_editing._property(s, "Reference").get_string(1).startswith("#"))


def _refs(path: Path) -> list[str]:
    return sorted(sheet_editing._property(s, "Reference").get_string(1) for s in F.children(path, "symbol"))


def _entry_refs(path: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for s in F.children(path, "symbol"):
        for inst in s.find_children("instances"):
            for proj in inst.find_children("project"):
                for e in proj.find_children("path"):
                    out.setdefault(sheet_editing._uuid(s), []).append(e.find_children("reference")[0].get_string(0))
    return out


def _others(path: Path, skip: set[str]) -> list[str]:
    return [write_layer.serialise(c, compact=True) for c in write_layer.load(path).children
            if c.is_atom or sheet_editing._uuid(c) not in skip]


# --------------------------------------------------------------------------- rename_net: scopes


def test_a_root_local_label_is_renamed_everywhere_it_stands(root, tools):
    sub_before = F.snapshot(root)["sub.kicad_sch"]
    result = tools["rename_net"](str(root), [{"old": "/SDA", "new": "I2C_SDA"}])
    assert result["ok"] is True and result["written"] is True, result
    assert _texts(root, "label") == ["I2C_SDA", "I2C_SDA"]
    # the sheet pin and the child's hierarchical label are wired to the renamed net and keep their own names
    assert _pins(root) == ["CLK", "SDA"] and "SDA" in _texts(_sub(root), "hierarchical_label")
    assert F.snapshot(root)["sub.kicad_sch"] == sub_before and result["files_changed"] == ["demo.kicad_sch"]
    assert F.nets(root) == {"/I2C_SDA": ["R1.2", "R10.1"], "/Sub/CLK": ["R3.2"], "/Sub/LOC": ["R10.2"], "GL": ["R2.1", "R3.1"],
                            "VCC": ["R1.1"], "unconnected-(R2-Pad2)": ["R2.2"]}
    assert result["connectivity"]["changed"] is False and result["count"] == 2
    assert {t["kind"] for t in result["renamed"][0]["targets"]} == {"label"}


def test_a_hierarchical_net_renames_the_label_and_the_sheet_pins_together(root, tools):
    """Mutant: leave the sheet pins out (`_find_targets` without the pins) -> test_the_interface_check_fires..., this one."""
    result = tools["rename_net"](str(root), [{"old": "/Sub/CLK", "new": "SCL"}])
    assert result["ok"] is True and result["written"] is True, result
    assert _texts(_sub(root), "hierarchical_label") == ["SCL", "SDA"] and _pins(root) == ["SCL", "SDA"]
    assert sorted(result["files_changed"]) == ["demo.kicad_sch", "sub.kicad_sch"]
    assert {t["kind"] for t in result["renamed"][0]["targets"]} == {"hierarchical_label", "sheet_pin"}
    assert F.nets(root)["/Sub/SCL"] == ["R3.2"] and "/Sub/CLK" not in F.nets(root)
    # the pin and its label agree: no pin without a label, no label without a pin
    assert sheet_rename._interface_problems(
        sheet_rename._session(sheet_rename.sheet_authoring._require_tree(root)), {root.resolve(): write_layer.load(root), _sub(root).resolve(): write_layer.load(_sub(root))}) == set()


def test_a_global_net_renames_every_global_label_on_every_sheet(root, tools):
    result = tools["rename_net"](str(root), [{"old": "GL", "new": "GLOB"}])
    assert result["ok"] is True and result["written"] is True, result
    assert _texts(root, "global_label") == ["GLOB"] and _texts(_sub(root), "global_label") == ["GLOB"]
    assert F.nets(root)["GLOB"] == ["R2.1", "R3.1"]


def test_a_power_symbols_value_is_renamed_and_the_library_symbol_is_not(root, tools):
    lib_before = write_layer.serialise(F.children(root, "lib_symbols")[0], compact=True)
    result = tools["rename_net"](str(root), [{"old": "VCC", "new": "+3V3"}])
    assert result["ok"] is True and result["written"] is True, result
    assert _power_values(root) == ["+3V3"]
    assert write_layer.serialise(F.children(root, "lib_symbols")[0], compact=True) == lib_before
    assert F.nets(root)["+3V3"] == ["R1.1"]
    assert result["renamed"][0]["targets"][0]["kind"] == "power_symbol"


def test_a_batch_renames_several_nets_in_one_transaction(root, tools):
    result = tools["rename_net"](str(root), [{"old": "/SDA", "new": "DATA"}, {"old": "GL", "new": "GLOB"}, {"old": "/Sub/LOC", "new": "LOCAL"}])
    assert result["ok"] is True and result["written"] is True and result["count"] == 5, result
    names = F.nets(root)
    assert {"/DATA", "GLOB", "/Sub/LOCAL"} <= set(names) and not {"/SDA", "GL", "/Sub/LOC"} & set(names)


def test_a_dry_run_proves_everything_and_writes_nothing(root, tools):
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "/Sub/CLK", "new": "SCL"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and "would_rename" in result and "renamed" not in result, result
    assert F.snapshot(root) == before


def test_the_other_nodes_of_every_sheet_are_untouched(root, tools):
    skip = {t for t in (sheet_editing._uuid(c) for c in F.children(root, "global_label"))}
    before = _others(root, skip)
    assert tools["rename_net"](str(root), [{"old": "GL", "new": "GLOB"}])["ok"] is True
    assert _others(root, skip) == before


# --------------------------------------------------------------------------- rename_net: refusals


def _refuses(result: dict, code: str, before: dict, root: Path) -> None:
    assert result["ok"] is False and result["code"] == code, result
    assert F.snapshot(root) == before


def test_an_existing_net_name_is_refused_without_merge(root, tools):
    """Mutant: accept an existing name (drop the name_exists test) -> this test and the merge test."""
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "GL", "new": "VCC"}])
    _refuses(result, "name_exists", before, root)
    assert result["existing"] == "VCC" and result["pins_on_existing"] == ["R1.1"]


def test_a_label_without_pins_counts_as_an_existing_name(root, tools):
    assert tools["add_labels"](str(root), [{"kind": "label", "x": 120.65, "y": 120.65, "text": "LONE"}])["status"] == "ok"
    before = F.snapshot(root)
    _refuses(tools["rename_net"](str(root), [{"old": "/SDA", "new": "LONE"}]), "name_exists", before, root)


def test_merge_joins_two_nets_on_purpose_and_says_so(root, tools):
    result = tools["rename_net"](str(root), [{"old": "GL", "new": "VCC"}], merge=True)
    assert result["ok"] is True and result["written"] is True, result
    assert result["connectivity"]["changed"] is True
    assert result["connectivity"]["merged"] == [{"from": "GL", "into": "VCC", "pins_joined": ["R1.1"]}]
    assert F.nets(root)["VCC"] == ["R1.1", "R2.1", "R3.1"] and "GL" not in F.nets(root)


def test_a_rename_chain_and_a_swap_are_not_a_merge(root, tools):
    result = tools["rename_net"](str(root), [{"old": "GL", "new": "VCC"}, {"old": "VCC", "new": "GL"}])
    assert result["ok"] is True and result["written"] is True and "merged" not in result["connectivity"], result
    assert F.nets(root)["VCC"] == ["R2.1", "R3.1"] and F.nets(root)["GL"] == ["R1.1"]


def test_unknown_automatic_and_malformed_requests_refuse(root, tools):
    before = F.snapshot(root)
    _refuses(tools["rename_net"](str(root), [{"old": "/NOPE", "new": "X"}]), "not_found", before, root)
    _refuses(tools["rename_net"](str(root), [{"old": "unconnected-(R2-Pad2)", "new": "X"}]), "net_not_named", before, root)
    _refuses(tools["rename_net"](str(root), [{"old": "/Nope/SDA", "new": "X"}]), "not_found", before, root)
    for bad in ("A/B", "D[0..3]", " X", "X\n", ""):
        result = tools["rename_net"](str(root), [{"old": "GL", "new": bad}])
        assert result["ok"] is False and result["code"] in ("invalid_spec", "bus_name_unsupported"), (bad, result)
    for specs in ([], [{"old": "GL"}], [{"old": "GL", "new": "X", "extra": 1}], [{"old": "GL", "new": "X"}, {"old": "GL", "new": "Y"}], "GL"):
        assert tools["rename_net"](str(root), specs)["code"] == "invalid_spec", specs
    assert F.snapshot(root) == before


def test_a_net_that_no_label_carries_is_refused(root, tools, monkeypatch):
    """Mutant: drop the no_label_carries_net test -> a rename that changes nothing would be written."""
    monkeypatch.setattr(sheet_rename, "_find_targets", lambda *a, **k: [])
    before = F.snapshot(root)
    _refuses(tools["rename_net"](str(root), [{"old": "GL", "new": "GLOB"}]), "no_label_carries_net", before, root)


def test_a_sheet_used_twice_cannot_have_a_local_name_renamed_for_one_use(root, tools):
    """Mutant: drop the sheet_used_more_than_once refusal -> /Sub/LOC would rename /Sub2/LOC too."""
    top = write_layer.load(root)
    sheet = next(c for c in top.children if not c.is_atom and c.name == "sheet")
    twin = copy.deepcopy(sheet)
    write_layer.set_string(twin.find_children("uuid")[0], 0, "99999999-9999-4999-8999-999999999999")
    for prop in twin.find_children("property"):
        if prop.get_string(0) == "Sheetname":
            write_layer.set_string(prop, 1, "Sub2")
    for pin in twin.find_children("pin"):
        write_layer.set_string(pin.find_children("uuid")[0], 0, "99999999-9999-4999-8999-9999999999" + ("01" if pin.get_string(0) == "SDA" else "02"))
    top.children.insert(top.children.index(sheet) + 1, twin)
    write_layer.save(top, root)
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "/Sub/LOC", "new": "LOCAL"}])
    _refuses(result, "sheet_used_more_than_once", before, root)
    assert sorted(result["uses"]) == ["/Sub/", "/Sub2/"]


def _set_project(root: Path, **settings) -> None:
    pro = root.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text(encoding="utf-8"))
    data.setdefault("net_settings", {}).update(settings)
    write_layer.save_project(data, pro)


def test_a_net_class_rule_that_would_be_orphaned_refuses(root, tools):
    """Mutant: drop the rule check -> the pattern /SDA would stop matching and nothing says so."""
    _set_project(root, netclass_patterns=[{"netclass": "Fast", "pattern": "/SDA"}], netclass_assignments={"/Sub/CLK": "Fast"})
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "/SDA", "new": "DATA"}])
    _refuses(result, "net_rule_orphaned", before, root)
    assert result["orphaned_rules"] == [{"rule": "netclass_pattern", "pattern": "/SDA", "netclass": "Fast", "net": "/SDA"}]
    result = tools["rename_net"](str(root), [{"old": "/Sub/CLK", "new": "SCL"}])
    _refuses(result, "net_rule_orphaned", before, root)
    assert result["orphaned_rules"] == [{"rule": "netclass_assignment", "net": "/Sub/CLK"}]


def test_a_rule_that_still_matches_does_not_block_and_the_override_lists_the_orphan(root, tools):
    _set_project(root, netclass_patterns=[{"netclass": "Fast", "pattern": "/*"}])
    assert tools["rename_net"](str(root), [{"old": "/SDA", "new": "DATA"}])["ok"] is True
    _set_project(root, netclass_patterns=[{"netclass": "Fast", "pattern": "/DATA"}])
    result = tools["rename_net"](str(root), [{"old": "/DATA", "new": "BUS"}], allow_orphaned_net_rules=True)
    assert result["ok"] is True and result["warnings"][-1]["code"] == "net_rule_orphaned", result


def test_a_custom_rules_file_that_mentions_the_net_is_a_warning(root, tools, monkeypatch):
    """The text of the rules file is handed in by a patched reader: a test may not write a .kicad_dru by hand."""
    real = sheet_rename._project_rules
    rules = "(version 1) (rule x (condition \"A.NetName == '/SDA'\") (constraint clearance (min 0.3mm)))"
    monkeypatch.setattr(sheet_rename, "_project_rules", lambda tree: (*real(tree)[:2], rules))
    result = tools["rename_net"](str(root), [{"old": "/SDA", "new": "DATA"}])
    assert result["ok"] is True and [w["code"] for w in result["warnings"]] == ["rules_file_mentions_net"], result


def test_an_unreadable_project_file_refuses(root, tools, monkeypatch):
    def boom(text):
        raise ValueError("not json")

    monkeypatch.setattr(sheet_rename, "json", types.SimpleNamespace(loads=boom))
    before = F.snapshot(root)
    _refuses(tools["rename_net"](str(root), [{"old": "GL", "new": "GLOB"}]), "project_file_unreadable", before, root)


def test_a_sheet_pin_name_that_stays_is_not_a_mismatch(root, tools):
    """The root label renamed, the pin SDA stays wired to it: no refusal from the interface check."""
    assert tools["rename_net"](str(root), [{"old": "/SDA", "new": "DATA"}])["ok"] is True


def test_a_lock_on_a_child_sheet_refuses_the_whole_rename(root, tools):
    sub = _sub(root)
    lock = sub.with_name(f"~{sub.name}.lck")
    lock.write_bytes(b'{"hostname":"other","username":"x"}')
    before = F.snapshot(root)  # the lock file is part of the snapshot
    result = tools["rename_net"](str(root), [{"old": "/Sub/CLK", "new": "SCL"}])
    assert result["ok"] is False and result["code"] == "file_open_in_kicad", result
    assert {k: v for k, v in F.snapshot(root).items() if k != lock.name} == {k: v for k, v in before.items() if k != lock.name}
    lock.unlink()


# --------------------------------------------------------------------------- rename_net: self-checks


def test_a_corrupted_write_is_refused_and_every_file_is_restored(root, tools, monkeypatch):
    real = write_layer.load
    before = F.snapshot(root)

    def corrupt(path):
        node = real(path)
        if ".tmp" in Path(path).name:
            node.children.pop()
        return node

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = tools["rename_net"](str(root), [{"old": "/Sub/CLK", "new": "SCL"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert F.snapshot(root) == before


def test_a_failure_on_the_second_file_restores_the_first(root, tools, monkeypatch):
    """Mutant: drop the rollback -> the parent would keep the new pin name with the child's label unchanged."""
    real = write_layer.save
    calls: list[str] = []

    def flaky(node, path, **kw):
        if Path(path).parent == root.parent:  # the real files; the scratch copies of the proof are saved too
            calls.append(Path(path).name)
            if len(calls) == 2:
                raise write_layer.WriteLayerError("write_failed", "disk full")
        return real(node, path, **kw)

    def trees():
        return {f: write_layer.serialise(write_layer.load(f), compact=True) for f in (root, _sub(root))}

    before = trees()
    monkeypatch.setattr(write_layer, "save", flaky)
    result = tools["rename_net"](str(root), [{"old": "/Sub/CLK", "new": "SCL"}])
    assert result["ok"] is False, result
    monkeypatch.undo()
    # the restore saves the original tree again: the same tree, not necessarily the same bytes (write_layer lays the file out itself)
    assert trees() == before and calls[:2] == ["demo.kicad_sch", "sub.kicad_sch"] and len(calls) == 3, calls  # written, failed, restored


def test_the_staged_check_fires_when_the_rename_touches_an_unselected_node(root, tools, monkeypatch):
    real = write_layer.set_string
    real_number = write_layer.set_number
    seen: dict = {}
    original = sheet_rename._find_targets

    def spy(session, *a, **k):
        seen["session"] = session
        return original(session, *a, **k)

    def stray(node, index, value):
        real(node, index, value)
        symbol = next(c for c in seen["session"].news[root.resolve()].children if not c.is_atom and c.name == "symbol")
        real_number(sheet_editing._property(symbol, "Value").find_children("at")[0], 0, 99.0)  # invisible to the netlist

    monkeypatch.setattr(sheet_rename, "_find_targets", spy)
    monkeypatch.setattr(write_layer, "set_string", stray)
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "GL", "new": "GLOB"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert F.snapshot(root) == before


def test_the_interface_check_fires_when_a_pin_is_left_behind(root, tools, monkeypatch):
    """Mutant: sheet pins left out of the targets. The hierarchical label alone is renamed, the pin keeps CLK."""
    original = sheet_rename._find_targets
    monkeypatch.setattr(sheet_rename, "_find_targets",
                        lambda *a, **k: [t for t in original(*a, **k) if t.kind != "sheet_pin"])
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "/Sub/CLK", "new": "SCL"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["interface_problems"], result
    assert F.snapshot(root) == before


def test_the_netlist_check_fires_when_the_written_name_is_not_the_asked_one(root, tools, monkeypatch):
    """Mutant: no netlist comparison -> a rename that wrote another name would be saved."""
    real = write_layer.set_string

    def wrong(node, index, value):
        real(node, index, value + "X" if value == "GLOB" else value)

    monkeypatch.setattr(write_layer, "set_string", wrong)
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "GL", "new": "GLOB"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "netlist" in result["reason"], result
    assert result["differences"], result
    assert F.snapshot(root) == before


def test_the_netlist_check_fires_when_a_pin_ties_the_old_name(root, tools, monkeypatch):
    """A net can be named by something other than the labels: the check sees a different netlist and refuses."""
    original = sheet_rename._find_targets

    def only_one(*a, **k):
        found = original(*a, **k)
        return found[:1]

    monkeypatch.setattr(sheet_rename, "_find_targets", only_one)
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "GL", "new": "GLOB"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert F.snapshot(root) == before


# --------------------------------------------------------------------------- rename_ref


def test_a_reference_is_renamed_in_the_instance_data_and_the_property(root, tools):
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R7"}])
    assert result["ok"] is True and result["written"] is True, result
    assert "R7" in _refs(root) and "R1" not in _refs(root)
    assert "R7" in sum(_entry_refs(root).values(), []) and "R1" not in sum(_entry_refs(root).values(), [])
    assert F.nets(root)["VCC"] == ["R7.1"] and F.nets(root)["/SDA"] == ["R10.1", "R7.2"]
    assert result["renamed"][0]["units"][0]["reference_property"] == "renamed"
    assert result["connectivity"]["changed"] is False and result["files_changed"] == ["demo.kicad_sch"]


def test_a_reference_on_a_child_sheet_is_renamed_there(root, tools):
    result = tools["rename_ref"](str(root), [{"old": "R10", "new": "R20"}, {"old": "R3", "new": "R30"}])
    assert result["ok"] is True and result["written"] is True and result["files_changed"] == ["sub.kicad_sch"], result
    assert _refs(_sub(root)) == ["R20", "R30"]
    assert F.nets(root)["/Sub/CLK"] == ["R30.2"]


def test_every_unit_of_a_multi_unit_part_on_every_sheet_follows(root, tools):
    spec = {"lib_id": "Amplifier_Operational:LM324", "ref": "U1", "value": "LM324", "y": 150.0}
    assert tools["add_symbols"](str(root), [{**spec, "x": 50.8, "unit": 1}])["status"] == "ok"
    assert tools["add_symbols"](str(_sub(root)), [{**spec, "x": 150.0, "unit": 2}])["status"] == "ok"
    result = tools["rename_ref"](str(root), [{"old": "U1", "new": "U9"}])
    assert result["ok"] is True and result["written"] is True, result
    assert sorted((u["sheet"], u["unit"]) for u in result["renamed"][0]["units"]) == [("demo.kicad_sch", "1"), ("sub.kicad_sch", "2")]
    assert "U9" in _refs(root) and "U9" in _refs(_sub(root)) and "U1" not in _refs(root) + _refs(_sub(root))
    assert any("U9" in name for name in F.nets(root)), F.nets(root)


def test_a_swap_and_a_chain_work_and_a_taken_reference_refuses(root, tools):
    before = F.snapshot(root)
    taken = tools["rename_ref"](str(root), [{"old": "R1", "new": "R2"}])
    _refuses(taken, "reference_exists", before, root)
    swapped = tools["rename_ref"](str(root), [{"old": "R1", "new": "R2"}, {"old": "R2", "new": "R1"}])
    assert swapped["ok"] is True and swapped["written"] is True, swapped
    assert F.nets(root)["VCC"] == ["R2.1"] and F.nets(root)["GL"] == ["R1.1", "R3.1"]


def test_bad_reference_requests_refuse(root, tools):
    before = F.snapshot(root)
    _refuses(tools["rename_ref"](str(root), [{"old": "R99", "new": "R100"}]), "not_found", before, root)
    _refuses(tools["rename_ref"](str(root), [{"old": "#PWR01", "new": "#PWR09"}]), "invalid_spec", before, root)
    for bad in ("R?", "#R1", "R 1", 'R"1', ""):
        assert tools["rename_ref"](str(root), [{"old": "R1", "new": bad}])["code"] == "invalid_spec", bad
    assert tools["rename_ref"](str(root), [{"old": "R1", "new": "R5"}, {"old": "R2", "new": "R5"}])["code"] == "invalid_spec"
    assert F.snapshot(root) == before


def test_an_unchanged_name_writes_nothing(root, tools):
    before = F.snapshot(root)
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R1"}])
    assert result["ok"] is True and result["written"] is False and result["renamed"][0]["unchanged"] is True, result
    assert F.snapshot(root) == before


def test_a_reference_dry_run_writes_nothing(root, tools):
    before = F.snapshot(root)
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R7"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and "would_rename" in result, result
    assert F.snapshot(root) == before


def _twin_the_sub_sheet(root: Path) -> None:
    """Use sub.kicad_sch a second time (sheet Sub2); its symbols get a second instance entry with the references R11 and R4."""
    top = write_layer.load(root)
    root_uuid = top.find_children("uuid")[0].get_string(0)
    sheet = next(c for c in top.children if not c.is_atom and c.name == "sheet")
    twin = copy.deepcopy(sheet)
    twin_uuid = "99999999-9999-4999-8999-999999999999"
    write_layer.set_string(twin.find_children("uuid")[0], 0, twin_uuid)
    for prop in twin.find_children("property"):
        if prop.get_string(0) == "Sheetname":
            write_layer.set_string(prop, 1, "Sub2")
    for n, pin in enumerate(twin.find_children("pin")):
        write_layer.set_string(pin.find_children("uuid")[0], 0, f"99999999-9999-4999-8999-99999999990{n}")
    top.children.insert(top.children.index(sheet) + 1, twin)
    write_layer.save(top, root)
    sheet_uuid = sheet.find_children("uuid")[0].get_string(0)
    sub = write_layer.load(_sub(root))
    for symbol in sub.children:
        if symbol.is_atom or symbol.name != "symbol":
            continue
        for inst in symbol.find_children("instances"):
            project = inst.find_children("project")[0]
            entry = next(e for e in project.find_children("path") if e.get_string(0) == f"/{root_uuid}/{sheet_uuid}")
            second = copy.deepcopy(entry)
            write_layer.set_string(second, 0, f"/{root_uuid}/{twin_uuid}")
            write_layer.set_string(second.find_children("reference")[0], 0, {"R10": "R11", "R3": "R4"}[entry.find_children("reference")[0].get_string(0)])
            project.children.append(second)
    write_layer.save(sub, _sub(root))


def test_a_sheet_used_twice_renames_only_the_use_that_carries_the_old_reference(root, tools):
    """Mutant: rename every entry of the symbol (not only the one at the use's path) -> R11 would become R20 too."""
    _twin_the_sub_sheet(root)
    result = tools["rename_ref"](str(root), [{"old": "R10", "new": "R20"}])
    assert result["ok"] is True and result["written"] is True, result
    entries = sorted(sum(_entry_refs(_sub(root)).values(), []))
    assert entries == ["R11", "R20", "R3", "R4"], entries
    assert [u["sheet_use"] for u in result["renamed"][0]["units"]] == [result["renamed"][0]["units"][0]["sheet_use"]] and len(result["renamed"][0]["units"]) == 1


def test_instance_data_that_cannot_be_shown_refuses(root, tools):
    """Mutant: write the Reference property only when the instance entry is missing -> the netlist would keep R1."""
    top = write_layer.load(root)
    symbol = next(c for c in top.children if not c.is_atom and c.name == "symbol" and sheet_editing._ref(c) == "R1")
    symbol.children[:] = [c for c in symbol.children if c.is_atom or c.name != "instances"]
    write_layer.save(top, root)
    before = F.snapshot(root)
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R7"}])
    assert result["ok"] is False and result["code"] in ("instance_data_unproven", "write_not_verified"), result
    assert F.snapshot(root) == before


def test_the_reference_netlist_check_fires_when_only_the_property_is_renamed(root, tools, monkeypatch):
    """Mutant: rename the Reference property and leave the instance entry. KiCad reads the entry, so the netlist keeps R1."""
    real = write_layer.set_string

    def only_the_property(node, index, value):
        if getattr(node, "name", "") == "reference":
            return
        real(node, index, value)

    monkeypatch.setattr(write_layer, "set_string", only_the_property)
    before = F.snapshot(root)
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R7"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert F.snapshot(root) == before


def test_the_component_check_fires_when_the_rename_also_changes_a_value(root, tools, monkeypatch):
    """Mutant: no component comparison. The selected symbol's Value changes with its reference: the staged check and
    the pins of every net stay equal, only the component table differs."""
    real = write_layer.set_string
    box: dict = {}
    original = sheet_rename._session

    def spy(tree):
        box["session"] = original(tree)
        return box["session"]

    def also_the_value(node, index, value):
        real(node, index, value)
        if value == "R7" and getattr(node, "name", "") == "property":
            symbol = next(c for c in box["session"].news[root.resolve()].children
                          if not c.is_atom and c.name == "symbol" and sheet_editing._ref(c) == "R7")
            real(sheet_editing._property(symbol, "Value"), 1, "99k")

    monkeypatch.setattr(sheet_rename, "_session", spy)
    monkeypatch.setattr(write_layer, "set_string", also_the_value)
    before = F.snapshot(root)
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R7"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "components" in result["reason"], result
    assert F.snapshot(root) == before


def test_a_reference_corrupted_write_is_refused(root, tools, monkeypatch):
    real = write_layer.load
    before = F.snapshot(root)

    def corrupt(path):
        node = real(path)
        if ".tmp" in Path(path).name:
            node.children.pop()
        return node

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R7"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert F.snapshot(root) == before


def test_the_reference_staged_check_fires_when_the_rename_touches_another_symbol(root, tools, monkeypatch):
    real = write_layer.set_string
    box: dict = {}
    original = sheet_rename._session

    def spy(tree):
        box["session"] = original(tree)
        return box["session"]

    def stray(node, index, value):
        real(node, index, value)
        other = next(c for c in box["session"].news[root.resolve()].children
                     if not c.is_atom and c.name == "symbol" and sheet_editing._ref(c) == "R2")
        write_layer.set_number(sheet_editing._property(other, "Value").find_children("at")[0], 0, 99.0)  # invisible to the netlist

    monkeypatch.setattr(sheet_rename, "_session", spy)
    monkeypatch.setattr(write_layer, "set_string", stray)
    before = F.snapshot(root)
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R7"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert F.snapshot(root) == before


def test_a_lock_on_the_root_sheet_refuses_a_reference_rename(root, tools):
    lock = root.with_name(f"~{root.name}.lck")
    lock.write_bytes(b'{"hostname":"other","username":"x"}')
    before = F.snapshot(root)  # the lock file is part of the snapshot
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R7"}])
    assert result["ok"] is False and result["code"] == "file_open_in_kicad", result
    assert {k: v for k, v in F.snapshot(root).items() if k != lock.name} == {k: v for k, v in before.items() if k != lock.name}
    lock.unlink()


# --------------------------------------------------------------------------- review round 1


def test_the_net_component_check_fires_when_a_component_value_differs_after(root, tools, monkeypatch):
    """Mutant: rename_net without the component comparison."""
    real = sheet_rename._nets_and_components
    calls: list[int] = []

    def altered(tree, replace):
        nets, comps = real(tree, replace)
        calls.append(1)
        if replace:
            comps = [{**c, "value": "99k"} if c["ref"] == "R1" else c for c in comps]
        return nets, comps

    monkeypatch.setattr(sheet_rename, "_nets_and_components", altered)
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "GL", "new": "GLOB"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "components" in result["differences"][0], result
    assert F.snapshot(root) == before


def test_a_whole_file_the_call_did_not_select_must_stay_unchanged(root, tools, monkeypatch):
    """Mutant: drop the unselected-file check of _staged_files. A plan that also edits sub.kicad_sch (a change the netlist cannot see)."""
    real = sheet_rename._plan_nets

    def greedy(session, *a, **k):
        out = real(session, *a, **k)
        sub = session.news[_sub(root).resolve()]
        symbol = next(c for c in sub.children if not c.is_atom and c.name == "symbol")
        write_layer.set_number(sheet_editing._property(symbol, "Value").find_children("at")[0], 0, 99.0)
        return out

    monkeypatch.setattr(sheet_rename, "_plan_nets", greedy)
    before = F.snapshot(root)
    result = tools["rename_net"](str(root), [{"old": "/SDA", "new": "DATA"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and "did not select" in result["reason"], result
    assert F.snapshot(root) == before


def test_a_resistor_whose_value_is_the_net_name_is_not_a_power_symbol(root, tools):
    """Mutant: any symbol whose Value is the text counts as a power symbol."""
    assert tools["add_symbols"](str(root), [{"lib_id": "Device:R", "x": 152.4, "y": 152.4, "ref": "R9", "value": "VCC"}])["status"] == "ok"
    result = tools["rename_net"](str(root), [{"old": "VCC", "new": "X"}])
    assert result["ok"] is True and result["written"] is True, result
    values = {sheet_editing._ref(s): sheet_editing._property(s, "Value").get_string(1) for s in F.children(root, "symbol")}
    assert values["R9"] == "VCC" and values["#PWR01"] == "X"


def test_a_restore_that_fails_is_reported_not_swallowed(root, tools, monkeypatch):
    real = write_layer.save
    n: list[str] = []

    def flaky(node, path, **kw):
        if Path(path).parent == root.parent:
            n.append(Path(path).name)
            if len(n) >= 2:
                raise write_layer.WriteLayerError("write_failed", "disk full")
        return real(node, path, **kw)

    monkeypatch.setattr(write_layer, "save", flaky)
    result = tools["rename_net"](str(root), [{"old": "/Sub/CLK", "new": "SCL"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["restore_failed"] == ["demo.kicad_sch"], result
    assert "NOT be restored" in result["reason"]


def test_a_reference_rename_restores_the_first_file_when_the_second_fails(root, tools, monkeypatch):
    real = write_layer.save
    calls: list[str] = []

    def flaky(node, path, **kw):
        if Path(path).parent == root.parent:
            calls.append(Path(path).name)
            if len(calls) == 2:
                raise write_layer.WriteLayerError("write_failed", "disk full")
        return real(node, path, **kw)

    def trees():
        return {f: write_layer.serialise(write_layer.load(f), compact=True) for f in (root, _sub(root))}

    before = trees()
    monkeypatch.setattr(write_layer, "save", flaky)
    result = tools["rename_ref"](str(root), [{"old": "R1", "new": "R7"}, {"old": "R10", "new": "R20"}])
    assert result["ok"] is False, result
    monkeypatch.undo()
    assert trees() == before and len(calls) == 3, calls


def test_an_unannotated_old_reference_is_refused_with_its_own_reason(root, tools):
    result = tools["rename_ref"](str(root), [{"old": "R?", "new": "R7"}])
    assert result["ok"] is False and result["code"] == "invalid_spec" and "unannotated" in result["reason"], result
