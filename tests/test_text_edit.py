"""move_text and delete_text (GitHub issue 198): move or remove free texts and text boxes, batch, and the
out-of-page note of check_project.

Fixtures are built through the tools on ``tmp_path`` copies; the only node no tool makes, a ``text_box``,
is parsed in memory and saved through ``write_layer.save``. Nothing here edits KiCad text. Every position
is read from the raw ``(at ..)`` of the saved file before and after.
"""

from __future__ import annotations

import inspect
import shutil
import subprocess
from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, project_check, sheet_authoring, sheet_editing, text_edit, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

SExp = write_layer.SExp
BOX_UUID = "00000000-0000-4000-8000-0000000000b1"


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def _tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _insert_box(sch: Path, text: str, x: float, y: float, rotation: int = 0, uid: str = BOX_UUID) -> None:
    root = write_layer.load(sch)
    node = parse_string(
        f'(text_box "{text}" (exclude_from_sim no) (at {x} {y} {rotation}) (size 20 8) (margins 0.9525 0.9525 0.9525 0.9525) '
        f'(stroke (width 0) (type solid)) (fill (type none)) (effects (font (size 1.27 1.27)) (justify left top)) (uuid "{uid}"))')
    tail = [k for k, c in enumerate(root.children) if not c.is_atom and c.name in ("sheet_instances", "embedded_fonts")]
    root.children.insert(tail[0] if tail else len(root.children), node)
    write_layer.save(root, sch)


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    """R1 with a wire and the label NETA, one note inside the A4 page (KEEP), eight notes N0..N7 below it
    (y 225 to 253 mm, step 4, the peer's case) and a text box BOX at y 240."""
    base = tmp_path_factory.mktemp("te198")
    tools = _tools()
    made = project_toolset._project_create(str(base / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    assert tools["add_wires"](str(sch), [{"p1": [50.8, 46.99], "p2": [50.8, 40.64], "route": "direct", "snap": False}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 50.8, "y": 40.64, "text": "NETA", "snap": False}])["status"] == "ok"
    items = [{"x": 20.0, "y": 100.0, "text": "KEEP"}]
    items += [{"x": 20.0, "y": 225.0 + 4 * i, "text": f"N{i}"} for i in range(8)]
    assert tools["add_text"](str(sch), items, snap=False)["status"] == "ok"
    _insert_box(sch, "BOX", 100.0, 240.0)
    return base / "proj"


@pytest.fixture
def proj(template, tmp_path) -> Path:
    dest = tmp_path / "proj"
    shutil.copytree(template, dest)
    return dest


@pytest.fixture
def sheet(proj) -> Path:
    return proj / "demo.kicad_sch"


@pytest.fixture
def tools() -> dict:
    return _tools()


def _raw(sch: Path) -> dict[str, tuple]:
    """text/text_box nodes by their text: (kind, x, y, angle, uuid), read from the raw (at) tokens."""
    out = {}
    for c in write_layer.load(sch).children:
        if not c.is_atom and c.name in ("text", "text_box"):
            at = c.find_children("at")[0]
            out[c.get_string(0)] = (c.name, at.get_value(0), at.get_value(1), at.get_value(2), sheet_editing._uuid(c))
    return out


def _bytes(sch: Path) -> dict:
    return {p.name: p.read_bytes() for p in sch.parent.iterdir() if p.is_file()}


def _serial(node) -> str:
    return write_layer.serialise(node, compact=True)


def _others(sch: Path, skip: set[str]) -> list[str]:
    return [_serial(c) for c in write_layer.load(sch).children if c.is_atom or sheet_editing._uuid(c) not in skip]


def _nets(sch: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(sch, None))
    return sheet_editing.read_nets(tree, sch, None)


# --------------------------------------------------------------------------- the peer's case


def test_eight_notes_below_the_page_come_back_with_one_call(sheet, tools, tmp_path):
    before = _raw(sheet)
    assert [before[f"N{i}"][2] for i in range(8)] == [225.0 + 4 * i for i in range(8)]       # raw (at) before: y 225..253
    nets = _nets(sheet)
    others = _others(sheet, {before[f"N{i}"][4] for i in range(8)})
    result = tools["move_text"](str(sheet), [{"pattern": "N?", "region": [0, 220, 297, 260], "offset": [0, -150]}], snap=False)
    assert result["ok"] is True and result["written"] is True and result["count"] == 8, result
    after = _raw(sheet)
    for i in range(8):
        assert after[f"N{i}"][1:3] == (20.0, 75.0 + 4 * i) and after[f"N{i}"][4] == before[f"N{i}"][4]       # raw (at) after: y 75..103
    assert after["BOX"] == before["BOX"] and after["KEEP"] == before["KEEP"]       # the region pattern picked only the notes
    assert result["outside_page_after"] == [] and result["page_mm"] == [297.0, 210.0]
    assert _others(sheet, {before[f"N{i}"][4] for i in range(8)}) == others          # every other node is byte-equal
    assert _nets(sheet) == nets and result["connectivity"]["changed"] is False
    kicad_cli.export_svg(sheet, tmp_path / "svg")                                    # KiCad loads and draws the result
    svg = next((tmp_path / "svg").glob("*.svg")).read_text(encoding="utf-8")
    assert "N7" in svg


def test_region_alone_moves_the_whole_band_including_the_text_box(sheet, tools):
    result = tools["move_text"](str(sheet), [{"region": [0, 220, 297, 260], "offset": [10, -150]}], snap=False)
    assert result["ok"] is True and result["count"] == 9, result
    after = _raw(sheet)
    assert after["BOX"][1:3] == (110.0, 90.0) and after["BOX"][0] == "text_box"


def test_a_text_box_keeps_its_size_and_its_other_children(sheet, tools):
    def box(sch):
        return next(c for c in write_layer.load(sch).children if not c.is_atom and c.name == "text_box")

    old = box(sheet)
    assert tools["move_text"](str(sheet), [{"uuids": [BOX_UUID], "to": [50.8, 50.8]}])["ok"] is True
    new = box(sheet)
    assert [_serial(c) for c in new.children if c.is_atom or c.name != "at"] == [_serial(c) for c in old.children if c.is_atom or c.name != "at"]
    assert new.find_children("at")[0].get_value(0) == 50.8


# --------------------------------------------------------------------------- selectors, to, offset, snap, rotation


def test_to_snaps_by_default_and_not_when_off(sheet, tools):
    result = tools["move_text"](str(sheet), [{"texts": ["KEEP"], "to": [30.0, 31.5]}])
    assert result["moved"][0]["asked"] == [30.0, 31.5] and result["moved"][0]["snapped"] == [30.48, 31.75]
    assert _raw(sheet)["KEEP"][1:3] == (30.48, 31.75)
    assert tools["move_text"](str(sheet), [{"texts": ["KEEP"], "to": [30.0, 31.5]}], snap=False)["moved"][0]["snapped"] is None
    assert _raw(sheet)["KEEP"][1:3] == (30.0, 31.5)


def test_selector_keys_texts_uuids_pattern_and_kind(sheet, tools):
    raw = _raw(sheet)
    one = tools["move_text"](str(sheet), [{"texts": ["N0", "N1"], "offset": [1.27, 0]},
                                          {"uuids": [raw["N2"][4]], "offset": [2.54, 0]},
                                          {"pattern": "N[34]", "offset": [3.81, 0]},
                                          {"kind": "text_box", "offset": [0, 1.27]}], snap=False)
    assert one["ok"] is True and one["count"] == 6, one
    after = _raw(sheet)
    assert [after[f"N{i}"][1] for i in range(5)] == [21.27, 21.27, 22.54, 23.81, 23.81] and after["N5"][1] == 20.0
    assert after["BOX"][2] == 241.27


def test_texts_is_an_exact_match_not_a_substring(sheet, tools):
    before = _bytes(sheet)
    for bad in (["N"], ["KEE"], ["keep"]):
        result = tools["move_text"](str(sheet), [{"texts": bad, "offset": [1, 0]}])
        assert result["code"] == "not_found", (bad, result)
        assert tools["delete_text"](str(sheet), [{"texts": bad}])["code"] == "not_found"
    assert _bytes(sheet) == before


def test_texts_picks_only_the_exact_text_when_a_longer_one_contains_it(sheet, tools):
    assert tools["add_text"](str(sheet), [{"x": 60.0, "y": 100.0, "text": "N10"}], snap=False)["ok"] is True
    before = _raw(sheet)
    moved = tools["move_text"](str(sheet), [{"texts": ["N1"], "offset": [1.27, 0]}], snap=False)
    assert moved["count"] == 1 and moved["moved"][0]["text"] == "N1", moved
    gone = tools["delete_text"](str(sheet), [{"texts": ["N1"]}])
    assert gone["count"] == 1 and "N10" in _raw(sheet) and _raw(sheet)["N10"] == before["N10"] and "N1" not in _raw(sheet)


def test_pattern_is_case_sensitive(sheet, tools):
    before = _bytes(sheet)
    for pattern in ("keep", "n?", "n[0-1]"):
        assert tools["move_text"](str(sheet), [{"pattern": pattern, "offset": [1, 0]}])["code"] == "selector_matched_nothing", pattern
        assert tools["delete_text"](str(sheet), [{"pattern": pattern}])["code"] == "selector_matched_nothing", pattern
    assert _bytes(sheet) == before
    result = tools["move_text"](str(sheet), [{"pattern": "N[0-1]", "offset": [1.27, 0]}], dry_run=True)
    assert result["count"] == 2


@pytest.mark.parametrize("flag", [False, True])
def test_a_boolean_rotation_is_refused(sheet, tools, flag):
    before = _bytes(sheet)
    result = tools["move_text"](str(sheet), [{"texts": ["KEEP"], "offset": [1, 0], "rotation": flag}])
    assert result["code"] == "invalid_spec" and "rotation" in result["reason"], result
    assert _bytes(sheet) == before


def test_rotation_alone_turns_where_it_is_and_is_not_snapped(sheet, tools):
    tools["move_text"](str(sheet), [{"texts": ["KEEP"], "to": [30.0, 31.0]}], snap=False)
    result = tools["move_text"](str(sheet), [{"texts": ["KEEP"], "rotation": 90}])
    assert result["ok"] is True and result["moved"][0]["rotation_from"] == 0 and result["moved"][0]["rotation"] == 90
    assert _raw(sheet)["KEEP"][1:4] == (30.0, 31.0, 90)


def test_a_text_keeps_only_0_and_90_a_text_box_keeps_all_four(sheet, tools):
    before = _bytes(sheet)
    for angle in (180, 270):
        refused = tools["move_text"](str(sheet), [{"texts": ["KEEP"], "rotation": angle}])
        assert refused["ok"] is False and refused["code"] == "invalid_spec" and "180 as 0" in refused["reason"], refused
    assert _bytes(sheet) == before
    for angle in (90, 180, 270, 0):
        assert tools["move_text"](str(sheet), [{"uuids": [BOX_UUID], "rotation": angle}])["ok"] is True
        assert _raw(sheet)["BOX"][3] == angle


def test_kicad_cli_keeps_the_angle_the_tool_wrote(sheet, tools, tmp_path):
    """The measurement behind the rotation rule, on the file the tool wrote: kicad-cli's own rewrite keeps 90 and the box's 270."""
    assert tools["move_text"](str(sheet), [{"texts": ["KEEP"], "rotation": 90}, {"uuids": [BOX_UUID], "rotation": 270}])["ok"] is True
    copy = tmp_path / "up.kicad_sch"
    shutil.copy(sheet, copy)
    shutil.copy(sheet.with_suffix(".kicad_pro"), tmp_path / "up.kicad_pro")
    done = subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "upgrade", "--force", str(copy)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr
    rewritten = _raw(copy)
    assert rewritten["KEEP"][3] == 90 and rewritten["BOX"][3] in (270, -90)


@pytest.mark.parametrize("move, code", [
    ({"texts": ["KEEP"]}, "invalid_spec"),                                           # neither to, offset nor rotation
    ({"texts": ["KEEP"], "to": [1, 1], "offset": [1, 1]}, "invalid_spec"),
    ({"pattern": "N[1-7]", "to": [1, 1]}, "invalid_spec"),                         # 'to' needs exactly one object
    ({"texts": ["KEEP"], "to": [1]}, "invalid_spec"),
    ({"texts": ["KEEP"], "to": [float("nan"), 1]}, "invalid_spec"),
    ({"texts": ["KEEP"], "offset": ["1", 1]}, "invalid_spec"),
    ({"texts": ["KEEP"], "offset": [1, 1], "rotation": True}, "invalid_spec"),
    ({"texts": ["KEEP"], "offset": [1, 1], "rotation": 45}, "invalid_spec"),
    ({"offset": [1, 1]}, "invalid_spec"),                                           # no selector key
    ({"texts": "KEEP", "offset": [1, 1]}, "invalid_spec"),
    ({"texts": ["KEEP"], "offset": [1, 1], "colour": "red"}, "invalid_spec"),
    ({"pattern": "", "offset": [1, 1]}, "invalid_spec"),
    ({"kind": "label", "offset": [1, 1]}, "invalid_spec"),
    ({"region": [0, 0, 1], "offset": [1, 1]}, "invalid_spec"),
    ({"texts": ["NOPE"], "offset": [1, 1]}, "not_found"),
    ({"texts": ["KEEP", "NOPE"], "offset": [1, 1]}, "not_found"),
    ({"pattern": "ZZZ*", "offset": [1, 1]}, "selector_matched_nothing"),
    ({"region": [200, 200, 210, 210], "offset": [1, 1]}, "selector_matched_nothing"),
    ({"texts": ["NETA"], "offset": [1, 1]}, "not_found"),                           # a label is not a text
], ids=lambda v: str(v)[:60])
def test_a_bad_move_refuses_the_whole_call_and_writes_nothing(sheet, tools, move, code):
    before = _bytes(sheet)
    result = tools["move_text"](str(sheet), [{"texts": ["N0"], "offset": [1.27, 0]}, move])
    assert result["ok"] is False and result["code"] == code, result
    assert _bytes(sheet) == before


@pytest.mark.parametrize("moves", [[], None, "x", {"texts": ["KEEP"]}], ids=["empty", "none", "string", "object"])
def test_moves_must_be_a_non_empty_list(sheet, tools, moves):
    before = _bytes(sheet)
    assert tools["move_text"](str(sheet), moves)["code"] == "invalid_spec"
    assert _bytes(sheet) == before


def test_an_object_picked_by_two_moves_is_refused(sheet, tools):
    before = _bytes(sheet)
    result = tools["move_text"](str(sheet), [{"texts": ["N0"], "offset": [1, 0]}, {"pattern": "N*", "offset": [0, 1]}])
    assert result["code"] == "invalid_spec" and "earlier move" in result["reason"], result
    assert _bytes(sheet) == before


@pytest.mark.parametrize("args", [{"snap": "yes"}, {"grid": 0}, {"grid": -1.27}, {"grid": float("inf")}, {"dry_run": "no"}], ids=str)
def test_bad_snap_grid_or_dry_run_is_refused(sheet, tools, args):
    before = _bytes(sheet)
    assert tools["move_text"](str(sheet), [{"texts": ["KEEP"], "offset": [1, 0]}], **args)["code"] == "invalid_spec"
    assert _bytes(sheet) == before


def test_a_missing_sheet_is_refused(tmp_path, tools):
    assert tools["move_text"](str(tmp_path / "nope.kicad_sch"), [{"texts": ["a"], "offset": [1, 0]}])["code"] == "file_not_found"
    assert tools["delete_text"](str(tmp_path / "nope.kicad_sch"), [{"texts": ["a"]}])["code"] == "file_not_found"


def test_a_dry_run_writes_nothing_and_says_what_would_move(sheet, tools):
    before = _bytes(sheet)
    result = tools["move_text"](str(sheet), [{"region": [0, 220, 297, 260], "offset": [0, -150]}], snap=False, dry_run=True)
    assert result["ok"] is True and result["written"] is False and result["count"] == 9 and len(result["would_move"]) == 9, result
    assert "moved" not in result and _bytes(sheet) == before
    assert result["would_move"][0]["from"] == [20.0, 225.0] and result["would_move"][0]["to"] == [20.0, 75.0]


def test_a_text_still_outside_the_page_after_the_move_is_listed_not_refused(sheet, tools):
    result = tools["move_text"](str(sheet), [{"texts": ["N0", "N1"], "offset": [0, -10]}], snap=False)
    assert result["ok"] is True and result["written"] is True, result
    assert result["outside_page_after_count"] == 2 and {e["label"] for e in result["outside_page_after"]} == {"N0", "N1"}
    assert result["outside_page_after"][0]["y1"] == 215.0


def test_a_sheet_a_parent_uses_gets_its_move_and_the_parent_is_untouched(proj, tools):
    root = proj / "demo.kicad_sch"
    assert tools["create_sheets"](str(root), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    sub = proj / "sub.kicad_sch"
    assert tools["add_text"](str(sub), [{"x": 10.16, "y": 20.32, "text": "SUBNOTE"}])["status"] == "ok"
    placed = tools["add_sheet_symbols"](str(root), [{"file": "sub.kicad_sch", "name": "Sub", "x": 101.6, "y": 50.8, "pins": [{"name": "SER", "direction": "input"}]}])
    assert placed["status"] == "ok", placed
    root_bytes = root.read_bytes()
    moved = tools["move_text"](str(sub), [{"texts": ["SUBNOTE"], "offset": [5.08, 0]}])
    assert moved["ok"] is True, moved
    assert _raw(sub)["SUBNOTE"][1] == 15.24 and root.read_bytes() == root_bytes
    gone = tools["delete_text"](str(sub), [{"texts": ["SUBNOTE"]}])
    assert gone["ok"] is True and "SUBNOTE" not in _raw(sub) and root.read_bytes() == root_bytes


# --------------------------------------------------------------------------- delete_text


def test_delete_text_removes_the_selected_texts_and_nothing_else(sheet, tools):
    raw = _raw(sheet)
    gone_ids = {raw["N0"][4], raw["N1"][4], raw["BOX"][4]}
    others = _others(sheet, gone_ids)
    nets = _nets(sheet)
    result = tools["delete_text"](str(sheet), [{"texts": ["N0"]}, {"uuids": [raw["N1"][4]]}, {"kind": "text_box"}])
    assert result["ok"] is True and result["written"] is True and result["count"] == 3, result
    assert {d["text"] for d in result["deleted"]} == {"N0", "N1", "BOX"} and {d["kind"] for d in result["deleted"]} == {"text", "text_box"}
    assert next(d for d in result["deleted"] if d["text"] == "N0")["at"] == [20.0, 225.0]
    after = _raw(sheet)
    assert set(raw) - set(after) == {"N0", "N1", "BOX"}
    assert _others(sheet, gone_ids) == others and _nets(sheet) == nets


def test_delete_text_by_pattern_and_region_in_one_call(sheet, tools):
    result = tools["delete_text"](str(sheet), [{"pattern": "N*", "region": [0, 220, 297, 260]}])
    assert result["count"] == 8 and set(_raw(sheet)) == {"KEEP", "BOX"}, result


def test_delete_text_dry_run_reports_and_writes_nothing(sheet, tools):
    before = _bytes(sheet)
    result = tools["delete_text"](str(sheet), [{"pattern": "N*"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and len(result["would_delete"]) == 8 and "deleted" not in result
    assert _bytes(sheet) == before


@pytest.mark.parametrize("sel, code", [
    ({"pattern": "ZZZ*"}, "selector_matched_nothing"),
    ({"region": [200, 200, 210, 210]}, "selector_matched_nothing"),
    ({"texts": ["NOPE"]}, "not_found"),
    ({"uuids": ["00000000-0000-4000-8000-00000000dead"]}, "not_found"),
    ({"texts": ["NETA"]}, "not_found"),                    # a label is not a text; delete_labels owns it
    ({}, "invalid_spec"),
    ({"kind": "wire"}, "invalid_spec"),
    ({"nonsense": 1}, "invalid_spec"),
], ids=lambda v: str(v)[:50])
def test_delete_text_refuses_a_selector_that_selects_nothing_and_writes_nothing(sheet, tools, sel, code):
    before = _bytes(sheet)
    result = tools["delete_text"](str(sheet), [{"texts": ["N0"]}, sel])
    assert result["ok"] is False and result["code"] == code, result
    assert _bytes(sheet) == before


@pytest.mark.parametrize("selectors", [[], None, "x", {"texts": ["N0"]}], ids=["empty", "none", "string", "object"])
def test_delete_text_selectors_must_be_a_non_empty_list(sheet, tools, selectors):
    before = _bytes(sheet)
    assert tools["delete_text"](str(sheet), selectors)["code"] == "invalid_spec"
    assert _bytes(sheet) == before


def test_two_selectors_picking_one_text_delete_it_once(sheet, tools):
    result = tools["delete_text"](str(sheet), [{"texts": ["N0"]}, {"pattern": "N0"}])
    assert result["ok"] is True and result["count"] == 1, result


def test_delete_text_never_touches_a_label_even_with_a_matching_pattern(sheet, tools):
    assert tools["delete_text"](str(sheet), [{"pattern": "*"}])["ok"] is True
    root = write_layer.load(sheet)
    assert [c.get_string(0) for c in root.children if not c.is_atom and c.name == "label"] == ["NETA"]
    assert not _raw(sheet)


# --------------------------------------------------------------------------- groups


GROUP_UUID = "00000000-0000-4000-8000-0000000000f1"


def _group(sch: Path, members: list[str]) -> None:
    root = write_layer.load(sch)
    names = " ".join(f'"{m}"' for m in members)
    node = parse_string(f'(group "G" (uuid "{GROUP_UUID}") (members {names}))')
    tail = [k for k, c in enumerate(root.children) if not c.is_atom and c.name in ("sheet_instances", "embedded_fonts")]
    root.children.insert(tail[0] if tail else len(root.children), node)
    write_layer.save(root, sch)


def _groups(sch: Path) -> list[list[str]]:
    out = []
    for c in write_layer.load(sch).children:
        if not c.is_atom and c.name == "group":
            out.append([str(m.value) for m in c.find_children("members")[0].children])
    return out


def test_delete_text_drops_the_deleted_uuid_from_a_group(sheet, tools, tmp_path):
    raw = _raw(sheet)
    _group(sheet, [raw["N0"][4], raw["N1"][4]])
    result = tools["delete_text"](str(sheet), [{"texts": ["N0"]}])
    assert result["ok"] is True, result
    assert _groups(sheet) == [[raw["N1"][4]]]                       # no dangling uuid (KiCad drops it silently on its next save)
    assert result["group_members_removed"] == [{"group": GROUP_UUID, "members": [raw["N0"][4]]}] and result["groups_removed"] == []
    kicad_cli.export_svg(sheet, tmp_path / "svg")


def test_a_group_left_with_no_member_is_removed_as_kicad_does(sheet, tools, tmp_path):
    """Measured (kicad-cli 10.0.7, sch upgrade): a group whose members are all gone is dropped; one member left is kept."""
    raw = _raw(sheet)
    _group(sheet, [raw["N0"][4], raw["N1"][4]])
    result = tools["delete_text"](str(sheet), [{"texts": ["N0", "N1"]}])
    assert result["ok"] is True and _groups(sheet) == [], result
    assert result["groups_removed"] == [GROUP_UUID]
    assert not [c for c in write_layer.load(sheet).children if not c.is_atom and c.name == "group"]
    kicad_cli.export_svg(sheet, tmp_path / "svg")


def test_a_dry_run_reports_the_group_change_and_writes_nothing(sheet, tools):
    raw = _raw(sheet)
    _group(sheet, [raw["N0"][4], raw["N1"][4]])
    before = _bytes(sheet)
    result = tools["delete_text"](str(sheet), [{"texts": ["N0"]}], dry_run=True)
    assert result["group_members_removed"][0]["members"] == [raw["N0"][4]] and _bytes(sheet) == before


def test_a_group_of_other_objects_is_left_alone(sheet, tools):
    raw = _raw(sheet)
    _group(sheet, [raw["N1"][4], raw["N2"][4]])
    result = tools["delete_text"](str(sheet), [{"texts": ["N0"]}])
    assert result["ok"] is True and result["group_members_removed"] == [] and _groups(sheet) == [[raw["N1"][4], raw["N2"][4]]]


# --------------------------------------------------------------------------- the lock and the tool surface


def test_a_lock_file_refuses_both_tools_before_anything_is_computed(sheet, tools, monkeypatch):
    lock = sheet.with_name(f"~{sheet.name}.lck")
    lock.write_bytes(b'{"hostname":"other","username":"x"}')
    before = _bytes(sheet)

    def boom(*a, **k):
        raise AssertionError("kicad-cli ran although the sheet is open in KiCad")

    monkeypatch.setattr(sheet_editing, "read_nets", boom)
    for name, args in {"move_text": [{"texts": ["N0"], "offset": [1.27, 0]}], "delete_text": [{"texts": ["N0"]}]}.items():
        result = tools[name](str(sheet), args)
        assert result["code"] == "file_open_in_kicad" and result["ok"] is False, (name, result)
    assert _bytes(sheet) == before


def test_there_is_no_connectivity_override(tools):
    for name in ("move_text", "delete_text"):
        assert "allow_connectivity_change" not in inspect.signature(tools[name]).parameters


# --------------------------------------------------------------------------- the self-checks (each fires on a broken write)


def test_move_text_corrupted_write_is_refused_and_leaves_the_sheet_alone(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _tools()["move_text"](str(sheet), [{"texts": ["N0"], "offset": [1.27, 0]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sheet) == before
    assert not [p for p in sheet.parent.iterdir() if p.name.endswith(".tmp")]


def test_delete_text_corrupted_write_is_refused_and_leaves_the_sheet_alone(sheet, monkeypatch):
    real = write_layer.load
    before = _bytes(sheet)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _tools()["delete_text"](str(sheet), [{"texts": ["N0"]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    monkeypatch.undo()
    assert _bytes(sheet) == before
    assert not [p for p in sheet.parent.iterdir() if p.name.endswith(".tmp")]


def test_move_text_staged_check_fires_when_a_move_also_moves_something_else(sheet, monkeypatch):
    real = sheet_editing._set_at

    def wide(node, x, y):
        real(node, x, y)
        root = state["root"]
        sym = next(c for c in root.children if not c.is_atom and c.name == "symbol")
        real(sym, 1.0, 1.0)

    state: dict = {}
    real_plan = text_edit._plan

    def spy(moves, root, snap, grid):
        state["root"] = root
        return real_plan(moves, root, snap, grid)

    before = _bytes(sheet)
    monkeypatch.setattr(text_edit, "_plan", spy)
    monkeypatch.setattr(sheet_editing, "_set_at", wide)
    result = _tools()["move_text"](str(sheet), [{"texts": ["N0"], "offset": [1.27, 0]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert _bytes(sheet) == before


def test_delete_text_staged_check_fires_when_a_delete_removes_more_than_was_selected(sheet, monkeypatch):
    real = sheet_editing._remove

    def greedy(parent, doomed):
        wire = next(c for c in parent.children if not c.is_atom and c.name == "wire")
        real(parent, [*doomed, wire])

    before = _bytes(sheet)
    monkeypatch.setattr(sheet_editing, "_remove", greedy)
    result = _tools()["delete_text"](str(sheet), [{"texts": ["N0"]}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert _bytes(sheet) == before


def test_delete_text_netlist_oracle_fires_when_the_selection_reaches_a_label(sheet, monkeypatch):
    """The connectivity comparison is what proves a text joins nothing: with a label picked, the delete is refused."""
    real = text_edit._pick

    def label_too(root, sel, where):
        return [*real(root, sel, where), next(c for c in root.children if not c.is_atom and c.name == "label")]

    before = _bytes(sheet)
    monkeypatch.setattr(text_edit, "_pick", label_too)
    monkeypatch.setattr(text_edit.add_text, "_erc_unchanged", lambda s, n: (lambda _b, _a: None))      # the ERC check runs first; this test is about the netlist
    result = _tools()["delete_text"](str(sheet), [{"texts": ["N0"]}])
    assert result["ok"] is False and result["code"] == "connectivity_changed", result
    assert _bytes(sheet) == before


def test_move_text_erc_check_fires_when_the_census_moves(sheet, monkeypatch):
    real = sheet_authoring.erc_census
    calls = {"n": 0}

    def moving(root):
        calls["n"] += 1
        census = real(root)
        if calls["n"] == 2:       # the second census is the one with the change
            census[("invented", "error")] += 1
        return census

    before = _bytes(sheet)
    monkeypatch.setattr(sheet_authoring, "erc_census", moving)
    result = _tools()["move_text"](str(sheet), [{"texts": ["N0"], "offset": [1.27, 0]}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "erc_unchanged", result
    assert _bytes(sheet) == before


def test_delete_text_erc_check_fires_when_the_census_moves(sheet, monkeypatch):
    real = sheet_authoring.erc_census
    calls = {"n": 0}

    def moving(root):
        calls["n"] += 1
        census = real(root)
        if calls["n"] == 2:       # the second census is the one with the change
            census[("invented", "error")] += 1
        return census

    before = _bytes(sheet)
    monkeypatch.setattr(sheet_authoring, "erc_census", moving)
    result = _tools()["delete_text"](str(sheet), [{"texts": ["N0"]}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "erc_unchanged", result
    assert _bytes(sheet) == before


# --------------------------------------------------------------------------- the out-of-page note of check_project


def _fake_conn():
    return lambda _path: {"status": "ok"}


def _erc_clean(proj, monkeypatch):
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"sheets": [
        {"path": "/", "uuid_path": p, "violations": []} for _f, p in project_check.sheet_tree.walk(proj / "demo.kicad_sch").uses]})


def _check(proj, checks=None):
    return project_check.check_project(str(proj / "demo.kicad_sch"), None, checks, _fake_conn())


def test_check_project_lists_the_objects_outside_the_page_and_says_what_it_does_not_measure(proj, monkeypatch):
    _erc_clean(proj, monkeypatch)
    res = _check(proj)
    sheet = res["sheets"][0]
    note = sheet["outside_page"]
    assert note["page_mm"] == [297.0, 210.0] and note["count"] == 9, note             # N0..N7 and the text box
    assert note["by_kind"] == {"text": 8, "text_box": 1} and len(note["listed"]) == 9 and note["truncated"] is False
    assert {e["label"] for e in note["listed"]} == {*(f"N{i}" for i in range(8)), "BOX"}
    assert any("extent of any text" in line for line in note["not_measured"])
    assert res["totals"]["outside_page"] == 9


def test_an_object_outside_the_page_makes_no_findings_false_but_changes_neither_status_nor_ok(proj, monkeypatch):
    _erc_clean(proj, monkeypatch)
    res = _check(proj)
    assert res["status"] == "ok" and res["ok"] is True and res["covered"] is True and res["no_findings"] is False
    assert res["sheets"][0]["outside_page"]["severity"] == "warning"


def test_after_move_text_check_project_is_clean(proj, monkeypatch, tools):
    _erc_clean(proj, monkeypatch)
    assert tools["move_text"](str(proj / "demo.kicad_sch"), [{"region": [0, 220, 297, 260], "offset": [0, -150]}], snap=False)["ok"] is True
    res = _check(proj)
    assert res["sheets"][0]["outside_page"]["count"] == 0 and res["totals"]["outside_page"] == 0
    assert res["no_findings"] is True and res["status"] == "ok"


def test_the_listing_is_capped_at_50_with_the_full_count(proj, monkeypatch, tools):
    _erc_clean(proj, monkeypatch)
    sch = proj / "demo.kicad_sch"
    assert tools["add_text"](str(sch), [{"x": 20.0, "y": 300.0 + i, "text": f"X{i}"} for i in range(60)], snap=False)["ok"] is True
    note = _check(proj)["sheets"][0]["outside_page"]
    assert note["count"] == 69 and len(note["listed"]) == 50 and note["truncated"] is True


def test_check_project_with_only_one_engine_still_reports_the_page_note(proj, monkeypatch):
    res = _check(proj, ["connectivity"])
    assert res["sheets"][0]["outside_page"]["count"] == 9 and res["no_findings"] is False


def test_an_unreadable_paper_is_reported_and_is_never_read_as_clean(proj, monkeypatch, tools):
    sch = proj / "demo.kicad_sch"
    root = write_layer.load(sch)
    write_layer.set_string(root.find_children("paper")[0], 0, "A6")                    # a paper KiCad cannot load
    from tests import _kicad_faults as faults

    faults.write_unchecked(root, sch)
    note = project_check.page_note(sch)
    assert note["checked"] is False and note["page_mm"] is None and note["count"] == 0 and "paper" in note["reason"], note
    res = _check(proj, ["connectivity"])
    assert res["no_findings"] is False


def test_an_object_that_cannot_be_measured_makes_the_sheet_unchecked_not_clean(proj, tools):
    sch = proj / "demo.kicad_sch"
    tools["move_text"](str(sch), [{"region": [0, 220, 297, 260], "offset": [0, -150]}], snap=False)
    assert project_check.page_note(sch)["checked"] is True
    root = write_layer.load(sch)
    root.children.insert(len(root.children) - 1, parse_string('(table (column_count 1) (uuid "00000000-0000-4000-8000-0000000000c1"))'))
    from tests import _kicad_faults as faults

    faults.write_unchecked(root, sch)
    note = project_check.page_note(sch)
    assert note["checked"] is False and note["unmeasured"][0]["kind"] == "table" and note["count"] == 0, note
    assert _check(proj, ["connectivity"])["totals"]["outside_page_unchecked_sheets"] == 1


def test_an_unreadable_sheet_file_is_unchecked_with_a_reason(sheet, tmp_path):
    from tests import _kicad_faults as faults

    faults.rewrite_bytes(sheet, lambda data: data[: len(data) // 2])        # truncated mid-file
    for path in (sheet, tmp_path / "missing.kicad_sch"):
        note = project_check.page_note(path)
        assert note["checked"] is False and note["page_mm"] is None and note["count"] == 0 and note["reason"], note


def test_an_unchecked_sheet_alone_makes_no_findings_false(proj, monkeypatch, tools):
    """Zero objects outside, ERC and connectivity clean: only the page check that could not be done blocks no_findings."""
    sch = proj / "demo.kicad_sch"
    tools["move_text"](str(sch), [{"region": [0, 220, 297, 260], "offset": [0, -150]}], snap=False)
    _erc_clean(proj, monkeypatch)
    assert _check(proj)["no_findings"] is True
    root = write_layer.load(sch)
    root.children.insert(len(root.children) - 1, parse_string('(table (column_count 1) (uuid "00000000-0000-4000-8000-0000000000c1"))'))
    from tests import _kicad_faults as faults

    faults.write_unchecked(root, sch)
    res = _check(proj)
    assert res["sheets"][0]["outside_page"]["checked"] is False and res["totals"]["outside_page"] == 0
    assert res["status"] == "ok" and res["no_findings"] is False, res


def test_check_project_stays_read_only_with_the_page_note(proj, monkeypatch):
    _erc_clean(proj, monkeypatch)
    before = _bytes(proj / "demo.kicad_sch")
    _check(proj)
    assert _bytes(proj / "demo.kicad_sch") == before


def test_the_tool_texts_state_the_contract():
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    texts = {t.name: " ".join(t.description.split()) for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    for phrase in ("text boxes", "use move_labels", "180 as 0", "there is no override",
                   "would_move", "outside_page_after", "by its anchor only", "file_open_in_kicad", "selector_matched_nothing", "erc_unchanged"):
        assert phrase in texts["move_text"], phrase
    for phrase in ("text boxes", "use delete_labels", "would_delete", "selector_matched_nothing", "not_found", "there is no override", "file_open_in_kicad"):
        assert phrase in texts["delete_text"], phrase
    assert "outside_page" in texts["check_project"] and "move_text" in texts["check_project"]


def test_the_skill_names_both_tools_and_the_page_note():
    skill = " ".join((Path(__file__).resolve().parents[1] / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8").split())
    for phrase in ("`move_text`", "`delete_text`", "`outside_page`", "text boxes"):
        assert phrase in skill, phrase
