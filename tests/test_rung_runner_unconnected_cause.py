"""The attempt cause names the unconnected nets the gate lists (issue 2).

Recorded data: the first official rung-1 batch on protocol 6 had KRT fail 5 of 5 with the same six
unconnected items (``/CC1``, ``/CC2``, ``GND`` twice, ``VBUS`` twice) and an under-width delivery,
and the attempt cause read ``delivered-below-requested`` alone, which hid the six.
"""

from __future__ import annotations

from test_rung_runner import make_run
from test_rung_runner import project as shared_project

_REGISTERED = (shared_project,)  # the import registers the fixture under this module's name

_BELOW = [{"net_name": "SIG1", "kind": "track_width", "requested_mm": 0.25, "delivered_mm": 0.2}]
_NETS = ["/CC1", "/CC2", "GND", "GND", "VBUS", "VBUS"]
_FAIL_GATE = {
    "verdict": "FAIL",
    "unconnected": [{"net": n, "items": [f"pad {i}"]} for i, n in enumerate(_NETS)],
    "violations": [], "warnings": [], "unstable": False, "incomplete_reasons": [],
}


def _backend(facts):
    def backend(board, params, timeout_s, log):
        return {"status": "ok", "cause": None, "facts": facts}

    return backend


def test_unconnected_nets_are_the_cause_next_to_the_under_width_fact(request, tmp_path):
    report, _ = make_run(request.getfixturevalue("shared_project"), tmp_path, gate=lambda b: dict(_FAIL_GATE),
                         backend=_backend({"delivered_below_requested": _BELOW}))
    attempt = report["attempts"][0]
    assert attempt["status"] == "FAIL"
    assert attempt["cause"] == "unconnected-nets"
    assert attempt["unconnected_nets"] == ["/CC1", "/CC2", "GND", "VBUS"]
    # The under-width fact stays in the attempt, in addition.
    assert attempt["delivered_below_requested_policy_hit"] is True
    assert attempt["delivered_below_requested_count"] == 1
    for text in ("/CC1", "/CC2", "GND", "VBUS", "under-width"):
        assert text in attempt["detail"], (text, attempt["detail"])


def test_unconnected_nets_name_the_cause_without_any_under_width_fact(request, tmp_path):
    report, _ = make_run(request.getfixturevalue("shared_project"), tmp_path, gate=lambda b: dict(_FAIL_GATE))
    attempt = report["attempts"][0]
    assert attempt["cause"] == "unconnected-nets"
    assert attempt["unconnected_nets"] == ["/CC1", "/CC2", "GND", "VBUS"]
    assert "under-width" not in attempt["detail"]


def test_a_router_failure_keeps_its_own_cause_and_still_lists_the_nets(request, tmp_path):
    def backend(board, params, timeout_s, log):
        return {"status": "failure", "cause": "router-failure"}

    report, _ = make_run(request.getfixturevalue("shared_project"), tmp_path, gate=lambda b: dict(_FAIL_GATE), backend=backend)
    attempt = report["attempts"][0]
    assert attempt["cause"] == "router-failure"
    assert attempt["unconnected_nets"] == ["/CC1", "/CC2", "GND", "VBUS"]


def test_a_pass_has_no_unconnected_nets_and_keeps_a_none_cause(request, tmp_path):
    report, _ = make_run(request.getfixturevalue("shared_project"), tmp_path)
    attempt = report["attempts"][0]
    assert attempt["cause"] is None and attempt["unconnected_nets"] == []


def test_a_krt_parse_gap_in_the_router_facts_is_named_in_the_attempt_detail(request, tmp_path):
    gap = [{"code": "KRT_PARSE_GAP", "reference": "J1", "raw_pads": 4, "parsed_pads": 0}]
    report, _ = make_run(request.getfixturevalue("shared_project"), tmp_path, gate=lambda b: dict(_FAIL_GATE),
                         backend=_backend({"krt_parse_gap": gap}))
    attempt = report["attempts"][0]
    assert "KRT_PARSE_GAP" in attempt["detail"] and "J1" in attempt["detail"]
    assert attempt["cause"] == "unconnected-nets"  # reported next to the cause, never instead of it
