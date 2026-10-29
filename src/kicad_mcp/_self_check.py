r"""Reload-and-compare for tools that write a KiCad S-expression file (build ticket 31).

One place for the comparison both ``board_outline`` and ``pour_step`` run after saving their
temporary revision; build ticket 22's shared checkpoint helper is meant to absorb it.

The comparison is exact on serialised text (the write layer's ``serialise(node, compact=True)``
of every top-level node, build ticket 42), never on parsed numbers with a tolerance: a parsed
atom keeps its original text, so an untouched node reloads byte-identical, and a node created
or set in memory is written exactly as the layer serialises it (numbers with full precision).
Two checks:

- **the write landed:** the reload equals the in-memory tree that was saved, node for node;
- **the write is the intended change:** the reload equals the source file's parse minus the
  nodes the tool removed plus the nodes it wrote. The one tolerated difference is named, not
  silent: ``PCB.load`` adds a net table (top-level ``(net ...)``) to a board that has none
  (KiCad 10 format). Those entries are returned under ``tolerated`` and are still covered by
  the first check.

What it cannot see: a loss inside kicad_tools' parser. Source and reload go through the same
parser, so a token it reads wrongly reads the same wrong way on both sides: ``PCB.load`` and
``parse_file`` read a raw carriage return inside a string as a line feed. The write layer
escapes ``\r`` and its ``load`` keeps a raw one (build ticket 42); a reload through
``PCB.load`` still cannot see that loss. (``"back\\slash"`` parses correctly in 0.20.0 and
0.23.0: ``tests/test_write_layer.py``.)

Tool results carry the lists bounded by ``bounded`` (docs/ARCHITECTURE.md decision 9: ``{count,
items}``, at most ``_MAX_LISTED`` items each).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

_MAX_LISTED = 20


def node_text(node: Any) -> str:
    """A node as the write layer writes it, on one line (what every comparison keys on)."""
    from kicad_mcp import write_layer

    return write_layer.serialise(node, compact=True)


def _child_value(node: Any, tag: str) -> str | None:
    child = node.find(tag)
    return str(child.values[0]) if child is not None and child.values else None


def _reference(node: Any) -> str | None:
    for child in node.children:
        if child.name == "property" and len(child.values) >= 2 and str(child.values[0]) == "Reference":
            return str(child.values[1])
        if child.name == "fp_text" and len(child.values) >= 2 and str(child.values[0]) == "reference":
            return str(child.values[1])
    return None


def node_label(node: Any) -> str:
    """A short name for a top-level node: ``footprint:C1``, ``zone:KEEPOUT_H1``, ``gr_arc:<uuid>``."""
    if node.is_atom:
        return f"atom:{node.value}"
    if node.name == "footprint" and (ref := _reference(node)):
        return f"footprint:{ref}"
    if node.name == "zone" and (name := _child_value(node, "name")):
        return f"zone:{name}"
    if uuid := _child_value(node, "uuid"):
        return f"{node.name}:{uuid}"
    text = node_text(node)
    return text if len(text) <= 60 else text[:57] + "..."


def _surplus(have: Counter, want: Counter) -> list[str]:
    return [t for t, n in have.items() for _ in range(n - want.get(t, 0))]


def reload_differences(
    *,
    source: Any,
    removed: Iterable[Any],
    written: Iterable[Any],
    memory: Any,
    reloaded: Any,
) -> dict:
    """Compare a reloaded revision with what was saved and with what was intended.

    ``source`` is a fresh parse of the original file, ``removed`` the source nodes the tool
    deleted, ``written`` the nodes it added (as held in memory), ``memory`` the tree that was
    saved and ``reloaded`` a fresh parse of the saved file. Returns ``missing`` (intended or
    saved, absent from the reload), ``unexpected`` (in the reload, neither saved nor intended)
    and ``tolerated``, each a list of node labels; the write is verified when the first two
    are empty."""
    by_text: dict[str, Any] = {}
    for root in (reloaded, memory, source):
        for child in root.children:
            by_text.setdefault(node_text(child), child)
    written = list(written)
    for node in written:
        by_text.setdefault(node_text(node), node)

    reload_texts = Counter(node_text(c) for c in reloaded.children)
    memory_texts = Counter(node_text(c) for c in memory.children)
    expected = Counter(node_text(c) for c in source.children)
    expected.subtract(node_text(n) for n in removed)
    expected.update(node_text(n) for n in written)

    source_has_nets = any(c.name == "net" for c in source.children if not c.is_atom)
    missing = _surplus(memory_texts, reload_texts) + _surplus(+expected, reload_texts)
    unexpected, tolerated = list(_surplus(reload_texts, memory_texts)), []
    for text in _surplus(reload_texts, +expected):
        node = by_text[text]
        if not source_has_nets and not node.is_atom and node.name == "net":
            tolerated.append(text)
        else:
            unexpected.append(text)

    def labels(texts: list[str]) -> list[str]:
        return sorted({node_label(by_text[t]) for t in texts})

    return {"missing": labels(missing), "unexpected": labels(unexpected), "tolerated": labels(tolerated)}


def bounded(differences: dict, limit: int = _MAX_LISTED) -> dict:
    """``reload_differences`` for a tool result: each list as ``{count, items}``, at most
    ``limit`` items (decision 9's CHARACTER_LIMIT; a board can add hundreds of net entries)."""
    return {
        key: {"count": len(differences[key]), "items": differences[key][:limit]}
        for key in ("missing", "unexpected", "tolerated")
    }


def describe(differences: dict) -> str:
    """One line naming the differing nodes (at most ``_MAX_LISTED`` per list)."""
    parts = []
    for key, what in (("missing", "missing after reload"), ("unexpected", "present but not intended")):
        items = differences[key]
        if items:
            more = f" (+{len(items) - _MAX_LISTED} more)" if len(items) > _MAX_LISTED else ""
            parts.append(f"{what}: " + ", ".join(items[:_MAX_LISTED]) + more)
    return "; ".join(parts)


def as_written(value: float) -> float:
    """``value`` as the write layer saves a number created in memory: full precision since
    build ticket 42 (kicad_tools' own writer rounded to ``%.6g``: 103.0125 to 103.013).
    Summary checks compare reloaded numbers with this."""
    from kicad_mcp import write_layer

    return float(write_layer.format_number(value))
