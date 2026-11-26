"""Lock or unlock a whole KiCad group (propose/confirm; issue 254).

A group is locked in two places: its own ``(locked yes)`` token and the ``(locked yes)`` of each member
footprint (``placement_anchor.set_locked``, the same write ``lock_footprints`` makes). This pair sets both
for the groups named by name or uuid. Members are read with ``board_groups`` (one reader, extended with
``resolve``): a group that holds a nested group, a member uuid that matches nothing, or whose name is shared
by several groups, is refused whole and listed, never partly changed. Other members (tracks, vias, text) keep
their own state; they are counted in the proposal so the gap is visible.

Self-check: the saved revision is parsed again; every top-level node outside the changed footprints and groups
is identical, each changed node is identical but for its locked token, and each carries exactly the requested
lock state (so positions, nets, member sets and other groups cannot differ).

Test seam ``_clock`` only; the tool layer exposes no seam.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from boardproof import board_groups, checkpoint, placement_anchor, raw_geometry, write_layer

_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())
_DRIFT = "the board changed since the proposal was made: re-run propose_group_lock"
_KEPT = "nothing was written and the revision_token is still valid"
_MAX_COPY_BYTES = 512 * 1024 * 1024

GROUP_LOCK_SENTENCE = (
    "Sets or clears the lock on whole groups, named by group name or uuid: the group's own lock flag and the lock "
    "of each member footprint, nothing else (positions, nets and member lists stay). A group that holds a nested "
    "group, names a member that is not on the board, or shares its name with another group is refused and listed "
    "with the reason. Tracks, vias and text in a group keep their own state. The confirm reloads the written copy "
    "and checks that only those lock flags changed."
)


def _lock_token(node: Any) -> list[Any]:
    return [c for c in node.children if (c.is_atom and c.value == "locked") or (not c.is_atom and c.name == "locked")]


def _stripped(node: Any) -> str:
    """Compact text of a node without its locked token."""
    drop = {id(c) for c in _lock_token(node)}
    return write_layer.serialise(write_layer.SExp.list(node.name, *[c for c in node.children if id(c) not in drop]), compact=True)


def _set_group_locked(group: Any, locked: bool) -> None:
    for token in _lock_token(group):
        group.children.remove(token)
    if locked:
        at = next((i for i, c in enumerate(group.children) if not c.is_atom and c.name == "members"), len(group.children))
        group.children.insert(at, write_layer.SExp.list("locked", write_layer.symbol("yes")))


def _plan(pcb: Path, groups: list[str], locked: bool) -> dict:
    root = write_layer.load(pcb)
    entries = board_groups.resolve(root)
    plan: list[dict] = []
    refused: list[dict] = []
    for ident in dict.fromkeys(groups):
        hits = [e for e in entries if e["uuid"] == ident] or [e for e in entries if e["name"] == ident]
        if len(hits) != 1:
            why = "no group with this name or uuid" if not hits else f"{len(hits)} groups share this name: use a uuid"
            refused.append({"group": ident, "reason": why})
            continue
        g = hits[0]
        if g["nested_groups"]:
            refused.append({"group": ident, "reason": f"it holds nested group(s) {g['nested_groups']}: name them one by one"})
        elif g["unresolved"]:
            refused.append({"group": ident, "reason": f"member uuid(s) {g['unresolved']} match nothing on the board"})
        elif not g["uuid"]:
            refused.append({"group": ident, "reason": "the group has no uuid in a recognised form"})
        else:
            fps = [f for f in g["footprint_nodes"] if placement_anchor.is_locked(f) != locked]
            plan.append(
                {
                    "group": g["name"],
                    "uuid": g["uuid"],
                    "group_flag_changes": placement_anchor.is_locked(g["node"]) != locked,
                    "footprints": sorted(raw_geometry._reference(f) for f in fps),
                    "footprint_uuids": sorted(raw_geometry.direct_uuid(f) for f in fps),
                    "other_members_unchanged": g["other_members"],
                }
            )
    return {"groups": plan, "refused": refused}


def _changes(plan: dict) -> bool:
    return any(g["group_flag_changes"] or g["footprints"] for g in plan["groups"])


def _digest(plan: dict, locked: bool) -> str:
    return hashlib.sha256(repr((locked, [(g["uuid"], g["group_flag_changes"], g["footprint_uuids"]) for g in plan["groups"]])).encode()).hexdigest()


def _public(plan: dict) -> dict:
    return {"groups": [{k: v for k, v in g.items() if k != "footprint_uuids"} for g in plan["groups"]], "refused": plan["refused"]}


def propose_group_lock(pcb_path: str | Path, groups: list[str], locked: bool = True, output_dir: str | Path | None = None) -> dict:
    """Dry run: per group the footprints whose lock flag would change. Writes nothing."""
    src = Path(pcb_path)
    if not isinstance(locked, bool):
        return {"status": "error", "reason": f"locked must be true or false, got {locked!r}"}
    if not isinstance(groups, list) or not groups or not all(isinstance(g, str) and g for g in groups):
        return {"status": "error", "reason": "groups must be a non-empty list of group names or uuids"}
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    resolved = src.resolve()
    out_dir = Path(output_dir) if output_dir else resolved.parent.parent / f"{resolved.parent.name}_group_{'locked' if locked else 'unlocked'}"
    if problem := checkpoint.output_dir_problem(src, out_dir, what="the group lock"):
        return {"status": "error", "reason": problem}
    try:
        plan = _plan(src, groups, locked)
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"could not plan the group lock: {exc}"}
    if not _changes(plan):
        return {"status": "nothing_to_do", "revision_token": None, "reason": "no listed group would change", **_public(plan)}
    try:
        token = _PENDING.issue(src, {"out_dir": str(out_dir), "groups": list(groups), "locked": locked, "digest": _digest(plan, locked)})
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        "revision_token": token,
        "locked": locked,
        "output_pcb": str(out_dir / src.name),
        "note": "creates a new board revision; the original is untouched. Call confirm_group_lock to apply.",
        **_public(plan),
    }


def _write(src: Path, entry: dict, tmp: Path) -> dict:
    pcb = checkpoint.copy_project(src, tmp, outputs=(Path(entry["out_dir"]).name,), skip_suffixes=(".kicad_net",), max_bytes=_MAX_COPY_BYTES)
    plan = _plan(pcb, entry["groups"], entry["locked"])  # against the copy: one consistent snapshot
    if _digest(plan, entry["locked"]) != entry["digest"]:
        raise checkpoint.Failed("error", _DRIFT, note="nothing was written; the proposal is stale and its revision_token is spent", stale=True)
    root = write_layer.load(pcb)
    before = [write_layer.serialise(n, compact=True) for n in root.children]
    fp_ids = {u for g in plan["groups"] for u in g["footprint_uuids"]}
    group_ids = {g["uuid"] for g in plan["groups"] if g["group_flag_changes"]}
    changed: dict[int, str] = {}  # top-level index -> stripped text before
    for i, node in enumerate(root.children):
        if node.is_atom:
            continue
        uid = raw_geometry.direct_uuid(node)
        if node.name == "footprint" and uid in fp_ids:
            changed[i] = _stripped(node)
            placement_anchor.set_locked(node, entry["locked"])
        elif node.name == "group" and uid in group_ids:
            changed[i] = _stripped(node)
            _set_group_locked(node, entry["locked"])
    write_layer.save(root, pcb)
    return {"pcb": pcb, "before": before, "changed": changed, "locked": entry["locked"]}


def _verify(tmp: Path, state: dict) -> dict:
    pcb: Path = state["pcb"]
    try:
        fresh = write_layer.load(pcb)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: file failed to re-parse: {exc}", [checkpoint.mismatch(pcb.name, "board", "a board that parses", str(exc))]
        ) from exc
    bad: list[dict] = []
    if len(fresh.children) != len(state["before"]):
        bad.append(checkpoint.mismatch(pcb.name, "top-level nodes", len(state["before"]), len(fresh.children)))
    for i, (old, node) in enumerate(zip(state["before"], fresh.children, strict=False)):
        if i in state["changed"]:
            if _stripped(node) != state["changed"][i]:
                bad.append(checkpoint.mismatch(pcb.name, f"{node.name} {raw_geometry.direct_uuid(node)} outside its lock flag", "unchanged", "changed"))
            if placement_anchor.is_locked(node) != state["locked"]:
                bad.append(checkpoint.mismatch(pcb.name, f"{node.name} {raw_geometry.direct_uuid(node)} lock flag", state["locked"], not state["locked"]))
        elif write_layer.serialise(node, compact=True) != old:
            bad.append(checkpoint.mismatch(pcb.name, f"top-level node {i}", "unchanged", "changed"))
    if bad:
        raise checkpoint.NotVerified(
            f"CRITICAL: {len(bad)} difference(s) between the intended lock change and the saved file (first: {bad[0]['item']}); nothing is written", bad
        )
    return {"nodes_changed": len(state["changed"])}


def confirm_group_lock(pcb_path: str | Path, revision_token: str) -> dict:
    """Apply a proposal onto a new board revision, reload it and verify before it is moved into place."""
    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    out_dir = Path(entry["out_dir"])
    if problem := checkpoint.output_dir_problem(src, out_dir, what="the group lock"):
        return {"status": "error", "reason": problem}
    built = checkpoint.build_revision(
        out_dir,
        source_pcb=src,
        write=lambda tmp: _write(src, entry, tmp),
        verify=_verify,
        success="locked" if entry["locked"] else "unlocked",
        before_verified="group lock failed before a revision was verified",
        note=_KEPT,
    )
    if built.get("stale") or built["status"] in ("locked", "unlocked"):
        _PENDING.spend(revision_token)
    if built["status"] in ("locked", "unlocked"):
        built["output_pcb"] = str(out_dir / src.name)
        built["verified"] = "reloaded from disk: only the lock flags of the listed groups and their footprints changed"
        built["note"] = "This is not a routing or done claim: only verify_board produces one."
    return built
