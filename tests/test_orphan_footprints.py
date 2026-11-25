"""Build ticket 227: propose/confirm removal of orphaned footprints (no schematic symbol).

Only an unlocked footprint with no copper on its pad nets is removed; the revision is verified by a
fresh reload before it is moved into place. Fixtures are built through the write layer on tmp copies:
an imported rung-1 board whose footprints are renamed to references the schematic does not know.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from boardproof import checkpoint, orphan_footprints as of
from boardproof import schematic_import as si
from boardproof import write_layer
from boardproof.kicad_cli import find_kicad_cli
from boardproof.placement_anchor import set_locked
from tests._fixture_copy import copy_fixture

REPO = Path(__file__).resolve().parents[1]
RUNG1 = REPO / "evals" / "rung1"
NAME = "rung1_usbc_attiny1616"


def _cli() -> bool:
    try:
        find_kicad_cli()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _cli(), reason="kicad-cli not installed")


def _digest(root: Path) -> dict[str, str]:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


def _refs(pcb: Path) -> list[str]:
    return [si._reference_of(f) for f in write_layer.load(pcb).children if f.name == "footprint"]


def _pad_net(pcb: Path, ref: str) -> str:
    root = write_layer.load(pcb)
    fp = next(f for f in root.children if f.name == "footprint" and si._reference_of(f) == ref)
    return sorted(n for (r, _), ns in si._pad_nets(SExpRoot(fp)).items() for n in ns if n)[0]


class SExpRoot:
    """A one-footprint stand-in root for ``_pad_nets``."""

    def __init__(self, fp):
        self.children = [fp]


@pytest.fixture()
def board(tmp_path: Path) -> Path:
    return make_board(tmp_path)


def make_board(tmp_path: Path) -> Path:
    """An imported rung-1 board with R1 -> ZZ1 (plain orphan), R2 -> ZZ2 (locked), R3 -> ZZ3 (its net gets a track)."""
    proj = tmp_path / "proj"
    copy_fixture(RUNG1 / "project", proj)
    first = tmp_path / "first"
    proposal = si.propose_schematic_import(proj / f"{NAME}.kicad_pcb", output_dir=first)
    assert proposal["status"] == "proposed", proposal
    assert si.confirm_schematic_import(proj / f"{NAME}.kicad_pcb", proposal["revision_token"])["status"] == "imported"
    pcb = first / f"{NAME}.kicad_pcb"
    root = write_layer.load(pcb)
    refs = _refs(pcb)
    victims = {"ZZ1": refs[0], "ZZ2": refs[1], "ZZ3": refs[2]}
    for new, old in victims.items():
        fp = next(f for f in root.children if f.name == "footprint" and si._reference_of(f) == old)
        prop = next(c for c in fp.children if c.name == "property" and str(c.values[0]) == "Reference")
        write_layer.set_string(prop, 1, new)
        if new == "ZZ2":
            set_locked(fp, True)
    write_layer.save(root, pcb)
    return pcb


def _propose(pcb: Path, tmp_path: Path) -> dict:
    return of.propose_orphan_footprint_removal(pcb, output_dir=tmp_path / "out")


def _track_on(pcb: Path, ref: str) -> str:
    root = write_layer.load(pcb)
    fp = next(f for f in root.children if f.name == "footprint" and si._reference_of(f) == ref)
    net = sorted(n for ns in si._pad_nets(SExpRoot(fp)).values() for n in ns if n)[0]
    seg = SExp.list(
        "segment",
        SExp.list("start", write_layer.number(1.0), write_layer.number(1.0)),
        SExp.list("end", write_layer.number(2.0), write_layer.number(1.0)),
        SExp.list("width", write_layer.number(0.2)),
        SExp.list("layer", write_layer.string("F.Cu")),
        SExp.list("net", write_layer.string(net)),
        SExp.list("uuid", write_layer.string("aaaaaaaa-0000-4000-8000-000000000001")),
    )
    root.append(seg)
    write_layer.save(root, pcb)
    return net


def test_dry_run_lists_removable_and_kept_and_writes_nothing(board: Path, tmp_path: Path) -> None:
    before = _digest(tmp_path / "first")
    result = _propose(board, tmp_path)
    assert result["status"] == "proposed", result
    assert "ZZ1" in result["orphans_removed"] and "ZZ3" in result["orphans_removed"]
    assert "it is locked" in result["orphans_kept"]["ZZ2"]
    assert _digest(tmp_path / "first") == before and not (tmp_path / "out").exists()


def test_confirm_removes_only_the_removable_ones_and_leaves_the_source(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    before = _digest(tmp_path / "first")
    result = of.confirm_orphan_footprint_removal(board, proposal["revision_token"])
    assert result["status"] == "removed", result
    refs = _refs(Path(result["output_pcb"]))
    assert "ZZ1" not in refs and "ZZ3" not in refs and "ZZ2" in refs
    assert sorted(_refs(board)) == sorted(refs + proposal["orphans_removed"])
    assert _digest(tmp_path / "first") == before
    # the token is spent
    assert of.confirm_orphan_footprint_removal(board, proposal["revision_token"])["status"] == "error"


def test_copper_on_a_pad_net_keeps_the_footprint(board: Path, tmp_path: Path) -> None:
    net = _track_on(board, "ZZ3")
    result = _propose(board, tmp_path)
    assert "ZZ3" in result["orphans_kept"] and "copper on its nets" in result["orphans_kept"]["ZZ3"][0], (net, result)
    assert "ZZ3" not in result["orphans_removed"]


def test_nothing_to_do_issues_no_token(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    done = of.confirm_orphan_footprint_removal(board, proposal["revision_token"])
    again = of.propose_orphan_footprint_removal(done["output_pcb"], output_dir=tmp_path / "out2")
    assert again["status"] == "nothing_to_do" and again["revision_token"] is None
    assert "ZZ2" in again["orphans_kept"]


def test_drift_after_proposal_is_refused(board: Path, tmp_path: Path) -> None:
    proposal = _propose(board, tmp_path)
    _track_on(board, "ZZ3")
    result = of.confirm_orphan_footprint_removal(board, proposal["revision_token"])
    assert result["status"] == "error" and not (tmp_path / "out").exists()


def test_corrupted_write_is_caught_by_the_self_check(board: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Checklist item 14: the write drops one more footprint than planned; the reload check fires."""
    proposal = _propose(board, tmp_path)
    before = _digest(tmp_path / "first")
    real_save = write_layer.save

    def corrupt(root, path, **kw):
        extra = next(f for f in root.children if f.name == "footprint" and si._reference_of(f) == "ZZ2")
        root.children[:] = [c for c in root.children if c is not extra]
        return real_save(root, path, **kw)

    monkeypatch.setattr(write_layer, "save", corrupt)
    result = of.confirm_orphan_footprint_removal(board, proposal["revision_token"])
    assert result["ok"] is False and result["code"] == checkpoint.WRITE_NOT_VERIFIED, result
    assert not (tmp_path / "out").exists() and _digest(tmp_path / "first") == before


def test_corrupted_write_that_removes_nothing_is_caught(board: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    proposal = _propose(board, tmp_path)
    monkeypatch.setattr(write_layer, "save", lambda root, path, **kw: Path(path))  # the file keeps the orphans
    result = of.confirm_orphan_footprint_removal(board, proposal["revision_token"])
    assert result["ok"] is False and result["code"] == checkpoint.WRITE_NOT_VERIFIED, result
