"""Issue 244: the pad, copper and graphic net readers of decoupling_check, the routing toolset and the
placement toolset go through the shared reader of issue 224 (``raw_geometry.net_name``): KiCad 10
(``(net "name")``) and older (``(net N "name")`` or ``(net N)`` plus a table) boards read the same,
and an unknown form is refused, never read as an empty or a guessed name."""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from boardproof import decoupling_check as dc
from boardproof import raw_geometry, write_layer
from boardproof.toolsets import placement, routing

FIXTURES = Path(__file__).parent / "fixtures"
KICAD10 = FIXTURES / "zone_obstacle" / "zone_obstacle.kicad_pcb"  # version 20260206
OLDER = FIXTURES / "graphics_removal_board.kicad_pcb"  # version 20241229, (net N) plus a table


def _pad(net: SExp | None) -> SExp:
    pad = SExp.list("pad", "1", "smd", "rect")
    if net is not None:
        pad.append(net)
    return pad


def _replace_net(node, net) -> None:
    node.remove(node.find("net"))
    node.insert(0, net)


def _first_pad(root):
    fp = next(c for c in root.children if c.name == "footprint")
    return next(c for c in fp.children if c.name == "pad")


# --- decoupling_check._pad_net ---------------------------------------------------------------------------------------


def test_pad_net_on_a_kicad10_board_is_the_pad_name() -> None:
    assert dc._pad_net(_first_pad(write_layer.load(KICAD10)), {}) == "VCC"


def test_pad_net_older_form_with_number_and_name() -> None:
    assert dc._pad_net(_pad(SExp.list("net", 1, write_layer.string("VCC"))), {}) == "VCC"


def test_pad_net_of_a_pad_without_net_is_empty() -> None:
    assert dc._pad_net(_pad(None), {}) == ""


def test_pad_net_unknown_form_is_refused() -> None:
    with pytest.raises(raw_geometry.RawGeometryError):
        dc._pad_net(_pad(SExp.list("net", 1, 2, 3)), {})


def test_check_with_an_unknown_pad_net_form_is_an_error(monkeypatch) -> None:
    root = write_layer.load(KICAD10)
    _replace_net(_first_pad(root), SExp.list("net", 1, 2, 3))
    monkeypatch.setattr(dc.write_layer, "load", lambda _p: root)
    out = dc.check_decoupling_placement(KICAD10)
    assert out["status"] == "error" and "net" in out["reason"], out


# --- routing._copper_key / raw_copper ---------------------------------------------------------------------------------


def _segment(net: SExp) -> SExp:
    return SExp.list("segment", SExp.list("start", 0, 0), SExp.list("end", 1, 0), SExp.list("width", 0.2),
                     SExp.list("layer", write_layer.string("F.Cu")), net)


def _root_with(net: SExp, table: dict[int, str] | None = None) -> SExp:
    root = SExp.list("kicad_pcb")
    for number, name in (table or {}).items():
        root.append(SExp.list("net", number, write_layer.string(name)))
    root.append(_segment(net))
    return root


def _only_net(root) -> str:
    (key,) = routing.raw_copper(root)
    return key[-1]


def test_copper_net_kicad10_name_only() -> None:
    assert _only_net(_root_with(SExp.list("net", write_layer.string("GND")))) == "GND"


def test_copper_net_older_number_only_comes_from_the_table() -> None:
    assert _only_net(_root_with(SExp.list("net", 2), {1: "VCC", 2: "GND"})) == "GND"


def test_copper_net_older_number_and_name() -> None:
    assert _only_net(_root_with(SExp.list("net", 2, write_layer.string("GND")))) == "GND"


def test_copper_net_undeclared_number_is_refused() -> None:
    with pytest.raises(raw_geometry.RawGeometryError):
        routing.raw_copper(_root_with(SExp.list("net", 7), {1: "VCC"}))


def test_copper_net_unknown_form_is_refused() -> None:
    with pytest.raises(raw_geometry.RawGeometryError):
        routing.raw_copper(_root_with(SExp.list("net", 1, 2, 3)))


# --- placement._graphic_node_net_name ---------------------------------------------------------------------------------


def _zone_name(path: Path, shape: str = "zone") -> str | None:
    board = write_layer.load_pcb(path)
    zone = next(n for n in placement._iter_top_level_graphics(board) if n.tag == shape)
    return placement._graphic_node_net_name(zone, board)


def test_graphic_net_on_the_kicad10_demo() -> None:
    assert _zone_name(KICAD10) == "GND"


def test_graphic_net_on_an_older_board_comes_from_the_table() -> None:
    assert _zone_name(OLDER, "gr_rect") == "VCC"  # (net 1), table (net 1 "VCC")


def test_graphic_net_unknown_form_is_refused() -> None:
    board = write_layer.load_pcb(KICAD10)
    zone = next(n for n in placement._iter_top_level_graphics(board) if n.tag == "zone")
    _replace_net(zone, SExp.list("net", 1, 2, 3))
    with pytest.raises(raw_geometry.RawGeometryError):
        placement._graphic_node_net_name(zone, board)
