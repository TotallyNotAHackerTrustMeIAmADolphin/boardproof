"""compare_spice_netlist (issue 195): topology of a SPICE netlist against kicad-cli's netlist of the project.

Real pairs: ``tests/fixtures/simple_led.kicad_sch`` (R1, D1, J1) and its kicad-cli netlist, with an LTspice-style
text written for it (the .asc export shape: a comment line, V1 R1 D1, a .model and a .op), plus mutated copies;
and the RC filter schematic of the simulation tests with kicad-cli's own SPICE export. The KiCad side is always
kicad-cli's netlist of a scratch copy of a fixture; nothing is hand-written into a KiCad file.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, spice_compare
from boardproof.toolsets import simulation as simulation_toolset
from boardproof.toolsets import verification
from tests import test_simulation_toolset as _simulation_tests

FIXTURES = Path(__file__).parent / "fixtures"
rc_filter_schematic = _simulation_tests.rc_filter_schematic  # the RC filter fixture of the simulation tests

LTSPICE_LED = """\
* C:\\work\\led.asc
V1 VCC 0 5
R1 VCC N001 330
D1 N001 0 LED
.model LED D(Is=1e-18)
.op
.backanno
.end
"""
MAPPING = {"names": {"V1": "J1"}, "pin_map": {"V1": {"1": "1", "2": "2"}, "D1": {"1": "2", "2": "1"}}}


@pytest.fixture(scope="module")
def led(tmp_path_factory) -> dict:
    folder = tmp_path_factory.mktemp("led")
    for suffix in (".kicad_sch", ".kicad_pro"):
        shutil.copyfile(FIXTURES / f"simple_led{suffix}", folder / f"simple_led{suffix}")
    out = kicad_cli.export_netlist(folder / "simple_led.kicad_sch", folder / "nl.net")
    return {"sch": folder / "simple_led.kicad_sch", "kicad": spice_compare.read_kicad_netlist(out)}


def run(led: dict, text: str, mapping=MAPPING, **kw) -> dict:
    return spice_compare.compare(led["kicad"], spice_compare.parse_spice(text), mapping, **kw)


def part(result: dict, ref: str) -> dict:
    return next(p for p in result["parts"] if p["reference"] == ref)


# --------------------------------------------------------------------------- the real pair


def test_kicad_side_is_read_from_the_real_netlist(led):
    k = led["kicad"]
    assert set(k["parts"]) == {"D1", "J1", "R1"}
    assert k["pin_net"][("R1", "2")] == "/LED_ANODE" and k["pin_net"][("D1", "2")] == "/LED_ANODE"
    assert k["pin_net"][("J1", "2")] == "GND" and k["pin_net"][("D1", "1")] == "GND"


def test_same_circuit_matches_although_every_net_name_differs(led):
    r = run(led, LTSPICE_LED)
    assert r["status"] == "ok" and r["topology_matches"] is True, r
    assert r["parts_compared"] == 3 and r["mismatched_parts"] == []
    assert r["missing_parts"] == r["extra_parts"] == r["uncompared_parts"] == []
    assert {(m["sim_node"], m["kicad_net"]) for m in r["node_map"]} == {("0", "GND"), ("vcc", "VCC"), ("n001", "/LED_ANODE")}
    assert r["ground_net"] == "GND" and r["node_map_ambiguous"] is False
    assert all(p["connections_match"] for p in r["parts"])


def test_result_states_what_it_did_not_compare_and_never_calls_the_circuit_good(led):
    r = run(led, LTSPICE_LED)
    text = json.dumps(r).casefold()
    for word in ("models", "parameters", "source", "directives", "values", "verdict"):
        assert word in " ".join(r["not_compared"]).casefold()
    assert '"pass"' not in text and '"circuit_ok"' not in text and r["values_compared"] is False
    assert "value_differences" not in r


def test_extra_part_in_the_simulation(led):
    r = run(led, LTSPICE_LED.replace(".model", "C1 N001 0 100n\n.model"))
    assert [e["sim_name"] for e in r["extra_parts"]] == ["C1"] and r["topology_matches"] is False
    assert r["mismatched_parts"] == []


def test_missing_part_in_the_simulation(led):
    r = run(led, LTSPICE_LED.replace("D1 N001 0 LED\n", ""))
    assert [m["reference"] for m in r["missing_parts"]] == ["D1"] and r["topology_matches"] is False


def test_swapped_pins_name_the_part(led):
    r = run(led, LTSPICE_LED.replace("R1 VCC N001 330", "R1 N001 VCC 330"))
    assert r["topology_matches"] is False and r["mismatched_parts"] == ["R1"], r
    bad = [p for p in part(r, "R1")["pins"] if not p["matches"]]
    assert {p["problem"] for p in bad} == {"net_differs"} and len(bad) == 2
    assert part(r, "D1")["connections_match"] and part(r, "J1")["connections_match"]


def test_a_simulation_only_helper_is_ignored_when_listed(led):
    text = LTSPICE_LED.replace(".model", "Rdis N001 0 1Meg\n.model")
    plain = run(led, text)
    assert [e["sim_name"] for e in plain["extra_parts"]] == ["Rdis"] and plain["topology_matches"] is False
    ignored = run(led, text, {**MAPPING, "ignore": ["Rdis", "Typo"]})
    assert ignored["topology_matches"] is True and ignored["ignore_unmatched"] == ["Typo"]
    assert {"name": "Rdis", "side": "simulation"} in ignored["ignored"]


def test_a_schematic_part_without_simulation_role_is_missing_unless_ignored(led):
    text = LTSPICE_LED.replace("V1 VCC 0 5\n", "")
    assert [m["reference"] for m in run(led, text, {"pin_map": MAPPING["pin_map"]})["missing_parts"]] == ["J1"]
    assert run(led, text, MAPPING)["mapping_unused"] == ["names.V1", "pin_map.V1"]
    r = run(led, text, {"pin_map": MAPPING["pin_map"], "ignore": ["J1"]})
    assert r["missing_parts"] == [] and r["topology_matches"] is True and {"name": "J1", "side": "schematic"} in r["ignored"]


def test_a_part_whose_pin_order_is_unknown_is_not_guessed(led):
    r = run(led, LTSPICE_LED, {"names": {"V1": "J1"}})
    assert {u["sim_name"] for u in r["uncompared_parts"]} == {"V1", "D1"}
    assert all(u["reason"] == "no_pin_map" for u in r["uncompared_parts"])
    assert r["topology_matches"] is False and r["parts_compared"] == 1


def test_a_wrong_pin_map_is_reported_with_an_ambiguity_note(led):
    wrong = {"names": {"V1": "J1"}, "pin_map": {"V1": {"1": "1", "2": "2"}, "D1": {"1": "1", "2": "2"}}}
    r = run(led, LTSPICE_LED, wrong)
    assert r["topology_matches"] is False and r["node_map_ambiguous"] is True and "node_map_note" in r


def test_two_schematic_nets_joined_in_the_simulation(led):
    r = run(led, LTSPICE_LED.replace("D1 N001 0 LED", "D1 VCC 0 LED"))
    assert r["topology_matches"] is False and r["mismatched_parts"] == ["D1"]


def test_one_schematic_net_split_in_the_simulation(led):
    r = run(led, LTSPICE_LED.replace("D1 N001 0 LED", "D1 N002 0 LED"))
    assert r["topology_matches"] is False and len(r["mismatched_parts"]) == 1


def test_names_are_case_insensitive_and_keep_their_spelling(led):
    r = run(led, "* x\nv1 vcc 0 5\nr1 VCC n001 330\nd1 N001 GND LED\n", {"names": {"v1": "J1"}, "pin_map": {"V1": {"1": "1", "2": "2"}, "d1": {"1": "2", "2": "1"}}})
    assert r["topology_matches"] is True, r
    assert {p["sim_name"] for p in r["parts"]} == {"v1", "r1", "d1"} and {p["reference"] for p in r["parts"]} == {"J1", "R1", "D1"}
    assert ("0", "GND") in {(m["sim_node"], m["kicad_net"]) for m in r["node_map"]}


def test_continuations_comments_subcircuit_bodies_and_the_title_line(led):
    text = "R42 my title line\nV1 VCC 0 5 ; supply\nR1 VCC N001\n+ 330\n.subckt foo a b\nR9 a b 1\n.ends foo\nD1 N001 0 LED\n$ junk\n.end\n"
    r = run(led, text)
    assert r["topology_matches"] is True and r["simulation"]["title_line"] == "R42 my title line"
    assert r["simulation"]["subckt_definitions"] == ["foo"] and r["simulation"]["elements_read"] == 3
    assert r["extra_parts"] == []


def test_a_subcircuit_instance_maps_with_its_pin_map(led):
    text = "* x\nV1 VCC 0 5\nR1 VCC N001 330\nXD1 N001 0 LEDMODEL params: m=1\n"
    r = run(led, text, {"names": {"V1": "J1", "XD1": "D1"}, "pin_map": {"V1": {"1": "1", "2": "2"}, "XD1": {"1": "2", "2": "1"}}})
    assert r["topology_matches"] is True and part(r, "D1")["sim_name"] == "XD1"
    assert [p for p in spice_compare.parse_spice(text)["parts"].values() if p["kind"] == "X"][0]["model"] == "LEDMODEL"


def test_pin_mapped_to_null_is_allowed_but_a_connected_pin_absent_from_the_simulation_blocks(led):
    r = run(led, LTSPICE_LED, {**MAPPING, "pin_map": {**MAPPING["pin_map"], "D1": {"1": "2", "2": None}}})
    assert part(r, "D1")["schematic_pins_not_in_simulation"] == ["1"] and r["schematic_pins_not_in_simulation"] == {"D1": ["1"]}
    assert r["topology_matches"] is False and r["mismatched_parts"] == []


def test_a_three_pin_part_with_a_pin_the_simulation_lacks_does_not_match():
    kicad = {"parts": {"R1": {"value": "1k", "lib_id": ""}, "R2": {"value": "1k", "lib_id": ""}},
             "pin_net": {("R1", "1"): "A", ("R1", "2"): "B", ("R1", "3"): "C", ("R2", "1"): "A", ("R2", "2"): "C"}}
    r = spice_compare.compare(kicad, spice_compare.parse_spice("* x\nR1 n1 n2 1k\nR2 n1 n3 1k\n"), None)
    assert r["topology_matches"] is False and r["schematic_pins_not_in_simulation"] == {"R1": ["3"]}


@pytest.mark.parametrize("line", ["U1 n1 n2 0 foo", "A1 n1 n2 n3 buf", "K1 L1 L2 0.9", "X9 onlyone", "R7 a"])
def test_an_element_line_that_is_not_read_blocks_the_verdict(led, line):
    r = run(led, LTSPICE_LED.replace(".op", line + "\n.op"))
    assert r["topology_matches"] is False
    assert [u["reason"] for u in r["uncompared_parts"]] == ["element_not_read"]


def test_a_pin_missing_from_the_pin_map_blocks_the_verdict(led):
    r = run(led, LTSPICE_LED, {**MAPPING, "pin_map": {**MAPPING["pin_map"], "D1": {"1": "2"}}})
    assert r["topology_matches"] is False and r["uncompared_pins"][0]["sim_pin"] == 2


def test_a_pin_number_the_part_does_not_have(led):
    r = run(led, LTSPICE_LED, {**MAPPING, "pin_map": {**MAPPING["pin_map"], "D1": {"1": "2", "2": "9"}}})
    bad = [p for p in part(r, "D1")["pins"] if not p["matches"]]
    assert bad and bad[0]["problem"] == "kicad_pin_not_in_schematic" and r["topology_matches"] is False


# --------------------------------------------------------------------------- values


def test_values_are_compared_only_when_asked(led):
    text = LTSPICE_LED.replace("R1 VCC N001 330", "R1 VCC N001 3.3k")
    assert run(led, text)["topology_matches"] is True
    r = run(led, text, compare_values=True)
    assert [d["sim_name"] for d in r["value_differences"]] == ["R1"] and r["value_differences"][0]["simulation_number"] == 3300
    assert r["topology_matches"] is True  # a value is not topology
    assert {v["sim_name"] for v in r["values_not_compared"]} == {"V1", "D1"}
    same = run(led, LTSPICE_LED, compare_values=True)
    assert same["value_differences"] == []


def test_an_unreadable_value_is_not_a_difference(led):
    r = run(led, LTSPICE_LED.replace("R1 VCC N001 330", "R1 VCC N001 {Rval}"), compare_values=True)
    assert r["value_differences"] == [] and any("not readable" in v["reason"] for v in r["values_not_compared"])


@pytest.mark.parametrize(
    "text,spice,expected",
    [("1u", True, 1e-6), ("1meg", True, 1e6), ("1MEG", True, 1e6), ("1M", True, 1e-3), ("1F", True, 1e-15), ("1mil", True, 25.4e-6),
     ("2.2k", True, 2200), ("1e3", True, 1000), ("{x}", True, None), ("R=5", True, None), ("", True, None), ("abc", True, None),
     ("100nF", False, 1e-7), ("10µF", False, 1e-5), ("1F", False, 1.0), ("1M", False, None), ("1MΩ", False, 1e6),
     ("4k7", False, 4700), ("4R7", False, 4.7), ("10k", False, 1e4), ("330", False, 330), ("1 k", False, 1000), ("22pF", False, 22e-12)],
)
def test_parse_value(text, spice, expected):
    got = spice_compare.parse_value(text, spice=spice)
    assert got == pytest.approx(expected) if expected is not None else got is None


# --------------------------------------------------------------------------- refusals and inputs


@pytest.mark.parametrize(
    "mapping,code",
    [("x", "invalid_spec"), ({"nmaes": {}}, "invalid_spec"), ({"names": {"V1": 3}}, "invalid_spec"), ({"names": []}, "invalid_spec"),
     ({"ignore": "R9"}, "invalid_spec"), ({"ignore": [""]}, "invalid_spec"), ({"pin_map": []}, "invalid_spec"),
     ({"pin_map": {"D1": {}}}, "invalid_spec"), ({"pin_map": {"D1": {"x": "1"}}}, "invalid_spec"), ({"pin_map": {"D1": {"0": "1"}}}, "invalid_spec"),
     ({"pin_map": {"D1": {"1": True}}}, "invalid_spec"), ({"pin_map": {"D1": {"1": "1", "01": "2"}}}, "invalid_spec"),
     ({"pin_map": {"D1": {"3": "1"}}}, "invalid_spec"), ({"names": {"V1": "J1", "v1": "R1"}}, "invalid_spec"),
     ({"names": {"V1": "R1"}}, "invalid_spec")],
)
def test_bad_mapping_is_refused(led, mapping, code):
    with pytest.raises(spice_compare.SpiceCompareError) as err:
        run(led, LTSPICE_LED, mapping)
    assert err.value.code == code


def test_an_unused_mapping_entry_is_listed_not_refused(led):
    r = run(led, LTSPICE_LED, {**MAPPING, "names": {**MAPPING["names"], "Q9": "R1"}, "pin_map": {**MAPPING["pin_map"], "Q8": {"1": "1"}}})
    assert r["mapping_unused"] == ["names.Q9", "pin_map.Q8"] and r["topology_matches"] is True


def test_compare_values_must_be_a_bool(led):
    with pytest.raises(spice_compare.SpiceCompareError) as err:
        run(led, LTSPICE_LED, compare_values="yes")
    assert err.value.code == "invalid_spec"


def test_a_name_used_twice_is_refused():
    with pytest.raises(spice_compare.SpiceCompareError) as err:
        spice_compare.parse_spice("* x\nR1 a b 1\nr1 b c 2\n")
    assert err.value.code == "duplicate_sim_name"


def test_a_reference_that_several_schematic_parts_match_is_refused():
    kicad = {"parts": {"RX": {"value": "1", "lib_id": ""}, "Rx": {"value": "1", "lib_id": ""}}, "pin_net": {}}
    with pytest.raises(spice_compare.SpiceCompareError) as err:
        spice_compare.compare(kicad, spice_compare.parse_spice("* x\nrX a b 1\n"), None)
    assert err.value.code == "ambiguous_reference"


def test_lines_that_are_not_read_are_listed():
    s = spice_compare.parse_spice("* x\nK1 L1 L2 0.9\nQ1 a b\nU1 a b c\nX1 onlyone\nR1 a b 1\n")
    assert [i["name"] for i in s["skipped"]] == ["K1", "Q1", "U1", "X1"] and list(s["parts"]) == ["r1"]
    assert "coupling" in s["skipped"][0]["reason"]


def test_comments_and_continuations_are_joined_before_reading():
    got = spice_compare._logical_lines("t\nR1 a b 1 ; c\n\nR2 a b 2 $ c\n* note\nR3 a\n+ b 3\n")
    assert [text for _n, text in got] == ["t", "R1 a b 1", "R2 a b 2", "R3 a b 3"]


def test_the_name_hint_puts_a_swap_on_the_part_that_has_it(led):
    # N001 renamed so that the sort order alone would blame V1 and D1 instead of R1
    r = run(led, LTSPICE_LED.replace("R1 VCC N001 330", "R1 W001 VCC 330").replace("D1 N001", "D1 W001"))
    assert r["mismatched_parts"] == ["R1"]


# --------------------------------------------------------------------------- the tool, end to end


def call(args: dict) -> dict:
    server = MCPServer("t")
    verification.build(server)
    result = asyncio.run(server.call_tool("compare_spice_netlist", args))
    return json.loads(result.content[0].text)


def snapshot(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_tool_end_to_end_is_read_only(led, tmp_path):
    folder = led["sch"].parent
    net = tmp_path / "led.net"
    net.write_text(LTSPICE_LED, encoding="utf-8")
    before = snapshot(folder)
    r = call({"sch_path": str(led["sch"]), "spice_netlist": str(net), "mapping": MAPPING})
    assert r["status"] == "ok" and r["topology_matches"] is True, r
    mutated = tmp_path / "led2.net"
    mutated.write_text(LTSPICE_LED.replace("R1 VCC N001 330", "R1 N001 VCC 330"), encoding="utf-16")
    r2 = call({"sch_path": str(led["sch"]), "spice_netlist": str(mutated), "mapping": MAPPING})
    assert r2["topology_matches"] is False and r2["mismatched_parts"] == ["R1"]
    assert snapshot(folder) == before


def test_tool_refusals(led, tmp_path):
    net = tmp_path / "led.net"
    net.write_text(LTSPICE_LED, encoding="utf-8")
    sch = str(led["sch"])
    assert call({"sch_path": sch + "x", "spice_netlist": str(net)})["code"] == "file_not_found"
    assert call({"sch_path": sch, "spice_netlist": str(tmp_path / "none.net")})["code"] == "file_not_found"
    asc = tmp_path / "led.asc"
    asc.write_text("Version 4\n", encoding="utf-8")
    r = call({"sch_path": sch, "spice_netlist": str(asc)})
    assert r["code"] == "unsupported_input" and r["ok"] is False
    bad = tmp_path / "bad.net"
    bad.write_bytes(b"\xff\xfe\xfa\x00\x00")
    assert call({"sch_path": sch, "spice_netlist": str(bad)})["code"] == "invalid_netlist"
    assert call({"sch_path": sch, "spice_netlist": str(net), "mapping": {"zz": 1}})["code"] == "invalid_spec"


def test_tool_is_registered_read_only_with_honest_text():
    server = MCPServer("t")
    assert "compare_spice_netlist" in verification.build(server)
    tool = next(t for t in asyncio.run(server.list_tools()) if t.name == "compare_spice_netlist")
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    text = tool.description
    for pin in ("topology only", "never a verdict", "not compared", "pin_map", "ignore", "uncompared_parts", ".asc", "drop their pins from net membership", "after .end", ".include", "neighbouring part", "swapped R, C or L shows as a mismatch", "element_not_read", "schematic_pins_not_in_simulation"):
        assert pin in text, pin


def test_real_rc_pair_with_kicad_clis_own_spice_export(rc_filter_schematic, tmp_path):
    export = tmp_path / "rc.cir"
    assert simulation_toolset.export_spice_netlist(rc_filter_schematic, export)["status"] == "ok"
    first = call({"sch_path": str(rc_filter_schematic), "root_sch": str(rc_filter_schematic), "spice_netlist": str(export)})
    assert first["status"] == "ok" and [u["sim_name"] for u in first["uncompared_parts"]] == ["V1"]
    assert first["topology_matches"] is False and first["parts_compared"] == 2
    pins = {"pin_map": {"V1": {"1": "1", "2": "2"}}}
    ok = call({"sch_path": str(rc_filter_schematic), "root_sch": str(rc_filter_schematic), "spice_netlist": str(export), "mapping": pins, "compare_values": True})
    assert ok["topology_matches"] is True, ok
    assert {m["kicad_net"] for m in ok["node_map"]} >= {"GND"} and ok["value_differences"] == []
    # kicad-cli's own export names nodes Net-_R1-Pad1_; an LTspice-style rename and a rewiring are told apart
    ltspice = "* rc\nV1 N001 0 DC 1 AC 1\nR1 N001 N002 1k\nC1 N002 0 100n\n"
    renamed = tmp_path / "rc.net"
    renamed.write_text(ltspice, encoding="utf-8")
    assert call({"sch_path": str(rc_filter_schematic), "root_sch": str(rc_filter_schematic), "spice_netlist": str(renamed), "mapping": pins})["topology_matches"] is True
    moved = tmp_path / "rc2.net"
    moved.write_text(ltspice.replace("C1 N002 0 100n", "C1 N001 0 100n"), encoding="utf-8")
    bad = call({"sch_path": str(rc_filter_schematic), "root_sch": str(rc_filter_schematic), "spice_netlist": str(moved), "mapping": pins})
    assert bad["topology_matches"] is False and bad["mismatched_parts"]
    wrong_value = tmp_path / "rc3.net"
    wrong_value.write_text(ltspice.replace("1k", "10k"), encoding="utf-8")
    diff = call({"sch_path": str(rc_filter_schematic), "root_sch": str(rc_filter_schematic), "spice_netlist": str(wrong_value), "mapping": pins, "compare_values": True})
    assert diff["topology_matches"] is True and [d["reference"] for d in diff["value_differences"]] == ["R1"]


def test_simulation_skill_names_the_tool_and_its_limits():
    text = (Path(__file__).parent.parent / ".claude" / "skills" / "kicad-simulation" / "SKILL.md").read_text(encoding="utf-8")
    for pin in ("compare_spice_netlist", "topology only", "uncompared_parts", "never guessed", "drop their pins", "an `.asc` is not read", "mapping.pin_map"):
        assert pin in text, pin


def test_a_broken_sheet_tree_and_a_missing_root_are_refused(tmp_path):
    from boardproof import sheet_authoring

    for suffix in (".kicad_sch", ".kicad_pro"):
        shutil.copyfile(FIXTURES / f"simple_led{suffix}", tmp_path / f"simple_led{suffix}")
    sch = tmp_path / "simple_led.kicad_sch"
    assert sheet_authoring.create_sheets(str(sch), [{"file": "sub.kicad_sch"}])["status"] == "ok"
    spec = {"file": "sub.kicad_sch", "name": "Sub", "x": 200.66, "y": 120.65, "pins": [{"name": "SER", "direction": "input"}]}
    assert sheet_authoring.add_sheet_symbols(str(sch), [spec])["status"] == "ok"
    net = tmp_path / "x.net"
    net.write_text(LTSPICE_LED, encoding="utf-8")
    (tmp_path / "sub.kicad_sch").unlink()
    r = call({"sch_path": str(sch), "spice_netlist": str(net), "mapping": MAPPING})
    assert r["ok"] is False and r["code"] == "sheet_tree_incomplete", r
    r = call({"sch_path": str(sch), "spice_netlist": str(net), "root_sch": str(tmp_path / "nope.kicad_sch")})
    assert r["code"] == "root_not_found"


def test_a_nul_character_is_not_a_text_netlist():
    with pytest.raises(spice_compare.SpiceCompareError) as err:
        spice_compare.parse_spice("* x\nR1 a b 1\x00\n")
    assert err.value.code == "invalid_netlist"


def test_a_pin_number_the_part_does_not_have_never_matches(led):
    pins = {**MAPPING["pin_map"], "D1": {"1": "2", "2": "99"}}
    r = run(led, LTSPICE_LED.replace("D1 N001 0 LED", "D1 N001 NX LED"), {**MAPPING, "pin_map": pins})
    pin = part(r, "D1")["pins"][1]
    assert pin["matches"] is False and pin["problem"] == "kicad_pin_not_in_schematic" and r["topology_matches"] is False


def test_a_swapped_passive_gets_a_hint_a_pin_map_does_not(led):
    r = run(led, LTSPICE_LED.replace("R1 VCC N001 330", "R1 N001 VCC 330"))
    assert any("pin_map" in p.get("hint", "") for p in part(r, "R1")["pins"])
    fixed = run(led, LTSPICE_LED.replace("R1 VCC N001 330", "R1 N001 VCC 330"), {**MAPPING, "pin_map": {**MAPPING["pin_map"], "R1": {"1": "2", "2": "1"}}})
    assert fixed["topology_matches"] is True


def test_nets_that_kicad_names_unconnected_are_not_schematic_only_pins(led):
    kicad = {"parts": {"R1": {"value": "1", "lib_id": ""}}, "pin_net": {("R1", "1"): "A", ("R1", "2"): "C", ("R1", "3"): "unconnected-(R1-Pad3)", ("R1", "4"): "B"}}
    r = spice_compare.compare(kicad, spice_compare.parse_spice("* x\nR1 a b 1\n"), None)
    assert part(r, "R1")["schematic_pins_not_in_simulation"] == ["4"]


def test_a_hint_only_on_default_pins_and_never_with_a_pin_map(led):
    swapped = LTSPICE_LED.replace("R1 VCC N001 330", "R1 N001 VCC 330")
    assert any("hint" in p for p in part(run(led, swapped), "R1")["pins"])
    wrong = {**MAPPING, "pin_map": {**MAPPING["pin_map"], "R1": {"1": "1", "2": "2"}}}
    r = run(led, swapped, wrong)
    assert not r["topology_matches"] and not any("hint" in p for x in r["parts"] for p in x["pins"])
    hints = [p["hint"] for x in run(led, swapped)["parts"] for p in x["pins"] if "hint" in p]
    assert hints and all("neighbouring" in h for h in hints)


@pytest.mark.parametrize(
    "text",
    ["* t\nR1 VCC N001 330\n.subckt foo a b\nR3 N1 0 1\nU1 a b c\n", "* t\nR1 VCC N001 330\n.subckt a x\n.subckt b y\n.ends\nU1 a b c\n", "* t\nR1 VCC N001 330\n.ends\n"],
)
def test_unbalanced_subckt_is_refused(text):
    with pytest.raises(spice_compare.SpiceCompareError) as err:
        spice_compare.parse_spice(text)
    assert err.value.code == "invalid_netlist"


def test_nested_balanced_subckt_is_read():
    s = spice_compare.parse_spice("* t\n.subckt a x\n.subckt b y\nR5 y 0 1\n.ends\n.ends\nR1 p q 1\n")
    assert list(s["parts"]) == ["r1"]


def test_nothing_compared_is_never_a_match(led):
    r = run(led, "* empty\n.end\n", {"ignore": ["J1", "R1", "D1"]})
    assert r["parts_compared"] == 0 and r["topology_matches"] is False and "nothing_compared" in r
    both = spice_compare.compare({"parts": {}, "pin_net": {}}, spice_compare.parse_spice("* x\n"), None)
    assert both["topology_matches"] is False


def test_a_first_line_that_looks_like_an_element_is_named_as_the_title(led):
    r = run(led, "R1 VCC N001 330\nV1 VCC 0 5\nD1 N001 0 LED\n")
    assert "title_note" in r and r["simulation"]["title_line"].startswith("R1 ")
    assert [m["reference"] for m in r["missing_parts"]] == ["R1"] or r["topology_matches"] is False
    assert "title_note" not in run(led, LTSPICE_LED)


def test_a_stray_ends_before_a_balanced_pair_is_refused():
    with pytest.raises(spice_compare.SpiceCompareError) as err:
        spice_compare.parse_spice("* t\n.ends\n.subckt a x\n.ends\nR1 p q 1\n")
    assert err.value.code == "invalid_netlist"


def test_elements_after_dot_end_are_listed_not_compared(led):
    text = LTSPICE_LED.replace(".end\n", "") + ".end\nC9 N001 0 1u\n.tran 1\n"
    r = run(led, text)
    assert r["topology_matches"] is False and r["extra_parts"] == []
    assert [(u["sim_name"], u["reason"]) for u in r["uncompared_parts"]] == [("C9", "content_after_end")]
    # an element that would complete the circuit does not count when it stands after .end
    missing_d1 = LTSPICE_LED.replace("D1 N001 0 LED\n", "").replace(".end\n", "") + ".end\nD1 N001 0 LED\n"
    r2 = run(led, missing_d1)
    assert [m["reference"] for m in r2["missing_parts"]] == ["D1"] and r2["topology_matches"] is False
    assert "after .end" in r["topology_note"] and r["simulation"]["elements_read"] == 3


def test_dot_end_inside_a_subcircuit_does_not_stop_reading():
    s = spice_compare.parse_spice("* t\n.subckt a x\n.end\n.ends\nR1 p q 1\n")
    assert list(s["parts"]) == ["r1"] and s["skipped"] == []


def test_include_contents_are_named_as_not_compared(led):
    assert any(".include" in n and ".lib" in n for n in run(led, LTSPICE_LED)["not_compared"])


def test_a_title_less_netlist_starting_with_an_element_gets_the_title_note(led):
    for first in ("Rload out 0 1k", "R1 VCC N001 330"):
        assert "title_note" in run(led, first + "\nV1 VCC 0 5\n")
    assert "title_note" not in run(led, LTSPICE_LED)
    assert "title_note" not in run(led, "RC\nV1 VCC 0 5\n")


def test_title_note_needs_three_tokens_and_says_how_to_ignore_it(led):
    assert "title_note" not in run(led, "Rload out\nV1 VCC 0 5\n")
    note = run(led, "Rload out 0\nV1 VCC 0 5\n")["title_note"]
    assert "ignore this note" in note
    assert "title_note" in run(led, "Low pass filter\nV1 VCC 0 5\n")  # a plain title that starts with an element letter: a note, nothing suppressed


def test_a_continuation_after_dot_end_is_listed(led):
    text = LTSPICE_LED.replace(".end\n", "") + ".end\n+ R2 a b 1\n"
    r = run(led, text)
    assert [u["reason"] for u in r["uncompared_parts"]] == ["content_after_end"] and r["topology_matches"] is False
    assert run(led, LTSPICE_LED)["topology_matches"] is True


def test_not_compared_names_lines_after_end_and_the_text_says_element_lines_only(led):
    assert any("lines after .end" in n and "other lines" in n for n in run(led, LTSPICE_LED)["not_compared"])
    root = Path(__file__).parent.parent
    for text in ((root / ".claude/skills/kicad-simulation/SKILL.md").read_text(encoding="utf-8"),
                 (root / "src/boardproof/toolsets/verification.py").read_text(encoding="utf-8")):
        assert "element lines after" in text and "other lines after" in text
    assert "other lines after .end are ignored" in run(led, LTSPICE_LED)["topology_note"]
