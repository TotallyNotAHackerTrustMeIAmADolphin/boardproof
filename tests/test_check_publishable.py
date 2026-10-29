"""Publish-prep ticket 01: the publishability checker (`scripts/check_publishable.py`).

Every planted value is assembled at run time from fragments, so this file itself holds no complete
home path, address or token shape and the checker passes on the repository that contains it. The
owner's identity patterns are not in the repository; the forbidden-file tests plant a synthetic
name of their own.
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("check_publishable", ROOT / "scripts" / "check_publishable.py")
check = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check)

PLANTED = {
    "windows-home-path": "path = '" + "C:" + "\\Users\\" + "alice" + "\\Documents'",
    "windows-home-path-slash": "path = '" + "D:" + "/Users/" + "alice" + "/x'",
    "unix-home-path": "cd " + "/home" + "/alice/" + "project",
    "email-address": "contact " + "alice" + "@" + "corp" + ".io",
    "github-token": "t = '" + "gh" + "p_" + "A" * 36 + "'",
    "aws-access-key": "k = '" + "AK" + "IA" + "ABCDEFGHIJKLMNOP" + "'",
    "api-secret-key": "k = '" + "sk" + "-" + "a1B2c3D4e5F6g7H8i9J0" + "'",
    "private-key-header": "-----BEGIN " + "RSA PRIVATE" + " KEY-----",
}
EXPECTED_PATTERN = {name: name.removesuffix("-slash") for name in PLANTED}
SYNTHETIC_NAME = "Zebra" + "Quux"


def _run(tmp_path, capsys, *args):
    code = check.main([str(tmp_path), *args])
    return code, capsys.readouterr()


def _clean_tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "a.py").write_text("print('hello')\n", encoding="utf-8")
    (root / "README.md").write_text(
        "Board in `<home>\\Documents\\x`, checkout `<main checkout>`, "
        "mail 60357333+alias@users.noreply.github.com, "
        "Co-Authored-By: Claude <noreply@anthropic.com>, t@example.com, "
        "example path " + "C:" + "/Users/" + "someone/x, " + "ipc://C:" + "\\\\Users\\\\me\\\\k.sock, "
        "api at https://example.org/a_b-c, version sk-short, ghp_short, decorator \\n@pytest.fixture.\n",
        encoding="utf-8",
    )


def test_a_clean_tree_passes(tmp_path, capsys):
    _clean_tree(tmp_path)

    code, out = _run(tmp_path, capsys)

    assert code == 0, out.out
    assert "0 hits" in out.out


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_each_builtin_pattern_fails_its_own_mutant(tmp_path, capsys, name):
    _clean_tree(tmp_path)
    (tmp_path / "src" / "planted.txt").write_text("ok line\n" + PLANTED[name] + "\n", encoding="utf-8")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert f"src/planted.txt:2: [{EXPECTED_PATTERN[name]}]" in out.out
    assert f"count {EXPECTED_PATTERN[name]}: 1" in out.out


def test_the_output_shows_the_matched_span_only(tmp_path, capsys):
    (tmp_path / "f.txt").write_text("prefix words " + PLANTED["email-address"] + " suffix words\n", encoding="utf-8")

    _, out = _run(tmp_path, capsys)

    assert "suffix words" not in out.out
    assert "prefix words" not in out.out
    assert "alice" + "@" + "corp" + ".io" in out.out


def test_a_forbidden_file_pattern_fails_its_own_mutant_case_insensitively(tmp_path, capsys):
    tree = tmp_path / "tree"
    tree.mkdir()
    _clean_tree(tree)
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_text("# comment\n\nunrelated\\d+\n" + SYNTHETIC_NAME + "\n", encoding="utf-8")
    (tree / "doc.md").write_text("by " + SYNTHETIC_NAME.lower() + " himself\n", encoding="utf-8")

    code = check.main([str(tree), "--forbidden-file", str(forbidden)])
    out = capsys.readouterr().out

    assert code == 1
    assert "doc.md:1: [forbidden#2] " + SYNTHETIC_NAME.lower() in out
    assert "forbidden#1" not in out


def test_a_clean_tree_passes_with_a_forbidden_file(tmp_path, capsys):
    tree = tmp_path / "tree"
    tree.mkdir()
    _clean_tree(tree)
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_text(SYNTHETIC_NAME + "\n", encoding="utf-8")

    assert check.main([str(tree), "--forbidden-file", str(forbidden)]) == 0


@pytest.mark.parametrize(
    "content",
    [None, "", "\n\n", "# only a comment\n   \n", "(unclosed\n"],
    ids=["missing", "empty", "blank-lines", "comments-only", "invalid-regex"],
)
def test_a_missing_empty_or_invalid_forbidden_file_exits_2(tmp_path, capsys, content):
    tree = tmp_path / "tree"
    tree.mkdir()
    _clean_tree(tree)
    forbidden = tmp_path / "forbidden.txt"
    if content is not None:
        forbidden.write_text(content, encoding="utf-8")

    code = check.main([str(tree), "--forbidden-file", str(forbidden)])

    assert code == 2
    assert "hits" not in capsys.readouterr().out


def test_an_unreadable_forbidden_file_exits_2(tmp_path, capsys):
    tree = tmp_path / "tree"
    tree.mkdir()
    forbidden = tmp_path / "forbidden.txt"
    forbidden.mkdir()  # a directory cannot be read as a file

    assert check.main([str(tree), "--forbidden-file", str(forbidden)]) == 2


def test_a_missing_directory_exits_2(tmp_path):
    assert check.main([str(tmp_path / "nope")]) == 2


def test_git_is_skipped_and_excludes_apply_to_a_prefix_only(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text(PLANTED["email-address"] + "\n", encoding="utf-8")
    (tmp_path / ".scratch").mkdir()
    (tmp_path / ".scratch" / "t.md").write_text(PLANTED["email-address"] + "\n", encoding="utf-8")
    (tmp_path / ".scratch-other").mkdir()
    (tmp_path / ".scratch-other" / "t.md").write_text(PLANTED["email-address"] + "\n", encoding="utf-8")

    code, out = _run(tmp_path, capsys, "--exclude", ".scratch")

    assert code == 1
    assert ".scratch-other/t.md:1:" in out.out
    assert ".git/config" not in out.out
    assert "\n.scratch/t.md" not in out.out


def test_clean_binary_files_are_counted(tmp_path, capsys):
    (tmp_path / "a.bin").write_bytes(b"\x00\x01 clean")
    (tmp_path / "b.bin").write_bytes(b"\x00")
    (tmp_path / "t.txt").write_text("fine\n", encoding="utf-8")

    code, out = _run(tmp_path, capsys)

    assert code == 0
    assert "2 binary files" in out.out


OWN_FILES = ("scripts/check_publishable.py", "tests/test_check_publishable.py")


def test_the_committed_checker_and_tests_hold_no_hit_of_their_own(tmp_path):
    # Only the two files the name mentions, copied under their relative paths: the result must not
    # depend on what else lies under the repository directory (a .venv, unreadable folders).
    for rel in OWN_FILES:
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_bytes((ROOT / rel).read_bytes())

    hits, scanned, _ = check.scan(tmp_path, [], [])

    assert scanned == len(OWN_FILES)
    assert hits == []


def test_the_own_hit_test_does_not_scan_the_repository_directory(tmp_path, monkeypatch):
    roots = []
    real = check.scan

    def spy(root, *args, **kwargs):
        roots.append(Path(root).resolve())
        return real(root, *args, **kwargs)

    monkeypatch.setattr(check, "scan", spy)

    test_the_committed_checker_and_tests_hold_no_hit_of_their_own(tmp_path)

    assert roots and ROOT.resolve() not in roots


# ---------------------------------------------------------------- review fixes (B1-B3 and follow-ups)

DASHED = "C-" + "-Users-" + SYNTHETIC_NAME.lower() + "-Documents-proj"


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def test_b1_the_dashed_claude_project_dir_form_of_a_home_path_fails(tmp_path, capsys):
    _clean_tree(tmp_path)
    _write(tmp_path, "r.json", '"<home>\\\\AppData\\\\claude\\\\' + DASHED + '\\\\scratchpad"\n')

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "r.json:1: [dashed-home-path]" in out.out


def test_b1_a_placeholder_or_generic_dashed_form_passes(tmp_path, capsys):
    _write(tmp_path, "r.md", "C-" + "-Users-<name>-Documents and C-" + "-Users-me-Documents and Users-facing docs\n")

    code, _ = _run(tmp_path, capsys)

    assert code == 0


def test_b2_a_utf8_bom_forbidden_file_still_catches_its_first_pattern(tmp_path, capsys):
    tree = tmp_path / "tree"
    tree.mkdir()
    _write(tree, "doc.md", "by " + SYNTHETIC_NAME + "\n")
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_bytes(b"\xef\xbb\xbf" + SYNTHETIC_NAME.encode() + b"\n")

    code = check.main([str(tree), "--forbidden-file", str(forbidden)])

    assert code == 1
    assert "[forbidden#1]" in capsys.readouterr().out


@pytest.mark.parametrize("junk", ["﻿", "\x00"], ids=["inner-bom", "nul"])
def test_b2_a_pattern_line_with_a_bom_or_nul_after_decoding_exits_2(tmp_path, capsys, junk):
    tree = tmp_path / "tree"
    tree.mkdir()
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_bytes(("fine\n" + junk + SYNTHETIC_NAME + "\n").encode("utf-8"))

    assert check.main([str(tree), "--forbidden-file", str(forbidden)]) == 2


def test_b2_a_utf16_forbidden_file_exits_2(tmp_path):
    tree = tmp_path / "tree"
    tree.mkdir()
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_text(SYNTHETIC_NAME + "\n", encoding="utf-16")

    assert check.main([str(tree), "--forbidden-file", str(forbidden)]) == 2


def test_b3_an_uppercase_or_lowercase_windows_home_path_fails_and_generic_names_pass_in_any_case(tmp_path, capsys):
    _write(tmp_path, "a.txt", "c:" + "\\users\\" + "alice\\x\nD:" + "/USERS/" + "Alice/x\n")
    _write(tmp_path, "ok.txt", "C:" + "\\USERS\\" + "ME\\x and c:" + "/users/" + "Someone/x\n")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "a.txt:1: [windows-home-path]" in out.out
    assert "a.txt:2: [windows-home-path]" in out.out
    assert "ok.txt" not in out.out


@pytest.mark.parametrize("escape", ["\\n", "\\t", "\\r", "\\\\n"], ids=["n", "t", "r", "doubled-n"])
def test_b3_an_address_after_a_literal_escape_sequence_is_found(tmp_path, capsys, escape):
    _write(tmp_path, "d.json", '{"a": "line' + escape + "alice" + "@" + "corp" + '.io"}\n')

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "[email-address] alice" + "@" + "corp" + ".io" in out.out


def test_b3_a_decorator_after_an_escape_sequence_is_still_not_an_address(tmp_path, capsys):
    _write(tmp_path, "d.py", 'x = "a\\n@pytest.fixture and b\\t@app.route"\n')

    assert _run(tmp_path, capsys)[0] == 0


def test_b3_noreply_inside_a_real_local_part_is_a_hit(tmp_path, capsys):
    _write(tmp_path, "m.txt", "bob" + "noreply" + "@" + "gmail" + ".com\n")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "[email-address]" in out.out


def test_b3_the_real_noreply_forms_pass(tmp_path, capsys):
    _write(
        tmp_path,
        "m.txt",
        "1+a@users.noreply.github.com noreply@anthropic.com noreply@github.com no-reply@github.com\n",
    )

    assert _run(tmp_path, capsys)[0] == 0


def test_f_a_utf16_text_file_with_a_bom_is_scanned(tmp_path, capsys):
    (tmp_path / "w.txt").write_text("contact " + "alice" + "@" + "corp" + ".io\n", encoding="utf-16")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "w.txt:1: [email-address]" in out.out


def test_f_a_binary_file_is_scanned_bytewise_for_builtin_and_forbidden_patterns(tmp_path, capsys):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.bin").write_bytes(b"\x00\x01" + PLANTED["email-address"].encode() + b"\x00")
    (tree / "b.bin").write_bytes(b"\x00\x02 " + SYNTHETIC_NAME.encode() + b" \x00")
    (tree / "c.bin").write_bytes(b"\x00\x03clean")
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_text(SYNTHETIC_NAME + "\n", encoding="utf-8")

    code = check.main([str(tree), "--forbidden-file", str(forbidden)])
    out = capsys.readouterr().out

    assert code == 1
    assert "a.bin:0: [email-address]" in out
    assert "b.bin:0: [forbidden#1]" in out
    assert "c.bin" not in out
    assert "3 binary files" in out


def test_f_a_symlink_is_reported(tmp_path, capsys, monkeypatch):
    (tmp_path / "real.txt").write_text("fine\n", encoding="utf-8")
    try:
        (tmp_path / "link.txt").symlink_to(tmp_path / "real.txt")
    except (OSError, NotImplementedError):
        # No symlink rights on this machine: stand in for one with a plain file.
        (tmp_path / "link.txt").write_text("fine\n", encoding="utf-8")
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "link.txt" or real_is_symlink(self))
        monkeypatch.setattr(os, "readlink", lambda p: "real.txt")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "link.txt:0: [symlink]" in out.out


def test_f_a_nested_dot_git_directory_does_not_hide_files(tmp_path, capsys):
    _write(tmp_path, "sub/.git/config", PLANTED["email-address"] + "\n")
    _write(tmp_path, ".git/config", PLANTED["email-address"] + "\n")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "sub/.git/config:1:" in out.out
    assert "\n.git/config" not in "\n" + out.out


def test_f_an_unreadable_file_exits_2_with_a_message_and_no_traceback(tmp_path, capsys, monkeypatch):
    (tmp_path / "locked.txt").write_text("x\n", encoding="utf-8")
    real = Path.read_bytes

    def boom(self):
        if self.name == "locked.txt":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", boom)

    code, out = _run(tmp_path, capsys)

    assert code == 2
    assert "locked.txt" in out.err
    assert "Traceback" not in out.err


def test_f_file_and_directory_names_are_scanned(tmp_path, capsys):
    tree = tmp_path / "tree"
    (tree / (SYNTHETIC_NAME + "dir")).mkdir(parents=True)
    (tree / ("report_" + SYNTHETIC_NAME + ".txt")).write_text("fine\n", encoding="utf-8")
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_text(SYNTHETIC_NAME + "\n", encoding="utf-8")

    code = check.main([str(tree), "--forbidden-file", str(forbidden)])
    out = capsys.readouterr().out

    assert code == 1
    assert f"{SYNTHETIC_NAME}dir:0: [forbidden#1]" in out
    assert f"report_{SYNTHETIC_NAME}.txt:0: [forbidden#1]" in out


NEW_SHAPES = {
    "msys-home-path": "cd /" + "c" + "/Users/" + "alice" + "/x",
    "macos-home-path": "cd " + "/Users" + "/alice" + "/x",
    "github-fine-grained-token": "t = github" + "_pat_" + "A1b2C3d4E5f6G7h8I9j0K1",
    "slack-token": "t = xo" + "xb-" + "1234567890-abcdefghij",
    "google-api-key": "k = AI" + "za" + "A" * 35,
}


@pytest.mark.parametrize("name", sorted(NEW_SHAPES))
def test_f_new_builtin_shapes_fail_their_own_mutant(tmp_path, capsys, name):
    _write(tmp_path, "p.txt", "ok\n" + NEW_SHAPES[name] + "\n")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert f"p.txt:2: [{name}]" in out.out
    assert "count windows-home-path" not in out.out


def test_f_a_windows_path_is_not_also_a_macos_hit(tmp_path, capsys):
    _write(tmp_path, "p.txt", "C:" + "/Users/" + "alice" + "/x\n")

    _, out = _run(tmp_path, capsys)

    assert "macos-home-path" not in out.out
    assert "msys-home-path" not in out.out


def test_f_junk_that_looks_like_a_tiny_address_in_a_binary_file_is_not_a_hit(tmp_path, capsys):
    (tmp_path / "z.bin").write_bytes(b"\x00\x01 4" + b"@" + b"c.cx \x00 h" + b"@" + b"y.O \x00")

    assert _run(tmp_path, capsys)[0] == 0


def test_f_a_capitalised_home_directory_of_a_program_is_not_a_unix_home_path(tmp_path, capsys):
    _write(tmp_path, "j.py", '"/Library/Java/Contents/' + 'Ho' + 'me/bin/java"\n')

    assert _run(tmp_path, capsys)[0] == 0


def test_delta_an_unreadable_subdirectory_exits_2_and_names_it(tmp_path, capsys, monkeypatch):
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "a.txt").write_text("fine\n", encoding="utf-8")
    (tmp_path / "open.txt").write_text("fine\n", encoding="utf-8")
    real = os.scandir

    def deny(path="."):
        if Path(path).name == "locked":
            raise PermissionError(13, "denied", str(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", deny)

    code, out = _run(tmp_path, capsys)

    assert code == 2
    assert "locked" in out.err
    assert "Traceback" not in out.err


def test_delta_a_hit_that_the_console_encoding_cannot_show_does_not_stop_the_output(tmp_path, monkeypatch):
    cjk = chr(0x4E2D) + chr(0x6587)
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "a.txt").write_text("x " + cjk + "\n", encoding="utf-8")
    (tree / "b.txt").write_text("y " + cjk + "\n", encoding="utf-8")
    forbidden = tmp_path / "forbidden.txt"
    forbidden.write_text(cjk + "\n", encoding="utf-8")
    raw = io.BytesIO()
    console = io.TextIOWrapper(raw, encoding="cp1252", errors="strict", newline="")
    monkeypatch.setattr(sys, "stdout", console)

    code = check.main([str(tree), "--forbidden-file", str(forbidden)])
    console.flush()

    text = raw.getvalue().decode("utf-8")
    assert code == 1
    assert "a.txt:1:" in text and "b.txt:1:" in text
    assert "scanned" in text


def test_delta_a_junction_is_reported_and_not_followed(tmp_path, capsys, monkeypatch):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("fine\n", encoding="utf-8")
    real = check._is_junction
    monkeypatch.setattr(check, "_is_junction", lambda p: Path(p).name == "d" or real(p))

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "d:0: [junction]" in out.out
    assert "d/f.txt" not in out.out
    assert "scanned 0 text files" in out.out


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_delta_a_real_junction_to_the_parent_does_not_loop(tmp_path, capsys):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "f.txt").write_text("fine\n", encoding="utf-8")
    made = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(tree / "up"), str(tree)], capture_output=True
    )
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")

    code, out = _run(tree, capsys)

    assert code == 1
    assert "up:0: [junction]" in out.out
    assert "scanned 1 text files" in out.out


def test_delta_archives_are_named_as_not_opened(tmp_path, capsys):
    import zipfile

    with zipfile.ZipFile(tmp_path / "a.docx", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("w.xml", "fine " * 50)
    (tmp_path / "t.txt").write_text("fine\n", encoding="utf-8")

    code, out = _run(tmp_path, capsys)

    assert code == 0
    assert "archive not opened: a.docx" in out.out
    assert "1 archives not opened" in out.out


# --- publish-prep ticket 02c: follow-ups of the checker --------------------------------------------


def test_c1_a_python_without_os_path_isjunction_still_runs_the_checker(tmp_path, capsys, monkeypatch):
    """Python 3.11 has no os.path.isjunction (pyproject says >=3.11): no AttributeError, no exit 1
    from a traceback; a clean tree stays clean."""
    _clean_tree(tmp_path)
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "f.txt").write_text("fine\n", encoding="utf-8")
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    assert not hasattr(os.path, "isjunction")

    code, out = _run(tmp_path, capsys)

    assert code == 0, out
    assert "scanned 3 text files" in out.out


@pytest.mark.skipif(sys.platform != "win32", reason="junctions are a Windows feature")
def test_c1_a_real_junction_is_still_found_without_os_path_isjunction(tmp_path, capsys, monkeypatch):
    tree = tmp_path / "tree"
    tree.mkdir()
    (tree / "f.txt").write_text("fine\n", encoding="utf-8")
    made = subprocess.run(["cmd", "/c", "mklink", "/J", str(tree / "up"), str(tree)], capture_output=True)
    if made.returncode != 0:
        pytest.skip("cannot create a junction here")
    monkeypatch.delattr(os.path, "isjunction", raising=False)

    code, out = _run(tree, capsys)

    assert code == 1
    assert "up:0: [junction]" in out.out
    assert "scanned 1 text files" in out.out


def test_c1_a_plain_file_or_directory_is_not_a_junction_without_os_path_isjunction(tmp_path, monkeypatch):
    (tmp_path / "d").mkdir()
    (tmp_path / "f.txt").write_text("x\n", encoding="utf-8")
    monkeypatch.delattr(os.path, "isjunction", raising=False)
    assert check._is_junction(tmp_path / "d") is False
    assert check._is_junction(tmp_path / "f.txt") is False
    assert check._is_junction(tmp_path / "missing") is False


def _pdf(tmp_path, name, body):
    (tmp_path / name).write_bytes(b"%PDF-1.4\n1 0 obj\n<< " + body + b" >>\nstream\n\x00\x01\x02\xff\nendstream\nendobj\n")


def test_c2_a_pdf_with_a_flate_stream_is_named_as_an_unopened_archive(tmp_path, capsys):
    _pdf(tmp_path, "a.pdf", b"/Filter /FlateDecode")
    _pdf(tmp_path, "plain.pdf", b"/Filter /ASCIIHexDecode")

    code, out = _run(tmp_path, capsys)

    assert code == 0
    assert "archive not opened: a.pdf" in out.out
    assert "plain.pdf" not in out.out.replace("scanned", "")
    assert "1 archives not opened" in out.out


def _zip(tmp_path, name):
    import zipfile

    with zipfile.ZipFile(tmp_path / name, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("w.xml", "fine " * 50)


def test_c3_without_the_flag_an_archive_is_a_notice_and_exit_0(tmp_path, capsys):
    _zip(tmp_path, "a.zip")
    code, out = _run(tmp_path, capsys)
    assert code == 0 and "archive not opened: a.zip" in out.out


def test_c3_strict_archives_exits_3_and_lists_every_unopened_archive(tmp_path, capsys):
    _zip(tmp_path, "a.zip")
    _pdf(tmp_path, "b.pdf", b"/Filter /FlateDecode")
    (tmp_path / "t.txt").write_text("fine\n", encoding="utf-8")

    code, out = _run(tmp_path, capsys, "--strict-archives")

    assert code == check.EXIT_ARCHIVES == 3
    assert "archive not allowed: a.zip" in out.out
    assert "archive not allowed: b.pdf" in out.out


def test_c3_strict_archives_accepts_an_allow_list_by_prefix(tmp_path, capsys):
    (tmp_path / "docs").mkdir()
    _zip(tmp_path / "docs", "a.zip")
    _zip(tmp_path, "docs2.zip")

    code, out = _run(tmp_path, capsys, "--strict-archives", "--allow-archive", "docs")
    assert code == 3
    assert "archive not allowed: docs2.zip" in out.out
    assert "archive not allowed: docs/a.zip" not in out.out

    code, out = _run(tmp_path, capsys, "--strict-archives", "--allow-archive", "docs", "--allow-archive", "docs2.zip")
    assert code == 0, out
    assert "archive not allowed" not in out.out


def test_c3_a_hit_outranks_an_unopened_archive_and_an_excluded_archive_is_not_listed(tmp_path, capsys):
    _zip(tmp_path, "a.zip")
    (tmp_path / "t.txt").write_text(PLANTED["email-address"] + "\n", encoding="utf-8")
    code, out = _run(tmp_path, capsys, "--strict-archives")
    assert code == 1 and "archive not allowed: a.zip" in out.out

    (tmp_path / "t.txt").write_text("fine\n", encoding="utf-8")
    code, out = _run(tmp_path, capsys, "--strict-archives", "--exclude", "a.zip")
    assert code == 0, out


def test_c3_allow_archive_without_strict_archives_exits_2(tmp_path):
    with pytest.raises(SystemExit) as stop:
        check.main([str(tmp_path), "--allow-archive", "x"])
    assert stop.value.code == 2


def test_c4_the_docstring_states_the_remaining_limits():
    doc = check.__doc__.lower()
    for limit in ("look-alike", "line break", "--strict-archives"):
        assert limit in doc, limit


@pytest.mark.parametrize("name, data", [
    ("a.bz2", b"BZh9" + b"\x31\x41\x59\x26\x53\x59" * 4),
    ("a.xz", b"\xfd7zXZ\x00" + b"\x00\x04\xe6\xd6\xb4\x46" * 4),
    ("a.zst", b"\x28\xb5\x2f\xfd" + b"\x20\x05\x29\x00\x00" * 4),
    ("a.tar", b"f.txt".ljust(257, b"\x00") + b"ustar\x0000" + b"\x00" * 200),
])
def test_c3_bz2_xz_zstd_and_tar_are_unopened_archives_too(tmp_path, capsys, name, data):
    (tmp_path / name).write_bytes(data)
    code, out = _run(tmp_path, capsys, "--strict-archives")
    assert code == 3, out
    assert f"archive not allowed: {name}" in out.out


def test_c3_a_text_file_that_starts_with_bzh_is_still_only_a_notice_candidate(tmp_path, capsys):
    """Honest limit of the magic list: 'BZh' text is named as an archive (a false notice, never a miss)."""
    (tmp_path / "t.txt").write_text("BZh is a name\n", encoding="utf-8")
    code, out = _run(tmp_path, capsys)
    assert code == 0 and "archive not opened: t.txt" in out.out


# --- publish-prep ticket 03b: a git submodule is another project's content ----------------------


SUBMODULE = "third_party/Other-Lib"


def _with_submodule(root: Path, gitmodules_path: str = SUBMODULE) -> None:
    """A tree with a `.gitmodules` entry and a checked-out submodule holding a planted hit."""
    (root / ".gitmodules").write_text(
        f'[submodule "{gitmodules_path}"]\n\tpath = {gitmodules_path}\n\turl = https://example.org/x/y\n',
        encoding="utf-8",
    )
    inside = root / gitmodules_path
    inside.mkdir(parents=True)
    (inside / "notes.md").write_text(PLANTED["email-address"] + "\n", encoding="utf-8")


def test_a_hit_inside_a_declared_submodule_is_ignored_and_the_skip_is_announced(tmp_path, capsys):
    _clean_tree(tmp_path)
    _with_submodule(tmp_path)

    code, out = _run(tmp_path, capsys)

    assert code == 0
    assert "Other-Lib/notes.md" not in out.out
    assert f"submodule skipped: {SUBMODULE}" in out.out


def test_a_hit_outside_the_submodule_is_still_caught(tmp_path, capsys):
    _clean_tree(tmp_path)
    _with_submodule(tmp_path)
    (tmp_path / "third_party" / "Mine.md").write_text(PLANTED["email-address"] + "\n", encoding="utf-8")
    (tmp_path / "third_party" / "Other-Lib-twin").mkdir()
    (tmp_path / "third_party" / "Other-Lib-twin" / "a.md").write_text(PLANTED["email-address"] + "\n", encoding="utf-8")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "third_party/Mine.md:1:" in out.out
    assert "third_party/Other-Lib-twin/a.md:1:" in out.out
    assert "Other-Lib/notes.md" not in out.out


def test_without_a_gitmodules_entry_the_same_path_is_scanned(tmp_path, capsys):
    _clean_tree(tmp_path)
    _with_submodule(tmp_path)
    (tmp_path / ".gitmodules").unlink()

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "third_party/Other-Lib/notes.md:1:" in out.out


def test_scan_submodules_flag_scans_them_too(tmp_path, capsys):
    _clean_tree(tmp_path)
    _with_submodule(tmp_path)

    code, out = _run(tmp_path, capsys, "--scan-submodules")

    assert code == 1
    assert "third_party/Other-Lib/notes.md:1:" in out.out
    assert "submodule skipped" not in out.out


@pytest.mark.parametrize("bad", [".", "./", "..", "../x", "/abs", "a/../b", ""])
def test_an_unsafe_gitmodules_path_never_hides_anything(tmp_path, capsys, bad):
    """A `path = .` entry must not turn the whole tree into 'another project's content'."""
    _clean_tree(tmp_path)
    (tmp_path / ".gitmodules").write_text(f'[submodule "x"]\n\tpath = {bad}\n', encoding="utf-8")
    (tmp_path / "leak.md").write_text(PLANTED["email-address"] + "\n", encoding="utf-8")

    code, out = _run(tmp_path, capsys)

    assert code == 1
    assert "leak.md:1:" in out.out
    assert "submodule skipped" not in out.out


def test_the_real_repository_declares_the_jlcpcb_library_and_the_checker_skips_it():
    """The pinned library path in `.gitmodules` is the one the sourcing module expects."""
    declared = check.submodule_paths(ROOT)
    assert "third_party/JLCPCB-Kicad-Library" in declared
