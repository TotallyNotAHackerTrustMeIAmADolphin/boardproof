"""ngspice for tests: skip when it is absent, fail when ``NGSPICE_REQUIRED`` is set (build ticket 02).

ngspice is an environment fact, not code. Since the owner decision of 2026-10-30 (issue 40) it is
optional while the simulation map is parked: the batch gate no longer sets ``NGSPICE_REQUIRED``, a
live test skips with the reason when ngspice is absent, and ``scripts/known-skips.toml`` allows
exactly those skips (a file and a ``max`` each), so they stay counted and printed and any other skip
is RED. The live tests still run when ``NGSPICE_PATH`` is set. ``NGSPICE_REQUIRED`` (any value but
empty or ``0``) still turns the skip into a failure, for a machine that wants it strict. The rule is
otherwise the same as
``KICAD_DEMOS_REQUIRED`` (``tests/_kicad_demos.py``) and ``KICAD_ROUTING_TOOLS_REQUIRED``
(``tests/_krt.py``).

The binary is found by ``simulation.find_ngspice()``: ``NGSPICE_PATH`` first (the pinned install,
``ngspice_con.exe`` on Windows), then PATH, then platform globs. A ``NGSPICE_PATH`` that is set
but wrong is reported, never silently bypassed.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NoReturn

import pytest

REQUIRED_FLAG = "NGSPICE_REQUIRED"
PATH_VAR = "NGSPICE_PATH"
PINNED_VERSION = "47"  # the version the recorded fixture and the live tests are pinned to


def ngspice_required(env: Mapping[str, str] = os.environ) -> bool:
    """True when ``NGSPICE_REQUIRED`` is set to anything but empty or ``0``."""
    return env.get(REQUIRED_FLAG, "").strip() not in ("", "0")


def missing(reason: str, env: Mapping[str, str] = os.environ) -> NoReturn:
    """Skip with the reason, or fail when ``NGSPICE_REQUIRED`` is set."""
    if ngspice_required(env):
        pytest.fail(f"ngspice is required ({REQUIRED_FLAG} is set) but not usable: {reason}", pytrace=False)
    pytest.skip(
        f"ngspice not installed/found: {reason.rstrip('. ')}. Set {PATH_VAR} to ngspice_con.exe "
        f"(pinned version {PINNED_VERSION}). ngspice is optional while the simulation map is parked (issue #40); "
        f"{REQUIRED_FLAG}=1 makes this a failure"
    )


def exe_or_missing(
    env: Mapping[str, str] = os.environ,
    find: Callable[[], Path | None] | None = None,
) -> Path:
    """The ngspice executable, or skip / fail by ``NGSPICE_REQUIRED``."""
    if find is None:
        from kicad_mcp.toolsets import simulation

        find = simulation.find_ngspice
    found = find()
    if found is None:
        override = env.get(PATH_VAR)
        missing(
            f"{PATH_VAR}={override!r} is not a file" if override else f"{PATH_VAR} is unset and none is on PATH",
            env,
        )
    return found
