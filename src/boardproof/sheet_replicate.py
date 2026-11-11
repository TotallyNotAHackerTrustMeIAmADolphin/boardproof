"""replicate_group: copy a group of symbols with their wires and labels n times on a grid (GitHub issue 94).

Ten identical relay branches were drawn by hand, symbol by symbol, with every reference and label
typed ten times. ``replicate_group`` takes a *region* of one sheet, treats everything that lies
wholly inside it as the group, and writes ``copies`` new copies of it, each moved by one more
``pitch`` (dx, dy). The original is branch 1; ``copies: 9`` makes ten branches.

The copy is made from the raw nodes of the sheet (decision 1, docs/ARCHITECTURE.md: the sheet is
loaded with ``write_layer.load``, new nodes are cloned from existing ones, every number is set with
``write_layer.set_number`` and every string with ``set_string``, the file is saved with
``write_layer.save``). So everything a symbol carries is kept: the library fields, the mirror, the
rotation, the hidden state of every property, ``lib_name``. Only these change per copy:

- every ``(at ..)`` and ``(xy ..)`` moves by the offset (property texts included), rounded to
  0.0001 mm; the pitch must be a multiple of ``grid`` (1.27 mm by default), else ``off_grid``;
- every uuid is new (the symbol's, its pins', the wires', the labels'; none is reused);
- the reference of a symbol is the next free number of its prefix: ``highest used in the whole
  sheet tree + 1``, one new reference per source reference and copy (the units of a multi-unit part
  share it; a power symbol's ``#PWR`` number follows KiCad's own spelling). The Reference property
  and the instance data are set together. On a sheet a parent uses (GitHub issue 79) the instance
  data has one entry per use path of the tree, each with its own new reference; the entry is cloned
  from the source's entry at that path, so the project name and the path text stay as KiCad wrote
  them. A symbol whose instance data is not proven for every use (stale, doubled, legacy table
  only, unproven path) refuses the call;
- a label is *numbered* when its text ends in digits and the call says so (``numbered_labels``,
  below): ``OUT1`` becomes ``OUT2``, ``OUT3`` and so on (source number + copy number, leading zeros
  kept: ``S01`` becomes ``S02``). A label that is not numbered keeps its text, so its copies join
  the same net (a global ``VBUS``, a ground symbol).

``numbered_labels`` is ``"auto"`` (default: every *local* label whose text ends in digits), or a list
of exact label texts of the group (any scope, each must end in digits and must be in the group), or
``[]`` (none). A numbered text that already exists on the sheet (any label kind), or, for a global
label, anywhere in the tree (global labels and power symbol Values), refuses the call as
``label_exists``: a count that runs into an existing net would silently join two nets.

The group is what lies wholly inside ``region`` (closed rectangle, mm). Each object is wholly
inside or wholly outside; one that crosses the boundary (a wire, or a symbol whose pins are on both
sides) refuses the call as ``crosses_region`` naming it, never half a wire. Supported in the group:
symbols, wires, junctions, no-connect markers, local and global labels and free text. A bus, bus
entry, hierarchical label, sheet symbol, text box or any other graphic inside the region refuses as
``unsupported_in_region`` (nothing is guessed). A symbol with only some units of a multi-unit part
inside refuses as ``partial_part``; a local power symbol refuses as ``local_power_in_group``; an
unannotated reference (``R?``) or one that is not ``<prefix><number>`` refuses as
``unannotated_reference``.

Refused before anything is written: a copy whose region would touch an existing object outside the
group or another copy (``overlaps_existing``, listed; the test uses the raw anchors, pin points, wire
ends and label points, not symbol graphics, so the region should enclose the symbol bodies), and a
copy that would leave the page (``outside_page``).

The checks, decision 4:

1. the staged check of ``sheet_editing.run``: every node that is not new is byte-equal, every new
   uuid is present;
2. the netlist oracle. kicad-cli's netlist of the project is read before, after, and once more from
   a scratch copy in which this sheet holds ONLY the new copies (so kicad-cli itself says how a
   copy connects within itself and under which name). The expected netlist is the netlist before
   plus exactly the new components and the new pins: a net whose name exists already (a shared global
   label, a ground symbol) gains the copy pins, every other net of the copies is new, and every old
   net is otherwise unchanged. The after netlist must equal it, net by net (an automatic ``Net-(..)``
   name compared by its pins), and each new component must carry the value and every field of its
   source. A difference is ``write_not_verified`` (check ``netlist_follows_plan``), nothing written;
3. the write goes through ``write_layer.save`` and its reload check.

Not covered, and said so in the tool text: a copy whose own net is named by a label that joins an
interface net of the sheet through a hierarchical label of the same text is refused by the oracle
(it cannot prove that join), and the overlap test does not read symbol graphics.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import sheet_authoring, sheet_editing as se, sheet_tree, write_layer
from boardproof.sheet_authoring import SheetAuthoringError

MAX_COPIES = 100
_REGION_KEYS = ("x1", "y1", "x2", "y2")
_PITCH_KEYS = ("dx", "dy")
_NUMBERED = re.compile(r"^(.*?)(\d+)$", re.DOTALL)
_TAIL = ("sheet_instances", "symbol_instances", "embedded_fonts", "embedded_files")
# what the group may hold, and the drawing kinds the isolated netlist strips from the other sheet items
_LABELS = ("label", "global_label")
_SUPPORTED = ("symbol", "wire", "junction", "no_connect", "label", "global_label", "text")
_QUIET = frozenset({"version", "generator", "generator_version", "uuid", "paper", "title_block", "lib_symbols",
                    "sheet_instances", "symbol_instances", "embedded_fonts", "embedded_files"})
_POINT_TAGS = ("at", "xy", "start", "end", "mid", "center", "pos")
_DRAWING = frozenset({"symbol", "wire", "bus", "junction", "label", "global_label", "hierarchical_label", "text",
                      "no_connect", "bus_entry", "text_box", "polyline", "rectangle", "circle", "arc", "bezier", "image"})
# paper sizes in mm (landscape width, height)
_PAPER = {"A5": (210.0, 148.0), "A4": (297.0, 210.0), "A3": (420.0, 297.0), "A2": (594.0, 420.0), "A1": (841.0, 594.0),
          "A0": (1189.0, 841.0), "A": (279.4, 215.9), "B": (431.8, 279.4), "C": (558.8, 431.8), "D": (863.6, 558.8),
          "E": (1117.6, 863.6)}

Box = tuple[float, float, float, float]  # x0, y0, x1, y1


# --------------------------------------------------------------------------- arguments


def _region(value: Any) -> Box:
    if not isinstance(value, dict) or set(value) != set(_REGION_KEYS):
        raise SheetAuthoringError("invalid_spec", f"region must be an object with exactly the keys {list(_REGION_KEYS)} (mm), got {value!r}")
    for key in _REGION_KEYS:
        if not se.is_mm(value[key]):
            raise SheetAuthoringError("invalid_spec", f"region.{key} must be a finite number of mm within {se.MAX_MM:g}, got {value[key]!r}")
    x0, x1 = sorted((float(value["x1"]), float(value["x2"])))
    y0, y1 = sorted((float(value["y1"]), float(value["y2"])))
    if x1 - x0 < se.TOL or y1 - y0 < se.TOL:
        raise SheetAuthoringError("invalid_spec", f"region has no area ({x0}, {y0}) to ({x1}, {y1})")
    return x0, y0, x1, y1


def _pitch(value: Any, grid: float) -> tuple[float, float]:
    if not isinstance(value, dict) or set(value) != set(_PITCH_KEYS):
        raise SheetAuthoringError("invalid_spec", f"pitch must be an object with exactly the keys {list(_PITCH_KEYS)} (mm), got {value!r}")
    for key in _PITCH_KEYS:
        if not se.is_mm(value[key]):
            raise SheetAuthoringError("invalid_spec", f"pitch.{key} must be a finite number of mm within {se.MAX_MM:g}, got {value[key]!r}")
    dx, dy = float(value["dx"]), float(value["dy"])
    if abs(dx) < se.TOL and abs(dy) < se.TOL:
        raise SheetAuthoringError("invalid_spec", "pitch is zero: a copy would lie on the original")
    for key, v in (("dx", dx), ("dy", dy)):
        steps = v / grid
        if abs(steps - round(steps)) * grid > 1e-6:
            raise SheetAuthoringError(
                "off_grid", f"pitch.{key} {v} mm is not a multiple of the grid {grid} mm; every copy would land off the grid; nothing was written",
                grid=grid, nearest=round(steps) * grid)
    return dx, dy


def _check_args(region: Any, copies: Any, pitch: Any, numbered: Any, grid: Any, dry_run: Any) -> tuple[Box, int, tuple[float, float], float]:
    se._check_move_args(True, grid, False)
    if not isinstance(dry_run, bool):
        raise SheetAuthoringError("invalid_spec", "dry_run must be true or false")
    if isinstance(copies, bool) or not isinstance(copies, int) or not 1 <= copies <= MAX_COPIES:
        raise SheetAuthoringError("invalid_spec", f"copies must be a whole number from 1 to {MAX_COPIES} (the number of NEW copies; the original stays), got {copies!r}")
    if numbered != "auto" and not (isinstance(numbered, list) and all(isinstance(t, str) and t for t in numbered)):
        raise SheetAuthoringError("invalid_spec", f'numbered_labels must be "auto" or a list of label texts, got {numbered!r}')
    if isinstance(numbered, list) and len(set(numbered)) != len(numbered):
        raise SheetAuthoringError("invalid_spec", "numbered_labels names a text twice")
    return _region(region), copies, _pitch(pitch, float(grid)), float(grid)


# --------------------------------------------------------------------------- geometry


def _bbox(points: list[tuple[float, float]]) -> Box:
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _inside(box: Box, region: Box) -> bool:
    return box[0] >= region[0] - se.TOL and box[1] >= region[1] - se.TOL and box[2] <= region[2] + se.TOL and box[3] <= region[3] + se.TOL


def _touches(box: Box, region: Box) -> bool:
    return not (box[2] < region[0] - se.TOL or box[0] > region[2] + se.TOL or box[3] < region[1] - se.TOL or box[1] > region[3] + se.TOL)


def _shift_box(box: Box, dx: float, dy: float) -> Box:
    return box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy


def _points(node: SExp) -> list[tuple[float, float]]:
    """Every coordinate pair of the node's ``at``, ``xy``, ``start`` ... children (any depth), plus a sheet's corner."""
    out: list[tuple[float, float]] = []
    for child in _walk(node):
        if child.is_atom or child.name not in _POINT_TAGS or len(child.children) < 2:
            continue
        try:
            out.append((child.get_float(0), child.get_float(1)))
        except (TypeError, ValueError):
            continue
    if node.name == "sheet":
        at, size = node.find_children("at"), node.find_children("size")
        if at and size:
            out.append((at[0].get_float(0) + size[0].get_float(0), at[0].get_float(1) + size[0].get_float(1)))
    return out


def _extent(root: SExp, node: SExp) -> Box | None:
    if node.name == "symbol" and node.find_children("lib_id"):
        x, y, _a = se._at(node)
        pts = [(x, y)] + [(p["x"], p["y"]) for p in se.pin_positions(root, node, with_hidden=True)]
        return _bbox(pts)
    if node.name == "wire":
        return _bbox(se._wire_ends(node))
    if node.name in ("junction", "no_connect", "label", "global_label", "hierarchical_label", "text"):
        at = node.find_children("at")
        return _bbox([(at[0].get_float(0), at[0].get_float(1))]) if at else None
    pts = _points(node)
    return _bbox(pts) if pts else None


def _page(root: SExp) -> tuple[float, float] | None:
    paper = root.find_children("paper")
    if not paper:
        return None
    name = paper[0].get_string(0) or ""
    if name == "User":
        try:
            return paper[0].get_float(1), paper[0].get_float(2)
        except (TypeError, ValueError):
            return None
    size = _PAPER.get(name)
    if size is None:
        return None
    if any((a.value if a.is_atom else None) == "portrait" for a in paper[0].children):
        return size[1], size[0]
    return size


# --------------------------------------------------------------------------- the group


def _classify(root: SExp, region: Box) -> tuple[list[SExp], list[SExp]]:
    """(group nodes in file order, the other nodes with an extent); a crossing or unsupported node refuses."""
    group: list[SExp] = []
    others: list[SExp] = []
    crossing: list[str] = []
    unsupported: list[str] = []
    for node in root.children:
        if node.is_atom or node.name in _QUIET:
            continue
        box = _extent(root, node)
        if box is None:
            continue
        what = f"{node.name} {_describe(node)} at ({box[0]:g}, {box[1]:g})"
        if _inside(box, region):
            if node.name not in _SUPPORTED:
                unsupported.append(what)
            else:
                group.append(node)
        elif _touches(box, region):
            crossing.append(what)
        else:
            others.append(node)
    if crossing:
        raise SheetAuthoringError(
            "crosses_region", "these objects cross the region's boundary (each object must be wholly inside or wholly outside; "
            "move the region's edge or move the object): " + "; ".join(crossing[:10]) + "; nothing was written", crossing=crossing[:se.MAX_LISTED])
    if unsupported:
        raise SheetAuthoringError(
            "unsupported_in_region", "the region holds objects replicate_group does not copy (it copies symbols, wires, junctions, no-connect "
            "markers, local and global labels and free text): " + "; ".join(unsupported[:10]) + "; nothing was written", unsupported=unsupported[:se.MAX_LISTED])
    if not group:
        raise SheetAuthoringError("empty_group", "the region holds nothing to copy; nothing was written")
    return group, others


def _describe(node: SExp) -> str:
    if node.name == "symbol":
        return se._ref(node) or se._uuid(node)
    if node.name in _LABELS + ("hierarchical_label", "text"):
        return repr(node.get_string(0))
    return se._uuid(node)


def _split_ref(ref: str, who: str) -> tuple[str, int, str]:
    match = _NUMBERED.match(ref)
    if not ref or "?" in ref or match is None or not match.group(1):
        raise SheetAuthoringError(
            "unannotated_reference", f"{who} has the reference {ref!r}: replicate_group counts references up and needs <prefix><number> "
            "(annotate the symbol first, for example with fix_annotation); nothing was written")
    return match.group(1), int(match.group(2)), match.group(2)


def _render_ref(prefix: str, number: int, digits: str) -> str:
    if prefix == "#PWR":  # KiCad's own spelling: #PWR01 .. #PWR099, then #PWR0100
        return f"#PWR{number:02d}" if number < 100 else f"#PWR0{number}"
    return f"{prefix}{str(number).zfill(len(digits))}"


# --------------------------------------------------------------------------- instance data and references


class _Plan:
    """Everything the copies need, worked out before any node is written."""

    def __init__(self) -> None:
        self.uses: list[str] = []                                 # the use paths of this sheet file in the tree
        self.entries: dict[str, dict[str, SExp]] = {}             # symbol uuid -> use path -> the matched (project, path) pair
        self.records: dict[str, dict[str, dict]] = {}             # symbol uuid -> use path -> placed_instances record
        self.new_ref: dict[tuple[int, str, str], str] = {}        # (copy, use path, source ref at that use) -> new reference
        self.new_text: dict[tuple[int, str], str] = {}            # (copy, source label text) -> new text
        self.numbered: list[str] = []                             # the label texts that count up


def _entry_of(symbol: SExp, use: str) -> tuple[str, SExp] | None:
    key = sheet_tree.path_key(use)
    for instances in symbol.find_children("instances"):
        for project in instances.find_children("project"):
            for entry in project.find_children("path"):
                if key is not None and sheet_tree.path_key(entry.get_string(0) or "") == key:
                    return project.get_string(0) or "", entry
    return None


def _plan_references(root: SExp, tree: sheet_tree.SheetTree, path: Path, group: list[SExp], copies: int, plan: _Plan) -> None:
    uses = [u for f, u in tree.uses if f == path.resolve()]
    if not uses:
        raise SheetAuthoringError("parent_not_in_tree", f"{path.name} is not a sheet of the project's tree; nothing was written")
    plan.uses = uses
    sheet_rel = tree.rel(path)
    records = [r for r in sheet_tree.placed_instances(tree) if r["sheet"] == sheet_rel]
    symbols = [n for n in group if n.name == "symbol"]
    group_ids = {se._uuid(n) for n in symbols}
    for symbol in symbols:
        uid = se._uuid(symbol)
        mine = {r["path"]: r for r in records if r["uuid"] == uid}
        who = se._ref(symbol) or uid
        for use in uses:
            rec = mine[use]  # placed_instances gives one record per symbol and sheet use
            bad = [k for k in ("stale_only", ) if rec[k] == "yes"] + [k for k in ("live_entries", "unproven_paths") if rec[k]]
            entry = _entry_of(symbol, use)
            if bad or entry is None:
                raise SheetAuthoringError(
                    "unproven_instance_data", f"{who} has instance data at {use} that is not proven ({', '.join(bad) or 'no matching entry'}: "
                    f"{rec['live_entries'] or rec['unproven_paths'] or 'annotate in KiCad'}); nothing was written")
            plan.records.setdefault(uid, {})[use] = rec
            plan.entries.setdefault(uid, {})[use] = (entry[0], entry[1])  # type: ignore[assignment]
        refs = {use: plan.records[uid][use]["ref"] for use in uses}
        for ref in refs.values():
            _split_ref(ref, who)
        lib = _lib_power(root, symbol)
        if lib == "local":
            raise SheetAuthoringError("local_power_in_group", f"{who} is a local power symbol; replicate_group does not copy those; nothing was written")
    # a part with units outside the group is not copied piecemeal
    group_refs = {use: {plan.records[g][use]["ref"] for g in plan.records} for use in uses}
    for rec in records:
        if rec["uuid"] not in group_ids and not rec["ref"].startswith("#") and rec["ref"] in group_refs.get(rec["path"], ()):
            raise SheetAuthoringError(
                "partial_part", f"unit {rec['unit']} of {rec['ref']} lies outside the region while another unit is inside; "
                "replicate_group copies whole parts only; nothing was written")
    highest: dict[str, int] = {}
    existing: set[str] = set()
    for rec in sheet_tree.placed_instances(tree):
        existing.add(rec["ref"])
        match = _NUMBERED.match(rec["ref"])
        if match and match.group(1) and "?" not in rec["ref"]:
            highest[match.group(1)] = max(highest.get(match.group(1), 0), int(match.group(2)))
    for i in range(1, copies + 1):
        for use in uses:
            for ref in sorted(group_refs[use], key=lambda r: (_split_ref(r, r)[0], _split_ref(r, r)[1], r)):
                prefix, _number, digits = _split_ref(ref, ref)
                highest[prefix] = highest.get(prefix, 0) + 1
                plan.new_ref[(i, use, ref)] = _render_ref(prefix, highest[prefix], digits)
    # the netlist compares pins, so two parts under one reference would pass it: the references are checked here
    made = list(plan.new_ref.values())
    bad = sorted({r for r, c in Counter(made).items() if c > 1} | {r for r in made if r in existing})
    if bad:
        raise SheetAuthoringError(
            "write_not_verified", f"check references_unique: the new references {bad[:10]} are used twice or already exist; nothing was written",
            check="references_unique", references=bad[:se.MAX_LISTED])


def _lib_power(root: SExp, symbol: SExp) -> str | None:
    lib = se._lib_symbol(root, symbol)
    if lib is None:
        return None
    power = lib.find_children("power")
    if not power:
        return None
    return power[0].get_string(0) or "global"


# --------------------------------------------------------------------------- labels


def _plan_labels(root: SExp, tree: sheet_tree.SheetTree, path: Path, group: list[SExp], copies: int, numbered: Any, plan: _Plan) -> None:
    labels = [n for n in group if n.name in _LABELS]
    texts = {n.get_string(0) or "": n.name for n in labels}
    if numbered == "auto":
        chosen = sorted(t for t, kind in texts.items() if kind == "label" and _NUMBERED.match(t))
    else:
        missing = [t for t in numbered if t not in texts]
        if missing:
            raise SheetAuthoringError("not_found", f"numbered_labels names texts that are no label of the group: {missing}; nothing was written", not_found=missing)
        plain = [t for t in numbered if not _NUMBERED.match(t)]
        if plain:
            raise SheetAuthoringError("not_numbered_text", f"numbered_labels names texts that do not end in digits: {plain}; nothing was written", texts=plain)
        chosen = sorted(numbered)
    plan.numbered = chosen
    if not chosen:
        return
    sheet_texts = {n.get_string(0) or "" for n in root.children if not n.is_atom and n.name in se.LABEL_TAGS}
    global_texts: set[str] = set()
    for file in tree.files:
        parsed = tree.parsed.get(Path(file).resolve()) or write_layer.load(Path(file))
        for node in parsed.children:
            if node.is_atom:
                continue
            if node.name == "global_label":
                global_texts.add(node.get_string(0) or "")
            elif node.name == "symbol":
                prop = se._property(node, "Value")
                if prop is not None and (se._lib_symbol(parsed, node) is not None) and se._ref(node).startswith("#"):
                    global_texts.add(prop.get_string(1) or "")
    made: dict[str, str] = {}
    for text in chosen:
        match = _NUMBERED.match(text)
        assert match is not None
        prefix, digits = match.group(1), match.group(2)
        for i in range(1, copies + 1):
            new = f"{prefix}{str(int(digits) + i).zfill(len(digits))}"
            clash = new in sheet_texts or (texts[text] == "global_label" and new in global_texts) or new in made
            if clash:
                raise SheetAuthoringError(
                    "label_exists", f"counting {text!r} up gives {new!r} for copy {i}, which already exists "
                    "(on this sheet, as a global name in the tree, or as another count of this call); nothing was written", label=text, would_be=new)
            made[new] = text
            plan.new_text[(i, text)] = new


# --------------------------------------------------------------------------- building the copies


def _clone(node: SExp) -> SExp:
    return write_layer.parse_string(write_layer.serialise(node))


def _walk(node: SExp):
    yield node
    for child in node.children:
        if not child.is_atom:
            yield from _walk(child)


def _shift(node: SExp, dx: float, dy: float) -> None:
    for sub in _walk(node):
        if sub.name in ("at", "xy") and len(sub.children) >= 2:
            write_layer.set_number(sub, 0, se._r(sub.get_float(0) + dx))
            write_layer.set_number(sub, 1, se._r(sub.get_float(1) + dy))


def _fresh_uuids(node: SExp, used: set[str]) -> None:
    for sub in _walk(node):
        if sub.name == "uuid" and sub.children:
            new = sheet_authoring._uuid().get_string(0) or ""
            while new in used:
                new = sheet_authoring._uuid().get_string(0) or ""
            used.add(new)
            write_layer.set_string(sub, 0, new)


def _copy_symbol(node: SExp, copy: int, plan: _Plan) -> tuple[SExp, dict[str, str]]:
    uid = se._uuid(node)
    new = _clone(node)
    refs: dict[str, str] = {}
    new_instances = SExp.list("instances")
    projects: dict[str, SExp] = {}
    for use in plan.uses:
        project, entry = plan.entries[uid][use]
        src = plan.records[uid][use]["ref"]
        refs[use] = plan.new_ref[(copy, use, src)]
        piece = _clone(entry)
        write_layer.set_string(piece.find_children("reference")[0], 0, refs[use])
        if project not in projects:
            projects[project] = SExp.list("project", write_layer.string(project))
            new_instances.children.append(projects[project])
        projects[project].children.append(piece)
    index = next(k for k, c in enumerate(new.children) if not c.is_atom and c.name == "instances")
    new.children[index] = new_instances
    prop = se._property(new, "Reference")
    if prop is not None:
        write_layer.set_string(prop, 1, refs[plan.uses[0]])
    return new, refs


def _insert(root: SExp, node: SExp) -> None:
    tail = [k for k, c in enumerate(root.children) if not c.is_atom and c.name in _TAIL]
    root.children.insert(tail[0] if tail else len(root.children), node)


# --------------------------------------------------------------------------- the netlist oracle


def _canon(nets: dict[str, frozenset]) -> dict[str, frozenset]:
    """Nets keyed by name; an automatic ``Net-(..)`` name is replaced by its pins (it follows the pins, not a label)."""
    return {(n if not se._is_auto(n) else "auto:" + ",".join(sorted(f"{r}.{p}" for r, p in v))): v for n, v in nets.items()}


def _oracle(sheet: se.Sheet, path: Path, new_root: SExp, added: set[str], group_ids: set[str], new_refs: set[str],
            sources: dict[str, str], copy_of: dict[str, int], components: dict, summary: dict, numbered: list[str]):
    """The self-check of ``netlist_follows_plan`` (see the module docstring)."""

    def only(keep: set[str]) -> SExp:
        tree = _clone(new_root)
        tree.children[:] = [c for c in tree.children if c.is_atom or c.name not in _DRAWING or se._uuid(c) in keep]
        return tree

    def check(before: dict[str, frozenset], after: dict[str, frozenset]) -> None:
        isolated: dict[str, frozenset] = se.read_nets(sheet.tree, path, only(added), None)
        alone: dict[str, frozenset] = se.read_nets(sheet.tree, path, only(group_ids), None)
        problems: list[str] = []
        # (1) each copy connects within itself exactly as the original group does within itself
        src_refs = set(sources.values())
        want = {frozenset(n for n in nodes if n[0] in src_refs) for nodes in alone.values()} - {frozenset()}
        for i in sorted(set(copy_of.values())):
            got = {frozenset((sources[r], p) for r, p in nodes if copy_of.get(r) == i) for nodes in isolated.values()} - {frozenset()}
            if got != want:
                problems.append(f"copy {i} connects differently from the original group: {sorted(map(_pins, got ^ want))[:3]}")
        # (2) the netlist after is the netlist before plus the pins of the copies
        mine = {name: frozenset(n for n in nodes if n[0] in new_refs) for name, nodes in isolated.items()}
        expected: dict[str, frozenset] = dict(before)
        joined = []
        for name, nodes in mine.items():
            if nodes and name in expected and not se._is_auto(name):
                joined.append(name)
            if nodes:
                expected[name] = expected.get(name, frozenset()) | nodes
        want_nets, got_nets = _canon(expected), _canon(after)
        for name in sorted(set(want_nets) | set(got_nets)):
            if want_nets.get(name) != got_nets.get(name):
                problems.append(f"net {name}: expected {_pins(want_nets.get(name))}, kicad-cli has {_pins(got_nets.get(name))}")
        # (3) the expectation is read from the tree that was written, so it cannot tell a label that was not counted up:
        # the nets named by a numbered source text must still be exactly what they were before
        for name in sorted(n for n in before if any(n == t or n.endswith("/" + t) for t in numbered)):
            if after.get(name) != before[name]:
                problems.append(f"net {name} (a numbered label's source) changed: {_pins(before[name])} before, {_pins(after.get(name))} after")
        # (4) every new component carries the value and every field of its source
        by_ref = {c["ref"]: c for c in components.get("after", [])}
        for new, src in sorted(sources.items()):
            if src in by_ref:
                got_c, src_c = by_ref.get(new), by_ref[src]
                if got_c is None or (got_c["value"], got_c["fields"]) != (src_c["value"], src_c["fields"]):
                    problems.append(f"component {new} is missing or differs from its source {src} in value or fields")
        if problems:
            raise SheetAuthoringError(
                "write_not_verified", "check netlist_follows_plan: kicad-cli's netlist after the change is not the netlist before plus the copies: "
                + "; ".join(problems[:6]) + "; nothing was written", check="netlist_follows_plan", problems=problems[:se.MAX_LISTED])
        summary.update({"checked": True, "nets_before": len(before), "nets_after": len(after),
                        "new_nets": len(after) - len(before), "joined_existing_nets": sorted(joined),
                        "new_components": len([r for r in new_refs if not r.startswith("#")])})

    return check


def _pins(nodes: frozenset | None) -> str:
    return "absent" if nodes is None else "{" + ", ".join(f"{r}.{p}" for r, p in sorted(nodes)[:8]) + (", ..." if len(nodes) > 8 else "") + "}"


# --------------------------------------------------------------------------- the tool


def replicate_group(path: str, region: Any, copies: Any, pitch: Any, numbered_labels: Any, root_sch: str | None, *,
                    grid: Any, dry_run: Any) -> dict[str, Any]:
    """Copy the group inside ``region`` ``copies`` times at ``pitch`` (see the tool text)."""

    def mutate(sheet: se.Sheet, root: SExp) -> se.Outcome:
        box, n, (dx, dy), step = _check_args(region, copies, pitch, numbered_labels, grid, dry_run)
        file = Path(path)
        group, others = _classify(root, box)
        plan = _Plan()
        _plan_references(root, sheet.tree, file, group, n, plan)
        _plan_labels(root, sheet.tree, file, group, n, numbered_labels, plan)
        # overlap: every copy's region clear of every other object and of every other copy
        page = _page(root)
        boxes = [box] + [_shift_box(box, i * dx, i * dy) for i in range(1, n + 1)]
        touching: list[str] = []
        for i in range(1, n + 1):
            for j in range(0, i):
                if _touches(boxes[i], boxes[j]):
                    touching.append(f"copy {i} meets {'the original' if j == 0 else f'copy {j}'}")
            for other in others:
                ext = _extent(root, other)
                if ext is not None and _touches(ext, boxes[i]):
                    touching.append(f"copy {i} meets {other.name} {_describe(other)}")
            if page is not None and (boxes[i][0] < 0 or boxes[i][1] < 0 or boxes[i][2] > page[0] or boxes[i][3] > page[1]):
                raise SheetAuthoringError(
                    "outside_page", f"copy {i} would lie at ({boxes[i][0]:g}, {boxes[i][1]:g}) to ({boxes[i][2]:g}, {boxes[i][3]:g}), "
                    f"outside the {page[0]:g} x {page[1]:g} mm page; nothing was written", page=list(page))
        if touching:
            raise SheetAuthoringError(
                "overlaps_existing", "the copies would overlap existing objects: " + "; ".join(touching[:10]) + "; nothing was written",
                overlaps=touching[:se.MAX_LISTED])
        outcome = se.Outcome()
        used = {sub.get_string(0) or "" for node in root.children if not node.is_atom for sub in _walk(node) if sub.name == "uuid" and sub.children}
        made: list[dict[str, Any]] = []
        sources: dict[str, str] = {}
        copy_of: dict[str, int] = {}
        new_refs: set[str] = set()
        counts: Counter = Counter()
        for i in range(1, n + 1):
            ox, oy = i * dx, i * dy
            entry: dict[str, Any] = {"copy": i, "offset": se._round_pt(ox, oy), "references": {}, "labels": {}}
            for node in group:
                if node.name == "symbol":
                    new, refs = _copy_symbol(node, i, plan)
                    src_ref = plan.records[se._uuid(node)][plan.uses[0]]["ref"]
                    entry["references"][src_ref] = refs[plan.uses[0]] if len(plan.uses) == 1 else refs
                    for use in plan.uses:
                        sources[refs[use]] = plan.records[se._uuid(node)][use]["ref"]
                        new_refs.add(refs[use])
                        copy_of[refs[use]] = i
                else:
                    new = _clone(node)
                    if node.name in _LABELS:
                        text = node.get_string(0) or ""
                        if (i, text) in plan.new_text:
                            write_layer.set_string(new, 0, plan.new_text[(i, text)])
                            entry["labels"][text] = plan.new_text[(i, text)]
                _shift(new, ox, oy)
                _fresh_uuids(new, used)
                _insert(root, new)
                outcome.added.add(se._uuid(new))
                counts[node.name] += 1
            made.append(entry)
        summary: dict[str, Any] = {"checked": False}
        components: dict[str, list] = {}

        def keep(found: list[dict]) -> None:
            components["after"] = found

        outcome.post_netlist = keep
        outcome.post_check = _oracle(sheet, file, root, set(outcome.added), {se._uuid(g) for g in group}, new_refs, sources, copy_of,
                                     components, summary, plan.numbered)
        outcome.compare = lambda _b, _a, _r: []  # the oracle replaces the "no connectivity change" rule: copies add nets
        shared = sorted({g.get_string(0) or "" for g in group if g.name == "label"} - set(plan.numbered))
        outcome.report = {
            "created": {"copies": made, "count": n, "objects": dict(counts), "uuids": len(outcome.added)},
            "group": {"symbols": sorted({se._ref(g) for g in group if g.name == "symbol"}), "objects": {k: v // n for k, v in counts.items()}},
            "pitch": se._round_pt(dx, dy), "grid": step, "numbered_labels": plan.numbered, "sheet_uses": plan.uses,
            "netlist": summary,
            **({"warnings": [{"code": "label_shared", "label": t, "note": "a local label that is not counted up keeps its text, so all copies join one net"}
                             for t in shared]} if shared else {}),
            "note": "the netlist was compared with the netlist before plus exactly the new components and nets (kicad-cli, scratch copies)"}
        return outcome

    result = se.run(path, root_sch, mutate, allow_connectivity_change=False, dry_run=dry_run if isinstance(dry_run, bool) else False)
    if result.get("ok") and dry_run is True:
        result["would_create"] = result.pop("created")
    return result
