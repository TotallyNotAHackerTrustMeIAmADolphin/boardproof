"""The explicit refresh of the local parts index (issue 200): download, split-zip extraction, validation, all-or-nothing
replace. Offline: a fake opener serves small hand-built archives (tests/_parts_index_zip.py); nothing is downloaded."""

from __future__ import annotations

import os
import struct
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from pathlib import Path

import pytest

from boardproof.sourcing import parts_index as pi
from boardproof.sourcing import parts_index_refresh as pr
from tests._parts_index_fixture import FIXTURE_DATE, build_fixture, ignore_ancestors_of
from tests._parts_index_zip import FakeOpener, FakeResponse, single_files, split_zip, single_zip

OLD = b"old index bytes, untouched"


@pytest.fixture
def sqlite_bytes(tmp_path_factory) -> bytes:
    path = build_fixture(tmp_path_factory.mktemp("fix") / "cache.sqlite3")
    return path.read_bytes()


@pytest.fixture(autouse=True)
def _no_ancestor_scan(tmp_path, monkeypatch):
    ignore_ancestors_of(monkeypatch, tmp_path)


@pytest.fixture
def target(tmp_path, monkeypatch) -> Path:
    monkeypatch.setattr(pi, "_now", lambda: FIXTURE_DATE + timedelta(days=1))
    monkeypatch.delenv(pi.ENV_VAR, raising=False)
    return tmp_path / "cache"


def _tree(path: Path) -> dict[str, bytes | None]:
    if not path.exists():
        return {}
    return {str(p.relative_to(path)): (p.read_bytes() if p.is_file() else None) for p in sorted(path.rglob("*"))}


def _with_old(target: Path) -> Path:
    target.mkdir(parents=True)
    old = target / "cache.sqlite3"
    old.write_bytes(OLD)
    return old


def _assert_failed_and_kept(result: dict, target: Path, reason: str) -> None:
    assert result["ok"] is False and result["reason"] == reason, result
    assert result["old_index_kept"] == str(target / "cache.sqlite3")
    assert (target / "cache.sqlite3").read_bytes() == OLD
    assert [p.name for p in target.iterdir()] == ["cache.sqlite3"], "a temporary file was left behind"
    assert "was not changed" in result["message"]


# ---------------------------------------------------------------- happy paths

def test_split_archive_is_assembled_extracted_validated_and_replaced(target, sqlite_bytes):
    files = split_zip("cache.sqlite3", sqlite_bytes, 1000)
    assert len(files) >= 3
    opener = FakeOpener(files)
    old = _with_old(target)
    result = pr.run(str(target), dry_run=False, opener=opener)
    assert result["ok"] is True and result["status"] == "refreshed" and result["written"] is True
    assert result["replaced_previous_index"] is True
    assert old.read_bytes() == sqlite_bytes
    assert result["index"]["index_date"] == "2026-11-15" and result["index"]["part_count"] == 34
    assert result["set_env"] == {pi.ENV_VAR: str(old)}
    assert result["segments"] == len(files)
    assert [n for _, n in opener.calls][:2] == [pr.BASE_URL + "cache.z01", pr.BASE_URL + "cache.z02"]
    assert [p.name for p in target.iterdir()] == ["cache.sqlite3"]
    # the reader works on it
    out = pi.filter_parts(str(old), category="Resistors")
    assert out["total_count"] > 0


def test_a_single_segment_archive_is_read_too(target, sqlite_bytes):
    result = pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert result["ok"] is True and result["segments"] == 1 and result["replaced_previous_index"] is False
    assert (target / "cache.sqlite3").read_bytes() == sqlite_bytes


@pytest.mark.parametrize("marker_in_offsets", [True, False])
def test_both_readings_of_the_split_marker_offset_work(target, sqlite_bytes, marker_in_offsets):
    files = split_zip("cache.sqlite3", sqlite_bytes, 900, marker_in_offsets=marker_in_offsets)
    assert pr.run(str(target), dry_run=False, opener=FakeOpener(files))["ok"] is True


def test_a_stored_member_is_read(target, sqlite_bytes):
    files = split_zip("cache.sqlite3", sqlite_bytes, 100_000, method=0)
    assert pr.run(str(target), dry_run=False, opener=FakeOpener(files))["ok"] is True


def test_the_tool_does_not_change_the_environment_variable(target, sqlite_bytes, monkeypatch):
    monkeypatch.setenv(pi.ENV_VAR, "elsewhere")
    pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert os.environ[pi.ENV_VAR] == "elsewhere"


# ---------------------------------------------------------------- dry run

def test_dry_run_reports_the_plan_and_writes_nothing(target, sqlite_bytes):
    files = split_zip("cache.sqlite3", sqlite_bytes, 1000)
    opener = FakeOpener(files)
    result = pr.run(str(target), dry_run=True, opener=opener)
    assert result["ok"] is True and result["status"] == "planned" and result["written"] is False
    assert not target.exists(), "a dry run must not even create the directory"
    assert all(method == "HEAD" for method, _ in opener.calls)
    assert result["download"]["urls"][-1] == pr.BASE_URL + "cache.zip"
    assert result["download"]["sizes_bytes"] == [len(v) for v in files.values()]
    assert result["download"]["total_bytes"] == sum(len(v) for v in files.values())
    assert result["target_path"] == str(target.resolve() / "cache.sqlite3")
    assert result["existing_index"] is None
    text = " ".join(result["notes"])
    assert "large" in text and "316 MB" in text and "terms" in text and "nothing ships" in text.lower()


def test_dry_run_is_the_default_and_leaves_an_existing_index_alone(target, sqlite_bytes):
    old = _with_old(target)
    before = _tree(target)
    result = pr.run(str(target), opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert result["status"] == "planned"
    assert _tree(target) == before and old.read_bytes() == OLD
    assert result["existing_index"]["readable"] is False


def test_dry_run_names_the_index_it_would_replace(target, sqlite_bytes):
    target.mkdir()
    (target / "cache.sqlite3").write_bytes(sqlite_bytes)
    result = pr.run(str(target), dry_run=True, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert result["existing_index"]["readable"] is True and result["existing_index"]["index_date"] == "2026-11-15"


def test_dry_run_with_a_failed_probe_is_not_ok_and_says_so(target):
    result = pr.run(str(target), dry_run=True, opener=FakeOpener({}))
    assert result["ok"] is False and result["would_fail"] is True and result["status"] == "probe_failed"
    assert result["reason"] == "archive_missing" and result["download"]["sizes_known"] is False
    assert result["download"]["probe_failed"]["reason"] == "archive_missing"
    assert not target.exists()


def test_dry_run_refuses_an_announced_download_over_the_cap(target, sqlite_bytes, monkeypatch):
    monkeypatch.setattr(pr, "MAX_DOWNLOAD_BYTES", 100)
    result = pr.run(str(target), dry_run=True, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert result["ok"] is False and result["reason"] == "download_too_large"


# ---------------------------------------------------------------- hosts and urls

@pytest.mark.parametrize("url", [
    "http://yaqwsx.github.io/jlcparts/data/cache.zip",
    "https://evil.example/jlcparts/data/cache.zip",
    "https://yaqwsx.github.io.evil.example/jlcparts/data/cache.zip",
    "https://yaqwsx.github.io@evil.example/jlcparts/data/cache.zip",
    "https://user:pw" "@yaqwsx.github.io/jlcparts/data/cache.zip",
    "https://yaqwsx.github.io:8443/jlcparts/data/cache.zip",
    "https://yaqwsx.github.io/other/cache.zip",
    "https://yaqwsx.github.io/jlcparts/data/../x/cache.zip",
    "https://yaqwsx.github.io/jlcparts/data/ cache.zip",
    "file:///etc/passwd",
])
def test_check_url_refuses(url):
    assert pr.check_url(url) is not None


def test_check_url_accepts_the_real_urls():
    assert all(pr.check_url(u) is None for u in pr.segment_urls(3))
    assert pr.segment_urls(3) == [pr.BASE_URL + "cache.z01", pr.BASE_URL + "cache.z02", pr.BASE_URL + "cache.zip"]


def test_a_redirect_to_another_host_is_refused_by_the_handler():
    handler = pr._CheckedRedirect()
    request = urllib.request.Request(pr.BASE_URL + "cache.zip")
    with pytest.raises(urllib.error.URLError, match="redirect refused"):
        handler.redirect_request(request, None, 302, "Found", {}, "https://evil.example/cache.zip")
    handler.redirect_request(request, None, 302, "Found", {}, pr.BASE_URL + "cache.zip")  # same host: fine


def test_a_response_that_ended_on_another_host_is_refused_and_the_old_index_is_kept(target, sqlite_bytes):
    old = _with_old(target)
    files = single_files("cache.sqlite3", sqlite_bytes)
    opener = FakeOpener(files, overrides={"cache.zip": lambda url, m, body: FakeResponse(url, body, final_url="https://evil.example/x")})
    result = pr.run(str(target), dry_run=False, opener=opener)
    _assert_failed_and_kept(result, target, "url_refused")
    assert old.read_bytes() == OLD


def test_the_real_opener_carries_the_redirect_check():
    opener = pr.build_opener()
    assert any(isinstance(h, pr._CheckedRedirect) for h in opener.handlers)


# ---------------------------------------------------------------- caps and truncation

def test_a_download_over_the_cap_announced_by_content_length_is_refused_before_reading(target, sqlite_bytes, monkeypatch):
    old = _with_old(target)
    monkeypatch.setattr(pr, "MAX_DOWNLOAD_BYTES", 500)
    result = pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    _assert_failed_and_kept(result, target, "download_too_large")
    assert old.read_bytes() == OLD


def test_the_cap_check_on_content_length_happens_before_a_byte_is_read(target, sqlite_bytes, monkeypatch):
    _with_old(target)
    monkeypatch.setattr(pr, "MAX_DOWNLOAD_BYTES", 500)
    made = []

    def serve(url, method, body):
        made.append(FakeResponse(url, body))
        return made[-1]

    opener = FakeOpener(single_files("cache.sqlite3", sqlite_bytes), overrides={"cache.zip": serve})
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=opener), target, "download_too_large")
    assert made and made[0]._served == 0, "bytes were read although Content-Length announced too much"


def test_a_download_over_the_cap_without_content_length_is_stopped(target, sqlite_bytes, monkeypatch):
    _with_old(target)
    monkeypatch.setattr(pr, "MAX_DOWNLOAD_BYTES", 500)
    opener = FakeOpener(single_files("cache.sqlite3", sqlite_bytes),
                        overrides={"cache.zip": lambda url, m, body: FakeResponse(url, body, declared=None)})
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=opener), target, "download_too_large")


def test_the_cap_counts_all_segments_together(target, sqlite_bytes, monkeypatch):
    _with_old(target)
    files = split_zip("cache.sqlite3", sqlite_bytes, 1000)
    each = max(len(v) for v in files.values())
    monkeypatch.setattr(pr, "MAX_DOWNLOAD_BYTES", each + 10)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "download_too_large")


def test_a_cap_of_exactly_the_total_still_works(target, sqlite_bytes, monkeypatch):
    files = split_zip("cache.sqlite3", sqlite_bytes, 1000)
    monkeypatch.setattr(pr, "MAX_DOWNLOAD_BYTES", sum(len(v) for v in files.values()))
    assert pr.run(str(target), dry_run=False, opener=FakeOpener(files))["ok"] is True


def test_a_truncated_part_is_refused(target, sqlite_bytes):
    _with_old(target)
    files = split_zip("cache.sqlite3", sqlite_bytes, 1000)
    opener = FakeOpener(files, overrides={"cache.z02": lambda url, m, body: FakeResponse(url, body[: len(body) // 2], declared=len(body))})
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=opener), target, "truncated_download")


def test_a_connection_that_breaks_mid_part_is_refused(target, sqlite_bytes):
    _with_old(target)
    files = split_zip("cache.sqlite3", sqlite_bytes, 1000)
    opener = FakeOpener(files, overrides={"cache.z01": lambda url, m, body: FakeResponse(url, body, fail_after=500)})
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=opener), target, "truncated_download")


def test_an_empty_part_is_refused(target, sqlite_bytes):
    _with_old(target)
    files = single_files("cache.sqlite3", sqlite_bytes)
    opener = FakeOpener(files, overrides={"cache.zip": lambda url, m, body: FakeResponse(url, b"", declared=0)})
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=opener), target, "truncated_download")


def test_a_missing_middle_part_is_found_by_the_segment_count(target, sqlite_bytes):
    _with_old(target)
    files = split_zip("cache.sqlite3", sqlite_bytes, 700)
    assert len(files) >= 4
    del files["cache.z02"]  # the list now ends at z01, and cache.zip says it had more
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "segment_count_mismatch")


def test_a_missing_last_segment_is_refused(target, sqlite_bytes):
    _with_old(target)
    files = split_zip("cache.sqlite3", sqlite_bytes, 1000)
    del files["cache.zip"]
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "archive_missing")


def test_an_http_error_other_than_404_is_a_failure_not_the_end_of_the_list(target, sqlite_bytes):
    _with_old(target)
    files = split_zip("cache.sqlite3", sqlite_bytes, 1000)

    def boom(url, method, body):
        raise urllib.error.HTTPError(url, 503, "Unavailable", {}, None)

    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files, overrides={"cache.z02": boom})), target, "download_failed")


def test_a_network_error_is_a_failure(target, sqlite_bytes):
    _with_old(target)

    def boom(url, method, body):
        raise urllib.error.URLError("no route")

    opener = FakeOpener(single_files("cache.sqlite3", sqlite_bytes), overrides={"cache.zip": boom})
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=opener), target, "download_failed")


def test_too_many_parts_are_refused(target, monkeypatch):
    _with_old(target)
    monkeypatch.setattr(pr, "MAX_SEGMENTS", 3)
    files = {f"cache.z{n:02d}": b"x" for n in range(1, 10)} | {"cache.zip": b"x"}
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "too_many_segments")


def test_the_total_deadline_stops_the_download(target, sqlite_bytes, monkeypatch):
    _with_old(target)
    ticks = iter(range(0, 10_000, 4000))
    monkeypatch.setattr(pr, "_clock", lambda: next(ticks))
    monkeypatch.setattr(pr, "DEADLINE_S", 5000)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes))), target, "download_timeout")


# ---------------------------------------------------------------- the archive

@pytest.mark.parametrize("name", ["../evil.sqlite3", "/abs/cache.sqlite3", "sub/cache.sqlite3", "a\\b.sqlite3", "C:evil.sqlite3", "..", "cache.sqlite3/"])
def test_an_unsafe_member_name_is_refused_and_nothing_is_written_outside(target, sqlite_bytes, tmp_path, name):
    old = _with_old(target)
    result = pr.run(str(target), dry_run=False, opener=FakeOpener(single_files(name, sqlite_bytes)))
    _assert_failed_and_kept(result, target, "unsafe_member" if name not in ("sub/cache.sqlite3", "cache.sqlite3/") else "unexpected_member")
    assert old.read_bytes() == OLD
    assert not (tmp_path / "evil.sqlite3").exists() and not (target.parent / "evil.sqlite3").exists()


def test_a_member_with_another_safe_name_is_refused(target, sqlite_bytes):
    _with_old(target)
    result = pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("other.sqlite3", sqlite_bytes)))
    _assert_failed_and_kept(result, target, "unexpected_member")


def test_an_extra_member_beside_the_index_is_refused(target, sqlite_bytes):
    _with_old(target)
    files = split_zip("cache.sqlite3", sqlite_bytes, 100_000, entries=[("readme.txt", b"hi")])
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "unexpected_member")


def test_an_extra_unsafe_member_is_refused_before_anything_is_extracted(target, sqlite_bytes):
    _with_old(target)
    files = split_zip("cache.sqlite3", sqlite_bytes, 100_000, entries=[("../x", b"hi")])
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "unsafe_member")


def test_too_many_members_are_refused(target, sqlite_bytes, monkeypatch):
    _with_old(target)
    monkeypatch.setattr(pr, "MAX_ENTRIES", 1)
    files = split_zip("cache.sqlite3", sqlite_bytes, 100_000, entries=[("a", b"1")])
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "too_many_members")


def test_a_member_declared_over_the_size_cap_is_refused(target, sqlite_bytes, monkeypatch):
    _with_old(target)
    monkeypatch.setattr(pr, "MAX_MEMBER_BYTES", len(sqlite_bytes) - 1)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes))), target, "member_too_large")


def test_a_zip_bomb_that_inflates_past_its_declared_size_stops_there(target, sqlite_bytes):
    _with_old(target)
    big = b"\0" * 200_000
    files = single_files("cache.sqlite3", big, usize=1000)
    result = pr.run(str(target), dry_run=False, opener=FakeOpener(files))
    _assert_failed_and_kept(result, target, "member_size_mismatch")


def test_a_bomb_is_stopped_while_inflating_not_after(target, sqlite_bytes, monkeypatch):
    _with_old(target)
    seen = []
    real = pr.zlib.crc32
    files = single_files("cache.sqlite3", bytes(2_000_000), usize=1000)
    monkeypatch.setattr(pr, "CHUNK", 256)
    monkeypatch.setattr(pr.zlib, "crc32", lambda data, value=0: (seen.append(len(data)), real(data, value))[1])
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "member_size_mismatch")
    assert sum(seen) <= 1000, f"{sum(seen)} bytes were inflated for a member that declared 1000"


def test_two_members_with_the_expected_name_are_refused(target, sqlite_bytes):
    _with_old(target)
    files = split_zip("cache.sqlite3", sqlite_bytes, 100_000, entries=[("cache.sqlite3", b"second")])
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "unexpected_member")


def test_a_stored_member_with_two_different_sizes_is_refused_as_invalid(target, sqlite_bytes):
    _with_old(target)
    files = single_files("cache.sqlite3", sqlite_bytes, method=0, usize=len(sqlite_bytes) + 5)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "archive_invalid")


def test_reads_are_never_larger_than_what_is_left_of_the_cap(target, sqlite_bytes, monkeypatch):
    _with_old(target)
    monkeypatch.setattr(pr, "MAX_DOWNLOAD_BYTES", 500)
    asked = []

    class Spy(FakeResponse):
        def read(self, n=-1):
            asked.append(n)
            return super().read(n)

    opener = FakeOpener(single_files("cache.sqlite3", sqlite_bytes),
                        overrides={"cache.zip": lambda url, m, body: Spy(url, body, declared=None)})
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=opener), target, "download_too_large")
    assert asked and max(asked) <= 501


def test_a_member_that_inflates_to_less_than_declared_is_refused(target, sqlite_bytes):
    _with_old(target)
    files = single_files("cache.sqlite3", sqlite_bytes, usize=len(sqlite_bytes) + 10)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "member_size_mismatch")


def test_a_crc_mismatch_is_refused(target, sqlite_bytes):
    _with_old(target)
    files = single_files("cache.sqlite3", sqlite_bytes, crc=12345)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "crc_mismatch")


def test_garbage_that_is_not_a_zip_is_refused(target):
    _with_old(target)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener({"cache.zip": b"not a zip at all" * 10})), target, "archive_invalid")


def test_a_damaged_deflate_stream_is_refused(target, sqlite_bytes):
    _with_old(target)
    data = bytearray(single_zip("cache.sqlite3", sqlite_bytes))
    data[60:80] = b"\xff" * 20
    result = pr.run(str(target), dry_run=False, opener=FakeOpener({"cache.zip": bytes(data)}))
    assert result["ok"] is False and result["reason"] in ("archive_invalid", "crc_mismatch", "member_size_mismatch")
    assert (target / "cache.sqlite3").read_bytes() == OLD


def test_an_encrypted_member_is_refused(target, sqlite_bytes):
    _with_old(target)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes, flags=1))), target, "archive_unsupported")


def test_an_unsupported_compression_method_is_refused(target, sqlite_bytes):
    _with_old(target)
    import struct
    data = bytearray(single_zip("cache.sqlite3", sqlite_bytes))
    cd = data.rfind(b"PK\x01\x02")
    struct.pack_into("<H", data, cd + 10, 12)  # bzip2
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener({"cache.zip": bytes(data)})), target, "archive_unsupported")


def test_a_local_header_that_does_not_match_the_directory_is_refused(target, sqlite_bytes):
    _with_old(target)
    data = bytearray(single_zip("cache.sqlite3", sqlite_bytes))
    data[30:33] = b"xxx"  # the name in the local header
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener({"cache.zip": bytes(data)})), target, "archive_invalid")


# ---------------------------------------------------------------- the extracted index

def test_a_corrupt_sqlite_file_is_refused(target):
    _with_old(target)
    files = single_files("cache.sqlite3", b"this is not a database" * 50)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "index_unreadable")


def test_a_database_cut_in_the_middle_is_refused(target, sqlite_bytes):
    _with_old(target)
    files = single_files("cache.sqlite3", sqlite_bytes[: len(sqlite_bytes) // 2])
    result = pr.run(str(target), dry_run=False, opener=FakeOpener(files))
    assert result["ok"] is False and result["reason"] in ("index_unreadable", "index_schema_unsupported")
    assert (target / "cache.sqlite3").read_bytes() == OLD


def test_a_database_without_the_schema_is_refused(target, tmp_path):
    import sqlite3
    _with_old(target)
    path = tmp_path / "other.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE unrelated (a INTEGER)")
    conn.commit()
    conn.close()
    files = single_files("cache.sqlite3", path.read_bytes())
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "index_schema_unsupported")


def test_an_index_without_parts_is_refused(target, tmp_path):
    import sqlite3
    _with_old(target)
    path = build_fixture(tmp_path / "empty.sqlite3")
    conn = sqlite3.connect(path)
    conn.execute("DELETE FROM components")
    conn.commit()
    conn.close()
    files = single_files("cache.sqlite3", path.read_bytes())
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "index_empty")


def test_a_failed_integrity_check_is_refused(target, sqlite_bytes, monkeypatch):
    _with_old(target)
    real_open = pi.open_index

    class Conn:
        def __init__(self, conn):
            self._c = conn

        def execute(self, sql, *a):
            if "quick_check" in sql:
                return type("R", (), {"fetchone": lambda s: ("row 3 missing from index",)})()
            return self._c.execute(sql, *a)

        def close(self):
            self._c.close()

    monkeypatch.setattr(pi, "open_index", lambda p: (lambda c, cols: (Conn(c), cols))(*real_open(p)))
    files = single_files("cache.sqlite3", sqlite_bytes)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener(files)), target, "index_unreadable")


# ---------------------------------------------------------------- replace

def test_a_failed_replace_keeps_the_old_index_and_cleans_up(target, sqlite_bytes, monkeypatch):
    _with_old(target)

    def deny(src, dst):
        raise PermissionError("in use")

    monkeypatch.setattr(pr.os, "replace", deny)
    result = pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    _assert_failed_and_kept(result, target, "replace_failed")


def test_the_old_index_stays_until_the_new_one_is_valid(target, sqlite_bytes, monkeypatch):
    old = _with_old(target)
    seen = {}
    real = pr._validate

    def spy(path):
        seen["old_during_validation"] = old.read_bytes()
        return real(path)

    monkeypatch.setattr(pr, "_validate", spy)
    pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert seen["old_during_validation"] == OLD


def test_a_second_refresh_at_the_same_time_is_refused(target, sqlite_bytes):
    old = _with_old(target)
    assert pr._LOCK.acquire(blocking=False)
    try:
        result = pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    finally:
        pr._LOCK.release()
    assert result["ok"] is False and result["reason"] == "refresh_in_progress" and old.read_bytes() == OLD
    assert [p.name for p in target.iterdir()] == ["cache.sqlite3"]


def test_a_refresh_refused_by_the_lock_does_not_create_the_directory(target, sqlite_bytes):
    assert pr._LOCK.acquire(blocking=False)
    try:
        result = pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    finally:
        pr._LOCK.release()
    assert result["reason"] == "refresh_in_progress" and not target.exists()


def test_a_deflate_stream_that_ends_early_is_refused_as_invalid(target, sqlite_bytes):
    import struct
    import zlib
    from tests._parts_index_zip import central, end_record
    _with_old(target)
    co = zlib.compressobj(9, zlib.DEFLATED, -15)
    packed = (co.compress(sqlite_bytes) + co.flush())[:-6]
    name = b"cache.sqlite3"
    local = struct.pack("<4s5H3I2H", b"PK", 20, 0, 8, 0, 0x21, zlib.crc32(sqlite_bytes), len(packed), len(sqlite_bytes), len(name), 0) + name + packed
    cd = central(name, 0, 8, zlib.crc32(sqlite_bytes), len(packed), len(sqlite_bytes), 0, 0)
    archive = local + cd + end_record(0, 0, 1, len(cd), len(local))
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=FakeOpener({"cache.zip": archive})), target, "archive_invalid")


def test_the_lock_is_released_after_a_failure(target, sqlite_bytes):
    pr.run(str(target), dry_run=False, opener=FakeOpener({}))
    assert pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))["ok"] is True


def test_a_stale_temporary_folder_is_swept_and_a_fresh_one_is_not(target, sqlite_bytes):
    target.mkdir()
    stale = target / ".refresh-old"
    fresh = target / ".refresh-new"
    for d in (stale, fresh):
        d.mkdir()
        (d / pr.TEMP_MARKER).touch()
        (d / "download.part").write_bytes(b"x")
    past = stale.stat().st_mtime - 3 * 24 * 3600
    os.utime(stale, (past, past))
    pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert not stale.exists() and fresh.exists()


# ---------------------------------------------------------------- target directory

def test_a_relative_target_is_refused(sqlite_bytes, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # outside any repository, so only the relative-path rule can refuse it
    result = pr.run("some/relative/dir", dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert result["ok"] is False and result["reason"] == "target_refused" and result["status"] == "refused"
    assert "not an absolute path" in result["message"] and list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("given", ["\\\\server\\share\\idx", "//server/share/idx", "\\\\?\\UNC\\server\\share", "\\??\\C:\\x"])
def test_a_network_target_is_refused_before_anything_happens(given, sqlite_bytes):
    opener = FakeOpener(single_files("cache.sqlite3", sqlite_bytes))
    result = pr.run(given, dry_run=False, opener=opener)
    assert result["reason"] == "target_refused" and opener.calls == []


def test_a_target_inside_a_git_repository_is_refused(tmp_path, sqlite_bytes):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    opener = FakeOpener(single_files("cache.sqlite3", sqlite_bytes))
    for sub in (repo, repo / "deep" / "er"):
        result = pr.run(str(sub), dry_run=False, opener=opener)
        assert result["reason"] == "target_refused" and "git repository" in result["message"]
    assert opener.calls == [] and not (repo / "deep").exists()


def test_a_dry_run_also_refuses_a_target_inside_a_repository(tmp_path, sqlite_bytes):
    (tmp_path / ".git").mkdir()
    assert pr.run(str(tmp_path / "x"), dry_run=True, opener=FakeOpener({}))["reason"] == "target_refused"


@pytest.mark.parametrize("marker", ["board.kicad_pro", "sheet.kicad_sch", "board.kicad_pcb"])
def test_a_target_inside_a_kicad_project_folder_is_refused(tmp_path, sqlite_bytes, marker):
    project = tmp_path / "proj"
    project.mkdir()
    (project / marker).touch()
    opener = FakeOpener(single_files("cache.sqlite3", sqlite_bytes))
    for sub in (project, project / "cache"):
        result = pr.run(str(sub), dry_run=False, opener=opener)
        assert result["reason"] == "target_refused" and "KiCad project" in result["message"]
    assert opener.calls == []


def test_a_target_that_is_a_file_is_refused(tmp_path):
    f = tmp_path / "file"
    f.write_bytes(b"x")
    assert pr.run(str(f), dry_run=True, opener=FakeOpener({}))["reason"] == "target_refused"


def test_the_default_directory_is_the_platform_data_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    expected = (tmp_path / ("local" if os.name == "nt" else "xdg")) / "boardproof" / "parts-index"
    if os.name != "nt" and os.uname().sysname == "Darwin":
        pytest.skip("the macOS data directory is under the home folder")
    assert pr.default_target_dir() == expected
    assert pr.run(None, dry_run=True, opener=FakeOpener({}))["target_dir"] == str(expected.resolve())


# ---------------------------------------------------------------- live (explicit only)

@pytest.mark.network
def test_the_live_site_answers_head_for_every_segment():
    """HEAD only: never downloads the data. Run with `pytest -m network` and BOARDPROOF_NETWORK_TEST=1."""
    if os.environ.get("BOARDPROOF_NETWORK_TEST") != "1":
        pytest.skip("set BOARDPROOF_NETWORK_TEST=1 to contact the live site")
    segments = pr.probe_segments(pr.build_opener())
    assert segments[-1]["url"].endswith("cache.zip")
    assert all(s["size_bytes"] and s["size_bytes"] > 0 for s in segments)


def test_threading_lock_is_a_real_lock():
    assert isinstance(pr._LOCK, type(threading.Lock()))



# ---------------------------------------------------------------- review round 1 (hostile or broken answers)

def _patched(data: bytes, where: int, fmt: str, value: int) -> bytes:
    out = bytearray(data)
    struct.pack_into(fmt, out, where, value)
    return bytes(out)


def _eocd(data: bytes) -> int:
    return data.rfind(b"PK")


def _cd(data: bytes) -> int:
    return data.rfind(b"PK")


def _run_archive(target, archive: bytes) -> dict:
    _with_old(target)
    return pr.run(str(target), dry_run=False, opener=FakeOpener({"cache.zip": archive}))


def test_a_member_on_a_disk_after_the_last_is_refused_not_an_index_error(target, sqlite_bytes):
    archive = _patched(single_zip("cache.sqlite3", sqlite_bytes), _cd(single_zip("cache.sqlite3", sqlite_bytes)) + 34, "<H", 7)
    _assert_failed_and_kept(_run_archive(target, archive), target, "archive_invalid")


def test_a_central_directory_on_a_disk_after_the_last_is_refused_not_an_index_error(target, sqlite_bytes):
    data = single_zip("cache.sqlite3", sqlite_bytes)
    archive = _patched(data, _eocd(data) + 6, "<H", 5)
    _assert_failed_and_kept(_run_archive(target, archive), target, "archive_invalid")


@pytest.mark.parametrize("shown", ["abc", "١٢٣", "12 3", "-5", "1e3", ""])
def test_a_bad_content_length_on_head_is_reported_in_the_plan(target, sqlite_bytes, shown):
    files = single_files("cache.sqlite3", sqlite_bytes)
    opener = FakeOpener(files, overrides={"cache.zip": lambda url, m, body: FakeResponse(url, b"", declared=shown)})
    result = pr.run(str(target), dry_run=True, opener=opener)
    assert result["ok"] is False and result["would_fail"] is True and result["reason"] == "download_failed"
    assert not target.exists()


@pytest.mark.parametrize("shown", ["abc", "١٢٣"])
def test_a_bad_content_length_on_get_is_a_refusal_not_an_exception(target, sqlite_bytes, shown):
    _with_old(target)
    files = single_files("cache.sqlite3", sqlite_bytes)
    opener = FakeOpener(files, overrides={"cache.zip": lambda url, m, body: FakeResponse(url, body, declared=shown)})
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=opener), target, "download_failed")


@pytest.mark.parametrize("where,fmt,value", [(10, "<H", 0xFFFF), (12, "<I", 0xFFFFFFFF), (16, "<I", 0xFFFFFFFF)])
def test_zip64_markers_in_the_end_record_are_unsupported_not_damaged(target, sqlite_bytes, where, fmt, value):
    data = single_zip("cache.sqlite3", sqlite_bytes)
    _assert_failed_and_kept(_run_archive(target, _patched(data, _eocd(data) + where, fmt, value)), target, "archive_unsupported")


@pytest.mark.parametrize("where", [20, 24])
def test_zip64_member_sizes_are_unsupported(target, sqlite_bytes, where):
    data = single_zip("cache.sqlite3", sqlite_bytes)
    _assert_failed_and_kept(_run_archive(target, _patched(data, _cd(data) + where, "<I", 0xFFFFFFFF)), target, "archive_unsupported")


@pytest.mark.parametrize("comment", [200, 65535])
def test_an_end_record_with_a_comment_is_still_found(target, sqlite_bytes, comment):
    data = single_zip("cache.sqlite3", sqlite_bytes)
    archive = _patched(data, _eocd(data) + 20, "<H", comment) + b"c" * comment
    assert _run_archive(target, archive)["ok"] is True


def test_an_oversized_central_directory_is_refused_before_it_is_read(target, sqlite_bytes):
    data = single_zip("cache.sqlite3", sqlite_bytes)
    archive = _patched(data, _eocd(data) + 12, "<I", 1_400_000_000)
    result = _run_archive(target, archive)
    _assert_failed_and_kept(result, target, "archive_invalid")
    assert "too large" in result["message"]


def test_the_central_directory_cap_applies_to_a_directory_inside_the_file(target, sqlite_bytes, monkeypatch):
    monkeypatch.setattr(pr, "MAX_CD_BYTES", 10)
    result = _run_archive(target, single_zip("cache.sqlite3", sqlite_bytes))
    _assert_failed_and_kept(result, target, "archive_invalid")
    assert "too large" in result["message"]


@pytest.mark.parametrize("path", [
    "%2e%2e/x/cache.zip", "%2E%2E/x/cache.zip", "..%2fx/cache.zip", "a/%2e%2e%2f%2e%2e/cache.zip", "x%5cy/cache.zip", "%2e/cache.zip",
])
def test_check_url_refuses_percent_encoded_dot_segments(path):
    assert pr.check_url(pr.BASE_URL + path) is not None


def test_reads_are_blocks_small_enough_to_check_the_deadline_between(target, sqlite_bytes):
    asked = []

    class Spy(FakeResponse):
        def read(self, n=-1):
            asked.append(n)
            return super().read(n)

    files = single_files("cache.sqlite3", sqlite_bytes)
    opener = FakeOpener(files, overrides={"cache.zip": lambda url, m, body: Spy(url, body)})
    assert pr.run(str(target), dry_run=False, opener=opener)["ok"] is True
    assert asked and max(asked) <= pr.READ_BLOCK <= 65536


def test_a_folder_named_like_a_temporary_one_but_without_our_marker_is_never_swept(target, sqlite_bytes):
    target.mkdir()
    users = target / ".refresh-mine"
    users.mkdir()
    (users / "notes.txt").write_bytes(b"important")
    past = users.stat().st_mtime - 5 * 24 * 3600
    os.utime(users, (past, past))
    pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert (users / "notes.txt").read_bytes() == b"important"


def test_our_temporary_folder_carries_the_marker_while_it_exists(target, sqlite_bytes, monkeypatch):
    seen = {}
    real = pr._validate

    def spy(path):
        seen["marker"] = (path.parent / pr.TEMP_MARKER).exists()
        return real(path)

    monkeypatch.setattr(pr, "_validate", spy)
    pr.run(str(target), dry_run=False, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert seen["marker"] is True


# ---------------------------------------------------------------- review round 2

@pytest.mark.parametrize("shown", ["9" * 5000, "1" * 13, "9" * 4301])
def test_an_enormous_content_length_is_a_refusal_on_head_and_get(target, sqlite_bytes, shown):
    files = single_files("cache.sqlite3", sqlite_bytes)
    opener = FakeOpener(files, overrides={"cache.zip": lambda url, m, body: FakeResponse(url, b"" if m == "HEAD" else body, declared=shown)})
    plan = pr.run(str(target), dry_run=True, opener=opener)
    assert plan["ok"] is False and plan["would_fail"] is True and plan["reason"] == "download_failed"
    _with_old(target)
    _assert_failed_and_kept(pr.run(str(target), dry_run=False, opener=opener), target, "download_failed")


def test_a_plausible_content_length_is_still_read(target, sqlite_bytes):
    assert pr.run(str(target), dry_run=True, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))["ok"] is True


def test_a_probe_failed_plan_offers_no_next_step(target):
    result = pr.run(str(target), dry_run=True, opener=FakeOpener({}))
    assert result["status"] == "probe_failed" and "next_step" not in result


def test_a_plan_refused_as_too_large_offers_no_next_step(target, sqlite_bytes):
    files = single_files("cache.sqlite3", sqlite_bytes)
    opener = FakeOpener(files, overrides={"cache.zip": lambda url, m, body: FakeResponse(url, b"", declared=2_000_000_000)})
    result = pr.run(str(target), dry_run=True, opener=opener)
    assert result["ok"] is False and result["status"] == "refused" and result["reason"] == "download_too_large"
    assert "next_step" not in result


def test_a_good_plan_offers_the_next_step(target, sqlite_bytes):
    result = pr.run(str(target), dry_run=True, opener=FakeOpener(single_files("cache.sqlite3", sqlite_bytes)))
    assert "dry_run=false" in result["next_step"]


def test_a_central_directory_of_exactly_the_cap_is_not_called_too_large(target, sqlite_bytes):
    data = single_zip("cache.sqlite3", sqlite_bytes)
    result = _run_archive(target, _patched(data, _eocd(data) + 12, "<I", pr.MAX_CD_BYTES))
    _assert_failed_and_kept(result, target, "archive_invalid")
    assert "too large" not in result["message"] and "outside the file" in result["message"]


def test_a_central_directory_one_over_the_cap_is_too_large(target, sqlite_bytes):
    data = single_zip("cache.sqlite3", sqlite_bytes)
    result = _run_archive(target, _patched(data, _eocd(data) + 12, "<I", pr.MAX_CD_BYTES + 1))
    _assert_failed_and_kept(result, target, "archive_invalid")
    assert "too large" in result["message"]


def test_a_symbolic_link_named_like_a_temporary_folder_is_never_swept(target, sqlite_bytes, monkeypatch):
    target.mkdir()
    old = target / ".refresh-link"
    old.mkdir()
    (old / pr.TEMP_MARKER).touch()
    (old / "keep.txt").write_bytes(b"keep")
    past = old.stat().st_mtime - 5 * 24 * 3600
    os.utime(old, (past, past))
    real = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda self: True if self.name == ".refresh-link" else real(self))
    pr._sweep_stale_temps(target)
    assert (old / "keep.txt").read_bytes() == b"keep"


# ---------------------------------------------------------------- review round 3 (boundaries)

def _head_says(shown):
    files = single_files("cache.sqlite3", b"x")
    return FakeOpener(files, overrides={"cache.zip": lambda url, m, body: FakeResponse(url, b"" if m == "HEAD" else body, declared=shown)})


def test_a_12_digit_content_length_is_read_and_judged_by_the_cap(target):
    result = pr.run(str(target), dry_run=True, opener=_head_says("999999999999"))
    assert result["status"] == "refused" and result["reason"] == "download_too_large" and result["download"]["sizes_bytes"] == [999999999999]


def test_a_12_digit_content_length_with_leading_zeros_is_a_small_size(target):
    result = pr.run(str(target), dry_run=True, opener=_head_says("000000000005"))
    assert result["status"] == "planned" and result["download"]["sizes_bytes"] == [5]


def test_a_13_digit_content_length_is_still_a_probe_failure(target):
    result = pr.run(str(target), dry_run=True, opener=_head_says("1" * 13))
    assert result["status"] == "probe_failed" and result["reason"] == "download_failed"


def test_a_12_digit_content_length_on_get_is_judged_by_the_cap(target):
    _with_old(target)
    result = pr.run(str(target), dry_run=False, opener=_head_says("999999999999"))
    _assert_failed_and_kept(result, target, "download_too_large")


def test_the_total_cap_boundary_in_the_plan(target):
    assert pr.run(str(target), dry_run=True, opener=_head_says("1500000000"))["status"] == "planned"
    over = pr.run(str(target), dry_run=True, opener=_head_says("1500000001"))
    assert over["status"] == "refused" and over["reason"] == "download_too_large"


def test_a_padded_content_length_is_read(target):
    result = pr.run(str(target), dry_run=True, opener=_head_says(" 123 "))
    assert result["status"] == "planned" and result["download"]["sizes_bytes"] == [123]

