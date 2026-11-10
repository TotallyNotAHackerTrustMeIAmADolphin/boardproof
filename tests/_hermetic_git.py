"""One hermetic git environment for every test that runs git (issue 174, follow-up of 157).

A user's global git settings (``core.excludesFile``, ``core.autocrlf``, an identity, a ``safe.directory``
list, a ``~/.config/git/ignore``) must not decide a test verdict. ``hermetic_env`` cuts off the user's
global and system configuration and puts a fixed global file of its own in its place:

- ``core.excludesFile`` is the null device, ``core.autocrlf`` is false, ``init.defaultBranch`` is main;
- ``safe.directory = *`` (git honours that key only from a global or system file, never from ``-c`` or
  the repository), so read-only calls on this checkout work whoever owns it;
- no commit identity: a test that commits passes ``identity=True`` (fixed name and address through the
  environment) or sets its own ``-c user.*`` or ``git config``, which wins over nothing it should not.

Every test that runs git goes through ``run_git`` or passes ``hermetic_env()`` as ``env=``;
``tests/test_hermetic_git.py`` holds the ratchet against a bare subprocess git call.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

_IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}
_CONFIG_TEXT = (
    "[core]\n\texcludesFile = {null}\n\tautocrlf = false\n\tfilemode = true\n"
    "[safe]\n\tdirectory = *\n[init]\n\tdefaultBranch = main\n[commit]\n\tgpgsign = false\n"
)
_config_path: Path | None = None


def _global_config() -> Path:
    """The fixed global config file, written once per process into a fresh temp directory."""
    global _config_path
    if _config_path is None or not _config_path.is_file():
        folder = Path(tempfile.mkdtemp(prefix="hermetic-git-"))
        path = folder / "gitconfig"
        path.write_text(_CONFIG_TEXT.format(null=os.devnull), encoding="utf-8", newline="\n")
        _config_path = path
    return _config_path


def hermetic_env(base: Mapping[str, str] | None = None, *, identity: bool = False) -> dict[str, str]:
    """``base`` (default: the process environment) with every user-level git setting replaced."""
    env = dict(os.environ if base is None else base)
    for key in [k for k in env if k.startswith("GIT_CONFIG_KEY_") or k.startswith("GIT_CONFIG_VALUE_")]:
        del env[key]
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "EMAIL"):
        env.pop(key, None)
    env.update(
        GIT_CONFIG_GLOBAL=str(_global_config()),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        XDG_CONFIG_HOME=str(_global_config().parent / "xdg"),
        GIT_CONFIG_COUNT="0",
    )
    if identity:
        env.update(_IDENTITY)
    return env


def run_git(
    *args: str,
    cwd: str | os.PathLike[str] | None = None,
    check: bool = True,
    identity: bool = False,
    env: Mapping[str, str] | None = None,
    extra_env: Mapping[str, str] | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """``git *args`` under ``hermetic_env`` (``env``, if given, is the base it isolates; ``extra_env``
    is added after the isolation, for a test that sets an identity variable on purpose). Captures
    output and has a 60 s timeout unless ``kwargs`` say otherwise."""
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("timeout", 60)
    return subprocess.run(
        ["git", *args], cwd=cwd, check=check, env={**hermetic_env(env, identity=identity), **(extra_env or {})}, **kwargs
    )


def apply_hermetic_env(monkeypatch, *, identity: bool = False) -> None:
    """Make ``os.environ`` itself hermetic for the test (``monkeypatch`` restores it), for code under
    test that runs git with the inherited environment (``scripts/batch_gate.py``)."""
    want = hermetic_env(identity=identity)
    for key in list(os.environ):
        if key not in want and (key in _IDENTITY or key == "EMAIL" or key.startswith("GIT_CONFIG_KEY_")
                                or key.startswith("GIT_CONFIG_VALUE_")):
            monkeypatch.delenv(key)
    for key, value in want.items():
        monkeypatch.setenv(key, value)
