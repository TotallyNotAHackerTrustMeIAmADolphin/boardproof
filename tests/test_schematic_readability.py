"""The schematic readability checker (``kicad_mcp.schematic_readability``, build ticket 10).

The base is ``tests/fixtures/simple_led.kicad_sch`` loaded into memory with the project's write
layer (``write_layer.load``), its one diagonal wire redrawn as two orthogonal ones. The checker
takes the parsed root, so every mutant is an edit of that in-memory tree through the write
layer's typed setters and nothing here writes a KiCad file. Each rule has its own mutant: the
base passes every rule, the mutant makes that rule (and in these cases only that rule) fire.

Positions are proven on the raw tokens: ``test_pins_land_on_wire_ends`` checks that every pin
the checker computes (rotation and the library's y-up frame included) is a wire end or lies on a
wire of the KiCad-written fixtures.
"""
from __future__ import annotations

import copy
import hashlib
import shutil
from pathlib import Path

import pytest

from kicad_mcp import schematic_readability as sr
from kicad_mcp import write_layer

FIXTURES = Path(__file__).parent / "fixtures"
SIMPLE_LED = FIXTURES / "simple_led.kicad_sch"
GOLDEN = FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_sch"


# ----------------------------------------------------------------------------- helpers


def symbol(root, ref):
    for node in root.find_children("symbol"):
        for prop in node.find_children("property"):
            if prop.values[0] == "Reference" and prop.values[1] == ref:
                return node
    raise AssertionError(f"no symbol {ref}")


def prop(node, name):
    for p in node.find_children("property"):
        if p.values[0] == name:
            return p
    raise AssertionError(f"no property {name}")


def set_at(node, x, y):
    at = node.find("at")
    write_layer.set_number(at, 0, x)
    write_layer.set_number(at, 1, y)


def wire_with(root, a, b):
    for w in root.find_children("wire"):
        pts = [tuple(xy.values[:2]) for xy in w.find("pts").find_children("xy")]
        if pts == [a, b]:
            return w
    raise AssertionError(f"no wire {a} -> {b}")


def set_wire(wire, a, b):
    for xy, point in zip(wire.find("pts").find_children("xy"), (a, b), strict=True):
        write_layer.set_number(xy, 0, point[0])
        write_layer.set_number(xy, 1, point[1])


def clean_sheet():
    """simple_led with its diagonal LED_ANODE wire redrawn as a horizontal and a vertical one."""
    root = write_layer.load(SIMPLE_LED)
    diagonal = wire_with(root, (100.33, 58.42), (104.14, 90.17))
    vertical = copy.deepcopy(diagonal)
    set_wire(diagonal, (100.33, 58.42), (104.14, 58.42))
    set_wire(vertical, (104.14, 58.42), (104.14, 90.17))
    root.append(vertical)
    return root


def rules_of(report):
    return {v.rule for v in report.violations}


def hide(prop_node):
    """Hide a property the way KiCad 10 writes it: a ``(hide yes)`` node (copied from the
    Datasheet property of the connector, which KiCad wrote hidden)."""
    template = prop(symbol(clean_sheet(), "J1"), "Datasheet").find("hide")
    assert template is not None
    prop_node.children.insert(2, copy.deepcopy(template))


# ----------------------------------------------------------------------------- the base


def test_clean_base_passes_every_rule():
    report = sr.check_sheet(clean_sheet(), name="base")
    assert report.violations == ()
    assert report.passed and report.symbols == 7 and report.wires == 10
    assert report.by_rule() == {rule: 0 for rule in sr.RULES}


def test_the_fixture_as_written_has_its_one_diagonal_wire():
    report = sr.check_sheet(SIMPLE_LED)  # a path: loaded through the write layer
    assert [v.rule for v in report.violations] == ["wire_not_orthogonal"]
    message = report.violations[0].message
    assert "(100.33, 58.42) to (104.14, 90.17)" in message and "(104.14, 58.42)" in message  # the fix is named


def test_check_is_read_only(tmp_path):
    copy_path = tmp_path / "simple_led.kicad_sch"
    shutil.copy(SIMPLE_LED, copy_path)
    before = hashlib.sha256(copy_path.read_bytes()).hexdigest()
    sr.check_sheet(copy_path)
    assert hashlib.sha256(copy_path.read_bytes()).hexdigest() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["simple_led.kicad_sch"]


def test_pins_land_on_wire_ends():
    """The raw-token oracle for placement: every pin of the KiCad-written fixtures, computed from
    the library pin, the symbol's (at x y angle) and the y-up flip, is where KiCad's author
    attached something to it: a wire, a label or a no-connect flag (read from their own tokens)."""
    for path in (SIMPLE_LED, GOLDEN):
        root = write_layer.load(path)
        model = sr._read_sheet(root, path.name)
        flags = [tuple(n.find("at").values[:2]) for tag in ("no_connect", "label", "global_label")
                 for n in root.find_children(tag)]
        assert model.pins and model.wires
        for ref, number, point, _power in model.pins:
            on_wire = any(sr._on_segment(point, a, b) for a, b in model.wires)
            flagged = any(sr._key(point) == sr._key(f) for f in flags)
            assert on_wire or flagged, (path.name, ref, number, point)
    # the 180-degree GND symbol of simple_led is in the set and lands on its wire end
    model = sr._read_sheet(write_layer.load(SIMPLE_LED), "simple_led")
    assert any(ref == "#PWR02" and abs(p[0] - 25.4) < 0.01 and abs(p[1] - 129.54) < 0.01
               for ref, _n, p, _pw in model.pins)


# ----------------------------------------------------------------------------- one mutant per rule


def test_symbol_overlap_fires_and_names_both_parts():
    root = clean_sheet()
    set_at(symbol(root, "R1"), 100.33, 88.9)  # onto the LED
    report = sr.check_sheet(root)
    hits = [v for v in report.violations if v.rule == "symbol_overlap"]
    assert len(hits) == 1
    assert set(hits[0].refs) == {"R1", "D1"}
    assert "move one of them apart" in hits[0].message


def test_symbol_overlap_ignores_touching_and_power_symbols():
    # the base has pin stubs meeting at shared wire ends and power symbols on pins: no overlap
    assert "symbol_overlap" not in rules_of(sr.check_sheet(clean_sheet()))


def test_text_overlap_between_two_texts_of_one_symbol():
    root = clean_sheet()
    r1 = symbol(root, "R1")
    ref_at = prop(r1, "Reference").find("at").values
    set_at(prop(r1, "Value"), ref_at[0], ref_at[1])  # the Value text on the Reference text
    hits = [v for v in sr.check_sheet(root).violations if v.rule == "text_overlap"]
    assert hits and "R1 Reference 'R1' overlaps R1 Value '330'" in hits[0].message


def test_text_overlap_of_a_property_on_another_symbol():
    root = clean_sheet()
    set_at(prop(symbol(root, "R1"), "Value"), 100.33, 90.17)  # R1's value on the LED's body
    hits = [v for v in sr.check_sheet(root).violations if v.rule == "text_overlap"]
    assert any("R1 Value '330' lies on top of D1" in v.message for v in hits)


def test_text_overlap_of_two_labels():
    root = clean_sheet()
    vcc = next(label for label in root.find_children("label") if label.values[0] == "VCC")
    gnd = next(label for label in root.find_children("label") if label.values[0] == "GND")
    set_at(gnd, *vcc.find("at").values[:2])
    hits = [v for v in sr.check_sheet(root).violations if v.rule == "text_overlap"]
    assert any("label 'VCC' overlaps label 'GND'" in v.message for v in hits)


def test_text_just_beside_another_is_not_an_overlap():
    root = clean_sheet()
    r1 = symbol(root, "R1")
    ref_at = prop(r1, "Reference").find("at").values
    set_at(prop(r1, "Value"), ref_at[0], ref_at[1] + 3.0)  # a clear line below
    assert "text_overlap" not in rules_of(sr.check_sheet(root))


def test_wire_not_orthogonal_names_the_corner_to_use():
    root = clean_sheet()
    set_wire(wire_with(root, (100.33, 58.42), (104.14, 58.42)), (100.33, 58.42), (104.14, 60.96))
    hits = [v for v in sr.check_sheet(root).violations if v.rule == "wire_not_orthogonal"]
    assert len(hits) == 1 and "diagonal" in hits[0].message and "(104.14, 58.42)" in hits[0].message


def test_wire_off_grid_names_the_end_and_the_grid_point():
    root = clean_sheet()
    # a wire end 0.2 mm off the 1.27 mm grid; the net stays orthogonal
    set_wire(wire_with(root, (100.33, 50.8), (100.33, 30.48)), (100.33, 50.8), (100.33, 30.28))
    hits = [v for v in sr.check_sheet(root).violations if v.rule == "wire_off_grid"]
    assert len(hits) == 1
    assert "(100.33, 30.28)" in hits[0].message and "move to (100.33, 30.48)" in hits[0].message


def test_unlabelled_long_net_between_blocks():
    root = clean_sheet()
    label = next(lab for lab in root.find_children("label") if lab.values[0] == "LED_ANODE")
    root.remove(label)
    assert "net_unlabelled_between_blocks" not in rules_of(sr.check_sheet(root))  # 35.6 mm: not long
    hits = [v for v in sr.check_sheet(root, long_net_mm=20.0).violations
            if v.rule == "net_unlabelled_between_blocks"]
    assert len(hits) == 1
    assert hits[0].refs == ("D1", "R1")
    assert "D1.2, R1.2" in hits[0].message
    assert "add a net label" in hits[0].message


def test_a_label_or_a_power_symbol_labels_the_net():
    root = clean_sheet()
    assert "net_unlabelled_between_blocks" not in rules_of(sr.check_sheet(root, long_net_mm=1.0))
    # the J1-to-power nets carry power symbols and labels; the only others are the LED_ANODE net's


def test_value_not_visible_when_hidden():
    root = clean_sheet()
    hide(prop(symbol(root, "R1"), "Value"))
    hits = [v for v in sr.check_sheet(root).violations if v.rule == "value_not_visible"]
    assert len(hits) == 1
    assert "R1" in hits[0].message and "hides its Value '330'" in hits[0].message


def test_value_not_visible_when_empty():
    root = clean_sheet()
    write_layer.set_string(prop(symbol(root, "D1"), "Value"), 1, "")
    hits = [v for v in sr.check_sheet(root).violations if v.rule == "value_not_visible"]
    assert len(hits) == 1 and "D1" in hits[0].message and "no Value text" in hits[0].message


def test_old_style_hide_inside_effects_is_hidden_too():
    root = clean_sheet()
    effects = prop(symbol(root, "R1"), "Value").find("effects")
    effects.children.append(write_layer.symbol("hide"))  # KiCad 6/7 wrote a bare "hide" in (effects ...)
    assert "value_not_visible" in rules_of(sr.check_sheet(root))


def add_model_property(root, ref, name, text, hidden):
    node = symbol(root, ref)
    new = copy.deepcopy(prop(node, "Value"))
    write_layer.set_string(new, 0, name)
    write_layer.set_string(new, 1, text)
    if hidden:
        hide(new)
    node.children.insert(node.children.index(prop(node, "Value")) + 1, new)


def test_model_name_not_visible_when_only_a_hidden_property_holds_it():
    root = clean_sheet()
    add_model_property(root, "D1", "Sim.Name", "D_RED", hidden=True)
    hits = [v for v in sr.check_sheet(root).violations if v.rule == "model_name_not_visible"]
    assert len(hits) == 1
    assert "simulation model 'D_RED'" in hits[0].message and "make the Value text 'D_RED'" in hits[0].message


def test_model_name_is_visible_by_its_own_property_or_by_the_value():
    shown = clean_sheet()
    add_model_property(shown, "D1", "Sim.Name", "D_RED", hidden=False)
    assert "model_name_not_visible" not in rules_of(sr.check_sheet(shown))
    value_names_it = clean_sheet()
    add_model_property(value_names_it, "D1", "Sim.Name", "LED", hidden=True)  # the Value reads "LED"
    assert "model_name_not_visible" not in rules_of(sr.check_sheet(value_names_it))


# ----------------------------------------------------------------------------- the guard rails


def test_a_symbol_missing_from_lib_symbols_is_named_not_skipped():
    root = clean_sheet()
    write_layer.set_string(symbol(root, "R1").find("lib_id"), 0, "Device:Nope")
    with pytest.raises(sr.ReadabilityError, match="R1.*Device:Nope"):
        sr.check_sheet(root)


def test_a_non_schematic_root_is_refused():
    with pytest.raises(sr.ReadabilityError, match="not a kicad_sch"):
        sr.check_sheet(write_layer.load(FIXTURES / "simple_led.kicad_pcb"))


def test_a_malformed_coordinate_is_refused():
    root = clean_sheet()
    write_layer.set_string(symbol(root, "R1").find("at"), 0, "NaN")
    with pytest.raises(sr.ReadabilityError, match="not a finite number"):
        sr.check_sheet(root)


def test_check_project_walks_the_sheet_tree_and_reports_a_problem(tmp_path):
    project = tmp_path / "p"
    shutil.copytree(GOLDEN.parent, project)
    report = sr.check_project(project / GOLDEN.name)
    assert [s.sheet for s in report.sheets] == [GOLDEN.name] and not report.problems
    summary = sr.summarise([report])
    assert summary["sheets"] == 1 and set(summary["violations_by_rule"]) == set(sr.RULES)
    missing = sr.check_project(project / "absent.kicad_sch")
    assert missing.problems and not missing.passed and not missing.sheets


def test_main_prints_the_baseline(tmp_path, capsys):
    project = tmp_path / "p"
    shutil.copytree(GOLDEN.parent, project)
    assert sr.main([str(project)]) == 0
    out = capsys.readouterr().out
    assert "sheets in 1 projects pass" in out and "text_overlap:" in out


# ----------------------------------------------------------------------------- KiCad's demos


def demo_roots(demos: Path) -> list[Path]:
    return sorted(p.with_suffix(".kicad_sch") for p in demos.rglob("*.kicad_pro")
                  if p.with_suffix(".kicad_sch").is_file())


def test_demo_sheets_baseline(kicad_demos_dir, capsys):
    """The recorded baseline over KiCad's demo projects (a baseline, not a target): every sheet
    reads without error, and the counts print. The ticket's Answer records the numbers."""
    roots = demo_roots(kicad_demos_dir)
    assert roots, "no demo project with a root sheet"
    reports = [sr.check_project(root) for root in roots]
    assert all(not r.problems for r in reports), [p for r in reports for p in r.problems]
    summary = sr.summarise(reports)
    with capsys.disabled():
        print(f"\nreadability baseline: {summary}")
    assert summary["sheets"] >= len(roots)
    assert all(v.at_mm is not None and v.message for r in reports for s in r.sheets for v in s.violations)
