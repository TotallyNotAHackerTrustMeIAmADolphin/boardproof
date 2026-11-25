"""Issue 234: java is found in the argument, JAVA_PATH, JAVA_HOME, PATH and a .tools folder; the
answer names the source and version; a too-old JRE is refused; "not found" lists the places."""

import sys
from pathlib import Path

import pytest

from boardproof.toolsets import routing as routing_toolset
from boardproof.toolsets.routing import FreeroutingError

WIN = sys.platform == "win32"
EXE = "java.cmd" if WIN else "java"  # the fake scripts; discovery is pointed at this name


def _fake_java(folder: Path, major: str = "25.0.5") -> Path:
    """A script that prints a `java -version` banner on stderr (a .cmd on Windows)."""
    folder.mkdir(parents=True, exist_ok=True)
    if WIN:
        path = folder / "java.cmd"
        path.write_text(f'@echo off\r\necho openjdk version "{major}" 1>&2\r\n')
    else:
        path = folder / "java"
        path.write_text(f'#!/bin/sh\necho \'openjdk version "{major}"\' >&2\n')
        path.chmod(0o755)
    return path


def _exe_in(folder: Path, banner: str | None = None) -> Path:
    """A file named like the java binary that prints no version (unreadable) or `banner`."""
    folder.mkdir(parents=True, exist_ok=True)
    if banner is None:
        (folder / EXE).write_text("x")
    else:
        return _fake_java(folder, banner)
    return folder / EXE


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    """No java from the environment, PATH, the install folders or this checkout's .tools."""
    for var in ("JAVA_PATH", "JAVA_HOME"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(routing_toolset.shutil, "which", lambda _name: None)
    monkeypatch.setattr(routing_toolset, "_JAVA_NAME", EXE)
    monkeypatch.setattr(routing_toolset, "_JAVA_CANDIDATE_GLOBS", {})
    (tmp_path / "project").mkdir()
    monkeypatch.chdir(tmp_path / "project")
    monkeypatch.setattr(routing_toolset.krt_backend, "REPO_ROOT", tmp_path / "checkout")
    return tmp_path


def test_java_path_argument_wins_and_names_its_source(isolated):
    java = _fake_java(isolated / "a")
    assert routing_toolset.locate_java(java) == (java, "java_path argument")
    assert routing_toolset.java_major_version(java) == 25


def test_java_path_argument_must_exist(isolated):
    with pytest.raises(FreeroutingError, match="not an existing file"):
        routing_toolset.locate_java(isolated / "missing" / EXE)


def test_too_old_jre_in_java_path_is_refused_with_the_reason(isolated):
    java = _fake_java(isolated / "old", "21.0.4")
    with pytest.raises(FreeroutingError, match=r"Java 21.*needs Java 25 or newer.*JRE 21"):
        routing_toolset.locate_java(java)


def test_old_style_version_string_is_read(isolated):
    assert routing_toolset.java_major_version(_fake_java(isolated / "o", "1.8.0_312")) == 8


def test_java_path_that_prints_no_version_is_refused(isolated):
    notjava = isolated / "x.txt"
    notjava.write_text("hello")
    with pytest.raises(FreeroutingError, match="not a java executable"):
        routing_toolset.locate_java(notjava)


def test_java_path_env_var(isolated, monkeypatch):
    java = _fake_java(isolated / "e")
    monkeypatch.setenv("JAVA_PATH", str(java))
    assert routing_toolset.locate_java() == (java, "JAVA_PATH")


def test_java_home(isolated, monkeypatch):
    home = isolated / "jdk"
    (home / "bin").mkdir(parents=True)
    _fake_java(home / "bin")
    monkeypatch.setenv("JAVA_HOME", str(home))
    assert routing_toolset.locate_java() == (home / "bin" / EXE, "JAVA_HOME")


def test_project_tools_folder(isolated):
    java = _exe_in(isolated / "project" / ".tools" / "temurin-25" / "bin", "25.0.5")
    path, source = routing_toolset.locate_java()
    assert path.resolve() == java.resolve()
    assert ".tools" in source


def test_checkout_tools_folder(isolated):
    java = _exe_in(isolated / "checkout" / ".tools" / "temurin-25" / "bin", "25.0.5")
    assert routing_toolset.locate_java()[0].resolve() == java.resolve()


def test_path_source(isolated, monkeypatch):
    java = _fake_java(isolated / "onpath")
    monkeypatch.setattr(routing_toolset.shutil, "which", lambda _name: str(java))
    assert routing_toolset.locate_java() == (java, "PATH")


def test_too_old_java_on_path_does_not_win_over_tools_folder(isolated, monkeypatch):
    old = _fake_java(isolated / "old", "21.0.4")
    monkeypatch.setattr(routing_toolset.shutil, "which", lambda _name: str(old))
    new = _exe_in(isolated / "project" / ".tools" / "t25" / "bin", "25.0.5")
    assert routing_toolset.locate_java()[0].resolve() == new.resolve()


def test_only_too_old_candidates_are_named_with_their_versions(isolated, monkeypatch):
    old = _fake_java(isolated / "old", "21.0.4")
    monkeypatch.setenv("JAVA_PATH", str(old))
    with pytest.raises(FreeroutingError, match=r"Java 21"):
        routing_toolset.locate_java()


def test_version_probe_rejects_a_nonzero_exit(isolated):
    bad = isolated / "bad"
    bad.mkdir()
    banner = 'openjdk version "25.0.1"'
    if WIN:
        script = bad / "java.cmd"
        script.write_text(f"@echo off\r\necho {banner} 1>&2\r\nexit /b 3\r\n")
    else:
        script = bad / "java"
        script.write_text(f"#!/bin/sh\necho '{banner}' >&2\nexit 3\n")
        script.chmod(0o755)
    assert routing_toolset.java_major_version(script) is None


def test_nothing_found_lists_the_places_searched(isolated, monkeypatch):
    monkeypatch.setenv("JAVA_HOME", str(isolated / "nowhere"))
    with pytest.raises(FreeroutingError) as err:
        routing_toolset.locate_java()
    text = str(err.value)
    for place in ("JAVA_PATH", "JAVA_HOME", "PATH", str(isolated / "project" / ".tools"), "java_path"):
        assert place in text


def test_find_java_records_source_for_the_report(isolated):
    java = _fake_java(isolated / "r")
    assert routing_toolset.find_java(java) == java
    assert routing_toolset.java_report(java) == {"path": str(java), "source": "java_path argument", "major_version": 25}


def test_unreadable_java_on_path_does_not_win_over_tools_folder(isolated, monkeypatch):
    broken = _exe_in(isolated / "broken")
    monkeypatch.setattr(routing_toolset.shutil, "which", lambda _name: str(broken))
    good = _exe_in(isolated / "project" / ".tools" / "t25" / "bin", "25.0.5")
    assert routing_toolset.locate_java()[0].resolve() == good.resolve()


def test_only_unreadable_candidates_are_named_in_the_error(isolated, monkeypatch):
    broken = _exe_in(isolated / "broken")
    monkeypatch.setattr(routing_toolset.shutil, "which", lambda _name: str(broken))
    with pytest.raises(FreeroutingError, match=r"found but unusable") as err:
        routing_toolset.locate_java()
    assert str(broken) in str(err.value)


def test_java_path_env_var_is_the_users_choice_even_if_its_version_is_unreadable(isolated, monkeypatch):
    odd = _exe_in(isolated / "odd")
    monkeypatch.setenv("JAVA_PATH", str(odd))
    assert routing_toolset.locate_java() == (odd, "JAVA_PATH")


def test_version_probe_is_confined_to_a_sandbox_without_perf_data(monkeypatch):
    """HotSpot would create hsperfdata_<user> in the OS temp folder (build ticket 33)."""
    import subprocess

    seen = {}

    def fake_run(args, **kw):
        seen["args"], seen["cwd"] = list(args), Path(kw["cwd"])
        seen["cwd_existed"] = seen["cwd"].is_dir()
        return subprocess.CompletedProcess(args, 0, b"", b'openjdk version "25.0.5"')

    monkeypatch.setattr(routing_toolset.subprocess, "run", fake_run)
    assert routing_toolset.java_major_version(Path("java")) == 25
    assert seen["args"][1] == "-XX:-UsePerfData"
    assert f"-Djava.io.tmpdir={seen['cwd']}" in seen["args"]
    assert seen["args"][-1] == "-version" and seen["cwd_existed"] and not seen["cwd"].exists()
