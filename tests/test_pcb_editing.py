"""Tests for propose_remove_graphic_items/confirm_remove_graphic_items
(archived backlog P3-10), added to the placement toolset.

Uses a dedicated fixture (tests/fixtures/graphics_removal_board.kicad_pcb,
built entirely through kicad_tools' own PCB/SExp object model -- see
tests/fixtures/README.md for the same discipline this project already
follows for simple_led.kicad_pcb) rather than any hand-edited board file, per
CLAUDE.md's "never hand-edit KiCad files" rule.

Fixture layout (all board-level graphics, no footprints needed):
  - gr_rect  F.Cu  net VCC   (10,10)-(30,20)   "old hand-drawn VCC plane"
  - gr_rect  F.Cu  net GND   (25,15)-(45,25)   overlaps the VCC rect above
  - gr_poly  F.Cu  net GND   a small quad at (60-75, 60-72), no overlap
  - gr_line  F.Cu  net VCC   (5,80)-(25,80), no overlap
  - gr_circle B.Cu  no net    center (80,80) r=5 -- must survive F.Cu filters
  - gr_arc   F.SilkS no net   near (5-9, 3-5) -- must survive F.Cu filters
  - plus the 4 gr_line Edge.Cuts board-outline segments PCB.create() emits
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import write_layer
from kicad_mcp.toolsets import placement as placement_toolset
from tests import _kicad_faults as faults

FIXTURES = Path(__file__).parent / "fixtures"
GRAPHICS_FIXTURE = FIXTURES / "graphics_removal_board.kicad_pcb"


def _build_and_get_fns(mcp: MCPServer) -> dict:
    placement_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def board_copy(tmp_path) -> str:
    """A private, per-test copy of the graphics fixture -- tests mutate it,
    so they must never touch the checked-in original."""
    dest = tmp_path / "board.kicad_pcb"
    shutil.copy(GRAPHICS_FIXTURE, dest)
    return str(dest)


def _graphic_tag_counts(pcb_path: str) -> dict[str, int]:
    from kicad_tools.schema import PCB

    board = PCB.load(pcb_path)
    counts: dict[str, int] = {}
    for child in board._sexp.children:
        if child.is_atom:
            continue
        counts[child.tag] = counts.get(child.tag or "", 0) + 1
    return counts


def test_fixture_has_expected_starting_shape():
    counts = _graphic_tag_counts(str(GRAPHICS_FIXTURE))
    assert counts["gr_rect"] == 2
    assert counts["gr_poly"] == 1
    # 4 Edge.Cuts outline segments (from PCB.create) + 1 deliberately added.
    assert counts["gr_line"] == 5
    assert counts["gr_circle"] == 1
    assert counts["gr_arc"] == 1


def test_build_registers_new_tools_alongside_existing_ones():
    mcp = MCPServer("test")
    names = placement_toolset.build(mcp)
    assert "propose_remove_graphic_items" in names
    assert "confirm_remove_graphic_items" in names


def test_propose_rejects_empty_filter(board_copy):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["propose_remove_graphic_items"](board_copy, [{}])
    assert result["status"] == "error"
    assert "no criteria" in result["reason"]


def test_propose_rejects_unknown_filter_key(board_copy):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["propose_remove_graphic_items"](board_copy, [{"nope": "F.Cu"}])
    assert result["status"] == "error"


def test_propose_rejects_unknown_shape(board_copy):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["propose_remove_graphic_items"](board_copy, [{"shape": "gr_bogus"}])
    assert result["status"] == "error"


def test_propose_zero_match_is_complete_not_error(board_copy):
    """A filter matching zero items is a legitimate outcome per archived backlog
    P3-10's acceptance criteria -- status complete, removed_count 0, no
    revision_token, not an error."""
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["propose_remove_graphic_items"](board_copy, [{"net_name": "DOES_NOT_EXIST"}])
    assert result["status"] == "complete"
    assert result["removed_count"] == 0
    assert "revision_token" not in result


def test_propose_does_not_modify_the_board(board_copy):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    before = _graphic_tag_counts(board_copy)
    result = tools["propose_remove_graphic_items"](board_copy, [{"layer": "F.Cu", "net_name": "VCC"}])
    assert result["status"] == "pending_confirmation"
    after = _graphic_tag_counts(board_copy)
    assert before == after


def test_propose_matches_by_layer_and_net(board_copy):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["propose_remove_graphic_items"](board_copy, [{"layer": "F.Cu", "net_name": "VCC"}])
    assert result["status"] == "pending_confirmation"
    shapes = {item["shape"] for item in result["matched_items"]}
    assert shapes == {"gr_rect", "gr_line"}
    assert all(item["net_name"] == "VCC" for item in result["matched_items"])
    assert all(item["layer"] == "F.Cu" for item in result["matched_items"])


def test_propose_matches_by_shape_only(board_copy):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["propose_remove_graphic_items"](board_copy, [{"shape": "gr_poly"}])
    assert result["status"] == "pending_confirmation"
    assert result["matched_count"] == 1
    assert result["matched_items"][0]["shape"] == "gr_poly"
    assert result["matched_items"][0]["net_name"] == "GND"


def test_propose_matches_by_bbox_region(board_copy):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    # Region around the no-net B.Cu circle at (80, 80) r=5 (bbox 75-85 both
    # axes) -- kept clear of the GND gr_poly's bbox (60-75, 60-72) so only
    # the circle intersects.
    result = tools["propose_remove_graphic_items"](board_copy, [{"bbox": [77.0, 77.0, 90.0, 90.0]}])
    assert result["status"] == "pending_confirmation"
    assert result["matched_count"] == 1
    assert result["matched_items"][0]["shape"] == "gr_circle"


def test_propose_filters_are_ored_together(board_copy):
    """Two filters in one call should match the union of what each would
    match alone (never one-item-per-call looping)."""
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["propose_remove_graphic_items"](
        board_copy,
        [{"shape": "gr_poly"}, {"shape": "gr_circle"}],
    )
    assert result["status"] == "pending_confirmation"
    shapes = sorted(item["shape"] for item in result["matched_items"])
    assert shapes == ["gr_circle", "gr_poly"]


def test_silk_and_no_net_graphics_survive_copper_net_filter(board_copy):
    """A filter scoped to F.Cu/VCC must never touch the no-net B.Cu circle
    or the no-net F.SilkS arc."""
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    proposal = tools["propose_remove_graphic_items"](board_copy, [{"layer": "F.Cu", "net_name": "VCC"}])
    token = proposal["revision_token"]
    confirm = tools["confirm_remove_graphic_items"](board_copy, token)
    assert confirm["status"] == "confirmed", confirm

    after = _graphic_tag_counts(board_copy)
    assert after["gr_circle"] == 1  # untouched
    assert after["gr_arc"] == 1  # untouched
    assert after["gr_poly"] == 1  # untouched (net GND, not VCC)


def test_confirm_removes_exactly_the_matched_items_byte_safe(board_copy):
    """The core correctness claim: after confirm, the SAVED FILE reloaded
    FRESH (not the in-memory board object) has exactly the expected
    per-shape counts, and every other top-level node type is untouched --
    not just 'DRC still ran'."""
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    before = _graphic_tag_counts(board_copy)
    proposal = tools["propose_remove_graphic_items"](board_copy, [{"layer": "F.Cu", "net_name": "VCC"}])
    assert proposal["status"] == "pending_confirmation"
    token = proposal["revision_token"]

    confirm = tools["confirm_remove_graphic_items"](board_copy, token)
    assert confirm["status"] == "confirmed", confirm
    assert confirm["removed_count"] == 2

    # Reload FRESH from disk -- not the object the tool mutated in-process.
    from kicad_tools.schema import PCB

    reloaded = PCB.load(board_copy)
    assert reloaded is not None  # file parsed successfully: not corrupted

    after = _graphic_tag_counts(board_copy)
    assert after["gr_rect"] == before["gr_rect"] - 1  # the VCC gr_rect
    assert after["gr_line"] == before["gr_line"] - 1  # the VCC gr_line
    # Nothing else changed.
    for tag in ("gr_poly", "gr_circle", "gr_arc"):
        assert after[tag] == before[tag]

    # None of the removed uuids exist anywhere in the reloaded file.
    remaining_uuids = set()
    for child in reloaded._sexp.children:
        if child.is_atom or child.tag not in {"gr_rect", "gr_poly", "gr_line", "gr_circle", "gr_arc"}:
            continue
        u = child.find("uuid")
        if u is not None:
            remaining_uuids.add(u.get_string(0))
    assert not (remaining_uuids & set(confirm["removed_uuids"]))


def test_confirm_second_time_fails_token_consumed(board_copy):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    proposal = tools["propose_remove_graphic_items"](board_copy, [{"shape": "gr_poly"}])
    token = proposal["revision_token"]
    first = tools["confirm_remove_graphic_items"](board_copy, token)
    assert first["status"] == "confirmed"
    second = tools["confirm_remove_graphic_items"](board_copy, token)
    assert second["status"] == "error"


def test_confirm_unknown_token(board_copy):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    result = tools["confirm_remove_graphic_items"](board_copy, "does-not-exist")
    assert result["status"] == "error"


def test_confirm_rejects_mismatched_pcb_path(board_copy, tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)
    proposal = tools["propose_remove_graphic_items"](board_copy, [{"shape": "gr_poly"}])
    token = proposal["revision_token"]

    other = tmp_path / "other.kicad_pcb"
    shutil.copy(GRAPHICS_FIXTURE, other)

    result = tools["confirm_remove_graphic_items"](str(other), token)
    assert result["status"] == "error"
    assert "does not match" in result["reason"]

    # Token must still be usable against the correct path afterward -- a
    # caller mistake shouldn't burn the token.
    correct = tools["confirm_remove_graphic_items"](board_copy, token)
    assert correct["status"] == "confirmed"


def test_confirm_reports_error_if_board_changed_since_proposal(board_copy):
    """If the targeted item is gone by confirm time (board mutated out from
    under the proposal), confirm must report an error, not silently
    'succeed' at removing nothing or removing the wrong thing."""
    from kicad_tools.schema import PCB

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    proposal = tools["propose_remove_graphic_items"](board_copy, [{"shape": "gr_poly"}])
    token = proposal["revision_token"]

    # Simulate the board changing between propose and confirm using
    # kicad_tools' own API (never hand-editing the file) -- remove the very
    # node the proposal targeted, out of band.
    board = PCB.load(board_copy)
    target_uuid = proposal["matched_items"][0]["uuid"]
    for child in list(board._sexp.children):
        if not child.is_atom and child.tag == "gr_poly":
            u = child.find("uuid")
            if u is not None and u.get_string(0) == target_uuid:
                board._sexp.remove(child)
    write_layer.save_pcb(board, board_copy)

    result = tools["confirm_remove_graphic_items"](board_copy, token)
    assert result["status"] == "error"
    assert "no longer present" in result["reason"]


def test_confirm_detects_partial_removal_failure(board_copy, monkeypatch):
    """Corruption-resistance regression (explicitly required by archived backlog
    P3-10): simulate a kicad_tools/SExp bug where one matched node silently
    fails to remove from the live tree (SExp.remove returning False for one
    call, as its real implementation legitimately can if a node isn't a
    direct child). confirm_remove_graphic_items must detect the mismatch
    and report status 'error' -- not report 'confirmed' while the file
    still contains one of the items it claimed to remove."""
    from kicad_tools.sexp.parser import SExp

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    proposal = tools["propose_remove_graphic_items"](board_copy, [{"layer": "F.Cu", "net_name": "VCC"}])
    assert proposal["matched_count"] == 2
    token = proposal["revision_token"]

    real_remove = SExp.remove
    call_count = {"n": 0}

    def flaky_remove(self, child):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return False  # simulate the first removal silently failing
        return real_remove(self, child)

    monkeypatch.setattr(SExp, "remove", flaky_remove)

    before = _graphic_tag_counts(board_copy)
    result = tools["confirm_remove_graphic_items"](board_copy, token)
    assert result["status"] == "error"
    assert "could not be removed" in result["reason"]

    # And the file on disk must be untouched -- confirm must not have
    # saved a half-applied removal.
    after = _graphic_tag_counts(board_copy)
    assert after == before


def test_confirm_detects_count_drift_as_corruption(board_copy, monkeypatch):
    """If, after a real save+reload, the graphic counts don't match exactly
    what was requested (simulating a corrupted/under- or over-removed
    file), confirm must report status 'error' rather than 'confirmed' --
    this is the safety net a malformed-S-expression bug would trip."""
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    proposal = tools["propose_remove_graphic_items"](board_copy, [{"shape": "gr_poly"}])
    token = proposal["revision_token"]

    from kicad_mcp.toolsets import placement as pt

    real_counts = pt._top_level_tag_counts

    def lying_counts(board):
        counts = dict(real_counts(board))
        # Pretend an unrelated node type also changed, as a stand-in for
        # sibling-structure corruption a bad removal could cause.
        counts["gr_line"] = counts.get("gr_line", 0) + 1
        return counts

    # Only lie on the SECOND call (the post-reload "after" count) so the
    # "before" snapshot stays real -- otherwise the diff would look like
    # nothing changed.
    calls = {"n": 0}
    real = pt._top_level_tag_counts

    def selective_lying_counts(board):
        calls["n"] += 1
        if calls["n"] >= 2:
            return lying_counts(board)
        return real(board)

    monkeypatch.setattr(pt, "_top_level_tag_counts", selective_lying_counts)

    result = tools["confirm_remove_graphic_items"](board_copy, token)
    assert result["status"] == "error"
    assert "possible corruption" in result["reason"]


def test_real_drc_before_and_after_removal_file_stays_parseable(board_copy):
    """Non-negotiable per archived backlog P3-10: run the REAL kicad-cli DRC
    (via pcb_run_drc's underlying kicad_cli.run_drc, same call the
    verification toolset uses) before and after removal, on the actual
    fixture file, to prove removal doesn't corrupt the board -- not just
    that our own re-parse succeeded."""
    from kicad_mcp import kicad_cli

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp)

    before_report = kicad_cli.run_drc(Path(board_copy), severity_all=True)
    assert before_report is not None  # real kicad-cli parsed the file fine

    proposal = tools["propose_remove_graphic_items"](board_copy, [{"layer": "F.Cu", "net_name": "GND"}])
    assert proposal["status"] == "pending_confirmation"
    confirm = tools["confirm_remove_graphic_items"](board_copy, proposal["revision_token"])
    assert confirm["status"] == "confirmed", confirm

    after_report = kicad_cli.run_drc(Path(board_copy), severity_all=True)
    assert after_report is not None  # real kicad-cli still parses the file fine after removal

    # The removal's own wired-in post-check already ran kicad-cli too;
    # cross-check it agrees with this independent call.
    assert confirm["drc_after"]["violation_count"] == len(after_report.get("violations", []))


def test_malformed_pcb_file_fails_to_parse_not_silently_accepted(tmp_path):
    """Explicit negative-path test for the 'malformed S-expression' failure
    mode archived backlog P3-10 calls out: if a .kicad_pcb file is genuinely
    broken (simulated here purely as test setup, not via this project's own
    tool -- the tool itself only ever removes whole, balanced sexp nodes,
    which structurally cannot unbalance parens), kicad_tools.PCB.load must
    raise rather than silently return a half-parsed board, and pcb_run_drc's
    underlying kicad_cli.run_drc must fail loudly rather than pretending
    the board is clean."""
    from kicad_tools.schema import PCB

    broken = tmp_path / "broken.kicad_pcb"
    shutil.copyfile(GRAPHICS_FIXTURE, broken)
    # Truncate mid-file to simulate a corrupted write (unbalanced parens) --
    # this file is never touched by our tool; it's a synthetic corruption
    # used only to prove the verification path used by confirm_* actually
    # catches this class of failure rather than rubber-stamping it.
    faults.rewrite_bytes(broken, lambda data: data[: len(data) // 2])

    with pytest.raises(Exception):
        PCB.load(broken)

    from kicad_mcp import kicad_cli

    with pytest.raises(Exception):
        kicad_cli.run_drc(broken, severity_all=True)
