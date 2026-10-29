"""The gate's netlist count guard (build ticket 50; docs/ARCHITECTURE.md decision 14).

A raw line break inside a quoted string of a sub-sheet makes KiCad leave the whole sheet
out of the design, and ``kicad-cli`` still exits 0 (mixelpixx #423, reproduced on 10.0.7:
``complex_hierarchy`` 68 to 10 components). Schematic parity then compares the board with a
truncated netlist, and KiCad's default parity severities are warnings, so the gate passed.

The guard only lowers a PASS to INCOMPLETE; a FAIL stays FAIL. Tests observe it through the
agreed seam ``verify_board()`` (S1). ``drc`` replaces only the three DRC runs; the guard's own
figures come from the real ``kicad-cli`` (netlist export, per-sheet load check). Expected
counts come from kicad-cli measurements recorded in the ticket, not from the code under test.

The fault is injected through kicad_tools' S-expression API. Its serialiser escapes a line
break in every string it writes, so the faulty atom carries its verbatim form
(``_original_str``, kicad_tools' round-trip hook for number atoms, written unchanged); the rest
of the file is serialised by kicad_tools. Never by hand (CLAUDE.md hard rule 1).
"""

from __future__ import annotations

import importlib.util
import shutil
from pathlib import Path

import pytest

from kicad_mcp import kicad_cli, sheet_tree
from kicad_mcp.verify_board import snapshot_rules, verify_board

ROOT = Path(__file__).resolve().parents[1]
RUNG1_INPUT = ROOT / "evals" / "rung1" / "layout_run" / "07_poured"
LF = chr(10)


# --- helpers -----------------------------------------------------------------------------


def _report(*, violations=None) -> dict:
    return {"violations": violations or [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _clearance_error() -> dict:
    return {
        "type": "clearance",
        "severity": "error",
        "description": "Clearance violation",
        "items": [{"uuid": "u1", "description": "Track on F.Cu", "pos": {"x": 1.0, "y": 2.0}}],
    }


class _FakeDrc:
    """Stands in for the three kicad-cli DRC runs only: one canned report for every run."""

    def __init__(self, report: dict) -> None:
        self._report = report

    def __call__(self, board, *, refill_zones, schematic_parity) -> dict:
        return self._report


def _clean_gate(pcb: Path, report: dict | None = None) -> dict:
    """The gate with clean (or the given) DRC reports and a pinned baseline: without the
    guard this is a PASS whenever ``report`` carries no error."""
    baseline = snapshot_rules(pcb, drc=_FakeDrc(_report()))
    return verify_board(pcb, parity="required", baseline=baseline, drc=_FakeDrc(report or _report()))


def _raw_line_break(text: str):
    """A string atom whose written form holds a raw line break, as a text-patching writer
    leaves it (mixelpixx #423)."""
    from kicad_tools.sexp.parser import SExp

    return SExp(value=0, _original_str='"' + text.replace("|", LF) + '"')


def _break_first_value(sheet: Path) -> None:
    """Put a raw line break into the Value property of the sheet's first placed symbol."""
    from kicad_tools.sexp import Document

    doc = Document.load(sheet)
    symbol = doc.root.find_children("symbol")[0]
    prop = next(p for p in symbol.find_children("property") if p.get_string(0) == "Value")
    prop.children[1] = _raw_line_break("line|break")
    doc.save(sheet)


def _guard_reasons(result: dict) -> list[str]:
    return [r for r in result["incomplete_reasons"] if "netlist" in r or "load" in r]


# --- reproduction, end to end on the real kicad-cli ----------------------------------------


def test_a_dropped_sub_sheet_no_longer_passes_the_real_gate(kicad_demo) -> None:
    """Reproduction (ticket 50 step 1). Before the guard, this board with real DRC, real
    parity and a pinned baseline was a PASS: kicad-cli's netlist held 10 of 68 components and
    parity reported only 58 ``extra_footprint`` warnings."""
    project = kicad_demo("complex_hierarchy")
    pcb = project / "complex_hierarchy.kicad_pcb"
    _break_first_value(project / "ampli_ht.kicad_sch")
    baseline = snapshot_rules(pcb)

    result = verify_board(pcb, parity="required", baseline=baseline)

    assert result["verdict"] == "INCOMPLETE"
    count = [r for r in result["incomplete_reasons"] if "netlist" in r and "68" in r and "10" in r]
    assert count and "ampli_ht.kicad_sch" in count[0], result["incomplete_reasons"]


# --- no false alarm: the demos of ticket 09, the rung-1 and the rung-2 input -------------------


def _stage_rung2(kicad_demo, tmp_path: Path) -> Path:
    spec = importlib.util.spec_from_file_location("stage_input", ROOT / "evals" / "rung2" / "stage_input.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return Path(module.stage(kicad_demo("pic_programmer"), tmp_path / "rung2")["board"])


@pytest.mark.parametrize(
    "demo,board",
    [
        ("complex_hierarchy", "complex_hierarchy.kicad_pcb"),  # one sheet used twice: 68 parts
        ("multichannel", "multichannel_mixer.kicad_pcb"),  # one sheet used four times
        ("vme-wren", "vme-wren.kicad_pcb"),  # 36 sheet files, 73 sheet uses, 1507 parts
        ("royalblue54L_feather", "RoyalBlue54L-Feather.kicad_pcb"),  # DNP and on_board no
        ("ecc83", "ecc83-pp.kicad_pcb"),  # multi-unit tubes
    ],
)
def test_a_healthy_demo_still_passes(kicad_demo, demo: str, board: str) -> None:
    pcb = kicad_demo(demo) / board

    result = _clean_gate(pcb)

    assert result["verdict"] == "PASS", result["incomplete_reasons"]


def test_the_rung1_input_still_passes(tmp_path: Path) -> None:
    project = tmp_path / "rung1"
    shutil.copytree(RUNG1_INPUT, project)

    result = _clean_gate(project / "rung1_usbc_attiny1616.kicad_pcb")

    assert result["verdict"] == "PASS", result["incomplete_reasons"]


def test_the_rung2_input_still_passes(kicad_demo, tmp_path: Path) -> None:
    result = _clean_gate(_stage_rung2(kicad_demo, tmp_path))

    assert result["verdict"] == "PASS", result["incomplete_reasons"]


# --- the guard lowers PASS, never FAIL -----------------------------------------------------


def test_a_dropped_sub_sheet_lowers_a_pass_and_names_the_sheet_uses(kicad_demo) -> None:
    """kicad-cli measured on this fault: 10 components, exit 0. Both uses of
    ``ampli_ht.kicad_sch`` lose their 29 parts (a sheet used twice counts twice)."""
    project = kicad_demo("complex_hierarchy")
    _break_first_value(project / "ampli_ht.kicad_sch")

    result = _clean_gate(project / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    count = next(r for r in result["incomplete_reasons"] if "netlist has 10 component(s)" in r)
    assert "places 68" in count
    assert count.count("ampli_ht.kicad_sch at /") == 2 and count.count("29 of 29 missing") == 2
    assert "C201" in count and "C301" in count


def test_a_fail_board_with_the_fault_stays_fail_and_still_reports_the_guard(kicad_demo) -> None:
    project = kicad_demo("complex_hierarchy")
    _break_first_value(project / "ampli_ht.kicad_sch")

    result = _clean_gate(project / "complex_hierarchy.kicad_pcb", _report(violations=[_clearance_error()]))

    assert result["verdict"] == "FAIL"
    assert len(result["violations"]) == 1
    assert any("netlist has 10 component(s)" in r for r in result["incomplete_reasons"])


def test_a_broken_root_sheet_is_incomplete(kicad_demo) -> None:
    """A raw line break in the root fails kicad-cli's load (exit 3): the guard cannot count."""
    project = kicad_demo("complex_hierarchy")
    _break_first_value(project / "complex_hierarchy.kicad_sch")

    result = _clean_gate(project / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    assert any("netlist count guard could not run" in r for r in result["incomplete_reasons"])


def test_a_guard_that_cannot_run_is_incomplete_and_keeps_fail_evidence(kicad_demo, monkeypatch) -> None:
    def broken(root):
        raise kicad_cli.KicadCliError("kicad-cli sch export netlist exited 3")

    monkeypatch.setattr(sheet_tree, "netlist_components", broken)
    pcb = kicad_demo("complex_hierarchy") / "complex_hierarchy.kicad_pcb"

    clean = _clean_gate(pcb)
    failing = _clean_gate(pcb, _report(violations=[_clearance_error()]))

    assert clean["verdict"] == "INCOMPLETE"
    assert any("could not run" in r and "exited 3" in r for r in clean["incomplete_reasons"])
    assert failing["verdict"] == "FAIL" and len(failing["violations"]) == 1


def test_a_netlist_listing_a_part_twice_is_incomplete(kicad_demo, monkeypatch) -> None:
    real = sheet_tree.netlist_components

    def doubled(root):
        components = real(root)
        return [*components, components[0]]

    monkeypatch.setattr(sheet_tree, "netlist_components", doubled)

    result = _clean_gate(kicad_demo("complex_hierarchy") / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    assert any("netlist has 69 component(s)" in r for r in result["incomplete_reasons"])


def test_a_placed_symbol_without_its_library_symbol_is_incomplete(kicad_demo) -> None:
    """kicad-cli leaves a placed symbol whose lib id has no ``lib_symbols`` entry out of the
    netlist, with exit 0 (measured on ticket 14's F6 fixture before ticket 50 fixed it)."""
    from kicad_tools.sexp import Document

    project = kicad_demo("complex_hierarchy")
    sheet = project / "ampli_ht.kicad_sch"
    doc = Document.load(sheet)
    symbol = next(
        s
        for s in doc.root.find_children("symbol")
        if any(p.get_string(0) == "Reference" and p.get_string(1).startswith("C") for p in s.find_children("property"))
    )
    symbol.find("lib_id").set_value(0, "Device:NotInThisFile")  # one symbol, two sheet uses
    doc.save(sheet)

    result = _clean_gate(project / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    assert any("netlist has 66 component(s)" in r and "places 68" in r for r in result["incomplete_reasons"])


# --- counting rules ------------------------------------------------------------------------


def _set_flag(sheet: Path, reference_prefix: str, flag: str, value: str) -> str:
    """Set ``(flag value)`` on the first symbol whose Reference property starts with the
    prefix; returns that reference."""
    from kicad_tools.sexp import Document

    doc = Document.load(sheet)
    for symbol in doc.root.find_children("symbol"):
        ref = next(p.get_string(1) for p in symbol.find_children("property") if p.get_string(0) == "Reference")
        if ref.startswith(reference_prefix):
            symbol.find(flag).set_value(0, value)
            doc.save(sheet)
            return ref
    raise AssertionError(f"no symbol {reference_prefix}* in {sheet}")


def test_a_reference_naming_two_symbols_is_incomplete(kicad_demo) -> None:
    """C101 renamed to C102 (build ticket 26's measurement: kicad-cli keeps one C102, 67 of 68
    parts). Both counts are 67 distinct references and the sets agree, so only the
    per-instance comparison of ``sheet_tree.netlist_disagreement`` sees it."""
    from tests._sheet_tree_fixture import set_reference

    project = kicad_demo("complex_hierarchy")
    set_reference(project / "complex_hierarchy.kicad_sch", "C101", "C102")

    result = _clean_gate(project / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    assert not any("netlist has" in r for r in result["incomplete_reasons"]), result["incomplete_reasons"]
    assert any(
        r.startswith("netlist count guard: ") and "more than one placed symbol" in r and "C102" in r
        for r in result["incomplete_reasons"]
    ), result["incomplete_reasons"]


def test_a_dropped_dnp_part_is_incomplete(kicad_demo) -> None:
    """Review round 1, finding 1. A DNP part is in neither count, so a DNP part that kicad-cli
    leaves out (here: a lib id with no ``lib_symbols`` entry; measured 66 parts listed, exit 0)
    keeps both counts at 66. The guard still compares the DNP parts and names the sheet uses."""
    from kicad_tools.sexp import Document

    project = kicad_demo("complex_hierarchy")
    sheet = project / "ampli_ht.kicad_sch"
    _set_flag(sheet, "C", "dnp", "yes")
    doc = Document.load(sheet)
    symbol = next(s for s in doc.root.find_children("symbol") if s.find("dnp").get_string(0) == "yes")
    symbol.find("lib_id").set_value(0, "Device:NotInThisFile")
    doc.save(sheet)

    result = _clean_gate(project / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    reason = next((r for r in result["incomplete_reasons"] if "DNP parts" in r), None)
    assert reason is not None, result["incomplete_reasons"]
    assert "the netlist has 0, the sheet tree places 2" in reason
    assert reason.count("ampli_ht.kicad_sch at /") == 2 and reason.count("1 of 29 missing") == 2
    assert "C201" in reason and "C301" in reason


def test_a_dropped_dnp_part_reason_names_only_the_parts_missing(kicad_demo) -> None:
    """Delta review 1, finding 2. A second DNP part (an R) stays in kicad-cli's netlist with
    its ``dnp`` property; the reason counts it but does not list it as missing."""
    from kicad_tools.sexp import Document

    project = kicad_demo("complex_hierarchy")
    sheet = project / "ampli_ht.kicad_sch"
    _set_flag(sheet, "R", "dnp", "yes")
    _set_flag(sheet, "C", "dnp", "yes")
    doc = Document.load(sheet)
    symbol = next(
        s
        for s in doc.root.find_children("symbol")
        if s.find("dnp").get_string(0) == "yes"
        and any(p.get_string(0) == "Reference" and p.get_string(1).startswith("C") for p in s.find_children("property"))
    )
    symbol.find("lib_id").set_value(0, "Device:NotInThisFile")
    doc.save(sheet)

    result = _clean_gate(project / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    reason = next((r for r in result["incomplete_reasons"] if "DNP parts" in r), None)
    assert reason is not None, result["incomplete_reasons"]
    assert "the netlist has 2, the sheet tree places 4" in reason
    assert "1 of 29 missing (C201)" in reason and "1 of 29 missing (C301)" in reason, reason


def _set_unit_dnp(sheet: Path, ref: str, dnp_units: set[str], remove_units: set[str]) -> None:
    """``(dnp yes)`` on the given units of the multi-unit part ``ref`` and ``(dnp no)`` on the
    others; the units in ``remove_units`` are removed from the sheet. Document API only."""
    from kicad_tools.sexp import Document

    doc = Document.load(sheet)
    units = [
        s
        for s in doc.root.find_children("symbol")
        if any(p.get_string(0) == "Reference" and p.get_string(1) == ref for p in s.find_children("property"))
    ]
    assert len(units) > 1, f"{ref} is not a multi-unit part in {sheet}"
    for symbol in units:
        unit = symbol.find("unit").get_string(0)
        symbol.find("dnp").set_value(0, "yes" if unit in dnp_units else "no")
        if unit in remove_units:
            doc.root.remove(symbol)
    doc.save(sheet)


@pytest.mark.parametrize(
    "dnp_units,remove_units,netlist_dnp",
    [
        ({"2"}, set(), False),  # delta review 1, finding 1
        ({"1"}, set(), True),
        ({"2"}, {"1"}, True),  # no unit 1: the lowest placed unit decides
        ({"3"}, {"1"}, False),
    ],
)
def test_a_multi_unit_part_with_one_dnp_unit_still_passes(
    kicad_demo, dnp_units: set[str], remove_units: set[str], netlist_dnp: bool
) -> None:
    """With all units on one sheet, KiCad 10.0.7 took the lowest placed unit's DNP flag
    (ecc83's U1, units 1-3, also in reverse file order); across sheets its pick is not that
    (next test). The guard accepts either netlist state for a part whose units disagree."""
    project = kicad_demo("ecc83")
    sheet = project / "ecc83-pp.kicad_sch"
    _set_unit_dnp(sheet, "U1", dnp_units, remove_units)
    u1 = next(c for c in sheet_tree.netlist_components(sheet) if c["ref"] == "U1")
    assert ("dnp" in u1["properties"]) is netlist_dnp

    result = _clean_gate(project / "ecc83-pp.kicad_pcb")

    assert result["verdict"] == "PASS", result["incomplete_reasons"]


def _set_dnp_on_unit(sheet: Path, ref: str, unit: str) -> None:
    """``(dnp yes)`` on the one unit ``unit`` of ``ref`` placed in ``sheet``. Document API only."""
    from kicad_tools.sexp import Document

    doc = Document.load(sheet)
    symbol = next(
        s
        for s in doc.root.find_children("symbol")
        if s.find("unit").get_string(0) == unit
        and any(p.get_string(0) == "Reference" and p.get_string(1) == ref for p in s.find_children("property"))
    )
    symbol.find("dnp").set_value(0, "yes")
    doc.save(sheet)


@pytest.mark.parametrize(
    "demo,board,sheet,ref,unit,netlist_dnp",
    [
        ("vme-wren", "vme-wren.kicad_pcb", "fpga-hd-banks.kicad_sch", "IC14", "1", False),
        ("vme-wren", "vme-wren.kicad_pcb", "fpga-hp-banks.kicad_sch", "IC14", "7", True),
        ("jetson-agx-thor-baseboard", "jetson-agx-thor-baseboard.kicad_pcb", "usb_dp.kicad_sch", "U30", "3", True),
    ],
)
def test_a_multi_unit_part_spread_over_sheets_with_one_dnp_unit_still_passes(
    kicad_demo, demo: str, board: str, sheet: str, ref: str, unit: str, netlist_dnp: bool
) -> None:
    """Delta review 2. With a part's units on several sheets, KiCad 10.0.7's netlist takes
    neither the lowest unit's DNP flag nor any fixed one (measured: IC14 DNP on unit 1 only is
    not DNP, on unit 7 only it is; U30 DNP on unit 3 only is DNP). The guard does not model
    the pick: it accepts either state, still checks the part is listed, and notes the
    disagreement."""
    project = kicad_demo(demo)
    _set_dnp_on_unit(project / sheet, ref, unit)
    root = project / board.replace(".kicad_pcb", ".kicad_sch")
    listed = next(c for c in sheet_tree.netlist_components(root) if c["ref"] == ref)
    assert ("dnp" in listed["properties"]) is netlist_dnp

    result = _clean_gate(project / board)

    assert result["verdict"] == "PASS", result["incomplete_reasons"]
    assert any(
        f"units of {ref} disagree on DNP" in n and f"DNP: unit {unit};" in n for n in result["notes"]
    ), result["notes"]


def test_a_part_whose_units_disagree_on_dnp_and_is_missing_from_the_netlist_is_incomplete(kicad_demo) -> None:
    """Accepting either DNP state never excuses absence: ecc83's U1 with DNP on unit 2 only and
    a lib id that has no ``lib_symbols`` entry on every unit, so kicad-cli leaves it out."""
    from kicad_tools.sexp import Document

    project = kicad_demo("ecc83")
    sheet = project / "ecc83-pp.kicad_sch"
    _set_unit_dnp(sheet, "U1", {"2"}, set())
    doc = Document.load(sheet)
    for symbol in doc.root.find_children("symbol"):
        if any(p.get_string(0) == "Reference" and p.get_string(1) == "U1" for p in symbol.find_children("property")):
            symbol.find("lib_id").set_value(0, "Device:NotInThisFile")
    doc.save(sheet)
    assert "U1" not in {c["ref"] for c in sheet_tree.netlist_components(sheet)}

    result = _clean_gate(project / "ecc83-pp.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    assert any("units of U1 disagree on DNP" in n and "(here: not listed)" in n for n in result["notes"]), result["notes"]
    assert any(
        "missing from the netlist: ecc83-pp.kicad_sch at /" in r and "(U1)" in r for r in result["incomplete_reasons"]
    ), result["incomplete_reasons"]


@pytest.mark.parametrize("flag,value", [("dnp", "yes"), ("on_board", "no")])
def test_dnp_and_off_board_parts_are_not_counted(kicad_demo, flag: str, value: str) -> None:
    """A part marked DNP stays in kicad-cli's netlist with a ``dnp`` property; a part off the
    board leaves it. Neither is counted, on either side: the healthy board still passes."""
    project = kicad_demo("complex_hierarchy")
    _set_flag(project / "ampli_ht.kicad_sch", "C", flag, value)

    result = _clean_gate(project / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "PASS", result["incomplete_reasons"]


# --- a sheet that does not load on its own ---------------------------------------------------


def _break_after_the_last_symbol(sheet: Path) -> None:
    """Append a text item with a raw line break after every symbol of the sheet: KiCad keeps
    what it parsed before the fault, so the count stays right while the sheet is truncated."""
    from kicad_tools.sexp import Document
    from kicad_tools.sexp.parser import SExp

    doc = Document.load(sheet)
    names = [child.name for child in doc.root.children]
    assert names[-1] == "symbol", "the symbols must come last for this fault to keep the count"
    doc.root.append(
        SExp.list(
            "text",
            _raw_line_break("line|break"),
            SExp.list("at", 10, 10, 0),
            SExp.list("effects", SExp.list("font", SExp.list("size", 1.27, 1.27))),
            SExp.list("uuid", SExp.quoted_atom("5b6a0f4e-0000-4000-8000-000000000050")),
        )
    )
    doc.save(sheet)


def test_a_sheet_that_does_not_load_on_its_own_is_incomplete_even_when_the_count_holds(kicad_demo) -> None:
    project = kicad_demo("complex_hierarchy")
    _break_after_the_last_symbol(project / "ampli_ht.kicad_sch")
    # The count alone cannot see this fault: kicad-cli still lists all 68 parts, exit 0.
    assert len(sheet_tree.netlist_components(project / "complex_hierarchy.kicad_sch")) == 68

    result = _clean_gate(project / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    assert not any("netlist has" in r for r in result["incomplete_reasons"])
    assert any(
        "ampli_ht.kicad_sch" in r and "does not load" in r and "sch upgrade" in r for r in result["incomplete_reasons"]
    ), result["incomplete_reasons"]


def test_every_sub_sheet_is_load_checked_down_the_tree(kicad_demo) -> None:
    """Review round 1, finding 2. ``pp_driver_2x.kicad_sch`` is three levels down in vme-wren
    (under ``pp_driver_32x`` and ``pp_driver_8x``, the fourth file of the walk). Its fault
    after the last symbol keeps the count; only its own load check reports it. Upgrading the
    sheets above it does not rewrite it, so the order of the checks cannot hide the fault."""
    import hashlib

    project = kicad_demo("vme-wren")
    sheet = project / "pp_driver_2x.kicad_sch"
    _break_after_the_last_symbol(sheet)

    result = _clean_gate(project / "vme-wren.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    load = [r for r in result["incomplete_reasons"] if "does not load" in r]
    assert len(load) == 1 and "sheet pp_driver_2x.kicad_sch does not load" in load[0], result["incomplete_reasons"]

    def digest() -> str:
        return hashlib.sha256(sheet.read_bytes()).hexdigest()

    before = digest()
    for parent in ("vme-wren.kicad_sch", "pp_driver_32x.kicad_sch", "pp_driver_8x.kicad_sch"):
        assert kicad_cli.upgrade_schematic(project / parent).returncode == 0
    assert digest() == before
    assert kicad_cli.upgrade_schematic(sheet).returncode != 0


def test_a_load_check_that_cannot_run_is_incomplete(kicad_demo, monkeypatch) -> None:
    real = kicad_cli._run

    def no_upgrade(args):
        if args[:2] == ["sch", "upgrade"]:
            raise kicad_cli.KicadCliError("kicad-cli timed out after 600s")
        return real(args)

    monkeypatch.setattr(kicad_cli, "_run", no_upgrade)

    result = _clean_gate(kicad_demo("complex_hierarchy") / "complex_hierarchy.kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    assert any(
        "ampli_ht.kicad_sch could not be load-checked" in r and "timed out" in r for r in result["incomplete_reasons"]
    ), result["incomplete_reasons"]
