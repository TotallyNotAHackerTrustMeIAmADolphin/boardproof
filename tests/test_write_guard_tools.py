"""Every writing tool, called with a fake KiCad lock file beside what it writes (GitHub issue 68).

``tests/test_write_guard.py`` pins the layer, the sentence in the tool text and the code scans.
This file calls EVERY tool in its ``WRITING_TOOLS`` set, so that removing the guard from any one
of them turns exactly that tool's test red:

* with the lock: the tool refuses, in the one shape every tool shares (``status``, ``ok: false``,
  ``code: file_open_in_kicad``, ``lock_file``, ``locks``, ``hint``), names the lock file, writes
  nothing (the whole tree is byte-identical, no temporary file or directory either), and leaves
  the lock alone. ``project_create`` raises ``WriteLayerError`` instead; its tool text says so;
* a confirm tool keeps its revision token: after the lock is gone the same token works;
* without the lock (the control) the same call goes through, so the refusal is the lock's doing.

Fake locks on ``tmp_path`` only. A case that cannot run its control here says why (``no_control``).
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import board_outline, board_rules, input_prep, kicad_cli, part_adoption, pour_step, silk_minimums
from kicad_mcp import schematic_import, write_layer
from kicad_mcp import verify_board as gate
from kicad_mcp.toolsets import library as library_toolset
from kicad_mcp.toolsets import manufacturing as manufacturing_toolset
from kicad_mcp.toolsets import placement as placement_toolset
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import routing as routing_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset
from kicad_mcp.toolsets import sourcing as sourcing_toolset
from tests import test_board_outline as t_outline
from tests import test_board_rules as t_rules
from tests import test_input_prep as t_prep
from tests import test_part_adoption as t_adopt
from tests import test_pour_step as t_pour
from tests import test_silk_minimums as t_silk
from tests.test_write_guard import CODE, OTHER_HOST, WRITING_TOOLS

FIXTURES = Path(__file__).parent / "fixtures"
RUNG1 = Path(__file__).resolve().parents[1] / "evals" / "rung1"
FAILED = ("incomplete", "error", "refused", "blocked", "invalid_params")


@dataclass
class Scenario:
    lock_target: Path                       # the file whose KiCad lock is faked
    call: Callable[[], Any]                 # the tool call (a confirm: its token was made before)
    no_control: str | None = None           # why the lock-free call is not run here
    raises: bool = False                    # the tool raises WriteLayerError instead of returning
    lock_name: str | None = None            # a lock placed by name (a directory target)


def _tools(*modules) -> dict:
    mcp = MCPServer("test")
    for module in modules:
        module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _snapshot(root: Path) -> dict[str, bytes | None]:
    """Every file and directory under ``root``: a leftover temporary file or directory shows."""
    return {str(p.relative_to(root)): (p.read_bytes() if p.is_file() else None) for p in sorted(root.rglob("*"))}


def _fake_lock(target: Path) -> Path:
    lock = target.with_name(f"~{target.name}.lck")
    lock.write_bytes(OTHER_HOST)
    return lock


def _project(tmp_path: Path) -> dict[str, Path]:
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    assert made["status"] == "created", made
    return {"dir": tmp_path / "proj", "pcb": Path(made["pcb_path"]), "sch": Path(made["schematic_path"]),
            "pro": Path(made["project_path"])}


def _led(tmp_path: Path) -> Path:
    """simple_led (two layers, R1, D1, J1) as a project folder."""
    dest = tmp_path / "led"
    dest.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy(FIXTURES / f"simple_led{suffix}", dest / f"simple_led{suffix}")
    return dest / "simple_led.kicad_pcb"


def _schematic_project(tmp_path: Path) -> dict[str, Path]:
    project = _project(tmp_path)
    tools = _tools(schematic_toolset)
    placed = tools["add_symbols"](str(project["sch"]), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])
    assert placed["status"] == "ok", placed
    return project


# --------------------------------------------------------------------------- the scenarios


def _confirm_input_preparation(tmp_path):
    board = t_prep._build_fixture(tmp_path / "proj")
    proposal = input_prep.propose_input_preparation(board, tmp_path / "prepared")
    assert proposal["status"] == "proposed", proposal
    return Scenario(board, lambda: input_prep.confirm_input_preparation(
        board, proposal["revision_token"], parity="required", drc=t_prep._fake_drc()))


def _confirm_pour_step(tmp_path):
    board = t_pour.build_board(tmp_path / "proj")
    drc = t_pour.fake_drc()
    baseline = gate.snapshot_rules(board, drc=drc)
    token = t_pour._propose(board, tmp_path / "poured")
    return Scenario(board, lambda: pour_step.confirm_pour_step(
        board, token, baseline=baseline, router=t_pour.make_fake_router(t_pour.FULL), drc=drc,
        fill=t_pour.fake_fill(tmp_path), connectivity_check=False, **t_pour.PARITY))


def _confirm_schematic_import(tmp_path):
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    shutil.copytree(RUNG1 / "project", tmp_path / "proj")
    pcb = tmp_path / "proj" / "rung1_usbc_attiny1616.kicad_pcb"
    proposal = schematic_import.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    assert proposal["status"] == "proposed", proposal
    return Scenario(pcb, lambda: schematic_import.confirm_schematic_import(pcb, proposal["revision_token"]))


def _confirm_board_rules(tmp_path):
    board = t_rules._build(tmp_path / "proj")
    proposal = board_rules.propose_board_rules(board, output_dir=tmp_path / "rules_rev", min_clearance=0.3)
    assert proposal["status"] == "proposed", proposal
    return Scenario(board, lambda: board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=t_rules._fake_drc()))


def _confirm_board_outline(tmp_path):
    shutil.copytree(t_outline.GOLDEN_DIR, tmp_path / "proj")
    board = tmp_path / "proj" / t_outline.GOLDEN_NAME
    proposal = board_outline.propose_board_outline(board, t_outline.RECT, output_dir=tmp_path / "out")
    assert proposal["status"] == "proposed", proposal
    return Scenario(board, lambda: board_outline.confirm_board_outline(
        board, proposal["revision_token"], drc=t_outline._no_outline_drc))


def _confirm_silkscreen_minimums(tmp_path):
    board = t_silk._build(tmp_path / "proj")
    proposal = silk_minimums.propose_silkscreen_minimums(board, tmp_path / "rev")
    assert proposal["status"] == "proposed", proposal
    return Scenario(board, lambda: silk_minimums.confirm_silkscreen_minimums(board, proposal["revision_token"]))


def _confirm_adopt_parts(tmp_path):
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    from kicad_tools.schema import PCB

    board = PCB.create(width=40.0, height=30.0, title="adopt")
    pcb = tmp_path / "proj" / "adopt.kicad_pcb"
    pcb.parent.mkdir()
    write_layer.save_pcb(board, pcb)
    proposal = t_adopt._propose(pcb, [t_adopt._part_assets(tmp_path, "C100")], tmp_path / "rev")
    assert proposal["status"] == "proposed", proposal
    return Scenario(pcb, lambda: part_adoption.confirm_adopt_parts(pcb, proposal["revision_token"]))


def _confirm_autoplacement(tmp_path):
    pcb = _led(tmp_path)
    tools = _tools(placement_toolset)
    proposal = tools["propose_autoplacement"](str(pcb), ["R1"])
    assert proposal["status"] == "pending_confirmation", proposal
    return Scenario(pcb, lambda: tools["confirm_autoplacement"](proposal["revision_token"]))


def _confirm_placement_optimization(tmp_path):
    pcb = _led(tmp_path)
    token = "issue68-optimization-token"
    placement_toolset._PENDING_PLACEMENTS[token] = (
        str(pcb), {"R1": (148.5, 85.5, 0.0)}, time.time() + 3600)  # what propose_placement_optimization parks
    return Scenario(pcb, lambda: _tools(placement_toolset)["confirm_placement_optimization"](token))


def _confirm_remove_graphic_items(tmp_path):
    pcb = tmp_path / "board.kicad_pcb"
    shutil.copy(FIXTURES / "graphics_removal_board.kicad_pcb", pcb)
    tools = _tools(placement_toolset)
    proposal = tools["propose_remove_graphic_items"](str(pcb), [{"shape": "gr_circle", "layer": "B.Cu"}])
    assert proposal["status"] in ("pending_confirmation", "proposed"), proposal
    return Scenario(pcb, lambda: tools["confirm_remove_graphic_items"](str(pcb), proposal["revision_token"]))


def _confirm_route(tmp_path):
    pcb = _led(tmp_path)
    token = routing_toolset._register_proposal(
        pcb,
        [{"start": [140.0, 80.0], "end": [145.0, 80.0], "width_mm": 0.25, "layer": "F.Cu", "net": None}],
        [],
    )
    return Scenario(pcb, lambda: _tools(routing_toolset)["confirm_route"](str(pcb), token))


def _project_create(tmp_path):
    project = _project(tmp_path)
    return Scenario(project["pro"], lambda: project_toolset._project_create(str(project["dir"]), "demo", overwrite=True),
                    raises=True)


def _generate_footprint(tmp_path):
    out = tmp_path / "lib.pretty" / "R.kicad_mod"
    out.parent.mkdir()
    tools = _tools(sourcing_toolset)
    return Scenario(out, lambda: tools["generate_footprint"]("chip", str(out), sourcing_toolset.FootprintParams(size="0402")))


def _register_library_asset(tmp_path):
    from kicad_mcp.sourcing.footprint_gen import generate_footprint

    project = _project(tmp_path)
    fp_dir = project["dir"] / "My.pretty"
    fp_dir.mkdir()
    assert generate_footprint("chip", fp_dir / "R.kicad_mod", size="0402")["status"] == "ok"
    tools = _tools(library_toolset)
    return Scenario(project["pro"], lambda: tools["register_library_asset"](
        str(project["dir"]), footprint_lib_path=str(fp_dir)))  # KiCad rewrites the lib tables with the project


def _fetch_part_assets(tmp_path):
    out = tmp_path / "assets"
    out.mkdir()
    tools = _tools(sourcing_toolset)
    return Scenario(out / "C25804.kicad_sym", lambda: tools["fetch_part_assets"]("C25804", str(out)),
                    no_control="the lock-free call fetches from the network through easyeda2kicad")


def _panelize_board(tmp_path):
    pcb = _led(tmp_path)
    out = tmp_path / "panel" / "panel.kicad_pcb"
    out.parent.mkdir()
    tools = _tools(manufacturing_toolset)
    return Scenario(out, lambda: tools["panelize_board"](str(pcb), str(out)),
                    no_control="the lock-free call needs KiKit, which tests/test_manufacturing_toolset.py covers")


def _sch(tool: str, args: Callable[[Path], tuple]) -> Callable[[Path], Scenario]:
    def make(tmp_path):
        project = _schematic_project(tmp_path)
        tools = _tools(schematic_toolset)
        return Scenario(project["sch"], lambda: tools[tool](str(project["sch"]), *args(project["sch"])))
    return make


def _pcb_tool(tool: str, *args: Any) -> Callable[[Path], Scenario]:
    def make(tmp_path):
        pcb = _led(tmp_path)
        tools = _tools(placement_toolset)
        return Scenario(pcb, lambda: tools[tool](str(pcb), *args))
    return make


def _add_traces(tmp_path):
    pcb = _led(tmp_path)
    tools = _tools(routing_toolset)
    return Scenario(pcb, lambda: tools["add_traces"](str(pcb), [{"start": [140, 80], "end": [145, 80]}]))


SCENARIOS: dict[str, Callable[[Path], Scenario]] = {
    "confirm_input_preparation": _confirm_input_preparation,
    "confirm_pour_step": _confirm_pour_step,
    "confirm_schematic_import": _confirm_schematic_import,
    "confirm_board_rules": _confirm_board_rules,
    "confirm_board_outline": _confirm_board_outline,
    "confirm_silkscreen_minimums": _confirm_silkscreen_minimums,
    "confirm_adopt_parts": _confirm_adopt_parts,
    "confirm_autoplacement": _confirm_autoplacement,
    "confirm_placement_optimization": _confirm_placement_optimization,
    "confirm_remove_graphic_items": _confirm_remove_graphic_items,
    "confirm_route": _confirm_route,
    "project_create": _project_create,
    "generate_footprint": _generate_footprint,
    "register_library_asset": _register_library_asset,
    "fetch_part_assets": _fetch_part_assets,
    "panelize_board": _panelize_board,
    "add_symbols": _sch("add_symbols", lambda p: ([{"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"}],)),
    "add_wires": _sch("add_wires", lambda p: ([{"p1": [50, 50], "p2": [60, 50]}],)),
    "add_junctions": _sch("add_junctions", lambda p: ([[50.8, 60.96]],)),
    "add_labels": _sch("add_labels", lambda p: ([{"kind": "label", "x": 50, "y": 50, "text": "NETA"}],)),
    "assign_footprints": _sch("assign_footprints", lambda p: ({"R1": "Resistor_SMD:R_0603_1608Metric"},)),
    "fix_annotation": _sch("fix_annotation", lambda p: ()),
    "place_footprints": _pcb_tool("place_footprints", [{"reference": "R1", "x": 3.0, "y": 4.0}]),
    "move_reference_designators_to_layer": _pcb_tool("move_reference_designators_to_layer", ["R1"], "F.Fab"),
    "align_footprints": _pcb_tool("align_footprints", ["R1", "D1"], "x", "min"),
    "distribute_footprints": _pcb_tool("distribute_footprints", ["R1", "D1", "J1"], "x"),
    "snap_footprints_to_grid": _pcb_tool("snap_footprints_to_grid", ["R1", "D1"], 1.0),
    "pack_footprints_densely": _pcb_tool("pack_footprints_densely", ["R1", "D1"]),
    "add_traces": _add_traces,
}


def test_every_writing_tool_has_a_lock_scenario():
    assert set(SCENARIOS) == set(WRITING_TOOLS), (
        f"WRITING_TOOLS and SCENARIOS differ: {sorted(set(WRITING_TOOLS) ^ set(SCENARIOS))}; a new writing tool "
        "needs a scenario here that calls it beside a fake lock file"
    )


def _assert_the_one_refusal_shape(result: dict, lock: Path) -> None:
    assert isinstance(result, dict), result
    assert result.get("code") == CODE, result
    assert result.get("ok") is False and result.get("status") == "incomplete", result
    assert result.get("lock_file") == str(lock), result
    assert isinstance(result.get("locks"), list) and result["locks"], result
    assert isinstance(result.get("hint"), str) and "close" in result["hint"].lower(), result
    assert str(lock) in result.get("reason", ""), "the reason must name the lock file"


@pytest.mark.parametrize("tool", sorted(SCENARIOS))
def test_each_writing_tool_refuses_while_the_lock_exists_and_writes_once_it_is_gone(tool, tmp_path):
    scenario = SCENARIOS[tool](tmp_path)
    lock = _fake_lock(scenario.lock_target)
    before = _snapshot(tmp_path)

    if scenario.raises:
        with pytest.raises(write_layer.WriteLayerError) as caught:
            scenario.call()
        assert caught.value.code == CODE and caught.value.details["lock_file"] == str(lock)
        assert str(lock) in str(caught.value)
    else:
        _assert_the_one_refusal_shape(scenario.call(), lock)
    assert _snapshot(tmp_path) == before, f"{tool} changed something while KiCad held a lock"
    assert lock.read_bytes() == OTHER_HOST, "no tool deletes or rewrites a lock file"

    if scenario.no_control:
        return
    lock.unlink()
    result = scenario.call()  # a confirm's token is still valid: the refusal did not spend it
    assert result.get("code") != CODE and result.get("status") not in FAILED, result
    assert _snapshot(tmp_path) != before, f"{tool} wrote nothing without the lock: the control proves nothing"
