"""Issue 172: the test session's replay cache of kicad-cli runs (``tests/_cli_replay.py``).

A cache that serves a stale answer is the failure to design against, so every guard of the key has a test
that goes red when the guard is removed (the mutant is named in the docstring and was run). The wrapper is
driven here with a FAKE ``kicad-cli`` (``_REAL_RUN`` replaced) so the tests are fast and exact; the last
test replays the real ``kicad-cli sch upgrade`` and compares with a fresh run.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from tests import _cli_replay as rp

CRASH = 3221225477  # 0xC0000005


class FakeCli:
    """``kicad-cli sch|pcb upgrade --force FILE`` appends a marker to FILE and names it on stdout; a file
    holding BROKEN exits 1; ``version`` prints ``self.version``."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.version = "10.0.7"
        self.crash = False
        self.extra_file: str | None = None
        self.drop: str | None = None
        self.noise = 0

    def __call__(self, argv, **kw):
        argv = [str(a) for a in argv]
        self.calls.append(argv)
        if argv[1:] == ["version"]:
            return subprocess.CompletedProcess(argv, 0, self.version + "\n", "")
        if "upgrade" not in argv:
            return subprocess.CompletedProcess(argv, 0, "ok\n", "")
        target = Path(argv[-1])
        data = target.read_bytes()
        if self.crash:
            return subprocess.CompletedProcess(argv, CRASH, "", "boom")
        self.noise += 1
        if b"(upgraded)" not in data:  # like kicad-cli's upgrade: running it again changes nothing
            target.write_bytes(data + b"(upgraded)")
        if self.extra_file:
            (target.parent / self.extra_file).write_bytes(b"side effect of " + str(target.parent).encode())
        if self.drop:
            (target.parent / self.drop).unlink()
        return subprocess.CompletedProcess(argv, 1 if b"BROKEN" in data else 0, f"upgraded {target} n{self.noise}\n", "")

    def upgrades(self) -> int:
        return sum(1 for c in self.calls if "upgrade" in c)


@pytest.fixture
def fake(monkeypatch, tmp_path_factory):
    cli = FakeCli()
    monkeypatch.setattr(rp, "_REAL_RUN", cli)
    monkeypatch.setenv(rp.ENV_ON, "1")
    monkeypatch.setenv(rp.ENV_DIR, str(tmp_path_factory.mktemp("store")))
    monkeypatch.delenv(rp.ENV_VERIFY, raising=False)
    monkeypatch.setattr(rp, "paused", False)
    monkeypatch.setattr(rp, "_version_memo", {})
    monkeypatch.setattr(rp, "_hash_memo", {})
    monkeypatch.setattr(rp, "_stats", {"hit": 0, "miss": 0, "stored": 0})
    return cli


def _project(root: Path, name: str, content: bytes = b"(kicad_sch)", *, sibling: bytes = b"pro") -> Path:
    root.mkdir(parents=True)
    (root / "x.kicad_pro").write_bytes(sibling)
    sheet = root / name
    sheet.write_bytes(content)
    return sheet


def _upgrade(sheet: Path, *flags: str, **kw):
    kw.setdefault("capture_output", True)
    kw.setdefault("text", True)
    return rp._run(["kicad-cli", "sch", "upgrade", "--force", *flags, str(sheet)], **kw)


def test_the_same_project_in_another_directory_is_replayed_exactly(fake, tmp_path) -> None:
    a = _project(tmp_path / "a", "s.kicad_sch")
    b = _project(tmp_path / "b", "s.kicad_sch")
    first = _upgrade(a)
    second = _upgrade(b)
    assert fake.upgrades() == 1, "the second call must not reach kicad-cli"
    assert second.returncode == first.returncode == 0
    assert second.stdout == f"upgraded {b} n1\n", "the path in the output is the new directory's"
    assert b.read_bytes() == a.read_bytes() == b"(kicad_sch)(upgraded)"
    assert rp._stats["hit"] == 1


def test_a_changed_input_byte_misses(fake, tmp_path) -> None:
    """Mutant R1: the key without the sha256 of the files (names only)."""
    _upgrade(_project(tmp_path / "a", "s.kicad_sch", b"(kicad_sch 1)"))
    _upgrade(_project(tmp_path / "b", "s.kicad_sch", b"(kicad_sch 2)"))
    assert fake.upgrades() == 2


def test_a_changed_sibling_file_misses(fake, tmp_path) -> None:
    """Mutant R2: the key hashes only the file named in the argv, not the directory around it."""
    _upgrade(_project(tmp_path / "a", "s.kicad_sch", sibling=b"one"))
    _upgrade(_project(tmp_path / "b", "s.kicad_sch", sibling=b"two"))
    assert fake.upgrades() == 2


def test_a_changed_argv_misses(fake, tmp_path) -> None:
    """Mutant R3: the key without the argv."""
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"))
    _upgrade(_project(tmp_path / "b", "s.kicad_sch"), "--verbose")
    assert fake.upgrades() == 2


def test_a_changed_file_name_misses(fake, tmp_path) -> None:
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"))
    _upgrade(_project(tmp_path / "b", "t.kicad_sch"))
    assert fake.upgrades() == 2


def test_a_changed_kicad_environment_misses(fake, tmp_path, monkeypatch) -> None:
    """Mutant R4: the key without the KICAD* variables."""
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"))
    monkeypatch.setenv("KICAD_SYMBOL_DIR", "elsewhere")
    _upgrade(_project(tmp_path / "b", "s.kicad_sch"))
    assert fake.upgrades() == 2


def test_a_changed_kicad_cli_version_misses(fake, tmp_path) -> None:
    """Mutant R5: the key without the ``kicad-cli version`` output."""
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"))
    fake.version = "10.0.8"
    rp._version_memo.clear()
    _upgrade(_project(tmp_path / "b", "s.kicad_sch"))
    assert fake.upgrades() == 2


@pytest.mark.parametrize("words", [["pcb", "drc"], ["sch", "erc"], ["sch", "export", "netlist"], ["pcb", "export", "gerbers"]])
def test_drc_erc_and_exports_are_never_replayed(fake, tmp_path, words) -> None:
    """Mutant R6: ``pcb drc`` in CACHEABLE (the gate's three DRC runs exist because runs can differ)."""
    sheet = _project(tmp_path / "a", "s.kicad_sch")
    for _ in range(2):
        rp._run(["kicad-cli", *words, "-o", str(tmp_path / "out.txt"), str(sheet)], capture_output=True, text=True)
    assert len([c for c in fake.calls if c[1:3] == words[:2]]) == 2


def test_a_short_timeout_is_never_replayed(fake, tmp_path) -> None:
    """Mutant R7: no timeout guard (a test of a timeout must see it)."""
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"), timeout=5)
    _upgrade(_project(tmp_path / "b", "s.kicad_sch"), timeout=5)
    assert fake.upgrades() == 2
    _upgrade(_project(tmp_path / "c", "s.kicad_sch"), timeout=300)
    _upgrade(_project(tmp_path / "d", "s.kicad_sch"), timeout=300)
    assert fake.upgrades() == 3


def test_a_call_without_captured_output_is_not_replayed(fake, tmp_path) -> None:
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"), capture_output=False)
    _upgrade(_project(tmp_path / "b", "s.kicad_sch"), capture_output=False)
    assert fake.upgrades() == 2


def test_a_paused_cache_runs_for_real(fake, tmp_path, monkeypatch) -> None:
    """Mutant R8: the pause for the excluded test files is ignored."""
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"))
    monkeypatch.setattr(rp, "paused", True)
    _upgrade(_project(tmp_path / "b", "s.kicad_sch"))
    assert fake.upgrades() == 2


def test_the_excluded_files_are_the_gate_and_the_rung_tests() -> None:
    for name in ("test_verify_board_stale_fill", "test_gate_field", "test_batch_gate_workers", "test_rung_runner",
                 "test_autonomy_run_review", "test_ladder_data_home"):
        assert name.startswith(rp.EXCLUDED_FILES), name
    assert not "test_write_layer".startswith(rp.EXCLUDED_FILES)


def test_a_failing_exit_code_is_replayed_as_it_was(fake, tmp_path) -> None:
    a = _project(tmp_path / "a", "s.kicad_sch", b"BROKEN")
    b = _project(tmp_path / "b", "s.kicad_sch", b"BROKEN")
    assert _upgrade(a).returncode == 1
    assert _upgrade(b).returncode == 1
    assert fake.upgrades() == 1


def test_a_crash_is_never_stored(fake, tmp_path) -> None:
    """Mutant R9: any exit code is stored (a crash code is not an answer)."""
    fake.crash = True
    assert _upgrade(_project(tmp_path / "a", "s.kicad_sch")).returncode == CRASH
    assert _upgrade(_project(tmp_path / "b", "s.kicad_sch")).returncode == CRASH
    assert fake.upgrades() == 2


def test_files_the_run_created_and_removed_are_restored_exactly(fake, tmp_path) -> None:
    """Mutant R10: only the argv file is restored, side effects beside it are not."""
    fake.extra_file = "side.txt"
    a = _project(tmp_path / "a", "s.kicad_sch")
    b = _project(tmp_path / "b", "s.kicad_sch")
    _upgrade(a)
    _upgrade(b)
    assert fake.upgrades() == 1
    assert (b.parent / "side.txt").read_bytes() == b"side effect of " + str(b.parent).encode(), "the new directory"
    fake.extra_file = None
    fake.drop = "gone.txt"
    c = _project(tmp_path / "c", "s.kicad_sch", sibling=b"p2")
    d = _project(tmp_path / "d", "s.kicad_sch", sibling=b"p2")
    for p in (c.parent, d.parent):
        (p / "gone.txt").write_bytes(b"x")
    _upgrade(c)
    _upgrade(d)
    assert not (c.parent / "gone.txt").exists() and not (d.parent / "gone.txt").exists()


def test_binary_output_holding_a_path_is_not_stored(fake, tmp_path) -> None:
    """Mutant R11: a binary file that holds the directory's path is tokenised or stored anyway."""
    a = _project(tmp_path / "a", "s.kicad_sch", b"\xff\xfe" + str(tmp_path / "a").encode())
    _upgrade(a)
    b = _project(tmp_path / "b", "s.kicad_sch", b"\xff\xfe" + str(tmp_path / "a").encode())
    _upgrade(b)
    assert fake.upgrades() == 2


def test_a_directory_over_the_size_limit_runs_for_real(fake, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(rp, "MAX_FILES", 1)
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"))
    _upgrade(_project(tmp_path / "b", "s.kicad_sch"))
    assert fake.upgrades() == 2


def test_verify_mode_fails_on_a_replay_that_differs_from_a_real_run(fake, tmp_path, monkeypatch) -> None:
    """The fidelity check itself: a kicad-cli whose answer changes between runs is caught."""
    monkeypatch.setenv(rp.ENV_VERIFY, "1")
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"))
    with pytest.raises(rp.ReplayMismatch):
        _upgrade(_project(tmp_path / "b", "s.kicad_sch"))  # the fake's stdout differs on every real run


def test_verify_mode_passes_when_the_replay_equals_a_real_run(fake, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(rp.ENV_VERIFY, "1")
    monkeypatch.setattr(fake, "noise", 0)

    def steady(argv, **kw):
        fake.noise = 0  # the same answer every time
        return FakeCli.__call__(fake, argv, **kw)

    monkeypatch.setattr(rp, "_REAL_RUN", steady)
    _upgrade(_project(tmp_path / "a", "s.kicad_sch"))
    assert _upgrade(_project(tmp_path / "b", "s.kicad_sch")).returncode == 0
    assert rp._stats["hit"] == 1


def test_the_real_kicad_cli_replays_a_sheet_upgrade_exactly(kicad_demo, tmp_path, monkeypatch) -> None:
    """The real thing: ``sch upgrade`` of two copies of a demo sheet; the replay (verify mode: the real run
    is made too and compared) leaves the same bytes, the same exit code and the same output."""
    from kicad_mcp import kicad_cli

    monkeypatch.setenv(rp.ENV_ON, "1")
    monkeypatch.setenv(rp.ENV_DIR, str(tmp_path / "store"))
    monkeypatch.setattr(rp, "paused", False)
    monkeypatch.setattr(rp, "_stats", {"hit": 0, "miss": 0, "stored": 0})
    cli = str(kicad_cli.find_kicad_cli())
    one = kicad_demo("complex_hierarchy") / "complex_hierarchy.kicad_sch"
    import shutil

    two = tmp_path / "second" / one.name
    fresh = tmp_path / "fresh" / one.name
    shutil.copytree(one.parent, two.parent)
    shutil.copytree(one.parent, fresh.parent)

    def run(path: Path):
        return rp._run([cli, "sch", "upgrade", "--force", str(path)], capture_output=True, text=True, timeout=300)

    a = run(one)
    expected = (rp._REAL_RUN([cli, "sch", "upgrade", "--force", str(fresh)], capture_output=True, text=True), fresh)
    monkeypatch.setenv(rp.ENV_VERIFY, "1")
    b = run(two)
    assert rp._stats["hit"] == 1, "the second copy must be a replay"
    assert (b.returncode, b.stdout, b.stderr) == (a.returncode, a.stdout.replace(str(one), str(two)), a.stderr.replace(str(one), str(two)))
    assert two.read_bytes() == one.read_bytes() == expected[1].read_bytes()


def test_with_the_replay_unset_subprocess_run_is_left_unpatched(monkeypatch) -> None:
    """Issue 176. Mutant M10: ``pytest_configure`` installs the wrapper without looking at
    ``KICAD_MCP_CLI_REPLAY`` (the default of every run, and of the batch gate, would then be the replay)."""
    sentinel = object()
    monkeypatch.setattr(subprocess, "run", sentinel)
    monkeypatch.delenv(rp.ENV_ON, raising=False)
    monkeypatch.delenv(rp.ENV_DIR, raising=False)

    class Config:
        pass

    rp.pytest_configure(Config())
    assert subprocess.run is sentinel
    # and the opt-in does install it (the guard is the variable, not a dead code path)
    monkeypatch.setenv(rp.ENV_ON, "1")
    monkeypatch.setenv(rp.ENV_DIR, str(Path(__file__).parent))
    rp.pytest_configure(Config())
    assert subprocess.run is rp._run
