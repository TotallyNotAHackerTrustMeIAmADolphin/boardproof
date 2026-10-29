"""Tests for src/kicad_mcp/kicad_cli.py, run against a real kicad-cli install."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp import kicad_cli

FIXTURES = Path(__file__).parent / "fixtures"
PCB_FIXTURE = FIXTURES / "simple_led.kicad_pcb"
SCH_FIXTURE = FIXTURES / "simple_led.kicad_sch"

def _kicad_cli_available() -> bool:
    try:
        kicad_cli.find_kicad_cli()
        return True
    except kicad_cli.KicadCliError:
        return False


requires_kicad_cli = pytest.mark.skipif(
    not _kicad_cli_available(), reason="kicad-cli not found on this machine"
)


@requires_kicad_cli
def test_find_kicad_cli_returns_existing_file() -> None:
    path = kicad_cli.find_kicad_cli()
    assert path.is_file()


@requires_kicad_cli
def test_run_drc_returns_parsed_json_report() -> None:
    report = kicad_cli.run_drc(PCB_FIXTURE)
    assert "violations" in report
    assert isinstance(report["violations"], list)


@requires_kicad_cli
def test_run_erc_returns_parsed_json_report() -> None:
    report = kicad_cli.run_erc(SCH_FIXTURE)
    assert "sheets" in report


@requires_kicad_cli
def test_export_svg_pcb_produces_layer_files(tmp_path) -> None:
    out_dir = kicad_cli.export_svg(PCB_FIXTURE, tmp_path / "pcb_svg")
    files = list(out_dir.glob("*.svg"))
    assert files, "expected at least one layer SVG to be produced"


@requires_kicad_cli
def test_export_svg_schematic_produces_a_file(tmp_path) -> None:
    out_dir = kicad_cli.export_svg(SCH_FIXTURE, tmp_path / "sch_svg")
    files = list(out_dir.glob("*.svg"))
    assert files, "expected at least one schematic SVG to be produced"


def test_find_kicad_cli_respects_env_override(monkeypatch, tmp_path) -> None:
    fake_cli = tmp_path / "kicad-cli.exe"
    fake_cli.write_text("not a real binary")
    monkeypatch.setenv("KICAD_CLI_PATH", str(fake_cli))
    assert kicad_cli.find_kicad_cli() == fake_cli


def test_find_kicad_cli_env_override_missing_file_raises(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("KICAD_CLI_PATH", str(tmp_path / "does_not_exist.exe"))
    with pytest.raises(kicad_cli.KicadCliError):
        kicad_cli.find_kicad_cli()


def test_find_kicad_cli_prefers_kicad_10_over_kicad_9(monkeypatch, tmp_path) -> None:
    """Regression for a real bug (found 2026-09-11 evaluating KiCadRoutingTools'
    own discovery code): plain lexical sort puts "9.0" ahead of "10.0" --
    '9' > '1' character-wise -- so a machine with both KiCad 9 and 10
    installed side by side would silently prefer the older one."""
    monkeypatch.delenv("KICAD_CLI_PATH", raising=False)
    monkeypatch.setattr(kicad_cli.shutil, "which", lambda _name: None)
    for version in ("9.0", "10.0"):
        d = tmp_path / "KiCad" / version / "bin"
        d.mkdir(parents=True)
        (d / "kicad-cli.exe").write_text("stub")
    monkeypatch.setattr(
        kicad_cli,
        "_CANDIDATE_GLOBS",
        {"win32": [str(tmp_path / "KiCad" / "*" / "bin" / "kicad-cli.exe")]},
    )
    monkeypatch.setattr(kicad_cli.sys, "platform", "win32")
    resolved = kicad_cli.find_kicad_cli()
    assert "10.0" in str(resolved), f"expected the 10.0 install, got {resolved}"


def test_export_svg_rejects_unsupported_extension(tmp_path) -> None:
    bogus = tmp_path / "board.txt"
    bogus.write_text("x")
    with pytest.raises(ValueError):
        kicad_cli.export_svg(bogus, tmp_path / "out")
