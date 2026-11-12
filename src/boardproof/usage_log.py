"""A local, anonymous usage log: which tools were called, how long they took, how they ended.

``install`` wraps the server's ``call_tool`` once, for every tool (current and future, sync and
async, tools that raise), the same seam ``argument_hints`` uses. Per call it appends one JSON line:

    {"t": "<UTC ISO time>", "tool": "...", "toolset": "...", "ms": 12.3, "outcome": "ok", "session": "<hex>"}

Nothing else is recorded: never an argument, a result, a path, a part number, a net name, a user or
machine name or an environment value. The tool name is written only when it is a registered tool
(a caller can send any text as a name); the outcome is ``ok``, ``error`` (the call raised or the
protocol answered an error), ``not_ok`` (the result says ``ok: false``), ``error`` also for a result with an ``error`` field, or the result's own
``verdict``/``status`` word when it is in the fixed ``RESULT_WORDS`` set (``PASS``, ``FAIL``,
``INCOMPLETE``, ``loaded``, ...), else ``other``; ``cancelled`` for a cancelled call. The session id is random per process and is not stored anywhere else.

The file stays on this machine: this module imports no networking module (a test checks). Default
location: the user's data directory (``BOARDPROOF_USAGE_LOG`` names another file, or ``off`` turns the
log off). The file is capped (``MAX_BYTES``) and rotates once to ``<name>.1``.

A failure of the log (unwritable directory, full disk, a bad clock) never reaches the tool call: it
is swallowed and counted in ``UsageLog.failures``. The wrapper never changes the arguments, the
result or the error of the call it wraps.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

ENV_VAR = "BOARDPROOF_USAGE_LOG"
OFF_VALUES = ("off", "0", "false", "no", "none", "disabled")
MAX_BYTES = 1_000_000
FILE_NAME = "usage.jsonl"
UNKNOWN_TOOL = "<unregistered>"
# The only status/verdict words a result may put into the outcome (privacy: a free word such as a net
# name never reaches the file). Derived from the string literals the tools assign to ``status`` or
# ``verdict``, or pass as a checkpoint ``success`` (tests/test_usage_log.py scans src/ for new ones, also
# in conditional arms, and fails until this set is reviewed).
# Anything else is recorded as ``other``.
RESULT_WORDS = frozenset(
    {
     "FAIL", "INCOMPLETE", "NOT_RUN", "NO_OFFICIAL_BATCH", "PASS", "RAN", "REFUSED", "SCORED",
     "SETUP_FAILED", "UNAVAILABLE", "already_loaded", "adopted", "already_present", "degraded", "no_local_assets", "no_results", "no_structured_tables_found", "violations_found", "applied",
     "below_fab_floor", "blocked", "board_rule_unspecified", "built", "complete", "confirmed",
     "connected", "created", "disconnected", "error", "errors", "fail", "failed", "failure",
     "imported", "incomplete", "invalid_params", "known", "loaded", "match",
     "match_with_mechanical_pads", "matches", "mismatch", "needs_acknowledgement", "none",
     "not_connected", "not_loaded", "not_published_by_fab", "not_queried", "not_run",
     "nothing_to_do", "ok", "opened", "partial", "partly_compared", "pass", "pending_confirmation",
     "poured", "prepared", "present", "proposed", "ready", "refused", "rejected", "selected",
     "stale", "timeout", "unavailable", "undetermined", "unhandled_only", "unknown", "unloaded",
     "unreadable", "unsupported", "unsupported_package_type",
    }
)
OTHER = "other"
CANCELLED = "cancelled"


def data_dir(environ: dict[str, str] | None = None, platform: str | None = None) -> Path:
    """The platform's per-user data directory for boardproof (not created here)."""
    env = os.environ if environ is None else environ
    platform = sys.platform if platform is None else platform
    home = Path(env.get("USERPROFILE") or env.get("HOME") or Path.home())
    if platform.startswith("win"):
        base = env.get("LOCALAPPDATA")
        return (Path(base) if base else home / "AppData" / "Local") / "boardproof"
    if platform == "darwin":
        return home / "Library" / "Application Support" / "boardproof"
    base = env.get("XDG_DATA_HOME")
    return (Path(base) if base else home / ".local" / "share") / "boardproof"


def resolve_path(environ: dict[str, str] | None = None, platform: str | None = None) -> Path | None:
    """The log file, or None when the log is off."""
    env = os.environ if environ is None else environ
    value = env.get(ENV_VAR)
    if value is not None:
        value = value.strip()
        if value.lower() in OFF_VALUES:
            return None
        if value:
            return Path(value)
    return data_dir(env, platform) / FILE_NAME


def outcome_of(result: Any = None, error: BaseException | None = None) -> str:
    """ok / error / not_ok, or the result's own plain status word. Never returns result data."""
    if error is not None:
        # asyncio.CancelledError (not imported: no asyncio in this module): a cancelled call is not a failure
        return CANCELLED if type(error).__name__ == "CancelledError" else "error"
    if getattr(result, "is_error", False):
        return "error"
    content = getattr(result, "structured_content", None)
    if isinstance(content, dict):
        inner = content.get("result") if set(content) == {"result"} else content
        if isinstance(inner, dict):
            for key in ("verdict", "status"):
                word = inner.get(key)
                if isinstance(word, str):
                    return word if word in RESULT_WORDS else OTHER
            if inner.get("ok") is False:
                return "not_ok"
            if isinstance(inner.get("error"), str):
                return "error"
        return "ok"
    # A dict result arrives as one JSON text block, indented by two spaces: its top-level keys are
    # the lines with exactly two spaces. A scan, not a parse (no cost on a large result, no copy).
    parts = getattr(result, "content", None)
    if isinstance(parts, list) and len(parts) == 1:
        text = getattr(parts[0], "text", None)
        if isinstance(text, str) and text.startswith("{\n"):
            for key in ("verdict", "status"):
                found = re.search(rf'^  "{key}": "(.*)",?$', text, re.M)
                if found:
                    return found.group(1) if found.group(1) in RESULT_WORDS else OTHER
            if re.search(r'^  "ok": false,?$', text, re.M):
                return "not_ok"
            if re.search(r'^  "error": "', text, re.M):
                return "error"
    return "ok"


class UsageLog:
    def __init__(self, path: Path, max_bytes: int = MAX_BYTES, session: str | None = None) -> None:
        self.path = Path(path)
        self.max_bytes = max_bytes
        self.session = session or uuid.uuid4().hex
        self.failures = 0
        self.written = 0
        self._lock = threading.Lock()

    def _open(self):
        try:
            return open(self.path, "a", encoding="utf-8", newline="\n")
        except FileNotFoundError:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            return open(self.path, "a", encoding="utf-8", newline="\n")

    def record(self, tool: str, toolset: str, started_wall: float, duration_ms: float, outcome: str) -> None:
        """Append one line; never raises."""
        try:
            stamp = datetime.fromtimestamp(started_wall, timezone.utc).isoformat(timespec="milliseconds")
            line = json.dumps(
                {
                    "t": stamp,
                    "tool": tool,
                    "toolset": toolset,
                    "ms": round(duration_ms, 1),
                    "outcome": outcome,
                    "session": self.session,
                },
                separators=(",", ":"),
            )
            with self._lock:
                handle = self._open()
                try:
                    size = handle.tell()  # append mode: the end of the file
                    if size > 0 and size + len(line) + 1 > self.max_bytes:
                        handle.close()
                        os.replace(self.path, self.path.with_name(self.path.name + ".1"))
                        handle = self._open()
                    handle.write(line + "\n")
                finally:
                    handle.close()
                self.written += 1
        except BaseException as exc:  # noqa: BLE001 - the log must never reach the tool call
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.failures += 1


def install(mcp: Any, toolset_of: Callable[[str], str], log: UsageLog | None = None) -> UsageLog | None:
    """Wrap ``mcp.call_tool`` so every call is recorded. Returns the log, or None when it is off.

    Install this last (outermost) so the time includes the other wrappers. ``toolset_of`` maps a
    tool name to its toolset name; it is called inside the same swallow as the write.
    """
    if log is None:
        path = resolve_path()
        if path is None:
            return None
        log = UsageLog(path)
    original = mcp.call_tool

    def _name_and_set(name: Any) -> tuple[str, str]:
        # A caller can send any text as a name: only a registered tool's name is written.
        try:
            if isinstance(name, str) and mcp._tool_manager.get_tool(name) is not None:
                return name, str(toolset_of(name))
        except Exception:  # noqa: BLE001
            pass
        return UNKNOWN_TOOL, ""

    async def call_tool(name, arguments, *args, **kwargs):
        known = _name_and_set(name)  # before the call: a tool may unload itself
        wall = time.time()
        start = time.perf_counter()
        try:
            result = await original(name, arguments, *args, **kwargs)
        except BaseException as exc:
            _finish(known, wall, start, None, exc)
            raise
        _finish(known, wall, start, result, None)
        return result

    def _finish(known, wall, start, result, error) -> None:
        try:
            duration = (time.perf_counter() - start) * 1000.0
            tool, toolset = known
            log.record(tool, toolset, wall, duration, outcome_of(result, error))
        except Exception:  # noqa: BLE001
            log.failures += 1

    mcp.call_tool = call_tool
    return log
