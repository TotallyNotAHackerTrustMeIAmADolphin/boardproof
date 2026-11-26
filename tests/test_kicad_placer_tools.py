"""Tests for the KiCadRoutingTools py_placer/place_simple.py wrapper (issue 257): the availability gate,
the groups file, the subprocess steps (a stand-in for the one process-starting function, never reachable
from a tool), the poses read back from raw tokens, and one real run on a small fixture copy."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from boardproof import write_layer
from boardproof.routers import kicad_placer_tools as placer
from boardproof.routers import kicad_routing_tools as krt
from tests import _krt
from tests._fixture_copy import copy_fixture

FIXTURE = Path(__file__).parent / "fixtures" / "golden_attiny1616_board"


def _fake_checkout(root: Path, version: str = "0.23.8") -> Path:
    (root / "py_router").mkdir(parents=True)
    (root / "py_router" / "route.py").write_text("# stub\n", encoding="utf-8")
    (root / "py_placer").mkdir()
    (root / "py_placer" / "place_simple.py").write_text("# stub\n", encoding="utf-8")
    (root / "py_tools").mkdir()
    (root / "py_tools" / "check_floorplan.py").write_text("# stub\n", encoding="utf-8")
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    return root


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    root = _fake_checkout(tmp_path / "krt")
    monkeypatch.setenv(krt.ENV_PATH, str(root))
    monkeypatch.setattr(placer, "_probe_imports", lambda python: [])
    return root


# ---------------------------------------------------------------------------
# Availability: unavailable is said, never invented
# ---------------------------------------------------------------------------


def test_the_placer_is_available_with_the_pinned_version_and_packages(checkout):
    assert placer.placer_problem() is None


def test_a_missing_script_is_unavailable_naming_the_script(checkout):
    (checkout / "py_placer" / "place_simple.py").unlink()
    problem = placer.placer_problem()
    assert problem and problem.startswith("unavailable") and "place_simple.py" in problem, problem


def test_a_version_other_than_the_pinned_one_is_unavailable_naming_both(tmp_path, monkeypatch):
    root = _fake_checkout(tmp_path / "krt", version="0.99.0")
    monkeypatch.setenv(krt.ENV_PATH, str(root))
    monkeypatch.setattr(placer, "_probe_imports", lambda python: [])
    pinned = krt.load_cli_snapshot()["router_version"]
    problem = placer.placer_problem()
    assert problem and "0.99.0" in problem and pinned in problem and "pinned" in problem, problem


def test_a_missing_package_is_unavailable_naming_it(checkout, monkeypatch):
    monkeypatch.setattr(placer, "_probe_imports", lambda python: ["shapely"])
    problem = placer.placer_problem()
    assert problem and "shapely" in problem and problem.startswith("unavailable"), problem


def test_no_checkout_is_unavailable(tmp_path, monkeypatch):
    monkeypatch.setenv(krt.ENV_PATH, str(tmp_path / "nowhere"))
    problem = placer.placer_problem()
    assert problem and problem.startswith("unavailable"), problem


# ---------------------------------------------------------------------------
# The groups file and the subprocess steps
# ---------------------------------------------------------------------------


def test_the_groups_file_is_one_group_of_every_reference_and_sets_nothing_else():
    doc = placer.groups_document(["U1", "C1", "J1"])
    assert doc["schema"] == 1 and len(doc["groups"]) == 1
    group = doc["groups"][0]
    assert group["name"] == "all" and group["refs"] == ["U1", "C1", "J1"]
    assert set(group) <= {"name", "refs", "why"}  # no edge, side, anchor, near, apart, templates, flippable


def _stand_in(calls, *, fail_step=None, write_output=True):
    def run(args, *, cwd, env, timeout):
        calls.append({"args": args, "cwd": cwd, "env": env, "timeout": timeout})
        script = Path(args[1]).name
        if script == fail_step:
            return subprocess.CompletedProcess(args, 3, "", "boom")
        if script == "check_floorplan.py":
            Path(args[args.index("--emit-intent") + 1]).write_text("{}", encoding="utf-8")
        elif write_output:
            shutil.copyfile(FIXTURE / "golden_attiny1616_board.kicad_pcb", args[3])
            Path(args[args.index("--json") + 1]).write_text(json.dumps({"illegal": []}), encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, "", "")

    return run


def test_run_place_simple_runs_two_steps_with_seed_manifest_and_minimal_environment(checkout, tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(placer, "_run_process", _stand_in(calls))
    board = tmp_path / "b.kicad_pcb"  # never read: the stand-in does not open it
    run_dir = tmp_path / "run"
    result = placer.run_place_simple(board, run_dir, ["R1", "R2"], seed=7, timeout=100)
    assert [Path(c["args"][1]).name for c in calls] == ["check_floorplan.py", "place_simple.py"]
    placed = calls[1]["args"]
    assert placed[placed.index("--seed") + 1] == "7"
    assert json.loads((run_dir / "groups.json").read_text())["groups"][0]["refs"] == ["R1", "R2"]
    for call in calls:
        assert Path(call["env"]["PLACEMENT_MANIFEST"]).parent == run_dir  # the run record stays in the folder
        assert set(call["env"]) <= {*placer._ENV_PASSED_THROUGH, "PYTHONUTF8", "PYTHONIOENCODING", "PLACEMENT_MANIFEST"}
        assert call["timeout"] <= 100
    assert Path(result["output_pcb"]).parent == run_dir and result["exit_code"] == 0


@pytest.mark.parametrize("step", ["check_floorplan.py", "place_simple.py"])
def test_a_failing_step_raises_with_its_stderr(checkout, tmp_path, monkeypatch, step):
    monkeypatch.setattr(placer, "_run_process", _stand_in([], fail_step=step))
    with pytest.raises(krt.KicadRoutingToolsError, match=r"code 3.*boom"):
        placer.run_place_simple(tmp_path / "b.kicad_pcb", tmp_path / "run", ["R1"])


def test_a_timeout_raises_and_adopts_nothing(checkout, tmp_path, monkeypatch):
    def run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs["timeout"])

    monkeypatch.setattr(placer, "_run_process", run)
    with pytest.raises(krt.KicadRoutingToolsError, match="timed out"):
        placer.run_place_simple(tmp_path / "b.kicad_pcb", tmp_path / "run", ["R1"], timeout=5)


def test_exit_zero_without_an_output_board_is_an_error(checkout, tmp_path, monkeypatch):
    monkeypatch.setattr(placer, "_run_process", _stand_in([], write_output=False))
    with pytest.raises(krt.KicadRoutingToolsError, match="wrote no board"):
        placer.run_place_simple(tmp_path / "b.kicad_pcb", tmp_path / "run", ["R1"])


@pytest.mark.needs_krt
def test_the_real_placer_places_a_fixture_copy_and_only_poses_are_read_back(tmp_path):
    """One real run (a few seconds): needs the pinned KiCadRoutingTools and numpy, scipy, shapely."""
    if placer.placer_problem() is not None:
        _krt.missing(placer.placer_problem())  # skips, or fails under KICAD_ROUTING_TOOLS_REQUIRED
    work = tmp_path / "in"
    copy_fixture(FIXTURE, work)
    board = work / "golden_attiny1616_board.kicad_pcb"
    refs = sorted(placer._raw_poses(board)[0])
    first = placer.run_place_simple(board, tmp_path / "run0", refs, seed=0, timeout=120)
    again = placer.run_place_simple(board, tmp_path / "run1", refs, seed=0, timeout=120)
    delta = placer.placement_delta(board, first["output_pcb"])
    assert delta, "the placer moved nothing on a board it was asked to place"
    assert delta == placer.placement_delta(board, again["output_pcb"])  # same seed, same poses
    assert Path(first["output_pcb"]).parent == tmp_path / "run0"


# ---------------------------------------------------------------------------
# placement_delta: real kicad_tools round-trip, small real boards.
# ---------------------------------------------------------------------------


def _tiny_board(path: Path, refs_and_positions: dict[str, tuple[float, float, float]]) -> None:
    from kicad_tools.schema import PCB

    from boardproof.sourcing.footprint_gen import generate_footprint

    chip = path.parent / "0402.kicad_mod"
    if not chip.is_file():
        result = generate_footprint("chip", str(chip), size="0402")
        assert result["status"] == "ok", result

    board = PCB.create(width=100.0, height=100.0)
    for ref, (x, y, rotation) in refs_and_positions.items():
        board.add_footprint_from_file(str(chip), ref, x, y, rotation=rotation)
    write_layer.save_pcb(board, path)


def test_placement_delta_finds_exactly_the_moved_footprints(tmp_path):
    original = tmp_path / "original.kicad_pcb"
    repaired = tmp_path / "repaired.kicad_pcb"
    _tiny_board(original, {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.0, 0.0), "R3": (30.0, 10.0, 0.0)})
    _tiny_board(repaired, {"R1": (10.0, 10.0, 0.0), "R2": (22.5, 12.0, 90.0), "R3": (30.0, 10.0, 0.0)})

    delta = placer.placement_delta(original, repaired)

    assert set(delta.keys()) == {"R2"}
    assert delta["R2"] == pytest.approx(placer._raw_poses(repaired)[0]["R2"])  # the file's own page frame


def test_placement_delta_on_identical_boards_is_empty(tmp_path):
    original = tmp_path / "original.kicad_pcb"
    repaired = tmp_path / "repaired.kicad_pcb"
    positions = {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.0, 0.0)}
    _tiny_board(original, positions)
    _tiny_board(repaired, positions)

    assert placer.placement_delta(original, repaired) == {}


# ---------------------------------------------------------------------------
# CLAUDE.md hard rule 2: placement facts come from the raw (at X Y rot) nodes,
# never from Footprint.position / .rotation (ticket 08).
# ---------------------------------------------------------------------------


@pytest.fixture
def untrusted_footprint_properties(monkeypatch):
    """Make the kicad_tools convenience properties unusable.

    If placement_delta still derives any fact from them, it fails loudly here.
    """
    from kicad_tools.schema import Footprint

    def _refuse(self):
        raise AssertionError("placement_delta must read the raw (at X Y rot) node")

    def _store(name):
        def setter(self, value):
            self.__dict__[f"_untrusted_{name}"] = value

        return setter

    # Writing stays possible (the loader sets the fields); reading is the violation.
    monkeypatch.setattr(Footprint, "position", property(_refuse, _store("position")), raising=False)
    monkeypatch.setattr(Footprint, "rotation", property(_refuse, _store("rotation")), raising=False)


def test_placement_delta_does_not_use_footprint_convenience_properties(
    tmp_path, untrusted_footprint_properties
):
    original = tmp_path / "original.kicad_pcb"
    repaired = tmp_path / "repaired.kicad_pcb"
    _tiny_board(original, {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.0, 0.0)})
    _tiny_board(repaired, {"R1": (10.0, 10.0, 0.0), "R2": (22.5, 12.0, 90.0)})

    delta = placer.placement_delta(original, repaired)

    assert set(delta) == {"R2"}
    assert delta["R2"] == pytest.approx(placer._raw_poses(repaired)[0]["R2"])


@pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0])
def test_placement_delta_reports_each_right_angle_rotation(tmp_path, rotation):
    original = tmp_path / "original.kicad_pcb"
    repaired = tmp_path / "repaired.kicad_pcb"
    _tiny_board(original, {"R1": (10.0, 10.0, 45.0)})
    _tiny_board(repaired, {"R1": (10.0, 10.0, rotation)})

    delta = placer.placement_delta(original, repaired)

    assert delta["R1"][2] == pytest.approx(rotation)
    assert delta["R1"] == pytest.approx(placer._raw_poses(repaired)[0]["R1"])


def test_placement_delta_applied_through_write_poses_reproduces_the_placed_raw_poses(tmp_path):
    """The delta is what write_poses takes: applying it to the original lands every footprint on the
    placed board's raw (at) values."""
    from boardproof import placement_anchor

    original = tmp_path / "original.kicad_pcb"
    repaired = tmp_path / "repaired.kicad_pcb"
    _tiny_board(original, {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.0, 0.0)})
    _tiny_board(repaired, {"R1": (14.0, 11.0, 270.0), "R2": (20.0, 10.0, 0.0)})

    delta = placer.placement_delta(original, repaired)
    placement_anchor.write_poses(original, delta)

    got, want = placer._raw_poses(original)[0], placer._raw_poses(repaired)[0]
    for ref in ("R1", "R2"):
        assert got[ref] == pytest.approx(want[ref])


def test_placement_delta_treats_equivalent_rotations_as_unchanged(tmp_path):
    """-90 and 270 (and 360 and 0) are the same pose: no spurious delta."""
    original = tmp_path / "original.kicad_pcb"
    repaired = tmp_path / "repaired.kicad_pcb"
    _tiny_board(original, {"R1": (10.0, 10.0, 270.0), "R2": (20.0, 10.0, 0.0)})
    _tiny_board(repaired, {"R1": (10.0, 10.0, -90.0), "R2": (20.0, 10.0, 360.0)})

    assert placer.placement_delta(original, repaired) == {}


def test_a_placer_board_with_other_footprints_is_refused(tmp_path):
    original = tmp_path / "original.kicad_pcb"
    placed = tmp_path / "placed.kicad_pcb"
    _tiny_board(original, {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.0, 0.0)})
    _tiny_board(placed, {"R1": (10.0, 10.0, 0.0)})
    with pytest.raises(ValueError, match="R2"):
        placer.placement_delta(original, placed)


def test_a_timeout_kills_the_child_and_its_own_children(tmp_path):
    import os
    import subprocess
    import sys
    import time

    pidfile = tmp_path / "grandchild.pid"
    code = (
        "import subprocess,sys,time;"
        "g=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
        f"open(r'{pidfile}','w').write(str(g.pid));time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired):
        placer._run_process([sys.executable, "-c", code], cwd=str(tmp_path), env=dict(os.environ), timeout=4)
    pid = int(pidfile.read_text())
    deadline = time.time() + 10
    while time.time() < deadline and _alive(pid):
        time.sleep(0.2)
    assert not _alive(pid), "the grandchild survived the timeout"


def _alive(pid: int) -> bool:
    import subprocess
    import os

    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, errors="replace").stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True
