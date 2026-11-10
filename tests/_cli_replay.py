"""Replay cache for kicad-cli runs in the test session (issue 172). Test infrastructure only: it lives in
``tests/``, is installed by ``tests/conftest.py`` only when ``KICAD_MCP_CLI_REPLAY=1`` and is never
imported by ``src/``.

What it does. A test run calls ``kicad-cli ... upgrade`` many times on byte-identical inputs (every write
tool parse-checks a copy of the sheet; 62 percent of the 5109 ``sch upgrade`` runs of the full suite had an
input and argv that an earlier run had; measured with the counting hook of issue 172). The wrapper around
``subprocess.run`` answers such a call from a recording of an earlier real run: same return code, same
stdout and stderr, and the same files created, changed or removed beside the inputs and the outputs.

WHAT IS CACHED: ``sch|pcb|sym|fp upgrade`` and ``version``. WHAT IS NEVER CACHED, by design:
  * ``pcb drc`` and ``sch erc``: the gate runs DRC three times because runs can differ
    (``verify_board.GATE_RUNS``); a cache would return one answer and hide that instability. They also had
    0 and 7 repeated inputs in the whole suite, so there is nothing to win;
  * ``sch export netlist`` and every ``pcb export``: 32 of 6886 and 21 of 165 repeated, not worth a replay
    of outputs that carry dates and absolute paths;
  * any call with a ``timeout`` under 60 s (a test of a timeout must see it), without captured output, with
    ``input``/``check``, or whose directories exceed ``MAX_FILES``/``MAX_BYTES``; a crash exit (outside 0
    to 255); binary output that contains a path;
  * the tests in ``EXCLUDED_FILES`` (the gate and the DRC-instability tests), which run with the cache off.

The key (everything that can change the answer, else the call is a miss and runs for real):
  * the output of ``kicad-cli version`` and the executable's name,
  * the argv, with each directory the call reads or writes replaced by a token (``<<KMCP_W0>>``), so a
    copy of the same project in another temporary directory has the same key,
  * the relative name and sha256 of EVERY file under each such directory (the inputs of a project are
    not only the files named in the argv: sub-sheets, ``.kicad_pro``, ``.kicad_dru``, library tables),
  * the ``KICAD*``/``KIPRJMOD*`` environment variables, cwd, and the text/encoding arguments.

Verify mode (``KICAD_MCP_CLI_REPLAY_VERIFY=1``): on a hit the real call runs too and the two are compared
(return code, stdout, stderr, every restored file); a difference raises ``ReplayMismatch``. This is how the
fidelity of the replay was measured over the full suite.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

ENV_ON = "KICAD_MCP_CLI_REPLAY"
ENV_DIR = "KICAD_MCP_CLI_REPLAY_DIR"
ENV_VERIFY = "KICAD_MCP_CLI_REPLAY_VERIFY"

# first words of the argv (after the executable) that may be replayed
CACHEABLE: tuple[tuple[str, ...], ...] = (
    ("sch", "upgrade"),
    ("pcb", "upgrade"),
    ("sym", "upgrade"),
    ("fp", "upgrade"),
    ("version",),
)
# test files that run with the cache off: the gate and its DRC-instability tests, and everything that
# drives a whole rung (they exist to see a real kicad-cli verdict)
EXCLUDED_FILES: tuple[str, ...] = (
    "test_verify_board", "test_gate_", "test_batch_gate", "test_rung", "test_autonomy_run", "test_ladder",
)
MAX_STORE_BYTES = 600 * 1024 * 1024  # per process
paused = False
MAX_FILES = 3000
MAX_BYTES = 400 * 1024 * 1024
MIN_TIMEOUT_S = 60.0
_TOKEN = "<<KMCP_W{}>>"
_REAL_RUN = subprocess.run

_lock = threading.Lock()
_stats = {"hit": 0, "miss": 0, "stored": 0}
_hash_memo: dict[tuple[str, int, int], str] = {}
_version_memo: dict[str, str] = {}


class ReplayMismatch(AssertionError):
    pass


def _sha(path: str) -> str:
    st = os.stat(path)
    k = (path, st.st_size, st.st_mtime_ns)
    got = _hash_memo.get(k)
    if got is None:
        h = hashlib.sha256()
        with open(path, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        got = _hash_memo[k] = h.hexdigest()
    return got


def snapshot(root: str) -> dict[str, str] | None:
    """relative posix name -> sha256 of every file under ``root``; None when the directory is too big."""
    out: dict[str, str] = {}
    total = 0
    for base, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            p = os.path.join(base, name)
            try:
                total += os.stat(p).st_size
                if len(out) >= MAX_FILES or total > MAX_BYTES:
                    return None
                out[os.path.relpath(p, root).replace(os.sep, "/")] = _sha(p)
            except OSError:
                return None
    return out


def _forms(path: str) -> list[str]:
    forms = {path, path.replace("\\", "/"), path.replace("/", "\\")}
    try:
        real = os.path.realpath(path)
        forms |= {real, real.replace("\\", "/"), real.replace("/", "\\")}
    except OSError:
        pass
    return sorted(forms, key=len, reverse=True)


def _detoken(text: str, dirs: list[str], forward: bool) -> str:
    """forward: replace each directory by its token; else the token by the directory (backslash form)."""
    if forward:
        for i, d in enumerate(dirs):
            for form in _forms(d):
                text = text.replace(form, _TOKEN.format(i))
    else:
        for i, d in enumerate(dirs):
            text = text.replace(_TOKEN.format(i), d)
    return text


def _cli_version(exe: str) -> str:
    got = _version_memo.get(exe)
    if got is None:
        done = _REAL_RUN([exe, "version"], capture_output=True, text=True, check=False, timeout=120)
        got = _version_memo[exe] = f"{done.returncode}:{done.stdout.strip()}"
    return got


def _words(argv: list[str]) -> list[str]:
    return [a for a in argv[1:] if not a.startswith("-")]


def _watch_dirs(argv: list[str], cwd: str | None) -> list[str] | None:
    """The directories the call reads or writes: that of every argv path that exists, and of every
    output (``-o``/``--output``; a path with no suffix is a directory). None when it cannot be told."""
    found: list[str] = []
    args = [str(a) for a in argv[1:]]
    for a in args:
        if a.startswith("-") and len(a) > 1 and not (os.sep in a or "/" in a):
            continue
        if not (os.sep in a or "/" in a or os.path.exists(a)):
            continue
        p = os.path.abspath(os.path.join(cwd, a) if cwd and not os.path.isabs(a) else a)
        if os.path.isdir(p):
            d = p
        elif os.path.isfile(p):
            d = os.path.dirname(p)
        else:  # an output that does not exist yet
            d = p if (not os.path.splitext(p)[1] or p.endswith(("/", "\\"))) else os.path.dirname(p)
            if not os.path.isdir(d):
                return None  # kicad-cli would create it: leave this to a real run
        found.append(os.path.normpath(d))
    out: list[str] = []
    for d in found:  # outermost wins; keep the order of first appearance
        if any(d == o or d.startswith(o + os.sep) for o in out):
            continue
        out = [o for o in out if not o.startswith(d + os.sep)]
        if d not in out:
            out.append(d)
    return out or None


def _eligible(argv, kw) -> bool:
    if not isinstance(argv, (list, tuple)) or not argv:
        return False
    exe = str(argv[0])
    if not os.path.basename(exe).lower().startswith("kicad-cli"):
        return False
    if os.environ.get(ENV_ON) != "1" or paused:
        return False
    words = tuple(_words([str(a) for a in argv]))
    if not any(words[: len(c)] == c for c in CACHEABLE):
        return False
    if not (kw.get("capture_output") or (kw.get("stdout") == subprocess.PIPE and kw.get("stderr") == subprocess.PIPE)):
        return False
    if kw.get("input") is not None or kw.get("stdin") is not None or kw.get("check"):
        return False
    timeout = kw.get("timeout")
    if timeout is not None and timeout < MIN_TIMEOUT_S:
        return False
    return True


def _key(argv, kw, dirs, snaps) -> str:
    cwd = kw.get("cwd")
    env = kw.get("env")
    env_items = sorted((k, v) for k, v in (env if env is not None else os.environ).items()
                       if k.upper().startswith(("KICAD", "KIPRJMOD")) and not k.startswith("KICAD_MCP_CLI_REPLAY"))
    doc = {
        "version": _cli_version(str(argv[0])),
        "exe": os.path.basename(str(argv[0])).lower(),
        "argv": [_detoken(str(a), dirs, True) for a in argv[1:]],
        "snaps": snaps,
        "env": env_items,
        "cwd": _detoken(str(cwd), dirs, True) if cwd else None,
        "text": [bool(kw.get("text") or kw.get("universal_newlines")), kw.get("encoding"), kw.get("errors")],
    }
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _store_dir() -> Path:
    d = os.environ.get(ENV_DIR)
    if not d:
        d = tempfile.mkdtemp(prefix="kicad-mcp-cli-replay-")
        os.environ[ENV_DIR] = d
    p = Path(d)
    p.mkdir(parents=True, exist_ok=True)
    return p


def _tok_bytes(data: bytes, dirs, forward: bool):
    """Tokenise (or restore) the paths in a file's bytes; None when binary content holds a path."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        if forward:
            for d in dirs:
                for form in _forms(d):
                    if form.encode() in data:
                        return None
        return data
    if forward and "<<KMCP_W" in text:
        return None  # the token itself is in the data: it could not be told apart on the way back
    return _detoken(text, dirs, forward).encode("utf-8")


def _changed(before: dict, after: dict) -> tuple[list[str], list[str]]:
    changed = [n for n, h in after.items() if before.get(n) != h]
    gone = [n for n in before if n not in after]
    return changed, gone


def _record(entry: Path, proc, dirs, before, after_snaps) -> bool:
    files = []
    tmp = Path(tempfile.mkdtemp(prefix="rec-", dir=entry.parent))
    try:
        for i, d in enumerate(dirs):
            changed, gone = _changed(before[i], after_snaps[i])
            for n in changed:
                data = Path(d, n).read_bytes()
                tok = _tok_bytes(data, dirs, True)
                if tok is None:
                    return False
                blob = f"{len(files)}.bin"
                (tmp / blob).write_bytes(tok)
                _stats["stored"] += len(tok)
                files.append({"dir": i, "name": n, "blob": blob})
            for n in gone:
                files.append({"dir": i, "name": n, "gone": True})
        out, err = proc.stdout, proc.stderr
        meta = {"returncode": proc.returncode, "files": files,
                "stdout": _tok_std(out, dirs, True), "stderr": _tok_std(err, dirs, True)}
        (tmp / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
        try:
            os.replace(tmp, entry)  # atomic; another worker may have won the race
        except OSError:
            return True
        return True
    finally:
        if tmp.exists():
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)


def _tok_std(value, dirs, forward):
    if value is None:
        return None
    if isinstance(value, bytes):
        got = _tok_bytes(value, dirs, forward)
        return {"b": (got if got is not None else value).hex()}
    return {"s": _detoken(value, dirs, forward)}


def _untok_std(doc, dirs):
    if doc is None:
        return None
    if "b" in doc:
        return _tok_bytes(bytes.fromhex(doc["b"]), dirs, False)
    return _detoken(doc["s"], dirs, False)


def _restore(entry: Path, meta, dirs, argv, kw):
    for f in meta["files"]:
        target = Path(dirs[f["dir"]], f["name"])
        if f.get("gone"):
            target.unlink(missing_ok=True)
            continue
        data = _tok_bytes((entry / f["blob"]).read_bytes(), dirs, False)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return subprocess.CompletedProcess(list(argv), meta["returncode"],
                                       _untok_std(meta["stdout"], dirs), _untok_std(meta["stderr"], dirs))


def _run(*args, **kw):
    argv = args[0] if args else kw.get("args")
    if len(args) > 1 or not _eligible(argv, kw):
        return _REAL_RUN(*args, **kw)
    kw = {k: v for k, v in kw.items() if k != "args"}
    cwd = kw.get("cwd")
    dirs = _watch_dirs([str(x) for x in argv], str(cwd) if cwd else None)
    if dirs is None:
        return _REAL_RUN(argv, **kw)
    before = [snapshot(d) for d in dirs]
    if any(s is None for s in before):
        return _REAL_RUN(argv, **kw)
    key = _key(argv, kw, dirs, before)
    entry = _store_dir() / key
    if (entry / "meta.json").is_file():
        meta = json.loads((entry / "meta.json").read_text(encoding="utf-8"))
        with _lock:
            _stats["hit"] += 1
        got = _restore(entry, meta, dirs, argv, kw)
        if os.environ.get(ENV_VERIFY) == "1":
            _verify(argv, kw, dirs, got, meta)
        return got
    proc = _REAL_RUN(argv, **kw)
    with _lock:
        _stats["miss"] += 1
    if 0 <= proc.returncode <= 255 and _stats['stored'] < MAX_STORE_BYTES:
        after = [snapshot(d) for d in dirs]
        if all(s is not None for s in after):
            try:
                _record(entry, proc, dirs, before, after)
            except OSError:
                pass
    return proc


_VOLATILE = re.compile(r'\(date "[^"]*"\)|"date": "[^"]*"|%TF\.CreationDate[^%]*%|G04 Created by[^\r\n]*')


def _verify(argv, kw, dirs, replayed, meta) -> None:
    """The replay has just written its files; run the real thing on top and compare."""
    wrote = {(f["dir"], f["name"]): Path(dirs[f["dir"]], f["name"]).read_bytes()
             for f in meta["files"] if not f.get("gone")}
    real = _REAL_RUN(argv, **kw)
    problems = []
    if real.returncode != replayed.returncode:
        problems.append(f"returncode {real.returncode} != replayed {replayed.returncode}")
    for what in ("stdout", "stderr"):
        a, b = getattr(real, what), getattr(replayed, what)
        if _VOLATILE.sub("", str(a)) != _VOLATILE.sub("", str(b)):
            problems.append(f"{what} differs")
    for (i, name), old in wrote.items():
        p = Path(dirs[i], name)
        new = p.read_bytes() if p.exists() else b"<missing>"
        if _VOLATILE.sub("", new.decode("utf-8", "replace")) != _VOLATILE.sub("", old.decode("utf-8", "replace")):
            problems.append(f"file {name} differs")
    if problems:
        raise ReplayMismatch(f"replay of {' '.join(map(str, argv[1:4]))} differs from the real run: {problems}")


def install() -> None:
    subprocess.run = _run


def uninstall() -> None:
    subprocess.run = _REAL_RUN


def stats() -> dict:
    return dict(_stats)


# --------------------------------------------------------------------------- pytest hooks (re-exported by conftest)


def _enabled() -> bool:
    return os.environ.get(ENV_ON) == "1"


def pytest_configure(config) -> None:
    """Install the wrapper when ``KICAD_MCP_CLI_REPLAY=1``. The controller (or a serial run) makes the store
    directory and names it in the environment before any xdist worker is started, so workers share it."""
    if not _enabled():
        return
    if not hasattr(config, "workerinput") and not os.environ.get(ENV_DIR):
        os.environ[ENV_DIR] = tempfile.mkdtemp(prefix="kicad-mcp-cli-replay-")
        config._cli_replay_owns_store = True
    install()


def pytest_runtest_setup(item) -> None:
    global paused
    paused = item.module.__name__.rsplit(".", 1)[-1].startswith(EXCLUDED_FILES)


def pytest_sessionfinish(session) -> None:
    if _enabled() and os.environ.get(ENV_DIR):
        try:
            Path(os.environ[ENV_DIR], f"stats-{os.getpid()}.json").write_text(json.dumps(_stats), encoding="utf-8")
        except OSError:
            pass


def pytest_terminal_summary(terminalreporter) -> None:
    if not _enabled() or not os.environ.get(ENV_DIR):
        return
    total = {"hit": 0, "miss": 0}
    for f in Path(os.environ[ENV_DIR]).glob("stats-*.json"):
        try:
            got = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        total["hit"] += got.get("hit", 0)
        total["miss"] += got.get("miss", 0)
    terminalreporter.write_line(f"kicad-cli replay: {total['hit']} replayed, {total['miss']} run for real")


def pytest_unconfigure(config) -> None:
    if getattr(config, "_cli_replay_owns_store", False):
        import shutil

        shutil.rmtree(os.environ.get(ENV_DIR, ""), ignore_errors=True)
        uninstall()
