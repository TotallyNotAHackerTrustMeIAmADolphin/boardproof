"""Issue 263: the KiCadRoutingTools minimum version (0.23.7), read from the checkout's VERSION file.
Too old, unknown or unreadable is unavailable, never a pass; the version read is reported."""

from __future__ import annotations

import json
import subprocess

import pytest

from boardproof.routers import kicad_placer_tools as placer
from boardproof.routers import kicad_routing_tools as krt
from tests.test_kicad_placer_tools import _fake_checkout


@pytest.mark.parametrize("version", ["0.23.7", "0.23.8", "0.24.0", "1.0.0", "0.100.0"])
def test_a_version_at_or_above_the_minimum_is_accepted(version):
    assert krt.min_version_problem(version) is None


@pytest.mark.parametrize("version", ["0.23.6", "0.22.9", "0.9.99", "0.23.1"])
def test_an_older_version_is_refused_naming_both(version):
    problem = krt.min_version_problem(version)
    assert problem and version in problem and "0.23.7" in problem, problem


@pytest.mark.parametrize("version", [None, "", "unknown", "0.23", "v0.23.8", "0.23.8-rc1"])
def test_an_unknown_or_unparsable_version_is_refused(version):
    assert krt.min_version_problem(version), version


def _stub_probe(monkeypatch):
    done = subprocess.CompletedProcess([], 0, json.dumps({"python": "p", "python_version": "3", "missing_packages": [],
                                                           "grid_router_error": None, "grid_router_version": "x"}), "")
    monkeypatch.setattr(krt.subprocess, "run", lambda *a, **k: done)


def test_availability_reports_the_version_it_read(tmp_path, monkeypatch):
    monkeypatch.setenv(krt.ENV_PATH, str(_fake_checkout(tmp_path / "krt", "0.23.8")))
    _stub_probe(monkeypatch)
    status = krt.check_availability()
    assert status["status"] == "complete" and status["router_version"] == "0.23.8", status


def test_availability_refuses_an_old_checkout_and_still_names_its_version(tmp_path, monkeypatch):
    monkeypatch.setenv(krt.ENV_PATH, str(_fake_checkout(tmp_path / "krt", "0.23.6")))
    _stub_probe(monkeypatch)
    status = krt.check_availability()
    assert status["status"] == "incomplete" and "0.23.6" in status["reason"] and "0.23.7" in status["reason"], status
    assert status["router_version"] == "0.23.6"


def test_availability_refuses_a_checkout_without_a_version_file(tmp_path, monkeypatch):
    root = _fake_checkout(tmp_path / "krt")
    (root / "VERSION").unlink()
    monkeypatch.setenv(krt.ENV_PATH, str(root))
    _stub_probe(monkeypatch)
    status = krt.check_availability()
    assert status["status"] == "incomplete" and "unknown" in status["reason"], status


def test_the_placer_refuses_an_old_checkout_with_the_minimum_named(tmp_path, monkeypatch):
    monkeypatch.setenv(krt.ENV_PATH, str(_fake_checkout(tmp_path / "krt", "0.23.6")))
    monkeypatch.setattr(placer, "_probe_imports", lambda python: [])
    problem = placer.placer_problem()
    assert problem and problem.startswith("unavailable") and "minimum 0.23.7" in problem, problem


def test_the_placer_refuses_a_checkout_without_a_version_file(tmp_path, monkeypatch):
    root = _fake_checkout(tmp_path / "krt")
    (root / "VERSION").unlink()
    monkeypatch.setenv(krt.ENV_PATH, str(root))
    monkeypatch.setattr(placer, "_probe_imports", lambda python: [])
    problem = placer.placer_problem()
    assert problem and "unknown" in problem and "0.23.7" in problem, problem


def test_the_placer_run_carries_the_version(tmp_path, monkeypatch):
    root = _fake_checkout(tmp_path / "krt", "0.23.8")
    monkeypatch.setenv(krt.ENV_PATH, str(root))

    def fake_run(args, **kw):
        if "place_simple.py" in args[1]:
            (tmp_path / "run" / "placed.kicad_pcb").touch()
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(placer, "_run_process", fake_run)
    run = placer.run_place_simple(tmp_path / "b.kicad_pcb", tmp_path / "run", ["R1"])
    assert run["router_version"] == "0.23.8"


@pytest.mark.parametrize("version", ["０.２３.８", "٠.٢٣.٨", "0.23.07", "0.023.8", "00.23.8", "0.23.8 ", "0.23.1000000"])
def test_only_plain_ascii_numbers_without_leading_zeros_are_read(version):
    assert krt.min_version_problem(version), version


def test_a_huge_number_gives_a_problem_sentence_not_an_exception():
    problem = krt.min_version_problem("0.23." + "9" * 5000)
    assert isinstance(problem, str) and problem


def test_the_routing_answer_carries_the_router_version_it_measured_with(tmp_path, monkeypatch):
    from tests.test_routing_toolset_surfaces_router_facts import _fake_runner, _golden_copy, _tools

    monkeypatch.setattr(krt, "check_availability", lambda **kw: {
        "status": "complete", "checkout": "c", "python": "p", "grid_router_version": "x", "router_version": "0.23.8"})
    monkeypatch.setattr(krt, "run_kicad_routing_tools", _fake_runner())
    pcb = _golden_copy(tmp_path)
    result = _tools()["propose_route_with_retry"](str(pcb), max_attempts=1, backend="kicad_routing_tools",
                                                  escalation="board")
    assert result["status"] == "pending_confirmation", result
    assert result["router_availability"]["router_version"] == "0.23.8"


def test_the_placement_answer_carries_the_router_version_that_ran(tmp_path, monkeypatch):
    from boardproof.sourcing.footprint_gen import generate_footprint
    from tests.test_placement_anchor_guards import _board, _fake_optimizer
    from tests.test_placement_anchor_guards import _tools as placement_tools

    chip = tmp_path / "1206.kicad_mod"
    assert generate_footprint("chip", str(chip), size="1206")["status"] == "ok"
    pcb = _board(tmp_path, {"1206": chip}, [("R1", "1206", 10, 10)])
    _fake_optimizer(monkeypatch, {"R1": (50.0, 20.0, 0.0)})
    plain = placer.run_place_simple
    monkeypatch.setattr(placer, "run_place_simple", lambda *a, **k: {**plain(*a, **k), "router_version": "0.23.8"})
    proposal = placement_tools()["propose_placement_optimization"](str(pcb))
    assert proposal["status"] == "pending_confirmation", proposal
    assert proposal["placer"]["router_version"] == "0.23.8"
