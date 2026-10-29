"""KiCad's own Specctra export and SES import through KiCad's bundled Python (build ticket 46).

The oracle for pin placement is KiCad itself: every pad's position as ``pcbnew`` computes it
(the runner's ``pads`` command), compared with where Freerouting 2.5.0 places the DSN's pins
(``tests/_dsn_pins.py``, ``paradigm-review-2`` ticket 16's method). Rung 1 is the pinned
``evals/rung1/layout_run/07_poured``; rung 2 is staged from the installed demo exactly as the
rung-2 tests stage it.

Reproduction (commit 7342e8d, before the old path was removed): on rung 2 our
``kicad_tools`` DSN put 36 of 247 pins in the wrong place (up to 40 mm; every pin of the
+-90 degree footprints that is not on the rotation axis, and both pins of the back-side JP1),
KiCad's DSN none of its 240 (the 7 others are pads without copper, which KiCad exports as
keepouts). On rung 1, where every footprint sits at 0 degrees on the front, both placed all.

Session files in these tests are Specctra text (not a KiCad file): they stand in for what
Freerouting writes, in KiCad's convention (y negated, ``library_out`` present).
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp.toolsets import routing as routing_toolset
from tests._dsn_pins import kids, misplaced_pins, one, parse
from tests._self_check_helpers import corrupt_writes, sexp_edit, top_nodes

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).parent / "fixtures"
RUNG1 = ROOT / "evals" / "rung1" / "layout_run" / "07_poured"
KICAD_SPECCTRA = routing_toolset.kicad_specctra_availability()
needs_kicad_python = pytest.mark.skipif(
    not KICAD_SPECCTRA["available"], reason=f"KiCad's bundled Python is not usable here: {KICAD_SPECCTRA['reason']}"
)


def _stage_rung2(kicad_demo, tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location("stage_input", ROOT / "evals" / "rung2" / "stage_input.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    staged = module.stage(kicad_demo("pic_programmer"), tmp_path / "rung2")
    return Path(staged["board"])


def _copy_rung1(tmp_path: Path) -> Path:
    shutil.copytree(RUNG1, tmp_path / "rung1")
    return tmp_path / "rung1" / "rung1_usbc_attiny1616.kicad_pcb"


@pytest.fixture
def stress_board(tmp_path):
    project = tmp_path / "proj"
    project.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        shutil.copy(FIXTURES / f"krt_stress_congestion{suffix}", project)
    return project / "krt_stress_congestion.kicad_pcb"


def _kicad_pads(board: Path) -> list[dict]:
    result = routing_toolset.run_kicad_specctra("pads", board)
    assert result["ok"], result
    return result["pads"]


def _classes(dsn: Path) -> dict[str, dict]:
    """``{class: {"nets": n, "width": um, "clearance": um}}`` from the DSN's network section."""
    root = parse(dsn.read_text(encoding="utf-8"))
    out = {}
    for cls in kids(one(root, "network"), "class"):
        rule = one(cls, "rule")
        out[cls[1]] = {
            "nets": sum(1 for v in cls[2:] if not isinstance(v, list)),
            "width": float(one(rule, "width")[1]),
            "clearance": float(kids(rule, "clearance")[0][1]),
        }
    return out


# --- the DSN -----------------------------------------------------------------------------------


@needs_kicad_python
def test_kicads_dsn_places_every_rung2_pin_and_keeps_both_net_classes(kicad_demo, tmp_path):
    board = _stage_rung2(kicad_demo, tmp_path)
    pads = _kicad_pads(board)
    assert len(pads) == 247
    before = board.read_bytes()

    export = routing_toolset.export_dsn_with_kicad(board, tmp_path / "work")

    dsn = Path(export["dsn_path"])
    placed = misplaced_pins(dsn, pads, y_sign=-1.0)
    assert (placed["pins"], placed["misplaced"], placed["unmatched"]) == (240, [], 0), placed
    classes = _classes(dsn)
    assert classes["POWER"] == {"nets": 2, "width": 800.0, "clearance": 280.0}, classes
    assert (classes["kicad_default"]["width"], classes["kicad_default"]["clearance"]) == (500.0, 250.0)
    assert board.read_bytes() == before  # the exporter worked on a scratch copy


@needs_kicad_python
def test_kicads_dsn_places_every_rung1_pin(tmp_path):
    board = _copy_rung1(tmp_path)
    export = routing_toolset.export_dsn_with_kicad(board, tmp_path / "work")
    placed = misplaced_pins(Path(export["dsn_path"]), _kicad_pads(board), y_sign=-1.0)
    assert (placed["pins"], placed["misplaced"], placed["unmatched"]) == (55, [], 0), placed
    assert _classes(Path(export["dsn_path"]))["Power"]["width"] == 400.0


# --- the SES import ------------------------------------------------------------------------------

_VIA = '"Via[0-1]_800:400_um"'
_LIBRARY = (f"(library_out (padstack {_VIA} (shape (circle F.Cu 8000 0 0)) (shape (circle B.Cu 8000 0 0)) "
            "(attach off)))")


def _session(*nets: str) -> str:
    return ("(session test (base_design test) (placement (resolution um 10)) (was_is)\n"
            "  (routes (resolution um 10) (parser (host_cad \"test\") (host_version \"1\"))\n"
            f"    {_LIBRARY}\n    (network_out\n      " + "\n      ".join(nets) + ")))\n")


_GOOD = ("(net SIG1 (wire (path F.Cu 2500 100000 -100000 120000 -100000 120000 -120000)) "
         f"(via {_VIA} 120000 -120000))")
_SIG1_SEGMENT = {"start": [30.0, 30.0], "end": [32.0, 30.0], "width_mm": 0.25, "layer": "F.Cu", "net": "SIG1"}


def _import(board: Path, ses: Path, work: Path) -> dict:
    """Snapshot the board, then import into the snapshot (what ``export_dsn_with_kicad``
    and ``import_ses_with_kicad`` do in a run, minus the DSN these tests do not need)."""
    snapshot = routing_toolset.snapshot_board(board, work.parent / f"{work.name}_snapshot")
    return routing_toolset.import_ses_with_kicad(snapshot, ses, work)


_ZERO = {"tracks": 0, "arcs": 0, "vias": 0}
CLEAN_REPORT = {"returned": True, "existing_copper": dict(_ZERO), "locked_for_import": 0,
                "session": {"expected": dict(_ZERO), "ignored_wire_shapes": 0, "nets": 0, "placement_entries": 0},
                "imported": dict(_ZERO), "missing": dict(_ZERO), "unexpected": dict(_ZERO),
                "new_items_on_no_net": 0, "importer_warnings": []}


def _fake_import_runner(monkeypatch, report: dict, edit_routed=None) -> list:
    """KiCad's runner replaced at its process boundary: ``import`` returns ``report`` (the
    runner's result line) and may first change the routed copy through the write layer."""
    calls = []

    def fake(command, *args, timeout=None):
        calls.append((command, *args))
        assert command == "import", command
        if edit_routed is not None:
            edit_routed(Path(args[0]))
        return {"ok": True, **report}

    monkeypatch.setattr(routing_toolset, "run_kicad_specctra", fake)
    return calls


def test_an_edit_saved_during_routing_refuses_the_import_by_name(stress_board, tmp_path, monkeypatch):
    """Review round 1, finding 3: the import used to diff a fresh copy of the board as it was
    AFTER Freerouting finished, so an edit saved meanwhile became the silent base. Now the
    session is imported into the export's snapshot, and a board whose content changed since
    the snapshot is refused by name (the general drift guard is build ticket 40)."""
    snapshot = routing_toolset.snapshot_board(stress_board, tmp_path / "snap")
    routing_toolset._apply_specs(stress_board, [_SIG1_SEGMENT], [])  # the user saves an edit meanwhile
    _fake_import_runner(monkeypatch, CLEAN_REPORT)
    ses = tmp_path / "board.ses"
    ses.write_text(_session(), encoding="utf-8")

    with pytest.raises(routing_toolset.BoardChangedDuringRouting) as refused:
        routing_toolset.import_ses_with_kicad(snapshot, ses, tmp_path / "routed")

    assert refused.value.code == "board_changed_during_routing"
    assert str(stress_board) in str(refused.value)


def test_the_import_goes_into_the_snapshot_not_the_live_board(stress_board, tmp_path, monkeypatch):
    snapshot = routing_toolset.snapshot_board(stress_board, tmp_path / "snap")
    calls = _fake_import_runner(monkeypatch, CLEAN_REPORT)
    ses = tmp_path / "board.ses"
    ses.write_text(_session(), encoding="utf-8")
    imported = routing_toolset.import_ses_with_kicad(snapshot, ses, tmp_path / "routed")
    assert calls == [("import", tmp_path / "routed" / stress_board.name, ses)]
    assert imported["source_sha256"] == snapshot["source_sha256"]
    assert (tmp_path / "routed" / stress_board.name).read_bytes() == Path(snapshot["snapshot_pcb"]).read_bytes()


def test_a_parenthesised_net_name_is_one_net_in_the_session_count():
    """KiCad's auto-generated net names carry parentheses (``Net-(D1-K)``); Freerouting
    quotes them. The deleted SES parser had a test for this; the runner's counter keeps it."""
    runner = _runner_module()
    text = _session(
        '(net "Net-(D1-K)" (wire (path F.Cu 2500 100000 -100000 120000 -100000 120000 -120000)) '
        f'(via {_VIA} 120000 -120000))',
        '(net "/(odd) name" (wire (path B.Cu 2500 0 0 1000 0)))',
        '(net "/VBUS)" (wire (path B.Cu 2500 0 1000 1000 1000)))',  # a lone paren, legal in KiCad
    )
    counted = runner.session_counts(text)
    assert counted == {"expected": {"tracks": 4, "arcs": 0, "vias": 1}, "ignored_wire_shapes": 0, "nets": 3,
                       "placement_entries": 0}


@needs_kicad_python
def test_the_import_lands_in_kicads_frame_with_the_session_via_size_and_drill(stress_board, tmp_path):
    ses = tmp_path / "board.ses"
    ses.write_text(_session(_GOOD), encoding="utf-8")
    before = stress_board.read_bytes()

    imported = _import(stress_board, ses, tmp_path / "routed")

    assert stress_board.read_bytes() == before  # only the scratch copy was imported into
    assert imported["import"]["importer_returned"] is True
    assert imported["import"]["skipped_items"] == 0 and imported["import"]["new_items_on_no_net"] == 0
    assert imported["import"]["session_expected"] == {"tracks": 2, "arcs": 0, "vias": 1}
    assert imported["removed_copper"] == 0
    # the routed copy's raw tokens: y negated back (session -10 mm is board +10 mm)
    root = write_layer.load(imported["routed_pcb"])
    segments = top_nodes(root, "segment")
    vias = top_nodes(root, "via")
    assert sorted((tuple(s.find("start").values), tuple(s.find("end").values)) for s in segments) == [
        ((10.0, 10.0), (12.0, 10.0)), ((12.0, 10.0), (12.0, 12.0))]
    assert [(float(v.find("size").values[0]), float(v.find("drill").values[0])) for v in vias] == [(0.8, 0.4)]
    assert [(s["size_mm"], s["drill_mm"]) for s in imported["via_specs"]] == [(0.8, 0.4)]

    check = routing_toolset._apply_specs(stress_board, imported["trace_specs"], imported["via_specs"],
                                         expected_copper=imported["expected_copper"])

    applied = write_layer.load(stress_board)  # the reloaded board, not the return value
    assert routing_toolset.raw_copper(applied) == routing_toolset.raw_copper(root)
    assert [(float(v.find("size").values[0]), float(v.find("drill").values[0]))
            for v in top_nodes(applied, "via")] == [(0.8, 0.4)]
    assert check["missing"]["count"] == 0 and check["unexpected"]["count"] == 0


@needs_kicad_python
def test_the_import_return_value_is_not_trusted_skips_and_lost_nets_are_counted(stress_board, tmp_path):
    ses = tmp_path / "board.ses"
    ses.write_text(_session(
        _GOOD,
        "(net SIG2 (wire (path In7.Cu 2500 50000 -50000 60000 -50000)))",  # unknown layer: skipped
        '(net SIG3 (via "Via[0-1]_999:111_um" 70000 -70000))',  # padstack not in library_out
        "(net NOSUCHNET (wire (path F.Cu 2500 80000 -80000 90000 -80000)))",  # lands on no net
    ), encoding="utf-8")

    imported = _import(stress_board, ses, tmp_path / "routed")

    report = imported["import"]
    assert report["importer_returned"] is True  # KiCad says success ...
    assert report["session_expected"] == {"tracks": 4, "arcs": 0, "vias": 2}
    assert report["imported"] == {"tracks": 3, "arcs": 0, "vias": 1}
    assert report["skipped_items"] == 2 and report["missing"] == {"tracks": 1, "arcs": 0, "vias": 1}
    assert report["new_items_on_no_net"] == 1
    assert any("2 session item" in w for w in report["importer_warnings"]), report["importer_warnings"]
    assert routing_toolset._freerouting_flags(imported) == ["ses_import_incomplete"]


@needs_kicad_python
def test_a_session_kicad_cannot_read_fails_closed_and_leaves_the_board(stress_board, tmp_path):
    before = stress_board.read_bytes()
    for name, text in (("empty", ""), ("truncated", _session(_GOOD)[:-5]),
                       ("no_routes", "(session test (base_design test))")):
        ses = tmp_path / f"{name}.ses"
        ses.write_text(text, encoding="utf-8")
        with pytest.raises(routing_toolset.KicadSpecctraError):
            _import(stress_board, ses, tmp_path / name)
    assert stress_board.read_bytes() == before


@needs_kicad_python
def test_copper_already_on_the_board_is_kept_fixed_and_not_in_the_delta(stress_board, tmp_path):
    first = tmp_path / "first.ses"
    first.write_text(_session(_GOOD), encoding="utf-8")
    done = _import(stress_board, first, tmp_path / "r1")
    routing_toolset._apply_specs(stress_board, done["trace_specs"], done["via_specs"],
                                 expected_copper=done["expected_copper"])

    export = routing_toolset.export_dsn_with_kicad(stress_board, tmp_path / "dsn")
    assert export["existing_copper_kept_fixed"] == {"tracks": 2, "arcs": 0, "vias": 1}
    dsn = parse(Path(export["dsn_path"]).read_text(encoding="utf-8"))
    wiring = one(dsn, "wiring")
    assert {str(one(w, "type")[1]) for w in kids(wiring, "wire") + kids(wiring, "via")} == {"fix"}

    second = tmp_path / "second.ses"  # a router that sends nothing back for the fixed copper
    second.write_text(_session("(net SIG4 (wire (path F.Cu 2500 150000 -150000 160000 -150000)))"),
                      encoding="utf-8")
    again = _import(stress_board, second, tmp_path / "r2")
    assert again["removed_copper"] == 0 and len(again["trace_specs"]) == 1 and again["via_specs"] == []


# --- the verified apply ----------------------------------------------------------------------------


def _bump_new_via_drill(root):
    via = top_nodes(root, "via")[-1]
    via.find("drill").set_value(0, 0.3)


def _move_new_segment(root):
    seg = top_nodes(root, "segment")[-1]
    seg.find("end").set_value(0, float(seg.find("end").values[0]) + 0.5)


@needs_kicad_python
@pytest.mark.parametrize("corrupt", [_bump_new_via_drill, _move_new_segment], ids=["via-drill", "segment-end"])
def test_the_apply_self_check_fires_on_a_corrupted_write_and_leaves_the_board(
        stress_board, tmp_path, monkeypatch, corrupt):
    ses = tmp_path / "board.ses"
    ses.write_text(_session(_GOOD), encoding="utf-8")
    imported = _import(stress_board, ses, tmp_path / "routed")
    before = stress_board.read_bytes()
    # The corruption lands in the write layer's temporary sibling after KiCad's parse check
    # and before the verify hook, i.e. exactly what a writer that alters a value would leave.
    handle = corrupt_writes(monkeypatch, write_layer, "parse_check", sexp_edit(corrupt),
                            path_of=lambda args, kwargs: Path(args[0]))

    with pytest.raises(write_layer.WriteLayerError) as failed:
        routing_toolset._apply_specs(stress_board, imported["trace_specs"], imported["via_specs"],
                                     expected_copper=imported["expected_copper"])

    assert failed.value.code == routing_toolset.WRITE_NOT_VERIFIED
    assert len(handle.hits) == 1
    assert stress_board.read_bytes() == before
    assert sorted(p.name for p in stress_board.parent.iterdir()) == sorted(
        f"krt_stress_congestion{s}" for s in (".kicad_pcb", ".kicad_pro", ".kicad_prl"))
    handle.disarm()
    routing_toolset._apply_specs(stress_board, imported["trace_specs"], imported["via_specs"],
                                 expected_copper=imported["expected_copper"])
    assert routing_toolset.raw_copper(write_layer.load(stress_board)) == routing_toolset.raw_copper(
        write_layer.load(imported["routed_pcb"]))


@needs_kicad_python
def test_the_apply_refuses_copper_that_is_not_the_routed_copys(stress_board, tmp_path):
    """A spec list and an expected set that disagree (here: the spec moved 1 um) never land."""
    ses = tmp_path / "board.ses"
    ses.write_text(_session(_GOOD), encoding="utf-8")
    imported = _import(stress_board, ses, tmp_path / "routed")
    specs = [dict(s) for s in imported["trace_specs"]]
    specs[0]["end"] = [specs[0]["end"][0] + 0.001, specs[0]["end"][1]]
    before = stress_board.read_bytes()
    with pytest.raises(write_layer.WriteLayerError, match="not the routed copy's"):
        routing_toolset._apply_specs(stress_board, specs, imported["via_specs"],
                                     expected_copper=imported["expected_copper"])
    assert stress_board.read_bytes() == before


# --- no bundled Python, no silent fallback ------------------------------------------------------------


def test_a_missing_kicad_python_is_unavailable_never_another_dsn(stress_board, tmp_path, monkeypatch):
    monkeypatch.setenv(routing_toolset.KICAD_PYTHON_ENV, str(tmp_path / "no" / "python.exe"))
    info = routing_toolset.kicad_specctra_availability()
    assert info["available"] is False and routing_toolset.KICAD_PYTHON_ENV in info["reason"]
    with pytest.raises(routing_toolset.KicadPythonUnavailable):
        routing_toolset.export_dsn_with_kicad(stress_board, tmp_path / "w")
    assert not (tmp_path / "w" / "board.dsn").exists()


def test_a_python_without_pcbnew_is_unavailable(tmp_path, monkeypatch):
    import sys

    monkeypatch.setenv(routing_toolset.KICAD_PYTHON_ENV, sys.executable)  # this venv has no pcbnew
    info = routing_toolset.kicad_specctra_availability()
    assert info["available"] is False and "pcbnew" in info["reason"], info


def test_the_runner_is_started_isolated_in_a_throwaway_profile(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["env"], seen["cwd"] = list(argv), kwargs["env"], kwargs["cwd"]
        return subprocess.CompletedProcess(argv, 0, b'noise\nKICAD_MCP_RESULT {"ok": true}\n', b"")

    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    monkeypatch.setenv(routing_toolset.KICAD_PYTHON_ENV, str(python))
    monkeypatch.setattr(routing_toolset.subprocess, "run", fake_run)
    assert routing_toolset.run_kicad_specctra("probe")["ok"] is True
    assert seen["argv"][:4] == [str(python), "-I", "-X", "utf8"]
    assert seen["argv"][4].endswith("pcbnew_specctra.py") and seen["argv"][5] == "probe"
    assert seen["env"]["KICAD_CONFIG_HOME"] == seen["cwd"]
    assert not Path(seen["cwd"]).exists()  # removed after the run


def test_relative_paths_reach_the_runner_resolved(tmp_path, monkeypatch):
    """Review round 1, finding 5: the runner runs with cwd=its throwaway directory, so a
    relative board path (``pads simple_led.kicad_pcb`` from the fixtures directory) reached
    ``LoadBoard`` as a path that does not exist there."""
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = list(argv)
        return subprocess.CompletedProcess(argv, 0, b'KICAD_MCP_RESULT {"ok": true}\n', b"")

    python = tmp_path / "python.exe"
    python.write_bytes(b"")
    monkeypatch.setenv(routing_toolset.KICAD_PYTHON_ENV, str(python))
    monkeypatch.setattr(routing_toolset.subprocess, "run", fake_run)
    monkeypatch.chdir(tmp_path)
    routing_toolset.run_kicad_specctra("export", "board.kicad_pcb", Path("out") / "board.dsn")
    assert seen["argv"][6:] == [str((tmp_path / "board.kicad_pcb").resolve()),
                                str((tmp_path / "out" / "board.dsn").resolve())]


def _runner_module():
    """The runner script loaded as a module in the host's Python (no pcbnew, no wx here)."""
    spec = importlib.util.spec_from_file_location("pcbnew_specctra_under_test", routing_toolset._SPECCTRA_RUNNER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_importing_the_runner_has_no_side_effects(monkeypatch, tmp_path):
    """Review round 1, finding 4: the profile directory, KICAD_CONFIG_HOME and the pcbnew
    import used to run at module level, so every import (and every failed probe) leaked a
    ``kicad_mcp_kicad_profile_*`` directory and set KICAD_CONFIG_HOME in the importer."""
    import os
    import tempfile

    monkeypatch.setenv("TMP", str(tmp_path))
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setattr(tempfile, "tempdir", None)
    monkeypatch.delenv("KICAD_CONFIG_HOME", raising=False)

    runner = _runner_module()

    assert "KICAD_CONFIG_HOME" not in os.environ
    assert list(tmp_path.iterdir()) == []
    assert runner.pcbnew is None


def test_the_runner_script_guards_against_dialogs_before_it_imports_pcbnew():
    """Brief rules 7 and 8, in the production script itself: a throwaway KICAD_CONFIG_HOME and
    wx logging to stderr, both set before ``import pcbnew``."""
    import ast

    source = routing_toolset._SPECCTRA_RUNNER.read_text(encoding="utf-8")
    tree = ast.parse(source)
    setup = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_setup")
    order = []
    for node in setup.body:
        text = ast.unparse(node)
        if text == "_require_throwaway_profile()":
            order.append("profile")
        elif text == "_route_wx_log_to_stderr()":
            order.append("wx")
        elif "pcbnew" in text and isinstance(node, (ast.Assign, ast.Import, ast.ImportFrom)):
            order.append("pcbnew")
    assert order == ["profile", "wx", "pcbnew"], order
    assert "wx.Log.SetActiveTarget(wx.LogStderr())" in source
    # nothing outside _setup imports pcbnew, and _setup runs first under __main__
    others = [n for n in ast.walk(tree) if n is not setup and isinstance(n, (ast.Import, ast.ImportFrom))]
    assert not any(a.name == "pcbnew" for n in others for a in n.names)
    assert source.count("importlib.import_module") == 1
    main_block = next(n for n in tree.body if isinstance(n, ast.If) and "__main__" in ast.unparse(n.test))
    assert "_setup()" in ast.unparse(main_block.body[0]), ast.unparse(main_block.body[0])


def _runner_main(runner, argv, capsys) -> dict:
    import json

    runner.main(argv)
    line = [x for x in capsys.readouterr().out.splitlines() if x.startswith(runner.RESULT_MARKER)][-1]
    return json.loads(line[len(runner.RESULT_MARKER):])


@pytest.mark.parametrize("command", ["import", "export"])
def test_the_runner_writes_only_inside_a_temp_directory(command, tmp_path, capsys):
    """Review round 1, finding 9: ``import`` saves over the board it is given and ``export``
    writes the DSN wherever it is told; neither may touch a path outside a temp directory
    (the host only ever passes scratch copies; the worktree is not one)."""
    runner = _runner_module()
    ses = tmp_path / "board.ses"
    ses.write_text(_session(_GOOD), encoding="utf-8")
    outside = ROOT / "not-a-temp-dir" / "board.kicad_pcb"
    argv = {"import": ["import", str(outside), str(ses)],
            "export": ["export", str(tmp_path / "b.kicad_pcb"), str(outside.with_suffix(".dsn"))]}[command]
    refused = _runner_main(runner, argv, capsys)
    assert refused["ok"] is False and "temporary directory" in refused["error"], refused
    assert not outside.parent.exists()
    inside = {"import": ["import", str(tmp_path / "b.kicad_pcb"), str(ses)],
              "export": ["export", str(tmp_path / "b.kicad_pcb"), str(tmp_path / "b.dsn")]}[command]
    passed = _runner_main(runner, inside, capsys)  # past the guard; fails only for want of pcbnew here
    assert "temporary directory" not in passed["error"], passed


def test_the_runner_refuses_a_profile_that_is_not_its_throwaway_working_directory(monkeypatch, tmp_path):
    runner = _runner_module()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path / "elsewhere"))
    (tmp_path / "elsewhere").mkdir()
    with pytest.raises(RuntimeError, match="throwaway"):
        runner._require_throwaway_profile()
    monkeypatch.delenv("KICAD_CONFIG_HOME")
    with pytest.raises(RuntimeError, match="throwaway"):
        runner._require_throwaway_profile()
    monkeypatch.setenv("KICAD_CONFIG_HOME", str(tmp_path))
    runner._require_throwaway_profile()  # the host's layout: profile == cwd


@needs_kicad_python
def test_confirm_route_self_check_fires_on_a_corrupted_write_and_keeps_the_token(
        stress_board, tmp_path, monkeypatch):
    """confirm_route applies a Freerouting proposal through ``_apply_specs``: a corrupted write
    is refused by name (``write_not_verified``), the board is untouched and the token stays
    valid; with the corruption disarmed the same token applies exactly the routed copy's copper."""
    from mcp.server.mcpserver import MCPServer

    ses = tmp_path / "board.ses"
    ses.write_text(_session(_GOOD), encoding="utf-8")
    imported = _import(stress_board, ses, tmp_path / "routed")
    token = routing_toolset._register_proposal(stress_board, imported["trace_specs"], imported["via_specs"],
                                               expected_copper=imported["expected_copper"])
    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    confirm = {t.name: t.fn for t in mcp._tool_manager.list_tools()}["confirm_route"]  # type: ignore[attr-defined]
    handle = corrupt_writes(monkeypatch, write_layer, "parse_check", sexp_edit(_bump_new_via_drill),
                            path_of=lambda args, kwargs: Path(args[0]))
    before = stress_board.read_bytes()

    failed = confirm(str(stress_board), token)

    assert len(handle.hits) == 1
    assert (failed["status"], failed["ok"], failed["code"]) == ("error", False, "write_not_verified"), failed
    named = " ".join(failed["mismatches"])
    assert "via:" in named and ", 0.8, 0.3, " in named, failed  # the corrupted via, by node and by drill
    assert failed["original_unchanged"] is True and stress_board.read_bytes() == before
    assert token in routing_toolset._PENDING_ROUTES
    handle.disarm()
    done = confirm(str(stress_board), token)
    assert done["status"] == "confirmed", done
    assert routing_toolset.raw_copper(write_layer.load(stress_board)) == routing_toolset.raw_copper(
        write_layer.load(imported["routed_pcb"]))


# --- the propose tools: flags bite, a changed board is refused ----------------------------------

PROPOSE_TOOLS = ["propose_route_with_freerouting", "propose_route_with_retry"]


def _tools() -> dict:
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _fake_freerouting(monkeypatch, session_text: str, during=None) -> None:
    """Freerouting replaced at its process boundary: it writes ``session_text`` as its SES
    (KiCad's export before it and KiCad's import after it stay real)."""
    def fake(dsn, ses, **kwargs):
        assert Path(dsn).is_file()
        if during is not None:
            during()
        Path(ses).write_text(session_text, encoding="utf-8")
        return {"self_reported_score": None, "self_reported_unrouted": None}

    monkeypatch.setattr(routing_toolset, "run_freerouting", fake)


@needs_kicad_python
@pytest.mark.parametrize("tool", PROPOSE_TOOLS)
def test_a_board_edited_while_freerouting_runs_is_refused_by_name(tool, stress_board, monkeypatch):
    _fake_freerouting(monkeypatch, _session(_GOOD),
                      during=lambda: routing_toolset._apply_specs(stress_board, [_SIG1_SEGMENT], []))
    pending = set(routing_toolset._PENDING_ROUTES)

    result = _tools()[tool](str(stress_board))

    assert (result["status"], result["ok"], result["code"]) == ("refused", False, "board_changed_during_routing"), result
    assert str(stress_board) in result["reason"]
    assert set(routing_toolset._PENDING_ROUTES) == pending  # no proposal parked


# --- one test per Freerouting flag condition (review round 1, finding 1) ------------------------


@needs_kicad_python
def test_copper_on_a_net_the_board_does_not_know_alone_flags_the_import(stress_board, tmp_path):
    ses = tmp_path / "board.ses"
    ses.write_text(_session("(net NOSUCHNET (wire (path F.Cu 2500 80000 -80000 90000 -80000)))"), encoding="utf-8")
    imported = _import(stress_board, ses, tmp_path / "routed")
    report = imported["import"]
    assert (report["skipped_items"], report["new_items_on_no_net"], report["ignored_wire_shapes"]) == (0, 1, 0)
    assert not any(report["unexpected"].values()) and imported["removed_copper"] == 0
    assert routing_toolset._freerouting_flags(imported) == ["ses_import_incomplete"]


@needs_kicad_python
def test_a_polygon_wire_alone_flags_the_import(stress_board, tmp_path):
    """KiCad's importer drops a polygon wire without a word; the session count sees it."""
    ses = tmp_path / "board.ses"
    ses.write_text(_session("(net SIG1 (wire (polygon F.Cu 0 100000 -100000 120000 -100000 120000 -120000 "
                            "100000 -100000)))"), encoding="utf-8")
    imported = _import(stress_board, ses, tmp_path / "routed")
    report = imported["import"]
    assert (report["skipped_items"], report["new_items_on_no_net"], report["ignored_wire_shapes"]) == (0, 0, 1)
    assert not any(report["unexpected"].values()) and imported["removed_copper"] == 0
    assert routing_toolset._freerouting_flags(imported) == ["ses_import_incomplete"]


def test_more_copper_than_the_session_holds_alone_flags_the_import(stress_board, tmp_path, monkeypatch):
    """KiCad adding more than the session asked for (``unexpected``): no real session is known
    to do it, so the runner's result line is given at its process boundary."""
    report = {**CLEAN_REPORT, "imported": {**_ZERO, "tracks": 1}, "unexpected": {**_ZERO, "tracks": 1}}
    _fake_import_runner(monkeypatch, report)
    ses = tmp_path / "board.ses"
    ses.write_text(_session(), encoding="utf-8")
    imported = _import(stress_board, ses, tmp_path / "routed")
    assert (imported["import"]["skipped_items"], imported["import"]["new_items_on_no_net"]) == (0, 0)
    assert routing_toolset._freerouting_flags(imported) == ["ses_import_incomplete"]


def test_copper_missing_from_the_routed_copy_alone_flags_existing_copper_removed(stress_board, tmp_path, monkeypatch):
    """The runner locks existing copper so KiCad's importer keeps it; should a routed copy
    still lack some (here: removed through the write layer by the faked import), the proposal
    is flagged."""
    routing_toolset._apply_specs(stress_board, [_SIG1_SEGMENT], [])

    def drop_the_segment(routed: Path) -> None:
        root = write_layer.load(routed)
        root.remove(top_nodes(root, "segment")[0])
        write_layer.save(root, routed)

    _fake_import_runner(monkeypatch, CLEAN_REPORT, edit_routed=drop_the_segment)
    ses = tmp_path / "board.ses"
    ses.write_text(_session(), encoding="utf-8")
    imported = _import(stress_board, ses, tmp_path / "routed")
    assert imported["removed_copper"] == 1
    assert routing_toolset._freerouting_flags(imported) == ["existing_copper_removed"]


def test_a_clean_import_raises_no_flag(stress_board, tmp_path, monkeypatch):
    _fake_import_runner(monkeypatch, CLEAN_REPORT)
    ses = tmp_path / "board.ses"
    ses.write_text(_session(), encoding="utf-8")
    assert routing_toolset._freerouting_flags(_import(stress_board, ses, tmp_path / "routed")) == []


@pytest.mark.parametrize("key", ["imported", "missing", "unexpected", "new_items_on_no_net", "session",
                                 "returned"])
def test_a_runner_result_without_a_count_is_incomplete_never_clean(key, stress_board, tmp_path, monkeypatch):
    """Review round 1, finding 9: a missing key used to read as zero (``or {}``, ``.get``),
    i.e. as a clean import. Missing now fails closed."""
    report = {k: v for k, v in CLEAN_REPORT.items() if k != key}
    _fake_import_runner(monkeypatch, report)
    ses = tmp_path / "board.ses"
    ses.write_text(_session(), encoding="utf-8")
    imported = _import(stress_board, ses, tmp_path / "routed")
    assert routing_toolset._freerouting_flags(imported) == ["ses_import_incomplete"], imported["import"]


@pytest.mark.parametrize("key", ["skipped_items", "new_items_on_no_net", "unexpected", "ignored_wire_shapes",
                                 "arcs_not_applied"])
def test_flags_read_a_missing_summary_key_as_incomplete(key):
    clean = {"skipped_items": 0, "new_items_on_no_net": 0, "unexpected": dict(_ZERO), "ignored_wire_shapes": 0,
             "arcs_not_applied": 0}
    assert routing_toolset._freerouting_flags({"import": clean, "removed_copper": 0}) == []
    del clean[key]
    assert "ses_import_incomplete" in routing_toolset._freerouting_flags({"import": clean, "removed_copper": 0})
    assert "ses_import_incomplete" in routing_toolset._freerouting_flags(
        {"import": {**clean, key: None}, "removed_copper": 0})


def test_flags_read_a_missing_removed_copper_count_as_incomplete():
    clean = {"skipped_items": 0, "new_items_on_no_net": 0, "unexpected": dict(_ZERO), "ignored_wire_shapes": 0,
             "arcs_not_applied": 0}
    assert routing_toolset._freerouting_flags({"import": clean}) == ["ses_import_incomplete"]
    assert routing_toolset._freerouting_flags({"import": clean, "removed_copper": "0"}) == ["ses_import_incomplete"]


_GOOD_WITH_A_POLYGON = ("(net SIG1 (wire (path F.Cu 2500 100000 -100000 120000 -100000 120000 -120000)) "
                        f"(via {_VIA} 120000 -120000) "
                        "(wire (polygon F.Cu 0 140000 -140000 150000 -140000 150000 -150000 140000 -140000)))")


@needs_kicad_python
@pytest.mark.parametrize("tool", PROPOSE_TOOLS)
def test_a_flagged_freerouting_proposal_is_refused_until_acknowledged(tool, stress_board, tmp_path, monkeypatch):
    """Mutant B of review round 1 (``[]`` instead of the flags handed to ``_register_proposal``)
    survived every test: the propose tools must carry the flags into the proposal, and
    confirm_route must refuse it, token kept, until ``acknowledge_router_flags=True``."""
    _fake_freerouting(monkeypatch, _session(_GOOD_WITH_A_POLYGON))
    tools = _tools()
    before = stress_board.read_bytes()

    proposal = tools[tool](str(stress_board))

    assert proposal["status"] == "pending_confirmation", proposal
    assert proposal["router_flags"] == ["ses_import_incomplete"] and proposal["requires_acknowledgement"] is True
    token = proposal["revision_token"]
    refused = tools["confirm_route"](str(stress_board), token)
    assert (refused["status"], refused["router_flags"]) == ("refused", ["ses_import_incomplete"]), refused
    assert stress_board.read_bytes() == before and token in routing_toolset._PENDING_ROUTES

    done = tools["confirm_route"](str(stress_board), token, acknowledge_router_flags=True)

    assert done["status"] == "confirmed", done
    assert done["router_flags_acknowledged"] == ["ses_import_incomplete"]
    segments = top_nodes(write_layer.load(stress_board), "segment")
    assert sorted((tuple(s.find("start").values), tuple(s.find("end").values)) for s in segments) == [
        ((10.0, 10.0), (12.0, 10.0)), ((12.0, 10.0), (12.0, 12.0))]


# --- arcs (review round 1, finding 6) ------------------------------------------------------------

# A quarter arc from (10, 10) to (12, 12) about (10, 12) on the board; y negated in the session.
_ARC = "(net SIG1 (wire (qarc F.Cu 2500 100000 -100000 120000 -120000 100000 -120000)))"


@needs_kicad_python
def test_arcs_are_not_applied_flagged_and_left_out_of_the_expected_copper(stress_board, tmp_path, monkeypatch):
    """``_apply_specs`` writes segments and vias only. The routed copy's arcs are therefore
    left out of ``expected_copper`` (so an acknowledged proposal applies, verified, instead of
    ending in write_not_verified) and the proposal is flagged ``arcs_not_applied``: those
    connections stay unrouted on the board."""
    ses = tmp_path / "board.ses"
    ses.write_text(_session(_GOOD, _ARC), encoding="utf-8")
    imported = _import(stress_board, ses, tmp_path / "routed")
    routed = write_layer.load(imported["routed_pcb"])
    assert len(top_nodes(routed, "arc")) == 1, "KiCad's importer made no arc of the qarc"
    assert imported["import"]["arcs_not_applied"] == 1
    assert [key for key in imported["expected_copper"] if key[0] == "arc"] == []
    assert routing_toolset._freerouting_flags(imported) == ["arcs_not_applied"]

    _fake_freerouting(monkeypatch, _session(_GOOD, _ARC))
    tools = _tools()
    proposal = tools["propose_route_with_freerouting"](str(stress_board))
    assert proposal["router_flags"] == ["arcs_not_applied"], proposal
    refused = tools["confirm_route"](str(stress_board), proposal["revision_token"])
    assert refused["status"] == "refused" and "arcs" in refused["reason"], refused

    done = tools["confirm_route"](str(stress_board), proposal["revision_token"], acknowledge_router_flags=True)

    assert done["status"] == "confirmed", done
    board = write_layer.load(stress_board)
    assert top_nodes(board, "arc") == [] and len(top_nodes(board, "segment")) == 2
    assert len(top_nodes(board, "via")) == 1


# --- the redundant-looking checks, each proven (review round 1, finding 7) ----------------------


def _routed_gains_a_segment(routed: Path) -> None:
    routing_toolset._apply_specs(routed, [_SIG1_SEGMENT], [])


def test_the_import_diffs_the_routed_copy_two_ways(stress_board, tmp_path, monkeypatch):
    """KRT's ``route_delta`` (the specs) and the raw S-expression diff (the expected copper)
    must agree on what the routed copy gained, before anything is proposed."""
    _fake_import_runner(monkeypatch, CLEAN_REPORT, edit_routed=_routed_gains_a_segment)
    ses = tmp_path / "board.ses"
    ses.write_text(_session(), encoding="utf-8")
    agreed = _import(stress_board, ses, tmp_path / "agreed")
    assert len(agreed["trace_specs"]) == 1 and [k[0] for k in agreed["expected_copper"]] == ["segment"]

    monkeypatch.setattr(routing_toolset.krt_backend, "route_delta",
                        lambda original, routed: {"trace_specs": [], "via_specs": []})
    with pytest.raises(routing_toolset.KicadSpecctraError, match="does not diff the same two ways"):
        _import(stress_board, ses, tmp_path / "disagreed")


def test_the_apply_checks_each_via_against_its_spec_without_expected_copper(stress_board, monkeypatch):
    """KRT's proposals carry no expected copper; then the per-via check is what catches a
    writer that drops the via size it was asked for (kicad_tools has silently dropped a
    size before: ``pad.size = ...``)."""
    from kicad_tools.schema import PCB

    real_add_via = PCB.add_via

    def add_via_ignoring_size(self, x, y, **kwargs):
        kwargs.pop("size", None)
        return real_add_via(self, x, y, **kwargs)

    monkeypatch.setattr(PCB, "add_via", add_via_ignoring_size)
    before = stress_board.read_bytes()
    spec = {"x": 20.0, "y": 20.0, "net": "SIG1", "size_mm": 0.8, "drill_mm": 0.4, "layers": ["F.Cu", "B.Cu"]}
    with pytest.raises(write_layer.WriteLayerError, match="via at") as failed:
        routing_toolset._apply_specs(stress_board, [], [spec])
    assert failed.value.code == routing_toolset.WRITE_NOT_VERIFIED
    assert stress_board.read_bytes() == before


def test_the_apply_refuses_a_write_that_loses_copper_already_on_the_board(stress_board, tmp_path, monkeypatch):
    """The expected-copper check no longer looks for removed copper itself (review round 1,
    finding 7): the write layer's reload comparison reports any source node the write lost."""
    routing_toolset._apply_specs(stress_board, [_SIG1_SEGMENT], [])
    new_spec = {**_SIG1_SEGMENT, "start": [40.0, 40.0], "end": [42.0, 40.0]}
    probe = tmp_path / "probe" / stress_board.name
    probe.parent.mkdir()
    shutil.copy(stress_board, probe)
    before_probe = write_layer.load(probe)
    routing_toolset._apply_specs(probe, [new_spec], [])
    expected = sorted(routing_toolset.raw_copper_delta(before_probe, write_layer.load(probe))["added"].elements())
    assert len(expected) == 1

    old_start = [tuple(s.find("start").values) for s in top_nodes(write_layer.load(stress_board), "segment")]
    assert len(old_start) == 1  # the raw (start ...) of the copper already on the board

    def drop_the_old_segment(root):
        root.remove(next(s for s in top_nodes(root, "segment") if tuple(s.find("start").values) == old_start[0]))

    handle = corrupt_writes(monkeypatch, write_layer, "parse_check", sexp_edit(drop_the_old_segment),
                            path_of=lambda args, kwargs: Path(args[0]))
    before = stress_board.read_bytes()
    with pytest.raises(write_layer.WriteLayerError) as failed:
        routing_toolset._apply_specs(stress_board, [new_spec], [], expected_copper=expected)
    assert failed.value.code == routing_toolset.WRITE_NOT_VERIFIED and len(handle.hits) == 1
    assert "segment" in " ".join(failed.value.details["mismatches"])
    assert stress_board.read_bytes() == before


# --- the expected-copper check, at every site it is wired (delta review 1, finding 1) -----------


def _route_delta_shifts_one_trace_end(monkeypatch) -> None:
    """The specs no longer match the routed copy: one trace end moves by 1 um, the counts stay
    equal, so only the raw expected-copper comparison can tell."""
    real = routing_toolset.krt_backend.route_delta

    def shifted(original, routed):
        delta = real(original, routed)
        spec = delta["trace_specs"][0]
        delta["trace_specs"][0] = {**spec, "end": [spec["end"][0] + 0.001, spec["end"][1]]}
        return delta

    monkeypatch.setattr(routing_toolset.krt_backend, "route_delta", shifted)


def _writer_shifts_one_trace_end(monkeypatch) -> None:
    """kicad_tools writes one trace end 1 um off what it was asked (it has silently mis-written
    geometry before: hard rule 2)."""
    from kicad_tools.schema import PCB

    real = PCB.add_trace
    shifted = []

    def add_trace(self, start, end, **kwargs):
        if not shifted:
            shifted.append(end)
            end = (end[0] + 0.001, end[1])
        return real(self, start, end, **kwargs)

    monkeypatch.setattr(PCB, "add_trace", add_trace)


@needs_kicad_python
@pytest.mark.parametrize(("tool", "fault"), [
    ("propose_route_with_freerouting", "route_delta"),
    ("propose_route_with_freerouting", "writer"),
    ("propose_route_with_retry", "writer"),
])
def test_confirm_refuses_copper_that_is_not_the_routed_copys(tool, fault, stress_board, monkeypatch):
    """Both propose tools hand the routed copy's raw copper to the proposal; confirm_route
    compares the board's new copper with it. (The retry tool is faulted at the writer: a
    route_delta fault is already refused by its trial apply, the next test.)"""
    _fake_freerouting(monkeypatch, _session(_GOOD))
    if fault == "route_delta":
        _route_delta_shifts_one_trace_end(monkeypatch)
    tools = _tools()
    proposal = tools[tool](str(stress_board))
    assert proposal["status"] == "pending_confirmation" and proposal["router_flags"] == [], proposal
    token = proposal["revision_token"]
    if fault == "writer":
        _writer_shifts_one_trace_end(monkeypatch)
    before = stress_board.read_bytes()

    failed = tools["confirm_route"](str(stress_board), token)

    assert (failed["status"], failed["ok"], failed["code"]) == ("error", False, "write_not_verified"), failed
    assert "not the routed copy's" in " ".join(failed["mismatches"]), failed
    assert failed["original_unchanged"] is True and stress_board.read_bytes() == before
    assert token in routing_toolset._PENDING_ROUTES


@needs_kicad_python
def test_a_retry_attempt_whose_copper_is_not_the_routed_copys_records_write_not_verified(
        stress_board, monkeypatch):
    _fake_freerouting(monkeypatch, _session(_GOOD))
    _route_delta_shifts_one_trace_end(monkeypatch)
    pending = set(routing_toolset._PENDING_ROUTES)
    before = stress_board.read_bytes()

    result = _tools()["propose_route_with_retry"](str(stress_board))

    assert result["status"] == "incomplete", result
    assert result["attempts"][0]["code"] == "write_not_verified", result["attempts"]
    assert "not the routed copy's" in result["attempts"][0]["error"]
    assert set(routing_toolset._PENDING_ROUTES) == pending and stress_board.read_bytes() == before


# --- what the board digest covers (delta review 1, finding 2) ------------------------------------


def _project_rule_changed(board: Path) -> None:
    import json

    pro = board.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text(encoding="utf-8"))
    data["board"]["design_settings"]["rules"]["min_copper_edge_clearance"] = 0.5
    write_layer.save_project(data, pro)


def _rules_file_added(board: Path) -> None:
    shutil.copy(FIXTURES / "openmppt_v1_3.kicad_dru", board.with_suffix(".kicad_dru"))


def _view_state_changed(board: Path) -> None:
    """What KiCad writes to ``.kicad_prl`` when it closes: local view state, not design."""
    import json

    prl = board.with_suffix(".kicad_prl")
    data = json.loads(prl.read_text(encoding="utf-8"))
    data["board"]["active_layer"] = 31
    write_layer.save_project(data, prl)


@pytest.mark.parametrize("change", [_project_rule_changed, _rules_file_added], ids=["kicad_pro", "kicad_dru"])
def test_a_project_or_rules_change_during_routing_refuses_the_import(change, stress_board, tmp_path, monkeypatch):
    snapshot = routing_toolset.snapshot_board(stress_board, tmp_path / "snap")
    change(stress_board)
    _fake_import_runner(monkeypatch, CLEAN_REPORT)
    ses = tmp_path / "board.ses"
    ses.write_text(_session(), encoding="utf-8")
    with pytest.raises(routing_toolset.BoardChangedDuringRouting):
        routing_toolset.import_ses_with_kicad(snapshot, ses, tmp_path / "routed")


def test_view_state_kicad_writes_on_close_does_not_refuse_the_import(stress_board, tmp_path, monkeypatch):
    """Coordinator decision (delta review 1): ``.kicad_prl`` is left out of the digest."""
    snapshot = routing_toolset.snapshot_board(stress_board, tmp_path / "snap")
    _view_state_changed(stress_board)
    _fake_import_runner(monkeypatch, CLEAN_REPORT)
    ses = tmp_path / "board.ses"
    ses.write_text(_session(), encoding="utf-8")
    imported = routing_toolset.import_ses_with_kicad(snapshot, ses, tmp_path / "routed")
    assert imported["source_sha256"] == snapshot["source_sha256"]


# --- the runner's writes are all guarded (delta review 1, ratchet gap) ---------------------------

# pcbnew's file writers and the argument that names the file each one writes.
_PCBNEW_WRITERS = {"SaveBoard": 0, "ExportSpecctraDSN": 1}


def test_every_runner_function_that_writes_a_file_is_a_guarded_command():
    """The raw-write ratchet cannot see ``pcbnew.SaveBoard`` or ``ExportSpecctraDSN``. Every
    runner function calling one must be a command in ``_WRITES``, and the path it writes must
    be the very argument ``_WRITES`` sends through the temp-directory guard."""
    import ast

    runner = _runner_module()
    commands = {func.__name__: name for name, (func, _) in runner._COMMANDS.items()}
    tree = ast.parse(routing_toolset._SPECCTRA_RUNNER.read_text(encoding="utf-8"))
    seen = []
    for func in (n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))):
        for call in (n for n in ast.walk(func) if isinstance(n, ast.Call)):
            name = call.func.attr if isinstance(call.func, ast.Attribute) else getattr(call.func, "id", None)
            if name not in _PCBNEW_WRITERS:
                continue
            command = commands.get(func.name)
            assert command in runner._WRITES, f"{func.name} calls {name} but is not a guarded command"
            params = [a.arg for a in func.args.args]
            written = call.args[_PCBNEW_WRITERS[name]]
            assert isinstance(written, ast.Name) and written.id == params[runner._WRITES[command]], (
                f"{func.name}: {name} writes {ast.unparse(written)}, the guard checks "
                f"{params[runner._WRITES[command]]}")
            seen.append((command, name))
    assert sorted(seen) == [("export", "ExportSpecctraDSN"), ("import", "SaveBoard")]


# --- the text an LLM reads (delta review 1, runtime text) -----------------------------------------


def _descriptions() -> dict:
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    return {t.name: t.description for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_confirm_route_describes_every_freerouting_flag():
    described = _descriptions()["confirm_route"]
    for flag in routing_toolset._FREEROUTING_FLAG_MEANINGS:
        assert flag in described, flag


@pytest.mark.parametrize("tool", PROPOSE_TOOLS)
def test_both_propose_tools_describe_the_changed_board_refusal(tool):
    assert "board_changed_during_routing" in _descriptions()[tool]


def test_the_incomplete_import_meaning_covers_copper_kicad_added_beyond_the_session():
    assert "more copper than the session" in routing_toolset._FREEROUTING_FLAG_MEANINGS["ses_import_incomplete"]


@needs_kicad_python
def test_a_retry_refusal_points_at_the_attempts_import_report(stress_board, monkeypatch):
    _fake_freerouting(monkeypatch, _session(_GOOD_WITH_A_POLYGON))
    tools = _tools()
    proposal = tools["propose_route_with_retry"](str(stress_board))
    refused = tools["confirm_route"](str(stress_board), proposal["revision_token"])
    assert refused["status"] == "refused", refused
    assert "attempts[best_attempt - 1].ses_import" in refused["reason"], refused["reason"]
    assert proposal["attempts"][proposal["best_attempt"] - 1]["ses_import"]["ignored_wire_shapes"] == 1
