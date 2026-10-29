"""Build ticket 34: tests find KiCad's demos from the install, never from a hard-coded path.

The discovery rule (``KICAD_DEMOS_DIR`` first, then ``<prefix>/share/kicad/demos`` next to
``<prefix>/bin/kicad-cli``), the ``KICAD_DEMOS_REQUIRED`` flag (missing demos fail instead of
skip), the copy-before-write guarantee, and the ratchet that no test reaches a path in a
user's home directory.
"""
from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path, PurePosixPath, PureWindowsPath

import pytest

from tests import _kicad_demos as demos

pytest_plugins = ["pytester"]  # the conftest wiring is tested in a nested pytest run

ROOT = Path(__file__).resolve().parents[1]


def _no_cli():
    raise AssertionError("kicad-cli must not be consulted when KICAD_DEMOS_DIR is set")


def _cli_missing():
    from kicad_mcp.kicad_cli import KicadCliError

    raise KicadCliError("kicad-cli not found")


def _assert_fails(match: str, fn, *args) -> None:
    """``fn(*args)`` must call ``pytest.fail``. Not ``pytest.raises``: a ``Skipped`` escaping it
    would turn this very test into a skip, so a fail-to-skip regression would pass unseen."""
    try:
        fn(*args)
    except BaseException as exc:  # noqa: BLE001 -- Skipped and Failed are BaseExceptions
        outcome = exc
    else:
        outcome = None
    assert isinstance(outcome, pytest.fail.Exception), f"expected pytest.fail, got {outcome!r}"
    assert match in str(outcome), str(outcome)


def _fake_install(root: Path, *, with_demos: bool = True) -> Path:
    cli = root / "bin" / "kicad-cli.exe"
    cli.parent.mkdir(parents=True)
    cli.write_bytes(b"")
    if with_demos:
        (root / "share" / "kicad" / "demos").mkdir(parents=True)
    return cli


# --- discovery -------------------------------------------------------------------------


def test_demos_sit_next_to_kicad_cli_on_windows_and_linux() -> None:
    windows = demos.demos_dir_next_to(PureWindowsPath(r"C:\Program Files\KiCad\10.0\bin\kicad-cli.exe"))
    linux = demos.demos_dir_next_to(PurePosixPath("/usr/bin/kicad-cli"))

    assert windows == PureWindowsPath(r"C:\Program Files\KiCad\10.0\share\kicad\demos")
    assert linux == PurePosixPath("/usr/share/kicad/demos")


def test_kicad_demos_dir_wins_over_kicad_cli(tmp_path: Path) -> None:
    path, how = demos.locate_demos_dir({demos.DIR_VAR: str(tmp_path)}, _no_cli)

    assert path == tmp_path
    assert demos.DIR_VAR in how


def test_a_kicad_demos_dir_that_does_not_exist_is_reported_not_bypassed(tmp_path: Path) -> None:
    path, reason = demos.locate_demos_dir({demos.DIR_VAR: str(tmp_path / "nope")}, _no_cli)

    assert path is None
    assert demos.DIR_VAR in reason and "nope" in reason


def test_without_the_variable_the_demos_are_found_next_to_kicad_cli(tmp_path: Path) -> None:
    cli = _fake_install(tmp_path)

    path, how = demos.locate_demos_dir({}, lambda: cli)

    assert path == tmp_path / "share" / "kicad" / "demos"
    assert str(cli) in how


def test_an_install_without_demos_names_where_it_looked(tmp_path: Path) -> None:
    cli = _fake_install(tmp_path, with_demos=False)

    path, reason = demos.locate_demos_dir({}, lambda: cli)

    assert path is None
    assert str(tmp_path / "share" / "kicad" / "demos") in reason
    assert "kicad-demos package" in reason


def test_no_kicad_cli_and_no_variable_is_a_stated_reason() -> None:
    path, reason = demos.locate_demos_dir({}, _cli_missing)

    assert path is None
    assert "kicad-cli" in reason and demos.DIR_VAR in reason


# --- the KICAD_DEMOS_REQUIRED flag -----------------------------------------------------


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_the_flag_turns_a_missing_demo_into_a_failure(value: str) -> None:
    _assert_fails(
        "KICAD_DEMOS_REQUIRED", demos.missing, "demo 'x' not installed", {demos.REQUIRED_FLAG: value}
    )


@pytest.mark.parametrize("env", [{}, {"KICAD_DEMOS_REQUIRED": ""}, {"KICAD_DEMOS_REQUIRED": "0"}])
def test_without_the_flag_a_missing_demo_skips_with_the_reason(env: dict) -> None:
    with pytest.raises(pytest.skip.Exception, match="demo 'x' not installed"):
        demos.missing("demo 'x' not installed", env)


def test_a_demo_absent_from_the_demos_dir_fails_under_the_flag(tmp_path: Path) -> None:
    _assert_fails(
        "no_such_demo",
        demos.copy_demo, tmp_path, "no_such_demo", tmp_path / "out", {demos.REQUIRED_FLAG: "1"},
    )


def test_the_fixture_body_fails_closed_under_the_flag(tmp_path: Path) -> None:
    env = {demos.DIR_VAR: str(tmp_path / "nowhere"), demos.REQUIRED_FLAG: "1"}

    _assert_fails("nowhere", demos.demos_dir_or_missing, env, _no_cli)


def test_the_fixture_body_skips_without_the_flag(tmp_path: Path) -> None:
    with pytest.raises(pytest.skip.Exception, match="nowhere"):
        demos.demos_dir_or_missing({demos.DIR_VAR: str(tmp_path / "nowhere")}, _no_cli)


def test_the_fixture_body_returns_the_found_directory(tmp_path: Path) -> None:
    assert demos.demos_dir_or_missing({demos.DIR_VAR: str(tmp_path)}, _no_cli) == tmp_path


def _link_dir(link: Path, target: Path) -> None:
    """A directory link: a junction on Windows (no privilege needed), a symlink elsewhere."""
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    else:
        link.symlink_to(target, target_is_directory=True)


def test_a_linked_kicad_cli_finds_the_demos_of_the_real_install(tmp_path: Path) -> None:
    real_cli = _fake_install(tmp_path / "real")
    (tmp_path / "link").mkdir()
    _link_dir(tmp_path / "link" / "bin", real_cli.parent)  # link/ has no share/
    linked_cli = tmp_path / "link" / "bin" / real_cli.name

    path, _ = demos.locate_demos_dir({}, lambda: linked_cli)

    assert path is not None
    assert path.resolve() == (tmp_path / "real" / "share" / "kicad" / "demos").resolve()


# --- KiCad's symbol libraries, found the same way (test_simulation_toolset) ------------


def test_kicad_symbol_dir_wins_over_kicad_cli(tmp_path: Path) -> None:
    env = {demos.SYMBOL_DIR_VAR: str(tmp_path)}

    assert demos.symbols_dir_or_missing(env, _no_cli) == tmp_path


def test_the_symbols_sit_next_to_kicad_cli(tmp_path: Path) -> None:
    cli = _fake_install(tmp_path)
    (tmp_path / "share" / "kicad" / "symbols").mkdir()

    assert demos.symbols_dir_or_missing({}, lambda: cli) == tmp_path / "share" / "kicad" / "symbols"


def test_missing_symbols_fail_under_the_flag_and_skip_without_it(tmp_path: Path) -> None:
    cli = _fake_install(tmp_path)  # no share/kicad/symbols

    _assert_fails("symbols", demos.symbols_dir_or_missing, {demos.REQUIRED_FLAG: "1"}, lambda: cli)
    with pytest.raises(pytest.skip.Exception, match="symbols"):
        demos.symbols_dir_or_missing({}, lambda: cli)


def test_the_symbols_miss_names_the_symbols_package_not_the_demos_package(tmp_path: Path) -> None:
    cli = _fake_install(tmp_path)  # no share/kicad/symbols

    with pytest.raises(pytest.skip.Exception) as skipped:
        demos.symbols_dir_or_missing({}, lambda: cli)

    message = str(skipped.value)
    assert "kicad-symbols package" in message
    assert "kicad-demos" not in message and "the demos come" not in message


# --- copy before any write -------------------------------------------------------------


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_a_demo_is_handed_out_as_a_copy_and_the_install_is_left_alone(
    kicad_demos_dir: Path, kicad_demo, tmp_path: Path
) -> None:
    source = kicad_demos_dir / "pic_programmer"
    before = _tree_digest(source)

    copy = kicad_demo("pic_programmer")
    assert copy.is_relative_to(tmp_path)
    assert not copy.resolve().is_relative_to(kicad_demos_dir.resolve())
    assert _tree_digest(copy) == before  # byte for byte what KiCad wrote

    (copy / "pic_programmer.kicad_pro").unlink()  # a write on the copy (no KiCad file is edited)
    (copy / "scratch.txt").write_text("x", encoding="utf-8")

    assert _tree_digest(source) == before
    assert (copy / "pic_programmer.kicad_pcb").stat().st_mode & 0o200  # the copy is writable


def test_a_demo_can_be_handed_out_twice_as_separate_copies(kicad_demo) -> None:
    first = kicad_demo("pic_programmer")
    second = kicad_demo("pic_programmer")

    assert first != second and first.name == second.name == "pic_programmer"
    (first / "pic_programmer.kicad_pro").unlink()
    assert (second / "pic_programmer.kicad_pro").is_file()


def test_the_install_guard_names_every_changed_added_and_removed_file(tmp_path: Path) -> None:
    source = tmp_path / "demo"
    (source / "sub").mkdir(parents=True)
    for name in ("a.txt", "b.txt", "sub/c.txt"):
        (source / name).write_text(name, encoding="utf-8")
    guard = demos.InstallGuard()
    guard.record(source)
    guard.record(source)  # a second hand-out keeps the first digest
    assert guard.changes() == []

    (source / "a.txt").write_text("changed", encoding="utf-8")
    (source / "sub" / "c.txt").unlink()
    (source / "new.txt").write_text("x", encoding="utf-8")

    changes = "\n".join(guard.changes())
    assert "a.txt" in changes and "sub/c.txt" in changes and "new.txt" in changes
    assert "b.txt" not in changes


def test_a_tree_inside_a_recorded_one_is_covered_not_hashed_again(tmp_path: Path) -> None:
    (tmp_path / "demo").mkdir()
    (tmp_path / "demo" / "a.txt").write_text("a", encoding="utf-8")
    guard = demos.InstallGuard()
    guard.record(tmp_path)
    guard.record(tmp_path / "demo")

    assert guard.recorded() == [tmp_path.resolve()]
    (tmp_path / "demo" / "a.txt").write_text("changed", encoding="utf-8")
    assert guard.changes() == [f"{tmp_path.resolve()}: demo/a.txt changed"]


def test_disjoint_trees_are_each_recorded_even_when_one_name_prefixes_the_other(
    tmp_path: Path,
) -> None:
    for name in ("demos/a.txt", "demos2/b.txt", "other/c.txt"):  # demos2 is not inside demos
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(name, encoding="utf-8")
    guard = demos.InstallGuard()
    for name in ("demos", "demos2", "other"):
        guard.record(tmp_path / name)

    assert guard.recorded() == [(tmp_path / name).resolve() for name in ("demos", "demos2", "other")]
    (tmp_path / "demos2" / "b.txt").write_text("changed", encoding="utf-8")
    (tmp_path / "other" / "new.txt").write_text("x", encoding="utf-8")
    assert guard.changes() == [
        f"{(tmp_path / 'demos2').resolve()}: b.txt changed",
        f"{(tmp_path / 'other').resolve()}: new.txt added",
    ]


def test_a_path_that_only_spells_itself_inside_a_recorded_tree_is_still_recorded(
    tmp_path: Path,
) -> None:
    """``demos/../other`` is not inside ``demos``: paths are resolved, not compared as text."""
    for name in ("demos/a.txt", "other/b.txt"):
        (tmp_path / name).parent.mkdir(exist_ok=True)
        (tmp_path / name).write_text(name, encoding="utf-8")
    guard = demos.InstallGuard()
    guard.record(tmp_path / "demos")
    guard.record(tmp_path / "demos" / ".." / "other")
    guard.record(tmp_path / "other" / ".." / "demos" / ".")  # the same tree again: covered

    assert guard.recorded() == [(tmp_path / "demos").resolve(), (tmp_path / "other").resolve()]
    (tmp_path / "other" / "b.txt").unlink()
    assert guard.changes() == [f"{(tmp_path / 'other').resolve()}: b.txt removed"]


def test_the_session_guard_covers_the_whole_demos_tree_not_only_handed_out_demos(
    kicad_demos_dir: Path, kicad_demo, demo_install_guard
) -> None:
    kicad_demo("pic_programmer")

    assert demo_install_guard.recorded() == [kicad_demos_dir.resolve()]


def test_the_conftest_fixtures_fail_closed_and_guard_the_whole_install(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real ``tests/conftest.py`` fixtures in a nested run (``-p tests.conftest``)."""
    monkeypatch.setenv(demos.REQUIRED_FLAG, "1")

    # A missing KICAD_DEMOS_DIR under the flag: one error, never a skip or a pass.
    monkeypatch.setenv(demos.DIR_VAR, str(pytester.path / "nowhere"))
    pytester.makepyfile(test_one="def test_one(kicad_demos_dir):\n    pass\n")
    result = pytester.runpytest("-p", "tests.conftest", "-p", "no:asyncio")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*ERROR at setup of test_one*", "*KICAD_DEMOS_REQUIRED is set*nowhere*"])

    # The install guard's teardown fires, also for a demo no test ever copied.
    install = pytester.mkdir("install")
    for name in ("copied/a.txt", "never_copied/b.txt"):
        (install / name).parent.mkdir(exist_ok=True)
        (install / name).write_text(name, encoding="utf-8")
    monkeypatch.setenv(demos.DIR_VAR, str(install))
    pytester.makepyfile(
        test_one=(
            "def test_one(kicad_demos_dir, kicad_demo):\n"
            "    kicad_demo('copied')\n"
            "    (kicad_demos_dir / 'never_copied' / 'probe.txt').write_text('x', encoding='utf-8')\n"
        )
    )
    result = pytester.runpytest("-p", "tests.conftest", "-p", "no:asyncio")
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(
        ["*ERROR at teardown of test_one*", "*the KiCad install's demos changed*", "*never_copied/probe.txt added*"]
    )


# --- ratchet: no test reaches into somebody's home directory ---------------------------

_OWN = "# home-path ratchet: own pattern"  # marks this file's pattern lines; their count is pinned
_HOME_PATH = re.compile(  # home-path ratchet: own pattern
    r"[A-Z]:[\\/]+Users[\\/]+|/home/[^/\s\"']+/|/Users/[^/\s\"']+/|/root/"  # home-path ratchet: own pattern
    r"|\.\s*home\s*\(|expanduser\(|USERPROFILE"  # .home( on any receiver  # home-path ratchet: own pattern
    r"|[\"']HOME(?:DRIVE|PATH)?[\"']|\$\{?HOME\b",  # the variables, $HOME  # home-path ratchet: own pattern
    re.I,
)
_OWN_PATTERN_LINES = 24  # lines of this file ending in _OWN; pinned so the marker cannot spread

# Files KiCad itself wrote, whose provenance fields record where they were saved. No test
# reads these fields as a location, and hard rule 1 forbids removing them by hand. The hit
# count is pinned per file: an exempted file cannot gain a hit, and one that loses its hits
# must leave the list.
_KICAD_WRITTEN_PROVENANCE: dict[str, tuple[int, str]] = {}  # public export: the three fixtures were regenerated or dropped


def _home_path_counts() -> dict[str, int]:
    tests = ROOT / "tests"
    own = Path(__file__).resolve()
    counts = {}
    for path in tests.rglob("*"):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if path.resolve() == own:
            text = "\n".join(line for line in text.splitlines() if not line.rstrip().endswith(_OWN))
        hits = len(_HOME_PATH.findall(text))
        if hits:
            counts[path.relative_to(tests).as_posix()] = hits
    return counts


def test_no_test_reaches_a_path_in_a_home_directory() -> None:
    pinned = {name: count for name, (count, _) in _KICAD_WRITTEN_PROVENANCE.items()}

    assert _home_path_counts() == pinned


def test_only_the_pattern_lines_of_this_file_are_exempt() -> None:
    lines = Path(__file__).read_text(encoding="utf-8").splitlines()

    assert sum(line.rstrip().endswith(_OWN) for line in lines) == _OWN_PATTERN_LINES


def test_the_home_path_pattern_catches_both_slash_styles_and_linux_homes() -> None:
    for text in (
        "C:/Us" "ers/someone/demos",  # home-path ratchet: own pattern
        r"C:\Us" r"ers\someone\demos",  # home-path ratchet: own pattern
        r'"C:\\Us' r'ers\\someone\\x"',  # JSON- and S-expression-escaped  # home-path ratchet: own pattern
        "c:/us" "ers/someone/demos",  # home-path ratchet: own pattern
        "/home/runner/work/x",  # home-path ratchet: own pattern
        "/Users/someone/x",  # home-path ratchet: own pattern
        "/root/kicad/demos",  # home-path ratchet: own pattern
        "Path.home() / 'demos'",  # home-path ratchet: own pattern
        "os.path.expanduser('~/demos')",  # home-path ratchet: own pattern
        "os.environ['USERPROFILE']",  # home-path ratchet: own pattern
    ):
        assert _HOME_PATH.search(text), text
    assert not _HOME_PATH.search("C:/Program Files/KiCad/10.0/share/kicad/demos")
    assert not _HOME_PATH.search("/usr/share/kicad/demos")


def test_the_home_path_pattern_catches_the_home_variable_and_home_on_any_receiver() -> None:
    for text in (
        "os.environ['HOME']",  # home-path ratchet: own pattern
        'os.getenv("HOME")',  # home-path ratchet: own pattern
        'monkeypatch.setenv("HOMEPATH", x)',  # home-path ratchet: own pattern
        "os.environ['HOMEDRIVE']",  # home-path ratchet: own pattern
        '"$HOME/kicad/demos"',  # home-path ratchet: own pattern
        "'${HOME}/kicad/demos'",  # home-path ratchet: own pattern
        "pathlib.Path.home() / 'demos'",  # home-path ratchet: own pattern
        "PurePath().home()",  # home-path ratchet: own pattern
        "P.home()",  # home-path ratchet: own pattern
        "Path . home ()",  # home-path ratchet: own pattern
    ):
        assert _HOME_PATH.search(text), text
    for text in ("homepage = 1", "def home(self):", "self.homepage()", "$HOMEBREW_PREFIX", "'HOMES'"):
        assert not _HOME_PATH.search(text), text
