"""Ticket 08: the rung-2 pin and the staged input (staged through the write layer since build
ticket 67, protocol 5)."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

from kicad_mcp import autonomy_run, rung_runner

ROOT = Path(__file__).resolve().parents[1]


def _stage_module():
    spec = importlib.util.spec_from_file_location("stage_input", ROOT / "evals" / "rung2" / "stage_input.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_rung2_is_pinned_without_touching_plan_or_budget():
    protocol = rung_runner.load_protocol()
    entry = protocol["rungs"]["2"]
    assert len(entry["board_sha256"]) == 64 and len(entry["project_sha256"]) == 64
    assert protocol["attempts"] == 5 and protocol["budget_s"] == 1800
    assert protocol["attempt_timeout_s"] == 300
    assert len(protocol["backends"]["krt"]["plan"]) == 5


def test_rung1_is_pinned_on_protocol_4():  # build ticket 51; values in test_autonomy_run_pins.py
    entry = rung_runner.load_protocol()["rungs"]["1"]
    assert len(entry["board_sha256"]) == 64 and len(entry["project_sha256"]) == 64


def _staged_from_the_install(kicad_demo, tmp_path: Path) -> tuple[dict, Path]:
    """The rung-2 input prepared at test time from the installed demo, through
    ``evals/rung2/stage_input.py`` (build ticket 34: no other machine's copy; the write layer
    since build ticket 67)."""
    staged = _stage_module().stage(kicad_demo("pic_programmer"), tmp_path / "elsewhere")
    return staged, Path(staged["board"])


def test_staged_input_carries_the_shas_the_runner_checks(kicad_demo, tmp_path):
    staged, pcb = _staged_from_the_install(kicad_demo, tmp_path)
    protocol = rung_runner.load_protocol()

    assert staged["board_sha256"] == autonomy_run.board_sha(pcb)
    assert staged["project_sha256"] == autonomy_run.project_sha(pcb)
    assert staged["board_sha256"] == protocol["rungs"]["2"]["board_sha256"]
    assert re.fullmatch(r"[0-9a-f]{64}", staged["project_sha256"]), staged["project_sha256"]
    reasons = autonomy_run._rung_reasons(
        2, autonomy_run.board_sha(pcb), autonomy_run.project_sha(pcb), protocol
    )
    assert reasons == []


STRIPPED = ("segment", "arc", "via", "zone")  # the tracks, vias and the zone: nothing else


def test_the_staged_board_is_kicads_own_file_with_only_the_tracks_removed(kicad_demo, tmp_path):
    """Build ticket 67. The oracle is the demo board KiCad wrote: the staged tree is that tree,
    token for token, without its segments, arcs, vias and zone. ``PCB.load`` adds a numeric net
    table (112 entries, ``(net 0 "")`` ... ``(net 111 ...)``) that KiCad 10's file does not
    have; protocol 4's staging carried it, protocol 5's does not. Both sides are read with the
    independent tokenizer below, not with the write layer or kicad_tools' parser, so a parser
    or serialiser fault that is the same on both sides cannot hide (review round 1)."""
    source_tokens = _tokens(_raw_text(kicad_demo("pic_programmer") / "pic_programmer.kicad_pcb"))
    _, pcb = _staged_from_the_install(kicad_demo, tmp_path)
    staged = _tokens(_raw_text(pcb))

    assert staged == _without_top_level(source_tokens, STRIPPED)
    names = _top_level_names(staged)
    assert "net" not in names
    assert names.count("footprint") == 63 and names.count("gr_text") == 19
    assert not set(STRIPPED) & set(names)
    assert len(source_tokens) - len(staged) > 10_000  # it did strip


# Parentheses, a quoted string exactly as written (escapes kept), or a bare atom.
_TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|[()]|[^\s()"]+', re.S)


def _raw_text(path: Path) -> str:
    """The file's text with no newline translation (delta review 1): a raw CR stays a CR."""
    with open(path, encoding="utf-8", newline="") as fh:
        return fh.read()


def _tokens(text: str) -> list[str]:
    """The raw token stream of an S-expression file; whitespace and line endings are not
    tokens, and nothing else may be skipped."""
    assert not _TOKEN.sub("", text).strip(), "text the tokenizer did not read"
    return _TOKEN.findall(text)


def _without_top_level(tokens: list[str], names: tuple[str, ...]) -> list[str]:
    """``tokens`` without the top-level lists (depth 1 under the root) named in ``names``."""
    out, depth, skipping = [], 0, False
    for i, tok in enumerate(tokens):
        if not skipping and tok == "(" and depth == 1 and tokens[i + 1] in names:
            skipping = True
        depth += {"(": 1, ")": -1}.get(tok, 0)
        if skipping:
            skipping = not (tok == ")" and depth == 1)
            continue
        out.append(tok)
    return out


def _top_level_names(tokens: list[str]) -> list[str]:
    names, depth = [], 0
    for i, tok in enumerate(tokens):
        if tok == "(" and depth == 1:
            names.append(tokens[i + 1])
        depth += {"(": 1, ")": -1}.get(tok, 0)
    return names


def test_kicad_tools_board_model_adds_the_net_table_the_history_cites(kicad_demo):
    """Build ticket 67 review: the reason protocol 5 records is the measured one. ``PCB.load``
    adds 112 top-level ``net`` entries (net 0 to net 111) that KiCad 10's file does not have."""
    from kicad_tools.schema import PCB

    from kicad_mcp import write_layer

    source = kicad_demo("pic_programmer") / "pic_programmer.kicad_pcb"
    nets = [c for c in PCB.load(source)._sexp.children if c.name == "net"]
    assert len(nets) == 112
    assert [int(n.values[0]) for n in nets] == list(range(112))
    assert not [c for c in write_layer.load(source).children if c.name == "net"]
    history = rung_runner.load_protocol()["version_history"]["5"]
    assert "112 entries, net 0 to net 111" in history
    assert "net 112" not in history
    for kept in ("_original_str", "_originally_quoted"):  # what the pin still relies on
        assert kept in history


def test_the_staging_refuses_a_board_whose_group_holds_a_stripped_item(kicad_demo, tmp_path):
    """Build ticket 67 review: removing a track a ``group`` lists would leave the group naming
    a uuid that no longer exists. The demo has no group; another board may. Refuse before any
    write: the board stays byte-equal."""
    from kicad_tools.sexp import SExp

    from kicad_mcp import write_layer

    stage = _stage_module()
    work = tmp_path / "grouped"
    import shutil

    shutil.copytree(kicad_demo("pic_programmer"), work)
    pcb = work / "pic_programmer.kicad_pcb"
    root = write_layer.load(pcb)
    segment = next(c for c in root.children if c.name == "segment")
    member = segment.find("uuid").values[0]
    root.children.append(SExp.list(
        "group", write_layer.string(""),
        SExp.list("uuid", write_layer.string("6f1c2a52-0000-4000-8000-000000000067")),
        SExp.list("members", write_layer.string(member)),
    ))
    write_layer.save(root, pcb)
    before = pcb.read_bytes()

    with pytest.raises(stage.StagingRefused, match=member):
        stage.strip_tracks(pcb)
    assert pcb.read_bytes() == before


def test_the_staging_refuses_any_node_whose_members_hold_a_stripped_item(kicad_demo, tmp_path):
    """Delta review 1: not only ``group`` lists members. A generated tuning pattern (KiCad's
    ``(generated ... (type tuning_pattern) ... (members ...))``) names the tracks it made;
    stripping them would leave it dangling. Refuse before any write."""
    from kicad_tools.sexp import SExp

    from kicad_mcp import write_layer

    stage = _stage_module()
    work = tmp_path / "generated"
    import shutil

    shutil.copytree(kicad_demo("pic_programmer"), work)
    pcb = work / "pic_programmer.kicad_pcb"
    root = write_layer.load(pcb)
    segment = next(c for c in root.children if c.name == "segment")
    member = segment.find("uuid").values[0]
    root.children.append(SExp.list(
        "generated",
        SExp.list("uuid", write_layer.string("6f1c2a52-0000-4000-8000-000000000068")),
        SExp.list("type", write_layer.symbol("tuning_pattern")),
        SExp.list("members", write_layer.string(member)),
    ))
    write_layer.save(root, pcb)
    before = pcb.read_bytes()

    with pytest.raises(stage.StagingRefused, match=member):
        stage.strip_tracks(pcb)
    assert pcb.read_bytes() == before


def test_the_staging_self_check_fires_on_a_corrupted_write(kicad_demo, tmp_path, monkeypatch):
    """Verify before replace: a write that lands a different tree (here one footprint fewer,
    still a board KiCad loads) is refused by the read-back compare; the board is untouched
    and no temporary file is left."""
    from kicad_mcp import write_layer

    stage = _stage_module()
    source = kicad_demo("pic_programmer")
    work = tmp_path / "work"
    real_save = write_layer.save
    seen = []

    def corrupted_save(root, path, *, verify=None):
        seen.append(verify)
        footprint = next(c for c in root.children if c.name == "footprint")
        root.children = [c for c in root.children if c is not footprint]
        return real_save(root, path, verify=verify)

    monkeypatch.setattr(write_layer, "save", corrupted_save)
    with pytest.raises(stage.StagingNotVerified):
        stage.stage(source, work)
    assert seen and seen[0] is not None
    board = work / "pic_programmer.kicad_pcb"
    assert board.read_bytes() == (source / "pic_programmer.kicad_pcb").read_bytes()
    assert sorted(p.name for p in work.iterdir()) == sorted(p.name for p in source.iterdir())


def test_the_staging_does_not_use_kicad_tools_board_model(kicad_demo, tmp_path, monkeypatch):
    """The pin must not hang on ``PCB.load`` (it adds a net table) or ``strip_traces``."""
    from kicad_tools.schema import PCB

    def refuse(*_args, **_kwargs):
        raise AssertionError("the rung-2 staging used kicad_tools' PCB model")

    monkeypatch.setattr(PCB, "load", refuse)
    monkeypatch.setattr(PCB, "strip_traces", refuse)
    staged, pcb = _staged_from_the_install(kicad_demo, tmp_path)
    assert staged["board_sha256"] == rung_runner.load_protocol()["rungs"]["2"]["board_sha256"]
    assert staged["removed"] == {"segment": 370, "arc": 0, "via": 6, "zone": 1}


# --- evals/: what its scripts write and launch (build ticket 67) -----------------------------
#
# The ratchet in tests/test_ratchets.py scans src/kicad_mcp. This one scans every .py file under
# evals/ and fails closed at the module boundary (fix round 3; two denylist rounds before it
# each missed a name):
# - references: every name a script uses from a module outside SAFE_MODULES, kicad_mcp and its
#   sibling scripts (subprocess, os, shutil, tempfile, asyncio, kicad_tools, pcbnew, mcp, any
#   third-party library) is resolved through the imports of its scope (``import X as Y``,
#   ``from X import name [as Y]``, attribute chains) and through the re-exports of kicad_mcp
#   modules and sibling scripts (``write_layer.os.replace`` is ``os.replace``), and must be a
#   reviewed entry of EVALS_REFERENCES keyed by (file, function, dotted name) with its count. A
#   kicad_tools writer is therefore a failure by default, whatever its name;
# - forbidden, never listable: ``__import__``, eval/exec/compile, getattr/setattr/delattr,
#   vars/globals/locals, importlib, builtins, runpy, ctypes, ``sys.modules``, a star or
#   relative import, a guarded module used as a value, ``symlink_to``/``hardlink_to``, an
#   import in a class body or into a name declared global or nonlocal (the scan resolves an
#   import only in its own scope and the functions nested in it), any attribute whose name
#   starts with ``__`` (``f.__globals__``, ``print.__self__``, ``object.__subclasses__()``)
#   or names a frame or traceback link (FRAME_ATTRIBUTES: ``tb_frame``, ``f_globals``...), and
#   a chain from a module, kicad_mcp or a sibling script that leaves it through a module name
#   (``pathlib.os``) or, in a stdlib module, a private name (``collections._sys``). These are
#   names and syntax the scan reads; it is a check against accidents and plain idioms, not a
#   sandbox (see the blind spots);
# - raw writes (open for writing, write_text, write_bytes, os.write, tempfile; here also
#   ``X.open(mode)`` with a mode that is not a literal), keyed by the file they name: only the
#   JSON/CSV records listed; a target naming a ``.kicad_`` file, or a call whose text names a
#   serialiser, fails even when listed;
# - high-level saves: ``<model>.save`` / ``.write`` / ``save_project`` on any object, not made
#   through kicad_mcp.write_layer: only the listed rung-1 schematic;
# - commits and copies (os.replace/rename, Path.replace/rename, shutil copy/copytree/move, a
#   ``--save-board`` string), keyed by destination;
# - launches (subprocess.*, os.system/popen/startfile/spawn*/exec*/posix_spawn*, asyncio
#   subprocesses, a call named SaveBoard/Save/SaveProject/SaveAs, an "upgrade" or
#   "--save-board" string), keyed by the program's argv with every name in it replaced by its
#   one value, plus ``cwd=`` with its value expanded the same way. Other keywords:
#   capture_output, text, timeout and check are reviewed (they change capture and waiting, not
#   what runs); any other (executable, shell, env, stdin...), ``**keywords`` or a positional
#   argument after the argv leaves the launch without a key. A name bound more than once (a parameter rebound, ``+=``, a loop other than over
#   a literal of constants), declared global or nonlocal, or, for the argv list and any list
#   spread into it, used anywhere but the launch, a reader (str, len, enumerate...), an operand
#   or a comparison (``cmd[1] = ...``, ``cmd.append``, ``c2 = cmd``, ``helper(cmd)``) leaves the
#   launch without a key, so it fails.
# Blind spots, which the scan cannot resolve statically:
# - what a listed launch writes (KRT's beautify_labels.py writes
#   beaut_out/pic_programmer.kicad_pcb, reviewed below) and what kicad_mcp code called from
#   evals/ writes (tests/test_ratchets.py covers src/);
# - the payload of a listed record write: ``out.write_text(x)`` with x built from a serialiser
#   one statement earlier passes, as long as the target and the count do not change;
# - a method, other than save/write/save_project, of an object that a listed reference or
#   kicad_mcp returned (``Schematic.load(...)`` gives a model; only its save and write are seen);
# - a value inside the argv changed through a method of its own when it is not a list (a str
#   or Path has none that changes it), and an argv element computed by a call whose result
#   differs between runs (keyed by the call's text);
# - a value the caller passes in: a parameter in the argv or the cwd is keyed by its name
#   (``str(krt_dir)``), not by what a caller hands it (FOLLOW-UP, delta review 3 L2);
# - files changed through methods of a safe module's objects other than the raw writes and
#   links above: pathlib's ``touch``, ``unlink``, ``chmod``, ``mkdir``, ``rmdir`` (FOLLOW-UP,
#   L1), and the target of a listed ``shutil.rmtree`` (counted, not keyed by what it removes:
#   FOLLOW-UP, L4);
# - which file a sibling import loads: ``sys.path.insert`` (sys is a safe module) before
#   ``import stage_input`` would load another file under that name (FOLLOW-UP, L3); the same
#   for import hooks through ``sys.meta_path``/``sys.path_hooks`` and tracing through
#   ``sys.settrace``;
# - deliberate obfuscation from roots that are not modules beyond the names refused above: an
#   attribute reached through a string (``"{0.x}".format(obj)``), a library entry that hands
#   back such objects (``operator.attrgetter``, ``gc``, ``inspect`` would each need a
#   reviewed entry, whose review is the check), or a frame or code attribute not in
#   FRAME_ATTRIBUTES. A static scan catches accidents; it is not a sandbox;
# - code outside a .py file.

EVALS = ROOT / "evals"
EVALS_RAW_WRITES = {  # (file under evals/, function, kind, target): (count, what it writes)
    ("rung1/build_rung1.py", "main", "open", "HERE / 'bom.csv'"): (1, "the rung-1 BOM (CSV)"),
    ("rung1/build_rung1.py", "main", "write_text", "HERE / 'erc_report.json'"): (1, "kicad-cli ERC record (JSON)"),
    ("rung1/build_rung1.py", "main", "write_text", "HERE / 'nets.json'"): (1, "net list record (JSON)"),
    ("rung1/build_rung1.py", "main", "write_text", "HERE / 'results_steps.json'"): (1, "tool step results (JSON)"),
    ("rung1/build_rung1.py", "main", "write_text", "HERE / 'selection_log.json'"): (1, "part selection log (JSON)"),
    ("rung1/build_rung1.py", "main", "write_text", "HERE / 'spice_report.json'"): (1, "ngspice record (JSON)"),
    ("rung1/build_rung1_layout.py", "<module>", "write_text", "RECORD"): (1, "layout_run_record.json (JSON)"),
    ("rung2/measure_input_floor.py", "main", "write_text", "out / 'input_fail_floor.json'"): (1, "floor declaration (JSON)"),
    ("rung2/measure_input_floor.py", "main", "write_text", "out / 'input_floor_result.json'"): (1, "floor record (JSON)"),
    ("rung2/record_first_batch.py", "<module>", "write_text", "out"): (1, "first_batch_record.json (JSON)"),
}
EVALS_HIGH_LEVEL_SAVES = {  # (file, function, kind, call text): (count, why it is still here)
    ("rung1/build_rung1.py", "main", "write", "s.write(SCH)"): (1, (
        "kicad_tools Schematic.write of the rung-1 schematic after setting LCSC/MPN through the "
        "high-level model (build ticket 09, before the write layer); not re-run since. Move it "
        "to the layer's typed setters and save in its own ticket; until then it may not grow.")),
}
EVALS_COPIES = {  # (file, function, kind, destination): (count, what it copies)
    ("rung1/build_rung1.py", "main", "shutil.copy", "stage"): (2, "KiCad's and the asset's .kicad_sym files, unchanged"),
    ("rung1/build_rung1.py", "main", "shutil.copytree", "libdir / 'Rung1_USBC'"): (1, "the project's own part assets, unchanged"),
    ("rung1/build_rung1_layout.py", "flow", "shutil.copytree", "RUN / 'src'"): (1, "the rung-1 project, unchanged"),
    ("rung1/build_rung1_layout.py", "flow", "shutil.copytree", "placed_dir"): (1, "a step's board directory, unchanged"),
    ("rung2/measure_input_floor.py", "strip_tracks", "shutil.copyfile", "dst_pcb"): (1, "KiCad's demo board before the layer strips it"),
    ("rung2/measure_input_floor.py", "measure", "shutil.copytree", "stripped_dir"): (1, "KiCad's demo project, unchanged"),
    ("rung2/measure_input_floor.py", "measure", "shutil.copytree", "beaut_in"): (1, "the prepared board, unchanged"),
    ("rung2/measure_input_floor.py", "measure", "shutil.copytree", "beaut_out"): (1, "the prepared board, unchanged"),
    ("rung2/stage_input.py", "stage", "shutil.copytree", "work_dir"): (1, "KiCad's demo project before the layer strips it"),
}
EVALS_LAUNCHES = {  # (file, function, kind, argv with its names expanded): (count, what the program writes)
    ("rung1/build_rung1.py", "main", "subprocess.run",
     "[str(kicad_cli.find_kicad_cli()), 'sch', 'export', 'netlist', '--format', 'kicadsexpr', '-o', "
     "str(Path(__file__).resolve().parent / 'spice' / 'rung1.net'), "
     "str(Path(__file__).resolve().parent / 'project' / f\"{'rung1_usbc_attiny1616'}.kicad_sch\")]"):
        (1, "kicad-cli sch export netlist: a netlist file, no KiCad design file"),
    ("rung1/build_rung1_layout.py", "measure", "subprocess.run",
     "[sys.executable, '-m', 'kicad_mcp.autonomy_run', '--rung', '1', '--backend', "
     "backend in ('krt', 'freerouting'), '--board', board, '--parity', 'required']"):
        (1, "python -m kicad_mcp.autonomy_run: writes through kicad_mcp (src ratchets)"),
    ("rung2/measure_input_floor.py", "run_beautifier", "subprocess.run",
     "[str(python), str(krt_dir / 'py_placer' / 'beautify_labels.py'), str(src_pcb), str(out_pcb), "
     "'--report-json', str(report)] with cwd=str(krt_dir)"): (1, (
        "KRT's beautify_labels.py writes beaut_out/pic_programmer.kicad_pcb with KRT's own writer: "
        "the third-party tool whose effect on the floor is measured; its output is read, never pinned")),
}
_COPIED = "a copy listed in EVALS_COPIES"
_CLEARED = "removes a scratch directory before a fresh copy; writes nothing"
_LAUNCHED = "the launch listed in EVALS_LAUNCHES"
EVALS_REFERENCES = {  # (file, function, resolved dotted name): (count, why it writes no KiCad file)
    ("rung1/build_usbc_asset.py", "<module>", "kicad_tools.sexp.SExp"): (11, ("builds an in-memory node tree (publish-prep ticket 02a); the file is written only by write_layer.save, with its parse check and the builder's read-back verify")),
    ("rung1/build_usbc_asset.py", "S", "kicad_tools.sexp.SExp.list"): (1, ("builds an in-memory node tree (publish-prep ticket 02a); the file is written only by write_layer.save, with its parse check and the builder's read-back verify")),
    ("rung1/build_usbc_asset.py", "node", "kicad_tools.sexp.SExp.list"): (1, ("builds an in-memory node tree (publish-prep ticket 02a); the file is written only by write_layer.save, with its parse check and the builder's read-back verify")),
    ("rung1/build_rung1.py", "main", "kicad_tools.schematic.models.schematic.Schematic.load"): (1, (
        "loads the rung-1 schematic for the one high-level save listed in EVALS_HIGH_LEVEL_SAVES")),
    ("rung1/build_rung1.py", "main", "kicad_tools.schema.Schematic.load"): (1, "reads the schematic for the BOM"),
    ("rung1/build_rung1.py", "main", "os.environ"): (2, "sets KICAD_SYMBOL_DIR and NGSPICE_PATH for the tools"),
    ("rung1/build_rung1.py", "main", "os.environ.get"): (2, "reads NGSPICE_PATH"),
    ("rung1/build_rung1.py", "main", "shutil.copy"): (2, _COPIED),
    ("rung1/build_rung1.py", "main", "shutil.copytree"): (1, _COPIED),
    ("rung1/build_rung1.py", "main", "shutil.rmtree"): (1, _CLEARED),
    ("rung1/build_rung1.py", "main", "subprocess.run"): (1, _LAUNCHED),
    ("rung1/build_rung1.py", "main", "tempfile.gettempdir"): (1, "names the temp directory; writes nothing"),
    ("rung1/build_rung1_layout.py", "<module>", "asyncio.run"): (1, (
        "runs the in-process MCP flow; its tools write through kicad_mcp (src ratchets)")),
    ("rung1/build_rung1.py", "tools_from", "mcp.server.mcpserver.MCPServer"): (1, (
        "the in-process MCP server the tools register on; launches nothing, its tools write through kicad_mcp")),
    ("rung1/build_rung1_layout.py", "flow", "mcp.client.client.Client"): (1, (
        "an MCP client on the in-process server object; launches nothing, the tools write through kicad_mcp")),
    ("rung1/build_rung1_layout.py", "flow", "shutil.copytree"): (2, _COPIED),
    ("rung1/build_rung1_layout.py", "flow", "shutil.rmtree"): (1, _CLEARED),
    ("rung1/build_rung1_layout.py", "measure", "subprocess.run"): (1, _LAUNCHED),
    ("rung2/measure_input_floor.py", "_raw_counts", "kicad_tools.sexp.parse_file"): (1, (
        "reads a board to count its top-level nodes")),
    ("rung2/measure_input_floor.py", "measure", "shutil.copytree"): (3, _COPIED),
    ("rung2/measure_input_floor.py", "measure", "shutil.rmtree"): (1, _CLEARED),
    ("rung2/measure_input_floor.py", "run_beautifier", "subprocess.TimeoutExpired"): (1, (
        "the exception caught around the listed launch")),
    ("rung2/measure_input_floor.py", "run_beautifier", "subprocess.run"): (1, _LAUNCHED),
    ("rung2/measure_input_floor.py", "strip_tracks", "shutil.copyfile"): (1, _COPIED),
    ("rung2/stage_input.py", "stage", "shutil.copytree"): (1, _COPIED),
    ("rung2/stage_input.py", "stage", "shutil.rmtree"): (1, _CLEARED),
}
EVALS_LAUNCH_CALLS = frozenset({
    "subprocess.run", "subprocess.Popen", "subprocess.call", "subprocess.check_call",
    "subprocess.check_output", "subprocess.getoutput", "subprocess.getstatusoutput",
    "os.system", "os.popen", "os.startfile", "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
})
KICAD_OWN_WRITERS = frozenset({"SaveBoard", "Save", "SaveProject", "SaveAs"})  # pcbnew and friends, by name
# The only modules a script under evals/ may use without a reviewed entry in EVALS_REFERENCES,
# besides kicad_mcp (src ratchets) and the scripts beside it (scanned here): stdlib modules
# that start no program and write no file except through calls the raw-write and commit
# scans list. Every other module (subprocess, os, shutil, tempfile, asyncio, kicad_tools,
# pcbnew, mcp, any third-party library) is guarded: each name used from it is a reviewed
# entry (fix round 3: an allowlist, after two rounds of names a denylist did not hold).
SAFE_MODULES = frozenset({"__future__", "collections", "csv", "hashlib", "json", "pathlib", "re", "sys", "time"})
# Never allowlisted: they load code or reach a module by a name the scan cannot read.
FORBIDDEN_REFERENCES = ("importlib", "builtins", "runpy", "ctypes", "sys.modules", "_winapi", "_posixsubprocess")
FORBIDDEN_NAMES = frozenset({"__import__", "eval", "exec", "compile", "getattr", "setattr", "delattr", "vars",
                             "globals", "locals", "__builtins__"})
FORBIDDEN_METHODS = frozenset({"symlink_to", "hardlink_to", "link_to"})  # a link swaps a file like a copy
# Attributes that reach a namespace from an object that is not a module (delta review 3): every
# ``__``-prefixed attribute (``f.__globals__``, ``print.__self__``, ``object.__subclasses__``)
# and the frame and traceback links (``tb_frame.f_globals``), refused wherever they appear.
FRAME_ATTRIBUTES = frozenset({"tb_frame", "tb_next", "gi_frame", "cr_frame", "ag_frame", "f_globals", "f_locals",
                              "f_builtins", "f_back", "f_code"})
# A launch's keywords (delta review 3): the reviewed ones change how its output is captured and
# how long it may run, not what runs; cwd moves where the argv's relative paths land, so its
# value joins the key. Any other keyword (executable, shell, env, stdin, preexec_fn...) can
# change the program, its environment or its input, and leaves the launch without a key.
LAUNCH_KEYWORDS = frozenset({"capture_output", "text", "timeout", "check"})
LAUNCH_KEYED_KEYWORDS = frozenset({"cwd"})
# Calls that read a launch's argv without changing it.
ARGV_READERS = frozenset({"str", "len", "enumerate", "list", "tuple", "repr", "sorted"})
SERIALISER_PAYLOAD = re.compile(r"sexp|serializ|serialis|to_string|SaveBoard", re.I)


class _Unresolvable(Exception):
    pass


def _imports_bound(node) -> dict[str, str]:
    """Local name -> dotted target for the absolute imports directly in ``node``'s scope (not
    in nested functions or classes): ``import a.b`` binds ``a``, ``import a.b as x`` binds
    ``x -> a.b``, ``from a import b as x`` binds ``x -> a.b``."""
    import ast

    out: dict[str, str] = {}
    todo = list(getattr(node, "body", []))
    while todo:
        child = todo.pop(0)
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            continue
        if isinstance(child, ast.Import):
            for alias in child.names:
                out[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(child, ast.ImportFrom) and not child.level and child.module:
            for alias in child.names:
                if alias.name != "*":
                    out[alias.asname or alias.name] = f"{child.module}.{alias.name}"
        todo.extend(ast.iter_child_nodes(child))
    return out


_MODULE_BINDINGS: dict[str, dict[str, str]] = {}


def _module_bindings(key: str, tree, package: list[str]) -> dict[str, str]:
    """A module's attributes that are other modules' names: its module-level imports (relative
    ones resolved against ``package``) and ``name = <dotted name>`` assignments."""
    import ast

    import test_ratchets as ratchets

    if key is not None and key in _MODULE_BINDINGS:
        return _MODULE_BINDINGS[key]
    out: dict[str, str] = {}
    todo = list(tree.body)
    while todo:
        child = todo.pop(0)
        if isinstance(child, ast.Import):
            for alias in child.names:
                out[alias.asname or alias.name.split(".")[0]] = alias.name if alias.asname else alias.name.split(".")[0]
        elif isinstance(child, ast.ImportFrom):
            base = package[:len(package) - (child.level - 1)] if child.level else []
            module = ".".join([*base, *([child.module] if child.module else [])])
            for alias in child.names:
                if alias.name != "*":
                    out[alias.asname or alias.name] = f"{module}.{alias.name}"
        elif isinstance(child, ast.Assign) and len(child.targets) == 1 and isinstance(child.targets[0], ast.Name):
            dotted = ratchets._dotted(child.value)
            head, dot, rest = dotted.partition(".")
            if head in out:
                out[child.targets[0].id] = out[head] + dot + rest
        elif isinstance(child, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            todo.extend(n for n in ast.iter_child_nodes(child) if isinstance(n, (ast.stmt, ast.excepthandler)))
        elif isinstance(child, ast.excepthandler):
            todo.extend(child.body)
    if key is not None:
        _MODULE_BINDINGS[key] = out
    return out


def _bindings_of(parts: list[str], file: str, modules: dict) -> dict[str, str] | None:
    """The bindings of module ``parts`` when its source is known: a ``kicad_mcp`` module, or a
    script beside ``file`` under evals/ imported by its bare name. None otherwise."""
    import ast

    import test_ratchets as ratchets

    if parts[0] == "kicad_mcp":
        path = ratchets.SRC.joinpath(*parts[1:])
        if (path / "__init__.py").is_file():
            path, package = path / "__init__.py", parts
        elif path.with_name(path.name + ".py").is_file() and len(parts) > 1:
            path, package = path.with_name(path.name + ".py"), parts[:-1]
        else:
            return None
        return _module_bindings(str(path), ast.parse(path.read_text(encoding="utf-8")), package)
    if len(parts) == 1:
        sibling = f"{file.rsplit('/', 1)[0]}/{parts[0]}.py"
        if sibling in modules:
            return _module_bindings(None, modules[sibling], [])  # not cached: mutants reparse it
    return None


def _follow(dotted: str, file: str, modules: dict) -> str:
    """``dotted`` with every attribute that a known module re-exports replaced by its source:
    ``kicad_mcp.write_layer.os.replace`` -> ``os.replace``."""
    for _ in range(10):
        parts = dotted.split(".")
        for i in range(len(parts) - 1, 0, -1):
            bindings = _bindings_of(parts[:i], file, modules)
            if bindings is not None:
                break
        else:
            return dotted
        if parts[i] not in bindings:
            return dotted
        dotted = ".".join([bindings[parts[i]], *parts[i + 1:]])
    return dotted


def _evals_scanners():
    import ast
    import copy

    import test_ratchets as ratchets

    class OpenAnyMode(ratchets.RawWriteScanner):
        """Here ``X.open(mode)`` with a non-literal mode counts as a write too (src/ lets it
        pass so ``pdfplumber.open(path)`` stays out; evals/ has no such reader)."""

        def _kind(self, node):
            kind = super()._kind(node)
            func = node.func
            if kind is None and isinstance(func, ast.Attribute) and func.attr == "open" \
                    and node.args and ratchets._literal_str(node.args[0]) is None:
                return "open"
            return kind

    class Resolving(ratchets._Scanner):
        """Resolves a name through the imports of its scope and the enclosing ones, then
        through the re-exports of kicad_mcp modules and sibling scripts."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.aliases: list[dict[str, str]] = []
            self.nodes: list[ast.AST] = []

        def enter(self, node):
            self.aliases.append({**(self.aliases[-1] if self.aliases else {}), **_imports_bound(node)})
            self.nodes.append(node)

        def leave(self):
            self.aliases.pop()
            self.nodes.pop()

        def resolve(self, dotted: str) -> str | None:
            head, dot, rest = dotted.partition(".")
            if not self.aliases or head not in self.aliases[-1]:
                return None
            return _follow(self.aliases[-1][head] + dot + rest, self.file, self.modules)

        def canon(self, node):
            dotted = ratchets._dotted(node)
            return (self.resolve(dotted) if dotted else None) or dotted

    class ReferenceScanner(Resolving):
        """Every reference into a guarded module, keyed by its resolved dotted name; the
        forbidden ones carry a kind starting with "forbidden"."""

        def visit_Import(self, node):
            self._outlives_its_scope(node)

        def visit_ImportFrom(self, node):
            if node.level:
                self.hit(node, "forbidden: a relative import")
            if any(alias.name == "*" for alias in node.names):
                self.hit(node, "forbidden: a star import")
            self._outlives_its_scope(node)

        def _outlives_its_scope(self, node):
            """An import is resolved only in the scope that binds it and the functions nested
            in it; a class body (read later as ``C.name`` or ``self.name``) or a name declared
            ``global``/``nonlocal`` (the lazy-import idiom) carries it where the scan does not
            follow, so both are refused (delta review 3)."""
            scope = self.nodes[-1]
            if isinstance(scope, ast.ClassDef):
                self.hit(node, "forbidden: an import in a class body")
                return
            bound = {alias.asname or alias.name.split(".")[0] for alias in node.names}
            declared = {name for kind, name in _declarations(scope)}
            for name in sorted(bound & declared):
                self.hit(node, f"forbidden: an import into the global or nonlocal {name}")

        def visit_Name(self, node):
            self._reference(node, node.id)

        def visit_Attribute(self, node):
            dotted = ratchets._dotted(node)
            parts = dotted.split(".")[1:] if dotted else [node.attr]
            for part in parts:
                if part in FORBIDDEN_METHODS or part in FRAME_ATTRIBUTES or part.startswith("__"):
                    self.hit(node, f"forbidden: .{part}")
            if dotted:
                self._reference(node, dotted)
            else:
                self.generic_visit(node)

        def _reference(self, node, dotted):
            resolved = self.resolve(dotted)
            if resolved is None:
                if dotted.split(".")[0] in FORBIDDEN_NAMES:
                    self.hit(node, f"forbidden: {dotted.split('.')[0]}")
                return
            parts = resolved.split(".")
            root = parts[0]
            if any(resolved == p or resolved.startswith(p + ".") for p in FORBIDDEN_REFERENCES):
                self.hit(node, f"forbidden: {resolved}")
            elif root in SAFE_MODULES or root == "kicad_mcp" or _bindings_of([root], self.file, self.modules) is not None:
                # Free, unless the chain leaves the module: through a dunder (also refused on any
                # chain by visit_Attribute), a module name (``pathlib.os``), or, in a stdlib module whose imports are not
                # followed, a private name (``collections._sys``). kicad_mcp's and the sibling
                # scripts' own imports were followed above, so their private names stay free.
                import sys

                private = "__" if root == "kicad_mcp" or root not in SAFE_MODULES else "_"
                hidden = [p for p in parts[1:] if p != root and (p.startswith(private) or p in sys.stdlib_module_names)]
                if hidden:
                    self.hit(node, f"forbidden: {resolved} leaves {root} through {hidden[0]}")
            elif len(parts) == 1:
                self.hit(node, f"forbidden: module {root} used as a value")
            else:
                self.hit(node, resolved)

    class LaunchScanner(Resolving):
        """Calls that start another program, pcbnew's own writers by name, and a "kicad-cli
        upgrade" or "--save-board" argument anywhere in code. Each site carries the source
        text of the program's argv with every name in it replaced by the one value it is
        bound to; a name bound more than once, changed in place, or passed anywhere but a
        reader before the launch leaves the launch without a key, so it fails."""

        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.argv: list[str] = []

        def visit_Call(self, node):
            dotted = self.canon(node.func)
            name = dotted.rsplit(".", 1)[-1] if dotted else ratchets._call_name(node.func)
            if dotted in EVALS_LAUNCH_CALLS or dotted.startswith(("os.spawn", "os.exec", "os.posix_spawn")):
                self.launch(node, dotted, self._argv(node))
            elif name in KICAD_OWN_WRITERS or ratchets._call_name(node.func) in KICAD_OWN_WRITERS:
                self.launch(node, f"kicad writer {name}", ast.unparse(node))
            self.generic_visit(node)

        def visit_Expr(self, node):
            if isinstance(node.value, ast.Constant):
                return  # a docstring passes nothing to a process
            self.generic_visit(node)

        def visit_Constant(self, node):
            if isinstance(node.value, str) and (node.value == "upgrade" or "--save-board" in node.value):
                self.launch(node, f"argument {node.value!r}", node.value)

        def launch(self, node, kind, argv):
            self.hit(node, kind)
            self.argv.append(argv)

        def _argv(self, call) -> str:
            first = call.args[0] if call.args else next((k.value for k in call.keywords if k.arg == "args"), None)
            if first is None:
                return ast.unparse(call)
            try:
                if isinstance(first, ast.Name):
                    kind, value = self._binding(first.id, call, first, strict=True)
                    if kind != "assign":
                        raise _Unresolvable(f"{first.id} is not bound to a value here")
                    first = value
                key = ast.unparse(self._expand(first, call, 0))
                if call.args[1:]:
                    raise _Unresolvable("a positional argument after the argv (bufsize, executable...)")
                for keyword in call.keywords:
                    if keyword.arg is None:
                        raise _Unresolvable("**keywords the scan cannot read")
                    if keyword.arg in LAUNCH_KEYED_KEYWORDS:
                        key += f" with {keyword.arg}={ast.unparse(self._expand(keyword.value, call, 0))}"
                    elif keyword.arg not in LAUNCH_KEYWORDS and not (keyword.arg == "args" and not call.args):
                        raise _Unresolvable(f"keyword {keyword.arg}= is not a reviewed launch keyword")
                return key
            except _Unresolvable as exc:
                return f"<no key: {exc}>"

        def _expand(self, expr, call, depth):
            if depth > 4:
                raise _Unresolvable("names nested too deep")
            scanner = self

            class Substitute(ast.NodeTransformer):
                def visit_Starred(self, node):
                    if isinstance(node.value, ast.Name):
                        kind, value = scanner._binding(node.value.id, call, node.value, strict=True)
                        node.value = scanner._expand(value, call, depth + 1) if kind == "assign" else node.value
                        return node
                    return self.generic_visit(node)

                def visit_Name(self, node):
                    kind, value = scanner._binding(node.id, call, node, strict=False)
                    if kind == "assign":
                        mutable = isinstance(value, (ast.List, ast.Set, ast.Dict, ast.ListComp, ast.SetComp, ast.DictComp))
                        if mutable:
                            scanner._binding(node.id, call, node, strict=True)
                        return scanner._expand(value, call, depth + 1)
                    if kind == "loop":
                        return ast.Name(id=f"{node.id} in {ast.unparse(value)}", ctx=ast.Load())
                    return node

            return Substitute().visit(copy.deepcopy(expr))

        def _binding(self, name, call, use, *, strict):
            """(kind, value) of the one binding of ``name`` in the innermost scope that binds
            it: "assign" with its value, "loop" over a literal of constants with the literal,
            or "free" (a parameter, import, def or builtin, with no other binding)."""
            for scope in reversed(self.nodes):
                if isinstance(scope, ast.ClassDef):
                    continue  # a class body's names are not visible in its methods
                bindings = _scope_bindings(scope, name)
                if not bindings:
                    continue
                if any(kind == "global" for kind, _ in bindings):
                    raise _Unresolvable(f"{name} is declared global or nonlocal")
                if len(bindings) > 1:
                    raise _Unresolvable(f"{name} is bound {len(bindings)} times")
                kind, value = bindings[0]
                if kind == "other":
                    raise _Unresolvable(f"{name} is bound by a {value}")
                if strict:
                    unsafe = _unsafe_use(scope, name, call)
                    if unsafe:
                        raise _Unresolvable(f"{name} {unsafe}")
                return ("free", None) if kind in ("param", "import", "def") else (kind, value)
            return "free", None

    def _declarations(scope):
        """(``global``|``nonlocal``, name) for the declarations of ``scope`` itself, not of the
        functions and classes nested in it."""
        out, todo = [], list(getattr(scope, "body", []))
        while todo:
            node = todo.pop()
            if isinstance(node, (ast.Global, ast.Nonlocal)):
                out += [(type(node).__name__.lower(), name) for name in node.names]
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                todo.extend(ast.iter_child_nodes(node))
        return out

    def _parents(scope):
        parents = {}
        for parent in ast.walk(scope):
            for child in ast.iter_child_nodes(parent):
                parents[child] = parent
        return parents

    def _scope_nodes(scope):
        """The nodes of a function (nested scopes included: they may rebind through
        ``nonlocal``), or of a module without its functions and classes."""
        if not isinstance(scope, ast.Module):
            return list(ast.walk(scope))
        out, todo = [], list(scope.body)
        while todo:
            node = todo.pop()
            out.append(node)
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                todo.extend(ast.iter_child_nodes(node))
        return out

    def _scope_bindings(scope, name):
        parents = _parents(scope)
        found = []
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
            args = scope.args
            params = [*args.posonlyargs, *args.args, *args.kwonlyargs, *filter(None, (args.vararg, args.kwarg))]
            found += [("param", None) for a in params if a.arg == name]
        nodes = _scope_nodes(scope)
        for node in (ast.walk(scope) if isinstance(scope, ast.Module) else nodes):
            if isinstance(node, (ast.Global, ast.Nonlocal)) and name in node.names:
                found.append(("global", None))
        for node in nodes:
            if node is scope:
                continue
            if isinstance(node, ast.Name) and node.id == name and not isinstance(node.ctx, ast.Load):
                parent = parents.get(node)
                if isinstance(parent, ast.Assign) and parent.targets == [node]:
                    found.append(("assign", parent.value))
                elif isinstance(parent, ast.AnnAssign) and parent.target is node and parent.value is not None:
                    found.append(("assign", parent.value))
                elif isinstance(parent, ast.For) and parent.target is node and isinstance(parent.iter, (ast.Tuple, ast.List)) \
                        and all(isinstance(e, ast.Constant) for e in parent.iter.elts):
                    found.append(("loop", parent.iter))
                else:
                    found.append(("other", type(parent).__name__))
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if (alias.asname or alias.name.split(".")[0]) == name:
                        found.append(("import", None))
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == name:
                found.append(("def", None))
            elif isinstance(node, ast.ExceptHandler) and node.name == name:
                found.append(("other", "except clause"))
            elif isinstance(node, ast.arg) and node.arg == name and parents.get(parents.get(node)) is not scope:
                found.append(("other", "nested parameter"))
        return found

    def _unsafe_use(scope, name, call):
        """How ``name`` may change before the launch reads it: any use but the launch's own
        argv, an argument of a reader (``str``, ``len``, ``enumerate``...), an operand, a
        comparison or an f-string."""
        argv = set(map(id, ast.walk(call.args[0]))) if call.args else set()
        module = scope if isinstance(scope, ast.Module) else None
        tree = module or scope
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load)):
                continue
            parent = parents.get(node)
            if call.args and node is call.args[0]:
                continue
            if id(node) in argv and isinstance(parent, (ast.List, ast.Tuple)):
                continue  # an element of the launch's own argv
            if isinstance(parent, ast.Call) and node in parent.args and isinstance(parent.func, ast.Name) \
                    and parent.func.id in ARGV_READERS:
                continue
            if isinstance(parent, (ast.BinOp, ast.Compare, ast.FormattedValue)):
                continue
            return f"is used as {type(parent).__name__} on line {node.lineno}"
        return None

    return ratchets, [("raw", OpenAnyMode), ("save", ratchets.SaveScanner), ("ref", ReferenceScanner),
                      ("copy", ratchets.CommitScanner), ("launch", LaunchScanner)]


def _evals_sites(sources: dict[str, str]) -> dict[str, list[tuple]]:
    """``{"raw", "save", "ref", "forbidden", "copy", "launch"}`` -> one ``(key, call text)``
    per site found in the given ``{relative path: source}``; duplicates are kept, so sites can
    be counted."""
    import ast

    ratchets, scanners = _evals_scanners()
    layer = "write_layer.py"
    modules = {layer: ast.parse((ratchets.SRC / layer).read_text(encoding="utf-8"))}
    modules |= {f"evals/{rel}": ast.parse(text) for rel, text in sources.items()}
    found: dict[str, list] = {key: [] for key, _ in scanners} | {"forbidden": []}
    for key, scanner in scanners:
        for rel, tree in modules.items():
            if rel == layer:
                continue
            visitor = scanner(rel, modules)
            visitor.visit(tree)
            for i, site in enumerate(visitor.sites):
                file = rel.removeprefix("evals/")
                if key == "raw":
                    k = (file, site.function, site.kind, _call_arg(site.code, receiver=True))
                elif key == "copy":
                    k = (file, site.function, site.kind, _call_arg(site.code, index=1))
                elif key == "launch":
                    k = (file, site.function, site.kind, visitor.argv[i])
                elif key == "ref":
                    k = (file, site.function, site.kind)
                else:
                    k = (file, site.function, site.kind, site.code)
                found["forbidden" if site.kind.startswith("forbidden") else key].append((k, site.code))
    return found


def _call_arg(code: str, *, receiver: bool = False, index: int = 0) -> str:
    """The file a call names: for a raw write ``X`` of ``X.write_text(...)``/``X.open(...)``
    or the first argument of ``open(X, ...)``; for a copy its destination (argument
    ``index``). The whole call text when neither applies."""
    import ast

    call = ast.parse(code, mode="eval").body
    if not isinstance(call, ast.Call):
        return code
    if receiver and isinstance(call.func, ast.Attribute) and call.func.attr in ("write_text", "write_bytes", "open"):
        return ast.unparse(call.func.value)
    if len(call.args) > index:
        return ast.unparse(call.args[index])
    return code


def _evals_sources() -> dict[str, str]:
    return {p.relative_to(EVALS).as_posix(): p.read_text(encoding="utf-8") for p in sorted(EVALS.rglob("*.py"))}


def _evals_problems(found: dict[str, list[tuple]]) -> list[str]:
    from collections import Counter

    problems = []
    for key, frozen, fix in (
        ("raw", EVALS_RAW_WRITES, "write KiCad files with kicad_mcp.write_layer; a new JSON/CSV record "
                                  "needs a reviewed entry in EVALS_RAW_WRITES"),
        ("save", EVALS_HIGH_LEVEL_SAVES, "save through kicad_mcp.write_layer (save, save_pcb, save_project)"),
        ("ref", EVALS_REFERENCES, "a name in a guarded module can launch, write, move or load; reach it "
                                  "through kicad_mcp, or add a reviewed entry to EVALS_REFERENCES saying "
                                  "why it writes no KiCad file"),
        ("copy", EVALS_COPIES, "let kicad_mcp.write_layer replace the file; a new copy of files KiCad or "
                               "the layer wrote needs a reviewed entry in EVALS_COPIES"),
        ("launch", EVALS_LAUNCHES, "a launched program can write any file; it needs a reviewed entry "
                                   "in EVALS_LAUNCHES saying what it writes"),
    ):
        counts = Counter(k for k, _ in found[key])
        for site in sorted(set(counts) | set(frozen)):
            have, allowed = counts.get(site, 0), frozen.get(site, (0, ""))[0]
            if have > allowed:
                problems.append(f"new {key} site in evals/{site[0]} {site[1]}: {site[2:]} x{have - allowed} -> {fix}")
            elif have < allowed:
                problems.append(f"fixed {key} site evals/{site[0]} {site[1]}: {site[2:]} -> lower its count "
                                f"to {have} (the list only shrinks)")
    for key, code in found["raw"]:
        if ".kicad_" in key[3] or SERIALISER_PAYLOAD.search(code):
            problems.append(f"raw write of KiCad text in evals/{key[0]} {key[1]}: {code} -> use kicad_mcp.write_layer")
    for key, code in found["forbidden"]:
        problems.append(f"{key[2]} in evals/{key[0]} {key[1]}: {code} -> never allowed; the scan cannot "
                        "read what it reaches")
    return problems


def test_the_evals_scan_catches_a_kicad_file_written_outside_the_layer():
    """Protocol 4's staging (build ticket 42) wrote kicad_tools' serialiser text with open()
    and os.replace; a model save and a layer save are told apart."""
    old_staging = (
        "import os\n"
        "def _write(board, pcb):\n"
        "    from kicad_tools.core.sexp_file import serialize_sexp\n"
        "    temp = pcb.with_name('.x.tmp')\n"
        "    with open(temp, 'w', encoding='utf-8', newline='') as fh:\n"
        "        fh.write(serialize_sexp(board._sexp, preserve_source=True))\n"
        "    os.replace(temp, pcb)\n"
        "def _model(board, pcb):\n"
        "    board.save(pcb)\n"
        "def _layer(root, pcb):\n"
        "    from kicad_mcp import write_layer\n"
        "    write_layer.save(root, pcb)\n"
        "    write_layer.save_pcb(root, pcb)\n"
    )
    found = _evals_sites({"rung2/old.py": old_staging})
    assert [(k[1], k[2], k[3]) for k, _ in found["raw"]] == [("_write", "open", "temp")]
    assert [(k[1], k[2]) for k, _ in found["copy"]] == [("_write", "os.replace")]
    assert [(k[1], k[3]) for k, _ in found["save"]] == [("_model", "board.save(pcb)")]


# Review round 1's surviving mutants: each one added to the real stage_input.py (or
# record_first_batch.py) must now fail the evals ratchet.
_STRIP_DEF = "def strip_tracks(pcb: Path) -> dict[str, int]:\n"
_MUTANTS = {
    "pcbnew.SaveBoard": ("rung2/stage_input.py", _STRIP_DEF, "    import pcbnew\n    pcbnew.SaveBoard(str(pcb), None)\n"),
    "kicad-cli upgrade": ("rung2/stage_input.py", _STRIP_DEF,
                          "    import subprocess\n    subprocess.run(['kicad-cli', 'pcb', 'upgrade', '--force', str(pcb)])\n"),
    "open with a variable mode": ("rung2/stage_input.py", _STRIP_DEF, "    mode = 'w'\n    pcb.open(mode)\n"),
    "shutil.copyfile onto the board": ("rung2/stage_input.py", _STRIP_DEF, "    shutil.copyfile(pcb, pcb)\n"),
    "a second serialiser write": ("rung2/record_first_batch.py", None,
                                  "out.write_text(serialize_sexp(board))\n"),
    "a second identical record write": ("rung2/record_first_batch.py", None, "out.write_text('{}')\n"),
}
# Delta review 1: the same writes and launches reached through import idioms, and a launch
# whose program is swapped. Values: (file, anchor, inserted) or (file, ("replace", old), new).
_X = "['python', 'x.py', str(pcb)]"
_MUTANTS |= {
    "import subprocess as sp": ("rung2/stage_input.py", _STRIP_DEF, f"    import subprocess as sp\n    sp.run({_X})\n"),
    "from subprocess import run": ("rung2/stage_input.py", _STRIP_DEF, f"    from subprocess import run\n    run({_X})\n"),
    "from subprocess import Popen": ("rung2/stage_input.py", _STRIP_DEF, f"    from subprocess import Popen\n    Popen({_X})\n"),
    "from subprocess import check_output as co": (
        "rung2/stage_input.py", _STRIP_DEF, f"    from subprocess import check_output as co\n    co({_X})\n"),
    "from asyncio import create_subprocess_exec": (
        "rung2/stage_input.py", _STRIP_DEF, "    from asyncio import create_subprocess_exec\n    create_subprocess_exec('x')\n"),
    "import asyncio as aio": ("rung2/stage_input.py", _STRIP_DEF, "    import asyncio as aio\n    aio.create_subprocess_shell('x')\n"),
    "from os import system": ("rung2/stage_input.py", _STRIP_DEF, "    from os import system\n    system('x')\n"),
    "import os as o": ("rung2/stage_input.py", _STRIP_DEF, "    import os as o\n    o.popen('x')\n"),
    "from pcbnew import SaveBoard": ("rung2/stage_input.py", _STRIP_DEF, "    from pcbnew import SaveBoard\n    SaveBoard(str(pcb), None)\n"),
    "import pcbnew as pn": ("rung2/stage_input.py", _STRIP_DEF, "    import pcbnew as pn\n    pn.SaveBoard(str(pcb), None)\n"),
    "import kicad_tools.core.sexp_file as sf": (
        "rung2/stage_input.py", _STRIP_DEF, "    import kicad_tools.core.sexp_file as sf\n    sf.save_pcb(root, pcb)\n"),
    "from kicad_tools.core import sexp_file": (
        "rung2/stage_input.py", _STRIP_DEF, "    from kicad_tools.core import sexp_file\n    sexp_file.save_pcb(root, pcb)\n"),
    **{f"from kicad_tools.core.sexp_file import {writer}": (
        "rung2/stage_input.py", _STRIP_DEF, f"    from kicad_tools.core.sexp_file import {writer}\n    {writer}(root, pcb)\n")
       for writer in ("save_pcb", "save_schematic", "save_footprint", "save_symbol_lib", "save_design_rules",
                      "atomic_write_text")},
    "a launch of another script": ("rung2/measure_input_floor.py", ("replace", '"beautify_labels.py"'), '"other.py"'),
    "a launch of another program": ("rung1/build_rung1.py", ("replace", '"sch", "export", "netlist"'), '"pcb", "upgrade_x"'),
}
# Delta review 2: names the writer list did not hold, star imports, dynamic imports and an argv
# changed after its one binding. The scan is now an allowlist (fix round 3).
_ARGV_END = '"--report-json", str(report)]\n'
_MUTANTS |= {
    "save_project as an alias": (
        "rung2/stage_input.py", _STRIP_DEF,
        "    from kicad_tools.core.project_file import save_project as sp_\n    sp_(pcb)\n"),
    "write_footprint_placements imported": (
        "rung2/stage_input.py", _STRIP_DEF,
        "    from kicad_tools.placement.writeback import write_footprint_placements\n"
        "    write_footprint_placements(pcb, {})\n"),
    "write_footprint_placements through a module alias": (
        "rung2/stage_input.py", _STRIP_DEF,
        "    import kicad_tools.placement.writeback as wb\n    wb.write_footprint_placements(pcb, {})\n"),
    "write_drc_constraints": (
        "rung2/stage_input.py", _STRIP_DEF,
        "    from kicad_tools.manufacturers.project_generator import write_drc_constraints\n"
        "    write_drc_constraints(pcb)\n"),
    "save_decisions": (
        "rung2/stage_input.py", _STRIP_DEF,
        "    from kicad_tools.explain import rationale\n    rationale.save_decisions([], pcb)\n"),
    **{f"from {module} import *": ("rung2/record_first_batch.py", None, f"from {module} import *\n{call}\n")
       for module, call in (("subprocess", f"run({_X})"), ("os", "system('x')"), ("shutil", "copyfile(out, out)"),
                            ("asyncio", "create_subprocess_shell('x')"), ("pcbnew", "SaveBoard('x.kicad_pcb', None)"),
                            ("kicad_tools.core", "sexp_file.save_pcb(out, out)"),
                            ("kicad_tools.core.sexp_file", "save_pcb(out, out)"),
                            ("importlib", "import_module('subprocess').run(['x'])"))},
    "__import__": ("rung2/record_first_batch.py", None, f"__import__('subprocess').run({_X})\n"),
    "importlib.import_module": (
        "rung2/record_first_batch.py", None, f"import importlib\nimportlib.import_module('subprocess').run({_X})\n"),
    "getattr on a module": ("rung2/record_first_batch.py", None, f"import subprocess\ngetattr(subprocess, 'run')({_X})\n"),
    "a module reached through the layer": (
        "rung2/stage_input.py", _STRIP_DEF, "    from kicad_mcp import write_layer\n    write_layer.os.replace(pcb, pcb)\n"),
    "a name the layer imports": (
        "rung2/stage_input.py", _STRIP_DEF, "    from kicad_mcp.write_layer import os as o_\n    o_.replace(pcb, pcb)\n"),
    "os.link": ("rung2/stage_input.py", _STRIP_DEF, "    import os\n    os.link(pcb, pcb)\n"),
    "sys.modules": ("rung2/record_first_batch.py", None, f"sys.modules['subprocess'].run({_X})\n"),
    "eval": ("rung2/record_first_batch.py", None, "eval('1')\n"),
    "a symlink onto the board": ("rung2/stage_input.py", _STRIP_DEF, "    pcb.symlink_to(pcb)\n"),
    "argv: an item replaced": ("rung2/measure_input_floor.py", ("replace", _ARGV_END), _ARGV_END + '    cmd[1] = "other.py"\n'),
    "argv: append": ("rung2/measure_input_floor.py", ("replace", _ARGV_END), _ARGV_END + '    cmd.append("--write")\n'),
    "argv: +=": ("rung2/measure_input_floor.py", ("replace", _ARGV_END), _ARGV_END + '    cmd += ["--write"]\n'),
    "argv: insert": ("rung2/measure_input_floor.py", ("replace", _ARGV_END), _ARGV_END + '    cmd.insert(1, "other.py")\n'),
    "argv: extend": ("rung2/measure_input_floor.py", ("replace", _ARGV_END), _ARGV_END + '    cmd.extend(["--write"])\n'),
    "argv: rebound to another program": (
        "rung2/measure_input_floor.py", ("replace", _ARGV_END), _ARGV_END + '    cmd = ["other.exe"]\n'),
    "argv: a parameter rebound before the binding": (
        "rung2/measure_input_floor.py", ("replace", "    cmd = [str(python),"), '    python = "evil"\n    cmd = [str(python),'),
    "argv: passed to a helper first": (
        "rung2/measure_input_floor.py", ("replace", _ARGV_END), _ARGV_END + "    _step(cmd, {})\n"),
    "argv: aliased, then changed": (
        "rung2/measure_input_floor.py", ("replace", _ARGV_END), _ARGV_END + "    c2 = cmd\n    c2.append('x')\n"),
    "a second reference to an allowlisted launch": (
        "rung2/measure_input_floor.py", ("replace", _ARGV_END), _ARGV_END + "    r = subprocess.run\n    r(['x'])\n"),
    # Fix round 3's own probes: modules outside the guarded names, and guarded ones reached
    # through a module the scan lets pass.
    "a module reached through a safe stdlib module": (
        "rung2/record_first_batch.py", None, "import pathlib\npathlib.os.system('x')\n"),
    "a private attribute of a safe stdlib module": (
        "rung2/record_first_batch.py", None, f"import collections\ncollections._sys.modules['subprocess'].run({_X})\n"),
    "a module reached through the layer's attribute": (
        "rung2/stage_input.py", _STRIP_DEF, "    from kicad_mcp import write_layer\n    write_layer.load.__globals__\n"),
    "another KiCad library": (
        "rung2/stage_input.py", _STRIP_DEF, "    from kiutils.board import Board\n    Board.from_file(str(pcb)).to_file()\n"),
    "the MCP stdio client": (
        "rung2/stage_input.py", _STRIP_DEF, "    from mcp.client.stdio import stdio_client\n    stdio_client(None)\n"),
    "a kicad_tools name reached through the layer": (
        "rung2/stage_input.py", _STRIP_DEF, "    from kicad_mcp import write_layer\n    write_layer.parse_string('(x)')\n"),
    "a module rebound to a local name": (
        "rung2/record_first_batch.py", None, "import shutil\nhelper = shutil\nhelper.copyfile(out, out)\n"),
}
# Delta review 3: an import whose name outlives the scope the scan tracks (a class body, a
# global or nonlocal), and a listed launch changed by a keyword its argv key did not hold.
_CWD = "cwd=str(krt_dir))"
_MUTANTS |= {
    "a class-body import used through the class": (
        "rung2/record_first_batch.py", None, "class _C:\n    import subprocess as sp\n_C.sp.run(['x'])\n"),
    "a class-body import used through self": (
        "rung2/record_first_batch.py", None,
        "class _C:\n    import subprocess as sp\n    def go(self):\n        self.sp.run(['x'])\n_C().go()\n"),
    "a class-body from-import": (
        "rung2/record_first_batch.py", None, "class _C:\n    from shutil import copyfile\n_C.copyfile(out, out)\n"),
    "a class-body import of a kicad_tools writer": (
        "rung2/record_first_batch.py", None,
        "class _C:\n    import kicad_tools.core.sexp_file as sf\n_C.sf.save_pcb(out, out)\n"),
    "a lazy import into a global": (
        "rung2/record_first_batch.py", None,
        "def _g():\n    global sp\n    import subprocess as sp\n_g()\nsp.run(['x'])\n"),
    "a lazy import into a nonlocal": (
        "rung2/record_first_batch.py", None,
        "def _outer():\n    sp = None\n    def _g():\n        nonlocal sp\n        from subprocess import run as sp\n"
        "    _g()\n    sp(['x'])\n"),
    "launch keyword: executable": (
        "rung2/measure_input_floor.py", ("replace", _CWD), 'cwd=str(krt_dir), executable="evil.exe")'),
    "launch keyword: shell": ("rung2/measure_input_floor.py", ("replace", _CWD), "cwd=str(krt_dir), shell=True)"),
    "launch keyword: env": ("rung2/measure_input_floor.py", ("replace", _CWD), "cwd=str(krt_dir), env={})"),
    "launch keyword: a changed cwd": ("rung2/measure_input_floor.py", ("replace", _CWD), 'cwd="other")'),
    "launch keyword: **kwargs": (
        "rung2/measure_input_floor.py", ("replace", _CWD), 'cwd=str(krt_dir), **{"shell": True})'),
    "launch: a positional executable": (
        "rung2/measure_input_floor.py", ("replace", "subprocess.run(cmd, capture_output"),
        'subprocess.run(cmd, -1, "evil.exe", capture_output'),
    "launch keyword: a cwd added": (
        "rung1/build_rung1_layout.py", ("replace", "timeout=3 * 1800 + 600,"), 'timeout=3 * 1800 + 600, cwd="other",'),
    # Its B3 escapes from roots that are not modules: refused by attribute name (a cheap rule,
    # not a sandbox: see the blind spots in the header).
    "a builtin's __self__": ("rung2/record_first_batch.py", None, "print.__self__.exec('1')\n"),
    "a lambda's __globals__": ("rung2/record_first_batch.py", None, "(lambda: 0).__globals__['json']\n"),
    "object.__subclasses__": ("rung2/record_first_batch.py", None, "object.__subclasses__()\n"),
    "a traceback's frame globals": (
        "rung2/record_first_batch.py", None,
        "try:\n    1 / 0\nexcept ZeroDivisionError:\n    sys.exc_info()[2].tb_frame.f_globals\n"),
}


def test_a_launch_argv_without_one_value_has_no_key():
    """A launch is keyed by the one value its argv is bound to; a parameter, a loop over
    anything but a literal of constants, or a global leaves it without a key."""
    found = _evals_sites({"rung2/x.py": (
        "import subprocess\n"
        "G = ['g']\n"
        "def literal():\n    subprocess.run(['a', 'b'])\n"
        "def bound(tool):\n    cmd = ['a', tool]\n    subprocess.run(cmd)\n"
        "def param(cmd):\n    subprocess.run(cmd)\n"
        "def loop(tools):\n    for t in tools:\n        subprocess.run(['a', t])\n"
        "def literal_loop():\n    for t in ('x', 'y'):\n        subprocess.run(['a', t])\n"
        "def rebinds():\n    global G\n    G = ['h']\n"
        "def uses_global():\n    subprocess.run(G)\n"
    )})
    keys = {k[1]: k[3] for k, _ in found["launch"]}
    assert keys["literal"] == "['a', 'b']"
    assert keys["bound"] == "['a', tool]"
    assert keys["literal_loop"] == "['a', t in ('x', 'y')]"
    for function in ("param", "loop", "uses_global"):
        assert keys[function].startswith("<no key"), (function, keys[function])


def test_a_launch_key_holds_its_cwd_and_refuses_other_keywords():
    """Only the reviewed keywords keep a launch's key; cwd's value joins it."""
    found = _evals_sites({"rung2/x.py": (
        "import subprocess\n"
        "def reviewed(d):\n    subprocess.run(['a'], capture_output=True, text=True, timeout=5, check=True)\n"
        "def in_dir(d):\n    where = str(d)\n    subprocess.run(['a'], cwd=where)\n"
        "def as_keyword():\n    subprocess.run(args=['a'])\n"
        "def shell():\n    subprocess.run(['a'], shell=True)\n"
        "def opts(o):\n    subprocess.run(['a'], **o)\n"
        "def positional():\n    subprocess.run(['a'], -1, 'b.exe')\n"
    )})
    keys = {k[1]: k[3] for k, _ in found["launch"]}
    assert keys["reviewed"] == keys["as_keyword"] == "['a']"
    assert keys["in_dir"] == "['a'] with cwd=str(d)"
    for function in ("shell", "opts", "positional"):
        assert keys[function].startswith("<no key"), (function, keys[function])


def test_an_import_that_outlives_its_scope_is_refused():
    """A class body's import and an import into a global or nonlocal name are refused; an
    import in a function that declares other names global stays free."""
    found = _evals_sites({"rung2/x.py": (
        "class C:\n    import json\n"
        "def lazy():\n    global j\n    import json as j\n"
        "def other():\n    global k\n    import json\n    k = json.dumps({})\n"
    )})
    assert sorted((k[1], k[2]) for k, _ in found["forbidden"]) == [
        ("C", "forbidden: an import in a class body"),
        ("lazy", "forbidden: an import into the global or nonlocal j")]


@pytest.mark.parametrize("name", sorted(_MUTANTS))
def test_the_evals_ratchet_kills_the_reviews_mutants(name):
    file, anchor, inserted = _MUTANTS[name]
    sources = _evals_sources()
    text = sources[file]
    if isinstance(anchor, tuple):
        assert text.count(anchor[1]) == 1, anchor
        text = text.replace(anchor[1], inserted)
    elif anchor is None:
        text = text + "\n" + inserted
    else:
        assert text.count(anchor) == 1
        body = text.index(anchor) + len(anchor)
        doc_end = text.index('"""', text.index('"""', body) + 3) + 4  # after the docstring
        text = text[:doc_end] + inserted + text[doc_end:]
    assert not _evals_problems(_evals_sites(sources)), "the unmutated tree must pass"
    assert _evals_problems(_evals_sites({**sources, file: text})), f"{name} survived"


def test_evals_write_kicad_files_only_through_the_write_layer():
    problems = _evals_problems(_evals_sites(_evals_sources()))
    assert not problems, "\n".join(problems)


def test_the_input_floor_script_strips_to_the_pinned_board(kicad_demo, tmp_path, monkeypatch):
    """measure_input_floor.py measures the floor of the board the protocol pins, not of a
    differently staged copy (it used PCB.load + strip_traces until build ticket 67)."""
    monkeypatch.syspath_prepend(str(ROOT / "evals" / "rung2"))  # as when the script runs: it imports stage_input
    spec = importlib.util.spec_from_file_location("measure_input_floor", ROOT / "evals" / "rung2" / "measure_input_floor.py")
    floor = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(floor)
    import shutil

    source = kicad_demo("pic_programmer")
    work = tmp_path / "stripped"
    shutil.copytree(source, work)
    strip = floor.strip_tracks(source / "pic_programmer.kicad_pcb", work / "pic_programmer.kicad_pcb")
    assert autonomy_run.board_sha(work / "pic_programmer.kicad_pcb") == rung_runner.load_protocol()["rungs"]["2"]["board_sha256"]
    assert strip["after"]["segment"] == strip["after"]["via"] == strip["after"]["zone"] == 0
    assert strip["before"]["segment"] == 370 and strip["after"]["footprint"] == 63


def test_staged_install_demo_reproduces_the_rung2_project_pin(kicad_demo, tmp_path):
    staged, pcb = _staged_from_the_install(kicad_demo, tmp_path)
    protocol = rung_runner.load_protocol()

    assert staged["project_sha256"] == protocol["rungs"]["2"]["project_sha256"]
    assert autonomy_run._rung_reasons(
        2, autonomy_run.board_sha(pcb), autonomy_run.project_sha(pcb), protocol
    ) == []
