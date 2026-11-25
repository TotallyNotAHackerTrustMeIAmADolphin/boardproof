"""Issue 241: the session guard against foreign KiCad files above the pytest base temp folder."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from tests._temp_guard import stray_kicad_files, stray_message


def test_a_kicad_file_in_a_parent_folder_is_named(tmp_path):
    root = tmp_path / "temp"
    root.mkdir()
    stray = tmp_path / "fo_test.kicad_stray"
    stray.write_text("{}")
    found = stray_kicad_files(root)
    assert stray.resolve() in found
    assert "fo_test.kicad_stray" in stray_message(found)


def test_files_in_or_below_the_base_do_not_count(tmp_path):
    base = tmp_path / "base"
    (base / "sub").mkdir(parents=True)
    (base / "sub" / "b.kicad_stray").write_text("")
    (tmp_path / "c.kicad_sym.bak").write_text("")
    assert not [p for p in stray_kicad_files(base) if p.name in ("b.kicad_stray", "c.kicad_sym.bak")]


def test_the_session_hook_ends_a_run_with_a_stray_file_in_the_temp_folder(tmp_path):
    temp = tmp_path / "temp"
    temp.mkdir()
    (temp / "fo_test.kicad_stray").write_text("{}")
    env = {**os.environ, "TEMP": str(temp), "TMP": str(temp), "TMPDIR": str(temp)}
    probe = "tests/test_temp_guard.py::test_files_in_or_below_the_base_do_not_count"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", probe, "-p", "no:cacheprovider"],
        cwd=Path(__file__).parent.parent, env=env, capture_output=True, text=True, timeout=300,
    )
    assert run.returncode == 3, run.stdout + run.stderr
    assert "fo_test.kicad_stray" in run.stdout + run.stderr
