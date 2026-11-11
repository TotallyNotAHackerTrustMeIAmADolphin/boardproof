"""The skills tell the walk from a fetched part to a wired symbol, and each sentence is pinned (issue 117).

Wording pins read the two SKILL.md files; behaviour tests run the walk on tmp_path copies through the registered tool
functions (project made by ``project_create``, parts by ``tests.test_part_adoption`` fixtures, no raw text edit) and
read the placed instances from the raw sheet, so a skill sentence that stops being true turns a test red.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_editing, write_layer
from boardproof.toolsets import library as library_toolset
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset
from tests.test_part_adoption import COMMON, _part_assets

REPO = Path(__file__).resolve().parents[1]
SCHEMATIC = " ".join((REPO / ".claude" / "skills" / "kicad-schematic" / "SKILL.md").read_text(encoding="utf-8").split())
SOURCING = " ".join((REPO / ".claude" / "skills" / "kicad-sourcing" / "SKILL.md").read_text(encoding="utf-8").split())
WALK_START = "**The walk from a fetched part to a wired symbol"


def _walk() -> str:
    start = SCHEMATIC.index(WALK_START)
    end = SCHEMATIC.index("**Build in batches.**")
    return SCHEMATIC[start:end]


def _tools(module) -> dict:
    mcp = MCPServer("test")
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture(autouse=False)
def _cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


# ---------------------------------------------------------------- wording pins


def test_the_walk_names_every_tool_in_the_working_order():
    walk = _walk()
    steps = re.findall(r"- ([a-h])\. `(\w+)`", walk)  # the first tool named by each step
    assert [t for _, t in steps] == ["propose_adopt_parts", "confirm_adopt_parts", "add_symbols", "check_symbol_fields",
                                     "assign_footprints", "get_symbol_pins", "add_no_connects", "sch_run_erc"], steps
    assert [letter for letter, _ in steps] == list("abcdefgh")
    assert walk.index("`get_symbol_pins`, then `connect_pins`") > walk.index("- e. `assign_footprints`")
    assert re.search(r"`add_labels` with kind `no_connect`", walk)


def test_the_walk_states_the_new_revision_folder():
    walk = _walk()
    for needle in ("writes a NEW revision folder", "`<project folder>_adopted`", "unless `output_dir` was given to `propose_adopt_parts`",
                   "leaves the source untouched", "returns the revision's board as `pcb_path`",
                   "every later call uses the schematic in that folder"):
        assert needle in walk, needle


def test_the_walk_states_when_a_placed_symbol_has_an_empty_footprint():
    walk = _walk()
    assert "takes over the Footprint, Datasheet and LCSC of its library symbol" in walk
    assert "has an empty Footprint only when its library symbol has none (`Device:R` has none)" in walk
    assert "until `assign_footprints`" in walk
    assert "empty Footprint until assign_footprints" not in SCHEMATIC  # the old, wrong blanket claim


def test_the_walk_states_the_rotation_and_the_pin_side():
    walk = _walk()
    assert "A turn by 180 degrees reverses the pin order" in walk
    assert "pin 1 is the top pin at rotation 0 and the bottom pin at rotation 180" in walk
    assert "The side of a pin inside the symbol's drawing is a property of the library symbol" in walk
    assert "only rotation and mirror of the instance change it" in walk


def test_the_walk_names_the_datasheet_rule_and_the_dry_run_writers():
    walk = _walk()
    assert "the datasheet is mandatory" in walk
    assert "`dry_run`" in walk
    for name in ("connect_pins", "rotate_items", "mirror_items", "replace_symbols", "add_no_connects"):
        assert name in walk


def test_the_sourcing_skill_states_the_revision_and_the_datasheet_rule():
    for needle in ("writes a NEW revision folder", "`<project folder>_adopted`", "all further work happens there",
                   "returns the revision's board as `pcb_path`", "continue with the kicad-schematic skill",
                   "The datasheet is mandatory: a part with no Datasheet (neither in the symbol nor passed as `datasheet`) is rejected, and one rejected part rejects the whole call"):
        assert needle in SOURCING, needle


def test_the_schematic_skill_numbers_its_items_in_order():
    numbers = re.findall(r"^(\d+)\. \*\*", (REPO / ".claude/skills/kicad-schematic/SKILL.md").read_text(encoding="utf-8"), re.M)
    assert numbers == [str(i) for i in range(1, len(numbers) + 1)], numbers
    assert not re.search(r"^\d+[ab]\. ", (REPO / ".claude/skills/kicad-schematic/SKILL.md").read_text(encoding="utf-8"), re.M)


def test_every_item_reference_of_the_walk_names_the_item_that_holds_the_topic():
    text = (REPO / ".claude/skills/kicad-schematic/SKILL.md").read_text(encoding="utf-8")
    heads = {m.group(2): int(m.group(1)) for m in re.finditer(r"^(\d+)\. \*\*([^*]+)\*\*", text, re.M)}
    def by_start(start: str) -> int:
        return next(n for h, n in heads.items() if h.startswith(start))

    expected = {
        "`connect_pins` (item": by_start("Wire by pin"),
        "(`rotation` and `mirror` of `add_symbols`, later `rotate_items` and `mirror_items`, item": by_start("Turn or mirror"),
        "`replace_symbols` and `library_fields: true` (item": by_start("Replace a placed symbol"),
        "`rename_net` (item": by_start("Rename a net"),
        "of its library symbol (item": by_start("Build in batches"),
    }
    walk = _walk()
    for needle, number in expected.items():
        assert re.search(re.escape(f"{needle} {number}") + r"[;)]", SCHEMATIC if needle.startswith("of its library") else walk), (needle, number)
    assert "`connect_pins` (item %d)" % by_start("Wire by pin") in walk  # step f itself, not item 3's copy
    assert f"(item {by_start('Wire by pin')})" in SCHEMATIC.split("**Be deliberate about `snap`")[1].split("**Render it")[0]


def test_replace_symbols_keeps_the_old_footprint_unless_library_fields_is_true():
    doc = inspect.getdoc(_tools(schematic_toolset)["replace_symbols"]) or ""
    assert "library_fields" in " ".join(doc.split())
    assert "without that option the symbol keeps its own Footprint text" in SCHEMATIC


DRY_RUN_SENTENCE = re.compile(r"((?:`\w+`(?:, | and ))+`\w+`) take `dry_run`: use it first when the result is not obvious; "
                              r"the first four also return `nets`, the net change they would make, and with `scope` \"sheet\" "
                              r"\(a sheet a parent uses\) a join through the parent sheet is not seen\.")


def test_the_dry_run_sentence_names_only_tools_that_take_dry_run_and_the_first_four_return_nets():
    match = DRY_RUN_SENTENCE.search(_walk())
    assert match, "the dry_run sentence of the walk changed"
    names = re.findall(r"`(\w+)`", match.group(1))
    assert names == ["add_symbols", "add_wires", "add_labels", "connect_pins", "rotate_items", "mirror_items",
                     "replace_symbols", "add_no_connects"]
    tools = _tools(schematic_toolset)
    for name in names:
        assert "dry_run" in inspect.signature(tools[name]).parameters, name
    for name in names[:4]:  # the docstrings say `nets` and the sheet scope (behaviour: tests/test_add_dry_run.py)
        doc = " ".join((inspect.getdoc(tools[name]) or "").split())
        assert "`nets.scope` is \"project\"" in doc or "scope \"project\"" in doc, name
    assert "scope \"sheet\"" in " ".join((inspect.getdoc(tools["add_symbols"]) or "").split())


def test_the_hierarchical_item_describes_the_guard_not_an_unsafe_toolset():
    start = SCHEMATIC.index("**Hierarchical (multi-sheet) projects")
    item = SCHEMATIC[start:SCHEMATIC.index("**Never hand-edit")]
    assert "these tools are unsafe" not in SCHEMATIC and "Until the tools guard against this" not in SCHEMATIC
    assert "low-level `kicad_tools.schema.Schematic`" not in SCHEMATIC
    for needle in ("the tools that place symbols are guarded", "`add_symbols` and `add_labels` with kind `power` attach it",
                   "`instance_data` (`reattached`, `reason`, `note`)", "`unproven_instance_data`",
                   "`instance_reason`: `tree_problems`, `root_not_found` or `walk_failed`) and write nothing",
                   "Wires, junctions, labels and no-connects hold no instance data"):
        assert needle in item, needle
    arch = (REPO / "docs" / ("ARCHITECTURE" + ".md")).read_text(encoding="utf-8")
    bullet = next(line for line in arch.splitlines() if line.startswith("- **Hierarchical schematics.**"))
    assert "no guard" not in bullet and "unproven_instance_data" in bullet


def test_the_instance_data_guard_the_item_names_exists_in_the_tool_texts():
    tools = _tools(schematic_toolset)
    for name in ("add_symbols", "add_labels"):
        doc = " ".join((inspect.getdoc(tools[name]) or "").split())
        assert "unproven_instance_data" in doc, name
    assert "instance_reason (tree_problems, root_not_found, walk_failed)" in " ".join((inspect.getdoc(tools["add_symbols"]) or "").split())


def test_the_walk_pins_the_forms_it_teaches():
    walk = _walk()
    for needle in ("**The walk from a fetched part to a wired symbol: one tool per step, in this order.**",
                   "`project_parts:<symbol name>`", "one `{reference: footprint_id}` mapping", "found from the netlist",
                   "for a hierarchical project `check_project`", "`diff_schematic` compares symbols and nets with an earlier state"):
        assert needle in walk, needle
    assert "its walk starts at the adoption, steps a and b" in SOURCING
    assert "unless `output_dir` was given to `propose_adopt_parts`" in SOURCING


# ---------------------------------------------------------------- behaviour


def _props(sch: Path, ref: str) -> dict[str, str]:
    for c in write_layer.load(sch).children:
        if not c.is_atom and c.name == "symbol" and c.find_children("lib_id") and sheet_editing._ref(c) == ref:
            return {p.get_string(0): p.get_string(1) for p in sheet_editing._kids(c, "property")}
    raise AssertionError(ref)


def test_a_part_without_a_datasheet_rejects_the_whole_call(tmp_path, _cli):
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    good = _part_assets(tmp_path, "C100")
    bad = _part_assets(tmp_path, "C200")
    from tests.test_part_adoption import _symbol_file

    _symbol_file(Path(bad["symbol_path"]), "R_C200", datasheet="~")
    result = _tools(library_toolset)["propose_adopt_parts"](made["pcb_path"], [good, bad], output_dir=str(tmp_path / "rev"), **COMMON)
    assert result["status"] == "blocked" and not result.get("revision_token"), result
    assert any("Datasheet is missing" in str(p) for part in result["parts"] for p in part.get("problems", [])), result


def test_the_walk_from_adoption_to_a_wired_symbol(tmp_path, _cli):
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    source_sch = Path(made["schematic_path"])
    source_before = {p.name: p.read_bytes() for p in source_sch.parent.iterdir() if p.is_file()}
    lib = _tools(library_toolset)
    assets = _part_assets(tmp_path, "C100")
    proposed = lib["propose_adopt_parts"](made["pcb_path"], [assets], **COMMON)  # no output_dir: the default folder
    assert proposed["status"] == "proposed", proposed
    confirmed = lib["confirm_adopt_parts"](made["pcb_path"], proposed["revision_token"])
    assert confirmed["status"] == "adopted", confirmed
    revision = Path(confirmed["pcb_path"]).parent
    assert revision.name == "proj_adopted" and revision.parent == (tmp_path)
    assert confirmed["source_pcb"] == made["pcb_path"]
    assert {p.name: p.read_bytes() for p in source_sch.parent.iterdir() if p.is_file()} == source_before  # source untouched
    sch = revision / source_sch.name
    assert sch.is_file()  # the revision holds the schematic: all further work happens there
    sch_tools = _tools(schematic_toolset)
    placed = sch_tools["add_symbols"](str(sch), [
        {"lib_id": "project_parts:R_C100", "x": 50.8, "y": 50.8, "ref": "R1"},
        {"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R2", "rotation": 180},
    ])
    assert placed["status"] == "ok", placed
    adopted = _props(sch, "R1")
    assert adopted["Footprint"] == "project_parts:R_C100" and adopted["Datasheet"] == "https://example.test/ds.pdf"
    assert adopted["LCSC"] == "C100"
    assert _props(sch, "R2")["Footprint"] == ""  # a library symbol with no Footprint: empty until assign_footprints
    gaps = sch_tools["check_symbol_fields"](str(sch))
    flagged = {s["reference"]: {g["field"] for g in s["gaps"]} for sheet in gaps["sheets"] for s in sheet["symbols"]}
    assert "R1" not in flagged and "Footprint" in flagged["R2"], flagged
    assert sch_tools["assign_footprints"](str(sch), {"R2": "Resistor_SMD:R_0402_1005Metric"})["status"] == "ok"
    assert _props(sch, "R2")["Footprint"] == "Resistor_SMD:R_0402_1005Metric"
    # rotation 180 reverses the pin order: Device:R pin 1 is the top pin (smaller y) at 0, the bottom pin at 180
    sch_tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 152.4, "y": 50.8, "ref": "R3"}])
    pins = {s["reference"]: {p["number"]: p["y"] for p in s["pins"]} for s in sch_tools["get_symbol_pins"](str(sch), references=["R2", "R3"])["symbols"]}
    assert pins["R3"]["1"] < 50.8 < pins["R3"]["2"], pins
    assert pins["R2"]["1"] > 50.8 > pins["R2"]["2"], pins
    # the walk's wiring step: connect, mark the open pin, then the sheet shows the pin as joined
    wired = sch_tools["connect_pins"](str(sch), [{"from": "R2.2", "to": "R3.1", "net_label": "N1"}], dry_run=True)
    assert wired.get("ok") is not False, wired
    marked = sch_tools["add_no_connects"](str(sch), [{"references": ["R1"]}])
    assert marked.get("ok") is not False and marked["status"] == "ok", marked


def test_the_pin_side_is_the_librarys_and_the_instance_only_turns_or_mirrors(tmp_path, _cli):
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    sch = Path(made["schematic_path"])
    sch_tools = _tools(schematic_toolset)
    specs = [
        {"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1"},
        {"lib_id": "Device:R", "x": 76.2, "y": 50.8, "ref": "R2", "rotation": 90},
        {"lib_id": "Device:R", "x": 101.6, "y": 50.8, "ref": "R3", "mirror": "x"},
    ]
    assert sch_tools["add_symbols"](str(sch), specs)["status"] == "ok"
    tree = write_layer.load(sch)
    instances = [c for c in tree.children if not c.is_atom and c.name == "symbol" and c.find_children("lib_id")]
    assert len(instances) == 3
    for inst in instances:  # an instance holds no pin position: its pins are (pin "n" (uuid ..)) only
        assert all(not p.find_children("at") for p in inst.find_children("pin"))
    pins = {s["reference"]: [(p["x"], p["y"]) for p in s["pins"]] for s in sch_tools["get_symbol_pins"](str(sch), references=["*"])["symbols"]}
    assert pins["R1"][0][0] == pins["R1"][1][0]  # upright: the two pins stand on one vertical
    assert pins["R2"][0][1] == pins["R2"][1][1]  # turned by 90 degrees: they lie on one horizontal
    assert pins["R3"][0][1] > pins["R3"][1][1]  # mirrored about x: pin 1 is the bottom pin
