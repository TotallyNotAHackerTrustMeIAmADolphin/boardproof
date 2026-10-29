"""KiCad's own view of a demo board, produced in the same test run (build ticket 43; docs/TESTING.md
layer 3: the oracle is a file KiCad wrote, with reference values from KiCad, never stored).

Three sources, all run on a copy of the board in a temporary directory:

- ``kicad-cli pcb export pos`` (CSV, mm, both sides): footprint position and rotation, y axis
  up, page frame, or relative to the drill/place (aux) origin with ``--use-drill-file-origin``.
- ``kicad-cli pcb export ipcd356``: every pad's absolute position, in 0.0001 inch, y axis up,
  relative to the aux origin.
- KiCad's ``pcbnew`` module in KiCad's bundled Python (``tests/_pcbnew_dump.py``): pad
  orientation and size, zone outlines and Edge.Cuts shapes, which no ``kicad-cli`` export
  carries exactly. It runs with a throwaway ``KICAD_CONFIG_HOME`` and wx logging on stderr
  (builder brief rules 7 and 8), so it can never show a dialog or touch the owner's profile.

A missing KiCad Python skips or fails by the same ``KICAD_DEMOS_REQUIRED`` rule as the demos
(``tests/_kicad_demos.py``): it ships with the same install.
"""
from __future__ import annotations

import csv
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from tests import _kicad_demos

DUMP_SCRIPT = Path(__file__).with_name("_pcbnew_dump.py")
FIXTURE_SCRIPT = Path(__file__).with_name("_pcbnew_fixture.py")
# wx logging to stderr before pcbnew loads, as in every pcbnew run (builder brief rule 8).
_PROBE = "import wx; wx.Log.SetActiveTarget(wx.LogStderr()); import pcbnew"
NM_PER_MM = 1_000_000
IPC_UNIT_MM = 0.00254  # 0.0001 inch (UNITS CUST 0)
_TIMEOUT_S = 600

# The 6 boards from paradigm-review-2 ticket 09 (rotations incl. 45 degrees, back sides,
# non-zero aux origins), plus the two outline boards whose gr_arc sticks out past the lines.
POSITION_BOARDS = {
    "pic_programmer": "pic_programmer/pic_programmer.kicad_pcb",
    "video": "video/video.kicad_pcb",
    "vme-wren": "vme-wren/vme-wren.kicad_pcb",
    "tiny_tapeout": "tiny_tapeout/tinytapeout-demo.kicad_pcb",
    "multichannel": "multichannel/multichannel_mixer.kicad_pcb",
    "stickhub": "stickhub/StickHub.kicad_pcb",
}
OUTLINE_BOARDS = {
    "cm5_minima": "cm5_minima/CM5_MINIMA_3.kicad_pcb",
    "nfc_antenna": "royalblue54L_feather/nfc_antenna/RoyalBlue54L-NFC-Antenna/RoyalBlue54L-NFC-Antenna.kicad_pcb",
    "royalblue54L_feather": "royalblue54L_feather/RoyalBlue54L-Feather.kicad_pcb",
}
ALL_BOARDS = {**POSITION_BOARDS, **OUTLINE_BOARDS}

_IPC_XY = re.compile(r"X([+-]\d+)Y([+-]\d+)")


def copy_board(demos_dir: Path, relative: str, dest_dir: Path) -> Path:
    """Copy one board (and its ``.kicad_pro``, when there is one) from the install into
    ``dest_dir`` and return the copy. Only these files are read from the install: copying a
    whole demo hits Windows' MAX_PATH on ``video``'s library."""
    source = Path(demos_dir) / relative
    if not source.is_file():
        _kicad_demos.missing(f"demo board {relative!r} is not in {demos_dir}")
    dest_dir.mkdir(parents=True, exist_ok=True)
    board = dest_dir / source.name
    shutil.copyfile(source, board)
    project = source.with_suffix(".kicad_pro")
    if project.is_file():
        shutil.copyfile(project, dest_dir / project.name)
    return board


def _cli(args: list[str]) -> None:
    from kicad_mcp import kicad_cli

    cli = kicad_cli.ensure_kicad_cli_on_path()
    done = subprocess.run([str(cli), *args], capture_output=True, text=True, timeout=_TIMEOUT_S)
    if done.returncode != 0:
        raise AssertionError(f"kicad-cli {' '.join(args)} exited {done.returncode}: {done.stderr}")


def position_file(board: Path, out: Path, *, drill_origin: bool = False) -> list[dict]:
    """``kicad-cli pcb export pos`` rows: Ref, Val, Package, PosX, PosY (y up), Rot, Side."""
    args = ["pcb", "export", "pos", "--format", "csv", "--units", "mm", "--side", "both"]
    if drill_origin:
        args.append("--use-drill-file-origin")
    _cli([*args, "-o", str(out), str(board)])
    with open(out, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


@dataclass(frozen=True)
class IpcPad:
    reference: str  # columns 21-26: KiCad cuts a longer reference to 6 characters
    pin: str  # columns 28-31, cut to 4 characters
    x_mm: float  # y axis up, relative to the aux origin
    y_mm: float


def ipcd356(board: Path, out: Path) -> list[IpcPad]:
    """The pad records (op codes 317 and 327, vias left out) of ``kicad-cli pcb export ipcd356``."""
    _cli(["pcb", "export", "ipcd356", "-o", str(out), str(board)])
    pads = []
    for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith(("317", "327")) or line[20:26].strip() == "VIA":
            continue
        match = _IPC_XY.search(line, 41)
        pads.append(IpcPad(
            line[20:26].strip(), line[27:31].strip(),
            int(match.group(1)) * IPC_UNIT_MM, int(match.group(2)) * IPC_UNIT_MM,
        ))
    return pads


def kicad_python() -> Path:
    """KiCad's bundled Python next to kicad-cli (Windows), else a ``python3`` that can import
    ``pcbnew`` (Linux packages). Missing: skip, or fail when ``KICAD_DEMOS_REQUIRED`` is set."""
    from kicad_mcp import kicad_cli

    try:
        cli = Path(kicad_cli.ensure_kicad_cli_on_path())
    except kicad_cli.KicadCliError as exc:
        _kicad_demos.missing(f"kicad-cli was not found ({exc})", what="KiCad's Python")
    candidates = [cli.parent / "python.exe", cli.parent / "python3", Path("/usr/bin/python3")]
    with tempfile.TemporaryDirectory(prefix="kicad-config-") as config_home:
        for candidate in candidates:
            if candidate.is_file():
                probe = subprocess.run(
                    [str(candidate), "-I", "-c", _PROBE], capture_output=True,
                    timeout=120, env=_quiet_env(Path(config_home)),
                )
                if probe.returncode == 0:
                    return candidate
    _kicad_demos.missing(
        "no Python that can import pcbnew next to kicad-cli "
        f"({', '.join(str(c) for c in candidates)})",
        what="KiCad's Python (pcbnew)",
    )


def _quiet_env(config_home: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
    env["KICAD_CONFIG_HOME"] = str(config_home)
    return env


def _run_pcbnew_script(python: Path, script: Path, args: list[Path], scratch: Path) -> dict:
    """Run ``script`` in KiCad's Python with a throwaway ``KICAD_CONFIG_HOME`` under
    ``scratch``; its stdout is JSON."""
    config_home = scratch / "kicad-config"
    config_home.mkdir(parents=True, exist_ok=True)
    done = subprocess.run(
        [str(python), "-I", str(script), *map(str, args)],
        capture_output=True, text=True, encoding="utf-8", timeout=_TIMEOUT_S,
        env=_quiet_env(config_home),
    )
    if done.returncode != 0:
        raise AssertionError(f"{script.name} {args} exited {done.returncode}: {done.stderr[-2000:]}")
    return json.loads(done.stdout)


def pcbnew_dump(python: Path, board: Path, scratch: Path) -> dict:
    """What KiCad's ``pcbnew`` loads from ``board`` (``tests/_pcbnew_dump.py``)."""
    return _run_pcbnew_script(python, DUMP_SCRIPT, [board], scratch)


def pcbnew_fixture(python: Path, source: Path, out: Path, scratch: Path) -> dict:
    """Build the padstack and zone-hole board ``out`` from ``source`` with KiCad's own
    ``pcbnew`` (``tests/_pcbnew_fixture.py``); returns the uuids of what it changed."""
    return _run_pcbnew_script(python, FIXTURE_SCRIPT, [source, out], scratch)


@dataclass
class BoardOracle:
    """One demo board copied to a scratch directory, with KiCad's exports of it. ``made``
    names what ``pcbnew_fixture`` changed, for a board built by it."""

    name: str
    board: Path
    scratch: Path
    python: Path
    made: dict = field(default_factory=dict)
    _cache: dict = field(default_factory=dict)

    def root(self):
        """The board's parsed tree (``write_layer.load``), parsed once: every reader takes it."""
        if "root" not in self._cache:
            from kicad_mcp import write_layer

            self._cache["root"] = write_layer.load(self.board)
        return self._cache["root"]

    def positions(self, *, drill_origin: bool = False) -> list[dict]:
        key = ("pos", drill_origin)
        if key not in self._cache:
            out = self.scratch / ("pos-drill.csv" if drill_origin else "pos.csv")
            self._cache[key] = position_file(self.board, out, drill_origin=drill_origin)
        return self._cache[key]

    def ipc_pads(self) -> list[IpcPad]:
        if "ipc" not in self._cache:
            self._cache["ipc"] = ipcd356(self.board, self.scratch / "board.d356")
        return self._cache["ipc"]

    def pcbnew(self) -> dict:
        if "pcbnew" not in self._cache:
            self._cache["pcbnew"] = pcbnew_dump(self.python, self.board, self.scratch)
        return self._cache["pcbnew"]


def nm_to_mm(value: int) -> float:
    return value / NM_PER_MM


def angle_delta(a: float, b: float) -> float:
    """The smallest difference between two angles in degrees (0 <= d <= 180)."""
    return abs((a - b + 180.0) % 360.0 - 180.0)

