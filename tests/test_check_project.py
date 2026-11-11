"""check_project (GitHub issue 129): connectivity and ERC over the whole sheet tree in one call.

Reference: kicad-cli's ERC of the ROOT sheet (``kicad_cli.run_erc``) and ``check_connectivity``'s own result.
One module-scoped project built through the tools: R1 on the root (unconnected), Connectors (R10 wired to two
hierarchical labels, matching sheet pins), MCU (R20 unconnected, a sheet pin without a child label) and Clean
(an empty sheet, which ERC lists with no finding). Cases that damage the tree work on a copy.
"""

from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path

import pytest

from boardproof import kicad_cli, project_check
from boardproof.toolsets import library as library_toolset
from boardproof.toolsets import project as project_toolset
from tests import test_sheet_pins as _base
from tests.test_sheet_pins import _bytes

tools = _base.tools
_need_kicad_cli = _base._need_kicad_cli


@pytest.fixture(scope="module")
def project(tmp_path_factory) -> Path:
    t = _base._tools()
    made = project_toolset._project_create(str(tmp_path_factory.mktemp("cp") / "proj"), "demo")
    sch = Path(made["schematic_path"])
    folder = sch.parent
    assert t["create_sheets"](str(sch), [{"file": "connectors.kicad_sch"}, {"file": "mcu.kicad_sch"}, {"file": "clean.kicad_sch"}])["status"] == "ok"
    conn, mcu = str(folder / "connectors.kicad_sch"), str(folder / "mcu.kicad_sch")
    assert t["add_symbols"](conn, [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R10", "value": "1k"}])["status"] == "ok"
    assert t["add_labels"](conn, [
        {"kind": "hier", "x": 38.1, "y": 46.99, "text": "PT_A", "shape": "bidirectional"},
        {"kind": "hier", "x": 38.1, "y": 54.61, "text": "PT_B", "shape": "bidirectional"}])["status"] == "ok"
    assert t["add_wires"](conn, [_base._wire((38.1, 46.99), (50.8, 46.99)), _base._wire((38.1, 54.61), (50.8, 54.61))])["status"] == "ok"
    assert t["add_symbols"](mcu, [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R20", "value": "1k"}])["status"] == "ok"
    assert t["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 63.5, "y": 60.96, "ref": "R1", "value": "10k"}])["status"] == "ok"
    placed = t["add_sheet_symbols"](str(sch), [
        {"file": "connectors.kicad_sch", "name": "Connectors", "x": 101.6, "y": 50.8, "width": 30.48, "height": 38.1,
         "pins": [{"name": "PT_A", "direction": "bidirectional"}, {"name": "PT_B", "direction": "bidirectional"}]},
        {"file": "mcu.kicad_sch", "name": "MCU", "x": 101.6, "y": 101.6, "width": 30.48, "height": 38.1,
         "pins": [{"name": "X", "direction": "input"}]},
        {"file": "clean.kicad_sch", "name": "Clean", "x": 152.4, "y": 50.8, "width": 30.48, "height": 38.1,
         "pins": [{"name": "Y", "direction": "input"}]}])
    assert placed["status"] == "ok", placed
    return sch


@pytest.fixture(scope="module")
def result(project) -> dict:
    return _base._tools()["check_project"](str(project))


@pytest.fixture(scope="module")
def reference(project) -> dict:
    """The two engines called directly on the root: the reference the tool must equal."""
    t = _base._tools()
    return {"erc": kicad_cli.run_erc(project), "connectivity": t["check_connectivity"](str(project))}


def _sheet(res: dict, name: str) -> dict:
    return next(s for s in res["sheets"] if s["sheet"] == name)


def test_result_is_grouped_per_sheet_and_equals_the_reference_engines(result, reference) -> None:
    assert result["status"] == "ok" and result["ok"] is True and result["covered"] is True and result["written"] is False, result
    assert [s["sheet"] for s in result["sheets"]] == ["demo.kicad_sch", "connectors.kicad_sch", "mcu.kicad_sch", "clean.kicad_sch"]
    by_path = {"demo.kicad_sch": "/", "connectors.kicad_sch": "/Connectors/", "mcu.kicad_sch": "/MCU/", "clean.kicad_sch": "/Clean/"}
    ref_erc = {e["path"]: e["violations"] for e in reference["erc"]["sheets"]}
    for entry in result["sheets"]:
        expected = ref_erc[by_path[entry["sheet"]]]
        assert entry["erc"]["count"] == len(expected) and entry["erc"]["reported"] is True
        assert Counter((v["type"], v["severity"]) for v in entry["erc"]["violations"]) == Counter((v["type"], v["severity"]) for v in expected)
        assert {v["sheet_path"] for v in entry["erc"]["violations"]} <= {by_path[entry["sheet"]]}
    assert _sheet(result, "clean.kicad_sch")["erc"]["count"] == 0
    # the case is not trivial: two sheets have findings, two have none
    assert {s["sheet"] for s in result["sheets"] if s["erc"]["count"]} == {"demo.kicad_sch", "mcu.kicad_sch"}
    conn = reference["connectivity"]
    pins = {(p["reference"], p["pin_number"]): ("" if s["sheet"] == "demo.kicad_sch" else s["sheet"]) for s in result["sheets"] for p in s["connectivity"]["unconnected_pins"]}
    assert pins == {(p["reference"], p["pin_number"]): p["sheet"] for p in conn["unconnected_pins"]}
    assert {p for p in pins} == {("R1", "1"), ("R1", "2"), ("R20", "1"), ("R20", "2")}
    assert _sheet(result, "mcu.kicad_sch")["connectivity"]["unconnected_count"] == 2
    assert _sheet(result, "demo.kicad_sch")["connectivity"]["unconnected_count"] == 2
    assert _sheet(result, "connectors.kicad_sch")["connectivity"]["unconnected_count"] == 0


def test_totals_are_the_sums_and_nothing_is_called_clean_with_findings(result, reference) -> None:
    totals = result["totals"]
    all_erc = [v for e in reference["erc"]["sheets"] for v in e["violations"]]
    assert totals["sheets"] == 4
    assert totals["erc_violations"] == len(all_erc) == sum(s["erc"]["count"] for s in result["sheets"]) and totals["erc_violations"] > 0
    assert totals["erc_by_severity"] == dict(sorted(Counter(v["severity"] for v in all_erc).items()))
    assert totals["unconnected_pins"] == 4 == sum(s["connectivity"]["unconnected_count"] for s in result["sheets"])
    assert totals["no_connect_pins"] == reference["connectivity"]["no_connect_count"]
    assert totals["connection_issues"] == len(reference["connectivity"]["connection_issues"])
    assert result["no_findings"] is False
    for s in result["sheets"]:
        assert s["erc"]["by_severity"] == dict(sorted(Counter(v["severity"] for v in s["erc"]["violations"]).items()))


def test_one_kicad_cli_erc_run_for_the_whole_project(project, monkeypatch) -> None:
    calls = []
    real = kicad_cli.run_erc
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: (calls.append(a), real(*a, **k))[1])
    res = _base._tools()["check_project"](str(project), checks=["erc"])
    assert res["status"] == "ok" and len(calls) == 1, calls
    assert [Path(c[0]).name for c in calls] == ["demo.kicad_sch"]  # the root, not a sub-sheet
    assert Path(calls[0][0]).resolve().parent != project.resolve().parent  # on the scratch copy


def test_checks_select_the_engines(project, monkeypatch) -> None:
    t = _base._tools()
    ran = []
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: ran.append("erc") or {"sheets": []})
    only_conn = t["check_project"](str(project), checks=["connectivity"])
    assert ran == [] and only_conn["checks_requested"] == ["connectivity"] and "erc" not in only_conn["sheets"][0]
    assert only_conn["totals"]["unconnected_pins"] == 4 and "erc_violations" not in only_conn["totals"]
    only_erc = t["check_project"](str(project), checks=["erc"])
    assert ran == ["erc"] and "connectivity" not in only_erc["sheets"][0] and "unconnected_pins" not in only_erc["totals"]


@pytest.mark.parametrize("checks", [[], ["bogus"], ["erc", "erc"], "erc", [1], ["erc", "bogus"]])
def test_bad_checks_are_refused(project, checks) -> None:
    res = _base._tools()["check_project"](str(project), checks=checks)
    assert res["ok"] is False and res["code"] == "invalid_spec", res


def test_a_sub_sheet_path_follows_the_hierarchy_from_the_root(project, result) -> None:
    res = _base._tools()["check_project"](str(project.parent / "mcu.kicad_sch"), checks=["connectivity"], root_sch=str(project))
    assert res["root"] == "demo.kicad_sch" and [s["sheet"] for s in res["sheets"]] == [s["sheet"] for s in result["sheets"]]
    assert res["totals"]["unconnected_pins"] == 4


def test_refusals(project) -> None:
    t = _base._tools()
    assert t["check_project"](str(project.parent / "nope.kicad_sch"))["code"] == "file_not_found"
    stray = project.parent / "stray.kicad_sch"
    shutil.copyfile(project.parent / "mcu.kicad_sch", stray)
    try:
        res = t["check_project"](str(stray), root_sch=str(project))
        assert res["ok"] is False and res["code"] == "parent_not_in_tree", res
    finally:
        stray.unlink()


def test_it_writes_nothing(project) -> None:
    before = _bytes(project.parent)
    names = sorted(p.name for p in project.parent.iterdir())
    stamps = {p: p.stat().st_mtime_ns for p in project.parent.iterdir() if p.is_file()}
    _base._tools()["check_project"](str(project))
    assert _bytes(project.parent) == before
    assert sorted(p.name for p in project.parent.iterdir()) == names
    assert {p: p.stat().st_mtime_ns for p in project.parent.iterdir() if p.is_file()} == stamps


def test_a_sheet_that_cannot_be_loaded_is_reported_not_dropped(project, tmp_path) -> None:
    copy = tmp_path / "copy"
    shutil.copytree(project.parent, copy)
    (copy / "mcu.kicad_sch").unlink()
    res = _base._tools()["check_project"](str(copy / "demo.kicad_sch"))
    assert res["status"] == "incomplete" and res["ok"] is False and res["covered"] is False and res["no_findings"] is False, res
    assert any("mcu.kicad_sch" in p for p in res["sheet_problems"]) and "mcu.kicad_sch" in res["reason"]
    assert "connectivity" in res["checks_not_run"] and "mcu.kicad_sch" in res["checks_not_run"]["connectivity"]
    assert "erc" not in res["checks_not_run"]  # ERC still ran on what kicad-cli could load
    assert [s["sheet"] for s in res["sheets"]] == ["demo.kicad_sch", "connectors.kicad_sch", "clean.kicad_sch"]
    assert all("connectivity" not in s for s in res["sheets"])  # no finding of the broken tree is passed off as a result
    assert res["totals"]["erc_violations"] > 0 and "unconnected_pins" not in res["totals"]


def test_an_unloadable_root_is_refused(project, tmp_path) -> None:
    copy = tmp_path / "copy2"
    shutil.copytree(project.parent, copy)
    (copy / "demo.kicad_sch").unlink()
    res = _base._tools()["check_project"](str(copy / "mcu.kicad_sch"), root_sch=str(copy / "demo.kicad_sch"))
    assert res["ok"] is False and res["code"] in {"root_not_found", "sheet_tree_incomplete"}, res


# --- the engine seams, with fakes: failure and attribution paths -------------------------------------------------

def _fake_conn(**lists):
    return lambda _path: {"status": "ok", **lists}


def test_a_failed_erc_run_is_never_read_as_clean(project, monkeypatch) -> None:
    def boom(*a, **k):
        raise kicad_cli.KicadCliError("kicad-cli exploded")

    monkeypatch.setattr(kicad_cli, "run_erc", boom)
    res = project_check.check_project(str(project), None, ["erc"], _fake_conn())
    assert res["status"] == "incomplete" and res["covered"] is False and res["no_findings"] is False, res
    assert "exploded" in res["checks_not_run"]["erc"] and "erc_violations" not in res["totals"]
    assert all("erc" not in s for s in res["sheets"])


def test_an_incomplete_connectivity_engine_is_reported(project) -> None:
    res = project_check.check_project(str(project), None, ["connectivity"], lambda _p: {"status": "incomplete", "reason": "engine says no"})
    assert res["status"] == "incomplete" and res["no_findings"] is False and res["checks_not_run"] == {"connectivity": "engine says no"}


def test_findings_that_name_no_unique_sheet_file_are_listed_and_counted(project) -> None:
    stray = {"reference": "R9", "pin_number": "1", "sheet": "elsewhere.kicad_sch"}
    res = project_check.check_project(str(project), None, ["connectivity"], _fake_conn(unconnected_pins=[stray], missing_from_netlist=[{"reference": "U1", "sheet": "mcu.kicad_sch"}]))
    assert res["connectivity_unattributed"]["unconnected_pins"] == [stray]
    assert res["totals"]["unconnected_pins"] == 1 and res["no_findings"] is False
    assert _sheet(res, "mcu.kicad_sch")["connectivity"]["missing_from_netlist_count"] == 1 and res["totals"]["missing_from_netlist"] == 1


def test_a_base_name_shared_by_two_files_is_not_guessed(tmp_path) -> None:
    class Tree:
        files = (tmp_path / "a" / "x.kicad_sch", tmp_path / "b" / "x.kicad_sch", tmp_path / "root.kicad_sch")

        @staticmethod
        def rel(path: Path) -> str:
            return path.relative_to(tmp_path).as_posix()

    item = {"reference": "R1", "sheet": "x.kicad_sch"}
    grouped, unattributed = project_check._group_connectivity({"unconnected_pins": [item, {"reference": "R2", "sheet": ""}]}, Tree, "root.kicad_sch")
    assert unattributed["unconnected_pins"] == [item]
    assert grouped["root.kicad_sch"]["unconnected_pins"] == [{"reference": "R2", "sheet": ""}]
    assert grouped["a/x.kicad_sch"]["unconnected_pins"] == [] == grouped["b/x.kicad_sch"]["unconnected_pins"]


def test_erc_sheets_without_a_match_and_sheet_uses_without_a_report_are_flagged(project, monkeypatch) -> None:
    real = kicad_cli.run_erc(project)
    root_entry = real["sheets"][0]
    odd = {"path": "/Ghost/", "uuid_path": "/nope/nope", "violations": [{"type": "pin_not_connected", "severity": "error", "description": "x", "items": []}]}
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"sheets": [root_entry, odd]})
    res = project_check.check_project(str(project), None, ["erc"], _fake_conn())
    assert res["status"] == "incomplete" and res["no_findings"] is False
    assert [u["path"] for u in res["erc_unmapped"]] == ["/Ghost/"]
    assert res["totals"]["erc_violations"] == len(root_entry["violations"]) + 1
    assert len(res["erc_unreported_paths"]) == 3 and _sheet(res, "mcu.kicad_sch")["erc"]["reported"] is False
    assert _sheet(res, "demo.kicad_sch")["erc"]["reported"] is True


def test_a_clean_complete_project_reports_no_findings(project, monkeypatch) -> None:
    t = _base._tools()
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"sheets": [
        {"path": "/", "uuid_path": p, "violations": []} for p in [path for _f, path in project_check.sheet_tree.walk(project).uses]]})
    res = project_check.check_project(str(project), None, None, _fake_conn())
    assert res["status"] == "ok" and res["no_findings"] is True and res["totals"]["erc_violations"] == 0
    assert t["check_project"].__name__ == "check_project"


def test_tool_text_states_the_contract() -> None:
    from mcp.server.mcpserver import MCPServer

    from boardproof.toolsets import schematic as schematic_toolset

    mcp = MCPServer("t")
    schematic_toolset.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "check_project")  # type: ignore[attr-defined]
    text = " ".join(tool.description.split())
    for phrase in ("ONE kicad-cli ERC run on the root sheet", "grouped per sheet", "counts per sheet and in total", "Nothing is dropped silently",
                   "sheet_problems", "covered false", "erc_unmapped", "not a verdict", "only verify_board gives one",
                   "an ERC run that failed never gives no_findings", "writes nothing"):
        assert phrase in text, phrase
    assert tool.annotations.read_only_hint is True  # type: ignore[union-attr]


def test_skill_names_the_tool() -> None:
    skill = (Path(__file__).resolve().parents[1] / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8")
    assert "`check_project`" in skill and "one `kicad-cli` ERC run" in skill


def _all_uses_reported(project) -> list[dict]:
    return [{"path": "/", "uuid_path": path, "violations": []} for _f, path in project_check.sheet_tree.walk(project).uses]


def test_an_unmatched_erc_sheet_alone_makes_the_result_incomplete(project, monkeypatch) -> None:
    odd = {"path": "/Ghost/", "uuid_path": "/nope/nope", "violations": []}
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"sheets": [*_all_uses_reported(project), odd]})
    res = project_check.check_project(str(project), None, ["erc"], _fake_conn())
    assert res["erc_unreported_paths"] == [] and len(res["erc_unmapped"]) == 1
    assert res["status"] == "incomplete" and res["ok"] is False and res["no_findings"] is False and "Ghost" not in res["reason"] and "erc_unmapped" in res["reason"]


def test_an_unreported_sheet_use_alone_makes_the_result_incomplete(project, monkeypatch) -> None:
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"sheets": _all_uses_reported(project)[:-1]})
    res = project_check.check_project(str(project), None, ["erc"], _fake_conn())
    assert res["erc_unmapped"] == [] and len(res["erc_unreported_paths"]) == 1
    assert res["status"] == "incomplete" and res["no_findings"] is False and _sheet(res, "clean.kicad_sch")["erc"]["reported"] is False


def test_a_broken_tree_alone_makes_the_result_incomplete_and_never_runs_connectivity(project, tmp_path, monkeypatch) -> None:
    copy = tmp_path / "copy3"
    shutil.copytree(project.parent, copy)
    (copy / "mcu.kicad_sch").unlink()
    root = copy / "demo.kicad_sch"
    uses = project_check.sheet_tree.walk(root).uses
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"sheets": [{"path": "/", "uuid_path": p, "violations": []} for _f, p in uses]})
    called = []
    res = project_check.check_project(str(root), None, ["erc"], lambda p: called.append(p) or {"status": "ok"})
    assert res["status"] == "incomplete" and res["covered"] is False and res["no_findings"] is False and "mcu.kicad_sch" in res["reason"]
    both = project_check.check_project(str(root), None, None, lambda p: called.append(p) or {"status": "ok"})
    assert called == [] and "connectivity" in both["checks_not_run"] and both["status"] == "incomplete"


# --- review of issue 129 (B1, B2, F1) ---------------------------------------------------------------------------

def _listing(folder: Path) -> dict[str, tuple[int, int]]:
    return {p.relative_to(folder).as_posix(): (p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(folder.rglob("*")) if p.is_file()}


@pytest.fixture
def fresh(tmp_path) -> Path:
    """A project just made by project_create plus one symbol: no .kicad_prl, no netlist yet (module-scoped
    fixtures hide a file that an earlier call created)."""
    t = _base._tools()
    sch = Path(project_toolset._project_create(str(tmp_path / "fresh"), "demo")["schematic_path"])
    assert t["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 63.5, "y": 60.96, "ref": "R1", "value": "10k"}])["status"] == "ok"
    return sch


@pytest.mark.parametrize("checks", [["connectivity"], ["erc"], None])
def test_a_fresh_project_folder_is_untouched_by_each_check(fresh, checks) -> None:
    before = _listing(fresh.parent)
    res = _base._tools()["check_project"](str(fresh), checks=checks)
    assert res["status"] == "ok", res
    assert _listing(fresh.parent) == before  # names, sizes and mtimes: no .kicad_prl, no netlist, nothing rewritten


def test_the_connectivity_engine_runs_on_a_scratch_copy(fresh) -> None:
    seen = []
    res = project_check.check_project(str(fresh), None, ["connectivity"], lambda p: seen.append(p) or {"status": "ok"})
    assert res["status"] == "ok" and len(seen) == 1
    assert Path(seen[0]).resolve().parent != fresh.resolve().parent and Path(seen[0]).name == fresh.name


def test_a_sheet_file_used_twice_counts_the_violations_of_every_use(project, tmp_path) -> None:
    from tests.test_sheet_pins import _add_twin

    copy = tmp_path / "twice"
    shutil.copytree(project.parent, copy)
    root = copy / "demo.kicad_sch"
    _add_twin(root, "MCU", "MCU2", "11111111-1111-4111-8111-111111111111", 160.02)
    # the twin's symbol needs instance data for its own sheet path, else kicad-cli lists nothing for the second use
    from tests._sheet_tree_fixture import add_instance_entry

    twin_path = f"{project_check.sheet_tree.walk(root).uses[0][1]}/11111111-1111-4111-8111-111111111111"
    add_instance_entry(copy / "mcu.kicad_sch", "R20", project="demo", path=twin_path, new_ref="R21", first=False)
    direct = kicad_cli.run_erc(root)
    by_path = {e["path"]: e["violations"] for e in direct["sheets"]}
    # measured with kicad-cli 10.0.7: the second use of a shared file is listed (it is a sheet use) but carries no
    # violation of the file's symbols; the sums must still equal the direct run, whatever it reports per use
    assert "/MCU2/" in by_path and by_path.get("/MCU/"), list(by_path)
    res = _base._tools()["check_project"](str(root), checks=["erc"])
    entry = _sheet(res, "mcu.kicad_sch")
    assert entry["uses"] == 2
    assert entry["erc"]["count"] == len(by_path["/MCU/"]) + len(by_path["/MCU2/"]) and entry["erc"]["reported"] is True
    assert Counter(v["sheet_path"] for v in entry["erc"]["violations"]) == Counter({p: len(by_path[p]) for p in ("/MCU/", "/MCU2/") if by_path[p]})
    assert res["totals"]["erc_violations"] == sum(len(e["violations"]) for e in direct["sheets"])
    assert res["totals"]["erc_by_severity"] == dict(sorted(Counter(v["severity"] for e in direct["sheets"] for v in e["violations"]).items()))


def test_an_erc_only_finding_with_clean_connectivity_is_not_no_findings(project, monkeypatch) -> None:
    one = {"type": "hier_label_mismatch", "severity": "error", "description": "x", "items": []}
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"sheets": [
        {"path": "/", "uuid_path": p, "violations": [one] if i == 1 else []} for i, (_f, p) in enumerate(project_check.sheet_tree.walk(project).uses)]})
    res = project_check.check_project(str(project), None, None, _fake_conn())
    assert res["status"] == "ok" and res["totals"]["erc_violations"] == 1 and res["totals"]["unconnected_pins"] == 0
    assert res["no_findings"] is False


def test_no_findings_needs_both_checks(project, monkeypatch) -> None:
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"sheets": _all_uses_reported(project)})
    assert project_check.check_project(str(project), None, None, _fake_conn())["no_findings"] is True
    assert project_check.check_project(str(project), None, ["erc"], _fake_conn())["no_findings"] is False
    assert project_check.check_project(str(project), None, ["connectivity"], _fake_conn())["no_findings"] is False


def test_every_use_of_a_shared_file_with_findings_is_counted_and_tagged(project, tmp_path, monkeypatch) -> None:
    """Both uses carry findings (kicad-cli does not produce that here, so the report is fed in): all are counted."""
    from tests.test_sheet_pins import _add_twin

    copy = tmp_path / "twice2"
    shutil.copytree(project.parent, copy)
    root = copy / "demo.kicad_sch"
    _add_twin(root, "MCU", "MCU2", "11111111-1111-4111-8111-111111111111", 160.02)
    uses = project_check.sheet_tree.walk(root).uses
    paths = {Path(f).name: [] for f, _ in uses}
    for f, p in uses:
        paths[Path(f).name].append(p)
    assert len(paths["mcu.kicad_sch"]) == 2
    def one(n: int) -> list[dict]:
        return [{"type": "pin_not_connected", "severity": "error", "description": "d", "items": []} for _ in range(n)]

    mcu_a, mcu_b = paths["mcu.kicad_sch"]
    monkeypatch.setattr(kicad_cli, "run_erc", lambda *a, **k: {"sheets": [
        {"path": "/MCU/", "uuid_path": mcu_a, "violations": one(2)}, {"path": "/MCU2/", "uuid_path": mcu_b, "violations": one(3)},
        *({"path": "/", "uuid_path": p, "violations": []} for n, ps in paths.items() if n != "mcu.kicad_sch" for p in ps)]})
    res = project_check.check_project(str(root), None, ["erc"], _fake_conn())
    entry = _sheet(res, "mcu.kicad_sch")
    assert entry["uses"] == 2 and entry["erc"]["count"] == 5
    assert Counter(v["sheet_path"] for v in entry["erc"]["violations"]) == Counter({"/MCU/": 2, "/MCU2/": 3})
    assert res["totals"]["erc_violations"] == 5 and res["totals"]["erc_by_severity"] == {"error": 5}


def _library_project(tmp_path: Path) -> Path:
    """A project with a project-local symbol library ``scbe`` (lib/scbe.kicad_sym, row in sym-lib-table, built
    through register_library_asset) and one symbol placed from it."""
    from tests.test_symbol_libs import _tools as _lib_tools, _write_lib

    sch = Path(project_toolset._project_create(str(tmp_path / "libproj"), "demo")["schematic_path"])
    lib = _write_lib(sch.parent / "lib" / "scbe.kicad_sym")
    reg = _lib_tools(library_toolset)["register_library_asset"](str(sch.parent), symbol_lib_path=str(lib))
    assert reg["symbol"]["status"] == "ok", reg
    placed = _base._tools()["add_symbols"](str(sch), [{"lib_id": "scbe:R", "x": 63.5, "y": 60.96, "ref": "R7", "value": "1k"}])
    assert placed["status"] == "ok", placed
    return sch


def test_project_local_libraries_give_the_same_erc_as_the_real_folder(tmp_path) -> None:
    sch = _library_project(tmp_path)
    reference = tmp_path / "reference"
    shutil.copytree(sch.parent, reference)  # the real-folder run happens on a second copy: kicad-cli writes beside its input
    direct = kicad_cli.run_erc(reference / sch.name)
    expected = Counter((v["type"], v["severity"]) for e in direct["sheets"] for v in e["violations"])
    res = _base._tools()["check_project"](str(sch), checks=["erc"])
    got = Counter((v["type"], v["severity"]) for s in res["sheets"] for v in s["erc"]["violations"])
    assert res["status"] == "ok" and got == expected, (got, expected)
    assert not any(t.startswith("lib_") for t, _s in got), got  # no library error that only the scratch copy has


def test_a_library_row_outside_the_project_folder_is_reported_and_not_copied(tmp_path) -> None:
    from tests.test_symbol_libs import _table

    sch = Path(project_toolset._project_create(str(tmp_path / "outproj"), "demo")["schematic_path"])
    from tests.test_symbol_libs import _write_lib

    _write_lib(tmp_path / "secret.kicad_sym")
    _table(sch.parent / "sym-lib-table", ("evil", "${KIPRJMOD}/../secret.kicad_sym"))
    res = _base._tools()["check_project"](str(sch), checks=["erc"])
    assert res["status"] == "incomplete" and res["covered"] is False and res["no_findings"] is False, res
    assert len(res["library_problems"]) == 1 and "leaves the project folder" in res["library_problems"][0] and "library_problems" not in res["reason"]
    assert "project libraries could not be copied" in res["reason"]


def test_the_library_copy_is_bounded(tmp_path, monkeypatch) -> None:
    sch = _library_project(tmp_path)
    monkeypatch.setattr(project_check, "_LIB_MAX_FILES", 0)
    res = _base._tools()["check_project"](str(sch), checks=["erc"])
    assert res["status"] == "incomplete" and any("exceed" in p for p in res["library_problems"]), res


# --- second delta review: the library copy (_copy_project_libraries) ------------------------------------------------

def _copy(project_dir: Path, tmp_path: Path) -> tuple[list[str], Path]:
    scratch = tmp_path / "scratch"
    scratch.mkdir(parents=True)
    return project_check._copy_project_libraries(project_dir, scratch), scratch


def _files(folder: Path) -> set[str]:
    return {p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()}


def _lib_table(path: Path, kind: str, uri_text: str) -> None:
    """A lib table written through the write layer (tests only); ``uri_text`` is the text after ``(uri``."""
    data = f'({kind} (version 7) (lib (name "x")(type "KiCad")(uri {uri_text})(options "")(descr "")))'.encode()
    project_check.sheet_authoring.write_layer.save_bytes(data, path, verify=lambda tmp: None)


def test_the_byte_bound_alone_stops_the_copy(tmp_path, monkeypatch) -> None:
    sch = _library_project(tmp_path)
    monkeypatch.setattr(project_check, "_LIB_MAX_BYTES", 1)
    problems, scratch = _copy(sch.parent, tmp_path)
    assert any("exceed" in p for p in problems) and not any(f.startswith("lib/") for f in _files(scratch)), (problems, _files(scratch))


def test_the_file_bound_counts_while_walking_and_copies_nothing_of_the_row(tmp_path, monkeypatch) -> None:
    sch = _library_project(tmp_path)
    for i in range(3):
        shutil.copyfile(sch.parent / "lib" / "scbe.kicad_sym", sch.parent / "lib" / f"more{i}.kicad_sym")
    _lib_table(sch.parent / "sym-lib-table", "sym_lib_table", '"${KIPRJMOD}/lib"')
    monkeypatch.setattr(project_check, "_LIB_MAX_FILES", 2)
    problems, scratch = _copy(sch.parent, tmp_path)
    assert any("exceed" in p for p in problems) and not any(f.startswith("lib/") for f in _files(scratch))


def test_a_footprint_library_row_is_copied_and_an_outside_footprint_row_is_reported(tmp_path) -> None:
    from boardproof.sourcing.footprint_gen import generate_footprint

    sch = Path(project_toolset._project_create(str(tmp_path / "fpproj"), "demo")["schematic_path"])
    pretty = sch.parent / "MyLibs.pretty"
    pretty.mkdir()
    assert generate_footprint("chip", pretty / "0402.kicad_mod", size="0402")["status"] == "ok"
    reg = _lib_tools()["register_library_asset"](str(sch.parent), footprint_lib_path=str(pretty))
    assert reg["footprint"]["status"] == "ok", reg
    problems, scratch = _copy(sch.parent, tmp_path)
    assert problems == [] and "MyLibs.pretty/0402.kicad_mod" in _files(scratch), (problems, _files(scratch))
    _lib_table(sch.parent / "fp-lib-table", "fp_lib_table", '"${KIPRJMOD}/../elsewhere.pretty"')
    problems, _ = _copy(sch.parent, tmp_path / "again")
    assert len(problems) == 1 and "fp-lib-table" in problems[0] and "leaves the project folder" in problems[0]


def _lib_tools() -> dict:
    from tests.test_symbol_libs import _tools

    return _tools(library_toolset)


def _junction(link: Path, target: Path) -> None:
    import subprocess
    import sys

    if sys.platform != "win32":
        pytest.skip("a junction is a Windows reparse point")
    done = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True, encoding="utf-8", errors="replace")
    if done.returncode != 0 or not link.exists():
        pytest.skip(f"cannot create a junction here: {done.stderr or done.stdout}")


def test_junctions_below_a_library_row_are_not_followed(tmp_path) -> None:
    sch = _library_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "o.txt").write_text("secret", encoding="utf-8")
    _junction(sch.parent / "lib" / "jn", outside)
    _junction(sch.parent / "lib" / "loop", sch.parent)
    _lib_table(sch.parent / "sym-lib-table", "sym_lib_table", '"${KIPRJMOD}/lib"')
    problems, scratch = _copy(sch.parent, tmp_path)
    got = _files(scratch)
    assert "lib/scbe.kicad_sym" in got and "lib/jn/o.txt" not in got and not any(f.startswith("lib/loop") for f in got), got
    assert len([p for p in problems if "is a link" in p]) == 2, problems


def test_unquoted_and_parenthesised_uri_forms_are_read_and_odd_ones_reported(tmp_path) -> None:
    sch = _library_project(tmp_path)
    table = sch.parent / "sym-lib-table"
    for uri_text in ("${KIPRJMOD}/lib/scbe.kicad_sym", "$(KIPRJMOD)/lib/scbe.kicad_sym", '"$(KIPRJMOD)/lib/scbe.kicad_sym"'):
        _lib_table(table, "sym_lib_table", uri_text)
        problems, scratch = _copy(sch.parent, tmp_path / f"f{abs(hash(uri_text))}")
        assert problems == [] and "lib/scbe.kicad_sym" in _files(scratch), (uri_text, problems)
    for odd in ('"/x/${KIPRJMOD}/y"', "${KIPRJMOD}/${KIPRJMOD}/y", '"${OTHER}/${KIPRJMOD}/y"'):
        _lib_table(table, "sym_lib_table", odd)
        problems, _ = _copy(sch.parent, tmp_path / f"o{abs(hash(odd))}")
        assert len(problems) == 1 and "form this check does not read" in problems[0], (odd, problems)


# --- third delta review: the guards that the link check alone hid, the early stop, OSError, the directory bound ------

def test_a_junction_is_kept_out_by_the_inside_guard_when_the_link_check_misses_it(tmp_path, monkeypatch) -> None:
    """With _is_link blind, only the resolve-and-compare guard of the directory loop stands between a junction and the copy."""
    sch = _library_project(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "o.txt").write_text("secret", encoding="utf-8")
    _junction(sch.parent / "lib" / "jn", outside)
    _lib_table(sch.parent / "sym-lib-table", "sym_lib_table", '"${KIPRJMOD}/lib"')
    monkeypatch.setattr(project_check, "_is_link", lambda p: False)
    problems, scratch = _copy(sch.parent, tmp_path)
    got = _files(scratch)
    assert "lib/scbe.kicad_sym" in got and not any(f.startswith("lib/jn") for f in got), got
    assert any("lib/jn" in p and "is a link" in p for p in problems), problems


def test_the_walk_stops_at_the_bound_and_is_not_read_further(tmp_path, monkeypatch) -> None:
    sch = _library_project(tmp_path)
    lib = sch.parent / "lib"
    for i in range(3):
        shutil.copyfile(lib / "scbe.kicad_sym", lib / f"more{i}.kicad_sym")
    _lib_table(sch.parent / "sym-lib-table", "sym_lib_table", '"${KIPRJMOD}/lib"')
    monkeypatch.setattr(project_check, "_LIB_MAX_FILES", 2)

    def walk(top, followlinks=False):
        yield str(top), [], sorted(p.name for p in Path(top).iterdir())
        raise AssertionError("the walk was read past the bound")

    monkeypatch.setattr(project_check.os, "walk", walk)
    problems, scratch = _copy(sch.parent, tmp_path)
    assert any("exceed" in p for p in problems) and not any(f.startswith("lib/") for f in _files(scratch)), problems


def test_the_directory_bound_stops_the_copy(tmp_path, monkeypatch) -> None:
    sch = _library_project(tmp_path)
    for i in range(3):
        (sch.parent / "lib" / f"d{i}").mkdir()
    _lib_table(sch.parent / "sym-lib-table", "sym_lib_table", '"${KIPRJMOD}/lib"')
    monkeypatch.setattr(project_check, "_LIB_MAX_DIRS", 2)
    problems, scratch = _copy(sch.parent, tmp_path)
    assert any("directories" in p for p in problems) and not any(f.startswith("lib/") for f in _files(scratch)), problems


def test_an_unreadable_library_table_gives_an_incomplete_result_not_a_crash(tmp_path, monkeypatch, _need_kicad_cli) -> None:
    sch = _library_project(tmp_path)
    real = Path.read_text

    def read_text(self, *args, **kwargs):
        if self.name == "sym-lib-table":
            raise PermissionError("denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    res = _base._tools()["check_project"](str(sch), checks=["erc"])
    assert res["status"] == "incomplete" and res["covered"] is False and res["no_findings"] is False, res
    assert len(res["library_problems"]) == 1 and "could not be read" in res["library_problems"][0] and "denied" in res["library_problems"][0]


def test_a_file_link_below_a_library_row_is_not_copied(tmp_path, monkeypatch) -> None:
    """The file-level link guard alone: _is_link is true only for lib/scbe.kicad_sym."""
    sch = _library_project(tmp_path)
    real = project_check._is_link
    monkeypatch.setattr(project_check, "_is_link", lambda p: p.as_posix().endswith("lib/scbe.kicad_sym") or real(p))
    problems, scratch = _copy(sch.parent, tmp_path)
    assert "lib/scbe.kicad_sym" not in _files(scratch), _files(scratch)
    assert any("lib/scbe.kicad_sym is a link" in p for p in problems), problems


def test_the_tool_text_names_every_bound_and_the_unreadable_case() -> None:
    from boardproof.toolsets import schematic

    text = (schematic.__doc__ or "") + Path(schematic.__file__).read_text(encoding="utf-8")
    assert "2000 files, 2000 directories or 200 MB" in text and "cannot be read (an OS error)" in text
