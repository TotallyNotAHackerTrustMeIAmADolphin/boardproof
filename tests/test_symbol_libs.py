"""Build ticket 76: lib_id tools resolve a nickname through the project sym-lib-table first.

``add_symbols`` (and ``add_labels`` kind power) used to look a lib_id up only in KiCad's folder,
so a project library such as ``scbe:R`` failed with "Library 'scbe.kicad_sym' not found".
Fixtures are built through the project's own tools (``register_library_asset`` writes the
table) and kicad_tools' S-expression model (the tiny library); no KiCad file is edited as text.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string
from mcp.server.mcpserver import MCPServer

from kicad_mcp import symbol_libs, write_layer
from kicad_mcp.toolsets import library as library_toolset
from kicad_mcp.toolsets import schematic as schematic_toolset

FIXTURE = Path(__file__).parent / "fixtures" / "simple_led.kicad_sch"

_TINY_LIB = """(kicad_symbol_lib (version 20231120) (generator "test") (generator_version "8.0")
  (symbol "R" (pin_numbers hide) (pin_names (offset 0)) (in_bom yes) (on_board yes)
    (property "Reference" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Value" "R" (at 0 0 0) (effects (font (size 1.27 1.27))))
    (property "Footprint" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (property "Datasheet" "" (at 0 0 0) (effects (font (size 1.27 1.27)) hide))
    (symbol "R_0_1" (rectangle (start -1.016 -2.54) (end 1.016 2.54)
      (stroke (width 0.254) (type default)) (fill (type none))))
    (symbol "R_1_1"
      (pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
      (pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27))))))))
"""


def _table(path: Path, *rows: tuple[str, str]) -> Path:
    """A lib table built through kicad_tools' S-expression model (tests only)."""
    body = "".join(f'(lib (name "{n}")(type "KiCad")(uri "{u}")(options "")(descr ""))' for n, u in rows)
    data = parse_string(f"(sym_lib_table (version 7) {body})").to_string().encode("utf-8")
    write_layer.save_bytes(data, path, verify=lambda tmp: None)
    return path


def _tools(module) -> dict:
    mcp = MCPServer("test")
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _write_lib(path: Path, symbol_name: str = "R") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_layer.save(parse_string(_TINY_LIB.replace('"R"', f'"{symbol_name}"').replace('"R_', f'"{symbol_name}_')), path)
    return path


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project folder with a sheet and a registered project library ``scbe`` (lib/scbe.kicad_sym)."""
    sheet = tmp_path / "proj.kicad_sch"
    shutil.copy(FIXTURE, sheet)
    lib = _write_lib(tmp_path / "lib" / "scbe.kicad_sym")
    result = _tools(library_toolset)["register_library_asset"](str(tmp_path), symbol_lib_path=str(lib))
    assert result["symbol"]["status"] == "ok", result
    return sheet


def _spec(lib_id: str, ref: str = "R90") -> dict:
    return {"lib_id": lib_id, "x": 150, "y": 100, "ref": ref, "value": "1k"}


def test_add_symbols_places_a_project_library_symbol(project):
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("scbe:R")])
    assert result["status"] == "ok", result
    assert result["placed"] == ["R90"]
    assert "scbe:R" in project.read_text(encoding="utf-8")


def test_add_labels_power_uses_a_project_library_symbol(tmp_path, project):
    _write_lib(tmp_path / "lib" / "scbe.kicad_sym", symbol_name="GND")
    result = _tools(schematic_toolset)["add_labels"](str(project), [{"kind": "power", "text": "scbe:GND", "x": 100, "y": 100}])
    assert result["status"] == "ok", result


def test_the_project_table_wins_over_kicads_folder(project):
    """``Device`` is also KiCad's nickname; the project row wins (KiCad semantics)."""
    root = project.parent
    lib = _write_lib(root / "lib" / "Device.kicad_sym", symbol_name="OnlyHere")
    assert _tools(library_toolset)["register_library_asset"](str(root), symbol_lib_path=str(lib))["symbol"]["status"] == "ok"
    tools = _tools(schematic_toolset)
    assert tools["add_symbols"](str(project), [_spec("Device:OnlyHere")])["status"] == "ok"
    # Stock Device:L lives only in KiCad's folder; the project row owns the nickname, so it is a miss.
    result = tools["add_symbols"](str(project), [_spec("Device:L", "L91")])
    assert result["status"] == "incomplete"
    assert "sym-lib-table" in result["reason"]


def test_stock_library_still_resolves(project):
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("Device:R")])
    assert result["status"] == "ok", result


def test_unknown_nickname_names_the_tables_searched_and_writes_nothing(project):
    before = project.read_bytes()
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("nosuchlib:R")])
    assert result["status"] == "incomplete"
    reason = result["reason"]
    assert "nosuchlib" in reason
    assert str(project.parent / "sym-lib-table") in reason
    assert "KiCad" in reason
    assert project.read_bytes() == before


def test_a_batch_with_one_unknown_nickname_places_nothing(project):
    before = project.read_bytes()
    result = _tools(schematic_toolset)["add_symbols"](project.as_posix(), [_spec("scbe:R"), _spec("nosuchlib:R", "R92")])
    assert result["status"] == "incomplete"
    assert project.read_bytes() == before


def test_a_missing_symbol_in_a_project_library_names_the_library_file(project):
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("scbe:Nope")])
    assert result["status"] == "incomplete"
    assert "scbe.kicad_sym" in result["reason"]


def test_a_row_pointing_outside_the_project_and_kicad_is_refused(tmp_path, project):
    """A hand-written row may name any path; the read is limited to the project and the KiCad install."""
    outside = _write_lib(tmp_path.parent / f"{tmp_path.name}_outside" / "evil.kicad_sym")
    _table(project.parent / "sym-lib-table", ("evil", outside.as_posix()))
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("evil:R")])
    assert result["status"] == "incomplete"
    assert "outside" in result["reason"]


def test_a_row_whose_file_name_differs_from_the_nickname_is_refused_with_the_reason(tmp_path, project):
    root = project.parent
    _write_lib(root / "lib" / "other.kicad_sym")
    assert _tools(library_toolset)["register_library_asset"](str(root), symbol_lib_path=str(root / "lib" / "other.kicad_sym"), nickname="alias")["symbol"]["status"] == "ok"
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("alias:R")])
    assert result["status"] == "incomplete"
    assert "other.kicad_sym" in result["reason"] and "alias" in result["reason"]


def test_the_global_table_is_read_after_the_project_table(tmp_path, project, monkeypatch):
    """A nickname only the user's global sym-lib-table has (inside the KiCad install) resolves."""
    kicad_dir = Path(symbol_libs.kicad_symbol_dirs()[0])
    config = tmp_path / "appdata" / "kicad" / "8.0"
    config.mkdir(parents=True)
    _table(config / "sym-lib-table", ("GlobalDevice", (kicad_dir / "Device.kicad_sym").as_posix()))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    # kicad_tools matches the file name to the nickname, so a differing nickname is refused with the reason.
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("GlobalDevice:R")])
    assert result["status"] == "incomplete"
    assert "Device.kicad_sym" in result["reason"]


def test_lock_refusal_of_issue_68_stays_intact(project):
    """KiCad's lock file next to the sheet still refuses the write."""
    lock = project.parent / "~proj.kicad_sch.lck"
    lock.write_bytes(b'{"hostname":"OTHER-HOST","username":"someone-else"}')
    before = project.read_bytes()
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("scbe:R")])
    assert result.get("code") == "file_open_in_kicad", result
    assert project.read_bytes() == before


def test_tool_text_tells_the_model_how_a_nickname_is_resolved():
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    docs = {t.name: t.description for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    for needle in ("project's sym-lib-table", "${KIPRJMOD}", "<nickname>.kicad_sym", "unknown nickname is refused"):
        assert needle in docs["add_symbols"], needle
    assert "sym-lib-table" in docs["add_labels"]


def _corrupt(path: Path) -> None:
    write_layer.save_bytes(b"(sym_lib_table (version 7) (lib (name ", path, verify=lambda tmp: None)


def test_an_unparseable_project_table_refuses_instead_of_falling_through_to_kicad(project):
    table = project.parent / "sym-lib-table"
    _corrupt(table)
    before = project.read_bytes()
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("Device:L", "L91")])
    assert result["status"] == "incomplete"
    assert str(table) in result["reason"] and "unreadable" in result["reason"]
    assert project.read_bytes() == before
    power = _tools(schematic_toolset)["add_labels"](str(project), [{"kind": "power", "text": "power:GND", "x": 1, "y": 1}])
    assert power["status"] == "incomplete" and str(table) in power["reason"]
    assert project.read_bytes() == before


def test_an_unparseable_global_table_refuses_instead_of_falling_through_to_kicad(tmp_path, project, monkeypatch):
    config = tmp_path / "appdata" / "kicad" / "8.0"
    config.mkdir(parents=True)
    _corrupt(config / "sym-lib-table")
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    before = project.read_bytes()
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("Device:L", "L91")])
    assert result["status"] == "incomplete"
    assert str(config / "sym-lib-table") in result["reason"]
    assert project.read_bytes() == before


@pytest.mark.parametrize("nick", ["..", "a/b", "a" + chr(92) + "b"])
def test_a_nickname_that_is_not_a_plain_name_is_refused_and_nothing_is_written(project, nick):
    before = project.read_bytes()
    tools = _tools(schematic_toolset)
    result = tools["add_symbols"](str(project), [_spec(f"{nick}:R")])
    assert result["status"] == "incomplete" and "not a plain name" in result["reason"]
    power = tools["add_labels"](str(project), [{"kind": "power", "text": f"{nick}:GND", "x": 1, "y": 1}])
    assert power["status"] == "incomplete" and "not a plain name" in power["reason"]
    assert project.read_bytes() == before


def test_the_project_table_wins_over_the_global_table_for_the_same_nickname(tmp_path, project, monkeypatch):
    root = project.parent
    mine = _write_lib(root / "lib" / "dup.kicad_sym", symbol_name="ProjectOnly")
    theirs = _write_lib(root / "other" / "dup.kicad_sym", symbol_name="GlobalOnly")
    assert _tools(library_toolset)["register_library_asset"](str(root), symbol_lib_path=str(mine))["symbol"]["status"] == "ok"
    config = tmp_path / "appdata" / "kicad" / "8.0"
    config.mkdir(parents=True)
    _table(config / "sym-lib-table", ("dup", theirs.as_posix()))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    tools = _tools(schematic_toolset)
    assert tools["add_symbols"](str(project), [_spec("dup:ProjectOnly")])["status"] == "ok"
    assert tools["add_symbols"](str(project), [_spec("dup:GlobalOnly", "R92")])["status"] == "incomplete"


def test_a_relative_uri_is_anchored_to_the_project_folder_not_the_working_directory(tmp_path, project, monkeypatch):
    root = project.parent
    _write_lib(root / "lib" / "rel.kicad_sym")
    _table(root / "sym-lib-table", ("rel", "lib/rel.kicad_sym"))
    elsewhere = tmp_path.parent / f"{tmp_path.name}_cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    result = _tools(schematic_toolset)["add_symbols"](str(project), [_spec("rel:R")])
    assert result["status"] == "ok", result
