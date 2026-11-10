"""Hermetic git for every test that runs git (issue 174, follow-up of 157).

- ``tests/_hermetic_git.py`` cuts off the user's global git settings; the tests here run it against a
  hostile configuration (a global ignore file, autocrlf on, a commit identity, no safe.directory) and
  show each setting is neutralised; each has a control run without the helper that DOES leak.
- Ratchet: no test module runs a bare ``["git", ...]`` command (``tests/*.py`` is scanned by AST); a
  call must go through ``run_git`` or pass an environment built by ``hermetic_env``.
- The default wiring of ``test_gitignore._git`` and ``run_git`` passes the isolated environment.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path

import pytest

from tests import _hermetic_git as hg

TESTS = Path(__file__).resolve().parent
HELPER = "_hermetic_git.py"


def _hostile_base(tmp_path: Path) -> dict[str, str]:
    """An environment whose global git config ignores a file, turns autocrlf on, names a user and
    trusts no directory."""
    ignore = tmp_path / "global_ignore"
    ignore.write_text("planted.txt\n", encoding="utf-8")
    cfg = tmp_path / "hostile_gitconfig"
    cfg.write_text(
        f"[core]\n\texcludesFile = {ignore.as_posix()}\n\tautocrlf = true\n"
        "[user]\n\tname = Someone Else\n\temail = someone@example.invalid\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.update(GIT_CONFIG_GLOBAL=str(cfg), XDG_CONFIG_HOME=str(tmp_path / "xdg_hostile"))
    env.pop("GIT_CONFIG_NOSYSTEM", None)
    return env


def _get(env: dict[str, str], repo: Path, key: str) -> str:
    r = subprocess.run(["git", "config", "--get", key], cwd=repo, env=env, capture_output=True, text=True, timeout=60)
    return r.stdout.strip()


def _ignored(env: dict[str, str], repo: Path) -> bool:
    (repo / "planted.txt").write_text("x\n", encoding="utf-8")
    r = subprocess.run(["git", "check-ignore", "-q", "--no-index", "planted.txt"], cwd=repo, env=env, timeout=60)
    return r.returncode == 0


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "r"
    r.mkdir()
    hg.run_git("init", "-q", cwd=r)
    return r


@pytest.mark.parametrize("key,hostile_value,hermetic_value", [
    ("core.autocrlf", "true", "false"),
    ("user.name", "Someone Else", ""),
    ("user.email", "someone@example.invalid", ""),
])
def test_a_hostile_global_setting_does_not_reach_a_hermetic_run(tmp_path, repo, key, hostile_value, hermetic_value):
    hostile = _hostile_base(tmp_path)
    assert _get(hostile, repo, key) == hostile_value  # control: the setting is live without the helper
    assert _get(hg.hermetic_env(hostile), repo, key) == hermetic_value


def test_a_hostile_global_ignore_file_does_not_reach_a_hermetic_run(tmp_path, repo):
    hostile = _hostile_base(tmp_path)
    assert _ignored(hostile, repo)  # control
    assert not _ignored(hg.hermetic_env(hostile), repo)


def test_safe_directory_is_trusted_through_the_helpers_own_global_file(tmp_path):
    hostile = _hostile_base(tmp_path)
    assert _get(hostile, tmp_path, "safe.directory") == ""  # control: the hostile file trusts nothing
    assert _get(hg.hermetic_env(hostile), tmp_path, "safe.directory") == "*"


def test_environment_overrides_of_identity_and_config_are_dropped(tmp_path):
    base = _hostile_base(tmp_path)
    base.update(GIT_AUTHOR_NAME="x", GIT_COMMITTER_EMAIL="x@y", GIT_CONFIG_COUNT="1",
                GIT_CONFIG_KEY_0="core.autocrlf", GIT_CONFIG_VALUE_0="true")
    env = hg.hermetic_env(base)
    assert "GIT_AUTHOR_NAME" not in env and "GIT_COMMITTER_EMAIL" not in env
    assert env["GIT_CONFIG_COUNT"] == "0" and "GIT_CONFIG_KEY_0" not in env


def test_identity_is_opt_in_and_a_commit_needs_it(repo):
    (repo / "a.txt").write_text("a\n", encoding="utf-8")
    hg.run_git("add", "a.txt", cwd=repo)
    env = hg.hermetic_env()
    assert "GIT_AUTHOR_NAME" not in env
    hg.run_git("commit", "-q", "-m", "x", cwd=repo, identity=True)
    log = hg.run_git("log", "-1", "--format=%an <%ae>", cwd=repo, text=True).stdout.strip()
    assert log == "t <t@example.invalid>"


def test_apply_hermetic_env_changes_the_process_environment_and_restores_it(monkeypatch, tmp_path):
    for k, v in _hostile_base(tmp_path).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "x")
    cfg = os.environ["GIT_CONFIG_GLOBAL"]
    with pytest.MonkeyPatch.context() as mp:
        hg.apply_hermetic_env(mp)
        assert os.environ["GIT_CONFIG_GLOBAL"] != cfg and "GIT_AUTHOR_NAME" not in os.environ
    assert os.environ["GIT_CONFIG_GLOBAL"] == cfg and os.environ["GIT_AUTHOR_NAME"] == "x"


# --- default wiring: a call that forgets the isolation is caught here, on any machine -------------


class _Spy:
    def __init__(self) -> None:
        self.env: dict[str, str] | None = None

    def __call__(self, cmd, **kw):
        self.env = kw.get("env")
        return subprocess.CompletedProcess(cmd, 0, "", "")


def _assert_isolated(env: dict[str, str] | None, hostile_cfg: str) -> None:
    assert env is not None, "the call passed no env, so it inherits the user's git configuration"
    assert env["GIT_CONFIG_GLOBAL"] != hostile_cfg
    assert Path(env["GIT_CONFIG_GLOBAL"]) == hg._global_config()
    assert env["GIT_CONFIG_NOSYSTEM"] == "1" and "GIT_AUTHOR_NAME" not in env


def test_run_git_passes_the_isolated_environment(monkeypatch, tmp_path):
    spy = _Spy()
    monkeypatch.setattr(hg.subprocess, "run", spy)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "hostile-config")
    hg.run_git("status")
    _assert_isolated(spy.env, "hostile-config")


def test_the_gitignore_tests_default_wiring_passes_the_isolated_environment(monkeypatch, tmp_path):
    import tests.test_gitignore as gi

    spy = _Spy()
    monkeypatch.setattr(gi.subprocess, "run", spy)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "hostile-config")
    gi._git(tmp_path, "status")
    _assert_isolated(spy.env, "hostile-config")


def test_the_default_wiring_check_fires_on_a_call_without_the_isolation(monkeypatch):
    # the mutant: a wrapper that forgets env= (or the helper) is red under the same assertion
    spy = _Spy()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "hostile-config")
    spy(["git", "status"])
    with pytest.raises(AssertionError):
        _assert_isolated(spy.env, "hostile-config")
    spy(["git", "status"], env=dict(os.environ))
    with pytest.raises(AssertionError):
        _assert_isolated(spy.env, "hostile-config")


# --- the ratchet: no bare subprocess git call in tests/ -------------------------------------------

_SUBPROCESS_FUNCS = {"run", "Popen", "call", "check_call", "check_output"}


def _is_git_list(node: ast.AST) -> bool:
    return (isinstance(node, (ast.List, ast.Tuple)) and bool(node.elts)
            and isinstance(node.elts[0], ast.Constant) and node.elts[0].value == "git")


def bare_git_calls(source: str) -> list[int]:
    """Line numbers of subprocess calls that run git (a literal ``["git", ...]`` argv) without an
    ``env=`` built by ``hermetic_env``. A shell string that runs git is out of its sight."""
    hits: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        fn = node.func
        name = fn.attr if isinstance(fn, ast.Attribute) else fn.id if isinstance(fn, ast.Name) else ""
        if name not in _SUBPROCESS_FUNCS:
            continue
        if not any(_is_git_list(a) for a in node.args):
            continue
        env = next((k.value for k in node.keywords if k.arg == "env"), None)
        if env is None or "hermetic_env" not in ast.unparse(env):
            hits.append(node.lineno)
    return hits


def test_no_test_runs_a_bare_subprocess_git_call():
    found = {}
    for path in sorted(TESTS.glob("*.py")):
        if path.name in (HELPER, Path(__file__).name):
            continue
        lines = bare_git_calls(path.read_text(encoding="utf-8"))
        if lines:
            found[path.name] = lines
    assert found == {}, f"use tests._hermetic_git.run_git (or env=hermetic_env(...)): {found}"


def test_the_ratchet_flags_a_bare_call_and_accepts_the_helper():
    assert bare_git_calls('import subprocess\nsubprocess.run(["git", "status"])\n') == [2]
    assert bare_git_calls('import subprocess\nsubprocess.check_output(("git", "log"), env=dict(os.environ))\n') == [2]
    assert bare_git_calls('subprocess.run(["sh", "-c", "git push"], env=None)\n') == []  # not a git argv
    assert bare_git_calls('subprocess.run(["git", "status"], env=hermetic_env(x))\n') == []
    assert bare_git_calls('run_git("status")\n') == []
    assert bare_git_calls('subprocess.run(["git", *a], env=_hermetic_env(dict(os.environ)))\n') == []
