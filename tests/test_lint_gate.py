"""Build ticket 45: the shrink-only ruff baseline (`scripts/lint_gate.py`).

Ruff itself runs in the local batch gate (`scripts/batch_gate.py`), not here: the suite must not
depend on a linter being installed. These tests pin the comparison logic and the baseline file.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("lint_gate", ROOT / "scripts" / "lint_gate.py")
lint_gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lint_gate)


def _finding(path: str, code: str | None, row: int = 1, root: Path = ROOT) -> dict:
    return {
        "filename": str(root / path),
        "code": code,
        "location": {"row": row, "column": 1},
        "message": "msg",
    }


def test_counts_are_keyed_by_relative_posix_path_and_code():
    counts, lines = lint_gate.count_findings(
        [_finding("src/a.py", "F401", 3), _finding("src/a.py", "F401", 9), _finding("tests/b.py", None)]
    )
    assert counts == Counter({("src/a.py", "F401"): 2, ("tests/b.py", "syntax-error"): 1})
    assert lines[("src/a.py", "F401")][1].startswith("src/a.py:9:1: F401")


def test_new_finding_grows_and_fixed_finding_shrinks():
    frozen = Counter({("a.py", "F401"): 2, ("b.py", "B905"): 1})
    current = Counter({("a.py", "F401"): 1, ("b.py", "B905"): 1, ("c.py", "F821"): 1})
    grown, shrunk = lint_gate.compare(frozen, current)
    assert grown == [("c.py", "F821")]
    assert shrunk == [("a.py", "F401")]


def test_a_line_move_is_neither_new_nor_fixed():
    frozen = Counter({("a.py", "F401"): 1})
    moved, _ = lint_gate.count_findings([_finding("a.py", "F401", row=40)])
    assert lint_gate.compare(frozen, Counter({("a.py", "F401"): 1})) == ([], [])
    assert lint_gate.compare(frozen, moved) == ([], [])


def test_shrink_lowers_but_never_raises_or_adds():
    frozen = Counter({("a.py", "F401"): 2, ("b.py", "B905"): 1, ("gone.py", "E741"): 3})
    current = Counter({("a.py", "F401"): 1, ("b.py", "B905"): 5, ("new.py", "F821"): 1})
    lowered = lint_gate.shrink(frozen, current)
    assert lowered == Counter({("a.py", "F401"): 1, ("b.py", "B905"): 1})
    grown, shrunk = lint_gate.compare(lowered, current)
    assert grown == [("b.py", "B905"), ("new.py", "F821")]  # still failing after a shrink
    assert shrunk == []


def test_nest_and_flatten_round_trip_and_drop_zero_counts():
    counts = Counter({("a.py", "F401"): 2, ("a.py", "B905"): 0, ("b.py", "E741"): 1})
    nested = lint_gate.nest(counts)
    assert nested == {"a.py": {"F401": 2}, "b.py": {"E741": 1}}
    assert lint_gate.flatten({"findings": nested}) == Counter({("a.py", "F401"): 2, ("b.py", "E741"): 1})


def test_pin_is_read_from_the_dev_extra(tmp_path):
    p = tmp_path / "pyproject.toml"
    p.write_text('[project.optional-dependencies]\ndev = ["pytest>=8", "ruff==1.2.3"]\n', encoding="utf-8")
    assert lint_gate.pinned_ruff_version(p) == "1.2.3"
    p.write_text('[project.optional-dependencies]\ndev = ["ruff>=1.2"]\n', encoding="utf-8")
    with pytest.raises(ValueError):
        lint_gate.pinned_ruff_version(p)


def test_committed_baseline_matches_the_pin_and_is_well_formed():
    baseline = json.loads((ROOT / "scripts" / "ruff-baseline.json").read_text(encoding="utf-8"))
    assert baseline["ruff"] == lint_gate.pinned_ruff_version()
    assert baseline["findings"], "an empty baseline would make the file pointless; delete it instead"
    for path, codes in baseline["findings"].items():
        assert "\\" not in path and not Path(path).is_absolute(), path
        for code, n in codes.items():
            assert isinstance(n, int) and n > 0, (path, code, n)


def test_pyproject_selects_an_explicit_rule_set():
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["ruff"]["lint"]["select"], "without select, a ruff upgrade changes the rules"


# --------------------------------------------------------------------------- main() with a fake ruff
# Review round 1: these branches of main() had surviving mutants.

FAKE_RUFF = '''
import json, os, sys, time
args = sys.argv[1:]
with open(os.environ["FAKE_RUFF_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(args) + chr(10))
if args == ["--version"]:
    print("ruff " + os.environ.get("FAKE_RUFF_VERSION", "1.2.3"))
    sys.exit(0)
time.sleep(float(os.environ.get("FAKE_RUFF_SLEEP", "0")))
sys.stdout.write(os.environ.get("FAKE_RUFF_OUT", "[]"))
sys.exit(int(os.environ.get("FAKE_RUFF_RC", "0")))
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "scripts").mkdir(parents=True)
    cfg = lint_gate.EXPECTED_RUFF_CONFIG
    select = ", ".join(f'"{c}"' for c in cfg["lint"]["select"])
    (root / "pyproject.toml").write_text(
        '[project]\nname = "x"\n[project.optional-dependencies]\ndev = ["ruff==1.2.3"]\n'
        f'[tool.ruff]\ntarget-version = "{cfg["target-version"]}"\nextend-exclude = {json.dumps(cfg["extend-exclude"])}\n'
        f'[tool.ruff.lint]\nselect = [{select}]\n',
        encoding="utf-8",
    )
    (root / "scripts" / "ruff-baseline.json").write_text(
        json.dumps({"ruff": "1.2.3", "findings": {"src/a.py": {"F401": 1}}}), encoding="utf-8"
    )
    script = tmp_path / "fake_ruff.py"
    script.write_text(FAKE_RUFF, encoding="utf-8")
    log = tmp_path / "ruff.log"
    monkeypatch.setenv("FAKE_RUFF_LOG", str(log))
    one = json.dumps([_finding("src/a.py", "F401", root=root)])
    monkeypatch.setenv("FAKE_RUFF_OUT", one)

    def run(*argv, timeout=60):
        return lint_gate.main(list(argv), root=root, ruff_cmd=[sys.executable, str(script)], timeout=timeout)

    run.root, run.log = root, log
    return run


def test_main_ok_and_runs_ruff_with_the_pinned_config_ignoring_noqa(fake, capsys):
    assert fake() == 0
    assert "lint: OK" in capsys.readouterr().out
    calls = [json.loads(line) for line in fake.log.read_text(encoding="utf-8").splitlines()]
    check = calls[-1]
    assert check[0] == "check"
    # A nested ruff.toml must not change the rules, and a noqa comment must not hide a finding.
    assert check[check.index("--config") + 1] == str(fake.root / "pyproject.toml")
    assert "--ignore-noqa" in check


def test_main_refuses_another_ruff_version(fake, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RUFF_VERSION", "9.9.9")
    assert fake() == 1
    assert "pins ruff==1.2.3" in capsys.readouterr().out


def test_main_fails_on_a_new_finding(fake, monkeypatch):
    two = json.dumps([_finding("src/a.py", "F401", root=fake.root), _finding("src/b.py", "F821", root=fake.root)])
    monkeypatch.setenv("FAKE_RUFF_OUT", two)
    assert fake() == 1


def test_main_fails_on_a_fixed_finding_until_the_baseline_shrinks(fake, monkeypatch):
    monkeypatch.setenv("FAKE_RUFF_OUT", "[]")
    assert fake() == 1
    assert fake("--shrink") == 0
    assert json.loads((fake.root / "scripts" / "ruff-baseline.json").read_text())["findings"] == {}


@pytest.mark.parametrize("rc", [1, 2])
def test_main_fails_when_ruff_check_exits_non_zero(fake, monkeypatch, rc):
    monkeypatch.setenv("FAKE_RUFF_RC", str(rc))
    assert fake() == 1


@pytest.mark.parametrize("out", ["{}", "not json", ""])
def test_main_fails_on_unreadable_ruff_output(fake, monkeypatch, out):
    monkeypatch.setenv("FAKE_RUFF_OUT", out)
    assert fake() == 1


def test_main_fails_when_ruff_times_out(fake, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_RUFF_SLEEP", "5")
    assert fake(timeout=1) == 1
    assert "timed out" in capsys.readouterr().out


def test_main_fails_when_ruff_cannot_start(fake, capsys):
    rc = lint_gate.main([], root=fake.root, ruff_cmd=[str(fake.root / "no-such-ruff.exe")], timeout=10)
    assert rc == 1
    assert "cannot be started" in capsys.readouterr().out


@pytest.mark.parametrize(
    "extra",
    [
        'ignore = ["F401"]\n',
        'exclude = ["src"]\n',
        'extend-exclude = ["src"]\n',
        '[tool.ruff.lint.per-file-ignores]\n"src/*" = ["F"]\n',
    ],
)
def test_main_refuses_a_ruff_config_other_than_the_reviewed_one(fake, extra, capsys):
    p = fake.root / "pyproject.toml"
    text = p.read_text(encoding="utf-8")
    if extra.startswith("extend-exclude"):  # replace the reviewed line (a duplicate key is not a case)
        text = text.replace('extend-exclude = ["third_party"]\n', "")
    if extra.startswith("["):
        text += extra
    else:
        text = text.replace("[tool.ruff.lint]\n", f"{extra}[tool.ruff.lint]\n")
    p.write_text(text, encoding="utf-8")
    assert fake() == 1
    assert "[tool.ruff]" in capsys.readouterr().out


def test_committed_pyproject_has_exactly_the_reviewed_ruff_config():
    import tomllib

    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert data["tool"]["ruff"] == lint_gate.EXPECTED_RUFF_CONFIG


def test_ruff_skips_third_party_submodules_but_still_lints_our_own_code(tmp_path):
    """Publish-prep ticket 03b: a finding inside third_party/ is another project's; one outside is ours."""
    import subprocess

    (tmp_path / "pyproject.toml").write_text((ROOT / "pyproject.toml").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "third_party" / "lib").mkdir(parents=True)
    (tmp_path / "third_party" / "lib" / "theirs.py").write_text("import os\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ours.py").write_text("import os\n", encoding="utf-8")

    cmd = lint_gate.ruff_command() + ["check", "--no-cache", "--config", "pyproject.toml", "--ignore-noqa",
                                      "--output-format", "json", "--exit-zero", "."]
    done = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert done.returncode == 0, done.stderr
    files = sorted(Path(f["filename"]).relative_to(tmp_path).as_posix() for f in json.loads(done.stdout))
    assert files == ["src/ours.py"]
