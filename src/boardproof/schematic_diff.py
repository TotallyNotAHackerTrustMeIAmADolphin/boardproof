"""diff_schematic: what changed between two states of a schematic project (GitHub issues 98 and 125).

Two states are compared: two projects (two paths), or one project at a git revision against its working
files or against another revision. A state read from a git revision is rebuilt in a scratch folder from the
object database (``git ls-tree`` and ``git cat-file``; nothing is checked out, no index or working file is
touched, ``GIT_OPTIONAL_LOCKS=0``). Everything else is read from the raw sheet files through the readers the
other schematic tools share (``sheet_tree``, ``field_check``'s walk) and from kicad-cli's netlist of each state, which
runs on a scratch copy (it writes beside its input). The tool writes nothing the caller owns.

Only symbols and nets are compared. Labels and texts, wires, junctions, sheet symbols, no-connect markers, graphics, the title block
and the paper are not (``NOT_COMPARED``, returned with every result): a dangling wire or a note added to a sheet changes neither
list, so ``no_symbol_or_net_differences`` is a statement about those two lists only, never "the two states are the same".

The comparison is split in two parts that never mix their evidence:

- symbols, per sheet file, from the raw files: ``added``, ``removed``, ``changed`` (reference, lib_id, unit,
  value, footprint, dnp, on_board, mirror and every other property, field by field) and ``moved`` (position or
  rotation). A symbol is matched inside one sheet file by its uuid, then (the rest) by reference and unit.
- nets, from the two netlists: ``added``, ``removed``, ``renamed`` (the same pins under another name), and
  ``members_changed`` (the same name, other pins). A generated name (``Net-(R2-Pad2)``) that only follows a reference
  is not a rename and is counted as ``auto_renamed``. Pins of a symbol whose reference changed are translated to the
  new reference first, so a renumbered part is not a net change.

A state that does not load (parse error, missing sheet, a revision that does not exist, a root sheet absent at a
revision) is reported with its reason and nothing is compared: the result is ``incomplete`` and ``ok`` false.
The comparison is never guessed from a partial tree.
"""

from __future__ import annotations

import math
import os
import re
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from boardproof import field_check, sheet_authoring, sheet_editing, sheet_tree
from boardproof.sheet_authoring import SheetAuthoringError

DEFAULT_LIMIT = 200
MAX_LIMIT = 5000
MOVE_TOLERANCE_MM = 0.001
# what a result never says anything about: only symbols (raw files) and nets (kicad-cli netlist) are compared
NOT_COMPARED = ["labels, texts and text boxes (a label shows only through the nets it changes)", "wires and junctions (only through the nets they change)",
                "sheet symbols and their sheet pins", "no-connect markers (only through the nets they change)", "graphic items",
                "title block, paper size, project file and library tables"]
GIT_TIMEOUT_S = 60
MAX_REVISION_FILES = 1000
MAX_REVISION_BYTES = 100 * 1024 * 1024
_KEPT_AT_TOP = ("sym-lib-table", "fp-lib-table")
_COVERED_PROPERTIES = ("Reference", "Value", "Footprint")
_REVISION_OK = re.compile(r"[^\s\x00-\x1f\x7f-]\S{0,199}")


# ------------------------------------------------------------------------------------------ symbol records


def read_symbols(tree: sheet_tree.SheetTree) -> dict[str, list[dict[str, Any]]]:
    """Per sheet file (relative name) the raw record of every symbol node on it."""
    out: dict[str, list[dict[str, Any]]] = {}
    for rel, rows in field_check._walk(tree):
        records = []
        for sym in rows:
            node = sym.node
            at = sheet_editing._at(node) if node.find_children("at") else (0.0, 0.0, 0.0)
            mirror = node.find_children("mirror")
            props = field_check._props(node)
            records.append({
                "uuid": sym.uuid, "reference": ",".join(sorted(sym.refs)), "refs": sorted(sym.refs), "unit": sym.unit, "lib_id": sym.lib_id,
                "value": props.get("Value", ""), "footprint": props.get("Footprint", ""),
                "dnp": "yes" if any(sheet_tree._atom(n, 0) == "yes" for n in node.find_children("dnp")) else "no",
                "on_board": "no" if any(sheet_tree._atom(n, 0) == "no" for n in node.find_children("on_board")) else "yes",
                "mirror": sheet_tree._atom(mirror[0], 0) if mirror else "",
                "at": (at[0], at[1]), "rotation": at[2],
                "properties": {k: v for k, v in props.items() if k not in _COVERED_PROPERTIES}, "uses": sym.uses})
        out[rel] = records
    return out


_SCALAR_FIELDS = ("reference", "unit", "lib_id", "value", "footprint", "dnp", "on_board", "mirror")


def _changes(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    out = [{"field": f, "before": before[f], "after": after[f]} for f in _SCALAR_FIELDS if before[f] != after[f]]
    if before["uuid"] != after["uuid"]:
        out.append({"field": "uuid", "before": before["uuid"], "after": after["uuid"]})
    names = sorted({*before["properties"], *after["properties"]})
    out += [{"field": f"property:{n}", "before": before["properties"].get(n), "after": after["properties"].get(n)}
            for n in names if before["properties"].get(n) != after["properties"].get(n)]
    return out


def _moved(before: dict[str, Any], after: dict[str, Any]) -> dict[str, Any] | None:
    distance = math.dist(before["at"], after["at"])
    turned = (before["rotation"] - after["rotation"]) % 360
    turned = turned if turned <= 180 else 360 - turned
    if distance <= MOVE_TOLERANCE_MM and turned <= MOVE_TOLERANCE_MM:
        return None
    return {"reference": after["reference"], "uuid": after["uuid"], "from": list(before["at"]), "to": list(after["at"]),
            "distance_mm": round(distance, 4), "rotation_from": before["rotation"], "rotation_to": after["rotation"]}


def _order(record: dict[str, Any]) -> tuple[str, str]:
    return record["reference"], record["uuid"]


def _brief(record: dict[str, Any]) -> dict[str, Any]:
    return {"reference": record["reference"], "uuid": record["uuid"], "unit": record["unit"], "lib_id": record["lib_id"],
            "value": record["value"], "footprint": record["footprint"], "at": list(record["at"])}


def _match(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> tuple[list[tuple[dict, dict]], list[dict], list[dict]]:
    """Pairs, removed, added of one sheet file: by uuid first, then by (reference, unit); a key that occurs twice on
    either side is never paired by guess."""
    pairs: list[tuple[dict, dict]] = []
    left, right = list(before), list(after)
    for key in (lambda r: r["uuid"], lambda r: (r["reference"], r["unit"])):
        count_l, count_r = Counter(key(r) for r in left), Counter(key(r) for r in right)
        by_right = {key(r): r for r in right if count_r[key(r)] == 1}
        next_left, taken = [], set()
        for rec in left:
            k = key(rec)
            if count_l[k] == 1 and k in by_right and k not in taken:
                pairs.append((rec, by_right[k]))
                taken.add(k)
            else:
                next_left.append(rec)
        left = next_left
        right = [r for r in right if not (count_r[key(r)] == 1 and key(r) in taken)]
    return pairs, left, right


def diff_symbols(before: dict[str, list[dict]], after: dict[str, list[dict]], limit: int) -> tuple[list[dict], dict[str, Any], dict[str, str]]:
    """The per sheet symbol comparison on prepared records: the sheet entries (only sheets with a change), the totals,
    and the reference translation (old reference -> new) of the matched single-use symbols whose reference changed."""
    sheets: list[dict[str, Any]] = []
    totals = {"added": 0, "removed": 0, "changed": 0, "moved": 0, "sheets_changed": 0, "sheets_unchanged": 0, "largest_move_mm": 0.0}
    ref_map: dict[str, str] = {}
    truncated: list[str] = []
    for rel in sorted({*before, *after}):
        pairs, removed, added = _match(before.get(rel, []), after.get(rel, []))
        changed, moved = [], []
        for old, new in pairs:
            if (changes := _changes(old, new)):
                changed.append({"reference": new["reference"], "uuid": new["uuid"], "before_reference": old["reference"], "changes": changes})
                if len(old["refs"]) == 1 == len(new["refs"]) and old["refs"] != new["refs"]:
                    ref_map[old["refs"][0]] = new["refs"][0]
            if (move := _moved(old, new)) is not None:
                moved.append(move)
        status = "added" if rel not in before else "removed" if rel not in after else "present"
        if not (added or removed or changed or moved) and status == "present":
            totals["sheets_unchanged"] += 1
            continue
        totals["sheets_changed"] += 1
        for key, items in (("added", added), ("removed", removed), ("changed", changed), ("moved", moved)):
            totals[key] += len(items)
        largest = max((m["distance_mm"] for m in moved), default=0.0)
        totals["largest_move_mm"] = max(totals["largest_move_mm"], largest)
        listing = {"added": sorted(map(_brief, added), key=_order), "removed": sorted(map(_brief, removed), key=_order),
                   "changed": sorted(changed, key=_order), "moved": sorted(moved, key=lambda m: (-m["distance_mm"], m["reference"], m["uuid"]))}
        entry: dict[str, Any] = {"sheet": rel, "status": status,
                                 "counts": {k: len(v) for k, v in listing.items()}, "largest_move_mm": largest}
        for key, items in listing.items():
            entry[key] = items[:limit]
            if len(items) > limit:
                truncated.append(f"{rel}:{key}")
        sheets.append(entry)
    totals["truncated_lists"] = truncated  # type: ignore[assignment]
    return sheets, totals, ref_map


# ------------------------------------------------------------------------------------------ nets


def _pins(nodes: frozenset) -> list[str]:
    return sorted(f"{r}.{p}" for r, p in nodes)


def _sheet_path(name: str) -> str:
    """The hierarchical sheet path a net name carries ("/Connectors/PT_A" -> "/Connectors/"), "" for a net with no path
    (a global label or a power net, which belongs to no one sheet)."""
    return name[: name.rfind("/") + 1] if name.startswith("/") else ""


def _auto(name: str) -> bool:
    return sheet_editing._is_auto(name)


def diff_nets(before: dict[str, frozenset], after: dict[str, frozenset], ref_map: dict[str, str], limit: int) -> dict[str, Any]:
    """The net comparison on prepared netlists (net name -> (reference, pin) members). ``ref_map`` translates the references
    of ``before`` first. A net with no members is compared by name only (it has nothing to rename it by)."""
    moved = {name: frozenset((ref_map.get(r, r), p) for r, p in nodes) for name, nodes in before.items()}
    only_before = {n: m for n, m in moved.items() if n not in after}
    only_after = {n: m for n, m in after.items() if n not in moved}
    members_after = {m: n for n, m in only_after.items() if m}
    renamed, removed, added, changed = [], [], [], []
    auto_renamed = 0
    paired: set[str] = set()
    for name, members in only_before.items():
        new = members_after.get(members) if members else None
        # a named net that now carries a generated name lost its name: that is a removal and an addition, not a rename
        if new is not None and new not in paired and not (_auto(new) and not _auto(name)):
            paired.add(new)
            if _auto(name) and _auto(new):
                auto_renamed += 1  # a generated name follows its references (Net-(R2-Pad2) -> Net-(R7-Pad2)); nobody renamed it
                continue
            renamed.append({"before": name, "after": new, "pins": _pins(members), "sheet_path": _sheet_path(new), "auto_before": _auto(name), "auto_after": _auto(new)})
        else:
            removed.append({"net": name, "pins": _pins(members), "sheet_path": _sheet_path(name), "auto": _auto(name)})
    added = [{"net": n, "pins": _pins(m), "sheet_path": _sheet_path(n), "auto": _auto(n)} for n, m in only_after.items() if n not in paired]
    for name in sorted(set(moved) & set(after)):
        if moved[name] != after[name]:
            changed.append({"net": name, "pins_added": _pins(after[name] - moved[name]), "pins_removed": _pins(moved[name] - after[name]),
                            "sheet_path": _sheet_path(name), "auto": _auto(name)})
    lists = {"added": sorted(added, key=lambda e: e["net"]), "removed": sorted(removed, key=lambda e: e["net"]),
             "renamed": sorted(renamed, key=lambda e: e["after"]), "members_changed": changed}
    per_sheet: dict[str, dict[str, int]] = {}
    for kind, items in lists.items():
        for item in items:
            per_sheet.setdefault(item["sheet_path"] or "(global)", {k: 0 for k in lists})[kind] += 1
    truncated = [kind for kind, items in lists.items() if len(items) > limit]
    return {"counts": {k: len(v) for k, v in lists.items()}, **{k: v[:limit] for k, v in lists.items()},
            "by_sheet_path": dict(sorted(per_sheet.items())), "truncated_lists": truncated,
            "nets_before": len(before), "nets_after": len(after), "auto_renamed": auto_renamed}


# ------------------------------------------------------------------------------------------ git states


class _GitError(Exception):
    pass


_REDIRECTING = ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_COMMON_DIR",
                "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES", "GIT_DISCOVERY_ACROSS_FILESYSTEM", "GIT_CONFIG", "GIT_CONFIG_PARAMETERS")


def _git_env() -> dict[str, str]:
    """The environment git runs in: the caller's, minus every variable that redirects git to another repository or object
    store or injects configuration, with the system and the user's global configuration cut off (the same approach as
    tests/_hermetic_git.py). A repository owned by another user is then refused by git's own safe.directory check and
    reported as a state that does not load."""
    env = {k: v for k, v in os.environ.items() if k not in _REDIRECTING and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env.update(GIT_OPTIONAL_LOCKS="0", GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_CONFIG_SYSTEM=os.devnull, GIT_CONFIG_COUNT="0")
    return env


def _git(cwd: Path, *args: str, stdin: bytes | None = None) -> bytes:
    env = _git_env()
    try:
        done = subprocess.run(["git", "-C", str(cwd), *args], input=stdin, capture_output=True, timeout=GIT_TIMEOUT_S, check=False, env=env)
    except FileNotFoundError as exc:
        raise _GitError("git is not installed or not on PATH") from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise _GitError(f"git {args[0]} did not complete: {exc}") from exc
    if done.returncode != 0:
        text = done.stderr.decode("utf-8", "replace").strip() or f"git {args[0]} failed"
        if "dubious ownership" in text:
            # the remedy is the owner's decision (it lowers git's ownership protection); this tool never sets it itself
            text += f"; if you trust this repository, run: git config --global --add safe.directory {cwd.as_posix()}"
        raise _GitError(text)
    return done.stdout


def _check_revision(revision: str) -> str:
    if not isinstance(revision, str) or not _REVISION_OK.fullmatch(revision):
        raise SheetAuthoringError("invalid_spec", f"revision {revision!r} is not a git revision name (no spaces or control characters, no leading '-')", revision=revision)
    return revision


def materialise(project_dir: Path, revision: str, dest: Path) -> None:
    """Rebuild the project folder ``project_dir`` as it was at ``revision`` into the empty ``dest``: every ``.kicad_sch``
    below it, and the ``.kicad_pro`` and the two library tables beside the root. Read from the object database only."""
    try:
        sha = _git(project_dir, "rev-parse", "--verify", "--quiet", f"{revision}^{{commit}}").decode().strip()
    except _GitError as exc:
        if "dubious ownership" in str(exc):
            raise
        raise _GitError(f"revision {revision!r} does not name a commit here ({exc or 'unknown revision'})") from exc
    listing = _git(project_dir, "ls-tree", "-r", "-z", "-l", sha, "--", ".").split(b"\0")
    wanted: list[tuple[str, str, int]] = []
    for line in filter(None, listing):
        meta, _, name = line.partition(b"\t")
        try:
            mode, kind, oid, size = meta.decode().split()
            listed = int(size) if size.isdigit() else 0  # a submodule entry lists "-" as its size
        except ValueError as exc:
            raise _GitError(f"git ls-tree gave a line this reader does not understand: {line[:80]!r}") from exc
        rel = name.decode("utf-8", "surrogateescape")
        # ls-tree run in a sub folder lists names relative to it (git refuses '..' and absolute names in a tree);
        # only plain files (modes 100644 and 100755) are read: a symbolic link (120000) and a submodule (160000) are skipped
        if mode not in ("100644", "100755"):
            continue
        if rel.endswith(".kicad_sch") or (rel.endswith(".kicad_pro") or rel in _KEPT_AT_TOP) and "/" not in rel:
            wanted.append((rel, oid, listed))
    if len(wanted) > MAX_REVISION_FILES:
        raise _GitError(f"revision {revision!r} holds {len(wanted)} files to read here; the limit is {MAX_REVISION_FILES}")
    if sum(size for _, _, size in wanted) > MAX_REVISION_BYTES:  # from the listing, before any content is read
        raise _GitError(f"revision {revision!r}: the sheet files exceed {MAX_REVISION_BYTES // 2**20} MB")
    out = _git(project_dir, "cat-file", "--batch", stdin="".join(f"{oid}\n" for _, oid, _ in wanted).encode())
    position = 0
    for rel, oid, listed in wanted:
        header_end = out.find(b"\n", position)
        header = out[position:header_end].decode(errors="replace").split() if header_end >= 0 else []
        if len(header) != 3 or header[1] != "blob" or not header[2].isdigit() or int(header[2]) != listed:
            raise _GitError(f"git cat-file did not return {rel} ({oid}) as listed")
        body = out[header_end + 1 : header_end + 1 + listed]
        if len(body) != listed:
            raise _GitError(f"git cat-file returned a short {rel} ({oid})")
        position = header_end + 1 + listed + 1
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body)


# ------------------------------------------------------------------------------------------ states


class _State:
    def __init__(self, label: str, source: str) -> None:
        self.label, self.source = label, source
        self.tree: sheet_tree.SheetTree | None = None
        self.root: Path | None = None  # the root sheet file the netlist is read from
        self.scratch = False
        self.problems: list[str] = []

    @property
    def loaded(self) -> bool:
        return self.tree is not None and not self.problems

    def report(self) -> dict[str, Any]:
        out: dict[str, Any] = {"label": self.label, "source": self.source, "loaded": self.loaded}
        if self.tree is not None:
            out.update(root=self.tree.rel(self.root) if self.root else "", sheets=len(self.tree.files))
        if self.problems:
            out["problems"] = self.problems
        return out


def _resolve(path: str, root_sch: str | None) -> tuple[Path, Path]:
    file = Path(path)
    if not file.is_file():
        raise SheetAuthoringError("file_not_found", f"sheet not found: {file}")
    root = sheet_authoring.find_root(file, root_sch)
    if not root.is_file():
        raise SheetAuthoringError("root_not_found", f"root sheet not found: {root}")
    return file, root


def _load(label: str, path: str, root_sch: str | None, revision: str | None, scratch_dir: Path) -> _State:
    file, root_file = _resolve(path, root_sch)
    state = _State(label, f"{file}" + (f"@{_check_revision(revision)}" if revision else ""))
    if revision is None:
        state.root = root_file
        walked = sheet_tree.walk(root_file)
    else:
        project_dir = root_file.resolve().parent
        dest = scratch_dir / label
        dest.mkdir(parents=True)
        try:
            materialise(project_dir, revision, dest)
        except _GitError as exc:
            state.problems.append(str(exc))
            return state
        state.root, state.scratch = dest / root_file.name, True
        if not state.root.is_file():
            state.problems.append(f"the root sheet {root_file.name} does not exist at revision {revision!r}")
            return state
        walked = sheet_tree.walk(state.root)
    state.tree = walked
    if not walked.files:
        state.problems.append("the root sheet cannot be loaded: " + "; ".join(walked.problems))
    elif walked.problems:
        state.problems += list(walked.problems)
    return state


def _read_nets(state: _State) -> dict[str, frozenset]:
    assert state.tree is not None and state.root is not None
    if state.scratch:
        return sheet_editing.nets_from(state.root)
    with tempfile.TemporaryDirectory(prefix="boardproof-diff-net-") as tmp:  # kicad-cli writes beside its input
        return sheet_editing.nets_from(sheet_authoring._copy_project(state.tree, [], Path(tmp) / "p"))


def _limit(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_LIMIT:
        raise SheetAuthoringError("invalid_spec", f"limit must be an integer from 1 to {MAX_LIMIT}; got {value!r}", limit=value)
    return value


def diff_schematic(
    before: str, after: str | None = None, before_revision: str | None = None, after_revision: str | None = None,
    root_sch: str | None = None, after_root_sch: str | None = None, limit: Any = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """Read-only: the symbol and net differences between two states of a schematic project."""
    try:
        cap = _limit(limit)
        same_path = after is None or Path(after).resolve() == Path(before).resolve()
        if same_path and before_revision == after_revision:
            raise SheetAuthoringError(
                "nothing_to_compare", "two states are needed: pass `after` (another path), or `before_revision`, or `after_revision` (two different ones)")
        for rev in (before_revision, after_revision):
            if rev is not None:
                _check_revision(rev)
        _resolve(before, root_sch)
        if after is not None:
            _resolve(after, after_root_sch)
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)

    with tempfile.TemporaryDirectory(prefix="boardproof-diff-schematic-") as tmp:
        try:
            states = [_load("before", before, root_sch, before_revision, Path(tmp)),
                      _load("after", after or before, after_root_sch if after is not None else root_sch, after_revision, Path(tmp))]
        except SheetAuthoringError as exc:
            return sheet_authoring.refusal(exc)
        states_report = [s.report() for s in states]
        base: dict[str, Any] = {"written": False, "states": states_report, "limit": cap, "not_compared": NOT_COMPARED}
        if not all(s.loaded for s in states):
            why = "; ".join(f"{s.label} state ({s.source}): {'; '.join(s.problems)}" for s in states if not s.loaded)
            return {"status": "incomplete", "ok": False, "compared": False, "reason": f"a state does not load, so nothing was compared: {why}",
                    "no_symbol_or_net_differences": False, **base}
        old, new = states
        assert old.tree is not None and new.tree is not None
        sheets, symbol_totals, ref_map = diff_symbols(read_symbols(old.tree), read_symbols(new.tree), cap)
        not_run: dict[str, str] = {}
        nets: dict[str, Any] | None = None
        try:
            nets = diff_nets(_read_nets(old), _read_nets(new), ref_map, cap)
        except Exception as exc:  # noqa: BLE001 - evidence-gated: a failed netlist is reported, never read as "no net changes"
            not_run["nets"] = f"kicad-cli netlist of a state did not complete: {exc}"
        complete = not not_run
        has_diff = bool(symbol_totals["sheets_changed"] or (nets and any(nets["counts"].values())))
        truncated = list(symbol_totals["truncated_lists"]) + ([f"nets:{k}" for k in nets["truncated_lists"]] if nets else [])
        return {
            "status": "ok" if complete else "incomplete", "ok": complete, "compared": True, "checks_not_run": not_run,
            **({"reason": "; ".join(not_run.values())} if not_run else {}),
            "no_symbol_or_net_differences": complete and not has_diff, "truncated": bool(truncated), "truncated_lists": truncated,
            "totals": {"symbols": {k: v for k, v in symbol_totals.items() if k != "truncated_lists"},
                       "nets": nets["counts"] if nets else None},
            "sheets": sheets, "nets": nets, "reference_changes": ref_map, **base,
        }
