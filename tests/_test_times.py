"""Per-test START/END timestamps for the batch gate (issue 65).

Active only when ``KICAD_MCP_TEST_TIMES`` names a file; ``scripts/batch_gate.py`` sets it for the
suite run. One line per event, appended and closed at once so a killed run keeps what it wrote:
``<ISO time, milliseconds> START|END <node id>``. ``tests/conftest.py`` re-exports the hooks.

Under pytest-xdist (``--workers N`` of the gate, build ticket 154) the controller process reports every
event again, so it writes nothing, and each worker writes its own file, ``<name>-gwN.out`` for
``<name>.out`` (``config.workerinput`` names the worker; the role is kept in the environment, see ``ROLE_ENV``): no two processes share a file.
"""

from __future__ import annotations

import os
from datetime import datetime

ENV = "KICAD_MCP_TEST_TIMES"
# The role of THIS process ("controller", "gw0", ... or "" for a serial run) is kept in the environment
# as "<pid>:<role>", not in module globals: a test that loads the conftest again (tests/test_krt_required.py,
# tests/test_kicad_demos.py) resets the globals, and the first --workers 4 gate run then wrote a worker's
# events into the base file. The pid keeps a nested pytest subprocess, which inherits the variable, from
# taking the role of its parent; its own session start overwrites it.
ROLE_ENV = "KICAD_MCP_TEST_TIMES_ROLE"


def pytest_sessionstart(session):
    config = session.config
    if config.pluginmanager.hasplugin("dsession"):
        role = "controller"
    else:
        role = getattr(config, "workerinput", {}).get("workerid") or ""
    os.environ[ROLE_ENV] = f"{os.getpid()}:{role}"


def _path() -> str | None:
    path = os.environ.get(ENV)
    if not path:
        return None
    pid, _, role = os.environ.get(ROLE_ENV, "").partition(":")
    if pid != str(os.getpid()):
        role = ""  # inherited from a parent process, or never set: a serial run
    if role == "controller":
        return None
    if role:
        base, ext = os.path.splitext(path)
        path = f"{base}-{role}{ext}"
    return path


def _write(kind: str, nodeid: str) -> None:
    path = _path()
    if not path:
        return
    stamp = datetime.now().isoformat(timespec="milliseconds")
    with open(path, "a", encoding="utf-8") as f:
        f.write(f"{stamp} {kind} {nodeid}\n")


def pytest_runtest_logstart(nodeid, location):
    _write("START", nodeid)


def pytest_runtest_logfinish(nodeid, location):
    _write("END", nodeid)
