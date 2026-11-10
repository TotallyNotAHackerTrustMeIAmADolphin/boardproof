"""set_title_block (GitHub issue 97): the title block of schematic sheets, with line breaks.

Fixtures are built through the tools and ``write_layer`` on ``tmp_path`` copies; nothing here edits
KiCad text. The encoding facts the tool rests on are measured here against kicad-cli 10 (the test
names say which) and recorded in docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli, sheet_authoring, sheet_editing, title_block, write_layer
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset

SExp = write_layer.SExp


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


def _set(sch: Path, items, **kw):
    return _tools()["set_title_block"](str(sch), items, **kw)


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    """A root with R1 and R2 joined by a labelled wire, a sub-sheet ``sensors.kicad_sch`` and one in a
    folder, ``sub/deep.kicad_sch`` (``create_sheets`` gives each a title block holding only its title)."""
    base = tmp_path_factory.mktemp("tb97")
    tools = _tools()
    made = project_toolset._project_create(str(base / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"}])["status"] == "ok"
    assert tools["add_wires"](str(sch), [
        {"p1": [50.8, 46.99], "p2": [50.8, 40.64], "route": "direct", "snap": False},
        {"p1": [50.8, 40.64], "p2": [76.2, 40.64], "route": "direct", "snap": False},
        {"p1": [76.2, 40.64], "p2": [76.2, 46.99], "route": "direct", "snap": False}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 63.5, "y": 40.64, "text": "NETA", "snap": False}])["status"] == "ok"
    (sch.parent / "sub").mkdir()
    assert tools["create_sheets"](str(sch), [{"file": "sensors.kicad_sch"}, {"file": "sub/deep.kicad_sch"}])["status"] == "ok"
    pins = [{"name": "SER", "direction": "input"}]
    added = tools["add_sheet_symbols"](str(sch), [
        {"file": "sensors.kicad_sch", "name": "Sensors", "x": 101.6, "y": 30.48, "pins": pins},
        {"file": "sub/deep.kicad_sch", "name": "Deep", "x": 101.6, "y": 76.2, "pins": pins}])
    assert added["status"] == "ok", added
    return base / "proj"


@pytest.fixture
def proj(template, tmp_path) -> Path:
    dest = tmp_path / "proj"
    shutil.copytree(template, dest)
    return dest


def _strip_title_block(sch: Path) -> None:
    """A sheet with no title block at all, made through the write layer on the test's copy."""
    root = write_layer.load(sch)
    root.children[:] = [c for c in root.children if c.is_atom or c.name != "title_block"]
    write_layer.save(root, sch)


def _sheet_files(proj: Path) -> dict[str, Path]:
    return {"root": proj / "demo.kicad_sch", "sensors": proj / "sensors.kicad_sch", "deep": proj / "sub" / "deep.kicad_sch"}


def _snapshot(proj: Path) -> dict:
    return {str(p.relative_to(proj)): p.read_bytes() for p in proj.rglob("*") if p.is_file()}


def _text(sch: Path) -> str:
    return sch.read_bytes().decode("utf-8")


def _fields(sch: Path) -> dict:
    return title_block.read_fields(write_layer.load(sch), sch.name)


def _nets(proj: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(proj / "demo.kicad_sch", None))
    return sheet_editing.read_nets(tree, proj / "demo.kicad_sch", None)


def _title_block_text(sch: Path) -> str:
    return re.search(r"\(title_block.*?\n\t\)", _text(sch), re.S).group(0)


# --------------------------------------------------------------------------- measured encoding (issue 97)


def test_a_line_break_is_stored_as_backslash_n_and_kicad_keeps_it_when_it_saves_the_sheet_itself(proj, tmp_path):
    """Measured, kicad-cli 10.0.7: KiCad writes LF in a title block string as the two characters backslash and n.
    The tool writes the same, never a raw line break; ``sch upgrade`` (KiCad's own writer) rewrites it unchanged."""
    sch = _sheet_files(proj)["root"]
    result = _set(sch, [{"comments": {"1": "line1\nline2"}}])
    assert result["ok"] is True and result["written"] is True, result
    block = _title_block_text(sch)
    assert '(comment 1 "line1\\nline2")' in block      # backslash + n
    assert "line1\r" not in block and "line1\n" not in block   # no raw break inside the string
    assert _fields(sch)["comment1"] == "line1\nline2"
    own = tmp_path / "own"
    own.mkdir()
    shutil.copy(sch, own / "x.kicad_sch")
    done = subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "upgrade", "--force", str(own / "x.kicad_sch")], capture_output=True, text=True)
    assert done.returncode == 0
    assert '(comment 1 "line1\\nline2")' in _title_block_text(own / "x.kicad_sch")


def test_the_drawing_sheet_prints_the_break_as_two_lines(proj, tmp_path):
    """Measured: the SVG export of the sheet holds the two lines as two text elements."""
    sch = _sheet_files(proj)["root"]
    assert _set(sch, [{"comments": {"1": "ZZfirst\nZZsecond"}}])["written"] is True
    out = tmp_path / "svg"
    out.mkdir()
    done = subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "export", "svg", "-o", str(out), str(sch)], capture_output=True, text=True)
    assert done.returncode == 0
    svg = "".join(f.read_text(encoding="utf-8", errors="replace") for f in out.glob("*.svg"))
    texts = re.findall(r">([^<>]*ZZ[^<>]*)<", svg)
    assert "ZZfirst" in texts and "ZZsecond" in texts and not any("ZZfirst" in t and "ZZsecond" in t for t in texts), texts


def test_kicad_drops_an_empty_field_when_it_saves_so_an_empty_text_removes_the_node(proj, tmp_path):
    """Measured: ``sch upgrade`` of a sheet with (company "") and (comment 2 "") writes neither; the tool removes them itself."""
    sch = _sheet_files(proj)["root"]
    assert '(company "")' in _title_block_text(sch)
    result = _set(sch, [{"company": "", "comments": {"1": None, "2": ""}, "rev": "B"}])
    assert result["ok"] is True, result
    block = _title_block_text(sch)
    assert "(company" not in block and "(comment" not in block and '(rev "B")' in block


def test_comment_numbers_outside_1_to_9_are_not_loadable_and_are_refused_before_anything_is_written(proj):
    sch = _sheet_files(proj)["root"]
    before = _snapshot(proj)
    for number in ("0", "10", "11"):
        result = _set(sch, [{"comments": {number: "x"}}])
        assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _snapshot(proj) == before


# --------------------------------------------------------------------------- the happy path


def test_one_call_sets_every_field_on_several_sheets_and_changes_nothing_else(proj):
    files = _sheet_files(proj)
    nets_before = _nets(proj)
    others_before = {k: [write_layer.serialise(c, compact=True) for c in write_layer.load(f).children if c.is_atom or c.name != "title_block"] for k, f in files.items()}
    result = _set(files["root"], [
        {"title": "Main board", "date": "2026-11-10", "rev": "2", "company": "Acme",
         "comments": {"1": "first\nsecond\nthird", "9": "last"}},
        {"sheet": "sensors.kicad_sch", "title": "Sensors"},
        {"sheet": "sub/deep.kicad_sch", "comments": {"4": "deep"}},
    ])
    assert result["ok"] is True and result["written"] is True, result
    assert [s["sheet"] for s in result["sheets"]] == ["demo.kicad_sch", "sensors.kicad_sch", "sub/deep.kicad_sch"]
    assert result["connectivity"] == {"nets_before": len(nets_before), "nets_after": len(nets_before), "changed": False}
    assert result["erc"]["violations_before"] == result["erc"]["violations_after"]
    assert "1.27" in result["note"]
    assert _fields(files["root"]) == {"title": "Main board", "date": "2026-11-10", "rev": "2", "company": "Acme",
                                      "comment1": "first\nsecond\nthird", "comment9": "last"}
    assert _fields(files["sensors"]) == {"title": "Sensors"}
    assert _fields(files["deep"]) == {"title": "deep", "comment4": "deep"}
    assert _nets(proj) == nets_before
    for key, file in files.items():
        assert [write_layer.serialise(c, compact=True) for c in write_layer.load(file).children if c.is_atom or c.name != "title_block"] == others_before[key]


def test_a_sheet_without_a_title_block_gets_one_after_paper_in_kicads_field_order(proj):
    sensors = _sheet_files(proj)["sensors"]
    _strip_title_block(sensors)
    assert not [c for c in write_layer.load(sensors).children if not c.is_atom and c.name == "title_block"]
    assert _set(sensors, [{"comments": {"2": "c2", "1": "c1"}, "company": "Co", "title": "T", "rev": "1", "date": "d"}])["written"] is True
    root = write_layer.load(sensors)
    names = [c.name for c in root.children if not c.is_atom]
    assert names.index("title_block") == names.index("paper") + 1
    block = next(c for c in root.children if not c.is_atom and c.name == "title_block")
    assert [c.name for c in block.children] == ["title", "date", "rev", "company", "comment", "comment"]
    assert [c.get_int(0) for c in block.children[-2:]] == [1, 2]


def test_a_comment_added_between_existing_ones_lands_in_number_order(proj):
    sch = _sheet_files(proj)["root"]
    assert _set(sch, [{"comments": {"3": "three"}}])["written"] is True
    assert _set(sch, [{"comments": {"1": "one", "2": "two"}}])["written"] is True
    block = next(c for c in write_layer.load(sch).children if not c.is_atom and c.name == "title_block")
    assert [c.get_int(0) for c in block.children if c.name == "comment"] == [1, 2, 3]


def test_text_that_looks_like_a_number_is_written_as_a_string(proj):
    sch = _sheet_files(proj)["root"]
    assert _set(sch, [{"title": "007", "rev": "2", "date": "1e5", "comments": {"1": "-3.5"}}])["written"] is True
    block = _title_block_text(sch)
    for needle in ('(title "007")', '(rev "2")', '(date "1e5")', '(comment 1 "-3.5")'):
        assert needle in block


def test_quotes_backslashes_tabs_and_unicode_survive(proj):
    sch = _sheet_files(proj)["root"]
    text = 'say "hi" back\\slash\ttab Größe µ 日本'
    assert _set(sch, [{"title": text}])["written"] is True
    assert _fields(sch)["title"] == text


def test_a_sub_sheet_alone_is_edited_through_sch_path(proj):
    files = _sheet_files(proj)
    before = _snapshot(proj)
    assert _set(files["deep"], [{"title": "Deep sheet"}], root_sch=str(files["root"]))["written"] is True
    after = _snapshot(proj)
    changed = [k for k in after if after[k] != before[k]]
    assert changed == [str(Path("sub") / "deep.kicad_sch")]


def test_the_sheet_key_is_found_from_the_project_folder_and_from_the_sheet_path_folder(proj):
    files = _sheet_files(proj)
    assert _set(files["deep"], [{"sheet": "deep.kicad_sch", "title": "A"}], root_sch=str(files["root"]))["written"] is True
    assert _fields(files["deep"]) == {"title": "A"}
    assert _set(files["root"], [{"sheet": str(files["deep"]), "title": "B"}])["written"] is True      # absolute
    assert _fields(files["deep"]) == {"title": "B"}


# --------------------------------------------------------------------------- dry run and no-op


def test_a_dry_run_reports_everything_and_writes_nothing(proj):
    files = _sheet_files(proj)
    before = _snapshot(proj)
    result = _set(files["root"], [{"title": "T", "comments": {"1": "a\nb"}}, {"sheet": "sensors.kicad_sch", "rev": "9"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and result["dry_run"] is True, result
    assert result["count"] == 3 and result["connectivity"]["changed"] is False
    assert _snapshot(proj) == before


def test_asking_for_what_is_already_there_writes_nothing(proj):
    sch = _sheet_files(proj)["root"]
    again = _snapshot(proj)   # project_create writes title demo, date 2025-01, rev A
    result = _set(sch, [{"title": "demo", "rev": "A", "date": "2025-01"}])
    assert result["ok"] is True and result["written"] is False and result["count"] == 0, result
    assert sorted(result["sheets"][0]["unchanged"]) == ["date", "rev", "title"]
    assert _snapshot(proj) == again


def test_a_text_set_twice_in_a_row_is_the_second_call_a_no_op(proj):
    sch = _sheet_files(proj)["root"]
    assert _set(sch, [{"comments": {"1": "a\nb"}}])["written"] is True
    again = _snapshot(proj)
    second = _set(sch, [{"comments": {"1": "a\nb"}}])
    assert second["written"] is False and _snapshot(proj) == again


# --------------------------------------------------------------------------- text classes


def test_crlf_is_written_as_one_line_break_and_listed(proj):
    sch = _sheet_files(proj)["root"]
    result = _set(sch, [{"comments": {"1": "a\r\nb"}}])
    assert result["written"] is True and _fields(sch)["comment1"] == "a\nb"
    assert [w["code"] for w in result["warnings"]] == ["line_break_normalised"]
    assert "\\r" not in _title_block_text(sch)


def test_edge_white_space_is_written_and_listed(proj):
    sch = _sheet_files(proj)["root"]
    result = _set(sch, [{"title": " padded ", "comments": {"1": "tail\n"}}])
    assert result["written"] is True and _fields(sch)["title"] == " padded " and _fields(sch)["comment1"] == "tail\n"
    assert sorted(w["field"] for w in result["warnings"]) == ["items[0].comments[1]", "items[0].title"]
    assert {w["code"] for w in result["warnings"]} == {"title_block_edge_whitespace"}


@pytest.mark.parametrize("value, code", [
    ("a\rb", "invalid_spec"),                # a lone CR prints as a stray character
    ("a\x01b", "invalid_spec"),              # a control character KiCad would write raw
    ("a\ud800b", "invalid_spec"),            # a lone surrogate cannot be written
    (123, "invalid_spec"),
    (["x"], "invalid_spec"),
    ("x" * 4001, "invalid_spec"),
])
def test_text_that_cannot_be_written_refuses_the_call_and_writes_nothing(proj, value, code):
    sch = _sheet_files(proj)["root"]
    before = _snapshot(proj)
    for item in ({"title": value}, {"comments": {"1": value}}):
        result = _set(sch, [item])
        assert result["ok"] is False and result["code"] == code, result
    assert _snapshot(proj) == before


def test_the_longest_accepted_text_is_written(proj):
    sch = _sheet_files(proj)["root"]
    text = "\n".join(["x" * 99] * 40)[: title_block.MAX_CHARS]
    assert _set(sch, [{"comments": {"5": text}}])["written"] is True
    assert _fields(sch)["comment5"] == text


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("items, code", [
    ([], "invalid_spec"),
    ("title", "invalid_spec"),
    ([{}], "invalid_spec"),
    ([{"sheet": "sensors.kicad_sch"}], "invalid_spec"),
    ([{"colour": "red", "title": "x"}], "invalid_spec"),
    ([{"title": "x"}, "y"], "invalid_spec"),
    ([{"comments": ["x"]}], "invalid_spec"),
    ([{"comments": {"one": "x"}}], "invalid_spec"),
    ([{"comments": {"01": "x"}}], "invalid_spec"),
    ([{"comments": {"1": "x", 1: "y"}}], "invalid_spec"),
    ([{"sheet": "", "title": "x"}], "invalid_spec"),
    ([{"sheet": 5, "title": "x"}], "invalid_spec"),
    ([{"sheet": "nosuch.kicad_sch", "title": "x"}], "sheet_not_in_project"),
    ([{"sheet": "../outside.kicad_sch", "title": "x"}], "sheet_not_in_project"),
    ([{"title": "x"}, {"sheet": "demo.kicad_sch", "rev": "y"}], "invalid_spec"),   # the same sheet twice
])
def test_a_bad_request_refuses_and_writes_nothing(proj, items, code):
    before = _snapshot(proj)
    result = _set(_sheet_files(proj)["root"], items)
    assert result["ok"] is False and result["code"] == code, result
    assert _snapshot(proj) == before


def test_one_bad_item_stops_the_whole_batch_before_any_sheet_is_written(proj):
    before = _snapshot(proj)
    result = _set(_sheet_files(proj)["root"], [{"title": "good"}, {"sheet": "sensors.kicad_sch", "comments": {"0": "bad"}}])
    assert result["ok"] is False and result["code"] == "invalid_spec"
    assert _snapshot(proj) == before


def test_a_sheet_that_is_not_in_the_project_is_refused(proj, tmp_path):
    stray = tmp_path / "stray.kicad_sch"
    shutil.copy(_sheet_files(proj)["sensors"], stray)
    before = stray.read_bytes()
    result = _set(_sheet_files(proj)["root"], [{"sheet": str(stray), "title": "x"}])
    assert result["ok"] is False and result["code"] == "sheet_not_in_project", result
    assert stray.read_bytes() == before


def test_a_missing_file_and_a_non_boolean_dry_run_are_refused(proj):
    assert _set(proj / "nope.kicad_sch", [{"title": "x"}])["code"] == "file_not_found"
    assert _set(_sheet_files(proj)["root"], [{"title": "x"}], dry_run="yes")["code"] == "invalid_spec"


def _damage_title_block(sch: Path, how: str) -> None:
    root = write_layer.load(sch)
    block = next(c for c in root.children if not c.is_atom and c.name == "title_block")
    if how == "duplicate":
        block.children.append(SExp.list("title", write_layer.string("second")))
    elif how == "second_block":
        root.children.append(SExp.list("title_block", SExp.list("title", write_layer.string("again"))))
    elif how == "bare":
        block.children[0].children[-1] = write_layer.symbol("bare")
    write_layer.save(root, sch)


@pytest.mark.parametrize("how", ["duplicate", "second_block", "bare"])
def test_a_title_block_the_tool_cannot_read_exactly_is_refused_not_guessed_at(proj, how):
    sch = _sheet_files(proj)["root"]
    _damage_title_block(sch, how)
    before = _snapshot(proj)
    result = _set(sch, [{"title": "x"}])
    assert result["ok"] is False and result["code"] == "title_block_unusual", result
    assert _snapshot(proj) == before


def test_a_title_block_child_this_tool_does_not_know_is_refused_by_the_reader():
    root = SExp.list("kicad_sch", SExp.list("title_block", SExp.list("title", write_layer.string("t")), SExp.list("sheet_note", write_layer.string("x"))))
    with pytest.raises(sheet_authoring.SheetAuthoringError) as caught:
        title_block.read_fields(root, "x.kicad_sch")
    assert caught.value.code == "title_block_unusual"


def test_a_project_file_named_as_a_sheet_is_refused(proj):
    pcb = proj / "demo.kicad_pcb"
    before = pcb.read_bytes()
    result = _set(proj / "demo.kicad_sch", [{"sheet": str(pcb), "title": "x"}])
    assert result["ok"] is False and result["code"] == "sheet_not_in_project", result
    assert pcb.read_bytes() == before


def test_a_kicad_lock_file_refuses_the_call(proj):
    sch = _sheet_files(proj)["root"]
    lock = sch.with_name(f"~{sch.name}.lck")
    lock.write_text('{"hostname": "other-host", "username": "someone"}', encoding="utf-8")
    before = _snapshot(proj)
    result = _set(sch, [{"title": "x"}])
    assert result["ok"] is False and result["code"] == "file_open_in_kicad", result
    assert _snapshot(proj) == before


# --------------------------------------------------------------------------- the self-checks (each fires on a broken write)


def test_a_corrupted_write_is_refused_and_leaves_the_sheet_alone(proj, monkeypatch):
    sch = _sheet_files(proj)["root"]
    real = write_layer.load
    before = _snapshot(proj)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name and Path(path).parent == sch.parent:   # the real write, not the scratch copies
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _set(sch, [{"title": "X"}])
    assert result["ok"] is False and result["code"] == "write_not_verified", result
    assert result.get("check") == "reload_equals_serialised"
    monkeypatch.undo()
    assert _snapshot(proj) == before


def test_the_staged_check_fires_when_the_edit_touches_more_than_the_title_block(proj, monkeypatch):
    sch = _sheet_files(proj)["root"]
    real = title_block.apply_fields

    def greedy(root, changes, who):
        real(root, changes, who)
        symbol = next(c for c in root.children if not c.is_atom and c.name == "symbol")
        write_layer.set_string(symbol.find_children("property")[0], 1, "STRAY")

    monkeypatch.setattr(title_block, "apply_fields", greedy)
    before = _snapshot(proj)
    result = _set(sch, [{"title": "X"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "staged", result
    assert _snapshot(proj) == before


def test_the_staged_check_fires_when_the_title_block_is_not_what_was_asked(proj, monkeypatch):
    sch = _sheet_files(proj)["root"]
    real = title_block.apply_fields
    monkeypatch.setattr(title_block, "apply_fields", lambda root, changes, who: real(root, {k: (v + "!" if v else v) for k, v in changes.items()}, who))
    before = _snapshot(proj)
    result = _set(sch, [{"title": "X"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "staged", result
    assert _snapshot(proj) == before


def test_the_netlist_check_fires_when_kicad_shows_other_text_than_asked(proj, monkeypatch):
    real = title_block._read_netlist

    def lying(root):
        nets, blocks = real(root)
        return nets, [(src, {**f, "comment1": "other"} if "comment1" in f else f) for src, f in blocks]

    monkeypatch.setattr(title_block, "_read_netlist", lying)
    before = _snapshot(proj)
    result = _set(_sheet_files(proj)["root"], [{"comments": {"1": "a\nb"}}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "netlist_follows_edit", result
    assert _snapshot(proj) == before


def test_the_netlist_check_fires_when_the_netlist_shows_a_title_block_no_sheet_file_holds(proj, monkeypatch):
    real = title_block._read_netlist
    monkeypatch.setattr(title_block, "_read_netlist", lambda root: (lambda r: (r[0], [*r[1], ("sensors.kicad_sch", {"title": "ghost"})]))(real(root)))
    before = _snapshot(proj)
    result = _set(_sheet_files(proj)["root"], [{"title": "X"}])
    assert result["ok"] is False and result["check"] == "netlist_follows_edit" and result["netlist_title_block"] == {"title": "ghost"}, result
    assert _snapshot(proj) == before


def test_the_netlist_check_fires_when_the_edited_sheet_is_missing_from_the_netlist(proj, monkeypatch):
    real = title_block._read_netlist
    monkeypatch.setattr(title_block, "_read_netlist", lambda root: (real(root)[0], []))
    before = _snapshot(proj)
    result = _set(_sheet_files(proj)["root"], [{"title": "gone"}])
    assert result["ok"] is False and result["check"] == "netlist_follows_edit", result
    assert _snapshot(proj) == before


def test_the_nets_check_fires_when_the_nets_differ_after_the_edit(proj, monkeypatch):
    real = title_block._read_netlist
    calls = []

    def changed(root):
        nets, blocks = real(root)
        calls.append(1)
        if len(calls) == 2:
            nets = {**nets, "NEW": frozenset({("R1", "1")})}
        return nets, blocks

    monkeypatch.setattr(title_block, "_read_netlist", changed)
    before = _snapshot(proj)
    result = _set(_sheet_files(proj)["root"], [{"title": "X"}])
    assert result["ok"] is False and result["check"] == "netlist_unchanged", result
    assert _snapshot(proj) == before


def test_the_erc_check_fires_when_the_violation_counts_differ(proj, monkeypatch):
    real = sheet_authoring.erc_census
    calls = []

    def census(root):
        out = real(root)
        calls.append(1)
        return out + Counter({("extra", "error"): 1}) if len(calls) == 2 else out

    monkeypatch.setattr(sheet_authoring, "erc_census", census)
    before = _snapshot(proj)
    result = _set(_sheet_files(proj)["root"], [{"title": "X"}])
    assert result["ok"] is False and result["check"] == "erc_unchanged", result
    assert _snapshot(proj) == before


def test_a_failed_second_write_leaves_the_first_sheet_with_its_old_title_block(proj, monkeypatch):
    files = _sheet_files(proj)
    old = _fields(files["root"])
    real = write_layer.save

    def failing(root, path, **kw):
        if Path(path).resolve() == files["sensors"].resolve() and kw.get("verify") is not None:
            raise write_layer.WriteLayerError("parse_check_failed", "forced for the test")
        return real(root, path, **kw)

    monkeypatch.setattr(write_layer, "save", failing)
    result = _set(files["root"], [{"title": "A"}, {"sheet": "sensors.kicad_sch", "title": "B"}])
    monkeypatch.undo()
    assert result["ok"] is False, result
    assert _fields(files["root"]) == old and _fields(files["sensors"]) == {"title": "sensors"}


# --------------------------------------------------------------------------- the tool text an LLM reads


def test_the_tool_text_states_the_facts_the_tests_pin():
    description = _description()
    for needle in ("title, date,", "comments 1 to 9", "\\n", "backslash and n", "CRLF", "lone CR", "4000", "1.27 mm", "dry_run",
                   "write_not_verified", "title_block_unusual", "line_break_normalised", "title_block_edge_whitespace",
                   "null", "ERC", "netlist", "file_open_in_kicad", "root_sch", "restored"):
        assert needle in description, needle


def test_every_refusal_code_the_tool_text_names_exists_in_the_code():
    source = Path(title_block.__file__).read_text(encoding="utf-8")
    for code in ("invalid_spec", "write_not_verified", "title_block_unusual", "line_break_normalised", "title_block_edge_whitespace", "sheet_not_in_project"):
        assert f'"{code}"' in source, code


def _description() -> str:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return next(t.description for t in mcp._tool_manager.list_tools() if t.name == "set_title_block")  # type: ignore[attr-defined]
