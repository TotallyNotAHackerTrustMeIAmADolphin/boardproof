"""Publish-prep ticket 03a, part C: `scripts/issues.py`, the helper that wraps the `gh` CLI.

No test touches GitHub: a fake `gh` (a small Python script placed first on PATH in a tmp dir)
records its argv and stdin and returns canned output. Planted values are assembled at run time from
fragments, so this file holds no complete home path and no real name.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("issues_script", ROOT / "scripts" / "issues.py")
issues = importlib.util.module_from_spec(_spec)
sys.modules["issues_script"] = issues
_spec.loader.exec_module(issues)

SYNTHETIC_NAME = "Zebra" + "Quux"
PLANTED_PATH = "C:" + "\\Users\\" + "alice" + "\\Documents\\x"

FAKE_GH = r'''
import json, os, sys
state = os.environ["FAKE_GH_DIR"]
argv = sys.argv[1:]
stdin = "" if sys.stdin is None or sys.stdin.isatty() else sys.stdin.read()
with open(os.path.join(state, "calls.jsonl"), "a", encoding="utf-8") as fh:
    fh.write(json.dumps({"argv": argv, "stdin": stdin}) + "\n")
if argv[:2] == ["repo", "view"]:
    if os.environ.get("FAKE_GH_REPO_DOWN"):
        sys.stderr.write("GraphQL: Could not resolve to a Repository\n")
        sys.exit(1)
    print("{}")
elif argv[:2] == ["issue", "create"]:
    counter = os.path.join(state, "counter")
    n = int(open(counter).read()) + 1 if os.path.exists(counter) else 1
    open(counter, "w").write(str(n))
    print("https://github.com/example/repo/issues/%d" % n)
elif argv[:2] == ["label", "list"]:
    print(os.environ.get("FAKE_GH_LABELS") or json.dumps(
        [{"name": n} for n in "map build research decision task critical parked needs-owner".split()]))
elif argv[:2] == ["issue", "view"]:
    print("CANNED VIEW " + argv[2])
elif argv[:2] == ["issue", "list"]:
    print(json.dumps([{"number": 7, "title": "T7", "state": "OPEN",
                       "labels": [{"name": "build"}]}]))
'''


@pytest.fixture()
def fake_gh(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    state = tmp_path / "state"
    state.mkdir()
    (bin_dir / "fake_gh.py").write_text(FAKE_GH, encoding="utf-8")
    if os.name == "nt":
        (bin_dir / "gh.cmd").write_text(
            f'@"{sys.executable}" "%~dp0fake_gh.py" %*\r\n', encoding="utf-8"
        )
    else:
        gh = bin_dir / "gh"
        gh.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$(dirname "$0")/fake_gh.py" "$@"\n')
        gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("FAKE_GH_DIR", str(state))
    monkeypatch.delenv("FAKE_GH_REPO_DOWN", raising=False)
    forbidden = tmp_path / "default-forbidden.txt"
    forbidden.write_text("NoSuchNameAnywhere" + "Zz\n", encoding="utf-8")
    monkeypatch.setenv("BOARDPROOF_FORBIDDEN_FILE", str(forbidden))
    monkeypatch.setenv("BOARDPROOF_REPO", "example/repo")
    monkeypatch.delenv("BOARDPROOF_ALLOW_UNCHECKED", raising=False)

    def calls():
        log = state / "calls.jsonl"
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return calls


def _write(tmp_path, name, text):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _forbidden(tmp_path, monkeypatch):
    path = _write(tmp_path, "forbidden.txt", SYNTHETIC_NAME + "\n")
    monkeypatch.setenv("BOARDPROOF_FORBIDDEN_FILE", str(path))


def _writes(calls):
    return [c for c in calls() if c["argv"][:2] not in (["repo", "view"],)]


def test_create_sends_title_labels_body_and_blocked_by(fake_gh, tmp_path, capsys):
    body = _write(tmp_path, "b.md", "Question and acceptance list.\n")
    code = issues.main(["create", "--title", "Do the thing", "--body-file", str(body),
                        "--label", "build", "--label", "critical",
                        "--blocked-by", "3", "--blocked-by", "5"])
    assert code == 0
    create = [c for c in fake_gh() if c["argv"][:2] == ["issue", "create"]]
    assert len(create) == 1
    argv = create[0]["argv"]
    assert argv[argv.index("--title") + 1] == "Do the thing"
    labels = [argv[i + 1] for i, a in enumerate(argv) if a == "--label"]
    assert labels == ["build", "critical"]
    assert "Question and acceptance list." in create[0]["stdin"]
    assert "Blocked by #3" in create[0]["stdin"]
    assert "Blocked by #5" in create[0]["stdin"]
    assert "/issues/1" in capsys.readouterr().out


def test_comment_close_list_show(fake_gh, tmp_path, capsys):
    body = _write(tmp_path, "c.md", "Answer: done at sha abc1234.\n")
    assert issues.main(["comment", "12", "--body-file", str(body)]) == 0
    assert issues.main(["close", "12", "--comment-file", str(body)]) == 0
    assert issues.main(["close", "13"]) == 0
    assert issues.main(["list", "--label", "build", "--state", "open"]) == 0
    assert "#7" in capsys.readouterr().out
    assert issues.main(["show", "12"]) == 0
    assert "CANNED VIEW 12" in capsys.readouterr().out
    argvs = [c["argv"] for c in fake_gh()]
    assert ["issue", "comment", "12", "--body-file", "-", "--repo", "example/repo"] in argvs
    closes = [a for a in argvs if a[:2] == ["issue", "close"]]
    assert closes[0][:3] == ["issue", "close", "12"] and "--comment" in closes[0]
    assert closes[1] == ["issue", "close", "13", "--repo", "example/repo"]
    listing = next(a for a in argvs if a[:2] == ["issue", "list"])
    assert "--label" in listing and "build" in listing and "open" in listing
    assert ["issue", "view", "12", "--comments", "--repo", "example/repo"] in argvs


def test_label_sync_creates_every_label(fake_gh):
    assert issues.main(["label-sync"]) == 0
    created = [c["argv"][2] for c in fake_gh() if c["argv"][:2] == ["label", "create"]]
    assert created == [name for name, _, _ in issues.LABELS]
    assert {"map", "build", "research", "decision", "task", "critical", "parked",
            "needs-owner"} <= set(created)
    assert all("--force" in c["argv"] for c in fake_gh() if c["argv"][:2] == ["label", "create"])


@pytest.mark.parametrize("where", ["title", "body", "comment", "close-comment"])
def test_refuses_forbidden_name_with_exit_2_and_sends_nothing(fake_gh, tmp_path, monkeypatch, where):
    _forbidden(tmp_path, monkeypatch)
    dirty = f"has {SYNTHETIC_NAME} in it"
    clean = "clean text"
    f = _write(tmp_path, "f.md", dirty if where != "title" else clean)
    if where == "title":
        argv = ["create", "--title", dirty, "--body-file", str(f)]
    elif where == "body":
        argv = ["create", "--title", "ok", "--body-file", str(f)]
    elif where == "comment":
        argv = ["comment", "4", "--body-file", str(f)]
    else:
        argv = ["close", "4", "--comment-file", str(f)]
    assert issues.main(argv) == 2
    assert _writes(fake_gh) == []


def test_refuses_machine_path_without_forbidden_file(fake_gh, tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("BOARDPROOF_FORBIDDEN_FILE")
    monkeypatch.setenv("BOARDPROOF_ALLOW_UNCHECKED", "1")
    f = _write(tmp_path, "p.md", f"see {PLANTED_PATH}\n")
    assert issues.main(["create", "--title", "ok", "--body-file", str(f)]) == 2
    err = capsys.readouterr().err
    assert "windows-home-path" in err
    assert PLANTED_PATH not in err
    assert _writes(fake_gh) == []


def test_refuses_label_with_forbidden_name(fake_gh, tmp_path, monkeypatch):
    _forbidden(tmp_path, monkeypatch)
    f = _write(tmp_path, "f.md", "clean")
    assert issues.main(["create", "--title", "ok", "--body-file", str(f),
                        "--label", SYNTHETIC_NAME]) == 2
    assert _writes(fake_gh) == []


def test_unreadable_forbidden_file_refuses(fake_gh, tmp_path, monkeypatch):
    monkeypatch.setenv("BOARDPROOF_FORBIDDEN_FILE", str(tmp_path / "missing.txt"))
    f = _write(tmp_path, "f.md", "clean")
    assert issues.main(["create", "--title", "ok", "--body-file", str(f)]) == 2
    assert _writes(fake_gh) == []


def test_unreachable_repository_is_said_plainly(fake_gh, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_GH_REPO_DOWN", "1")
    f = _write(tmp_path, "f.md", "clean")
    for argv in (["create", "--title", "ok", "--body-file", str(f)], ["show", "1"],
                 ["list"], ["label-sync"]):
        assert issues.main(argv) == 3
        assert "repository is not reachable" in capsys.readouterr().err
    assert [c for c in fake_gh() if c["argv"][0] == "issue"] == []


def test_missing_gh_is_said_plainly(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PATH", str(tmp_path))
    monkeypatch.setenv("BOARDPROOF_REPO", "example/repo")
    assert issues.main(["show", "1"]) == 3
    assert "gh" in capsys.readouterr().err


def test_repo_option_is_passed_on(fake_gh, monkeypatch):
    monkeypatch.delenv("BOARDPROOF_REPO")
    assert issues.main(["--repo", "example/repo", "show", "2"]) == 0
    calls = fake_gh()
    assert calls[0]["argv"] == ["repo", "view", "example/repo", "--json", "name"]
    for c in calls[1:]:
        assert c["argv"][-2:] == ["--repo", "example/repo"], c


BATCH_A = """Id: build-111
Title: Pin the first thing
Labels: build, critical
Blocked by: build-112

Formerly build ticket 111.

Acceptance: a, b.
"""
BATCH_B = """Id: build-112
Title: Base thing
Labels: build

Formerly build ticket 112.
"""
BATCH_MAP = """Id: map-1
Title: Map of rung 1
Labels: map

Formerly a map.

- [ ] #ref:build-111
- [ ] #ref:build-112

After #ref:build-112. Then (#ref:build-111).
"""


def _batch_dir(tmp_path):
    d = tmp_path / "batch"
    d.mkdir()
    _write(d, "a.md", BATCH_A)
    _write(d, "b.md", BATCH_B)
    _write(d, "map-1.md", BATCH_MAP)
    return d


def test_create_batch_orders_by_dependency_and_rewrites_ids(fake_gh, tmp_path, capsys):
    d = _batch_dir(tmp_path)
    assert issues.main(["create-batch", str(d)]) == 0
    creates = [c for c in fake_gh() if c["argv"][:2] == ["issue", "create"]]
    titles = [c["argv"][c["argv"].index("--title") + 1] for c in creates]
    assert titles == ["Base thing", "Pin the first thing", "Map of rung 1"]
    assert "Blocked by #1" in creates[1]["stdin"]
    assert "build-112" not in creates[1]["stdin"]
    assert "- [ ] #2" in creates[2]["stdin"] and "- [ ] #1" in creates[2]["stdin"]
    assert "#ref:" not in creates[2]["stdin"]
    assert "After #1. Then (#2)." in creates[2]["stdin"]
    assert "build-111 -> #2" in capsys.readouterr().out


def test_create_batch_is_all_or_nothing_on_a_hit(fake_gh, tmp_path, monkeypatch):
    _forbidden(tmp_path, monkeypatch)
    d = _batch_dir(tmp_path)
    _write(d, "b.md", BATCH_B + f"\nmentions {SYNTHETIC_NAME}\n")
    assert issues.main(["create-batch", str(d)]) == 2
    assert _writes(fake_gh) == []


def test_create_batch_cycle_and_unknown_reference_refuse(fake_gh, tmp_path):
    d = _batch_dir(tmp_path)
    _write(d, "b.md", BATCH_B.replace("Labels: build", "Labels: build\nBlocked by: build-111"))
    assert issues.main(["create-batch", str(d)]) == 2
    d2 = tmp_path / "batch2"
    d2.mkdir()
    _write(d2, "a.md", BATCH_A)
    assert issues.main(["create-batch", str(d2)]) == 2
    assert _writes(fake_gh) == []


def test_create_batch_dry_run_sends_nothing(fake_gh, tmp_path, capsys):
    d = _batch_dir(tmp_path)
    assert issues.main(["create-batch", str(d), "--dry-run"]) == 0
    assert _writes(fake_gh) == []
    assert "Base thing" in capsys.readouterr().out


def test_script_runs_as_a_process_and_refuses(fake_gh, tmp_path, monkeypatch):
    import subprocess

    f = _write(tmp_path, "p.md", f"see {PLANTED_PATH}\n")
    done = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "issues.py"), "create", "--title", "ok",
         "--body-file", str(f)], capture_output=True, text=True, env=os.environ.copy())
    assert done.returncode == 2
    assert _writes(fake_gh) == []


WRITING = [
    lambda f: ["create", "--title", "ok", "--body-file", f],
    lambda f: ["comment", "4", "--body-file", f],
    lambda f: ["close", "4", "--comment-file", f],
    lambda f: ["close", "4"],
    lambda f: ["create-batch", str(Path(f).parent / "batch")],
]


@pytest.mark.parametrize("index", range(len(WRITING)))
def test_unset_forbidden_file_refuses_every_writing_command(fake_gh, tmp_path, monkeypatch, capsys, index):
    """Absent pattern file means the identity check was not done, which is not a pass."""
    _batch_dir(tmp_path)
    f = _write(tmp_path, "f.md", "clean")
    monkeypatch.delenv("BOARDPROOF_FORBIDDEN_FILE")
    assert issues.main(WRITING[index](str(f))) == 2
    assert "BOARDPROOF_FORBIDDEN_FILE" in capsys.readouterr().err
    assert _writes(fake_gh) == []


@pytest.mark.parametrize("how", ["flag", "env"])
def test_explicit_opt_out_warns_loudly_and_proceeds(fake_gh, tmp_path, monkeypatch, capsys, how):
    monkeypatch.delenv("BOARDPROOF_FORBIDDEN_FILE")
    f = _write(tmp_path, "f.md", "clean")
    argv = ["create", "--title", "ok", "--body-file", str(f)]
    if how == "flag":
        argv.insert(0, "--no-identity-check")
    else:
        monkeypatch.setenv("BOARDPROOF_ALLOW_UNCHECKED", "1")
    assert issues.main(argv) == 0
    out = capsys.readouterr()
    assert "WARNING" in out.err and "identity check" in out.err
    assert "WARNING" in out.out
    assert any(c["argv"][:2] == ["issue", "create"] for c in fake_gh())


def test_opt_out_still_applies_the_builtin_patterns(fake_gh, tmp_path, monkeypatch):
    monkeypatch.delenv("BOARDPROOF_FORBIDDEN_FILE")
    f = _write(tmp_path, "p.md", f"see {PLANTED_PATH}\n")
    assert issues.main(["--no-identity-check", "create", "--title", "ok", "--body-file", str(f)]) == 2
    assert _writes(fake_gh) == []


def test_batch_with_a_name_only_in_a_title_or_only_in_a_label_is_refused(fake_gh, tmp_path, monkeypatch):
    _forbidden(tmp_path, monkeypatch)
    for field, old, new in (("title", "Title: Base thing", f"Title: Base {SYNTHETIC_NAME}"),
                            ("label", "Labels: build\n\nFormerly build ticket 112",
                             f"Labels: build, {SYNTHETIC_NAME}\n\nFormerly build ticket 112")):
        d = tmp_path / ("b-" + field)
        d.mkdir()
        _write(d, "a.md", BATCH_B.replace(old, new))
        assert BATCH_B.replace(old, new) != BATCH_B, field
        assert issues.main(["create-batch", str(d)]) == 2, field
        assert _writes(fake_gh) == [], field


def test_comment_close_and_list_check_the_repository_first(fake_gh, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_GH_REPO_DOWN", "1")
    f = _write(tmp_path, "f.md", "clean")
    for argv in (["comment", "4", "--body-file", str(f)], ["close", "4"],
                 ["close", "4", "--comment-file", str(f)]):
        assert issues.main(argv) == 3, argv
        assert "repository is not reachable" in capsys.readouterr().err
    assert [c for c in fake_gh() if c["argv"][0] == "issue"] == []


@pytest.mark.parametrize("bad", ["abc", "3x", "#", "3 --label x", "-1", "#a"])
def test_blocked_by_must_be_a_number(fake_gh, tmp_path, bad):
    f = _write(tmp_path, "f.md", "clean")
    assert issues.main(["create", "--title", "ok", "--body-file", str(f), "--blocked-by", bad]) == 2
    assert _writes(fake_gh) == []


def test_blocked_by_accepts_digits_and_hash_digits(fake_gh, tmp_path):
    f = _write(tmp_path, "f.md", "clean")
    assert issues.main(["create", "--title", "ok", "--body-file", str(f),
                        "--blocked-by", "#4", "--blocked-by", "5"]) == 0


@pytest.mark.parametrize("argv", [["show", "1"], ["list"], ["label-sync"], ["comment", "1", "--body-file", "x"],
                                  ["close", "1"], ["create-batch", "x"]])
def test_no_repository_given_is_refused_not_guessed_from_the_directory(fake_gh, monkeypatch, capsys, argv):
    monkeypatch.delenv("BOARDPROOF_REPO")
    assert issues.main(argv) == 2
    assert "--repo" in capsys.readouterr().err
    assert fake_gh() == []


def test_create_batch_checks_the_labels_before_the_first_write(fake_gh, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_GH_LABELS", json.dumps([{"name": "build"}, {"name": "map"}]))
    d = _batch_dir(tmp_path)
    assert issues.main(["create-batch", str(d)]) == 2
    err = capsys.readouterr().err
    assert "critical" not in err or "label-sync" in err
    assert "label-sync" in err
    assert [c for c in fake_gh() if c["argv"][:2] == ["issue", "create"]] == []


def test_prose_after_blocked_by_is_not_a_dependency_but_an_unknown_id_is(fake_gh, tmp_path):
    d = tmp_path / "prose"
    d.mkdir()
    _write(d, "a.md", BATCH_B.replace("Formerly build ticket 112.",
                                      "Blocked by every other thing, and blocked by nothing."))
    assert issues.main(["create-batch", str(d), "--dry-run"]) == 0
    d2 = tmp_path / "unknown"
    d2.mkdir()
    _write(d2, "a.md", BATCH_B.replace("Formerly build ticket 112.", "Blocked by build-999"))
    assert issues.main(["create-batch", str(d2), "--dry-run"]) == 2
    assert _writes(fake_gh) == []
