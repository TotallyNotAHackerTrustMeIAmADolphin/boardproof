"""Fresh-machine install facts that live in pyproject.toml (build ticket 33)."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from kicad_mcp.routers import kicad_routing_tools as krt_tools

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def _config() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_the_krt_extra_declares_every_package_the_krt_probe_requires():
    extra = _config()["project"]["optional-dependencies"].get("krt", [])
    declared = {re.split(r"[<>=!~;\[ ]", req, maxsplit=1)[0].lower() for req in extra}
    required = {pip_name.lower() for pip_name, _module in krt_tools.REQUIRED_PACKAGES}
    assert "scipy" in required
    assert required <= declared, f"missing from the krt extra: {sorted(required - declared)}"


def test_pytest_puts_the_repository_root_on_sys_path_for_the_tests_package_imports():
    # tests/test_copilot_toolset.py does `from tests.test_ipc import ...`; the `pytest`
    # console script does not add the repository root by itself.
    assert "." in _config()["tool"]["pytest"]["ini_options"].get("pythonpath", [])
