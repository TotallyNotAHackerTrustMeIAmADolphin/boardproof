"""Corrupted-write injection for self-check tests (build ticket 31; reused by 30 and 32).

docs/TESTING.md rule 5 and docs/ARCHITECTURE.md decision 4: every self-check has a test that
feeds it a corrupted write and shows it fires. The pattern:

1. ``corrupt_writes`` wraps the function a tool writes its file with (for the PCB
   tools the write layer's ``kicad_mcp.write_layer.save_pcb``, build ticket 42). The
   real write runs first (temporary sibling, parse check, ``os.replace``); then, while the
   handle is armed and ``when(path)`` holds, ``corrupt(path)`` changes the file that
   was just written. The tool's in-memory model is NOT touched, so the tool still
   knows what it meant to write; only the bytes on disk differ, exactly as with a
   writer that drops or alters a value.
2. ``sexp_edit(edit)`` builds such a ``corrupt`` for KiCad S-expression files: it
   loads the file with kicad_tools' low-level ``Document`` API, lets ``edit(root)``
   change one node through the node API (``set_value``, ``remove``, ``append``),
   and saves it with the same API. No text is patched (CLAUDE.md hard rule 1). An
   edit that changes nothing fails the test, so a corruption can never be vacuous.
3. The test asserts the tool reports a failure that names the mismatch (not
   success), that the injection really hit (``handle.hits``), that nothing was left
   behind, and then, as the positive control, calls ``handle.disarm()`` and repeats
   the call with the same token: it must now succeed. That proves the failure came
   from the corruption alone.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class WriteCorruption:
    """Handle of one armed injection: which written files were corrupted."""

    hits: list[Path] = field(default_factory=list)
    armed: bool = True

    def disarm(self) -> None:
        self.armed = False


def _layer_save_path_of(args: tuple, kwargs: dict) -> Path:
    """The target path of ``write_layer.save(root, path)`` / ``save_pcb(board, path)``."""
    if kwargs.get("path") is not None:
        return Path(kwargs["path"])
    if len(args) >= 2 and args[1] is not None:
        return Path(args[1])
    raise AssertionError("corrupt_writes: cannot tell which file this write targets")


def corrupt_writes(
    monkeypatch: Any,
    owner: Any,
    attr: str,
    corrupt: Callable[[Path], None],
    *,
    when: Callable[[Path], bool] | None = None,
    path_of: Callable[[tuple, dict], Path] | None = None,
) -> WriteCorruption:
    """Wrap ``owner.attr`` (a writing function or method) so that each write it makes is
    followed by ``corrupt(path)`` while the returned handle is armed and ``when(path)``
    holds. ``path_of(args, kwargs)`` names the written file from the wrapped call's
    arguments; it may be left out only for the write layer's ``save`` and ``save_pcb``, whose
    signatures the helper knows. Any other writer (a lib-table write, ``Path.write_text``) must
    pass it, so a guessed argument position can never corrupt the wrong file."""
    if path_of is None:
        from kicad_mcp import write_layer

        if not (owner is write_layer and attr in ("save", "save_pcb")):
            raise TypeError(f"corrupt_writes: pass path_of to wrap {getattr(owner, '__name__', owner)}.{attr}")
        path_of = _layer_save_path_of
    handle = WriteCorruption()
    real = getattr(owner, attr)

    @functools.wraps(real)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        result = real(*args, **kwargs)
        path = path_of(args, kwargs)
        if handle.armed and (when is None or when(path)):
            corrupt(path)
            handle.hits.append(path)
        return result

    monkeypatch.setattr(owner, attr, wrapped)
    return handle


def corrupt_pcb_saves(
    monkeypatch: Any, edit: Callable[[Any], None], *, when: Callable[[Path], bool] | None = None
) -> WriteCorruption:
    """``corrupt_writes`` on ``write_layer.save_pcb`` (every board save since build ticket 42)
    with an S-expression edit."""
    from kicad_mcp import write_layer

    return corrupt_writes(monkeypatch, write_layer, "save_pcb", sexp_edit(edit), when=when)


def sexp_edit(edit: Callable[[Any], None]) -> Callable[[Path], None]:
    """A ``corrupt`` that applies ``edit(root)`` to a KiCad S-expression file through
    kicad_tools' ``Document`` API and fails if the edit changed nothing."""

    def corrupt(path: Path) -> None:
        from kicad_tools.sexp import Document

        doc = Document.load(path)
        before = doc.root.to_string()
        edit(doc.root)
        assert doc.root.to_string() != before, f"the corruption changed nothing in {path}"
        doc.save(path)

    return corrupt


def in_revision_tmp(out_dir: Path) -> Callable[[Path], bool]:
    """``when`` for the checkpoint tools that build ``.<out_dir name>.tmp-<hex>`` beside
    ``out_dir`` and move it into place only after their self-check: matches writes into
    that temporary revision and nothing else (fills, probes and fixtures are left alone).

    It fits ONLY that temporary-directory layout (``board_outline`` and ``pour_step`` today).
    A tool that writes a temporary sibling file next to its input (build ticket 22's shared
    helper) or writes in place needs its own ``when``, matched to its own temporary name."""
    prefix = f".{out_dir.name}.tmp-"

    def when(path: Path) -> bool:
        return path.parent.parent == out_dir.parent and path.parent.name.startswith(prefix)

    return when


# --- node edits (kicad_tools node API only) ------------------------------------------------


def top_nodes(root: Any, tag: str, where: Callable[[Any], bool] | None = None) -> list[Any]:
    """Top-level children named ``tag`` (and matching ``where``)."""
    return [c for c in root.children if c.name == tag and (where is None or where(c))]


def on_layer(layer: str) -> Callable[[Any], bool]:
    def where(node: Any) -> bool:
        child = node.find("layer")
        return child is not None and bool(child.values) and str(child.values[0]) == layer

    return where


def shift_xy(node: Any, tag: str, dx: float = 0.0, dy: float = 0.0) -> None:
    """Move ``node``'s ``(tag x y ...)`` child by (dx, dy)."""
    child = node.find(tag)
    assert child is not None, f"{node.name} has no ({tag} ...)"
    child.set_value(0, float(child.values[0]) + dx)
    child.set_value(1, float(child.values[1]) + dy)


def polygon_points(node: Any) -> list[Any]:
    """The ``(xy x y)`` nodes of a zone's ``(polygon (pts ...))``."""
    return node.find("polygon").find("pts").find_all("xy")


def footprint_reference(fp: Any) -> str:
    for child in fp.children:
        if child.name == "property" and len(child.values) >= 2 and str(child.values[0]) == "Reference":
            return str(child.values[1])
    for child in fp.children:
        if child.name == "fp_text" and len(child.values) >= 2 and str(child.values[0]) == "reference":
            return str(child.values[1])
    return ""
