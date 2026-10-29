"""The batch gate's standing publishability check (publish-prep ticket 03a, item 6).

``batch_gate.check_publishable_tracked`` exports the TRACKED files (``git checkout-index``, never
the working tree) into a temporary directory and runs ``scripts/check_publishable.py`` over them
with the owner's private pattern file named by ``BOARDPROOF_FORBIDDEN_FILE``, ``--strict-archives``
and an explicit allow-list of reviewed hits and unopened archives. These tests use throw-away git
repositories and a made-up pattern file (``zzforbidden``); the owner's real file is never read.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("batch_gate_pub", ROOT / "scripts" / "batch_gate.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules["batch_gate_pub"] = gate
_spec.loader.exec_module(gate)

CHECKER = ROOT / "scripts" / "check_publishable.py"
ENV = "BOARDPROOF_FORBIDDEN_FILE"
HIT = "the name zzforbidden appears here"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                   check=True, capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "ok.txt").write_text("clean\n", encoding="utf-8")
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_text("# a made-up pattern\nzzforbidden\n", encoding="utf-8")
    monkeypatch.setenv(ENV, str(forbidden))
    return r


def blob(repo: Path, path: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "rev-parse", f":{path}"], capture_output=True, text=True,
                          check=True).stdout.strip()


def commit(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "c")


def check(repo: Path, said: list[str] | None = None, **kw) -> list[str]:
    said = said if said is not None else []
    kw.setdefault("allowed_hits", {})
    kw.setdefault("allowed_archives", ())
    return gate.check_publishable_tracked(repo, sys.executable, said.append, checker=CHECKER, **kw)


def test_clean_tracked_tree_passes(repo):
    commit(repo)
    said: list[str] = []
    assert check(repo, said) == []
    assert any("publishability" in s and "no hits" in s for s in said), said


def test_unset_variable_is_red(repo, monkeypatch):
    commit(repo)
    monkeypatch.delenv(ENV)
    reasons = check(repo)
    assert reasons and ENV in reasons[0] and "unset" in reasons[0]


def test_empty_variable_is_red(repo, monkeypatch):
    commit(repo)
    monkeypatch.setenv(ENV, "")
    assert "unset" in check(repo)[0]


def test_missing_pattern_file_is_red(repo, monkeypatch, tmp_path):
    commit(repo)
    monkeypatch.setenv(ENV, str(tmp_path / "nope.txt"))
    reasons = check(repo)
    assert reasons and "not a file" in reasons[0]


@pytest.mark.parametrize("text", ["", "\n\n", "# only a comment\n"])
def test_empty_pattern_file_is_red(repo, monkeypatch, tmp_path, text):
    commit(repo)
    f = tmp_path / "empty.txt"
    f.write_text(text, encoding="utf-8")
    monkeypatch.setenv(ENV, str(f))
    reasons = check(repo)
    assert reasons and "could not run" in reasons[0], reasons


def test_invalid_regex_in_the_pattern_file_is_red(repo, monkeypatch, tmp_path):
    commit(repo)
    f = tmp_path / "bad.txt"
    f.write_text("(unclosed\n", encoding="utf-8")
    monkeypatch.setenv(ENV, str(f))
    assert "could not run" in check(repo)[0]


def test_tracked_hit_is_red_and_the_log_never_prints_the_matched_text(repo):
    (repo / "notes.md").write_text(HIT + "\n", encoding="utf-8")
    commit(repo)
    said: list[str] = []
    reasons = check(repo, said)
    assert reasons and "1 hit" in reasons[0]
    log = "\n".join(said)
    assert "notes.md:1: [forbidden#1]" in log
    assert "zzforbidden" not in log, "the matched span must not reach the gate log"


def test_untracked_and_ignored_files_are_not_looked_at(repo):
    (repo / ".gitignore").write_text("ignored/\n", encoding="utf-8")
    commit(repo)
    (repo / "untracked.txt").write_text(HIT + "\n", encoding="utf-8")
    (repo / "ignored").mkdir()
    (repo / "ignored" / "x.txt").write_text(HIT + "\n", encoding="utf-8")
    with zipfile.ZipFile(repo / "stray.zip", "w") as z:
        z.writestr("a", "b")
    assert check(repo) == []


def test_the_export_comes_from_the_index_not_the_working_tree(repo):
    (repo / "notes.md").write_text("clean\n", encoding="utf-8")
    commit(repo)
    (repo / "notes.md").write_text(HIT + "\n", encoding="utf-8")  # unstaged edit
    assert check(repo) == []
    _git(repo, "add", "notes.md")  # staged: now the index holds it
    assert check(repo)


def test_scratch_tree_is_not_part_of_the_published_tree(repo):
    old_notes = repo / ("." + "scratch")  # not a quoted path: see tests/test_scratch_absent_skips.py
    old_notes.mkdir()
    (old_notes / "t.md").write_text(HIT + "\n", encoding="utf-8")
    commit(repo)
    assert check(repo) == []


def test_allow_listed_hits_pass_but_only_up_to_their_count(repo):
    (repo / "fx.txt").write_text(HIT + "\n", encoding="utf-8")
    commit(repo)
    assert check(repo, allowed_hits={"fx.txt": (1, blob(repo, "fx.txt"))}) == []
    (repo / "fx.txt").write_text(HIT + "\n" + HIT + "\n", encoding="utf-8")
    commit(repo)
    reasons = check(repo, allowed_hits={"fx.txt": (1, blob(repo, "fx.txt"))})
    assert reasons and "outside the reviewed allow-list" in reasons[0]


def test_a_reviewed_file_that_changed_is_red_even_with_the_same_hit_count(repo):
    # A new leak replacing the reviewed one (same file, same count) must not pass unseen.
    (repo / "fx.txt").write_text(HIT + "\n", encoding="utf-8")
    commit(repo)
    reviewed = {"fx.txt": (1, blob(repo, "fx.txt"))}
    assert check(repo, allowed_hits=reviewed) == []
    (repo / "fx.txt").write_text("the name zzforbidden, a different one\n", encoding="utf-8")
    commit(repo)
    said: list[str] = []
    reasons = check(repo, said, allowed_hits=reviewed)
    assert reasons and "outside the reviewed allow-list" in reasons[0]
    assert "changed since its review" in "\n".join(said)
    assert "zzforbidden" not in "\n".join(said)


def test_allow_list_does_not_cover_other_files(repo):
    (repo / "fx.txt").write_text(HIT + "\n", encoding="utf-8")
    (repo / "other.txt").write_text(HIT + "\n", encoding="utf-8")
    commit(repo)
    reasons = check(repo, allowed_hits={"fx.txt": (1, blob(repo, "fx.txt"))})
    assert reasons and "1 hit" in reasons[0]


def test_exit_3_without_a_parsable_archive_line_is_red(repo, monkeypatch):
    # fail-open guard: the checker says "an archive was refused" but the output cannot be read
    commit(repo)
    real = gate.subprocess.run

    def odd(cmd, *a, **kw):
        if any(str(c).endswith("check_publishable.py") for c in cmd):
            return subprocess.CompletedProcess(cmd, 3, stdout="something unexpected\n", stderr="")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(gate.subprocess, "run", odd)
    said: list[str] = []
    reasons = check(repo, said)
    assert reasons and "output could not be read" in reasons[0]


def _zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("a", "b")


def test_new_unopened_archive_is_red(repo):
    _zip(repo / "new.zip")
    commit(repo)
    said: list[str] = []
    reasons = check(repo, said)
    assert reasons and "archive" in reasons[0]
    assert "  new.zip" in said and "outside the reviewed allow-list" in "\n".join(said)


def test_allow_listed_archive_passes(repo):
    _zip(repo / "known.zip")
    commit(repo)
    assert check(repo, allowed_archives=("known.zip",)) == []


def test_a_second_archive_next_to_an_allowed_one_is_red(repo):
    _zip(repo / "known.zip")
    _zip(repo / "other.zip")
    commit(repo)
    assert check(repo, allowed_archives=("known.zip",))


def test_checker_that_hangs_is_red(repo, monkeypatch):
    commit(repo)
    real = gate.subprocess.run

    def slow(cmd, *a, **kw):
        if any(str(c).endswith("check_publishable.py") for c in cmd):
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 1))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(gate.subprocess, "run", slow)
    reasons = check(repo)
    assert reasons and "timed out" in reasons[0]


def test_export_that_fails_is_red(tmp_path, monkeypatch):
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    monkeypatch.setenv(ENV, str(tmp_path / "f.txt"))
    (tmp_path / "f.txt").write_text("zzforbidden\n", encoding="utf-8")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    reasons = check(not_a_repo)
    assert reasons and "export" in reasons[0]


def test_missing_checker_script_is_red(repo):
    commit(repo)
    reasons = gate.check_publishable_tracked(repo, sys.executable, lambda s: None, checker=repo / "nope.py",
                                             allowed_hits={}, allowed_archives=())
    assert reasons


# --------------------------------------------------------------------------- the real allow-lists


def _tracked() -> set[str]:
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files"], capture_output=True, text=True, check=True).stdout
    return set(out.splitlines())


def test_the_reviewed_allow_lists_name_exactly_what_the_ticket_says():
    # public export: the KiCad-written fixtures were regenerated or dropped, the datasheet PDFs left out
    assert gate.PUBLISH_ALLOWED_HITS == {}
    assert gate.PUBLISH_ALLOWED_ARCHIVES == (
        "tests/fixtures/golden_attiny1616_board/manufacturing/gerbers/gerbers.zip",
    )


def test_every_allow_listed_entry_is_a_tracked_file():
    # A stale entry (a regenerated or dropped fixture) must leave the list: it would hide a new hit.
    tracked = _tracked()
    for p in (*gate.PUBLISH_ALLOWED_HITS, *gate.PUBLISH_ALLOWED_ARCHIVES):
        assert p in tracked, f"{p} is allow-listed but not tracked: shrink the list in scripts/batch_gate.py"


def test_reviewed_hit_files_are_pinned_to_the_blob_that_was_reviewed():
    # Changing a reviewed fixture (even to another leak with the same hit count) needs a reviewed
    # change of its blob id in scripts/batch_gate.py.
    if not gate.PUBLISH_ALLOWED_HITS:  # the public export has none (an empty pathspec would list every file)
        return
    out = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-s", "--", *gate.PUBLISH_ALLOWED_HITS],
                         capture_output=True, text=True, check=True).stdout.splitlines()
    current = {line.split("\t", 1)[1]: line.split()[1] for line in out}
    assert current == {k: v[1] for k, v in gate.PUBLISH_ALLOWED_HITS.items()}


def test_the_publish_step_fits_inside_the_printed_worst_case():
    # Its budget was taken from the lint timeout, so the worst case, the builders' outer timeout and
    # the figures quoted in docs/TESTING.md and the playbook stay as they were.
    assert gate.PUBLISH_BUDGET_S + gate.LINT_TIMEOUT_S == 300
    assert gate.worst_case_s(gate.SUITE_TIMEOUT_S) == 10110  # about 168 min, as before
    assert gate.worst_case_s(gate.SUITE_TIMEOUT_S) + 600 <= 10800  # builder.md's outer timeout


# --------------------------------------------------------------------------- --publishable-only


def test_publishable_only_prints_one_verdict_line_and_the_exit_code(repo, capsys, monkeypatch):
    monkeypatch.setattr(gate, "PUBLISH_CHECK", CHECKER)
    monkeypatch.setattr(gate, "PUBLISH_ALLOWED_HITS", {})
    monkeypatch.setattr(gate, "PUBLISH_ALLOWED_ARCHIVES", ())
    commit(repo)
    capsys.readouterr()
    assert gate.main(["--publishable-only"], root=repo) == 0
    assert capsys.readouterr().out.splitlines()[-1] == "PUBLISHABLE: OK"
    (repo / "notes.md").write_text(HIT + "\n", encoding="utf-8")
    commit(repo)
    assert gate.main(["--publishable-only"], root=repo) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[-1].startswith("PUBLISHABLE: RED (") and "zzforbidden" not in "\n".join(out)
    monkeypatch.delenv(ENV)
    assert gate.main(["--publishable-only"], root=repo) == 1
    assert ENV in capsys.readouterr().out


# --------------------------------------------------------------------------- the identity guard's state


def test_gate_reports_the_state_of_the_identity_guard(repo, tmp_path):
    (repo / "scripts" / "git-hooks").mkdir(parents=True)
    source = repo / "scripts" / "git-hooks" / "commit-msg"
    source.write_bytes((ROOT / "scripts" / "git-hooks" / "commit-msg").read_bytes())
    hooks = repo / ".git" / "hooks"
    assert gate.hook_states(repo)[1].startswith("NOT INSTALLED (cp scripts/git-hooks/commit-msg ")
    (hooks / "commit-msg").write_bytes(source.read_bytes().replace(b"\n", b"\r\n"))  # CRLF copy is the same hook
    assert gate.hook_states(repo)[1] == "installed"
    (hooks / "commit-msg").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    assert gate.hook_states(repo)[1] == "DIFFERS from scripts/git-hooks/commit-msg"
    assert gate.pre_push_state(repo).startswith("NOT INSTALLED")
