"""Per-tool report of the local boardproof usage log. Offline, read-only, prints to stdout.

Usage: ``python scripts/usage_report.py [--log FILE] [--since DAYS] [--no-registry] [--json]``

The log is the JSON Lines file ``boardproof.usage_log`` writes (default: the user's data directory;
``BOARDPROOF_USAGE_LOG`` or ``--log`` names another; the rotated ``<name>.1`` is read too). Per tool:
count, share of all calls, median and p95 duration (ms), error rate, last used. Then the tools the
server registers that were never called in the period (needs the package importable; ``--no-registry``
skips it). Lines that do not parse are counted and skipped. Nothing is sent anywhere.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

# "cancelled" is deliberately not here: a cancelled call is not a failure.
_ERROR_WORDS = {"error", "not_ok", "FAIL", "fail", "failed"}


MAX_NAME = 80


def _clean(text: str) -> str:
    """Printable, short: no control character (ESC, BEL, C1) and no format character (bidi override,
    zero-width) reaches the terminal, and a name is cut at MAX_NAME characters with "..."."""
    cut = len(text) > MAX_NAME
    text = text[:MAX_NAME]
    text = "".join("?" if unicodedata.category(ch) in ("Cc", "Cf") else ch for ch in text)
    return text + "..." if cut else text


def read_records(log: Path, since: datetime | None = None) -> tuple[list[dict], int]:
    """Records of the log and its rotated sibling (oldest first), and the count of skipped lines."""
    records: list[dict] = []
    bad = 0
    for path in (log.with_name(log.name + ".1"), log):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
                stamp = datetime.fromisoformat(row["t"])
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                row = {
                    "t": stamp,
                    "tool": _clean(str(row["tool"])),
                    "toolset": _clean(str(row.get("toolset", ""))),
                    "ms": float(row["ms"]),
                    "outcome": _clean(str(row["outcome"])),
                }
            except (ValueError, KeyError, TypeError, RecursionError, OverflowError):
                bad += 1
                continue
            if since is None or row["t"] >= since:
                records.append(row)
    return records, bad


def _percentile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def summarise(records: list[dict]) -> list[dict]:
    by_tool: dict[str, list[dict]] = {}
    for row in records:
        by_tool.setdefault(row["tool"], []).append(row)
    total = len(records)
    out = []
    for tool, rows in by_tool.items():
        times = [r["ms"] for r in rows]
        errors = sum(1 for r in rows if r["outcome"] in _ERROR_WORDS)
        out.append(
            {
                "tool": tool,
                "toolset": rows[-1]["toolset"],
                "count": len(rows),
                "share": len(rows) / total,
                "median_ms": _percentile(times, 0.5),
                "p95_ms": _percentile(times, 0.95),
                "error_rate": errors / len(rows),
                "last_used": max(r["t"] for r in rows).isoformat(timespec="seconds"),
            }
        )
    return sorted(out, key=lambda r: (-r["count"], r["tool"]))


def registered_tools() -> list[str]:
    """Every tool the server can register, with all toolsets loaded (offline)."""
    import asyncio

    from mcp.client.client import Client

    from boardproof.server import build_server

    server = build_server()

    async def load_all() -> None:
        async with Client(server, mode="auto") as client:
            listing = await client.call_tool("list_toolsets", {})
            for ts in listing.structured_content["result"]:
                await client.call_tool("load_toolset", {"name": ts["name"]})

    asyncio.run(load_all())
    return sorted(t.name for t in server._tool_manager.list_tools())


def never_called(registered: list[str], rows: list[dict]) -> list[str]:
    seen = {r["tool"] for r in rows}
    return [name for name in registered if name not in seen]


def render(rows: list[dict], unused: list[str] | None, bad: int, period: str) -> str:
    lines = [f"Usage report ({period}): {sum(r['count'] for r in rows)} calls, {len(rows)} tools"]
    if rows:
        lines.append(f"{'tool':<40} {'count':>6} {'share':>7} {'median ms':>10} {'p95 ms':>9} {'errors':>7}  last used")
        for r in rows:
            lines.append(
                f"{r['tool']:<40} {r['count']:>6} {r['share']:>6.1%} {r['median_ms']:>10.1f} "
                f"{r['p95_ms']:>9.1f} {r['error_rate']:>6.1%}  {r['last_used']}"
            )
    if unused is not None:
        lines.append("")
        lines.append(f"Registered but never called in the period ({len(unused)}):")
        lines.extend(f"  {name}" for name in unused)
    if bad:
        lines.append(f"\n{bad} unreadable log line(s) skipped.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--log", type=Path, help="the usage log (default: the user's data directory)")
    parser.add_argument("--since", type=float, metavar="DAYS", help="only calls of the last DAYS days")
    parser.add_argument("--no-registry", action="store_true", help="skip the list of never-called tools")
    parser.add_argument("--json", action="store_true", help="print JSON instead of a table")
    args = parser.parse_args(argv)

    log = args.log
    if log is None:
        from boardproof import usage_log

        # The env var may say "off" (no writing); reading then falls back to the default file.
        log = usage_log.resolve_path() or usage_log.data_dir() / usage_log.FILE_NAME
    since = datetime.now(timezone.utc) - timedelta(days=args.since) if args.since is not None else None
    records, bad = read_records(log, since)
    rows = summarise(records)
    unused = None if args.no_registry else never_called(registered_tools(), records)
    period = f"last {args.since:g} days" if args.since is not None else "whole log"
    if args.json:
        payload = {"period": period, "tools": rows, "never_called": unused, "unreadable_lines": bad}
        print(json.dumps(payload, indent=2))
    else:
        print(render(rows, unused, bad, period))
    return 0


if __name__ == "__main__":
    sys.exit(main())
