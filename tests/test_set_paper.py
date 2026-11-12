"""set_paper (GitHub issue 197): the paper size of schematic sheets, batch, with a fit report.

Fixtures are built through the tools and ``write_layer`` on ``tmp_path`` copies; nothing here edits KiCad
text. The paper facts the tool rests on are measured here against kicad-cli 10 (the test names say which)
and recorded in docs/ARCHITECTURE.md.
"""

from __future__ import annotations

import copy
import re
import shutil
import subprocess
import uuid as uuidlib
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from tests import _kicad_faults as faults
from boardproof import kicad_cli, sheet_authoring, sheet_editing, sheet_extents, sheet_paper, title_block, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

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
    return _tools()["set_paper"](str(sch), items, **kw)


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    """A root with R1 and R2 joined by a labelled wire, a sub-sheet ``sensors.kicad_sch`` and one in a
    folder, ``sub/deep.kicad_sch``; everything lies in the top left 160 x 100 mm of an A4 page."""
    base = tmp_path_factory.mktemp("pp197")
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


def _files(proj: Path) -> dict[str, Path]:
    return {"root": proj / "demo.kicad_sch", "sensors": proj / "sensors.kicad_sch", "deep": proj / "sub" / "deep.kicad_sch"}


def _snapshot(proj: Path) -> dict:
    return {str(p.relative_to(proj)): p.read_bytes() for p in proj.rglob("*") if p.is_file()}


def _raw_paper(sch: Path) -> str:
    """The raw (paper ..) token as it stands in the file text."""
    found = re.findall(r"^\t\(paper [^\n]*\)$", sch.read_text(encoding="utf-8"), re.M)
    assert len(found) == 1, found
    return found[0].strip()


def _svg_page(sch: Path, tmp_path: Path) -> tuple[float, float]:
    out = tmp_path / ("svg-" + uuidlib.uuid4().hex[:6])
    done = subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "export", "svg", "-o", str(out), str(sch)], capture_output=True, text=True)
    assert done.returncode == 0, done.stderr
    svg = next(f for f in out.glob("*.svg") if f.stem == sch.stem)
    m = re.search(r'<svg[^>]*?\bwidth="([0-9.]+)mm"[^>]*?\bheight="([0-9.]+)mm"', svg.read_text(encoding="utf-8")[:3000], re.S)
    return float(m.group(1)), float(m.group(2))


def _upgrade_ok(sch: Path, tmp_path: Path) -> bool:
    copy_ = tmp_path / ("up-" + uuidlib.uuid4().hex[:6] + ".kicad_sch")
    shutil.copy(sch, copy_)
    return subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "upgrade", "--force", str(copy_)], capture_output=True, text=True).returncode == 0


def _nets(proj: Path) -> dict:
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(proj / "demo.kicad_sch", None))
    return sheet_editing.read_nets(tree, proj / "demo.kicad_sch", None)


def _with_paper(sch: Path, parts: list) -> None:
    """Put a paper token KiCad's own writer would not make onto a scratch sheet, through the object model."""
    root = write_layer.load(sch)
    node = SExp.list("paper")
    for p in parts:
        node.children.append(write_layer.number(p) if isinstance(p, (int, float)) else (write_layer.symbol(p) if p in ("portrait", "landscape") else write_layer.string(p)))
    root.children[root.children.index(root.find_children("paper")[0])] = node
    write_layer.save(root, sch)


def _put_unloadable(sch: Path, parts: list) -> None:
    """A paper token KiCad cannot load. ``write_layer.save`` refuses to write such a sheet (its parse check), so the
    sanctioned fault helper writes it (tests/_kicad_faults.py)."""
    root = write_layer.load(sch)
    node = SExp.list("paper")
    for p in parts:
        node.children.append(write_layer.number(p) if isinstance(p, (int, float)) else (write_layer.symbol(p) if p in ("portrait", "landscape") else write_layer.string(p)))
    root.children[root.children.index(root.find_children("paper")[0])] = node
    faults.write_unchecked(root, sch)


def _blank(tmp_path: Path, name: str) -> Path:
    """A one-sheet scratch file (no sub-sheets) made through the object model."""
    path = tmp_path / f"{name}.kicad_sch"
    write_layer.save(sheet_authoring.empty_sheet("t"), path)
    return path


# --------------------------------------------------------------------------- measured paper facts (kicad-cli 10.0.7)


@pytest.mark.parametrize("size, portrait, page", [
    ("A5", False, (210.0, 148.0)), ("A4", False, (297.0, 210.0)), ("A3", False, (420.0, 297.0)), ("A2", False, (594.0, 420.0)),
    ("A1", False, (841.0, 594.0)), ("A0", False, (1189.0, 841.0)), ("A", False, (279.4, 215.9)), ("B", False, (431.8, 279.4)),
    ("C", False, (558.8, 431.8)), ("D", False, (863.6, 558.8)), ("E", False, (1117.6, 863.6)), ("USLetter", False, (279.4, 215.9)),
    ("USLegal", False, (355.6, 215.9)), ("USLedger", False, (431.8, 279.4)), ("A4", True, (210.0, 297.0)), ("USLegal", True, (215.9, 355.6)),
])
def test_every_named_size_has_the_page_kicad_draws_and_the_token_kicad_writes(proj, tmp_path, size, portrait, page):
    """Measured: the SVG export has these page sizes (to a mil), and the token is (paper "NAME") or (paper "NAME" portrait)."""
    sch = _files(proj)["root"]
    result = _set(sch, [{"size": size, "portrait": portrait}])
    assert result["ok"] is True and result["written"] is not (size == "A4" and not portrait), result     # the sheet is A4 already
    assert _raw_paper(sch) == (f'(paper "{size}" portrait)' if portrait else f'(paper "{size}")')
    got = _svg_page(sch, tmp_path)
    assert got == pytest.approx(page, abs=0.05)
    assert result["sheets"][0]["page_mm_to"] == pytest.approx(list(page))
    assert _upgrade_ok(sch, tmp_path)


def test_a_user_size_is_width_then_height_and_kicad_rounds_it_to_a_mil(proj, tmp_path):
    sch = _files(proj)["root"]
    result = _set(sch, [{"size": "User", "width": 300, "height": 200}])
    assert result["ok"] is True, result
    assert _raw_paper(sch) == '(paper "User" 300 200)'
    assert _svg_page(sch, tmp_path) == pytest.approx((300.0, 200.0), abs=0.05)
    assert result["sheets"][0]["page_mm_to"] == [300.0, 200.0]
    assert _set(sch, [{"size": "User", "width": 210.0071, "height": 229.9}])["ok"] is True
    assert _raw_paper(sch) == '(paper "User" 210.0071 229.9)'
    assert _svg_page(sch, tmp_path) == pytest.approx((210.0071, 229.9), abs=0.05)     # 209.98 x 229.8954: a mil of rounding


def test_kicad_reads_a_user_portrait_flag_as_a_swap_and_never_writes_it_back(tmp_path, proj):
    """Measured: (paper "User" 300 200 portrait) is a 200 x 300 page and sch upgrade writes (paper "User" 200 300).
    So the tool takes the page as it is and refuses the flag with User."""
    sch = _files(proj)["root"]
    scratch = _blank(tmp_path, "u")
    _with_paper(scratch, ["User", 300, 200, "portrait"])
    assert sheet_extents.page_size_mm(write_layer.load(scratch)) == (200.0, 300.0)
    assert _svg_page(scratch, tmp_path) == pytest.approx((200.0, 300.0), abs=0.05)
    older = write_layer.load(scratch)       # an older sheet, so that KiCad rewrites it
    write_layer.set_number(older.find_children("version")[0], 0, 20231120)
    write_layer.save(older, scratch)
    assert subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "upgrade", "--force", str(scratch)], capture_output=True, text=True).returncode == 0
    assert re.search(r'\(paper "User" 200 300\)', scratch.read_text(encoding="utf-8"))
    before = _snapshot(proj)
    result = _set(sch, [{"size": "User", "width": 300, "height": 200, "portrait": True}])
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _snapshot(proj) == before


def test_kicad_clamps_a_user_size_silently_so_the_tool_refuses_what_it_would_clamp(proj, tmp_path):
    """Measured: "User" 10 10 draws 25.4 x 25.4 and 5000 draws 3048."""
    scratch = _blank(tmp_path, "c")
    _with_paper(scratch, ["User", 10, 5000])
    assert _svg_page(scratch, tmp_path) == pytest.approx((25.4, 3048.0), abs=0.05)
    before = _snapshot(proj)
    for w, h in ((10, 200), (300, 5000), (25.39, 200), (300, 3048.1)):
        result = _set(_files(proj)["root"], [{"size": "User", "width": w, "height": h}])
        assert result["ok"] is False and result["code"] == "invalid_spec", (w, h, result)
    assert _set(_files(proj)["root"], [{"size": "User", "width": 25.4, "height": 3048}], dry_run=True)["ok"] is True
    assert _snapshot(proj) == before


@pytest.mark.parametrize("parts", [["A6"], ["Letter"], ["Tabloid"], ["User"], ["User", 5], ["A4", "landscape"], ["A4", 300, 200]])
def test_tokens_kicad_cannot_load_are_exit_3_and_the_tool_refuses_to_start_from_them(proj, tmp_path, parts):
    """Measured: each of these makes sch upgrade exit 3 (cannot load)."""
    sch = _files(proj)["root"]
    _put_unloadable(sch, parts)
    assert not _upgrade_ok(sch, tmp_path)
    before = _snapshot(proj)
    result = _set(sch, [{"size": "A3"}])
    assert result["ok"] is False and result["code"] == "paper_unusual", result
    assert _snapshot(proj) == before


def test_kicad_reads_a_lower_case_name_and_the_tool_rewrites_it_canonical(proj, tmp_path):
    sch = _files(proj)["root"]
    _with_paper(sch, ["a4"])
    assert _svg_page(sch, tmp_path) == pytest.approx((297.0, 210.0), abs=0.05)
    result = _set(sch, [{"size": "A4"}])
    assert result["ok"] is True and result["written"] is True and result["sheets"][0]["action"] == "changed", result
    assert _raw_paper(sch) == '(paper "A4")'


def test_the_demo_sheets_of_kicad_use_exactly_these_forms():
    demos = Path(str(kicad_cli.find_kicad_cli())).parent.parent / "share" / "kicad" / "demos"
    if not demos.is_dir():
        pytest.skip("KiCad demos not installed")
    found = set()
    for sch in demos.rglob("*.kicad_sch"):
        found.update(re.findall(r"\(paper [^)]*\)", sch.read_text(encoding="utf-8", errors="replace")))
    assert '(paper "A4")' in found and '(paper "A3")' in found
    assert all(re.fullmatch(r'\(paper "(A[0-5]|[A-E]|USLetter|USLegal|USLedger)"( portrait)?\)|\(paper "User" [0-9.]+ [0-9.]+\)', f) for f in found), found


# --------------------------------------------------------------------------- the tool


def test_one_call_changes_the_paper_of_several_sheets_and_nothing_else(proj, tmp_path):
    files = _files(proj)
    before_nets = _nets(proj)
    others = {k: {c for c in map(lambda n: write_layer.serialise(n, compact=True), write_layer.load(f).children) if not c.startswith("(paper")} for k, f in files.items()}
    assert [_raw_paper(f) for f in files.values()] == ['(paper "A4")'] * 3
    result = _set(files["root"], [{"size": "A3"}, {"sheet": "sensors.kicad_sch", "size": "A4", "portrait": True}, {"sheet": "sub/deep.kicad_sch", "size": "User", "width": 150, "height": 100}])
    assert result["ok"] is True and result["written"] is True and result["count"] == 3, result
    assert [s["sheet"] for s in result["sheets"]] == ["demo.kicad_sch", "sensors.kicad_sch", "sub/deep.kicad_sch"]
    assert [s["paper_from"] for s in result["sheets"]] == ['(paper "A4")'] * 3
    assert [s["paper_to"] for s in result["sheets"]] == ['(paper "A3")', '(paper "A4" portrait)', '(paper "User" 150 100)']
    assert [_raw_paper(f) for f in files.values()] == ['(paper "A3")', '(paper "A4" portrait)', '(paper "User" 150 100)']
    for k, f in files.items():
        assert {c for c in map(lambda n: write_layer.serialise(n, compact=True), write_layer.load(f).children) if not c.startswith("(paper")} == others[k]
        assert _upgrade_ok(f, tmp_path)
    assert _nets(proj) == before_nets and result["connectivity"] == {"nets_before": len(before_nets), "nets_after": len(before_nets), "changed": False}
    assert _svg_page(files["root"], tmp_path) == pytest.approx((420.0, 297.0), abs=0.05)


def test_the_paper_node_keeps_its_place_among_the_sheets_children(proj):
    sch = _files(proj)["root"]
    order = [c.name for c in write_layer.load(sch).children if not c.is_atom]
    assert _set(sch, [{"size": "A2", "portrait": True}])["written"] is True
    assert [c.name for c in write_layer.load(sch).children if not c.is_atom] == order


def test_a_dry_run_reports_everything_and_writes_nothing(proj):
    before = _snapshot(proj)
    result = _set(_files(proj)["root"], [{"size": "A5"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False and result["dry_run"] is True and result["count"] == 1, result
    assert result["sheets"][0]["paper_to"] == '(paper "A5")' and "connectivity" in result and "erc" in result
    assert _snapshot(proj) == before


def test_asking_for_the_paper_already_there_writes_nothing(proj):
    before = _snapshot(proj)
    result = _set(_files(proj)["root"], [{"size": "A4"}, {"sheet": "sensors.kicad_sch", "size": "A4", "portrait": False}])
    assert result["ok"] is True and result["written"] is False and result["count"] == 0, result
    assert [s["action"] for s in result["sheets"]] == ["unchanged", "unchanged"]
    assert _snapshot(proj) == before


def test_a_second_identical_call_is_a_no_op(proj):
    assert _set(_files(proj)["root"], [{"size": "A3"}])["written"] is True
    again = _set(_files(proj)["root"], [{"size": "A3"}])
    assert again["ok"] is True and again["written"] is False and again["count"] == 0


def test_a_sub_sheet_alone_is_edited_through_sch_path(proj):
    result = _set(_files(proj)["sensors"], [{"size": "A5"}])
    assert result["ok"] is True and result["written"] is True, result
    assert _raw_paper(_files(proj)["sensors"]) == '(paper "A5")' and _raw_paper(_files(proj)["root"]) == '(paper "A4")'


def test_a_sheet_used_twice_is_one_file_and_is_changed_once(proj):
    files = _files(proj)
    root = write_layer.load(files["root"])
    sheets = [c for c in root.children if not c.is_atom and c.name == "sheet"]
    twin = copy.deepcopy(next(s for s in sheets if "sensors.kicad_sch" in write_layer.serialise(s, compact=True)))
    for node in [twin, *twin.children]:
        if not node.is_atom and node.name == "uuid":
            write_layer.set_string(node, 0, str(uuidlib.uuid4()))
    for prop in twin.find_children("property"):
        if prop.get_string(0) == "Sheetname":
            write_layer.set_string(prop, 1, "Sensors2")
        if prop.get_string(0) in ("Sheetfile", "Sheet file"):
            pass
    for at in twin.find_children("at"):
        write_layer.set_number(at, 1, at.get_float(1) + 60)
    root.children.insert(root.children.index(sheets[-1]) + 1, twin)
    write_layer.save(root, files["root"])
    tree = sheet_authoring._require_tree(sheet_authoring.find_root(files["root"], None))
    assert sum(1 for f in tree.files if f.name == "sensors.kicad_sch") == 1 and sum(1 for _f, _n in tree.uses if _f.name == "sensors.kicad_sch") == 2
    result = _set(files["root"], [{"sheet": "sensors.kicad_sch", "size": "A3"}])
    assert result["ok"] is True and result["count"] == 1 and len(result["sheets"]) == 1, result
    assert _raw_paper(files["sensors"]) == '(paper "A3")'
    twice = _set(files["root"], [{"sheet": "sensors.kicad_sch", "size": "A3"}, {"sheet": "sensors.kicad_sch", "size": "A2"}])
    assert twice["ok"] is False and twice["code"] == "invalid_spec", twice


# --------------------------------------------------------------------------- the fit report


@pytest.fixture
def wide(proj) -> Path:
    """The root sheet with objects far to the right (x 215..290 mm): they fit A4 (297 mm) and not A5 (210 mm)."""
    sch = _files(proj)["root"]
    tools = _tools()
    assert tools["add_wires"](str(sch), [{"p1": [215.9, 100.33], "p2": [289.56, 100.33], "route": "direct", "snap": False}])["status"] == "ok"
    assert tools["add_text"](str(sch), [{"text": "far note", "x": 250.19, "y": 120.65}])["status"] in ("ok",)
    assert tools["add_junctions"](str(sch), [[228.6, 100.33]])["status"] == "ok"
    return sch


def test_a_sheet_that_does_not_fit_is_reported_with_the_objects_outside_and_a_warning(wide):
    before = wide.read_bytes()
    result = _set(wide, [{"size": "A5"}], dry_run=True)
    assert result["ok"] is True and result["written"] is False, result
    fit = result["sheets"][0]["fit"]
    assert fit["outside_page"]["count"] == 3 and fit["outside_page"]["by_kind"] == {"junction": 1, "text": 1, "wire": 1}, fit
    kinds = {e["kind"]: e for e in fit["outside_page"]["listed"]}
    assert kinds["wire"]["x1"] == 215.9 and kinds["wire"]["x2"] == 289.56 and kinds["wire"]["y1"] == 100.33
    assert kinds["text"]["label"] == "far note" and (kinds["text"]["x1"], kinds["text"]["y1"]) == (250.19, 120.65)
    assert kinds["junction"]["x1"] == 228.6 and kinds["wire"]["uuid"] and kinds["wire"]["measured"] == "points"
    assert fit["extents"]["x2"] == 289.56 and fit["extents"]["x1"] < 60 and fit["outside_old_page_count"] == 0
    assert [w["code"] for w in result["warnings"]] == ["outside_page"] and result["warnings"][0]["count"] == 3 and result["warnings"][0]["sheet"] == "demo.kicad_sch"
    assert result["not_measured"] and any("text" in n for n in result["not_measured"])
    assert wide.read_bytes() == before


def test_the_same_objects_fit_a_bigger_paper_and_the_edge_counts_as_inside(wide, proj):
    for size in ("A4", "A3"):
        result = _set(wide, [{"size": size}], dry_run=True)
        assert result["sheets"][0]["fit"]["outside_page"]["count"] == 0 and result["warnings"] == [], size
    edge = _set(wide, [{"size": "User", "width": 289.56, "height": 150}], dry_run=True)       # the wire ends exactly on the edge
    assert edge["sheets"][0]["fit"]["outside_page"]["count"] == 0
    short = _set(wide, [{"size": "User", "width": 289.5, "height": 150}], dry_run=True)
    assert short["sheets"][0]["fit"]["outside_page"]["count"] == 1
    low = _set(wide, [{"size": "User", "width": 297, "height": 120.6}], dry_run=True)
    assert low["sheets"][0]["fit"]["outside_page"]["by_kind"] == {"text": 1}


def test_a_page_too_short_reports_the_objects_below_it_not_only_right_of_it(proj):
    result = _set(_files(proj)["root"], [{"size": "User", "width": 297, "height": 45}], dry_run=True)
    fit = result["sheets"][0]["fit"]
    assert {e["kind"] for e in fit["outside_page"]["listed"]} == {"symbol"} or fit["outside_page"]["count"] >= 2      # the resistors reach y 54.61
    assert fit["outside_page"]["count"] >= 2


def test_portrait_is_what_changes_the_fit(wide):
    assert _set(wide, [{"size": "A4", "portrait": False}], dry_run=True)["sheets"][0]["fit"]["outside_page"]["count"] == 0
    assert _set(wide, [{"size": "A4", "portrait": True}], dry_run=True)["sheets"][0]["fit"]["outside_page"]["count"] == 3


def test_the_outside_list_is_capped_at_50_with_the_total_count(proj):
    sch = _files(proj)["root"]
    points = [[215.9 + 1.27 * (i % 40), 20.32 + 1.27 * (i // 40) * 4] for i in range(120)]
    assert _tools()["add_junctions"](str(sch), points)["status"] == "ok"
    fit = _set(sch, [{"size": "A5"}], dry_run=True)["sheets"][0]["fit"]
    assert fit["outside_page"]["count"] == sum(1 for p in points if p[0] > 210) and fit["outside_page"]["count"] > 50
    assert len(fit["outside_page"]["listed"]) == 50 and fit["outside_page"]["truncated"] is True


def test_the_report_names_what_was_already_outside_the_old_page(wide):
    assert _set(wide, [{"size": "A5"}])["written"] is True          # written although objects are outside: a warning, not a refusal
    result = _set(wide, [{"size": "A5"}, ], dry_run=True)
    assert result["sheets"][0]["action"] == "unchanged"
    assert result["sheets"][0]["fit"]["outside_old_page_count"] == 3 == result["sheets"][0]["fit"]["outside_page"]["count"]


def test_an_object_outside_the_new_page_does_not_stop_the_write(wide):
    result = _set(wide, [{"size": "A5"}])
    assert result["ok"] is True and result["written"] is True and result["warnings"][0]["code"] == "outside_page", result
    assert _raw_paper(wide) == '(paper "A5")'


def test_an_object_that_cannot_be_measured_refuses_the_call(proj, monkeypatch):
    real = sheet_extents._measure
    monkeypatch.setattr(sheet_extents, "_measure", lambda root, node: (None, "none") if node.name == "junction" else real(root, node))
    sch = _files(proj)["root"]
    assert _tools()["add_junctions"](str(sch), [[60.96, 40.64]])["status"] == "ok"
    before = _snapshot(proj)
    result = _set(sch, [{"size": "A5"}], dry_run=True)
    assert result["ok"] is False and result["code"] == "object_not_classified" and result["total"] == 1 and result["objects"][0]["kind"] == "junction", result
    assert _snapshot(proj) == before
    assert _set(sch, [{"size": "A5"}])["code"] == "object_not_classified" and _snapshot(proj) == before


# --------------------------------------------------------------------------- refusals


@pytest.mark.parametrize("items", [
    None, [], {}, "A4", [None], [{}], [{"size": "A6"}], [{"size": "a4"}], [{"size": "Letter"}], [{"size": 4}], [{"size": None}],
    [{"size": "A4", "colour": 1}], [{"size": "A4", "sheet": ""}], [{"size": "A4", "sheet": 4}],
    [{"size": "A4", "portrait": "yes"}], [{"size": "A4", "portrait": 1}],
    [{"size": "A4", "width": 100}], [{"size": "A4", "height": 100}], [{"size": "A4", "width": 297, "height": 210}],
    [{"size": "User"}], [{"size": "User", "width": 100}], [{"size": "User", "height": 100}], [{"size": "User", "width": "100", "height": 100}],
    [{"size": "User", "width": True, "height": 100}], [{"size": "User", "width": float("nan"), "height": 100}],
    [{"size": "User", "width": float("inf"), "height": 100}], [{"size": "User", "width": 100, "height": None}],
    [{"size": "User", "width": 100, "height": 100, "portrait": True}],
])
def test_a_bad_request_refuses_and_writes_nothing(proj, items):
    before = _snapshot(proj)
    result = _set(_files(proj)["root"], items)
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert _snapshot(proj) == before


def test_one_bad_item_stops_the_whole_batch_before_any_sheet_is_written(proj):
    before = _snapshot(proj)
    result = _set(_files(proj)["root"], [{"size": "A3"}, {"sheet": "sensors.kicad_sch", "size": "A6"}])
    assert result["ok"] is False and result["code"] == "invalid_spec"
    assert _snapshot(proj) == before


def test_a_sheet_not_in_the_project_a_missing_file_and_a_non_boolean_dry_run_are_refused(proj, tmp_path):
    before = _snapshot(proj)
    assert _set(_files(proj)["root"], [{"sheet": "nope.kicad_sch", "size": "A3"}])["code"] == "sheet_not_in_project"
    assert _set(proj / "missing.kicad_sch", [{"size": "A3"}])["code"] == "file_not_found"
    assert _set(_files(proj)["root"], [{"size": "A3"}], dry_run="no")["code"] == "invalid_spec"
    pcb = proj / "demo.kicad_pcb"
    if pcb.exists():
        assert _set(_files(proj)["root"], [{"sheet": "demo.kicad_pcb", "size": "A3"}])["code"] == "sheet_not_in_project"
    assert _snapshot(proj) == before


def test_a_sheet_with_two_paper_nodes_or_none_is_refused_not_guessed_at(proj):
    sch = _files(proj)["root"]
    root = write_layer.load(sch)
    root.children.insert(root.children.index(root.find_children("paper")[0]) + 1, SExp.list("paper", write_layer.string("A3")))
    write_layer.save(root, sch, verify=None)
    before = _snapshot(proj)
    result = _set(sch, [{"size": "A4"}])
    assert result["ok"] is False and result["code"] == "paper_unusual", result
    root = write_layer.load(sch)
    root.children[:] = [c for c in root.children if c.is_atom or c.name != "paper"]
    write_layer.save(root, sch, verify=None)
    before = _snapshot(proj)
    result = _set(sch, [{"size": "A4"}])
    assert result["ok"] is False and result["code"] == "paper_unusual", result
    assert _snapshot(proj) == before


def test_a_kicad_lock_file_refuses_the_call(proj):
    sch = _files(proj)["root"]
    sch.with_name(f"~{sch.name}.lck").write_text('{"hostname": "other-host", "username": "someone"}', encoding="utf-8")
    before = _snapshot(proj)
    result = _set(sch, [{"size": "A3"}])
    assert result["ok"] is False and result["code"] == "file_open_in_kicad", result
    assert _snapshot(proj) == before
    assert _set(sch, [{"size": "A3"}], dry_run=True)["ok"] is True       # a dry run writes nothing, so a lock does not matter


def test_the_lock_refusal_comes_before_any_kicad_cli_work(proj, monkeypatch):
    sch = _files(proj)["root"]
    sch.with_name(f"~{sch.name}.lck").write_text('{"hostname": "other-host", "username": "someone"}', encoding="utf-8")

    def boom(root):
        raise AssertionError("kicad-cli was run although the sheet is open in KiCad")

    monkeypatch.setattr(title_block, "_read_netlist", boom)
    assert _set(sch, [{"size": "A3"}])["code"] == "file_open_in_kicad"


# --------------------------------------------------------------------------- the self-checks (each fires on a broken write)


def test_a_corrupted_write_is_refused_and_leaves_the_sheet_alone(proj, monkeypatch):
    sch = _files(proj)["root"]
    real = write_layer.load
    before = _snapshot(proj)

    def corrupt(path):
        root = real(path)
        if ".tmp" in Path(path).name and Path(path).parent == sch.parent:
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "load", corrupt)
    result = _set(sch, [{"size": "A3"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result.get("check") == "reload_equals_serialised", result
    monkeypatch.undo()
    assert _snapshot(proj) == before


def test_the_staged_check_fires_when_the_edit_touches_more_than_the_paper(proj, monkeypatch):
    sch = _files(proj)["root"]
    real = sheet_paper.apply_paper

    def greedy(root, spec, who):
        real(root, spec, who)
        symbol = next(c for c in root.children if not c.is_atom and c.name == "symbol")
        write_layer.set_string(symbol.find_children("property")[0], 1, "STRAY")

    monkeypatch.setattr(sheet_paper, "apply_paper", greedy)
    before = _snapshot(proj)
    result = _set(sch, [{"size": "A3"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "staged", result
    assert _snapshot(proj) == before


def test_the_staged_check_fires_when_the_paper_is_not_what_was_asked(proj, monkeypatch):
    sch = _files(proj)["root"]
    real = sheet_paper.apply_paper

    def other(root, spec, who):
        real(root, {**spec, "size": "A2" if spec["size"] == "A3" else "A3"}, who)

    monkeypatch.setattr(sheet_paper, "apply_paper", other)
    before = _snapshot(proj)
    result = _set(sch, [{"size": "A3"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "staged", result
    assert _snapshot(proj) == before


def test_the_page_check_fires_when_kicad_draws_another_page_than_asked(proj, monkeypatch):
    real = sheet_paper._svg_page_mm
    monkeypatch.setattr(sheet_paper, "_svg_page_mm", lambda d, stem: (real(d, stem)[0] + 5, real(d, stem)[1]))
    before = _snapshot(proj)
    result = _set(_files(proj)["root"], [{"size": "A3"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "page_follows_edit", result
    assert _snapshot(proj) == before


def test_the_staged_check_fires_when_only_the_token_differs_and_the_page_is_the_same(proj, monkeypatch):
    """USLetter and A are the same 279.4 x 215.9 mm page: the check on the token itself must see the difference."""
    sch = _files(proj)["root"]
    real = sheet_paper.apply_paper
    monkeypatch.setattr(sheet_paper, "apply_paper", lambda root, spec, who: real(root, {**spec, "size": "A"}, who))
    before = _snapshot(proj)
    result = _set(sch, [{"size": "USLetter"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "staged", result
    assert "the paper node is" in result["reason"]
    assert _snapshot(proj) == before


def test_the_page_check_fires_when_only_the_height_is_off(proj, monkeypatch):
    real = sheet_paper._svg_page_mm
    monkeypatch.setattr(sheet_paper, "_svg_page_mm", lambda d, stem: (real(d, stem)[0], real(d, stem)[1] + 5))
    result = _set(_files(proj)["root"], [{"size": "A3"}])
    assert result["ok"] is False and result["check"] == "page_follows_edit", result


def test_the_page_check_fires_when_the_page_cannot_be_read_back(proj, monkeypatch):
    monkeypatch.setattr(sheet_paper, "_svg_page_mm", lambda d, stem: None)
    result = _set(_files(proj)["root"], [{"size": "A3"}])
    assert result["ok"] is False and result["check"] == "page_follows_edit", result


def test_the_page_check_looks_at_the_turned_page_not_only_the_named_size(proj, monkeypatch):
    real = sheet_paper._svg_page_mm
    monkeypatch.setattr(sheet_paper, "_svg_page_mm", lambda d, stem: tuple(reversed(real(d, stem))))
    result = _set(_files(proj)["root"], [{"size": "A4", "portrait": True}])
    assert result["ok"] is False and result["check"] == "page_follows_edit", result


def test_the_nets_check_fires_when_the_nets_differ_after_the_edit(proj, monkeypatch):
    real = title_block._read_netlist
    calls = {"n": 0}

    def drifting(root):
        nets, blocks = real(root)
        calls["n"] += 1
        return ({**nets, "EXTRA": frozenset({("R9", "1")})} if calls["n"] == 2 else nets), blocks

    monkeypatch.setattr(title_block, "_read_netlist", drifting)
    before = _snapshot(proj)
    result = _set(_files(proj)["root"], [{"size": "A3"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "netlist_unchanged", result
    assert _snapshot(proj) == before


def test_the_erc_check_fires_when_the_violation_counts_differ(proj, monkeypatch):
    from collections import Counter

    real = sheet_authoring.erc_census
    calls = {"n": 0}

    def drifting(root):
        calls["n"] += 1
        counts = Counter(real(root))
        if calls["n"] == 2:
            counts[("made_up", "error")] += 1
        return counts

    monkeypatch.setattr(sheet_authoring, "erc_census", drifting)
    before = _snapshot(proj)
    result = _set(_files(proj)["root"], [{"size": "A3"}])
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "erc_unchanged", result
    assert _snapshot(proj) == before


def test_a_failed_second_write_leaves_the_first_sheet_with_its_old_paper(proj, monkeypatch):
    files = _files(proj)
    real = write_layer.save

    def failing(root, path, **kw):
        if Path(path).resolve() == files["sensors"].resolve() and kw.get("verify") is not None:
            raise write_layer.WriteLayerError("parse_check_failed", "forced for the test")
        return real(root, path, **kw)

    monkeypatch.setattr(write_layer, "save", failing)
    result = _set(files["root"], [{"size": "A3"}, {"sheet": "sensors.kicad_sch", "size": "A2"}])
    monkeypatch.undo()
    assert result["ok"] is False, result
    assert _raw_paper(files["root"]) == '(paper "A4")' and _raw_paper(files["sensors"]) == '(paper "A4")'


def test_a_failed_reload_after_the_writes_restores_every_sheet(proj, monkeypatch):
    """The post-write comparison (every written file reads back as the staged tree) fires, and every sheet is put back."""
    files = _files(proj)
    real_load, real_save = write_layer.load, write_layer.save
    state = {"saves": 0}

    def save(root, path, **kw):
        out = real_save(root, path, **kw)
        if Path(path).parent == proj:       # the real files, not the scratch copies
            state["saves"] += 1
        return out

    def load(path):
        root = real_load(path)
        if state["saves"] == 2 and ".tmp" not in Path(path).name:      # both sheets are written; now a reload that differs
            root.children.pop()
        return root

    monkeypatch.setattr(write_layer, "save", save)
    monkeypatch.setattr(write_layer, "load", load)
    result = _set(files["root"], [{"size": "A3"}, {"sheet": "sensors.kicad_sch", "size": "A2"}])
    state["saves"] = 99
    monkeypatch.undo()
    assert result["ok"] is False and result["code"] == "write_not_verified" and result["check"] == "reload_equals_serialised", result
    assert _raw_paper(files["root"]) == '(paper "A4")' and _raw_paper(files["sensors"]) == '(paper "A4")'


# --------------------------------------------------------------------------- the tool text an LLM reads


def _description() -> str:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return next(t.description for t in mcp._tool_manager.list_tools() if t.name == "set_paper")  # type: ignore[attr-defined]


def test_the_tool_text_states_the_facts_the_tests_pin():
    description = _description()
    for needle in ("A5 A4 A3 A2 A1 A0", "USLetter USLegal USLedger", "User", "25.4 to 3048", "portrait", "(paper \"A4\" portrait)", "mil",
                   "outside_page", "dry_run", "at most 50", "not_measured", "object_not_classified", "paper_unusual", "invalid_spec",
                   "write_not_verified", "SVG", "netlist", "ERC", "file_open_in_kicad", "root_sch", "restored", "changed once", "anchor point only",
                   "warning", "never a refusal", "bezier curve counts by its control points"):
        assert needle in description, needle


def test_every_code_the_tool_text_names_exists_in_the_code():
    source = Path(sheet_paper.__file__).read_text(encoding="utf-8")
    for code in ("invalid_spec", "write_not_verified", "paper_unusual", "object_not_classified", "outside_page", "page_follows_edit", "netlist_unchanged", "erc_unchanged"):
        assert f'"{code}"' in source, code


def test_the_schematic_skill_names_set_paper_with_what_it_does_and_does_not_measure():
    repo = Path(__file__).resolve().parent.parent
    skill = " ".join((repo / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8").split())
    for phrase in ("use `set_paper`", "`dry_run` first", "outside the NEW page", "`outside_page`, a warning", "at most 50",
                   "`not_measured`", "not the extent of text", "moves nothing"):
        assert phrase in skill, phrase
