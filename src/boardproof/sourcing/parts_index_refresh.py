"""Explicit refresh of the local parts index (issue 200, decision ticket 53; reader: ``parts_index``).

The user asks for it, never automatic. It downloads the jlcparts cache (the community project
yaqwsx/jlcparts; the data is not shipped with Boardproof and no claim about its licence is made here) into a
local cache directory, assembles and extracts it, checks that the result opens read-only with the schema the
reader needs, and only then replaces the old index in one ``os.replace``. Any failure leaves the old index
exactly as it was and says so.

Where the files live (verified 2026-11-17 from the project's update workflow
``.github/workflows/update_components.yaml`` on its master branch, read through its raw file, not from a
download): the site is the project's GitHub Pages, ``https://yaqwsx.github.io/jlcparts/data/``; the workflow
creates the archive with ``zip -s 50m cache.zip cache.sqlite3`` and its own download step fetches
``cache.zip``, then ``cache.z01``, ``cache.z02`` ... until one is missing, and extracts with ``7z x``. So the
parts are ``cache.z01 .. cache.zNN`` followed by ``cache.zip``, the last segment (it holds the central
directory). Verified live on 2026-11-17 with HEAD requests only (no data downloaded): the site answers HEAD with
Content-Length and no redirect; the parts are cache.z01 .. cache.z06 of 52,428,800 bytes each and a last segment
cache.zip of 1,904,211 bytes, 316,477,011 bytes in all. NOT verified: that the real archive's local header
offsets count the 4-byte split marker (both readings are tried; the local header signature and name and the
CRC decide), the content of the real archive (never downloaded) and the size of the extracted file.

Safety: https only, host allow-list ``ALLOWED_HOSTS`` re-checked on every redirect and on the final URL, no
credentials or port, the path must stay under ``/jlcparts/data/``; per-socket timeout and a deadline checked between reads of at most 64 KiB (not a hard total deadline: a server that drips bytes can stretch one read); a
cap on the total download (read as max+1) and on the extracted size; the archive is read by this module's own
reader of the split-zip layout (Python's ``zipfile`` cannot read a split archive): exactly one member named
``cache.sqlite3`` (any other name, a path, ``..`` or a backslash is refused, never extracted), stored or
deflate only, no encryption, no zip64, sizes taken from the central directory and enforced while inflating
(a zip bomb stops at the declared size), CRC-32 checked. The target directory is never inside a git
repository or a KiCad project folder and never a network path.
"""

from __future__ import annotations

import http.client
import os
import shutil
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Any

from boardproof.sourcing import parts_index

BASE_URL = "https://yaqwsx.github.io/jlcparts/data/"
ALLOWED_HOSTS = frozenset({"yaqwsx.github.io"})
ALLOWED_PATH_PREFIX = "/jlcparts/data/"
ARCHIVE_NAME = "cache.zip"
MEMBER_NAME = "cache.sqlite3"
TARGET_NAME = "cache.sqlite3"
TEMP_PREFIX = ".refresh-"
STALE_TEMP_SECONDS = 24 * 3600

MAX_SEGMENTS = 40  # 39 split parts of 50 MB plus the last segment
MAX_DOWNLOAD_BYTES = 1_500_000_000
MAX_MEMBER_BYTES = 4_000_000_000
MAX_ENTRIES = 16
MAX_CD_BYTES = 1 << 20  # one expected member: a few hundred bytes; 16 entries with long names stay far below this
MAX_LENGTH_DIGITS = 12  # a Content-Length of more digits is over 100 GB; refused before int() (which also caps at 4300)
READ_BLOCK = 64 * 1024  # the deadline is checked between reads of at most this size
TEMP_MARKER = ".boardproof-refresh-temp"
TIMEOUT_S = 60
DEADLINE_S = 7200
CHUNK = 1 << 20
DOCUMENTED_SIZE_NOTE = (
    "The jlcparts cache is large: about 316 MB to download (the sizes the site announced by HEAD on 2026-11-17; "
    "they change with every build) and the extracted file is bigger. Check free disk space."
)

_LOCK = threading.Lock()
_clock = time.monotonic

_EOCD = struct.Struct("<4sHHHHIIH")
_CD = struct.Struct("<4s6H3I5H2I")
_LOCAL = struct.Struct("<4s5H3I2H")
_SPLIT_MARKER = b"PK\x07\x08"
_NO_ZIP64 = (0xFFFFFFFF, 0xFFFF)


class RefreshRefusal(Exception):
    """A refusal or failure with a stable code; becomes ``{"ok": False, ...}`` naming the kept old index."""

    def __init__(self, code: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra


class _NotFound(Exception):
    pass


# ---------------------------------------------------------------- where to write

def default_target_dir() -> Path:
    """The platform's per-user data directory, ``boardproof/parts-index`` below it (never in a project)."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    elif os.uname().sysname == "Darwin":  # type: ignore[attr-defined]
        base = str(Path.home() / "Library" / "Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "boardproof" / "parts-index"


_KICAD_SUFFIXES = (".kicad_pro", ".kicad_sch", ".kicad_pcb")


def _check_target(target_dir: str | None) -> Path:
    given = target_dir if target_dir not in (None, "") else str(default_target_dir())
    if "\x00" in given:
        raise RefreshRefusal("target_refused", "The target directory holds a NUL character.")
    if parts_index._is_network_path(given):
        raise RefreshRefusal(
            "target_refused",
            "A network (UNC) target directory is refused: the index is written to a local disk only.",
        )
    path = Path(os.path.expanduser(given))
    if not path.is_absolute():
        raise RefreshRefusal(
            "target_refused",
            f"The target directory {given!r} is not an absolute path; a relative one would depend on the "
            "server's working directory.",
        )
    resolved = path.resolve()
    if resolved.exists() and not resolved.is_dir():
        raise RefreshRefusal("target_refused", f"{str(resolved)!r} exists and is not a directory.")
    for folder in (resolved, *resolved.parents):
        if (folder / ".git").exists():
            raise RefreshRefusal(
                "target_refused",
                f"{str(resolved)!r} lies inside a git repository ({str(folder)!r}); the index is a download of hundreds of MB "
                "download and never belongs in a repository. Choose a directory outside it.",
            )
        try:
            hit = next((e.name for e in folder.iterdir() if e.name.lower().endswith(_KICAD_SUFFIXES)), None)
        except OSError:
            hit = None
        if hit is not None:
            raise RefreshRefusal(
                "target_refused",
                f"{str(resolved)!r} lies inside a KiCad project folder ({str(folder)!r} holds {hit}); the index "
                "never goes into a project. Choose a directory outside it.",
            )
    return resolved


# ---------------------------------------------------------------- network

def check_url(url: str) -> str | None:
    """None when ``url`` may be fetched, else why not."""
    if not url or any(c.isspace() or ord(c) < 32 for c in url):
        return "the URL holds whitespace or control characters"
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return "the URL does not parse"
    if parts.scheme != "https":
        return "only https URLs are fetched"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "a URL with credentials is refused"
    if port is not None:
        return "a URL with a port is refused"
    if (parts.hostname or "") not in ALLOWED_HOSTS:
        return f"host {parts.hostname!r} is not allow-listed ({', '.join(sorted(ALLOWED_HOSTS))})"
    path = urllib.parse.unquote(parts.path)  # %2e%2e and ..%2f are dot segments too
    if not path.startswith(ALLOWED_PATH_PREFIX) or "\\" in path or any(seg in (".", "..") for seg in path.split("/")):
        return f"the path is not under {ALLOWED_PATH_PREFIX} or holds dot segments"
    return None


class _CheckedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = check_url(newurl)
        if problem:
            raise urllib.error.URLError(f"redirect refused: {problem}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def build_opener() -> Any:
    return urllib.request.build_opener(_CheckedRedirect)


def segment_urls(count: int) -> list[str]:
    """URLs of ``count`` segments in reading order: ``cache.z01`` .. then ``cache.zip`` (the last one)."""
    names = [f"cache.z{n:02d}" for n in range(1, count)] + [ARCHIVE_NAME]
    return [BASE_URL + name for name in names]


def _open(opener: Any, url: str, method: str) -> Any:
    problem = check_url(url)
    if problem:
        raise RefreshRefusal("url_refused", f"Refused to fetch {url}: {problem}.", url=url)
    request = urllib.request.Request(url, method=method, headers={"User-Agent": "boardproof-parts-index-refresh"})
    try:
        response = opener.open(request, timeout=TIMEOUT_S)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise _NotFound(url) from None
        raise RefreshRefusal("download_failed", f"{url} answered HTTP {exc.code}.", url=url) from None
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException) as exc:
        raise RefreshRefusal("download_failed", f"Fetching {url} failed: {type(exc).__name__}: {exc}", url=url) from None
    geturl = getattr(response, "geturl", None)
    if callable(geturl):
        problem = check_url(geturl())
        if problem:
            try:
                response.close()
            finally:
                raise RefreshRefusal("url_refused", f"{url} ended at a URL that is refused: {problem}.", url=url)
    return response


def _declared_length(response: Any, url: str) -> int | None:
    headers = getattr(response, "headers", None)
    raw = headers.get("Content-Length") if headers is not None else None
    if raw is None:
        return None
    text = str(raw).strip()
    if not text.isascii() or not text.isdigit() or len(text) > MAX_LENGTH_DIGITS:
        shown = raw if len(str(raw)) <= 40 else str(raw)[:40] + "..."
        raise RefreshRefusal("download_failed", f"{url} sent an unreadable or implausible Content-Length {shown!r}.", url=url)
    return int(text)


def probe_segments(opener: Any) -> list[dict]:
    """HEAD every segment: the list of ``{url, size_bytes}`` in reading order. ``cache.zip`` must exist."""
    found: list[dict] = []
    for number in range(1, MAX_SEGMENTS):
        url = f"{BASE_URL}cache.z{number:02d}"
        try:
            response = _open(opener, url, "HEAD")
        except _NotFound:
            break
        with response:
            found.append({"url": url, "size_bytes": _declared_length(response, url)})
    else:
        raise RefreshRefusal("too_many_segments", f"More than {MAX_SEGMENTS - 1} split parts; refused.")
    url = BASE_URL + ARCHIVE_NAME
    try:
        response = _open(opener, url, "HEAD")
    except _NotFound:
        raise RefreshRefusal("archive_missing", f"{url} does not exist (HTTP 404).", url=url) from None
    with response:
        found.append({"url": url, "size_bytes": _declared_length(response, url)})
    return found


def _download_segment(opener: Any, url: str, sink: Any, budget: int, deadline: float) -> int:
    try:
        response = _open(opener, url, "GET")
    except _NotFound:
        raise
    got = 0
    try:
        with response:
            declared = _declared_length(response, url)
            if declared is not None and declared > budget:
                raise RefreshRefusal("download_too_large", f"{url} announces {declared} bytes; at most {budget} remain of the "
                                     f"{MAX_DOWNLOAD_BYTES} byte cap.", url=url)
            while True:
                if _clock() > deadline:
                    raise RefreshRefusal("download_timeout", f"The download passed its {DEADLINE_S} second limit during {url}.", url=url)
                block = response.read(min(READ_BLOCK, budget - got + 1))
                if not block:
                    break
                got += len(block)
                if got > budget:
                    raise RefreshRefusal("download_too_large", f"{url} passed the {MAX_DOWNLOAD_BYTES} byte download cap.", url=url)
                sink(block)
    except RefreshRefusal:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise RefreshRefusal("truncated_download", f"Reading {url} broke off after {got} bytes: {type(exc).__name__}: {exc}", url=url) from None
    if declared is not None and got != declared:
        raise RefreshRefusal("truncated_download", f"{url} announced {declared} bytes and delivered {got}.", url=url)
    if got == 0:
        raise RefreshRefusal("truncated_download", f"{url} delivered no bytes.", url=url)
    return got


def _download_all(opener: Any, archive: Path) -> list[int]:
    """Download the segments into one file, in order; returns each segment's size."""
    deadline = _clock() + DEADLINE_S
    sizes: list[int] = []
    with archive.open("wb") as out:
        for number in range(1, MAX_SEGMENTS):
            url = f"{BASE_URL}cache.z{number:02d}"
            try:
                sizes.append(_download_segment(opener, url, out.write, MAX_DOWNLOAD_BYTES - sum(sizes), deadline))
            except _NotFound:
                break
        else:
            raise RefreshRefusal("too_many_segments", f"More than {MAX_SEGMENTS - 1} split parts; refused.")
        url = BASE_URL + ARCHIVE_NAME
        try:
            sizes.append(_download_segment(opener, url, out.write, MAX_DOWNLOAD_BYTES - sum(sizes), deadline))
        except _NotFound:
            raise RefreshRefusal("archive_missing", f"{url} does not exist (HTTP 404).", url=url) from None
    return sizes


# ---------------------------------------------------------------- the split zip

def _unsafe_name(name: str) -> bool:
    return (not name or "\\" in name or "\x00" in name or name.startswith("/") or ":" in name
            or any(part in ("", ".", "..") for part in name.rstrip("/").split("/")))


def _invalid(message: str) -> RefreshRefusal:
    return RefreshRefusal("archive_invalid", f"The downloaded archive is not a readable zip: {message}")


def extract_member(archive: Path, sizes: list[int], out_path: Path) -> int:
    """Write the one expected member of the (split) zip in ``archive`` to ``out_path``; returns its size.

    ``archive`` is the segments concatenated in order, ``sizes`` their lengths. Offsets in a split archive
    count from the start of the disk they name, so they are turned into positions in the concatenation.
    """
    total = sum(sizes)
    prefix = [sum(sizes[:i]) for i in range(len(sizes) + 1)]
    with archive.open("rb") as fh:
        tail_len = min(total, 65_557)  # the end record is 22 bytes plus a comment of at most 65535: exactly this window
        fh.seek(total - tail_len)
        tail = fh.read(tail_len)
        at = tail.rfind(b"PK\x05\x06")
        if at < 0 or len(tail) - at < _EOCD.size:
            raise _invalid("no end-of-central-directory record (a segment is missing or cut short)")
        _, disk, cd_disk, _, n_total, cd_size, cd_off, _ = _EOCD.unpack_from(tail, at)
        if n_total == 0xFFFF or cd_size == 0xFFFFFFFF or cd_off == 0xFFFFFFFF:
            raise RefreshRefusal("archive_unsupported", "A zip64 archive is not supported.")
        if disk != len(sizes) - 1:
            raise RefreshRefusal(
                "segment_count_mismatch",
                f"The archive says it has {disk + 1} segments and {len(sizes)} were downloaded: a part is missing or extra.",
            )
        if cd_disk > disk:
            raise _invalid("the central directory starts on a disk after the last")
        if n_total > MAX_ENTRIES:
            raise RefreshRefusal("too_many_members", f"The archive lists {n_total} members; at most {MAX_ENTRIES} are accepted.")
        if cd_size > MAX_CD_BYTES:
            raise _invalid(f"the central directory claims {cd_size} bytes, which is too large (at most {MAX_CD_BYTES} are read)")
        cd_start = prefix[cd_disk] + cd_off
        # The bound and the length check below overlap on purpose: the bound keeps the read inside the file,
        # the length check catches a file that changed; either alone gives the same refusal.
        if cd_start + cd_size > total:
            raise _invalid("the central directory lies outside the file")
        fh.seek(cd_start)
        directory = fh.read(cd_size)
        if len(directory) != cd_size:
            raise _invalid("the central directory is cut short")
        marker = False
        fh.seek(0)
        marker = fh.read(4) == _SPLIT_MARKER
        entries = []
        pos = 0
        for _ in range(n_total):
            if directory[pos:pos + 4] != b"PK\x01\x02" or pos + _CD.size > len(directory):
                raise _invalid("a central directory entry is damaged")
            (_, _, _, flags, method, _, _, crc, csize, usize, nlen, xlen, clen, disk_start, _, _, loc_off) = _CD.unpack_from(directory, pos)
            raw = directory[pos + _CD.size:pos + _CD.size + nlen]
            if len(raw) != nlen:
                raise _invalid("a member name is cut short")
            pos += _CD.size + nlen + xlen + clen
            try:
                name = raw.decode("utf-8" if flags & 0x800 else "cp437")
            except UnicodeDecodeError:
                raise RefreshRefusal("unsafe_member", "A member name is not readable text; refused.") from None
            entries.append((name, flags, method, crc, csize, usize, disk_start, loc_off))
        for name, *_rest in entries:
            if _unsafe_name(name):
                raise RefreshRefusal("unsafe_member", f"The archive holds a member named {name!r} (a path, '..', a backslash or similar); refused, nothing extracted.")
        for name, *_rest in entries:
            if name != MEMBER_NAME:
                raise RefreshRefusal("unexpected_member", f"The archive holds {name!r}; only {MEMBER_NAME!r} is expected; refused, nothing extracted.")
        if len(entries) != 1:
            raise RefreshRefusal("unexpected_member", f"The archive must hold exactly one member {MEMBER_NAME!r}; it lists {len(entries)}.")
        name, flags, method, crc, csize, usize, disk_start, loc_off = entries[0]
        if flags & 0x1:
            raise RefreshRefusal("archive_unsupported", "An encrypted member is not supported.")
        if method not in (0, 8):
            raise RefreshRefusal("archive_unsupported", f"Compression method {method} is not supported (stored and deflate only).")
        if csize == 0xFFFFFFFF or usize == 0xFFFFFFFF:
            raise RefreshRefusal("archive_unsupported", "A zip64 member is not supported.")
        if usize > MAX_MEMBER_BYTES:
            raise RefreshRefusal("member_too_large", f"The member declares {usize} bytes; at most {MAX_MEMBER_BYTES} are accepted.")
        if method == 0 and csize != usize:
            raise _invalid("a stored member whose sizes differ")
        if disk_start >= len(sizes):
            raise _invalid("a member starts on a disk that does not exist")
        base = prefix[disk_start] + loc_off
        data_start = None
        for candidate in (base, base + 4) if marker and disk_start == 0 else (base,):
            fh.seek(candidate)
            head = fh.read(_LOCAL.size)
            if len(head) != _LOCAL.size or head[:4] != b"PK\x03\x04":
                continue
            lnlen, lxlen = _LOCAL.unpack(head)[-2:]
            if fh.read(lnlen) != MEMBER_NAME.encode("ascii"):
                continue
            data_start = candidate + _LOCAL.size + lnlen + lxlen
            break
        # The bound overlaps with _inflate's short-read refusal (same code); kept so no read starts past the end.
        if data_start is None or data_start + csize > total:
            raise _invalid("the member's local header or data is not where the directory says")
        fh.seek(data_start)
        return _inflate(fh, out_path, method, csize, usize, crc)


def _inflate(fh: Any, out_path: Path, method: int, csize: int, usize: int, crc: int) -> int:
    written = 0
    check = 0
    inflater = zlib.decompressobj(-15) if method == 8 else None

    def emit(sink: Any, data: bytes) -> None:
        nonlocal written, check
        if not data:
            return
        written += len(data)
        if written > usize:
            raise RefreshRefusal("member_size_mismatch", f"The member inflates past its declared {usize} bytes; refused.")
        check = zlib.crc32(data, check)
        sink(data)

    remaining = csize
    with out_path.open("wb") as out_fh:
        try:
            while remaining > 0:
                block = fh.read(min(CHUNK, remaining))
                if not block:
                    raise _invalid("the member's data is cut short")
                remaining -= len(block)
                if inflater is None:
                    emit(out_fh.write, block)
                    continue
                pending = block
                while pending:
                    emit(out_fh.write, inflater.decompress(pending, CHUNK))
                    pending = inflater.unconsumed_tail
            if inflater is not None:
                emit(out_fh.write, inflater.flush())
                if not inflater.eof:
                    raise _invalid("the deflate stream ends early")
        except zlib.error as exc:
            raise _invalid(f"the deflate stream is damaged ({exc})") from None
    if written != usize:
        raise RefreshRefusal("member_size_mismatch", f"The member has {written} bytes, the directory says {usize}; refused.")
    if check != crc:
        raise RefreshRefusal("crc_mismatch", "The member's CRC-32 does not match the directory; the download is damaged.")
    return written


# ---------------------------------------------------------------- the index

def _validate(path: Path) -> dict:
    """The extracted file must open as the reader needs it and hold parts; returns its index info."""
    import sqlite3

    try:
        conn, _columns = parts_index.open_index(str(path))
    except parts_index.IndexRefusal as exc:
        raise RefreshRefusal(exc.code, f"The downloaded file is not a usable parts index: {exc.message}", **exc.extra) from None
    try:
        try:
            verdict = conn.execute("PRAGMA quick_check(1)").fetchone()
            if verdict is None or verdict[0] != "ok":
                raise RefreshRefusal("index_unreadable", f"The downloaded file fails SQLite's integrity check: {verdict[0] if verdict else 'no answer'}.")
            info = parts_index.index_info(conn, TARGET_NAME)
        except sqlite3.Error as exc:
            raise RefreshRefusal("index_unreadable", f"The downloaded file could not be read: {exc}") from None
    finally:
        conn.close()
    if not info["part_count"]:
        raise RefreshRefusal("index_empty", "The downloaded index holds no parts; the old index is kept.")
    return info


def _existing_info(target: Path) -> dict | None:
    if not target.is_file():
        return None
    import sqlite3

    try:
        conn, _ = parts_index.open_index(str(target))
    except (parts_index.IndexRefusal, sqlite3.Error) as exc:
        return {"file": str(target), "readable": False, "note": f"The existing file is not a readable index: {exc}"}
    try:
        info = parts_index.index_info(conn, TARGET_NAME)
    except sqlite3.Error as exc:
        return {"file": str(target), "readable": False, "note": f"The existing file could not be read: {exc}"}
    finally:
        conn.close()
    return {"file": str(target), "readable": True, **info}


def _sweep_stale_temps(target_dir: Path) -> None:
    now = time.time()
    try:
        entries = list(target_dir.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            # is_symlink: rmtree refuses a link; the marker proves we made the folder (a user's own
            # .refresh-* folder is never touched).
            if entry.name.startswith(TEMP_PREFIX) and entry.is_dir() and not entry.is_symlink() \
                    and (entry / TEMP_MARKER).is_file() and now - entry.stat().st_mtime > STALE_TEMP_SECONDS:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def _licence_note() -> str:
    return (
        "You download this data to your own disk; nothing ships with Boardproof, and this tool makes no claim "
        "about the terms the data is published under: read the jlcparts project's terms yourself."
    )


# ---------------------------------------------------------------- the two entry points

def plan(target_dir: str | None, *, opener: Any = None) -> dict:
    """Dry run: where it would write and what it would download. Writes nothing, not even the directory."""
    target = _check_target(target_dir)
    out_path = target / TARGET_NAME
    result: dict[str, Any] = {
        "ok": True, "status": "planned", "dry_run": True, "written": False,
        "target_dir": str(target), "target_path": str(out_path),
        "existing_index": _existing_info(out_path),
        "limits": {
            "max_download_bytes": MAX_DOWNLOAD_BYTES, "max_extracted_bytes": MAX_MEMBER_BYTES,
            "timeout_seconds_per_socket_operation": TIMEOUT_S, "deadline_seconds": DEADLINE_S,
        },
        "notes": [DOCUMENTED_SIZE_NOTE, _licence_note()],
        "next_step": "Call again with dry_run=false to download, check and replace the index.",
    }
    try:
        segments = probe_segments(opener if opener is not None else build_opener())
    except RefreshRefusal as exc:
        result["download"] = {"urls": segment_urls(1), "sizes_known": False,
                              "probe_failed": {"reason": exc.code, "message": exc.message},
                              "url_list_note": "The number of split parts could not be probed; the list shows the final segment only."}
        result.update(ok=False, status="probe_failed", would_fail=True, reason=exc.code,
                      message=f"A real run would fail now: {exc.message}")
        result.pop("next_step", None)
        return result
    sizes = [s["size_bytes"] for s in segments]
    known = all(s is not None for s in sizes)
    result["download"] = {
        "urls": [s["url"] for s in segments], "sizes_bytes": sizes, "sizes_known": known,
        "total_bytes": sum(sizes) if known else None, "size_source": "HEAD Content-Length",
    }
    if known and sum(sizes) > MAX_DOWNLOAD_BYTES:
        result["ok"] = False
        result["status"] = "refused"
        result["reason"] = "download_too_large"
        result.pop("next_step", None)
        result["message"] = f"The announced download is {sum(sizes)} bytes, over the {MAX_DOWNLOAD_BYTES} byte cap."
    return result


def refresh(target_dir: str | None, *, opener: Any = None) -> dict:
    """Download, check and replace the index. All or nothing: on any failure the old index is untouched."""
    target = _check_target(target_dir)
    out_path = target / TARGET_NAME
    old_kept = str(out_path) if out_path.is_file() else None
    if not _LOCK.acquire(blocking=False):
        raise RefreshRefusal("refresh_in_progress", "Another refresh is running in this server; wait for it.", old_index_kept=old_kept)
    work: Path | None = None
    try:
        try:
            target.mkdir(parents=True, exist_ok=True)
            _sweep_stale_temps(target)
            work = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=target))
            (work / TEMP_MARKER).touch()
            archive = work / "download.part"
            sizes = _download_all(opener if opener is not None else build_opener(), archive)
            extracted = work / "extracted.sqlite3"
            member_bytes = extract_member(archive, sizes, extracted)
            archive.unlink()
            info = _validate(extracted)
            try:
                os.replace(extracted, out_path)
            except OSError as exc:
                raise RefreshRefusal("replace_failed", f"The new index could not be moved into place (is it open in another program?): {exc}") from None
        except RefreshRefusal as exc:
            exc.extra.setdefault("old_index_kept", old_kept)
            raise
        except OSError as exc:
            raise RefreshRefusal("refresh_failed", f"{type(exc).__name__}: {exc}", old_index_kept=old_kept) from None
    finally:
        _LOCK.release()
        if work is not None:
            shutil.rmtree(work, ignore_errors=True)
    return {
        "ok": True, "status": "refreshed", "dry_run": False, "written": True,
        "target_path": str(out_path), "replaced_previous_index": old_kept is not None,
        "downloaded_bytes": sum(sizes), "segments": len(sizes), "index_bytes": member_bytes,
        "index": info,
        "set_env": {parts_index.ENV_VAR: str(out_path)},
        "message": (f"The index is at {out_path} (index_date {info['index_date']}, {info['part_count']} parts). "
                    f"{parts_index.ENV_VAR} is not changed by this tool: set it to that path, or pass index_path to "
                    "list_part_filters and filter_parts."),
        "notes": [_licence_note()],
    }


def run(target_dir: str | None = None, dry_run: bool = True, *, opener: Any = None) -> dict:
    """The tool's body: a plan by default, the download only with ``dry_run=False``."""
    try:
        if dry_run:
            return plan(target_dir, opener=opener)
        return refresh(target_dir, opener=opener)
    except RefreshRefusal as exc:
        out = {"ok": False, "status": "refused" if exc.code in _REFUSED_BEFORE_DOWNLOAD else "failed",
               "reason": exc.code, "message": exc.message, **exc.extra}
        if exc.extra.get("old_index_kept"):
            out["message"] += f" The previous index at {exc.extra['old_index_kept']} was not changed."
        return out


_REFUSED_BEFORE_DOWNLOAD = frozenset({"target_refused", "url_refused", "refresh_in_progress"})
