"""Stage the rung-2 input (build ticket 08; source re-pinned by build ticket 51; staged through
the write layer since build ticket 67, protocol 5).

Copies KiCad's ``demos/pic_programmer`` from a KiCad 10.0.x RELEASE (the install's
``share/kicad/demos/pic_programmer``; only read) into a scratch directory and removes the
board's tracks, arcs, vias and zone through the write layer: ``write_layer.load`` parses the
file KiCad wrote, the top-level ``segment``, ``arc``, ``via`` and ``zone`` nodes are removed
from that tree and nothing else changes, and ``write_layer.save`` writes it (temporary sibling,
``kicad-cli pcb upgrade`` parse check, the self-check below, then ``os.replace``). It prints the
two pins the protocol holds for rung 2: ``board_sha256`` (``autonomy_run.board_sha``) and
``project_sha256`` (``autonomy_run.project_sha``). Pins are content shas with CRLF read as LF,
so the Windows installer (CRLF) and a Linux package (LF) of the same release stage to the same
pins; every tag 10.0.0 to 10.0.7 carries identical ``demos/pic_programmer`` content. These are
the SAME values the runner computes for ``--board <staged board>``. ``board_file_sha256`` is
the raw byte sha of the staged board on this machine, for reference only.

Why not ``kicad_tools``' ``PCB`` model (protocol 4's staging, ``PCB.load`` then
``strip_traces``): ``PCB.load`` adds a numeric net table, 112 entries from ``(net 0 "")`` to
``(net 111 ...)``, that KiCad 10's own file does not have, and the text was written by
``kicad_tools``' serialiser, so the pin hung on two ``kicad_tools`` behaviours a release can
change. Here the staged board is KiCad's file minus the removed nodes, serialised by the
layer. The pin still relies on ``kicad_tools``' low-level parser: the layer writes each atom
from the source text and quoting the parser keeps on it (``_original_str``,
``_originally_quoted``, ``_originally_bare``), so a parser release that dropped those would
move the pin. Measured the same with ``kicad_tools`` 0.23.0 and 0.24.0 (build ticket 67).

Protocol 3 pinned KiCad master ``6a26ce9df3067b2233b415ba2d4cbc3164272ccc`` checked out with
CRLF; no release install reproduces it (build ticket 34). It stays in the protocol's
``version_history``, as do protocol 4's pins (build ticket 67).

Usage (repo root, project venv):
    .venv/Scripts/python.exe evals/rung2/stage_input.py <pic_programmer_dir> <work_dir>

Nothing is written into the fixture directory or any other repository.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import sys
from pathlib import Path

SOURCE = "KiCad 10.0.x release demos/pic_programmer (identical in tags 10.0.0 to 10.0.7)"
STRIPPED = ("segment", "arc", "via", "zone")  # top-level nodes removed; nothing else changes


class StagingNotVerified(RuntimeError):
    """The written board does not read back as the stripped tree (the self-check fired)."""


class StagingRefused(RuntimeError):
    """The board cannot be stripped without leaving a dangling reference; nothing is written."""


def _uuid(node) -> str | None:
    found = node.find("uuid")
    return str(found.values[0]) if found is not None and found.values else None


def _members(node):
    """Every uuid a ``(members ...)`` list names anywhere under ``node``: a ``group``'s, a
    generated tuning pattern's, or any other node's (delta review 1)."""
    for child in node.children:
        if child.is_atom:
            continue
        if child.name == "members":
            yield from (str(v) for v in child.values)
        yield from _members(child)


def strip_tracks(pcb: Path) -> dict[str, int]:
    """Remove every top-level ``segment``, ``arc``, ``via`` and ``zone`` from ``pcb`` through
    the write layer and return how many of each were removed. The layer's ``verify`` hook
    re-reads the temporary file and compares it, token for token, with the stripped tree
    before it replaces ``pcb``; on a mismatch ``pcb`` is untouched and ``StagingNotVerified``
    is raised. A board where a ``members`` list (a ``group``, a generated tuning pattern, any
    node) names a removed item is refused before any write (``StagingRefused``): it would name
    a uuid that no longer exists."""
    from kicad_mcp import write_layer

    root = write_layer.load(pcb)
    removed = {name: 0 for name in STRIPPED}
    removed_uuids: set[str] = set()
    kept = []
    for child in root.children:
        if child.name in STRIPPED:
            removed[child.name] += 1
            removed_uuids.add(_uuid(child))
        else:
            kept.append(child)
    dangling = sorted(set(_members(root)) & (removed_uuids - {None}))
    if dangling:
        raise StagingRefused(f"{pcb.name}: a members list names stripped items {dangling}; remove them from it first")
    root.children = kept
    intended = write_layer.serialise(root, compact=True)

    def verify(temp: Path) -> None:
        if write_layer.serialise(write_layer.load(temp), compact=True) != intended:
            raise StagingNotVerified(f"{pcb.name}: the written board does not read back as the stripped tree")

    write_layer.save(root, pcb, verify=verify)
    return removed


def stage(fixture_dir: Path, work_dir: Path) -> dict:
    from kicad_mcp import autonomy_run

    fixture_dir, work_dir = Path(fixture_dir), Path(work_dir)
    if work_dir.exists():
        shutil.rmtree(work_dir)
    shutil.copytree(fixture_dir, work_dir)
    pcb = work_dir / "pic_programmer.kicad_pcb"
    removed = strip_tracks(pcb)
    return {
        "board": str(pcb),
        "removed": removed,
        "board_sha256": autonomy_run.board_sha(pcb),
        "project_sha256": autonomy_run.project_sha(pcb),
        "board_file_sha256": hashlib.sha256(pcb.read_bytes()).hexdigest(),
        "source": SOURCE,
    }


if __name__ == "__main__":
    print(json.dumps(stage(Path(sys.argv[1]), Path(sys.argv[2])), indent=2))
