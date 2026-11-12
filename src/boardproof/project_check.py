"""check_project: connectivity and ERC over the whole sheet tree in one call (GitHub issue 129).

A thin layer over the two engines that already exist: ``check_connectivity`` (the raw reading of every
sheet of the tree, passed in as ``connectivity``) and one ``kicad-cli sch erc`` run on the project's
ROOT sheet (``kicad_cli.run_erc``; the JSON report lists every sheet use of the hierarchy under
``sheets``, a sheet without a finding with an empty ``violations`` list, measured with kicad-cli
10.0.7). The result is grouped per sheet file, with counts per sheet and in total.

Nothing is dropped silently: a sheet that cannot be loaded is listed (``sheet_problems``,
``covered`` false, ``status`` incomplete); a finding that cannot be attributed to a sheet file is
listed apart (``connectivity_unattributed``, ``erc_unmapped``) and counted in the totals; a sheet use
that ERC did not report is marked ``reported: false``. Read-only: writes nothing, takes no lock.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import tempfile
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from boardproof import sheet_authoring, sheet_extents, sheet_tree, write_layer
from boardproof.sheet_authoring import SheetAuthoringError

CHECKS = ("connectivity", "erc")
_CONNECTIVITY_LISTS = ("unconnected_pins", "no_connect_pins", "connection_issues", "missing_from_netlist")


def _checks(value: Any) -> list[str]:
    if value is None:
        return list(CHECKS)
    if not isinstance(value, list) or not value or any(not isinstance(v, str) for v in value):
        raise SheetAuthoringError("invalid_spec", f"checks must be a non-empty list drawn from {list(CHECKS)}", checks=value)
    bad = [v for v in value if v not in CHECKS]
    if bad or len(set(value)) != len(value):
        raise SheetAuthoringError("invalid_spec", f"checks must be distinct names drawn from {list(CHECKS)}; got {value}", checks=value)
    return [c for c in CHECKS if c in value]


MAX_LISTED = 50


def page_note(path: Path) -> dict[str, Any]:
    """The out-of-page note of one sheet file (issue 198): the objects whose box is not wholly inside the sheet's own paper,
    measured by ``sheet_extents`` from the raw tokens (a warning, never an error). ``checked`` is false, with a ``reason``, when the
    file cannot be read, its paper is not one KiCad reads, or an object cannot be measured at all (listed under ``unmeasured``):
    such a sheet is never read as inside its page."""
    note: dict[str, Any] = {"severity": "warning", "not_measured": list(sheet_extents.NOT_MEASURED)}
    try:
        root = write_layer.load(path)
        papers = root.find_children("paper")
        page = sheet_extents.page_size_mm(papers[0]) if papers else None
        entries = sheet_extents.sheet_extents(root)
    except Exception as exc:  # noqa: BLE001 - an unreadable sheet is reported, never read as inside its page
        return {**note, "checked": False, "page_mm": None, "count": 0, "reason": f"the sheet could not be read: {exc}"}
    if page is None:
        return {**note, "checked": False, "page_mm": None, "count": 0, "reason": "the paper token of the sheet is missing or is not one KiCad reads"}
    outside = sheet_extents.outside_page(entries, page)
    unknown = [e for e in entries if e["measured"] == "none"]
    note.update(
        page_mm=[page[0], page[1]], checked=not unknown, count=len(outside), by_kind=dict(sorted(Counter(e["kind"] for e in outside).items())),
        listed=[{k: e[k] for k in ("kind", "uuid", "label", "x1", "y1", "x2", "y2")} for e in outside[:MAX_LISTED]], truncated=len(outside) > MAX_LISTED)
    if unknown:
        note["reason"] = f"{len(unknown)} object(s) cannot be measured, so the page check of this sheet is incomplete"
        note["unmeasured"] = [{"kind": e["kind"], "uuid": e["uuid"], "label": e["label"]} for e in unknown[:MAX_LISTED]]
    return note


def _item_sheet(item: Any) -> str:
    return str(item.get("sheet", "") if isinstance(item, dict) else getattr(item, "sheet", ""))


def _group_connectivity(result: dict[str, Any], tree: Any, root_rel: str) -> tuple[dict[str, dict[str, list]], dict[str, list]]:
    """The engine's lists split per sheet file. The engine names a sheet by "" (the root) or its file's
    base name; a base name shared by two files, or one no file has, is not guessed: it goes to the
    unattributed lists."""
    rels = [tree.rel(f) for f in tree.files]
    by_name: dict[str, list[str]] = {}
    for rel in rels:
        by_name.setdefault(Path(rel).name, []).append(rel)
    grouped: dict[str, dict[str, list]] = {rel: {key: [] for key in _CONNECTIVITY_LISTS} for rel in rels}
    unattributed: dict[str, list] = {key: [] for key in _CONNECTIVITY_LISTS}
    for key in _CONNECTIVITY_LISTS:
        for item in result.get(key, []):
            name = _item_sheet(item)
            targets = [root_rel] if name == "" else by_name.get(name, [])
            if len(targets) == 1:
                grouped[targets[0]][key].append(item)
            else:
                unattributed[key].append(item)
    return grouped, unattributed


def _severities(violations: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(str(v.get("severity", "?")) for v in violations).items()))


def check_project(sch_path: str, root_sch: str | None, checks: Any, connectivity: Callable[[str], dict]) -> dict[str, Any]:
    """Read-only: connectivity and ERC findings of every sheet of the project, grouped per sheet file."""
    try:
        wanted = _checks(checks)
        file = Path(sch_path)
        if not file.is_file():
            raise SheetAuthoringError("file_not_found", f"sheet not found: {file}")
        root_file = sheet_authoring.find_root(file, root_sch)
        if not root_file.is_file():
            raise SheetAuthoringError("root_not_found", f"root sheet not found: {root_file}")
        tree = sheet_tree.walk(root_file)
        if not tree.files:
            raise SheetAuthoringError(
                "sheet_tree_incomplete", f"the root sheet {root_file.name} cannot be loaded: " + "; ".join(tree.problems), **tree.report())
        if not tree.problems and file.resolve() not in {f.resolve() for f in tree.files}:
            raise SheetAuthoringError("parent_not_in_tree", f"{file.name} is not reached from the root sheet {root_file.name}", **tree.report())
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)

    # Both engines write beside the schematic they are given (the connectivity engine a netlist, kicad-cli ERC a
    # .kicad_prl), so both run on a scratch copy of the tree and the project folder is never touched (issue 129 review).
    with tempfile.TemporaryDirectory(prefix="boardproof-check-project-") as scratch:
        scratch_root = sheet_authoring._copy_project(tree, [], Path(scratch))
        library_problems = _copy_project_libraries(tree.project_dir, Path(scratch))
        return _check_tree(wanted, root_file, scratch_root, tree, connectivity, library_problems)


_LIB_TABLES = ("sym-lib-table", "fp-lib-table")
# KiCad reads the uri quoted or not, and the project variable as ${KIPRJMOD} or $(KIPRJMOD)
_URI = re.compile(r'\(uri\s+(?:"([^"]*)"|((?:\$\(\w+\)|[^\s()"])+))')
_KIPRJMOD_PREFIX = re.compile(r"^\$[({]KIPRJMOD[)}]")
_LIB_MAX_FILES = 2000
_LIB_MAX_DIRS = 2000
_LIB_MAX_BYTES = 200 * 1024 * 1024


def _is_junction(path: Path) -> bool:
    """A Windows junction. ``os.path.isjunction`` exists from Python 3.12 only (pyproject allows 3.11): there, a
    mount-point reparse tag that is not a symlink is the same test (as in scripts/check_publishable.py)."""
    isjunction = getattr(os.path, "isjunction", None)
    if isjunction is not None:
        return bool(isjunction(path))
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (
        not stat.S_ISLNK(info.st_mode)
        and getattr(info, "st_reparse_tag", 0) == getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", -1)
    )


def _is_link(path: Path) -> bool:
    return path.is_symlink() or _is_junction(path)


def _inside(path: Path, base: Path) -> bool:
    try:
        path.resolve().relative_to(base)
    except (ValueError, OSError):
        return False
    return True


def _copy_project_libraries(project_dir: Path, scratch: Path) -> list[str]:
    """The copy below, with a file system error (an unreadable library table, a stat or resolve failing on a broken
    entry) turned into a problem: the result is then incomplete, never a crash."""
    problems: list[str] = []
    try:
        _copy_libraries(project_dir, scratch, problems)
    except OSError as exc:
        problems.append(f"the project libraries could not be read: {exc}")
    return problems


def _copy_libraries(project_dir: Path, scratch: Path, problems: list[str]) -> None:
    """Copy what the project's two library tables name under ``${KIPRJMOD}`` (also ``$(KIPRJMOD)``, the uri quoted or
    not) into the scratch copy, at the same relative place, so ERC on the copy sees the project-local libraries (without
    them it adds a lib_symbol_issues warning that the real folder does not have, measured with kicad-cli 10.0.7).
    Bounded in files and bytes, counted while walking. Returned as problems (the result is then not ``covered``) and
    not copied: a row that leaves the project folder or names the variable anywhere but at its start, a symbolic link
    or Windows junction below the row (never followed: it can lead out of the project or back into it), and a row that
    would pass the bound of files, bytes or directories (then no further row is read). Rows with any other path (absolute, KiCad's own folders) are the
    same on the copy and are left alone."""
    base = project_dir.resolve()
    files = size = dir_count = 0
    for table in _LIB_TABLES:
        table_file = project_dir / table
        if not table_file.is_file():
            continue
        for quoted, bare in _URI.findall(table_file.read_text(encoding="utf-8", errors="replace")):
            uri = quoted or bare
            prefix = _KIPRJMOD_PREFIX.match(uri)
            if prefix is None and "KIPRJMOD" not in uri:
                continue
            if prefix is None:
                problems.append(f"{table}: {uri} names KIPRJMOD in a form this check does not read; not copied")
                continue
            rest = uri[prefix.end():].lstrip("/\\")
            if "KIPRJMOD" in rest:
                problems.append(f"{table}: {uri} names KIPRJMOD in a form this check does not read; not copied")
                continue
            source = (base / rest).resolve()
            if not _inside(source, base):
                problems.append(f"{table}: {uri} leaves the project folder; not copied")
                continue
            if not source.exists():
                continue  # the real folder lacks it too: ERC reports the same on the copy
            members: list[Path] = []
            if source.is_file():
                walk: list[tuple[str, list[str], list[str]]] = [(str(source.parent), [], [source.name])]
            else:
                walk = os.walk(source, followlinks=False)  # type: ignore[assignment]
            for current, dirs, names in walk:
                dir_count += len(dirs)
                if dir_count > _LIB_MAX_DIRS:
                    problems.append(f"{table}: the libraries under ${{KIPRJMOD}} exceed {_LIB_MAX_DIRS} directories; {uri} not copied")
                    return
                kept = []
                for name in dirs:
                    if _is_link(Path(current) / name) or not _inside(Path(current) / name, base):
                        problems.append(f"{table}: {uri}: {(Path(current) / name).relative_to(base).as_posix()} is a link; not followed or copied")
                    else:
                        kept.append(name)
                dirs[:] = kept
                for name in names:
                    member = Path(current) / name
                    # a file link (symlink) is caught here; its target outside the project is never read
                    if _is_link(member):
                        problems.append(f"{table}: {uri}: {member.relative_to(base).as_posix()} is a link; not copied")
                        continue
                    files += 1
                    size += member.stat().st_size
                    if files > _LIB_MAX_FILES or size > _LIB_MAX_BYTES:
                        problems.append(f"{table}: the libraries under ${{KIPRJMOD}} exceed {_LIB_MAX_FILES} files or {_LIB_MAX_BYTES // 2**20} MB; {uri} not copied")
                        return
                    members.append(member)
            for member in members:
                dest = scratch / member.relative_to(base)
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(member, dest)


def _check_tree(wanted: list[str], root_file: Path, scratch_root: Path, tree: Any, connectivity: Callable[[str], dict], library_problems: list[str]) -> dict[str, Any]:
    root_rel = tree.rel(root_file)
    rels = [tree.rel(f) for f in tree.files]
    uses_by_path = {path: tree.rel(f) for f, path in tree.uses}
    use_count = Counter(tree.rel(f) for f, _ in tree.uses)
    sheets: dict[str, dict[str, Any]] = {rel: {"sheet": rel, "uses": use_count.get(rel, 1)} for rel in rels}
    not_run: dict[str, str] = {}
    unattributed: dict[str, list] = {key: [] for key in _CONNECTIVITY_LISTS}
    erc_unmapped: list[dict[str, Any]] = []
    totals: dict[str, Any] = {"sheets": len(rels)}

    if "connectivity" in wanted:
        if tree.problems:
            not_run["connectivity"] = "the sheet tree is incomplete (" + "; ".join(tree.problems) + "); the connectivity engine refuses such a project"
        else:
            result = connectivity(str(scratch_root))
            if result.get("status") != "ok":
                not_run["connectivity"] = str(result.get("reason", "the connectivity check did not complete"))
            else:
                grouped, unattributed = _group_connectivity(result, tree, root_rel)
                for rel, lists in grouped.items():
                    sheets[rel]["connectivity"] = {
                        **lists, "unconnected_count": len(lists["unconnected_pins"]), "no_connect_count": len(lists["no_connect_pins"]),
                        "connection_issue_count": len(lists["connection_issues"]), "missing_from_netlist_count": len(lists["missing_from_netlist"])}
                totals.update({
                    "unconnected_pins": len(result.get("unconnected_pins", [])), "no_connect_pins": len(result.get("no_connect_pins", [])),
                    "connection_issues": len(result.get("connection_issues", [])), "missing_from_netlist": len(result.get("missing_from_netlist", []))})

    page_unchecked = 0
    page_total = 0
    for file in tree.files:
        note = page_note(file)
        sheets[tree.rel(file)]["outside_page"] = note
        page_total += note["count"]
        page_unchecked += 0 if note["checked"] else 1
    totals["outside_page"] = page_total
    if page_unchecked:
        totals["outside_page_unchecked_sheets"] = page_unchecked

    erc_unreported: list[str] = []
    if "erc" in wanted:
        from boardproof import kicad_cli

        try:
            report = kicad_cli.run_erc(scratch_root)
        except Exception as exc:  # noqa: BLE001 - evidence-gated: a failed run is reported, never read as clean
            not_run["erc"] = f"kicad-cli ERC did not complete: {exc}"
            report = None
        if report is not None:
            per_sheet: dict[str, list[dict[str, Any]]] = {rel: [] for rel in rels}
            reported_paths: set[str] = set()
            for entry in report.get("sheets", []):
                violations = [{**v, "sheet_path": entry.get("path", "")} for v in entry.get("violations", [])]
                rel = uses_by_path.get(entry.get("uuid_path", ""))
                if rel is None:
                    erc_unmapped.append({"path": entry.get("path", ""), "uuid_path": entry.get("uuid_path", ""), "violations": violations})
                else:
                    reported_paths.add(entry.get("uuid_path", ""))
                    per_sheet[rel] += violations
            erc_unreported = sorted(path for path in uses_by_path if path not in reported_paths)
            unreported_files = {uses_by_path[p] for p in erc_unreported}
            for rel, violations in per_sheet.items():
                sheets[rel]["erc"] = {
                    "reported": rel not in unreported_files, "violations": violations, "count": len(violations), "by_severity": _severities(violations)}
            every = [v for vs in per_sheet.values() for v in vs] + [v for u in erc_unmapped for v in u["violations"]]
            totals.update({"erc_violations": len(every), "erc_by_severity": _severities(every)})

    complete = not not_run and not tree.problems and not library_problems and not erc_unmapped and not erc_unreported
    unattributed_count = sum(len(v) for v in unattributed.values())
    has_findings = bool(
        totals.get("unconnected_pins") or totals.get("connection_issues") or totals.get("missing_from_netlist") or totals.get("erc_violations")
        or page_total or page_unchecked)
    reasons = []
    if tree.problems:
        reasons.append("sheets that cannot be loaded: " + "; ".join(tree.problems))
    reasons += [f"{name} not run: {why}" for name, why in not_run.items()]
    if library_problems:
        reasons.append("project libraries could not be copied for the scratch run: " + "; ".join(library_problems))
    if erc_unmapped:
        reasons.append(f"{len(erc_unmapped)} ERC sheet(s) could not be matched to a sheet file (listed under erc_unmapped)")
    if erc_unreported:
        reasons.append(f"{len(erc_unreported)} sheet use(s) are not in the ERC report (marked reported: false)")
    return {
        "status": "ok" if complete else "incomplete", "ok": complete, "written": False, "root": root_rel,
        "checks_requested": wanted, "library_problems": library_problems, "checks_not_run": not_run, "covered": complete,
        **({"reason": "; ".join(reasons)} if reasons else {}),
        "totals": totals, "no_findings": complete and len(wanted) == len(CHECKS) and not has_findings and not unattributed_count,
        "sheets": list(sheets.values()),
        "connectivity_unattributed": unattributed, "erc_unmapped": erc_unmapped, "erc_unreported_paths": erc_unreported,
        **{k: v for k, v in tree.report().items() if k != "sheets"},
    }
