"""Tests for the shared checkpoint helper (src/kicad_mcp/checkpoint.py, build ticket 22).

Contract: build ticket 22 and docs/ARCHITECTURE.md
decisions 4 and 12. Seams: the helper's public interface (``Pending``, ``token_project_hash``,
``build_revision``) and the five checkpointed confirm functions that use it. Project files are
built from the golden board and changed only through ``kicad_tools`` and the write layer.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from kicad_mcp import checkpoint, gate_state, write_layer

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    shutil.copytree(GOLDEN_DIR, tmp_path / "proj")
    return tmp_path / "proj" / GOLDEN_NAME


def _touch_project_file(pcb: Path) -> None:
    """Change the .kicad_pro (not the board) through the write layer."""
    pro = pcb.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text(encoding="utf-8"))
    data["x_test_drift"] = "changed between propose and confirm"
    write_layer.save_project(data, pro)


def _composite(pcb: Path) -> str:
    """The gate field's project hash, the independent reference for the token's hash."""
    field = gate_state.GateState().gate_field(pcb)
    assert field["project_sha"] is not None
    return field["project_sha"]


class _Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


# --- the revision token ----------------------------------------------------------------------


def test_the_token_carries_the_composite_project_hash_and_a_per_proposal_id(board: Path) -> None:
    pending = checkpoint.Pending(clock=_Clock())
    first = pending.issue(board, {"n": 1})
    second = pending.issue(board, {"n": 2})
    assert first != second
    assert checkpoint.token_project_hash(first) == _composite(board)
    assert checkpoint.token_project_hash(second) == _composite(board)


def test_a_malformed_token_carries_no_hash() -> None:
    for bad in ("", "abc", "0123456789abcdef", "z" * 64 + "-" + "0" * 16):
        assert checkpoint.token_project_hash(bad) is None


def test_each_claim_returns_exactly_its_own_proposal(board: Path) -> None:
    pending = checkpoint.Pending(clock=_Clock())
    first = pending.issue(board, {"n": 1})
    second = pending.issue(board, {"n": 2})
    assert pending.claim(second, board, drift_reason="drift") == ({"n": 2}, None)
    assert pending.claim(first, board, drift_reason="drift") == ({"n": 1}, None)


def test_a_project_file_changed_after_the_proposal_refuses_the_claim_and_spends_the_token(board: Path) -> None:
    pending = checkpoint.Pending(clock=_Clock())
    token = pending.issue(board, {"n": 1})
    _touch_project_file(board)
    payload, refusal = pending.claim(token, board, drift_reason="the board changed: re-propose")
    assert payload is None
    assert refusal == {"status": "error", "reason": "the board changed: re-propose"}
    _, again = pending.claim(token, board, drift_reason="x")
    assert again == {"status": "error", "reason": "unknown or expired revision_token"}


def test_an_extra_file_outside_the_project_is_checked_too(board: Path, tmp_path: Path) -> None:
    outside = tmp_path / "elsewhere" / "root.kicad_sch"
    outside.parent.mkdir()
    shutil.copy(board.with_suffix(".kicad_sch"), outside)
    pending = checkpoint.Pending(clock=_Clock())
    changed = pending.issue(board, {"n": 1}, extra_files=[outside])
    removed = pending.issue(board, {"n": 2}, extra_files=[outside])
    shutil.copy(FIXTURES / "simple_led.kicad_sch", outside)  # another KiCad-written sheet's bytes
    _, refusal = pending.claim(changed, board, drift_reason="drift")
    assert refusal == {"status": "error", "reason": "drift"}
    outside.unlink()
    _, refusal = pending.claim(removed, board, drift_reason="drift")
    assert refusal == {"status": "error", "reason": "drift"}


def test_a_different_path_is_refused_and_keeps_the_token(board: Path, tmp_path: Path) -> None:
    other = tmp_path / "other" / GOLDEN_NAME
    shutil.copytree(GOLDEN_DIR, other.parent)
    pending = checkpoint.Pending(clock=_Clock())
    token = pending.issue(board, {"n": 1})
    _, refusal = pending.claim(token, other, drift_reason="drift")
    assert refusal == {"status": "error", "reason": "pcb_path does not match the path this proposal was made for"}
    assert pending.claim(token, board, drift_reason="drift") == ({"n": 1}, None)


def test_a_claim_does_not_spend_the_token_only_spend_does(board: Path) -> None:
    pending = checkpoint.Pending(clock=_Clock())
    token = pending.issue(board, {"n": 1})
    assert pending.claim(token, board, drift_reason="drift")[1] is None
    assert pending.claim(token, board, drift_reason="drift")[1] is None
    pending.spend(token)
    assert pending.claim(token, board, drift_reason="drift")[1]["reason"] == "unknown or expired revision_token"


def test_the_token_expires_after_the_ttl(board: Path) -> None:
    clock = _Clock()
    pending = checkpoint.Pending(clock=clock)
    token = pending.issue(board, {"n": 1})
    clock.now += checkpoint.TTL_SECONDS - 1
    assert pending.claim(token, board, drift_reason="drift")[1] is None
    clock.now += 2
    assert pending.claim(token, board, drift_reason="drift")[1]["reason"] == "unknown or expired revision_token"


def test_the_store_is_bounded_and_drops_the_oldest(board: Path) -> None:
    clock = _Clock()
    pending = checkpoint.Pending(clock=clock)
    tokens = []
    for n in range(checkpoint.MAX_ENTRIES + 1):
        clock.now += 1
        tokens.append(pending.issue(board, {"n": n}))
    clock.now += 1
    pending.issue(board, {"n": "last"})  # issuing evicts down to the bound first
    assert pending.claim(tokens[0], board, drift_reason="drift")[1]["reason"] == "unknown or expired revision_token"
    assert pending.claim(tokens[-1], board, drift_reason="drift")[1] is None


def test_a_project_that_cannot_be_fingerprinted_gets_no_token(board: Path) -> None:
    pending = checkpoint.Pending(clock=_Clock())
    with pytest.raises(checkpoint.Refused) as caught:
        pending.issue(board.parent / "missing.kicad_pcb", {"n": 1})
    assert "missing.kicad_pcb" in str(caught.value)


# --- the new revision, verified before it is moved into place -----------------------------------


def _write_marker(tmp: Path, board: Path) -> Path:
    """The write step of a stand-in tool: copy the project and change its .kicad_pro."""
    copied = checkpoint.copy_project(board, tmp)
    _touch_project_file(copied)
    return copied


def _tree_bytes(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


def test_a_verified_revision_is_moved_into_place_and_the_source_is_untouched(board: Path, tmp_path: Path) -> None:
    before = _tree_bytes(board.parent)
    out = tmp_path / "deep" / "rev"
    result = checkpoint.build_revision(
        out,
        write=lambda tmp: _write_marker(tmp, board),
        verify=lambda tmp, copied: {"output": copied.name},
        success="built",
        before_verified="the stand-in failed before a revision was verified",
    )
    assert result == {"status": "built", "output": GOLDEN_NAME}
    assert json.loads((out / board.with_suffix(".kicad_pro").name).read_text(encoding="utf-8"))["x_test_drift"]
    assert _tree_bytes(board.parent) == before
    assert sorted(p.name for p in (tmp_path / "deep").iterdir()) == ["rev"]


def test_a_failed_self_check_returns_write_not_verified_and_leaves_nothing(board: Path, tmp_path: Path) -> None:
    before = _tree_bytes(board.parent)
    out = tmp_path / "deep" / "rev"

    def verify(tmp: Path, copied: Path) -> dict:
        raise checkpoint.NotVerified(
            "CRITICAL: the stand-in reloaded something else",
            [checkpoint.mismatch(copied.name, "gr_text 0 layer", "F.SilkS", "F.Cu")],
            before={"gr_text": 1},
        )

    result = checkpoint.build_revision(
        out, write=lambda tmp: _write_marker(tmp, board), verify=verify, success="built",
        before_verified="unused",
    )
    assert result == {
        "status": "error",
        "ok": False,
        "code": "write_not_verified",
        "reason": "CRITICAL: the stand-in reloaded something else",
        "mismatches": {
            "count": 1,
            "items": [{"file": GOLDEN_NAME, "item": "gr_text 0 layer", "expected": "F.SilkS", "actual": "F.Cu"}],
        },
        "before": {"gr_text": 1},
        "note": "no revision was left behind and the revision_token is still valid",
    }
    assert not (tmp_path / "deep").exists()
    assert _tree_bytes(board.parent) == before


def test_a_failed_write_step_is_incomplete_and_leaves_nothing(board: Path, tmp_path: Path) -> None:
    def write(tmp: Path) -> None:
        checkpoint.copy_project(board, tmp)
        raise OSError("disk full")

    result = checkpoint.build_revision(
        tmp_path / "a" / "rev", write=write, verify=lambda tmp, state: {}, success="built",
        before_verified="the stand-in failed before a revision was verified", note="custom note",
    )
    assert result == {
        "status": "incomplete",
        "reason": "the stand-in failed before a revision was verified: disk full",
        "note": "custom note",
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


def test_a_step_may_refuse_by_name_with_its_own_status_and_fields(board: Path, tmp_path: Path) -> None:
    def verify(tmp: Path, state: Any) -> dict:
        raise checkpoint.Failed("incomplete", "kicad-cli did not run", outline_drc={"status": "not_run"})

    result = checkpoint.build_revision(
        tmp_path / "rev", write=lambda tmp: _write_marker(tmp, board), verify=verify, success="built",
        before_verified="unused",
    )
    assert result == {
        "status": "incomplete",
        "reason": "kicad-cli did not run",
        "note": "no revision was left behind and the revision_token is still valid",
        "outline_drc": {"status": "not_run"},
    }
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


def test_a_move_that_fails_is_incomplete_and_leaves_nothing(board: Path, tmp_path: Path, monkeypatch) -> None:
    def denied(temp_dir, target):
        raise PermissionError("[WinError 5] Access is denied")

    monkeypatch.setattr(write_layer, "commit_directory", denied)
    result = checkpoint.build_revision(
        tmp_path / "rev", write=lambda tmp: _write_marker(tmp, board), verify=lambda tmp, s: {}, success="built",
        before_verified="unused",
    )
    assert result["status"] == "incomplete"
    assert result["reason"] == "could not move the verified revision into place: [WinError 5] Access is denied"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


def test_an_empty_output_directory_the_caller_made_is_used(board: Path, tmp_path: Path) -> None:
    out = tmp_path / "rev"
    out.mkdir()
    result = checkpoint.build_revision(
        out, write=lambda tmp: _write_marker(tmp, board), verify=lambda tmp, s: {}, success="applied",
        before_verified="unused",
    )
    assert result == {"status": "applied"}
    assert (out / GOLDEN_NAME).is_file()


def test_reload_differences_become_mismatches() -> None:
    differences = {"missing": ["zone:KEEPOUT_H1"], "unexpected": ["footprint:C9"], "tolerated": ["net:3"]}
    assert checkpoint.reload_mismatches("b.kicad_pcb", differences) == [
        {"file": "b.kicad_pcb", "item": "zone:KEEPOUT_H1", "expected": "present after reload", "actual": "missing"},
        {"file": "b.kicad_pcb", "item": "footprint:C9", "expected": "absent", "actual": "present but not intended"},
    ]


def test_the_mismatch_list_is_bounded(board: Path, tmp_path: Path) -> None:
    many = [checkpoint.mismatch("f", f"item {i}", i, i + 1) for i in range(50)]

    def verify(tmp: Path, state: Any) -> dict:
        raise checkpoint.NotVerified("CRITICAL: many", many)

    result = checkpoint.build_revision(
        tmp_path / "rev", write=lambda tmp: _write_marker(tmp, board), verify=verify, success="built",
        before_verified="unused",
    )
    assert result["mismatches"]["count"] == 50
    assert result["mismatches"]["items"] == many[:20]


def test_copy_project_leaves_out_other_boards_tool_outputs_and_backups(board: Path, tmp_path: Path) -> None:
    project = board.parent
    shutil.copy(board, project / "other.kicad_pcb")
    (project / "proj_prepared").mkdir()
    (project / "proj_prepared" / "x.txt").write_text("old output", encoding="utf-8")
    (project / "proj-backups").mkdir()
    (project / "mine").mkdir()
    (project / "mine" / "keep.txt").write_text("user file", encoding="utf-8")
    copied = checkpoint.copy_project(board, tmp_path / "copy", outputs=("proj_prepared",), skip_suffixes=(".kicad_net",))
    names = sorted(str(p.relative_to(tmp_path / "copy")).replace("\\", "/") for p in (tmp_path / "copy").rglob("*"))
    assert copied == tmp_path / "copy" / GOLDEN_NAME
    assert "other.kicad_pcb" not in names and "proj_prepared" not in names and "proj-backups" not in names
    assert "mine/keep.txt" in names
    assert not any(n.endswith(".kicad_net") for n in names)


def test_copy_project_refuses_a_project_over_the_cap(board: Path, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="too large to copy"):
        checkpoint.copy_project(board, tmp_path / "copy", max_bytes=1)
    assert not (tmp_path / "copy").exists()


def test_output_dir_problems_are_named(board: Path, tmp_path: Path) -> None:
    project = board.parent
    assert checkpoint.output_dir_problem(board, project, what="the import") == (
        "output_dir is the original project directory: the import must be a new revision"
    )
    assert "inside the source project tree" in checkpoint.output_dir_problem(board, project / "x", what="w")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "f").write_text("x", encoding="utf-8")
    assert "exists and is not empty" in checkpoint.output_dir_problem(board, busy, what="w")
    assert checkpoint.output_dir_problem(board, tmp_path / "fresh", what="w") is None


# --- the five checkpointed pairs use the helper ------------------------------------------------
#
# One adapter per pair: how to build its project, propose with one of two variants, confirm,
# and read back from a confirmed revision which variant it carries. The variants differ in an
# argument the revision shows, so "each confirm applies exactly its own proposal" is visible.


def _drop_first_footprint(path: Path) -> None:
    """Corrupt a saved board file after the write layer wrote it: one footprint goes missing."""
    root = write_layer.load(path)
    first = next(c for c in root.children if not c.is_atom and c.name == "footprint")
    root.children.remove(first)
    write_layer.save(root, path)


class _InputPrep:
    name = "input_prep"
    variants = ("a", "b")

    def build(self, tmp_path: Path) -> Path:
        from tests.test_input_prep import _build_fixture

        return _build_fixture(tmp_path / "proj")

    def propose(self, board: Path, out: Path, variant: str) -> dict:
        from kicad_mcp import input_prep

        return input_prep.propose_input_preparation(board, out / variant)

    def confirm(self, board: Path, token: str) -> dict:
        from kicad_mcp import input_prep
        from tests.test_input_prep import _fake_drc

        return input_prep.confirm_input_preparation(board, token, parity="required", drc=_fake_drc())

    def variant_of(self, result: dict) -> str:
        return Path(result["output_pcb"]).parent.name

    def corrupt(self, monkeypatch) -> None:
        _corrupt_saved_board(monkeypatch)


def _corrupt_saved_board(monkeypatch) -> None:
    original = write_layer.save_pcb

    def save_then_corrupt(board, path, **kwargs):
        saved = original(board, path, **kwargs)
        _drop_first_footprint(Path(path))
        return saved

    monkeypatch.setattr(write_layer, "save_pcb", save_then_corrupt)


class _SilkMinimums:
    name = "silk_minimums"
    variants = ("low", "high")
    _heights = {"low": 1.0, "high": 1.5}

    def build(self, tmp_path: Path) -> Path:
        from tests.test_silk_minimums import _build

        return _build(tmp_path / "proj")

    def propose(self, board: Path, out: Path, variant: str) -> dict:
        from kicad_mcp import silk_minimums

        return silk_minimums.propose_silkscreen_minimums(board, out / variant, min_height_mm=self._heights[variant])

    def confirm(self, board: Path, token: str) -> dict:
        from kicad_mcp import silk_minimums

        return silk_minimums.confirm_silkscreen_minimums(board, token)

    def variant_of(self, result: dict) -> str:
        assert Path(result["output_pcb"]).parent.name == next(
            v for v, h in self._heights.items() if h == result["minimums"]["height_mm"]
        )
        return Path(result["output_pcb"]).parent.name

    def corrupt(self, monkeypatch) -> None:
        _corrupt_saved_board(monkeypatch)


class _BoardOutline:
    name = "board_outline"
    variants = ("square", "rounded")
    _radius = {"square": 0.0, "rounded": 1.0}

    def build(self, tmp_path: Path) -> Path:
        from tests.test_board_outline import GOLDEN_DIR, GOLDEN_NAME

        shutil.copytree(GOLDEN_DIR, tmp_path / "proj")
        return tmp_path / "proj" / GOLDEN_NAME

    def propose(self, board: Path, out: Path, variant: str) -> dict:
        from kicad_mcp import board_outline
        from tests.test_board_outline import RECT

        outline = {**RECT, "corner_radius": self._radius[variant]}
        return board_outline.propose_board_outline(board, outline, output_dir=out / variant)

    def confirm(self, board: Path, token: str) -> dict:
        from kicad_mcp import board_outline
        from tests.test_board_outline import _no_outline_drc

        return board_outline.confirm_board_outline(board, token, drc=_no_outline_drc)

    def variant_of(self, result: dict) -> str:
        assert self._radius[Path(result["output_pcb"]).parent.name] == result["outline"]["corner_radius"]
        return Path(result["output_pcb"]).parent.name

    def corrupt(self, monkeypatch) -> None:
        _corrupt_saved_board(monkeypatch)


class _BoardRules:
    name = "board_rules"
    variants = ("tight", "loose")
    _clearance = {"tight": 0.15, "loose": 0.18}

    def build(self, tmp_path: Path) -> Path:
        from tests.test_board_rules import _build

        return _build(tmp_path / "proj")

    def propose(self, board: Path, out: Path, variant: str) -> dict:
        from kicad_mcp import board_rules

        return board_rules.propose_board_rules(board, output_dir=out / variant, min_clearance=self._clearance[variant])

    def confirm(self, board: Path, token: str) -> dict:
        from kicad_mcp import board_rules
        from tests.test_board_rules import _fake_drc

        return board_rules.confirm_board_rules(board, token, parity="required", drc=_fake_drc())

    def variant_of(self, result: dict) -> str:
        assert self._clearance[Path(result["output_pcb"]).parent.name] == result["readback"]["constraints"]["min_clearance"]
        return Path(result["output_pcb"]).parent.name

    def corrupt(self, monkeypatch) -> None:
        original = write_layer.save_project

        def save_then_corrupt(data, path, **kwargs):  # the saved project loses the requested rule
            saved = original(data, path, **kwargs)
            corrupted = json.loads(Path(path).read_text(encoding="utf-8"))
            corrupted["board"]["design_settings"]["rules"]["min_clearance"] = 9.5
            original(corrupted, path)
            return saved

        monkeypatch.setattr(write_layer, "save_project", save_then_corrupt)


class _SchematicImport:
    name = "schematic_import"
    variants = ("narrow", "wide")
    _columns = {"narrow": 3, "wide": 5}

    def build(self, tmp_path: Path) -> Path:
        from kicad_mcp.kicad_cli import find_kicad_cli
        from tests.test_schematic_import import NAME, RUNG1

        try:
            find_kicad_cli()
        except Exception:  # noqa: BLE001
            pytest.skip("kicad-cli not installed")
        shutil.copytree(RUNG1 / "project", tmp_path / "proj")
        return tmp_path / "proj" / f"{NAME}.kicad_pcb"

    def propose(self, board: Path, out: Path, variant: str) -> dict:
        from kicad_mcp import schematic_import

        return schematic_import.propose_schematic_import(board, output_dir=out / variant, columns=self._columns[variant])

    def confirm(self, board: Path, token: str) -> dict:
        from kicad_mcp import schematic_import

        return schematic_import.confirm_schematic_import(board, token)

    def variant_of(self, result: dict) -> str:
        assert self._columns[Path(result["output_pcb"]).parent.name] == result["staging"]["columns"]
        return Path(result["output_pcb"]).parent.name

    def corrupt(self, monkeypatch) -> None:
        _corrupt_saved_board(monkeypatch)


PAIRS = [_InputPrep(), _SilkMinimums(), _BoardOutline(), _BoardRules(), _SchematicImport()]


@pytest.fixture(params=PAIRS, ids=lambda pair: pair.name)
def pair(request):
    return request.param


def test_two_proposals_on_an_unchanged_project_each_apply_their_own(pair, tmp_path: Path) -> None:
    board = pair.build(tmp_path)
    first = pair.propose(board, tmp_path / "out", pair.variants[0])
    second = pair.propose(board, tmp_path / "out", pair.variants[1])
    assert first["revision_token"] != second["revision_token"]
    assert checkpoint.token_project_hash(first["revision_token"]) == _composite(board)
    assert checkpoint.token_project_hash(second["revision_token"]) == _composite(board)
    later = pair.confirm(board, second["revision_token"])
    earlier = pair.confirm(board, first["revision_token"])
    assert pair.variant_of(later) == pair.variants[1]
    assert pair.variant_of(earlier) == pair.variants[0]


def test_a_project_file_changed_between_propose_and_confirm_is_refused(pair, tmp_path: Path) -> None:
    board = pair.build(tmp_path)
    proposal = pair.propose(board, tmp_path / "out", pair.variants[0])
    _touch_project_file(board)  # the .kicad_pro, not the board itself
    refused = pair.confirm(board, proposal["revision_token"])
    assert refused["status"] == "error"
    assert "changed since the proposal was made" in refused["reason"]
    assert not (tmp_path / "out").exists()
    again = pair.confirm(board, proposal["revision_token"])
    assert again["reason"] == "unknown or expired revision_token"


def test_a_corrupted_temporary_file_is_write_not_verified_and_the_source_stays_byte_identical(
    pair, tmp_path: Path, monkeypatch
) -> None:
    board = pair.build(tmp_path)
    before = _tree_bytes(board.parent)
    proposal = pair.propose(board, tmp_path / "out", pair.variants[0])
    pair.corrupt(monkeypatch)
    result = pair.confirm(board, proposal["revision_token"])
    assert result["ok"] is False
    assert result["code"] == "write_not_verified"
    assert result["status"] == "error"
    assert result["mismatches"]["count"] >= 1
    for item in result["mismatches"]["items"]:
        assert set(item) == {"file", "item", "expected", "actual"}
    assert _tree_bytes(board.parent) == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["proj"]


def test_a_confirmed_token_is_spent_even_when_the_output_is_writable_again(pair, tmp_path: Path) -> None:
    """The success path spends the token: removing the output must not make it usable again."""
    board = pair.build(tmp_path)
    proposal = pair.propose(board, tmp_path / "out", pair.variants[0])
    done = pair.confirm(board, proposal["revision_token"])
    assert done["status"] in {"applied", "imported", "prepared", "outlined"} or done.get("output_pcb")
    shutil.rmtree(Path(done["output_pcb"]).parent)
    again = pair.confirm(board, proposal["revision_token"])
    assert again["status"] == "error"
    assert again["reason"] == "unknown or expired revision_token"


def test_a_token_expires_by_the_modules_own_clock(pair, tmp_path: Path, monkeypatch) -> None:
    import importlib

    module = importlib.import_module(f"kicad_mcp.{pair.name}")
    now = [1_000_000.0]
    monkeypatch.setattr(module, "_clock", lambda: now[0])
    board = pair.build(tmp_path)
    proposal = pair.propose(board, tmp_path / "out", pair.variants[0])
    now[0] += checkpoint.TTL_SECONDS + 1
    expired = pair.confirm(board, proposal["revision_token"])
    assert expired["status"] == "error"
    assert expired["reason"] == "unknown or expired revision_token"
    assert not (tmp_path / "out" / pair.variants[0]).exists()
