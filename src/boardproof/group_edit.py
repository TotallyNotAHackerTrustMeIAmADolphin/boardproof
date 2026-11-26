"""Move a whole KiCad group and create a new one (propose/confirm pairs; issue 255, slice 4 of 231).

``move_group``: every member footprint of the named groups moves by one offset (dx, dy), through
``placement_anchor.write_positions`` (the translation write ``place_footprints`` uses). A group that holds a
nested group, a member uuid that matches nothing, a member that is not a footprint (track, via, text), a
locked member footprint or a locked group flag, no footprint at all, or a footprint whose reference is empty
or shared on the board is refused whole and listed, never partly moved. Tracks, vias and text are refused
rather than moved: moving them needs a second write whose net and clearance consequences are not checked here.

``create_group``: appends one ``(group "name" (uuid ..) (members ..))`` node whose members are footprints named
by reference. Refused: an empty name, a name already used by a group, an unknown, shared or repeated
reference, a footprint that already belongs to a group (KiCad allows one group per item).

Self-check of both: the saved revision is parsed again and compared node by node with the intended change
(move: each member footprint equals the original but for its raw ``(at x y)``, which is the original plus the
offset with the same angle, and its pads' absolute positions follow by the same offset; create: exactly one
new top-level node, equal to the intended one, and every other node identical).

Reused: ``board_groups.resolve`` (the one group reader), ``checkpoint`` (Pending, build_revision), the
``group_lock`` shape, ``placement_anchor.write_positions``, ``raw_geometry`` readers. Test seam ``_clock`` only.
"""

from __future__ import annotations

import hashlib
import math
import time
import uuid
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from boardproof import board_groups, checkpoint, placement_anchor, raw_geometry, write_layer

_clock = time.time  # replaced in tests
_MOVE = checkpoint.Pending(clock=lambda: _clock())
_CREATE = checkpoint.Pending(clock=lambda: _clock())
_DRIFT = "the board changed since the proposal was made: propose again"
_KEPT = "nothing was written and the revision_token is still valid"
_MAX_COPY_BYTES = 512 * 1024 * 1024
_TOL = 2e-6  # mm; positions are written with 6 decimals
_NOT_A_CLAIM = "This is not a routing or done claim: only verify_board produces one."

MOVE_GROUP_SENTENCE = (
    "The pair moves every member footprint of the named groups (group names or uuids, see list_footprint_groups) by one "
    "offset dx, dy in mm: only each footprint's (at x y) changes, so its angle and its pad positions relative to the "
    "footprint stay. A group that holds a nested group, a member that is not on the board, a member that is not a "
    "footprint (track, via, text), a locked member footprint, a locked group, or a footprint whose reference is empty or "
    "shared is refused and listed with the reason, never partly moved. The confirm reloads the written copy and checks "
    "that only the member footprints' (at) changed, by exactly dx, dy. No collision, cutout or board-edge check is made: "
    "run pcb_check_placement on the new revision after a move. A footprint on the board with neither courtyard nor pad "
    "(a logo) is listed under not_measured and does not stop the move; a member footprint with neither is refused and "
    "listed, never moved."
)
CREATE_GROUP_SENTENCE = (
    "Adds one new group with a fresh uuid whose members are the footprints named by reference. An empty name, a name "
    "that a group already has, an unknown, shared or repeated reference, and a footprint that already belongs to a "
    "group are refused and listed. The confirm reloads the written copy and checks that exactly one top-level node "
    "was added, equal to the proposed group, and every other node is identical."
)


# --------------------------------------------------------------------------- shared flow


def _propose(
    pending: checkpoint.Pending, pcb_path: str | Path, output_dir: str | Path | None, tag: str, what: str,
    plan_fn: Callable[[Path, dict], dict], args: dict, public: Callable[[dict], dict],
) -> dict:
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    resolved = src.resolve()
    out_dir = Path(output_dir) if output_dir else resolved.parent.parent / f"{resolved.parent.name}_{tag}"
    if problem := checkpoint.output_dir_problem(src, out_dir, what=what):
        return {"status": "error", "reason": problem}
    try:
        plan = plan_fn(src, args)
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"could not plan {what}: {exc}"}
    if not plan["changes"]:
        word = "refused" if plan["refused"] else "nothing_to_do"
        return {"status": word, "revision_token": None, "reason": f"nothing would change for {what}", **public(plan)}
    try:
        token = pending.issue(src, {"out_dir": str(out_dir), "args": args, "digest": plan["digest"]})
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        "revision_token": token,
        "output_pcb": str(out_dir / src.name),
        "note": "creates a new board revision; the original is untouched. Call the confirm tool to apply.",
        **public(plan),
    }


def _copy_and_plan(src: Path, entry: dict, tmp: Path, plan_fn: Callable[[Path, dict], dict]) -> tuple[Path, dict]:
    pcb = checkpoint.copy_project(src, tmp, outputs=(Path(entry["out_dir"]).name,), skip_suffixes=(".kicad_net",), max_bytes=_MAX_COPY_BYTES)
    plan = plan_fn(pcb, entry["args"])  # against the copy: one consistent snapshot
    if plan["digest"] != entry["digest"]:
        raise checkpoint.Failed("error", _DRIFT, note="nothing was written; the proposal is stale and its revision_token is spent", stale=True)
    return pcb, plan


def _confirm(
    pending: checkpoint.Pending, pcb_path: str | Path, revision_token: str, what: str, success: str,
    write: Callable[[Path, dict, Path], Any], verify: Callable[[Path, Any], dict], proof: str,
) -> dict:
    src = Path(pcb_path)
    entry, refused = pending.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    out_dir = Path(entry["out_dir"])
    if problem := checkpoint.output_dir_problem(src, out_dir, what=what):
        return {"status": "error", "reason": problem}
    built = checkpoint.build_revision(
        out_dir, source_pcb=src, write=lambda tmp: write(src, entry, tmp), verify=verify, success=success,
        before_verified=f"{what} failed before a revision was verified", note=_KEPT,
    )
    if built.get("stale") or built["status"] == success:
        pending.spend(revision_token)
    if built["status"] == success:
        built["output_pcb"] = str(out_dir / src.name)
        built["verified"] = proof
        built["note"] = _NOT_A_CLAIM
    return built


def _find(entries: list[dict], ident: str) -> tuple[dict | None, str]:
    hits = [e for e in entries if e["uuid"] == ident] or [e for e in entries if e["name"] == ident]
    if len(hits) == 1:
        return hits[0], ""
    return None, "no group with this name or uuid" if not hits else f"{len(hits)} groups share this name: use a uuid"


def _top(root: Any, name: str) -> list[Any]:
    return [c for c in root.children if not c.is_atom and c.name == name]


def _hash(value: Any) -> str:
    return hashlib.sha256(repr(value).encode()).hexdigest()


# --------------------------------------------------------------------------- move_group


def _move_refusal(g: dict, refs: Counter) -> str:
    fps = g["footprint_nodes"]
    names = [raw_geometry._reference(f) for f in fps]
    if g["nested_groups"]:
        return f"it holds nested group(s) {g['nested_groups']}: name them one by one"
    if g["unresolved"]:
        return f"member uuid(s) {g['unresolved']} match nothing on the board"
    if g["other_members"]:
        return f"it has members that are not footprints {g['other_members']}: tracks, vias and text are not moved by this tool"
    if not g["uuid"]:
        return "the group has no uuid in a recognised form"
    if not fps:
        return "the group has no footprint member"
    if placement_anchor.is_locked(g["node"]):
        return "the group is locked: unlock it with propose_group_lock first"
    if locked := sorted(n for f, n in zip(fps, names, strict=True) if placement_anchor.is_locked(f)):
        return f"member footprint(s) {locked} are locked: unlock them first"
    if bad := sorted({n or "(empty reference)" for n in names if not n or refs[n] > 1}):
        return f"footprint reference(s) {bad} are empty or shared by several footprints on the board"
    return ""


def _plan_move(pcb: Path, a: dict) -> dict:
    root = write_layer.load(pcb)
    refs = Counter(raw_geometry._reference(f) for f in _top(root, "footprint"))
    not_measured: list[str] = []
    placement_anchor.read_parts(root, unmeasured=not_measured)  # the shape a move needs; those without are listed
    entries = board_groups.resolve(root)
    plan: list[dict] = []
    refused: list[dict] = []
    seen: set[str] = set()
    for ident in dict.fromkeys(a["groups"]):
        g, why = _find(entries, ident)
        why = why or _move_refusal(g, refs) if g else why
        if g and not why and (blind := sorted(n for n in (raw_geometry._reference(f) for f in g["footprint_nodes"]) if n in not_measured)):
            why = f"member footprint(s) {blind} have neither courtyard nor pad to measure: not moved"
        if why:
            refused.append({"group": ident, "reason": why})
        elif g["uuid"] not in seen:
            seen.add(g["uuid"])
            fps = []
            for f in g["footprint_nodes"]:
                x, y, _ = raw_geometry._at(f, "footprint " + raw_geometry._reference(f))
                fps.append({"reference": raw_geometry._reference(f), "uuid": raw_geometry.direct_uuid(f), "from": [x, y],
                            "to": [round(x + a["dx"], 6), round(y + a["dy"], 6)]})
            plan.append({"group": g["name"], "uuid": g["uuid"], "footprints": sorted(fps, key=lambda p: p["reference"])})
    key = (a["dx"], a["dy"], [(g["uuid"], [(p["uuid"], p["from"]) for p in g["footprints"]]) for g in plan])
    return {"groups": plan, "refused": refused, "changes": bool(plan), "digest": _hash(key), "not_measured": sorted(not_measured)}


def _move_public(plan: dict) -> dict:
    groups = [{"group": g["group"], "uuid": g["uuid"], "footprints": [{k: v for k, v in p.items() if k != "uuid"} for p in g["footprints"]]}
              for g in plan["groups"]]
    return {"groups": groups, "refused": plan["refused"], "not_measured": plan["not_measured"]}


def propose_move_group(pcb_path: str | Path, groups: list[str], dx: float, dy: float, output_dir: str | Path | None = None) -> dict:
    """Dry run: per group the member footprints with their old and new (at x y). Writes nothing."""
    if not isinstance(groups, list) or not groups or not all(isinstance(g, str) and g for g in groups):
        return {"status": "error", "reason": "groups must be a non-empty list of group names or uuids"}
    for name, v in (("dx", dx), ("dy", dy)):
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            return {"status": "error", "reason": f"{name} must be a finite number in mm, got {v!r}"}
    if dx == 0 and dy == 0:
        return {"status": "nothing_to_do", "revision_token": None, "reason": "dx and dy are both 0"}
    return _propose(_MOVE, pcb_path, output_dir, "group_moved", "the group move", _plan_move,
                    {"groups": list(groups), "dx": float(dx), "dy": float(dy)}, _move_public)


def _write_move(src: Path, entry: dict, tmp: Path) -> dict:
    pcb, plan = _copy_and_plan(src, entry, tmp, _plan_move)
    root = write_layer.load(pcb)
    moves = {p["uuid"]: p for g in plan["groups"] for p in g["footprints"]}
    before = [write_layer.serialise(n, compact=True) for n in root.children]
    refs = {p["reference"] for p in moves.values()}
    pads = {f.reference: [p.at_mm for p in f.pads] for f in raw_geometry.read_footprints(root, refs)}
    pad_angles = {f.reference: [p.angle_deg for p in f.pads] for f in raw_geometry.read_footprints(root, refs)}
    index = {raw_geometry.direct_uuid(n): i for i, n in enumerate(root.children) if not n.is_atom and n.name == "footprint"}
    moved = {}
    for u, p in moves.items():
        node = root.children[index[u]]
        moved[index[u]] = {"reference": p["reference"], "from": p["from"], "angle": raw_geometry._at(node, p["reference"])[2],
                           "stripped": _without_at(node), "pads": pads[p["reference"]], "pad_angles": pad_angles[p["reference"]]}
    placement_anchor.write_positions(pcb, {p["reference"]: tuple(p["to"]) for p in moves.values()})
    return {"pcb": pcb, "before": before, "moved": moved, "dx": entry["args"]["dx"], "dy": entry["args"]["dy"]}


def _without_at(node: Any) -> str:
    drop = {id(c) for c in node.children if not c.is_atom and c.name == "at"}
    return write_layer.serialise(write_layer.SExp.list(node.name, *[c for c in node.children if id(c) not in drop]), compact=True)


def _verify_move(tmp: Path, state: dict) -> dict:
    pcb: Path = state["pcb"]
    try:
        fresh = write_layer.load(pcb)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(f"CRITICAL: file failed to re-parse: {exc}", [checkpoint.mismatch(pcb.name, "board", "a board that parses", str(exc))]) from exc
    bad: list[dict] = []
    dx, dy = state["dx"], state["dy"]
    if len(fresh.children) != len(state["before"]):
        bad.append(checkpoint.mismatch(pcb.name, "top-level nodes", len(state["before"]), len(fresh.children)))
    for i, (old, node) in enumerate(zip(state["before"], fresh.children, strict=False)):
        m = state["moved"].get(i)
        if m is None:
            if write_layer.serialise(node, compact=True) != old:
                bad.append(checkpoint.mismatch(pcb.name, f"top-level node {i}", "unchanged", "changed"))
            continue
        ref = m["reference"]
        if _without_at(node) != m["stripped"]:
            bad.append(checkpoint.mismatch(pcb.name, f"{ref} outside its (at)", "unchanged", "changed"))
        x, y, angle = raw_geometry._at(node, ref)
        if abs(x - (m["from"][0] + dx)) > _TOL or abs(y - (m["from"][1] + dy)) > _TOL or angle != m["angle"]:
            bad.append(checkpoint.mismatch(pcb.name, f"{ref} raw (at)", [m["from"][0] + dx, m["from"][1] + dy, m["angle"]], [x, y, angle]))
    after = {f.reference: f for f in raw_geometry.read_footprints(fresh, {m["reference"] for m in state["moved"].values()})}
    for m in state["moved"].values():  # hard rule 2: the pads' absolute positions, read raw, follow by the same offset
        now = after.get(m["reference"])
        pads = [p.at_mm for p in now.pads] if now else []
        angles = [p.angle_deg for p in now.pads] if now else []
        same = len(pads) == len(m["pads"]) and all(abs(a[0] - (b[0] + dx)) <= _TOL and abs(a[1] - (b[1] + dy)) <= _TOL for a, b in zip(pads, m["pads"], strict=True))
        if not same or angles != m["pad_angles"]:
            bad.append(checkpoint.mismatch(pcb.name, f"{m['reference']} pad positions", "old plus the offset, same angles", "differ"))
    if bad:
        raise checkpoint.NotVerified(
            f"CRITICAL: {len(bad)} difference(s) between the intended move and the saved file (first: {bad[0]['item']}); nothing is written", bad
        )
    return {"footprints_moved": len(state["moved"])}


def confirm_move_group(pcb_path: str | Path, revision_token: str) -> dict:
    """Apply a proposal onto a new board revision, reload it and check it before it is moved into place."""
    return _confirm(_MOVE, pcb_path, revision_token, "the group move", "moved", _write_move, _verify_move,
                    "reloaded from disk: only the member footprints' (at) changed, each by exactly the offset, and their pads follow")


# --------------------------------------------------------------------------- create_group


def _plan_create(pcb: Path, a: dict) -> dict:
    root = write_layer.load(pcb)
    entries = board_groups.resolve(root)
    by_ref: dict[str, list[Any]] = {}
    for f in _top(root, "footprint"):
        by_ref.setdefault(raw_geometry._reference(f), []).append(f)
    owner = {raw_geometry.direct_uuid(f): e["name"] for e in entries for f in e["footprint_nodes"]}
    name, refs = a["name"], a["members"]
    refused: list[dict] = []
    try:
        write_layer.string(name)
    except Exception as exc:  # noqa: BLE001
        refused.append({"item": "name", "reason": f"the name cannot be written: {exc}"})
    if not name.strip():
        refused.append({"item": "name", "reason": "the name is empty"})
    elif any(e["name"] == name for e in entries):
        refused.append({"item": "name", "reason": f"a group named {name!r} already exists"})
    if dup := sorted(r for r, n in Counter(refs).items() if n > 1):
        refused.append({"item": "members", "reason": f"reference(s) {dup} are listed more than once"})
    ids: list[str] = []
    for ref in dict.fromkeys(refs):
        nodes = by_ref.get(ref, [])
        if len(nodes) != 1:
            refused.append({"item": ref, "reason": "no footprint with this reference" if not nodes else f"{len(nodes)} footprints share this reference"})
            continue
        uid = raw_geometry.direct_uuid(nodes[0])
        if not uid:
            refused.append({"item": ref, "reason": "the footprint has no uuid in a recognised form"})
        elif uid in owner:
            refused.append({"item": ref, "reason": f"it already belongs to group {owner[uid]!r}: a footprint can be in one group only"})
        else:
            ids.append(uid)
    group = {"name": name, "uuid": a["uuid"], "members": list(dict.fromkeys(refs))}
    return {"group": group, "member_uuids": ids, "refused": refused, "changes": not refused,
            "digest": _hash((name, a["uuid"], ids, len(entries)))}


def _create_public(plan: dict) -> dict:
    return {"group": plan["group"] if plan["changes"] else None, "refused": plan["refused"]}


def propose_create_group(pcb_path: str | Path, name: str, members: list[str], output_dir: str | Path | None = None) -> dict:
    """Dry run: the group that would be added. Writes nothing."""
    if not isinstance(name, str):
        return {"status": "error", "reason": "name must be a string"}
    if not isinstance(members, list) or not members or not all(isinstance(m, str) and m for m in members):
        return {"status": "error", "reason": "members must be a non-empty list of footprint references"}
    return _propose(_CREATE, pcb_path, output_dir, "group_created", "the new group", _plan_create,
                    {"name": name, "members": list(members), "uuid": str(uuid.uuid4())}, _create_public)


def _new_node(a: dict, ids: list[str]) -> Any:
    sx = write_layer.SExp
    return sx.list("group", write_layer.string(a["name"]), sx.list("uuid", write_layer.string(a["uuid"])),
                   sx.list("members", *[write_layer.string(u) for u in ids]))


def _write_create(src: Path, entry: dict, tmp: Path) -> dict:
    pcb, plan = _copy_and_plan(src, entry, tmp, _plan_create)
    root = write_layer.load(pcb)
    before = [write_layer.serialise(n, compact=True) for n in root.children]
    node = _new_node(entry["args"], plan["member_uuids"])
    expected = write_layer.serialise(node, compact=True)  # before the save, so nothing done to the node later can change it
    root.append(node)
    write_layer.save(root, pcb)
    return {"pcb": pcb, "before": before, "expected": expected, "name": entry["args"]["name"],
            "members": plan["group"]["members"]}


def _verify_create(tmp: Path, state: dict) -> dict:
    pcb: Path = state["pcb"]
    try:
        fresh = write_layer.load(pcb)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(f"CRITICAL: file failed to re-parse: {exc}", [checkpoint.mismatch(pcb.name, "board", "a board that parses", str(exc))]) from exc
    bad: list[dict] = []
    n = len(state["before"])
    if len(fresh.children) != n + 1:
        bad.append(checkpoint.mismatch(pcb.name, "top-level nodes", n + 1, len(fresh.children)))
    for i, (old, node) in enumerate(zip(state["before"], fresh.children, strict=False)):
        if write_layer.serialise(node, compact=True) != old:
            bad.append(checkpoint.mismatch(pcb.name, f"top-level node {i}", "unchanged", "changed"))
    if len(fresh.children) > n:
        if write_layer.serialise(fresh.children[n], compact=True) != state["expected"]:
            bad.append(checkpoint.mismatch(pcb.name, "the new group node", state["expected"], write_layer.serialise(fresh.children[n], compact=True)))
        found = [e for e in board_groups.resolve(fresh) if e["name"] == state["name"]]
        got = sorted(raw_geometry._reference(f) for e in found for f in e["footprint_nodes"])
        if len(found) != 1 or got != sorted(state["members"]) or found[0]["unresolved"] or found[0]["other_members"]:
            bad.append(checkpoint.mismatch(pcb.name, "group members as read back", sorted(state["members"]), got))
    if bad:
        raise checkpoint.NotVerified(
            f"CRITICAL: {len(bad)} difference(s) between the intended group and the saved file (first: {bad[0]['item']}); nothing is written", bad
        )
    return {"nodes_added": 1}


def confirm_create_group(pcb_path: str | Path, revision_token: str) -> dict:
    """Apply a proposal onto a new board revision, reload it and check it before it is moved into place."""
    return _confirm(_CREATE, pcb_path, revision_token, "the new group", "created", _write_create, _verify_create,
                    "reloaded from disk: exactly one top-level node was added, equal to the proposed group; every other node is identical")
