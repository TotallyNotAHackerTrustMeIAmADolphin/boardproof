"""The ngspice guard (spice-simulation build ticket 02): a live test skips without ngspice in an
ad-hoc run and fails when ``NGSPICE_REQUIRED`` is set (the gate run), the pinned version is the
one the recorded fixtures came from, and no other test module decides an ngspice skip itself."""
from __future__ import annotations

import ast
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
from _pytest.outcomes import Failed, Skipped

from tests import _ngspice


def _gone() -> None:
    return None


@pytest.mark.parametrize("value", ["1", "yes", "true"])
def test_required_flag_turns_a_missing_ngspice_into_a_failure(value):
    with pytest.raises(Failed, match="NGSPICE_REQUIRED is set"):
        _ngspice.exe_or_missing({_ngspice.REQUIRED_FLAG: value}, find=_gone)


@pytest.mark.parametrize("env", [{}, {"NGSPICE_REQUIRED": ""}, {"NGSPICE_REQUIRED": "0"}])
def test_without_the_flag_a_missing_ngspice_skips_and_says_how_the_gate_treats_it(env):
    with pytest.raises(Skipped) as caught:
        _ngspice.exe_or_missing(env, find=_gone)
    assert _ngspice.REQUIRED_FLAG in str(caught.value)
    assert _ngspice.PATH_VAR in str(caught.value)


def test_a_wrong_ngspice_path_is_reported_not_bypassed():
    with pytest.raises(Failed, match="NGSPICE_PATH='nowhere.exe' is not a file"):
        _ngspice.exe_or_missing({_ngspice.REQUIRED_FLAG: "1", "NGSPICE_PATH": "nowhere.exe"}, find=_gone)


def test_a_found_ngspice_is_returned(tmp_path):
    exe = tmp_path / "ngspice_con.exe"
    assert _ngspice.exe_or_missing({_ngspice.REQUIRED_FLAG: "1"}, find=lambda: exe) == exe


def test_the_installed_ngspice_is_the_pinned_version(ngspice_exe: Path):
    """The recorded fixtures in tests/fixtures/spice/ come from this version; a different one must
    be a decision (re-record, then change PINNED_VERSION), not a drift."""
    out = subprocess.run([str(ngspice_exe), "--version"], capture_output=True, text=True, timeout=30, check=False)
    assert f"ngspice-{_ngspice.PINNED_VERSION}" in out.stdout, out.stdout[:300]


def _stray_ngspice_skips(directory: Path, *, allowed: set[str]) -> list[str]:
    """Lines in the test modules of ``directory`` that decide an ngspice skip outside ``allowed``."""
    offenders = []
    for path in sorted(directory.glob("*.py")):
        if path.name in allowed:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"skipif|pytest\.skip|importorskip", line) and re.search(r"ngspice", line, re.IGNORECASE):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    return offenders


def test_no_other_test_module_decides_an_ngspice_skip_itself():
    """Every ngspice availability decision goes through ``tests/_ngspice.py`` (the ``ngspice_exe``
    fixture), so the allowed skip below is the only one and a private skipif cannot hide another."""
    assert _stray_ngspice_skips(Path(__file__).parent, allowed={"_ngspice.py", Path(__file__).name}) == []


def test_mutant_a_stray_ngspice_skip_in_another_module_is_caught(tmp_path):
    stray = ["import pytest", "", "@pytest.mark.skipif(True, reason='no ngspice here')", "def test_x(): ..."]
    (tmp_path / "test_other.py").write_text("\n".join(stray) + "\n", encoding="utf-8")
    (tmp_path / "test_clean.py").write_text("def test_y(): ...\n", encoding="utf-8")
    found = _stray_ngspice_skips(tmp_path, allowed=set())
    assert len(found) == 1 and found[0].startswith("test_other.py:3:")


# --- ngspice is optional while the simulation map is parked (owner decision 2026-10-30, issue 40) ---
#
# The live tests skip without ngspice. The gate no longer sets NGSPICE_REQUIRED, so the skip must be
# visible and counted: `scripts/known-skips.toml` holds one entry per file with a `max`, and the
# gate is RED on any other skip. These tests pin that the entries exist, name the ticket and match
# the number of tests that take the `ngspice_exe` fixture (a new live test is a deliberate raise).

_ROOT = Path(__file__).resolve().parents[1]
_SKIP_FILES = {"tests/test_simulation_toolset.py", "tests/test_ngspice_required.py"}


def _ngspice_skip_message() -> str:
    with pytest.raises(Skipped) as caught:
        _ngspice.exe_or_missing({}, find=_gone)
    return str(caught.value)


def _fixture_users(relative: str) -> int:
    tree = ast.parse((_ROOT / relative).read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name.startswith("test_")
        and any(arg.arg == "ngspice_exe" for arg in node.args.args)
    )


def _ngspice_entries() -> list[dict]:
    data = tomllib.loads((_ROOT / "scripts" / "known-skips.toml").read_text(encoding="utf-8"))
    return [e for e in data["skip"] if "ngspice" in e["reason"].lower()]


def test_the_gate_run_does_not_require_ngspice():
    """The batch gate neither sets NGSPICE_REQUIRED nor is RED for a missing NGSPICE_PATH."""
    source = (_ROOT / "scripts" / "batch_gate.py").read_text(encoding="utf-8")
    assert 'env["NGSPICE_REQUIRED"]' not in source
    assert "NGSPICE_PATH unset" not in source


def test_each_ngspice_skip_file_has_a_ticket_referenced_counted_allowance():
    entries = _ngspice_entries()
    assert {e["file"] for e in entries} == _SKIP_FILES
    message = _ngspice_skip_message()
    for entry in entries:
        assert message.startswith(entry["reason"]), (entry["reason"], message[:80])
        assert "#40" in entry["why"], entry["why"]
        assert entry["max"] == _fixture_users(entry["file"]), entry


def test_the_skip_message_names_the_optional_status_and_the_ticket():
    message = _ngspice_skip_message()
    assert "optional" in message and "#40" in message


@pytest.mark.parametrize("process_flag", ["1", None])
def test_an_explicit_env_wins_over_the_process_environment(monkeypatch, process_flag):
    """The gate sets NGSPICE_REQUIRED=1 in the real environment; a caller passing its own env must
    still get that env's answer from ``exe_or_missing`` (it once ignored ``env`` for the decision)."""
    if process_flag is None:
        monkeypatch.delenv(_ngspice.REQUIRED_FLAG, raising=False)
    else:
        monkeypatch.setenv(_ngspice.REQUIRED_FLAG, process_flag)
    with pytest.raises(Skipped):
        _ngspice.exe_or_missing({}, find=_gone)
    with pytest.raises(Failed, match="NGSPICE_REQUIRED is set"):
        _ngspice.exe_or_missing({_ngspice.REQUIRED_FLAG: "1"}, find=_gone)
