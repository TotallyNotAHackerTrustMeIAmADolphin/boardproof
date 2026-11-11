"""Two tool calls writing one KiCad file at once are serialised inside the server (GitHub issue 87).

Real fixture: a project made by ``project_create``, a sheet built through the tools. The two calls
run in two threads through the same wrapper the server registers every tool with
(``attach_gate_field``); a barrier starts them together.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from mcp.server.mcpserver import MCPServer

import pytest

from boardproof import gate_state, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset


def _wrapped_tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    state = gate_state.GateState()
    return {
        t.name: gate_state.attach_gate_field(t.fn, t.annotations, state)
        for t in mcp._tool_manager.list_tools()  # type: ignore[attr-defined]
    }


def _sheet(tmp_path: Path, tools: dict) -> Path:
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    assert made["status"] == "created", made
    sch = Path(made["schematic_path"])
    first = tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])
    assert first["status"] == "ok", first
    return sch


def _together(*calls) -> list:
    barrier = threading.Barrier(len(calls))
    results: list = [None] * len(calls)

    def run(i, call):
        barrier.wait()
        try:
            results[i] = call()
        except BaseException as exc:  # noqa: BLE001 - the test reports it
            results[i] = exc

    threads = [threading.Thread(target=run, args=(i, c)) for i, c in enumerate(calls)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=300)
    return results


def test_concurrent_add_symbols_and_add_wires_both_land(tmp_path: Path) -> None:
    tools = _wrapped_tools()
    sch = _sheet(tmp_path, tools)

    symbols, wires = _together(
        lambda: tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"}]),
        lambda: tools["add_wires"](str(sch), [{"p1": [20, 20], "p2": [30, 20]}]),
    )

    assert not isinstance(symbols, BaseException), symbols
    assert not isinstance(wires, BaseException), wires
    assert symbols["status"] == "ok", symbols
    assert wires["status"] == "ok", wires
    text = sch.read_text(encoding="utf-8")
    assert '"R2"' in text and '"R1"' in text, "a symbol was lost"
    assert "(wire" in text, "the wire was lost (lost update)"


def test_path_lock_is_per_normalised_path_and_reentrant(tmp_path: Path) -> None:
    a = tmp_path / "a.kicad_sch"
    with write_layer.path_lock([a]), write_layer.path_lock([tmp_path / "x" / ".." / "a.kicad_sch"]):
        taken = threading.Event()

        def other() -> None:
            with write_layer.path_lock([tmp_path / "b.kicad_sch"]):
                taken.set()

        t = threading.Thread(target=other)
        t.start()
        t.join(timeout=5)
        assert taken.is_set(), "a lock on one file blocked another file"


def test_a_persistent_sharing_denial_becomes_a_file_busy_refusal_and_changes_nothing(tmp_path: Path, monkeypatch) -> None:
    tools = _wrapped_tools()
    sch = _sheet(tmp_path, tools)
    before = sch.read_bytes()

    def denied(*_a, **_k):
        exc = PermissionError(13, "Zugriff verweigert")
        exc.winerror = 5
        raise exc

    monkeypatch.setattr(schematic_toolset.os, "replace", denied)
    result = tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"}])

    assert result["ok"] is False and result["code"] == "file_busy", result
    assert "nothing changed" in result["reason"]
    assert sch.read_bytes() == before
    assert not list(sch.parent.glob("*.tmp"))


def test_a_permission_error_that_is_not_a_sharing_denial_is_not_called_busy(tmp_path: Path, monkeypatch) -> None:
    tools = _wrapped_tools()
    sch = _sheet(tmp_path, tools)
    before = sch.read_bytes()

    def denied(*_a, **_k):
        raise PermissionError(13, "read-only")

    monkeypatch.setattr(schematic_toolset.os, "replace", denied)
    result = tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"}])

    assert result["status"] != "ok" and result.get("code") != "file_busy", result
    assert sch.read_bytes() == before


_ADD_R2 = [{"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"}]


def _denial() -> PermissionError:
    exc = PermissionError(13, "Zugriff verweigert")
    exc.winerror = 5
    return exc


def test_replace_file_rides_out_two_sharing_denials(tmp_path: Path, monkeypatch) -> None:
    target, temp = tmp_path / "a.txt", tmp_path / "a.tmp"
    target.write_text("old")
    temp.write_text("new")
    real, calls = write_layer.os.replace, []

    def flaky(src, dst):
        calls.append(1)
        if len(calls) <= 2:
            raise _denial()
        return real(src, dst)

    monkeypatch.setattr(write_layer.os, "replace", flaky)
    write_layer.replace_file(temp, target)
    assert len(calls) == 3 and target.read_text() == "new"


def test_replace_file_denial_past_the_budget_is_file_busy_and_untouched(tmp_path: Path, monkeypatch) -> None:
    target, temp = tmp_path / "a.txt", tmp_path / "a.tmp"
    target.write_text("old")
    temp.write_text("new")
    clock = iter(range(0, 10_000, 1))
    monkeypatch.setattr(write_layer.time, "monotonic", lambda: next(clock))
    monkeypatch.setattr(write_layer.time, "sleep", lambda _s: None)

    def denied(*_a):
        raise _denial()

    monkeypatch.setattr(write_layer.os, "replace", denied)
    with pytest.raises(write_layer.WriteLayerError) as caught:
        write_layer.replace_file(temp, target)
    assert caught.value.code == "file_busy"
    assert target.read_text() == "old"


def test_a_second_call_on_a_held_file_is_refused_after_the_timeout(tmp_path: Path, monkeypatch) -> None:
    tools = _wrapped_tools()
    sch = _sheet(tmp_path, tools)
    before = sch.read_bytes()
    monkeypatch.setattr(write_layer, "LOCK_TIMEOUT_S", 0.3)
    held, release = threading.Event(), threading.Event()

    def holder() -> None:
        with write_layer.path_lock([sch]):
            held.set()
            release.wait(30)

    a = threading.Thread(target=holder)
    a.start()
    assert held.wait(5)
    try:
        started = time.monotonic()
        result = tools["add_symbols"](str(sch), _ADD_R2)
        assert time.monotonic() - started < 20
    finally:
        release.set()
        a.join(10)
    assert result["ok"] is False and result["code"] == "file_busy", result
    assert "another call of this server" in result["reason"] and sch.name in result["reason"]
    assert "never started" in result["reason"]
    assert sch.read_bytes() == before
    again = tools["add_symbols"](str(sch), _ADD_R2)  # A released: the lock works again
    assert again["status"] == "ok", again


def test_a_tool_that_wrote_one_file_then_was_denied_on_another_does_not_claim_nothing_changed(tmp_path: Path) -> None:
    def two_files(path: str) -> dict:
        Path(path).write_text("A written")
        raise _denial()

    mcp = MCPServer("test")
    mcp.add_tool(two_files)
    fn = gate_state.attach_gate_field(mcp._tool_manager.list_tools()[0].fn, {}, gate_state.GateState())  # type: ignore[attr-defined]
    target = tmp_path / "a.txt"
    result = fn(path=str(target))
    assert result["ok"] is False and result["code"] == "file_busy", result
    assert "nothing changed" not in result["reason"]
    assert "may have changed" in result["reason"]


def test_concurrent_connect_pins_and_add_wires_both_land(tmp_path: Path) -> None:
    tools = _wrapped_tools()
    sch = _sheet(tmp_path, tools)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "value": "1k"}])["status"] == "ok"

    pins, wires = _together(
        lambda: tools["connect_pins"](str(sch), [{"from": "R1.1", "to": "R2.1"}]),
        lambda: tools["add_wires"](str(sch), [{"p1": [20.32, 20.32], "p2": [30.48, 20.32], "route": "direct"}]),
    )

    assert not isinstance(pins, BaseException), pins
    assert not isinstance(wires, BaseException), wires
    assert pins["ok"] is True and pins["written"] is True, pins
    assert wires["status"] == "ok", wires
    root = write_layer.load(sch)
    ends = [e for w in root.children if not w.is_atom and w.name == "wire" for e in [tuple(xy.get_float(i) for i in (0, 1)) for xy in w.find_children("pts")[0].find_children("xy")]]
    assert (20.32, 20.32) in ends and (50.8, 46.99) in ends and (76.2, 46.99) in ends, "a wire was lost (lost update)"
