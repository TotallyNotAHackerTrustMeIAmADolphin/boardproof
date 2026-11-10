"""Build ticket 153: ``board_view`` reads only the blocks a reader asks for, with the same answers.

Equality is checked against a full ``parse_file`` of the same board: the eager nodes, the
copper nodes and the results of the five readers of ``schematic_import`` that use the view.
Every guard of the splitter has a test that goes red when the guard is removed (the mutants run
in this work are listed in the ticket's Answer). Boards come from the repo fixtures and from
KiCad's demos; nothing here writes a KiCad file except through ``tmp_path`` copies of text built
by string, which the view only reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from kicad_tools.sexp import parse_file, parse_string

from kicad_mcp import board_view as bv
from kicad_mcp import schematic_import as si

FIXTURES = Path(__file__).resolve().parent / "fixtures"
BOARDS = ["simple_led.kicad_pcb", "krt_stress_congestion.kicad_pcb", "krt_pad_geometry_isolated.kicad_pcb", "graphics_removal_board.kicad_pcb"]
DEMO_BOARDS = [("cm5_minima", "CM5_MINIMA_3.kicad_pcb"), ("sonde xilinx", "sonde xilinx.kicad_pcb"), ("complex_hierarchy", "complex_hierarchy.kicad_pcb")]


def shape(node: Any) -> Any:
    """A comparable form of a node: name, values (type and text) and children."""
    return (node.name, [(type(v).__name__, str(v)) for v in node.values if not hasattr(v, "children")], [shape(c) for c in node.children])


def _trimmed(footprint: Any) -> Any:
    """A footprint as the view keeps it (stated here, not imported): its head, ``at``, ``fp_text``,
    ``pad`` and the ``Reference`` property."""
    name, values, kids = shape(footprint)
    keep = []
    for child, kid in zip(footprint.children, kids, strict=True):
        if child.name is None or child.name in ("at", "fp_text", "pad"):
            keep.append(kid)
        elif child.name == "property" and (not child.values or child.values[0] == "Reference"):
            keep.append(kid)
    return (name, values, keep)


def _wanted(root: Any, names: frozenset[str]) -> list[Any]:
    return [shape(c) for c in root.children if c.name in names]


def _assert_same_as_full(path: Path) -> bv.BoardView:
    full = parse_file(str(path))
    view = bv.read_board_view(path)
    wanted = [c for c in full.children if c.name in bv.EAGER]
    assert len(view.children) == len(wanted)
    for got, want in zip(view.children, wanted, strict=True):
        assert shape(got) in (shape(want), _trimmed(want)), want.name
        if want.name == "footprint":
            assert (got.find("at") is None) == (want.find("at") is None)
            assert shape(got.find("at")) == shape(want.find("at"))
    assert [shape(c) for c in view.copper_children()] == _wanted(full, bv.COPPER)
    # the five readers of the import give the same result on the view as on the full tree
    assert si._footprint_facts(view) == si._footprint_facts(full)
    assert si._outline_bbox(view) == si._outline_bbox(full)
    assert si._pad_nets(view) == si._pad_nets(full)
    assert si._net_table(view) == si._net_table(full)
    names = set(si._net_table(full).values()) | {"GND", "+5V", "no-such-net"}
    assert si._copper_on_nets(view, names) == _full_copper_on_nets(full, names)
    return view


def _full_copper_on_nets(full: Any, names: set[str]) -> list[str]:
    class Whole:
        children = full.children

        @staticmethod
        def copper_children() -> list[Any]:
            return [c for c in full.children if c.name in bv.COPPER]

    return si._copper_on_nets(Whole, names)


@pytest.mark.parametrize("name", BOARDS)
def test_the_view_equals_a_full_parse_on_fixture_boards(name: str) -> None:
    view = _assert_same_as_full(FIXTURES / name)
    assert view.fast, "a plain KiCad board must take the block reader, not the fallback"


@pytest.mark.parametrize("demo,board", DEMO_BOARDS)
def test_the_view_equals_a_full_parse_on_demo_boards(kicad_demos_dir: Path, demo: str, board: str) -> None:
    path = kicad_demos_dir / demo / board
    if not path.is_file():
        pytest.skip(f"{path} missing")
    view = _assert_same_as_full(path)
    assert view.fast


def test_the_view_equals_a_full_parse_on_the_71_mb_vme_wren_board(kicad_demos_dir: Path) -> None:
    view = _assert_same_as_full(kicad_demos_dir / "vme-wren" / "vme-wren.kicad_pcb")
    assert view.fast
    assert len(view.children) > 1000


def test_the_view_reads_the_file_every_call_and_never_serves_a_stale_one(tmp_path: Path) -> None:
    import shutil

    from kicad_mcp import write_layer

    board = tmp_path / "b.kicad_pcb"
    shutil.copy(FIXTURES / "simple_led.kicad_pcb", board)
    assert si._net_table(bv.read_board_view(board)) == {0: "", 1: "VCC", 2: "LED_ANODE", 3: "GND"}
    root = write_layer.load(board)
    net = next(c for c in root.children if c.name == "net" and str(c.values[0]) == "2")
    write_layer.set_string(net, 1, "RENAMED")  # a write through the write layer, between two reads
    write_layer.save(root, board)
    assert si._net_table(bv.read_board_view(board)) == {0: "", 1: "VCC", 2: "RENAMED", 3: "GND"}


# --- the splitter's guards: each falls back to the full parse, never to a guess ---------------

PLAIN = '(kicad_pcb (version 1) (footprint "L:F" (at 1 2) (property "Reference" "R1")) (segment (net 1)))'


def _split(text: str) -> Any:
    return bv.split_top_level(text)


def test_a_plain_document_is_split_into_its_top_level_blocks() -> None:
    root, blocks = _split(PLAIN)
    assert root == "kicad_pcb"
    assert [(n, PLAIN[s:e][:9]) for n, s, e, _ in blocks] == [("version", "(version "), ("footprint", "(footprin"), ("segment", "(segment ")]


def test_a_parenthesis_inside_a_string_does_not_end_a_block() -> None:
    text = '(kicad_pcb (footprint "a)(b" (at 1 2)) (net 1 "x(y"))'
    root, blocks = _split(text)
    assert [n for n, _, _, _ in blocks] == ["footprint", "net"]
    assert text[blocks[0][1] : blocks[0][2]] == '(footprint "a)(b" (at 1 2))'


def test_an_escaped_quote_inside_a_string_does_not_end_it() -> None:
    text = '(kicad_pcb (footprint "a' + chr(92) + '"(b" (at 1 2)) (net 1 "x"))'
    root, blocks = _split(text)
    assert [n for n, _, _, _ in blocks] == ["footprint", "net"]
    view = bv.view_of_text(text)
    assert view.fast and [shape(c) for c in view.children] == _wanted(parse_string(text), bv.EAGER)
    _agree(text)  # the fast finder and the exact loop both honour the escape


@pytest.mark.parametrize(
    "text",
    [
        '(kicad_pcb (net 1 "a") ; comment\n (net 2 "b"))',  # comment outside a string
        '(kicad_pcb (net 1 "unterminated))',  # unterminated string
        "(kicad_pcb (net 1 a)",  # unbalanced: missing close
        "(kicad_pcb (net 1 a)))",  # unbalanced: extra close
        "(kicad_pcb (net 1 a)) (kicad_pcb)",  # content after the root
        "(kicad_pcb (net 1 a)) junk",  # a bare atom after the root
        "junk (kicad_pcb (net 1 a))",  # content before the root
        "",  # empty
    ],
)
def test_a_document_the_tokenizer_is_not_sure_about_is_not_split(text: str) -> None:
    assert _split(text) is None
    assert bv._split_slow(text) is None  # the exact loop itself refuses, whatever the fast finder did


def test_a_document_that_is_not_split_is_fully_parsed_with_the_same_answer() -> None:
    text = '(kicad_pcb (net 1 "a") ; comment\n (net 2 "b") (segment (net 1)))'
    view = bv.view_of_text(text)
    full = parse_string(text)
    assert not view.fast
    assert [shape(c) for c in view.children] == [shape(c) for c in full.children]
    assert [shape(c) for c in view.copper_children()] == _wanted(full, bv.COPPER)


def test_a_root_that_is_not_a_board_is_fully_parsed() -> None:
    view = bv.view_of_text('(kicad_sch (net 1 "a"))')
    assert not view.fast
    assert [c.name for c in view.children] == ["net"]


def test_copper_is_not_parsed_until_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = bv._parse_text
    monkeypatch.setattr(bv, "_parse_text", lambda t: (calls.append(t), real(t))[1])
    view = bv.view_of_text(PLAIN)
    assert len(calls) == 1 and "segment" not in calls[0], calls
    assert [c.name for c in view.copper_children()] == ["segment"]
    assert len(calls) == 2 and "segment" in calls[1]
    view.copper_children()
    assert len(calls) == 2, "the copper is parsed once"


def test_blocks_keep_the_file_order() -> None:
    text = '(kicad_pcb (footprint "b" (at 2 2)) (net 1 "x") (footprint "a" (at 1 1)) (segment (net 1)) (via (net 1)))'
    view = bv.view_of_text(text)
    assert [(c.name, str(c.values[0]) if c.values else "") for c in view.children] == [("footprint", "b"), ("net", "1"), ("footprint", "a")]
    assert [c.name for c in view.copper_children()] == ["segment", "via"]


# --- the netlist import file is parsed once, with the same result ------------------------------


def test_the_default_exporter_parses_the_netlist_file_once(kicad_demos_dir: Path, tmp_path: Path, monkeypatch) -> None:
    import shutil

    src = kicad_demos_dir / "cm5_minima"
    if not src.is_dir():
        pytest.skip("cm5_minima missing")
    shutil.copytree(src, tmp_path / "p")
    sch = tmp_path / "p" / "CM5_MINIMA_3.kicad_sch"
    parsed: list[str] = []
    real = si._parse
    monkeypatch.setattr(si, "_parse", lambda p: (parsed.append(Path(p).name), real(p))[1])
    import kicad_tools.operations.netlist as nl

    loads: list[Path] = []
    real_load = nl.Netlist.load.__func__
    monkeypatch.setattr(nl.Netlist, "load", classmethod(lambda cls, p: (loads.append(Path(p)), real_load(cls, p))[1]))

    netlist, raw, nodes = si._load_netlist(sch, None)

    assert parsed == ["import.kicad_net"], parsed
    assert loads == [], "Netlist.load would parse the file a second time"
    assert raw and nodes and netlist.components

    # same netlist as the old three-parse route
    out = tmp_path / "old.kicad_net"
    old = si._kicad_cli_netlist(sch, out)
    assert si._netlist_digest(old) == si._netlist_digest(netlist)
    assert si._netlist_components(real(out)) == raw
    assert si._netlist_nodes(real(out)) == nodes


# --- the footprint trimming: only what the readers use, and whole when unsure -------------------

FP = (
    '(kicad_pcb (footprint "L:F" (layer "F.Cu") (descr "d") (at 1 2 90) '
    '(property "Reference" "R1" (at 0 0)) (fp_line (start 0 0) (end 1 1) (layer "F.SilkS")) '
    '(pad "1" smd rect (at 0 0) (size 1 1) (net 3 "N")) (model "m.step" (offset (xyz 0 0 0)))%s))'
)


def test_a_footprint_keeps_its_head_and_the_children_the_readers_use() -> None:
    view = bv.view_of_text(FP % "")
    fp = view.children[0]
    assert fp.values[0] == "L:F"
    assert [c.name for c in fp.children if c.name] == ["at", "property", "pad"]
    assert si._footprint_facts(view) == {"R1": {"name": "L:F", "at": [1.0, 2.0]}}
    assert si._pad_nets(view) == {("R1", "1"): {"N"}}


def test_a_footprint_with_a_dropped_child_holding_an_at_is_kept_whole() -> None:
    text = '(kicad_pcb (footprint "L:F" (fp_poly (pts (xy 0 0)) (at 9 9)) (at 1 2) (fp_line (start 0 0))))'
    fp = bv.view_of_text(text).children[0]
    assert [c.name for c in fp.children] == [c.name for c in parse_string(text).children[0].children]
    # an `at` mentioned in a dropped child's string counts too: unsure means whole
    text2 = '(kicad_pcb (footprint "L:F" (descr "see (at 1 1) here") (at 1 2) (fp_line (start 0 0))))'
    assert "descr" in [c.name for c in bv.view_of_text(text2).children[0].children]
    # a dropped child without any `at` is dropped
    assert "fp_line" not in [c.name for c in bv.view_of_text(FP % "").children[0].children]


def test_a_footprint_without_children_is_kept_as_it_is() -> None:
    view = bv.view_of_text('(kicad_pcb (footprint "L:F"))')
    assert shape(view.children[0]) == shape(parse_string('(kicad_pcb (footprint "L:F"))').children[0])


# --- the two block finders agree; the fast one declines what it does not match whole --------------


def _agree(text: str) -> None:
    fast, slow = bv._split_fast(text), bv._split_slow(text)
    assert fast is not None and fast == slow


@pytest.mark.parametrize("name", BOARDS)
def test_both_block_finders_agree_on_fixture_boards(name: str) -> None:
    _agree((FIXTURES / name).read_text(encoding="utf-8"))


@pytest.mark.parametrize("demo,board", DEMO_BOARDS + [("vme-wren", "vme-wren.kicad_pcb")])
def test_both_block_finders_agree_on_demo_boards(kicad_demos_dir: Path, demo: str, board: str) -> None:
    path = kicad_demos_dir / demo / board
    if not path.is_file():
        pytest.skip(f"{path} missing")
    _agree(path.read_text(encoding="utf-8"))


def test_the_fast_finder_declines_a_comment_a_deep_block_and_an_unterminated_string() -> None:
    assert bv._split_fast('(kicad_pcb (net 1 "a") ; c\n (net 2 "b"))') is None
    deep = "(kicad_pcb (footprint \"F\" (at 1 2) " + "(x " * (bv._NEST + 3) + ")" * (bv._NEST + 3) + "))"
    assert bv._split_fast(deep) is None
    assert bv._split_slow(deep) is not None
    view = bv.view_of_text(deep)
    assert view.fast and [c.name for c in view.children] == ["footprint"]  # the loop decided, same blocks
    assert bv._split_fast('(kicad_pcb (net 1 "a))') is None


def test_a_comment_character_at_a_token_start_is_not_taken_for_an_atom() -> None:
    assert bv._split_fast("(kicad_pcb (net 1 #x))") is None  # '#' starts a comment for the parser
    assert bv._split_fast("(kicad_pcb (net 1 a#b))") is not None  # inside an atom it is part of the atom
    assert bv._split_fast('(kicad_pcb (net 1 "#x"))') is not None  # inside a string it is text


def test_a_hash_inside_an_atom_or_string_is_split_and_equals_the_full_parse() -> None:
    text = '(kicad_pcb (footprint "F" (at 1 2) (property "Reference" "#PWR01")) (net 1 a#b))'
    view = bv.view_of_text(text)
    assert view.fast
    assert si._footprint_facts(view) == si._footprint_facts(parse_string(text))


# --- footprint trimming rules ---------------------------------------------------------------------


def _fp(*children: str) -> str:
    return '(kicad_pcb (footprint "L:F" ' + " ".join(children) + "))"


def test_only_the_reference_property_is_kept() -> None:
    text = _fp('(at 1 2)', '(property "Value" "10k" (at 0 0))', '(property "Reference" "R7" (at 0 0))')
    fp = bv.view_of_text(text).children[0]
    props = [c for c in fp.children if c.name == "property"]
    assert [str(p.values[1]) for p in props] == ["R7"]
    assert si._footprint_facts(bv.view_of_text(text)) == {"R7": {"name": "L:F", "at": [1.0, 2.0]}}


def test_a_property_whose_name_is_not_a_plain_quoted_string_is_kept() -> None:
    text = _fp('(at 1 2)', '(property Reference R8)', '(property "Refe\\rence" "x")', '(property ki_fp_filters "R_*")')
    fp = bv.view_of_text(text).children[0]
    assert len([c for c in fp.children if c.name == "property"]) == 2  # a bare Reference and an escaped name; the bare ki_fp_filters goes


def test_a_footprint_without_a_direct_at_is_kept_whole() -> None:
    text = _fp('(property "Value" "v" (at 3 4))', '(property "Reference" "R9" (at 5 6))')
    view = bv.view_of_text(text)
    assert shape(view.children[0]) == shape(parse_string(text).children[0])
    assert si._footprint_facts(view) == si._footprint_facts(parse_string(text))  # the property's `at`, as before


def test_a_dropped_child_with_an_at_before_the_direct_at_keeps_the_footprint_whole() -> None:
    text = _fp('(property "Value" "v" (at 3 4))', '(at 1 2)', '(property "Reference" "R9" (at 5 6))')
    view = bv.view_of_text(text)
    assert si._footprint_facts(view) == si._footprint_facts(parse_string(text)) == {"R9": {"name": "L:F", "at": [3.0, 4.0]}}


def test_a_dropped_child_with_an_at_after_the_direct_at_is_dropped() -> None:
    text = _fp('(at 1 2)', '(property "Value" "v" (at 3 4))', '(property "Reference" "R9" (at 5 6))')
    view = bv.view_of_text(text)
    assert len([c for c in view.children[0].children if c.name == "property"]) == 1
    assert si._footprint_facts(view) == si._footprint_facts(parse_string(text))


# --- placed_instances is read once per plan (ticket 153) ------------------------------------------


def _copy_demo(kicad_demos_dir: Path, tmp_path: Path, name: str) -> Path:
    import shutil

    src = kicad_demos_dir / name
    if not src.is_dir():
        pytest.skip(f"{src} missing")
    shutil.copytree(src, tmp_path / name)
    return tmp_path / name


def test_check_schematic_reads_the_placed_instances_once(kicad_demos_dir: Path, tmp_path: Path, monkeypatch) -> None:
    from kicad_mcp import sheet_tree

    sch = _copy_demo(kicad_demos_dir, tmp_path, "complex_hierarchy") / "complex_hierarchy.kicad_sch"
    netlist, raw, nodes = si._load_netlist(sch, None)
    calls: list[int] = []
    real = sheet_tree.placed_instances

    def counting(*a, **k):
        calls.append(1)
        return real(*a, **k)

    monkeypatch.setattr(sheet_tree, "placed_instances", counting)
    monkeypatch.setattr(si, "placed_instances", counting)
    tree = si._check_schematic(sch, netlist, raw, nodes)
    assert tree is not None
    assert len(calls) == 1, f"{len(calls)} passes over the sheets"


def test_netlist_disagreement_gives_the_same_answer_with_records_passed_in(kicad_demos_dir: Path, tmp_path: Path) -> None:
    from kicad_mcp import sheet_tree

    sch = _copy_demo(kicad_demos_dir, tmp_path, "complex_hierarchy") / "complex_hierarchy.kicad_sch"
    tree = sheet_tree.walk(sch)
    refs = [c["ref"] for c in sheet_tree.netlist_components(sch)]
    placed = sheet_tree.placed_instances(tree)
    for listed in (refs, refs[1:], refs + ["ZZ9"], refs + refs[:1]):
        assert sheet_tree.netlist_disagreement(tree, listed, records=placed) == sheet_tree.netlist_disagreement(tree, listed)
    assert sheet_tree.netlist_disagreement(tree, refs[1:], records=placed), "a missing part must be named"


@pytest.mark.parametrize("opener", ["(at 7 7 0)", "( at 7 7 0)", "(\n at 7 7 0)", "(\tat 7 7 0)"])
def test_a_spaced_at_in_a_dropped_child_before_the_direct_at_keeps_the_footprint_whole(opener: str) -> None:
    text = _fp(f'(property "Datasheet" "d" {opener})', '(at 1 2)', '(property "Reference" "J1" (at 5 6))')
    view = bv.view_of_text(text)
    full = parse_string(text)
    assert si._footprint_facts(view) == si._footprint_facts(full) == {"J1": {"name": "L:F", "at": [7.0, 7.0]}}


# One board per Edge.Cuts shape kind, named here (not read from OUTLINE_SHAPES): only that shape
# sets coordinates, and the exact box is stated. A kind dropped from the view's list, or from the
# import's, gives None and goes red.
OUTLINE_CASES = {
    "gr_line": ('(gr_line (start -5 -6) (end 7 8) (layer "Edge.Cuts"))', (-5.0, -6.0, 7.0, 8.0)),
    "gr_rect": ('(gr_rect (start 1 2) (end 30 40) (layer "Edge.Cuts"))', (1.0, 2.0, 30.0, 40.0)),
    "gr_arc": ('(gr_arc (start 0 0) (mid 5 3) (end 10 0) (layer "Edge.Cuts"))', (0.0, 0.0, 10.0, 3.0)),
    "gr_circle": ('(gr_circle (center 50 60) (end 60 60) (layer "Edge.Cuts"))', (40.0, 50.0, 60.0, 70.0)),
    "gr_poly": ('(gr_poly (pts (xy 3 4) (xy 20 4) (xy 20 25)) (layer "Edge.Cuts"))', (3.0, 4.0, 20.0, 25.0)),
    # a (pts (xy ..)) curve makes _outline_bbox raise, on the full parse too
    "gr_curve": ('(gr_curve (start -40 5) (end 3 3) (layer "Edge.Cuts"))', (-40.0, 3.0, 3.0, 5.0)),
}


@pytest.mark.parametrize("kind", sorted(OUTLINE_CASES))
def test_each_edge_cuts_shape_kind_alone_sets_the_exact_outline_box(kind: str) -> None:
    shape_text, box = OUTLINE_CASES[kind]
    text = f'(kicad_pcb {shape_text} (gr_line (start 999 999) (end 1000 1000) (layer "F.SilkS")))'
    assert si._outline_bbox(parse_string(text)) == box
    assert si._outline_bbox(bv.view_of_text(text)) == box


def test_the_outline_shapes_are_one_list_for_the_view_and_the_import() -> None:
    assert si.OUTLINE_SHAPES is bv.OUTLINE_SHAPES
