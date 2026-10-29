"""Build ticket 109 (owner's decision, ticket 108): protocol 6 allows a necked pad entry.

A narrower-than-class track is a *forced neck* only when ALL of these hold; otherwise it stays
an under-width fail:

1. the pad geometry forces it: the class width plus the class clearance cannot fit between the
   neighbouring pads, ``w <= s + 2(g - c)`` is violated by the class width;
2. its width is at or above the board minimum (``min_track_width``);
3. it lies within the length bound from the pad edge, measured along the track;
4. it is counted and labelled "PASS (necked)" (see ``test_rung_runner_necked_label.py``).

Every geometry fact is read from the RAW S-expressions: pad ``(at)``, ``(size)``, ``(layers)``,
``(primitives)``; segment ``(start)``, ``(end)``, ``(width)``. The board is a ``tmp_path`` copy of
the committed rung-1 input (``evals/rung1/layout_run/07_poured``); copper is added through
``write_layer`` (low-level S-expression API) and ``parse_string``. Nothing in the repo is written.

Raw geometry of that board (verified from its ``(pad ...)`` nodes): U1 ``(at 153 107.5)``,
pads 0.8 x 0.2 mm at 0.4 mm pitch, pad 4 (+3V3, Power class 0.4 mm / 0.2 mm clearance) at
(151.55, 107.9) between pad 3 (GND) and pad 5, gap 0.2 mm: the Power limit there is
0.2 + 2 * (0.2 - 0.2) = 0.2 mm. Board minimum track width 0.127 mm.
"""

from __future__ import annotations

import json
import math
import shutil
from collections import Counter
from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string

from kicad_mcp import rung_backends, rung_runner, write_layer

ROOT = Path(__file__).resolve().parents[1]
RUNG1 = ROOT / "evals" / "rung1" / "layout_run" / "07_poured"
BOARD_NAME = "rung1_usbc_attiny1616.kicad_pcb"
NET_ID = {"+3V3": 1, "/VBUS_F": 7, "GND": 8}

# The copper of ticket 107's scratch PASS board (Freerouting, fanout off, neck_width_um 200),
# +3V3 around U1.4: (start, end, width). Raw segment nodes of that board.
PASS_BOARD_3V3 = [
    ((151.55, 107.9), (150.8528, 107.9), 0.15),
    ((150.8528, 107.9), (150.3869, 107.9), 0.2),
    ((150.3869, 107.9), (150.3869, 107.8897), 0.4),
    ((150.3869, 107.8897), (149.4024, 107.8897), 0.2),
    ((149.4024, 107.8897), (149.4024, 106.5084), 0.4),
    ((150.3869, 109.3619), (150.3869, 107.9), 0.4),
]
PASS_BOARD_NECKED = [0.15, 0.2, 0.2]
PASS_BOARD_BEYOND_PAD_MM = (0.6972 - 0.4) + 0.4659 + 0.0103 + 0.9845  # 1.7579: from the pad edge x = 151.15


@pytest.fixture
def board(tmp_path):
    project = tmp_path / "proj"
    shutil.copytree(RUNG1, project)
    return project / BOARD_NAME


def _add(board: Path, segments, net: str = "+3V3", layer: str = "F.Cu"):
    root = write_layer.load(board)
    for (sx, sy), (ex, ey), width in segments:
        root.children.append(parse_string(
            f'(segment (start {sx} {sy}) (end {ex} {ey}) (width {width}) (layer "{layer}") (net {NET_ID[net]}) '
            f'(uuid "00000000-0000-4000-8000-{len(root.children):012d}"))'))
    write_layer.save(root, board)


def _classify(board: Path, max_length_mm=None):
    project = rung_backends._project_rules(board)
    items, _checked = rung_backends.under_width_items(board, Counter(), project)
    kwargs = {} if max_length_mm is None else {"max_length_mm": max_length_mm}
    return rung_backends.classify_necks(board, items, project, **kwargs)


def _by_width(items):
    return sorted((i["delivered_mm"], i["neck_verdict"], i["neck_reason"]) for i in items)


# --- reproduction: today's policy fails ticket 107's PASS board ----------------------------------


def test_reproduction_the_raw_check_still_lists_the_pass_board_neck_as_under_width(board):
    """The raw under-width list is unchanged by protocol 6: the three necked segments of ticket 107's
    scratch PASS board (0.15, 0.2, 0.2 mm in a 0.4 mm class) are listed. Only their CLASS changes."""
    _add(board, PASS_BOARD_3V3)
    project = rung_backends._project_rules(board)
    items, _ = rung_backends.under_width_items(board, Counter(), project)
    assert sorted(i["delivered_mm"] for i in items) == sorted(PASS_BOARD_NECKED)
    assert all(i["required_mm"] == 0.4 and i["netclass"] == "Power" for i in items)


# --- the forced neck ----------------------------------------------------------------------------


def test_the_pass_board_neck_at_u1_pin_4_is_a_forced_neck(board):
    _add(board, PASS_BOARD_3V3)
    items = _classify(board)
    assert [i["neck_verdict"] for i in items] == ["necked"] * 3
    for item in items:
        assert item["neck_pad"] == "U1.4"
        assert item["neck_pad_limit_mm"] == pytest.approx(0.2)  # s + 2 (g - c) = 0.2 + 2 (0.2 - 0.2)
        assert item["length_mm"] == pytest.approx(math.dist(item["start"], item["end"]))
    # the farthest necked point lies 1.758 mm beyond the pad edge (pad x from 151.15 to 151.95)
    assert max(i["neck_distance_mm"] for i in items) == pytest.approx(PASS_BOARD_BEYOND_PAD_MM, abs=1e-3)


def test_the_length_bound_is_stated_in_the_protocol_and_covers_the_pass_board():
    policy = rung_runner.load_protocol()["policies"]["necked_pad_entry"]
    assert policy["label"] == "PASS (necked)"
    assert PASS_BOARD_BEYOND_PAD_MM < policy["max_length_beyond_pad_mm"] < 7.8
    assert rung_backends.necked_pad_entry_policy()["max_length_beyond_pad_mm"] == policy["max_length_beyond_pad_mm"]


# --- limit 1: the pad geometry must force the neck (mutant: no geometry test) -------------------


def test_a_neck_where_the_class_width_would_fit_is_a_fail(board):
    """U2 pad 2 is 1.475 mm (plus polygon) wide with 0.6 mm gaps: 0.4 mm fits. Freerouting's 3/4 neck
    there (/VBUS_F 0.3 mm, ticket 107) is an unforced undercut."""
    _add(board, [((146.1611, 98.6489), (146.9886, 98.6489), 0.3), ((146.9886, 98.6489), (147.6375, 98.0), 0.3)],
         net="/VBUS_F")
    items = _classify(board)
    assert _by_width(items) == [(0.3, "fail", "not_forced_by_pad_geometry")] * 2


def test_a_narrow_track_that_touches_no_pad_is_a_fail(board):
    _add(board, [((150.0, 105.0), (150.0, 104.0), 0.2)])
    assert _by_width(_classify(board)) == [(0.2, "fail", "not_forced_by_pad_geometry")]


def test_a_neck_at_a_forcing_pad_of_another_net_does_not_count(board):
    """U1.4 forces a Power neck, but only for its own net: a +3V3-width problem on the GND pad 3 (Default
    0.127 mm fits 0.2 mm) is not the same pad."""
    _add(board, [((151.55, 107.5), (150.9141, 107.5), 0.1)], net="GND")
    # 0.1 is below the board minimum anyway; the point is the reason is not 'necked'
    assert all(i["neck_verdict"] == "fail" for i in _classify(board))


# --- limit 2: the board minimum (mutant: no floor) ------------------------------------------------


def test_a_necked_track_below_the_board_minimum_is_a_fail(board):
    """Freerouting's 3/4 neck of the Default class, 0.0952 mm, at U1.3 (ticket 107): below
    min_track_width 0.127 mm, a real DRC error. Here on the forcing pad U1.4 so ONLY the floor can fail it."""
    _add(board, [((151.55, 107.9), (150.8528, 107.9), 0.0952), ((150.8528, 107.9), (150.3869, 107.9), 0.2)])
    verdicts = {i["delivered_mm"]: (i["neck_verdict"], i["neck_reason"]) for i in _classify(board)}
    assert verdicts[0.0952] == ("fail", "below_board_minimum")
    assert verdicts[0.2] == ("necked", "forced_neck")  # the sub-minimum segment does not drag the rest down


def test_the_board_minimum_itself_is_allowed(board):
    _add(board, [((151.55, 107.9), (150.8528, 107.9), 0.127)])
    assert _by_width(_classify(board)) == [(0.127, "necked", "forced_neck")]


# --- limit 3: the length bound from the pad edge (mutant: no bound) -------------------------------


def test_a_neck_longer_than_the_bound_is_a_fail_and_the_bound_is_measured_from_the_pad_edge(board):
    """A 2.5 mm run out of the pad at 0.2 mm. Pad U1.4 spans x 151.15 .. 151.95; the run ends at x = 149.05,
    2.1 mm beyond the edge (2.5 from the centre)."""
    _add(board, [((151.55, 107.9), (149.05, 107.9), 0.2)])
    (item,) = _classify(board, max_length_mm=2.0)
    assert (item["neck_verdict"], item["neck_reason"]) == ("fail", "neck_longer_than_bound")
    assert item["neck_distance_mm"] == pytest.approx(2.1, abs=1e-3)
    (item,) = _classify(board, max_length_mm=2.2)
    assert item["neck_verdict"] == "necked"  # the same copper under a longer bound: only the bound failed it


def test_the_bound_counts_the_path_through_full_width_copper(board):
    """Narrow copper far down the net, reached through a long 0.4 mm connector, is not a neck at the pad."""
    _add(board, [((151.55, 107.9), (150.8, 107.9), 0.2),            # 0.35 mm beyond the pad edge, necked
                 ((150.8, 107.9), (148.0, 107.9), 0.4),             # full width
                 ((148.0, 107.9), (147.5, 107.9), 0.2)])            # narrow again, 3.65 mm out
    verdicts = {(i["start"][0], i["neck_verdict"]) for i in _classify(board, max_length_mm=2.0)}
    assert verdicts == {(151.55, "necked"), (148.0, "fail")}


def test_the_krt_run_of_ticket_107_is_a_fail(board):
    """KRT's power-tap neck-down (ticket 107): 7.8 mm at 0.127 mm, 1.5 mm of it starting at U1.4. Over the
    bound, and the 2.45 mm run out of U2 pad 3 (1.3 x 0.9 mm, 0.4 fits) is not forced at all."""
    _add(board, [((151.6, 107.9), (151.1, 107.9), 0.127), ((151.1, 107.9), (150.0, 106.8), 0.127),
                 ((150.0, 106.8), (150.0, 104.0), 0.127), ((150.0, 104.0), (150.0, 102.3), 0.127)])
    _add(board, [((147.6, 99.5), (149.332412, 101.232412), 0.127)])
    items = _classify(board)
    reasons = {(tuple(i["start"]), tuple(i["end"])): i["neck_reason"] for i in items}
    assert reasons[((147.6, 99.5), (149.332412, 101.232412))] == "not_forced_by_pad_geometry"
    assert reasons[((150.0, 106.8), (150.0, 104.0))] == "neck_longer_than_bound"
    assert reasons[((150.0, 104.0), (150.0, 102.3))] == "neck_longer_than_bound"
    assert not any(i["neck_verdict"] == "necked" and i["start"][1] < 106.0 for i in items)


def test_a_pad_in_front_of_the_entry_is_not_a_neighbour_beside_it(board):
    """Entered along +y (toward pad 5) the pad is 0.8 mm wide across the entry and only the exposed pad
    EP lies beside it (0.2 mm away): 0.8 + 2 (0.2 - 0.2) = 0.8 mm, so 0.4 mm fits. Pad 5, in front of
    the entry at 0.2 mm, must not count as a neighbour beside it (it would give a negative limit)."""
    _add(board, [((151.55, 107.9), (151.55, 108.0), 0.2)])
    assert _by_width(_classify(board)) == [(0.2, "fail", "not_forced_by_pad_geometry")]


# --- what is never a neck ------------------------------------------------------------------------


def test_a_via_and_an_arc_under_width_are_always_fails(board):
    root = write_layer.load(board)
    root.children.append(parse_string(
        '(via (at 151.0 108.5) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") (net 1) '
        '(uuid "00000000-0000-4000-8000-0000000000aa"))'))
    root.children.append(parse_string(
        '(arc (start 151.55 107.9) (mid 151.3 108.0) (end 151.0 108.2) (width 0.2) (layer "F.Cu") (net 1) '
        '(uuid "00000000-0000-4000-8000-0000000000ab"))'))
    write_layer.save(root, board)
    items = _classify(board)
    assert {(i["kind"], i["neck_verdict"], i["neck_reason"]) for i in items} == {
        ("via", "fail", "not_a_straight_track"), ("arc", "fail", "not_a_straight_track")}


def test_a_pad_whose_geometry_cannot_be_read_never_forces_a_neck(board):
    """Fail closed: a flipped footprint's pads are not read (their stored geometry is not verified), so the
    neck at one of them is a fail, not a guess."""
    root = write_layer.load(board)
    u1 = next(c for c in root.children if c.name == "footprint" and "VQFN" in str(c.values[0]))
    write_layer.set_string(u1.find("layer"), 0, "B.Cu")
    write_layer.save(root, board)
    _add(board, [((151.55, 107.9), (150.8528, 107.9), 0.15)])
    assert _by_width(_classify(board)) == [(0.15, "fail", "not_forced_by_pad_geometry")]


# --- review fixes: guard tests that go red when their guard line is removed ----------------------------


def test_a_forcing_pad_of_another_net_is_not_the_entry_of_a_net_that_merely_touches_it(board):
    """Review BLOCKS 1: a 0.2 mm /VBUS_F segment starting on U1.4 (net +3V3). U1.4 forces a Power neck, and
    /VBUS_F is a Power net (0.4 mm class), so the segment would read as a forced neck at U1.4 if the pad's
    net were not compared with the track's. 0.2 mm is above the board minimum, so ONLY the net test fails it."""
    _add(board, [((151.55, 107.9), (150.8528, 107.9), 0.2)], net="/VBUS_F")
    assert _by_width(_classify(board)) == [(0.2, "fail", "not_forced_by_pad_geometry")]


def test_a_forcing_pad_on_the_other_copper_layer_is_not_the_entry_of_a_back_layer_track(board):
    """Review BLOCKS 2: U1.4 is an F.Cu pad; a 0.2 mm +3V3 segment on B.Cu starting at its centre touches
    no copper of that pad. The neighbour U1.3 is made a through-copper pad ("*.Cu"), so a B.Cu entry limit
    exists (0.2 mm); without the pad-layer test the F.Cu-only U1.4 would be taken as its contact."""
    root = write_layer.load(board)
    u1 = next(c for c in root.children if c.name == "footprint" and "VQFN" in str(c.values[0]))
    pad3 = next(p for p in u1.find_all("pad") if str(p.values[0]) == "3")
    write_layer.set_string(pad3.find("layers"), 0, "*.Cu")
    write_layer.save(root, board)
    _add(board, [((151.55, 107.9), (150.8528, 107.9), 0.2)], layer="B.Cu")
    assert _by_width(_classify(board)) == [(0.2, "fail", "not_forced_by_pad_geometry")]


def _make_neighbour_unreadable(board: Path):
    """Pad 3 of U1 (GND, 0.4 mm from pad 4) becomes a trapezoid, a shape the check does not read."""
    root = write_layer.load(board)
    u1 = next(c for c in root.children if c.name == "footprint" and "VQFN" in str(c.values[0]))
    pad3 = next(p for p in u1.find_all("pad") if str(p.values[0]) == "3")
    write_layer.set_symbol(pad3, 2, "trapezoid")
    write_layer.save(root, board)


def test_an_unreadable_neighbour_pad_next_to_a_contact_pad_stops_the_judgement(board):
    """Review BLOCKS 3: unknown is never evidence of room. The neck at U1.4 is NOT judged; the check raises."""
    _make_neighbour_unreadable(board)
    _add(board, [((151.55, 107.9), (150.8528, 107.9), 0.15)])
    with pytest.raises(rung_backends.UnderWidthCheckError, match="does not read"):
        _classify(board)


def test_an_unreadable_neighbour_pad_makes_the_attempt_a_router_failure_never_a_neck(tmp_path):

    # the router adds the neck; its output board has the unreadable neighbour pad already (input copy edited)
    source = tmp_path / "pre"
    shutil.copytree(RUNG1, source)
    _make_neighbour_unreadable(source / BOARD_NAME)
    report = _run_rung1_from(tmp_path, source, [((151.55, 107.9), (150.8528, 107.9), 0.15)])
    attempt = report["attempts"][0]
    assert attempt["status"] != "PASS"
    assert attempt["cause"] == "router-failure"
    assert attempt["router_facts"]["under_width_check"]["ran"] is False
    assert "necked" not in attempt["router_facts"]["under_width_check"]


def _run_rung1_from(tmp_path, source: Path, segments):
    from test_rung_runner_necked_label import PASS_GATE, _router_adding

    name = "rung1_usbc_attiny1616"
    return rung_runner.run_rung(
        rung="neck-unreadable", backend_name="freerouting", board=source / f"{name}.kicad_pcb",
        rule_files=[source / f"{name}.kicad_pro"],
        plan=rung_runner.load_protocol()["backends"]["freerouting"]["plan"][:1],
        backend=rung_backends.with_under_width_check(_router_adding(segments)),
        gate=lambda board: dict(PASS_GATE), baseline={"rules": "pinned"}, tool_versions={},
        runs_root=tmp_path / "runs", repo_state=lambda: {"commit": "abc123", "dirty": False},
        protocol=rung_runner.load_protocol(), invariants=lambda board: {},
    )


def test_the_larger_of_the_two_nets_clearances_decides_whether_a_neck_is_forced(board):
    """Review BLOCKS 4: "c the larger of the two nets' class clearances". Power class width 0.2 mm and
    clearance 0.2 mm, Default (the GND neighbour U1.3, 0.2 mm away) clearance 0.25 mm. With the contact
    net's clearance alone the limit at U1.4 is 0.2 + 2 (0.2 - 0.2) = 0.2: 0.2 fits, the 0.15 mm track is
    unforced. With the larger, 0.25 mm: 0.2 + 2 (0.2 - 0.25) = 0.1 < 0.2, the neck is forced."""
    project_file = board.with_suffix(".kicad_pro")
    data = json.loads(project_file.read_text(encoding="utf-8"))
    for cls in data["net_settings"]["classes"]:
        if cls["name"] == "Power":
            cls["track_width"] = 0.2
        if cls["name"] == "Default":
            cls["clearance"] = 0.25
    write_layer.save_project(data, project_file)
    _add(board, [((151.55, 107.9), (150.8528, 107.9), 0.15)])
    (item,) = _classify(board)
    assert (item["neck_verdict"], item["neck_reason"]) == ("necked", "forced_neck")
    assert item["neck_pad_limit_mm"] == pytest.approx(0.1)
