"""Review round for ticket 06: the adapter applies only the router's added copper, the
invariant fingerprint bites on each thing it claims to cover, the exit-code and lever
wiring is pinned, and every non-baseline KRT lever runs once for real."""

from __future__ import annotations

import hashlib
import shutil
from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp import rung_backends, rung_runner
from kicad_mcp.routers import kicad_routing_tools as krt_tools

from test_rung_backends import FIXTURES, KRT_BASELINE, needs_krt, stress_board  # noqa: F401


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _record(exit_code=0, **over):
    record = {
        "exit_code": exit_code, "elapsed_s": 1.5, "router_version": "0.22.0",
        "version_check": {"matches": True}, "strict_sizes_violated": exit_code == 3,
        "delivered_below_requested": [], "logged_parameters": {"strict_sizes": True},
        "router_overwrote_project": False,
        "log": {"failed_nets": [], "boxed_in_nets": [], "router_reported_nets": ["SIG1"],
                "project_rule_writes": [], "fab_floor_relaxed": False, "fab_tier_escalations": 0},
    }
    record.update(over)
    return record


class FakeRouter:
    """Stands in for ``run_kicad_routing_tools``: writes a routed copy that carries the
    added copper AND router-side edits the adapter must never adopt."""

    def __init__(self, exit_code=0, sabotage=True, error=None, **record_over):
        self.exit_code, self.sabotage, self.error, self.record_over = exit_code, sabotage, error, record_over
        self.calls = []

    def __call__(self, board, routed, **kwargs):
        from kicad_tools.schema import PCB

        self.calls.append(kwargs)
        if self.error:
            raise self.error
        Path(routed).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(board, routed)
        pcb = PCB.load(routed)
        pcb.add_trace((10.0, 10.0), (12.0, 10.0), width=0.25, layer="F.Cu", net="SIG1")
        if self.sabotage:  # router-side edits that must not land on the graded board
            footprint = next(c for c in pcb._sexp.children if c.name == "footprint")
            at = footprint.find("at")
            at.set_value(0, float(at.values[0]) + 3.0)
            net = next(c for c in pcb._sexp.children if c.name == "net" and len(c.values) > 1)
            net.set_value(1, "RENAMED_BY_ROUTER")
        write_layer.save_pcb(pcb, routed)
        return _record(self.exit_code, **self.record_over)


@pytest.fixture
def fake(monkeypatch):
    def install(**kw):
        router = FakeRouter(**kw)
        monkeypatch.setattr(krt_tools, "run_kicad_routing_tools", router)
        return router

    return install


def _run(board, tmp_path, params=None, timeout=60.0):
    (tmp_path / "a").mkdir(exist_ok=True)
    return rung_backends.krt_backend()(board, params or dict(KRT_BASELINE), timeout, tmp_path / "a" / "router.log")


# --- (1) only the router's ADDED copper reaches the graded board ------------------------------


def test_graded_board_is_working_copy_plus_delta_never_the_routers_file(stress_board, tmp_path, fake):  # noqa: F811
    router = fake()
    before = rung_backends.board_invariants(stress_board)
    outcome = _run(stress_board, tmp_path)
    routed = tmp_path / "a" / "router_scratch" / stress_board.name

    assert outcome["status"] == "ok"
    assert stress_board.read_text().count("(segment") == 1  # the added copper is on the board
    assert _sha(stress_board) != _sha(routed)  # ...but the board is not the router's file
    assert rung_runner._changed(before, rung_backends.board_invariants(stress_board)) == []  # sabotage did not land
    assert rung_backends.board_invariants(routed)["placement"] != before["placement"]
    assert rung_backends.board_invariants(routed)["nets"] != before["nets"]
    assert outcome["facts"]["added_segments"] == 1
    assert router.calls[0]["fab_overrides"] is None or Path(router.calls[0]["fab_overrides"]).parent.name == "router_scratch"


# --- exit-code and lever wiring ---------------------------------------------------------------


@pytest.mark.parametrize("exit_code, status", [(0, "ok"), (3, "ok"), (1, "failure"), (2, "failure"), (-1, "failure")])
def test_only_exit_codes_0_and_3_are_an_ok_router_run(stress_board, tmp_path, fake, exit_code, status):  # noqa: F811
    fake(exit_code=exit_code, sabotage=False)
    outcome = _run(stress_board, tmp_path)
    assert outcome["status"] == status
    if status == "failure":
        assert outcome["cause"] == "router-failure"


def test_levers_reach_the_router_call(stress_board, tmp_path, fake):  # noqa: F811
    router = fake(sabotage=False)
    _run(stress_board, tmp_path, {"escalation": "board", "grid_step_mm": 0.05, "max_iterations": 800000,
                                  "ordering": "inside_out"})
    call = router.calls[0]
    assert call["grid_step_mm"] == 0.05
    assert call["max_iterations"] == 800000
    assert call["advanced_flags"] == {"--ordering": "inside_out"}
    assert call["escalation"] == "board"
    router2 = fake(sabotage=False)
    _run(stress_board, tmp_path, dict(KRT_BASELINE))
    assert router2.calls[0]["advanced_flags"] is None
    assert router2.calls[0]["grid_step_mm"] is None
    assert router2.calls[0]["timeout"] == 60


def test_escalation_off_pins_no_floor_overrides(stress_board, tmp_path, fake):  # noqa: F811
    router = fake(sabotage=False)
    _run(stress_board, tmp_path, {**KRT_BASELINE, "escalation": "off"})
    assert router.calls[0]["escalation"] == "off"
    assert router.calls[0]["fab_overrides"] is None


# --- (7) timeouts are recognised by the router's own message prefix, nothing else --------------


def test_router_time_is_reported_separately_from_adapter_overhead(stress_board, tmp_path, fake):  # noqa: F811
    fake(sabotage=False, elapsed_s=2.25)
    outcome = _run(stress_board, tmp_path)
    assert outcome["router_elapsed_s"] == 2.25


# --- (9) the router's own disclosures are recorded as facts ----------------------------------------


def test_removed_counts_and_fab_floor_facts_are_recorded(stress_board, tmp_path, fake):  # noqa: F811
    fake(sabotage=False)
    facts = _run(stress_board, tmp_path)["facts"]
    for key in ("removed_segment_count", "removed_via_count", "fab_floor_relaxed", "fab_tier_escalations"):
        assert key in facts
    assert facts["fab_floor_relaxed"] is False
    assert facts["removed_segment_count"] == 0


# --- invariants bite on each thing they claim to cover ------------------------------------------------


def _edit(board, tmp_path, name, mutate):
    from kicad_tools.schema import PCB

    out = tmp_path / name
    pcb = PCB.load(board)
    mutate(pcb._sexp)
    write_layer.save_pcb(pcb, out)
    return rung_backends.board_invariants(out)


def test_net_table_edit_is_caught_by_the_nets_fingerprint(stress_board, tmp_path):  # noqa: F811
    before = rung_backends.board_invariants(stress_board)

    def rename(root):
        next(c for c in root.children if c.name == "net" and len(c.values) > 1).set_value(1, "X")

    after = _edit(stress_board, tmp_path, "renamed.kicad_pcb", rename)
    assert after["nets"] != before["nets"]
    assert after["placement"] == before["placement"]


def test_a_pad_net_reassignment_is_caught_by_the_placement_fingerprint(stress_board, tmp_path):  # noqa: F811
    before = rung_backends.board_invariants(stress_board)

    def reassign(root):
        for fp in (c for c in root.children if c.name == "footprint"):
            for pad in fp.find_all("pad"):
                net = pad.find("net")
                if net is not None and len(net.values) > 1:
                    net.set_value(1, "OTHER")
                    return

    after = _edit(stress_board, tmp_path, "padnet.kicad_pcb", reassign)
    assert after["placement"] != before["placement"]


_KEEPOUT = ('(zone (net 0) (net_name "") (layer "F.Cu") (hatch edge 0.5) '
            '(keepout (tracks not_allowed) (vias not_allowed) (pads allowed) (copperpour allowed)) '
            '(polygon (pts (xy 1 1) (xy 5 1) (xy 5 5))))')


def test_keepouts_and_zone_outlines_are_fingerprinted_but_not_their_fill(stress_board, tmp_path):  # noqa: F811
    from kicad_tools.sexp import parse_string

    with_zone = tmp_path / "zone.kicad_pcb"
    shutil.copy(stress_board, with_zone)
    from kicad_tools.schema import PCB

    pcb = PCB.load(with_zone)
    pcb._sexp.append(parse_string(_KEEPOUT))
    write_layer.save_pcb(pcb, with_zone)
    base = rung_backends.board_invariants(with_zone)
    assert base["zones"] != rung_backends.board_invariants(stress_board)["zones"]

    def edit_polygon(root):
        zone = next(c for c in root.children if c.name == "zone")
        zone.find("polygon").find("pts").find("xy").set_value(0, 2.0)

    assert _edit(with_zone, tmp_path, "moved.kicad_pcb", edit_polygon)["zones"] != base["zones"]

    def add_fill(root):
        zone = next(c for c in root.children if c.name == "zone")
        zone.append(parse_string("(filled_polygon (layer \"F.Cu\") (pts (xy 1 1) (xy 2 1) (xy 2 2)))"))

    assert _edit(with_zone, tmp_path, "filled.kicad_pcb", add_fill)["zones"] == base["zones"]


def test_preexisting_copper_must_survive_but_added_copper_is_the_routers(stress_board, tmp_path):  # noqa: F811
    from kicad_tools.schema import PCB

    seeded = tmp_path / "seeded.kicad_pcb"
    pcb = PCB.load(stress_board)
    pcb.add_trace((10.0, 10.0), (12.0, 10.0), width=0.25, layer="F.Cu", net="SIG1")
    write_layer.save_pcb(pcb, seeded)
    before = rung_backends.board_invariants(seeded)["copper"]
    assert before["kind"] == "superset" and len(before["items"]) == 1

    more = tmp_path / "more.kicad_pcb"
    pcb = PCB.load(seeded)
    pcb.add_trace((20.0, 10.0), (22.0, 10.0), width=0.25, layer="F.Cu", net="SIG2")
    write_layer.save_pcb(pcb, more)
    after_more = rung_backends.board_invariants(more)
    assert rung_runner._changed({"copper": before}, {"copper": after_more["copper"]}) == []

    stripped = tmp_path / "stripped.kicad_pcb"
    pcb = PCB.load(seeded)
    for node in [c for c in pcb._sexp.children if c.name == "segment"]:
        pcb._sexp.remove(node)
    write_layer.save_pcb(pcb, stripped)
    after_stripped = rung_backends.board_invariants(stripped)
    assert rung_runner._changed({"copper": before}, {"copper": after_stripped["copper"]}) == ["copper"]


# --- (2) every non-baseline KRT lever, for real ---------------------------------------------------------

_REAL_LEVERS = [
    {"escalation": "off"},
    {"grid_step_mm": 0.05},
    {"grid_step_mm": 0.025},
    {"max_iterations": 800000},
    {"ordering": "inside_out"},
    {"ordering": "original"},
    {"ordering": "bus"},
]


@needs_krt
@pytest.mark.parametrize("lever", _REAL_LEVERS, ids=lambda d: "-".join(f"{k}={v}" for k, v in d.items()))
def test_each_predeclared_krt_lever_runs_for_real_on_a_small_fixture(stress_board, tmp_path, lever):  # noqa: F811
    params = {**KRT_BASELINE, **lever}
    assert rung_runner.plan_problems(
        {"lever": "x", "parameters": params}, rung_runner.load_protocol()["backends"]["krt"]["levers"]) == []
    before = rung_backends.board_invariants(stress_board)
    pro = _sha(stress_board.with_suffix(".kicad_pro"))

    outcome = _run(stress_board, tmp_path, params, timeout=180.0)

    assert outcome["status"] == "ok", outcome
    facts = outcome["facts"]
    assert facts["exit_code"] in (0, 3)
    assert outcome["router_elapsed_s"] > 0.1
    logged = facts["logged_parameters"]
    assert logged["escalation"] == params["escalation"]
    assert logged["grid_step_mm"] == params["grid_step_mm"]
    assert logged["max_iterations"] == params["max_iterations"]
    expected_flags = {"--ordering": params["ordering"]} if params["ordering"] else {}
    assert logged["advanced_flags"] == expected_flags
    after = rung_backends.board_invariants(stress_board)
    assert {k: v for k, v in after.items() if k != "copper"} == {k: v for k, v in before.items() if k != "copper"}
    assert _sha(stress_board.with_suffix(".kicad_pro")) == pro
