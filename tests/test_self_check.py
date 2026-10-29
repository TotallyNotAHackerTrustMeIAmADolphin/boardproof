"""Unit tests for the shared reload compare (src/kicad_mcp/_self_check.py) and the corrupted-write
helper (tests/_self_check_helpers.py). Build ticket 31; the tool-level tests that show each check
fires live in test_board_outline.py and test_pour_step.py."""

from __future__ import annotations

import pytest

from kicad_mcp import _self_check


def _root(text: str):
    from kicad_tools.sexp import parse_string

    return parse_string(text)


BOARD = '(kicad_pcb (version 20240108) (general (thickness 1.6)) (net 0 "") (net 1 "GND") ' \
    '(gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts") (uuid "u-line")))'
NO_NETS = '(kicad_pcb (version 20240108) (general (thickness 1.6)))'


def test_an_exact_round_trip_has_no_differences():
    source = _root(BOARD)
    diff = _self_check.reload_differences(
        source=source, removed=[], written=[], memory=_root(BOARD), reloaded=_root(BOARD)
    )
    assert diff == {"missing": [], "unexpected": [], "tolerated": []}


def test_a_number_written_differently_is_a_difference_even_below_a_micrometre():
    reloaded = _root(BOARD.replace("(end 10 0)", "(end 10.0001 0)"))
    diff = _self_check.reload_differences(
        source=_root(BOARD), removed=[], written=[], memory=_root(BOARD), reloaded=reloaded
    )
    assert diff["missing"] == ["gr_line:u-line"] and diff["unexpected"] == ["gr_line:u-line"]


def test_quoting_is_compared():
    reloaded = _root(BOARD.replace('(layer "Edge.Cuts")', "(layer Edge.Cuts)"))
    diff = _self_check.reload_differences(
        source=_root(BOARD), removed=[], written=[], memory=_root(BOARD), reloaded=reloaded
    )
    assert diff["missing"] == ["gr_line:u-line"]


def test_removed_and_written_nodes_make_the_intended_change():
    source = _root(BOARD)
    new = _root('(gr_line (start 0 0) (end 20 0) (layer "Edge.Cuts") (uuid "u-new"))')
    after = BOARD.replace('(gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts") (uuid "u-line"))', new.to_string())
    old = next(c for c in source.children if c.name == "gr_line")
    diff = _self_check.reload_differences(
        source=source, removed=[old], written=[new], memory=_root(after), reloaded=_root(after)
    )
    assert diff == {"missing": [], "unexpected": [], "tolerated": []}
    # the same reload without declaring the removal: the old line is missing
    diff = _self_check.reload_differences(
        source=source, removed=[], written=[new], memory=_root(after), reloaded=_root(after)
    )
    assert diff["missing"] == ["gr_line:u-line"]


def test_a_net_table_added_to_a_board_without_one_is_tolerated_by_name():
    with_nets = NO_NETS[:-1] + ' (net 0 "") (net 1 "GND"))'
    diff = _self_check.reload_differences(
        source=_root(NO_NETS), removed=[], written=[], memory=_root(with_nets), reloaded=_root(with_nets)
    )
    assert diff == {"missing": [], "unexpected": [], "tolerated": ['(net 0 "")', '(net 1 "GND")']}


def test_a_net_entry_added_to_a_board_that_has_a_net_table_is_unexpected():
    more = BOARD[:-1] + ' (net 2 "VCC"))'
    diff = _self_check.reload_differences(
        source=_root(BOARD), removed=[], written=[], memory=_root(more), reloaded=_root(more)
    )
    assert diff["unexpected"] == ['(net 2 "VCC")'] and diff["tolerated"] == []


def test_a_tolerated_net_table_must_still_reload_as_saved():
    saved = NO_NETS[:-1] + ' (net 0 "") (net 1 "GND"))'
    reloaded = NO_NETS[:-1] + ' (net 0 "") (net 1 "VCC"))'
    diff = _self_check.reload_differences(
        source=_root(NO_NETS), removed=[], written=[], memory=_root(saved), reloaded=_root(reloaded)
    )
    assert diff["missing"] == ['(net 1 "GND")'] and diff["unexpected"] == ['(net 1 "VCC")']


def test_labels_name_footprints_zones_and_uuids():
    fp = _root('(footprint "X" (uuid "f") (property "Reference" "C1" (at 0 0)))')
    zone = _root('(zone (net 0) (uuid "z") (name "KEEPOUT_H1"))')
    arc = _root('(gr_arc (start 0 0) (mid 1 1) (end 2 0) (uuid "a"))')
    assert [_self_check.node_label(n) for n in (fp, zone, arc)] == ["footprint:C1", "zone:KEEPOUT_H1", "gr_arc:a"]


def test_describe_lists_both_sides():
    text = _self_check.describe({"missing": ["footprint:C1"], "unexpected": ["footprint:C1"], "tolerated": []})
    assert text == "missing after reload: footprint:C1; present but not intended: footprint:C1"


@pytest.mark.parametrize(
    ("value", "written"),
    [(1048.535534, 1048.535534), (103.0125, 103.0125), (110.01234, 110.01234), (105.0, 105.0), (-0.0, -0.0)],
)
def test_as_written_is_what_the_write_layer_saves_for_a_new_number(value, written):
    """Full precision since build ticket 42 (kicad_tools' own writer rounded to %.6g)."""
    assert _self_check.as_written(value) == written


def test_corrupt_writes_needs_path_of_for_anything_but_the_layer_save(monkeypatch):
    from tests import _self_check_helpers as helpers

    class Writer:
        def write(self, target):
            return target

    with pytest.raises(TypeError, match="path_of"):
        helpers.corrupt_writes(monkeypatch, Writer, "write", lambda path: None)
    handle = helpers.corrupt_writes(
        monkeypatch, Writer, "write", lambda path: None, path_of=lambda args, kwargs: args[1]
    )
    Writer().write("x.kicad_sch")
    assert [str(p) for p in handle.hits] == ["x.kicad_sch"]


def test_bounded_gives_count_and_at_most_limit_items():
    diff = {"missing": [f"m{i}" for i in range(25)], "unexpected": [], "tolerated": ["t"]}
    assert _self_check.bounded(diff) == {
        "missing": {"count": 25, "items": [f"m{i}" for i in range(20)]},
        "unexpected": {"count": 0, "items": []},
        "tolerated": {"count": 1, "items": ["t"]},
    }
