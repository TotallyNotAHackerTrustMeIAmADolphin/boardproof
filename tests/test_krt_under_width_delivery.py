"""Build ticket 37: why KRT 0.23.1 "delivers under-width" tracks and vias, and what is ours.

Both measurements replay small committed fixtures taken from ``paradigm-review-2`` ticket 04's
KRT batches (``tests/fixtures/uw/r2krt`` and ``r1krt``, see ``tests/fixtures/README.md``): the
frozen input (``in/``) and the board attempt 00 delivered (``out/``) are copied into
``tmp_path``; ``attempt0.json`` holds the attempt's recorded router facts. Nothing in the
repository is written.

* Rung 2 (``pic_programmer``): KRT's ledger names four 0.326 mm necks on one net, the CLI
  exits 3, and the delivered board holds no copper under its net-class width. The ledger
  lists necking events, not shipped copper (KRT prunes and merges segments afterwards), so
  the exit-3 flag alone must not fail an attempt whose raw board check is clean.
* Rung 1: the delivered board really holds +3V3 copper under the Power class (0.4 mm tracks,
  0.6/0.3 mm vias). That one is KRT's neck-down and "kept at the run size" descents; it stays
  a policy hit, and every reported row now names the KRT site that made it.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest

from kicad_mcp import rung_backends, rung_runner

UW = Path(__file__).resolve().parent / "fixtures" / "uw"
RUNG2 = UW / "r2krt"
RUNG1 = UW / "r1krt"
PASS_GATE = {"verdict": "PASS", "unconnected": [], "violations": [], "warnings": [],
             "unstable": False, "incomplete_reasons": []}


def _attempt0(batch: Path) -> dict:
    return json.loads((batch / "attempt0.json").read_text(encoding="utf-8"))


def _run_one(tmp_path: Path, batch: Path, board_name: str, facts: dict) -> dict:
    """The committed attempt-00 delivery, fed through the shared under-width wrapper and the
    runner with the router facts KRT's adapter recorded for it."""
    source = tmp_path / "input"
    shutil.copytree(batch / "in", source)
    routed = batch / "out" / board_name

    def backend(board, params, timeout_s, log):
        shutil.copyfile(routed, board)
        return {"status": "ok", "cause": None, "facts": json.loads(json.dumps(facts))}

    plan = rung_runner.load_protocol()["backends"]["krt"]["plan"][:1]
    return rung_runner.run_rung(
        rung="ticket37-replay", backend_name="krt", board=source / board_name,
        rule_files=[source / board_name.replace(".kicad_pcb", ".kicad_pro")], plan=plan,
        backend=rung_backends.with_under_width_check(backend), gate=lambda board: dict(PASS_GATE),
        baseline={"rules": "pinned"}, tool_versions={}, runs_root=tmp_path / "runs",
        repo_state=lambda: {"commit": "abc123", "dirty": False},
        protocol=rung_runner.load_protocol(), invariants=lambda board: {},
    )


# --- rung 2: KRT's ledger names features the board does not hold ---------------------------------


def test_reproduction_rung2_exit3_failed_a_board_with_no_under_width_copper(tmp_path):
    recorded = _attempt0(RUNG2)["router_facts"]
    assert recorded["exit_code"] == 3 and recorded["strict_sizes_violated"] is True
    assert [r["delivered_mm"] for r in recorded["delivered_below_requested"]] == [0.326] * 4

    attempt = _run_one(tmp_path, RUNG2, "pic_programmer.kicad_pcb", recorded)["attempts"][0]
    check = attempt["router_facts"]["under_width_check"]
    # The raw board check read 571 added pieces of copper and found none under width.
    assert check["ran"] is True and check["under_width"] == 0 and check["checked"] == 571
    assert attempt["router_facts"]["delivered_below_requested"] == []
    # KRT's four ledger rows stay in the audit trail.
    assert len(attempt["router_facts"]["router_reported_below_requested"]) == 4
    # Fixed by this ticket: the stale exit-3 flag no longer fails the attempt on its own.
    assert attempt["delivered_below_requested_policy_hit"] is False
    assert attempt["status"] == "PASS" and attempt["cause"] is None
    assert attempt["router_facts"]["strict_sizes_violated"] is False
    assert attempt["router_facts"]["strict_sizes_reported_by_router"] is True


def _facts(**overrides) -> dict:
    row = {"net_name": None, "kind": "track_width", "requested_mm": 0.5, "delivered_mm": 0.326, "site": "terminal neck"}
    facts = {"exit_code": 3, "strict_sizes_violated": True, "fab_tier_escalations": 0,
             "delivered_below_requested": [row]}
    facts.update(overrides)
    return facts


@pytest.mark.parametrize("facts", [
    _facts(delivered_below_requested=[]),  # exit 3 with nothing listed: unexplained, stays a hit
    _facts(fab_tier_escalations=2),  # a tier descent the raw width check does not judge
    _facts(fab_tier_escalations=None),  # unknown is never evidence of none
    _facts(delivered_below_requested=[{"kind": "clearance", "requested_mm": 0.28, "delivered_mm": 0.25}]),
    _facts(delivered_below_requested=[{"kind": "track_width", "requested_mm": 0.5, "delivered_mm": 0.326},
                                      {"kind": "annular", "requested_mm": 0.1, "delivered_mm": 0.05}]),
    _facts(exit_code=0),  # the flag did not come from `--strict-sizes` exiting 3
])
def test_the_exit3_flag_stays_a_policy_hit_unless_every_row_is_a_size_the_raw_check_judges(tmp_path, facts):
    attempt = _run_one(tmp_path, RUNG2, "pic_programmer.kicad_pcb", facts)["attempts"][0]
    assert attempt["router_facts"]["under_width_check"]["under_width"] == 0
    assert attempt["router_facts"]["strict_sizes_violated"] is True
    assert attempt["delivered_below_requested_policy_hit"] is True and attempt["status"] == "FAIL"


def test_a_raw_under_width_item_is_never_cleared(tmp_path):
    """Rung 1's board holds real under-width copper: the flag and the items both stand."""
    recorded = _attempt0(RUNG1)["router_facts"]
    attempt = _run_one(tmp_path, RUNG1, "rung1_usbc_attiny1616.kicad_pcb", recorded)["attempts"][0]
    assert attempt["router_facts"]["strict_sizes_violated"] is True
    assert attempt["delivered_below_requested_policy_hit"] is True and attempt["status"] == "FAIL"


# --- rung 1: what the raw board holds, per value ---------------------------------------------------


def test_rung1_under_width_is_real_copper_on_one_net_with_a_taper(tmp_path):
    recorded = _attempt0(RUNG1)["router_facts"]
    attempt = _run_one(tmp_path, RUNG1, "rung1_usbc_attiny1616.kicad_pcb", recorded)["attempts"][0]
    items = attempt["router_facts"]["delivered_below_requested"]
    assert {i["net"] for i in items} == {"+3V3"} and {i["netclass"] for i in items} == {"Power"}
    tracks = Counter(round(i["delivered_mm"], 4) for i in items if i["kind"] == "track")
    # 0.127 mm (the Default width KRT necks to), then KRT's stepped taper back to 0.4 mm.
    assert tracks == {0.127: 7, 0.1816: 4, 0.2362: 4, 0.2908: 4, 0.3454: 4}
    vias = [(i["dimension"], i["delivered_mm"], i["required_mm"]) for i in items if i["kind"] == "via"]
    assert sorted(vias) == [("diameter", 0.45, 0.6)] * 2 + [("drill", 0.2, 0.3)] * 2


def test_the_wrapper_keeps_the_ledger_site_of_every_row():
    from kicad_mcp.routers.kicad_routing_tools import parse_route_log

    summary = {"design_rules": {"narrowed": [
        {"net": 13, "net_name": None, "kind": "track_width", "requested": 0.5, "delivered": 0.326,
         "site": "terminal neck", "count": 1},
        {"net": 1, "net_name": None, "kind": "via_diameter", "requested": 0.6, "delivered": 0.45,
         "site": "multipoint via kept at the run size", "count": 2}]}}
    rows = parse_route_log("", json.dumps(summary))["delivered_below_requested"]
    assert [(r["kind"], r["site"]) for r in rows] == [
        ("track_width", "terminal neck"), ("via_diameter", "multipoint via kept at the run size")]
