"""Tests for the visual diagnostics toolset (archived backlog Phase 3.5).

Renders the real simple_led fixtures via the real kicad-cli install on this
machine (same pattern as tests/test_kicad_cli.py) -- no mocking, since the
whole point is confirming kicad-cli actually produces usable images.
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.mcpserver import MCPServer

from boardproof.toolsets import diagnostics as diagnostics_toolset

FIXTURES = Path(__file__).parent / "fixtures"


def _build_and_get_fns(mcp: MCPServer, module) -> dict:
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_build_registers_expected_tools():
    mcp = MCPServer("test")
    names = diagnostics_toolset.build(mcp)
    assert set(names) == {"render_board_diagnostic", "render_schematic_diagnostic"}


def test_render_board_diagnostic_produces_svgs_and_is_non_authoritative(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_board_diagnostic"](str(FIXTURES / "simple_led.kicad_pcb"), str(tmp_path))
    assert result["status"] == "ok"
    assert result["authoritative"] is False
    assert "pcb_run_drc" in result["note"]
    assert result["images"]
    for img in result["images"]:
        assert Path(img).is_file()
        assert Path(img).suffix == ".svg"


def test_render_schematic_diagnostic_produces_svgs_and_is_non_authoritative(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_schematic_diagnostic"](str(FIXTURES / "simple_led.kicad_sch"), str(tmp_path))
    assert result["status"] == "ok"
    assert result["authoritative"] is False
    assert "sch_run_erc" in result["note"]
    assert result["images"]
    for img in result["images"]:
        assert Path(img).is_file()


def test_render_board_diagnostic_missing_file_is_incomplete_not_an_exception(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_board_diagnostic"](str(tmp_path / "does_not_exist.kicad_pcb"), str(tmp_path))
    assert result["status"] == "incomplete"
    assert result["authoritative"] is False


def test_render_schematic_diagnostic_missing_file_is_incomplete_not_an_exception(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_schematic_diagnostic"](str(tmp_path / "does_not_exist.kicad_sch"), str(tmp_path))
    assert result["status"] == "incomplete"
    assert result["authoritative"] is False


def test_render_schematic_diagnostic_png_dpi_gives_png_of_requested_size(tmp_path):
    from PIL import Image

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_schematic_diagnostic"](
        str(FIXTURES / "simple_led.kicad_sch"), str(tmp_path), png_dpi=200
    )
    assert result["status"] == "ok"
    assert result["authoritative"] is False
    assert result["png_images"]
    for img in result["png_images"]:
        assert Path(img).suffix == ".png"
        w, h = Image.open(img).size
        # A4 landscape is 11.69 x 8.27 in; 200 dpi gives about 2339 x 1654 px.
        assert abs(w - 2339) <= 4 and abs(h - 1654) <= 4
    assert result["images"]  # the SVG stays


def test_render_schematic_diagnostic_default_has_no_png(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    result = tools["render_schematic_diagnostic"](str(FIXTURES / "simple_led.kicad_sch"), str(tmp_path))
    assert "png_images" not in result


def test_render_schematic_diagnostic_png_dpi_out_of_range_is_incomplete(tmp_path):
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, diagnostics_toolset)
    for bad in (0, 10, 5000):
        result = tools["render_schematic_diagnostic"](
            str(FIXTURES / "simple_led.kicad_sch"), str(tmp_path), png_dpi=bad
        )
        assert result["status"] == "incomplete"
        assert "png_dpi" in result["reason"]


def test_render_schematic_diagnostic_description_mentions_png_dpi():
    mcp = MCPServer("test")
    diagnostics_toolset.build(mcp)
    desc = {t.name: t.description for t in mcp._tool_manager.list_tools()}["render_schematic_diagnostic"]  # type: ignore[attr-defined]
    assert "png_dpi" in desc
    assert "PNG" in desc


def _schematic_fn():
    mcp = MCPServer("test")
    return _build_and_get_fns(mcp, diagnostics_toolset)["render_schematic_diagnostic"]


def test_two_concurrent_png_dpi_renders_both_return(tmp_path):
    """Build ticket 104: two parallel png_dpi calls ended the whole process in PDFium."""
    import threading

    fn = _schematic_fn()
    results: dict[int, dict] = {}

    def run(n: int) -> None:
        results[n] = fn(str(FIXTURES / "simple_led.kicad_sch"), str(tmp_path / f"o{n}"), png_dpi=200)

    threads = [threading.Thread(target=run, args=(n,)) for n in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    assert set(results) == {0, 1}
    for r in results.values():
        assert r["status"] == "ok" and r["png_images"]
        assert all(Path(p).is_file() for p in r["png_images"])


def _fake_worker(tmp_path, body: str):
    w = tmp_path / "fake_worker.py"
    w.write_text(body, encoding="utf-8")
    return w


def test_crashing_png_renderer_gives_png_render_failed_and_keeps_svg(tmp_path, monkeypatch):
    from boardproof import png_render

    monkeypatch.setattr(
        png_render, "_WORKER", _fake_worker(tmp_path, "import os\nos._exit(139)\n")
    )
    out = tmp_path / "out"
    result = _schematic_fn()(str(FIXTURES / "simple_led.kicad_sch"), str(out), png_dpi=100)
    assert result["status"] == "incomplete"
    assert result["refusal"] == "png_render_failed"
    assert "139" in result["reason"]
    assert result["images"] and all(p.endswith(".svg") for p in result["images"])
    assert result["authoritative"] is False
    assert not list(out.glob("*.png"))


def test_hung_png_renderer_is_killed_at_timeout(tmp_path, monkeypatch):
    from boardproof import png_render

    monkeypatch.setattr(png_render, "_WORKER", _fake_worker(tmp_path, "import time\ntime.sleep(60)\n"))
    monkeypatch.setattr(png_render, "PNG_TIMEOUT_SECONDS", 1)
    result = _schematic_fn()(str(FIXTURES / "simple_led.kicad_sch"), str(tmp_path / "out"), png_dpi=100)
    assert result["refusal"] == "png_render_failed"
    assert "timed out" in result["reason"]
    assert result["images"]


def test_png_renderer_runs_isolated_and_cleans_its_temp_dir(tmp_path, monkeypatch):
    import subprocess
    import tempfile

    from boardproof import png_render

    seen = {}
    real_run = subprocess.run

    def spy(cmd, **kw):
        seen["cmd"] = cmd
        return real_run(cmd, **kw)

    monkeypatch.setattr(png_render.subprocess, "run", spy)
    monkeypatch.setattr(png_render, "_WORKER", _fake_worker(tmp_path, "import os\nos._exit(3)\n"))
    # A private temp directory: other tests (and other xdist workers) create png_render_* directories in the shared one.
    scoped = tmp_path / "scoped_tmp"
    scoped.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scoped))
    before = set(scoped.glob("png_render_*"))
    _schematic_fn()(str(FIXTURES / "simple_led.kicad_sch"), str(tmp_path / "out"), png_dpi=100)
    assert seen["cmd"][1] == "-I"
    assert set(scoped.glob("png_render_*")) == before


def _direct(tmp_path, out_name="out"):
    from boardproof import png_render

    return png_render.render_pdf_pages(tmp_path / "x.pdf", tmp_path / out_name, "s", 100)


def test_png_renderer_waits_for_the_pdfium_lock_before_spawning(tmp_path, monkeypatch):
    import threading
    import time

    from boardproof import png_render

    spawned = threading.Event()
    real_run = png_render.subprocess.run

    def spy(cmd, **kw):
        spawned.set()
        return real_run(cmd, **kw)

    monkeypatch.setattr(png_render.subprocess, "run", spy)
    monkeypatch.setattr(png_render, "_WORKER", _fake_worker(tmp_path, "import os\nos._exit(3)\n"))
    errors = []

    def go():
        try:
            _direct(tmp_path)
        except png_render.PngRenderError as exc:
            errors.append(exc)

    with png_render.PDFIUM_LOCK:
        t = threading.Thread(target=go)
        t.start()
        time.sleep(1.0)
        assert not spawned.is_set()
    t.join(timeout=60)
    assert spawned.is_set() and len(errors) == 1


def test_png_renderer_unusable_output_is_png_render_failed(tmp_path, monkeypatch):
    import pytest

    from boardproof import png_render

    for i, (body, message) in enumerate(
        [
            ("print('[]')\n", "wrote no PNG files"),  # empty page list
            ("print('garbage')\n", "no usable page list"),  # not JSON
            ("print('[\"never-written.png\"]')\n", "wrote no PNG files"),  # names a file it never wrote
            (  # a.png written, b.png named but missing: nothing may be moved
                "import os,sys\nopen(os.path.join(sys.argv[2],'a.png'),'wb').write(b'x')\n"
                "print('[\"a.png\",\"b.png\"]')\n",
                "wrote no PNG files",
            ),
        ]
    ):
        monkeypatch.setattr(png_render, "_WORKER", _fake_worker(tmp_path, body))
        with pytest.raises(png_render.PngRenderError, match=message):
            _direct(tmp_path, f"out{i}")


_TWO_PNG_WORKER = (
    "import sys, json, os\n"
    "d = sys.argv[2]\n"
    "for n in ('a.png', 'b.png'):\n"
    "    open(os.path.join(d, n), 'wb').write(b'x')\n"
    "print(json.dumps(['a.png', 'b.png']))\n"
)


def test_unwritable_output_dir_is_png_render_failed_not_a_raw_error(tmp_path, monkeypatch):
    import pytest

    from boardproof import png_render

    monkeypatch.setattr(png_render, "_WORKER", _fake_worker(tmp_path, _TWO_PNG_WORKER))
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(png_render.PngRenderError):
        png_render.render_pdf_pages(tmp_path / "x.pdf", blocker / "sub", "s", 100)


def test_failed_move_leaves_no_half_moved_png_set(tmp_path, monkeypatch):
    import shutil

    import pytest

    from boardproof import png_render

    monkeypatch.setattr(png_render, "_WORKER", _fake_worker(tmp_path, _TWO_PNG_WORKER))
    real_move = shutil.move
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise PermissionError("denied")
        return real_move(src, dst)

    monkeypatch.setattr(png_render.shutil, "move", flaky)
    out = tmp_path / "out"
    with pytest.raises(png_render.PngRenderError, match="denied"):
        png_render.render_pdf_pages(tmp_path / "x.pdf", out, "s", 100)
    assert not list(out.glob("*.png"))


def test_failed_cleanup_unlink_still_surfaces_png_render_error(tmp_path, monkeypatch):
    import shutil

    import pytest

    from boardproof import png_render

    monkeypatch.setattr(png_render, "_WORKER", _fake_worker(tmp_path, _TWO_PNG_WORKER))
    real_move = shutil.move
    calls = []

    def flaky(src, dst):
        calls.append(src)
        if len(calls) == 2:
            raise PermissionError("denied")
        return real_move(src, dst)

    monkeypatch.setattr(png_render.shutil, "move", flaky)
    real_unlink = Path.unlink

    def bad_unlink(self, *a, **k):
        if self.suffix == ".png" and self.parent.name == "out":
            raise PermissionError("locked")
        return real_unlink(self, *a, **k)

    monkeypatch.setattr(Path, "unlink", bad_unlink)
    with pytest.raises(png_render.PngRenderError, match="denied"):
        png_render.render_pdf_pages(tmp_path / "x.pdf", tmp_path / "out", "s", 100)
