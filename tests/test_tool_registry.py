"""A new tool touches only its own files (GitHub issue 147).

Pins the three decouplings and shows that every guard they feed still fires:

* ``toolsets/schematic.py``: every function defined in ``build`` is registered; ``@_read_only``
  marks the read-only ones, and nothing else lists tools;
* ``tests/tool_cases/<tool>.py``: the write-guard class, the lock scenario and the wording
  allow-list of one tool, merged into the central structures;
* ``tests/ratchets.d/<tool>.toml``: the tool's ratchet entry, merged into ``_data()``;
* the SKILL.md description no longer lists tools, so it cannot drift from the toolset.

Each test names the mutant it kills in its docstring.
"""

from __future__ import annotations

import re
import sys
import types
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp.toolsets import schematic as schematic_toolset
from tests import _tool_registry as registry
from tests import test_ratchets
from tests.test_write_guard import NOT_WRITING_TOOLS, TOOL_CASES, WRITING_TOOLS

REPO = Path(__file__).resolve().parents[1]
SKILL = REPO / ".claude" / "skills" / "kicad-schematic" / "SKILL.md"

_MARKER = "    # Every function defined in `build` is a tool"


def _annotations(module: types.ModuleType) -> dict[str, tuple[bool, bool]]:
    mcp = MCPServer("t147")
    module.build(mcp)
    return {
        t.name: (bool(t.annotations.read_only_hint), bool(t.annotations.destructive_hint))
        for t in mcp._tool_manager.list_tools()  # type: ignore[attr-defined]
    }


def _module_with(extra: str) -> types.ModuleType:
    """The schematic toolset's source with ``extra`` (indented source) added to ``build`` before the registration."""
    path = Path(schematic_toolset.__file__)
    source = path.read_text(encoding="utf-8")
    assert source.count(_MARKER) == 1
    source = source.replace(_MARKER, extra + "\n" + _MARKER)
    module = types.ModuleType("t147_schematic_variant")
    module.__file__ = str(path)
    sys.modules[module.__name__] = module  # dataclasses and enums look their module up
    try:
        exec(compile(source, str(path), "exec"), module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)
    return module


# --------------------------------------------------------------------------- the toolset registers by itself


def test_a_function_added_to_build_is_registered_as_a_writing_tool():
    """Kills: a registration list that a new def must be added to (the dummy would be missing)."""
    module = _module_with("    def t147_dummy(sch_path: str) -> dict:\n        '''dummy'''\n        return {}\n")
    found = _annotations(module)
    assert found["t147_dummy"] == (False, True)


def test_a_read_only_mark_registers_a_read_only_tool():
    """Kills: @_read_only ignored (the dummy would come out destructive) or applied to every tool."""
    module = _module_with("    @_read_only\n    def t147_reader(sch_path: str) -> dict:\n        '''dummy'''\n        return {}\n")
    found = _annotations(module)
    assert found["t147_reader"] == (True, False)
    assert found["add_symbols"] == (False, True)


def test_the_read_only_marks_agree_with_the_write_classification():
    """Kills: a lost @_read_only (a reader becomes a writer) or a stray one (a writer annotated read-only)."""
    found = _annotations(schematic_toolset)
    for name, (read_only, destructive) in found.items():
        assert read_only != destructive, name
        if read_only:
            assert name in NOT_WRITING_TOOLS and name not in WRITING_TOOLS, f"{name} is read-only but classed as a writer"
        else:
            assert name in WRITING_TOOLS and name not in NOT_WRITING_TOOLS, f"{name} is a writer but not classed as one"


# --------------------------------------------------------------------------- per-tool test cases


def _case(directory: Path, tool: str, body: str, *, file: str | None = None) -> Path:
    path = directory / f"{file or tool}.py"
    path.write_text(f"TOOL = {tool!r}\n" + body, encoding="utf-8")
    return path


def test_a_complete_case_file_is_accepted_and_flows_into_the_derived_sets(tmp_path):
    """Kills: a loader that drops WRITES, not-writing reasons or wording phrases."""
    _case(tmp_path, "t147_w", "WRITES = True\ndef scenario(h):\n    return ('factory', h)\nWORDING_ALLOWED = {'a phrase': 'why'}\n")
    _case(tmp_path, "t147_r", "WRITES = False\nNOT_WRITING_REASON = 'read-only'\n")
    cases = registry.load_cases(tmp_path)
    assert registry.writing_tools(cases) == {"t147_w"}
    assert registry.not_writing_tools(cases) == {"t147_r": "read-only"}
    assert registry.wording_allowed(cases) == {("t147_w", "a phrase"): "why"}
    assert cases["t147_w"].scenario("H") == ("factory", "H")


@pytest.mark.parametrize(
    ("body", "file", "fragment"),
    [
        ("WRITES = True\ndef scenario(h): return None\n", "other_name", "TOOL must be"),
        ("def scenario(h): return None\n", None, "WRITES must be"),
        ("WRITES = 1\ndef scenario(h): return None\n", None, "WRITES must be"),
        ("WRITES = True\n", None, "needs scenario(h)"),
        ("WRITES = True\nscenario = 3\n", None, "needs scenario(h)"),
        ("WRITES = False\n", None, "NOT_WRITING_REASON"),
        ("WRITES = False\nNOT_WRITING_REASON = ''\n", None, "NOT_WRITING_REASON"),
        ("WRITES = True\ndef scenario(h): return None\nWORDING_ALLOWED = {'p': ''}\n", None, "WORDING_ALLOWED"),
        ("WRITES = True\ndef scenario(h): return None\nWORDING_ALLOWED = ['p']\n", None, "WORDING_ALLOWED"),
    ],
)
def test_a_malformed_case_file_is_refused(tmp_path, body, file, fragment):
    """Kills: each contract check of the loader (one parametrised row per check)."""
    _case(tmp_path, "t147_x", body, file=file)
    with pytest.raises(registry.RegistryError, match=re.escape(fragment)):
        registry.load_cases(tmp_path)


def test_a_tool_listed_centrally_and_in_a_case_file_is_refused():
    """Kills: a merge that lets the second entry silently win (two classes for one tool)."""
    with pytest.raises(registry.RegistryError, match="add_symbols"):
        registry.merged(WRITING_TOOLS, {"add_symbols"}, "WRITING_TOOLS")
    with pytest.raises(registry.RegistryError, match="k"):
        registry.merged({"k": "a"}, {"k": "b"}, "dict")
    assert registry.merged({"a": 1}, {"b": 2}, "dict") == {"a": 1, "b": 2}
    assert registry.merged(frozenset({"a"}), {"b"}, "set") == {"a", "b"}


def test_the_shipped_case_files_are_valid_and_unique():
    """Kills: a shipped case file that the loaders accept but the central lists also hold."""
    assert set(TOOL_CASES) == {p.stem for p in registry.CASES_DIR.glob("*.py") if not p.name.startswith("_")}
    for name in TOOL_CASES:
        assert (name in WRITING_TOOLS) == TOOL_CASES[name].WRITES


# --------------------------------------------------------------------------- per-tool ratchet entries


def _ratchet_file(directory: Path, name: str, text: str) -> None:
    (directory / f"{name}.toml").write_text(text, encoding="utf-8")


def test_a_ratchet_file_adds_its_tools_entry(tmp_path):
    """Kills: a loader that ignores the directory or files the entry under the wrong table."""
    _ratchet_file(tmp_path, "t147_w", '[writing_tool.t147_w]\nself_check = "reload-verify"\nnote = "n"\nproof = []\n')
    _ratchet_file(tmp_path, "t147_n", '[not_a_file_writer.t147_n]\nnote = "n"\n')
    extra = registry.load_ratchet_entries(tmp_path)
    assert set(extra["writing_tool"]) == {"t147_w"} and set(extra["not_a_file_writer"]) == {"t147_n"}
    data = registry.merge_ratchet_data({"writing_tool": {"old": {}}, "raw_write": [1]}, extra)
    assert set(data["writing_tool"]) == {"old", "t147_w"}
    assert data["raw_write"] == [1] and set(data["not_a_file_writer"]) == {"t147_n"}


@pytest.mark.parametrize(
    ("name", "text", "fragment"),
    [
        ("t147_x", '[writing_tool.other]\nnote = "n"\n', "must be [writing_tool.t147_x]"),
        ("t147_x", '[raw_write.t147_x]\nnote = "n"\n', "unknown section"),
        ("t147_x", "", "empty"),
    ],
)
def test_a_malformed_ratchet_file_is_refused(tmp_path, name, text, fragment):
    """Kills: the file-name check, the section whitelist (no frozen list can be edited from here) and the empty-file check."""
    _ratchet_file(tmp_path, name, text)
    with pytest.raises(registry.RegistryError, match=re.escape(fragment)):
        registry.load_ratchet_entries(tmp_path)


def test_a_tool_in_the_central_ratchets_and_in_a_file_is_refused(tmp_path):
    """Kills: a merge in which a per-tool file overrides (and so can weaken) a reviewed central entry."""
    extra = {"writing_tool": {"add_symbols": {"self_check": "none"}}, "not_a_file_writer": {}, "read_only_tool_writes": {}}
    with pytest.raises(registry.RegistryError, match="add_symbols"):
        registry.merge_ratchet_data({"writing_tool": {"add_symbols": {}}}, extra)


def test_a_per_tool_ratchet_entry_without_a_proof_fails_the_ratchet_exactly_as_a_central_one(tmp_path, monkeypatch):
    """Kills: _data() not reading ratchets.d, or the proof requirement skipped for merged entries."""
    _ratchet_file(
        tmp_path, "t147_dummy",
        '[writing_tool.t147_dummy]\nself_check = "reload-verify"\nnote = "dummy"\nproof = []\n',
    )
    monkeypatch.setattr(registry, "RATCHETS_DIR", tmp_path)
    test_ratchets._data.cache_clear()
    try:
        assert "t147_dummy" in test_ratchets._data()["writing_tool"]
        with pytest.raises(pytest.fail.Exception) as caught:
            test_ratchets.test_writing_tools_without_a_proven_self_check_only_shrink()
        assert "NEW: writing tool 't147_dummy'" in str(caught.value)
    finally:
        monkeypatch.undo()
        test_ratchets._data.cache_clear()
    assert "t147_dummy" not in test_ratchets._data()["writing_tool"]


# --------------------------------------------------------------------------- the skill's description


def _skill_description() -> str:
    text = SKILL.read_text(encoding="utf-8")
    match = re.search(r"^description: (.*)$", text, re.MULTILINE)
    assert match, "SKILL.md has no description line"
    return match.group(1)


def test_the_schematic_skill_description_lists_no_tool_and_says_how_to_find_them():
    """Kills: a tool list back in the description (it drifts, and every new tool edits that line) and the pointer lost."""
    description = _skill_description()
    names = set(_annotations(schematic_toolset))
    listed = sorted(n for n in names if re.search(rf"\b{re.escape(n)}\b", description))
    assert not listed, f"the description names tools again: {listed}"
    assert "`list_toolsets`" in description and "`load_toolset`" in description
    assert "`schematic` MCP toolset" in description
    assert 'e.g. "draw the schematic"' in description  # the trigger phrases stay


# --------------------------------------------------------------------------- the central tests read the case files


@pytest.fixture
def dummy_case_dir(tmp_path, monkeypatch):
    """A tool_cases directory with one writing and one non-writing dummy; the three central test modules
    are reloaded against it and again, in the end, against the shipped directory."""
    import importlib

    from tests import test_description_gate_wording, test_write_guard, test_write_guard_tools

    _case(
        tmp_path, "t147_w",
        "WRITES = True\ndef scenario(h):\n    return ('scenario of', h.__name__)\nWORDING_ALLOWED = {'t147 phrase': 'why'}\n",
    )
    _case(tmp_path, "t147_r", "WRITES = False\nNOT_WRITING_REASON = 'read-only'\n")
    monkeypatch.setattr(registry, "CASES_DIR", tmp_path)
    try:
        importlib.reload(test_write_guard)
        importlib.reload(test_write_guard_tools)
        importlib.reload(test_description_gate_wording)
        yield test_write_guard, test_write_guard_tools, test_description_gate_wording
    finally:
        monkeypatch.undo()
        importlib.reload(test_write_guard)
        importlib.reload(test_write_guard_tools)
        importlib.reload(test_description_gate_wording)


def test_the_write_guard_classes_a_case_file_tool(dummy_case_dir):
    """Kills: WRITING_TOOLS / NOT_WRITING_TOOLS not reading the case files (the dummy would be unclassified)."""
    guard, _, _ = dummy_case_dir
    assert "t147_w" in guard.WRITING_TOOLS and "t147_r" not in guard.WRITING_TOOLS
    assert guard.NOT_WRITING_TOOLS["t147_r"] == "read-only"
    assert "add_symbols" in guard.WRITING_TOOLS  # the central entries stay


def test_the_lock_matrix_gets_a_scenario_for_a_case_file_tool(dummy_case_dir):
    """Kills: SCENARIOS not reading the case files, or scenario(h) not given the module of the helpers."""
    _, tools_module, _ = dummy_case_dir
    assert tools_module.SCENARIOS["t147_w"] == ("scenario of", tools_module.__name__)
    assert "t147_r" not in tools_module.SCENARIOS
    assert set(tools_module.SCENARIOS) == set(tools_module.WRITING_TOOLS)


def test_the_wording_sweep_allows_a_case_file_phrase_for_its_own_tool_only(dummy_case_dir):
    """Kills: _CLAIM_WORD_ALLOWED not reading the case files."""
    _, _, wording = dummy_case_dir
    assert wording._CLAIM_WORD_ALLOWED[("t147_w", "t147 phrase")] == "why"
    assert ("t147_r", "t147 phrase") not in wording._CLAIM_WORD_ALLOWED
