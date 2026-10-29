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
import enum
import os
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

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

    kicad_tools 0.20.0 writes a ``\\r`` inside a string raw (docs/ARCHITECTURE.md decision 1:
    escaping is build ticket 42's write layer) and its parser drops it on reload, so the
    text would not survive. The tools refuse such input before loading anything.
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
    return {"components": len(refs), "refs": refs, "nets": nets}


def _verify_schematic_write(path: Path, written: Path, added_refs: set[str]) -> None:
    """The builder tools' self-check (decision 4, build ticket 38), on the temporary file
    ``written`` before it replaces ``path``; raises ``SchematicWriteNotVerified``.

    1. Tokens: every top-level node of ``path`` (``_kept_nodes``) is in ``written``
       unchanged: the tools only add.
    2. kicad-cli's netlist of each, read as the root: every component before is still there
       and the count rises by exactly the new references in ``added_refs`` (``#`` power
       references are not components); every net before lies within one net after (a new
       wire may join two nets; nothing may split one or lose a pin)."""
    from collections import Counter

    from kicad_mcp import write_layer

    source_kept, written_kept = _kept_nodes(write_layer.load(path)), _kept_nodes(write_layer.load(written))
    missing = sorted(label for (_text, label), n in (source_kept - written_kept).items() for _ in range(n))
    write_check = {"missing": missing, "unexpected": [], "tolerated": []}
    if missing:
        raise SchematicWriteNotVerified(
            f"write_not_verified: {len(missing)} node(s) of {path.name} would not survive the write: "
            + ", ".join(missing[:10]),
            [{"file": path.name, "expected": label, "actual": None} for label in missing],
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


def _write_schematic_utf8(sch: Any, path: Path, *, added_refs: set[str] = frozenset()) -> None:
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
    if getattr(sch, "_synthesized_pwr_defs", None):
        # write() would also emit kicad_tools_pwr sidecars, with the platform
        # encoding. This toolset never synthesizes power symbols (add_power
        # uses the stock power: library), so refuse rather than skip them.
        raise RuntimeError("synthesized power symbols need kicad_tools' sidecar files; not written")
    sch.auto_size_paper()  # what write() does by default
    content = _schematic_text(sch)
    if "\r" in content:  # backstop: the tools refuse \r in their input before this
        raise ValueError("carriage return in the schematic text; kicad_tools would write it unescaped")
    tmp = path.with_name(path.name + ".utf8.tmp")
    try:
        tmp.write_text(content, encoding="utf-8")
        if tmp.read_text(encoding="utf-8") != content:
            raise OSError(f"read-back of the written schematic differs from its content; {path.name} left unchanged")
        _verify_schematic_write(path, tmp, set(added_refs))
        os.replace(tmp, path)
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


def build(mcp: MCPServer) -> list[str]:
    """Register the schematic toolset. See toolsets/registry.py for the contract."""

    def add_symbols(sch_path: str, specs: list[dict]) -> dict:
        """Place one or more symbols onto an existing schematic in one call.

        Each spec: lib_id, x, y, ref (required); value, rotation, footprint,
        unit, in_bom, dnp (optional). Batch-oriented -- never loop this one
        symbol at a time.

        Refuses a sheet older than KiCad 8's format (error old_sheet_format, nothing
        written): kicad_tools would rewrite it and drop what it does not model. Before
        the sheet is replaced, every node it had must be unchanged (the paper size
        aside, which the write may enlarge) and kicad-cli's netlist must keep every
        component and every connection; otherwise the result is ok: false and lists
        each mismatch, and the sheet stays unchanged.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        if (refused := _refuse_carriage_returns(specs, "specs")) is not None:
            return refused
        _ensure_kicad_symbol_dir_set()
        if (refused := _old_format_refusal(path)) is not None:
            return refused
        try:
            from kicad_tools.schematic.models.schematic import Schematic

            sch = Schematic.load(path)
            placed = []
            for spec in specs:
                instance = sch.add_symbol(
                    lib_id=spec["lib_id"],
                    x=spec["x"],
                    y=spec["y"],
                    ref=spec["ref"],
                    value=spec.get("value"),
                    rotation=spec.get("rotation", 0),
                    footprint=spec.get("footprint", ""),
                    unit=spec.get("unit", 1),
                    in_bom=spec.get("in_bom", True),
                    dnp=spec.get("dnp", False),
                )
                placed.append(instance.reference)
            _write_schematic_utf8(sch, path, added_refs=set(placed))
        except SchematicWriteNotVerified as exc:
            return _not_verified_result(exc)
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"could not add symbols: {exc}")
        return {"status": "ok", "placed": placed, "count": len(placed)}

    def add_wires(sch_path: str, specs: list[dict]) -> dict:
        """Add one or more wires to an existing schematic in one call,
        drawn with right-angle corners (no diagonals) by default.

        Each spec:
          * `p1` [x, y], `p2` [x, y]  (required)
          * `route`: "orthogonal" (default) | "direct".
          * `snap`: bool, default True -- see WARNING below.
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

        WARNING (real bug found and worked around 2026-09-02, see archived backlog):
        kicad_tools' underlying add_wire() defaults to grid-snapping wire
        endpoints. If p1/p2 are a pin's *exact* coordinates and that pin
        isn't itself on-grid, snapping silently moves the wire endpoint off
        the pin -- the wire is added with no error, but the intended
        electrical connection never happens (every such pad shows
        "unconnected" in ERC/netlist export). Pass `"snap": false` in a
        spec when p1/p2 are exact pin coordinates you need to land on
        precisely; leave snap on (the default) for general tidy routing
        between points that don't need to be exact. Each spec's `snap` is
        forwarded to *every* segment of its route, intermediate corners
        included -- snapping is per-coordinate and deterministic, so a
        snapped route stays orthogonal.

        Returns `count` (specs drawn) and `segments` (wire segments actually
        emitted, >= count once any spec takes an L or Z path).

        Refuses a sheet older than KiCad 8's format (error old_sheet_format, nothing
        written): kicad_tools would rewrite it and drop what it does not model. Before
        the sheet is replaced, every node it had must be unchanged (the paper size
        aside, which the write may enlarge) and kicad-cli's netlist must keep every
        component and every connection; otherwise the result is ok: false and lists
        each mismatch, and the sheet stays unchanged.
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
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"could not load schematic: {exc}")

        added, segments = 0, 0
        errors: list[dict] = []
        for i, spec in enumerate(specs):
            try:
                p1 = (float(spec["p1"][0]), float(spec["p1"][1]))
                p2 = (float(spec["p2"][0]), float(spec["p2"][1]))
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
                # add_wire_path forwards `snap` to every segment it creates,
                # intermediate waypoints included -- do NOT drop the kwarg.
                sch.add_wire_path(*points, snap=spec.get("snap", True))
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
                return _incomplete(f"could not write schematic: {exc}", errors=errors)
        if not added:
            if not specs:
                return {"status": "ok", "count": 0, "segments": 0}
            return _incomplete("no wires could be added", errors=errors, count=0, segments=0)
        result = {"status": "partial" if errors else "ok", "count": added, "segments": segments}
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
            return _incomplete(f"could not add junctions: {exc}")
        return {"status": "ok", "count": len(points)}

    def add_labels(sch_path: str, specs: list[dict]) -> dict:
        """Add one or more labels/power symbols/no-connects to an existing
        schematic in one call.

        Each spec: kind ("label"|"global"|"hier"|"power"|"no_connect"),
        x, y (required); text (required for label/global/hier/power -- for
        "power" it's the power symbol lib_id, e.g. "power:GND"), rotation,
        shape (global/hier label direction), snap (optional bool, default
        True -- see WARNING).

        WARNING (real bug found and fixed 2026-09-02, same root cause as
        add_wires' snap bug in this same module, see archived backlog): every
        underlying kicad_tools add_label/add_global_label/add_hier_label/
        add_power/add_no_connect call defaults to grid-snapping its
        position. If x/y are a pin's *exact* coordinate (as they must be
        for the label/flag to actually land on that pin's net) and the pin
        isn't itself on-grid, snapping silently moves it off -- e.g. a
        power-flag symbol meant to drive a chip's VCC pin ends up on an
        unconnected phantom net, and ERC reports "Input Power pin not
        driven by any Output Power pins" with no indication the real cause
        was a silent position shift. Pass `"snap": false` in a spec when
        x/y must land exactly on a pin.

        Refuses a sheet older than KiCad 8's format (error old_sheet_format, nothing
        written): kicad_tools would rewrite it and drop what it does not model. Before
        the sheet is replaced, every node it had must be unchanged (the paper size
        aside, which the write may enlarge) and kicad-cli's netlist must keep every
        component and every connection; otherwise the result is ok: false and lists
        each mismatch, and the sheet stays unchanged.
        """
        path = Path(sch_path)
        if not path.is_file():
            return _incomplete(f"file not found: {sch_path}")
        if (refused := _refuse_carriage_returns(specs, "specs")) is not None:
            return refused
        _ensure_kicad_symbol_dir_set()
        if (refused := _old_format_refusal(path)) is not None:
            return refused
        try:
            from kicad_tools.schematic.models.schematic import Schematic

            sch = Schematic.load(path)
            placed = []
            for spec in specs:
                kind = spec["kind"]
                x, y = spec["x"], spec["y"]
                rotation = spec.get("rotation", 0)
                snap = spec.get("snap", True)
                if kind == "label":
                    sch.add_label(spec["text"], x, y, rotation=rotation, snap=snap)
                elif kind == "global":
                    sch.add_global_label(spec["text"], x, y, shape=spec.get("shape", "bidirectional"), rotation=rotation, snap=snap)
                elif kind == "hier":
                    sch.add_hier_label(spec["text"], x, y, shape=spec.get("shape", "input"), rotation=rotation, snap=snap)
                elif kind == "power":
                    sch.add_power(spec["text"], x, y, rotation=rotation, snap=snap)
                elif kind == "no_connect":
                    sch.add_no_connect(x, y, snap=snap)
                else:
                    return {"status": "error", "reason": f"unknown label kind: {kind!r}"}
                placed.append(kind)
            _write_schematic_utf8(sch, path)
        except SchematicWriteNotVerified as exc:
            return _not_verified_result(exc)
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"could not add labels: {exc}")
        return {"status": "ok", "placed": placed, "count": len(placed)}

    def assign_footprints(sch_path: str, mapping: dict[str, str]) -> dict:
        """Assign footprints to symbols by reference designator in one call.

        `mapping` is {reference: footprint_id}. Batch-oriented -- assigns
        every footprint a design needs in one call, not one call per part.
        Before writing, it copies every sheet to the returned `backup_dir`, outside the project.
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
            backup_dir = _backup_outside_project(tree)
            exit_code = run_fix_annotation(path, dry_run=False, backup=False)
            if exit_code != 0:
                return _incomplete(
                    f"run_fix_annotation exited with code {exit_code}", backup_dir=str(backup_dir), **tree.report()
                )
            after_refs = sorted(ref for ref, _ in sheet_tree.placed_references(sheet_tree.walk(path)))
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

    def check_connectivity(sch_path: str) -> dict:
        """Check for unconnected pins and connection issues -- a narrower,
        faster check than full ERC (`sch_run_erc` in the `verification`
        toolset runs KiCad's ERC). Useful right after wiring a schematic,
        before running full ERC. Neither check is a verdict: a clean result
        here or from ERC says nothing about the board, and only verify_board
        gives one.
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
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"connectivity check could not complete: {exc}")
        return {
            **tree.report(),
            "analysed_sheets": analysed,
            "status": "ok",
            "unconnected_pins": _to_jsonable(unconnected),
            "unconnected_count": len(unconnected),
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
    mutating_tools = [add_symbols, add_wires, add_junctions, add_labels, assign_footprints, fix_annotation]
    read_only_tools = [check_connectivity]
    for tool_fn in mutating_tools:
        mcp.add_tool(tool_fn, annotations=dict(_MUTATING_LOCAL))
    for tool_fn in read_only_tools:
        mcp.add_tool(tool_fn, annotations=dict(_READ_ONLY_LOCAL))
    return [tool_fn.__name__ for tool_fn in (*mutating_tools, *read_only_tools)]
