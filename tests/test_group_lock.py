"""propose_group_lock / confirm_group_lock (issue 254): lock or unlock whole KiCad groups.

Groups are added through the write layer to a copy of simple_led (same helpers as test_board_groups).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import group_lock, placement_anchor, raw_geometry, write_layer
from boardproof.toolsets import placement as placement_toolset
from tests.test_board_groups import FIXTURES, GHOST, _fp_uuids, _group

PAIR = "cccccccc-0000-4000-8000-000000000001"
OUTER = "cccccccc-0000-4000-8000-000000000002"
BROKEN = "cccccccc-0000-4000-8000-000000000003"
TWIN1 = "cccccccc-0000-4000-8000-000000000004"
TWIN2 = "cccccccc-0000-4000-8000-000000000005"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    dest = tmp_path / "led"
    dest.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy(FIXTURES / f"simple_led{suffix}", dest / f"simple_led{suffix}")
    pcb = dest / "simple_led.kicad_pcb"
    ids = _fp_uuids(pcb)
    root = write_layer.load(pcb)
    root.append(_group("pair", PAIR, [ids["R1"], ids["D1"]]))
    root.append(_group("outer", OUTER, [PAIR, ids["J1"]]))
    root.append(_group("broken", BROKEN, [ids["J1"], GHOST]))
    root.append(_group("twin", TWIN1, [ids["J1"]]))
    root.append(_group("twin", TWIN2, [ids["R1"]]))
    write_layer.save(root, pcb)
    return pcb


def _lock_states(pcb: Path) -> dict[str, bool]:
    root = write_layer.load(pcb)
    out = {raw_geometry._reference(f): placement_anchor.is_locked(f) for f in root.children if not f.is_atom and f.name == "footprint"}
    for g in root.children:
        if not g.is_atom and g.name == "group":
            out["group:" + raw_geometry.direct_uuid(g)] = placement_anchor.is_locked(g)
    return out


def _propose(board: Path, tmp_path: Path, groups=("pair",), locked=True) -> dict:
    return group_lock.propose_group_lock(board, list(groups), locked, tmp_path / "out")


def _confirm_refused(board: Path, tmp_path: Path) -> dict:
    before = board.read_bytes()
    found = _propose(board, tmp_path)
    assert found["status"] == "proposed", found
    result = group_lock.confirm_group_lock(board, found["revision_token"])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert board.read_bytes() == before and not (tmp_path / "out").exists()
    return result


def test_propose_lists_the_footprints_and_writes_nothing(board: Path, tmp_path: Path) -> None:
    before = board.read_bytes()
    found = _propose(board, tmp_path)
    assert found["status"] == "proposed" and found["revision_token"]
    (entry,) = found["groups"]
    assert entry["group"] == "pair" and entry["footprints"] == ["D1", "R1"] and entry["group_flag_changes"] is True
    assert board.read_bytes() == before and not (tmp_path / "out").exists()


def test_confirm_locks_group_and_members_and_leaves_the_source_untouched(board: Path, tmp_path: Path) -> None:
    before, states = board.read_bytes(), _lock_states(board)
    result = group_lock.confirm_group_lock(board, _propose(board, tmp_path)["revision_token"])
    assert result["status"] == "locked", result
    assert board.read_bytes() == before
    after = _lock_states(Path(result["output_pcb"]))
    changed = {k for k in after if after[k] != states[k]}
    assert changed == {"R1", "D1", f"group:{PAIR}"}
    assert all(after[k] for k in changed)


def test_unlock_by_uuid_clears_the_flags(board: Path, tmp_path: Path) -> None:
    first = group_lock.confirm_group_lock(board, _propose(board, tmp_path)["revision_token"])
    locked_board = Path(first["output_pcb"])
    found = group_lock.propose_group_lock(locked_board, [PAIR], False, tmp_path / "out2")
    assert found["status"] == "proposed", found
    second = group_lock.confirm_group_lock(locked_board, found["revision_token"])
    assert second["status"] == "unlocked", second
    assert not any(v for k, v in _lock_states(Path(second["output_pcb"])).items() if k in ("R1", "D1", f"group:{PAIR}"))


def test_an_already_locked_group_is_nothing_to_do(board: Path, tmp_path: Path) -> None:
    first = group_lock.confirm_group_lock(board, _propose(board, tmp_path)["revision_token"])
    again = group_lock.propose_group_lock(Path(first["output_pcb"]), ["pair"], True, tmp_path / "out2")
    assert again["status"] == "nothing_to_do" and again["revision_token"] is None


@pytest.mark.parametrize(
    ("ident", "word"),
    [("outer", "nested"), ("broken", "match nothing"), ("twin", "share this name"), ("nope", "no group")],
)
def test_a_group_that_cannot_be_read_whole_is_refused_and_listed(board: Path, tmp_path: Path, ident: str, word: str) -> None:
    found = _propose(board, tmp_path, groups=(ident,))
    assert found["status"] == "nothing_to_do" and found["groups"] == []
    assert word in found["refused"][0]["reason"]


def test_a_refused_group_does_not_stop_the_others(board: Path, tmp_path: Path) -> None:
    found = _propose(board, tmp_path, groups=("pair", "broken"))
    assert found["status"] == "proposed" and [g["group"] for g in found["groups"]] == ["pair"]
    assert [r["group"] for r in found["refused"]] == ["broken"]


def test_a_stale_token_is_refused_and_nothing_is_written(board: Path, tmp_path: Path) -> None:
    found = _propose(board, tmp_path)
    root = write_layer.load(board)
    root.append(_group("late", "cccccccc-0000-4000-8000-0000000000aa", []))
    write_layer.save(root, board)
    changed = board.read_bytes()
    result = group_lock.confirm_group_lock(board, found["revision_token"])
    assert result["status"] == "error" and "changed since" in result["reason"]
    assert board.read_bytes() == changed and not (tmp_path / "out").exists()


def test_an_unknown_token_is_refused(board: Path) -> None:
    assert group_lock.confirm_group_lock(board, "nope")["status"] == "error"


def test_bad_arguments_are_errors(board: Path) -> None:
    assert group_lock.propose_group_lock(board, [], True)["status"] == "error"
    assert group_lock.propose_group_lock(board, ["pair"], "yes")["status"] == "error"  # type: ignore[arg-type]


def test_a_plan_that_drifts_between_propose_and_confirm_is_refused_by_the_digest_alone(board: Path, tmp_path: Path, monkeypatch) -> None:
    found = _propose(board, tmp_path)
    before = board.read_bytes()
    real = group_lock._plan

    def drifting(*a, **k):
        plan = real(*a, **k)
        plan["groups"][0]["footprint_uuids"] = ["x", *plan["groups"][0]["footprint_uuids"]]  # the source file is unchanged
        return plan

    monkeypatch.setattr(group_lock, "_plan", drifting)
    result = group_lock.confirm_group_lock(board, found["revision_token"])
    assert result["status"] == "error" and result.get("stale") is True, result
    assert board.read_bytes() == before and not (tmp_path / "out").exists()


# --- the self-check fires: each corrupted write is caught by its own guard ----------------------


def _corrupt_save(monkeypatch, change) -> None:
    real = write_layer.save

    def saving(root, path, *a, **k):
        if str(path).endswith(".kicad_pcb"):
            change(root)
        return real(root, path, *a, **k)

    monkeypatch.setattr(write_layer, "save", saving)


def _fp(root, ref):
    return next(f for f in root.children if not f.is_atom and f.name == "footprint" and raw_geometry._reference(f) == ref)


def test_a_moved_member_is_caught_by_the_stripped_check_alone(board: Path, tmp_path: Path, monkeypatch) -> None:
    _corrupt_save(monkeypatch, lambda root: write_layer.set_number(_fp(root, "R1").find("at"), 0, 99.0))
    _confirm_refused(board, tmp_path)


def test_a_changed_node_outside_the_group_is_caught_by_the_other_nodes_check_alone(board: Path, tmp_path: Path, monkeypatch) -> None:
    _corrupt_save(monkeypatch, lambda root: write_layer.set_number(_fp(root, "J1").find("at"), 0, 99.0))
    _confirm_refused(board, tmp_path)


def test_a_locked_bystander_is_caught(board: Path, tmp_path: Path, monkeypatch) -> None:
    _corrupt_save(monkeypatch, lambda root: placement_anchor.set_locked(_fp(root, "J1"), True))
    _confirm_refused(board, tmp_path)


def test_a_dropped_last_node_is_caught_by_the_node_count_alone(board: Path, tmp_path: Path, monkeypatch) -> None:
    _corrupt_save(monkeypatch, lambda root: root.children.__setitem__(slice(None), root.children[:-1]))
    _confirm_refused(board, tmp_path)


def test_a_lock_that_is_not_written_is_caught_by_the_lock_state_check_alone(board: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(placement_anchor, "set_locked", lambda node, locked: None)
    _confirm_refused(board, tmp_path)


# --- the tools -----------------------------------------------------------------------------------


def test_the_pair_is_registered_and_the_propose_is_read_only() -> None:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert {"propose_group_lock", "confirm_group_lock"} <= set(tools)
    assert tools["propose_group_lock"].annotations.read_only_hint is True
    for t in ("propose_group_lock", "confirm_group_lock"):
        assert set(tools[t].fn.__code__.co_varnames[: tools[t].fn.__code__.co_argcount]) <= {"pcb_path", "groups", "locked", "output_dir", "revision_token"}


def test_the_confirm_tool_names_the_new_revision(board: Path, tmp_path: Path) -> None:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    fns = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    found = fns["propose_group_lock"](str(board), ["pair"], True, str(tmp_path / "out"))
    done = fns["confirm_group_lock"](str(board), found["revision_token"])
    assert done["status"] == "locked" and done["pcb_path"] == done["output_pcb"] and done["source_pcb"] == str(board)
