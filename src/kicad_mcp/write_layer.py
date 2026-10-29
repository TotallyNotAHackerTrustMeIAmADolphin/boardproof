r"""The write layer: every KiCad file this project writes is saved here (build ticket 42).

docs/ARCHITECTURE.md decision 1: ``kicad_tools`` parses, and its low-level ``SExp`` tree is the
model, but serialising and saving are ours, because ``kicad_tools``' own writers lose data
silently or fail:

- a string set through ``SExp.set_value`` that looks numeric is written bare (``100``), and
  KiCad refuses the file;
- ``\r`` inside a string is written raw, and ``kicad_tools`` reads it back as ``\n``;
- a new float is written with 6 significant digits (``%.6g``: 1234.56789 becomes 1234.57),
  and ``-0.0`` becomes ``0``;
- 0.23.0's ``PCB.save`` cannot save on Windows (``OSError [Errno 9]``, upstream #7478).

What this module gives callers:

- **Serialising:** ``serialise(node)`` writes the tree the way KiCad reads it. Strings are
  quoted unless they were read bare (an enum keyword such as ``smd``), or are a keyword
  ``kicad_tools`` itself writes bare; a string that looks like a number is always quoted.
  Quoted strings escape ``\\``, ``"``, ``\n`` and ``\r`` exactly as KiCad's
  ``OUTPUTFORMATTER::Quotes`` does (a tab is written raw, as KiCad writes it). Numbers keep
  the text they were read with, and a number created in memory keeps full precision
  (``format_number``). Layout (tabs, line breaks) is cosmetic: KiCad reads tokens, not
  layout (``paradigm-review-2`` ticket 03).
- **Typed atoms:** ``string``, ``symbol`` and ``number`` build atoms, ``set_string``,
  ``set_symbol`` and ``set_number`` replace one in a node. Use them instead of
  ``SExp.set_value``, which guesses the type from the text. A new string (``string``,
  ``set_string``, ``symbol``) holding a lone surrogate or a control character other than
  TAB, LF and CR is refused (``InvalidStringError``) where it is made; at serialisation
  only a lone surrogate is refused, because KiCad itself writes other control characters
  raw and a file it wrote must save.
- **Loading:** ``load(path)`` parses a file without newline translation, so a raw carriage
  return inside a string (which KiCad tolerates) is kept, not turned into ``\n``.
- **Saving:** ``save(root, path)`` (any ``.kicad_pcb``/``.kicad_sch``/``.kicad_mod``/
  ``.kicad_sym`` tree) and ``save_pcb(board, path)`` (a ``kicad_tools`` ``PCB``) serialise,
  write a temporary sibling with explicit UTF-8 and the line ending KiCad writes on the
  platform (CRLF on Windows, measured), run ``kicad-cli <pcb|sch|fp|sym> upgrade
  --force`` on a copy of it as a parse check (any non-zero exit, including a crash code such
  as 0xC0000005, is a failure; for a schematic every sub-sheet it references is checked on
  its own, because ``sch upgrade`` on a root reports success over a broken sub-sheet), run
  the caller's ``verify`` on the temporary file if one is given, and only then ``os.replace``
  the target (a transient Windows sharing denial of that move is retried on the schedule
  ``commit_directory`` uses, build ticket 78). On any failure the target is untouched and
  the temporary file is removed (its delete is retried the same way; if it still cannot go,
  a note on the error names it). What propagates: the layer's own checks raise
  ``WriteLayerError``; the caller's ``verify`` raises what it raises; a failed move raises
  the ``OSError`` from ``os.replace`` unchanged, after the retry for a sharing denial.

A passed parse check means only that KiCad can load the file. It does not see semantic loss
(lost nets, stale pad angles); that is each tool's self-check (decision 4).

Every public entry point is a plain module-level function, bound once: the save ratchet
(``tests/test_ratchets.py``, build ticket 29) exempts exactly those. The layer's own
temporary-file write, copies and ``os.replace`` are reviewed entries in
``tests/ratchets.toml``.
"""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

from kicad_tools.sexp import SExp, parse_string

# Root tag -> the kicad-cli subcommand that loads that file type.
FILE_KINDS = {
    "kicad_pcb": "pcb",
    "kicad_sch": "sch",
    "footprint": "fp",
    "module": "fp",
    "kicad_symbol_lib": "sym",
}

# How long one parse check may take before it counts as a failure (P10: never wait silently).
# A 5.9 MB board takes about 0.8 s.
PARSE_CHECK_TIMEOUT_S = 120

# Characters that force a string to be quoted: KiCad's lexer ends a bare token at whitespace
# or a parenthesis, and kicad_tools' parser reads "#" and ";" as the start of a comment.
_STRUCTURAL = frozenset(' \t\n\r"()\\')
_COMMENT_START = frozenset("#;")
_SUB_SHEET_PROPERTIES = ("Sheetfile", "Sheet file")
_INLINE_LIMIT = 100


class InvalidStringError(ValueError):
    """A string the layer will not write: it holds a lone surrogate (never writable: not
    encodable as UTF-8), or it is a NEW value a caller supplies that holds a control
    character other than TAB, LF and CR."""


_ALLOWED_CONTROLS = frozenset("\t\n\r")


def check_text(value: str, *, new: bool = True) -> str:
    """``value`` unchanged if the layer can write it, else ``InvalidStringError`` naming the
    first offending character and its position.

    Every string is refused if it holds a lone surrogate: no strict UTF-8 read produces one,
    and it cannot be written. ``new=True`` (a value a caller supplies: ``string``,
    ``set_string``, ``symbol``) also refuses C0 control characters other than TAB, LF and CR:
    KiCad escapes only LF and CR, so they would go into the file raw, and a tool has no
    business inventing them. ``new=False`` (any atom being written, including atoms read
    from disk) lets them through: KiCad itself writes and reads them raw (measured: ESC in a
    footprint Value, build ticket 42 fix round 2), so refusing them would refuse to save a
    file KiCad wrote."""
    for i, ch in enumerate(value):
        code = ord(ch)
        if 0xD800 <= code <= 0xDFFF:
            kind = "a lone surrogate"
        elif new and code < 0x20 and ch not in _ALLOWED_CONTROLS:
            kind = "a control character"
        else:
            continue
        raise InvalidStringError(
            f"{value!r} holds {kind} (U+{code:04X} at index {i}); a new KiCad string may hold "
            "TAB, LF and CR but no other control character, and no string a lone surrogate"
        )
    return value


class WriteLayerError(RuntimeError):
    """A save that did not happen. ``code`` is machine-readable (``parse_check_failed``,
    ``kicad_cli_unavailable``, ``sub_sheet_missing``, ``unsupported_file``,
    ``verify_failed``, ``line_break_in_sheet_pin``); ``details`` carries the evidence (exit
    code, output tail, file, pins)."""

    def __init__(self, code: str, message: str, **details: Any) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.details = details


# --------------------------------------------------------------------------- numbers, strings


def format_number(value: int | float) -> str:
    """A number as the layer writes it: integers as they are, floats with full precision in
    plain decimal notation (no exponent, no 6-digit rounding), an integral float without a
    fraction (``5.0`` as ``5``), and a negative zero as ``-0``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"not a number: {value!r}")
    if isinstance(value, int):
        return str(value)
    if not math.isfinite(value):
        raise ValueError(f"KiCad files hold finite numbers only, not {value!r}")
    if value == 0:
        return "-0" if math.copysign(1.0, value) < 0 else "0"
    if value.is_integer():
        return str(int(value))
    text = repr(value)
    if "e" in text or "E" in text:
        text = format(Decimal(text), "f")
    return text


def quote(value: str) -> str:
    """``value`` as a quoted KiCad string: backslash, double quote, line feed and carriage
    return escaped as KiCad's ``OUTPUTFORMATTER::Quotes`` writes them; a tab stays raw.
    Refuses (``InvalidStringError``) a lone surrogate. Other control characters are written
    raw, as KiCad writes them (``quote`` also writes atoms read from disk)."""
    escaped = check_text(value, new=False).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\r", "\\r")
    return f'"{escaped}"'


def looks_numeric(value: str) -> bool:
    """True when ``value`` written bare would be read as a number (by KiCad's lexer and by
    kicad_tools' parser): it starts like a number and parses as one."""
    if not value or value[0] not in "0123456789-+.":
        return False
    try:
        float(value)
    except ValueError:
        return False
    return True


def _breaks_token(value: str) -> bool:
    """True when ``value`` written bare would not be read back as one token."""
    return not value or value[0] in _COMMENT_START or any(c in _STRUCTURAL for c in value)


def must_quote(value: str) -> bool:
    """True when a string ``value`` cannot be written bare: it would not be read back as one
    token, or it would be read back as a number."""
    return _breaks_token(value) or looks_numeric(value)


def string(value: str) -> SExp:
    """A string atom that is always written quoted (a reference, a value, a net or layer
    name, a pad number), whatever its text looks like. Refuses (``InvalidStringError``) a
    lone surrogate or a control character other than TAB, LF and CR."""
    if not isinstance(value, str):
        raise TypeError(f"string() takes a str, not {type(value).__name__}: {value!r}")
    check_text(value)
    return SExp(value=value, _originally_quoted=True)


def symbol(value: str) -> SExp:
    """A bare keyword atom (``smd``, ``yes``, ``thru_hole``; a layer or net name is a
    ``string``). Refuses a value that would not be read back as the same bare token, and
    (``InvalidStringError``) a lone surrogate or a control character other than TAB, LF, CR."""
    if not isinstance(value, str):
        raise TypeError(f"symbol() takes a str, not {type(value).__name__}: {value!r}")
    check_text(value)
    if must_quote(value):
        raise ValueError(f"{value!r} cannot be written as a bare KiCad keyword; use string()")
    return SExp(value=value, _originally_bare=True)


def number(value: int | float) -> SExp:
    """A number atom written with full precision (``format_number``)."""
    return SExp(value=value, _original_str=format_number(value))


def _replace(node: SExp, index: int, atom: SExp) -> None:
    if node.is_atom:
        raise TypeError("set_* takes a list node, not an atom")
    if not 0 <= index <= len(node.children):
        raise IndexError(f"index {index} is outside ({node.name} ...) with {len(node.children)} children")
    if index == len(node.children):
        node.children.append(atom)
    else:
        node.children[index] = atom


def set_string(node: SExp, index: int, value: str) -> None:
    """Set child ``index`` of ``node`` to a quoted string (``index == len`` appends)."""
    _replace(node, index, string(value))


def set_symbol(node: SExp, index: int, value: str) -> None:
    """Set child ``index`` of ``node`` to a bare keyword (``index == len`` appends)."""
    _replace(node, index, symbol(value))


def set_number(node: SExp, index: int, value: int | float) -> None:
    """Set child ``index`` of ``node`` to a full-precision number (``index == len`` appends)."""
    _replace(node, index, number(value))


# --------------------------------------------------------------------------- serialising


def _same_number(text: str, value: int | float) -> bool:
    """Whether ``text`` (an atom's remembered source text) still denotes ``value``: a caller
    that changed ``atom.value`` in place leaves the old text behind."""
    try:
        parsed = float(text)
    except ValueError:
        return False
    if parsed != value:
        return False
    # An int has no negative zero: "-0" read as int 0 keeps its text. A float that was set to
    # -0.0 (or from -0.0 to 0.0) in place must not keep the other sign's text.
    return not isinstance(value, float) or math.copysign(1.0, parsed) == math.copysign(1.0, value)


def _quoted(atom: SExp, value: str) -> bool:
    if getattr(atom, "_originally_quoted", False):
        return True
    if getattr(atom, "_originally_bare", False):
        return _breaks_token(value)  # read bare: stays bare (an enum such as not_allowed)
    # Created in memory without a type (SExp.atom, SExp.list, set_value, kicad_tools' own
    # model code): quote it unless kicad_tools itself would write it as a bare keyword, and
    # always when it looks like a number (set_value(1, "100") must not write a bare 100).
    if must_quote(value):
        return True
    needs_quoting = getattr(atom, "_needs_quoting", None)
    return True if needs_quoting is None else bool(needs_quoting(value))


def atom_text(atom: SExp) -> str:
    """One atom as the layer writes it."""
    value = atom.value
    if isinstance(value, bool):
        raise TypeError(f"a KiCad atom is not a bool: {value!r}")
    if isinstance(value, (int, float)):
        original = getattr(atom, "_original_str", None)
        if original is not None and _same_number(original, value):
            return original
        return format_number(value)
    if isinstance(value, str):
        # Lone surrogates only (new=False): the atom may come from a file KiCad wrote, with a
        # raw control character KiCad itself reads back. Checked here too because an atom made
        # without string() may be written bare, where quote() never sees it.
        return quote(value) if _quoted(atom, check_text(value, new=False)) else value
    raise TypeError(f"cannot serialise an atom holding {value!r}")


def _compact(node: SExp) -> str:
    if node.is_atom:
        return atom_text(node)
    parts = [node.name] if node.name else []
    parts.extend(_compact(child) for child in node.children)
    return "(" + " ".join(parts) + ")"


def _flat(node: SExp) -> bool:
    """A list whose children are atoms or lists of atoms: written on one line when short."""
    return all(c.is_atom or all(g.is_atom for g in c.children) for c in node.children)


def _lines(node: SExp, depth: int, out: list[str]) -> None:
    tabs = "\t" * depth
    if depth > 0 and _flat(node):
        text = _compact(node)
        if len(text) <= _INLINE_LIMIT:
            out.append(tabs + text)
            return
    head = [node.name] if node.name else []
    children = node.children
    i = 0
    while i < len(children) and children[i].is_atom:
        head.append(atom_text(children[i]))
        i += 1
    out.append(tabs + "(" + " ".join(head))
    for child in children[i:]:
        if child.is_atom:
            out.append(tabs + "\t" + atom_text(child))
        else:
            _lines(child, depth + 1, out)
    out.append(tabs + ")")


def serialise(node: SExp, *, compact: bool = False) -> str:
    """``node`` as KiCad S-expression text. ``compact`` gives one line with single spaces
    (what the self-checks compare); otherwise one tab per level, short lists on one line."""
    if compact or node.is_atom:
        return _compact(node)
    out: list[str] = []
    _lines(node, 0, out)
    return "\n".join(out)


# --------------------------------------------------------------------------- loading


def load(path: str | Path) -> SExp:
    """Parse a KiCad S-expression file as UTF-8 without newline translation (a raw carriage
    return inside a string survives; ``kicad_tools.sexp.parse_file`` turns it into ``\n``)."""
    with open(path, encoding="utf-8", newline="") as fh:
        text = fh.read()
    return parse_string(text)


# --------------------------------------------------------------------------- the parse check


def file_kind(root: SExp) -> str:
    """The ``kicad-cli`` file type (``pcb``, ``sch``, ``fp``, ``sym``) of a root node."""
    kind = FILE_KINDS.get(root.name or "")
    if kind is None:
        raise WriteLayerError(
            "unsupported_file", f"the write layer saves {sorted(FILE_KINDS)} roots, not ({root.name} ...)"
        )
    return kind


def _kicad_cli() -> Path:
    from kicad_mcp import kicad_cli

    try:
        return kicad_cli.find_kicad_cli()
    except kicad_cli.KicadCliError as exc:
        raise WriteLayerError(
            "kicad_cli_unavailable", f"cannot run the parse check without kicad-cli: {exc}"
        ) from exc


def _exit_text(code: int) -> str:
    return f"{code} (0x{code & 0xFFFFFFFF:08X})" if code < 0 or code > 255 else str(code)


def _upgrade(kind: str, target: Path, shown: Path) -> None:
    """Run ``kicad-cli <kind> upgrade --force`` on ``target`` (a scratch copy of ``shown``)."""
    cli = _kicad_cli()
    try:
        proc = subprocess.run(
            [str(cli), kind, "upgrade", "--force", str(target)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PARSE_CHECK_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise WriteLayerError(
            "parse_check_failed",
            f"kicad-cli {kind} upgrade did not finish within {PARSE_CHECK_TIMEOUT_S} s on {shown.name}",
            file=str(shown),
        ) from exc
    if proc.returncode != 0:
        output = ((proc.stdout or "") + (proc.stderr or "")).strip()
        raise WriteLayerError(
            "parse_check_failed",
            f"KiCad cannot load {shown.name}: kicad-cli {kind} upgrade exited {_exit_text(proc.returncode)}",
            file=str(shown),
            exit_code=proc.returncode,
            output=output[-2000:],
        )


def _sub_sheets(root: SExp) -> list[str]:
    """The file names a schematic's ``(sheet ...)`` blocks load, in order."""
    names = []
    for sheet in root.children:
        if sheet.is_atom or sheet.name != "sheet":
            continue
        for prop in sheet.children:
            if (
                not prop.is_atom
                and prop.name == "property"
                and len(prop.children) >= 2
                and str(prop.children[0].value) in _SUB_SHEET_PROPERTIES
            ):
                names.append(str(prop.children[1].value))
    return names


def parse_check(written: str | Path, kind: str, *, target: str | Path | None = None) -> list[str]:
    """Check that KiCad loads ``written`` as a ``kind`` file, on a scratch copy (``kicad-cli
    upgrade`` rewrites in place). ``target`` is where the file will live (default: where it
    is); it names the copy and, for a schematic, the directory its sub-sheets are read from.
    A schematic's sub-sheets are each checked on their own, recursively. Returns the names
    of the files checked; raises ``WriteLayerError`` on the first that fails."""
    written = Path(written)
    target = Path(target) if target is not None else written
    if kind not in set(FILE_KINDS.values()):
        raise WriteLayerError("unsupported_file", f"no parse check for file kind {kind!r}")
    scratch = Path(tempfile.mkdtemp(prefix="kicad_mcp_parse_check_"))
    try:
        if kind == "fp":
            library = scratch / "check.pretty"
            library.mkdir()
            shutil.copyfile(written, library / target.name)
            _upgrade("fp", library, target)
            return [target.name]
        copy = scratch / target.name
        shutil.copyfile(written, copy)
        _upgrade(kind, copy, target)
        checked = [target.name]
        if kind == "sch":
            checked += _check_sub_sheets(written, target.parent, scratch, {target.resolve()})
        return checked
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _check_sub_sheets(sheet_file: Path, project_dir: Path, scratch: Path, seen: set[Path]) -> list[str]:
    checked = []
    for name in _sub_sheets(load(sheet_file)):
        source = (project_dir / name).resolve()
        if source in seen:
            continue  # a sheet used twice is checked once
        seen.add(source)
        if not source.is_file():
            raise WriteLayerError(
                "sub_sheet_missing",
                f"{sheet_file.name} loads sub-sheet {name!r}, which does not exist in {project_dir}",
                file=str(project_dir / name),
            )
        copy = scratch / f"sub-{len(seen)}" / source.name
        copy.parent.mkdir()
        shutil.copyfile(source, copy)
        _upgrade("sch", copy, source)
        checked.append(name)
        checked += _check_sub_sheets(source, source.parent, scratch, seen)
    return checked


# --------------------------------------------------------------------------- saving


# The line ending KiCad itself writes: its file writers use text mode, so KiCad 10.0.7's
# kicad-cli writes CRLF on Windows (measured for pcb, sch, fp and sym upgrades, from an LF
# input too; build ticket 42 fix round 1) and LF elsewhere. The layer matches it, so a board
# saved by KiCad and then by the layer differs only where its content does.
_KICAD_NEWLINE = os.linesep


def _write_temp(text: str, path: Path) -> Path:
    """Write ``text`` to a new temporary sibling of ``path`` as UTF-8, with the line ending
    KiCad writes on this platform (``_KICAD_NEWLINE``). If the write fails (a full disk, an
    unencodable character) the file is removed."""
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        with open(temp, "w", encoding="utf-8", newline=_KICAD_NEWLINE) as fh:
            fh.write(text)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return temp


def save(root: SExp, path: str | Path, *, verify: Callable[[Path], None] | None = None) -> Path:
    """Save the tree ``root`` to ``path``: serialise, write a temporary sibling (UTF-8), parse-
    check it with ``kicad-cli`` (each sub-sheet of a schematic on its own), call
    ``verify(temporary_path)`` if given (it raises to refuse), then ``os.replace`` it onto
    ``path``. On any failure ``path`` is untouched, the temporary file is removed and the
    error propagates (``WriteLayerError`` for the layer's own checks)."""
    path = Path(path)
    kind = file_kind(root)
    if kind == "sch" and (pins := _sheet_pins_with_line_breaks(root)):
        raise WriteLayerError(
            "line_break_in_sheet_pin",
            f"sheet pin name(s) {pins!r} in {path.name} hold a line break: KiCad 10.0.7 writes a "
            "sheet-pin name unescaped, so its next save of this file would break it (a LF makes "
            "the schematic unloadable); nothing was written",
            file=str(path),
            pins=pins,
        )
    temp = _write_temp(serialise(root) + "\n", path)
    return _commit(temp, path, lambda: parse_check(temp, kind, target=path), verify)


def _sheet_pins_with_line_breaks(root: SExp) -> list[str]:
    """Names of the ``(sheet ... (pin "NAME" ...))`` pins that hold a LF or CR (build ticket
    25: escaping them is not enough, because KiCad's own writer does not)."""
    found = []
    for sheet in root.children:
        if sheet.is_atom or sheet.name != "sheet":
            continue
        for pin in sheet.children:
            if pin.is_atom or pin.name != "pin" or not pin.children:
                continue
            name = pin.children[0].value
            if isinstance(name, str) and ("\n" in name or "\r" in name):
                found.append(name)
    return found


def _commit(temp: Path, path: Path, check: Callable[[], Any], verify: Callable[[Path], None] | None) -> Path:
    """Run ``check()`` and ``verify(temp)`` once each, then move ``temp`` onto ``path``,
    retrying a transient Windows sharing denial (``_retry_sharing_denial``; only the move is
    retried, never the checks). On any failure, a denial that outlasts the budget included,
    remove ``temp`` and re-raise the error unchanged, leaving ``path`` untouched. The delete
    is retried on the same schedule, since whatever held the file may hold it still; if it
    still fails, a note on the original error names the leftover ``temp`` (review round 1 of
    build ticket 78: the delete's own error used to replace the move's)."""
    try:
        check()
        if verify is not None:
            verify(temp)
        _retry_sharing_denial(lambda: os.replace(temp, path))
    except BaseException as exc:
        try:
            _retry_sharing_denial(lambda: temp.unlink(missing_ok=True))
        except OSError as cleanup:
            exc.add_note(f"the temporary file {temp} was left behind: deleting it failed ({cleanup})")
        raise
    return path


def save_project(data: dict, path: str | Path, *, verify: Callable[[Path], None] | None = None) -> Path:
    """Save a ``.kicad_pro`` (JSON) the way ``kicad_tools.core.project_file.save_project``
    formats it (``json.dumps(data, indent=2)``), then ``verify`` and ``os.replace``. Replaces
    ``save_project``, which 0.22.0+ writes through the Windows-broken ``atomic_write_text``
    (upstream #7478).

    What is checked, and what is not: ``data`` must be JSON-serialisable with finite numbers
    only (NaN and Infinity are not JSON and KiCad cannot read them: ``ValueError`` before any
    file exists). The temporary file is then read back and must parse to the same JSON; this
    guards the write itself (a short or mis-encoded write), not the data. There is no KiCad
    parse check for a project file (kicad-cli has none), so a well-formed JSON document KiCad
    does not understand is not caught here."""
    path = Path(path)
    try:
        text = json.dumps(data, indent=2, allow_nan=False)
    except ValueError as exc:
        raise ValueError(f"{path.name}: a KiCad project holds finite numbers only ({exc})") from exc
    expected = json.loads(text)
    temp = _write_temp(text, path)

    def check() -> None:
        try:
            with open(temp, encoding="utf-8") as fh:
                reread = json.load(fh)
        except ValueError as exc:  # JSONDecodeError, UnicodeDecodeError
            raise WriteLayerError(
                "parse_check_failed", f"{path.name} as written is not valid JSON: {exc}", file=str(path)
            ) from exc
        if reread != expected:
            raise WriteLayerError("parse_check_failed", f"{path.name} does not read back as the data written", file=str(path))

    return _commit(temp, path, check, verify)


_BYTE_EXACT_FILES = ("fp-lib-table", "sym-lib-table")


def save_bytes(data: bytes, path: str | Path, *, verify: Callable[[Path], None]) -> Path:
    """Save ``data`` to ``path`` exactly as given (no newline or encoding translation): write a
    temporary sibling, call ``verify(temporary_path)`` (it raises to refuse), then
    ``os.replace``. Only for a ``fp-lib-table`` or ``sym-lib-table`` whose bytes the caller
    keeps as they were (build ticket 76): kicad-cli cannot parse-check a lib table, so
    ``verify`` is required and is the only check. Any other file name raises
    ``WriteLayerError("unsupported_file")`` and a missing ``verify`` ``TypeError``, both before
    any file exists; its callers are frozen in tests/test_ratchets.py (``SAVE_BYTES_CALLERS``).
    On any failure ``path`` is untouched and the temporary file is removed."""
    path = Path(path)
    if path.name not in _BYTE_EXACT_FILES:
        raise WriteLayerError(
            "unsupported_file",
            f"save_bytes writes only {' and '.join(_BYTE_EXACT_FILES)}, not {path.name}; a KiCad design file goes "
            "through save",
            file=str(path),
        )
    if not callable(verify):
        raise TypeError(f"save_bytes needs a verify function (its only check), got {verify!r}")
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:12]}.tmp")
    try:
        with open(temp, "xb") as fh:
            fh.write(data)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return _commit(temp, path, lambda: None, verify)


def save_pcb(board: Any, path: str | Path, *, verify: Callable[[Path], None] | None = None) -> Path:
    """Save a ``kicad_tools`` ``PCB`` (its ``_sexp`` tree, the one ``PCB.save`` writes) or a
    ``kicad_pcb`` root through ``save``. Replaces ``board.save(path)``."""
    root = board if isinstance(board, SExp) else getattr(board, "_sexp", None)
    if not isinstance(root, SExp) or root.name != "kicad_pcb":
        raise WriteLayerError("unsupported_file", f"save_pcb takes a kicad_tools PCB or a kicad_pcb root, not {board!r}")
    return save(root, path, verify=verify)


# Windows refuses to rename a directory while any process holds a file inside it open. A
# scanner or indexer briefly opening a file the tool has just written makes the rename of a
# freshly verified revision fail with WinError 5 or 32 for a fraction of a second (measured
# 0.26-0.39 s in build ticket 42; frequent under kicad_tools 0.23.0's faster load path).
COMMIT_RETRY_BUDGET_S = 3.0
_SHARING_DENIED = (5, 32)  # ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION


def commit_directory(temp_dir: str | Path, target: str | Path) -> Path:
    """Move a verified temporary revision directory onto ``target`` (which must not exist).
    On Windows a transient sharing denial is retried with backoff for up to
    ``COMMIT_RETRY_BUDGET_S``; any other error, or a denial that outlasts the budget, is
    raised and ``temp_dir`` stays where it was for the caller to remove."""
    temp_dir, target = Path(temp_dir), Path(target)
    _retry_sharing_denial(lambda: os.rename(temp_dir, target))
    return target


def _retry_sharing_denial(move: Callable[[], Any]) -> None:
    """Call ``move()`` (one ``os.replace`` or ``os.rename``) until it succeeds. On Windows a
    ``PermissionError`` with WinError 5 or 32 (``_SHARING_DENIED``) is retried after 0.02 s,
    doubling to at most 0.5 s, while the next attempt still starts within
    ``COMMIT_RETRY_BUDGET_S`` of the first; the denial that outlasts it is raised unchanged.
    Any other error, and any error off Windows, is raised at once. The one retry schedule of
    the layer: ``_commit`` (every file save) and ``commit_directory`` both use it (build
    tickets 42, 78)."""
    deadline = time.monotonic() + COMMIT_RETRY_BUDGET_S
    delay = 0.02
    while True:
        try:
            move()
            return
        except PermissionError as exc:
            if os.name != "nt" or getattr(exc, "winerror", None) not in _SHARING_DENIED:
                raise
            if time.monotonic() + delay > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.5)
