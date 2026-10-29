"""The protocol file is the single source of the protocol constants and attempt plans.

Expected values are literals from the closed protocol ticket
(pivot-ladder ticket 6): N = 5, 30 min per rung and
backend, 300 s router cap, backends never pooled.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_mcp import rung_runner

from test_rung_runner import PASS, Clock, project  # noqa: F401  (fixture reuse)


def test_protocol_file_declares_the_ticket_constants():
    protocol = rung_runner.load_protocol()
    assert protocol["protocol_version"] == "8"
    assert protocol["attempts"] == 5
    assert protocol["budget_s"] == 1800
    assert protocol["attempt_timeout_s"] == 300
    assert set(protocol["backends"]) == {"krt", "freerouting"}
    for name in ("krt", "freerouting"):
        assert len(protocol["backends"][name]["plan"]) == 5


def test_runner_constants_come_from_the_protocol_file():
    protocol = rung_runner.load_protocol()
    assert rung_runner.PROTOCOL_VERSION == protocol["protocol_version"]
    assert rung_runner.EXPECTED_ATTEMPTS == protocol["attempts"]
    assert rung_runner.DEFAULT_BUDGET_S == protocol["budget_s"]
    assert rung_runner.DEFAULT_ATTEMPT_TIMEOUT_S == protocol["attempt_timeout_s"]


def test_every_planned_lever_is_a_predeclared_discrete_value():
    protocol = rung_runner.load_protocol()
    for name, spec in protocol["backends"].items():
        vocabulary = spec["levers"]
        for entry in spec["plan"]:
            assert rung_runner.plan_problems(entry, vocabulary) == []
            assert set(entry["parameters"]) <= set(vocabulary)


def test_a_lever_outside_the_table_is_rejected():
    vocabulary = rung_runner.load_protocol()["backends"]["krt"]["levers"]
    assert rung_runner.plan_problems(
        {"lever": "x", "parameters": {"clearance_mm": 0.05}}, vocabulary
    )  # rules are never levers
    assert rung_runner.plan_problems(
        {"lever": "x", "parameters": {"escalation": "fab"}}, vocabulary
    )  # fab escalation descends below the board's rules
    assert rung_runner.plan_problems(
        {"lever": "x", "parameters": {"grid_step_mm": 0.07}}, vocabulary
    )  # not one of the declared values
    assert rung_runner.plan_problems(
        {"lever": "x", "parameters": {"nets": ["GND"]}}, vocabulary
    )  # net scope is never a lever


def test_malformed_protocol_file_is_refused(tmp_path):
    bad = tmp_path / "p.json"
    bad.write_text(json.dumps({"protocol_version": "1"}))
    with pytest.raises(ValueError):
        rung_runner.load_protocol(bad)


def test_a_plan_that_differs_from_the_committed_table_is_unofficial(project, tmp_path):  # noqa: F811
    from test_rung_runner import make_run

    custom = [{"lever": "grid", "parameters": {"grid_step_mm": 0.05}} for _ in range(5)]
    report, _ = make_run(project, tmp_path, plan=custom)
    assert report["unofficial"] is True
    assert any("committed plan" in r for r in report["unofficial_reasons"])
    assert report["autonomous_pass"] is False


def test_the_committed_plan_run_is_official_and_manifest_pins_the_protocol(project, tmp_path):  # noqa: F811
    from test_rung_runner import load, make_run

    plan = rung_runner.load_protocol()["backends"]["krt"]["plan"]
    report, _ = make_run(project, tmp_path, plan=plan)
    manifest, _ = load(report)
    assert report["unofficial"] is False
    assert report["autonomous_pass"] is True
    assert manifest["protocol_sha256"] == rung_runner.protocol_sha256()
