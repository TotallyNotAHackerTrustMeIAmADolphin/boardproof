"""The identity guard (``scripts/git-hooks/commit-msg``, publish-prep ticket 03a item 5).

A commit whose author or committer e-mail is not the public alias's noreply address is refused,
in a throw-away repository with the hook installed the way the playbook installs the pre-push
guard (a copy into the common git dir). Normal commits with the alias go through, also amends and
a squash merge.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from tests._hermetic_git import hermetic_env, run_git

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "git-hooks" / "commit-msg"
ALIAS = "TotallyNotAHackerTrustMeIAmADolphin"
NOREPLY = f"60357333+{ALIAS}@users.noreply.github.com"
WRONG = "someone@example.invalid"


def _git(cwd: Path, *args: str, check: bool = True, env: dict | None = None) -> subprocess.CompletedProcess:
    return run_git(*args, cwd=cwd, text=True, check=check, extra_env=env)


def _repo(tmp_path: Path, hook: Path = HOOK, email: str = NOREPLY) -> Path:
    if shutil.which("git") is None:
        pytest.fail("git is not on PATH; the identity guard cannot be tested")
    repo = tmp_path / "r"
    _git(tmp_path, "init", "-q", "-b", "main", str(repo))
    _git(repo, "config", "user.name", ALIAS)
    _git(repo, "config", "user.email", email)
    _git(repo, "config", "commit.gpgsign", "false")
    hooks = repo / ".git" / "hooks"
    hooks.mkdir(exist_ok=True)
    shutil.copyfile(hook, hooks / "commit-msg")
    (hooks / "commit-msg").chmod(0o755)
    return repo


def _commit(repo: Path, name: str = "f", *extra: str, env: dict | None = None) -> subprocess.CompletedProcess:
    (repo / name).write_text(name + "\n")
    _git(repo, "add", name)
    return _git(repo, "commit", "-qm", f"add {name}", *extra, check=False, env=env)


def test_hook_is_lf_only():
    assert b"\r" not in HOOK.read_bytes()


def test_alias_commit_passes_and_amend_passes(tmp_path):
    repo = _repo(tmp_path)
    r = _commit(repo)
    assert r.returncode == 0, r.stderr
    assert _git(repo, "commit", "-q", "--amend", "-m", "again", check=False).returncode == 0
    assert _git(repo, "log", "-1", "--format=%ae|%ce").stdout.strip() == f"{NOREPLY}|{NOREPLY}"


def test_wrong_configured_identity_is_refused(tmp_path):
    repo = _repo(tmp_path, email=WRONG)
    r = _commit(repo)
    assert r.returncode != 0
    assert "identity guard: BLOCKED" in r.stderr and "author" in r.stderr and "committer" in r.stderr
    assert _git(repo, "rev-parse", "--verify", "-q", "HEAD", check=False).returncode != 0  # nothing committed


def test_wrong_author_flag_is_refused(tmp_path):
    repo = _repo(tmp_path)
    r = _commit(repo, "f", f"--author=X <{WRONG}>")
    assert r.returncode != 0
    assert "the author e-mail" in r.stderr and "committer e-mail" not in r.stderr


def test_wrong_committer_environment_is_refused(tmp_path):
    repo = _repo(tmp_path)
    r = _commit(repo, env={"GIT_COMMITTER_EMAIL": WRONG})
    assert r.returncode != 0
    assert "the committer e-mail" in r.stderr and "author e-mail" not in r.stderr


def test_wrong_author_environment_is_refused(tmp_path):
    repo = _repo(tmp_path)
    r = _commit(repo, env={"GIT_AUTHOR_EMAIL": WRONG})
    assert r.returncode != 0 and "the author e-mail" in r.stderr


def test_empty_email_is_refused(tmp_path):
    repo = _repo(tmp_path, email="")
    r = _commit(repo, "f", env={"GIT_AUTHOR_EMAIL": "", "GIT_COMMITTER_EMAIL": ""})
    assert r.returncode != 0


def test_private_address_with_noreply_substring_is_refused(tmp_path):
    # an exact comparison, not a substring match
    repo = _repo(tmp_path, email=f"x{NOREPLY}")
    assert _commit(repo).returncode != 0


def test_squash_merge_with_the_alias_passes(tmp_path):
    repo = _repo(tmp_path)
    assert _commit(repo, "a").returncode == 0
    _git(repo, "checkout", "-q", "-b", "ticket-x")
    assert _commit(repo, "b").returncode == 0
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--squash", "ticket-x")
    r = _git(repo, "commit", "-qm", "Squash-merge ticket x", check=False)
    assert r.returncode == 0, r.stderr


def test_squash_merge_with_a_wrong_identity_is_refused(tmp_path):
    repo = _repo(tmp_path)
    assert _commit(repo, "a").returncode == 0
    _git(repo, "checkout", "-q", "-b", "ticket-x")
    assert _commit(repo, "b").returncode == 0
    _git(repo, "checkout", "-q", "main")
    _git(repo, "merge", "--squash", "ticket-x")
    _git(repo, "config", "user.email", WRONG)
    assert _git(repo, "commit", "-qm", "Squash-merge", check=False).returncode != 0


def test_mutant_hook_that_accepts_everything_turns_the_refusal_tests_red(tmp_path):
    # The mutant: the comparison removed. The refusal test above must fail against it.
    mutant = tmp_path / "commit-msg-mutant"
    text = HOOK.read_text(encoding="utf-8")
    assert 'if [ "$email" != "$allowed" ]; then' in text
    mutant.write_text(text.replace('if [ "$email" != "$allowed" ]; then', "if false; then"), encoding="utf-8",
                      newline="\n")
    (tmp_path / "m").mkdir()
    repo = _repo(tmp_path / "m", hook=mutant, email=WRONG)
    assert _commit(repo).returncode == 0  # the mutant lets a wrong identity through: the real tests would fail


def test_mutant_hook_that_checks_only_the_author_misses_a_committer(tmp_path):
    mutant = tmp_path / "commit-msg-mutant"
    text = HOOK.read_text(encoding="utf-8")
    assert "for which in AUTHOR COMMITTER; do" in text
    mutant.write_text(text.replace("for which in AUTHOR COMMITTER; do", "for which in AUTHOR; do"),
                      encoding="utf-8", newline="\n")
    (tmp_path / "m").mkdir()
    repo = _repo(tmp_path / "m", hook=mutant)
    assert _commit(repo, env={"GIT_COMMITTER_EMAIL": WRONG}).returncode == 0


# --------------------------------------------------------------------------- push time
# The commit-msg hook does not see cherry-picks, rebases, `commit --no-verify` or `commit-tree`.
# The pre-push guard looks at the author and committer e-mail of every commit a push would add, so
# a commit that got around the commit-time hook is still caught before it leaves the machine.

PRE_PUSH = ROOT / "scripts" / "git-hooks" / "pre-push"


def _clone(tmp_path: Path, pre_push: Path = PRE_PUSH) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "-q", "-b", "main", str(clone))
    _git(clone, "remote", "add", "origin", str(remote))
    _git(clone, "config", "user.name", ALIAS)
    _git(clone, "config", "user.email", NOREPLY)
    _git(clone, "config", "commit.gpgsign", "false")
    hooks = clone / ".git" / "hooks"
    shutil.copyfile(pre_push, hooks / "pre-push")  # no commit-msg hook: the commit-time guard is bypassed
    (hooks / "pre-push").chmod(0o755)
    (clone / "a.txt").write_text("one\n")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-qm", "one")
    assert _git(clone, "push", "-q", "origin", "main", check=False).returncode == 0
    return remote, clone


def _remote_main(remote: Path) -> str:
    return _git(remote, "rev-parse", "main").stdout.strip()


def _plant(clone: Path, how: str) -> None:
    """Leave a commit with a wrong identity on main, the way each gap in the commit-time hook does."""
    wrong = {"GIT_AUTHOR_EMAIL": WRONG, "GIT_AUTHOR_NAME": "X"}
    wrong_c = {"GIT_COMMITTER_EMAIL": WRONG, "GIT_COMMITTER_NAME": "X"}
    if how == "no-verify":  # the hook is not even installed here; a direct wrong author
        (clone / "b.txt").write_text("b\n")
        _git(clone, "add", "b.txt")
        _git(clone, "commit", "-qm", "b", env=wrong)
    elif how == "cherry-pick":  # author stays wrong although the picker is the alias
        _git(clone, "checkout", "-q", "-b", "side")
        (clone / "b.txt").write_text("b\n")
        _git(clone, "add", "b.txt")
        _git(clone, "commit", "-qm", "b", env=wrong)
        _git(clone, "checkout", "-q", "main")
        _git(clone, "cherry-pick", "side")
    elif how == "rebase":  # leaves a wrong committer
        _git(clone, "checkout", "-q", "-b", "side")
        (clone / "b.txt").write_text("b\n")
        _git(clone, "add", "b.txt")
        _git(clone, "commit", "-qm", "b")
        _git(clone, "checkout", "-q", "main")
        (clone / "c.txt").write_text("c\n")
        _git(clone, "add", "c.txt")
        _git(clone, "commit", "-qm", "c")
        _git(clone, "checkout", "-q", "side")
        _git(clone, "rebase", "main", env=wrong_c)
        _git(clone, "checkout", "-q", "main")
        _git(clone, "merge", "-q", "--ff-only", "side")
    elif how == "commit-tree":
        tree = _git(clone, "rev-parse", "HEAD^{tree}").stdout.strip()
        sha = _git(clone, "commit-tree", tree, "-p", "HEAD", "-m", "t", env={**wrong, **wrong_c}).stdout.strip()
        _git(clone, "update-ref", "refs/heads/main", sha)
    else:
        raise AssertionError(how)


@pytest.mark.parametrize("how", ["no-verify", "cherry-pick", "rebase", "commit-tree"])
def test_push_of_a_commit_that_bypassed_the_commit_time_hook_is_refused(tmp_path, how):
    remote, clone = _clone(tmp_path)
    before = _remote_main(remote)
    _plant(clone, how)
    r = _git(clone, "push", "origin", "main", check=False)
    assert r.returncode != 0
    assert "pre-push guard: BLOCKED" in r.stderr and "e-mail" in r.stderr
    assert _remote_main(remote) == before


def test_push_of_a_new_branch_with_a_wrong_commit_is_refused(tmp_path):
    remote, clone = _clone(tmp_path)
    _git(clone, "checkout", "-q", "-b", "feature")
    (clone / "b.txt").write_text("b\n")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-qm", "b", env={"GIT_AUTHOR_EMAIL": WRONG})
    r = _git(clone, "push", "origin", "feature", check=False)
    assert r.returncode != 0 and "e-mail" in r.stderr


def test_push_of_alias_commits_passes_also_new_branches_and_tags(tmp_path):
    remote, clone = _clone(tmp_path)
    (clone / "a.txt").write_text("two\n")
    _git(clone, "commit", "-qam", "two")
    assert _git(clone, "push", "origin", "main", check=False).returncode == 0
    _git(clone, "checkout", "-q", "-b", "feature")
    (clone / "b.txt").write_text("b\n")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-qm", "b")
    assert _git(clone, "push", "origin", "feature", check=False).returncode == 0
    _git(clone, "tag", "-a", "v1", "-m", "v1")
    assert _git(clone, "push", "origin", "v1", check=False).returncode == 0
    assert _git(clone, "push", "origin", "--delete", "feature", check=False).returncode == 0


def test_an_old_wrong_commit_already_on_the_remote_does_not_block_later_pushes(tmp_path):
    # only the commits a push would ADD are looked at
    remote, clone = _clone(tmp_path)
    (clone / "a.txt").write_text("two\n")
    _git(clone, "commit", "-qam", "two")
    assert _git(clone, "push", "origin", "main", check=False).returncode == 0
    (clone / "a.txt").write_text("three\n")
    _git(clone, "commit", "-qam", "three")
    assert _git(clone, "push", "origin", "main", check=False).returncode == 0


def test_mutant_pre_push_without_the_identity_check_lets_the_wrong_commit_through(tmp_path):
    text = PRE_PUSH.read_text(encoding="utf-8")
    assert "check_identity" in text
    mutant = tmp_path / "pre-push-mutant"
    mutant.write_text(text.replace('check_identity "$remote_sha" "$local_sha" || status=1', ":")
                      .replace('check_identity "" "$local_sha" || status=1', ":"), encoding="utf-8", newline="\n")
    (tmp_path / "m").mkdir()
    remote, clone = _clone(tmp_path / "m", pre_push=mutant)
    _plant(clone, "cherry-pick")
    assert _git(clone, "push", "origin", "main", check=False).returncode == 0


# --- issue 9 item 7 ----------------------------------------------------------------------------


def _new_branch_with_a_wrong_commit_in_the_middle(clone: Path) -> None:
    _git(clone, "checkout", "-q", "-b", "feature")
    (clone / "b.txt").write_text("b\n")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-qm", "b", env={"GIT_AUTHOR_EMAIL": WRONG})  # wrong, and not the tip
    (clone / "c.txt").write_text("c\n")
    _git(clone, "add", "c.txt")
    _git(clone, "commit", "-qm", "c")  # the tip has the alias


def test_push_of_a_new_branch_whose_wrong_commit_is_in_the_middle_is_refused(tmp_path):
    remote, clone = _clone(tmp_path)
    _new_branch_with_a_wrong_commit_in_the_middle(clone)
    r = _git(clone, "push", "origin", "feature", check=False)
    assert r.returncode != 0 and "e-mail" in r.stderr
    assert _git(remote, "branch", "--list", "feature").stdout.strip() == ""


def test_mutant_pre_push_that_looks_only_at_the_tip_of_a_new_branch_lets_the_middle_commit_through(tmp_path):
    text = PRE_PUSH.read_text(encoding="utf-8")
    assert 'range="$2 --not --remotes"' in text
    mutant = tmp_path / "pre-push-mutant"
    mutant.write_text(text.replace('range="$2 --not --remotes"', 'range="$2 --max-count=1"'),
                      encoding="utf-8", newline="\n")
    (tmp_path / "m").mkdir()
    remote, clone = _clone(tmp_path / "m", pre_push=mutant)
    _new_branch_with_a_wrong_commit_in_the_middle(clone)
    assert _git(clone, "push", "origin", "feature", check=False).returncode == 0  # the mutant misses it


def _hook_run(clone: Path, hook: Path, local_sha: str) -> subprocess.CompletedProcess:
    """The pre-push hook run directly, as git would: one line on stdin for a new branch."""
    line = f"refs/heads/feature {local_sha} refs/heads/feature {'0' * 40}\n"
    done = subprocess.run(["sh", str(hook), "origin", "url"], cwd=clone, input=line.encode(),
                          capture_output=True, env=hermetic_env(), check=False)  # bytes: no CRLF translation
    done.stderr = done.stderr.decode("utf-8", errors="replace")
    return done


def _clone_with_a_missing_object(tmp_path: Path, hook: Path = PRE_PUSH) -> tuple[Path, str]:
    """A new branch b -> c where the loose object of b is gone: `git log c --not --remotes` fails."""
    remote, clone = _clone(tmp_path, pre_push=hook)
    _git(clone, "checkout", "-q", "-b", "feature")
    (clone / "b.txt").write_text("b\n")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-qm", "b")
    middle = _git(clone, "rev-parse", "HEAD").stdout.strip()
    (clone / "c.txt").write_text("c\n")
    _git(clone, "add", "c.txt")
    _git(clone, "commit", "-qm", "c")
    tip = _git(clone, "rev-parse", "HEAD").stdout.strip()
    gone = clone / ".git" / "objects" / middle[:2] / middle[2:]
    gone.chmod(0o700)  # git writes objects read-only
    gone.unlink()
    assert _git(clone, "log", "--format=%h", tip, "--not", "--remotes", check=False).returncode != 0
    return clone, tip


def test_a_git_log_that_fails_is_a_refusal_not_a_pass(tmp_path):
    clone, tip = _clone_with_a_missing_object(tmp_path)
    done = _hook_run(clone, PRE_PUSH, tip)
    assert done.returncode == 1
    assert "pre-push guard: BLOCKED" in done.stderr and "could not be checked" in done.stderr


def test_mutant_pre_push_that_ignores_a_failing_git_log_passes_the_unchecked_range(tmp_path):
    text = PRE_PUSH.read_text(encoding="utf-8")
    assert "could not be checked" in text
    mutant = tmp_path / "pre-push-mutant"
    mutant.write_text(text.replace("if ! log=$(git log", "if false && log=$(git log"), encoding="utf-8", newline="\n")
    assert mutant.read_text(encoding="utf-8") != text
    (tmp_path / "m").mkdir()
    clone, tip = _clone_with_a_missing_object(tmp_path / "m", mutant)
    assert _hook_run(clone, mutant, tip).returncode == 0  # the mutant waves the unchecked range through


def test_the_block_messages_of_pre_push_have_no_stray_tabs():
    """Only leading indentation may be a tab: a tab inside a line is part of the message text."""
    text = PRE_PUSH.read_text(encoding="utf-8")
    inner = [line for line in text.splitlines() if "\t" in line.lstrip("\t")]
    assert inner == []


def test_pre_push_does_not_claim_a_tag_on_a_non_commit_fails_git_log():
    """Measured: `git log <tree> --not --remotes` exits 0 with no output, so that example was false."""
    text = PRE_PUSH.read_text(encoding="utf-8")
    assert "non-commit" not in text
    assert "missing or corrupt object" in text
