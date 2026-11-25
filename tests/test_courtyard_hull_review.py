"""Review round 1 of issue 228: a good courtyard stays untouched (B1), custom pads are measured by
their primitives (B2), box_at and the edge placement use the hull (B3), the list of adopted parts
reports an unreadable footprint (B4) and refuses a library outside the project (F3), the wording
of a missing courtyard (F4), and the fix variants (F5)."""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.schema import PCB
from kicad_tools.sexp import SExp

from boardproof import footprint_courtyard as fc
from boardproof import part_adoption as pa
from boardproof import placement_anchor, write_layer
from tests import _kicad_faults as faults
from tests.test_courtyard_hull import _crtyd, _fns, _n, _s, _text, _tools, c1206
from tests.test_part_adoption import _part_assets, _propose, needs_cli


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj" / "adopt.kicad_pcb"
    path.parent.mkdir()
    write_layer.save_pcb(PCB.create(width=40.0, height=30.0, title="adopt"), path)
    return path


def _crtyd_text(root: SExp) -> list[str]:
    return [write_layer.serialise(n) for n in fc._crtyd_items(root)]


def _circle_courtyard(r: float) -> SExp:
    return SExp.list(
        "fp_circle", SExp.list("center", _n(0), _n(0)), SExp.list("end", _n(r), _n(0)),
        SExp.list("stroke", SExp.list("width", _n(0.05)), SExp.list("type", write_layer.symbol("solid"))),
        SExp.list("fill", write_layer.symbol("none")), SExp.list("layer", _s("F.CrtYd")),
    )  # fmt: skip


def test_b1_fix_leaves_a_courtyard_that_already_encloses_the_pads_untouched(tmp_path: Path) -> None:
    root = write_layer.load(c1206(tmp_path / "a.kicad_mod", courtyard=(-2.435, -0.965, 2.435, 0.965)))
    before = _crtyd_text(root)
    assert fc.check(root)["encloses"] is True
    assert fc.fix(root) is None
    assert _crtyd_text(root) == before
    circle = write_layer.load(c1206(tmp_path / "b.kicad_mod", courtyard=None))
    circle.append(_circle_courtyard(3.0))  # radius 3 holds the pad corners (at 2.6)
    before = _crtyd_text(circle)
    assert fc.check(circle)["encloses"] is True
    assert fc.fix(circle) is None and _crtyd_text(circle) == before


@needs_cli
def test_b1_confirm_keeps_a_good_courtyard_byte_for_byte(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")
    source = write_layer.load(next(Path(part["footprint_dir"]).glob("*.kicad_mod")))
    assert fc.check(source)["encloses"] is True
    proposed = _propose(project, [part], tmp_path / "rev")
    assert "courtyard_check" not in proposed["parts"][0]
    assert pa.confirm_adopt_parts(project, proposed["revision_token"])["status"] == "adopted"
    written = write_layer.load(tmp_path / "rev" / "lib" / "project_parts.pretty" / "R_C100.kicad_mod")
    assert _crtyd_text(written) == _crtyd_text(source)


def _custom_footprint(path: Path, poly, extra: SExp | None = None) -> SExp:
    prims = [SExp.list("gr_poly", SExp.list("pts", *[SExp.list("xy", _n(x), _n(y)) for x, y in poly]),
                       SExp.list("width", _n(0)), SExp.list("fill", write_layer.symbol("yes")))]
    if extra is not None:
        prims.append(extra)
    pad = SExp.list(
        "pad", _s("1"), write_layer.symbol("smd"), write_layer.symbol("custom"),
        SExp.list("at", _n(0), _n(0)), SExp.list("size", _n(0.2), _n(0.2)),
        SExp.list("layers", _s("F.Cu")),
        SExp.list("options", SExp.list("clearance", write_layer.symbol("outline")), SExp.list("anchor", write_layer.symbol("circle"))),
        SExp.list("primitives", *prims),
    )  # fmt: skip
    items = [_s("C_CUSTOM"), SExp.list("version", _n(20241229)), SExp.list("generator", _s("pcbnew")),
             SExp.list("layer", _s("F.Cu")), _text("Reference", "REF**", -2, "F.SilkS"),
             _text("Value", "C_CUSTOM", 2, "F.Fab"), SExp.list("attr", write_layer.symbol("smd")),
             pad, _crtyd(-0.5, -0.5, 0.5, 0.5)]  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    write_layer.save(SExp.list("footprint", *items), path)
    return write_layer.load(path)


SIX = [(-3, -3), (3, -3), (3, 3), (-3, 3)]
SMALL = [(-0.3, -0.3), (0.3, -0.3), (0.3, 0.3)]


def test_b2_a_custom_pad_is_measured_by_its_primitives(tmp_path: Path) -> None:
    result = fc.check(_custom_footprint(tmp_path / "c.kicad_mod", SIX))
    assert result["encloses"] is False and result["pads_box"] == [-3.0, -3.0, 3.0, 3.0]
    assert fc.check(_custom_footprint(tmp_path / "d.kicad_mod", SMALL))["encloses"] is True


def test_b2_a_primitive_that_cannot_be_measured_is_never_a_pass(tmp_path: Path, monkeypatch) -> None:
    root = _custom_footprint(tmp_path / "e.kicad_mod", SMALL)
    pad = root.find("pad")
    pad.find("primitives").append(SExp.list("gr_weird", SExp.list("pts", SExp.list("xy", _n(0), _n(0)))))
    with pytest.raises(fc.RawGeometryError):  # in memory: KiCad would not load such a file either
        fc.check(root)
    nopad_size = write_layer.load(tmp_path / "e.kicad_mod")
    nopad_size.find("pad").children.remove(nopad_size.find("pad").find("size"))
    with pytest.raises(fc.RawGeometryError):
        fc.check(nopad_size)
    monkeypatch.setattr(fc, "check", lambda r, margin=fc.MARGIN_MM: (_ for _ in ()).throw(fc.RawGeometryError("x")))
    result = _tools()["validate_klc"](str(tmp_path / "e.kicad_mod"))
    assert result["checks"]["courtyard_encloses_pads"] is False and "unmeasurable" in result["courtyard"]
    assert result["status"] == "violations_found"


def test_b2_propose_rejects_a_part_whose_pads_cannot_be_measured(project: Path, tmp_path: Path, monkeypatch) -> None:
    part = _part_assets(tmp_path, "C100")
    monkeypatch.setattr(fc, "check", lambda r, margin=fc.MARGIN_MM: (_ for _ in ()).throw(fc.RawGeometryError("pad has no size")))
    result = _propose(project, [part], tmp_path / "rev")
    assert "cannot be measured" in str(result), result
    assert result.get("revision_token") is None


def _wide_board(tmp_path: Path) -> Path:
    board = PCB.create(width=100.0, height=60.0)
    board.add_footprint_from_file(c1206(tmp_path / "C_1206.kicad_mod"), "C1", 40.0, 30.0)
    path = tmp_path / "wide.kicad_pcb"
    write_layer.save_pcb(board, path)
    return path


def test_b3_box_at_is_the_hull_also_when_turned(tmp_path: Path) -> None:
    part = placement_anchor.read_parts(write_layer.load(_wide_board(tmp_path)))[0]
    assert part.box[2] - part.box[0] == pytest.approx(4.67, abs=1e-6)  # the pads, not the 3.2 mm courtyard
    assert placement_anchor.box_at(part, part.at, part.angle) == pytest.approx(part.box)
    turned = placement_anchor.box_at(part, part.at, part.angle + 90.0)
    assert turned[3] - turned[1] == pytest.approx(4.67, abs=1e-6)
    assert turned[2] - turned[0] == pytest.approx(1.73, abs=1e-6)


def test_b3_edge_placement_keeps_the_offset_from_the_pads(tmp_path: Path) -> None:
    path = _wide_board(tmp_path)
    result = _fns()["place_footprints_at_edge"](str(path), [{"reference": "C1", "edge": "left", "offset_mm": 2.0}])
    assert result["status"] == "ok", result
    root = write_layer.load(path)
    part = placement_anchor.read_parts(root)[0]
    outline, _ = placement_anchor.outline_rect(root)
    assert part.box[0] - outline.box[0] == pytest.approx(2.0, abs=1e-6)  # the pad's edge, not the courtyard's


def test_b4_an_unreadable_footprint_makes_the_list_incomplete(tmp_path: Path) -> None:
    pretty = tmp_path / "proj" / "lib" / "project_parts.pretty"
    c1206(pretty / "C_OK.kicad_mod", "C_OK", courtyard=(-2.6, -1.1, 2.6, 1.1))
    broken = c1206(pretty / "C_BROKEN.kicad_mod", "C_BROKEN")
    faults.rewrite_bytes(broken, lambda data: data[:30])
    result = fc.list_adopted(tmp_path / "proj" / "x.kicad_pcb")
    assert result["status"] == "incomplete"
    assert [u["footprint"] for u in result["unreadable"]] == ["C_BROKEN"]
    assert result["checked"] == 2


@pytest.mark.parametrize("library_dir", ["../outside", "/abs/lib", "C:/abs/lib", "lib/../../x", ""])
def test_f3_the_list_refuses_a_library_dir_outside_the_project(tmp_path: Path, library_dir: str) -> None:
    result = fc.list_adopted(tmp_path / "x.kicad_pcb", library_dir=library_dir)
    assert result["status"] == "error" and "library_dir" in result["reason"]
    assert fc.list_adopted(tmp_path / "x.kicad_pcb", library_name="../evil")["status"] == "error"


def test_f4_a_missing_courtyard_is_worded_as_missing(project: Path, tmp_path: Path) -> None:
    part = _part_assets(tmp_path, "C100")
    fp_file = next(Path(part["footprint_dir"]).glob("*.kicad_mod"))
    root = write_layer.load(fp_file)
    for node in fc._crtyd_items(root):
        root.children.remove(node)
    write_layer.save(root, fp_file)
    entry = _propose(project, [part], tmp_path / "rev")["parts"][0]
    text = " ".join(entry["normalised"])
    assert "has no courtyard" in text and "None" not in text


def test_f5_fix_keeps_the_back_layer_and_writes_the_old_format_as_lines(tmp_path: Path) -> None:
    root = write_layer.load(c1206(tmp_path / "b.kicad_mod"))
    for node in fc._crtyd_items(root):
        node.find("layer").children[0] = _s("B.CrtYd")
    assert fc.fix(root) == [-2.59, -1.12, 2.59, 1.12]
    assert [n.find("layer").get_string(0) for n in fc._crtyd_items(root)] == ["B.CrtYd"]
    assert fc.check(root)["encloses"] is True
    legacy = write_layer.load(c1206(tmp_path / "m.kicad_mod"))
    legacy.name = "module"
    assert fc.fix(legacy) == [-2.59, -1.12, 2.59, 1.12]
    assert [n.name for n in fc._crtyd_items(legacy)] == ["fp_line"] * 4
    assert fc.check(legacy)["encloses"] is True


def test_the_hull_tools_say_that_a_custom_pad_counts_by_its_anchor_size() -> None:
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import placement as placement_toolset

    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    texts = {t.name: t.description for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    for name in ("align_footprints", "distribute_footprints", "place_footprints_at_edge", "propose_autoplacement", "pack_footprints_densely"):
        assert "a custom pad counts by its anchor size" in " ".join(texts[name].split()), name
