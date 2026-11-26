"""List the KiCad groups of a board (read only; build ticket 231, slice 2).

A top-level ``(group "name" (uuid ..) (members uuid ..))`` names its members by uuid. Members are
resolved against the top-level nodes' own uuids, read with ``raw_geometry.direct_uuid`` (``find``
would also return a descendant's uuid). A member uuid that matches no top-level node is listed under
``unresolved``, never dropped.

Reused: ``write_layer.load``, ``raw_geometry._reference`` and ``direct_uuid``, ``placement_anchor.is_locked``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from boardproof import placement_anchor, raw_geometry, write_layer


def _top(root: Any, name: str) -> list[Any]:
    return [c for c in root.children if not c.is_atom and c.name == name]


def _members(group: Any) -> list[str]:
    """Member entries of every (members ..) node, each once; a list-shaped entry is written out."""
    seen: dict[str, None] = {}
    for node in (c for c in group.children if not c.is_atom and c.name == "members"):
        for child in node.children:
            seen[str(child.value) if child.is_atom else write_layer.serialise(child).strip()] = None
    return list(seen)


def list_groups(pcb_path: str | Path) -> dict:
    path = Path(pcb_path)
    if not path.is_file():
        return {"status": "incomplete", "reason": f"file not found: {pcb_path}"}
    try:
        root = write_layer.load(path)
    except Exception as exc:  # noqa: BLE001
        return {"status": "incomplete", "reason": f"failed to load board: {exc}"}
    kinds: dict[str, str] = {}  # uuid of a top-level node -> its node name
    for node in root.children:
        if not node.is_atom:
            uid = raw_geometry.direct_uuid(node)
            if uid:
                kinds[uid] = node.name
    footprints = {raw_geometry.direct_uuid(f): f for f in _top(root, "footprint")}
    group_ids = {raw_geometry.direct_uuid(g) for g in _top(root, "group")}
    groups = []
    for g in _top(root, "group"):
        name = str(g.children[0].value) if g.children and g.children[0].is_atom else ""
        uuids = _members(g)
        fps = [footprints[u] for u in uuids if u in footprints]
        other: dict[str, int] = {}
        for u in uuids:
            if u in kinds and u not in footprints and u not in group_ids:
                other[kinds[u]] = other.get(kinds[u], 0) + 1
        groups.append(
            {
                "name": name,
                "uuid": raw_geometry.direct_uuid(g),
                "locked": placement_anchor.is_locked(g),
                "members": len(uuids),
                "footprints": sorted(raw_geometry._reference(f) for f in fps),
                "locked_footprints": sorted(raw_geometry._reference(f) for f in fps if placement_anchor.is_locked(f)),
                "nested_groups": [u for u in uuids if u in group_ids],
                "other_members": other,
                "unresolved": [u for u in uuids if u not in kinds],
            }
        )
    result = {"status": "ok", "group_count": len(groups), "groups": groups}
    nodes = _top(root, "footprint") + _top(root, "group")
    if any(not raw_geometry.direct_uuid(n) for n in nodes):
        # older files name nodes (id ..) or (tstamp ..); member uuids then cannot be matched
        result["note"] = "uuid form not recognised on some footprint or group: their members are not matched and show as unresolved"
    return result
