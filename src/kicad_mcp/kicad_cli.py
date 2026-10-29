"""Subprocess wrapper around KiCad's ``kicad-cli`` executable.

Per docs/ARCHITECTURE.md, ``kicad-cli`` is the headless verification/export
mechanism for the autopilot (file-based) path — used here mainly for SVG
export (the secondary, human-facing rendering aid) and as an optional
cross-check against ``kicad_tools``' own pure-Python DRC engine.

It also doubles as a fix for a real gap in ``kicad_tools`` itself:
``Schematic.run_erc()`` shells out to ``kicad-cli`` internally, but its
bundled candidate-path search (``kicad_tools.cli.runner.find_kicad_cli``)
only lists KiCad 7.0/8.0 Windows install directories, so it silently
fails to find a KiCad 9/10 install (confirmed on this dev machine, which
has KiCad 10.0). ``ensure_kicad_cli_on_path()`` works around that by
adding our own more version-tolerant resolution to PATH, so
``kicad_tools``' internal calls succeed too. Worth an upstream PR per
docs/ARCHITECTURE.md's kicad-tools contribution policy — tracked, not filed
yet.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

# Version-tolerant candidate globs, checked newest-first. kicad_tools'
# own bundled list hardcodes 7.0/8.0 only (see module docstring above).
_CANDIDATE_GLOBS: dict[str, list[str]] = {
    "win32": ["C:/Program Files/KiCad/*/bin/kicad-cli.exe"],
    "darwin": ["/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli"],
}


class KicadCliError(RuntimeError):
    """Raised when kicad-cli can't be found, or exits nonzero."""


def _version_sort_key(path: str) -> tuple:
    """Natural-version sort key for install-dir globs.

    Plain lexical sort (found as a real bug 2026-09-11, evaluating
    KiCadRoutingTools' own discovery code) puts "9.0" ahead of "10.0" --
    '9' > '1' character-wise -- so a machine with both KiCad 9 and 10
    installed side by side would silently prefer the older one. Splits
    each numeric run into an int so "10" sorts after "9" for real.
    """
    return tuple(int(part) if part.isdigit() else part for part in re.split(r"(\d+)", path))


def find_kicad_cli() -> Path:
    """Resolve the kicad-cli executable.

    Resolution order: ``KICAD_CLI_PATH`` env var override, then PATH
    (``shutil.which``), then common per-platform install-dir globs.
    """
    env_override = os.environ.get("KICAD_CLI_PATH")
    if env_override:
        path = Path(env_override)
        if path.is_file():
            return path
        raise KicadCliError(f"KICAD_CLI_PATH={env_override!r} does not point to a file")

    which = shutil.which("kicad-cli")
    if which:
        return Path(which)

    for pattern in _CANDIDATE_GLOBS.get(sys.platform, []):
        matches = sorted(glob.glob(pattern), key=_version_sort_key, reverse=True)
        if matches:
            return Path(matches[0])

    raise KicadCliError(
        "kicad-cli not found. Set KICAD_CLI_PATH, add kicad-cli to PATH, or install KiCad "
        "(https://www.kicad.org/download/)."
    )


def ensure_kicad_cli_on_path() -> Path:
    """Resolve kicad-cli and make sure its directory is on PATH.

    Needed so that ``kicad_tools``' own internal kicad-cli calls (e.g.
    ``Schematic.run_erc``) succeed even when only ``KICAD_CLI_PATH`` was
    set or kicad-cli was found via a candidate glob rather than PATH.
    Idempotent — safe to call repeatedly.
    """
    cli_path = find_kicad_cli()
    cli_dir = str(cli_path.parent)
    path_entries = os.environ.get("PATH", "").split(os.pathsep)
    if cli_dir not in path_entries:
        os.environ["PATH"] = cli_dir + os.pathsep + os.environ.get("PATH", "")
    return cli_path


_TIMEOUT_SECONDS = 900


def _run(args: list[str]) -> subprocess.CompletedProcess[str]:
    cli = find_kicad_cli()
    try:
        return subprocess.run(
            [str(cli), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:
        raise KicadCliError(
            f"kicad-cli timed out after {_TIMEOUT_SECONDS}s: {exc.cmd}"
        ) from exc


def _run_report(
    subcommand: list[str],
    input_path: str | Path,
    *,
    severity_all: bool,
    report_name: str,
    extra_args: tuple[str, ...] = (),
    ok_exit_codes: tuple[int, ...] | None = None,
) -> dict:
    input_path = Path(input_path)
    with tempfile.TemporaryDirectory() as tmp_dir:
        out_path = Path(tmp_dir) / report_name
        args = [
            *subcommand,
            "--format",
            "json",
            *(["--severity-all"] if severity_all else []),
            *extra_args,
            "-o",
            str(out_path),
            str(input_path),
        ]
        proc = _run(args)
        if ok_exit_codes is not None and proc.returncode not in ok_exit_codes:
            raise KicadCliError(
                f"kicad-cli {' '.join(subcommand)} exited {proc.returncode} "
                f"(expected one of {ok_exit_codes}):\nstderr: {proc.stderr}"
            )
        if not out_path.exists():
            raise KicadCliError(
                f"kicad-cli {' '.join(subcommand)} did not produce a report "
                f"(exit {proc.returncode}):\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
            )
        return json.loads(out_path.read_text(encoding="utf-8"))


def run_drc(
    pcb_path: str | Path,
    *,
    severity_all: bool = True,
    refill_zones: bool = False,
    schematic_parity: bool = False,
) -> dict:
    """Run ``kicad-cli pcb drc --format json`` and return the parsed report.

    ``refill_zones`` fills zones in memory before checking (the input file is
    never modified: ``--save-board`` is deliberately never passed).
    """
    extra = (
        *(("--refill-zones",) if refill_zones else ()),
        *(("--schematic-parity",) if schematic_parity else ()),
    )
    return _run_report(
        ["pcb", "drc"],
        pcb_path,
        severity_all=severity_all,
        report_name="drc_report.json",
        extra_args=extra,
        # No --exit-code-violations is passed, so a healthy run always exits 0;
        # anything else (1-4) is a crash that must not read as a clean report.
        ok_exit_codes=(0,),
    )


def run_erc(sch_path: str | Path, *, severity_all: bool = True) -> dict:
    """Run ``kicad-cli sch erc --format json`` and return the parsed report."""
    return _run_report(["sch", "erc"], sch_path, severity_all=severity_all, report_name="erc_report.json")


def export_netlist(sch_path: str | Path, out_path: str | Path) -> Path:
    """``kicad-cli sch export netlist --format kicadsexpr`` of the root sheet ``sch_path``
    into ``out_path``. kicad-cli walks the sheet tree from that root itself."""
    out_path = Path(out_path)
    out_path.unlink(missing_ok=True)
    proc = _run(["sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(out_path), str(sch_path)])
    if proc.returncode != 0 or not out_path.is_file():
        raise KicadCliError(
            f"kicad-cli sch export netlist exited {proc.returncode}:\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return out_path


def upgrade_schematic(sch_path: str | Path) -> subprocess.CompletedProcess[str]:
    """``kicad-cli sch upgrade --force`` of the one sheet file ``sch_path``: KiCad loads that
    file alone and rewrites it in place, so call it on a copy only, never on the user's file.

    A non-zero exit means KiCad could not load the file (3 for a raw line break in a quoted
    string). Upgrading a root says "saved" with exit 0 even when a sub-sheet does not load,
    so each sheet file must be passed on its own. Returns the process for its exit code and
    output; a timeout raises ``KicadCliError``."""
    return _run(["sch", "upgrade", "--force", str(sch_path)])


_DEFAULT_PCB_SVG_LAYERS = "F.Cu,B.Cu,F.Silkscreen,B.Silkscreen,Edge.Cuts"


def export_svg(input_path: str | Path, output_dir: str | Path, **extra_args: str) -> Path:
    """Export ``input_path`` (a ``.kicad_pcb`` or ``.kicad_sch``) to SVG under ``output_dir``.

    Secondary/human-facing rendering aid per docs/ARCHITECTURE.md — never the
    primary correctness signal. Extra flags (e.g. ``layers="F.Cu,B.Cu"``
    for a PCB export) are passed through as ``--<key> <value>``. PCB
    export requires at least one layer as of KiCad 9+; defaults to a
    sensible top-level set (copper, silkscreen, board outline) when the
    caller doesn't specify one.
    """
    input_path = Path(input_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if input_path.suffix == ".kicad_pcb":
        subcommand = ["pcb", "export", "svg", "--mode-multi"]
        extra_args.setdefault("layers", _DEFAULT_PCB_SVG_LAYERS)
    elif input_path.suffix == ".kicad_sch":
        subcommand = ["sch", "export", "svg"]
    else:
        raise ValueError(f"unsupported file type for export_svg: {input_path.suffix}")

    args = [*subcommand, "-o", str(output_dir)]
    for key, value in extra_args.items():
        args.extend([f"--{key.replace('_', '-')}", str(value)])
    args.append(str(input_path))

    proc = _run(args)
    if proc.returncode != 0:
        raise KicadCliError(
            f"kicad-cli {' '.join(subcommand)} failed (exit {proc.returncode}):\n"
            f"stdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return output_dir
