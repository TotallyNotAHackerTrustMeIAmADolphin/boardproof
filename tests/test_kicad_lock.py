"""Build ticket 41: refuse to write a file KiCad has open.

The naming rule and the lock contents were recorded on this machine against
KiCad 10.0.7 (ticket 41's ``## Answer``) and checked in KiCad's source
(``include/lockfile.h``, ``common/wildcards_and_files_ext.cpp``): the lock of
``<dir>/<name>.<ext>`` is ``<dir>/~<name>.<ext>.lck`` and holds
``{"hostname":"<host>","username":"<user>"}`` with no newline and no process id.

Every lock file here is synthetic: written by the test in ``tmp_path`` with
exactly that name and byte layout. No KiCad file is written; the targets are
empty placeholders, because the guard reads only names and lock files.
"""
from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from kicad_mcp import kicad_lock

ME_USER = "Test User"
ME_HOST = "TESTHOST-01"


def kicad_lock_bytes(username: str, hostname: str) -> bytes:
    """Exactly what KiCad 10.0.7 writes: nlohmann::json::dump(), keys sorted."""
    return json.dumps(
        {"hostname": hostname, "username": username}, separators=(",", ":")
    ).encode("utf-8")


def write_lock(target: Path, username: str = ME_USER, hostname: str = ME_HOST) -> Path:
    lock = target.with_name("~" + target.name + ".lck")
    lock.write_bytes(kicad_lock_bytes(username, hostname))
    return lock


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@pytest.fixture
def me(monkeypatch):
    """Pin who "we" are, so the stale report does not depend on the machine."""
    monkeypatch.setattr(kicad_lock, "current_user", lambda: ME_USER)
    monkeypatch.setattr(kicad_lock, "current_host", lambda: ME_HOST.lower())


def no_kicad() -> list[str]:
    return []


def kicad_up() -> list[str]:
    return ["pcbnew.exe"]


def cannot_tell() -> None:
    return None


# --- the naming rule -------------------------------------------------------


@pytest.mark.parametrize(
    "name, lock",
    [
        ("pic_programmer.kicad_pcb", "~pic_programmer.kicad_pcb.lck"),
        ("pic_programmer.kicad_sch", "~pic_programmer.kicad_sch.lck"),
        ("pic_programmer.kicad_pro", "~pic_programmer.kicad_pro.lck"),
        ("my.board.v2.kicad_pcb", "~my.board.v2.kicad_pcb.lck"),
        ("Ω board.kicad_pcb", "~Ω board.kicad_pcb.lck"),
    ],
)
def test_lock_file_name_follows_the_recorded_rule(tmp_path, name, lock):
    assert kicad_lock.lock_file_for(tmp_path / name) == tmp_path / lock


def test_lock_file_sits_in_the_target_directory_for_a_relative_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert kicad_lock.lock_file_for("b.kicad_pcb") == tmp_path / "~b.kicad_pcb.lck"


# --- which locks guard which target ---------------------------------------


def test_board_is_guarded_by_its_own_lock_only(tmp_path):
    board = touch(tmp_path / "p.kicad_pcb")
    touch(tmp_path / "p.kicad_pro")
    assert kicad_lock.guarding_lock_files([board]) == {
        str(board): [str(tmp_path / "~p.kicad_pcb.lck")]
    }


def test_project_file_is_guarded_by_the_project_lock(tmp_path):
    pro = touch(tmp_path / "p.kicad_pro")
    assert kicad_lock.guarding_lock_files([pro])[str(pro)] == [str(tmp_path / "~p.kicad_pro.lck")]


def test_root_schematic_is_guarded_once(tmp_path):
    touch(tmp_path / "p.kicad_pro")
    root = touch(tmp_path / "p.kicad_sch")
    assert kicad_lock.guarding_lock_files([root])[str(root)] == [str(tmp_path / "~p.kicad_sch.lck")]


def test_sub_sheet_is_also_guarded_by_the_root_schematic_lock(tmp_path):
    """KiCad 10.0.7 locks only the root sheet (run B in the ticket)."""
    touch(tmp_path / "p.kicad_pro")
    sub = touch(tmp_path / "pic_sockets.kicad_sch")
    assert kicad_lock.guarding_lock_files([sub])[str(sub)] == [
        str(tmp_path / "~pic_sockets.kicad_sch.lck"),
        str(tmp_path / "~p.kicad_sch.lck"),
    ]


def test_sub_sheet_in_a_subdirectory_finds_the_project_above(tmp_path):
    touch(tmp_path / "p.kicad_pro")
    sub = touch(tmp_path / "sheets" / "power.kicad_sch")
    assert str(tmp_path / "~p.kicad_sch.lck") in kicad_lock.guarding_lock_files([sub])[str(sub)]


@pytest.mark.parametrize("name", ["p.kicad_prl", "fp-lib-table", "sym-lib-table", "design-block-lib-table"])
def test_files_kicad_rewrites_with_the_project_are_guarded_by_the_project_lock(tmp_path, name):
    touch(tmp_path / "p.kicad_pro")
    target = touch(tmp_path / name)
    assert kicad_lock.guarding_lock_files([target])[str(target)] == [str(tmp_path / "~p.kicad_pro.lck")]


def test_design_rules_are_guarded_by_board_and_project_lock(tmp_path):
    dru = touch(tmp_path / "p.kicad_dru")
    assert kicad_lock.guarding_lock_files([dru])[str(dru)] == [
        str(tmp_path / "~p.kicad_pcb.lck"),
        str(tmp_path / "~p.kicad_pro.lck"),
    ]


def test_a_new_file_that_does_not_exist_yet_is_still_mapped(tmp_path):
    board = tmp_path / "new.kicad_pcb"
    assert kicad_lock.guarding_lock_files([board])[str(board)] == [str(tmp_path / "~new.kicad_pcb.lck")]


# --- the refusal ----------------------------------------------------------


def test_no_lock_means_writable(tmp_path, me):
    board = touch(tmp_path / "p.kicad_pcb")
    assert kicad_lock.check_writable([board], kicad_running=no_kicad) is None


def test_open_board_is_refused_naming_the_file_and_its_lock(tmp_path, me):
    board = touch(tmp_path / "p.kicad_pcb")
    lock = write_lock(board)
    refusal = kicad_lock.check_writable([board], kicad_running=kicad_up)
    assert refusal is not None
    assert refusal["ok"] is False
    assert refusal["code"] == "file_open_in_kicad"
    assert refusal["file"] == str(board)
    assert refusal["lock_file"] == str(lock)
    assert [(entry["file"], entry["lock_file"]) for entry in refusal["locks"]] == [(str(board), str(lock))]
    assert str(board) in refusal["message"] and str(lock) in refusal["message"]


def test_a_single_path_is_accepted_as_well_as_a_list(tmp_path, me):
    board = touch(tmp_path / "p.kicad_pcb")
    write_lock(board)
    assert kicad_lock.check_writable(board, kicad_running=kicad_up)["code"] == "file_open_in_kicad"


def test_sub_sheet_is_refused_while_the_root_is_open(tmp_path, me):
    touch(tmp_path / "p.kicad_pro")
    root = touch(tmp_path / "p.kicad_sch")
    sub = touch(tmp_path / "pic_sockets.kicad_sch")
    root_lock = write_lock(root)
    refusal = kicad_lock.check_writable([sub], kicad_running=kicad_up)
    assert refusal["file"] == str(sub)
    assert refusal["lock_file"] == str(root_lock)


def test_project_lock_alone_does_not_refuse_a_board_write(tmp_path, me):
    """Run C: the project manager alone holds only the project lock and never
    rewrites the board, so a board write stays allowed."""
    pro = touch(tmp_path / "p.kicad_pro")
    board = touch(tmp_path / "p.kicad_pcb")
    write_lock(pro)
    assert kicad_lock.check_writable([board], kicad_running=kicad_up) is None
    assert kicad_lock.check_writable([pro], kicad_running=kicad_up)["file"] == str(pro)


def test_every_locked_target_is_listed(tmp_path, me):
    pro = touch(tmp_path / "p.kicad_pro")
    board = touch(tmp_path / "p.kicad_pcb")
    sch = touch(tmp_path / "p.kicad_sch")
    write_lock(board)
    write_lock(pro)
    refusal = kicad_lock.check_writable([sch, board, pro], kicad_running=kicad_up)
    assert [entry["file"] for entry in refusal["locks"]] == [str(board), str(pro)]
    assert refusal["file"] == str(board)


def test_a_directory_target_is_refused_when_any_lock_lies_inside(tmp_path, me):
    revision = tmp_path / "rev"
    board = touch(revision / "p.kicad_pcb")
    lock = write_lock(board)
    refusal = kicad_lock.check_writable([revision], kicad_running=kicad_up)
    assert refusal["file"] == str(board)
    assert refusal["lock_file"] == str(lock)


def test_a_directory_without_locks_is_writable(tmp_path, me):
    touch(tmp_path / "rev" / "p.kicad_pcb")
    assert kicad_lock.check_writable([tmp_path / "rev"], kicad_running=kicad_up) is None


def test_a_directory_at_the_lock_path_counts_as_a_lock(tmp_path, me):
    """Review round 1, finding 6: anything at the lock path (a directory, a
    broken link) means "do not write"; its owner is unknown."""
    board = touch(tmp_path / "p.kicad_pcb")
    (tmp_path / "~p.kicad_pcb.lck").mkdir()
    refusal = kicad_lock.check_writable([board], kicad_running=no_kicad)
    assert refusal["lock_file"] == str(tmp_path / "~p.kicad_pcb.lck")
    assert refusal["locks"][0]["owner"] is None
    assert refusal["locks"][0]["stale"] is None


def test_a_broken_link_at_the_lock_path_counts_as_a_lock(tmp_path, me):
    board = touch(tmp_path / "p.kicad_pcb")
    link = tmp_path / "~p.kicad_pcb.lck"
    try:
        os.symlink(tmp_path / "gone", link)
    except (OSError, NotImplementedError) as exc:
        if os.name != "nt":
            pytest.skip(f"cannot create a symbolic link here: {exc}")
        # No symlink right on Windows: a directory junction needs none. Point
        # it at a directory, then remove that directory to break it.
        import _winapi

        (tmp_path / "gone").mkdir()
        _winapi.CreateJunction(str(tmp_path / "gone"), str(link))
        (tmp_path / "gone").rmdir()
    assert not link.exists() and os.path.lexists(link)
    assert kicad_lock.check_writable([board], kicad_running=no_kicad)["lock_file"] == str(link)


def test_a_directory_target_finds_locks_in_subdirectories(tmp_path, me):
    revision = tmp_path / "rev"
    board = touch(revision / "sub" / "x.kicad_pcb")
    lock = write_lock(board)
    refusal = kicad_lock.check_writable([revision], kicad_running=kicad_up)
    assert (refusal["file"], refusal["lock_file"]) == (str(board), str(lock))


def test_a_directory_target_counts_a_lock_shaped_directory_inside_it(tmp_path, me):
    revision = tmp_path / "rev"
    touch(revision / "p.kicad_pcb")
    (revision / "~p.kicad_pcb.lck").mkdir()
    assert kicad_lock.check_writable([revision], kicad_running=no_kicad) is not None


def test_upper_case_suffix_is_still_a_schematic(tmp_path, me):
    touch(tmp_path / "p.kicad_pro")
    root_lock = write_lock(touch(tmp_path / "p.kicad_sch"))
    sub = touch(tmp_path / "SUB.KICAD_SCH")
    assert kicad_lock.check_writable([sub], kicad_running=kicad_up)["lock_file"] == str(root_lock)


@pytest.mark.parametrize("name", ["FP-LIB-TABLE", "Sym-Lib-Table"])
def test_side_file_names_compare_case_insensitively(tmp_path, me, name):
    pro = touch(tmp_path / "p.kicad_pro")
    lock = write_lock(pro)
    target = tmp_path / name
    assert kicad_lock.guarding_lock_files([target])[str(target)] == [str(lock)]
    assert kicad_lock.check_writable([target], kicad_running=kicad_up)["lock_file"] == str(lock)


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names exist only on Windows")
def test_a_short_8_3_name_is_guarded_like_the_long_name(tmp_path, me):
    import ctypes

    board = touch(tmp_path / "longboardname.kicad_pcb")
    lock = write_lock(board)
    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(board), buffer, 1024)
    short = buffer.value
    if not short or Path(short).name.lower() == board.name.lower():
        pytest.skip(f"no 8.3 short name on this volume ({short!r})")
    refusal = kicad_lock.check_writable([short], kicad_running=kicad_up)
    assert refusal is not None
    assert Path(refusal["lock_file"]).name.lower() == lock.name.lower()


def test_sub_sheet_of_a_nested_project_is_guarded_by_every_enclosing_root(tmp_path, me):
    """Review round 1, finding 2: top/ is open in eeschema and uses
    top/sub/psu.kicad_sch; top/sub/ holds its own project, which must not hide
    top's root lock."""
    touch(tmp_path / "top.kicad_pro")
    top_lock = write_lock(touch(tmp_path / "top.kicad_sch"))
    touch(tmp_path / "sub" / "sub.kicad_pro")
    psu = touch(tmp_path / "sub" / "psu.kicad_sch")
    sub_root = touch(tmp_path / "sub" / "sub.kicad_sch")
    assert str(top_lock) in kicad_lock.guarding_lock_files([psu])[str(psu)]
    assert kicad_lock.check_writable([psu], kicad_running=kicad_up)["lock_file"] == str(top_lock)
    assert kicad_lock.check_writable([sub_root], kicad_running=kicad_up)["lock_file"] == str(top_lock)


def test_nested_roots_are_listed_nearest_first(tmp_path):
    touch(tmp_path / "top.kicad_pro")
    touch(tmp_path / "sub" / "sub.kicad_pro")
    psu = tmp_path / "sub" / "psu.kicad_sch"
    assert kicad_lock.guarding_lock_files([psu])[str(psu)] == [
        str(tmp_path / "sub" / "~psu.kicad_sch.lck"),
        str(tmp_path / "sub" / "~sub.kicad_sch.lck"),
        str(tmp_path / "~top.kicad_sch.lck"),
    ]


def test_a_project_three_levels_up_still_guards_the_sheet(tmp_path, me):
    """Review round 2, N3: no cap on how far up the enclosing projects are."""
    touch(tmp_path / "top.kicad_pro")
    top_lock = write_lock(touch(tmp_path / "top.kicad_sch"))
    sheet = touch(tmp_path / "a" / "b" / "c" / "s.kicad_sch")
    assert kicad_lock.check_writable([sheet], kicad_running=kicad_up)["lock_file"] == str(top_lock)


def short_name(path: Path) -> str:
    import ctypes

    buffer = ctypes.create_unicode_buffer(1024)
    ctypes.windll.kernel32.GetShortPathNameW(str(path), buffer, 1024)
    return buffer.value


@pytest.mark.skipif(os.name != "nt", reason="8.3 short names exist only on Windows")
def test_one_lock_reached_by_two_spellings_is_reported_once(tmp_path, me):
    """Review round 2, finding 2: a short (8.3) spelling of the directory and
    the resolved long one name the same lock; it is one lock, not two."""
    folder = tmp_path / "a long folder name"
    board = touch(folder / "p.kicad_pcb")
    lock = write_lock(board)
    short_folder = short_name(folder)
    if not short_folder or Path(short_folder).name.lower() == folder.name.lower():
        pytest.skip(f"no 8.3 short name on this volume ({short_folder!r})")
    short_board = Path(short_folder) / "p.kicad_pcb"
    refusal = kicad_lock.check_writable([short_board], kicad_running=kicad_up)
    assert len(refusal["locks"]) == 1
    assert "lock files guard" not in refusal["message"]
    both = kicad_lock.check_writable([short_board, board], kicad_running=kicad_up)
    assert len(both["locks"]) == 1
    assert os.path.samefile(both["lock_file"], lock)


def test_a_directory_target_finds_a_lock_with_an_upper_case_extension(tmp_path, me):
    revision = tmp_path / "rev"
    touch(revision / "p.kicad_pcb")
    (revision / "~p.kicad_pcb.LCK").write_bytes(kicad_lock_bytes(ME_USER, ME_HOST))
    assert kicad_lock.check_writable([revision], kicad_running=kicad_up) is not None


def _break_inside(monkeypatch, owner, name, at: str):
    real = getattr(owner, name)

    def broken(*args, **kwargs):
        if str(args[0]).endswith(at):
            raise RuntimeError(f"unexpected failure at {at}")
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, broken)


def test_an_unexpected_error_listing_a_directory_propagates(tmp_path, me, monkeypatch):
    """Fail closed: only OSError (an unreadable folder) is skipped in the walk;
    anything else stops the check instead of silently allowing the write."""
    touch(tmp_path / "rev" / "sub" / "p.kicad_pcb")
    _break_inside(monkeypatch, Path, "iterdir", "sub")
    with pytest.raises(RuntimeError):
        kicad_lock.check_writable([tmp_path / "rev"], kicad_running=kicad_up)


def test_an_unexpected_error_resolving_a_directory_propagates(tmp_path, me, monkeypatch):
    touch(tmp_path / "rev" / "sub" / "p.kicad_pcb")
    _break_inside(monkeypatch, os.path, "realpath", "sub")
    with pytest.raises(RuntimeError):
        kicad_lock.check_writable([tmp_path / "rev"], kicad_running=kicad_up)


def test_a_deep_directory_target_is_walked_to_its_bottom(tmp_path, me):
    revision = tmp_path / "rev"
    deep = revision.joinpath(*[f"d{i}" for i in range(12)])
    lock = write_lock(touch(deep / "x.kicad_pcb"))
    assert kicad_lock.check_writable([revision], kicad_running=kicad_up)["lock_file"] == str(lock)


@pytest.mark.skipif(os.name != "nt", reason="directory junctions exist only on Windows")
def test_a_junction_loop_inside_a_directory_target_is_walked_once(tmp_path, me):
    """Review round 2, finding 4: rglob followed a junction to its own parent
    and reported rev/loop/loop/.../~p.kicad_pcb.lck."""
    import _winapi

    revision = tmp_path / "rev"
    board = touch(revision / "p.kicad_pcb")
    lock = write_lock(board)
    _winapi.CreateJunction(str(revision), str(revision / "loop"))
    try:
        refusal = kicad_lock.check_writable([revision], kicad_running=kicad_up)
        assert [entry["lock_file"] for entry in refusal["locks"]] == [str(lock)]
    finally:
        os.rmdir(revision / "loop")


# --- never deletes --------------------------------------------------------

LOCK_CONTENTS = {
    "valid_mine": kicad_lock_bytes(ME_USER, ME_HOST),
    "other_host": kicad_lock_bytes(ME_USER, "OTHER-PC"),
    "other_user": kicad_lock_bytes("Someone Else", ME_HOST),
    "empty": b"",
    "unparseable": b"{not json",
}


def snapshot(root: Path) -> dict:
    """Every entry under ``root`` with its bytes and mtime (directories too)."""
    state = {}
    for entry in sorted(root.rglob("*")):
        st = os.lstat(entry)
        data = entry.read_bytes() if entry.is_file() else None
        state[str(entry.relative_to(root))] = (data, st.st_mtime_ns, entry.is_dir())
    return state


DESTRUCTIVE = [
    (os, "unlink"), (os, "remove"), (os, "rmdir"), (os, "removedirs"), (os, "rename"),
    (os, "renames"), (os, "replace"), (os, "truncate"), (os, "chmod"), (os, "utime"),
    (os, "open"), (os, "mkdir"), (os, "makedirs"), (os, "symlink"),
    (Path, "unlink"), (Path, "rmdir"), (Path, "rename"), (Path, "replace"),
    (Path, "write_text"), (Path, "write_bytes"), (Path, "touch"), (Path, "chmod"),
    (Path, "mkdir"),
]


@pytest.fixture
def no_destruction(monkeypatch):
    """Record (and refuse) every destructive call made while the guard runs."""
    import builtins
    import shutil

    calls: list[str] = []

    def refuse(name):
        def _refuse(*args, **kwargs):
            calls.append(name)
            raise AssertionError(f"kicad_lock called {name}")
        return _refuse

    def arm():
        for owner, name in DESTRUCTIVE:
            monkeypatch.setattr(owner, name, refuse(f"{getattr(owner, '__name__', owner)}.{name}"))
        for name in ("rmtree", "move", "copy", "copy2", "copyfile", "copytree"):
            monkeypatch.setattr(shutil, name, refuse(f"shutil.{name}"))
        for owner, name in ((Path, "open"), (builtins, "open")):
            monkeypatch.setattr(owner, name, read_only(getattr(owner, name), f"{owner.__name__}.open"))

    def read_only(real, name):
        def _open(*args, **kwargs):
            mode = kwargs.get("mode", args[1] if len(args) > 1 else "r")
            if set(mode) - set("rbt"):
                calls.append(f"{name}({mode!r})")
                raise AssertionError(f"kicad_lock opened a file with mode {mode!r}")
            return real(*args, **kwargs)
        return _open

    return arm, calls


@pytest.mark.parametrize("content_name", sorted(LOCK_CONTENTS))
@pytest.mark.parametrize("target_kind", ["file", "directory"])
@pytest.mark.parametrize("running", [no_kicad, kicad_up, cannot_tell], ids=["no_kicad", "kicad_up", "cannot_tell"])
def test_no_entry_point_changes_anything_on_disk(tmp_path, me, no_destruction, content_name, target_kind, running):
    """Review round 1, finding 1: bytes, mtimes and the directory listing stay
    exactly as they were, for every lock content, file and directory targets,
    through find_locks, check_writable and refusal; and no destructive call is
    even attempted."""
    project = tmp_path / "proj"
    board = touch(project / "p.kicad_pcb")
    touch(project / "p.kicad_pro")
    lock = project / "~p.kicad_pcb.lck"
    lock.write_bytes(LOCK_CONTENTS[content_name])
    target = board if target_kind == "file" else project
    before = snapshot(tmp_path)
    arm, calls = no_destruction
    arm()
    locks = kicad_lock.find_locks([target], kicad_running=running)
    found = kicad_lock.check_writable([target], kicad_running=running)
    # refusal() fed the worst case too: every lock marked stale and unreadable.
    worst = [dict(entry, stale=True, owner_readable=False, owner=None) for entry in locks]
    kicad_lock.refusal(found["file"], found["lock_file"], worst)
    kicad_lock.refusal(found["file"], found["lock_file"], locks)
    kicad_lock.refusal(board, lock)
    assert calls == []
    assert snapshot(tmp_path) == before
    assert [entry["lock_file"] for entry in locks] == [str(lock)]


ALLOWED_IMPORTS = {"__future__", "csv", "getpass", "io", "json", "os", "re", "socket", "subprocess", "pathlib", "typing"}
ALLOWED_FROM = {"__future__": {"annotations"}, "pathlib": {"Path"}, "typing": {"Any", "Callable", "Iterable"}}
FORBIDDEN_NAMES = {
    # removing, renaming, writing, creating, changing files
    "unlink", "remove", "removedirs", "rmdir", "rmtree", "rename", "renames", "replace",
    "write_text", "write_bytes", "write", "writelines", "truncate", "ftruncate", "move",
    "copy", "copyfile", "copy2", "copytree", "copymode", "copystat", "touch", "chmod",
    "lchmod", "chown", "lchown", "utime", "open", "fdopen", "mkdir", "makedirs",
    "symlink", "symlink_to", "link", "hardlink_to", "link_to", "mkfifo", "mknod",
    # reaching any of those by a string or a side door
    "getattr", "setattr", "delattr", "vars", "globals", "locals", "eval", "exec",
    "compile", "__import__", "importlib", "import_module", "ctypes", "builtins",
    "__builtins__", "__dict__", "__getattribute__", "attrgetter", "methodcaller",
    "operator", "shutil", "tempfile",
    # starting other programs
    "system", "popen", "Popen", "startfile", "call", "check_call", "check_output",
    "getoutput", "getstatusoutput", "posix_spawn", "posix_spawnp",
}
FORBIDDEN_PREFIXES = ("spawn", "exec")
# Review round 2 (finding 1c): an allowlist per module, not only a denylist.
# Every attribute read from an imported module must be listed here; anything
# else (io.FileIO, os.scandir, socket.socket, ...) is a violation.
MODULE_ATTRIBUTES = {
    "csv": {"reader"},
    "getpass": {"getuser"},
    "io": {"StringIO"},
    "json": {"loads"},
    "os": {"path", "environ", "name", "PathLike", "fspath"},
    "os.path": {"abspath", "realpath", "lexists", "normcase"},
    "re": {"split"},
    "socket": {"gethostname"},
    "subprocess": {"run", "SubprocessError"},
}
SUBPROCESS_RUN_KEYWORDS = {"capture_output", "text", "errors", "timeout", "creationflags"}
SHELL_METACHARACTERS = set("&|;<>^`$%\n\r")
ALLOWED_PROGRAMS = {"tasklist", "ps"}


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return None if head is None else f"{head}.{node.attr}"
    return None


def module_violations(source: str) -> list[str]:
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    hits = []

    def bad(name: str) -> bool:
        return name in FORBIDDEN_NAMES or name.startswith(FORBIDDEN_PREFIXES)

    for node in ast.walk(tree):
        where = f"line {getattr(node, 'lineno', '?')}"
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name not in ALLOWED_IMPORTS or alias.asname is not None:
                    hits.append(f"{where}: import {alias.name} as {alias.asname}")
        elif isinstance(node, ast.ImportFrom):
            allowed = ALLOWED_FROM.get(node.module or "", set())
            for alias in node.names:
                if alias.name not in allowed or bad(alias.name) or (alias.asname and bad(alias.asname)):
                    hits.append(f"{where}: from {node.module} import {alias.name} as {alias.asname}")
        elif isinstance(node, ast.Name) and bad(node.id):
            hits.append(f"{where}: name {node.id}")
        elif isinstance(node, ast.Attribute):
            if bad(node.attr):
                hits.append(f"{where}: attribute .{node.attr}")
            if node.attr.startswith("__"):  # round 3, N6: no introspection (__globals__, __subclasses__, ...)
                hits.append(f"{where}: dunder attribute .{node.attr}")
            owner = _dotted(node.value)
            if owner in MODULE_ATTRIBUTES and node.attr not in MODULE_ATTRIBUTES[owner]:
                hits.append(f"{where}: {owner}.{node.attr} is not on the module allowlist")
            if owner == "subprocess" and node.attr == "run":
                call = parents.get(node)
                if not (isinstance(call, ast.Call) and call.func is node):
                    hits.append(f"{where}: subprocess.run referenced without being called")
                    continue
                argv = call.args[0] if len(call.args) == 1 else None
                if not (
                    isinstance(argv, ast.List)
                    and argv.elts
                    and all(isinstance(e, ast.Constant) and isinstance(e.value, str) for e in argv.elts)
                    and argv.elts[0].value in ALLOWED_PROGRAMS
                    and not any(SHELL_METACHARACTERS & set(e.value) for e in argv.elts)
                ):
                    hits.append(f"{where}: subprocess.run needs one literal tasklist/ps argv without shell metacharacters")
                for keyword in call.keywords:
                    if keyword.arg is None:
                        hits.append(f"{where}: subprocess.run with ** unpacking")
                    elif keyword.arg not in SUBPROCESS_RUN_KEYWORDS:
                        hits.append(f"{where}: subprocess.run keyword {keyword.arg}=")
        if isinstance(node, ast.Name) and node.id in {"subprocess", "os", "shutil", "io", "socket", "csv", "json", "re", "getpass"}:
            if not isinstance(parents.get(node), ast.Attribute):
                hits.append(f"{where}: bare reference to module {node.id}")
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and bad(node.value):
            hits.append(f"{where}: string constant {node.value!r}")
    return hits


def test_the_module_has_no_way_to_remove_or_write_a_file():
    source = Path(kicad_lock.__file__).read_text(encoding="utf-8")
    assert module_violations(source) == []


@pytest.mark.parametrize(
    "snippet",
    [
        'getattr(Path(x), "un" + "link")()',
        "from os import remove as _rm\n_rm(x)",
        "list(map(os.unlink, [x]))",
        "import shutil\nshutil.rmtree(x)",
        "import os as o\no.remove(x)",
        "import importlib\nimportlib.import_module('os')",
        "f = os.remove",
        "vars(os)['unlink'](x)",
        "os.system('del x')",
        "os.spawnl(0, 'x')",
        "os.execv('x', [])",
        "subprocess.run(['cmd', '/c', 'del', x])",
        "subprocess.run(argv)",
        "subprocess.Popen(['tasklist'])",
        "s = subprocess\ns.run(['cmd'])",
        "Path(x).open('w')",
        "eval('os.remove(x)')",
        "__import__('os').remove(x)",
        "import ctypes",
        "o = os\no.remove(x)",
        # Review round 2, N1/N2 and their relatives.
        "subprocess.run(['tasklist', '&', 'del', '/q', '~*.lck'], shell=True, cwd=d, capture_output=True)",
        "subprocess.run(['tasklist'], shell=True)",
        "subprocess.run(['tasklist'], executable='cmd.exe')",
        "subprocess.run(['tasklist'], cwd=d)",
        "subprocess.run(['tasklist'], **opts)",
        "subprocess.run(['tasklist', '/FO', 'CSV'], 'extra')",
        "subprocess.run(['ps', '|', 'sh'])",
        "subprocess.run(['tasklist', '>', 'x.lck'])",
        "run = subprocess.run",
        "io.FileIO(path, 'w').close()",
        "io.open(path, 'wb')",
        "os.scandir(d)",
        "socket.socket()",
        "os.path.join(a, b)",
        "x = io",
        # Review round 3, N6: introspection through dunder attributes.
        "c = [k for k in ().__class__.__base__.__subclasses__() if k.__name__ == '_wrap_close'][0]\n"
        "c.__init__.__globals__['re' + 'name'](a, b)",
        "os.path.realpath.__globals__['re' + 'name'](a, b)",
        "g = os.path.realpath.__globals__\ng['r' + 'emove'](a)",
    ],
)
def test_the_ban_catches_each_known_way_round_it(snippet):
    assert module_violations(snippet) != []


def test_the_ban_accepts_the_process_list_call_the_module_needs():
    ok = (
        "subprocess.run(['tasklist', '/FO', 'CSV', '/NH'], capture_output=True, text=True, "
        "errors='backslashreplace', timeout=15, creationflags=1)"
    )
    assert module_violations(ok) == []


# --- the stale report -----------------------------------------------------


def only_lock(tmp_path: Path, **lock_kwargs) -> tuple[Path, Path]:
    board = touch(tmp_path / "p.kicad_pcb")
    return board, write_lock(board, **lock_kwargs)


def test_own_lock_with_no_kicad_running_is_reported_stale(tmp_path, me):
    board, _ = only_lock(tmp_path)
    [entry] = kicad_lock.find_locks([board], kicad_running=no_kicad)
    assert entry["owner"] == {"username": ME_USER, "hostname": ME_HOST}
    assert entry["owned_by_this_user_and_host"] is True
    assert entry["stale"] is True
    assert "no KiCad" in entry["stale_reason"]


def test_a_stale_lock_still_refuses_and_says_it_is_stale(tmp_path, me):
    board, lock = only_lock(tmp_path)
    refusal = kicad_lock.check_writable([board], kicad_running=no_kicad)
    assert refusal["code"] == "file_open_in_kicad"
    assert refusal["locks"][0]["stale"] is True
    assert "stale" in refusal["message"]
    assert lock.exists()


def test_own_lock_with_kicad_running_is_not_stale(tmp_path, me):
    board, _ = only_lock(tmp_path)
    [entry] = kicad_lock.find_locks([board], kicad_running=kicad_up)
    assert entry["stale"] is False
    assert "pcbnew.exe" in entry["stale_reason"]


def test_lock_of_another_host_has_unknown_liveness(tmp_path, me):
    board, _ = only_lock(tmp_path, hostname="OTHER-PC")
    [entry] = kicad_lock.find_locks([board], kicad_running=no_kicad)
    assert entry["owned_by_this_user_and_host"] is False
    assert entry["stale"] is None
    assert "OTHER-PC" in entry["stale_reason"]


def test_lock_of_another_user_on_this_host_has_unknown_liveness(tmp_path, me):
    board, _ = only_lock(tmp_path, username="Someone Else")
    [entry] = kicad_lock.find_locks([board], kicad_running=no_kicad)
    assert entry["stale"] is None


def test_host_name_compares_case_insensitively_and_without_domain(tmp_path, monkeypatch):
    """wxGetHostName gives the upper-case computer name on Windows, Python the
    lower-case one (recorded in the ticket)."""
    monkeypatch.setattr(kicad_lock, "current_user", lambda: ME_USER)
    monkeypatch.setattr(kicad_lock, "current_host", lambda: "testhost-01.lab.example")
    board, _ = only_lock(tmp_path)
    [entry] = kicad_lock.find_locks([board], kicad_running=no_kicad)
    assert entry["owned_by_this_user_and_host"] is True


@pytest.mark.parametrize(
    "content",
    [b"", b"{not json", b'{"hostname":"X"}', b"[1,2]", b'{"hostname":1,"username":2}', b"\xff\xfe"],
)
def test_unreadable_lock_has_unknown_owner_and_unknown_liveness(tmp_path, me, content):
    """Review round 1, finding 4: an empty or unparseable lock may be a
    cloud-sync placeholder of another machine's lock, so neither its owner nor
    its liveness is known (KiCad itself would reclaim it; we do not guess)."""
    board = touch(tmp_path / "p.kicad_pcb")
    (tmp_path / "~p.kicad_pcb.lck").write_bytes(content)
    for running in (no_kicad, kicad_up):
        [entry] = kicad_lock.find_locks([board], kicad_running=running)
        assert entry["owner"] is None
        assert entry["owner_readable"] is False
        assert entry["owned_by_this_user_and_host"] is None
        assert entry["stale"] is None
    refusal = kicad_lock.check_writable([board], kicad_running=no_kicad)
    assert refusal["code"] == "file_open_in_kicad"
    assert "remove" not in refusal["hint"].lower()


def test_a_deeply_nested_lock_is_unreadable_not_a_crash(tmp_path, me):
    board = touch(tmp_path / "p.kicad_pcb")
    (tmp_path / "~p.kicad_pcb.lck").write_bytes(b"[" * 100000 + b"]" * 100000)
    [entry] = kicad_lock.find_locks([board], kicad_running=no_kicad)
    assert entry["owner"] is None and entry["stale"] is None


def test_non_string_owner_fields_are_unreadable(tmp_path, me):
    board = touch(tmp_path / "p.kicad_pcb")
    (tmp_path / "~p.kicad_pcb.lck").write_bytes(b'{"hostname":["TESTHOST-01"],"username":"Test User"}')
    [entry] = kicad_lock.find_locks([board], kicad_running=no_kicad)
    assert entry["owner"] is None and entry["owner_readable"] is False


@pytest.mark.parametrize("windows, expected", [(True, True), (False, False)])
def test_user_name_compares_case_insensitively_only_on_windows(tmp_path, me, monkeypatch, windows, expected):
    monkeypatch.setattr(kicad_lock, "WINDOWS", windows)
    board, _ = only_lock(tmp_path, username=ME_USER.upper())
    [entry] = kicad_lock.find_locks([board], kicad_running=no_kicad)
    assert entry["owned_by_this_user_and_host"] is expected


# --- the hint never advises removing a lock that is not stale for certain --


@pytest.mark.parametrize(
    "lock_kwargs, running",
    [({}, kicad_up), ({}, cannot_tell), ({"hostname": "OTHER-PC"}, no_kicad), ({"username": "X"}, no_kicad)],
)
def test_hint_never_mentions_removing_a_lock_that_is_not_stale(tmp_path, me, lock_kwargs, running):
    board, _ = only_lock(tmp_path, **lock_kwargs)
    refusal = kicad_lock.check_writable([board], kicad_running=running)
    assert refusal["locks"][0]["stale"] is not True
    assert "remove" not in refusal["hint"].lower()
    assert "remove" not in refusal["message"].lower()


def test_hint_names_only_the_stale_locks_as_removable(tmp_path, me):
    board, stale_lock = only_lock(tmp_path)
    pro = touch(tmp_path / "p.kicad_pro")
    other_lock = write_lock(pro, hostname="OTHER-PC")
    refusal = kicad_lock.check_writable([board, pro], kicad_running=no_kicad)
    assert [e["stale"] for e in refusal["locks"]] == [True, None]
    assert str(stale_lock) in refusal["hint"]
    assert str(other_lock) not in refusal["hint"]
    assert "Some of these locks look stale" in refusal["message"]


def test_all_stale_says_this_lock_looks_stale(tmp_path, me):
    board, lock = only_lock(tmp_path)
    refusal = kicad_lock.check_writable([board], kicad_running=no_kicad)
    assert "This lock looks stale" in refusal["message"]
    assert str(lock) in refusal["hint"]


def test_refusal_without_a_stale_report_never_mentions_removing(tmp_path):
    refusal = kicad_lock.refusal(tmp_path / "p.kicad_pcb", tmp_path / "~p.kicad_pcb.lck")
    assert "remove" not in refusal["hint"].lower()


def test_unknown_process_list_gives_unknown_liveness(tmp_path, me):
    board, _ = only_lock(tmp_path)
    [entry] = kicad_lock.find_locks([board], kicad_running=cannot_tell)
    assert entry["stale"] is None


def test_process_list_is_not_asked_when_nothing_is_locked(tmp_path, me):
    board = touch(tmp_path / "p.kicad_pcb")

    def boom():
        raise AssertionError("listed processes with no lock present")

    assert kicad_lock.check_writable([board], kicad_running=boom) is None


def test_process_list_is_asked_once_per_check(tmp_path, me):
    board = touch(tmp_path / "p.kicad_pcb")
    pro = touch(tmp_path / "p.kicad_pro")
    write_lock(board)
    write_lock(pro)
    calls = []

    def counting():
        calls.append(1)
        return []

    kicad_lock.check_writable([board, pro], kicad_running=counting)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "names, expected",
    [
        (["explorer.exe", "python.exe"], []),
        (["pcbnew.exe", "kicad-cli.exe"], ["pcbnew.exe"]),
        (["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad", "eeschema"], ["kicad", "eeschema"]),
        (["KICAD.EXE"], ["KICAD.EXE"]),
        ([r"C:\Program Files\KiCad\10.0\bin\eeschema.exe"], ["eeschema.exe"]),
        (["pcbnew-helper", "my_kicad", "kicad.exe.bak"], []),
    ],
)
def test_only_programs_that_take_locks_count_as_kicad(names, expected):
    assert kicad_lock.kicad_programs_in(names) == expected


def test_the_real_process_list_is_a_list_or_unknown():
    running = kicad_lock.kicad_processes_running()
    assert running is None or isinstance(running, list)


# --- the process list: "cannot tell" stays None, never [] -----------------


@pytest.mark.parametrize("windows", [True, False], ids=["tasklist", "ps"])
@pytest.mark.parametrize(
    "failure",
    [
        FileNotFoundError(2, "not found"),
        PermissionError(13, "denied"),
        subprocess.TimeoutExpired(cmd="x", timeout=15),
    ],
    ids=["not_found", "denied", "timeout"],
)
def test_a_process_list_that_cannot_run_is_unknown(monkeypatch, windows, failure):
    def run(*args, **kwargs):
        raise failure

    monkeypatch.setattr(kicad_lock.subprocess, "run", run)
    assert kicad_lock.kicad_processes_running(windows=windows) is None


@pytest.mark.parametrize("windows", [True, False], ids=["tasklist", "ps"])
def test_a_process_list_that_exits_non_zero_is_unknown(monkeypatch, windows):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout='"pcbnew.exe","1"\n', stderr="boom")

    monkeypatch.setattr(kicad_lock.subprocess, "run", run)
    assert kicad_lock.kicad_processes_running(windows=windows) is None


@pytest.mark.parametrize(
    "windows, stdout, expected",
    [
        (True, '"System Idle Process","0","Services","0","8 K"\n"pcbnew.exe","42","Console","1","9 K"\n', ["pcbnew.exe"]),
        (True, "", []),
        (False, "/usr/bin/eeschema\nbash\n", ["eeschema"]),
        (False, "", []),
    ],
)
def test_a_process_list_that_runs_is_parsed(monkeypatch, windows, stdout, expected):
    seen = {}

    def run(argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    monkeypatch.setattr(kicad_lock.subprocess, "run", run)
    assert kicad_lock.kicad_processes_running(windows=windows) == expected
    assert seen["argv"][0] == ("tasklist" if windows else "ps")
    assert isinstance(seen["kwargs"].get("timeout"), (int, float)) and 0 < seen["kwargs"]["timeout"] <= 30


def test_a_failed_process_list_makes_an_own_lock_unknown_not_stale(tmp_path, me, monkeypatch):
    def run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="tasklist", timeout=15)

    monkeypatch.setattr(kicad_lock.subprocess, "run", run)
    board, _ = only_lock(tmp_path)
    [entry] = kicad_lock.find_locks([board])
    assert entry["stale"] is None
