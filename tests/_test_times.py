"""Per-test START/END timestamps for the batch gate (issue 65).

Active only when ``KICAD_MCP_TEST_TIMES`` names a file; ``scripts/batch_gate.py`` sets it for the
suite run. One line per event, appended and closed at once so a killed run keeps what it wrote:
``<ISO time, milliseconds> START|END <node id>``. ``tests/conftest.py`` re-exports the two hooks.
"""

from __future__ import annotations

import os
from datetime import datetime

ENV = "KICAD_MCP_TEST_TIMES"


def _write(kind: str, nodeid: str) -> None:
    path = os.environ.get(ENV)
    if not path:
        return
    stamp = datetime.now().isoformat(timespec="milliseconds")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{stamp} {kind} {nodeid}\n")


def pytest_runtest_logstart(nodeid, location):
    _write("START", nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _write("END", nodeid)
