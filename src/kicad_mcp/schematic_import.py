"""Import footprints and nets from a schematic into a board (propose/confirm).

Contract: build ticket 17.
Ticket 10 measured what ``kicad_tools.PCB.from_schematic`` gets wrong on the
rung-1 project: it searches only the installed KiCad libraries (so J1, a part
of a project-local library such as rung 1's Rung1_USBC, is silently dropped), and without ``kicad-cli``
on PATH it falls back to a pure-Python netlist extractor that mislabels
PWR_FLAG and no-connect pins. This module is "Update PCB from Schematic" done
under the checkpoint discipline:

* ``kicad-cli`` is put on PATH first and is the only netlist source (no
  fallback extractor: a failed export is a failed import).
* Every footprint is resolved strictly through the project ``fp-lib-table``,
  then the installed KiCad libraries, then the user's global table. There is no
  "search every library" fallback. One unresolved footprint blocks the whole
  proposal and fails the whole confirm, naming the part; nothing is written.
* New footprints are staged on a deterministic grid (see ``_stage``): to the
  right of the Edge.Cuts bounding box by default, or from a declared origin.
  Placement itself is a separate step.
* The result is a NEW revision directory built beside the source, reloaded
  fresh from disk and verified against the kicad-cli netlist before it is
  renamed into place (the shared checkpoint helper, ``kicad_mcp.checkpoint``).
  The source project is never touched.

Mutations go through ``kicad_tools`` (``add_footprint_from_file``,
``assign_net_to_footprint_pad``), never raw text. Every position or net fact
used for verification is read from the raw S-expression (``parse_file``),
never from ``Footprint.position`` / ``Pad`` convenience properties (CLAUDE.md
hard rule 2).

Test seams (``footprint_dirs``, ``netlist_exporter``, module ``_clock``) are
keyword parameters of the Python functions only; the tool layer must not
expose them.
"""

from __future__ import annotations

import hashlib
import os
import re
import json
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from kicad_mcp import checkpoint, write_layer
from kicad_mcp import verify_board as gate
from kicad_mcp.sheet_tree import netlist_disagreement, path_key, placed_instances
from kicad_mcp.sheet_tree import walk as walk_sheet_tree

if not hasattr(gate, "_annotation_problems"):  # the sheet-hierarchy walk is reused, not copied
    raise ImportError("schematic_import reuses kicad_mcp.verify_board._annotation_problems, which no longer exists")

_MAX_COPY_BYTES = 512 * 1024 * 1024

_clock = time.time  # replaced in tests
# token -> {"sch", "out_dir", "plan_hash", "staging_origin", "columns", "spacing"}
_PENDING = checkpoint.Pending(clock=lambda: _clock())
_DRIFT = "the board or schematic changed since the proposal was made: re-run propose_schematic_import"
_KEPT = "nothing was written and the revision_token is still valid"

_EXPORT_TIMEOUT = 120.0  # seconds for the kicad-cli netlist export (replaced in tests)
_JOIN_GRACE = 2.0  # the watcher waits this much longer than the subprocess timeout, so the child dies first

_STAGING_MARGIN = 10.0  # mm between the outline's right edge and the first staged column
_DEFAULT_COLUMNS = 5
_DEFAULT_SPACING = 15.0

NetlistExporter = Callable[[Path, Path], Any]


# --- small helpers ------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _norm(path: str | Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _parse(path: str | Path) -> Any:
    from kicad_tools.sexp import parse_file

    return parse_file(str(path))


def _natural_key(ref: str) -> list:
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", ref)]


# --- netlist ------------------------------------------------------------------


class _Blocked(Exception):
    """The import cannot be proposed; the message is the reason (no token is issued)."""


def _kicad_cli_netlist(sch_path: Path, out_path: Path) -> Any:
    """Netlist via kicad-cli only, under a timeout. No Python fallback extractor:
    it mislabels PWR_FLAG and no-connect pins."""
    from kicad_tools.operations.netlist import Netlist

    from kicad_mcp.kicad_cli import ensure_kicad_cli_on_path

    cli = ensure_kicad_cli_on_path()
    cmd = [str(cli), "sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(out_path), str(sch_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False, timeout=_EXPORT_TIMEOUT)
    except subprocess.TimeoutExpired as exc:
        raise _Blocked(f"kicad-cli netlist export timed out after {_EXPORT_TIMEOUT:g}s") from exc
    if proc.returncode != 0 or not out_path.is_file():
        raise RuntimeError(f"kicad-cli netlist export failed (exit {proc.returncode}): {proc.stderr.strip()}")
    return Netlist.load(out_path)


def _call_with_timeout(fn: Callable[[], Any]) -> Any:
    """Run ``fn`` and give up after ``_EXPORT_TIMEOUT`` seconds (covers any exporter, not only the
    subprocess one). A hung exporter thread is a daemon: it cannot keep the server alive."""
    box: dict[str, Any] = {}

    def target() -> None:
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread
            box["error"] = exc

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    thread.join(_EXPORT_TIMEOUT + _JOIN_GRACE)
    if thread.is_alive():
        raise _Blocked(f"netlist export did not finish within {_EXPORT_TIMEOUT:g}s")
    if "error" in box:
        raise box["error"]
    return box["value"]


def _netlist_components(path: Path) -> list[dict]:
    """Raw component records of an exported kicadsexpr netlist (symbol uuid, sheet path, sheet properties)."""
    comps = _parse(path).find("components")
    out: list[dict] = []
    for comp in comps.find_all("comp") if comps is not None else []:
        def first(node: Any, key: str) -> str:
            # direct children only: find() descends, and sheetpath also holds a tstamps node
            n = next((c for c in node.children if c.name == key), None)
            return str(n.values[0]) if n is not None and n.values else ""

        props: dict[str, str] = {}
        dnp = False
        for prop in comp.find_all("property"):
            name = first(prop, "name")
            if name == "dnp":
                dnp = True
            props[name] = first(prop, "value")
        sheetpath = comp.find("sheetpath")
        out.append(
            {
                "reference": first(comp, "ref"),
                "value": first(comp, "value"),
                "tstamps": first(comp, "tstamps"),
                # every symbol uuid of the part: kicad-cli lists all units of a multi-unit part here
                "uuids": [str(v) for c in comp.children if c.name == "tstamps" for v in c.values],
                "sheet_names": first(sheetpath, "names") if sheetpath is not None else "",
                "sheet_tstamps": first(sheetpath, "tstamps") if sheetpath is not None else "",
                "sheetname": props.get("Sheetname", ""),
                "sheetfile": props.get("Sheetfile", ""),
                "dnp": dnp,
            }
        )
    return out


def _netlist_nodes(path: Path) -> set[tuple[str, str]]:
    """``(reference, pin)`` of every ``(nets (net ... (node (ref R) (pin P) ...)))`` of an exported
    kicadsexpr netlist, read from the raw S-expression. kicad-cli gives every pin of a placed
    unit a node, an unconnected pin on a net of its own (``unconnected-(U30A-GPIO12-Pad40)``)."""
    out: set[tuple[str, str]] = set()
    for nets in _parse(path).find_children("nets"):
        for net in nets.find_children("net"):
            for node in net.find_children("node"):
                ref, pin = node.find_children("ref"), node.find_children("pin")
                if ref and pin:
                    out.add((ref[0].get_string(0) or "", pin[0].get_string(0) or ""))
    return out


def _load_netlist(
    sch_path: Path, exporter: NetlistExporter | None
) -> tuple[Any, list[dict], set[tuple[str, str]]]:
    """``(Netlist, raw component records, raw net nodes)``; the raw records and nodes are empty
    when the exporter wrote no file."""
    td = Path(tempfile.mkdtemp(prefix="kmcp-net-"))
    try:
        out = td / "import.kicad_net"
        netlist = _call_with_timeout(lambda: (exporter or _kicad_cli_netlist)(sch_path, out))
        if not out.is_file():
            return netlist, [], set()
        return netlist, _netlist_components(out), _netlist_nodes(out)
    finally:
        for _ in range(10):  # a just-killed child can hold the directory for a moment on Windows
            shutil.rmtree(td, ignore_errors=True)
            if not td.exists():
                break
            time.sleep(0.1)


def _netlist_digest(netlist: Any) -> str:
    comps = sorted((c.reference, c.value, c.footprint) for c in netlist.components)
    nets = sorted((n.name, sorted((x.reference, str(x.pin)) for x in n.nodes)) for n in netlist.nets if n.name)
    return hashlib.sha256(json.dumps([comps, nets]).encode()).hexdigest()


# --- footprint resolution -----------------------------------------------------


def _expand(uri: str, project_dir: Path) -> Path | None:
    uri = uri.replace("${KIPRJMOD}", str(project_dir))

    def sub(match: re.Match) -> str:
        return os.environ.get(match.group(1), match.group(0))

    uri = re.sub(r"\$\{([^}]+)\}", sub, uri)
    return None if "${" in uri else Path(uri)


def _read_lib_table(table: Path, project_dir: Path) -> dict[str, Path]:
    if not table.is_file():
        return {}
    libs: dict[str, Path] = {}
    for node in _parse(table).children:
        if node.name != "lib":
            continue
        name, uri = node.find("name"), node.find("uri")
        if name is None or uri is None or not name.values or not uri.values:
            continue
        path = _expand(str(uri.values[0]), project_dir)
        if path is not None:
            libs[str(name.values[0])] = path
    return libs


def _installed_footprint_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("KICAD_FOOTPRINT_DIR")
    if env:
        dirs.append(Path(env))
    try:
        from kicad_mcp.kicad_cli import find_kicad_cli

        cli = find_kicad_cli()
        dirs.append(cli.parent.parent / "share" / "kicad" / "footprints")
    except Exception:  # noqa: BLE001
        pass
    return [d for d in dirs if d.is_dir()]


def _global_table_libs(project_dir: Path) -> dict[str, Path]:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return {}
    libs: dict[str, Path] = {}
    for table in sorted(Path(appdata, "kicad").glob("*/fp-lib-table")):
        libs.update(_read_lib_table(table, project_dir))
    return libs


class _Resolver:
    def __init__(self, project_dir: Path, footprint_dirs: Sequence[str | Path] | None) -> None:
        self.project_dir = project_dir.resolve()
        self.project = _read_lib_table(project_dir / "fp-lib-table", project_dir)
        dirs = [Path(d) for d in footprint_dirs] if footprint_dirs is not None else _installed_footprint_dirs()
        self.installed = dirs
        # The global table is only consulted for the real installed-library mode.
        self.global_libs = _global_table_libs(project_dir) if footprint_dirs is None else {}

    # Resolution order, as in KiCad: project fp-lib-table, then the global fp-lib-table, then the
    # installed standard libraries. A nickname found in a table never falls through to another source.
    def resolve(self, library_id: str) -> tuple[Path | None, str, str]:
        """``(file, source, reason)``; ``file`` is None when unresolved."""
        if not library_id:
            return None, "", "the symbol has an empty Footprint field"
        if ":" not in library_id:
            return None, "", f"footprint {library_id!r} has no library nickname"
        nick, name = library_id.split(":", 1)
        if nick in self.project:
            f = self.project[nick] / f"{name}.kicad_mod"
            if f.is_file():
                return f, "project", ""
            return None, "", f"library {nick!r} (project fp-lib-table, {self.project[nick]}) has no {name}.kicad_mod"
        if nick in self.global_libs:  # a global-table nickname is authoritative in KiCad
            f = self.global_libs[nick] / f"{name}.kicad_mod"
            if f.is_file():
                return f, "global", ""
            return None, "", f"library {nick!r} (global fp-lib-table, {self.global_libs[nick]}) has no {name}.kicad_mod"
        for d in self.installed:
            f = d / f"{nick}.pretty" / f"{name}.kicad_mod"
            if f.is_file():
                return f, "installed", ""
        return None, "", f"library {nick!r} not in the project fp-lib-table, the installed libraries or the global table, or it lacks {name}.kicad_mod"


# --- board facts (raw S-expression) -----------------------------------------------


def _reference_of(fp: Any) -> str:
    for child in fp.children:
        if child.name == "property" and len(child.values) > 1 and str(child.values[0]) == "Reference":
            return str(child.values[1])
    for child in fp.children:  # KiCad 7 fp_text form
        if child.name == "fp_text" and len(child.values) > 1 and str(child.values[0]) == "reference":
            return str(child.values[1])
    return ""


def _board_refs(root: Any) -> set[str]:
    return {_reference_of(n) for n in root.children if n.name == "footprint"} - {""}


def _outline_bbox(root: Any) -> tuple[float, float, float, float] | None:
    """Bounding box of the raw Edge.Cuts nodes (lines, arcs, rects, circles, polygons)."""
    xs: list[float] = []
    ys: list[float] = []
    for node in root.children:
        if node.name not in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_curve"):
            continue
        layer = node.find("layer")
        if layer is None or not layer.values or str(layer.values[0]) != "Edge.Cuts":
            continue
        pts: list[Any] = []
        if node.name == "gr_poly":
            pts_node = node.find("pts")
            pts = [c for c in (pts_node.children if pts_node is not None else []) if c.name == "xy"]
            coords = [(float(p.values[0]), float(p.values[1])) for p in pts]
        else:
            coords = []
            for key in ("start", "mid", "end", "center", "pts"):
                n = node.find(key)
                if n is not None and len(n.values) >= 2:
                    coords.append((float(n.values[0]), float(n.values[1])))
            if node.name == "gr_circle":
                c, e = node.find("center"), node.find("end")
                if c is not None and e is not None:
                    r = max(abs(float(e.values[0]) - float(c.values[0])), abs(float(e.values[1]) - float(c.values[1])))
                    cx, cy = float(c.values[0]), float(c.values[1])
                    coords += [(cx - r, cy - r), (cx + r, cy + r)]
        for x, y in coords:
            xs.append(x)
            ys.append(y)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


def _footprint_facts(root: Any) -> dict[str, dict]:
    """ref -> {name, at} of every footprint, from the raw nodes."""
    out: dict[str, dict] = {}
    for n in root.children:
        if n.name != "footprint":
            continue
        at = n.find("at")
        out[_reference_of(n)] = {
            "name": str(n.values[0]) if n.values else "",
            "at": [float(v) for v in at.values[:2]] if at is not None and len(at.values) >= 2 else None,
        }
    return out


def _stage(
    refs: Sequence[str],
    outline: tuple[float, float, float, float] | None,
    origin: tuple[float, float] | None,
    columns: int,
    spacing: float,
    occupied: Sequence[Sequence[float]] = (),
) -> tuple[dict[str, list[float]], dict]:
    """Deterministic grid, board-absolute coordinates, natural reference order.

    Default origin: ``(outline max x + 10 mm, outline min y)``, i.e. entirely
    to the right of the board; with no outline, ``(0, 0)``. A declared origin
    is used as given. Row-major, ``columns`` per row, ``spacing`` mm pitch.
    A grid cell is skipped when an existing footprint raw ``(at)`` lies within
    half a pitch of it (so a later import never lands on an earlier one), and,
    for the default origin, when it falls inside the outline.
    """
    declared = origin is not None
    if origin is not None:
        ox, oy = float(origin[0]), float(origin[1])
    elif outline is not None:
        ox, oy = outline[2] + _STAGING_MARGIN, outline[1]
    else:
        ox, oy = 0.0, 0.0
    # kicad_tools serialises 6 significant digits; staged coordinates are kept on a 1 um grid so
    # the position that is planned is the position that survives a save and reload.
    ox, oy = round(ox, 3), round(oy, 3)
    spacing = round(spacing, 3)
    half = spacing / 2.0

    def taken(x: float, y: float) -> bool:
        if any(abs(x - px) < half and abs(y - py) < half for px, py in occupied):
            return True
        return (
            not declared
            and outline is not None
            and outline[0] <= x <= outline[2]
            and outline[1] <= y <= outline[3]
        )

    positions: dict[str, list[float]] = {}
    cell = 0
    skipped = 0
    for ref in sorted(refs, key=_natural_key):
        while True:
            x, y = ox + (cell % columns) * spacing, oy + (cell // columns) * spacing
            cell += 1
            if not taken(x, y):
                break
            skipped += 1
            if cell > 100000:
                raise _Blocked("no free staging cell found")
        positions[ref] = [round(x, 3), round(y, 3)]
    info = {
        "origin": [ox, oy],
        "declared": declared,
        "columns": columns,
        "spacing_mm": spacing,
        "order": "natural reference order, row-major, cells near existing footprints skipped",
        "cells_skipped": skipped,
    }
    return positions, info


# --- plan -------------------------------------------------------------------------


def _placed_symbols(root_sheet: Path, project: str | None = None) -> list[dict]:
    """Every placed (non-power) symbol unit of the sheet hierarchy: ``_schematic_records``
    without the power symbols (``#``)."""
    return [r for r in _schematic_records(walk_sheet_tree(root_sheet), project) if not r["reference"].startswith("#")]


def _schematic_records(tree: Any, project: str | None = None) -> list[dict]:
    """Every placed symbol unit of the sheet tree, power symbols included, read from the raw
    schematics. ``_check_schematic`` walks the tree and reads these once for all its checks.

    kicad-cli silently keeps ONE symbol when two share a reference, so duplicates must be
    found here. The records come from ``sheet_tree.placed_instances`` (build ticket 68): one
    per symbol unit per sheet use reachable from the root, with KiCad's reference for that
    use (a sheet used twice has different references). An instance entry whose path no sheet
    use has (a stale one, left by a deleted or copied sheet) is not counted when the symbol has
    an entry at a reachable path; without one, the record falls back to the symbol's Reference
    property, while kicad-cli may use the stale entry instead. ``_check_schematic`` therefore
    also refuses such a symbol (``_stale_only_entries``) and compares each symbol's reference
    with kicad-cli's netlist (``_netlist_mismatches``). ``unit_count`` is the number of units
    of the symbol's embedded lib symbol (``None`` when the sheet embeds none); ``instance_paths``
    are all the paths of the symbol's ``(instances ...)`` entries, reachable or not;
    ``on_board`` is false for a symbol marked ``(on_board no)``, which kicad-cli's netlist
    leaves out. ``pins`` are the pin numbers of the record's unit in that embedded lib symbol,
    its own and the shared unit 0's (``_symbol_facts``), or ``None`` when no pin of the unit's
    own can be read (no embedded lib symbol, no body for the unit, or a body without pins).
    """
    facts = _symbol_facts(tree)
    out = []
    for rec in placed_instances(tree, project):
        unit_count, instance_paths, on_board, pins_of = facts.get((rec["sheet"], rec["uuid"]), (None, [], True, {}))
        own = pins_of.get(rec["unit"], set())
        out.append(
            {
                "reference": rec["ref"],
                "unit": rec["unit"],
                "lib_id": rec["lib_id"],
                "uuid": rec["uuid"],
                "value": rec["value"],
                "footprint": rec["footprint"],
                "unit_count": unit_count,
                "instance_paths": instance_paths,
                "on_board": on_board,
                "pins": sorted(own | pins_of.get("0", set()), key=_natural_key) if own else None,
                "sheet": rec["sheet"],
                "path": rec["path"],
            }
        )
    return out


_LIB_UNIT = re.compile(r"_(\d+)_(\d+)$")  # a lib symbol's unit body: <name>_<unit>_<body style>


_SymbolFacts = tuple[int | None, list[str], bool, dict[str, set[str]]]


def _symbol_facts(tree: Any) -> dict[tuple[str, str], _SymbolFacts]:
    """``(sheet, symbol uuid) -> (unit count, instance paths, on board, pins by unit)`` of each
    symbol of the tree's sheets, read from the raw S-expression. The unit count is that of the
    symbol's embedded lib symbol, the ``(lib_symbols (symbol <lib_name or lib_id> (symbol
    "<name>_<unit>_<style>" ...)))`` entry of its own sheet (unit 0 holds what all units share;
    one unit when there is no other; ``None`` without the entry). Pins by unit maps each unit
    (``"0"`` for the shared one) to the ``(number ...)`` of the ``(pin ...)`` nodes in that
    entry's bodies of the unit, of every body style (a De Morgan body adds its numbers too, so
    a pin only it has is still demanded); empty without the entry. The instance paths are those of every
    ``(instances (project P (path ...)))`` entry, whatever the project. On board is false for
    ``(on_board no)``, the rule of ``placed_instances``' ``on_board_only``."""
    out: dict[tuple[str, str], _SymbolFacts] = {}
    for sheet in tree.files:
        root = _parse(sheet)
        counts: dict[str, int] = {}
        bodies: dict[str, list[tuple[str, set[str]]]] = {}  # lib -> [(unit, pins)], one per body
        for table in root.find_children("lib_symbols"):
            for lib in table.find_children("symbol"):
                name = lib.get_string(0) or ""
                bodies[name] = [
                    (
                        m.group(1),
                        {
                            number[0].get_string(0) or ""
                            for pin in body.find_children("pin")
                            if (number := pin.find_children("number"))
                        },
                    )
                    for body in lib.find_children("symbol")
                    if (m := _LIB_UNIT.search(body.get_string(0) or ""))
                ]
                counts[name] = max({int(unit) for unit, _ in bodies[name]} - {0}, default=1)
        rel = tree.rel(sheet)
        for symbol in root.find_children("symbol"):
            names = symbol.find_children("lib_name") or symbol.find_children("lib_id")
            uuids = symbol.find_children("uuid")
            if uuids:
                key = (names[0].get_string(0) if names else "") or ""
                paths = [
                    entry.get_string(0) or ""
                    for instances in symbol.find_children("instances")
                    for proj in instances.find_children("project")
                    for entry in proj.find_children("path")
                ]
                on_board = not any(n.get_string(0) == "no" for n in symbol.find_children("on_board"))
                pins_of: dict[str, set[str]] = {}
                for unit, pins in bodies.get(key, []):
                    pins_of.setdefault(unit, set()).update(pins)
                out[(rel, uuids[0].get_string(0) or "")] = (counts.get(key), paths, on_board, pins_of)
    return out


_STALE_ONLY = "instance data does not match the sheet hierarchy; annotate in KiCad"


def _stale_only_entries(records: list[dict]) -> list[str]:
    """Each placed symbol (power symbols too) that has ``(instances ...)`` entries but none at
    the sheet use's path (build ticket 68, fix round 2). Its reference is then not defined by
    this hierarchy: ``placed_instances`` falls back to the Reference property, kicad-cli reads
    the stale or foreign entry, and the two can name different parts while the reference sets
    still agree (sonde xilinx: U1's unit 2 whose only entry says R5 became a second R5, its
    plan shorting R5.1 to VCC). KiCad 6 sheets keep no such entries (``symbol_instances``).
    Paths are compared by ``sheet_tree.path_key`` (build ticket 73), as kicad-cli reads them,
    not as strings; a path ``path_key`` rejects matches no use."""
    out = []
    for r in records:
        if not r["instance_paths"]:
            continue
        use_key = path_key(r["path"])
        if use_key is not None and use_key in {path_key(p) for p in r["instance_paths"]}:
            continue
        out.append(
            f"{r['reference']} uuid {r['uuid']} in {r['sheet']} (sheet use {r['path']}; "
            f"instance paths {', '.join(r['instance_paths'])})"
        )
    return out


def _netlist_mismatches(
    tree: Any, records: list[dict], raw: list[dict], nodes: set[tuple[str, str]]
) -> list[str]:
    """Each placed on-board symbol whose reference is not the one kicad-cli's netlist gives the
    component that lists its uuid (build ticket 68, fix round 2). The netlist gives a component
    one sheet path (``/`` for the root, ``/<sheet>/`` below it) and lists in ``(tstamps ...)``
    the symbol uuids of the part's units on that sheet use only: a unit on another sheet (the
    jetson-agx-thor-baseboard demo's U30 and J1, vme-wren's IC14 over nine sheet uses) is not
    listed anywhere. Such a unit, unlisted while kicad-cli's component of its reference sits on
    another sheet use, is checked through the netlist's net nodes instead (build ticket 83):
    each of its pins (``pins``, unconnected ones included) must appear as a node (reference,
    pin). This proves that the netlist has a node for each of the unit's pin numbers under its
    reference, not which placed symbol gave it. No readable pin of the unit's own, or a node
    missing, refuses. Compared symbol by symbol, not as reference sets: a stale entry can
    rename one part to another existing one and leave both sets equal."""
    named: dict[tuple[str, str], str] = {}
    sheets_of: dict[str, set[str]] = {}
    for comp in raw:
        sheet = comp["sheet_tstamps"] or "/"
        sheet = sheet if sheet.endswith("/") else sheet + "/"
        sheets_of.setdefault(comp["reference"], set()).add(sheet)
        for uuid in comp["uuids"]:
            named[(sheet, uuid)] = comp["reference"]
    root_path = tree.uses[0][1] if tree.uses else ""
    problems = []
    for rec in records:
        if rec["reference"].startswith("#") or not rec["on_board"]:
            continue
        ref = rec["reference"]
        sheet = rec["path"][len(root_path) :] + "/"
        listed = named.get((sheet, rec["uuid"]))
        if listed is None and sheets_of.get(ref, set()) - {sheet}:
            # a unit on another sheet use than kicad-cli's component of its part
            where = (
                f"{ref} unit {rec['unit']} uuid {rec['uuid']} in {rec['sheet']}: "
                f"a unit on another sheet than kicad-cli's {ref}, and "
            )
            if rec["pins"] is None:
                problems.append(where + f"no pin of unit {rec['unit']} can be read from the sheet's embedded lib symbol")
                continue
            missing = [pin for pin in rec["pins"] if (ref, pin) not in nodes]
            if missing:
                problems.append(where + f"kicad-cli's netlist has no node {ref} pin {', '.join(missing)}")
            continue
        if listed != ref:
            said = "does not list it" if listed is None else f"names it {listed}"
            problems.append(f"{ref} uuid {rec['uuid']} in {rec['sheet']}: kicad-cli's netlist {said}")
    return problems


def _duplicate_problems(records: list[dict]) -> list[str]:
    """A reference that names more than one part, with the rule that found it. The unit rule
    (build ticket 68): the units of one multi-unit symbol share a reference and are one part,
    whatever their lib ids (sonde xilinx's U1 units 1-4 carry four rescued lib ids). Two parts
    are: the same unit twice under one reference, or units whose Value, Footprint or embedded
    lib symbol's unit count differ (KiCad keeps those equal across the units of one part; a
    resistor annotated as a free unit of a 74LS125 repeats no unit, and kicad-cli's netlist
    then keeps the resistor and drops the gate package without an error). A unit count that
    cannot be read (no embedded lib symbol) fails closed: "unit count unreadable" (fix round 2).
    Power symbols (``#``) are not parts and are skipped."""
    by_ref: dict[str, list[dict]] = {}
    for rec in records:
        if not rec["reference"].startswith("#"):
            by_ref.setdefault(rec["reference"], []).append(rec)
    problems = []
    for ref in sorted(by_ref, key=_natural_key):
        recs = by_ref[ref]
        units = Counter(r["unit"] for r in recs)
        rules = [f"unit {u} placed {n} times" for u, n in sorted(units.items()) if n > 1]
        if len({r["value"] for r in recs}) > 1:
            rules.append("different values")
        if len({r["footprint"] for r in recs}) > 1:
            rules.append("different footprints")
        if len({r["unit_count"] for r in recs if r["unit_count"] is not None}) > 1:
            rules.append("different unit counts")
        if len(recs) > 1 and any(r["unit_count"] is None for r in recs):
            rules.append("unit count unreadable")
        if rules:
            where = " and ".join(
                f"{r['reference']} unit {r['unit']} value {r['value']!r} footprint {r['footprint']!r} "
                f"{r['lib_id']} ({_units_text(r['unit_count'])}) in {r['sheet']} uuid {r['uuid']}"
                for r in recs
            )
            problems.append(f"{ref} ({', '.join(rules)}): {where}")
    return problems


def _units_text(count: int | None) -> str:
    if count is None:
        return "unit count unknown"
    return "1 unit" if count == 1 else f"{count} units"


def _check_schematic(sch_path: Path, netlist: Any, raw: list[dict], nodes: set[tuple[str, str]]) -> None:
    """Fail closed (raise ``_Blocked``) on anything that would make a silently partial or
    ambiguous import: unannotated or duplicate references, missing sub-sheets, empty netlist."""
    refs = [c.reference for c in netlist.components]
    unannotated = sorted(r for r in refs if "?" in r)
    problems = list(gate._annotation_problems(sch_path))
    if unannotated:
        problems.insert(0, f"unannotated reference(s): {', '.join(unannotated)}")
    if problems:
        raise _Blocked("schematic is not provably complete and annotated: " + "; ".join(problems))
    if not refs:
        raise _Blocked("the schematic exports no components: refusing an empty import")
    tree = walk_sheet_tree(sch_path)
    records = _schematic_records(tree)  # read once: every check below works on these
    stale_only = _stale_only_entries(records)
    if stale_only:
        raise _Blocked(f"{_STALE_ONLY}: " + "; ".join(stale_only))
    dup_symbols = _duplicate_problems(records)
    if dup_symbols:
        raise _Blocked(
            "duplicate reference designators (a reference must name one symbol): " + "; ".join(dup_symbols)
        )
    counts = Counter(refs)
    dups = sorted((r for r, n in counts.items() if n > 1), key=_natural_key)
    if dups:
        parts = []
        for ref in dups:
            where = [
                f"{c['reference']} value {c['value']!r} sheet {c['sheet_names'] or '/'} uuid {c['tstamps']}"
                for c in raw
                if c["reference"] == ref
            ]
            parts.append(f"{ref}: " + " and ".join(where or [f"{counts[ref]} symbols"]))
        raise _Blocked("duplicate reference designators (a reference must name one symbol): " + "; ".join(parts))
    # The scan above reads each symbol's reference as placed_instances does. kicad-cli can read
    # another one (a symbol whose only instance entry is stale or another project's: its netlist
    # takes that entry's reference and silently drops a part). Not modelled; any disagreement
    # between the sheets' placed references and the netlist's refuses, by name (build ticket 68).
    disagreement = netlist_disagreement(tree, refs)
    if disagreement:
        raise _Blocked("the sheets and kicad-cli's netlist disagree on the placed parts: " + "; ".join(disagreement))
    if not raw:  # an exporter that wrote no netlist file: nothing to compare symbol by symbol
        raise _Blocked("kicad-cli's netlist file could not be read: the parts cannot be matched symbol by symbol")
    mismatches = _netlist_mismatches(tree, records, raw, nodes)
    if mismatches:
        raise _Blocked("the sheets and kicad-cli's netlist name a symbol differently: " + "; ".join(mismatches))


def _net_table(root: Any) -> dict[int, str]:
    out: dict[int, str] = {}
    for n in root.children:
        if n.name == "net" and len(n.values) > 1:
            try:
                out[int(n.values[0])] = str(n.values[1])
            except (TypeError, ValueError):
                continue
    return out


def _copper_on_nets(root: Any, names: set[str]) -> list[str]:
    """``kind uuid (net)`` of every top-level segment/arc/via/zone on one of ``names``, from raw nodes."""
    table = _net_table(root)
    hits: list[str] = []
    for n in root.children:
        if n.name not in ("segment", "arc", "via", "zone"):
            continue
        net = n.find("net")
        name = ""
        if net is not None and net.values:
            try:
                name = table.get(int(net.values[0]), "")
            except (TypeError, ValueError):
                name = str(net.values[0])
        if not name:
            nn = n.find("net_name")
            name = str(nn.values[0]) if nn is not None and nn.values else ""
        if name in names:
            uuid = n.find("uuid")
            hits.append(f"{n.name} {uuid.values[0] if uuid is not None and uuid.values else '(no uuid)'} ({name})")
    return hits


def _check_net_reconciliation(
    root: Any,
    nets: dict[str, list[str]],
    pin_net: dict[tuple[str, str], str],
    pads: dict[tuple[str, str], set[str]],
    present: set[str],
    components: dict[str, Any],
    renetted: list[dict],
) -> None:
    """Raise ``_Blocked`` for the two changes this version cannot apply consistently."""
    stale = sorted(n for n in set(_net_table(root).values()) - {""} if n not in nets)
    if stale:
        moves = []
        for old in stale:
            targets = sorted(
                {
                    pin_net[(ref, pad)]
                    for (ref, pad), board_nets in pads.items()
                    if old in board_nets and ref in present and (ref, pad) in pin_net
                }
            )
            moves.append(f"{old} -> {', '.join(targets) if targets else '(removed)'}")
        raise _Blocked(
            "net renamed or removed in the schematic: reconcile the net table (not supported in this version): "
            + "; ".join(moves)
        )
    if renetted:
        affected = {r["board_net"] for r in renetted} | {r["schematic_net"] for r in renetted}
        copper = _copper_on_nets(root, affected)
        if copper:
            pads_txt = ", ".join(f"{r['reference']}.{r['pad']} {r['board_net']} -> {r['schematic_net']}" for r in renetted)
            raise _Blocked(
                f"renetting existing pads ({pads_txt}) would leave copper on the old net: "
                + "; ".join(copper)
                + ". Remove that copper or reconcile the board first (not supported in this version)"
            )


def _rel(path: Path, base: Path) -> str:
    try:
        return Path(path).resolve().relative_to(base).as_posix()
    except ValueError:
        return str(path)


def _plan(
    pcb_path: Path,
    sch_path: Path,
    staging_origin: tuple[float, float] | None,
    columns: int,
    spacing: float,
    footprint_dirs: Sequence[str | Path] | None,
    netlist_exporter: NetlistExporter | None,
) -> dict:
    netlist, raw, nodes = _load_netlist(sch_path, netlist_exporter)
    _check_schematic(sch_path, netlist, raw, nodes)
    root = _parse(pcb_path)
    facts = _footprint_facts(root)
    present = set(facts) - {""}
    resolver = _Resolver(pcb_path.parent, footprint_dirs)

    components = {c.reference: c for c in netlist.components}
    meta = {c["reference"]: c for c in raw}
    new_refs = [r for r in components if r not in present]
    occupied = [f["at"] for f in facts.values() if f["at"] is not None]
    positions, staging = _stage(new_refs, _outline_bbox(root), staging_origin, columns, spacing, occupied)

    footprints: list[dict] = []
    unresolved: list[dict] = []
    for ref in sorted(new_refs, key=_natural_key):
        comp = components[ref]
        f, source, reason = resolver.resolve(comp.footprint)
        if f is None:
            unresolved.append({"reference": ref, "library_id": comp.footprint, "reason": reason})
            continue
        m = meta.get(ref)
        spec = {
            "reference": ref,
            "value": comp.value,
            "library_id": comp.footprint,
            "source": source,
            "file": str(f),
            "file_sha256": _sha256(f),
            "staging_at": positions[ref],
            "dnp": bool(m and m["dnp"]),
        }
        if m and m["tstamps"]:
            # KiCad matches PCB footprints to symbols by this path: sheet-path uuids + symbol uuid.
            sheet = m["sheet_tstamps"] or "/"
            spec["path"] = (sheet if sheet.endswith("/") else sheet + "/") + m["tstamps"]
            spec["sheetname"] = m["sheetname"]
            spec["sheetfile"] = m["sheetfile"]
        footprints.append(spec)

    nets = {n.name: sorted(f"{x.reference}.{x.pin}" for x in n.nodes) for n in netlist.nets if n.name}
    pin_net = {(x.reference, str(x.pin)): n.name for n in netlist.nets if n.name for x in n.nodes}
    pads = _pad_nets(root)
    mismatches = [
        {"reference": r, "board": facts[r]["name"], "schematic": components[r].footprint}
        for r in sorted(present & set(components), key=_natural_key)
        if components[r].footprint and facts[r]["name"] != components[r].footprint
    ]
    orphans = sorted(present - set(components), key=_natural_key)
    renetted = []
    for (ref, pad), board_nets in sorted(pads.items(), key=lambda kv: (_natural_key(kv[0][0]), kv[0][1])):
        if ref not in components or ref not in present:
            continue
        want = pin_net.get((ref, pad))
        if want is not None and board_nets != {want}:
            renetted.append({"reference": ref, "pad": pad, "board_net": sorted(board_nets)[0], "schematic_net": want})
    _check_net_reconciliation(root, nets, pin_net, pads, present, components, renetted)
    digest = hashlib.sha256(
        json.dumps(
            {
                "netlist": _netlist_digest(netlist),
                "footprints": [
                    (f["reference"], f["library_id"], f["file_sha256"], f["staging_at"], f.get("path"))
                    for f in footprints
                ],
                "unresolved": [(u["reference"], u["library_id"]) for u in unresolved],
                "project_libs": sorted((k, _rel(v, resolver.project_dir)) for k, v in resolver.project.items()),
                "global_libs": sorted((k, str(v)) for k, v in resolver.global_libs.items()),
            },
            sort_keys=True,
        ).encode()
    ).hexdigest()
    return {
        "netlist": netlist,
        "footprints": footprints,
        "unresolved": unresolved,
        "already_present": sorted((r for r in components if r in present), key=_natural_key),
        "footprint_mismatches": mismatches,
        "orphans": orphans,
        "renetted": renetted,
        "staging": staging,
        "nets": nets,
        "plan_hash": digest,
    }


def _unresolved_reason(unresolved: list[dict]) -> str:
    return "unresolved footprint(s), nothing written: " + "; ".join(
        f"{u['reference']} ({u['library_id'] or 'no footprint'}): {u['reason']}" for u in unresolved
    )


def _default_output_dir(pcb_path: Path) -> Path:
    resolved = pcb_path.resolve()
    return resolved.parent.parent / f"{resolved.parent.name}_imported"


def _output_dir_problem(src: Path, out_dir: Path) -> str | None:
    return checkpoint.output_dir_problem(src, out_dir, what="the import")


def propose_schematic_import(
    pcb_path: str | Path,
    schematic_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    *,
    staging_origin: tuple[float, float] | None = None,
    columns: int = _DEFAULT_COLUMNS,
    spacing: float = _DEFAULT_SPACING,
    footprint_dirs: Sequence[str | Path] | None = None,
    netlist_exporter: NetlistExporter | None = None,
) -> dict:
    """Dry run. Exports the kicad-cli netlist (to a temp file), resolves every
    footprint, plans the staging grid and the nets. Writes nothing. With any
    unresolved footprint the status is ``blocked`` and no token is issued."""
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    sch = Path(schematic_path) if schematic_path else src.with_suffix(".kicad_sch")
    if not sch.is_file():
        return {"status": "incomplete", "reason": f"schematic not found: {sch}"}
    if columns < 1 or spacing <= 0:
        return {"status": "error", "reason": "columns must be >= 1 and spacing > 0"}
    out_dir = Path(output_dir) if output_dir else _default_output_dir(src)
    problem = _output_dir_problem(src, out_dir)
    if problem:
        return {"status": "error", "reason": problem}
    try:
        plan = _plan(src, sch, staging_origin, columns, spacing, footprint_dirs, netlist_exporter)
    except _Blocked as exc:
        return {"status": "blocked", "revision_token": None, "reason": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"could not plan the import: {exc}"}

    base = {
        "footprints": plan["footprints"],
        "unresolved": plan["unresolved"],
        "already_present": plan["already_present"],
        "footprint_mismatches": plan["footprint_mismatches"],
        "orphans": plan["orphans"],
        "renetted": plan["renetted"],
        "staging": plan["staging"],
        "nets": plan["nets"],
        "net_count": len(plan["nets"]),
        # never read: kicad-cli and the walks start at the root (build ticket 26)
        "unreferenced_sheets": list(walk_sheet_tree(sch).unreferenced),
    }
    if plan["unresolved"]:
        return {"status": "blocked", "revision_token": None, "reason": _unresolved_reason(plan["unresolved"]), **base}

    payload = {
        "sch": _norm(sch),
        "out_dir": str(out_dir),
        "plan_hash": plan["plan_hash"],
        "staging_origin": staging_origin,
        "columns": columns,
        "spacing": spacing,
    }
    try:  # the schematic may lie outside the board's directory tree: hashed on its own too
        token = _PENDING.issue(src, payload, extra_files=(sch,))
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        "revision_token": token,
        "output_pcb": str(out_dir / src.name),
        "note": (
            "creates a new board revision; the original is untouched. Footprints are staged on a grid "
            "(not placed). Call confirm_schematic_import with this revision_token to apply."
        ),
        **base,
    }


# --- confirm ------------------------------------------------------------------------


def _set_quoted_child(fp: Any, name: str, value: str) -> None:
    from kicad_tools.sexp import SExp

    old = fp.find(name)
    if old is not None:
        fp.remove(old)
    fp.append(SExp.list(name, SExp.quoted_atom(value)))


def _apply_symbol_link(fp: Any, spec: dict) -> None:
    """Tie a new footprint to its symbol the way KiCad does (``path`` = sheet uuids + symbol uuid,
    plus sheetname/sheetfile), and carry the schematic DNP flag into ``(attr ... dnp)``."""
    from kicad_tools.sexp import SExp

    if spec.get("path"):
        _set_quoted_child(fp, "path", spec["path"])
        _set_quoted_child(fp, "sheetname", spec.get("sheetname", ""))
        _set_quoted_child(fp, "sheetfile", spec.get("sheetfile", ""))
    if spec.get("dnp"):
        attr = fp.find("attr")
        if attr is None:
            fp.append(SExp.list("attr", "dnp"))
        elif "dnp" not in [str(v) for v in attr.values]:
            attr.add("dnp")


def _tag_counts(root: Any) -> Counter:
    return Counter(c.name for c in root.children if c.name)


def _pad_nets(root: Any) -> dict[tuple[str, str], set[str]]:
    out: dict[tuple[str, str], set[str]] = {}
    for fp in root.children:
        if fp.name != "footprint":
            continue
        ref = _reference_of(fp)
        for pad in fp.children:
            if pad.name != "pad" or not pad.values:
                continue
            net = pad.find("net")
            name = str(net.values[1]) if net is not None and len(net.values) > 1 else ""
            out.setdefault((ref, str(pad.values[0])), set()).add(name)
    return out


def _verify(
    fresh: Any, before: Counter, present: set[str], plan: dict, before_facts: dict
) -> tuple[str, str, Any, Any] | None:
    """``(reason, item, expected, actual)`` for the first way the reloaded revision does not
    match the plan, else None."""
    netlist = plan["netlist"]
    planned = {f["reference"]: f for f in plan["footprints"]}
    refs = _board_refs(fresh)
    if refs != present | set(planned):
        extra = sorted(refs - present - set(planned))
        missing = sorted((present | set(planned)) - refs)
        return (
            f"footprint set after reload differs from the plan: extra {extra}, missing {missing}",
            "footprint set", sorted(present | set(planned)), sorted(refs),
        )
    for fp in fresh.children:
        if fp.name != "footprint":
            continue
        ref = _reference_of(fp)
        spec = planned.get(ref)
        if spec is None:
            continue
        if str(fp.values[0]) != spec["library_id"]:
            return (
                f"{ref}: footprint is {fp.values[0]!r}, expected {spec['library_id']!r}",
                f"footprint:{ref} library id", spec["library_id"], str(fp.values[0]),
            )
        for key in ("path", "sheetname", "sheetfile"):
            if spec.get("path"):
                node = fp.find(key)
                got_v = str(node.values[0]) if node is not None and node.values else None
                if got_v != spec[key if key == "path" else key]:
                    return (
                        f"{ref}: raw ({key} ...) is {got_v!r}, expected {spec[key]!r}",
                        f"footprint:{ref} ({key} ...)", spec[key], got_v,
                    )
        if spec.get("dnp"):
            attr = fp.find("attr")
            if attr is None or "dnp" not in [str(v) for v in attr.values]:
                return (
                    f"{ref}: schematic marks it DNP but the footprint has no dnp attribute",
                    f"footprint:{ref} (attr ...)", "dnp", [str(v) for v in attr.values] if attr is not None else None,
                )
        at = fp.find("at")
        got = [float(v) for v in at.values[:2]] if at is not None else None
        if got is None or any(
            abs(got[i] - spec["staging_at"][i]) > 0.005 + 1e-5 * abs(spec["staging_at"][i]) for i in (0, 1)
        ):
            return (
                f"{ref}: raw (at ...) is {got}, planned {spec['staging_at']}",
                f"footprint:{ref} (at ...)", spec["staging_at"], got,
            )
    after_facts = _footprint_facts(fresh)
    for ref, was in before_facts.items():  # re-import only adds: existing lib id and position stay
        now = after_facts.get(ref)
        if now != was:
            return f"{ref}: existing footprint changed (was {was}, now {now})", f"footprint:{ref}", was, now
    after = _tag_counts(fresh)
    for tag in set(before) | set(after):
        if tag == "net":
            if after[tag] < before[tag]:
                return "net table shrank during import", "top-level net count", f"at least {before[tag]}", after[tag]
            continue
        want = before[tag] + (len(planned) if tag == "footprint" else 0)
        if after[tag] != want:
            return f"top-level {tag!r} count is {after[tag]}, expected {want}", f"top-level {tag} count", want, after[tag]
    pads = _pad_nets(fresh)
    wanted: dict[tuple[str, str], str] = {}
    for net in netlist.nets:
        if not net.name:
            continue
        for node in net.nodes:
            wanted[(node.reference, node.pin)] = net.name
    for key, name in sorted(wanted.items()):
        if key not in pads:
            return f"{key[0]}.{key[1]}: pad does not exist on the board", f"pad {key[0]}.{key[1]}", name, None
        if pads[key] != {name}:
            return (
                f"{key[0]}.{key[1]}: net is {sorted(pads[key])}, kicad-cli netlist says {name!r}",
                f"pad {key[0]}.{key[1]} net", name, sorted(pads[key]),
            )
    for key in sorted(k for k in pads if k[0] in planned and k not in wanted):
        if pads[key] != {""}:
            return (
                f"{key[0]}.{key[1]}: carries net {sorted(pads[key])} but is on no net in the netlist",
                f"pad {key[0]}.{key[1]} net", "", sorted(pads[key]),
            )
    board_nets = {str(n.values[1]) for n in fresh.children if n.name == "net" and len(n.values) > 1} - {""}
    netlist_nets = {n.name for n in netlist.nets if n.name}
    if board_nets != netlist_nets:
        return (
            "net names on the board differ from the kicad-cli netlist",
            "net names", sorted(netlist_nets), sorted(board_nets),
        )
    return None


def _build_revision(
    src: Path,
    sch: Path,
    entry: dict,
    out_dir: Path,
    footprint_dirs: Sequence[str | Path] | None,
    netlist_exporter: NetlistExporter | None,
) -> dict:
    """Build, verify and move the revision into ``out_dir`` (the checkpoint helper's temporary
    sibling, reload and compare, then the move). Every failure leaves nothing behind."""
    return checkpoint.build_revision(
        out_dir,
        write=lambda tmp: _build_inner(src, sch, entry, tmp, footprint_dirs, netlist_exporter),
        verify=_verify_revision,
        success="built",
        before_verified="import failed before a revision was verified",
        note=_KEPT,
    )


def _build_inner(
    src: Path,
    sch: Path,
    entry: dict,
    tmp: Path,
    footprint_dirs: Sequence[str | Path] | None,
    netlist_exporter: NetlistExporter | None,
) -> dict:
    """The write step: copy the project into ``tmp``, re-plan against the copy, add the
    footprints and nets, save through the write layer. Returns what the verify step needs."""
    from kicad_tools.schema import PCB

    tmp_pcb = checkpoint.copy_project(
        src, tmp, outputs=(f"{src.parent.name}_imported", f"{src.parent.name}_prepared"),
        skip_suffixes=(".kicad_net",), max_bytes=_MAX_COPY_BYTES,
    )
    tmp_sch = tmp / sch.name
    # Plan against the copy so netlist, library files and board are one consistent snapshot.
    plan = _plan(
        tmp_pcb, tmp_sch, entry["staging_origin"], entry["columns"], entry["spacing"],
        footprint_dirs, netlist_exporter,
    )
    if plan["unresolved"]:
        raise checkpoint.Failed("error", _unresolved_reason(plan["unresolved"]), unresolved=plan["unresolved"])
    if plan["plan_hash"] != entry["plan_hash"]:
        raise checkpoint.Failed(
            "error",
            "the footprint libraries, netlist or staging changed since the proposal was made: "
            "re-run propose_schematic_import",
            note="nothing was written; the proposal is stale and its revision_token is spent",
            stale=True,
        )
    board = PCB.load(tmp_pcb)
    before = _tag_counts(board._sexp)
    before_facts = _footprint_facts(board._sexp)
    present = _board_refs(board._sexp)
    ox, oy = board._board_origin
    for spec in plan["footprints"]:
        x, y = spec["staging_at"]
        # add_footprint_from_file takes board-relative coordinates; the raw (at) is verified below.
        board.add_footprint_from_file(spec["file"], spec["reference"], x - ox, y - oy, value=spec["value"])
        node = board.get_footprint(spec["reference"])._sexp_node  # the node the API just appended
        if node is None or node.name not in ("footprint", "module") or _reference_of(node) != spec["reference"]:
            raise RuntimeError(f"unexpected tree after adding {spec['reference']}")
        # EasyEDA-converted libraries still use the legacy "module" token, which
        # kicad_tools' net writer (find_all("footprint")) would skip.
        node.name = "footprint"
        _apply_symbol_link(node, spec)
        node.set_value(0, spec["library_id"])  # nickname as KiCad's update-from-schematic writes it
    stats = board.assign_nets_from_netlist(plan["netlist"])
    if stats["missing_pads"] or stats["missing_footprints"]:
        raise checkpoint.Failed(
            "error",
            "netlist pins with no matching footprint pad: "
            f"pads {stats['missing_pads']}, footprints {stats['missing_footprints']}",
        )
    write_layer.save_pcb(board, tmp_pcb)
    return {"pcb": tmp_pcb, "plan": plan, "before": before, "present": present, "before_facts": before_facts}


def _verify_revision(tmp: Path, state: dict) -> dict:
    """Verify from a fresh parse of the saved file, never the in-memory tree."""
    tmp_pcb: Path = state["pcb"]
    try:
        fresh = _parse(tmp_pcb)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: imported file failed to re-parse: {exc}",
            [checkpoint.mismatch(tmp_pcb.name, "board", "a board that parses", str(exc))],
        ) from exc
    problem = _verify(fresh, state["before"], state["present"], state["plan"], state["before_facts"])
    if problem:
        reason, item, expected, actual = problem
        raise checkpoint.NotVerified(
            f"CRITICAL: {reason}", [checkpoint.mismatch(tmp_pcb.name, item, expected, actual)]
        )
    return {"plan": state["plan"]}


def confirm_schematic_import(
    pcb_path: str | Path,
    revision_token: str,
    *,
    footprint_dirs: Sequence[str | Path] | None = None,
    netlist_exporter: NetlistExporter | None = None,
) -> dict:
    """Apply a proposal: build the revision, reload it fresh, verify footprints,
    raw positions and every pad net against the kicad-cli netlist, then rename
    it into place. Any unresolved footprint fails the whole confirm and nothing
    is written. The token is consumed only by success, by detected source
    drift or by a stale plan (the re-plan's hash differs); every other failure leaves it usable."""
    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    sch = Path(entry["sch"])
    out_dir = Path(entry["out_dir"])
    problem = _output_dir_problem(src, out_dir)
    if problem:
        return {"status": "error", "reason": problem}

    built = _build_revision(src, sch, entry, out_dir, footprint_dirs, netlist_exporter)
    if built["status"] != "built":
        if built.get("stale"):  # the proposal can no longer match: spend it, re-propose
            _PENDING.spend(revision_token)
        return built
    _PENDING.spend(revision_token)
    plan = built["plan"]
    return {
        "status": "imported",
        "output_pcb": str(out_dir / src.name),
        "footprints_added": [
            {k: f[k] for k in ("reference", "library_id", "source", "staging_at")} for f in plan["footprints"]
        ],
        "already_present": plan["already_present"],
        "footprint_mismatches": plan["footprint_mismatches"],
        "orphans": plan["orphans"],
        "renetted": plan["renetted"],
        "staging": plan["staging"],
        "nets": plan["nets"],
        "net_count": len(plan["nets"]),
        "verified": "reloaded from disk: footprint set, raw positions, every pad net equals the kicad-cli netlist",
        "note": (
            "footprints are staged, not placed; placement, rules and outline are separate steps. "
            "This is not a routing or done claim: only verify_board produces one."
        ),
    }
