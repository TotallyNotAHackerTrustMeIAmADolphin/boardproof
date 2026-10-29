"""The gate's annotation check sees stale instance paths (build ticket 86).

KiCad 7 and later key a placed symbol's reference on its sheet use: the
``(instances (project P (path "/<root uuid>/<sheet uuid>..." (reference R))))`` entry whose
path is the use's instance path (``sheet_tree.walk``). An entry at a path no use has is
stale: a deleted or copied sheet leaves one, and a root sheet without its own ``(uuid ...)``
makes every path stale. Measured on kicad-cli 10.0.7 (build ticket 68, docs/ARCHITECTURE.md known
facts): a symbol whose entries are all stale takes its reference from a stale entry, so its
netlist reference is not the one this hierarchy defines. ``_annotation_problems`` used to
accept any entry of the project, whatever its path; it now names each such symbol with its
reference and sheet, the rule ``schematic_import._stale_only_entries`` uses (0 of 11166
measured sheet uses in 83 roots had that shape).

Fixtures are built through kicad_tools' S-expression builder (tiny sheets) or edited through
``kicad_mcp.write_layer`` (a demo copy), never by hand (CLAUDE.md hard rule 1).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_mcp import verify_board as gate
from kicad_mcp import write_layer
from kicad_mcp.verify_board import snapshot_rules, verify_board

PROJECT = "proj"
ROOT_UUID = "11111111-1111-4111-8111-111111111111"
OLD_ROOT_UUID = "99999999-9999-4999-8999-999999999999"
LETTER_UUID = "abcdefab-abcd-4bcd-8bcd-abcdefabcdef"
SHEET_A = "22222222-2222-4222-8222-222222222222"
SHEET_B = "33333333-3333-4333-8333-333333333333"
_EMPTY = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _q(text: str):
    from kicad_tools.sexp.parser import SExp

    return SExp.quoted_atom(text)


def _symbol(reference: str, uuid: str, entries: list[tuple[str, str, str]] = ()):
    """A placed ``Device:R`` with its Reference property and one ``(instances ...)`` entry per
    ``(project, path, reference)``."""
    from kicad_tools.sexp.parser import SExp

    node = SExp.list("symbol", SExp.list("lib_id", _q("Device:R")))
    node.append(SExp.list("uuid", _q(uuid)))
    node.append(SExp.list("property", _q("Reference"), _q(reference)))
    node.append(SExp.list("property", _q("Value"), _q("10k")))
    if entries:
        instances = SExp.list("instances")
        for project, path, ref in entries:
            instances.append(
                SExp.list("project", _q(project), SExp.list("path", _q(path), SExp.list("reference", _q(ref))))
            )
        node.append(instances)
    return node


def _sheet_use(file: str, uuid: str, name: str):
    from kicad_tools.sexp.parser import SExp

    return SExp.list(
        "sheet",
        SExp.list("uuid", _q(uuid)),
        SExp.list("property", _q("Sheetname"), _q(name)),
        SExp.list("property", _q("Sheetfile"), _q(file)),
    )


def _write(path: Path, *nodes, uuid: str | None = None) -> Path:
    from kicad_tools.sexp.parser import SExp

    root = SExp.list("kicad_sch", SExp.list("version", 20250114))
    if uuid is not None:
        root.append(SExp.list("uuid", _q(uuid)))
    root.append(SExp.list("lib_symbols"))
    for node in nodes:
        root.append(node)
    write_layer.save(root, path)
    return path


def _stale(problems: list[str]) -> list[str]:
    return [p for p in problems if "stale instance path" in p]


# --- reproduction: a root without a matching uuid ---------------------------------------------


def test_a_root_whose_uuid_matches_no_instance_path_names_the_reference_and_sheet(tmp_path: Path) -> None:
    """The root's uuid changed (a copied or re-created root sheet): R1's only entry is stale."""
    root = _write(
        tmp_path / f"{PROJECT}.kicad_sch",
        _symbol("R1", "a1", [(PROJECT, f"/{OLD_ROOT_UUID}", "R1")]),
        uuid=ROOT_UUID,
    )

    stale = _stale(gate._annotation_problems(root))

    assert len(stale) == 1, stale
    assert f"R1 uuid a1 in {PROJECT}.kicad_sch" in stale[0]
    assert f"sheet use /{ROOT_UUID}" in stale[0]
    assert f"/{OLD_ROOT_UUID} R1" in stale[0]


def test_a_root_without_any_uuid_makes_every_instance_path_stale(tmp_path: Path) -> None:
    """Ticket 50's F6 fixture shape: no ``(uuid ...)`` on the root, entry path ``/root``."""
    root = _write(tmp_path / f"{PROJECT}.kicad_sch", _symbol("C1", "a1", [(PROJECT, "/root", "C1")]))

    stale = _stale(gate._annotation_problems(root))

    assert len(stale) == 1, stale
    assert f"C1 uuid a1 in {PROJECT}.kicad_sch" in stale[0]
    assert "the root sheet has no uuid" in stale[0]


def test_a_live_root_path_is_not_stale(tmp_path: Path) -> None:
    root = _write(
        tmp_path / f"{PROJECT}.kicad_sch",
        _symbol("R1", "a1", [(PROJECT, f"/{ROOT_UUID}", "R1")]),
        uuid=ROOT_UUID,
    )

    assert gate._annotation_problems(root) == []


# --- the rule: entries exist, none at this sheet use ------------------------------------------


def test_a_reused_sheet_missing_the_entry_of_one_use_names_that_use(tmp_path: Path) -> None:
    _write(
        tmp_path / "ch.kicad_sch",
        _symbol("C1", "c1", [(PROJECT, f"/{ROOT_UUID}/{SHEET_A}", "C1"), (PROJECT, f"/{ROOT_UUID}/{SHEET_B}", "C2")]),
        _symbol("C3", "c3", [(PROJECT, f"/{ROOT_UUID}/{SHEET_A}", "C3")]),
        uuid="44444444-4444-4444-8444-444444444444",
    )
    root = _write(
        tmp_path / f"{PROJECT}.kicad_sch",
        _sheet_use("ch.kicad_sch", SHEET_A, "A"),
        _sheet_use("ch.kicad_sch", SHEET_B, "B"),
        uuid=ROOT_UUID,
    )

    stale = _stale(gate._annotation_problems(root))

    assert len(stale) == 1, stale
    assert "C3 uuid c3 in ch.kicad_sch" in stale[0]
    assert f"sheet use /{ROOT_UUID}/{SHEET_B}" in stale[0]
    assert "C1" not in stale[0]


def test_a_stale_entry_beside_a_live_one_is_not_reported(tmp_path: Path) -> None:
    """What KiCad keeps after a sheet is deleted: the live entry decides the reference."""
    root = _write(
        tmp_path / f"{PROJECT}.kicad_sch",
        _symbol("R1", "a1", [(PROJECT, f"/{ROOT_UUID}", "R1"), (PROJECT, f"/{OLD_ROOT_UUID}", "R9")]),
        uuid=ROOT_UUID,
    )

    assert gate._annotation_problems(root) == []


def test_another_projects_stale_entry_alone_is_reported(tmp_path: Path) -> None:
    """kicad-cli reads another project's entry too (build ticket 68, other-project-stale-path)."""
    root = _write(
        tmp_path / f"{PROJECT}.kicad_sch",
        _symbol("R1", "a1", [("other", f"/{OLD_ROOT_UUID}", "R7")]),
        uuid=ROOT_UUID,
    )

    stale = _stale(gate._annotation_problems(root))

    assert len(stale) == 1 and "R1 uuid a1 in" in stale[0] and "other" in stale[0], stale


def test_another_projects_live_entry_is_not_stale(tmp_path: Path) -> None:
    """A copied project keeps its uuids: the old project's entry still sits at the live path."""
    root = _write(
        tmp_path / f"{PROJECT}.kicad_sch",
        _symbol("R1", "a1", [("old_name", f"/{ROOT_UUID}", "R1")]),
        uuid=ROOT_UUID,
    )

    assert _stale(gate._annotation_problems(root)) == []


def test_a_symbol_without_instance_entries_is_not_stale(tmp_path: Path) -> None:
    """KiCad 6 sheets keep no per-symbol entries; the Reference property decides."""
    root = _write(tmp_path / f"{PROJECT}.kicad_sch", _symbol("R1", "a1"), uuid=ROOT_UUID)

    assert gate._annotation_problems(root) == []


@pytest.mark.parametrize(
    "entry_path",
    [ROOT_UUID, f"/{LETTER_UUID.upper()}", f"/{ROOT_UUID}/", "", "/"],
    ids=["no-leading-slash", "upper-case-uuid", "trailing-slash", "empty", "slash-only"],
)
def test_a_path_that_is_not_exactly_the_use_path_is_reported_stale(tmp_path: Path, entry_path: str) -> None:
    """Fail-closed by design (round-1 review finding 2): the comparison is exact and
    case-sensitive, so any spelling other than the use's own path counts as stale."""
    root = _write(
        tmp_path / f"{PROJECT}.kicad_sch",
        _symbol("R1", "a1", [(PROJECT, entry_path, "R1")]),
        uuid=LETTER_UUID if entry_path == f"/{LETTER_UUID.upper()}" else ROOT_UUID,
    )

    assert len(_stale(gate._annotation_problems(root))) == 1


# --- through the gate ------------------------------------------------------------------------


def test_a_real_sheet_with_one_stale_only_entry_no_longer_passes_the_gate(kicad_demo) -> None:
    """Reproduction on a real KiCad 10 project. ``complex_hierarchy`` uses ``ampli_ht`` twice;
    one of C201's two entries is moved to a sheet use that does not exist (what a deleted
    sheet leaves). The entry still names C201, so kicad-cli's netlist keeps the part and the
    netlist count guard is satisfied: before this ticket the gate said PASS."""
    project = kicad_demo("complex_hierarchy")
    sheet = project / "ampli_ht.kicad_sch"
    doc = write_layer.load(sheet)
    symbol = next(
        s
        for s in doc.find_children("symbol")
        if any(p.get_string(0) == "Reference" and p.get_string(1) == "C201" for p in s.find_children("property"))
    )
    entry = symbol.find("instances").find("project").find_children("path")[0]
    live = entry.get_string(0)
    stale_path = live.rsplit("/", 1)[0] + "/00000000-0000-0000-0000-0000deadbeef"
    write_layer.set_string(entry, 0, stale_path)
    write_layer.save(doc, sheet)
    pcb = project / "complex_hierarchy.kicad_pcb"
    baseline = snapshot_rules(pcb, drc=lambda *a, **k: _EMPTY)

    result = verify_board(pcb, parity="required", baseline=baseline, drc=lambda *a, **k: _EMPTY)

    assert result["verdict"] == "INCOMPLETE"
    stale = _stale(result["incomplete_reasons"])
    assert len(stale) == 1, result["incomplete_reasons"]
    assert re.search(r"C201 uuid [0-9a-f-]+ in ampli_ht\.kicad_sch", stale[0]), stale[0]
    assert f"sheet use {live}" in stale[0]


def test_a_failing_board_with_a_stale_path_stays_fail(tmp_path: Path) -> None:
    """The reason is INCOMPLETE evidence: it never hides a demonstrated failure."""
    from test_verify_board import _clearance_error, _report, _write_board_dir

    board = _write_board_dir(tmp_path)
    _write(board.with_suffix(".kicad_sch"), _symbol("R1", "a1", [(board.stem, "/root", "R1")]))

    result = verify_board(board, parity="required", drc=lambda *a, **k: _report(violations=[_clearance_error()]))

    assert result["verdict"] == "FAIL"
    assert _stale(result["incomplete_reasons"]), result["incomplete_reasons"]


def test_a_stale_check_that_cannot_run_is_a_reason_not_a_pass(tmp_path: Path, monkeypatch) -> None:
    from kicad_mcp import sheet_tree

    root = _write(
        tmp_path / f"{PROJECT}.kicad_sch",
        _symbol("R1", "a1", [(PROJECT, f"/{ROOT_UUID}", "R1")]),
        uuid=ROOT_UUID,
    )

    def broken(_root):
        raise OSError("disk gone")

    monkeypatch.setattr(sheet_tree, "walk", broken)

    problems = gate._annotation_problems(root)

    assert any("stale instance path check could not run" in p and "disk gone" in p for p in problems), problems
