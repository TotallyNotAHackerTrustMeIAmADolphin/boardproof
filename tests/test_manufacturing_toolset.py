"""Tests for the manufacturing export toolset (archived backlog Phase 5,
P5-01/P5-02/P5-03).

Uses the real simple_led fixtures and real kicad-cli (via
ensure_kicad_cli_on_path, same as the rest of this project's DRC/ERC
tests) -- no mocking of kicad_tools' export pipeline, since that's the
whole point of this toolset.

The panelization tests likewise run KiKit for real, and skip cleanly
(with an actionable reason) when it isn't installed, so CI stays green
without ever faking a panelization result. The KiKit-absent behaviour of
`panelize_board` itself is covered unconditionally, so the
evidence-gating is still tested on a machine without KiKit.
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp.toolsets import manufacturing as manufacturing_toolset

FIXTURES = Path(__file__).parent / "fixtures"
PCB_FIXTURE = FIXTURES / "simple_led.kicad_pcb"
SCH_FIXTURE = FIXTURES / "simple_led.kicad_sch"
GOLDEN_PCB_FIXTURE = (
    FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"
)


def _kikit_path():
    try:
        return manufacturing_toolset.find_kikit()
    except manufacturing_toolset.KikitError:
        return None


requires_kikit = pytest.mark.skipif(
    _kikit_path() is None,
    reason=(
        "KiKit not installed. It cannot live in this venv (it imports pcbnew); install it "
        "into KiCad's bundled Python and set KIKIT_PATH -- see manufacturing.py's docstring."
    ),
)


def _copy_board(fixture: Path, tmp_path: Path) -> Path:
    """Copy a fixture board into tmp_path.

    KiKit writes .kicad_pro/.kicad_prl/.kicad_dru sidecars and a .lck lock
    file next to its OUTPUT, and reads the project next to its INPUT, so
    tests must never point it at tests/fixtures/ -- especially not at
    golden_attiny1616_board/, which is a verified acceptance artifact.
    """
    dest = tmp_path / "src" / fixture.name
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(fixture, dest)
    for suffix in (".kicad_pro", ".kicad_prl", ".kicad_sch"):
        sidecar = fixture.with_suffix(suffix)
        if sidecar.is_file():
            shutil.copy2(sidecar, dest.with_suffix(suffix))
    return dest


def _build_and_get_fns(mcp: MCPServer) -> dict:
    manufacturing_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_build_registers_expected_tools():
    mcp = MCPServer("test")
    names = manufacturing_toolset.build(mcp)
    assert names == ["export_manufacturing_package", "panelize_board"]


def test_export_manufacturing_package_produces_real_files(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["export_manufacturing_package"](
        str(PCB_FIXTURE), str(tmp_path), schematic_path=str(SCH_FIXTURE)
    )
    assert result["status"] == "ok", result
    assert result["bom"] and Path(result["bom"]).is_file()
    assert result["placement"] and Path(result["placement"]).is_file()
    assert result["gerbers"] and Path(result["gerbers"]).exists()


def test_bom_and_cpl_have_real_jlcpcb_column_shapes(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["export_manufacturing_package"](
        str(PCB_FIXTURE), str(tmp_path), schematic_path=str(SCH_FIXTURE)
    )
    with open(result["bom"], newline="", encoding="utf-8") as fh:
        bom_header = next(csv.reader(fh))
    assert bom_header == ["Comment", "Designator", "Footprint", "LCSC Part #"]

    with open(result["placement"], newline="", encoding="utf-8") as fh:
        cpl_header = next(csv.reader(fh))
    assert cpl_header == ["Designator", "Val", "Package", "Mid X", "Mid Y", "Rotation", "Layer"]


def test_lcsc_overrides_fill_the_bom_column(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["export_manufacturing_package"](
        str(PCB_FIXTURE),
        str(tmp_path),
        schematic_path=str(SCH_FIXTURE),
        lcsc_overrides={"R1": "C17513", "D1": "C2286929"},
    )
    assert result["lcsc_part_numbers_filled"] == 2

    with open(result["bom"], newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    by_ref = {row["Designator"]: row["LCSC Part #"] for row in rows}
    assert by_ref["R1"] == "C17513"
    assert by_ref["D1"] == "C2286929"


def test_missing_pcb_file_is_incomplete_not_an_exception(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["export_manufacturing_package"](
        str(tmp_path / "does_not_exist.kicad_pcb"), str(tmp_path)
    )
    assert result["status"] == "incomplete"
    assert "not found" in result["reason"]


def test_export_without_schematic_still_produces_gerbers(tmp_path):
    # schematic_path is optional -- BOM/CPL actually still get produced from
    # the PCB's own footprint data even without one (verified directly), but
    # the real point of this test is that gerber generation doesn't depend
    # on a schematic being supplied at all.
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["export_manufacturing_package"](str(PCB_FIXTURE), str(tmp_path))
    assert result["gerbers"] and Path(result["gerbers"]).exists()


# ---------------------------------------------------------------------------
# Panelization (P5-02)
# ---------------------------------------------------------------------------


def _panel_ok(result: dict) -> None:
    """Assert a panelize_board result really is a verified panel."""
    assert result["status"] in ("ok", "partial"), result
    assert result.get("warnings", []) == [] or all(
        "kicad-cli resolution" in w for w in result["warnings"]
    ), result
    assert Path(result["panel_path"]).is_file()
    assert result["verification"]["footprint_multiple_ok"] is True, result["verification"]


def test_panelize_rejects_unknown_separation_without_running_kikit(tmp_path):
    # Runs with or without KiKit installed: validation must happen before any
    # subprocess, and must not raise.
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["panelize_board"](
        str(PCB_FIXTURE), str(tmp_path / "panel.kicad_pcb"), separation="scoreline"
    )
    assert result["status"] == "incomplete"
    assert "mousebites" in result["reason"]
    assert not (tmp_path / "panel.kicad_pcb").exists()


def test_panelize_rejects_unknown_framing_tooling_fiducials(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    for kwargs, needle in (
        ({"framing": "rails"}, "framing"),
        ({"tooling": "2hole"}, "tooling"),
        ({"fiducials": "2fid"}, "fiducials"),
    ):
        result = tools["panelize_board"](
            str(PCB_FIXTURE), str(tmp_path / "panel.kicad_pcb"), **kwargs
        )
        assert result["status"] == "incomplete"
        assert needle in result["reason"]


def test_panelize_missing_pcb_is_incomplete_not_an_exception(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["panelize_board"](
        str(tmp_path / "nope.kicad_pcb"), str(tmp_path / "panel.kicad_pcb")
    )
    assert result["status"] == "incomplete"
    assert "not found" in result["reason"]


def test_panelize_without_kikit_says_so_and_writes_nothing(tmp_path, monkeypatch):
    """The core anti-lie guarantee (archived backlog P1-14): with KiKit missing the
    tool must refuse explicitly, not emit an un-panelized board."""
    monkeypatch.setattr(
        manufacturing_toolset,
        "find_kikit",
        lambda: (_ for _ in ()).throw(
            manufacturing_toolset.KikitError(manufacturing_toolset._KIKIT_INSTALL_HINT)
        ),
    )
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    out = tmp_path / "panel.kicad_pcb"
    result = tools["panelize_board"](str(PCB_FIXTURE), str(out))
    assert result["status"] == "incomplete"
    assert "pip install kikit" in result["reason"]
    assert "KIKIT_PATH" in result["reason"]
    assert not out.exists()


def test_find_kikit_honours_env_override(tmp_path, monkeypatch):
    fake = tmp_path / "kikit.exe"
    fake.write_text("", encoding="utf-8")
    monkeypatch.setenv("KIKIT_PATH", str(fake))
    assert manufacturing_toolset.find_kikit() == fake

    monkeypatch.setenv("KIKIT_PATH", str(tmp_path / "missing.exe"))
    with pytest.raises(manufacturing_toolset.KikitError):
        manufacturing_toolset.find_kikit()


@requires_kikit
def test_panelize_2x2_mousebites_really_multiplies_the_board(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    src = _copy_board(PCB_FIXTURE, tmp_path)
    out = tmp_path / "panel_2x2.kicad_pcb"

    result = tools["panelize_board"](
        str(src), str(out), rows=2, cols=2, separation="mousebites", run_drc=False
    )
    _panel_ok(result)

    v = result["verification"]
    assert result["boards"] == 4
    assert v["source_footprints"] == 3  # simple_led: R1, D1, J1
    assert v["panel_board_footprints"] == 12 == v["source_footprints"] * 4
    # KiKit really cut the boards apart: mousebite marker footprints exist.
    assert v["kikit_auxiliary_footprints"] > 0

    # Verified independently of the tool's own bookkeeping.
    from kicad_tools.schema import PCB

    refs = [fp.reference for fp in PCB.load(out).footprints]
    assert sum(1 for r in refs if r == "R1") == 4
    assert sum(1 for r in refs if r.startswith("KiKit_MB")) > 0


@requires_kikit
def test_panelize_vcuts_with_frame_tooling_and_fiducials(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    src = _copy_board(PCB_FIXTURE, tmp_path)
    out = tmp_path / "panel_vcut.kicad_pcb"

    result = tools["panelize_board"](
        str(src),
        str(out),
        rows=1,
        cols=3,
        separation="vcuts",
        framing="frame",
        tooling="3hole",
        fiducials="3fid",
        run_drc=False,
    )
    _panel_ok(result)
    assert result["verification"]["panel_board_footprints"] == 9

    from kicad_tools.schema import PCB

    refs = [fp.reference for fp in PCB.load(out).footprints]
    assert any(r.startswith("KiKit_TO") for r in refs), refs  # tooling holes
    assert any(r.startswith("KiKit_FID") for r in refs), refs  # fiducials
    # V-cuts leave no mousebite drills behind.
    assert not any(r.startswith("KiKit_MB") for r in refs), refs


@requires_kikit
def test_panelize_golden_board_passes_real_drc(tmp_path):
    """The acceptance-shaped case: panelize the golden-path board and prove
    with real kicad-cli DRC that the panel introduced no new violations."""
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    src = _copy_board(GOLDEN_PCB_FIXTURE, tmp_path)
    out = tmp_path / "golden_panel.kicad_pcb"

    result = tools["panelize_board"](
        str(src), str(out), rows=2, cols=3, separation="vcuts", framing="frame", run_drc=True
    )
    _panel_ok(result)

    v = result["verification"]
    assert result["boards"] == 6
    assert v["panel_board_footprints"] == v["source_footprints"] * 6

    drc = v["drc"]
    assert drc["status"] == "complete", drc
    # Real numbers, not "the subprocess exited 0": the panel must not add
    # violations beyond what six copies of the source board already carry.
    assert drc["panel_violations"] <= drc["source_violations"] * 6, drc[
        "panel_violation_details"
    ]
    assert drc["panel_unconnected"] == drc["source_unconnected"] * 6, drc


@requires_kikit
def test_panelize_reports_the_kikit_it_actually_used(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    src = _copy_board(PCB_FIXTURE, tmp_path)
    result = tools["panelize_board"](
        str(src), str(tmp_path / "p.kicad_pcb"), rows=1, cols=2, run_drc=False
    )
    _panel_ok(result)
    assert Path(result["kikit_path"]).is_file()
    assert result["kikit_version"] and "kikit" in result["kikit_version"].lower()
