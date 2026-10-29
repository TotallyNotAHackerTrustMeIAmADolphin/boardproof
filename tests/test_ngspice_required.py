"""The ngspice guard (spice-simulation build ticket 02): a live test skips without ngspice in an
ad-hoc run and fails when ``NGSPICE_REQUIRED`` is set (the gate run), the pinned version is the
one the recorded fixtures came from, and no other test module decides an ngspice skip itself."""
from __future__ import annotations

import re
import subprocess
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


def test_no_other_test_module_decides_an_ngspice_skip_itself():
    """Every ngspice availability decision goes through ``tests/_ngspice.py`` (the ``ngspice_exe``
    fixture), so the gate's NGSPICE_REQUIRED cannot be bypassed by a private skipif."""
    offenders = []
    for path in sorted(Path(__file__).parent.glob("*.py")):
        if path.name in {"_ngspice.py", Path(__file__).name}:
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r"skipif|pytest\.skip|importorskip", line) and re.search(r"ngspice", line, re.IGNORECASE):
                offenders.append(f"{path.name}:{number}: {line.strip()}")
    assert offenders == []


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
