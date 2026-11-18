"""Custom design rules (``.kicad_dru``): reading, the managed pair-clearance block, a condition
evaluator and a KiCad load probe (GitHub issue 131; docs/ARCHITECTURE.md "Custom rules").

What KiCad 10.0.7 does with the file (measured with ``kicad-cli pcb drc`` on scratch copies):

* **One error drops the whole file, silently.** A missing ``(version 1)``, an unbalanced
  parenthesis, an unknown constraint name, an unparsable condition, an unknown function in a
  condition or a number without a unit (``(min 0.5)``; write ``0.5mm``) makes ``kicad-cli`` ignore
  EVERY rule of the file: exit code 0, no message, the report simply lacks the rule's violations.
  ``load_probe`` is the only authoritative check that a file loads.
* A clearance rule **replaces** the net class clearance and the board minimum for the items it
  matches, in either direction: a rule of 0.1 mm lets 0.25 mm gaps pass where the class asks for
  0.8 mm (a loosening). Of several matching rules the LAST in the file wins.
* ``A.NetClass == 'x'`` is true when ``x`` is any constituent class of the item's net or its whole
  effective name (``'X,HV'``); ``'HV,X'`` (a different order) is false. ``NetClass`` and ``NetName`` compare
  case-INsensitively in ``==`` (``'hv'`` matches ``HV``), ``hasNetclass`` case-sensitively; ``*`` and ``?`` in the
  compared text are wildcards; ``(min 0.9 mm)`` with a space before the unit is accepted. The rule matches with A and B
  in either order. ``A.hasNetclass('x')`` and ``A.NetName == 'n'`` match the same way.
* Without a rule, two nets are held apart by the largest of the two net classes' clearances and the
  board minimum.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from boardproof import write_layer

DRU_SUFFIX = ".kicad_dru"

# --- tokenizer ---------------------------------------------------------------------


class Str(str):
    """A quoted string token (a rule name or condition), as opposed to a bare atom."""


def tokenize_checked(text: str) -> tuple[list[Any], list[str]]:
    """Parse ``.kicad_dru`` text into nested lists: ``#`` comments, double-quoted strings with
    backslash escapes, bare atoms. Unbalanced input is closed at end of file and reported in the
    second result (KiCad drops a file with such a defect)."""
    problems: list[str] = []
    root: list[Any] = []
    stack: list[list[Any]] = [root]
    i, n = 0, len(text)
    while i < n:
        char = text[i]
        if char.isspace():
            i += 1
        elif char == "#":
            while i < n and text[i] not in "\r\n":
                i += 1
        elif char == "(":
            child: list[Any] = []
            stack[-1].append(child)
            stack.append(child)
            i += 1
        elif char == ")":
            if len(stack) > 1:
                stack.pop()
            else:
                problems.append("a closing parenthesis without an opening one")
            i += 1
        elif char == '"':
            i += 1
            chars: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                chars.append(text[i])
                i += 1
            if i >= n:
                problems.append("a string that is never closed")
            i += 1  # closing quote (or end of file)
            stack[-1].append(Str("".join(chars)))
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in '()"#':
                i += 1
            stack[-1].append(text[start:i])
    if len(stack) > 1:
        problems.append(f"{len(stack) - 1} opening parenthesis(es) never closed")
    return root, problems


def tokenize(text: str) -> list[Any]:
    return tokenize_checked(text)[0]


def atom(node: Any) -> str | None:
    return node.lower() if isinstance(node, str) and not isinstance(node, Str) else None


def render(node: Any) -> str:
    if isinstance(node, list):
        return "(" + " ".join(render(c) for c in node) + ")"
    return f'"{node}"' if isinstance(node, Str) else str(node)


def constraints_in(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, list):
        if node and atom(node[0]) == "constraint" and len(node) > 1:
            kind = atom(node[1])
            if kind:
                found.append(kind)
        for child in node:
            found.extend(constraints_in(child))
    return found


# --- the rule model ------------------------------------------------------------------

_NUMBER = re.compile(r"^([0-9]*\.?[0-9]+)(mm|in|mil|um)?$", re.IGNORECASE)
_UNIT_MM = {"mm": 1.0, "in": 25.4, "mil": 0.0254, "um": 0.001}


@dataclass(frozen=True)
class Constraint:
    kind: str
    min_mm: float | None = None
    max_mm: float | None = None
    opt_mm: float | None = None
    unitless: bool = False  # a number with no unit: KiCad drops the whole file


@dataclass(frozen=True)
class Rule:
    name: str
    condition: str | None
    constraints: tuple[Constraint, ...]
    layer: str | None
    severity: str | None
    text: str
    order: int  # position among the file's rules, from 0
    managed: bool = False

    def clearance(self) -> Constraint | None:
        return next((c for c in self.constraints if c.kind == "clearance"), None)

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "condition": self.condition,
            "constraints": [
                {
                    "kind": c.kind,
                    **{k: v for k, v in (("min_mm", c.min_mm), ("max_mm", c.max_mm), ("opt_mm", c.opt_mm)) if v is not None},
                }
                for c in self.constraints
            ],
            "layer": self.layer,
            "severity": self.severity,
            "managed_by_boardproof": self.managed,
            "order": self.order,
            "text": self.text,
        }


def _length(token: Any) -> tuple[float | None, bool]:
    """``(mm, unitless)`` of a length token; ``(None, False)`` when it is not a length at all."""
    if not isinstance(token, str) or isinstance(token, Str):
        return None, False
    match = _NUMBER.match(token)
    if not match:
        return None, False
    unit = match.group(2)
    return float(match.group(1)) * _UNIT_MM[(unit or "mm").lower()], unit is None


def _constraint(node: list) -> Constraint | None:
    kind = atom(node[1]) if len(node) > 1 else None
    if not kind:
        return None
    values: dict[str, float] = {}
    unitless = False
    for part in node[2:]:
        if isinstance(part, list) and len(part) > 1 and atom(part[0]) in ("min", "max", "opt"):
            number = part[1]
            if len(part) == 3 and isinstance(part[2], str) and not isinstance(part[2], Str) and part[2].lower() in _UNIT_MM:
                number = f"{part[1]}{part[2]}"  # KiCad accepts a space before the unit (measured): (min 0.9 mm)
            mm, bare = _length(number)
            if mm is not None:
                values[atom(part[0])] = mm
                unitless = unitless or bare
    return Constraint(kind, values.get("min"), values.get("max"), values.get("opt"), unitless)


def parse(text: str) -> tuple[list[Rule], int | None, list[str]]:
    """``(rules, version, problems)``. ``problems`` are the defects that make KiCad drop the whole
    file and that can be seen without KiCad: unbalanced input, no ``(version N)``, a rule without a
    name, a length without a unit. A file this finds nothing wrong with may still be refused by
    KiCad (an unknown constraint name or condition function): ``load_probe`` decides."""
    tree, problems = tokenize_checked(text)
    version: int | None = None
    rules: list[Rule] = []
    managed_names = _managed_rule_names(text)
    for node in tree:
        if not isinstance(node, list) or not node:
            continue
        head = atom(node[0])
        if head == "version" and len(node) > 1:
            try:
                version = int(str(node[1]))
            except ValueError:
                problems.append(f"the version line is not a number: {render(node)}")
        elif head == "rule":
            name = str(node[1]) if len(node) > 1 and not isinstance(node[1], list) else None
            if name is None:
                problems.append(f"a rule without a name: {render(node)[:80]}")
                name = "?"
            condition = layer = severity = None
            constraints: list[Constraint] = []
            for part in node[2:]:
                if not (isinstance(part, list) and part):
                    continue
                key = atom(part[0])
                if key == "condition" and len(part) > 1:
                    condition = str(part[1])
                elif key == "layer" and len(part) > 1:
                    layer = str(part[1])
                elif key == "severity" and len(part) > 1:
                    severity = str(part[1]).lower()
                elif key == "constraint":
                    parsed = _constraint(part)
                    if parsed is not None:
                        constraints.append(parsed)
                        if parsed.unitless:
                            problems.append(f"rule {name!r}: a length without a unit (write 0.5mm, not 0.5)")
            rules.append(
                Rule(name, condition, tuple(constraints), layer, severity, render(node), len(rules), name in managed_names)
            )
    if version is None:
        problems.append("no (version N) line: KiCad ignores a rules file without one")
    return rules, version, problems


# --- conditions -------------------------------------------------------------------------


@dataclass(frozen=True)
class NetCtx:
    """What a condition can tell of one item's net: its name and its constituent classes."""

    name: str
    classes: tuple[str, ...]

    @property
    def effective(self) -> str:
        return ",".join(self.classes)


_COND_TOKEN = re.compile(
    r"""\s*(?:(?P<str>'(?:[^'\\]|\\.)*')|(?P<op>==|!=|&&|\|\||[()!,])|(?P<word>[A-Za-z_][A-Za-z_0-9.]*))"""
)


class _Unsupported(Exception):
    pass


def _cond_tokens(text: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    pos = 0
    text = text.rstrip()
    while pos < len(text):
        match = _COND_TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise _Unsupported(text[pos:])
        kind = match.lastgroup
        out.append((kind, match.group(kind)))
        pos = match.end()
    return out


def _and(a: bool | None, b: bool | None) -> bool | None:
    if a is False or b is False:
        return False
    return None if a is None or b is None else True


def _or(a: bool | None, b: bool | None) -> bool | None:
    if a is True or b is True:
        return True
    return None if a is None or b is None else False


def _not(a: bool | None) -> bool | None:
    return None if a is None else not a


def _wild(value: str) -> bool:
    return "*" in value or "?" in value


class _Evaluator:
    def __init__(self, tokens: list[tuple[str, str]], a: NetCtx, b: NetCtx) -> None:
        self.t, self.i, self.items = tokens, 0, {"A": a, "B": b}

    def peek(self) -> tuple[str, str] | tuple[None, None]:
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self) -> tuple[str, str]:
        tok = self.peek()
        if tok[0] is None:
            raise _Unsupported("unexpected end")
        self.i += 1
        return tok  # type: ignore[return-value]

    def expect(self, value: str) -> None:
        if self.take()[1] != value:
            raise _Unsupported(value)

    def parse(self) -> bool | None:
        result = self.or_()
        if self.peek()[0] is not None:
            raise _Unsupported("trailing tokens")
        return result

    def or_(self) -> bool | None:
        result = self.and_()
        while self.peek() == ("op", "||"):
            self.take()
            result = _or(result, self.and_())
        return result

    def and_(self) -> bool | None:
        result = self.not_()
        while self.peek() == ("op", "&&"):
            self.take()
            result = _and(result, self.not_())
        return result

    def not_(self) -> bool | None:
        if self.peek() == ("op", "!"):
            self.take()
            return _not(self.not_())
        return self.atom_()

    def string(self) -> str:
        kind, value = self.take()
        if kind != "str":
            raise _Unsupported("a quoted string was expected")
        return value[1:-1]

    def atom_(self) -> bool | None:
        kind, value = self.take()
        if (kind, value) == ("op", "("):
            result = self.or_()
            self.expect(")")
            return result
        if kind != "word":
            raise _Unsupported(value)
        item, _, member = value.partition(".")
        if item not in self.items or not member:
            self._skip_operand()
            return None  # a function or a property this evaluator does not model
        net = self.items[item]
        if member == "hasNetclass":
            self.expect("(")
            wanted = self.string()
            self.expect(")")
            return None if _wild(wanted) else wanted in net.classes
        if member in ("NetClass", "NetName"):
            op = self.take()
            if op[1] not in ("==", "!="):
                raise _Unsupported(op[1])
            wanted = self.string()
            if _wild(wanted):
                return None
            # KiCad compares NetClass and NetName case-INsensitively in == (measured 10.0.7); hasNetclass is not
            lowered = wanted.lower()
            have = (
                lowered in {c.lower() for c in net.classes} or lowered == net.effective.lower()
                if member == "NetClass"
                else lowered == net.name.lower()
            )
            return have if op[1] == "==" else not have
        self._skip_operand()
        return None

    def _skip_operand(self) -> None:
        """Consume the rest of a comparison or call this evaluator does not model."""
        if self.peek() == ("op", "("):
            depth = 0
            while True:
                kind, value = self.take()
                depth += (value == "(") - (value == ")")
                if depth == 0:
                    break
        elif self.peek()[1] in ("==", "!="):
            self.take()
            kind, value = self.take()
            if kind == "word" and self.peek() == ("op", "("):
                self._skip_operand()


def evaluate(condition: str | None, a: NetCtx, b: NetCtx) -> bool | None:
    """Whether ``condition`` holds with ``A`` and ``B`` as given: True, False, or None when it
    depends on something this evaluator does not model (item type, layer, a wildcard, a syntax it
    does not know). No condition holds everywhere."""
    if condition is None or not condition.strip():
        return True
    try:
        return _Evaluator(_cond_tokens(condition), a, b).parse()
    except _Unsupported:
        return None


def matches(condition: str | None, a: NetCtx, b: NetCtx) -> bool | None:
    """The rule's reading for two nets, A and B in either order (measured: both orders match)."""
    return _or(evaluate(condition, a, b), evaluate(condition, b, a))


# --- the managed pair-clearance block -------------------------------------------------

BLOCK_BEGIN = "# >>> boardproof pair clearances (managed by propose_board_rules and confirm_board_rules; change them there) >>>"
BLOCK_END = "# <<< boardproof pair clearances <<<"
RULE_PREFIX = "boardproof pair clearance: "
MAX_CLASS_NAME = 200
_BAD_CLASS_CHARS = re.compile(r"['\"\\\x00-\x1f\x7f]")


def class_name_problem(name: Any) -> str | None:
    """Why a net class name cannot go into a rule condition, or None."""
    if not isinstance(name, str) or not name:
        return "a net class name must be a non-empty string"
    if name != name.strip():
        return f"net class {name!r} has leading or trailing whitespace"
    if len(name) > MAX_CLASS_NAME:
        return f"net class {name[:30]!r}... is longer than {MAX_CLASS_NAME} characters"
    if _BAD_CLASS_CHARS.search(name):
        return f"net class {name!r} holds a quote, a backslash or a control character, which a rule condition cannot carry"
    if "," in name:
        return f"net class {name!r} holds a comma, which KiCad reads as the join of two classes"
    if "*" in name or "?" in name:
        return f"net class {name!r} holds * or ?, which KiCad reads as a wildcard in a condition (measured: 'Ma*' matched Mains)"
    return None


def pair_key(a: str, b: str) -> tuple[str, str]:
    return (a, b) if a <= b else (b, a)


def pair_name(a: str, b: str) -> str:
    return f"{RULE_PREFIX}{a} / {b}"


def pair_condition(a: str, b: str) -> str:
    return f"A.NetClass == '{a}' && B.NetClass == '{b}'"


def format_mm(value: float) -> str:
    return f"{value:.4f}".rstrip("0").rstrip(".") + "mm"


def representable(value: float) -> bool:
    """A clearance this module writes exactly: positive, at most four decimals."""
    return value > 0 and abs(round(value, 4) - value) < 1e-12 and round(value, 4) > 0


def render_block(pairs: dict[tuple[str, str], float], newline: str = "\n") -> str:
    lines = [BLOCK_BEGIN]
    for (a, b), value in sorted(pairs.items()):
        lines += [
            f'(rule "{pair_name(a, b)}"',
            f'\t(condition "{pair_condition(a, b)}")',
            f"\t(constraint clearance (min {format_mm(value)}))",
            ")",
        ]
    lines.append(BLOCK_END)
    return newline.join(lines) + newline


def _line_starts(text: str) -> list[tuple[int, str]]:
    out, pos = [], 0
    for line in text.splitlines(keepends=True):
        out.append((pos, line.strip()))
        pos += len(line)
    return out


def split_block(text: str) -> tuple[int, int, dict[tuple[str, str], float], list[str]] | None:
    """The managed block of ``text``: ``(start, end, pairs, problems)`` with ``text[start:end]`` the
    whole block, markers and trailing line break included; None when there is none. ``problems`` is
    non-empty when the block was edited by hand (the caller refuses then, so a hand edit is never
    silently overwritten) or its markers are not a clean begin/end pair."""
    starts = _line_starts(text)
    begins = [(p, i) for i, (p, s) in enumerate(starts) if s == BLOCK_BEGIN]
    ends = [(p, i) for i, (p, s) in enumerate(starts) if s == BLOCK_END]
    if not begins and not ends:
        return None
    if len(begins) != 1 or len(ends) != 1 or ends[0][1] < begins[0][1]:
        return 0, 0, {}, [f"the managed block markers are not exactly one begin followed by one end ({len(begins)} begin, {len(ends)} end)"]
    start = begins[0][0]
    end_line = text.splitlines(keepends=True)[ends[0][1]]
    end = ends[0][0] + len(end_line)
    inner = text[start + len(text[start:].splitlines(keepends=True)[0]) : ends[0][0]]
    tree, problems = tokenize_checked(inner)
    problems = list(problems)
    pairs: dict[tuple[str, str], float] = {}
    for node in tree:
        parsed = _managed_rule(node)
        if parsed is None:
            problems.append(f"something other than a pair-clearance rule inside the block: {render(node)[:80]}")
            continue
        key, value = parsed
        if key in pairs:
            problems.append(f"the pair {key[0]} / {key[1]} is in the block twice")
        pairs[key] = value
    return start, end, pairs, [f"the managed block was changed by hand: {p}" for p in problems]


_COND_CANON = re.compile(r"^A\.NetClass == '([^']+)' && B\.NetClass == '([^']+)'$")


def _managed_rule(node: Any) -> tuple[tuple[str, str], float] | None:
    if not (isinstance(node, list) and len(node) == 4 and atom(node[0]) == "rule"):
        return None
    name, cond, cons = node[1], node[2], node[3]
    if not (isinstance(name, Str) and name.startswith(RULE_PREFIX)):
        return None
    if not (isinstance(cond, list) and len(cond) == 2 and atom(cond[0]) == "condition" and isinstance(cond[1], Str)):
        return None
    match = _COND_CANON.match(str(cond[1]))
    if not match:
        return None
    a, b = match.groups()
    if str(name) != pair_name(a, b) or pair_key(a, b) != (a, b):
        return None
    if not (isinstance(cons, list) and len(cons) == 3 and atom(cons[0]) == "constraint" and atom(cons[1]) == "clearance"):
        return None
    inner = cons[2]
    if not (isinstance(inner, list) and len(inner) == 2 and atom(inner[0]) == "min"):
        return None
    mm, bare = _length(inner[1])
    if mm is None or bare:
        return None
    return (a, b), mm


def _managed_rule_names(text: str) -> set[str]:
    block = split_block(text) if BLOCK_BEGIN in text else None
    if not block:
        return set()
    return {pair_name(a, b) for (a, b) in block[2]}


def newline_of(text: str | None) -> str:
    return "\r\n" if text and "\r\n" in text else "\n"


def with_pairs(text: str | None, pairs: dict[tuple[str, str], float]) -> str:
    """``text`` with the managed block replaced by ``pairs`` (appended after everything else when
    there is none; removed when ``pairs`` is empty). The rest of the text is kept byte for byte.
    A missing file becomes ``(version 1)`` plus the block. Raises ``ValueError`` on a hand-edited
    block (``split_block`` problems)."""
    newline = newline_of(text)
    if text is None:
        text = "(version 1)" + newline
    block = split_block(text)
    if block is not None and block[3]:
        raise ValueError("; ".join(block[3]))
    new_block = render_block(pairs, newline) if pairs else ""
    if block is not None:
        start, end = block[0], block[1]
        before = text[:start]
        if not new_block and before.endswith(newline * 2):  # exactly the blank line that separated the block
            before = before[: -len(newline)]
        return before + new_block + text[end:]
    if not pairs:
        return text
    body = text if text.endswith(("\n", "\r")) or not text else text + newline
    blank = newline if body.strip() else ""
    return body + blank + new_block


def outside_block(text: str) -> str:
    """The text without the managed block: what a pair change must leave byte for byte."""
    block = split_block(text) if BLOCK_BEGIN in text or BLOCK_END in text else None
    return text if not block else text[: block[0]] + text[block[1] :]


# --- does KiCad load the file? ---------------------------------------------------------------

PROBE_RULES = (
    '(rule "boardproof_probe_track" (constraint track_width (min 1000mm)))',
    '(rule "boardproof_probe_hole" (constraint hole_size (min 1000mm)))',
)
_BOARD_HAS_TARGET = re.compile(r"\((?:segment|arc|via)\b|\(drill\b")


def load_probe(pcb_path: Path, dru_text: str, drc: Any, *, max_bytes: int | None = None) -> dict:
    """Does ``kicad-cli`` load ``dru_text`` as the rules file of ``pcb_path``? One DRC run on a
    scratch copy of the project whose rules file is ``dru_text`` plus two probe rules at its end
    (a track width and a hole size no board meets): they fire only when KiCad read the file to its
    end, because a single defect makes it drop every rule (module docstring). ``drc`` is the
    ``run_drc`` function. Returns ``{"loaded": True|False|None, "reason": ...}``; None means the
    board has no track, via or hole the probes could fire on, so nothing can be concluded."""
    from boardproof import checkpoint

    scratch = Path(tempfile.mkdtemp(prefix="boardproof_dru_probe_"))
    try:
        copy = checkpoint.copy_project(Path(pcb_path), scratch / "p", max_bytes=max_bytes)
        newline = newline_of(dru_text)
        probed = dru_text + ("" if dru_text.endswith(("\n", "\r")) or not dru_text else newline) + newline.join(PROBE_RULES) + newline
        write_layer.save_bytes(probed.encode("utf-8"), copy.with_suffix(DRU_SUFFIX), verify=lambda _p: None)
        report = drc(copy, refill_zones=False, schematic_parity=False)
        fired = any("boardproof_probe" in json.dumps(v) for v in report.get("violations", []))
        if fired:
            return {"loaded": True, "reason": "the probe rules at the end of the file fired"}
        board = Path(pcb_path).read_text(encoding="utf-8", errors="replace")
        if _BOARD_HAS_TARGET.search(board):
            return {
                "loaded": False,
                "reason": (
                    "KiCad ignored the rules file: the probe rules at its end did not fire on a board that has tracks, "
                    "vias or holes (a defect in any rule drops every rule of the file, silently)"
                ),
            }
        return {"loaded": None, "reason": "the board has no track, via or hole the probe rules could fire on"}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
