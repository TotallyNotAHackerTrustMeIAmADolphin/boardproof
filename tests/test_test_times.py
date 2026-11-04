"""The per-test START/END timestamps the batch gate asks pytest for (issue 65).

The hook lives in ``tests/_test_times.py`` and is re-exported by ``tests/conftest.py``; it writes only
when ``KICAD_MCP_TEST_TIMES`` names a file. Each case runs a tiny test file in a subprocess pytest.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VAR = "KICAD_MCP_TEST_TIMES"
LINE = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3} (START|END) (\S+)$")

TINY = "def test_ok():\n    pass\n\n\ndef test_bad():\n    assert False\n"


def _pytest(tmp_path, times: Path | None):
    (tmp_path / "conftest.py").write_text("from tests._test_times import *  # noqa: F403\n", encoding="utf-8")
    (tmp_path / "test_tiny.py").write_text(TINY, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in (VAR, "PYTEST_ADDOPTS")}
    env["PYTHONPATH"] = str(ROOT)
    if times is not None:
        env[VAR] = str(times)
    return subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", str(tmp_path)],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=120)


def test_start_and_end_for_every_test_failing_one_included(tmp_path):
    times = tmp_path / "t.times.out"
    _pytest(tmp_path, times)
    lines = times.read_text(encoding="utf-8").splitlines()
    assert all(LINE.match(x) for x in lines), lines
    seen = [(m[1], m[2].split("::")[-1]) for m in map(LINE.match, lines)]
    assert seen == [("START", "test_ok"), ("END", "test_ok"), ("START", "test_bad"), ("END", "test_bad")]


def test_nothing_is_written_when_the_variable_is_unset(tmp_path):
    _pytest(tmp_path, None)
    assert not list(tmp_path.glob("*.times.out"))


def test_the_committed_conftest_carries_the_hooks():
    from tests import _test_times, conftest
    assert conftest.pytest_runtest_logstart is _test_times.pytest_runtest_logstart
    assert conftest.pytest_runtest_logfinish is _test_times.pytest_runtest_logfinish
