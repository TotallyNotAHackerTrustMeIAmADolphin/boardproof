"""check_decoupling_placement (src/boardproof/decoupling_check.py; GitHub issue 26, formerly build ticket 115).

The rule is the measure of build ticket 114 (evals/rung1/build_rung1_layout.py): the gap between the
capacitor's supply pad edge and the IC supply pin's pad edge, read from raw pad ``(at)``/``(size)``/angle
nodes, at most MAX_PAD_GAP_MM (1.5 mm). The committed rung-1 board (after 114) is the positive case, the
same board with C3 back at its pre-114 place (153.0, 111.2, angle 0) the negative one; every other rule has
a mutant of that board that turns exactly its verdict.
"""

from __future__ import annotations

import importlib.util
import math
import shutil
from pathlib import Path

import pytest

from boardproof import decoupling_check as dc
from boardproof import raw_geometry, write_layer

ROOT = Path(__file__).resolve().parents[1]
DRIVER = ROOT / "evals" / "rung1" / "build_rung1_layout.py"
BOARD = ROOT / "evals" / "rung1" / "layout_run" / "07_poured" / "rung1_usbc_attiny1616.kicad_pcb"


@pytest.fixture(scope="module")
def drv():
    spec = importlib.util.spec_from_file_location("build_rung1_layout_26", DRIVER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _ref(fp) -> str:
    for c in fp.children:
        if c.name == "property" and c.values and c.values[0] == "Reference":
            return str(c.values[1])
    return ""


def _fp(root, ref):
    return next(f for f in root.children if f.name == "footprint" and _ref(f) == ref)


def mutated(tmp_path: Path, *, c3_at=None, c3_side=None, name="mut.kicad_pcb") -> Path:
    """The committed board with C3 moved or flipped, written through the write layer (never raw text)."""
    root = write_layer.load(BOARD)
    fp = _fp(root, "C3")
    if c3_at is not None:
        node = fp.find("at")
        write_layer.set_number(node, 0, c3_at[0])
        write_layer.set_number(node, 1, c3_at[1])
        write_layer.set_number(node, 2, c3_at[2])
    if c3_side is not None:
        write_layer.set_string(fp.find("layer"), 0, c3_side)
    out = tmp_path / name
    write_layer.save(root, out)
    return out


def check(board, **kw) -> dict:
    kw.setdefault("limit", dc.MAX_LIMIT)
    return dc.check_decoupling_placement(board, **kw)


def row(report: dict, ref: str) -> dict:
    [item] = [c for c in report["capacitors"]["items"] if c["capacitor"] == ref]
    return item


# --- the default is the rule of build ticket 114, measured the same way ------------------------------------------


def test_the_default_threshold_is_the_rung1_rule_and_the_measure_is_the_same(drv) -> None:
    assert dc.MAX_PAD_GAP_MM == drv.MAX_PAD_GAP_MM == 1.5
    report = check(BOARD)
    assert report["threshold_mm"] == 1.5
    table = drv.supply_capacitor_distances(write_layer.load(BOARD))
    for ref, entry in table.items():
        nearest = row(report, ref)["nearest"]
        assert nearest["ic"] == entry["ic"]
        assert nearest["gap_mm"] == pytest.approx(entry["gap_mm"], abs=1e-3), ref


def test_the_known_input_c3_is_near_c1_and_c2_are_far() -> None:
    report = check(BOARD)
    c3, c1, c2 = row(report, "C3"), row(report, "C1"), row(report, "C2")
    assert c3["nearest"]["ic"] == "U1" and c3["nearest"]["pin"] == "4"
    assert c3["nearest"]["gap_mm"] == pytest.approx(0.632, abs=1e-3)
    assert c3["status"] == "ok"
    assert c1["nearest"]["gap_mm"] == pytest.approx(3.55, abs=0.01) and c1["status"] == "too_far"
    assert c2["nearest"]["gap_mm"] == pytest.approx(6.10, abs=0.01) and c2["status"] == "too_far"
    assert {c["capacitor"] for c in report["capacitors"]["items"] if c["status"] != "ok"} == {"C1", "C2"}
    assert report["summary"]["flagged"] == 2 and report["summary"]["checked"] == 3
    assert report["verdict_is_layout_quality"] is False


def test_before_ticket_114_c3_is_flagged_and_after_it_is_not(tmp_path: Path) -> None:
    before = mutated(tmp_path, c3_at=(153.0, 111.2, 0.0))
    assert row(check(before), "C3")["status"] == "too_far"
    assert row(check(before), "C3")["nearest"]["gap_mm"] == pytest.approx(2.725, abs=1e-3)
    assert row(check(BOARD), "C3")["status"] == "ok"


# --- one mutant per rule -------------------------------------------------------------------------------------------


def test_rule_threshold_is_caller_settable_and_strict(tmp_path: Path) -> None:
    gap = row(check(BOARD), "C3")["nearest"]["gap_mm"]
    assert row(check(BOARD, max_pad_gap_mm=gap), "C3")["status"] == "ok"  # equal to the bound passes
    assert row(check(BOARD, max_pad_gap_mm=gap - 0.01), "C3")["status"] == "too_far"
    assert check(BOARD, max_pad_gap_mm=10.0)["summary"]["flagged"] == 0


def test_rule_the_gap_is_pad_edge_to_pad_edge_not_centre_to_centre() -> None:
    c3 = row(check(BOARD), "C3")["nearest"]
    assert c3["centre_distance_mm"] > c3["gap_mm"] + 0.3
    assert c3["gap_mm"] == pytest.approx(0.632, abs=1e-3)


def test_rule_the_rotation_of_the_capacitor_is_read_from_the_raw_angle(tmp_path: Path) -> None:
    """C3 turned by 180 degrees around its origin: its +3V3 pad is now the far one (gap grows)."""
    board = mutated(tmp_path, c3_at=(149.293, 108.239, 0.0))
    nearest = row(check(board), "C3")["nearest"]
    assert nearest["gap_mm"] > 1.5
    assert row(check(board), "C3")["status"] == "too_far"


def test_rule_the_side_must_be_the_same(tmp_path: Path) -> None:
    board = mutated(tmp_path, c3_side="B.Cu")
    c3 = row(check(board), "C3")
    assert c3["nearest"]["gap_mm"] == pytest.approx(0.632, abs=1e-3)  # geometry unchanged
    assert c3["nearest"]["same_side"] is False
    assert c3["status"] == "different_side"
    assert "different_side" in c3["reasons"]
    assert row(check(BOARD), "C3")["nearest"]["same_side"] is True


def test_rule_the_nearest_supply_pin_of_all_ics_decides(tmp_path: Path) -> None:
    """Every (capacitor pad, IC pin) pair of the net is listed; the status follows the nearest one."""
    c2 = row(check(BOARD), "C2")
    pins = {(p["ic"], p["pin"]) for p in c2["pairs"]}
    assert ("U2", "2") in pins or any(ic == "U2" for ic, _ in pins)
    assert any(ic == "U1" for ic, _ in pins), "C2's +3V3 net also supplies U1: those pins are pairs too"
    assert c2["nearest"]["gap_mm"] == min(p["gap_mm"] for p in c2["pairs"])
    assert c2["pairs_total"] == len(c2["pairs"])


def test_rule_path_estimate_is_at_least_the_gap_and_at_most_manhattan(tmp_path: Path) -> None:
    for item in check(BOARD)["capacitors"]["items"]:
        for p in item["pairs"]:
            assert p["path_estimate_mm"] >= p["gap_mm"] - 1e-9
            assert p["path_estimate_mm"] <= p["manhattan_mm"] + 1e-9
    # a diagonal offset costs more than the straight gap: 3 across and 4 up is 5.243 mm with 45-degree bends
    assert dc._octilinear(3.0, 4.0) == pytest.approx(4.0 + (math.sqrt(2) - 1) * 3.0)
    assert dc._octilinear(3.0, 0.0) == 3.0
    c3 = row(check(mutated(tmp_path, c3_at=(153.0, 111.2, 0.0))), "C3")["nearest"]
    assert c3["manhattan_mm"] >= c3["path_estimate_mm"] >= c3["gap_mm"] > 1.5


def test_rule_only_ics_and_capacitors_are_considered_and_the_basis_is_explicit() -> None:
    report = check(BOARD)
    cls = report["classification"]
    assert {n["net"]: n["basis"] for n in cls["power_nets"]}["+3V3"].startswith("name_pattern")
    assert "/VBUS_F" in {n["net"] for n in cls["power_nets"]}
    assert "GND" in cls["ground_nets"]
    assert set(cls["capacitors"]) == {"C1", "C2", "C3"}
    assert set(cls["ics"]) == {"U1", "U2"}
    assert cls["supply_pin_rule"]
    assert "not a verdict" in report["not_checked"][0].lower() or any("layout quality" in n for n in report["not_checked"])


def test_rule_no_name_patterns_means_nothing_is_guessed_and_it_says_so() -> None:
    report = check(BOARD, use_name_patterns=False)
    assert report["classification"]["power_nets"] == []
    assert report["capacitors"]["total"] == 0 and report["summary"]["checked"] == 0
    assert report["status"] == "nothing_checked"
    shared = {n["net"] for n in report["classification"]["unclassified_shared_nets"]}
    assert {"+3V3", "/VBUS_F"} <= shared  # named, so the caller sees what was skipped


def test_rule_a_caller_named_power_net_is_classified_with_basis_caller() -> None:
    report = check(BOARD, use_name_patterns=False, power_nets=["+3V3"])
    basis = {n["net"]: n["basis"] for n in report["classification"]["power_nets"]}
    assert basis == {"+3V3": "caller"}
    assert {c["capacitor"] for c in report["capacitors"]["items"]} == {"C2", "C3"}
    assert "/VBUS_F" in {n["net"] for n in report["classification"]["unclassified_shared_nets"]}


def test_rule_ic_and_capacitor_selection_by_the_caller() -> None:
    only_c3 = check(BOARD, capacitors=["C3"])
    assert [c["capacitor"] for c in only_c3["capacitors"]["items"]] == ["C3"]
    only_u2 = check(BOARD, ics=["U2"])
    assert {p["ic"] for c in only_u2["capacitors"]["items"] for p in c["pairs"]} == {"U2"}
    c3 = row(only_u2, "C3")  # +3V3 also reaches U2's output pin: against U2 alone C3 is far
    assert c3["nearest"]["ic"] == "U2" and c3["status"] == "too_far"
    assert only_u2["classification"]["ics"] == ["U2"]


def test_rule_unknown_selected_reference_is_named_not_ignored() -> None:
    report = check(BOARD, capacitors=["C3", "C99"])
    assert report["classification"]["unknown_references"] == ["C99"]


def test_paging_lists_worst_first_and_says_what_is_left() -> None:
    first = check(BOARD, limit=1)
    page = first["capacitors"]
    assert page["total"] == 3 and page["returned"] == 1 and page["all_listed"] is False and page["next_offset"] == 1
    assert page["items"][0]["capacitor"] == "C2"  # the farthest first
    rest = check(BOARD, offset=1, limit=5)["capacitors"]
    assert rest["all_listed"] is True and rest["next_offset"] is None
    assert [c["capacitor"] for c in rest["items"]] == ["C1", "C3"]
    assert first["summary"]["flagged"] == 2  # the summary covers all pages


@pytest.mark.parametrize("kw", [{"max_pad_gap_mm": -1}, {"max_pad_gap_mm": math.nan}, {"max_pad_gap_mm": math.inf},
                                {"max_pad_gap_mm": True}, {"offset": -1}, {"limit": 0}, {"limit": 100000}])
def test_bad_parameters_are_refused(kw) -> None:
    out = dc.check_decoupling_placement(BOARD, **kw)
    assert out["status"] == "error"


def test_missing_file_is_an_error(tmp_path: Path) -> None:
    assert dc.check_decoupling_placement(tmp_path / "nope.kicad_pcb")["status"] == "error"


def test_the_check_never_writes(tmp_path: Path) -> None:
    board = tmp_path / "b.kicad_pcb"
    shutil.copyfile(BOARD, board)
    before = board.read_bytes()
    check(board)
    assert board.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["b.kicad_pcb"]


# --- geometry helper ------------------------------------------------------------------------------------------------


def test_rotated_rectangle_gap_is_exact() -> None:
    """A 2x1 pad turned by 90 degrees is a 1x2 box: the same gap an axis-aligned box of that size gives."""
    a = dc._rect((0.0, 0.0), (2.0, 1.0), 90.0)
    b = dc._rect((3.0, 0.0), (1.0, 1.0), 0.0)
    gap, dx, dy = dc._gap(a, b)
    assert gap == pytest.approx(2.0) and dx == pytest.approx(2.0) and dy == pytest.approx(0.0)


def test_diagonal_rectangle_gap_and_overlap() -> None:
    a = dc._rect((0.0, 0.0), (2.0, 2.0), 45.0)  # a diamond with its corner at x = 1.4142
    b = dc._rect((3.0, 0.0), (1.0, 1.0), 0.0)
    gap, _, _ = dc._gap(a, b)
    assert gap == pytest.approx(3.0 - 0.5 - math.sqrt(2), abs=1e-9)
    assert dc._gap(a, dc._rect((1.0, 0.0), (1.0, 1.0), 0.0))[0] == 0.0
    assert dc._gap(a, dc._rect((0.0, 0.0), (0.2, 0.2), 0.0))[0] == 0.0  # one inside the other


def test_raw_pad_data_is_the_source_not_a_convenience_property() -> None:
    """The reader behind the check is raw_geometry (raw (at)/(size)/angle), the one the ratchet allows."""
    assert dc.raw_geometry is raw_geometry


# --- the tool: registration, annotations and the wording an LLM reads --------------------------------------------


@pytest.mark.asyncio
async def test_the_tool_is_a_read_only_verification_tool_with_pinned_wording() -> None:
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import verification

    server = MCPServer("t")
    assert "check_decoupling_placement" in verification.build(server)
    tool = next(t for t in await server.list_tools() if t.name == "check_decoupling_placement")
    assert tool.annotations.read_only_hint is True and tool.annotations.destructive_hint is False
    assert set(tool.input_schema["properties"]) == {
        "pcb_path", "max_pad_gap_mm", "power_nets", "use_name_patterns", "capacitors", "ics", "offset", "limit"}
    assert tool.input_schema["required"] == ["pcb_path"]
    text = " ".join(tool.description.split())
    for phrase in (
        "Read-only batch check",
        "the measure of the rung-1 placement rule of build ticket 114",
        "never convenience properties",
        "default 1.5",
        "classification.power_nets",
        "never guessed",
        "unclassified_shared_nets and NOT checked",
        "the board file has no pin type",
        "What this is NOT: not a verdict on layout quality",
        "not verify_board",
        "not a route, inductance, via or return-path check",
        "summary has NO pass word",
        "coverage_complete is false whenever nothing was checked",
        "excluded_footprints_on_supply_nets",
        "never report all_checked_within_bound alone",
        "excluded_by_reference_rule_on_supply_nets",
        "3V3_EN, 5V_DETECT",
        "Only a rect pad is measured exactly",
        "duplicate_reference",
    ):
        assert phrase in text, phrase


@pytest.mark.asyncio
async def test_a_call_through_the_server_returns_the_report() -> None:
    import json

    from mcp.client import Client

    from boardproof.server import build_server

    async with Client(build_server(), mode="auto") as client:
        await client.call_tool("load_toolset", {"name": "verification"})
        result = await client.call_tool("check_decoupling_placement", {"pcb_path": str(BOARD)})
    report = json.loads(result.content[0].text)
    assert report["summary"]["flagged"] == 2 and report["threshold_mm"] == 1.5


# --- the texts a reader sees -----------------------------------------------------------------------------------------


def test_skill_readme_and_tool_state_what_is_checked_and_what_is_not() -> None:
    pcb = " ".join((ROOT / ".claude/skills/kicad-pcb/SKILL.md").read_text(encoding="utf-8").split())
    ver = " ".join((ROOT / ".claude/skills/kicad-verification/SKILL.md").read_text(encoding="utf-8").split())
    readme = " ".join((ROOT / "README.md").read_text(encoding="utf-8").split())
    for phrase in ("check_decoupling_placement", "Read `classification` before the rows", "unclassified_shared_nets",
                   "the board file has no pin type", "not a verdict on layout quality", "not `verify_board`",
                   "not a route, inductance, via or return-path check", "There is no pass word", "coverage_complete",
                   "excluded_by_reference_rule_on_supply_nets", "3V3_EN"):
        assert phrase in pcb, phrase
    assert "report `all_checked_within_bound` only together with `coverage_complete`" in ver
    assert "A `verify_board` PASS is not a good layout" in ver and "verdict_is_layout_quality" in ver
    assert "A `verify_board` PASS means the board is connected and DRC-clean. It does not mean the layout is good" in readme


# --- KiCad's installed demo boards: every one reads, the counts add up (the baseline is in ARCHITECTURE) ------------


def test_over_the_installed_demo_boards_every_board_reads_and_the_counts_add_up(kicad_demos_dir: Path) -> None:
    boards = [b for b in sorted(kicad_demos_dir.rglob("*.kicad_pcb")) if "backup" not in str(b).lower()]
    assert boards
    tally = {"all_within": 0, "ok_flagged": 0, "nothing_checked": 0}
    for board in boards:
        report = dc.check_decoupling_placement(board, limit=dc.MAX_LIMIT)
        assert report["status"] in ("ok", "nothing_checked"), (board, report)
        summary = report["summary"]
        assert summary["checked"] == summary["flagged"] + summary["ok"] == report["capacitors"]["total"]
        if report["status"] == "nothing_checked":
            tally["nothing_checked"] += 1
        else:
            tally["all_within" if summary["all_checked_within_bound"] else "ok_flagged"] += 1
    assert sum(tally.values()) == len(boards)


def test_rule_a_capacitor_with_both_pads_on_one_net_is_skipped_and_named(tmp_path: Path) -> None:
    root = write_layer.load(BOARD)
    pad2 = next(p for p in _fp(root, "C3").children if p.name == "pad" and p.values and str(p.values[0]) == "2")
    net = pad2.find("net")
    write_layer.set_number(net, 0, 1)
    write_layer.set_string(net, 1, "+3V3")
    out = tmp_path / "same_net.kicad_pcb"
    write_layer.save(root, out)
    report = check(out)
    assert {"reference": "C3", "reason": "both_pads_on_one_net", "net": "+3V3"} in report["skipped"]
    assert "C3" not in {c["capacitor"] for c in report["capacitors"]["items"]}


def test_rule_a_capacitor_whose_net_has_no_selected_ic_pin_is_skipped_and_named() -> None:
    report = check(BOARD, ics=["U1"])
    assert {"reference": "C1", "reason": "no_supply_pin_on_net", "nets": ["/VBUS_F"]} in report["skipped"]
    assert report["summary"]["skipped"] == 1
    assert "C1" not in {c["capacitor"] for c in report["capacitors"]["items"]}


def _pad_node(root, ref, number):
    return next(p for p in _fp(root, ref).children if p.name == "pad" and p.values and str(p.values[0]) == number)


def test_the_pair_fields_are_exact_on_the_known_pair() -> None:
    """C3 pad 1 to U1 pin 4: centres measured independently from the raw reader, gap on the pad edges."""
    fps = {f.reference: f for f in raw_geometry.read_footprints(write_layer.load(BOARD))}
    a = next(p for p in fps["C3"].pads if p.number == "1").at_mm
    b = next(p for p in fps["U1"].pads if p.number == "4").at_mm
    pair = row(check(BOARD), "C3")["nearest"]
    assert pair["centre_distance_mm"] == pytest.approx(math.hypot(a[0] - b[0], a[1] - b[1]), abs=1e-4)
    assert pair["over_threshold"] is False
    gap = pair["gap_mm"]
    tight = [p for p in row(check(BOARD, max_pad_gap_mm=gap), "C3")["pairs"] if p["pin"] == "4"][0]
    assert tight["over_threshold"] is False  # equal to the bound is not over it
    loose = [p for p in row(check(BOARD, max_pad_gap_mm=gap - 0.001), "C3")["pairs"] if p["pin"] == "4"][0]
    assert loose["over_threshold"] is True


def test_unclassified_shared_nets_are_exactly_the_nets_both_sides_touch() -> None:
    shared = check(BOARD, use_name_patterns=False)["classification"]["unclassified_shared_nets"]
    assert shared == [
        {"net": "+3V3", "capacitors": ["C2", "C3"], "ics": ["U1", "U2"]},
        {"net": "/VBUS_F", "capacitors": ["C1"], "ics": ["U2"]},
    ]


def test_a_flipped_capacitor_counts_in_the_flagged_summary(tmp_path: Path) -> None:
    report = check(mutated(tmp_path, c3_side="B.Cu"))
    assert report["summary"]["flagged"] == 3 and report["summary"]["all_checked_within_bound"] is False


def test_a_same_side_pin_farther_away_beats_a_nearer_pin_on_the_other_side(tmp_path: Path) -> None:
    """U1 flipped to the back: C3's nearest pin (U1 pin 4, 0.632 mm) is on the wrong side, so U2's pin decides."""
    root = write_layer.load(BOARD)
    write_layer.set_string(_fp(root, "U1").find("layer"), 0, "B.Cu")
    board = tmp_path / "u1_back.kicad_pcb"
    write_layer.save(root, board)
    c3 = row(check(board), "C3")
    assert c3["nearest"]["ic"] == "U2" and c3["nearest"]["same_side"] is True
    assert c3["status"] == "too_far"
    assert any(p["ic"] == "U1" and not p["same_side"] and p["gap_mm"] < 1 for p in c3["pairs"])


def test_a_through_hole_supply_pad_is_on_both_sides(tmp_path: Path) -> None:
    root = write_layer.load(BOARD)
    write_layer.set_string(_fp(root, "C3").find("layer"), 0, "B.Cu")
    write_layer.set_symbol(_pad_node(root, "U1", "4"), 1, "thru_hole")
    board = tmp_path / "tht.kicad_pcb"
    write_layer.save(root, board)
    c3 = row(check(board), "C3")
    assert c3["nearest"]["pin"] == "4" and c3["nearest"]["same_side"] is True and c3["status"] == "ok"


def test_a_non_plated_hole_is_never_a_supply_pin(tmp_path: Path) -> None:
    root = write_layer.load(BOARD)
    write_layer.set_symbol(_pad_node(root, "U1", "4"), 1, "np_thru_hole")
    board = tmp_path / "np.kicad_pcb"
    write_layer.save(root, board)
    c3 = row(check(board), "C3")
    assert all(p["pin"] != "4" for p in c3["pairs"]) and c3["nearest"]["ic"] == "U2"


def test_a_net_only_a_capacitor_touches_is_not_listed_as_shared(tmp_path: Path) -> None:
    root = write_layer.load(BOARD)
    net = _pad_node(root, "C3", "2").find("net")
    write_layer.set_number(net, 0, 99)
    write_layer.set_string(net, 1, "SIG_ONLY_C3")
    board = tmp_path / "sig.kicad_pcb"
    write_layer.save(root, board)
    shared = check(board, use_name_patterns=False)["classification"]["unclassified_shared_nets"]
    assert "SIG_ONLY_C3" not in {n["net"] for n in shared}



# --- review findings of d675cf5: nothing silent, no bare pass word --------------------------------------------------


def _renamed(tmp_path: Path, renames: dict[str, str], name="renamed.kicad_pcb") -> Path:
    root = write_layer.load(BOARD)
    for old, new in renames.items():
        fp = _fp(root, old)
        for c in fp.children:
            if c.name == "property" and c.values and c.values[0] == "Reference":
                write_layer.set_string(c, 1, new)
    out = tmp_path / name
    write_layer.save(root, out)
    return out


def test_duplicate_references_are_skipped_naming_every_footprint_and_block_coverage(tmp_path: Path) -> None:
    report = check(_renamed(tmp_path, {"C1": "C3", "C2": "C3"}))
    [dup] = [s for s in report["skipped"] if s["reason"] == "duplicate_reference"]
    assert dup["reference"] == "C3" and dup["count"] == 3 and len(dup["uuids"]) == 3
    assert "C3" not in {c["capacitor"] for c in report["capacitors"]["items"]}
    assert "C3" not in report["classification"]["excluded_by_reference_rule_on_supply_nets"]  # named once, as skipped
    assert report["summary"]["coverage_complete"] is False and "skipped" in report["summary"]["coverage_gaps"]


def test_a_duplicate_ic_reference_is_named_too(tmp_path: Path) -> None:
    report = check(_renamed(tmp_path, {"U2": "U1"}))
    assert any(s["reason"] == "duplicate_reference" and s["reference"] == "U1" for s in report["skipped"])
    assert report["summary"]["coverage_complete"] is False


def test_an_empty_reference_on_a_supply_net_is_skipped_and_named(tmp_path: Path) -> None:
    report = check(_renamed(tmp_path, {"C3": ""}))
    [empty] = [s for s in report["skipped"] if s["reason"] == "empty_reference"]
    assert empty["nets"] == ["+3V3"] and empty["uuid"]
    assert report["summary"]["coverage_complete"] is False


def test_an_unnumbered_pad_on_a_supply_net_is_skipped_and_named(tmp_path: Path) -> None:
    root = write_layer.load(BOARD)
    write_layer.set_string(_pad_node(root, "C3", "1"), 0, "")
    board = tmp_path / "unnumbered.kicad_pcb"
    write_layer.save(root, board)
    report = check(board)
    assert {"reference": "C3", "reason": "unnumbered_pad_on_supply_net", "nets": ["+3V3"]} in report["skipped"]
    assert report["summary"]["coverage_complete"] is False


def test_there_is_no_bare_pass_word_and_coverage_is_explicit() -> None:
    report = check(BOARD)
    assert "board_passes" not in str(report)
    assert report["summary"]["all_checked_within_bound"] is False  # C1 and C2 are far


def test_coverage_is_incomplete_when_nothing_was_checked() -> None:
    report = check(BOARD, use_name_patterns=False)
    assert report["status"] == "nothing_checked"
    s = report["summary"]
    assert s["all_checked_within_bound"] is False and s["coverage_complete"] is False
    assert "nothing_checked" in s["coverage_gaps"]


def test_complete_coverage_needs_no_skip_and_no_unclassified_shared_net() -> None:
    clean = check(BOARD, capacitors=["C3"], ics=["U1", "U2", "D1", "F1", "J1", "J2", "C1", "C2"], power_nets=[])
    s = clean["summary"]
    # every footprint on a supply net is judged (named by the caller), nothing is skipped or unclassified
    assert s["all_checked_within_bound"] is True and s["coverage_complete"] is True and s["coverage_gaps"] == []
    unclassified = check(BOARD, use_name_patterns=False, power_nets=["+3V3"], capacitors=["C3", "C1"], ics=["U1", "U2"])
    assert unclassified["classification"]["unclassified_shared_nets"]  # /VBUS_F: C1 and U2 share it, not classified
    assert unclassified["summary"]["coverage_complete"] is False
    assert "unclassified_shared_nets" in unclassified["summary"]["coverage_gaps"]
    skip = check(BOARD, ics=["U1"], capacitors=["C3", "C1"])
    assert skip["summary"]["skipped"] == 1 and skip["summary"]["coverage_complete"] is False
    assert skip["summary"]["all_checked_within_bound"] is True  # what was checked is fine; coverage says what was not


def test_a_capacitor_between_two_supply_nets_is_judged_pad_by_pad(tmp_path: Path) -> None:
    """C3 pad 2 (GND) is put on +5V together with U1 pad 5: each capacitor pad meets only the pins of its own net."""
    root = write_layer.load(BOARD)
    for ref, number in (("C3", "2"), ("U1", "5")):
        net = _pad_node(root, ref, number).find("net")
        write_layer.set_number(net, 0, 98)
        write_layer.set_string(net, 1, "+5V")
    board = tmp_path / "two_supply.kicad_pcb"
    write_layer.save(root, board)
    report = check(board, capacitors=["C3"], ics=["U1"])
    by_net = {r["net"]: r for r in report["capacitors"]["items"] if r["capacitor"] == "C3"}
    assert set(by_net) == {"+3V3", "+5V"}
    assert by_net["+3V3"]["supply_pad"] == "1" and by_net["+3V3"]["status"] == "ok"
    assert {p["net"] for p in by_net["+3V3"]["pairs"]} == {"+3V3"}
    assert by_net["+5V"]["supply_pad"] == "2" and {p["net"] for p in by_net["+5V"]["pairs"]} == {"+5V"}
    assert {p["pin"] for p in by_net["+5V"]["pairs"]} == {"5"}


def test_roundrect_pads_are_labelled_bounding_and_only_rect_is_exact(tmp_path: Path) -> None:
    assert row(check(BOARD), "C3")["nearest"]["pad_boxes"] == "bounding"
    root = write_layer.load(BOARD)
    write_layer.set_symbol(_pad_node(root, "C3", "1"), 2, "rect")
    write_layer.set_symbol(_pad_node(root, "U1", "4"), 2, "rect")
    board = tmp_path / "rect.kicad_pcb"
    write_layer.save(root, board)
    assert row(check(board), "C3")["nearest"]["pad_boxes"] == "exact"
    write_layer.set_symbol(_pad_node(root, "U1", "4"), 2, "oval")
    oval = tmp_path / "oval.kicad_pcb"
    write_layer.save(root, oval)
    assert row(check(oval), "C3")["nearest"]["pad_boxes"] == "bounding"


def test_references_excluded_by_the_reference_rule_are_listed_not_silent(tmp_path: Path) -> None:
    report = check(_renamed(tmp_path, {"U2": "Q7", "C1": "X1"}))
    excluded = report["classification"]["excluded_by_reference_rule_on_supply_nets"]
    assert "Q7" in excluded and "X1" in excluded
    assert "U1" not in excluded and "C3" not in excluded
    narrowed = check(BOARD, capacitors=["C3"], ics=["U1"])["classification"]["excluded_by_reference_rule_on_supply_nets"]
    assert {"C1", "C2", "U2"} <= set(narrowed) and "U1" not in narrowed and "C3" not in narrowed


def test_name_match_is_case_insensitive_and_ground_wins() -> None:
    assert dc._classify("vdd_core", set(), True)[0] == "power"
    assert dc._classify("+3v3", set(), True)[0] == "power"
    assert dc._classify("/sheet/vcc", set(), True)[0] == "power"
    assert dc._classify("gnd", set(), True)[0] == "ground"
    assert dc._classify("Vss", set(), True)[0] == "ground"
    assert dc._classify("GNDA", set(), True)[0] == "ground"
    assert dc._classify("3V3_EN", set(), True)[0] == "power"  # the documented limit of the suffix branch


def test_reference_rules_are_u_for_ics_and_c_for_capacitors() -> None:
    assert dc._IC_REF.match("U1") and dc._IC_REF.match("U12A")
    assert not dc._IC_REF.match("Q1") and not dc._IC_REF.match("J1") and not dc._IC_REF.match("IC1")
    assert dc._CAP_REF.match("C7") and not dc._CAP_REF.match("CN1") and not dc._CAP_REF.match("D3")


def test_crossing_pads_without_a_corner_inside_the_other_have_gap_zero() -> None:
    plus_a = dc._rect((0.0, 0.0), (4.0, 1.0), 0.0)
    plus_b = dc._rect((0.0, 0.0), (1.0, 4.0), 0.0)
    assert not any(dc._inside(p, plus_b) for p in plus_a) and not any(dc._inside(p, plus_a) for p in plus_b)
    assert dc._gap(plus_a, plus_b) == (0.0, 0.0, 0.0)
    assert dc._gap(plus_a, dc._rect((0.0, 3.0), (1.0, 4.0), 0.0))[0] == pytest.approx(0.5)


# --- delta review of dcd0c0e: a part nobody judged is a coverage gap ------------------------------------------------


@pytest.mark.parametrize("renames", [{"C1": "C_1"}, {"C1": "c1"}, {"C1": "CAP1"}, {"U1": "IC1"}])
def test_a_part_left_out_by_the_reference_rule_makes_coverage_incomplete(tmp_path: Path, renames) -> None:
    report = check(_renamed(tmp_path, renames))
    new = next(iter(renames.values()))
    assert new in report["classification"]["excluded_by_reference_rule_on_supply_nets"]
    assert report["summary"]["coverage_complete"] is False
    assert "excluded_footprints_on_supply_nets" in report["summary"]["coverage_gaps"]


def test_coverage_is_complete_only_when_nothing_on_a_supply_net_was_left_unjudged() -> None:
    first = check(BOARD)
    excluded = first["classification"]["excluded_by_reference_rule_on_supply_nets"]
    assert excluded and first["summary"]["coverage_complete"] is False  # D1, F1, J1, J2: left out by the rule
    assert first["summary"]["coverage_gaps"] == ["excluded_footprints_on_supply_nets"]
    full = check(BOARD, capacitors=["C1", "C2", "C3"], ics=["U1", "U2", *excluded])
    assert full["classification"]["excluded_by_reference_rule_on_supply_nets"] == []
    assert full["summary"]["coverage_complete"] is True and full["summary"]["coverage_gaps"] == []


def test_a_footprint_whose_only_supply_pad_is_unnumbered_is_still_listed_as_excluded(tmp_path: Path) -> None:
    root = write_layer.load(BOARD)
    for c in _fp(root, "C3").children:
        if c.name == "property" and c.values and c.values[0] == "Reference":
            write_layer.set_string(c, 1, "X9")
    write_layer.set_string(_pad_node(root, "X9", "1"), 0, "")
    board = tmp_path / "x9.kicad_pcb"
    write_layer.save(root, board)
    assert "X9" in check(board)["classification"]["excluded_by_reference_rule_on_supply_nets"]


def test_a_net_that_only_unnumbered_pads_carry_is_still_classified(tmp_path: Path) -> None:
    root = write_layer.load(BOARD)
    pad = _pad_node(root, "C3", "1")
    write_layer.set_number(pad.find("net"), 0, 97)
    write_layer.set_string(pad.find("net"), 1, "+9V9")
    write_layer.set_string(pad, 0, "")
    board = tmp_path / "only_unnumbered.kicad_pcb"
    write_layer.save(root, board)
    report = check(board)
    assert {"reference": "C3", "reason": "unnumbered_pad_on_supply_net", "nets": ["+9V9"]} in report["skipped"]
    assert "+9V9" in {n["net"] for n in report["classification"]["power_nets"]}
