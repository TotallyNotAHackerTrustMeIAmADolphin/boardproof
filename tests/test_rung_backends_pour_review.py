"""Review of ticket 23: embedded zones, provenance warnings, manifest shape, Freerouting
refusal, and a machine-checked "a track through the pour is clean after refill"."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp import autonomy_run, rung_backends

from test_autonomy_run_review import _measure
from test_rung_backends import KRT_BASELINE, needs_krt  # noqa: F401
from test_rung_backends_pour import _zone_board

FIXTURES = Path(__file__).parent / "fixtures"


def _embed_zone_in_first_footprint(board: Path, keepout_child: bool = False) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    pcb = PCB.load(board)
    zone = next(c for c in pcb._sexp.children if c.name == "zone")
    pcb._sexp.remove(zone)
    footprint = next(c for c in pcb._sexp.children if c.name == "footprint")
    text = zone.to_string()
    if keepout_child:  # a keepout child must not hide stored fill
        text = text.rstrip().rstrip(")") + " (keepout (tracks not_allowed) (vias not_allowed)))"
    footprint.append(parse_string(text))
    write_layer.save_pcb(pcb, board)


# --- (1) embedded zones are seen --------------------------------------------------------------


def test_a_filled_zone_embedded_in_a_footprint_is_refused(tmp_path):
    board = _zone_board(tmp_path, "embedded", strip_fill=False)
    _embed_zone_in_first_footprint(board)
    assert "filled_polygon" in board.read_text()
    assert not any(c.name == "zone" for c in __import__("kicad_tools.sexp", fromlist=["parse_file"])
                   .parse_file(str(board)).children)  # really not top-level any more
    reason = rung_backends.input_refusal(board)
    assert reason and "stored fill" in reason


def test_an_embedded_outline_only_zone_is_listed_in_the_pour_zones(tmp_path):
    board = _zone_board(tmp_path, "embedded_outline", strip_fill=True)
    _embed_zone_in_first_footprint(board)
    zones = rung_backends.pour_zones(board)
    assert [(z["net"], z["filled_in_file"]) for z in zones] == [("GND", False)]
    assert zones[0]["embedded_in"]
    assert rung_backends.input_refusal(board) is None


def test_a_keepout_child_cannot_hide_stored_fill(tmp_path):
    board = _zone_board(tmp_path, "hidden", strip_fill=False)
    _embed_zone_in_first_footprint(board, keepout_child=True)
    assert rung_backends.input_refusal(board) is not None
    # ... and at top level too
    top = _zone_board(tmp_path, "hidden_top", strip_fill=False)
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    pcb = PCB.load(top)
    zone = next(c for c in pcb._sexp.children if c.name == "zone")
    zone.append(parse_string("(keepout (tracks not_allowed) (vias not_allowed))"))
    write_layer.save_pcb(pcb, top)
    assert rung_backends.input_refusal(top) is not None


# --- (2) provenance is structural, and says so ------------------------------------------------


def test_the_manifest_says_provenance_is_structural_and_warns_for_every_pour(tmp_path):
    board = _zone_board(tmp_path, "outline", strip_fill=True)
    setup = _setup(board, tmp_path)
    assert setup["pour_provenance"] == "structural: not verified as pour-step output"
    assert [w["net"] for w in setup["warnings"]] == ["GND"]
    assert "GND" in setup["warnings"][0]["message"]


def test_an_expected_pour_is_not_warned_about_but_another_one_is(tmp_path):
    board = _zone_board(tmp_path, "outline", strip_fill=True)
    quiet = _setup(board, tmp_path, expected_pours={("GND", "F.Cu")})
    assert quiet["warnings"] == []
    loud = _setup(board, tmp_path, expected_pours={("+3V3", "B.Cu")})
    assert [(w["net"], w["layer"]) for w in loud["warnings"]] == [("GND", "F.Cu")]


def test_a_pour_without_a_net_is_warned_about(tmp_path):
    from kicad_tools.schema import PCB

    board = _zone_board(tmp_path, "nonet", strip_fill=True)
    pcb = PCB.load(board)
    zone = next(c for c in pcb._sexp.children if c.name == "zone")
    zone.find("net").set_value(0, "")
    write_layer.save_pcb(pcb, board)
    setup = _setup(board, tmp_path, expected_pours={("GND", "F.Cu")})
    assert any("no net" in w["message"] for w in setup["warnings"])


def test_expect_pour_is_a_repeatable_cli_option(tmp_path):
    parsed = autonomy_run.parse_expect_pours(["GND:B.Cu", "+3V3:F.Cu"])
    assert parsed == {("GND", "B.Cu"), ("+3V3", "F.Cu")}
    with pytest.raises(ValueError):
        autonomy_run.parse_expect_pours(["GND"])


def _setup(board, tmp_path, **kw):
    outcome = _measure(board, tmp_path, parity="not_applicable", parity_reason="fixture has no schematic",
                       runs_root=tmp_path / f"runs{len(list(tmp_path.glob('runs*')))}", **kw)
    assert outcome["status"] == "RAN", outcome
    manifest = json.loads((Path(outcome["report"]["run_dir"]) / "manifest.json").read_text())
    return manifest["setup"]


# --- (3) manifest shape -------------------------------------------------------------------------


def test_pour_zones_is_always_a_list_with_a_separate_error_string(tmp_path, monkeypatch):
    board = _zone_board(tmp_path, "outline", strip_fill=True)
    ok = _setup(board, tmp_path)
    assert isinstance(ok["pour_zones"], list) and ok["pour_zones_error"] is None
    assert ok["measured_board_sha256"] and ok["source_board_sha256"]

    def boom(path):
        raise RuntimeError("cannot parse")

    # the preflight guard is bypassed here so only the manifest recording is exercised
    monkeypatch.setattr(rung_backends, "pour_zones", boom)
    bad = _setup(board, tmp_path, preflight=lambda b: None)
    assert bad["pour_zones"] == []
    assert "cannot parse" in bad["pour_zones_error"]


# --- (4) test gaps ----------------------------------------------------------------------------------


def test_the_freerouting_adapter_refuses_a_filled_pour_before_routing(tmp_path, monkeypatch):
    board = _zone_board(tmp_path, "filled", strip_fill=False)
    monkeypatch.setattr(rung_backends, "_freerouting_ready", lambda: None)  # tool "present"
    called = []
    monkeypatch.setattr(rung_backends.routing_toolset, "export_dsn_with_kicad",
                        lambda *a, **k: called.append(1))
    (tmp_path / "a").mkdir()
    params = {"max_passes": 10, "optimization_strategy": None, "selection_strategy": None}
    before = board.read_bytes()
    outcome = rung_backends.freerouting_backend()(board, params, 30.0, tmp_path / "a" / "router.log")
    assert outcome["status"] == "failure" and outcome["cause"] == "input-refused"
    assert called == []
    assert board.read_bytes() == before


def _segments_crossing_pour(board: Path) -> list[dict]:
    """Raw-file check: added track segments of a DIFFERENT net than the zone that intersect
    the zone's raw polygon (page-absolute frame, as written)."""
    from kicad_tools.sexp import parse_file

    root = parse_file(str(board))
    zone = next(c for c in root.children if c.name == "zone")
    pts = [(float(xy.values[0]), float(xy.values[1])) for xy in zone.find("polygon").find("pts").find_all("xy")]
    zone_net = str(zone.find("net").values[-1])

    def seg_cross(a, b, c, d):
        def o(p, q, r):
            return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
        return o(a, b, c) * o(a, b, d) < 0 and o(c, d, a) * o(c, d, b) < 0

    def inside(p):
        x, y, hit = p[0], p[1], False
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
                hit = not hit
        return hit

    net_names = {str(c.values[0]): str(c.values[1]) for c in root.children
                 if c.name == "net" and len(c.values) > 1}
    out = []
    for seg in (c for c in root.children if c.name == "segment"):
        net_value = str(seg.find("net").values[0])
        net = net_names.get(net_value, net_value)
        if net == zone_net:
            continue
        a = (float(seg.find("start").values[0]), float(seg.find("start").values[1]))
        b = (float(seg.find("end").values[0]), float(seg.find("end").values[1]))
        edges = list(zip(pts, pts[1:] + pts[:1]))
        if inside(a) or inside(b) or any(seg_cross(a, b, c, d) for c, d in edges):
            out.append({"net": net, "start": a, "end": b})
    return out


@needs_krt
def test_a_track_of_another_net_really_crosses_the_pour_and_the_gate_is_still_clean(tmp_path):
    from kicad_mcp import verify_board

    board = _zone_board(tmp_path, "real", strip_fill=True)
    baseline = verify_board.snapshot_rules(board)
    (tmp_path / "a").mkdir()
    outcome = rung_backends.krt_backend()(board, dict(KRT_BASELINE), 120.0, tmp_path / "a" / "router.log")
    assert outcome["status"] == "ok", outcome
    crossing = _segments_crossing_pour(board)
    assert crossing, "the evidence only counts if added copper actually runs through the pour"
    graded = verify_board.verify_board(
        board, parity="not_applicable", parity_reason="fixture has no schematic", baseline=baseline)
    assert (graded["verdict"], graded["violations"], graded["unconnected"]) == ("PASS", [], [])
