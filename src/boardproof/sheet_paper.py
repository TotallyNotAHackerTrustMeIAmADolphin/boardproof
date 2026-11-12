"""set_paper: the paper size of schematic sheets, batch, with a fit report (GitHub issue 197).

A sheet's ``(paper "A4")`` token names its page. Facts measured with kicad-cli 10.0.7 (SVG export of the
page, ``sch upgrade`` for what KiCad writes; docs/ARCHITECTURE.md, known facts; tests pin each of them):

- Named sizes: A5 A4 A3 A2 A1 A0, A B C D E, USLetter USLegal USLedger, written ``(paper "A4")``, landscape.
  ``(paper "A4" portrait)`` is the same size turned (210 x 297). KiCad reads any letter case ("a4" is A4)
  and writes the canonical name; this tool accepts and writes only the canonical spelling.
- A custom size is ``(paper "User" 300 200)`` (width, height in mm). KiCad clamps each side to 25.4 ..
  3048 mm and rounds to a mil (0.0254 mm). A ``portrait`` flag after a User size is read as a swap of the
  two numbers and is not written back, so this tool takes width and height as the page really is and
  refuses ``portrait`` together with ``User``.
- A name KiCad does not know ("A6", "Letter", "Tabloid"), a User size without both numbers, a named size
  with numbers and any other trailing word make the sheet unloadable (exit 3).

The paper change cannot change a net. The oracle after the edit is kicad-cli on scratch copies: the SVG
export of each edited sheet must have the asked page size, the netlist must have the nets it had, and
the ERC census must be unchanged. Where objects lie against the new page is reported (the fit report,
``sheet_extents``); an object outside the new page is a warning ``outside_page``, never a refusal. An
object that cannot be measured at all refuses the call (``object_not_classified``): the report would
otherwise claim a fit it did not check.
"""

from __future__ import annotations

import math
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp

from boardproof import sheet_authoring, sheet_editing, sheet_extents, sheet_tree, title_block, write_layer
from boardproof.sheet_authoring import SheetAuthoringError

_ITEM_KEYS = {"sheet", "size", "portrait", "width", "height"}
SIZES = (*sheet_extents.PAPER_MM, "User")
PAGE_TOLERANCE_MM = 0.1   # the SVG page is the size KiCad rounded to a mil, drawn in 4 decimals
MAX_LISTED = sheet_editing.MAX_LISTED


# --------------------------------------------------------------------------- the request


def check_items(items: Any) -> list[dict]:
    """[{sheet (or None), size, portrait, width, height}] with every error refusing the whole call."""
    if not isinstance(items, list) or not items:
        raise SheetAuthoringError("invalid_spec", "items must be a non-empty list of {sheet?, size, portrait?, width?, height?}")
    checked = []
    for i, item in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(item, dict) or set(item) - _ITEM_KEYS:
            raise SheetAuthoringError("invalid_spec", f"{where} must be an object with keys from {sorted(_ITEM_KEYS)}, got {item!r}")
        sheet = item.get("sheet")
        if sheet is not None and not (isinstance(sheet, str) and sheet.strip()):
            raise SheetAuthoringError("invalid_spec", f"{where}.sheet must be a non-empty path string (a sheet of the project) or be left out")
        size = item.get("size")
        if not isinstance(size, str) or size not in SIZES:
            raise SheetAuthoringError("invalid_spec", f"{where}.size must be one of {list(SIZES)} (KiCad's spelling), got {size!r}")
        portrait = item.get("portrait", False)
        if portrait is None:
            portrait = False
        if not isinstance(portrait, bool):
            raise SheetAuthoringError("invalid_spec", f"{where}.portrait must be true or false, got {portrait!r}")
        width, height = item.get("width"), item.get("height")
        if size == "User":
            if portrait:
                raise SheetAuthoringError("invalid_spec", f'{where}: portrait cannot be combined with "User" (KiCad reads it as a swap of the two numbers and does not write it back); give width and height as the page is, for example width 200 and height 300')
            for key, value in (("width", width), ("height", height)):
                if (not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value)
                        or not sheet_extents.USER_MIN_MM <= value <= sheet_extents.USER_MAX_MM):
                    raise SheetAuthoringError(
                        "invalid_spec", f'{where}.{key} must be a number of mm from {sheet_extents.USER_MIN_MM:g} to {sheet_extents.USER_MAX_MM:g} for size "User" '
                        f"(KiCad clamps anything else silently), got {value!r}")
        elif width is not None or height is not None:
            raise SheetAuthoringError("invalid_spec", f'{where}: width and height belong to size "User" only, not to {size!r}')
        checked.append({"sheet": sheet, "size": size, "portrait": portrait, "width": width, "height": height})
    return checked


def _new_paper(spec: dict) -> SExp:
    node = SExp.list("paper", write_layer.string(spec["size"]))
    if spec["size"] == "User":
        node.children.append(write_layer.number(spec["width"]))
        node.children.append(write_layer.number(spec["height"]))
    elif spec["portrait"]:
        node.children.append(write_layer.symbol("portrait"))
    return node


def _expected_page(spec: dict) -> tuple[float, float]:
    if spec["size"] == "User":
        return float(spec["width"]), float(spec["height"])
    w, h = sheet_extents.PAPER_MM[spec["size"]]
    return (h, w) if spec["portrait"] else (w, h)


def _paper_node(root: SExp, who: str) -> SExp:
    nodes = root.find_children("paper")
    if len(nodes) != 1:
        raise SheetAuthoringError("paper_unusual", f"{who} has {len(nodes)} paper nodes (one is expected); nothing was written")
    return nodes[0]


def apply_paper(root: SExp, spec: dict, who: str) -> None:
    """Replace the sheet's one paper node by the asked one, in the same place."""
    old = _paper_node(root, who)
    root.children[root.children.index(old)] = _new_paper(spec)


# --------------------------------------------------------------------------- the fit report


def fit_report(root: SExp, page: tuple[float, float], old_page: tuple[float, float] | None, who: str) -> dict[str, Any]:
    entries = sheet_extents.sheet_extents(root)
    unknown = [e for e in entries if e["measured"] == "none"]
    if unknown:
        listed = [{"kind": e["kind"], "uuid": e["uuid"], "label": e["label"]} for e in unknown[:MAX_LISTED]]
        raise SheetAuthoringError(
            "object_not_classified",
            f"{who} holds {len(unknown)} object(s) whose extent cannot be measured ({sorted({e['kind'] for e in unknown})}); the fit of the new paper cannot be "
            "reported, so nothing was written", objects=listed, total=len(unknown))
    outside = sheet_extents.outside_page(entries, page)
    box = sheet_extents.union_box(entries)
    by_kind = Counter(e["kind"] for e in outside)
    return {
        "objects": len(entries),
        "extents": None if box is None else {"x1": box[0], "y1": box[1], "x2": box[2], "y2": box[3]},
        "outside_page": {
            "count": len(outside),
            "by_kind": dict(sorted(by_kind.items())),
            "listed": [{k: e[k] for k in ("kind", "uuid", "label", "x1", "y1", "x2", "y2", "measured")} for e in outside[:MAX_LISTED]],
            "truncated": len(outside) > MAX_LISTED,
        },
        "outside_old_page_count": None if old_page is None else len(sheet_extents.outside_page(entries, old_page)),
    }


# --------------------------------------------------------------------------- the oracle (kicad-cli)


def _svg_page_mm(svg_dir: Path, stem: str) -> tuple[float, float] | None:
    import re

    candidates = [f for f in svg_dir.glob("*.svg") if f.stem == stem]
    if len(candidates) != 1:
        return None
    head = candidates[0].read_text(encoding="utf-8", errors="replace")[:3000]
    m = re.search(r'<svg[^>]*?\bwidth="([0-9.]+)mm"[^>]*?\bheight="([0-9.]+)mm"', head, re.S)
    return (float(m.group(1)), float(m.group(2))) if m else None


def _check_page_follows(tree: sheet_tree.SheetTree, root_after: Path, expected: dict[Path, tuple[float, float]]) -> None:
    """kicad-cli's SVG export of each edited sheet (on the scratch copy) has the asked page size."""
    from boardproof import kicad_cli

    base = tree.project_dir
    for file, want in expected.items():
        sheet_copy = root_after.parent / file.resolve().relative_to(base)    # the scratch project keeps the layout
        with tempfile.TemporaryDirectory(prefix="boardproof-paper-svg-") as tmp:
            try:
                kicad_cli.export_svg(sheet_copy, tmp)
            except kicad_cli.KicadCliError as exc:
                raise SheetAuthoringError("write_not_verified", f"check page_follows_edit: kicad-cli could not export {tree.rel(file)} to SVG: {exc}; nothing was written",
                                          check="page_follows_edit") from exc
            got = _svg_page_mm(Path(tmp), sheet_copy.stem)
        if got is None or abs(got[0] - want[0]) > PAGE_TOLERANCE_MM or abs(got[1] - want[1]) > PAGE_TOLERANCE_MM:
            raise SheetAuthoringError(
                "write_not_verified", f"check page_follows_edit: kicad-cli draws {tree.rel(file)} on a page of {got} mm, not the asked {want[0]:g} x {want[1]:g} mm; nothing was written",
                check="page_follows_edit", page_mm=None if got is None else list(got))


# --------------------------------------------------------------------------- the call


def set_paper(path: str, items: list[dict], root_sch: str | None, *, dry_run: bool) -> dict[str, Any]:
    """Set the paper size of one or several sheets of a project, in one call."""
    try:
        return _run(Path(path), items, root_sch, dry_run)
    except SheetAuthoringError as exc:
        return sheet_authoring.refusal(exc)
    except write_layer.WriteLayerError as exc:
        if (locked := write_layer.as_lock_refusal(exc)) is not None:
            return locked
        return {"status": "incomplete", "ok": False, "code": exc.code, "reason": str(exc), **exc.details}


def _run(path: Path, items: Any, root_sch: str | None, dry_run: Any) -> dict[str, Any]:
    if not isinstance(dry_run, bool):
        raise SheetAuthoringError("invalid_spec", "dry_run must be true or false")
    checked = check_items(items)
    if not path.is_file():
        raise SheetAuthoringError("file_not_found", f"sheet not found: {path}")
    tree, standalone = sheet_editing._tree_for(path, root_sch)
    files: list[Path] = []
    for i, spec in enumerate(checked):
        file = title_block._resolve_sheet(tree, path, spec["sheet"], f"items[{i}]")
        if file in files:
            raise SheetAuthoringError("invalid_spec", f"items[{i}] names {tree.rel(file)} again; give each sheet one item")
        files.append(file)
        spec["file"] = file
    if not dry_run:
        write_layer.check_writable(*files)
    with write_layer.path_lock(files):
        return _locked(tree, standalone, checked, dry_run)


def _locked(tree: sheet_tree.SheetTree, standalone: bool, checked: list[dict], dry_run: bool) -> dict[str, Any]:
    originals: dict[Path, SExp] = {}
    staged: dict[Path, SExp] = {}
    expected: dict[Path, tuple[float, float]] = {}
    listed: list[dict] = []
    warnings: list[dict] = []
    for spec in checked:
        file: Path = spec["file"]
        who = tree.rel(file)
        original = write_layer.load(file)
        if original.name != "kicad_sch":
            raise SheetAuthoringError("invalid_spec", f"{who} is not a schematic sheet")
        old_node = _paper_node(original, who)
        old_page = sheet_extents.page_size_mm(old_node)
        if old_page is None:
            raise SheetAuthoringError("paper_unusual", f"{who}: the paper token {write_layer.serialise(old_node, compact=True)!r} is not one KiCad reads; nothing was written")
        old_text = write_layer.serialise(old_node, compact=True)
        new = write_layer.load(file)
        apply_paper(new, spec, who)
        new_page = _expected_page(spec)
        new_text = write_layer.serialise(_new_paper(spec), compact=True)     # from the request, not from the staged tree
        _staged_check(original, new, new_text, new_page, who)
        fit = fit_report(new, new_page, old_page, who)
        changed = old_text != new_text
        originals[file], staged[file] = original, new
        if changed:
            expected[file] = new_page
        listed.append({"sheet": who, "action": "changed" if changed else "unchanged", "paper_from": old_text, "paper_to": new_text,
                       "page_mm_from": list(old_page), "page_mm_to": list(new_page), "fit": fit})
        if fit["outside_page"]["count"]:
            warnings.append({"code": "outside_page", "sheet": who, "count": fit["outside_page"]["count"], "by_kind": fit["outside_page"]["by_kind"],
                             "note": f"{fit['outside_page']['count']} object(s) of {who} are not wholly inside the {new_page[0]:g} x {new_page[1]:g} mm page (see fit.outside_page); "
                                     "they are still on the sheet and will print off the page"})
    edited = list(expected)
    result: dict[str, Any] = {
        "status": "ok", "ok": True, "written": False, "dry_run": dry_run, "sheets": listed, "count": len(edited), "warnings": warnings,
        "not_measured": list(sheet_extents.NOT_MEASURED),
        "note": "Page size in mm is the page KiCad draws (a landscape page is wider than high; portrait swaps them). The fit report measures the objects "
                "listed in not_measured only as far as stated there; an object inside the page by its anchor can still print partly outside."}
    if standalone:
        result["standalone_sheet"] = True
    if not edited:
        result["note"] += " Every sheet already had the asked paper; nothing was written."
        return result
    with tempfile.TemporaryDirectory(prefix="boardproof-paper-") as tmp:
        scratch_before, scratch_after = Path(tmp) / "before", Path(tmp) / "after"
        scratch_before.mkdir()
        scratch_after.mkdir()
        root_before = title_block._scratch_project(tree, {}, str(scratch_before))
        root_after = title_block._scratch_project(tree, {f: staged[f] for f in edited}, str(scratch_after))
        nets_before, _ = title_block._read_netlist(root_before)
        nets_after, _ = title_block._read_netlist(root_after)
        erc_before = sheet_authoring.erc_census(root_before)
        erc_after = sheet_authoring.erc_census(root_after)
        _check_page_follows(tree, root_after, expected)
    if nets_before != nets_after:
        raise SheetAuthoringError("write_not_verified", "check netlist_unchanged: kicad-cli's netlist of the result has other nets than before; a paper change cannot change connectivity; nothing was written",
                                  check="netlist_unchanged")
    if Counter(erc_before) != Counter(erc_after):
        raise SheetAuthoringError("write_not_verified", f"check erc_unchanged: kicad-cli's ERC of the result differs from before ({dict(erc_before)} -> {dict(erc_after)}); nothing was written",
                                  check="erc_unchanged")
    result["connectivity"] = {"nets_before": len(nets_before), "nets_after": len(nets_after), "changed": False}
    result["erc"] = {"violations_before": sum(erc_before.values()), "violations_after": sum(erc_after.values())}
    if dry_run:
        return result
    written: list[Path] = []
    try:
        for file in edited:
            write_layer.save(staged[file], file, verify=sheet_editing._expected_verify(staged[file]))
            written.append(file)
        for file in edited:
            if write_layer.serialise(write_layer.load(file), compact=True) != write_layer.serialise(staged[file], compact=True):
                raise SheetAuthoringError("write_not_verified", f"check reload_equals_serialised: {tree.rel(file)} does not read back as the edited tree; the sheets written so far were restored",
                                          check="reload_equals_serialised")
    except BaseException:
        for file in written:  # one write failed: no sheet may keep a change the call did not finish
            try:
                write_layer.save(originals[file], file)
            except Exception:  # noqa: BLE001 - the original failure is the one to report
                pass
        raise
    result["written"] = True
    return result


def _staged_check(original: SExp, new: SExp, want_text: str, want_page: tuple[float, float], who: str) -> None:
    """Every child of the sheet except the paper node is unchanged, and the paper node is exactly the asked one."""
    def others(root: SExp) -> list[str]:
        return [write_layer.serialise(c, compact=True) for c in root.children if c.is_atom or c.name != "paper"]

    problems = []
    if others(original) != others(new):
        problems.append("a node other than the paper changed")
    nodes = new.find_children("paper")
    got = write_layer.serialise(nodes[0], compact=True) if len(nodes) == 1 else None
    if got != want_text:
        problems.append(f"the paper node is {got!r}, not {want_text!r}")
    page = sheet_extents.page_size_mm(new)
    if page is None or any(abs(a - b) > 1e-9 for a, b in zip(page, want_page, strict=True)):
        problems.append(f"the paper reads as {page}, not {want_page}")
    if problems:
        raise SheetAuthoringError("write_not_verified", f"{who}: the edited sheet differs from what was asked: " + "; ".join(problems) + "; nothing was written",
                                  check="staged", problems=problems)
