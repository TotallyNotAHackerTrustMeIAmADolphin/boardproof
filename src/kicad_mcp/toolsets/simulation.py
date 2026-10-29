"""SPICE simulation toolset (archived backlog P-SIM-01 / P-SIM-02).

Wraps KiCad's own SPICE netlist export -- confirmed real via
`kicad-cli sch export netlist --help` on the installed KiCad 10.0,
which lists `spice` as a supported `--format` value, so this project
doesn't need to build netlist generation itself -- plus ngspice run as
a local subprocess for headless simulation, the same arm's-length
pattern already used for kicad-cli/easyeda2kicad/Freerouting elsewhere
in this project.

License note (2026-09-02): ngspice's core is "Modified BSD" (confirmed
against its real COPYING file at
https://sourceforge.net/p/ngspice/ngspice/ci/master/tree/COPYING?format=raw,
not guessed -- a GitHub-API license lookup came back inconclusive
since ngspice is SourceForge-hosted, not GitHub-hosted). A handful of
internal sub-modules (XSPICE's icm/table code, tclspice.c, the KLU
solver) are LGPLv2/GPLv2-or-newer/MPLv2, but since this project only
ever invokes the pre-built `ngspice` executable as a subprocess --
never imports or links against its source -- that internal mixed
licensing doesn't propagate here, the same "mere aggregation" reasoning
already applied to Freerouting (GPLv3) elsewhere in docs/ARCHITECTURE.md.

Environment note: the ngspice subprocess is verified against a real, pinned binary
(ngspice-47, official Windows build; NGSPICE_PATH points at ngspice_con.exe). The parser is
pinned by raw `wrdata` files and stdout recorded from that binary
(tests/fixtures/spice/), and the live tests run it for real: they skip without ngspice in an
ad-hoc run and fail when NGSPICE_REQUIRED is set (the batch gate; tests/_ngspice.py).
`export_spice_netlist` is verified end to end with the real `kicad-cli`.

Evidence-gated throughout, per this project's standing convention: a
missing ngspice/kicad-cli binary, a non-convergent simulation, or a
result ngspice didn't actually produce is reported explicitly via
status/reason, never silently returned as a clean success.
"""

from __future__ import annotations

import glob
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from kicad_mcp.kicad_cli import KicadCliError, ensure_kicad_cli_on_path

_PROBE_RE = re.compile(r"[^\s()]+")
# A SPICE number: optional sign, digits, optional exponent, optional scale/unit letters ("1us", "1meg", "1kHz").
_SPICE_NUMBER_RE = re.compile(r"[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?[A-Za-z]{0,6}")
_SWEEPS = ("dec", "oct", "lin")
# Per analysis type: the numeric fields that go into the ngspice control script.
_NUMBER_FIELDS = {"tran": ("step", "stop"), "ac": ("fstart", "fstop")}


def _validate_analysis(analysis_type: str | None, analysis: dict) -> str | None:
    """Error text when an analysis field could end its control-script line or add a command
    (ngspice's `shell` runs programs), else None. Every interpolated field is checked."""
    for field in _NUMBER_FIELDS.get(analysis_type or "", ()):
        value = analysis.get(field)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            return f"analysis[{field!r}] must be a number with an optional SPICE suffix, got {value!r}"
        if isinstance(value, str):
            if not _SPICE_NUMBER_RE.fullmatch(value):
                return f"analysis[{field!r}] must be a number with an optional SPICE suffix (1us, 10ms, 1meg), got {value!r}"
        elif not math.isfinite(value):
            return f"analysis[{field!r}] must be a finite number, got {value!r}"
    if analysis_type == "ac":
        sweep = analysis.get("sweep", "dec")
        if sweep not in _SWEEPS:
            return f"analysis['sweep'] must be one of {', '.join(_SWEEPS)}, got {sweep!r}"
        points = analysis.get("points", 20)
        if isinstance(points, bool) or not isinstance(points, int) or points < 1:
            return f"analysis['points'] must be a positive integer, got {points!r}"
    return None
_NUM_RE = re.compile(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")

# Common ngspice install-dir layouts, newest-version-first via sort(reverse=True)
# on the matched paths. Windows' official release unpacks to a
# "Spice64\bin\" subfolder regardless of where you extract it -- no
# standard install location, so this is a best-effort net, not a
# guarantee; NGSPICE_PATH is the reliable override.
_NGSPICE_CANDIDATE_GLOBS: dict[str, list[str]] = {
    "win32": [
        r"C:\Program Files\ngspice*\Spice64\bin\ngspice_con.exe",
        r"C:\ngspice*\Spice64\bin\ngspice_con.exe",
    ],
    "darwin": ["/usr/local/bin/ngspice", "/opt/homebrew/bin/ngspice"],
    "linux": ["/usr/bin/ngspice", "/usr/local/bin/ngspice"],
}


def _incomplete(reason: str, **extra: Any) -> dict:
    return {"status": "incomplete", "reason": reason, **extra}


def export_spice_netlist(sch_path: str | Path, output_path: str | Path) -> dict:
    """Export a SPICE-format netlist from a schematic via kicad-cli's
    real, built-in `sch export netlist --format spice`.

    Evidence-gated: a missing schematic, missing kicad-cli, or a
    nonzero/no-output export is reported explicitly.
    """
    sch_path = Path(sch_path)
    output_path = Path(output_path)
    if not sch_path.is_file():
        return _incomplete(f"schematic not found: {sch_path}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        cli = ensure_kicad_cli_on_path()
    except KicadCliError as exc:
        return _incomplete(f"kicad-cli not available: {exc}")

    proc = subprocess.run(
        [str(cli), "sch", "export", "netlist", "--format", "spice", "-o", str(output_path), str(sch_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0 or not output_path.is_file():
        return _incomplete(
            f"kicad-cli sch export netlist failed (exit {proc.returncode})",
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
    return {"status": "ok", "path": str(output_path)}


def find_ngspice() -> Path | None:
    """Resolve the ngspice executable for headless/batch use.

    Resolution order: ``NGSPICE_PATH`` env var override, then PATH
    (preferring the console-only build over the GUI one -- see below),
    then common per-platform install-dir globs. Returns None if not
    found -- callers must treat that as evidence-gated 'incomplete',
    not an error to raise, since ngspice is a separately-installed
    optional tool.

    Windows note (found 2026-09-03, installing ngspice for real): the
    prebuilt Windows release ships two executables, ``ngspice.exe``
    (GUI build) and ``ngspice_con.exe`` (console-only build).
    ``ngspice.exe`` hangs indefinitely even with ``-b`` (batch mode)
    and piped/redirected stdin -- it still waits on something GUI-side.
    ``ngspice_con.exe`` runs cleanly in batch mode with no window.
    Always prefer the ``_con`` variant when both are present.
    """
    env_override = os.environ.get("NGSPICE_PATH")
    if env_override:
        path = Path(env_override)
        return path if path.is_file() else None

    for name in ("ngspice_con", "ngspice"):
        which = shutil.which(name)
        if which:
            return Path(which)

    for pattern in _NGSPICE_CANDIDATE_GLOBS.get(sys.platform, []):
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return Path(matches[0])

    return None


def run_spice_simulation(netlist_path: str | Path, analysis: dict) -> dict:
    """Run ngspice in headless/batch mode against a SPICE netlist.

    `analysis` shape (dict, `type` required):
      - `{"type": "op", "probe": ["out"]}` -- DC operating point.
      - `{"type": "tran", "step": "1us", "stop": "10ms", "probe": ["out"]}` -- transient.
      - `{"type": "ac", "sweep": "dec", "points": 20, "fstart": "1", "fstop": "1meg", "probe": ["out"]}`
        -- AC/frequency-sweep. For `ac`, each series also includes
        `cutoff_frequency_hz`: the frequency where that node's
        magnitude first drops to 1/sqrt(2) of its own maximum (the standard
        -3dB point for a single-pole filter), found by log-linear
        interpolation between the two straddling sample points.

    `probe` lists the node name(s) to measure (SPICE node names, as they
    appear in the exported netlist -- e.g. the net name kicad-cli assigned,
    like "Net-_C1-Pad1_"). Every probe returns its own series.

    Result (status "ok"): `analysis`, `probes` (call order, duplicates
    dropped), `points`, `x` (`{name, unit, values}`: time/s, frequency/Hz,
    or None for op) and `series` -- `{probe: {unit: "V", quantity:
    "voltage" | "magnitude", values: [...]}}`, each as long as `x`. For `ac`,
    quantity is the linear magnitude and each series carries its own
    `cutoff_frequency_hz` (None when that node has no -3 dB crossing); with a
    single probe it is repeated at the top level. Not returned: currents,
    AC phase or dB, `.meas`, sweeps, several analyses per call, plots.

    Evidence-gated: missing ngspice, a timeout, a nonzero exit, or a
    simulation that produced no usable data is reported explicitly via
    status/reason -- never silently returned as a clean success.
    """
    netlist_path = Path(netlist_path)
    if not netlist_path.is_file():
        return _incomplete(f"netlist not found: {netlist_path}")

    ngspice = find_ngspice()
    if ngspice is None:
        return _incomplete(
            "ngspice not found -- set NGSPICE_PATH, add it to PATH, or install it "
            "(https://ngspice.sourceforge.io/, license: Modified BSD). On Windows, "
            "point NGSPICE_PATH at ngspice_con.exe specifically (the console build) "
            "-- the GUI ngspice.exe hangs indefinitely even in batch mode."
        )

    analysis_type = analysis.get("type")
    raw_probes = analysis.get("probe") or []
    if not isinstance(raw_probes, list):
        return {"status": "error", "reason": f"analysis['probe'] must be a list of node names, got {raw_probes!r}"}
    bad_type = [p for p in raw_probes if not isinstance(p, str)]
    if bad_type:
        return {"status": "error", "reason": f"probe names must be strings, got {bad_type!r}"}
    probes = list(dict.fromkeys(raw_probes))  # one series per name, in call order
    if not probes:
        return {"status": "error", "reason": "analysis['probe'] must list at least one node to measure"}
    bad = [p for p in probes if not isinstance(p, str) or not _PROBE_RE.fullmatch(p)]
    if bad:
        # A probe goes into ngspice's control script: whitespace, parentheses or control characters
        # could end the vector expression or add commands (ngspice's `shell` runs programs).
        return {"status": "error", "reason": f"probe names must be plain node names without spaces or parentheses: {bad!r}"}

    invalid = _validate_analysis(analysis_type, analysis)
    if invalid:
        return {"status": "error", "reason": invalid}

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp = Path(tmp_dir)
        data_path = tmp / "sim_out.txt"
        control_lines = _build_control_lines(analysis_type, analysis, probes, data_path)
        if control_lines is None:
            return {"status": "error", "reason": f"unsupported analysis type: {analysis_type!r}"}

        # Append our control block to a copy of the real exported netlist
        # rather than `.include`-ing it -- the exported netlist already
        # ends in `.end`, and ngspice errors on a doubled `.end` when
        # `.include`d alongside another one in the wrapper script.
        netlist_text = netlist_path.read_text(encoding="utf-8")
        body = netlist_text.rsplit(".end", 1)[0] if ".end" in netlist_text.lower() else netlist_text
        combined = "\n".join([body, ".control", *control_lines, ".endc", ".end", ""])
        run_path = tmp / "run.cir"
        run_path.write_text(combined, encoding="utf-8")

        try:
            proc = subprocess.run(
                [str(ngspice), "-b", str(run_path)],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
        except subprocess.TimeoutExpired as exc:
            return _incomplete(f"ngspice timed out: {exc}")

        if proc.returncode != 0:
            return _incomplete(f"ngspice exited {proc.returncode}", stdout=proc.stdout, stderr=proc.stderr)
        if not data_path.is_file():
            unknown = _unknown_vectors(proc.stdout + proc.stderr)
            if unknown:
                return _incomplete(
                    f"ngspice has no such probe: {', '.join(unknown)} -- probes are node names as they appear "
                    "in the exported netlist",
                    stdout=proc.stdout,
                    stderr=proc.stderr,
                )
            return _incomplete(
                "ngspice produced no data file -- likely non-convergence or a missing SPICE model",
                stdout=proc.stdout,
                stderr=proc.stderr,
            )

        parsed = _parse_wrdata(data_path, probes, analysis_type)

    if parsed is None:
        return {"status": "incomplete", "reason": "could not parse ngspice output data"}
    xs, values = parsed

    quantity = "magnitude" if analysis_type == "ac" else "voltage"
    series = {p: {"unit": "V", "quantity": quantity, "values": values[p]} for p in probes}
    x_name, x_unit = _X_AXIS.get(analysis_type or "", (None, None))
    result: dict = {
        "status": "ok",
        "analysis": analysis_type,
        "probes": probes,
        "points": len(values[probes[0]]),
        "x": None if xs is None else {"name": x_name, "unit": x_unit, "values": xs},
        "series": series,
    }
    if analysis_type == "ac":
        assert xs is not None
        for p in probes:
            series[p]["cutoff_frequency_hz"] = _find_cutoff_frequency(list(zip(xs, values[p], strict=True)))
        if all(series[p]["cutoff_frequency_hz"] is None for p in probes):
            result["status"] = "incomplete"
            result["reason"] = "AC sweep produced data but no -3dB crossing was found in the swept range"
        if len(probes) == 1:
            result["cutoff_frequency_hz"] = series[probes[0]]["cutoff_frequency_hz"]
    return result


# (name, unit) of the sweep variable; `op` has none.
_X_AXIS: dict[str, tuple[str, str]] = {"tran": ("time", "s"), "ac": ("frequency", "Hz")}

_NO_SUCH_VECTOR_RE = re.compile(r"no such vector\s+(\S+)", re.IGNORECASE)


def _unknown_vectors(output: str) -> list[str]:
    """Probe names ngspice reported as 'no such vector' (real ngspice-47 stdout, recorded)."""
    return list(dict.fromkeys(_NO_SUCH_VECTOR_RE.findall(output)))


def _build_control_lines(
    analysis_type: str | None, analysis: dict, probes: list[str], data_path: Path
) -> list[str] | None:
    # Real-ngspice finding (2026-09-03, installing ngspice for real): vm()
    # -- ngspice's own linear-magnitude operator, which would otherwise be
    # the natural choice for AC analysis -- fails with "bad v() syntax" on
    # any node name containing a hyphen when passed through wrdata, e.g.
    # exactly the "Net-_C1-Pad1_"-style names kicad-cli's netlist export
    # generates. v() (the raw complex phasor) parses these names fine in
    # the same position -- so we always use v() and, for AC, compute
    # magnitude ourselves from the real+imaginary columns wrdata emits for
    # a complex vector (see _parse_wrdata's is_ac handling below), rather
    # than depend on vm()'s node-name parser bug being fixed upstream.
    vectors = " ".join(f"v({p})" for p in probes)
    if analysis_type == "op":
        return ["op", f"wrdata {data_path} {vectors}"]
    if analysis_type == "tran":
        step = analysis.get("step", "1us")
        stop = analysis.get("stop", "1ms")
        return [f"tran {step} {stop}", f"wrdata {data_path} {vectors}"]
    if analysis_type == "ac":
        sweep = analysis.get("sweep", "dec")
        points = analysis.get("points", 20)
        fstart = analysis.get("fstart", "1")
        fstop = analysis.get("fstop", "1meg")
        return [f"ac {sweep} {points} {fstart} {fstop}", f"wrdata {data_path} {vectors}"]
    return None


def _parse_wrdata(
    path: Path, probes: list[str], analysis_type: str | None
) -> tuple[list[float] | None, dict[str, list[float]]] | None:
    """Parse ngspice `wrdata` output into ``(x values, {probe: values})``.

    Layout, confirmed against the real ngspice-47 binary (recorded in tests/fixtures/spice/):
    one line per point, and each probe contributes its own *group* of columns, the sweep
    variable repeated first: ``x v1  x v2`` for real vectors (op, tran), ``x re im  x re im``
    for AC, where v() is a complex phasor (magnitude is computed here as hypot(re, im), because
    vm() fails on hyphenated node names; see _build_control_lines). An earlier version kept only
    the last column and so only the last probe. For ``op`` the first column of a group is not a
    sweep variable, so ``x`` is None.

    Returns None when a line does not have exactly the column count the probes imply, or when
    there is no data line: a mismatch is reported, never guessed at."""
    is_ac = analysis_type == "ac"
    width = 3 if is_ac else 2
    columns = width * len(probes)
    xs: list[float] = []
    values: dict[str, list[float]] = {p: [] for p in probes}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        nums = [float(n) for n in _NUM_RE.findall(line)]
        if len(nums) != columns:
            return None
        xs.append(nums[0])
        for i, probe in enumerate(probes):
            group = nums[i * width : (i + 1) * width]
            values[probe].append(math.hypot(group[1], group[2]) if is_ac else group[1])
    if not xs:
        return None
    return (None if analysis_type == "op" else xs), values


def _find_cutoff_frequency(rows: list[tuple[float, float]]) -> float | None:
    """Find the -3dB point (magnitude drops to 1/sqrt(2) of its max) via
    log-linear interpolation between the two straddling sample points."""
    if len(rows) < 2:
        return None
    max_mag = max(v for _, v in rows)
    if max_mag <= 0:
        return None
    threshold = max_mag / math.sqrt(2)
    for i in range(1, len(rows)):
        f0, v0 = rows[i - 1]
        f1, v1 = rows[i]
        if v0 >= threshold >= v1 and f0 > 0 and f1 > 0:
            if v0 == v1:
                return f0
            frac = (v0 - threshold) / (v0 - v1)
            return math.exp(math.log(f0) + frac * (math.log(f1) - math.log(f0)))
    return None


def build(mcp: MCPServer) -> list[str]:
    """Register the simulation toolset. See toolsets/registry.py for the contract."""

    mcp.add_tool(
        export_spice_netlist,
        name="export_spice_netlist",
        description=(
            "Export a SPICE-format netlist from a KiCad schematic via kicad-cli's real "
            "'sch export netlist --format spice'. Required before run_spice_simulation."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    mcp.add_tool(
        run_spice_simulation,
        name="run_spice_simulation",
        description=(
            "Run a real ngspice headless simulation (DC operating point 'op', transient 'tran', or AC "
            "frequency sweep 'ac') on a SPICE netlist from export_spice_netlist. Probes are node names as "
            "they appear in that netlist. Returns one series per probe: x ({name, unit, values}; null for op) "
            "and series {probe: {unit 'V', quantity 'voltage' (op, tran) or 'magnitude' (ac, linear, not dB), "
            "values}}; for ac each series also has cutoff_frequency_hz (-3 dB point, null when that node has "
            "none). Not returned: currents, AC phase, dB, .meas results, sweeps, several analyses per call, "
            "plots. A converged run is not evidence that the circuit works: compare the numbers with a hand "
            "calculation. Requires ngspice (https://ngspice.sourceforge.io/): status 'incomplete' with the "
            "reason when it is missing, a probe is unknown, or no data came back."
        ),
        annotations={
            "readOnlyHint": True,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    return ["export_spice_netlist", "run_spice_simulation"]
