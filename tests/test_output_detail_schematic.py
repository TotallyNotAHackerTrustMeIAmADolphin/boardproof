"""Short default answers of propose_schematic_import, add_symbols and check_project (GitHub issue 230,
slice 2): counts and the first 20 items by default, every item with ``detail=true``; what a caller decides
on (unresolved footprints, mismatches, error violations, off-grid pins, refusals) is never behind ``detail``."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, output_detail, project_check, schematic_import
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

N = 30  # more than the cap of 20


def _tools() -> dict:
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


# --- propose_schematic_import: the list shaping (the plan dict is what _plan returns) ------------------------------


def _plan(n: int, **extra) -> dict:
    return {
        "footprints": [{"reference": f"R{i}", "library_id": "R:R", "file": "x" * 80, "file_sha256": "0" * 64, "staging_at": [i, 0]} for i in range(n)],
        "already_present": [f"C{i}" for i in range(n)],
        "unresolved": [], "footprint_mismatches": [], "orphans": [], "renetted": [], "stale_nets": {},
        "staging": {"origin": [0, 0]}, "nets": {f"N{i}": [f"R{i}.1"] for i in range(n)}, "unreferenced_sheets": [],
        **extra,
    }


@pytest.mark.parametrize("n,cut", [(0, False), (20, False), (21, True)])
def test_import_lists_are_cut_after_20(n: int, cut: bool) -> None:
    out = schematic_import._proposal_lists(_plan(n), False)
    assert len(out["footprints"]) == min(n, 20) and out["footprints_count"] == n
    assert out.get("footprints_truncated", False) is cut and out.get("nets_truncated", False) is cut
    assert len(out["nets"]) == min(n, 20) and out["net_count"] == n
    assert out["already_present_count"] == n and "already_present" not in out


def test_import_detail_true_gives_every_item() -> None:
    plan = _plan(N)
    out = schematic_import._proposal_lists(plan, True)
    assert out["footprints"] == plan["footprints"] and out["nets"] == plan["nets"]
    assert out["already_present"] == plan["already_present"] and "footprints_truncated" not in out
    assert "detail_note" not in out


def test_import_decision_lists_are_never_cut() -> None:
    many = [{"reference": f"U{i}", "library_id": "L:X", "reason": "r"} for i in range(N)]
    plan = _plan(N, unresolved=many, footprint_mismatches=many, orphans=[f"X{i}" for i in range(N)], renetted=many, stale_nets={f"S{i}": 1 for i in range(N)})
    out = schematic_import._proposal_lists(plan, False)
    for key in ("unresolved", "footprint_mismatches", "orphans", "renetted", "stale_nets_removed"):
        assert len(out[key]) == N and out[f"{key}_count"] == N and f"{key}_truncated" not in out


def test_import_default_answer_is_much_shorter_than_detail() -> None:
    short = len(json.dumps(schematic_import._proposal_lists(_plan(226), False)))
    full = len(json.dumps(schematic_import._proposal_lists(_plan(226), True)))
    assert short < 8000 < full  # 226 footprints: the old answer was 100080 characters


def test_import_tool_registers_detail_and_description_names_it() -> None:
    from boardproof.server import build_server

    tool = next(t for t in build_server()._tool_manager.list_tools() if t.name == "propose_schematic_import")  # type: ignore[attr-defined]
    assert "detail" in tool.parameters["properties"] and tool.parameters["properties"]["detail"]["default"] is False
    assert "detail=true" in tool.description and "always full" in tool.description


# --- check_project -------------------------------------------------------------------------------------------------


def _sheet(n_pins: int, n_err: int, n_warn: int) -> dict:
    return {
        "sheet": "a.kicad_sch",
        "connectivity": {
            "unconnected_pins": [{"ref": f"R{i}"} for i in range(n_pins)], "no_connect_pins": [{"ref": "X"}] * n_pins,
            "connection_issues": [], "missing_from_netlist": [], "unconnected_count": n_pins, "no_connect_count": n_pins,
        },
        "erc": {"violations": [{"severity": "error", "type": f"e{i}"} for i in range(n_err)]
                + [{"severity": "warning", "type": f"w{i}"} for i in range(n_warn)], "count": n_err + n_warn},
    }


def test_cut_sheets_boundaries_and_counts() -> None:
    for n, cut in ((0, False), (20, False), (21, True)):
        sheets = [_sheet(n, 0, n)]
        project_check._cut_sheets(sheets)
        conn, erc = sheets[0]["connectivity"], sheets[0]["erc"]
        assert len(conn["unconnected_pins"]) == min(n, 20) and conn.get("unconnected_pins_truncated", False) is cut
        assert conn["unconnected_count"] == n and conn["no_connect_count"] == n and "no_connect_pins" not in conn
        assert len(erc["violations"]) == min(n, 20) and erc.get("violations_truncated", False) is cut and erc["count"] == n


def test_cut_sheets_never_cuts_error_violations() -> None:
    sheets = [_sheet(0, 35, 40)]
    project_check._cut_sheets(sheets)
    kept = sheets[0]["erc"]["violations"]
    assert sum(v["severity"] == "error" for v in kept) == 35 and len(kept) == 35 + 20
    assert sheets[0]["erc"]["violations_truncated"] is True and sheets[0]["erc"]["count"] == 75


def _project(tmp_path: Path) -> Path:
    return Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])


def _fake(pins: int):
    return lambda _p: {"status": "ok", "unconnected_pins": [{"reference": f"R{i}", "pin": "1", "sheet": ""} for i in range(pins)],
                       "no_connect_pins": [{"reference": "N", "pin": "1", "sheet": ""}], "connection_issues": [], "missing_from_netlist": []}


def test_check_project_default_cuts_and_detail_restores(tmp_path: Path) -> None:
    sch = _project(tmp_path)
    short = project_check.check_project(str(sch), None, ["connectivity"], _fake(N))
    full = project_check.check_project(str(sch), None, ["connectivity"], _fake(N), True)
    assert short["status"] == full["status"] == "ok"
    s, f = short["sheets"][0]["connectivity"], full["sheets"][0]["connectivity"]
    assert len(s["unconnected_pins"]) == 20 and s["unconnected_pins_truncated"] is True and s["unconnected_count"] == N
    assert len(f["unconnected_pins"]) == N and "no_connect_pins" in f and "no_connect_pins" not in s
    assert short["totals"] == full["totals"] and short["totals"]["unconnected_pins"] == N
    assert short["no_findings"] is full["no_findings"] is False and "detail_note" in short and "detail_note" not in full


def test_incomplete_check_project_is_never_cut(tmp_path: Path, monkeypatch) -> None:
    sch = _project(tmp_path)
    monkeypatch.setattr(project_check, "_copy_project_libraries", lambda *_a: ["a library could not be read"])
    res = project_check.check_project(str(sch), None, ["connectivity"], _fake(N), False)
    assert res["status"] == "incomplete" and res["covered"] is False
    assert len(res["sheets"][0]["connectivity"]["unconnected_pins"]) == N and "detail_note" not in res


# --- add_symbols -----------------------------------------------------------------------------------------------------


def _specs(n: int, dx: float = 0.0) -> list[dict]:
    return [{"lib_id": "Device:R", "x": 20.32 + (i % 10) * 12.7 + dx, "y": 20.32 + (i // 10) * 12.7, "ref": f"R{i + 1}", "value": "1k"} for i in range(n)]


def test_add_symbols_default_is_short_and_detail_has_pins(tmp_path: Path, need_kicad_cli) -> None:
    sch = _project(tmp_path)
    t = _tools()
    short = t["add_symbols"](str(sch), _specs(N), True)  # dry run: same shaping, nothing written
    full = t["add_symbols"](str(sch), _specs(N), True, True)
    assert short["status"] == full["status"] == "ok" and short["dry_run"] is True
    assert len(short["would_place"]) == 20 and short["would_place_count"] == N and short["would_place_truncated"] is True
    assert len(short["symbols"]) == 20 and short["symbols_count"] == N and short["symbols_truncated"] is True
    assert all("pins" not in e and e["pin_count"] == 2 for e in short["symbols"])
    assert len(full["would_place"]) == N and len(full["symbols"]) == N and all(e["pins"] for e in full["symbols"])
    assert "symbols_truncated" not in full and "off_grid_symbols" not in full
    assert len(json.dumps(short)) < len(json.dumps(full)) * 0.7  # measured 8950 vs 14854 characters; the nets block (own cap) stays
    for key in ("nets", "snapped", "stale_instance_symbols", "instance_data", "grid", "count", "written"):
        assert key in short and short[key] == full[key], key


def test_add_symbols_off_grid_pins_stay_visible_beyond_the_cut(tmp_path: Path, need_kicad_cli) -> None:
    sch = _project(tmp_path)
    specs = _specs(N)
    specs[-1] = {**specs[-1], "x": specs[-1]["x"] + 0.3}  # the 30th symbol: past the 20 listed
    out = _tools()["add_symbols"](str(sch), specs, True)
    assert out["symbols_truncated"] is True
    assert [e["reference"] for e in out["off_grid_symbols"]] == [f"R{N}"] and out["off_grid_symbols"][0]["off_grid_pins"]


def test_add_symbols_real_write_matches_dry_run_shape(tmp_path: Path, need_kicad_cli) -> None:
    sch = _project(tmp_path)
    out = _tools()["add_symbols"](str(sch), _specs(21))
    assert out["status"] == "ok" and out["written"] is True and len(out["placed"]) == 20 and out["placed_count"] == 21 and out["count"] == 21
    assert out["placed_truncated"] is True and len(out["symbols"]) == 20


def test_registered_descriptions_name_detail() -> None:
    doc = _tools()["add_symbols"].__doc__ or ""
    assert "detail=true" in doc and "off_grid_symbols" in doc
    cdoc = _tools()["check_project"].__doc__ or ""
    assert "detail=true" in cdoc and "never cuts an error-severity violation" in cdoc
    assert output_detail.LIMIT == 20


def test_add_symbols_cut_boundary_and_note(tmp_path: Path, need_kicad_cli) -> None:
    sch = _project(tmp_path)
    t = _tools()
    at20 = t["add_symbols"](str(sch), _specs(20), True)
    at21 = t["add_symbols"](str(sch), _specs(21), True)
    assert len(at20["symbols"]) == 20 and "symbols_truncated" not in at20 and "would_place_truncated" not in at20
    assert len(at21["symbols"]) == 20 and at21["symbols_truncated"] is True and at21["would_place_truncated"] is True
    assert "no pin points" in at20["grid_note"] and "connect_pins" in at20["grid_note"]
    assert "no pin points" not in t["add_symbols"](str(sch), _specs(2), True, True)["grid_note"]


def test_add_symbols_description_says_pins_need_detail() -> None:
    doc = _tools()["add_symbols"].__doc__ or ""
    assert "ONLY with detail=true" in doc and "no pin points" in doc


@pytest.mark.parametrize("n,cut", [(20, False), (21, True)])
def test_check_project_non_error_erc_boundary(tmp_path: Path, monkeypatch, n: int, cut: bool) -> None:
    sch = _project(tmp_path)
    viol = [{"severity": "error", "type": "e"}] * 2 + [{"severity": "warning", "type": f"w{i}"} for i in range(n)]
    monkeypatch.setattr(kicad_cli, "run_erc", lambda _p: {"sheets": [{"path": "/", "uuid_path": p, "violations": viol} for _f, p in project_check.sheet_tree.walk(sch).uses]})
    res = project_check.check_project(str(sch), None, ["erc"], _fake(0))
    sheet = res["sheets"][0]["erc"]
    assert res["status"] == "ok", res
    assert sheet["count"] == n + 2 and sheet.get("violations_truncated", False) is cut
    assert len(sheet["violations"]) == (n + 2 if not cut else 22) and sum(v["severity"] == "error" for v in sheet["violations"]) == 2
