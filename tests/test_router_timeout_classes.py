"""Ticket 16: a router timeout raises a dedicated subclass; nothing else does, even when the
failing output contains the words "timed out"."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from kicad_mcp.routers import kicad_routing_tools as krt_tools
from kicad_mcp.toolsets import routing as routing_toolset


class _FakePopen:
    """Writes a fixed line to the router's stdout log, then 'exits' or 'hangs'."""

    def __init__(self, line: str, returncode: int | None):
        self.line, self.returncode_value = line, returncode

    def __call__(self, args, cwd=None, stdout=None, stderr=None, env=None):
        stdout.write(self.line)
        stdout.flush()
        return self

    def wait(self, timeout=None):
        if self.returncode_value is None:
            raise subprocess.TimeoutExpired("router", timeout)
        return self.returncode_value

    def kill(self):
        pass


def _run(tmp_path, monkeypatch, line, returncode, timeout=5):
    project = tmp_path / "in"
    project.mkdir()
    pcb = project / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    monkeypatch.setattr(krt_tools, "find_kicad_routing_tools", lambda: tmp_path)
    monkeypatch.setattr(krt_tools, "find_kicad_routing_tools_python", lambda: Path("python"))
    monkeypatch.setattr(krt_tools.subprocess, "Popen", _FakePopen(line, returncode))
    return krt_tools.run_router_script(
        "route.py", [], output_pcb=tmp_path / "out" / "b.kicad_pcb", original_pcb=pcb, timeout=timeout
    )


def test_a_killed_router_raises_the_timeout_subclass_with_its_numbers(tmp_path, monkeypatch):
    with pytest.raises(krt_tools.KicadRoutingToolsTimeout) as caught:
        _run(tmp_path, monkeypatch, "routing...\n", None, timeout=5)
    error = caught.value
    assert isinstance(error, krt_tools.KicadRoutingToolsError)
    assert error.timeout == 5
    assert error.elapsed_s >= 0
    assert error.project_restored is True  # same disclosure fields as every other failure


def test_a_failure_whose_output_says_timed_out_is_not_a_timeout(tmp_path, monkeypatch):
    with pytest.raises(krt_tools.KicadRoutingToolsError) as caught:
        _run(tmp_path, monkeypatch, "connection timed out after 3 retries\n", 1)
    assert not isinstance(caught.value, krt_tools.KicadRoutingToolsTimeout)


def test_a_freerouting_timeout_raises_its_subclass(tmp_path, monkeypatch):
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)

    def hang(args, **kwargs):
        raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

    monkeypatch.setattr(routing_toolset.subprocess, "run", hang)
    with pytest.raises(routing_toolset.FreeroutingTimeout) as caught:
        routing_toolset.run_freerouting(tmp_path / "a.dsn", tmp_path / "a.ses", timeout=4)
    assert isinstance(caught.value, routing_toolset.FreeroutingError)
    assert caught.value.timeout == 4 and caught.value.elapsed_s >= 0


def test_a_freerouting_failure_that_mentions_timed_out_is_not_a_timeout(tmp_path, monkeypatch):
    jar = tmp_path / "f.jar"
    jar.write_bytes(b"x")
    monkeypatch.setattr(routing_toolset, "find_java", lambda: Path("java"))
    monkeypatch.setattr(routing_toolset, "find_freerouting_jar", lambda: jar)
    monkeypatch.setattr(
        routing_toolset.subprocess, "run",
        lambda args, **kw: subprocess.CompletedProcess(args, 1, b"request timed out after 3 tries", b""),
    )
    with pytest.raises(routing_toolset.FreeroutingError) as caught:
        routing_toolset.run_freerouting(tmp_path / "a.dsn", tmp_path / "a.ses", timeout=4)
    assert not isinstance(caught.value, routing_toolset.FreeroutingTimeout)
