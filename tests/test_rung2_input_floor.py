"""Rung-2 input floor (build ticket 07). Seams: the pure record builders of
evals/rung2/measure_input_floor.py (diff, declaration) and the artifacts the
measurement leaves on disk, checked independently."""
import importlib.util
import json
from pathlib import Path

import pytest

R2 = Path(__file__).parent.parent / "evals" / "rung2"


def _load():
    spec = importlib.util.spec_from_file_location("measure_input_floor", R2 / "measure_input_floor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _v(kind, desc, x, y, sev="error"):
    return {"type": kind, "severity": sev, "description": kind, "items": [{"description": desc, "pos": {"x": x, "y": y}}]}


def _graded(violations, unconnected=(), warnings=()):
    return {"verdict": "FAIL", "violations": list(violations), "unconnected": list(unconnected),
            "warnings": list(warnings), "unstable": False, "list_complete": True,
            "incomplete_reasons": [], "runs": 3}


def test_diff_reports_cleared_created_and_persisting_by_signature():
    a, b, c = _v("silk_overlap", "A", 1, 1), _v("silk_overlap", "B", 2, 2), _v("clearance", "C", 3, 3)
    mod = _load()
    d = mod.diff_gate_results(_graded([a, b]), _graded([b, c]))
    assert [v["items"][0]["description"] for v in d["cleared"]] == ["A"]
    assert [v["items"][0]["description"] for v in d["created"]] == ["C"]
    assert [v["items"][0]["description"] for v in d["persisting"]] == ["B"]


def test_diff_is_insensitive_to_uuid_noise():
    a = _v("silk_overlap", "A", 1, 1)
    a2 = json.loads(json.dumps(a))
    a2["items"][0]["uuid"] = "different-runtime-uuid"
    d = _load().diff_gate_results(_graded([a]), _graded([a2]))
    assert d["cleared"] == [] and d["created"] == [] and len(d["persisting"]) == 1


def test_floor_declaration_carries_complete_list_and_no_waiver():
    vs = [_v("silk_overlap", f"T{i}", i, i) for i in range(7)]
    decl = _load().floor_declaration(_graded(vs), source={"commit": "x"}, reason="why")
    assert decl["input_fail_floor"] is True
    assert len(decl["violations"]) == 7
    assert decl["waived"] == [] and decl["rules_changed"] is False


def test_floor_declaration_is_false_when_floor_is_zero():
    decl = _load().floor_declaration(_graded([]), source={}, reason="r")
    assert decl["input_fail_floor"] is False and decl["violations"] == []


def test_choose_floor_never_adopts_a_fix_that_adds_errors():
    mod = _load()
    prepared = _graded([_v("silk_overlap", "A", 1, 1)])
    worse = _graded([_v("silk_overlap", "A", 1, 1), _v("silk_overlap", "B", 2, 2)])
    board, graded, _ = mod.choose_floor(prepared, worse)
    assert board == "prepared" and graded is prepared


def test_choose_floor_adopts_a_fix_that_reaches_zero():
    mod = _load()
    board, graded, _ = mod.choose_floor(_graded([_v("silk_overlap", "A", 1, 1)]), _graded([]))
    assert board == "beautified" and graded["violations"] == []


def test_recorded_results_are_consistent():
    path = R2 / "input_floor_result.json"
    if not path.exists():
        pytest.fail("missing evals/rung2/input_floor_result.json (run measure_input_floor.py)")
    r = json.loads(path.read_text(encoding="utf-8"))
    # baseline pinned before any fix attempt, from the prepared board
    assert r["steps"][0]["name"] == "stripped_prepared_baseline"
    names = [s["name"] for s in r["steps"]]
    assert names.index("stripped_prepared_baseline") < names.index("beautifier_after")
    for s in r["steps"]:
        if "gate" in s:
            assert s["gate"]["list_complete"] is True
            assert s["gate"]["unstable"] is False
    d = r["beautifier_diff"]
    before = {json.dumps(v, sort_keys=True) for v in r["steps"][0]["gate"]["violations"]}
    assert len(d["cleared"]) + len(d["persisting"]) == len(before)
    final = r["decision"]
    assert final["input_fail_floor"] == bool(final["floor_violations"])
    assert final["rules_changed"] is False
    # the declaration on disk equals the decision
    decl = json.loads((R2 / "input_fail_floor.json").read_text(encoding="utf-8"))
    assert decl["input_fail_floor"] == final["input_fail_floor"]
    assert len(decl["violations"]) == len(final["floor_violations"])
