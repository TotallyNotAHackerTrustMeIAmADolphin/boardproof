"""rename_net and rename_ref: a rename with every place that carries the name (GitHub issue 96).

A net name is written in several places and a reference in two per symbol; a rename that changes one
of them and not the others is a half-applied edit, and the downstream project did them with raw text
edits. Two batch tools rename in one transaction over the whole sheet tree:

- ``rename_net``: a net, named as kicad-cli's netlist names it (``VCC``, ``/SDA``, ``/Sub/SDA``).
  The places that carry the name depend on its scope, read from the name itself:
  ``/<sheet path>/<text>`` is a local label or hierarchical label with ``<text>`` on that sheet
  (the sheet path is the chain of sheet names; the root is ``/``), and a hierarchical label's
  sheet pins on the parent sheet symbols are renamed with it so the two sides stay in sync;
  a name without a leading ``/`` is a global label or a power symbol's Value, on every sheet.
- ``rename_ref``: a reference, every unit of the part on every sheet. KiCad reads the reference of
  a symbol from its instance data at the sheet use's path (``(instances (project (path .. (reference
  ..))))``); the ``Reference`` property text is kept equal to it. A sheet file that is used twice
  has one reference per use, and only the entry of the use that carries the old reference changes.

Decision 1 (docs/ARCHITECTURE.md): nothing here edits KiCad text. Every sheet of the tree is loaded
with ``write_layer.load`` into ``kicad_tools``' low-level ``SExp`` tree, changed in memory with typed
atoms (``write_layer.set_string``) and saved with ``write_layer.save`` (kicad-cli parse check,
reload-and-compare, ``os.replace``). Decision 4, the order of work for every call:

1. validate the specs and find every place to change; a name that matches nothing, a name that
   already exists (net: unless ``merge``; reference: never) and a rename that cannot be proven refuse
   the whole call before any edit;
2. change copies of the trees in memory;
3. staged check, per file: every top-level node the call did not mean to touch is unchanged; across
   the tree: no sheet pin and hierarchical label disagree that did not disagree before;
4. the changed files are saved into a scratch copy of the project and kicad-cli's netlist of it is
   compared with the netlist of the unchanged project: the netlist after must equal the netlist
   before modulo the rename (net names mapped, or references mapped), or, with ``merge``, modulo the
   rename and the one union the merge is asked to make; the components (value and every field)
   must be equal too. Any other difference is ``write_not_verified`` and nothing is written;
5. only then the real write, one file after the other, every file reloaded and compared; a failure
   restores the files already written.

Never touched: a project file, library, PCB. A net-class assignment or pattern of the project file
that names the old net and would stop matching is refused (``net_rule_orphaned``); a custom rules
file that mentions the old net name is reported as a warning. The board keeps its old names until it
is updated from the schematic.
"""

from __future__ import annotations

import fnmatch
import json
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp

from kicad_mcp import sheet_authoring, sheet_editing as se, sheet_tree, write_layer
from kicad_mcp.sheet_authoring import SheetAuthoringError

MAX_LISTED = se.MAX_LISTED
_SPEC_KEYS = {"old", "new"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f\u0085  ]")
_BUS = re.compile(r"\[.*\]|\{.*\}")


# --------------------------------------------------------------------------- the tree in memory


@dataclass
class Session:
    """The sheet tree of one call, loaded twice: ``originals`` stay as read, ``news`` are changed."""

    tree: sheet_tree.SheetTree
    originals: dict[Path, SExp]
    news: dict[Path, SExp]
    changed: dict[Path, set[str]] = field(default_factory=dict)   # file -> uuids of nodes changed in place

    def touch(self, file: Path, node: SExp) -> None:
        self.changed.setdefault(file, set()).add(se._uuid(node))


def _kids(node: SExp, tag: str) -> list[SExp]:
    return [c for c in node.children if not c.is_atom and c.name == tag]


def _specs(value: Any, what: str) -> list[tuple[str, str]]:
    if not isinstance(value, list) or not value:
        raise SheetAuthoringError("invalid_spec", f"{what} must be a non-empty list of {{old, new}}")
    out: list[tuple[str, str]] = []
    for i, item in enumerate(value):
        where = f"{what}[{i}]"
        if not isinstance(item, dict) or set(item) != _SPEC_KEYS:
            raise SheetAuthoringError("invalid_spec", f"{where} must be an object with exactly the keys old and new, got {item!r}")
        for key in ("old", "new"):
            if not isinstance(item[key], str) or not item[key]:
                raise SheetAuthoringError("invalid_spec", f"{where}.{key} must be a non-empty string, got {item[key]!r}")
            if _CONTROL.search(item[key]):
                raise SheetAuthoringError("invalid_spec", f"{where}.{key} holds a control character or line break")
            if item[key] != item[key].strip():
                raise SheetAuthoringError("invalid_spec", f"{where}.{key} has leading or trailing white space: {item[key]!r}")
        out.append((item["old"], item["new"]))
    olds = [o for o, _n in out]
    if len(set(olds)) != len(olds):
        raise SheetAuthoringError("invalid_spec", f"{what} names the same old name twice: {sorted({o for o in olds if olds.count(o) > 1})}")
    return out


def _session(tree: sheet_tree.SheetTree) -> Session:
    files = [f.resolve() for f in tree.files]
    originals = {f: write_layer.load(f) for f in files}
    for f, node in originals.items():
        if node.name != "kicad_sch":
            raise SheetAuthoringError("invalid_spec", f"{f.name} is not a schematic sheet")
    return Session(tree, originals, {f: write_layer.load(f) for f in files})


def _name_paths(session: Session) -> dict[str, str]:
    """Instance path -> the sheet-name path kicad-cli prefixes local net names with (``/`` for the root,
    ``/Sub/`` for the sheet named Sub on it): built from the sheet symbols of the tree, never guessed."""
    tree = session.tree
    file_of = {path: file.resolve() for file, path in tree.uses}
    names: dict[str, str] = {}
    for file, path in tree.uses:
        if path == tree.uses[0][1]:
            names[path] = "/"
            continue
        parent_path, _, sheet_uuid = path.rpartition("/")
        parent_file = file_of.get(parent_path)
        sheet = next((s for s in _kids(session.originals[parent_file], "sheet") if se._uuid(s) == sheet_uuid), None) if parent_file else None
        name = sheet_tree.sheet_name_property(sheet) if sheet is not None else None
        if not name or parent_path not in names:
            raise SheetAuthoringError("sheet_path_unresolved", f"cannot name the sheet use {path} ({file.name}); nothing was written")
        names[path] = f"{names[parent_path]}{name}/"
    return names


def _sheet_file(tree: sheet_tree.SheetTree, parent_file: Path, sheet: SExp) -> Path | None:
    rel = sheet_tree.sheet_file_property(sheet)[0]
    if not rel:
        return None
    target = (parent_file.parent / rel.replace("\\", "/")).resolve()
    return target if target in {f.resolve() for f in tree.files} else None


# --------------------------------------------------------------------------- the netlist oracle


def _nets_and_components(tree: sheet_tree.SheetTree, replace: dict[Path, SExp] | None) -> tuple[dict[str, frozenset], list[dict]]:
    """kicad-cli's netlist (name -> pins) and components of the project, from a scratch copy in which the
    sheets of ``replace`` hold the given trees."""
    capture: dict = {}
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-rename-") as tmp:
        scratch = Path(tmp) / "p"
        root = sheet_authoring._copy_project(tree, [], scratch)
        for file, node in (replace or {}).items():
            sheet_authoring._save(node, scratch / file.relative_to(tree.project_dir))
        nets = se.nets_from(root, capture)
    return nets, capture["components"]


def _first(items: list, limit: int = 5) -> list:
    return items[:limit]


# --------------------------------------------------------------------------- project rules


def _project_rules(tree: sheet_tree.SheetTree) -> tuple[list[str], list[tuple[str, str]], str]:
    """(net names with a class assignment, (pattern, class) pairs, text of the custom rules file) of the project
    file beside the root sheet, read only. A project file that does not parse refuses: nothing is proven then."""
    pro = tree.root.resolve().with_suffix(".kicad_pro")
    assignments: list[str] = []
    patterns: list[tuple[str, str]] = []
    if pro.is_file():
        try:
            data = json.loads(pro.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise SheetAuthoringError("project_file_unreadable", f"{pro.name} cannot be read as JSON ({exc}); the net rules cannot be checked; nothing was written") from exc
        settings = data.get("net_settings") if isinstance(data, dict) else None
        settings = settings if isinstance(settings, dict) else {}
        raw = settings.get("netclass_assignments")
        if isinstance(raw, dict):
            assignments = [str(k) for k in raw]
        elif isinstance(raw, list):
            assignments = [str(k.get("netname", k)) if isinstance(k, dict) else str(k) for k in raw]
        for entry in settings.get("netclass_patterns") or []:
            if isinstance(entry, dict) and isinstance(entry.get("pattern"), str):
                patterns.append((entry["pattern"], str(entry.get("netclass", ""))))
    dru = tree.root.resolve().with_suffix(".kicad_dru")
    dru_text = dru.read_text(encoding="utf-8", errors="replace") if dru.is_file() else ""
    return assignments, patterns, dru_text


# --------------------------------------------------------------------------- rename_net


@dataclass
class _Target:
    file: Path
    node: SExp
    kind: str        # label, global_label, hierarchical_label, sheet_pin, power_symbol
    old: str
    owner: SExp | None = None   # the top-level node that changes with this one (the sheet symbol of a pin); default: node


def _is_power(root: SExp, symbol: SExp) -> bool:
    lib = se._lib_symbol(root, symbol)
    return lib is not None and bool(lib.find_children("power"))


def _scope(net: str) -> tuple[str, str]:
    """('/Sub/', 'SDA') for ``/Sub/SDA``; ('', 'VCC') for a global name."""
    if net.startswith("/"):
        prefix, _, text = net.rpartition("/")
        return prefix + "/", text
    return "", net


def _find_targets(session: Session, names: dict[str, str], prefix: str, text: str, *, count_only: bool = False) -> list[_Target]:
    """The places that carry the name ``text`` at scope ``prefix`` ('' = global). With ``count_only`` the
    scope's files are not checked for reuse and the sheet pins are left out (the existence probe of a new name)."""
    tree = session.tree
    out: list[_Target] = []
    if prefix:
        files = {file.resolve() for file, path in tree.uses if names[path] == prefix}
        if not files:
            raise SheetAuthoringError("not_found", f"no sheet is named {prefix!r} in this project; nothing was written", sheet_path=prefix)
        for file in files:
            uses = [p for f, p in tree.uses if f.resolve() == file]
            if len(uses) > 1 and not count_only:
                raise SheetAuthoringError(
                    "sheet_used_more_than_once",
                    f"{file.name} is used by {len(uses)} sheet symbols, so a local label in it names one net per use "
                    f"({', '.join(names[p] + text for p in uses)}) and one rename cannot change only one of them; nothing was written",
                    uses=[names[p] for p in uses])
            root = session.news[file]
            out += [_Target(file, n, "label", text) for n in _kids(root, "label") if n.get_string(0) == text]
            hier = [n for n in _kids(root, "hierarchical_label") if n.get_string(0) == text]
            out += [_Target(file, n, "hierarchical_label", text) for n in hier]
            if hier and not count_only:
                for parent in session.news:
                    for sheet in _kids(session.news[parent], "sheet"):
                        if _sheet_file(tree, parent, sheet) == file:
                            out += [_Target(parent, p, "sheet_pin", text, sheet) for p in _kids(sheet, "pin") if p.get_string(0) == text]
        return out
    for file, root in session.news.items():
        out += [_Target(file, n, "global_label", text) for n in _kids(root, "global_label") if n.get_string(0) == text]
        for symbol in _kids(root, "symbol"):
            value = se._property(symbol, "Value")
            if value is not None and value.get_string(1) == text and _is_power(root, symbol):
                out.append(_Target(file, symbol, "power_symbol", text))
    return out


def _check_net_text(new: str, where: str) -> None:
    if "/" in new:
        raise SheetAuthoringError("invalid_spec", f"{where} {new!r} holds a slash; give the new name without a sheet path (the scope of the net stays what it is)")
    if _BUS.search(new):
        raise SheetAuthoringError("bus_name_unsupported", f"{where} {new!r} looks like a bus name; buses are not renamed by this tool; nothing was written")


def _interface_problems(session: Session, roots: dict[Path, SExp]) -> set[str]:
    out: set[str] = set()
    for parent, root in roots.items():
        for sheet in _kids(root, "sheet"):
            child = _sheet_file(session.tree, parent, sheet)
            rel = sheet_tree.sheet_file_property(sheet)[0]
            if child is not None and rel:
                out.update(f"{session.tree.rel(parent)}: {p}" for p in sheet_authoring.interface_mismatches(root, roots[child], rel))
    return out


def _staged_files(session: Session) -> None:
    for file, uuids in session.changed.items():
        se._staged_check(session.originals[file], session.news[file], se.Outcome(changed=set(uuids)))
    for file in session.news:
        if file not in session.changed and write_layer.serialise(session.news[file], compact=True) != write_layer.serialise(session.originals[file], compact=True):
            raise SheetAuthoringError("write_not_verified", f"{session.tree.rel(file)} changed but the call did not select it; nothing was written")
    fresh = sorted(_interface_problems(session, session.news) - _interface_problems(session, session.originals))
    if fresh:
        raise SheetAuthoringError("write_not_verified", "sheet pins and hierarchical labels disagree after the change: " + "; ".join(_first(fresh)) + "; nothing was written",
                                  interface_problems=fresh[:MAX_LISTED])


def _plan_nets(session: Session, specs: list[tuple[str, str]], merge: bool, allow_orphaned: bool, before: dict[str, frozenset]) -> dict[str, Any]:
    names = _name_paths(session)
    olds = {o for o, _n in specs}
    assignments, patterns, dru_text = _project_rules(session.tree)
    mapping: dict[str, str] = {}
    rows: list[dict[str, Any]] = []
    orphaned: list[dict[str, str]] = []
    warnings: list[dict[str, Any]] = []
    seen_nodes: set[int] = set()
    for i, (old, new) in enumerate(specs):
        where = f"renames[{i}]"
        _check_net_text(new, f"{where}.new")
        if old not in before:
            near = sorted(n for n in before if n.rsplit("/", 1)[-1].lower() == old.rsplit("/", 1)[-1].lower())[:MAX_LISTED]
            raise SheetAuthoringError("not_found", f"{where}: the netlist has no net named {old!r} (nets are named as kicad-cli lists them, for example /SDA, /Sub/SDA or VCC); nothing was written",
                                      nets_named_alike=near)
        if se._is_auto(old):
            raise SheetAuthoringError("net_not_named", f"{where}: {old!r} is an automatic net name (made from a pin), not carried by a label; name it by adding a label; nothing was written")
        prefix, text = _scope(old)
        if not text:
            raise SheetAuthoringError("invalid_spec", f"{where}.old {old!r} has no name after the sheet path")
        new_net = prefix + new
        mapping[old] = new_net
        if old == new_net:
            rows.append({"old": old, "new": new_net, "unchanged": True, "targets": []})
            continue
        targets = _find_targets(session, names, prefix, text)
        if not [t for t in targets if t.kind != "sheet_pin"]:
            raise SheetAuthoringError(
                "no_label_carries_net",
                f"{where}: no label, hierarchical label, global label or power symbol with the text {text!r} carries {old!r} "
                "(a net can be named by a pin: a hidden power pin, or a pin of the same name tied by KiCad); renaming it would need the symbol changed; nothing was written")
        renamed_away = new_net in olds
        exists_as = new_net in before and not renamed_away
        probe = _find_targets(session, names, prefix, new, count_only=True)
        merging = exists_as or (bool(probe) and not renamed_away)
        if merging and not merge:
            raise SheetAuthoringError(
                "name_exists",
                f"{where}: the net {new_net!r} already exists"
                + (f" (a label or power symbol with the text {new!r} is on the sheet)" if probe and not exists_as else "")
                + f"; renaming {old!r} to it would join the two nets, which changes connectivity. Pass merge true if the join is meant; nothing was written",
                existing=new_net, pins_on_existing=sorted(f"{r}.{p}" for r, p in before.get(new_net, ())))
        for t in targets:
            if id(t.node) in seen_nodes:
                raise SheetAuthoringError("invalid_spec", f"{where}: {t.kind} {se._uuid(t.node)} is already renamed by an earlier item")
            seen_nodes.add(id(t.node))
        for pattern, klass in patterns:
            if fnmatch.fnmatchcase(old, pattern) and not fnmatch.fnmatchcase(new_net, pattern):
                orphaned.append({"rule": "netclass_pattern", "pattern": pattern, "netclass": klass, "net": old})
        if old in assignments and new_net not in assignments:
            orphaned.append({"rule": "netclass_assignment", "net": old})
        if dru_text and old in dru_text:
            warnings.append({"code": "rules_file_mentions_net", "net": old, "note": f"the custom rules file ({session.tree.root.with_suffix('.kicad_dru').name}) mentions {old!r}; check it after the rename"})
        rows.append({"old": old, "new": new_net, "merge": merging, "targets": targets, "pins_joined": sorted(f"{r}.{p}" for r, p in before.get(new_net, ())) if merging else []})
    if orphaned and not allow_orphaned:
        raise SheetAuthoringError(
            "net_rule_orphaned",
            f"the project file has {len(orphaned)} net-class rule(s) that name the old net and would stop matching the new name: {orphaned[0]}; "
            "this tool does not edit the project file. Change the rule first, or repeat with allow_orphaned_net_rules true to rename anyway and leave the rule orphaned; nothing was written",
            orphaned_rules=orphaned[:MAX_LISTED])
    if orphaned:
        warnings.append({"code": "net_rule_orphaned", "rules": orphaned[:MAX_LISTED], "note": "these rules no longer match; fix them in KiCad's board setup"})
    # apply: every target is a node of the in-memory copy, picked before any change
    for row in rows:
        listed = []
        for t in row["targets"]:
            text, new_text = _scope(row["old"])[1], _scope(row["new"])[1]
            node = t.node if t.kind != "power_symbol" else se._property(t.node, "Value")
            index = 1 if t.kind == "power_symbol" else 0
            try:
                write_layer.set_string(node, index, new_text)
            except write_layer.InvalidStringError as exc:
                raise SheetAuthoringError("invalid_spec", f"the name {new_text!r} cannot be written: {exc}") from exc
            session.touch(t.file, t.owner or t.node)
            listed.append({"sheet": session.tree.rel(t.file), "kind": t.kind, "uuid": se._uuid(t.node), "from": text, "to": new_text})
        row["targets"] = listed
    return {"rows": rows, "mapping": mapping, "warnings": warnings}


def _expected_nets(before: dict[str, frozenset], mapping: dict[str, str]) -> dict[str, frozenset]:
    out: dict[str, frozenset] = {}
    for name, nodes in before.items():
        key = mapping.get(name, name)
        out[key] = out.get(key, frozenset()) | nodes
    return out


def _net_differences(expected: dict[str, frozenset], after: dict[str, frozenset]) -> list[str]:
    out = []
    for name in sorted(set(expected) | set(after)):
        if name not in after:
            out.append(f"net {name!r} should exist with {sorted(f'{r}.{p}' for r, p in expected[name])} and is gone")
        elif name not in expected:
            out.append(f"net {name!r} with {sorted(f'{r}.{p}' for r, p in after[name])} is new")
        elif expected[name] != after[name]:
            out.append(f"net {name!r} should hold {sorted(f'{r}.{p}' for r, p in expected[name])} and holds {sorted(f'{r}.{p}' for r, p in after[name])}")
    return out


def _comp_table(components: list[dict]) -> dict[str, tuple]:
    return {c["ref"]: (c["value"], tuple(sorted(c["fields"].items()))) for c in components}


# --------------------------------------------------------------------------- the shared run


def _run(sch_path: str, root_sch: str | None, dry_run: Any, plan) -> dict[str, Any]:
    path = Path(sch_path)
    if not isinstance(dry_run, bool):
        raise SheetAuthoringError("invalid_spec", "dry_run must be true or false")
    if not path.is_file():
        raise SheetAuthoringError("file_not_found", f"sheet not found: {path}")
    root = sheet_authoring.find_root(path, root_sch)
    tree = sheet_authoring._require_tree(root)
    files = sorted({f.resolve() for f in tree.files})
    if path.resolve() not in files:
        raise SheetAuthoringError("parent_not_in_tree", f"{path.name} is not reached from the root sheet {root.name}", **tree.report())
    with write_layer.path_lock(files):
        session = _session(tree)
        before_nets, before_components = _nets_and_components(tree, None)
        report = plan(session, before_nets, before_components)
        _staged_files(session)
        changed_files = sorted(session.changed, key=lambda f: tree.rel(f))
        if not dry_run:
            write_layer.check_writable(*changed_files)
        report["verify"](session, changed_files, before_nets, before_components)
        result: dict[str, Any] = {"status": "ok", "ok": True, "written": False, "dry_run": dry_run, **report["result"]}
        result["files_changed"] = [tree.rel(f) for f in changed_files]
        if dry_run or not changed_files:
            if not changed_files:
                result["note"] = "nothing to change; nothing was written"
            return result
        written: list[Path] = []
        try:
            for file in changed_files:
                write_layer.save(session.news[file], file, verify=se._expected_verify(session.news[file]))
                written.append(file)
            for file in changed_files:
                if write_layer.serialise(write_layer.load(file), compact=True) != write_layer.serialise(session.news[file], compact=True):
                    raise SheetAuthoringError("write_not_verified", f"check reload_equals_serialised: {file.name} does not read back as the edited tree; the files written so far are being restored")
        except BaseException as failure:
            not_restored: list[str] = []
            for file in written:
                try:
                    write_layer.save(session.originals[file], file)
                except Exception:  # noqa: BLE001 - reported below, never swallowed
                    not_restored.append(tree.rel(file))
            if not_restored:
                raise SheetAuthoringError(
                    "write_not_verified",
                    f"the write failed ({failure}) and these files could NOT be restored and now hold the new names: {', '.join(not_restored)}; "
                    "the other written files were restored to the same content (not necessarily the same bytes)",
                    restore_failed=not_restored) from failure
            raise
        result["written"] = True
        return result


def _guarded(sch_path: str, root_sch: str | None, dry_run: Any, plan) -> dict[str, Any]:
    try:
        return _run(sch_path, root_sch, dry_run, plan)
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)
    except write_layer.WriteLayerError as exc:
        if (locked := write_layer.as_lock_refusal(exc)) is not None:
            return locked
        return {"status": "incomplete", "ok": False, "code": exc.code, "reason": str(exc), **exc.details}


def rename_net(sch_path: str, renames: list[dict], root_sch: str | None = None, *, merge: bool = False,
               allow_orphaned_net_rules: bool = False, dry_run: bool = False) -> dict[str, Any]:
    """Rename nets: every label, hierarchical label with its sheet pins, global label and power symbol value that names them."""

    def plan(session: Session, before: dict[str, frozenset], before_components: list[dict]) -> dict[str, Any]:
        if not isinstance(merge, bool) or not isinstance(allow_orphaned_net_rules, bool):
            raise SheetAuthoringError("invalid_spec", "merge and allow_orphaned_net_rules must be true or false")
        specs = _specs(renames, "renames")
        planned = _plan_nets(session, specs, merge, allow_orphaned_net_rules, before)
        mapping = {o: n for o, n in planned["mapping"].items() if o != n}

        def verify(sess: Session, changed: list[Path], b_nets: dict[str, frozenset], b_comp: list[dict]) -> None:
            if not changed:
                return
            after, after_comp = _nets_and_components(sess.tree, {f: sess.news[f] for f in changed})
            problems = _net_differences(_expected_nets(b_nets, mapping), after)
            if _comp_table(b_comp) != _comp_table(after_comp):
                problems.append("the components (reference, value, fields) of the netlist changed")
            if problems:
                raise SheetAuthoringError(
                    "write_not_verified",
                    "kicad-cli's netlist of the result is not the netlist before with the nets renamed: " + "; ".join(_first(problems))
                    + "; nothing was written (a net can also be tied by a pin of the same name, which a rename of labels does not change)",
                    differences=problems[:MAX_LISTED])

        rows = planned["rows"]
        merges = [{"from": r["old"], "into": r["new"], "pins_joined": r["pins_joined"]} for r in rows if r.get("merge")]
        count = sum(len(r["targets"]) for r in rows)
        body: dict[str, Any] = {"renamed": rows, "count": count}
        if merges:
            body["connectivity"] = {"changed": True, "merged": merges,
                                    "note": "merge: the netlist after joins these nets on purpose; every other net is unchanged"}
        else:
            body["connectivity"] = {"changed": False, "note": "the netlist after equals the netlist before with the nets renamed"}
        body["connectivity"]["nets_before"] = len(before)
        if planned["warnings"]:
            body["warnings"] = planned["warnings"]
        body["note"] = "the board keeps its old net names until it is updated from the schematic"
        return {"result": body, "verify": verify}

    result = _guarded(sch_path, root_sch, dry_run, plan)
    if result.get("ok") and dry_run:
        result["would_rename"] = result.pop("renamed")
    return result


# --------------------------------------------------------------------------- rename_ref


def _check_ref_text(new: str, where: str) -> None:
    if "?" in new or new.startswith("#") or '"' in new or any(ch.isspace() for ch in new):
        raise SheetAuthoringError(
            "invalid_spec", f"{where}.new {new!r} is not a usable reference (no white space, quote or question mark, and no leading # which marks a power symbol)")


def _instance_entry(symbol: SExp, use_path: str) -> SExp | None:
    """The one ``(path ..)`` instance entry of ``symbol`` at the sheet use's path, compared as kicad-cli compares paths."""
    use_key = sheet_tree.path_key(use_path)
    found = [e for inst in _kids(symbol, "instances") for proj in _kids(inst, "project") for e in _kids(proj, "path")
             if use_key is not None and sheet_tree.path_key(e.get_string(0) or "") == use_key]
    return found[0] if len(found) == 1 else None


def rename_ref(sch_path: str, renames: list[dict], root_sch: str | None = None, *, dry_run: bool = False) -> dict[str, Any]:
    """Rename references: every unit of the part, on every sheet, in the instance data and the Reference property."""

    def plan(session: Session, before: dict[str, frozenset], before_components: list[dict]) -> dict[str, Any]:
        specs = _specs(renames, "renames")
        tree = session.tree
        project = tree.root.stem
        records = sheet_tree.placed_instances(tree, project)
        by_rel = {tree.rel(f): f.resolve() for f in tree.files}
        olds = {o for o, _n in specs}
        holders: dict[str, list[dict]] = {}
        for rec in records:
            holders.setdefault(rec["ref"], []).append(rec)
        news = [n for _o, n in specs]
        if len(set(news)) != len(news):
            raise SheetAuthoringError("invalid_spec", f"renames gives the same new reference to two parts: {sorted({n for n in news if news.count(n) > 1})}")
        rows: list[dict[str, Any]] = []
        mapping: dict[str, str] = {}
        for i, (old, new) in enumerate(specs):
            where = f"renames[{i}]"
            _check_ref_text(new, f"{where}")
            if old.endswith("?"):
                raise SheetAuthoringError("invalid_spec", f"{where}.old {old!r} is an unannotated reference (it ends in ?); run fix_annotation first; nothing was written")
            if old.startswith("#"):
                raise SheetAuthoringError("invalid_spec", f"{where}.old {old!r} is a power symbol or flag, not a part; nothing was written")
            mine = holders.get(old, [])
            if not mine:
                raise SheetAuthoringError("not_found", f"{where}: no placed symbol has the reference {old!r}; nothing was written",
                                          references=sorted({r['ref'] for r in records if not r['ref'].startswith('#')})[:MAX_LISTED])
            if old == new:
                rows.append({"old": old, "new": new, "unchanged": True, "units": []})
                continue
            if new in holders and new not in olds:
                raise SheetAuthoringError(
                    "reference_exists", f"{where}: {new!r} is already the reference of {sorted({r['sheet'] for r in holders[new]})[0]} (uuid {holders[new][0]['uuid']}); "
                    "two parts cannot share a reference and a rename never merges parts; nothing was written",
                    held_by=[{"sheet": r["sheet"], "uuid": r["uuid"], "unit": r["unit"]} for r in holders[new]][:MAX_LISTED])
            mapping[old] = new
            units = []
            for rec in mine:
                if rec["unproven_paths"] or rec["live_entries"] or rec["stale_only"] == "yes":
                    raise SheetAuthoringError(
                        "instance_data_unproven",
                        f"{where}: the instance data of {old!r} on {rec['sheet']} cannot be proven to be the entry KiCad reads "
                        f"({rec['unproven_paths'] or rec['live_entries'] or 'only an entry of a path no sheet use has'}); nothing was written",
                        sheet=rec["sheet"], uuid=rec["uuid"])
                file = by_rel[rec["sheet"]]
                symbol = next((s for s in _kids(session.news[file], "symbol") if se._uuid(s) == rec["uuid"]), None)
                entry = _instance_entry(symbol, rec["path"]) if symbol is not None else None
                refs = entry.find_children("reference") if entry is not None else []
                if entry is None or not refs:
                    raise SheetAuthoringError(
                        "instance_data_unproven",
                        f"{where}: {old!r} on {rec['sheet']} has no single (reference ..) instance entry at its sheet use (a KiCad 6 sheet or a bare Reference property); nothing was written",
                        sheet=rec["sheet"], uuid=rec["uuid"])
                prop = se._property(symbol, "Reference")
                write_layer.set_string(refs[0], 0, new)
                prop_text = prop.get_string(1) if prop is not None else None
                if prop is not None and prop_text == old:
                    write_layer.set_string(prop, 1, new)
                session.touch(file, symbol)
                units.append({"sheet": rec["sheet"], "uuid": rec["uuid"], "unit": rec["unit"], "lib_id": rec["lib_id"], "sheet_use": rec["path"],
                              "reference_property": "renamed" if prop is not None and prop_text == old else (f"kept ({prop_text!r})" if prop is not None else "absent")})
            rows.append({"old": old, "new": new, "units": units})

        def verify(sess: Session, changed: list[Path], b_nets: dict[str, frozenset], b_comp: list[dict]) -> None:
            if not changed:
                return
            after, after_comp = _nets_and_components(sess.tree, {f: sess.news[f] for f in changed})

            def sig(nets: dict[str, frozenset], refmap: dict[str, str]) -> set:
                return {(frozenset((refmap.get(r, r), p) for r, p in nodes), None if se._is_auto(name) else name) for name, nodes in nets.items()}

            problems = []
            if sig(b_nets, mapping) != sig(after, {}):
                problems.append("a net holds other pins than before the rename")
            expected = {mapping.get(c["ref"], c["ref"]): (c["value"], tuple(sorted(c["fields"].items()))) for c in b_comp}
            if len(expected) != len(b_comp) or expected != _comp_table(after_comp):
                problems.append("the components (reference, value, fields) of the netlist are not the components before with the references renamed")
            if problems:
                raise SheetAuthoringError(
                    "write_not_verified",
                    "kicad-cli's netlist of the result is not the netlist before with the references renamed: " + "; ".join(problems) + "; nothing was written",
                    differences=problems)

        body: dict[str, Any] = {"renamed": rows, "count": sum(len(r["units"]) for r in rows),
                                "connectivity": {"changed": False, "nets_before": len(before),
                                                 "note": "the netlist after equals the netlist before with the references renamed"},
                                "note": "the board keeps its old references until it is updated from the schematic"}
        return {"result": body, "verify": verify}

    result = _guarded(sch_path, root_sch, dry_run, plan)
    if result.get("ok") and dry_run:
        result["would_rename"] = result.pop("renamed")
    return result
