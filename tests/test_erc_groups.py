"""sch_run_erc groups related findings (GitHub issue 127).

Measured with kicad-cli 10.0.7 on a project built through the tools (scenario in `_scenario`):

* a local label alone on a pin, same text on the root and on a sub-sheet: two isolated_pin_label findings,
  different label uuids, same quoted text;
* kicad-cli files the sub-sheet's isolated_pin_label under the root sheet path "/" (its pin findings
  stay under "/Sub/"), so the group's `sheets` lists what the report says, not where the label sits;
* two labels (X2, Y1) on one wire: one multiple_net_names that names both label uuids, plus an
  isolated_pin_label for each (the same uuids again);
* a resistor with both pins open: two pin_not_connected findings with different pin uuids and the
  item text "Symbol R6 Pin 1 [Passive, Line]" (the first word is localised);
* a finding's position and uuid are in the raw JSON only; kicad_tools' report drops them.
"""

from __future__ import annotations

import ast
import copy
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import erc_groups, kicad_cli, sheet_authoring
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset
from boardproof.toolsets import verification

from tests.test_verification_toolset import SCH_FIXTURE


def _finding(ftype, sheet, *items, severity="warning", description=None):
    return {
        "type": ftype,
        "severity": severity,
        "description": description or ftype,
        "items": [{"description": d, "uuid": u, "pos": {"x": 0, "y": 0}} for d, u in items],
        "pos": {"x": 0, "y": 0},
    } | {"_sheet": sheet}


def _raw(*findings):
    sheets: dict[str, list] = {}
    for f in findings:
        f = dict(f)
        sheets.setdefault(f.pop("_sheet"), []).append(f)
    return {"sheets": [{"path": p, "violations": v} for p, v in sheets.items()]}


def _listed(raw):
    return [
        {"type_str": v["type"], "description": v["description"], "sheet": s["path"]}
        for s in raw["sheets"]
        for v in s["violations"]
    ]


def _group(raw):
    return erc_groups.grouping_for(raw, _listed(raw))


LABEL = 'Bezeichner "{}"'
PIN = "Symbol {} Pin {} [Passive, Line]"


def test_same_label_text_groups_across_sheets():
    raw = _raw(
        _finding("isolated_pin_label", "/", (LABEL.format("ONLY"), "u1")),
        _finding("isolated_pin_label", "/", (LABEL.format("OTHER"), "u2")),
        _finding("isolated_pin_label", "/Sub/", (LABEL.format("ONLY"), "u3")),
    )
    g = _group(raw)
    assert [(x["rule"], x["key"], x["violation_indexes"], x["sheets"]) for x in g["groups"]] == [
        ("same_label_text", "isolated_pin_label:ONLY", [0, 2], ["/", "/Sub/"])
    ]
    assert g["ungrouped_indexes"] == [1] and g["ungrouped_count"] == 1 and g["grouped_count"] == 2


def test_same_label_text_does_not_join_different_types():
    raw = _raw(
        _finding("isolated_pin_label", "/", (LABEL.format("A"), "u1")),
        _finding("label_dangling", "/", (LABEL.format("A"), "u2"), severity="error"),
    )
    assert _group(raw)["groups"] == []


def test_same_label_text_only_for_the_measured_types():
    raw = _raw(
        _finding("pin_not_driven", "/", (LABEL.format("A"), "u1")),
        _finding("pin_not_driven", "/", (LABEL.format("A"), "u2")),
    )
    assert _group(raw)["groups"] == []


def test_same_item_links_findings_that_name_one_uuid_and_wins_over_label_text():
    raw = _raw(
        _finding("multiple_net_names", "/", (LABEL.format("X2"), "ux"), (LABEL.format("Y1"), "uy")),
        _finding("isolated_pin_label", "/", (LABEL.format("X2"), "ux")),
        _finding("isolated_pin_label", "/", (LABEL.format("Y1"), "uy")),
        _finding("isolated_pin_label", "/Sub/", (LABEL.format("Y1"), "uz")),
    )
    g = _group(raw)
    assert [(x["rule"], x["violation_indexes"], x["type"]) for x in g["groups"]] == [
        ("same_item", [0, 1, 2], "mixed")
    ]
    assert g["groups"][0]["types"] == ["isolated_pin_label", "multiple_net_names"]
    assert g["groups"][0]["severity"] == "warning"
    assert g["ungrouped_indexes"] == [3]


def test_same_item_ignores_findings_without_a_uuid():
    raw = _raw(
        _finding("wire_dangling", "/", ("Draht", None)),
        _finding("wire_dangling", "/", ("Draht", None)),
    )
    assert _group(raw)["groups"] == []
    raw = _raw(_finding("wire_dangling", "/", ("Draht", "")), _finding("wire_dangling", "/", ("Draht", "")))
    assert _group(raw)["groups"] == []


def test_group_severity_is_the_highest_of_its_members():
    raw = _raw(
        _finding("label_dangling", "/", (LABEL.format("A"), "u1"), severity="warning"),
        _finding("label_dangling", "/Sub/", (LABEL.format("A"), "u2"), severity="error"),
    )
    assert _group(raw)["groups"][0]["severity"] == "error"


def test_same_symbol_pins_groups_one_reference_on_one_sheet():
    raw = _raw(
        _finding("pin_not_connected", "/", (PIN.format("R6", 1), "p1"), severity="error"),
        _finding("pin_not_connected", "/", (PIN.format("R7", 2), "p2"), severity="error"),
        _finding("pin_not_connected", "/", (PIN.format("R6", 2), "p3"), severity="error"),
        _finding("pin_not_connected", "/Sub/", (PIN.format("R6", 1), "p4"), severity="error"),
    )
    g = _group(raw)
    assert [(x["rule"], x["key"], x["violation_indexes"], x["severity"]) for x in g["groups"]] == [
        ("same_symbol_pins", "/:R6", [0, 2], "error")
    ]
    assert g["groups"][0]["note"] == "symbol R6 on sheet /: 2 unconnected pins (1, 2)"
    assert g["ungrouped_indexes"] == [1, 3]


def test_same_symbol_pins_needs_exactly_one_item():
    raw = _raw(
        _finding("pin_not_connected", "/", (PIN.format("R6", 1), "p1"), (PIN.format("R6", 2), "p2"), severity="error"),
        _finding("pin_not_connected", "/", (PIN.format("R6", 3), "p3"), (PIN.format("R6", 4), "p4"), severity="error"),
    )
    assert _group(raw)["groups"] == []


def test_same_symbol_pins_reads_only_the_pin_shape():
    raw = _raw(
        _finding("pin_not_connected", "/", ('Hierarchischer Schaltplanpin "A"', "s1"), severity="error"),
        _finding("pin_not_connected", "/", ('Hierarchischer Schaltplanpin "B"', "s2"), severity="error"),
        _finding("pin_not_driven", "/", (PIN.format("R6", 1), "p1")),
        _finding("pin_not_driven", "/", (PIN.format("R6", 2), "p2")),
    )
    assert _group(raw)["groups"] == []


def test_counts_are_equal_and_every_finding_is_in_exactly_one_place():
    raw = _raw(
        _finding("isolated_pin_label", "/", (LABEL.format("ONLY"), "u1")),
        _finding("isolated_pin_label", "/Sub/", (LABEL.format("ONLY"), "u3")),
        _finding("pin_not_connected", "/", (PIN.format("R6", 1), "p1"), severity="error"),
        _finding("pin_not_connected", "/", (PIN.format("R6", 2), "p3"), severity="error"),
        _finding("pin_not_connected", "/", (PIN.format("R9", 2), "p9"), severity="error"),
        _finding("multiple_net_names", "/", (LABEL.format("X"), "ux"), (LABEL.format("Y"), "uy")),
        _finding("isolated_pin_label", "/", (LABEL.format("X"), "ux")),
    )
    g = _group(raw)
    placed = [i for x in g["groups"] for i in x["violation_indexes"]] + g["ungrouped_indexes"]
    assert sorted(placed) == list(range(7))
    assert sum(x["count"] for x in g["groups"]) + g["ungrouped_count"] == 7
    assert g["grouped_count"] == sum(x["count"] for x in g["groups"])
    assert all(x["count"] == len(x["violation_indexes"]) >= 2 for x in g["groups"])


def test_grouping_is_deterministic_and_does_not_change_its_input():
    raw = _raw(
        _finding("isolated_pin_label", "/", (LABEL.format("ONLY"), "u1")),
        _finding("isolated_pin_label", "/Sub/", (LABEL.format("ONLY"), "u3")),
    )
    before = copy.deepcopy(raw)
    assert _group(raw) == _group(raw) and raw == before


def test_a_report_that_does_not_line_up_gives_no_groups():
    raw = _raw(
        _finding("isolated_pin_label", "/", (LABEL.format("ONLY"), "u1")),
        _finding("isolated_pin_label", "/Sub/", (LABEL.format("ONLY"), "u3")),
    )
    listed = _listed(raw)
    for bad in (listed[:1], [listed[1], listed[0]], [dict(listed[0], description="x"), listed[1]]):
        g = erc_groups.grouping_for(raw, bad)
        assert g["status"] == "unavailable" and g["groups"] == [] and g["reason"]
    assert erc_groups.grouping_for(None, listed)["status"] == "unavailable"
    assert erc_groups.grouping_for({"sheets": [3]}, listed)["status"] == "unavailable"


def test_a_grouping_failure_never_hides_the_flat_list(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("nope")

    monkeypatch.setattr(erc_groups, "group_findings", boom)
    raw = _raw(_finding("label_dangling", "/", (LABEL.format("A"), "u1")))
    out = verification._erc_grouping(raw, _listed(raw))
    assert out["status"] == "unavailable" and "nope" in out["reason"] and out["groups"] == []


def test_groups_come_back_sorted_by_first_index_across_rules():
    raw = _raw(
        _finding("pin_not_connected", "/", (PIN.format("R6", 1), "p1"), severity="error"),
        _finding("isolated_pin_label", "/", (LABEL.format("ONLY"), "u1")),
        _finding("multiple_net_names", "/", (LABEL.format("X"), "ux")),
        _finding("pin_not_connected", "/", (PIN.format("R6", 2), "p2"), severity="error"),
        _finding("isolated_pin_label", "/Sub/", (LABEL.format("ONLY"), "u3")),
        _finding("isolated_pin_label", "/", (LABEL.format("X"), "ux")),
    )
    g = _group(raw)
    assert [x["rule"] for x in g["groups"]] == ["same_symbol_pins", "same_label_text", "same_item"]
    assert [x["violation_indexes"][0] for x in g["groups"]] == [0, 1, 2]


def test_same_symbol_pins_needs_the_bracket_suffix():
    raw = _raw(
        _finding("pin_not_connected", "/", ("Symbol R6 Pin 1 extra", "p1"), severity="error"),
        _finding("pin_not_connected", "/", ("Symbol R6 Pin 2 extra", "p2"), severity="error"),
    )
    assert _group(raw)["groups"] == []


def test_a_label_text_with_a_double_quote_is_not_read_and_not_merged():
    raw = _raw(
        _finding("isolated_pin_label", "/", ('Label "A"B"', "u1")),
        _finding("isolated_pin_label", "/", ('Label "A"C"', "u2")),
    )
    assert _group(raw)["groups"] == []


def test_notes_claim_no_fix_and_name_the_report_path():
    raw = _raw(
        _finding("multiple_net_names", "/", (LABEL.format("X"), "ux")),
        _finding("isolated_pin_label", "/", (LABEL.format("X"), "ux")),
        _finding("isolated_pin_label", "/", (LABEL.format("ONLY"), "u1")),
        _finding("isolated_pin_label", "/", (LABEL.format("ONLY"), "u2")),
    )
    notes = [x["note"] for x in _group(raw)["groups"]]
    assert len(notes) == 2 and not any("fix" in n for n in notes)
    assert "reported under" in notes[1] and "on 1 sheet" not in notes[1]


def test_same_item_key_is_the_smallest_shared_uuid():
    raw = _raw(
        _finding("multiple_net_names", "/", (LABEL.format("X"), "ub"), (LABEL.format("Y"), "ua")),
        _finding("isolated_pin_label", "/", (LABEL.format("X"), "ub")),
        _finding("isolated_pin_label", "/", (LABEL.format("Y"), "ua")),
    )
    assert _group(raw)["groups"][0]["key"] == "ua"


def test_unavailable_counts_every_finding_as_ungrouped():
    raw = _raw(_finding("label_dangling", "/", (LABEL.format("A"), "u1")), _finding("label_dangling", "/", (LABEL.format("B"), "u2")))
    g = erc_groups.grouping_for(raw, _listed(raw)[:1])
    assert g["status"] == "unavailable" and g["ungrouped_count"] == 1 and g["ungrouped_indexes"] == [0]
    assert g["grouped_count"] + g["ungrouped_count"] == 1
    g = erc_groups.grouping_for(None, _listed(raw))
    assert g["ungrouped_count"] == 2 and g["ungrouped_indexes"] == [0, 1]


# ---------------------------------------------------------------------------- one real run


@pytest.fixture
def real_tools():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    verification.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _scenario(tmp_path: Path, tools) -> Path:
    root = Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])
    assert sheet_authoring.create_sheets(str(root), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    sub = root.parent / "sub.kicad_sch"

    def place(sch, ref, x, y):
        out = tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": x, "y": y, "ref": ref, "value": "1k"}])
        assert out["status"] == "ok", out

    def label(sch, specs):
        out = tools["add_labels"](str(sch), specs)
        assert out["status"] == "ok", out

    place(root, "R1", 50.8, 50.8)
    place(root, "R3", 101.6, 50.8)
    place(root, "R6", 152.4, 50.8)  # both pins open
    place(sub, "R4", 50.8, 50.8)
    label(root, [{"kind": "label", "text": "ONLY", "x": 50.8, "y": 46.99}])
    label(sub, [{"kind": "label", "text": "ONLY", "x": 50.8, "y": 46.99}])
    label(root, [{"kind": "label", "text": "X1", "x": 101.6, "y": 46.99}])
    wired = tools["add_wires"](str(root), [{"p1": [101.6, 54.61], "p2": [127, 54.61]}])
    assert wired["status"] == "ok", wired
    label(root, [{"kind": "label", "text": "X2", "x": 127, "y": 54.61}])
    label(root, [{"kind": "label", "text": "Y1", "x": 101.6, "y": 54.61}])
    attached = sheet_authoring.add_sheet_symbols(
        str(root), [{"file": "sub.kicad_sch", "name": "Sub", "x": 101.6, "y": 101.6, "pins": [{"name": "A", "direction": "input"}]}]
    )
    assert attached["status"] == "ok", attached
    return root


def test_real_run_groups_what_kicad_reports_and_keeps_the_list(real_tools, tmp_path):
    root = _scenario(tmp_path, real_tools)
    out = real_tools["sch_run_erc"](str(root))
    assert out["status"] == "complete"
    violations = out["violations"]
    g = out["grouping"]
    assert g["status"] == "ok", g
    assert "groups" not in g and set(g) == {"status", "rules", "grouped_count", "ungrouped_count", "ungrouped_indexes"}
    # counts equal
    assert sum(x["count"] for x in out["groups"]) + g["ungrouped_count"] == len(violations) > 0
    assert g["grouped_count"] + g["ungrouped_count"] == len(violations)
    by_rule = {x["rule"]: x for x in out["groups"]}
    # the peer's case: one label text on two sheets (KiCad 10.0.7 reports both findings under sheet "/")
    only = by_rule["same_label_text"]
    assert only["key"] == "isolated_pin_label:ONLY" and only["sheets"] == ["/"]
    assert [violations[i]["type_str"] for i in only["violation_indexes"]] == ["isolated_pin_label"] * 2
    # one wire with two labels: the shared uuid links the net-name finding and the label findings
    item = by_rule["same_item"]
    assert "multiple_net_names" in item["types"] and "isolated_pin_label" in item["types"]
    # a symbol with both pins open
    pins = by_rule["same_symbol_pins"]
    assert pins["key"] == "/:R6" and pins["count"] == 2
    assert all(violations[i]["type_str"] == "pin_not_connected" for i in pins["violation_indexes"])
    # the ungrouped list is what is left, nothing is dropped
    assert all(0 <= i < len(violations) for i in g["ungrouped_indexes"])


def test_the_flat_fields_are_the_same_as_before(real_tools):
    out = real_tools["sch_run_erc"](str(SCH_FIXTURE))
    from kicad_tools.schema import Schematic

    plain = verification._to_jsonable(Schematic.load(SCH_FIXTURE).run_erc())
    assert {k: v for k, v in out.items() if k not in ("status", "groups", "grouping")} == plain


# ---------------------------------------------------------------------------- guards on the text


def test_the_tool_text_states_every_rule_and_the_guarantees():
    mcp = MCPServer("t")
    verification.build(mcp)
    text = next(t for t in mcp._tool_manager.list_tools() if t.name == "sch_run_erc").description  # type: ignore[attr-defined]
    for word in (*erc_groups.RULES, "violation_indexes", "ungrouped_count", "unchanged", "never dropped", "unavailable"):
        assert word in text, word
    for t in erc_groups.LABEL_TEXT_TYPES:
        assert t in text


def test_the_grouping_module_reads_and_writes_no_file():
    tree = ast.parse(Path(erc_groups.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)
    }
    assert not names & {"open", "write_text", "read_text", "subprocess", "os", "replace"}
