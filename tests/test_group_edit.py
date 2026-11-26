"""propose/confirm move_group and create_group (issue 255): move or add a KiCad group.

Groups are added through the write layer to a copy of simple_led (same helpers as test_board_groups).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import group_edit, placement_anchor, raw_geometry, write_layer
from boardproof.toolsets import placement as placement_toolset
from tests.test_board_groups import FIXTURES, GHOST, SEG1, _fp_uuids, _group, _items

PAIR = "dddddddd-0000-4000-8000-000000000001"
OUTER = "dddddddd-0000-4000-8000-000000000002"
BROKEN = "dddddddd-0000-4000-8000-000000000003"
MIXED = "dddddddd-0000-4000-8000-000000000004"
LOCKED = "dddddddd-0000-4000-8000-000000000005"
SOLO = "dddddddd-0000-4000-8000-000000000006"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    dest = tmp_path / "led"
    dest.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy(FIXTURES / f"simple_led{suffix}", dest / f"simple_led{suffix}")
    pcb = dest / "simple_led.kicad_pcb"
    ids = _fp_uuids(pcb)
    root = write_layer.load(pcb)
    for item in _items():
        root.append(item)
    root.append(_group("pair", PAIR, [ids["R1"], ids["D1"]]))
    root.append(_group("outer", OUTER, [PAIR]))
    root.append(_group("broken", BROKEN, [GHOST]))
    root.append(_group("mixed", MIXED, [SEG1]))
    write_layer.save(root, pcb)
    return pcb


def _fp(root, ref):
    return next(f for f in root.children if not f.is_atom and f.name == "footprint" and raw_geometry._reference(f) == ref)


def _ats(pcb: Path) -> dict[str, list]:
    root = write_layer.load(pcb)
    return {raw_geometry._reference(f): [float(v) for v in f.find("at").values] for f in root.children if not f.is_atom and f.name == "footprint"}


def _pads(pcb: Path) -> dict[str, list]:
    return {f.reference: [p.at_mm for p in f.pads] for f in raw_geometry.read_footprints(write_layer.load(pcb))}


def _rename(pcb: Path, old: str, new: str) -> None:
    root = write_layer.load(pcb)
    for c in _fp(root, old).children:
        if not c.is_atom and c.name in ("property", "fp_text") and str(c.children[0].value).lower() == "reference":
            c.children[1] = write_layer.string(new)
    write_layer.save(root, pcb)
    assert old not in _ats(pcb)


def _lock(pcb: Path, ref: str | None = None, group: str | None = None) -> None:
    root = write_layer.load(pcb)
    if ref:
        placement_anchor.set_locked(_fp(root, ref), True)
    if group:
        g = next(n for n in root.children if not n.is_atom and n.name == "group" and raw_geometry.direct_uuid(n) == group)
        g.children.insert(2, write_layer.SExp.list("locked", write_layer.symbol("yes")))
    write_layer.save(root, pcb)


def _propose_move(board: Path, tmp_path: Path, groups=("pair",), dx=1.5, dy=-2.0) -> dict:
    return group_edit.propose_move_group(board, list(groups), dx, dy, tmp_path / "out")


def _confirm_refused(confirm, board: Path, found: dict, tmp_path: Path) -> dict:
    before = board.read_bytes()
    assert found["status"] == "proposed", found
    result = confirm(board, found["revision_token"])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert board.read_bytes() == before and not (tmp_path / "out").exists()
    return result


# --- move_group -----------------------------------------------------------------------------------


def test_propose_move_lists_old_and_new_and_writes_nothing(board: Path, tmp_path: Path) -> None:
    before = board.read_bytes()
    found = _propose_move(board, tmp_path)
    assert found["status"] == "proposed" and found["revision_token"]
    (g,) = found["groups"]
    ats = _ats(board)
    assert [p["reference"] for p in g["footprints"]] == ["D1", "R1"]
    for p in g["footprints"]:
        assert p["from"] == ats[p["reference"]][:2] and p["to"] == [round(p["from"][0] + 1.5, 6), round(p["from"][1] - 2.0, 6)]
    assert board.read_bytes() == before and not (tmp_path / "out").exists()


def test_confirm_move_moves_only_the_members_by_the_offset(board: Path, tmp_path: Path) -> None:
    before_bytes, ats, pads = board.read_bytes(), _ats(board), _pads(board)
    result = group_edit.confirm_move_group(board, _propose_move(board, tmp_path)["revision_token"])
    assert result["status"] == "moved", result
    assert board.read_bytes() == before_bytes
    out = Path(result["output_pcb"])
    now, now_pads = _ats(out), _pads(out)
    for ref in ats:
        want = [ats[ref][0] + (1.5 if ref in ("R1", "D1") else 0), ats[ref][1] + (-2.0 if ref in ("R1", "D1") else 0), *ats[ref][2:]]
        assert now[ref] == pytest.approx(want, abs=1e-6), ref
        shift = (1.5, -2.0) if ref in ("R1", "D1") else (0, 0)
        assert [(a[0] - b[0], a[1] - b[1]) for a, b in zip(now_pads[ref], pads[ref], strict=True)] == [pytest.approx(shift, abs=1e-6)] * len(pads[ref])


def test_a_group_moved_by_a_rotated_member_keeps_angles(board: Path, tmp_path: Path) -> None:
    root = write_layer.load(board)
    at = _fp(root, "R1").find("at")
    write_layer.set_number(at, 2, 90) if len(at.values) > 2 else at.children.append(write_layer.number(90))
    write_layer.save(root, board)
    result = group_edit.confirm_move_group(board, _propose_move(board, tmp_path)["revision_token"])
    assert result["status"] == "moved", result
    assert _ats(Path(result["output_pcb"]))["R1"][2] == 90


@pytest.mark.parametrize(
    ("ident", "word"),
    [("outer", "nested"), ("broken", "match nothing"), ("mixed", "not footprints"), ("nope", "no group")],
)
def test_a_group_that_cannot_move_whole_is_refused_and_listed(board: Path, tmp_path: Path, ident: str, word: str) -> None:
    found = _propose_move(board, tmp_path, groups=(ident,))
    assert found["status"] == "refused" and found["groups"] == [] and found["revision_token"] is None
    assert word in found["refused"][0]["reason"]


def test_an_empty_group_and_a_group_without_uuid_are_refused(board: Path, tmp_path: Path) -> None:
    root = write_layer.load(board)
    root.append(_group("empty", SOLO, []))
    sx = write_layer.SExp
    root.append(sx.list("group", write_layer.string("nouid"), sx.list("members", write_layer.string(_fp_uuids(board)["J1"]))))
    write_layer.save(root, board)
    assert "no footprint member" in _propose_move(board, tmp_path, groups=("empty",))["refused"][0]["reason"]
    assert "no uuid" in _propose_move(board, tmp_path, groups=("nouid",))["refused"][0]["reason"]


def test_a_locked_member_or_locked_group_is_refused(board: Path, tmp_path: Path) -> None:
    _lock(board, ref="R1")
    assert "locked" in _propose_move(board, tmp_path)["refused"][0]["reason"]
    root = write_layer.load(board)
    placement_anchor.set_locked(_fp(root, "R1"), False)
    write_layer.save(root, board)
    assert _propose_move(board, tmp_path)["status"] == "proposed"
    _lock(board, group=PAIR)
    assert "group is locked" in _propose_move(board, tmp_path)["refused"][0]["reason"]


def test_a_shared_reference_is_refused(board: Path, tmp_path: Path) -> None:
    _rename(board, "D1", "R1")
    assert "shared" in _propose_move(board, tmp_path)["refused"][0]["reason"]


def test_a_refused_group_does_not_stop_the_others(board: Path, tmp_path: Path) -> None:
    found = _propose_move(board, tmp_path, groups=("pair", "broken", PAIR))
    assert found["status"] == "proposed" and [g["group"] for g in found["groups"]] == ["pair"]
    assert [r["group"] for r in found["refused"]] == ["broken"]


def test_bad_move_arguments(board: Path, tmp_path: Path) -> None:
    for bad in ({"groups": []}, {"dx": float("nan")}, {"dy": "1"}, {"dx": True}):
        args = {"groups": ["pair"], "dx": 1.0, "dy": 1.0, **bad}
        assert group_edit.propose_move_group(board, **args)["status"] == "error", bad
    assert group_edit.propose_move_group(board, ["pair"], 0, 0)["status"] == "nothing_to_do"
    assert group_edit.propose_move_group(tmp_path / "none.kicad_pcb", ["pair"], 1, 1)["status"] == "incomplete"


def test_a_stale_move_token_is_refused_and_nothing_is_written(board: Path, tmp_path: Path) -> None:
    found = _propose_move(board, tmp_path)
    root = write_layer.load(board)
    write_layer.set_number(_fp(root, "R1").find("at"), 0, 77.0)
    write_layer.save(root, board)
    changed = board.read_bytes()
    result = group_edit.confirm_move_group(board, found["revision_token"])
    assert result["status"] == "error" and "changed since" in result["reason"]
    assert board.read_bytes() == changed and not (tmp_path / "out").exists()
    assert group_edit.confirm_move_group(board, "nope")["status"] == "error"


def test_a_move_plan_that_drifts_is_refused_by_the_digest_alone(board: Path, tmp_path: Path, monkeypatch) -> None:
    found = _propose_move(board, tmp_path)
    before = board.read_bytes()
    real = group_edit._plan_move

    def drifting(*a, **k):
        plan = real(*a, **k)
        plan["digest"] = "other"  # the source file is unchanged
        return plan

    monkeypatch.setattr(group_edit, "_plan_move", drifting)
    result = group_edit.confirm_move_group(board, found["revision_token"])
    assert result["status"] == "error" and result.get("stale") is True, result
    assert board.read_bytes() == before and not (tmp_path / "out").exists()


def _after_write(monkeypatch, change) -> None:
    """Corrupt the copy right after the translation write, so only this tool's own reload check can see it."""
    real = placement_anchor.write_positions

    def wrapper(path, *a, **k):
        out = real(path, *a, **k)
        root = write_layer.load(path)
        change(root)
        write_layer.save(root, path)
        return out

    monkeypatch.setattr(placement_anchor, "write_positions", wrapper)


@pytest.mark.parametrize(
    "change",
    [
        lambda root: write_layer.set_number(_fp(root, "R1").find("at"), 0, 99.0),  # wrong offset
        lambda root: write_layer.set_number(_fp(root, "J1").find("at"), 0, 99.0),  # a bystander moved
        lambda root: placement_anchor.set_locked(_fp(root, "R1"), True),  # a member changed outside its (at)
        lambda root: root.children.__setitem__(slice(None), root.children[:-1]),  # a node dropped
    ],
    ids=["offset", "bystander", "outside_at", "dropped"],
)
def test_the_move_self_check_fires(board: Path, tmp_path: Path, monkeypatch, change) -> None:
    _after_write(monkeypatch, change)
    _confirm_refused(group_edit.confirm_move_group, board, _propose_move(board, tmp_path), tmp_path)


def test_a_pad_that_does_not_follow_is_caught_by_the_pad_check_alone(board: Path, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(group_edit, "_TOL", 1e9)  # silence the (at) comparison: only the pads can complain

    def shove_pad(root):
        pad = next(c for c in _fp(root, "R1").children if not c.is_atom and c.name == "pad")
        write_layer.set_number(pad.find("at"), 0, 55.0)

    _after_write(monkeypatch, shove_pad)
    monkeypatch.setattr(group_edit, "_TOL", 2e-6)
    # _without_at keeps the pad change visible too, so make the stripped check blind to it
    monkeypatch.setattr(group_edit, "_without_at", lambda node: "same")
    result = _confirm_refused(group_edit.confirm_move_group, board, _propose_move(board, tmp_path), tmp_path)
    assert "pad positions" in str(result)


# --- create_group ---------------------------------------------------------------------------------


def _propose_create(board: Path, tmp_path: Path, name="new", members=("J1",)) -> dict:
    return group_edit.propose_create_group(board, name, list(members), tmp_path / "out")


def _groups(pcb: Path) -> list:
    return [n for n in write_layer.load(pcb).children if not n.is_atom and n.name == "group"]


def test_propose_create_shows_the_group_and_writes_nothing(board: Path, tmp_path: Path) -> None:
    before = board.read_bytes()
    found = _propose_create(board, tmp_path)
    assert found["status"] == "proposed" and found["group"]["name"] == "new" and found["group"]["members"] == ["J1"]
    assert len(found["group"]["uuid"]) == 36
    assert board.read_bytes() == before and not (tmp_path / "out").exists()


def test_confirm_create_adds_exactly_one_group(board: Path, tmp_path: Path) -> None:
    before_bytes, before = board.read_bytes(), len(write_layer.load(board).children)
    found = _propose_create(board, tmp_path, name="Strom ä", members=("J1",))
    result = group_edit.confirm_create_group(board, found["revision_token"])
    assert result["status"] == "created", result
    assert board.read_bytes() == before_bytes
    out = Path(result["output_pcb"])
    root = write_layer.load(out)
    assert len(root.children) == before + 1
    new = root.children[-1]
    ids = _fp_uuids(out)
    assert raw_geometry.direct_uuid(new) == found["group"]["uuid"] and str(new.children[0].value) == "Strom ä"
    assert [str(v) for v in new.find("members").values] == [ids["J1"]]


def test_a_created_group_can_be_moved(board: Path, tmp_path: Path) -> None:
    made = group_edit.confirm_create_group(board, _propose_create(board, tmp_path, members=("J1",))["revision_token"])
    moved = group_edit.propose_move_group(made["output_pcb"], ["new"], 3, 0, tmp_path / "out2")
    assert moved["status"] == "proposed" and [p["reference"] for p in moved["groups"][0]["footprints"]] == ["J1"]


@pytest.mark.parametrize(
    ("name", "members", "word"),
    [
        ("", ("J1",), "empty"),
        ("   ", ("J1",), "empty"),
        ("pair", ("J1",), "already exists"),
        ("new", ("J1", "J1"), "more than once"),
        ("new", ("X9",), "no footprint"),
        ("new", ("R1",), "already belongs"),
        ("new\x01", ("J1",), "cannot be written"),
    ],
)
def test_create_refusals_are_listed(board: Path, tmp_path: Path, name: str, members: tuple, word: str) -> None:
    found = _propose_create(board, tmp_path, name=name, members=members)
    assert found["status"] == "refused" and found["revision_token"] is None and found["group"] is None
    assert any(word in r["reason"] for r in found["refused"]), found


def test_a_shared_reference_cannot_join_a_group(board: Path, tmp_path: Path) -> None:
    _rename(board, "J1", "R1")
    assert any("share" in r["reason"] for r in _propose_create(board, tmp_path, members=("R1",))["refused"])


def test_bad_create_arguments(board: Path) -> None:
    assert group_edit.propose_create_group(board, "n", [])["status"] == "error"
    assert group_edit.propose_create_group(board, 5, ["J1"])["status"] == "error"  # type: ignore[arg-type]
    assert group_edit.propose_create_group(board.with_name("none.kicad_pcb"), "n", ["J1"])["status"] == "incomplete"


def test_a_stale_create_token_is_refused(board: Path, tmp_path: Path) -> None:
    found = _propose_create(board, tmp_path)
    root = write_layer.load(board)
    root.append(_group("new", "dddddddd-0000-4000-8000-0000000000aa", []))  # the name is now taken
    write_layer.save(root, board)
    changed = board.read_bytes()
    result = group_edit.confirm_create_group(board, found["revision_token"])
    assert result["status"] == "error" and "changed since" in result["reason"]
    assert board.read_bytes() == changed and not (tmp_path / "out").exists()


def test_a_create_plan_that_drifts_is_refused_by_the_digest_alone(board: Path, tmp_path: Path, monkeypatch) -> None:
    found = _propose_create(board, tmp_path)
    real = group_edit._plan_create

    def drifting(*a, **k):
        plan = real(*a, **k)
        plan["digest"] = "other"
        return plan

    monkeypatch.setattr(group_edit, "_plan_create", drifting)
    result = group_edit.confirm_create_group(board, found["revision_token"])
    assert result["status"] == "error" and result.get("stale") is True, result
    assert not (tmp_path / "out").exists()


def _after_save(monkeypatch, change) -> None:
    real = write_layer.save

    def saving(root, path, *a, **k):
        if str(path).endswith(".kicad_pcb"):
            change(root)
        return real(root, path, *a, **k)

    monkeypatch.setattr(write_layer, "save", saving)


@pytest.mark.parametrize(
    "change",
    [
        lambda root: write_layer.set_number(_fp(root, "J1").find("at"), 0, 99.0),  # another node changed
        lambda root: root.children.__setitem__(slice(None), root.children[:-1]),  # the group is not there
        lambda root: root.append(_group("extra", "dddddddd-0000-4000-8000-0000000000bb", [])),  # two nodes added
        lambda root: root.children[-1].find("members").children.clear(),  # the group lost its members
        lambda root: root.children[-1].find("uuid").children.__setitem__(0, write_layer.string("dddddddd-0000-4000-8000-0000000000cc")),
    ],
    ids=["other_node", "no_group", "extra_node", "members_lost", "wrong_uuid"],
)
def test_the_create_self_check_fires(board: Path, tmp_path: Path, monkeypatch, change) -> None:
    found = _propose_create(board, tmp_path)
    _after_save(monkeypatch, change)
    _confirm_refused(group_edit.confirm_create_group, board, found, tmp_path)


# --- the tools ------------------------------------------------------------------------------------


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_the_pairs_are_registered_and_the_proposes_are_read_only(tools: dict) -> None:
    names = ("propose_move_group", "confirm_move_group", "propose_create_group", "confirm_create_group")
    assert set(names) <= set(tools)
    assert tools["propose_move_group"].annotations.read_only_hint is True
    assert tools["propose_create_group"].annotations.read_only_hint is True
    for n in names:
        code = tools[n].fn.__code__
        assert set(code.co_varnames[: code.co_argcount]) <= {"pcb_path", "groups", "dx", "dy", "name", "members", "output_dir", "revision_token"}


def test_the_confirm_tools_name_the_new_revision(board: Path, tmp_path: Path, tools: dict) -> None:
    fns = {k: v.fn for k, v in tools.items()}
    found = fns["propose_move_group"](str(board), ["pair"], 1.0, 1.0, str(tmp_path / "out"))
    done = fns["confirm_move_group"](str(board), found["revision_token"])
    assert done["status"] == "moved" and done["pcb_path"] == done["output_pcb"] and done["source_pcb"] == str(board)
    found = fns["propose_create_group"](str(board), "new", ["J1"], str(tmp_path / "out2"))
    done = fns["confirm_create_group"](str(board), found["revision_token"])
    assert done["status"] == "created" and done["pcb_path"] == done["output_pcb"]


def test_a_second_confirm_with_the_same_token_is_refused(board: Path, tmp_path: Path) -> None:
    found = _propose_move(board, tmp_path)
    assert group_edit.confirm_move_group(board, found["revision_token"])["status"] == "moved"
    shutil.rmtree(tmp_path / "out")  # the output directory is free again: only the spent token can refuse
    assert group_edit.confirm_move_group(board, found["revision_token"])["status"] == "error"
    made = _propose_create(board, tmp_path / "c")
    assert group_edit.confirm_create_group(board, made["revision_token"])["status"] == "created"
    shutil.rmtree(tmp_path / "c" / "out") if (tmp_path / "c" / "out").exists() else shutil.rmtree(tmp_path / "c")
    assert group_edit.confirm_create_group(board, made["revision_token"])["status"] == "error"


def test_the_move_text_tells_to_run_the_placement_check(tools: dict) -> None:
    for name in ("propose_move_group", "confirm_move_group"):
        text = tools[name].description
        assert "run pcb_check_placement on the new revision" in text and "neither courtyard nor pad" in text
