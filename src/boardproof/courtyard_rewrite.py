"""Rewrite the courtyard of already adopted footprints, in the library and on the board (propose/confirm; issue 250).

Adoption never overwrites and ``list_adopted_courtyard_errors`` only reports (issue 228). This pair takes a
list of adopted footprint names, sets each library footprint's courtyard to the hull of its pads plus the margin
(``footprint_courtyard.fix``: the measurement, the box and the write all reuse that module) and puts the same
rectangle into every placed copy on the board (``footprint_courtyard.apply_box``), hulled with that copy's own
old courtyard. The copy's ``(at)``, pads and every other node are not touched: a pad angle in a board footprint is
board-absolute, so a copy's box comes from the library footprint, never from the copy's own pads.

A footprint is refused (listed, nothing written for it) when it is not in the library, its courtyard already
encloses every pad, it cannot be measured, or a placed copy sits on the back side (a flipped copy stores mirrored
graphics; that case is not proven, so it is refused whole). The new revision is built through
``checkpoint.build_revision``; the self-check reloads the saved files and demands that the library footprints and
the copies changed only in their courtyard items, that every other node and file is unchanged, and that each
rewritten footprint now encloses its pads (``list_adopted``'s own check) and each copy's courtyard encloses the
library box. A mismatch is ``write_not_verified`` and the source project is untouched.

Test seam ``_clock`` only; the tool layer exposes no seam.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

from boardproof import checkpoint, footprint_courtyard, write_layer
from boardproof.raw_geometry import RawGeometryError

_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())
_DRIFT = "the project changed since the proposal was made: re-run propose_courtyard_rewrite"
_KEPT = "nothing was written and the revision_token is still valid"
_MAX_COPY_BYTES = 512 * 1024 * 1024
_BOARD_FP = ("footprint", "module")

REWRITE_SENTENCE = (
    "Only the courtyard rectangle changes: the library footprint's courtyard becomes the box of its pads plus 0.25 mm "
    "(never smaller than the old one) and every placed copy on the board gets the same box in its own courtyard, "
    "without moving: position, rotation, pads and every other node stay as they are. Only copies linked as "
    "project_parts:<name> are rewritten. A footprint that is not in the library, already encloses its pads, cannot be measured, "
    "has a copy on the back side or in a group, or has a copy under another library prefix is refused and listed "
    "with the reason. The confirm reloads the written files, checks that nothing but the courtyards changed and "
    "that every rewritten footprint now encloses its pads."
)


def _fp_name(node: Any) -> str:
    return str(node.get_string(0) or "")


def _reference(node: Any) -> str:
    for prop in node.find_all("property"):
        if prop.get_string(0) == "Reference":
            return str(prop.get_string(1) or "")
    return ""


def _stripped(node: Any) -> str:
    """Compact text of a footprint node without its courtyard items."""
    drop = {id(n) for n in footprint_courtyard._crtyd_items(node)}
    copy = write_layer.SExp.list(node.name, *[c for c in node.children if id(c) not in drop])
    return write_layer.serialise(copy, compact=True)


def _names_problem(names: Any) -> str | None:
    if not isinstance(names, list) or not names or not all(isinstance(n, str) and n for n in names):
        return "footprints must be a non-empty list of footprint names"
    for n in names:
        if n in (".", "..") or any(c in n for c in "/\\:") or n != n.strip():
            return f"footprint name {n!r} must be a plain name without a path or a library prefix"
    return None


def _board_copies(board: Any, library_name: str, name: str) -> list[Any]:
    wanted = f"{library_name}:{name}"
    return [n for n in board.children if not n.is_atom and n.name in _BOARD_FP and _fp_name(n) == wanted]


def _other_prefix(board: Any, library_name: str, name: str) -> list[Any]:
    """Board footprints with this bare name under another library prefix (or none): not matched, so never silently skipped."""
    own = f"{library_name}:{name}"
    return [n for n in board.children if not n.is_atom and n.name in _BOARD_FP and _fp_name(n) != own and _fp_name(n).rsplit(":", 1)[-1] == name]


def _plan(pcb: Path, names: list[str], library_name: str, library_dir: str, margin: float = footprint_courtyard.MARGIN_MM) -> dict:
    """Read-only: per requested name either a rewrite entry or a refusal."""
    base = pcb.parent / Path(*library_dir.replace("\\", "/").split("/")) / f"{library_name}.pretty"
    board = write_layer.load(pcb)
    rewrite: list[dict] = []
    refused: list[dict] = []
    grouped: set[str] = set()  # a group names its members by uuid
    for group in board.children:
        if not group.is_atom and group.name == "group" and group.find("members") is not None:
            grouped.update(str(v) for v in group.find("members").values)
    for name in dict.fromkeys(names):
        file = base / f"{name}.kicad_mod"
        if not file.is_file():
            refused.append({"footprint": name, "reason": f"{file.name} is not in {base.name}"})
            continue
        try:
            fit = footprint_courtyard.check(write_layer.load(file), margin)
        except Exception as exc:  # noqa: BLE001 - unmeasurable is refused, never counted clean
            refused.append({"footprint": name, "reason": f"the footprint cannot be measured: {exc}"})
            continue
        if fit["encloses"]:
            refused.append({"footprint": name, "reason": "its courtyard already encloses every pad: nothing to rewrite"})
            continue
        strays = _other_prefix(board, library_name, name)
        if strays:
            refs = ", ".join(_reference(c) or "(no reference)" for c in strays)
            refused.append({"footprint": name, "reason": f"a placed copy {refs} has another library prefix: it is not rewritten"})
            continue
        copies = _board_copies(board, library_name, name)
        flipped = [_reference(c) or "(no reference)" for c in copies if (c.find("layer").get_string(0) if c.find("layer") else None) != "F.Cu"]
        if flipped:
            refused.append({"footprint": name, "reason": f"placed copy {', '.join(flipped)} is not on the front side (F.Cu): a flipped copy is not rewritten"})
            continue
        in_group = [_reference(c) or "(no reference)" for c in copies if c.find("uuid") is not None and str(c.find("uuid").values[0]) in grouped]
        if in_group:
            refused.append({"footprint": name, "reason": f"placed copy {', '.join(in_group)} is a member of a group: it is not rewritten"})
            continue
        rewrite.append({
            "footprint": name,
            "uncovered_pads": fit["uncovered_pads"],
            "courtyard_before": fit["courtyard_box"],
            "courtyard_after": fit["will_be_set_to"],
            "placed_copies": sorted(_reference(c) for c in copies),
        })  # fmt: skip
    return {"rewrite": rewrite, "refused": refused}


def _digest(plan: dict) -> str:
    return hashlib.sha256(repr([(e["footprint"], e["courtyard_after"], e["placed_copies"]) for e in plan["rewrite"]]).encode()).hexdigest()


def _problem(library_name: str, library_dir: str, names: Any) -> str | None:
    return footprint_courtyard._library_problem(library_name, library_dir) or _names_problem(names)


def propose_courtyard_rewrite(
    pcb_path: str | Path,
    footprints: list[str],
    library_name: str = "project_parts",
    library_dir: str = "lib",
    output_dir: str | Path | None = None,
) -> dict:
    """Dry run: per footprint the courtyard before and after and the placed copies that follow. Writes nothing."""
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    if (problem := _problem(library_name, library_dir, footprints)) is not None:
        return {"status": "error", "reason": problem}
    resolved = src.resolve()
    out_dir = Path(output_dir) if output_dir else resolved.parent.parent / f"{resolved.parent.name}_courtyards_rewritten"
    if problem := checkpoint.output_dir_problem(src, out_dir, what="the courtyard rewrite"):
        return {"status": "error", "reason": problem}
    try:
        plan = _plan(src, footprints, library_name, library_dir)
    except (RawGeometryError, Exception) as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"could not plan the rewrite: {exc}"}
    if not plan["rewrite"]:
        return {"status": "nothing_to_do", "revision_token": None, "reason": "no listed footprint can be rewritten", **plan}
    try:
        token = _PENDING.issue(src, {"out_dir": str(out_dir), "names": list(footprints), "lib": library_name, "dir": library_dir, "digest": _digest(plan)})
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        "revision_token": token,
        "output_pcb": str(out_dir / src.name),
        "note": "creates a new project revision; the original is untouched. Call confirm_courtyard_rewrite to apply.",
        **plan,
    }


def _write(src: Path, entry: dict, tmp: Path) -> dict:
    pcb = checkpoint.copy_project(src, tmp, outputs=(Path(entry["out_dir"]).name,), skip_suffixes=(".kicad_net",), max_bytes=_MAX_COPY_BYTES)
    plan = _plan(pcb, entry["names"], entry["lib"], entry["dir"])  # against the copy: one consistent snapshot
    if _digest(plan) != entry["digest"]:
        raise checkpoint.Failed(
            "error", _DRIFT, note="nothing was written; the proposal is stale and its revision_token is spent", stale=True
        )
    base = pcb.parent / Path(*entry["dir"].replace("\\", "/").split("/")) / f"{entry['lib']}.pretty"
    board = write_layer.load(pcb)
    before_board = [write_layer.serialise(n, compact=True) for n in board.children]
    names = {f"{entry['lib']}:{i['footprint']}" for i in plan["rewrite"]}
    before_stripped = {
        i: _stripped(n) for i, n in enumerate(board.children) if not n.is_atom and n.name in _BOARD_FP and _fp_name(n) in names
    }
    done: list[dict] = []
    for item in plan["rewrite"]:
        name = item["footprint"]
        lib_root = write_layer.load(base / f"{name}.kicad_mod")
        lib_before = _stripped(lib_root)
        box = footprint_courtyard.fix(lib_root)
        if box is None:
            raise checkpoint.Failed("error", f"{name}: the courtyard changed under the plan: nothing was written")
        write_layer.save(lib_root, base / f"{name}.kicad_mod")
        for copy in _board_copies(board, entry["lib"], name):
            old = footprint_courtyard._courtyard(copy)
            footprint_courtyard.apply_box(copy, footprint_courtyard._round(footprint_courtyard._hull([tuple(box)] + ([old] if old else []))))
        done.append({"footprint": name, "box": box, "lib_before": lib_before})
    write_layer.save(board, pcb)
    return {"pcb": pcb, "base": base, "done": done, "before_board": before_board, "before_stripped": before_stripped, "library": entry["lib"]}


def _verify(tmp: Path, state: dict) -> dict:
    pcb: Path = state["pcb"]
    try:
        fresh = write_layer.load(pcb)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: file failed to re-parse: {exc}", [checkpoint.mismatch(pcb.name, "board", "a board that parses", str(exc))]
        ) from exc
    after_board = [write_layer.serialise(n, compact=True) for n in fresh.children]
    bad: list[dict] = []
    if len(after_board) != len(state["before_board"]):
        bad.append(checkpoint.mismatch(pcb.name, "top-level nodes", len(state["before_board"]), len(after_board)))
    rewritten = {f"{state['library']}:{d['footprint']}": d["box"] for d in state["done"]}
    for index, (old, new) in enumerate(zip(state["before_board"], after_board, strict=False)):
        node = fresh.children[index]
        if not node.is_atom and node.name in _BOARD_FP and _fp_name(node) in rewritten:
            if state["before_stripped"].get(index) != _stripped(node):  # the same node but for its courtyard items
                bad.append(checkpoint.mismatch(pcb.name, f"{_fp_name(node)} {_reference(node)} outside its courtyard", "unchanged", "changed"))
            court = footprint_courtyard._courtyard(node)
            want = rewritten[_fp_name(node)]
            if court is None or any(a > b + 1e-6 for a, b in zip(court[:2], want[:2], strict=True)) or any(a < b - 1e-6 for a, b in zip(court[2:], want[2:], strict=True)):
                bad.append(checkpoint.mismatch(pcb.name, f"{_fp_name(node)} {_reference(node)} courtyard", f">= {want}", court))
        elif old != new:
            bad.append(checkpoint.mismatch(pcb.name, f"top-level node {index}", "unchanged", "changed"))
    for d in state["done"]:
        file = state["base"] / f"{d['footprint']}.kicad_mod"
        try:
            lib = write_layer.load(file)
            fit = footprint_courtyard.check(lib)
        except Exception as exc:  # noqa: BLE001
            bad.append(checkpoint.mismatch(file.name, "footprint", "a footprint that parses and measures", str(exc)))
            continue
        if not fit["encloses"]:
            bad.append(checkpoint.mismatch(file.name, "courtyard", "encloses every pad", footprint_courtyard.describe(fit)))
        if _stripped(lib) != d["lib_before"]:
            bad.append(checkpoint.mismatch(file.name, "nodes outside the courtyard", "unchanged", "changed"))
    if bad:
        raise checkpoint.NotVerified(
            f"CRITICAL: {len(bad)} difference(s) between the intended rewrite and the saved files (first: {bad[0]['item']}); nothing is written", bad
        )
    return {"footprints_rewritten": [d["footprint"] for d in state["done"]]}


def confirm_courtyard_rewrite(pcb_path: str | Path, revision_token: str) -> dict:
    """Apply a proposal onto a new project revision, reload it and verify before it is moved into place."""
    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    out_dir = Path(entry["out_dir"])
    if problem := checkpoint.output_dir_problem(src, out_dir, what="the courtyard rewrite"):
        return {"status": "error", "reason": problem}
    built = checkpoint.build_revision(
        out_dir,
        source_pcb=src,
        write=lambda tmp: _write(src, entry, tmp),
        verify=_verify,
        success="rewritten",
        before_verified="courtyard rewrite failed before a revision was verified",
        note=_KEPT,
    )
    if built.get("stale") or built["status"] == "rewritten":
        _PENDING.spend(revision_token)
    if built["status"] == "rewritten":
        built["output_pcb"] = str(out_dir / src.name)
        built["verified"] = "reloaded from disk: only the courtyards changed and every rewritten footprint encloses its pads"
        built["note"] = "This is not a routing or done claim: only verify_board produces one."
    return built
