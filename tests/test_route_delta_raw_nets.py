"""route_delta and the neck network read track, via and pad nets from the raw S-expressions
(build ticket 252, CLAUDE.md hard rule 2) through raw_geometry.net_name / net_table, and fail closed."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from kicad_tools.sexp import parse_string

from boardproof import raw_geometry
from boardproof.routers import kicad_routing_tools as krt

FIXTURES = Path(__file__).parent / "fixtures" / "uw" / "r1krt"  # version 20241229, (net N) tracks


def _board(body: str, table: str = ""):
    return parse_string(f"(kicad_pcb (version 20260206) {table} {body})")


def _models(segs=(), vias=()):
    return SimpleNamespace(
        segments=[SimpleNamespace(uuid=u, layer="F.Cu") for u in segs],
        vias=[SimpleNamespace(uuid=u) for u in vias],
    )


def test_kicad10_name_only_nets():
    root = _board('(segment (layer "F.Cu") (net "GND") (uuid "s0")) (via (net "VCC") (uuid "v0"))')
    assert krt._copper_nets(_models(["s0"], ["v0"]), root) == (["GND"], ["VCC"])


def test_older_number_nets_resolved_by_table():
    root = _board('(segment (layer "F.Cu") (net 3) (uuid "s0")) (via (net 0) (uuid "v0"))', '(net 0 "") (net 3 "GND")')
    assert krt._copper_nets(_models(["s0"], ["v0"]), root) == (["GND"], [""])


def test_objects_without_own_uuid_pair_by_file_order():
    root = _board('(segment (layer "F.Cu") (net "A")) (segment (layer "F.Cu") (net "B")) (via (net "C"))')
    assert krt._copper_nets(_models(["", ""], [""]), root) == (["A", "B"], ["C"])


@pytest.mark.parametrize("net", ["(net 7)", "(net 1 2 3)", "(net)"])
def test_unknown_net_form_fails_closed(net):
    root = _board(f'(segment (layer "F.Cu") {net} (uuid "s0"))', '(net 3 "GND")')
    with pytest.raises(raw_geometry.RawGeometryError):
        krt._copper_nets(_models(["s0"]), root)


def test_via_missing_from_raw_board_fails_closed():
    with pytest.raises(raw_geometry.RawGeometryError):
        krt._copper_nets(_models([], ["v0"]), _board(""))


def test_route_delta_older_board_names_nets():
    delta = krt.route_delta(FIXTURES / "in" / "rung1_usbc_attiny1616.kicad_pcb",
                            FIXTURES / "out" / "rung1_usbc_attiny1616.kicad_pcb")
    assert delta["trace_specs"] and all(spec["net"] for spec in delta["trace_specs"])


def test_neck_network_segment_on_undeclared_net_fails_closed():
    from boardproof import rung_backends

    root = parse_string('(kicad_pcb (version 20240108) (net 3 "GND") '
                        '(segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net 9)))')
    with pytest.raises(raw_geometry.RawGeometryError):
        rung_backends._neck_network(root, {3: "GND"}, [], {}, "GND", "F.Cu")


def test_segment_layer_mismatch_fails_closed():
    root = _board('(segment (layer "B.Cu") (net "GND") (uuid "s0"))')
    with pytest.raises(raw_geometry.RawGeometryError):
        krt._copper_nets(_models(["s0"]), root)  # the model says F.Cu


def test_import_ses_route_delta_failure_is_a_specctra_error(monkeypatch, tmp_path):
    import shutil

    from boardproof.toolsets import routing

    snap = tmp_path / "snap.kicad_pcb"
    shutil.copy(Path(__file__).parent / "fixtures" / "simple_led.kicad_pcb", snap)
    monkeypatch.setattr(routing, "run_kicad_specctra", lambda *_a, **_k: {"ok": True})
    monkeypatch.setattr(routing, "_board_digest", lambda _p: "x")

    def boom(*_a):
        raise raw_geometry.RawGeometryError("net node of an unknown form")

    monkeypatch.setattr(routing.krt_backend, "route_delta", boom)
    snapshot = {"source_pcb": str(snap), "snapshot_pcb": str(snap), "source_sha256": "x"}
    with pytest.raises(routing.KicadSpecctraError):
        routing.import_ses_with_kicad(snapshot, tmp_path / "a.ses", tmp_path / "work")


def test_neck_pad_and_segment_undeclared_net_fails_closed():
    from boardproof import rung_backends

    root = parse_string('(kicad_pcb (version 20240108) (net 3 "GND") (footprint "X" (at 0 0) (layer "F.Cu") '
                        '(pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 9))))')
    with pytest.raises(raw_geometry.RawGeometryError):
        rung_backends._neck_pads(root, ["F.Cu", "B.Cu"])
