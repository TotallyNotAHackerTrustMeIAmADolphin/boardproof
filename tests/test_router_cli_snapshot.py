"""Ticket 03 (pivot-ladder-build): router wrapper flags and the CLI-surface snapshot.

A router upgrade must fail a TEST instead of a RUN. The snapshot
(`src/kicad_mcp/routers/krt_cli_snapshot.json`) is captured from the real
installed router's `route.py --capabilities`; the checks below are one-way:
every flag the wrapper can emit must exist in the pinned router's surface.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

from kicad_mcp.routers import kicad_routing_tools as krt

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_PCB = FIXTURES / "golden_attiny1616_board" / "golden_attiny1616_board.kicad_pcb"
POUR_PCB = FIXTURES / "zone_obstacle" / "zone_obstacle.kicad_pcb"

_KEEPOUT_ONLY = """(kicad_pcb (version 20240108)
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (zone (net 0) (net_name "") (layer "F.Cu") (keepout (tracks not_allowed) (vias not_allowed)))
  (footprint "X" (zone (net 1) (layers "F.Cu")))
)
"""


# ---------------------------------------------------------------------------
# Pour detection (raw S-expression, never a kicad_tools convenience property)
# ---------------------------------------------------------------------------


def test_pour_detected_on_a_board_with_a_real_zone():
    info = krt.board_pour_info(POUR_PCB)
    assert info["has_pour"] is True
    assert info["pour_layers"] == ["F.Cu"]
    assert info["copper_layers"] == ["F.Cu", "B.Cu"]


def test_no_pour_on_a_board_without_zones():
    assert krt.board_pour_info(GOLDEN_PCB)["has_pour"] is False


def test_keepout_and_footprint_level_zones_are_not_pours(tmp_path):
    pcb = tmp_path / "k.kicad_pcb"
    pcb.write_text(_KEEPOUT_ONLY, encoding="utf-8")
    assert krt.board_pour_info(pcb)["has_pour"] is False


# ---------------------------------------------------------------------------
# Wrapper flags
# ---------------------------------------------------------------------------


def test_layer_costs_present_with_a_pour_and_absent_without():
    with_pour = krt.build_route_args("a", "b", pour_info=krt.board_pour_info(POUR_PCB))
    assert "--layer-costs" in with_pour
    assert with_pour[with_pour.index("--layers") + 1:with_pour.index("--layers") + 3] == ["F.Cu", "B.Cu"]
    # pour layer is costlier than the signal layer; order matches --layers
    costs = with_pour[with_pour.index("--layer-costs") + 1:with_pour.index("--layer-costs") + 3]
    assert [float(c) for c in costs] == [4.0, 1.0]

    without = krt.build_route_args("a", "b", pour_info=krt.board_pour_info(GOLDEN_PCB))
    assert "--layer-costs" not in without and "--layers" not in without
    assert "--layer-costs" not in krt.build_route_args("a", "b")


def test_strict_sizes_is_always_emitted():
    assert "--strict-sizes" in krt.build_route_args("a", "b")
    assert "--strict-sizes" in krt.build_route_args("a", "b", escalation="fab", clearance_mm=0.2)


def test_advanced_flag_is_emitted_and_logged():
    args = krt.build_route_args("a", "b", advanced_flags={"--via-cost": 120, "--bus": True})
    assert args[args.index("--via-cost") + 1] == "120"
    assert "--bus" in args
    logged = krt.logged_parameters(advanced_flags={"--via-cost": 120, "--bus": True})
    assert logged["advanced_flags"] == {"--via-cost": 120, "--bus": True}
    assert logged["strict_sizes"] is True


def test_advanced_flag_unknown_to_the_pinned_router_is_rejected_naming_it():
    with pytest.raises(ValueError, match="--no-such-flag"):
        krt.build_route_args("a", "b", advanced_flags={"--no-such-flag": 1})


def test_wrapper_managed_flags_cannot_be_smuggled_in_as_advanced():
    for flag in ("--overwrite", "--escalation", "--nets", "--layer-costs"):
        with pytest.raises(ValueError, match=flag):
            krt.build_route_args("a", "b", advanced_flags={flag: 1})


def test_delivered_below_requested_is_reported_not_hidden():
    log = krt.parse_route_log(
        "",
        json.dumps({"design_rules": {"count": 1, "narrowed": [
            {"net": 3, "net_name": "SDA", "kind": "track_width", "requested": 0.3, "delivered": 0.2,
             "site": "x", "count": 1}]}}),
    )
    assert log["delivered_below_requested"] == [
        {"net_name": "SDA", "kind": "track_width", "requested_mm": 0.3, "delivered_mm": 0.2,
         "site": "x"}  # the ledger site is kept since build ticket 37
    ]
    assert krt.parse_route_log("", json.dumps({}))["delivered_below_requested"] == []


# ---------------------------------------------------------------------------
# Snapshot: version-keyed, one-way flag check, names the flag
# ---------------------------------------------------------------------------


def test_every_flag_the_wrapper_can_emit_is_in_the_pinned_snapshot():
    snapshot = krt.load_cli_snapshot()
    assert snapshot["router_version"], "snapshot must record the router version it was taken from"
    assert krt.check_emitted_flags(snapshot) == []


def test_snapshot_check_fails_naming_the_flag_when_the_router_drops_it():
    snapshot = krt.load_cli_snapshot()
    broken = json.loads(json.dumps(snapshot))
    broken["modules"]["route.py"]["flags"].remove("--strict-sizes")
    missing = krt.check_emitted_flags(broken)
    assert [m["flag"] for m in missing] == ["--strict-sizes"]
    assert missing[0]["module"] == "route.py"


def test_version_mismatch_is_reported_not_ignored():
    snapshot = {"router_version": "0.22.0"}
    ok = krt.compare_router_version(snapshot, "0.22.0")
    assert ok["matches"] is True
    bad = krt.compare_router_version(snapshot, "0.23.1")
    assert bad["matches"] is False
    assert "0.23.1" in bad["message"] and "0.22.0" in bad["message"]
    unknown = krt.compare_router_version(snapshot, None)
    assert unknown["matches"] is False and "unknown" in unknown["message"].lower()


def test_installed_router_still_matches_the_snapshot(krt_checkout):
    checkout = krt_checkout  # absent: skip with the reason, or fail in the gate run (build ticket 53)
    live = krt.capture_cli_snapshot(checkout, Path(sys.executable))
    pinned = krt.load_cli_snapshot()
    version = krt.compare_router_version(pinned, live["router_version"], live["router_commit"])
    assert version["matches"], version["message"]
    for module, entry in pinned["modules"].items():
        gone = sorted(set(entry["flags"]) - set(live["modules"].get(module, {}).get("flags", [])))
        assert not gone, f"{module} no longer offers flag(s) the snapshot pins: {gone}"
    pinned_arity = pinned["modules"]["route.py"]["arity"]
    live_arity = live["modules"]["route.py"]["arity"]
    changed = {f: (a, live_arity.get(f)) for f, a in pinned_arity.items() if live_arity.get(f) != a}
    assert not changed, f"route.py argument arity changed (pinned, live): {changed}"


# ---------------------------------------------------------------------------
# Original project file restored after EVERY router script the wrapper runs
# ---------------------------------------------------------------------------

_SCRIPT_CLOBBERS_PROJECT = """\
import sys
from pathlib import Path
out = Path(sys.argv[2])
out.write_text("(kicad_pcb routed)\\n", encoding="utf-8")
out.with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
Path(sys.argv[1]).with_suffix(".kicad_pro").write_text('{"clobbered": true}', encoding="utf-8")
"""


def _checkout_with(root: Path, *scripts: str) -> Path:
    (root / "py_router").mkdir(parents=True)
    for name in scripts:
        (root / "py_router" / name).write_text(_SCRIPT_CLOBBERS_PROJECT, encoding="utf-8")
    return root


@pytest.mark.parametrize("script", ["route.py", "route_planes.py", "repair_planes.py"])
def test_original_project_is_restored_after_every_router_script(tmp_path, monkeypatch, script):
    checkout = _checkout_with(tmp_path / "krt", "route.py", script)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    src = tmp_path / "in" / "b.kicad_pcb"
    src.parent.mkdir()
    src.write_text("(kicad_pcb)", encoding="utf-8")
    original = '{"meta": {"original": true}}'
    src.with_suffix(".kicad_pro").write_text(original, encoding="utf-8")
    out = tmp_path / "out" / "b.kicad_pcb"

    if script == "route.py":
        result = krt.run_kicad_routing_tools(src, out, timeout=30)
    else:
        result = krt.run_router_script(script, [str(src), str(out)], output_pcb=out, original_pcb=src, timeout=30)

    assert out.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original
    assert src.with_suffix(".kicad_pro").read_text(encoding="utf-8") == original
    assert result["project_restored"] is True
    assert result["router_overwrote_project"] is True  # disclosed, then undone


def test_run_router_script_refuses_scripts_outside_the_pinned_set(tmp_path):
    with pytest.raises(ValueError, match="evil.py"):
        krt.run_router_script("evil.py", [], output_pcb=tmp_path / "o.kicad_pcb", original_pcb=tmp_path / "i.kicad_pcb")


def test_run_result_carries_logged_parameters_and_version_check(tmp_path, monkeypatch):
    checkout = _checkout_with(tmp_path / "krt", "route.py")
    (checkout / "VERSION").write_text("9.9.9\n", encoding="utf-8")
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    src = tmp_path / "b.kicad_pcb"
    shutil.copy(POUR_PCB, src)
    result = krt.run_kicad_routing_tools(src, tmp_path / "o" / "b.kicad_pcb", timeout=30,
                                         advanced_flags={"--via-cost": 90})
    params = result["logged_parameters"]
    assert params["strict_sizes"] is True
    assert params["advanced_flags"] == {"--via-cost": 90}
    assert params["layer_costs"]["layers"] == ["F.Cu", "B.Cu"]
    assert result["router_version"] == "9.9.9"
    assert result["version_check"]["matches"] is False
    assert "--strict-sizes" in result["args"] and "--layer-costs" in result["args"]
    assert result["strict_sizes_violated"] is False


# ---------------------------------------------------------------------------
# The pin sees the commit (issue 54): `VERSION` read 0.23.7 for the v0.23.7 tag and for the earlier
# pinned commit 234 commits later, so the version text alone could not tell them apart. The pin is
# now the v0.23.8 release commit (issue 64).
# ---------------------------------------------------------------------------

PINNED_COMMIT = "c64c0db25e6cf23afa6769ee05665c7ea22b3ff2"
OTHER_COMMIT = "0123456789abcdef0123456789abcdef01234567"


def _fake_git(checkout: Path, head: str, refs: dict[str, str] | None = None, packed: str | None = None) -> None:
    git = checkout / ".git"
    (git / "refs" / "heads").mkdir(parents=True, exist_ok=True)
    (git / "HEAD").write_text(head, encoding="utf-8")
    for name, sha in (refs or {}).items():
        target = git / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(sha + "\n", encoding="utf-8")
    if packed is not None:
        (git / "packed-refs").write_text(packed, encoding="utf-8")


def test_router_commit_reads_a_detached_head(tmp_path):
    _fake_git(tmp_path, PINNED_COMMIT + "\n")
    assert krt.router_commit(tmp_path) == PINNED_COMMIT


def test_router_commit_follows_a_branch_ref_loose_and_packed(tmp_path):
    loose = tmp_path / "loose"
    _fake_git(loose, "ref: refs/heads/main\n", {"refs/heads/main": PINNED_COMMIT})
    assert krt.router_commit(loose) == PINNED_COMMIT
    packed = tmp_path / "packed"
    _fake_git(packed, "ref: refs/heads/main\n", packed=f"# pack-refs\n{OTHER_COMMIT} refs/heads/other\n{PINNED_COMMIT} refs/heads/main\n")
    assert krt.router_commit(packed) == PINNED_COMMIT


def test_router_commit_is_none_without_git_or_with_a_broken_head(tmp_path):
    assert krt.router_commit(tmp_path) is None
    broken = tmp_path / "broken"
    _fake_git(broken, "ref: refs/heads/gone\n")
    assert krt.router_commit(broken) is None
    junk = tmp_path / "junk"
    _fake_git(junk, "not a sha\n")
    assert krt.router_commit(junk) is None


def test_the_snapshot_pins_the_commit_not_only_the_version():
    snapshot = krt.load_cli_snapshot()
    assert snapshot["router_commit"] == PINNED_COMMIT


def test_same_version_text_but_another_commit_does_not_match():
    snapshot = {"router_version": "0.23.8", "router_commit": PINNED_COMMIT}
    ok = krt.compare_router_version(snapshot, "0.23.8", PINNED_COMMIT)
    assert ok["matches"] is True
    tag = krt.compare_router_version(snapshot, "0.23.8", OTHER_COMMIT)
    assert tag["matches"] is False
    assert PINNED_COMMIT[:8] in tag["message"] and OTHER_COMMIT[:8] in tag["message"]
    assert tag["pinned_commit"] == PINNED_COMMIT and tag["live_commit"] == OTHER_COMMIT


def test_an_unknown_commit_does_not_match_a_snapshot_that_pins_one():
    snapshot = {"router_version": "0.23.8", "router_commit": PINNED_COMMIT}
    unknown = krt.compare_router_version(snapshot, "0.23.8", None)
    assert unknown["matches"] is False and "commit" in unknown["message"].lower()
    assert "unknown" in unknown["message"].lower()


def test_a_snapshot_without_a_commit_pin_still_compares_the_version_only():
    assert krt.compare_router_version({"router_version": "0.22.0"}, "0.22.0", None)["matches"] is True


def test_run_result_carries_the_commit_and_flags_a_commit_mismatch(tmp_path, monkeypatch):
    checkout = _checkout_with(tmp_path / "krt", "route.py")
    (checkout / "VERSION").write_text(krt.load_cli_snapshot()["router_version"] + "\n", encoding="utf-8")
    _fake_git(checkout, OTHER_COMMIT + "\n")
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.delenv(krt.ENV_PYTHON, raising=False)
    src = tmp_path / "b.kicad_pcb"
    shutil.copy(POUR_PCB, src)
    result = krt.run_kicad_routing_tools(src, tmp_path / "o" / "b.kicad_pcb", timeout=30)
    assert result["router_commit"] == OTHER_COMMIT
    assert result["version_check"]["matches"] is False
    assert "commit" in result["version_check"]["message"].lower()


def test_installed_router_is_at_the_pinned_commit(krt_checkout):
    commit = krt.router_commit(krt_checkout)
    assert commit == krt.load_cli_snapshot()["router_commit"], (
        f"the KRT checkout is at {commit}, the snapshot pins {krt.load_cli_snapshot()['router_commit']}"
    )


# ---------------------------------------------------------------------------
# Review of issue 54: the availability path, and router_commit never raises or leaves .git
# ---------------------------------------------------------------------------


def _availability_with(tmp_path, monkeypatch, *, head: str | None):
    from kicad_mcp import rung_backends

    checkout = tmp_path / "krt"
    checkout.mkdir()
    (checkout / "VERSION").write_text(krt.load_cli_snapshot()["router_version"] + "\n", encoding="utf-8")
    if head is not None:
        _fake_git(checkout, head)
    monkeypatch.setenv(krt.ENV_PATH, str(checkout))
    monkeypatch.setattr(krt, "check_availability", lambda: {"status": "complete", "checkout": str(checkout)})
    return rung_backends, rung_backends.krt_availability()


def test_availability_flags_a_checkout_at_another_commit_as_unofficial(tmp_path, monkeypatch):
    rung_backends, info = _availability_with(tmp_path, monkeypatch, head=OTHER_COMMIT + "\n")
    assert info["available"] is True
    assert info["version_check"]["matches"] is False
    assert info["versions"]["router_commit"] == OTHER_COMMIT
    assert rung_backends.unofficial_reasons(info)


def test_availability_flags_a_checkout_without_git_as_unofficial(tmp_path, monkeypatch):
    rung_backends, info = _availability_with(tmp_path, monkeypatch, head=None)
    assert info["version_check"]["matches"] is False
    assert info["versions"]["router_commit"] is None
    assert rung_backends.unofficial_reasons(info)


def test_availability_at_the_pinned_commit_is_official(tmp_path, monkeypatch):
    rung_backends, info = _availability_with(tmp_path, monkeypatch, head=PINNED_COMMIT + "\n")
    assert info["version_check"]["matches"] is True
    assert info["versions"]["router_commit"] == PINNED_COMMIT
    assert rung_backends.unofficial_reasons(info) == []


def test_router_commit_never_raises_on_a_gitdir_with_a_nul(tmp_path):
    (tmp_path / ".git").write_text("gitdir: a\x00b\n", encoding="utf-8")
    assert krt.router_commit(tmp_path) is None


def test_router_commit_refuses_a_ref_that_leaves_refs(tmp_path):
    (tmp_path / "outside").write_text(PINNED_COMMIT + "\n", encoding="utf-8")
    for ref in ("../../outside", "refs/../../outside", "outside", "/outside"):
        root = tmp_path / ("r" + str(abs(hash(ref))))
        _fake_git(root, f"ref: {ref}\n")
        (root.parent / "outside").write_text(PINNED_COMMIT + "\n", encoding="utf-8")
        assert krt.router_commit(root) is None, ref


def test_router_commit_follows_commondir_for_a_worktree_on_a_branch(tmp_path):
    main = tmp_path / "main"
    _fake_git(main, "ref: refs/heads/main\n", {"refs/heads/work": PINNED_COMMIT})
    gitdir = main / ".git" / "worktrees" / "w"
    gitdir.mkdir(parents=True)
    (gitdir / "HEAD").write_text("ref: refs/heads/work\n", encoding="utf-8")
    (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
    assert krt.router_commit(wt) == PINNED_COMMIT


def test_the_pin_is_the_v0_23_8_release_and_the_binary_version_note_is_recorded():
    """Issue 64: the checkout is v0.23.8; its prebuilt binary still reports 0.23.7 (the crate version
    was not bumped), and docs/ARCHITECTURE.md says so next to the pin so nobody reads it as a mismatch."""
    snapshot = krt.load_cli_snapshot()
    assert snapshot["router_version"] == "0.23.8"
    assert snapshot["router_commit"] == PINNED_COMMIT
    text = (Path(__file__).resolve().parents[1] / "docs/ARCHITECTURE.md").read_text(encoding="utf-8")
    pin_paragraph = next(p for p in text.splitlines() if p.startswith("- **KRT checkout pinned to"))
    assert "v0.23.8" in pin_paragraph and PINNED_COMMIT[:8] in pin_paragraph
    assert "reports `0.23.7`" in pin_paragraph and "not a mismatch" in pin_paragraph
