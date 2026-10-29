"""Tests for the pour step (src/kicad_mcp/pour_step.py).

Contract: build ticket 4.
Seams: ``propose_pour_step`` / ``confirm_pour_step`` (public), and
``read_zone_polygons`` (raw S-expression reader). ``kicad-cli`` and, where noted,
the real KiCadRoutingTools ``route_planes.py`` run for real; the only injected
boundary is the router callable (to stage a pour the real router would not make).

The fixture is the stock ``simple_led`` board (two layers, three nets) plus one
SMD GND/VCC capacitor appended through ``kicad_tools`` nodes, so a GND pour on
B.Cu has to reach an F.Cu pad. Expected coordinates are literals read off the
fixture, not recomputed by the code under test.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp import pour_step
from kicad_mcp import verify_board as gate
from kicad_mcp.kicad_cli import run_drc
from kicad_mcp.routers import kicad_routing_tools as krt

FIXTURES = Path(__file__).parent / "fixtures"
SIMPLE_LED = FIXTURES / "simple_led.kicad_pcb"
NAME = "simple_led.kicad_pcb"

# simple_led's outline is (136, 77.5)-(161, 97.5); the router insets the pour by 0.5 mm.
# These are PAGE-absolute coordinates as written in the file.
RAW_POLYGON = [(136.5, 78.0), (160.5, 78.0), (160.5, 97.0), (136.5, 97.0)]

_C1 = """(footprint "Capacitor_SMD:C_0805_2012Metric"
  (layer "F.Cu") (uuid "00000000-0000-4000-8000-0000000000c1") (at 150 92 0)
  (fp_text reference "C1" (at 0 -1.5) (layer "F.SilkS") (uuid "00000000-0000-4000-8000-0000000000c2")
    (effects (font (size 1 1) (thickness 0.15))))
  (fp_text value "100n" (at 0 1.5) (layer "F.Fab") (uuid "00000000-0000-4000-8000-0000000000c3")
    (effects (font (size 1 1) (thickness 0.15))))
  (pad "1" smd roundrect (at -0.95 0 0) (size 1.0 1.45) (layers "F.Cu" "F.Paste" "F.Mask")
    (roundrect_rratio 0.25) (net 1 "VCC"))
  (pad "2" smd roundrect (at 0.95 0 0) (size 1.0 1.45) (layers "F.Cu" "F.Paste" "F.Mask")
    (roundrect_rratio 0.25) (net 3 "GND")))
"""


def build_board(dest: Path, *, with_capacitor: bool = True) -> Path:
    """Copy ``simple_led`` (no schematic) and add C1 through kicad_tools nodes."""
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    dest.mkdir(parents=True)
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        shutil.copy(FIXTURES / f"simple_led{suffix}", dest / f"simple_led{suffix}")
    pcb = dest / NAME
    if with_capacitor:
        board = PCB.load(pcb)
        board._sexp.append(parse_string(_C1))
        write_layer.save_pcb(board, pcb)
    return pcb


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def project_hashes(pcb: Path) -> dict[str, str]:
    return {p.name: sha(p) for p in sorted(pcb.parent.iterdir()) if p.is_file()}


@pytest.fixture(scope="module")
def module_baseline(tmp_path_factory):
    """The pinned rule baseline of the plain fixture (one kicad-cli run, shared)."""
    pcb = build_board(tmp_path_factory.mktemp("baseline") / "proj")
    return gate.snapshot_rules(pcb)


@pytest.fixture()
def board(tmp_path) -> Path:
    return build_board(tmp_path / "proj")


# --- slice 1: no pour requested -------------------------------------------------


def test_step_is_skipped_with_a_reason_when_no_pour_is_requested(board):
    before = project_hashes(board)
    result = pour_step.propose_pour_step(board, pours={})
    assert result["status"] == "skipped"
    assert "no pour requested" in result["reason"]
    assert result["revision_token"] is None
    assert project_hashes(board) == before


# --- slice 2: raw zone polygon, page versus board origin -------------------------

_ZONE = """(zone (net 3) (net_name "GND") (layer "B.Cu") (uuid "00000000-0000-4000-8000-0000000000a1")
  (hatch edge 0.5) (connect_pads yes (clearance 0.2)) (min_thickness 0.1)
  (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))
  (polygon (pts (xy 136.5 78) (xy 160.5 78) (xy 160.5 97) (xy 136.5 97))))
"""


def add_zone(pcb: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    board = PCB.load(pcb)
    board._sexp.append(parse_string(_ZONE))
    write_layer.save_pcb(board, pcb)


def test_the_convenience_property_really_is_origin_shifted_on_this_fixture(board):
    """Pins the premise of the next test: Zone.polygon is board-relative, the file is page-absolute."""
    from kicad_tools.schema import PCB

    add_zone(board)
    shifted = PCB.load(board).zones[0].polygon
    assert shifted[0] == pytest.approx((0.5, 0.5))
    assert shifted[0] != RAW_POLYGON[0]


def test_zone_polygon_is_read_from_the_raw_node_never_the_convenience_property(board, monkeypatch):
    from kicad_tools.schema import pcb as pcb_module

    add_zone(board)

    def boom(self):
        raise AssertionError("pour_step must read the raw (polygon (pts (xy ...))) node")

    monkeypatch.setattr(pcb_module.Zone, "polygon", property(boom), raising=False)
    zones = pour_step.read_zone_polygons(board)
    assert len(zones) == 1
    assert zones[0]["net"] == "GND"
    assert zones[0]["layer"] == "B.Cu"
    assert zones[0]["polygon"] == RAW_POLYGON
    assert zones[0]["filled_in_file"] is False


# --- slice 3: propose is a dry run ------------------------------------------------


def test_propose_returns_a_token_and_writes_nothing(board, tmp_path):
    before = project_hashes(board)
    out_dir = tmp_path / "poured"
    result = pour_step.propose_pour_step(board, pours={"GND": "B.Cu"}, output_dir=out_dir)
    assert result["status"] == "proposed"
    assert result["revision_token"]
    assert result["pours"] == [{"net": "GND", "layer": "B.Cu"}]
    assert result["output_pcb"] == str(out_dir / NAME)
    assert not out_dir.exists()
    assert project_hashes(board) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


@pytest.mark.parametrize(
    ("pours", "fragment"),
    [
        ({"NOSUCHNET": "B.Cu"}, "NOSUCHNET"),
        ({"GND": "In1.Cu"}, "In1.Cu"),
        ({"GND": "F.SilkS"}, "F.SilkS"),
    ],
)
def test_propose_rejects_a_net_or_layer_the_board_does_not_have(board, tmp_path, pours, fragment):
    result = pour_step.propose_pour_step(board, pours=pours, output_dir=tmp_path / "o")
    assert result["status"] == "error"
    assert fragment in result["reason"]
    assert result["revision_token"] is None


def test_propose_skips_a_pour_the_board_already_has(board, tmp_path):
    add_zone(board)
    result = pour_step.propose_pour_step(board, pours={"GND": "B.Cu"}, output_dir=tmp_path / "o")
    assert result["status"] == "skipped"
    assert "already" in result["reason"]
    assert "GND" in result["reason"]


def test_propose_refuses_an_output_dir_inside_the_source_project(board):
    result = pour_step.propose_pour_step(board, pours={"GND": "B.Cu"}, output_dir=board.parent / "sub")
    assert result["status"] == "error"
    assert "source" in result["reason"]


# --- slice 4: the real router, end to end ------------------------------------------

PARITY = {"parity": "not_applicable", "parity_reason": "fixture board has no schematic"}


needs_router = pytest.mark.needs_krt  # skip with the reason, or fail in the gate run (build ticket 53)


def _propose(board: Path, out_dir: Path, pours=None) -> str:
    result = pour_step.propose_pour_step(board, pours=pours or {"GND": "B.Cu"}, output_dir=out_dir)
    assert result["status"] == "proposed", result
    return result["revision_token"]


@pytest.fixture(scope="module")
def real_pour(tmp_path_factory):
    """One real plane-script run (real router, real kicad-cli) shared by the tests below."""
    root = tmp_path_factory.mktemp("real_pour")
    board = build_board(root / "proj")
    baseline = gate.snapshot_rules(board)
    before = project_hashes(board)
    out_dir = root / "poured"
    token = _propose(board, out_dir)
    existed_at_grading: list[bool] = []

    def recording_drc(pcb, **kwargs):
        existed_at_grading.append(out_dir.exists())
        return run_drc(pcb, **kwargs)

    result = pour_step.confirm_pour_step(
        board, token, baseline=baseline, timeout=120, drc=recording_drc, **PARITY
    )
    return {
        "board": board, "baseline": baseline, "before": before, "out_dir": out_dir,
        "result": result, "existed_at_grading": existed_at_grading,
    }


@needs_router
def test_real_router_pour_is_graded_then_carried_onto_a_new_revision(real_pour):
    result, out_dir = real_pour["result"], real_pour["out_dir"]
    assert result["status"] == "poured", result
    out_pcb = Path(result["output_pcb"])
    assert out_pcb == out_dir / NAME and out_pcb.is_file()

    # The zone, read from the raw file of the new revision: GND on B.Cu, page-absolute corners.
    zones = pour_step.read_zone_polygons(out_pcb)
    assert [(z["net"], z["layer"], z["polygon"]) for z in zones] == [("GND", "B.Cu", RAW_POLYGON)]
    (zone,) = result["zones"]
    assert (zone["net"], zone["layer"], zone["polygon"]) == ("GND", "B.Cu", RAW_POLYGON)
    assert zone["uuid"] == zones[0]["uuid"]
    assert zone["filled_in_file"] is False
    # fill evidence is measured in kicad-cli's refilled copy, not asserted
    assert zone["refilled_by_kicad_cli"] is True
    assert zone["fill_polygons"] >= 1
    assert zone["fill_fraction"] >= pour_step.MIN_FILL_FRACTION
    assert 0 < zone["filled_area_mm2"] <= zone["outline_area_mm2"] == pytest.approx(24 * 19)
    assert "OUTLINE only" in result["fill_note"]

    # The gate's own report. The only open GND item is C1 pad 2, an F.Cu pad that the
    # plane script leaves for the routing step (it places no taps): named as pending,
    # still listed by the gate, and not an isolation caused by the pour.
    gnd_items = [
        item["description"]
        for entry in result["gate"]["unconnected"]
        if entry["net"] == "GND"
        for item in entry["items"]
    ]
    assert any("C1" in text for text in gnd_items)
    assert [(p["ref"], p["pad"]) for p in result["pending_pads"]] == [("C1", "2")]
    assert result["gate"]["verdict"] == "FAIL"  # signals and the pending pad are still open

    # An independent grade of the revision on disk agrees.
    regraded = gate.verify_board(out_pcb, baseline=real_pour["baseline"], **PARITY)
    assert regraded["verdict"] == result["gate"]["verdict"]
    assert len(regraded["unconnected"]) == len(result["gate"]["unconnected"])


@needs_router
def test_the_original_project_and_rule_baseline_are_byte_identical_after_the_step(real_pour):
    board, out_dir = real_pour["board"], real_pour["out_dir"]
    assert project_hashes(board) == real_pour["before"]
    assert real_pour["result"]["original_unchanged"] is True
    # the new revision carries the original rules, not the router's rewritten ones
    assert sha(out_dir / "simple_led.kicad_pro") == real_pour["before"]["simple_led.kicad_pro"]
    # and nothing but the zone was added to the board
    from kicad_mcp import input_prep

    names_before = [c.name for c in input_prep._parse(board).children]
    names_after = [c.name for c in input_prep._parse(out_dir / NAME).children]
    assert sorted(names_after) == sorted([*names_before, "zone"])


@needs_router
def test_every_grade_ran_before_the_revision_existed_and_the_routers_file_is_not_the_revision(real_pour):
    """The router's writer never becomes the board of record without the gate having graded it."""
    calls = real_pour["existed_at_grading"]
    assert len(calls) >= 3 * 3  # original, candidate, carried revision: three gate runs each (plus a probe)
    assert calls and not any(calls), "the output revision already existed while a grade was still running"
    out_pcb = Path(real_pour["result"]["output_pcb"])
    assert str(out_pcb) not in real_pour["result"]["router"]["args"]  # the router never wrote into it


@needs_router
def test_routing_proceeds_on_the_poured_revision_and_welds_the_pending_pad(real_pour, tmp_path):
    """The claim behind 'pending': the router's own routing step connects what the plane step left."""
    revision = tmp_path / "rev"
    shutil.copytree(real_pour["out_dir"], revision)
    pcb = revision / NAME
    pour_info = krt.board_pour_info(pcb)
    assert pour_info["pour_layers"] == ["B.Cu"]
    # route into a second directory so the revision itself is only read
    out = tmp_path / "routed_copy"
    shutil.copytree(revision, out)
    args = krt.build_route_args(pcb, out / NAME, nets=("*",), pour_info=pour_info)
    run = krt.run_router_script("route.py", args, output_pcb=out / NAME, original_pcb=pcb, timeout=180)
    assert run["exit_code"] == 0
    graded = gate.verify_board(out / NAME, baseline=real_pour["baseline"], **PARITY)
    assert graded["unconnected"] == []
    assert graded["verdict"] == "PASS"


@needs_router
def test_no_result_of_the_step_claims_a_board_is_routed_or_finished(board, tmp_path, isolated_run, real_pour):
    skipped = pour_step.propose_pour_step(board, pours={})
    proposed = pour_step.propose_pour_step(board, pours={"GND": "B.Cu"}, output_dir=tmp_path / "o")
    for result in (skipped, proposed, isolated_run["result"], real_pour["result"]):
        text = json.dumps(result, default=str)
        assert not re.search(r"\b(routed|done|finished)\b", text, re.I), text[:200]


# --- slice 5: a pour that isolates a pad is a failure naming the pad ---------------


def make_fake_router(
    polygon: list[tuple[float, float]] | None = None,
    *,
    zones: list[dict] | None = None,
    edit=None,
    calls: list | None = None,
):
    """A router stand-in that writes a candidate through kicad_tools nodes and exits 0.

    By default ONE GND zone on B.Cu over ``polygon``; ``zones`` is a list of
    ``{"net_id", "net", "layer", "polygon"}`` for other shapes; ``edit(board)`` may
    modify the loaded board first (to stage an in-place change)."""
    if zones is None:
        zones = [{"net_id": 3, "net": "GND", "layer": "B.Cu", "polygon": polygon}]

    def fake(script, args, *, output_pcb, original_pcb, timeout=300, stop_after=None):
        from types import SimpleNamespace

        from kicad_tools.sexp import parse_file, parse_string

        if calls is not None:
            calls.append({"script": script, "args": list(args)})
        # raw tree, so the net table (or its absence, KiCad 10) is carried through untouched
        board = SimpleNamespace(_sexp=parse_file(str(original_pcb)))
        if edit is not None:
            edit(board)
        for index, zone in enumerate(zones):
            pts = " ".join(f"(xy {x} {y})" for x, y in zone["polygon"])
            net = zone.get("net_text") or f'(net {zone["net_id"]}) (net_name "{zone["net"]}")'
            connect = zone.get("connect_text", "(connect_pads yes (clearance 0.2))")
            fill_text = zone.get("fill_text", "(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2))")
            board._sexp.append(
                parse_string(
                    f'(zone {net} (layer "{zone["layer"]}") '
                    f'(uuid "00000000-0000-4000-8000-0000000000f{index + 1}") '
                    f"(hatch edge 0.5) {connect} (min_thickness 0.1) "
                    f"{fill_text} (polygon (pts {pts})))"
                )
            )
        Path(output_pcb).write_text(board._sexp.to_string() + chr(10), encoding="utf-8")
        return {
            "script": script, "exit_code": 0, "elapsed_s": 0.0, "args": list(args),
            "output_pcb": str(output_pcb), "stdout_text": "", "project_restored": True,
            "router_overwrote_project": False,
        }

    return fake


# Covers J1 (x 141) and nothing east of x=143: D1 pad 1 (x 154.73) and C1 are left out.
WEST_STRIP = [(137.0, 78.5), (143.0, 78.5), (143.0, 96.5), (137.0, 96.5)]


@pytest.fixture(scope="module")
def isolated_run(tmp_path_factory, module_baseline):
    root = tmp_path_factory.mktemp("isolated")
    board = build_board(root / "proj")
    before = project_hashes(board)
    out_dir = root / "poured"
    token = _propose(board, out_dir)
    result = pour_step.confirm_pour_step(
        board, token, baseline=module_baseline, router=make_fake_router(WEST_STRIP), **PARITY
    )
    return {"board": board, "before": before, "out_dir": out_dir, "result": result}


def test_a_pour_that_isolates_a_pad_is_rejected_and_names_the_pad(isolated_run):
    result = isolated_run["result"]
    assert result["status"] == "rejected", result
    # D1 pad 1 (THT, x=154.73) is outside the strip: the pour itself fails to reach it.
    named = {(p["ref"], p["pad"]) for p in result["isolated_pads"]}
    assert ("D1", "1") in named
    # C1 pad 2 waits for a via (F.Cu pad, B.Cu pour): it is not the pour's isolation.
    assert ("C1", "2") not in named
    assert "D1" in result["reason"]
    assert not isolated_run["out_dir"].exists()
    assert project_hashes(isolated_run["board"]) == isolated_run["before"]
    assert result["original_unchanged"] is True




# --- slice 6: cheap refusals happen before anything is graded ----------------------


def _spy_router(calls: list):
    def spy(script, args, **kwargs):
        calls.append(script)
        raise AssertionError("the router must not run")

    return spy


def test_a_changed_board_makes_the_token_unusable(board, tmp_path, module_baseline):
    token = _propose(board, tmp_path / "o")
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    changed = PCB.load(board)
    changed._sexp.append(parse_string('(gr_line (start 137 79) (end 140 79) (layer "Edge.Cuts") (width 0.1))'))
    write_layer.save_pcb(changed, board)
    calls: list = []
    result = pour_step.confirm_pour_step(
        board, token, baseline=module_baseline, router=_spy_router(calls), **PARITY
    )
    assert result["status"] == "error"
    assert "changed" in result["reason"]
    assert calls == []
    again = pour_step.confirm_pour_step(board, token, baseline=module_baseline, **PARITY)
    assert again["status"] == "error"


def test_copper_layer_text_sends_the_board_back_to_input_preparation(board, tmp_path, module_baseline):
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    with_text = PCB.load(board)
    with_text._sexp.append(
        parse_string(
            '(gr_text "NOTE" (at 140 80 0) (layer "F.Cu") (uuid "00000000-0000-4000-8000-0000000000e1") '
            "(effects (font (size 1 1) (thickness 0.15))))"
        )
    )
    write_layer.save_pcb(with_text, board)
    token = _propose(board, tmp_path / "o")
    calls: list = []
    result = pour_step.confirm_pour_step(
        board, token, baseline=module_baseline, router=_spy_router(calls), **PARITY
    )
    assert result["status"] == "incomplete"
    assert "input preparation" in result["reason"]
    assert [t["text"] for t in result["copper_texts"]] == ["NOTE"]
    assert calls == []
    assert not (tmp_path / "o").exists()


def test_an_unpinned_baseline_is_refused_before_the_router_runs(board, tmp_path):
    token = _propose(board, tmp_path / "o")
    calls: list = []
    result = pour_step.confirm_pour_step(board, token, router=_spy_router(calls), **PARITY)
    assert result["status"] == "incomplete"
    assert "not pinned" in result["reason"]
    assert calls == []


def test_a_candidate_that_removes_existing_copper_is_rejected(board, tmp_path, module_baseline):
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    with_track = PCB.load(board)
    with_track._sexp.append(
        parse_string(
            '(segment (start 141 86.23) (end 147.5 85.5) (width 0.25) (layer "F.Cu") (net 1) '
            '(uuid "00000000-0000-4000-8000-0000000000d1"))'
        )
    )
    write_layer.save_pcb(with_track, board)
    before = project_hashes(board)

    def dropping_router(script, args, *, output_pcb, original_pcb, timeout=300, stop_after=None):
        board_ = PCB.load(original_pcb)
        for node in [c for c in board_._sexp.children if c.name == "segment"]:
            board_._sexp.remove(node)
        write_layer.save_pcb(board_, output_pcb)
        return {
            "script": script, "exit_code": 0, "elapsed_s": 0.0, "args": args,
            "output_pcb": str(output_pcb), "stdout_text": "",
        }

    token = _propose(board, tmp_path / "o")
    result = pour_step.confirm_pour_step(
        board, token, baseline=module_baseline, router=dropping_router, **PARITY
    )
    assert result["status"] == "rejected"
    assert "removed 1" in result["reason"]
    assert not (tmp_path / "o").exists()
    assert project_hashes(board) == before


def test_a_failing_router_leaves_nothing_behind_and_the_token_usable(board, tmp_path, module_baseline):
    def failing(script, args, **kwargs):
        raise krt.KicadRoutingToolsError("boom")

    before = project_hashes(board)
    token = _propose(board, tmp_path / "o")
    result = pour_step.confirm_pour_step(board, token, baseline=module_baseline, router=failing, **PARITY)
    assert result["status"] == "incomplete"
    assert "boom" in result["reason"]
    assert project_hashes(board) == before
    assert not (tmp_path / "o").exists()
    assert token in pour_step._PENDING


def test_every_flag_the_step_emits_exists_in_the_pinned_plane_script_surface(board, tmp_path, module_baseline):
    seen: list = []

    def recording(script, args, **kwargs):
        seen.append((script, list(args)))
        raise krt.KicadRoutingToolsError("stop after recording")

    token = _propose(board, tmp_path / "o")
    pour_step.confirm_pour_step(board, token, baseline=module_baseline, router=recording, **PARITY)
    (script, args), = seen
    assert script == "route_planes.py"
    snapshot = krt.load_cli_snapshot()["modules"]["route_planes.py"]["flags"]
    flags = [a for a in args if a.startswith("--")]
    assert {"--nets", "--plane-layers", "--strict-sizes", "--no-fix-drc-settings"} <= set(flags)
    assert set(flags) <= set(snapshot), sorted(set(flags) - set(snapshot))
    # one net, one layer, positionally matched
    assert args[args.index("--nets") + 1] == "GND"
    assert args[args.index("--plane-layers") + 1] == "B.Cu"


# --- review round: false accept, fill evidence, KiCad 10 nets, fake-router matrix ------

FULL = [(137.0, 78.5), (160.0, 78.5), (160.0, 97.0), (137.0, 97.0)]
_VIOLATION = {
    "type": "clearance",
    "severity": "error",
    "description": "Clearance violation",
    "items": [
        {"description": "Track [LED_ANODE] on F.Cu", "pos": {"x": 1.0, "y": 2.0}},
        {"description": "Pad 1 [VCC] of R1 on F.Cu", "pos": {"x": 3.0, "y": 4.0}},
    ],
}


def report(violations=()):
    return {
        "violations": list(violations),
        "schematic_parity": [],
        "unconnected_items": [],
        "ignored_checks": [],
    }


def fake_drc(*, always=(), when_zone=()):
    """kicad-cli stand-in: an empty report, plus ``always`` violations, plus ``when_zone``
    violations once a board carries a zone. Unconnected items are never listed (the case a
    net with one remaining pad produces in real KiCad)."""

    def drc(pcb, **kwargs):
        text = Path(pcb).read_text(encoding="utf-8")
        return report([*always, *(when_zone if "(zone" in text else ())])

    return drc


def fake_fill(directory: Path, *, shrink: float = 0.0, holes=()):
    """Refill stand-in: a copy whose zones carry one filled polygon equal to the outline
    (shrunk towards its centre by ``shrink``, for a thin fill)."""

    def fill(pcb: Path) -> Path:
        from kicad_tools.schema import PCB
        from kicad_tools.sexp import parse_string

        board = PCB.load(pcb)
        for zone in board._sexp.find_all("zone"):
            pts = [(float(xy.values[0]), float(xy.values[1])) for xy in zone.find("polygon").find("pts").find_all("xy")]
            cx = sum(x for x, _ in pts) / len(pts)
            cy = sum(y for _, y in pts) / len(pts)
            pts = [(cx + (x - cx) * (1 - shrink), cy + (y - cy) * (1 - shrink)) for x, y in pts]
            layer = zone.find("layer").values[0]
            for (hx0, hy0, hx1, hy1) in holes:  # keyhole: bridge to a hole and back
                pts = pts + [pts[0], (hx0, hy0), (hx1, hy0), (hx1, hy1), (hx0, hy1), (hx0, hy0), pts[0]]
            body = " ".join(f"(xy {x} {y})" for x, y in pts)
            zone.append(parse_string(f'(filled_polygon (layer "{layer}") (pts {body}))'))
        out = directory / "fake_filled.kicad_pcb"
        write_layer.save_pcb(board, out)
        return out

    return fill


def confirm_fake(board, tmp_path, *, router, drc=None, fill=None, pours=None, out="o"):
    drc = drc or fake_drc()
    baseline = gate.snapshot_rules(board, drc=drc)
    token = _propose(board, tmp_path / out, pours)
    return pour_step.confirm_pour_step(
        board, token, baseline=baseline, router=router, drc=drc,
        fill=fill or fake_fill(tmp_path), connectivity_check=False, **PARITY,
    )


def detach_pads(pcb: Path, wanted: set[tuple[str, str]]) -> None:
    """Fixture edit through kicad_tools nodes: the given (ref, pad) lose their net."""
    from kicad_tools.schema import PCB

    board = PCB.load(pcb)
    for fp in board._sexp.find_all("footprint"):
        ref = pour_step._footprint_reference(fp)
        for pad in fp.find_all("pad"):
            if (ref, str(pad.values[0])) in wanted:
                net = pad.find("net")
                net.set_value(0, 0)
                net.set_value(1, "")
    write_layer.save_pcb(board, pcb)


def test_blocker_a_single_gnd_pad_and_a_zone_that_does_not_touch_it_is_rejected(board, tmp_path):
    """The reviewer's repro: kicad-cli lists no item when one pad remains next to a far zone."""
    detach_pads(board, {("J1", "2")})  # GND pads left: D1 pad 1 (THT) and C1 pad 2 (waiting)
    tiny = [(137.0, 78.5), (139.0, 78.5), (139.0, 80.0), (137.0, 80.0)]
    result = confirm_fake(board, tmp_path, router=make_fake_router(tiny))
    assert result["status"] == "rejected", result
    assert ("D1", "1") in {(p["ref"], p["pad"]) for p in result["isolated_pads"]}
    assert not (tmp_path / "o").exists()


def test_blocker_a_plane_net_whose_only_pad_waits_for_a_via_needs_the_zone_to_cover_it(board, tmp_path):
    detach_pads(board, {("J1", "2"), ("D1", "1")})  # the only GND pad left is C1 pad 2 (SMD, F.Cu)
    far = [(137.0, 78.5), (139.0, 78.5), (139.0, 80.0), (137.0, 80.0)]
    result = confirm_fake(board, tmp_path, router=make_fake_router(far))
    assert result["status"] == "rejected", result
    assert [(p["ref"], p["pad"]) for p in result["unreachable_waiting_pads"]] == [("C1", "2")]
    # the same board with a zone that covers the pad is accepted
    again = confirm_fake(board, tmp_path / "again", router=make_fake_router(FULL), out="o2")
    assert again["status"] == "poured", again


def test_a_tiny_zone_is_not_a_filled_plane(board, tmp_path):
    """Real kicad-cli refill: a 0.05 mm square cannot hold 0.1 mm minimum-thickness copper."""
    speck = [(150.9, 91.9), (150.95, 91.9), (150.95, 91.95), (150.9, 91.95)]
    drc = fake_drc()
    baseline = gate.snapshot_rules(board, drc=drc)
    token = _propose(board, tmp_path / "o")
    result = pour_step.confirm_pour_step(
        board, token, baseline=baseline, router=make_fake_router(speck), drc=drc, **PARITY
    )
    assert result["status"] == "rejected", result
    assert "fill" in result["reason"]
    assert not (tmp_path / "o").exists()


def test_a_thin_fill_below_the_fraction_is_rejected(board, tmp_path):
    result = confirm_fake(
        board, tmp_path, router=make_fake_router(FULL), fill=fake_fill(tmp_path, shrink=0.8)
    )
    assert result["status"] == "rejected"
    assert "covers" in result["reason"]


def test_a_refill_that_cannot_run_is_incomplete_not_poured(board, tmp_path):
    def broken(pcb):
        raise kicad_cli_error("kicad-cli exited 3")

    result = confirm_fake(board, tmp_path, router=make_fake_router(FULL), fill=broken)
    assert result["status"] == "incomplete"
    assert "fill could not be verified" in result["reason"]
    assert not (tmp_path / "o").exists()


def kicad_cli_error(message):
    from kicad_mcp.kicad_cli import KicadCliError

    return KicadCliError(message)


def test_a_poured_result_reports_measured_fill_and_says_the_file_is_unfilled(board, tmp_path):
    result = confirm_fake(board, tmp_path, router=make_fake_router(FULL))
    assert result["status"] == "poured", result
    (zone,) = result["zones"]
    assert zone["filled_in_file"] is False
    assert zone["fill_fraction"] == pytest.approx(1.0)
    assert "OUTLINE only" in result["fill_note"]


# --- KiCad 10 net format ---------------------------------------------------------------


def to_kicad10_nets(pcb: Path) -> None:
    """Rewrite a board's net references to the KiCad 10 shape (net "NAME"), through nodes."""
    from kicad_tools.schema import PCB

    board = PCB.load(pcb)
    table = {int(n.values[0]): str(n.values[1]) for n in board._sexp.children if n.name == "net" and len(n.values) > 1}
    for node in [n for n in board._sexp.children if n.name == "net"]:
        board._sexp.remove(node)
    for fp in board._sexp.find_all("footprint"):
        for pad in fp.find_all("pad"):
            net = pad.find("net")
            if net is not None:
                name = str(net.values[1]) if len(net.values) > 1 else table.get(int(net.values[0]), "")
                net.children[:] = []
                net.set_value(0, name)
    write_layer.save_pcb(board, pcb)


def test_kicad10_nets_are_read_from_the_pic_programmer_copy(kicad_demo, tmp_path):
    pcb = kicad_demo("pic_programmer") / "pic_programmer.kicad_pcb"
    names = pour_step.board_net_names(pcb)
    assert {"GND", "VCC"} <= names
    assert pour_step._net_table(pour_step.input_prep._parse(pcb)) == {}  # no table in KiCad 10
    already = pour_step.propose_pour_step(pcb, pours={"GND": "B.Cu"}, output_dir=tmp_path / "o")
    assert already["status"] == "skipped"
    assert "GND on B.Cu" in already["reason"]
    unknown = pour_step.propose_pour_step(pcb, pours={"NOPE": "F.Cu"}, output_dir=tmp_path / "o")
    assert unknown["status"] == "error" and "NOPE" in unknown["reason"]
    clash = pour_step.propose_pour_step(pcb, pours={"VCC": "B.Cu"}, output_dir=tmp_path / "o")
    assert clash["status"] == "error" and "already holds a zone for net GND" in clash["reason"]


def test_kicad10_format_board_poured_and_an_unknown_net_zone_is_rejected(board, tmp_path):
    to_kicad10_nets(board)
    assert pour_step.board_net_names(board) == {"VCC", "LED_ANODE", "GND"}
    ok = confirm_fake(
        board, tmp_path,
        router=make_fake_router(zones=[{"net": "GND", "layer": "B.Cu", "polygon": FULL, "net_text": '(net "GND")'}]),
    )
    assert ok["status"] == "poured", ok
    assert [z["net"] for z in ok["zones"]] == ["GND"]
    assert ok["net_check"]["net_table"] is False
    assert "cannot be cross-checked" in ok["net_check"]["note"]
    bad = confirm_fake(
        board, tmp_path / "bad", out="o2",
        router=make_fake_router(zones=[{"net": "X", "layer": "B.Cu", "polygon": FULL, "net_text": '(net "NOSUCH")'}]),
    )
    assert bad["status"] == "rejected"
    assert "not on the board" in bad["reason"]


def test_a_carried_zone_whose_net_id_disagrees_with_the_net_table_is_rejected(board, tmp_path):
    """The KRT net-id collision gotcha: id 2 is LED_ANODE, the node says GND."""
    collided = make_fake_router(zones=[{"net_id": 2, "net": "GND", "layer": "B.Cu", "polygon": FULL}])
    result = confirm_fake(board, tmp_path, router=collided)
    assert result["status"] == "rejected"
    assert "net id collision" in result["reason"]


def test_pad_world_positions_follow_kicads_rotation_convention(tmp_path):
    """C1 rotated 90 degrees: pad 1 local (-0.95, 0) lands at (150, 92.95) in the file frame;
    kicad-cli's own report of the unrouted pads must agree with the computed positions."""
    from kicad_tools.schema import PCB

    pcb = build_board(tmp_path / "proj")
    board = PCB.load(pcb)
    for fp in board._sexp.find_all("footprint"):
        if pour_step._footprint_reference(fp) == "C1":
            fp.find("at").set_value(2, 90)
    write_layer.save_pcb(board, pcb)
    root = pour_step.input_prep._parse(pcb)
    pads = {(p["ref"], p["pad"]): p["at"] for p in pour_step._pad_records(root, ["F.Cu", "B.Cu"])}
    assert pads[("C1", "1")] == pytest.approx((150.0, 92.95))
    assert pads[("C1", "2")] == pytest.approx((150.0, 91.05))
    reported = {}
    for entry in run_drc(pcb, severity_all=True)["unconnected_items"]:
        for item in entry["items"]:
            reported[item["description"]] = (item["pos"]["x"], item["pos"]["y"])
    c1 = [pos for text, pos in reported.items() if " C1" in text]
    assert (150.0, 92.95) in [tuple(round(v, 3) for v in p) for p in c1]
    assert (150.0, 91.05) in [tuple(round(v, 3) for v in p) for p in c1]


# --- the fake-router matrix --------------------------------------------------------------


def test_a_pour_that_introduces_a_violation_is_rejected(board, tmp_path):
    drc = fake_drc(when_zone=[_VIOLATION])
    result = confirm_fake(board, tmp_path, router=make_fake_router(FULL), drc=drc)
    assert result["status"] == "rejected"
    assert "introduced an error-level violation" in result["reason"]
    assert not (tmp_path / "o").exists()


def test_a_violation_the_original_already_had_is_tolerated(board, tmp_path):
    drc = fake_drc(always=[_VIOLATION])
    result = confirm_fake(board, tmp_path, router=make_fake_router(FULL), drc=drc)
    assert result["status"] == "poured", result
    assert len(result["gate"]["violations"]) == 1  # still listed in full by the gate result


def test_an_expired_token_is_refused(board, tmp_path, monkeypatch):
    import time as time_module

    token = _propose(board, tmp_path / "o")
    monkeypatch.setattr(pour_step, "_clock", lambda: time_module.time() + 2 * 3600)
    result = pour_step.confirm_pour_step(
        board, token, baseline={}, router=_spy_router([]), **PARITY
    )
    assert result["status"] == "error"
    assert "expired" in result["reason"]
    assert token not in pour_step._PENDING


def test_a_token_proposed_for_another_board_is_refused(board, tmp_path):
    other = build_board(tmp_path / "other")
    token = _propose(board, tmp_path / "o")
    result = pour_step.confirm_pour_step(
        other, token, baseline={}, router=_spy_router([]), **PARITY
    )
    assert result["status"] == "error"
    assert "does not match" in result["reason"]
    assert token in pour_step._PENDING


def test_keyboard_interrupt_leaves_nothing_behind(board, tmp_path, module_baseline):
    def interrupted(script, args, **kwargs):
        raise KeyboardInterrupt

    before = project_hashes(board)
    token = _propose(board, tmp_path / "o")
    with pytest.raises(KeyboardInterrupt):
        pour_step.confirm_pour_step(board, token, baseline=module_baseline, router=interrupted, **PARITY)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]
    assert project_hashes(board) == before
    assert token in pour_step._PENDING


def test_a_same_layer_pour_covers_the_pad_that_sits_on_the_pour_layer(board, tmp_path):
    f_cu = [{"net_id": 3, "net": "GND", "layer": "F.Cu", "polygon": FULL}]
    result = confirm_fake(
        board, tmp_path, router=make_fake_router(zones=f_cu), pours={"GND": "F.Cu"}
    )
    assert result["status"] == "poured", result
    assert result["pending_pads"] == []  # C1 pad 2 is on F.Cu: nothing waits for a via


def test_a_same_layer_pour_that_misses_a_pad_on_that_layer_is_rejected_naming_it(board, tmp_path):
    f_cu = [{"net_id": 3, "net": "GND", "layer": "F.Cu",
             "polygon": [(137.0, 78.5), (158.0, 78.5), (158.0, 88.5), (137.0, 88.5)]}]
    result = confirm_fake(
        board, tmp_path, router=make_fake_router(zones=f_cu), pours={"GND": "F.Cu"}
    )
    assert result["status"] == "rejected", result
    assert {(p["ref"], p["pad"]) for p in result["isolated_pads"]} == {("C1", "2")}


def test_two_nets_are_poured_together_one_per_layer(board, tmp_path):
    zones = [
        {"net_id": 3, "net": "GND", "layer": "B.Cu", "polygon": FULL},
        {"net_id": 1, "net": "VCC", "layer": "F.Cu", "polygon": FULL},
    ]
    result = confirm_fake(
        board, tmp_path, router=make_fake_router(zones=zones), pours={"GND": "B.Cu", "VCC": "F.Cu"}
    )
    assert result["status"] == "poured", result
    assert {(z["net"], z["layer"]) for z in result["zones"]} == {("GND", "B.Cu"), ("VCC", "F.Cu")}
    assert result["carried"]["zone"] == 2
    zones_on_disk = pour_step.read_zone_polygons(Path(result["output_pcb"]))
    assert {(z["net"], z["layer"]) for z in zones_on_disk} == {("GND", "B.Cu"), ("VCC", "F.Cu")}


def test_an_existing_pad_modified_in_place_is_a_rejection(board, tmp_path):
    def widen(board_):
        for fp in board_._sexp.find_all("footprint"):
            if pour_step._footprint_reference(fp) == "R1":
                fp.find_all("pad")[0].find("size").set_value(0, 1.4)

    result = confirm_fake(board, tmp_path, router=make_fake_router(FULL, edit=widen))
    assert result["status"] == "rejected"
    assert "changed board content" in result["reason"]


def test_an_existing_track_modified_in_place_is_a_rejection(board, tmp_path):
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    with_track = PCB.load(board)
    with_track._sexp.append(
        parse_string(
            '(segment (start 141 86.23) (end 147.5 85.5) (width 0.25) (layer "F.Cu") (net 1) '
            '(uuid "00000000-0000-4000-8000-0000000000d2"))'
        )
    )
    write_layer.save_pcb(with_track, board)

    def narrow(board_):
        for seg in board_._sexp.find_all("segment"):
            seg.find("width").set_value(0, 0.1)

    result = confirm_fake(board, tmp_path, router=make_fake_router(FULL, edit=narrow))
    assert result["status"] == "rejected"
    assert "modified 1 existing" in result["reason"]


def test_two_pads_sharing_a_number_do_not_merge(tmp_path):
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    pcb = build_board(tmp_path / "proj")
    board = PCB.load(pcb)
    fp = [f for f in board._sexp.find_all("footprint") if pour_step._footprint_reference(f) == "C1"][0]
    fp.append(
        parse_string(
            '(pad "2" smd rect (at 0 2) (size 1 1) (layers "B.Cu") (net 3 "GND"))'
        )
    )
    write_layer.save_pcb(board, pcb)
    root = pour_step.input_prep._parse(pcb)
    pads = pour_step._pad_records(root, ["F.Cu", "B.Cu"])
    c1 = [p for p in pads if p["ref"] == "C1" and p["pad"] == "2"]
    assert len(c1) == 2 and c1[0]["layers"] != c1[1]["layers"]
    # one of the two sits on B.Cu, so the number does not "wait" for a B.Cu pour (fail closed)
    assert pour_step._waiting_pads(pads, {"GND": "B.Cu"}) == []


def test_zone_layers_are_all_read_and_an_unreadable_candidate_is_a_status(board, tmp_path, module_baseline):
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    two = PCB.load(board)
    two._sexp.append(
        parse_string(
            '(zone (net 1) (net_name "VCC") (layers "F.Cu" "B.Cu") (uuid "00000000-0000-4000-8000-0000000000b1") '
            "(polygon (pts (xy 137 78) (xy 139 78) (xy 139 80))))"
        )
    )
    write_layer.save_pcb(two, board)
    (zone,) = pour_step.read_zone_polygons(board)
    assert zone["layers"] == ["F.Cu", "B.Cu"]
    # a different net's zone already on the requested layer is refused at propose
    refused = pour_step.propose_pour_step(board, pours={"GND": "B.Cu"}, output_dir=tmp_path / "o")
    assert refused["status"] == "error"
    assert "already holds a zone for net VCC" in refused["reason"]


def test_a_garbled_candidate_is_a_status_not_an_exception(board, tmp_path, module_baseline):
    def garbage(script, args, *, output_pcb, original_pcb, timeout=300, stop_after=None):
        Path(output_pcb).write_text("(((", encoding="utf-8")
        return {"script": script, "exit_code": 0, "args": args, "output_pcb": str(output_pcb), "stdout_text": ""}

    token = _propose(board, tmp_path / "o")
    result = pour_step.confirm_pour_step(board, token, baseline=module_baseline, router=garbage, **PARITY)
    assert result["status"] == "incomplete"
    assert "could not be parsed" in result["reason"]


# --- delta review: connectivity decided by kicad-cli on the refilled board (real refill) ----


def real_refill_confirm(board, tmp_path, zone, pours=None, out="o"):
    """Fake router and fake gating report, but the REAL kicad-cli refill and connectivity."""
    drc = fake_drc()
    baseline = gate.snapshot_rules(board, drc=drc)
    token = _propose(board, tmp_path / out, pours or {"GND": "F.Cu"})
    return pour_step.confirm_pour_step(
        board, token, baseline=baseline, router=make_fake_router(zones=[zone]), drc=drc, **PARITY
    )


def named(result):
    return {(p["ref"], p["pad"]) for p in result.get("isolated_pads", [])}


def test_real_refill_a_zone_edge_in_the_air_beside_a_pad_is_not_a_connection(board, tmp_path):
    """E2: C1 pad 2 (F.Cu) with the zone edge 0.65 mm from the pad edge: inside any 'reach'
    heuristic, but kicad-cli lists the pad unconnected."""
    detach_pads(board, {("J1", "2"), ("D1", "1")})
    zone = {"net_id": 3, "net": "GND", "layer": "F.Cu",
            "polygon": [(152.1, 78.5), (160.0, 78.5), (160.0, 97.0), (152.1, 97.0)]}
    result = real_refill_confirm(board, tmp_path, zone)
    assert result["status"] == "rejected", result
    assert ("C1", "2") in named(result)
    assert not (tmp_path / "o").exists()


def test_real_refill_connect_pads_no_leaves_a_through_hole_pad_unconnected(board, tmp_path):
    detach_pads(board, {("J1", "2"), ("C1", "2")})  # the only GND pad left: D1 pad 1 (THT)
    zone = {"net_id": 3, "net": "GND", "layer": "B.Cu", "polygon": FULL,
            "connect_text": "(connect_pads no (clearance 0.2))"}
    result = real_refill_confirm(board, tmp_path, zone, {"GND": "B.Cu"})
    assert result["status"] == "rejected", result
    assert ("D1", "1") in named(result)
    assert "connect_pads" in result["reason"]


def test_real_refill_thermal_relief_without_spokes_leaves_the_pad_unconnected(board, tmp_path):
    detach_pads(board, {("J1", "2"), ("C1", "2")})
    zone = {"net_id": 3, "net": "GND", "layer": "B.Cu", "polygon": FULL,
            "connect_text": "(connect_pads (clearance 0.2))",
            "fill_text": "(fill yes (thermal_gap 0.2) (thermal_bridge_width 0))"}
    result = real_refill_confirm(board, tmp_path, zone, {"GND": "B.Cu"})
    assert result["status"] == "rejected", result
    assert ("D1", "1") in named(result)


def test_real_refill_an_elongated_pad_does_not_borrow_reach_from_its_long_axis(board, tmp_path):
    from kicad_tools.schema import PCB

    wide = PCB.load(board)
    for fp in wide._sexp.find_all("footprint"):
        if pour_step._footprint_reference(fp) == "C1":
            for pad in fp.find_all("pad"):
                if str(pad.values[0]) == "2":
                    pad.find("size").set_value(0, 3.0)
                    pad.find("size").set_value(1, 1.0)
    write_layer.save_pcb(wide, board)
    detach_pads(board, {("J1", "2"), ("D1", "1")})
    zone = {"net_id": 3, "net": "GND", "layer": "F.Cu",
            "polygon": [(152.9, 78.5), (160.0, 78.5), (160.0, 97.0), (152.9, 97.0)]}
    result = real_refill_confirm(board, tmp_path, zone)
    assert result["status"] == "rejected", result
    assert ("C1", "2") in named(result)


def test_real_refill_an_island_fenced_off_by_another_nets_track_is_not_connected(board, tmp_path):
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    fenced = PCB.load(board)
    corners = [(149.9, 90.0), (152.6, 90.0), (152.6, 94.0), (149.9, 94.0)]
    for i, (a, b) in enumerate(zip(corners, corners[1:] + corners[:1])):
        fenced._sexp.append(
            parse_string(
                f'(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width 0.3) (layer "F.Cu") (net 1) '
                f'(uuid "00000000-0000-4000-8000-00000000a0{i}"))'
            )
        )
    write_layer.save_pcb(fenced, board)
    detach_pads(board, {("J1", "2")})  # D1 pad 1 stays: it keeps the outer fill alive
    zone = {"net_id": 3, "net": "GND", "layer": "F.Cu", "polygon": FULL}
    result = real_refill_confirm(board, tmp_path, zone)
    assert result["status"] == "rejected", result
    assert ("C1", "2") in named(result)


def test_real_refill_a_same_layer_pour_that_does_connect_is_accepted(board, tmp_path):
    zone = {"net_id": 3, "net": "GND", "layer": "F.Cu", "polygon": FULL}
    result = real_refill_confirm(board, tmp_path, zone)
    assert result["status"] == "poured", result
    assert result["connectivity"]["checked"] is True
    assert result["connectivity"]["runs"] >= 3  # control, position cross-check, at least one group


# --- delta review: batching, cap, parameters, guards -----------------------------------

_TEN_SMD = "(footprint \"Test:TEN\" (layer \"F.Cu\") (uuid \"00000000-0000-4000-8000-0000000000ee\") (at 0 0 0) "     "(fp_text reference \"P1\" (at 0 0) (layer \"F.SilkS\") (uuid \"00000000-0000-4000-8000-0000000000ef\") "     "(effects (font (size 1 1) (thickness 0.15)))) " + " ".join(
        f'(pad "{n}" smd rect (at 150.95 {79.0 + 1.2 * (n - 1):.2f}) (size 1 1) '
        '(layers "F.Cu" "F.Paste" "F.Mask") (net 3 "GND"))' for n in range(1, 11)
    ) + ")"


def with_ten_pads(board: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    b = PCB.load(board)
    b._sexp.append(parse_string(_TEN_SMD))
    write_layer.save_pcb(b, board)


def test_a_healthy_ten_pad_net_costs_five_connectivity_runs(board, tmp_path):
    with_ten_pads(board)
    detach_pads(board, {("J1", "2")})
    zone = {"net_id": 3, "net": "GND", "layer": "F.Cu", "polygon": FULL}
    result = real_refill_confirm(board, tmp_path, zone)
    assert result["status"] == "poured", result
    # position cross-check once; the accepting control and the ONE group of all pads are asked twice
    assert result["connectivity"]["runs"] == 5


def test_ten_isolated_pads_are_all_named_within_the_run_cap(board, tmp_path):
    with_ten_pads(board)
    detach_pads(board, {("J1", "2")})  # D1 pad 1 stays inside the fill and keeps it alive
    # the zone edge sits 0.45 mm from the pad edges: inside any reach heuristic, not a connection
    zone = {"net_id": 3, "net": "GND", "layer": "F.Cu",
            "polygon": [(151.9, 78.5), (160.0, 78.5), (160.0, 97.0), (151.9, 97.0)]}
    result = real_refill_confirm(board, tmp_path, zone)
    assert result["status"] == "rejected", result
    assert {(p["ref"], p["pad"]) for p in result["isolated_pads"]} >= {("P1", str(n)) for n in range(1, 11)}
    assert result["connectivity"]["runs"] <= pour_step.MAX_CONNECTIVITY_RUNS


def test_over_the_cap_an_unproven_pad_is_never_accepted(board, tmp_path, monkeypatch):
    with_ten_pads(board)
    detach_pads(board, {("J1", "2")})
    monkeypatch.setattr(pour_step, "MAX_CONNECTIVITY_RUNS", 4)  # cross-check + control + 2 groups
    zone = {"net_id": 3, "net": "GND", "layer": "F.Cu",
            "polygon": [(151.9, 78.5), (160.0, 78.5), (160.0, 97.0), (151.9, 97.0)]}
    result = real_refill_confirm(board, tmp_path, zone)
    assert result["status"] in {"incomplete", "rejected"}, result
    assert not (tmp_path / "o").exists()


def test_a_waiting_pad_over_a_hole_in_the_fill_is_unreachable(board, tmp_path):
    hole = fake_fill(tmp_path, holes=[(150.0, 91.0, 152.0, 93.0)])
    result = confirm_fake(board, tmp_path, router=make_fake_router(FULL), fill=hole)
    assert result["status"] == "rejected", result
    assert [(p["ref"], p["pad"]) for p in result["unreachable_waiting_pads"]] == [("C1", "2")]
    assert "no fill copper under the pad" in result["reason"]


def test_a_multi_layer_zone_is_refused_with_a_clear_reason(board, tmp_path):
    both = [{"net_id": 3, "net": "GND", "layer": "B.Cu", "polygon": FULL,
             "net_text": '(net 3) (net_name "GND") (layers "F.Cu" "B.Cu")'}]
    result = confirm_fake(board, tmp_path, router=make_fake_router(zones=both))
    assert result["status"] == "rejected"
    assert "only single-layer pours" in result["reason"]


def test_min_fill_fraction_is_a_parameter(board, tmp_path):
    drc = fake_drc()
    baseline = gate.snapshot_rules(board, drc=drc)
    token = _propose(board, tmp_path / "o")
    result = pour_step.confirm_pour_step(
        board, token, baseline=baseline, router=make_fake_router(FULL), drc=drc,
        fill=fake_fill(tmp_path, shrink=0.5), connectivity_check=False,
        min_fill_fraction=0.9, **PARITY,
    )
    assert result["status"] == "rejected"
    assert "below 90%" in result["reason"]


_FORBIDDEN_SEAMS = {"drc", "router", "fill", "connectivity_check"}


def _seam_offenders(source: str, name: str = "x.py") -> list[str]:
    import ast

    found = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        callee = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if callee not in {"confirm_pour_step", "propose_pour_step"}:
            continue
        for kw in node.keywords:
            if kw.arg is None or kw.arg in _FORBIDDEN_SEAMS:
                found.append(f"{name}:{node.lineno} passes {kw.arg or '**kwargs'}")
    return found


def test_the_seam_guard_recognises_a_forwarding_registration():
    assert _seam_offenders("pour_step.confirm_pour_step(p, t, drc=drc)")
    assert _seam_offenders("confirm_pour_step(p, t, **kwargs)")
    assert _seam_offenders("ps.confirm_pour_step(p, t, parity=a, baseline=b, timeout=3)") == []


def test_no_registered_tool_forwards_the_test_seams_of_the_pour_step():
    """Activates the day a tool wrapper calls the step: it must not pass drc, router,
    fill or connectivity_check through, nor any **kwargs."""
    src_root = Path(pour_step.__file__).parent
    offenders = []
    for path in src_root.rglob("*.py"):
        if path.name != "pour_step.py":
            offenders += _seam_offenders(path.read_text(encoding="utf-8"), path.name)
    assert offenders == []


# --- round 3: fills that survive without a pad, other-layer copper, the proof statement ----

_NEVER_REMOVE = "(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2) (island_removal_mode 1))"


def add_nodes(pcb: Path, texts) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    b = PCB.load(pcb)
    for text in texts:
        b._sexp.append(parse_string(text))
    write_layer.save_pcb(b, pcb)


def _smd_footprint(ref: str, pads: list[tuple[str, float, float]], n: int, layer: str = "F.Cu") -> str:
    body = " ".join(
        f'(pad "{num}" smd rect (at {x} {y}) (size 0.5 0.5) (layers "{layer}" "F.Paste" "F.Mask") (net 3 "GND"))'
        for num, x, y in pads
    )
    return (
        f'(footprint "Test:X" (layer "F.Cu") (uuid "00000000-0000-4000-8000-0000000d{n:04d}") (at 0 0 0) '
        f'(fp_text reference "{ref}" (at 0 0) (layer "F.SilkS") (uuid "00000000-0000-4000-8000-0000000e{n:04d}") '
        f"(effects (font (size 1 1) (thickness 0.15)))) {body})"
    )


def test_a_pad_with_a_stub_nearer_than_itself_is_not_connected_by_its_neighbour_link(board, tmp_path):
    """A1: pad P1 plus a GND stub ending 0.15 mm from the zone edge. The control (pad detached)
    lists the same stub-to-zone link by description, so subtracting the control hides the
    isolation. The fill survives without a pad (island_removal_mode 1)."""
    detach_pads(board, {("J1", "2"), ("D1", "1"), ("C1", "2")})
    add_nodes(
        board,
        [
            _smd_footprint("P1", [("1", 151.4, 85.0)], 1),
            '(segment (start 151.4 85) (end 151.95 85) (width 0.25) (layer "F.Cu") (net 3) '
            '(uuid "00000000-0000-4000-8000-00000000b001"))',
        ],
    )
    zone = {"net_id": 3, "net": "GND", "layer": "F.Cu", "fill_text": _NEVER_REMOVE,
            "polygon": [(152.1, 78.5), (160.0, 78.5), (160.0, 97.0), (152.1, 97.0)]}
    result = real_refill_confirm(board, tmp_path, zone)
    assert result["status"] == "rejected", result
    assert ("P1", "1") in named(result)


def test_other_layer_plane_copper_and_a_through_anchor_do_not_credit_the_pour(board, tmp_path):
    """A2: an existing B.Cu GND plane, a new F.Cu GND pour, copperpour keepouts around the two
    THT pads. Their only real connection is the B.Cu plane; the pour must not be credited."""
    pads = {(p["ref"], p["pad"]): p for p in pour_step._pad_records(pour_step.input_prep._parse(board), ["F.Cu", "B.Cu"])}
    detach_pads(board, {("C1", "2")})
    keepouts = []
    for i, key in enumerate([("J1", "2"), ("D1", "1")]):
        x, y = pads[key]["at"]
        h = max(pads[key]["size"]) / 2 + 0.4
        keepouts.append(
            f'(zone (net 0) (net_name "") (layer "F.Cu") (uuid "00000000-0000-4000-8000-00000000c{i:03d}") '
            "(hatch edge 0.5) (connect_pads (clearance 0)) (min_thickness 0.1) "
            "(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour not_allowed) "
            "(footprints allowed)) (fill (thermal_gap 0.5) (thermal_bridge_width 0.5)) "
            f"(polygon (pts (xy {x - h} {y - h}) (xy {x + h} {y - h}) (xy {x + h} {y + h}) (xy {x - h} {y + h}))))"
        )
    add_nodes(
        board,
        [
            '(zone (net 3) (net_name "GND") (layer "B.Cu") (uuid "00000000-0000-4000-8000-0000000000b9") '
            "(hatch edge 0.5) (connect_pads yes (clearance 0.2)) (min_thickness 0.1) "
            "(fill yes (thermal_gap 0.2) (thermal_bridge_width 0.2)) "
            "(polygon (pts (xy 137 78.5) (xy 160 78.5) (xy 160 97) (xy 137 97))))",
            *keepouts,
        ],
    )
    zone = {"net_id": 3, "net": "GND", "layer": "F.Cu", "fill_text": _NEVER_REMOVE,
            "polygon": [(137.0, 78.5), (160.0, 78.5), (160.0, 97.0), (137.0, 97.0)]}
    result = real_refill_confirm(board, tmp_path, zone)
    assert result["status"] == "rejected", result
    assert {("J1", "2"), ("D1", "1")} <= named(result)


def test_a_healthy_net_over_the_run_cap_is_incomplete_not_accepted(board, tmp_path, monkeypatch):
    with_ten_pads(board)
    monkeypatch.setattr(pour_step, "MAX_CONNECTIVITY_RUNS", 2)  # the cross-check and control use it up
    result = real_refill_confirm(board, tmp_path, {"net_id": 3, "net": "GND", "layer": "F.Cu", "polygon": FULL})
    assert result["status"] == "incomplete", result
    assert "run cap" in result["reason"]
    assert not (tmp_path / "o").exists()


def test_a_connectivity_report_without_unconnected_items_is_incomplete(board, tmp_path, monkeypatch):
    monkeypatch.setattr(pour_step.kicad_cli, "run_drc", lambda *a, **k: {})
    result = real_refill_confirm(board, tmp_path, {"net_id": 3, "net": "GND", "layer": "F.Cu", "polygon": FULL})
    assert result["status"] == "incomplete", result
    assert "unconnected_items" in result["reason"]


def test_every_plane_net_pad_carries_its_own_connectivity_verdict(board, tmp_path):
    result = real_refill_confirm(board, tmp_path, {"net_id": 3, "net": "GND", "layer": "F.Cu", "polygon": FULL})
    assert result["status"] == "poured", result
    verdicts = {(p["ref"], p["pad"]): p for p in result["pads"]}
    assert set(verdicts) == {("J1", "2"), ("D1", "1"), ("C1", "2")}
    assert all(p["connectivity"] == "proven" for p in verdicts.values())
    assert all(p["reason"] for p in verdicts.values())
    assert "verify_board" in result["proves"]
    assert result["revision_differences"]["other_content_identical"] is True


def test_the_anchor_uuid_is_deterministic():
    assert pour_step._anchor_uuid("GND") == pour_step._anchor_uuid("GND")
    assert pour_step._anchor_uuid("GND") != pour_step._anchor_uuid("VCC")


# --- the reload self-check fires on a corrupted write (build ticket 31) ----------------------
# The real save of the carried revision runs, then ONE value in the written temporary revision is
# changed through kicad_tools' Document API (tests/_self_check_helpers.py). The self-check must
# refuse it by name before any grade or fill runs, leave nothing behind and keep the token; with
# the corruption disarmed the same token must pour, so the failure came from the corruption alone.

from tests._self_check_helpers import (  # noqa: E402
    corrupt_pcb_saves,
    footprint_reference,
    in_revision_tmp,
    polygon_points,
    top_nodes,
)

_CARRIED_ZONE_UUID = "00000000-0000-4000-8000-0000000000f1"  # make_fake_router's first zone


def _carried_zone(root):
    return next(z for z in top_nodes(root, "zone") if z.find("uuid").values[0] == _CARRIED_ZONE_UUID)


def _drop_c1(root):
    root.remove(next(f for f in top_nodes(root, "footprint") if footprint_reference(f) == "C1"))


def _reissue_the_zone_uuid(root):
    _carried_zone(root).find("uuid").set_value(0, "00000000-0000-4000-8000-0000000000ff")


def _move_a_zone_corner(root):
    xy = polygon_points(_carried_zone(root))[1]
    xy.set_value(0, float(xy.values[0]) - 5.0)


def _nudge_a_zone_corner(root):
    """2 um: just over the 1 um the check allows for kicad_tools' 6-significant-digit numbers."""
    xy = polygon_points(_carried_zone(root))[1]
    xy.set_value(1, float(xy.values[1]) + 0.002)


def _move_the_zone_to_the_front(root):
    _carried_zone(root).find("layer").set_value(0, "F.Cu")


def _rename_the_zone_net(root):
    _carried_zone(root).find("net_name").set_value(0, "VCC")


def _confirm_corrupted(board, tmp_path, monkeypatch, corrupt):
    drc = fake_drc()
    baseline = gate.snapshot_rules(board, drc=drc)
    (tmp_path / "fill").mkdir()
    token = _propose(board, tmp_path / "o")

    def confirm():
        return pour_step.confirm_pour_step(
            board, token, baseline=baseline, router=make_fake_router(FULL), drc=drc,
            fill=fake_fill(tmp_path / "fill"), connectivity_check=False, **PARITY,
        )

    saves = corrupt_pcb_saves(monkeypatch, corrupt, when=in_revision_tmp(tmp_path / "o"))
    return token, saves, confirm


def _assert_refused_then_poured(board, tmp_path, token, saves, confirm, named):
    before = project_hashes(board)
    failed = confirm()
    assert len(saves.hits) == 1  # the corruption really landed in the temporary revision
    assert failed["status"] == "error", failed
    assert failed["reason"].startswith("CRITICAL:") and named in failed["reason"], failed
    assert failed["original_unchanged"] is True
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fill", "proj"]  # no revision, no temporary
    assert list((tmp_path / "fill").iterdir()) == []  # refused before the refill ran
    assert project_hashes(board) == before
    assert token in pour_step._PENDING
    saves.disarm()
    ok = confirm()
    assert ok["status"] == "poured", ok  # same token: the corruption alone caused the failure
    zones = pour_step.read_zone_polygons(Path(ok["output_pcb"]))
    assert [(z["net"], z["layer"], z["polygon"]) for z in zones] == [("GND", "B.Cu", FULL)]


@pytest.mark.parametrize(
    ("corrupt", "named"),
    [
        pytest.param(_drop_c1, "top-level node counts", id="P1-node-counts"),
        pytest.param(_reissue_the_zone_uuid, "carried node is missing", id="P2-carried-key"),
    ],
)
def test_reload_self_check_fires_on_a_corrupted_write(board, tmp_path, monkeypatch, corrupt, named):
    token, saves, confirm = _confirm_corrupted(board, tmp_path, monkeypatch, corrupt)
    _assert_refused_then_poured(board, tmp_path, token, saves, confirm, named)


@pytest.mark.parametrize(
    "corrupt",
    [
        pytest.param(_move_a_zone_corner, id="zone-polygon"),
        pytest.param(_nudge_a_zone_corner, id="zone-corner-2um"),
        pytest.param(_move_the_zone_to_the_front, id="zone-layer"),
        pytest.param(_rename_the_zone_net, id="zone-net"),
    ],
)
def test_reload_self_check_compares_the_carried_content_not_only_its_uuid(board, tmp_path, monkeypatch, corrupt):
    """Corruptions that keep the uuid: the carried node must reload as it was accepted."""
    token, saves, confirm = _confirm_corrupted(board, tmp_path, monkeypatch, corrupt)
    _assert_refused_then_poured(board, tmp_path, token, saves, confirm, f"zone:{_CARRIED_ZONE_UUID}")


# --- review round (ticket 31): the whole file, exact text --------------------------------------


def _move_c1(root):
    from tests._self_check_helpers import shift_xy

    shift_xy(next(f for f in top_nodes(root, "footprint") if footprint_reference(f) == "C1"), "at", dx=5.0)


def _thicken_the_board(root):
    root.find("general").find("thickness").set_value(0, 2.0)


def _append_a_child_to_the_zone(root):
    from kicad_tools.sexp import SExp

    _carried_zone(root).append(SExp.list("locked", "yes"))


def _rename_a_child_of_the_zone(root):
    _carried_zone(root).find("hatch").name = "hatches"


def _replace_a_zone_list_by_an_atom(root):
    from kicad_tools.sexp import SExp

    zone = _carried_zone(root)
    hatch = zone.find("hatch")
    index = zone.children.index(hatch)
    zone.remove(hatch)
    zone.insert(index, SExp.atom("hatch"))


@pytest.mark.parametrize(
    ("corrupt", "named"),
    [
        pytest.param(_move_c1, "footprint:C1", id="untouched-footprint-moved"),
        pytest.param(_thicken_the_board, "(general", id="board-thickness"),
        pytest.param(_append_a_child_to_the_zone, f"zone:{_CARRIED_ZONE_UUID}", id="carried-child-appended"),
        pytest.param(_rename_a_child_of_the_zone, f"zone:{_CARRIED_ZONE_UUID}", id="carried-child-renamed"),
        pytest.param(_replace_a_zone_list_by_an_atom, f"zone:{_CARRIED_ZONE_UUID}", id="carried-list-becomes-atom"),
    ],
)
def test_reload_self_check_compares_the_whole_file_exactly(board, tmp_path, monkeypatch, corrupt, named):
    """Nodes the step did not carry, and every shape of change to one it did, must reload as saved."""
    token, saves, confirm = _confirm_corrupted(board, tmp_path, monkeypatch, corrupt)
    _assert_refused_then_poured(board, tmp_path, token, saves, confirm, named)


def gate_parse(pcb: Path):
    from kicad_tools.sexp import parse_file

    return parse_file(str(pcb))


def test_a_kicad10_board_pours_and_the_added_net_table_is_named_not_hidden(board, tmp_path):
    """PCB.load adds a net table to a board without one: tolerated by name, nothing else is."""
    to_kicad10_nets(board)
    result = confirm_fake(board, tmp_path, router=make_fake_router(FULL))
    assert result["status"] == "poured", result
    tolerated = result["write_check"]["tolerated"]
    nets = {str(c.values[1]) for c in gate_parse(Path(result["output_pcb"])).children if c.name == "net"}
    assert tolerated["count"] == len(tolerated["items"]) == len(nets) >= 3, tolerated
    assert all(label.startswith("(net ") for label in tolerated["items"]), tolerated
    assert {"GND", "VCC"} <= nets
    assert result["revision_differences"]["other_content_identical"] is True


# --- review round 2 (ticket 31): the tolerated net table must still reload as saved ---------------


def _gnd_net(root):
    return next(n for n in top_nodes(root, "net") if len(n.values) > 1 and str(n.values[1]) == "GND")


def _rename_the_added_gnd_net(root):
    _gnd_net(root).set_value(1, "GNDX")


def _renumber_the_added_gnd_net(root):
    _gnd_net(root).set_value(0, 99)


def _drop_the_added_gnd_net(root):
    root.remove(_gnd_net(root))


@pytest.mark.parametrize(
    ("corrupt", "named"),
    [
        # count-neutral: only the reload-versus-saved half of the write check can see these
        pytest.param(_rename_the_added_gnd_net, 'present but not intended: (net 2 "GNDX")', id="added-net-renamed"),
        pytest.param(_renumber_the_added_gnd_net, 'present but not intended: (net 99 "GND")', id="added-net-renumbered"),
        # a dropped entry already changes the node counts
        pytest.param(_drop_the_added_gnd_net, "CRITICAL: top-level node counts are wrong", id="added-net-dropped"),
    ],
)
def test_on_a_kicad10_board_the_added_net_table_must_reload_as_saved(board, tmp_path, monkeypatch, corrupt, named):
    """PCB.load adds a net table to a board without one; it is tolerated against the source, so
    only the reload-versus-saved half of the check can catch a corrupted entry."""
    to_kicad10_nets(board)
    assert top_nodes(gate_parse(board), "net") == []  # the premise: no net table in the source
    drc = fake_drc()
    baseline = gate.snapshot_rules(board, drc=drc)
    (tmp_path / "fill").mkdir()
    token = _propose(board, tmp_path / "o")
    router = make_fake_router(zones=[{"net": "GND", "layer": "B.Cu", "polygon": FULL, "net_text": '(net "GND")'}])

    def confirm():
        return pour_step.confirm_pour_step(
            board, token, baseline=baseline, router=router, drc=drc,
            fill=fake_fill(tmp_path / "fill"), connectivity_check=False, **PARITY,
        )

    saves = corrupt_pcb_saves(monkeypatch, corrupt, when=in_revision_tmp(tmp_path / "o"))
    before = project_hashes(board)
    failed = confirm()
    assert len(saves.hits) == 1
    assert failed["status"] == "error", failed
    assert failed["reason"].startswith("CRITICAL:") and named in failed["reason"], failed
    assert sorted(p.name for p in tmp_path.iterdir()) == ["fill", "proj"]
    assert project_hashes(board) == before and token in pour_step._PENDING
    saves.disarm()
    ok = confirm()
    assert ok["status"] == "poured", ok
    assert ok["write_check"]["tolerated"]["count"] == 4 and ok["write_check"]["unexpected"]["count"] == 0
