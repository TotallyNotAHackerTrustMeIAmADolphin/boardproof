"""Publish-prep ticket 03b: `scripts/refresh_jlcpcb_index.py` on a small fake library tree.

The library is built through `kicad_tools`' object model under `tmp_path`; nothing here reads the
real submodule (tests/test_jlcpcb_basic.py has the skip-if-absent tests for that).
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest
from test_jlcpcb_basic import _symbol, _write_symbol_lib

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("refresh_jlcpcb_index", ROOT / "scripts" / "refresh_jlcpcb_index.py")
refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh)

COMMIT_A = "a" * 40
COMMIT_B = "b" * 40


def _write_lib(path: Path, *symbols) -> None:
    _write_symbol_lib(path, *symbols)


def _library(root: Path, *, c2_class: str = "Preferred Component", with_c3: bool = False) -> Path:
    symbols = root / "symbols"
    symbols.mkdir(parents=True)
    parts = [
        _symbol("R1", LCSC="C1", Part="RES1", Manufacturer="m", Class="Basic Component",
                Footprint="PCM_JLCPCB:R0603", Stock="999", Price="0.001USD"),
        _symbol("R2", LCSC="C2", Part="RES2", Manufacturer="m", Class=c2_class, Footprint="PCM_JLCPCB:R0805"),
    ]
    if with_c3:
        parts.append(_symbol("R3", LCSC="C3", Part="RES3", Manufacturer="m", Class="Basic Component",
                             Footprint="PCM_JLCPCB:R1206"))
    _write_lib(symbols / "JLCPCB-Resistors.kicad_sym", *parts)
    return root


def _run(capsys, *args):
    code = refresh.main([str(a) for a in args])
    out = capsys.readouterr()
    return code, out.out, out.err


def _first_index(tmp_path, capsys) -> Path:
    lib = _library(tmp_path / "lib1")
    index = tmp_path / "index.json"
    code, out, err = _run(capsys, "--library", lib, "--commit", COMMIT_A, "--index", index)
    assert code == 0, (out, err)
    return index


def test_first_run_writes_an_index_that_records_its_commit(tmp_path, capsys):
    index = _first_index(tmp_path, capsys)
    payload = json.loads(index.read_text(encoding="utf-8"))
    assert payload["source"]["commit"] == COMMIT_A
    assert payload["counts"]["total"] == 2


def test_the_index_never_carries_stock_or_price(tmp_path, capsys):
    index = _first_index(tmp_path, capsys)
    text = index.read_text(encoding="utf-8").lower()
    for part in json.loads(text)["parts"]:
        assert not {"stock", "price"} & set(part)
    assert "999" not in text and "0.001usd" not in text


def test_a_second_run_prints_the_tier_changes_and_the_commit_move(tmp_path, capsys):
    index = _first_index(tmp_path, capsys)
    lib2 = _library(tmp_path / "lib2", c2_class="Extended Component", with_c3=True)

    code, out, _ = _run(capsys, "--library", lib2, "--commit", COMMIT_B, "--index", index)

    assert code == 0
    assert f"commit: {COMMIT_A[:7]} -> {COMMIT_B[:7]}" in out
    assert "parts: 2 -> 3" in out
    assert "tier changes: 1" in out
    assert "preferred -> extended: 1" in out
    assert "C2" in out  # the sample names the part
    assert "added: 1" in out and "C3" in out
    assert "removed: 0" in out
    payload = json.loads(index.read_text(encoding="utf-8"))
    assert payload["source"]["commit"] == COMMIT_B
    assert {p["lcsc"]: p["tier"] for p in payload["parts"]}["C2"] == "extended"


def test_dry_run_prints_the_diff_and_leaves_the_index_untouched(tmp_path, capsys):
    index = _first_index(tmp_path, capsys)
    before = index.read_bytes()
    lib2 = _library(tmp_path / "lib2", c2_class="Extended Component")

    code, out, _ = _run(capsys, "--library", lib2, "--commit", COMMIT_B, "--index", index, "--dry-run")

    assert code == 0
    assert "preferred -> extended: 1" in out
    assert "dry run" in out
    assert index.read_bytes() == before


def test_an_empty_library_directory_is_reported_with_the_fetch_command_and_changes_nothing(tmp_path, capsys):
    index = _first_index(tmp_path, capsys)
    before = index.read_bytes()
    empty = tmp_path / "empty-submodule"
    empty.mkdir()

    code, out, err = _run(capsys, "--library", empty, "--commit", COMMIT_B, "--index", index)

    assert code == 2
    assert "git submodule update --init --depth 1 third_party/JLCPCB-Kicad-Library" in err
    assert index.read_bytes() == before


def test_a_custom_library_without_a_commit_is_refused(tmp_path, capsys):
    lib = _library(tmp_path / "lib")
    index = tmp_path / "index.json"

    code, out, err = _run(capsys, "--library", lib, "--index", index)

    assert code == 2
    assert "--commit" in err
    assert not index.exists()


def test_a_malformed_commit_is_refused(tmp_path, capsys):
    lib = _library(tmp_path / "lib")

    code, _, err = _run(capsys, "--library", lib, "--commit", "main", "--index", tmp_path / "i.json")

    assert code == 2
    assert "40" in err


def test_a_library_that_yields_no_parts_never_replaces_a_good_index(tmp_path, capsys):
    index = _first_index(tmp_path, capsys)
    before = index.read_bytes()
    bad = tmp_path / "bad"
    (bad / "symbols").mkdir(parents=True)
    _write_lib(bad / "symbols" / "JLCPCB-Empty.kicad_sym")  # a library with no symbols

    code, out, err = _run(capsys, "--library", bad, "--commit", COMMIT_B, "--index", index)

    assert code == 2
    assert index.read_bytes() == before


def test_the_write_is_verified_before_it_replaces_the_index(tmp_path, capsys, monkeypatch):
    """Self-check: a corrupted write is caught, reported as write_not_verified, original untouched."""
    index = _first_index(tmp_path, capsys)
    before = index.read_bytes()
    lib2 = _library(tmp_path / "lib2", c2_class="Extended Component")
    real_write = refresh.jlc.write_index

    def corrupting_write(payload, path=None):
        result = real_write(payload, path)
        Path(path).write_text('{"parts": []}', encoding="utf-8")  # a truncated / wrong write
        return result

    monkeypatch.setattr(refresh.jlc, "write_index", corrupting_write)

    code, out, err = _run(capsys, "--library", lib2, "--commit", COMMIT_B, "--index", index)

    assert code == 2
    assert "write_not_verified" in err
    assert index.read_bytes() == before
    assert not list(tmp_path.glob("index.json.*"))  # the temporary sibling is gone


def test_the_default_library_is_the_submodule_path_and_is_pinned_by_the_gitlink():
    assert refresh.DEFAULT_LIBRARY == ROOT / "third_party" / "JLCPCB-Kicad-Library"
    # The recorded commit of the vendored index is the commit the repository pins.
    pinned = subprocess.run(
        ["git", "rev-parse", "HEAD:third_party/JLCPCB-Kicad-Library"],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert pinned.returncode == 0, pinned.stderr
    recorded = json.loads(refresh.jlc.VENDORED_INDEX_PATH.read_text(encoding="utf-8"))["source"]["commit"]
    assert recorded == pinned.stdout.strip()


def test_a_library_that_lost_most_of_its_parts_is_refused_unless_allowed(tmp_path, capsys):
    """Upstream commit de4e6c4 emptied 17 of 20 symbol libraries; a refresh must not follow it silently."""
    index = _first_index(tmp_path, capsys)  # 2 parts
    before = index.read_bytes()
    shrunk = tmp_path / "shrunk"
    (shrunk / "symbols").mkdir(parents=True)
    _write_lib(shrunk / "symbols" / "JLCPCB-Resistors.kicad_sym",
               _symbol("R1", LCSC="C1", Part="RES1", Class="Basic Component", Footprint="PCM_JLCPCB:R0603"))

    code, out, err = _run(capsys, "--library", shrunk, "--commit", COMMIT_B, "--index", index)

    assert code == 2
    assert "index_would_shrink" in err and "--allow-shrink" in err
    assert "removed: 1" in out  # the diff is shown before the refusal
    assert index.read_bytes() == before

    code, out, err = _run(capsys, "--library", shrunk, "--commit", COMMIT_B, "--index", index, "--allow-shrink")

    assert code == 0, err
    assert json.loads(index.read_text(encoding="utf-8"))["counts"]["total"] == 1


def test_the_shrink_guard_also_applies_to_a_dry_run(tmp_path, capsys):
    index = _first_index(tmp_path, capsys)
    shrunk = tmp_path / "shrunk"
    (shrunk / "symbols").mkdir(parents=True)
    _write_lib(shrunk / "symbols" / "JLCPCB-Resistors.kicad_sym",
               _symbol("R1", LCSC="C1", Part="RES1", Class="Basic Component", Footprint="PCM_JLCPCB:R0603"))

    code, _, err = _run(capsys, "--library", shrunk, "--commit", COMMIT_B, "--index", index, "--dry-run")

    assert code == 2 and "index_would_shrink" in err


@pytest.mark.parametrize(
    ("marker", "expected"),
    [("-", "git submodule update --init --depth 1 third_party/JLCPCB-Kicad-Library"),
     ("+", "differs from the pinned commit"), (" ", None)],
)
def test_the_default_run_reads_the_pin_from_the_gitlink_and_refuses_a_wrong_checkout(monkeypatch, marker, expected):
    sha = "c" * 40

    def fake_run(cmd, **_kw):
        nl = chr(10)
        out = sha + nl if "rev-parse" in cmd else f"{marker}{sha} third_party/JLCPCB-Kicad-Library (x)" + nl
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    monkeypatch.setattr(refresh.subprocess, "run", fake_run)
    commit, problem = refresh._gitlink_commit()
    if expected is None:
        assert (commit, problem) == (sha, "")
    else:
        assert commit is None and expected in problem
