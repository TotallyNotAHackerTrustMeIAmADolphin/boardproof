"""dry_run on add_symbols, add_wires, add_labels and connect_pins, with a net-change preview (GitHub issue 124).

A peer project asked for a preview before a placement or a wire is written. ``dry_run: true`` runs the very code
path of the real call (the same validation, the same staged text, the same self-check against kicad-cli's netlist)
and stops before the sheet is replaced: it writes nothing, says ``written: false`` and returns ``nets``, the net
change of the staged sheet against the sheet on disk (created, merged, renamed, extended, split, removed), read
from kicad-cli's netlist of scratch copies. The real call returns the same ``nets``.

Fixtures are built through the tools on ``tmp_path`` (never a raw text edit); the base project is made once per
module and copied per test (a folder copy, no kicad-cli run).
"""

from __future__ import annotations

import shutil
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


def _snapshot(folder: Path) -> dict[str, bytes]:
    """Every file below ``folder`` by relative name, with its bytes: a leftover scratch or backup file shows up."""
    return {str(p.relative_to(folder)): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def base(tmp_path_factory) -> dict:
    """A root sheet with R1 and R2 placed side by side, nothing wired."""
    folder = tmp_path_factory.mktemp("dry") / "proj"
    sheet = Path(project_toolset._project_create(str(folder), "demo")["schematic_path"])
    placed = _tools()["add_symbols"](str(sheet), [
        {"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R1", "value": "1k"},
        {"lib_id": "Device:R", "x": 127.0, "y": 50.8, "ref": "R2", "value": "2k"}])
    assert placed["status"] == "ok", placed
    pins = {(s["reference"], p["number"]): (p["x"], p["y"]) for s in placed["symbols"] for p in s["pins"]}
    return {"folder": folder, "sheet": sheet.name, "pins": pins}


@pytest.fixture
def work(base, tmp_path) -> tuple[Path, Path, dict]:
    """(folder, sheet, pins) of a fresh copy of the base project."""
    copy = tmp_path / "proj"
    shutil.copytree(base["folder"], copy)
    return copy, copy / base["sheet"], base["pins"]


def _calls(pins: dict) -> dict[str, tuple[str, dict]]:
    r1_2, r2_2 = pins[("R1", "2")], pins[("R2", "2")]
    return {
        "add_symbols": ("add_symbols", {"specs": [{"lib_id": "Device:R", "x": 152.4, "y": 50.8, "ref": "R3", "value": "3k"}]}),
        "add_labels": ("add_labels", {"specs": [{"kind": "label", "text": "SIG", "x": r1_2[0], "y": r1_2[1]}]}),
        "add_wires": ("add_wires", {"specs": [{"p1": list(r1_2), "p2": list(r2_2)}]}),
        "connect_pins": ("connect_pins", {"connections": [{"from": "R1.1", "to": "R2.1"}]}),
    }


CASES = ["add_symbols", "add_labels", "add_wires", "connect_pins"]


def _run(tools, case: str, sheet: Path, pins: dict, **extra) -> dict:
    name, args = _calls(pins)[case]
    return tools[name](str(sheet), **args, **extra)


# ---------------------------------------------------------------- writes nothing


@pytest.mark.parametrize("case", CASES)
def test_dry_run_leaves_the_folder_byte_identical(work, case):
    folder, sheet, pins = work
    before = _snapshot(folder)
    result = _run(_tools(), case, sheet, pins, dry_run=True)
    assert result.get("status") == "ok", result
    assert result["written"] is False and result["dry_run"] is True
    assert _snapshot(folder) == before  # same names (no scratch or backup file) and same bytes


@pytest.mark.parametrize("case", CASES)
def test_the_real_call_says_written_and_returns_the_same_net_change(work, tmp_path_factory, case, base):
    folder, sheet, pins = work
    other = tmp_path_factory.mktemp("real") / "proj"
    shutil.copytree(base["folder"], other)
    dry = _run(_tools(), case, sheet, pins, dry_run=True)
    real = _run(_tools(), case, other / base["sheet"], pins)
    assert real["status"] == "ok", real
    assert real["written"] is True
    assert dry["nets"] == real["nets"]
    assert dry["nets"]["changed"] is True


# ---------------------------------------------------------------- what the net change says


def test_add_symbols_creates_the_nets_of_the_new_pins(work):
    _folder, sheet, pins = work
    nets = _run(_tools(), "add_symbols", sheet, pins, dry_run=True)["nets"]
    assert sorted(p for n in nets["created"] for p in n["pins"]) == ["R3.1", "R3.2"]
    assert not nets["merged"] and not nets["renamed"] and not nets["split"] and not nets["removed"]
    assert nets["nets_after"] == nets["nets_before"] + 2


def test_add_symbols_dry_run_lists_the_symbols_that_would_be_written(work):
    _folder, sheet, pins = work
    result = _run(_tools(), "add_symbols", sheet, pins, dry_run=True)
    assert result["would_place"] == ["R3"] and "placed" not in result
    assert [s["reference"] for s in result["symbols"]] == ["R3"] and result["symbols"][0]["pins"]


def test_add_labels_renames_the_net_of_the_pin_it_lands_on(work):
    _folder, sheet, pins = work
    nets = _run(_tools(), "add_labels", sheet, pins, dry_run=True)["nets"]
    assert [(r["net"], r["now"]) for r in nets["renamed"]] == [("unconnected-(R1-Pad2)", "/SIG")], nets
    assert not nets["created"] and not nets["merged"]


def test_add_wires_merges_the_nets_of_the_two_pins(work):
    _folder, sheet, pins = work
    result = _run(_tools(), "add_wires", sheet, pins, dry_run=True)
    assert result["would_add"] == {"wires": 1, "segments": 1}
    assert [m["nets"] for m in result["nets"]["merged"]] == [["unconnected-(R1-Pad2)", "unconnected-(R2-Pad2)"]]
    assert result["nets"]["nets_after"] == result["nets"]["nets_before"] - 1


def test_connect_pins_merges_the_nets_of_the_two_pins(work):
    _folder, sheet, pins = work
    result = _run(_tools(), "connect_pins", sheet, pins, dry_run=True)
    assert "would_connect" in result
    assert [m["nets"] for m in result["nets"]["merged"]] == [["unconnected-(R1-Pad1)", "unconnected-(R2-Pad1)"]]


def test_a_symbol_placed_on_pins_extends_the_nets(work):
    """A symbol placed on an existing net, by a label: the net keeps its name and gains pins (extended)."""
    _folder, sheet, pins = work
    tools = _tools()
    r2_2 = pins[("R2", "2")]
    assert tools["add_labels"](str(sheet), [{"kind": "label", "text": "BUS", "x": r2_2[0], "y": r2_2[1]}])["status"] == "ok"
    # R3 with its pin 2 on that pin point: it joins the net BUS
    result = tools["add_symbols"](str(sheet), [{"lib_id": "Device:R", "x": r2_2[0], "y": r2_2[1] - 3.81, "ref": "R3", "rotation": 0}],
                                  dry_run=True)
    nets = result["nets"]
    by_net = {e["net"]: e["added_pins"] for e in nets["extended"]}
    # R3's pin 2 is on the label's point; its pin 1 lands on R2's pin 1, whose net grows too
    assert by_net["/BUS"] == ["R3.2"] and by_net["Net-(R2-Pad1)"] == ["R3.1"], nets
    assert not nets["created"] and not nets["merged"]


# ---------------------------------------------------------------- the same refusals


REFUSALS = [
    ("add_symbols", {"specs": [{"lib_id": "Device:R", "x": 1, "y": 1}]}),                       # missing ref: invalid_spec
    ("add_symbols", {"specs": [{"lib_id": "Device:R", "x": 1, "y": 1, "ref": "R9", "nope": 1}]}),  # unknown key
    ("add_symbols", {"specs": [{"lib_id": "NoSuchLib:R", "x": 1, "y": 1, "ref": "R9"}]}),        # unknown library
    ("add_symbols", {"specs": [{"lib_id": "Device:R", "x": 1, "y": 1, "ref": "R9", "value": "a\rb"}]}),  # carriage return
    ("add_labels", {"specs": [{"kind": "bogus", "text": "A", "x": 1, "y": 1}]}),                 # unknown kind
    ("add_labels", {"specs": [{"kind": "power", "text": "NoSuchLib:GND", "x": 1, "y": 1}]}),
    ("add_wires", {"specs": [{"p1": [0, 0], "p2": [10, 10], "route": "zigzag"}]}),               # no wire could be added
    ("connect_pins", {"connections": [{"from": "R1.9", "to": "R2.1"}]}),                          # pin_not_found
    ("connect_pins", {"connections": [{"from": "R7.1", "to": "R2.1"}]}),                          # not_found
]


@pytest.mark.parametrize(("name", "args"), REFUSALS, ids=[f"{n}-{i}" for i, (n, _a) in enumerate(REFUSALS)])
def test_a_dry_run_is_refused_exactly_as_the_real_call_is(work, name, args):
    folder, sheet, _pins = work
    tools = _tools()
    before = _snapshot(folder)
    dry = tools[name](str(sheet), **args, dry_run=True)
    real = tools[name](str(sheet), **args)
    assert dry.get("status") != "ok", dry
    assert dry == real  # the same refusal, the same code and text
    assert _snapshot(folder) == before


def test_a_missing_file_is_refused_in_dry_run(tmp_path):
    tools = _tools()
    for name, args in (("add_symbols", {"specs": []}), ("add_labels", {"specs": []}), ("add_wires", {"specs": []}),
                       ("connect_pins", {"connections": []})):
        dry = tools[name](str(tmp_path / "none.kicad_sch"), **args, dry_run=True)
        assert dry == tools[name](str(tmp_path / "none.kicad_sch"), **args), name


@pytest.mark.parametrize("case", ["add_symbols", "add_labels", "add_wires"])
def test_the_self_check_fires_in_a_dry_run_on_a_corrupted_stage(work, monkeypatch, case):
    """A write that loses a node of the sheet is refused as write_not_verified, dry run or not."""
    folder, sheet, pins = work
    real_text = schematic_toolset._schematic_text

    def corrupt(sch):
        root = write_layer.parse_string(real_text(sch))
        victim = next(c for c in root.children if not c.is_atom and c.name == "symbol" and c.find("uuid") is not None)
        root.children.remove(victim)
        return write_layer.serialise(root) + chr(10)

    monkeypatch.setattr(schematic_toolset, "_schematic_text", corrupt)
    before = _snapshot(folder)
    tools = _tools()
    dry = _run(tools, case, sheet, pins, dry_run=True)
    real = _run(tools, case, sheet, pins)
    for result in (dry, real):
        assert result["ok"] is False and result["error"] == "write_not_verified", result
    assert dry == real
    assert _snapshot(folder) == before


def test_connect_pins_dry_run_refuses_a_connectivity_change_as_the_real_call_does(work):
    folder, sheet, pins = work
    tools = _tools()
    for text, ref in (("A", "R1"), ("B", "R2")):
        x, y = pins[(ref, "1")]
        assert tools["add_labels"](str(sheet), [{"kind": "label", "text": text, "x": x, "y": y}])["status"] == "ok"
    before = _snapshot(folder)
    args = {"connections": [{"from": "R1.1", "to": "R2.1"}]}
    dry = tools["connect_pins"](str(sheet), **args, dry_run=True)
    real = tools["connect_pins"](str(sheet), **args)
    assert dry["ok"] is False and dry["code"] == "connectivity_changed", dry
    assert real["code"] == "connectivity_changed"
    assert dry["connectivity"] == real["connectivity"]
    assert _snapshot(folder) == before


def test_a_dry_run_on_a_sheet_whose_tree_cannot_prove_the_instance_data_is_refused(tmp_path):
    """Issue 187: the staged symbols' instance paths cannot be proven, so the dry run refuses like the real call."""
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    assert sheet_authoring.create_sheets(str(root), [{"file": "a.kicad_sch"}, {"file": "b.kicad_sch"}])["status"] == "ok"
    for name, y in (("a.kicad_sch", 50.8), ("b.kicad_sch", 101.6)):
        assert sheet_authoring.add_sheet_symbols(
            str(root), [{"file": name, "name": name[0].upper(), "x": 101.6, "y": y, "pins": PIN}])["status"] == "ok"
    (root.parent / "b.kicad_sch").unlink()
    assert sheet_tree.walk(root).problems
    sub = root.parent / "a.kicad_sch"
    tools = _tools()
    specs = [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R3", "value": "3k"}]
    before = _snapshot(tmp_path)
    dry = tools["add_symbols"](str(sub), specs, dry_run=True)
    assert dry["ok"] is False and dry["code"] == "unproven_instance_data", dry
    power = [{"kind": "power", "text": "power:GND", "x": 101.6, "y": 76.2}]
    assert tools["add_labels"](str(sub), power, dry_run=True)["code"] == "unproven_instance_data"
    assert tools["add_symbols"](str(sub), specs)["code"] == "unproven_instance_data"
    assert _snapshot(tmp_path) == before


def test_a_dry_run_reads_the_sheet_while_kicad_holds_it_and_the_real_call_is_refused(work):
    """The sheet is not written by a dry run, so the lock of an open KiCad does not stop a preview (the convention of
    the other dry runs); the real call is still refused with file_open_in_kicad."""
    folder, sheet, pins = work
    lock = folder / f"~{sheet.name}.lck"
    lock.write_text('{"hostname":"x","username":"y"}')
    tools = _tools()
    assert _run(tools, "add_labels", sheet, pins, dry_run=True)["status"] == "ok"
    assert _run(tools, "add_labels", sheet, pins)["code"] == "file_open_in_kicad"


def test_dry_run_must_be_a_boolean_it_is_not_a_seam_for_the_real_write(work):
    """A dry run never writes, whatever else is asked: the flag is the only switch and it has no override."""
    folder, sheet, pins = work
    before = _snapshot(folder)
    result = _run(_tools(), "add_symbols", sheet, pins, dry_run=True)
    assert result["written"] is False
    assert _snapshot(folder) == before


# ---------------------------------------------------------------- scope of the net change on a sub-sheet (review B1)


def test_on_a_sub_sheet_the_net_change_says_it_is_read_with_the_sheet_as_root(tmp_path):
    """The census of a sheet used by a parent walks the tree only downwards, so a join through the parent (here a
    global label GG on the root) is not seen: the preview says its scope is the sheet, never the project."""
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    tools = _tools()
    assert sheet_authoring.create_sheets(str(root), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    assert sheet_authoring.add_sheet_symbols(
        str(root), [{"file": "sub.kicad_sch", "name": "S", "x": 101.6, "y": 101.6, "pins": PIN}])["status"] == "ok"
    sub = root.parent / "sub.kicad_sch"
    placed = tools["add_symbols"](str(sub), [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R5"}])
    assert placed["status"] == "ok", placed
    assert placed["nets"]["scope"] == "sheet" and "parent" in placed["nets"]["scope_note"]
    pin = next(p for p in placed["symbols"][0]["pins"] if p["number"] == "1")
    label = [{"kind": "global", "text": "GG", "x": pin["x"], "y": pin["y"]}]
    dry = tools["add_labels"](str(sub), label, dry_run=True)
    assert dry["nets"]["scope"] == "sheet"
    assert dry["nets"] == tools["add_labels"](str(sub), label)["nets"]
    top = tools["add_symbols"](str(root), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1"}])
    assert top["nets"]["scope"] == "project"  # a root sheet no parent uses: the tree below it is the project


def test_connect_pins_reads_the_net_change_from_the_project_root(work):
    _folder, sheet, pins = work
    assert _run(_tools(), "connect_pins", sheet, pins, dry_run=True)["nets"]["scope"] == "project"


# ---------------------------------------------------------------- net_change itself (review B2)


def _n(*pins):
    return frozenset(tuple(p.split(".")) for p in pins)


def test_net_change_classes_each_kind_of_change():
    from boardproof.sheet_editing import MAX_LISTED, net_change

    # automatic to automatic name: a join, never a rename; the new pin extends
    got = net_change({"unconnected-(R1-Pad1)": _n("R1.1")}, {"Net-(R1-Pad1)": _n("R1.1", "R2.1")})
    assert got["renamed"] == [] and [e["added_pins"] for e in got["extended"]] == [["R2.1"]] and got["changed"]
    # a named net under another name is a rename
    got = net_change({"/A": _n("R1.1", "R2.1")}, {"/B": _n("R1.1", "R2.1")})
    assert [(r["net"], r["now"]) for r in got["renamed"]] == [("/A", "/B")] and not got["extended"]
    # two nets on one
    got = net_change({"/A": _n("R1.1"), "/B": _n("R2.1")}, {"/A": _n("R1.1", "R2.1")})
    assert got["merged"] == [{"nets": ["/A", "/B"], "now": "/A", "pins": ["R1.1", "R2.1"]}] and not got["split"]
    # one net on two
    got = net_change({"/A": _n("R1.1", "R2.1")}, {"/A": _n("R1.1"), "/B": _n("R2.1")})
    assert got["split"] == [{"net": "/A", "pins": ["R1.1", "R2.1"], "now_on": ["/A", "/B"]}] and not got["merged"]
    # a net with no pin left
    got = net_change({"/A": _n("R1.1"), "/B": _n("R2.1")}, {"/B": _n("R2.1")})
    assert got["removed"] == [{"net": "/A", "pins": ["R1.1"]}] and got["changed"]
    # nothing changed
    same = {"/A": _n("R1.1")}
    assert net_change(same, dict(same))["changed"] is False
    # the lists are bounded and say so
    many = {f"/N{i}": _n(f"R{i}.1") for i in range(MAX_LISTED + 5)}
    got = net_change({}, many)
    assert len(got["created"]) == MAX_LISTED and got["truncated"] is True
    assert net_change({}, {f"/N{i}": _n(f"R{i}.1") for i in range(MAX_LISTED)})["truncated"] is False


# ---------------------------------------------------------------- staging leaves nothing beside the sheet (review B3)


@pytest.mark.parametrize("case", ["add_symbols", "add_labels", "add_wires"])
def test_a_dry_run_stages_nothing_beside_the_sheet_while_it_checks(work, monkeypatch, case):
    folder, sheet, pins = work
    names = set(_snapshot(folder))
    seen: list[set[str]] = []
    real = schematic_toolset._verify_schematic_write

    def spy(path, written, *args, **kwargs):
        seen.append(set(_snapshot(folder)))
        assert Path(written).parent != folder  # the staged text is outside the project folder
        return real(path, written, *args, **kwargs)

    monkeypatch.setattr(schematic_toolset, "_verify_schematic_write", spy)
    assert _run(_tools(), case, sheet, pins, dry_run=True)["status"] == "ok"
    assert seen and all(s == names for s in seen)


# ---------------------------------------------------------------- tool text (review F1, F2, F3)


def test_the_dry_run_text_of_the_four_tools_says_what_it_does():
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    texts = {t.name: t.description for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    for name in ("add_symbols", "add_wires", "add_labels"):
        text = " ".join(texts[name].split())
        for sentence in (
            "dry_run (GitHub issue 124): true runs this call's own code path up to the write and stops there.",
            "writes nothing (no file, no backup, no scratch file beside the sheet",
            "`written` false, `dry_run` true and `would_",
            "the lock file of an open KiCad does not stop a preview (nothing is written), while the real call is refused with file_open_in_kicad",
            "`nets`, the net change",
            "scope \"sheet\"", "cross-sheet joins through a parent sheet are not seen",
            "Net names are kicad-cli's choice",
            "split and removed (a refusal when they occur, as before)",
        ):
            assert sentence in text, (name, sentence)
    wires = " ".join(texts["add_wires"].split())
    assert "(`nets` is not returned when no wire could be added)" in wires
    pins = " ".join(texts["connect_pins"].split())
    for sentence in ("written false, dry_run true", "the lock file of an open KiCad aside", "`nets`", "scope \"project\"",
                     "split and removed are listed, and refused unless allow_connectivity_change is true", "Net names are kicad-cli's choice"):
        assert sentence in pins, sentence


def test_add_wires_without_specs_in_dry_run_says_it_would_add_nothing(work):
    folder, sheet, _pins = work
    before = _snapshot(folder)
    result = _tools()["add_wires"](str(sheet), [], dry_run=True)
    assert result["status"] == "ok" and result["written"] is False and result["would_add"] == {"wires": 0, "segments": 0}
    assert "nets" not in result and _snapshot(folder) == before


# ---------------------------------------------------------------- unknown or unproven means "sheet" (delta review N1)


def _two_subs(tmp_path) -> tuple[Path, Path, list]:
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    tools = _tools()
    assert sheet_authoring.create_sheets(str(root), [{"file": "sub.kicad_sch"}, {"file": "other.kicad_sch"}])["status"] == "ok"
    for name, y in (("sub.kicad_sch", 50.8), ("other.kicad_sch", 101.6)):
        assert sheet_authoring.add_sheet_symbols(
            str(root), [{"file": name, "name": name[:3].upper(), "x": 101.6, "y": y, "pins": PIN}])["status"] == "ok"
    sub = root.parent / "sub.kicad_sch"
    placed = tools["add_symbols"](str(sub), [{"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R5"}])
    pin = next(p for p in placed["symbols"][0]["pins"] if p["number"] == "1")
    return root, sub, [{"kind": "global", "text": "GG", "x": pin["x"], "y": pin["y"]}]


def test_a_project_that_does_not_read_whole_gives_scope_sheet(tmp_path):
    root, sub, label = _two_subs(tmp_path)
    (root.parent / "other.kicad_sch").unlink()  # the root names a sheet file that is gone
    assert sheet_tree.walk(root).problems
    nets = _tools()["add_labels"](str(sub), label, dry_run=True)["nets"]
    assert nets["scope"] == "sheet" and nets["scope_note"]


def test_a_failing_project_lookup_gives_scope_sheet(tmp_path, monkeypatch):
    _root, sub, _label = _two_subs(tmp_path)

    def boom(_path):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(sheet_authoring, "discover_projects", boom)
    scope = schematic_toolset._net_scope(sub)
    assert scope["scope"] == "sheet" and scope["scope_note"]
