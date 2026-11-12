"""Build ticket 3: `scripts/refresh_jlcpcb_index.py --update` moves the pin to upstream's newest commit.

Everything runs on temporary git repositories: a stand-in upstream, and a stand-in superproject with
the stand-in as its submodule. Nothing here reads or moves the real submodule. The symbol libraries are
built through `kicad_tools`' object model (tests/test_jlcpcb_basic.py helpers).
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
from pathlib import Path

import pytest
from test_jlcpcb_basic import _symbol, _write_symbol_lib
from tests._hermetic_git import apply_hermetic_env, run_git

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("refresh_jlcpcb_index_u", ROOT / "scripts" / "refresh_jlcpcb_index.py")
refresh = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(refresh)

SUB = refresh.jlc.SUBMODULE_RELATIVE_PATH


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch):
    apply_hermetic_env(monkeypatch, identity=True)


def _git(cwd, *args, **kw):
    return run_git(*args, cwd=cwd, text=True, identity=True, **kw).stdout.strip()


def _resistors(n: int, *, c2_class: str = "Preferred Component"):
    out = []
    for i in range(1, n + 1):
        cls = c2_class if i == 2 else "Basic Component"
        out.append(_symbol(f"R{i}", LCSC=f"C{i}", Part=f"RES{i}", Manufacturer="m", Class=cls,
                           Footprint="PCM_JLCPCB:R0603", Stock="5"))
    return out


def _diodes(n: int):
    return [_symbol(f"D{i}", LCSC=f"C{100 + i}", Part=f"DIO{i}", Manufacturer="m", Class="Basic Component",
                    Footprint="PCM_JLCPCB:D0603") for i in range(1, n + 1)]


def _upstream_commit(up: Path, *, resistors, diodes, message: str) -> str:
    (up / "symbols").mkdir(parents=True, exist_ok=True)
    _write_symbol_lib(up / "symbols" / "JLCPCB-Resistors.kicad_sym", *resistors)
    _write_symbol_lib(up / "symbols" / "JLCPCB-Diodes.kicad_sym", *diodes)
    _git(up, "add", "-A")
    _git(up, "commit", "-q", "-m", message)
    return _git(up, "rev-parse", "HEAD")


class World:
    """A stand-in upstream and a superproject that pins its first commit and carries the matching index."""

    def __init__(self, tmp: Path):
        self.up = tmp / "upstream"
        self.up.mkdir()
        _git(self.up, "init", "-q")
        self.v1 = _upstream_commit(self.up, resistors=_resistors(10), diodes=_diodes(1), message="v1")
        self.sup = tmp / "super"
        self.sup.mkdir()
        _git(self.sup, "init", "-q")
        _git(self.sup, "-c", "protocol.file.allow=always", "submodule", "add", "-q", self.up.as_posix(), SUB)
        self.index = self.sup / "idx.json"
        assert refresh.main(["--library", str(self.sup / SUB), "--commit", self.v1, "--index", str(self.index)]) == 0
        _git(self.sup, "add", "-A")
        _git(self.sup, "commit", "-q", "-m", "pin v1")

    def update(self, capsys, *extra: str):
        args = ["--update", "--repo-root", str(self.sup), "--upstream", self.up.as_posix(),
                "--index", str(self.index), *extra]
        code = refresh.main(args)
        out = capsys.readouterr()
        return code, out.out, out.err

    def snapshot(self) -> dict:
        return {
            "index": self.index.read_bytes(),
            "gitlink": _git(self.sup, "ls-files", "-s", "--", SUB),
            "staged": _git(self.sup, "diff", "--cached", "--name-only"),
            "status": _git(self.sup, "status", "--porcelain"),
            "sub_head": _git(self.sup / SUB, "rev-parse", "HEAD"),
            "head": _git(self.sup, "rev-parse", "HEAD"),
        }


@pytest.fixture
def world(tmp_path):
    return World(tmp_path)


def test_update_moves_the_pin_rebuilds_the_index_prints_tier_changes_and_stages_both(world, capsys):
    v2 = _upstream_commit(world.up, resistors=_resistors(10, c2_class="Extended Component") + _resistors(11)[10:],
                          diodes=_diodes(1), message="v2")
    before = world.snapshot()

    code, out, err = world.update(capsys)

    assert code == 0, err
    assert "preferred -> extended: 1" in out and "C2" in out
    assert "added: 1" in out and "C11" in out
    after = world.snapshot()
    assert after["head"] == before["head"]  # staged, never committed
    assert after["sub_head"] == v2
    assert f"{v2} 0\t{SUB}" in after["gitlink"]
    assert sorted(after["staged"].split()) == sorted([SUB, "idx.json"])
    payload = json.loads(world.index.read_text(encoding="utf-8"))
    assert payload["source"]["commit"] == v2
    assert payload["source"]["commit_date"]  # taken from the fetched commit
    assert {p["lcsc"]: p["tier"] for p in payload["parts"]}["C2"] == "extended"
    assert not list(world.sup.glob("idx.json.*"))


def _assert_nothing_moved(world, before):
    assert world.snapshot() == before


def test_a_library_emptied_upstream_is_refused_and_nothing_moves(world, capsys):
    """The real case: upstream commit de4e6c4 emptied 17 of 20 libraries. 10 of 11 parts remain (91 percent),
    so only the emptied-library rule can catch it."""
    _upstream_commit(world.up, resistors=_resistors(10), diodes=[], message="diodes emptied")
    before = world.snapshot()

    code, out, err = world.update(capsys)

    assert code == 2
    assert "library_emptied" in err and "JLCPCB-Diodes" in err
    assert "removed: 1" in out  # the diff is shown before the refusal
    _assert_nothing_moved(world, before)


def test_a_library_file_missing_upstream_counts_as_emptied(world, capsys):
    (world.up / "symbols" / "JLCPCB-Diodes.kicad_sym").unlink()
    _write_symbol_lib(world.up / "symbols" / "JLCPCB-Resistors.kicad_sym", *_resistors(10))
    _git(world.up, "add", "-A")
    _git(world.up, "commit", "-q", "-m", "diodes file gone")
    before = world.snapshot()

    code, _out, err = world.update(capsys)

    assert code == 2 and "library_emptied" in err and "JLCPCB-Diodes" in err
    _assert_nothing_moved(world, before)


def test_a_part_count_that_shrinks_by_more_than_the_share_is_refused(world, capsys):
    """11 -> 8 parts (73 percent): no library is emptied, so only the share rule can catch it."""
    _upstream_commit(world.up, resistors=_resistors(7), diodes=_diodes(1), message="resistors cut")
    before = world.snapshot()

    code, out, err = world.update(capsys)

    assert code == 2
    assert "index_would_shrink" in err and "80%" in err
    _assert_nothing_moved(world, before)


def test_a_shrink_inside_the_share_is_accepted(world, capsys):
    """11 -> 9 parts (82 percent) passes: the limit is a share, not any loss."""
    v2 = _upstream_commit(world.up, resistors=_resistors(8), diodes=_diodes(1), message="a few removed")
    code, out, err = world.update(capsys)
    assert code == 0, err
    assert "removed: 2" in out
    assert _git(world.sup / SUB, "rev-parse", "HEAD") == v2


def test_a_symbol_file_that_fails_to_parse_is_refused(world, capsys, monkeypatch):
    # A new library that fails to parse: no old part lives in it, so only the parse rule can catch it.
    _write_symbol_lib(world.up / "symbols" / "JLCPCB-New.kicad_sym", *_diodes(1))
    _upstream_commit(world.up, resistors=_resistors(10), diodes=_diodes(2), message="v2")
    import kicad_tools.sexp.parser as parser

    real = parser.parse_file

    def failing(path, *a, **k):
        if "JLCPCB-New" in str(path):
            raise ValueError("unbalanced parenthesis")
        return real(path, *a, **k)

    monkeypatch.setattr(parser, "parse_file", failing)
    before = world.snapshot()

    code, _out, err = world.update(capsys)

    assert code == 2 and "parse failed" in err and "JLCPCB-New" in err
    _assert_nothing_moved(world, before)


def test_dry_run_prints_the_plan_and_changes_nothing(world, capsys):
    _upstream_commit(world.up, resistors=_resistors(10, c2_class="Extended Component"), diodes=_diodes(1),
                     message="v2")
    before = world.snapshot()

    code, out, err = world.update(capsys, "--dry-run")

    assert code == 0, err
    assert "preferred -> extended: 1" in out and "dry run" in out and "would fetch" in out
    _assert_nothing_moved(world, before)


def test_dry_run_still_refuses_a_broken_upstream(world, capsys):
    _upstream_commit(world.up, resistors=_resistors(10), diodes=[], message="diodes emptied")
    code, _out, err = world.update(capsys, "--dry-run")
    assert code == 2 and "library_emptied" in err


def test_already_at_the_newest_commit_does_nothing(world, capsys, monkeypatch):
    def not_reached(*_a, **_k):
        raise AssertionError("the same-commit guard was skipped: the run went on to build and apply")

    monkeypatch.setattr(refresh, "_extract_symbols", not_reached)  # fail fast if the guard is gone
    monkeypatch.setattr(refresh, "_apply", not_reached)
    before = world.snapshot()
    code, out, _err = world.update(capsys)
    assert code == 0 and "nothing to do" in out
    _assert_nothing_moved(world, before)


def test_an_empty_submodule_directory_is_named_with_its_fetch_command_and_nothing_changes(world, tmp_path, capsys):
    """A fresh worktree has the submodule directory empty: the dry run still judges upstream, the apply says what it needs."""
    v2 = _upstream_commit(world.up, resistors=_resistors(10, c2_class="Extended Component"), diodes=_diodes(1),
                          message="v2")
    fresh = tmp_path / "fresh"
    _git(tmp_path, "clone", "-q", world.sup.as_posix(), fresh.as_posix())
    assert not (fresh / SUB / ".git").exists() and not any((fresh / SUB).iterdir())
    clone = World.__new__(World)
    clone.up, clone.sup, clone.index = world.up, fresh, fresh / "idx.json"
    bytes_before = clone.index.read_bytes()

    code, out, err = clone.update(capsys, "--dry-run")
    assert code == 0, err
    assert "git submodule update --init --depth 1" in out and "preferred -> extended: 1" in out

    code, out, err = clone.update(capsys)
    assert code == 2
    assert "git submodule update --init --depth 1 third_party/JLCPCB-Kicad-Library" in err
    assert clone.index.read_bytes() == bytes_before
    assert _git(fresh, "diff", "--cached", "--name-only") == ""
    assert v2 not in _git(fresh, "ls-files", "-s", "--", SUB)


def test_allow_shrink_is_not_accepted_together_with_update(world, capsys):
    _upstream_commit(world.up, resistors=_resistors(7), diodes=_diodes(1), message="resistors cut")
    before = world.snapshot()
    code, _out, err = world.update(capsys, "--allow-shrink")
    assert code == 2 and "--allow-shrink" in err
    _assert_nothing_moved(world, before)


def test_uncommitted_changes_to_the_index_stop_the_apply_before_the_pin_moves(world, capsys):
    _upstream_commit(world.up, resistors=_resistors(10, c2_class="Extended Component"), diodes=_diodes(1),
                     message="v2")
    world.index.write_bytes(world.index.read_bytes() + b"\n")  # someone's unfinished work
    before = world.snapshot()

    code, _out, err = world.update(capsys)

    assert code == 2 and "uncommitted" in err
    _assert_nothing_moved(world, before)


def test_the_index_write_is_verified_before_the_pin_moves(world, capsys, monkeypatch):
    """Self-check: a corrupted write ends in write_not_verified, and the submodule stays on the old commit."""
    _upstream_commit(world.up, resistors=_resistors(10, c2_class="Extended Component"), diodes=_diodes(1),
                     message="v2")
    before = world.snapshot()
    real = refresh.jlc.write_index

    def corrupting(payload, path=None):
        result = real(payload, path)
        Path(path).write_text('{"parts": []}', encoding="utf-8")
        return result

    monkeypatch.setattr(refresh.jlc, "write_index", corrupting)

    code, _out, err = world.update(capsys)

    assert code == 2 and "write_not_verified" in err
    _assert_nothing_moved(world, before)
    assert not list(world.sup.glob("idx.json.*"))


def test_an_unreachable_upstream_changes_nothing(world, tmp_path, capsys):
    before = world.snapshot()
    missing = (tmp_path / "nowhere").as_uri()  # a file:// URL passes the check and then fails to fetch
    code = refresh.main(["--update", "--repo-root", str(world.sup), "--upstream", missing, "--index", str(world.index)])
    err = capsys.readouterr().err
    assert code == 2 and "cannot fetch" in err
    _assert_nothing_moved(world, before)


# --- the guards and the printout on synthetic old/new payloads (no git) -----------------------------


def _payload(*parts):
    return {"source": {"commit": "x" * 40}, "counts": {"total": len(parts)},
            "parts": [{"lcsc": c, "tier": t, "symbol_lib": lib, "symbol": c, "mpn": f"M{c}"} for c, t, lib in parts]}


def test_the_tier_change_printout_names_every_move_on_a_synthetic_pair():
    old = _payload(("C1", "basic", "A"), ("C2", "preferred", "A"), ("C3", "extended", "B"), ("C4", "basic", "B"))
    new = _payload(("C1", "extended", "A"), ("C2", "basic", "A"), ("C3", "extended", "B"), ("C4", "basic", "B"))
    text = refresh.format_diff(refresh.diff_payloads(old, new), 10)
    assert "tier changes: 2" in text
    assert "basic -> extended: 1" in text and "preferred -> basic: 1" in text
    assert "C1 MC1: basic -> extended" in text and "C2 MC2: preferred -> basic" in text
    assert "C3" not in text and "C4" not in text  # unchanged parts are not listed


def test_emptied_libraries_compares_with_the_old_index_per_library():
    old = _payload(("C1", "basic", "A"), ("C2", "basic", "B"), ("C3", "basic", "B"))
    assert refresh.emptied_libraries(old, _payload(("C1", "basic", "A"))) == ["B"]
    assert refresh.emptied_libraries(old, _payload(("C2", "basic", "B"), ("C1", "basic", "A"))) == []
    assert refresh.emptied_libraries(None, _payload(("C1", "basic", "A"))) == []  # no old index: only the share rule
    built = _payload(("C1", "basic", "A"))
    built["counts"]["total"] = 1
    assert "library_emptied" in refresh.update_refusal(old, built)


def test_update_refusal_applies_the_share_boundary_exactly():
    old = _payload(*[(f"C{i}", "basic", "A") for i in range(10)])
    eight = _payload(*[(f"C{i}", "basic", "A") for i in range(8)])
    seven = _payload(*[(f"C{i}", "basic", "A") for i in range(7)])
    assert refresh.update_refusal(old, eight) is None  # exactly 80 percent passes
    assert "index_would_shrink" in refresh.update_refusal(old, seven)


# --- review round 1: all-or-nothing apply (B1), no argument injection (B2), upstream moved (B3) ---


def _v2(world):
    return _upstream_commit(world.up, resistors=_resistors(10, c2_class="Extended Component"), diodes=_diodes(1),
                            message="v2")


def _failing_git(monkeypatch, matches):
    real = refresh._git

    def fake(cwd, *args, **kw):
        if matches(args):
            return subprocess.CompletedProcess(list(args), 1, stdout="", stderr="injected failure")
        return real(cwd, *args, **kw)

    monkeypatch.setattr(refresh, "_git", fake)


def test_a_denied_index_replace_leaves_everything_as_it_was(world, capsys, monkeypatch):
    """B1a: os.replace raising (a Windows sharing denial) must not leave the submodule on the new commit."""
    _v2(world)
    before = world.snapshot()
    real = refresh.os.replace

    def deny(src, dst, *a, **k):
        if Path(dst) == world.index:
            raise PermissionError("sharing violation")
        return real(src, dst, *a, **k)

    monkeypatch.setattr(refresh.os, "replace", deny)

    code, _out, err = world.update(capsys)

    assert code == 2 and "PermissionError" in err and "nothing changed" in err
    _assert_nothing_moved(world, before)
    assert not list(world.sup.glob("idx.json.*"))


def test_a_failing_git_add_rolls_back_the_checkout_and_the_index(world, capsys, monkeypatch):
    """B1b: the submodule and the index had both moved when git add failed; both go back."""
    _v2(world)
    before = world.snapshot()
    _failing_git(monkeypatch, lambda a: a[0] == "add")

    code, _out, err = world.update(capsys)

    assert code == 2 and "git add failed" in err and "Restored the submodule" in err
    _assert_nothing_moved(world, before)


def test_a_failing_checkout_rolls_back_the_index(world, capsys, monkeypatch):
    v2 = _v2(world)
    before = world.snapshot()
    _failing_git(monkeypatch, lambda a: a[0] == "checkout" and v2 in a)

    code, _out, err = world.update(capsys)

    assert code == 2 and "checkout in the submodule failed" in err
    _assert_nothing_moved(world, before)


def test_a_partly_staged_failure_is_unstaged(world, capsys, monkeypatch):
    """git add stages the gitlink and then fails: the staging is undone."""
    _v2(world)
    before = world.snapshot()
    real = refresh._git

    def half(cwd, *args, **kw):
        if args[0] == "add":
            real(cwd, "add", "--", args[-2])  # the gitlink only
            return subprocess.CompletedProcess(list(args), 1, stdout="", stderr="injected")
        return real(cwd, *args, **kw)

    monkeypatch.setattr(refresh, "_git", half)
    code, _out, _err = world.update(capsys)
    assert code == 2
    _assert_nothing_moved(world, before)


def test_when_the_restore_itself_fails_the_manual_commands_are_printed(world, capsys, monkeypatch):
    _v2(world)
    old_bytes = world.index.read_bytes()
    _failing_git(monkeypatch, lambda a: a[0] == "add" or (a[0] == "checkout" and world.v1 in a))

    code, _out, err = world.update(capsys)

    assert code == 2 and "INCOMPLETE" in err
    assert f"checkout --detach {world.v1}" in err and SUB in err
    assert world.index.read_bytes() == old_bytes  # the part that could be restored was


def test_the_restore_check_notices_an_index_that_cannot_be_put_back(world, capsys, monkeypatch):
    _v2(world)
    _failing_git(monkeypatch, lambda a: a[0] == "add")
    real = Path.write_bytes

    def locked(self, data):
        if self == world.index:
            raise OSError("locked")
        return real(self, data)

    monkeypatch.setattr(Path, "write_bytes", locked)
    code, _out, err = world.update(capsys)
    assert code == 2 and "INCOMPLETE" in err and "git checkout HEAD -- idx.json" in err


def test_upstream_moving_between_the_scratch_fetch_and_the_submodule_fetch_is_refused(world, capsys, monkeypatch):
    """B3: the plan was judged on v2; a v3 pushed meanwhile must not be pinned unjudged."""
    _v2(world)
    before = world.snapshot()
    real = refresh._write_verified_temp

    def then_upstream_moves(*a, **k):
        result = real(*a, **k)
        _upstream_commit(world.up, resistors=_resistors(10), diodes=_diodes(3), message="v3")
        return result

    monkeypatch.setattr(refresh, "_write_verified_temp", then_upstream_moves)

    code, _out, err = world.update(capsys)

    assert code == 2 and "moved meanwhile" in err
    _assert_nothing_moved(world, before)


@pytest.mark.parametrize("ref", ["--upload-pack=touch MARKER", "-x", "a b", "", "HEAD;rm", "x\ny"])
def test_a_ref_that_could_be_an_option_is_refused_and_runs_nothing(world, tmp_path, capsys, ref):
    marker = tmp_path / "marker"
    ref = ref.replace("MARKER", marker.as_posix())
    before = world.snapshot()
    code = refresh.main([f"--ref={ref}", "--update", "--repo-root", str(world.sup), "--upstream", world.up.as_posix(),
                         "--index", str(world.index)])
    err = capsys.readouterr().err
    assert code == 2 and "--ref" in err
    assert not marker.exists()
    _assert_nothing_moved(world, before)


@pytest.mark.parametrize("upstream", ["-x", "--upload-pack=touch x", "ext::sh -c touch% x", "ext::true", "ssh://host/repo",
                                      "git://host/repo", "http://host/repo", "https://host/a b", ""])
def test_an_upstream_that_is_not_a_plain_https_url_or_local_path_is_refused(world, capsys, upstream):
    before = world.snapshot()
    code = refresh.main(["--update", "--repo-root", str(world.sup), f"--upstream={upstream}",
                         "--index", str(world.index)])
    err = capsys.readouterr().err
    assert code == 2 and "upstream" in err
    _assert_nothing_moved(world, before)


@pytest.mark.parametrize("url", ["-x", "ext::sh -c true", "ssh://host/repo", "/some/local/dir", "file:///tmp/x"])
def test_a_gitmodules_url_that_is_not_https_is_refused(world, capsys, url):
    """The url read from .gitmodules is held to https; a local path is allowed only when typed as --upstream."""
    _git(world.sup, "config", "-f", ".gitmodules", f"submodule.{SUB}.url", url)
    before = world.snapshot()
    code = refresh.main(["--update", "--repo-root", str(world.sup), "--index", str(world.index)])
    assert code == 2 and "upstream" in capsys.readouterr().err
    assert world.snapshot()["index"] == before["index"]


def test_every_network_git_call_carries_the_transport_allow_list(world, capsys, monkeypatch):
    _v2(world)
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        if "fetch" in cmd:
            seen.append(list(cmd))
        return real(cmd, *a, **k)

    monkeypatch.setattr(refresh.subprocess, "run", spy)
    assert world.update(capsys)[0] == 0
    assert len(seen) == 2  # the scratch fetch and the submodule fetch
    for cmd in seen:
        assert "protocol.allow=never" in cmd and "protocol.https.allow=always" in cmd
        assert cmd.index("--") == len(cmd) - 3  # `-- <repository> <ref>`: nothing after -- is an option
        assert Path(cmd[-2]) == world.up.resolve()


def test_a_git_redirecting_environment_does_not_change_which_repository_is_used(world, tmp_path, capsys, monkeypatch):
    """F1: GIT_DIR, GIT_INDEX_FILE and friends in the environment must not send git elsewhere."""
    v2 = _v2(world)
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    _git(decoy, "init", "-q")
    keys = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY")
    for key, value in zip(keys, (str(decoy / ".git"), str(decoy), str(decoy / "idx"), str(decoy / "obj")), strict=True):
        monkeypatch.setenv(key, value)

    code, _out, err = world.update(capsys)

    for key in keys:
        monkeypatch.delenv(key)
    assert code == 0, err
    assert _git(world.sup / SUB, "rev-parse", "HEAD") == v2
    assert sorted(_git(world.sup, "diff", "--cached", "--name-only").split()) == sorted([SUB, "idx.json"])


def test_an_oversize_symbol_file_is_refused(world, capsys, monkeypatch):
    """F2: a member above MEMBER_LIMIT ends the run before anything is built."""
    _v2(world)
    monkeypatch.setattr(refresh, "MEMBER_LIMIT", 200)
    before = world.snapshot()
    code, _out, err = world.update(capsys)
    assert code == 2 and "larger than 200 bytes" in err
    _assert_nothing_moved(world, before)


def test_an_oversize_archive_is_refused(world, capsys, monkeypatch):
    _v2(world)
    monkeypatch.setattr(refresh, "ARCHIVE_LIMIT", 2048)
    before = world.snapshot()
    code, _out, err = world.update(capsys)
    assert code == 2 and "archive" in err and "larger than 2048" in err
    _assert_nothing_moved(world, before)


def test_an_existing_directory_named_like_an_option_is_still_refused(world, tmp_path, capsys, monkeypatch):
    """The leading-dash rule stands on its own: a local directory called -x passes is_dir()."""
    (tmp_path / "-x").mkdir()
    monkeypatch.chdir(tmp_path)
    before = world.snapshot()
    code = refresh.main(["--update", "--repo-root", str(world.sup), "--upstream=-x", "--index", str(world.index)])
    assert code == 2 and "upstream" in capsys.readouterr().err
    _assert_nothing_moved(world, before)


# --- delta review: global config, interrupts, relative paths, credentials ---------------------------


def test_the_git_environment_cuts_off_user_and_system_configuration(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/some/user/gitconfig")
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    env = refresh._git_env()
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull and env["GIT_CONFIG_NOSYSTEM"] == "1"


def test_a_user_gitconfig_cannot_rewrite_the_upstream(world, tmp_path, capsys, monkeypatch):
    """BH1: an insteadOf in the user's global config would send the https fetch to a repository of its choosing."""
    _v2(world)
    config = tmp_path / "evil-gitconfig"
    config.write_text(f'[url "{world.up.as_posix()}"]\n\tinsteadOf = https://example.invalid/up\n', encoding="utf-8")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    (tmp_path / ".gitconfig").write_text(config.read_text(encoding="utf-8"), encoding="utf-8")
    before = world.snapshot()

    code = refresh.main(["--update", "--repo-root", str(world.sup), "--upstream", "https://example.invalid/up",
                         "--index", str(world.index)])

    assert code == 2 and "cannot fetch" in capsys.readouterr().err  # the rewrite was ignored: nothing to reach
    _assert_nothing_moved(world, before)


def test_an_interrupt_during_the_apply_restores_everything_and_propagates(world, capsys, monkeypatch):
    """F1: Ctrl-C between the index replace and git add must not leave a half-moved state."""
    _v2(world)
    before = world.snapshot()
    real = refresh._git

    def interrupt_on_add(cwd, *args, **kw):
        if args[0] == "add":
            raise KeyboardInterrupt
        return real(cwd, *args, **kw)

    monkeypatch.setattr(refresh, "_git", interrupt_on_add)

    with pytest.raises(KeyboardInterrupt):
        world.update(capsys)

    monkeypatch.setattr(refresh, "_git", real)
    _assert_nothing_moved(world, before)


def test_a_relative_local_upstream_is_resolved_against_the_callers_directory(world, capsys, monkeypatch):
    """F2: git runs inside the scratch repository, so a relative path must be made absolute first."""
    v2 = _v2(world)
    monkeypatch.chdir(world.up.parent)
    code = refresh.main(["--update", "--repo-root", str(world.sup), f"--upstream={world.up.name}",
                         "--index", str(world.index)])
    out = capsys.readouterr()
    assert code == 0, out.err
    assert _git(world.sup / SUB, "rev-parse", "HEAD") == v2


@pytest.mark.parametrize("url", ["https://user:s3cretpw@example.invalid/up", "https://s3cretpw@example.invalid/up"])
def test_credentials_in_an_upstream_url_are_refused_and_never_printed(world, capsys, url):
    """F3: userinfo is refused outright (a token belongs in a credential helper, which this script cuts off)."""
    before = world.snapshot()
    code = refresh.main(["--update", "--repo-root", str(world.sup), f"--upstream={url}", "--index", str(world.index)])
    captured = capsys.readouterr()
    assert code == 2 and "without credentials" in captured.err
    assert "s3cretpw" not in captured.err and "s3cretpw" not in captured.out
    _assert_nothing_moved(world, before)


def test_the_redaction_hides_userinfo_wherever_a_url_is_printed():
    assert "pw" not in refresh._redact("https://u:pw@host/x") and "host/x" in refresh._redact("https://u:pw@host/x")
    assert refresh._redact("https://host/x") == "https://host/x"
