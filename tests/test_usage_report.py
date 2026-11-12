"""scripts/usage_report.py on a fixture log (GitHub issue 45): offline, stdout only."""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "usage_report.py"
spec = importlib.util.spec_from_file_location("usage_report", SCRIPT)
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)

NOW = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)


def _row(tool, ms, outcome="ok", days_ago=0.0, toolset="pcb"):
    stamp = (NOW - timedelta(days=days_ago)).isoformat(timespec="milliseconds")
    return json.dumps({"t": stamp, "tool": tool, "toolset": toolset, "ms": ms, "outcome": outcome, "session": "a" * 32})


def _fixture(tmp_path: Path) -> Path:
    log = tmp_path / "usage.jsonl"
    rotated = tmp_path / "usage.jsonl.1"
    rotated.write_text(_row("place_footprints", 500.0, days_ago=40) + "\n", encoding="utf-8")
    lines = [_row("verify_board", float(ms), "PASS" if ms else "FAIL", days_ago=ms) for ms in range(0, 20)]
    lines += [_row("place_footprints", 100.0, days_ago=1), _row("place_footprints", 300.0, "error", days_ago=0.5)]
    lines += ["not json at all", '{"t": "2026-11-10", "tool": "x"}', ""]
    log.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log


def test_per_tool_figures(tmp_path):
    records, bad = report.read_records(_fixture(tmp_path))
    assert bad == 2 and len(records) == 20 + 2 + 1
    rows = {r["tool"]: r for r in report.summarise(records)}
    vb, pf = rows["verify_board"], rows["place_footprints"]
    assert vb["count"] == 20 and pf["count"] == 3
    assert abs(vb["share"] - 20 / 23) < 1e-9
    assert vb["median_ms"] == 9.0 and vb["p95_ms"] == 18.0  # nearest rank on 0..19
    assert vb["error_rate"] == 1 / 20  # the one FAIL
    assert pf["median_ms"] == 300.0 and pf["p95_ms"] == 500.0
    assert abs(pf["error_rate"] - 1 / 3) < 1e-9
    assert pf["last_used"] == (NOW - timedelta(days=0.5)).isoformat(timespec="seconds")
    assert [r["tool"] for r in report.summarise(records)] == ["verify_board", "place_footprints"]  # by count


def test_the_period_filter(tmp_path):
    records, _ = report.read_records(_fixture(tmp_path), since=NOW - timedelta(days=5))
    tools = {}
    for r in records:
        tools[r["tool"]] = tools.get(r["tool"], 0) + 1
    assert tools == {"verify_board": 6, "place_footprints": 2}  # days_ago 0..5 (the bound is inclusive) and the two recent calls


def test_never_called_tools():
    records = [{"tool": "a"}, {"tool": "b"}]
    assert report.never_called(["a", "b", "c", "d"], records) == ["c", "d"]


def test_main_prints_a_table_to_stdout_and_writes_nothing(tmp_path, capsys):
    log = _fixture(tmp_path)
    before = sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir())
    assert report.main(["--log", str(log), "--no-registry"]) == 0
    out = capsys.readouterr().out
    assert "verify_board" in out and "place_footprints" in out and "2 unreadable" in out
    assert sorted((p.name, p.read_bytes()) for p in tmp_path.iterdir()) == before


def test_main_lists_registered_tools_never_called(tmp_path, capsys):
    log = _fixture(tmp_path)
    assert report.main(["--log", str(log), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert "list_toolsets" in payload["never_called"]
    assert "verify_board" not in payload["never_called"]  # it was called in the fixture
    assert len(payload["never_called"]) > 50


def test_a_missing_log_is_an_empty_report(tmp_path, capsys):
    assert report.main(["--log", str(tmp_path / "none.jsonl"), "--no-registry"]) == 0
    assert "0 calls" in capsys.readouterr().out


def test_control_characters_in_a_name_never_reach_the_terminal(tmp_path, capsys):
    log = tmp_path / "usage.jsonl"
    evil = "\x1b[31mred\x1b]0;title\x07\x9b"
    log.write_text(_row(evil, 1.0, outcome=evil, toolset=evil) + "\n", encoding="utf-8")
    assert report.main(["--log", str(log), "--no-registry"]) == 0
    out = capsys.readouterr().out
    assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out
    assert "red" in out


def test_a_pathologically_nested_line_is_a_bad_line_not_a_crash(tmp_path, capsys):
    log = tmp_path / "usage.jsonl"
    log.write_text("[" * 100_000 + "\n" + _row("verify_board", 1.0) + "\n", encoding="utf-8")
    records, bad = report.read_records(log)
    assert bad == 1 and len(records) == 1
    assert report.main(["--log", str(log), "--no-registry"]) == 0


def test_a_cancelled_call_is_not_an_error_in_the_error_rate():
    rows = [
        {"t": NOW, "tool": "a", "toolset": "x", "ms": 1.0, "outcome": o}
        for o in ("cancelled", "ok", "error", "other")
    ]
    assert report.summarise(rows)[0]["error_rate"] == 0.25


def test_control_characters_in_outcome_and_toolset_are_cleaned_too(tmp_path):
    log = tmp_path / "usage.jsonl"
    log.write_text(_row("tool", 1.0, outcome="ok\x1b[2J", toolset="set\x1b]0;x\x07") + "\n", encoding="utf-8")
    records, bad = report.read_records(log)
    assert bad == 0
    assert "\x1b" not in records[0]["outcome"] and "\x1b" not in records[0]["toolset"]
    assert "\x07" not in records[0]["toolset"]


def test_bidi_and_zero_width_characters_are_replaced(tmp_path):
    log = tmp_path / "usage.jsonl"
    log.write_text(_row("evil\u202egnp.exe\u200b", 1.0) + "\n", encoding="utf-8")
    records, _ = report.read_records(log)
    assert records[0]["tool"] == "evil?gnp.exe?"


def test_a_huge_name_is_cut_and_the_output_stays_small(tmp_path, capsys):
    log = tmp_path / "usage.jsonl"
    log.write_text(_row("n" * 1_000_000, 1.0, toolset="t" * 1_000_000) + "\n", encoding="utf-8")
    assert report.main(["--log", str(log), "--no-registry"]) == 0
    out = capsys.readouterr().out
    assert len(out) < 1_000 and "n" * 80 + "..." in out and "n" * 81 not in out
