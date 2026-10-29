"""Build ticket 36: both router backends are judged by the same under-width rule.

The check reads the graded board's raw S-expressions (track ``width``, via ``size`` and
``drill``) and compares every piece of copper the attempt added with the board's own net
classes (``.kicad_pro``) and board-wide minimums. It lives in one wrapper that
``rung_backends.make_backend`` puts around every backend; a router's own report is kept
for the audit trail and never replaces it.

The reproduction replays ticket 04's rung-1 Freerouting attempt 00
(committed as the small fixture ``tests/fixtures/uw/r1fr``, see ``tests/fixtures/README.md``):
its input (``in/``) and the board it delivered (``out/``) are copied into ``tmp_path``;
nothing in the repository is written.
"""

from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path

import pytest
from kicad_tools.sexp import parse_file

from kicad_mcp import rung_runner

ROOT = Path(__file__).resolve().parents[1]
TICKET04 = Path(__file__).resolve().parent / "fixtures" / "uw" / "r1fr"
BOARD_NAME = "rung1_usbc_attiny1616.kicad_pcb"
PASS_GATE = {"verdict": "PASS", "unconnected": [], "violations": [], "warnings": [],
             "unstable": False, "incomplete_reasons": []}


def _ticket04_attempt0() -> dict:
    return json.loads((TICKET04 / "attempt0.json").read_text(encoding="utf-8"))


def _replay(facts: dict):
    """A backend that delivers exactly what Freerouting delivered in ticket 04's attempt 00."""
    routed = TICKET04 / "out" / BOARD_NAME

    def backend(board, params, timeout_s, log):
        shutil.copyfile(routed, board)
        return {"status": "ok", "cause": None, "facts": dict(facts)}

    return backend


def _run_one(tmp_path: Path, backend) -> dict:
    source = tmp_path / "input"
    shutil.copytree(TICKET04 / "in", source)
    plan = rung_runner.load_protocol()["backends"]["freerouting"]["plan"][:1]
    return rung_runner.run_rung(
        rung="ticket04-replay", backend_name="freerouting", board=source / BOARD_NAME,
        rule_files=[source / BOARD_NAME.replace(".kicad_pcb", ".kicad_pro")], plan=plan,
        backend=backend, gate=lambda board: dict(PASS_GATE), baseline={"rules": "pinned"},
        tool_versions={}, runs_root=tmp_path / "runs",
        repo_state=lambda: {"commit": "abc123", "dirty": False},
        protocol=rung_runner.load_protocol(), invariants=lambda board: {},
    )


# --- reproduction (acceptance item 1) ---------------------------------------------------------


def test_reproduction_ticket04_freerouting_under_width_was_never_flagged(tmp_path):
    """What ticket 04 recorded: Freerouting's facts carry no width information, so the
    policy counted nothing, and today's runner fed the same delivery and a PASS gate lets it
    pass. The power tracks on the delivered board are 0.1874-0.25 mm in a 0.4 mm class."""
    recorded = _ticket04_attempt0()
    assert recorded["delivered_below_requested_count"] == 0
    assert recorded["delivered_below_requested_policy_hit"] is False
    assert "delivered_below_requested" not in recorded["router_facts"]
    assert "strict_sizes_violated" not in recorded["router_facts"]

    report = _run_one(tmp_path, _replay(recorded["router_facts"]))
    attempt = report["attempts"][0]
    assert attempt["delivered_below_requested_policy_hit"] is False
    assert attempt["status"] == "PASS"  # the gap: nothing looked at the widths


# --- the shared check ---------------------------------------------------------------------------


def test_the_shared_check_fails_ticket04_freerouting_delivery_and_names_each_item(tmp_path):
    from kicad_mcp import rung_backends

    recorded = _ticket04_attempt0()
    report = _run_one(tmp_path, rung_backends.with_under_width_check(_replay(recorded["router_facts"])))
    attempt = report["attempts"][0]
    assert attempt["status"] == "FAIL"
    assert attempt["cause"] == "delivered-below-requested"
    assert attempt["delivered_below_requested_policy_hit"] is True

    items = attempt["router_facts"]["delivered_below_requested"]
    # Independent count, read from the raw (segment ...) nodes of the delivered board:
    # +3V3 4x0.1874 + 1x0.1998 + 15x0.25, /VBUS_F 2x0.1874 + 10x0.25, VBUS 6x0.25.
    assert Counter(i["net"] for i in items) == {"+3V3": 20, "/VBUS_F": 12, "VBUS": 6}
    assert attempt["delivered_below_requested_count"] == 38
    assert Counter(i["delivered_mm"] for i in items) == {0.25: 31, 0.1874: 6, 0.1998: 1}
    for item in items:
        assert item["kind"] == "track" and item["dimension"] == "width"
        assert item["netclass"] == "Power" and item["required_mm"] == 0.4
        assert item["layer"] in ("F.Cu", "B.Cu")
        assert item["required_from"] == "netclass Power track_width"
    assert Counter(i["layer"] for i in items) == {"F.Cu": 33, "B.Cu": 5}
    # Default-class tracks at 0.1874 mm are above Default's 0.127 mm: not under width.
    assert not any(i["netclass"] == "Default" for i in items)
    check = attempt["router_facts"]["under_width_check"]
    assert check["ran"] is True and check["under_width"] == 38
    assert check["checked"] == 90  # 82 segments + 8 vias the attempt added


# --- small boards built through kicad_tools on tmp_path copies ---------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def stress_board(tmp_path):
    """krt_stress_congestion with its .kicad_pro: Default class 0.25 mm track, 0.6/0.3 mm via,
    no board-wide minimums in the file (KiCad's defaults 0.2 / 0.5 / 0.3 apply)."""
    project = tmp_path / "proj"
    project.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_prl"):
        shutil.copy(FIXTURES / f"krt_stress_congestion{suffix}", project)
    return project / "krt_stress_congestion.kicad_pcb"


def _add_copper(board: Path, tracks=(), vias=()):
    """Add tracks ``(x, width, net)`` and vias ``(x, size, drill, net)`` through kicad_tools."""
    from kicad_tools.schema import PCB

    from kicad_mcp import write_layer

    pcb = PCB.load(board)
    for x, width, net in tracks:
        pcb.add_trace((x, 5.0), (x, 7.0), width=width, layer="B.Cu", net=net, dedupe=False)
    for x, size, drill, net in vias:
        pcb.add_via(x, 9.0, size=size, drill=drill, net=net, dedupe=False)
    write_layer.save_pcb(pcb, board)


def _router(tracks=(), vias=(), facts=None, status="ok", calls=None):
    def backend(board, params, timeout_s, log):
        if calls is not None:
            calls.append(board)
        _add_copper(Path(board), tracks, vias)
        return {"status": status, "cause": None, "facts": dict(facts or {})}

    return backend


def _set_project(board: Path, change):
    from kicad_tools.core import project_file

    from kicad_mcp import write_layer

    pro = board.with_suffix(".kicad_pro")
    data = project_file.load_project(pro)
    change(data)
    write_layer.save_project(data, pro)


def _check(board: Path, backend, tmp_path):
    from kicad_mcp import rung_backends

    return rung_backends.with_under_width_check(backend)(board, {}, 60.0, tmp_path / "a" / "router.log")


def test_tracks_and_vias_are_judged_by_their_net_class_and_input_copper_is_not(stress_board, tmp_path):
    _add_copper(stress_board, tracks=[(3.0, 0.1, "SIG1")])  # in the input: not this attempt's delivery

    def wide_class(data):
        ns = data["net_settings"]
        ns["classes"].append({**ns["classes"][0], "name": "Wide", "track_width": 0.5, "priority": 0})
        ns["netclass_patterns"] = [{"netclass": "Wide", "pattern": "SIG2"}]

    _set_project(stress_board, wide_class)
    outcome = _check(stress_board, _router(
        tracks=[(4.0, 0.25, "SIG1"), (5.0, 0.22, "SIG1"), (6.0, 0.3, "SIG2")],
        vias=[(7.0, 0.6, 0.3, "SIG1"), (8.0, 0.5, 0.2, "SIG3")]), tmp_path)

    assert outcome["status"] == "ok"
    items = outcome["facts"]["delivered_below_requested"]
    summary = sorted((i["net"], i["kind"], i["dimension"], i["delivered_mm"], i["required_mm"],
                      i["required_from"]) for i in items)
    assert summary == [
        ("SIG1", "track", "width", 0.22, 0.25, "netclass Default track_width"),
        ("SIG2", "track", "width", 0.3, 0.5, "netclass Wide track_width"),
        ("SIG3", "via", "diameter", 0.5, 0.6, "netclass Default via_diameter"),
        ("SIG3", "via", "drill", 0.2, 0.3, "netclass Default via_drill"),
    ]
    # Positions are the raw (at ...)/(start ...)/(end ...) tokens. kicad_tools' add_* offset
    # the given coordinates by the board origin, so compare with the saved file's own nodes.
    raw = parse_file(str(stress_board))
    raw_via = next(c for c in raw.children if c.name == "via" and float(c.find("drill").values[0]) == 0.2)
    raw_seg = next(c for c in raw.children if c.name == "segment" and float(c.find("width").values[0]) == 0.22)
    via = next(i for i in items if i["kind"] == "via")
    assert via["layer"] == "F.Cu-B.Cu" and via["at"] == [float(v) for v in raw_via.find("at").values[:2]]
    track = next(i for i in items if i["net"] == "SIG1")
    assert track["layer"] == "B.Cu"
    assert track["start"] == [float(v) for v in raw_seg.find("start").values[:2]]
    assert track["end"] == [float(v) for v in raw_seg.find("end").values[:2]]
    assert track["end"][1] - track["start"][1] == pytest.approx(2.0)
    assert outcome["facts"]["under_width_check"]["checked"] == 5  # the 0.1 mm input track is not


def test_a_board_wide_minimum_above_the_class_raises_the_requirement(stress_board, tmp_path):
    _set_project(stress_board, lambda d: d["board"]["design_settings"]["rules"].update(min_track_width=0.3))
    outcome = _check(stress_board, _router(tracks=[(4.0, 0.28, "SIG1"), (5.0, 0.3, "SIG1")]), tmp_path)
    items = outcome["facts"]["delivered_below_requested"]
    assert [(i["delivered_mm"], i["required_mm"], i["required_from"]) for i in items] == [
        (0.28, 0.3, "rules.min_track_width")]


def test_composite_and_regex_net_classes_set_the_requirement_like_kicad(stress_board, tmp_path):
    """Review round 1, finding 1, on the review's own measurement (KiCad 10 pcbnew
    GetEffectiveNetClass on this fixture): A (first, no track or via values, ``SIG*``) and B
    (0.5 mm, 0.8/0.4 mm via, ``SIG1``) make SIG1's class "A,B" with B's values; ``VD[DX]`` is a
    regex that gives VDD class R (0.45 mm). Before the fix all three passed at Default's values.
    ``test_board_rules_netclass_oracle.py`` holds KiCad's own answers."""

    def classes(data):
        ns = data["net_settings"]
        default = {k: v for k, v in ns["classes"][0].items()
                   if k not in ("track_width", "via_diameter", "via_drill", "microvia_diameter", "microvia_drill")}
        ns["classes"] += [
            {**default, "name": "A", "priority": 0},
            {**ns["classes"][0], "name": "B", "priority": 1, "track_width": 0.5, "via_diameter": 0.8, "via_drill": 0.4},
            {**ns["classes"][0], "name": "R", "priority": 2, "track_width": 0.45},
        ]
        ns["netclass_patterns"] = [{"netclass": "A", "pattern": "SIG*"}, {"netclass": "B", "pattern": "SIG1"},
                                   {"netclass": "R", "pattern": "VD[DX]"}]

    _set_project(stress_board, classes)
    outcome = _check(stress_board, _router(tracks=[(4.0, 0.3, "SIG1"), (5.0, 0.3, "VDD"), (6.0, 0.3, "SIG2")],
                                           vias=[(7.0, 0.6, 0.3, "SIG1")]), tmp_path)
    summary = sorted((i["net"], i["netclass"], i["kind"], i["dimension"], i["delivered_mm"], i["required_mm"],
                      i["required_from"]) for i in outcome["facts"]["delivered_below_requested"])
    assert summary == [
        ("SIG1", "A,B", "track", "width", 0.3, 0.5, "netclass B track_width"),
        ("SIG1", "A,B", "via", "diameter", 0.6, 0.8, "netclass B via_diameter"),
        ("SIG1", "A,B", "via", "drill", 0.3, 0.4, "netclass B via_drill"),
        ("VDD", "R", "track", "width", 0.3, 0.45, "netclass R track_width"),
    ]  # SIG2 is "A,Default": Default's 0.25 mm, met


def test_an_under_width_arc_is_judged(stress_board, tmp_path):
    """Review round 1, finding 2: an arc's raw (width ...) is judged like a segment's."""
    from kicad_tools.sexp import SExp

    from kicad_mcp import write_layer

    def arc_router(board, params, timeout_s, log):
        _add_copper(Path(board), tracks=[(4.0, 0.2, "SIG1"), (5.0, 0.25, "SIG1")])
        root = write_layer.load(board)
        segments = [c for c in root.children if c.name == "segment"]
        for segment in segments:  # each becomes an arc through the same two ends
            start = [float(v) for v in segment.find("start").values[:2]]
            end = [float(v) for v in segment.find("end").values[:2]]
            mid = SExp.list("mid", write_layer.number(start[0] + 0.5), write_layer.number((start[1] + end[1]) / 2))
            segment.name = "arc"
            segment.children.insert(segment.children.index(segment.find("start")) + 1, mid)
        write_layer.save(root, board)
        return {"status": "ok", "cause": None, "facts": {}}

    outcome = _check(stress_board, arc_router, tmp_path)
    raw = parse_file(str(stress_board))
    assert [c.name for c in raw.children if c.name in ("segment", "arc")] == ["arc", "arc"]
    items = outcome["facts"]["delivered_below_requested"]
    assert [(i["kind"], i["dimension"], i["delivered_mm"], i["required_mm"], i["required_from"]) for i in items] == [
        ("arc", "width", 0.2, 0.25, "netclass Default track_width")]
    assert outcome["facts"]["under_width_check"]["checked"] == 2


def test_a_microvia_is_judged_by_the_microvia_class_values(stress_board, tmp_path):
    """Review round 1, finding 2: a ``(via micro ...)`` is judged against the class's
    ``microvia_diameter``/``microvia_drill`` (Default 0.3/0.1 here), not the through-via pair
    (0.6/0.3): 0.35/0.12 passes as a microvia although it would fail as a via."""

    def micro_router(board, params, timeout_s, log):
        from kicad_tools.schema import PCB

        from kicad_mcp import write_layer

        pcb = PCB.load(board)
        pcb.add_via(7.0, 9.0, size=0.25, drill=0.08, net="SIG1", dedupe=False, via_type="micro")
        pcb.add_via(8.0, 9.0, size=0.35, drill=0.12, net="SIG1", dedupe=False, via_type="micro")
        write_layer.save_pcb(pcb, board)
        return {"status": "ok", "cause": None, "facts": {}}

    outcome = _check(stress_board, micro_router, tmp_path)
    raw = parse_file(str(stress_board))
    assert sum(1 for c in raw.children if c.name == "via" and "micro" in [str(v) for v in c.values]) == 2
    items = outcome["facts"]["delivered_below_requested"]
    assert [(i["kind"], i["dimension"], i["delivered_mm"], i["required_mm"], i["required_from"]) for i in items] == [
        ("microvia", "diameter", 0.25, 0.3, "netclass Default microvia_diameter"),
        ("microvia", "drill", 0.08, 0.1, "netclass Default microvia_drill"),
    ]


def test_a_board_gone_after_the_backend_fails_an_ok_attempt(stress_board, tmp_path):
    """Fail closed, as the wrapper's docstring says: no board to judge is never a pass."""

    def vanishing_router(board, params, timeout_s, log):
        Path(board).unlink()
        return {"status": "ok", "cause": None, "facts": {}}

    outcome = _check(stress_board, vanishing_router, tmp_path)
    assert outcome["status"] == "failure" and outcome["cause"] == "router-failure"
    assert outcome["facts"]["under_width_check"] == {"ran": False, "error": "no board after the backend ran"}
    assert "no board after the backend ran" in outcome["facts"]["error"]
    assert "delivered_below_requested" not in outcome["facts"]


def test_without_a_project_file_the_attempt_is_refused_before_the_router_runs(stress_board, tmp_path):
    stress_board.with_suffix(".kicad_pro").unlink()
    calls = []
    outcome = _check(stress_board, _router(tracks=[(4.0, 0.1, "SIG1")], calls=calls), tmp_path)
    assert calls == []
    assert outcome["status"] == "failure" and outcome["cause"] == "input-refused"
    assert outcome["facts"]["under_width_check"]["ran"] is False
    assert "kicad_pro" in outcome["facts"]["error"]


def _pwr_case_insensitive(data):
    ns = data["net_settings"]
    ns["classes"].append({**ns["classes"][0], "name": "Pwr", "priority": 0, "track_width": 0.41})
    ns["netclass_patterns"] = [{"netclass": "Pwr", "pattern": "(?i)vdd"}]


def test_a_pattern_python_and_kicad_read_differently_refuses_the_attempt(stress_board, tmp_path):
    """Delta review 1, finding 1, end to end: KiCad 10 puts VDD in Pwr (0.41 mm) through
    ``(?i)vdd``; Python's ``re`` does not, so a 0.3 mm VDD track passed with zero items.
    Such a pattern now refuses the attempt before the router runs: never ``ok``."""
    _set_project(stress_board, _pwr_case_insensitive)
    calls = []
    outcome = _check(stress_board, _router(tracks=[(4.0, 0.3, "VDD")], calls=calls), tmp_path)
    assert outcome["status"] != "ok"
    assert calls == []
    assert outcome["status"] == "failure" and outcome["cause"] == "input-refused"
    assert outcome["facts"]["under_width_check"]["ran"] is False
    assert "'Pwr'" in outcome["facts"]["error"] and "'(?i)vdd'" in outcome["facts"]["error"]


def test_a_divergent_pattern_after_a_plain_one_still_refuses_the_attempt(stress_board, tmp_path):
    """Delta review 2, finding 5: every pattern is checked, not only the first one."""

    def plain_then_divergent(data):
        _pwr_case_insensitive(data)
        ns = data["net_settings"]
        ns["classes"].append({**ns["classes"][0], "name": "Sig", "priority": 1, "track_width": 0.25})
        ns["netclass_patterns"].insert(0, {"netclass": "Sig", "pattern": "GND"})

    _set_project(stress_board, plain_then_divergent)
    patterns = [(p["netclass"], p["pattern"]) for p in _project_of(stress_board)["net_settings"]["netclass_patterns"]]
    assert patterns == [("Sig", "GND"), ("Pwr", "(?i)vdd")]
    calls = []
    outcome = _check(stress_board, _router(tracks=[(4.0, 0.3, "VDD")], calls=calls), tmp_path)
    assert calls == []
    assert outcome["status"] == "failure" and outcome["cause"] == "input-refused"
    assert "'Pwr'" in outcome["facts"]["error"] and "'(?i)vdd'" in outcome["facts"]["error"]


def test_a_net_kicad_would_class_through_its_bus_members_refuses_the_attempt(stress_board, tmp_path, monkeypatch):
    """Round 3: a board net with braces that no pattern matches by its whole name, while a
    pattern matches the text inside its braces, is classed by KiCad through its members; its
    requirement is unknown here, so the attempt is refused before the router runs. Rung 2's
    ``/VPP{slash}MCLR`` under patterns that match no member is judged as usual."""
    from kicad_mcp import pour_step

    real = pour_step.board_net_names
    monkeypatch.setattr(pour_step, "board_net_names", lambda path: real(path) | {"Q{VDD}", "/VPP{slash}MCLR"})

    def member_pattern(data):
        ns = data["net_settings"]
        ns["classes"].append({**ns["classes"][0], "name": "Pwr", "priority": 0, "track_width": 0.41})
        ns["netclass_patterns"] = [{"netclass": "Pwr", "pattern": "VDD"}]

    _set_project(stress_board, member_pattern)
    calls = []
    outcome = _check(stress_board, _router(tracks=[(4.0, 0.3, "SIG1")], calls=calls), tmp_path)
    assert calls == []
    assert outcome["status"] == "failure" and outcome["cause"] == "input-refused"
    assert "'Q{VDD}'" in outcome["facts"]["error"] and "'Pwr'" in outcome["facts"]["error"]

    monkeypatch.setattr(pour_step, "board_net_names", lambda path: real(path) | {"/VPP{slash}MCLR"})
    calls = []
    outcome = _check(stress_board, _router(tracks=[(4.0, 0.3, "SIG1")], calls=calls), tmp_path / "again")
    assert len(calls) == 1 and outcome["facts"]["under_width_check"]["ran"] is True


def test_a_bus_vector_net_refuses_the_attempt(stress_board, tmp_path, monkeypatch):
    """Round 5 (delta review 4, finding 1): KiCad classes ``D[0..1]`` through its expanded
    members D0 and D1 (``D?`` gives it the class); its requirement is unknown here, so the
    attempt is refused before the router runs instead of judging it as Default."""
    from kicad_mcp import pour_step

    real = pour_step.board_net_names
    monkeypatch.setattr(pour_step, "board_net_names", lambda path: real(path) | {"D[0..1]"})

    def vector_pattern(data):
        ns = data["net_settings"]
        ns["classes"].append({**ns["classes"][0], "name": "Pwr", "priority": 0, "track_width": 0.41})
        ns["netclass_patterns"] = [{"netclass": "Pwr", "pattern": "D?"}]

    _set_project(stress_board, vector_pattern)
    calls = []
    outcome = _check(stress_board, _router(tracks=[(4.0, 0.3, "SIG1")], calls=calls), tmp_path)
    assert calls == []
    assert outcome["status"] == "failure" and outcome["cause"] == "input-refused"
    assert "'D[0..1]'" in outcome["facts"]["error"] and "[a..b]" in outcome["facts"]["error"]


def _project_of(board: Path) -> dict:
    from kicad_mcp import board_rules

    return board_rules._read_project(board.with_suffix(".kicad_pro"))


def test_under_width_items_raises_its_own_error_on_such_a_pattern(stress_board):
    from kicad_mcp import board_rules, rung_backends

    _set_project(stress_board, _pwr_case_insensitive)
    project = board_rules._read_project(stress_board.with_suffix(".kicad_pro"))
    with pytest.raises(rung_backends.UnderWidthCheckError, match="'Pwr'"):
        rung_backends.under_width_items(stress_board, Counter(), project)


def test_a_check_that_cannot_read_the_delivery_fails_an_ok_attempt(stress_board, tmp_path):
    """Fail closed: copper on a net the board does not define cannot be judged."""
    from kicad_tools.schema import PCB

    from kicad_mcp import write_layer

    def bad_router(board, params, timeout_s, log):
        _add_copper(Path(board), tracks=[(4.0, 0.25, "SIG1")])
        pcb = PCB.load(board)
        segment = next(c for c in pcb._sexp.children if c.name == "segment")
        segment.find("net").set_value(0, 999)  # low-level node write on a tmp_path copy
        write_layer.save_pcb(pcb, board)
        return {"status": "ok", "cause": None, "facts": {}}

    outcome = _check(stress_board, bad_router, tmp_path)
    assert outcome["status"] == "failure" and outcome["cause"] == "router-failure"
    assert outcome["facts"]["under_width_check"]["ran"] is False
    assert "under-width check could not run" in outcome["facts"]["error"]
    assert "delivered_below_requested" not in outcome["facts"]


def test_a_routers_own_report_is_kept_for_the_audit_but_never_replaces_the_check(stress_board, tmp_path):
    reported = [{"net_name": "SIG1", "kind": "track", "requested_mm": 0.25, "delivered_mm": 0.2}]
    outcome = _check(stress_board, _router(tracks=[(4.0, 0.25, "SIG1")], facts={
        "delivered_below_requested": reported, "strict_sizes_violated": True}), tmp_path)
    assert outcome["facts"]["router_reported_below_requested"] == reported
    assert outcome["facts"]["delivered_below_requested"] == []
    assert outcome["facts"]["strict_sizes_violated"] is True  # KRT's exit-3 flag is untouched


def test_krt_strict_sizes_flag_still_fails_the_attempt_through_the_runner(stress_board, tmp_path):
    """KRT's existing behaviour: exit 3 alone is a policy hit, with or without the raw check."""
    from kicad_mcp import rung_backends

    backend = rung_backends.with_under_width_check(_router(facts={
        "strict_sizes_violated": True, "delivered_below_requested": []}))
    report = rung_runner.run_rung(
        rung="fixture", backend_name="krt", board=stress_board,
        rule_files=[stress_board.with_suffix(".kicad_pro")],
        plan=rung_runner.load_protocol()["backends"]["krt"]["plan"][:1], backend=backend,
        gate=lambda board: dict(PASS_GATE), baseline={"rules": "pinned"}, tool_versions={},
        runs_root=tmp_path / "runs", repo_state=lambda: {"commit": "abc123", "dirty": False},
        protocol=rung_runner.load_protocol(), invariants=lambda board: {},
    )
    attempt = report["attempts"][0]
    assert attempt["status"] == "FAIL" and attempt["cause"] == "delivered-below-requested"
    assert attempt["delivered_below_requested_count"] == 0


# --- one place for every backend -----------------------------------------------------------------


@pytest.mark.parametrize("name", ["krt", "freerouting"])
def test_every_real_backend_is_built_behind_the_one_check(name):
    from kicad_mcp import rung_backends

    backend = rung_backends.make_backend(name)
    assert getattr(backend, "under_width_checked", False) is True
    assert backend.__wrapped__.__qualname__ == f"{name}_backend.<locals>.run"


def test_the_check_has_one_call_site_and_no_backend_copy():
    import ast

    src = ROOT / "src" / "kicad_mcp"
    callers = []
    for path in sorted(src.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for node in ast.walk(func):
                if isinstance(node, ast.Call):
                    target = node.func
                    called = target.attr if isinstance(target, ast.Attribute) else getattr(target, "id", None)
                    if called == "under_width_items":
                        callers.append((path.name, func.name))
    assert sorted(set(callers)) == [("rung_backends.py", "run"), ("rung_backends.py", "with_under_width_check")]
    assert len(callers) == 2  # the same call seen from the closure and from its enclosing function


@pytest.mark.parametrize("name", ["krt", "freerouting"])
def test_a_measurement_without_an_injected_backend_judges_every_attempt(name, tmp_path, monkeypatch):
    """autonomy_run hands the runner ``make_backend``'s result: a delivery below its net
    class fails every attempt, whichever router delivered it, and the CLI summary names
    each item."""
    from kicad_mcp import autonomy_run, rung_backends

    from test_autonomy_run_review import OK_INFO, _drc
    from test_input_prep import _build_fixture

    board = _build_fixture(tmp_path / "proj")
    monkeypatch.setitem(rung_backends._BACKEND_FACTORIES, name,
                        lambda: _router(tracks=[(4.0, 0.15, "GND")]))
    outcome = autonomy_run.run_measurement(
        rung="fixture", backend_name=name, board=board, runs_root=tmp_path / "runs",
        parity="required", drc=_drc, availability=OK_INFO)
    attempts = outcome["report"]["attempts"]
    assert [a["cause"] for a in attempts] == ["delivered-below-requested"] * 5
    assert [a["status"] for a in attempts] == ["FAIL"] * 5
    summary = autonomy_run._summary(outcome, "fixture", name)
    assert summary["attempts_delivered_below_requested"] == 5
    assert summary["under_width"] == [
        {"attempt": i, "items": ["GND B.Cu track width 0.15 mm < 0.25 mm (netclass Default track_width)"]}
        for i in range(5)
    ]


def test_rules_a_router_rewrites_during_the_attempt_do_not_lower_the_requirement(stress_board, tmp_path):
    def relaxing_router(board, params, timeout_s, log):
        _set_project(Path(board), lambda d: d["net_settings"]["classes"][0].update(track_width=0.1))
        _add_copper(Path(board), tracks=[(4.0, 0.15, "SIG1")])
        return {"status": "ok", "cause": None, "facts": {}}

    outcome = _check(stress_board, relaxing_router, tmp_path)
    items = outcome["facts"]["delivered_below_requested"]
    assert [(i["delivered_mm"], i["required_mm"]) for i in items] == [(0.15, 0.25)]
