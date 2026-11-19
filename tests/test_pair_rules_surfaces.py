"""The pair-rule report reaches every caller of the Freerouting export (issue 212, review round 1, B1).

Freerouting is replaced at its process boundary (an empty session); KiCad's export and import stay real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from boardproof import rung_backends
from boardproof.toolsets import routing as routing_toolset
from tests.test_dsn_class_class import _golden_with_pair
from tests.test_pcbnew_specctra import _session, _tools, needs_kicad_python


def _fake_freerouting(monkeypatch) -> list[str]:
    seen: list[str] = []

    def fake(dsn, ses, **kwargs):
        seen.append(Path(dsn).read_text(encoding="utf-8"))
        Path(ses).write_text(_session(), encoding="utf-8")
        return {"self_reported_score": None, "self_reported_unrouted": None}

    monkeypatch.setattr(routing_toolset, "run_freerouting", fake)
    return seen


def _assert_reported(pair_rules, note) -> None:
    assert [r["dsn_classes"] for r in pair_rules["applied_pair_rules"]] == [["Gnd", "Sig"]]
    assert pair_rules["pairs_not_applied"] == [] and pair_rules["problems"] == []
    assert "unrouted nets can follow" in note and "no evidence" in note


@needs_kicad_python
@pytest.mark.parametrize("tool", ["propose_route_with_freerouting", "propose_route_with_retry"])
def test_a_route_result_lists_the_applied_pair_rules_and_says_unrouted_nets_can_follow(tool, tmp_path, monkeypatch):
    pcb = _golden_with_pair(tmp_path)
    seen = _fake_freerouting(monkeypatch)
    result = _tools()[tool](str(pcb))
    assert result["status"] == "pending_confirmation", result
    assert "(class_class" in seen[0]  # the router was handed the rule
    _assert_reported(result["pair_rules"], result["pair_rules_note"])


@needs_kicad_python
def test_the_rung_backend_facts_carry_the_pair_rules(tmp_path, monkeypatch):
    pcb = _golden_with_pair(tmp_path)
    _fake_freerouting(monkeypatch)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    outcome = rung_backends._freerouting_round_trip(
        pcb, {"max_passes": 1}, 60, scratch, tmp_path / "work")
    assert outcome["status"] == "ok", outcome
    _assert_reported(outcome["facts"]["pair_rules"], outcome["facts"]["pair_rules_note"])
    assert "(class_class" in (scratch / "board.dsn").read_text(encoding="utf-8")
