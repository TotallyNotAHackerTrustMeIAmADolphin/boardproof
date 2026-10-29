"""Manufacturing export toolset (archived backlog Phase 5, P5-01 + P5-02 + P5-03).

Wraps `kicad_tools`' assembly-export system (`kicad_tools.export.AssemblyPackage`)
rather than hand-rolling gerber/BOM/CPL generation -- per this project's
"use existing libraries" principle, and consistent with `kicad_tools`
repeatedly covering more than initial research credited it for.

Two real gaps found and worked around while building this (2026-09-02):

1. `PCB.export_manufacturing()` (the documented one-call convenience
   wrapper) always uses `AssemblyConfig`'s default `auto_lcsc=True`,
   which tries to hit a live LCSC-parts API via the optional `requests`
   dependency (`kicad-tools[parts]` extra) -- not installed here, and we
   don't want an implicit network dependency crashing the whole export
   anyway. This module calls `AssemblyConfig`/`AssemblyPackage` directly
   instead, with `auto_lcsc=False`, and fills in LCSC part numbers
   ourselves from `lcsc_overrides` (this project's own sourcing pipeline,
   `sourcing/asset_fetch.py`'s `fetch_part_assets`, already knows real
   LCSC IDs -- no need for kicad_tools' live-API auto-suggestion at all).
2. `kicad_tools`' internal gerber generation only searches PATH and a
   macOS-style install dir for `kicad-cli` -- same class of Windows
   path-resolution gap already found and fixed for `Schematic.run_erc()`
   in `kicad_cli.py`. Reused via `ensure_kicad_cli_on_path()`.
3. (build ticket 26, 2026-10-20) `kicad_tools`' schematic BOM is not used:
   its `build_hierarchy` resolves a sub-sheet against the working directory
   first (a stray same-named copy there became the BOM), lists a sheet used
   twice under the references of its symbols' own Reference property both
   times (`complex_hierarchy`: the 29 parts of the second use missing), lists the power
   symbols and PWR_FLAGs of a project symbol library (`#U0101`...), and
   skips KiCad 6's `Sheet file` key. The
   BOM is built from kicad-cli's netlist of the root sheet instead (one entry
   per placed, on-board instance), after `sheet_tree.walk` reached every
   sheet and agrees with that netlist, and written with `kicad_tools`'
   `export_bom` formatter (same columns as before).

Confirmed real output shape for `manufacturer="jlcpcb"` (verified against
the `simple_led` fixture): BOM columns are exactly
`Comment,Designator,Footprint,LCSC Part #`; CPL columns are exactly
`Designator,Val,Package,Mid X,Mid Y,Rotation,Layer` -- both already
JLCPCB's standard upload format, no reformatting needed.

Panelization (P5-02, added 2026-09-04) is delegated to **KiKit**
(https://github.com/yaqwsx/KiKit). License re-confirmed **MIT** three
independent ways before depending on it, per this project's standing
policy after being burned by `kiutils` (GPLv3), `easyeda2kicad`
(AGPL-3.0) and Freerouting (GPLv3):
  1. `gh api repos/yaqwsx/KiKit --jq .license.spdx_id` -> `MIT`
  2. the PyPI wheel's own METADATA classifier
     `License :: OSI Approved :: MIT License`
  3. the verbatim MIT text in the wheel's `kikit-1.8.1.dist-info/LICENCE`
     ("Copyright (c) 2020 Jan Mrazek").
MIT would permit importing KiKit directly (unlike the copyleft tools,
which must stay subprocess-isolated) -- but it is **not importable from
this project's interpreter**, for a purely technical reason:

  KiKit imports `pcbnew`, KiCad's own SWIG Python binding, which ships
  only inside KiCad's bundled interpreter and is not on PyPI. This repo
  runs on a standalone CPython 3.13; KiCad 10.0.5's bundled interpreter
  is CPython 3.11. So `pip install kikit` into our venv would install a
  package that cannot import. KiKit must be installed into KiCad's own
  Python (`"C:/Program Files/KiCad/<ver>/bin/python.exe" -m pip install
  kikit` on Windows -- which lands the `kikit` console script in
  `~/Documents/KiCad/<ver>/3rdparty/Python311/Scripts/`, a directory
  that is NOT on PATH) and driven as a subprocess. Hence the
  `find_kikit()` discovery below follows the same shape as
  `kicad_cli.find_kicad_cli()`: `KIKIT_PATH` env override -> PATH ->
  platform-specific glob fallback, matching `KICAD_CLI_PATH` /
  `NGSPICE_PATH` / `JAVA_PATH` / `FREEROUTING_JAR_PATH`.

Because of this, KiKit deliberately is NOT a `pyproject.toml` dependency
-- adding it there would install a broken copy into our venv. It is an
optional external tool, like Freerouting and ngspice.

Verified for real on 2026-09-04 with KiKit 1.8.1 + KiCad 10.0.5:
  * `simple_led`, 2x2 grid, mousebites: 3 source footprints -> 12 board
    footprints (3 x 4) + 32 `KiKit_MB_*` mousebite marker footprints.
  * `golden_attiny1616_board`, 2x3 grid, V-cuts + 5 mm frame + 3 tooling
    holes + 3 fiducials: 6 source footprints -> 36 board footprints
    (6 x 6) + 9 `KiKit_*` auxiliary footprints (3 `KiKit_TO_*` tooling,
    6 `KiKit_FID_[TB]_*` fiducials). Real `kicad-cli pcb drc`: source
    0 violations / 11 unconnected, panel 0 violations / 66 unconnected
    -- exactly 11 x 6, i.e. the panel introduced no new DRC problems.
All KiKit option names/enums used below were read out of KiKit's own
`panelize_ui_sections.py` and a `--dump`ed resolved preset, not guessed.
"""

from __future__ import annotations

import csv
import glob as _glob
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from kicad_mcp.kicad_cli import ensure_kicad_cli_on_path


def export_manufacturing_package(
    pcb_path: str | Path,
    output_dir: str | Path,
    *,
    schematic_path: str | Path | None = None,
    manufacturer: str = "jlcpcb",
    lcsc_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Export the manufacturing files (gerbers, drill, BOM, CPL) for a
    PCB, in one call -- never call this once per file format. Checks
    nothing about the board: only verify_board gives a verdict.

    `lcsc_overrides` (optional) maps reference designator -> LCSC part
    number (e.g. from this project's own sourcing pipeline) and is used
    to fill the BOM's "LCSC Part #" column directly, rather than relying
    on kicad_tools' live-API auto-suggestion (which needs an optional
    dependency this project doesn't install by default -- see module
    docstring). Evidence-gated: reports exactly what was produced and
    what wasn't, never implies a complete package when gerbers/BOM/CPL
    generation partially failed.
    """
    from kicad_tools.export import AssemblyConfig, AssemblyPackage
    from kicad_tools.schema import PCB

    pcb_path = Path(pcb_path)
    output_dir = Path(output_dir)
    if not pcb_path.exists():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}

    try:
        ensure_kicad_cli_on_path()
    except Exception as exc:  # noqa: BLE001 -- evidence-gated: report, gerbers just won't be produced
        kicad_cli_reason = str(exc)
    else:
        kicad_cli_reason = None

    try:
        PCB.load(pcb_path)  # validate the board loads before attempting export
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"failed to load PCB: {exc}"}

    output_dir.mkdir(parents=True, exist_ok=True)
    # AssemblyConfig has no create_zip field (that only exists on the
    # higher-level PCB.export_manufacturing()/export_gerbers_zip() wrappers
    # this function deliberately bypasses, per the auto_lcsc issue in the
    # module docstring) -- callers wanting a zip can archive output_dir
    # themselves; not worth reimplementing zip creation here for now.
    # The BOM is built here from kicad-cli's netlist of the root sheet (build ticket 26), not by
    # kicad_tools: its build_hierarchy resolved a sub-sheet in the working directory first (a
    # stray copy there became the BOM), gave both uses of a sheet used twice the first use's
    # references (complex_hierarchy: the 29 parts of the second use missing), and it does not
    # follow KiCad 6's "Sheet file" key.
    config = AssemblyConfig(output_dir=output_dir, auto_lcsc=False, include_bom=False)
    root = Path(schematic_path) if schematic_path else pcb_path.with_suffix(".kicad_sch")

    try:
        pkg = AssemblyPackage(
            pcb_path=pcb_path,
            schematic_path=root if root.is_file() else None,
            manufacturer=manufacturer,
            config=config,
        )
        result = pkg.export(output_dir)
    except Exception as exc:  # noqa: BLE001 -- evidence-gated, never let a partial export look like a crash with no info
        return {"status": "incomplete", "reason": f"export failed: {exc}"}

    errors: list[str] = list(getattr(result, "errors", []) or [])
    if kicad_cli_reason:
        errors.append(f"kicad-cli resolution: {kicad_cli_reason}")

    bom_path, tree_report, bom_error = _bom_from_root(root, pcb_path, output_dir, pkg.fab_family, config.bom_filename)
    if bom_error:
        errors.append(bom_error)
    lcsc_fill_count = 0
    if bom_path and lcsc_overrides:
        lcsc_fill_count = _fill_lcsc_column(Path(bom_path), lcsc_overrides)

    response: dict[str, Any] = {
        "status": "ok" if not errors else "partial",
        "manufacturer": manufacturer,
        "gerbers": str(getattr(result, "gerber_path", None) or "") or None,
        "bom": str(bom_path) if bom_path else None,
        "placement": str(getattr(result, "pnp_path", None) or "") or None,
        "lcsc_part_numbers_filled": lcsc_fill_count,
        "schematic": str(root) if root.is_file() else None,
        **tree_report,
    }
    if errors:
        response["errors"] = errors
    return response


_LCSC_FIELDS = ("lcsc", "lcsc_pn", "lcsc part", "lcsc part #", "jlc", "jlcpcb")
_MPN_FIELDS = ("mpn", "mfr_pn", "manufacturer_pn", "pn")
_MANUFACTURER_FIELDS = ("manufacturer", "mfr", "mfg")


def _bom_from_root(
    root: Path, pcb_path: Path, output_dir: Path, fab_family: str, filename: str
) -> tuple[Path | None, dict[str, Any], str | None]:
    """``(bom path, sheet-tree report, error)``. The BOM lists kicad-cli's netlist of the root
    sheet, one entry per placed instance, after the sheet tree walked from the root reached
    every sheet, the gate's annotation check finds nothing, and the netlist agrees with the
    placed instances (no reference naming two symbols); otherwise there is no BOM, no BOM file
    from an earlier run is left in ``output_dir``, and the error says why. Field names map as
    kicad_tools' own schematic BOM maps them."""
    from kicad_tools.export.bom_formats import BOMExportConfig, export_bom
    from kicad_tools.schema.bom import BOMItem, backfill_footprints_from_pcb

    from kicad_mcp import sheet_tree
    from kicad_mcp import verify_board as gate

    path = output_dir / filename.format(manufacturer=fab_family)
    path.unlink(missing_ok=True)  # a refused BOM must not leave the previous run's file behind
    if not root.is_file():
        return None, {}, (
            f"BOM not produced: root schematic not found: {root}. The root is <board>.kicad_sch "
            "unless schematic_path names the root .kicad_sch."
        )
    tree = sheet_tree.walk(root)
    report = tree.report()
    if tree.problems:
        return None, report, f"BOM not produced: the sheet tree of {root.name} is incomplete: " + "; ".join(tree.problems)
    annotation = gate._annotation_problems(root)  # read only: the gate's own check
    if annotation:
        return None, report, "BOM not produced: " + "; ".join(annotation)
    try:
        comps = sheet_tree.netlist_components(root)
    except Exception as exc:  # noqa: BLE001 -- evidence-gated
        return None, report, f"BOM not produced: kicad-cli netlist export failed: {exc}"
    disagreement = sheet_tree.netlist_disagreement(tree, [c["ref"] for c in comps])
    if disagreement:
        return None, report, "BOM not produced: " + "; ".join(disagreement)

    def field(comp: dict, names: tuple[str, ...]) -> str:
        return next((v for k, v in comp["fields"].items() if k.lower() in names and v), "")

    items = [
        BOMItem(
            reference=c["ref"],
            value=c["value"],
            footprint=c["footprint"],
            lib_id=c["lib_id"],
            datasheet=c["datasheet"],
            description=c["description"],
            manufacturer=field(c, _MANUFACTURER_FIELDS),
            mpn=field(c, _MPN_FIELDS),
            lcsc=field(c, _LCSC_FIELDS),
            dnp="dnp" in c["properties"],
            in_bom="exclude_from_bom" not in c["properties"],
            on_board="exclude_from_board" not in c["properties"],
        )
        for c in comps
        if not c["ref"].startswith("#")
    ]
    try:
        backfill_footprints_from_pcb(items, pcb_path)
    except Exception:  # noqa: BLE001,S110 -- as kicad_tools does: a blank footprint stays blank
        pass
    path.write_text(export_bom(items, fab_family, BOMExportConfig()), encoding="utf-8", newline="\n")
    return path, report, None


def _fill_lcsc_column(bom_path: Path, overrides: dict[str, str]) -> int:
    """Fill the BOM CSV's "LCSC Part #" column for any row whose
    Designator has a known LCSC override. Returns how many rows were
    filled. A BOM groups references by value/footprint (confirmed real
    output shape -- see module docstring), so a row's Designator field
    may contain multiple comma/space-separated refs; fill if any of them
    has an override.
    """
    with bom_path.open("r", newline="", encoding="utf-8") as fh:
        rows = list(csv.reader(fh))
    if not rows:
        return 0

    header = rows[0]
    try:
        designator_col = header.index("Designator")
        lcsc_col = header.index("LCSC Part #")
    except ValueError:
        return 0  # not the shape we expect -- don't guess, leave the file untouched

    filled = 0
    for row in rows[1:]:
        if len(row) <= max(designator_col, lcsc_col):
            continue
        refs = [r.strip() for r in row[designator_col].replace(",", " ").split()]
        for ref in refs:
            if ref in overrides:
                row[lcsc_col] = overrides[ref]
                filled += 1
                break

    if filled:
        with bom_path.open("w", newline="", encoding="utf-8") as fh:
            csv.writer(fh).writerows(rows)
    return filled


# --------------------------------------------------------------------------
# Panelization (P5-02) -- KiKit subprocess wrapper
# --------------------------------------------------------------------------


class KikitError(RuntimeError):
    """Raised when the KiKit executable can't be found."""


_KIKIT_INSTALL_HINT = (
    "KiKit (MIT, https://github.com/yaqwsx/KiKit) is not installed. It cannot be "
    "installed into this project's venv, because it imports `pcbnew`, which ships only "
    "inside KiCad's own bundled Python. Install it into KiCad's interpreter instead -- "
    'Windows: `"C:/Program Files/KiCad/<version>/bin/python.exe" -m pip install kikit`; '
    "Linux/macOS: run `pip install kikit` with the Python that can `import pcbnew`. "
    "Then either add the resulting `kikit` script's directory to PATH or set KIKIT_PATH "
    "to the executable."
)


def _kikit_candidate_globs() -> list[str]:
    """Platform-specific fallback locations for the `kikit` executable.

    On Windows, `pip install kikit` under KiCad's bundled interpreter puts
    the console script under the *user's* KiCad 3rdparty dir (confirmed
    with KiCad 10.0: `~/Documents/KiCad/10.0/3rdparty/Python311/Scripts/`),
    which pip itself warns is not on PATH -- so PATH lookup alone is not
    enough and this fallback is load-bearing, not defensive padding.
    """
    home = Path.home()
    if sys.platform == "win32":
        return [
            str(home / "Documents/KiCad/*/3rdparty/Python*/Scripts/kikit.exe"),
            "C:/Program Files/KiCad/*/bin/Scripts/kikit.exe",
            "C:/Program Files/KiCad/*/bin/kikit.exe",
        ]
    if sys.platform == "darwin":
        return [
            str(home / "Library/Application Support/kicad/*/3rdparty/Python*/bin/kikit"),
            "/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/Versions/*/bin/kikit",
        ]
    return [
        str(home / ".local/share/kicad/*/3rdparty/Python*/bin/kikit"),
        str(home / ".local/bin/kikit"),
    ]


def find_kikit() -> Path:
    """Resolve the KiKit executable.

    Resolution order matches `kicad_cli.find_kicad_cli()`: the
    ``KIKIT_PATH`` env override, then PATH, then platform-specific globs.
    """
    env_override = os.environ.get("KIKIT_PATH")
    if env_override:
        path = Path(env_override)
        if path.is_file():
            return path
        raise KikitError(f"KIKIT_PATH={env_override!r} does not point to a file")

    which = shutil.which("kikit")
    if which:
        return Path(which)

    for pattern in _kikit_candidate_globs():
        matches = sorted(_glob.glob(pattern), reverse=True)  # newest KiCad version first
        if matches:
            return Path(matches[0])

    raise KikitError(_KIKIT_INSTALL_HINT)


def kikit_version(kikit_path: Path | None = None) -> str | None:
    """Return KiKit's reported version, or None if it can't be queried."""
    try:
        exe = kikit_path or find_kikit()
        proc = subprocess.run(
            [str(exe), "--version"], capture_output=True, text=True, check=False, timeout=120
        )
    except Exception:  # noqa: BLE001 -- purely informational
        return None
    return (proc.stdout or proc.stderr).strip() or None


def _param_list(params: dict[str, Any]) -> str:
    """Render KiKit's `key: value; key: value` PARAMETER_LIST syntax."""
    return "; ".join(f"{k}: {v}" for k, v in params.items() if v is not None)


# Enum values read directly out of KiKit 1.8.1's own panelize_ui_sections.py
# (SChoice declarations), not guessed from documentation.
_SEPARATION_TYPES = ("mousebites", "vcuts")
_FRAMING_TYPES = ("none", "railstb", "railslr", "frame", "tightframe")
_TOOLING_TYPES = ("none", "3hole", "4hole")
_FIDUCIAL_TYPES = ("none", "3fid", "4fid")
_SIDECAR_SUFFIXES = (".kicad_pro", ".kicad_prl", ".kicad_dru")


def panelize_board(
    pcb_path: str | Path,
    output_path: str | Path,
    *,
    rows: int = 2,
    cols: int = 2,
    hspace_mm: float = 2.0,
    vspace_mm: float = 2.0,
    separation: str = "mousebites",
    tab_width_mm: float = 3.0,
    tab_count: int = 1,
    mousebite_drill_mm: float = 0.5,
    mousebite_spacing_mm: float = 1.0,
    mousebite_offset_mm: float = 0.2,
    vcut_layer: str = "Cmts_User",
    vcut_clearance_mm: float = 0.0,
    framing: str = "none",
    frame_width_mm: float = 5.0,
    frame_space_mm: float = 3.0,
    tooling: str = "none",
    tooling_hole_size_mm: float = 1.5,
    tooling_offset_mm: float = 2.5,
    fiducials: str = "none",
    fiducial_copper_size_mm: float = 1.0,
    fiducial_opening_mm: float = 1.0,
    fiducial_hoffset_mm: float = 5.0,
    fiducial_voffset_mm: float = 2.5,
    run_drc: bool = True,
    timeout_s: int = 900,
) -> dict[str, Any]:
    """Panelize a single board into an rows x cols grid panel via KiKit, in
    one fully-parameterized call -- never one knob per call.

    Evidence-gated in the strongest sense this project uses: a zero exit
    code is NOT treated as success. After KiKit runs, the resulting panel
    is loaded back through `kicad_tools` and its footprints counted; the
    call only reports `status: "ok"` if the panel actually contains
    `rows * cols` copies of every source footprint. Real DRC
    (`kicad-cli pcb drc`, KiCad's own engine, not the gate -- see archived backlog
    P1-14) is run on both the source board and the panel so the caller can
    see whether panelization introduced new violations, rather than
    inheriting the source board's pre-existing ones.

    If KiKit isn't installed, this returns `status: "incomplete"` with
    install instructions and produces no output file -- it never copies
    the source board through and calls it a panel.
    """
    pcb_path = Path(pcb_path)
    output_path = Path(output_path)

    if not pcb_path.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    if rows < 1 or cols < 1:
        return {"status": "incomplete", "reason": f"rows/cols must be >= 1, got {rows}x{cols}"}
    if separation not in _SEPARATION_TYPES:
        return {
            "status": "incomplete",
            "reason": f"separation must be one of {list(_SEPARATION_TYPES)}, got {separation!r}",
        }
    for name, value, allowed in (
        ("framing", framing, _FRAMING_TYPES),
        ("tooling", tooling, _TOOLING_TYPES),
        ("fiducials", fiducials, _FIDUCIAL_TYPES),
    ):
        if value not in allowed:
            return {
                "status": "incomplete",
                "reason": f"{name} must be one of {list(allowed)}, got {value!r}",
            }

    try:
        kikit = find_kikit()
    except KikitError as exc:
        return {"status": "incomplete", "reason": str(exc)}

    # KiKit runs on KiCad's own interpreter and touches KiCad libraries; and
    # kicad_tools' Windows kicad-cli discovery gap bites here too (this
    # project has hit it four separate times). Non-fatal -- report, don't abort.
    try:
        ensure_kicad_cli_on_path()
    except Exception as exc:  # noqa: BLE001
        kicad_cli_reason = str(exc)
    else:
        kicad_cli_reason = None

    try:
        source_refs = _footprint_refs(pcb_path)
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"failed to load source PCB: {exc}"}

    cuts: dict[str, Any]
    if separation == "mousebites":
        cuts = {
            "type": "mousebites",
            "drill": f"{mousebite_drill_mm}mm",
            "spacing": f"{mousebite_spacing_mm}mm",
            "offset": f"{mousebite_offset_mm}mm",
        }
    else:
        cuts = {
            "type": "vcuts",
            "layer": vcut_layer,
            "clearance": f"{vcut_clearance_mm}mm",
        }

    args: list[str] = [
        "panelize",
        "-l",
        _param_list(
            {
                "type": "grid",
                "rows": rows,
                "cols": cols,
                "hspace": f"{hspace_mm}mm",
                "vspace": f"{vspace_mm}mm",
            }
        ),
        "-t",
        _param_list(
            {
                "type": "fixed",
                "hcount": tab_count,
                "vcount": tab_count,
                "hwidth": f"{tab_width_mm}mm",
                "vwidth": f"{tab_width_mm}mm",
            }
        ),
        "-c",
        _param_list(cuts),
    ]
    if framing != "none":
        args += [
            "-r",
            _param_list(
                {
                    "type": framing,
                    "width": f"{frame_width_mm}mm",
                    "space": f"{frame_space_mm}mm",
                }
            ),
        ]
    if tooling != "none":
        args += [
            "-o",
            _param_list(
                {
                    "type": tooling,
                    "size": f"{tooling_hole_size_mm}mm",
                    "hoffset": f"{tooling_offset_mm}mm",
                    "voffset": f"{tooling_offset_mm}mm",
                }
            ),
        ]
    if fiducials != "none":
        args += [
            "-f",
            _param_list(
                {
                    "type": fiducials,
                    "coppersize": f"{fiducial_copper_size_mm}mm",
                    "opening": f"{fiducial_opening_mm}mm",
                    "hoffset": f"{fiducial_hoffset_mm}mm",
                    "voffset": f"{fiducial_voffset_mm}mm",
                }
            ),
        ]
    args += [str(pcb_path), str(output_path)]

    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [str(kikit), *args]
    try:
        proc = subprocess.run(
            command, capture_output=True, text=True, check=False, timeout=timeout_s
        )
    except subprocess.TimeoutExpired:
        return {
            "status": "incomplete",
            "reason": f"kikit panelize timed out after {timeout_s}s",
            "command": command,
        }
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"kikit panelize failed to run: {exc}"}

    if proc.returncode != 0 or not output_path.is_file():
        return {
            "status": "incomplete",
            "reason": (
                f"kikit panelize failed (exit {proc.returncode}).\n"
                f"stdout: {proc.stdout.strip()}\nstderr: {proc.stderr.strip()}"
            ),
            "command": command,
        }

    # ---- Real verification: a zero exit code proves nothing ----
    try:
        panel_refs = _footprint_refs(output_path)
    except Exception as exc:  # noqa: BLE001
        return {
            "status": "incomplete",
            "reason": f"kikit exited 0 but the panel could not be loaded back: {exc}",
            "panel_path": str(output_path),
            "command": command,
        }

    boards = rows * cols
    # KiKit's own auxiliary footprints (mousebite markers KiKit_MB_*, tooling
    # holes KiKit_TO_*, fiducials KiKit_FID_[TB]_*) are all `KiKit_`-prefixed --
    # confirmed on real output; they must not count toward the board multiple.
    aux = [r for r in panel_refs if r.startswith("KiKit")]
    board_fps = [r for r in panel_refs if not r.startswith("KiKit")]
    expected = len(source_refs) * boards
    multiple_ok = len(board_fps) == expected

    verification: dict[str, Any] = {
        "source_footprints": len(source_refs),
        "expected_board_footprints": expected,
        "panel_board_footprints": len(board_fps),
        "kikit_auxiliary_footprints": len(aux),
        "footprint_multiple_ok": multiple_ok,
    }

    warnings: list[str] = []
    if kicad_cli_reason:
        warnings.append(f"kicad-cli resolution: {kicad_cli_reason}")
    if not multiple_ok:
        warnings.append(
            f"panel contains {len(board_fps)} board footprints, expected "
            f"{len(source_refs)} x {boards} = {expected}"
        )

    if run_drc:
        drc = _drc_pair(pcb_path, output_path)
        verification["drc"] = drc
        if drc.get("status") != "complete":
            warnings.append(f"DRC did not complete: {drc.get('reason')}")
        elif drc["panel_violations"] > drc["source_violations"] * boards:
            warnings.append(
                f"panel has {drc['panel_violations']} DRC violations, more than the "
                f"{drc['source_violations'] * boards} inherited from the source board"
            )

    sidecars = [
        str(p)
        for suffix in _SIDECAR_SUFFIXES
        if (p := output_path.with_suffix(suffix)).is_file()
    ]

    return {
        "status": "ok" if not warnings else "partial",
        **({"warnings": warnings} if warnings else {}),
        "panel_path": str(output_path),
        "boards": boards,
        "layout": {
            "rows": rows,
            "cols": cols,
            "hspace_mm": hspace_mm,
            "vspace_mm": vspace_mm,
            "separation": separation,
            "framing": framing,
            "tooling": tooling,
            "fiducials": fiducials,
        },
        "verification": verification,
        "sidecar_files": sidecars,
        "kikit_path": str(kikit),
        "kikit_version": kikit_version(kikit),
        "command": command,
    }


def _footprint_refs(pcb_path: Path) -> list[str]:
    """Reference designators of every footprint on a board, via kicad_tools.

    Deliberately goes through `kicad_tools` rather than reading the
    `.kicad_pcb` as text -- same rule as every other board access here.
    """
    from kicad_tools.schema import PCB

    return [fp.reference for fp in PCB.load(pcb_path).footprints]


def _drc_pair(source: Path, panel: Path) -> dict[str, Any]:
    """Run the real kicad-cli DRC on both boards so a caller can tell
    panel-introduced violations apart from ones the source board already had.
    """
    from kicad_mcp import kicad_cli

    try:
        src_report = kicad_cli.run_drc(source)
        panel_report = kicad_cli.run_drc(panel)
    except Exception as exc:  # noqa: BLE001 -- evidence-gated, never raise out of a tool
        return {"status": "incomplete", "reason": str(exc)}

    return {
        "status": "complete",
        "source_violations": len(src_report.get("violations", [])),
        "source_unconnected": len(src_report.get("unconnected_items", [])),
        "panel_violations": len(panel_report.get("violations", [])),
        "panel_unconnected": len(panel_report.get("unconnected_items", [])),
        "panel_violation_details": panel_report.get("violations", []),
    }


def build(mcp: MCPServer) -> list[str]:
    mcp.add_tool(
        export_manufacturing_package,
        name="export_manufacturing_package",
        description=(
            "Export the manufacturing files (gerbers, drill, BOM, CPL) for a PCB in one call, "
            "targeting a manufacturer's expected format (default 'jlcpcb', which produces "
            "JLCPCB's standard BOM/CPL CSV column shapes). Pass 'lcsc_overrides' (ref -> LCSC "
            "part number, from this project's own sourcing pipeline) to populate the BOM's LCSC "
            "column -- never call this once per output format. Exporting checks nothing: the "
            "files are written for any board that loads, unrouted or failing DRC included, and "
            "status 'ok' means only that the export reported no errors. Run verify_board first: "
            "only its "
            "PASS may be reported as routed or done (the `gate` field shows its last verdict "
            "for this file); pcb_check_manufacturability compares the board's rules with the "
            "fab's limits."
        ),
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": True,
        },
    )
    mcp.add_tool(
        panelize_board,
        name="panelize_board",
        description=(
            "Panelize a single board into a rows x cols production panel using KiKit (MIT), "
            "fully parameterized in ONE call: grid size, board spacing, separation method "
            "('mousebites' or 'vcuts'), tab geometry, framing/rails, tooling holes and "
            "fiducials -- never one call per knob. Requires KiKit installed into KiCad's own "
            "bundled Python (it needs `pcbnew`); set KIKIT_PATH if the `kikit` executable is "
            "not on PATH. Evidence-gated: the panel is loaded back and its footprints counted "
            "to prove it really contains rows*cols copies of the source board, and real "
            "kicad-cli DRC is run on both source and panel -- if KiKit is missing the call "
            "returns status 'incomplete' with install instructions and writes no file, it "
            "never passes an un-panelized board off as a panel. Those DRC counts are single "
            "runs for spotting violations the panel added, not the acceptance gate: run "
            "verify_board on the source board, and only its PASS may be reported as routed or "
            "done."
        ),
        annotations={
            "readOnlyHint": False,
            "destructiveHint": False,
            "idempotentHint": True,
            "openWorldHint": False,
        },
    )
    return ["export_manufacturing_package", "panelize_board"]
