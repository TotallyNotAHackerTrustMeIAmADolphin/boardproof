"""No tool writes a KiCad file or project while KiCad holds its lock file (GitHub issue 68).

``kicad_lock`` (build ticket 41) finds the locks; this file holds the wiring to its word:

* the write layer refuses first, before it writes anything, and once more right before the move;
* every writer that bypasses the layer (the schematic writer, ``generate_footprint``, the
  KiKit panelizer, the asset fetcher, ``fix_annotation``) calls the same check;
* the checkpoint revision builder refuses on a locked source project, and its copies leave
  lock files behind;
* every tool the server lists is classified as a writer or not, a writer carries the one pinned
  sentence in its description, and a new tool without a class turns the test red;
* an AST scan lists the functions that write KiCad files outside the layer; a new one turns red.

Fake lock files on ``tmp_path`` only: no test touches a real lock file.
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest
from mcp.client.client import Client
from mcp.server.mcpserver import MCPServer

from kicad_mcp import checkpoint, kicad_lock, write_layer
from kicad_mcp.server import build_server
from kicad_mcp.toolsets import project as project_toolset
from kicad_mcp.toolsets import routing as routing_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset

SRC = Path(write_layer.__file__).parent
CODE = "file_open_in_kicad"
LIB_TABLE_BYTES = b"(fp_lib_table\n)\n"
OTHER_HOST =b'{"hostname":"SOMEONE-ELSES-PC","username":"someone"}'


def _lock(target: Path, content: bytes = OTHER_HOST) -> Path:
    lock = target.with_name(f"~{target.name}.lck")
    lock.write_bytes(content)
    return lock


def _text(result) -> str:
    """The result as JSON text with the path separators un-doubled, so a lock path can be found in it."""
    return json.dumps(result).replace("\\\\", "\\")


def _snapshot(directory: Path) -> dict[str, bytes]:
    return {str(p.relative_to(directory)): p.read_bytes() for p in sorted(directory.rglob("*")) if p.is_file()}


def _tools(module) -> dict:
    mcp = MCPServer("test")
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def project(tmp_path):
    made = project_toolset._project_create(str(tmp_path / "proj"), "demo")
    assert made["status"] == "created", made
    return {
        "dir": tmp_path / "proj",
        "pcb": Path(made["pcb_path"]),
        "sch": Path(made["schematic_path"]),
        "pro": Path(made["project_path"]),
    }


# --------------------------------------------------------------------------- the layer


def _assert_refused(exc: write_layer.WriteLayerError, lock: Path) -> None:
    assert exc.code == CODE
    assert str(lock) in str(exc), "the message must name the lock file"
    assert exc.details["lock_file"] == str(lock)
    assert "close" in str(exc).lower()


def _layer_calls(project):
    pcb, pro, d = project["pcb"], project["pro"], project["dir"]
    root = write_layer.load(pcb)
    lib_table = d / "fp-lib-table"
    write_layer.save_bytes(LIB_TABLE_BYTES, lib_table, verify=lambda p: None)
    revision = d.parent / "rev"
    revision.mkdir()
    return {
        "save": (pcb, lambda: write_layer.save(root, pcb)),
        "save_pcb": (pcb, lambda: write_layer.save_pcb(root, pcb)),
        "save_project": (pro, lambda: write_layer.save_project(json.loads(pro.read_text()), pro)),
        "save_bytes": (pro, lambda: write_layer.save_bytes(b"(fp_lib_table\n)\n", lib_table, verify=lambda p: None)),
        "commit_directory": (d.parent / "rev2", lambda: write_layer.commit_directory(revision, d.parent / "rev2")),
    }


@pytest.mark.parametrize("name", ["save", "save_pcb", "save_project", "save_bytes", "commit_directory"])
def test_each_write_layer_entry_point_refuses_a_locked_file_and_changes_nothing(project, name):
    target, call = _layer_calls(project)[name]
    lock = _lock(target)
    before = _snapshot(project["dir"].parent)
    with pytest.raises(write_layer.WriteLayerError) as caught:
        call()
    _assert_refused(caught.value, lock)
    assert _snapshot(project["dir"].parent) == before, "a refusal must leave every file as it was (no temp file either)"
    assert lock.exists(), "no tool deletes a lock file"


def test_a_lock_that_appears_while_the_checks_run_is_caught_before_the_move(project):
    pcb = project["pcb"]
    root = write_layer.load(pcb)
    before = pcb.read_bytes()
    made: list[Path] = []

    def verify(_temp):
        made.append(_lock(pcb))

    with pytest.raises(write_layer.WriteLayerError) as caught:
        write_layer.save(root, pcb, verify=verify)
    _assert_refused(caught.value, made[0])
    assert pcb.read_bytes() == before
    assert not [p for p in pcb.parent.iterdir() if p.suffix == ".tmp"], "the temporary file is removed"


def test_without_a_lock_the_layer_still_writes(project):
    root = write_layer.load(project["pcb"])
    assert write_layer.save(root, project["pcb"]) == project["pcb"]


def test_a_project_lock_alone_does_not_stop_a_board_write_but_stops_the_project_file(project):
    _lock(project["pro"])
    root = write_layer.load(project["pcb"])
    write_layer.save(root, project["pcb"])  # kicad_lock's rule: a board has its own lock
    with pytest.raises(write_layer.WriteLayerError) as caught:
        write_layer.save_project(json.loads(project["pro"].read_text()), project["pro"])
    assert caught.value.code == CODE


def test_reads_stay_allowed_with_every_lock_present(project):
    for f in (project["pcb"], project["sch"], project["pro"]):
        _lock(f)
    assert write_layer.load(project["pcb"]).name == "kicad_pcb"
    assert write_layer.load(project["sch"]).name == "kicad_sch"
    assert json.loads(project["pro"].read_text())
    tools = _tools(routing_toolset)
    assert "status" in tools["get_unrouted_nets"](str(project["pcb"]))


def test_a_stale_lock_is_explained_and_still_refuses(project, monkeypatch):
    me = json.dumps({"hostname": kicad_lock.current_host(), "username": kicad_lock.current_user()}).encode()
    lock = _lock(project["pcb"], me)
    monkeypatch.setattr(kicad_lock, "kicad_processes_running", lambda **kw: [])
    with pytest.raises(write_layer.WriteLayerError) as caught:
        write_layer.save(write_layer.load(project["pcb"]), project["pcb"])
    text = str(caught.value)
    assert "stale" in text and str(lock) in text
    assert lock.exists() and lock.read_bytes() == me, "a stale lock is never deleted or rewritten"
    assert caught.value.details["locks"][0]["stale"] is True


def test_a_live_looking_lock_is_never_called_stale(project):
    lock = _lock(project["pcb"])
    with pytest.raises(write_layer.WriteLayerError) as caught:
        write_layer.check_writable(project["pcb"])
    assert "stale" not in str(caught.value) and lock.exists()


# --------------------------------------------------------------------------- writers outside the layer


def test_add_traces_refuses_with_a_lock_and_the_board_is_unchanged(project):
    tools = _tools(routing_toolset)
    spec = [{"start": [5, 5], "end": [9, 5]}]
    lock = _lock(project["pcb"])
    before = project["pcb"].read_bytes()
    result = tools["add_traces"](str(project["pcb"]), spec)
    assert CODE in _text(result) and str(lock) in _text(result)
    assert project["pcb"].read_bytes() == before
    lock.unlink()
    assert tools["add_traces"](str(project["pcb"]), spec)["status"] in ("ok", "complete"), "control: it writes without the lock"
    assert project["pcb"].read_bytes() != before


@pytest.mark.parametrize(
    "tool, args",
    [
        ("add_wires", [{"p1": [50, 50], "p2": [60, 50]}]),
        ("add_labels", [{"kind": "label", "x": 50, "y": 50, "text": "NETA"}]),
    ],
)
def test_schematic_writers_refuse_with_a_root_lock_and_the_sheet_is_unchanged(project, tool, args):
    tools = _tools(schematic_toolset)
    lock = _lock(project["sch"])
    before = project["sch"].read_bytes()
    result = tools[tool](str(project["sch"]), args)
    assert CODE in _text(result) and str(lock) in _text(result)
    assert project["sch"].read_bytes() == before
    assert not list(project["dir"].glob("*.tmp")) and not list(project["dir"].glob("*.utf8.tmp"))
    lock.unlink()
    assert tools[tool](str(project["sch"]), args)["status"] == "ok", "control: it writes without the lock"


def test_fix_annotation_refuses_before_its_backup_and_its_in_place_write(project):
    tools = _tools(schematic_toolset)
    lock = _lock(project["sch"])
    before = _snapshot(project["dir"])
    result = tools["fix_annotation"](str(project["sch"]))
    assert CODE in _text(result) and str(lock) in _text(result)
    assert _snapshot(project["dir"]) == before


def test_project_create_overwrite_refuses_when_any_of_its_files_is_locked(project):
    lock = _lock(project["pro"])
    before = _snapshot(project["dir"])
    with pytest.raises(write_layer.WriteLayerError) as caught:
        project_toolset._project_create(str(project["dir"]), "demo", overwrite=True)
    _assert_refused(caught.value, lock)
    assert _snapshot(project["dir"]) == before


def test_project_create_in_a_directory_holding_a_foreign_lock_writes_nothing(tmp_path):
    target = tmp_path / "new"
    target.mkdir()
    lock = _lock(target / "demo.kicad_sch")
    with pytest.raises(write_layer.WriteLayerError) as caught:
        project_toolset._project_create(str(target), "demo")
    _assert_refused(caught.value, lock)
    assert [p.name for p in target.iterdir()] == [lock.name]


def test_generate_footprint_refuses_a_locked_output_file(tmp_path):
    from kicad_mcp.sourcing.footprint_gen import generate_footprint

    out = tmp_path / "lib.pretty" / "R.kicad_mod"
    out.parent.mkdir()
    lock = _lock(out)
    result = generate_footprint("chip", out, size="0402")
    assert result["status"] == "incomplete" and result["code"] == CODE and str(lock) in result["reason"]
    assert not out.exists()


def test_register_library_asset_refuses_when_the_project_lib_table_is_guarded(project):
    from kicad_mcp.sourcing.footprint_gen import generate_footprint

    fp_dir = project["dir"] / "My.pretty"
    fp_dir.mkdir()
    assert generate_footprint("chip", fp_dir / "R.kicad_mod", size="0402")["status"] == "ok"
    lock = _lock(project["pro"])  # KiCad rewrites the lib tables with the project
    tools = _tools(__import__("kicad_mcp.toolsets.library", fromlist=["build"]))
    result = tools["register_library_asset"](str(project["dir"]), footprint_lib_path=str(fp_dir))
    assert CODE in _text(result) and str(lock) in _text(result)
    assert not (project["dir"] / "fp-lib-table").exists()


# --------------------------------------------------------------------------- checkpoint revisions


def test_build_revision_refuses_a_locked_source_project_and_writes_nothing(project):
    lock = _lock(project["pcb"])
    out_dir = project["dir"].parent / "revision"
    called: list[Path] = []
    result = checkpoint.build_revision(
        out_dir,
        source_pcb=project["pcb"],
        write=lambda tmp: called.append(tmp),
        verify=lambda tmp, state: {},
        success="done",
        before_verified="could not build",
    )
    assert result["status"] == "incomplete" and result["code"] == CODE and str(lock) in result["reason"]
    assert not called and not out_dir.exists()
    assert not [p for p in out_dir.parent.iterdir() if p.name.startswith(".revision")]


def _build(project, out_name="revision"):
    out_dir = project["dir"].parent / out_name
    called: list[Path] = []
    result = checkpoint.build_revision(
        out_dir,
        source_pcb=project["pcb"],
        write=lambda tmp: called.append(tmp) or checkpoint.copy_project(project["pcb"], tmp),
        verify=lambda tmp, state: {"verified": True},
        success="built",
        before_verified="could not build",
    )
    return result, called, out_dir


@pytest.mark.parametrize("lock_of", ["pcb", "pro"])
def test_build_revision_refuses_on_this_projects_own_board_and_project_lock(project, lock_of):
    lock = _lock(project[lock_of])
    result, called, out_dir = _build(project)
    assert result["code"] == CODE and result["lock_file"] == str(lock) and not called and not out_dir.exists()


@pytest.mark.parametrize(
    "make_lock",
    [
        pytest.param(lambda p: (p["dir"] / "~demo.kicad_pcbX.lck"), id="prefix-only name"),
        pytest.param(lambda p: (p["dir"] / "~other.kicad_pcb.lck"), id="another project's board lock"),
        pytest.param(lambda p: (p["dir"] / "~other.kicad_pro.lck"), id="another project's project lock"),
        pytest.param(lambda p: (p["dir"] / "~demo.kicad_sch.lck"), id="root-schematic lock, board-only revision"),
        pytest.param(lambda p: (p["dir"] / "sub-backups" / "~demo.kicad_pcb.lck"), id="lock inside a backup folder"),
        pytest.param(lambda p: (p["dir"] / "sheets" / "~part.kicad_sch.lck"), id="sub-sheet lock"),
    ],
)
def test_build_revision_does_not_refuse_on_a_lock_that_is_not_this_projects_board_or_project(project, make_lock):
    lock = make_lock(project)
    lock.parent.mkdir(exist_ok=True)
    lock.write_bytes(OTHER_HOST)
    result, called, out_dir = _build(project)
    assert result["status"] == "built", result
    assert called and out_dir.exists()
    assert lock.exists(), "the lock is left where it is"
    assert not [p for p in out_dir.rglob("*") if kicad_lock.is_lock_name(p.name)], "the revision holds no lock"


def test_build_revision_without_a_source_is_a_type_error(project):
    with pytest.raises(TypeError):
        checkpoint.build_revision(  # type: ignore[call-arg]
            project["dir"].parent / "r", write=lambda t: None, verify=lambda t, s: {}, success="x", before_verified="y"
        )


def test_a_revision_copy_leaves_lock_files_behind(project):
    _lock(project["pro"])  # a project lock alone does not forbid a board revision copy...
    copy = checkpoint.copy_project(project["pcb"], project["dir"].parent / "copy")
    assert not [p for p in copy.parent.rglob("*") if kicad_lock.is_lock_name(p.name)]
    # ...and the copy can be written through the layer
    write_layer.save_project(json.loads((copy.parent / "demo.kicad_pro").read_text()), copy.parent / "demo.kicad_pro")


def test_the_grading_copy_leaves_lock_files_behind(project):
    from kicad_mcp import verify_board

    _lock(project["pro"])
    copy = verify_board._copy_project(project["pcb"], project["dir"].parent / "graded")
    assert not [p for p in copy.parent.rglob("*") if kicad_lock.is_lock_name(p.name)]


# --------------------------------------------------------------------------- the tool listing

# Tools that write a KiCad file or project folder in place or into a named place, by tool name.
WRITING_TOOLS = frozenset({
    "confirm_input_preparation", "confirm_pour_step", "confirm_schematic_import", "confirm_board_rules",
    "confirm_board_outline", "confirm_silkscreen_minimums", "confirm_adopt_parts", "confirm_autoplacement",
    "confirm_placement_optimization", "confirm_remove_graphic_items", "confirm_route",
    "project_create", "generate_footprint", "register_library_asset", "fetch_part_assets", "panelize_board",
    "add_symbols", "add_wires", "add_junctions", "add_labels", "assign_footprints", "fix_annotation",
    "create_sheets", "add_sheet_symbols", "add_sheet_pins", "remove_sheet_pins", "move_sheet_pins",
    "delete_symbols", "delete_wires", "delete_labels", "delete_junctions", "move_symbols", "move_labels",
    "set_property_positions", "connect_pins", "replace_symbols",
    "place_footprints", "move_reference_designators_to_layer", "align_footprints", "distribute_footprints",
    "snap_footprints_to_grid", "pack_footprints_densely", "add_traces",
})

# Reviewed: tools that write no KiCad design file or library. Each reason is a claim for the reviewer.
NOT_WRITING_TOOLS = {
    "list_toolsets": "registry", "load_toolset": "registry", "unload_toolset": "registry",
    "verify_board": "reads a scratch copy", "pin_rule_baseline": "read-only snapshot",
    "propose_input_preparation": "dry run: writes nothing", "propose_pour_step": "dry run on scratch copies",
    "propose_schematic_import": "dry run", "propose_board_rules": "dry run", "propose_board_outline": "dry run",
    "propose_silkscreen_minimums": "dry run", "project_open": "read-only",
    "pcb_run_drc": "kicad-cli report only (never --save-board)", "pcb_check_placement": "read-only",
    "sch_run_erc": "kicad-cli report only", "pcb_check_collisions": "read-only",
    "pcb_validate_proposed_placements": "read-only", "get_manufacturer_design_rules": "read-only",
    "list_manufacturer_profiles": "read-only", "pcb_get_design_rules": "read-only",
    "pcb_check_manufacturability": "read-only", "search_parts": "network read", "list_sourcing_providers": "read-only",
    "select_component": "no file", "propose_component_match": "no file",
    "confirm_component_match": "returns the chosen candidate, writes no file",
    "lookup_jlcpcb_part_tiers": "read-only", "jlcpcb_library_status": "read-only", "resolve_jlcpcb_assets": "read-only",
    "fetch_datasheet": "writes a PDF", "inspect_datasheet": "reads a PDF, writes PNG crops",
    "list_package_types": "read-only", "render_board_diagnostic": "writes an image from a kicad-cli export",
    "render_schematic_diagnostic": "writes an image", "check_project_portability": "read-only",
    "validate_klc": "read-only", "propose_adopt_parts": "dry run on scratch copies", "check_connectivity": "read-only", "get_symbol_pins": "read-only",
    "propose_autoplacement": "dry run", "propose_placement_optimization": "trial copies in scratch",
    "propose_remove_graphic_items": "dry run", "export_manufacturing_package": "writes fab outputs, no design file",
    "diagnose_placement_congestion": "read-only", "get_unrouted_nets": "read-only",
    "propose_route_with_freerouting": "trial copies in scratch", "propose_route_with_retry": "trial copies in scratch",
    "copilot_status": "IPC read", "copilot_connect": "IPC", "copilot_disconnect": "IPC",
    "copilot_get_board_state": "IPC read", "copilot_get_schematic_state": "IPC read", "copilot_get_selection": "IPC read",
    "copilot_highlight": "IPC: the open KiCad itself", "copilot_propose_moves": "dry run",
    "copilot_apply_proposal": "applied by the running KiCad itself, which owns the lock",
    "export_spice_netlist": "writes a netlist", "run_spice_simulation": "writes simulation output",
}


async def _listing() -> dict[str, str]:
    async with Client(build_server(), mode="auto") as client:
        listed = await client.call_tool("list_toolsets", {})
        toolsets = listed.structured_content["result"] if listed.structured_content is not None \
            else json.loads(listed.content[0].text)["result"]
        for toolset in toolsets:
            await client.call_tool("load_toolset", {"name": toolset["name"]})
        return {t.name: t.description or "" for t in (await client.list_tools()).tools}


@pytest.mark.asyncio
async def test_every_listed_tool_is_classified_as_a_writer_or_not():
    names = set(await _listing())
    unclassified = sorted(names - WRITING_TOOLS - set(NOT_WRITING_TOOLS))
    assert not unclassified, (
        f"New tool(s) {unclassified}: add each to WRITING_TOOLS (it must call write_layer.check_writable or write "
        "through the write layer, and carry write_layer.LOCK_SENTENCE in its description) or to NOT_WRITING_TOOLS "
        "with a reason, in tests/test_write_guard.py."
    )
    assert not WRITING_TOOLS & set(NOT_WRITING_TOOLS)
    gone = sorted((WRITING_TOOLS | set(NOT_WRITING_TOOLS)) - names)
    assert not gone, f"classified tools that no longer exist: {gone}"


@pytest.mark.asyncio
async def test_every_writing_tool_carries_the_pinned_sentence_and_no_other_tool_does():
    listing = await _listing()
    flat = {name: " ".join(text.split()) for name, text in listing.items()}
    missing = sorted(n for n in WRITING_TOOLS if write_layer.LOCK_SENTENCE not in flat[n])
    assert not missing, f"description lacks write_layer.LOCK_SENTENCE: {missing}"
    stray = sorted(n for n in NOT_WRITING_TOOLS if write_layer.LOCK_SENTENCE in flat[n])
    assert not stray, f"description claims a lock refusal but the tool is classed as not writing: {stray}"


@pytest.mark.asyncio
async def test_project_create_says_that_its_lock_refusal_is_a_tool_error():
    flat = " ".join((await _listing())["project_create"].split())
    assert project_toolset.PROJECT_CREATE_LOCK_NOTE in flat
    assert "tool error" in flat and "not a result dictionary" in flat


def test_the_pinned_sentence_names_the_code_and_the_remedy_and_claims_nothing_else():
    s = write_layer.LOCK_SENTENCE
    assert CODE in s and "naming the lock file" in s and "lock file" in s and "close" in s.lower()
    assert not re.search(r"\b(complete|done|routed|verified|ready|clean|passes)\b", s, re.I)
    assert "\n" not in s and s.endswith(".")


# --------------------------------------------------------------------------- the code enumeration


def _calls(node: ast.AST) -> set[str]:
    out = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
    return out


def _own_calls(fn: ast.AST) -> set[str]:
    """Calls in ``fn`` itself, not in the functions nested inside it (those are judged on their own)."""
    out: set[str] = set()
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        if isinstance(n, ast.Call):
            f = n.func
            out.add(f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", ""))
        stack.extend(ast.iter_child_nodes(n))
    return out


def _function(tree: ast.Module, qualname: str) -> ast.FunctionDef:
    parts = qualname.split(".")
    scope: ast.AST = tree
    for part in parts:
        scope = next(
            n for n in ast.walk(scope) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == part
        )
    return scope  # type: ignore[return-value]


@pytest.mark.parametrize("name", ["save", "save_project", "save_bytes", "commit_directory", "_commit"])
def test_each_write_layer_writer_calls_the_check(name):
    tree = ast.parse((SRC / "write_layer.py").read_text(encoding="utf-8"))
    assert "check_writable" in _calls(_function(tree, name)), f"write_layer.{name} must call check_writable"


# (file, function) -> the check it must call. Every function in src/ that writes a KiCad file
# or project folder by a route other than write_layer.save*/commit_directory is listed here.
OUTSIDE_WRITERS = {
    ("toolsets/schematic.py", "_write_schematic_utf8"): "check_writable",
    ("toolsets/schematic.py", "build.fix_annotation"): "check_writable",
    ("sourcing/footprint_gen.py", "generate_footprint"): "check_writable",
    ("toolsets/project.py", "_project_create"): "check_writable",
    ("toolsets/manufacturing.py", "panelize_board"): "check_writable",
    ("sourcing/asset_fetch.py", "fetch_part_assets"): "check_writable",
    ("checkpoint.py", "build_revision"): "check_project_writable",
    ("pour_step.py", "_carry_over"): "check_project_writable",
    ("pour_step.py", "confirm_pour_step"): "check_project_writable",
    ("rung_runner.py", "run_rung"): "check_project_writable",
}
# Reviewed writers of a KiCad file that stay unguarded, with the reason.
UNGUARDED_REVIEWED = {
    ("routers/kicad_routing_tools.py", "_restore_original_project"): "restores the .kicad_pro of a scratch trial copy",
}
# Reviewed: functions that write or rename a file that is not a KiCad design or library file.
NON_KICAD_RAW = {
    ("routers/kicad_routing_tools.py", "write_fab_overrides"): "KRT --fab-overrides text file in a scratch dir",
    ("routers/kicad_routing_tools.py", "request_graceful_stop"): "a stop-request marker file for a router run",
    ("rung_runner.py", "_write_json"): "run records (JSON) in the ladder's own run directory",
    ("sourcing/jlcpcb_basic.py", "write_index"): "the JLCPCB parts index (JSON), not a KiCad file",
    ("_png_worker.py", "main"): "diagnostic PNG raster written into the child process's own temp dir (issue 104)",
    ("png_render.py", "render_pdf_pages"): "moves finished diagnostic PNGs from scratch into the caller's output dir (issue 104)",
    ("toolsets/manufacturing.py", "_bom_from_root"): "BOM CSV output",
    ("toolsets/simulation.py", "run_spice_simulation"): "SPICE run output file",
}
_DOTTED_WRITES = {"os.replace", "os.rename", "shutil.move"}
_METHOD_WRITES = {"write_text", "write_bytes", "save", "run_fix_annotation"}


def _raw_write_hits(fn: ast.AST) -> set[str]:
    """Names of the file-writing calls in ``fn`` itself: ``os.replace``, ``os.rename``, ``shutil.move``,
    a one-argument ``.replace(x)``/``.rename(x)`` (``Path``'s; ``str.replace`` takes two), and the
    methods in ``_METHOD_WRITES``."""
    hits: set[str] = set()
    stack = list(ast.iter_child_nodes(fn))
    while stack:
        n = stack.pop()
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            continue
        stack.extend(ast.iter_child_nodes(n))
        if not isinstance(n, ast.Call):
            continue
        f = n.func
        if isinstance(f, ast.Attribute):
            base = f.value.id if isinstance(f.value, ast.Name) else ""
            if f"{base}.{f.attr}" in _DOTTED_WRITES:
                hits.add(f"{base}.{f.attr}")
            elif base == "write_layer":
                continue  # the layer checks for itself
            elif f.attr in _METHOD_WRITES:
                hits.add(f.attr)
            elif f.attr in ("replace", "rename") and len(n.args) == 1 and not n.keywords:
                hits.add(f"Path.{f.attr}")
        elif isinstance(f, ast.Name) and f.id == "run_fix_annotation":
            hits.add("run_fix_annotation")
    return hits


def _qualnames(tree: ast.Module):
    def walk(node, prefix):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                q = f"{prefix}{child.name}"
                yield q, child
                yield from walk(child, q + ".")
            elif isinstance(child, ast.ClassDef):
                yield from walk(child, f"{prefix}{child.name}.")

    yield from walk(tree, "")


def test_every_listed_outside_writer_calls_its_check():
    for (rel, qual), check in OUTSIDE_WRITERS.items():
        tree = ast.parse((SRC / rel).read_text(encoding="utf-8"))
        found = [fn for q, fn in _qualnames(tree) if q == qual or q.endswith("." + qual)]
        assert found, f"{rel}: {qual} is gone; update OUTSIDE_WRITERS"
        assert check in _calls(found[0]), f"{rel}: {qual} writes a KiCad file and must call {check}"


def test_a_new_raw_writer_of_a_kicad_file_outside_the_layer_turns_this_red():
    """Any function outside write_layer that calls os.replace/os.rename/shutil.move/Footprint.save/
    Schematic.write/run_fix_annotation or writes text or bytes must be in OUTSIDE_WRITERS or
    UNGUARDED_REVIEWED, or not touch KiCad files. Non-KiCad writers are reviewed in NON_KICAD_RAW."""
    unknown = []
    for path in sorted(SRC.rglob("*.py")):
        rel = path.relative_to(SRC).as_posix()
        if rel in ("write_layer.py", "kicad_lock.py"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for qual, fn in _qualnames(tree):
            hits = _raw_write_hits(fn)
            if not hits:
                continue
            key = (rel, qual)
            if key in OUTSIDE_WRITERS or key in UNGUARDED_REVIEWED or key in NON_KICAD_RAW:
                continue
            unknown.append(f"{rel}:{qual} calls {sorted(hits)}")
    assert not unknown, (
        "These functions write or rename files outside the write layer. If they touch a KiCad file or "
        "project folder, call write_layer.check_writable(<paths>) before the write and list them in "
        "OUTSIDE_WRITERS; otherwise list them in NON_KICAD_RAW with a reason:\n  " + "\n  ".join(unknown)
    )


