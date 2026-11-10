"""Tests for fetch_part_assets (archived backlog P1-05).

The "real" tests hit the live EasyEDA API via the easyeda2kicad CLI --
deliberate per the ticket: this is a thin wrapper whose only real risk is
the actual conversion pipeline, which mocking would hide entirely. LCSC
C2040 (RP2040) is the tool's own canonical example part (used in its
--help text), chosen for stability.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp.sourcing.asset_fetch import fetch_part_assets
from tests._hermetic_git import run_git


def test_fetch_part_assets_rejects_malformed_lcsc_id(tmp_path):
    result = fetch_part_assets("not-an-lcsc-id", tmp_path)
    assert result["status"] == "error"
    assert "not a valid LCSC part number" in result["reason"]


def test_fetch_part_assets_rejects_path_traversal_attempt(tmp_path):
    # Security regression test (independent review, 2026-09-02): this used
    # to pass validation (only checked startswith("C")) and, because
    # `output_dir / lcsc_id` discards output_dir when the right side looks
    # absolute, would have written outside tmp_path entirely.
    result = fetch_part_assets("C:/Windows/Temp/evil", tmp_path)
    assert result["status"] == "error"
    assert "not a valid LCSC part number" in result["reason"]


@pytest.mark.network
def test_fetch_part_assets_real_part(tmp_path):
    result = fetch_part_assets("C2040", tmp_path)
    assert result["status"] == "ok", result
    assert result["symbol_path"] and Path(result["symbol_path"]).is_file()
    assert result["footprint_dir"] and Path(result["footprint_dir"]).is_dir()
    assert result["model_3d_dir"] and Path(result["model_3d_dir"]).is_dir()
    assert any(Path(result["footprint_dir"]).glob("*.kicad_mod"))


def test_fetch_part_assets_nonexistent_lcsc_id_is_error_not_exception(tmp_path):
    result = fetch_part_assets("C999999999", tmp_path)
    assert result["status"] == "error"
    assert result["lcsc_id"] == "C999999999"


# Issue 67: easyeda2kicad is a core dependency, and a venv without it is reported actionably.
MISSING_MODULE_REASON = (
    "easyeda2kicad is not installed for this interpreter; install it with "
    "`python -m pip install \"easyeda2kicad>=1.0.1\"` (or reinstall boardproof) and retry"
)


def test_missing_module_gives_actionable_incomplete_result(tmp_path, monkeypatch):
    import importlib.util

    import kicad_mcp.sourcing.asset_fetch as af

    real_find_spec = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a, **k: None if name == "easyeda2kicad" else real_find_spec(name, *a, **k))

    def _no_subprocess(*a, **k):
        raise AssertionError("the subprocess must not start when the module is missing")

    monkeypatch.setattr(af.subprocess, "run", _no_subprocess)
    result = fetch_part_assets("C2040", tmp_path)
    assert result["status"] == "incomplete"
    assert result["lcsc_id"] == "C2040"
    assert result["reason"] == MISSING_MODULE_REASON
    assert "No module named" not in result["reason"]
    assert result["symbol_path"] is None and result["footprint_dir"] is None and result["model_3d_dir"] is None
    assert not (tmp_path / "C2040").exists()


def test_invalid_id_is_still_checked_before_the_module(tmp_path, monkeypatch):
    import importlib.util

    monkeypatch.setattr(importlib.util, "find_spec", lambda *a, **k: None)
    assert fetch_part_assets("nope", tmp_path)["status"] == "error"


def test_easyeda2kicad_runs_with_the_project_interpreter():
    """The cheap real check: a venv without the package shows up as a skip reason."""
    import importlib.util
    import subprocess
    import sys

    if importlib.util.find_spec("easyeda2kicad") is None:
        pytest.skip("easyeda2kicad is not installed in this interpreter (core dependency, issue 67)")
    proc = subprocess.run([sys.executable, "-m", "easyeda2kicad", "--help"], capture_output=True, text=True, timeout=60, check=False)
    assert proc.returncode == 0, proc.stderr
    assert "lcsc_id" in proc.stdout


def test_pyproject_lists_easyeda2kicad_as_core_dependency_only():
    import tomllib
    from pathlib import Path

    data = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    assert any(d.replace(" ", "").startswith("easyeda2kicad>=1.0.1") for d in data["project"]["dependencies"])
    assert "assets" not in data["project"].get("optional-dependencies", {})


def test_readme_and_third_party_agree_it_is_core():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    readme = " ".join((root / "README.md").read_text(encoding="utf-8").split())
    third = " ".join((root / "THIRD_PARTY.md").read_text(encoding="utf-8").split())
    assert "`assets`" not in readme and "[assets]" not in readme and "(the `assets` extra)" not in readme
    assert "core dependency" in readme and "easyeda2kicad" in readme
    assert "Optional (`[assets]` extra)" not in third
    assert "core dependency" in third and "subprocess" in third and "never imported" in third


def test_fetch_part_assets_description_pins_missing_module_sentence():
    import ast

    path = Path(__file__).resolve().parents[1] / "src" / "kicad_mcp" / "toolsets" / "sourcing.py"
    tree = ast.parse(path.read_text(encoding="utf-8"))
    strings = [" ".join(n.value.split()) for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    sentence = "If the easyeda2kicad package is missing from the server's Python environment the result is status incomplete with the install command, and nothing is fetched."
    assert [s for s in strings if s.startswith("Retrieve a ready KiCad symbol/footprint/3D model") and sentence in s]


def test_no_tracked_doc_calls_easyeda2kicad_optional_or_an_assets_extra():

    root = Path(__file__).resolve().parents[1]
    files = run_git("ls-files", "*.md", cwd=root, text=True).stdout.split()
    for name in files:
        if name.startswith("third_party/"):
            continue
        text = " ".join((root / name).read_text(encoding="utf-8", errors="replace").split())
        assert "[assets]" not in text, name
        assert "`assets` extra" not in text, name
        assert "optional `[assets]`" not in text, name
    arch = " ".join((root / "docs/ARCHITECTURE.md").read_text(encoding="utf-8").split())
    assert "easyeda2kicad AGPL-3.0 (subprocess, core dependency)" in arch
