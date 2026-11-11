"""Each schematic sheet file is parsed at most once per tool call (issue 71).

A sheet-tree tool call (``propose_schematic_import``, the BOM, ``verify_board``'s annotation
and netlist-count checks) used to re-parse the same sheet files about eight times: 296
``parse_file`` calls on the 36 sheet files of the vme-wren demo. The parsed sheets now travel
through the call, in the ``parsed`` memo of the ``SheetTree`` (or the dict a caller passes to
``walk`` and ``_annotation_problems``). The memo is owned by one call: nothing is module-level,
so a file edited between two calls is read fresh by the second.
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import kicad_tools.sexp as sexp_pkg
import kicad_tools.sexp.parser as parser_mod
from kicad_tools.sexp import Document

from boardproof import schematic_import as si
from boardproof import sheet_tree
from boardproof import verify_board as gate


@contextmanager
def counted_sheet_parses(monkeypatch) -> Iterator[list[str]]:
    """The ``.kicad_sch`` paths handed to ``parse_file``, whichever import spelling is used."""
    seen: list[str] = []
    real = parser_mod.parse_file

    def counting(path, *args, **kwargs):
        if str(path).endswith(".kicad_sch"):
            seen.append(str(Path(path).resolve()))
        return real(path, *args, **kwargs)

    monkeypatch.setattr(parser_mod, "parse_file", counting)
    monkeypatch.setattr(sexp_pkg, "parse_file", counting)
    yield seen


def _first_part(sheet: Path, new_value: str) -> None:
    """Change the Value of the first non-power symbol of ``sheet`` through the object model."""
    doc = Document.load(sheet)
    for symbol in doc.root.find_children("symbol"):
        if (symbol.find("lib_id").get_string(0) or "").startswith("power:"):
            continue
        for prop in symbol.find_children("property"):
            if prop.get_string(0) == "Value":
                prop.set_value(1, new_value)
                doc.save(sheet)
                return
    raise AssertionError("no part to edit")


def test_a_schematic_import_parses_each_vme_wren_sheet_at_most_once(kicad_demo, tmp_path, monkeypatch) -> None:
    project = kicad_demo("vme-wren")
    sheets = len(sheet_tree.walk(project / "vme-wren.kicad_sch").files)
    assert sheets > 30  # the case this guards: a 36-file hierarchy

    with counted_sheet_parses(monkeypatch) as seen:
        result = si.propose_schematic_import(project / "vme-wren.kicad_pcb", output_dir=tmp_path / "out")

    assert result["status"] == "proposed", result.get("reason")
    assert len(seen) <= sheets, f"{len(seen)} sheet parses for {sheets} sheet files"
    assert len(seen) == len(set(seen)), "a sheet file was parsed twice"


def test_the_annotation_check_parses_each_vme_wren_sheet_at_most_once(kicad_demo, monkeypatch) -> None:
    root = kicad_demo("vme-wren") / "vme-wren.kicad_sch"
    sheets = len(sheet_tree.walk(root).files)

    with counted_sheet_parses(monkeypatch) as seen:
        problems = gate._annotation_problems(root)

    assert problems == []
    assert len(seen) <= sheets, f"{len(seen)} sheet parses for {sheets} sheet files"


def test_one_walk_serves_every_reader_of_its_tree(kicad_demo, monkeypatch) -> None:
    root = kicad_demo("vme-wren") / "vme-wren.kicad_sch"
    with counted_sheet_parses(monkeypatch) as seen:
        tree = sheet_tree.walk(root)
        walked = len(seen)
        sheet_tree.placed_instances(tree)
        sheet_tree.placed_instances(tree, on_board_only=True)
        sheet_tree.legacy_table_problems(tree)
        sheet_tree.netlist_disagreement(tree, [])
    assert walked == len(tree.files)
    assert len(seen) == walked, "a reader of the tree parsed a sheet again"


def test_a_file_edited_between_two_calls_is_seen_by_the_second(kicad_demo) -> None:
    project = kicad_demo("complex_hierarchy")
    root = project / "complex_hierarchy.kicad_sch"
    sub = project / "ampli_ht.kicad_sch"

    before = sheet_tree.placed_instances(sheet_tree.walk(root))
    assert "EDITED" not in {r["value"] for r in before}

    _first_part(sub, "EDITED")

    after = sheet_tree.placed_instances(sheet_tree.walk(root))
    assert "EDITED" in {r["value"] for r in after}


def test_the_annotation_check_reads_an_edit_made_between_two_calls(kicad_demo) -> None:
    project = kicad_demo("complex_hierarchy")
    root = project / "complex_hierarchy.kicad_sch"
    sub = project / "ampli_ht.kicad_sch"
    assert gate._annotation_problems(root) == []

    doc = Document.load(sub)
    for symbol in doc.root.find_children("symbol"):
        for prop in symbol.find_children("property"):
            if prop.get_string(0) == "Reference" and not (prop.get_string(1) or "").startswith("#"):
                prop.set_value(1, "R?")
                break
        else:
            continue
        break
    for symbol in doc.root.find_children("symbol"):  # the instance entries carry the reference too
        for instances in symbol.find_children("instances"):
            for proj in instances.find_children("project"):
                for path in proj.find_children("path"):
                    for ref in path.find_children("reference"):
                        if not (ref.get_string(0) or "").startswith("#"):
                            ref.set_value(0, "R?")
    doc.save(sub)

    assert any("not fully annotated" in p for p in gate._annotation_problems(root))


def test_a_tree_is_a_snapshot_of_the_call_that_walked_it(kicad_demo) -> None:
    """A file edited during one call is the caller's explicit choice: the tree keeps the parse it
    walked, and a caller that wants the edit walks again. No cache outlives the tree."""
    project = kicad_demo("complex_hierarchy")
    root = project / "complex_hierarchy.kicad_sch"
    tree = sheet_tree.walk(root)
    _first_part(project / "ampli_ht.kicad_sch", "EDITED")

    assert "EDITED" not in {r["value"] for r in sheet_tree.placed_instances(tree)}
    assert "EDITED" in {r["value"] for r in sheet_tree.placed_instances(sheet_tree.walk(root))}


def test_no_module_level_parse_cache() -> None:
    """The memo lives on the tree or in a dict the call passes: no module-level dict of parses."""
    for module in (sheet_tree, si, gate):
        holders = [
            name
            for name, value in vars(module).items()
            if isinstance(value, dict) and not name.startswith("__") and "pars" in name.lower()
        ]
        assert holders == [], (module.__name__, holders)
