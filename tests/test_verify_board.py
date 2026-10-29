"""Tests for the acceptance gate, ``verify_board`` (src/kicad_mcp/verify_board.py).

Contract: pivot-ladder ticket 3 and
docs/ARCHITECTURE.md decision 14. Tests observe behaviour only through the public
seams agreed for this module: ``verify_board()`` (S1) and the MCP tool (S2).

These run real ``kicad-cli`` against fixtures. Expected numbers come from
independent sources (docs/TESTING.md; the 2026-10-11 baseline measurements), not
from re-deriving them with the code under test.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest
from mcp.client.client import Client

from kicad_mcp import kicad_cli
from kicad_mcp.server import build_server
from kicad_mcp.verify_board import snapshot_rules, verify_board

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_PCB = FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"


def test_unrouted_board_fails_with_complete_per_net_unconnected_list() -> None:
    """The v1 golden board is completely unrouted: 11 unconnected items.

    This is the case ``pcb_run_drc`` reports as "0 violations" (docs/TESTING.md), so
    the gate must instead say FAIL and list every unconnected item per net.
    """
    result = verify_board(GOLDEN_PCB, parity="required")

    assert result["verdict"] == "FAIL"

    unconnected = result["unconnected"]
    assert len(unconnected) == 11
    per_net = Counter(item["net"] for item in unconnected)
    assert per_net["GND"] == 5
    assert per_net["Net-(D1-K)"] == 1


def _report(*, violations=None, unconnected=None) -> dict:
    return {
        "violations": violations or [],
        "unconnected_items": unconnected or [],
        "schematic_parity": [],
        "ignored_checks": [],
    }


def _clearance_error(uuid: str = "u1") -> dict:
    return {
        "type": "clearance",
        "severity": "error",
        "description": "Clearance violation",
        "items": [{"uuid": uuid, "description": "Track on F.Cu", "pos": {"x": 1.0, "y": 2.0}}],
    }


class _FakeDrc:
    """Stands in for the kicad-cli subprocess boundary: one canned report per run."""

    def __init__(self, *reports: dict) -> None:
        self._reports = list(reports)
        self.calls: list[dict] = []

    def __call__(self, board, *, refill_zones, schematic_parity) -> dict:
        self.calls.append({"refill_zones": refill_zones, "schematic_parity": schematic_parity})
        return self._reports[min(len(self.calls), len(self._reports)) - 1]


def test_error_in_only_one_of_three_runs_fails_and_is_flagged_unstable() -> None:
    """Union semantics: any error in any run is FAIL, and the disagreement is shown."""
    fake = _FakeDrc(_report(), _report(violations=[_clearance_error()]), _report())

    result = verify_board(GOLDEN_PCB, parity="required", drc=fake)

    assert len(fake.calls) == 3
    assert all(call["refill_zones"] for call in fake.calls)
    assert result["verdict"] == "FAIL"
    assert result["unstable"] is True
    assert len(result["violations"]) == 1


def _many_unconnected(count: int) -> list[dict]:
    return [
        {
            "type": "unconnected_items",
            "severity": "error",
            "description": "Missing connection",
            "items": [
                {"uuid": f"a{i}", "description": "Pad 1 [N] von R1", "pos": {"x": float(i), "y": 0.0}},
                {"uuid": f"b{i}", "description": "Pad 2 [N] von R2", "pos": {"x": float(i), "y": 1.0}},
            ],
        }
        for i in range(count)
    ]


def test_cap_hit_on_unconnected_items_still_fails_but_flags_the_list_as_truncated() -> None:
    """KiCad silently stops at 499 unconnected items: the list is "at least N".

    The board is demonstrably bad, so the verdict is FAIL (FAIL beats INCOMPLETE),
    but the result must say the list is not complete.
    """
    fake = _FakeDrc(_report(unconnected=_many_unconnected(499)))

    result = verify_board(GOLDEN_PCB, parity="required", drc=fake)

    assert result["verdict"] == "FAIL"
    assert result["list_complete"] is False
    assert any("truncat" in reason for reason in result["incomplete_reasons"])


def test_truncated_warning_list_without_errors_is_incomplete_not_pass() -> None:
    """A board with no errors but a warning list cut at the cap cannot be certified."""
    warnings = [
        {
            "type": "silk_overlap",
            "severity": "warning",
            "description": "Silkscreen overlap",
            "items": [{"uuid": f"s{i}", "description": "Text", "pos": {"x": float(i), "y": 0.0}}],
        }
        for i in range(199)
    ]
    fake = _FakeDrc(_report(violations=warnings))

    result = verify_board(GOLDEN_PCB, parity="required", drc=fake)

    assert result["verdict"] == "INCOMPLETE"
    assert result["list_complete"] is False


def test_missing_schematic_is_incomplete_when_parity_is_required(tmp_path: Path) -> None:
    """kicad-cli silently skips schematic parity and exits 0 when the schematic is absent."""
    board = tmp_path / "bare.kicad_pcb"
    board.write_text((GOLDEN_PCB).read_text(encoding="utf-8"), encoding="utf-8")
    fake = _FakeDrc(_report())

    result = verify_board(board, parity="required", drc=fake)

    assert result["verdict"] == "INCOMPLETE"
    assert any("schematic" in reason for reason in result["incomplete_reasons"])


def test_parity_not_applicable_is_stated_in_the_verdict(tmp_path: Path) -> None:
    board = tmp_path / "bare.kicad_pcb"
    board.write_text((GOLDEN_PCB).read_text(encoding="utf-8"), encoding="utf-8")
    fake = _FakeDrc(_report())

    result = verify_board(
        board, parity="not_applicable", parity_reason="imported board, no schematic", drc=fake
    )

    assert "schematic" not in " ".join(result.get("incomplete_reasons", []))
    assert result["parity"] == {"status": "not_applicable", "reason": "imported board, no schematic"}




def test_clean_runs_with_a_pinned_baseline_pass() -> None:
    baseline = snapshot_rules(GOLDEN_PCB, drc=_FakeDrc(_report()))

    result = verify_board(
        GOLDEN_PCB, parity="required", baseline=baseline, drc=_FakeDrc(_report(), _report(), _report())
    )

    assert result["verdict"] == "PASS"
    assert result["unconnected"] == []
    assert result["violations"] == []
    assert result["incomplete_reasons"] == []


def test_clean_runs_without_a_pinned_baseline_are_incomplete_and_carry_the_snapshot() -> None:
    """A PASS must not mean "clean under whatever rules I found" (decision 2026-10-11)."""
    result = verify_board(GOLDEN_PCB, parity="required", drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("not pinned" in reason for reason in result["incomplete_reasons"])
    assert "rule_severities" in result["baseline_snapshot"]


def test_a_check_silently_added_to_ignored_checks_is_incomplete() -> None:
    baseline = snapshot_rules(GOLDEN_PCB, drc=_FakeDrc(_report()))
    weakened = _report()
    weakened["ignored_checks"] = [{"key": "clearance", "description": "Clearance"}]

    result = verify_board(GOLDEN_PCB, parity="required", baseline=baseline, drc=_FakeDrc(weakened))

    assert result["verdict"] == "INCOMPLETE"
    assert any("rules changed" in reason and "ignored_checks" in reason for reason in result["incomplete_reasons"])


def test_a_dru_severity_ignore_rule_added_after_pinning_is_incomplete(tmp_path: Path) -> None:
    """kicad-cli reports nothing about a `.kicad_dru` ``(severity ignore)`` rule."""
    for source in GOLDEN_PCB.parent.iterdir():
        if source.is_file():
            shutil.copy2(source, tmp_path / source.name)
    board = tmp_path / GOLDEN_PCB.name
    baseline = snapshot_rules(board, drc=_FakeDrc(_report()))

    # Test-local rules file: kicad_tools has no .kicad_dru writer, and this is
    # a throwaway copy under tmp_path, never a repo fixture.
    (tmp_path / "golden_attiny1616_board.kicad_dru").write_text(
        '(version 1)\n(rule "mute" (severity ignore) (constraint silk_clearance (min 0mm)))\n',
        encoding="utf-8",
    )

    result = verify_board(board, parity="required", baseline=baseline, drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("dru_severity_clauses" in reason for reason in result["incomplete_reasons"])






@pytest.mark.asyncio
async def test_verify_board_is_available_at_startup_without_loading_a_toolset() -> None:
    """S2: the gate must be reachable by any agent, so it is not a lazy toolset."""
    async with Client(build_server(), mode="auto") as client:
        names = {t.name for t in (await client.list_tools()).tools}
        assert {"verify_board", "pin_rule_baseline"} <= names

        result = await client.call_tool(
            "verify_board", {"pcb_path": str(GOLDEN_PCB), "parity": "required"}
        )
        payload = json.loads(result.content[0].text)

    assert payload["verdict"] == "FAIL"
    assert len(payload["unconnected"]) == 11


# --- review follow-ups (2026-10-11): ways to a false PASS or a raw exception ---



class _CrashingDrc:
    def __call__(self, board, *, refill_zones, schematic_parity) -> dict:
        raise kicad_cli.KicadCliError("kicad-cli exploded")


def test_unknown_parity_value_is_incomplete_not_a_silent_pass() -> None:
    baseline = snapshot_rules(GOLDEN_PCB, drc=_FakeDrc(_report()))

    result = verify_board(GOLDEN_PCB, parity="none", baseline=baseline, drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("parity" in reason for reason in result["incomplete_reasons"])


def test_parity_not_applicable_needs_a_reason() -> None:
    baseline = snapshot_rules(GOLDEN_PCB, drc=_FakeDrc(_report()))

    result = verify_board(
        GOLDEN_PCB, parity="not_applicable", baseline=baseline, drc=_FakeDrc(_report())
    )

    assert result["verdict"] == "INCOMPLETE"


def test_parity_not_applicable_is_rejected_when_a_schematic_exists() -> None:
    """The golden board has a schematic, so "no schematic" is a false claim."""
    baseline = snapshot_rules(GOLDEN_PCB, drc=_FakeDrc(_report()))

    result = verify_board(
        GOLDEN_PCB,
        parity="not_applicable",
        parity_reason="imported board",
        baseline=baseline,
        drc=_FakeDrc(_report()),
    )

    assert result["verdict"] == "INCOMPLETE"
    assert any("schematic exists" in reason for reason in result["incomplete_reasons"])


def test_kicad_cli_failure_is_incomplete_not_an_exception() -> None:
    result = verify_board(GOLDEN_PCB, parity="required", drc=_CrashingDrc())

    assert result["verdict"] == "INCOMPLETE"
    assert any("kicad-cli" in reason for reason in result["incomplete_reasons"])


def test_unpinned_message_names_the_real_pinning_tool() -> None:
    result = verify_board(GOLDEN_PCB, parity="required", drc=_FakeDrc(_report()))

    assert any("pin_rule_baseline" in reason for reason in result["incomplete_reasons"])


def test_warnings_are_returned_in_full_and_do_not_fail_the_gate() -> None:
    warning = {
        "type": "silk_overlap",
        "severity": "warning",
        "description": "Silkscreen overlap",
        "items": [{"uuid": "w1", "description": "Text", "pos": {"x": 3.0, "y": 4.0}}],
    }
    baseline = snapshot_rules(GOLDEN_PCB, drc=_FakeDrc(_report()))

    result = verify_board(
        GOLDEN_PCB,
        parity="required",
        baseline=baseline,
        drc=_FakeDrc(_report(violations=[warning])),
    )

    assert result["verdict"] == "PASS"
    assert [w["type"] for w in result["warnings"]] == ["silk_overlap"]


def test_unconnected_entries_with_unparseable_nets_stay_distinct_and_are_flagged() -> None:
    def entry(x: float) -> dict:
        return {
            "type": "unconnected_items",
            "severity": "error",
            "description": "Missing connection",
            "items": [{"uuid": "x", "description": "Pad without a net token", "pos": {"x": x, "y": 0.0}}],
        }

    result = verify_board(
        GOLDEN_PCB, parity="required", drc=_FakeDrc(_report(unconnected=[entry(1.0), entry(1.0)]))
    )

    assert result["verdict"] == "FAIL"
    assert len(result["unconnected"]) == 2
    assert any("net" in reason for reason in result["incomplete_reasons"])


def test_a_multiline_dru_severity_clause_is_detected(tmp_path: Path) -> None:
    for source in GOLDEN_PCB.parent.iterdir():
        if source.is_file():
            shutil.copy2(source, tmp_path / source.name)
    board = tmp_path / GOLDEN_PCB.name
    baseline = snapshot_rules(board, drc=_FakeDrc(_report()))
    (tmp_path / "golden_attiny1616_board.kicad_dru").write_text(
        "(version 1)\n"
        '(rule "mute"\n'
        "  (severity\n"
        "    ignore)\n"
        "  (constraint silk_clearance (min 0mm)))\n",
        encoding="utf-8",
    )

    result = verify_board(board, parity="required", baseline=baseline, drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("dru_severity_clauses" in reason for reason in result["incomplete_reasons"])


def test_excluded_error_still_fails() -> None:
    """KiCad lists excluded markers with their underlying severity; they must not hide errors."""
    excluded = _clearance_error()
    excluded["excluded"] = True
    excluded["comment"] = "accepted"

    result = verify_board(GOLDEN_PCB, parity="required", drc=_FakeDrc(_report(violations=[excluded])))

    assert result["verdict"] == "FAIL"


def test_kicad_cli_non_zero_exit_is_an_error_even_when_a_report_file_exists(monkeypatch) -> None:
    def fake_run(args):
        out = Path(args[args.index("-o") + 1])
        out.write_text('{"violations": [], "unconnected_items": []}', encoding="utf-8")
        return subprocess.CompletedProcess(args, 3, "", "board load failed")

    monkeypatch.setattr(kicad_cli, "_run", fake_run)

    with pytest.raises(kicad_cli.KicadCliError):
        kicad_cli.run_drc(GOLDEN_PCB)


# --- round 2 review follow-ups -------------------------------------------------


def _write_board_dir(tmp_path: Path) -> Path:
    for source in GOLDEN_PCB.parent.iterdir():
        if source.is_file():
            shutil.copy2(source, tmp_path / source.name)
    return tmp_path / GOLDEN_PCB.name


def test_an_excluded_error_reported_with_severity_exclusion_still_fails() -> None:
    """KiCad's schema names a third severity string, `exclusion`; an excluded marker
    must not be able to hide an error (no waivers, decision 2)."""
    hidden = _clearance_error()
    hidden["severity"] = "exclusion"
    hidden["excluded"] = True

    result = verify_board(GOLDEN_PCB, parity="required", drc=_FakeDrc(_report(violations=[hidden])))

    assert result["verdict"] == "FAIL"


def test_an_unknown_severity_is_never_silently_dropped() -> None:
    odd = _clearance_error()
    odd["severity"] = "mystery"
    baseline = snapshot_rules(GOLDEN_PCB, drc=_FakeDrc(_report()))

    result = verify_board(
        GOLDEN_PCB, parity="required", baseline=baseline, drc=_FakeDrc(_report(violations=[odd]))
    )

    assert result["verdict"] != "PASS"


def test_a_dru_rule_that_relaxes_a_constraint_without_a_severity_clause_is_incomplete(
    tmp_path: Path,
) -> None:
    board = _write_board_dir(tmp_path)
    baseline = snapshot_rules(board, drc=_FakeDrc(_report()))
    (tmp_path / "golden_attiny1616_board.kicad_dru").write_text(
        '(version 1)\n(rule relax (constraint clearance (min 0mm)))\n', encoding="utf-8"
    )

    result = verify_board(board, parity="required", baseline=baseline, drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("rules changed" in reason for reason in result["incomplete_reasons"])


def test_a_parity_error_fails_the_gate() -> None:
    parity_error = {
        "type": "footprint_symbol_mismatch",
        "severity": "error",
        "description": "Footprint differs from the schematic",
        "items": [{"uuid": "p1", "description": "Footprint R1", "pos": {"x": 1.0, "y": 1.0}}],
    }
    report = _report()
    report["schematic_parity"] = [parity_error]

    result = verify_board(GOLDEN_PCB, parity="required", drc=_FakeDrc(report))

    assert result["verdict"] == "FAIL"


def test_an_unannotated_schematic_is_incomplete_when_parity_is_required(tmp_path: Path) -> None:
    board = _write_board_dir(tmp_path)
    sheet = tmp_path / "golden_attiny1616_board.kicad_sch"
    text = sheet.read_text(encoding="utf-8")
    assert '"Reference" "R1"' in text, "fixture no longer has R1; pick another reference"
    # KiCad reads the instance reference over the property, so unannotate both.
    assert '(reference "R1")' in text, "fixture no longer carries instance data for R1"
    text = text.replace('"Reference" "R1"', '"Reference" "R?"', 1)
    text = text.replace('(reference "R1")', '(reference "R?")', 1)
    sheet.write_text(text, encoding="utf-8")
    baseline = snapshot_rules(board, drc=_FakeDrc(_report()))

    result = verify_board(board, parity="required", baseline=baseline, drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("annotated" in reason for reason in result["incomplete_reasons"])


def test_sub_sheets_are_copied_so_parity_sees_the_whole_design(tmp_path: Path) -> None:
    board = _write_board_dir(tmp_path)
    (tmp_path / "power.kicad_sch").write_text("(kicad_sch (version 20250114))\n", encoding="utf-8")
    seen: list[set[str]] = []

    def recording_drc(copied_board, *, refill_zones, schematic_parity) -> dict:
        seen.append({p.name for p in Path(copied_board).parent.iterdir()})
        return _report()

    verify_board(board, parity="required", drc=recording_drc)

    assert "power.kicad_sch" in seen[0]


def test_errors_seen_before_a_later_run_crashes_are_still_reported_as_fail() -> None:
    class _CrashOnSecondRun:
        def __init__(self) -> None:
            self.calls = 0

        def __call__(self, board, *, refill_zones, schematic_parity) -> dict:
            self.calls += 1
            if self.calls == 2:
                raise kicad_cli.KicadCliError("timed out")
            return _report(violations=[_clearance_error()])

    result = verify_board(GOLDEN_PCB, parity="required", drc=_CrashOnSecondRun())

    assert result["verdict"] == "FAIL"
    assert len(result["violations"]) == 1
    assert any("kicad-cli" in reason for reason in result["incomplete_reasons"])


def test_the_run_count_cannot_be_lowered_by_a_caller() -> None:
    fake = _FakeDrc(_report())

    verify_board(GOLDEN_PCB, parity="required", drc=fake)

    assert len(fake.calls) == 3


def test_two_distinct_violations_at_one_position_are_both_listed() -> None:
    first = _clearance_error("a")
    second = _clearance_error("b")
    second["items"][0]["description"] = "Via on B.Cu"

    result = verify_board(
        GOLDEN_PCB, parity="required", drc=_FakeDrc(_report(violations=[first, second]))
    )

    assert len(result["violations"]) == 2


# --- ticket 14: annotation check reads placed instances, never lib_symbols -------

def _q(text: str):
    from kicad_tools.sexp.parser import SExp

    return SExp.quoted_atom(text)


def _symbol_node(
    lib_id: str,
    reference: str | None,
    *,
    instances=None,
    uuid: str | None = None,
    path: str = "/root",
):
    """A placed symbol. ``reference=None`` omits the Reference property; ``instances``
    is a list of ``(project_name, reference)`` written as (instances (project ...)) at
    instance path ``path``."""
    from kicad_tools.sexp.parser import SExp

    node = SExp.list("symbol", SExp.list("lib_id", _q(lib_id)))
    if uuid is not None:
        node.append(SExp.list("uuid", _q(uuid)))
    if reference is not None:
        node.append(SExp.list("property", _q("Reference"), _q(reference)))
    node.append(SExp.list("property", _q("Value"), _q("x")))
    if instances:
        inst = SExp.list("instances")
        for project, ref in instances:
            inst.append(
                SExp.list(
                    "project",
                    _q(project),
                    SExp.list("path", _q(path), SExp.list("reference", _q(ref))),
                )
            )
        node.append(inst)
    return node


def _sheet_node(file: str | None, *, key: str = "Sheetfile", name: str = "subsheet"):
    from kicad_tools.sexp.parser import SExp

    node = SExp.list(
        "sheet",
        SExp.list("at", 10, 20),
        SExp.list("property", _q("Sheetname"), _q(name)),
    )
    if file is not None:
        node.append(SExp.list("property", _q(key), _q(file)))
    return node


def _build_sheet(
    path: Path, *, placed=(), lib_refs=(), sub_sheets=(), extra=(), tag="kicad_sch"
) -> Path:
    """Write a tiny schematic through kicad_tools' S-expression builder."""
    from kicad_tools.sexp.parser import SExp, serialize_sexp

    lib = SExp.list("lib_symbols")
    for ref in lib_refs:
        sym = SExp.list("symbol", _q("Device:R"))
        sym.append(SExp.list("property", _q("Reference"), _q(ref)))
        lib.append(sym)
    root = SExp.list(tag, SExp.list("version", 20250114), lib)
    for node in extra:
        root.append(node)
    for ref in placed:
        root.append(_symbol_node("Device:R", ref))
    for sheetfile in sub_sheets:
        root.append(
            SExp.list(
                "sheet",
                SExp.list("property", _q("Sheetname"), _q(sheetfile)),
                SExp.list("property", _q("Sheetfile"), _q(sheetfile)),
            )
        )
    path.write_text(serialize_sexp(root), encoding="utf-8")
    return path


def _gate_with_schematic(tmp_path: Path, **sheet_kwargs) -> dict:
    board = _write_board_dir(tmp_path)
    _build_sheet(board.with_suffix(".kicad_sch"), **sheet_kwargs)
    return verify_board(board, parity="required", drc=_FakeDrc(_report()))


def _annotation_reasons(result: dict) -> list[str]:
    return [r for r in result.get("incomplete_reasons", []) if "annot" in r or "schematic" in r]


def test_question_marks_only_in_lib_symbols_are_annotated(tmp_path: Path) -> None:
    result = _gate_with_schematic(tmp_path, placed=["R1", "#PWR01"], lib_refs=["R?", "U?"])

    assert not any("annotated" in r for r in result.get("incomplete_reasons", []))


def test_a_placed_unannotated_symbol_is_incomplete_and_named(tmp_path: Path) -> None:
    result = _gate_with_schematic(tmp_path, placed=["R1", "J1?"], lib_refs=["R?"])

    assert result["verdict"] == "INCOMPLETE"
    assert any("annotated" in r and "J1?" in r for r in result["incomplete_reasons"])


def test_power_symbol_references_are_not_unannotated(tmp_path: Path) -> None:
    result = _gate_with_schematic(tmp_path, placed=["R1", "#PWR?", "#FLG01"])

    assert not any("annotated" in r for r in result.get("incomplete_reasons", []))


def test_an_unannotated_symbol_in_a_sub_sheet_is_found(tmp_path: Path) -> None:
    board = _write_board_dir(tmp_path)
    _build_sheet(board.with_suffix(".kicad_sch"), placed=["R1"], sub_sheets=["sub.kicad_sch"])
    _build_sheet(tmp_path / "sub.kicad_sch", placed=["C?"])

    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("annotated" in r and "C?" in r for r in result["incomplete_reasons"])


def test_a_missing_sub_sheet_file_is_incomplete(tmp_path: Path) -> None:
    result = _gate_with_schematic(tmp_path, placed=["R1"], sub_sheets=["gone.kicad_sch"])

    assert result["verdict"] == "INCOMPLETE"
    assert any("gone.kicad_sch" in r for r in result["incomplete_reasons"])


def test_an_unparseable_schematic_is_incomplete_not_annotated(tmp_path: Path) -> None:
    board = _write_board_dir(tmp_path)
    board.with_suffix(".kicad_sch").write_text("(kicad_sch (version 1", encoding="utf-8")

    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("parse" in r for r in result["incomplete_reasons"])


def test_real_multi_sheet_pic_programmer_has_no_annotation_reason(kicad_demo) -> None:
    project = kicad_demo("pic_programmer")  # the installed demo, copied to tmp_path

    result = verify_board(
        project / "pic_programmer.kicad_pcb", parity="required", drc=_FakeDrc(_report())
    )

    reasons = result.get("incomplete_reasons", [])
    assert not any("annotated" in r or "parse" in r or "sub-sheet" in r for r in reasons), reasons


# --- ticket 14 review follow-ups (F1, F2, F4, F5, F6) ---------------------------


def _reasons(result: dict) -> list[str]:
    return result.get("incomplete_reasons", [])


def _gate(tmp_path: Path, **sheet_kwargs) -> tuple[Path, dict]:
    board = _write_board_dir(tmp_path)
    _build_sheet(board.with_suffix(".kicad_sch"), **sheet_kwargs)
    return board, verify_board(board, parity="required", drc=_FakeDrc(_report()))


def _annotated_reason(result: dict, needle: str = "") -> bool:
    return any("annotated" in r and needle in r for r in _reasons(result))


# F1: a sheet node that yields no filename must not drop its hierarchy silently
@pytest.mark.parametrize(
    "file,key",
    [(None, "Sheetfile"), ("", "Sheetfile"), ("sub.kicad_sch", "File")],
    ids=["no-file-property", "empty-value", "unknown-key"],
)
def test_a_sheet_node_without_a_usable_filename_is_incomplete(tmp_path: Path, file, key) -> None:
    _, result = _gate(tmp_path, placed=["R1"], extra=[_sheet_node(file, key=key)])

    assert result["verdict"] == "INCOMPLETE"
    assert any("subsheet" in r and "file" in r for r in _reasons(result))


def test_kicad6_style_sheet_file_key_is_followed(tmp_path: Path) -> None:
    board = _write_board_dir(tmp_path)
    _build_sheet(
        board.with_suffix(".kicad_sch"),
        placed=["R1"],
        extra=[_sheet_node("sub.kicad_sch", key="Sheet file")],
    )
    _build_sheet(tmp_path / "sub.kicad_sch", placed=["C?"])

    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))

    assert _annotated_reason(result, "C?")


# F2: reference reading branches
def test_an_empty_reference_is_unannotated(tmp_path: Path) -> None:
    _, result = _gate(tmp_path, placed=["R1", ""])

    assert _annotated_reason(result, "no reference")


def test_a_placed_symbol_without_any_reference_is_unannotated(tmp_path: Path) -> None:
    _, result = _gate(tmp_path, extra=[_symbol_node("Device:R", None)])

    assert _annotated_reason(result, "no reference")


def test_an_instance_only_reference_with_a_question_mark_is_unannotated(tmp_path: Path) -> None:
    node = _symbol_node("Device:R", None, instances=[(GOLDEN_PCB.stem, "R?")])
    _, result = _gate(tmp_path, extra=[node])

    assert _annotated_reason(result, "R?")


def test_property_r1_with_instance_r_question_is_unannotated(tmp_path: Path) -> None:
    node = _symbol_node("Device:R", "R1", instances=[(GOLDEN_PCB.stem, "R?")])
    _, result = _gate(tmp_path, extra=[node])

    assert _annotated_reason(result, "R?")


def test_a_wrong_root_tag_is_incomplete(tmp_path: Path) -> None:
    _, result = _gate(tmp_path, placed=["R1"], tag="kicad_pcb")

    assert result["verdict"] == "INCOMPLETE"
    assert any("kicad_sch" in r for r in _reasons(result))


def test_a_sheet_cycle_terminates_and_is_incomplete(tmp_path: Path) -> None:
    board = _write_board_dir(tmp_path)
    _build_sheet(board.with_suffix(".kicad_sch"), placed=["R1"], sub_sheets=["a.kicad_sch"])
    _build_sheet(tmp_path / "a.kicad_sch", placed=["C1"], sub_sheets=["b.kicad_sch"])
    _build_sheet(tmp_path / "b.kicad_sch", placed=["C2"], sub_sheets=["a.kicad_sch"])

    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("cycle" in r for r in _reasons(result))


def test_a_sheet_that_includes_itself_is_incomplete(tmp_path: Path) -> None:
    board = _write_board_dir(tmp_path)
    own = board.with_suffix(".kicad_sch")
    _build_sheet(own, placed=["R1"], sub_sheets=[own.name])

    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))

    assert any("cycle" in r for r in _reasons(result))


def test_the_same_sub_sheet_used_twice_is_not_a_cycle(tmp_path: Path) -> None:
    board = _write_board_dir(tmp_path)
    _build_sheet(
        board.with_suffix(".kicad_sch"), placed=["R1"], sub_sheets=["ch.kicad_sch", "ch.kicad_sch"]
    )
    _build_sheet(tmp_path / "ch.kicad_sch", placed=["C1"])

    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))

    assert not any("cycle" in r or "annotated" in r for r in _reasons(result))


def test_sub_sheet_paths_resolve_relative_to_the_containing_sheet(tmp_path: Path) -> None:
    board = _write_board_dir(tmp_path)
    (tmp_path / "sub").mkdir()
    _build_sheet(board.with_suffix(".kicad_sch"), placed=["R1"], sub_sheets=["sub/a.kicad_sch"])
    _build_sheet(tmp_path / "sub" / "a.kicad_sch", placed=["C1"], sub_sheets=["b.kicad_sch"])
    _build_sheet(tmp_path / "sub" / "b.kicad_sch", placed=["L?"])
    _build_sheet(tmp_path / "b.kicad_sch", placed=["L1"])  # annotated decoy at the project root

    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))

    assert _annotated_reason(result, "L?")


# F4: instance data of THIS project wins; other projects' data is ignored
def test_this_projects_instance_reference_wins_over_a_question_mark_property(
    tmp_path: Path,
) -> None:
    node = _symbol_node("Device:R", "R?", instances=[(GOLDEN_PCB.stem, "R7")])
    _, result = _gate(tmp_path, extra=[node])

    assert not _annotated_reason(result)


def test_instances_of_another_project_are_ignored(tmp_path: Path) -> None:
    node = _symbol_node("Device:R", "R1", instances=[("other_project", "R?")])
    _, result = _gate(tmp_path, extra=[node])

    assert not _annotated_reason(result)


def test_a_question_mark_property_with_only_foreign_instance_data_is_unannotated(
    tmp_path: Path,
) -> None:
    node = _symbol_node("Device:R", "R?", instances=[("other_project", "R3")])
    _, result = _gate(tmp_path, extra=[node])

    assert _annotated_reason(result, "R?")


def test_legacy_kicad6_symbol_instances_supply_the_reference(tmp_path: Path) -> None:
    from kicad_tools.sexp.parser import SExp

    legacy = SExp.list(
        "symbol_instances",
        SExp.list("path", _q("/rootuuid/symuuid-1"), SExp.list("reference", _q("R5"))),
        SExp.list("path", _q("/rootuuid/symuuid-2"), SExp.list("reference", _q("C?"))),
    )
    extra = [
        _symbol_node("Device:R", "R?", uuid="symuuid-1"),
        _symbol_node("Device:C", "C1", uuid="symuuid-2"),
        legacy,
    ]
    _, result = _gate(tmp_path, extra=extra)

    assert _annotated_reason(result, "C?")
    assert not _annotated_reason(result, "R?")


# F5: sheets the graded copy cannot contain
@pytest.mark.parametrize("name", ["../outside.kicad_sch", "sub/../../outside.kicad_sch"])
def test_a_sheet_file_leaving_the_project_is_incomplete(tmp_path: Path, name: str) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    board = _write_board_dir(project)
    _build_sheet(tmp_path / "outside.kicad_sch", placed=["R?"])
    _build_sheet(board.with_suffix(".kicad_sch"), placed=["R1"], sub_sheets=[name])

    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))

    assert result["verdict"] == "INCOMPLETE"
    assert any("outside the project" in r and "outside.kicad_sch" in r for r in _reasons(result))


def test_an_absolute_sheet_file_is_incomplete(tmp_path: Path) -> None:
    other = _build_sheet(tmp_path.parent / f"{tmp_path.name}_abs.kicad_sch", placed=["R1"])
    _, result = _gate(tmp_path, placed=["R1"], sub_sheets=[str(other)])

    assert any("outside the project" in r for r in _reasons(result))


def test_a_missing_sub_sheet_names_its_project_relative_path(tmp_path: Path) -> None:
    _, result = _gate(tmp_path, placed=["R1"], sub_sheets=["sub/gone.kicad_sch"])

    assert any("sub/gone.kicad_sch" in r.replace("\\", "/") for r in _reasons(result))


# F6: the acceptance bullet verified without any external fixture
def test_multi_sheet_project_with_lib_symbol_questions_has_no_annotation_reason(
    tmp_path: Path,
) -> None:
    from kicad_tools.sexp.parser import SExp

    board = _write_board_dir(tmp_path)
    (tmp_path / "power").mkdir()
    # Build ticket 50: the root's uuid makes the instance path "/<uuid>" a live one, and the
    # symbol's lib id has a lib_symbols entry. Without both, KiCad leaves the part out of
    # its netlist (measured: kicad-cli listed R1, U2, L1), which the netlist count guard
    # reports; that case is test_verify_board_netlist_guard's missing-lib-symbol test.
    # Build ticket 95: the uuid is a real one. This fixture had `(uuid "root")` and the path
    # "/root"; kicad-cli reads no such string as an id it matches (measured, the two tests
    # below), so that path was stale, not live, and the guard rightly does not prove it.
    _build_sheet(
        board.with_suffix(".kicad_sch"),
        placed=["R1", "#PWR01"],
        lib_refs=["R?", "U?", "#PWR?"],
        extra=[
            SExp.list("uuid", _q(ROOT_UUID)),
            _symbol_node(
                "Device:R", "C?", instances=[(GOLDEN_PCB.stem, "C1")], path=f"/{ROOT_UUID}"
            ),
        ],
        sub_sheets=["power/reg.kicad_sch"],
    )
    _build_sheet(
        tmp_path / "power" / "reg.kicad_sch",
        placed=["U2", "#FLG01"],
        lib_refs=["U?"],
        sub_sheets=["filter.kicad_sch"],
    )
    _build_sheet(tmp_path / "power" / "filter.kicad_sch", placed=["L1"], lib_refs=["L?"])

    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))

    assert not any(
        "annotated" in r or "sheet" in r or "parsed" in r for r in _reasons(result)
    ), _reasons(result)


# Build ticket 95: a sheet id that is not an id.
ROOT_UUID = "11111111-2222-3333-4444-555555555555"


def _non_id_fixture(tmp_path: Path, root_id: str) -> Path:
    """A one-sheet project whose root has uuid ``root_id`` and whose symbol has two instance
    entries: a stale one (naming C1), then one at path ``/<root_id>`` (naming C2)."""
    from kicad_tools.sexp.parser import SExp

    board = _write_board_dir(tmp_path)
    sym = _symbol_node("Device:R", "R9", instances=[(GOLDEN_PCB.stem, "C1")], path="/" + "9" * 8 + "-8888-4777-8666-555555555555")
    sym.find_children("instances")[0].find_children("project")[0].append(
        SExp.list("path", _q(f"/{root_id}"), SExp.list("reference", _q("C2")), SExp.list("unit", 1))
    )
    _build_sheet(board.with_suffix(".kicad_sch"), lib_refs=["R?"], extra=[SExp.list("uuid", _q(root_id)), sym])
    return board


@pytest.mark.parametrize("root_id", ["root", "ROOT", "zz", "a" * 36 + "z"])
def test_kicad_cli_never_matches_a_sheet_id_that_is_not_an_id(tmp_path: Path, root_id: str) -> None:
    """Measured (ticket 95): `(uuid "root")` with the path `/root` is not live. kicad-cli takes
    the last entry only when its path is the sheet's, here it took the earlier stale one."""
    from kicad_mcp.sheet_tree import netlist_components

    board = _non_id_fixture(tmp_path, root_id)
    assert [c["ref"] for c in netlist_components(board.with_suffix(".kicad_sch"))] == ["C1"]


def test_kicad_cli_matches_a_real_sheet_id_the_same_way(tmp_path: Path) -> None:
    """The control for the test above: with a real uuid the last entry is live (C2)."""
    from kicad_mcp.sheet_tree import netlist_components

    board = _non_id_fixture(tmp_path, ROOT_UUID)
    assert [c["ref"] for c in netlist_components(board.with_suffix(".kicad_sch"))] == ["C2"]


def test_the_gate_does_not_prove_a_sheet_id_that_is_not_an_id(tmp_path: Path) -> None:
    board = _non_id_fixture(tmp_path, "root")
    result = verify_board(board, parity="required", drc=_FakeDrc(_report()))
    assert any("not proven" in r for r in _reasons(result)), _reasons(result)
