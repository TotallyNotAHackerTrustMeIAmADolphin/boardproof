"""Publishability checker (publish-prep ticket 01).

    python scripts/check_publishable.py <dir> [--forbidden-file <path>] [--exclude <prefix>]...
                                        [--strict-archives [--allow-archive <prefix>]...]

Scans every file and every file or directory name under ``<dir>`` (only the top-level ``.git`` is
skipped; a nested ``.git`` directory is scanned) and prints every hit as
``file:line: [pattern] matched-span``: the matched span only, never the whole line. A hit in a
file or directory name, in a binary file, or on a symlink has line 0. Exit codes:

- 0: no hit;
- 1: at least one hit;
- 2: the checker could not do its job: ``<dir>`` is not a directory, a file under it cannot be
  read, or ``--forbidden-file`` was given but is missing, unreadable, empty (blank lines and ``#``
  comments do not count), not valid UTF-8 (a UTF-8 BOM is accepted; a BOM or NUL inside a line is
  not) or holds an invalid regex. A check that cannot run never passes by accident;
- 3: only with ``--strict-archives``: no hit, but an archive-like file (see below) outside the
  ``--allow-archive`` allow-list was not opened. A hit (1) outranks it.

Built-in patterns are generic: Windows, MSYS, macOS and Linux home paths, the dashed form of a
home path in a Claude project-directory name, e-mail addresses other than ``noreply`` forms, and
token shapes. The owner's identity patterns are not in the repository; they come from
``--forbidden-file`` (one case-insensitive regex per line, ``#`` starts a comment line).
``--exclude`` skips every entry whose path relative to ``<dir>`` equals the prefix or lies below
it (for example ``.scratch``).

Paths declared in ``<dir>/.gitmodules`` are git submodules: another project's content (publish-prep
ticket 03b). They are skipped as if ``--exclude``d, and each skip is printed as
``submodule skipped: <path>`` so it is never silent; ``--scan-submodules`` scans them too. Only
safe relative paths count (see ``submodule_paths``). ``.gitmodules`` itself is scanned.

Text files (UTF-8, or UTF-16/UTF-8 with a BOM) are scanned line by line. A file with a NUL byte in
its first 8 KiB is binary: it is scanned bytewise, as one block, against the same patterns. Stated
limit: compressed streams (a PDF's, a zip's) and UTF-16 without a BOM cannot be seen that way.
A file that looks like an archive (zip, jar, docx and the like by their PK magic, gzip, 7z, rar,
a bzip2, xz, zstd or tar file, or a PDF with Flate streams) is named on a line ``archive not opened: <file>`` and counted in the
summary: it is not a hit, but what is inside was not checked. In a binary file the e-mail shape
is stricter than in text (a local part of 3 or more characters and domain labels of 2 or more),
because compressed data produces chance matches of the short shape.
``--strict-archives`` makes that notice a failure for the public export: every unopened archive
not equal to or below an ``--allow-archive`` prefix is printed as ``archive not allowed: <file>``
and the run ends with exit 3 (with no hit). Without the flag an archive stays a notice and exit 0.
Symlinks and Windows junctions are reported (line 0) and not followed (on Python 3.11, which has
no ``os.path.isjunction``, a junction is recognised by its mount-point reparse tag instead). A directory that cannot
be listed ends the run with exit 2 and names the directory.
Stated limits, not detected: Unicode look-alikes of a forbidden name (a Cyrillic or full-width
letter, a zero-width character inside the name, accents) match no pattern; a name split across a
line break, or built by string concatenation or an escape, is not seen as one string; encoded
forms (base64, URL escapes) and the inside of archives are not decoded or opened.
Output is written as UTF-8 with undisplayable characters replaced, so a hit never stops the run.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_HITS = 1
EXIT_CANNOT_RUN = 2
EXIT_ARCHIVES = 3

# A path component after the user folder. Not a hit: a placeholder (<name>), a quote or space, or
# one of the generic example names below.
_GENERIC_NAMES = r"(?:me|user|username|someone|name|you|x|example|public|default|runner)"
_NAME = "(?!" + _GENERIC_NAMES + r"(?![\w.-]))" + r"""[^\\/\s<>"'`$%*{}()\[\]|,;:]+"""
_DASHED_NAME = "(?!" + _GENERIC_NAMES + r"(?![A-Za-z0-9._]))[A-Za-z0-9._]+"

BUILTIN_PATTERNS: dict[str, re.Pattern[str]] = {
    "windows-home-path": re.compile(rf"[A-Za-z]:[\\/]+Users[\\/]+{_NAME}", re.IGNORECASE),
    # Git Bash / MSYS form of a Windows home path: /c/Users/<name>.
    "msys-home-path": re.compile(rf"(?<![\w.])/[A-Za-z]/Users/{{1,}}{_NAME}", re.IGNORECASE),
    # macOS home path. Not preceded by a drive colon, a word or path character, so a drive-letter
    # or MSYS path is not also counted here.
    "macos-home-path": re.compile(rf"(?<![\w:.\\/-])/Users/{{1,}}{_NAME}", re.IGNORECASE),
    # Claude Code names a project directory after the working path with separators turned into
    # dashes: C--Users-<name>-Documents-...
    "dashed-home-path": re.compile(rf"(?:--|[\\/])Users-{_DASHED_NAME}", re.IGNORECASE),
    "unix-home-path": re.compile(r"/home/(?!" + _GENERIC_NAMES + r"/)[A-Za-z0-9._-]+/"),
    # Literal escape sequences (backslash-n and the like) are blanked out before this pattern
    # runs (see _ESCAPE), so a decorator after one is not an address and an address after one is.
    "email-address": re.compile(r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+"),
    "github-token": re.compile(r"\bgh[opsu]_[A-Za-z0-9]{20,}"),
    "github-fine-grained-token": re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    "slack-token": re.compile(r"\bxox[bp]-[A-Za-z0-9-]{10,}"),
    "google-api-key": re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
    "aws-access-key": re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    "api-secret-key": re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    "private-key-header": re.compile(r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY-----"),
}

# In a binary file the address shape also turns up in compressed data by chance, so
# there it needs a local part of three characters or more and domain labels of two or more.
_BINARY_EMAIL = re.compile(
    r"(?<![\w.+-])[\w.+-]{3,}@(?:[A-Za-z0-9-]{2,}\.)+[A-Za-z]{2,}(?![A-Za-z0-9-])"
)

# A backslash followed by n, r, t, f or v: a literal escape sequence in JSON or source text.
_ESCAPE = re.compile(r"\\[nrtfv]")

# An e-mail address that is not a hit: a GitHub noreply address, the Anthropic or GitHub noreply
# senders, or a reserved example domain. Anchored: "noreply" inside a real local part is a hit.
_HARMLESS_EMAIL = re.compile(
    r"[\w.+-]+@users\.noreply\.github\.com"
    r"|no-?reply@(?:anthropic|github)\.com"
    r"|[\w.+-]+@(?:[\w-]+\.)*(?:example\.(?:com|org|net)|example|invalid|test|localhost)",
    re.IGNORECASE,
)

_BINARY_SNIFF = 8192

Hit = tuple[str, int, str, str]


class CannotRun(Exception):
    """The check itself is impossible; maps to exit code 2."""


def load_forbidden(path: Path) -> list[re.Pattern[str]]:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise CannotRun(f"forbidden file unreadable: {type(exc).__name__}") from exc
    patterns: list[re.Pattern[str]] = []
    for number, line in enumerate(text.splitlines(), start=1):
        if "\ufeff" in line or "\x00" in line:
            raise CannotRun(f"forbidden file line {number} holds a BOM or NUL byte (wrong encoding?)")
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line, re.IGNORECASE))
        except re.error as exc:
            raise CannotRun(f"forbidden file line {number} is not a valid regex: {exc.msg}") from exc
    if not patterns:
        raise CannotRun("forbidden file holds no pattern")
    return patterns


def _excluded(rel: Path, excludes: list[str]) -> bool:
    posix = rel.as_posix()
    return any(posix == e or posix.startswith(e + "/") for e in excludes)


_GITMODULES_PATH = re.compile(r"^\s*path\s*=\s*(\S.*?)\s*$")


def submodule_paths(root: Path) -> list[str]:
    """The submodule paths declared in ``<root>/.gitmodules`` (publish-prep ticket 03b).

    A submodule is another project's content, not this repository's: its own licence and its own
    history apply, and a hit in it is not ours to fix. Only safe relative paths count: an empty
    path, ``.``, an absolute path or one with a ``..`` component is ignored, so a malformed entry
    can never turn the whole tree into "somebody else's content". A missing or unreadable file
    declares nothing."""
    try:
        text = (root / ".gitmodules").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    paths: list[str] = []
    for line in text.splitlines():
        match = _GITMODULES_PATH.match(line)
        if not match:
            continue
        raw = match.group(1).strip().strip('"').replace("\\", "/")
        parts = [p for p in raw.split("/") if p]
        if raw.startswith("/") or re.match(r"^[A-Za-z]:", raw) or not parts or {".", ".."} & set(parts):
            continue
        norm = "/".join(parts)
        if norm not in paths:
            paths.append(norm)
    return paths


def _scan_line(
    line: str, forbidden: list[re.Pattern[str]], binary: bool = False
) -> list[tuple[str, str]]:
    """Return (pattern name, span) for every hit in one line (or one binary block)."""
    found: list[tuple[str, str]] = []
    blanked = _ESCAPE.sub("  ", line)
    for name, pattern in BUILTIN_PATTERNS.items():
        subject = blanked if name == "email-address" else line
        if binary and name == "email-address":
            pattern = _BINARY_EMAIL
        for match in pattern.finditer(subject):
            if name == "email-address" and _HARMLESS_EMAIL.fullmatch(match.group(0)):
                continue
            found.append((name, match.group(0)))
    for index, pattern in enumerate(forbidden, start=1):
        for match in pattern.finditer(line):
            found.append((f"forbidden#{index}", match.group(0)))
    return found


def _decode_text(data: bytes) -> str | None:
    """Return the text of a text file, or None when the file is binary."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    if data.startswith(b"\xef\xbb\xbf"):
        return data.decode("utf-8-sig", errors="replace")
    if b"\x00" in data[:_BINARY_SNIFF]:
        return None
    return data.decode("utf-8", errors="replace")


_ARCHIVE_MAGIC = (b"PK\x03\x04", b"PK\x05\x06", b"\x1f\x8b", b"7z\xbc\xaf\x27\x1c", b"Rar!\x1a\x07",
    b"BZh", b"\xfd7zXZ\x00", b"\x28\xb5\x2f\xfd")  # bzip2, xz, zstd


def _is_archive(data: bytes) -> bool:
    if data.startswith(_ARCHIVE_MAGIC):
        return True
    if data[257:262] == b"ustar":  # tar (POSIX and GNU headers)
        return True
    return data.startswith(b"%PDF") and b"/FlateDecode" in data


def _is_junction(path: Path) -> bool:
    """A Windows junction. ``os.path.isjunction`` exists from Python 3.12 only (pyproject allows
    3.11): there, a mount-point reparse tag that is not a symlink is the same test."""
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return bool(isjunction(path))
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(info.st_mode)
        and getattr(info, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    )


def _link_kind(path: Path) -> str | None:
    if path.is_symlink():
        return "symlink"
    if _is_junction(path):
        return "junction"
    return None


def _walk_failed(exc: OSError) -> None:
    raise CannotRun(f"cannot list directory {exc.filename}: {type(exc).__name__}") from exc


def _entries(root: Path, excludes: list[str]):
    """Yield (absolute path, relative path) for every file, directory and link to scan."""
    for dirpath, dirnames, filenames in os.walk(root, onerror=_walk_failed):
        base = Path(dirpath)
        rel_base = base.relative_to(root)
        keep = []
        for name in sorted(dirnames):
            rel = rel_base / name
            if rel.as_posix() == ".git" or _excluded(rel, excludes):
                continue
            yield base / name, rel
            if _link_kind(base / name) is None:
                keep.append(name)
        dirnames[:] = keep
        for name in sorted(filenames):
            rel = rel_base / name
            if rel.as_posix() == ".git" or _excluded(rel, excludes):
                continue
            yield base / name, rel


def scan(
    root: Path,
    forbidden: list[re.Pattern[str]],
    excludes: list[str],
    archives: list[str] | None = None,
) -> tuple[list[Hit], int, int]:
    """Return (hits, text files scanned, binary files scanned). A hit is (file, line, pattern, span).

    The paths of archive-like files (scanned bytewise but not opened) are appended to ``archives``."""
    hits: list[Hit] = []
    scanned = binary = 0
    for path, rel in _entries(root, excludes):
        shown = rel.as_posix()
        for name, span in _scan_line(path.name, forbidden):
            hits.append((shown, 0, name, span))
        kind = _link_kind(path)
        if kind is not None:
            hits.append((shown, 0, kind, "not followed"))
            if kind == "symlink":
                for name, span in _scan_line(os.readlink(path), forbidden):
                    hits.append((shown, 0, name, span))
            continue
        if not path.is_file():
            continue
        try:
            data = path.read_bytes()
        except OSError as exc:
            raise CannotRun(f"cannot read {shown}: {type(exc).__name__}") from exc
        if archives is not None and _is_archive(data):
            archives.append(shown)
        text = _decode_text(data)
        if text is not None:
            scanned += 1
            for lineno, line in enumerate(text.splitlines(), start=1):
                for name, span in _scan_line(line, forbidden):
                    hits.append((shown, lineno, name, span))
            continue
        binary += 1
        seen: set[tuple[str, str]] = set()
        for view in (data.decode("latin-1"), data.decode("utf-8", errors="replace")):
            for name, span in _scan_line(view, forbidden, binary=True):
                if (name, span) not in seen:
                    seen.add((name, span))
                    hits.append((shown, 0, name, span))
    return hits, scanned, binary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("directory", type=Path)
    parser.add_argument("--forbidden-file", type=Path, default=None)
    parser.add_argument("--exclude", action="append", default=[], metavar="PREFIX")
    parser.add_argument("--strict-archives", action="store_true",
                        help="exit 3 when an archive-like file outside --allow-archive was not opened")
    parser.add_argument("--allow-archive", action="append", default=[], metavar="PREFIX",
                        help="with --strict-archives: an archive at or below this path is allowed")
    parser.add_argument("--scan-submodules", action="store_true",
                        help="also scan the paths declared in .gitmodules (skipped by default)")
    args = parser.parse_args(argv)
    if args.allow_archive and not args.strict_archives:
        parser.error("--allow-archive needs --strict-archives")
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    if not args.directory.is_dir():
        print(f"cannot run: {args.directory} is not a directory", file=sys.stderr)
        return EXIT_CANNOT_RUN
    forbidden: list[re.Pattern[str]] = []
    if args.forbidden_file is not None:
        try:
            forbidden = load_forbidden(args.forbidden_file)
        except CannotRun as exc:
            print(f"cannot run: {exc}", file=sys.stderr)
            return EXIT_CANNOT_RUN

    excludes = [e.strip("/\\").replace("\\", "/") for e in args.exclude]
    skipped_submodules = [] if args.scan_submodules else submodule_paths(args.directory)
    excludes += skipped_submodules
    for sub in skipped_submodules:
        print(f"submodule skipped: {sub} (declared in .gitmodules; another project's content)")
    try:
        archives: list[str] = []
        hits, scanned, binary = scan(args.directory, forbidden, excludes, archives)
    except CannotRun as exc:
        print(f"cannot run: {exc}", file=sys.stderr)
        return EXIT_CANNOT_RUN
    for file, lineno, name, span in hits:
        print(f"{file}:{lineno}: [{name}] {span}")
    counts: dict[str, int] = {}
    for _, _, name, _ in hits:
        counts[name] = counts.get(name, 0) + 1
    for name in sorted(counts):
        print(f"count {name}: {counts[name]}")
    for shown in archives:
        print(f"archive not opened: {shown}")
    allowed = [e.strip("/\\").replace("\\", "/") for e in args.allow_archive]
    refused = [a for a in archives if args.strict_archives and not _excluded(Path(a), allowed)]
    for shown in refused:
        print(f"archive not allowed: {shown}")
    print(
        f"scanned {scanned} text files and {binary} binary files (bytewise), "
        f"{len(archives)} archives not opened, {len(hits)} hits"
    )
    if hits:
        return EXIT_HITS
    return EXIT_ARCHIVES if refused else EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
