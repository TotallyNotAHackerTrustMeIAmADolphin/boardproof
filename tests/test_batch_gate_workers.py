"""Build ticket 154: the batch gate with ``--workers N`` (pytest-xdist, stage 2 of issue 70).

Default behaviour (``--workers 1``) is the serial gate and is pinned by ``tests/test_batch_gate.py``
unchanged. Here: the xdist output shapes the gate accepts (observed on pytest 9.2.0 with
pytest-xdist 3.8.0 and the gate's own command shape ``-rfEs`` without ``-q``), every RED condition
of the gate's docstring under ``--workers 4``, the separate timeout basis of the parallel mode and a
real mini project under ``-n`` for the per-test times file.

What xdist prints differently from a serial run, observed:

- no ``collected N items`` line; instead ``created: 4/4 workers`` and ``4 workers [M items]``,
  where M is the number of SELECTED tests (after ``-m "not network"``);
- the final summary has no ``N deselected`` part, so the deselected count cannot be read from the
  run; the gate compares M with the two collection runs (all tests minus the network-marked ones);
- the short test summary, the SKIPPED lines and the final summary line keep their serial shapes.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

import tests.test_batch_gate as base
from tests._hermetic_git import apply_hermetic_env
from tests.test_batch_gate import ALL_COLLECTED, IPC_SKIP, KNOWN, NETWORK, FakeRunner, TimeoutRecorder, run, run_main

gate = base.gate
ROOT = Path(__file__).resolve().parents[1]
WORKERS = 4
SELECTED = 400  # tests that pytest_output selects by default (passed=400)


@pytest.fixture
def root(tmp_path, monkeypatch):
    """As tests/test_batch_gate.py's ``root``: a fake worktree, a router folder, no outer-shell leaks."""
    r = tmp_path / "wt-1"
    (r / "src" / "kicad_mcp").mkdir(parents=True)
    krt = tmp_path / "krt"
    krt.mkdir()
    monkeypatch.setenv("KICAD_ROUTING_TOOLS_PATH", str(krt))
    ngspice = tmp_path / "ngspice_con.exe"
    ngspice.write_text("", encoding="utf-8")
    monkeypatch.setenv("NGSPICE_PATH", str(ngspice))
    monkeypatch.delenv("NGSPICE_REQUIRED", raising=False)
    monkeypatch.setattr(gate, "check_publishable_tracked", lambda *a, **kw: [])
    return r


@pytest.fixture
def repo(root, monkeypatch):
    """A committed, clean git repo shaped like this project, for main()."""
    apply_hermetic_env(monkeypatch)
    (root / "src" / "kicad_mcp" / "__init__.py").write_text("", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "known-red.txt").write_text(base.KNOWN_TEXT, encoding="utf-8")
    (root / "scripts" / "known-skips.toml").write_text(base.KNOWN_SKIPS, encoding="utf-8")
    (root / "tests").mkdir()
    for f in base.TRACKED:
        (root / f).write_text("def test_x():\n    pass\n", encoding="utf-8")
    (root / "pyproject.toml").write_text(base.PYPROJECT, encoding="utf-8")
    base._git(root, "init", "-q")
    base._git(root, "add", "-A")
    base._git(root, "commit", "-q", "-m", "start")
    return root


def xdist_output(root: Path, *, workers=WORKERS, **kw) -> str:
    """The canned serial output turned into what pytest-xdist prints for the same run."""
    out = base.pytest_output(root, **kw)
    lines = []
    selected = None
    for line in out.splitlines():
        m = re.match(r"collected (\d+) items(?: / (\d+) deselected / (\d+) selected)?", line)
        if m:
            selected = int(m.group(3) or m.group(1))
            lines += [f"created: {workers}/{workers} workers", f"{workers} workers [{selected} items]"]
            continue
        if re.match(r"=+ .* in [\d.]+s", line):  # xdist's summary has no "N deselected"
            line = re.sub(r", \d+ deselected", "", line)
        lines.append(line)
    return "\n".join(lines) + "\n"


class WorkersRunner(FakeRunner):
    """A FakeRunner whose suite run prints xdist-shaped output and records the suite command."""

    def __init__(self, root, **kw):
        out = kw.pop("suite_out", None)
        out = out if out is not None else xdist_output(root)
        # consistent collections: all tests = the workers' selected items + the network-marked ones,
        # unless a test hands its own full collection (to make them disagree)
        wl = gate.workers_line(out)
        network = kw.get("network", NETWORK)
        if wl and "all_out" not in kw:
            kw["all_out"] = ("tests/test_a.py::test_1\ntests/test_b.py::TestB::test_2[x - y]\n\n"
                             f"{wl[1] + network} tests collected in 2.00s\n")
        if "collect_out" not in kw:
            found = re.search(r"(\d+) tests collected", kw.get("all_out", ALL_COLLECTED))
            total = found.group(1) if found else "403"
            kw["collect_out"] = f"{network}/{total} tests collected ({int(total) - network} deselected) in 2.10s\n"
        super().__init__(root, suite_out=out, **kw)
        self.suite_cmds: list[list[str]] = []

    def __call__(self, cmd, env, cwd, timeout, out_path):
        if "not network" in cmd:
            self.suite_cmds.append(cmd)
        return super().__call__(cmd, env, cwd, timeout, out_path)


def run_w(root, runner, workers=WORKERS, **kw):
    log = root.parent / "logs"
    log.mkdir(exist_ok=True)
    said: list[str] = []
    res = gate.run_gate(root=root, sha="abcdef1234567", dirty=[], python="py",
                        known_red_text=kw.get("known", base.KNOWN_TEXT),
                        known_skips_text=kw.get("skips", base.KNOWN_SKIPS), tracked_tests=base.TRACKED,
                        pyproject_text=base.PYPROJECT, runner=runner, log_dir=log, say=said.append,
                        suite_timeout=gate.SUITE_TIMEOUT_PARALLEL_S, workers=workers)
    return res, "\n".join(said)


# --------------------------------------------------------------------------- command and defaults


def test_workers_adds_n_and_dist_load_to_the_suite_command_only(root):
    rec = WorkersRunner(root)
    res, _ = run_w(root, rec)
    assert res.green, res.reasons
    (cmd,) = rec.suite_cmds
    assert cmd == ["py", "-m", "pytest", "-m", "not network", "-rfEs", "-p", "no:cacheprovider",
                   "-n", "4", "--dist", "load"]
    # the collections stay serial and addopts is never used
    assert all("-n" not in c for c in rec.calls if "--collect-only" in c)


def test_workers_1_and_the_default_run_the_exact_serial_command(root):
    for kwargs in ({}, {"workers": 1}):
        rec = WorkersRunner(root, suite_out=base.pytest_output(root))
        log = root.parent / "logs"
        log.mkdir(exist_ok=True)
        res = gate.run_gate(root=root, sha="abcdef1234567", dirty=[], python="py", known_red_text=base.KNOWN_TEXT,
                            known_skips_text=base.KNOWN_SKIPS, tracked_tests=base.TRACKED,
                            pyproject_text=base.PYPROJECT, runner=rec, log_dir=log, say=lambda m: None, **kwargs)
        assert res.green, res.reasons
        assert rec.suite_cmds == [["py", "-m", "pytest", "-m", "not network", "-rfEs", "-p", "no:cacheprovider"]]


def test_a_rerun_of_a_new_failure_stays_serial(root):
    out = xdist_output(root, failed=["tests/test_a.py::test_new"])
    rec = WorkersRunner(root, suite_rc=1, suite_out=out)
    res, _ = run_w(root, rec)
    assert res.new == ["tests/test_a.py::test_new"]
    rerun = [c for c in rec.calls if c[-1] == "tests/test_a.py::test_new"]
    assert rerun and all("-n" not in c and "--dist" not in c for c in rerun)


def test_the_expected_pytest_config_is_untouched_and_never_carries_addopts():
    assert "addopts" not in gate.EXPECTED_PYTEST_CONFIG
    assert set(gate.EXPECTED_PYTEST_CONFIG) == {"testpaths", "pythonpath", "markers", "filterwarnings"}


# --------------------------------------------------------------------------- the accepted shapes


def test_xdist_shaped_green_run_is_green_and_the_workers_line_is_printed(root):
    res, said = run_w(root, WorkersRunner(root))
    assert res.green, res.reasons
    assert "4 workers [400 items]" in said


def test_known_red_failure_alone_stays_green_under_workers(root):
    res, _ = run_w(root, WorkersRunner(root, suite_rc=1, suite_out=xdist_output(root, failed=[KNOWN])))
    assert res.green, res.reasons


def test_known_skips_are_counted_from_the_xdist_summary(root):
    out = xdist_output(root, skipped=[(*IPC_SKIP, 3)])
    res, said = run_w(root, WorkersRunner(root, suite_out=out))
    assert res.green, res.reasons
    assert "3x tests/test_ipc.py" in said


def test_the_real_xdist_header_shapes_are_parsed():
    out = ("rootdir: X\nconfigfile: pyproject.toml\nplugins: xdist-3.8.0\ncreated: 4/4 workers\n"
           "4 workers [51 items]\n\n....\n")
    assert gate.workers_line(out) == (4, 51)
    assert gate.workers_line("2 workers [1 item]\n") == (2, 1)
    assert gate.workers_line("created: 4/4 workers\n") is None
    assert gate.workers_line("collected 51 items\n") is None
    assert gate.workers_line("collecting: 3/4 workers\n") is None


# --------------------------------------------------------------------------- RED conditions under --workers 4


def test_red_xdist_did_not_start_workers_the_serial_shape_is_not_accepted(root):
    res, _ = run_w(root, WorkersRunner(root, suite_out=base.pytest_output(root)))
    assert any("workers line" in r for r in res.reasons), res.reasons


def test_red_a_different_number_of_workers(root):
    res, _ = run_w(root, WorkersRunner(root, suite_out=xdist_output(root, workers=2)))
    assert any("2 workers" in r and "4" in r for r in res.reasons), res.reasons


def test_red_selected_items_differ_from_collected_minus_network(root):
    # e.g. -k or --ignore smuggled in: fewer items than the full collection less the network tests
    out = xdist_output(root).replace("[400 items]", "[300 items]")
    res, _ = run_w(root, WorkersRunner(root, suite_out=out))
    assert any("300" in r and "400" in r for r in res.reasons), res.reasons


def test_red_more_or_fewer_network_marked_tests_than_deselected(root):
    # the full collection says 403, the network collection says 5: 398 selected expected, 400 run
    res, _ = run_w(root, WorkersRunner(root, network=5, all_out=ALL_COLLECTED))
    assert any("selected" in r for r in res.reasons), res.reasons


def test_red_a_deselected_part_in_the_summary_that_is_not_the_network_count(root):
    out = xdist_output(root).replace("400 passed", "400 passed, 9 deselected")
    res, _ = run_w(root, WorkersRunner(root, suite_out=out))
    assert any("deselected" in r for r in res.reasons), res.reasons


def test_red_unreadable_full_collection_cannot_check_the_selection(root):
    res, _ = run_w(root, WorkersRunner(root, all_out="boom\n", all_rc=4))
    assert any("full collection" in r for r in res.reasons)
    assert any("selection" in r or "selected" in r for r in res.reasons), res.reasons


def test_red_unreadable_network_collection(root):
    res, _ = run_w(root, WorkersRunner(root, collect_out="boom\n", collect_rc=4))
    assert any("network" in r for r in res.reasons)


def test_red_tracked_file_not_collected(root):
    res, _ = run_w(root, WorkersRunner(root, all_out="tests/test_a.py::test_1\n\n403 tests collected in 2s\n"))
    assert any("not collected" in r for r in res.reasons)


def test_red_new_failure_is_printed_by_id(root):
    out = xdist_output(root, failed=["tests/test_a.py::test_new"])
    res, said = run_w(root, WorkersRunner(root, suite_rc=1, suite_out=out))
    assert any("new failure" in r for r in res.reasons)
    assert "tests/test_a.py::test_new" in said


def test_red_new_failure_in_a_parametrised_id_with_a_dash(root):
    out = xdist_output(root, failed=["tests/test_a.py::test_p[a - b]"])
    res, _ = run_w(root, WorkersRunner(root, suite_rc=1, suite_out=out))
    assert res.new == ["tests/test_a.py::test_p[a - b]"]


def test_red_flaky_candidate_is_still_red(root):
    out = xdist_output(root, failed=["tests/test_a.py::test_new"])
    res, _ = run_w(root, WorkersRunner(root, suite_rc=1, suite_out=out, rerun_rc=0))
    assert res.flaky == ["tests/test_a.py::test_new"] and not res.green


def test_red_timeout(root):
    res, _ = run_w(root, WorkersRunner(root, suite_rc=None, suite_out="partial\n"))
    assert "suite timed out" in res.reasons


@pytest.mark.parametrize("rc", [2, 3, 4, 5, 137])
def test_red_other_exit_codes(root, rc):
    res, _ = run_w(root, WorkersRunner(root, suite_rc=rc))
    assert any(f"exit code {rc}" in r for r in res.reasons)


def test_red_exit_1_without_failure_ids_and_exit_0_with_them(root):
    res, _ = run_w(root, WorkersRunner(root, suite_rc=1))
    assert any("without failure ids" in r for r in res.reasons)
    res, _ = run_w(root, WorkersRunner(root, suite_rc=0, suite_out=xdist_output(root, failed=[KNOWN])))
    assert any("exit code 0 with failure ids" in r for r in res.reasons)


def test_red_failure_lines_must_add_up_to_the_summary(root):
    out = xdist_output(root, failed=[KNOWN]).replace("1 failed", "2 failed")
    res, _ = run_w(root, WorkersRunner(root, suite_rc=1, suite_out=out))
    assert any("failure lines" in r for r in res.reasons)


def test_red_missing_output(root):
    res, _ = run_w(root, WorkersRunner(root, suite_out=""))
    assert any("rootdir" in r for r in res.reasons) and any("workers line" in r for r in res.reasons)


def test_red_rootdir_of_another_checkout(root, tmp_path):
    res, _ = run_w(root, WorkersRunner(root, suite_out=xdist_output(root, rootdir=str(tmp_path / "other"))))
    assert "rootdir is not this worktree" in res.reasons


@pytest.mark.parametrize("configfile", [None, "tox.ini"])
def test_red_configfile_must_be_pyproject(root, configfile):
    res, _ = run_w(root, WorkersRunner(root, suite_out=xdist_output(root, configfile=configfile)))
    assert any("configfile" in r for r in res.reasons)


def test_red_nothing_passed_and_not_every_selected_test_ran(root):
    res, _ = run_w(root, WorkersRunner(root, suite_out=xdist_output(root, passed=0, skipped=[(*IPC_SKIP, 3)])))
    assert any("nothing passed" in r for r in res.reasons)
    out = xdist_output(root, failed=[KNOWN], ran=10)
    res, _ = run_w(root, WorkersRunner(root, suite_rc=1, suite_out=out))
    assert any("selected" in r and "ran" in r for r in res.reasons)


def test_red_unknown_skip_over_max_skip_and_module_level_skip(root):
    res, _ = run_w(root, WorkersRunner(root, suite_out=xdist_output(root, skipped=[("tests/test_x.py", "why not", 1)])))
    assert any("unknown skip" in r for r in res.reasons)
    res, _ = run_w(root, WorkersRunner(root, suite_out=xdist_output(root, skipped=[(*IPC_SKIP, 4)])))
    assert any("more skips than allowed" in r for r in res.reasons)
    res, _ = run_w(root, WorkersRunner(root, suite_out=xdist_output(root, skipped=[(*IPC_SKIP, 1)])
                                       .replace("1 skipped", "2 skipped")))
    assert any("skip lines" in r for r in res.reasons)


def test_red_known_red_without_ticket_and_lint_and_dirty_and_wrong_import(root):
    res, _ = run_w(root, WorkersRunner(root), known=f"{KNOWN}\n")
    assert any("known-red" in r for r in res.reasons)
    res, _ = run_w(root, WorkersRunner(root, lint_rc=1))
    assert "lint" in res.reasons
    res, _ = run_w(root, WorkersRunner(root, import_path="/elsewhere/kicad_mcp/__init__.py"))
    assert any("not imported" in r for r in res.reasons)


# --------------------------------------------------------------------------- main(): the option and the modes


@pytest.mark.parametrize("value", ["0", "5", "8", "-1", "four", "2.5", ""])
def test_workers_outside_1_to_4_is_a_bad_argument(repo, capsys, value):
    rec = TimeoutRecorder(repo)
    with pytest.raises(SystemExit) as exc:
        run_main(repo, capsys, "--workers", value, runner=rec)
    assert exc.value.code == 2
    assert rec.timeouts == [], "nothing may run"


def test_main_with_workers_4_is_green_on_xdist_output_and_red_on_serial_output(repo, capsys):
    rc, lines = run_main(repo, capsys, "--workers", "4", runner=WorkersRunner(repo))
    assert rc == 0, lines[-1]
    assert lines[-1].endswith(": GREEN")
    rc, lines = run_main(repo, capsys, "--workers", "4", runner=FakeRunner(repo))
    assert rc == 1 and "workers line" in lines[-1]


def test_main_default_still_rejects_nothing_new_and_runs_serial(repo, capsys):
    rec = WorkersRunner(repo, suite_out=base.pytest_output(repo))
    rc, lines = run_main(repo, capsys, runner=rec)
    assert rc == 0, lines[-1]
    assert all("-n" not in c for c in rec.suite_cmds)
    assert not any("workers" in line for line in lines if line.startswith(("workers", "expected")))


def test_parallel_mode_has_its_own_timeout_unless_one_is_asked_for(repo, capsys):
    rec = TimeoutRecorder(repo, suite_out=xdist_output(repo))
    run_main(repo, capsys, "--workers", "4", runner=rec)
    assert ("suite", gate.SUITE_TIMEOUT_PARALLEL_S) in rec.timeouts
    rec = TimeoutRecorder(repo, suite_out=xdist_output(repo))
    run_main(repo, capsys, "--workers", "4", "--timeout", "1000", runner=rec)
    assert ("suite", 1000.0) in rec.timeouts
    rec = TimeoutRecorder(repo)
    run_main(repo, capsys, "--workers", "1", runner=rec)
    assert ("suite", gate.SUITE_TIMEOUT_S) in rec.timeouts


def test_parallel_timeout_is_never_below_the_serial_one_until_a_quiet_run_is_logged():
    # No quiet-machine parallel run exists: the only full run took 3772 s on a loaded machine, with no
    # speed-up over the serial one (3694 s). A claimed 3x basis of an unmeasured figure was the trap.
    assert gate.SUITE_MEASURED_PARALLEL_QUIET is False
    assert gate.SUITE_TIMEOUT_PARALLEL_S >= gate.SUITE_TIMEOUT_S
    assert gate.SUITE_MEASURED_PARALLEL_S == 3772
    assert "no speed-up shown" in gate.SUITE_MEASURED_PARALLEL_WHEN
    assert gate.MAX_WORKERS == 4
    source = (ROOT / "scripts" / "batch_gate.py").read_text(encoding="utf-8")
    assert "TO RE-BASE" in source


def test_expected_duration_names_the_mode_and_the_worst_case_follows(repo, capsys):
    rc, lines = run_main(repo, capsys, "--workers", "4", runner=WorkersRunner(repo))
    expected = next(line for line in lines if line.startswith("expected duration: "))
    worst = gate.worst_case_s(gate.SUITE_TIMEOUT_PARALLEL_S)
    assert "--workers 4" in expected and "pytest-xdist" in expected
    assert f"the suite times out at {gate.SUITE_TIMEOUT_PARALLEL_S} s" in expected
    assert f"at worst about {round(worst / 60)} min" in expected
    assert "not yet measured on a quiet machine" in expected and "no expected duration yet" in expected
    assert "about 22 min" not in expected and "1250" not in expected, "no duration that no run produced"
    serial = gate.expected_duration(gate.SUITE_TIMEOUT_S)
    assert "workers" not in serial, "the serial line stays as it was"
    assert any(line.startswith("workers: 4") for line in lines)


def test_the_serial_expected_duration_line_is_byte_for_byte_the_old_one():
    worst = gate.worst_case_s(gate.SUITE_TIMEOUT_S)
    assert gate.expected_duration(gate.SUITE_TIMEOUT_S) == (
        f"expected duration: about {round((gate.SUITE_MEASURED_S + gate.OVERHEAD_S) / 60)} min (pytest took up to"
        f" {gate.SUITE_MEASURED_S} s, {gate.SUITE_MEASURED_WHEN}); the suite times out at"
        f" {gate.SUITE_TIMEOUT_S} s and the gate ends at worst about {round(worst / 60)} min after its start."
        " A run still going after that has hung: stop it and escalate (fork playbook P11, docs/TESTING.md)")


def test_the_times_file_names_of_the_workers_are_known_to_the_log_cleanup():
    assert gate._LOG_FILE.fullmatch("abcdef1-20261104-101010-42.times.out")
    assert gate._LOG_FILE.fullmatch("abcdef1-20261104-101010-42.times-gw3.out")
    assert not gate._LOG_FILE.fullmatch("abcdef1-20261104-101010-42.times-x.out")


# --------------------------------------------------------------------------- real mini project under xdist

MINI_TESTS = """\
import pytest


def test_ok1(): pass
def test_ok2(): pass
def test_ok3(): pass
def test_ok4(): pass
def test_ok5(): pass
def test_ok6(): pass


@pytest.mark.network
def test_net(): pass


@pytest.mark.skip(reason="no thing")
def test_skip(): pass
"""


def _mini(tmp_path, *args, times=None):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\nmarkers = ["network: x"]\n',
                                             encoding="utf-8")
    (tmp_path / "conftest.py").write_text("from tests._test_times import *  # noqa: F403\n", encoding="utf-8")
    (tmp_path / "test_mini.py").write_text(MINI_TESTS, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("KICAD_MCP_TEST_TIMES", "PYTEST_ADDOPTS")}
    env["PYTHONPATH"] = str(ROOT)
    if times is not None:
        env["KICAD_MCP_TEST_TIMES"] = str(times)
    return subprocess.run([sys.executable, "-m", "pytest", "-m", "not network", "-rfEs", "-p", "no:cacheprovider",
                           *args], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)


def test_a_real_xdist_run_of_the_gates_command_shape_is_read_as_observed(tmp_path):
    done = _mini(tmp_path, "-n", "2", "--dist", "load")
    out = done.stdout
    assert done.returncode == 0, out
    assert gate.workers_line(out) == (2, 7), out  # 6 passes and 1 skip; the network test is deselected
    assert gate.collected_line(out) is None
    assert gate.final_summary(out) is not None and "deselected" not in gate.final_summary(out)
    assert gate.summary_count(gate.final_summary(out), "passed") == 6
    assert sum(gate.skip_counts(out).values()) == 1


def test_per_test_times_under_workers_are_one_file_per_worker_and_the_controller_writes_none(tmp_path):
    times = tmp_path / "t.times.out"
    done = _mini(tmp_path, "-n", "2", "--dist", "load", times=times)
    assert done.returncode == 0, done.stdout
    assert not times.exists(), "the controller must not write the base file (every event was written twice)"
    files = sorted(tmp_path.glob("t.times-gw*.out"))
    assert files, list(tmp_path.iterdir())
    events = [line for f in files for line in f.read_text(encoding="utf-8").splitlines()]
    starts = [e.split(" ", 2)[2] for e in events if " START " in e]
    ends = [e.split(" ", 2)[2] for e in events if " END " in e]
    assert sorted(starts) == sorted(ends)
    assert len(starts) == 7 and len(set(starts)) == 7, "each of the 7 selected tests exactly once"


def test_per_test_times_of_a_serial_run_keep_the_single_file(tmp_path):
    times = tmp_path / "t.times.out"
    done = _mini(tmp_path, times=times)
    assert done.returncode == 0, done.stdout
    assert len(times.read_text(encoding="utf-8").splitlines()) == 14
    assert not list(tmp_path.glob("t.times-gw*.out"))


# --------------------------------------------------------------------------- the texts that state the option


def test_the_docstring_testing_md_and_p11_state_the_option_and_its_figures():
    doc = " ".join((gate.__doc__ or "").split())
    testing = base._prose(ROOT / "docs/TESTING.md")
    playbook = base._prose(ROOT / "docs" / "playbook" / "PROJECT-KICAD-MCP-PLAYBOOK.md")
    p11 = playbook.split("**Project rule P11:", 1)[1].split("**Project rule P12", 1)[0]
    for name, text in (("docstring", doc), ("docs/TESTING.md", testing), ("P11", p11)):
        assert "--workers" in text and "-n N --dist load" in text.replace("`", ""), name
        assert "addopts" in text, name
        assert "SUITE_TIMEOUT_PARALLEL_S" in text and "SUITE_MEASURED_PARALLEL_S" in text, name
    assert f"`SUITE_MEASURED_PARALLEL_S` ({gate.SUITE_MEASURED_PARALLEL_S} s" in testing
    assert f"`SUITE_TIMEOUT_PARALLEL_S` ({gate.SUITE_TIMEOUT_PARALLEL_S} s" in testing
    assert f"`SUITE_MEASURED_PARALLEL_S` ({gate.SUITE_MEASURED_PARALLEL_S} s" in p11
    assert f"`SUITE_TIMEOUT_PARALLEL_S` ({gate.SUITE_TIMEOUT_PARALLEL_S} s" in p11


RELOAD_TESTS = """\
import importlib

import tests._test_times as t


def test_a():
    importlib.reload(t)


def test_b(): pass
def test_c(): pass
def test_d(): pass
"""


def test_a_test_that_reloads_the_hook_module_keeps_the_per_worker_file(tmp_path):
    # tests/test_krt_required.py and tests/test_kicad_demos.py load the conftest again inside a worker;
    # module globals set at the session start were lost, and the worker then wrote to the base file
    # (found in the first --workers 4 gate run, 2026-11-05).
    times = tmp_path / "t.times.out"
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text("from tests._test_times import *  # noqa: F403\n", encoding="utf-8")
    (tmp_path / "test_reload.py").write_text(RELOAD_TESTS, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k not in ("KICAD_MCP_TEST_TIMES", "PYTEST_ADDOPTS")}
    env["PYTHONPATH"] = str(ROOT)
    env["KICAD_MCP_TEST_TIMES"] = str(times)
    done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-n", "2", "--dist", "load"],
                          cwd=tmp_path, env=env, capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, done.stdout
    assert not times.exists(), times.read_text(encoding="utf-8")
    events = [line for f in tmp_path.glob("t.times-gw*.out") for line in f.read_text(encoding="utf-8").splitlines()]
    assert len(events) == 8, events


# --------------------------------------------------------------------------- the M check is the only guard


def test_red_fewer_tests_selected_and_run_is_caught_by_the_selection_check_alone(root):
    # -k, --ignore, --deselect or a PYTEST_ADDOPTS filter: xdist prints no deselected count, so under
    # workers only M against the collections reveals a silently reduced run (review of 154, B1).
    out = xdist_output(root, passed=300)  # M = 300 and 300 passed: the ran-vs-selected check agrees
    res, _ = run_w(root, WorkersRunner(root, suite_out=out, all_out=ALL_COLLECTED))  # 403 collected, 3 network
    assert res.reasons == ["300 tests selected by the workers, but 403 collected less 3 network-marked is 400"]


def test_red_more_tests_selected_than_collected_less_network_is_caught_alone(root):
    out = xdist_output(root, passed=450)
    res, _ = run_w(root, WorkersRunner(root, suite_out=out, all_out=ALL_COLLECTED))
    assert res.reasons == ["450 tests selected by the workers, but 403 collected less 3 network-marked is 400"]


# --------------------------------------------------------------------------- the role guard of the times hook


def test_a_role_inherited_from_another_process_is_ignored_by_a_serial_child(tmp_path):
    # F1: a nested pytest subprocess inherits KICAD_MCP_TEST_TIMES_ROLE of its parent worker; the pid
    # in it differs, so the child writes the base file, not the parent's worker file.
    times = tmp_path / "t.times.out"
    (tmp_path / "pyproject.toml").write_text("[tool.pytest.ini_options]\n", encoding="utf-8")
    (tmp_path / "conftest.py").write_text("from tests._test_times import *  # noqa: F403\n", encoding="utf-8")
    (tmp_path / "test_x.py").write_text("def test_x(): pass\n", encoding="utf-8")
    for role in ("gw0", "controller"):
        env = {k: v for k, v in os.environ.items() if k not in ("KICAD_MCP_TEST_TIMES", "PYTEST_ADDOPTS")}
        env.update(PYTHONPATH=str(ROOT), KICAD_MCP_TEST_TIMES=str(times), KICAD_MCP_TEST_TIMES_ROLE=f"1:{role}")
        done = subprocess.run([sys.executable, "-m", "pytest", "-p", "no:cacheprovider"], cwd=tmp_path, env=env,
                              capture_output=True, text=True, timeout=300)
        assert done.returncode == 0, done.stdout
        assert len(times.read_text(encoding="utf-8").splitlines()) == 2, role
        assert not list(tmp_path.glob("t.times-gw*.out"))
        times.unlink()


def test_the_gate_does_not_pass_on_the_times_variables_of_an_outer_run(root, monkeypatch):
    monkeypatch.setenv("KICAD_MCP_TEST_TIMES_ROLE", "1:gw0")
    seen = []

    class Spy(FakeRunner):
        def __call__(self, cmd, env, cwd, timeout, out_path):
            seen.append(env.get("KICAD_MCP_TEST_TIMES_ROLE"))
            return super().__call__(cmd, env, cwd, timeout, out_path)

    res, _ = run(root, Spy(root))
    assert res.green, res.reasons
    assert seen and set(seen) == {None}


def test_the_role_guard_only_honours_a_role_set_by_this_very_process(monkeypatch, tmp_path):
    # F1, the guard itself: a child that never reaches its own session start (so never overwrites the
    # inherited variable) must still not take its parent's role.
    from tests import _test_times as t

    path = str(tmp_path / "x.times.out")
    monkeypatch.setenv(t.ENV, path)
    monkeypatch.setenv(t.ROLE_ENV, f"{os.getpid() + 1}:gw0")
    assert t._path() == path
    monkeypatch.setenv(t.ROLE_ENV, f"{os.getpid() + 1}:controller")
    assert t._path() == path
    monkeypatch.setenv(t.ROLE_ENV, f"{os.getpid()}:gw0")
    assert t._path() == str(tmp_path / "x.times-gw0.out")
    monkeypatch.setenv(t.ROLE_ENV, f"{os.getpid()}:controller")
    assert t._path() is None
    monkeypatch.delenv(t.ROLE_ENV)
    assert t._path() == path
