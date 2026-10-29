"""Every schematic reader walks the sheet tree from the root, never a glob (build ticket 26).

The fixture (``tests/_sheet_tree_fixture.py``) is KiCad's ``complex_hierarchy`` demo plus
stray copies whose references, values and labels differ from the live sheets: a kicad_tools
style ``*_backup_*`` copy, two ``.history/`` copies, an orphan sub-sheet the root does not
reference, and a same-named copy of the sub-sheet in the working directory. The oracle is
``kicad-cli sch export netlist`` of the root, exported fresh in each test. Each reader's
output is compared with it as a set of references or nets, never as a count.
"""
from __future__ import annotations

import csv
import hashlib
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import sheet_tree
from tests._sheet_tree_fixture import STRAY_REF, build_stray_project, oracle_netlist, strayify

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def _tools(module) -> dict:
    mcp = MCPServer("test")
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _hashes(paths) -> dict[Path, str]:
    return {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


def _backups(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.rglob("*") if "_backup_" in p.name and p.name != "ampli_ht_backup_20261019_120000.kicad_sch")


@pytest.fixture(params=[False, True], ids=["Sheetfile", "legacy-Sheet-file"])
def project(request, kicad_demo, tmp_path, monkeypatch):
    """The stray project, with the working directory set to the one holding a stray copy."""
    proj = build_stray_project(kicad_demo, tmp_path, legacy=request.param)
    monkeypatch.chdir(proj.cwd)
    return proj


@pytest.fixture
def oracle(project, tmp_path):
    return oracle_netlist(project.root, tmp_path / "oracle")


# --- the walk itself ------------------------------------------------------------------------


def test_walk_reaches_the_live_sheets_only_and_lists_every_stray(project) -> None:
    tree = sheet_tree.walk(project.root)

    assert [tree.rel(f) for f in tree.files] == ["complex_hierarchy.kicad_sch", "ampli_ht.kicad_sch"]
    assert tree.problems == ()
    assert list(tree.unreferenced) == project.unreferenced
    assert set(project.unreferenced) == {
        ".history/ampli_ht.kicad_sch",
        ".history/complex_hierarchy.kicad_sch",
        "ampli_ht_backup_20261019_120000.kicad_sch",
        "orphan.kicad_sch",
    }


def test_walk_never_parses_an_unreferenced_sheet(project, monkeypatch) -> None:
    parsed: list[Path] = []
    real = sheet_tree._parse

    def spy(path):
        parsed.append(Path(path).resolve())
        return real(path)

    monkeypatch.setattr(sheet_tree, "_parse", spy)
    tree = sheet_tree.walk(project.root)
    sheet_tree.placed_references(tree)

    assert {p.name for p in parsed} == {"complex_hierarchy.kicad_sch", "ampli_ht.kicad_sch"}
    assert not {p.resolve() for p in project.strays} & set(parsed)


def test_legacy_sheet_file_key_is_followed_and_marked(project) -> None:
    tree = sheet_tree.walk(project.root)
    legacy = [tree.rel(f) for f in tree.legacy_files]
    uses_legacy = "Sheet file" in project.root.read_text(encoding="utf-8")

    assert legacy == (["ampli_ht.kicad_sch"] if uses_legacy else [])


def test_placed_references_match_the_oracle(project, oracle) -> None:
    refs = {ref for ref, _ in sheet_tree.placed_references(sheet_tree.walk(project.root)) if not ref.startswith("#")}

    assert refs == oracle.refs


def test_a_missing_sub_sheet_is_a_problem_never_a_silent_drop(project) -> None:
    (project.dir / "ampli_ht.kicad_sch").unlink()

    tree = sheet_tree.walk(project.root)

    assert tree.problems == ("sheet file missing: ampli_ht.kicad_sch",)


def test_a_sub_sheet_in_a_subdirectory_resolves_against_its_parent_sheet(tmp_path, kicad_demo) -> None:
    """KiCad resolves a sheet file relative to the sheet that holds it; a same-named copy
    beside the root (or in the working directory) must never stand in for it."""
    from kicad_tools.sexp import Document

    proj = build_stray_project(kicad_demo, tmp_path)
    sub_dir = proj.dir / "sub"
    sub_dir.mkdir()
    (proj.dir / "ampli_ht.kicad_sch").rename(sub_dir / "ampli_ht.kicad_sch")
    strayify(sub_dir / "ampli_ht.kicad_sch", proj.dir / "ampli_ht.kicad_sch", mode="9")
    doc = Document.load(proj.root)
    for sheet in doc.root.find_children("sheet"):
        for prop in sheet.find_children("property"):
            if prop.get_string(0) == "Sheetfile":
                prop.set_value(1, "sub/ampli_ht.kicad_sch")
    doc.save(proj.root)

    tree = sheet_tree.walk(proj.root)

    assert [tree.rel(f) for f in tree.files] == ["complex_hierarchy.kicad_sch", "sub/ampli_ht.kicad_sch"]
    assert "ampli_ht.kicad_sch" in tree.unreferenced


# --- the readers ----------------------------------------------------------------------------


def test_bom_matches_the_oracle_and_reads_no_stray(project, oracle, tmp_path) -> None:
    """Reproduced 2026-10-20: kicad_tools' build_hierarchy resolved ampli_ht.kicad_sch in the
    working directory first, so the BOM listed C7201... from the stray copy; it gave both uses
    of the sheet the first use's references, so the 29 parts of the second use (C301...) were
    missing; and it listed the project library's power symbols and PWR_FLAGs (#U0101...)."""
    from kicad_mcp.toolsets import manufacturing

    result = manufacturing.export_manufacturing_package(project.pcb, tmp_path / "mfg")

    assert result["status"] == "ok", result
    with open(result["bom"], newline="", encoding="utf-8") as fh:
        designators = {
            ref.strip() for row in csv.DictReader(fh) for ref in row["Designator"].split(",") if ref.strip()
        }
    assert designators == oracle.bom_refs
    assert not {r for r in designators if STRAY_REF.search(r)}
    assert result["unreferenced_sheets"] == project.unreferenced


def test_bom_is_refused_when_a_sub_sheet_is_missing(project, tmp_path) -> None:
    from kicad_mcp.toolsets import manufacturing

    (project.dir / "ampli_ht.kicad_sch").unlink()
    result = manufacturing.export_manufacturing_package(project.pcb, tmp_path / "mfg")

    assert result["status"] != "ok"
    assert result["bom"] is None
    assert any("ampli_ht.kicad_sch" in e for e in result["errors"]), result


def test_schematic_import_plan_matches_the_oracle(project, oracle, tmp_path) -> None:
    from kicad_mcp import schematic_import

    plan = schematic_import.propose_schematic_import(project.pcb, output_dir=tmp_path / "imported")

    assert plan["status"] == "proposed", plan
    assert {name: sorted(nodes) for name, nodes in plan["nets"].items()} == {
        name: nodes for name, nodes in oracle.nets.items() if name in plan["nets"]
    }
    assert {p.split(".")[0] for nodes in plan["nets"].values() for p in nodes} <= oracle.refs
    assert plan["unreferenced_sheets"] == project.unreferenced


def test_schematic_import_duplicate_scan_reads_every_live_sheet(project, oracle) -> None:
    """The duplicate-reference scan missed every sub-sheet behind a KiCad 6 ``Sheet file``."""
    from kicad_mcp import schematic_import

    refs = {r["reference"] for r in schematic_import._placed_symbols(project.root)}

    assert refs == oracle.refs


def test_the_gates_annotation_walk_reads_no_stray(project) -> None:
    """The stray .history copies hold ``R?`` references: reading one would be a problem."""
    from kicad_mcp import verify_board

    assert verify_board._annotation_problems(project.root) == []


def test_check_connectivity_reads_every_live_sheet_and_no_stray(project, oracle) -> None:
    from kicad_mcp.toolsets import schematic

    result = _tools(schematic)["check_connectivity"](str(project.root))

    assert result["status"] == "ok", result
    rows = result["unconnected_pins"] + result["missing_from_netlist"]
    refs = {r["reference"] for r in rows}
    live = {ref for ref, _ in sheet_tree.placed_references(sheet_tree.walk(project.root))}
    assert refs <= live, refs - live
    assert not {r for r in refs if STRAY_REF.search(r)}
    # which sheets were analysed, not which pins kicad_tools flags: that differs between
    # kicad_tools 0.20.0 and 0.23.0 (0.23.0 flags no pin in ampli_ht.kicad_sch)
    assert result["analysed_sheets"] == ["complex_hierarchy.kicad_sch", "ampli_ht.kicad_sch"]
    assert not result["missing_from_netlist"] or {r["sheet"] for r in result["missing_from_netlist"]} <= {
        "",
        "ampli_ht.kicad_sch",
    }
    assert result["unreferenced_sheets"] == project.unreferenced


def test_check_connectivity_is_the_same_for_both_sheet_file_spellings(kicad_demo, tmp_path) -> None:
    from kicad_mcp.toolsets import schematic

    check = _tools(schematic)["check_connectivity"]
    plain = build_stray_project(kicad_demo, tmp_path / "a")
    legacy = build_stray_project(kicad_demo, tmp_path / "b", legacy=True)

    def pins(result):
        return sorted((r["reference"], r["pin_number"], r["sheet"]) for r in result["unconnected_pins"])

    assert pins(check(str(plain.root))) == pins(check(str(legacy.root)))


def _uses_legacy_keys(project) -> bool:
    return "Sheet file" in project.root.read_text(encoding="utf-8")


def _assert_refused_for_legacy_keys(project, result, before) -> None:
    """kicad_tools' own walk would skip the sub-sheet behind "Sheet file" and still report
    success, so a tool that lets kicad_tools write refuses before writing anything."""
    assert result["status"] == "incomplete", result
    assert "Sheet file" in result["reason"] and "ampli_ht.kicad_sch" in result["reason"]
    assert _hashes([*project.strays, project.root, project.dir / "ampli_ht.kicad_sch"]) == before
    assert result["unreferenced_sheets"] == project.unreferenced


def test_fix_annotation_reports_every_live_reference(project, oracle) -> None:
    from kicad_mcp.toolsets import schematic

    before = _hashes([*project.strays, project.root, project.dir / "ampli_ht.kicad_sch"])
    result = _tools(schematic)["fix_annotation"](str(project.root))

    if _uses_legacy_keys(project):
        return _assert_refused_for_legacy_keys(project, result, before)
    assert result["status"] == "ok", result
    for key in ("references_before", "references_after"):
        assert {r for r in result[key] if not r.startswith("#")} == oracle.refs, key
    assert _hashes(project.strays) == {p: before[p] for p in project.strays}
    assert result["unreferenced_sheets"] == project.unreferenced


def test_assign_footprints_verifies_against_the_oracle(project, tmp_path) -> None:
    from kicad_mcp.toolsets import schematic

    # C201 and C301 are the two uses of one symbol on the reused sheet, which holds one
    # footprint: both are named (build ticket 25 fix round 2 refuses naming one of them)
    mapping = {
        "C201": "Capacitor_SMD:C_0805_2012Metric",
        "C301": "Capacitor_SMD:C_0805_2012Metric",
        "C101": "Capacitor_SMD:C_1206_3216Metric",
    }
    before = _hashes([*project.strays, project.root, project.dir / "ampli_ht.kicad_sch"])
    result = _tools(schematic)["assign_footprints"](str(project.root), mapping)

    if _uses_legacy_keys(project):
        return _assert_refused_for_legacy_keys(project, result, before)
    after = oracle_netlist(project.root, tmp_path / "after")
    assert result["status"] == "ok", result
    assert result["assigned"] == mapping
    assert {ref: after.footprints[ref] for ref in mapping} == mapping
    # C301 is the second use of the same symbol: KiCad gives it the same footprint
    assert after.footprints["C301"] == mapping["C201"]
    assert _hashes(project.strays) == {p: before[p] for p in project.strays}
    assert result["unreferenced_sheets"] == project.unreferenced


def test_schematic_tools_leave_no_backup_in_the_project(project) -> None:
    from kicad_mcp.toolsets import schematic

    tools = _tools(schematic)
    both = {"C201": "Capacitor_SMD:C_0805_2012Metric", "C301": "Capacitor_SMD:C_0805_2012Metric"}
    tools["assign_footprints"](str(project.root), both)
    tools["fix_annotation"](str(project.root))

    assert _backups(project.dir) == []


def test_project_open_lists_the_live_references(project, oracle) -> None:
    from kicad_mcp.toolsets import project as project_toolset

    opened = project_toolset._project_open(str(project.dir))

    assert set(opened["references"]) == oracle.refs
    assert opened["sheets"] == ["complex_hierarchy.kicad_sch", "ampli_ht.kicad_sch"]
    assert opened["unreferenced_sheets"] == project.unreferenced


def test_erc_is_unaffected_by_the_strays(project, kicad_demo, tmp_path) -> None:
    from kicad_mcp.toolsets import verification
    from tests._sheet_tree_fixture import use_legacy_sheet_keys

    clean = kicad_demo("complex_hierarchy")
    if "Sheet file" in project.root.read_text(encoding="utf-8"):
        use_legacy_sheet_keys(clean / "complex_hierarchy.kicad_sch")
    erc = _tools(verification)["sch_run_erc"]

    def violations(result):
        assert result["status"] == "complete", result
        return sorted(str(v) for v in result.get("violations", []))

    assert violations(erc(str(project.root))) == violations(erc(str(clean / "complex_hierarchy.kicad_sch")))


def test_file_schematic_state_names_the_sheet_file_for_both_spellings(project) -> None:
    from kicad_mcp.ipc import adapter

    backend = adapter.FileBackend(sch_path=project.root)
    state = backend.schematic_state(include=["sheets"])

    rows = state["sheets"]["items"] if isinstance(state["sheets"], dict) else state["sheets"]
    assert [r["filename"] for r in rows] == ["ampli_ht.kicad_sch", "ampli_ht.kicad_sch"]


# --- fail closed ----------------------------------------------------------------------------


@pytest.mark.parametrize("tool", ["check_connectivity", "fix_annotation", "assign_footprints"])
def test_schematic_tools_refuse_a_tree_with_a_missing_sheet(project, tool) -> None:
    """kicad_tools' walk skips a missing sub-sheet without a word; the tools say so instead."""
    from kicad_mcp.toolsets import schematic

    (project.dir / "ampli_ht.kicad_sch").unlink()
    before = _hashes([project.root, *project.strays])
    args = (str(project.root), {"C101": "Capacitor_SMD:C_0805_2012Metric"}) if tool == "assign_footprints" else (str(project.root),)

    result = _tools(schematic)[tool](*args)

    assert result["status"] == "incomplete", result
    assert "sheet file missing: ampli_ht.kicad_sch" in result["reason"]
    assert _hashes([project.root, *project.strays]) == before


def test_check_connectivity_refuses_when_kicad_tools_reads_a_stray(project, monkeypatch) -> None:
    from kicad_tools.cli import sch_set_footprint

    from kicad_mcp.toolsets import schematic

    stray = project.dir / "orphan.kicad_sch"
    real = sch_set_footprint._collect_schematic_files
    monkeypatch.setattr(sch_set_footprint, "_collect_schematic_files", lambda root: [*real(root), stray])

    result = _tools(schematic)["check_connectivity"](str(project.root))

    assert result["status"] == "incomplete", result
    assert "orphan.kicad_sch" in result["reason"]


def test_backups_go_outside_the_project_and_hold_every_sheet(kicad_demo, tmp_path) -> None:
    from kicad_mcp.toolsets import schematic

    proj = build_stray_project(kicad_demo, tmp_path)
    before = _hashes([proj.root, proj.dir / "ampli_ht.kicad_sch"])
    both = {"C201": "Capacitor_SMD:C_0805_2012Metric", "C301": "Capacitor_SMD:C_0805_2012Metric"}
    result = _tools(schematic)["assign_footprints"](str(proj.root), both)

    backup = Path(result["backup_dir"])
    assert proj.dir.resolve() not in backup.resolve().parents
    copies = {backup / "complex_hierarchy.kicad_sch": proj.root, backup / "ampli_ht.kicad_sch": proj.dir / "ampli_ht.kicad_sch"}
    assert {orig: hashlib.sha256(copy.read_bytes()).hexdigest() for copy, orig in copies.items()} == before
    assert _backups(proj.dir) == []


# --- the walk's own edge cases, on sheets built through kicad_tools' S-expression API -------


def _q(text: str):
    from kicad_tools.sexp.parser import SExp

    return SExp.quoted_atom(text)


def _sheet(path: Path, *subs: tuple[str, str]) -> Path:
    """A schematic holding one ``(sheet ...)`` node per ``(key, file)`` in ``subs``."""
    from kicad_tools.sexp.parser import SExp, serialize_sexp

    root = SExp.list("kicad_sch", SExp.list("version", 20250114))
    for key, file in subs:
        node = SExp.list("sheet", SExp.list("property", _q("Sheetname"), _q(file or "noname")))
        if key:
            node.append(SExp.list("property", _q(key), _q(file)))
        root.append(node)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(serialize_sexp(root), encoding="utf-8")
    return path


def test_walk_reports_a_cycle(tmp_path) -> None:
    root = _sheet(tmp_path / "p" / "root.kicad_sch", ("Sheetfile", "a.kicad_sch"))
    _sheet(tmp_path / "p" / "a.kicad_sch", ("Sheetfile", "root.kicad_sch"))

    tree = sheet_tree.walk(root)

    assert tree.problems == ("sheet hierarchy cycle through root.kicad_sch",)


@pytest.mark.parametrize("name", ["../outside.kicad_sch", "C:/elsewhere/x.kicad_sch", "/abs/x.kicad_sch"])
def test_walk_reports_a_sheet_outside_the_project(tmp_path, name) -> None:
    _sheet(tmp_path / "outside.kicad_sch")
    root = _sheet(tmp_path / "p" / "root.kicad_sch", ("Sheetfile", name))

    tree = sheet_tree.walk(root)

    assert tree.problems == (f"sheet file outside the project: {name} (in root.kicad_sch)",)
    assert [p.name for p in tree.files] == ["root.kicad_sch"]


def test_walk_reports_a_sheet_node_without_a_file(tmp_path) -> None:
    root = _sheet(tmp_path / "p" / "root.kicad_sch", ("", "nofile"))

    assert sheet_tree.walk(root).problems == ("sheet 'nofile' in root.kicad_sch has no sheet file property",)


def test_walk_reports_an_unparseable_sheet(tmp_path) -> None:
    """The bad sheet is a well-formed S-expression with the wrong root tag (``kicad_pcb``),
    built through the S-expression API: the parser's own error path is not ours to test."""
    from kicad_tools.sexp.parser import SExp, serialize_sexp

    root = _sheet(tmp_path / "p" / "root.kicad_sch", ("Sheetfile", "bad.kicad_sch"))
    (tmp_path / "p" / "bad.kicad_sch").write_text(
        serialize_sexp(SExp.list("kicad_pcb", SExp.list("version", 20250114))), encoding="utf-8"
    )

    (problem,) = sheet_tree.walk(root).problems
    assert problem.startswith("sheet bad.kicad_sch could not be parsed as a kicad_sch")


def test_walk_reports_each_problem_once_for_a_sheet_used_twice(tmp_path) -> None:
    root = _sheet(tmp_path / "p" / "root.kicad_sch", ("Sheetfile", "a.kicad_sch"), ("Sheetfile", "a.kicad_sch"))
    _sheet(tmp_path / "p" / "a.kicad_sch", ("", "nofile"))

    tree = sheet_tree.walk(root)

    assert tree.problems == ("sheet 'nofile' in a.kicad_sch has no sheet file property",)
    assert len(tree.uses) == 3  # the root and both uses of a.kicad_sch


# --- fix round 1 (review of 5b4eef7) -------------------------------------------------------


def _export(project_dir_pcb: Path, out: Path, **kw) -> dict:
    from kicad_mcp.toolsets import manufacturing

    return manufacturing.export_manufacturing_package(project_dir_pcb, out, **kw)


def _bom_rows(result: dict) -> dict[str, str]:
    """Designator -> LCSC column of the BOM file."""
    rows: dict[str, str] = {}
    with open(result["bom"], newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            for ref in row["Designator"].split(","):
                if ref.strip():
                    rows[ref.strip()] = row["LCSC Part #"]
    return rows


@pytest.fixture
def plain(kicad_demo, tmp_path):
    return build_stray_project(kicad_demo, tmp_path)


def test_bom_refuses_a_reference_that_names_two_symbols(plain, tmp_path) -> None:
    """C101 renamed to C102: kicad-cli keeps one C102, a set comparison saw nothing (67 of 68)."""
    from tests._sheet_tree_fixture import set_reference

    set_reference(plain.root, "C101", "C102")
    result = _export(plain.pcb, tmp_path / "mfg")

    assert result["status"] != "ok" and result["bom"] is None, result
    assert any("C102" in e and "more than one" in e for e in result["errors"]), result["errors"]
    assert not list((tmp_path / "mfg").glob("bom_*.csv"))


def test_bom_refuses_an_unannotated_part(plain, tmp_path) -> None:
    from tests._sheet_tree_fixture import set_reference

    set_reference(plain.root, "C101", "C?")
    result = _export(plain.pcb, tmp_path / "mfg")

    assert result["status"] != "ok" and result["bom"] is None, result
    assert any("C?" in e and "annotated" in e for e in result["errors"]), result["errors"]


def test_a_refused_bom_leaves_no_bom_file_from_an_earlier_run(plain, tmp_path) -> None:
    out = tmp_path / "mfg"
    first = _export(plain.pcb, out)
    assert first["status"] == "ok" and Path(first["bom"]).is_file(), first

    (plain.dir / "ampli_ht.kicad_sch").unlink()
    second = _export(plain.pcb, out)

    assert second["bom"] is None
    assert not list(out.glob("bom_*.csv")), "the previous run's BOM is still there"


def test_missing_root_says_how_to_pass_schematic_path(plain, tmp_path) -> None:
    plain.root.rename(plain.root.with_name("renamed.kicad_sch"))

    result = _export(plain.pcb, tmp_path / "mfg")

    assert result["bom"] is None
    assert any("schematic_path" in e and "<board>.kicad_sch" in e for e in result["errors"]), result["errors"]


@pytest.mark.parametrize("mode", ["netlist-lacks-a-placed-part", "netlist-has-an-unplaced-part", "netlist-lists-a-part-twice"])
def test_bom_refuses_a_netlist_that_disagrees_with_the_sheets(plain, tmp_path, monkeypatch, mode) -> None:
    """kicad-cli drops an unreadable sub-sheet with exit 0: the BOM is only built when its
    netlist and the placed instances agree, both ways."""
    real = sheet_tree.netlist_components

    def tampered(root):
        comps = real(root)
        if mode == "netlist-lacks-a-placed-part":
            return [c for c in comps if c["ref"] != "C101"]
        extra = dict(comps[0], ref="X999" if mode == "netlist-has-an-unplaced-part" else comps[0]["ref"])
        return [*comps, extra]

    monkeypatch.setattr(sheet_tree, "netlist_components", tampered)
    result = _export(plain.pcb, tmp_path / "mfg")

    assert result["bom"] is None, result
    needle = {"netlist-lacks-a-placed-part": "C101", "netlist-has-an-unplaced-part": "X999"}.get(mode, "more than once")
    assert any(needle in e for e in result["errors"]), result["errors"]


@pytest.mark.parametrize(
    "demo,root",
    [("cm5_minima", "CM5_MINIMA_3"), ("royalblue54L_feather", "RoyalBlue54L-Feather")],
)
def test_bom_of_a_real_board_matches_the_oracle_with_dnp_exclusions_and_lcsc(kicad_demo, tmp_path, demo, root) -> None:
    """cm5_minima: (on_board no) symbols, exclude_from_bom parts, "LCSC" and "LCSC Part"
    fields. RoyalBlue54L-Feather: DNP-only and exclude_from_bom-only parts, sheets in sch/."""
    project = kicad_demo(demo)
    sch = project / f"{root}.kicad_sch"
    oracle = oracle_netlist(sch, tmp_path / "oracle")

    result = _export(project / f"{root}.kicad_pcb", tmp_path / "mfg")

    assert result["status"] == "ok", result
    rows = _bom_rows(result)
    assert set(rows) == oracle.bom_refs
    assert {ref: col for ref, col in rows.items() if col} == {r: v for r, v in oracle.lcsc.items() if r in oracle.bom_refs}
    assert oracle.lcsc, "the demo should carry LCSC fields"


def test_a_multi_unit_part_is_not_a_duplicate(kicad_demo) -> None:
    """sonde xilinx: U1 and U2 have four units each, each unit with its own rescued lib id."""
    project = kicad_demo("sonde xilinx")
    root = project / "sonde xilinx.kicad_sch"
    tree = sheet_tree.walk(root)
    refs = [c["ref"] for c in sheet_tree.netlist_components(root)]

    assert sheet_tree.netlist_disagreement(tree, refs) == []


def test_assign_footprints_reports_a_reference_it_could_not_assign(plain) -> None:
    from kicad_mcp.toolsets import schematic

    result = _tools(schematic)["assign_footprints"](
        str(plain.root), {"C101": "Capacitor_SMD:C_0805_2012Metric", "C999": "Capacitor_SMD:C_0805_2012Metric"}
    )

    assert result["status"] == "partial", result
    assert result["assigned"] == {"C101": "Capacitor_SMD:C_0805_2012Metric"}
    assert result["not_assigned"] == ["C999"]


def test_assign_footprints_keeps_backup_dir_when_the_read_back_fails(plain, monkeypatch) -> None:
    from kicad_mcp.toolsets import schematic

    before = plain.root.read_bytes()
    calls = []
    real = sheet_tree.netlist_components

    def broken_after_the_write(root):
        # the first call is build ticket 25's netlist before the write (fix round 2)
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("netlist export failed")
        return real(root)

    monkeypatch.setattr(sheet_tree, "netlist_components", broken_after_the_write)
    result = _tools(schematic)["assign_footprints"](str(plain.root), {"C101": "Capacitor_SMD:C_0805_2012Metric"})

    assert result["status"] == "incomplete", result
    assert (Path(result["backup_dir"]) / plain.root.name).read_bytes() == before
    assert plain.root.read_bytes() != before  # the write happened: the backup is the way back


def test_fix_annotation_keeps_backup_dir_when_the_read_back_fails(plain, monkeypatch) -> None:
    from kicad_mcp.toolsets import schematic

    calls = []
    real = sheet_tree.placed_references

    def second_call_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) > 1:
            raise RuntimeError("read-back failed")
        return real(*args, **kwargs)

    monkeypatch.setattr(sheet_tree, "placed_references", second_call_fails)
    result = _tools(schematic)["fix_annotation"](str(plain.root))

    assert result["status"] == "incomplete", result
    assert (Path(result["backup_dir"]) / "ampli_ht.kicad_sch").is_file()


@pytest.mark.parametrize("tool", ["fix_annotation", "assign_footprints"])
@pytest.mark.parametrize("where", ["bk", "."])
def test_a_backup_root_inside_the_project_is_refused_and_nothing_is_written(plain, monkeypatch, tool, where) -> None:
    """Backups inside the project would be the very stray copies this ticket is about."""
    from kicad_mcp.toolsets import schematic

    backup_root = plain.dir / where
    monkeypatch.setenv("KICAD_MCP_BACKUP_DIR", str(backup_root))
    files_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in plain.dir.rglob("*") if p.is_file()}
    args = (str(plain.root), {"C101": "Capacitor_SMD:C_0805_2012Metric"}) if tool == "assign_footprints" else (str(plain.root),)

    result = _tools(schematic)[tool](*args)

    assert result["status"] == "incomplete", result
    assert "inside the project" in result["reason"] and "backup_dir" not in result
    files_after = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in plain.dir.rglob("*") if p.is_file()}
    assert files_after == files_before
    assert where == "." or not backup_root.exists()


def test_backups_go_to_the_configured_durable_directory(plain, tmp_path, monkeypatch) -> None:
    from kicad_mcp.toolsets import schematic

    monkeypatch.setenv("KICAD_MCP_BACKUP_DIR", str(tmp_path / "backups"))
    result = _tools(schematic)["fix_annotation"](str(plain.root))

    assert Path(result["backup_dir"]).parent == tmp_path / "backups"


def test_backup_root_defaults_to_a_per_user_data_directory(tmp_path, monkeypatch) -> None:
    import os

    from kicad_mcp.toolsets import schematic

    monkeypatch.delenv("KICAD_MCP_BACKUP_DIR", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    expected = (tmp_path / "local") if os.name == "nt" else (tmp_path / "xdg")

    assert schematic.backup_root() == expected / "kicad-mcp" / "schematic-backups"


def test_this_projects_instance_entry_wins_over_another_projects(plain, tmp_path) -> None:
    """A copied project keeps its uuids, so another project's entry can carry the same path;
    kicad-cli uses this project's (checked: C101, not C555)."""
    from tests._sheet_tree_fixture import add_instance_entry

    root_path = "/" + sheet_tree._uuid(sheet_tree._parse(plain.root))
    add_instance_entry(plain.root, "C101", project="other_project", path=root_path, new_ref="C555", first=True)
    oracle = oracle_netlist(plain.root, tmp_path / "oracle")

    refs = {r for r, _ in sheet_tree.placed_references(sheet_tree.walk(plain.root)) if not r.startswith("#")}

    assert "C555" not in oracle.refs and refs == oracle.refs


def test_a_stale_instance_path_is_not_a_placed_symbol(plain, tmp_path) -> None:
    """An instance entry for a sheet use that no longer exists: kicad-cli ignores it, and so
    must the walk (counting it made the BOM refuse a valid project)."""
    from tests._sheet_tree_fixture import add_instance_entry

    root_path = "/" + sheet_tree._uuid(sheet_tree._parse(plain.root))
    add_instance_entry(
        plain.dir / "ampli_ht.kicad_sch",
        "C201",
        project="complex_hierarchy",
        path=f"{root_path}/00000000-0000-0000-0000-0000deadbeef",
        new_ref="C777",
        first=False,
    )
    oracle = oracle_netlist(plain.root, tmp_path / "oracle")
    tree = sheet_tree.walk(plain.root)

    refs = {r for r, _ in sheet_tree.placed_references(tree) if not r.startswith("#")}

    assert "C777" not in oracle.refs and refs == oracle.refs
    assert _export(plain.pcb, tmp_path / "mfg")["status"] == "ok"


def test_kicad6_symbol_instances_name_the_parts_of_a_reused_sheet(plain, tmp_path) -> None:
    """KiCad 6 kept instance data in the root's (symbol_instances ...) table; ampli_ht is used
    twice, so each use's references come only from that table (the properties say X1)."""
    from tests._sheet_tree_fixture import use_kicad6_instance_data

    use_kicad6_instance_data(plain.root, plain.dir / "ampli_ht.kicad_sch")
    oracle = oracle_netlist(plain.root, tmp_path / "oracle")

    refs = {r for r, _ in sheet_tree.placed_references(sheet_tree.walk(plain.root)) if not r.startswith("#")}

    assert "X1" not in oracle.refs and len(oracle.refs) == 68
    assert refs == oracle.refs


def test_project_open_is_incomplete_when_a_sheet_is_missing(plain) -> None:
    from kicad_mcp.toolsets import project as project_toolset

    (plain.dir / "ampli_ht.kicad_sch").unlink()
    opened = project_toolset._project_open(str(plain.dir))

    assert opened["status"] == "incomplete"
    assert opened["sheet_problems"] == ["sheet file missing: ampli_ht.kicad_sch"]


# --- a rule broken once becomes a test ---------------------------------------------------------

# Functions that list a directory and name .kicad_sch for another reason than reading sheets.
_DIRECTORY_SCAN_ALLOWED = {
    ("sheet_tree.py", "walk"): "lists unreferenced sheets, never reads them",
    ("gate_state.py", "_scan"): "stats project inputs to stale a verdict; never parses a sheet",
}
_LISTING_CALLS = {"iterdir", "scandir", "listdir"}
_GLOB_CALLS = {"glob", "rglob", "iglob"}
_BANNED_CALLS = {"build_hierarchy", "extract_bom", "extract_bom_hierarchical", "find_schematic", "iter_missing_footprint_symbols"}


def _strings(node) -> list[str]:
    """String constants under ``node``, f-string parts included."""
    import ast

    return [n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _scan_source(text: str, filename: str) -> list[str]:
    """Banned kicad_tools tree walkers, and functions that list a directory (os.walk, iterdir,
    scandir, listdir, glob) and name ``kicad_sch`` (plain string, f-string part, or a module
    constant holding it), unless allowed above with its reason."""
    import ast

    tree = ast.parse(text)
    sch_constants = {
        target.id
        for node in tree.body
        if isinstance(node, ast.Assign) and any("kicad_sch" in s for s in _strings(node.value))
        for target in node.targets
        if isinstance(target, ast.Name)
    }

    def called(n) -> str:
        return n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", "")

    def lists(n) -> bool:
        """os.walk, iterdir, scandir, listdir, or a glob whose pattern is not a plain string
        naming another file type (``glob("*.kicad_pro")`` selects no sheet)."""
        if not isinstance(n, ast.Call):
            return False
        name = called(n)
        if name == "walk":
            return isinstance(n.func, ast.Attribute) and getattr(n.func.value, "id", "") == "os"
        if name in _GLOB_CALLS:
            first = n.args[0] if n.args else None
            return not (isinstance(first, ast.Constant) and isinstance(first.value, str) and "kicad_sch" not in first.value)
        return name in _LISTING_CALLS

    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and called(node) in _BANNED_CALLS:
            hits.append(f"{filename}:{node.lineno} {called(node)}")
        if isinstance(node, ast.ImportFrom) and any(a.name in _BANNED_CALLS for a in node.names):
            hits.append(f"{filename}:{node.lineno} import")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lists_a_directory = any(lists(n) for n in ast.walk(node))
            names_sch = any("kicad_sch" in s for s in _strings(node)) or any(
                isinstance(n, ast.Name) and n.id in sch_constants for n in ast.walk(node)
            )
            if lists_a_directory and names_sch and (filename, node.name) not in _DIRECTORY_SCAN_ALLOWED:
                hits.append(f"{filename}:{node.lineno} {node.name} lists a directory and names kicad_sch")
    return hits


def test_no_reader_in_src_globs_for_schematics_or_uses_kicad_tools_tree_walkers() -> None:
    """Static scan of src/kicad_mcp (build ticket 26), see ``_scan_source``."""
    import kicad_mcp

    hits = []
    for path in Path(kicad_mcp.__file__).parent.rglob("*.py"):
        hits += _scan_source(path.read_text(encoding="utf-8"), path.name)
    assert hits == []


def test_the_static_scan_catches_an_f_string_glob_and_a_walk_filter() -> None:
    src = "\n".join(
        [
            "import os",
            "from kicad_tools.schema.hierarchy import build_hierarchy",
            "def a(d, stem):",
            "    return list(d.glob(f'{stem}*.kicad_sch'))",
            "SUFFIX = '.kicad_sch'",
            "def b(d):",
            "    return [f for _, _, fs in os.walk(d) for f in fs if f.endswith(SUFFIX)]",
            "def c(d):",
            "    return [p for p in d.iterdir() if p.suffix == '.kicad_sch']",
        ]
    )

    hits = _scan_source(src, "example.py")

    assert [h.split(" ", 1)[1] for h in hits] == [
        "import",
        "a lists a directory and names kicad_sch",
        "b lists a directory and names kicad_sch",
        "c lists a directory and names kicad_sch",
    ]
