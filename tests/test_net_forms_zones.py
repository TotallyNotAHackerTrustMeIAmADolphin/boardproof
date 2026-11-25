"""Issue 239: zone and pad net readers on KiCad 10 boards (``(net "name")``, no net table) and on
older ones (``(net N)`` plus ``(net_name "name")``), through the shared reader of issue 224."""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from boardproof import board_outline, isolation, pour_step, raw_geometry, verify_board, write_layer

KICAD10 = Path(__file__).parent / "fixtures" / "zone_obstacle" / "zone_obstacle.kicad_pcb"  # version 20260206


def _zone(root):
    return next(c for c in root.children if c.name == "zone")


def _old_form(root) -> None:
    """In memory only: the older zone shape, ``(net 1)`` + ``(net_name "GND")`` and a net table."""
    zone = _zone(root)
    zone.remove(zone.find("net"))
    zone.insert(0, SExp.list("net_name", write_layer.string("GND")))
    zone.insert(0, SExp.list("net", 1))
    root.insert(0, SExp.list("net", 1, write_layer.string("GND")))
    root.insert(0, SExp.list("net", 0, write_layer.string("")))


@pytest.fixture(params=["kicad10", "older"])
def root(request):
    root = write_layer.load(KICAD10)
    if request.param == "older":
        _old_form(root)
    return root


def test_stored_fill_reads_the_zone_net(root, monkeypatch) -> None:
    monkeypatch.setattr(verify_board.write_layer, "load", lambda _p: root)
    fill = verify_board._read_stored_fill(KICAD10)
    assert [z["net"] for z in fill["zones"]] == ["GND"]


def test_stored_fill_with_an_unknown_net_declaration_is_unreadable(monkeypatch) -> None:
    root = write_layer.load(KICAD10)
    root.insert(0, SExp.list("net", 1, 2, 3))
    monkeypatch.setattr(verify_board.write_layer, "load", lambda _p: root)
    assert verify_board.stored_fill_report(KICAD10)["status"] == "unreadable"


def test_isolation_pour_reports_the_zone_net(root) -> None:
    assert [p["net"] for p in isolation._Copper(root).pours] == ["GND"]


def test_outline_overlap_reports_the_zone_net(root) -> None:
    hole = {"reference": "H1", "at": (150.0, 86.0), "keepout_radius_mm": 1.0}
    hard, pours = board_outline._overlaps(root, [hole], geometry=[])
    assert [p["net"] for p in pours] == ["GND"], (hard, pours)


def test_zone_with_an_unknown_net_form_refuses(root) -> None:
    _zone(root).find("net").children[:] = [write_layer.number(1), write_layer.number(2), write_layer.number(3)]
    with pytest.raises(raw_geometry.RawGeometryError):
        isolation._Copper(root)


def test_pour_step_net_reader_refuses_unknown_forms_and_reads_both_good_ones() -> None:
    def node(*values):
        return SExp.list("pad", SExp.list("net", *values))

    assert pour_step._net_of(node(write_layer.string("GND"))) == {"id": None, "name": "GND"}
    assert pour_step._net_of(node(1, write_layer.string("GND"))) == {"id": 1, "name": "GND"}
    assert pour_step._net_of(node(1)) == {"id": 1, "name": None}
    for bad in [(1, 2), (write_layer.string("A"), write_layer.string("B")), (1, write_layer.string("G"), 3)]:
        with pytest.raises(raw_geometry.RawGeometryError):
            pour_step._net_of(node(*bad))


def test_probe_detach_writes_no_zero_net_on_a_kicad10_pad() -> None:
    root = write_layer.load(KICAD10)
    footprints = [c for c in root.children if c.name == "footprint"]
    fi, pi, pad = next(
        (fi, pi, p)
        for fi, fp in enumerate(footprints)
        for pi, p in enumerate(fp.find_all("pad"))
        if p.find("net") is not None
    )
    ref = pour_step._footprint_reference(footprints[fi])
    changed = pour_step._detach_pads(root, [{"fi": fi, "pi": pi, "ref": ref, "pad": str(pad.values[0])}])
    assert changed == 1 and pad.find("net") is None


def test_probe_detach_sets_net_zero_on_an_older_form_pad() -> None:
    root = write_layer.load(KICAD10)
    footprints = [c for c in root.children if c.name == "footprint"]
    fi, pi, pad = next(
        (fi, pi, p)
        for fi, fp in enumerate(footprints)
        for pi, p in enumerate(fp.find_all("pad"))
        if p.find("net") is not None
    )
    pad.remove(pad.find("net"))
    pad.append(SExp.list("net", 1, write_layer.string("GND")))
    ref = pour_step._footprint_reference(footprints[fi])
    assert pour_step._detach_pads(root, [{"fi": fi, "pi": pi, "ref": ref, "pad": str(pad.values[0])}]) == 1
    assert [*pad.find("net").values] == [0, ""]


def test_older_zone_net_number_the_table_does_not_declare_refuses() -> None:
    root = write_layer.load(KICAD10)
    _old_form(root)
    _zone(root).find("net").set_value(0, 7)  # (net 7) beside (net_name "GND"): the name is never taken from the sibling
    with pytest.raises(raw_geometry.RawGeometryError, match="not declared"):
        isolation._Copper(root)
