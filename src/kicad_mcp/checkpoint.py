"""The checkpoint mechanism, once (build ticket 22; docs/ARCHITECTURE.md decisions 4 and 12).

Propose writes nothing and returns a ``revision_token``; confirm applies exactly that proposal
onto a NEW revision beside the source, verified before it is moved into place. This module holds
what the checkpointed pairs (``input_prep``, ``schematic_import``, ``board_rules``,
``board_outline``, ``silk_minimums``) used to copy among themselves:

- **the revision token** (``Pending.issue``, ``token_project_hash``): ``<composite>-<id>``, the
  project's composite hash (``gate_state``'s gate-field hashing: the board plus every schematic,
  rules, project, library and lib-table file in its directory tree) and a per-proposal id, so
  two proposals on an unchanged project never collide. Confirm recomputes the composite hash and
  refuses on any difference; the drift guard (build ticket 40) can check the same hash from the
  token alone.
- **the pending state** (``Pending``): bounded (``MAX_ENTRIES``) and TTL-limited
  (``TTL_SECONDS``). A token is spent by ``spend`` after a success, or by ``claim`` when it finds
  drift; every other refusal keeps it.
- **the new revision** (``output_dir_problem``, ``copy_project``, ``build_revision``): the
  project is copied into a temporary sibling directory, the tool's write step changes the copy,
  its verify step reloads and compares, and only then is the directory moved into place with
  ``write_layer.commit_directory``. A failure leaves nothing behind.
- **the self-check result** (``NotVerified``): a verify step that finds the reload is not the
  intended change raises it, and the tool returns ``ok: false`` with ``code: write_not_verified``
  and the mismatch list (expected, actual, file). The source is never touched either way.

The hashing deliberately builds a fresh ``GateState`` per call: its (path, size, mtime) cache
would let a same-size edit that restores the mtime pass, and a confirm copies the whole project
anyway, so reading it once more costs the same order.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_mcp import gate_state, write_layer

if not hasattr(gate_state.GateState, "_fingerprint"):  # the gate field's hashing is reused, not copied
    raise ImportError("checkpoint reuses kicad_mcp.gate_state.GateState._fingerprint, which no longer exists")

TTL_SECONDS = 60 * 60
MAX_ENTRIES = 32
WRITE_NOT_VERIFIED = "write_not_verified"
UNKNOWN_TOKEN = "unknown or expired revision_token"
PATH_MISMATCH = "pcb_path does not match the path this proposal was made for"

_TOKEN = re.compile(r"([0-9a-f]{64})-([0-9a-f]{16})")
_MAX_LISTED = 20


class Refused(Exception):
    """No proposal can be made; the message is the reason."""


def _norm(path: str | Path) -> str:
    """Comparable spelling of a path: absolute, resolved, case-normalised on Windows."""
    return os.path.normcase(str(Path(path).resolve()))


def _digest(path: Path) -> str | None:
    return gate_state._hash_file(path)


def composite_hash(pcb_path: str | Path) -> tuple[str | None, str | None]:
    """``(hash, note)``: the project's composite hash as the gate field computes it, or ``None``
    and the reason when the project cannot be fingerprinted (missing board, unreadable input,
    over the size or file-count cap)."""
    fingerprint = gate_state.GateState()._fingerprint(Path(pcb_path), {})
    return fingerprint.project_sha, fingerprint.note


def token_project_hash(token: str) -> str | None:
    """The composite project hash a revision token carries, or ``None`` for a malformed token."""
    match = _TOKEN.fullmatch(token or "")
    return match.group(1) if match else None


@dataclass
class _Entry:
    src: str
    project_hash: str
    extra: dict[str, str | None]
    payload: Any
    expiry: float = field(default=0.0)


class Pending:
    """Bounded, TTL-limited proposals of one checkpointed pair.

    ``clock`` is read on every call, so a module can pass ``lambda: _clock()`` and tests can
    replace the module's ``_clock``."""

    def __init__(self, clock: Callable[[], float], ttl: float = TTL_SECONDS, max_entries: int = MAX_ENTRIES) -> None:
        self._clock = clock
        self._ttl = ttl
        self._max = max_entries
        self._entries: dict[str, _Entry] = {}

    def _evict(self) -> None:
        now = self._clock()
        for token in [t for t, e in self._entries.items() if e.expiry <= now]:
            del self._entries[token]
        overflow = len(self._entries) - self._max
        if overflow > 0:
            for token, _ in sorted(self._entries.items(), key=lambda kv: kv[1].expiry)[:overflow]:
                del self._entries[token]

    def issue(self, pcb_path: str | Path, payload: Any, *, extra_files: Iterable[str | Path] = ()) -> str:
        """Record a proposal for ``pcb_path`` and return its token. ``extra_files`` are files the
        proposal depends on that may lie outside the board's directory tree (each hashed on its
        own, a missing one as absent). Raises ``Refused`` when the project cannot be hashed."""
        project_hash, note = composite_hash(pcb_path)
        if project_hash is None:
            raise Refused(f"no proposal without a project hash: {note}")
        extra = {_norm(p): _digest(Path(p)) for p in extra_files}
        self._evict()
        token = f"{project_hash}-{secrets.token_hex(8)}"
        self._entries[token] = _Entry(_norm(pcb_path), project_hash, extra, payload, self._clock() + self._ttl)
        return token

    def claim(self, token: str, pcb_path: str | Path, *, drift_reason: str) -> tuple[Any, dict | None]:
        """``(payload, None)`` when ``token`` is pending for ``pcb_path`` and nothing the proposal
        hashed has changed; otherwise ``(None, refusal)``. An unknown or expired token and a
        different path keep nothing and keep the token respectively; drift spends the token and
        is refused with ``drift_reason``. A successful claim does not spend the token."""
        self._evict()
        entry = self._entries.get(token)
        if entry is None:
            return None, {"status": "error", "reason": UNKNOWN_TOKEN}
        if _norm(pcb_path) != entry.src:
            return None, {"status": "error", "reason": PATH_MISMATCH}
        current, _ = composite_hash(pcb_path)
        if current != entry.project_hash or any(_digest(Path(p)) != d for p, d in entry.extra.items()):
            del self._entries[token]
            return None, {"status": "error", "reason": drift_reason}
        return entry.payload, None

    def spend(self, token: str) -> None:
        """The proposal was applied: its token is used up."""
        self._entries.pop(token, None)


# --- the new revision ---------------------------------------------------------------------------

KEPT_NOTE = "no revision was left behind and the revision_token is still valid"
_SKIP_DIRS = {".git", "__pycache__", ".venv", "node_modules"}


class Failed(Exception):
    """A write or verify step refuses by name: ``status`` and ``reason`` plus extra result fields
    (an ``extra["note"]`` replaces the default note)."""

    def __init__(self, status: str, reason: str, **extra: Any) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.extra = extra


class NotVerified(Exception):
    """The reloaded revision is not the intended change (the self-check fired). ``mismatches``
    are ``mismatch(...)`` entries; ``extra`` are further result fields."""

    def __init__(self, reason: str, mismatches: Iterable[dict] = (), **extra: Any) -> None:
        super().__init__(reason)
        self.reason = reason
        self.mismatches = list(mismatches)
        self.extra = extra


def mismatch(file: str, item: str, expected: Any, actual: Any) -> dict:
    """One self-check finding: where (``file``, ``item``), what was intended and what reloaded."""
    return {"file": file, "item": item, "expected": expected, "actual": actual}


def reload_mismatches(file: str, differences: dict) -> list[dict]:
    """``_self_check.reload_differences`` as mismatch entries: each node missing after the reload,
    then each node present that was not intended (tolerated differences are not mismatches)."""
    return [mismatch(file, label, "present after reload", "missing") for label in differences["missing"]] + [
        mismatch(file, label, "absent", "present but not intended") for label in differences["unexpected"]
    ]


def output_dir_problem(src: Path, out_dir: Path, *, what: str) -> str | None:
    """Why ``out_dir`` cannot take the new revision of the board ``src``, or ``None``: it must
    be neither the project directory nor inside it, and if it exists it must be an empty
    directory. ``what`` names the result in the first reason (``"the import"``)."""
    source = Path(src).resolve().parent
    target = Path(out_dir).resolve()
    if target == source:
        return f"output_dir is the original project directory: {what} must be a new revision"
    if target.is_relative_to(source):
        return f"output_dir {out_dir} is inside the source project tree: the source must gain nothing"
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        return f"output_dir {out_dir} exists and is not empty"
    return None


def copy_project(
    src_pcb: Path,
    dest: Path,
    *,
    outputs: Iterable[str] = (),
    skip_suffixes: Iterable[str] = (),
    max_bytes: int | None = None,
) -> Path:
    """Copy the board's project directory to ``dest`` (which must not exist) and return the
    copied board. Left out: VCS, cache and virtualenv directories, ``*-backups``, the other
    boards in the project directory, the top-level ``outputs`` (earlier tool revisions kept
    inside the project) and any file ending in one of ``skip_suffixes``. A project larger than
    ``max_bytes`` is refused with ``ValueError`` before anything is copied."""
    from kicad_mcp import verify_board

    src_pcb = Path(src_pcb)
    source = src_pcb.parent
    if max_bytes is not None and verify_board._directory_size(source) > max_bytes:
        raise ValueError(f"project directory {source} is too large to copy")
    outputs = set(outputs)
    suffixes = tuple(skip_suffixes)

    def _ignore(directory: str, names: list[str]) -> list[str]:
        skipped = [n for n in names if n in _SKIP_DIRS or n.endswith("-backups") or (suffixes and n.endswith(suffixes))]
        if Path(directory) == source:
            skipped += [n for n in names if n in outputs]
            skipped += [n for n in names if n.endswith(".kicad_pcb") and n != src_pcb.name]
        return skipped

    shutil.copytree(source, dest, ignore=_ignore)
    return Path(dest) / src_pcb.name


def build_revision(
    out_dir: Path,
    *,
    write: Callable[[Path], Any],
    verify: Callable[[Path, Any], dict],
    success: str,
    before_verified: str,
    note: str = KEPT_NOTE,
) -> dict:
    """Build a new revision in a temporary sibling of ``out_dir``, verify it, then move it into
    place; the source project is only ever read.

    ``write(tmp)`` copies the project into ``tmp`` (``copy_project``), changes the copy and saves
    it through the write layer; it returns whatever ``verify`` needs. ``verify(tmp, state)``
    reloads the saved files and compares them with the intended change; it returns the result
    fields of a success, or raises ``NotVerified`` (the result is ``ok: false``, ``code:
    write_not_verified`` with the mismatches) or ``Failed``. Only after it returns is the
    directory moved onto ``out_dir`` (an empty ``out_dir`` the caller made is replaced).

    Returns ``{"status": success, **fields}``, or a failure carrying ``note``: an exception in
    ``write`` is ``incomplete`` with ``before_verified: <error>``; a failed move is
    ``incomplete``. Every failure, a ``BaseException`` included, removes the temporary
    directory and the parent directories this call created."""
    out_dir = Path(out_dir)
    missing: list[Path] = []
    probe = out_dir.parent
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    tmp = out_dir.parent / f".{out_dir.name}.tmp-{secrets.token_hex(4)}"

    def fail(status: str, reason: str, **extra: Any) -> dict:
        shutil.rmtree(tmp, ignore_errors=True)
        return {"status": status, "reason": reason, "note": note, **extra}

    committed = False
    try:
        try:
            out_dir.parent.mkdir(parents=True, exist_ok=True)
            state = write(tmp)
        except Failed as exc:
            return fail(exc.status, exc.reason, **exc.extra)
        except Exception as exc:  # noqa: BLE001  (any write error: nothing may be left behind)
            return fail("incomplete", f"{before_verified}: {exc}")
        try:
            fields = verify(tmp, state)
        except NotVerified as exc:
            listed = {"count": len(exc.mismatches), "items": exc.mismatches[:_MAX_LISTED]}
            return {
                "status": "error",
                "ok": False,
                "code": WRITE_NOT_VERIFIED,
                **fail("error", exc.reason, mismatches=listed, **exc.extra),
            }
        except Failed as exc:
            return fail(exc.status, exc.reason, **exc.extra)
        try:
            if out_dir.exists():
                out_dir.rmdir()  # an empty directory the caller pre-created
            write_layer.commit_directory(tmp, out_dir)
        except OSError as exc:
            return fail("incomplete", f"could not move the verified revision into place: {exc}")
        committed = True
        return {"status": success, **fields}
    finally:
        if not committed:
            shutil.rmtree(tmp, ignore_errors=True)
            for directory in missing:  # deepest first
                try:
                    directory.rmdir()
                except OSError:
                    break
