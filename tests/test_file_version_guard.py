"""Fail closed on KiCad file versions this project has not measured (GitHub issue 208).

A KiCad 11 nightly writes ``(version 20261031)`` boards in which a footprint's placement is no
longer ``(at x y a)``; the raw readers then returned wrong positions without an error
(docs/research-kicad11-nightly.md). The write layer and the raw readers now refuse a ``.kicad_pcb``
or ``.kicad_sch`` newer than ``write_layer.NEWEST_MEASURED_VERSION``, or without a readable version.

Test inputs: the committed rung-1 board and schematic are loaded through the write layer, their
``(version ...)`` token is changed with the typed setters, and ``faults.write_unchecked`` writes the
result (the layer's own ``save`` would refuse it: kicad-cli 10.0.7 cannot load a newer file). No
KiCad 11 file is vendored; the files are KiCad 10 files with only the version token changed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from boardproof import decoupling_check, raw_geometry, write_layer
from tests import _kicad_faults as faults

ROOT = Path(__file__).resolve().parents[1]
RUNG1 = ROOT / "evals" / "rung1" / "layout_run" / "07_poured"
BOARD = RUNG1 / "rung1_usbc_attiny1616.kicad_pcb"
SHEET = RUNG1 / "rung1_usbc_attiny1616.kicad_sch"

NEWEST = write_layer.NEWEST_MEASURED_VERSION
READERS = (
    raw_geometry.read_footprints,
    raw_geometry.read_zones,
    raw_geometry.read_board_outline,
    raw_geometry.read_origins,
)


def top_version(root):
    return next(c for c in root.children if not c.is_atom and c.name == "version")


def with_version(source: Path, dest: Path, version: int | None) -> Path:
    root = write_layer.load(source)
    node = top_version(root)
    if version is None:
        root.remove(node)
    else:
        write_layer.set_number(node, 0, version)
    return faults.write_unchecked(root, dest)


def test_the_newest_measured_versions_are_what_kicad_10_0_7_writes() -> None:
    assert NEWEST == {"kicad_pcb": 20260206, "kicad_sch": 20260306}


@pytest.mark.parametrize("kind,source", [("kicad_pcb", BOARD), ("kicad_sch", SHEET)])
def test_load_refuses_a_newer_version_and_names_both(tmp_path: Path, kind: str, source: Path) -> None:
    newer = with_version(source, tmp_path / source.name, NEWEST[kind] + 1)
    with pytest.raises(write_layer.UnsupportedFileVersionError) as caught:
        write_layer.load(newer)
    exc = caught.value
    assert isinstance(exc, write_layer.WriteLayerError) and exc.code == "unsupported_file_version"
    assert exc.details["version_found"] == NEWEST[kind] + 1
    assert exc.details["newest_accepted"] == NEWEST[kind]
    assert str(NEWEST[kind] + 1) in str(exc) and str(NEWEST[kind]) in str(exc)


@pytest.mark.parametrize("kind,source", [("kicad_pcb", BOARD), ("kicad_sch", SHEET)])
def test_the_newest_measured_version_itself_and_older_ones_load(tmp_path: Path, kind: str, source: Path) -> None:
    for version in (NEWEST[kind], 20231120):
        write_layer.load(with_version(source, tmp_path / f"{version}-{source.name}", version))


@pytest.mark.parametrize("source", [BOARD, SHEET])
def test_load_refuses_a_file_without_a_version(tmp_path: Path, source: Path) -> None:
    with pytest.raises(write_layer.UnsupportedFileVersionError) as caught:
        write_layer.load(with_version(source, tmp_path / source.name, None))
    assert caught.value.details["version_found"] is None


def test_load_refuses_a_version_that_is_not_a_number(tmp_path: Path) -> None:
    root = write_layer.load(BOARD)
    write_layer.set_string(top_version(root), 0, "soon")
    path = faults.write_unchecked(root, tmp_path / "x.kicad_pcb")
    with pytest.raises(write_layer.UnsupportedFileVersionError):
        write_layer.load(path)


@pytest.mark.parametrize("reader", READERS)
def test_every_raw_reader_refuses_a_newer_board_by_path_and_by_node(tmp_path: Path, reader) -> None:
    newer = with_version(BOARD, tmp_path / "n.kicad_pcb", NEWEST["kicad_pcb"] + 1)
    with pytest.raises(write_layer.UnsupportedFileVersionError):
        reader(newer)
    root = write_layer.load(BOARD)  # an already parsed tree is checked too
    write_layer.set_number(top_version(root), 0, NEWEST["kicad_pcb"] + 1)
    with pytest.raises(write_layer.UnsupportedFileVersionError):
        reader(root)


@pytest.mark.parametrize("reader", READERS)
def test_every_raw_reader_still_reads_the_committed_board(reader) -> None:
    reader(BOARD)


def test_library_files_are_not_version_checked(tmp_path: Path) -> None:
    root = write_layer.parse_string('(footprint "X" (version 29990101) (generator "t"))')
    write_layer.check_file_version(root)  # footprints and symbol libraries are out of scope


def test_a_tool_result_carries_the_named_error_not_a_crash_text(tmp_path: Path) -> None:
    newer = with_version(BOARD, tmp_path / "n.kicad_pcb", NEWEST["kicad_pcb"] + 1)
    result = decoupling_check.check_decoupling_placement(newer)
    assert result["status"] == "error"
    assert "unsupported_file_version" in result["reason"]
    assert str(NEWEST["kicad_pcb"] + 1) in result["reason"] and str(NEWEST["kicad_pcb"]) in result["reason"]


def test_the_constant_matches_what_kicad_cli_10_0_7_upgrades_to(tmp_path: Path) -> None:
    """The oracle: ``kicad-cli ... upgrade`` of a copy writes the newest format of the installed KiCad."""
    from boardproof import kicad_cli

    try:
        cli = kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001 - no KiCad here
        pytest.skip("kicad-cli not installed")
    for kind, sub, source in (("kicad_pcb", "pcb", BOARD), ("kicad_sch", "sch", SHEET)):
        copy = tmp_path / source.name
        shutil.copy(source, copy)
        subprocess.run([str(cli), sub, "upgrade", "--force", str(copy)], check=True, capture_output=True, timeout=120)
        assert top_version(write_layer.load(copy)).values[0] == NEWEST[kind]


def test_a_second_version_token_is_refused() -> None:
    """KiCad writes one (version); a second token must not let an older first one hide a newer one."""
    root = write_layer.parse_string("(kicad_pcb (version 20260101) (version 20990101))")
    with pytest.raises(write_layer.UnsupportedFileVersionError) as err:
        write_layer.check_file_version(root)
    assert err.value.code == "unsupported_file_version"
    write_layer.check_file_version(write_layer.parse_string("(kicad_pcb (version 20260101))"))
