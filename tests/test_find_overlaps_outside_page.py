"""find_overlaps kind outside_page (GitHub issue 220), read-only, measured against real kicad-cli.

The frame margin and the page origin are what KiCad itself draws: the SVG export of the sheet is read
back (test_the_frame_margin_is_what_kicad_draws) and the constant must match it. Every box is
cross-checked against the raw ``(at ...)`` and ``(paper ...)`` of the sheet file, never against a
kicad_tools property.
"""

from __future__ import annotations

import inspect
import json
import re
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_overlaps, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

K = ["outside_page"]


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


def _project(tmp_path: Path) -> Path:
    return Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])


def _text(tools: dict, sch: Path, x: float, y: float, text: str) -> None:
    assert tools["add_text"](str(sch), [{"x": x, "y": y, "text": text}], snap=False)["status"] == "ok"


def _find(sch: Path, **kw) -> dict:
    out = _tools()["find_overlaps"](str(sch), kinds=K, **kw)
    assert out["status"] == "ok", out
    return out


def _svg_frame(sch: Path, out_dir: Path) -> tuple[list[float], list[float]]:
    """(page width, height, from the svg size) and the outer frame line (x1, y1, x2, y2) KiCad draws."""
    cli = kicad_cli.find_kicad_cli()
    r = subprocess.run([str(cli), "sch", "export", "svg", str(sch), "-o", str(out_dir)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    svg = next(out_dir.glob("*.svg")).read_text(encoding="utf-8")
    w, h = (float(v) for v in re.search(r'viewBox="0\.0+ 0\.0+ ([\d.]+) ([\d.]+)"', svg).groups())
    paths = [[tuple(map(float, p)) for p in re.findall(r"[ML]([\d.]+) ([\d.]+)", d)] for d in re.findall(r'<path[^>]*?d="([^"]+)"', svg)]
    # the outer frame is the first closed rectangle whose left edge is at x < 20 and spans most of the page
    frame = next(p for p in paths if len(p) == 5 and p[0][0] < 20 and abs(p[2][0] - p[0][0]) > w * 0.8)
    return [w, h], [frame[0][0], frame[0][1], frame[2][0], frame[2][1]]


@pytest.mark.parametrize("paper", [("A4", False), ("A3", True)])
def test_the_frame_margin_is_what_kicad_draws(tmp_path, paper):
    name, portrait = paper
    sch = _project(tmp_path)
    assert _tools()["set_paper"](str(sch), [{"sheet": str(sch), "size": name, "portrait": portrait}])["status"] == "ok"
    (w, h), (x1, y1, x2, y2) = _svg_frame(sch, tmp_path / "svg")
    m = sheet_overlaps.FRAME_MARGIN_MM
    assert (x1, y1) == pytest.approx((m, m), abs=1e-3)
    assert (x2, y2) == pytest.approx((w - m, h - m), abs=0.01)


def test_a_note_running_over_the_right_edge_is_reported_with_mm_and_direction(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _text(tools, sch, 250.0, 100.0, "A long single line note that runs off the sheet to the right and keeps on going well beyond the paper edge")
    _text(tools, sch, 20.0, 100.0, "short and inside")
    out = _find(sch)
    assert out["found"] == 1 and out["counts"] == {"outside_page": 1}
    f = out["findings"][0]
    box = f["items"][0]["box"]
    assert f["items"][0]["kind"] == "text" and f["estimated"] is True
    assert box[0] == pytest.approx(250.0)                       # raw (at 250 100), left justified
    assert f["page_mm"] == [297.0, 210.0] and f["frame_margin_mm"] == 10.0
    assert f["over_frame_mm"] == {"right": pytest.approx(box[2] - 287.0, abs=0.01)}
    assert f["over_paper_mm"] == {"right": pytest.approx(box[2] - 297.0, abs=0.01)}
    assert "move_text" in f["fix"] or "move" in f["fix"]


def test_between_the_frame_and_the_paper_edge_is_over_the_frame_only(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _text(tools, sch, 285.0, 100.0, "abcd")                     # box 285 .. about 289.0: past the frame at 287, inside the paper
    f = _find(sch)["findings"][0]
    assert list(f["over_frame_mm"]) == ["right"] and f["over_paper_mm"] == {}


@pytest.mark.parametrize("side, x, y", [("bottom", 100.0, 205.0), ("top", 100.0, 10.5), ("left", 8.0, 100.0), ("right", 285.0, 100.0)])
def test_each_side_of_the_frame_alone_is_measured_from_the_frame_not_the_paper_edge(tmp_path, side, x, y):
    sch = _project(tmp_path)
    _text(_tools(), sch, x, y, "abcd")
    f = _find(sch)["findings"][0]
    box = f["items"][0]["box"]
    frame = {"left": 10.0 - box[0], "top": 10.0 - box[1], "right": box[2] - 287.0, "bottom": box[3] - 200.0}[side]
    assert 0 < frame < 10 and box[0] >= 0 and box[1] >= 0 and box[2] <= 297 and box[3] <= 210    # between the frame and the paper edge
    assert f["over_frame_mm"] == {side: pytest.approx(frame, abs=0.01)}
    assert f["over_paper_mm"] == {}


def test_every_direction_is_named_and_a_corner_has_two(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _text(tools, sch, 5.0, 5.0, "corner")                       # left of the frame (10) and above it
    out = _find(sch)
    f = out["findings"][0]
    assert set(f["over_frame_mm"]) == {"left", "top"}
    assert f["over_frame_mm"]["left"] == pytest.approx(5.0, abs=0.01)
    _text(tools, sch, 100.0, 215.0, "below")
    assert any(set(g["over_paper_mm"]) == {"bottom"} for g in _find(sch)["findings"])
    _text(tools, sch, -20.0, 100.0, "negative x")
    assert any("left" in g["over_paper_mm"] for g in _find(sch)["findings"])


def test_a_label_and_a_symbol_beyond_the_edge_are_reported_and_a_part_that_fits_is_not(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    # add_symbols grows the paper by itself when a part does not fit, so the parts go in on A4 and the paper is made smaller after
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 209.55, "y": 100.0, "ref": "R1", "value": "1k"},
                                           {"lib_id": "Device:R", "x": 100.0, "y": 100.0, "ref": "R2", "value": "1k"}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": 185.0, "y": 50.0, "text": "A_RATHER_LONG_NET_NAME", "snap": False}])["status"] == "ok"
    assert tools["set_paper"](str(sch), [{"sheet": str(sch), "size": "A5"}])["status"] == "ok"   # 210 x 148: R1 reaches past 210
    out = _find(sch)
    kinds = {(i["kind"], i.get("reference") or i.get("text")) for f in out["findings"] for i in f["items"]}
    assert ("symbol", "R1") in kinds and ("label", "A_RATHER_LONG_NET_NAME") in kinds
    assert not any(i.get("reference") == "R2" for f in out["findings"] for i in f["items"])
    r1 = next(f for f in out["findings"] if f["items"][0].get("reference") == "R1")
    assert r1["estimated"] is False and "right" in r1["over_paper_mm"]


def test_a_note_inside_the_frame_and_the_threshold(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _text(tools, sch, 11.0, 11.0, "ok")
    assert _find(sch)["found"] == 0
    _text(tools, sch, 9.8, 100.0, "ok2")                         # 0.2 mm over the frame: below the text threshold of 0.5 mm
    assert _find(sch)["found"] == 0
    _text(tools, sch, 9.0, 120.0, "ok3")                         # 1 mm over
    assert _find(sch)["found"] == 1


def test_the_page_is_read_from_the_raw_paper_token_portrait_and_user(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _text(tools, sch, 250.0, 100.0, "A long single line note that runs off the sheet to the right and keeps on going well beyond the paper edge")
    assert _find(sch)["found"] == 1
    assert tools["set_paper"](str(sch), [{"sheet": str(sch), "size": "A3"}])["status"] == "ok"
    assert _find(sch)["found"] == 0                              # the same note fits on A3
    assert tools["set_paper"](str(sch), [{"sheet": str(sch), "size": "A4", "portrait": True}])["status"] == "ok"
    f = _find(sch)["findings"][0]                                # A4 portrait: 210 wide
    assert f["page_mm"] == [210.0, 297.0] and "right" in f["over_paper_mm"]


def test_an_unreadable_paper_is_a_named_gap_never_a_clean_sheet(tmp_path, monkeypatch):
    tools = _tools()
    sch = _project(tmp_path)
    _text(tools, sch, 250.0, 100.0, "A long single line note that runs off the sheet to the right and keeps on going well beyond the paper edge")
    from boardproof import sheet_extents

    monkeypatch.setattr(sheet_extents, "page_size_mm", lambda paper: None)
    out = _find(sch)
    assert out["found"] == 0
    assert any(s["reason"] == "paper_unreadable" and s["kind"] == "outside_page" for s in out["skipped"])


def test_a_custom_drawing_sheet_makes_the_frame_unknown_and_only_the_paper_edge_is_checked(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    pro = sch.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text(encoding="utf-8"))
    data["schematic"]["page_layout_descr_file"] = "my.kicad_wks"
    write_layer.save_project(data, pro)
    _text(tools, sch, 285.0, 100.0, "abcd")                      # over the default frame, inside the paper
    _text(tools, sch, 290.0, 120.0, "a note that leaves the paper")
    out = _find(sch)
    assert out["found"] == 1 and out["findings"][0]["frame_margin_mm"] is None
    assert out["findings"][0]["over_frame_mm"] == {} and "right" in out["findings"][0]["over_paper_mm"]
    assert any(s["reason"] == "frame_unknown" for s in out["skipped"])


@pytest.mark.parametrize("how", ["directory", "no_schematic_key"])
def test_an_unreadable_project_file_makes_the_frame_unknown(tmp_path, how):
    tools = _tools()
    sch = _project(tmp_path)
    pro = sch.with_suffix(".kicad_pro")
    if how == "directory":
        pro.unlink()
        pro.mkdir()                                              # exists, cannot be read as a file
    else:
        write_layer.save_project({"meta": {}}, pro)              # JSON, but not a project that says where the drawing sheet is
    _text(tools, sch, 285.0, 100.0, "abcd")
    out = _find(sch)
    assert out["found"] == 0 and any(s["reason"] == "frame_unknown" for s in out["skipped"])


def test_a_sheet_symbol_is_checked_on_its_parent_page_and_the_child_on_its_own(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    assert tools["create_sheets"](str(sch), [{"file": "a.kicad_sch"}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(sch), [{"file": "a.kicad_sch", "x": 203.2, "y": 50.8, "pins": [{"name": "X", "direction": "input"}], "name": "A"}])["status"] == "ok"
    assert tools["set_paper"](str(sch), [{"sheet": str(sch), "size": "A5"}])["status"] == "ok"   # the parent is 210 wide, the child stays A4
    _text(tools, tmp_path / "proj" / "a.kicad_sch", 150.0, 20.0, "inside the child but past the 210 mm of the parent")
    out = _find(sch)
    assert out["sheets_read"] == 2
    assert {f["sheet"] for f in out["findings"]} == {"demo.kicad_sch"}      # the note in the child lies inside the child's A4
    assert any(f["items"][0]["kind"] == "sheet" for f in out["findings"])


def test_nothing_is_written_and_the_other_kinds_do_not_report_it(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _text(tools, sch, 250.0, 100.0, "A long single line note that runs off the sheet to the right and keeps on going well beyond the paper edge")
    before = sch.read_bytes()
    assert tools["find_overlaps"](str(sch), kinds=["text_text", "text_symbol"])["found"] == 0
    assert tools["find_overlaps"](str(sch))["counts"]["outside_page"] == 1
    assert sch.read_bytes() == before


def test_the_tool_text_pins_the_kind_its_numbers_and_its_limits():
    doc = " ".join((inspect.getdoc(_tools()["find_overlaps"]) or "").split())
    assert "outside_page" in doc and "outside_page" in sheet_overlaps.KINDS
    for phrase in ("10 mm", "over_frame_mm", "over_paper_mm", "page_mm", "frame_margin_mm", "left, right, top or bottom",
                   "custom drawing sheet", "paper_unreadable", "frame_unknown", "sheet symbol",
                   "Wires, junctions and graphic shapes are not checked"):
        assert phrase in doc, phrase


def test_the_schematic_skill_names_the_kind_and_its_limits():
    skill = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "kicad-schematic" / "SKILL.md"
    line = next(ln for ln in skill.read_text(encoding="utf-8").splitlines() if ln.startswith("11. "))
    for phrase in ("outside_page", "10 mm inside the page edge", "wires and graphics are not checked", "custom drawing sheet"):
        assert phrase in line, phrase


def test_the_raw_paper_token_is_what_the_check_reads(tmp_path):
    sch = _project(tmp_path)
    raw = write_layer.load(sch).find_children("paper")[0]
    assert raw.get_string(0) == "A4"
    _text(_tools(), sch, 250.0, 100.0, "A long single line note that runs off the sheet to the right and keeps on going well beyond the paper edge")
    assert _find(sch)["findings"][0]["page_mm"] == [297.0, 210.0]
