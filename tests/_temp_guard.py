"""Issue 241: no foreign KiCad file above the pytest base temp folder.

The project folder of a sheet is found by walking up to the nearest ``.kicad_pro``, and a board
beside the temp root is picked up as a target; a file another tool left in the system temp folder
broke a hundred gate tests twice. The session guard (``tests/conftest.py``) stops the run at the
start and names the file instead.
"""

from __future__ import annotations

import os
from pathlib import Path


def stray_kicad_files(root: Path) -> list[Path]:
    """The KiCad files (suffix ``.kicad_*``; a ``.bak`` or ``.lck`` is no project) lying directly in ``root`` or a
    parent of it, not below it. ``root`` is the temp folder pytest puts its base temp folder in."""
    found: list[Path] = []
    root = Path(root).resolve()
    for parent in (root, *root.parents):
        try:
            names = sorted(os.listdir(parent))
        except OSError:
            continue  # a folder the process cannot list cannot hold a file the tests can find
        found += [parent / n for n in names if Path(n).suffix.startswith(".kicad_") and (parent / n).is_file()]
    return found


def stray_message(found: list[Path]) -> str:
    return (
        "Issue 241: KiCad file(s) in the pytest temp folder or a parent of it would be picked up "
        "as a project or board by tests that build in tmp_path. Move or delete: "
        + ", ".join(str(p) for p in found)
    )
