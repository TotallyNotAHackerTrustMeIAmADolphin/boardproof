"""Issue 105: inspect_datasheet renders its page images in a child process.

A native PDFium fault cannot be raised as an exception, so the faults here are real child
processes: a fake worker script that exits with a fault code, kills itself or hangs, swapped in
for ``png_render._CROP_WORKER`` (the pattern of the issue 104 tests in test_diagnostics_toolset.py).
The test process itself surviving each one is the proof that the server would survive.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from boardproof import png_render
from boardproof.sourcing import datasheet
from boardproof.sourcing.datasheet import inspect_datasheet
from tests.test_datasheet_pin_numbering import _image_page, _pdf

PNG_STUB = 'b"\\x89PNG\\r\\n\\x1a\\n" + b"stub" + bytes.fromhex("0000000049454e44ae426082")'
TRUNCATED_STUB = 'b"\\x89PNG\\r\\n\\x1a\\n" + b"stub"'


def _two_page_pdf(tmp_path: Path) -> Path:
    # Both pages name the package and carry a tiny image: two full_page regions.
    return _pdf(tmp_path / "two.pdf", [_image_page(20), _image_page(20)])


def _fake(tmp_path: Path, monkeypatch, body: str) -> None:
    w = tmp_path / "fake_crop_worker.py"
    w.write_text(body, encoding="utf-8")
    monkeypatch.setattr(png_render, "_CROP_WORKER", w)


def _run(tmp_path: Path):
    return inspect_datasheet(_two_page_pdf(tmp_path), package="SOT-23", image_dir=tmp_path / "img")


def _assert_incomplete(result, *needles: str) -> None:
    assert result["status"] == "incomplete"
    assert result["refusal"] == "png_render_failed"
    assert result["reason"].startswith("png_render_failed:")
    for n in needles:
        assert n in result["reason"]
    # the extraction itself is still reported, not dropped
    assert "pin_tables" in result and result["pin_numbering"]["machine_extracted"] is False


def test_real_worker_renders_both_pages_and_result_is_not_incomplete(tmp_path):
    result = _run(tmp_path)
    pn = result["pin_numbering"]
    assert [i["page"] for i in pn["page_images"]] == [1, 2]
    assert "page_images_failed" not in pn and "refusal" not in result
    assert all(Path(i["path"]).read_bytes().startswith(b"\x89PNG") for i in pn["page_images"])


def test_render_runs_in_the_child_not_in_the_server_process(tmp_path, monkeypatch):
    import pdfplumber.page

    def boom(*a, **k):
        raise AssertionError("PDFium was called in the server process")

    monkeypatch.setattr(pdfplumber.page.Page, "to_image", boom)
    cmds = []
    real_run = png_render.subprocess.run

    def spy(cmd, *a, **k):
        cmds.append(cmd)
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(png_render.subprocess, "run", spy)
    result = _run(tmp_path)
    assert result["pin_numbering"]["page_images"]
    assert len(cmds) == 1 and cmds[0][1] == "-I" and cmds[0][2] == str(png_render._CROP_WORKER)


def test_worker_that_dies_with_a_fault_code_is_a_structured_incomplete_result(tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, "import os\nos._exit(139)\n")
    result = _run(tmp_path)  # the test process is still alive to assert: the server survives
    _assert_incomplete(result, "139", "page 1", "page 2")
    assert result["pin_numbering"]["page_images"] == []
    assert not list((tmp_path / "img").glob("*.png"))


def test_worker_that_kills_itself_is_a_structured_incomplete_result(tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, "import os, signal\nos.kill(os.getpid(), signal.SIGTERM)\n")
    result = _run(tmp_path)
    _assert_incomplete(result, "PNG renderer exited with code")


def test_crash_after_the_first_page_keeps_that_image_and_names_the_failed_one(tmp_path, monkeypatch):
    body = (
        "import os, sys\n"
        f"open(os.path.join(sys.argv[2], 'region-0.png'), 'wb').write({PNG_STUB})\n"
        "os._exit(139)\n"
    )
    _fake(tmp_path, monkeypatch, body)
    result = _run(tmp_path)
    pn = result["pin_numbering"]
    assert [i["page"] for i in pn["page_images"]] == [1]
    assert Path(pn["page_images"][0]["path"]).is_file()
    assert [f["page"] for f in pn["page_images_failed"]] == [2]
    _assert_incomplete(result, "page 2", "139")
    assert "page 1:" not in result["reason"]


def test_hung_worker_is_killed_at_the_timeout(tmp_path, monkeypatch):
    _fake(tmp_path, monkeypatch, "import time\ntime.sleep(60)\n")
    monkeypatch.setattr(png_render, "PNG_TIMEOUT_SECONDS", 1)
    t0 = time.monotonic()
    result = _run(tmp_path)
    assert time.monotonic() - t0 < 30
    _assert_incomplete(result, "timed out after 1s")


@pytest.mark.parametrize(
    "body,needle",
    [
        ("print('not json')\n", "no usable result"),
        ("import json\nprint(json.dumps({'rendered': [0, 1], 'errors': {}}))\n", "wrote no PNG"),
        ("import json\nprint(json.dumps(['x']))\n", "no usable result"),
    ],
)
def test_unusable_worker_output_is_incomplete_never_ok(tmp_path, monkeypatch, body, needle):
    _fake(tmp_path, monkeypatch, body)
    _assert_incomplete(_run(tmp_path), needle)


def test_worker_that_cannot_start_is_incomplete(tmp_path, monkeypatch):
    def nope(*a, **k):
        raise OSError("no interpreter")

    monkeypatch.setattr(png_render.subprocess, "run", nope)
    _assert_incomplete(_run(tmp_path), "could not start")


def test_a_python_error_on_one_region_is_reported_and_the_others_still_render(tmp_path):
    pdf = _two_page_pdf(tmp_path)
    got = png_render.render_pdf_regions(pdf, [(1, None), (99, None), (2, None)], 100)
    assert got.images[0] and got.images[2] and got.images[1] is None
    assert "IndexError" in got.failures[1] and set(got.failures) == {1}


def test_no_regions_spawns_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(png_render.subprocess, "run", lambda *a, **k: pytest.fail("spawned"))
    got = png_render.render_pdf_regions(tmp_path / "x.pdf", [], 100)
    assert got.images == [] and got.failures == {}


def test_two_renders_never_run_at_once(tmp_path, monkeypatch):
    """The lock is held across the child's whole life: a second render waits for the first."""
    _fake(tmp_path, monkeypatch, "import os\nos._exit(3)\n")
    spawned = threading.Event()
    real_run = png_render.subprocess.run

    def spy(*a, **k):
        spawned.set()
        return real_run(*a, **k)

    monkeypatch.setattr(png_render.subprocess, "run", spy)
    pdf = _two_page_pdf(tmp_path)
    worker = threading.Thread(target=png_render.render_pdf_regions, args=(pdf, [(1, None)], 100))
    with png_render.PDFIUM_LOCK:
        worker.start()
        assert not spawned.wait(0.7), "a render started while the PDFium lock was held"
    worker.join(30)
    assert spawned.is_set() and not worker.is_alive()


def test_datasheet_module_no_longer_renders_in_process():
    assert not hasattr(datasheet, "_png_bytes") and not hasattr(datasheet, "PDFIUM_LOCK")


def test_truncated_png_from_a_killed_worker_is_rejected_not_returned(tmp_path, monkeypatch):
    body = (
        "import os, sys\n"
        f"open(os.path.join(sys.argv[2], 'region-0.png'), 'wb').write({TRUNCATED_STUB})\n"
        "os._exit(139)\n"
    )
    _fake(tmp_path, monkeypatch, body)  # the stub has the PNG signature but no IEND chunk
    got = png_render.render_pdf_regions(_two_page_pdf(tmp_path), [(1, None)], 100)
    assert got.images == [None] and "139" in got.failures[0]
