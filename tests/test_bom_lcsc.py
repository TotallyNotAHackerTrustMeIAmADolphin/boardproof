"""check_bom_against_lcsc (GitHub issue 123): the parts of a project checked against LCSC data in one call.

Reproduction of the need: a 0402 part was suggested for a 0603 footprint, and the check was one
lookup_jlcpcb_part_tiers call per part. Fixture: the project of tests/test_check_symbol_fields.py (built only
through the tools and the write layer, R1 root sheet, R10 Connectors sheet, R20 MCU sheet, R32 to R34, with
R30 DNP, R31 off board and #PWR01 power skipped); the LCSC and Footprint properties are then set through
set_properties. Real index entries used (read from the shipped index): C25804 Basic R_0603, C17414 Basic
R_0805, C1525 Basic C_0402, C4172 Preferred R_0603, C7955 Basic SOIC-8, C2040 Extended QFN (no recognised
package). No test makes a network call: stock comes from a stub with the SourcingProvider contract.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from boardproof import bom_lcsc, kicad_cli
from boardproof.sourcing.models import PartResult
from tests import _kicad_faults as _faults
from tests import test_check_symbol_fields as _check
from tests import test_sheet_pins as _base

FIXTURES = Path(__file__).parent / "fixtures"
RES = "Resistor_SMD:R_0603_1608Metric"
CAP0402 = "Capacitor_SMD:C_0402_1005Metric"


@pytest.fixture(scope="module")
def _template(tmp_path_factory) -> Path:
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    tools = _base._tools()
    built = _base.root._get_wrapped_function()(tmp_path_factory.mktemp("template"), tools)
    return _check.project._get_wrapped_function()(built, tools)


@pytest.fixture
def project(_template, tmp_path) -> Path:
    shutil.copytree(_template.parent, tmp_path / "proj")
    return tmp_path / "proj" / _template.name


@pytest.fixture
def tools() -> dict:
    return _base._tools()


def _set(tools: dict, project: Path, ref: str, **fields: str) -> None:
    sheet = {"R10": "connectors.kicad_sch", "R20": "mcu.kicad_sch"}.get(ref)
    path = project.parent / sheet if sheet else project
    done = tools["set_properties"](str(path), [{"reference": ref, "set": fields}], root_sch=str(project))
    assert done["status"] == "ok", done


class _Registry:
    """The SourcingProvider contract (ProviderRegistry.providers and search_all), counting its calls."""

    def __init__(self, answer, providers: int = 1) -> None:
        self.answer, self.calls, self._n = answer, [], providers

    def providers(self) -> list:
        return [object()] * self._n

    def search_all(self, query: str, *, limit: int = 20):
        self.calls.append(query)
        return self.answer(query)


def _part(number: str, stock: int | None) -> PartResult:
    return PartResult(manufacturer="Acme", mpn="M-" + number, provider="stub", lcsc_id=number, stock_qty=stock)


def _lines(result: dict) -> dict[str, dict]:
    return {ln["reference"]: ln for ln in result["lines"]}


def _no_network(monkeypatch) -> None:
    """Any provider registry reached without being passed in would be the real one."""
    import boardproof.sourcing as sourcing

    def boom(*a, **k):
        raise AssertionError("a network search was made")

    monkeypatch.setattr(sourcing.PROVIDER_REGISTRY, "search_all", boom)


# ---------------------------------------------------------------------------- package tokens and fit


@pytest.mark.parametrize("name,token", [
    ("Resistor_SMD:R_0603_1608Metric", "0603"), ("PCM_JLCPCB:C_0402", "0402"), ("Diode_SMD:D_0805_2012Metric", "0805"),
    ("LED_SMD:LED_0603_1608Metric", "0603"), ("Package_SO:SOIC-8_3.9x4.9mm_P1.27mm", "SOIC-8-p1.27"),
    ("PCM_JLCPCB:SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL", "SOIC-8-p1.27"), ("Package_TO_SOT_SMD:SOT-23", "SOT-23-3"),
    ("Package_TO_SOT_SMD:SOT-23-5", "SOT-23-5"), ("PCM_JLCPCB:Q_SOT-23", "SOT-23-3"), ("Diode_SMD:D_SMA", "SMA"),
    ("PCM_JLCPCB:D_DO-214AC(SMA)", "SMA"), ("PCM_JLCPCB:D_SMC(DO-214AB)", "SMC"), ("Diode_SMD:D_SOD-123F", "SOD-123"),
    ("PCM_JLCPCB:D_SOD-123FL", "SOD-123"), ("Package_SO:TSSOP-14_4.4x5mm_P0.65mm", "TSSOP-14-w4.4-p0.65"),
])
def test_package_token_recognises(name, token) -> None:
    assert bom_lcsc.package_token(name) == token


@pytest.mark.parametrize("name", [
    "Crystal_SMD:Crystal_SMD_0603-2Pin_6.0x3.5mm",  # a crystal whose name holds 0603 is not a 0603 chip
    "Package_DFN_QFN:DFN0603", "PCM_JLCPCB:R_0603x4", "Resistor_SMD:R_Array_Convex_4x0603", "Package_TO_SOT_SMD:SOT-23W",
    "PCM_JLCPCB:D_SMAF", "Package_QFP:LQFP-48_7x7mm_P0.5mm", "", None, 5,
])
def test_package_token_declines_what_it_does_not_know(name) -> None:
    assert bom_lcsc.package_token(name) is None


def test_fit_is_match_mismatch_or_unknown_never_a_default_pass() -> None:
    assert bom_lcsc.footprint_fit("PCM_JLCPCB:C_0402", CAP0402)["fit"] == "match"
    bad = bom_lcsc.footprint_fit("PCM_JLCPCB:C_0402", "Capacitor_SMD:C_0603_1608Metric")
    assert bad["fit"] == "mismatch" and bad["part_package"] == "0402" and bad["placed_package"] == "0603"
    assert bom_lcsc.footprint_fit("PCM_JLCPCB:SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL", "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm")["fit"] == "mismatch"
    assert bom_lcsc.footprint_fit(None, CAP0402)["fit"] == "unknown"
    assert bom_lcsc.footprint_fit("PCM_JLCPCB:C_0402", "Connector:Odd")["fit"] == "unknown"
    assert bom_lcsc.footprint_fit("PCM_JLCPCB:QFN-56-1EP_7x7mm_P0.4mm", "Package_DFN_QFN:QFN-56")["fit"] == "unknown"
    assert bom_lcsc.footprint_fit(None, None)["fit"] == "unknown"


# ---------------------------------------------------------------------------- the whole project in one call


def test_the_whole_project_in_one_call_with_stage_and_fit_per_line(project, tools, monkeypatch) -> None:
    _no_network(monkeypatch)
    _set(tools, project, "R10", LCSC="C25804")  # Basic, R_0603 on R_0603: match
    _set(tools, project, "R34", LCSC="C4172")  # Preferred, R_0603 on R_0603
    _set(tools, project, "R20", LCSC="C2040", Footprint="Package_DFN_QFN:QFN-56-1EP_7x7mm_P0.4mm")  # Extended, package not compared
    _set(tools, project, "R32", LCSC="C99999999")  # not in the index
    result = bom_lcsc.check_bom(str(project))
    assert result["ok"] is True and result["written"] is False and result["status"] == "ok", result
    rows = _lines(result)
    assert set(rows) == {"R1", "R10", "R20", "R32", "R33", "R34"} and result["line_count"] == 6 and result["placed_symbols"] == 9
    assert result["skipped_counts"] == {"power": 1, "off_board": 1, "dnp": 1, "no_lib_id": 0}
    assert (rows["R10"]["stage"], rows["R10"]["tier"], rows["R10"]["stage_source"]) == ("Basic", "basic", "local_index")
    assert rows["R10"]["footprint_fit"]["fit"] == "match" and rows["R10"]["severity"] == "ok"
    assert rows["R34"]["stage"] == "Preferred Extended"
    assert rows["R20"]["stage"] == "Extended" and rows["R20"]["footprint_fit"]["fit"] == "unknown"
    assert rows["R32"]["stage"] == "unknown" and rows["R32"]["tier"] is None and "not Extended" in rows["R32"]["stage_reason"]
    assert rows["R32"]["footprint_fit"]["fit"] == "unknown" and rows["R32"]["footprint_fit"]["part_package_source"] is None
    assert result["stage_summary"] == {"Basic": 1, "Preferred Extended": 1, "Extended": 1, "unknown": 3}
    assert result["fit_summary"] == {"match": 2, "mismatch": 0, "unknown": 4}
    assert result["has_errors"] is False and result["error_count"] == 0


def test_a_part_of_one_package_on_a_footprint_of_another_is_an_error_line(project, tools) -> None:
    _set(tools, project, "R34", LCSC="C1525")  # the 0402 capacitor, placed on the 0603 footprint of the fixture
    result = bom_lcsc.check_bom(str(project))
    line = _lines(result)["R34"]
    assert line["footprint_fit"]["fit"] == "mismatch" and line["severity"] == "error"
    [err] = [f for f in line["findings"] if f["severity"] == "error"]
    assert err["code"] == "package_footprint_mismatch" and "0402" in err["message"] and "0603" in err["message"]
    assert result["has_errors"] is True and result["error_count"] == 1 and result["error_references"] == ["R34"]
    _set(tools, project, "R34", Footprint=CAP0402)
    fixed = bom_lcsc.check_bom(str(project))
    assert fixed["has_errors"] is False and _lines(fixed)["R34"]["footprint_fit"]["fit"] == "match"


def test_lcsc_and_footprint_gaps_are_warning_lines_not_errors(project, tools) -> None:
    _set(tools, project, "R10", LCSC="c25804")  # lower case is malformed: names and numbers are exact
    result = bom_lcsc.check_bom(str(project))
    rows = _lines(result)

    def codes(ref: str) -> set[str]:
        return {f["code"] for f in rows[ref]["findings"]}

    assert "lcsc_malformed" in codes("R10") and rows["R10"]["stage"] == "unknown" and rows["R10"]["lcsc"] == "c25804"
    assert {"lcsc_missing", "footprint_missing"} <= codes("R1") and rows["R1"]["severity"] == "warning"
    assert "lcsc_blank" in codes("R33") and "lcsc_missing" in codes("R32")
    variant = next(f for f in rows["R32"]["findings"] if f["code"] == "lcsc_missing")
    assert "Lcsc" in variant["message"]  # the wrong-case property is named
    assert result["error_count"] == 0 and result["warning_count"] >= 5


def test_a_selection_by_references_and_the_ones_not_found(project, tools) -> None:
    result = bom_lcsc.check_bom(str(project), references=["R10", "R20", "R999"])
    assert set(_lines(result)) == {"R10", "R20"} and result["selected_by"] == "references"
    assert result["not_found"] == ["R999"] and result["all_found"] is False
    whole = bom_lcsc.check_bom(str(project))
    assert whole["selected_by"] == "all" and whole["not_found"] == [] and whole["all_found"] is True
    assert bom_lcsc.check_bom(str(project), references=["R10"], root_sch=None)["line_count"] == 1


def test_a_child_sheet_given_reports_the_whole_project(project, tools) -> None:
    whole = bom_lcsc.check_bom(str(project))
    assert bom_lcsc.check_bom(str(project.parent / "mcu.kicad_sch"), root_sch=str(project)) == whole


def test_a_skipped_symbol_named_in_references_is_listed_as_skipped_not_as_a_line(project) -> None:
    result = bom_lcsc.check_bom(str(project), references=["R30", "#PWR01"])
    assert result["lines"] == [] and result["skipped_counts"]["dnp"] == 1 and result["skipped_counts"]["power"] == 1
    assert result["not_found"] == []


def test_a_multi_unit_part_is_one_line_and_disagreeing_units_are_named(project, tools) -> None:
    spec = {"lib_id": "Amplifier_Operational:LM324", "ref": "U9", "value": "LM324", "y": 40.0}
    placed = tools["add_symbols"](str(project), [{**spec, "x": 150.0, "unit": 1}, {**spec, "x": 190.0, "unit": 2}])
    assert placed["status"] == "ok", placed
    one = _lines(bom_lcsc.check_bom(str(project), references=["U9"]))
    assert list(one) == ["U9"] and sorted(one["U9"]["units"]) == ["1", "2"]
    assert not any(f["code"] == "units_disagree" for f in one["U9"]["findings"])
    from boardproof import sheet_editing, write_layer

    tree = write_layer.load(project)
    syms = [x for x in tree.find_children("symbol") if x.find_children("lib_id") and sheet_editing._property(x, "Reference").get_string(1) == "U9"]
    assert len(syms) == 2
    done = tools["set_properties"](str(project), [{"uuid": sheet_editing._uuid(syms[0]), "set": {"LCSC": "C7955"}}])
    assert done["status"] == "ok", done
    tree = write_layer.load(project)  # the tool may copy the field to every unit: set the second unit's copy apart, on the node
    second = [x for x in tree.find_children("symbol") if x.find_children("lib_id") and sheet_editing._property(x, "Reference").get_string(1) == "U9"][1]
    write_layer.set_string(sheet_editing._property(second, "LCSC"), 1, "C1525")
    write_layer.save(tree, project)
    rows = _lines(bom_lcsc.check_bom(str(project), references=["U9"]))
    assert list(rows) == ["U9"]
    assert "units_disagree" in {f["code"] for f in rows["U9"]["findings"]}


# ---------------------------------------------------------------------------- offline behaviour and stock


def test_default_makes_no_network_call_and_says_stock_is_unknown(project, tools, monkeypatch) -> None:
    _no_network(monkeypatch)
    _set(tools, project, "R10", LCSC="C25804")
    result = bom_lcsc.check_bom(str(project))
    stock = _lines(result)["R10"]["stock"]
    assert stock["status"] == "not_queried" and stock["qty"] is None and "no network call" in stock["reason"]
    assert "not queried" in result["data"]["stock"] and "no network call" in result["data"]["stock"]
    assert result["data"]["stage_and_package"].startswith("local index only, no network")
    assert "exposed-pad" in result["data"]["package_rules"] and "SMA coax connector" in result["data"]["package_rules"]
    assert result["data"]["index"]["total_parts"] == 889


def test_the_stock_figure_comes_from_a_provider_only_when_asked(project, tools) -> None:
    _set(tools, project, "R10", LCSC="C25804")
    _set(tools, project, "R34", LCSC="C25804")
    reg = _Registry(lambda q: ([_part(q, 1200)], []))
    result = bom_lcsc.check_bom(str(project), include_stock=True, registry=reg)
    assert reg.calls == ["C25804"]  # one search per distinct LCSC number
    for ref in ("R10", "R34"):
        assert _lines(result)[ref]["stock"] == {"status": "known", "qty": 1200, "source": "stub", "source_kind": "api"}
    assert "network" in result["data"]["stock"] and "not queried" not in result["data"]["stock"]


def test_a_provider_result_for_another_number_is_not_taken_as_the_stock(project, tools) -> None:
    _set(tools, project, "R10", LCSC="C25804")
    reg = _Registry(lambda q: ([_part("C1", 5000)], []))
    stock = _lines(bom_lcsc.check_bom(str(project), references=["R10"], include_stock=True, registry=reg))["R10"]["stock"]
    assert stock["status"] == "unknown" and stock["qty"] is None


def test_zero_stock_and_stock_below_the_parts_of_one_board_are_warnings(project, tools) -> None:
    _set(tools, project, "R10", LCSC="C25804")
    _set(tools, project, "R34", LCSC="C25804")
    _set(tools, project, "R20", LCSC="C17414", Footprint="Resistor_SMD:R_0805_2012Metric")
    answers = {"C25804": 1, "C17414": 0}
    reg = _Registry(lambda q: ([_part(q, answers[q])], []))
    rows = _lines(bom_lcsc.check_bom(str(project), include_stock=True, registry=reg))
    assert {f["code"] for f in rows["R10"]["findings"]} >= {"stock_below_one_board"}
    assert {f["code"] for f in rows["R20"]["findings"]} >= {"out_of_stock"} and rows["R20"]["stock"]["qty"] == 0


def test_every_provider_failing_is_unknown_stock_not_zero_and_the_search_stops(project, tools) -> None:
    for ref, n in (("R10", "C25804"), ("R34", "C4184"), ("R20", "C14663"), ("R32", "C4177"), ("R33", "C4172")):
        _set(tools, project, ref, LCSC=n)
    boom = [{"provider": "stub", "error": "code=500"}]
    reg = _Registry(lambda q: ([], boom))
    result = bom_lcsc.check_bom(str(project), include_stock=True, registry=reg)
    rows = _lines(result)
    assert len(reg.calls) == 3  # stops after three searches in a row where every provider failed
    stocks = [r["stock"] for r in rows.values() if r["lcsc"]]
    assert all(s["qty"] is None and s["status"] in ("unknown", "not_queried") for s in stocks)
    assert sum(s["status"] == "not_queried" for s in stocks) == 2
    assert all(s["provider_errors"] == boom for s in stocks if s["status"] == "unknown")
    assert not any(f["code"] == "out_of_stock" for r in rows.values() for f in r["findings"])
    assert result["ok"] is True and result["has_errors"] is False


def test_a_success_resets_the_failure_stop(project, tools) -> None:
    for ref, n in (("R10", "C25804"), ("R34", "C4184"), ("R20", "C14663"), ("R32", "C4177"), ("R33", "C4172")):
        _set(tools, project, ref, LCSC=n)
    order = sorted(["C25804", "C4184", "C14663", "C4177", "C4172"])
    fail = {order[0], order[1], order[3]}
    reg = _Registry(lambda q: ([], [{"provider": "stub", "error": "x"}]) if q in fail else ([_part(q, 7)], []))
    rows = _lines(bom_lcsc.check_bom(str(project), include_stock=True, registry=reg))
    assert len(reg.calls) == 5 and not any(r["stock"]["status"] == "not_queried" for r in rows.values())


def test_max_stock_lookups_caps_the_searches_and_says_so(project, tools) -> None:
    for ref, n in (("R10", "C25804"), ("R34", "C4184"), ("R20", "C14663")):
        _set(tools, project, ref, LCSC=n)
    reg = _Registry(lambda q: ([_part(q, 9)], []))
    rows = _lines(bom_lcsc.check_bom(str(project), include_stock=True, max_stock_lookups=1, registry=reg))
    assert len(reg.calls) == 1
    statuses = sorted(r["stock"]["status"] for r in rows.values() if r["lcsc"])
    assert statuses == ["known", "not_queried", "not_queried"]
    assert "max_stock_lookups" in next(r["stock"]["reason"] for r in rows.values() if r["stock"]["status"] == "not_queried")


def test_no_registered_provider_is_stated(project, tools) -> None:
    _set(tools, project, "R10", LCSC="C25804")
    stock = _lines(bom_lcsc.check_bom(str(project), references=["R10"], include_stock=True, registry=_Registry(lambda q: ([], []), providers=0)))["R10"]["stock"]
    assert stock["status"] == "unknown" and "no sourcing provider" in stock["reason"]


def test_a_missing_index_makes_every_stage_unknown_and_says_why(project, tools, monkeypatch) -> None:
    from boardproof.sourcing import jlcpcb_basic as jlc

    _set(tools, project, "R10", LCSC="C25804")
    monkeypatch.setattr(jlc, "_INDEX_CACHE", None)
    monkeypatch.setattr(jlc, "VENDORED_INDEX_PATH", Path("does-not-exist.json"))
    result = bom_lcsc.check_bom(str(project), references=["R10"])
    line = result["lines"][0]
    assert result["ok"] is True and line["stage"] == "unknown" and line["stage_reason"] and "index" in line["stage_reason"].lower()
    assert "could not be loaded" in result["data"]["stage_and_package"] and result["data"]["index"] is None


# ---------------------------------------------------------------------------- the board's footprint


def test_pcb_path_checks_the_footprint_placed_on_the_board(project, tools, tmp_path) -> None:
    board = tmp_path / "simple_led.kicad_pcb"
    shutil.copy(FIXTURES / "simple_led.kicad_pcb", board)  # R1 is Resistor_SMD:R_0805_2012Metric on this board
    _set(tools, project, "R1", LCSC="C25804", Footprint=RES)  # a 0603 part; the schematic says 0603, the board has 0805
    from_schematic = _lines(bom_lcsc.check_bom(str(project)))["R1"]
    assert from_schematic["footprint_fit"]["fit"] == "match" and from_schematic["placed_footprint_source"] == "schematic"
    result = bom_lcsc.check_bom(str(project), pcb_path=str(board))
    line = _lines(result)["R1"]
    assert line["footprint_fit"]["fit"] == "mismatch" and line["placed_footprint_source"] == "pcb"
    assert line["placed_footprint"] == "Resistor_SMD:R_0805_2012Metric" and result["error_references"] == ["R1"]
    assert "schematic_pcb_footprint_differs" in {f["code"] for f in line["findings"]}
    assert "R10" in _lines(result) and "not_on_board" in {f["code"] for f in _lines(result)["R10"]["findings"]}


def test_an_unreadable_board_is_a_refusal_not_a_fall_back_to_the_schematic(project, tmp_path) -> None:
    bad = tmp_path / "bad.kicad_pcb"
    shutil.copy(FIXTURES / "simple_led.kicad_pcb", bad)
    _faults.rewrite_bytes(bad, lambda data: data[: len(data) // 2])  # a truncated board
    result = bom_lcsc.check_bom(str(project), pcb_path=str(bad))
    assert result["ok"] is False and result["code"] == "pcb_unreadable"


# ---------------------------------------------------------------------------- refusals and read-only


@pytest.mark.parametrize("kwargs,code", [
    ({"references": []}, "invalid_spec"), ({"references": ["R1", "R1"]}, "invalid_spec"), ({"references": "R1"}, "invalid_spec"),
    ({"include_stock": "yes"}, "invalid_spec"), ({"max_stock_lookups": -1}, "invalid_spec"), ({"max_stock_lookups": 201}, "invalid_spec"),
    ({"max_stock_lookups": True}, "invalid_spec"), ({"max_stock_lookups": 1.5}, "invalid_spec"),
])
def test_bad_arguments_are_refused(project, kwargs, code) -> None:
    result = bom_lcsc.check_bom(str(project), **kwargs)
    assert result["ok"] is False and result["code"] == code, result


def test_a_missing_sheet_is_refused(tmp_path) -> None:
    result = bom_lcsc.check_bom(str(tmp_path / "nope.kicad_sch"))
    assert result["ok"] is False and result["code"] == "file_not_found"


def test_the_check_writes_nothing(project, tools, tmp_path) -> None:
    board = tmp_path / "simple_led.kicad_pcb"
    shutil.copy(FIXTURES / "simple_led.kicad_pcb", board)
    before = {p: p.read_bytes() for p in [*project.parent.glob("*"), board] if p.is_file()}
    bom_lcsc.check_bom(str(project), pcb_path=str(board))
    bom_lcsc.check_bom(str(project), include_stock=True, registry=_Registry(lambda q: ([], [])))
    after = {p: p.read_bytes() for p in [*project.parent.glob("*"), board] if p.is_file()}
    assert before == after


# ---------------------------------------------------------------------------- the registered tool


def test_the_registered_tool_is_the_same_check_and_states_the_offline_behaviour(project, tools, monkeypatch) -> None:
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import sourcing

    _no_network(monkeypatch)
    mcp = MCPServer("t")
    assert "check_bom_against_lcsc" in sourcing.build(mcp)
    fn = next(t.fn for t in mcp._tool_manager.list_tools() if t.name == "check_bom_against_lcsc")
    _set(tools, project, "R10", LCSC="C25804")
    assert fn(str(project)) == bom_lcsc.check_bom(str(project))
    doc = " ".join(fn.__doc__.split())
    for phrase in ("Offline behaviour", "no network", "include_stock=true", "stage unknown (not Extended)", "package_footprint_mismatch",
                   "unknown means no data", "a 0402 part on a 0603 footprint"):
        assert phrase in doc, phrase


# ---------------------------------------------------------------------------- review round 1


def test_a_wide_or_exposed_pad_or_connector_package_is_never_a_match() -> None:
    fit = bom_lcsc.footprint_fit
    assert fit("PCM_JLCPCB:SOIC-8_L5.3-W5.3-P1.27-LS8.0-BL", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")["fit"] == "unknown"  # wide part, narrow footprint
    assert fit("PCM_JLCPCB:SOIC-16_L9.9-W3.9-P1.27-LS6.0-BL", "Package_SO:SOIC-16W_7.5x10.3mm_P1.27mm")["fit"] == "unknown"  # narrow part, wide footprint
    assert fit("PCM_JLCPCB:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL", "Package_SO:SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.29x3mm")["fit"] == "unknown"
    assert fit("PCM_JLCPCB:SOIC-8-1EP_L4.9-W3.9-P1.27", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")["fit"] == "unknown"
    assert fit("PCM_JLCPCB:D_SMA", "Connector_Coaxial:SMA_Amphenol_901-144_Vertical")["fit"] == "unknown"  # an SMA coax connector is not an SMA diode
    assert bom_lcsc.package_token("Connector_Coaxial:SMA_Amphenol_901-144_Vertical") is None
    assert fit("PCM_JLCPCB:D_SMA", "Diode_SMD:D_SMA")["fit"] == "match"
    assert fit("PCM_JLCPCB:D_SMA", "Diode_SMD:D_SMB")["fit"] == "mismatch"


def test_sop_and_soic_are_one_family_so_the_same_land_pattern_is_not_a_mismatch() -> None:
    fit = bom_lcsc.footprint_fit
    assert fit("PCM_JLCPCB:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL", "Package_SO:SOP-8_3.9x4.9mm_P1.27mm")["fit"] == "match"
    assert fit("PCM_JLCPCB:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL", "Package_SO:SOIC-14_3.9x8.7mm_P1.27mm")["fit"] == "mismatch"
    assert fit("PCM_JLCPCB:SOIC-8_L5.0-W4.0-P1.27-LS6.0-BL", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")["fit"] == "match"
    assert fit("PCM_JLCPCB:TSSOP-10_3x3mm_P0.5mm", "Package_SO:TSSOP-10_3x3mm_P0.5mm")["fit"] == "match"
    assert fit("PCM_JLCPCB:TSSOP-16_4.4x5mm_P0.65mm", "Package_SO:TSSOP-16_5.3x5mm_P0.65mm")["fit"] != "match"


def test_a_sheet_used_twice_gives_one_line_per_instance_and_counts_the_uses(project, tools) -> None:
    from tests.test_get_properties import _use_mcu_twice

    _set(tools, project, "R20", LCSC="C25804", Footprint=RES)
    _set(tools, project, "R10", LCSC="C25804")
    _use_mcu_twice(project)
    result = bom_lcsc.check_bom(str(project))
    rows = _lines(result)
    assert {"R20", "R21"} <= set(rows) and rows["R20"]["uuid"] == rows["R21"]["uuid"] and rows["R21"]["lcsc"] == "C25804"
    only = bom_lcsc.check_bom(str(project), references=["R21"])
    assert list(_lines(only)) == ["R21"] and only["all_found"] is True and only["not_found"] == []
    reg = _Registry(lambda q: ([_part(q, 2)], []))
    stocked = _lines(bom_lcsc.check_bom(str(project), include_stock=True, registry=reg))
    assert reg.calls == ["C25804"] and "stock_below_one_board" in {f["code"] for f in stocked["R10"]["findings"]}  # 3 placed (R10, R20, R21), 2 in stock
    assert "R20" in stocked


def test_the_board_footprint_of_a_second_instance_is_checked(project, tools, tmp_path) -> None:
    from tests.test_get_properties import _use_mcu_twice

    board = tmp_path / "b.kicad_pcb"
    shutil.copy(FIXTURES / "simple_led.kicad_pcb", board)  # R1 on the board is Resistor_SMD:R_0805_2012Metric
    _set(tools, project, "R20", LCSC="C25804", Footprint=RES)
    _use_mcu_twice(project)
    tree = _write_layer_load(board)
    rename_board_reference(tree, "R1", "R21")
    _write_layer_save(tree, board)
    line = _lines(bom_lcsc.check_bom(str(project), pcb_path=str(board)))["R21"]
    assert line["footprint_fit"]["fit"] == "mismatch" and line["placed_footprint_source"] == "pcb"


def test_stock_equal_to_the_need_is_not_a_warning(project, tools) -> None:
    _set(tools, project, "R10", LCSC="C25804")
    _set(tools, project, "R34", LCSC="C25804")
    reg = _Registry(lambda q: ([_part(q, 2)], []))  # two placed, two in stock
    rows = _lines(bom_lcsc.check_bom(str(project), include_stock=True, registry=reg))
    assert rows["R10"]["stock"]["qty"] == 2 and not {f["code"] for f in rows["R10"]["findings"]} & {"stock_below_one_board", "out_of_stock"}


def test_a_partial_provider_failure_does_not_count_toward_the_stop_rule(project, tools) -> None:
    for ref, n in (("R10", "C25804"), ("R34", "C4184"), ("R20", "C14663"), ("R32", "C4177"), ("R33", "C4172")):
        _set(tools, project, ref, LCSC=n)
    reg = _Registry(lambda q: ([], [{"provider": "one", "error": "code=500"}]), providers=2)  # the other provider answered with nothing
    rows = _lines(bom_lcsc.check_bom(str(project), include_stock=True, registry=reg))
    assert len(reg.calls) == 5 and not any(r["stock"]["status"] == "not_queried" for r in rows.values())
    assert all("some providers failed" in r["stock"]["reason"] for r in rows.values() if r["lcsc"])


def test_a_duplicate_reference_on_the_board_is_a_warning_and_the_fit_is_not_guessed(project, tools, tmp_path) -> None:
    board = tmp_path / "dup.kicad_pcb"
    shutil.copy(FIXTURES / "simple_led.kicad_pcb", board)
    tree = _write_layer_load(board)
    rename_board_reference(tree, "D1", "R1")  # a second footprint named R1 (the LED, another footprint)
    _write_layer_save(tree, board)
    _set(tools, project, "R1", LCSC="C25804", Footprint=RES)
    line = _lines(bom_lcsc.check_bom(str(project), pcb_path=str(board)))["R1"]
    assert "duplicate_board_reference" in {f["code"] for f in line["findings"]}
    assert line["footprint_fit"]["fit"] == "unknown" and line["severity"] != "error"


def _write_layer_load(path):
    from boardproof import write_layer

    return write_layer.load(path)


def _write_layer_save(tree, path) -> None:
    from boardproof import write_layer

    write_layer.save(tree, path)


def rename_board_reference(tree, old: str, new: str) -> None:
    from boardproof import write_layer

    for fp in tree.find_children("footprint"):
        for child in fp.children:
            if not child.is_atom and child.name == "fp_text" and child.get_string(0) == "reference" and child.get_string(1) == old:
                write_layer.set_string(child, 1, new)
                return
    raise AssertionError(old)


def test_pitch_pin_count_and_universal_footprints_are_not_blurred() -> None:
    fit = bom_lcsc.footprint_fit
    assert fit("PCM_JLCPCB:SOIC-8_L4.9-W3.9-P1.27-LS6.0-BL", "Package_SO:SOIC-8_3.81x9.347mm_P2.54mm")["fit"] == "mismatch"  # a 2.54 mm pitch SOIC-8
    assert fit("PCM_JLCPCB:TSSOP-16_L5.0-W4.4-P0.65-LS6.4-BL", "Package_SO:TSSOP-16_4.4x3.6mm_P0.4mm")["fit"] == "mismatch"
    assert fit("PCM_JLCPCB:TSSOP-16_L5.0-W4.4-P0.65-LS6.4-BL", "Package_SO:TSSOP-16_4.4x5mm_P0.65mm")["fit"] == "match"
    assert fit("PCM_JLCPCB:SOIC-8_L4.9-W3.9-LS6.0-BL", "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm")["fit"] == "unknown"  # no pitch: not decided
    assert fit("PCM_JLCPCB:D_SMC", "Diode_SMD:D_SMB-SMC_Universal_Handsoldering")["fit"] == "unknown"
    assert fit("PCM_JLCPCB:Q_SOT-89-3L", "Package_TO_SOT_SMD:SOT-89-5_Handsoldering")["fit"] == "mismatch"
    assert fit("PCM_JLCPCB:Q_SOT-89-3L", "Package_TO_SOT_SMD:SOT-89-3")["fit"] == "match"


# ---------------------------------------------------------------------------- review round 2


def test_ssop_tssop_and_msop_of_one_pin_count_are_unknown_across_families() -> None:
    fit = bom_lcsc.footprint_fit
    assert fit("PCM_JLCPCB:TSSOP-10_3x3mm_P0.5mm", "Package_SO:MSOP-10_3x3mm_P0.5mm")["fit"] == "unknown"  # ADS1115IDGS, the same land pattern
    assert fit("PCM_JLCPCB:TSSOP-8_3x3mm_P0.65mm", "Package_SO:MSOP-8_3x3mm_P0.65mm")["fit"] == "unknown"
    assert fit("PCM_JLCPCB:SSOP-16_4.4x5mm_P0.65mm", "Package_SO:TSSOP-16_4.4x5mm_P0.65mm")["fit"] == "unknown"
    assert "family" in fit("PCM_JLCPCB:TSSOP-10_3x3mm_P0.5mm", "Package_SO:MSOP-10_3x3mm_P0.5mm")["reason"]
    assert fit("PCM_JLCPCB:TSSOP-10_3x3mm_P0.5mm", "Package_SO:TSSOP-10_3x3mm_P0.5mm")["fit"] == "match"  # one family stays compared
    assert fit("PCM_JLCPCB:TSSOP-10_3x3mm_P0.5mm", "Package_SO:MSOP-12_3x4.039mm_P0.65mm")["fit"] == "mismatch"  # another pin count is a different package


def test_a_width_off_the_standard_values_is_unknown_not_rounded_onto_one() -> None:
    token = bom_lcsc.package_token
    assert token("Package_SO:SSOP-8_2.95x2.8mm_P0.65mm") is None  # 2.95 is not 3.0
    assert token("Package_SO:MSOP-8_3x3mm_P0.65mm") == "MSOP-8-w3-p0.65"
    assert token("Package_SO:TSSOP-16_4.4x5mm_P0.65mm") == "TSSOP-16-w4.4-p0.65"
    assert token("Package_SO:TSSOP-16_4.42x5mm_P0.65mm") == "TSSOP-16-w4.4-p0.65"  # within 0.03 of a standard value
    assert token("Package_SO:TSSOP-16_4.44x5mm_P0.65mm") is None  # 0.04 off is outside the 0.03 tolerance
    assert token("Package_SO:TSSOP-16_4.5x5mm_P0.65mm") is None
    assert bom_lcsc.footprint_fit("PCM_JLCPCB:SSOP-8_3x3mm_P0.65mm", "Package_SO:SSOP-8_2.95x2.8mm_P0.65mm")["fit"] == "unknown"


@pytest.mark.parametrize("width,known", [("3.8", True), ("3.75", False), ("4.1", True), ("4.15", False)])
def test_the_narrow_soic_body_bounds_are_pinned(width, known) -> None:
    token = bom_lcsc.package_token(f"Package_SO:SOIC-8_{width}x4.9mm_P1.27mm")
    assert (token == "SOIC-8-p1.27") is known and (token is None) is (not known)


def test_an_unnumbered_sot_is_three_pins() -> None:
    token = bom_lcsc.package_token
    assert token("PCM_JLCPCB:SOT-223_L6.5-W3.5-P2.30-LS7.0-BR") == "SOT-223-3"
    assert token("PCM_JLCPCB:Q_SOT-89") == "SOT-89-3"
    assert token("PCM_JLCPCB:Q_SOT-323") == "SOT-323-3"
    assert token("Package_TO_SOT_SMD:SOT-223-5") == "SOT-223-5" and token("Package_TO_SOT_SMD:SOT-89-5") == "SOT-89-5"


def test_a_diode_package_name_needs_the_d_prefix() -> None:
    assert bom_lcsc.package_token("Diode_SMD:D_SMA") == "SMA"
    assert bom_lcsc.package_token("Diode_SMD:SMA_Something") is None


SAME_SIZE_NOTE = "does not tell R, C, L, D and LED apart (a resistor part on an LED_0603 footprint is a match)"


def test_fit_does_not_tell_passive_kinds_apart_and_the_result_says_so(project, tools) -> None:
    assert bom_lcsc.footprint_fit("PCM_JLCPCB:R_0603", "LED_SMD:LED_0603_1608Metric")["fit"] == "match"
    _set(tools, project, "R10", LCSC="C25804")
    assert SAME_SIZE_NOTE in bom_lcsc.check_bom(str(project), references=["R10"])["data"]["package_rules"]
