"""Pour step: planes first, through a candidate the acceptance gate has graded.

Contract: build ticket 4 and
docs/ARCHITECTURE.md decision 3 (amended). For a board that should carry a pour, the
router's plane script (``route_planes.py``) runs on a scratch copy. Its output
is a *candidate*: it is graded by ``verify_board`` and compared with the grade of
the untouched original, and only when it is accepted are the nodes the router
ADDED (zones, vias, tracks) carried onto a NEW revision of the original through
``kicad_tools``. The router's writer never becomes the board of record: the
revision is the original plus carried nodes, and it is graded, refilled and
checked again before it is moved into place.

WHAT A "poured" RESULT PROVES, AND WHAT IT DOES NOT. ``poured`` means: the plane
script's candidate was graded by ``verify_board`` and compared with the original's
grade; every carried node names a board net; the zone is filled (measured in a copy
refilled by kicad-cli); and no pad was found isolated by the checks listed below. Each
plane-net pad carries ``connectivity: proven | unproven`` with its reason, so no reader
has to infer it. It does NOT prove the board is connected, routed or finished: pads
that wait for a via (placed by the routing step) are ``unproven``, signals are still
open, and the acceptance authority for connectivity remains ``verify_board`` on the
finished board.

What "accepted" means (the pour step's own rule; the verdict itself stays the
gate's):

* the router changed nothing of the original (every existing node is compared by
  content, not by count) and every carried node names a net of the board, with
  id and name agreeing with the net table where one exists;
* the candidate's and the revision's grades are complete, and nothing the pour
  introduced is new (unconnected items off the plane nets, error-level
  violations) relative to the original's own grade;
* no pad the pour must reach is isolated. KiCad reports an unconnected item as a
  link between two clusters and lists NOTHING for a net with one remaining pad
  and a zone that does not touch it, so the unconnected list alone proves
  nothing. Isolation is therefore also checked geometrically against the zone
  KiCad actually filled: every plane-net pad that has copper on the pour layer
  must have filled copper within its reach, and every plane-net pad that does
  not (it waits for a via from the routing step) must lie inside the zone
  outline;
* the zone is really filled: the refilled copy (``kicad-cli --refill-zones
  --save-board``) shows fill copper covering at least ``MIN_FILL_FRACTION`` of
  the outline.

Every zone and pad fact is read from raw S-expression nodes. ``Zone.polygon``
and ``Pad.position`` were measured to carry the page-versus-board origin
(CLAUDE.md hard rule 2); they are never read here. Pad world positions are
computed from the raw footprint ``(at x y rot)`` and pad ``(at x y)`` and
cross-checked against the positions ``kicad-cli`` reports.

Flow (dry-run plus revision-token checkpoint, docs/ARCHITECTURE.md decision 4):
``propose_pour_step`` writes nothing and returns a token; ``confirm_pour_step``
does the work. Nothing is left behind by a rejected or failed attempt (also on
KeyboardInterrupt), and the original project directory gains nothing and loses
nothing (hashes are re-checked at the end).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import secrets
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from kicad_mcp import _self_check, input_prep, kicad_cli, write_layer
from kicad_mcp import verify_board as gate
from kicad_mcp.routers import kicad_routing_tools as krt

PLANE_SCRIPT = "route_planes.py"
PROOF_STATEMENT = (
    "poured means: the candidate was graded by verify_board, the zone is filled (measured in a "
    "kicad-cli refilled copy) and no pad was found isolated by the checks in this result. It does "
    "not mean the board is connected or complete: pads with connectivity 'unproven' (for example "
    "those waiting for a via) and every open signal remain, and the acceptance authority for "
    "connectivity is verify_board on the board after routing."
)
# Top-level node kinds a plane run may add and that this step may carry over.
_CARRIED_TAGS = ("zone", "via", "segment", "arc")
# Header nodes that carry no board content.
_META_TAGS = {"version", "generator", "generator_version"}
_DEFAULT_TIMEOUT_S = 300
# Filled copper must cover at least this share of the zone outline. Thermal reliefs,
# clearances to other nets and board-edge insets take a few percent on a healthy pour;
# a pour below a quarter of its outline is a sliver, not a plane.
MIN_FILL_FRACTION = 0.25
_POSITION_TOLERANCE_MM = 0.05
# Upper bound on kicad-cli runs the connectivity check may spend per confirm (control run,
# position cross-check, and the group/bisect runs). Over the cap a pad that is not proven
# connected is never accepted: with a pad already found isolated the result is a rejection,
# otherwise it is incomplete.
MAX_CONNECTIVITY_RUNS = 40
_ANCHOR_VIA = "(size 0.6) (drill 0.3) (layers \"F.Cu\" \"B.Cu\")"

_PENDING: dict[str, dict[str, Any]] = {}
_PENDING_TTL_SECONDS = 60 * 60
_PENDING_MAX_ENTRIES = 32

_clock = time.time  # replaced in tests


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --- raw reads (never a kicad_tools convenience property) ---------------------------


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _net_table(root: Any) -> dict[int, str]:
    """The top-level ``(net N "name")`` table. KiCad 10 files have none."""
    table: dict[int, str] = {}
    for node in root.children:
        if node.name == "net" and len(node.values) >= 2 and _is_number(node.values[0]):
            table[int(node.values[0])] = str(node.values[1])
    return table


def _net_of(node: Any) -> dict:
    """``{"id", "name"}`` of a pad, zone, segment or via, in either file format:
    ``(net 3 "GND")`` / ``(net 3)`` (KiCad <= 9) and ``(net "GND")`` (KiCad 10).
    A zone's name may come from a sibling ``(net_name "GND")``."""
    net_id: int | None = None
    name: str | None = None
    net = node.find("net")
    if net is not None and net.values:
        first = net.values[0]
        if _is_number(first):
            net_id = int(first)
            if len(net.values) > 1:
                name = str(net.values[1])
        else:
            name = str(first)
    if name is None:
        net_name = node.find("net_name")
        if net_name is not None and net_name.values:
            name = str(net_name.values[0])
    return {"id": net_id, "name": name}


def _resolved_net(node: Any, table: dict[int, str]) -> str | None:
    info = _net_of(node)
    if info["name"] is not None:
        return info["name"]
    if info["id"] is not None:
        return table.get(info["id"], "" if info["id"] == 0 else None)
    return None


def board_net_names(pcb_path: str | Path) -> set[str]:
    """Every real net name of a board, in either file format: the top-level net
    table plus the names carried by pads, zones, tracks and vias."""
    root = input_prep._parse(pcb_path)
    return _net_names(root)


def _net_names(root: Any) -> set[str]:
    table = _net_table(root)
    names = set(table.values())
    for child in root.children:
        if child.name == "footprint":
            for pad in child.find_all("pad"):
                names.add(_resolved_net(pad, table) or "")
        elif child.name in _CARRIED_TAGS:
            names.add(_resolved_net(child, table) or "")
    return {n for n in names if n}


def _zone_info(node: Any, table: dict[int, str] | None = None) -> dict | None:
    """Facts of one board-level ``(zone ...)`` node, or None for a keepout rule area."""
    if node.find("keepout") is not None:
        return None
    table = table or {}
    layers: list[str] = []
    layers_node = node.find("layers")
    layer_node = node.find("layer")
    if layers_node is not None:
        layers = [str(v) for v in layers_node.values]
    elif layer_node is not None and layer_node.values:
        layers = [str(layer_node.values[0])]
    polygon: list[tuple[float, float]] = []
    poly = node.find("polygon")
    pts = poly.find("pts") if poly is not None else None
    if pts is not None:
        for xy in pts.find_all("xy"):
            polygon.append((float(xy.values[0]), float(xy.values[1])))
    uuid = node.find("uuid")
    return {
        "net": _resolved_net(node, table) or "",
        "layer": layers[0] if layers else "",
        "layers": layers,
        "uuid": str(uuid.values[0]) if uuid is not None and uuid.values else "",
        "polygon": polygon,
        "filled_in_file": node.find("filled_polygon") is not None,
    }


def read_zone_polygons(pcb_path: str | Path) -> list[dict]:
    """Every board-level copper zone with its raw polygon, from the file's own
    ``(xy ...)`` nodes: ``[{net, layer, layers, uuid, polygon, filled_in_file}]``.

    ``polygon`` is in the file's (page-absolute) frame, exactly as written;
    ``filled_in_file`` says whether the file itself carries fill copper (the
    gate refills zones in its graded copy either way)."""
    root = input_prep._parse(pcb_path)
    table = _net_table(root)
    zones = [_zone_info(node, table) for node in root.children if node.name == "zone"]
    return [zone for zone in zones if zone is not None]


def _footprint_reference(fp: Any) -> str:
    ref = ""
    for prop in fp.find_all("property"):
        if prop.values and str(prop.values[0]) == "Reference" and len(prop.values) > 1:
            ref = str(prop.values[1])
    if not ref:
        for text in fp.find_all("fp_text"):
            if text.values and str(text.values[0]) == "reference" and len(text.values) > 1:
                ref = str(text.values[1])
    return ref


def _pad_records(root: Any, copper: list[str]) -> list[dict]:
    """Every footprint pad of the board from raw nodes: reference, number, net,
    copper layers, size and WORLD position (footprint ``(at x y rot)`` applied to the
    pad's local ``(at x y)`` with KiCad's rotation convention). One record per pad,
    keyed by position in the file, so two pads with one number never merge."""
    table = _net_table(root)
    records: list[dict] = []
    footprints = [c for c in root.children if c.name == "footprint"]
    for fi, fp in enumerate(footprints):
        fat = fp.find("at")
        fx, fy = (float(fat.values[0]), float(fat.values[1])) if fat is not None else (0.0, 0.0)
        rot = float(fat.values[2]) if fat is not None and len(fat.values) > 2 else 0.0
        c, s = math.cos(math.radians(rot)), math.sin(math.radians(rot))
        ref = _footprint_reference(fp)
        for pi, pad in enumerate(fp.find_all("pad")):
            at = pad.find("at")
            px, py = (float(at.values[0]), float(at.values[1])) if at is not None else (0.0, 0.0)
            size = pad.find("size")
            layers: set[str] = set()
            layers_node = pad.find("layers")
            for token in layers_node.values if layers_node is not None else []:
                layers.update(krt._expand_layer_token(str(token), copper))
            uuid = pad.find("uuid")
            records.append(
                {
                    "fi": fi,
                    "pi": pi,
                    "ref": ref,
                    "pad": str(pad.values[0]) if pad.values else "",
                    "kind": str(pad.values[1]) if len(pad.values) > 1 else "",
                    "net": _resolved_net(pad, table) or "",
                    "layers": layers,
                    "size": (float(size.values[0]), float(size.values[1]))
                    if size is not None and len(size.values) >= 2
                    else (0.0, 0.0),
                    "at": (fx + px * c + py * s, fy - px * s + py * c),
                    "uuid": str(uuid.values[0]) if uuid is not None and uuid.values else "",
                }
            )
    return records


# --- propose ------------------------------------------------------------------------


def _evict_stale() -> None:
    now = _clock()
    for token in [t for t, entry in _PENDING.items() if entry["expiry"] <= now]:
        del _PENDING[token]
    overflow = len(_PENDING) - _PENDING_MAX_ENTRIES
    if overflow > 0:
        for token, _ in sorted(_PENDING.items(), key=lambda kv: kv[1]["expiry"])[:overflow]:
            del _PENDING[token]


def _default_output_dir(pcb_path: Path) -> Path:
    resolved = pcb_path.resolve()
    return resolved.parent.parent / f"{resolved.parent.name}_poured"


def _no_token(status: str, reason: str) -> dict:
    return {"status": status, "reason": reason, "revision_token": None}


def propose_pour_step(
    pcb_path: str | Path,
    *,
    pours: dict[str, str] | None = None,
    output_dir: str | Path | None = None,
) -> dict:
    """Dry run: validate the requested pours (``{net: copper layer}``, one net per
    layer, several nets allowed) and return a revision token. Writes nothing and
    runs nothing; the original is only read."""
    if not pours:
        return _no_token("skipped", "no pour requested: pass pours={net: copper layer} to pour a plane")
    src = Path(pcb_path)
    if not src.is_file():
        return _no_token("error", f"file not found: {pcb_path}")
    try:
        root = input_prep._parse(src)
        copper = krt.board_pour_info(src)["copper_layers"]
        nets = _net_names(root)
        table = _net_table(root)
        zones = [z for z in (_zone_info(n, table) for n in root.children if n.name == "zone") if z]
    except Exception as exc:  # noqa: BLE001
        return _no_token("error", f"failed to read board: {exc}")

    wanted: list[dict] = []
    already: list[str] = []
    for net, layer in pours.items():
        if not isinstance(net, str) or not net or net not in nets:
            return _no_token(
                "error", f"net {net!r} is not a net of this board (nets: {sorted(nets)})"
            )
        if layer not in copper:
            return _no_token(
                "error", f"{layer!r} is not a copper layer of this board (copper layers: {copper})"
            )
        same = [z for z in zones if layer in z["layers"] and z["net"] == net]
        other = sorted({z["net"] for z in zones if layer in z["layers"] and z["net"] != net})
        if other:
            return _no_token(
                "error",
                f"{layer} already holds a zone for net {other[0]}: a pour for {net} would overlap it",
            )
        if same:
            already.append(f"{net} on {layer}")
            continue
        wanted.append({"net": net, "layer": layer})
    if not wanted:
        return _no_token(
            "skipped",
            "the board already carries a pour for every requested net: " + ", ".join(already),
        )
    if len({w["layer"] for w in wanted}) != len(wanted):
        return _no_token(
            "error", "two requested nets share one layer: the plane script pours one net per layer"
        )

    out_dir = Path(output_dir) if output_dir else _default_output_dir(src)
    problem = input_prep._output_dir_problem(src, out_dir)
    if problem:
        return _no_token("error", problem)

    _evict_stale()
    token = secrets.token_hex(8)
    _PENDING[token] = {
        "src": input_prep._norm(src),
        "sha": _sha256(src),
        "pours": wanted,
        "out_dir": str(out_dir),
        "expiry": _clock() + _PENDING_TTL_SECONDS,
    }
    return {
        "status": "proposed",
        "pours": wanted,
        "already_poured": already,
        "output_pcb": str(out_dir / src.name),
        "revision_token": token,
        "note": (
            "creates a new board revision only if the router's candidate is graded and accepted; "
            "the original is untouched. Call confirm_pour_step with this revision_token and a "
            "pinned rule baseline to run it."
        ),
    }


# --- candidate comparison -----------------------------------------------------------

_PAD_DESCRIPTION = re.compile(r"(\S+)\s+(\S+)\s+\[([^\]]*)\]\s+\S+\s+(\S+)")
_JSON_SUMMARY = re.compile(r"^JSON_SUMMARY:\s*(\{.*\})\s*$", re.MULTILINE)


def _project_fingerprint(pcb_path: Path) -> dict[str, str]:
    """Hash of every file of the project directory the gate would copy."""
    root = pcb_path.parent
    found: dict[str, str] = {}
    for current, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d not in gate._SKIP_DIRS and not d.endswith("-backups")]
        for name in files:
            path = Path(current) / name
            found[str(path.relative_to(root))] = _sha256(path)
    return found


def _canon(node: Any) -> Any:
    """Content of a node as a comparable value; floats are rounded to 4 places so a
    writer that reformats numbers is not read as a modification."""
    if node.is_atom:
        value = node.value
        return round(value, 4) if isinstance(value, float) else value
    return (node.name, tuple(_canon(c) for c in node.children))


def _node_key(node: Any) -> str:
    uuid = node.find("uuid")
    if uuid is not None and uuid.values:
        return f"{node.name}:{uuid.values[0]}"
    return f"{node.name}:{node.to_string(compact=True)}"


def _diff_boards(original: Any, candidate: Any, plane_nets: set[str]) -> dict:
    """What the plane run added and removed, and every change an add-only carry-over
    cannot express. Compared by content (not counts, not uuids), from raw nodes."""
    problems: list[str] = []
    table_before, table_after = _net_table(original), _net_table(candidate)
    if table_before != table_after:
        problems.append("the router changed the net table; its nodes cannot be carried over")

    def carried(root: Any) -> dict[str, Any]:
        return {_node_key(c): c for c in root.children if c.name in _CARRIED_TAGS}

    before, after = carried(original), carried(candidate)
    modified = sorted(k for k in before if k in after and _canon(before[k]) != _canon(after[k]))
    if modified:
        problems.append(
            f"the router modified {len(modified)} existing zone/via/track node(s) in place: "
            + ", ".join(modified[:10])
        )

    def others(root: Any) -> Counter:
        return Counter(
            _canon(c)
            for c in root.children
            if c.name and c.name not in _CARRIED_TAGS and c.name not in _META_TAGS
        )

    changed = others(original) != others(candidate)
    if changed:
        kinds = sorted(
            {c[0] for c in (others(original) - others(candidate))}
            | {c[0] for c in (others(candidate) - others(original))}
        )
        problems.append(
            "the router changed board content other than zones, vias and tracks: " + ", ".join(kinds)
        )

    added = [after[k] for k in after if k not in before]
    # Carried nodes: net id and name must agree with the table; every name must be a net.
    known = _net_names(original)
    for node in added:
        info = _net_of(node)
        label = f"{node.name} {_node_key(node)}"
        if info["id"] is not None and info["id"] != 0 and table_before and info["id"] not in table_before:
            problems.append(f"{label}: net id {info['id']} is not in the net table")
        if (
            info["id"] is not None
            and info["name"] is not None
            and table_before
            and table_before.get(info["id"]) != info["name"]
        ):
            problems.append(
                f"{label}: net id {info['id']} is {table_before.get(info['id'])!r} in the net table "
                f"but the node says {info['name']!r} (net id collision)"
            )
        name = _resolved_net(node, table_before)
        if name is None or (name and name not in known):
            problems.append(f"{label}: names a net that is not on the board ({name!r})")
        if node.name == "zone" and name not in plane_nets:
            problems.append(f"{label}: a zone for net {name!r}, which was not requested")
        if node.name == "zone":
            zone_layers = (_zone_info(node, table_before) or {}).get("layers", [])
            if len(zone_layers) != 1:
                problems.append(
                    f"{label}: a zone on {len(zone_layers)} layers ({zone_layers}); only single-layer "
                    "pours are carried over"
                )
    return {
        "added": added,
        "removed": sorted(k for k in before if k not in after),
        "problems": problems,
    }


def _descriptions(entry: dict) -> tuple[str, ...]:
    return tuple(sorted(str(i.get("description", "")) for i in entry.get("items", [])))


def _violation_key(violation: dict) -> tuple:
    return (
        violation.get("type"),
        tuple(sorted(str(i.get("description", "")) for i in violation.get("items", []))),
    )


def _pad_items(entry: dict) -> list[dict]:
    """Pad items of one unconnected entry: ``[{ref, pad, net, description, pos}]``.
    kicad-cli localises descriptions ("Pad 2 [GND] of C1 on F.Cu" reads "Pad 2 [GND]
    von C1 auf F.Cu" on a German install), so only the shape
    ``<kind> <pad> [<net>] <word> <ref>`` is relied on."""
    found = []
    for item in entry.get("items", []):
        text = str(item.get("description", ""))
        match = _PAD_DESCRIPTION.search(text)
        if match:
            found.append(
                {
                    "ref": match.group(4),
                    "pad": match.group(2),
                    "net": match.group(3),
                    "description": text,
                    "pos": item.get("pos"),
                }
            )
    return found


def _by_key(pads: list[dict]) -> dict[tuple[str, str], list[dict]]:
    index: dict[tuple[str, str], list[dict]] = {}
    for pad in pads:
        index.setdefault((pad["ref"], pad["pad"]), []).append(pad)
    return index


def _position_problems(pads: list[dict], grade: dict) -> list[str]:
    """Pads whose computed world position disagrees with what kicad-cli reported."""
    index = _by_key(pads)
    problems = []
    for entry in grade["unconnected"]:
        for item in _pad_items(entry):
            records = index.get((item["ref"], item["pad"]))
            pos = item.get("pos") or {}
            if not records or "x" not in pos:
                continue
            nearest = min(math.dist(r["at"], (pos["x"], pos["y"])) for r in records)
            if nearest > _POSITION_TOLERANCE_MM:
                problems.append(
                    f"{item['ref']} pad {item['pad']}: computed {records[0]['at']} but kicad-cli "
                    f"reports ({pos['x']}, {pos['y']})"
                )
    return problems


def _waiting_pads(pads: list[dict], plane_layers: dict[str, str]) -> list[dict]:
    """Plane-net pads with no copper on the pour layer: the plane script places no
    taps, so they wait for a via from the routing step. A pad number shared by several
    pads of one footprint waits only if every one of them does (fail closed)."""
    index = _by_key(pads)
    waiting = []
    for pad in pads:
        layer = plane_layers.get(pad["net"])
        if layer is None or layer in pad["layers"]:
            continue
        if all(layer not in r["layers"] for r in index[(pad["ref"], pad["pad"])]):
            waiting.append(pad)
    return waiting


def _assess(
    before: dict,
    candidate: dict,
    plane_layers: dict[str, str],
    pads: list[dict],
    probe_unconnected: list[dict] | None,
) -> dict:
    """The unconnected and violation part of the acceptance rule (geometry is checked
    after the revision is refilled).

    KiCad reports an unconnected item as a LINK between two clusters, so a pad that
    sits in a link next to a waiting pad may be perfectly connected. When pads wait for
    a via, the caller grades a probe copy in which every waiting pad of a plane net is
    detached; what is still unconnected there is something the pour failed to reach."""
    reasons: list[str] = []
    plane_entries = [u for u in candidate["unconnected"] if u["net"] in plane_layers]
    waiting = _waiting_pads(pads, plane_layers)
    if plane_entries and waiting:
        if probe_unconnected is None:
            raise ValueError("a probe grade is required when pads are waiting for a via")
        failing = [u for u in probe_unconnected if u["net"] in plane_layers]
    else:
        failing = plane_entries
    isolated: list[dict] = []
    for entry in failing:
        isolated.extend(_pad_items(entry))
        reasons.append(
            f"the pour leaves a missing connection on plane net {entry['net']}: "
            + "; ".join(_descriptions(entry))
        )
    listed = {(p["ref"], p["pad"]) for e in plane_entries for p in _pad_items(e)}
    pending = [
        {"ref": p["ref"], "pad": p["pad"], "net": p["net"], "pos": list(p["at"])}
        for p in waiting
        if (p["ref"], p["pad"]) in listed
    ]
    known_open = {(u["net"], _descriptions(u)) for u in before["unconnected"]}
    for entry in candidate["unconnected"]:
        if entry["net"] in plane_layers:
            continue
        if (entry["net"], _descriptions(entry)) not in known_open:
            reasons.append(
                f"the pour introduced an unconnected item on net {entry['net']}: "
                + "; ".join(_descriptions(entry))
            )
    known = Counter(_violation_key(v) for v in before["violations"])
    introduced = Counter(_violation_key(v) for v in candidate["violations"]) - known
    new_violations = [v for v in candidate["violations"] if introduced.get(_violation_key(v), 0) > 0]
    for v in new_violations:
        reasons.append(
            f"the pour introduced an error-level violation: {v.get('type')}: {_violation_key(v)[1]}"
        )
    return {
        "accepted": not reasons,
        "reasons": reasons,
        "isolated_pads": isolated,
        "pending_pads": pending,
        "new_violations": new_violations,
        "needs_probe": bool(plane_entries and waiting),
    }


def _detach_pads(root: Any, targets: list[dict]) -> int:
    """Set the net of the given pads (by footprint and pad position in the file) to
    none, in a parsed probe copy; returns how many changed. Never used on a board of
    record."""
    wanted = {(p["fi"], p["pi"]): (p["ref"], p["pad"]) for p in targets}
    footprints = [c for c in root.children if c.name == "footprint"]
    changed = 0
    for (fi, pi), (ref, number) in wanted.items():
        if fi >= len(footprints):
            continue
        pads = footprints[fi].find_all("pad")
        if pi >= len(pads) or _footprint_reference(footprints[fi]) != ref:
            continue
        pad = pads[pi]
        net = pad.find("net")
        if str(pad.values[0]) != number or net is None:
            continue
        net.set_value(0, 0)
        net.set_value(1, "")
        changed += 1
    return changed


def _probe_unconnected(
    cand_pcb: Path, scratch: Path, targets: list[dict], grade_args: dict
) -> list[dict]:
    """Unconnected entries of the candidate with every waiting pad detached from its net."""
    from kicad_tools.schema import PCB

    probe_pcb = gate._copy_project(cand_pcb, scratch / "probe")
    board = PCB.load(probe_pcb)
    if _detach_pads(board._sexp, targets) != len(targets):
        raise ValueError("a waiting pad could not be found in the probe copy")
    write_layer.save_pcb(board, probe_pcb)
    return gate.verify_board(probe_pcb, **grade_args)["unconnected"]


# --- fill evidence and coverage -------------------------------------------------------


def _ring_area(ring: list[tuple[float, float]]) -> float:
    return abs(sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]))) / 2.0


def _inside(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    x, y = point
    inside = False
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _distance_to_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> float:
    best = math.inf
    px, py = point
    for (x1, y1), (x2, y2) in zip(ring, ring[1:] + ring[:1]):
        dx, dy = x2 - x1, y2 - y1
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((px - x1) * dx + (py - y1) * dy) / length2))
        best = min(best, math.hypot(px - (x1 + t * dx), py - (y1 + t * dy)))
    return best


def _filled_rings(root: Any) -> dict[str, list[tuple[str, list[tuple[float, float]]]]]:
    """``{zone uuid: [(layer, ring)]}`` from the ``filled_polygon`` nodes of a
    kicad-cli-refilled board."""
    found: dict[str, list[tuple[str, list[tuple[float, float]]]]] = {}
    for zone in root.children:
        if zone.name != "zone":
            continue
        uuid = zone.find("uuid")
        key = str(uuid.values[0]) if uuid is not None and uuid.values else ""
        for fp in zone.find_all("filled_polygon"):
            layer = fp.find("layer")
            pts = fp.find("pts")
            if pts is None:
                continue
            ring = [(float(xy.values[0]), float(xy.values[1])) for xy in pts.find_all("xy")]
            found.setdefault(key, []).append((str(layer.values[0]) if layer is not None else "", ring))
    return found


def _zone_reach(zone_node: Any) -> float:
    """How far a fill edge may lie from a pad centre for the zone to touch the pad:
    clearance and thermal gap of the zone, read from its raw settings."""
    clearance, gap = 0.5, 0.5
    connect = zone_node.find("connect_pads")
    if connect is not None:
        c = connect.find("clearance")
        if c is not None and c.values:
            clearance = float(c.values[0])
    fill = zone_node.find("fill")
    if fill is not None:
        g = fill.find("thermal_gap")
        if g is not None and g.values:
            gap = float(g.values[0])
    return clearance + gap


def _refilled_copy(pcb_path: Path, scratch: Path) -> Path:
    """A copy of the board refilled by ``kicad-cli pcb drc --refill-zones
    --save-board``. The copy is saved; the board itself is never touched."""
    copy = gate._copy_project(pcb_path, scratch / "refilled")
    kicad_cli._run_report(
        ["pcb", "drc"],
        copy,
        severity_all=True,
        report_name="refill_report.json",
        extra_args=("--refill-zones", "--save-board"),
        ok_exit_codes=(0,),
    )
    return copy


def _fill_findings(
    revision_pcb: Path,
    refilled_pcb: Path,
    added_zone_uuids: set[str],
    plane_layers: dict[str, str],
    pads: list[dict],
    min_fill_fraction: float = MIN_FILL_FRACTION,
) -> dict:
    """Fill evidence per added zone and a cheap geometric PRE-FILTER of every plane-net
    pad, from the refilled copy. Everything found here can reject; nothing here accepts a
    pad (connectivity is decided by kicad-cli, see ``_connectivity``).

    ``fill_fraction`` is the sum of ALL fill polygons over the outline area, so it
    includes islands that touch no pad: it says "this is a pour, not a sliver" and
    nothing about connection.

    Waiting pads (no copper on the pour layer) must lie inside the zone outline and over
    fill copper (no other-net hole under the pad). The via itself, its drill clearance
    and tap track are NOT checked: the routing step decides those.

    Returns ``zones``, ``isolated``, ``unreachable_waiting``, ``problems`` and ``rings``
    (net -> fill rings on the pour layer)."""
    original_root = input_prep._parse(revision_pcb)
    table = _net_table(original_root)
    refilled = _filled_rings(input_prep._parse(refilled_pcb))
    zones_out: list[dict] = []
    isolated: list[dict] = []
    unreachable: list[dict] = []
    problems: list[str] = []
    rings_by_net: dict[str, list] = {}
    for node in original_root.children:
        if node.name != "zone":
            continue
        info = _zone_info(node, table)
        if not info or info["uuid"] not in added_zone_uuids:
            continue
        layer = info["layer"]
        rings = [ring for ring_layer, ring in refilled.get(info["uuid"], []) if ring_layer in ("", layer)]
        outline_area = _ring_area(info["polygon"]) if info["polygon"] else 0.0
        fill_area = sum(_ring_area(r) for r in rings)
        fraction = fill_area / outline_area if outline_area > 0 else 0.0
        zones_out.append(
            {
                **{k: info[k] for k in ("net", "layer", "layers", "uuid", "polygon", "filled_in_file")},
                "refilled_by_kicad_cli": True,
                "filled_area_mm2": round(fill_area, 3),
                "outline_area_mm2": round(outline_area, 3),
                "fill_fraction": round(fraction, 3),
                "fill_polygons": len(rings),
            }
        )
        if not rings:
            problems.append(f"zone {info['net']} on {layer}: kicad-cli's refill produced no fill copper")
            for pad in pads:
                if pad["net"] == info["net"] and layer in pad["layers"]:
                    isolated.append(
                        _pad_report(pad, "the zone has no fill copper (KiCad drops fill that touches no pad)")
                    )
            continue
        rings_by_net.setdefault(info["net"], []).extend(rings)
        if fraction < min_fill_fraction:
            problems.append(
                f"zone {info['net']} on {layer}: fill covers {fraction:.0%} of the outline "
                f"(below {min_fill_fraction:.0%})"
            )
        reach = _zone_reach(node)
        connect = node.find("connect_pads")
        mode = "thermal"
        if connect is not None and connect.values and str(connect.values[0]) in ("yes", "no", "thru_hole_only"):
            mode = str(connect.values[0])
        bridge_node = node.find("fill").find("thermal_bridge_width") if node.find("fill") is not None else None
        bridge = float(bridge_node.values[0]) if bridge_node is not None and bridge_node.values else 0.5
        for pad in pads:
            if pad["net"] != info["net"]:
                continue
            if layer not in pad["layers"]:
                if not (info["polygon"] and _inside(pad["at"], info["polygon"])):
                    unreachable.append(_pad_report(pad, "waits for a via but lies outside the zone outline"))
                elif not any(_inside(pad["at"], ring) for ring in rings):
                    unreachable.append(
                        _pad_report(pad, "waits for a via but there is no fill copper under the pad")
                    )
                continue
            tht = pad["kind"] != "smd"
            if mode == "no":
                isolated.append(_pad_report(pad, "the zone has connect_pads no: no pad is connected"))
                continue
            if mode == "thru_hole_only" and not tht:
                isolated.append(
                    _pad_report(pad, "the zone has connect_pads thru_hole_only: an SMD pad is not connected")
                )
                continue
            if mode in ("thermal", "thru_hole_only") and bridge <= 0:
                isolated.append(
                    _pad_report(pad, "thermal relief with thermal_bridge_width 0 has no spokes: not connected")
                )
                continue
            radius = max(pad["size"]) / 2.0
            covered = any(
                _inside(pad["at"], ring) or _distance_to_ring(pad["at"], ring) <= radius + reach + 0.1
                for ring in rings
            )
            if not covered:
                isolated.append(_pad_report(pad, "no fill copper within reach of the pad"))
    return {
        "zones": zones_out,
        "isolated": isolated,
        "unreachable_waiting": unreachable,
        "problems": problems,
        "rings": rings_by_net,
    }


# --- connectivity: decided by kicad-cli on the refilled board ------------------------


def _net_spec(name: str, table: dict[int, str]) -> str:
    reverse = {v: k for k, v in table.items()}
    return f"(net {reverse[name]})" if name in reverse else f'(net "{name}")'


def _pad_net_spec(name: str, table: dict[int, str]) -> str:
    reverse = {v: k for k, v in table.items()}
    return f'(net {reverse[name]} "{name}")' if name in reverse else f'(net "{name}")'


def _anchor_uuid(net: str) -> str:
    """Deterministic uuid of a net's anchor pad (no process-dependent hashing)."""
    digits = int(hashlib.sha1(net.encode("utf-8")).hexdigest(), 16) % 10**12
    return f"00000000-0000-4000-8000-{digits:012d}"


def _anchor_point(
    rings: list[list[tuple[float, float]]], avoid: list[tuple[tuple[float, float], float]]
) -> tuple[float, float] | None:
    """A point deep inside the largest fill polygon (grid search for the point farthest
    from every edge) that is also clear of every plane-net pad (``avoid`` is a list of
    centre/radius), where the anchor pad is surely on the zone's copper and touches
    nothing else."""
    if not rings:
        return None
    ring = max(rings, key=_ring_area)
    xs, ys = [x for x, _ in ring], [y for _, y in ring]
    best, best_d = None, 0.0
    steps = 40
    for i in range(steps + 1):
        for j in range(steps + 1):
            point = (min(xs) + (max(xs) - min(xs)) * i / steps, min(ys) + (max(ys) - min(ys)) * j / steps)
            if not _inside(point, ring) or any(math.dist(point, c) < r for c, r in avoid):
                continue
            d = _distance_to_ring(point, ring)
            if d > best_d:
                best, best_d = point, d
    return best if best_d >= 0.4 else None


# --- copper clusters (a pad together with the same-net copper physically attached to it) ---


def _point_segment(p, a, b) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / length2))
    return math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy))


def _segments_cross(a, b, c, d) -> bool:
    def side(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    return side(a, b, c) * side(a, b, d) < 0 and side(c, d, a) * side(c, d, b) < 0


def _poly_distance(p: list, q: list) -> float:
    """Minimum distance between two polylines (a single point is a one-point polyline)."""
    def segs(poly):
        return list(zip(poly, poly[1:])) if len(poly) > 1 else [(poly[0], poly[0])]

    best = math.inf
    for a, b in segs(p):
        for c, d in segs(q):
            if _segments_cross(a, b, c, d):
                return 0.0
            best = min(best, _point_segment(a, c, d), _point_segment(b, c, d),
                       _point_segment(c, a, b), _point_segment(d, a, b))
    return best


def _copper_items(root: Any, plane_nets: set[str], table: dict[int, str]) -> list[dict]:
    """Tracks, arcs and vias of the plane nets: ``{node, net, poly, radius, layers}``."""
    items = []
    for node in root.children:
        if node.name not in ("segment", "arc", "via"):
            continue
        net = _resolved_net(node, table)
        if net not in plane_nets:
            continue
        if node.name == "via":
            at, size, layers = node.find("at"), node.find("size"), node.find("layers")
            if at is None or size is None:
                continue
            items.append({
                "node": node, "net": net,
                "poly": [(float(at.values[0]), float(at.values[1]))],
                "radius": float(size.values[0]) / 2.0,
                "layers": {str(v) for v in layers.values} if layers is not None else set(),
            })
            continue
        width = node.find("width")
        layer = node.find("layer")
        pts = [node.find(tag) for tag in ("start", "mid", "end")]
        poly = [(float(p.values[0]), float(p.values[1])) for p in pts if p is not None]
        if len(poly) < 2 or width is None:
            continue
        items.append({
            "node": node, "net": net, "poly": poly, "radius": float(width.values[0]) / 2.0,
            "layers": {str(layer.values[0])} if layer is not None and layer.values else set(),
        })
    return items


def _attached_copper(items: list[dict], seeds: list[dict]) -> set[int]:
    """Indices of ``items`` physically attached (same net, shared layer, copper touching,
    with a small tolerance) to a seed pad, directly or through other attached items.
    Over-inclusion keeps more copper in a variant; under-inclusion only makes a pad look
    less connected, which can reject but never accept."""
    reached: set[int] = set()
    frontier: list[dict] = list(seeds)
    while frontier:
        current = frontier.pop()
        for i, item in enumerate(items):
            if i in reached or item["net"] != current["net"] or not (item["layers"] & current["layers"]):
                continue
            if _poly_distance(item["poly"], current["poly"]) <= item["radius"] + current["radius"] + 0.01:
                reached.add(i)
                frontier.append(item)
    return reached


def _variant(
    refilled_pcb: Path,
    scratch: Path,
    name: str,
    pads: list[dict],
    keep: set[int],
    anchors: dict[str, tuple[float, float]],
    plane_layers: dict[str, str],
    keep_zones: set[str],
    *,
    keep_attached_copper: bool = True,
) -> Path:
    """A scratch copy of the refilled board for one connectivity question.

    Every plane-net pad outside ``keep`` (indices into ``pads``) has no net. Every
    plane-net track, arc and via is removed except the copper physically attached to a
    kept pad (``keep_attached_copper``), and every plane-net zone is removed except the
    carried ones (``keep_zones``): the answer must stand on the new pour alone, not on a
    plane on another layer or on a stray stub. Each plane net gets one anchor pad that
    exists on the pour layer ONLY (a through via would merge the pour with same-net
    copper on the other layer under it), placed inside the fill, so the zone forms a
    cluster of its own and a pad outside it is listed by kicad-cli. Never a board of
    record."""
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import parse_string

    plane_nets = set(plane_layers)
    copy = gate._copy_project(refilled_pcb, scratch / name)
    board = PCB.load(copy)
    root = board._sexp
    table = _net_table(root)
    # kicad-cli re-saved the board, so footprints and pads are matched by reference,
    # number and world position, never by their order in the file.
    here = _pad_records(root, plane_layers_of(pads))

    def ident(p):
        return (p["ref"], p["pad"], round(p["at"][0], 2), round(p["at"][1], 2))

    index: dict[tuple, list[dict]] = {}
    for rec in here:
        index.setdefault(ident(rec), []).append(rec)
    detach, kept_recs = [], []
    for i, p in enumerate(pads):
        if p["net"] not in plane_nets:
            continue
        found = index.get(ident(p))
        if not found:
            raise ValueError(f"{p['ref']} pad {p['pad']} could not be found in the variant copy")
        (kept_recs if i in keep else detach).extend(found)
    if _detach_pads(root, detach) != len(detach):
        raise ValueError("a plane-net pad could not be detached in the variant copy")

    items = _copper_items(root, plane_nets, table)
    attached: set[int] = set()
    if keep_attached_copper and kept_recs:
        seeds = [
            {"net": r["net"], "poly": [r["at"]], "radius": math.hypot(*r["size"]) / 2.0, "layers": r["layers"]}
            for r in kept_recs
        ]
        attached = _attached_copper(items, seeds)
    for i, item in enumerate(items):
        if i not in attached:
            root.remove(item["node"])
    for zone in [c for c in root.children if c.name == "zone"]:
        info = _zone_info(zone, table)
        if info and info["net"] in plane_nets and info["uuid"] not in keep_zones:
            root.remove(zone)
    for net, (x, y) in anchors.items():
        layer = plane_layers[net]
        root.append(
            parse_string(
                f'(footprint "kicad-mcp:ANCHOR" (layer "{layer}") (uuid "{_anchor_uuid(net)}") (at {x:.4f} {y:.4f} 0) '
                f'(fp_text reference "ANCHOR" (at 0 0) (layer "F.SilkS") (uuid "{_anchor_uuid(net + "-t")}") '
                "(effects (font (size 1 1) (thickness 0.15)))) "
                f'(pad "1" smd circle (at 0 0) (size 0.6 0.6) (layers "{layer}") {_pad_net_spec(net, table)}))'
            )
        )
    write_layer.save_pcb(board, copy)
    return copy


def plane_layers_of(pads: list[dict]) -> list[str]:
    """Copper layers named by any pad (used to expand pad layer wildcards in a copy)."""
    return sorted({layer for p in pads for layer in p["layers"]})


def _connectivity(
    refilled_pcb: Path,
    scratch: Path,
    pads: list[dict],
    plane_layers: dict[str, str],
    rings: dict[str, list],
    zone_uuids: set[str],
    run_report: Callable[[Path], dict],
) -> dict:
    """Which plane-net pads kicad-cli itself finds connected to the new zone, measured on
    the refilled board (stored fill, no refill).

    Each variant (see ``_variant``) keeps a GROUP of reaching pads attached; a group
    with no unconnected plane-net item is connected as a whole, otherwise it is bisected
    down to single pads (one group run for a healthy net, about 2N for N isolated
    pads), under the ``MAX_CONNECTIVITY_RUNS`` cap. Nothing is subtracted by
    description: a link is a link (a track's description says only layer and length, so
    the same words can end a different cluster). A report that finds nothing is run once
    more and the two are united, as the gate does; a report without ``unconnected_items``
    is an error, never a clean result. A separate variant without any plane-net copper
    makes every pad its own cluster, to cross-check ALL pad positions against kicad-cli.
    """
    plane_nets = set(plane_layers)
    out: dict[str, Any] = {"checked": True, "isolated": [], "incomplete": None, "runs": 0,
                           "positions_verified": False, "proven": []}
    avoid = [
        (p["at"], max(p["size"]) / 2.0 + 0.8) for p in pads if p["net"] in plane_nets
    ]
    anchors = {}
    for net in plane_nets:
        point = _anchor_point(rings.get(net, []), avoid)
        if point is None:
            out["incomplete"] = f"no room for an anchor pad inside the {net} fill, clear of its pads"
            return out
        anchors[net] = point
    runs = 0

    def run(variant: Path) -> dict:
        nonlocal runs
        runs += 1
        report = run_report(variant)
        if "unconnected_items" not in report:
            raise ValueError("kicad-cli report has no 'unconnected_items' (not read as clean)")
        return report

    def plane_links(report: dict) -> set[tuple]:
        return {
            (_net_of_entry(e), _descriptions(e))
            for e in report["unconnected_items"]
            if _net_of_entry(e) in plane_nets
        }

    def links_in(variant: Path) -> set[tuple]:
        found = plane_links(run(variant))
        if not found:  # the accepting answer is asked twice and the answers united
            found = plane_links(run(variant))
        return found

    # 1. position cross-check of every plane-net pad: each is a lone cluster.
    all_plane = {i for i, p in enumerate(pads) if p["net"] in plane_nets}
    v_all = _variant(refilled_pcb, scratch, "v_all", pads, all_plane, anchors, plane_layers, set(),
                     keep_attached_copper=False)
    listed: list[tuple[str, str, float, float]] = []
    for entry in run(v_all)["unconnected_items"]:
        for item in _pad_items(entry):
            pos = item.get("pos") or {}
            if "x" in pos:
                listed.append((item["ref"], item["pad"], pos["x"], pos["y"]))
    bad = []
    for i in sorted(all_plane):
        p = pads[i]
        hits = [(x, y) for ref, num, x, y in listed if (ref, num) == (p["ref"], p["pad"])]
        if not hits:
            bad.append(f"{p['ref']} pad {p['pad']}: not listed, position unverified")
        elif min(math.dist(p["at"], h) for h in hits) > _POSITION_TOLERANCE_MM:
            bad.append(
                f"{p['ref']} pad {p['pad']}: computed {tuple(round(v, 3) for v in p['at'])}, kicad-cli {hits[0]}"
            )
    out["positions_verified"] = not bad
    if bad:
        out["incomplete"] = "pad positions could not be cross-checked against kicad-cli: " + "; ".join(bad)
        out["runs"] = runs
        return out

    # 2. control: no pad attached. Any plane-net link here is unexplained: fail closed.
    control = links_in(
        _variant(refilled_pcb, scratch, "v_control", pads, set(), anchors, plane_layers, zone_uuids)
    )
    if control:
        out["incomplete"] = (
            "kicad-cli lists plane-net links with no pad attached (fill islands that survive "
            "without a pad?): " + "; ".join(" / ".join(d) for _, d in sorted(control))
        )
        out["runs"] = runs
        return out

    # 3. group / bisect.
    reaching = [
        i for i, p in enumerate(pads) if p["net"] in plane_nets and plane_layers[p["net"]] in p["layers"]
    ]
    queue = [reaching] if reaching else []
    isolated_idx: list[int] = []
    proven: list[int] = []
    n = 0
    while queue and runs < MAX_CONNECTIVITY_RUNS:
        group = queue.pop(0)
        n += 1
        variant = _variant(refilled_pcb, scratch, f"v_{n}", pads, set(group), anchors, plane_layers, zone_uuids)
        if not links_in(variant):
            proven.extend(group)
            continue
        if len(group) == 1:
            isolated_idx.append(group[0])
        else:
            half = len(group) // 2
            queue += [group[:half], group[half:]]
    out["isolated"] = [
        _pad_report(pads[i], "kicad-cli lists it unconnected to the new zone in the refilled board")
        for i in isolated_idx
    ]
    out["proven"] = sorted(proven)
    if queue:
        left = sum(len(g) for g in queue)
        out["incomplete"] = (
            f"connectivity run cap ({MAX_CONNECTIVITY_RUNS}) reached with {left} pad(s) unproven"
        )
    out["runs"] = runs
    return out


def _net_of_entry(entry: dict) -> str | None:
    return gate._net_of(entry)


def _pad_report(pad: dict, why: str) -> dict:
    return {
        "ref": pad["ref"],
        "pad": pad["pad"],
        "net": pad["net"],
        "pos": [round(pad["at"][0], 3), round(pad["at"][1], 3)],
        "reason": why,
    }


# --- confirm ------------------------------------------------------------------------


def _gate_summary(grade: dict) -> dict:
    keys = (
        "verdict", "unconnected", "violations", "warnings", "unstable", "runs",
        "list_complete", "incomplete_reasons", "notes", "parity",
    )
    return {k: (grade.get(k, []) if k == "notes" else grade[k]) for k in keys}


def _router_summary(stdout: str) -> dict | None:
    matches = _JSON_SUMMARY.findall(stdout or "")
    if not matches:
        return None
    try:
        return json.loads(matches[-1])
    except ValueError:
        return None


def _result(status: str, reason: str, **extra: Any) -> dict:
    return {"status": status, "reason": reason, **extra}


def _same_findings(a: dict, b: dict) -> bool:
    """Same unconnected items per net and same error-level violations in two gate results."""
    return {(u["net"], _descriptions(u)) for u in a["unconnected"]} == {
        (u["net"], _descriptions(u)) for u in b["unconnected"]
    } and Counter(_violation_key(v) for v in a["violations"]) == Counter(
        _violation_key(v) for v in b["violations"]
    )


def confirm_pour_step(
    pcb_path: str | Path,
    revision_token: str,
    *,
    parity: str,
    parity_reason: str | None = None,
    baseline: dict | None = None,
    drc: Callable[..., dict] | None = None,
    router: Callable[..., dict] | None = None,
    fill: Callable[[Path], Path] | None = None,
    connectivity_check: bool = True,
    min_fill_fraction: float = MIN_FILL_FRACTION,
    timeout: int = _DEFAULT_TIMEOUT_S,
) -> dict:
    """Run the plane script on a scratch copy, grade its candidate, and only when it
    is accepted carry the zone (and any vias and tracks the plane script added) onto
    a NEW revision of the original, which is graded, refilled and checked again before
    it is moved into place.

    ``baseline`` must be the pinned rule baseline (the gate never pins silently).
    ``drc``, ``router``, ``fill`` and ``connectivity_check=False`` replace or disable
    the ``kicad-cli`` grading, router, ``kicad-cli`` refill and ``kicad-cli``
    connectivity boundaries. They are for tests ONLY: a registered tool must never
    forward them (a source-scan test guards this). ``min_fill_fraction`` is the share of
    the zone outline the fill must cover. The token is consumed only by a
    poured revision or by detected source drift; every other outcome leaves it usable.
    """
    _evict_stale()
    entry = _PENDING.get(revision_token)
    if entry is None:
        return _result("error", "unknown or expired revision_token")
    src = Path(pcb_path)
    if input_prep._norm(src) != entry["src"]:
        return _result("error", "pcb_path does not match the path this proposal was made for")
    if not src.is_file() or _sha256(src) != entry["sha"]:
        del _PENDING[revision_token]
        return _result("error", "the board changed since the proposal was made: re-run propose_pour_step")
    if baseline is None:
        return _result(
            "incomplete",
            "rules not pinned: pass the pinned rule baseline (snapshot_rules) so the candidate "
            "is graded against the rules the board was agreed on",
        )
    try:
        text_check = input_prep.check_router_text_moves(src)
    except Exception as exc:  # noqa: BLE001
        return _result("incomplete", f"the board could not be read: {exc}")
    if not text_check["ok"]:
        return _result(
            "incomplete",
            "the board still carries copper-layer text the router would silently move to silkscreen: "
            "run input preparation first, so the carried nodes stay faithful to the original",
            copper_texts=text_check["copper_texts"],
            unhandled=text_check["unhandled"],
        )
    out_dir = Path(entry["out_dir"])
    problem = input_prep._output_dir_problem(src, out_dir)
    if problem:
        return _result("error", problem)

    fingerprint_before = _project_fingerprint(src)
    try:
        outcome = _run(
            src, out_dir, entry["pours"], parity, parity_reason, baseline, drc, router, fill, timeout,
            connectivity_check, min_fill_fraction,
        )
    except Exception as exc:  # noqa: BLE001  (BaseException such as KeyboardInterrupt still propagates)
        outcome = _result("error", f"the pour step failed unexpectedly: {type(exc).__name__}: {exc}")
    if _project_fingerprint(src) != fingerprint_before:
        return _result(
            "error",
            "CRITICAL: the original project directory changed during the pour step",
            original_unchanged=False,
        )
    outcome["original_unchanged"] = True
    if outcome["status"] == "poured":
        del _PENDING[revision_token]
    return outcome


def _run(
    src, out_dir, pours, parity, parity_reason, baseline, drc, router, fill, timeout,
    connectivity_check, min_fill_fraction,
) -> dict:
    plane_layers = {p["net"]: p["layer"] for p in pours}
    grade_args = {"parity": parity, "parity_reason": parity_reason, "baseline": baseline, "drc": drc}
    run_router = router or krt.run_router_script

    with tempfile.TemporaryDirectory() as scratch_str:
        scratch = Path(scratch_str)
        try:
            work_pcb = gate._copy_project(src, scratch / "work")
            cand_pcb = gate._copy_project(src, scratch / "candidate")
            floors = krt.board_floor_overrides(src)["floors"]
            overrides = (
                krt.write_fab_overrides(floors, scratch / "board_floor_overrides.txt") if floors else None
            )
            args = krt.build_route_args(
                work_pcb,
                cand_pcb,
                nets=tuple(p["net"] for p in pours),
                escalation="board",
                clearance_mm=floors.get("clearance"),
                fab_overrides=overrides,
            )
            args += ["--plane-layers", *(p["layer"] for p in pours)]
            run = run_router(PLANE_SCRIPT, args, output_pcb=cand_pcb, original_pcb=work_pcb, timeout=timeout)
        except Exception as exc:  # noqa: BLE001  (KicadRoutingToolsError, OSError, ValueError)
            return _result("incomplete", f"the plane script did not produce a candidate: {exc}")

        router_info = {
            "script": PLANE_SCRIPT,
            "exit_code": run.get("exit_code"),
            "elapsed_s": run.get("elapsed_s"),
            "args": run.get("args"),
            "summary": _router_summary(run.get("stdout_text", "")),
            "project_restored": run.get("project_restored"),
            "router_overwrote_project": run.get("router_overwrote_project"),
            "claim_note": "the router's own summary is a claim, not evidence; only the gate's grade counts",
        }
        if run.get("exit_code") == krt.STRICT_SIZES_EXIT_CODE:
            return _result(
                "rejected",
                "the plane script delivered a feature below the requested size (strict sizes, exit 3)",
                router=router_info,
            )
        if run.get("exit_code") != 0:
            return _result(
                "incomplete",
                f"the plane script exited {run.get('exit_code')}: no candidate to grade",
                router=router_info,
            )

        try:
            original_root = input_prep._parse(src)
            candidate_root = input_prep._parse(cand_pcb)
            copper = krt.board_pour_info(src)["copper_layers"]
            pads = _pad_records(original_root, copper)
        except Exception as exc:  # noqa: BLE001
            return _result("incomplete", f"the candidate could not be parsed: {exc}", router=router_info)
        diff = _diff_boards(original_root, candidate_root, set(plane_layers))
        problems = list(diff["problems"])
        if diff["removed"]:
            problems.append(
                f"the router removed {len(diff['removed'])} existing zone/via/track node(s); "
                "an add-only carry-over cannot express that: " + ", ".join(diff["removed"][:10])
            )
        table = _net_table(candidate_root)
        added_zones = [_zone_info(n, table) for n in diff["added"] if n.name == "zone"]
        for pour in pours:
            if not any(
                z and z["net"] == pour["net"] and pour["layer"] in z["layers"] for z in added_zones
            ):
                problems.append(f"no zone for {pour['net']} on {pour['layer']} in the candidate")
        if problems:
            return _result("rejected", "; ".join(problems), router=router_info)

        # Cheap structural checks are done: now grade the original and the candidate.
        try:
            before = gate.verify_board(src, **grade_args)
        except Exception as exc:  # noqa: BLE001
            return _result("incomplete", f"the original could not be graded: {exc}", router=router_info)
        if before["incomplete_reasons"]:
            return _result(
                "incomplete",
                "the original's grade is incomplete, so a candidate cannot be compared with it: "
                + "; ".join(before["incomplete_reasons"]),
                original_grade=_gate_summary(before),
                router=router_info,
            )
        try:
            graded = gate.verify_board(cand_pcb, **grade_args)
        except Exception as exc:  # noqa: BLE001
            return _result("incomplete", f"the candidate could not be graded: {exc}", router=router_info)
        if graded["incomplete_reasons"]:
            return _result(
                "incomplete",
                "the candidate's grade is incomplete: " + "; ".join(graded["incomplete_reasons"]),
                router=router_info,
                candidate_grade=_gate_summary(graded),
            )
        position_problems = _position_problems(pads, graded)
        if position_problems:
            return _result(
                "incomplete",
                "the pad position model disagrees with kicad-cli (fail closed): "
                + "; ".join(position_problems),
                router=router_info,
            )
        probe = None
        waiting = _waiting_pads(pads, plane_layers)
        if waiting and any(u["net"] in plane_layers for u in graded["unconnected"]):
            try:
                probe = _probe_unconnected(cand_pcb, scratch, waiting, grade_args)
            except Exception as exc:  # noqa: BLE001
                return _result(
                    "incomplete",
                    f"the isolation probe could not be graded: {exc}",
                    router=router_info,
                    candidate_grade=_gate_summary(graded),
                )
        assessment = _assess(before, graded, plane_layers, pads, probe)
        if not assessment["accepted"]:
            return _result(
                "rejected",
                "the candidate was graded and not accepted: " + "; ".join(assessment["reasons"]),
                isolated_pads=assessment["isolated_pads"],
                router=router_info,
                candidate_grade=_gate_summary(graded),
                original_grade=_gate_summary(before),
            )
        return _carry_over(
            src, out_dir, diff["added"], plane_layers, pads, assessment, before,
            grade_args, router_info, graded, fill, scratch, connectivity_check, min_fill_fraction,
            drc,
        )


def _revision_differences(src: Path, revision_pcb: Path, added: list) -> dict:
    """What differs between the original and the written revision, measured: the file is a
    kicad_tools re-serialisation, so text differs widely; semantically it should differ
    only by the net table and the carried nodes."""
    import difflib

    before, after = input_prep._parse(src), input_prep._parse(revision_pcb)

    def content(root: Any) -> Counter:
        return Counter(
            _canon(c)
            for c in root.children
            if c.name and c.name not in _META_TAGS and c.name not in _CARRIED_TAGS and c.name != "net"
        )

    text_before = src.read_text(encoding="utf-8").splitlines()
    text_after = revision_pcb.read_text(encoding="utf-8").splitlines()
    changed_lines = sum(
        1
        for line in difflib.unified_diff(text_before, text_after, n=0, lineterm="")
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    )
    return {
        "text_lines_changed": changed_lines,
        "reserialised_by": "kicad_tools (formatting differs; this is not avoided)",
        "net_table_entries_before": len(_net_table(before)),
        "net_table_entries_after": len(_net_table(after)),
        "carried_nodes": len(added),
        "other_content_identical": content(before) == content(after),
    }


def _carry_over(
    src, out_dir, added, plane_layers, pads, assessment, before, grade_args, router_info,
    cand_graded, fill, scratch, connectivity_check, min_fill_fraction, drc,
) -> dict:
    """Append the candidate's added nodes to a fresh copy of the original, reload it,
    grade and refill it, check fill and reach, and only then move it into ``out_dir``."""
    from kicad_tools.schema import PCB

    missing: list[Path] = []
    probe = out_dir.parent
    while not probe.exists():
        missing.append(probe)
        probe = probe.parent
    tmp = out_dir.parent / f".{out_dir.name}.tmp-{secrets.token_hex(4)}"
    committed = False
    try:
        try:
            out_dir.parent.mkdir(parents=True, exist_ok=True)
            tmp_pcb = input_prep._copy_project(src, tmp)
            board = PCB.load(tmp_pcb)
            original_counts = Counter(c.name for c in board._sexp.children if c.name)
            for node in added:
                board._sexp.append(node)
            write_layer.save_pcb(board, tmp_pcb)
            fresh = input_prep._parse(tmp_pcb)
            source_root = input_prep._parse(src)
            net_table_present = bool(_net_table(source_root))  # the ORIGINAL decides what could be cross-checked
        except Exception as exc:  # noqa: BLE001
            return _result("incomplete", f"the revision could not be built: {exc}", router=router_info)

        # Verify from a fresh parse of the saved file, never the in-memory tree.
        expected = original_counts + Counter(n.name for n in added)
        if Counter(c.name for c in fresh.children if c.name) != expected:
            return _result("error", "CRITICAL: top-level node counts are wrong after carry-over and reload")
        have = {_node_key(c): c for c in fresh.children if c.name in _CARRIED_TAGS}
        lost = [_node_key(n) for n in added if _node_key(n) not in have]
        if lost:
            return _result(
                "error", "CRITICAL: a carried node is missing after save and reload: " + ", ".join(lost)
            )
        # The whole file, exactly: the reload must be what was saved, and the original plus the
        # carried nodes (a net table PCB.load adds to a board without one is named, not hidden).
        write_check = _self_check.reload_differences(
            source=source_root, removed=[], written=added, memory=board._sexp, reloaded=fresh
        )
        if write_check["missing"] or write_check["unexpected"]:
            return _result(
                "error",
                "CRITICAL: the reloaded revision is not the intended write: " + _self_check.describe(write_check),
                write_check=_self_check.bounded(write_check),
            )

        final = gate.verify_board(tmp_pcb, **grade_args)
        if final["incomplete_reasons"]:
            return _result(
                "incomplete",
                "the carried revision's grade is incomplete: " + "; ".join(final["incomplete_reasons"]),
                router=router_info,
                candidate_grade=_gate_summary(cand_graded),
                revision_grade=_gate_summary(final),
            )
        if not _same_findings(cand_graded, final):
            return _result(
                "rejected",
                "the carried revision grades differently from the candidate that was accepted "
                "(the carry-over is not faithful); nothing was written",
                router=router_info,
                candidate_grade=_gate_summary(cand_graded),
                revision_grade=_gate_summary(final),
            )

        # Fill evidence and geometric reach, from a copy refilled by kicad-cli.
        added_zone_uuids = {(_zone_info(n) or {}).get("uuid") for n in added if n.name == "zone"}
        try:
            refilled = (fill or (lambda p: _refilled_copy(p, scratch)))(tmp_pcb)
            findings = _fill_findings(
                tmp_pcb, Path(refilled), added_zone_uuids, plane_layers, pads, min_fill_fraction
            )
        except Exception as exc:  # noqa: BLE001
            return _result(
                "incomplete",
                f"the zone fill could not be verified (kicad-cli refill failed): {exc}",
                router=router_info,
                revision_grade=_gate_summary(final),
            )
        reasons = list(findings["problems"])
        isolated_all = list(findings["isolated"])
        connectivity: dict = {"checked": False, "runs": 0, "note": "disabled (tests only)"}
        if connectivity_check and not reasons and not findings["isolated"] and not findings["unreachable_waiting"]:
            try:
                connectivity = _connectivity(
                    Path(refilled), scratch, pads, plane_layers, findings["rings"],
                    {z["uuid"] for z in findings["zones"]},
                    lambda variant: kicad_cli.run_drc(variant, severity_all=True, refill_zones=False),
                )
            except Exception as exc:  # noqa: BLE001
                return _result(
                    "incomplete",
                    f"connectivity could not be measured on the refilled board: {exc}",
                    router=router_info,
                    revision_grade=_gate_summary(final),
                )
            isolated_all += connectivity["isolated"]
            if connectivity["incomplete"] and not connectivity["isolated"]:
                return _result(
                    "incomplete",
                    connectivity["incomplete"],
                    router=router_info,
                    zones=findings["zones"],
                    connectivity=connectivity,
                    revision_grade=_gate_summary(final),
                )
        for pad in isolated_all + findings["unreachable_waiting"]:
            reasons.append(f"{pad['ref']} pad {pad['pad']} ({pad['net']}): {pad['reason']}")
        if reasons:
            return _result(
                "rejected",
                "the pour does not reach what it must reach: " + "; ".join(reasons),
                isolated_pads=isolated_all,
                unreachable_waiting_pads=findings["unreachable_waiting"],
                connectivity=connectivity,
                zones=findings["zones"],
                router=router_info,
                candidate_grade=_gate_summary(cand_graded),
                revision_grade=_gate_summary(final),
            )
        proven = set(connectivity.get("proven", []))
        pad_verdicts = []
        for i, pad in enumerate(pads):
            if pad["net"] not in plane_layers:
                continue
            layer = plane_layers[pad["net"]]
            if layer not in pad["layers"]:
                verdict, why = "unproven", (
                    "waits for a via from the routing step: inside the zone outline and over fill, "
                    "but the via is neither placed nor checked here"
                )
            elif i in proven:
                verdict, why = "proven", (
                    "kicad-cli found it connected to the new zone in the refilled board with every "
                    "other plane-net track, via and zone removed (only copper attached to the pad kept)"
                )
            else:
                verdict, why = "unproven", (
                    "connectivity check disabled (tests only)"
                    if not connectivity.get("checked")
                    else "not proven within the connectivity run cap"
                )
            pad_verdicts.append({**_pad_report(pad, why), "connectivity": verdict})
        revision_differences = _revision_differences(src, tmp_pcb, added)
        try:
            if out_dir.exists():
                out_dir.rmdir()  # an empty directory the caller pre-created
            write_layer.commit_directory(tmp, out_dir)
        except OSError as exc:
            return _result("incomplete", f"could not move the verified revision into place: {exc}")
        committed = True
    finally:
        if not committed:
            shutil.rmtree(tmp, ignore_errors=True)
            for directory in missing:
                try:
                    directory.rmdir()
                except OSError:
                    break

    out_pcb = out_dir / src.name
    return {
        "status": "poured",
        "output_pcb": str(out_pcb),
        "revision_sha256": _sha256(out_pcb),
        "zones": findings["zones"],
        "carried": {t: sum(1 for n in added if n.name == t) for t in _CARRIED_TAGS},
        "gate": _gate_summary(final),
        "pending_pads": assessment["pending_pads"],
        "original_grade": _gate_summary(before),
        "pads": pad_verdicts,
        "connectivity": connectivity,
        "revision_differences": revision_differences,
        "write_check": _self_check.bounded(write_check),
        "proves": PROOF_STATEMENT,
        "net_check": {
            "net_table": bool(net_table_present),
            "note": (
                "net ids and names of carried nodes were cross-checked against the net table"
                if net_table_present
                else "this file format has no net table (KiCad 10): carried nodes were checked "
                "by name against the board's pad, zone and track names; an id given next to a "
                "name, as in (net 2 \"GND\"), cannot be cross-checked here"
            )
            + ". The revision is a kicad_tools re-serialisation of the original (whitespace and "
            "layout differ, and a net table may be added) with the carried zones, vias and tracks "
            "appended: see revision_differences for what differs semantically.",
        },
        "fill_fraction_note": (
            "fill_fraction counts every fill polygon, islands included; whether pads are "
            "connected is decided separately by kicad-cli (connectivity)"
        ),
        "candidate_grade": _gate_summary(cand_graded),
        "router": router_info,
        "fill_note": (
            "The revision on disk holds the zone OUTLINE only (filled_in_file is false): the fill "
            "copper above was produced by kicad-cli in a refilled copy and measured there. KiCad "
            "refills on open and the gate refills in its graded copy; any export that needs fill "
            "copper must refill first."
        ),
        "note": (
            "A new revision with the pour was written; the original is untouched. The gate grade "
            "of this revision is in 'gate' (signals still open on the board are listed there). "
            "Call verify_board on output_pcb for a verdict bound to the file."
        ),
    }
