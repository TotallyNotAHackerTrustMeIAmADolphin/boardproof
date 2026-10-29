"""Fetch ready KiCad symbol/footprint/3D-model assets for an LCSC part number.

Wraps the `easyeda2kicad` CLI tool as an arm's-length subprocess --
deliberately NOT `import easyeda2kicad` anywhere in this codebase. Same
reasoning docs/ARCHITECTURE.md already applied to kiutils: `easyeda2kicad` is
**AGPL-3.0 licensed** (confirmed via `pip show easyeda2kicad`), not the
permissive tool the original research assumed. Importing it as a library
and distributing the combined program would put the whole project under
AGPL (network-copyleft, stricter than plain GPL). Invoking it as a
separately-installed process -- the same arm's-length pattern already used
for `kicad-cli` (kicad_cli.py) and planned for Freerouting -- keeps this
project's own code independently licensed. Flagged for docs/ARCHITECTURE.md,
see archived backlog P1-05 notes.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

# lcsc_id may originate from third-party sourcing-provider data -- treat it
# as untrusted. Security fix (2026-09-02, independent review): the previous
# `startswith("C")` check let a value like "C:/Windows/Temp/x" through,
# and `output_dir / lcsc_id` silently discards output_dir entirely when the
# right-hand side looks like an absolute/drive-rooted path (documented
# pathlib behavior), writing outside the intended directory. A real LCSC
# part number is always "C" followed by digits only.
_LCSC_ID_RE = re.compile(r"^C\d+$")


def fetch_part_assets(lcsc_id: str, output_dir: str | Path, *, overwrite: bool = True) -> dict:
    """Retrieve a KiCad symbol, footprint, and 3D model for an LCSC part
    number into output_dir, via the easyeda2kicad CLI.

    Evidence-gated (docs/ARCHITECTURE.md): a partial or failed fetch is reported
    explicitly in the returned dict's "status"/"reason", never raised as an
    unhandled exception and never silently presented as a full success.
    """
    if not _LCSC_ID_RE.match(lcsc_id):
        return {"status": "error", "reason": f"lcsc_id {lcsc_id!r} is not a valid LCSC part number (expected 'C' followed by digits)"}

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lib_base = output_dir / lcsc_id

    cmd = [
        sys.executable,
        "-m",
        "easyeda2kicad",
        f"--lcsc_id={lcsc_id}",
        "--full",
        f"--output={lib_base}",
    ]
    if overwrite:
        cmd.append("--overwrite")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "error", "lcsc_id": lcsc_id, "reason": f"failed to run easyeda2kicad: {exc}"}

    symbol_path = lib_base.with_suffix(".kicad_sym")
    footprint_dir = Path(f"{lib_base}.pretty")
    model_dir = Path(f"{lib_base}.3dshapes")

    got = {
        "symbol_path": str(symbol_path) if symbol_path.is_file() else None,
        "footprint_dir": str(footprint_dir) if footprint_dir.is_dir() and any(footprint_dir.iterdir()) else None,
        "model_3d_dir": str(model_dir) if model_dir.is_dir() and any(model_dir.iterdir()) else None,
    }
    retrieved = [k for k, v in got.items() if v]

    result: dict = {"lcsc_id": lcsc_id, **got}
    if not retrieved:
        result["status"] = "error"
        result["reason"] = proc.stderr.strip() or proc.stdout.strip() or f"easyeda2kicad exited {proc.returncode} with no output"
    elif len(retrieved) < 3:
        result["status"] = "partial"
        result["reason"] = f"only retrieved: {retrieved}"
        if proc.stderr.strip():
            result["reason"] += f"; stderr: {proc.stderr.strip()}"
    else:
        result["status"] = "ok"
    return result
