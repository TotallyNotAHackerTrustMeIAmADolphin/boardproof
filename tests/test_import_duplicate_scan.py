"""The schematic import's duplicate scan counts only placed instances (build ticket 68).

``schematic_import._placed_symbols`` used to count every instance path stored in a sheet,
stale ones included, and called a reference with differing lib ids a duplicate. It refused
KiCad's ``sonde xilinx`` demo (U1 and U2: four units each, every unit with its own rescued
lib id) and any project with a stale instance entry. The scan now reads
``sheet_tree.placed_instances`` (only instance paths reachable from the root) and applies
the unit rule: several units of one symbol under one reference are one part. Fix round 1:
the import also compares the placed references with kicad-cli's netlist, compares the units'
embedded lib symbols' unit counts, and counts off-board symbols. Fix round 2: a symbol
with instance entries but none at its sheet use is refused, each symbol's reference is
compared with the netlist component listing its uuid, and an unreadable unit count fails
closed. Build ticket 83: a unit on another sheet than kicad-cli's component of its part (not
listed in that component's ``tstamps``) is checked pin by pin through the netlist's net nodes.

Tested at the import's public seam, ``propose_schematic_import``, on copies of KiCad demos
(``kicad_demo``). Every schematic change is made through ``kicad_mcp.write_layer``.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from kicad_mcp import schematic_import as si
from kicad_mcp import sheet_tree, write_layer

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def test_sonde_xilinx_multi_unit_parts_are_not_duplicates(kicad_demo, tmp_path: Path) -> None:
    """Before ticket 68: blocked, "U1: U1 unit 1 ... sonde_xilinx:74LS125_1 ... and U1 unit 2
    ... sonde_xilinx:74LS125_6 ..." (one part, four units, four rescued lib ids)."""
    project = kicad_demo("sonde xilinx")

    result = si.propose_schematic_import(project / "sonde xilinx.kicad_pcb", output_dir=tmp_path / "out")

    assert "duplicate" not in str(result.get("reason", "")), result.get("reason")
    assert result["status"] == "proposed", result


DELETED_SHEET = "00000000-0000-0000-0000-0000deadbeef"


def _instance_paths(sheet: Path, ref: str) -> list[str]:
    """The instance paths that name ``ref`` in ``sheet``, read from the raw S-expression."""
    return [
        entry.get_string(0)
        for symbol in write_layer.load(sheet).find_children("symbol")
        for instances in symbol.find_children("instances")
        for project in instances.find_children("project")
        for entry in project.find_children("path")
        if any(r.get_string(0) == ref for r in entry.find_children("reference"))
    ]


def _add_stale_entry(sheet: Path, ref: str, *, project: str, path: str) -> None:
    """Give the symbol whose instance reference is ``ref`` one more ``(path <path>
    (reference <ref>) (unit 1))`` entry under ``project``: the entry KiCad keeps when the sheet
    use it belonged to is deleted. Written through the write layer."""
    from kicad_tools.sexp.parser import SExp

    root = write_layer.load(sheet)
    for symbol in root.find_children("symbol"):
        for instances in symbol.find_children("instances"):
            for block in instances.find_children("project"):
                refs = [r.get_string(0) for e in block.find_children("path") for r in e.find_children("reference")]
                if block.get_string(0) == project and ref in refs:
                    block.children.append(
                        SExp.list(
                            "path",
                            write_layer.string(path),
                            SExp.list("reference", write_layer.string(ref)),
                            SExp.list("unit", write_layer.number(1)),
                        )
                    )
                    write_layer.save(root, sheet)
                    return
    raise AssertionError(f"no symbol with reference {ref} under project {project} in {sheet}")


def test_a_stale_instance_entry_is_not_a_duplicate(kicad_demo, tmp_path: Path) -> None:
    """complex_hierarchy: C201 in ampli_ht.kicad_sch gains a second entry with the same
    reference for a sheet use that no longer exists. The symbol keeps its live entry, so
    kicad-cli uses that one (a symbol whose only entry is stale is another case: see
    ``test_a_symbol_whose_only_instance_entry_is_unreachable_is_refused``); before ticket 68
    the scan counted it ("C201 unit 1 ... and C201 unit 1 ...") and refused the import."""
    project = kicad_demo("complex_hierarchy")
    sub = project / "ampli_ht.kicad_sch"
    (live,) = _instance_paths(sub, "C201")
    stale = live.rsplit("/", 1)[0] + "/" + DELETED_SHEET
    _add_stale_entry(sub, "C201", project="complex_hierarchy", path=stale)
    assert sorted(_instance_paths(sub, "C201")) == sorted([live, stale])  # the setup holds

    result = si.propose_schematic_import(project / "complex_hierarchy.kicad_pcb", output_dir=tmp_path / "out")

    assert "duplicate" not in str(result.get("reason", "")), result.get("reason")
    assert result["status"] == "proposed", result
    assert "C201" in result["already_present"]  # the demo board carries every part already


def _rename(sheet: Path, old: str, new: str) -> None:
    """Rename reference ``old`` to ``new`` in ``sheet``: the Reference property and every
    instance entry, through the write layer."""
    root = write_layer.load(sheet)
    changed = 0
    for symbol in root.find_children("symbol"):
        for prop in symbol.find_children("property"):
            if prop.get_string(0) == "Reference" and prop.get_string(1) == old:
                write_layer.set_string(prop, 1, new)
                changed += 1
        for instances in symbol.find_children("instances"):
            for block in instances.find_children("project"):
                for entry in block.find_children("path"):
                    for ref in entry.find_children("reference"):
                        if ref.get_string(0) == old:
                            write_layer.set_string(ref, 0, new)
                            changed += 1
    assert changed, f"no reference {old} in {sheet}"
    write_layer.save(root, sheet)


def test_two_symbols_on_reachable_paths_sharing_a_reference_are_refused(kicad_demo, tmp_path: Path) -> None:
    """complex_hierarchy: the root's C101 renamed to C301, which the second use of
    ampli_ht.kicad_sch already gives its 15nF capacitor. kicad-cli would keep one of them."""
    project = kicad_demo("complex_hierarchy")
    _rename(project / "complex_hierarchy.kicad_sch", "C101", "C301")

    result = si.propose_schematic_import(project / "complex_hierarchy.kicad_pcb", output_dir=tmp_path / "out")

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "duplicate reference designators" in result["reason"]
    assert "C301 (unit 1 placed 2 times, different values, different footprints): " in result["reason"]
    assert "C301 unit 1 value '47uF/63V' footprint '" in result["reason"]
    assert "' complex_hierarchy:CP (1 unit) in complex_hierarchy.kicad_sch" in result["reason"]
    assert "C301 unit 1 value '15nF' footprint '" in result["reason"]
    assert "' complex_hierarchy:C (1 unit) in ampli_ht.kicad_sch" in result["reason"]
    assert not (tmp_path / "out").exists()


def _rename_symbol(sheet: Path, uuid: str, new: str, **properties: str) -> None:
    """Rename the one symbol with ``uuid`` (Reference property and instance entries) and set
    the given properties (``Value="74LS125"``), through the write layer."""
    root = write_layer.load(sheet)
    for symbol in root.find_children("symbol"):
        if any(u.get_string(0) == uuid for u in symbol.find_children("uuid")):
            for prop in symbol.find_children("property"):
                if prop.get_string(0) == "Reference":
                    write_layer.set_string(prop, 1, new)
                elif prop.get_string(0) in properties:
                    write_layer.set_string(prop, 1, properties[prop.get_string(0)])
            for instances in symbol.find_children("instances"):
                for block in instances.find_children("project"):
                    for entry in block.find_children("path"):
                        for ref in entry.find_children("reference"):
                            write_layer.set_string(ref, 0, new)
            write_layer.save(root, sheet)
            return
    raise AssertionError(f"no symbol {uuid} in {sheet}")


DIP14 = "Package_DIP:DIP-14_W7.62mm_LongPads"


R1 = "00000000-0000-0000-0000-00003ebf7d16"  # the resistor R1 (sonde_xilinx:R, one unit)
U2_UNIT1 = "00000000-0000-0000-0000-00003ebf7d92"  # U2's unit 1 (a 74LS125 gate, four units)


@pytest.mark.parametrize(
    ("mover", "properties", "rules", "entry"),
    [
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
    ],
    ids=["resistor", "only-value-differs", "only-footprint-differs", "only-unit-count-differs"],
)
def test_a_different_symbol_on_a_free_unit_of_a_reference_is_refused(
    kicad_demo, tmp_path: Path, mover: str, properties: dict, rules: str, entry: str
) -> None:
    """sonde xilinx: U1's unit 1 (a 74LS125 gate) renamed U9, then another symbol renamed U1
    takes the free unit 1. No unit repeats, yet these are two parts. With the resistor R1:
    kicad-cli's netlist keeps one U1 (the resistor, value 100) and drops the 74LS125 without
    an error, and its ERC reports only the 74LS125's unplaced unit A (checked, kicad-cli 10).
    Each case leaves exactly one rule to tell the two symbols apart: a different Value, a
    different Footprint (U2's unit 1, four units like U1) or a different unit count of the
    embedded lib symbol (R1 given U1's Value and Footprint; review finding 2)."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _rename_symbol(sch, "00000000-0000-0000-0000-00003ebf7d9f", "U9")  # U1 unit 1
    _rename_symbol(sch, mover, "U1", **properties)

    result = si.propose_schematic_import(project / "sonde xilinx.kicad_pcb", output_dir=tmp_path / "out")

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "duplicate reference designators" in result["reason"]
    assert f"U1 ({rules}): " in result["reason"], result["reason"]
    assert f"U1 unit 1 {entry}" in result["reason"], result["reason"]
    assert (
        f"U1 unit 2 value '74LS125' footprint '{DIP14}' sonde_xilinx:74LS125_6 (4 units)" in result["reason"]
    ), result["reason"]
    assert "U9" not in result["reason"]


# --- fix round 1 (review of 95795b1) ------------------------------------------------------

SONDE_ROOT = "/5ade2176-b41a-41ec-91fe-ba9644669489"  # the root sheet's instance path
R2 = "00000000-0000-0000-0000-00003ebf8187"


def _only_instance_entry(sheet: Path, uuid: str, *, project: str, path: str, ref: str) -> None:
    """Replace the instance data of the symbol ``uuid`` by one entry ``(project <project>
    (path <path> (reference <ref>) (unit 1)))``, through the write layer; its Reference
    property is left as it is."""
    from kicad_tools.sexp.parser import SExp

    root = write_layer.load(sheet)
    for symbol in root.find_children("symbol"):
        if any(u.get_string(0) == uuid for u in symbol.find_children("uuid")):
            (instances,) = symbol.find_children("instances")
            instances.children[:] = [
                SExp.list(
                    "project",
                    write_layer.string(project),
                    SExp.list(
                        "path",
                        write_layer.string(path),
                        SExp.list("reference", write_layer.string(ref)),
                        SExp.list("unit", write_layer.number(1)),
                    ),
                )
            ]
            write_layer.save(root, sheet)
            return
    raise AssertionError(f"no symbol {uuid} in {sheet}")


def _without_copper(pcb: Path) -> Path:
    """Remove every track, arc, via and zone from the board, through the write layer, so a
    renet of existing pads leaves no copper on an old net and the import can propose."""
    root = write_layer.load(pcb)
    root.children[:] = [c for c in root.children if c.name not in {"segment", "arc", "via", "zone"}]
    write_layer.save(root, pcb)
    return pcb


@pytest.mark.parametrize(
    ("project_name", "path", "named"),
    [
        ("sonde xilinx", SONDE_ROOT + "/" + DELETED_SHEET, "R2"),
        ("sonde xilinx", "/" + DELETED_SHEET, "R2"),
        ("other", "/" + DELETED_SHEET, "R2"),
        ("other", SONDE_ROOT, "R1"),
    ],
    ids=["stale-child-path", "foreign-root-path", "other-project-stale-path", "other-project-live-path"],
)
def test_a_symbol_whose_only_instance_entry_is_unreachable_is_refused(
    kicad_demo, tmp_path: Path, project_name: str, path: str, named: str
) -> None:
    """sonde xilinx: R2 (5,1K) keeps its Reference property "R2" but its only instance entry
    says "R1" at a path no sheet use has, or under another project. kicad-cli then uses that
    entry: its netlist lists one R1 (100) and R2 is gone, without an error (checked,
    kicad-cli 10). The scan's own reading (the Reference property) cannot see that, so the
    import compares the placed references with the netlist's and refuses by name."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _only_instance_entry(sch, R2, project=project_name, path=path, ref="R1")
    pcb = _without_copper(project / "sonde xilinx.kicad_pcb")
    listed = [c["ref"] for c in sheet_tree.netlist_components(sch)]
    assert listed.count("R1") == 1 and "R2" not in listed  # kicad-cli drops R2 silently

    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert named in result["reason"], result["reason"]
    assert not (tmp_path / "out").exists()


def _set_off_board(sheet: Path, uuid: str) -> None:
    """Mark the symbol ``uuid`` ``(on_board no)``, through the write layer."""
    root = write_layer.load(sheet)
    for symbol in root.find_children("symbol"):
        if any(u.get_string(0) == uuid for u in symbol.find_children("uuid")):
            (on_board,) = symbol.find_children("on_board")
            write_layer.set_symbol(on_board, 0, "no")
            write_layer.save(root, sheet)
            return
    raise AssertionError(f"no symbol {uuid} in {sheet}")


def test_an_off_board_symbol_sharing_a_reference_is_still_a_duplicate(kicad_demo, tmp_path: Path) -> None:
    """sonde xilinx: R2 renamed R1 and marked ``(on_board no)``. kicad-cli's netlist leaves
    R2 out, so the netlist cross-check sees nothing, but the schematic names two symbols R1:
    the scan counts off-board symbols and refuses (review low finding: the
    ``on_board_only=True`` mutant survived)."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _rename_symbol(sch, R2, "R1")
    _set_off_board(sch, R2)
    listed = [c["ref"] for c in sheet_tree.netlist_components(sch)]
    assert listed.count("R1") == 1 and "R2" not in listed

    result = si.propose_schematic_import(project / "sonde xilinx.kicad_pcb", output_dir=tmp_path / "out")

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "R1 (unit 1 placed 2 times, different values): " in result["reason"], result["reason"]
    assert R2 in result["reason"] and R1 in result["reason"]


# --- fix round 2 (delta review 1 of c9598a8) ----------------------------------------------

U1_UNIT2 = "00000000-0000-0000-0000-00003ebf7dad"  # U1's unit 2 (lib symbol 74LS125_6, only this one)
STALE_ENTRY = "instance data does not match the sheet hierarchy; annotate in KiCad"


def _without_net_table(pcb: Path) -> Path:
    """Remove the board's top-level ``(net <n> <name>)`` table, through the write layer, so the
    import's net-reconciliation check sees no old net name to keep."""
    root = write_layer.load(pcb)
    root.children[:] = [c for c in root.children if c.name != "net"]
    write_layer.save(root, pcb)
    return pcb


def test_a_stale_only_entry_naming_an_existing_part_is_refused(kicad_demo, tmp_path: Path) -> None:
    """The reviewer's construction: U1's unit 2 keeps its Reference property "U1" but its only
    instance entry says R5, unit 1, at a path no sheet use has. kicad-cli names it R5 and keeps
    one R5, so the netlist's reference set still equals the sheets' (U1 and R5 both listed) and
    a set comparison passes; c9598a8 proposed a plan shorting R5.1 to VCC and R5.2 to GND.
    Refused twice over: the stale-only entry (fix a) and the per-uuid comparison with the
    netlist (fix b); the two tests after this one show each fires on its own."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _only_instance_entry(sch, U1_UNIT2, project="sonde xilinx", path="/" + DELETED_SHEET, ref="R5")
    pcb = _without_net_table(_without_copper(project / "sonde xilinx.kicad_pcb"))
    listed = [c["ref"] for c in sheet_tree.netlist_components(sch)]
    assert listed.count("R5") == 1 and listed.count("U1") == 1  # the sets agree

    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert U1_UNIT2 in result["reason"], result["reason"]
    assert not (tmp_path / "out").exists()


def test_a_stale_only_entry_is_refused_even_when_kicad_cli_agrees(kicad_demo, tmp_path: Path) -> None:
    """Fix a on its own: R2's only instance entry sits at a deleted sheet's path but names R2,
    so kicad-cli's netlist matches the sheets symbol by symbol. The entry is still not KiCad's
    data for this hierarchy: refused by name, the user annotates in KiCad."""
    project = kicad_demo("sonde xilinx")
    sch = project / "sonde xilinx.kicad_sch"
    _only_instance_entry(sch, R2, project="sonde xilinx", path=SONDE_ROOT + "/" + DELETED_SHEET, ref="R2")

    result = si.propose_schematic_import(project / "sonde xilinx.kicad_pcb", output_dir=tmp_path / "out")

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert STALE_ENTRY in result["reason"], result["reason"]
    assert f"R2 uuid {R2} in sonde xilinx.kicad_sch" in result["reason"], result["reason"]
    assert not (tmp_path / "out").exists()


def _edit_netlist_uuids(out: Path, change: dict[str, str | None]) -> None:
    """Replace each symbol uuid in ``change`` by its value (``None`` drops it) in the
    ``(tstamps ...)`` of the netlist's components, through the S-expression API; the
    references stay."""
    root = write_layer.load(out)
    for block in root.find_children("components"):
        for comp in block.find_children("comp"):
            for tstamps in comp.find_children("tstamps"):
                for i in range(len(tstamps.values)):
                    new = change.get(tstamps.get_string(i) or "", "")
                    if new:
                        write_layer.set_string(tstamps, i, new)
                tstamps.children[:] = [
                    c for c in tstamps.children if not (c.is_atom and str(c.value) in change and change[str(c.value)] is None)
                ]
    out.write_text(write_layer.serialise(root), encoding="utf-8")


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (
            {R1: R2, R2: R1},
            [
                f"R1 uuid {R1} in sonde xilinx.kicad_sch: kicad-cli's netlist names it R2",
                f"R2 uuid {R2} in sonde xilinx.kicad_sch: kicad-cli's netlist names it R1",
            ],
        ),
        ({U1_UNIT2: None}, [f"U1 uuid {U1_UNIT2} in sonde xilinx.kicad_sch: kicad-cli's netlist does not list it"]),
    ],
    ids=["two-symbols-swapped", "a-unit-missing-from-its-part"],
)
def test_a_netlist_naming_a_symbol_differently_is_refused(
    kicad_demo, tmp_path: Path, change: dict, expected: list[str]
) -> None:
    """Fix b on its own: the schematic is untouched, but the netlist lists R1's uuid under R2
    and R2's under R1, or U1's component (on the root sheet, like all of U1's units) lacks
    unit 2's uuid, as when kicad-cli gave that unit to another part. Same reference set, same
    instance data; only the per-uuid comparison sees it."""
    from kicad_tools.operations.netlist import Netlist

    project = kicad_demo("sonde xilinx")

    def exporter(sch: Path, out: Path):
        si._kicad_cli_netlist(sch, out)
        _edit_netlist_uuids(out, change)
        return Netlist.load(out)

    result = si.propose_schematic_import(
        project / "sonde xilinx.kicad_pcb", output_dir=tmp_path / "out", netlist_exporter=exporter
    )

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "the sheets and kicad-cli's netlist name a symbol differently: " in result["reason"], result["reason"]
    for text in expected:
        assert text in result["reason"], result["reason"]


def _drop_lib_symbol(sheet: Path, name: str) -> None:
    """Remove the embedded ``(lib_symbols (symbol <name> ...))`` entry, through the write layer."""
    root = write_layer.load(sheet)
    (table,) = root.find_children("lib_symbols")
    before = len(table.children)
    table.children[:] = [c for c in table.children if not (c.name == "symbol" and c.get_string(0) == name)]
    assert len(table.children) == before - 1, f"no lib symbol {name} in {sheet}"
    write_layer.save(root, sheet)


def test_an_unreadable_unit_count_is_a_mismatch(kicad_demo, tmp_path: Path) -> None:
    """Review low finding: a unit count that cannot be read (U1 unit 2's embedded lib symbol
    74LS125_6 removed) was left out of the comparison. It now fails closed."""
    project = kicad_demo("sonde xilinx")
    _drop_lib_symbol(project / "sonde xilinx.kicad_sch", "74LS125_6")

    result = si.propose_schematic_import(project / "sonde xilinx.kicad_pcb", output_dir=tmp_path / "out")

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "U1 (unit count unreadable): " in result["reason"], result["reason"]
    assert f"U1 unit 2 value '74LS125' footprint '{DIP14}' sonde_xilinx:74LS125_6 (unit count unknown)" in result[
        "reason"
    ]


def test_an_exporter_that_writes_no_netlist_file_is_refused(kicad_demo, tmp_path: Path) -> None:
    """Without the netlist file there is nothing to match symbol by symbol: fail closed."""
    project = kicad_demo("sonde xilinx")

    def exporter(sch: Path, out: Path):
        netlist = si._kicad_cli_netlist(sch, out)
        out.unlink()
        return netlist

    result = si.propose_schematic_import(
        project / "sonde xilinx.kicad_pcb", output_dir=tmp_path / "out", netlist_exporter=exporter
    )

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "netlist file could not be read" in result["reason"], result["reason"]


def test_units_of_one_part_on_several_sheets_match_the_netlist(kicad_demo, tmp_path: Path) -> None:
    """kicad-cli's netlist lists, for a part, only the uuids of its units on the component's
    own sheet use (vme-wren's IC14: 20 units over nine sheet uses, three listed). The per-uuid
    comparison checks the other units pin by pin through the netlist's nodes, and every one
    passes: the import is proposed, so it got past ``_check_schematic`` and the rest of the
    plan. A first cut of build ticket 68 refused this demo. The jetson-agx-thor-baseboard demo
    (U30 and J1 over several sheets) is not proposed whatever this check says: its board's
    unconnected nets carry ``_1``/``_2`` suffixes the net reconciliation refuses. Its other-sheet
    units are covered by ``test_a_unit_on_another_sheet_is_checked_pin_by_pin``, whose exact
    list of problems shows that all but the edited ones pass (build ticket 83)."""
    project = kicad_demo("vme-wren")

    result = si.propose_schematic_import(project / "vme-wren.kicad_pcb", output_dir=tmp_path / "out")

    assert result["status"] == "proposed", result.get("reason")
    assert result["revision_token"], result


# --- build ticket 83: units on other sheets are checked through the netlist's net nodes ----

JETSON = "jetson-agx-thor-baseboard"
J1_UNIT1 = "baa2e86c-0584-4554-b892-e1c15de6123f"  # som_power.kicad_sch; J1's component sits on SoM_IO
J1_UNIT2 = "48de9417-629d-4e35-936c-4de92aa18993"  # som_io2.kicad_sch
J1_UNIT4 = "3663b128-e3fc-450a-bb00-a0a616b13606"  # som_io.kicad_sch, listed in J1's tstamps
U30_UNIT1 = "8e3a0911-7f09-46eb-ad69-4fd70d482c8d"  # usb_debug_pd.kicad_sch; U30's component sits on USB DP Alt mode
U30_UNIT2 = "1b28da22-bf3b-491c-a1c3-d55b1c37c668"  # usb_debug_pd.kicad_sch
NAMED_DIFFERENTLY = "the sheets and kicad-cli's netlist name a symbol differently: "


def _drop_netlist_nodes(out: Path, nodes: set[tuple[str, str]]) -> None:
    """Remove each ``(node (ref R) (pin P) ...)`` with ``(R, P)`` in ``nodes`` from the netlist's
    nets, through the S-expression API; asserts that each was there."""
    root = write_layer.load(out)
    dropped = set()
    for block in root.find_children("nets"):
        for net in block.find_children("net"):
            keep = []
            for child in net.children:
                key = None
                if child.name == "node":
                    key = (child.find_children("ref")[0].get_string(0), child.find_children("pin")[0].get_string(0))
                if key in nodes:
                    dropped.add(key)
                else:
                    keep.append(child)
            net.children[:] = keep
    assert dropped == nodes, f"not in the netlist: {nodes - dropped}"
    out.write_text(write_layer.serialise(root), encoding="utf-8")


U30_LIB = "antmicroInterfaceControllers:TPS65988DHRSHR"


def _add_shared_pin(sheet: Path, lib: str, body: str, number: str) -> None:
    """Give the embedded ``(lib_symbols (symbol <lib> ...))`` entry a unit 0 body (``<name>_0_1``,
    shared by every unit) holding a copy of pin ``number`` of ``body``, through the write layer."""
    import copy

    from kicad_tools.sexp.parser import SExp

    root = write_layer.load(sheet)
    (table,) = root.find_children("lib_symbols")
    (entry,) = [c for c in table.find_children("symbol") if c.get_string(0) == lib]
    (source,) = [c for c in entry.find_children("symbol") if c.get_string(0) == body]
    (pin,) = [p for p in source.find_children("pin") if p.find_children("number")[0].get_string(0) == number]
    shared = body.rsplit("_", 2)[0] + "_0_1"
    assert not [c for c in entry.find_children("symbol") if c.get_string(0) == shared]
    at = entry.children.index(entry.find_children("symbol")[0])  # bodies stay together, unit 0 first
    entry.children.insert(at, SExp.list("symbol", write_layer.string(shared), copy.deepcopy(pin)))
    write_layer.save(root, sheet)


def _reason_problems(reason: str, prefix: str) -> set[str]:
    """The ``; ``-separated problems after ``prefix`` in a refusal reason."""
    assert reason.startswith(prefix), reason
    return set(reason[len(prefix) :].split("; "))


def test_a_unit_on_another_sheet_is_checked_pin_by_pin(kicad_demo, tmp_path: Path) -> None:
    """jetson-agx-thor-baseboard: kicad-cli's component J1 sits on the SoM_IO sheet and lists
    the uuids of units 4, 8, 9 and 11 only; units 1 (SoM_Power) and 2, 3, 5, 6, 7, 10 (SoM_IO2)
    are on other sheets. U30's component sits on "USB DP Alt mode" (unit 3); units 1 and 2 are
    on "USB Debug, PD". Build ticket 68 accepted any unlisted unit whose reference kicad-cli
    places on another sheet. Now each pin of such a unit must appear in the netlist's nets as
    (reference, pin). The netlist is edited three ways: the node of U30's pin 40 (unit 1,
    unconnected: its net is ``unconnected-(U30A-GPIO12-Pad40)``) and of J1's pin B3 (unit 1,
    on net ``SYS_VIN_HV``) are dropped, and J1 unit 4's uuid is dropped from J1's own
    ``tstamps``. A unit's pins include the shared unit 0's: usb_debug_pd.kicad_sch's embedded
    TPS65988 gets a unit 0 body holding a copy of unit 2's pin 13, and every node U30 pin 13
    is dropped, so units 1 and 2 both miss it. Exactly these are refused: every other unit on
    another sheet (J1's seven) passes the pin check."""
    from kicad_tools.operations.netlist import Netlist

    project = kicad_demo(JETSON)
    _add_shared_pin(project / "usb_debug_pd.kicad_sch", U30_LIB, "TPS65988DHRSHR_2_1", "13")

    def exporter(sch: Path, out: Path):
        si._kicad_cli_netlist(sch, out)
        _drop_netlist_nodes(out, {("U30", "40"), ("J1", "B3"), ("U30", "13")})
        _edit_netlist_uuids(out, {J1_UNIT4: None})
        return Netlist.load(out)

    result = si.propose_schematic_import(
        project / f"{JETSON}.kicad_pcb", output_dir=tmp_path / "out", netlist_exporter=exporter
    )

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert _reason_problems(result["reason"], NAMED_DIFFERENTLY) == {
        f"J1 uuid {J1_UNIT4} in som_io.kicad_sch: kicad-cli's netlist does not list it",
        f"U30 unit 1 uuid {U30_UNIT1} in usb_debug_pd.kicad_sch: a unit on another sheet than "
        "kicad-cli's U30, and kicad-cli's netlist has no node U30 pin 13, 40",
        f"U30 unit 2 uuid {U30_UNIT2} in usb_debug_pd.kicad_sch: a unit on another sheet than "
        "kicad-cli's U30, and kicad-cli's netlist has no node U30 pin 13",
        f"J1 unit 1 uuid {J1_UNIT1} in som_power.kicad_sch: a unit on another sheet than "
        "kicad-cli's J1, and kicad-cli's netlist has no node J1 pin B3",
    }, result["reason"]


J1_LIB = "antmicroB2BConnectors:Hermaphroditic_Molex_203456-0003_CUSTOM_Jetson_AGX_Thor_H8mm"


def _drop_lib_body(sheet: Path, lib: str, body: str) -> None:
    """Remove the body ``(symbol <body> ...)`` from the embedded ``(lib_symbols (symbol <lib>
    ...))`` entry, through the write layer."""
    root = write_layer.load(sheet)
    (table,) = root.find_children("lib_symbols")
    (entry,) = [c for c in table.find_children("symbol") if c.get_string(0) == lib]
    before = len(entry.children)
    entry.children[:] = [c for c in entry.children if not (c.name == "symbol" and c.get_string(0) == body)]
    assert len(entry.children) == before - 1, f"no body {body} in {lib} in {sheet}"
    write_layer.save(root, sheet)


def test_a_unit_on_another_sheet_without_readable_pins_is_refused(kicad_demo, tmp_path: Path) -> None:
    """jetson-agx-thor-baseboard: the body of J1's unit 2 (``..._2_1``) is removed from
    som_io2.kicad_sch's embedded lib symbol. Units 3 to 11 keep theirs, so the unit count (11)
    still agrees with the other sheets and the duplicate scan passes. Unit 2 sits on another
    sheet than kicad-cli's J1, and none of its pins can be read: unknown fails closed."""
    project = kicad_demo(JETSON)
    _drop_lib_body(project / "som_io2.kicad_sch", J1_LIB, "Hermaphroditic_Molex_203456-0003_CUSTOM_Jetson_AGX_Thor_H8mm_2_1")

    result = si.propose_schematic_import(project / f"{JETSON}.kicad_pcb", output_dir=tmp_path / "out")

    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert _reason_problems(result["reason"], NAMED_DIFFERENTLY) == {
        f"J1 unit 2 uuid {J1_UNIT2} in som_io2.kicad_sch: a unit on another sheet than kicad-cli's J1, "
        "and no pin of unit 2 can be read from the sheet's embedded lib symbol",
    }, result["reason"]


def test_an_off_board_symbol_is_not_compared_with_the_netlist(kicad_demo, tmp_path: Path) -> None:
    """sonde xilinx: R2 marked ``(on_board no)``. kicad-cli's netlist leaves it out, as it
    should; the per-uuid comparison must not call that a mismatch."""
    project = kicad_demo("sonde xilinx")
    _set_off_board(project / "sonde xilinx.kicad_sch", R2)

    result = si.propose_schematic_import(project / "sonde xilinx.kicad_pcb", output_dir=tmp_path / "out")

    assert "name a symbol differently" not in str(result.get("reason", "")), result.get("reason")
    assert "disagree" not in str(result.get("reason", "")), result.get("reason")
