"""Issue 172: the write layer's parse check of a schematic runs ``kicad-cli sch upgrade`` once per distinct
sheet CONTENT and kicad-cli identity, not once per save.

Every save checks the written sheet and every sub-sheet below it. The sub-sheets of a tree do not change
when one sheet is saved, so a second save re-ran 35 identical upgrades on vme-wren (about 20 s measured).
The memo remembers only "these exact bytes loaded" (never a failure), keyed by the sha256 of the bytes, the
kind, the kicad-cli executable (path, size, mtime) and the ``KICAD*`` environment. Each guard here has a
mutant (named in the docstring) that turns its test red.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from kicad_tools.sexp import Document, SExp

from boardproof import write_layer as wl


def _project(kicad_demo) -> Path:
    return kicad_demo("complex_hierarchy") / "complex_hierarchy.kicad_sch"


def _count_upgrades(monkeypatch) -> list[list[str]]:
    seen: list[list[str]] = []
    real = subprocess.run

    def counting(cmd, *args, **kwargs):
        if "upgrade" in cmd:
            seen.append([str(c) for c in cmd])
        return real(cmd, *args, **kwargs)

    monkeypatch.setattr(wl.subprocess, "run", counting)
    return seen


def _break(sheet: Path) -> None:
    """A sub-sheet KiCad cannot load, made through kicad_tools' Document API (as test_write_layer does)."""
    doc = Document.load(sheet)
    doc.root.children.append(SExp.list("boardproof_marker", 1))
    doc.save(sheet)


def test_a_second_save_does_not_recheck_unchanged_sub_sheets(kicad_demo, monkeypatch) -> None:
    root = _project(kicad_demo)
    subs = wl._sub_sheets(wl.load(root))
    assert subs, "the fixture must have sub-sheets"
    wl.clear_load_check_memo()
    seen = _count_upgrades(monkeypatch)
    wl.save(wl.load(root), root)
    first = len(seen)
    assert first >= 1 + len(set(subs))
    seen.clear()
    wl.save(wl.load(root), root)
    # only the written sheet is new bytes or not: the same bytes were just saved, so none may run at all
    assert len(seen) < first
    assert not any(Path(c[-1]).name in set(subs) for c in seen)


def test_changed_sub_sheet_bytes_are_checked_again(kicad_demo, monkeypatch) -> None:
    """Mutant M1: the memo key without the sha256 of the bytes (a path key) lets a broken sub-sheet pass."""
    root = _project(kicad_demo)
    wl.clear_load_check_memo()
    wl.save(wl.load(root), root)  # every sheet passes and is remembered
    sub = root.parent / wl._sub_sheets(wl.load(root))[0]
    _break(sub)
    before = root.read_bytes()
    with pytest.raises(wl.WriteLayerError) as raised:
        wl.save(wl.load(root), root)
    assert raised.value.code == "parse_check_failed"
    assert root.read_bytes() == before


def test_a_failed_check_is_never_remembered(kicad_demo, monkeypatch) -> None:
    """Mutant M2: remembering the bytes before the exit code is read."""
    root = _project(kicad_demo)
    wl.clear_load_check_memo()
    sub = root.parent / wl._sub_sheets(wl.load(root))[0]
    _break(sub)
    for _ in range(2):
        with pytest.raises(wl.WriteLayerError) as raised:
            wl.save(wl.load(root), root)
        assert raised.value.code == "parse_check_failed"


def test_a_change_of_the_kicad_environment_or_executable_misses(kicad_demo, monkeypatch) -> None:
    """Mutant M3: a key without the KICAD* environment (the same bytes under another KiCad setup)."""
    root = _project(kicad_demo)
    wl.clear_load_check_memo()
    wl.save(wl.load(root), root)
    seen = _count_upgrades(monkeypatch)
    wl.save(wl.load(root), root)
    quiet = len(seen)
    monkeypatch.setenv("BOARDPROOF_TEST_MARKER", "other")
    seen.clear()
    wl.save(wl.load(root), root)
    assert len(seen) > quiet


def test_boards_and_libraries_are_never_memoised(kicad_demo, monkeypatch) -> None:
    """Mutant M4: the memo also for ``pcb`` (board writers keep the real check every time)."""
    pcb = next(kicad_demo("pic_programmer").glob("*.kicad_pcb"))
    seen = _count_upgrades(monkeypatch)
    wl.clear_load_check_memo()
    wl.save(wl.load(pcb), pcb)
    wl.save(wl.load(pcb), pcb)
    assert len([c for c in seen if "pcb" in c]) == 2


def test_the_memo_is_bounded(monkeypatch) -> None:
    """Mutant M5: no bound (a long-lived server grows without limit)."""
    wl.clear_load_check_memo()
    monkeypatch.setattr(wl, "_LOAD_CHECK_MEMO_MAX", 3)
    for i in range(10):
        wl._remember_load_ok(("k", i))
    assert wl._load_check_memo_size() <= 3


def test_the_key_names_the_executable(tmp_path: Path) -> None:
    """Mutant M6: a key without the kicad-cli identity (a KiCad upgrade in a long-lived server)."""
    sheet = tmp_path / "a.kicad_sch"
    sheet.write_bytes(b"(kicad_sch)")
    cli_a, cli_b = tmp_path / "cli-a.exe", tmp_path / "cli-b.exe"
    cli_a.write_bytes(b"a")
    cli_b.write_bytes(b"bb")
    assert wl._load_check_key("sch", sheet, cli_a) != wl._load_check_key("sch", sheet, cli_b)
    assert wl._load_check_key("sch", sheet, cli_a) == wl._load_check_key("sch", sheet, cli_a)


def test_the_key_names_the_executable_size_and_mtime(tmp_path: Path) -> None:
    """Issue 176. Mutant M7: a key without the executable's mtime_ns. Mutant M8: a key without its size
    (and mtime). Each part of the identity alone must change the key."""
    import os

    plain = tmp_path / "plain.txt"
    plain.write_bytes(b"bytes that only get hashed")
    cli = tmp_path / "cli.exe"
    cli.write_bytes(b"aa")
    os.utime(cli, ns=(1_000_000_000, 1_000_000_000))
    base = wl._load_check_key("sch", plain, cli)
    assert base is not None
    # same path, same size, another mtime_ns
    os.utime(cli, ns=(2_000_000_000, 2_000_000_000))
    assert wl._load_check_key("sch", plain, cli) != base
    # same path, another size, the first mtime_ns again
    cli.write_bytes(b"aaaa")
    os.utime(cli, ns=(1_000_000_000, 1_000_000_000))
    assert wl._load_check_key("sch", plain, cli) != base


def test_the_autouse_fixture_clears_the_memo(request) -> None:
    """Issue 176. Mutant M9: the autouse fixture in tests/conftest.py without ``clear_load_check_memo()``.
    The fixture's own generator is run here after the memo was filled: it must leave the memo empty (every
    other memo test clears the memo itself, so removing the call showed nothing before)."""
    name = "_write_layer_load_memo_is_per_test"
    defs = request._fixturemanager.getfixturedefs(name, request.node)
    assert defs, "the autouse fixture is gone from tests/conftest.py"
    assert name in request.fixturenames or any(d.argname == name for d in defs)
    wl._remember_load_ok(("leftover", 1))
    assert wl._load_check_memo_size() >= 1
    gen = defs[-1].func()
    next(gen)
    try:
        assert wl._load_check_memo_size() == 0
    finally:
        gen.close()
