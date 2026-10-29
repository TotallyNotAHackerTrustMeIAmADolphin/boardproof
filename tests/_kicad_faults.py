"""The one sanctioned way for a test to write a KiCad file KiCad would not write (build ticket 74).

CLAUDE.md hard rule 1 holds in tests: a test builds a KiCad variant through the write layer
(``write_layer.load``, change the tree with the typed setters, ``write_layer.save``), which
writes what KiCad writes and parse-checks it with ``kicad-cli``. Some tests must write a file
the layer refuses to write, to prove that the code under test refuses it too: a file that
changed on disk after a proposal (drift), a raw line break inside a string (mixelpixx #423), a
structure KiCad cannot load, a byte below the tokens (a lone CR, a truncated file). Those
writes come here, and only here: the raw-write ratchet over ``tests/``
(``tests/test_ratchets.py``, ``[[test_kicad_write]]`` in ``tests/ratchets.toml``) lists this
module's writes as the reviewed fault sites, and every other raw write of a KiCad file in
``tests/`` as debt that only shrinks. Every call of ``write_unchecked``, ``raw_atom`` and
``rewrite_bytes`` elsewhere in ``tests/`` is listed by its text in ``[[test_kicad_fault_call]]``,
so each new fault is reviewed.

Each helper changes exactly the one thing it names; everything else is the write layer's own
serialisation (or, for ``rewrite_bytes``, the file's own bytes), so a test that uses it
isolates the fault. Never use ``write_unchecked`` for a valid fixture: a fixture KiCad can load
goes through ``write_layer.save``.

Decision (ticket 74): faults are written from the write layer's serialisation, with a unique
marker atom replaced by its verbatim text, not through ``kicad_tools``' ``_original_str``
round-trip hook (ticket 50's first approach). The layer's serialiser writes a number atom's
``_original_str`` only when it still denotes the value, so the hook is lost on the project's
own write path, and ``kicad_tools``' serialiser (which keeps it) also writes numeric-looking
strings bare and floats with 6 significant digits, so the rest of the file would differ from
what KiCad writes.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable
from pathlib import Path

from kicad_tools.sexp import SExp

from kicad_mcp import write_layer

_MARKER_PREFIX = "kicad-mcp-test-fault-"
_VERBATIM: dict[str, str] = {}


def drift(path: str | Path) -> None:
    """Append one line feed to ``path``: its bytes (and so its hash) change, its tokens do
    not. Stands in for an edit made outside any tool after a proposal or a verdict."""
    with open(path, "ab") as fh:
        fh.write(b"\n")


def rewrite_bytes(path: str | Path, change: Callable[[bytes], bytes]) -> Path:
    """Replace ``path``'s bytes with ``change(its bytes)``, as they are: no line-ending or
    encoding conversion. For a fault that lives below the tokens, where no tree edit can
    reach: a flipped or inserted byte, a lone CR, a converted line ending, a truncated file,
    an appended comment line. Tests of hashes, pins, drift and parse errors need these. Every
    caller is listed by its call text in ``[[test_kicad_fault_call]]`` of
    ``tests/ratchets.toml``, so a new one is reviewed. Returns ``path``."""
    path = Path(path)
    with open(path, "rb") as fh:
        data = fh.read()
    with open(path, "wb") as fh:
        fh.write(change(data))
    return path


def raw_atom(text: str) -> SExp:
    """An atom that ``write_unchecked`` writes exactly as ``text``: no quoting, no escaping.
    ``raw_atom('"line' + chr(10) + 'break"')`` is a quoted string with a raw line break, as a
    text-patching writer leaves it. The write layer itself would write the marker it carries
    as a plain quoted string, so a tree holding one is only ever written here."""
    marker = f"{_MARKER_PREFIX}{uuid.uuid4().hex}"
    _VERBATIM[marker] = text
    return write_layer.string(marker)


def write_unchecked(root: SExp, path: str | Path) -> Path:
    """Write ``root`` to ``path`` as the write layer serialises it (the layer's line ending),
    with every ``raw_atom`` replaced by its verbatim text, and without the layer's
    ``kicad-cli`` parse check or temporary sibling. For a tree that carries a fault on
    purpose: a raw atom, or a structure KiCad or the layer refuses (a sheet whose file is
    missing, an unknown token). Returns ``path``."""
    path = Path(path)
    text = (write_layer.serialise(root) + "\n").replace("\n", os.linesep)
    for marker, verbatim in _VERBATIM.items():
        text = text.replace(write_layer.quote(marker), verbatim)
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return path
