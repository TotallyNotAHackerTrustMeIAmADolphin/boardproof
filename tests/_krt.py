"""KiCadRoutingTools (KRT) for tests: skip when it is absent, fail in the gate run (build ticket 53).

KRT is an environment fact, not code: a test that needs it skips with the probe's own reason
in an ad-hoc run, and fails when ``KICAD_ROUTING_TOOLS_REQUIRED`` is set (any value but empty
or ``0``; the local gate run sets it), so the gate can never pass by skipping the KRT tests.
The same rule as ``KICAD_DEMOS_REQUIRED`` for KiCad's demos (``tests/_kicad_demos.py``).

Every KRT availability decision in ``tests/`` goes through here: the ``needs_krt`` marker
(``tests/conftest.py`` calls ``require`` before the test's fixtures) for tests that run the
router, and the ``krt_checkout`` fixture (``checkout_or_missing``) for tests that only read the
checkout. ``tests/test_krt_required.py`` holds the ratchet that keeps it that way.

The probe is ``check_availability()`` (checkout, interpreter packages, the prebuilt
``grid_router``). It runs a subprocess, so its result is cached per (``KICAD_ROUTING_TOOLS_PATH``,
``KICAD_ROUTING_TOOLS_PYTHON``): a test that points the variables elsewhere gets a fresh probe.
"""
from __future__ import annotations

import functools
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import NoReturn

import pytest

REQUIRED_FLAG = "KICAD_ROUTING_TOOLS_REQUIRED"
MARKER = "needs_krt"
MARKER_HELP = (
    f"{MARKER}: needs a usable KiCadRoutingTools install; skips with the probe's reason when "
    f"it is absent, fails instead when {REQUIRED_FLAG} is set (the gate run; build ticket 53)"
)


def krt_required(env: Mapping[str, str] = os.environ) -> bool:
    """True when ``KICAD_ROUTING_TOOLS_REQUIRED`` is set to anything but empty or ``0``."""
    return env.get(REQUIRED_FLAG, "").strip() not in ("", "0")


def missing(reason: str, env: Mapping[str, str] = os.environ) -> NoReturn:
    """Skip with the reason, or fail when ``KICAD_ROUTING_TOOLS_REQUIRED`` is set."""
    if krt_required(env):
        pytest.fail(
            f"KiCadRoutingTools is required ({REQUIRED_FLAG} is set) but not usable: {reason}",
            pytrace=False,
        )
    pytest.skip(
        f"KiCadRoutingTools not usable: {reason.rstrip('. ')}. Set KICAD_ROUTING_TOOLS_PATH to the checkout; "
        f"the gate run sets {REQUIRED_FLAG}=1, which makes this a failure"
    )


@functools.lru_cache(maxsize=None)
def _probe(path: str | None, python: str | None) -> tuple[bool, str]:
    """``check_availability()`` for one setting of the two variables (they are the cache key;
    the probe itself reads them from ``os.environ``)."""
    from kicad_mcp.routers import kicad_routing_tools as krt

    try:
        result = krt.check_availability()
    except Exception as exc:  # noqa: BLE001 - documented never to raise; a raise is "not usable"
        return False, f"availability probe raised {type(exc).__name__}: {exc}"
    if result.get("status") == "complete":
        return True, ""
    return False, str(result.get("reason") or f"probe status {result.get('status')!r}")


def availability() -> tuple[bool, str]:
    """``(usable, reason)`` for the KRT that ``os.environ`` points at (cached per setting)."""
    from kicad_mcp.routers import kicad_routing_tools as krt

    return _probe(os.environ.get(krt.ENV_PATH), os.environ.get(krt.ENV_PYTHON))


def require(
    env: Mapping[str, str] = os.environ,
    probe: Callable[[], tuple[bool, str]] = availability,
) -> None:
    """The ``needs_krt`` marker's body: return when KRT is usable, else skip / fail
    (``env`` only carries the flag; the probe reads the KRT variables from ``os.environ``)."""
    usable, reason = probe()
    if not usable:
        missing(reason, env)


def checkout_or_missing(env: Mapping[str, str] = os.environ, find: Callable[[], Path] | None = None) -> Path:
    """The ``krt_checkout`` fixture's body: the KRT checkout directory, or skip / fail.

    For tests that only read the checkout (its CLI help) and run no route."""
    from kicad_mcp.routers import kicad_routing_tools as krt

    try:
        return Path((find or krt.find_kicad_routing_tools)())
    except krt.KicadRoutingToolsError as exc:
        missing(str(exc), env)
