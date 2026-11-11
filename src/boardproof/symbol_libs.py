"""Resolve a schematic ``lib_id`` nickname through the library tables (build ticket 76).

kicad_tools looks a symbol up only in ``KICAD_SYMBOL_DIR`` plus platform folders, so a project
library such as ``scbe:R`` (a row of the project's ``sym-lib-table``) was never found. This module
adds KiCad's order in front of that search: the project ``sym-lib-table`` (its directory is what
``${KIPRJMOD}`` names), then the user's global ``sym-lib-table``, then KiCad's own symbol folder
(left to kicad_tools). A nickname that a table declares is decided by that table: a miss in its
file is a miss, with no fall-through. A nickname no table declares and KiCad's folder lacks is
reported with every place that was searched, before anything is written.

Reads stay inside the project directory and the KiCad install: a row whose file lies elsewhere is
refused, never read. The libraries are read only; nothing here writes a file.

kicad_tools matches ``<nickname>.kicad_sym`` by file name, so a row whose file name differs from its
nickname is refused with that reason instead of being copied or renamed.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_VAR = re.compile(r"\$\{([^}]+)\}")
_SYM_ENV = re.compile(r"^KICAD\d*_SYMBOL_DIR$")


def kicad_symbol_dirs() -> list[Path]:
    """KiCad's own symbol folders: ``KICAD_SYMBOL_DIR`` and the install next to ``kicad-cli``."""
    dirs: list[Path] = []
    env = os.environ.get("KICAD_SYMBOL_DIR")
    if env:
        dirs.append(Path(env))
    try:
        from boardproof.kicad_cli import find_kicad_cli

        dirs.append(find_kicad_cli().parent.parent / "share" / "kicad" / "symbols")
    except Exception:  # noqa: BLE001 - no install found: the project tables still work
        pass
    out: list[Path] = []
    for d in dirs:
        if d.is_dir() and d not in out:
            out.append(d)
    return out


def project_dir_of(sheet: Path) -> Path:
    """The folder ``${KIPRJMOD}`` names for ``sheet``: the nearest folder, going up, holding a
    ``.kicad_pro``; else the sheet's own folder."""
    start = sheet.resolve().parent
    for folder in (start, *start.parents):
        try:
            if any(folder.glob("*.kicad_pro")):
                return folder
        except OSError:
            break
    return start


def _global_tables() -> list[Path]:
    appdata = os.environ.get("APPDATA")
    config = Path(appdata) / "kicad" if appdata else Path.home() / ".config" / "kicad"
    if not config.is_dir():
        return []
    return sorted(config.glob("*/sym-lib-table"), reverse=True)  # newest KiCad version first


def _expand(uri: str, project: Path, kicad_dirs: list[Path]) -> Path | None:
    def sub(match: re.Match) -> str:
        key = match.group(1)
        if key == "KIPRJMOD":
            return str(project)
        if _SYM_ENV.match(key) and kicad_dirs:
            return str(kicad_dirs[-1])
        return os.environ.get(key, match.group(0))

    out = _VAR.sub(sub, uri)
    if "${" in out:
        return None
    path = Path(out)
    # A relative path is the project's, never the process working directory's.
    return path if path.anchor else project / path


def _rows(table: Path) -> dict[str, str] | None:
    """``nickname -> uri`` of a table (first row of a nickname wins), None when unreadable."""
    try:
        from kicad_tools.sexp import parse_file

        root = parse_file(str(table))
    except Exception:  # noqa: BLE001
        return None
    rows: dict[str, str] = {}
    for lib in root.children:
        if lib.name != "lib":
            continue
        name, uri = lib.find("name"), lib.find("uri")
        if name is None or uri is None or not name.values or not uri.values:
            continue
        rows.setdefault(str(name.values[0]), str(uri.values[0]))
    return rows


def _inside(path: Path, roots: Iterable[Path]) -> bool:
    try:
        real = path.resolve()
    except OSError:
        return False
    for root in roots:
        try:
            real.relative_to(root.resolve())
            return True
        except (ValueError, OSError):
            continue
    return False


@dataclass
class SymbolLibResolution:
    """What ``resolve_symbol_libraries`` found: ``files`` maps a nickname that a table decides to
    its ``.kicad_sym`` (a nickname absent from it is left to KiCad's folder), ``problems`` has one
    sentence per lib_id that cannot be resolved."""

    files: dict[str, Path] = field(default_factory=dict)
    sources: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def reason(self) -> str:
        return "; ".join(self.problems)


def _searched(project_table: Path, project_rows: dict[str, str] | None, globals_: list[Path], kicad_dirs: list[Path]) -> str:
    project = f"project sym-lib-table {project_table}" + ("" if project_rows is not None else " (absent or unreadable)")
    glob = ", ".join(f"global sym-lib-table {g}" for g in globals_) or "no global sym-lib-table"
    kicad = ", ".join(str(d) for d in kicad_dirs) or "none found"
    return f"searched the {project}; {glob}; KiCad's symbol folder ({kicad})"


def resolve_symbol_libraries(sheet: str | Path, lib_ids: Iterable[str]) -> SymbolLibResolution:
    """Resolve the nickname of every ``lib_id`` for a schematic ``sheet`` (see the module doc)."""
    sheet = Path(sheet)
    project = project_dir_of(sheet)
    kicad_dirs = kicad_symbol_dirs()
    roots = [project, *kicad_dirs]
    project_table = project / "sym-lib-table"
    project_rows = _rows(project_table) if project_table.is_file() else None
    globals_ = _global_tables()
    global_rows = [(t, _rows(t)) for t in globals_]

    out = SymbolLibResolution()
    # A table that exists but cannot be read must not be treated as absent: the lookup would fall
    # through to KiCad's folder and place a different symbol than the table names.
    unreadable = [t for t in ([project_table] if project_table.is_file() and project_rows is None else [])]
    unreadable += [t for t, rows in global_rows if rows is None]
    if unreadable:
        out.problems.append("sym-lib-table unreadable (cannot be parsed), nothing resolved: " + ", ".join(str(t) for t in unreadable))
        return out
    global_rows = [(t, rows or {}) for t, rows in global_rows]
    seen: set[str] = set()
    for lib_id in lib_ids:
        if not isinstance(lib_id, str) or ":" not in lib_id:
            out.problems.append(f"lib_id {lib_id!r} has no Library:Symbol form")
            continue
        nick = lib_id.split(":", 1)[0]
        if nick in seen:
            continue
        seen.add(nick)
        if not nick or any(sep in nick for sep in ("/", "\\")) or nick in (".", ".."):
            out.problems.append(f"library nickname {nick!r} in lib_id {lib_id!r} is not a plain name")
            continue

        decided: tuple[str, str, Path | None] | None = None  # (where, uri, expanded)
        if project_rows is not None and nick in project_rows:
            uri = project_rows[nick]
            decided = (f"project sym-lib-table {project_table}", uri, _expand(uri, project, kicad_dirs))
        else:
            for table, rows in global_rows:
                if nick in rows:
                    uri = rows[nick]
                    decided = (f"global sym-lib-table {table}", uri, _expand(uri, project, kicad_dirs))
                    break

        if decided is not None:
            where, uri, path = decided
            if path is None:
                out.problems.append(f"library {nick!r} ({where}) uses {uri!r}, which names a variable that is not set")
            elif not _inside(path, roots):
                out.problems.append(
                    f"library {nick!r} ({where}) points at {path}, outside the project folder {project} "
                    "and the KiCad install; it is not read"
                )
            elif not path.is_file():
                out.problems.append(f"library {nick!r} ({where}) points at {path}, which does not exist")
            elif path.name != f"{nick}.kicad_sym":
                out.problems.append(
                    f"library {nick!r} ({where}) points at {path.name}, but the symbol reader finds a library "
                    f"by the file name {nick}.kicad_sym; register the file under its own name as the nickname"
                )
            else:
                out.files[nick] = path
                out.sources[nick] = where
            continue

        if not any((d / f"{nick}.kicad_sym").is_file() for d in kicad_dirs):
            out.problems.append(
                f"unknown library nickname {nick!r} (lib_id {lib_id!r}): "
                + _searched(project_table, project_rows, globals_, kicad_dirs)
            )
    return out


def seed_symbol_defs(sch: Any, resolution: SymbolLibResolution, lib_ids: Iterable[str]) -> None:
    """Load each project-table symbol from its file into ``sch``'s symbol cache, so add_symbol and
    add_power use it. Raises kicad_tools' own symbol errors (library file named) for a missing symbol."""
    from kicad_tools.schematic.models.symbol import SymbolDef

    for lib_id in lib_ids:
        nick = lib_id.split(":", 1)[0]
        path = resolution.files.get(nick)
        if path is None or lib_id in sch._symbol_defs:
            continue
        try:
            sch._symbol_defs[lib_id] = SymbolDef.from_library(lib_id, lib_paths=[path.parent])
        except Exception as exc:  # noqa: BLE001 - re-raised with where the nickname came from
            raise ValueError(f"{exc} (library {nick!r} resolved through the {resolution.sources[nick]} to {path})") from exc
