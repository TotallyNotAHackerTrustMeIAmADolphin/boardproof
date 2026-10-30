"""Library registration, dedup, portability, and KLC-subset validation.

archived backlog P1-07. Registers assets produced by P1-05 (`asset_fetch.py`,
easyeda2kicad) or P1-06 (`footprint_gen.py`, kicad_tools generators) into a
project's local `sym-lib-table`/`fp-lib-table`.

kicad_tools has read support for fp-lib-table (`kicad_tools.footprints.
fp_lib_table`) but no write support, and no sym-lib-table module at all. Its
S-expression reader also reads these tables differently from KiCad: it
unescapes backslashes, and it reads '#' and ';' as comments (build ticket 76).
So this module reads both tables with its own `_Reader`, which follows the
grammar KiCad 10.0.7 reads them with (`library_table_grammar.h`, every rule
measured with kicad-cli), and uses kicad_tools only for URI expansion
(`expand_kicad_vars`) and for building the new row.

A registration never rewrites the table (build ticket 76; until then every
registration re-serialised the whole file, dropping `(disabled)`/`(hidden)`,
and a table it could not parse was replaced by one holding only the new
row). It inserts the one new row before the root's closing parenthesis and
keeps every other byte, saved through `write_layer.save_bytes` with a
self-check on the temporary file (`_verify_insertion`), which also refuses
when the table changed on disk after it was read. It writes only the
project's own table in `project_dir`, the one KiCad reads for that project.
A table this module will not edit is refused with `lib_table_unparseable`
and left byte-identical: everything KiCad 10.0.7 skips (a UTF-8 BOM,
anything its grammar rejects, the wrong root), bytes that are not UTF-8 and
an empty file. A table that is a symbolic or hard link is refused with
`lib_table_is_a_link`, since replacing it would cut the link; a table the
OS refuses to replace (usually because another program holds it open; a permission problem on
the folder gives the same code) gives `lib_table_in_use`, a read-only one `lib_table_read_only`. A nickname or library path
holding '"' or '\\' is refused too.

kicad_tools has zero KLC (KiCad Library Convention) support anywhere in
its module tree (checked directly, no hits). No other reasonable
in-process option exists (KiCad's own KLC checker lives in a separate
GitLab repo of lint scripts, not a library). `validate_klc` here is
therefore an explicit MINIMAL SUBSET of KLC -- a handful of objective,
well-documented, cheaply-checkable rules -- not full KLC compliance, and
says so in its own docstring/return so a caller can't mistake a pass here
for "this would pass klc.kicad.org's real checker."

Portability (added after this ticket started, per updated docs/ARCHITECTURE.md
"Library portability" / archived backlog P1-07 acceptance): every registered
entry MUST use a ${KIPRJMOD}-relative URI, never an absolute filesystem
path -- a project's libraries must survive being cloned/moved to another
machine. `register_library_asset` refuses (evidence-gated error, not a
silent absolute-path write) when the asset lives outside the project
directory tree, since there is no honest way to make an external path
project-relative without copying it -- that copy is a deliberate decision
for the caller to make explicitly, not something to do silently as a side
effect of registration. `check_project_portability` audits an existing
project's tables for the absolute-path violation this exists to prevent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, NoReturn

from mcp.server.mcpserver import MCPServer

from kicad_tools.footprints.fp_lib_table import expand_kicad_vars
from kicad_tools.schema import Footprint
from kicad_tools.sexp import parse_string
from kicad_tools.sexp.parser import SExp

from kicad_mcp import write_layer

_TableKind = Literal["sym", "fp"]

_TABLE_FILENAME = {"sym": "sym-lib-table", "fp": "fp-lib-table"}
_TABLE_TAG = {"sym": "sym_lib_table", "fp": "fp_lib_table"}


_UTF8_BOM = b"\xef\xbb\xbf"
_KICAD_INDENT = "  "  # what KiCad 10.0.7 writes before each row of a lib table

# Refusal and self-check codes of register_library_asset (build ticket 76).
LIB_TABLE_UNPARSEABLE = "lib_table_unparseable"
LIB_TABLE_IS_A_LINK = "lib_table_is_a_link"
LIB_TABLE_IN_USE = "lib_table_in_use"
LIB_TABLE_READ_ONLY = "lib_table_read_only"
LIB_TABLE_NOT_WRITTEN = "lib_table_not_written"
WRITE_NOT_VERIFIED = "write_not_verified"
_CHANGED_ON_DISK = "changed on disk after"


class _TableUnreadable(Exception):
    """A lib table this module will not rewrite: ``problem`` says what is wrong with the file,
    ``fix`` what the user has to change; ``may_list_libraries`` is False only for a file that
    holds nothing but whitespace."""

    def __init__(self, path: Path, problem: str, fix: str, *, may_list_libraries: bool = True) -> None:
        super().__init__(f"{path}: {problem}")
        self.path = path
        self.problem = problem
        self.fix = fix
        self.may_list_libraries = may_list_libraries


@dataclass(frozen=True)
class _Row:
    """One ``(lib ...)`` row as KiCad 10.0.7 reads it: its members in file order, each
    ``(key, value)``; a field's value is the text of its token, or of its quoted string between
    the quotes, as written (10.0.7 does not unescape it); a flag's value is None."""

    members: tuple[tuple[str, str | None], ...]

    def field(self, key: str) -> str | None:
        """The value KiCad keeps for ``key``: its parser overwrites, so the last one given."""
        values = [value for name, value in self.members if name == key]
        return values[-1] if values else None


@dataclass(frozen=True)
class _TableText:
    """A lib table's content as KiCad 10.0.7 reads it (``_Reader``)."""

    tag: str
    version: str | None
    rows: tuple[_Row, ...]


@dataclass(frozen=True)
class _Table:
    """A lib table as read: its exact bytes (for a missing table, ``existed`` False, the empty
    table this module would create) and what KiCad reads in them."""

    path: Path
    raw: bytes
    text: str
    read: _TableText
    existed: bool


def _make_lib_entry(name: str, uri: str, *, lib_type: str = "KiCad", options: str = "", descr: str = "") -> SExp:
    lib = SExp.list("lib")
    for field_name, value in (("name", name), ("type", lib_type), ("uri", uri), ("options", options), ("descr", descr)):
        field = SExp.list(field_name)
        field.append(write_layer.string(value))
        lib.append(field)
    return lib


def _find_table_path(project_dir: Path, kind: _TableKind) -> Path:
    """The project's own table: KiCad reads a project's lib tables only from the project's
    directory, the one ``${KIPRJMOD}`` names. Not kicad_tools' ``find_project_fp_lib_table``:
    it walks up the directory tree and returned a parent project's table (delta review 1 of
    build ticket 76), whose ``${KIPRJMOD}`` is the parent."""
    return project_dir / _TABLE_FILENAME[kind]


def _line_col(text: str, position: int) -> str:
    """``line L, column C`` (both from 1) of ``position`` in ``text``; CRLF, LF and CR-only
    line endings each count as one line break."""
    before = text[:position].replace("\r\n", "\n").replace("\r", "\n")
    return f"line {before.count(chr(10)) + 1}, column {len(before) - (before.rfind(chr(10)) + 1) + 1}"


_SKIPS = "KiCad 10.0.7 skips such a table, so none of its libraries load"
# KiCad 10.0.7 reads a lib table with the PEGTL grammar of its library_table_grammar.h, without the
# escape rule that file has on KiCad's master branch (build ticket 76 fix round 2: every rule below
# measured with kicad-cli; a table the grammar rejects is skipped whole).
_TABLE_TYPES = ("sym_lib_table", "fp_lib_table", "design_block_lib_table")
_ROW_FIELDS = ("name", "type", "uri", "options", "descr")  # each with exactly one value
_ROW_FLAGS = ("disabled", "hidden")  # each without a value
_SPACE = " \t\n\r\v\f"  # PEGTL's space: what may stand between tokens; nothing else, no U+00A0
_TOKEN_STOP = "() \t\n\r"  # a token runs until one of these: '"', \v, \f and U+00A0 belong to it
_GRAMMAR = (
    "KiCad reads a lib table only in this form: the table's name right after its '(', at most one "
    "(version N) before the first row, then (lib ...) rows; a row holds (name X), (type X), (uri X), "
    "(options X) and (descr X) with one value each, and (disabled) and (hidden) without one, with no "
    "whitespace after their '(' or before their ')'"
)


class _Reader:
    """Reads a lib table's text as KiCad 10.0.7 does: its grammar rule by rule, a quoted string
    ending at the next '"' (10.0.7 has no escapes). ``table`` returns what KiCad reads, or raises
    ``_TableUnreadable`` at the first text the grammar rejects, naming what and where."""

    def __init__(self, path: Path, text: str) -> None:
        self.path = path
        self.text = text
        self.n = len(text)

    def where(self, pos: int) -> str:
        return _line_col(self.text, pos)

    def refuse(self, problem: str, fix: str) -> NoReturn:
        raise _TableUnreadable(self.path, f"{problem}: {_SKIPS}", fix)

    def refuse_token(self, pos: int, what: str, why: str, hint: str = "") -> NoReturn:
        where = self.where(pos)
        self.refuse(f"{what} at {where} {why}", f"correct or remove {what} at {where}{hint} ({_GRAMMAR})")

    def ends_inside(self, pos: int) -> NoReturn:
        where = self.where(pos)
        self.refuse(f"the file ends before the '(' at {where} is closed", f"add the missing ')' for the '(' at {where}")

    def skip_space(self, i: int) -> int:
        while i < self.n and self.text[i] in _SPACE:
            i += 1
        return i

    def token_end(self, i: int) -> int:
        while i < self.n and self.text[i] not in _TOKEN_STOP:
            i += 1
        return i

    def keyword(self, i: int, words: tuple[str, ...]) -> str | None:
        """The one of ``words`` at ``i``, if space, a parenthesis or the end follows it. KiCad
        matches a keyword as a prefix and then needs one of these, so "(name\\vX)" is a name
        although \\v belongs to a token."""
        for word in words:
            after = i + len(word)
            if self.text.startswith(word, i) and (after >= self.n or self.text[after] in _SPACE + "()"):
                return word
        return None

    def value(self, i: int) -> tuple[int, str, bool]:
        """The value at ``i`` (``i`` holds neither space nor a parenthesis): its end, its text,
        whether it was quoted. A string without its closing quote is refused."""
        if self.text[i] == '"':
            close = self.text.find('"', i + 1)
            if close < 0:
                where = self.where(i)
                self.refuse(
                    f"the string that starts at {where} has no closing quote",
                    f'add the missing closing quote (") to the string at {where}',
                )
            return close + 1, self.text[i + 1 : close], True
        end = self.token_end(i)
        return end, self.text[i:end], False

    def table(self, tag: str) -> _TableText:
        """The whole file, which must be one ``(<tag> ...)`` table and nothing else."""
        text, n = self.text, self.n
        if not text.startswith("("):
            first = self.skip_space(0)
            if first < n and text[first] == "(":
                self.refuse(
                    f"whitespace before the table's opening '(' at {self.where(first)}; KiCad reads a lib table only "
                    "if '(' is its first character",
                    "remove the whitespace before the table's opening '(' (the file must start with it)",
                )
            if text[0] == ")":
                self.refuse(f"a ')' at {self.where(0)} comes before the table's opening '('", "remove that ')'")
            self.refuse(f"text at {self.where(0)} comes before the table's opening '('", "remove the text before the table")
        name = text[1 : self.token_end(1)]
        if 1 < n and text[1] in _SPACE:
            self.refuse(
                f"whitespace after the '(' at {self.where(0)} that opens the table; KiCad accepts none there",
                f"remove the whitespace after the table's opening '(' at {self.where(0)}",
            )
        if name.startswith('"'):
            self.refuse_quoted(1, name)
        if self.keyword(1, _TABLE_TYPES) != tag:
            self.wrong_root(name, tag)
        i = self.skip_space(1 + len(tag))
        version: str | None = None
        rows: list[_Row] = []
        while i < n and text[i] == "(":
            j = self.skip_space(i + 1)
            if j == i + 1 and self.keyword(j, ("version",)):
                if rows:
                    where = self.where(i)
                    self.refuse(
                        f"(version ...) at {where} comes after a (lib ...) row; KiCad accepts it only before the first row",
                        f"move (version ...) at {where} before the first (lib ...) row ({_GRAMMAR})",
                    )
                if version is not None:
                    where = self.where(i)
                    self.refuse(
                        f"a second (version ...) at {where}; KiCad accepts only one",
                        f"remove the second (version ...) at {where}",
                    )
                i, version = self.field(i, "version")
            elif text.startswith("lib", j):
                i, row = self.row(i, j)
                rows.append(row)
            else:
                self.not_a_member(i, "a lib table")
            i = self.skip_space(i)
        if i >= n:
            self.ends_inside(0)
        if text[i] != ")":
            self.refuse_token(i, repr(self.bare(i)), "stands bare in the table")
        close = i
        i = self.skip_space(i + 1)
        if i < n:
            where, end = self.where(i), self.where(close)
            if text[i] == "(":
                self.refuse(
                    f"the ')' at {end} closes the table, and more rows follow it at {where}",
                    f"remove the extra ')' that closes the table early (at {end} or before it), so the rows "
                    "after it are inside the table again",
                )
            self.refuse(
                f"text at {where} follows the table's closing ')' at {end}",
                f"remove the text after the table's last ')' (from {where})",
            )
        return _TableText(tag, version, tuple(rows))

    def bare(self, i: int) -> str:
        """The token or quoted string at ``i``, for a message."""
        if self.text[i] == '"':
            close = self.text.find('"', i + 1)
            return self.text[i : close + 1] if close >= 0 else self.text[i : self.token_end(i)]
        return self.text[i : self.token_end(i)]

    def wrong_root(self, name: str, tag: str) -> NoReturn:
        found = f"({name} ...)" if name else "a table without a name"
        raise _TableUnreadable(
            self.path,
            f"its root is {found}, not ({tag} ...)",
            f"check that the right file is in this place: {self.path.name} must hold a ({tag} ...) table",
        )

    def refuse_quoted(self, pos: int, name: str) -> NoReturn:
        what, where = f"({name} ...)", self.where(pos)
        self.refuse(
            f"a quoted name at {where}, in {what}; KiCad reads the name of a list only unquoted",
            f"correct or remove {what} at {where} ({_GRAMMAR})",
        )

    def field(self, i: int, key: str) -> tuple[int, str]:
        """The ``(key value)`` at ``i``, ``key`` right after the '(': its end and its value."""
        text, n, what = self.text, self.n, f"({key} ...)"
        k = i + 1 + len(key)
        s = self.skip_space(k)
        if s >= n:
            self.ends_inside(i)
        if text[s] == ")":
            self.refuse_token(i, what, "does not hold a value")
        if text[s] == "(":
            self.refuse_token(i, what, "holds a list, not a value")
        end, value, quoted = self.value(s)
        if end >= n:
            self.ends_inside(i)
        if text[end] == ")":
            return end + 1, value
        if text[end] in _SPACE:
            after = self.skip_space(end)
            if after < n and text[after] == ")":
                self.refuse_token(i, what, "has whitespace before its ')'; KiCad accepts none there")
            self.refuse_token(i, what, "holds more than one value")
        hint = ""
        if quoted and value.endswith("\\"):
            hint = "; KiCad's string ends at the first '\"' after its opening one: a backslash does not escape a quote"
        self.refuse_token(i, what, f"has text after its value, at {self.where(end)}", hint)

    def row(self, i: int, j: int) -> tuple[int, _Row]:
        """The ``(lib ...)`` row whose '(' is at ``i`` and whose ``lib`` is at ``j``."""
        text, n, where = self.text, self.n, self.where(i)
        k = j + 3
        if k >= n:
            self.ends_inside(i)
        if text[k] not in _SPACE:
            if text[k] == ")":
                self.refuse_token(i, "(lib ...)", "is an empty row")
            if text[k] == "(":
                self.refuse(
                    f"no whitespace after 'lib' at {where}; KiCad needs at least one before the row's first field",
                    f"correct or remove (lib ...) at {where} ({_GRAMMAR})",
                )
            self.not_a_member(i, "a lib table")
        m = self.skip_space(k)
        members: list[tuple[str, str | None]] = []
        while True:
            if m >= n:
                self.refuse(
                    f"the (lib ...) row at {where} has no closing ')'",
                    f"add the missing ')' at the end of the row at {where}",
                )
            if text[m] == ")":
                if not members:
                    self.refuse_token(i, "(lib ...)", "is an empty row")
                return m + 1, _Row(tuple(members))
            if text[m] != "(":
                self.refuse_token(m, repr(self.bare(m)), "stands bare in a (lib ...) row")
            head = self.keyword(m + 1, _ROW_FIELDS + _ROW_FLAGS)
            if head in _ROW_FIELDS:
                end, value = self.field(m, head)
                members.append((head, value))
            elif head in _ROW_FLAGS and text.startswith(f"({head})", m):
                end = m + len(head) + 2
                members.append((head, None))
            elif head in _ROW_FLAGS:
                self.refuse_token(m, f"({head} ...)", f"holds a value or whitespace; KiCad accepts it only as ({head})")
            elif self.keyword(self.skip_space(m + 1), ("lib",)):
                self.refuse(
                    f"the (lib ...) row at {where} is not closed before the (lib ...) at {self.where(m)}: either "
                    "the first row lacks its ')' or the second is nested inside it; a row holds only fields and "
                    "flags, never another row",
                    f"check the row at {where}: add its missing ')', or remove the nested (lib ...) at "
                    f"{self.where(m)}",
                )
            else:
                self.not_a_member(m, "a (lib ...) row")
            m = self.skip_space(end)

    def not_a_member(self, i: int, place: str) -> NoReturn:
        """Refuse the list at ``i``, which is none of the lists KiCad accepts in ``place``."""
        j = self.skip_space(i + 1)
        head = self.text[j : self.token_end(j)]
        if j >= self.n:
            self.ends_inside(i)
        if head.startswith('"'):
            self.refuse_quoted(j, head)
        if j > i + 1:
            what, where = f"({head} ...)", self.where(i)
            self.refuse(
                f"whitespace after the '(' at {where}, in {what}; KiCad accepts none there",
                f"correct or remove {what} at {where} ({_GRAMMAR})",
            )
        for key in (*_ROW_FIELDS, "version"):
            if head.startswith(key + '"'):
                where = self.where(i)
                self.refuse(
                    f"no whitespace between '{key}' and its value at {where}",
                    f"correct or remove ({key} ...) at {where} ({_GRAMMAR})",
                )
        what = f"({head} ...)" if head else "a list without a name"
        self.refuse_token(i, what, f"is not a token KiCad knows in {place}")


def _read_table(path: Path, kind: _TableKind) -> _Table:
    """The table at ``path`` as bytes and as KiCad reads them, or ``_TableUnreadable`` if this
    module will not edit it. A missing table reads as the empty table (``(version 7)``, no
    rows), in the line ending KiCad writes on this platform.

    Refused, never repaired: a UTF-8 byte-order mark, bytes that are not UTF-8, an empty file,
    and everything KiCad 10.0.7's grammar rejects (``_Reader``), a root other than
    ``(<kind>_lib_table ...)`` included. KiCad 10.0.7 skips every one of these tables except
    the non-UTF-8 one (measured with kicad-cli, build ticket 76)."""
    tag = _TABLE_TAG[kind]
    if not path.is_file():
        root = SExp.list(tag)
        version = SExp.list("version")
        version.append(write_layer.number(7))
        root.append(version)
        text = (write_layer.serialise(root) + "\n").replace("\n", os.linesep)
        return _Table(path, text.encode("utf-8"), text, _Reader(path, text).table(tag), existed=False)
    raw = path.read_bytes()
    if raw.startswith(_UTF8_BOM):
        raise _TableUnreadable(
            path,
            f"a UTF-8 byte-order mark at line 1, column 1 (bytes EF BB BF), before the table's opening '(': {_SKIPS}",
            "re-save it as UTF-8 without a byte-order mark",
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        offset = exc.args[2]  # UnicodeDecodeError(encoding, object, start, end, reason)
        before = raw[:offset].decode("utf-8")
        raise _TableUnreadable(
            path,
            f"byte 0x{raw[offset]:02X} at {_line_col(before, len(before))} is not UTF-8; KiCad 10.0.7 still loads "
            "this table, but register_library_asset edits only UTF-8 tables",
            "re-save it as UTF-8 (KiCad writes its tables as UTF-8)",
        ) from exc
    if not text.strip(_SPACE):
        raise _TableUnreadable(
            path,
            f"the file is empty (it holds no ({tag} ...) table)",
            "delete this empty file, which lists no libraries (register_library_asset then creates a new table)",
            may_list_libraries=False,
        )
    return _Table(path, raw, text, _Reader(path, text).table(tag), existed=True)


def _entries(table: _Table) -> list[dict]:
    """Return [{"name", "type", "uri", "options", "descr", "disabled", "hidden",
    "resolved_path"}] for every row with a name and a uri, each value as KiCad 10.0.7 reads it
    (``_Reader``; kicad_tools' reader unescapes backslashes and reads '#' as a comment, which
    KiCad does not). Only for reading: a registration never rewrites a row (``_insert_row``)."""
    kiprjmod = table.path.parent.resolve()
    entries = []
    for row in table.read.rows:
        fields = {key: row.field(key) or "" for key in _ROW_FIELDS}
        flags = {flag: (flag, None) in row.members for flag in _ROW_FLAGS}
        if not fields["name"] or not fields["uri"]:
            continue
        resolved = expand_kicad_vars(fields["uri"], kiprjmod) if fields["type"] == "KiCad" else None
        entries.append({**fields, **flags, "resolved_path": resolved})
    return entries


def _parse_table(path: Path, kind: _TableKind) -> list[dict]:
    """The rows of the table at ``path`` (``_entries``); [] if it does not exist. Raises
    ``_TableUnreadable`` for a table that exists but cannot be parsed, never returns [] for it
    (build ticket 76: that [] made the registration replace the user's table)."""
    if not path.is_file():
        return []
    return _entries(_read_table(path, kind))


def _insert_row(table: _Table, row: SExp) -> tuple[int, bytes]:
    """Where the new row goes and its bytes: on its own line before the root's closing
    parenthesis, in the table's own line ending and row indent. Every other byte of the file
    stays as it was, so a row this tool does not add keeps every token, (disabled) and
    (hidden) included, byte for byte. A CR-only table gets CR (KiCad 10.0.7 loads one)."""
    text = table.text
    eol = next((e for e in ("\r\n", "\n", "\r") if e in text), os.linesep)
    indent = _KICAD_INDENT
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        if stripped.startswith(("(lib", "(version")):
            indent = line[: len(line) - len(stripped)] or _KICAD_INDENT
    close = len(text.rstrip(_SPACE)) - 1
    line_start = max(text.rfind("\n", 0, close), text.rfind("\r", 0, close)) + 1
    row_text = indent + write_layer.serialise(row, compact=True) + eol
    if text[line_start:close].strip(" \t"):
        at, segment = close, eol + row_text  # the closing parenthesis shares its line with a row
    else:
        at, segment = line_start, row_text
    return len(text[:at].encode("utf-8")), segment.encode("utf-8")


class _NotVerified(Exception):
    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


def _verify_insertion(temp: Path, table: _Table, at: int, segment: bytes, row: SExp) -> list[str]:
    """The self-check (decision 4) on the temporary file, just before it replaces the table:
    the table on disk is still the one read (nothing saved it meanwhile), the temporary file's
    bytes are the source's bytes with exactly ``segment`` inserted at ``at``, and KiCad 10.0.7
    reads it (``_Reader``) as the same table with ``row`` as its last row, member for member in
    order. Returns the mismatches; empty means verified.

    It runs once, before ``write_layer``'s move and the retries of a Windows sharing denial
    that build ticket 78 added to it: a save by another program while the move is retried
    (up to about 3 s, only while that program holds the table open) is not seen here."""
    problems = []
    try:
        on_disk: bytes | None = table.path.read_bytes()
    except FileNotFoundError:
        on_disk = None
    if on_disk != (table.raw if table.existed else None):
        problems.append(
            f"{table.path.name} {_CHANGED_ON_DISK} register_library_asset read it (KiCad or another program "
            "saved it meanwhile)"
        )
    written = temp.read_bytes()
    end = at + len(segment)
    if written[:at] != table.raw[:at]:
        problems.append(f"the bytes before the new row differ from {table.path.name}'s")
    if written[at:end] != segment:
        problems.append("the new row was not written as built")
    if written[end:] != table.raw[at:]:
        problems.append(f"the bytes after the new row differ from {table.path.name}'s")
    try:
        reread = _Reader(table.path, written.decode("utf-8")).table(table.read.tag)
    except (UnicodeDecodeError, _TableUnreadable) as exc:
        reason = exc.problem if isinstance(exc, _TableUnreadable) else f"it is not UTF-8 ({exc})"
        return problems + [f"the written table does not parse as KiCad 10.0.7 reads it: {reason}"]
    new = tuple((field.name, field.get_string(0)) for field in row.children)
    want = [table.read.version] + [r.members for r in table.read.rows] + [new]
    have = [reread.version] + [r.members for r in reread.rows]
    if have != want:
        missing = [m for m in want if m not in have]
        unexpected = [m for m in have if m not in want]
        problems.append(
            f"the reloaded table is not the source plus the new row (missing: {missing[:5]}, "
            f"unexpected: {unexpected[:5]})"
        )
    return problems


def _write_table(table: _Table, row: SExp) -> list[str]:
    """Insert ``row`` into ``table`` through ``write_layer.save_bytes``: a temporary sibling,
    verified by ``_verify_insertion``, moved onto the table only on a match. Returns the
    mismatches; on any the table is untouched and the temporary file removed. An ``OSError``
    (the table held open past the retries, a full disk) propagates, the table untouched.

    Not ``write_layer.save``: it has no lib-table kind (kicad-cli has no parse check for a lib
    table) and re-serialises the whole tree, which would re-lay every row. The new row itself
    is built from the layer's typed atoms and serialised by it."""
    at, segment = _insert_row(table, row)

    def verify(temp: Path) -> None:
        problems = _verify_insertion(temp, table, at, segment, row)
        if problems:
            raise _NotVerified(problems)

    try:
        write_layer.save_bytes(table.raw[:at] + segment + table.raw[at:], table.path, verify=verify)
    except _NotVerified as exc:
        return exc.problems
    return []


def _link(path: Path) -> _TableUnreadable | None:
    """Why the table at ``path`` must not be replaced although it may parse: it is a symbolic
    or hard link, and ``os.replace`` would put a plain file in its place, cutting the project
    off from the table it shares (review round 1 of build ticket 76). None if it is neither."""
    if path.is_symlink():
        try:
            target = f" to {os.readlink(path)}"
        except OSError:
            target = ""
        what = f"it is a symbolic link{target}"
    elif path.is_file() and path.stat().st_nlink > 1:
        what = f"it is a hard link (the same file has {path.stat().st_nlink} names)"
    else:
        return None
    return _TableUnreadable(
        path,
        f"{what}, and replacing it would put a plain file in its place and cut this project off from the "
        "table it shares",
        "add the library through KiCad's library manager, or replace the link with a plain copy of the table "
        "if this project should keep its own",
    )


def _refusal(code: str, table_path: Path, exc: _TableUnreadable) -> str:
    """The reason of a table this module refuses to edit: what is wrong, that nothing was
    written, what the user has to do, and that the agent must not touch the file."""
    return (
        f"{code}: {table_path} cannot be edited: {exc.problem}. Nothing was written; the file is unchanged. "
        f"Ask the user to {exc.fix}, then call register_library_asset again. Do not edit this file yourself"
        + (
            "; do not delete, recreate or rewrite it either: it lists the user's other libraries."
            if exc.may_list_libraries
            else "."
        )
    )


def _not_replaced(table_path: Path, exc: OSError) -> dict:
    """The result when the verified table could not be moved into place (or its temporary
    file not written): ``write_layer`` left the table untouched. A ``PermissionError`` is
    the OS refusing the replace (usually another program holding the table open, or a permission
    problem on the folder), after build
    ticket 78's retries (delta review 1 of build ticket 76: it escaped as a raw exception)."""
    notes = "".join(f"; {note}" for note in getattr(exc, "__notes__", []))
    if isinstance(exc, PermissionError) and table_path.exists() and not os.access(table_path, os.W_OK):
        code, cause, ask = (
            LIB_TABLE_READ_ONLY,
            "the file is read-only",
            "make it writable (clear its read-only attribute or fix its permissions)",
        )
    elif isinstance(exc, PermissionError):
        code, cause, ask = (
            LIB_TABLE_IN_USE,
            "another program (KiCad's library manager, an editor, a sync or backup tool) probably holds it open",
            "close the program that holds it open",
        )
    else:
        code, cause, ask = (
            LIB_TABLE_NOT_WRITTEN,
            "the file system refused the write",
            "check the disk and the file's permissions",
        )
    return {
        "status": "error",
        "code": code,
        "table": str(table_path),
        "reason": (
            f"{code}: {table_path} could not be replaced ({exc}{notes}): {cause}. Nothing was written; the file "
            f"is unchanged. Ask the user to {ask}, then call register_library_asset again. "
            "Do not edit this file yourself."
        ),
    }


def _register_one(project_dir: Path, asset_path: Path, kind: _TableKind, nickname: str | None) -> dict:
    project_dir = project_dir.resolve()
    asset_path = asset_path.resolve()

    try:
        relative = asset_path.relative_to(project_dir)
    except ValueError:
        return {
            "status": "error",
            "reason": (
                f"'{asset_path}' is outside the project directory '{project_dir}' -- refusing to register it "
                "with an absolute path (this project's libraries must stay portable across machines, see "
                "docs/ARCHITECTURE.md). Copy the asset under the project directory first, then register the copy."
            ),
        }

    uri = f"${{KIPRJMOD}}/{relative.as_posix()}"
    # A nickname is one token of a lib id: refuse any line break instead of writing it
    # (build ticket 25). A '"' cannot be written so that KiCad 10.0.7 reads it: it ends a string
    # at the next quote and does not unescape, so (name "a\"b") makes it skip the whole table. It
    # reads a '\' as written, but the grammar on KiCad's development (master) branch reads it as the
    # start of an escape, so a '\' would likely not survive the next KiCad release (build ticket 76).
    for field, text in (("nickname", nickname or ""), ("library path", uri)):
        if "\n" in text or "\r" in text:
            return {"status": "error", "reason": f"line break in {field} {text!r}: it must be one line; nothing was written"}
        if '"' in text or "\\" in text:
            return {
                "status": "error",
                "reason": (
                    f"{field} {text!r} holds a '\"' or '\\': KiCad 10.0.7 cannot read a '\"' in a lib table "
                    "string, and KiCad's development branch reads '\\' as the start of an escape; nothing was "
                    "written"
                ),
            }
    table_path = _find_table_path(project_dir, kind)
    link = _link(table_path)
    if link is not None:
        return {
            "status": "error",
            "code": LIB_TABLE_IS_A_LINK,
            "table": str(table_path),
            "reason": _refusal(LIB_TABLE_IS_A_LINK, table_path, link),
        }
    try:
        table = _read_table(table_path, kind)
    except _TableUnreadable as exc:
        return {
            "status": "error",
            "code": LIB_TABLE_UNPARSEABLE,
            "table": str(table_path),
            "parse_error": exc.problem,
            "reason": _refusal(LIB_TABLE_UNPARSEABLE, table_path, exc),
        }
    entries = _entries(table)

    for e in entries:
        if e["resolved_path"] is not None and e["resolved_path"] == asset_path:
            result = {"status": "already_present", "nickname": e["name"], "uri": e["uri"], "table": str(table_path)}
            if e["disabled"]:
                result["disabled"] = True
                result["note"] = (
                    f"this library's row in {table_path} is disabled, so KiCad does not load it; the row was "
                    "left as it is. The user can enable it in KiCad's library manager."
                )
            return result

    requested_nickname = nickname or asset_path.stem
    # Every row's name, also of a row _entries skips (no uri): KiCad keys libraries by nickname.
    existing_names = {value for row in table.read.rows for key, value in row.members if key == "name"}
    final_nickname = requested_nickname
    suffix = 2
    while final_nickname in existing_names:
        final_nickname = f"{requested_nickname}_{suffix}"
        suffix += 1

    try:
        row = _make_lib_entry(final_nickname, uri)
    except write_layer.InvalidStringError as exc:
        return {"status": "error", "reason": f"{exc}; nothing was written"}
    try:
        problems = _write_table(table, row)
    except OSError as exc:
        return _not_replaced(table_path, exc)
    if problems:
        return {
            "status": "error",
            "code": WRITE_NOT_VERIFIED,
            "table": str(table_path),
            "mismatches": problems,
            "reason": (
                f"{WRITE_NOT_VERIFIED}: the new {table_path.name} did not verify ({'; '.join(problems)}); "
                f"{table_path} was left unchanged. "
                + (
                    "Call register_library_asset again: it reads the table anew."
                    if any(_CHANGED_ON_DISK in m for m in problems)
                    else "Report this to the user; do not edit the table yourself."
                )
            ),
        }

    result = {"status": "ok", "nickname": final_nickname, "uri": uri, "table": str(table_path)}
    if final_nickname != requested_nickname:
        result["note"] = f"requested nickname '{requested_nickname}' was already taken by a different path; used '{final_nickname}'"
    return result


def build(mcp: MCPServer) -> list[str]:
    def register_library_asset(
        project_dir: str,
        symbol_lib_path: str | None = None,
        footprint_lib_path: str | None = None,
        nickname: str | None = None,
    ) -> dict:
        """Register a symbol library (.kicad_sym) and/or footprint library
        (.pretty directory) into the project's local sym-lib-table/
        fp-lib-table, deduped by resolved path. Both paths must live under
        project_dir -- entries are always written as ${KIPRJMOD}-relative
        URIs, never absolute, so the project stays portable across
        machines. Pass whichever of symbol_lib_path/footprint_lib_path is
        relevant; at least one is required.
        """
        if symbol_lib_path is None and footprint_lib_path is None:
            return {"status": "error", "reason": "at least one of symbol_lib_path/footprint_lib_path is required"}

        project_path = Path(project_dir)
        out: dict = {}
        if symbol_lib_path is not None:
            out["symbol"] = _register_one(project_path, Path(symbol_lib_path), "sym", nickname)
        if footprint_lib_path is not None:
            out["footprint"] = _register_one(project_path, Path(footprint_lib_path), "fp", nickname)

        statuses = {v["status"] for v in out.values()}
        out["status"] = "error" if "error" in statuses else ("ok" if "ok" in statuses else "already_present")
        return out

    def check_project_portability(project_dir: str) -> dict:
        """Audit a project's sym-lib-table/fp-lib-table for entries that
        would break if the project were moved to another machine: any URI
        that is a raw absolute filesystem path rather than a ${KIPRJMOD}-
        or KiCad-env-var-relative reference. Evidence-gated: reports which
        tables could/couldn't be read, never silently skips a missing or
        unparseable table.
        """
        project_path = Path(project_dir)
        if not project_path.is_dir():
            return {"status": "incomplete", "reason": f"'{project_dir}' is not a directory"}

        violations = []
        tables_checked = []
        unreadable_tables = []
        for kind in ("sym", "fp"):
            table_path = _find_table_path(project_path, kind)
            if not table_path.is_file():
                continue
            try:
                entries = _parse_table(table_path, kind)
            except _TableUnreadable as exc:
                unreadable_tables.append(
                    {"table": str(table_path), "kind": kind, "code": LIB_TABLE_UNPARSEABLE, "parse_error": exc.problem}
                )
                continue
            tables_checked.append(str(table_path))
            for e in entries:
                uri = e["uri"]
                is_relative_ref = uri.startswith("${") or uri.startswith("file://${")
                if not is_relative_ref:
                    violations.append(
                        {
                            "table": kind,
                            "table_path": str(table_path),
                            "nickname": e["name"],
                            "uri": uri,
                            "reason": "absolute path, not ${KIPRJMOD}- or env-var-relative",
                        }
                    )

        result = {
            "status": "incomplete" if unreadable_tables else ("ok" if not violations else "violations_found"),
            "tables_checked": tables_checked,
            "violation_count": len(violations),
            "violations": violations,
        }
        if unreadable_tables:
            result["unreadable_tables"] = unreadable_tables
            result["reason"] = (
                f"{len(unreadable_tables)} lib table(s) cannot be parsed and were not audited; see unreadable_tables"
            )
        return result

    def validate_klc(footprint_path: str) -> dict:
        """Check a .kicad_mod footprint against a MINIMAL SUBSET of the
        KiCad Library Convention -- NOT full KLC compliance (kicad_tools
        has no KLC support at all, and no other in-process checker was
        available; this covers only a few objective, cheaply-checkable
        rules). Checked: reference designator is the literal placeholder
        "REF**" (KLC G1.1), a Value field is present, a component
        attribute (smd/through_hole) is declared, and a courtyard outline
        (F.CrtYd/B.CrtYd) and fab-layer outline (F.Fab/B.Fab) both exist.
        NOT checked: pad sizing/spacing against IPC-7351 tables, silkscreen
        clearance, 3D model association, naming-convention rules, and
        everything else in the real KLC document.
        """
        path = Path(footprint_path)
        if not path.is_file():
            return {"status": "incomplete", "reason": f"file not found: {footprint_path}"}

        try:
            node = parse_string(path.read_text(encoding="utf-8"))
            fp = Footprint.from_sexp(node)
        except Exception as exc:  # noqa: BLE001 -- evidence-gated
            return {"status": "incomplete", "reason": f"failed to parse footprint: {exc}"}

        checks = {}
        reference_text = getattr(fp.reference, "text", fp.reference) if fp.reference is not None else None
        checks["reference_is_REF_placeholder"] = reference_text == "REF**"

        value_text = getattr(fp.value, "text", fp.value) if fp.value is not None else None
        checks["has_value_field"] = bool(value_text)

        checks["has_component_attribute"] = fp.attr in ("smd", "through_hole")

        layers = {getattr(g, "layer", None) for g in (fp.graphics or [])}
        checks["has_courtyard_outline"] = bool(layers & {"F.CrtYd", "B.CrtYd"})
        checks["has_fab_layer_outline"] = bool(layers & {"F.Fab", "B.Fab"})

        failed = [name for name, passed in checks.items() if not passed]
        return {
            "status": "ok" if not failed else "violations_found",
            "checks": checks,
            "failed_checks": failed,
            "scope": "minimal subset only -- see validate_klc's docstring for exactly what is and isn't checked",
        }

    def propose_adopt_parts(
        pcb_path: str,
        parts: list[dict],
        library_name: str = "project_parts",
        library_dir: str = "lib",
        license: str | None = None,
        source: str | None = None,
        source_version: str | None = None,
        output_dir: str | None = None,
    ) -> dict:
        from kicad_mcp import part_adoption

        return part_adoption.propose_adopt_parts(
            pcb_path, parts, library_name, library_dir, license, source, source_version, output_dir
        )

    def confirm_adopt_parts(pcb_path: str, revision_token: str) -> dict:
        from kicad_mcp import part_adoption

        result = part_adoption.confirm_adopt_parts(pcb_path, revision_token)
        output = result.get("output_pcb")
        if isinstance(output, str):  # the gate field marks the source and the revision
            return {**result, "pcb_path": output, "source_pcb": pcb_path}
        return result

    mcp.add_tool(
        register_library_asset,
        name="register_library_asset",
        description=(
            "Register a symbol/footprint library into the project's local sym-lib-table/fp-lib-table, "
            "deduped by real path, always as a ${KIPRJMOD}-relative URI (never absolute) so the project "
            "stays portable across machines. Both symbol_lib_path/footprint_lib_path must live under project_dir. "
            "Adds one row and leaves every existing row byte-for-byte as it was, (disabled) and (hidden) "
            "included; it writes only project_dir's own table, the one KiCad reads for that project. A table "
            "KiCad 10.0.7 would skip, or one this tool cannot edit exactly, is never written: that table's "
            "result is status error with code lib_table_unparseable (lib_table_is_a_link for a linked table), "
            "naming the file, the problem (with its line and column where it has one) and what to ask the "
            "user; pass that on to the user and do not edit, delete or recreate the table yourself. Code "
            "lib_table_in_use means the OS refused to replace the table, usually because another program "
            "(KiCad) holds it open; a permission problem on the folder gives the same code. Nothing was "
            "written: ask the user to close the program and check the folder's permissions, then call "
            "again. Code lib_table_read_only means the table is read-only and "
            "nothing was written: ask the user to make it writable, then call again."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        check_project_portability,
        name="check_project_portability",
        description="Audit a project's library tables for absolute (non-portable) paths that would break if the project were moved to another machine.",
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        validate_klc,
        name="validate_klc",
        description="Check a footprint against a minimal, explicitly-partial subset of the KiCad Library Convention (see docstring for exact scope -- not full KLC compliance).",
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_adopt_parts,
        name="propose_adopt_parts",
        description=(
            "Dry run of adopting a LIST of parts (symbol, footprint, 3D model) into a project-local library "
            "(default lib/project_parts.kicad_sym, .pretty and .3dshapes next to the project): reads every "
            "file, lists per part what it would add and every problem, writes nothing and returns a "
            "revision_token. One rejected part rejects the whole call (status blocked, no token). pcb_path is "
            "the project's board. Each part: {lcsc_id, symbol_path, footprint_path or footprint_dir, "
            "model_path or model_3d_dir, datasheet?, source?, source_version?, license?}: pass what "
            "fetch_part_assets returned (symbol_path, footprint_dir, model_3d_dir): one symbol per file, "
            "easyeda2kicad as source. Not resolve_jlcpcb_assets output: its symbol_path is a many-symbol "
            "library (symbol_name picks one) and is rejected. "
            "A part with no 3D model is not adopted. license is required (per part or for the call): state "
            "the licence the USER gives you, never guess one. source and source_version default to "
            "easyeda2kicad and its installed version. The mandatory symbol properties are Footprint, "
            "Datasheet, LCSC, Source (name and version) and License; a part missing one is rejected. "
            "Nothing is overwritten: a symbol or footprint name already in the library for another part, or "
            "a model file with other content, rejects the part; a part whose LCSC id is already in the "
            "library is reported already_adopted and skipped. Then call confirm_adopt_parts with the token."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
    )
    mcp.add_tool(
        confirm_adopt_parts,
        name="confirm_adopt_parts",
        description=(
            "Apply a propose_adopt_parts proposal: writes a NEW project revision (output_pcb; the source is "
            "never touched) holding the library, both lib-table rows (${KIPRJMOD}-relative), model paths "
            "rewritten to ${KIPRJMOD}/..., and the mandatory properties set. Re-read from a fresh reload, then "
            "checked per part: model_path_project_relative, model_file_present, mandatory_properties and "
            "side_view_render (kicad-cli pcb render --side front, with and without the model: the model must "
            "rise above the board's top face and not sink below its bottom face). Status adopted means every "
            "check passed; failed means a check failed; incomplete means a check could not run (for example "
            "the render): neither moves a revision into place, and incomplete is never to be reported as a "
            "pass. The render check sees the height profile only: a model shifted sideways or rotated about the "
            "vertical axis is not caught by it, so tell the user to look at the front and top images in the "
            "revision's adoption_evidence folder before relying on the 3D model. It does not judge whether the "
            "model is the right part. The revision_token is single use and is refused with an error if the "
            "project or a part file changed since the proposal."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False, "openWorldHint": False},
    )
    return ["register_library_asset", "check_project_portability", "validate_klc", "propose_adopt_parts", "confirm_adopt_parts"]
