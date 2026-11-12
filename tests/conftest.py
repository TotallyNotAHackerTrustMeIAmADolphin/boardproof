"""Shared fixtures.

``enforce_seams``: the rung runner marks any batch run through an injected seam (drc,
backend, preflight, repo_state, clock, ...) unofficial, so the runner's own tests, which
all use fakes, could never observe an official run. They run with the policy lifted by
default (``rung_runner.SEAMS_ENFORCED = False``); tests of the policy itself request
``enforce_seams``.

``kicad_demos_dir`` / ``kicad_demo``: KiCad's demos, found in the install (build ticket 34);
``demo_install_guard`` checks the whole demos tree is unchanged at session end (build ticket 59).

``needs_krt`` marker / ``krt_checkout``: KiCadRoutingTools; absent, the test skips with the
probe's reason, or fails when ``KICAD_ROUTING_TOOLS_REQUIRED`` is set (the gate run; build
ticket 53, rule in ``tests/_krt.py``).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

import pytest

from tests import _kicad_demos, _krt, _ngspice
from tests._test_times import pytest_runtest_logfinish as pytest_runtest_logfinish  # issue 65
from tests._test_times import pytest_runtest_logstart as pytest_runtest_logstart
from tests._test_times import pytest_sessionstart as pytest_sessionstart  # issue 154: xdist controller writes none

# Issue 172: the opt-in replay cache of kicad-cli upgrade runs (BOARDPROOF_CLI_REPLAY=1); docs/TESTING.md
from tests import _cli_replay
from tests._cli_replay import pytest_sessionfinish as pytest_sessionfinish
from tests._cli_replay import pytest_terminal_summary as pytest_terminal_summary
from tests._cli_replay import pytest_unconfigure as pytest_unconfigure


# Issue 45: the suite never writes the real usage log. Set at import (before any server is built) so
# every test and every subprocess a test spawns inherits "off"; tests of the log itself point it at
# tmp_path. tests/test_usage_log.py proves the real data directory stays untouched.
os.environ["BOARDPROOF_USAGE_LOG"] = "off"


def pytest_configure(config):
    config.addinivalue_line("markers", _krt.MARKER_HELP)
    _cli_replay.pytest_configure(config)


def pytest_runtest_setup(item):
    """``needs_krt``: decided before the test's own fixtures run (a module-scoped fixture
    that drives KRT is never set up when it is absent)."""
    _cli_replay.pytest_runtest_setup(item)
    if item.get_closest_marker(_krt.MARKER) is not None:
        _krt.require()


@pytest.fixture(scope="session")
def krt_checkout() -> Path:
    """The KiCadRoutingTools checkout (``KICAD_ROUTING_TOOLS_PATH``, else ``.tools/KiCadRoutingTools``, else the KiCad plugin
    directory), for tests that only read it. Missing: skip with the reason, or fail when
    ``KICAD_ROUTING_TOOLS_REQUIRED`` is set."""
    return _krt.checkout_or_missing()


@pytest.fixture(scope="session")
def ngspice_exe() -> Path:
    """The pinned ngspice (``NGSPICE_PATH``, else PATH). Missing: skip with the reason (ngspice is
    optional in the batch gate while the simulation map is parked, issue 40; known-skips.toml allows
    those skips), or fail only when ``NGSPICE_REQUIRED`` is set (build ticket 02, spice-simulation;
    rule in ``tests/_ngspice.py``)."""
    return _ngspice.exe_or_missing()


_RUNG_TEST_PREFIXES = ("test_rung_runner", "test_autonomy_run", "test_rung_backends")


@pytest.fixture(autouse=True)
def _seams_lifted_for_rung_tests(request, monkeypatch):
    name = request.module.__name__.rsplit(".", 1)[-1]
    if name.startswith(_RUNG_TEST_PREFIXES) and "enforce_seams" not in request.fixturenames:
        from boardproof import rung_runner

        monkeypatch.setattr(rung_runner, "SEAMS_ENFORCED", False)


@pytest.fixture
def enforce_seams():
    """Marker fixture: leave the seam policy on for this test."""
    return True


@pytest.fixture(autouse=True)
def _no_default_router_folder_of_this_machine(request, monkeypatch, tmp_path_factory):
    """Issue 64: ``find_kicad_routing_tools`` falls back to ``.tools/KiCadRoutingTools`` of the
    repository (or of the main checkout of a linked worktree). A machine that has restored it must not
    change the tests of "KRT missing" or of the platform glob, so the default-folder discovery looks at
    an empty directory. Opt-out: the tests that need the real router (``needs_krt``, ``krt_checkout``)."""
    if request.node.get_closest_marker("needs_krt") or "krt_checkout" in request.fixturenames:
        return
    from boardproof.routers import kicad_routing_tools as krt

    monkeypatch.setattr(krt, "REPO_ROOT", tmp_path_factory.mktemp("no-default-krt"))


@pytest.fixture(autouse=True)
def _write_layer_load_memo_is_per_test():
    """Issue 172: the write layer remembers sheets that KiCad loaded (one ``sch upgrade`` per distinct
    content). A test that fakes kicad-cli's answer, or counts its runs, must not see another test's memory."""
    from boardproof import write_layer

    write_layer.clear_load_check_memo()
    yield


@pytest.fixture(autouse=True)
def _schematic_backups_in_tmp(monkeypatch, tmp_path_factory):
    """assign_footprints and fix_annotation back up every sheet to a durable per-user
    directory (build ticket 26); tests send those copies to a temporary directory instead."""
    monkeypatch.setenv("BOARDPROOF_BACKUP_DIR", str(tmp_path_factory.mktemp("schematic-backups")))


@pytest.fixture(scope="session")
def kicad_demos_dir(demo_install_guard) -> Path:
    """KiCad's demos directory in the install (build ticket 34; rule in ``tests/_kicad_demos.py``).

    ``KICAD_DEMOS_DIR`` first, then ``<prefix>/share/kicad/demos`` next to kicad-cli. Missing:
    skip with the reason, or fail when ``KICAD_DEMOS_REQUIRED`` is set (the gate run).
    Read only: tests that write use ``kicad_demo``, which hands out a copy. The whole tree is
    hashed here, before any test sees it, and checked at session end by ``demo_install_guard``.
    """
    from boardproof import kicad_cli

    path = _kicad_demos.demos_dir_or_missing(os.environ, kicad_cli.ensure_kicad_cli_on_path)
    demo_install_guard.record(path)
    return path


@pytest.fixture(scope="session")
def demo_install_guard():
    """Holds the digest of the whole demos tree, taken at ``kicad_demos_dir``'s first setup,
    and fails the session's teardown if any file in it changed, appeared or vanished by its
    end, whether or not a test copied that demo: the install is only read."""
    guard = _kicad_demos.InstallGuard()
    yield guard
    changes = guard.changes()
    if changes:
        pytest.fail("the KiCad install's demos changed during the session:\n" + "\n".join(changes))


@pytest.fixture
def kicad_demo(
    kicad_demos_dir: Path, demo_install_guard, tmp_path: Path
) -> Callable[[str], Path]:
    """``kicad_demo("pic_programmer")`` copies that demo project into ``tmp_path/demos`` and
    returns the copy's directory; calling it again gives a separate copy. A demo missing from
    the install skips or fails as above."""

    def copy(name: str) -> Path:
        return _kicad_demos.copy_demo(
            kicad_demos_dir, name, tmp_path / "demos", guard=demo_install_guard
        )

    return copy
