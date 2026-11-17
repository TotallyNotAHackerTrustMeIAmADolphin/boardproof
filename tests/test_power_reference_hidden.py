"""Hidden Reference of power symbols and a way to hide or show a Reference (GitHub issue 202).

Reproduction: ``add_symbols`` and ``add_labels`` placed a power symbol (``power:GND``, reference ``#PWR01``) with a
visible Reference text, while every stock KiCad power symbol hides its Reference and KiCad hides it on each power
symbol it places (measured on all installed demo sheets: no ``#`` reference is visible). ``set_properties`` refused
the Reference, so no tool could hide it. Fixtures are built through the tools on ``tmp_path``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_editing, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _fresh(tmp_path, name="p") -> Path:
    return Path(project_toolset._project_create(str(tmp_path / name), "demo")["schematic_path"])


def _ref_prop(sch: Path, ref: str):
    for s in write_layer.load(sch).children:
        if not s.is_atom and s.name == "symbol" and sheet_editing._ref(s) == ref:
            return sheet_editing._property(s, "Reference")
    raise AssertionError(ref)


def _ref_props(sch: Path) -> dict[str, bool]:
    out = {}
    for s in write_layer.load(sch).children:
        if not s.is_atom and s.name == "symbol" and s.find_children("lib_id"):
            out[sheet_editing._ref(s)] = sheet_editing.library_hidden(sheet_editing._property(s, "Reference"))
    return out


def test_add_symbols_hides_power_reference_only(tmp_path, tools):
    sch = _fresh(tmp_path)
    got = tools["add_symbols"](str(sch), [
        {"lib_id": "power:GND", "x": 50.8, "y": 50.8, "ref": "#PWR01"},
        {"lib_id": "power:+5V", "x": 76.2, "y": 50.8, "ref": "#PWR02"},
        {"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R1"},
    ])
    assert got["status"] == "ok", got
    assert _ref_props(sch) == {"#PWR01": True, "#PWR02": True, "R1": False}


def test_add_labels_power_hides_reference(tmp_path, tools):
    sch = _fresh(tmp_path)
    got = tools["add_labels"](str(sch), [{"kind": "power", "text": "power:GND", "x": 50.8, "y": 50.8},
                                         {"kind": "label", "text": "N", "x": 76.2, "y": 50.8}])
    assert got["status"] == "ok", got
    refs = _ref_props(sch)
    assert len(refs) == 1 and all(refs.values())


def test_existing_symbols_are_not_touched(tmp_path, tools):
    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "power:GND", "x": 50.8, "y": 50.8, "ref": "#PWR01"}])["status"] == "ok"
    # make the first one visible by hand through the tool, then add another: the first stays visible
    assert tools["set_properties"](str(sch), [{"reference": "#PWR01", "set": {"Reference": {"hidden": False}}}])["status"] == "ok"
    assert tools["add_symbols"](str(sch), [{"lib_id": "power:+5V", "x": 76.2, "y": 50.8, "ref": "#PWR02"}])["status"] == "ok"
    assert _ref_props(sch) == {"#PWR01": False, "#PWR02": True}


def test_kicad_reads_the_hidden_reference_as_hidden(tmp_path, tools):
    """kicad-cli sch upgrade rewrites the sheet in KiCad's own form: the Reference is still hidden there."""
    import subprocess

    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "power:GND", "x": 50.8, "y": 50.8, "ref": "#PWR01"}])["status"] == "ok"
    subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "upgrade", "--force", str(sch)], check=True, capture_output=True)
    assert _ref_props(sch) == {"#PWR01": True}


def test_set_properties_hides_and_shows_reference_keeping_value(tmp_path, tools):
    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1"}])["status"] == "ok"
    before = _ref_props(sch)
    assert before == {"R1": False}
    got = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": {"hidden": True}}}])
    assert got["status"] == "ok", got
    row = got["updated"][0]["properties"][0]
    assert row["name"] == "Reference" and row["action"] == "changed" and row["hidden"] is True and row["value"] == "R1"
    assert _ref_props(sch) == {"R1": True}
    again = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": {"hidden": True}}}])
    assert again["updated"][0]["properties"][0]["action"] == "unchanged"
    shown = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": {"hidden": False}}}])
    assert shown["status"] == "ok" and _ref_props(sch) == {"R1": False}


def test_noop_hide_writes_nothing(tmp_path, tools):
    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1"}])["status"] == "ok"
    before = sch.read_bytes()
    got = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": {"hidden": False}}}])
    assert got["status"] == "ok"
    assert sch.read_bytes() == before


@pytest.mark.parametrize("spec, code", [
    ("R9", "reference_not_settable"),
    ({"value": "R9"}, "reference_not_settable"),
    ({"value": "R9", "hidden": True}, "reference_not_settable"),
    ({"value": "R1", "hidden": True}, "reference_not_settable"),
    ({}, "invalid_spec"),
    ({"hidden": "yes"}, "invalid_spec"),
    ({"hidden": 1}, "invalid_spec"),
    ({"hidden": True, "angle": 0}, "invalid_spec"),
    (None, "invalid_spec"),
    (True, "invalid_spec"),
])
def test_reference_value_stays_refused(tmp_path, tools, spec, code):
    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1"}])["status"] == "ok"
    before = sch.read_bytes()
    got = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": spec}}])
    assert got["ok"] is False and got["code"] == code, got
    assert sch.read_bytes() == before


def test_hide_reference_with_other_fields_in_one_item(tmp_path, tools):
    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1"}])["status"] == "ok"
    got = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": {"hidden": True}, "LCSC": "C1"}}])
    assert got["status"] == "ok", got
    assert _ref_props(sch) == {"R1": True}
    assert sheet_editing._property(next(s for s in write_layer.load(sch).children if not s.is_atom and s.name == "symbol"
                                        and s.find_children("lib_id")), "LCSC").get_string(1) == "C1"


def test_multi_unit_part_hides_every_unit(tmp_path, tools):
    sch = _fresh(tmp_path)
    got = tools["add_symbols"](str(sch), [
        {"lib_id": "Amplifier_Operational:LM324", "x": 50.8, "y": 50.8, "ref": "U1", "unit": 1},
        {"lib_id": "Amplifier_Operational:LM324", "x": 101.6, "y": 50.8, "ref": "U1", "unit": 2},
    ])
    assert got["status"] == "ok", got
    assert tools["set_properties"](str(sch), [{"reference": "U1", "set": {"Reference": {"hidden": True}}}])["status"] == "ok"
    hidden = [sheet_editing.library_hidden(sheet_editing._property(s, "Reference")) for s in write_layer.load(sch).children
              if not s.is_atom and s.name == "symbol" and s.find_children("lib_id")]
    assert hidden == [True, True]


def test_fix_annotation_keeps_hidden_references(tmp_path, tools):
    """fix_annotation (it repairs power and flag references) runs on a sheet with hidden References and changes no hidden state."""
    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2"},
        {"lib_id": "power:GND", "x": 101.6, "y": 50.8, "ref": "#PWR01"},
    ])["status"] == "ok"
    assert tools["set_properties"](str(sch), [{"reference": "R2", "set": {"Reference": {"hidden": True}}}])["status"] == "ok"
    before = _ref_props(sch)
    assert before == {"R1": False, "R2": True, "#PWR01": True}
    tools["fix_annotation"](str(sch))
    assert _ref_props(sch) == before


def test_add_labels_power_leaves_visible_old_power_reference_alone(tmp_path, tools):
    """Only the symbols this call adds are hidden: an old power symbol made visible stays visible after add_labels."""
    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "power:GND", "x": 50.8, "y": 50.8, "ref": "#PWR01"}])["status"] == "ok"
    assert tools["set_properties"](str(sch), [{"reference": "#PWR01", "set": {"Reference": {"hidden": False}}}])["status"] == "ok"
    got = tools["add_labels"](str(sch), [{"kind": "power", "text": "power:+5V", "x": 76.2, "y": 50.8}])
    assert got["status"] == "ok", got
    refs = _ref_props(sch)
    assert len(refs) == 2 and refs["#PWR01"] is False and [h for r, h in refs.items() if r != "#PWR01"] == [True], refs


@pytest.mark.parametrize("start_hidden, ask", [(False, False), (True, True)])
def test_noop_reference_request_is_reported_as_not_written(tmp_path, tools, start_hidden, ask):
    """A request that matches the current state (also a repeated one) says written False, an `updated` row with action unchanged, and the note 'nothing was written'."""
    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1"}])["status"] == "ok"
    if start_hidden:
        assert tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": {"hidden": True}}}])["status"] == "ok"
    before = sch.read_bytes()
    got = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": {"hidden": ask}}}])
    assert got["status"] == "ok" and got["written"] is False, got
    assert sch.read_bytes() == before
    assert got["updated"][0]["properties"][0]["action"] == "unchanged" and "nothing was written" in got["note"], got
    dry = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": {"hidden": not ask}}}], dry_run=True)
    assert dry["status"] == "ok" and dry["written"] is False and "would_update" in dry and sch.read_bytes() == before, dry
    real = tools["set_properties"](str(sch), [{"reference": "R1", "set": {"Reference": {"hidden": not ask}}}])
    assert real["written"] is True and sch.read_bytes() != before


def test_replace_symbols_keeps_the_reference_hidden_state(tmp_path, tools):
    sch = _fresh(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1"},
                                           {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2"}])["status"] == "ok"
    assert tools["set_properties"](str(sch), [{"reference": "R2", "set": {"Reference": {"hidden": True}}}])["status"] == "ok"
    got = tools["replace_symbols"](str(sch), [{"reference": "R1", "lib_id": "Device:R_Small"}, {"reference": "R2", "lib_id": "Device:R_Small"}])
    assert got["status"] == "ok", got
    assert _ref_props(sch) == {"R1": False, "R2": True}


def test_tool_text_and_skill_say_it():
    text = schematic_toolset.build.__module__  # keep the module imported for the pins below
    assert text
    root = Path(__file__).resolve().parent.parent
    skill = (root / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8")
    src = (root / "src" / "boardproof" / "toolsets" / "schematic.py").read_text(encoding="utf-8")
    assert "Reference hidden (issue 202)" in src
    assert "its #PWR Reference text is hidden, issue 202" in src
    assert "There is deliberately no opt-out" in src
    assert "its Reference text and its hidden state stay as they are" in src
    assert "there is deliberately no opt-out" in skill
    assert "set: {Reference: {hidden: true|false}}" in src
    assert "A placed power symbol (a Reference starting with #) has its Reference text hidden by `add_symbols` and `add_labels`" in skill
    assert '"Reference": {"hidden": true}' in skill
