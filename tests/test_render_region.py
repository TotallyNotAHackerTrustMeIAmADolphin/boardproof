"""render_region (GitHub issue 98): one rectangular area of a sheet as a PNG at a stated resolution.

The coordinate mapping from sheet millimetres to the PDF page is PROVEN here against the raw wire coordinates of a
sheet built through the tools: an L of two wires, (50, 50) to (100, 50) and (100, 50) to (100, 90), is rendered in
a region, and the bounding box of its dark pixels is compared with the pixel position the mapping predicts (x to the
right, y downwards from the top-left of the page, 72 / 25.4 points per mm, no other offset).

Mutants, each shown red by a test named in its docstring: a wrong scale, a flipped y axis, a shifted origin, no
region validation, no page check, no pixel bound, a render failure written as a file, a render on the project itself
instead of a scratch copy.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image

from boardproof import kicad_cli, png_render, render_region
from boardproof.toolsets import project as project_toolset
from tests import test_sheet_pins as _base


@pytest.fixture(scope="module")
def sheet(tmp_path_factory) -> Path:
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    made = project_toolset._project_create(str(tmp_path_factory.mktemp("rr") / "proj"), "demo")
    sch = Path(made["schematic_path"])
    wires = [_base._wire((50, 50), (100, 50)), _base._wire((100, 50), (100, 90))]
    assert _base._tools()["add_wires"](str(sch), wires)["status"] == "ok"
    return sch


def _ink(png: str) -> tuple[int, int, int, int] | None:
    """Bounding box (left, top, right, bottom, inclusive) of the pixels that are not near white, or None for a blank image."""
    image = Image.open(png).convert("RGB")
    box = image.point(lambda v: 255 if v < 200 else 0).convert("L").getbbox()
    return None if box is None else (box[0], box[1], box[2] - 1, box[3] - 1)


def _px(mm: float, dpi: int) -> float:
    return mm / 25.4 * dpi


def _dark(image: Image.Image, x: float, y: float, reach: int = 2) -> bool:
    cx, cy = round(x), round(y)
    return any(min(image.getpixel((cx + dx, cy + dy))) < 200 for dx in range(-reach, reach + 1) for dy in range(-reach, reach + 1))


def test_the_region_is_the_requested_size_and_the_wires_sit_where_the_mapping_puts_them(sheet, tmp_path) -> None:
    """Mutants: a wrong scale (72/25), a flipped y axis, a shifted origin. The L's corner and both ends are checked on both axes."""
    dpi = 150
    out = render_region.render_region(str(sheet), 40, 40, 110, 100, str(tmp_path), dpi)
    assert out["ok"] is True and out["status"] == "ok" and out["authoritative"] is False, out
    png = out["png"]
    assert Path(png).parent == tmp_path and Path(png).is_file()
    image = Image.open(png).convert("RGB")
    assert image.size == (out["width_px"], out["height_px"])
    assert abs(image.size[0] - _px(70, dpi)) <= 2 and abs(image.size[1] - _px(60, dpi)) <= 2
    left, top, right, bottom = _ink(png)
    # the L spans x 50..100 mm and y 50..90 mm; the region starts at (40, 40) mm; the line is about 0.15 mm wide
    tolerance = 4
    assert abs(left - _px(10, dpi)) <= tolerance and abs(right - _px(60, dpi)) <= tolerance, (left, right)
    assert abs(top - _px(10, dpi)) <= tolerance and abs(bottom - _px(50, dpi)) <= tolerance, (top, bottom)
    # on the horizontal wire (y = 50 mm, x from 50 to 100) and on the vertical one (x = 100 mm, y from 50 to 90) ...
    assert _dark(image, _px(30, dpi), _px(10, dpi)) and _dark(image, _px(60, dpi), _px(30, dpi))
    # ... and nothing at the mirrored places: not at the left of the horizontal wire, not above or left of the vertical one
    assert not _dark(image, _px(5, dpi), _px(10, dpi)) and not _dark(image, _px(60, dpi), _px(3, dpi))
    assert not _dark(image, _px(10, dpi), _px(30, dpi)) and not _dark(image, _px(30, dpi), _px(40, dpi))


def test_a_region_without_ink_is_blank_and_the_size_follows_the_dpi(sheet, tmp_path) -> None:
    out = render_region.render_region(str(sheet), 150, 40, 200, 70, str(tmp_path), 100)
    assert out["ok"] is True and _ink(out["png"]) is None
    double = render_region.render_region(str(sheet), 150, 40, 200, 70, str(tmp_path), 200)
    assert abs(double["width_px"] - 2 * out["width_px"]) <= 2 and double["png"] != out["png"]


def test_the_page_is_reported_and_a_region_outside_it_is_refused(sheet, tmp_path) -> None:
    """Mutant: no page check (the child would fail with a library message, or a short image would be written)."""
    ok = render_region.render_region(str(sheet), 0, 0, 20, 20, str(tmp_path), 50)
    assert ok["ok"] is True and ok["page_size_mm"] == [297.0, 210.0]
    for box in ((280, 100, 300, 120), (100, 190, 120, 215), (-5, 10, 20, 20)):
        out = render_region.render_region(str(sheet), *box, str(tmp_path), 50)
        assert out["ok"] is False and out["code"] == "region_outside_page" and "297.0 x 210.0" in out["reason"], box
    assert [p.name for p in tmp_path.iterdir()] == [Path(ok["png"]).name]


def test_bad_arguments_are_refused_and_nothing_is_written(sheet, tmp_path) -> None:
    """Mutant: no region validation."""
    out_dir = tmp_path / "never"
    cases = [((10, 10, 10, 20), 100), ((10, 10, 5, 20), 100), ((10, 20, 30, 20), 100), ((float("nan"), 0, 5, 5), 100), ((True, 0, 5, 5), 100),
             (("1", 0, 5, 5), 100), ((0, 0, 5, 5), 49), ((0, 0, 5, 5), 601), ((0, 0, 5, 5), 100.5), ((0, 0, 5, 5), True)]
    for box, dpi in cases:
        out = render_region.render_region(str(sheet), *box, str(out_dir), dpi)
        assert out["ok"] is False and out["code"] == "invalid_spec", (box, dpi, out)
    assert not out_dir.exists()
    assert render_region.render_region(str(sheet.parent / "absent.kicad_sch"), 0, 0, 5, 5, str(out_dir), 100)["code"] == "file_not_found"
    assert render_region.render_region(str(sheet.parent / "demo.kicad_pro"), 0, 0, 5, 5, str(out_dir), 100)["code"] == "file_not_found"


def test_a_region_over_the_pixel_bound_is_refused_before_any_render(sheet, tmp_path, monkeypatch) -> None:
    """Mutant: no pixel bound. The refusal comes before kicad-cli is called."""
    monkeypatch.setattr(kicad_cli, "export_sch_pdf", lambda *a, **k: pytest.fail("rendered"))
    out = render_region.render_region(str(sheet), 0, 0, 297, 210, str(tmp_path), 600)
    assert out["ok"] is False and out["code"] == "region_too_large" and "dpi" in out["reason"]
    assert render_region.render_region(str(sheet), 0, 0, 280, 200, str(tmp_path), 600)["code"] == "region_too_large"


def test_an_unusable_output_dir_and_an_unbounded_region_are_refused_with_a_code(sheet, tmp_path) -> None:
    """Mutants: an output_dir that is a file raised FileExistsError; 1e308 mm made int(inf) raise OverflowError."""
    afile = tmp_path / "plain"
    afile.write_text("x", encoding="utf-8")
    out = render_region.render_region(str(sheet), 40, 40, 60, 60, str(afile), 50)
    assert out["ok"] is False and out["code"] == "output_dir_unusable" and out["authoritative"] is False
    for box in ((0, 0, 1e308, 1e308), (-1e308, -1e308, 1e308, 1e308), (0, 0, 1e200, 1e200)):
        out = render_region.render_region(str(sheet), *box, str(tmp_path), 600)
        assert out["ok"] is False and out["code"] == "region_too_large", box


def test_every_refusal_says_authoritative_false(sheet, tmp_path) -> None:
    """Mutant: a refusal without the key (the description says every result carries it)."""
    for args in ((10, 10, 5, 20, 100), (0, 0, 5, 5, 49), (0, 0, 280, 100, 50)):
        assert render_region.render_region(str(sheet), *args[:4], str(tmp_path), args[4])["authoritative"] is False
    assert render_region.render_region(str(tmp_path / "no.kicad_sch"), 0, 0, 5, 5, str(tmp_path), 50)["authoritative"] is False


def test_a_pdf_whose_page_size_cannot_be_read_is_refused_not_rendered(sheet, tmp_path, monkeypatch) -> None:
    """Mutant: the page check skipped when the size is unknown (fail-open)."""
    monkeypatch.setattr(render_region, "_page_size_mm", lambda pdf: None)
    monkeypatch.setattr(png_render, "render_pdf_regions", lambda *a, **k: pytest.fail("rendered"))
    out = render_region.render_region(str(sheet), 280, 100, 300, 120, str(tmp_path), 50)
    assert out["ok"] is False and out["code"] == "page_size_unreadable" and out["authoritative"] is False
    assert not list(tmp_path.iterdir())


def test_a_failed_raster_is_reported_and_no_png_is_written(sheet, tmp_path, monkeypatch) -> None:
    """Mutant: a render failure written (or reported ok) anyway."""
    failed = png_render.RegionRender(1)
    failed.failures[0] = "PNG renderer timed out"
    monkeypatch.setattr(png_render, "render_pdf_regions", lambda *a, **k: failed)
    out = render_region.render_region(str(sheet), 40, 40, 60, 60, str(tmp_path / "o"), 100)
    assert out["ok"] is False and out["status"] == "incomplete" and out["code"] == "png_render_failed" and "timed out" in out["reason"]
    assert not list((tmp_path / "o").glob("*"))


def test_the_project_folder_is_not_touched(sheet, tmp_path, monkeypatch) -> None:
    """Mutant: exporting from the project itself. kicad-cli is shown a scratch copy, and no byte or file of the project changes."""
    seen: list[Path] = []
    real = kicad_cli.export_sch_pdf

    def spy(path, out):
        seen.append(Path(path))
        return real(path, out)

    monkeypatch.setattr(kicad_cli, "export_sch_pdf", spy)
    before = {p.name: p.read_bytes() for p in sheet.parent.iterdir() if p.is_file()}
    assert render_region.render_region(str(sheet), 40, 40, 60, 60, str(tmp_path), 50)["ok"] is True
    assert {p.name: p.read_bytes() for p in sheet.parent.iterdir() if p.is_file()} == before
    assert seen and sheet.parent.resolve() not in [p.resolve().parent for p in seen]


def test_the_tool_is_registered_read_only_with_the_gate_wording(sheet, tmp_path) -> None:
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import diagnostics

    mcp = MCPServer("t")
    assert "render_region" in diagnostics.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "render_region")  # type: ignore[attr-defined]
    assert tool.annotations.read_only_hint is True
    assert "verify_board" in tool.description and "authoritative: false" in tool.description
    out = tool.fn(str(sheet), 40, 40, 60, 60, str(tmp_path), 100)
    assert out["ok"] is True and out["authoritative"] is False
