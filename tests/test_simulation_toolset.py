"""Tests for the simulation toolset (archived backlog P-SIM-01/P-SIM-02).

export_spice_netlist is tested for real against real kicad-cli (genuinely
installed on this dev machine) exporting a real RC low-pass filter
schematic built via the actual schematic toolset's own add_symbols/
add_wires tools (not hand-written S-expressions, not kicad_tools called
directly). run_spice_simulation's ngspice subprocess call is mocked
(ngspice itself isn't installed in this environment -- see the module
docstring in simulation.py) but its surrounding logic -- control-script
construction, output parsing, -3dB cutoff-frequency extraction -- is
exercised against a realistic synthetic dataset following the real
analytical response of the same RC filter, so the acceptance test
(P-SIM-02) is genuinely checking the math, not a canned answer.
"""

from __future__ import annotations

import math
import os
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp.toolsets import simulation as simulation_toolset
from tests import _kicad_demos

# RC low-pass: Vin --R1-- out --C1-- GND. Round values so the expected
# cutoff frequency is easy to verify independently by hand:
# f_c = 1 / (2*pi*R*C) = 1 / (2*pi*1000*100e-9) ~= 1591.5 Hz.
_R_OHMS = 1000.0
_C_FARADS = 100e-9
_EXPECTED_FC_HZ = 1.0 / (2 * math.pi * _R_OHMS * _C_FARADS)


@pytest.fixture()
def rc_filter_schematic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Build a real RC low-pass filter schematic: a Simulation_SPICE:VDC
    source with an AC magnitude set via its Value field ("1 AC 1" --
    confirmed by inspecting the real kicad-cli-exported netlist to
    produce 'DC 1 AC 1' on the V-source line, exactly the SPICE syntax
    an AC sweep needs), R1, C1, and two power:GND references, wired via
    `wire_pins`/`wire_power_to_pin` (pin-aware; resolves real pin
    positions internally). Confirmed by inspecting the real exported
    netlist to produce exactly 3 distinct nets (V1/R1 junction, R1/C1
    junction -- the probed "out" node -- and GND), not a short.

    Note: `wire_power_to_pin` emits a UserWarning here ("Wire start
    endpoint ... lands on existing wire segment ... creating unintended
    connection") that turned out to be a false positive for this
    topology while developing this ticket -- the warned-about point is
    V1's own pin, which is exactly where its other wire also
    legitimately originates, not an actual short (verified against the
    real exported netlist above). `schematic.py`'s own `add_wires`
    (raw-coordinate, not pin-aware) has a related but separate real bug
    worth a fast-follow: it calls `add_wire(p1, p2)` with the default
    `snap=True`, which snaps given coordinates to a grid -- even when
    those coordinates are a pin's own exact position, snapping can move
    the wire endpoint off that pin and silently break the connection
    (reproduced during development: kicad-cli's export then reported
    every pad as "unconnected-..." instead of a real net). Flagged for
    the coordinator rather than fixed here (out of this ticket's scope).
    """
    # KiCad's symbol libraries from the install (KICAD_SYMBOL_DIR, else next to kicad-cli),
    # skip or fail by the KICAD_DEMOS_REQUIRED rule (build ticket 34).
    from kicad_mcp import kicad_cli

    symbols = _kicad_demos.symbols_dir_or_missing(os.environ, kicad_cli.ensure_kicad_cli_on_path)
    monkeypatch.setenv("KICAD_SYMBOL_DIR", str(symbols))

    from kicad_tools.schematic.models.schematic import Schematic

    sch_path = tmp_path / "rc_filter.kicad_sch"
    sch = Schematic(title="RC Filter", project_name="rc_filter")
    v1 = sch.add_symbol("Simulation_SPICE:VDC", 50, 50, ref="V1", value="1 AC 1", rotation=90)
    r1 = sch.add_symbol("Device:R", 90, 50, ref="R1", value="1k", rotation=90)
    c1 = sch.add_symbol("Device:C", 120, 50, ref="C1", value="100n", rotation=0)

    sch.wire_pins(v1, "1", r1, "1")  # V1.1(+) -> R1.1
    sch.wire_pins(r1, "2", c1, "1")  # R1.2 -> C1.1 (the probed "out" node)
    sch.wire_power_to_pin("power:GND", v1, "2")  # V1.2(-) -> GND
    sch.wire_power_to_pin("power:GND", c1, "2")  # C1.2 -> GND
    sch.write(sch_path)
    return sch_path


def test_export_spice_netlist_real_kicad_cli(rc_filter_schematic: Path, tmp_path: Path):
    out = tmp_path / "rc_filter.cir"
    result = simulation_toolset.export_spice_netlist(rc_filter_schematic, out)
    assert result["status"] == "ok", result
    assert out.is_file()
    text = out.read_text(encoding="utf-8")
    # Real, confirmed netlist shape for this exact circuit (verified by
    # hand while developing this ticket): R1 and C1 present with their
    # values, V1 carrying both a DC and an AC magnitude.
    assert "R1 " in text and "1k" in text
    assert "C1 " in text and "100n" in text
    assert "V1 " in text and "DC 1" in text and "AC 1" in text

    # Confirm this is a genuine 3-node RC filter, not shorted -- the same
    # wiring geometry mistake (a wire's endpoint landing on top of another
    # wire, merging two nets) was actually caught here during development
    # via kicad-cli's own "unintended connection" ERC-adjacent warning and
    # fixed; this assertion guards against silently regressing to that.
    nodes: set[str] = set()
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] in ("R1", "C1", "V1"):
            nodes.update(parts[1:3])
    assert len(nodes) == 3, f"expected 3 distinct nets (in, out, GND), got {nodes}"


def test_export_spice_netlist_missing_schematic(tmp_path: Path):
    result = simulation_toolset.export_spice_netlist(tmp_path / "nope.kicad_sch", tmp_path / "out.cir")
    assert result["status"] == "incomplete"


def test_run_spice_simulation_missing_netlist(tmp_path: Path):
    result = simulation_toolset.run_spice_simulation(tmp_path / "nope.cir", {"type": "op", "probe": ["out"]})
    assert result["status"] == "incomplete"


def test_run_spice_simulation_missing_ngspice(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    netlist = tmp_path / "n.cir"
    netlist.write_text(".title t\nR1 a b 1k\n.end\n", encoding="utf-8")
    monkeypatch.setattr(simulation_toolset, "find_ngspice", lambda: None)
    result = simulation_toolset.run_spice_simulation(netlist, {"type": "op", "probe": ["a"]})
    assert result["status"] == "incomplete"
    assert "ngspice" in result["reason"]


def test_run_spice_simulation_rejects_unsupported_analysis(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    netlist = tmp_path / "n.cir"
    netlist.write_text(".title t\nR1 a b 1k\n.end\n", encoding="utf-8")
    monkeypatch.setattr(simulation_toolset, "find_ngspice", lambda: Path("ngspice"))
    result = simulation_toolset.run_spice_simulation(netlist, {"type": "bogus", "probe": ["a"]})
    assert result["status"] == "error"


def test_run_spice_simulation_ac_recovers_analytical_cutoff_frequency(
    rc_filter_schematic: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The real P-SIM-02 acceptance check: simulate an AC sweep of the RC
    filter and confirm the recovered -3dB frequency matches the analytical
    f_c = 1/(2*pi*R*C) ~= 1591.5 Hz within 5% -- not a canned answer, a
    realistic synthetic dataset generated from the filter's actual transfer
    function |H(f)| = 1/sqrt(1+(f/f_c)^2), standing in for real ngspice
    output since ngspice isn't installed in this environment (see
    simulation.py's module docstring)."""
    netlist_out = tmp_path / "rc_filter.cir"
    export_result = simulation_toolset.export_spice_netlist(rc_filter_schematic, netlist_out)
    assert export_result["status"] == "ok", export_result

    monkeypatch.setattr(simulation_toolset, "find_ngspice", lambda: Path("ngspice"))

    real_subprocess_run = __import__("subprocess").run

    def fake_ngspice_run(cmd, **kwargs):
        # The real code passes ["ngspice", "-b", "<run.cir>"] -- write a
        # synthetic wrdata-format file where the real code expects it,
        # following the RC filter's actual analytical transfer function,
        # then behave like a normal successful subprocess.run.
        run_cir_path = Path(cmd[2])
        control_text = run_cir_path.read_text(encoding="utf-8")
        # Extract the wrdata target path the real code generated.
        wrdata_line = next(line for line in control_text.splitlines() if line.startswith("wrdata"))
        data_path = Path(wrdata_line.split()[1])

        lines = []
        for i in range(60):
            # log-spaced 1 Hz .. 1 MHz
            f = 10 ** (0 + i * (6 / 59))
            mag = 1.0 / math.sqrt(1 + (f / _EXPECTED_FC_HZ) ** 2)
            # 3 columns (freq, real, imag) matching real ngspice's AC wrdata
            # shape confirmed 2026-09-03 -- write the magnitude as the real
            # part with zero imaginary, since _parse_wrdata(is_ac=True)
            # computes magnitude as hypot(real, imag) and hypot(mag, 0) == mag.
            lines.append(f"{f:.6e}\t{mag:.6e}\t0.0")
        data_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

        class _Result:
            returncode = 0
            stdout = "ngspice-fake-batch-output"
            stderr = ""

        return _Result()

    monkeypatch.setattr(simulation_toolset.subprocess, "run", fake_ngspice_run)

    result = simulation_toolset.run_spice_simulation(
        netlist_out, {"type": "ac", "sweep": "dec", "points": 10, "fstart": "1", "fstop": "1meg", "probe": ["out"]}
    )
    assert result["status"] == "ok", result
    assert result["cutoff_frequency_hz"] is not None
    relative_error = abs(result["cutoff_frequency_hz"] - _EXPECTED_FC_HZ) / _EXPECTED_FC_HZ
    assert relative_error < 0.05, (result["cutoff_frequency_hz"], _EXPECTED_FC_HZ, relative_error)


def test_run_spice_simulation_ac_real_ngspice_no_mocking(rc_filter_schematic: Path, tmp_path: Path, ngspice_exe: Path):
    """Genuine end-to-end proof, no mocking anywhere: real kicad-cli netlist
    export, then a real ngspice_con subprocess run, against the real RC
    filter schematic, checked against the same analytical f_c as the
    mocked P-SIM-02 test above. Skips cleanly (not a failure) when ngspice
    isn't available -- matches this project's convention for other
    optional-external-tool tests (see docs/TESTING.md).

    Note the probe node name differs from the mocked test above ("out" is
    a placeholder the fake doesn't validate) -- the real kicad-cli-exported
    netlist names the R1/C1 junction "Net-_C1-Pad1_" (confirmed by
    inspecting a real export while writing this test), and real ngspice
    will error on an unknown probe name, unlike the mock.

    ngspice was installed into the session scratchpad on 2026-09-03
    (ngspice-47_64, official Windows prebuilt release, Modified BSD core
    license -- see simulation.py's module docstring) specifically to
    upgrade P-SIM-01/02 from "logic tested, binary mocked" to "verified
    against a real binary" per the honesty gap flagged in archived backlog.
    """
    netlist_out = tmp_path / "rc_filter.cir"
    export_result = simulation_toolset.export_spice_netlist(rc_filter_schematic, netlist_out)
    assert export_result["status"] == "ok", export_result

    result = simulation_toolset.run_spice_simulation(
        netlist_out,
        {"type": "ac", "sweep": "dec", "points": 20, "fstart": "1", "fstop": "1meg", "probe": ["Net-_C1-Pad1_"]},
    )
    assert result["status"] == "ok", result
    assert abs(result["cutoff_frequency_hz"] - _EXPECTED_FC_HZ) / _EXPECTED_FC_HZ < 0.05, (
        f"real ngspice gave {result['cutoff_frequency_hz']} Hz, expected ~{_EXPECTED_FC_HZ:.1f} Hz"
    )


def test_find_cutoff_frequency_no_crossing_returns_none():
    # Magnitude never drops below the -3dB threshold in this range.
    rows = [(1.0, 1.0), (10.0, 0.99), (100.0, 0.98)]
    assert simulation_toolset._find_cutoff_frequency(rows) is None


def test_build_registers_expected_tools():
    mcp = MCPServer("test")
    names = simulation_toolset.build(mcp)
    assert set(names) == {"export_spice_netlist", "run_spice_simulation"}


# --- every probe returns its own series (build ticket 02, spice-simulation) ---------------------
#
# The fixtures in tests/fixtures/spice/ are the raw `wrdata` files and the stdout of the real
# ngspice-47 (official Windows build, ngspice_con.exe) run on the netlists beside them; see the
# README there. The live tests below run the same circuits through the tool and need the binary
# (NGSPICE_PATH): they skip without it, and fail when NGSPICE_REQUIRED is set (the gate run).

SPICE_FIXTURES = Path(__file__).parent / "fixtures" / "spice"
RC_NETLIST = SPICE_FIXTURES / "rc_lowpass.cir"
RC_PULSE_NETLIST = SPICE_FIXTURES / "rc_pulse.cir"
VIN, VOUT = "Net-_R1-Pad1_", "Net-_C1-Pad1_"


def _expected_mag(f: float) -> float:
    return 1.0 / math.sqrt(1.0 + (f / _EXPECTED_FC_HZ) ** 2)


def test_parse_recorded_ac_wrdata_gives_each_probe_its_own_series():
    xs, series = simulation_toolset._parse_wrdata(SPICE_FIXTURES / "rc_lowpass_ac.wrdata", [VIN, VOUT], "ac")
    assert xs[0] == 10.0 and xs[-1] == 10000.0 and len(xs) == 7
    assert set(series) == {VIN, VOUT}
    assert series[VIN] == [1.0] * 7  # the source node: |V| = 1 at every frequency
    for f, mag in zip(xs, series[VOUT], strict=True):
        assert mag == pytest.approx(_expected_mag(f), rel=1e-3)
    assert series[VOUT][0] > 0.99 and series[VOUT][-1] < 0.2  # not the same numbers twice


def test_parse_recorded_tran_wrdata_gives_each_probe_its_own_series():
    xs, series = simulation_toolset._parse_wrdata(SPICE_FIXTURES / "rc_pulse_tran.wrdata", [VIN, VOUT], "tran")
    assert xs[0] == 0.0 and xs[-1] == pytest.approx(5e-4) and len(xs) == 70
    assert series[VIN][0] == 0.0 and series[VIN][-1] == 1.0  # the pulse source
    assert series[VOUT][0] == 0.0 and 0.99 < series[VOUT][-1] < 1.0  # the capacitor charging
    assert series[VOUT][-1] == pytest.approx(1 - math.exp(-5e-4 / 1e-4), rel=1e-3)


def test_parse_recorded_op_wrdata_has_no_sweep_variable():
    xs, series = simulation_toolset._parse_wrdata(SPICE_FIXTURES / "rc_lowpass_op.wrdata", [VIN, VOUT], "op")
    assert xs is None  # the first column of an op wrdata is not a sweep variable
    assert series == {VIN: [1.0], VOUT: [1.0]}


def test_parse_refuses_a_column_count_that_does_not_fit_the_probes(tmp_path: Path):
    bad = tmp_path / "bad.txt"
    bad.write_text(" 1.0 2.0 3.0 \n", encoding="utf-8")  # 3 columns are neither 2 real probes nor 1 complex one
    assert simulation_toolset._parse_wrdata(bad, [VIN, VOUT], "tran") is None


def test_missing_probe_is_named_from_ngspice_stdout(monkeypatch: pytest.MonkeyPatch):
    """Recorded stdout of the real binary for `v(nosuch)`: no data file, and the reason names the probe."""
    recorded = (SPICE_FIXTURES / "rc_lowpass_missing_probe.stdout").read_text(encoding="utf-8")
    monkeypatch.setattr(simulation_toolset, "find_ngspice", lambda: Path("ngspice"))

    class _Done:
        returncode, stdout, stderr = 0, recorded, ""

    monkeypatch.setattr(simulation_toolset.subprocess, "run", lambda *a, **k: _Done())
    result = simulation_toolset.run_spice_simulation(RC_NETLIST, {"type": "op", "probe": [VIN, "nosuch"]})
    assert result["status"] == "incomplete"
    assert "nosuch" in result["reason"]


@pytest.mark.parametrize("probe", ["a b", "a)\nshell calc", "v(a)", "", 3])
def test_probe_names_that_could_inject_control_commands_are_refused(probe, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(simulation_toolset, "find_ngspice", lambda: Path("ngspice"))

    def boom(*a, **k):
        raise AssertionError("ngspice must not be started")

    monkeypatch.setattr(simulation_toolset.subprocess, "run", boom)
    result = simulation_toolset.run_spice_simulation(RC_NETLIST, {"type": "op", "probe": [VIN, probe]})
    assert result["status"] == "error", result
    assert "probe" in result["reason"]


def test_duplicate_probe_names_are_listed_once(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(simulation_toolset, "find_ngspice", lambda: Path("ngspice"))
    seen: list[str] = []

    def fake(cmd, **kwargs):
        text = Path(cmd[2]).read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if ln.startswith("wrdata"))
        seen.append(line)
        Path(line.split()[1]).write_text(" 0.0 1.0 0.0 0.5 \n", encoding="utf-8")

        class _R:
            returncode, stdout, stderr = 0, "", ""

        return _R()

    monkeypatch.setattr(simulation_toolset.subprocess, "run", fake)
    result = simulation_toolset.run_spice_simulation(RC_NETLIST, {"type": "op", "probe": ["a", "b", "a"]})
    assert result["status"] == "ok", result
    assert seen[0].count("v(a)") == 1
    assert list(result["series"]) == ["a", "b"]


def test_live_ac_two_probes_each_return_their_own_series(ngspice_exe: Path):
    """The 2026-10-22 finding (102, item 1): probe [a, b] kept only b. Real ngspice, no mocking."""
    result = simulation_toolset.run_spice_simulation(
        RC_NETLIST, {"type": "ac", "sweep": "dec", "points": 5, "fstart": "1", "fstop": "100k", "probe": [VIN, VOUT]}
    )
    assert result["status"] == "ok", result
    assert result["analysis"] == "ac"
    assert result["probes"] == [VIN, VOUT]
    assert result["x"]["name"] == "frequency" and result["x"]["unit"] == "Hz"
    xs = result["x"]["values"]
    assert result["points"] == len(xs) == 26
    assert set(result["series"]) == {VIN, VOUT}
    vin, vout = result["series"][VIN], result["series"][VOUT]
    assert vin["unit"] == "V" and vin["quantity"] == "magnitude"
    assert vin["values"] == pytest.approx([1.0] * len(xs))
    for f, mag in zip(xs, vout["values"], strict=True):
        assert mag == pytest.approx(_expected_mag(f), rel=1e-3)
    assert vout["cutoff_frequency_hz"] == pytest.approx(_EXPECTED_FC_HZ, rel=0.05)
    assert vin["cutoff_frequency_hz"] is None  # no -3 dB point on the source node
    assert "cutoff_frequency_hz" not in result  # ambiguous with two probes: read it per series


def test_live_ac_single_probe_still_reports_the_cutoff_at_top_level(ngspice_exe: Path):
    result = simulation_toolset.run_spice_simulation(
        RC_NETLIST, {"type": "ac", "points": 20, "fstart": "1", "fstop": "1meg", "probe": [VOUT]}
    )
    assert result["status"] == "ok", result
    assert result["cutoff_frequency_hz"] == pytest.approx(_EXPECTED_FC_HZ, rel=0.05)
    assert result["series"][VOUT]["cutoff_frequency_hz"] == result["cutoff_frequency_hz"]


def test_live_tran_two_probes_each_return_their_own_series(ngspice_exe: Path):
    result = simulation_toolset.run_spice_simulation(
        RC_PULSE_NETLIST, {"type": "tran", "step": "100us", "stop": "500us", "probe": [VIN, VOUT]}
    )
    assert result["status"] == "ok", result
    assert result["x"]["name"] == "time" and result["x"]["unit"] == "s"
    assert result["x"]["values"][-1] == pytest.approx(5e-4)
    vin, vout = result["series"][VIN], result["series"][VOUT]
    assert vin["unit"] == "V" and vin["quantity"] == "voltage"
    assert vin["values"][-1] == 1.0 and vout["values"][0] == 0.0
    assert vout["values"][-1] == pytest.approx(1 - math.exp(-5e-4 / 1e-4), rel=1e-3)
    assert result["points"] == len(result["x"]["values"]) == len(vout["values"]) == len(vin["values"])


def test_live_op_two_probes_each_return_their_own_value(tmp_path: Path, ngspice_exe: Path):
    divider = tmp_path / "divider.cir"
    divider.write_text(".title divider\nV1 a 0 DC 1\nR1 a b 1k\nR2 b 0 3k\n.end\n", encoding="utf-8")
    result = simulation_toolset.run_spice_simulation(divider, {"type": "op", "probe": ["a", "b"]})
    assert result["status"] == "ok", result
    assert result["x"] is None and result["points"] == 1
    assert result["series"]["a"]["values"] == pytest.approx([1.0])
    assert result["series"]["b"]["values"] == pytest.approx([0.75])
    assert result["series"]["b"]["unit"] == "V"


def test_live_recorded_fixture_matches_a_live_run(ngspice_exe: Path):
    """The recorded wrdata is what the pinned binary writes today (a version drift shows here)."""
    result = simulation_toolset.run_spice_simulation(
        RC_NETLIST, {"type": "ac", "sweep": "dec", "points": 2, "fstart": "10", "fstop": "10k", "probe": [VIN, VOUT]}
    )
    xs, series = simulation_toolset._parse_wrdata(SPICE_FIXTURES / "rc_lowpass_ac.wrdata", [VIN, VOUT], "ac")
    assert result["x"]["values"] == pytest.approx(xs)
    assert result["series"][VOUT]["values"] == pytest.approx(series[VOUT])


def test_live_unknown_probe_is_named(ngspice_exe: Path):
    result = simulation_toolset.run_spice_simulation(RC_NETLIST, {"type": "op", "probe": [VIN, "nosuch"]})
    assert result["status"] == "incomplete"
    assert "nosuch" in result["reason"]


# --- control-script injection guard (ticket 02 review, BLOCKS 3) ------------------------------------

_BAD_VALUES = ["1us\nshell echo pwned", "1 us", "1us;shell x", "1us(", "$(calc)", "1us)", "", "abc", "nan", True, [1], {"a": 1}]
_FIELDS = [
    ("tran", "step"), ("tran", "stop"),
    ("ac", "fstart"), ("ac", "fstop"), ("ac", "points"), ("ac", "sweep"),
]


def _must_not_start_ngspice(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(simulation_toolset, "find_ngspice", lambda: Path("ngspice"))

    def boom(*a, **k):
        raise AssertionError("ngspice must not be started")

    monkeypatch.setattr(simulation_toolset.subprocess, "run", boom)


@pytest.mark.parametrize("bad", _BAD_VALUES, ids=repr)
@pytest.mark.parametrize(("kind", "field"), _FIELDS)
def test_analysis_fields_that_could_inject_control_commands_are_refused(kind, field, bad, monkeypatch):
    _must_not_start_ngspice(monkeypatch)
    analysis = {"type": kind, "probe": ["a"], field: bad}
    result = simulation_toolset.run_spice_simulation(RC_NETLIST, analysis)
    assert result["status"] == "error", result
    assert field in result["reason"]


@pytest.mark.parametrize("sweep", ["dec", "oct", "lin"])
def test_valid_sweeps_pass_validation(sweep):
    assert simulation_toolset._validate_analysis("ac", {"sweep": sweep, "points": 5, "fstart": 1, "fstop": 1e6}) is None


@pytest.mark.parametrize("value", ["1us", "10ms", "1meg", "1e-6", "2.5k", 1, 1e-6, "1kHz", ".5"])
def test_numbers_with_spice_suffixes_pass_validation(value):
    assert simulation_toolset._validate_analysis("tran", {"step": value, "stop": value}) is None


def test_the_shell_injection_reproduction_no_longer_creates_the_file(tmp_path: Path, ngspice_exe: Path):
    marker = tmp_path / "pwned.txt"
    result = simulation_toolset.run_spice_simulation(
        RC_NETLIST,
        {"type": "tran", "step": f"1us\nshell echo pwned > {marker.as_posix()}", "stop": "1ms", "probe": ["in"]},
    )
    assert result["status"] == "error", result
    assert not marker.exists()


@pytest.mark.parametrize("probe", ["in", [["in"]], ("in",), 5, {"in": 1}])
def test_probe_must_be_a_list_of_strings(probe, monkeypatch):
    _must_not_start_ngspice(monkeypatch)
    result = simulation_toolset.run_spice_simulation(RC_NETLIST, {"type": "op", "probe": probe})
    assert result["status"] == "error", result
    assert "probe" in result["reason"]


def test_an_ac_run_where_no_series_has_a_cutoff_is_incomplete(monkeypatch):
    monkeypatch.setattr(simulation_toolset, "find_ngspice", lambda: Path("ngspice"))

    def fake(cmd, **kwargs):
        text = Path(cmd[2]).read_text(encoding="utf-8")
        line = next(ln for ln in text.splitlines() if ln.startswith("wrdata"))
        # flat unity magnitude at every frequency: never falls 3 dB
        Path(line.split()[1]).write_text(" 1.0 1.0 0.0\n 10.0 1.0 0.0\n 100.0 1.0 0.0\n", encoding="utf-8")

        class _R:
            returncode, stdout, stderr = 0, "", ""

        return _R()

    monkeypatch.setattr(simulation_toolset.subprocess, "run", fake)
    result = simulation_toolset.run_spice_simulation(RC_NETLIST, {"type": "ac", "probe": ["a"]})
    assert result["status"] == "incomplete", result
    assert "-3dB" in result["reason"]
