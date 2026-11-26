"""KiCad groups (build ticket 231): placement writes keep them, and list_footprint_groups reads them.

The group is added through the write layer to a copy of simple_led; positions are not read here.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp
from mcp.server.mcpserver import MCPServer

from boardproof import board_groups, raw_geometry, write_layer
from boardproof.toolsets import placement as placement_toolset

FIXTURES = Path(__file__).parent / "fixtures"
G1 = "bbbbbbbb-0000-4000-8000-000000000001"
G2 = "bbbbbbbb-0000-4000-8000-000000000002"
GHOST = "bbbbbbbb-0000-4000-8000-0000000000ff"
SEG1, SEG2, VIA, TXT = (f"bbbbbbbb-0000-4000-8000-0000000001{n:02d}" for n in range(4))


def _group(name: str, uid: str, members: list[str], locked: bool = False) -> SExp:
    kids = [write_layer.string(name), SExp.list("uuid", write_layer.string(uid))]
    if locked:
        kids.append(SExp.list("locked", write_layer.symbol("yes")))
    kids.append(SExp.list("members", *[write_layer.string(m) for m in members]))
    return SExp.list("group", *kids)


def _fp_uuids(pcb: Path) -> dict[str, str]:
    root = write_layer.load(pcb)
    return {raw_geometry._reference(f): raw_geometry.direct_uuid(f) for f in root.children if not f.is_atom and f.name == "footprint"}


def _items() -> list[SExp]:
    def num(*v):
        return [write_layer.number(x) for x in v]

    def seg(uid):
        return SExp.list("segment", SExp.list("start", *num(1, 1)), SExp.list("end", *num(2, 1)), SExp.list("width", *num(0.2)),
                         SExp.list("layer", write_layer.string("F.Cu")), SExp.list("net", *num(0)), SExp.list("uuid", write_layer.string(uid)))

    via = SExp.list("via", SExp.list("at", *num(3, 3)), SExp.list("size", *num(0.6)), SExp.list("drill", *num(0.3)),
                    SExp.list("layers", write_layer.string("F.Cu"), write_layer.string("B.Cu")), SExp.list("net", *num(0)),
                    SExp.list("uuid", write_layer.string(VIA)))
    txt = SExp.list("gr_text", write_layer.string("hi"), SExp.list("at", *num(4, 4)), SExp.list("layer", write_layer.string("F.SilkS")),
                    SExp.list("uuid", write_layer.string(TXT)))
    return [seg(SEG1), seg(SEG2), via, txt]


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
    root.append(_group("Strom ä", G1, [ids["R1"], ids["D1"], GHOST, SEG1, SEG2, VIA, TXT, SEG1]))
    root.append(_group("outer", G2, [G1, ids["J1"]], locked=True))
    write_layer.save(root, pcb)
    return pcb


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _groups_raw(pcb: Path) -> list[tuple[str, str, frozenset]]:
    root = write_layer.load(pcb)
    return sorted(
        (str(g.children[0].value), raw_geometry.direct_uuid(g), frozenset(str(v) for v in g.find("members").values))
        for g in root.children
        if not g.is_atom and g.name == "group"
    )


def test_place_footprints_without_a_move_and_lock_keep_every_group(board: Path, tools: dict) -> None:
    before = _groups_raw(board)
    assert len(before) == 2
    placed = tools["place_footprints"](str(board), [{"reference": "R1", "x": 10.0, "y": 10.0}])
    assert placed.get("status") in ("ok", "partial"), placed
    for locked in (True, False):
        assert tools["lock_footprints"](str(board), ["R1", "D1"], locked)["status"] == "ok"
    assert _groups_raw(board) == before


def _at(pcb: Path) -> dict[str, tuple]:
    """Raw (at ..) of each footprint: its own direct child, not a descendant's."""
    root = write_layer.load(pcb)
    out = {}
    for f in root.children:
        if not f.is_atom and f.name == "footprint":
            at = next(c for c in f.children if not c.is_atom and c.name == "at")
            out[raw_geometry._reference(f)] = tuple(float(v) for v in at.values)
    return out


def test_a_real_move_does_not_drag_or_drop_the_group(board: Path, tools: dict) -> None:
    before, at_before = _groups_raw(board), _at(board)
    moved = tools["place_footprints"](str(board), [{"reference": "R1", "x": 20.0, "y": 15.0}])
    assert moved.get("status") in ("ok", "partial"), moved
    assert _groups_raw(board) == before
    at_after = _at(board)
    assert at_after["R1"] != at_before["R1"]
    assert {r: p for r, p in at_after.items() if r != "R1"} == {r: p for r, p in at_before.items() if r != "R1"}


def test_listing_leaves_the_file_untouched(board: Path, tools: dict) -> None:
    data, mtime = board.read_bytes(), board.stat().st_mtime_ns
    assert tools["list_footprint_groups"](str(board))["group_count"] == 2
    assert board.read_bytes() == data and board.stat().st_mtime_ns == mtime


def test_several_members_nodes_are_all_read_and_list_shaped_members_are_written_out(board: Path) -> None:
    root = write_layer.load(board)
    g = next(c for c in root.children if not c.is_atom and c.name == "group" and raw_geometry.direct_uuid(c) == G2)
    g.append(SExp.list("members", SExp.list("uuid", write_layer.string(GHOST))))
    write_layer.save(root, board)
    outer = board_groups.list_groups(board)["groups"][1]
    assert any("uuid" in u and GHOST in u for u in outer["unresolved"]) and not any("SExp" in u for u in outer["unresolved"])
    assert outer["members"] == 3


def test_list_groups_reads_members_nesting_and_unresolved(board: Path) -> None:
    out = board_groups.list_groups(board)
    assert out["status"] == "ok" and out["group_count"] == 2
    inner, outer = out["groups"]
    assert inner["name"] == "Strom ä" and inner["footprints"] == ["D1", "R1"] and inner["locked"] is False
    assert inner["unresolved"] == [GHOST] and inner["members"] == 7  # SEG1 named twice counts once
    assert inner["other_members"] == {"segment": 2, "via": 1, "gr_text": 1}
    assert outer["locked"] is True and outer["nested_groups"] == [G1] and outer["footprints"] == ["J1"]


def test_list_groups_reports_locked_member_footprints(board: Path, tools: dict) -> None:
    tools["lock_footprints"](str(board), ["R1"], True)
    assert board_groups.list_groups(board)["groups"][0]["locked_footprints"] == ["R1"]


def test_list_groups_on_a_board_without_groups_and_a_missing_file(tmp_path: Path) -> None:
    plain = tmp_path / "p.kicad_pcb"
    shutil.copy(FIXTURES / "simple_led.kicad_pcb", plain)
    assert board_groups.list_groups(plain) == {"status": "ok", "group_count": 0, "groups": []}
    assert board_groups.list_groups(tmp_path / "none.kicad_pcb")["status"] == "incomplete"


def test_the_tool_is_registered_read_only(board: Path, tools: dict) -> None:
    assert tools["list_footprint_groups"](str(board))["group_count"] == 2
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "list_footprint_groups")
    assert tool.annotations.read_only_hint is True


def test_an_old_uuid_form_is_noted_and_a_normal_board_has_no_note(board: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert "note" not in board_groups.list_groups(board)
    real = write_layer.load

    def old_form(path):  # in memory only: KiCad 10 refuses a file whose group has (tstamp ..) in place of (uuid ..)
        root = real(path)
        for g in root.children:
            if not g.is_atom and g.name == "group":
                g.children[:] = [c for c in g.children if c.is_atom or c.name != "uuid"]
        return root

    monkeypatch.setattr(board_groups.write_layer, "load", old_form)
    out = board_groups.list_groups(board)
    assert "uuid form not recognised" in out["note"]
