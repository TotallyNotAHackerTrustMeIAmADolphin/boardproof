"""Guards of issue 133's placement tools that a review found unpinned: each test here is red when
the one guard it names is removed (the mutant list is in the ticket's Answer)."""

from __future__ import annotations

import math

import pytest
from kicad_tools.sexp import SExp, parse_string

from boardproof import placement_anchor as pa
from boardproof import write_layer
from boardproof.toolsets import placement as placement_toolset
from tests.test_placement_anchor import (  # noqa: F401  (fixtures and raw readers)
    BOARD_W,
    _board,
    _corrupt_write,
    _fp,
    _inside,
    _parts_grid,
    _propose,
    _raw_at,
    _raw_courtyard,
    _raw_outline,
    _root,
    _tools,
)
from boardproof.sourcing.footprint_gen import generate_footprint


@pytest.fixture
def tools() -> dict:
    return _tools()


@pytest.fixture
def chips(tmp_path) -> dict:
    out = {}
    for size in ("0805", "1206"):
        path = tmp_path / f"{size}.kicad_mod"
        assert generate_footprint("chip", str(path), size=size)["status"] == "ok"
        out[size] = path
    return out


def _part(ref, box, side="front", through_hole=False, locked=False):
    return pa.Part(ref, None, (0.0, 0.0), 0.0, locked, side, through_hole, box, "courtyard")


# --------------------------------------------------------------------------- the optimizer honours the lock


def _fake_optimizer(monkeypatch, delta):
    from boardproof import kicad_cli
    from boardproof.routers import kicad_placer_tools, kicad_routing_tools

    monkeypatch.setattr(kicad_placer_tools, "run_placement_repair", lambda *a, **k: {"output_produced": True})
    monkeypatch.setattr(kicad_placer_tools, "placement_delta", lambda *a, **k: dict(delta))
    monkeypatch.setattr(kicad_routing_tools, "run_kicad_routing_tools", lambda *a, **k: {})
    monkeypatch.setattr(kicad_cli, "run_drc", lambda *a, **k: {"violations": [], "unconnected_items": []})
    monkeypatch.setattr(kicad_cli, "ensure_kicad_cli_on_path", lambda *a, **k: None)


def test_a_locked_part_survives_optimizer_propose_and_confirm(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("R1", "1206", 10, 10), ("R2", "1206", 30, 10)])
    tools["lock_footprints"](str(pcb), ["R1"])
    before = _raw_at(pcb, "R1")
    _fake_optimizer(monkeypatch, {"R1": (50.0, 20.0, 0.0), "R2": (60.0, 20.0, 0.0)})
    proposal = tools["propose_placement_optimization"](str(pcb))
    assert proposal["status"] == "pending_confirmation", proposal
    assert proposal["locked_skipped"] == ["R1"] and set(proposal["proposed_positions"]) == {"R2"}
    assert tools["confirm_placement_optimization"](proposal["revision_token"])["status"] == "confirmed"
    assert _raw_at(pcb, "R1") == before
    assert _raw_at(pcb, "R2")[:2] != (30.0 + 98.5, 10.0 + 75.0)  # the unlocked one did move


def test_an_optimizer_proposal_for_a_part_locked_since_is_refused_and_keeps_its_token(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("R1", "1206", 10, 10), ("R2", "1206", 30, 10)])
    _fake_optimizer(monkeypatch, {"R1": (50.0, 20.0, 0.0)})
    proposal = tools["propose_placement_optimization"](str(pcb))
    assert proposal["status"] == "pending_confirmation", proposal
    tools["lock_footprints"](str(pcb), ["R1"])
    before = pcb.read_bytes()
    refused = tools["confirm_placement_optimization"](proposal["revision_token"])
    assert refused["status"] == "error" and refused["code"] == "locked_footprint", refused
    assert pcb.read_bytes() == before
    tools["lock_footprints"](str(pcb), ["R1"], locked=False)
    assert tools["confirm_placement_optimization"](proposal["revision_token"])["status"] == "confirmed"


def test_an_optimizer_that_only_moves_locked_parts_proposes_nothing(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("R1", "1206", 10, 10)])
    tools["lock_footprints"](str(pcb), ["R1"])
    _fake_optimizer(monkeypatch, {"R1": (50.0, 20.0, 0.0)})
    result = tools["propose_placement_optimization"](str(pcb))
    assert result.get("changed") is False and "revision_token" not in result, result


def test_the_optimizer_descriptions_say_so():
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer("t")
    placement_toolset.build(mcp)
    text = {t.name: t.description for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert "locked_skipped" in text["propose_placement_optimization"] and "never a locked footprint" in text["propose_placement_optimization"]
    assert "locked since" in text["confirm_placement_optimization"]
    assert "propose_placement_optimization" in text["lock_footprints"] and "confirm_placement_optimization" in text["lock_footprints"]


# --------------------------------------------------------------------------- the lock fails closed


def test_an_unreadable_lock_state_refuses_instead_of_meaning_no_locks(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("R1", "1206", 10, 10), ("R2", "1206", 30, 14), ("R3", "1206", 50, 20)])
    before = pcb.read_bytes()

    def boom(*a, **k):
        raise OSError("unreadable")

    monkeypatch.setattr(write_layer, "load", boom)
    calls = {
        "place_footprints": (str(pcb), [{"reference": "R1", "x": 1.0, "y": 1.0}]),
        "align_footprints": (str(pcb), ["R1", "R2"], "y", "center"),
        "distribute_footprints": (str(pcb), ["R1", "R2", "R3"], "x"),
        "snap_footprints_to_grid": (str(pcb), ["R1"], 2.54),
        "pack_footprints_densely": (str(pcb), ["R1", "R2"]),
        "propose_placement_optimization": (str(pcb),),
    }
    for name, args in calls.items():
        result = tools[name](*args)
        assert result["status"] == "error" and result["code"] == "lock_state_unreadable", (name, result)
    monkeypatch.undo()
    assert pcb.read_bytes() == before


# --------------------------------------------------------------------------- pack and collisions, unit level


def test_pack_keeps_spacing_from_blocked_boxes():
    region, blocked = (0.0, 0.0, 100.0, 100.0), [((10.0, 0.0, 20.0, 10.0), {"front"})]
    centres, unplaced = pa.pack([("A", 8.0, 8.0)], region, blocked, {"A": {"front"}}, 5.0)
    assert not unplaced
    cx, cy = centres["A"]
    box = (cx - 4, cy - 4, cx + 4, cy + 4)
    gap_x = max(10.0 - box[2], box[0] - 20.0)
    gap_y = max(0.0 - box[3], box[1] - 10.0)
    assert max(gap_x, gap_y) >= 5.0 - 1e-9, box


def test_pack_ignores_a_blocked_box_on_the_other_side_and_respects_one_on_the_same_side():
    region = (0.0, 0.0, 100.0, 100.0)
    blocked = [((0.0, 0.0, 30.0, 30.0), {"front"})]
    back, _ = pa.pack([("B", 5.0, 5.0)], region, blocked, {"B": {"back"}}, 2.0)
    assert back["B"] == (2.5, 2.5)  # the back part may sit under the front one
    front, _ = pa.pack([("F", 5.0, 5.0)], region, blocked, {"F": {"front"}}, 2.0)
    assert front["F"] != (2.5, 2.5)
    both, _ = pa.pack([("T", 5.0, 5.0)], region, blocked, {"T": {"front", "back"}}, 2.0)  # through-hole
    assert both["T"] != (2.5, 2.5)


def test_pack_ignores_a_box_placed_before_on_the_other_side():
    centres, _ = pa.pack([("A", 5.0, 5.0), ("B", 5.0, 5.0)], (0.0, 0.0, 50.0, 50.0), [],
                         {"A": {"front"}, "B": {"back"}}, 2.0)
    assert centres["A"] == centres["B"] == (2.5, 2.5)
    centres, _ = pa.pack([("A", 5.0, 5.0), ("B", 5.0, 5.0)], (0.0, 0.0, 50.0, 50.0), [],
                         {"A": {"front"}, "B": {"front"}}, 2.0)
    assert centres["A"] != centres["B"]


def test_collisions_meet_only_on_a_shared_side():
    box = (0.0, 0.0, 5.0, 5.0)
    front, back, thru = _part("F", box), _part("B", box, side="back"), _part("T", box, through_hole=True)
    assert pa.collisions({"F": box}, [front, back]) == []
    assert pa.collisions({"F": box}, [front, _part("G", box)]) == [{"between": ["F", "G"]}]
    assert pa.collisions({"F": box}, [front, thru]) == [{"between": ["F", "T"]}]
    assert pa.collisions({"B": box}, [back, thru]) == [{"between": ["B", "T"]}]
    assert pa.collisions({"B": box}, [back, _part("F2", box)]) == []


# --------------------------------------------------------------------------- is_locked, set_locked


@pytest.mark.parametrize("text, expected", [
    ('(footprint "x" (locked yes))', True),
    ('(footprint "x" (locked no))', False),
    ('(footprint "x" locked)', True),
    ('(footprint "x" (layer "F.Cu"))', False),
])
def test_is_locked_reads_every_form(text, expected):
    assert pa.is_locked(parse_string(text)) is expected


def test_set_locked_inserts_right_after_the_library_id_and_replaces_older_forms():
    node = parse_string('(footprint "x" (layer "F.Cu") locked)')
    pa.set_locked(node, True)
    assert node.children[0].value == "x"
    assert node.children[1].name == "locked" and node.children[1].values == ["yes"]
    assert sum(1 for c in node.children if (c.is_atom and c.value == "locked") or (not c.is_atom and c.name == "locked")) == 1
    pa.set_locked(node, False)
    assert not pa.is_locked(node) and [c.name for c in node.children if not c.is_atom] == ["layer"]


def test_a_lock_written_to_a_board_sits_after_the_library_id(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "1206", 10, 10)])
    tools["lock_footprints"](str(pcb), ["R1"])
    node = _fp(_root(pcb), "R1")
    assert node.children[1].name == "locked"


# --------------------------------------------------------------------------- autoplacement argument and lock guards


def test_propose_autoplacement_with_every_part_locked_refuses(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "1206", 10, 10)])
    tools["lock_footprints"](str(pcb), ["R1"])
    result = tools["propose_autoplacement"](str(pcb), ["R1"])
    assert result["status"] == "error" and result["code"] == "all_locked", result


@pytest.mark.parametrize("kwargs", [
    {"edge_clearance_mm": -1.0}, {"spacing_mm": -1.0},
    {"edge_clearance_mm": math.nan}, {"spacing_mm": math.nan},
    {"edge_clearance_mm": math.inf}, {"spacing_mm": math.inf},
    {"edge_clearance_mm": True},
])
def test_propose_autoplacement_refuses_bad_numbers(tmp_path, chips, tools, kwargs):
    pcb = _board(tmp_path, chips, [("R1", "1206", 10, 10)])
    result = tools["propose_autoplacement"](str(pcb), ["R1"], **kwargs)
    assert result["status"] == "error" and result["code"] == "invalid_argument", result


def test_spacing_between_proposed_parts_is_at_least_spacing_mm(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, _parts_grid(8))
    proposal = _propose(tools, pcb, spacing_mm=5.0)
    assert tools["confirm_autoplacement"](proposal["revision_token"])["status"] == "confirmed"
    boxes = [_raw_courtyard(pcb, f"R{i}") for i in range(8)]
    for i, a in enumerate(boxes):
        assert _inside(a, _raw_outline(pcb))
        for b in boxes[i + 1:]:
            gap = max(b[0] - a[2], a[0] - b[2], b[1] - a[3], a[1] - b[3])
            assert gap >= 5.0 - 1e-6, (a, b)


def test_a_proposal_for_a_part_rotated_since_is_stale(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "1206", 50, 30), ("R2", "1206", 20, 20)])
    proposal = _propose(tools, pcb, ["R1"])
    assert tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 50.0, "y": 30.0, "rotation": 90.0}])["status"] == "ok"
    before = pcb.read_bytes()
    refused = tools["confirm_autoplacement"](proposal["revision_token"])
    assert refused["status"] == "error" and refused["code"] == "proposal_stale", refused
    assert "another rotation" in refused["reason"]
    assert pcb.read_bytes() == before


# --------------------------------------------------------------------------- edge planning guards


def test_a_reference_in_two_specs_is_a_duplicate(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    before = pcb.read_bytes()
    result = tools["place_footprints_at_edge"](str(pcb), [
        {"reference": "J1", "edge": "left"}, {"reference": "J1", "edge": "right"}])
    assert result["status"] == "error" and result["problems"][0]["code"] == "duplicate_reference", result
    assert pcb.read_bytes() == before


def test_two_footprints_with_one_reference_are_ambiguous(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30), ("J1", "1206", 60, 30)])
    before = pcb.read_bytes()
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left"}])
    assert result["status"] == "error" and result["problems"][0]["code"] == "ambiguous_reference", result
    assert pcb.read_bytes() == before
    result = tools["lock_footprints"](str(pcb), ["J1"])
    assert result["code"] == "ambiguous_reference", result
    result = tools["propose_autoplacement"](str(pcb), ["J1"])
    assert result["code"] == "ambiguous_reference", result


def test_a_single_gr_rect_outline_is_accepted(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    root = write_layer.load(pcb)
    lines = [n for n in root.children if not n.is_atom and n.name == "gr_line"]
    x0, y0, x1, y1 = _raw_outline(pcb)
    for n in lines:
        root.children.remove(n)
    root.children.append(SExp.list(
        "gr_rect", SExp.list("start", x0, y0), SExp.list("end", x1, y1),
        SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default")),
        SExp.list("fill", write_layer.symbol("no")), SExp.list("layer", "Edge.Cuts")))
    write_layer.save(root, pcb)
    rect, problem = pa.outline_rect(_root(pcb))
    assert problem is None and rect.box == pytest.approx((x0, y0, x1, y1))
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "right", "offset_mm": 2.0}])
    assert result["status"] == "ok", result
    assert x1 - _raw_courtyard(pcb, "J1")[2] == pytest.approx(2.0, abs=1e-6)
    assert BOARD_W == x1 - x0


# --------------------------------------------------------------------------- each self-check layer on its own


def _swap_set_at(monkeypatch, after):
    original = pa._set_at

    def wrong(node, x, y):
        original(node, x, y)
        after(node, x, y)

    monkeypatch.setattr(pa, "_set_at", wrong)


def test_tree_compare_alone_fires_when_a_neighbouring_node_is_corrupted(tmp_path, chips, tools, monkeypatch):
    """The position, angle and lock checks cannot see this: a node other than the footprint changed."""
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    before = pcb.read_bytes()
    _corrupt_write(monkeypatch, lambda text: text.replace('(generator "kicad_tools")', '(generator "other")', 1))
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left", "offset_mm": 3.0}])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert pcb.read_bytes() == before


def test_position_check_alone_fires_when_the_edit_itself_is_wrong(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    before = pcb.read_bytes()
    _swap_set_at(monkeypatch, lambda node, x, y: write_layer.set_number(node.find("at"), 0, x + 0.5))
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left", "offset_mm": 3.0}])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert pcb.read_bytes() == before


def test_angle_check_alone_fires_when_the_edit_turns_the_part(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30, 90.0)])
    before = pcb.read_bytes()
    _swap_set_at(monkeypatch, lambda node, x, y: write_layer.set_number(node.find("at"), 2, 0))
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left", "offset_mm": 3.0}])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert "angle or lock" in " ".join(result["mismatches"])
    assert pcb.read_bytes() == before


def test_lock_check_alone_fires_when_the_edit_locks_the_part(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    before = pcb.read_bytes()
    real_save = pa.save_footprint_edits

    def locking(path, edit, check):
        def both(root, parts):
            nodes = edit(root, parts)
            for node in nodes:
                pa.set_locked(node, True)
            return nodes

        return real_save(path, both, check)

    monkeypatch.setattr(pa, "save_footprint_edits", locking)
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left", "offset_mm": 3.0}])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert "angle or lock" in " ".join(result["mismatches"])
    assert pcb.read_bytes() == before


def test_gap_check_alone_fires_when_the_plan_is_wrong(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    before = pcb.read_bytes()
    real = pa.edge_target

    def off(box, rect, edge, offset, along):
        dx, dy = real(box, rect, edge, offset, along)
        return dx + 0.5, dy

    monkeypatch.setattr(pa, "edge_target", off)
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left", "offset_mm": 3.0}])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert "from the left edge" in " ".join(result["mismatches"])
    assert pcb.read_bytes() == before


def test_lock_state_check_alone_fires_when_the_edit_does_not_lock(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    before = pcb.read_bytes()
    monkeypatch.setattr(pa, "set_locked", lambda node, locked: None)
    result = tools["lock_footprints"](str(pcb), ["J1"])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert pcb.read_bytes() == before


def test_position_check_alone_fires_when_confirm_autoplacement_writes_a_wrong_position(tmp_path, chips, tools, monkeypatch):
    """No gap check here (only the edge tool has one): the position check of write_positions is the only layer
    that can see an edit that is wrong in memory and written faithfully."""
    pcb = _board(tmp_path, chips, [("R1", "1206", 50, 30)])
    proposal = _propose(tools, pcb)
    before = pcb.read_bytes()
    _swap_set_at(monkeypatch, lambda node, x, y: write_layer.set_number(node.find("at"), 0, x + 0.5))
    result = tools["confirm_autoplacement"](proposal["revision_token"])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert "intended" in " ".join(result["mismatches"])
    assert pcb.read_bytes() == before
