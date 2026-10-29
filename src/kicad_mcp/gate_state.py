"""Session store for the last acceptance-gate verdict, and the ``gate`` field.

Every tool that returns the state of a board carries a ``gate`` field so an
agent can see, next to whatever else the tool said, what the acceptance gate
last concluded about *exactly that file*:

    {"verdict": "PASS"|"FAIL"|"INCOMPLETE"|"NOT_RUN",
     "board_sha": "<sha256 of the board file now>",
     "project_sha": "<one hash over the board and every file the gate reads>",
     "verdict_board_sha": "<sha256 the verdict was recorded for>"|None,
     "age_s": seconds since the verdict|None,
     "stale": bool,
     "note": "...",
     "fingerprint_note": "..."}        # only when the project could not be hashed

Rules (docs/ARCHITECTURE.md decision 14, spec "Gate field"):

* A verdict is keyed by the board's resolved path, its content hash and the
  hash of everything the gate's graded copy reads. It is never shown for a
  different file; a different path has no verdict.
* ``stale`` is True when there is no verdict; when the board's hash or the
  project hash differs from the one recorded; when any mutating tool ran
  against that board's directory (or a directory above or below it) after the
  verdict; or when any mutating call could not say which board it touched.
* Fail safe: a project too large to fingerprint (file-count or size cap, the
  size cap being verify_board's own copy cap) or with an unreadable input file
  gets ``project_sha: None``, ``stale: true`` and a ``fingerprint_note``.
* Conservative on purpose, and kept: ``export_*`` tools are not read-only, so
  exporting stales a PASS even though it leaves the board alone; and
  ``confirm_component_match`` has a ``revision_token`` but names no board, so it
  raises the global unknown-mutation mark and stales every verdict. A false
  "stale" costs one re-run of ``verify_board``; a false "fresh" costs a wrong
  claim.
* Remaining trade-off: bulk files (symbol/footprint libraries, sub-sheets,
  lib tables, ``.kicad_prl``) are hashed through a (path, size, mtime_ns)
  cache, so a same-size edit that also restores the mtime to the nanosecond is
  missed for those files. The board, ``.kicad_dru`` and ``.kicad_pro`` are
  always re-read.
* Nothing in this module says a board is routed or done. Only the gate's own
  output may; this field points at the gate instead.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_mcp.verify_board import _MAX_COPY_BYTES, _SKIP_DIRS

NOT_RUN = "NOT_RUN"
_NOTE = "Only a verify_board PASS certifies this board; call verify_board for a current verdict."

_MAX_FILES = 20_000  # input files per project; over this the project is not fingerprinted
_HASH_CACHE_ENTRIES = 4096
# The board, its rules and its project settings are small and are exactly the
# files whose silent edit this field exists to catch, so they are read on every
# call. Only bulk files (libraries, footprints, sub-sheets) use the cache.
_ALWAYS_READ_SUFFIXES = {".kicad_pcb", ".kicad_dru", ".kicad_pro"}
_TOKEN_ENTRIES = 1024
_TOO_LARGE = "project too large to fingerprint: verdict cannot be shown fresh"


@dataclass
class _Verdict:
    verdict: str
    sha: str | None
    project_sha: str | None
    recorded_at: float
    mutation_seq: int


@dataclass(frozen=True)
class Snapshot:
    """What was true when grading began (taken BEFORE the gate runs)."""

    sha: str | None
    project_sha: str | None
    mutation_seq: int


def _hash_file(path: Path) -> str | None:
    """sha256 of the file's bytes, or None when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


# The files the gate's graded copy can read besides the board: schematic sheets,
# rules, project settings, libraries, lib tables. The walk mirrors
# verify_board._copy_project (whole directory tree, symlinks followed because
# copytree(symlinks=False) follows them, the same skipped directories, other
# boards in the board's own directory left out).
_INPUT_SUFFIXES = {".kicad_sch", ".kicad_dru", ".kicad_pro", ".kicad_sym", ".kicad_mod", ".kicad_prl"}
_INPUT_NAMES = {"fp-lib-table", "sym-lib-table"}


def _skipped_dir(name: str) -> bool:
    return name in _SKIP_DIRS or name.endswith("-backups")


def _prune_cycles(directory: str, dirs: list[str], chains: dict[str, frozenset[str]]) -> None:
    """Drop subdirectories whose real path is already on the way down to here
    (a symlink loop), and remember each kept subdirectory's chain."""
    here = chains[directory]
    kept = []
    for name in dirs:
        child = os.path.join(directory, name)
        real = os.path.realpath(child)
        if real in here:
            continue
        chains[child] = here | {real}
        kept.append(name)
    dirs[:] = kept


@dataclass
class _Scan:
    """One walk of a project directory: its boards and its non-board inputs."""

    boards: dict[Path, os.stat_result] = field(default_factory=dict)
    inputs: list[tuple[str, Path, os.stat_result]] = field(default_factory=list)
    error: str | None = None


def _scan(root: Path) -> _Scan:
    scan = _Scan()
    total = 0
    chains = {str(root): frozenset({os.path.realpath(root)})}
    try:
        for directory, dirs, files in os.walk(root, followlinks=True):
            dirs[:] = sorted(d for d in dirs if not _skipped_dir(d))
            _prune_cycles(directory, dirs, chains)
            for name in sorted(files):
                full = Path(directory) / name
                lower = name.lower()
                if Path(directory) == root and lower.endswith(".kicad_pcb"):
                    scan.boards[full] = full.stat()
                elif Path(name).suffix.lower() in _INPUT_SUFFIXES or lower in _INPUT_NAMES:
                    info = full.stat()
                    scan.inputs.append((str(full.relative_to(root)).replace("\\", "/"), full, info))
                    total += info.st_size
                    if len(scan.inputs) > _MAX_FILES or total > _MAX_COPY_BYTES:
                        scan.error = f"{_TOO_LARGE} ({len(scan.inputs)} files, {total} bytes)"
                        return scan
    except OSError as exc:
        scan.error = f"project could not be fingerprinted ({exc}): verdict cannot be shown fresh"
    return scan


@dataclass(frozen=True)
class _Fingerprint:
    board_sha: str | None
    project_sha: str | None
    note: str | None


def _key(path: str | Path) -> str:
    try:
        return str(Path(path).resolve()).lower()
    except OSError:
        return str(path).lower()


def _related(a: Path, b: Path) -> bool:
    """True when one directory is the other or an ancestor of it."""
    return a == b or a in b.parents or b in a.parents


class GateState:
    """Per-server-session record of verdicts, mutations and revision tokens."""

    def __init__(self, clock=time.time) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._verdicts: dict[str, _Verdict] = {}
        self._mutated_dirs: dict[Path, int] = {}  # directory -> latest mutation seq
        self._unknown_seq = 0  # latest mutation that could not name its board
        self._seq = 0
        # (path, size, mtime_ns) -> sha256 ; an unchanged file is never re-read.
        self._hashes: OrderedDict[tuple[str, int, int], str] = OrderedDict()
        self._tokens: OrderedDict[str, list[str]] = OrderedDict()

    # -- fingerprints --------------------------------------------------------
    def _cached_hash(self, path: Path, info: os.stat_result) -> str | None:
        if path.suffix.lower() in _ALWAYS_READ_SUFFIXES:
            return _hash_file(path)  # never trust (size, mtime) for these
        key = (str(path), info.st_size, info.st_mtime_ns)
        with self._lock:
            hit = self._hashes.get(key)
            if hit is not None:
                self._hashes.move_to_end(key)
                return hit
        digest = _hash_file(path)  # outside the lock: file reads are slow
        if digest is not None:
            with self._lock:
                self._hashes[key] = digest
                while len(self._hashes) > _HASH_CACHE_ENTRIES:
                    self._hashes.popitem(last=False)
        return digest

    def _fingerprint(self, pcb: Path, scans: dict[Path, _Scan]) -> _Fingerprint:
        root = pcb.parent
        scan = scans.get(root)
        if scan is None:  # one walk per directory per call
            scan = scans[root] = _scan(root)
        board_info = scan.boards.get(root / pcb.name)
        if board_info is None:
            try:
                board_info = pcb.stat()
            except OSError:
                return _Fingerprint(None, None, f"board unreadable: {pcb}")
        board_sha = self._cached_hash(pcb, board_info)
        if board_sha is None:
            return _Fingerprint(None, None, f"board unreadable: {pcb}")
        if scan.error is not None:
            return _Fingerprint(board_sha, None, scan.error)
        digest = hashlib.sha256()
        digest.update(f"{pcb.name}\0{board_sha}\0".encode("utf-8"))
        for relative, full, info in scan.inputs:
            file_digest = self._cached_hash(full, info)
            if file_digest is None:
                return _Fingerprint(
                    board_sha, None,
                    f"unreadable input file {relative}: verdict cannot be shown fresh",
                )
            digest.update(f"{relative}\0{file_digest}\0".encode("utf-8"))
        return _Fingerprint(board_sha, digest.hexdigest(), None)

    # -- revision tokens (bounded) --------------------------------------------
    def remember_token(self, token: str, boards: list[str]) -> None:
        with self._lock:
            self._tokens[token] = list(boards)
            self._tokens.move_to_end(token)
            while len(self._tokens) > _TOKEN_ENTRIES:
                self._tokens.popitem(last=False)

    def recall_token(self, token: str) -> list[str]:
        with self._lock:
            return list(self._tokens.get(token, []))

    def token_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    # -- writes --------------------------------------------------------------
    def begin_grading(self, pcb_path: str | Path) -> Snapshot:
        path = Path(pcb_path)
        with self._lock:
            seq = self._seq
        fingerprint = self._fingerprint(path, {})
        return Snapshot(fingerprint.board_sha, fingerprint.project_sha, seq)

    def record_verdict(self, pcb_path: str | Path, verdict: str, snapshot: Snapshot) -> None:
        with self._lock:
            self._verdicts[_key(pcb_path)] = _Verdict(
                verdict=verdict,
                sha=snapshot.sha,
                project_sha=snapshot.project_sha,
                recorded_at=self._clock(),
                mutation_seq=snapshot.mutation_seq,
            )

    def note_mutation(self, path: str | Path) -> None:
        """A mutating tool ran against ``path`` (a board, schematic or directory)."""
        p = Path(_key(path))
        directory = p if Path(path).is_dir() else p.parent
        with self._lock:
            self._seq += 1
            self._mutated_dirs[directory] = self._seq

    def note_unknown_mutation(self) -> None:
        """A mutating tool ran and could not say which board it touched: fail safe
        by making every verdict stale."""
        with self._lock:
            self._seq += 1
            self._unknown_seq = self._seq

    # -- reads ---------------------------------------------------------------
    def gate_fields(self, pcb_paths: list[str | Path]) -> dict[str, dict[str, Any]]:
        """The gate field for each board; boards sharing a directory share one walk."""
        scans: dict[Path, _Scan] = {}
        return {str(p): self._gate_field(Path(p), scans) for p in pcb_paths}

    def gate_field(self, pcb_path: str | Path) -> dict[str, Any]:
        return self._gate_field(Path(pcb_path), {})

    def _gate_field(self, path: Path, scans: dict[Path, _Scan]) -> dict[str, Any]:
        fingerprint = self._fingerprint(path, scans)
        board_dir = Path(_key(path)).parent
        with self._lock:
            record = self._verdicts.get(_key(path))
            now = self._clock()
            latest = self._unknown_seq
            for directory, seq in self._mutated_dirs.items():
                if _related(directory, board_dir):
                    latest = max(latest, seq)
        out: dict[str, Any] = {
            "verdict": NOT_RUN if record is None else record.verdict,
            "board_sha": fingerprint.board_sha,
            "project_sha": fingerprint.project_sha,
            "verdict_board_sha": None if record is None else record.sha,
            "age_s": None if record is None else round(max(0.0, now - record.recorded_at), 1),
            "stale": True,
            "note": _NOTE,
        }
        if fingerprint.note:
            out["fingerprint_note"] = fingerprint.note
        if record is not None:
            out["stale"] = (
                fingerprint.board_sha is None
                or fingerprint.board_sha != record.sha
                or fingerprint.project_sha is None
                or fingerprint.project_sha != record.project_sha
                or latest > record.mutation_seq
            )
        return out


# -- tool wrapping -----------------------------------------------------------
_BOARD_ARGS = ("pcb_path",)
_BOARD_LIST_ARGS = ("pcb_paths",)
_MUTATION_ARGS = ("pcb_path", "sch_path", "project_dir", "path")


def _str_paths(value: Any) -> list[str]:
    if isinstance(value, str) and value:
        return [value]
    if isinstance(value, (list, tuple)):
        return [v for v in value if isinstance(v, str) and v]
    return []


def attach_gate_field(fn, annotations: Any, state: GateState):
    """Wrap a toolset tool so board-state results carry ``gate``.

    The wrapper keeps the original signature, name and docstring (FastMCP builds
    the tool schema from them) and adds nothing to the arguments.
    """
    import functools
    import inspect

    read_only = bool(
        (annotations.get("readOnlyHint") if isinstance(annotations, dict)
         else getattr(annotations, "readOnlyHint", None))
    )
    signature = inspect.signature(fn)

    def _bind(args, kwargs) -> dict[str, Any]:
        try:
            return dict(signature.bind_partial(*args, **kwargs).arguments)
        except TypeError:
            return dict(kwargs)

    def _board_paths(arguments: dict, result: Any) -> list[str]:
        paths: list[str] = []
        for name in _BOARD_ARGS + _BOARD_LIST_ARGS:
            paths += [p for p in _str_paths(arguments.get(name)) if p.lower().endswith(".kicad_pcb")]
        if isinstance(result, dict):
            paths += [p for p in _str_paths(result.get("pcb_path")) if p.lower().endswith(".kicad_pcb")]
        token = arguments.get("revision_token")
        if isinstance(token, str):
            paths += state.recall_token(token)
        return list(dict.fromkeys(paths))

    def _finish(arguments: dict, result: Any) -> Any:
        boards = _board_paths(arguments, result)
        if isinstance(result, dict):
            token = result.get("revision_token")
            if isinstance(token, str) and boards:
                state.remember_token(token, boards)
        if not isinstance(result, dict) or not boards:
            return result
        out = dict(result)
        if len(boards) == 1:
            out["gate"] = state.gate_field(boards[0])
        else:
            out["gates"] = state.gate_fields(boards)
        return out

    def _mark_mutation(arguments: dict, result: Any) -> None:
        if read_only:
            return
        touched = _board_paths(arguments, result)
        for name in _MUTATION_ARGS:
            touched += _str_paths(arguments.get(name))
        for path in dict.fromkeys(touched):
            state.note_mutation(path)
        if not touched and "revision_token" in signature.parameters:
            # A token-only mutating call whose board is unknown (the token was not
            # issued by a tool that named a board, e.g. a live-GUI proposal): fail safe.
            state.note_unknown_mutation()

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            arguments = _bind(args, kwargs)
            result = None
            try:
                result = await fn(*args, **kwargs)
            finally:
                _mark_mutation(arguments, result)
            return _finish(arguments, result)

    else:

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            arguments = _bind(args, kwargs)
            result = None
            try:
                result = fn(*args, **kwargs)
            finally:
                _mark_mutation(arguments, result)
            return _finish(arguments, result)

    return wrapper
