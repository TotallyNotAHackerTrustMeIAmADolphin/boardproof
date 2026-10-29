"""The real backends behind the runner's backend seam.

KiCadRoutingTools runs for real on a copy of a small fixture in ``tmp_path`` (about 4 s per
run). Freerouting needs Java and a jar the repository never bundles: its adapter is tested
at the subprocess boundary and its availability is reported honestly (UNAVAILABLE when the
jar is absent, never a failed attempt).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp import rung_backends, rung_runner
from kicad_mcp.routers import kicad_routing_tools as krt_tools

FIXTURES = Path(__file__).parent / "fixtures"
needs_krt = pytest.mark.needs_krt  # skip with the probe's reason, or fail in the gate run (build ticket 53)

KRT_BASELINE = {"escalation": "board", "grid_step_mm": None, "max_iterations": None, "ordering": None}
KICAD_SPECCTRA = rung_backends.routing_toolset.kicad_specctra_availability()
needs_kicad_python = pytest.mark.skipif(
    not KICAD_SPECCTRA["available"], reason=f"KiCad's bundled Python is not usable here: {KICAD_SPECCTRA['reason']}")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture
def stress_board(tmp_path):
    """krt_stress_congestion: 30x36 mm, a real sibling .kicad_pro, KRT routes it in ~4 s."""
    project = tmp_path / "proj"
    project.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        shutil.copy(FIXTURES / f"krt_stress_congestion{suffix}", project)
    return project / "krt_stress_congestion.kicad_pcb"


# --- invariants: what must be identical before and after an attempt ---------------------


def test_invariants_ignore_added_copper_but_catch_a_moved_footprint(stress_board, tmp_path):
    from kicad_tools.schema import PCB

    before = rung_backends.board_invariants(stress_board)
    assert set(before) == {"placement", "nets", "setup", "graphics", "zones", "copper"}

    routed = tmp_path / "routed.kicad_pcb"
    shutil.copy(stress_board, routed)
    pcb = PCB.load(routed)
    pcb.add_trace((10.0, 10.0), (12.0, 10.0), width=0.25, layer="F.Cu", net="SIG1")
    write_layer.save_pcb(pcb, routed)
    assert (routed.read_text().count("(segment")) == 1  # the raw file has the new copper
    assert rung_runner._changed(before, rung_backends.board_invariants(routed)) == []

    moved = tmp_path / "moved.kicad_pcb"
    pcb = PCB.load(stress_board)
    footprint = next(c for c in pcb._sexp.children if c.name == "footprint")
    at = footprint.find("at")
    at.set_value(0, float(at.values[0]) + 1.0)  # raw (at ...) node, not a convenience property
    write_layer.save_pcb(pcb, moved)
    changed = rung_backends.board_invariants(moved)
    assert rung_runner._changed(before, changed) == ["placement"]


# --- the KiCadRoutingTools adapter, for real ---------------------------------------------


@needs_krt
def test_krt_adapter_routes_a_real_copy_and_reports_router_facts(stress_board, tmp_path):
    pro_before = _sha(stress_board.with_suffix(".kicad_pro"))
    attempt_dir = tmp_path / "attempt"
    attempt_dir.mkdir()
    backend = rung_backends.krt_backend()

    outcome = backend(stress_board, dict(KRT_BASELINE), 120.0, attempt_dir / "router.log")

    assert outcome["status"] == "ok"
    assert stress_board.read_text().count("(segment") > 0  # the delta was applied to the board
    assert _sha(stress_board.with_suffix(".kicad_pro")) == pro_before  # rules never adopted
    facts = outcome["facts"]
    assert facts["exit_code"] in (0, 3)
    # The version the CLI snapshot pins; test_router_cli_snapshot checks the live router against it.
    assert facts["router_version"] == krt_tools.load_cli_snapshot()["router_version"]
    assert facts["logged_parameters"]["strict_sizes"] is True
    assert facts["elapsed_s"] > 0
    # Scratch and logs live beside the attempt log, never in the project directory.
    leftovers = {p.name for p in stress_board.parent.iterdir()}
    assert leftovers == {"krt_stress_congestion.kicad_pcb", "krt_stress_congestion.kicad_pro",
                         "krt_stress_congestion.kicad_prl"}
    assert (attempt_dir / "router_scratch").is_dir()


@needs_krt
def test_krt_adapter_reports_a_real_router_timeout_as_timeout(stress_board, tmp_path):
    before = _sha(stress_board)
    (tmp_path / "a").mkdir()
    outcome = rung_backends.krt_backend()(stress_board, dict(KRT_BASELINE), 1.0, tmp_path / "a" / "router.log")
    assert outcome["status"] == "timeout"
    assert outcome["cause"] == "router-timeout"
    assert _sha(stress_board) == before  # no partial output was adopted


def test_krt_adapter_refuses_a_lever_that_is_not_predeclared(stress_board, tmp_path):
    before = _sha(stress_board)
    (tmp_path / "a").mkdir()
    outcome = rung_backends.krt_backend()(
        stress_board, {**KRT_BASELINE, "clearance_mm": 0.05}, 60.0, tmp_path / "a" / "router.log"
    )
    assert outcome["status"] == "failure"
    assert outcome["cause"] == "router-failure"
    assert "clearance_mm" in outcome["facts"]["error"]
    assert _sha(stress_board) == before


def test_krt_adapter_refuses_a_board_with_a_filled_pour(tmp_path):
    src = FIXTURES / "zone_obstacle"
    project = tmp_path / "zone"
    shutil.copytree(src, project)
    board = project / "zone_obstacle.kicad_pcb"
    (tmp_path / "a").mkdir()
    outcome = rung_backends.krt_backend()(board, dict(KRT_BASELINE), 60.0, tmp_path / "a" / "router.log")
    assert outcome["status"] == "failure"
    assert outcome["cause"] == "input-refused"
    assert "filled" in outcome["facts"]["error"]


# --- availability, honestly ------------------------------------------------------------------


def test_freerouting_is_unavailable_without_a_jar(monkeypatch):
    monkeypatch.delenv("FREEROUTING_JAR_PATH", raising=False)
    result = rung_backends.freerouting_availability()
    assert result["available"] is False
    assert "FREEROUTING_JAR_PATH" in result["reason"]


def test_freerouting_is_unavailable_when_the_jvm_cannot_load_the_jar(tmp_path, monkeypatch):
    jar = tmp_path / "freerouting.jar"
    jar.write_bytes(b"not a real jar")
    monkeypatch.setenv("FREEROUTING_JAR_PATH", str(jar))

    def fake_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, b"", b"Exception in thread main java.lang.UnsupportedClassVersionError: class file version 69"
        )

    monkeypatch.setattr(rung_backends.subprocess, "run", fake_run)
    monkeypatch.setattr(rung_backends.routing_toolset, "find_java", lambda: Path("java"))
    result = rung_backends.freerouting_availability()
    assert result["available"] is False
    assert "UnsupportedClassVersionError" in result["reason"]


def test_freerouting_is_available_with_java_and_a_loadable_jar(tmp_path, monkeypatch):
    jar = tmp_path / "freerouting.jar"
    jar.write_bytes(b"jar bytes")
    monkeypatch.setenv("FREEROUTING_JAR_PATH", str(jar))
    monkeypatch.setattr(rung_backends.routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(
        rung_backends.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, b"INFO   Freerouting v2.3.0 (build-date: 2025-01-01)", b""),
    )
    # KiCad's bundled Python is probed separately (its own tests: test_pcbnew_specctra.py)
    kicad = {"available": True, "reason": None, "python": "kicad-python", "pcbnew_version": "10.0.7"}
    monkeypatch.setattr(rung_backends, "_kicad_specctra_problem", lambda: (None, kicad))
    result = rung_backends.freerouting_availability()
    assert result["available"] is True
    assert (result["versions"]["kicad_python"], result["versions"]["pcbnew"]) == ("kicad-python", "10.0.7")
    assert result["versions"]["freerouting_jar_sha256"] == hashlib.sha256(b"jar bytes").hexdigest()
    assert result["versions"]["freerouting"] == "2.3.0"


# --- the Freerouting adapter at its subprocess boundary ----------------------------------------


def test_freerouting_adapter_passes_the_predeclared_levers_and_maps_a_timeout(stress_board, tmp_path, monkeypatch):
    seen = {}

    def fake_freerouting(dsn, ses, **kwargs):
        seen.update(kwargs)
        error = rung_backends.routing_toolset.FreeroutingTimeout("Freerouting timed out after 7s")
        error.timeout, error.elapsed_s = 7, 7.0
        raise error

    monkeypatch.setattr(rung_backends.routing_toolset, "run_freerouting", fake_freerouting)
    monkeypatch.setattr(rung_backends, "_freerouting_ready", lambda: None)
    before = _sha(stress_board)
    (tmp_path / "a").mkdir()
    params = {"max_passes": 40, "optimization_strategy": "hybrid", "selection_strategy": "prioritized"}

    outcome = rung_backends.freerouting_backend()(stress_board, params, 7.0, tmp_path / "a" / "router.log")

    assert seen == {"max_passes": 40, "timeout": 7, "optimization_strategy": "hybrid",
                    "selection_strategy": "prioritized"}
    assert (outcome["status"], outcome["cause"], outcome["router_elapsed_s"]) == ("timeout", "router-timeout", 7.0)
    assert _sha(stress_board) == before


@needs_kicad_python
def test_freerouting_adapter_imports_the_ses_through_kicad_and_records_the_self_report_only_as_a_fact(
        stress_board, tmp_path, monkeypatch):
    """The session is KiCad's convention (y negated, library_out); KiCad's importer reads it
    into a scratch copy and only the new copper reaches the board, via size and drill kept."""
    def fake_freerouting(dsn, ses, **kwargs):
        assert Path(dsn).read_text(encoding="utf-8").startswith("(pcb ")  # KiCad's DSN
        Path(ses).write_text(
            '(session x (base_design x) (placement (resolution um 10)) (was_is) (routes (resolution um 10) '
            '(parser (host_cad "x") (host_version "1")) (library_out (padstack "Via[0-1]_800:400_um" '
            '(shape (circle F.Cu 8000 0 0)) (shape (circle B.Cu 8000 0 0)) (attach off))) (network_out '
            '(net SIG1 (wire (path F.Cu 2500 100000 -100000 200000 -100000)) '
            '(via "Via[0-1]_800:400_um" 200000 -100000)))))'
        )
        return {"self_reported_score": 0.0, "self_reported_unrouted": 0, "self_reported_violations": 0,
                "optimizer_plateaued": False, "ignored_layers": []}

    monkeypatch.setattr(rung_backends.routing_toolset, "run_freerouting", fake_freerouting)
    monkeypatch.setattr(rung_backends, "_freerouting_ready", lambda: None)
    (tmp_path / "a").mkdir()
    params = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None}

    outcome = rung_backends.freerouting_backend()(stress_board, params, 60.0, tmp_path / "a" / "router.log")

    assert outcome["status"] == "ok", outcome
    root = write_layer.load(stress_board)  # the reloaded board, raw tokens
    segments = [c for c in root.children if c.name == "segment"]
    vias = [c for c in root.children if c.name == "via"]
    assert [(tuple(s.find("start").values), tuple(s.find("end").values)) for s in segments] == [
        ((10.0, 10.0), (20.0, 10.0))]
    assert [(tuple(v.find("at").values), v.find("size").values[0], v.find("drill").values[0]) for v in vias] == [
        ((20.0, 10.0), 0.8, 0.4)]
    facts = outcome["facts"]
    assert facts["ses_import"]["skipped_items"] == 0 and facts["removed_copper"] == 0
    assert facts["dsn_exporter"].startswith("pcbnew ")
    assert facts["freerouting_self_report"]["self_reported_unrouted"] == 0
    assert "freerouting_self_report" not in {k for k in outcome if k != "facts"}
    assert {p.name for p in stress_board.parent.iterdir()} == {
        "krt_stress_congestion.kicad_pcb", "krt_stress_congestion.kicad_pro", "krt_stress_congestion.kicad_prl"}


@needs_kicad_python
def test_freerouting_adapter_works_when_the_run_directory_is_not_a_temp_directory(
        stress_board, tmp_path, monkeypatch):
    """Real runs keep their logs under the local ``runs/`` folder (not a temp directory),
    and KiCad's runner writes only inside one (review round 1, finding 9): the adapter does
    KiCad's export and import in a temporary directory and keeps the DSN and SES beside the
    log for the audit trail."""
    import tempfile

    system_temp = tmp_path / "system_temp"
    system_temp.mkdir()
    for name in ("TMP", "TEMP", "TMPDIR"):
        monkeypatch.setenv(name, str(system_temp))
    monkeypatch.setattr(tempfile, "tempdir", None)

    def fake_freerouting(dsn, ses, **kwargs):
        Path(ses).write_text(
            '(session x (base_design x) (placement (resolution um 10)) (was_is) (routes (resolution um 10) '
            '(parser (host_cad "x") (host_version "1")) (library_out (padstack "Via[0-1]_800:400_um" '
            '(shape (circle F.Cu 8000 0 0)) (shape (circle B.Cu 8000 0 0)) (attach off))) (network_out '
            '(net SIG1 (wire (path F.Cu 2500 100000 -100000 200000 -100000))))))'
        )
        return {"self_reported_score": 0.0}

    monkeypatch.setattr(rung_backends.routing_toolset, "run_freerouting", fake_freerouting)
    monkeypatch.setattr(rung_backends, "_freerouting_ready", lambda: None)
    run_dir = tmp_path / "runs" / "a"
    run_dir.mkdir(parents=True)
    params = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None}

    outcome = rung_backends.freerouting_backend()(stress_board, params, 60.0, run_dir / "router.log")

    assert outcome["status"] == "ok", outcome
    assert [c.name for c in write_layer.load(stress_board).children].count("segment") == 1
    assert {p.name for p in (run_dir / "router_scratch").iterdir()} >= {"board.dsn", "board.ses"}
    # the KiCad work directory is gone (KiCad itself keeps an org.kicad.kicad folder in TEMP)
    assert [p.name for p in system_temp.iterdir() if p.name.startswith("kicad_mcp")] == []


_ONE_TRACK_SESSION = (
    '(session x (base_design x) (placement (resolution um 10)) (was_is) (routes (resolution um 10) '
    '(parser (host_cad "x") (host_version "1")) (library_out (padstack "Via[0-1]_800:400_um" '
    '(shape (circle F.Cu 8000 0 0)) (shape (circle B.Cu 8000 0 0)) (attach off))) (network_out '
    '(net SIG1 (wire (path F.Cu 2500 100000 -100000 200000 -100000))))))'
)


def _freerouting_writes(monkeypatch, session: str, during=None) -> None:
    def fake_freerouting(dsn, ses, **kwargs):
        if during is not None:
            during()
        Path(ses).write_text(session)
        return {"self_reported_score": 0.0}

    monkeypatch.setattr(rung_backends.routing_toolset, "run_freerouting", fake_freerouting)
    monkeypatch.setattr(rung_backends, "_freerouting_ready", lambda: None)


@needs_kicad_python
def test_freerouting_adapter_refuses_copper_that_is_not_the_routed_copys(stress_board, tmp_path, monkeypatch):
    """Delta review 1, finding 1: the adapter applies with the routed copy's raw copper as the
    expected copper. One trace end moved by 1 um in the specs (counts equal) is refused."""
    _freerouting_writes(monkeypatch, _ONE_TRACK_SESSION)
    real = krt_tools.route_delta

    def shifted(original, routed):
        delta = real(original, routed)
        spec = delta["trace_specs"][0]
        delta["trace_specs"][0] = {**spec, "end": [spec["end"][0] + 0.001, spec["end"][1]]}
        return delta

    monkeypatch.setattr(krt_tools, "route_delta", shifted)
    before = _sha(stress_board)
    (tmp_path / "a").mkdir()
    params = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None}

    outcome = rung_backends.freerouting_backend()(stress_board, params, 60.0, tmp_path / "a" / "router.log")

    assert (outcome["status"], outcome["facts"]["code"]) == ("failure", "write_not_verified"), outcome
    assert "not the routed copy's" in outcome["facts"]["error"]
    assert _sha(stress_board) == before


@needs_kicad_python
def test_freerouting_adapter_names_a_board_changed_during_routing(stress_board, tmp_path, monkeypatch):
    """Delta review 1, finding 3: the refusal reaches the batch record by its code."""
    edit = {"start": [30.0, 30.0], "end": [32.0, 30.0], "width_mm": 0.25, "layer": "F.Cu", "net": "SIG1"}
    _freerouting_writes(monkeypatch, _ONE_TRACK_SESSION,
                        during=lambda: rung_backends.routing_toolset._apply_specs(stress_board, [edit], []))
    (tmp_path / "a").mkdir()
    params = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None}

    outcome = rung_backends.freerouting_backend()(stress_board, params, 60.0, tmp_path / "a" / "router.log")

    assert (outcome["status"], outcome["facts"]["code"]) == ("failure", "board_changed_during_routing"), outcome
    segments = [c for c in write_layer.load(stress_board).children if c.name == "segment"]
    assert len(segments) == 1  # the user's edit; nothing of the routed copy reached the board


def test_freerouting_adapter_is_unavailable_without_kicads_python(stress_board, tmp_path, monkeypatch):
    """No silent fallback to another DSN writer: without KiCad's bundled Python the backend is
    UNAVAILABLE for the batch, and an attempt refuses before Freerouting runs."""
    monkeypatch.setattr(rung_backends, "_freerouting_probe", lambda: (None, "2.5.0"))
    monkeypatch.setenv(rung_backends.routing_toolset.KICAD_PYTHON_ENV, str(tmp_path / "missing" / "python.exe"))
    called = []
    monkeypatch.setattr(rung_backends.routing_toolset, "run_freerouting", lambda *a, **k: called.append(1))
    info = rung_backends.freerouting_availability()
    assert info["available"] is False and "KiCad" in info["reason"], info
    before = _sha(stress_board)
    (tmp_path / "a").mkdir()
    params = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None}
    outcome = rung_backends.freerouting_backend()(stress_board, params, 60.0, tmp_path / "a" / "router.log")
    assert (outcome["status"], outcome["cause"]) == ("failure", "router-failure")
    assert "KiCad" in outcome["facts"]["error"] and called == []
    assert _sha(stress_board) == before


# --- one real scored run, end to end ----------------------------------------------------------


@needs_krt
def test_one_real_scored_run_with_krt_and_the_real_gate(stress_board, tmp_path):
    """Real router, real kicad-cli, real gate (3 DRC runs per attempt). Two planned
    attempts instead of five, so the run is honestly labelled unofficial."""
    from kicad_mcp import verify_board

    baseline = verify_board.snapshot_rules(stress_board)
    plan = rung_runner.load_protocol()["backends"]["krt"]["plan"][:2]

    def gate(board):
        return verify_board.verify_board(
            board, parity="not_applicable", parity_reason="fixture has no schematic", baseline=baseline
        )

    report = rung_runner.run_rung(
        rung="fixture", backend_name="krt", board=stress_board,
        rule_files=[stress_board.with_suffix(".kicad_pro")], plan=plan,
        backend=rung_backends.krt_backend(), gate=gate, baseline=baseline,
        tool_versions=rung_backends.tool_versions("krt"), runs_root=tmp_path / "runs",
        attempt_timeout_s=180.0, invariants=rung_backends.board_invariants,
    )

    assert report["attempts_planned"] == 2 and report["attempts_run"] == 2
    assert report["unofficial"] is True and report["autonomous_pass"] is False
    for attempt in report["attempts"]:
        assert attempt["router_wall_s"] > 0.5  # a real router run, not a stub
        assert attempt["verify_wall_s"] > 0
        assert attempt["gate"]["verdict"] in ("PASS", "FAIL", "INCOMPLETE")
        assert attempt["invariant_violations"] == []
        assert attempt["router_facts"]["exit_code"] in (0, 3)
        assert attempt["status"] == attempt["gate"]["verdict"]  # only the gate's verdict decides
    assert report["assisted"] is False, report["assisted_reasons"]
