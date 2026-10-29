"""Build ticket 45: the local batch gate's verdict on canned runs (playbook A9).

`scripts/batch_gate.py` gets a fake runner, so these tests feed it the cases A9 lists: a timeout,
a known-red failure, a captured ERROR log line, a parametrised id with " - ", exit codes 2/4/5/137,
a lint failure, and missing output; plus this project's worktree checks (`kicad_mcp.__file__` and
pytest's `rootdir:` must name the worktree).
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("batch_gate", ROOT / "scripts" / "batch_gate.py")
gate = importlib.util.module_from_spec(_spec)
sys.modules["batch_gate"] = gate  # dataclasses resolve string annotations through sys.modules
_spec.loader.exec_module(gate)

KNOWN = (
    "tests/test_routing_toolset.py::TestRealRetryLoopOnGoldenPathBoard::"
    "test_propose_route_with_retry_on_golden_path_board"
)
KNOWN_TEXT = f"# comment\n\n{KNOWN}  # build ticket 46: Freerouting overlap\n"


KNOWN_SKIPS = '''
[[skip]]
file = "tests/test_ipc.py"
reason = "no running KiCad instance"
max = 3
why = "needs a running KiCad with the IPC API"
'''
IPC_SKIP = ("tests\\test_ipc.py", "no running KiCad instance with the IPC API enabled was reachable")
NETWORK = 3  # network-marked tests, deselected by -m "not network"
TRACKED = ["tests/test_a.py", "tests/test_b.py"]
ALL_COLLECTED = "tests/test_a.py::test_1\ntests/test_b.py::TestB::test_2[x - y]\n\n403 tests collected in 2.00s\n"
PYPROJECT = (  # the reviewed [tool.pytest.ini_options], written out as TOML
    '[project]\nname = "x"\n[tool.pytest.ini_options]\n'
    + "".join(f"{k} = {json.dumps(v)}\n" for k, v in gate.EXPECTED_PYTEST_CONFIG.items())
)


def pytest_output(root: Path, *, failed=(), errors=(), passed=400, skipped=(), deselected=NETWORK,
                  collected=None, rootdir=None, configfile="pyproject.toml", extra="", ran=None):
    """Canned pytest output. ``skipped``: (file, reason, count) triples."""
    n_skipped = sum(n for _, _, n in skipped)
    selected = passed + len(failed) + n_skipped
    total = collected if collected is not None else selected + deselected
    lines = [
        "============================= test session starts =============================",
        "platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.7.0",
        f"rootdir: {rootdir if rootdir is not None else root}",
    ]
    if configfile is not None:
        lines.append(f"configfile: {configfile}")
    lines += [
        f"collected {total} items" + (f" / {deselected} deselected / {selected} selected" if deselected else ""),
        "",
        "tests/test_a.py ....F.... [100%]",
        extra,
    ]
    if failed or errors or skipped:
        lines.append("=========================== short test summary info ===========================")
        lines += [f"FAILED {i} - AssertionError: boom" for i in failed]
        lines += [f"ERROR {i} - RuntimeError: setup" for i in errors]
        lines += [f"SKIPPED [{n}] {f}:12: {r}" for f, r, n in skipped]
    parts = [f"{len(failed)} failed"] if failed else []
    parts.append(f"{passed if ran is None else ran} passed")
    if n_skipped:
        parts.append(f"{n_skipped} skipped")
    if deselected:
        parts.append(f"{deselected} deselected")
    if errors:
        parts.append(f"{len(errors)} error" + ("s" if len(errors) > 1 else ""))
    lines.append(f"========== {', '.join(parts)} in 431.20s (0:07:11) ==========")
    return "\n".join(lines) + "\n"


class FakeRunner:
    def __init__(self, root: Path, *, suite_rc=0, suite_out=None, lint_rc=0, import_path=None,
                 rerun_rc=1, import_rc=0, network=NETWORK, collect_rc=0, collect_out=None,
                 all_out=None, all_rc=0):
        self.root = root
        self.suite_rc, self.suite_out = suite_rc, suite_out
        self.lint_rc, self.rerun_rc, self.import_rc = lint_rc, rerun_rc, import_rc
        self.network, self.collect_rc, self.collect_out = network, collect_rc, collect_out
        self.all_out = all_out if all_out is not None else ALL_COLLECTED
        self.all_rc = all_rc
        self.import_path = import_path if import_path is not None else str(root / "src" / "kicad_mcp" / "__init__.py")
        self.calls: list[list[str]] = []

    def __call__(self, cmd, env, cwd, timeout, out_path):
        self.calls.append(cmd)
        assert env["KICAD_DEMOS_REQUIRED"] == "1"
        assert env["KICAD_ROUTING_TOOLS_REQUIRED"] == "1"  # KRT tests fail, never skip (build ticket 53)
        assert env["NGSPICE_REQUIRED"] == "1"  # live ngspice tests fail, never skip (spice-simulation 02)
        assert env["PYTHONPATH"] == str(self.root / "src")
        assert Path(cwd) == self.root
        if cmd[1] == "-c":
            Path(out_path).write_text(self.import_path + "\n", encoding="utf-8")
            return self.import_rc
        if cmd[-1].endswith("lint_gate.py"):
            Path(out_path).write_text("lint: OK\n" if self.lint_rc == 0 else "lint: FAILED\n", encoding="utf-8")
            return self.lint_rc
        if "--collect-only" in cmd and "network" not in cmd:
            assert "-m" not in cmd[2:], "the full collection takes no marker filter"
            Path(out_path).write_text(self.all_out, encoding="utf-8")
            return self.all_rc
        if "--collect-only" in cmd:
            assert cmd[cmd.index("-m", 2) + 1] == "network"
            out = self.collect_out
            if out is None:
                out = f"{self.network}/403 tests collected (400 deselected) in 2.10s\n"
            Path(out_path).write_text(out, encoding="utf-8")
            return self.collect_rc
        if "not network" in cmd:
            assert "-q" not in cmd, "-q hides the rootdir line"
            assert "-rfEs" in cmd, "skips must be listed"
            Path(out_path).write_text(self.suite_out if self.suite_out is not None else pytest_output(self.root),
                                      encoding="utf-8")
            return self.suite_rc
        Path(out_path).write_text("rerun\n", encoding="utf-8")  # a rerun of one id
        return self.rerun_rc


@pytest.fixture
def root(tmp_path, monkeypatch):
    r = tmp_path / "wt-1"
    (r / "src" / "kicad_mcp").mkdir(parents=True)
    krt = tmp_path / "krt"
    krt.mkdir()
    monkeypatch.setenv("KICAD_ROUTING_TOOLS_PATH", str(krt))
    ngspice = tmp_path / "ngspice_con.exe"
    ngspice.write_text("", encoding="utf-8")
    monkeypatch.setenv("NGSPICE_PATH", str(ngspice))
    # The publishability check has its own tests (tests/test_gate_publishable.py); here it passes.
    monkeypatch.setattr(gate, "check_publishable_tracked", lambda *a, **kw: [])
    return r


def run(root, runner, *, dirty=(), known=KNOWN_TEXT, skips=KNOWN_SKIPS, tracked=TRACKED, pyproject=PYPROJECT):
    log = root.parent / "logs"
    log.mkdir(exist_ok=True)
    said: list[str] = []
    res = gate.run_gate(root=root, sha="abcdef1234567", dirty=list(dirty), python="py",
                        known_red_text=known, known_skips_text=skips, tracked_tests=tracked,
                        pyproject_text=pyproject, runner=runner, log_dir=log,
                        say=said.append)
    return res, "\n".join(said)


def test_green_run(root):
    res, said = run(root, FakeRunner(root))
    assert res.green, res.reasons
    assert "collected: collected 403 items / 3 deselected / 400 selected" in said
    assert "(this worktree)" in said


def test_known_red_failure_alone_stays_green(root):
    out = pytest_output(root, failed=[KNOWN])
    res, said = run(root, FakeRunner(root, suite_rc=1, suite_out=out))
    assert res.green, res.reasons
    assert f"{KNOWN}: failing (known)" in said


def test_new_failure_is_red_and_printed_by_id(root):
    out = pytest_output(root, failed=[KNOWN, "tests/test_b.py::test_new"])
    res, said = run(root, FakeRunner(root, suite_rc=1, suite_out=out))
    assert not res.green
    assert res.new == ["tests/test_b.py::test_new"]
    assert "  tests/test_b.py::test_new" in said


def test_parametrised_id_with_dash_is_kept_whole(root):
    pid = "tests/test_p.py::test_p[x - y]"
    out = pytest_output(root, failed=[pid])
    res, _ = run(root, FakeRunner(root, suite_rc=1, suite_out=out))
    assert res.new == [pid]


def test_collection_error_without_double_colon_is_an_id(root):
    out = pytest_output(root, errors=["tests/test_broken.py"])
    res, _ = run(root, FakeRunner(root, suite_rc=1, suite_out=out))
    assert res.new == ["tests/test_broken.py"]
    assert not res.green


def test_captured_error_log_line_is_not_an_id(root):
    extra = "----- Captured log call -----\nERROR tests/test_x.py::looks_like_an_id - from a log"
    out = pytest_output(root, extra=extra)
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert res.green, res.reasons


def test_flaky_candidate_is_still_red(root):
    out = pytest_output(root, failed=["tests/test_b.py::test_flaky"])
    res, said = run(root, FakeRunner(root, suite_rc=1, suite_out=out, rerun_rc=0))
    assert res.flaky == ["tests/test_b.py::test_flaky"]
    assert not res.green
    assert "flaky candidate" in said


def test_timeout_is_red(root):
    res, _ = run(root, FakeRunner(root, suite_rc=None, suite_out="partial output\n"))
    assert "suite timed out" in res.reasons


@pytest.mark.parametrize("rc", [2, 3, 4, 5, 137, -9])
def test_other_exit_codes_are_red(root, rc):
    res, _ = run(root, FakeRunner(root, suite_rc=rc))
    assert f"pytest exit code {rc}" in res.reasons


def test_exit_1_without_ids_is_red(root):
    res, _ = run(root, FakeRunner(root, suite_rc=1))
    assert "pytest exit code 1 without failure ids" in res.reasons


def test_failure_lines_must_add_up_to_the_summary(root):
    out = pytest_output(root, failed=["tests/test_b.py::test_x"]).replace("1 failed", "2 failed")
    res, _ = run(root, FakeRunner(root, suite_rc=1, suite_out=out))
    assert any("summary says 2" in r for r in res.reasons)


def test_missing_output_is_red(root):
    res, _ = run(root, FakeRunner(root, suite_rc=0, suite_out=""))
    assert {"no rootdir line", "no collected line", "no final summary line"} <= set(res.reasons)


def test_lint_failure_is_red(root):
    res, _ = run(root, FakeRunner(root, lint_rc=1))
    assert res.reasons == ["lint"]


def test_rootdir_of_another_checkout_is_red(root):
    out = pytest_output(root, rootdir=root.parent / "main-checkout")
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert "rootdir is not this worktree" in res.reasons


def test_old_style_rootdir_line_is_parsed(root):
    out = pytest_output(root, rootdir=f"{root}, configfile: pyproject.toml")
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert res.green, res.reasons


def test_kicad_mcp_from_the_main_checkout_stops_the_run(root):
    other = str(root.parent / "main" / "src" / "kicad_mcp" / "__init__.py")
    runner = FakeRunner(root, import_path=other)
    res, said = run(root, runner)
    assert "kicad_mcp is not imported from this worktree's src" in res.reasons
    assert not any("not network" in c for c in runner.calls), "the suite must not run"


def test_sibling_directory_with_a_shared_prefix_is_not_inside(root):
    sibling = root.parent / (root.name + "-other") / "src" / "kicad_mcp" / "__init__.py"
    res, _ = run(root, FakeRunner(root, import_path=str(sibling)))
    assert not res.green


def test_dirty_tree_cannot_be_green(root):
    res, said = run(root, FakeRunner(root), dirty=[" M src/kicad_mcp/x.py"])
    assert res.reasons == ["working tree not clean (1 paths)"]
    assert "src/kicad_mcp/x.py" in said


def test_known_red_entry_without_ticket_is_red(root):
    res, _ = run(root, FakeRunner(root), known=f"{KNOWN}  # flaky, someone look at it\n")
    assert "known-red file invalid" in res.reasons


def test_missing_known_red_file_is_red(root):
    res, _ = run(root, FakeRunner(root), known=None)
    assert "known-red file missing" in res.reasons


def test_committed_known_red_file_is_valid_and_matches_fork_p11():
    kr = gate.read_known_red((ROOT / "scripts" / "known-red.txt").read_text(encoding="utf-8"))
    assert kr.problems == []
    assert kr.ids == []  # fork P11: build ticket 46 removed the only entry (the golden-path retry test)


def test_default_runner_reports_timeout_and_exit_code(tmp_path):
    out = tmp_path / "o.txt"
    rc = gate.default_runner([sys.executable, "-c", "print('hi'); raise SystemExit(3)"], dict(__import__("os").environ),
                             tmp_path, 60, out)
    assert rc == 3 and "hi" in out.read_text()
    rc = gate.default_runner([sys.executable, "-c", "import time; time.sleep(30)"], dict(__import__("os").environ),
                             tmp_path, 1, out)
    assert rc is None


def test_default_runner_enforces_the_timeout_on_the_wall_clock(tmp_path, monkeypatch):
    # Fix round 3 (delta review 2, BLOCKS F1): a 4200 s suite timeout did not fire in a run that
    # took 5331 s, because the machine was in standby for 3770 s and Popen.wait(timeout) does not
    # count time in low-power states on Windows. The runner measures its deadline with time.time()
    # and waits in short slices. Here wall time jumps 10000 s while the child is still running:
    # the runner must kill the tree and return None well before the child's 30 s sleep ends.
    import types
    killed = []
    real_kill = gate._kill_tree

    def kill_and_record(proc):
        killed.append(proc.pid)
        real_kill(proc)
        killed.append(proc.poll())

    monkeypatch.setattr(gate, "_kill_tree", kill_and_record)
    calls = {"n": 0}
    real_time = __import__("time").time

    def clock():
        calls["n"] += 1
        return 1000.0 if calls["n"] == 1 else 1000.0 + 10000.0  # the jump (standby)

    monkeypatch.setattr(gate, "time", types.SimpleNamespace(time=clock))
    started = real_time()
    rc = gate.default_runner([sys.executable, "-c", "import time; time.sleep(30)"],
                             dict(__import__("os").environ), tmp_path, 4200, tmp_path / "o.txt")
    assert rc is None
    assert real_time() - started < 20
    assert len(killed) == 2 and killed[1] is not None  # the tree was killed and the child is gone


def test_default_runner_returns_the_exit_code_of_a_process_finishing_before_the_deadline(tmp_path, monkeypatch):
    import types
    monkeypatch.setattr(gate, "time", types.SimpleNamespace(time=lambda: 1000.0))  # wall time stands still
    rc = gate.default_runner([sys.executable, "-c", "raise SystemExit(5)"], dict(__import__("os").environ),
                             tmp_path, 4200, tmp_path / "o.txt")
    assert rc == 5


# --------------------------------------------------------------------------- review round 1
# The gate reported GREEN for runs that were mostly skipped or deselected.


def test_everything_skipped_is_red(root):
    out = pytest_output(root, passed=0, skipped=[("tests\\test_a.py", "kicad-cli not found", 1514)])
    res, said = run(root, FakeRunner(root, suite_out=out))
    assert not res.green
    assert "nothing passed" in res.reasons
    assert any("unknown skip" in r for r in res.reasons)


def test_unknown_skip_is_red_and_printed(root):
    out = pytest_output(root, skipped=[("tests\\test_b.py", "demo not found", 2)])
    res, said = run(root, FakeRunner(root, suite_out=out))
    assert any("unknown skip" in r for r in res.reasons)
    assert "tests/test_b.py: demo not found" in said


def test_known_skip_within_its_maximum_is_green(root):
    out = pytest_output(root, skipped=[(*IPC_SKIP, 3)])
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert res.green, res.reasons


def test_known_skip_above_its_maximum_is_red(root):
    out = pytest_output(root, skipped=[(*IPC_SKIP, 4)])
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert any("more skips than allowed" in r for r in res.reasons)


def test_skip_lines_must_add_up_to_the_summary(root):
    out = pytest_output(root, skipped=[(*IPC_SKIP, 2)]).replace("2 skipped", "5 skipped")
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert any("skip lines" in r for r in res.reasons)


def test_deselected_count_must_equal_the_network_marked_count(root):
    out = pytest_output(root, passed=117, deselected=1400)
    res, said = run(root, FakeRunner(root, suite_out=out))
    assert any("deselected" in r for r in res.reasons)


def test_unreadable_network_collection_is_red(root):
    res, _ = run(root, FakeRunner(root, collect_out="boom\n", collect_rc=4))
    assert any("network" in r for r in res.reasons)


def test_not_every_selected_test_ran_is_red(root):
    # e.g. -x in addopts: the run stops early but exits 1 with one failure.
    out = pytest_output(root, failed=[KNOWN], ran=10)
    res, _ = run(root, FakeRunner(root, suite_rc=1, suite_out=out))
    assert any("selected" in r and "ran" in r for r in res.reasons)


@pytest.mark.parametrize("configfile", [None, "tox.ini", "pytest.ini"])
def test_configfile_must_be_pyproject(root, configfile):
    out = pytest_output(root, configfile=configfile)
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert any("configfile" in r for r in res.reasons)


def test_unset_routing_tools_path_is_red(root, monkeypatch):
    monkeypatch.delenv("KICAD_ROUTING_TOOLS_PATH")
    res, _ = run(root, FakeRunner(root))
    assert any("KICAD_ROUTING_TOOLS_PATH" in r for r in res.reasons)


def test_routing_tools_path_that_is_no_directory_is_red(root, monkeypatch, tmp_path):
    monkeypatch.setenv("KICAD_ROUTING_TOOLS_PATH", str(tmp_path / "missing"))
    res, _ = run(root, FakeRunner(root))
    assert any("KICAD_ROUTING_TOOLS_PATH" in r for r in res.reasons)


def test_unset_ngspice_path_is_red(root, monkeypatch):
    monkeypatch.delenv("NGSPICE_PATH")
    res, _ = run(root, FakeRunner(root))
    assert any("NGSPICE_PATH" in r for r in res.reasons)


def test_ngspice_path_that_is_no_file_is_red(root, monkeypatch, tmp_path):
    monkeypatch.setenv("NGSPICE_PATH", str(tmp_path / "missing.exe"))
    res, _ = run(root, FakeRunner(root))
    assert any("NGSPICE_PATH" in r for r in res.reasons)


@pytest.mark.parametrize(
    "text",
    [
        '[[skip]]\nfile = "tests/test_ipc.py"\nreason = "x"\nmax = 1\n',  # no why
        '[[skip]]\nfile = "tests/test_ipc.py"\nreason = ""\nmax = 1\nwhy = "y"\n',  # empty reason
        '[[skip]]\nfile = "tests/test_ipc.py"\nreason = "x"\nmax = 0\nwhy = "y"\n',  # max < 1
        "not toml [",
    ],
)
def test_invalid_known_skips_file_is_red(root, text):
    res, _ = run(root, FakeRunner(root), skips=text)
    assert "known-skips file invalid" in res.reasons


def test_missing_known_skips_file_is_red(root):
    res, _ = run(root, FakeRunner(root), skips=None)
    assert "known-skips file missing" in res.reasons


def test_rc_0_with_failure_ids_is_red(root):
    out = pytest_output(root, failed=["tests/test_b.py::test_x"])
    res, _ = run(root, FakeRunner(root, suite_rc=0, suite_out=out))
    assert "pytest exit code 0 with failure ids" in res.reasons


def test_committed_known_skips_file_is_valid():
    ks = gate.read_known_skips((ROOT / "scripts" / "known-skips.toml").read_text(encoding="utf-8"))
    assert ks.problems == []
    assert ks.entries


# --------------------------------------------------------------------------- delta review 1


def test_new_skip_reason_in_an_allowed_file_is_red(root):
    out = pytest_output(root, skipped=[("tests\\test_ipc.py", "a brand-new reason", 1)])
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert any("unknown skip" in r for r in res.reasons)


def test_allowed_reason_in_another_file_is_red(root):
    out = pytest_output(root, skipped=[("tests\\test_other.py", IPC_SKIP[1], 1)])
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert any("unknown skip" in r for r in res.reasons)


def test_module_level_skip_is_red_even_with_a_known_reason(root):
    # pytest.skip(allow_module_level=True): the tests of that file are never selected, but the
    # summary counts one skip, so more tests "ran" than were selected.
    out = pytest_output(root, skipped=[(*IPC_SKIP, 1)]).replace(" / 401 selected", " / 1 skipped / 400 selected")
    res, _ = run(root, FakeRunner(root, suite_out=out))
    assert res.reasons == ["400 tests selected, but 401 ran (0 errors)"]


def test_pytest_config_other_than_the_reviewed_one_is_red(root):
    for extra in ('addopts = "--ignore=tests/test_b.py"\n', 'python_files = ["check_*.py"]\n',
                  'norecursedirs = ["tests"]\n'):
        res, said = run(root, FakeRunner(root), pyproject=PYPROJECT + extra)
        assert any("[tool.pytest.ini_options]" in r for r in res.reasons), extra


def test_tracked_test_file_that_is_not_collected_is_red(root):
    # e.g. collect_ignore in a conftest.py, or --ignore in addopts
    res, said = run(root, FakeRunner(root), tracked=TRACKED + ["tests/test_c.py"])
    assert any("not collected" in r for r in res.reasons)
    assert "tests/test_c.py" in said


def test_module_skipped_file_counts_as_collected_for_the_file_check(root):
    all_out = ALL_COLLECTED.replace("\n\n403", "\n\n=== short test summary info ===\nSKIPPED [1] tests\\test_c.py:2: off\n403")
    res, _ = run(root, FakeRunner(root, all_out=all_out), tracked=TRACKED + ["tests/test_c.py"])
    assert not any("not collected" in r for r in res.reasons)


def test_unreadable_full_collection_is_red(root):
    res, _ = run(root, FakeRunner(root, all_out="boom\n", all_rc=2))
    assert any("collection" in r for r in res.reasons)


def test_unknown_tracked_list_is_red(root):
    res, _ = run(root, FakeRunner(root), tracked=None)
    assert any("tracked test files" in r for r in res.reasons)


def test_committed_pyproject_has_exactly_the_reviewed_pytest_config():
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["pytest"]["ini_options"] == gate.EXPECTED_PYTEST_CONFIG


# --------------------------------------------------------------------------- build ticket 70
# main() in a real git repo: the tree is read at the start and again at the end. A run during which
# HEAD or the working tree changes tested something other than the start sha, so it is RED.


def _git(root, *args):
    return subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(root):
    """A committed, clean git repo shaped like this project, for main()."""
    (root / "src" / "kicad_mcp" / "__init__.py").write_text("", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "known-red.txt").write_text(KNOWN_TEXT, encoding="utf-8")
    (root / "scripts" / "known-skips.toml").write_text(KNOWN_SKIPS, encoding="utf-8")
    (root / "tests").mkdir()
    for f in TRACKED:
        (root / f).write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "start")
    return root


def run_main(repo, capsys, *argv, during=None, at="not network", runner=None):
    """main() on ``repo`` with a FakeRunner; ``during()`` runs when the step whose command contains
    ``at`` starts (by default the suite). Returns (exit code, printed lines)."""
    fake = runner or FakeRunner(repo)

    def stub(cmd, env, cwd, timeout, out):
        if during is not None and any(at in c for c in cmd):
            during()
        return fake(cmd, env, cwd, timeout, out)

    capsys.readouterr()
    rc = gate.main(["--python", "py", "--log-dir", str(repo.parent / "logs"), *argv], root=repo, runner=stub)
    return rc, capsys.readouterr().out.splitlines()


def test_unchanged_tree_stays_green_through_main(repo, capsys):
    rc, lines = run_main(repo, capsys)
    start = _git(repo, "rev-parse", "HEAD")
    assert rc == 0, lines
    assert lines[-1] == f"BATCH GATE {start[:7]}: GREEN"


def test_commit_during_the_run_is_red_and_names_both_shas(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")

    def edit_and_commit():
        (repo / "tests" / "test_a.py").write_text("def test_x():\n    assert 0\n", encoding="utf-8")
        _git(repo, "commit", "-q", "-am", "edit during the run")

    rc, lines = run_main(repo, capsys, during=edit_and_commit)
    end = _git(repo, "rev-parse", "HEAD")
    assert rc == 1
    assert lines[-1].startswith(f"BATCH GATE {start[:7]}: RED (")
    assert f"HEAD changed during the run ({start[:7]} -> {end[:7]})" in lines[-1]


def test_uncommitted_edit_during_the_run_is_red_and_names_the_path(repo, capsys):
    def edit():
        (repo / "tests" / "test_b.py").write_text("def test_x():\n    assert 0\n", encoding="utf-8")

    rc, lines = run_main(repo, capsys, during=edit)
    assert rc == 1
    assert "working tree changed during the run (1 status lines: M tests/test_b.py)" in lines[-1]
    assert "  end only:    M tests/test_b.py" in lines


def test_untracked_file_is_seen_even_when_git_config_hides_untracked_files(repo, capsys):
    _git(repo, "config", "status.showUntrackedFiles", "no")

    def add_file():
        (repo / "tests" / "test_new.py").write_text("def test_x():\n    pass\n", encoding="utf-8")

    rc, lines = run_main(repo, capsys, during=add_file)
    assert rc == 1
    assert "?? tests/test_new.py" in lines[-1]


def test_untracked_file_at_the_start_is_red_even_when_git_config_hides_it(repo, capsys):
    _git(repo, "config", "status.showUntrackedFiles", "no")
    (repo / "tests" / "conftest.py").write_text("collect_ignore = ['test_b.py']\n", encoding="utf-8")
    rc, lines = run_main(repo, capsys)
    assert rc == 1
    assert "working tree not clean (1 paths)" in lines[-1]


def test_change_during_the_lint_step_is_red(repo, capsys):
    # the recheck comes after the last step, not after pytest
    def edit():
        (repo / "src" / "kicad_mcp" / "__init__.py").write_text("x = 1\n", encoding="utf-8")

    rc, lines = run_main(repo, capsys, during=edit, at="lint_gate.py")
    assert rc == 1
    assert "working tree changed during the run" in lines[-1]


def test_checkout_of_another_commit_during_the_run_is_red(repo, capsys):
    start = _git(repo, "rev-parse", "HEAD")
    (repo / "tests" / "test_a.py").write_text("def test_x():\n    assert 1\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "second")
    second = _git(repo, "rev-parse", "HEAD")

    rc, lines = run_main(repo, capsys, during=lambda: _git(repo, "checkout", "-q", start))
    assert rc == 1
    assert f"HEAD changed during the run ({second[:7]} -> {start[:7]})" in lines[-1]


def test_recheck_that_cannot_run_is_red(repo, capsys):
    # git fails at the end (here: the repository is gone); the gate must not assume "unchanged"
    rc, lines = run_main(repo, capsys, during=lambda: (repo / ".git").rename(repo / "git-moved"),
                         at="lint_gate.py")
    assert rc == 1
    assert "the tree could not be rechecked at the end (git failed)" in lines[-1]
    assert lines[-1].startswith("BATCH GATE ") and ": RED (" in lines[-1]


def test_two_runs_on_the_same_sha_keep_separate_logs(repo, capsys):
    # Gates of several worktrees share the temp log directory; a sha-only name let two runs on the
    # same sha overwrite each other's log and pytest output (ticket 66 review, added to ticket 70).
    logs = []
    for _ in range(2):
        rc, lines = run_main(repo, capsys)
        assert rc == 0, lines
        logs.append(Path(next(line for line in lines if line.startswith("log: "))[5:]))
    assert logs[0] != logs[1]
    for log in logs:
        assert log.is_file()
        assert log.read_text(encoding="utf-8").splitlines()[-1].endswith(": GREEN")
        # sha7, start date and time, and the pid: unique across concurrent gate processes
        sha7 = _git(repo, "rev-parse", "HEAD")[:7]
        assert re.fullmatch(rf"{sha7}-\d{{8}}-\d{{6}}-{os.getpid()}(-\d+)?\.log", log.name), log.name
    pytest_outs = sorted((repo.parent / "logs").glob("*.pytest.out"))
    assert len(pytest_outs) == 2, pytest_outs


# The suite took 460-870 s on 2026-10-20 with one gate at a time. With several gates at once the
# shared log directory holds exit-0 runs of 1184 s (3c8fe14), 1234 s (7ddbe68), 1350 s (d498916)
# and 1358 s (dead735.log, the slowest; ticket 70 review round 1). A 1500 s timeout left 1.1x.
# The timeout is about 3x the slowest measured run (fork P10).
# On 2026-10-21, with up to six gates at once and about 2500-2720 tests, the log directory held
# exit-0 runs of 1506 s (c19edc6), 1573 s (0da7605), 1635 s (aae8de0), 1658 s (a7d5f1c), 1674 s
# (0e7b173) and 1801 s (18f281b), past the 1358 s of dead735 that the 4200 s timeout was set from
# (build ticket 79 review, build ticket 88). Later that day, with more gates at once and about
# 2850 tests, runs took 2240 s (d78da21), 2378 s (d16dff3), 2461 s (0a42080), 2512 s (9928412)
# and 2545 s (374c526, the slowest; "pytest finished in 2545 s"): the 1801 s figure was outrun
# while ticket 88 was under review (review round 1, BLOCKS).
SLOWEST_MEASURED_SUITE_S = 2545


def test_suite_timeout_has_about_3x_headroom_over_the_slowest_measured_suite():
    assert gate.SUITE_MEASURED_S >= SLOWEST_MEASURED_SUITE_S
    assert gate.SUITE_TIMEOUT_S >= 3 * SLOWEST_MEASURED_SUITE_S - 60
    assert gate.SUITE_TIMEOUT_S >= 3 * gate.SUITE_MEASURED_S - 60


EXCLUSION_CLAUSE = ("is excluded from the timeout's basis: a reviewer's targeted tests ran at the same time"
                    " and the machine was in standby for 3770 s of it")


def test_the_timeout_basis_names_the_excluded_5331_s_run_everywhere():
    # Fix round 2 (delta review 1, BLOCKS F1): the shared log directory holds c1c8f1b, "pytest
    # finished in 5331 s" (ticket 36's gate, 2026-10-21, a reviewer's targeted tests ran at the
    # same time). The 3x basis is the slowest run with only other gates running (2545 s); that run
    # is excluded from it, so every text that gives the basis must name the exclusion.
    script_text = (ROOT / "scripts" / "batch_gate.py").read_text(encoding="utf-8")
    script = " ".join(re.sub(r"(?m)^\s*#", "", script_text).split())
    playbook = _prose(ROOT / "docs" / "playbook" / "PROJECT-KICAD-MCP-PLAYBOOK.md")
    p11 = playbook.split("**Project rule P11:", 1)[1].split("**Project rule P12", 1)[0]
    for name, text in (("batch_gate.py", script), ("docs/TESTING.md", _prose(ROOT / "docs/TESTING.md")),
                       ("P11", p11)):
        assert "5331 s" in text and "c1c8f1b" in text, name
        # The exclusion clause itself, once per text, with its reason and what happened to the run.
        assert text.count(EXCLUSION_CLAUSE) == 1, name
        assert text.count("exited 0 and ended GREEN under the 4200 s timeout then in force") == 1, name
        assert text.count("enforces the timeout on the wall clock") == 1, name
        # The wrong fail-safe prediction about that run is gone.
        for wrong in ("never GREEN", "can end RED on the timeout", "fail-safe"):
            assert wrong not in text, (name, wrong)


class TimeoutRecorder(FakeRunner):
    def __init__(self, root, **kw):
        super().__init__(root, **kw)
        self.timeouts: list[tuple[str, float]] = []

    def __call__(self, cmd, env, cwd, timeout, out_path):
        step = ("suite" if "not network" in cmd else "lint" if cmd[-1].endswith("lint_gate.py")
                else "import" if cmd[1] == "-c" else "collect" if "--collect-only" in cmd else "rerun")
        self.timeouts.append((step, timeout))
        return super().__call__(cmd, env, cwd, timeout, out_path)


def test_main_gives_the_suite_the_default_timeout_or_the_one_asked_for(repo, capsys):
    rec = TimeoutRecorder(repo)
    run_main(repo, capsys, runner=rec)
    assert ("suite", gate.SUITE_TIMEOUT_S) in rec.timeouts
    rec = TimeoutRecorder(repo)
    run_main(repo, capsys, "--timeout", "4000", runner=rec)
    assert ("suite", 4000.0) in rec.timeouts


@pytest.mark.parametrize("value", ["0", "-5", "nan", "inf", "-inf", "ten"])
def test_timeout_that_is_not_a_positive_finite_number_is_refused(repo, capsys, value):
    rec = TimeoutRecorder(repo)
    with pytest.raises(SystemExit) as exc:
        run_main(repo, capsys, "--timeout", value, runner=rec)
    assert exc.value.code not in (0, None)
    assert rec.timeouts == [], "nothing may run"


def test_suite_timeout_in_main_is_red(repo, capsys):
    rc, lines = run_main(repo, capsys, runner=FakeRunner(repo, suite_rc=None, suite_out="partial\n"))
    assert rc == 1
    assert "suite timed out" in lines[-1] and ": RED (" in lines[-1]


def test_expected_duration_and_worst_case_are_printed_before_any_step(repo, capsys):
    rec = TimeoutRecorder(repo)
    rc, lines = run_main(repo, capsys, runner=rec)
    first_step = next(i for i, line in enumerate(lines) if line.startswith("kicad_mcp.__file__"))
    expected = next(i for i, line in enumerate(lines) if line.startswith("expected duration: "))
    assert expected < first_step
    # every step that can time out, each followed by a bounded kill, plus the git calls
    kill_waits = 6 * gate.KILL_WAIT_S  # import, two collections, suite, lint, the last rerun
    worst = (sum(t for _, t in rec.timeouts if _ != "rerun") + gate.RERUN_BUDGET_S + kill_waits
             + gate.GIT_CALLS * gate.GIT_TIMEOUT_S + gate.PUBLISH_BUDGET_S)
    assert gate.worst_case_s(gate.SUITE_TIMEOUT_S) == worst
    assert f"at worst about {round(worst / 60)} min" in lines[expected]
    assert f"{gate.SUITE_MEASURED_S} s" in lines[expected]


def test_printed_worst_case_follows_the_timeout_override(repo, capsys):
    # import 120 + collections 2 x 300 + suite 1000 + reruns 600 + lint 300 + 6 kills x 60
    # + 8 git calls x 60 = 3460 s, about 58 min
    rc, lines = run_main(repo, capsys, "--timeout", "1000")
    expected = next(line for line in lines if line.startswith("expected duration: "))
    assert "the suite times out at 1000 s" in expected
    assert "at worst about 58 min" in expected


def test_worst_case_counts_every_git_call_of_main(repo, capsys, monkeypatch):
    calls = []
    real = gate.subprocess.run

    def counting(cmd, *a, **kw):
        if cmd[0] == "git":
            calls.append(cmd)
            assert kw.get("timeout") == gate.GIT_TIMEOUT_S, cmd
        return real(cmd, *a, **kw)

    monkeypatch.setattr(gate.subprocess, "run", counting)
    run_main(repo, capsys)
    # --python is given here; without it default_python() makes one more git call
    assert len(calls) + 1 <= gate.GIT_CALLS, calls


def test_builder_wraps_the_gate_in_a_timeout_above_its_own_worst_case():
    # The gate's own timeouts must fire first, so its RED-on-timeout always reaches the builder
    # (ticket 70 review round 1, medium 2): the outer `timeout N` is the worst case plus 10 minutes.
    text = (ROOT / ".claude" / "agents" / "builder.md").read_text(encoding="utf-8")
    found = re.findall(r"`timeout (\d+) python <worktree>/scripts/batch_gate\.py`", text)
    assert found, "builder.md starts the gate under timeout N"
    for n in found:
        assert int(n) >= gate.worst_case_s(gate.SUITE_TIMEOUT_S) + 600, n


def _prose(path: Path) -> str:
    """A Markdown file's text with blockquote markers dropped and whitespace collapsed."""
    text = path.read_text(encoding="utf-8")
    return " ".join(re.sub(r"(?m)^>\s?", "", text).split())


STOP_POINT = "the worst case it prints at its start"
# Only the suite's timeout is set from a measurement; the other steps and the git calls have
# fixed bounds (ticket 70 delta review 2), so no text may claim every step is 3x its duration.
BOUNDS = ("the suite's timeout is about 3x the slowest measured run with only other gates known to be"
          " running and every other step has a"
          " fixed bound, so the worst case the gate prints at its start is its stop point")
FALSE_CLAIMS = ("each step's timeout is about 3x", "already apply the 3x rule",
                "P10's 3x rule is enforced inside the gate")


def test_the_gate_stop_point_is_the_printed_worst_case_everywhere(repo, capsys):
    # Ticket 70 delta review 1: P10's "stop at about 3x the expected duration" (about 69 min for
    # 8-23 min) would stop the gate before its own suite timeout fires (about 82 min). The gate's
    # own timeouts bound every step, so a waiting agent stops it only after the worst case it
    # prints; CLAUDE.md, fork P11, docs/TESTING.md and the printed line must all say so, and P10 must
    # not claim the gate.
    playbook = _prose(ROOT / "docs" / "playbook" / "PROJECT-KICAD-MCP-PLAYBOOK.md")
    p10 = playbook.split("**Project rule P10:", 1)[1].split("**Project rule P11:", 1)[0]
    assert "batch gate" not in p10.lower() and "batch_gate" not in p10.lower()
    p11 = playbook.split("**Project rule P11:", 1)[1].split("**Project rule P12", 1)[0]
    assert BOUNDS in p11.lower()

    claude = _prose(ROOT / "CLAUDE.md")
    bullet = claude.split("**Don't over-wait.**", 1)[1].split(" - ", 1)[0]
    assert (f"(for the batch gate, the stop point is {STOP_POINT}; its own timeouts bound every"
            " step: docs/TESTING.md, fork playbook P11)") in bullet

    testing = _prose(ROOT / "docs/TESTING.md")
    assert BOUNDS in testing.lower()
    assert "not at 3x the expected duration (fork playbook P11)" in testing

    for name, text in (("P11", p11), ("CLAUDE.md", claude), ("docs/TESTING.md", testing)):
        for claim in FALSE_CLAIMS:
            assert claim.lower() not in text.lower(), (name, claim)

    rc, lines = run_main(repo, capsys)
    printed = next(line for line in lines if line.startswith("expected duration: "))
    assert printed.endswith("stop it and escalate (fork playbook P11, docs/TESTING.md)"), printed
    assert "P10" not in printed


def test_no_free_log_name_is_red(repo, capsys, monkeypatch):
    monkeypatch.setattr(gate.time, "strftime", lambda fmt: "20261020-120000")
    logs = repo.parent / "logs"
    logs.mkdir()
    stem = f"{_git(repo, 'rev-parse', 'HEAD')[:7]}-20261020-120000-{os.getpid()}"
    for n in range(1000):
        (logs / (f"{stem}.log" if n == 0 else f"{stem}-{n}.log")).write_text("taken", encoding="utf-8")
    rec = TimeoutRecorder(repo)
    rc, lines = run_main(repo, capsys, runner=rec)
    assert rc == 1
    assert lines[-1].startswith("BATCH GATE ") and ": RED (no free log name" in lines[-1]
    assert rec.timeouts == []


def test_log_dir_inside_the_tree_is_red(repo, capsys):
    rec = TimeoutRecorder(repo)
    capsys.readouterr()
    rc = gate.main(["--python", "py", "--log-dir", str(repo / "build" / "logs")], root=repo, runner=rec)
    lines = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert "RED (" in lines[-1] and "--log-dir" in lines[-1]
    assert rec.timeouts == [], "nothing runs"


def test_kill_tree_returns_when_taskkill_hangs(monkeypatch):
    # The kill of a timed-out step is bounded by KILL_WAIT_S (half for taskkill, half for the wait),
    # so the worst case holds.
    seen = {}

    def hanging_run(cmd, *a, **kw):
        seen["timeout"] = kw.get("timeout")
        raise gate.subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)

    class Proc:
        pid = 4242
        killed = False

        def wait(self, timeout=None):
            seen["wait"] = timeout
            raise gate.subprocess.TimeoutExpired("x", timeout)

        def kill(self):
            Proc.killed = True

    monkeypatch.setattr(gate.os, "name", "nt")
    monkeypatch.setattr(gate.subprocess, "run", hanging_run)
    gate._kill_tree(Proc())
    assert seen == {"timeout": gate.KILL_WAIT_S / 2, "wait": gate.KILL_WAIT_S / 2}
    assert Proc.killed


def test_reruns_stay_within_their_budget(root):
    # Ten new failures whose reruns each hang to their timeout: the reruns may not take
    # 10 x RERUN_TIMEOUT_S; they stop at RERUN_BUDGET_S, and the verdict stays RED.
    ids = [f"tests/test_b.py::test_{k}" for k in range(10)]
    out = pytest_output(root, failed=ids)
    clock = [0.0]

    class Hanging(TimeoutRecorder):
        def __call__(self, cmd, env, cwd, timeout, out_path):
            rc = super().__call__(cmd, env, cwd, timeout, out_path)
            if self.timeouts[-1][0] == "rerun":
                clock[0] += timeout + 100  # hangs to its timeout, plus process start and teardown
                return None
            return rc

    rec = Hanging(root, suite_rc=1, suite_out=out)
    log = root.parent / "logs"
    log.mkdir(exist_ok=True)
    said: list[str] = []
    res = gate.run_gate(root=root, sha="abcdef1234567", dirty=[], python="py", known_red_text=KNOWN_TEXT,
                        known_skips_text=KNOWN_SKIPS, tracked_tests=TRACKED, pyproject_text=PYPROJECT,
                        runner=rec, log_dir=log, say=said.append, clock=lambda: clock[0])
    reruns = [t for s, t in rec.timeouts if s == "rerun"]
    assert (gate.RERUN_BUDGET_S, gate.RERUN_TIMEOUT_S) == (600, 300)
    assert reruns == [300, 200], "the second rerun gets only what is left of the budget"
    assert any("rerun budget" in line for line in said)
    assert not res.green and res.flaky == []


def test_lint_timeout_is_short_and_a_lint_timeout_is_red(root):
    class LintHangs(TimeoutRecorder):
        def __call__(self, cmd, env, cwd, timeout, out_path):
            rc = super().__call__(cmd, env, cwd, timeout, out_path)
            return None if cmd[-1].endswith("lint_gate.py") else rc

    rec = LintHangs(root)
    res, _ = run(root, rec)
    assert ("lint", gate.LINT_TIMEOUT_S) in rec.timeouts and gate.LINT_TIMEOUT_S <= 300
    assert res.reasons == ["lint"]


# --------------------------------------------------------------------------- build ticket 79
# A git failure at the start prints a verdict line (it used to end in a traceback without one).

START_RED = "BATCH GATE <unknown>: RED (could not read the tree at the start: "


def _start_fails_in_a_directory_that_is_no_repo(root, monkeypatch):
    (root / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")  # no `git init`
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(root.parent))  # tmp_path may lie in a repo


def _start_fails_in_a_repo_without_a_commit(root, monkeypatch):
    _git(root, "init", "-q")  # --show-toplevel works, `rev-parse --verify HEAD` fails


def _start_fails_when_git_cannot_start(root, monkeypatch):
    _git(root, "init", "-q")
    real = gate.subprocess.run

    def no_git(cmd, *a, **kw):
        if cmd[0] == "git":
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(gate.subprocess, "run", no_git)


def _start_fails_when_git_hangs(root, monkeypatch):
    _git(root, "init", "-q")

    def hangs(cmd, *a, **kw):
        raise gate.subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(gate.subprocess, "run", hangs)


@pytest.mark.parametrize("setup, detail", [
    (_start_fails_in_a_directory_that_is_no_repo, "git rev-parse --show-toplevel exited 128: fatal: not a git repository"),
    (_start_fails_in_a_repo_without_a_commit, "git rev-parse --verify HEAD exited 128: fatal: "),
    (_start_fails_when_git_cannot_start, "git rev-parse --show-toplevel could not start: "),
    (_start_fails_when_git_hangs, f"git rev-parse --show-toplevel timed out after {gate.GIT_TIMEOUT_S} s"),
], ids=["no-repo", "no-commit", "no-git", "git-hangs"])
def test_git_failure_at_the_start_prints_a_red_verdict_line(root, capsys, monkeypatch, setup, detail):
    setup(root, monkeypatch)
    rec = TimeoutRecorder(root)
    capsys.readouterr()
    rc = gate.main(["--python", "py", "--log-dir", str(root.parent / "logs")], root=root, runner=rec)
    out = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert out[-1].startswith(START_RED + detail), out
    assert out[-1].endswith(")") and len(out) == 1, "one line, the verdict"
    assert rec.timeouts == [], "nothing runs"


def test_git_failure_while_finding_the_interpreter_prints_a_red_verdict_line(repo, capsys, monkeypatch):
    monkeypatch.delenv("KICAD_MCP_PYTHON", raising=False)
    sha7 = _git(repo, "rev-parse", "HEAD")[:7]
    real = gate.subprocess.run

    def common_dir_fails(cmd, *a, **kw):
        if "--git-common-dir" in cmd:
            raise gate.subprocess.CalledProcessError(128, cmd, "", "fatal: broken\n")
        return real(cmd, *a, **kw)

    monkeypatch.setattr(gate.subprocess, "run", common_dir_fails)
    rec = TimeoutRecorder(repo)
    capsys.readouterr()
    rc = gate.main(["--log-dir", str(repo.parent / "logs")], root=repo, runner=rec)
    out = capsys.readouterr().out.splitlines()
    assert rc == 1
    assert out[-1] == (f"BATCH GATE {sha7}: RED (could not read the tree at the start:"
                       " git rev-parse --path-format=absolute --git-common-dir exited 128: fatal: broken)")
    assert rec.timeouts == []


# Gates of every worktree share one log directory; each run starts by removing old runs' files
# (build ticket 79), never a run that may still be going, never a file the gate did not name.

DAY = 24 * 3600
RUN_SUFFIXES = ("log", "import.out", "network.out", "collect.out", "pytest.out", "lint.out")


def old_run(logs: Path, run_id: str, age_s: float, suffixes=RUN_SUFFIXES) -> list[Path]:
    """A finished gate run's files in ``logs``, all last written ``age_s`` ago."""
    logs.mkdir(exist_ok=True)
    stamp = gate.time.time() - age_s
    files = []
    for suffix in suffixes:
        f = logs / f"{run_id}.{suffix}"
        f.write_text(f"{run_id} {suffix}\n", encoding="utf-8")
        os.utime(f, (stamp, stamp))
        files.append(f)
    return files


def cleanup_line(lines: list[str]) -> str:
    return next(line for line in lines if line.startswith("log cleanup: "))


def test_runs_older_than_the_maximum_age_are_removed_at_the_start(repo, capsys):
    logs = repo.parent / "logs"
    stale = old_run(logs, "1234567-20261001-101010-111", gate.LOG_MAX_AGE_S + 3600)
    stale_old_style = old_run(logs, "89abcde", gate.LOG_MAX_AGE_S + 3600, ("log", "pytest.out", "rerun-0.out"))
    recent = old_run(logs, "1234567-20261019-101010-222", 2 * DAY)
    foreign = logs / "notes.txt"  # not a gate file: never touched, whatever its age
    foreign.write_text("mine\n", encoding="utf-8")
    os.utime(foreign, (0, 0))
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert not any(f.exists() for f in stale + stale_old_style)
    assert all(f.exists() for f in recent) and foreign.exists()
    line = cleanup_line(lines)
    assert "removed 2 runs (9 files" in line, line
    first_step = next(i for i, x in enumerate(lines) if x.startswith("kicad_mcp.__file__"))
    assert lines.index(line) < first_step, "at the start"


def test_runs_beyond_the_kept_count_are_removed_once_older_than_a_day(repo, capsys):
    logs = repo.parent / "logs"
    keep = gate.LOG_KEEP_RUNS
    # the newest `keep` runs are a few hours old; 3 more beyond them: two of 2 days, one of 2 hours
    for k in range(keep):
        old_run(logs, f"aaaaaaa-20261021-000000-{k}", 3 * 3600 + k, ("log",))
    beyond_old = old_run(logs, "bbbbbbb-20261019-000000-1", 2 * DAY) + old_run(logs, "bbbbbbb-20261019-000000-2",
                                                                               2 * DAY + 60)
    beyond_young = old_run(logs, "ccccccc-20261021-000000-1", 2 * 3600)  # newer than some of the kept
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert not any(f.exists() for f in beyond_old)
    assert all(f.exists() for f in beyond_young), "a run younger than a day may still be going"
    # the 'c' run pushed the oldest 'a' run beyond the count, but it is only hours old: it stays
    assert len(list(logs.glob("aaaaaaa-*.log"))) == keep
    assert "removed 2 runs" in cleanup_line(lines)


def test_young_runs_are_kept_however_many_there_are(repo, capsys):
    # a burst of parallel gates: more than LOG_KEEP_RUNS runs within the last day; none may go
    logs = repo.parent / "logs"
    for k in range(gate.LOG_KEEP_RUNS + 20):
        old_run(logs, f"ddddddd-20261021-000000-{k}", gate.LOG_MIN_AGE_S - 600, ("log", "pytest.out"))
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert len(list(logs.glob("ddddddd-*"))) == 2 * (gate.LOG_KEEP_RUNS + 20)
    assert "removed 0 runs" in cleanup_line(lines)


@pytest.mark.skipif(os.name != "nt", reason="an open file blocks its removal only on Windows")
def test_a_run_whose_log_a_gate_still_holds_is_never_removed(repo, capsys):
    # Another gate holds its .log open from before its first step to its verdict. Even if its files
    # look old (a clock change, a copied directory), none of that run's files may go.
    logs = repo.parent / "logs"
    held_run = old_run(logs, "eeeeeee-20261001-000000-1", gate.LOG_MAX_AGE_S + DAY)
    gone_run = old_run(logs, "eeeeeee-20261001-000000-2", gate.LOG_MAX_AGE_S + DAY)
    with open(held_run[0], "a", encoding="utf-8"):  # held_run[0] is the .log, as a gate holds it
        rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert all(f.exists() for f in held_run), "not one file of a held run is removed"
    assert not any(f.exists() for f in gone_run)
    line = cleanup_line(lines)
    assert "removed 1 runs" in line and "1 files could not be removed" in line, line
    assert any(x.startswith("  not removed (held or locked): eeeeeee-20261001-000000-1.log") for x in lines)


def test_a_file_that_cannot_be_removed_is_skipped_and_the_gate_stays_green(repo, capsys, monkeypatch):
    logs = repo.parent / "logs"
    files = old_run(logs, "fffffff-20261001-000000-1", gate.LOG_MAX_AGE_S + DAY)
    stuck = logs / "fffffff-20261001-000000-1.pytest.out"
    real_remove = gate.os.remove

    def remove(path, *a, **kw):
        if Path(path) == stuck:
            raise PermissionError(13, "Access is denied")
        return real_remove(path, *a, **kw)

    monkeypatch.setattr(gate.os, "remove", remove)
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert stuck.exists() and [f for f in files if f.exists()] == [stuck]
    assert "1 files could not be removed" in cleanup_line(lines)
    assert "  not removed (held or locked): fffffff-20261001-000000-1.pytest.out (PermissionError: Access is denied)" in lines


def test_two_gates_cleaning_up_at_once_do_not_trip_over_each_other(repo, capsys, monkeypatch):
    # Two gates that start together list the same old files; whichever removes a file second finds
    # it gone. That is no error, and the other gate's fresh files are untouched.
    logs = repo.parent / "logs"
    files = old_run(logs, "0000000-20261001-000000-1", gate.LOG_MAX_AGE_S + DAY)
    other_gate = old_run(logs, f"{_git(repo, 'rev-parse', 'HEAD')[:7]}-20261021-000000-99999", 5, ("log",))
    real_remove = gate.os.remove

    def the_other_gate_was_faster(path, *a, **kw):
        real_remove(path)
        return real_remove(path, *a, **kw)  # FileNotFoundError

    monkeypatch.setattr(gate.os, "remove", the_other_gate_was_faster)
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert not any(f.exists() for f in files) and other_gate[0].exists()
    assert "0 files could not be removed" in cleanup_line(lines)


def test_the_current_runs_files_are_excluded_by_name(repo, capsys, monkeypatch):
    # The current run's own name is never a candidate, even when a file with it looks old.
    monkeypatch.setattr(gate.time, "strftime", lambda fmt: "20261001-120000")
    logs = repo.parent / "logs"
    run_id = f"{_git(repo, 'rev-parse', 'HEAD')[:7]}-20261001-120000-{os.getpid()}"
    mine = old_run(logs, run_id, gate.LOG_MAX_AGE_S + DAY, ("rerun-7.out",))  # a green run writes no rerun
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert f"log: {logs / (run_id + '.log')}" in lines
    assert mine[0].exists()
    # not even tried (on Windows the gate's own open .log would also refuse; the name comes first)
    line = cleanup_line(lines)
    assert "removed 0 runs" in line and "kept 0;" in line and "0 files could not be removed" in line, line


def test_a_cleanup_that_fails_cannot_fail_the_gate(repo, capsys, monkeypatch):
    def broken(*a, **kw):
        raise RuntimeError("listing exploded")

    monkeypatch.setattr(gate.os, "scandir", broken)
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert lines[-1].endswith(": GREEN")
    assert cleanup_line(lines) == "log cleanup: failed and skipped (RuntimeError: listing exploded)"


# Ticket 79 review round 1: three guards that no test pinned.


def test_names_that_only_start_like_a_gate_file_are_never_touched(repo, capsys):
    # a kept copy of an old log (evidence) is not the gate's file, however old
    logs = repo.parent / "logs"
    copies = old_run(logs, "1234567-20261001-101010-1", gate.LOG_MAX_AGE_S + DAY, ("log.keep", "pytest.out.bak"))
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert all(f.exists() for f in copies)
    assert "removed 0 runs" in cleanup_line(lines)


def test_a_runs_age_is_that_of_its_newest_file(repo, capsys):
    # one file written an hour ago makes the whole run young, even if its other files are old
    # (old-style runs named by sha7 alone share their names across runs)
    logs = repo.parent / "logs"
    run_files = old_run(logs, "2345678", 3600, ("log",)) + old_run(logs, "2345678", gate.LOG_MAX_AGE_S + DAY,
                                                                    ("pytest.out",))
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert all(f.exists() for f in run_files)
    assert "removed 0 runs" in cleanup_line(lines)


def test_the_first_run_beyond_the_kept_count_is_removed(repo, capsys):
    logs = repo.parent / "logs"
    for k in range(gate.LOG_KEEP_RUNS):
        old_run(logs, f"3456789-20261021-000000-{k}", 3600 + k, ("log",))
    first_beyond = old_run(logs, "3456789-20261019-000000-1", 2 * DAY, ("log",))
    rc, lines = run_main(repo, capsys)
    assert rc == 0, lines
    assert not first_beyond[0].exists()
    assert len(list(logs.glob("3456789-20261021-*.log"))) == gate.LOG_KEEP_RUNS
    assert "removed 1 runs" in cleanup_line(lines)


# --------------------------------------------------------------------------- build ticket 88
# Every start failure ends in exactly one verdict line, `BATCH GATE <sha7|unknown>: RED (...)`, and
# exit code 1, never a traceback or a bare message (ticket 79 review, delta review 1).


def start_red(root, capsys, *argv, log_dir=None):
    """main() on ``root``; asserts a start failure: exit 1, one BATCH GATE line, the last one, and
    nothing run. Returns that line."""
    rec = TimeoutRecorder(root)
    capsys.readouterr()
    rc = gate.main([*argv, "--log-dir", str(log_dir or root.parent / "logs")], root=root, runner=rec)
    out = capsys.readouterr().out.splitlines()
    assert rc == 1, out
    verdicts = [line for line in out if line.startswith("BATCH GATE")]
    assert len(verdicts) == 1 and out[-1] == verdicts[0], out
    assert re.fullmatch(r"BATCH GATE (<unknown>|[0-9a-f]{7}(-dirty)?): RED \(.+\)", out[-1]), out
    assert rec.timeouts == [], "nothing runs"
    return out[-1]


def test_script_below_the_top_of_its_tree_prints_a_red_verdict_line(repo, capsys):
    sub = repo / "scripts"
    line = start_red(sub, capsys, "--python", "py")
    assert line.startswith(f"BATCH GATE <unknown>: RED (script at {sub} is not the top of its git tree "), line


def test_no_shared_interpreter_prints_a_red_verdict_line(repo, capsys, monkeypatch):
    # the repo's main checkout is the repo itself, and it has no .venv
    monkeypatch.delenv("KICAD_MCP_PYTHON", raising=False)
    sha7 = _git(repo, "rev-parse", "HEAD")[:7]
    line = start_red(repo, capsys)
    assert line.startswith(f"BATCH GATE {sha7}: RED (no shared interpreter under "), line
    assert line.endswith("; pass --python)"), line


@pytest.mark.parametrize("where", ["is-a-file", "below-a-file"])
def test_log_directory_that_cannot_be_made_prints_a_red_verdict_line(repo, capsys, where):
    blocker = repo.parent / "not-a-dir"
    blocker.write_text("a file, not a directory", encoding="utf-8")
    log_dir = blocker if where == "is-a-file" else blocker / "logs"
    sha7 = _git(repo, "rev-parse", "HEAD")[:7]
    line = start_red(repo, capsys, "--python", "py", log_dir=log_dir)
    assert line.startswith(f"BATCH GATE {sha7}: RED (could not create the log directory {log_dir}: "), line


def test_log_file_that_cannot_be_created_prints_a_red_verdict_line(repo, capsys, monkeypatch):
    # not a taken name (that tries the next one) but a refusal, such as a denied write
    real_open = open

    def denied(file, mode="r", *a, **kw):
        if mode == "x":
            raise PermissionError(13, "Permission denied", str(file))
        return real_open(file, mode, *a, **kw)

    monkeypatch.setattr(gate, "open", denied, raising=False)
    sha7 = _git(repo, "rev-parse", "HEAD")[:7]
    line = start_red(repo, capsys, "--python", "py")
    assert line.startswith(f"BATCH GATE {sha7}: RED (could not create the log {repo.parent / 'logs'}"), line
    assert "Permission denied" in line, line


@pytest.mark.parametrize("name", ["pyproject.toml", "scripts/known-red.txt", "scripts/known-skips.toml"])
def test_input_file_that_cannot_be_read_prints_a_red_verdict_line(repo, capsys, name):
    # a directory in the file's place: it exists, and reading it fails
    (repo / name).unlink()
    (repo / name).mkdir()
    sha7 = _git(repo, "rev-parse", "HEAD")[:7]
    line = start_red(repo, capsys, "--python", "py")
    assert line.startswith(f"BATCH GATE {sha7}-dirty: RED (could not read {repo / name}: "), line
    log = next((repo.parent / "logs").glob("*.log"))
    assert log.read_text(encoding="utf-8").splitlines()[-1] == line, "the log ends with the verdict too"


def test_figures_quoted_in_builder_and_testing_follow_the_gate():
    # builder.md and docs/TESTING.md repeat the gate's timeout, worst case and expected duration in prose;
    # a timeout raised without them left "about 111 minutes" against a printed 131 (build ticket 88).
    worst_min = round(gate.worst_case_s(gate.SUITE_TIMEOUT_S) / 60)
    expected_min = round((gate.SUITE_MEASURED_S + gate.OVERHEAD_S) / 60)
    builder = _prose(ROOT / ".claude" / "agents" / "builder.md")
    (outer,) = set(re.findall(r"`timeout (\d+) python <worktree>/scripts/batch_gate\.py`", builder))
    assert f"`timeout {outer}` ({round(int(outer) / 60)} minutes) makes sure it exits" in builder
    assert f"the worst case the gate prints at its start (about {worst_min} minutes)" in builder
    assert f"takes about {expected_min} minutes" in builder
    testing = _prose(ROOT / "docs/TESTING.md")
    assert f"up to {gate.SUITE_MEASURED_S} s with several builders' gates in parallel" in testing
    assert f"its expected duration (about {expected_min} min) and its worst case (about {worst_min} min," in testing
    assert f"times the suite out at {gate.SUITE_TIMEOUT_S} s (`SUITE_TIMEOUT_S`" in testing
    assert f"Builders wrap it in `timeout {outer}`" in testing
    # fork P11 carries the same figures (ticket 88 review round 1)
    playbook = _prose(ROOT / "docs" / "playbook" / "PROJECT-KICAD-MCP-PLAYBOOK.md")
    p11 = playbook.split("**Project rule P11:", 1)[1].split("**Project rule P12", 1)[0]
    assert f"under a {gate.SUITE_TIMEOUT_S} s timeout" in p11
    assert f"up to {gate.SUITE_MEASURED_S} s" in p11
    assert f"about {worst_min} min, at its start" in p11
    assert f"It takes 8-{expected_min} minutes" in p11


# --------------------------------------------------------------------------- publish-prep ticket 03a
# The standing publishability check is a step of the gate (its own behaviour: test_gate_publishable.py).


def test_publishability_reasons_make_the_gate_red(root, monkeypatch):
    monkeypatch.setattr(gate, "check_publishable_tracked",
                        lambda *a, **kw: ["BOARDPROOF_FORBIDDEN_FILE unset (the publishability check cannot run)"])
    res, _ = run(root, FakeRunner(root))
    assert res.reasons == ["BOARDPROOF_FORBIDDEN_FILE unset (the publishability check cannot run)"]


def test_the_gate_runs_the_publishability_check_with_its_interpreter(root, monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "check_publishable_tracked", lambda r, python, say, **kw: seen.append((r, python)) or [])
    res, _ = run(root, FakeRunner(root))
    assert res.green and seen == [(root, "py")]


def test_main_runs_the_publishability_check_and_goes_red_without_the_variable(repo, capsys, monkeypatch):
    monkeypatch.undo()  # the real check, not the stub of the fixture
    monkeypatch.setenv("KICAD_ROUTING_TOOLS_PATH", str(repo.parent / "krt"))
    monkeypatch.setenv("NGSPICE_PATH", str(repo.parent / "ngspice_con.exe"))
    monkeypatch.delenv("BOARDPROOF_FORBIDDEN_FILE", raising=False)
    rc, lines = run_main(repo, capsys)
    assert rc == 1
    assert "BOARDPROOF_FORBIDDEN_FILE unset" in lines[-1], lines[-1]
