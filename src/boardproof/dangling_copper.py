"""Remove dangling vias and track stubs after a router import (propose/confirm; issue 246).

Detection is KiCad's own, not ours: ``kicad-cli pcb drc`` reports ``track_dangling`` ("Track has
unconnected end") and ``via_dangling`` for exactly the copper whose end touches nothing on its net, and
a track or via that joins two copper items is never reported. Our code only resolves each reported
uuid against the raw board (net, position and layer come from the raw ``(start)``, ``(end)``, ``(at)``,
``(layer)`` and ``(net)`` nodes, never from a convenience property), refuses locked copper and writes the
new revision through the write layer. The KiCadRoutingTools check ``_check_kicad_dangling`` (MIT) models
the same KiCad rule in Python for one shape only and says nothing for the rest; it was read, not copied.

Self-check (docs/dispatch/fail-open-checklist.md, item 14): the saved copy is parsed again; exactly the
listed items are gone and every other top-level node is unchanged; then DRC runs once more on the copy
and no unconnected pair may exist afterwards that did not exist before (compared as a set of endpoint uuids per net; KiCad may name other endpoints and cause a refusal, never a false pass). Removing a stub can leave the next item of a
chain with a free end: the proposal plans on scratch copies for up to ``MAX_ROUNDS`` rounds and lists the whole chain;
``dangling_after`` reports what is still dangling (locked or grouped copper).

Test seam ``drc`` is a keyword of the Python functions only; the tool layer must not expose it.
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path
from typing import Any

from boardproof import checkpoint, kicad_cli, write_layer
from boardproof.placement_anchor import is_locked
from boardproof.raw_geometry import RawGeometryError, net_name, net_table

_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())
_DRIFT = "the board changed since the proposal was made: re-run propose_dangling_copper_removal"
_KEPT = "nothing was written and the revision_token is still valid"
_MAX_COPY_BYTES = 512 * 1024 * 1024
_TYPES = {"track_dangling": ("segment", "arc"), "via_dangling": ("via",)}
_COPPER = ("segment", "arc", "via")

DANGLING_SENTENCE = (
    "Dangling copper is what KiCad's own DRC reports as track_dangling or via_dangling: a track end or a via that "
    "touches nothing on its net (a track or via that joins two copper items is never reported). Only an unlocked "
    "top-level track, arc or via is removed; locked copper stays, listed with its reason. The confirm reloads the "
    "written copy, checks that exactly the listed items are gone and that a second kicad-cli DRC run shows no "
    "unconnected pair that was not there before (it refuses rather than guess when KiCad names other endpoints). "
    "A chain whose end goes leaves its next item dangling; the proposal lists such items whole (rounds), and "
    "group members are kept. A chain longer than six rounds is cut off: the proposal then says truncated true and the confirm refuses it."
)


def _num(node: Any, name: str) -> list[float]:
    child = node.find(name)
    if child is None or not child.values:
        raise RawGeometryError(f"{node.name} has no ({name} ...)")
    return [float(v) for v in child.values]


def _uuid(node: Any) -> str:
    child = node.find("uuid")
    return str(child.values[0]) if child is not None and child.values else ""


def _drc(drc: Any, pcb: Path, refill: bool) -> dict:
    return (drc or kicad_cli.run_drc)(pcb, refill_zones=refill)


def _describe(node: Any, table: dict[int, str]) -> dict:
    if node.name == "via":
        return {
            "kind": "via",
            "net": net_name(node, table),
            "at": _num(node, "at")[:2],
            "layers": [str(v) for v in node.find("layers").values],
        }
    return {
        "kind": "track" if node.name == "segment" else "arc",
        "net": net_name(node, table),
        "layer": str(node.find("layer").values[0]),
        "start": _num(node, "start")[:2],
        "end": _num(node, "end")[:2],
    }


def _net_of_uuids(root: Any, table: dict[int, str]) -> dict[str, str]:
    """uuid -> net name of every pad, track, via and zone in the tree (an unknown net form raises)."""
    found: dict[str, str] = {}
    stack = list(root.children)
    while stack:
        node = stack.pop()
        if node.is_atom:
            continue
        if node.name in ("pad", "segment", "arc", "via", "zone") and node.find("net") is not None:
            uid = _uuid(node)
            if uid:
                found[uid] = net_name(node, table)
        stack.extend(node.children)
    return found


def _unconnected_pairs(report: dict, nets: dict[str, str]) -> set[tuple]:
    """Every unconnected pair of the report as ``(net, sorted endpoint uuids)``. A count per net would let a
    pair that vanishes with a removed floating stub hide a pair the removal opens elsewhere on the same net."""
    pairs: set[tuple] = set()
    for entry in report.get("unconnected_items", []):
        items = entry.get("items", [])
        named = [nets[i["uuid"]] for i in items if i.get("uuid") in nets]
        ends = tuple(sorted(str(i.get("uuid") or i.get("pos")) for i in items))
        pairs.add((named[0] if named else "(unknown)", ends))
    return pairs


def _plan(pcb: Path, drc: Any) -> dict:
    root = write_layer.load(pcb)  # a full parse; an unknown net form raises before KiCad is asked anything
    table = net_table(root)
    nets = _net_of_uuids(root, table)
    copper: dict[str, Any] = {}
    for node in root.children:
        if not node.is_atom and node.name in _COPPER:
            uid = _uuid(node)
            if not uid or uid in copper:
                raise RawGeometryError(f"a {node.name} has no uuid or shares it with another track or via")
            copper[uid] = node
    members: set[str] = set()  # a group names its members by uuid: a removed uuid would leave a dangling reference
    stack = list(root.children)
    while stack:
        node = stack.pop()
        if not node.is_atom:
            if node.name == "members":
                members.update(str(v) for v in node.values)
            stack.extend(node.children)
    refill = any(n.name == "zone" for n in root.children if not n.is_atom)  # a track may end on a fill
    report = _drc(drc, pcb, refill)
    ignored = {c.get("key") for c in report.get("ignored_checks", [])} & set(_TYPES)
    if ignored:
        raise _Blocked(f"the board's rule severities ignore {sorted(ignored)}: KiCad would not report the copper")
    removable: list[dict] = []
    kept: list[dict] = []
    seen: set[str] = set()
    for violation in report.get("violations", []):
        kinds = _TYPES.get(violation.get("type"))
        if kinds is None:
            continue
        for item in violation.get("items", []):
            uid = item.get("uuid", "")
            if uid in seen:
                continue
            seen.add(uid)
            node = copper.get(uid)
            if node is None or node.name not in kinds:
                kept.append({"uuid": uid, "reason": "KiCad names an item that is not a top-level track or via of this board"})
                continue
            entry = {"uuid": uid, **_describe(node, table), "reason": violation.get("description", violation["type"])}
            if is_locked(node):
                kept.append({**entry, "reason": f"it is locked ({entry['reason']})"})
            elif uid in members:
                kept.append({**entry, "reason": f"it is a group member ({entry['reason']})"})
            else:
                removable.append(entry)
    removable.sort(key=lambda c: (c["net"], c["uuid"]))
    return {
        "removable": removable,
        "kept": sorted(kept, key=lambda c: (c.get("net", ""), c["uuid"])),
        "unconnected": _unconnected_pairs(report, nets),
        "dangling": len(seen),
        "refill": refill,
    }


class _Blocked(Exception):
    pass


MAX_ROUNDS = 6


def _remove(pcb: Path, gone: set[str]) -> tuple[Any, list[str]]:
    """Drop the top-level tracks and vias with a uuid in ``gone`` from ``pcb`` (write layer, low-level path);
    returns the expected compact text of every other node, taken before the save."""
    root = write_layer.load(pcb)
    drop = {id(n) for n in root.children if not n.is_atom and n.name in _COPPER and _uuid(n) in gone}
    if len(drop) != len(gone):
        raise checkpoint.Failed("error", f"expected to remove {len(gone)} item(s) and found {len(drop)}: nothing was written")
    expected = _snapshot(root, gone)
    root.children[:] = [c for c in root.children if id(c) not in drop]
    write_layer.save(root, pcb)
    return root, expected


def _plan_rounds(pcb: Path, drc: Any, work_parent: Path) -> dict:
    """``_plan`` repeated on scratch copies with the copper found so far removed, so that a chain (a stub whose
    removal leaves its via with a free end) is listed whole: removing a part of a chain would leave the rest
    named by a new unconnected pair, which the self-check refuses. At most ``MAX_ROUNDS`` rounds."""
    first = _plan(pcb, drc)
    removable = [{**c, "round": 1} for c in first["removable"]]
    kept = list(first["kept"])
    latest = first
    truncated = False
    for number in range(2, MAX_ROUNDS + 2):  # the plan after the last round is detection only
        if not latest["removable"]:
            break
        work = Path(tempfile.mkdtemp(prefix=".dangling-round-", dir=work_parent if work_parent.is_dir() else None))
        try:
            copy = checkpoint.copy_project(pcb, work / "p", skip_suffixes=(".kicad_net",), max_bytes=_MAX_COPY_BYTES)
            _remove(copy, {c["uuid"] for c in removable})
            latest = _plan(copy, drc)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if number > MAX_ROUNDS:
            truncated = bool(latest["removable"])  # more copper after the allowed rounds: the chain goes on
            break
        known = {c["uuid"] for c in kept}
        removable += [{**c, "round": number} for c in latest["removable"]]
        kept += [c for c in latest["kept"] if c["uuid"] not in known]
    first["removable"] = sorted(removable, key=lambda c: (c["net"], c["uuid"]))
    first["kept"] = sorted(kept, key=lambda c: (c.get("net", ""), c["uuid"]))
    first["rounds"] = max([c["round"] for c in removable] or [1])
    first["truncated"] = truncated
    return first


def propose_dangling_copper_removal(pcb_path: str | Path, output_dir: str | Path | None = None, *, drc: Any = None) -> dict:
    """Dry run: lists each dangling track and via that would go (net, position, layer, reason) and the
    ones that stay with the reason. Writes nothing."""
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    resolved = src.resolve()
    out_dir = Path(output_dir) if output_dir else resolved.parent.parent / f"{resolved.parent.name}_dangling_removed"
    problem = checkpoint.output_dir_problem(src, out_dir, what="the dangling copper removal")
    if problem:
        return {"status": "error", "reason": problem}
    try:
        plan = _plan_rounds(src, drc, out_dir.parent)
    except (_Blocked, RawGeometryError) as exc:
        return {"status": "blocked", "revision_token": None, "reason": f"the board cannot be read safely: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"could not plan the removal: {exc}"}
    base = {"dangling_removed": plan["removable"], "kept": plan["kept"]}
    if not plan["removable"]:
        return {"status": "nothing_to_do", "revision_token": None, "reason": "no removable dangling track or via", **base}
    try:
        token = _PENDING.issue(src, {"out_dir": str(out_dir), "uuids": sorted(c["uuid"] for c in plan["removable"])})
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        "revision_token": token,
        "rounds": plan["rounds"],
        "truncated": plan["truncated"],
        "output_pcb": str(out_dir / src.name),
        "note": "creates a new board revision; the original is untouched. Call confirm_dangling_copper_removal to apply.",
        **base,
    }


def _snapshot(root: Any, skip: set[str]) -> list[str]:
    """Compact text of every top-level node but the ones with a uuid in ``skip``, in order."""
    return [write_layer.serialise(n, compact=True) for n in root.children if n.is_atom or _uuid(n) not in skip or n.name not in _COPPER]


def _write(src: Path, entry: dict, tmp: Path, drc: Any) -> dict:
    tmp_pcb = checkpoint.copy_project(
        src, tmp, outputs=(f"{src.parent.name}_dangling_removed",), skip_suffixes=(".kicad_net",), max_bytes=_MAX_COPY_BYTES
    )
    try:
        plan = _plan_rounds(tmp_pcb, drc, tmp.parent)  # against the copy: one consistent snapshot
    except (_Blocked, RawGeometryError) as exc:
        raise checkpoint.Failed("blocked", f"the board cannot be read safely: {exc}") from exc
    if sorted(c["uuid"] for c in plan["removable"]) != sorted(entry["uuids"]):
        raise checkpoint.Failed(
            "error",
            "the dangling copper changed since the proposal was made: re-run propose_dangling_copper_removal",
            note="nothing was written; the proposal is stale and its revision_token is spent",
            stale=True,
        )
    gone = set(entry["uuids"])
    _, expected = _remove(tmp_pcb, gone)
    return {"pcb": tmp_pcb, "gone": sorted(gone), "expected": expected, "before": plan["unconnected"], "refill": plan["refill"], "drc": drc}


def _verify(tmp: Path, state: dict) -> dict:
    """From a fresh parse and a fresh DRC run of the saved file: exactly the planned items are gone,
    every other top-level node is unchanged, and no unconnected pair is new."""
    pcb: Path = state["pcb"]
    name = pcb.name
    try:
        fresh = write_layer.load(pcb)
        table = net_table(fresh)
        nets = _net_of_uuids(fresh, table)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: file failed to re-parse: {exc}", [checkpoint.mismatch(name, "board", "a board that parses", str(exc))]
        ) from exc
    left = sorted({_uuid(n) for n in fresh.children if not n.is_atom and n.name in _COPPER} & set(state["gone"]))
    if left:
        raise checkpoint.NotVerified(
            f"CRITICAL: listed item(s) still on the board: {left}", [checkpoint.mismatch(name, "removed items", "gone", left)]
        )
    if _snapshot(fresh, set()) != state["expected"]:
        raise checkpoint.NotVerified(
            "CRITICAL: the board after reload is not exactly the source minus the listed items",
            [checkpoint.mismatch(name, "other top-level nodes", len(state["expected"]), len(_snapshot(fresh, set())))],
        )
    try:
        report = _drc(state["drc"], pcb, state["refill"])
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"the connectivity check could not run on the written copy: {exc}",
            [checkpoint.mismatch(name, "kicad-cli DRC", "a report", str(exc))],
        ) from exc
    after = _unconnected_pairs(report, nets)
    opened = sorted(after - state["before"])
    if opened:
        raise checkpoint.NotVerified(
            f"CRITICAL: {len(opened)} unconnected pair(s) are new after the removal (first: net {opened[0][0]}, "
            f"{' and '.join(opened[0][1])}); refused, nothing is written (a pair KiCad now names differently is refused too)",
            [checkpoint.mismatch(name, f"unconnected pair on {n}", "absent before", " / ".join(e)) for n, e in opened],
        )
    dangling = {i.get("uuid") for v in report.get("violations", []) if v.get("type") in _TYPES for i in v.get("items", [])}
    return {"items_removed": len(state["gone"]), "dangling_after": len(dangling), "unconnected_after": len(after)}


def confirm_dangling_copper_removal(pcb_path: str | Path, revision_token: str, *, drc: Any = None) -> dict:
    """Apply a proposal onto a new revision, reload it, run DRC on it and verify before it is moved into place."""
    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    out_dir = Path(entry["out_dir"])
    problem = checkpoint.output_dir_problem(src, out_dir, what="the dangling copper removal")
    if problem:
        return {"status": "error", "reason": problem}
    built = checkpoint.build_revision(
        out_dir,
        source_pcb=src,
        write=lambda tmp: _write(src, entry, tmp, drc),
        verify=_verify,
        success="removed",
        before_verified="dangling copper removal failed before a revision was verified",
        note=_KEPT,
    )
    if built.get("stale"):
        _PENDING.spend(revision_token)
    if built["status"] == "removed":
        _PENDING.spend(revision_token)
        built["output_pcb"] = str(out_dir / src.name)
        built["verified"] = "reloaded from disk: the listed items are gone, nothing else changed, no unconnected pair is new"
        built["note"] = "This is not a routing or done claim: only verify_board produces one."
    return built
