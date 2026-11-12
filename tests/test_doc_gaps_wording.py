"""Doc and tool-text gaps from the downstream project's feedback (issue 77, items 9 to 16).

Wording pins read the two SKILL.md files; the two behaviour tests measure what a pinned sentence claims
(kicad-cli ERC of a hierarchical label on the root sheet; the frame of ``set_property_positions`` offsets) on
tmp_path projects made by ``project_create``, written through the registered tools.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_authoring
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

REPO = Path(__file__).resolve().parents[1]
SCHEMATIC = " ".join((REPO / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8").split())
SOURCING = " ".join((REPO / ".claude" / "skills" / "kicad-sourcing" / "SKILL.md").read_text(encoding="utf-8").split())


def _item(text: str, number: int) -> str:
    start = text.index(f"{number}. **")
    end = text.find(f" {number + 1}. **", start)
    return text[start:end if end > 0 else None]


def _tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def _cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


# ---------------------------------------------------------------- wording pins


def test_item_10_sub_sheet_build_and_reattach_are_told_in_the_hierarchy_item():
    item = _item(SCHEMATIC, 7)
    assert "`create_sheets` (blank files) and `add_sheet_symbols`" in item
    assert "symbols already placed in the child are re-attached to the new use path (`symbols_reattached`)" in item


def test_item_13_a_hier_label_on_the_root_is_told_with_its_erc_error():
    item = _item(SCHEMATIC, 7)
    assert "A `hier` label belongs on a child sheet." in item
    assert "`add_labels` also accepts one on the root sheet" in item
    assert "(`pin_not_connected`, a hierarchical label on the root sheet has no parent)" in item
    assert "name a net on the root sheet with kind `label` or `global`" in item


def test_item_16_parallel_writes_are_told():
    item = _item(SCHEMATIC, 2)
    assert "Calls that write the same file wait for each other" in item
    assert "up to 60 s" in item and "`file_busy`" in item
    assert "the order of calls sent in parallel is not defined" in item
    assert "one after another" in item


def test_item_15_the_offset_frame_of_property_texts_is_told():
    item = _item(SCHEMATIC, 9)
    assert "`offset` is measured in sheet axes from the symbol's origin, not in the symbol's turned frame" in item


def test_item_11_adoption_does_not_check_the_land_pattern():
    assert "None of these checks compares the pad positions or sizes with the land pattern in the datasheet" in SOURCING
    assert "do that yourself (`inspect_datasheet`) before you rely on a fetched footprint" in SOURCING


def test_the_60_second_claim_is_the_lock_timeout_of_the_write_layer():
    from boardproof import write_layer

    assert write_layer.LOCK_TIMEOUT_S == 60.0


# ---------------------------------------------------------------- measured claims


def test_a_hier_label_on_the_root_sheet_is_placed_and_kicad_cli_errors_it(tmp_path, _cli):
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    tools = _tools()
    placed = tools["add_labels"](str(root), [{"kind": "hier", "x": 88.9, "y": 53.34, "text": "SIG", "shape": "input", "snap": False}])
    assert placed["status"] == "ok", placed
    census = sheet_authoring.erc_census(root)
    assert census[("pin_not_connected", "error")] == 1, census  # the sentence in the skill

    other = Path(project_toolset._project_create(str(tmp_path / "proj2"), "demo")["schematic_path"])
    tools["add_labels"](str(other), [{"kind": "label", "x": 88.9, "y": 53.34, "text": "SIG", "snap": False}])
    assert ("pin_not_connected", "error") not in sheet_authoring.erc_census(other)  # kind label does not raise it


def test_property_text_offset_is_in_sheet_axes_for_a_turned_symbol(tmp_path):
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    tools = _tools()
    placed = tools["add_symbols"](str(root), [{"lib_id": "Device:R", "x": 100.33, "y": 140.8, "rotation": 90, "ref": "R91",
                                               "value": "1k", "snap": False}])
    assert placed["status"] == "ok", placed
    done = tools["set_property_positions"](str(root), [{"references": ["R91"], "properties": {"Reference": {"offset": [5, 0]}}}])
    assert done["status"] == "ok", done
    raw = root.read_text(encoding="utf-8")
    match = re.search(r'\(property "Reference" "R91"[^\n]*\n\s*\(at ([\d.]+) ([\d.]+) ', raw)
    assert match is not None
    assert (float(match.group(1)), float(match.group(2))) == pytest.approx((105.33, 140.8))


def test_item_9_does_not_claim_a_netlist_comparison_for_set_property_positions():
    """Review of ticket 77: set_property_positions makes no netlist comparison (its description says so)."""
    assert "Every call except `set_property_positions` (it moves no net) compares" in _item(SCHEMATIC, 9)
