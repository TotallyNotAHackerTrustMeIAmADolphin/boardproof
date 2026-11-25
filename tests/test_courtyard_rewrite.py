"""Issue 250: rewrite the courtyard of adopted footprints in the library and on the board (propose/confirm)."""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.schema import PCB

from boardproof import courtyard_rewrite as cr
from boardproof import footprint_courtyard as fc
from boardproof import write_layer

from tests.test_courtyard_hull import c1206


def make_project(tmp_path: Path, *, flipped: bool = False, ok_too: bool = False) -> Path:
    """A project with a bad C_1206 and two copies (one turned), optionally a good C_OK."""
    proj = tmp_path / "proj"
    pretty = proj / "lib" / "project_parts.pretty"
    fp = c1206(pretty / "C_1206.kicad_mod")
    ok = c1206(pretty / "C_OK.kicad_mod", "C_OK", courtyard=(-2.6, -1.1, 2.6, 1.1))
    board = PCB.create(width=60.0, height=40.0, title="rw")
    board.add_footprint_from_file(fp, "C1", 10.0, 10.0)
    board.add_footprint_from_file(fp, "C2", 30.0, 12.0, rotation=90.0)
    if ok_too:
        board.add_footprint_from_file(ok, "C3", 20.0, 30.0)
    path = proj / "rw.kicad_pcb"
    write_layer.save_pcb(board, path)
    root = write_layer.load(path)
    for node in root.children:
        if not node.is_atom and node.name == "footprint":
            node.children[0] = write_layer.string("project_parts:" + str(node.get_string(0)))
            if flipped and _ref(node) == "C2":
                node.find("layer").children[0] = write_layer.string("B.Cu")
    write_layer.save(root, path)
    return path


def _ref(node) -> str:
    return next(p.get_string(1) for p in node.find_all("property") if p.get_string(0) == "Reference")


def _copies(pcb: Path) -> dict:
    root = write_layer.load(pcb)
    return {_ref(n): n for n in root.children if not n.is_atom and n.name == "footprint"}


def _bytes(root: Path) -> dict:
    return {str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()}


def _propose(board: Path, tmp_path: Path, names=("C_1206",)) -> dict:
    found = cr.propose_courtyard_rewrite(board, list(names), output_dir=tmp_path / "out")
    assert found["status"] == "proposed", found
    return found


def test_propose_lists_before_after_and_copies_and_writes_nothing(tmp_path: Path) -> None:
    board = make_project(tmp_path, ok_too=True)
    before = _bytes(board.parent)
    found = _propose(board, tmp_path, ("C_1206", "C_OK", "C_NOPE"))
    entry = found["rewrite"][0]
    assert entry["footprint"] == "C_1206" and entry["courtyard_after"] == [-2.59, -1.12, 2.59, 1.12]
    assert entry["placed_copies"] == ["C1", "C2"] and entry["uncovered_pads"] == ["1", "2"]
    reasons = {r["footprint"]: r["reason"] for r in found["refused"]}
    assert "already encloses" in reasons["C_OK"] and "not in" in reasons["C_NOPE"]
    assert _bytes(board.parent) == before and not (tmp_path / "out").exists()


def test_confirm_rewrites_library_and_copies_without_moving_them(tmp_path: Path) -> None:
    board = make_project(tmp_path)
    src_bytes = _bytes(board.parent)
    found = _propose(board, tmp_path)
    old = _copies(board)
    result = cr.confirm_courtyard_rewrite(board, found["revision_token"])
    assert result["status"] == "rewritten", result
    assert _bytes(board.parent) == src_bytes  # the source project is untouched
    new_board = Path(result["output_pcb"])
    listed = fc.list_adopted(new_board)
    assert listed["status"] == "ok" and listed["violations"] == []
    new = _copies(new_board)
    for ref in ("C1", "C2"):
        assert write_layer.serialise(new[ref].find("at"), compact=True) == write_layer.serialise(old[ref].find("at"), compact=True)
        assert fc._courtyard(new[ref]) == (-2.59, -1.12, 2.59, 1.12)
        assert [p.find("at").values for p in new[ref].find_all("pad")] == [p.find("at").values for p in old[ref].find_all("pad")]


def test_a_flipped_copy_refuses_the_footprint_and_an_empty_list_is_refused(tmp_path: Path) -> None:
    board = make_project(tmp_path, flipped=True)
    found = cr.propose_courtyard_rewrite(board, ["C_1206"], output_dir=tmp_path / "out")
    assert found["status"] == "nothing_to_do" and "C2" in found["refused"][0]["reason"]
    assert cr.propose_courtyard_rewrite(board, [])["status"] == "error"
    assert cr.propose_courtyard_rewrite(board, ["../x"])["status"] == "error"


def test_a_copy_in_a_group_refuses_the_footprint(tmp_path: Path) -> None:
    board = make_project(tmp_path)
    root = write_layer.load(board)
    uid = str(next(n for n in root.children if not n.is_atom and n.name == "footprint").find("uuid").values[0])
    root.append(write_layer.SExp.list(
        "group", write_layer.string("g"), write_layer.SExp.list("uuid", write_layer.string("00000000-0000-0000-0000-0000000000aa")),
        write_layer.SExp.list("members", write_layer.string(uid)),
    ))  # fmt: skip
    write_layer.save(root, board)
    found = cr.propose_courtyard_rewrite(board, ["C_1206"], output_dir=tmp_path / "out")
    assert found["status"] == "nothing_to_do" and "group" in found["refused"][0]["reason"]


def test_a_stale_token_after_the_library_changes_is_refused(tmp_path: Path) -> None:
    board = make_project(tmp_path)
    found = _propose(board, tmp_path)
    c1206(board.parent / "lib" / "project_parts.pretty" / "C_1206.kicad_mod", courtyard=(-1.0, -0.5, 1.0, 0.5))
    result = cr.confirm_courtyard_rewrite(board, found["revision_token"])
    assert result["status"] == "error" and not (tmp_path / "out").exists()


def _confirm_refused(board: Path, tmp_path: Path) -> dict:
    src_bytes = _bytes(board.parent)
    found = _propose(board, tmp_path)
    result = cr.confirm_courtyard_rewrite(board, found["revision_token"])
    assert result["status"] == "error" and result.get("ok") is False and result["code"] == "write_not_verified", result
    assert _bytes(board.parent) == src_bytes and not (tmp_path / "out").exists()
    return result


def test_a_courtyard_written_too_small_is_caught(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    real = fc.apply_box
    monkeypatch.setattr(fc, "apply_box", lambda root, box: real(root, [box[0] + 1, box[1], box[2] - 1, box[3]]))
    _confirm_refused(board, tmp_path)


def test_a_copy_that_moves_is_caught(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    real = fc.apply_box

    def moving(root, box):
        real(root, box)
        if root.name == "footprint" and root.find("at") is not None and root.get_string(0).startswith("project_parts:"):
            root.find("at").children[0] = write_layer.number(float(root.find("at").values[0]) + 0.5)

    monkeypatch.setattr(fc, "apply_box", moving)
    _confirm_refused(board, tmp_path)


def test_a_changed_node_outside_the_courtyards_is_caught(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    real = write_layer.save

    def dropping(root, path, *a, **k):
        if str(path).endswith(".kicad_pcb"):
            root.children[:] = [c for c in root.children if c.is_atom or c.name != "setup"]
        return real(root, path, *a, **k)

    monkeypatch.setattr(write_layer, "save", dropping)
    _confirm_refused(board, tmp_path)


def test_a_failure_in_the_write_leaves_the_source_alone(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    src_bytes = _bytes(board.parent)
    found = _propose(board, tmp_path)
    monkeypatch.setattr(fc, "fix", lambda root, margin=0.25: (_ for _ in ()).throw(RuntimeError("boom")))
    result = cr.confirm_courtyard_rewrite(board, found["revision_token"])
    assert result["status"] == "incomplete" and _bytes(board.parent) == src_bytes and not (tmp_path / "out").exists()


@pytest.mark.parametrize("flip", [False])
def test_nothing_to_do_for_a_good_library(tmp_path: Path, flip: bool) -> None:
    board = make_project(tmp_path, ok_too=True)
    assert cr.propose_courtyard_rewrite(board, ["C_OK"])["status"] == "nothing_to_do"


# ---- each self-check alone: every corruption below is caught by exactly one check (mutation-tested one by one)


def _on_board(root) -> bool:
    return root.name == "footprint" and root.get_string(0).startswith("project_parts:")


def test_a_library_courtyard_written_too_small_is_caught_by_the_library_check_alone(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    real = fc.fix

    def small(root, margin=0.25):
        box = real(root, margin)
        fc.apply_box(root, [box[0] + 1, box[1], box[2] - 1, box[3]])  # the copies still get the right box
        return box

    monkeypatch.setattr(fc, "fix", small)
    _confirm_refused(board, tmp_path)


def test_a_copy_courtyard_written_too_small_is_caught_by_the_copy_check_alone(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    real = fc.apply_box
    monkeypatch.setattr(fc, "apply_box", lambda root, box: real(root, [box[0] + 1, box[1], box[2] - 1, box[3]] if _on_board(root) else box))
    _confirm_refused(board, tmp_path)


def test_a_top_level_node_changed_is_caught_by_the_node_comparison_alone(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    real = write_layer.save

    def changing(root, path, *a, **k):
        if str(path).endswith(".kicad_pcb"):
            root.find("version").children[0] = write_layer.number(1)  # same node count, same footprints
        return real(root, path, *a, **k)

    monkeypatch.setattr(write_layer, "save", changing)
    _confirm_refused(board, tmp_path)


def test_a_dropped_last_node_is_caught_by_the_node_count_alone(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    real = write_layer.save

    def dropping(root, path, *a, **k):
        if str(path).endswith(".kicad_pcb"):
            root.children[:] = root.children[:-1]  # every remaining index still lines up
        return real(root, path, *a, **k)

    monkeypatch.setattr(write_layer, "save", dropping)
    _confirm_refused(board, tmp_path)


def test_a_library_node_changed_outside_the_courtyard_is_caught_by_the_stripped_check_alone(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    real = fc.fix

    def touching(root, margin=0.25):
        box = real(root, margin)
        root.find("version").children[0] = write_layer.number(1)  # the courtyard stays right, the pads stay as they are
        return box

    monkeypatch.setattr(fc, "fix", touching)
    _confirm_refused(board, tmp_path)


def test_a_plan_that_drifts_between_propose_and_confirm_is_refused_by_the_digest_alone(tmp_path: Path, monkeypatch) -> None:
    board = make_project(tmp_path)
    src_bytes = _bytes(board.parent)
    found = _propose(board, tmp_path)
    real = cr._plan

    def drifting(*a, **k):
        plan = real(*a, **k)
        plan["rewrite"][0]["placed_copies"] = [*plan["rewrite"][0]["placed_copies"], "C99"]  # the source files are unchanged
        return plan

    monkeypatch.setattr(cr, "_plan", drifting)
    result = cr.confirm_courtyard_rewrite(board, found["revision_token"])
    assert result["status"] == "error" and result.get("stale") is True, result
    assert _bytes(board.parent) == src_bytes and not (tmp_path / "out").exists()


def test_a_copy_with_a_larger_courtyard_keeps_it(tmp_path: Path) -> None:
    board = make_project(tmp_path)
    root = write_layer.load(board)
    c1 = next(n for n in root.children if not n.is_atom and n.name == "footprint" and _ref(n) == "C1")
    fc.apply_box(c1, [-4.0, -2.0, 4.0, 2.0])  # larger than the library box on every side
    write_layer.save(root, board)
    result = cr.confirm_courtyard_rewrite(board, _propose(board, tmp_path)["revision_token"])
    assert result["status"] == "rewritten", result
    new = _copies(Path(result["output_pcb"]))
    assert fc._courtyard(new["C1"]) == (-4.0, -2.0, 4.0, 2.0)
    assert fc._courtyard(new["C2"]) == (-2.59, -1.12, 2.59, 1.12)


@pytest.mark.parametrize("linked", ["other_lib:C_1206", "C_1206"])
def test_a_copy_under_another_library_prefix_refuses_the_footprint(tmp_path: Path, linked: str) -> None:
    board = make_project(tmp_path)
    root = write_layer.load(board)
    c2 = next(n for n in root.children if not n.is_atom and n.name == "footprint" and _ref(n) == "C2")
    c2.children[0] = write_layer.string(linked)
    write_layer.save(root, board)
    found = cr.propose_courtyard_rewrite(board, ["C_1206"], output_dir=tmp_path / "out")
    assert found["status"] == "nothing_to_do", found
    assert "C2" in found["refused"][0]["reason"] and "another library prefix" in found["refused"][0]["reason"]
