"""Build ticket 42: `scripts/check_jlcpcb_tiers.py`, the read-only tier cross-check, on small fixture CSVs.

Offline: the fetch path runs against a fake opener. A live fetch is a network test, skipped by default.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_jlcpcb_tiers", ROOT / "scripts" / "check_jlcpcb_tiers.py")
chk = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chk)

HEADER = "lcsc,package,mfr,manufacturer,library_type,preferred,stock,price,description,attributes"


def _csv(*rows: str, header: str = HEADER, eol: str = "\n") -> bytes:
    return (eol.join([header, *rows]) + eol).encode("utf-8")


def _row(lcsc: str, lib: str = "base", pref: str = "0") -> str:
    return f'{lcsc},0603,"R, thick",Acme,{lib},{pref},12345,0.002,"a, b","{{}}"'


def _index(tmp_path: Path, **tiers: str) -> Path:
    path = tmp_path / "index.json"
    path.write_text(json.dumps({"parts": [{"lcsc": k, "tier": v, "mpn": "X"} for k, v in tiers.items()]}), encoding="utf-8")
    return path


class TestParse:
    def test_tiers(self):
        got = chk.parse_csv(_csv(_row("C1", "base", "0"), _row("C2", "expand", "1"), _row("C3", "expand", "0"),
                                 _row("C4", "base", "1")))
        assert got == {"C1": "basic", "C2": "preferred", "C3": "extended", "C4": "basic"}

    def test_bom_crlf_quoted_commas_and_column_order(self):
        header = "preferred,description,library_type,lcsc"
        data = b"\xef\xbb\xbf" + _csv('0,"x, y",base,C7', header=header, eol="\r\n")
        assert chk.parse_csv(data) == {"C7": "basic"}

    def test_numeric_lcsc_and_case_are_normalised(self):
        assert chk.parse_csv(_csv(_row("7"), _row("c8"))) == {"C7": "basic", "C8": "basic"}

    @pytest.mark.parametrize("data,why", [
        (b"", "empty"),
        (_csv(), "no rows"),
        (_csv(_row("C1"), header="lcsc,library_type"), "missing column"),
        (_csv("C1,C1,base,0", header="lcsc,lcsc,library_type,preferred"), "duplicate column"),
        (_csv(_row("C1"), _row("C1")), "duplicate lcsc"),
        (_csv(_row("")), "empty lcsc"),
        (_csv(_row("XYZ")), "bad lcsc"),
        (_csv(_row("C1", "weird")), "bad library_type"),
        (_csv(_row("C1", "base", "yes")), "bad preferred"),
        (_csv(_row("C1") + ",extra"), "wrong field count"),
        (_csv("C1,base"), "short row"),
        (_csv(_row("C1").replace("Acme", "Ac" + chr(0) + "me")), "nul"),
        (b"\xff\xfe" + _csv(_row("C1")), "not utf8"),
        (_csv(_row("C1").replace('"R, thick"', '"R "thick"')), "bad quoting"),
    ])
    def test_malformed_is_refused(self, data, why):
        with pytest.raises(chk.InputError):
            chk.parse_csv(data)

    def test_trailing_blank_line_is_fine(self):
        assert chk.parse_csv(_csv(_row("C1")) + b"\n") == {"C1": "basic"}


class TestCompare:
    def test_three_reports(self, tmp_path):
        index = chk.load_index(_index(tmp_path, C1="basic", C2="basic", C3="preferred", C4="extended", C5="preferred"))
        csv_tiers = chk.parse_csv(_csv(_row("C1"), _row("C2", "expand", "1"), _row("C3", "expand", "1"),
                                       _row("C9", "expand", "1")))
        result = chk.compare(index, csv_tiers)
        assert result["tier_changes"] == [("C2", "basic", "preferred")]
        assert result["absent_from_csv"] == [("C4", "extended"), ("C5", "preferred")]
        assert result["missing_from_index"] == [("C9", "preferred")]
        text = chk.format_report(result, 20)
        assert "tier changes: 1" in text and "basic -> preferred: 1" in text
        assert "absent from the CSV: 2 (basic or preferred in the index: 1; extended, as expected: 1)" in text
        assert "C5 (preferred in the index)" in text and "C4 (extended" not in text
        assert "missing from the index: 1" in text

    def test_lists_are_capped_and_numerically_sorted(self):
        index = {f"C{n}": "basic" for n in (2, 10, 1)}
        csv_tiers = {f"C{n}": "preferred" for n in (2, 10, 1)}
        text = chk.format_report(chk.compare(index, csv_tiers), 2)
        assert text.index("C1:") < text.index("C2:")
        assert "C10:" not in text and "... and 1 more" in text

    def test_output_has_no_stock_or_price(self):
        text = chk.format_report(chk.compare({"C1": "basic"}, chk.parse_csv(_csv(_row("C1", "expand", "1")))), 20)
        assert "12345" not in text and "0.002" not in text


class TestIndex:
    @pytest.mark.parametrize("payload", ["not json", "{}", '{"parts": []}', '{"parts": [{"lcsc": "C1", "tier": "gold"}]}',
                                         '{"parts": [{"tier": "basic"}]}'])
    def test_unreadable_index_is_refused(self, tmp_path, payload):
        path = tmp_path / "i.json"
        path.write_text(payload, encoding="utf-8")
        with pytest.raises(chk.InputError):
            chk.load_index(path)

    def test_missing_index_is_refused(self, tmp_path):
        with pytest.raises(chk.InputError):
            chk.load_index(tmp_path / "nope.json")

    def test_committed_index_loads(self):
        assert len(chk.load_index(chk.DEFAULT_INDEX)) > 800


    @pytest.mark.parametrize("tiers", [("basic", "basic"), ("basic", "preferred")])
    def test_duplicate_index_entry_is_refused(self, tmp_path, tiers):
        path = tmp_path / "d.json"
        path.write_text(json.dumps({"parts": [{"lcsc": "C1", "tier": tiers[0]}, {"lcsc": "c1", "tier": tiers[1]}]}),
                        encoding="utf-8")
        with pytest.raises(chk.InputError, match="twice"):
            chk.load_index(path)


class TestUrl:
    @pytest.mark.parametrize("url", [
        chk.DEFAULT_URL,
        "https://github.com/CDFER/jlcpcb-parts-database/raw/main/x.csv",
    ])
    def test_allowed(self, url):
        assert chk.check_url(url) is None

    @pytest.mark.parametrize("url", [
        "http://raw.githubusercontent.com/a/b.csv",
        "ftp://raw.githubusercontent.com/a/b.csv",
        "file:///etc/passwd",
        "https://evil.example/a.csv",
        "https://raw.githubusercontent.com.evil.example/a.csv",
        "https://user:pw" + chr(64) + "raw.githubusercontent.com/a.csv",
        "https://raw.githubusercontent.com" + chr(64) + "evil.example/a.csv",
        "https://raw.githubusercontent.com:8443/a.csv",
        "https://raw.githubusercontent.com/a b.csv",
        "https://raw.githubusercontent.com/a\n.csv",
        "",
    ])
    def test_refused(self, url):
        assert chk.check_url(url)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class _Opener:
    def __init__(self, data=b"", exc=None):
        self.data, self.exc, self.calls = data, exc, []

    def open(self, request, timeout=None):
        self.calls.append((request.full_url, timeout))
        if self.exc:
            raise self.exc
        return _Resp(self.data)


class _RecordingResp(_Resp):
    sizes: list = []

    def read(self, size=-1):
        type(self).sizes.append(size)
        return super().read(size)


class _RecordingOpener(_Opener):
    def open(self, request, timeout=None):
        return _RecordingResp(self.data)


class TestFetch:
    def test_read_is_bounded_by_cap_plus_one(self):
        _RecordingResp.sizes = []
        chk.fetch_csv(chk.DEFAULT_URL, opener=_RecordingOpener(b"x" * 5), max_bytes=10)
        assert _RecordingResp.sizes == [11]
        with pytest.raises(chk.InputError, match="larger"):
            chk.fetch_csv(chk.DEFAULT_URL, opener=_RecordingOpener(b"x" * 50), max_bytes=10)

    def test_returns_bytes_and_passes_timeout(self):
        opener = _Opener(b"abc")
        assert chk.fetch_csv(chk.DEFAULT_URL, opener=opener) == b"abc"
        assert opener.calls == [(chk.DEFAULT_URL, chk.TIMEOUT_S)]

    def test_size_cap(self):
        with pytest.raises(chk.InputError, match="larger"):
            chk.fetch_csv(chk.DEFAULT_URL, opener=_Opener(b"x" * 11), max_bytes=10)
        assert chk.fetch_csv(chk.DEFAULT_URL, opener=_Opener(b"x" * 10), max_bytes=10) == b"x" * 10

    def test_bad_url_never_opens(self):
        opener = _Opener(b"abc")
        with pytest.raises(chk.InputError):
            chk.fetch_csv("https://evil.example/x", opener=opener)
        assert opener.calls == []

    def test_network_error_is_input_error(self):
        with pytest.raises(chk.InputError, match="failed"):
            chk.fetch_csv(chk.DEFAULT_URL, opener=_Opener(exc=urllib.error.URLError("down")))

    def test_redirect_off_allow_list_is_refused(self):
        handler = chk._CheckedRedirect()
        with pytest.raises(urllib.error.URLError, match="redirect refused"):
            handler.redirect_request(None, None, 302, "Found", {}, "https://evil.example/x")
        with pytest.raises(urllib.error.URLError, match="redirect refused"):
            handler.redirect_request(None, None, 302, "Found", {}, "http://raw.githubusercontent.com/x")

    @pytest.mark.network
    @pytest.mark.skipif(os.environ.get("BOARDPROOF_NETWORK_TESTS") != "1", reason="live fetch; set BOARDPROOF_NETWORK_TESTS=1")
    def test_live_default_url_parses(self):
        assert chk.parse_csv(chk.fetch_csv(chk.DEFAULT_URL))


class TestMain:
    def test_report_exit_zero_and_nothing_written(self, tmp_path, capsys):
        index = _index(tmp_path, C1="basic", C2="preferred")
        csv_path = tmp_path / "p.csv"
        csv_path.write_bytes(_csv(_row("C1", "expand", "1"), _row("C3")))
        before = {p: p.read_bytes() for p in (index, csv_path)}
        files = set(tmp_path.iterdir())
        assert chk.main(["--csv", str(csv_path), "--index", str(index)]) == 0
        out = capsys.readouterr().out
        assert "tier changes: 1" in out and "basic -> preferred: 1" in out
        assert "missing from the index: 1" in out
        assert {p: p.read_bytes() for p in before} == before and set(tmp_path.iterdir()) == files

    def test_clean_run_is_exit_zero(self, tmp_path):
        index = _index(tmp_path, C1="basic")
        csv_path = tmp_path / "p.csv"
        csv_path.write_bytes(_csv(_row("C1")))
        assert chk.main(["--csv", str(csv_path), "--index", str(index)]) == 0

    def test_unreadable_inputs_exit_two(self, tmp_path, capsys):
        index = _index(tmp_path, C1="basic")
        bad = tmp_path / "bad.csv"
        bad.write_bytes(_csv(_row("C1", "weird")))
        assert chk.main(["--csv", str(bad), "--index", str(index)]) == 2
        assert chk.main(["--csv", str(tmp_path / "nope.csv"), "--index", str(index)]) == 2
        good = tmp_path / "good.csv"
        good.write_bytes(_csv(_row("C1")))
        assert chk.main(["--csv", str(good), "--index", str(tmp_path / "nope.json")]) == 2
        assert chk.main(["--csv", str(good), "--index", str(index), "--sample", "-1"]) == 2
        assert capsys.readouterr().err.count("check_jlcpcb_tiers:") == 4

    def test_oversized_local_csv_is_refused(self, tmp_path, monkeypatch):
        monkeypatch.setattr(chk, "MAX_BYTES", 10)
        index = _index(tmp_path, C1="basic")
        big = tmp_path / "big.csv"
        big.write_bytes(_csv(_row("C1")))
        assert chk.main(["--csv", str(big), "--index", str(index)]) == 2

    def test_fetch_goes_through_fetch_csv(self, tmp_path, monkeypatch, capsys):
        index = _index(tmp_path, C1="basic")
        seen = []
        monkeypatch.setattr(chk, "fetch_csv", lambda url: seen.append(url) or _csv(_row("C1")))
        assert chk.main(["--fetch", "--index", str(index)]) == 0
        assert seen == [chk.DEFAULT_URL]

    def test_url_with_csv_is_refused(self, tmp_path, capsys):
        index = _index(tmp_path, C1="basic")
        csv_path = tmp_path / "p.csv"
        csv_path.write_bytes(_csv(_row("C1")))
        assert chk.main(["--csv", str(csv_path), "--index", str(index), "--url", "https://github.com/x.csv"]) == 2
        assert "--url only goes with --fetch" in capsys.readouterr().err

    def test_csv_and_fetch_are_exclusive(self, tmp_path):
        with pytest.raises(SystemExit):
            chk.main(["--csv", "a", "--fetch"])
        with pytest.raises(SystemExit):
            chk.main([])
