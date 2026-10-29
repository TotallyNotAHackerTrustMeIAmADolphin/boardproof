"""Find KiCad's demo projects (and symbol libraries) for tests (build ticket 34, docs/TESTING.md layer 3).

Demos are read from the KiCad install, never from a hard-coded or another machine's path:
``KICAD_DEMOS_DIR`` first, then ``<prefix>/share/kicad/demos`` next to the
``<prefix>/bin/kicad-cli`` that ``kicad_cli.ensure_kicad_cli_on_path()`` finds (also next to
the link target when kicad-cli is reached through a symlink or junction). That one rule covers
Windows (``C:\\Program Files\\KiCad\\10.0\\share\\kicad\\demos``) and Ubuntu's ``kicad-demos``
package (``/usr/share/kicad/demos`` beside ``/usr/bin/kicad-cli``). The symbol libraries are
found the same way (``KICAD_SYMBOL_DIR``, then ``share/kicad/symbols``).

A missing demo skips in an ad-hoc run with the reason, and fails when ``KICAD_DEMOS_REQUIRED``
is set (the local gate run sets it), so the gate can never pass by skipping them.

Tests get a copy in ``tmp_path`` (``copy_demo``); the install is only read, and
``InstallGuard`` checks that at the end of the session: it hashes the whole demos tree when
the ``kicad_demos_dir`` fixture is first set up, so a write to a demo no test copied is caught
too (build ticket 59).
"""
from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path, PurePath
from typing import NoReturn, TypeVar

import pytest

DIR_VAR = "KICAD_DEMOS_DIR"
SYMBOL_DIR_VAR = "KICAD_SYMBOL_DIR"
REQUIRED_FLAG = "KICAD_DEMOS_REQUIRED"

P = TypeVar("P", bound=PurePath)


def demos_required(env: Mapping[str, str] = os.environ) -> bool:
    """True when ``KICAD_DEMOS_REQUIRED`` is set to anything but empty or ``0``."""
    return env.get(REQUIRED_FLAG, "").strip() not in ("", "0")


def share_dir_next_to(cli: P, name: str) -> P:
    """``<prefix>/bin/kicad-cli`` -> ``<prefix>/share/kicad/<name>``."""
    return cli.parent.parent / "share" / "kicad" / name


def demos_dir_next_to(cli: P) -> P:
    """``<prefix>/bin/kicad-cli`` -> ``<prefix>/share/kicad/demos``."""
    return share_dir_next_to(cli, "demos")


def _locate(
    env: Mapping[str, str], find_cli: Callable[[], Path], var: str, name: str
) -> tuple[Path | None, str]:
    from kicad_mcp.kicad_cli import KicadCliError

    override = env.get(var)
    if override:
        path = Path(override)
        if path.is_dir():
            return path, f"{var}={override}"
        return None, f"{var}={override!r} is not a directory"
    try:
        cli = Path(find_cli())
    except KicadCliError as exc:
        return None, f"{var} is unset and kicad-cli was not found ({exc})"
    candidates = [share_dir_next_to(cli, name)]
    try:
        resolved = share_dir_next_to(cli.resolve(), name)  # kicad-cli reached through a link
    except OSError:
        resolved = candidates[0]
    if resolved != candidates[0]:
        candidates.append(resolved)
    for path in candidates:
        if path.is_dir():
            return path, f"next to kicad-cli {cli}"
    looked = " or ".join(str(path) for path in candidates)
    return None, (
        f"{var} is unset and there is no {name} directory next to kicad-cli {cli} "
        f"(looked in {looked}; on Ubuntu the {name} come with the kicad-{name} package)"
    )


def locate_demos_dir(
    env: Mapping[str, str], find_cli: Callable[[], Path]
) -> tuple[Path | None, str]:
    """``(demos_dir, how it was found)`` or ``(None, why not)``.

    A ``KICAD_DEMOS_DIR`` that is set but wrong is reported, not silently bypassed.
    """
    return _locate(env, find_cli, DIR_VAR, "demos")


def missing(
    reason: str,
    env: Mapping[str, str] = os.environ,
    what: str = "KiCad demos",
    var: str = DIR_VAR,
) -> NoReturn:
    """Skip with the reason, or fail when ``KICAD_DEMOS_REQUIRED`` is set."""
    if demos_required(env):
        pytest.fail(f"{what} are required ({REQUIRED_FLAG} is set) but missing: {reason}", pytrace=False)
    pytest.skip(
        f"{what} not available: {reason}. Set {var} to their directory; "
        f"the gate run sets {REQUIRED_FLAG}=1, which makes this a failure"
    )


def demos_dir_or_missing(env: Mapping[str, str], find_cli: Callable[[], Path]) -> Path:
    """The ``kicad_demos_dir`` fixture's body: the demos directory, or skip / fail."""
    path, reason = locate_demos_dir(env, find_cli)
    if path is None:
        missing(reason, env)
    return path


def symbols_dir_or_missing(env: Mapping[str, str], find_cli: Callable[[], Path]) -> Path:
    """KiCad's symbol libraries (``KICAD_SYMBOL_DIR``, then ``share/kicad/symbols`` next to
    kicad-cli), or skip / fail by the same flag rule as the demos."""
    path, reason = _locate(env, find_cli, SYMBOL_DIR_VAR, "symbols")
    if path is None:
        missing(reason, env, what="KiCad symbol libraries", var=SYMBOL_DIR_VAR)
    return path


def tree_digest(root: Path) -> dict[str, str]:
    """sha256 of every file under ``root``, keyed by its POSIX relative path."""
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path(root).rglob("*"))
        if path.is_file()
    }


class InstallGuard:
    """Digests of every recorded tree, taken when it is first recorded; ``changes()``
    re-hashes them and names every changed, added or removed file.

    Paths are resolved before they are stored or compared (``demos/../other`` is not inside
    ``demos``; a link is followed), and a tree inside one already recorded is covered by it
    and not hashed again."""

    def __init__(self) -> None:
        self._before: dict[Path, dict[str, str]] = {}

    def record(self, source: Path) -> None:
        source = Path(source).resolve()
        if any(source.is_relative_to(root) for root in self._before):  # roots are resolved too
            return
        self._before[source] = tree_digest(source)

    def recorded(self) -> list[Path]:
        return list(self._before)

    def changes(self) -> list[str]:
        found = []
        for source, before in self._before.items():
            after = tree_digest(source) if source.is_dir() else {}
            for name in sorted(before.keys() | after.keys()):
                if name not in after:
                    found.append(f"{source}: {name} removed")
                elif name not in before:
                    found.append(f"{source}: {name} added")
                elif before[name] != after[name]:
                    found.append(f"{source}: {name} changed")
        return found


def copy_demo(
    demos_dir: Path,
    name: str,
    dest_root: Path,
    env: Mapping[str, str] = os.environ,
    guard: InstallGuard | None = None,
) -> Path:
    """Copy the demo project ``name`` under ``dest_root`` and return the copy.

    The first copy is ``dest_root/name``; a repeat call gives a separate copy at
    ``dest_root/<n>/name``, so the project keeps its directory name. ``shutil.copyfile``
    writes fresh files, so the copy is writable even where the install marks its files
    read-only; the install itself is only read (and recorded with ``guard``).
    """
    source = Path(demos_dir) / name
    if not source.is_dir():
        missing(f"demo {name!r} is not in {demos_dir}", env)
    if guard is not None:
        # Under the conftest fixtures this is covered by the whole-tree record that
        # ``kicad_demos_dir`` makes; it is the fallback for a direct caller with its own guard.
        guard.record(source)
    dest = Path(dest_root) / name
    n = 1
    while dest.exists():
        n += 1
        dest = Path(dest_root) / str(n) / name
    shutil.copytree(source, dest, copy_function=shutil.copyfile)
    return dest
