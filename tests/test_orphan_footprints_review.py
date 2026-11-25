"""Review of build ticket 227 (395cef7): zones inside footprints, the other-node self-check, group members,
the stale re-plan and the netlist_exporter seam. Shares the fixture of test_orphan_footprints.py."""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from boardproof import checkpoint, orphan_footprints as of
from boardproof import schematic_import as si
from boardproof import write_layer
from tests.test_orphan_footprints import _digest, _cli, _pad_net, _propose, _refs, make_board

pytestmark = pytest.mark.skipif(not _cli(), reason="kicad-cli not installed")


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    return make_board(tmp_path)


def _node(name: str, *children) -> SExp:
    return SExp.list(name, *[write_layer.string(c) if isinstance(c, str) else c for c in children])


def _kept_ref(pcb: Path) -> str:
    return next(r for r in _refs(pcb) if not r.startswith("ZZ"))


def test_a_zone_inside_another_footprint_is_copper_evidence(board: Path, tmp_path: Path) -> None:
    net = _pad_net(board, "ZZ3")
    root = write_layer.load(board)
    host = next(f for f in root.children if f.name == "footprint" and si._reference_of(f) == _kept_ref(board))
    host.append(_node("zone", _node("net", net), _node("net_name", net)))
    write_layer.save(root, board)
    result = _propose(board, tmp_path)
    assert "ZZ3" not in result["orphans_removed"] and "copper on its nets" in result["orphans_kept"]["ZZ3"][0], result


def _isolated_segment(pcb: Path) -> None:
    root = write_layer.load(pcb)
    root.append(
        SExp.list(
            "segment",
            SExp.list("start", write_layer.number(1.0), write_layer.number(1.0)),
            SExp.list("end", write_layer.number(2.0), write_layer.number(1.0)),
            SExp.list("width", write_layer.number(0.2)),
            SExp.list("layer", write_layer.string("F.Cu")),
            SExp.list("net", write_layer.string("NO_SUCH_NET")),
            SExp.list("uuid", write_layer.string("aaaaaaaa-0000-4000-8000-000000000009")),
        )
    )
    write_layer.save(root, pcb)


@pytest.mark.parametrize("how", ["drop", "alter"])
def test_a_non_footprint_node_changed_by_the_write_is_caught(
    board: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, how: str
) -> None:
    _isolated_segment(board)
    proposal = _propose(board, tmp_path)
    assert proposal["status"] == "proposed", proposal
    before = _digest(tmp_path / "first")
    real_save = write_layer.save

    def corrupt(root, path, **kw):
        seg = next(c for c in root.children if c.name == "segment")
        if how == "drop":
            root.children[:] = [c for c in root.children if c is not seg]
        else:
            write_layer.set_number(seg.find("width"), 0, 0.5)
        return real_save(root, path, **kw)

    monkeypatch.setattr(write_layer, "save", corrupt)
    result = of.confirm_orphan_footprint_removal(board, proposal["revision_token"])
    assert result["ok"] is False and result["code"] == checkpoint.WRITE_NOT_VERIFIED, result
    assert not (tmp_path / "out").exists() and _digest(tmp_path / "first") == before


def test_a_group_member_orphan_is_kept(board: Path, tmp_path: Path) -> None:
    root = write_layer.load(board)
    fp = next(f for f in root.children if f.name == "footprint" and si._reference_of(f) == "ZZ1")
    uuid = str(fp.find("uuid").values[0])
    root.append(_node("group", "", _node("uuid", "bbbbbbbb-0000-4000-8000-000000000001"), _node("members", uuid)))
    write_layer.save(root, board)
    result = _propose(board, tmp_path)
    assert "ZZ1" not in result["orphans_removed"] and "it is a group member" in result["orphans_kept"]["ZZ1"], result


def test_a_replan_that_differs_at_confirm_is_stale_and_spends_the_token(
    board: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    proposal = _propose(board, tmp_path)
    real = of._plan
    monkeypatch.setattr(of, "_plan", lambda *a, **k: {**real(*a, **k), "removable": ["ZZ1"]})
    result = of.confirm_orphan_footprint_removal(board, proposal["revision_token"])
    assert result["status"] == "error" and "changed since the proposal" in result["reason"], result
    assert not (tmp_path / "out").exists()
    monkeypatch.undo()
    assert of.confirm_orphan_footprint_removal(board, proposal["revision_token"])["status"] == "error"  # spent


def test_the_netlist_exporter_seam_fails_closed(board: Path, tmp_path: Path) -> None:
    from kicad_tools.operations.netlist import Netlist

    before = _digest(tmp_path / "first")

    def boom(sch, out):
        raise RuntimeError("export exploded")

    raised = of.propose_orphan_footprint_removal(board, output_dir=tmp_path / "o1", netlist_exporter=boom)
    assert raised["status"] == "incomplete" and raised.get("revision_token") is None, raised
    empty = of.propose_orphan_footprint_removal(board, output_dir=tmp_path / "o2", netlist_exporter=lambda s, o: Netlist())
    assert empty["status"] == "blocked" and empty["revision_token"] is None, empty
    assert _digest(tmp_path / "first") == before and not (tmp_path / "o1").exists() and not (tmp_path / "o2").exists()
