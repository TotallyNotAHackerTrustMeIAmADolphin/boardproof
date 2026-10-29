"""Tests for KiCadRoutingTools' py_placer/place_route_loop.py wrapper
(krt-capability-suite ticket 08).

Unit tests need no real KRT install: argument construction, the log
streaming/timeout behavior (fake stand-in script, same pattern
test_routing_krt.py uses), and placement_delta's real round-trip through
kicad_tools on real small boards built via its own object model.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp.routers import kicad_placer_tools as placer
from kicad_mcp.routers import kicad_routing_tools as krt


def _fake_checkout(root: Path, place_route_loop_body: str) -> Path:
    (root / "py_router").mkdir(parents=True)
    (root / "py_router" / "route.py").write_text("# stub, only discovery reads this path exists\n", encoding="utf-8")
    (root / "py_placer").mkdir(parents=True)
    (root / "py_placer" / "place_route_loop.py").write_text(place_route_loop_body, encoding="utf-8")
    return root


# ---------------------------------------------------------------------------
# Argument construction
# ---------------------------------------------------------------------------


def test_build_placer_args_defaults_include_sheet_in_group_by(tmp_path):
    args = placer.build_placer_args(tmp_path / "in.kicad_pcb", tmp_path / "out.kicad_pcb")
    assert "sheet" in " ".join(args[args.index("--group-by") + 1 : args.index("--group-by") + 2])
    assert "--relocate" not in args  # ticket 04: measured net-negative, never passed
    assert "--movie" in args
    assert "--verbose" in args


def test_build_placer_args_no_movie_when_disabled(tmp_path):
    args = placer.build_placer_args(tmp_path / "in.kicad_pcb", tmp_path / "out.kicad_pcb", movie=False)
    assert "--no-movie" in args
    assert "--movie" not in args


def test_build_placer_args_always_includes_a_nonempty_route_args_flag(tmp_path):
    """Real bug found via two actual KRT invocations: place_route_loop.py's
    --route-args is REQUIRED and must be non-empty -- both omitting it and
    passing "" hit the same argparse error ("--route-args is required
    (except with --suggest-locks)"), since the tool's own check is
    `if not args.route_args`. Pin the default to always pass a real value."""
    args = placer.build_placer_args(tmp_path / "in.kicad_pcb", tmp_path / "out.kicad_pcb")
    assert "--route-args" in args
    idx = args.index("--route-args")
    assert args[idx + 1]  # must be truthy/non-empty


def test_build_placer_args_passes_route_args_through_verbatim(tmp_path):
    args = placer.build_placer_args(
        tmp_path / "in.kicad_pcb", tmp_path / "out.kicad_pcb", route_args="--escalation board --clearance 0.2"
    )
    idx = args.index("--route-args")
    assert args[idx + 1] == "--escalation board --clearance 0.2"


def test_build_placer_args_lock_repeats_flag_per_ref(tmp_path):
    args = placer.build_placer_args(
        tmp_path / "in.kicad_pcb", tmp_path / "out.kicad_pcb", lock=("J1", "U1")
    )
    assert args.count("--lock") == 2
    assert "J1" in args and "U1" in args


# ---------------------------------------------------------------------------
# run_placement_repair: fake stand-in script, no real KRT/placer needed.
# ---------------------------------------------------------------------------

_FAKE_PLACER_SUCCESS = """\
import sys
from pathlib import Path
sys.stdout.write("Round 0: 3 target parts, 2 failed nets\\n")
sys.stdout.flush()
sys.stderr.write("fake placer warning\\n")
sys.stderr.flush()
Path(sys.argv[2]).write_text("(kicad_pcb fake-repaired)\\n", encoding="utf-8")
sys.stdout.write("FAKE_PLACER_DONE\\n")
"""

_FAKE_PLACER_HANGS = """\
import sys, time
sys.stdout.write("fake placer starting, about to hang\\n")
sys.stdout.flush()
time.sleep(30)
"""


def test_run_placement_repair_streams_log_and_reports_output(tmp_path, monkeypatch):
    checkout = _fake_checkout(tmp_path / "krt", _FAKE_PLACER_SUCCESS)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)

    input_pcb = tmp_path / "board.kicad_pcb"
    input_pcb.write_text("(kicad_pcb)", encoding="utf-8")
    output_pcb = tmp_path / "out" / "board_optimized.kicad_pcb"

    result = placer.run_placement_repair(input_pcb, output_pcb, timeout=30)

    assert result["exit_code"] == 0
    assert result["output_produced"] is True
    stdout_log = Path(result["stdout_log"])
    assert stdout_log.is_file()
    assert stdout_log.name == "board_optimized.placer_repair.log"
    assert "FAKE_PLACER_DONE" in stdout_log.read_text(encoding="utf-8")
    assert "fake placer warning" in Path(result["stderr_log"]).read_text(encoding="utf-8")
    assert result["movie_path"] is None  # fake script never wrote placement.mp4


def test_run_placement_repair_timeout_preserves_log_and_raises(tmp_path, monkeypatch):
    checkout = _fake_checkout(tmp_path / "krt", _FAKE_PLACER_HANGS)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)

    input_pcb = tmp_path / "board.kicad_pcb"
    input_pcb.write_text("(kicad_pcb)", encoding="utf-8")
    output_pcb = tmp_path / "out" / "board_optimized.kicad_pcb"

    with pytest.raises(krt.KicadRoutingToolsError, match="timed out"):
        placer.run_placement_repair(input_pcb, output_pcb, timeout=1)

    stdout_log = output_pcb.with_name("board_optimized.placer_repair.log")
    assert stdout_log.is_file()
    assert "fake placer starting, about to hang" in stdout_log.read_text(encoding="utf-8")
    assert not output_pcb.is_file()


def test_no_graceful_stop_mechanism_exists_for_the_placer(tmp_path):
    """Confirmed by direct source reading (ticket 08): neither
    place_route_loop.py nor place_optimize.py reference KICAD_STOP_FILE or
    any equivalent -- unlike route.py's real one. This test just pins that
    run_placement_repair's signature has no stop_after/graceful-stop
    parameter, so a future edit that silently adds one gets caught by an
    honest module-docstring update, not a stale claim."""
    import inspect

    sig = inspect.signature(placer.run_placement_repair)
    assert "stop_after" not in sig.parameters


# ---------------------------------------------------------------------------
# placement_delta: real kicad_tools round-trip, small real boards.
# ---------------------------------------------------------------------------


def _tiny_board(path: Path, refs_and_positions: dict[str, tuple[float, float, float]]) -> None:
    from kicad_tools.schema import PCB

    from kicad_mcp.sourcing.footprint_gen import generate_footprint

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
    x, y, rotation = delta["R2"]
    assert (x, y, rotation) == pytest.approx((22.5, 12.0, 90.0))


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
    assert delta["R2"] == pytest.approx((22.5, 12.0, 90.0))


@pytest.mark.parametrize("rotation", [0.0, 90.0, 180.0, 270.0])
def test_placement_delta_reports_each_right_angle_rotation(tmp_path, rotation):
    original = tmp_path / "original.kicad_pcb"
    repaired = tmp_path / "repaired.kicad_pcb"
    _tiny_board(original, {"R1": (10.0, 10.0, 45.0)})
    _tiny_board(repaired, {"R1": (10.0, 10.0, rotation)})

    delta = placer.placement_delta(original, repaired)

    assert delta["R1"] == pytest.approx((10.0, 10.0, rotation))


def test_placement_delta_applied_through_kicad_tools_reproduces_the_repaired_raw_positions(tmp_path):
    """The delta is the contract `confirm_autoplacement` applies via
    update_footprint_position; applying it to the original must land every
    footprint on the repaired board's raw (at) values."""
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_file

    original = tmp_path / "original.kicad_pcb"
    repaired = tmp_path / "repaired.kicad_pcb"
    _tiny_board(original, {"R1": (10.0, 10.0, 0.0), "R2": (20.0, 10.0, 0.0)})
    _tiny_board(repaired, {"R1": (14.0, 11.0, 270.0), "R2": (20.0, 10.0, 0.0)})

    delta = placer.placement_delta(original, repaired)
    board = PCB.load(original)
    for ref, (x, y, rotation) in delta.items():
        assert board.update_footprint_position(ref, x, y, rotation)
    applied = tmp_path / "applied.kicad_pcb"
    write_layer.save_pcb(board, applied)

    def raw_at(path, ref):
        for fp in parse_file(str(path)).find_all("footprint"):
            if any(
                p.values[:2] == ["Reference", ref] or str(p.values[:2]) == str(["Reference", ref])
                for p in fp.find_all("property")
            ):
                return [float(v) for v in fp.find("at").values]
        raise AssertionError(f"{ref} not found")

    for ref in ("R1", "R2"):
        got = raw_at(applied, ref) + [0.0] * (3 - len(raw_at(applied, ref)))
        want = raw_at(repaired, ref) + [0.0] * (3 - len(raw_at(repaired, ref)))
        assert got == pytest.approx(want)


def test_placement_delta_treats_equivalent_rotations_as_unchanged(tmp_path):
    """-90 and 270 (and 360 and 0) are the same pose: no spurious delta."""
    original = tmp_path / "original.kicad_pcb"
    repaired = tmp_path / "repaired.kicad_pcb"
    _tiny_board(original, {"R1": (10.0, 10.0, 270.0), "R2": (20.0, 10.0, 0.0)})
    _tiny_board(repaired, {"R1": (10.0, 10.0, -90.0), "R2": (20.0, 10.0, 360.0)})

    assert placer.placement_delta(original, repaired) == {}
