"""The one way a test copies a fixture folder (issue 238).

A parallel worker, or kicad-cli run in place by another tool, can leave a KiCad lock (``~*.lck``)
in a shared fixture; a copy that carries it fails with ``file_open_in_kicad``. Every test copies
through ``copy_fixture``; ``tests/test_ratchets.py`` fails on a direct ``shutil.copytree``.
"""

from __future__ import annotations

import shutil

LOCKS = shutil.ignore_patterns("~*.lck")


def copy_fixture(src, dest, **kwargs):
    """``shutil.copytree`` that leaves KiCad lock files behind."""
    kwargs.setdefault("ignore", LOCKS)
    return shutil.copytree(src, dest, **kwargs)
