"""Tests for the schematic toolset (archived backlog P2-01..P2-06).

Builds a real, small schematic from scratch using the toolset's own
tools (not hand-written S-expressions), matching the golden-path
board's parts list (docs/TESTING.md): a USB-C connector, an ATtiny1616, a
decoupling capacitor, an LED with current-limit resistor, and a UPDI
header -- then confirms it's ERC-clean via the existing
`verification.sch_run_erc` tool.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp.toolsets import schematic as schematic_toolset
from kicad_mcp.toolsets import verification as verification_toolset

FIXTURE = Path(__file__).parent / "fixtures" / "simple_led.kicad_sch"


def _build_and_get_fns(mcp: MCPServer, module) -> dict:
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def sch_tools():
    mcp = MCPServer("test")
    return _build_and_get_fns(mcp, schematic_toolset)


@pytest.fixture
def verification_tools():
    mcp = MCPServer("test")
    return _build_and_get_fns(mcp, verification_toolset)


def test_build_registers_expected_tools(sch_tools):
    assert set(sch_tools) == {
        "add_symbols",
        "add_wires",
        "add_junctions",
        "add_labels",
        "assign_footprints",
        "fix_annotation",
        "check_connectivity",
    }


def test_add_symbols_missing_file_is_incomplete(sch_tools):
    result = sch_tools["add_symbols"]("/no/such/file.kicad_sch", [{"lib_id": "Device:R", "x": 0, "y": 0, "ref": "R1"}])
    assert result["status"] == "incomplete"


def test_add_symbols_on_real_fixture(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    result = sch_tools["add_symbols"](
        str(dest),
        [{"lib_id": "Device:R", "x": 150, "y": 100, "ref": "R99", "value": "1k", "footprint": "Resistor_SMD:R_0402_1005Metric"}],
    )
    assert result["status"] == "ok", result
    assert result["placed"] == ["R99"]

    from kicad_tools.schema import Schematic

    reloaded = Schematic.load(dest)
    assert reloaded.get_symbol("R99") is not None


def test_add_wires_and_junctions_on_real_fixture(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    wire_result = sch_tools["add_wires"](str(dest), [{"p1": [200, 100], "p2": [210, 100]}])
    assert wire_result["status"] == "ok"
    assert wire_result["count"] == 1

    junction_result = sch_tools["add_junctions"](str(dest), [[210, 100]])
    assert junction_result["status"] == "ok"
    assert junction_result["count"] == 1


def test_add_labels_on_real_fixture(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    result = sch_tools["add_labels"](
        str(dest),
        [
            {"kind": "label", "text": "TEST_NET", "x": 220, "y": 100},
            {"kind": "no_connect", "x": 230, "y": 100},
        ],
    )
    assert result["status"] == "ok", result
    assert result["count"] == 2


def test_add_labels_rejects_unknown_kind(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    result = sch_tools["add_labels"](str(dest), [{"kind": "bogus", "text": "X", "x": 0, "y": 0}])
    assert result["status"] == "error"


def test_check_connectivity_on_real_fixture(tmp_path, sch_tools):
    # Deliberately run against a *copy*: check_connectivity's netlist
    # cross-check shells out to `kicad-cli sch export netlist`, which writes
    # `<name>-netlist.kicad_net` next to its input. Pointed at FIXTURE
    # directly it rewrote the committed tests/fixtures/ netlist (timestamp
    # churn) on every full test run.
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    result = sch_tools["check_connectivity"](str(dest))
    assert result["status"] == "ok", result
    assert "unconnected_pins" in result
    assert isinstance(result["unconnected_count"], int)


def test_check_connectivity_missing_file_is_incomplete(sch_tools):
    result = sch_tools["check_connectivity"]("/no/such/file.kicad_sch")
    assert result["status"] == "incomplete"


def test_fix_annotation_on_real_fixture(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    result = sch_tools["fix_annotation"](str(dest))
    assert result["status"] == "ok", result
    assert "references_before" in result and "references_after" in result


def test_assign_footprints_on_real_fixture(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    from kicad_tools.schema import Schematic

    before = Schematic.load(dest)
    ref = next(s.reference for s in before.iter_symbols() if s.reference.startswith("R"))
    result = sch_tools["assign_footprints"](str(dest), {ref: "Resistor_SMD:R_0603_1608Metric"})
    assert result["status"] == "ok", result
    assert result["assigned"].get(ref) == "Resistor_SMD:R_0603_1608Metric"


# ---------------------------------------------------------------------------
# P2-06 -- orthogonal wire routing
#
# The bug this covers was found by a human looking at a rendered PDF, not by
# any automated check: a diagonal wire is electrically identical to an
# L-shaped one, so ERC and connectivity checks both pass either way. These
# tests therefore assert on wire *geometry* (no segment may be simultaneously
# non-horizontal and non-vertical), which is the property that was actually
# broken -- while separately asserting connectivity is preserved, so the fix
# can't have traded a readable schematic for a broken netlist.
# ---------------------------------------------------------------------------


def _wires(path: Path) -> list[tuple[float, float, float, float]]:
    from kicad_tools.schematic.models.schematic import Schematic

    return [(w.x1, w.y1, w.x2, w.y2) for w in Schematic.load(path).wires]


def _diagonals(path: Path) -> list[tuple[float, float, float, float]]:
    """Wires that are neither horizontal nor vertical.

    NB: the shared `simple_led.kicad_sch` fixture already contains one such
    wire from before this routing work, so tests that use it must compare
    against a baseline rather than asserting an empty list.
    """
    return [w for w in _wires(path) if w[0] != w[2] and w[1] != w[3]]


def test_plan_orthogonal_route_passes_through_axis_aligned_pairs():
    plan = schematic_toolset.plan_orthogonal_route
    assert plan((10, 20), (50, 20)) == [(10.0, 20.0), (50.0, 20.0)]
    assert plan((10, 20), (10, 60)) == [(10.0, 20.0), (10.0, 60.0)]


def test_plan_orthogonal_route_makes_an_l_when_one_corner_suffices():
    plan = schematic_toolset.plan_orthogonal_route
    # Endpoints facing different axes: one corner is enough.
    assert plan((0, 0), (10, 5), axis1="horizontal", axis2="vertical") == [
        (0.0, 0.0),
        (10.0, 0.0),
        (10.0, 5.0),
    ]
    assert plan((0, 0), (10, 5), axis1="vertical", axis2="horizontal") == [
        (0.0, 0.0),
        (0.0, 5.0),
        (10.0, 5.0),
    ]


def test_plan_orthogonal_route_makes_a_z_when_both_ends_face_the_same_way():
    plan = schematic_toolset.plan_orthogonal_route
    horizontal_z = plan((0, 0), (10, 5), axis1="horizontal", axis2="horizontal", grid=1.27)
    assert len(horizontal_z) == 4, horizontal_z
    # Leaves p1 horizontally and enters p2 horizontally -- neither pin is left sideways.
    assert horizontal_z[1][1] == 0.0 and horizontal_z[-2][1] == 5.0
    assert 0.0 < horizontal_z[1][0] < 10.0

    vertical_z = plan((0, 0), (10, 5), axis1="vertical", axis2="vertical", grid=1.27)
    assert len(vertical_z) == 4, vertical_z
    assert vertical_z[1][0] == 0.0 and vertical_z[-2][0] == 10.0
    assert 0.0 < vertical_z[1][1] < 5.0


def test_plan_orthogonal_route_first_overrides_pin_axes():
    plan = schematic_toolset.plan_orthogonal_route
    forced = plan((0, 0), (10, 5), axis1="vertical", axis2="vertical", first="horizontal")
    assert forced == [(0.0, 0.0), (10.0, 0.0), (10.0, 5.0)]


@pytest.mark.parametrize(
    "kwargs",
    [
        {},
        {"axis1": "horizontal"},
        {"axis2": "vertical"},
        {"axis1": "horizontal", "axis2": "horizontal"},
        {"axis1": "vertical", "axis2": "vertical"},
        {"first": "vertical"},
    ],
)
@pytest.mark.parametrize("p2", [(10, 5), (-10, 5), (10, -5), (-10, -5), (0.5, 40)])
def test_plan_orthogonal_route_is_always_orthogonal_and_keeps_endpoints(kwargs, p2):
    """The whole point of the ticket: never a diagonal, never a moved endpoint,
    never a zero-length segment."""
    points = schematic_toolset.plan_orthogonal_route((0, 0), p2, **kwargs)
    assert points[0] == (0.0, 0.0)
    assert points[-1] == (float(p2[0]), float(p2[1]))
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        assert (x1 == x2) != (y1 == y2), f"segment ({x1},{y1})->({x2},{y2}) in {points}"


def test_add_wires_defaults_to_orthogonal_routing(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    before = len(_wires(dest))
    diagonals_before = _diagonals(dest)

    result = sch_tools["add_wires"](str(dest), [{"p1": [200, 100], "p2": [230, 130]}])
    assert result["status"] == "ok", result
    assert result["count"] == 1
    assert result["segments"] > 1, "a non-axis-aligned pair must be broken into legs"
    assert len(_wires(dest)) == before + result["segments"]
    assert _diagonals(dest) == diagonals_before, "routing must not add a diagonal"


def test_add_wires_direct_route_is_still_available(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    result = sch_tools["add_wires"](str(dest), [{"p1": [200, 100], "p2": [230, 130], "route": "direct"}])
    assert result["status"] == "ok", result
    assert result["segments"] == 1
    assert (200.0, 100.0, 230.0, 130.0) in _wires(dest)


def test_add_wires_axis_aligned_specs_are_unchanged_by_the_new_default(tmp_path, sch_tools):
    """Orthogonal routing is a no-op for already-orthogonal specs -- which is
    why it is safe as the default for already-shipped callers."""
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    result = sch_tools["add_wires"](str(dest), [{"p1": [200, 100], "p2": [210, 100], "snap": False}])
    assert result["status"] == "ok", result
    assert result["segments"] == 1
    assert (200.0, 100.0, 210.0, 100.0) in _wires(dest)


def test_add_wires_forwards_snap_false_to_every_segment_including_corners(tmp_path, sch_tools):
    """Regression guard for this module's twice-repeated snap bug: an
    orthogonal route creates *intermediate* waypoints too, and if `snap` were
    dropped for any of them the endpoints would silently move off their pins.
    """
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    diagonals_before = _diagonals(dest)
    p1, p2 = [200.13, 100.37], [231.91, 130.61]  # deliberately off-grid
    result = sch_tools["add_wires"](str(dest), [{"p1": p1, "p2": p2, "snap": False}])
    assert result["status"] == "ok", result

    wires = _wires(dest)
    endpoints = {(w[0], w[1]) for w in wires} | {(w[2], w[3]) for w in wires}
    assert (200.13, 100.37) in endpoints
    assert (231.91, 130.61) in endpoints
    assert _diagonals(dest) == diagonals_before


def test_add_wires_reports_partial_on_a_bad_spec(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    result = sch_tools["add_wires"](
        str(dest),
        [
            {"p1": [200, 100], "p2": [230, 130]},
            {"p1": [200, 100], "p2": [230, 130], "route": "diagonal-please"},
        ],
    )
    assert result["status"] == "partial", result
    assert result["count"] == 1
    assert [e["index"] for e in result["errors"]] == [1]


def test_add_wires_all_specs_bad_is_incomplete(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    result = sch_tools["add_wires"](str(dest), [{"p1": [1, 2]}])
    assert result["status"] == "incomplete", result
    assert result["count"] == 0


def test_add_wires_leaves_pins_along_their_own_axis_and_stays_connected(tmp_path, sch_tools):
    """End-to-end geometry check on real symbols: two stock resistors placed
    so that no wire between them can be axis-aligned. Device:R's pins point
    vertically at rotation 0, so the route must leave *both* pins vertically
    (a Z), and must still put the two pins on one net.
    """
    from kicad_tools.schematic.models.schematic import Schematic

    sch_path = tmp_path / "route.kicad_sch"
    Schematic(title="route", project_name="route").write(sch_path)
    placed = sch_tools["add_symbols"](
        str(sch_path),
        [
            {"lib_id": "Device:R", "x": 60, "y": 60, "ref": "R1", "value": "1k"},
            {"lib_id": "Device:R", "x": 120, "y": 85, "ref": "R2", "value": "2k"},
        ],
    )
    assert placed["status"] == "ok", placed

    loaded = Schematic.load(sch_path)
    by_ref = {s.reference: s for s in loaded.symbols}
    p1 = list(by_ref["R1"].pin_position("2"))
    p2 = list(by_ref["R2"].pin_position("1"))
    assert p1[0] != p2[0] and p1[1] != p2[1], "test setup must be non-axis-aligned"

    result = sch_tools["add_wires"](str(sch_path), [{"p1": p1, "p2": p2, "snap": False}])
    assert result["status"] == "ok", result
    assert result["segments"] == 3, result  # Z: both pins face the same way

    assert _diagonals(sch_path) == []
    wires = _wires(sch_path)
    # The leg touching each pin runs along that pin's own (vertical) axis.
    touching_p1 = [w for w in wires if (w[0], w[1]) == tuple(p1) or (w[2], w[3]) == tuple(p1)]
    assert touching_p1 and all(w[0] == w[2] for w in touching_p1), touching_p1
    touching_p2 = [w for w in wires if (w[0], w[1]) == tuple(p2) or (w[2], w[3]) == tuple(p2)]
    assert touching_p2 and all(w[0] == w[2] for w in touching_p2), touching_p2

    assert Schematic.load(sch_path).are_connected("R1", "2", "R2", "1")


def test_golden_path_style_schematic_is_erc_clean(tmp_path, sch_tools, verification_tools):
    """Build a small real schematic (power/ground, decoupling cap, LED/R,
    matching the golden-path board's parts per docs/TESTING.md) from scratch
    using this toolset's own tools, and confirm ERC actually runs (via the
    existing sch_run_erc tool). No network access needed -- symbol
    resolution is entirely local, against KiCad's own installed stock
    libraries (see _ensure_kicad_symbol_dir_set's docstring for why that
    needed a workaround on Windows).
    """
    from kicad_tools.schematic.models.schematic import Schematic

    sch_path = tmp_path / "golden.kicad_sch"
    sch = Schematic(title="Golden Path Test Board", project_name="golden")
    sch.write(sch_path)

    add_result = sch_tools["add_symbols"](
        str(sch_path),
        [
            {"lib_id": "power:GND", "x": 100, "y": 150, "ref": "#PWR01"},
            {"lib_id": "power:VCC", "x": 100, "y": 50, "ref": "#PWR02"},
            {"lib_id": "Device:C", "x": 120, "y": 100, "ref": "C1", "value": "100nF", "footprint": "Capacitor_SMD:C_0402_1005Metric"},
            {"lib_id": "Device:R", "x": 140, "y": 100, "ref": "R1", "value": "330", "footprint": "Resistor_SMD:R_0402_1005Metric"},
            {"lib_id": "Device:LED", "x": 160, "y": 100, "ref": "D1", "footprint": "LED_SMD:LED_0603_1608Metric"},
        ],
    )
    assert add_result["status"] == "ok", add_result

    wire_result = sch_tools["add_wires"](
        str(sch_path),
        [
            {"p1": [100, 50], "p2": [120, 50]},
            {"p1": [120, 50], "p2": [120, 90]},
            {"p1": [140, 100], "p2": [150, 100]},
            {"p1": [100, 150], "p2": [120, 150]},
            {"p1": [120, 150], "p2": [120, 110]},
        ],
    )
    assert wire_result["status"] == "ok", wire_result

    erc = verification_tools["sch_run_erc"](str(sch_path))
    # Not asserting zero violations unconditionally -- report honestly
    # either way (evidence-gated), but the status itself must be "complete"
    # (i.e. ERC actually ran), which is the acceptance bar for this test.
    assert erc["status"] == "complete", erc


# ---------------------------------------------------------------------------
# Build ticket 12 -- every schematic write is UTF-8, whatever the platform
# default (docs/ARCHITECTURE.md known fact: kicad_tools' Schematic.write() uses the
# platform encoding, cp1252 on a German Windows).
#
# Reproduced before the fix on kicad_tools 0.20.0 under cp1252:
#   * "V_Ω" (Ω is not in cp1252): add_labels returned incomplete and left the
#     .kicad_sch at 0 bytes.
#   * "V_µ" (µ IS in cp1252): add_labels returned ok and wrote the byte 0xB5;
#     kicad_tools could no longer load the file, and `kicad-cli sch upgrade`
#     read the label as "" -- a silent loss.
# The oracle is KiCad itself: a copy is upgraded with kicad-cli (which parses
# the file and writes it back in KiCad's own format) and its raw label tokens
# are read.
# ---------------------------------------------------------------------------

_NON_ASCII_TEXTS = ["V_\u03a9", "V_\u00b5", "\u7535\u6e90"]  # V_Ω, V_µ, 电源


def _label_texts(path: Path) -> list[str]:
    from kicad_tools.sexp import parse_file

    return [str(node.get_first_atom()) for node in parse_file(path).find_all("label")]


def _labels_as_kicad_reads_them(path: Path, workdir: Path) -> list[str]:
    import subprocess

    from kicad_mcp.kicad_cli import find_kicad_cli

    try:
        cli = find_kicad_cli()
    except Exception as exc:  # noqa: BLE001 - absent tool: skip with the reason
        pytest.skip(f"kicad-cli not available: {exc}")
    workdir.mkdir()
    copy = workdir / path.name
    shutil.copy(path, copy)
    proc = subprocess.run(
        [str(cli), "sch", "upgrade", "--force", str(copy)], capture_output=True, timeout=120
    )
    assert proc.returncode == 0, proc
    return _label_texts(copy)


def _add_non_ascii_labels(sch_tools, dest: Path) -> dict:
    return sch_tools["add_labels"](
        str(dest),
        [
            {"kind": "label", "text": text, "x": 220 + 10 * i, "y": 100}
            for i, text in enumerate(_NON_ASCII_TEXTS)
        ],
    )


def test_add_labels_writes_non_ascii_text_as_utf8_that_kicad_reads_back(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)

    result = _add_non_ascii_labels(sch_tools, dest)

    assert result["status"] == "ok", result
    raw = dest.read_bytes()
    assert raw, "the schematic must never be left empty"
    raw.decode("utf-8")  # raises if any byte is not UTF-8
    for text in _NON_ASCII_TEXTS:
        assert text.encode("utf-8") in raw, text
    assert set(_NON_ASCII_TEXTS) <= set(_label_texts(dest))
    assert set(_NON_ASCII_TEXTS) <= set(_labels_as_kicad_reads_them(dest, tmp_path / "kicad"))


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        ("add_symbols", [[{"lib_id": "Device:R", "x": 150, "y": 100, "ref": "R99", "value": "1k"}]]),
        ("add_wires", [[{"p1": [200, 120], "p2": [210, 120]}]]),
        ("add_junctions", [[[210, 120]]]),
        ("add_labels", [[{"kind": "label", "text": "ASCII_ONLY", "x": 260, "y": 120}]]),
    ],
)
def test_every_schematic_writer_keeps_existing_non_ascii_text(tmp_path, sch_tools, tool, args):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    assert _add_non_ascii_labels(sch_tools, dest)["status"] == "ok"

    result = sch_tools[tool](str(dest), *args)

    assert result["status"] == "ok", result
    dest.read_bytes().decode("utf-8")
    assert set(_NON_ASCII_TEXTS) <= set(_label_texts(dest))


@pytest.mark.parametrize(
    ("tool", "args"),
    [
        # A lone surrogate survives JSON decoding but no codec can encode it.
        ("add_labels", [[{"kind": "label", "text": "BAD_\ud800", "x": 220, "y": 100}]]),
        ("add_symbols", [[{"lib_id": "Device:R", "x": 150, "y": 100, "ref": "R99", "value": "\ud800"}]]),
    ],
)
def test_a_write_that_cannot_be_encoded_leaves_the_schematic_byte_identical(tmp_path, sch_tools, tool, args):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    before = dest.read_bytes()

    result = sch_tools[tool](str(dest), *args)

    assert result["status"] == "incomplete", result
    assert dest.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sch.kicad_sch"], "no temporary file left behind"


def test_a_short_write_is_caught_by_the_read_back_and_the_schematic_is_untouched(tmp_path, sch_tools, monkeypatch):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    before = dest.read_bytes()
    real_write_text = Path.write_text

    def truncating_write_text(self, data, *a, **kw):
        return real_write_text(self, data[: len(data) // 2], *a, **kw)

    monkeypatch.setattr(Path, "write_text", truncating_write_text)
    result = sch_tools["add_labels"](str(dest), [{"kind": "label", "text": "X", "x": 220, "y": 100}])
    monkeypatch.undo()

    assert result["status"] == "incomplete", result
    assert "read-back" in result["reason"]
    assert dest.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sch.kicad_sch"]


def test_a_character_cp1252_can_encode_is_still_written_as_utf8(tmp_path, sch_tools):
    # The silent case: under cp1252 this returned ok and wrote 0xB5, which
    # KiCad then read as an empty label.
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)

    result = sch_tools["add_labels"](str(dest), [{"kind": "label", "text": "V_\u00b5", "x": 220, "y": 100}])

    assert result["status"] == "ok", result
    assert "V_\u00b5".encode("utf-8") in dest.read_bytes()
    assert "V_\u00b5" in _labels_as_kicad_reads_them(dest, tmp_path / "kicad")


def test_every_write_is_utf8_into_a_sibling_then_os_replace(tmp_path, sch_tools, monkeypatch):
    # Spies, so this fails on any platform (also one whose default is UTF-8) if the
    # encoding is dropped or the original is written in place.
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    writes: list[tuple[Path, dict]] = []
    replaces: list[tuple[Path, Path]] = []
    real_write_text, real_replace = Path.write_text, schematic_toolset.os.replace

    def spy_write_text(self, data, *args, **kwargs):
        writes.append((Path(self), {**dict(zip(("encoding", "errors", "newline"), args)), **kwargs}))
        return real_write_text(self, data, *args, **kwargs)

    def spy_replace(src, dst):
        replaces.append((Path(src), Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(Path, "write_text", spy_write_text)
    monkeypatch.setattr(schematic_toolset.os, "replace", spy_replace)
    result = sch_tools["add_labels"](str(dest), [{"kind": "label", "text": "X", "x": 220, "y": 100}])
    monkeypatch.undo()

    assert result["status"] == "ok", result
    assert len(replaces) == 1
    src, dst = replaces[0]
    assert dst == dest and src.parent == dest.parent and src.name != dest.name
    assert [path for path, _ in writes] == [src], "only the sibling is written; the original only replaced"
    assert writes[0][1].get("encoding") == "utf-8"
    assert not src.exists()


def test_a_synthesized_power_symbol_is_refused_and_nothing_is_written(tmp_path):
    from kicad_tools.schematic.models.schematic import Schematic

    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    before = dest.read_bytes()
    sch = Schematic.load(dest)
    sch.add_pwr_symbol("VMOTOR", x=200, y=100)

    with pytest.raises(RuntimeError, match="synthesized power symbols"):
        schematic_toolset._write_schematic_utf8(sch, dest)

    assert dest.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sch.kicad_sch"], "no sidecar or temporary file"


def test_the_writer_refuses_a_carriage_return_in_the_serialised_text_before_writing(tmp_path, monkeypatch):
    # Build ticket 60: the backstop behind the tools' up-front input check. A \r that reached
    # the in-memory schematic another way (here kicad_tools' own add_label) must be refused
    # before any file is written; the read-back alone would catch it only after a write.
    from kicad_tools.schematic.models.schematic import Schematic

    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    before = dest.read_bytes()
    sch = Schematic.load(dest)
    sch.add_label("A\rB", 220, 100)
    assert "\r" in sch.to_sexp(), "the precondition: kicad_tools serialises the \\r raw"
    writes: list[Path] = []
    real_write_text = Path.write_text

    def spy_write_text(self, *args, **kwargs):
        writes.append(Path(self))
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", spy_write_text)
    with pytest.raises(ValueError, match="carriage return"):
        schematic_toolset._write_schematic_utf8(sch, dest)
    monkeypatch.undo()

    assert writes == [], "refused before any temporary file was written"
    assert dest.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sch.kicad_sch"]


def test_the_paper_is_still_auto_sized_like_schematic_write(tmp_path, sch_tools):
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    from kicad_tools.sexp import parse_file

    assert parse_file(dest).find("paper").get_first_atom() == "A4"

    result = sch_tools["add_symbols"](str(dest), [{"lib_id": "Device:R", "x": 380, "y": 250, "ref": "R99"}])

    assert result["status"] == "ok", result
    assert parse_file(dest).find("paper").get_first_atom() in ("A3", "A2", "A1", "A0")


@pytest.mark.parametrize(
    ("tool", "args", "field"),
    [
        ("add_labels", [[{"kind": "label", "text": "A\rB", "x": 220, "y": 100}]], "specs[0].text"),
        ("add_symbols", [[{"lib_id": "Device:R", "x": 150, "y": 100, "ref": "R99", "value": "1\rk"}]],
         "specs[0].value"),
        ("assign_footprints", [{"R1": "Resistor_SMD:R_0603\r_1608Metric"}], "mapping.R1"),
    ],
)
def test_a_carriage_return_in_input_text_is_refused_up_front(tmp_path, sch_tools, tool, args, field):
    # kicad_tools 0.20.0 writes a \r inside a string raw (docs/ARCHITECTURE.md decision 1), and its
    # parser drops it on reload: refuse it before anything is loaded or written.
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    before = dest.read_bytes()

    result = sch_tools[tool](str(dest), *args)

    assert result["status"] == "incomplete", result
    assert "carriage return" in result["reason"] and field in result["reason"], result
    assert dest.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["sch.kicad_sch"]
