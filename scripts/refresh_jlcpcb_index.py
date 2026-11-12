"""Regenerate the vendored JLCPCB part-tier index from the library submodule (publish-prep ticket 03b).

    python scripts/refresh_jlcpcb_index.py [--library DIR] [--commit SHA] [--index FILE] [--dry-run]
                                           [--sample N]

Reads the symbol libraries of CDFER/JLCPCB-Kicad-Library (default: the git submodule at
``third_party/JLCPCB-Kicad-Library``) through ``sourcing.jlcpcb_basic.build_index``, prints how the
index changes (commit, part counts, tier moves with a sample, parts added or removed) and rewrites
``src/boardproof/sourcing/data/jlcpcb_basic_index.json``. ``Stock`` and ``Price`` are never read into
the index: upstream rewrites them daily. The library is only read, never modified or executed.

The index also records ``source.commit_date``, the committer date of the source commit (``git show -s
--format=%cI``, run in the library checkout). If git cannot give it, no date is recorded and the lookup tools say so.

Commit: with the default library the pinned commit is the submodule's gitlink in this repository
(``git rev-parse HEAD:third_party/JLCPCB-Kicad-Library``), and a checkout that differs from it is
refused. With ``--library`` you must name the commit with ``--commit`` (40 hex digits).

The new index is written to a temporary sibling, read back and compared with what was built, and
only then moved over the old one with ``os.replace``. A mismatch ends with ``write_not_verified``
and leaves the old index untouched.

A new index with fewer than 80% of the old parts is refused (``index_would_shrink``) unless
``--allow-shrink`` is given: upstream commit de4e6c4 (2026-09-21) emptied 17 of the 20 symbol
libraries, and following it would have turned 868 known Basic and Preferred parts into "unknown".

Move the pin (``--update``, build ticket 3): one command that takes the library to upstream's newest
commit.

    python scripts/refresh_jlcpcb_index.py --update [--dry-run] [--upstream URL] [--ref REF]

It fetches upstream's ``--ref`` (default HEAD) into a scratch repository, extracts only its
``symbols/`` directory, builds the candidate index and compares it with the committed one. It
REFUSES, moving nothing, when a symbol library that holds parts in the committed index holds none in
the candidate (``library_emptied``), when the part count falls below 80% of the old one
(``index_would_shrink``) or when a symbol file fails to parse. ``--allow-shrink`` is not accepted
together with ``--update``: to follow a real loss, move the pin by hand. When the candidate passes,
it prints the diff (commit, counts, parts that change tier), then with ``--dry-run`` stops, and
otherwise fetches that commit into the submodule, checks it out, writes the verified index and stages
the gitlink and the index (``git add``), leaving the commit to the person. The submodule must be
initialised for the apply step (``git submodule update --init --depth 1 third_party/JLCPCB-Kicad-Library``);
the dry run does not need it. An empty library is judged against the committed index, so a first
index has only the shrink rule. ``--repo-root`` and ``--upstream`` exist so tests can use stand-in repositories.

Exit codes: 0 done (or dry run done); 2 the refresh could not run (library missing or empty, no
commit, no parts, shrink refused, write not verified).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import io
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Use this checkout's own package, not whichever copy the interpreter has installed.
sys.path.insert(0, str(ROOT / "src"))

# importlib, not an import statement: the path above must come first.
jlc = importlib.import_module("boardproof.sourcing.jlcpcb_basic")

DEFAULT_LIBRARY = ROOT / jlc.SUBMODULE_RELATIVE_PATH
_SHA = re.compile(r"^[0-9a-f]{40}$")
#: A new index below this fraction of the old part count is refused without --allow-shrink.
#: Upstream commit de4e6c4 (2026-09-21) emptied 17 of the 20 symbol libraries: 889 parts became 21.
SHRINK_LIMIT = 0.8


def _key(part: dict) -> str:
    return part.get("lcsc") or f"{part.get('symbol_lib')}/{part.get('symbol')}"


def diff_payloads(old: dict | None, new: dict) -> dict:
    """Compare two index payloads by LCSC number. ``old`` may be None (no index yet)."""
    old_parts = {_key(p): p for p in (old or {}).get("parts", [])}
    new_parts = {_key(p): p for p in new.get("parts", [])}
    added = sorted(set(new_parts) - set(old_parts))
    removed = sorted(set(old_parts) - set(new_parts))
    tier_changes = [
        (k, old_parts[k]["tier"], new_parts[k]["tier"])
        for k in sorted(set(old_parts) & set(new_parts))
        if old_parts[k].get("tier") != new_parts[k].get("tier")
    ]
    other_changes = [
        k
        for k in sorted(set(old_parts) & set(new_parts))
        if old_parts[k].get("tier") == new_parts[k].get("tier")
        and {f: v for f, v in old_parts[k].items()} != {f: v for f, v in new_parts[k].items()}
    ]
    return {
        "old_commit": ((old or {}).get("source") or {}).get("commit"),
        "new_commit": (new.get("source") or {}).get("commit"),
        "old_counts": (old or {}).get("counts") or {},
        "new_counts": new.get("counts") or {},
        "added": added,
        "removed": removed,
        "tier_changes": tier_changes,
        "other_changes": other_changes,
        "new_parts": new_parts,
        "old_parts": old_parts,
    }


def format_diff(diff: dict, sample: int) -> str:
    def short(c: str | None) -> str:
        return (c or "none")[:7]

    lines = [f"commit: {short(diff['old_commit'])} -> {short(diff['new_commit'])}"]
    oc, nc = diff["old_counts"], diff["new_counts"]
    for name in ("total", "basic", "preferred", "extended"):
        lines.append(f"{'parts' if name == 'total' else name}: {oc.get(name, 0)} -> {nc.get(name, 0)}")
    changes = diff["tier_changes"]
    lines.append(f"tier changes: {len(changes)}")
    for (a, b), n in sorted(Counter((a, b) for _, a, b in changes).items()):
        lines.append(f"  {a} -> {b}: {n}")
    for key, a, b in changes[:sample]:
        part = diff["new_parts"][key]
        lines.append(f"    {key} {part.get('mpn')}: {a} -> {b}")
    if len(changes) > sample:
        lines.append(f"    ... and {len(changes) - sample} more")
    for label in ("added", "removed"):
        keys = diff[label]
        lines.append(f"{label}: {len(keys)}")
        source = diff["new_parts"] if label == "added" else diff["old_parts"]
        for key in keys[:sample]:
            part = source[key]
            lines.append(f"    {key} {part.get('mpn')} ({part.get('tier')})")
        if len(keys) > sample:
            lines.append(f"    ... and {len(keys) - sample} more")
    lines.append(f"other field changes (same tier): {len(diff['other_changes'])}")
    return "\n".join(lines)


def _fail(message: str) -> int:
    print(f"refresh_jlcpcb_index: {message}", file=sys.stderr)
    return 2


def _gitlink_commit() -> tuple[str | None, str]:
    """(pinned commit, problem). The commit comes from this repository's own gitlink."""
    git = ["git", "-C", str(ROOT)]
    pinned = subprocess.run(
        [*git, "rev-parse", f"HEAD:{jlc.SUBMODULE_RELATIVE_PATH}"], capture_output=True, text=True, check=False,
        env=_git_env(),
    )
    if pinned.returncode != 0 or not _SHA.match(pinned.stdout.strip()):
        return None, f"no committed gitlink at {jlc.SUBMODULE_RELATIVE_PATH}: {pinned.stderr.strip()}"
    status = subprocess.run(
        [*git, "submodule", "status", "--", jlc.SUBMODULE_RELATIVE_PATH], capture_output=True, text=True, check=False,
        env=_git_env(),
    )
    marker = status.stdout[:1]
    if marker == "+":
        return None, "the submodule checkout differs from the pinned commit; run `git submodule update` first"
    if marker == "-":
        return None, f"the submodule is not initialised; fetch it with: {jlc.SUBMODULE_FETCH_COMMAND}"
    return pinned.stdout.strip(), ""


def _commit_date(library: Path, commit: str) -> str | None:
    """Committer date (ISO 8601) of ``commit`` in the library checkout, or None when git cannot say."""
    try:
        done = subprocess.run(
            ["git", "-C", str(library), "show", "-s", "--format=%cI", commit],
            capture_output=True, text=True, check=False, env=_git_env(),
        )
    except OSError:
        return None
    text = done.stdout.strip()
    if done.returncode != 0 or jlc.valid_index_date(text) is None:
        return None
    return text


def _write_verified_temp(built: dict, target: Path, commit: str, commit_date: str | None) -> tuple[Path | None, str]:
    """Write ``built`` to a temporary sibling of ``target`` and read it back. (tmp path, "") when it
    matches what was built; (None, problem) otherwise, with the temporary file already removed."""
    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    try:
        written = jlc.write_index(built, tmp)
        verified = False
        if written.get("status") == "ok":
            back = json.loads(tmp.read_text(encoding="utf-8"))
            verified = (
                back.get("parts") == built["parts"]
                and (back.get("source") or {}).get("commit") == commit
                and (back.get("source") or {}).get("commit_date") == commit_date
                and back.get("counts") == built["counts"]
            )
        if not verified:
            tmp.unlink(missing_ok=True)
            return None, "write_not_verified: the index read back differs from the one built; nothing replaced"
    except (OSError, ValueError) as exc:
        tmp.unlink(missing_ok=True)
        return None, f"write_not_verified: {type(exc).__name__}: {exc}"
    return tmp, ""


def emptied_libraries(old: dict | None, built: dict) -> list[str]:
    """Symbol libraries that hold parts in ``old`` and none in ``built`` (an absent file counts as none)."""
    had = Counter(p.get("symbol_lib") for p in (old or {}).get("parts", []))
    now = Counter(p.get("symbol_lib") for p in built.get("parts", []))
    return sorted(str(lib) for lib in had if lib and now.get(lib, 0) == 0)


def update_refusal(old: dict | None, built: dict) -> str | None:
    """Why a candidate index must not become the pin, or None. No override exists for this mode."""
    emptied = emptied_libraries(old, built)
    if emptied:
        return (
            f"library_emptied: {len(emptied)} symbol librar{'y' if len(emptied) == 1 else 'ies'} that hold parts in "
            f"the committed index hold none in the candidate ({', '.join(emptied)}). Upstream is broken at this "
            "commit (see issue 41); the pin was not moved."
        )
    old_total = (old or {}).get("counts", {}).get("total", 0)
    if built["counts"]["total"] < SHRINK_LIMIT * old_total:
        return (
            f"index_would_shrink: {old_total} -> {built['counts']['total']} parts (below {SHRINK_LIMIT:.0%}); "
            "the pin was not moved."
        )
    failed = built.get("parse_failed_libraries") or []
    if failed:
        return f"parse failed for {', '.join(failed)}; the pin was not moved."
    return None


# Same list as boardproof.schematic_diff._REDIRECTING: variables that point git at another repository
# or inject configuration. Copied, not imported: a script must not depend on a product module's privates.
_REDIRECTING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
                "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR", "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES",
                "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS", "GIT_EXEC_PATH",
                "GIT_SSH", "GIT_SSH_COMMAND", "GIT_PROXY_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_ASKPASS")
#: Seconds. A network fetch may be slow; every other git call is local.
GIT_TIMEOUT = 60
GIT_NETWORK_TIMEOUT = 600
#: Caps on what is read from upstream: the whole `git archive` of symbols/ and each extracted file.
ARCHIVE_LIMIT = 64 * 1024 * 1024
MEMBER_LIMIT = 16 * 1024 * 1024
_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
#: Transport rules for every git call that touches a remote. protocol.allow=never switches every
#: transport off, then https is allowed (the upstream is a GitHub https URL) and `file` only for a path
#: the person typed on the command line (`user`: git allows it when not run from a submodule/helper):
#: tests and --upstream need it, the url read from .gitmodules is held to https by _check_upstream.
_NETWORK_CONFIG = ("-c", "protocol.allow=never", "-c", "protocol.https.allow=always", "-c", "protocol.file.allow=user")


def _git_env() -> dict[str, str]:
    """The process environment with git redirection removed and user/system configuration cut off."""
    env = {k: v for k, v in os.environ.items()
           if k not in _REDIRECTING and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0", GIT_CONFIG_NOSYSTEM="1",
               GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_COUNT="0")
    return env


def _git(cwd: Path, *args: str, network: bool = False) -> subprocess.CompletedProcess:
    """``git -C cwd args``. Never raises on a git failure or timeout (a timeout is returncode 124)."""
    cmd = ["git", *(_NETWORK_CONFIG if network else ()), "-C", str(cwd), *args]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, env=_git_env(),
                              timeout=GIT_NETWORK_TIMEOUT if network else GIT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, stdout="", stderr="git timed out")
    except OSError as exc:
        return subprocess.CompletedProcess(cmd, 127, stdout="", stderr=f"cannot run git: {exc}")


def _check_ref(ref: str) -> str | None:
    if not _REF.fullmatch(ref):
        return f"--ref {ref!r} is not a plain ref name or commit id (letters, digits, . _ / -; no leading dash)"
    return None


def _redact(url: str) -> str:
    """``url`` without a user:password@ part, for any message."""
    return re.sub(r"(//)[^/@]*@", r"\1***@", url)


def _check_upstream(url: str, *, explicit: bool) -> str | None:
    """None when ``url`` may be fetched. From .gitmodules only https; typed on the command line also a
    local directory or file:// URL (tests, a mirror). Never a leading dash or whitespace; anything else (ssh://, ext::, git://) fails the allow-list."""
    if not url or url.startswith("-") or any(c.isspace() or ord(c) < 32 for c in url):
        return f"upstream {_redact(url)!r} is not acceptable (empty, leading dash or whitespace)"
    if re.fullmatch(r"https://[^/@]+/\S+", url):  # no user:password@ -- credentials are never accepted here
        return None
    if explicit and (url.startswith("file://") or Path(url).is_dir()):
        return None
    return f"upstream {_redact(url)!r} must be an https:// URL without credentials" + (" or an existing local directory" if explicit else "")


def _rmtree(path: Path) -> None:
    def _chmod_and_retry(func, target, _exc):  # git object files are read-only on Windows
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onerror=_chmod_and_retry)


def _upstream_url(root: Path) -> str | None:
    done = _git(root, "config", "-f", ".gitmodules", "--get", f"submodule.{jlc.SUBMODULE_RELATIVE_PATH}.url")
    return (done.stdout.strip() or None) if done.returncode == 0 else None


_SYMBOL_FILE = re.compile(r"symbols/JLCPCB-[^/\\]+\.kicad_sym")


def _extract_symbols(bare: Path, commit: str, dest: Path) -> str | None:
    """Write ``symbols/JLCPCB-*.kicad_sym`` of ``commit`` under ``dest``. Returns a problem or None.
    The archive is capped at ARCHIVE_LIMIT bytes and each extracted file at MEMBER_LIMIT."""
    proc = subprocess.Popen(
        ["git", "-C", str(bare), "archive", "--format=tar", commit, "symbols"],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_git_env(),
    )
    try:
        data = proc.stdout.read(ARCHIVE_LIMIT + 1)
        if len(data) > ARCHIVE_LIMIT:
            proc.kill()
            return f"the symbols/ archive of {commit[:7]} is larger than {ARCHIVE_LIMIT} bytes"
    finally:
        proc.stdout.close()
        proc.wait()
    if proc.returncode != 0:
        return f"git archive failed (exit {proc.returncode})"
    try:
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            for member in tar.getmembers():
                if not member.isfile() or not _SYMBOL_FILE.fullmatch(member.name):
                    continue
                if member.size > MEMBER_LIMIT:
                    return f"{member.name} is larger than {MEMBER_LIMIT} bytes"
                target = dest / member.name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(tar.extractfile(member).read(MEMBER_LIMIT + 1))
    except (tarfile.TarError, OSError) as exc:
        return f"cannot read the symbol libraries of {commit[:7]}: {type(exc).__name__}: {exc}"
    return None


def _restore(root: Path, sub: str, sub_dir: Path, index: Path, index_rel: str, old_commit: str,
             old_bytes: bytes) -> list[str]:
    """Put the submodule commit, the index bytes and the staging back. Returns what could NOT be restored."""
    left: list[str] = []
    if _git(sub_dir, "rev-parse", "HEAD").stdout.strip() != old_commit:
        _git(sub_dir, "checkout", "-q", "--detach", old_commit)
        if _git(sub_dir, "rev-parse", "HEAD").stdout.strip() != old_commit:
            left.append(f"git -C {sub} checkout --detach {old_commit}")
    try:
        same = index.read_bytes() == old_bytes
    except OSError:
        same = False
    if not same:
        try:
            index.write_bytes(old_bytes)
            same = index.read_bytes() == old_bytes
        except OSError:
            same = False
        if not same:
            left.append(f"git checkout HEAD -- {index_rel}")
    _git(root, "reset", "-q", "--", sub, index_rel)
    if _git(root, "diff", "--cached", "--name-only", "--", sub, index_rel).stdout.strip():
        left.append(f"git reset -- {sub} {index_rel}")
    return left


def _apply(root: Path, sub: str, sub_dir: Path, index: Path, index_rel: str, upstream: str, ref: str,
           resolved: str, tmp: Path) -> str | None:
    """Move the pin, the index and the staging together. Returns a problem (everything restored, or the
    manual commands for what could not be) or None. Order, chosen so the likeliest failure comes first:
    the submodule fetch only adds objects; the index replace (the step Windows can deny) comes before
    the checkout, so a failure there has nothing to undo; checkout; stage. Any failure after the
    replace restores the old commit, the old index bytes and the old staging."""
    old_commit = _git(sub_dir, "rev-parse", "HEAD").stdout.strip()
    if not _SHA.match(old_commit):
        return "cannot read the submodule's current commit; nothing changed"
    try:
        old_bytes = index.read_bytes()
    except OSError as exc:
        return f"cannot read {index_rel} ({type(exc).__name__}); nothing changed"
    got = _git(sub_dir, "fetch", "--depth", "1", "-q", "--", upstream, ref, network=True)
    if got.returncode != 0 or _git(sub_dir, "rev-parse", "FETCH_HEAD^{commit}").stdout.strip() != resolved:
        return (f"the submodule fetch of {ref} failed or upstream moved meanwhile ({got.stderr.strip()}); "
                "run again; nothing changed")
    failure = None
    try:
        os.replace(tmp, index)
        if _git(sub_dir, "checkout", "-q", "--detach", resolved).returncode != 0:
            failure = "git checkout in the submodule failed"
        else:
            staged = _git(root, "add", "--", sub, index_rel)
            if staged.returncode != 0:
                failure = f"git add failed: {staged.stderr.strip()}"
    except BaseException as exc:  # noqa: BLE001 -- any failure, Ctrl-C included, must roll back
        failure = f"{type(exc).__name__}: {exc}"
        interrupt = None if isinstance(exc, Exception) else exc
    else:
        interrupt = None
    if failure is None:
        return None
    try:
        left = _restore(root, sub, sub_dir, index, index_rel, old_commit, old_bytes)
    except Exception as exc:  # noqa: BLE001 -- keep the original failure text
        left = [f"(the restore itself raised {type(exc).__name__}) git -C {sub} checkout --detach {old_commit}; "
                f"git checkout HEAD -- {index_rel}; git reset -- {sub} {index_rel}"]
    if interrupt is not None:
        raise interrupt
    if not left:
        return (f"{failure}. Restored the submodule to {old_commit[:7]}, {index_rel} to its old bytes and "
                "unstaged both; nothing changed.")
    return (f"{failure}. The rollback is INCOMPLETE; run these in {root} to finish it: " + "; ".join(left))


def update_to_newest(root: Path, upstream: str | None, ref: str, index: Path, dry_run: bool, sample: int) -> int:
    """Move the submodule pin and the index to ``ref`` of ``upstream``, or refuse and move nothing."""
    sub = jlc.SUBMODULE_RELATIVE_PATH
    explicit = upstream is not None
    problem = _check_ref(ref)
    if problem:
        return _fail(f"{problem}; nothing changed")
    if upstream is None:
        upstream = _upstream_url(root)
    if upstream is None:
        return _fail("no upstream: .gitmodules names no url for the submodule; pass --upstream URL")
    problem = _check_upstream(upstream, explicit=explicit)
    if problem:
        return _fail(f"{problem}; nothing changed")
    if not upstream.startswith(("https://", "file://")):
        upstream = str(Path(upstream).resolve())  # git runs in the scratch repository: no relative paths
    pinned = _git(root, "rev-parse", f"HEAD:{sub}")
    pinned_sha = pinned.stdout.strip()
    if pinned.returncode != 0 or not _SHA.match(pinned_sha):
        return _fail(f"no committed gitlink at {sub}: {pinned.stderr.strip()}")
    try:
        old = json.loads(index.read_text(encoding="utf-8")) if index.is_file() else None
    except (OSError, ValueError) as exc:
        return _fail(f"the committed index cannot be read ({type(exc).__name__}); nothing changed")

    scratch = Path(tempfile.mkdtemp(prefix="jlcpcb-refresh-"))
    try:
        bare = scratch / "fetch"
        if _git(scratch, "init", "--bare", "-q", str(bare)).returncode != 0:
            return _fail("git init failed in the scratch directory")
        fetched = _git(bare, "fetch", "--depth", "1", "-q", "--", upstream, ref, network=True)
        if fetched.returncode != 0:
            return _fail(f"cannot fetch {ref} from {_redact(upstream)}: {fetched.stderr.strip()}; nothing changed")
        resolved = _git(bare, "rev-parse", "FETCH_HEAD^{commit}").stdout.strip()
        if not _SHA.match(resolved):
            return _fail(f"upstream {ref} did not resolve to a commit; nothing changed")
        print(f"upstream {ref}: {resolved}; pinned: {pinned_sha}")
        if resolved == pinned_sha:
            print("already at the newest commit: nothing to do")
            return 0
        commit_date = _commit_date(bare, resolved)
        problem = _extract_symbols(bare, resolved, scratch / "lib")
        if problem:
            return _fail(f"{problem}; nothing changed")
        built = jlc.build_index(scratch / "lib", commit=resolved)
        if built.get("status") != "ok":
            return _fail(f"the library at {resolved[:7]} gave no usable index: {built.get('reason')}; "
                         "the pin was not moved")
        if commit_date:
            built["source"]["commit_date"] = commit_date
        print(f"commit date: {commit_date or 'not recorded'}")
        print(format_diff(diff_payloads(old, built), sample))
        refusal = update_refusal(old, built)
        if refusal:
            return _fail(refusal)
        sub_dir = root / sub
        if dry_run:
            print(f"dry run: would fetch {resolved[:7]} into {sub}, check it out, write {index.name} and stage both; "
                  "nothing changed")
            if not (sub_dir / ".git").exists():
                print(f"note: applying needs the submodule initialised first: {jlc.SUBMODULE_FETCH_COMMAND}")
            return 0
        if not (sub_dir / ".git").exists():
            return _fail(f"the submodule at {sub} is not initialised (the directory is empty or missing). "
                         f"Fetch it first with: {jlc.SUBMODULE_FETCH_COMMAND}; nothing changed")
        try:
            index_rel = index.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return _fail(f"{index} lies outside {root}; it cannot be staged; nothing changed")
        dirty = _git(root, "status", "--porcelain", "--", sub, index_rel).stdout.strip()
        if dirty:
            return _fail(f"{sub} or {index_rel} has uncommitted changes; commit or revert them first:\n{dirty}")
        tmp, problem = _write_verified_temp(built, index, resolved, commit_date)
        if tmp is None:
            return _fail(problem)
        try:
            problem = _apply(root, sub, sub_dir, index, index_rel, upstream, ref, resolved, tmp)
        finally:
            tmp.unlink(missing_ok=True)
        if problem:
            return _fail(problem)
        print(_git(root, "diff", "--cached", "--stat", "--", sub, index_rel).stdout.rstrip())
        print(f"staged the new pin {resolved[:7]} and {index_rel} ({built['counts']['total']} parts); "
              "review, then commit")
        return 0
    finally:
        _rmtree(scratch)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--library", type=Path, default=None, help="library checkout (default: the submodule)")
    parser.add_argument("--commit", default=None, help="commit of the library checkout (40 hex digits)")
    parser.add_argument("--index", type=Path, default=None, help="index file to rewrite")
    parser.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    parser.add_argument("--allow-shrink", action="store_true",
                        help=f"accept a new index with fewer than {SHRINK_LIMIT:.0%} of the old parts")
    parser.add_argument("--update", action="store_true",
                        help="move the submodule pin and the index to upstream's newest commit (see the module text)")
    parser.add_argument("--upstream", default=None, help="with --update: repository URL (default: .gitmodules)")
    parser.add_argument("--ref", default="HEAD", help="with --update: upstream ref to take (default HEAD)")
    parser.add_argument("--repo-root", type=Path, default=ROOT,
                        help="with --update: the superproject (default: this one)")
    parser.add_argument("--sample", type=int, default=10, metavar="N", help="parts to name per list (default 10)")
    args = parser.parse_args(argv)

    if args.update:
        if args.allow_shrink or args.library is not None or args.commit is not None:
            return _fail("--update takes no --allow-shrink, --library or --commit: it refuses a broken upstream "
                         "and moves the pin itself")
        index = args.index if args.index is not None else (
            args.repo_root / "src" / "boardproof" / "sourcing" / "data" / "jlcpcb_basic_index.json")
        return update_to_newest(args.repo_root, args.upstream, args.ref, index, args.dry_run, max(args.sample, 0))

    library = args.library if args.library is not None else DEFAULT_LIBRARY
    commit = args.commit
    if commit is not None and not _SHA.match(commit):
        return _fail("--commit must be a full 40-digit lowercase hex commit id")
    if args.library is None and commit is None:
        commit, problem = _gitlink_commit()
        if commit is None:
            return _fail(problem)
    if commit is None:
        return _fail("with --library you must also pass --commit (the index records its source commit)")

    if _problem := jlc._source_problem("submodule", library):
        return _fail(_problem)

    built = jlc.build_index(library, commit=commit)
    if built.get("status") != "ok":
        return _fail(f"the library gave no usable index: {built.get('reason')}")

    commit_date = _commit_date(library, commit)
    if commit_date:
        built["source"]["commit_date"] = commit_date
    print(f"commit date: {commit_date or 'not recorded (git could not give the date of this commit)'}")

    target = args.index if args.index is not None else jlc.VENDORED_INDEX_PATH
    old = None
    if target.is_file():
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"note: the existing index cannot be read ({type(exc).__name__}); diffing against none")
    print(format_diff(diff_payloads(old, built), max(args.sample, 0)))
    failed = built.get("parse_failed_libraries") or []
    for name in failed:
        print(f"parse failed: {name}.kicad_sym (its parts are missing from the new index)")
    old_total = (old or {}).get("counts", {}).get("total", 0)
    if not args.allow_shrink and built["counts"]["total"] < SHRINK_LIMIT * old_total:
        return _fail(
            f"index_would_shrink: {old_total} -> {built['counts']['total']} parts (below {SHRINK_LIMIT:.0%}). "
            "The library at this commit may be broken upstream; nothing written. "
            "Pass --allow-shrink if the loss is real."
        )
    if failed and not args.allow_shrink:
        return _fail(
            f"parse failed for {len(failed)} symbol librar{'y' if len(failed) == 1 else 'ies'} "
            f"({', '.join(failed)}); nothing written, even though {built['counts']['total']} parts remain. "
            "Fix the library or pass --allow-shrink if the loss is real."
        )
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    tmp, problem = _write_verified_temp(built, target, commit, commit_date)
    if tmp is None:
        return _fail(problem)
    try:
        os.replace(tmp, target)
    except OSError as exc:
        return _fail(f"write_not_verified: {type(exc).__name__}: {exc}")
    finally:
        tmp.unlink(missing_ok=True)
    print(f"wrote {target} ({built['counts']['total']} parts, commit {commit[:7]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
