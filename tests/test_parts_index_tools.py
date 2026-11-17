"""list_part_filters and filter_parts through the MCP surface (issue 199): registration, annotations, argument
refusals, the skill paragraph and its wording pins. The reader itself is tested in test_parts_index.py."""

from __future__ import annotations

import ast
import json
from datetime import timedelta
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from boardproof.sourcing import parts_index as pi
from boardproof.sourcing import parts_index_refresh as pir
from boardproof.toolsets import sourcing as sourcing_toolset
from tests._parts_index_fixture import FIXTURE_DATE, build_fixture
from tests._parts_index_zip import FakeOpener, single_files

REPO = Path(__file__).resolve().parents[1]
SKILL = " ".join((REPO / ".claude" / "skills" / "kicad-sourcing" / "SKILL.md").read_text(encoding="utf-8").split())


@pytest.fixture
def mcp() -> MCPServer:
    server = MCPServer("test")
    sourcing_toolset.build(server)
    return server


@pytest.fixture
def idx(tmp_path, monkeypatch):
    monkeypatch.delenv(pi.ENV_VAR, raising=False)
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE + timedelta(days=1))
    return str(build_fixture(tmp_path / "cache.sqlite3"))


async def _call(mcp: MCPServer, name: str, arguments: dict) -> dict:
    result = await mcp.call_tool(name, arguments)
    return json.loads(result.content[0].text)


async def _tool(mcp: MCPServer, name: str):
    return {t.name: t for t in await mcp.list_tools()}[name]


def _strings(name: str = "sourcing.py") -> list[str]:
    tree = ast.parse((REPO / "src" / "boardproof" / "toolsets" / name).read_text(encoding="utf-8"))
    return [" ".join(n.value.split()) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _having(*needles: str) -> list[str]:
    return [s for s in _strings() if all(n in s for n in needles)]


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["list_part_filters", "filter_parts"])
async def test_both_tools_are_registered_read_only(mcp, name):
    tool = await _tool(mcp, name)
    ann = tool.annotations
    assert ann.read_only_hint is True and ann.destructive_hint is False and ann.idempotent_hint is True
    assert ann.open_world_hint is False  # no network
    assert name in sourcing_toolset.build(MCPServer("t"))


@pytest.mark.asyncio
async def test_list_part_filters_through_call_tool(mcp, idx):
    r = await _call(mcp, "list_part_filters", {"category": "Resistors", "index_path": idx})
    assert r["ok"] is True and r["index"]["index_date"] == "2026-11-15"
    assert {a["name"] for a in r["attributes"]} == {"Resistance", "Tolerance", "Power Watts"}


@pytest.mark.asyncio
async def test_filter_parts_through_call_tool_with_attribute_filters(mcp, idx):
    r = await _call(mcp, "filter_parts", {
        "category": "Resistors", "index_path": idx, "tiers": ["basic"], "sort_by": "price", "limit": 3,
        "attributes": [{"name": "Resistance", "op": "ge", "value": "10k"}, {"name": "Tolerance", "value": "1%"}],
    })
    assert r["ok"] is True and r["total_count"] == 4 and r["count"] == 3 and r["has_more"] is True
    assert [i["part"]["lcsc_id"] for i in r["items"]] == ["C1003", "C1001", "C1004"]  # price ascending, ties by LCSC number
    assert r["index"]["index_stale"] is False


@pytest.mark.asyncio
async def test_the_env_var_is_enough(mcp, idx, monkeypatch):
    monkeypatch.setenv(pi.ENV_VAR, idx)
    r = await _call(mcp, "filter_parts", {"category": "Diodes"})
    assert r["ok"] is True and r["total_count"] == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["list_part_filters", "filter_parts"])
async def test_without_an_index_the_answer_is_a_clear_refusal(mcp, monkeypatch, name):
    monkeypatch.delenv(pi.ENV_VAR, raising=False)
    r = await _call(mcp, name, {})
    assert r["ok"] is False and r["reason"] == "index_not_configured" and "BOARDPROOF_PARTS_INDEX" in r["message"]
    r = await _call(mcp, name, {"index_path": "no/such/index.sqlite3"})
    assert r["reason"] == "index_not_found"


@pytest.mark.asyncio
async def test_a_stale_index_is_flagged_in_the_answer(mcp, idx, monkeypatch):
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE + timedelta(days=30))
    r = await _call(mcp, "filter_parts", {"category": "Diodes", "index_path": idx})
    assert r["ok"] is True and r["index"]["index_stale"] is True and "30 days old" in r["index"]["stale_note"]


@pytest.mark.asyncio
async def test_an_unknown_argument_is_refused_by_name_not_dropped(mcp, idx):
    with pytest.raises(ToolError, match="unknown argument.*categoty"):
        await _call(mcp, "filter_parts", {"index_path": idx, "categoty": "Resistors"})
    with pytest.raises(ToolError, match="unknown argument.*mfr"):
        await _call(mcp, "list_part_filters", {"index_path": idx, "mfr": "x"})


@pytest.mark.asyncio
async def test_unknown_argument_message_names_what_is_accepted(mcp, idx):
    with pytest.raises(ToolError) as err:
        await _call(mcp, "filter_parts", {"index_path": idx, "foo": 1})
    for name in pi.FILTER_NAMES:
        assert name in str(err.value)
    assert "nothing was written" not in str(err.value) and "generator" not in str(err.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"tiers": ["cheap"]}, {"sort_by": "name"}, {"sort_order": "up"},
    {"attributes": [{"name": "Resistance", "op": "like", "value": "1"}]}, {"attributes": [{"name": "Resistance"}]},
    {"attributes": [{"name": "Resistance", "value": "1", "bogus": 1}]},
])
async def test_a_bad_enum_or_filter_shape_is_refused(mcp, idx, bad):
    with pytest.raises(ToolError):
        await _call(mcp, "filter_parts", {"category": "Resistors", "index_path": idx, **bad})


@pytest.mark.asyncio
async def test_a_wrong_attribute_name_is_refused_with_the_valid_list(mcp, idx):
    r = await _call(mcp, "filter_parts", {"category": "Resistors", "index_path": idx,
                                          "attributes": [{"name": "Resistence", "value": "1k"}]})
    assert r["ok"] is False and r["valid_attributes"] == ["Power Watts", "Resistance", "Tolerance"]


@pytest.mark.asyncio
async def test_a_corrupt_index_is_refused_not_raised(mcp, tmp_path):
    import sqlite3
    p = tmp_path / "half.sqlite3"
    conn = sqlite3.connect(p)
    conn.executescript("CREATE TABLE components (lcsc INTEGER, category_id INTEGER, mfr TEXT, package TEXT, manufacturer_id INTEGER,"
                       " basic INTEGER, description TEXT, datasheet TEXT, stock INTEGER, price TEXT, last_update INTEGER);"
                       "CREATE TABLE categories (id INTEGER, category TEXT, subcategory TEXT);"
                       "CREATE TABLE manufacturers (id INTEGER, name TEXT);"
                       "INSERT INTO categories VALUES (1, 'A', 'B');")
    conn.commit()
    conn.close()
    # Emptied tables are a valid, empty index: no crash, no parts, an unknown date flagged.
    r = await _call(mcp, "filter_parts", {"index_path": str(p)})
    assert r["ok"] is True and r["total_count"] == 0 and r["index"]["index_stale"] is True


# ------------------------------------------------------------------ the tool text and the skill

def test_the_tool_descriptions_say_what_the_pins_need():
    assert _having("Read-only. Start here before filter_parts", "downloaded", "nothing ships with this server",
                   "so an attribute name is never guessed")
    assert _having("days old: stock and price may have changed", "index_not_configured", "refused with the valid names")
    assert _having("Read-only. Filter parts in a local parts index", "one call for the whole question")
    assert _having("A misspelled filter name, attribute name, op, tier or", "sort field is refused with the valid names, never ignored")
    assert _having("a part with no parameter data has attributes null and a note", "no attribute filter")
    assert _having("index is never written")


def test_skill_pins_the_parts_index_paragraph():
    assert "7. **Find parts by parameters in a local index**" in SKILL
    assert "call `list_part_filters(category)` first" in SKILL
    assert "never guess an attribute name, a misspelled filter is refused with the valid names" in SKILL
    assert "nothing is downloaded or shipped with this server" in SKILL
    assert "Every answer states `index.index_date` and `index.index_stale`: an index more than 7 days old is flagged" in SKILL
    assert "a hit with `attributes` null has no parameter data in the index and no attribute filter can match it" in SKILL
    assert "A hit is a candidate only: verify it against its datasheet (step 3)" in SKILL


def test_skill_and_descriptions_agree_on_the_stale_days():
    assert pi.STALE_AFTER_DAYS == 7 and "more than 7 days old" in SKILL


def test_skill_does_not_claim_a_data_licence():
    for word in ("licen", "permitted", "legal"):
        assert word not in SKILL.lower().split("7. **find parts by parameters")[1].split("never hand-write")[0]
    for text in _strings():
        if "jlcparts" in text:
            assert "licen" not in text.lower()


@pytest.mark.asyncio
async def test_offset_and_sort_reach_the_reader_through_the_tool(mcp, idx):
    first = await _call(mcp, "filter_parts", {"category": "Resistors", "index_path": idx, "sort_by": "lcsc", "limit": 2})
    second = await _call(mcp, "filter_parts", {"category": "Resistors", "index_path": idx, "sort_by": "lcsc", "limit": 2, "offset": 2})
    assert [i["part"]["lcsc_id"] for i in first["items"]] == ["C1001", "C1002"]
    assert [i["part"]["lcsc_id"] for i in second["items"]] == ["C1003", "C1004"] and second["offset"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["list_part_filters", "filter_parts"])
async def test_a_sqlite_failure_during_the_read_is_a_refusal(mcp, idx, monkeypatch, name):
    import sqlite3

    def boom(*a, **kw):
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr(pi, "list_filters" if name == "list_part_filters" else "filter_parts", boom)
    r = await _call(mcp, name, {"index_path": idx})
    assert r["ok"] is False and r["reason"] == "index_unreadable" and "malformed" in r["message"]


def test_descriptions_and_skill_name_the_new_result_fields():
    assert _having("numeric_values and non_numeric_values count the distinct values that do", "non_numeric_parts the parts holding a non-numeric value",
                   "unit is null with a unit_note", "range_note says so when some are not numeric")
    assert _having("skipped_categories counts category rows whose name is not text, which cannot be listed")
    assert _having("skipped_unreadable_rows counts index rows left out because their stock is not a whole number", "they are not in total_count")
    assert _having("unreadable_rows counts listed items whose row could not be read as a part", "only lcsc_id and unreadable")
    assert "`non_numeric_values` and `non_numeric_parts` say how much of it is not a quantity, so its `numeric_range` covers the numeric values only" in SKILL
    assert "a null `unit` comes with a `unit_note`; `skipped_categories` counts categories that cannot be listed" in SKILL
    assert "`skipped_unreadable_rows` and `unreadable_rows` count index rows that were left out or could not be read as a part, so a total is not the whole index" in SKILL


# ---------------------------------------------------------------- refresh_parts_index (issue 200)



@pytest.mark.asyncio
async def test_refresh_parts_index_is_registered_as_a_destructive_open_world_tool(mcp):
    ann = (await _tool(mcp, "refresh_parts_index")).annotations
    assert ann.read_only_hint is False and ann.destructive_hint is True and ann.open_world_hint is True
    assert ann.idempotent_hint is False


@pytest.mark.asyncio
async def test_refresh_parts_index_takes_exactly_target_dir_and_dry_run(mcp):
    tool = await _tool(mcp, "refresh_parts_index")
    assert set(tool.input_schema["properties"]) == {"target_dir", "dry_run"}
    assert tool.input_schema["properties"]["dry_run"]["default"] is True
    with pytest.raises(ToolError, match="unknown argument.*opener"):
        await _call(mcp, "refresh_parts_index", {"opener": "x"})
    with pytest.raises(ToolError, match="unknown argument.*base_url"):
        await _call(mcp, "refresh_parts_index", {"base_url": "https://evil.example/"})


def test_refresh_description_pins_the_wording():
    (text,) = _having("on the user's say-so")
    for needle in ("never automatic", "downloads the data to their own disk", "nothing ships with this server",
                   "no claim about the terms the data is published under", "The download is large", "about 316 MB", "dry_run is true by default",
                   "downloads nothing", "dry_run=false", "old_index_kept", "does not change BOARDPROOF_PARTS_INDEX",
                   "path inside a git repository or a KiCad project folder is refused", "Writes no KiCad file"):
        assert needle in text, needle
    assert "ensure" not in text.lower() and "guarantee" not in text.lower()


def test_the_read_tools_name_the_refresh_tool_when_there_is_no_index():
    assert "refresh_parts_index" in pi.HOW_TO_GET and "dry run first" in pi.HOW_TO_GET


@pytest.mark.asyncio
async def test_a_plain_call_is_a_dry_run_that_writes_nothing(mcp, tmp_path, monkeypatch):
    opener = FakeOpener(single_files("cache.sqlite3", b"x" * 100))
    monkeypatch.setattr(pir, "build_opener", lambda: opener)
    target = tmp_path / "idx"
    r = await _call(mcp, "refresh_parts_index", {"target_dir": str(target)})
    assert r["ok"] is True and r["status"] == "planned" and r["written"] is False
    assert not target.exists() and {m for m, _ in opener.calls} == {"HEAD"}


@pytest.mark.asyncio
async def test_a_real_call_replaces_the_index_through_the_tool(mcp, tmp_path, monkeypatch, idx):
    data = Path(idx).read_bytes()
    monkeypatch.setattr(pir, "build_opener", lambda: FakeOpener(single_files("cache.sqlite3", data)))
    target = tmp_path / "idx"
    r = await _call(mcp, "refresh_parts_index", {"target_dir": str(target), "dry_run": False})
    assert r["ok"] is True and r["status"] == "refreshed" and r["index"]["part_count"] > 0
    assert (target / "cache.sqlite3").read_bytes() == data
    out = await _call(mcp, "filter_parts", {"category": "Diodes", "index_path": r["set_env"][pi.ENV_VAR]})
    assert out["ok"] is True


def test_no_test_seam_is_reachable_from_the_tool_surface():
    """The registered function passes only target_dir and dry_run; the opener is a module-level seam, not an argument."""
    tree = ast.parse((REPO / "src" / "boardproof" / "toolsets" / "sourcing.py").read_text(encoding="utf-8"))
    (fn,) = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "refresh_parts_index"]
    assert [a.arg for a in fn.args.args] == ["target_dir", "dry_run"]
    calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call)]
    assert len(calls) == 1 and [k.arg for k in calls[0].keywords] == [] and len(calls[0].args) == 2


def test_the_skill_paragraph_and_its_wording_pins():
    (item,) = [p for p in SKILL.split(" 8. **") if p.startswith("Getting or updating")]
    for needle in ("only when the user asks for it or agrees", "the download is large", "make no statement about the terms it is published under",
                   "default `dry_run=true`", "downloads nothing and writes nothing", "only after they agree", "old_index_kept", "means do not proceed",
                   "never changes `BOARDPROOF_PARTS_INDEX`", "target_path", "never start a refresh on your own"):
        assert needle in item, needle


def test_refresh_description_says_a_failed_probe_is_not_ok():
    (text,) = _having("on the user's say-so")
    assert "if the probe fails it answers ok false with would_fail true" in text
