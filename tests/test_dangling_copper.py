"""Issue 246: propose/confirm removal of dangling vias and track stubs (what KiCad's DRC calls
``track_dangling`` and ``via_dangling``) after a router import.

Fixtures are built through the write layer on tmp copies of the imported rung-1 board: copper is added far
away from every pad (x 1 to 5 mm; the footprints sit near x 190), so a segment there is dangling by
construction. The detection is KiCad's own (kicad-cli DRC); the connectivity proof is the per-net
unconnected count of a second DRC run on the written copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from boardproof import checkpoint, dangling_copper as dc
from boardproof import write_layer
from tests.test_orphan_footprints import _cli, _digest, make_board

pytestmark = pytest.mark.skipif(not _cli(), reason="kicad-cli not installed")

NET = "+3V3"
U = "aaaaaaaa-0000-4000-8000-0000000000{:02d}"


def _seg(n: int, a, b, layer="F.Cu", locked=False, net=NET) -> SExp:
    kids = [
        SExp.list("start", write_layer.number(a[0]), write_layer.number(a[1])),
        SExp.list("end", write_layer.number(b[0]), write_layer.number(b[1])),
        SExp.list("width", write_layer.number(0.2)),
        SExp.list("layer", write_layer.string(layer)),
    ]
    if locked:
        kids.append(SExp.list("locked", write_layer.symbol("yes")))
    kids += [SExp.list("net", write_layer.string(net)), SExp.list("uuid", write_layer.string(U.format(n)))]
    return SExp.list("segment", *kids)


def _via(n: int, at, net=NET) -> SExp:
    return SExp.list(
        "via",
        SExp.list("at", write_layer.number(at[0]), write_layer.number(at[1])),
        SExp.list("size", write_layer.number(0.6)),
        SExp.list("drill", write_layer.number(0.3)),
        SExp.list("layers", write_layer.string("F.Cu"), write_layer.string("B.Cu")),
        SExp.list("net", write_layer.string(net)),
        SExp.list("uuid", write_layer.string(U.format(n))),
    )


def _add(pcb: Path, *nodes: SExp) -> None:
    root = write_layer.load(pcb)
    for node in nodes:
        root.append(node)
    write_layer.save(root, pcb)


def _uuids(pcb: Path) -> set[str]:
    return {str(n.find("uuid").values[0]) for n in write_layer.load(pcb).children if n.name in ("segment", "via", "arc")}


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    pcb = make_board(tmp_path)
    # 1: a plain stub, 2: a dangling via, 3: a locked stub, 4 to 6: F.Cu track, via, B.Cu track in a row
    _add(
        pcb,
        # one floating item per net: KiCad names the endpoints of the remaining pairs itself, so a net with several
        # floating clusters would see a pair renamed (and refused) when only some of them go
        _seg(1, (1, 1), (2, 1)),
        _via(2, (1, 3), "GND"),
        _seg(3, (1, 5), (2, 5), locked=True, net="VBUS"),
        _seg(4, (10, 1), (11, 1), net="/CC1"),
        _via(5, (11, 1), "/CC1"),
        _seg(6, (11, 1), (12, 1), layer="B.Cu", net="/CC1"),
    )
    return pcb


def _propose(pcb: Path, tmp_path: Path, **kw) -> dict:
    return dc.propose_dangling_copper_removal(pcb, output_dir=tmp_path / "out", **kw)


def _listed(result: dict) -> dict[str, dict]:
    return {c["uuid"]: c for c in result["dangling_removed"]}


def test_dry_run_lists_each_candidate_and_writes_nothing(board: Path, tmp_path: Path) -> None:
    before = _digest(tmp_path / "first")
    result = _propose(board, tmp_path)
    assert result["status"] == "proposed" and result["revision_token"], result
    listed = _listed(result)
    assert {U.format(1), U.format(2), U.format(4), U.format(6)} <= set(listed), listed.keys()
    stub = listed[U.format(1)]
    assert stub["kind"] == "track" and stub["net"] == NET and stub["layer"] == "F.Cu" and stub["start"] == [1.0, 1.0]
    assert stub["end"] == [2.0, 1.0] and "unconnected end" in stub["reason"]
    via = listed[U.format(2)]
    assert via["kind"] == "via" and via["at"] == [1.0, 3.0] and via["layers"] == ["F.Cu", "B.Cu"]
    assert _digest(tmp_path / "first") == before and not (tmp_path / "out").exists()


def test_a_via_that_joins_two_tracks_goes_only_with_the_whole_chain(board: Path, tmp_path: Path) -> None:
    """The via is not dangling while both tracks are there (round 1 does not name it); once the dangling
    tracks are gone it has a free end, so the plan lists it in round 2 with them and not alone."""
    result = _propose(board, tmp_path)
    listed = _listed(result)
    assert listed[U.format(4)]["round"] == 1 and listed[U.format(6)]["round"] == 1
    assert listed[U.format(5)]["round"] == 2 and result["rounds"] == 2


def test_locked_copper_is_kept_with_its_reason(board: Path, tmp_path: Path) -> None:
    result = _propose(board, tmp_path)
    assert U.format(3) not in _listed(result)
    kept = {c["uuid"]: c for c in result["kept"]}
    assert "locked" in kept[U.format(3)]["reason"]
    proposal = result
    done = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert done["status"] == "removed", done
    assert U.format(3) in _uuids(tmp_path / "out" / board.name)


def test_confirm_removes_exactly_the_listed_items_and_leaves_the_source(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    before = _digest(tmp_path / "first")
    done = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert done["status"] == "removed" and done["output_pcb"] == str(tmp_path / "out" / board.name)
    assert _digest(tmp_path / "first") == before
    gone = {c["uuid"] for c in proposal["dangling_removed"]}
    assert _uuids(board) - _uuids(tmp_path / "out" / board.name) == gone
    assert "routed" not in done["note"].lower() or "not a routing" in done["note"].lower()
    assert done["dangling_after"] == 1  # the locked stub is still dangling; the chain is gone
    assert U.format(3) in _uuids(tmp_path / "out" / board.name) and U.format(5) not in _uuids(tmp_path / "out" / board.name)


def test_nothing_to_do_issues_no_token(tmp_path: Path) -> None:
    result = _propose(make_board(tmp_path), tmp_path)
    assert result["status"] == "nothing_to_do" and result["revision_token"] is None, result


def test_drift_after_proposal_is_refused(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    _add(board, _seg(9, (20, 1), (21, 1)))
    result = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert result["status"] == "error" and "changed since the proposal" in result["reason"] and not (tmp_path / "out").exists()


def test_ignored_dangling_checks_refuse(board: Path, tmp_path: Path) -> None:
    def drc(path, *, refill_zones=False):
        report = dc.kicad_cli.run_drc(path, refill_zones=refill_zones)
        report["ignored_checks"] = [*report["ignored_checks"], {"key": "track_dangling", "description": "x"}]
        return report

    result = _propose(board, tmp_path, drc=drc)
    assert result["status"] == "blocked" and "ignore" in result["reason"], result


def test_unreadable_net_form_blocks(board: Path, tmp_path: Path, monkeypatch) -> None:
    """A net form the shared reader does not know fails closed (issue 244): no token, no DRC run."""

    def unknown(node, table=None):
        raise dc.RawGeometryError("net node of an unknown form: (net 'a' 'b')")

    monkeypatch.setattr(dc, "net_name", unknown)
    result = _propose(board, tmp_path, drc=lambda *a, **k: pytest.fail("DRC ran on an unreadable board"))
    assert result["status"] == "blocked" and result["revision_token"] is None, result


def test_corrupted_write_that_drops_an_unlisted_item_is_caught(board: Path, tmp_path: Path, monkeypatch) -> None:
    """Checklist item 14: the write also drops a locked stub the plan kept; the reload check fires."""
    proposal = _propose(board, tmp_path)
    before = _digest(tmp_path / "first")
    real_save = write_layer.save

    def corrupt(root, path, **kw):
        root.children[:] = [c for c in root.children if not (c.name == "segment" and str(c.find("uuid").values[0]) == U.format(3))]
        return real_save(root, path, **kw)

    monkeypatch.setattr(write_layer, "save", corrupt)
    result = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert result.get("ok") is False and result["code"] == checkpoint.WRITE_NOT_VERIFIED, result
    assert not (tmp_path / "out").exists() and _digest(tmp_path / "first") == before


def test_corrupted_write_that_removes_nothing_is_caught(board: Path, tmp_path: Path, monkeypatch) -> None:
    proposal = _propose(board, tmp_path)
    real = write_layer.save

    def save(root, path, **kw):  # the planning rounds still write their scratch copies; the real write does not
        return real(root, path, **kw) if ".dangling-round-" in str(path) else Path(path)

    monkeypatch.setattr(write_layer, "save", save)
    result = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert result.get("ok") is False and result["code"] == checkpoint.WRITE_NOT_VERIFIED, result


def test_a_lost_connection_in_the_second_drc_run_is_caught(board: Path, tmp_path: Path, monkeypatch) -> None:
    """The connectivity line: the DRC run on the written copy shows one more unconnected pair on a net."""
    proposal = _propose(board, tmp_path)
    real = dc.kicad_cli.run_drc

    def drc(path, *, refill_zones=False):
        report = real(path, refill_zones=refill_zones)
        if ".dangling-round-" not in str(path) and U.format(1) not in _uuids(Path(path)):  # the verify run only
            first = report["unconnected_items"][0]
            fake = {**first, "items": [{**i, "uuid": f"ffffffff-0000-4000-8000-{k:012d}"} for k, i in enumerate(first["items"])]}
            report["unconnected_items"] = [*report["unconnected_items"], fake]
        return report

    monkeypatch.setattr(dc.kicad_cli, "run_drc", drc)
    result = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert result.get("ok") is False and result["code"] == checkpoint.WRITE_NOT_VERIFIED, result
    assert "unconnected" in result["reason"] and not (tmp_path / "out").exists()


def _via_only_link(pcb: Path) -> None:
    """Two F.Cu tracks from two pads of +3V3 that both end under one via: the via is their only link, and
    KiCad flags it as via_dangling (it is connected on one layer only)."""
    _add(
        pcb,
        _seg(21, (192.725, 90), (200, 90)),
        _seg(22, (207.725, 90), (200.5, 90)),
        _via(23, (200.25, 90)),
    )


def test_a_via_that_is_the_only_link_of_two_pads_is_not_removed_with_a_stub(tmp_path: Path) -> None:
    """Review of 246, BLOCKS 1: a floating stub's vanished unconnected pair must not hide the pair the via's removal opens."""
    pcb = make_board(tmp_path)
    _via_only_link(pcb)
    _add(pcb, _seg(24, (1, 1), (2, 1)))
    proposal = _propose(pcb, tmp_path)
    assert {U.format(23), U.format(24)} <= set(_listed(proposal)), proposal
    result = dc.confirm_dangling_copper_removal(pcb, proposal["revision_token"])
    assert result.get("ok") is False and result["code"] == checkpoint.WRITE_NOT_VERIFIED, result
    assert "unconnected" in result["reason"] and not (tmp_path / "out").exists()


def test_the_same_via_alone_is_refused_too(tmp_path: Path) -> None:
    pcb = make_board(tmp_path)
    _via_only_link(pcb)
    proposal = _propose(pcb, tmp_path)
    result = dc.confirm_dangling_copper_removal(pcb, proposal["revision_token"])
    assert result.get("ok") is False and result["code"] == checkpoint.WRITE_NOT_VERIFIED, result


def _edit_end(pcb: Path, n: int, at) -> None:
    """Test-local edit through the write layer: move the end of track ``n``."""
    root = write_layer.load(pcb)
    node = next(c for c in root.children if not c.is_atom and c.name == "segment" and str(c.find("uuid").values[0]) == U.format(n))
    write_layer.set_number(node.find("end"), 0, at[0])
    write_layer.set_number(node.find("end"), 1, at[1])
    write_layer.save(root, pcb)


def test_an_item_edited_under_the_same_uuid_after_the_proposal_is_refused(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    _edit_end(board, 1, (3, 1))  # still a floating stub with the same uuid: the list is the same, the item is not (the token binds the project hash)
    result = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert result["status"] == "error" and "changed since the proposal" in result["reason"], result
    assert not (tmp_path / "out").exists()


def test_a_group_member_is_kept_with_its_reason(board: Path, tmp_path: Path) -> None:
    """A group names its members by uuid; removing one would leave a dangling reference, so it stays."""
    _add(board, SExp.list("group", write_layer.string("g"), SExp.list("uuid", write_layer.string(U.format(40))), SExp.list("members", write_layer.string(U.format(1)))))
    result = _propose(board, tmp_path)
    assert U.format(1) not in _listed(result)
    assert "group member" in {c["uuid"]: c for c in result["kept"]}[U.format(1)]["reason"]


def _arc(n: int, start, mid, end, net=NET) -> SExp:
    return SExp.list(
        "arc",
        SExp.list("start", write_layer.number(start[0]), write_layer.number(start[1])),
        SExp.list("mid", write_layer.number(mid[0]), write_layer.number(mid[1])),
        SExp.list("end", write_layer.number(end[0]), write_layer.number(end[1])),
        SExp.list("width", write_layer.number(0.2)),
        SExp.list("layer", write_layer.string("F.Cu")),
        SExp.list("net", write_layer.string(net)),
        SExp.list("uuid", write_layer.string(U.format(n))),
    )


def test_a_floating_arc_track_is_listed_and_removed(board: Path, tmp_path: Path) -> None:
    _add(board, _arc(50, (30, 1), (31, 2), (32, 1), net="/UPDI"))
    proposal = _propose(board, tmp_path)
    assert _listed(proposal)[U.format(50)]["reason"]
    done = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert done["status"] == "removed" and U.format(50) not in _uuids(tmp_path / "out" / board.name), done


def test_a_blind_via_with_one_free_side_is_listed_and_removed(board: Path, tmp_path: Path) -> None:
    via = _via(51, (40, 1), "/LED_A")
    write_layer.set_string(via.find("layers"), 1, "In1.Cu")
    _add(board, via)
    proposal = _propose(board, tmp_path)
    assert U.format(51) in _listed(proposal), proposal
    done = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert done["status"] == "removed" and U.format(51) not in _uuids(tmp_path / "out" / board.name), done


def test_a_dru_rule_cannot_hide_the_copper_from_the_plan(board: Path, tmp_path: Path) -> None:
    """A test-local .kicad_dru (the one tolerated hand-written file) that mutes tracks and vias: the severity
    of the two checks lives in the project file, so the plan either still lists the stub or says blocked."""
    rules = b"(version 1)\n(rule \"mute\" (condition \"A.Type == 'Track' || A.Type == 'Via'\") (constraint disallow track) (severity ignore))\n"
    write_layer.save_bytes(rules, board.parent / f"{board.stem}.kicad_dru", verify=lambda path: None)
    result = _propose(board, tmp_path)
    assert result["status"] in ("proposed", "blocked"), result
    if result["status"] == "proposed":
        assert U.format(1) in _listed(result)
    else:
        assert "ignore" in result["reason"]


def test_a_chain_longer_than_the_rounds_is_flagged_truncated_and_confirm_refuses(board: Path, tmp_path: Path, monkeypatch) -> None:
    """The chain 4/5/6 needs two rounds; with one round allowed the proposal says so and the self-check refuses."""
    monkeypatch.setattr(dc, "MAX_ROUNDS", 1)
    proposal = _propose(board, tmp_path)
    assert proposal["truncated"] is True and U.format(5) not in _listed(proposal), proposal
    done = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    assert done.get("ok") is False and done["code"] == checkpoint.WRITE_NOT_VERIFIED, done
    assert not (tmp_path / "out").exists()


def test_a_chain_that_needs_exactly_the_allowed_rounds_is_not_truncated(board: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dc, "MAX_ROUNDS", 2)  # the chain 4/5/6 needs two rounds
    proposal = _propose(board, tmp_path)
    assert proposal["rounds"] == 2 and proposal["truncated"] is False and U.format(5) in _listed(proposal), proposal
    assert dc.confirm_dangling_copper_removal(board, proposal["revision_token"])["status"] == "removed"


def test_a_complete_plan_is_not_flagged_truncated(board: Path, tmp_path: Path) -> None:
    assert _propose(board, tmp_path)["truncated"] is False


def test_a_via_in_a_pad_is_removed_or_refused_never_wrongly_removed(board: Path, tmp_path: Path) -> None:
    """A via at the centre of a GND pad (footprint at 178.5,90 angle 0, pad at +0.95,0: raw S-expressions): KiCad may
    call it via_dangling (one layer only). Whatever it says, a removal must pass the self-check or be refused."""
    _add(board, _via(70, (179.45, 90), "GND"))
    proposal = _propose(board, tmp_path)
    if U.format(70) not in _listed(proposal):
        return  # KiCad does not call it dangling: nothing to remove
    done = dc.confirm_dangling_copper_removal(board, proposal["revision_token"])
    if done["status"] == "removed":
        assert U.format(70) not in _uuids(tmp_path / "out" / board.name)
    else:
        assert done["code"] == checkpoint.WRITE_NOT_VERIFIED and not (tmp_path / "out").exists(), done


def test_three_extra_stubs_on_one_net_are_removed_with_no_new_pair(tmp_path: Path) -> None:
    pcb = make_board(tmp_path)
    _add(pcb, _seg(60, (20, 1), (21, 1)), _seg(61, (20, 3), (21, 3)), _seg(62, (20, 5), (21, 5)))
    proposal = _propose(pcb, tmp_path)
    assert {U.format(60), U.format(61), U.format(62)} <= set(_listed(proposal)), proposal
    done = dc.confirm_dangling_copper_removal(pcb, proposal["revision_token"])
    assert done["status"] == "removed", done


def test_the_only_link_via_is_refused_with_two_stubs_on_its_net(tmp_path: Path) -> None:
    pcb = make_board(tmp_path)
    _via_only_link(pcb)
    _add(pcb, _seg(24, (1, 1), (2, 1)), _seg(25, (1, 3), (2, 3)))
    proposal = _propose(pcb, tmp_path)
    assert {U.format(23), U.format(24), U.format(25)} <= set(_listed(proposal)), proposal
    done = dc.confirm_dangling_copper_removal(pcb, proposal["revision_token"])
    assert done.get("ok") is False and done["code"] == checkpoint.WRITE_NOT_VERIFIED, done
    assert not (tmp_path / "out").exists()
