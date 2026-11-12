"""The local, anonymous usage log (GitHub issue 45): one wrapper around tool dispatch.

What is pinned: every registered tool produces a record; no argument value reaches the file; a log
that cannot be written changes nothing for the tool call; the tool schemas an LLM reads are
byte-identical with the wrapper installed; rotation and the size cap; the module imports no
networking module; the suite never writes the real user data directory (also not from a subprocess).
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer

from boardproof import usage_log
from boardproof.server import build_server
from boardproof.usage_log import UsageLog

SRC = Path(usage_log.__file__)
SRC_DIR = SRC.parent.parent


def _lines(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x]


def _server_with_log(monkeypatch, path: Path) -> MCPServer:
    monkeypatch.setenv(usage_log.ENV_VAR, str(path))
    return build_server()


async def _load_all(client) -> None:
    for ts in (await client.call_tool("list_toolsets", {})).structured_content["result"]:
        await client.call_tool("load_toolset", {"name": ts["name"]})


# -- every tool produces a record, nothing but the allowed fields -------------------------------


def test_every_registered_tool_produces_one_record(tmp_path, monkeypatch):
    log_path = tmp_path / "usage.jsonl"
    server = _server_with_log(monkeypatch, log_path)

    async def run():
        async with Client(server, mode="auto") as client:
            await _load_all(client)
            tools = server._tool_manager.list_tools()
            for tool in tools:
                if not tool.parameters.get("required"):
                    # A tool without required fields would run for real: stand in for its body only.
                    tool.fn = lambda: {"ok": True}
                    tool.is_async = False
            log_path.unlink()
            for tool in tools:
                await client.call_tool(tool.name, {})
            return [t.name for t in tools]

    names = asyncio.run(run())
    rows = _lines(log_path)
    assert len(names) > 50
    assert sorted(r["tool"] for r in rows) == sorted(names)
    assert all(set(r) == {"t", "tool", "toolset", "ms", "outcome", "session"} for r in rows)
    assert len({r["session"] for r in rows}) == 1 and len(rows[0]["session"]) == 32
    by_tool = {r["tool"]: r for r in rows}
    assert by_tool["list_manufacturer_profiles"]["outcome"] == "ok"
    assert by_tool["list_manufacturer_profiles"]["toolset"] == "verification"
    assert by_tool["verify_board"]["toolset"] == "core"
    assert by_tool["verify_board"]["outcome"] == "error"  # missing required field


def test_a_status_word_of_the_result_is_the_outcome(tmp_path, monkeypatch):
    log_path = tmp_path / "usage.jsonl"
    server = _server_with_log(monkeypatch, log_path)

    async def run():
        async with Client(server, mode="auto") as client:
            await client.call_tool("load_toolset", {"name": "verification"})
            await client.call_tool("load_toolset", {"name": "verification"})
            await client.call_tool("load_toolset", {"name": "no-such-toolset"})

    asyncio.run(run())
    outcomes = [r["outcome"] for r in _lines(log_path)]
    assert outcomes == ["loaded", "already_loaded", "error"]


def test_outcome_words():
    class R:
        def __init__(self, content, is_error=False):
            self.structured_content, self.is_error = content, is_error

    assert usage_log.outcome_of(R({"verdict": "PASS"})) == "PASS"
    assert usage_log.outcome_of(R({"result": {"verdict": "INCOMPLETE"}})) == "INCOMPLETE"
    assert usage_log.outcome_of(R({"ok": False, "reason": "x"})) == "not_ok"
    assert usage_log.outcome_of(R({"ok": True})) == "ok"
    assert usage_log.outcome_of(R({"error": "unknown toolset"})) == "error"
    assert usage_log.outcome_of(R(None, is_error=True)) == "error"
    assert usage_log.outcome_of(None, error=RuntimeError("x")) == "error"
    # a dict result as the JSON text block the protocol carries (top-level keys only)
    def text(payload):
        part = type("P", (), {"text": json.dumps(payload, indent=2)})()
        return type("T", (), {"structured_content": None, "is_error": False, "content": [part]})()

    assert usage_log.outcome_of(text({"verdict": "FAIL", "x": 1})) == "FAIL"
    assert usage_log.outcome_of(text({"a": {"verdict": "PASS"}})) == "ok"  # nested: not the call's word
    assert usage_log.outcome_of(text({"ok": False})) == "not_ok"
    assert usage_log.outcome_of(text({"error": "C:/Users/zed"})) == "error"
    assert usage_log.outcome_of(text({"status": "C:/Users/zed/x"})) == "other"
    # a status value outside RESULT_WORDS (could hold data) is never copied: "other"
    assert usage_log.outcome_of(R({"status": "C:/Users/someone/board.kicad_pcb"})) == "other"
    assert usage_log.outcome_of(R({"status": "x" * 40})) == "other"


def test_a_tool_that_raises_and_an_unknown_name_are_recorded_without_the_name_text(tmp_path):
    mcp = MCPServer("t")

    @mcp.tool()
    def boom() -> dict:
        raise RuntimeError("secret-net-name")

    log = usage_log.install(mcp, lambda n: "x", UsageLog(tmp_path / "u.jsonl"))
    assert log is not None

    async def run():
        async with Client(mcp, mode="auto") as client:
            await client.call_tool("boom", {})
            await client.call_tool("SENTINEL_TOOL_NAME_C:/Users/zed", {})

    asyncio.run(run())
    text = (tmp_path / "u.jsonl").read_text(encoding="utf-8")
    rows = _lines(tmp_path / "u.jsonl")
    assert [(r["tool"], r["outcome"]) for r in rows] == [("boom", "error"), (usage_log.UNKNOWN_TOOL, "error")]
    assert "secret-net-name" not in text and "SENTINEL" not in text


def test_sentinel_arguments_and_results_never_reach_the_file(tmp_path):
    mcp = MCPServer("t")
    sentinel = "SENTINEL-NET-VCC_3V3-C:/Users/zed/board.kicad_pcb-LCSC-C12345"

    @mcp.tool()
    def echo(pcb_path: str, net: str, parts: list[str]) -> dict:
        return {"ok": True, "echo": pcb_path, "status": sentinel}

    usage_log.install(mcp, lambda n: "x", UsageLog(tmp_path / "u.jsonl"))

    async def run():
        async with Client(mcp, mode="auto") as client:
            res = await client.call_tool("echo", {"pcb_path": sentinel, "net": sentinel, "parts": [sentinel]})
            assert sentinel in res.content[0].text  # the caller does get the result
            await client.call_tool("echo", {"pcb_path": sentinel})  # a validation failure

    asyncio.run(run())
    text = (tmp_path / "u.jsonl").read_text(encoding="utf-8")
    for fragment in ("SENTINEL", "VCC_3V3", "kicad_pcb", "C12345", "zed", os.environ.get("USERNAME", "\0x")):
        assert fragment not in text
    assert [r["outcome"] for r in _lines(tmp_path / "u.jsonl")] == ["other", "error"]


# -- the log never changes a tool's result -------------------------------------------------------


def _answers(server: MCPServer) -> list:
    async def run():
        out = []
        async with Client(server, mode="auto") as client:
            await client.call_tool("load_toolset", {"name": "verification"})
            for name, args in (
                ("list_toolsets", {}),
                ("list_manufacturer_profiles", {}),
                ("verify_board", {}),
                ("verify_board", {"pcb_path": "does-not-exist.kicad_pcb"}),
                ("not_a_tool", {}),
            ):
                res = await client.call_tool(name, args)
                out.append((res.is_error, [c.text for c in res.content], res.structured_content))
        return out

    return asyncio.run(run())


def test_results_are_identical_with_the_log_on_off_and_unwritable(tmp_path, monkeypatch):
    monkeypatch.setenv(usage_log.ENV_VAR, "off")
    off = _answers(build_server())
    on = _answers(_server_with_log(monkeypatch, tmp_path / "ok" / "u.jsonl"))
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is needed", encoding="utf-8")
    bad_server = _server_with_log(monkeypatch, blocker / "sub" / "u.jsonl")
    bad = _answers(bad_server)
    assert off == on == bad
    assert (tmp_path / "ok" / "u.jsonl").exists()
    assert not (blocker / "sub").exists()


def test_a_failing_log_is_counted_and_never_raised(tmp_path):
    blocker = tmp_path / "blocker"
    blocker.write_text("x", encoding="utf-8")
    log = UsageLog(blocker / "sub" / "u.jsonl")
    log.record("t", "s", time.time(), 1.0, "ok")  # must not raise
    assert log.failures == 1 and log.written == 0

    class Exploding(UsageLog):
        def record(self, *a, **k):
            raise RuntimeError("log bug")

    mcp = MCPServer("t")

    @mcp.tool()
    def fine() -> dict:
        return {"ok": True}

    usage_log.install(mcp, lambda n: "x", Exploding(tmp_path / "u.jsonl"))

    async def run():
        async with Client(mcp, mode="auto") as client:
            return await client.call_tool("fine", {})

    assert json.loads(asyncio.run(run()).content[0].text) == {"ok": True}

    def bad_toolset(name):
        raise RuntimeError("registry bug")

    mcp2 = MCPServer("t2")
    mcp2.tool()(fine)
    log2 = usage_log.install(mcp2, bad_toolset, UsageLog(tmp_path / "u2.jsonl"))

    async def run2():
        async with Client(mcp2, mode="auto") as client:
            return await client.call_tool("fine", {})

    assert json.loads(asyncio.run(run2()).content[0].text) == {"ok": True}
    assert log2.failures + log2.written >= 1


# -- schemas an LLM reads are untouched ------------------------------------------------------------


def test_tool_descriptions_and_schemas_are_byte_identical_with_the_wrapper(tmp_path, monkeypatch):
    def dump(server):
        async def run():
            async with Client(server, mode="auto") as client:
                await _load_all(client)
                listing = await client.list_tools()
                return json.dumps([t.model_dump(mode="json") for t in listing.tools], sort_keys=True)

        return asyncio.run(run())

    monkeypatch.setenv(usage_log.ENV_VAR, "off")
    off = dump(build_server())
    on = dump(_server_with_log(monkeypatch, tmp_path / "u.jsonl"))
    assert on == off and len(off) > 10_000


# -- size cap and rotation -----------------------------------------------------------------------


def test_the_file_is_capped_and_rotates_once(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", max_bytes=2_000)
    for i in range(200):
        log.record("tool", "set", time.time(), float(i), "ok")
    assert log.failures == 0
    current, rotated = tmp_path / "u.jsonl", tmp_path / "u.jsonl.1"
    assert current.stat().st_size <= 2_000 and rotated.stat().st_size <= 2_000
    assert sorted(p.name for p in tmp_path.iterdir()) == ["u.jsonl", "u.jsonl.1"]
    kept = len(_lines(current)) + len(_lines(rotated))
    assert 0 < kept < 200  # the oldest lines are gone
    assert _lines(current)[-1]["ms"] == 199.0  # the newest is kept
    assert all(isinstance(r, dict) for r in _lines(rotated))  # a rotation never cuts a line


def test_a_single_line_larger_than_the_cap_does_not_loop_or_raise(tmp_path):
    log = UsageLog(tmp_path / "u.jsonl", max_bytes=10)
    for _ in range(3):
        log.record("tool", "set", time.time(), 1.0, "ok")
    assert log.failures == 0


# -- where the file goes, and the switch -----------------------------------------------------------


@pytest.mark.parametrize("value", ["off", "OFF", "0", "false", "disabled"])
def test_off_values(value):
    assert usage_log.resolve_path({usage_log.ENV_VAR: value}) is None


def test_a_named_file_and_the_default_locations(tmp_path):
    assert usage_log.resolve_path({usage_log.ENV_VAR: str(tmp_path / "x.jsonl")}) == tmp_path / "x.jsonl"
    win = usage_log.resolve_path({"LOCALAPPDATA": "C:/L", "USERPROFILE": "C:/U"}, "win32")
    assert win == Path("C:/L/boardproof/usage.jsonl")
    mac = usage_log.resolve_path({"HOME": "/h"}, "darwin")
    assert mac == Path("/h/Library/Application Support/boardproof/usage.jsonl")
    xdg = usage_log.resolve_path({"XDG_DATA_HOME": "/x", "HOME": "/h"}, "linux")
    assert xdg == Path("/x/boardproof/usage.jsonl")
    assert usage_log.resolve_path({"HOME": "/h"}, "linux") == Path("/h/.local/share/boardproof/usage.jsonl")


def test_on_by_default_when_the_variable_is_unset(monkeypatch):
    monkeypatch.delenv(usage_log.ENV_VAR)
    mcp = MCPServer("t")
    assert usage_log.install(mcp, lambda n: "x", None) is not None  # only builds; nothing is written
    monkeypatch.setenv(usage_log.ENV_VAR, "off")
    assert usage_log.install(MCPServer("t"), lambda n: "x", None) is None


# -- privacy boundary of the code ------------------------------------------------------------------


def test_the_module_imports_no_networking_module():
    tree = ast.parse(SRC.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    banned = {
        "socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp", "ftplib",
        "smtplib", "telnetlib", "xmlrpc", "websockets", "asyncio", "subprocess", "webbrowser",
    }
    assert not imported & banned, imported & banned


def test_the_script_imports_no_networking_module():
    path = SRC_DIR.parent / "scripts" / "usage_report.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names = {
        n.names[0].name.split(".")[0] if isinstance(n, ast.Import) else (n.module or "").split(".")[0]
        for n in ast.walk(tree)
        if isinstance(n, (ast.Import, ast.ImportFrom))
    }
    assert not names & {"socket", "ssl", "http", "urllib", "urllib3", "requests", "httpx", "aiohttp"}


# -- the suite never writes the real data directory ------------------------------------------------


def _real_default_log() -> Path:
    env = {k: v for k, v in os.environ.items() if k != usage_log.ENV_VAR}
    path = usage_log.resolve_path(env)
    assert path is not None
    return path


def _stat(path: Path):
    try:
        st = path.stat()
        return (st.st_size, st.st_mtime_ns)
    except FileNotFoundError:
        return None


def test_the_suite_environment_switches_the_log_off():
    assert os.environ[usage_log.ENV_VAR] == "off"
    assert usage_log.resolve_path() is None


def test_calls_through_a_default_built_server_leave_the_real_data_directory_alone():
    real = _real_default_log()
    before = _stat(real)
    server = build_server()  # the environment of the suite: off

    async def run():
        async with Client(server, mode="auto") as client:
            await client.call_tool("list_toolsets", {})

    asyncio.run(run())
    assert _stat(real) == before


def test_a_subprocess_started_by_a_test_inherits_the_off_switch_and_writes_nothing():
    real = _real_default_log()
    before = _stat(real)
    code = (
        "import asyncio\n"
        "from mcp.client.client import Client\n"
        "from boardproof import usage_log\n"
        "from boardproof.server import build_server\n"
        "assert usage_log.resolve_path() is None\n"
        "s = build_server()\n"
        "async def go():\n"
        "    async with Client(s, mode='auto') as c:\n"
        "        await c.call_tool('list_toolsets', {})\n"
        "asyncio.run(go())\n"
        "print('SUBPROCESS-OK')\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(SRC_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    done = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120)
    assert "SUBPROCESS-OK" in done.stdout, done.stderr[-500:]
    assert _stat(real) == before


def test_conftest_forces_the_switch_even_when_the_developer_set_a_path():
    text = (Path(__file__).parent / "conftest.py").read_text(encoding="utf-8")
    assert 'os.environ["BOARDPROOF_USAGE_LOG"] = "off"' in text  # assignment, not setdefault


# -- the outcome word is a fixed list (issue 45 review) ---------------------------------------------


def _text_result(payload):
    part = type("P", (), {"text": json.dumps(payload, indent=2)})()
    return type("T", (), {"structured_content": None, "is_error": False, "content": [part]})()


def test_a_long_status_in_a_text_only_result_is_never_copied():
    forty = "A" * 40
    assert usage_log.outcome_of(_text_result({"status": forty})) == "other"
    assert usage_log.outcome_of(_text_result({"verdict": forty})) == "other"


def test_a_word_outside_the_list_is_other_and_every_listed_word_passes_through():
    for word in ("GND", "VCC_3V3", "R1", "my-net", "Pass"):
        assert word not in usage_log.RESULT_WORDS
        assert usage_log.outcome_of(_text_result({"status": word})) == "other"
        assert usage_log.outcome_of(_text_result({"verdict": word})) == "other"
        dict_result = type("D", (), {"structured_content": {"status": word}, "is_error": False})()
        assert usage_log.outcome_of(dict_result) == "other"
    assert {"PASS", "FAIL", "INCOMPLETE", "ok", "error", "partial", "loaded"} <= usage_log.RESULT_WORDS
    for word in sorted(usage_log.RESULT_WORDS):
        assert usage_log.outcome_of(_text_result({"status": word})) == word
        dict_result = type("D", (), {"structured_content": {"verdict": word}, "is_error": False})()
        assert usage_log.outcome_of(dict_result) == word


def _string_arms(value):
    """The string literals a value can be: a constant, or either arm of a (nested) conditional."""
    if isinstance(value, ast.Constant) and isinstance(value.value, str):
        yield value.value
    elif isinstance(value, ast.IfExp):
        yield from _string_arms(value.body)
        yield from _string_arms(value.orelse)


def _status_literals(sources: dict[str, str] | None = None) -> dict[str, set[str]]:
    """String literals the source assigns to a ``status``, ``verdict`` or checkpoint ``success``
    (dict key, keyword, name; both arms of a conditional). ``sources`` replaces the file scan."""
    found: dict[str, set[str]] = {}

    def add(value, where):
        for word in _string_arms(value):
            found.setdefault(word, set()).add(where)

    if sources is None:
        sources = {p.name: p.read_text(encoding="utf-8") for p in sorted(SRC_DIR.rglob("*.py")) if p != SRC}
    for name, text in sources.items():
        for node in ast.walk(ast.parse(text)):
            if isinstance(node, ast.Dict):
                for key, value in zip(node.keys, node.values, strict=True):
                    if isinstance(key, ast.Constant) and key.value in ("status", "verdict"):
                        add(value, name)
            elif isinstance(node, ast.keyword) and node.arg in ("status", "verdict", "success"):
                add(node.value, name)
            elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in ("status", "verdict") for t in node.targets
            ):
                add(node.value, name)
    return found


# Found by the scan but only ever a nested value (a per-sheet or per-zone field), never the call's own
# top-level word, so deliberately NOT in RESULT_WORDS (reviewed, issue 45):
NESTED_ONLY = {
    "clean": "board_outline.py: status of the outline_drc sub-record",
    "added": "schematic_diff.py: per-sheet status inside the diff listing",
    "removed": "schematic_diff.py: per-sheet status inside the diff listing",
    "no_copper_zones": "verify_board.py: status of the stored-fill sub-record",
    "problems": "verify_board.py: status of the stored-fill sub-record",
}


def test_every_status_word_the_source_assigns_is_on_the_list():
    """A tool that starts returning a new status word is logged as ``other`` until someone looks.

    Seen: string literals as a ``status``/``verdict`` dict value, keyword or assignment, and as the
    ``success=`` of a checkpoint helper, including both arms of a conditional. Not seen: a word built
    at run time, or one that reaches ``status`` through a variable or another function's return.
    """
    found = _status_literals()
    assert len(found) > 40  # the scan works
    missing = {
        w: sorted(files) for w, files in found.items() if w not in usage_log.RESULT_WORDS and w not in NESTED_ONLY
    }
    assert not missing, f"add to usage_log.RESULT_WORDS after review: {missing}"


def test_the_list_scan_notices_a_new_word(tmp_path, monkeypatch):
    monkeypatch.setattr(usage_log, "RESULT_WORDS", usage_log.RESULT_WORDS - {"poured"})
    with pytest.raises(AssertionError, match="poured"):
        test_every_status_word_the_source_assigns_is_on_the_list()


def test_a_cancelled_call_is_cancelled_not_error(tmp_path):
    mcp = MCPServer("t")

    @mcp.tool()
    async def slow() -> dict:
        await asyncio.sleep(30)
        return {"ok": True}

    usage_log.install(mcp, lambda n: "x", UsageLog(tmp_path / "u.jsonl"))

    async def run():
        task = asyncio.ensure_future(mcp.call_tool("slow", {}))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert [r["outcome"] for r in _lines(tmp_path / "u.jsonl")] == [usage_log.CANCELLED]


def test_the_scan_sees_conditional_arms_and_the_success_keyword():
    sources = {
        "x.py": "def f(a):\n    status = 'arm_one' if a else ('arm_two' if a else 'arm_three')\n"
        "    return helper(success='scratch_success', verdict='scratch_verdict' if a else 'scratch_other')\n"
    }
    found = _status_literals(sources)
    assert set(found) == {"arm_one", "arm_two", "arm_three", "scratch_success", "scratch_verdict", "scratch_other"}


def test_a_new_word_in_a_conditional_or_a_success_keyword_turns_the_drift_test_red(monkeypatch):
    real = _status_literals

    def with_scratch(sources=None):
        out = real(sources)
        out.update(real({"s.py": "x = helper(success='scratch_one')\nstatus = 'ok' if y else 'scratch_two'\n"}))
        return out

    monkeypatch.setitem(globals(), "_status_literals", with_scratch)
    with pytest.raises(AssertionError, match="scratch_one.*scratch_two|scratch_two.*scratch_one"):
        test_every_status_word_the_source_assigns_is_on_the_list()


def test_adopted_is_a_listed_word_and_the_nested_only_words_are_not():
    assert "adopted" in usage_log.RESULT_WORDS
    assert not set(NESTED_ONLY) & usage_log.RESULT_WORDS
