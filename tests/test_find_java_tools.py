"""Issue 234: java_path reaches the run, and the answer names where java was found, in both tools."""

import shutil
import subprocess
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from boardproof.toolsets import routing as routing_toolset
from boardproof.toolsets.routing import FreeroutingError

FIXTURES = Path(__file__).parent / "fixtures"
WIN = sys.platform == "win32"


def _fake_java(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    if WIN:
        path = folder / "java.cmd"
        path.write_text('@echo off\r\necho openjdk version "25.0.5" 1>&2\r\n')
    else:
        path = folder / "java"
        path.write_text('#!/bin/sh\necho \'openjdk version "25.0.5"\' >&2\n')
        path.chmod(0o755)
    return path


def _tools() -> dict:
    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _board(tmp_path: Path) -> Path:
    proj = tmp_path / "proj"
    proj.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl", ".kicad_sch"):
        shutil.copy(FIXTURES / f"simple_led{suffix}", proj / f"simple_led{suffix}")
    return proj / "simple_led.kicad_pcb"


def _stub_export(monkeypatch, tmp_path):
    monkeypatch.setattr(routing_toolset, "export_dsn_with_kicad",
                        lambda *_a, **_k: {"exporter": "t", "dsn_path": tmp_path / "a.dsn"})


def test_freerouting_tool_passes_java_path_and_answers_where_java_was_found(tmp_path, monkeypatch):
    java = _fake_java(tmp_path / "fj")
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    real_run = subprocess.run
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)
    _stub_export(monkeypatch, tmp_path)
    monkeypatch.setattr(routing_toolset, "import_ses_with_kicad", lambda *_a, **_k: {
        "import": {}, "trace_specs": [], "via_specs": [], "expected_copper": None})
    monkeypatch.setattr(routing_toolset, "_register_proposal", lambda *_a, **_k: "tok")

    def fake_run(args, **kw):
        if list(args)[-1] == "-version":
            return real_run(args, **kw)
        Path(args[args.index("-do") + 1]).write_text("(session)")
        return subprocess.CompletedProcess(args, 0, b"final score: 1.0 (0 unrouted and 0 violations)", b"")

    monkeypatch.setattr(routing_toolset.subprocess, "run", fake_run)
    result = _tools()["propose_route_with_freerouting"](str(_board(tmp_path)), java_path=str(java))
    assert result["status"] == "pending_confirmation"
    assert result["freerouting_log"]["java"] == {"path": str(java), "source": "java_path argument", "major_version": 25}


def test_retry_tool_passes_java_path_to_run_freerouting(tmp_path, monkeypatch):
    seen = {}

    def fake_run_freerouting(*_a, **kw):
        seen.update(kw)
        raise FreeroutingError("stop here")

    _stub_export(monkeypatch, tmp_path)
    monkeypatch.setattr(routing_toolset, "run_freerouting", fake_run_freerouting)
    _tools()["propose_route_with_retry"](str(_board(tmp_path)), max_attempts=1, java_path="J")
    assert seen["java_path"] == "J"
