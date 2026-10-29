"""Tests for the library toolset: registration/dedup, portability audit,
and the minimal-subset KLC validator."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from kicad_mcp import write_layer
from kicad_mcp.sourcing.footprint_gen import generate_footprint
from kicad_mcp.toolsets import library as library_toolset


def _build_and_get_fns(mcp: MCPServer, module) -> dict:
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _make_footprint(project_dir: Path, name: str = "0402_1005Metric") -> Path:
    fp_dir = project_dir / "MyLibs.pretty"
    fp_dir.mkdir(parents=True, exist_ok=True)
    fp_path = fp_dir / f"{name}.kicad_mod"
    result = generate_footprint("chip", fp_path, size="0402")
    assert result["status"] == "ok", result
    return fp_dir


def test_build_registers_expected_tools():
    mcp = MCPServer("test")
    names = library_toolset.build(mcp)
    assert set(names) == {"register_library_asset", "check_project_portability", "validate_klc"}


def test_register_footprint_lib_writes_relative_kiprjmod_uri(tmp_path):
    fp_dir = _make_footprint(tmp_path)
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)

    result = tools["register_library_asset"](str(tmp_path), footprint_lib_path=str(fp_dir))
    assert result["status"] == "ok"
    assert result["footprint"]["status"] == "ok"
    assert result["footprint"]["uri"] == "${KIPRJMOD}/MyLibs.pretty"

    table_text = (tmp_path / "fp-lib-table").read_text()
    assert "${KIPRJMOD}/MyLibs.pretty" in table_text
    assert "C:" not in table_text and "/tmp" not in table_text  # never an absolute path


def test_register_same_asset_twice_dedupes(tmp_path):
    fp_dir = _make_footprint(tmp_path)
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)

    first = tools["register_library_asset"](str(tmp_path), footprint_lib_path=str(fp_dir))
    assert first["footprint"]["status"] == "ok"

    second = tools["register_library_asset"](str(tmp_path), footprint_lib_path=str(fp_dir))
    assert second["footprint"]["status"] == "already_present"
    assert second["footprint"]["nickname"] == first["footprint"]["nickname"]

    table_text = (tmp_path / "fp-lib-table").read_text()
    assert table_text.count("(lib") == 1  # no duplicate entry


def test_register_nickname_collision_with_different_path_gets_suffixed(tmp_path):
    fp_dir_a = _make_footprint(tmp_path, name="chip_a")
    (tmp_path / "OtherLibs.pretty").mkdir()
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)

    r1 = tools["register_library_asset"](str(tmp_path), footprint_lib_path=str(fp_dir_a), nickname="MyLibs")
    assert r1["footprint"]["nickname"] == "MyLibs"

    r2 = tools["register_library_asset"](
        str(tmp_path), footprint_lib_path=str(tmp_path / "OtherLibs.pretty"), nickname="MyLibs"
    )
    assert r2["footprint"]["status"] == "ok"
    assert r2["footprint"]["nickname"] != "MyLibs"
    assert "note" in r2["footprint"]


def test_register_asset_outside_project_dir_refuses_absolute_path(tmp_path):
    outside = tmp_path.parent / f"outside_{tmp_path.name}.pretty"
    outside.mkdir(exist_ok=True)
    project_dir = tmp_path / "proj"
    project_dir.mkdir()

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)
    result = tools["register_library_asset"](str(project_dir), footprint_lib_path=str(outside))
    assert result["status"] == "error"
    assert result["footprint"]["status"] == "error"
    assert "portable" in result["footprint"]["reason"] or "outside" in result["footprint"]["reason"]


def test_register_preserves_other_entries_options_and_descr(tmp_path):
    # A pre-existing table entry with real options/descr text must survive
    # a subsequent registration rewriting the file.
    table_path = tmp_path / "fp-lib-table"
    table_path.write_text(
        '(fp_lib_table\n\t(version 7)\n\t(lib (name "Existing") (type "KiCad") '
        '(uri "${KIPRJMOD}/Existing.pretty") (options "opt1") (descr "hand-written entry"))\n)\n'
    )
    fp_dir = _make_footprint(tmp_path)

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)
    result = tools["register_library_asset"](str(tmp_path), footprint_lib_path=str(fp_dir))
    assert result["footprint"]["status"] == "ok"

    table_text = table_path.read_text()
    assert "opt1" in table_text
    assert "hand-written entry" in table_text


# --------------------------------------------------------------------------------------------
# Build ticket 76: a registration inserts one row and leaves every other byte of the table as it
# was; a table it cannot parse is refused, never rewritten. Lib tables are user configuration,
# not a KiCad design file: these fixtures are raw bytes on purpose, because the tests assert the
# exact bytes the tool must keep (the layout KiCad 10.0.7 itself writes: two-space indent, no
# space between fields, CRLF on Windows).

_ROW_A = b'  (lib (name "A")(type "KiCad")(uri "${KIPRJMOD}/A.pretty")(options "")(descr "")%s)\r\n'
_ROW_B = b'  (lib (name "B")(type "KiCad")(uri "${KIPRJMOD}/B.pretty")(options "opt")(descr "kept"))\r\n'
_NEW_FP_ROW = b'  (lib (name "New") (type "KiCad") (uri "${KIPRJMOD}/New.pretty") (options "") (descr ""))\r\n'


def _fp_table(row_a_tail: bytes = b"") -> bytes:
    return b"(fp_lib_table\r\n  (version 7)\r\n" + _ROW_A % row_a_tail + _ROW_B + b")\r\n"


def _register_new_pretty(project: Path) -> dict:
    (project / "New.pretty").mkdir(exist_ok=True)
    tools = _build_and_get_fns(MCPServer("test"), library_toolset)
    return tools["register_library_asset"](str(project), footprint_lib_path=str(project / "New.pretty"))


@pytest.mark.parametrize(
    "row_a_tail", [b"(disabled)", b"(hidden)", b"(disabled)(hidden)"], ids=["disabled", "hidden", "both"]
)
def test_register_keeps_every_existing_row_byte_for_byte(tmp_path, row_a_tail):
    # Reproduced on main f7d5b5a: the rewrite dropped (disabled)/(hidden) (KiCad 10.0.7 then
    # loads a library the user switched off) and re-laid every row with tabs and spaces.
    table = tmp_path / "fp-lib-table"
    table.write_bytes(_fp_table(row_a_tail))

    result = _register_new_pretty(tmp_path)

    assert result["footprint"]["status"] == "ok", result
    assert table.read_bytes() == (
        b"(fp_lib_table\r\n  (version 7)\r\n" + _ROW_A % row_a_tail + _ROW_B + _NEW_FP_ROW + b")\r\n"
    )


def _with_new_row(before: bytes) -> bytes:
    """``before`` (a CRLF table whose closing ``)`` starts a line) with the New row inserted as its
    last row, on the line before that ``)``."""
    close = before.rstrip().rfind(b")")
    line_start = before.rfind(b"\n", 0, close) + 1
    assert before[line_start:close] == b""
    return before[:line_start] + _NEW_FP_ROW + before[line_start:]


def _b_row(old: bytes, new: bytes) -> bytes:
    """``_fp_table()`` with ``old`` in row B replaced by ``new``."""
    assert old in _ROW_B
    return _fp_table().replace(_ROW_B, _ROW_B.replace(old, new, 1), 1)


_KICAD_LOADS = {
    # KiCad 10.0.7 reads each of these tables without skipping it (kicad-cli sch erc, throwaway
    # KICAD_CONFIG_HOME, build ticket 76 fix round 1), so the strict check must let it through and
    # the registration keep it as is.
    "escaped-backslash": _b_row(b'(descr "kept")', b'(descr "a\\\\b")'),
    "trailing-escaped-backslash": _b_row(b'(descr "kept")', b'(descr "a\\\\")'),
    "backslash-letter": _b_row(b'(descr "kept")', b'(descr "C:\\x")'),
    "escaped-newline": _b_row(b'(descr "kept")', b'(descr "a\\nb")'),
    "parens-and-comment-marks-in-a-string": _b_row(b'(descr "kept")', b'(descr "a ) ( b #1; c")'),
    "tab-and-utf8-in-a-string": _b_row(b'(descr "kept")', b'(descr "\xc2\xb5\tM\xc3\xbcller")'),
    "bare-atoms": _b_row(b'(name "B")(type "KiCad")', b"(name B)(type KiCad)"),
    "hash-inside-a-bare-atom": _b_row(b'(options "opt")', b"(options a#b;c)"),
    "fields-reordered-and-spaced": _b_row(
        b'(name "B")(type "KiCad")(uri "${KIPRJMOD}/B.pretty")',
        b'(uri "${KIPRJMOD}/B.pretty")  (type "KiCad") (name "B")',
    ),
    "no-options-no-descr": _b_row(b'(options "opt")(descr "kept")', b""),
    "no-version": _fp_table().replace(b"  (version 7)\r\n", b"", 1),
    "version-8": _fp_table().replace(b"(version 7)", b"(version 8)", 1),
    "nested-table-row": _b_row(b'(type "KiCad")', b'(type "Table")'),
    # Loaded table, unusable row: KiCad reports it but does not skip the table.
    "flag-twice": _b_row(b'(descr "kept")', b'(descr "kept")(disabled)(disabled)'),
    # Fix round 2, each measured the same way: KiCad 10.0.7 reads a lib table with the grammar of
    # its library_table_grammar.h (PEGTL), without the escape rule that file has on master. These
    # tables load; round 1 refused some of them with a false reason.
    "trailing-single-backslash": _b_row(b'(descr "kept")', b'(descr "a\\")'),
    "hash-atom": _b_row(b'(descr "kept")', b"(descr #ab)"),
    "semicolon-atom": _b_row(b'(descr "kept")', b"(descr ;x)"),
    "quote-inside-an-atom": _b_row(b'(descr "kept")', b'(descr a"b")'),
    "hash-atom-on-the-next-line": _b_row(b'(descr "kept")', b"(descr\r\n#x)"),
    "formfeed-and-vtab-between-fields": _b_row(b'(type "KiCad")(uri', b'(type "KiCad")\x0c(uri').replace(
        b'(options "opt")', b'\x0b(options "opt")', 1
    ),
    "formfeed-after-the-table": _fp_table() + b"\x0c",
    # A vertical tab or form feed is space between tokens, yet part of a bare atom.
    "vtab-ending-an-atom": _b_row(b'(descr "kept")', b"(descr x\x0b)"),
    "formfeed-inside-an-atom": _b_row(b'(descr "kept")', b"(descr a\x0cb)"),
    "space-inside-the-row-parentheses": _b_row(b'(lib (name "B")', b'( lib (name "B")').replace(
        b'(descr "kept"))', b'(descr "kept") )', 1
    ),
    "line-break-between-a-field-and-its-value": _b_row(b'(descr "kept")', b'(descr\r\n  "kept")'),
    "quoted-version-number": _fp_table().replace(b"(version 7)", b'(version "7")', 1),
    "no-whitespace-after-the-root-name": _fp_table().replace(b"(fp_lib_table\r\n  (version", b"(fp_lib_table(version", 1),
}


@pytest.mark.parametrize("case", sorted(_KICAD_LOADS))
def test_register_accepts_a_table_kicad_loads_and_keeps_it_byte_for_byte(tmp_path, case):
    table = tmp_path / "fp-lib-table"
    table.write_bytes(_KICAD_LOADS[case])

    result = _register_new_pretty(tmp_path)

    assert result["footprint"]["status"] == "ok", result
    assert table.read_bytes() == _with_new_row(_KICAD_LOADS[case])


@pytest.mark.parametrize("nickname", ['a"b', "a\\b"], ids=["quote", "backslash"])
def test_register_refuses_a_nickname_kicad_cannot_read_and_writes_nothing(tmp_path, nickname):
    # Reproduced on 501bc9b: nickname 'a"b' was written as (name "a\"b"), and KiCad 10.0.7 then
    # skips the whole table, so none of its libraries load.
    table = tmp_path / "fp-lib-table"
    table.write_bytes(_fp_table())
    (tmp_path / "New.pretty").mkdir()
    tools = _build_and_get_fns(MCPServer("test"), library_toolset)

    result = tools["register_library_asset"](str(tmp_path), footprint_lib_path=str(tmp_path / "New.pretty"), nickname=nickname)

    assert result["status"] == "error"
    assert repr(nickname) in result["footprint"]["reason"]
    assert "nothing was written" in result["footprint"]["reason"]
    # Delta review 1, low a: the reason names what is true of each character (measured for
    # 10.0.7; the escape rule is in library_table_grammar.h on KiCad's development branch).
    assert "KiCad 10.0.7 cannot read a '\"' in a lib table string" in result["footprint"]["reason"]
    assert "KiCad's development branch reads '\\' as the start of an escape" in result["footprint"]["reason"]
    assert table.read_bytes() == _fp_table()


def test_register_refuses_a_library_path_with_a_quote_and_creates_no_table(tmp_path):
    tools = _build_and_get_fns(MCPServer("test"), library_toolset)

    # Only the path is registered, never read, so a name Windows cannot create still tests it.
    result = tools["register_library_asset"](str(tmp_path), footprint_lib_path=str(tmp_path / 'a"b.pretty'))

    assert result["status"] == "error"
    assert "library path" in result["footprint"]["reason"]
    assert not (tmp_path / "fp-lib-table").exists()


def test_register_into_an_lf_tab_table_matches_its_line_ending_and_indent(tmp_path):
    table = tmp_path / "sym-lib-table"
    before = b'(sym_lib_table\n\t(version 7)\n\t(lib (name "S")(type "KiCad")(uri "${KIPRJMOD}/S.kicad_sym")(options "")(descr "")(disabled))\n)\n'
    table.write_bytes(before)
    (tmp_path / "New.kicad_sym").write_bytes(b"")  # only the path is registered, never read

    tools = _build_and_get_fns(MCPServer("test"), library_toolset)
    result = tools["register_library_asset"](str(tmp_path), symbol_lib_path=str(tmp_path / "New.kicad_sym"))

    assert result["symbol"]["status"] == "ok", result
    assert table.read_bytes() == before[: -len(b")\n")] + (
        b'\t(lib (name "New") (type "KiCad") (uri "${KIPRJMOD}/New.kicad_sym") (options "") (descr ""))\n)\n'
    )


def test_register_into_a_cr_only_table_keeps_its_line_ending(tmp_path):
    # Review round 1, item 7: on 501bc9b a CR-only table (KiCad 10.0.7 loads it) got "\r\r\n".
    table = tmp_path / "fp-lib-table"
    before = _fp_table().replace(b"\r\n", b"\r")
    table.write_bytes(before)

    result = _register_new_pretty(tmp_path)

    assert result["footprint"]["status"] == "ok", result
    assert table.read_bytes() == before[: -len(b")\r")] + _NEW_FP_ROW.replace(b"\r\n", b"\r") + b")\r"


def _link_to_shared_table(tmp_path: Path, monkeypatch, kind: str) -> tuple[Path, Path]:
    """A project whose fp-lib-table is a link to a table outside it. A symbolic link needs a
    privilege Windows accounts often lack; without it the project holds a copy that claims to
    be a link (``Path.is_symlink``), which is what the tool checks."""
    shared = tmp_path / "shared" / "fp-lib-table"
    shared.parent.mkdir()
    shared.write_bytes(_fp_table())
    project = tmp_path / "project"
    project.mkdir()
    link = project / "fp-lib-table"
    if kind == "hard":
        os.link(shared, link)
        return project, shared
    try:
        link.symlink_to(shared)
    except OSError:
        link.write_bytes(_fp_table())
        real_is_symlink = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "fp-lib-table" and self.parent.name == "project" or real_is_symlink(self))
    return project, shared


@pytest.mark.parametrize("kind", ["symbolic", "hard"])
def test_register_refuses_a_table_that_is_a_link_and_keeps_the_link(tmp_path, monkeypatch, kind):
    # Review round 1, item 7: os.replace would put a plain file where the link was, cutting the
    # project off from the shared table without a word.
    project, shared = _link_to_shared_table(tmp_path, monkeypatch, kind)
    link = project / "fp-lib-table"
    link_was_symlink = link.is_symlink()

    result = _register_new_pretty(project)

    entry = result["footprint"]
    assert entry["code"] == "lib_table_is_a_link"
    assert entry["table"] == str(link.resolve() if kind == "hard" else project.resolve() / "fp-lib-table")
    assert f"{kind} link" in entry["reason"]
    assert "Ask the user to" in entry["reason"]
    assert "Do not edit this file yourself" in entry["reason"]
    assert link.is_symlink() is link_was_symlink
    assert link.read_bytes() == _fp_table()
    assert shared.read_bytes() == _fp_table()
    assert sorted(p.name for p in project.iterdir()) == ["New.pretty", "fp-lib-table"]


def test_register_creates_a_missing_table_with_the_new_row_only(tmp_path):
    result = _register_new_pretty(tmp_path)

    assert result["footprint"]["status"] == "ok", result
    eol = os.linesep.encode()
    assert (tmp_path / "fp-lib-table").read_bytes() == (
        b"(fp_lib_table" + eol + b"\t(version 7)" + eol
        + b'\t(lib (name "New") (type "KiCad") (uri "${KIPRJMOD}/New.pretty") (options "") (descr ""))' + eol
        + b")" + eol
    )


_UNPARSEABLE = {
    # Reproduced on main f7d5b5a: each of these was replaced by a table holding only the new row.
    "bom": (b"\xef\xbb\xbf" + _fp_table(), "a UTF-8 byte-order mark at line 1, column 1", "without a byte-order mark"),
    "broken-row": (
        b"(fp_lib_table\r\n  (version 7)\r\n" + (_ROW_A % b"")[: -len(b")\r\n")] + b"\r\n" + _ROW_B + b")\r\n",
        "the (lib ...) row at line 3, column 3 is not closed before the (lib ...) at line 4, column 3",
        "add its missing ')'",
    ),
    "extra-parenthesis": (
        b"(fp_lib_table\r\n  (version 7)\r\n" + (_ROW_A % b"")[: -len(b"\r\n")] + b")\r\n" + _ROW_B + b")\r\n",
        "line 4, column 3",  # row A's extra ')' closes the table; row B is then text after it
        "remove the extra ')'",
    ),
    "wrong-root": (_fp_table().replace(b"(fp_lib_table", b"(sym_lib_table", 1), "(sym_lib_table ...)", "(fp_lib_table ...)"),
    "not-utf8": (_b_row(b'(descr "kept")', b'(descr "M\xfcller")'), "byte 0xFC at line 4, column 84 is not UTF-8", "re-save it as UTF-8"),
    "empty": (b"\r\n", "the file is empty", "lists no libraries"),
    # Fix round 1: tables kicad_tools parses but KiCad 10.0.7 skips (measured with kicad-cli, each
    # one a skip warning and none of the table's libraries loaded).
    "unknown-row-token": (_fp_table(b"(future_token 1 bare)"), "(future_token ...) at line 3", "(future_token ...)"),
    "unknown-root-token": (
        _fp_table().replace(b"  (version 7)\r\n", b"  (version 7)\r\n  (foo 1)\r\n", 1),
        "(foo ...) at line 3",
        "(foo ...)",
    ),
    # Fix round 2: KiCad's string ends at the next '"' (no escapes), so text follows the value.
    "escaped-quote-in-descr": (
        _b_row(b'(descr "kept")', b'(descr "a\\"b")'),
        "(descr ...) at line 4, column 75 has text after its value",
        "(descr ...) at line 4, column 75",
    ),
    "escaped-quote-in-name": (
        _b_row(b'(name "B")', b'(name "B\\"")'),
        "(name ...) at line 4, column 8 has text after its value",
        "a backslash does not escape a quote",
    ),
    # KiCad has no comments in a lib table: '#' and ';' are ordinary text, here outside any row.
    "comment-line": (
        _fp_table().replace(b"  (version 7)\r\n", b"  (version 7)\r\n# a comment\r\n", 1),
        "'#' at line 3, column 1 stands bare in the table",
        "'#' at line 3, column 1",
    ),
    "semicolon-comment-line": (
        _fp_table().replace(b"  (version 7)\r\n", b"  (version 7)\r\n  ; a comment\r\n", 1),
        "';' at line 3, column 3 stands bare in the table",
        "';' at line 3, column 3",
    ),
    "trailing-comment": (_fp_table() + b"# c\r\n", "text at line 6, column 1 follows the table's closing ')'", "remove the text after"),
    # Fix round 2 (delta review 1, B1): tables round 1 accepted and KiCad 10.0.7 skips.
    "version-twice": (
        _fp_table().replace(b"  (version 7)\r\n", b"  (version 7)\r\n  (version 7)\r\n", 1),
        "a second (version ...) at line 3, column 3",
        "remove the second (version ...)",
    ),
    "version-after-a-row": (
        _fp_table().replace(b"  (version 7)\r\n", b"", 1).replace(_ROW_B, _ROW_B + b"  (version 7)\r\n", 1),
        "(version ...) at line 4, column 3 comes after a (lib ...) row",
        "move (version ...) at line 4, column 3",
    ),
    "quoted-head": (_b_row(b'(name "B")', b'("name" "B")'), "quoted name at line 4, column 9", '("name" ...)'),
    "quoted-flag": (_fp_table(b'("hidden")'), "quoted name at line 3, column 83", '("hidden" ...)'),
    "quoted-version": (_fp_table().replace(b"(version 7)", b'("version" 7)', 1), "quoted name at line 2, column 4", '("version" ...)'),
    "quoted-lib": (_b_row(b"(lib (name", b'("lib" (name'), "quoted name at line 4, column 4", '("lib" ...)'),
    "quoted-root": (_fp_table().replace(b"(fp_lib_table", b'("fp_lib_table"', 1), "quoted name at line 1, column 2", '("fp_lib_table" ...)'),
    "nbsp-separator": (
        _b_row(b'(type "KiCad")(uri', b'(type "KiCad")\xc2\xa0(uri'),
        "'\\xa0' at line 4, column 32 stands bare in a (lib ...) row",
        "'\\xa0' at line 4, column 32",
    ),
    "u2028-separator": (
        _b_row(b'(type "KiCad")(uri', b'(type "KiCad")\xe2\x80\xa8(uri'),
        "'\\u2028' at line 4, column 32 stands bare in a (lib ...) row",
        "'\\u2028' at line 4, column 32",
    ),
    "u0085-separator": (_b_row(b'(type "KiCad")(uri', b'(type "KiCad")\xc2\x85(uri'), "'\\x85' at line 4, column 32", "'\\x85'"),
    "x1c-separator": (_b_row(b'(type "KiCad")(uri', b'(type "KiCad")\x1c(uri'), "'\\x1c' at line 4, column 32", "'\\x1c'"),
    "nul-separator": (_b_row(b'(type "KiCad")(uri', b'(type "KiCad")\x00(uri'), "'\\x00' at line 4, column 32", "'\\x00'"),
    "nbsp-before-close": (
        _b_row(b'(descr "kept")', b'(descr "kept"\xc2\xa0)'),
        "(descr ...) at line 4, column 75 has text after its value",
        "(descr ...) at line 4, column 75",
    ),
    "nbsp-before-the-table-close": (
        _fp_table().replace(b"\r\n)\r\n", b"\r\n\xc2\xa0)\r\n"),
        "'\\xa0' at line 5, column 1 stands bare in the table",
        "'\\xa0' at line 5, column 1",
    ),
    "trailing-nbsp": (_fp_table() + b"\xc2\xa0", "text at line 6, column 1 follows the table's closing ')'", "remove the text after"),
    "whitespace-before-the-table": (b"\r\n" + _fp_table(), "whitespace before the table's opening '('", "remove the whitespace"),
    "whitespace-after-a-field-parenthesis": (
        _b_row(b'(descr "kept")', b'( descr "kept")'),
        "whitespace after the '(' at line 4, column 75",
        "(descr ...) at line 4, column 75",
    ),
    "whitespace-after-the-version-parenthesis": (
        _fp_table().replace(b"(version 7)", b"( version 7)", 1),
        "whitespace after the '(' at line 2, column 3, in (version ...)",
        "(version ...) at line 2, column 3",
    ),
    "whitespace-before-a-field-close": (
        _b_row(b'(descr "kept")', b'(descr "kept"\r\n)'),
        "(descr ...) at line 4, column 75 has whitespace before its ')'",
        "(descr ...) at line 4, column 75",
    ),
    "whitespace-before-the-version-close": (
        _fp_table().replace(b"(version 7)", b"(version 7 )", 1),
        "(version ...) at line 2, column 3 has whitespace before its ')'",
        "(version ...) at line 2, column 3",
    ),
    "whitespace-inside-a-flag": (_fp_table(b"(hidden )"), "(hidden ...) at line 3, column 82", "(hidden ...)"),
    "whitespace-after-the-root-parenthesis": (
        _fp_table().replace(b"(fp_lib_table", b"( fp_lib_table", 1),
        "whitespace after the '(' at line 1, column 1",
        "the table's opening '('",
    ),
    "no-whitespace-after-lib": (_b_row(b"(lib (name", b"(lib(name"), "no whitespace after 'lib' at line 4, column 3", "(lib ...)"),
    "no-whitespace-before-a-value": (
        _b_row(b'(descr "kept")', b'(descr"kept")'),
        "no whitespace between 'descr' and its value at line 4, column 75",
        "(descr ...)",
    ),
    "text-right-after-a-string": (_b_row(b'(descr "kept")', b'(descr "kept"x)'), "has text after its value", "(descr ...)"),
    "space-inside-an-atom-with-a-quote": (_b_row(b'(descr "kept")', b'(descr a"b c")'), "holds more than one value", "(descr ...)"),
    "trailing-text": (_fp_table() + b"x\r\n", "line 6, column 1", "remove the text after"),
    "text-before-the-table": (b"x " + _fp_table(), "line 1, column 1", "remove the text before"),
    "unclosed-string": (_b_row(b'(descr "kept"))', b'(descr "kept))'), "line 4", "closing quote"),
    "disabled-with-a-value": (_fp_table(b"(disabled yes)"), "(disabled ...) at line 3", "(disabled ...)"),
    "hidden-with-a-value": (_fp_table(b"(hidden yes)"), "(hidden ...) at line 3", "(hidden ...)"),
    "descr-with-two-values": (_b_row(b'(descr "kept")', b'(descr "a" "b")'), "(descr ...) at line 4", "(descr ...)"),
    "descr-without-a-value": (_b_row(b'(descr "kept")', b"(descr)"), "(descr ...) at line 4", "(descr ...)"),
    "descr-holding-a-list": (_b_row(b'(descr "kept")', b"(descr (x))"), "(descr ...) at line 4", "(descr ...)"),
    "empty-row": (
        _fp_table().replace(b"  (version 7)\r\n", b"  (version 7)\r\n  (lib)\r\n", 1),
        "(lib ...) at line 3",
        "(lib ...)",
    ),
    "version-without-a-number": (_fp_table().replace(b"(version 7)", b"(version)", 1), "(version ...) at line 2", "(version ...)"),
    "atom-in-the-table": (_fp_table().replace(b"(fp_lib_table\r\n", b"(fp_lib_table foo\r\n", 1), "'foo' at line 1", "'foo'"),
    "atom-in-a-row": (_b_row(b"(lib (name", b"(lib foo (name"), "'foo' at line 4", "'foo'"),
}


@pytest.mark.parametrize("case", sorted(_UNPARSEABLE))
def test_register_refuses_an_unparseable_table_and_leaves_it_byte_identical(tmp_path, case):
    before, problem, fix = _UNPARSEABLE[case]
    table = tmp_path / "fp-lib-table"
    table.write_bytes(before)

    result = _register_new_pretty(tmp_path)

    entry = result["footprint"]
    assert result["status"] == "error"
    assert entry["status"] == "error"
    assert entry["code"] == "lib_table_unparseable"
    assert entry["table"] == str(table.resolve())
    assert problem in entry["parse_error"]
    assert str(table.resolve()) in entry["reason"]
    assert fix in entry["reason"]
    assert "Nothing was written" in entry["reason"]
    # Review round 1, item 3: the agent passes the fix on to the user and never edits the file.
    assert "Ask the user to" in entry["reason"]
    assert "Do not edit this file yourself" in entry["reason"]
    assert ("do not delete" in entry["reason"]) is (case != "empty")
    assert table.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["New.pretty", "fp-lib-table"]  # no temporary left


def test_register_does_not_reuse_the_nickname_of_a_row_without_a_uri(tmp_path):
    table = tmp_path / "fp-lib-table"
    row = b'  (lib (name "New")(type "KiCad")(options "")(descr ""))\r\n'
    table.write_bytes(b"(fp_lib_table\r\n  (version 7)\r\n" + row + b")\r\n")

    result = _register_new_pretty(tmp_path)

    assert result["footprint"]["nickname"] == "New_2"
    assert table.read_bytes() == (
        b"(fp_lib_table\r\n  (version 7)\r\n" + row
        + b'  (lib (name "New_2") (type "KiCad") (uri "${KIPRJMOD}/New.pretty") (options "") (descr ""))\r\n)\r\n'
    )


@pytest.mark.parametrize(
    "corrupt, mismatch",
    [
        (lambda data: data.replace(b"(disabled)", b"", 1), "the bytes before the new row differ"),
        (lambda data: data.replace(b'(descr ""))\r\n)', b'(descr "")\r\n)', 1), "does not parse"),
        (lambda data: data.replace(b'"New"', b'"Nwe"', 1), "not written as built"),
        # Review round 1, item 4: each of these trips one part of the check alone (the same length
        # and the same parse, or the same bytes around the row), so each part is pinned on its own.
        (lambda data: data.replace(b"  (version 7)", b" \t(version 7)", 1), "the bytes before the new row differ"),
        (lambda data: data[: -len(b")\r\n")] + b")\n\n", "the bytes after the new row differ"),
        (lambda data: data.replace(b"(fp_lib_table\r\n ", b"(sym_lib_table\r\n", 1), "root is (sym_lib_table ...)"),
    ],
    ids=[
        "existing-row-loses-its-flag",
        "new-row-unparseable",
        "new-row-altered",
        "prefix-whitespace-same-length",
        "suffix-line-ending-same-length",
        "root-renamed",
    ],
)
def test_register_library_asset_self_check_fires_on_a_corrupted_write(tmp_path, monkeypatch, corrupt, mismatch):
    table = tmp_path / "fp-lib-table"
    before = _fp_table(b"(disabled)")
    table.write_bytes(before)
    real_save = write_layer.save_bytes
    monkeypatch.setattr(
        write_layer, "save_bytes", lambda data, path, *, verify: real_save(corrupt(data), path, verify=verify)
    )

    result = _register_new_pretty(tmp_path)

    entry = result["footprint"]
    assert result["status"] == "error"
    assert entry["code"] == "write_not_verified"
    assert any(mismatch in m for m in entry["mismatches"]), entry["mismatches"]
    assert "was left unchanged" in entry["reason"]
    assert "Report this to the user; do not edit the table yourself." in entry["reason"]
    assert table.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["New.pretty", "fp-lib-table"]  # temporary removed


def _inside_the_last_row(raw: bytes) -> int:
    return raw.rfind(b")", 0, raw.rstrip().rfind(b")"))


def _before_the_last_row(raw: bytes) -> int:
    return raw.rfind(b"\n", 0, raw.rfind(b"(lib")) + 1


@pytest.mark.parametrize(
    "where, mismatch",
    [
        (_inside_the_last_row, "does not parse"),
        (_before_the_last_row, "not the source plus the new row"),
    ],
    ids=["inside-the-last-row", "before-the-last-row"],
)
def test_register_library_asset_self_check_fires_on_a_row_inserted_in_the_wrong_place(
    tmp_path, monkeypatch, where, mismatch
):
    # Review round 1, item 4: a wrong insertion point writes exactly the bytes the byte checks
    # expect, so only the reparse (fix round 2: KiCad's grammar) and its comparison can catch it.
    table = tmp_path / "fp-lib-table"
    before = _fp_table(b"(disabled)")
    table.write_bytes(before)
    real_insert = library_toolset._insert_row

    def wrong_place(source, row):
        _, segment = real_insert(source, row)
        return where(source.raw), segment

    monkeypatch.setattr(library_toolset, "_insert_row", wrong_place)

    result = _register_new_pretty(tmp_path)

    entry = result["footprint"]
    assert entry["code"] == "write_not_verified"
    assert len(entry["mismatches"]) == 1, entry["mismatches"]
    assert mismatch in entry["mismatches"][0]
    assert table.read_bytes() == before


@pytest.mark.parametrize("existed", [True, False], ids=["changed", "created"])
def test_register_refuses_when_the_table_changed_on_disk_after_it_was_read(tmp_path, monkeypatch, existed):
    # Review round 1, item 6: KiCad (or the user) saving the table between the read and the
    # replace must not be overwritten; build ticket 78's retry widens that window by up to 3 s.
    table = tmp_path / "fp-lib-table"
    if existed:
        table.write_bytes(_fp_table())
    saved_meanwhile = _fp_table(b"(hidden)")
    real_save = write_layer.save_bytes

    def kicad_saves_first(data, path, *, verify):
        Path(path).write_bytes(saved_meanwhile)
        return real_save(data, path, verify=verify)

    monkeypatch.setattr(write_layer, "save_bytes", kicad_saves_first)

    result = _register_new_pretty(tmp_path)

    entry = result["footprint"]
    assert entry["code"] == "write_not_verified"
    assert any("changed on disk after" in m for m in entry["mismatches"]), entry["mismatches"]
    assert "Call register_library_asset again" in entry["reason"]
    assert table.read_bytes() == saved_meanwhile
    assert sorted(p.name for p in tmp_path.iterdir()) == ["New.pretty", "fp-lib-table"]


def test_register_writes_the_readable_table_and_refuses_only_the_unparseable_one(tmp_path):
    sym_table = tmp_path / "sym-lib-table"
    sym_before = b"\xef\xbb\xbf(sym_lib_table\r\n  (version 7)\r\n)\r\n"
    sym_table.write_bytes(sym_before)
    fp_table = tmp_path / "fp-lib-table"
    fp_table.write_bytes(_fp_table())
    (tmp_path / "New.kicad_sym").write_bytes(b"")
    (tmp_path / "New.pretty").mkdir()

    tools = _build_and_get_fns(MCPServer("test"), library_toolset)
    result = tools["register_library_asset"](
        str(tmp_path), symbol_lib_path=str(tmp_path / "New.kicad_sym"), footprint_lib_path=str(tmp_path / "New.pretty")
    )

    assert result["status"] == "error"
    assert result["symbol"]["code"] == "lib_table_unparseable"
    assert sym_table.read_bytes() == sym_before
    assert result["footprint"]["status"] == "ok"
    assert fp_table.read_bytes() == b"(fp_lib_table\r\n  (version 7)\r\n" + _ROW_A % b"" + _ROW_B + _NEW_FP_ROW + b")\r\n"


def test_register_reports_an_already_present_row_that_is_disabled_and_keeps_it(tmp_path):
    table = tmp_path / "fp-lib-table"
    before = b'(fp_lib_table\r\n  (version 7)\r\n  (lib (name "New")(type "KiCad")(uri "${KIPRJMOD}/New.pretty")(options "")(descr "")(disabled))\r\n)\r\n'
    table.write_bytes(before)

    result = _register_new_pretty(tmp_path)

    assert result["footprint"]["status"] == "already_present"
    assert result["footprint"]["disabled"] is True
    assert "disabled" in result["footprint"]["note"]
    assert table.read_bytes() == before


def test_register_never_writes_a_table_outside_project_dir(tmp_path):
    # Delta review 1, B2: kicad_tools' find_project_fp_lib_table walks up the directory tree, so on
    # d86c974 the row went into a parent project's fp-lib-table, whose ${KIPRJMOD} is the parent.
    # KiCad reads a project's own table only from the project's directory.
    parent_table = tmp_path / "fp-lib-table"
    parent_table.write_bytes(_fp_table())
    project = tmp_path / "sub"
    project.mkdir()

    result = _register_new_pretty(project)

    assert result["footprint"]["status"] == "ok", result
    assert result["footprint"]["table"] == str(project.resolve() / "fp-lib-table")
    assert parent_table.read_bytes() == _fp_table()
    assert b'(uri "${KIPRJMOD}/New.pretty")' in (project / "fp-lib-table").read_bytes()
    audit = _build_and_get_fns(MCPServer("test"), library_toolset)["check_project_portability"](str(project))
    assert audit["tables_checked"] == [str(project / "fp-lib-table")]


def test_register_reports_a_table_in_use_and_writes_nothing(tmp_path, monkeypatch):
    # Delta review 1, low b: on d86c974 a table another program held open made os.replace fail
    # after build ticket 78's retries, and the PermissionError escaped as a raw exception.
    table = tmp_path / "fp-lib-table"
    table.write_bytes(_fp_table())

    def held_open(data, path, *, verify):
        raise PermissionError(13, "The process cannot access the file", str(path))

    monkeypatch.setattr(write_layer, "save_bytes", held_open)

    result = _register_new_pretty(tmp_path)

    entry = result["footprint"]
    assert result["status"] == "error"
    assert entry["code"] == "lib_table_in_use"
    assert entry["table"] == str(table.resolve())
    assert "The process cannot access the file" in entry["reason"]
    assert "Nothing was written; the file is unchanged." in entry["reason"]
    assert "call register_library_asset again" in entry["reason"]
    assert table.read_bytes() == _fp_table()


def test_register_reports_any_other_failed_replace_and_writes_nothing(tmp_path, monkeypatch):
    # Delta review 1, low b: an OSError other than a sharing denial (a full disk, say) gets its
    # own code, so the caller is not told to close a program that does not exist.
    table = tmp_path / "fp-lib-table"
    table.write_bytes(_fp_table())

    def disk_full(data, path, *, verify):
        raise OSError(28, "No space left on device", str(path))

    monkeypatch.setattr(write_layer, "save_bytes", disk_full)

    result = _register_new_pretty(tmp_path)

    entry = result["footprint"]
    assert result["status"] == "error"
    assert entry["code"] == "lib_table_not_written"
    assert "No space left on device" in entry["reason"]
    assert "Nothing was written; the file is unchanged." in entry["reason"]
    assert "close the program" not in entry["reason"]
    assert table.read_bytes() == _fp_table()


def test_register_reads_each_name_as_kicad_does(tmp_path):
    # Fix round 2: KiCad 10.0.7 reads a lib table string as written (measured: a uri holding
    # '\n' named the file after the backslash), while kicad_tools' reader turned "N\ew" into
    # "New", so the free nickname New was taken for a clash and New_2 written.
    table = tmp_path / "fp-lib-table"
    before = b'(fp_lib_table\r\n  (version 7)\r\n  (lib (name "N\\ew")(type "KiCad")(uri "${KIPRJMOD}/Other.pretty")(options "")(descr ""))\r\n)\r\n'
    table.write_bytes(before)

    result = _register_new_pretty(tmp_path)

    assert result["footprint"]["nickname"] == "New", result
    assert table.read_bytes() == _with_new_row(before)


def test_check_project_portability_reports_an_unparseable_table_as_incomplete(tmp_path):
    table = tmp_path / "fp-lib-table"
    before = b"\xef\xbb\xbf" + _fp_table()
    table.write_bytes(before)

    tools = _build_and_get_fns(MCPServer("test"), library_toolset)
    result = tools["check_project_portability"](str(tmp_path))

    assert result["status"] == "incomplete"
    assert result["tables_checked"] == []
    # Review round 1, item 7: "table" is the path, as in register_library_asset's result.
    assert result["unreadable_tables"][0]["table"] == str(table)
    assert result["unreadable_tables"][0]["kind"] == "fp"
    assert result["unreadable_tables"][0]["code"] == "lib_table_unparseable"
    assert "byte-order mark" in result["unreadable_tables"][0]["parse_error"]
    assert table.read_bytes() == before


def test_check_project_portability_clean_project(tmp_path):
    fp_dir = _make_footprint(tmp_path)
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)
    tools["register_library_asset"](str(tmp_path), footprint_lib_path=str(fp_dir))

    result = tools["check_project_portability"](str(tmp_path))
    assert result["status"] == "ok"
    assert result["violation_count"] == 0


def test_check_project_portability_catches_absolute_path(tmp_path):
    absolute_lib = (tmp_path / "somewhere_else").resolve()
    table_path = tmp_path / "fp-lib-table"
    table_path.write_text(
        f'(fp_lib_table\n\t(version 7)\n\t(lib (name "Bad") (type "KiCad") '
        f'(uri "{absolute_lib.as_posix()}") (options "") (descr ""))\n)\n'
    )

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)
    result = tools["check_project_portability"](str(tmp_path))
    assert result["status"] == "violations_found"
    assert result["violation_count"] == 1
    assert result["violations"][0]["nickname"] == "Bad"


def test_check_project_portability_missing_directory():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)
    result = tools["check_project_portability"]("Z:/definitely/does/not/exist")
    assert result["status"] == "incomplete"


def test_validate_klc_real_generated_footprint_passes(tmp_path):
    fp_dir = _make_footprint(tmp_path)
    fp_path = next(fp_dir.glob("*.kicad_mod"))

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)
    result = tools["validate_klc"](str(fp_path))
    assert result["status"] == "ok", result
    assert result["checks"]["reference_is_REF_placeholder"] is True
    assert result["checks"]["has_courtyard_outline"] is True
    assert result["checks"]["has_fab_layer_outline"] is True


def test_validate_klc_flags_a_real_violation(tmp_path):
    # Take a real, otherwise-valid footprint and corrupt its reference
    # designator -- confirms the checker actually inspects content rather
    # than always returning a pass.
    fp_dir = _make_footprint(tmp_path)
    fp_path = next(fp_dir.glob("*.kicad_mod"))
    text = fp_path.read_text()
    assert '"REF**"' in text
    fp_path.write_text(text.replace('"REF**"', '"U1"'))

    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)
    result = tools["validate_klc"](str(fp_path))
    assert result["status"] == "violations_found"
    assert "reference_is_REF_placeholder" in result["failed_checks"]


def test_validate_klc_missing_file_is_incomplete_not_an_exception():
    mcp = MCPServer("test")
    tools = _build_and_get_fns(mcp, library_toolset)
    result = tools["validate_klc"]("does/not/exist.kicad_mod")
    assert result["status"] == "incomplete"


def test_register_reads_the_last_uri_of_a_row_as_kicad_does(tmp_path):
    # Delta review 2, F1: kicad-cli 10.0.7 keeps the last (uri ...) of a row, so this row is the
    # New.pretty library, nickname B; the mutant that read the first value reported a new row.
    table = tmp_path / "fp-lib-table"
    before = b'(fp_lib_table\r\n  (version 7)\r\n  (lib (name "B")(type "KiCad")(uri "${KIPRJMOD}/other.pretty")(uri "${KIPRJMOD}/New.pretty"))\r\n)\r\n'
    table.write_bytes(before)

    result = _register_new_pretty(tmp_path)

    assert result["footprint"]["status"] == "already_present", result
    assert result["footprint"]["nickname"] == "B"
    assert table.read_bytes() == before


def test_register_refuses_a_row_inside_a_row_without_calling_it_unclosed(tmp_path):
    # Delta review 2, F5: (lib (lib (name "x"))) closes its outer row; the cause is the nested '(lib'.
    table = tmp_path / "fp-lib-table"
    before = b'(fp_lib_table\r\n  (version 7)\r\n  (lib (lib (name "x")))\r\n)\r\n'
    table.write_bytes(before)

    result = _register_new_pretty(tmp_path)

    entry = result["footprint"]
    assert entry["code"] == "lib_table_unparseable"
    assert "no closing" not in entry["parse_error"]
    assert "no closing" not in entry["reason"]
    assert "the second is nested inside it" in entry["parse_error"]
    assert "the (lib ...) row at line 3, column 3 is not closed before the (lib ...) at line 3, column 8" in entry["parse_error"]
    assert table.read_bytes() == before


def test_register_reports_a_read_only_table_apart_from_one_held_open(tmp_path):
    # Delta review 2, F3: a table the user made read-only is not "held open by another program".
    table = tmp_path / "fp-lib-table"
    table.write_bytes(_fp_table())
    os.chmod(table, stat.S_IREAD)
    try:
        result = _register_new_pretty(tmp_path)
    finally:
        os.chmod(table, stat.S_IWRITE | stat.S_IREAD)

    entry = result["footprint"]
    assert entry["code"] == "lib_table_read_only", result
    assert "read-only" in entry["reason"]
    assert "make it writable" in entry["reason"]
    assert "close the program" not in entry["reason"]
    assert "Nothing was written; the file is unchanged." in entry["reason"]
    assert table.read_bytes() == _fp_table()


def test_register_description_does_not_claim_in_use_is_always_another_program():
    # Delta review 3 (F3b): a PermissionError with a writable table (a deny ACL on the
    # folder) gives lib_table_in_use too, so the text must not state the cause flatly.
    mcp = MCPServer("test")
    library_toolset.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == "register_library_asset")
    text = " ".join(tool.description.split())
    assert "lib_table_in_use means the OS refused to replace the table, usually because another program" in text
    assert "a permission problem on the folder gives the same code" in text
    assert "another program held the table open and nothing was written" not in text
