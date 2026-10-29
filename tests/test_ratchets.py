"""Ratchet tests over ``src/kicad_mcp/`` (build ticket 29; docs/ARCHITECTURE.md decision 4).

Static scans with ``ast`` that keep two bug classes from coming back: silent writes and
unchecked convenience-property geometry. Each ratchet compares what the scan finds today with
the frozen list in ``ratchets.toml`` (next to this file):

1. **Pad-write ban.** No assignment to ``.size``, ``.drill``, ``.layers`` or ``.shape`` on any
   receiver, nor to ``net_number``, ``net_name``, ``roundrect_rratio``, ``solder_mask_margin``
   or ``position`` on a pad receiver (P3-13: up to 0.23.0 ``Pad.__setattr__`` mirrored only
   ``rotation``, so these were lost on save; every setter bypasses the write layer).
   The exempt list for non-pad look-alikes starts empty.
2. **Raw writes.** Every ``open(..., "w")``, ``write_text``, ``write_bytes``, ``os.write``,
   ``os.open``/``os.fdopen`` for writing, ``io.FileIO``/``gzip``/``bz2``/``lzma`` writes and
   ``tempfile`` file in ``src/``, each keyed on its own call text.
3. **Convenience reads.** ``.position``, ``.polygon``, ``.is_filled``, ``.filled_polygons`` and
   the same family (``board_origin``, ``_board_origin``, ``Segment.start``/``end``), as
   attributes or through ``getattr``, plus calls to ``get_pad_position(s)``/``pad_position``.
   CLAUDE.md hard rule 2 in code.
4. **High-level saves.** ``<x>.save(...)``, ``<x>.write(...)`` (not a file or buffer handle),
   ``save_project``, ``run_set_footprint``, ``run_fix_annotation``, each keyed on its call
   text. Calls into the reserved module ``kicad_mcp.write_layer`` (build ticket 42) are exempt.
5. **Writing tools.** Every registered tool that is not annotated read-only is classified, and
   a classified tool may not be annotated read-only. A writing tool without a proven
   reload-and-compare self-check is listed, and only the tools frozen on 2026-10-19 may be.
   Read-only tools and tools listed as writing no KiCad file have their reached write and
   commit sites (copies, renames, ``--save-board``) frozen per tool, and may not reach a
   KiCad-file write.

Lists are keyed by file and function (never line numbers). They may only shrink: a site that
is gone, or a count that fell, fails until the list says so. A failure names each site and how
to fix it.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import re
import sys
import tomllib
from collections import defaultdict
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "kicad_mcp"
DATA = Path(__file__).with_name("ratchets.toml")
DATA_NAME = "tests/ratchets.toml"

# kicad_tools' Pad setters bypass the write layer (decision 1). Up to 0.23.0 Pad.__setattr__
# mirrored only rotation, so these were lost on save; from 0.24.0 size, drill and shape reach
# the file and PCB.save refuses (ValueError) an edit it cannot persist (build ticket 71).
PAD_ATTRS = frozenset({"size", "drill", "layers", "shape"})  # banned on any receiver
PAD_RECEIVER_ATTRS = frozenset(
    {"net_number", "net_name", "roundrect_rratio", "solder_mask_margin", "position"}
)  # banned when the receiver's text names a pad (these names are common on other objects)
LIST_MUTATORS = frozenset({"append", "extend", "insert", "remove", "pop", "clear", "sort", "reverse"})
CONVENIENCE_ATTRS = frozenset(
    {"position", "polygon", "is_filled", "filled_polygons", "board_origin", "_board_origin", "start", "end"}
)
PAD_POSITION_CALLS = frozenset({"get_pad_position", "get_pad_positions", "pad_position"})
CONVENIENCE_PATHS = frozenset({"mutation", "advisory", "cross-checked", "kipy"})
SELF_CHECK_CLASSES = ("none", "return-value", "reload-observe", "reload-verify")
SAVE_FUNCTIONS = frozenset({"save_project", "run_set_footprint", "run_fix_annotation"})
TEMPFILE_FACTORIES = frozenset({"NamedTemporaryFile", "TemporaryFile", "SpooledTemporaryFile"})
HANDLE_FACTORIES = frozenset({"open", "fdopen", "FileIO", "StringIO", "BytesIO", *TEMPFILE_FACTORIES})
OPEN_FUNCTIONS = frozenset(
    {"open", "builtins.open", "io.open", "codecs.open", "gzip.open", "bz2.open", "lzma.open", "io.FileIO"}
)
OS_WRITE_FLAGS = frozenset({"O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC"})
SHUTIL_COMMITS = frozenset({"shutil.copy", "shutil.copy2", "shutil.copyfile", "shutil.copytree", "shutil.move"})
STDLIB_MODULES = frozenset({"os", "shutil", "io", "gzip", "bz2", "lzma", "builtins", "tempfile", "codecs"})
# Build ticket 74: what makes a raw write in tests/ a write of a KiCad file. Words are matched
# on the target expression split at every non-alphanumeric character (input_pcb -> input, pcb;
# "fp-lib-table" -> fp, lib, table; "x.kicad_pro" -> x, kicad, pro), heads on the start of a
# written literal. "kicad" alone is not a word: kicad-cli.exe is no KiCad file.
KICAD_TARGET_WORDS = frozenset(
    {"pcb", "sch", "board", "sheet", "schematic", "mod", "footprint", "fp", "sym", "dru", "pro", "prl", "wks", "table"}
)
KICAD_TEXT_HEADS = (
    "(kicad_", "(footprint", "(module", "(fp_lib_table", "(sym_lib_table", "(version",
)
KICAD_SERIALISERS = frozenset({"serialize_sexp", "serialise", "to_string", "to_sexp"})
TARGET_KEYWORDS = frozenset({"file", "path", "suffix", "prefix", "dir"})
TEST_WRITE_USES = ("fixture", "serialised", "fault", "stub", "copy")
FAULT_HELPER = "_kicad_faults.py"
HELPER_MODULE = FAULT_HELPER[:-3]
# What the fault-helper resolver returns for a path it must not or cannot follow (delta review 3).
VIA_HELPER_TESTS = ("<via the helper's tests>",)
UNRESOLVED = ("<unresolved>",)
# A string naming the helper as a module or a file: "tests._kicad_faults", "tests/_kicad_faults.py".
HELPER_PATH = re.compile(r"(?:^|[./\\])_kicad_faults(?:\.py)?$")
FAULT_HELPER_TESTS = "test_kicad_faults.py"
HELPER_TESTS_MODULE = FAULT_HELPER_TESTS[:-3]
# The fault helper's writers of a file KiCad would not write; every caller is listed (ticket 74).
FAULT_WRITERS = frozenset({"write_unchecked", "raw_atom", "rewrite_bytes"})
# The helper's one writer whose fault changes bytes, not tokens; its calls are not listed.
FAULT_HELPER_BENIGN = frozenset({"drift"})
# A name bound to one of these calls is still a path the scan follows (build ticket 74).
# A Path method called on the class takes the path as its first argument: Path.open(p).
PATH_CLASSES = frozenset({"Path", "PurePath", "pathlib.Path", "pathlib.PurePath"})
# The reads of an open handle whose result is the file's text (readlines: delta review 2).
HANDLE_READS = frozenset({"read", "readlines", "readline"})
# str(), os.fspath and os.path.join added in delta review 1.
PATH_BUILDERS = frozenset(
    {"Path", "PurePath", "with_suffix", "with_name", "joinpath", "resolve", "absolute", "str", "fspath", "join"}
)

# Build ticket 42's write layer. Calls that resolve to this module (or package) through an
# import are its sanctioned save path, not a high-level save.
WRITE_LAYER_MODULE = "kicad_mcp.write_layer"

# The writing tools that had no proven self-check when the ratchet was frozen (ticket 07's
# audit, 2026-10-19): 19 without a reload-and-compare check, and 4 whose check no test has
# yet seen fire (today 13 and 1). Only these may appear in the data file without ``reload-verify`` plus a
# proof. A new writing tool must self-check (decision 4), so a new name here is never the fix.
FROZEN_WITHOUT_PROVEN_SELF_CHECK = frozenset(
    {
        # none (8; the four schematic builders became reload-verify with proofs, build ticket 38;
        # register_library_asset, build ticket 76)
        "place_footprints",
        "align_footprints", "distribute_footprints", "snap_footprints_to_grid",
        "confirm_autoplacement", "add_traces", "project_create",
        "generate_footprint",
        # return-value (2)
        "move_reference_designators_to_layer", "fetch_part_assets",
        # reload-observe (3; confirm_route became reload-verify with a proof, build ticket 46)
        "fix_annotation", "pack_footprints_densely", "confirm_placement_optimization",
        # reload-verify, no test yet proves the check fires (1; build ticket 31 proved two,
        # build ticket 25 assign_footprints)
        "panelize_board",
    }
)

FIX_PAD = (
    "kicad_tools' Pad setters bypass the write layer (decision 1): up to 0.23.0 this change "
    "was lost on save (P3-13); from 0.24.0 PCB.save persists size, drill and shape and refuses "
    "other edits. Mutate the raw (size W H) / (drill D) / (layers ...) / (net ...) node through the "
    "S-expression API (node.set_value) and reload the saved file to confirm it. If the receiver "
    "is provably not a kicad_tools Pad, add the site to [[pad_write_exempt]] in "
    + "tests/ratchets.toml with a note, for the reviewer."
)
FIX_RAW_WRITE = (
    "Each raw write is reviewed once, keyed on its exact call text. If it writes a KiCad file "
    "(any .kicad_* file, fp-lib-table, sym-lib-table), route it through the write layer "
    "(docs/ARCHITECTURE.md decision 1) or the low-level kicad_tools API with a reload-and-compare "
    "self-check, never raw text. If it writes a scratch or export file, add it to [[raw_write]] "
    "in " + DATA_NAME + " with its code, kicad_file and a note saying what it writes, in the "
    "same change, for the reviewer."
)
FIX_CONVENIENCE = (
    "CLAUDE.md hard rule 2: kicad_tools' position/polygon/is_filled/filled_polygons/board_origin "
    "properties and pad-position helpers have been wrong by the board origin. Read the raw "
    "(at ...)/(polygon ...)/(filled_polygon ...) tokens instead (raw-token readers, build ticket "
    "43) or cross-check against them. If the read is advisory, already cross-checked, or on a "
    "kipy (IPC) object, add it to [[convenience_read]] in " + DATA_NAME + " with its path and a "
    "note, for the reviewer."
)
FIX_SAVE = (
    "New code does not save through kicad_tools' high-level models (docs/ARCHITECTURE.md decision 1: "
    "PCB.save failed on Windows in 0.22-0.23 and has no parse check or self-check, Schematic.write collapses hierarchies). Use the write "
    "layer (module kicad_mcp.write_layer, build ticket 42) or the low-level API with a "
    "reload-and-compare self-check."
)
FIX_COMMIT = (
    "A copy, rename, os.replace or --save-board puts a file in place without writing it, so a "
    "changed target can overwrite the caller's board from a read-only path. If it commits a "
    "verified temporary sibling of a KiCad file, that belongs to the write layer (decision 1); "
    "if it copies into or within a scratch directory, add it to [[commit]] in " + DATA_NAME +
    " with its code and a note saying what it copies where, for the reviewer."
)
# write_layer.save_bytes writes raw bytes to a lib table with the caller's verify as the only
# check (build ticket 76). Its callers are frozen here: (file, function) of each reference.
SAVE_BYTES_CALLERS = frozenset({("toolsets/library.py", "_write_table")})
FIX_SAVE_BYTES = (
    "write_layer.save_bytes is for fp-lib-table and sym-lib-table only and has no parse check of "
    "its own: a new caller needs its own reload-and-compare verify, a self-check proof test with a "
    "corrupted write, and a reviewed entry in SAVE_BYTES_CALLERS in tests/test_ratchets.py. A "
    "KiCad design file goes through write_layer.save (decision 1)."
)
FIX_SHRINK = (
    "The site is gone or fewer than listed: shrink the entry in " + DATA_NAME + " (lower its "
    "count, or delete it) in the same change. The list only shrinks."
)
FIX_SHRINK_TOOL = "Delete the tool's entry in " + DATA_NAME + " in the same change. The list only shrinks."
FIX_FROZEN = (
    "remove it from FROZEN_WITHOUT_PROVEN_SELF_CHECK in tests/test_ratchets.py in the same change "
    "(the set only shrinks)."
)


@dataclass(frozen=True)
class Site:
    file: str  # relative to src/kicad_mcp, POSIX separators
    function: str  # dotted qualname without <locals>; "<module>" at top level
    kind: str
    line: int
    code: str  # the full normalised call text (ast.unparse)
    root: str = "src/kicad_mcp"  # the scanned tree, for the failure message

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.file, self.function, self.kind)

    @property
    def code_key(self) -> tuple[str, str, str, str]:
        return (self.file, self.function, self.kind, self.code)

    def where(self) -> str:
        code = self.code if len(self.code) <= 120 else self.code[:117] + "..."
        return f"{self.root}/{self.file}:{self.line} in {self.function}: {code}"


# --------------------------------------------------------------------------- scanning


def _literal_str(node: ast.AST | None) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _call_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _dotted(node: ast.AST) -> str:
    """``os.path.join`` for a chain of Names and Attributes, else ""."""
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _is_self(node: ast.AST) -> bool:
    return isinstance(node, ast.Name) and node.id in ("self", "cls")


def _names_a_pad(node: ast.AST) -> bool:
    return "pad" in ast.unparse(node).lower()


def _arg(call: ast.Call, index: int, keyword: str) -> tuple[bool, ast.AST | None]:
    """The argument at ``index`` or passed as ``keyword``: (present, node)."""
    for kw in call.keywords:
        if kw.arg == keyword:
            return True, kw.value
    if len(call.args) > index and not any(isinstance(a, ast.Starred) for a in call.args[: index + 1]):
        return True, call.args[index]
    return False, None


def _mode_writes(mode: ast.AST | None) -> bool:
    text = _literal_str(mode)
    if text is None:
        return True  # not a literal: cannot prove it reads, so it counts as a write
    return any(ch in text for ch in "wax+")


def _dotted_module(rel: str) -> str:
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(["kicad_mcp", *parts])


def _kicad_imports(rel: str, tree: ast.Module) -> dict[str, tuple[str, str | None]]:
    """Local name -> (module, symbol or None) for every ``kicad_mcp`` import in a module,
    absolute or relative, at any depth (a function-local import counts module-wide)."""
    package = _dotted_module(rel).split(".")
    if not rel.endswith("__init__.py"):
        package = package[:-1]
    names: dict[str, tuple[str, str | None]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("kicad_mcp"):
                    names[alias.asname or alias.name] = (alias.name, None)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                base = ".".join(package[: len(package) - node.level + 1] + ([base] if base else []))
            if not base.startswith("kicad_mcp"):
                continue
            for alias in node.names:
                # Either a submodule or a symbol; callers try both.
                names[alias.asname or alias.name] = (base, alias.name)
    return names


def _in_write_layer(dotted: str) -> bool:
    return dotted == WRITE_LAYER_MODULE or dotted.startswith(WRITE_LAYER_MODULE + ".")


def _package(rel: str) -> list[str]:
    package = _dotted_module(rel).split(".")
    return package if rel.endswith("__init__.py") else package[:-1]


def _from_base(rel: str, node: ast.ImportFrom) -> str:
    """The absolute module an ``from ... import`` names, relative imports resolved."""
    base = node.module or ""
    if node.level:
        package = _package(rel)
        base = ".".join(package[: len(package) - node.level + 1] + ([base] if base else []))
    return base


def _stdlib_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> canonical dotted name for the stdlib modules the write scans match:
    ``import shutil as _sh`` gives ``_sh -> shutil``, ``from os import replace as rp`` gives
    ``rp -> os.replace``. Module-wide and flow-insensitive."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname and alias.name.split(".")[0] in STDLIB_MODULES:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and not node.level and (node.module or "").split(".")[0] in STDLIB_MODULES:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return aliases


class _Scanner(ast.NodeVisitor):
    """Walks one module, tracking the dotted function/class scope of every node."""

    root = "src/kicad_mcp"  # the tree the scanned files are relative to

    def __init__(self, file: str, modules: dict[str, ast.Module] | None = None) -> None:
        self.file = file
        self.modules = modules or {}
        self.scope: list[str] = []
        self.sites: list[Site] = []
        self.stdlib: dict[str, str] = {}

    def visit_Module(self, node: ast.Module) -> None:
        self.stdlib = _stdlib_aliases(node)
        self.enter(node)
        self.generic_visit(node)
        self.leave()

    def canon(self, node: ast.AST) -> str:
        """The dotted name of a call target with stdlib aliases resolved (``_os.replace`` ->
        ``os.replace``, ``copyfile`` -> ``shutil.copyfile``)."""
        dotted = _dotted(node)
        head, dot, rest = dotted.partition(".")
        if head in self.stdlib:
            return self.stdlib[head] + dot + rest
        return dotted

    def _function(self) -> str:
        return ".".join(self.scope) or "<module>"

    def hit(self, node: ast.AST, kind: str) -> None:
        code = ast.unparse(node).replace("\n", " ")
        self.sites.append(Site(self.file, self._function(), kind, node.lineno, code, self.root))

    def enter(self, node: ast.AST) -> None:
        """Hook: a module, function or class scope starts."""

    def leave(self) -> None:
        """Hook: the scope ends."""

    def _scoped(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        # Decorators, defaults, annotations and class bases run in the enclosing scope.
        for decorator in node.decorator_list:
            self.visit(decorator)
        if isinstance(node, ast.ClassDef):
            for base in [*node.bases, *node.keywords]:
                self.visit(base)
        else:
            self.visit(node.args)
            if node.returns is not None:
                self.visit(node.returns)
        self.scope.append(node.name)
        self.enter(node)
        for child in node.body:
            self.visit(child)
        self.leave()
        self.scope.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped


class PadWriteScanner(_Scanner):
    """Changes to a pad attribute that kicad_tools does not save (P3-13)."""

    def __init__(self, file: str, modules: dict[str, ast.Module] | None = None) -> None:
        super().__init__(file, modules)
        self.aliases: list[dict[str, str]] = [{}]  # local name -> pad attribute it holds

    def enter(self, node: ast.AST) -> None:
        self.aliases.append({})

    def leave(self) -> None:
        self.aliases.pop()

    def _banned(self, receiver: ast.AST, attr: str) -> bool:
        if _is_self(receiver):
            return False  # a class setting its own field is not a kicad_tools Pad
        return attr in PAD_ATTRS or (attr in PAD_RECEIVER_ATTRS and _names_a_pad(receiver))

    def _alias(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.aliases[-1].get(node.id)
        return None

    def _targets(self, target: ast.AST, statement: ast.AST) -> None:
        if isinstance(target, (ast.Tuple, ast.List)):
            for element in target.elts:
                self._targets(element, statement)
        elif isinstance(target, ast.Starred):
            self._targets(target.value, statement)
        elif isinstance(target, ast.Subscript):
            # pad.size[0] = ... (or an alias of it) changes the value in place: not saved either.
            alias = self._alias(target.value)
            if alias:
                self.hit(statement, f"assign .{alias}")
            else:
                self._targets(target.value, statement)
        elif isinstance(target, ast.Attribute) and self._banned(target.value, target.attr):
            self.hit(statement, f"assign .{target.attr}")

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._targets(target, node)
        value = node.value
        for target in node.targets:
            if isinstance(target, ast.Name):
                if isinstance(value, ast.Attribute) and value.attr in PAD_ATTRS and not _is_self(value.value):
                    self.aliases[-1][target.id] = value.attr  # layers = pad.layers
                else:
                    self.aliases[-1].pop(target.id, None)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._targets(node.target, node)
        alias = self._alias(node.target)
        if alias:
            self.hit(node, f"assign .{alias}")  # layers += [...] on an alias mutates the list
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._targets(node.target, node)
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._targets(node.target, node)
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_withitem(self, node: ast.withitem) -> None:
        if node.optional_vars is not None:
            self._targets(node.optional_vars, node.context_expr)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node.func)
        func = node.func
        if name in ("setattr", "object.__setattr__") and len(node.args) >= 2:
            attr = _literal_str(node.args[1])
            if attr and self._banned(node.args[0], attr):
                self.hit(node, f"setattr {attr}")
        elif isinstance(func, ast.Attribute) and func.attr in LIST_MUTATORS:
            receiver = func.value
            if isinstance(receiver, ast.Attribute) and receiver.attr in PAD_ATTRS and not _is_self(receiver.value):
                self.hit(node, f"mutate .{receiver.attr}")
            elif self._alias(receiver):
                self.hit(node, f"mutate .{self._alias(receiver)}")
        self.generic_visit(node)


class RawWriteScanner(_Scanner):
    """Calls that put new bytes or text into a file outside kicad_tools' writers."""

    def visit_Call(self, node: ast.Call) -> None:
        kind = self._kind(node)
        if kind:
            self.hit(node, kind)
        self.generic_visit(node)

    def _kind(self, node: ast.Call) -> str | None:
        func = node.func
        dotted = self.canon(func)
        name = dotted.rsplit(".", 1)[-1] if dotted else _call_name(func)
        # open(p, **kw) / open(p, *args): the mode cannot be read, so it counts as a write.
        unreadable = any(k.arg is None for k in node.keywords) or any(isinstance(a, ast.Starred) for a in node.args)
        if dotted in OPEN_FUNCTIONS:
            present, mode = _arg(node, 1, "mode")
            return "open" if (present and _mode_writes(mode)) or (not present and unreadable) else None
        if dotted == "os.fdopen":
            present, mode = _arg(node, 1, "mode")
            return "os.fdopen" if not present or _mode_writes(mode) else None
        if dotted == "os.write":
            return "os.write"
        if dotted == "os.open":
            present, flags = _arg(node, 1, "flags")
            if not present:
                return "os.open" if unreadable else None
            if isinstance(flags, ast.Constant) and isinstance(flags.value, int):
                return "os.open" if flags.value else None  # O_RDONLY is 0; any other bit writes
            attrs = {n.attr for n in ast.walk(flags) if isinstance(n, ast.Attribute)}
            variables = {n.id for n in ast.walk(flags) if isinstance(n, ast.Name)} - {"os"}
            # A write flag counts; so does a variable, whose flags cannot be read statically.
            unknown = {v for v in variables if not v.startswith("O_")}
            return "os.open" if (attrs | variables) & OS_WRITE_FLAGS or unknown else None
        if name in ("write_text", "write_bytes") and isinstance(func, ast.Attribute):
            return name
        if name in TEMPFILE_FACTORIES:
            present, mode = _arg(node, 0, "mode")
            return "tempfile" if not present or _mode_writes(mode) else None
        if name == "open" and isinstance(func, ast.Attribute):
            # Path.open(mode): only a literal write mode (or a non-literal mode=) counts, so
            # pdfplumber.open(path) and friends stay out.
            for kw in node.keywords:
                if kw.arg == "mode":
                    return "open" if _mode_writes(kw.value) else None
            if node.args:
                text = _literal_str(node.args[0])
                return "open" if text is not None and _mode_writes(node.args[0]) else None
        return None


class KicadTestWriteScanner(RawWriteScanner):
    """Raw writes in ``tests/`` that write a KiCad file (build ticket 74, CLAUDE.md hard rule 1).

    A raw write counts when the file it writes is named like a KiCad file (a word of
    ``KICAD_TARGET_WORDS`` in the target expression: ``pcb``, ``x.kicad_sch``,
    ``fp-lib-table``, ``board``), or when what it writes is KiCad text: a literal that starts
    with a KiCad root such as ``(kicad_pcb``, the output of a serialiser such as
    ``serialize_sexp``, or the text read from a KiCad-named path (``board.read_text()``,
    ``Path.read_text(board)``, ``open(board).read()``, ``"".join(Path.open(board).readlines())``,
    ``fh.read()`` of a handle opened on one). A
    name in the target or the payload is resolved up to two hops through its bindings: the
    assignments, loop and ``with`` targets of the enclosing functions, the test's
    ``pytest.mark.parametrize`` values, and the module's top-level constants, so
    ``target = project.parent / relative`` with ``relative`` parametrized over KiCad names
    counts. A name bound to a call is followed only through a path builder
    (``PATH_BUILDERS``: ``Path``, ``with_suffix``, ``str``, ``os.fspath``, ``os.path.join``,
    ...). A KiCad lock file (``.lck``) is JSON and does not count. In the fault helper
    (``tests/_kicad_faults.py``) every raw write counts, whatever its target is named.

    Blind spots, by design (a static scan reads names, not values): a target that reaches a
    KiCad name only through a function argument, a call's return value or more than two hops
    (``def touch(path): path.write_bytes(b"")``); a suffix assembled at run time or held in an
    attribute; ``Path.write_text(p, t)`` called on the class; an aliased method
    (``w = p.write_text; w(t)``); a temporary file moved onto a ``.kicad_*`` file with
    ``shutil.copy`` or ``os.replace``; plural directory names (``boards / name``); ``print(...,
    file=fh)``, and ``fh.write(text)`` on a handle opened under a generic name (the text is not
    an argument of the ``open``); a name bound to several targets (a loop, a parametrize) that
    mixes a lock file with other non-KiCad names is taken for a lock-file write, so its payload
    is not checked (``for t in [a / "x.lck", a / "y.txt"]: t.write_text(BOARD_TEXT)``); a
    payload read from a handle without a read method (``"".join(fh)``, ``for line in fh``,
    ``shutil.copyfileobj(src, dst)``) or from a handle bound more than two hops away. The
    ratchet lists what the scan finds; the review of each new test is what catches these."""

    root = "tests"

    def __init__(self, file: str, modules: dict[str, ast.Module] | None = None) -> None:
        super().__init__(file, modules)
        self.bindings: list[dict[str, list[ast.AST]]] = []

    def enter(self, node: ast.AST) -> None:
        self.bindings.append(_bindings(node))

    def leave(self) -> None:
        self.bindings.pop()

    def visit_Call(self, node: ast.Call) -> None:
        kind = self._kind(node)
        # Every write of the fault helper writes a KiCad file: that is its job.
        if kind and (self.file == FAULT_HELPER or self._writes_kicad(node, self.canon(node.func))):
            self.hit(node, kind)
        self.generic_visit(node)

    def _bound(self, name: str) -> list[ast.AST]:
        """What ``name`` is bound to in the innermost scope that binds it."""
        return next((frame[name] for frame in reversed(self.bindings) if name in frame), [])

    def _expand(self, expressions: list[ast.AST], hops: int = 2) -> list[ast.AST]:
        """``expressions`` plus what each name in them is bound to, ``hops`` levels deep."""
        out, frontier, seen = list(expressions), list(expressions), set()
        for _ in range(hops):
            reached: list[ast.AST] = []
            for expression in frontier:
                for node in ast.walk(expression):
                    if isinstance(node, ast.Name) and node.id not in seen:
                        seen.add(node.id)
                        reached.extend(self._bound(node.id))
            out.extend(reached)
            frontier = reached
        return out

    def _kicad_named(self, target: ast.AST, hops: int = 2) -> bool | None:
        """True when ``target`` names a KiCad file, None for a KiCad lock file, else False.
        A literal file name decides by its suffix; otherwise the words of the expression
        decide, then what its names are bound to (``hops`` levels deep). A name bound to a
        call's result is not followed (``krt.stop_file_for(output_pcb)`` is no board)."""
        leaf = _file_name_literal(target)
        if leaf is not None:
            return _file_name_verdict(leaf)
        if isinstance(target, (ast.List, ast.Tuple, ast.Set)):
            return _any_kicad(self._kicad_named(element, hops) for element in target.elts)
        if isinstance(target, ast.BinOp) and isinstance(target.op, ast.Div):
            name = self._kicad_named(target.right, hops)  # the last segment names the file
            if name is not False:
                return name
        text = ast.unparse(target)
        if re.search(r"\.lck\b", text, re.IGNORECASE):
            return None  # KiCad's lock file is JSON, written by the drift-guard tests
        if set(re.findall(r"[a-z0-9]+", text.lower())) & KICAD_TARGET_WORDS:
            return True
        if hops == 0:
            return False
        names = {node.id for node in ast.walk(target) if isinstance(node, ast.Name)}
        return _any_kicad(
            self._kicad_named(bound, hops - 1)
            for name in sorted(names)
            for bound in self._bound(name)
            if not isinstance(bound, ast.Call) or _call_name(bound.func) in PATH_BUILDERS
        )

    def _writes_kicad(self, call: ast.Call, dotted: str) -> bool:
        targets, payload = _target_and_payload(call, dotted)
        named = _any_kicad(self._kicad_named(target) for target in targets)
        if named is not False:
            return bool(named)
        for expression in self._expand(payload):
            for node in ast.walk(expression):
                if isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes)):
                    text = node.value.decode("latin-1") if isinstance(node.value, bytes) else node.value
                    if text.lstrip().startswith(KICAD_TEXT_HEADS):
                        return True
                if isinstance(node, ast.Call) and _call_name(node.func) in KICAD_SERIALISERS:
                    return True
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in ("read_text", "read_bytes")
                    and self._kicad_named(_receiver(node))
                ):
                    return True  # the text of a KiCad file, read and written back raw
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr in HANDLE_READS
                    and self._opens_kicad(node.func.value)
                ):
                    return True  # the same, through open(board).read() or fh.readlines()
        return False

    def _opens_kicad(self, handle: ast.AST, hops: int = 2) -> bool:
        """Whether ``handle``, an ``open(...)`` call or a name bound to one (``with
        board.open() as fh``), opens a KiCad-named path."""
        if isinstance(handle, ast.Call) and _call_name(handle.func) == "open":
            func = handle.func
            if isinstance(func, ast.Attribute) and self.canon(func) not in OPEN_FUNCTIONS:
                return bool(self._kicad_named(_receiver(handle)))  # path.open(), Path.open(path)
            return bool(handle.args) and bool(self._kicad_named(handle.args[0]))
        if isinstance(handle, ast.Name) and hops:
            return any(self._opens_kicad(bound, hops - 1) for bound in self._bound(handle.id))
        return False


def _receiver(call: ast.Call) -> ast.AST:
    """The path a ``Path`` method call acts on: ``p`` in ``p.read_text()`` and in
    ``Path.read_text(p)`` (the method called on the class)."""
    func = call.func
    assert isinstance(func, ast.Attribute)
    if _dotted(func.value) in PATH_CLASSES and call.args:
        return call.args[0]
    return func.value


def _any_kicad(verdicts) -> bool | None:
    """True if any verdict is True, else None if any names a lock file, else False."""
    verdicts = list(verdicts)
    if True in verdicts:
        return True
    return None if None in verdicts else False


def _file_name_literal(target: ast.AST) -> str | None:
    """The literal last segment of a path expression, when the code spells it:
    ``"a.kicad_sch"``, ``x / "a.kicad_sch"``, ``x.with_suffix(".kicad_pro")``,
    ``f"{name}.kicad_pro"`` (its literal parts)."""
    if isinstance(target, ast.Constant) and isinstance(target.value, str):
        return target.value
    if isinstance(target, ast.JoinedStr) and target.values:
        last = target.values[-1]
        if isinstance(last, ast.Constant) and isinstance(last.value, str) and "." in last.value:
            return "".join(v.value for v in target.values if isinstance(v, ast.Constant))
        return None
    if isinstance(target, ast.BinOp) and isinstance(target.op, ast.Div):
        return _file_name_literal(target.right)
    if isinstance(target, ast.Call) and _call_name(target.func) in ("with_suffix", "with_name") and target.args:
        return _file_name_literal(target.args[0])
    return None


def _file_name_verdict(name: str) -> bool | None:
    """A spelled file name: a KiCad suffix or library table is True, a lock file None,
    any other suffix False; a name without a suffix decides by its words."""
    name = name.strip().lower()
    if name.endswith(".lck"):
        return None
    if re.search(r"\.kicad_[a-z]+$", name) or name.endswith("lib-table"):
        return True
    if re.search(r"\.[a-z0-9]+$", name):
        return False
    return bool(set(re.findall(r"[a-z0-9]+", name)) & KICAD_TARGET_WORDS)


def _target_and_payload(call: ast.Call, dotted: str) -> tuple[list[ast.AST], list[ast.AST]]:
    """The expressions naming the written file, and the ones giving what is written.
    ``dotted`` is the call's target with stdlib aliases resolved (``io.open`` is not a
    method of a path)."""
    func = call.func
    path_method = dotted not in OPEN_FUNCTIONS and dotted.partition(".")[0] not in STDLIB_MODULES
    if isinstance(func, ast.Attribute) and func.attr in ("write_text", "write_bytes", "open") and path_method:
        return [func.value], [*call.args, *(k.value for k in call.keywords)]
    targets = call.args[:1] + [k.value for k in call.keywords if k.arg in TARGET_KEYWORDS]
    payload = call.args[1:] + [k.value for k in call.keywords if k.arg not in TARGET_KEYWORDS]
    return targets, payload


def _own_nodes(statements: list[ast.stmt]) -> list[ast.AST]:
    """Every node of ``statements`` outside nested functions, classes and lambdas."""
    nodes: list[ast.AST] = []
    stack: list[ast.AST] = list(statements)
    while stack:
        node = stack.pop()
        nodes.append(node)
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(node))
    return nodes


def _bind(bindings: dict[str, list[ast.AST]], target: ast.AST, value: ast.AST) -> None:
    if isinstance(target, ast.Name):
        bindings[target.id].append(value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        values = value.elts if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts) else None
        for i, element in enumerate(target.elts):
            _bind(bindings, element, values[i] if values else value)


def _parametrized(decorator: ast.AST) -> dict[str, list[ast.AST]]:
    """``pytest.mark.parametrize(names, values)``: each name with the values it takes."""
    if not (isinstance(decorator, ast.Call) and _call_name(decorator.func) == "parametrize"):
        return {}
    present, names = _arg(decorator, 0, "argnames")
    has_values, values = _arg(decorator, 1, "argvalues")
    if not (present and has_values):
        return {}
    if isinstance(names, ast.Constant) and isinstance(names.value, str):
        argnames = [n.strip() for n in names.value.split(",") if n.strip()]
    elif isinstance(names, (ast.Tuple, ast.List)):
        argnames = [n.value for n in names.elts if isinstance(n, ast.Constant) and isinstance(n.value, str)]
    else:
        return {}
    if len(argnames) == 1:
        return {argnames[0]: [values]}
    rows = values.elts if isinstance(values, (ast.Tuple, ast.List)) else []
    taken: dict[str, list[ast.AST]] = {name: [] for name in argnames}
    for row in rows:
        if isinstance(row, ast.Call) and _call_name(row.func) == "param":
            cells = row.args
        else:
            cells = row.elts if isinstance(row, (ast.Tuple, ast.List)) else []
        if len(cells) != len(argnames):
            return {name: [values] for name in argnames}  # a row the scan cannot split
        for name, cell in zip(argnames, cells, strict=True):
            taken[name].append(cell)
    return taken


def _bindings(scope: ast.AST) -> dict[str, list[ast.AST]]:
    """What each name of a scope is bound to: a function's own assignments, loop and ``with``
    targets and parametrize values; a module's or class's top-level assignments."""
    bindings: dict[str, list[ast.AST]] = defaultdict(list)
    if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
        for decorator in scope.decorator_list:
            for name, values in _parametrized(decorator).items():
                bindings[name].extend(values)
        nodes = _own_nodes(scope.body)
    elif isinstance(scope, (ast.Module, ast.ClassDef)):
        nodes = list(scope.body)
    else:
        nodes = []
    for node in nodes:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                _bind(bindings, target, node.value)
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            _bind(bindings, node.target, node.value)
        elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
            _bind(bindings, node.target, node.iter)
        elif isinstance(node, ast.withitem) and node.optional_vars is not None:
            _bind(bindings, node.optional_vars, node.context_expr)
    return dict(bindings)


def _import_refs(tree: ast.Module) -> dict[str, str]:
    """Local name -> the dotted path it was imported as, module-wide and flow-insensitive:
    ``from tests import _kicad_faults as faults`` gives ``faults -> tests._kicad_faults``,
    ``import tests._kicad_faults`` gives ``tests -> tests``. A relative import is taken from the
    ``tests`` package, the one package the scanned modules live in."""
    refs: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    refs[alias.asname] = alias.name
                else:
                    head = alias.name.split(".")[0]
                    refs[head] = head
        elif isinstance(node, ast.ImportFrom):
            base = ".".join(filter(None, ["tests" if node.level else "", node.module or ""]))
            for alias in node.names:
                if alias.name != "*":
                    refs[alias.asname or alias.name] = f"{base}.{alias.name}" if base else alias.name
    return refs


class _FaultHelperRefs(_Scanner):
    """Resolves an expression to what it names in the fault helper, if anything: the helper
    module itself (``faults``, ``tests._kicad_faults``) or one of its members
    (``faults.rewrite_bytes``, an imported ``rb``), also through a re-export by another test
    module (``from tests.test_x import faults``, the top-level ``from test_x import faults`` the
    suite uses since ``tests/`` has no ``__init__.py``, ``tests/sub/test_y.py`` included), followed for
    up to three modules. Anything reached through the helper's own tests
    (``tests.test_kicad_faults``) gives ``VIA_HELPER_TESTS``; a longer chain, or a ``tests.``
    path naming no scanned module, gives ``UNRESOLVED``."""

    root = "tests"

    def visit_Module(self, node: ast.Module) -> None:
        self.refs = _import_refs(node)
        super().visit_Module(node)

    def _exempt(self) -> bool:
        return self.file in (FAULT_HELPER, FAULT_HELPER_TESTS)

    def _dotted_ref(self, node: ast.AST) -> str | None:
        if isinstance(node, ast.Name):
            return self.refs.get(node.id)
        if isinstance(node, ast.Attribute):
            base = self._dotted_ref(node.value)
            return f"{base}.{node.attr}" if base else None
        return None

    def _resolve(self, dotted: str | None, hops: int = 2) -> tuple[str, ...] | None:
        """``()`` for the helper module, ``(member, ...)`` for a member, None for anything else."""
        if dotted is None:
            return None
        parts = dotted.split(".")
        if HELPER_TESTS_MODULE in parts:
            return VIA_HELPER_TESTS  # a wrapper there is exempt from both ratchets (delta review 3)
        if HELPER_MODULE in parts:
            return tuple(parts[parts.index(HELPER_MODULE) + 1:])
        if f"{parts[0]}.py" in self.modules or f"{parts[0]}/__init__.py" in self.modules:
            parts = ["tests", *parts]  # a top-level import between test modules (delta review 4)
        if parts[0] != "tests" or len(parts) < 2:
            return None
        if hops < 0:
            return UNRESOLVED  # a re-export chain longer than the scan follows: fail closed
        found = self._test_module(parts)
        if found is None:
            return UNRESOLVED  # a test module the scan does not have: fail closed
        key, i = found
        bound = _import_refs(self.modules[key]).get(parts[i]) if i < len(parts) else None
        if bound is None:
            return None  # the module itself, or a name it defines rather than imports
        return self._resolve(".".join([bound, *parts[i + 1:]]), hops - 1)

    def _test_module(self, parts: list[str]) -> tuple[str, int] | None:
        """The scanned module a ``tests.a.b.name`` path names (``a/b.py`` or ``a/b/__init__.py``,
        the longest that exists) and the index of the first part after it."""
        for i in range(len(parts), 0, -1):
            stem = "/".join(parts[1:i])
            for key in ((f"{stem}.py", f"{stem}/__init__.py") if stem else ("__init__.py",)):
                if key in self.modules:
                    return key, i
        return None

    def helper_ref(self, node: ast.AST) -> tuple[str, ...] | None:
        return self._resolve(self._dotted_ref(node))


class KicadFaultCallScanner(_FaultHelperRefs):
    """References to the fault helper's writers in ``tests/`` (build ticket 74): each call writes
    a KiCad file KiCad would not write, so each is listed by its text, and a new one needs a
    reviewed entry. A reference counts, not only a call: a writer imported under another name
    (``from tests._kicad_faults import rewrite_bytes as rb``) or re-exported by another test
    module (``from tests.test_x import faults`` or top-level ``from test_x import faults``), any
    attribute named like a writer even when its receiver is not resolved
    (``holder.write_unchecked``), a name bound to it (``w = rb``), one passed on (``functools.partial(rb, root)``) or
    fetched with ``getattr(faults, "raw_atom")``. Each of these is one site, keyed on its own
    text: the binding ``w = rb`` is the site, and a later call ``w(path)`` through the bound
    name, or a test-local wrapper (``def corrupt(p): return rb(p, f)``) called many times, is
    not listed again. The helper's own module and its tests are not listed. ``drift`` is not
    counted: its bytes change, its tokens do not. A reference to any other member of the
    helper, or one the scan cannot resolve (``getattr(faults, name)``, ``vars(faults)``,
    ``faults.__dict__``, ``importlib.import_module("tests._kicad_faults")``), fails
    ``FaultHelperUseScanner`` instead, so no writer of the helper is reached unlisted."""

    def _writer(self, node: ast.AST) -> str | None:
        if isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
            ref = self.helper_ref(node)
            if ref and ref[0] in FAULT_WRITERS:
                return ref[0]
            if isinstance(node, ast.Name) and node.id in FAULT_WRITERS:
                return node.id  # a bare writer name, imported or not
            if isinstance(node, ast.Attribute) and node.attr in FAULT_WRITERS:
                return node.attr  # named like a writer but unresolved: counts (fail closed)
        return None

    def visit_Call(self, node: ast.Call) -> None:
        writer = self._writer(node.func)
        if writer is None and _call_name(node.func) == "getattr" and len(node.args) >= 2:
            name = _literal_str(node.args[1])
            writer = name if name in FAULT_WRITERS else None
        if writer and not self._exempt():
            self.hit(node, writer)
        if self._writer(node.func):
            for child in [*node.args, *node.keywords]:  # the callee is this site, not another
                self.visit(child)
        else:
            self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        writer = self._writer(node)
        if writer and not self._exempt():
            self.hit(node, writer)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        writer = self._writer(node)
        if writer and not self._exempt():
            self.hit(node, writer)
            return
        self.generic_visit(node)


class FaultHelperUseScanner(_FaultHelperRefs):
    """Every reference through the fault helper outside its own module and tests (build ticket
    74, delta review 2), so a writer the helper exposes under any name is either listed by
    ``KicadFaultCallScanner`` or fails here. A member must be in ``FAULT_WRITERS`` or
    ``FAULT_HELPER_BENIGN`` (``faults.X``, ``from tests._kicad_faults import X``,
    ``getattr(faults, "X")``, also through a re-export). Fails closed on what it cannot
    resolve: the helper module used bare (``vars(faults)``, ``w = faults``, ``f(faults)``),
    ``getattr`` with a computed name, a star import from ``tests``, and any call argument or
    subscript that names the helper in a string (``importlib.import_module``, ``__import__``,
    ``sys.modules[...]``). Also a problem (delta review 3): any import from, or attribute
    reached through, the helper's own tests (``from tests.test_kicad_faults import corrupt``),
    since a wrapper defined there is exempt from both ratchets, and a reference the resolver
    cannot follow (a re-export chain over three modules, a ``tests.`` module it does not have).
    Blind spots of a static scan: a module name assembled at run time
    (``"_kicad" + "_faults"``), a reference reached past a member
    (``faults.drift.__globals__["rewrite_bytes"]``), and a dotted name resolved at run time from
    a string (``pkgutil.resolve_name``, ``pydoc.locate``)."""

    def _member(self, node: ast.AST, ref: tuple[str, ...]) -> None:
        if ref == VIA_HELPER_TESTS:
            self.hit(node, "via the helper's tests")
        elif ref == UNRESOLVED:
            self.hit(node, "unresolved reference")
        elif not ref:
            self.hit(node, "bare helper module")
        elif ref[0] not in FAULT_WRITERS | FAULT_HELPER_BENIGN:
            self.hit(node, f"unclassified {ref[0]}")

    def _names_helper(self, node: ast.AST) -> bool:
        text = _literal_str(node)
        return text is not None and HELPER_PATH.search(text) is not None

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._exempt():
            return
        base = ".".join(filter(None, ["tests" if node.level else "", node.module or ""]))
        for alias in node.names:
            if alias.name == "*":
                if base.split(".")[0] == "tests" or HELPER_MODULE in base:
                    self.hit(node, "star import")
                continue
            ref = self._resolve(f"{base}.{alias.name}")
            if ref:  # importing the module itself is fine; its uses are checked
                self._member(node, ref)

    def visit_Import(self, node: ast.Import) -> None:
        if self._exempt():
            return
        for alias in node.names:
            ref = self._resolve(alias.name)
            if ref:
                self._member(node, ref)

    def _expression(self, node: ast.Name | ast.Attribute) -> None:
        ref = self.helper_ref(node) if isinstance(node.ctx, ast.Load) else None
        if ref is None:
            self.generic_visit(node)
        elif not self._exempt():
            self._member(node, ref)

    def visit_Name(self, node: ast.Name) -> None:
        self._expression(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._expression(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._exempt():
            return
        rest: list[ast.AST] = [*node.args, *node.keywords]
        if _call_name(node.func) == "getattr" and node.args and self.helper_ref(node.args[0]) == ():
            name = _literal_str(node.args[1]) if len(node.args) >= 2 else None
            if name is None:
                self.hit(node, "dynamic helper access")
            else:
                self._member(node, (name,))
            rest = [*node.args[2:], *node.keywords]
        elif any(self._names_helper(arg) for arg in [*node.args, *(k.value for k in node.keywords)]):
            self.hit(node, "dynamic helper access")
        self.visit(node.func)
        for child in rest:
            self.visit(child)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        if not self._exempt() and self._names_helper(node.slice):
            self.hit(node, "dynamic helper access")
        self.generic_visit(node)


def fault_helper_writer_problems(helper: ast.Module) -> list[str]:
    """Every callable ``tests/_kicad_faults.py`` binds at top level, but ``drift``, must be in
    ``FAULT_WRITERS``, so a new wrapper (``replace_text`` around ``rewrite_bytes``) is classified
    when it is written. A callable is a ``def`` or a class, also under ``if``/``try``/``with``; a
    name imported from another ``tests`` module; a public assigned name; a private assigned name
    whose value is not plain data (``_r = rewrite_bytes``, a lambda, a call; annotated and
    tuple assigns included). A star import from ``tests`` is a problem. This check names a
    problem early; ``FaultHelperUseScanner`` is the guarantee, on the caller side."""
    defined: set[str] = set()
    problems: list[str] = []

    def data(value: ast.AST | None) -> bool:
        return value is None or not any(
            isinstance(n, (ast.Name, ast.Attribute, ast.Lambda, ast.Call)) for n in ast.walk(value)
        )

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for name in (n.id for t in targets for n in ast.walk(t) if isinstance(n, ast.Name)):
                    if not name.startswith("_") or not data(node.value):
                        defined.add(name)
            elif isinstance(node, ast.ImportFrom):
                module = ".".join(filter(None, ["tests" if node.level else "", node.module or ""]))
                for alias in node.names:
                    if module.split(".")[0] != "tests":
                        continue
                    if alias.name == "*":
                        problems.append(f"{FAULT_HELPER} has a star import from {module}: name each import")
                    else:
                        defined.add(alias.asname or alias.name)
            for field in ("body", "orelse", "finalbody", "handlers"):
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    walk([n for n in getattr(node, field, []) if isinstance(n, (ast.stmt, ast.excepthandler))])

    walk(helper.body)
    found = [
        f"{FAULT_HELPER} defines {name}, which is not in FAULT_WRITERS: a writer of a KiCad file KiCad "
        "would not write goes into FAULT_WRITERS, so every call of it is listed"
        for name in sorted(defined - FAULT_WRITERS - FAULT_HELPER_BENIGN)
    ]
    found += [f"FAULT_WRITERS names {name}, which {FAULT_HELPER} does not define" for name in sorted(FAULT_WRITERS - defined)]
    return found + problems


class SaveBytesScanner(_Scanner):
    """Every reference to ``save_bytes`` outside its definition: a call or alias by attribute
    or name, an import, and a lookup by its name as a string (``getattr``)."""

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "save_bytes":
            self.hit(node, "reference")

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == "save_bytes":
            self.hit(node, "reference")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if any(alias.name == "save_bytes" for alias in node.names):
            self.hit(node, "import")

    def visit_Constant(self, node: ast.Constant) -> None:
        if node.value == "save_bytes":
            self.hit(node, "by name")


class CommitScanner(_Scanner):
    """Calls that put a file in place without writing new bytes: renames, copies, and
    ``kicad-cli --save-board``. Used only by the tool-reachability checks."""

    def visit_Call(self, node: ast.Call) -> None:
        dotted = self.canon(node.func)
        name = _call_name(node.func)
        if dotted in ("os.replace", "os.rename") or dotted in SHUTIL_COMMITS:
            self.hit(node, dotted)
        elif isinstance(node.func, ast.Attribute) and name in ("replace", "rename") \
                and len(node.args) == 1 and not node.keywords and not dotted.startswith("os."):
            self.hit(node, f"Path.{name}")  # str.replace always takes two arguments
        self.generic_visit(node)

    def visit_Expr(self, node: ast.Expr) -> None:
        if isinstance(node.value, ast.Constant):
            return  # a docstring or bare string statement passes nothing to a process
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        if isinstance(node.value, str) and "--save-board" in node.value:
            self.hit(node, "--save-board")


class ConvenienceReadScanner(_Scanner):
    """Reads of kicad_tools' geometry convenience properties (CLAUDE.md hard rule 2)."""

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in CONVENIENCE_ATTRS and isinstance(node.ctx, ast.Load):
            self.hit(node, node.attr)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if isinstance(node.func, ast.Attribute):
            # m.start() / m.end() call a method; only the receiver is scanned.
            self.visit(node.func.value)
        else:
            self.visit(node.func)
        if _dotted(node.func) == "getattr" and len(node.args) >= 2:
            attr = _literal_str(node.args[1])
            if attr in CONVENIENCE_ATTRS:
                self.hit(node, attr)
        if _call_name(node.func) in PAD_POSITION_CALLS:
            self.hit(node, f"call {_call_name(node.func)}")
        for arg in node.args:
            self.visit(arg)
        for kw in node.keywords:
            self.visit(kw.value)


class SaveScanner(_Scanner):
    """Saves through kicad_tools' high-level models and CLI writers."""

    def __init__(self, file: str, modules: dict[str, ast.Module] | None = None) -> None:
        super().__init__(file, modules)
        self.handles: list[set[str]] = [set()]
        # One binding table per scope (module, functions, classes), innermost last.
        self.bindings: list[tuple[ast.AST, dict[str, tuple]]] = []
        # The write layer's modules (dotted name -> top-level defs), from the scanned tree.
        self.layer_defs = {
            _dotted_module(rel): {n.name for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
            for rel, tree in self.modules.items()
            if _in_write_layer(_dotted_module(rel))
        }

    def enter(self, node: ast.AST) -> None:
        self.handles.append(set())
        self.bindings.append((node, self._scope_bindings(node)))

    def leave(self) -> None:
        self.handles.pop()
        self.bindings.pop()

    def _scope_bindings(self, scope: ast.AST) -> dict[str, tuple]:
        """Every name a scope binds: ("import", module, symbol), ("def",) or ("other",).
        A name bound two different ways in one scope counts as ("other",)."""
        table: dict[str, tuple] = {}

        def bind(name: str, value: tuple) -> None:
            table[name] = value if table.get(name, value) == value else ("other",)

        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = scope.args
            for arg in [*args.posonlyargs, *args.args, *args.kwonlyargs, args.vararg, args.kwarg]:
                if arg is not None:
                    bind(arg.arg, ("other",))
        todo = list(scope.body)
        while todo:
            node = todo.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                bind(node.name, ("def",))
                todo.extend(node.decorator_list)
                continue  # its body is its own scope
            if isinstance(node, ast.ClassDef):
                bind(node.name, ("other",))
                continue
            if isinstance(node, ast.Lambda):
                continue
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.asname:
                        bind(alias.asname, ("import", alias.name, None))
                    else:
                        head = alias.name.split(".")[0]
                        bind(head, ("import", head, None))
            elif isinstance(node, ast.ImportFrom):
                base = _from_base(self.file, node)
                for alias in node.names:
                    bind(alias.asname or alias.name, ("import", base, alias.name))
            elif isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Load):
                bind(node.id, ("other",))
            elif isinstance(node, ast.arg):
                bind(node.arg, ("other",))
            todo.extend(ast.iter_child_nodes(node))
        return table

    def _lookup(self, name: str) -> tuple[tuple, int] | None:
        """The binding of ``name`` as Python resolves it, and the depth of its scope (0 is the
        module). A class body is not visible from the functions inside it."""
        for depth in range(len(self.bindings) - 1, -1, -1):
            node, table = self.bindings[depth]
            if isinstance(node, ast.ClassDef) and depth != len(self.bindings) - 1:
                continue
            if name in table:
                return table[name], depth
        return None

    @staticmethod
    def _handle_call(node: ast.AST) -> bool:
        return isinstance(node, ast.Call) and _call_name(node.func) in HANDLE_FACTORIES

    def visit_withitem(self, node: ast.withitem) -> None:
        if self._handle_call(node.context_expr) and isinstance(node.optional_vars, ast.Name):
            self.handles[-1].add(node.optional_vars.id)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        if self._handle_call(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self.handles[-1].add(target.id)  # buf = io.StringIO()
        self.generic_visit(node)

    def _is_handle(self, receiver: ast.AST) -> bool:
        if self._handle_call(receiver):
            return True  # io.StringIO().write(...)
        return isinstance(receiver, ast.Name) and any(receiver.id in h for h in self.handles)

    def _target(self, func: ast.AST) -> str | None:
        """The absolute dotted name a call resolves to through this scope's bindings, or None
        (a parameter, a local variable, an unresolvable expression)."""
        if isinstance(func, ast.Name):
            found = self._lookup(func.id)
            if found is None:
                return None
            (binding, depth) = found
            if binding[0] == "def" and depth == 0:
                return f"{_dotted_module(self.file)}.{func.id}"  # a module's own top-level def
            if binding[0] == "import" and binding[2] is not None:
                return f"{binding[1]}.{binding[2]}"
            return None
        if isinstance(func, ast.Attribute):
            dotted = _dotted(func.value)
            if not dotted:
                return None  # write_layer.load(p).save(...): a method on a returned object
            head, dot, rest = dotted.partition(".")
            found = self._lookup(head)
            if found is None or found[0][0] != "import":
                return None
            binding = found[0]
            base = binding[1] if binding[2] is None else f"{binding[1]}.{binding[2]}"
            return f"{base}{dot}{rest}.{func.attr}"
        return None

    def _via_write_layer(self, func: ast.AST) -> bool:
        """The call resolves to a top-level ``def`` of the write layer's own source (build
        ticket 42): not a class or object it re-exports, not a shadowed name."""
        target = self._target(func)
        if target is None:
            return False
        module, _, name = target.rpartition(".")
        return name in self.layer_defs.get(module, set())

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        name = _call_name(func)
        kind = None
        if isinstance(func, ast.Attribute) and name == "save":
            kind = "save"
        elif isinstance(func, ast.Attribute) and name == "write":
            receiver = func.value
            # os.write is a raw write (scanned there); handles and stdout/stderr are not models.
            if not self._is_handle(receiver) and self.canon(receiver) not in ("os", "sys.stdout", "sys.stderr"):
                kind = "write"
        elif name in SAVE_FUNCTIONS:
            kind = name
        if kind and not self._via_write_layer(func):
            self.hit(node, kind)
        self.generic_visit(node)


@cache
def _modules() -> dict[str, ast.Module]:
    modules = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        modules[rel] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return modules


def scan(scanner: type[_Scanner], modules: dict[str, ast.Module] | None = None) -> list[Site]:
    sites: list[Site] = []
    modules = modules if modules is not None else _modules()
    for rel, tree in modules.items():
        visitor = scanner(rel, modules)
        visitor.visit(tree)
        sites.extend(visitor.sites)
    return sites


# --------------------------------------------------------------------------- the ratchet


@cache
def _data() -> dict:
    with DATA.open("rb") as fh:
        return tomllib.load(fh)


def _frozen(section: str, *, by_code: bool = False) -> dict[tuple, dict]:
    """The section's entries by key; ``by_code`` sections key each site on its call text."""
    entries: dict[tuple, dict] = {}
    for entry in _data().get(section, []):
        key: tuple = (entry["file"], entry["function"], entry["kind"])
        if by_code:
            assert isinstance(entry.get("code"), str), f"{DATA_NAME} [[{section}]] {key}: needs code"
            key = (*key, entry["code"])
        assert key not in entries, f"{DATA_NAME} [[{section}]] lists {key} twice"
        count = entry.get("count", 1) if by_code else entry.get("count")
        assert isinstance(count, int) and count > 0, f"{DATA_NAME} {key}: count must be a positive int"
        assert entry.get("note"), f"{DATA_NAME} [[{section}]] {key}: every entry needs a note"
        entries[key] = {**entry, "count": count}
    return entries


def ratchet_failures(found: list[Site], frozen: dict[tuple, dict], *, fix: str, by_code: bool = False) -> list[str]:
    """Every new or grown site, and every listed site that is gone or shrank."""
    by_key: dict[tuple, list[Site]] = defaultdict(list)
    for site in found:
        by_key[site.code_key if by_code else site.key].append(site)
    problems: list[str] = []
    for key, sites in sorted(by_key.items()):
        listed = frozen.get(key, {}).get("count", 0)
        if len(sites) > listed:
            lines = "\n      ".join(s.where() for s in sites)
            problems.append(
                f"NEW: {key[2]} in {key[0]} {key[1]}: {len(sites)} found, {listed} listed\n"
                f"      {lines}\n    Fix: {fix}"
            )
    for key, entry in sorted(frozen.items()):
        have = len(by_key.get(key, []))
        if have < entry["count"]:
            code = f" `{key[3]}`" if by_code else ""
            problems.append(
                f"STALE: {key[2]}{code} in {key[0]} {key[1]}: {entry['count']} listed, {have} found.\n"
                f"    Fix: {FIX_SHRINK}"
            )
    return problems


def _assert_ratchet(name: str, found: list[Site], section: str, fix: str, *, by_code: bool = False) -> None:
    problems = ratchet_failures(found, _frozen(section, by_code=by_code), fix=fix, by_code=by_code)
    if problems:
        pytest.fail(f"{name} ratchet ({DATA_NAME} [[{section}]]):\n  " + "\n  ".join(problems), pytrace=False)


# --------------------------------------------------------------------------- the four scans


def test_no_pad_attribute_writes_but_the_exempt_list() -> None:
    _assert_ratchet("Pad-write ban (P3-13)", scan(PadWriteScanner), "pad_write_exempt", FIX_PAD)


def test_raw_writes_are_the_frozen_list() -> None:
    for entry in _data().get("raw_write", []):
        assert isinstance(entry.get("kicad_file"), bool), (
            f"{DATA_NAME} [[raw_write]] {entry.get('file')} {entry.get('function')}: kicad_file must be "
            "true or false"
        )
    _assert_ratchet("Raw-write", scan(RawWriteScanner), "raw_write", FIX_RAW_WRITE, by_code=True)


def test_convenience_property_reads_are_the_frozen_list() -> None:
    for entry in _frozen("convenience_read").values():
        assert entry.get("path") in CONVENIENCE_PATHS, (
            f"{DATA_NAME} convenience_read {entry['file']} {entry['function']}: path must be one of "
            f"{sorted(CONVENIENCE_PATHS)}"
        )
    _assert_ratchet("Convenience-read", scan(ConvenienceReadScanner), "convenience_read", FIX_CONVENIENCE)


def test_high_level_saves_are_the_frozen_list() -> None:
    _assert_ratchet("High-level save", scan(SaveScanner), "high_level_save", FIX_SAVE, by_code=True)


def test_commits_are_the_frozen_list() -> None:
    _assert_ratchet("Commit", scan(CommitScanner), "commit", FIX_COMMIT, by_code=True)


def test_save_bytes_has_only_the_frozen_callers() -> None:
    found = {(site.file, site.function) for site in scan(SaveBytesScanner)}
    new = sorted(found - SAVE_BYTES_CALLERS)
    assert not new, f"New callers of write_layer.save_bytes: {new}. {FIX_SAVE_BYTES}"
    gone = sorted(SAVE_BYTES_CALLERS - found)
    assert not gone, f"SAVE_BYTES_CALLERS lists callers that are gone: {gone}. Remove them in the same change."


# --------------------------------------------------------------------------- writing tools


@dataclass(frozen=True)
class Tool:
    name: str
    read_only: bool
    file: str  # relative to src/kicad_mcp
    function: str  # dotted qualname without <locals>


def _registered_tools() -> list[Tool]:
    """Every tool the server can register, with all toolsets loaded."""
    import inspect

    import kicad_mcp
    from mcp.client.client import Client

    from kicad_mcp.server import build_server

    package = Path(kicad_mcp.__file__).resolve().parent
    if os.path.normcase(str(package)) != os.path.normcase(str(SRC.resolve())):
        pytest.fail(
            f"kicad_mcp is imported from {package}, but this test scans {SRC}. In a worktree, run "
            "with PYTHONPATH=<worktree>/src (docs/TESTING.md, 'Test suite').",
            pytrace=False,
        )

    server = build_server()

    async def load_all() -> None:
        async with Client(server, mode="auto") as client:
            listing = await client.call_tool("list_toolsets", {})
            toolsets = listing.structured_content["result"]
            assert toolsets, "list_toolsets returned no toolsets"
            for toolset in toolsets:
                result = await client.call_tool("load_toolset", {"name": toolset["name"]})
                # The meta tool has no output schema: its dict comes back as JSON text.
                text = "".join(getattr(c, "text", "") for c in result.content)
                try:
                    status = json.loads(text).get("status")
                except ValueError:
                    status = None
                assert not result.is_error and status in ("loaded", "already_loaded"), (
                    f"load_toolset({toolset['name']!r}) returned {text[:200]!r}: the tool list would be incomplete"
                )

    asyncio.run(load_all())
    tools = []
    for tool in server._tool_manager.list_tools():
        fn = inspect.unwrap(tool.fn)
        assert fn.__name__ in (tool.name, "_" + tool.name), (
            f"tool {tool.name!r} unwraps to {fn.__qualname__}: the scan would follow the wrong function"
        )
        path = Path(inspect.getsourcefile(fn)).resolve()
        annotations = tool.annotations
        tools.append(
            Tool(
                name=tool.name,
                read_only=bool(annotations and annotations.read_only_hint),
                file=path.relative_to(package).as_posix(),
                function=fn.__qualname__.replace(".<locals>", ""),
            )
        )
    return tools


class _CallGraph:
    """Best-effort static call graph over ``src/kicad_mcp``, resolved by name.

    Resolves plain calls to functions in an enclosing scope or the module, imported
    ``kicad_mcp`` functions and modules (absolute or relative), and ``self.method``. Calls
    through variables, lambdas or ``functools.partial`` are not followed.
    """

    def __init__(self, modules: dict[str, ast.Module]) -> None:
        self.functions: dict[tuple[str, str], ast.AST] = {}
        self.imports: dict[str, dict[str, tuple[str, str | None]]] = {}
        for rel, tree in modules.items():
            self.imports[rel] = _kicad_imports(rel, tree)
            self._index(rel, tree, [])
        self.module_files = {_dotted_module(rel): rel for rel in modules}

    def _index(self, rel: str, node: ast.AST, scope: list[str]) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = [*scope, child.name]
                if not isinstance(child, ast.ClassDef):
                    self.functions[(rel, ".".join(inner))] = child
                self._index(rel, child, inner)
            else:
                self._index(rel, child, scope)

    def _lookup(self, module: str, name: str) -> tuple[str, str] | None:
        rel = self.module_files.get(module)
        if rel and (rel, name) in self.functions:
            return (rel, name)
        return None

    def resolve(self, rel: str, scope: str, func: ast.AST) -> tuple[str, str] | None:
        imports = self.imports[rel]
        if isinstance(func, ast.Name):
            parts = scope.split(".") if scope != "<module>" else []
            for depth in range(len(parts), -1, -1):
                candidate = ".".join([*parts[:depth], func.id])
                if (rel, candidate) in self.functions:
                    return (rel, candidate)
            if func.id in imports:
                module, symbol = imports[func.id]
                return self._lookup(module, symbol) if symbol else None
            return None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
            owner = func.value.id
            if owner in ("self", "cls"):
                parts = scope.split(".")
                for depth in range(len(parts) - 1, 0, -1):
                    candidate = ".".join([*parts[: depth - 1], parts[depth - 1], func.attr])
                    if (rel, candidate) in self.functions:
                        return (rel, candidate)
                return None
            if owner in imports:
                module, symbol = imports[owner]
                target = f"{module}.{symbol}" if symbol else module
                return self._lookup(target, func.attr)
        return None

    def callees(self, rel: str, function: str) -> set[tuple[str, str]]:
        """Functions called by name, plus every function nested inside this one (a closure
        defined in reached code is assumed to run: it may be passed as a callback)."""
        node = self.functions[(rel, function)]
        out = {key for key in self.functions if key[0] == rel and key[1].startswith(function + ".")}
        todo = list(ast.iter_child_nodes(node))
        while todo:
            child = todo.pop()
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # a nested def is its own node in the graph, resolved in its own scope
            if isinstance(child, ast.Call):
                target = self.resolve(rel, function, child.func)
                if target:
                    out.add(target)
            todo.extend(ast.iter_child_nodes(child))
        return out

    def reachable(self, start: tuple[str, str]) -> set[tuple[str, str]]:
        seen = {start}
        todo = [start]
        while todo:
            for callee in self.callees(*todo.pop()):
                if callee not in seen:
                    seen.add(callee)
                    todo.append(callee)
        return seen


def _write_sites_reached(graph: _CallGraph, tool: Tool, write_sites: list[Site]) -> list[Site]:
    start = (tool.file, tool.function)
    assert start in graph.functions, f"tool {tool.name}: {start} not found by the call graph"
    reached = graph.reachable(start)
    return [s for s in write_sites if (s.file, s.function) in reached]


def _reach_key(site: Site) -> str:
    return f"{site.file} | {site.function} | {site.kind}"


@pytest.fixture(scope="module")
def registered_tools() -> list[Tool]:
    return _registered_tools()


def _proof_problem(proof: str, tool: str, tool_modules: set[str]) -> str | None:
    """A proof is ``tests/test_x.py::test_name`` (or ``::Class::test_name``) naming a test that
    uses monkeypatch (or mock.patch) and mentions the tool or its module, itself or through a
    helper in the same file. Returns the problem, or None."""
    path_text, _, rest = proof.partition("::")
    path = REPO / path_text
    if not rest or not path.is_file():
        return f"proof {proof!r} does not name a test file and function."
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    nodes: list[ast.AST] = tree.body
    match = None
    for name in rest.split("::"):
        match = next((n for n in nodes if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                      and n.name == name), None)
        if match is None:
            return f"proof {proof!r} does not name an existing test function."
        nodes = match.body
    helpers = {n.name: n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    seen, todo, texts, patches = {match.name}, [match], [], False
    while todo:  # the test plus the same-file helpers it calls, transitively
        node = todo.pop()
        texts.append(ast.get_source_segment(source, node) or "")
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and child.id == "monkeypatch" and isinstance(child.ctx, ast.Load):
                patches = True  # monkeypatch is used, not merely requested
            if isinstance(child, ast.Call) and (_call_name(child.func) == "patch"
                                                or _dotted(child.func).endswith("patch.object")):
                patches = True
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
                helper = helpers.get(child.func.id)
                if helper is not None and helper.name not in seen:
                    seen.add(helper.name)
                    todo.append(helper)
    text = "\n".join(texts)
    if not patches:
        return f"proof {proof!r} never uses monkeypatch or mock.patch, so it cannot corrupt the write."
    words = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text))
    if tool not in words and not (words & tool_modules):
        return (f"proof {proof!r} mentions neither {tool!r} nor its module "
                f"({', '.join(sorted(tool_modules)) or 'none found'}).")
    return None


def _tool_modules(graph: _CallGraph, tool: str) -> set[str]:
    """Stems of the src modules that define the tool as a top-level function."""
    return {Path(rel).stem for (rel, name) in graph.functions if name in (tool, "_" + tool)}


def test_every_non_read_only_tool_is_classified(registered_tools: list[Tool]) -> None:
    data = _data()
    writing = data.get("writing_tool", {})
    not_writers = data.get("not_a_file_writer", {})
    registered = {t.name for t in registered_tools}
    problems = []
    for tool in registered_tools:
        if tool.read_only:
            if tool.name in writing or tool.name in not_writers:
                section = "writing_tool" if tool.name in writing else "not_a_file_writer"
                problems.append(
                    f"tool {tool.name!r} is classified under [{section}] but annotated read-only.\n"
                    f"    Fix: a tool that writes (a file, the live editor, a connection) is not "
                    f"read-only: restore readOnlyHint False. If it truly writes nothing now, delete "
                    f"its [{section}] entry in {DATA_NAME} (and, for a frozen writing tool, its name "
                    f"in FROZEN_WITHOUT_PROVEN_SELF_CHECK) in the same change."
                )
            continue
        if tool.name in writing or tool.name in not_writers:
            continue
        problems.append(
            f"NEW: tool {tool.name!r} ({tool.file} {tool.function}) is not annotated read-only and is "
            f"not classified.\n    Fix: if it writes a KiCad file, give it a reload-and-compare "
            f"self-check (decision 4) and a test that feeds it a corrupted write and shows it fires "
            f"(ticket 31's pattern), then add [writing_tool.{tool.name}] with self_check = "
            f"\"reload-verify\" and proof = [that test]. If it writes no KiCad file, add "
            f"[not_a_file_writer.{tool.name}] with the reason and the sites it reaches."
        )
    for name in sorted((writing.keys() | not_writers.keys()) - registered):
        problems.append(f"STALE: {name!r} is classified in {DATA_NAME} but no longer registered.\n    Fix: {FIX_SHRINK_TOOL}")
    for name in sorted(writing.keys() & not_writers.keys()):
        problems.append(f"{name!r} is both a writing tool and not a file writer in {DATA_NAME}.")
    if problems:
        pytest.fail("Tool classification:\n  " + "\n  ".join(problems), pytrace=False)


def test_writing_tools_without_a_proven_self_check_only_shrink() -> None:
    graph = _CallGraph(_modules())
    problems = []
    for name, entry in sorted(_data().get("writing_tool", {}).items()):
        check = entry.get("self_check")
        proof = entry.get("proof", [])
        if check not in SELF_CHECK_CLASSES:
            problems.append(f"{name}: self_check must be one of {SELF_CHECK_CLASSES}, not {check!r}.")
            continue
        if not entry.get("note"):
            problems.append(f"{name}: every entry needs a note.")
        if check != "reload-verify" and proof:
            problems.append(f"{name}: a proof needs self_check = \"reload-verify\".")
        for test in proof:
            problem = _proof_problem(test, name, _tool_modules(graph, name))
            if problem:
                problems.append(f"{name}: {problem}")
        proven = check == "reload-verify" and proof
        if not proven and name not in FROZEN_WITHOUT_PROVEN_SELF_CHECK:
            problems.append(
                f"NEW: writing tool {name!r} has self_check = {check!r} and {len(proof)} proof test(s). "
                f"Only the tools frozen on 2026-10-19 may lack a proven self-check.\n    Fix: give it a "
                f"reload-and-compare self-check (write a temporary sibling, reload, compare, then "
                f"os.replace; decision 4) and a test that corrupts the write and shows it fires, then "
                f"set self_check = \"reload-verify\" and list that test under proof."
            )
    if problems:
        pytest.fail("Writing-tool self-check list:\n  " + "\n  ".join(problems), pytrace=False)


def test_frozen_tools_without_a_proven_self_check_leave_the_constant_when_fixed() -> None:
    writing = _data().get("writing_tool", {})
    problems = []
    for name in sorted(FROZEN_WITHOUT_PROVEN_SELF_CHECK):
        entry = writing.get(name)
        if entry is None:
            problems.append(f"{name!r} is no longer a listed writing tool: " + FIX_FROZEN)
        elif entry.get("self_check") == "reload-verify" and entry.get("proof"):
            problems.append(f"{name!r} now has a proven self-check: " + FIX_FROZEN)
    if problems:
        pytest.fail("Shrink-only (test_ratchets.py):\n  " + "\n  ".join(problems), pytrace=False)


def test_tools_that_may_not_write_reach_only_their_frozen_sites(registered_tools: list[Tool]) -> None:
    """Read-only tools and tools listed as writing no KiCad file: the write and commit sites
    each one reaches are frozen per tool, and none may reach a KiCad-file write."""
    graph = _CallGraph(_modules())
    data = _data()
    read_only_listed = data.get("read_only_tool_writes", {})
    not_writers = data.get("not_a_file_writer", {})
    registered = {t.name: t for t in registered_tools}
    raw_entries = _frozen("raw_write", by_code=True)
    kicad_raw = {k[:3] for k, e in raw_entries.items() if e.get("kicad_file") and not e.get("read_only_reach")}
    raw_sites = scan(RawWriteScanner)
    save_sites = scan(SaveScanner)
    sites = raw_sites + save_sites + scan(CommitScanner)
    problems = []
    for tool in registered_tools:
        listed = read_only_listed if tool.read_only else not_writers
        if not tool.read_only and tool.name not in not_writers:
            continue  # a writing tool: its self-check is the guard
        reached = _write_sites_reached(graph, tool, sites)
        section = "read_only_tool_writes" if tool.read_only else "not_a_file_writer"
        for site in reached:
            if site in raw_sites and site.key in kicad_raw:
                problems.append(
                    f"{tool.name!r} ({section}) reaches a KiCad-file write: {site.where()}\n    Fix: a "
                    f"tool that writes a KiCad file is a writing tool: drop the read-only annotation or "
                    f"the [{section}] entry, give it a self-check (decision 4) and classify it under "
                    f"[writing_tool.{tool.name}]."
                )
            if not tool.read_only and site in save_sites:
                problems.append(
                    f"{tool.name!r} is listed under [not_a_file_writer] but reaches a high-level save: "
                    f"{site.where()}\n    Fix: classify it under [writing_tool.{tool.name}] instead."
                )
        if not reached and tool.name not in listed:
            continue
        if tool.name not in listed:
            lines = "\n      ".join(f"{s.where()} [{s.kind}]" for s in reached)
            problems.append(
                f"NEW: tool {tool.name!r} is annotated read-only but reaches write or commit sites:\n"
                f"      {lines}\n    Fix: if it writes or replaces a caller-visible KiCad file it is a "
                f"writing tool: drop the read-only annotation and give it a self-check (decision 4). If "
                f"it touches only scratch copies or exports, add [{section}.{tool.name}] to {DATA_NAME} "
                f"with the reason and these sites under reaches."
            )
            continue
        frozen = set(listed[tool.name].get("reaches", []))
        found = {_reach_key(s): s for s in reached}
        for key in sorted(found.keys() - frozen):
            problems.append(
                f"NEW: {tool.name!r} ({section}) now reaches {found[key].where()} [{found[key].kind}]\n"
                f"    Fix: if it writes the caller's file, the tool is a writing tool (self-check, "
                f"decision 4). If it is scratch only, add \"{key}\" to its reaches, for the reviewer."
            )
        for key in sorted(frozen - found.keys()):
            problems.append(f"STALE: {tool.name!r} ({section}) no longer reaches {key}.\n    Fix: {FIX_SHRINK}")
    for name in sorted(read_only_listed.keys() - registered.keys()):
        problems.append(f"STALE: {name!r} is listed but no longer registered.\n    Fix: {FIX_SHRINK_TOOL}")
    for name in sorted(read_only_listed.keys() & registered.keys()):
        if not registered[name].read_only:
            problems.append(f"{name!r} is listed under [read_only_tool_writes] but is not annotated read-only.")
    for name, entry in sorted({**read_only_listed, **not_writers}.items()):
        if not isinstance(entry, dict) or not entry.get("reason"):
            problems.append(f"{name!r}: each entry is a table with a reason (and reaches).")
    if problems:
        pytest.fail("Tools that may not write:\n  " + "\n  ".join(problems), pytrace=False)


# --------------------------------------------------------------------------- guard the guard
# A broken scanner must not pass as "no offenders". Each scanner is shown to catch the forms
# it claims and to leave the look-alikes alone.


def _scan_text(scanner: type[_Scanner], text: str, rel: str = "probe.py") -> list[tuple[str, str]]:
    return [(s.function, s.kind) for s in scan(scanner, {rel: ast.parse(text)})]


def test_pad_scanner_catches_every_assignment_form() -> None:
    text = """
def f(pad, pads, other, fp, item):
    pad.size = (1, 1)
    pad.drill += 0.1
    pad.layers: list = ["F.Cu"]
    a, pads[0].shape = 1, "rect"
    setattr(pad, "size", (2, 2))
    object.__setattr__(pad, "drill", 0.3)
    for pad.layers in []: pass
    pad.size[0] = 1.0
    pad.layers.append("B.Cu")
    pad.net_name = "GND"
    pads[0].net_number = 3
    setattr(pad, "solder_mask_margin", 0.05)
    pad.position = (1, 2)
    layers = pad.layers
    layers.append("In1.Cu")
    layers[0] = "F.Mask"
    x = pad.size            # a read
    pad.rotation = 90       # mirrored by kicad_tools
    setattr(pad, name, 1)   # not a literal
    pads.sort()             # not a pad attribute
    fp.position = (1, 2)    # not a pad receiver (IPC footprint)
    item.net_name = "x"     # not a pad receiver
    layers = []
    layers.append("ok")     # rebound: no longer the pad's list
def g(arg=setattr(pad, "drill", 1)):  # a default runs in the enclosing scope
    pass
class Own:
    def __init__(self):
        self.size = 3       # a class's own field
        self.layers.append(1)
"""
    assert _scan_text(PadWriteScanner, text) == [
        ("f", "assign .size"), ("f", "assign .drill"), ("f", "assign .layers"), ("f", "assign .shape"),
        ("f", "setattr size"), ("f", "setattr drill"), ("f", "assign .layers"), ("f", "assign .size"),
        ("f", "mutate .layers"), ("f", "assign .net_name"), ("f", "assign .net_number"),
        ("f", "setattr solder_mask_margin"), ("f", "assign .position"), ("f", "mutate .layers"),
        ("f", "assign .layers"), ("<module>", "setattr drill"),
    ]


def test_raw_write_scanner_catches_every_write_form() -> None:
    text = """
import os, io, tempfile
def f(p, mode, flags):
    open(p, "w")
    open(p, mode="ab")
    open(p, mode)
    builtins.open(p, "a")
    io.open(p, "x")
    io.FileIO(p, "w")
    gzip.open(p, "wt")
    p.open("w+")
    p.open(mode=mode)
    p.write_text("x")
    p.write_bytes(b"x")
    os.write(3, b"x")
    os.open(p, os.O_WRONLY | os.O_CREAT)
    os.open(p, flags)
    os.fdopen(3, "w")
    tempfile.NamedTemporaryFile("w", suffix=".ses")
    tempfile.NamedTemporaryFile(suffix=".bin")
def g(p):
    open(p)
    open(p, "rb")
    io.FileIO(p)
    gzip.open(p)
    p.open()
    p.open("r")
    pdfplumber.open(p)
    os.open(p, os.O_RDONLY)
    os.open(p, 0)
    p.read_text()
def h(log=open("x.log", "w")):
    pass
def k(p, kw, args):
    from os import write as w
    import io as _io
    from tempfile import NamedTemporaryFile as NTF
    w(3, b"x")
    _io.open(p, "w")
    NTF()
    os.open(p, 1)
    open(p, **kw)
    open(*args)
"""
    assert _scan_text(RawWriteScanner, text) == [
        ("f", "open"), ("f", "open"), ("f", "open"), ("f", "open"), ("f", "open"), ("f", "open"),
        ("f", "open"), ("f", "open"), ("f", "open"), ("f", "write_text"), ("f", "write_bytes"),
        ("f", "os.write"), ("f", "os.open"), ("f", "os.open"), ("f", "os.fdopen"), ("f", "tempfile"),
        ("f", "tempfile"), ("<module>", "open"),
        ("k", "os.write"), ("k", "open"), ("k", "tempfile"), ("k", "os.open"), ("k", "open"), ("k", "open"),
    ]


def test_commit_scanner_catches_renames_copies_and_save_board() -> None:
    text = """
def f(tmp, dest, s):
    os.replace(tmp, dest)
    os.rename(tmp, dest)
    tmp.replace(dest)
    tmp.rename(dest)
    shutil.copy(tmp, dest)
    shutil.copytree(tmp, dest)
    shutil.move(tmp, dest)
    run(["kicad-cli", "pcb", "drc", "--save-board", str(dest)])
    "never pass --save-board here"   # a bare string statement
    s.replace("a", "b")       # str.replace
    shutil.rmtree(dest)
def g(a, b):
    import shutil as _sh
    import os as _os
    from shutil import copyfile
    from os import replace as rp
    _sh.copy2(a, b)
    _os.rename(a, b)
    copyfile(a, b)
    rp(a, b)
"""
    assert _scan_text(CommitScanner, text) == [
        ("f", "os.replace"), ("f", "os.rename"), ("f", "Path.replace"), ("f", "Path.rename"),
        ("f", "shutil.copy"), ("f", "shutil.copytree"), ("f", "shutil.move"), ("f", "--save-board"),
        ("g", "shutil.copy2"), ("g", "os.rename"), ("g", "shutil.copyfile"), ("g", "os.replace"),
    ]


def test_convenience_scanner_catches_attribute_and_getattr_reads() -> None:
    text = """
def f(fp, zone, seg, board, m, pcb):
    a = fp.position
    b = zone.polygon[0]
    if getattr(zone, "is_filled", False): pass
    c = board.board_origin
    d = board._board_origin
    e = (seg.start, seg.end)
    g = getattr(zone, "filled_polygons", None)
    h = pcb.get_pad_position("R1", "1")
    i = pad_position(fp, "1")
    m.start(); m.end()          # method calls, not properties
    fp.position = (1, 2)        # a store, not a read
    getattr(fp, "reference")
class K:
    def g(self, pad):
        return pad.position
"""
    assert _scan_text(ConvenienceReadScanner, text) == [
        ("f", "position"), ("f", "polygon"), ("f", "is_filled"), ("f", "board_origin"),
        ("f", "_board_origin"), ("f", "start"), ("f", "end"), ("f", "filled_polygons"),
        ("f", "call get_pad_position"), ("f", "call pad_position"), ("K.g", "position"),
    ]


def test_save_scanner_catches_saves_and_skips_file_handles() -> None:
    text = """
import io, sys, tempfile
def build():
    def tool(board, sch, path):
        board.save(path)
        sch.write(path)
        save_project(data, path)
        kicad_tools.cli.run_set_footprint(path)
        run_fix_annotation(path)
        with open(path, "w") as fh:
            fh.write("x")
        with tempfile.NamedTemporaryFile("w") as tmp:
            tmp.write("x")
        with path.open("w") as out:
            out.write("x")
        sys.stdout.write("x")
        io.StringIO().write("x")
        buf = io.BytesIO()
        buf.write(b"x")
        def inner():
            fh.write("y")        # the enclosing scope's handle
        os.write(1, b"x")
        import os as _os
        _os.write(1, b"x")       # os.write through an alias is a raw write, not a save
"""
    assert _scan_text(SaveScanner, text) == [
        ("build.tool", "save"), ("build.tool", "write"), ("build.tool", "save_project"),
        ("build.tool", "run_set_footprint"), ("build.tool", "run_fix_annotation"),
    ]


def test_save_scanner_exempts_only_calls_into_the_write_layer() -> None:
    layer_init = (
        "from kicad_tools.schema import PCB\n"
        "schematic = None\n"
        "def save(doc, path):\n    pass\n"
        "def save_project(doc, path):\n    pass\n"
    )
    layer_core = "def save(doc, path):\n    pass\ndef commit(doc, path):\n    pass\n"
    caller = """
from kicad_mcp import write_layer
from kicad_mcp import write_layer as wl
from kicad_mcp.write_layer import save, save_project
from kicad_mcp.write_layer import save_project as sp
from kicad_mcp.write_layer import PCB
import kicad_mcp.write_layer
from ..write_layer import core
def f(doc, board, path):
    write_layer.save(doc, path)        # exempt: a def of the layer
    wl.save(doc, path)                 # exempt
    save_project(doc, path)            # exempt
    sp(doc, path)                      # exempt: an alias of a real def
    kicad_mcp.write_layer.save(doc, path)  # exempt
    core.save(doc, path)               # exempt: a def of a layer submodule
    board.save(path)                   # still a high-level save
    doc = write_layer.load(path)
    doc.save(path)                     # a method on a returned object
    PCB.save(path)                     # a class the layer re-exports
    write_layer.PCB.save(path)         # the same, through the module
def g(schematic, path):
    schematic.write(path)              # a parameter, though h() imports a layer name "schematic"
def h():
    from kicad_mcp.write_layer import schematic
    return schematic
def k(path):
    schematic = make(path)
    schematic.write(path)              # a local variable
def m(path, save_project):
    save_project(None, path)           # a parameter shadows the module-level import
"""
    modules = {
        "write_layer/__init__.py": ast.parse(layer_init),
        "write_layer/core.py": ast.parse(layer_core),
        "toolsets/demo.py": ast.parse(caller),
    }
    found = [(s.function, s.kind, s.code) for s in scan(SaveScanner, modules) if s.file == "toolsets/demo.py"]
    assert found == [
        ("f", "save", "board.save(path)"), ("f", "save", "doc.save(path)"), ("f", "save", "PCB.save(path)"),
        ("f", "save", "write_layer.PCB.save(path)"), ("g", "write", "schematic.write(path)"),
        ("k", "write", "schematic.write(path)"), ("m", "save_project", "save_project(None, path)"),
    ]
    layer = """
def save_project(doc, path):
    pass
def commit(doc, path, pcb):
    save_project(doc, path)        # the layer's own function
    pcb.save(path)                 # a high-level save inside the layer still fails
"""
    assert _scan_text(SaveScanner, layer, "write_layer.py") == [("commit", "save")]
    elsewhere = "def save_project(d, p):\n    pass\ndef g(d, p):\n    save_project(d, p)\n"
    assert _scan_text(SaveScanner, elsewhere, "toolsets/other.py") == [("g", "save_project")]


def test_save_bytes_scanner_catches_a_new_caller_in_every_form() -> None:
    # Review round 1 of build ticket 76: a place_footprints mutation through save_bytes passed
    # every ratchet. Each form below must show up as a caller outside SAVE_BYTES_CALLERS.
    text = """
from kicad_mcp.write_layer import save_bytes as raw_save

def place(write_layer, board):
    write_layer.save_bytes(b"(kicad_pcb)", board, verify=print)

def alias(write_layer):
    save = write_layer.save_bytes
    return save

def by_name(write_layer):
    return getattr(write_layer, "save_bytes")

def bare(board):
    save_bytes(b"", board, verify=print)

def look_alikes(layer, board):
    layer.save_byte(board)
    layer.save(board)
    return "calls save_bytes elsewhere"
"""
    assert _scan_text(SaveBytesScanner, text, "toolsets/placement.py") == [
        ("<module>", "import"),
        ("place", "reference"),
        ("alias", "reference"),
        ("by_name", "by name"),
        ("bare", "reference"),
    ]
    found = {(s.file, s.function) for s in scan(SaveBytesScanner, {"toolsets/placement.py": ast.parse(text)})}
    assert not found & SAVE_BYTES_CALLERS


def test_ratchet_reports_new_grown_gone_and_shrunk_sites() -> None:
    def site(function: str, line: int, code: str = "p.write_text(x)") -> Site:
        return Site("m.py", function, "write_text", line, code)

    frozen = {
        ("m.py", "kept", "write_text"): {"count": 1},
        ("m.py", "grown", "write_text"): {"count": 1},
        ("m.py", "shrunk", "write_text"): {"count": 2},
        ("m.py", "gone", "write_text"): {"count": 1},
    }
    found = [site("kept", 1), site("grown", 2), site("grown", 3), site("shrunk", 4), site("new", 5)]
    problems = ratchet_failures(found, frozen, fix="FIX")
    heads = sorted(p.split(":")[0] + ":" + p.split(" in m.py ")[1].split(":")[0] for p in problems)
    assert heads == ["NEW:grown", "NEW:new", "STALE:gone", "STALE:shrunk"]
    assert all("Fix:" in p for p in problems)
    assert any("src/kicad_mcp/m.py:5 in new" in p for p in problems)


def test_code_keyed_ratchet_catches_a_retargeted_site() -> None:
    frozen = {("m.py", "f", "open", "open(log, 'w')"): {"count": 1}}
    ok = [Site("m.py", "f", "open", 1, "open(log, 'w')")]
    moved = [Site("m.py", "f", "open", 1, "open(output_pcb, 'w')")]
    assert ratchet_failures(ok, frozen, fix="FIX", by_code=True) == []
    problems = ratchet_failures(moved, frozen, fix="FIX", by_code=True)
    assert [p.split(":")[0] for p in problems] == ["NEW", "STALE"]
    assert "open(output_pcb, 'w')" in problems[0]


def test_call_graph_reaches_writes_through_helpers_and_module_imports() -> None:
    modules = {
        "toolsets/demo.py": ast.parse(
            "from kicad_mcp import helper\n"
            "from .other import write_it\n"
            "def build(mcp):\n"
            "    def tool(p):\n"
            "        return _local(p)\n"
            "    def tool2(p):\n"
            "        helper.do(p)\n"
            "    def tool3(p):\n"
            "        write_it(p)\n"
            "def _local(p):\n"
            "    p.write_text('x')\n"
        ),
        "helper.py": ast.parse("def do(p):\n    board.save(p)\n"),
        "toolsets/other.py": ast.parse(
            "class W:\n"
            "    def run(self, p):\n"
            "        self.inner(p)\n"
            "    def inner(self, p):\n"
            "        open(p, 'w')\n"
            "def write_it(p):\n"
            "    W().run(p)\n"
        ),
    }
    graph = _CallGraph(modules)
    assert ("toolsets/demo.py", "_local") in graph.reachable(("toolsets/demo.py", "build.tool"))
    assert ("helper.py", "do") in graph.reachable(("toolsets/demo.py", "build.tool2"))
    assert ("toolsets/other.py", "write_it") in graph.reachable(("toolsets/demo.py", "build.tool3"))
    assert ("toolsets/other.py", "W.inner") in graph.reachable(("toolsets/other.py", "W.run"))


def test_proof_must_patch_and_name_the_tool(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_p.py").write_text(
        "def _run(board):\n    return board_rules.confirm_board_rules(board)\n"
        "def test_good(board, monkeypatch):\n    monkeypatch.setattr(x, 'y', 1)\n    _run(board)\n"
        "def test_no_patch(board):\n    confirm_board_rules(board)\n"
        "def test_unused_fixture(board, monkeypatch):\n    confirm_board_rules(board)\n"
        "def test_unrelated(monkeypatch):\n    monkeypatch.setattr(x, 'y', 1)\n    assert 1\n",
        encoding="utf-8",
    )
    modules = {"board_rules"}
    assert _proof_problem("tests/test_p.py::test_good", "confirm_board_rules", modules) is None
    assert "monkeypatch" in _proof_problem("tests/test_p.py::test_no_patch", "confirm_board_rules", modules)
    assert "monkeypatch" in _proof_problem("tests/test_p.py::test_unused_fixture", "confirm_board_rules", modules)
    assert "mentions neither" in _proof_problem("tests/test_p.py::test_unrelated", "confirm_board_rules", modules)
    assert "existing" in _proof_problem("tests/test_p.py::test_missing", "confirm_board_rules", modules)


def test_scanners_see_the_source_tree() -> None:
    modules = _modules()
    assert len(modules) >= 40, f"only {len(modules)} modules under {SRC}"
    assert "toolsets/routing.py" in modules and "server.py" in modules


# --------------------------------------------------------------------------- tests/ (build ticket 74)
# CLAUDE.md hard rule 1 holds for tests too: a test builds its KiCad variants through the write
# layer (kicad_mcp.write_layer: load, change the tree with the typed setters, save), never by
# editing text, because a hand edit can build a file KiCad would never write and the test then
# proves less than it seems. A test that must write a broken or drifted file on purpose does it
# through tests/_kicad_faults.py, the one sanctioned fault-injection helper.

TESTS = REPO / "tests"
FIX_TEST_WRITE = (
    "CLAUDE.md hard rule 1: a test does not write a KiCad file as raw text. Build the variant "
    "through the write layer: write_layer.load(path), change the tree (write_layer.set_string / "
    "set_number / set_symbol, SExp.append / remove), write_layer.save(root, path). To change a "
    "file on purpose in a way KiCad would never write it (drift, a raw line break, a truncated "
    "file), call a helper of tests/_kicad_faults.py, adding one there if none fits. If the "
    "scan's match is not a KiCad file, or a write cannot move yet, add [[test_kicad_write]] to "
    + DATA_NAME + " with its count, a use (" + ", ".join(TEST_WRITE_USES) + ") and a note, "
    "for the reviewer."
)


@cache
def _test_modules() -> dict[str, ast.Module]:
    modules = {}
    for path in sorted(TESTS.rglob("*.py")):
        rel = path.relative_to(TESTS).as_posix()
        modules[rel] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return modules


FIX_FAULT_CALL = (
    "A call of a tests/_kicad_faults.py writer (write_unchecked, raw_atom, rewrite_bytes) writes "
    "a KiCad file KiCad would not write. Check that the test needs that fault, that a "
    "write_layer.save could not build the same case, and that the change is the one the test "
    "names; then add [[test_kicad_fault_call]] to " + DATA_NAME + " with file, function, kind, "
    "code and a note."
)


def kicad_write_entry_problems(entries: list[dict]) -> list[str]:
    """The schema of ``[[test_kicad_write]]`` beyond ``_frozen``'s: every entry says what the
    write is for (``use``), and every write of the fault helper is a fault."""
    problems = []
    for entry in entries:
        key = (entry.get("file"), entry.get("function"), entry.get("kind"))
        if entry.get("use") not in TEST_WRITE_USES:
            problems.append(f"{key}: use must be one of {TEST_WRITE_USES}, not {entry.get('use')!r}")
        elif entry.get("file") == FAULT_HELPER and entry.get("use") != "fault":
            problems.append(f'{key}: every write of the fault helper is a fault (use = "fault")')
    return problems


def test_raw_kicad_writes_in_tests_are_the_frozen_list() -> None:
    modules = _test_modules()
    assert len(modules) >= 60 and "test_schematic_import.py" in modules, f"only {len(modules)} modules under {TESTS}"
    problems = kicad_write_entry_problems(_data().get("test_kicad_write", []))
    assert not problems, f"{DATA_NAME} [[test_kicad_write]]:\n  " + "\n  ".join(problems)
    _assert_ratchet("Raw KiCad write in tests", scan(KicadTestWriteScanner, modules), "test_kicad_write", FIX_TEST_WRITE)


def test_test_kicad_write_scanner_catches_kicad_targets_and_payloads() -> None:
    text = """
import io
def f(pcb, project, root, src, path, out, tmp_path, board, rules, input_pcb, pro, x):
    pcb.write_text("x")                                  # the target names a board
    (project / "a.kicad_sch").write_text(text)           # a KiCad suffix
    (root / "fp-lib-table").write_text(t)                # a library table
    src.write_text("(kicad_pcb)")                        # the payload is a KiCad file
    path.write_text(serialize_sexp(node))                # serialised from an object model
    path.write_text(f"(fp_lib_table (lib {x}))")         # an f-string payload
    out.with_suffix(".kicad_pro").write_text("{}")       # the project file (JSON)
    open(tmp_path / "x.kicad_mod", "w")
    board.open("a")
    path.write_bytes(b"(kicad_sch)")                     # a bytes payload
    rules.write_text("(version 1)\\n(rule x)")            # a custom-rules payload
    input_pcb.write_text("")
    pro.write_bytes(pro.read_bytes() + b"x")
    path.write_text(write_layer.serialise(root) + "\\n")  # the layer's text, written raw
    io.open(tmp_path / "b.kicad_dru", mode="w")
    (tmp_path / "x.kicad_prl").write_text("{}")         # local project settings
def g(tmp_path, lock, jar, log, path, pcb, ses, d):
    (d / "kicad-cli.exe").write_text("stub")             # a stub executable
    (tmp_path / "~p.kicad_pcb.lck").write_bytes(b"{}")   # a KiCad lock file is JSON
    lock.write_bytes(kicad_lock_bytes("u", "h"))
    jar.write_bytes(b"x")
    (tmp_path / "a.json").write_text("{}")
    ses.write_text("(session x)")                        # Specctra, not KiCad
    pcb.read_text()                                      # a read
    open(pcb)                                            # a read
    path.write_text("x")                                 # a generic name: the documented blind spot
    (tmp_path / "project.txt").write_text("x")           # "project" is not "pro"
"""
    assert _scan_text(KicadTestWriteScanner, text, "test_probe.py") == [("f", "write_text")] * 7 + [
        ("f", "open"), ("f", "open"), ("f", "write_bytes"), ("f", "write_text"), ("f", "write_text"),
        ("f", "write_bytes"), ("f", "write_text"), ("f", "open"), ("f", "write_text"),
    ]
    # A target or payload held in a variable is resolved through its assignments in the
    # enclosing function, the test's parametrize values and the module's constants (round 1).
    resolved = """
import pytest
BOARD_TEXT = "(kicad_pcb (version 20240108))"
NOT_KICAD = "(session x)"
def h(tmp_path, board, path, data):
    variant = tmp_path / "variant.kicad_pcb"
    variant.write_text(board.read_text(encoding="utf-8").replace("GND", "G ND"), encoding="utf-8")
    target = board
    target.write_bytes(target.read_bytes().replace(b"(net 1", b"(net 9"))
    path.write_text(BOARD_TEXT)                          # a module constant holding KiCad text
    copied = board.read_bytes()
    path.write_bytes(copied)                             # a payload read from a KiCad-named path
    for out in [tmp_path / "a.kicad_sch"]:
        out.write_text(data)                             # a loop variable
@pytest.mark.parametrize("relative", ["x.kicad_pcb", "rules.kicad_dru"])
def k(project, relative):
    target = project.parent / relative
    target.write_bytes(target.read_bytes() + b"x")       # a parametrized KiCad name
@pytest.mark.parametrize(("name", "other"), [("log.txt", "a.kicad_pcb")])
def m(tmp_path, name, other, path):
    (tmp_path / name).write_text("x")                    # only "other" is a KiCad name
    log = tmp_path / "run.log"
    log.write_text(path.read_text())                     # a generic read
    path.write_text(NOT_KICAD)
@pytest.mark.parametrize("benign", ["router.log", "board.kicad_pcb.lck", "out/result.ses"])
def n(board, tmp_path, benign, krt, output_pcb):
    target = board.parent / benign
    target.write_bytes(b"router output")                 # no KiCad name among the values
    ses = tmp_path / "board.ses"
    ses.write_text("x")                                  # a spelled suffix decides: Specctra
    bogus = tmp_path / "board.txt"
    bogus.write_text("x")
    leftover = krt.stop_file_for(output_pcb)
    leftover.write_text("x")                             # a call's result is not followed
@pytest.mark.parametrize("new", [pytest.param("a.kicad_sch", id="sch"), "b.txt"])
def q(board, new):
    target = board.parent / new
    target.write_bytes(b"planted")                       # one KiCad name among the values
def r(tmp_path, board, d, log, out, path):
    p = str(tmp_path / "a.kicad_pcb")
    open(p, "w")                                         # str() of a KiCad path (delta review 1)
    s = os.fspath(tmp_path / "b.kicad_sch")
    open(s, "w")
    j = os.path.join(d, "c.kicad_pro")
    open(j, "w")
    path.write_text(open(board).read())                  # a read through open()
    with board.open() as fh:
        path.write_text(fh.read())                       # a read through a handle
    with open(log) as fh2:
        out.write_text(fh2.read())                       # a generic read
    path.write_text("".join(open(board).readlines()))    # readlines (delta review 2)
    path.write_text(Path.open(board).read())             # Path.open called on the class
    path.write_text(Path.read_text(board))               # Path.read_text called on the class
    out.write_text("".join(open(log).readlines()))       # a generic read
    for t in [tmp_path / "a.lck", tmp_path / "b.txt"]:
        t.write_text(BOARD_TEXT)                         # the documented mixed-suffix limit
"""
    assert _scan_text(KicadTestWriteScanner, "import os\n" + resolved, "test_probe.py") == [
        ("h", "write_text"), ("h", "write_bytes"), ("h", "write_text"), ("h", "write_bytes"),
        ("h", "write_text"), ("k", "write_bytes"), ("q", "write_bytes"),
        ("r", "open"), ("r", "open"), ("r", "open"), ("r", "write_text"), ("r", "write_text"),
        ("r", "write_text"), ("r", "write_text"), ("r", "write_text"),
    ]
    helper = """
def drift(path):
    open(path, "ab")
def other(path):
    path.write_text("x")
"""
    assert _scan_text(KicadTestWriteScanner, helper, FAULT_HELPER) == [("drift", "open"), ("other", "write_text")]
    assert _scan_text(KicadTestWriteScanner, helper, "test_other.py") == []


def test_test_kicad_write_entry_checks_name_every_bad_entry() -> None:
    good = {"file": "test_x.py", "function": "f", "kind": "write_text", "count": 1, "use": "stub", "note": "n"}
    entries = [
        good,
        {**good, "function": "g", "use": "debt"},                      # not one of the uses
        {**good, "function": "h", "use": None},                        # no use at all
        {**good, "file": FAULT_HELPER, "function": "drift", "use": "stub"},  # the helper's write is a fault
        {**good, "file": FAULT_HELPER, "function": "raw", "use": "fault"},
    ]
    problems = kicad_write_entry_problems(entries)
    assert len(problems) == 3, problems
    assert "'debt'" in problems[0] and "None" in problems[1]
    assert "drift" in problems[2] and 'use = "fault"' in problems[2]
    assert kicad_write_entry_problems([good]) == []


def test_kicad_fault_calls_outside_the_helpers_own_tests_are_the_frozen_list() -> None:
    _assert_ratchet(
        "Fault-helper call in tests", scan(KicadFaultCallScanner, _test_modules()), "test_kicad_fault_call",
        FIX_FAULT_CALL, by_code=True,
    )


def test_fault_call_scanner_counts_every_writer_outside_its_own_tests() -> None:
    text = """
from tests import _kicad_faults as faults
from tests._kicad_faults import rewrite_bytes
def f(root, path):
    faults.write_unchecked(root, path)
    node = faults.raw_atom('"a' + chr(10) + 'b"')
    rewrite_bytes(path, lambda data: data[:10])
    faults.drift(path)                                   # bytes change, tokens do not: not listed
    write_unchecked_report(path)                         # another name
    holder.write_unchecked(root, path)                   # unresolved, named like a writer: counts
"""
    expected = [("f", "write_unchecked"), ("f", "raw_atom"), ("f", "rewrite_bytes"), ("f", "write_unchecked")]
    assert _scan_text(KicadFaultCallScanner, text, "test_probe.py") == expected
    assert _scan_text(KicadFaultCallScanner, text, "test_kicad_faults.py") == []
    assert _scan_text(KicadFaultCallScanner, text, FAULT_HELPER) == []
    # A writer reached under another name or passed on still counts (delta review 1): an
    # aliased import, a name bound to it, functools.partial, getattr.
    aliased = """
import functools
from tests import _kicad_faults as faults
from tests._kicad_faults import rewrite_bytes as rb
def g(root, path):
    rb(path, lambda data: data[:10])
    w = faults.rewrite_bytes
    functools.partial(faults.write_unchecked, root)
    getattr(faults, "raw_atom")
    faults.drift(path)
    getattr(faults, "drift")
    functools.partial(rb, path)                          # a bare alias passed on (delta review 2)
    w2 = rb                                              # a bare alias bound
"""
    assert _scan_text(KicadFaultCallScanner, aliased, "test_probe.py") == [
        ("g", "rewrite_bytes"), ("g", "rewrite_bytes"), ("g", "write_unchecked"), ("g", "raw_atom"),
        ("g", "rewrite_bytes"), ("g", "rewrite_bytes"),
    ]
    # A writer re-exported through another test module is the same writer (delta review 2).
    modules = {
        "test_reexport.py": ast.parse("from tests._kicad_faults import rewrite_bytes as rb2\nfrom tests import _kicad_faults as fx"),
        "test_probe.py": ast.parse(
            "from tests.test_reexport import rb2, fx\ndef h(path):\n    rb2(path, bytes.upper)\n    fx.write_unchecked(None, path)"
        ),
    }
    assert [(s.function, s.kind) for s in scan(KicadFaultCallScanner, modules)] == [
        ("h", "rewrite_bytes"), ("h", "write_unchecked"),
    ]


def test_every_reference_through_the_fault_helper_is_classified() -> None:
    found = scan(FaultHelperUseScanner, _test_modules())
    assert not found, (
        "A test reaches tests/_kicad_faults.py through a name the fault-call ratchet does not list. "
        "Use a writer by its name (faults.rewrite_bytes, faults.write_unchecked, faults.raw_atom) or "
        "faults.drift; a new writer goes into FAULT_WRITERS first. "
        + "; ".join(f"{s.file}:{s.line} {s.function} {s.kind}: {s.code}" for s in found)
    )


def test_fault_helper_use_check_fails_every_unclassified_or_dynamic_reference() -> None:
    probe = """
import functools, importlib, sys
import tests._kicad_faults
import tests._kicad_faults as tf
from tests import _kicad_faults as faults
from tests._kicad_faults import rewrite_bytes as rb, drift
from tests._kicad_faults import _r as hidden
from tests._kicad_faults import *
from tests.test_reexport import fx
from tests.test_kicad_faults import corrupt                # a wrapper in the helper's tests (delta review 3)
import tests.test_kicad_faults as tkf
from tests.test_hop1 import fy                             # four modules away: cannot resolve
from tests.sub.test_deep import fz                         # a subdirectory re-export, resolved
from tests.nowhere import fq                               # no such test module: cannot resolve
def f(tmp, path, name):
    faults._r(tmp / "x.kicad_pcb", bytes.upper)          # the reviewer's pair (delta review 2)
    faults.rewrite_bytes(path, bytes.upper)              # a writer: fine
    faults.drift(path)                                   # benign: fine
    rb(path, bytes.upper)
    drift(path)
    getattr(faults, "raw_atom")                          # a writer by literal: fine
    functools.partial(faults.write_unchecked, None)      # fine
    tests._kicad_faults._r(path)
    tf.corrupt(path)
    getattr(faults, "_r")(path)
    getattr(faults, name)
    vars(faults)["_r"]
    faults.__dict__["_r"]
    w = faults
    importlib.import_module("tests._kicad_faults")
    sys.modules["tests._kicad_faults"]
    importlib.util.spec_from_file_location("m", "tests/_kicad_faults.py")
    __import__("tests._kicad_faults")
    open("test_kicad_faults.py")                         # the helper's tests, by file name: fine
    fx._r(path)
    corrupt(path)
    tkf.corrupt(path)
    getattr(tkf, "corrupt")
    fz._r(path)
"""
    modules = {
        "test_reexport.py": ast.parse("from tests import _kicad_faults as fx"),
        "test_kicad_faults.py": ast.parse(
            "from tests import _kicad_faults as faults\ndef corrupt(path):\n    return faults.rewrite_bytes(path, bytes.upper)"
        ),
        "test_hop1.py": ast.parse("from tests.test_hop2 import fy"),
        "test_hop2.py": ast.parse("from tests.test_hop3 import fy"),
        "test_hop3.py": ast.parse("from tests.test_hop4 import fy"),
        "test_hop4.py": ast.parse("from tests import _kicad_faults as fy"),
        "sub/test_deep.py": ast.parse("from tests import _kicad_faults as fz"),
        "test_probe.py": ast.parse(probe),
    }
    assert [(s.function, s.kind) for s in scan(FaultHelperUseScanner, modules)] == [
        ("<module>", "unclassified _r"), ("<module>", "star import"),
        ("<module>", "via the helper's tests"), ("<module>", "via the helper's tests"),
        ("<module>", "unresolved reference"), ("<module>", "unresolved reference"),
        ("f", "unclassified _r"), ("f", "unclassified _r"), ("f", "unclassified corrupt"),
        ("f", "unclassified _r"), ("f", "dynamic helper access"), ("f", "bare helper module"),
        ("f", "unclassified __dict__"), ("f", "bare helper module"), ("f", "dynamic helper access"),
        ("f", "dynamic helper access"), ("f", "dynamic helper access"), ("f", "dynamic helper access"),
        ("f", "unclassified _r"),
        ("f", "via the helper's tests"), ("f", "via the helper's tests"), ("f", "via the helper's tests"),
        ("f", "unclassified _r"),
    ]
    for exempt in (FAULT_HELPER, FAULT_HELPER_TESTS):
        assert scan(FaultHelperUseScanner, {exempt: ast.parse(probe)}) == []


def test_fault_scanners_follow_a_top_level_import_between_test_modules() -> None:
    # tests/ has no __init__.py, and test modules import each other top-level
    # (``from test_input_prep import _build_fixture``): a re-export in that style resolves too
    # (delta review 4).
    modules = {
        "test_x.py": ast.parse("from tests import _kicad_faults as fx\nfrom tests._kicad_faults import rewrite_bytes as rw"),
        "test_probe.py": ast.parse(
            "from test_x import fx\nfrom test_x import rw\ndef t(b):\n"
            "    fx.rewrite_bytes(b, f)\n    fx._r(b)\n    rw(b, f)"
        ),
    }
    calls = [(s.function, s.kind) for s in scan(KicadFaultCallScanner, modules) if s.file == "test_probe.py"]
    assert calls == [("t", "rewrite_bytes"), ("t", "rewrite_bytes")]
    uses = [(s.function, s.kind) for s in scan(FaultHelperUseScanner, modules) if s.file == "test_probe.py"]
    assert uses == [("t", "unclassified _r")]
    # End to end, the reviewer's repro against the real tree.
    real = _test_modules()
    real["test_probe_e2e.py"] = ast.parse(
        "from test_gate_field import faults as ff\ndef t(tmp_path):\n"
        "    ff.rewrite_bytes(tmp_path / 'x.kicad_pcb', lambda data: data.replace(b'GND', b'VCC'))\n"
        "    ff._r(tmp_path)"
    )
    calls = [(s.function, s.kind) for s in scan(KicadFaultCallScanner, real) if s.file == "test_probe_e2e.py"]
    assert calls == [("t", "rewrite_bytes")]
    uses = [(s.function, s.kind) for s in scan(FaultHelperUseScanner, real) if s.file == "test_probe_e2e.py"]
    assert uses == [("t", "unclassified _r")]


def test_fault_writers_are_every_callable_the_helper_defines() -> None:
    assert fault_helper_writer_problems(_test_modules()[FAULT_HELPER]) == []


def test_fault_writer_check_names_a_new_wrapper_in_the_helper() -> None:
    helper = """
_MARKER = "x"
_VERBATIM: dict = {}
def drift(path): ...
def raw_atom(text): ...
def write_unchecked(root, path): ...
def rewrite_bytes(path, change): ...
def replace_text(path, old, new):
    return rewrite_bytes(path, lambda d: d.replace(old, new))
_hidden = lambda path: rewrite_bytes(path, bytes.upper)
class Corrupt:
    def __call__(self, path):
        return rewrite_bytes(path, bytes.upper)
_r = rewrite_bytes
_a: object = rewrite_bytes
_b, _c = rewrite_bytes, raw_atom
if True:
    def nested(path): ...
try:
    from tests._other import corrupt
except ImportError:
    pass
from tests._more import *
"""
    problems = fault_helper_writer_problems(ast.parse(helper))
    names = ["Corrupt", "_a", "_b", "_c", "_hidden", "_r", "corrupt", "nested", "replace_text"]
    assert len(problems) == len(names) + 1, problems
    for problem, name in zip(problems, names, strict=False):
        assert f" {name}," in problem, (name, problem)
    assert "star import" in problems[-1]
    missing = fault_helper_writer_problems(ast.parse("def drift(path): ...\ndef raw_atom(text): ..."))
    assert len(missing) == 2 and "rewrite_bytes" in missing[0] and "write_unchecked" in missing[1]
