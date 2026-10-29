"""Freerouting leaves nothing behind outside its own temp directory (build ticket 33).

Freerouting 2.5.0 reads ``-l <x>`` as a log location too: a run from the caller's
directory created ``<cwd>/en/freerouting.log`` and saved that path into
``%APPDATA%/freerouting/freerouting.json`` (paradigm-review-2 ticket 01). The availability
probe decoded the JVM's output with the locale codec, so under ``PYTHONUTF8=1`` the German
cp1252 ``-help`` text raised in the reader thread, the output was lost and the probe still
reported "available".

The real-Freerouting tests point ``APPDATA``, ``LOCALAPPDATA``, ``TMP``/``TEMP`` and the
working directory at empty temp directories, so a regression shows up as a file there and
never touches the user's own Freerouting settings. The availability tests run a child
Python under ``PYTHONUTF8=1`` against a fake ``java`` that prints cp1252 bytes.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from kicad_mcp import rung_backends
from kicad_mcp.toolsets import routing as routing_toolset

FIXTURE = Path(__file__).parent / "fixtures" / "simple_led.kicad_pcb"
SRC = Path(rung_backends.__file__).resolve().parents[1]


def _freerouting_available() -> bool:
    try:
        routing_toolset.find_java()
        routing_toolset.find_freerouting_jar()
    except routing_toolset.FreeroutingError:
        return False
    return True


needs_freerouting = pytest.mark.skipif(
    not _freerouting_available(), reason="FREEROUTING_JAR_PATH/java not available in this environment")


def _files_with_bytes(root: Path) -> dict[str, bytes]:
    return {str(p.relative_to(root)): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


@pytest.fixture(scope="module")
def board_dir(tmp_path_factory) -> Path:
    """A copy of ``FIXTURE``'s project: the export never runs on ``tests/fixtures/`` itself
    (kicad-tools 0.24.0's exporter wrote a sidecar beside the board; build ticket 71)."""
    proj = tmp_path_factory.mktemp("proj")
    for sibling in FIXTURE.parent.glob(f"{FIXTURE.stem}.*"):
        shutil.copy(sibling, proj / sibling.name)
    return proj


@pytest.fixture(scope="module")
def board_dir_before(board_dir) -> dict[str, bytes]:
    return _files_with_bytes(board_dir)


@pytest.fixture(scope="module")
def fixture_dsn(tmp_path_factory, board_dir, board_dir_before) -> Path:
    """KiCad's DSN of the copied ``FIXTURE`` (build ticket 46), exported once before any test
    redirects TEMP: KiCad's runner writes only inside the real temp directory."""
    export = routing_toolset.export_dsn_with_kicad(board_dir / FIXTURE.name, tmp_path_factory.mktemp("kicad_dsn"))
    return Path(export["dsn_path"])


def _kicad_dsn(fixture_dsn: Path, dsn: Path) -> None:
    shutil.copy(fixture_dsn, dsn)


def _files_under(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


@pytest.fixture
def watched(tmp_path, monkeypatch):
    """Empty stand-ins for every place a Freerouting run could write to by default."""
    dirs = {name: tmp_path / name for name in ("cwd", "appdata", "localappdata", "temp")}
    for d in dirs.values():
        d.mkdir()
    monkeypatch.setenv("APPDATA", str(dirs["appdata"]))
    monkeypatch.setenv("LOCALAPPDATA", str(dirs["localappdata"]))
    monkeypatch.setenv("TMP", str(dirs["temp"]))
    monkeypatch.setenv("TEMP", str(dirs["temp"]))
    monkeypatch.setenv("TMPDIR", str(dirs["temp"]))
    monkeypatch.setattr(tempfile, "tempdir", str(dirs["temp"]))
    monkeypatch.chdir(dirs["cwd"])
    return dirs


# --- the real tool -------------------------------------------------------------------------------


@needs_freerouting
def test_a_real_freerouting_run_leaves_no_file_outside_its_temp_dir(
        tmp_path, fixture_dsn, watched, board_dir, board_dir_before):
    work = tmp_path / "work"
    work.mkdir()
    dsn, ses = work / "board.dsn", work / "board.ses"
    _kicad_dsn(fixture_dsn, dsn)

    log = routing_toolset.run_freerouting(dsn, ses, max_passes=2, timeout=240)

    assert ses.is_file()
    assert log["self_reported_score"] is not None, log  # the console log was still read and parsed
    for name, d in watched.items():
        assert _files_under(d) == [], f"Freerouting wrote into {name}: {_files_under(d)}"
    assert _files_under(work) == ["board.dsn", "board.ses"]
    # The board's own directory: the export and the run left it as it was (build ticket 71).
    assert _files_with_bytes(board_dir) == board_dir_before


@needs_freerouting
def test_relative_dsn_and_ses_paths_still_resolve_against_the_callers_directory(fixture_dsn, watched):
    _kicad_dsn(fixture_dsn, watched["cwd"] / "board.dsn")

    routing_toolset.run_freerouting("board.dsn", "out/board.ses", max_passes=1, timeout=240)

    assert (watched["cwd"] / "out" / "board.ses").is_file()
    assert _files_under(watched["cwd"]) == ["board.dsn", "out", str(Path("out") / "board.ses")]


@needs_freerouting
def test_the_real_availability_probe_reads_the_version_and_leaves_nothing_behind(watched):
    result = rung_backends.freerouting_availability()

    assert result["available"] is True, result
    assert result["versions"]["freerouting"], result
    for name, d in watched.items():
        assert _files_under(d) == [], f"the probe wrote into {name}: {_files_under(d)}"


# --- the subprocess boundary ---------------------------------------------------------------------


def test_run_freerouting_runs_in_a_throwaway_dir_that_is_also_its_user_data_path(tmp_path, monkeypatch):
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)
    monkeypatch.chdir(tmp_path)
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["kwargs"] = list(args), kwargs
        seen["cwd_existed"] = Path(kwargs["cwd"]).is_dir()
        Path(args[args.index("-do") + 1]).write_text("(session)")
        return subprocess.CompletedProcess(args, 0, b"final score: 1.0 (0 unrouted and 0 violations)", b"")

    monkeypatch.setattr(routing_toolset.subprocess, "run", fake_run)
    log = routing_toolset.run_freerouting("a.dsn", "a.ses", timeout=4)

    sandbox = Path(seen["kwargs"]["cwd"])
    assert seen["cwd_existed"] and sandbox.resolve() != tmp_path.resolve()
    assert f"--user_data_path={sandbox}" in seen["args"]
    assert "-l" not in seen["args"]  # build ticket 46: -l made Freerouting create <cwd>/en/
    assert not sandbox.exists()  # removed after the run
    assert seen["args"][seen["args"].index("-de") + 1] == str(tmp_path.resolve() / "a.dsn")
    assert seen["args"][seen["args"].index("-do") + 1] == str(tmp_path.resolve() / "a.ses")
    assert log["self_reported_score"] == 1.0


def test_run_freerouting_reads_non_utf8_output_without_losing_it(tmp_path, monkeypatch):
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)

    def fake_run(args, **kwargs):
        Path(args[args.index("-do") + 1]).write_text("(session)")
        out = "Durchläufe\nfinal score: 2.5 (1 unrouted and 3 violations)\n".encode("cp1252")
        return subprocess.CompletedProcess(args, 0, out, b"")

    monkeypatch.setattr(routing_toolset.subprocess, "run", fake_run)
    log = routing_toolset.run_freerouting(tmp_path / "a.dsn", tmp_path / "a.ses", timeout=4)
    assert (log["self_reported_score"], log["self_reported_unrouted"]) == (2.5, 1)


# --- availability under PYTHONUTF8=1, against a fake java printing cp1252 bytes -------------------

_FAKE_JAVA = '''\
import sys
out = {stdout!r}
err = {stderr!r}
sys.stdout.buffer.write(out)
sys.stderr.buffer.write(err)
sys.exit({code})
'''

_PROBE = '''\
import json
from kicad_mcp import rung_backends
print("RESULT " + json.dumps(rung_backends.freerouting_availability()))
'''

_GERMAN_HELP = "VERWENDUNG\n\n-mp <Anzahl_der_Durchläufe>\n  Maximale Anzahl der auszuführenden Durchläufe\n"


def _fake_java(tmp_path: Path, stdout: bytes, stderr: bytes = b"", code: int = 0) -> Path:
    script = tmp_path / "fake_java.py"
    script.write_text(_FAKE_JAVA.format(stdout=stdout, stderr=stderr, code=code), encoding="utf-8")
    if os.name == "nt":
        launcher = tmp_path / "java.bat"
        launcher.write_text(f'@"{sys.executable}" "{script}" %*\r\n', encoding="ascii")
    else:
        launcher = tmp_path / "java"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n', encoding="ascii")
        launcher.chmod(0o755)
    return launcher


def _availability_under_utf8_mode(tmp_path: Path, java: Path) -> dict:
    jar = tmp_path / "freerouting.jar"
    jar.write_bytes(b"jar bytes")
    env = {**os.environ, "PYTHONUTF8": "1", "JAVA_PATH": str(java), "FREEROUTING_JAR_PATH": str(jar),
           "PYTHONPATH": os.pathsep.join([str(SRC), os.environ.get("PYTHONPATH", "")])}
    proc = subprocess.run([sys.executable, "-c", _PROBE], capture_output=True, env=env, timeout=120,
                          cwd=tmp_path, check=False)
    text = proc.stdout.decode("utf-8", "replace")
    lines = [line for line in text.splitlines() if line.startswith("RESULT ")]
    assert lines, (proc.returncode, text, proc.stderr.decode("utf-8", "replace"))
    return json.loads(lines[-1][len("RESULT "):])


def test_availability_under_utf8_mode_reads_the_version_through_cp1252_help_text(tmp_path):
    out = ("2026-10-19 19:28:46.079 INFO   Freerouting v2.5.0 (build-date: 2026-10-05)\n"
           + _GERMAN_HELP).encode("cp1252")
    result = _availability_under_utf8_mode(tmp_path, _fake_java(tmp_path, out))
    assert result["available"] is True, result
    assert result["versions"]["freerouting"] == "2.5.0"


def test_availability_under_utf8_mode_still_sees_a_jvm_failure_beside_cp1252_bytes(tmp_path):
    err = ("Fehler: Hauptklasse konnte nicht geladen werden (Größe)\n"
           "java.lang.UnsupportedClassVersionError: class file version 69\n").encode("cp1252")
    result = _availability_under_utf8_mode(tmp_path, _fake_java(tmp_path, _GERMAN_HELP.encode("cp1252"), err, 1))
    assert result["available"] is False, result
    assert "UnsupportedClassVersionError" in result["reason"]


def test_availability_is_unavailable_when_the_version_cannot_be_read(tmp_path):
    result = _availability_under_utf8_mode(tmp_path, _fake_java(tmp_path, _GERMAN_HELP.encode("cp1252")))
    assert result["available"] is False, result
    assert "version" in result["reason"]


_BANNER = b"2026-10-19 19:28:46.079 INFO   Freerouting v2.5.0 (build-date: 2026-10-05)\n"


@pytest.mark.parametrize("err, code", [
    (b"Exception in thread \"main\" java.lang.NoClassDefFoundError: app/freerouting/gui/Foo\n", 1),
    (b"Exception in thread \"main\" java.lang.OutOfMemoryError: Java heap space\n", 3),
])
def test_availability_is_unavailable_when_help_prints_the_banner_but_exits_nonzero(tmp_path, err, code):
    # The banner comes first, before -help is handled; a JVM that dies after it is not usable.
    result = _availability_under_utf8_mode(tmp_path, _fake_java(tmp_path, _BANNER + _GERMAN_HELP.encode("cp1252"),
                                                                 err, code))
    assert result["available"] is False, result
    assert f"exit {code}" in result["reason"]
    assert err.decode().split(":")[0].rsplit(".", 1)[-1] in result["reason"]  # the excerpt names the error


# --- the probe at its subprocess boundary, in process ---------------------------------------------


def _probe_with(monkeypatch, tmp_path, stdout: bytes, stderr: bytes = b"", code: int = 0) -> tuple[dict, dict]:
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["kwargs"] = list(args), kwargs
        seen["cwd_existed"] = Path(kwargs["cwd"]).is_dir()
        return subprocess.CompletedProcess(args, code, stdout, stderr)

    monkeypatch.setattr(rung_backends.subprocess, "run", fake_run)
    # KiCad's bundled Python (build ticket 46) is probed on its own; only the JVM probe is faked here.
    monkeypatch.setattr(rung_backends, "_kicad_specctra_problem",
                        lambda: (None, {"available": True, "python": "kicad", "pcbnew_version": "10"}))
    return rung_backends.freerouting_availability(), seen


def test_the_probe_runs_confined_to_a_sandbox_it_removes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result, seen = _probe_with(monkeypatch, tmp_path, _BANNER)
    assert result["available"] is True and result["versions"]["freerouting"] == "2.5.0"
    sandbox = Path(seen["kwargs"]["cwd"])
    assert seen["cwd_existed"] and sandbox.resolve() != tmp_path.resolve()
    args = seen["args"]
    for flag in (f"--user_data_path={sandbox}", f"-Djava.io.tmpdir={sandbox}", "-XX:-UsePerfData",
                 "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8"):
        assert flag in args, (flag, args)
    assert args.index("-Dstdout.encoding=UTF-8") < args.index("-jar") < args.index(f"--user_data_path={sandbox}")
    assert args[-1] == "-help"
    assert seen["kwargs"].get("text") is None and seen["kwargs"].get("encoding") is None  # bytes, decoded by us
    assert not sandbox.exists()


def test_run_freerouting_passes_the_same_jvm_confinement_flags(tmp_path, monkeypatch):
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"], seen["kwargs"] = list(args), kwargs
        Path(args[args.index("-do") + 1]).write_text("(session)")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(routing_toolset.subprocess, "run", fake_run)
    routing_toolset.run_freerouting(tmp_path / "a.dsn", tmp_path / "a.ses", timeout=4)
    sandbox = Path(seen["kwargs"]["cwd"])
    for flag in (f"-Djava.io.tmpdir={sandbox}", "-XX:-UsePerfData", "-Dstdout.encoding=UTF-8",
                 "-Dstderr.encoding=UTF-8"):
        assert flag in seen["args"], (flag, seen["args"])
    assert seen["kwargs"].get("text") is None and seen["kwargs"].get("encoding") is None


@pytest.mark.parametrize("marker", [
    "UnsupportedClassVersionError", "Unable to access jarfile", "Error: Invalid or corrupt jarfile",
])
def test_every_jvm_failure_marker_is_unavailable_even_beside_a_banner(tmp_path, monkeypatch, marker):
    result, _ = _probe_with(monkeypatch, tmp_path, _BANNER, f"{marker} x\n".encode(), 0)
    assert result["available"] is False, result
    assert marker in result["reason"]


# --- build ticket 117: no Freerouting window -----------------------------------------------------
# Freerouting 2.5.0 opens its main window (and an "Autorouter" confirmation dialog) for every run
# unless `gui.enabled` is false; `--gui.enabled=false` is the command-line override that works
# (measured per process id: a window title with it absent, none with it present).

NO_GUI = "--gui.enabled=false"


def test_the_command_builder_always_passes_the_no_gui_override(tmp_path):
    args = routing_toolset.freerouting_command(Path("java"), Path("f.jar"), tmp_path, "-help")
    assert args.count(NO_GUI) == 1
    assert args.index("-jar") < args.index(NO_GUI) < args.index("-help")


def test_the_no_gui_override_is_not_a_lever(tmp_path):
    for kwargs in ({}, {"fanout_enabled": False}, {"neck_width_um": 100}, {"fanout_enabled": False, "neck_width_um": 5}):
        assert NO_GUI not in routing_toolset.freerouting_router_options(**kwargs)
    with pytest.raises(routing_toolset.FreeroutingError):
        routing_toolset.freerouting_router_options(fanout_enabled=NO_GUI)


def test_run_freerouting_and_the_probe_both_pass_the_no_gui_override(tmp_path, monkeypatch):
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)
    seen = {}

    def fake_run(args, **kwargs):
        seen["args"] = list(args)
        Path(args[args.index("-do") + 1]).write_text("(session)")
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(routing_toolset.subprocess, "run", fake_run)
    routing_toolset.run_freerouting(tmp_path / "a.dsn", tmp_path / "a.ses", timeout=4)
    assert seen["args"].count(NO_GUI) == 1
    _result, probe_seen = _probe_with(monkeypatch, tmp_path, _BANNER)
    assert probe_seen["args"].count(NO_GUI) == 1


def _main_window_titles(pid: int) -> str:
    out = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"$p = Get-Process -Id {pid} -ErrorAction SilentlyContinue; "
         "if ($p) { '{0}|{1}' -f $p.MainWindowHandle, $p.MainWindowTitle }"],
        capture_output=True, check=False, timeout=30)
    return decode_jvm_text(out.stdout)


def decode_jvm_text(data: bytes) -> str:
    return routing_toolset.decode_jvm_output(data).strip()


@needs_freerouting
@pytest.mark.skipif(sys.platform != "win32", reason="the window is measured per process id with PowerShell")
def test_a_real_freerouting_run_opens_no_window(tmp_path, fixture_dsn):
    """Started from here, so the only process id measured and stopped is this one."""
    dsn, ses = tmp_path / "board.dsn", tmp_path / "board.ses"
    _kicad_dsn(fixture_dsn, dsn)
    with routing_toolset.freerouting_sandbox() as sandbox:
        cmd = routing_toolset.freerouting_command(
            routing_toolset.find_java(), routing_toolset.find_freerouting_jar(), sandbox,
            "-de", str(dsn), "-do", str(ses), "-mp", "2", "-mt", "1")
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, cwd=sandbox)
        seen: set[str] = set()
        try:
            deadline = time.monotonic() + 120
            while proc.poll() is None and time.monotonic() < deadline:
                row = _main_window_titles(proc.pid)
                if row:
                    seen.add(row)
                time.sleep(0.5)
        finally:
            if proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
            proc.communicate()
    assert ses.exists()
    windows = {row for row in seen if row.split("|", 1)[0] not in ("", "0")}
    assert not windows, windows
