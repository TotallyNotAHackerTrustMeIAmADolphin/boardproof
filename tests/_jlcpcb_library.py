"""The real JLCPCB library for tests: skip when it is absent, fail when the switch is set (issue 9).

The tests that compare the vendored part-tier index with what the pinned library builds need the
real checkout (a git submodule, empty in a fresh worktree). A skip there lets a stale index pass.
``BOARDPROOF_JLCPCB_LIBRARY_REQUIRED`` (any value but empty or ``0``) turns that skip into a failure,
the same rule as ``KICAD_DEMOS_REQUIRED`` and ``KICAD_ROUTING_TOOLS_REQUIRED``. The batch gate does
NOT set it (docs/TESTING.md, "Test suite"): a worktree has no submodule checkout, so the gate would be
red for every builder; set it, together with ``BOARDPROOF_JLCPCB_LIBRARY`` pointing at a checkout of
the pinned commit, for the run that owns the index.

Lookup order is the product's own: ``BOARDPROOF_JLCPCB_LIBRARY`` first, then the submodule path.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import NoReturn

import pytest

from boardproof.sourcing import jlcpcb_basic as jlc

REQUIRED_FLAG = "BOARDPROOF_JLCPCB_LIBRARY_REQUIRED"


def library_required(env: Mapping[str, str] = os.environ) -> bool:
    """True when the switch is set to anything but empty or ``0``."""
    return env.get(REQUIRED_FLAG, "").strip() not in ("", "0")


def missing(reason: str, env: Mapping[str, str] = os.environ) -> NoReturn:
    if library_required(env):
        pytest.fail(f"the JLCPCB library is required ({REQUIRED_FLAG} is set) but not usable: {reason}", pytrace=False)
    pytest.skip(f"{reason}; set {REQUIRED_FLAG}=1 to make this a failure")


def real_library(env: Mapping[str, str] = os.environ, checkout_root: Path | None = None) -> Path:
    """The library root: the environment variable's directory when set (it must hold a library, or
    this is a miss, never a silent fall back to another one), else the submodule of this checkout."""
    named = env.get(jlc.JLCPCB_LIBRARY_ENV_VAR, "").strip()
    if named:
        path = Path(named)
        if path.is_dir() and jlc._library_layout(path) is not None:
            return path
        missing(f"{jlc.JLCPCB_LIBRARY_ENV_VAR}={named} holds no 'symbols/JLCPCB-*.kicad_sym'", env)
    root = checkout_root if checkout_root is not None else jlc._checkout_root()
    path = root / jlc.SUBMODULE_RELATIVE_PATH if root else None
    if path is None or not path.is_dir() or jlc._library_layout(path) is None:
        missing(f"JLCPCB library submodule not initialised; fetch it with: {jlc.SUBMODULE_FETCH_COMMAND}", env)
    return path
