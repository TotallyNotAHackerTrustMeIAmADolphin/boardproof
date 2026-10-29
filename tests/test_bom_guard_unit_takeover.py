"""The BOM's guard catches a part that takes a free unit of another part's reference (build ticket 73).

``sheet_tree.netlist_disagreement`` is the guard ``export_manufacturing_package`` runs before it
writes a BOM from kicad-cli's netlist (build ticket 26), and ``verify_board``'s netlist count
guard runs it too (build ticket 50). It used the unit rule alone: a reference names two parts
only when one unit repeats. When a different symbol takes a free unit of a multi-unit part's
reference, no unit repeats, kicad-cli's netlist keeps one of the two and drops the other with
exit 0, and the BOM shipped without it. The guard now applies build ticket 68's rule: a unit
repeats, or the units of one reference differ in Value, Footprint or the unit count of their
embedded lib symbol (an unreadable count fails closed), and it names the rule that fired.

Tested at the guard's public seam and at the BOM's, ``export_manufacturing_package``, on copies
of KiCad demos (``kicad_demo``). Every schematic change goes through ``kicad_mcp.write_layer``
(the helpers of ``tests/test_import_duplicate_scan.py``).
"""
from __future__ import annotations

import csv
from pathlib import Path

import pytest

from kicad_mcp import sheet_tree, write_layer
from kicad_mcp.toolsets import manufacturing
from tests.test_import_duplicate_scan import (
    DELETED_SHEET,
    DIP14,
    R1,
    R2,
    SONDE_ROOT,
    STALE_ENTRY,
    U1_UNIT2,
    U2_UNIT1,
    _drop_lib_symbol,
    _only_instance_entry,
    _rename_symbol,
    _set_off_board,
)

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

U1_UNIT1 = "00000000-0000-0000-0000-00003ebf7d9f"  # U1's unit 1 (a 74LS125 gate, four units)
DUPLICATE_ENTRIES = "more than one instance entry at the symbol's own sheet use"
RESISTOR_FP = "Resistor_THT:R_Axial_DIN0207_L6.3mm_D2.5mm_P10.16mm_Horizontal"


def _bom(result: dict) -> dict[str, tuple[str, str]]:
    """Designator -> (Comment, Footprint) of the BOM file."""
    rows: dict[str, tuple[str, str]] = {}
    with open(result["bom"], newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for ref in row["Designator"].split(","):
                rows[ref.strip()] = (row["Comment"], row["Footprint"])
    return rows


def _takeover(kicad_demo, mover: str, **properties: str) -> Path:
    """sonde xilinx with U1's unit 1 renamed U9 and ``mover`` renamed U1 (it takes the free
    unit 1); returns the root sheet."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _rename_symbol(sch, U1_UNIT1, "U9")
    _rename_symbol(sch, mover, "U1", **properties)
    return sch


def test_the_untouched_demo_bom_lists_the_74ls125_as_u1(kicad_demo, tmp_path: Path) -> None:
    """The baseline the takeover breaks: U1 is a 74LS125 in DIP-14."""
    project = kicad_demo("sonde xilinx")

    result = manufacturing.export_manufacturing_package(project / "sonde xilinx.kicad_pcb", tmp_path / "mfg")

    assert result["status"] == "ok", result
    assert _bom(result)["U1"] == ("74LS125", DIP14)


TAKEOVERS = [
    (R1, {}, "different values, different footprints, different unit counts", "value '100'"),
    (U2_UNIT1, {"Value": "74HC125"}, "different values", f"value '74HC125' footprint '{DIP14}'"),
    (
        U2_UNIT1,
        {"Footprint": "Package_DIP:DIP-14_W7.62mm"},
        "different footprints",
        "value '74LS125' footprint 'Package_DIP:DIP-14_W7.62mm'",
    ),
    (
        R1,
        {"Value": "74LS125", "Footprint": DIP14},
        "different unit counts",
        f"value '74LS125' footprint '{DIP14}' sonde_xilinx:R (1 unit)",
    ),
]
TAKEOVER_IDS = ["resistor", "only-value-differs", "only-footprint-differs", "only-unit-count-differs"]


@pytest.mark.parametrize(("mover", "properties", "rules", "entry"), TAKEOVERS, ids=TAKEOVER_IDS)
def test_a_part_on_a_free_unit_of_another_parts_reference_is_refused_by_name(
    kicad_demo, mover: str, properties: dict, rules: str, entry: str
) -> None:
    """Each case leaves exactly one rule (the first: all three) to tell the two parts apart."""
    sch = _takeover(kicad_demo, mover, **properties)
    refs = [c["ref"] for c in sheet_tree.netlist_components(sch)]

    problems = sheet_tree.netlist_disagreement(sheet_tree.walk(sch), refs)

    text = "; ".join(problems)
    assert f"U1 ({rules}): " in text, problems
    assert f"U1 unit 1 {entry}" in text, problems
    assert f"U1 unit 2 value '74LS125' footprint '{DIP14}' sonde_xilinx:74LS125_6 (4 units)" in text, problems
    assert "U9" not in text, problems


def test_the_bom_is_refused_when_a_resistor_takes_a_free_unit_of_the_74ls125(kicad_demo, tmp_path: Path) -> None:
    """The ticket's construction: R1 renamed U1 takes U1's free unit 1. kicad-cli's netlist
    lists U1 as the 100 ohm resistor and drops the 74LS125 package U1 without an error (checked
    first, kicad-cli 10); before ticket 73 the BOM shipped that way (status ok, U1 = '100')."""
    sch = _takeover(kicad_demo, R1)
    listed = {c["ref"]: (c["value"], c["footprint"]) for c in sheet_tree.netlist_components(sch)}
    assert listed["U1"] == ("100", RESISTOR_FP)  # kicad-cli kept the resistor, dropped the 74LS125

    result = manufacturing.export_manufacturing_package(sch.with_suffix(".kicad_pcb"), tmp_path / "mfg")

    assert result["bom"] is None, result
    assert any(
        "U1 (different values, different footprints, different unit counts): " in e for e in result["errors"]
    ), result["errors"]
    assert not list((tmp_path / "mfg").glob("bom_*.csv"))


def test_an_unreadable_unit_count_fails_closed(kicad_demo) -> None:
    """U1 unit 2's embedded lib symbol 74LS125_6 removed: its unit count cannot be read, so
    the units of U1 cannot be shown to be one part. Refused, never passed."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _drop_lib_symbol(sch, "74LS125_6")

    problems = sheet_tree.netlist_disagreement(sheet_tree.walk(sch), [c["ref"] for c in sheet_tree.netlist_components(sch)])

    text = "; ".join(problems)
    assert "U1 (unit count unreadable): " in text, problems
    assert f"U1 unit 2 value '74LS125' footprint '{DIP14}' sonde_xilinx:74LS125_6 (unit count unknown)" in text


def test_an_off_board_symbol_sharing_a_reference_is_refused(kicad_demo, tmp_path: Path) -> None:
    """R2 renamed R1 and marked ``(on_board no)``: kicad-cli's netlist leaves R2 out (checked),
    so no set comparison sees it, but the schematic names two symbols R1. The guard counts
    off-board symbols for this rule, as the import's scan does (build ticket 68)."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _rename_symbol(sch, R2, "R1")
    _set_off_board(sch, R2)
    listed = [c["ref"] for c in sheet_tree.netlist_components(sch)]
    assert listed.count("R1") == 1 and "R2" not in listed

    result = manufacturing.export_manufacturing_package(project / "sonde xilinx.kicad_pcb", tmp_path / "mfg")

    assert result["bom"] is None, result
    assert any("R1 (unit 1 placed 2 times, different values): " in e and R2 in e for e in result["errors"]), result[
        "errors"
    ]


@pytest.mark.parametrize("named", ["R14", "C1", "D2", "P1"])
def test_a_stale_only_entry_that_renames_a_unit_to_another_part_is_refused(
    kicad_demo, tmp_path: Path, named: str
) -> None:
    """U1's unit 2 keeps its Reference property "U1" but its only instance entry names another
    existing part, at a path no sheet use has. kicad-cli takes the stale entry: its netlist
    lists that part as a 74LS125 in DIP-14 and drops the real part (R14, a 100 ohm resistor),
    exit 0 (checked, kicad-cli 10). Both reference sets still agree, so before ticket 73 the
    BOM shipped with R14 as a 74LS125. Refused, naming the symbol: KiCad's instance data does
    not match the sheet hierarchy (build ticket 68's check a)."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _only_instance_entry(sch, U1_UNIT2, project="sonde xilinx", path="/" + DELETED_SHEET, ref=named)
    listed = {c["ref"]: (c["value"], c["footprint"]) for c in sheet_tree.netlist_components(sch)}
    assert listed[named] == ("74LS125", DIP14)  # kicad-cli gave the stale entry's reference to the gate

    result = manufacturing.export_manufacturing_package(project / "sonde xilinx.kicad_pcb", tmp_path / "mfg")

    assert result["bom"] is None, result
    assert any(STALE_ENTRY in e and f"U1 uuid {U1_UNIT2} in sonde xilinx.kicad_sch" in e for e in result["errors"]), (
        result["errors"]
    )


def _add_entry_at_live_path(
    sheet: Path, uuid: str, *, project: str | None, ref: str, path: str = SONDE_ROOT
) -> None:
    """Give the symbol ``uuid`` a second ``(path <path> (reference <ref>) (unit 1))`` entry
    (``path`` spelled as given, the root sheet's by default) in a new ``(project <project>)``
    block after its own when ``project`` is given, else as a second ``path`` inside its own
    project block. Written through the write layer; the Reference property stays as it is."""
    from kicad_tools.sexp.parser import SExp

    root = write_layer.load(sheet)
    for symbol in root.find_children("symbol"):
        if any(u.get_string(0) == uuid for u in symbol.find_children("uuid")):
            (instances,) = symbol.find_children("instances")
            entry = SExp.list(
                "path",
                write_layer.string(path),
                SExp.list("reference", write_layer.string(ref)),
                SExp.list("unit", write_layer.number(1)),
            )
            if project is None:
                instances.find_children("project")[-1].children.append(entry)
            else:
                instances.children.append(SExp.list("project", write_layer.string(project), entry))
            write_layer.save(root, sheet)
            return
    raise AssertionError(f"no symbol {uuid} in {sheet}")


@pytest.mark.parametrize("project", ["other", None], ids=["foreign-project", "same-project"])
@pytest.mark.parametrize("named", ["R14", "C1", "D2", "P1"])
def test_a_second_entry_at_the_live_path_that_renames_a_unit_to_another_part_is_refused(
    kicad_demo, tmp_path: Path, named: str, project: str | None
) -> None:
    """U1's unit 2 gets a second instance entry at the live sheet use's path, naming another
    existing part. kicad-cli 10 takes the LAST entry at a path, whatever project it belongs to:
    its netlist lists that part as a 74LS125 in DIP-14 and drops the real one (checked), exit 0.
    ``placed_instances`` read this project's entry first, so the symbol was not ``stale_only``
    and the BOM shipped a wrong part. A symbol with more than one entry at its own sheet use is
    refused by name, whatever project the entries belong to."""
    project_dir = kicad_demo("sonde xilinx")
    sch = project_dir / "sonde xilinx.kicad_sch"
    _add_entry_at_live_path(sch, U1_UNIT2, project=project, ref=named)
    listed = {c["ref"]: (c["value"], c["footprint"]) for c in sheet_tree.netlist_components(sch)}
    assert listed[named] == ("74LS125", DIP14)  # kicad-cli took the last entry

    result = manufacturing.export_manufacturing_package(project_dir / "sonde xilinx.kicad_pcb", tmp_path / "mfg")

    assert result["bom"] is None, result
    assert any(
        DUPLICATE_ENTRIES in e and f"U1 uuid {U1_UNIT2} in sonde xilinx.kicad_sch" in e and named in e
        for e in result["errors"]
    ), result["errors"]


def test_a_stale_only_entry_is_refused_even_when_kicad_cli_agrees(kicad_demo) -> None:
    """R2's only instance entry sits at a deleted sheet's path but names R2: kicad-cli agrees
    today, but the entry is not KiCad's data for this hierarchy. Refused by name."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _only_instance_entry(sch, R2, project="sonde xilinx", path=SONDE_ROOT + "/" + DELETED_SHEET, ref="R2")

    problems = sheet_tree.netlist_disagreement(sheet_tree.walk(sch), [c["ref"] for c in sheet_tree.netlist_components(sch)])

    assert any(STALE_ENTRY in p and f"R2 uuid {R2} in sonde xilinx.kicad_sch" in p for p in problems), problems


def test_the_gate_does_not_pass_a_part_on_a_free_unit_of_another_parts_reference(kicad_demo) -> None:
    """verify_board's netlist count guard (build ticket 50) runs the same guard: with clean DRC
    reports the takeover lowers the verdict and names U1 with the rules that fired."""
    from tests.test_verify_board_netlist_guard import _clean_gate

    sch = _takeover(kicad_demo, R1)

    result = _clean_gate(sch.with_suffix(".kicad_pcb"))

    assert result["verdict"] != "PASS", result
    assert any(
        r.startswith("netlist count guard: a reference names more than one placed symbol: ")
        and "U1 (different values, different footprints, different unit counts): " in r
        for r in result["incomplete_reasons"]
    ), result["incomplete_reasons"]


# kicad-cli 10 reads an instance path as a vector of ids (build ticket 73, measured on sonde xilinx
# and pic_programmer): case does not matter, empty segments are dropped, a segment may be 32 hex
# digits, braced, or an 8-digit legacy timestamp, and the project name is ignored. Each spelling
# below was measured to make the entry the symbol's LIVE entry (kicad-cli takes the last one).
SPELLINGS = {
    "uppercase": lambda p: p.upper(),
    "trailing-slash": lambda p: p + "/",
    "doubled-slash": lambda p: p.replace("/", "//"),
    "no-leading-slash": lambda p: p[1:],
    "no-hyphens": lambda p: p.replace("-", ""),
    "braced": lambda p: "/".join(f"{{{seg}}}" if seg else seg for seg in p.split("/")),
    "braced-no-hyphens": lambda p: "/".join(f"{{{seg.replace('-', '')}}}" if seg else seg for seg in p.split("/")),
    "braced-no-hyphens-uppercase": lambda p: "/".join(
        f"{{{seg.replace('-', '')}}}".upper() if seg else seg for seg in p.split("/")
    ),
    "exact": lambda p: p,
}
PROJECT_NAMES = {
    "foreign-project": "other",
    "same-project": None,
    "other-case": "Sonde Xilinx",
    "padded-name": " sonde xilinx",
}


@pytest.mark.parametrize("project", PROJECT_NAMES.values(), ids=PROJECT_NAMES.keys())
@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS.keys())
def test_a_second_entry_in_any_spelling_of_the_live_path_is_refused(
    kicad_demo, tmp_path: Path, spelling, project: str | None
) -> None:
    """The reviewer's matrix (ticket 73, delta 1, F1b). kicad-cli reads the entry's path as an id
    vector and ignores the project name, so every spelling below is the live path and its entry
    (last) renames U1's unit 2 to R14: the netlist lists R14 as a 74LS125 and drops the resistor,
    exit 0 (asserted first). Compared as strings, the entry was neither a duplicate nor stale and
    the BOM shipped. Refused by name, whatever the spelling or project name."""
    project_dir = kicad_demo("sonde xilinx")
    sch = project_dir / "sonde xilinx.kicad_sch"
    _add_entry_at_live_path(sch, U1_UNIT2, project=project, ref="R14", path=spelling(SONDE_ROOT))
    listed = {c["ref"]: (c["value"], c["footprint"]) for c in sheet_tree.netlist_components(sch)}
    assert listed["R14"] == ("74LS125", DIP14)  # kicad-cli took the variant entry

    result = manufacturing.export_manufacturing_package(project_dir / "sonde xilinx.kicad_pcb", tmp_path / "mfg")

    assert result["bom"] is None, result
    assert any(
        DUPLICATE_ENTRIES in e and f"U1 uuid {U1_UNIT2} in sonde xilinx.kicad_sch" in e and "R14" in e
        for e in result["errors"]
    ), result["errors"]


SUBSHEETS_ROOT = "/e63e39d7-6ac0-4ffd-8aa3-1841a4541b55"
SUBSHEET1_USE = SUBSHEETS_ROOT + "/51ab3a6c-36b1-4056-a2d2-39c83ee99c02"  # subsheet1.kicad_sch, R4 lives there
R4 = "84fb99cd-cd4d-45fa-95d5-fc922374f67b"
PIC_USE = "/2e45d1d2-c73f-46e5-98d6-3a9dc360bff5/00000000-0000-0000-0000-00004804a5e2"  # pic_sockets.kicad_sch
PIC_U6 = "00000000-0000-0000-0000-0000442a81a5"
PIC_TIMESTAMP_USE = "/2e45d1d2-c73f-46e5-98d6-3a9dc360bff5/4804a5e2"  # the same use, the sheet as an 8-digit timestamp


def _disagreement(root: Path) -> list[str]:
    refs = [c["ref"] for c in sheet_tree.netlist_components(root)]
    return sheet_tree.netlist_disagreement(sheet_tree.walk(root), refs)


@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS.keys())
def test_a_second_entry_at_a_sub_sheet_use_in_any_spelling_is_refused(kicad_demo, spelling) -> None:
    """The same family at a sub-sheet use (simulation/subsheets): R4 in subsheet1.kicad_sch gets a
    second entry at its own use's path naming R5. kicad-cli takes it: no R4 in its netlist
    (asserted first)."""
    project = kicad_demo("simulation/subsheets")
    root = project / "mainsheet.kicad_sch"
    _add_entry_at_live_path(project / "subsheet1.kicad_sch", R4, project="other", ref="R5", path=spelling(SUBSHEET1_USE))
    assert "R4" not in {c["ref"] for c in sheet_tree.netlist_components(root)}  # kicad-cli took the entry

    problems = _disagreement(root)

    assert any(DUPLICATE_ENTRIES in p and R4 in p for p in problems), problems


@pytest.mark.parametrize(
    "path",
    [PIC_TIMESTAMP_USE, PIC_TIMESTAMP_USE.upper(), PIC_USE.upper(), PIC_USE.replace("/", "//")],
    ids=["timestamp", "timestamp-uppercase", "uuid-uppercase", "doubled-slash"],
)
def test_a_sub_sheet_used_by_its_legacy_timestamp_is_compared_as_the_same_path(kicad_demo, path: str) -> None:
    """pic_programmer's sub-sheet uuid is a KiCad 5 timestamp (``...00004804a5e2``), which kicad-cli
    reads as ``4804a5e2`` too (measured). U6 gets a second entry spelled that way naming U1."""
    project = kicad_demo("pic_programmer")
    root = project / "pic_programmer.kicad_sch"
    _add_entry_at_live_path(project / "pic_sockets.kicad_sch", PIC_U6, project="other", ref="U1", path=path)
    assert "U6" not in {c["ref"] for c in sheet_tree.netlist_components(root)}  # kicad-cli took the entry

    problems = _disagreement(root)

    assert any(DUPLICATE_ENTRIES in p and PIC_U6 in p for p in problems), problems


UNPROVEN = "an instance path or symbol_instances key whose reading by kicad-cli is not proven"


@pytest.mark.parametrize(
    "path",
    [
        "/urn:uuid:" + SONDE_ROOT[1:],
        " " + SONDE_ROOT,
        SONDE_ROOT + " ",
        "/zz" + SONDE_ROOT,
        SONDE_ROOT + "zz",
        SONDE_ROOT[:-1],
        SONDE_ROOT + "0",
        "/zzzz",
    ],
    ids=["urn", "leading-space", "trailing-space", "garbage-segment", "garbage-tail", "short", "long", "text"],
)
def test_an_instance_path_that_is_not_proven_to_be_read_as_kicad_does_is_refused(kicad_demo, path: str) -> None:
    """A path in a form nobody measured (kicad-cli read each of these as no live path, but that is
    only the cases measured) is refused by name, so a spelling nobody thought of cannot fail open."""
    project_dir = kicad_demo("sonde xilinx")
    sch = project_dir / "sonde xilinx.kicad_sch"
    _add_entry_at_live_path(sch, U1_UNIT2, project=None, ref="R14", path=path)

    problems = _disagreement(sch)

    assert any(UNPROVEN in p and f"U1 uuid {U1_UNIT2} in sonde xilinx.kicad_sch" in p for p in problems), problems


def _to_legacy_table(sheet: Path, *, key_of: dict[str, str], ref_of: dict[str, str] | None = None) -> None:
    """Rewrite ``sheet`` the KiCad 6 way through the write layer: version 20211123, no
    ``(instances)`` on any symbol, one ``(symbol_instances)`` table with a ``(path "/<symbol
    uuid>" (reference R) (unit N))`` entry per placed symbol (the root has no uuid in the key).
    ``key_of`` maps a symbol uuid to the key to write for it instead, ``ref_of`` to the
    reference; the Reference properties stay as they are."""
    from kicad_tools.sexp.parser import SExp

    root = write_layer.load(sheet)
    table = SExp.list("symbol_instances")
    for symbol in root.find_children("symbol"):
        uuid = symbol.find_children("uuid")[0].get_string(0)
        for instances in list(symbol.find_children("instances")):
            for block in instances.find_children("project"):
                for entry in block.find_children("path"):
                    ref = (ref_of or {}).get(uuid) or entry.find_children("reference")[0].get_string(0)
                    unit = entry.find_children("unit")[0]
                    table.children.append(
                        SExp.list(
                            "path",
                            write_layer.string(key_of.get(uuid, "/" + uuid)),
                            SExp.list("reference", write_layer.string(ref)),
                            SExp.list("unit", write_layer.number(int(unit.get_value(0)))),
                        )
                    )
            symbol.children.remove(instances)
    for version in root.find_children("version"):
        write_layer.set_number(version, 0, 20211123)
    root.children.append(table)
    write_layer.save(root, sheet)


LEGACY_KEYS = {
    "exact": lambda u: "/" + u,
    "uppercase": lambda u: "/" + u.upper(),
    "timestamp": lambda u: "/" + u[-8:],
    "no-leading-slash": lambda u: u,
    "doubled-slash": lambda u: "//" + u,
    "trailing-slash": lambda u: "/" + u + "/",
    "no-hyphens": lambda u: "/" + u.replace("-", ""),
}


@pytest.mark.parametrize("key", LEGACY_KEYS.values(), ids=LEGACY_KEYS.keys())
def test_a_legacy_table_key_in_another_spelling_is_read_as_the_same_path(kicad_demo, key) -> None:
    """KiCad 6 kept the parts' references in the root's ``(symbol_instances)`` table. kicad-cli reads
    its keys as id vectors too (measured: every spelling here). U1's unit 2 is renamed Z9 in the
    table only, under a spelled key; the Reference property still says U1. kicad-cli lists Z9
    (asserted first), so a reader that compares keys as strings, and reads U1, is wrong and
    refused the sheet for a Z9 it did not know; the id-vector reader agrees with the netlist."""
    project_dir = kicad_demo("sonde xilinx")
    sch = project_dir / "sonde xilinx.kicad_sch"
    _to_legacy_table(sch, key_of={U1_UNIT2: key(U1_UNIT2)}, ref_of={U1_UNIT2: "Z9"})
    assert {c["ref"]: c["value"] for c in sheet_tree.netlist_components(sch)}["Z9"] == "74LS125"

    assert _disagreement(sch) == []


@pytest.mark.parametrize("key", LEGACY_KEYS.values(), ids=LEGACY_KEYS.keys())
def test_a_legacy_table_unit_that_takes_the_name_of_another_part_is_refused_in_any_key_spelling(
    kicad_demo, key
) -> None:
    """U1's unit 2 is renamed R4 in the table under a spelled key (kicad-cli keeps the real resistor
    R4, value 47, asserted first, and drops the unit). Read as string keys, the reader said U1,
    agreed with the netlist's reference set, and refused nothing; read as KiCad reads them it
    sees a 74LS125 unit named R4 next to the resistor, a reference naming two parts."""
    project_dir = kicad_demo("sonde xilinx")
    sch = project_dir / "sonde xilinx.kicad_sch"
    _to_legacy_table(sch, key_of={U1_UNIT2: key(U1_UNIT2)}, ref_of={U1_UNIT2: "R4"})
    assert {c["ref"]: c["value"] for c in sheet_tree.netlist_components(sch)}["R4"] == "47"

    problems = _disagreement(sch)

    assert any("a reference names more than one placed symbol" in p and "R4" in p for p in problems), problems


@pytest.mark.parametrize("key", ["/zz", "/urn:uuid:" + U1_UNIT2, " /" + U1_UNIT2], ids=["text", "urn", "space"])
def test_a_legacy_table_key_that_is_not_proven_to_be_read_as_kicad_does_is_refused(kicad_demo, key: str) -> None:
    project_dir = kicad_demo("sonde xilinx")
    sch = project_dir / "sonde xilinx.kicad_sch"
    _to_legacy_table(sch, key_of={U1_UNIT2: key})

    problems = _disagreement(sch)

    assert any(UNPROVEN in p and f"symbol_instances path {key!r}" in p for p in problems), problems


def test_two_legacy_table_keys_for_one_path_are_refused(kicad_demo) -> None:
    """Which of two keys that read as the same path kicad-cli keeps is not measured: refused."""
    from kicad_tools.sexp.parser import SExp

    project_dir = kicad_demo("sonde xilinx")
    sch = project_dir / "sonde xilinx.kicad_sch"
    _to_legacy_table(sch, key_of={U1_UNIT2: "/" + U1_UNIT2.upper()})
    root = write_layer.load(sch)
    root.find_children("symbol_instances")[0].children.append(
        SExp.list(
            "path",
            write_layer.string("/" + U1_UNIT2),
            SExp.list("reference", write_layer.string("U1")),
            SExp.list("unit", write_layer.number(2)),
        )
    )
    write_layer.save(root, sch)

    problems = _disagreement(sch)

    assert any(UNPROVEN in p and "a second entry for the same path" in p for p in problems), problems


@pytest.mark.parametrize("spelling", SPELLINGS.values(), ids=SPELLINGS.keys())
def test_the_import_stale_check_reads_instance_paths_as_kicad_does(spelling) -> None:
    """``schematic_import._stale_only_entries`` (called by the import) compared paths as strings, so
    an entry at the use's path in another spelling counted as stale and a symbol KiCad reads fine
    was refused. It compares by ``path_key`` now; a path ``path_key`` rejects still counts as stale."""
    from kicad_mcp import schematic_import

    def record(paths: list[str]) -> dict:
        return {"reference": "U1", "uuid": U1_UNIT2, "sheet": "s.kicad_sch", "path": SONDE_ROOT, "instance_paths": paths}

    assert schematic_import._stale_only_entries([record([spelling(SONDE_ROOT)])]) == []
    assert len(schematic_import._stale_only_entries([record(["/zzzz"])])) == 1
    assert len(schematic_import._stale_only_entries([record(["/5ade2176-0000-4000-8000-000000000000"])])) == 1
