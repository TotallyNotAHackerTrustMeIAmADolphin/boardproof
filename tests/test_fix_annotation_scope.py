"""fix_annotation changes only the references of stock ``power:`` symbols that are real
duplicates or malformed, never the reference of any other symbol (issue 35).

kicad_tools 0.25's ``run_fix_annotation`` treats every symbol whose library entry carries the
``(power)`` flag as a power symbol and renumbers 33 references of the unmodified
``complex_hierarchy`` demo (``#PWR0110`` to ``#PWR01``); 0.24.0 changes none. The tool pins the
library's predicate to the stock-library rule and checks the library's own plan before it writes.
Every test grades with raw sheet bytes or the tool's refusal, on copies of a KiCad demo.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import write_layer as wl


@pytest.fixture
def sch_tools() -> dict:
    from boardproof.toolsets import schematic

    mcp = MCPServer("test")
    schematic.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}


def _sheet_bytes(project: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(project.glob("*.kicad_sch"))}


def test_an_annotated_project_with_flagged_library_power_symbols_is_left_byte_equal(sch_tools, kicad_demo) -> None:
    """complex_hierarchy holds 33 library-flagged power symbols (``#PWR0110``, ``#U0201``) that are
    not real duplicates or gaps: nothing may change in any sheet."""
    project = kicad_demo("complex_hierarchy")
    before = _sheet_bytes(project)

    result = sch_tools["fix_annotation"](str(project / "complex_hierarchy.kicad_sch"))

    assert result["status"] == "ok", result
    assert result["changed"] is False
    assert result["references_before"] == result["references_after"]
    assert _sheet_bytes(project) == before


def test_a_plan_naming_a_symbol_outside_the_stock_library_is_refused_before_any_write(
    sch_tools, kicad_demo, monkeypatch
) -> None:
    """The guard behind the pin: should a kicad_tools release plan a rename for a symbol the pin does
    not cover, the tool writes nothing and names it."""
    from kicad_tools.cli import sch_fix_annotation

    project = kicad_demo("complex_hierarchy")
    root = project / "complex_hierarchy.kicad_sch"
    foreign = next(
        str(c.find("uuid").children[0].value)
        for c in wl.load(root).children
        if not c.is_atom and c.name == "symbol" and c.find("lib_id") is not None
        and not str(c.find("lib_id").children[0].value).startswith("power:")
    )
    calls: list[bool] = []

    def fake(path, dry_run=False, backup=True, format="text", skip_net_check=False) -> int:
        calls.append(dry_run)
        print(json.dumps({"renames": [{"old": "R1", "new": "#PWR99", "uuid": foreign}], "total": 1, "dry_run": True}))
        return 0

    monkeypatch.setattr(sch_fix_annotation, "run_fix_annotation", fake)
    before = _sheet_bytes(project)

    result = sch_tools["fix_annotation"](str(root))

    assert result["status"] != "ok"
    assert result["error"] == "fix_annotation_out_of_scope"
    assert foreign in json.dumps(result)
    assert calls == [True]  # only the dry run was asked for
    assert _sheet_bytes(project) == before


def test_the_pin_is_restored_after_the_call(sch_tools, kicad_demo) -> None:
    from kicad_tools.cli import sch_fix_annotation

    from boardproof.toolsets import schematic

    sentinel = lambda sym: False  # noqa: E731 - a stand-in that is neither the stock rule nor the pin
    original = sch_fix_annotation._is_power_symbol
    sch_fix_annotation._is_power_symbol = sentinel
    try:
        project = kicad_demo("complex_hierarchy")
        sch_tools["fix_annotation"](str(project / "complex_hierarchy.kicad_sch"))
        assert sch_fix_annotation._is_power_symbol is sentinel
        assert sch_fix_annotation._is_power_symbol is not schematic._stock_power_predicate
    finally:
        sch_fix_annotation._is_power_symbol = original


def test_the_pin_is_restored_when_the_pinned_block_raises() -> None:
    import types

    from boardproof.toolsets import schematic

    sentinel = lambda sym: False  # noqa: E731
    module = types.SimpleNamespace(_is_power_symbol=sentinel)
    with pytest.raises(RuntimeError):
        with schematic._stock_power_rule(module) as pinned:
            assert pinned is True
            assert module._is_power_symbol is schematic._stock_power_predicate
            raise RuntimeError("boom")
    assert module._is_power_symbol is sentinel


def test_a_library_that_lost_the_predicate_is_refused_not_run_unpinned(sch_tools, kicad_demo, monkeypatch) -> None:
    from kicad_tools.cli import sch_fix_annotation

    project = kicad_demo("complex_hierarchy")
    monkeypatch.delattr(sch_fix_annotation, "_is_power_symbol")
    before = _sheet_bytes(project)

    result = sch_tools["fix_annotation"](str(project / "complex_hierarchy.kicad_sch"))

    assert result["status"] != "ok"
    assert result["error"] == "fix_annotation_unpinnable"
    assert _sheet_bytes(project) == before

