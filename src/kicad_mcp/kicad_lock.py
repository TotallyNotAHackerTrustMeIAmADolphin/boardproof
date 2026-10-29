"""Refuse to write a file KiCad has open (build ticket 41).

If the human has a board open in KiCad while a tool writes it, KiCad's next
save silently overwrites the tool's work. docs/ARCHITECTURE.md decision 12 and
``paradigm-review-2`` ticket 11 (Q4): while KiCad's lock file for a file
exists, writing tools refuse with ``file_open_in_kicad``; reads stay allowed;
a stale lock is reported, never deleted.

Facts this module rests on (recorded on KiCad 10.0.7 in ticket 41's Answer
and checked in the source at tag 10.0.7):

* The lock of ``<dir>/<name>.<ext>`` is ``<dir>/~<name>.<ext>.lck``
  (``include/lockfile.h``: prefix ``~``, extension appended with ``.lck``).
* Its content is ``{"hostname":"<wxGetHostName>","username":"<wxGetUserId>"}``.
  No process id and no time is recorded.
* pcbnew locks the board, eeschema only the **root** schematic (never a
  sub-sheet), every program that loads a project locks the ``.kicad_pro``.
  The symbol and footprint editors lock nothing.
* A clean close removes the lock; a crash or kill leaves it (stale). KiCad
  itself reclaims a board or sheet lock silently when it is "locked by me"
  (same user and host, or unreadable) and no other instance runs, but not a
  project lock in a standalone editor.

Which locks guard a target (``guarding_lock_files``):

* ``.kicad_pcb`` and ``.kicad_pro``: their own lock.
* ``.kicad_sch``: its own lock plus the root schematic lock of every project
  that encloses it (each directory at or above it holding a ``*.kicad_pro``,
  nearest first), because a sub-sheet is open whenever its root is and a
  nested project must not hide the outer one. Known gap: a sheet shared from
  a sibling folder (``../common/psu.kicad_sch``) is not linked to its user.
* ``.kicad_prl`` and project lib tables (``fp-lib-table``, ``sym-lib-table``,
  ``design-block-lib-table``, any letter case): the project lock; KiCad
  rewrites them with the project (pcbnew rewrote ``.kicad_pro`` and wrote
  ``.kicad_prl`` on a plain close in the reproduction).
* ``.kicad_dru``: the board lock and the project lock of the same stem
  (pcbnew's Board Setup writes it).
* A directory (a revision directory replaced as a whole): every lock-named
  entry anywhere inside it; each real directory is walked once, so a link or
  junction back up is not a loop.
* Anything else: its own lock by the same rule. KiCad 10.0.7 never creates
  one for a library file, so an open symbol or footprint editor is not
  detectable (known gap).

Each target is looked at both as given (made absolute) and resolved (8.3
short names expanded, links followed), so either spelling finds the lock;
a lock reached through two spellings is reported once. Anything at a lock
path counts as a lock, also a directory or a broken link. Known gap: a sheet
that a project reaches through a junction inside the project, written by its
real path elsewhere, is not linked to that project.

Liveness: KiCad records no process id. A lock counts as stale only when it is
readable, names this user on this host, and no KiCad program runs here.
An unreadable lock (empty, partial, not KiCad's JSON: possibly a cloud-sync
placeholder of another machine's lock) has an unknown owner and unknown
liveness. A process list that cannot be read is "unknown", never "none".

This module only reads. It never creates, changes or deletes a lock file.
``tests/test_kicad_lock.py`` holds it to that with an AST allowlist (every
module attribute it may touch is listed; ``subprocess.run`` only with a
literal tasklist/ps argv and five known keywords) and a disk check.
"""
from __future__ import annotations

import csv
import getpass
import io
import json
import os
import re
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable

CODE = "file_open_in_kicad"
LOCK_PREFIX = "~"
LOCK_EXTENSION = "lck"

WINDOWS = os.name == "nt"

# The programs that take locks (pcbnew/files.cpp, eeschema/files-io.cpp,
# settings_manager.cpp, kicad_manager_frame.cpp). kicad-cli takes none.
KICAD_LOCKING_PROGRAMS = frozenset({"kicad", "pcbnew", "eeschema"})

PROJECT_SIDE_FILES = frozenset({"fp-lib-table", "sym-lib-table", "design-block-lib-table"})

PROCESS_LIST_TIMEOUT_S = 15
_CREATE_NO_WINDOW = 0x08000000  # Windows process-creation flag: no console window

ProcessLister = Callable[[], "list[str] | None"]


def lock_file_for(path: str | os.PathLike[str]) -> Path:
    """KiCad's lock-file name for ``path``: ``~<name>.<ext>.lck`` beside it."""
    target = Path(os.path.abspath(path))
    return target.with_name(f"{LOCK_PREFIX}{target.name}.{LOCK_EXTENSION}")


def _guarded_file_of(lock: Path) -> Path:
    """The file a lock guards, from the lock's own name."""
    name = lock.name[len(LOCK_PREFIX):-(len(LOCK_EXTENSION) + 1)]
    return lock.with_name(name)


def _project_stems(directory: Path) -> list[str]:
    try:
        return sorted(p.stem for p in directory.glob("*.kicad_pro") if p.is_file())
    except OSError:
        return []


def _project_locks_in(directory: Path) -> list[Path]:
    return [directory / f"{LOCK_PREFIX}{stem}.kicad_pro.{LOCK_EXTENSION}" for stem in _project_stems(directory)]


def _enclosing_root_sheet_locks(start: Path) -> list[Path]:
    """The root-sheet lock of every project at or above ``start``, nearest first."""
    locks: list[Path] = []
    for directory in (start, *start.parents):
        locks += [directory / f"{LOCK_PREFIX}{s}.kicad_sch.{LOCK_EXTENSION}" for s in _project_stems(directory)]
    return locks


MAX_WALK_DEPTH = 64


def _is_lock_name(name: str) -> bool:
    return name.startswith(LOCK_PREFIX) and name.casefold().endswith(f".{LOCK_EXTENSION}") and len(name) > len(LOCK_PREFIX) + len(LOCK_EXTENSION) + 1


def _same_place(path: Path) -> str:
    """A spelling-independent key: the resolved parent (8.3 names expanded,
    links followed) plus the entry's own name, which is not followed."""
    try:
        parent = os.path.realpath(path.parent)
    except (OSError, ValueError):
        parent = str(path.parent)
    return os.path.normcase(str(Path(parent) / path.name))


def _locks_inside(directory: Path) -> list[Path]:
    """Every lock-named entry under ``directory``. Each real directory is
    visited once (a link or junction back up is not a loop) and the walk
    stops at ``MAX_WALK_DEPTH`` levels. Known limit: a lock deeper than
    ``MAX_WALK_DEPTH`` (64) levels, or under a folder that cannot be listed
    (``OSError``), is not seen."""
    found: list[Path] = []
    visited: set[str] = set()
    pending: list[tuple[Path, int]] = [(directory, 0)]
    while pending:
        current, depth = pending.pop()
        try:
            key = os.path.normcase(os.path.realpath(current))
        except (OSError, ValueError):
            continue
        if key in visited or depth > MAX_WALK_DEPTH:
            continue
        visited.add(key)
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for entry in entries:
            if _is_lock_name(entry.name):
                found.append(entry)
            try:
                is_dir = entry.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                pending.append((entry, depth + 1))
    return sorted(found)


def _guards(target: Path) -> list[Path]:
    if target.is_dir():
        return _locks_inside(target)
    suffix = target.suffix.lower()
    directory = target.parent
    stem = target.stem
    if suffix in (".kicad_pcb", ".kicad_pro"):
        return [lock_file_for(target)]
    if suffix == ".kicad_sch":
        return [lock_file_for(target), *_enclosing_root_sheet_locks(directory)]
    if suffix == ".kicad_prl":
        return [directory / f"{LOCK_PREFIX}{stem}.kicad_pro.{LOCK_EXTENSION}"]
    if suffix == ".kicad_dru":
        return [
            directory / f"{LOCK_PREFIX}{stem}.kicad_pcb.{LOCK_EXTENSION}",
            directory / f"{LOCK_PREFIX}{stem}.kicad_pro.{LOCK_EXTENSION}",
        ]
    if target.name.casefold() in PROJECT_SIDE_FILES:
        return _project_locks_in(directory)
    return [lock_file_for(target)]


def _spellings(target: Path) -> list[Path]:
    """The target as given and resolved (8.3 names expanded, links followed)."""
    try:
        resolved = Path(os.path.realpath(target))
    except (OSError, ValueError):
        return [target]
    return _dedupe([target, resolved])


def _all_guards(target: Path) -> list[Path]:
    return _dedupe([lock for spelling in _spellings(target) for lock in _guards(spelling)])


def _as_targets(paths: Any) -> list[Path]:
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    return _dedupe([Path(os.path.abspath(path)) for path in paths])


def _dedupe(paths: list[Path]) -> list[Path]:
    seen: set[str] = set()
    kept: list[Path] = []
    for path in paths:
        key = os.path.normcase(str(path))
        if key not in seen:
            seen.add(key)
            kept.append(path)
    return kept


def guarding_lock_files(paths: Any) -> dict[str, list[str]]:
    """For each target, the lock files (present or not) whose existence means
    KiCad has it open. A directory maps to the lock entries inside it."""
    return {str(t): [str(lock) for lock in _all_guards(t)] for t in _as_targets(paths)}


# --- who owns a lock, and is it stale -------------------------------------


def current_user() -> str:
    """This process's login name (KiCad: ``wxGetUserId``)."""
    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no login name is "unknown", not a crash
        return ""


def current_host() -> str:
    """This machine's name (KiCad: ``wxGetHostName``, the computer name)."""
    return os.environ.get("COMPUTERNAME") or socket.gethostname()


def _same_host(a: str, b: str) -> bool:
    def short(name: str) -> str:
        return name.split(".", 1)[0].casefold()

    return bool(a) and bool(b) and short(a) == short(b)


def _same_user(a: str, b: str) -> bool:
    if WINDOWS:
        return a.casefold() == b.casefold()
    return a == b


def kicad_programs_in(names: Iterable[str]) -> list[str]:
    """The entries of a process-name list that are KiCad programs that lock."""
    found = []
    for name in names:
        base = re.split(r"[\\/]", name)[-1].strip()
        program = base[:-4] if base.casefold().endswith(".exe") else base
        if program.casefold() in KICAD_LOCKING_PROGRAMS:
            found.append(base)
    return found


def kicad_processes_running(*, windows: bool | None = None) -> list[str] | None:
    """KiCad programs running on this host, or ``None`` if that cannot be told
    (the lister is missing, fails, times out or exits non-zero). Never ``[]``
    for "cannot tell": ``[]`` makes an own lock stale."""
    windows = WINDOWS if windows is None else windows
    try:
        if windows:
            done = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                errors="backslashreplace",
                timeout=PROCESS_LIST_TIMEOUT_S,
                creationflags=_CREATE_NO_WINDOW,
            )
        else:
            done = subprocess.run(
                ["ps", "-A", "-o", "comm="],
                capture_output=True,
                text=True,
                errors="backslashreplace",
                timeout=PROCESS_LIST_TIMEOUT_S,
            )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    if windows:
        names = [row[0] for row in csv.reader(io.StringIO(done.stdout)) if row]
    else:
        names = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    return kicad_programs_in(names)


def _read_owner(lock: Path) -> dict[str, str] | None:
    """The lock's owner, or ``None`` when it is not KiCad's readable JSON."""
    try:
        data = json.loads(lock.read_bytes().decode("utf-8"))
        owner = {"username": data["username"], "hostname": data["hostname"]}
    except (OSError, ValueError, KeyError, TypeError, IndexError, RecursionError):
        return None
    if not all(isinstance(v, str) for v in owner.values()):
        return None
    return owner


def _report(target: Path, lock: Path, running: Callable[[], list[str] | None]) -> dict[str, Any]:
    owner = _read_owner(lock)
    stale: bool | None
    mine: bool | None
    if owner is None:
        mine, stale = None, None
        reason = (
            "the lock file is not readable as KiCad's {hostname, username} (empty, partial, "
            "not JSON, or not a file); it may be a cloud-sync placeholder of another "
            "machine's lock, so neither its owner nor whether it is live is known"
        )
    else:
        mine = _same_user(owner["username"], current_user()) and _same_host(
            owner["hostname"], current_host()
        )
        if not mine:
            stale = None
            reason = (
                f"held by {owner['username']!r} on host {owner['hostname']!r}; KiCad records no "
                "process id, so whether that KiCad still runs cannot be checked from here"
            )
        else:
            programs = running()
            if programs is None:
                stale = None
                reason = "held by this user on this host; the running processes could not be listed"
            elif programs:
                stale = False
                reason = (
                    f"held by this user on this host, and KiCad runs here "
                    f"({', '.join(sorted(set(programs)))}), so the lock may be live "
                    "(KiCad records no process id to tell which file it holds)"
                )
            else:
                stale = True
                reason = (
                    "held by this user on this host, but no KiCad program (kicad, pcbnew, "
                    "eeschema) runs on this host: left behind by a crash or a killed KiCad"
                )
    return {
        "file": str(target),
        "lock_file": str(lock),
        "owner": owner,
        "owner_readable": owner is not None,
        "owned_by_this_user_and_host": mine,
        "stale": stale,
        "stale_reason": reason,
    }


def find_locks(paths: Any, *, kicad_running: ProcessLister | None = None) -> list[dict[str, Any]]:
    """Every existing lock entry that guards one of ``paths``, with its owner
    and a stale report. The process list is read at most once, and only when
    a readable own lock needs it."""
    lister = kicad_running or kicad_processes_running
    cache: list[list[str] | None] = []

    def running() -> list[str] | None:
        if not cache:
            cache.append(lister())
        return cache[0]

    reports: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for target in _as_targets(paths):
        is_dir = target.is_dir()
        for lock in _all_guards(target):
            if not os.path.lexists(lock):
                continue
            guarded = _guarded_file_of(lock) if is_dir else target
            # One lock reached through two spellings (8.3 name, junction) is one lock.
            key = (_same_place(guarded), _same_place(lock))
            if key in seen:
                continue
            seen.add(key)
            reports.append(_report(guarded, lock, running))
    return reports


def refusal(
    file: str | os.PathLike[str],
    lock_file: str | os.PathLike[str],
    locks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """The ``file_open_in_kicad`` refusal for a write to ``file``. The hint
    mentions removing a lock only for locks reported ``stale: true``."""
    locks = locks if locks is not None else [{"file": str(file), "lock_file": str(lock_file)}]
    stale = [entry for entry in locks if entry.get("stale") is True]
    message = (
        f"{file} is open in KiCad: its lock file {lock_file} exists. A write now would be "
        "overwritten by KiCad's next save, so nothing was written."
    )
    if len(locks) > 1:
        message += f" {len(locks)} lock files guard the files of this write; all are listed in 'locks'."
    hint = "Save and close the file in KiCad, then retry; reading tools keep working meanwhile."
    if stale:
        message += (
            " " + ("This lock looks" if len(stale) == len(locks) else "Some of these locks look")
            + " stale (no KiCad program runs on this host). No tool deletes a lock file."
        )
        hint += (
            " These lock files are stale (this user, this host, no KiCad running): "
            + ", ".join(str(entry["lock_file"]) for entry in stale)
            + ". Once sure no KiCad has the file open, the human may remove them by hand."
        )
    return {
        "ok": False,
        "code": CODE,
        "file": str(file),
        "lock_file": str(lock_file),
        "locks": locks,
        "message": message,
        "hint": hint,
    }


def check_writable(paths: Any, *, kicad_running: ProcessLister | None = None) -> dict[str, Any] | None:
    """``None`` when no KiCad lock guards any of ``paths``; otherwise the
    ``file_open_in_kicad`` refusal naming the first locked file and its lock,
    with every lock in ``locks``. A writer calls this before it writes."""
    locks = find_locks(paths, kicad_running=kicad_running)
    if not locks:
        return None
    first = locks[0]
    return refusal(first["file"], first["lock_file"], locks)
