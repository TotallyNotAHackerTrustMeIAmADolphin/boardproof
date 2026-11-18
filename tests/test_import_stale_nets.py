"""GitHub issue 69: confirm_schematic_import removes orphaned nets instead of refusing.

A net declaration (top-level ``(net N "name")``) that no pad, segment, arc, via or zone uses and
that the kicad-cli netlist no longer contains is what KiCad's "Update PCB from Schematic" drops. The
proposal lists such nets (``stale_nets_removed``) and the confirm removes them through the write
layer and the checkpoint. A net with any copper, any pad, or evidence that cannot be read keeps
the old refusal. Fixtures are built through the write layer on tmp copies, never by text edits.
"""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp, parse_file

from boardproof import checkpoint
from boardproof import schematic_import as si
from boardproof import write_layer
from tests import _kicad_faults as faults
from boardproof.kicad_cli import find_kicad_cli

REPO = Path(__file__).resolve().parents[1]
RUNG1 = REPO / "evals" / "rung1"
NAME = "rung1_usbc_attiny1616"


def _cli() -> bool:
    try:
        find_kicad_cli()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _cli(), reason="kicad-cli not installed")

ORPHANS = ["/Endstufe/Vset", "VISO", "unconnected-(U1-PA0)"]


def _node(name: str, *children) -> SExp:
    atoms = []
    for c in children:
        if isinstance(c, SExp):
            atoms.append(c)
        elif isinstance(c, str):
            atoms.append(write_layer.string(c))
        else:
            atoms.append(write_layer.number(c))
    return SExp.list(name, *atoms)


def _digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _table(pcb: Path) -> dict[str, int]:
    return {str(n.values[1]): int(n.values[0]) for n in parse_file(str(pcb)).children if n.name == "net" and len(n.values) > 1}


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    """An imported rung-1 board (every pad on its netlist net) plus three orphan net declarations."""
    proj = tmp_path / "proj"
    shutil.copytree(RUNG1 / "project", proj)
    first = tmp_path / "first"
    proposal = si.propose_schematic_import(proj / f"{NAME}.kicad_pcb", output_dir=first)
    assert proposal["status"] == "proposed", proposal
    assert si.confirm_schematic_import(proj / f"{NAME}.kicad_pcb", proposal["revision_token"])["status"] == "imported"
    pcb = first / f"{NAME}.kicad_pcb"
    root = write_layer.load(pcb)
    top = max(int(n.values[0]) for n in root.children if n.name == "net")
    for i, name in enumerate(ORPHANS, start=1):
        root.append(_node("net", top + 10 * i, name))
    write_layer.save(root, pcb)
    return pcb


def _propose(pcb: Path, tmp_path: Path, out: str = "out") -> dict:
    return si.propose_schematic_import(pcb, output_dir=tmp_path / out)


def _add(pcb: Path, node: SExp) -> None:
    root = write_layer.load(pcb)
    root.append(node)
    write_layer.save(root, pcb)


def _net_id(pcb: Path, name: str) -> int:
    return _table(pcb)[name]


# --- the reproduction ----------------------------------------------------------------


def test_orphan_nets_are_listed_by_the_proposal_and_removed_by_the_confirm(board: Path, tmp_path: Path) -> None:
    before = _digest(tmp_path / "first")
    proposal = _propose(board, tmp_path)
    assert proposal["status"] == "proposed", proposal
    assert proposal["stale_nets_removed"] == sorted(ORPHANS)
    assert _digest(tmp_path / "first") == before and not (tmp_path / "out").exists()  # a dry run

    result = si.confirm_schematic_import(board, proposal["revision_token"])
    assert result["status"] == "imported", result
    assert result["stale_nets_removed"] == sorted(ORPHANS)
    table = _table(tmp_path / "out" / board.name)
    assert not set(ORPHANS) & set(table)
    assert _digest(tmp_path / "first") == before  # the source stays as it was


def test_confirm_result_netlist_check_still_compares_every_net(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    result = si.confirm_schematic_import(board, proposal["revision_token"])
    nets = {n for n in _table(tmp_path / "out" / board.name) if n}
    assert nets == set(result["nets"])  # the board net table equals the kicad-cli netlist


def test_a_board_without_orphans_lists_none(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    out = tmp_path / "out"
    assert si.confirm_schematic_import(board, proposal["revision_token"])["status"] == "imported"
    again = si.propose_schematic_import(out / board.name, output_dir=tmp_path / "out2")
    assert again["status"] == "proposed" and again["stale_nets_removed"] == []


# --- a net with copper or a pad is never removed ---------------------------------------------


def _segment(net_id: int) -> SExp:
    return _node(
        "segment", _node("start", 1.0, 1.0), _node("end", 2.0, 1.0), _node("width", 0.2),
        _node("layer", "F.Cu"), _node("net", net_id), _node("uuid", "aaaaaaaa-0000-4000-8000-000000000001"),
    )


def _arc(net_id: int) -> SExp:
    return _node(
        "arc", _node("start", 1.0, 1.0), _node("mid", 1.5, 1.5), _node("end", 2.0, 1.0), _node("width", 0.2),
        _node("layer", "F.Cu"), _node("net", net_id), _node("uuid", "aaaaaaaa-0000-4000-8000-000000000002"),
    )


def _via(net_id: int) -> SExp:
    return _node(
        "via", _node("at", 3.0, 3.0), _node("size", 0.6), _node("drill", 0.3),
        _node("layers", "F.Cu", "B.Cu"), _node("net", net_id), _node("uuid", "aaaaaaaa-0000-4000-8000-000000000003"),
    )


def _zone(net_id: int, name: str) -> SExp:
    return _node(
        "zone", _node("net", net_id), _node("net_name", name), _node("layer", "F.Cu"),
        _node("uuid", "aaaaaaaa-0000-4000-8000-000000000004"),
        _node("polygon", _node("pts", _node("xy", 0.0, 0.0), _node("xy", 5.0, 0.0), _node("xy", 5.0, 5.0))),
    )


@pytest.mark.parametrize("kind", ["segment", "arc", "via", "zone"])
def test_a_net_with_copper_is_never_removed_and_is_named_in_the_refusal(board: Path, tmp_path: Path, kind: str) -> None:
    net = "VISO"
    nid = _net_id(board, net)
    node = {"segment": _segment, "arc": _arc, "via": _via}.get(kind, lambda i: _zone(i, net))(nid)
    _add(board, node)
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "VISO" in result["reason"] and kind in result["reason"]
    assert "/Endstufe/Vset" not in result["reason"]  # removable orphans are not blamed
    assert not (tmp_path / "out").exists()


def test_a_zone_that_names_the_net_only_by_name_is_copper_evidence(board: Path, tmp_path: Path) -> None:
    _add(board, _node(
        "zone", _node("net", 0), _node("net_name", "VISO"), _node("layer", "F.Cu"),
        _node("uuid", "aaaaaaaa-0000-4000-8000-000000000005"),
        _node("polygon", _node("pts", _node("xy", 0.0, 0.0), _node("xy", 5.0, 0.0), _node("xy", 5.0, 5.0))),
    ))
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked" and "VISO" in result["reason"]


def test_a_segment_that_names_the_net_only_by_id_is_copper_evidence(board: Path, tmp_path: Path) -> None:
    _add(board, _segment(_net_id(board, "unconnected-(U1-PA0)")))
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked" and "unconnected-(U1-PA0)" in result["reason"]


def test_a_net_on_a_pad_is_never_removed(board: Path, tmp_path: Path) -> None:
    nid = _net_id(board, "VISO")
    root = write_layer.load(board)
    pad = next(p for fp in root.children if fp.name == "footprint" for p in fp.children if p.name == "pad" and p.find("net") is None)
    pad.append(_node("net", nid, "VISO"))
    write_layer.save(root, board)
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "VISO" in result["reason"] and "pad" in result["reason"]


def test_a_pad_naming_the_net_with_another_number_is_still_a_use(board: Path, tmp_path: Path) -> None:
    root = write_layer.load(board)
    pad = next(p for fp in root.children if fp.name == "footprint" for p in fp.children if p.name == "pad" and p.find("net") is None)
    pad.append(_node("net", 7777, "VISO"))  # not VISO's declared number: the name alone is the evidence
    write_layer.save(root, board)
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "VISO" in result["reason"] and "pad" in result["reason"]


def test_a_legacy_net_class_listing_the_net_is_a_use(board: Path, tmp_path: Path) -> None:
    _add(board, _node("net_class", "Default", "", _node("add_net", "VISO")))
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked" and "VISO" in result["reason"] and "add_net" in result["reason"], result


def test_the_plan_hash_covers_the_orphans_to_remove(board: Path) -> None:
    sch = board.parent / f"{NAME}.kicad_sch"

    def plan_hash() -> str:
        return si._plan(board, sch, None, si._DEFAULT_COLUMNS, si._DEFAULT_SPACING, None, None)["plan_hash"]

    with_orphans = plan_hash()
    root = write_layer.load(board)
    gone = next(n for n in root.children if n.name == "net" and len(n.values) > 1 and str(n.values[1]) == "VISO")
    root.children[:] = [c for c in root.children if c is not gone]
    write_layer.save(root, board)
    assert plan_hash() != with_orphans


def test_copper_on_a_stale_net_blocks_even_when_other_orphans_are_clean(board: Path, tmp_path: Path) -> None:
    _add(board, _via(_net_id(board, "VISO")))
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked"
    assert "stale_nets_removed" not in result


# --- evidence that cannot be read keeps the refusal ----------------------------------------------


def test_a_duplicate_net_number_is_undetermined_and_blocks(board: Path, tmp_path: Path) -> None:
    _add(board, _node("net", _net_id(board, "VISO"), "VISO_COPY"))
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked" and "VISO" in result["reason"]


def test_a_duplicate_net_name_is_undetermined_and_blocks(board: Path, tmp_path: Path) -> None:
    _add(board, _node("net", 9999, "VISO"))
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked" and "VISO" in result["reason"]


def test_a_net_entry_with_an_unreadable_number_is_undetermined_and_blocks(board: Path, tmp_path: Path) -> None:
    root = write_layer.load(board)  # KiCad itself cannot load this: written without its parse check
    root.append(_node("net", write_layer.symbol("abc"), "Weird"))
    faults.write_unchecked(root, board)
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked" and "Weird" in result["reason"]


def test_a_copper_item_with_an_unreadable_net_reference_is_undetermined(board: Path, tmp_path: Path) -> None:
    seg = _segment(1)
    seg.find("net").children[0] = write_layer.string("not-a-number")
    _add(board, seg)
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked"


# --- the confirm: rollback, self-check, lock -------------------------------------------------------


def test_a_removal_that_leaves_the_net_table_wrong_is_rolled_back(board: Path, tmp_path: Path, monkeypatch) -> None:
    proposal = _propose(board, tmp_path)
    monkeypatch.setattr(si, "_remove_stale_nets", lambda root, plan: None)  # corrupted write: nothing removed
    before = _digest(tmp_path / "first")
    result = si.confirm_schematic_import(board, proposal["revision_token"])
    assert result["status"] == "error" and result["ok"] is False and result["code"] == "write_not_verified", result
    assert "net names" in result["reason"]
    assert not (tmp_path / "out").exists() and _digest(tmp_path / "first") == before
    monkeypatch.undo()
    assert si.confirm_schematic_import(board, proposal["revision_token"])["status"] == "imported"  # token stayed valid


def test_a_removal_that_also_drops_a_used_net_is_rolled_back(board: Path, tmp_path: Path, monkeypatch) -> None:
    proposal = _propose(board, tmp_path)
    real = si._remove_stale_nets

    def too_much(root, plan):
        real(root, plan)
        used = next(n for n in root.children if n.name == "net" and len(n.values) > 1 and str(n.values[1]) == "GND")
        root.children[:] = [c for c in root.children if c is not used]

    monkeypatch.setattr(si, "_remove_stale_nets", too_much)
    result = si.confirm_schematic_import(board, proposal["revision_token"])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert "net table shrank" in result["reason"]
    assert not (tmp_path / "out").exists()


def test_a_removal_that_takes_copper_with_it_is_rolled_back(board: Path, tmp_path: Path, monkeypatch) -> None:
    proposal = _propose(board, tmp_path)
    real = si._remove_stale_nets

    def eats_copper(root, plan):
        real(root, plan)
        root.append(_segment(0))  # a count change the checkpoint must notice

    monkeypatch.setattr(si, "_remove_stale_nets", eats_copper)
    result = si.confirm_schematic_import(board, proposal["revision_token"])
    assert result["ok"] is False and result["code"] == "write_not_verified", result


def test_a_plan_naming_a_net_that_is_not_there_fails_the_write(board: Path) -> None:
    with pytest.raises(checkpoint.Failed):
        si._remove_stale_nets(parse_file(str(board)), {"stale_nets": {"no-such-net": 4242}})


def test_evidence_wrongly_calling_a_net_with_copper_unused_is_caught_by_the_reload(
    board: Path, tmp_path: Path, monkeypatch
) -> None:
    """Self-check: even if the evidence step were wrong, the reload sees copper on a net number
    the table no longer declares."""
    _add(board, _segment(_net_id(board, "VISO")))
    nid = _net_id(board, "VISO")
    monkeypatch.setattr(si, "_stale_net_evidence", lambda path, stale: ({n: nid for n in stale}, {}))
    monkeypatch.setattr(si, "_remove_stale_nets", lambda root, plan: _drop_viso(root, nid))
    proposal = _propose(board, tmp_path)
    assert proposal["status"] == "proposed", proposal
    result = si.confirm_schematic_import(board, proposal["revision_token"])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert "still in use" in result["reason"] and not (tmp_path / "out").exists()


def _drop_viso(root, nid: int) -> None:
    gone = [n for n in root.children if n.name == "net" and len(n.values) > 1 and str(n.values[1]) == "VISO"]
    root.children[:] = [c for c in root.children if all(c is not g for g in gone)]


def test_the_board_changing_after_the_proposal_is_drift(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    faults.drift(board)
    result = si.confirm_schematic_import(board, proposal["revision_token"])
    assert result.get("status") != "imported" and not (tmp_path / "out").exists()


def test_a_kicad_lock_file_refuses_the_removal_and_keeps_the_token(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    lock = board.with_name(f"~{board.name}.lck")
    lock.write_bytes(b'{"hostname":"SOMEONE-ELSES-PC","username":"someone"}')
    result = si.confirm_schematic_import(board, proposal["revision_token"])
    assert result["ok"] is False and result["code"] == "file_open_in_kicad", result
    assert not (tmp_path / "out").exists()
    lock.unlink()
    assert si.confirm_schematic_import(board, proposal["revision_token"])["status"] == "imported"


def test_removal_works_only_on_the_copy_never_the_source_net_table(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    si.confirm_schematic_import(board, proposal["revision_token"])
    assert set(ORPHANS) <= set(_table(board))  # the source revision still declares them


def test_a_renamed_net_keeps_the_old_refusal(board: Path, tmp_path: Path) -> None:
    """A stale net whose pads exist is a rename, not an orphan: unchanged behaviour (ticket 17)."""
    root = write_layer.load(board.parent / f"{NAME}.kicad_sch")
    labels = [n for n in root.iter_all() if n.name in ("label", "global_label", "hierarchical_label") and n.values and str(n.values[0]) == "UPDI"]
    assert labels
    for n in labels:
        write_layer.set_string(n, 0, "UPDI_NEW")
    write_layer.save(root, board.parent / f"{NAME}.kicad_sch")
    result = _propose(board, tmp_path)
    assert result["status"] == "blocked"
    assert "net renamed or removed in the schematic: reconcile the net table" in result["reason"]
    assert "/UPDI -> /UPDI_NEW" in result["reason"]
    assert "Endstufe" not in result["reason"]


# --- tool text (critical class) ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_both_tool_descriptions_carry_the_pinned_orphan_net_sentence() -> None:
    from mcp.client.client import Client

    from boardproof.server import build_server

    sentence = (
        "Orphaned nets (declared on the board, used by no pad, track, arc, via or zone, and absent from "
        "the netlist) are listed in stale_nets_removed and removed by the confirm, as KiCad's Update PCB "
        "from Schematic does; a net that still has a pad or copper, or whose use cannot be read, is never "
        "removed and keeps the refusal, which names it."
    )
    assert si.STALE_NETS_SENTENCE == sentence
    async with Client(build_server(), mode="auto") as client:
        tools = {t.name: t.description for t in (await client.list_tools()).tools}
    for name in ("propose_schematic_import", "confirm_schematic_import"):
        assert sentence in tools[name], name
