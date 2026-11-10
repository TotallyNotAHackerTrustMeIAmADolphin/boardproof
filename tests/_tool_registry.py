"""Per-tool test registrations, so a new tool touches only its own files (GitHub issue 147).

Before this module every new writing tool edited the same central lists: ``WRITING_TOOLS``
(``test_write_guard.py``), the scenarios (``test_write_guard_tools.py``), the wording allow-list
(``test_description_gate_wording.py``) and ``tests/ratchets.toml``; parallel tickets collided on them.
Now a new tool adds:

* ``tests/tool_cases/<tool>.py`` with ``TOOL`` (the tool's name, equal to the file stem) and
  ``WRITES`` (True: a writing tool, False: a tool that writes no KiCad design file or library);
  a writing tool also has ``scenario(h)`` returning the lock scenario factory (``h`` is
  ``tests.test_write_guard_tools``, so ``h._editing(TOOL, lambda p: (...,))`` and the other
  helpers are at hand); a non-writing tool has ``NOT_WRITING_REASON``. Optional
  ``WORDING_ALLOWED``: {phrase: reason} for the claim-word allow-list of the tool's own description;
* ``tests/ratchets.d/<tool>.toml`` with the tool's ``[writing_tool.<tool>]`` (or
  ``[not_a_file_writer.<tool>]`` / ``[read_only_tool_writes.<tool>]``) entry, in the shape
  ``tests/ratchets.toml`` documents.

The tool itself needs no list: every function defined in ``toolsets/schematic.py``'s ``build`` is
registered, and ``@_read_only`` marks the read-only ones.

A name that is listed both here and in the central lists, a file whose name is not the tool's, a
writing tool without a scenario and an unknown section are errors, never merged silently.
"""

from __future__ import annotations

import importlib.util
import tomllib
from pathlib import Path
from types import ModuleType
from typing import Any

HERE = Path(__file__).parent
CASES_DIR = HERE / "tool_cases"
RATCHETS_DIR = HERE / "ratchets.d"

# The tables a per-tool ratchet file may hold, each keyed by the tool's name.
RATCHET_TABLES = ("writing_tool", "not_a_file_writer", "read_only_tool_writes")


class RegistryError(AssertionError):
    """A per-tool registration is malformed or collides with another."""


def load_cases(directory: Path | None = None) -> dict[str, ModuleType]:
    """Every ``tool_cases/<tool>.py`` module by tool name, each checked for the contract."""
    directory = CASES_DIR if directory is None else directory
    cases: dict[str, ModuleType] = {}
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"_tool_case_{path.stem}", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tool = getattr(module, "TOOL", None)
        if tool != path.stem:
            raise RegistryError(f"{path.name}: TOOL must be {path.stem!r} (the file's name), not {tool!r}")
        writes = getattr(module, "WRITES", None)
        if not isinstance(writes, bool):
            raise RegistryError(f"{path.name}: WRITES must be True (a writing tool) or False")
        if writes and not callable(getattr(module, "scenario", None)):
            raise RegistryError(f"{path.name}: a writing tool needs scenario(h) returning its lock scenario factory")
        if not writes and not (isinstance(getattr(module, "NOT_WRITING_REASON", None), str) and module.NOT_WRITING_REASON):
            raise RegistryError(f"{path.name}: a non-writing tool needs a NOT_WRITING_REASON")
        allowed = getattr(module, "WORDING_ALLOWED", {})
        if not isinstance(allowed, dict) or not all(isinstance(k, str) and isinstance(v, str) and v for k, v in allowed.items()):
            raise RegistryError(f"{path.name}: WORDING_ALLOWED must map a phrase to a non-empty reason")
        cases[tool] = module
    return cases


def writing_tools(cases: dict[str, ModuleType]) -> frozenset[str]:
    return frozenset(name for name, module in cases.items() if module.WRITES)


def not_writing_tools(cases: dict[str, ModuleType]) -> dict[str, str]:
    return {name: module.NOT_WRITING_REASON for name, module in cases.items() if not module.WRITES}


def wording_allowed(cases: dict[str, ModuleType]) -> dict[tuple[str, str], str]:
    return {(name, phrase): reason for name, module in cases.items() for phrase, reason in getattr(module, "WORDING_ALLOWED", {}).items()}


def merged(central: Any, extra: Any, what: str) -> Any:
    """``central`` plus ``extra`` (sets or dicts); a key in both is an error."""
    clash = sorted(set(central) & set(extra))
    if clash:
        raise RegistryError(f"{what}: {clash} listed both centrally and in a per-tool file; keep one")
    if isinstance(central, dict):
        return {**central, **extra}
    return frozenset(central) | frozenset(extra)


def load_ratchet_entries(directory: Path | None = None) -> dict[str, dict[str, dict]]:
    """{table: {tool: entry}} from ``ratchets.d/<tool>.toml``; the file's name is the tool's."""
    directory = RATCHETS_DIR if directory is None else directory
    tables: dict[str, dict[str, dict]] = {name: {} for name in RATCHET_TABLES}
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        if not data:
            raise RegistryError(f"{path.name}: empty")
        for table, entries in data.items():
            if table not in RATCHET_TABLES:
                raise RegistryError(f"{path.name}: unknown section [{table}]; a per-tool file holds {RATCHET_TABLES}")
            for tool, entry in entries.items():
                if tool != path.stem:
                    raise RegistryError(f"{path.name}: [{table}.{tool}] must be [{table}.{path.stem}] (the file's name is the tool's)")
                tables[table][tool] = entry
    return tables


def merge_ratchet_data(data: dict, extra: dict[str, dict[str, dict]]) -> dict:
    """``data`` (the central ratchets.toml) with the per-tool entries added; a tool in both is an error."""
    result = dict(data)
    for table, entries in extra.items():
        if not entries:
            continue
        result[table] = merged(data.get(table, {}), entries, f"ratchets [{table}]")
    return result
