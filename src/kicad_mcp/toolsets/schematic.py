"""Schematic toolset: symbol placement, wiring, labels, footprint
assignment, annotation fixing, and connectivity checking.

Per docs/ARCHITECTURE.md's two-API split: uses the high-level
`kicad_tools.schematic.models.schematic.Schematic` builder for
authoring (its `add_symbol`/`add_wire`/`add_label`/etc. also work fine
against an *existing* schematic loaded via `.load()`, not just a blank
one -- confirmed by inspection, 2026-09-02), and the low-level
`kicad_tools.schema.Schematic` for read-only inspection (connectivity
checking).

Every tool is batch-oriented (accepts a list of specs), never
one-item-per-call, per this project's standing convention. Every tool
is evidence-gated: a failure is reported as `{"status": "incomplete"/
"error", "reason": ...}`, never raised past the tool boundary.
"""

from __future__ import annotations

import dataclasses
import math
import enum
import inspect
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from kicad_mcp import connectivity_check, pin_wiring, symbol_libs, write_layer
from kicad_mcp.kicad_cli import ensure_kicad_cli_on_path, find_kicad_cli

_symbol_dir_checked = False


def _ensure_kicad_symbol_dir_set() -> None:
    """Point kicad_tools at KiCad's standard symbol libraries (power:GND,
    Device:R, etc.) via the KICAD_SYMBOL_DIR env var it already honors.

    Upstream gap found 2026-09-02: kicad_tools.schematic.grid's
    get_symbol_search_paths() has platform defaults for macOS, Linux, and
    user-local dirs, but NONE for Windows -- so on this OS, adding any
    stock-library symbol (add_symbol("power:GND", ...), "Device:C", etc.)
    to a freshly-created Schematic fails with "Library not found", since
    there's no local_symbol_libs set and the global fallback search finds
    nothing. Worked around here (can't edit kicad_cli.py from this
    ticket's scope) by deriving the symbols dir from the same
    already-working find_kicad_cli() resolution used elsewhere in this
    project, rather than duplicating KiCad-install-path detection.
    Candidate for an upstream PR per docs/ARCHITECTURE.md's contribution policy.
    """
    global _symbol_dir_checked
    if _symbol_dir_checked or os.environ.get("KICAD_SYMBOL_DIR"):
        return
    _symbol_dir_checked = True
    try:
        cli_path = find_kicad_cli()
    except Exception:  # noqa: BLE001 - best-effort; let the real error surface downstream if this fails
        return
    symbol_dir = cli_path.parent.parent / "share" / "kicad" / "symbols"
    if symbol_dir.is_dir():
        os.environ["KICAD_SYMBOL_DIR"] = str(symbol_dir)


def _to_jsonable(value: Any) -> Any:
    """Recursively convert kicad_tools result objects into JSON-safe values.

    Duplicated from toolsets/verification.py's helper of the same name
    (not shared, to keep this ticket's file-ownership scope clean while
    other work lands in parallel) -- worth deduping into a shared
    kicad_mcp._jsonable module later.
    """
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _to_jsonable(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, (list, tuple, set)):
        return [_to_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _to_jsonable(v) for k, v in value.items()}
    if hasattr(value, "x") and hasattr(value, "y") and not isinstance(value, (str, bytes)):
        return {"x": value.x, "y": value.y}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _incomplete(reason: str, **extra: Any) -> dict:
    return {"status": "incomplete", "reason": reason, **extra}


def _carriage_returns(value: Any, where: str) -> list[str]:
    """Paths (``specs[0].text``) of every input string that holds a carriage return.

    kicad_tools up to 0.24.0 writes a ``\\r`` inside a string raw (docs/ARCHITECTURE.md decision 1:
    escaping is build ticket 42's write layer) and its parser reads it back as a line feed, so
    the text would not survive (0.25.0 escapes it; the refusal stays, with its backstop in
    ``_write_schematic_utf8``). The tools refuse such input before loading anything.
    """
    if isinstance(value, str):
        return [where] if "\r" in value else []
    if isinstance(value, dict):
        return [hit for key, item in value.items() for hit in _carriage_returns(item, f"{where}.{key}")]
    if isinstance(value, (list, tuple)):
        return [hit for i, item in enumerate(value) for hit in _carriage_returns(item, f"{where}[{i}]")]
    return []


def _refuse_carriage_returns(value: Any, where: str) -> dict | None:
    hits = _carriage_returns(value, where)
    if not hits:
        return None
    return _incomplete(
        f"carriage return in {', '.join(hits)}: kicad_tools writes it unescaped and drops it on "
        "reload, so the text would not survive; nothing was written"
    )


def _sheet_tree_refusal(tree: Any, *, kicad_tools_writes: bool) -> dict | None:
    """Why a tool must not run on the project ``tree`` describes (build ticket 26), or None.

    kicad_tools' own hierarchy walk (``sch_set_footprint._collect_schematic_files``,
    0.20.0 and 0.23.0) follows only ``Sheetfile`` and skips a missing sheet without a word,
    so a tool that lets kicad_tools write refuses a tree with a problem or with a sheet
    reached only through KiCad 6's ``Sheet file`` key, before anything is written."""
    if tree.problems:
        return _incomplete(
            f"the sheet tree from {tree.root.name} is incomplete: " + "; ".join(tree.problems), **tree.report()
        )
    if kicad_tools_writes and tree.legacy_files:
        names = ", ".join(tree.rel(f) for f in tree.legacy_files)
        return _incomplete(
            f"kicad_tools follows only the 'Sheetfile' key, so it would skip {names} (reached through "
            "KiCad 6's 'Sheet file' key); nothing was written. Saving the project once in KiCad 7 "
            "or later rewrites the key.",
            **tree.report(),
        )
    return None


BACKUP_DIR_ENV = "KICAD_MCP_BACKUP_DIR"


def backup_root() -> Path:
    """Where schematic backups go: ``$KICAD_MCP_BACKUP_DIR``, else a durable per-user data
    directory, not the temp directory (which the OS may clear):
    ``%LOCALAPPDATA%\\kicad-mcp\\schematic-backups`` on Windows,
    ``$XDG_DATA_HOME/kicad-mcp/schematic-backups`` (default ``~/.local/share``) elsewhere.
    Nothing is created here; a root inside the project is refused by ``_backup_root_refusal``."""
    override = os.environ.get(BACKUP_DIR_ENV)
    if override:
        root = Path(override)
    elif os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"]) / "kicad-mcp" / "schematic-backups"
    else:
        data = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
        root = Path(data) / "kicad-mcp" / "schematic-backups"
    return root


def _backup_root_refusal(tree: Any) -> dict | None:
    """A backup root inside the project (or the project directory itself) would put copies
    of the sheets where a directory scan takes them for live sheets, the stray copies of
    build ticket 26: refused before anything is written."""
    root = backup_root().resolve()
    project = tree.project_dir
    if root == project or project in root.parents:
        return _incomplete(
            f"the backup directory {root} is inside the project {project}: its copies would be stray "
            f"sheets; nothing was written. Point {BACKUP_DIR_ENV} outside the project.",
            **tree.report(),
        )
    return None


def _backup_outside_project(tree: Any) -> Path:
    """Copy every sheet of ``tree`` to a new directory under ``backup_root()`` and return it.
    kicad_tools' own backups (``<stem>_backup_<time>.kicad_sch``) land beside the sheet,
    inside the project, where any directory scan would take them for a live sheet."""
    import shutil
    import tempfile
    import time

    if (refused := _backup_root_refusal(tree)) is not None:  # callers check first; never write inside
        raise RuntimeError(refused["reason"])
    root = backup_root()
    root.mkdir(parents=True, exist_ok=True)
    target = Path(tempfile.mkdtemp(prefix=f"{tree.root.stem}-{time.strftime('%Y%m%d_%H%M%S')}-", dir=root))
    for sheet in tree.files:
        dest = target / tree.rel(sheet)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(sheet, dest)
    return target


class FootprintWriteNotVerified(RuntimeError):
    """A sheet ``assign_footprints`` wrote did not reload as the tree it serialised.
    ``mismatches`` lists each footprint that did not land (file, symbol uuid, references,
    expected, actual); ``write_check`` is ``_self_check.reload_differences`` of the sheet."""

    def __init__(self, message: str, mismatches: list[dict[str, Any]], write_check: dict) -> None:
        super().__init__(message)
        self.mismatches = mismatches
        self.write_check = write_check


@dataclasses.dataclass
class _FootprintStage:
    """What ``_stage_footprints`` found: ``refused`` (a mapping KiCad cannot hold, nothing may
    be written), ``changed`` (sheet, in-memory root, ``{uuid: (id, references)}`` set there)
    and ``reasons`` (per reference that cannot be set, why)."""

    refused: list[str]
    changed: list[tuple[Path, Any, dict[str, tuple[str, list[str]]]]]
    reasons: dict[str, str]


def _times(n: int) -> str:
    return {1: "once", 2: "twice"}.get(n, f"{n} times")


def _symbol_uuid(node: Any) -> str:
    found = node.find("uuid")
    return str(found.children[0].value) if found is not None and found.children else ""


def _footprint_of(node: Any) -> Any:
    """The ``(property "Footprint" ...)`` node of a placed symbol, or None."""
    for p in node.children:
        if not p.is_atom and p.name == "property" and len(p.children) >= 2 and p.children[0].value == "Footprint":
            return p
    return None


def _stage_footprints(tree: Any, mapping: dict[str, str]) -> _FootprintStage:
    """Set, in memory, the ``Footprint`` property of every placed symbol an instance reference
    of which is a key of ``mapping``, on each sheet of ``tree`` loaded through the write layer
    (build ticket 25). Nothing is written here.

    A key is resolved through the symbol's ``(instances (project (path P (reference R))))``
    entries for the paths the sheet tree uses (``sheet_tree.placed_instances``), never through
    its ``Reference`` property: on a sheet used twice that property holds the first use's
    reference only (fix round 2, delta review 1 blocker 1). KiCad keeps one ``Footprint`` per
    symbol, shared by every use of the sheet, so a mapping that names only some of a symbol's
    instances, or gives them different ids, would change an instance it does not name or
    cannot hold: both are refused by name. Every unit of a multi-unit part is its own placed
    symbol with its own property, and each is set.

    The id is a typed string atom (``write_layer.set_string``), so the layer's serialiser
    escapes it: a backslash stays a backslash. kicad_tools' ``run_set_footprint`` used it as a
    ``re.sub`` replacement template (``\\n`` became a raw LF, ``\\g<0>`` inserted the match),
    and read and wrote each sheet with universal newlines (a raw CR already in a string became
    a raw LF): either dropped a sub-sheet from the design with exit 0. ``write_layer.load``
    reads without newline translation, so every other value of the sheet survives. Raises
    ``write_layer.InvalidStringError`` for an id the layer refuses (a control character)."""
    from kicad_mcp import sheet_tree, write_layer

    instances: dict[tuple[str, str], list[str]] = {}  # (sheet, symbol uuid) -> its references
    for record in sheet_tree.placed_instances(tree):
        refs = instances.setdefault((record["sheet"], record["uuid"]), [])
        if record["ref"] not in refs:
            refs.append(record["ref"])
    uses: dict[str, int] = {}
    for sheet, _ in tree.uses:
        uses[tree.rel(sheet)] = uses.get(tree.rel(sheet), 0) + 1

    refused: list[str] = []
    targets: dict[str, dict[str, tuple[str, list[str]]]] = {}
    found: set[str] = set()
    for (sheet, uuid), refs in instances.items():
        named = [r for r in refs if r in mapping]
        if not named:
            continue
        found.update(named)
        where = f"sheet {Path(sheet).stem} used {_times(uses.get(sheet, 1))}"
        ids = sorted({mapping[r] for r in named})
        if left_out := [r for r in refs if r not in mapping]:
            what = "both" if len(refs) == 2 else "all of them"
            refused.append(
                f"{', '.join(named)} {'shares' if len(named) == 1 else 'share'} its symbol with "
                f"{', '.join(left_out)} ({where}): map {what} to the same id"
            )
        elif len(ids) > 1:
            refused.append(
                f"{' and '.join(refs)} share one symbol ({where}), which holds one footprint, but the "
                f"mapping gives them {', '.join(repr(i) for i in ids)}: map them to the same id"
            )
        else:
            targets.setdefault(sheet, {})[uuid] = (ids[0], refs)
    reasons = {ref: "no placed instance in the sheet tree has this reference" for ref in mapping if ref not in found}
    if refused:
        return _FootprintStage(list(dict.fromkeys(refused)), [], reasons)  # a part's units repeat it

    changed: list[tuple[Path, Any, dict[str, tuple[str, list[str]]]]] = []
    for sheet in tree.files:
        wanted = targets.get(tree.rel(sheet))
        if not wanted:
            continue
        root = write_layer.load(sheet)
        setting: dict[str, tuple[str, list[str]]] = {}
        for node in root.children:
            if node.is_atom or node.name != "symbol" or node.find("lib_id") is None:
                continue  # placed symbols only; lib_symbols holds the library copies
            if (uuid := _symbol_uuid(node)) not in wanted:
                continue
            footprint_id, refs = wanted[uuid]
            if (footprint := _footprint_of(node)) is None:
                reasons.update(dict.fromkeys(refs, "the symbol has no Footprint property"))
            elif footprint.children[1].value != footprint_id:
                write_layer.set_string(footprint, 1, footprint_id)
                setting[uuid] = (footprint_id, refs)
        if setting:
            changed.append((sheet, root, setting))
    return _FootprintStage([], changed, reasons)


def _writable(text: str) -> bool:
    """Whether the write layer takes ``text`` as a new string value."""
    from kicad_mcp import write_layer

    try:
        write_layer.check_text(text)
    except write_layer.InvalidStringError:
        return False
    return True


def _save_footprint_sheet(sheet: Path, root: Any, setting: dict[str, tuple[str, list[str]]], label: str) -> None:
    """Save one sheet ``_stage_footprints`` changed (``setting``: uuid to (id, references))
    through the write layer. Before the move, the temporary file is reloaded and must
    serialise exactly as the tree did (self-check, decision 4); otherwise
    ``FootprintWriteNotVerified`` with the mismatches and ``sheet`` is untouched. ``label`` is
    the sheet's path in the tree, for the mismatch list."""
    from kicad_mcp import _self_check, write_layer

    expected = write_layer.serialise(root, compact=True)

    def verify(written: Path) -> None:
        reloaded = write_layer.load(written)
        if write_layer.serialise(reloaded, compact=True) == expected:
            return
        source = write_layer.load(sheet)

        def set_here(tree: Any) -> list[Any]:
            return [n for n in tree.children if not n.is_atom and n.name == "symbol" and _symbol_uuid(n) in setting]

        landed = {_symbol_uuid(n): _footprint_of(n) for n in set_here(reloaded)}
        mismatches = []
        for uuid, (footprint_id, refs) in setting.items():
            prop = landed.get(uuid)
            actual = None if prop is None else str(prop.children[1].value)
            if actual != footprint_id:
                mismatches.append(
                    {"file": label, "symbol": uuid, "references": refs, "expected": footprint_id, "actual": actual}
                )
        write_check = _self_check.reload_differences(
            source=source, removed=set_here(source), written=set_here(root), memory=root, reloaded=reloaded
        )
        raise FootprintWriteNotVerified(
            f"{label} did not reload as the tree that was serialised: " + (_self_check.describe(write_check) or "text differs"),
            mismatches,
            write_check,
        )

    write_layer.save(root, sheet, verify=verify)


def _node_names(node: Any) -> dict[str, int]:
    """How many list nodes of each name ``node`` holds, itself included, at any depth."""
    counts: dict[str, int] = {}
    stack = [node]
    while stack:
        current = stack.pop()
        if current.is_atom:
            continue
        counts[current.name] = counts.get(current.name, 0) + 1
        stack.extend(current.children)
    return counts


def _old_format_refusal(path: Path) -> dict | None:
    """The refusal for a sheet the builder tools would rewrite from kicad_tools' model, or None.

    kicad_tools 0.24.0's ``Schematic.load`` keeps a sheet's source only when its raw
    ``(version N)`` is at least ``KICAD_SCH_FORMAT_VERSION`` (20231120, KiCad 8); an older
    sheet (or one without a readable version) is regenerated wholesale from the model and
    restamped (``io_mixin.load``, kept so deliberately upstream). On vme-wren's KiCad 7 sheet
    that dropped every bus, bus entry and text box, most symbol fields, title-block comments
    and stroke widths (build ticket 38). ``would_drop`` is how many nodes of each name the
    in-memory regeneration has fewer of than the file; nothing is written here."""
    from kicad_tools.core.version import KICAD_SCH_FORMAT_VERSION
    from kicad_tools.schematic.models.schematic import Schematic
    from kicad_tools.sexp import parse_string

    from kicad_mcp import write_layer

    try:
        root = write_layer.load(path)
    except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
        return _incomplete(f"could not read {path.name}: {exc}; nothing was written")
    node = root.find("version")
    try:
        version = int(str(node.children[0].value))
    except (AttributeError, IndexError, ValueError):
        version = 0
    if version >= KICAD_SCH_FORMAT_VERSION:
        return None
    try:
        regenerated = parse_string(Schematic.load(path).to_sexp())
        after, before = _node_names(regenerated), _node_names(root)
        would_drop = {name: n - after.get(name, 0) for name, n in before.items() if n > after.get(name, 0)}
        would_drop = dict(sorted(would_drop.items(), key=lambda item: (-item[1], item[0])))

        def top(tree: Any) -> dict[str, int]:
            counts: dict[str, int] = {}
            for child in tree.children:
                if not child.is_atom:
                    counts[child.name] = counts.get(child.name, 0) + 1
            return counts

        top_after = top(regenerated)
        top_drop = {name: n - top_after.get(name, 0) for name, n in top(root).items() if n > top_after.get(name, 0)}
        parts = [f"{n} {name}" for name, n in sorted(top_drop.items(), key=lambda item: (-item[1], item[0]))]
        nested = sum(would_drop.values()) - sum(top_drop.values())
        if nested > 0:
            parts.append(
                f"{nested} nodes nested in what stays ({would_drop.get('property', 0)} of them property "
                "fields; would_drop tallies every name)"
            )
        what = ", ".join(parts) or "nodes it does not model"
    except Exception as exc:  # noqa: BLE001 - the refusal stands without the tally
        would_drop, what = None, f"nodes it does not model (the tally failed: {exc})"
    return _incomplete(
        f"{path.name} is format {version or 'unknown'}, older than kicad_tools' {KICAD_SCH_FORMAT_VERSION} "
        f"(KiCad 8): kicad_tools rewrites such a sheet from its model, which would drop {what} and "
        f"restamp it; nothing was written. Saving the sheet once in KiCad 8 or later updates its format.",
        error="old_sheet_format",
        version=version,
        would_drop=would_drop,
    )


def _schematic_text(sch: Any) -> str:
    """kicad_tools' serialisation of ``sch``: the text ``_write_schematic_utf8`` writes."""
    return sch.to_sexp()


class SchematicWriteNotVerified(RuntimeError):
    """A sheet a builder tool wrote failed the self-check before the move (build ticket 38).
    ``mismatches`` lists each finding (file, expected, actual); ``write_check`` is the token
    view (``missing``: source nodes absent from the written text); ``netlist_check`` holds
    kicad-cli's netlist counts before and after, or None when the token check failed first."""

    def __init__(self, message: str, mismatches: list[dict[str, Any]], write_check: dict, netlist_check: dict | None) -> None:
        super().__init__(message)
        self.mismatches = mismatches
        self.write_check = write_check
        self.netlist_check = netlist_check


def _not_verified_result(exc: SchematicWriteNotVerified) -> dict:
    """Decision 4 and 9: a failed self-check is ``ok: false`` with ``write_not_verified``."""
    from kicad_mcp import _self_check

    return {
        "ok": False,
        "status": "error",
        "error": "write_not_verified",
        "reason": f"{exc}; the sheet was left unchanged",
        "mismatches": {"count": len(exc.mismatches), "items": exc.mismatches[:20]},
        "write_check": _self_check.bounded(exc.write_check),
        "netlist_check": exc.netlist_check,
    }


def _kept_nodes(root: Any) -> Any:
    """``root``'s top-level nodes a builder write must keep unchanged, as a multiset of
    ``(compact text, label)``: every node but ``paper`` (``auto_size_paper`` may enlarge it,
    asked for by no one but the writer) and ``lib_symbols``, whose children count one by
    one (an added symbol adds its library copy)."""
    from collections import Counter

    from kicad_mcp import _self_check

    kept: Counter = Counter()
    for child in root.children:
        if child.is_atom or child.name == "paper":
            continue
        if child.name == "lib_symbols":
            kept.update(
                (_self_check.node_text(s), f"lib_symbols/{_self_check.node_label(s)}") for s in child.children if not s.is_atom
            )
        else:
            kept[(_self_check.node_text(child), _self_check.node_label(child))] += 1
    return kept


def _netlist_census(sheet: Path, replacement: Path | None = None) -> dict[str, Any]:
    """kicad-cli's netlist of ``sheet`` read as the root, exported from scratch copies of the
    sheets ``sheet_tree.walk`` reaches from it (the export writes a ``.kicad_prl`` beside the
    root, build ticket 77), with ``replacement``'s text standing in for ``sheet``. Returns
    ``components`` (count), ``refs`` (references, as a list) and ``nets`` (each net's
    ``(ref, pin)`` nodes, unannotated ``?`` references left out)."""
    import shutil
    import tempfile

    from kicad_tools.sexp.parser import parse_file

    from kicad_mcp import kicad_cli, sheet_tree

    sheet = sheet.resolve()
    files = {f.resolve() for f in sheet_tree.walk(sheet).files} | {sheet}
    base = Path(os.path.commonpath([str(f.parent) for f in files]))
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-sch-check-") as tmp:
        for source in files:
            copy = Path(tmp) / "p" / source.relative_to(base)
            copy.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(replacement if replacement is not None and source == sheet else source, copy)
        root = Path(tmp) / "p" / sheet.relative_to(base)
        parsed = parse_file(str(kicad_cli.export_netlist(root, Path(tmp) / "check.net")))
    refs = [
        str(comp.find("ref").children[0].value)
        for block in parsed.find_children("components")
        for comp in block.find_children("comp")
    ]
    nets = []
    for block in parsed.find_children("nets"):
        for net in block.find_children("net"):
            nodes = set()
            for node in net.find_children("node"):
                ref, pin = str(node.find("ref").children[0].value), str(node.find("pin").children[0].value)
                if not ref.endswith("?"):
                    nodes.add((ref, pin))
            nets.append(frozenset(nodes))
    from kicad_mcp import sheet_editing

    return {"components": len(refs), "refs": refs, "nets": nets, "component_records": sheet_editing._components_of(parsed)}


def _clip(text: str, limit: int = 160) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _describe_lost_node(label: str, source_root: Any, written_root: Any) -> tuple[str, str]:
    """``(subject, actual)`` for a top-level node of the source that the written text does not
    hold unchanged (``label`` as ``_self_check.node_label`` names it): the subject names a
    symbol by reference and uuid, ``actual`` says whether the node is absent or changed in
    place and which of its children differ (expected text against the text read back)."""
    from collections import Counter

    from kicad_mcp import _self_check

    def find(root: Any) -> Any:
        return next((c for c in root.children if not c.is_atom and _self_check.node_label(c) == label), None)

    src, got = find(source_root), find(written_root)
    subject = label
    if src is not None and src.name == "symbol" and (ref := _self_check._reference(src)):
        subject = f"symbol {ref} ({_self_check._child_value(src, 'uuid')})"
    if got is None:
        return subject, "absent from the written text (no node with that uuid)"
    have, want = Counter(_self_check.node_text(c) for c in got.children), Counter(_self_check.node_text(c) for c in src.children)
    lost = [c for c in src.children if want[_self_check.node_text(c)] > have[_self_check.node_text(c)]]
    gained = [c for c in got.children if have[_self_check.node_text(c)] > want[_self_check.node_text(c)]]
    names = sorted({c.name if not c.is_atom else "atom" for c in lost + gained})
    expected_text = _clip("; ".join(_self_check.node_text(c) for c in lost))
    read_text = _clip("; ".join(_self_check.node_text(c) for c in gained))
    return subject, (
        f"changed in place: {', '.join(names) or 'unknown part'} differ; expected {expected_text or 'nothing'}, "
        f"read back {read_text or 'nothing'}"
    )


def _verify_schematic_write(path: Path, written: Path, added_refs: set[str], expected_fields: dict[str, dict[str, str]] | None = None) -> None:
    """The builder tools' self-check (decision 4, build ticket 38), on the temporary file
    ``written`` before it replaces ``path``; raises ``SchematicWriteNotVerified``.

    1. Tokens: every top-level node of ``path`` (``_kept_nodes``) is in ``written``
       unchanged: the tools only add.
    2. kicad-cli's netlist of each, read as the root: every component before is still there
       and the count rises by exactly the new references in ``added_refs`` (``#`` power
       references are not components); every net before lies within one net after (a new
       wire may join two nets; nothing may split one or lose a pin).
    3. Fields (issue 162, ``expected_fields``: reference -> field name -> value): the netlist component of each
       new reference carries those fields (``~`` and an empty text count as equal)."""
    from collections import Counter

    from kicad_mcp import write_layer

    source_root, written_root = write_layer.load(path), write_layer.load(written)
    source_kept, written_kept = _kept_nodes(source_root), _kept_nodes(written_root)
    missing = sorted(label for (_text, label), n in (source_kept - written_kept).items() for _ in range(n))
    write_check = {"missing": missing, "unexpected": [], "tolerated": []}
    if missing:
        details = [_describe_lost_node(label, source_root, written_root) for label in missing]
        raise SchematicWriteNotVerified(
            f"write_not_verified: check top_level_nodes_unchanged on {path.name}: {len(missing)} node(s) of the sheet "
            f"would not survive the write ({'; '.join(f'{d[0]}: {d[1]}' for d in details[:5])}"
            f"{'; and more' if len(details) > 5 else ''})",
            [
                {"file": path.name, "check": "top_level_nodes_unchanged", "subject": subject,
                 "expected": f"{label} present and unchanged", "actual": actual}
                for label, (subject, actual) in zip(missing, details, strict=True)
            ],
            write_check,
            None,
        )

    before, after = _netlist_census(path), _netlist_census(path, written)
    new_refs = sorted(r for r in added_refs if not r.startswith("#") and r not in before["refs"])
    expected = before["components"] + len(new_refs)
    netlist_check = {
        "before": {"components": before["components"], "nets": len(before["nets"])},
        "after": {"components": after["components"], "nets": len(after["nets"])},
        "expected_components": expected,
    }
    mismatches = []
    if lost := sorted((Counter(before["refs"]) - Counter(after["refs"])).elements()):
        mismatches.append({"file": path.name, "expected": f"components {', '.join(lost[:10])}", "actual": None})
    if after["components"] != expected:
        absent = [r for r in new_refs if r not in after["refs"]]
        mismatches.append({
            "file": path.name,
            "expected": f"{expected} components (added: {', '.join(new_refs) or 'none'})",
            "actual": f"{after['components']} components" + (f"; not found: {', '.join(absent)}" if absent else ""),
        })
    for ref, wanted in (expected_fields or {}).items():
        comps = [c for c in after.get("component_records", []) if c["ref"] == ref]
        if ref in before["refs"] or len(comps) != 1:  # a duplicate reference: the netlist keeps one of the two, not ours to guess
            continue  # also: absent (off-board symbol, count already checked above) or not unique
        for name, value in wanted.items():
            got = comps[0]["fields"].get(name)
            if "${" in value or (got or "") == (value if value != "~" else ""):
                continue
            mismatches.append({"file": path.name, "check": "netlist_follows_fields", "expected": f"{ref} field {name} = {value!r}",
                               "actual": "no such field" if got is None else repr(got)})
    net_of = {node: i for i, net in enumerate(after["nets"]) for node in net}
    for net in before["nets"]:
        landed = {net_of.get(node) for node in net}
        if None in landed or len(landed) > 1:
            pins = ", ".join(f"{r}.{p}" for r, p in sorted(net)[:6])
            mismatches.append({"file": path.name, "expected": f"one net holding {pins}", "actual": "split or pin lost"})
    if mismatches:
        raise SchematicWriteNotVerified(
            f"write_not_verified: kicad-cli's netlist of {path.name} after the write does not match before: "
            + "; ".join(f"expected {m['expected']}, got {m['actual'] or 'nothing'}" for m in mismatches[:5]),
            mismatches,
            write_check,
            netlist_check,
        )


def _power_reference(number: int) -> str:
    """A power symbol's reference as KiCad writes it: ``#PWR01`` .. ``#PWR099``, then ``#PWR0100`` and up
    (kicad_tools' own ``#PWR{n:02d}`` gave ``#PWR604``, issue 121)."""
    return f"#PWR{number:02d}" if number < 100 else f"#PWR0{number}"


def _highest_power_number(path: Path) -> int:
    """The highest ``#PWR<n>`` number among the symbols of ``path`` and of every sheet of the project's
    tree (``0`` when there is none), so a power symbol added next gets a reference new tree-wide."""
    import re

    from kicad_mcp import sheet_authoring, sheet_tree

    files = [path]
    try:
        tree = sheet_tree.walk(sheet_authoring.find_root(path, None))
        if not tree.problems and path.resolve() in {Path(f).resolve() for f in tree.files}:
            files = list(tree.files)
    except Exception:  # noqa: BLE001 - a sheet written alone
        pass
    highest = 0
    for file in files:
        for node in write_layer.load(Path(file)).children:
            if node.is_atom or node.name != "symbol":
                continue
            for prop in node.find_children("property"):
                match = re.fullmatch(r"#PWR0*(\d+)", prop.get_string(1)) if prop.get_string(0) == "Reference" else None
                if match:
                    highest = max(highest, int(match.group(1)))
    return highest


def _stale_instance_refs(path: Path, old_uuids: Any) -> list[str]:
    """References of the symbols that were on ``path`` before a call (``old_uuids``) and still carry
    instance data keyed on the sheet's own uuid although a parent sheet uses the sheet (GitHub issue
    107): add_symbols re-keys only what it adds and lists these. ``[]`` for the root sheet, a sheet
    no sheet symbol uses, or a tree that does not read whole."""
    from kicad_mcp import sheet_authoring, sheet_tree

    try:
        root_sch = sheet_authoring.find_root(path, None)
        tree = sheet_tree.walk(root_sch)
    except Exception:  # noqa: BLE001 - no project around it
        return []
    if tree.problems or path.resolve() == Path(root_sch).resolve() or not [u for f, u in tree.uses if f == path.resolve()]:
        return []
    sheet = write_layer.load(path)
    own = "/" + (sheet.find("uuid").get_string(0) if sheet.find("uuid") is not None else "")
    stale = []
    for node in sheet.children:
        if node.is_atom or node.name != "symbol" or node.find("uuid") is None or node.find("uuid").get_string(0) not in old_uuids:
            continue
        paths = [p.get_string(0).lower() for i in node.find_children("instances") for pr in i.find_children("project")
                 for p in pr.find_children("path")]
        if own.lower() in paths:
            ref = next((q.get_string(1) for q in node.find_children("property") if q.get_string(0) == "Reference"), None)
            stale.append(ref or node.find("uuid").get_string(0))
    return sorted(stale)


def _reattach_to_tree(content: str, path: Path) -> str:
    """``content`` (the text about to replace ``path``) with the instance data of the symbols
    keyed on the sheet's own uuid moved to every use path of the project's sheet tree (GitHub
    issue 79), by ``sheet_authoring.rewrite_child_instances``, the rewrite ``add_sheet_symbols``
    makes. A sheet that is the project's root, is not reached from it, or sits in a tree that
    does not read whole keeps ``content`` unchanged."""
    from kicad_mcp import sheet_authoring, sheet_tree, write_layer

    try:
        root_sch = sheet_authoring.find_root(path, None)
        tree = sheet_tree.walk(root_sch)
    except Exception:  # noqa: BLE001 - no project around it: a sheet written alone
        return content
    if tree.problems or path.resolve() == Path(root_sch).resolve():
        return content
    uses = [use for file, use in tree.uses if file == path.resolve()]
    if not uses:
        return content
    node = write_layer.parse_string(content)
    # Only the symbols this call adds are re-keyed: an old symbol is kept unchanged (the "tools only add"
    # check refuses a changed one), stale instance data on it is the sheet's own business (GitHub issue 107).
    try:
        old = {
            str(c.find("uuid").values[0])
            for c in write_layer.load(path).children
            if not c.is_atom and c.name == "symbol" and c.find("uuid") is not None and c.find("uuid").values
        }
    except OSError:
        old = set()
    if not sheet_authoring.rewrite_child_instances(node, Path(root_sch).stem, uses, frozenset(old)):
        return content
    return write_layer.serialise(node) + chr(10)


_ADD_SYMBOL_KEYS = frozenset({"lib_id", "x", "y", "ref", "value", "rotation", "footprint", "unit", "in_bom", "dnp", "snap", "mirror", "properties"})
_MIRROR_AXES = ("x", "y")


def _symbol_spec_refusal(specs: Any) -> dict | None:
    """Why ``specs`` (add_symbols input) is refused before anything is read or written (GitHub issue 111), or None.

    Unknown keys are refused by name (a mistyped ``mirrr`` used to be dropped without a word); ``lib_id``, ``x``,
    ``y`` and ``ref`` are required; ``mirror`` is "x", "y" or "" (none)."""
    from kicad_mcp import sheet_authoring

    def refuse(message: str) -> dict:
        return sheet_authoring.refusal(sheet_authoring.SheetAuthoringError("invalid_spec", message + "; nothing was written"))

    if not isinstance(specs, list):
        return refuse("specs must be a list of objects")
    mirrors: dict[tuple[str, int], str] = {}
    field_sets: dict[tuple[str, str], str] = {}
    for i, spec in enumerate(specs):
        if not isinstance(spec, dict):
            return refuse(f"specs[{i}] must be an object, got {spec!r}")
        unknown = sorted(str(k) for k in set(spec) - _ADD_SYMBOL_KEYS)
        if unknown:
            return refuse(f"specs[{i}] has unknown key(s) {', '.join(map(repr, unknown))}; the keys are {sorted(_ADD_SYMBOL_KEYS)}")
        missing = [k for k in ("lib_id", "x", "y", "ref") if k not in spec]
        if missing:
            return refuse(f"specs[{i}] lacks required key(s) {', '.join(missing)}")
        for key in ("x", "y"):
            if isinstance(spec[key], bool) or not isinstance(spec[key], (int, float)) or not math.isfinite(spec[key]):
                return refuse(f"specs[{i}].{key} must be a finite number in mm, got {spec[key]!r}")
        for key in ("lib_id", "ref"):
            if not isinstance(spec[key], str) or not spec[key]:
                return refuse(f"specs[{i}].{key} must be a non-empty string, got {spec[key]!r}")
        rotation = spec.get("rotation", 0)
        if isinstance(rotation, bool) or not isinstance(rotation, (int, float)) or rotation not in (0, 90, 180, 270):
            return refuse(f"specs[{i}].rotation must be 0, 90, 180 or 270, got {rotation!r}")
        unit = spec.get("unit", 1)
        if isinstance(unit, bool) or not isinstance(unit, int) or unit < 1:
            return refuse(f"specs[{i}].unit must be a whole number from 1, got {unit!r}")
        mirror = spec.get("mirror", "")
        if not (isinstance(mirror, str) and mirror in ("", *_MIRROR_AXES)):
            return refuse(f"specs[{i}].mirror must be \"x\", \"y\" or \"\" (none), got {mirror!r}")
        if "properties" in spec:
            try:
                checked = _check_symbol_properties(spec["properties"], f"specs[{i}].properties")
            except sheet_authoring.SheetAuthoringError as exc:
                return refuse(str(exc))
            if "Footprint" in checked and spec.get("footprint"):
                return refuse(f"specs[{i}] gives the footprint twice (footprint and properties.Footprint); give one")
        # KiCad's netlist reads one unit's copy of a part's fields, so the units of one symbol must ask for the same fields
        fieldset = repr((spec.get("footprint") or "", sorted((spec.get("properties") or {}).items(), key=repr)))
        if not spec["ref"].endswith("?") and field_sets.setdefault((spec["ref"], spec["lib_id"]), fieldset) != fieldset:
            return refuse(f"specs[{i}] places another unit of {spec['ref']} of the same symbol with a different footprint or properties; "
                          "the units of one symbol share their fields, so give the same ones")
        asked = mirrors.setdefault((spec["ref"], unit), mirror)
        if asked != mirror and not spec["ref"].endswith("?"):  # unannotated specs are separate placements
            return refuse(f"specs[{i}] places {spec['ref']} unit {unit} again with a different mirror ({asked!r} before, {mirror!r} now)")
    return None


def _check_symbol_properties(properties: Any, where: str) -> dict[str, tuple[str, bool | None]]:
    """The per-entry field overrides of an add_symbols spec (issue 162): ``{name: value | {value, hidden}}``."""
    from kicad_mcp import sheet_authoring, sheet_editing

    if not isinstance(properties, dict):
        raise sheet_authoring.SheetAuthoringError("invalid_spec", f"{where} must be an object {{property name: value | {{value, hidden}}}}")
    out: dict[str, tuple[str, bool | None]] = {}
    for name, value in properties.items():
        name = sheet_editing._property_name(name, where)
        if name in ("Reference", "Value", "uuid"):
            raise sheet_authoring.SheetAuthoringError(
                "invalid_spec", f"{where}: {name!r} is not an override here (use the spec's ref and value keys)")
        out[name] = sheet_editing._property_value(value, f"{where}[{name!r}]")
    return out


class _FieldPairingError(ValueError):
    """The new symbols in the text do not pair one to one with the specs (raised only after the other write checks)."""


def _library_field_overrides(specs: list[dict]) -> list[dict]:
    """Per spec, in spec order (the placement, not the reference: two ``R?`` specs are two placements): the field names the
    spec fixes itself (``skip``), the override values and whether it gave a value."""
    out: list[dict] = []
    for spec in specs:
        props = _check_symbol_properties(spec.get("properties", {}), "properties")
        skip = set(props)
        if spec.get("footprint"):
            skip.add("Footprint")
        out.append({"skip": frozenset(skip), "properties": props, "value_given": bool(spec.get("value"))})
    return out


def _apply_library_fields(root: Any, overrides: list[dict], old_uuids: set[str]) -> dict[str, dict[str, str]]:
    """Give each symbol this call added every field of its embedded library symbol, as KiCad does (issue 162), then the
    spec's own overrides. Returns ref -> {field name: value} of every field the netlist should carry (Reference and Value
    aside)."""
    from kicad_mcp import sheet_editing

    expected: dict[str, dict[str, str]] = {}
    seen: dict[str, dict[str, str]] = {}
    conflicting: set[str] = set()
    new_nodes = [n for n in root.children
                 if not n.is_atom and n.name == "symbol" and n.find_children("lib_id") and n.find_children("uuid")
                 and n.find_children("uuid")[0].get_string(0) not in old_uuids]
    if len(new_nodes) != len(overrides):  # the new symbols come in spec order, one per spec; anything else is not guessed
        raise _FieldPairingError(f"{len(new_nodes)} new symbol(s) in the text for {len(overrides)} spec(s); the sheet was left unchanged")
    for node, asked in zip(new_nodes, overrides, strict=True):
        ref = sheet_editing._ref(node)
        entry = sheet_editing._lib_symbol(root, node)
        if entry is not None:
            sheet_editing.take_library_fields(node, entry, asked["skip"])
            lib_value = sheet_editing._property(entry, "Value")
            value = sheet_editing._property(node, "Value")
            if not asked["value_given"] and lib_value is not None and value is not None and (lib_value.get_string(1) or ""):
                if value.get_string(1) != lib_value.get_string(1):
                    write_layer.set_string(value, 1, lib_value.get_string(1))
        for name, (text, hidden) in asked["properties"].items():
            prop = sheet_editing._property(node, name)
            if prop is None:
                sheet_editing._new_property(node, name, text, True if hidden is None else hidden)
                continue
            write_layer.set_string(prop, 1, text)
            if hidden is not None:
                sheet_editing._set_hidden(prop, hidden)
        if not ref.startswith("#"):
            fields = {p.get_string(0): p.get_string(1) or "" for p in sheet_editing._kids(node, "property")
                      if p.get_string(0) not in ("Reference", "Value")}
            if seen.setdefault(ref, fields) != fields:
                conflicting.add(ref)  # KiCad reads one copy of a part's fields; which one is not ours to guess
            expected[ref] = fields
    for ref in conflicting:
        expected.pop(ref, None)
    return expected


def _unit_of(node: Any) -> int:
    unit = node.find_children("unit")
    return (int(unit[0].get_float(0)) if unit else 1) or 1


def _new_symbol_nodes(root: Any, old_uuids: set[str]) -> list[Any]:
    """The placed-symbol nodes of ``root`` this call added (uuid not in ``old_uuids``), in file order, which is the order of the specs."""
    return [n for n in root.children
            if not n.is_atom and n.name == "symbol" and n.find_children("lib_id") and n.find_children("uuid")
            and n.find_children("uuid")[0].get_string(0) not in old_uuids]


def _apply_mirrors(root: Any, mirrors: list[str], old_uuids: set[str]) -> None:
    """Give the i-th symbol node this call added (uuid not in ``old_uuids``) the i-th entry of ``mirrors`` (spec order, "" for none) as a
    ``(mirror x|y)`` node right after ``(at ..)``, where KiCad writes it, through the write layer. Paired by position, not by
    reference: two ``R?`` specs are two placements."""
    for node, axis in zip(_new_symbol_nodes(root, old_uuids), mirrors, strict=False):
        if not axis or node.find_children("mirror"):
            continue
        at = node.find_children("at")[0]
        node.children.insert(node.children.index(at) + 1, write_layer.SExp.list("mirror", write_layer.symbol(axis)))


def _check_mirrors(content: str, mirrors: list[str], old_uuids: set[str]) -> None:
    """Self-check (decision 4): the text about to be written holds the mirror each new symbol was asked for."""
    from kicad_mcp import sheet_editing

    nodes = _new_symbol_nodes(write_layer.parse_string(content), old_uuids)
    wrong = []
    for i, axis in enumerate(mirrors):
        if not axis:
            continue
        node = nodes[i] if i < len(nodes) else None
        actual = None if node is None else (node.find_children("mirror")[0].children[0].value if node.find_children("mirror") else "")
        if actual != axis:
            wrong.append({"reference": f"{sheet_editing._ref(node)} unit {_unit_of(node)}" if node is not None else f"spec {i}",
                          "expected": axis, "actual": actual})
    if wrong:
        raise SchematicWriteNotVerified(
            f"the mirror of {len(wrong)} new symbol(s) did not survive into the text; the sheet was left unchanged",
            [{"file": "sheet", "check": "mirror", "subject": w["reference"], "expected": w["expected"], "actual": w["actual"]} for w in wrong],
            {"missing": [w["reference"] for w in wrong], "unexpected": [], "tolerated": []}, None)


def _write_schematic_utf8(
    sch: Any, path: Path, *, added_refs: set[str] = frozenset(), reattach: bool = False,
    mirrors: list[str] | None = None, old_uuids: set[str] = frozenset(),
    field_overrides: list[dict] | None = None,
) -> None:
    """Write a kicad_tools high-level ``Schematic`` to ``path`` as UTF-8.

    Replaces ``sch.write(path)`` (build ticket 12). kicad_tools 0.20.0's
    ``Schematic.write()`` calls ``Path.write_text(content)`` without an
    encoding, so on Windows it writes the platform code page (cp1252 here).
    A character cp1252 lacks (``Ω``) raised after the file was truncated and
    left a 0-byte ``.kicad_sch``; one it has (``µ``) was written as a single
    byte that is not UTF-8, and KiCad read that label back as an empty
    string. KiCad files are UTF-8.

    The text goes to a temporary sibling first and is read back; only a
    read-back equal to what was written replaces the original with
    ``os.replace``. Any failure (an unencodable lone surrogate, a short
    write) raises and leaves the original byte-identical. Before the move,
    ``_verify_schematic_write`` checks the content (build ticket 38): every
    top-level node of the original unchanged, and kicad-cli's netlist with
    every component and net it had plus the ``added_refs``; a mismatch raises
    ``SchematicWriteNotVerified``. Newlines are translated as before
    (``write_text`` default, CRLF on Windows). Build ticket 44 moves this onto
    the write layer.
    """
    write_layer.check_writable(path)  # nothing is touched while KiCad holds the sheet (GitHub issue 68)
    if getattr(sch, "_synthesized_pwr_defs", None):
        # write() would also emit kicad_tools_pwr sidecars, with the platform
        # encoding. This toolset never synthesizes power symbols (add_power
        # uses the stock power: library), so refuse rather than skip them.
        raise RuntimeError("synthesized power symbols need kicad_tools' sidecar files; not written")
    sch.auto_size_paper()  # what write() does by default
    content = _schematic_text(sch)
    if reattach:
        content = _reattach_to_tree(content, path)
    if mirrors and any(mirrors):  # GitHub issue 111: kicad_tools' add_symbol has no mirror parameter
        node = write_layer.parse_string(content)
        _apply_mirrors(node, mirrors, set(old_uuids))
        content = write_layer.serialise(node) + chr(10)
        _check_mirrors(content, mirrors, set(old_uuids))
    expected_fields: dict[str, dict[str, str]] | None = None
    deferred: Exception | None = None
    if field_overrides is not None:  # GitHub issue 162: take over every field of the library symbol, as KiCad does
        node = write_layer.parse_string(content)
        try:
            expected_fields = _apply_library_fields(node, field_overrides, set(old_uuids))
            content = write_layer.serialise(node) + chr(10)
        except _FieldPairingError as exc:
            deferred = exc  # raised after the token and netlist checks, so a damaged text is reported by those first
    if "\r" in content:  # backstop: the tools refuse \r in their input before this
        raise ValueError("carriage return in the schematic text; kicad_tools would write it unescaped")
    tmp = path.with_name(path.name + ".utf8.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        if tmp.read_text(encoding="utf-8") != content:
            raise OSError(f"read-back of the written schematic differs from its content; {path.name} left unchanged")
        _verify_schematic_write(path, tmp, set(added_refs), expected_fields)
        if deferred is not None:
            raise deferred
        write_layer.check_writable(path)  # KiCad may have opened the sheet while the checks ran
        write_layer.replace_file(tmp, path)  # retries a transient sharing denial, then file_busy
    finally:
        tmp.unlink(missing_ok=True)


# --------------------------------------------------------------------------
# Orthogonal wire routing (archived backlog P2-06)
#
# Found by real visual inspection of a rendered golden-path schematic
# (2026-09-03), not by any automated check: kicad_tools' add_wire() draws a
# SINGLE straight segment between p1 and p2 regardless of angle, so any
# non-axis-aligned pair produces a diagonal slash across the sheet. ERC is
# perfectly happy with that (a diagonal wire is electrically identical to an
# L-shaped one -- same two endpoints), which is exactly why nobody caught it
# until a human looked at the PDF. Universal schematic convention is
# horizontal/vertical runs with right-angle corners.
#
# kicad_tools *does* ship a private Schematic._route_orthogonal(), but it is
# unusable here for three reasons, so this module computes its own path and
# emits the segments via the public add_wire_path():
#   1. it calls add_wire() WITHOUT forwarding snap -- i.e. it hardcodes the
#      exact grid-snap bug this module has already had to fix twice (see the
#      WARNINGs on add_wires/add_labels below);
#   2. it only ever produces an L (one corner), never a Z, so two pins that
#      face the same direction get a wire that leaves one of them sideways;
#   3. it's private, and its "auto" rule ignores pin orientation entirely.
# Worth an upstream PR per docs/ARCHITECTURE.md's kicad-tools contribution policy.
# --------------------------------------------------------------------------

HORIZONTAL = "horizontal"
VERTICAL = "vertical"
_AXES = (HORIZONTAL, VERTICAL)
_ROUTE_MODES = ("direct", "orthogonal")

#: kicad_tools' own Schematic.POINT_TOLERANCE -- the distance within which a
#: coordinate is considered to be "on" a pin.
_POINT_TOLERANCE = 0.1

Point = tuple[float, float]


def _close(a: float, b: float, tol: float = 1e-6) -> bool:
    return abs(a - b) <= tol


def _grid_round(value: float, grid: float) -> float:
    if not grid:
        return value
    return round(round(value / grid) * grid, 4)


def _dedupe(points: list[Point]) -> list[Point]:
    """Drop consecutive coincident waypoints (a grid-rounded dogleg can
    collapse onto an endpoint), so no zero-length wire segment is emitted."""
    out: list[Point] = []
    for pt in points:
        if not out or not (_close(out[-1][0], pt[0]) and _close(out[-1][1], pt[1])):
            out.append(pt)
    return out


def plan_orthogonal_route(
    p1: Point,
    p2: Point,
    *,
    axis1: str | None = None,
    axis2: str | None = None,
    first: str | None = None,
    grid: float = 1.27,
) -> list[Point]:
    """Compute a Manhattan (right-angle-only) waypoint path from p1 to p2.

    Pure geometry -- no schematic mutation, no I/O. Returns the full waypoint
    list *including* both endpoints, so ``len(result) - 1`` segments are
    needed. The endpoints are returned exactly as given: routing changes only
    the drawn path, never where the wire lands, so it can never alter
    connectivity relative to a direct wire.

    Args:
        p1/p2: endpoints.
        axis1/axis2: the axis the wire should leave p1 / enter p2 along
            (``"horizontal"``/``"vertical"``), where known -- normally derived
            from the pin each endpoint sits on, so the wire leaves a pin along
            the pin's own stub rather than sideways.
        first: explicit override for the first leg's direction; wins over
            axis1/axis2.
        grid: schematic grid, used to keep a Z-path's dogleg on-grid.

    Shapes produced:
        * already axis-aligned -> one straight segment (identical to
          ``route="direct"`` for such a pair);
        * endpoints facing *different* axes, or only one axis known -> an
          **L** (one corner);
        * endpoints facing the *same* axis -> a **Z** (two corners, dogleg at
          the midpoint), because an L would have to leave one of the two pins
          sideways;
        * nothing known -> L, leaving along whichever axis has the longer run
          (most symbol pins point left/right, so a predominantly horizontal
          connection most often wants to leave horizontally).
    """
    x1, y1 = float(p1[0]), float(p1[1])
    x2, y2 = float(p2[0]), float(p2[1])
    p1 = (x1, y1)
    p2 = (x2, y2)

    if _close(x1, x2) or _close(y1, y2):
        return [p1, p2]

    if first in _AXES:
        axis1, axis2 = first, None
    if axis1 not in _AXES:
        axis1 = None
    if axis2 not in _AXES:
        axis2 = None

    if axis1 is not None and axis2 is not None and axis1 == axis2:
        # Z path: both endpoints face the same way, so a single corner would
        # necessarily leave one of them sideways. Dogleg halfway between.
        if axis1 == HORIZONTAL:
            mid = _grid_round((x1 + x2) / 2.0, grid)
            if not min(x1, x2) < mid < max(x1, x2):
                mid = (x1 + x2) / 2.0
            return _dedupe([p1, (mid, y1), (mid, y2), p2])
        mid = _grid_round((y1 + y2) / 2.0, grid)
        if not min(y1, y2) < mid < max(y1, y2):
            mid = (y1 + y2) / 2.0
        return _dedupe([p1, (x1, mid), (x2, mid), p2])

    if axis1 is None:
        if axis2 is not None:
            # An L that enters p2 along axis2 leaves p1 along the other axis.
            axis1 = VERTICAL if axis2 == HORIZONTAL else HORIZONTAL
        else:
            axis1 = HORIZONTAL if abs(x2 - x1) >= abs(y2 - y1) else VERTICAL

    corner = (x2, y1) if axis1 == HORIZONTAL else (x1, y2)
    return _dedupe([p1, corner, p2])


def _pin_axis_index(sch: Any) -> list[tuple[Point, str | None]]:
    """Best-effort map of absolute pin position -> the axis that pin points
    along, for every placed symbol in ``sch``.

    A pin's ``angle`` is the direction its graphic extends into the symbol
    body; the wire therefore runs along that same axis (mod 180), and the
    symbol's own rotation adds to it. The library's Y-up -> schematic Y-down
    flip negates the angle, which cannot change an axis taken mod 180, so it
    is safely ignored here.

    Two different pins landing on the same point with conflicting axes yields
    ``None`` (unknown) for that point rather than an arbitrary pick. Entirely
    best-effort: any symbol whose definition can't be resolved is skipped, and
    routing simply falls back to its geometric heuristic.
    """
    index: dict[Point, set[str]] = {}
    for symbol in getattr(sch, "symbols", []) or []:
        symbol_def = getattr(symbol, "symbol_def", None)
        pins = getattr(symbol_def, "pins", None) or []
        for pin in pins:
            try:
                pos = symbol.pin_position(pin.number or pin.name)
            except Exception:  # noqa: BLE001 - best-effort hint only
                continue
            axis = HORIZONTAL if (float(pin.angle) + float(symbol.rotation)) % 180 == 0 else VERTICAL
            index.setdefault((round(pos[0], 2), round(pos[1], 2)), set()).add(axis)
    return [(pos, next(iter(axes)) if len(axes) == 1 else None) for pos, axes in index.items()]


def _axis_at(index: list[tuple[Point, str | None]], point: Point, tol: float = _POINT_TOLERANCE) -> str | None:
    for pos, axis in index:
        if abs(pos[0] - point[0]) <= tol and abs(pos[1] - point[1]) <= tol:
            return axis
    return None



def _refine_connectivity(tree: Any, root_path: Path, unconnected: list, issues: list, missing: list):
    """Replace the unconnected pins and floating-wire findings of ``kicad_tools`` by the raw reading
    of ``connectivity_check`` and drop power symbols from ``missing`` (they are never in a netlist)."""
    found: list[dict] = []
    marked: list[dict] = []
    kept_issues: list = []
    power: set[tuple[str, str]] = set()
    root_resolved = root_path.resolve()
    for sheet in tree.files:
        name = "" if sheet.resolve() == root_resolved else sheet.name
        raw = write_layer.load(sheet)
        sub_found, sub_marked = connectivity_check.unconnected_pins(raw, name, unconnected)
        found += sub_found
        marked += sub_marked
        kept_issues += connectivity_check.refine_issues(raw, [i for i in issues if getattr(i, "sheet", "") == name], name)
        power |= {(name, ref) for ref in connectivity_check.power_symbol_references(raw)}
    kept_issues += [i for i in issues if getattr(i, "sheet", "") not in {("" if s.resolve() == root_resolved else s.name) for s in tree.files}]
    missing = [m for m in missing if (m.sheet, m.reference) not in power]
    return found, marked, kept_issues, missing


def _read_only(tool_fn):
    """Mark a tool function of ``build`` as read-only (``readOnlyHint`` true). Every function defined
    in ``build`` is a tool and is registered by the end of ``build`` (issue 147): there is no list to
    add a new tool to. A tool without this mark is a writing tool (destructive, not read-only)."""
    tool_fn._kicad_read_only = True
    return tool_fn


def build(mcp: MCPServer) -> list[str]:
    """Register the schematic toolset. See toolsets/registry.py for the contract."""

    def add_symbols(sch_path: str, specs: list[dict]) -> dict:
        """Place one or more symbols onto an existing schematic in one call.

        Each spec: lib_id, x, y, ref (required); value, rotation, footprint,
        unit, in_bom, dnp, snap, mirror, properties (optional). Batch-oriented -- never loop this one
        symbol at a time.

        Library fields (issue 162): like a symbol placed by hand in KiCad, each new instance takes over every
        field of its library symbol -- Footprint, Datasheet, Description, LCSC and any custom field -- with the
        library's hidden state, and the library's Value unless the spec gives `value`. A field the library
        lacks stays absent. Overrides win: `footprint` (as before) and `properties`, an object
        {field name: text | {value, hidden}} that sets or adds fields (a field the library lacks is added
        hidden unless "hidden": false; Reference, Value and a name with edge white space are refused as
        invalid_spec; giving the footprint in both `footprint` and `properties` is refused; units of one
        symbol must ask for the same fields). Before the sheet is replaced, kicad-cli's netlist of the
        project must carry those fields on every new component, else the call is refused
        as a failed write check (check netlist_follows_fields) and the sheet stays unchanged. Fields of existing symbols change with set_properties, and replace_symbols
        takes a new library symbol's fields with library_fields true.

        mirror (issue 111): "x", "y" or "" (none; the default). KiCad applies the rotation first and
        then the mirror: "y" flips the symbol left to right (a connector's pins that were on its left
        are on its right, each pin keeping its row), "x" flips it top to bottom. It is stored as
        (mirror x|y) after (at ..) and read back in `symbols` as `mirror`, with the pin points already
        transformed. A key that is not listed, a missing required key, a non-numeric x or y or any other
        mirror value is refused as invalid_spec naming it (nothing written).

        Positions are placed as given (issue 86): the origin is NOT rounded to the 1.27 mm grid
        unless a spec carries "snap": true, because existing callers place at arbitrary
        coordinates. The result reads back every symbol and unit that was written, from the raw
        sheet: `symbols` ({reference, uuid, lib_id, unit, at, rotation, mirror, asked, pins [{number,
        name, x, y}], pin_count, off_grid_pins}), `snapped` (what "snap": true moved), `grid`.
        Use those pin points, or wire by pin with connect_pins, and do not compute them.

        A lib_id's library nickname is resolved as KiCad does: the project's sym-lib-table
        first (${KIPRJMOD} is the project folder), then the user's global sym-lib-table, then
        KiCad's own symbol folder; a nickname a table declares is decided by that table. The
        library file must be named <nickname>.kicad_sym and lie inside the project or the KiCad
        install. An unknown nickname is refused (nothing written) with the tables searched.

        On a sheet that a parent sheet uses (add_sheet_symbols), each new symbol's instance data
        gets the project's name and one path per use of the sheet in the tree; on the root sheet,
        or a sheet no sheet symbol uses yet, it keeps the sheet's own uuid path. Symbols already on
        the sheet are never re-keyed; those that still carry the sheet's own uuid path are listed
        in `stale_instance_symbols` (references; empty when none, and also when the sheet tree cannot be read).

        Refuses a sheet older than KiCad 8's format (error old_sheet_format, nothing
        written): kicad_tools would rewrite it and drop what it does not model. Before
        the sheet is replaced, every node it had must be unchanged (the paper size
        aside, which the write may enlarge) and kicad-cli's netlist must keep every
        component and every connection; otherwise the result is ok: false and lists
        each mismatch, and the sheet stays unchanged.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        if (refused := _symbol_spec_refusal(specs)) is not None:
            return refused
        if (refused := _refuse_carriage_returns(specs, "specs")) is not None:
            return refused
        _ensure_kicad_symbol_dir_set()
        if (refused := _old_format_refusal(path)) is not None:
            return refused
        lib_ids = [spec.get("lib_id") for spec in specs]
        libs = symbol_libs.resolve_symbol_libraries(path, lib_ids)
        if libs.problems:
            return _incomplete(libs.reason)
        try:
            from kicad_tools.schematic.models.schematic import Schematic

            sch = Schematic.load(path)
            symbol_libs.seed_symbol_defs(sch, libs, lib_ids)
            placed = []
            before_uuids = pin_wiring.symbol_uuids(write_layer.load(path))
            grid = float(getattr(sch, "grid", 1.27) or 1.27)
            snapped_refs: list[dict] = []
            for index, spec in enumerate(specs):
                x, y = spec["x"], spec["y"]
                if spec.get("snap", False):  # opt in only: callers place at arbitrary coordinates today
                    sx, sy = pin_wiring.snap_value(float(x), grid), pin_wiring.snap_value(float(y), grid)
                    if (sx, sy) != (x, y):
                        snapped_refs.append({"spec": index, "ref": spec["ref"], "asked": [x, y], "now": [sx, sy]})
                    x, y = sx, sy
                instance = sch.add_symbol(
                    lib_id=spec["lib_id"],
                    x=x,
                    y=y,
                    ref=spec["ref"],
                    value=spec.get("value"),
                    rotation=spec.get("rotation", 0),
                    footprint=spec.get("footprint", ""),
                    unit=spec.get("unit", 1),
                    in_bom=spec.get("in_bom", True),
                    dnp=spec.get("dnp", False),
                )
                placed.append(instance.reference)
            _write_schematic_utf8(sch, path, added_refs=set(placed), reattach=True,
                                  mirrors=[spec.get("mirror", "") for spec in specs], old_uuids=before_uuids,
                                  field_overrides=_library_field_overrides(specs))
        except SchematicWriteNotVerified as exc:
            return _not_verified_result(exc)
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            if (locked := write_layer.as_lock_refusal(exc)) is not None:
                return locked
            return _incomplete(f"could not add symbols: {exc}")
        symbols = pin_wiring.read_back(write_layer.load(path), before_uuids, grid)
        # the new symbol nodes come back in the order the specs were written: one per spec
        by_spec = len(symbols) == len(specs)
        moved = {r["spec"] for r in snapped_refs}
        for index, entry in enumerate(symbols):
            entry["spec"] = index if by_spec else None
            entry["asked"] = [specs[index]["x"], specs[index]["y"]] if by_spec else None
            entry["snapped"] = index in moved if by_spec else None
        return {"status": "ok", "placed": placed, "count": len(placed), "symbols": symbols, "grid": grid,
                "snapped": snapped_refs,
                "stale_instance_symbols": _stale_instance_refs(path, before_uuids),
                "grid_note": "Positions are NOT snapped to the grid unless a spec says \"snap\": true; "
                             "off_grid_pins lists the pin numbers whose point is off the grid. "
                             "Wire by pin with connect_pins."}

    def add_wires(sch_path: str, specs: list[dict]) -> dict:
        """Add one or more wires to an existing schematic in one call,
        drawn with right-angle corners (no diagonals) by default.

        Each spec:
          * `p1` [x, y], `p2` [x, y]  (required)
          * `route`: "orthogonal" (default) | "direct".
          * `snap`: bool, default True -- see SNAP below.
          * `first`: "horizontal" | "vertical" (optional) -- force the first
            leg's direction instead of letting the router infer it.
          * `p1_axis` / `p2_axis`: "horizontal" | "vertical" (optional) --
            declare the axis the wire must leave p1 / enter p2 along, when
            the endpoint isn't on a pin this tool can look up itself.

        ROUTING (archived backlog P2-06, found by looking at a rendered PDF, not by
        any automated check): kicad_tools' add_wire() draws one straight
        segment between the two points whatever their angle, so any
        non-axis-aligned pair came out as a diagonal slash across the sheet.
        ERC never complains -- a diagonal is electrically identical -- but it
        violates universal schematic convention and is hard for a human to
        read. `"route": "orthogonal"` (the default) instead emits a connected
        2-3 segment L or Z path. The endpoints are never moved by routing, so
        a spec's connectivity is identical either way; only the drawn path
        changes. Where an endpoint sits on a symbol pin, the router leaves
        along that pin's own axis (a wire leaving a pin sideways looks wrong
        even when it's orthogonal), using a Z-dogleg when both pins face the
        same way. Use `"route": "direct"` only when you deliberately want the
        old single-segment behavior.

        SNAP (issue 86; behind it the bug of 2026-09-02: kicad_tools' own snap moved a wire end
        that sat exactly on an off-grid pin off the pin, so the connection never existed). `snap`
        (default true) rounds p1 and p2 to the 1.27 mm grid, one coordinate at a time, EXCEPT a
        point within 0.01 mm of something already on the sheet: a pin, a label, a junction, a sheet
        pin, a no-connect marker or the end of a wire. What is there wins: the point lands exactly on
        it, on the grid or not (a point that already equals it stays). The result says what
        happened: `snapped` ({spec, point, asked, now}) for the points the grid moved,
        `landed_on_pin` for the points moved onto such a connector point, and `grid`. The corners of
        an L or Z derive from the two end points. With "snap": false NOTHING is moved or rounded,
        not even next to a pin (for a free wire end that is off the grid on purpose). Take a pin's coordinates from get_symbol_pins, or better wire by pin with
        connect_pins.

        Returns `count` (specs drawn), `segments` (wire segments actually
        emitted, >= count once any spec takes an L or Z path), `grid`, `snapped`
        and `landed_on_pin`.

        Refuses a sheet older than KiCad 8's format (error old_sheet_format, nothing
        written): kicad_tools would rewrite it and drop what it does not model. Before
        the sheet is replaced, every node it had must be unchanged (the paper size
        aside, which the write may enlarge) and kicad-cli's netlist must keep every
        component and every connection; otherwise the result is ok: false and lists
        each mismatch, and the sheet stays unchanged.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        if (refused := _refuse_carriage_returns(specs, "specs")) is not None:
            return refused
        if (refused := _old_format_refusal(path)) is not None:
            return refused
        try:
            from kicad_tools.schematic.models.schematic import Schematic

            sch = Schematic.load(path)
            pin_axes = _pin_axis_index(sch)
            pins = pin_wiring.connector_points(write_layer.load(path))
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"could not load schematic: {exc}")
        grid = float(getattr(sch, "grid", 1.27) or 1.27)

        added, segments = 0, 0
        errors: list[dict] = []
        snapped: list[dict] = []
        landed: list[dict] = []
        for i, spec in enumerate(specs):
            try:
                p1 = (float(spec["p1"][0]), float(spec["p1"][1]))
                p2 = (float(spec["p2"][0]), float(spec["p2"][1]))
                snap_on = bool(spec.get("snap", True))
                for label, asked in (("p1", p1), ("p2", p2)):
                    got, how = pin_wiring.land(asked, pins, grid, snap_on)
                    if how != "unchanged":
                        (landed if how == "pin" else snapped).append(
                            {"spec": i, "point": label, "asked": [asked[0], asked[1]], "now": [got[0], got[1]]})
                    if label == "p1":
                        p1 = got
                    else:
                        p2 = got
                route = spec.get("route", "orthogonal")
                if route not in _ROUTE_MODES:
                    raise ValueError(f"unknown route mode {route!r} (expected one of {list(_ROUTE_MODES)})")
                if route == "direct":
                    points = [p1, p2]
                else:
                    points = plan_orthogonal_route(
                        p1,
                        p2,
                        axis1=spec.get("p1_axis") or _axis_at(pin_axes, p1),
                        axis2=spec.get("p2_axis") or _axis_at(pin_axes, p2),
                        first=spec.get("first"),
                        grid=getattr(sch, "grid", 1.27) or 1.27,
                    )
                # The end points were landed on a pin or rounded to the grid above (issue 86) and the
                # route corners derive from them, so kicad_tools must not snap again: it would move a
                # pin-exact end off an off-grid pin. Do NOT pass snap=True here.
                sch.add_wire_path(*points, snap=False)
                segments += len(points) - 1
                added += 1
            except Exception as exc:  # noqa: BLE001 - evidence-gated, per-spec
                errors.append({"index": i, "reason": str(exc)})

        if added:
            try:
                _write_schematic_utf8(sch, path)
            except SchematicWriteNotVerified as exc:
                return {**_not_verified_result(exc), "errors": errors}
            except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
                if (locked := write_layer.as_lock_refusal(exc)) is not None:
                    return locked
                return _incomplete(f"could not write schematic: {exc}", errors=errors)
        if not added:
            if not specs:
                return {"status": "ok", "count": 0, "segments": 0}
            return _incomplete("no wires could be added", errors=errors, count=0, segments=0)
        result = {"status": "partial" if errors else "ok", "count": added, "segments": segments,
                  "grid": grid, "snapped": snapped, "landed_on_pin": landed}
        if errors:
            result["errors"] = errors
        return result

    def add_junctions(sch_path: str, points: list[list[float]]) -> dict:
        """Add one or more junctions to an existing schematic in one call.

        `points` is a list of [x, y] pairs. Batch-oriented.

        Refuses a sheet older than KiCad 8's format (error old_sheet_format, nothing
        written): kicad_tools would rewrite it and drop what it does not model. Before
        the sheet is replaced, every node it had must be unchanged (the paper size
        aside, which the write may enlarge) and kicad-cli's netlist must keep every
        component and every connection; otherwise the result is ok: false and lists
        each mismatch, and the sheet stays unchanged.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        if (refused := _old_format_refusal(path)) is not None:
            return refused
        try:
            from kicad_tools.schematic.models.schematic import Schematic

            sch = Schematic.load(path)
            for x, y in points:
                sch.add_junction(x, y)
            _write_schematic_utf8(sch, path)
        except SchematicWriteNotVerified as exc:
            return _not_verified_result(exc)
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            if (locked := write_layer.as_lock_refusal(exc)) is not None:
                return locked
            return _incomplete(f"could not add junctions: {exc}")
        return {"status": "ok", "count": len(points)}

    def add_labels(sch_path: str, specs: list[dict]) -> dict:
        """Add one or more labels/power symbols/no-connects to an existing
        schematic in one call.

        Each spec: kind ("label"|"global"|"hier"|"power"|"no_connect"),
        x, y (required); text (required for label/global/hier/power -- for
        "power" it's the power symbol lib_id, e.g. "power:GND", resolved through the project
        sym-lib-table, then the global one, then KiCad's folder, as add_symbols does; the symbol is placed as
        add_symbols places a power lib_id, so its instance data is the same: the project's name and the
        use path(s) on an attached sheet, and a reference #PWR01.. that is new in the whole sheet tree), rotation,
        shape (global/hier label direction), snap (optional bool, default
        True -- see SNAP).

        SNAP (issue 86; behind it the bug of 2026-09-02: kicad_tools' own snap moved a label or
        power flag that sat exactly on an off-grid pin off the pin, onto a phantom net). `snap`
        (default true) rounds x and y to the 1.27 mm grid EXCEPT a point within 0.01 mm of something
        already on the sheet (a pin, label, junction, sheet pin, no-connect marker or wire end): that
        wins, the item lands exactly on it, on the grid or not. The result lists `snapped` ({spec,
        asked, now}), `landed_on_pin` and `grid`. With "snap": false NOTHING is moved, not even next
        to a pin.

        Refuses a sheet older than KiCad 8's format (error old_sheet_format, nothing
        written): kicad_tools would rewrite it and drop what it does not model. Before
        the sheet is replaced, every node it had must be unchanged (the paper size
        aside, which the write may enlarge) and kicad-cli's netlist must keep every
        component and every connection; otherwise the result is ok: false and lists
        each mismatch, and the sheet stays unchanged.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        if (refused := _refuse_carriage_returns(specs, "specs")) is not None:
            return refused
        _ensure_kicad_symbol_dir_set()
        if (refused := _old_format_refusal(path)) is not None:
            return refused
        power_ids = [spec.get("text") for spec in specs if spec.get("kind") == "power"]
        libs = symbol_libs.resolve_symbol_libraries(path, power_ids)
        if libs.problems:
            return _incomplete(libs.reason)
        try:
            from kicad_tools.schematic.models.schematic import Schematic

            sch = Schematic.load(path)
            symbol_libs.seed_symbol_defs(sch, libs, power_ids)
            pins = pin_wiring.connector_points(write_layer.load(path))
            grid = float(getattr(sch, "grid", 1.27) or 1.27)
            next_power = _highest_power_number(path)
            placed = []
            snapped: list[dict] = []
            landed: list[dict] = []
            for i, spec in enumerate(specs):
                kind = spec["kind"]
                x, y = spec["x"], spec["y"]
                rotation = spec.get("rotation", 0)
                # issue 86: the grid applies here and pins win; kicad_tools is told not to snap again
                got, how = pin_wiring.land((float(x), float(y)), pins, grid, bool(spec.get("snap", True)))
                if how != "unchanged":
                    (landed if how == "pin" else snapped).append({"spec": i, "asked": [x, y], "now": [got[0], got[1]]})
                    x, y = got
                snap = False
                if kind == "label":
                    sch.add_label(spec["text"], x, y, rotation=rotation, snap=snap)
                elif kind == "global":
                    sch.add_global_label(spec["text"], x, y, shape=spec.get("shape", "bidirectional"), rotation=rotation, snap=snap)
                elif kind == "hier":
                    sch.add_hier_label(spec["text"], x, y, shape=spec.get("shape", "input"), rotation=rotation, snap=snap)
                elif kind == "power":
                    # issue 121: placed exactly as add_symbols places a power lib_id (same instance data:
                    # the project's name, the sheet-tree use paths), with a reference that is new tree-wide.
                    next_power += 1
                    sch.add_symbol(lib_id=spec["text"], x=x, y=y, ref=_power_reference(next_power),
                                   rotation=rotation, snap=snap)
                elif kind == "no_connect":
                    sch.add_no_connect(x, y, snap=snap)
                else:
                    return {"status": "error", "reason": f"unknown label kind: {kind!r}"}
                placed.append(kind)
            _write_schematic_utf8(sch, path, reattach=True)
        except SchematicWriteNotVerified as exc:
            return _not_verified_result(exc)
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            if (locked := write_layer.as_lock_refusal(exc)) is not None:
                return locked
            return _incomplete(f"could not add labels: {exc}")
        return {"status": "ok", "placed": placed, "count": len(placed), "grid": grid, "snapped": snapped, "landed_on_pin": landed}

    def assign_footprints(sch_path: str, mapping: dict[str, str]) -> dict:
        """Assign footprints to symbols by reference designator in one call.

        `mapping` is {reference: footprint_id}. Batch-oriented -- assigns
        every footprint a design needs in one call, not one call per part.
        Before writing, it copies every sheet to the returned `backup_dir`, outside the project.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        if (refused := _refuse_carriage_returns(mapping, "mapping")) is not None:
            return refused
        # A footprint id is one line. The write layer would escape a LF, but no footprint id
        # holds one (build ticket 25: kicad_tools' text substitution wrote it raw and the
        # sub-sheet fell out of the design, 68 to 10 components).
        if line_feeds := [f"mapping.{ref}" for ref, fp in mapping.items() if "\n" in f"{ref}{fp}"]:
            return _incomplete(f"line feed in {', '.join(line_feeds)}: a footprint id is one line; nothing was written")
        backup_dir: Path | None = None
        written: list[Path] = []
        current: Path | None = None
        try:
            from kicad_mcp import _self_check, sheet_tree, write_layer

            tree = sheet_tree.walk(path)
            # Before kicad-cli runs (it leaves a .kicad_prl behind) and before the backup: a sheet of
            # the tree that KiCad holds stops the call with nothing touched (GitHub issue 68).
            write_layer.check_writable(*tree.files)
            # kicad_tools_writes=True keeps build ticket 26's refusal of a tree reached through
            # KiCad 6's "Sheet file" key, although kicad_tools no longer writes here (ticket 25).
            if (refused := _sheet_tree_refusal(tree, kicad_tools_writes=True) or _backup_root_refusal(tree)) is not None:
                return refused
            try:
                stage = _stage_footprints(tree, mapping)
            except write_layer.InvalidStringError as exc:
                bad = [f"mapping.{ref}" for ref, fp in mapping.items() if not _writable(fp)]
                return _incomplete(f"{', '.join(bad) or 'mapping'}: {exc}; nothing was written", **tree.report())
            if stage.refused:
                return _incomplete(
                    "; ".join(stage.refused) + ". KiCad keeps one footprint per symbol, shared by every use of "
                    "its sheet; nothing was written",
                    refused=stage.refused,
                    **tree.report(),
                )
            reasons = stage.reasons
            # kicad-cli's netlist of the root before and after: one component per placed
            # instance on every sheet of the tree (the root file alone misses sub-sheet symbols).
            before = {c["ref"]: c["footprint"] for c in sheet_tree.netlist_components(path)}
            backup_dir = _backup_outside_project(tree)
            for current, root, setting in stage.changed:
                _save_footprint_sheet(current, root, setting, tree.rel(current))
                written.append(current)
            footprints = {c["ref"]: c["footprint"] for c in sheet_tree.netlist_components(path)}
            assigned = {ref: fp for ref, fp in mapping.items() if footprints.get(ref) == fp}
            missing = [ref for ref in mapping if ref not in assigned]
        except FootprintWriteNotVerified as exc:
            return {
                "ok": False,
                "status": "error",
                "error": "write_not_verified",
                "reason": f"write_not_verified: {exc}; {tree.rel(current)} was left unchanged",
                "mismatches": {"count": len(exc.mismatches), "items": exc.mismatches[:20]},
                "write_check": _self_check.bounded(exc.write_check),
                "written_sheets": [tree.rel(s) for s in written],
                "backup_dir": str(backup_dir),
                **tree.report(),
            }
        except write_layer.WriteLayerError as exc:  # str(exc) is "<code>: <message>"
            if (locked := write_layer.as_lock_refusal(exc)) is not None:
                return {
                    **locked,
                    "written_sheets": [tree.rel(s) for s in written],
                    "backup_dir": str(backup_dir) if backup_dir is not None else None,
                    **tree.report(),
                }
            return _incomplete(
                f"{exc}; {tree.rel(current) if current else 'no sheet'} was left unchanged",
                error=exc.code,
                written_sheets=[tree.rel(s) for s in written],
                backup_dir=str(backup_dir),
                **tree.report(),
            )
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            # after a write the backup is the way back: never drop it from the answer
            extra = {"backup_dir": str(backup_dir)} if backup_dir is not None else {}
            return _incomplete(f"could not assign footprints: {exc}", **extra)
        # Backstop: a component outside the mapping must read back with the footprint it had.
        # Staging changes only the symbols the mapping names in full, so this does not fire
        # unless the write itself went wrong; then the backup is the way back.
        if strays := [
            {"ref": ref, "before": fp, "after": footprints.get(ref)}
            for ref, fp in sorted(before.items())
            if ref not in mapping and footprints.get(ref) != fp
        ]:
            return {
                "ok": False,
                "status": "error",
                "error": "footprint_changed_outside_mapping",
                "reason": "kicad-cli's netlist after the write shows a footprint the mapping does not name "
                f"changed: {', '.join(s['ref'] for s in strays[:20])}; every sheet as it was is in backup_dir",
                "changed_outside_mapping": {"count": len(strays), "items": strays[:20]},
                "written_sheets": [tree.rel(s) for s in written],
                "backup_dir": str(backup_dir),
                **tree.report(),
            }
        status = "ok" if not missing else "partial"
        result = {
            "status": status,
            "assigned": assigned,
            "not_assigned": missing,
            "backup_dir": str(backup_dir),
            **tree.report(),
        }
        if missing:
            result["reasons"] = {
                ref: reasons.get(ref, f"kicad-cli's netlist of the root shows {footprints.get(ref)!r}")
                for ref in missing
            }
        return result

    def fix_annotation(sch_path: str) -> dict:
        """Automatically renumber duplicate/missing reference designators
        across the whole schematic in one call.
        Before writing, it copies every sheet to the returned `backup_dir`, outside the project.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        backup_dir: Path | None = None
        try:
            from kicad_tools.cli.sch_fix_annotation import run_fix_annotation

            from kicad_mcp import sheet_tree

            tree = sheet_tree.walk(path)
            if (refused := _sheet_tree_refusal(tree, kicad_tools_writes=True) or _backup_root_refusal(tree)) is not None:
                return refused
            # run_fix_annotation's own "net-neutrality gate" shells out to
            # kicad-cli internally and, like Schematic.run_erc() before it
            # (archived backlog P1-14's fix), only checks bundled KiCad 7/8
            # Windows install paths -- doesn't find KiCad 9/10 on its own.
            ensure_kicad_cli_on_path()
            # every placed instance of every sheet, not the root file's symbols only
            before_refs = sorted(ref for ref, _ in sheet_tree.placed_references(tree))
            write_layer.check_writable(*tree.files)  # before the backup and before the in-place write
            backup_dir = _backup_outside_project(tree)
            exit_code = run_fix_annotation(path, dry_run=False, backup=False)
            if exit_code != 0:
                return _incomplete(
                    f"run_fix_annotation exited with code {exit_code}", backup_dir=str(backup_dir), **tree.report()
                )
            after_refs = sorted(ref for ref, _ in sheet_tree.placed_references(sheet_tree.walk(path)))
        except write_layer.WriteLayerError as exc:
            extra = {"backup_dir": str(backup_dir)} if backup_dir is not None else {}
            return {**write_layer.lock_refusal_result(exc), **extra}
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            extra = {"backup_dir": str(backup_dir)} if backup_dir is not None else {}
            return _incomplete(f"could not fix annotation: {exc}", **extra)
        return {
            "status": "ok",
            "references_before": before_refs,
            "references_after": after_refs,
            "changed": before_refs != after_refs,
            "backup_dir": str(backup_dir),
            **tree.report(),
        }

    def create_sheets(root_sch: str, specs: list[dict]) -> dict:
        """Create one or more new, empty sub-sheets (.kicad_sch) inside an existing project in one call.

        root_sch: the project's root sheet. Each spec: file (required; a path relative to the root
        sheet's folder, forward slashes, ending in .kicad_sch, inside the project), title (optional).
        The new file is a blank sheet in the format KiCad 10 writes (version 20260306), so add_symbols,
        add_wires and add_labels can work on it. It is unreferenced until add_sheet_symbols points a
        sheet symbol at it: the result lists it under awaiting_reference. Refuses (ok: false, a code,
        nothing written) when the sheet tree of the project is incomplete (a missing, unparseable or
        cyclic sheet: sheet_tree_incomplete), when a file already exists (sheet_exists) or lies
        outside the project (sheet_outside_project), or on a malformed spec (invalid_spec).

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_authoring

        return sheet_authoring.create_sheets(root_sch, specs)

    def add_sheet_symbols(parent_sch: str, specs: list[dict], root_sch: str | None = None) -> dict:
        """Insert one or more hierarchical sheet symbols, with their sheet pins, into a parent sheet
        in one call, and create the matching hierarchical labels in each child sheet.

        parent_sch: the sheet that receives the symbols. root_sch: the project's root sheet (optional
        when parent_sch is the root or the only .kicad_pro of the folder names it). Each spec: file
        (the child sheet, already created with create_sheets and not yet referenced; relative to
        parent_sch's folder), x, y (mm, top-left corner of the symbol; a multiple of 1.27),
        pins (required, a list), name (the sheet name; default the file's stem), width (default
        30.48), height (default fits the pins). Each pin: name, direction (input, output,
        bidirectional or passive), side (left, right, top or bottom; default left for input,
        bidirectional and passive, right for output), offset (mm along that border from its start,
        a multiple of 1.27 strictly inside the border; default the next free 2.54 mm step). Every
        pin gets a hierarchical label of the same name and direction in the child (a label the
        child already has with the same name and direction is reused; a different direction is
        refused: label_direction_mismatch). The result lists each pin's position so a wire can
        be drawn to it.

        Symbols the child already holds are re-attached: the instance data of each (its project
        and path, written while the sheet stood alone) is rewritten to the project's name and the
        new sheet use's path (parent path plus the sheet symbol's uuid), keeping reference, unit
        and value; the result reports symbols_reattached per sheet. A child whose instance data
        does not match the hierarchy after that is refused (write_not_verified, naming the
        instance data). add_symbols on a sheet that is already attached
        writes the new symbols' instance data the same way: the project's name and one path per
        use of the sheet in the tree.

        Before any file is written the change is built on a scratch copy and checked: the sheet
        tree must be whole (no missing file, no unreferenced sheet), every pin must have its label
        with the same direction, kicad-cli's ERC must show no new violation type beyond the
        unconnected sheet pins (pin_not_connected) and unconnected labels (label_dangling) the new
        interface has until it is wired, and kicad-cli's netlist export must list the sheet.
        Otherwise the result is ok: false with code write_not_verified and nothing is changed.
        Other refusals (ok: false, a code, nothing written): sheet_tree_incomplete,
        sheet_file_missing, sheet_already_referenced, parent_not_in_tree, invalid_spec.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_authoring

        return sheet_authoring.add_sheet_symbols(parent_sch, specs, root_sch)

    def add_sheet_pins(
        parent_sch: str, specs: list[dict], root_sch: str | None = None,
        allow_connectivity_change: bool = False, dry_run: bool = False,
    ) -> dict:
        """Add sheet pins to sheet symbols that are already on a parent sheet, in one call, and create
        (or reuse) the matching hierarchical labels in the child sheets. add_sheet_symbols only takes
        sheet files no sheet symbol uses yet; this is for the symbol that already exists.

        parent_sch: the sheet that holds the sheet symbols. root_sch: the project's root sheet (optional
        when parent_sch is the root or the only .kicad_pro of the folder names it). Each spec: sheet (the
        sheet name; a uuid also matches) or uuid, and pins (a non-empty list). Each pin: name, direction
        (input, output, bidirectional, tri_state or passive), side (left, right, top or bottom; default
        left, right for output), offset (mm along that edge from its start; snapped so the pin sits on the
        1.27 mm grid of the page, strictly inside the edge; default the next free 2.54 mm step). A pin
        never lands on another pin.

        The child sheet gets one hierarchical label per pin, of the same name and direction: a label it
        already has with that name and direction is reused (the result says created or reused, with the
        label's position in the child); a label of the same name with another direction is refused:
        label_direction_mismatch. A pin name the sheet symbol already has is refused: duplicate_pin_name.

        The result lists, per sheet symbol and pin, side, offset and at: the pin's absolute position on
        the parent sheet. get_symbol_pins and connect_pins cannot address a sheet pin (it has no reference
        or pin number); draw to the listed point with add_wires, which lands a point within 0.01 mm of a
        sheet pin exactly on it.

        Before anything is written the change is built on scratch copies and checked: every node the call
        did not select is unchanged (the child sheet's symbols and their instance data included),
        kicad-cli's ERC shows no violation type the project did not show before besides one unconnected
        pin (pin_not_connected) per new pin and one dangling label (label_dangling) per created label,
        and kicad-cli's netlist before and after is compared net by net. A new pin that lands on a wire
        end, label or symbol pin is a connectivity change: connectivity_changed, nothing written, unless
        allow_connectivity_change is true. dry_run true computes and returns everything and writes nothing.
        Other refusals (ok: false, a code, nothing written): sheet_not_found, pin_off_edge,
        pin_position_taken, no_room_on_edge, sheet_file_shared, invalid_spec.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_pins

        return sheet_pins.add_sheet_pins(parent_sch, specs, root_sch, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def remove_sheet_pins(
        parent_sch: str, specs: list[dict], root_sch: str | None = None, remove_child_labels: bool = False,
        allow_connectivity_change: bool = False, dry_run: bool = False,
    ) -> dict:
        """Remove sheet pins from sheet symbols on a parent sheet, in one call. A sheet symbol may end up
        with no pins.

        Each spec: sheet (the sheet name; a uuid also matches) or uuid, and pins (exact pin names) and/or
        pattern (a shell pattern on the pin names). A name that is not a pin, or a pattern that matches
        none, refuses the whole call: pin_not_found.

        The hierarchical label of each pin in the child sheet is LEFT as it is, unless remove_child_labels
        is true; a label with no pin is an ERC error (hier_label_mismatch) until it is removed or its pin
        is added again (add_sheet_pins reuses it). The result says left, removed or absent per pin.

        A pin with something attached (a wire end, label, junction or symbol pin exactly on it; with
        remove_child_labels also on the child's label) is a connectivity change: connectivity_changed,
        nothing written, the attached items listed, unless allow_connectivity_change is true. The netlist
        before and after is compared net by net as well. dry_run true computes and returns everything and
        writes nothing. A sheet file that several sheet symbols use is refused with remove_child_labels
        (sheet_file_shared).

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_pins

        return sheet_pins.remove_sheet_pins(parent_sch, specs, root_sch, remove_child_labels=remove_child_labels,
                                            allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def move_sheet_pins(
        parent_sch: str, specs: list[dict], root_sch: str | None = None,
        allow_connectivity_change: bool = False, dry_run: bool = False,
    ) -> dict:
        """Move sheet pins of sheet symbols on a parent sheet along their edge or to another side, in one
        call. Each spec: sheet (the sheet name; a uuid also matches) or uuid, and pins, a list of objects
        {name, side?, offset?}: side is left, right, top or bottom (default the pin's own), offset is mm
        along that edge from its start (snapped to the 1.27 mm grid of the page, strictly inside the edge;
        without it a pin that changes side takes the next free 2.54 mm step). At least one of side and
        offset is required. A pin never lands on another pin; pin_position_taken, pin_off_edge,
        no_room_on_edge and pin_not_found refuse the whole call. The pin keeps its name, direction and
        uuid; its angle and text justification follow the side. The child sheet is not touched.

        A wire is NOT dragged along: a pin that has something attached at its old point (a wire end,
        label, junction or symbol pin) is a connectivity change, and so is a new point that lands on such
        an item: connectivity_changed, nothing written, the items listed, unless allow_connectivity_change
        is true. The netlist before and after is compared net by net as well. dry_run true computes and
        returns everything and writes nothing. The result lists each pin's old and new absolute position.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_pins

        return sheet_pins.move_sheet_pins(parent_sch, specs, root_sch, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    @_read_only
    def compare_sheet_pins(parent_sch: str, sheets: list[str] | None = None, root_sch: str | None = None) -> dict:
        """Read-only, in one call: compare the sheet pins of the sheet symbols on a parent sheet with the
        hierarchical labels of their child sheets, direction included. It writes nothing and takes no
        write lock. sheets: sheet names (a uuid also matches) to compare; omitted, every sheet symbol of
        parent_sch. root_sch: the project's root sheet (optional when parent_sch is the root or the only
        .kicad_pro of the folder names it).

        Per sheet symbol the result lists every difference: missing_pins (a hierarchical label in the child
        with no pin of that name: name, direction of the label, label_at), missing_labels (a pin with no
        label in the child: name, pin_direction, pin_at), direction_mismatches (name, pin_direction,
        label_direction; kicad-cli's ERC does not report a different direction) and duplicate_labels
        (the same label name twice in the child). pins_in_sync names the pins that agree; differences and
        in_sync summarise, and the top level counts differences and sheets_in_sync. A sheet name that
        matches nothing refuses the whole call: sheet_not_found.

        To make the pins that are missing, call sync_sheet_pins.
        """
        from kicad_mcp import sheet_pins

        return sheet_pins.compare_sheet_pins(parent_sch, sheets, root_sch)

    def sync_sheet_pins(
        parent_sch: str, sheets: list[str] | None = None, root_sch: str | None = None,
        allow_connectivity_change: bool = False, dry_run: bool = False,
    ) -> dict:
        """Create, in one call, the sheet pins that the hierarchical labels of the child sheets ask for: every
        label with no pin of that name gets a pin of the same name and direction on its sheet symbol (side
        and position as add_sheet_pins chooses them: left, right for output, the next free 2.54 mm step on
        the 1.27 mm grid). sheets: sheet names (a uuid also matches); omitted, every sheet symbol of
        parent_sch. Run compare_sheet_pins first to see the differences without a lock.

        This tool never creates or removes a hierarchical label, never changes a direction and never removes
        a pin: which side is right is for the caller to decide. What it leaves comes back under unresolved:
        missing_labels (a pin with no label), direction_mismatches (pin and label disagree; use
        remove_sheet_pins and add_sheet_pins, or change the label with the label tools), duplicate_labels
        and unsupported_directions, with differences_left counting them. A second call with nothing to
        create writes nothing (count 0).

        The same safety as add_sheet_pins: the change is built on scratch copies, every node not selected
        stays unchanged, kicad-cli's ERC may show no new violation type besides the unconnected new pins,
        and kicad-cli's netlist before and after is compared net by net: a new pin that lands on a wire end,
        label or symbol pin is connectivity_changed, nothing written, unless allow_connectivity_change is
        true. dry_run true computes and returns everything and writes nothing. The result lists each pin's
        absolute position (at) like add_sheet_pins.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_pins

        return sheet_pins.sync_sheet_pins(parent_sch, sheets, root_sch, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def delete_symbols(
        sch_path: str, selectors: list[dict], allow_connectivity_change: bool = False,
        remove_dangling: bool = False, dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Delete placed symbols (parts and power symbols) from a sheet in one call.

        Selectors (`selectors`, a list of objects; the keys of one object must all match, the objects are
        joined): references (symbol references; every unit), uuids, pattern (shell pattern on the reference, case sensitive), region [x1, y1, x2, y2] (the symbol's origin inside it, mm), lib_id. A selector that selects nothing, or a listed reference or uuid that does not exist on
        the sheet, refuses the whole call (selector_matched_nothing, not_found; nothing written).

        Connectivity: before anything is written, kicad-cli's netlist of the project is read from a scratch
        copy before and after the change and compared net by net. A split net, two nets merged, a pin left
        with nothing else on its net, a renamed net or a vanished named net refuses the call with
        connectivity_changed (nothing written; the changes are listed under connectivity). Pass
        allow_connectivity_change true when that is meant; the result then lists the changes. The pins of
        deleted symbols are not counted, only what their going does to the rest. dry_run true computes and
        returns everything and writes nothing.

        Dangling items: the result lists under dangling_after the wires with an end that touches
        nothing, the junctions where fewer than three wire ends or pins meet, and the labels that
        touch no wire and no pin, as far as this delete made them so (dangling_before_untouched counts
        those that were already there). Default is report only; remove_dangling true also removes the
        dangling wires and junctions this delete made (repeatedly, until none is left; listed under
        dangling_removed), never labels. The removals are part of the connectivity comparison.

        Refuses a failed self-check (write_not_verified, the sheet unchanged) and a project whose sheet
        tree cannot be read (sheet_tree_incomplete). root_sch names the project's root sheet when sch_path
        is a sub-sheet and its folder holds several projects; a sheet no sheet symbol uses yet is checked
        alone. Instance data of the symbols is never touched.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.

        Returns deleted (reference, uuid, lib_id, at), count, dangling_after, dangling_removed, connectivity (nets_before,
        nets_after, changed, changes) and written.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.delete("symbol", sch_path, selectors, root_sch, remove_dangling=remove_dangling,
                                    allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def delete_wires(
        sch_path: str, selectors: list[dict], allow_connectivity_change: bool = False,
        remove_dangling: bool = False, dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Delete wire segments from a sheet in one call.

        Selectors (`selectors`, a list of objects; the keys of one object must all match, the objects are
        joined): uuids, region [x1, y1, x2, y2] (both ends inside it), net (every wire joined, wire to wire on this sheet, to a label with that text). A selector that selects nothing, or a listed uuid or net that does not exist on
        the sheet, refuses the whole call (selector_matched_nothing, not_found; nothing written).

        Connectivity: before anything is written, kicad-cli's netlist of the project is read from a scratch
        copy before and after the change and compared net by net. A split net, two nets merged, a pin left
        with nothing else on its net, a renamed net or a vanished named net refuses the call with
        connectivity_changed (nothing written; the changes are listed under connectivity). Pass
        allow_connectivity_change true when that is meant; the result then lists the changes. The pins of
        deleted symbols are not counted, only what their going does to the rest. dry_run true computes and
        returns everything and writes nothing.

        Dangling items: the result lists under dangling_after the wires with an end that touches
        nothing, the junctions where fewer than three wire ends or pins meet, and the labels that
        touch no wire and no pin, as far as this delete made them so (dangling_before_untouched counts
        those that were already there). Default is report only; remove_dangling true also removes the
        dangling wires and junctions this delete made (repeatedly, until none is left; listed under
        dangling_removed), never labels. The removals are part of the connectivity comparison.

        Refuses a failed self-check (write_not_verified, the sheet unchanged) and a project whose sheet
        tree cannot be read (sheet_tree_incomplete). root_sch names the project's root sheet when sch_path
        is a sub-sheet and its folder holds several projects; a sheet no sheet symbol uses yet is checked
        alone. Instance data of the symbols is never touched.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.

        Returns deleted (uuid, points), count, dangling_after, dangling_removed, connectivity (nets_before,
        nets_after, changed, changes) and written.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.delete("wire", sch_path, selectors, root_sch, remove_dangling=remove_dangling,
                                    allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def delete_labels(
        sch_path: str, selectors: list[dict], allow_connectivity_change: bool = False,
        remove_dangling: bool = False, dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Delete net labels, global labels and hierarchical labels from a sheet in one call.

        Selectors (`selectors`, a list of objects; the keys of one object must all match, the objects are
        joined): texts (exact label texts), uuids, pattern (shell pattern on the text), region [x1, y1, x2, y2], kind (label, global or hier). A selector that selects nothing, or a listed text or uuid that does not exist on
        the sheet, refuses the whole call (selector_matched_nothing, not_found; nothing written).

        Connectivity: before anything is written, kicad-cli's netlist of the project is read from a scratch
        copy before and after the change and compared net by net. A split net, two nets merged, a pin left
        with nothing else on its net, a renamed net or a vanished named net refuses the call with
        connectivity_changed (nothing written; the changes are listed under connectivity). Pass
        allow_connectivity_change true when that is meant; the result then lists the changes. The pins of
        deleted symbols are not counted, only what their going does to the rest. dry_run true computes and
        returns everything and writes nothing.

        Dangling items: the result lists under dangling_after the wires with an end that touches
        nothing, the junctions where fewer than three wire ends or pins meet, and the labels that
        touch no wire and no pin, as far as this delete made them so (dangling_before_untouched counts
        those that were already there). Default is report only; remove_dangling true also removes the
        dangling wires and junctions this delete made (repeatedly, until none is left; listed under
        dangling_removed), never labels. The removals are part of the connectivity comparison.

        Refuses a failed self-check (write_not_verified, the sheet unchanged) and a project whose sheet
        tree cannot be read (sheet_tree_incomplete). root_sch names the project's root sheet when sch_path
        is a sub-sheet and its folder holds several projects; a sheet no sheet symbol uses yet is checked
        alone. Instance data of the symbols is never touched.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.

        Returns deleted (text, kind, uuid, at), count, dangling_after, dangling_removed, connectivity (nets_before,
        nets_after, changed, changes) and written.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.delete("label", sch_path, selectors, root_sch, remove_dangling=remove_dangling,
                                    allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def delete_junctions(
        sch_path: str, selectors: list[dict], allow_connectivity_change: bool = False,
        remove_dangling: bool = False, dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Delete junction dots from a sheet in one call.

        Selectors (`selectors`, a list of objects; the keys of one object must all match, the objects are
        joined): uuids, region [x1, y1, x2, y2], points ([[x, y], ...] exact positions), net (junctions on the wires of that net). A selector that selects nothing, or a listed uuid or point that does not exist on
        the sheet, refuses the whole call (selector_matched_nothing, not_found; nothing written).

        Connectivity: before anything is written, kicad-cli's netlist of the project is read from a scratch
        copy before and after the change and compared net by net. A split net, two nets merged, a pin left
        with nothing else on its net, a renamed net or a vanished named net refuses the call with
        connectivity_changed (nothing written; the changes are listed under connectivity). Pass
        allow_connectivity_change true when that is meant; the result then lists the changes. The pins of
        deleted symbols are not counted, only what their going does to the rest. dry_run true computes and
        returns everything and writes nothing.

        Dangling items: the result lists under dangling_after the wires with an end that touches
        nothing, the junctions where fewer than three wire ends or pins meet, and the labels that
        touch no wire and no pin, as far as this delete made them so (dangling_before_untouched counts
        those that were already there). Default is report only; remove_dangling true also removes the
        dangling wires and junctions this delete made (repeatedly, until none is left; listed under
        dangling_removed), never labels. The removals are part of the connectivity comparison.

        Refuses a failed self-check (write_not_verified, the sheet unchanged) and a project whose sheet
        tree cannot be read (sheet_tree_incomplete). root_sch names the project's root sheet when sch_path
        is a sub-sheet and its folder holds several projects; a sheet no sheet symbol uses yet is checked
        alone. Instance data of the symbols is never touched.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.

        Returns deleted (uuid, at), count, dangling_after, dangling_removed, connectivity (nets_before,
        nets_after, changed, changes) and written.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.delete("junction", sch_path, selectors, root_sch, remove_dangling=remove_dangling,
                                    allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def add_no_connects(
        sch_path: str, selectors: list[dict] | None = None, allow_connectivity_change: bool = False,
        dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Put no-connect markers on pins, by reference and pin, in one call.

        Selectors (`selectors`, a list of objects): references (symbol references, shell patterns allowed)
        and/or uuids, plus pins: a list of pin numbers (strings, as get_symbol_pins lists them) or
        "unconnected" (the default). With "unconnected" the marker goes on every open pin of the picked
        symbols. With no selectors at all, every placed symbol of this sheet is picked (child sheets are not
        followed). A reference, uuid or pin number that does not exist refuses the whole call (not_found).

        Open means: kicad-cli's netlist of the project lists the pin alone on an automatically named
        unconnected-(..) net. The netlist decides, not the geometry: a wire that runs over a pin without
        ending there joins nothing, so that pin is open. "unconnected" leaves out pins whose electrical type is
        no_connect and power symbols and flags (references that start with #). Hidden pins of one name are
        tied by KiCad into a shared net, so they sit in a net with other pins and are not listed open; a hidden
        pin on an open net would be marked. A pin named explicitly that is wired, labelled or tied to another pin (including a
        hidden pin of the same name) is refused as pin_connected, a hidden pin as pin_hidden, a pin of a power symbol as
        power_symbol_pin (nothing written).
        A pin that already has a marker on its point is skipped and listed under already_marked.

        The marker sits exactly on the pin point, computed from the raw (at ...) of the symbol and of the pin
        in the embedded library symbol (rotation, mirror and unit applied). Marked pins no longer show as
        pin_not_connected in ERC.

        Checks before anything is written: the netlist of the project, read from a scratch copy before and
        after, must show no change (a marker joins nothing; connectivity_changed otherwise, unless
        allow_connectivity_change), and kicad-cli's ERC of the project before and after must show exactly as
        many fewer pin_not_connected as pins were marked and no violation type that grew; otherwise the call is
        refused as write_not_verified and the sheet is unchanged. dry_run true computes and returns everything
        (would_add) and writes nothing. A sheet used more than once and an unannotated reference are refused
        as pin_not_in_netlist. root_sch names the project's root sheet when sch_path is a sub-sheet and its
        folder holds several projects. Instance data and every other node are never touched.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file it
        would write; close the file in KiCad, then retry.

        Returns added (reference, pin, name, type, at, uuid; two pins on one point share one marker and its
        uuid, the second entry has marker "shared with another pin on the same point"), count, already_marked,
        silenced_other_erc (count and pins of the types input and power_in: a marker also silences
        pin_not_driven or power_pin_not_driven for that pin), connected_pins_left_alone,
        connectivity (nets_before, nets_after, changed, changes) and written.
        """
        from kicad_mcp import no_connects

        return no_connects.add_no_connects(sch_path, selectors, root_sch, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def delete_no_connects(
        sch_path: str, selectors: list[dict], allow_connectivity_change: bool = False,
        dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Delete no-connect markers from a sheet in one call.

        Selectors (`selectors`, a list of objects; the keys of one object must all match, the objects are
        joined): uuids, region [x1, y1, x2, y2] (the marker's point inside it), points ([[x, y], ...] exact
        positions), or references plus pins (the markers sitting on those pin numbers of those symbols;
        shell patterns allowed in references, the two keys go together). A selector that selects nothing, a
        listed uuid, point, reference or pin that does not exist on the sheet, or a pin without a marker,
        refuses the whole call (selector_matched_nothing, not_found; nothing written).

        A marker joins nothing, so the netlist of the project does not change; it is still read from a
        scratch copy before and after and compared net by net (connectivity_changed unless
        allow_connectivity_change). The pins become open again and ERC reports them as pin_not_connected.
        dry_run true computes and returns everything (would_delete) and writes nothing.

        Refuses a failed self-check (write_not_verified, the sheet unchanged) and a project whose sheet tree
        cannot be read (sheet_tree_incomplete). root_sch names the project's root sheet when sch_path is a
        sub-sheet and its folder holds several projects. Instance data and every other node are never touched.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file it
        would write; close the file in KiCad, then retry.

        Returns deleted (uuid, at), count, dangling_after, connectivity (nets_before, nets_after, changed,
        changes) and written.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.delete("no_connect", sch_path, selectors, root_sch, remove_dangling=False,
                                    allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def move_symbols(
        sch_path: str, moves: list[dict], move_wires: bool = True, snap: bool = True, grid: float = 1.27,
        allow_connectivity_change: bool = False, dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Move placed symbols on a sheet in one call, absolute or relative.

        moves: a list of objects; each holds selector keys (all must match) and exactly one of `to`
        [x, y] (absolute position of the symbol's origin, mm; the selector must pick exactly one symbol
        node) or `offset` [dx, dy] (relative, mm, any number of symbols). Selector keys: references, uuids,
        pattern (shell pattern on the reference), region [x1, y1, x2, y2] (the symbol's origin inside it),
        lib_id. A reference the sheet does not have, or a selector that selects nothing, refuses the call
        (not_found, selector_matched_nothing). A symbol may be picked by one move only.

        move_wires (default true): the wire ends, junctions, labels and no-connect markers that sit
        exactly on a moved symbol's pin move with it by the same offset, so the wiring follows. A wire
        whose other end stays becomes slanted when the move is not along it: the result lists those
        wires under slanted, and every dragged item with its old and new position under followed. A
        mirrored symbol ((mirror x|y), set by add_symbols) is moved like any other: its pins are read with
        rotation and mirror applied, so the wires on them follow; the mirror itself is kept. With
        move_wires false nothing attached moves (and the connectivity comparison below will usually
        refuse the call).

        snap (default true) and grid (default 1.27 mm): the new position of each symbol's origin is
        rounded to the grid; moved lists what was asked and what it became (snapped), and snapped lists
        the uuids that changed by it. The symbol's property texts move by the same offset; instance data
        (references and unit paths, on the root sheet or on a sheet a parent sheet uses) is not touched.

        Returns moved: per symbol, reference, uuid, from, to, asked, snapped and pins (number, name, x, y:
        the pin positions on the sheet after the move, read from the raw symbol and library geometry, so a
        wire can be drawn to them), followed, slanted, connectivity and written.

        Connectivity: before anything is written, kicad-cli's netlist of the project is read from a
        scratch copy before and after the change and compared net by net. A split net, two nets merged, a
        pin left alone on its net, a renamed net or a vanished named net refuses the call with
        connectivity_changed (nothing written; the changes are listed under connectivity). Pass
        allow_connectivity_change true when that is meant. dry_run true computes and returns everything and
        writes nothing.

        Refuses a failed self-check (write_not_verified, the sheet unchanged), two moved items at one wire
        end that move differently (ambiguous_drag) and a project with a missing, unparseable or cyclic
        sheet (sheet_tree_incomplete). root_sch names the project's root sheet when sch_path is a sub-sheet and
        its folder holds several projects.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.move("symbol", sch_path, moves, root_sch, move_wires=move_wires, snap=snap, grid=grid,
                                  allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def move_labels(
        sch_path: str, moves: list[dict], move_wires: bool = True, snap: bool = True, grid: float = 1.27,
        allow_connectivity_change: bool = False, dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Move net labels, global labels and hierarchical labels on a sheet in one call, absolute or relative.

        moves: a list of objects; each holds selector keys (all must match) and one of `to` [x, y]
        (absolute, mm; the selector must pick exactly one label) or `offset` [dx, dy] (relative, mm), and
        optionally `angle` (0, 90, 180 or 270, the label's text direction). Selector keys: texts (exact
        label texts), uuids, pattern (shell pattern on the text), region [x1, y1, x2, y2], kind (label,
        global or hier). A label may be picked by one move only.

        move_wires (default true): wire ends, junctions and no-connect markers that sit exactly on the
        label's old position move with it, as for move_symbols (followed and slanted in the result). snap
        (default true) and grid (default 1.27 mm) round the new position; moved lists what was asked and
        what it became.

        A label moved off its wire changes the net's name: the connectivity comparison (netlist before and
        after on a scratch copy, as in move_symbols) refuses that with connectivity_changed unless
        allow_connectivity_change is true. dry_run true computes and returns everything and writes nothing.
        Returns moved (text, uuid, from, to, asked, snapped), followed, slanted, connectivity and written.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.move("label", sch_path, moves, root_sch, move_wires=move_wires, snap=snap, grid=grid,
                                  allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def set_text(
        sch_path: str, items: list[dict], allow_connectivity_change: bool = False, dry_run: bool = False,
        root_sch: str | None = None,
    ) -> dict:
        """Change the text of existing net labels, global labels, hierarchical labels and free text objects
        on a sheet in one call. The object keeps its uuid, position, rotation, shape and effects; only the
        text changes.

        items: a list of objects {uuid | kind + old_text [+ point], text}. `text` is the new text (never
        empty). The object is picked by `uuid`, or by `kind` (label, global, hier, text) and `old_text`
        (its current text, exact); `point` [x, y] (mm) tells apart several objects of the same kind and
        text. More than one match is refused (ambiguous, candidates listed with uuid and position), none is
        refused (not_found), and so is an object picked twice, an unknown key or kind, a text that is not a
        string or holds a control character, and a line break or tab in a label, however the label is picked
        (invalid_spec; empty_text for an empty or blank text). Free text may hold several lines; a label is one
        line. Leading or trailing whitespace in a label is written and listed as a label_whitespace warning.

        Renaming a net label (local, global or hierarchical) changes connectivity: kicad-cli's netlist of
        the project is read before and after on scratch copies and compared, and every renamed label is
        also listed as label_renamed. The call is refused with connectivity_changed (nothing written, the
        changes listed) unless allow_connectivity_change is true. A change of free text only never changes
        connectivity, so no netlist is read for it. A text set to what it already is is listed as unchanged.

        A renamed hierarchical label leaves the sheet pin of the parent sheet's sheet symbol alone. When
        the project has a parent that uses this file and its pin still carries the old name, the result
        lists a warnings entry pin_name_mismatch (parent_sheet, sheet_name, pin, label): rename the pin
        with remove_sheet_pins and add_sheet_pins, or ERC reports the two as unmatched. A parent without a pin of
        the old name, or with a pin of the new name, gives no warning.

        Returns changed (uuid, kind, from, to, at), count, unchanged, connectivity (when a label was
        renamed), warnings and written. dry_run true computes and returns everything (would_change) and
        writes nothing. Every other node of the sheet is checked against the original, and the written file
        is reloaded and compared before it replaces the original. Refuses a failed self-check
        (write_not_verified, the sheet unchanged). root_sch names the project's root sheet when sch_path is a
        sub-sheet and its folder holds several projects.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.set_text(sch_path, items, root_sch, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def set_title_block(sch_path: str, items: list[dict], dry_run: bool = False, root_sch: str | None = None) -> dict:
        """Set the title block of one or several sheets of a schematic project in one call: title, date,
        revision, company and the comments 1 to 9, with line breaks in any of them.

        items: a list of objects {sheet, title, date, rev, company, comments}. sheet is a sheet of the
        project (its path from the root sheet's folder, or an absolute path; left out it is sch_path
        itself); two items may not name one sheet. Every other key is optional and only the keys given
        are changed. A text of null or "" removes the field (KiCad treats an empty field and none as the
        same, and drops an empty one when it saves). comments is {"1": text, ..., "9": text}; KiCad
        cannot load a comment 0 or a comment above 9, so those are refused (invalid_spec).

        Line breaks: write \\n in a text for a line break; the drawing sheet prints a second line. KiCad
        stores it as the two characters backslash and n, which is what is written, so KiCad's own next
        save of the sheet keeps it. CRLF is written as one line break and listed (line_break_normalised);
        a lone CR, any other control character, a text that is not a string and a text longer than 4000
        characters are refused, and so is a whole call with one bad item (nothing written). Leading or
        trailing white space or a line break at the ends is written and listed (title_block_edge_whitespace).
        A title block with a duplicated field, a second title_block or a child this tool does not know is
        refused (title_block_unusual) instead of guessed at; a sheet with no title block gets one.

        Nothing but the title block changes: every other node of each sheet is compared with the original,
        kicad-cli's netlist of the project is read after the change and must show exactly the asked text on
        each edited sheet (a line break comes back as a line break) with the same nets as before, and the
        ERC violation counts of the project must be the same as before, or the call is refused with
        write_not_verified (nothing written). Each written file is reloaded and compared before it replaces
        the original; if one sheet fails, the sheets already written are restored. A title block has no
        position, so the 1.27 mm grid does not apply and nothing on the sheet moves. The sheet's text
        variables such as ${TITLE} are not expanded here: the text is stored as given.

        Returns sheets (per sheet: changed with field, from, to and action added, changed or removed;
        unchanged fields), count, connectivity (nets before and after), erc (violation counts before and
        after), warnings, note and written. dry_run true computes and returns everything and writes nothing.
        A call that asks for what is already there writes nothing. root_sch names the project's root sheet
        when sch_path is a sub-sheet and its folder holds several projects.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import title_block

        return title_block.set_title_block(sch_path, items, root_sch, dry_run=dry_run)

    def add_text(
        sch_path: str, items: list[dict], snap: bool = True, grid: float = 1.27, dry_run: bool = False,
        root_sch: str | None = None,
    ) -> dict:
        """Create free text objects (notes, pin annotations such as "U501.24 VDD") on a sheet in one call.
        A free text is not a label: it names no net and joins nothing, so use it for annotations and never
        add_labels. The new text can be changed later with set_text through the uuid this call returns.

        items: a list of objects {x, y, text [, rotation, size, justify]}. x and y are in mm. `text` is one
        line: a line break, a tab or a Unicode line separator is refused (invalid_spec), an empty or blank
        text too (empty_text); leading or trailing whitespace is written and listed as a text_whitespace
        warning. `rotation` is 0 (default) or 90 (KiCad reads 180 and 270 as 0 and 90, so they are refused). `size` is the text height and width in mm
        (default 1.27, from 0.1 to 50). `justify` is words such as "left bottom" (left or right, top or
        bottom, mirror; none by default). Two identical items in one call are refused. One bad item refuses
        the whole call and nothing is written.

        snap (default true) and grid (default 1.27 mm) round the position; each added entry lists asked and
        snapped. With "snap": false the position is written as asked (rounded to 0.0001 mm).

        Checks before anything is written: kicad-cli's netlist of the project, read from a scratch copy
        before and after, must show no change (connectivity_changed otherwise; there is no override), and
        its ERC census of the project must be the same before and after, otherwise the call is refused
        (write_not_verified, the sheet unchanged). Every other node of the sheet is checked against the
        original, and the written file is reloaded and compared before it replaces the original. dry_run
        true computes and returns everything (would_add) and writes nothing. Works on a sheet that a parent
        sheet symbol uses (no instance data is touched). root_sch names the project's root sheet when
        sch_path is a sub-sheet and its folder holds several projects.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file it
        would write; close the file in KiCad, then retry.

        Returns added (uuid, text, at, rotation, size, justify, asked, snapped), count, grid, warnings,
        connectivity (nets_before, nets_after, changed, changes) and written.
        """
        from kicad_mcp import add_text as add_text_module

        return add_text_module.add_text(sch_path, items, root_sch, snap=snap, grid=grid, dry_run=dry_run)

    def set_property_positions(
        sch_path: str, items: list[dict], dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Set the position, angle and justification of the property texts (Reference, Value, Footprint,
        Datasheet, any other field) of placed symbols in one call, for example to move texts off bodies
        and pins.

        items: a list of objects; each holds symbol selector keys (references, uuids, pattern, region
        [x1, y1, x2, y2], lib_id; all must match) and `properties`: {property name: change}. A change is an
        object with `offset` [dx, dy] (mm from the symbol's origin; works for any number of symbols, so one
        item can place the Reference of every R*) or `at` [x, y] (absolute, exactly one symbol), and
        optionally `angle` (0, 90, 180 or 270, as KiCad stores it for the text) and `justify` (words from
        left, right, top, bottom, mirror, for example "left bottom"; "" removes it). A property the symbol
        does not have is refused (property_not_found).

        Returns updated: per symbol and property, the text's at, angle and justify, the symbol's at and its
        pins (number, name, x, y), so the texts can be put clear of them. The flag fields_autoplaced is
        removed from each edited symbol, because its texts are no longer where KiCad's auto-placement put
        them. No net can change, so no netlist comparison is made; the staged check (nothing but the
        selected symbols differs) and the reload check of the write apply. dry_run true returns what would
        be set and writes nothing.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.set_property_positions(sch_path, items, root_sch, dry_run=dry_run)

    def align_items(
        sch_path: str, groups: list[dict], box: str = "pins", move_wires: bool = True, snap: bool = True, grid: float = 1.27,
        allow_connectivity_change: bool = False, dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Align the symbols (or the labels) of a sheet to a common edge or centre line, in one call.

        groups: a list of objects, each {kind, references | uuids | texts, edge, reference}. kind is
        "symbol" or "label" (one kind per call). The list names the items one by one: references (symbols),
        texts (labels) or uuids; every entry must pick exactly one node (use uuids for several units of
        one reference or for equal label texts), an entry the sheet does not have refuses the call
        (not_found), and an item may be in one group only. edge: left, right, hcenter, top, bottom or
        vcenter (top is the smaller y). reference (default "first"): "first" or "last" (in the order of the
        list you gave), "min" or "max" (the smallest or largest edge coordinate), or a number, the
        coordinate in mm to align to. At least two items per group.

        Boxes: a symbol's box is computed from the raw geometry of its library symbol in the sheet file
        and the raw (at ...) and mirror of the placed symbol: it is the symbol's graphics of the placed unit
        and body style plus its pin connection points (and the pin lines), for the pins that are not hidden.
        Texts and text boxes inside the library symbol, pin name and number texts, the stroke width and the
        property texts are not counted; a bezier curve counts by its control points, so the box can be
        slightly too large. box accepts "pins" only (any other value is invalid_spec). A label is
        its anchor point (its connection point), because its text extent is not in the file. Numbers
        (reference, spacing, grid) must be finite and at most 1e6 mm, else invalid_spec.

        The property texts of a moved symbol move with it. move_wires (default true): wire ends, junctions,
        labels and no-connect markers that sit exactly on a moved pin (or label) follow by the same offset;
        wires left slanted are listed under slanted, every dragged item under followed. snap (default true)
        and grid (default 1.27 mm): the new origin of each moved item is rounded to the grid on the moved
        axis, so items of different size may not match the target exactly; every item lists its residual
        (edge after minus target) and every group its max_residual and aligned, and moved lists what was
        asked and what it became (snapped).

        Connectivity: before anything is written, kicad-cli's netlist of the project is read from a scratch
        copy before and after the change and compared net by net; a split or merged net, a pin left alone
        on its net, a renamed or vanished named net refuses the call with connectivity_changed (nothing
        written; the changes are listed) unless allow_connectivity_change is true. dry_run true computes and
        returns everything (would_move) and writes nothing. Instance data is not touched.

        Refuses a failed self-check (write_not_verified, the sheet unchanged), two moved items at one wire
        end that move differently (ambiguous_drag) and a project whose sheet tree cannot be read
        (sheet_tree_incomplete). root_sch names the project's root sheet when sch_path is a sub-sheet.
        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.

        Returns groups (per group: edge, reference, target, items with edge_before, edge_after, residual,
        moved, snapped; max_residual; aligned), moved, followed, slanted, connectivity and written. Wires and
        junctions as items to align are not supported.
        """
        from kicad_mcp import sheet_align

        return sheet_align.align(sch_path, groups, root_sch, box=box, move_wires=move_wires, snap=snap, grid=grid,
                                 allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def distribute_items(
        sch_path: str, groups: list[dict], box: str = "pins", move_wires: bool = True, snap: bool = True, grid: float = 1.27,
        allow_connectivity_change: bool = False, dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Space the symbols (or the labels) of a sheet evenly along x or y, in one call.

        groups: a list of objects, each {kind, references | uuids | texts, axis, spacing}. kind "symbol" or
        "label" (one kind per call); the list names the items one by one exactly as for align_items (each
        entry picks exactly one node; not_found refuses). At least three items per group. axis: "x" or "y".
        The items are ordered by their centre along the axis. spacing (default "gaps"): "gaps" keeps the
        first and the last item and makes the gaps between the boxes equal; "centres" keeps them and makes
        the centre-to-centre distances equal; a positive number is a pitch in mm: the first item stays and
        the k-th is put at first centre + k * pitch (for example ten branches on a pitch of 17.78).

        Boxes, property texts, move_wires, snap and grid, the connectivity comparison, dry_run, root_sch and
        the refusals are as for align_items (box "pins" only; a label is its anchor point). Because
        the new origins are rounded to the grid, the spacing may be off by less than half a grid step: the
        group lists gaps_after, centre_pitches_after, nominal, max_residual and even, and overlapping_after
        names neighbours whose boxes overlap after the move (reported, not refused).

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.

        Returns groups (order, items with centre_before, centre_after, moved, snapped), moved, followed,
        slanted, connectivity and written. Wires and junctions as items are not supported.
        """
        from kicad_mcp import sheet_align

        return sheet_align.distribute(sch_path, groups, root_sch, box=box, move_wires=move_wires, snap=snap, grid=grid,
                                      allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def replace_symbols(
        sch_path: str, replacements: list[dict], allow_connectivity_change: bool = False,
        dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Replace placed symbols by another library symbol in one call, for example a placeholder by the
        final part, keeping what the sheet already has: the uuid, the reference, the position, the
        rotation, the mirror, the unit, the property texts and the instance data stay as they are.

        replacements: a list of objects {reference | uuid, lib_id, pin_map, library_fields}. reference (exact, one placed
        symbol; use uuid when several share a reference such as "R?") or uuid picks the symbol; lib_id is
        the new Library:Symbol, read from the project's symbol library table or KiCad's own folder (or
        reused when the sheet already embeds it); pin_map is optional: {old pin number: new pin number}
        for pins whose numbers differ. Without a pin_map pins are paired by pin number (by pin name for a
        pin that has no number). library_fields (default false, issue 162): true also takes over every field
        of the new library symbol like KiCad's Change Symbol with fields updated (Footprint, Datasheet,
        Description, LCSC and any custom field, with the library's hidden state; Reference and Value are
        never touched, a field only the old symbol has stays); the default keeps the symbol's own field
        texts, so a Footprint you assigned survives. The netlist after the change must carry the adopted
        fields, else the call is refused as a failed write check and the sheet stays unchanged; library_fields_taken in the result lists them per symbol. A part with several units placed on the sheet is refused
        (multi_unit_symbol); so is a unit the new symbol does not have (unit_missing).

        The embedded library symbol is rewritten: the new one is added, and the old one is dropped only
        when no other symbol on the sheet still uses it (kept_library_symbols says which stayed).

        Wires are never moved. A wired pin of the old symbol with no counterpart on the new one refuses
        the call as pin_unmapped (nothing written, the pins listed) unless allow_connectivity_change is
        true. A wired pin whose counterpart lies at another point, and a new pin that lands on a wire
        end, label, junction or other pin, refuse it as connectivity_changed with the old and new
        positions in the list. Before anything is written, kicad-cli's netlist of the project is read
        from a scratch copy before and after the change and compared net by net; a split net, two nets
        merged, a pin left alone on its net, a renamed net or a vanished named net refuses it as
        connectivity_changed too. Pass allow_connectivity_change true only when that is meant. dry_run
        true computes and returns everything and writes nothing.

        Returns replaced: per symbol, reference, uuid, old_lib_id, lib_id, library_symbol (added or
        reused), at, rotation, mirror, unit, pin_map (old and new pin, was and now positions, wired,
        moved), unmapped_old_pins and pins (the new pins: number, name, type, x, y, read from the raw
        symbol and library geometry, so a wire can be drawn to them), plus dropped_library_symbols,
        kept_library_symbols, connectivity and written. With dry_run the list is called would_replace.

        Refuses a failed self-check (write_not_verified, the sheet unchanged) and a project with a
        missing, unparseable or cyclic sheet (sheet_tree_incomplete). root_sch names the project's root
        sheet when sch_path is a sub-sheet and its folder holds several projects.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.replace_symbols(sch_path, replacements, root_sch, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    def set_properties(
        sch_path: str, items: list[dict], allow_connectivity_change: bool = False, dry_run: bool = False,
        root_sch: str | None = None, standalone: bool = False,
    ) -> dict:
        """Set, add or remove the properties (fields: LCSC, MPN, Datasheet, Description, Value, Footprint, any
        custom name) of placed symbols in one call, for example to put the LCSC number on every new instance.

        items: a list of objects, each `{reference | uuid, set: {name: value | {value, hidden}}, remove:
        [names]}`; at least one of set and remove. Exactly one of reference (exact text) and uuid picks the
        symbol. The units of a multi-unit part (same reference and lib_id, different unit numbers) are
        edited together, whichever unit's uuid or the reference is given, because KiCad reads the fields of a
        part from one unit's copy only; every touched symbol is listed with its uuid and unit. Several symbols
        that share a reference but are not units of one part are refused with ambiguous_reference, naming
        their uuids, so pick one by uuid. A part with units on more than one sheet of the project is refused
        (part_spans_sheets, naming the other sheets and units): an edit of one sheet cannot keep the units in
        step. Units on other sheets are seen only through the project tree, so the sheet must be tied to one
        readable project that holds it: root_sch (the root sheet of the project) or, without it, the one project
        in the folder of the sheet or up to 12 folders above it whose sheet tree holds it. Otherwise (no project
        found, several holding it, a project that does not read whole, a project root sheet without a .kicad_pro
        file, a project more than 12 levels up, a sheet no sheet symbol uses yet) the call is refused with
        root_not_found and writes nothing. standalone true (not with root_sch) is the explicit way out: the sheet
        is then edited without looking for units on other sheets, and the result says so (part_checked_standalone
        true and a note); it changes nothing when one project holds the sheet. What it costs: KiCad may keep
        reading the copy of the other unit, so the netlist may not follow the edit, and the call is then refused
        (write_not_verified, the sheet unchanged); it risks a refusal, not a silent edit that has no effect. A value is a
        string (numbers are not converted). A property is only a field: one named "dnp" or "in_bom" becomes a netlist field and does not set the real
        flag of the symbol. Setting a property the symbol has changes only its value text
        (and its hidden flag when `hidden` is given); its position, font and place in the symbol stay as they
        are. Setting a property the symbol lacks adds it after the last one, at the symbol's own (at) with
        angle 0, hidden unless `hidden` is false; a shown new field sits on the symbol's origin, so move it
        with set_property_positions. A property set to the value it already has is reported `unchanged`; when
        nothing changes the file is not written.

        Not allowed: setting the Reference (refused, reference_not_settable: fix_annotation owns it) and
        removing the Reference, the Value or the uuid (property_not_removable; set a Value instead). Removing
        a property no unit of the symbol has is property_not_found. Removing the Footprint is allowed, and the
        result carries a note: the netlist then holds an empty footprint. Other refusals: not_found (a
        reference or uuid not on this sheet), invalid_spec (including a property name with leading or
        trailing white space). A refusal writes nothing.

        Properties only change text, but the Value of a power symbol names its net: the netlist of the project
        is compared before and after on scratch copies, and a changed net is refused with
        connectivity_changed unless allow_connectivity_change is true. A staged check confirms that
        every top-level node the call did not select is unchanged; the instance data (reference, unit, project
        path) of a selected symbol is never touched. The netlist of the edited project (kicad-cli, scratch copy) must carry every field
        asked for, found by the reference of the instance data of each sheet use (the one KiCad uses, which may
        differ from the Reference text): Value as the component value, the other fields by name, a removed
        standard field as empty and a removed custom field as absent; power symbols and off-board symbols are not
        in the netlist and are not compared. Any mismatch, or a reference that is not exactly one component, is a
        refusal (write_not_verified, the sheet unchanged) that names the reference, the field, the asked value and
        the netlist value. The written file is read back and compared (write_not_verified, the sheet unchanged).

        dry_run true computes and returns everything (would_update) and writes nothing. Returns updated: per
        symbol (a multi-unit part: per unit) its reference, uuid, unit and per property name, action (added, changed, unchanged, removed, absent), value,
        previous and hidden; count, connectivity and written.

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.set_properties(sch_path, items, root_sch, allow_connectivity_change=allow_connectivity_change, dry_run=dry_run,
                                            standalone=standalone)

    @_read_only
    def get_symbol_pins(sch_path: str, references: list[str] | None = None, uuids: list[str] | None = None) -> dict:
        """Read the pins of placed symbols: absolute sheet coordinates, name and electrical type, for
        wiring by pin instead of by hand-computed offsets. Read-only; writes nothing.

        references: a list of reference strings (shell patterns allowed, "*" for every placed symbol on
        this sheet; child sheets are not followed). uuids: a list of symbol uuids, which picks exactly
        one placed symbol even when several share a reference such as "R?". Give at least one of the two;
        the selectors are ORed. A reference, pattern or uuid that matches nothing refuses the whole call
        (not_found, listed).

        Returns symbols, one entry per placed symbol (a multi-unit part such as an op-amp gives one entry
        per unit placed, each with its `unit`): reference (the sheet's own Reference property; on a sheet
        used more than once it is not the per-use reference), uuid, lib_id, unit, at [x, y], rotation, mirror
        and pins, a list of {number, name, type, x, y}. x and y are where a wire end or label must sit to
        connect to that pin (mm, sheet coordinates, Y down), computed from the raw (at ...) of the symbol
        and of the pin in the library symbol embedded in the sheet, with the symbol's rotation and
        mirror applied (rotate, then mirror); the maths is covered by tests against kicad-cli's netlist. type is
        the pin's electrical type from the library (passive, input, output, power_in, ...). A symbol whose
        library symbol is missing from the sheet comes back with no pins and a warning. A pin sits on its
        listed point whether or not that point is on the 1.27 mm grid: pass snap false to add_wires and
        add_labels when using it.
        """
        from kicad_mcp import sheet_editing

        return sheet_editing.get_symbol_pins(sch_path, references, uuids)

    def connect_pins(
        sch_path: str, connections: list[dict], snap: bool = True, grid: float = 1.27,
        allow_connectivity_change: bool = False, dry_run: bool = False, root_sch: str | None = None,
    ) -> dict:
        """Wire pins to pins in one call: say which pins, not where they are.

        connections: a list of objects {from, to, via?, net_label?}. `from` and `to` each name one placed pin:
        "R1.2" (reference.pin) or {ref | uuid, pin, unit?}. pin is the pin number, or its name when that is unique
        on the symbol; unit picks the placed unit of a multi-unit part (needed when the pin exists in several
        placed units, else pin_ambiguous); uuid picks one symbol when several share a reference. The tool finds
        each pin point from the raw symbol and library geometry (rotation, mirror and unit applied, as
        get_symbol_pins reports them) and draws an orthogonal path of wire segments: straight, an L, a Z (dogleg)
        or a U (both pins face the same way), the first one that leaves each pin along its own direction (away
        from the symbol body) and touches nothing. `via`: a list of [x, y] waypoints to choose the path yourself
        (every leg horizontal or vertical); then the pin directions are not checked, only what the path touches.
        `net_label`: a net label text placed on the longest segment of the new wire.

        The grid: snap (default true) and grid (default 1.27 mm). A pin point is never moved: it sits where the
        library puts it, on the grid or not, and the wire ends exactly on it. Only the points the tool chooses
        itself are rounded to the grid (the dogleg of a Z, the line of a U, via waypoints, the label spot; a
        waypoint coordinate that equals a pin's is that pin's exact coordinate). Per connection the result lists
        `snapped` ({axis, asked, now}) and `off_grid_endpoints` (pin points off the grid, kept exact). With snap
        false nothing the tool chooses is rounded.

        What is checked: the path must not pass over another pin (the result names it), a label, junction,
        no-connect marker or sheet pin, nor touch a wire (end on route, corner on wire, collinear overlap), also
        one drawn earlier in the same call; it may cross a wire without touching it (listed as crosses_wires).
        Another route of the same pair is tried first; when none is clear the call is refused (no_clear_route,
        blockers listed; no_route when the pin directions allow none; via_not_orthogonal). Symbol bodies are not
        checked: render the sheet. Two parts side by side whose pins face along the same axis the wrong
        way round (for example R1.2 leaving down and R2.1 leaving up, R2 beside R1 and level with it)
        may give no_route or no_clear_route: choose the path with `via` waypoints. A wire end that lands on the middle of another wire gets the junction it needs
        (junctions_added). A pair already on one net (in the project's netlist, or by an earlier
        connection of the same call) is not drawn again (already_connected).

        Connectivity: kicad-cli's netlist of the project is read before and after on scratch copies. Every
        requested pair must end on one net in the edited sheet's netlist; if not, nothing is written and the
        refusal names the pairs. A net that
        merges with another that no requested pair asked for, or two differently named nets joined (even when
        asked for), refuses the call with connectivity_changed (nothing written, the changes listed); pass
        allow_connectivity_change true when that is meant. dry_run true plans, checks and returns everything
        (would_connect) and writes nothing. junction_candidates lists points where three or more wire ends or
        pins meet without a junction (add_junctions draws the dot).

        Refuses unannotated symbols (unannotated_symbol, run fix_annotation first), a pin or symbol that does not
        exist (not_found, pin_not_found, with the pins listed), a pin the netlist does not list (pin_not_in_netlist),
        a failed self-check (the sheet unchanged) and a project with a missing, unparseable or
        cyclic sheet (sheet_tree_incomplete). root_sch names the project's root sheet when sch_path is a sub-sheet
        and its folder holds several projects. A sheet that a parent uses more than once is not supported (its
        per-use references differ from the sheet's own).

        Refuses with file_open_in_kicad, naming the lock file, while KiCad holds a lock file for a file
        it would write; close the file in KiCad, then retry.
        """
        return pin_wiring.connect_pins(sch_path, connections, root_sch, snap=snap, grid=grid,
                                       allow_connectivity_change=allow_connectivity_change, dry_run=dry_run)

    @_read_only
    def check_symbol_fields(sch_path: str, required_fields: list[str] | None = None, root_sch: str | None = None) -> dict:
        """Read-only, in one call over the whole sheet tree: list every placed symbol that has a gap in a
        required field, per sheet and by reference and uuid. Neither check_connectivity nor ERC reports an empty
        Footprint, a missing LCSC or a Datasheet of "~", so a wired schematic can still give an
        incomplete BOM. It writes nothing, makes no scratch copy and takes no write lock.

        required_fields: the field names that must hold a real value (exact, case-sensitive names); omitted,
        Footprint, LCSC and Datasheet. sch_path: any sheet of the project; the check always starts at the
        project's root sheet (root_sch, optional when sch_path is the root or the only .kicad_pro of its
        folder names it) and follows the sheet symbols, so every sheet of the tree is read, each file once.

        A field is a gap when the symbol has no such property (problem missing), or its value is empty (empty),
        only white space (blank) or "~" (placeholder). A missing property that exists under a name that differs
        only in case (for example "Lcsc") lists that name under case_variant. The result: sheets, one entry per
        sheet file (checked, symbols_with_gaps and symbols: reference, uuid, unit, lib_id, uses, gaps, each
        {field, problem, value}); a sheet file used twice is listed once and reports the reference of its first
        use; a multi-unit part is checked per placed unit, as each unit carries its own fields. Totals:
        placed_symbols, checked, symbols_with_gaps, gap_count and no_gaps (true when no symbol has a gap). Nothing is truncated: every
        symbol with a gap is listed.

        Not checked, and listed apart under skipped with a count each (the first matching reason): power
        (a power symbol, or a reference starting with #), off_board (marked not on board) and dnp (marked do
        not populate), plus no_lib_id (a symbol node without a library id). placed_symbols = checked + all skipped. To repair a gap use set_properties; no_gaps is not a verdict on the board.
        """
        from kicad_mcp import field_check

        return field_check.check_symbol_fields(sch_path, required_fields, root_sch)

    @_read_only
    def get_properties(
        sch_path: str, references: list[str] | None = None, uuids: list[str] | None = None,
        properties: list[str] | None = None, root_sch: str | None = None,
    ) -> dict:
        """Read-only, in one call over the whole sheet tree: the values of chosen properties (fields) of the selected
        placed symbols, or of all of them, per sheet file by reference and uuid. It is the read companion of
        set_properties and the way to check a whole design (for example the LCSC number, Footprint, Datasheet,
        Description and Value of every part) in one call. It writes nothing, makes no scratch copy and takes no
        write lock; the values are read from the raw property nodes of each sheet file.

        properties: the names to read (exact, case-sensitive); omitted, Value, Footprint, Datasheet, Description
        and LCSC. references: exact references to select; uuids: exact symbol uuids to select; both given, a symbol
        matching either is returned; neither given, every placed symbol is returned. sch_path: any sheet of the
        project; the read always starts at the project's root sheet (root_sch, optional when sch_path is the root
        or the only .kicad_pro of its folder names it) and follows the sheet symbols, so every sheet of the tree is
        read, each file once. A reference is matched against the instance reference of the project (what the
        netlist carries) and the Reference text.

        The result: sheets, one entry per sheet file, each with symbols: reference, uuid, unit, lib_id, uses,
        properties ({name: value} of the requested names the symbol has; an empty value is present, as "") and
        absent (the requested names the symbol has no property of, each {name} with case_variant when it exists
        under a name that differs only in case, for example "Lcsc"), plus flags (power, off_board, dnp,
        no_lib_id: those symbols are returned too, not skipped) and, for a sheet file used more than once,
        use_references. A multi-unit part is returned per placed unit, as each unit carries its own copy of the
        fields; KiCad's netlist reads one unit's copy. Totals: placed_symbols, returned, absent_count, and
        not_found (the references and uuids that matched no symbol) with all_found. Nothing is truncated.

        A selection list must be non-empty and hold distinct strings, and a property name must have no white
        space at the ends (invalid_spec). Other refusals: file_not_found, parent_not_in_tree, root_not_found, and a
        missing, unparseable or cyclic sheet (sheet_tree_incomplete). To change a property use set_properties; to
        list the symbols whose required fields are empty use check_symbol_fields.
        """
        from kicad_mcp import field_check

        return field_check.get_properties(sch_path, references, uuids, properties, root_sch)

    @_read_only
    def check_connectivity(sch_path: str) -> dict:
        """Check for unconnected pins and connection issues -- a narrower,
        faster check than full ERC (`sch_run_erc` in the `verification`
        toolset runs KiCad's ERC). Useful right after wiring a schematic,
        before running full ERC. Neither check is a verdict: a clean result
        here or from ERC says nothing about the board, and only verify_board
        gives one.

        Reads every sheet's raw nodes. unconnected_pins lists pins with nothing on them: no wire end, other
        pin, label or sheet pin, and no junction with a wire passing over it; a wire that only runs over a pin joins nothing, so that pin is listed.
        An invisible power_in pin of an ordinary symbol (KiCad joins it to the net of its pin name without a wire)
        is not listed when that name has another member on the same sheet: another such pin (wired or not), an attached power
        symbol of that value or a label of that text on that sheet; a lone one is listed, as ERC does.
        A pin with a no-connect marker on it (or an electrical type no_connect) is not a finding and is listed
        apart in no_connect_pins (via says which). connection_issues has possible_floating_wire for a wire end
        that touches no pin, sheet pin, label, junction, no-connect marker or second wire end; its description
        says whether the end lies on the middle of another wire (a junction is missing there). Power symbols
        (library symbol flagged power, whatever the library's name) are never in missing_from_netlist.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        try:
            from kicad_tools.cli.sch_find_unconnected import analyze_schematic
            from kicad_tools.schema import Schematic

            # analyze_schematic's netlist-export cross-check shells out to
            # kicad-cli internally (same Windows-discovery gap as run_erc and
            # run_fix_annotation -- this is the fifth site in this project).
            # Without this the cross-check silently degrades. Found 2026-09-04
            # while tracking down why a full test run kept rewriting
            # tests/fixtures/*-netlist.kicad_net: that export is this call's
            # side effect, written next to the *input* schematic.
            ensure_kicad_cli_on_path()
            from kicad_tools.cli.sch_set_footprint import _collect_schematic_files

            from kicad_mcp import sheet_tree

            tree = sheet_tree.walk(path)
            if (refused := _sheet_tree_refusal(tree, kicad_tools_writes=False)) is not None:
                return refused
            schematic = Schematic.load(path)
            unconnected, issues, missing_from_netlist = analyze_schematic(schematic, root_path=path)
            # kicad_tools walks the tree itself but follows only "Sheetfile": a sheet reached
            # through KiCad 6's "Sheet file" is analysed here, sheet by sheet (build ticket 26).
            walked = {p.resolve() for p in _collect_schematic_files(path)}
            if walked - set(tree.files):
                return _incomplete(
                    "kicad_tools read a sheet the root does not reference: "
                    + ", ".join(sorted(str(p) for p in walked - set(tree.files))),
                    **tree.report(),
                )
            analysed = [tree.rel(f) for f in tree.files if f in walked]
            for sheet in tree.files:
                if sheet in walked:
                    continue
                analysed.append(tree.rel(sheet))
                sub_unconnected, sub_issues, _ = analyze_schematic(Schematic.load(sheet), root_path=None)
                unconnected += [dataclasses.replace(u, sheet=sheet.name) for u in sub_unconnected]
                issues += [
                    dataclasses.replace(i, sheet=sheet.name) if hasattr(i, "sheet") else i for i in sub_issues
                ]
            # kicad_tools' wire graph knows nothing of no-connect markers, direct pin-to-pin touches,
            # wire ends on pins and project-library power symbols (issue 109): re-read each sheet raw.
            unconnected, no_connect_pins, issues, missing_from_netlist = _refine_connectivity(
                tree, path, unconnected, issues, missing_from_netlist
            )
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"connectivity check could not complete: {exc}")
        return {
            **tree.report(),
            "analysed_sheets": analysed,
            "status": "ok",
            "unconnected_pins": _to_jsonable(unconnected),
            "unconnected_count": len(unconnected),
            "no_connect_pins": no_connect_pins,
            "no_connect_count": len(no_connect_pins),
            "connection_issues": _to_jsonable(issues),
            "missing_from_netlist": _to_jsonable(missing_from_netlist),
        }

    # add_symbols/add_wires/add_junctions/add_labels/assign_footprints/
    # fix_annotation all mutate the schematic file (.write()/run_*
    # rewrite it) -- destructive in the sense of overwriting prior file
    # content (assign_footprints and fix_annotation copy every sheet to
    # backup_dir under the system temp directory first, outside the project), not
    # idempotent (re-adding the same symbols/wires would duplicate them),
    # and purely local (no network/external-entity interaction).
    _MUTATING_LOCAL = {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": False,
    }
    _READ_ONLY_LOCAL = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": False,
    }
    # Every function defined in `build` is a tool (issue 147): collected here, so a new tool is one new
    # `def` and is not added to any list. `@_read_only` marks the read-only ones; the others are writing
    # tools and need a write-guard case (tests/tool_cases/) and a ratchet entry (tests/ratchets.d/).
    tools = [
        tool_fn for tool_fn in locals().values()
        if inspect.isfunction(tool_fn) and tool_fn.__qualname__.startswith("build.<locals>.")
    ]
    mutating_tools = [tool_fn for tool_fn in tools if not getattr(tool_fn, "_kicad_read_only", False)]
    read_only_tools = [tool_fn for tool_fn in tools if getattr(tool_fn, "_kicad_read_only", False)]
    for tool_fn in mutating_tools:
        mcp.add_tool(tool_fn, annotations=dict(_MUTATING_LOCAL))
    for tool_fn in read_only_tools:
        mcp.add_tool(tool_fn, annotations=dict(_READ_ONLY_LOCAL))
    return [tool_fn.__name__ for tool_fn in (*mutating_tools, *read_only_tools)]
