"""Tests for the board-rules setter (src/kicad_mcp/board_rules.py).

Contract: build ticket 18. Both places
KiCad reads rules live in the ``.kicad_pro`` JSON (``board.design_settings.rules``
for the constraints, ``net_settings`` for net classes and their assignments); the
``.kicad_pcb`` setup block holds neither. Fixtures are built through ``kicad_tools``
from the golden board; the real-DRC tests run ``kicad-cli`` and look at the actual
clearance figure in its report.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest

from kicad_mcp import write_layer
from kicad_mcp import board_rules
from kicad_mcp.kicad_cli import run_drc

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"

# Two parallel 0.25 mm tracks in free space of the golden board (outline 123.5..173.5 x 85..125),
# on nets 2 (Net-(D1-K)) and 3 (Net-(J2-Pin_2)): centre distance 0.50 -> copper gap 0.25 mm.
_TRACKS = [((130.0, 88.0), (140.0, 88.0), 2), ((130.0, 88.5), (140.0, 88.5), 3)]
_GAP_MM = 0.25

_EMPTY = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _read_pro(pcb: Path) -> dict:
    return json.loads(pcb.with_suffix(".kicad_pro").read_text(encoding="utf-8"))


def _write_pro(pcb: Path, data: dict) -> None:
    from kicad_mcp import write_layer

    write_layer.save_project(data, pcb.with_suffix(".kicad_pro"))


def _build(dest: Path, *, tracks: bool = True, constraints: dict | None = None) -> Path:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    shutil.copytree(GOLDEN_DIR, dest)
    pcb_path = dest / GOLDEN_NAME
    if tracks:
        board = PCB.load(pcb_path)
        for i, (start, end, net) in enumerate(_TRACKS):
            seg = SExp.list("segment")
            seg.append(SExp.list("start", *start))
            seg.append(SExp.list("end", *end))
            seg.append(SExp.list("width", 0.25))
            seg.append(SExp.list("layer", "F.Cu"))
            seg.append(SExp.list("net", net))
            seg.append(SExp.list("uuid", f"44444444-0000-4000-8000-00000000000{i}"))
            board._sexp.append(seg)
        write_layer.save_pcb(board, pcb_path)
    data = _read_pro(pcb_path)
    data["x_custom_unrelated"] = {"keep": [1, 2, {"deep": True}], "note": "must survive"}
    if constraints:
        data["board"]["design_settings"]["rules"].update(constraints)
    _write_pro(pcb_path, data)
    return pcb_path


@pytest.fixture
def board(tmp_path: Path) -> Path:
    return _build(tmp_path / "proj")


def _fake_drc(calls: list | None = None):
    def run(path, **_kwargs):
        if calls is not None:
            calls.append(Path(path))
        return _EMPTY

    return run


def _apply(board: Path, tmp_path: Path, name: str = "rules_rev", drc=None, **spec) -> dict:
    proposal = board_rules.propose_board_rules(board, output_dir=tmp_path / name, **spec)
    assert proposal["status"] == "proposed", proposal
    return board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=drc or _fake_drc()
    )


def _digest_tree(directory: Path) -> dict[str, str]:
    return {
        str(p.relative_to(directory)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(directory.rglob("*"))
        if p.is_file()
    }


def _leaf_diff(a, b, path="") -> list[str]:
    """Paths at which two JSON trees differ (lists compared as whole values per index)."""
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for key in sorted(set(a) | set(b)):
            if key not in a or key not in b:
                out.append(f"{path}/{key}")
            else:
                out += _leaf_diff(a[key], b[key], f"{path}/{key}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _leaf_diff(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [path]


def _clearance_violations(report: dict) -> list[dict]:
    return [v for v in report["violations"] if v["type"] == "clearance"]


# --- the two places KiCad reads rules --------------------------------------------


def test_constraints_set_in_the_project_and_visible_after_a_fresh_read(board: Path, tmp_path: Path) -> None:
    result = _apply(
        board,
        tmp_path,
        min_clearance=0.3,
        min_track_width=0.25,
        via_diameter=0.7,
        min_via_annular_width=0.15,
        min_through_hole_diameter=0.35,
        # the Default class must itself respect the new minimums (coherence check)
        netclasses=[{"name": "Default", "via_diameter": 0.8, "via_drill": 0.4, "clearance": 0.3}],
    )
    assert result["status"] == "applied", result
    rules = _read_pro(Path(result["output_pcb"]))["board"]["design_settings"]["rules"]
    assert rules["min_clearance"] == 0.3
    assert rules["min_track_width"] == 0.25
    assert rules["min_via_diameter"] == 0.7
    assert rules["min_via_annular_width"] == 0.15
    assert rules["min_through_hole_diameter"] == 0.35
    assert result["readback"]["constraints"]["min_clearance"] == 0.3


def test_named_netclass_and_assignment_written_and_read_back(board: Path, tmp_path: Path) -> None:
    result = _apply(
        board,
        tmp_path,
        netclasses=[
            {
                "name": "Power",
                "clearance": 0.3,
                "track_width": 0.5,
                "via_diameter": 0.8,
                "via_drill": 0.4,
                "nets": ["VCC", "GND"],
            }
        ],
    )
    assert result["status"] == "applied"
    settings = _read_pro(Path(result["output_pcb"]))["net_settings"]
    power = next(c for c in settings["classes"] if c["name"] == "Power")
    assert (power["clearance"], power["track_width"], power["via_diameter"], power["via_drill"]) == (
        0.3, 0.5, 0.8, 0.4,
    )
    assert {"netclass": "Power", "pattern": "VCC"} in settings["netclass_patterns"]
    assert {"netclass": "Power", "pattern": "GND"} in settings["netclass_patterns"]
    # a Default class (and its colours etc.) is still there untouched
    default = next(c for c in settings["classes"] if c["name"] == "Default")
    assert default == next(
        c for c in _read_pro(board)["net_settings"]["classes"] if c["name"] == "Default"
    )
    assert result["readback"]["assignments"] == {"VCC": "Power", "GND": "Power"}


def test_unrelated_project_keys_survive_the_round_trip(board: Path, tmp_path: Path) -> None:
    before = _read_pro(board)
    result = _apply(board, tmp_path, min_clearance=0.3, netclasses=[{"name": "P", "clearance": 0.3, "nets": ["VCC"]}])
    after = _read_pro(Path(result["output_pcb"]))
    assert after["x_custom_unrelated"] == before["x_custom_unrelated"]
    changed = _leaf_diff(before, after)
    assert changed, "nothing changed"
    allowed = ("/board/design_settings/rules/min_clearance", "/net_settings/")
    assert all(p.startswith(allowed) for p in changed), changed


def test_no_op_roundtrip_of_a_real_project_is_semantically_identical(tmp_path: Path) -> None:
    """The writer itself must not change anything it was not asked to change."""
    for name, src in {
        "golden": GOLDEN_DIR / "golden_attiny1616_board.kicad_pro",
        "rung1": Path(__file__).parent.parent / "evals" / "rung1" / "project" / "rung1_usbc_attiny1616.kicad_pro",
    }.items():
        out = tmp_path / f"{name}.kicad_pro"
        shutil.copyfile(src, out)
        board_rules._write_project(out, board_rules._read_project(out))
        assert json.loads(out.read_text(encoding="utf-8")) == json.loads(src.read_text(encoding="utf-8"))


# --- checkpoint: dry run, token, source untouched ---------------------------------


def test_propose_writes_nothing_and_confirm_leaves_the_source_untouched(board: Path, tmp_path: Path) -> None:
    before = _digest_tree(board.parent)
    out = tmp_path / "rev"
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=out)
    assert proposal["status"] == "proposed"
    assert proposal["revision_token"]
    assert not out.exists()
    assert _digest_tree(board.parent) == before
    assert any(c["key"] == "min_clearance" and c["after"] == 0.3 for c in proposal["changes"])

    result = board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["status"] == "applied"
    assert _digest_tree(board.parent) == before
    assert Path(result["output_pcb"]).is_file()
    # the board file itself is carried over byte for byte
    assert Path(result["output_pcb"]).read_bytes() == board.read_bytes()


def test_unknown_token_and_changed_source_are_refused(board: Path, tmp_path: Path) -> None:
    assert board_rules.confirm_board_rules(board, "nope", parity="required", drc=_fake_drc())["status"] == "error"
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    data = _read_pro(board)
    data["board"]["design_settings"]["rules"]["min_track_width"] = 0.19
    _write_pro(board, data)
    result = board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["status"] == "error"
    assert not (tmp_path / "rev").exists()


def test_nothing_to_do_when_values_already_match(board: Path, tmp_path: Path) -> None:
    current = _read_pro(board)["board"]["design_settings"]["rules"]["min_track_width"]
    proposal = board_rules.propose_board_rules(board, min_track_width=current, output_dir=tmp_path / "rev")
    assert proposal["status"] == "nothing_to_do"
    assert proposal["revision_token"] is None


def test_unknown_net_is_an_error_with_no_token(board: Path, tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(
        board, netclasses=[{"name": "P", "clearance": 0.3, "nets": ["NOT_A_NET"]}], output_dir=tmp_path / "rev"
    )
    assert proposal["status"] == "error"
    assert "NOT_A_NET" in proposal["reason"]
    assert proposal.get("revision_token") is None


def test_missing_project_file_is_an_error(board: Path, tmp_path: Path) -> None:
    board.with_suffix(".kicad_pro").unlink()
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    assert proposal["status"] == "error"


def test_output_dir_must_be_new_and_outside_the_source(board: Path, tmp_path: Path) -> None:
    inside = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=board.parent / "sub")
    assert inside["status"] == "error"
    same = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=board.parent)
    assert same["status"] == "error"


# --- loosening --------------------------------------------------------------------


def test_loosening_is_refused_without_the_flag_and_listed(tmp_path: Path) -> None:
    board = _build(tmp_path / "proj", constraints={"min_clearance": 0.3})
    proposal = board_rules.propose_board_rules(board, min_clearance=0.15, output_dir=tmp_path / "rev")
    assert proposal["status"] == "refused"
    assert proposal["revision_token"] is None
    assert proposal["loosened"] == [
        {"where": "constraints", "key": "min_clearance", "before": 0.3, "after": 0.15, "before_source": "file"}
    ]
    assert not (tmp_path / "rev").exists()


def test_loosening_with_the_flag_is_applied_and_reported(tmp_path: Path) -> None:
    board = _build(tmp_path / "proj", constraints={"min_clearance": 0.3})
    result = _apply(board, tmp_path, min_clearance=0.15, allow_loosen=True)
    assert result["status"] == "applied"
    assert result["loosened"] == [
        {"where": "constraints", "key": "min_clearance", "before": 0.3, "after": 0.15, "before_source": "file"}
    ]
    assert _read_pro(Path(result["output_pcb"]))["board"]["design_settings"]["rules"]["min_clearance"] == 0.15


def test_tightening_from_unset_zero_is_not_a_loosening(board: Path, tmp_path: Path) -> None:
    assert _read_pro(board)["board"]["design_settings"]["rules"]["min_clearance"] == 0.0
    result = _apply(board, tmp_path, min_clearance=0.2)
    assert result["loosened"] == []


def test_lowering_an_existing_netclass_value_is_a_loosening(board: Path, tmp_path: Path) -> None:
    first = _apply(board, tmp_path, name="r1", netclasses=[{"name": "Power", "clearance": 0.4, "nets": ["VCC"]}])
    revision = Path(first["output_pcb"])
    proposal = board_rules.propose_board_rules(
        revision, netclasses=[{"name": "Power", "clearance": 0.25}], output_dir=tmp_path / "r2"
    )
    assert proposal["status"] == "refused"
    assert proposal["loosened"][0]["where"] == "netclass:Power"
    assert proposal["loosened"][0]["key"] == "clearance"


def test_moving_a_net_to_a_looser_class_is_a_loosening(board: Path, tmp_path: Path) -> None:
    first = _apply(
        board,
        tmp_path,
        name="r1",
        netclasses=[
            {"name": "Tight", "clearance": 0.4, "nets": ["VCC"]},
            {"name": "Loose", "clearance": 0.15},
        ],
    )
    proposal = board_rules.propose_board_rules(
        Path(first["output_pcb"]), netclasses=[{"name": "Loose", "nets": ["VCC"]}], output_dir=tmp_path / "r2"
    )
    assert proposal["status"] == "refused"
    assert proposal["loosened"][0]["where"] == "assignment"
    assert proposal["loosened"][0]["key"] == "VCC"


# --- baseline pin -----------------------------------------------------------------


def test_baseline_of_the_new_revision_differs_and_is_reported(board: Path, tmp_path: Path) -> None:
    result = _apply(board, tmp_path, min_clearance=0.3, netclasses=[{"name": "Power", "clearance": 0.3, "nets": ["VCC"]}])
    assert result["baseline_changed"] is True
    assert result["baseline"] != result["baseline_before"]
    assert result["baseline"]["constraints"]["min_clearance"] == 0.3
    assert any(
        c["name"] == "Power" for c in result["baseline"]["net_settings"]["classes"]
    )
    assert "constraints" in result["baseline_diff"]
    assert "net_settings" in result["baseline_diff"]
    assert result["grading"]["certified"] is False


# --- failure paths ----------------------------------------------------------------


def _stray(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name != "proj")


def test_failure_while_writing_leaves_nothing_and_token_usable(board: Path, tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "rev"
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=out)
    with monkeypatch.context() as patch:

        def boom(*_a, **_k):
            raise RuntimeError("disk full")

        patch.setattr(board_rules, "_write_project", boom)
        failed = board_rules.confirm_board_rules(
            board, proposal["revision_token"], parity="required", drc=_fake_drc()
        )
    assert failed["status"] == "incomplete"
    assert not out.exists()
    assert _stray(tmp_path) == []
    retry = board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert retry["status"] == "applied"


def test_keyboard_interrupt_cleans_up(board: Path, tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "rev"
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=out)
    with monkeypatch.context() as patch:

        def interrupt(*_a, **_k):
            raise KeyboardInterrupt

        patch.setattr(board_rules, "_write_project", interrupt)
        with pytest.raises(KeyboardInterrupt):
            board_rules.confirm_board_rules(
                board, proposal["revision_token"], parity="required", drc=_fake_drc()
            )
    assert not out.exists()
    assert _stray(tmp_path) == []


def test_readback_mismatch_is_a_critical_failure_and_leaves_nothing(board: Path, tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "rev"
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=out)
    real_write = board_rules._write_project

    def corrupting(path, data):
        data = copy.deepcopy(data)
        data["board"]["design_settings"]["rules"]["min_clearance"] = 0.0  # a writer that loses the value
        real_write(path, data)

    monkeypatch.setattr(board_rules, "_write_project", corrupting)
    failed = board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert failed["status"] == "error"
    assert "CRITICAL" in failed["reason"]
    assert not out.exists()
    assert _stray(tmp_path) == []


def test_kicad_cli_missing_after_write_reports_incomplete_with_the_revision(board: Path, tmp_path: Path) -> None:
    from kicad_mcp.kicad_cli import KicadCliError

    def missing(path, **_k):
        raise KicadCliError("kicad-cli not found")

    result = _apply(board, tmp_path, min_clearance=0.3, drc=missing)
    assert result["status"] == "incomplete"
    assert Path(result["output_pcb"]).is_file()
    assert "readback" in result


# --- real kicad-cli ---------------------------------------------------------------


def _drc(pcb: Path) -> dict:
    return run_drc(pcb, refill_zones=False, schematic_parity=False)


def _between_tracks(report: dict) -> list[dict]:
    """Clearance violations between exactly the two test segments, by uuid (the report
    text is localised, so descriptions are never matched by words)."""
    mine = {f"44444444-0000-4000-8000-00000000000{i}" for i in range(2)}
    return [
        v
        for v in _clearance_violations(report)
        if {item.get("uuid") for item in v.get("items", [])} == mine
    ]


def _mm_figures(violation: dict) -> list[float]:
    return [float(m.replace(",", ".")) for m in re.findall(r"(\d+[.,]\d+) mm", violation["description"])]


def test_real_drc_uses_the_new_clearance_constraint(board: Path, tmp_path: Path) -> None:
    assert _between_tracks(_drc(board)) == []  # 0.25 mm gap passes the Default 0.2 class
    result = _apply(board, tmp_path, min_clearance=0.3, drc=run_drc)
    assert result["status"] == "applied"
    violations = _between_tracks(_drc(Path(result["output_pcb"])))
    assert len(violations) == 1
    assert _mm_figures(violations[0]) == [0.3, 0.25], violations[0]["description"]
    # the source is still graded against its own rules
    assert _between_tracks(_drc(board)) == []


def test_real_drc_uses_the_netclass_assignment(board: Path, tmp_path: Path) -> None:
    result = _apply(
        board, tmp_path, drc=run_drc, netclasses=[{"name": "Power", "clearance": 0.35, "nets": ["Net-(D1-K)"]}]
    )
    assert result["status"] == "applied"
    assert result["readback"]["assignments"] == {"Net-(D1-K)": "Power"}
    violations = _between_tracks(_drc(Path(result["output_pcb"])))
    assert len(violations) == 1
    assert _mm_figures(violations[0]) == [0.35, 0.25], violations[0]["description"]


# --- review round: custom rules, defaults, validation, coherence, priority ----------

RUNG1_PCB = Path(__file__).parent.parent / "evals" / "rung1" / "project" / "rung1_usbc_attiny1616.kicad_pcb"


def _add_dru(pcb: Path, text: str) -> Path:
    dru = pcb.with_suffix(".kicad_dru")
    dru.write_text(text, encoding="utf-8")  # scratch fixture in a tmp dir, never a real project file
    return dru


_DRU = """(version 1)
(rule "big_clearance"
  (constraint clearance (min 0.5))
  (condition "A.NetClass == 'Power'"))
(rule "fat_tracks"
  (constraint track_width (min 0.4)))
(rule "silk"
  (constraint silk_clearance (min 0.2)))
"""


def test_custom_rules_file_is_detected_and_its_constraints_listed(board: Path, tmp_path: Path) -> None:
    dru = _add_dru(board, _DRU)
    before = dru.read_bytes()
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    assert proposal["status"] == "proposed"
    assert proposal["custom_rules_file"] == dru.name
    listed = {(r["rule"], r["constraint"], r["overrides_key"]) for r in proposal["custom_rules"]}
    assert listed == {
        ("big_clearance", "clearance", "min_clearance"),
        ("fat_tracks", "track_width", "min_track_width"),
    }
    assert any("min_clearance" in w and dru.name in w for w in proposal["warnings"])
    result = board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["custom_rules_file"] == dru.name
    assert result["custom_rules"] == proposal["custom_rules"]
    assert dru.read_bytes() == before  # never edited
    assert Path(result["output_pcb"]).with_suffix(".kicad_dru").read_bytes() == before


def test_no_custom_rules_file_means_empty_fields(board: Path, tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    assert proposal["custom_rules_file"] is None
    assert proposal["custom_rules"] == []


def test_absent_keys_are_kicad_defaults_for_loosening(tmp_path: Path) -> None:
    rules = _read_pro(RUNG1_PCB)["board"]["design_settings"]["rules"]
    assert not {"min_via_diameter", "min_track_width", "min_through_hole_diameter"} & set(rules)
    for kwargs, key, default in [
        ({"via_diameter": 0.4}, "min_via_diameter", 0.5),
        ({"min_track_width": 0.1}, "min_track_width", 0.2),
        ({"min_through_hole_diameter": 0.2}, "min_through_hole_diameter", 0.3),
        ({"min_via_annular_width": 0.05}, "min_via_annular_width", 0.1),
    ]:
        proposal = board_rules.propose_board_rules(RUNG1_PCB, output_dir=tmp_path / "o", **kwargs)
        assert proposal["status"] == "refused", (kwargs, proposal)
        entry = proposal["loosened"][0]
        assert (entry["key"], entry["before"], entry["before_source"]) == (key, default, "kicad_default")


def test_a_value_above_the_absent_default_is_not_a_loosening(tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(RUNG1_PCB, via_diameter=0.6, output_dir=tmp_path / "o")
    assert proposal["status"] == "proposed"
    assert proposal["loosened"] == []


def test_default_table_matches_real_kicad_cli(tmp_path: Path) -> None:
    """The defaults are measured, not remembered: DRC with ``rules = {}`` names the limit applied."""
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    proj = tmp_path / "p"
    shutil.copytree(GOLDEN_DIR, proj)
    pcb = proj / GOLDEN_NAME
    brd = PCB.load(pcb)

    seg = SExp.list("segment")
    for part in (SExp.list("start", 130, 88), SExp.list("end", 140, 88), SExp.list("width", 0.1),
                 SExp.list("layer", "F.Cu"), SExp.list("net", 2),
                 SExp.list("uuid", "55555555-0000-4000-8000-000000000000")):
        seg.append(part)
    brd._sexp.append(seg)
    for i, (x, size, drill) in enumerate([(150, 0.4, 0.2), (160, 0.6, 0.45)]):
        via = SExp.list("via")
        for part in (SExp.list("at", x, 90), SExp.list("size", size), SExp.list("drill", drill),
                     SExp.list("layers", "F.Cu", "B.Cu"), SExp.list("net", 3),
                     SExp.list("uuid", f"66666666-0000-4000-8000-00000000000{i}")):
            via.append(part)
        brd._sexp.append(via)
    write_layer.save_pcb(brd, pcb)
    data = _read_pro(pcb)
    data["board"]["design_settings"]["rules"] = {}
    _write_pro(pcb, data)
    seen: dict[str, set[float]] = {}
    for v in _drc(pcb)["violations"]:
        if v["type"] in ("track_width", "via_diameter", "drill_out_of_range", "annular_width"):
            seen.setdefault(v["type"], set()).add(_mm_figures(v)[0])
    k = board_rules.KICAD_RULE_DEFAULTS
    assert seen["track_width"] == {k["min_track_width"]}
    assert seen["via_diameter"] == {k["min_via_diameter"]}
    assert seen["drill_out_of_range"] == {k["min_through_hole_diameter"]}
    assert seen["annular_width"] == {k["min_via_annular_width"]}


@pytest.mark.parametrize(
    "bad", [float("inf"), float("-inf"), float("nan"), 1000.0, 100.5, 0, -0.1, True, "0.2"]
)
def test_non_finite_or_absurd_numbers_never_reach_the_file(board: Path, tmp_path: Path, bad) -> None:
    for spec in (
        {"min_clearance": bad},
        {"netclasses": [{"name": "X", "clearance": bad}]},
        {"netclasses": [{"name": "X", "via_drill": bad}]},
    ):
        proposal = board_rules.propose_board_rules(board, output_dir=tmp_path / "rev", **spec)
        assert proposal["status"] == "error", (bad, spec, proposal)
        assert proposal.get("revision_token") is None
    assert not (tmp_path / "rev").exists()


def test_upper_bound_itself_is_accepted(board: Path, tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(board, min_clearance=100.0, output_dir=tmp_path / "rev")
    assert proposal["status"] == "proposed"


def test_unintended_change_anywhere_in_net_settings_fails_the_readback(
    board: Path, tmp_path: Path, monkeypatch
) -> None:
    out = tmp_path / "rev"
    proposal = board_rules.propose_board_rules(
        board, netclasses=[{"name": "Power", "clearance": 0.3, "nets": ["VCC"]}], output_dir=out
    )
    real_write = board_rules._write_project

    def corrupting(path, data):
        data = copy.deepcopy(data)
        default = next(c for c in data["net_settings"]["classes"] if c["name"] == "Default")
        default["clearance"] = 0.01  # inside /net_settings, unrelated to the request
        real_write(path, data)

    monkeypatch.setattr(board_rules, "_write_project", corrupting)
    failed = board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert failed["status"] == "error"
    assert "CRITICAL" in failed["reason"]
    assert not out.exists()


def test_unrelated_top_level_change_also_fails(board: Path, tmp_path: Path, monkeypatch) -> None:
    out = tmp_path / "rev"
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=out)
    real_write = board_rules._write_project

    def corrupting(path, data):
        data = copy.deepcopy(data)
        data["x_custom_unrelated"]["note"] = "tampered"
        real_write(path, data)

    monkeypatch.setattr(board_rules, "_write_project", corrupting)
    failed = board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert failed["status"] == "error"
    assert not out.exists()


_FINE = [{"name": "Fine", "track_width": 0.1, "clearance": 0.05}]


def test_class_values_below_the_global_minimums_are_refused(tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(
        RUNG1_PCB, min_track_width=0.3, netclasses=_FINE, output_dir=tmp_path / "o"
    )
    assert proposal["status"] == "refused"
    assert proposal["revision_token"] is None
    assert {
        "class": "Fine", "key": "track_width", "value": 0.1, "limit_key": "min_track_width", "limit": 0.3,
    } in proposal["incoherent"]
    allowed = board_rules.propose_board_rules(
        RUNG1_PCB, min_track_width=0.3, netclasses=_FINE, allow_incoherent=True, output_dir=tmp_path / "o"
    )
    assert allowed["status"] == "proposed"
    assert allowed["incoherent"]  # still listed


def test_via_and_hole_incoherence_is_refused_and_clearance_is_flagged_moot(tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(
        RUNG1_PCB,
        via_diameter=0.6,
        min_clearance=0.15,
        netclasses=[{"name": "V", "via_diameter": 0.55, "via_drill": 0.2, "clearance": 0.1}],
        output_dir=tmp_path / "o",
    )
    assert proposal["status"] == "refused"
    keys = {(i["class"], i["key"]) for i in proposal["incoherent"]}
    assert ("V", "via_diameter") in keys
    assert ("V", "via_drill") in keys  # below the 0.3 default board hole minimum
    assert ("V", "clearance") not in keys  # moot, not refused
    moot = proposal["moot"][0]
    assert (moot["class"], moot["key"], moot["value"], moot["limit"]) == ("V", "clearance", 0.1, 0.15)


def test_coherent_request_has_empty_lists(board: Path, tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(
        board,
        min_track_width=0.25,
        netclasses=[{"name": "Wide", "track_width": 0.5, "clearance": 0.3, "nets": ["VCC"]}],
        output_dir=tmp_path / "rev",
    )
    assert proposal["status"] == "proposed"
    assert proposal["incoherent"] == [] and proposal["moot"] == []


def test_priority_decides_overlapping_patterns(board: Path) -> None:
    from kicad_tools.core.project_file import get_netclass_definitions, get_netclass_patterns

    data = _read_pro(board)
    for name, prio in (("A", 0), ("B", 1)):
        cls = copy.deepcopy(next(c for c in get_netclass_definitions(data) if c["name"] == "Default"))
        cls["name"], cls["priority"] = name, prio
        get_netclass_definitions(data).append(cls)
    # the wildcard belongs to the higher-priority class A; the exact name to B
    get_netclass_patterns(data).extend(
        [{"netclass": "B", "pattern": "Net-(D1-K)"}, {"netclass": "A", "pattern": "Net-(*"}]
    )
    assert board_rules._effective_class(data, "Net-(D1-K)") == "A"
    classes = {c["name"]: c for c in get_netclass_definitions(data)}
    classes["A"]["priority"], classes["B"]["priority"] = 5, 0  # now B takes precedence
    assert board_rules._effective_class(data, "Net-(D1-K)") == "B"
    assert board_rules._effective_class(data, "Net-(J2-Pin_2)") == "A"
    assert board_rules._effective_class(data, "GND") == "Default"


def test_a_request_that_loses_to_a_higher_priority_pattern_is_an_error(board: Path, tmp_path: Path) -> None:
    first = _apply(board, tmp_path, name="r1", netclasses=[{"name": "Top", "priority": 0, "clearance": 0.3}])
    revision = Path(first["output_pcb"])
    data = _read_pro(revision)
    data["net_settings"]["netclass_patterns"].append({"netclass": "Top", "pattern": "Net-*"})
    _write_pro(revision, data)
    proposal = board_rules.propose_board_rules(
        revision,
        netclasses=[{"name": "Mine", "priority": 3, "clearance": 0.25, "nets": ["Net-(D1-K)"]}],
        output_dir=tmp_path / "r2",
    )
    assert proposal["status"] == "error"
    assert "Top" in proposal["reason"]


def test_real_drc_priority_picks_the_class(board: Path, tmp_path: Path) -> None:
    classes = [
        {"name": "Wide", "priority": 0, "clearance": 0.35},
        {"name": "Narrow", "priority": 1, "clearance": 0.15, "nets": ["Net-(D1-K)"]},
    ]
    first = _apply(board, tmp_path, name="r1", drc=run_drc, netclasses=classes, allow_loosen=True)
    revision = Path(first["output_pcb"])
    data = _read_pro(revision)
    data["net_settings"]["netclass_patterns"].append({"netclass": "Wide", "pattern": "Net-(D*"})
    _write_pro(revision, data)
    violations = _between_tracks(_drc(revision))
    assert len(violations) == 1  # the wildcard (priority 0, 0.35) beats the exact name (priority 1)
    assert _mm_figures(violations[0]) == [0.35, 0.25]
    assert board_rules._effective_class(data, "Net-(D1-K)") == "Wide"


def test_save_project_byte_shape_against_a_kicad_written_file(tmp_path: Path) -> None:
    source = GOLDEN_DIR / "golden_attiny1616_board.kicad_pro"
    expected = source.read_bytes().decode("utf-8").replace("\r\n", "\n")
    out = tmp_path / "rt.kicad_pro"
    board_rules._write_project(out, board_rules._read_project(source))
    text = out.read_bytes().decode("utf-8").replace("\r\n", "\n")
    assert expected.endswith("\n") and not text.endswith("\n")  # the documented difference
    assert text == expected.rstrip("\n")  # everything else identical
    board_rules._write_project(out, {"label": "Qté"})
    assert "\\u00e9" in out.read_text(encoding="utf-8")  # non-ASCII is escaped by the writer


def test_result_documents_the_file_format_difference(board: Path, tmp_path: Path) -> None:
    result = _apply(board, tmp_path, min_clearance=0.3)
    note = result["file_format_note"]
    assert "trailing newline" in note
    assert "\\uXXXX" in note


def test_token_goes_stale_when_custom_rules_or_a_lib_table_changes(board: Path, tmp_path: Path) -> None:
    _add_dru(board, _DRU)
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    _add_dru(board, _DRU + '(rule "later" (constraint clearance (min 0.9)))\n')
    result = board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["status"] == "error"
    assert not (tmp_path / "rev").exists()

    lib = board.parent / "fp-lib-table"
    lib.write_text("(fp_lib_table\n)\n", encoding="utf-8")  # scratch fixture in a tmp dir
    again = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    lib.write_text("(fp_lib_table\n  (lib (name x))\n)\n", encoding="utf-8")
    stale = board_rules.confirm_board_rules(
        board, again["revision_token"], parity="required", drc=_fake_drc()
    )
    assert stale["status"] == "error"


# --- delta review ------------------------------------------------------------------


def _tie_board(board: Path, names: tuple[str, str], classes_order: str, patterns: str, legacy=None) -> dict:
    """Write two equal-priority classes (first name 0.35 mm / second 0.15 mm) matching one net."""
    data = _read_pro(board)
    base = next(c for c in data["net_settings"]["classes"] if c["name"] == "Default")
    wide = {**base, "name": names[0], "priority": 1, "clearance": 0.35}
    narrow = {**base, "name": names[1], "priority": 1, "clearance": 0.15}
    by_letter = {"W": wide, "N": narrow}
    data["net_settings"]["classes"] = [base] + [by_letter[c] for c in classes_order]
    data["net_settings"]["netclass_patterns"] = [
        {"netclass": by_letter[c]["name"], "pattern": "Net-(D1-K)"} for c in patterns
    ]
    data["net_settings"]["netclass_assignments"] = legacy
    _write_pro(board, data)
    return data


@pytest.mark.parametrize(
    "names, classes_order, patterns, legacy_letter",
    [
        (("Wide", "Narrow"), "WN", "WN", None),
        (("Wide", "Narrow"), "WN", "NW", None),
        (("Wide", "Narrow"), "NW", "WN", None),
        (("Wide", "Narrow"), "NW", "NW", None),
        (("Wide", "Narrow"), "WN", "N", "W"),
        (("Wide", "Narrow"), "WN", "W", "N"),
        (("A_Wide", "Z_Narrow"), "WN", "WN", None),
        (("A_Wide", "Z_Narrow"), "WN", "NW", None),
        (("A_Wide", "Z_Narrow"), "WN", "N", "W"),
    ],
)
def test_equal_priority_tie_goes_to_the_smaller_class_name_like_kicad(
    board: Path, names, classes_order, patterns, legacy_letter
) -> None:
    legacy = None
    if legacy_letter:
        legacy = {"Net-(D1-K)": names[0] if legacy_letter == "W" else names[1]}
    data = _tie_board(board, names, classes_order, patterns, legacy)
    real_winner = names[0] if _between_tracks(_drc(board)) else names[1]  # a violation means 0.35 applied
    assert board_rules._effective_class(data, "Net-(D1-K)") == real_winner
    assert real_winner == min(names)


def test_tokenizer_cases_from_the_review(tmp_path: Path) -> None:
    cases = {
        "plain_multiline": (
            '(version 1)\n(rule "a"\n  (constraint\n    clearance\n    (min 0.5)))\n',
            [("a", "clearance")],
        ),
        "comment_with_open_paren": (
            '(version 1)\n# note (see issue\n(rule "a" (constraint clearance (min 0.5)))\n',
            [("a", "clearance")],
        ),
        "commented_out_rule": ('(version 1)\n# (rule "old" (constraint clearance (min 0.5)))\n', []),
        "quoted_name_with_space": (
            '(version 1)\n(rule "wide gap" (constraint clearance (min 0.5)))\n',
            [("wide gap", "clearance")],
        ),
        "paren_inside_condition": (
            '(version 1)\n(rule "a" (condition "A.Name == \'x)\'") (constraint clearance (min 0.5)))\n'
            '(rule "b" (constraint track_width (min 0.3)))\n',
            [("a", "clearance"), ("b", "track_width")],
        ),
        "paren_inside_name": (
            '(version 1)\n(rule "a(b" (constraint track_width (min 0.5)))\n'
            '(rule "c" (constraint clearance (min 0.5)))\n',
            [("a(b", "track_width"), ("c", "clearance")],
        ),
        "escaped_quote": (
            '(version 1)\n(rule "q\\"x" (constraint clearance (min 0.5)))\n',
            [('q"x', "clearance")],
        ),
        "uppercase": ('(version 1)\n(RULE "a" (constraint CLEARANCE (min 0.5)))\n', [("a", "clearance")]),
        "unmapped_types": (
            '(version 1)\n(rule "a" (constraint hole_clearance (min 0.5)))\n'
            '(rule "b" (constraint edge_clearance (min 0.5)))\n',
            [],
        ),
        "two_constraints": (
            '(version 1)\n(rule "a" (constraint clearance (min 0.5)) (constraint track_width (min 0.3)))\n',
            [("a", "clearance"), ("a", "track_width")],
        ),
        "no_space_after_rule": ('(version 1)\n(rule"a"(constraint clearance (min 0.5)))\n', [("a", "clearance")]),
        "crlf": ('(version 1)\r\n(rule "a"\r\n(constraint clearance (min 0.5)))\r\n', [("a", "clearance")]),
        "unterminated": ('(version 1)\n(rule "a" (constraint clearance (min 0.5)', [("a", "clearance")]),
    }
    for name, (text, expected) in cases.items():
        directory = tmp_path / name
        directory.mkdir()
        pcb = directory / "b.kicad_pcb"
        pcb.write_text("", encoding="utf-8")
        (directory / "b.kicad_dru").write_bytes(text.encode("utf-8"))
        file_name, rules = board_rules._custom_rules(pcb)
        assert file_name == "b.kicad_dru"
        assert [(r["rule"], r["constraint"]) for r in rules] == expected, name


def test_generic_warning_remains_when_nothing_is_parsed(board: Path, tmp_path: Path) -> None:
    _add_dru(board, '(version 1)\n# (rule "old" (constraint clearance (min 0.5)))\n')
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    assert proposal["custom_rules"] == []
    assert any("exists next to the board" in w for w in proposal["warnings"])


def test_cli_version_is_recorded_and_unverified_defaults_are_flagged(
    board: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(board_rules, "_cli_version", lambda: "10.0.5")
    ok = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    assert ok["kicad_cli_version"] == "10.0.5"
    assert ok["defaults_unverified"] is False
    monkeypatch.setattr(board_rules, "_cli_version", lambda: "9.0.7")
    other = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    assert other["kicad_cli_version"] == "9.0.7"
    assert other["defaults_unverified"] is True
    assert any("9.0.7" in w for w in other["warnings"])
    monkeypatch.setattr(board_rules, "_cli_version", lambda: None)
    none = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    assert none["kicad_cli_version"] is None and none["defaults_unverified"] is True


def test_real_cli_version_is_what_the_defaults_were_measured_on() -> None:
    version = board_rules._cli_version()
    assert version is not None and version.startswith("10.0."), version


def test_raising_a_minimum_over_existing_classes_says_so(board: Path, tmp_path: Path) -> None:
    proposal = board_rules.propose_board_rules(board, min_track_width=0.3, output_dir=tmp_path / "rev")
    assert proposal["status"] == "refused"
    assert "existing" in proposal["reason"] and "Default" in proposal["reason"]
    assert any(i["class"] == "Default" and i["key"] == "track_width" for i in proposal["incoherent"])
    assert proposal["incoherent_existing"] == []


def test_preexisting_incoherence_is_reported_but_not_refused(tmp_path: Path) -> None:
    board = _build(tmp_path / "proj", constraints={"min_track_width": 0.3})  # Default 0.25: already incoherent
    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    assert proposal["status"] == "proposed"
    assert proposal["incoherent"] == []
    assert any(i["class"] == "Default" and i["key"] == "track_width" for i in proposal["incoherent_existing"])


def test_moving_a_net_to_a_class_with_smaller_widths_is_a_loosening(board: Path, tmp_path: Path) -> None:
    first = _apply(
        board,
        tmp_path,
        name="r1",
        netclasses=[
            {"name": "Thick", "track_width": 0.6, "via_diameter": 0.9, "via_drill": 0.5, "nets": ["VCC"]},
            {"name": "Thin", "track_width": 0.25, "via_diameter": 0.6, "via_drill": 0.3},
        ],
    )
    proposal = board_rules.propose_board_rules(
        Path(first["output_pcb"]), netclasses=[{"name": "Thin", "nets": ["VCC"]}], output_dir=tmp_path / "r2"
    )
    assert proposal["status"] == "refused"
    kinds = {(e["kind"], e["key"], e["value_key"]) for e in proposal["loosened"]}
    assert ("assignment", "VCC", "track_width") in kinds
    assert ("assignment", "VCC", "via_diameter") in kinds
    assert ("assignment", "VCC", "via_drill") in kinds


def test_pcb_changed_between_propose_and_confirm_voids_the_token(board: Path, tmp_path: Path) -> None:
    from kicad_tools.schema import PCB
    from kicad_tools.sexp import SExp

    proposal = board_rules.propose_board_rules(board, min_clearance=0.3, output_dir=tmp_path / "rev")
    brd = PCB.load(board)
    seg = SExp.list("segment")
    for part in (
        SExp.list("start", 130, 100),
        SExp.list("end", 131, 100),
        SExp.list("width", 0.25),
        SExp.list("layer", "F.Cu"),
        SExp.list("net", 2),
        SExp.list("uuid", "77777777-0000-4000-8000-000000000000"),
    ):
        seg.append(part)
    brd._sexp.append(seg)
    write_layer.save_pcb(brd, board)
    result = board_rules.confirm_board_rules(
        board, proposal["revision_token"], parity="required", drc=_fake_drc()
    )
    assert result["status"] == "error"
    assert not (tmp_path / "rev").exists()
