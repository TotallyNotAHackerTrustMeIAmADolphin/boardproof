"""The Freerouting adapter hands the managed pair clearances to the router as DSN class_class rules (issue 212).

Issue 207 measured (Freerouting 2.5.0, KiCad 10.0.7): ``(class_class (classes A B) (rule (clearance N)))``
inside ``(network``, N in the DSN's own ``(unit ...)``, is honoured; an unknown class name changes nothing.
The pure text function is tested on literal DSN text; the last test runs the real router.
"""

from __future__ import annotations

import json
import re
from pathlib import Path


from boardproof import board_rules, dru, kicad_cli, write_layer
from boardproof.toolsets import routing as routing_toolset
from tests.test_freerouting_sandbox import needs_freerouting
from tests.test_pcbnew_specctra import needs_kicad_python
from tests._fixture_copy import copy_fixture

FIXTURES = Path(__file__).parent / "fixtures"


def _dsn(unit: str = "um", classes=("kicad_default", "Sig", "Gnd"), extra_network: str = "") -> str:
    body = "".join(f"    (class {c} N{i}\n      (rule\n        (width 250)\n        (clearance 200)\n      )\n    )\n"
                   for i, c in enumerate(classes))
    return (
        '(pcb "x.kicad_pcb"\n  (parser\n    (string_quote ")\n  )\n  (resolution um 10)\n'
        f"  (unit {unit})\n  (structure\n  )\n  (network\n    (net N0 (pins R1-1))\n{body}{extra_network}  )\n"
        "  (wiring\n  )\n)\n"
    )


def _rules(text: str) -> list[tuple[str, str, str]]:
    return re.findall(r"\(class_class\s+\(classes\s+(\S+)\s+(\S+)\)\s+\(rule\s+\(clearance\s+([0-9.]+)\)\s+\)\s+\)", text)


def test_one_rule_per_pair_goes_into_the_network_before_the_wiring():
    pairs = {("Gnd", "Sig"): 0.6, ("Sig", "kicad_default"): 0.5}
    out, report = routing_toolset.add_class_class_rules(_dsn(), pairs)
    assert _rules(out) == [("Gnd", "Sig", "600"), ("Sig", "kicad_default", "500")]
    assert out.index("(class_class") < out.index("(wiring") and out.rindex("(class_class") < out.index("\n  )\n  (wiring")
    assert [r["clearance_mm"] for r in report["applied_pair_rules"]] == [0.6, 0.5]
    assert report["pairs_not_applied"] == []
    # nothing else changed: removing the inserted lines gives the input back
    assert re.sub(r"    \(class_class.*?\n    \)\n", "", out, flags=re.S) == _dsn()


def test_default_is_the_exporters_default_class_in_either_position():
    out, report = routing_toolset.add_class_class_rules(_dsn(), {("Default", "Sig"): 0.5, ("Gnd", "Default"): 0.7})
    assert _rules(out) == [("kicad_default", "Sig", "500"), ("Gnd", "kicad_default", "700")]
    assert [(r["a"], r["b"]) for r in report["applied_pair_rules"]] == [("Default", "Sig"), ("Gnd", "Default")]
    assert report["applied_pair_rules"][0]["dsn_classes"] == ["kicad_default", "Sig"]


def test_the_value_is_scaled_to_the_dsn_unit():
    for unit, expected in (("um", "600"), ("mm", "0.6"), ("mil", "23.622047"), ("cm", "0.06"), ("inch", "0.023622")):
        out, report = routing_toolset.add_class_class_rules(_dsn(unit), {("Gnd", "Sig"): 0.6})
        assert _rules(out) == [("Gnd", "Sig", expected)], unit
        assert report["applied_pair_rules"][0]["dsn_unit"] == unit


def test_an_unknown_unit_or_none_applies_nothing_and_says_why():
    for text in (_dsn("furlong"), _dsn().replace("  (unit um)\n", "")):
        out, report = routing_toolset.add_class_class_rules(text, {("Gnd", "Sig"): 0.6})
        assert out == text and report["applied_pair_rules"] == []
        assert [p["code"] for p in report["pairs_not_applied"]] == ["pair_not_applied"]
        assert "unit" in report["pairs_not_applied"][0]["reason"]


def test_a_class_the_export_does_not_have_is_pair_not_applied_for_that_pair_only():
    out, report = routing_toolset.add_class_class_rules(_dsn(), {("Gnd", "Sig"): 0.6, ("Gnd", "Mains"): 0.9})
    assert _rules(out) == [("Gnd", "Sig", "600")]
    [miss] = report["pairs_not_applied"]
    assert (miss["a"], miss["b"], miss["clearance_mm"], miss["code"]) == ("Gnd", "Mains", 0.9, "pair_not_applied")
    assert "Mains" in miss["reason"]


def test_a_class_name_the_measurement_did_not_cover_is_not_applied():
    odd = '"My Class"'
    text = _dsn(classes=("kicad_default", odd, "Sig"))
    out, report = routing_toolset.add_class_class_rules(text, {("My Class", "Sig"): 0.6, ("Sig", "Sig"): 0.4})
    assert out == text and report["applied_pair_rules"] == []
    assert len(report["pairs_not_applied"]) == 2 and all(p["code"] == "pair_not_applied" for p in report["pairs_not_applied"])


def test_a_class_only_named_inside_a_string_or_a_class_class_does_not_count():
    text = _dsn(classes=("kicad_default", "Sig"), extra_network='    (net "(class Gnd" (pins R1-2))\n')
    out, report = routing_toolset.add_class_class_rules(text, {("Gnd", "Sig"): 0.6})
    assert out == text and len(report["pairs_not_applied"]) == 1
    again, rep2 = routing_toolset.add_class_class_rules(out, {("Gnd", "Sig"): 0.6})
    assert again == out and rep2["applied_pair_rules"] == []


def test_the_string_quote_declaration_does_not_open_a_string():
    # KiCad's DSN declares ``(string_quote ")`` and then quotes names with spaces or parentheses
    quoted = '(net "Net-(D1-K)" (pins R1-1))\n    (net "a (class Gnd" (pins R1-2))'
    text = _dsn().replace("(net N0 (pins R1-1))", quoted)
    out, report = routing_toolset.add_class_class_rules(text, {("Gnd", "Sig"): 0.6})
    assert _rules(out) == [("Gnd", "Sig", "600")] and report["pairs_not_applied"] == []


def test_a_dsn_without_a_network_applies_nothing():
    out, report = routing_toolset.add_class_class_rules("(pcb x (unit um))", {("Gnd", "Sig"): 0.6})
    assert out == "(pcb x (unit um))" and len(report["pairs_not_applied"]) == 1


def test_no_pairs_returns_the_text_untouched():
    text = _dsn()
    assert routing_toolset.add_class_class_rules(text, {}) == (text, {"applied_pair_rules": [], "pairs_not_applied": []})


def test_a_pair_that_already_has_a_class_class_rule_is_left_alone():
    first, _ = routing_toolset.add_class_class_rules(_dsn(), {("Gnd", "Sig"): 0.6})
    out, report = routing_toolset.add_class_class_rules(first, {("Sig", "Gnd"): 0.6})
    assert out == first and report["applied_pair_rules"] == []
    assert "already" in report["pairs_not_applied"][0]["reason"]


# --- the managed block of the board's .kicad_dru ----------------------------------------------------


def _pcb_with_dru(tmp_path: Path, dru_text: str | None) -> Path:
    pcb = tmp_path / "b.kicad_pcb"  # never created: only the rules file beside it is read
    if dru_text is not None:
        write_layer.save_bytes(dru_text.encode("utf-8"), pcb.with_suffix(".kicad_dru"), verify=lambda _path: None)
    return pcb


def test_managed_pairs_are_read_from_the_block_only(tmp_path):
    text = dru.with_pairs('(version 1)\n(rule "mine"\n\t(constraint clearance (min 1mm))\n)\n', {("Gnd", "Sig"): 0.6})
    pairs, problems = routing_toolset.managed_pair_clearances(_pcb_with_dru(tmp_path, text))
    assert pairs == {("Gnd", "Sig"): 0.6} and problems == []


def test_no_file_or_no_block_is_no_pairs_and_a_hand_edited_block_is_a_problem(tmp_path):
    assert routing_toolset.managed_pair_clearances(_pcb_with_dru(tmp_path, None)) == ({}, [])
    assert routing_toolset.managed_pair_clearances(_pcb_with_dru(tmp_path, "(version 1)\n")) == ({}, [])
    edited = dru.with_pairs(None, {("Gnd", "Sig"): 0.6}).replace("0.6mm", "0.6mm 7")
    pairs, problems = routing_toolset.managed_pair_clearances(_pcb_with_dru(tmp_path, edited))
    assert pairs == {} and problems and "changed by hand" in problems[0]


# --- the export: every caller gets the rules, the .kicad_dru stays as it was -----------------------


def _golden_with_pair(tmp_path: Path, pair: float | None = 0.6) -> Path:
    proj = tmp_path / "proj"
    copy_fixture(FIXTURES / "golden_attiny1616_board", proj)
    pcb = proj / "golden_attiny1616_board.kicad_pcb"
    kwargs = {"netclasses": [
        {"name": "Gnd", "track_width": 0.25, "clearance": 0.2, "via_diameter": 1.0, "via_drill": 0.5, "nets": ["GND"]},
        {"name": "Sig", "track_width": 0.25, "clearance": 0.2, "via_diameter": 0.7, "via_drill": 0.35, "nets": ["Net-(U1-PA1)"]},
    ]}
    if pair is not None:
        kwargs["pair_clearances"] = [{"a": "Gnd", "b": "Sig", "clearance": pair}]
    proposal = board_rules.propose_board_rules(pcb, output_dir=tmp_path / "rev", **kwargs)
    assert proposal["status"] == "proposed", proposal
    done = board_rules.confirm_board_rules(pcb, proposal["revision_token"], parity="skip",
                                           parity_reason="test board, no schematic parity wanted")
    assert done["status"] == "applied", done
    return Path(done["output_pcb"])


@needs_kicad_python
def test_the_export_carries_the_class_class_rule_and_leaves_the_rules_file_alone(tmp_path):
    pcb = _golden_with_pair(tmp_path)
    dru_file = pcb.with_suffix(".kicad_dru")
    before = (pcb.read_bytes(), dru_file.read_bytes())
    export = routing_toolset.export_dsn_with_kicad(pcb, tmp_path / "work")
    dsn_text = Path(export["dsn_path"]).read_text(encoding="utf-8")
    assert _rules(dsn_text) == [("Gnd", "Sig", "600")]
    assert export["pair_rules"]["applied_pair_rules"][0]["dsn_classes"] == ["Gnd", "Sig"]
    assert export["pair_rules"]["pairs_not_applied"] == [] and export["pair_rules"]["problems"] == []
    assert (pcb.read_bytes(), dru_file.read_bytes()) == before


@needs_kicad_python
def test_a_board_without_pair_rules_exports_a_dsn_without_class_class(tmp_path):
    pcb = _golden_with_pair(tmp_path, pair=None)
    export = routing_toolset.export_dsn_with_kicad(pcb, tmp_path / "work")
    assert "class_class" not in Path(export["dsn_path"]).read_text(encoding="utf-8")
    assert export["pair_rules"] == {"applied_pair_rules": [], "pairs_not_applied": [], "problems": []}


@needs_kicad_python
def test_a_hand_edited_block_hands_the_router_nothing_and_says_so(tmp_path):
    pcb = _golden_with_pair(tmp_path)
    dru_file = pcb.with_suffix(".kicad_dru")
    edited = dru_file.read_bytes().replace(b"0.6mm", b"0.6mm 7")
    write_layer.save_bytes(edited, dru_file, verify=lambda _path: None)  # a hand edit of the managed block
    export = routing_toolset.export_dsn_with_kicad(pcb, tmp_path / "work")
    assert "class_class" not in Path(export["dsn_path"]).read_text(encoding="utf-8")
    assert export["pair_rules"]["applied_pair_rules"] == [] and export["pair_rules"]["problems"]


def test_the_route_note_says_unrouted_nets_may_follow():
    note = routing_toolset.pair_rules_note({"applied_pair_rules": [{"a": "Gnd", "b": "Sig"}], "pairs_not_applied": [], "problems": []})
    assert "unrouted" in note and "no evidence" in note
    assert routing_toolset.pair_rules_note({"applied_pair_rules": [], "pairs_not_applied": [], "problems": []}) is None


# --- the router honours it (issue 207, case B) ------------------------------------------------------


def _route_and_drc(pcb: Path, work: Path, *, pairs_in_dsn: bool) -> dict:
    export = routing_toolset.export_dsn_with_kicad(pcb, work / "dsn")
    assert export["pair_rules"]["applied_pair_rules"], export["pair_rules"]
    if not pairs_in_dsn:  # the control: the same DSN without the rule, as before this ticket
        dsn = Path(export["dsn_path"])
        dsn.write_text(re.sub(r"    \(class_class.*?\n    \)\n", "", dsn.read_text(encoding="utf-8"), flags=re.S), encoding="utf-8")
    ses = work / "board.ses"
    routing_toolset.run_freerouting(export["dsn_path"], ses, max_passes=6, timeout=240)
    routed = routing_toolset.import_ses_with_kicad(export, ses, work / "routed")
    report = kicad_cli.run_drc(routed["routed_pcb"])
    # kicad-cli words its descriptions in the user's language: the rule's own name and the items' uuids carry the test
    from kicad_tools.sexp import parse_file

    copper = {m.group(1) for c in parse_file(str(routed["routed_pcb"])).children if c.name in ("segment", "via")
              for m in [re.search(r'\(uuid "?([0-9a-f-]{36})', c.to_string())] if m}
    track_pair_errors = [
        v for v in report["violations"]
        if v["type"] == "clearance" and v["severity"] == "error" and "boardproof pair clearance" in v["description"]
        and any(i["uuid"] in copper for i in v["items"])
    ]
    return {"track_pair_errors": track_pair_errors, "unconnected": len(report["unconnected_items"]),
            "violations": json.dumps(report["violations"])[:2000]}


@needs_freerouting
@needs_kicad_python
def test_freerouting_keeps_the_pair_clearance_when_the_rule_is_in_the_dsn_and_not_without_it(tmp_path):
    """Issue 207 case B on the golden board (5 nets, classes Gnd and Sig, pair 0.6 mm): the control
    routes a track closer than 0.6 mm to the other class; with the class_class rule no track or via
    does (the pad-to-pad error of fixed input geometry may remain). The price, unrouted nets, is
    reported by the tool, not asserted here; DRC is the measure, not Freerouting's own report."""
    pcb = _golden_with_pair(tmp_path)
    control = _route_and_drc(pcb, tmp_path / "control", pairs_in_dsn=False)
    applied = _route_and_drc(pcb, tmp_path / "applied", pairs_in_dsn=True)
    assert control["track_pair_errors"], "the control must violate the pair rule, else this test measures nothing: " + control["violations"]
    assert applied["track_pair_errors"] == [], applied["violations"]
