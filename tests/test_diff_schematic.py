"""diff_schematic (GitHub issues 98 and 125): the symbol and net differences between two states of a schematic project.

Two layers. The logic (symbol matching, net renaming, the limit) is tested on prepared records, no kicad-cli.
The tool is tested on the rename fixture (tests/rename_fixture.py: root plus one sub sheet, built through the
tools) kept in a git repository one folder above the project, changed through the tools between two commits.

Mutants, each shown red by a test named in its docstring: matching by reference instead of uuid, a pair by guess on
a duplicated key, the reference translation dropped, a net with no pins paired as a rename, the limit ignored, the
tolerance of a move, a bad revision accepted, a state that does not load compared anyway, a failed netlist read as
"no net changes", the working tree or index touched by a revision read, option injection through a revision.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from boardproof import kicad_cli, schematic_diff, sheet_editing
from tests import rename_fixture as F
from tests._hermetic_git import apply_hermetic_env, run_git

# ------------------------------------------------------------------------------------------ unit: symbols


def rec(uuid: str, ref: str, *, unit: str = "1", value: str = "1k", at: tuple[float, float] = (10.0, 20.0), rotation: float = 0.0, props: dict | None = None, **over) -> dict:
    base = {"uuid": uuid, "reference": ref, "refs": [ref], "unit": unit, "lib_id": "Device:R", "value": value, "footprint": "", "dnp": "no",
            "on_board": "yes", "mirror": "", "at": at, "rotation": rotation, "properties": props or {}, "uses": 1}
    return {**base, **over}


def sym(before: list[dict], after: list[dict], limit: int = 200, sheet: str = "a.kicad_sch"):
    return schematic_diff.diff_symbols({sheet: before}, {sheet: after}, limit)


def test_symbols_added_removed_changed_and_moved_are_told_apart() -> None:
    sheets, totals, _ = sym(
        [rec("u1", "R1"), rec("u2", "R2"), rec("u3", "R3", value="1k"), rec("u4", "R4", at=(0, 0))],
        [rec("u1", "R1"), rec("u3", "R3", value="2k", props={"MPN": "X"}), rec("u4", "R4", at=(3, 4)), rec("u5", "R5")])
    (entry,) = sheets
    assert [s["reference"] for s in entry["added"]] == ["R5"] and [s["reference"] for s in entry["removed"]] == ["R2"]
    (changed,) = entry["changed"]
    assert changed["reference"] == "R3"
    assert changed["changes"] == [{"field": "value", "before": "1k", "after": "2k"}, {"field": "property:MPN", "before": None, "after": "X"}]
    (moved,) = entry["moved"]
    assert moved["reference"] == "R4" and moved["distance_mm"] == 5.0 and moved["from"] == [0, 0] and moved["to"] == [3, 4]
    assert totals["added"] == totals["removed"] == totals["changed"] == totals["moved"] == 1
    assert totals["largest_move_mm"] == 5.0 and entry["largest_move_mm"] == 5.0
    assert entry["counts"] == {"added": 1, "removed": 1, "changed": 1, "moved": 1}


def test_a_symbol_is_matched_by_uuid_not_by_reference() -> None:
    """Mutant: matching by reference first. A rename keeps the uuid: one change of the reference, not a removal plus an addition."""
    sheets, totals, ref_map = sym([rec("u1", "R1")], [rec("u1", "R9")])
    assert totals["added"] == totals["removed"] == 0 and totals["changed"] == 1
    assert sheets[0]["changed"][0]["changes"] == [{"field": "reference", "before": "R1", "after": "R9"}]
    assert ref_map == {"R1": "R9"}


def test_the_leftovers_are_paired_by_reference_and_unit_and_say_the_uuid_changed() -> None:
    sheets, totals, _ = sym([rec("old", "R1")], [rec("new", "R1")])
    assert totals["changed"] == 1 and totals["added"] == totals["removed"] == 0
    assert sheets[0]["changed"][0]["changes"] == [{"field": "uuid", "before": "old", "after": "new"}]


def test_a_duplicated_key_is_never_paired_by_guess() -> None:
    """Mutant: pairing the first of two equal keys. Two symbols with one reference and unit on one side stay unpaired."""
    _, totals, _ = sym([rec("a", "R1"), rec("b", "R1")], [rec("c", "R1")])
    assert totals["added"] == 1 and totals["removed"] == 2 and totals["changed"] == 0


def test_a_duplicated_key_on_the_other_side_is_not_paired_either() -> None:
    _, totals, _ = sym([rec("c", "R1")], [rec("a", "R1"), rec("b", "R1")])
    assert totals["added"] == 2 and totals["removed"] == 1 and totals["changed"] == 0


def test_two_symbols_that_swap_references_are_two_reference_changes_not_two_new_uuids() -> None:
    """Mutant: reference matched before uuid (the swap would read as a uuid change on each)."""
    sheets, totals, _ = sym([rec("a", "R1", value="1k"), rec("b", "R2", value="2k")], [rec("a", "R2", value="1k"), rec("b", "R1", value="2k")])
    assert totals["changed"] == 2 and all([c["field"] for c in e["changes"]] == ["reference"] for e in sheets[0]["changed"])


def test_different_units_of_one_reference_are_different_symbols() -> None:
    _, totals, _ = sym([rec("a", "U1", unit="1"), rec("b", "U1", unit="2")], [rec("a", "U1", unit="1"), rec("b", "U1", unit="2", value="x")])
    assert totals["changed"] == 1 and totals["added"] == totals["removed"] == 0


def test_a_move_under_the_tolerance_is_not_a_move_and_a_turn_is() -> None:
    """Mutant: tolerance 0 (float noise reads as a move) or turns ignored."""
    _, totals, _ = sym([rec("a", "R1"), rec("b", "R2")], [rec("a", "R1", at=(10.0004, 20.0)), rec("b", "R2", rotation=90.0)])
    assert totals["moved"] == 1 and totals["largest_move_mm"] == 0.0
    sheets, _, _ = sym([rec("b", "R2", rotation=0.0)], [rec("b", "R2", rotation=360.0)])
    assert sheets == []  # 0 and 360 degrees are the same turn


def test_a_new_sheet_lists_all_its_symbols_added_and_an_unchanged_sheet_is_only_counted() -> None:
    sheets, totals, _ = schematic_diff.diff_symbols({"a.kicad_sch": [rec("1", "R1")]}, {"a.kicad_sch": [rec("1", "R1")], "b.kicad_sch": [rec("2", "R2")]}, 200)
    assert [(s["sheet"], s["status"]) for s in sheets] == [("b.kicad_sch", "added")]
    assert totals["sheets_changed"] == 1 and totals["sheets_unchanged"] == 1
    gone, _, _ = schematic_diff.diff_symbols({"b.kicad_sch": [rec("2", "R2")]}, {}, 200)
    assert gone[0]["status"] == "removed" and gone[0]["counts"]["removed"] == 1


def test_the_limit_caps_the_listing_but_never_the_counts() -> None:
    """Mutant: the limit ignored."""
    sheets, totals, _ = sym([], [rec(f"u{i}", f"R{i:02d}") for i in range(7)], limit=3)
    assert len(sheets[0]["added"]) == 3 and sheets[0]["counts"]["added"] == 7 and totals["added"] == 7
    assert totals["truncated_lists"] == ["a.kicad_sch:added"]
    moved, _, _ = sym([rec(f"u{i}", f"R{i}", at=(0, 0)) for i in range(5)], [rec(f"u{i}", f"R{i}", at=(i + 1, 0)) for i in range(5)], limit=2)
    assert [m["distance_mm"] for m in moved[0]["moved"]] == [5.0, 4.0]  # the largest moves are kept


# ------------------------------------------------------------------------------------------ unit: nets


def net(*pins: str) -> frozenset:
    return frozenset((p.split(".")[0], p.split(".")[1]) for p in pins)


def test_a_net_with_the_same_pins_under_another_name_is_renamed() -> None:
    out = schematic_diff.diff_nets({"/A": net("R1.1", "R2.1")}, {"/B": net("R1.1", "R2.1")}, {}, 200)
    assert out["renamed"] == [{"before": "/A", "after": "/B", "pins": ["R1.1", "R2.1"], "sheet_path": "/", "auto_before": False, "auto_after": False}]
    assert out["counts"] == {"added": 0, "removed": 0, "renamed": 1, "members_changed": 0}


def test_a_generated_name_that_follows_its_reference_is_counted_not_listed_as_a_rename() -> None:
    """Mutant: listing every pair of same pins. Net-(R2-Pad2) -> Net-(R7-Pad2) is the renumbered part, not a net rename; auto to named is."""
    out = schematic_diff.diff_nets({"Net-(R2-Pad2)": net("R2.2"), "Net-(R1-Pad1)": net("R1.1")}, {"Net-(R7-Pad2)": net("R7.2"), "/X": net("R1.1")}, {"R2": "R7"}, 200)
    assert out["auto_renamed"] == 1 and [(n["before"], n["after"]) for n in out["renamed"]] == [("Net-(R1-Pad1)", "/X")]


def test_other_pins_under_another_name_is_a_removal_and_an_addition_not_a_rename() -> None:
    out = schematic_diff.diff_nets({"/A": net("R1.1")}, {"/B": net("R1.1", "R2.1")}, {}, 200)
    assert out["counts"] == {"added": 1, "removed": 1, "renamed": 0, "members_changed": 0}


def test_same_name_other_pins_is_members_changed_with_the_pins_each_way() -> None:
    out = schematic_diff.diff_nets({"/A": net("R1.1", "R2.1")}, {"/A": net("R1.1", "R3.1")}, {}, 200)
    assert out["members_changed"] == [{"net": "/A", "pins_added": ["R3.1"], "pins_removed": ["R2.1"], "sheet_path": "/", "auto": False}]


def test_a_renumbered_part_is_not_a_net_change() -> None:
    """Mutant: the reference translation dropped. R2 became R9; its net keeps the same pin, so nothing changed."""
    before, after = {"/A": net("R1.1", "R2.1")}, {"/A": net("R1.1", "R9.1")}
    assert schematic_diff.diff_nets(before, after, {"R2": "R9"}, 200)["counts"] == {"added": 0, "removed": 0, "renamed": 0, "members_changed": 0}
    assert schematic_diff.diff_nets(before, after, {}, 200)["counts"]["members_changed"] == 1


def test_a_net_with_no_pins_is_never_paired_as_a_rename() -> None:
    """Mutant: pairing on an empty pin set (every empty net would be 'the same net')."""
    out = schematic_diff.diff_nets({"/A": frozenset()}, {"/B": frozenset()}, {}, 200)
    assert out["counts"] == {"added": 1, "removed": 1, "renamed": 0, "members_changed": 0}


def test_net_sheet_paths_auto_names_and_global_nets() -> None:
    out = schematic_diff.diff_nets({}, {"/Sub/X": net("R1.1"), "GND": net("R2.1"), "Net-(R3-Pad1)": net("R3.1")}, {}, 200)
    paths = {n["net"]: (n["sheet_path"], n["auto"]) for n in out["added"]}
    assert paths == {"/Sub/X": ("/Sub/", False), "GND": ("", False), "Net-(R3-Pad1)": ("", True)}
    assert out["by_sheet_path"] == {"(global)": {"added": 2, "removed": 0, "renamed": 0, "members_changed": 0},
                                    "/Sub/": {"added": 1, "removed": 0, "renamed": 0, "members_changed": 0}}


def test_the_net_limit_caps_the_listing_not_the_counts() -> None:
    out = schematic_diff.diff_nets({}, {f"/N{i}": net(f"R{i}.1") for i in range(5)}, {}, 2)
    assert len(out["added"]) == 2 and out["counts"]["added"] == 5 and out["truncated_lists"] == ["added"]


# ------------------------------------------------------------------------------------------ git states without kicad-cli


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    """A repository with the project in the sub folder ``proj``: two commits of plain text files in the places the reader looks."""
    apply_hermetic_env(monkeypatch, identity=True)
    run_git("init", "-q", cwd=tmp_path)
    folder = tmp_path / "proj"
    (folder / "sub").mkdir(parents=True)
    files = {"demo.kicad_sch": "root-1", "demo.kicad_pro": "pro-1", "sym-lib-table": "t1", "fp-lib-table": "f1", "sub/child.kicad_sch": "child-1",
             "notes.txt": "x", "sub/deep.kicad_pro": "no", "sub/sym-lib-table": "no"}
    for name, text in files.items():
        (folder / name).write_text(text, encoding="utf-8")
    run_git("add", "-A", cwd=tmp_path)
    run_git("commit", "-q", "-m", "one", cwd=tmp_path)
    (folder / "demo.kicad_sch").write_text("root-2", encoding="utf-8")
    run_git("commit", "-q", "-am", "two", cwd=tmp_path)
    return folder


def _tree(folder: Path) -> dict[str, bytes]:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def test_a_revision_is_rebuilt_from_the_object_database_with_only_the_files_the_reader_needs(repo, tmp_path) -> None:
    dest = tmp_path / "out"
    dest.mkdir()
    schematic_diff.materialise(repo, "HEAD~1", dest)
    assert _tree(dest) == {"demo.kicad_sch": b"root-1", "demo.kicad_pro": b"pro-1", "sym-lib-table": b"t1", "fp-lib-table": b"f1", "sub/child.kicad_sch": b"child-1"}


def test_reading_a_revision_leaves_the_working_tree_and_the_index_alone(repo, tmp_path) -> None:
    """Mutant: a checkout of the revision (the working file would read root-1)."""
    index = (repo.parent / ".git" / "index").read_bytes()
    before = _tree(repo)
    dest = tmp_path.parent / (tmp_path.name + "-out")
    dest.mkdir()
    schematic_diff.materialise(repo, "HEAD~1", dest)
    assert _tree(repo) == before and (repo.parent / ".git" / "index").read_bytes() == index
    assert run_git("status", "--porcelain", cwd=repo.parent).stdout == b""


def test_a_revision_that_is_no_commit_is_an_error_and_an_option_is_refused(repo, tmp_path) -> None:
    """Mutants: a bad revision accepted; a leading dash passed on to git."""
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(schematic_diff._GitError, match="does not name a commit"):
        schematic_diff.materialise(repo, "no-such-branch", dest)
    for bad in ("--output=x", "-h", "a b", "", "x\ny", "HEAD\n", "a" * 300):
        result = schematic_diff.diff_schematic(str(repo / "demo.kicad_sch"), before_revision=bad)
        assert result["ok"] is False and result["code"] == "invalid_spec", bad


def test_the_file_count_bound_is_enforced(repo, tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(schematic_diff, "MAX_REVISION_FILES", 2)
    dest = tmp_path / "out"
    dest.mkdir()
    with pytest.raises(schematic_diff._GitError, match="limit is 2"):
        schematic_diff.materialise(repo, "HEAD", dest)


def test_asking_for_nothing_to_compare_is_refused(repo) -> None:
    sheet = str(repo / "demo.kicad_sch")
    assert schematic_diff.diff_schematic(sheet)["code"] == "nothing_to_compare"
    assert schematic_diff.diff_schematic(sheet, sheet)["code"] == "nothing_to_compare"
    assert schematic_diff.diff_schematic(sheet, None, "HEAD", "HEAD")["code"] == "nothing_to_compare"
    assert schematic_diff.diff_schematic(sheet, limit=0, before_revision="HEAD")["code"] == "invalid_spec"
    assert schematic_diff.diff_schematic(str(repo / "missing.kicad_sch"), before_revision="HEAD")["code"] == "file_not_found"


def _commit_entries(repo: Path) -> None:
    """Add a symbolic link and a submodule entry named like sheet files to the index without touching the file system, and commit."""
    oid = run_git("hash-object", "-w", "--stdin", cwd=repo.parent, input=b"demo.kicad_sch").stdout.decode().strip()
    run_git("update-index", "--add", "--cacheinfo", f"120000,{oid},proj/link.kicad_sch", cwd=repo.parent)
    run_git("update-index", "--add", "--cacheinfo", f"160000,{'a' * 40},proj/mod/inner.kicad_sch", cwd=repo.parent)
    run_git("commit", "-q", "-m", "links", cwd=repo.parent)


def test_a_symbolic_link_and_a_submodule_entry_are_skipped(repo, tmp_path) -> None:
    """Mutants: the mode filter or the kind filter removed (the link's target text would be written as a sheet; the submodule has no object to read)."""
    _commit_entries(repo)
    dest = tmp_path.parent / (tmp_path.name + "-out")
    dest.mkdir()
    schematic_diff.materialise(repo, "HEAD", dest)
    assert sorted(_tree(dest)) == ["demo.kicad_pro", "demo.kicad_sch", "fp-lib-table", "sub/child.kicad_sch", "sym-lib-table"]


def test_the_byte_bound_is_checked_from_the_listing(repo, tmp_path, monkeypatch) -> None:
    """Mutant: the byte bound removed."""
    monkeypatch.setattr(schematic_diff, "MAX_REVISION_BYTES", 10)
    calls = []
    real = schematic_diff._git
    monkeypatch.setattr(schematic_diff, "_git", lambda cwd, *a, **k: (calls.append(a[0]), real(cwd, *a, **k))[1])
    dest = tmp_path.parent / (tmp_path.name + "-out")
    dest.mkdir()
    with pytest.raises(schematic_diff._GitError, match="exceed"):
        schematic_diff.materialise(repo, "HEAD", dest)
    assert "cat-file" not in calls and not list(dest.iterdir())  # refused before any content was read


def test_git_runs_with_a_hermetic_environment(repo, tmp_path, monkeypatch) -> None:
    """Mutants: GIT_OPTIONAL_LOCKS dropped; a GIT_DIR or GIT_INDEX_FILE of the caller passed on (it would read another repository)."""
    other = tmp_path.parent / (tmp_path.name + "-other")
    other.mkdir()
    run_git("init", "-q", cwd=other)
    (other / "demo.kicad_sch").write_text("other", encoding="utf-8")
    run_git("add", "-A", cwd=other)
    run_git("commit", "-q", "-m", "o", cwd=other)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / "idx"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.bare")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "true")
    monkeypatch.delenv("GIT_CONFIG_NOSYSTEM", raising=False)
    monkeypatch.delenv("GIT_CONFIG_SYSTEM", raising=False)
    seen = []
    real = schematic_diff.subprocess.run

    def spy(cmd, **kw):
        seen.append(kw["env"])
        return real(cmd, **kw)

    monkeypatch.setattr(schematic_diff.subprocess, "run", spy)
    dest = tmp_path.parent / (tmp_path.name + "-out")
    dest.mkdir()
    schematic_diff.materialise(repo, "HEAD~1", dest)
    assert _tree(dest)["demo.kicad_sch"] == b"root-1"
    assert seen and all(e["GIT_OPTIONAL_LOCKS"] == "0" and e["GIT_CONFIG_NOSYSTEM"] == "1" and e["GIT_CONFIG_GLOBAL"] == os.devnull for e in seen)
    assert all(not {"GIT_DIR", "GIT_INDEX_FILE", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"} & set(e) and e["GIT_CONFIG_COUNT"] == "0" for e in seen)


def test_a_cat_file_answer_that_is_not_the_listed_blob_is_an_incomplete_state_not_a_crash(repo, monkeypatch) -> None:
    """Mutant: the header check dropped (a 'missing' answer would be read as content or crash)."""
    real = schematic_diff._git

    def fake(cwd, *args, stdin=None):
        if args[0] == "cat-file":
            return b"".join(f"{line} missing\n".encode() for line in stdin.decode().split())
        return real(cwd, *args, stdin=stdin)

    monkeypatch.setattr(schematic_diff, "_git", fake)
    out = schematic_diff.diff_schematic(str(repo / "demo.kicad_sch"), before_revision="HEAD~1")
    assert out["ok"] is False and out["compared"] is False and out["states"][0]["loaded"] is False and "did not return" in out["reason"]
    monkeypatch.setattr(schematic_diff, "_git", lambda cwd, *a, stdin=None: b"" if a[0] == "cat-file" else real(cwd, *a, stdin=stdin))
    assert "did not return" in schematic_diff.diff_schematic(str(repo / "demo.kicad_sch"), before_revision="HEAD~1")["reason"]


def _fake_git(monkeypatch, tool: str, change) -> None:
    """Run the real git, but pass the output of one subcommand through ``change``."""
    real = schematic_diff._git
    monkeypatch.setattr(schematic_diff, "_git", lambda cwd, *a, stdin=None: change(real(cwd, *a, stdin=stdin)) if a[0] == tool else real(cwd, *a, stdin=stdin))


def _incomplete(repo, text: str) -> None:
    out = schematic_diff.diff_schematic(str(repo / "demo.kicad_sch"), before_revision="HEAD~1")
    assert out["ok"] is False and out["compared"] is False and out["states"][0]["loaded"] is False and text in out["reason"], out


def test_a_cat_file_header_with_another_size_than_listed_is_an_incomplete_state(repo, monkeypatch) -> None:
    """Mutant: the size comparison of the header with the ls-tree size dropped."""
    import re

    _fake_git(monkeypatch, "cat-file", lambda out: re.sub(rb" blob (\d+)\n", lambda m: b" blob " + str(int(m.group(1)) + 1).encode() + b"\n", out))
    _incomplete(repo, "did not return")


def test_a_cat_file_answer_of_another_type_is_an_incomplete_state(repo, monkeypatch) -> None:
    """Mutant: the object type check dropped (right size, but a tree)."""
    _fake_git(monkeypatch, "cat-file", lambda out: out.replace(b" blob ", b" tree "))
    _incomplete(repo, "did not return")


def test_an_ls_tree_line_without_a_size_is_an_incomplete_state_not_a_crash(repo, monkeypatch) -> None:
    """Mutant: the line-format guard dropped (a ValueError would escape the tool)."""
    _fake_git(monkeypatch, "ls-tree", lambda out: b"100644 blob " + b"a" * 40 + b"\tdemo.kicad_sch\0")
    _incomplete(repo, "does not understand")


def test_git_refusing_a_repository_of_another_owner_names_the_remedy_and_does_not_apply_it(repo, monkeypatch) -> None:
    """The reason carries the command the owner can run; the tool itself never sets safe.directory."""
    import subprocess

    seen = []

    def refuse(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 128, b"", b"fatal: detected dubious ownership in repository at 'X'\n")

    monkeypatch.setattr(schematic_diff.subprocess, "run", refuse)
    out = schematic_diff.diff_schematic(str(repo / "demo.kicad_sch"), before_revision="HEAD~1")
    assert out["ok"] is False and out["states"][0]["loaded"] is False
    assert "dubious ownership" in out["reason"] and "git config --global --add safe.directory" in out["reason"] and repo.as_posix() in out["reason"]
    assert "does not name a commit" not in out["reason"]
    assert not any("safe.directory" in " ".join(c) for c in seen)


def test_a_short_cat_file_body_is_an_error(repo, tmp_path, monkeypatch) -> None:
    """Mutant: the body length check dropped (a truncated answer would be written as a sheet)."""
    real = schematic_diff._git
    monkeypatch.setattr(schematic_diff, "_git", lambda cwd, *a, stdin=None: real(cwd, *a, stdin=stdin)[:-2] if a[0] == "cat-file" else real(cwd, *a, stdin=stdin))
    dest = tmp_path.parent / (tmp_path.name + "-out")
    dest.mkdir()
    with pytest.raises(schematic_diff._GitError, match="short"):
        schematic_diff.materialise(repo, "HEAD", dest)


def test_a_revision_that_names_a_tree_is_not_a_commit(repo, tmp_path) -> None:
    """Mutant: ^{commit} dropped from the verification."""
    dest = tmp_path.parent / (tmp_path.name + "-out")
    dest.mkdir()
    with pytest.raises(schematic_diff._GitError, match="does not name a commit"):
        schematic_diff.materialise(repo, "HEAD^{tree}", dest)


def test_a_named_net_that_becomes_a_generated_name_is_a_removal_and_an_addition() -> None:
    """Mutant: any same-pin pair is a rename (VCC to unconnected-(R1-Pad1) when the power symbol goes)."""
    out = schematic_diff.diff_nets({"VCC": net("R1.1")}, {"unconnected-(R1-Pad1)": net("R1.1")}, {}, 200)
    assert out["counts"] == {"added": 1, "removed": 1, "renamed": 0, "members_changed": 0}
    assert out["removed"][0]["net"] == "VCC" and out["added"][0]["auto"] is True


# ------------------------------------------------------------------------------------------ the tool, with kicad-cli


@pytest.fixture(scope="module")
def _cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture(scope="module")
def history(tmp_path_factory, _cli):
    """The rename fixture in a repository (project in ``proj/``): revision ``one`` as built, revision ``two`` after changes through
    the tools, ``three`` after the sub sheet is deleted. The working tree stays at ``two``. Returns (root sheet, revisions, copy of one)."""
    mp = pytest.MonkeyPatch()
    apply_hermetic_env(mp, identity=True)
    try:
        base = tmp_path_factory.mktemp("diff")
        root = F.build(base)
        run_git("init", "-q", cwd=base)
        run_git("add", "-A", cwd=base)
        run_git("commit", "-q", "-m", "one", cwd=base)
        one_copy = F.copy_of(root, base.parent / (base.name + "-one"))
        t = F.tools()
        assert t["rename_net"](str(root), [{"old": "/SDA", "new": "I2C"}])["status"] == "ok"
        assert t["rename_ref"](str(root), [{"old": "R2", "new": "R7"}])["status"] == "ok"
        assert t["set_properties"](str(root), [{"reference": "R1", "set": {"Value": "22k", "MPN": "ABC"}}])["status"] == "ok"
        assert t["add_symbols"](str(root), [{"lib_id": "Device:R", "x": 127.0, "y": 127.0, "ref": "R4", "value": "5k"}])["status"] == "ok"
        run_git("commit", "-q", "-am", "two", cwd=base)
        sub = root.parent / "sub.kicad_sch"
        run_git("rm", "-q", sub.name, cwd=root.parent)
        run_git("commit", "-q", "-m", "three", cwd=base)
        run_git("checkout", "-q", "HEAD~1", "--", "proj/sub.kicad_sch", cwd=base)
        run_git("reset", "-q", cwd=base)
        assert sub.is_file()
        revs = {"one": "HEAD~2", "two": "HEAD~1", "three": "HEAD"}
        yield root, revs, one_copy
    finally:
        mp.undo()


def test_two_revisions_give_the_symbol_and_net_differences(history) -> None:
    root, revs, _ = history
    out = schematic_diff.diff_schematic(str(root), before_revision=revs["one"], after_revision=revs["two"])
    assert out["status"] == "ok" and out["ok"] is True and out["compared"] is True and out["written"] is False, out
    assert [s["loaded"] for s in out["states"]] == [True, True]
    (entry,) = out["sheets"]
    assert entry["sheet"] == "demo.kicad_sch" and entry["status"] == "present"
    assert [s["reference"] for s in entry["added"]] == ["R4"] and entry["removed"] == []
    by_ref = {c["reference"]: c for c in entry["changed"]}
    assert set(by_ref) == {"R1", "R7"}
    assert {c["field"]: (c["before"], c["after"]) for c in by_ref["R1"]["changes"]} == {"value": ("10k", "22k"), "property:MPN": (None, "ABC")}
    assert by_ref["R7"]["before_reference"] == "R2" and by_ref["R7"]["changes"] == [{"field": "reference", "before": "R2", "after": "R7"}]
    assert out["reference_changes"] == {"R2": "R7"}
    # nets: /SDA became /I2C (same pins), nothing else; the renumbered R2 stays on GL
    assert [(n["before"], n["after"]) for n in out["nets"]["renamed"]] == [("/SDA", "/I2C")]
    assert out["nets"]["added"] == [] or all(n["auto"] for n in out["nets"]["added"])
    assert out["nets"]["members_changed"] == []
    assert out["no_symbol_or_net_differences"] is False and out["truncated"] is False
    assert out["totals"]["symbols"]["added"] == 1 and out["totals"]["nets"]["renamed"] == 1


def test_a_revision_against_the_working_files_equals_the_two_revision_diff(history) -> None:
    root, revs, _ = history
    against_two = schematic_diff.diff_schematic(str(root), before_revision=revs["one"])
    both = schematic_diff.diff_schematic(str(root), before_revision=revs["one"], after_revision=revs["two"])
    for key in ("sheets", "nets", "totals", "reference_changes"):
        assert against_two[key] == both[key], key


def test_two_paths_give_the_same_difference_as_the_two_revisions(history) -> None:
    root, revs, one_copy = history
    by_paths = schematic_diff.diff_schematic(str(one_copy), str(root))
    by_rev = schematic_diff.diff_schematic(str(root), before_revision=revs["one"], after_revision=revs["two"])
    for key in ("sheets", "nets", "totals", "reference_changes"):
        assert by_paths[key] == by_rev[key], key


def test_two_equal_copies_are_identical(history) -> None:
    root, revs, one_copy = history
    twin = one_copy.parent.parent / "twin"
    shutil.copytree(one_copy.parent, twin, dirs_exist_ok=True)
    same = schematic_diff.diff_schematic(str(one_copy), str(twin / one_copy.name))
    assert same["ok"] is True and same["no_symbol_or_net_differences"] is True and same["sheets"] == []
    assert same["totals"]["nets"] == {"added": 0, "removed": 0, "renamed": 0, "members_changed": 0}


def test_a_net_only_change_is_not_identical(history, tmp_path) -> None:
    """Mutant: identical judged on the symbols alone. Only a net is renamed; no symbol differs."""
    _, _, one_copy = history
    work = F.copy_of(one_copy, tmp_path / "netonly")
    assert F.tools()["rename_net"](str(work), [{"old": "/Sub/LOC", "new": "LOCX"}])["status"] == "ok"
    out = schematic_diff.diff_schematic(str(one_copy), str(work))
    assert out["ok"] is True and out["sheets"] == [] and out["totals"]["symbols"]["sheets_changed"] == 0
    assert [(n["before"], n["after"]) for n in out["nets"]["renamed"]] == [("/Sub/LOC", "/Sub/LOCX")]
    assert out["nets"]["by_sheet_path"] == {"/Sub/": {"added": 0, "removed": 0, "renamed": 1, "members_changed": 0}}
    assert out["no_symbol_or_net_differences"] is False


def test_a_text_and_a_dangling_wire_are_not_seen_and_the_result_says_so(history, tmp_path) -> None:
    """Mutant: the flag read as 'identical'. A note and a wire that joins nothing change neither list; the result must not claim the sheets are the same."""
    _, _, one_copy = history
    work = F.copy_of(one_copy, tmp_path / "unseen")
    t = F.tools()
    assert t["add_text"](str(work), [{"x": 150.0, "y": 150.0, "text": "a note"}])["status"] == "ok"
    assert t["add_wires"](str(work), [{"p1": [150.0, 170.0], "p2": [170.0, 170.0], "route": "direct", "snap": False}])["status"] == "ok"
    out = schematic_diff.diff_schematic(str(one_copy), str(work))
    assert out["ok"] is True and out["no_symbol_or_net_differences"] is True and "identical" not in out
    assert any("texts" in x for x in out["not_compared"]) and any("wires" in x for x in out["not_compared"])


def test_a_sheet_that_is_missing_at_a_revision_is_reported_and_nothing_is_compared(history) -> None:
    """Mutant: comparing a partial tree. Revision three has no sub.kicad_sch, so the root's sheet symbol points at nothing."""
    root, revs, _ = history
    out = schematic_diff.diff_schematic(str(root), before_revision=revs["two"], after_revision=revs["three"])
    assert out["status"] == "incomplete" and out["ok"] is False and out["compared"] is False and out["no_symbol_or_net_differences"] is False, out
    assert [s["loaded"] for s in out["states"]] == [True, False]
    assert "sub.kicad_sch" in out["reason"] and "after state" in out["reason"]
    assert "sheets" not in out and "nets" not in out


def test_a_revision_that_does_not_exist_is_reported_not_guessed(history) -> None:
    root, revs, _ = history
    out = schematic_diff.diff_schematic(str(root), before_revision="no-such-rev")
    assert out["ok"] is False and out["compared"] is False and out["states"][0]["loaded"] is False
    assert "no-such-rev" in out["reason"] and "does not name a commit" in out["reason"]


def test_the_netlist_that_fails_is_reported_and_the_symbol_part_is_still_given(history, monkeypatch) -> None:
    """Mutant: a failed netlist read as 'no net changes'."""
    root, revs, _ = history

    def broken(state):
        raise RuntimeError("kicad-cli exploded")

    monkeypatch.setattr(schematic_diff, "_read_nets", broken)
    out = schematic_diff.diff_schematic(str(root), before_revision=revs["one"], after_revision=revs["two"])
    assert out["status"] == "incomplete" and out["ok"] is False and out["no_symbol_or_net_differences"] is False and out["nets"] is None
    assert "kicad-cli exploded" in out["checks_not_run"]["nets"] and out["totals"]["nets"] is None
    assert out["totals"]["symbols"]["added"] == 1


def test_an_unreadable_working_sheet_is_reported_for_a_path_state(history, tmp_path) -> None:
    root, _, one_copy = history
    broken = tmp_path / "broken"
    shutil.copytree(one_copy.parent, broken)
    (broken / "sub.kicad_sch").unlink()
    out = schematic_diff.diff_schematic(str(broken / one_copy.name), str(root))
    assert out["ok"] is False and out["compared"] is False and out["states"][0]["loaded"] is False and "sub.kicad_sch" in out["reason"]


def test_the_working_tree_and_the_netlist_scratch_leave_nothing_behind(history) -> None:
    """Read-only: no byte of the project folder and no new file in it (kicad-cli writes beside what it is given)."""
    root, revs, one_copy = history
    snap = {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file() and ".git" not in p.parts}
    files = sorted(p.name for p in one_copy.parent.iterdir())
    schematic_diff.diff_schematic(str(root), before_revision=revs["one"])
    schematic_diff.diff_schematic(str(one_copy), str(root))
    assert {p: p.read_bytes() for p in root.parent.rglob("*") if p.is_file() and ".git" not in p.parts} == snap
    assert sorted(p.name for p in one_copy.parent.iterdir()) == files


def test_the_raw_position_of_a_moved_symbol_is_read_from_the_at_node(history, tmp_path) -> None:
    """The from and to of a move are the raw (at ..) of the symbol node, not a convenience property: move R4 by a known offset through the tool."""
    root, revs, _ = history
    work = F.copy_of(root, tmp_path / "moved")
    assert F.tools()["move_symbols"](str(work), [{"references": ["R4"], "offset": [2.54, 5.08]}])["status"] == "ok"
    out = schematic_diff.diff_schematic(str(root), str(work), limit=10)
    (entry,) = out["sheets"]
    (move,) = entry["moved"]
    assert move["reference"] == "R4" and move["from"] == [127.0, 127.0] and move["to"] == [129.54, 132.08]
    assert move["distance_mm"] == pytest.approx(5.6803, abs=1e-3) and out["totals"]["symbols"]["largest_move_mm"] == move["distance_mm"]
    raw = [sheet_editing._at(s) for s in F.children(work, "symbol") if sheet_editing._property(s, "Reference").get_string(1) == "R4"]
    assert raw == [(129.54, 132.08, 0.0)] or raw[0][:2] == (129.54, 132.08)


def test_the_tool_is_registered_read_only_and_not_a_seam(_cli) -> None:
    tool = F.tools()["diff_schematic"]
    assert getattr(tool, "_kicad_read_only", False) is True
    import inspect

    assert list(inspect.signature(tool).parameters) == ["before", "after", "before_revision", "after_revision", "root_sch", "after_root_sch", "limit"]
    assert os.fspath(Path(schematic_diff.__file__)).startswith(str(Path(__file__).resolve().parents[1] / "src"))
