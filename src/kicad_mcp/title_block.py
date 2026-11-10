"""set_title_block: the title block of schematic sheets, batch, with line breaks (GitHub issue 97).

A sheet's ``(title_block (title ..) (date ..) (rev ..) (company ..) (comment N ..))`` holds the text
that the drawing sheet prints. A note in a comment is often several lines long, and a line break
written raw into a KiCad string is the classic way to drop a whole sub-sheet (project rule 1), so
this tool never writes text: it sets typed atoms in the loaded ``kicad_tools`` tree and saves through
the write layer.

Facts measured with kicad-cli 10.0.7 (docs/ARCHITECTURE.md, known facts; tests pin each of them):

- KiCad stores a line break in a title block string as the two characters ``\\n`` (a backslash and
  an n), a CR as ``\\r``, a tab raw. ``kicad-cli sch upgrade`` rewrites a file that holds them the
  same way, so the file survives KiCad's own next save (unlike a sheet pin name, which KiCad writes
  unescaped: ``write_layer.save`` refuses that).
- The drawing sheet prints the break as a second line (SVG export). A CR is printed as a stray
  character, so a lone CR is refused here and CRLF becomes LF (and is listed).
- Comments are numbered 1 to 9; ``(comment 0 ..)`` and ``(comment 10 ..)`` make the file unloadable.
- KiCad's own rewrite drops a field whose text is empty, so an empty or null value removes the node
  (an empty field and no field are the same to KiCad).
- A duplicated field node is read last-wins; this tool refuses such a block instead of guessing.
- kicad-cli's netlist lists the title block of every sheet instance under ``(design (sheet ..
  (title_block ..)))``, with all nine comments. That is the oracle: after the edit, the staged
  netlist must carry exactly the asked text (a break comes back as a break), and the netlist's nets
  and the ERC census must be what they were.

A title block has no position, so the 1.27 mm grid does not apply; nothing on the sheet moves.
"""

from __future__ import annotations

import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp

from kicad_mcp import sheet_authoring, sheet_editing, sheet_tree, write_layer
from kicad_mcp.sheet_authoring import SheetAuthoringError

TEXT_FIELDS = ("title", "date", "rev", "company")
COMMENT_NUMBERS = tuple(range(1, 10))
MAX_CHARS = 4000
_ITEM_KEYS = {"sheet", "title", "date", "rev", "company", "comments"}
Fields = dict[str, str]  # "title" | "date" | "rev" | "company" | "comment1" .. "comment9" -> text; absent = no node
FIELD_ORDER = (*TEXT_FIELDS, *(f"comment{n}" for n in COMMENT_NUMBERS))


# --------------------------------------------------------------------------- the node


def _title_blocks(root: SExp) -> list[SExp]:
    return [c for c in root.children if not c.is_atom and c.name == "title_block"]


def _field_name(node: SExp) -> str | None:
    if node.is_atom:
        return None
    if node.name in TEXT_FIELDS:
        return node.name
    if node.name == "comment":
        number = node.get_int(0)
        return f"comment{number}" if number in COMMENT_NUMBERS else None
    return None


def read_fields(root: SExp, who: str) -> Fields:
    """The title block of a sheet as {field: text}; a block this tool cannot read exactly (a second
    block, a duplicated or unknown child, a comment number outside 1-9, a value that is not a string)
    is refused, never guessed at."""
    blocks = _title_blocks(root)
    if len(blocks) > 1:
        raise SheetAuthoringError("title_block_unusual", f"{who} has {len(blocks)} title_block nodes; nothing was written")
    if not blocks:
        return {}
    fields: Fields = {}
    for child in blocks[0].children:
        name = _field_name(child)
        if name is None:
            raise SheetAuthoringError(
                "title_block_unusual", f"{who}: the title block holds {write_layer.serialise(child, compact=True)[:60]!r}, "
                "which this tool does not know (fields: title, date, rev, company, comment 1 to 9); nothing was written")
        if name in fields:
            raise SheetAuthoringError("title_block_unusual", f"{who}: the title block holds {name} twice (KiCad reads the last); nothing was written")
        atoms = [a for a in child.children if a.is_atom]
        text_atom = atoms[-1] if atoms else None
        if text_atom is None or not isinstance(text_atom.value, str) or not write_layer.atom_text(text_atom).startswith('"'):
            raise SheetAuthoringError("title_block_unusual", f"{who}: {name} is not a quoted string; nothing was written")
        if text_atom.value != "":     # an empty field and none are the same to KiCad (and to its netlist)
            fields[name] = text_atom.value
    return fields


def _new_field(name: str, text: str) -> SExp:
    if name.startswith("comment"):
        return SExp.list("comment", write_layer.number(int(name[7:])), write_layer.string(text))
    return SExp.list(name, write_layer.string(text))


def _block_for(root: SExp) -> SExp:
    blocks = _title_blocks(root)
    if blocks:
        return blocks[0]
    block = SExp.list("title_block")
    # where KiCad writes it: after paper (or, with no paper, after uuid); position is cosmetic to the loader
    index = 0
    for i, child in enumerate(root.children):
        if not child.is_atom and child.name in ("version", "generator", "generator_version", "uuid", "paper"):
            index = i + 1
    root.children.insert(index, block)
    return block


def apply_fields(root: SExp, changes: dict[str, str | None], who: str) -> None:
    """Set (text) or remove (None) fields in the sheet's title block, children in KiCad's order."""
    if not _title_blocks(root) and all(v is None for v in changes.values()):
        return      # nothing to remove from a sheet that has no title block; do not make an empty one
    block = _block_for(root)
    present = {_field_name(c): c for c in block.children}
    for name, text in changes.items():
        node = present.get(name)
        if text is None:
            if node is not None:
                block.children.remove(node)
            continue
        if node is not None:
            atom_index = len(node.children) - 1
            try:
                write_layer.set_string(node, atom_index, text)
            except write_layer.InvalidStringError as exc:
                raise SheetAuthoringError("invalid_spec", f"{who}.{name} cannot be written: {exc}") from exc
            continue
        try:
            fresh = _new_field(name, text)
        except write_layer.InvalidStringError as exc:
            raise SheetAuthoringError("invalid_spec", f"{who}.{name} cannot be written: {exc}") from exc
        rank = FIELD_ORDER.index(name)
        at = len(block.children)
        for i, child in enumerate(block.children):
            if FIELD_ORDER.index(_field_name(child)) > rank:
                at = i
                break
        block.children.insert(at, fresh)
        present[name] = fresh


# --------------------------------------------------------------------------- the request


def _text(value: Any, where: str, warnings: list[dict]) -> str | None:
    """None (remove) or the text to write; CRLF becomes LF (listed), a lone CR and any other control
    character is refused."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise SheetAuthoringError("invalid_spec", f"{where} must be a string or null, got {value!r}")
    if "\r\n" in value:
        value = value.replace("\r\n", "\n")
        warnings.append({"code": "line_break_normalised", "field": where, "note": "CRLF was written as one line break; KiCad prints a CR as a stray character"})
    if "\r" in value:
        raise SheetAuthoringError("invalid_spec", f"{where} holds a lone CR; KiCad prints it as a stray character. Use \\n for a line break; nothing was written")
    try:
        write_layer.check_text(value)
    except write_layer.InvalidStringError as exc:
        raise SheetAuthoringError("invalid_spec", f"{where} cannot be written: {exc}; nothing was written") from exc
    if len(value) > MAX_CHARS:
        raise SheetAuthoringError("invalid_spec", f"{where} is {len(value)} characters; at most {MAX_CHARS} are accepted; nothing was written")
    if value == "":
        return None
    if value != value.strip(" \t") or value.endswith("\n") or value.startswith("\n"):
        warnings.append({"code": "title_block_edge_whitespace", "field": where,
                         "note": "the text starts or ends with white space or a line break; the drawing sheet prints it as is"})
    return value


def check_items(items: Any) -> tuple[list[dict], list[dict]]:
    """[{sheet (or None), changes {field: text | None}}] and the warnings; every error refuses the whole call."""
    if not isinstance(items, list) or not items:
        raise SheetAuthoringError("invalid_spec", "items must be a non-empty list of {sheet?, title?, date?, rev?, company?, comments?}")
    warnings: list[dict] = []
    checked = []
    for i, item in enumerate(items):
        where = f"items[{i}]"
        if not isinstance(item, dict) or set(item) - _ITEM_KEYS:
            raise SheetAuthoringError("invalid_spec", f"{where} must be an object with keys from {sorted(_ITEM_KEYS)}, got {item!r}")
        sheet = item.get("sheet")
        if sheet is not None and not (isinstance(sheet, str) and sheet.strip()):
            raise SheetAuthoringError("invalid_spec", f"{where}.sheet must be a non-empty path string (a sheet of the project) or be left out")
        changes: dict[str, str | None] = {}
        for name in TEXT_FIELDS:
            if name in item:
                changes[name] = _text(item[name], f"{where}.{name}", warnings)
        comments = item.get("comments", {})
        if not isinstance(comments, dict):
            raise SheetAuthoringError("invalid_spec", f"{where}.comments must be an object {{\"1\": text, ...}} with numbers 1 to 9")
        for key, value in comments.items():
            number = int(key) if isinstance(key, int) and not isinstance(key, bool) else (int(key) if isinstance(key, str) and key.isdigit() else None)
            if number not in COMMENT_NUMBERS or (isinstance(key, str) and key != str(number)):
                raise SheetAuthoringError("invalid_spec", f"{where}.comments key {key!r} is not a comment number from 1 to 9 (KiCad cannot load any other)")
            if f"comment{number}" in changes:
                raise SheetAuthoringError("invalid_spec", f"{where}.comments names comment {number} twice")
            changes[f"comment{number}"] = _text(value, f"{where}.comments[{number}]", warnings)
        if not changes:
            raise SheetAuthoringError("invalid_spec", f"{where} sets no field; give at least one of title, date, rev, company, comments")
        checked.append({"sheet": sheet, "changes": changes})
    return checked, warnings


def _resolve_sheet(tree: sheet_tree.SheetTree, path: Path, sheet: str | None, where: str) -> Path:
    if sheet is None:
        return path.resolve()
    given = Path(sheet)
    candidates = [given] if given.is_absolute() else [tree.project_dir / given, path.parent / given]
    in_tree = {f.resolve(): f for f in tree.files}
    found = []
    for candidate in candidates:
        try:
            key = candidate.resolve()
        except OSError:
            continue
        if key in in_tree and key not in found:
            found.append(key)
    if not found:
        raise SheetAuthoringError("sheet_not_in_project", f"{where}.sheet {sheet!r} is not a sheet of this project (sheets: {[tree.rel(f) for f in tree.files]}); nothing was written",
                                  **tree.report())
    if len(found) > 1:
        raise SheetAuthoringError("ambiguous", f"{where}.sheet {sheet!r} names {[tree.rel(f) for f in found]}; give the path from the project's root folder; nothing was written")
    return found[0]


# --------------------------------------------------------------------------- the oracle (kicad-cli)


def _read_netlist(root: Path) -> tuple[dict[str, frozenset], list[tuple[str, Fields]]]:
    """kicad-cli's netlist of the project at ``root`` (a scratch copy): nets as ``sheet_editing.nets_from``
    reads them, and the title block of every sheet instance as (source file name, fields)."""
    from kicad_tools.sexp.parser import parse_file

    from kicad_mcp import kicad_cli

    with tempfile.TemporaryDirectory(prefix="kicad-mcp-title-net-") as tmp:
        parsed = parse_file(str(kicad_cli.export_netlist(root, Path(tmp) / "check.net")))
    nets: dict[str, frozenset] = {}
    for block in parsed.find_children("nets"):
        for net in block.find_children("net"):
            name = str(net.find("name").children[0].value)
            nets[name] = frozenset((str(n.find("ref").children[0].value), str(n.find("pin").children[0].value))
                                   for n in net.find_children("node") if not str(n.find("ref").children[0].value).endswith("?"))
    blocks: list[tuple[str, Fields]] = []
    for design in parsed.find_children("design"):
        for sheet in design.find_children("sheet"):
            for tb in sheet.find_children("title_block"):
                fields: Fields = {}
                source = ""
                for child in tb.children:
                    if child.is_atom:
                        continue
                    if child.name == "source":
                        source = str(child.children[0].value) if child.children else ""
                    elif child.name in TEXT_FIELDS:
                        if child.children:
                            fields[child.name] = str(child.children[0].value)
                    elif child.name == "comment":
                        number = child.find("number")
                        value = child.find("value")
                        text = str(value.children[0].value) if value is not None and value.children else ""
                        if text:
                            fields[f"comment{number.children[0].value}"] = text
                blocks.append((source, {k: v for k, v in fields.items() if v != ""}))
    return nets, blocks


def _scratch_project(tree: sheet_tree.SheetTree, replacements: dict[Path, SExp], tmp: str) -> Path:
    scratch = Path(tmp) / "p"
    root = sheet_authoring._copy_project(tree, [], scratch)
    for file, node in replacements.items():
        sheet_authoring._save(node, scratch / file.resolve().relative_to(tree.project_dir))
    return root


def _check_netlist_follows(tree: sheet_tree.SheetTree, expected: dict[Path, Fields], blocks: list[tuple[str, Fields]]) -> None:
    """Every sheet instance of the staged netlist shows a title block some file of that name has (after the
    edit), and every edited sheet shows exactly the asked block."""
    after: dict[Path, Fields] = {}
    for file in tree.files:
        key = file.resolve()
        after[key] = expected[key] if key in expected else read_fields(write_layer.load(file), tree.rel(file))
    by_name: dict[str, list[Fields]] = {}
    for key, fields in after.items():
        by_name.setdefault(key.name, []).append(fields)
    shown: dict[str, list[Fields]] = {}
    for source, fields in blocks:
        shown.setdefault(source, []).append(fields)
        if fields not in by_name.get(source, []):
            raise SheetAuthoringError(
                "write_not_verified",
                f"check netlist_follows_edit: kicad-cli's netlist of the result shows a title block for {source} that no sheet file of that name holds: "
                f"{fields!r}; nothing was written", check="netlist_follows_edit", netlist_title_block=fields)
    for key, fields in expected.items():
        if fields not in shown.get(key.name, []):
            raise SheetAuthoringError(
                "write_not_verified",
                f"check netlist_follows_edit: kicad-cli's netlist of the result does not show the asked title block for {tree.rel(key)}: "
                f"asked {fields!r}, netlist has {shown.get(key.name, [])!r}; nothing was written", check="netlist_follows_edit")


# --------------------------------------------------------------------------- the call


def set_title_block(path: str, items: list[dict], root_sch: str | None, *, dry_run: bool) -> dict[str, Any]:
    """Set the title block fields of one or several sheets of a project, in one call."""
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
    checked, warnings = check_items(items)
    if not path.is_file():
        raise SheetAuthoringError("file_not_found", f"sheet not found: {path}")
    tree, standalone = sheet_editing._tree_for(path, root_sch)
    files: list[Path] = []
    for i, spec in enumerate(checked):
        file = _resolve_sheet(tree, path, spec["sheet"], f"items[{i}]")
        if file in files:
            raise SheetAuthoringError("invalid_spec", f"items[{i}] names {tree.rel(file)} again; put all its fields in one item")
        files.append(file)
        spec["file"] = file
    if not dry_run:
        write_layer.check_writable(*files)
    with write_layer.path_lock(files):
        return _locked(tree, standalone, checked, warnings, dry_run)


def _locked(tree: sheet_tree.SheetTree, standalone: bool, checked: list[dict], warnings: list[dict], dry_run: bool) -> dict[str, Any]:
    originals: dict[Path, SExp] = {}
    staged: dict[Path, SExp] = {}
    before_fields: dict[Path, Fields] = {}
    expected: dict[Path, Fields] = {}
    listed = []
    for spec in checked:
        file: Path = spec["file"]
        who = tree.rel(file)
        original = write_layer.load(file)
        if original.name != "kicad_sch":
            raise SheetAuthoringError("invalid_spec", f"{who} is not a schematic sheet")
        new = write_layer.load(file)
        before = read_fields(original, who)
        want = dict(before)
        rows = []
        for name in FIELD_ORDER:
            if name not in spec["changes"]:
                continue
            text = spec["changes"][name]
            old = before.get(name)
            if text is None:
                want.pop(name, None)
            else:
                want[name] = text
            rows.append({"field": name, "from": old, "to": text, "action": "unchanged" if old == text else ("removed" if text is None else "added" if old is None else "changed")})
        apply_fields(new, dict(spec["changes"]), who)
        _staged_check(original, new, want, who)
        originals[file], staged[file], before_fields[file], expected[file] = original, new, before, want
        listed.append({"sheet": who, "changed": [r for r in rows if r["action"] != "unchanged"], "unchanged": [r["field"] for r in rows if r["action"] == "unchanged"]})
    edited = [f for f in staged if expected[f] != before_fields[f]]
    result: dict[str, Any] = {"status": "ok", "ok": True, "written": False, "dry_run": dry_run, "sheets": listed,
                              "count": sum(len(s["changed"]) for s in listed),
                              "note": "A title block has no position: the 1.27 mm grid does not apply and nothing on the sheet moves."}
    if standalone:
        result["standalone_sheet"] = True
    if warnings:
        result["warnings"] = warnings
    if not edited:
        result["note"] += " Every field already had the asked text; nothing was written."
        return result
    with tempfile.TemporaryDirectory(prefix="kicad-mcp-title-") as tmp:
        scratch_before = Path(tmp) / "before"
        scratch_after = Path(tmp) / "after"
        scratch_before.mkdir()
        scratch_after.mkdir()
        root_before = _scratch_project(tree, {}, str(scratch_before))
        root_after = _scratch_project(tree, {f: staged[f] for f in edited}, str(scratch_after))
        nets_before, _ = _read_netlist(root_before)
        nets_after, blocks = _read_netlist(root_after)
        erc_before = sheet_authoring.erc_census(root_before)
        erc_after = sheet_authoring.erc_census(root_after)
    _check_netlist_follows(tree, {f: expected[f] for f in edited}, blocks)
    if nets_before != nets_after:
        raise SheetAuthoringError("write_not_verified", "check netlist_unchanged: kicad-cli's netlist of the result has other nets than before; a title block cannot change connectivity; nothing was written",
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


def _staged_check(original: SExp, new: SExp, want: Fields, who: str) -> None:
    """Every child of the sheet except the title block is unchanged, and the title block holds exactly ``want``."""
    def others(root: SExp) -> list[str]:
        return [write_layer.serialise(c, compact=True) for c in root.children if c.is_atom or c.name != "title_block"]

    problems = []
    if others(original) != others(new):
        problems.append("a node other than the title block changed")
    got = read_fields(new, who) if _title_blocks(new) else {}
    if got != want:
        problems.append(f"the title block holds {got!r}, not {want!r}")
    if problems:
        raise SheetAuthoringError("write_not_verified", f"{who}: the edited sheet differs from what was asked: " + "; ".join(problems) + "; nothing was written",
                                  check="staged", problems=problems)
