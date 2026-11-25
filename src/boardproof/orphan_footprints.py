"""Remove orphaned footprints from a board (propose/confirm; build ticket 227).

An orphan is a footprint whose reference has no symbol in the schematic (the kicad-cli netlist is
the only source, as in ``schematic_import``). Since ticket 224 the schematic import refuses while a
net hangs only on orphans; this pair removes them first, as a new revision beside the source.

Only a footprint that is not locked and has no copper is removed. "No copper" is read from the raw
board: no pad of it is on a net that carries a segment, arc, via or zone, and the footprint holds
no zone of its own. Anything else stays and is listed with its reason. Nothing is guessed: a pad net
that cannot be read blocks that footprint, a reference shared by two footprints blocks both.

Reused, not copied: the netlist load and schematic checks, the pad-net and copper readers of
``schematic_import``, ``placement_anchor.is_locked``, and the checkpoint helper
(``checkpoint.Pending``, ``build_revision``, ``copy_project``). Test seam ``netlist_exporter`` is a
keyword of the Python functions only; the tool layer must not expose it.
"""

from __future__ import annotations

import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from boardproof import checkpoint, write_layer
from boardproof import schematic_import as si
from boardproof.placement_anchor import is_locked
from boardproof.raw_geometry import RawGeometryError

_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())
_DRIFT = "the board or schematic changed since the proposal was made: re-run propose_orphan_footprint_removal"
_KEPT = "nothing was written and the revision_token is still valid"
_MAX_COPY_BYTES = 512 * 1024 * 1024
_COPPER = ("segment", "arc", "via", "zone")

ORPHAN_SENTENCE = (
    "Orphans are footprints whose reference has no symbol in the schematic. Only an unlocked "
    "footprint that is not a group member and none of whose pad nets carries a track, via or zone, "
    "also one inside another footprint (and that holds no zone of its own) is removed; every other orphan stays, listed with its reason. Net declarations are not "
    "touched: the schematic import drops unused ones."
)


def _footprints(root: Any) -> list[Any]:
    return [n for n in root.children if n.name == "footprint"]


def _plan(pcb: Path, sch: Path, netlist_exporter: Any) -> dict:
    netlist, raw, nodes = si._load_netlist(sch, netlist_exporter)
    si._check_schematic(sch, netlist, raw, nodes)
    root = write_layer.load(pcb)  # a full parse: the locked token and the copper must all be there
    components = {c.reference for c in netlist.components}
    fps = _footprints(root)
    count = Counter(si._reference_of(f) for f in fps)
    # every copper node anywhere in the tree: a zone inside another footprint carries a net too
    carriers: list[Any] = []
    members: set[str] = set()
    stack = list(root.children)
    while stack:
        node = stack.pop()
        if node.is_atom:
            continue
        if node.name in _COPPER:
            carriers.append(node)
        elif node.name == "members":
            members.update(str(v) for v in node.values)
        stack.extend(node.children)
    shim = SimpleNamespace(children=root.children, copper_children=lambda: carriers)
    pads = si._pad_nets(root)  # raises RawGeometryError on a net form it does not know
    removable: list[str] = []
    kept: dict[str, list[str]] = {}
    for fp in fps:
        ref = si._reference_of(fp)
        if ref in components:
            continue
        why: list[str] = []
        if not ref:
            why.append("it has no readable reference")
        if count[ref] > 1:
            why.append(f"{count[ref]} footprints share the reference {ref!r}")
        if is_locked(fp):
            why.append("it is locked")
        uuid = fp.find("uuid")
        if uuid is not None and uuid.values and str(uuid.values[0]) in members:
            why.append("it is a group member")
        if any(c.name == "zone" for c in fp.children):
            why.append("it holds a zone of its own")
        nets = {n for (r, _), ns in pads.items() if r == ref for n in ns} - {""}
        hits = si._copper_on_nets(shim, nets) if nets else []
        if hits:
            why.append(f"copper on its nets: {', '.join(hits[:5])}")
        if why:
            kept[ref or "(no reference)"] = why
        else:
            removable.append(ref)
    removable.sort(key=si._natural_key)
    return {"removable": removable, "kept": kept, "footprints": len(fps), "components": len(components)}


def propose_orphan_footprint_removal(
    pcb_path: str | Path,
    schematic_path: str | Path | None = None,
    output_dir: str | Path | None = None,
    *,
    netlist_exporter: Any = None,
) -> dict:
    """Dry run: lists the orphans that would go and the ones that stay with the reason. Writes nothing."""
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    sch = Path(schematic_path) if schematic_path else src.with_suffix(".kicad_sch")
    if not sch.is_file():
        return {"status": "incomplete", "reason": f"schematic not found: {sch}"}
    resolved = src.resolve()
    out_dir = Path(output_dir) if output_dir else resolved.parent.parent / f"{resolved.parent.name}_orphans_removed"
    problem = checkpoint.output_dir_problem(src, out_dir, what="the orphan removal")
    if problem:
        return {"status": "error", "reason": problem}
    try:
        plan = _plan(src, sch, netlist_exporter)
    except si._Blocked as exc:
        return {"status": "blocked", "revision_token": None, "reason": str(exc)}
    except RawGeometryError as exc:
        return {"status": "blocked", "revision_token": None, "reason": f"the net names on the board cannot be read: {exc}"}
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"could not plan the removal: {exc}"}
    base = {"orphans_removed": plan["removable"], "orphans_kept": plan["kept"]}
    if not plan["removable"]:
        return {"status": "nothing_to_do", "revision_token": None, "reason": "no removable orphaned footprint", **base}
    payload = {"sch": si._norm(sch), "out_dir": str(out_dir), "removable": plan["removable"]}
    try:
        token = _PENDING.issue(src, payload, extra_files=(sch,))
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        "revision_token": token,
        "output_pcb": str(out_dir / src.name),
        "note": "creates a new board revision; the original is untouched. Call confirm_orphan_footprint_removal to apply.",
        **base,
    }


def _snapshot(root: Any) -> tuple[list[str], list[str]]:
    """(compact text of every footprint in order, of every other top-level node in order)."""
    fp, rest = [], []
    for n in root.children:
        (fp if n.name == "footprint" else rest).append(write_layer.serialise(n, compact=True))
    return fp, rest


def _write(src: Path, sch: Path, entry: dict, tmp: Path, netlist_exporter: Any) -> dict:
    tmp_pcb = checkpoint.copy_project(
        src, tmp, outputs=(f"{src.parent.name}_orphans_removed",), skip_suffixes=(".kicad_net",), max_bytes=_MAX_COPY_BYTES
    )
    plan = _plan(tmp_pcb, tmp / sch.name, netlist_exporter)  # against the copy: one consistent snapshot
    if plan["removable"] != entry["removable"]:
        raise checkpoint.Failed(
            "error",
            "the orphans that can be removed changed since the proposal was made: re-run propose_orphan_footprint_removal",
            note="nothing was written; the proposal is stale and its revision_token is spent",
            stale=True,
        )
    root = write_layer.load(tmp_pcb)
    gone = set(plan["removable"])
    drop = {id(f) for f in _footprints(root) if si._reference_of(f) in gone}
    if len(drop) != len(gone):
        raise checkpoint.Failed("error", f"expected to remove {len(gone)} footprint(s) and found {len(drop)}: nothing was written")
    kept_fp = [write_layer.serialise(f, compact=True) for f in _footprints(root) if id(f) not in drop]
    rest = _snapshot(root)[1]
    root.children[:] = [c for c in root.children if id(c) not in drop]  # the write layer's low-level path
    write_layer.save(root, tmp_pcb)
    return {"pcb": tmp_pcb, "gone": sorted(gone), "kept_fp": kept_fp, "rest": rest}


def _verify(tmp: Path, state: dict) -> dict:
    """From a fresh parse of the saved file: exactly the planned footprints are gone, every other
    footprint and every other top-level node is unchanged."""
    pcb: Path = state["pcb"]
    try:
        fresh = write_layer.load(pcb)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: file failed to re-parse: {exc}", [checkpoint.mismatch(pcb.name, "board", "a board that parses", str(exc))]
        ) from exc
    fp, rest = _snapshot(fresh)
    refs = {si._reference_of(f) for f in _footprints(fresh)}
    leftover = sorted(set(state["gone"]) & refs)
    if leftover:
        raise checkpoint.NotVerified(
            f"CRITICAL: orphan(s) still on the board: {leftover}",
            [checkpoint.mismatch(pcb.name, "footprint set", "without " + ", ".join(state["gone"]), sorted(refs))],
        )
    if fp != state["kept_fp"]:
        raise checkpoint.NotVerified(
            f"CRITICAL: footprints after reload are not exactly the kept ones ({len(fp)} found, {len(state['kept_fp'])} expected)",
            [checkpoint.mismatch(pcb.name, "kept footprints", len(state["kept_fp"]), len(fp))],
        )
    if rest != state["rest"]:
        raise checkpoint.NotVerified(
            "CRITICAL: a top-level node other than a footprint changed",
            [checkpoint.mismatch(pcb.name, "other top-level nodes", len(state["rest"]), len(rest))],
        )
    return {"orphans_removed": state["gone"]}


def confirm_orphan_footprint_removal(pcb_path: str | Path, revision_token: str, *, netlist_exporter: Any = None) -> dict:
    """Apply a proposal onto a new revision, reload it and verify before it is moved into place."""
    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    sch, out_dir = Path(entry["sch"]), Path(entry["out_dir"])
    problem = checkpoint.output_dir_problem(src, out_dir, what="the orphan removal")
    if problem:
        return {"status": "error", "reason": problem}
    built = checkpoint.build_revision(
        out_dir,
        source_pcb=src,
        write=lambda tmp: _write(src, sch, entry, tmp, netlist_exporter),
        verify=_verify,
        success="removed",
        before_verified="orphan removal failed before a revision was verified",
        note=_KEPT,
    )
    if built.get("stale"):
        _PENDING.spend(revision_token)
    if built["status"] == "removed":
        _PENDING.spend(revision_token)
        built["output_pcb"] = str(out_dir / src.name)
        built["verified"] = "reloaded from disk: the planned footprints are gone, nothing else changed"
        built["note"] = "This is not a routing or done claim: only verify_board produces one."
    return built

