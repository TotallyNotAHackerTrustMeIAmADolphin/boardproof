"""Board-rules setter: minimum clearance / track width / via size plus named net classes.

Contract: build ticket 18 and
docs/ARCHITECTURE.md ("Rule storage": editing only a netclass leaves
``rules.min_clearance = 0.0``, so rules must be set in both places).

Where KiCad reads the rules (measured on the golden board, KiCad 10 project files):
both places live in the ``.kicad_pro`` JSON. The constraints are
``board.design_settings.rules.*``; net classes and their nets are
``net_settings.classes`` / ``net_settings.netclass_patterns``. The ``.kicad_pcb``
``(setup ...)`` block carries neither, so the board file is carried over unchanged.

Writer (see ``_read_project`` / ``_write_project``): ``kicad_tools.core.project_file``
(``load_project`` plus its ``get_design_settings`` /
``get_netclass_definitions`` / ``get_netclass_patterns`` accessors) to read, and the write
layer's ``save_project`` to write (build ticket 42; 0.22.0+'s own ``save_project`` fails on
Windows), never raw text. Known behaviour, measured 2026-10-11 on kicad_tools' writer and kept
by the layer: ``save_project`` is
``json.dumps(indent=2)``, which escapes non-ASCII as ``\\uXXXX`` and omits KiCad's
trailing newline; the parsed content is identical, KiCad reads both. Library helpers
that would be destructive are deliberately NOT used: ``add_netclass_definition``
replaces a whole class with library defaults (dropping ``priority``, colours,
``tuning_profile``) and ``apply_manufacturer_rules`` writes keys KiCad does not
define. Only the touched values are set, on the loaded dict, in place.

Flow (dry-run plus revision-token checkpoint, docs/ARCHITECTURE.md decision 4):
``propose_board_rules`` writes nothing and returns the planned changes, any
loosening and a token; ``confirm_board_rules`` builds the revision in a temporary
sibling directory, reloads it fresh from disk, verifies every value and that nothing
else changed, renames it into place and only then re-pins and grades it. A failed
attempt leaves nothing behind and the token stays usable.
"""

from __future__ import annotations

import copy
import functools
import hashlib
import math
import re
import time
from pathlib import Path
from typing import Any

from kicad_tools.core import project_file as _pf

from kicad_mcp import checkpoint, write_layer
from kicad_mcp import verify_board as gate

# request argument -> key under board.design_settings.rules
_CONSTRAINT_KEYS = {
    "min_clearance": "min_clearance",
    "min_track_width": "min_track_width",
    "via_diameter": "min_via_diameter",
    "min_via_annular_width": "min_via_annular_width",
    # Board-wide minimum for EVERY drilled hole (vias and through-hole pads), not a via setting.
    "min_through_hole_diameter": "min_through_hole_diameter",
}
_CLASS_VALUE_KEYS = ("clearance", "track_width", "via_diameter", "via_drill")
_MAX_MM = 100.0

# What KiCad applies when a key is ABSENT from board.design_settings.rules. Measured
# 2026-10-11 with kicad-cli 10.0.x DRC on the golden board with ``rules = {}`` and items
# violating each limit: the violation text names the limit that was applied. An absent
# key is therefore never "unset": it is this value, and lowering below it is a loosening.
# min_clearance 0.0 means "no board-wide floor" (the net class clearance applies).
KICAD_RULE_DEFAULTS: dict[str, float] = {
    "min_clearance": 0.0,
    "min_track_width": 0.2,
    "min_via_diameter": 0.5,
    "min_through_hole_diameter": 0.3,
    "min_via_annular_width": 0.1,
}

# .kicad_dru constraint name -> the rules key it can override.
_DRU_TARGETS = {
    "clearance": "min_clearance",
    "track_width": "min_track_width",
    "via_diameter": "min_via_diameter",
    "annular_width": "min_via_annular_width",
    "hole_size": "min_through_hole_diameter",
}
_INT_MAX = 2**31 - 1
_MEASURED_ON = "10.0."  # kicad-cli version prefix KICAD_RULE_DEFAULTS were measured on

_DEFAULT_CLASS = "Default"


_clock = time.time  # replaced in tests
_PENDING = checkpoint.Pending(clock=lambda: _clock())  # token -> {"out_dir", "wanted", "classes"}
_DRIFT = (
    "the board, its project, its custom rules or its library tables changed since the "
    "proposal was made: re-run propose_board_rules"
)


# --- project file access (the only place the .kicad_pro is read or written) -----


def _read_project(path: str | Path) -> dict[str, Any]:
    return _pf.load_project(path)


def _write_project(path: str | Path, data: dict[str, Any]) -> None:
    write_layer.save_project(data, path)


# --- small helpers ---------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _valid_mm(value: Any) -> bool:
    """A finite, positive length no larger than ``_MAX_MM``; bool, NaN and inf never qualify."""
    return _is_number(value) and math.isfinite(value) and 0 < value <= _MAX_MM


def _leaf_diff(a: Any, b: Any, path: str = "") -> list[str]:
    if isinstance(a, dict) and isinstance(b, dict):
        out: list[str] = []
        for key in sorted(set(a) | set(b), key=str):
            if key not in a or key not in b:
                out.append(f"{path}/{key}")
            else:
                out += _leaf_diff(a[key], b[key], f"{path}/{key}")
        return out
    if isinstance(a, list) and isinstance(b, list) and len(a) == len(b):
        out = []
        for i, (x, y) in enumerate(zip(a, b)):
            out += _leaf_diff(x, y, f"{path}[{i}]")
        return out
    return [] if a == b else [path or "/"]


def _board_net_names(pcb_path: Path) -> set[str]:
    from kicad_tools.sexp import parse_file

    root = parse_file(str(pcb_path))
    names: set[str] = set()
    for node in root.children:
        if node.name == "net" and len(node.values) >= 2 and str(node.values[1]):
            names.add(str(node.values[1]))
    return names


# Net-class resolution, measured against KiCad 10.0's own ``NET_SETTINGS.GetEffectiveNetClass``
# (``tests/test_board_rules_netclass_oracle.py``; build ticket 36, review round 1).

# A matching class that leaves one of these undefined brings Default into the effective class
# (and its name: "A,Default"). Measured one key at a time; ``diff_pair_via_gap``, ``line_style``,
# the colours and ``tuning_profile`` do not.
_DEFAULT_FILL_KEYS = (
    "bus_width", "clearance", "diff_pair_gap", "diff_pair_width", "microvia_diameter",
    "microvia_drill", "track_width", "via_diameter", "via_drill", "wire_width",
)
# Below this ``net_settings`` schema version KiCad ignores each class's ``priority`` key and
# ranks the classes by their order in the file (version 3: every rung input and krt fixture).
_PRIORITY_KEY_SINCE = 4


class UnsupportedNetclassPattern(ValueError):
    """A net's class cannot be resolved here as KiCad resolves it, so the resolution refuses
    instead of guessing. Either a net-class pattern is outside the allowlist of pattern syntax
    (``_pattern_syntax_problem``: whatever Python's ``re`` and KiCad's wxRegEx or bus parser might
    read differently is refused) (``net`` is None: every resolution on that
    project refuses, ``kind`` "pattern"); or ``net`` matches no pattern by its whole name while a
    pattern matches text inside its braces, which KiCad reads as a bus group (``kind``
    "bus_member", ``_bus_member_refusal``); or ``net`` matches no pattern by its whole name, holds
    a bus vector ``[a..b]`` and the project has a pattern or assignment (``kind`` "bus_vector",
    ``_bus_vector_refusal``; ``why`` is the vector); or ``net`` holds a control character while the
    project has a pattern or assignment (``kind`` "net", ``_control_net_refusal``; ``why`` is the
    escaped character)."""

    def __init__(self, netclass: str, pattern: str, why: str, net: str | None = None, kind: str | None = None) -> None:
        self.netclass, self.pattern, self.why, self.net = netclass, pattern, why, net
        self.kind = kind or ("pattern" if net is None else "bus_member")
        if self.kind == "pattern":
            problem = (
                f"net class {netclass!r} has the pattern {pattern!r}, which uses {why}: that is outside the "
                "pattern syntax this tool resolves (plain names, the wildcards * and ?, and the regular-expression "
                "forms verified against KiCad's own classifier), because KiCad can read it differently from "
                "Python's regex engine, so the class of the nets it covers cannot be resolved as KiCad resolves it."
            )
        elif self.kind == "bus_member":
            problem = (
                f"net {net!r} matches no net-class pattern by its whole name, but the pattern {pattern!r} "
                f"of net class {netclass!r} matches {why}: KiCad then reads the name as a bus group and "
                "can give the net a class through its members, which is not reproduced here, so the "
                "net's class cannot be resolved as KiCad resolves it."
            )
        elif self.kind == "net":
            super().__init__(
                f"net {net!r} holds the control character {why} (KiCad's '.', '*' and '?' match a line break "
                "and every other control character, which is not reproduced here), and the project has "
                f"net-class patterns or assignments (the first: {pattern!r} of net class {netclass!r}), so "
                "the net's class cannot be resolved as KiCad resolves it. Rename the net in KiCad, or ask "
                "the user to; the .kicad_pro must not be edited by hand."
            )
            return
        else:
            super().__init__(
                f"net {net!r} matches no net-class pattern by its whole name and holds the bus vector "
                f"{why!r} (a range [a..b]): KiCad then reads the name as a bus and gives the net a class "
                "when every expanded member gets it ('D[0..1]' as D0 and D1), which is not reproduced "
                "here. The project has net-class patterns or assignments, any of which may class a member "
                f"(the first: {pattern!r} of net class {netclass!r}), so the net's class cannot be resolved "
                "as KiCad resolves it. Give the net its class in KiCad (Board Setup > Net Classes) with a "
                "pattern that matches its whole name, which KiCad applies before any member, or ask the "
                "user to; the .kicad_pro must not be edited by hand."
            )
            return
        super().__init__(
            f"{problem} Change or remove this pattern in KiCad (Board Setup > Net Classes) or ask the "
            "user to; no tool here edits an existing pattern, and the .kicad_pro must not be edited by hand."
        )


# The allowlist (fix round 6). Three fail-opens in one family (a brace, a net-side vector, a
# pattern-side vector) showed that listing what Python's ``re`` and KiCad's wxRegEx / bus parser
# read differently never ends. A net-class pattern is resolved here only when it is written in the
# grammar below, which ``tests/test_board_rules_netclass_oracle.py`` re-measures against
# ``GetEffectiveNetClass`` (a seeded sweep of ~400 patterns x ~120 nets); every other pattern
# refuses, whatever it would have matched.
#
#   pattern  := branch ('|' branch)*, and groups '(' pattern ')' not starting '(?' (parentheses that do
#               not balance make the regex invalid in both engines: the pattern is a wildcard only)
#   atoms    := a literal character (any non-ASCII character, space, or an ASCII printable other
#               than the specials below), '.', '*', '?', '+', a group, a bracket expression,
#               or an escape
#   '^'      := only first in the pattern, or right after '(' or '|'
#   '$'      := only last in the pattern, or right before '|' or ')'
#   quantifier '*' or '+' directly after another quantifier: refused; '?' after one (lazy) is kept
#   escape   := '\' + one of d D w W s S, or one of  . $ ^ * + ? | ( ) \ - /
#   bracket  := '[' '^'? item+ ']' with item a literal (ASCII letter or digit, '_' '/' '+' ',' '.'
#               '~' '!' '@' '#' '%' '&' '<' '>' ';' quote, or non-ASCII), \d \D \w \W \s \S, or a
#               range x-y of two ASCII digits or two letters of one case with x <= y; no '..' (a bus
#               vector), no ':' '=' '[' (POSIX classes, collating elements, nesting), no brace
#   refused  := '{' '}' (bus groups, bounds), a lone ']', control characters, any other escape,
#               unbalanced brackets
_ESCAPED_LITERALS = frozenset(".$^*+?|()\\-/")
_CLASS_ESCAPES = frozenset("dDwWsS")
_BRACKET_LITERALS = frozenset("_/+,.~!@#%&<>';\"")
_SPECIAL = frozenset("\\[]{}()|^$*+?.")


def _bracket_problem(pattern: str, start: int) -> tuple[str | None, int]:
    """Check the bracket expression opening at ``pattern[start] == '['``: ``(problem, index after
    its closing ']')``."""
    body_start = start + 1 + (pattern.startswith("^", start + 1))
    end = pattern.find("]", body_start)
    if pattern[body_start : body_start + 1] == "]":
        return "an empty bracket expression or one starting with ']'", start + 1
    if end < 0:
        return "an unclosed bracket expression '['", start + 1
    body = pattern[body_start:end]
    if ".." in body:
        return "a bus vector or range '..' inside a bracket expression ('[a..b]' is expanded by KiCad as a bus)", start + 1
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            if body[i + 1 : i + 2] not in _CLASS_ESCAPES:
                return f"the escape {body[i : i + 2]!r} inside a bracket expression", start + 1
            i += 2
        elif c.isascii() and c.isalnum():
            if body[i + 1 : i + 2] == "-" and i + 2 < len(body):
                hi = body[i + 2]
                same = (c.isdigit() and hi.isdigit()) or (c.isalpha() and hi.isalpha() and c.islower() == hi.islower())
                if not (hi.isascii() and same and c <= hi):
                    return f"the range {body[i : i + 3]!r} inside a bracket expression", start + 1
                i += 3
            else:
                i += 1
        elif c in _BRACKET_LITERALS or ord(c) >= 128:
            i += 1
        else:
            return f"the character {c!r} inside a bracket expression", start + 1
    return None, end + 1


@functools.lru_cache(maxsize=4096)
def _pattern_syntax_problem(pattern: str) -> str | None:
    """None when ``pattern`` is written in the allowlist above, else what in it is outside it."""
    i, last = 0, len(pattern) - 1
    branch_start, quantifiers = True, 0  # at the start of a branch; quantifiers in a row
    while i <= last:
        c = pattern[i]
        at_start, in_a_row = branch_start, quantifiers
        branch_start, quantifiers = False, 0
        if c == "\\":
            escaped = pattern[i + 1 : i + 2]
            if not escaped:
                return "a trailing backslash"
            if escaped not in _CLASS_ESCAPES and escaped not in _ESCAPED_LITERALS:
                return f"the escape {pattern[i : i + 2]!r}"
            i += 2
        elif c == "[":
            problem, i = _bracket_problem(pattern, i)
            if problem:
                return problem
        elif c in "{}":
            return "a brace '{' or '}' (a bus group or a repetition bound)"
        elif c == "]":
            return "a ']' that closes no bracket expression"
        elif c == "(":
            if pattern[i + 1 : i + 2] == "?":
                return "'(?' (an embedded option or group extension)"
            branch_start, i = True, i + 1
        elif c == ")":  # unbalanced parentheses make the regex invalid in both engines: a wildcard only
            i += 1
        elif c == "|":
            branch_start, i = True, i + 1
        elif c == "^":
            if not at_start:
                return "a '^' that is not at the start of a branch"
            i += 1
        elif c == "$":
            if i < last and pattern[i + 1] not in "|)":
                return "a '$' that is not at the end of a branch"
            i += 1
        elif c in "*+?":
            if in_a_row >= 2 or (in_a_row == 1 and c != "?"):
                return "two quantifiers in a row (a possessive or repeated quantifier)"
            quantifiers, i = in_a_row + 1, i + 1
        elif ord(c) < 32 or ord(c) == 127:
            return f"the control character {c!r}"
        else:  # a literal or '.'
            i += 1
    return None


def _check_patterns(data: dict) -> None:
    """Raise ``UnsupportedNetclassPattern`` for the first pattern (file order) that is outside the
    allowlist (``_pattern_syntax_problem``)."""
    for entry in _pf.get_netclass_patterns(data):
        pattern = str(entry.get("pattern", ""))
        why = _pattern_syntax_problem(pattern)
        if why is not None:
            raise UnsupportedNetclassPattern(str(entry.get("netclass")), pattern, why)


def _wildcard_matches(pattern: str, net: str) -> bool:
    regex = "".join(".*" if c == "*" else "." if c == "?" else re.escape(c) for c in pattern)
    return re.fullmatch(regex, net) is not None


def _regex_matches(pattern: str, net: str) -> bool:
    """KiCad's anchored regex match. The anchors are added as text: ``^`` unless the pattern
    starts with ``^``, ``$`` unless it ends with ``$`` -- an escaped ``\\$`` too, so ``VFF\\$``
    stays open at the end and matches ``VFF$X``. The match must begin at the first character
    (``AB|CD`` matches ``ABX`` and ``CD`` but not ``XCD``). ``\\w \\d \\s \\W \\S`` are ASCII-only,
    as in KiCad (``re.ASCII``). A pattern that is not a valid regex never matches this way.
    Python's ``re`` stands in for wxRegEx only for the syntax of the allowlist
    (``_pattern_syntax_problem``, re-measured against KiCad by the oracle tests); ``_check_patterns``
    refuses every other pattern first, and ``_control_net_refusal`` every net whose name holds a
    control character."""
    anchored = ("" if pattern.startswith("^") else "^") + pattern + ("" if pattern.endswith("$") else "$")
    try:
        compiled = re.compile(anchored, re.ASCII)
    except re.error:
        return False
    found = compiled.search(net)
    return found is not None and found.start() == 0


def _pattern_matches(pattern: str, net: str) -> bool:
    """A net-class pattern matches as a wildcard or as an anchored regex, case-sensitively.
    An empty pattern never matches (KiCad: not even the net named "")."""
    if not pattern:
        return False
    return _wildcard_matches(pattern, net) or _regex_matches(pattern, net)


def _class_by_name(data: dict, name: str) -> dict | None:
    for cls in _pf.get_netclass_definitions(data):
        if cls.get("name") == name:
            return cls
    return None


def _priority_of(data: dict, name: str) -> int:
    """The ``priority`` key as the file stores it (``_INT_MAX`` when absent): what a rules
    diff reports as written. What KiCad ranks the classes by is ``_kicad_priority``."""
    cls = _class_by_name(data, name)
    value = None if cls is None else cls.get("priority")
    return int(value) if _is_number(value) else _INT_MAX


def _net_settings_version(data: dict) -> int:
    meta = ((data.get("net_settings") or {}).get("meta") or {}).get("version")
    return int(meta) if _is_number(meta) else 0


def _kicad_priority(data: dict, name: str) -> int:
    """The rank KiCad gives a defined, non-Default class (lower first). From ``net_settings``
    schema version 4 on it is the ``priority`` key (absent: -1, measured); before, the class's
    index among the non-Default classes in file order."""
    if _net_settings_version(data) < _PRIORITY_KEY_SINCE:
        others = [c.get("name") for c in _pf.get_netclass_definitions(data) if c.get("name") != _DEFAULT_CLASS]
        return others.index(name) if name in others else _INT_MAX
    value = (_class_by_name(data, name) or {}).get("priority")
    return int(value) if _is_number(value) else -1


def _defines(data: dict, name: str, key: str) -> bool:
    return _is_number((_class_by_name(data, name) or {}).get(key))


def _direct_classes(data: dict, net: str) -> set[str]:
    """Every class a pattern or a legacy ``netclass_assignments`` entry gives ``net`` by its whole name."""
    matched: set[str] = set()
    for entry in _pf.get_netclass_patterns(data):
        if _pattern_matches(str(entry.get("pattern", "")), net):
            matched.add(str(entry.get("netclass")))
    legacy = (data.get("net_settings") or {}).get("netclass_assignments")
    if isinstance(legacy, dict) and net in legacy:
        names = legacy[net] if isinstance(legacy[net], list) else [legacy[net]]
        matched.update(str(name) for name in names)
    return matched


def _bus_group_members(net: str) -> list[str]:
    """The texts inside every ``{...}`` of ``net`` that does not follow ``~``, split at spaces and
    commas: a superset of the members KiCad reads from a bus group. An overbar ``~{...}`` opens
    no group (measured 2026-10-21: ``~{CS}``, ``Net-(U1-~{CS})`` and ``Q{~{CS}}`` stay in Default
    under ``CS``), but a plain group beside one does (``~{CS}{CS}`` gets the class of ``CS``).
    A group runs to its matching ``}``, or to the end of the name when it has none."""
    groups = []
    for start, char in enumerate(net):
        if char != "{" or net[start - 1 : start] == "~":
            continue
        depth, end = 1, start + 1
        while end < len(net) and depth:
            depth += {"{": 1, "}": -1}.get(net[end], 0)
            end += 1
        groups.append(net[start + 1 : end - 1 if depth == 0 else end])
    return [member for inside in groups for member in re.split(r"[\s,]+", inside) if member]


def _bus_member_refusal(data: dict, net: str) -> UnsupportedNetclassPattern | None:
    """KiCad reads a net that no pattern matches by its whole name as a bus group when it holds
    a plain ``{...}`` (an overbar ``~{...}`` is none), and gives it a class when every member of
    its first group (split at spaces and commas) gets that class (measured 2026-10-21: ``x``
    classes ``Q{x}`` and ``Q{x}Z{y}``, not ``Q{x y}``; ``?`` classes ``Q{x y}``). Not reproduced:
    the refusal when any pattern or legacy assignment matches any member of any plain group
    (``_bus_group_members``); None where KiCad keeps the net in Default as here
    (``/VPP{slash}MCLR`` under the patterns GND and VCC, ``/~{RESET}`` under ``?``)."""
    for member in _bus_group_members(net):
        for entry in _pf.get_netclass_patterns(data):
            pattern = str(entry.get("pattern", ""))
            if _pattern_matches(pattern, member):
                return UnsupportedNetclassPattern(
                    str(entry.get("netclass")), pattern, f"the text {member!r} inside its braces", net=net)
        legacy = (data.get("net_settings") or {}).get("netclass_assignments")
        if isinstance(legacy, dict) and member in legacy:
            return UnsupportedNetclassPattern(
                _legacy_class_name(legacy[member]), member,
                f"the text {member!r} inside its braces (a netclass_assignments entry)",
                net=net)
    return None


_BUS_VECTOR = re.compile(r"\[[^\[\]]*\.\.[^\[\]]*\]")


def _bus_vector_refusal(data: dict, net: str) -> UnsupportedNetclassPattern | None:
    """KiCad reads a net that no pattern matches by its whole name as a bus when it holds a
    vector ``[a..b]``, bare or inside a group, and gives it a class when every expanded member
    gets that class (measured 2026-10-21: ``D?`` classes ``D[0..1]``, ``D[1..0]``, ``Q{D[0..1]}``
    and ``{D[0..1]}``, not ``D[0..10]`` or ``Q{D[0..1] E}``; ``D0`` alone classes none; a pattern
    matching the whole name wins). Not reproduced: the refusal for such a net whenever the
    project has any non-empty pattern or any legacy assignment, which could class a member; None
    when it has neither, where KiCad keeps the net in Default as here."""
    vector = _BUS_VECTOR.search(net)
    if vector is None:
        return None
    for entry in _pf.get_netclass_patterns(data):
        if str(entry.get("pattern", "")):
            return UnsupportedNetclassPattern(
                str(entry.get("netclass")), str(entry["pattern"]), vector.group(), net=net, kind="bus_vector")
    legacy = (data.get("net_settings") or {}).get("netclass_assignments")
    if isinstance(legacy, dict) and legacy:
        member, names = next(iter(legacy.items()))
        return UnsupportedNetclassPattern(
            _legacy_class_name(names), str(member), vector.group(), net=net, kind="bus_vector")
    return None


def _control_net_refusal(data: dict, net: str) -> UnsupportedNetclassPattern | None:
    """KiCad's ``.``, ``*`` and ``?`` match a line break (measured: ``*``, ``.`` and ``A.B`` put
    ``A\nB`` in a class, Python's do not), so a net whose name holds any control character
    (C0 or DEL, as the pattern side refuses) cannot be judged here. Refused whenever the project has
    any non-empty pattern or legacy assignment, before any pattern is tried; None when it has neither,
    where KiCad keeps the net in Default as here."""
    char = next((c for c in net if ord(c) < 32 or ord(c) == 127), None)
    if char is None:
        return None
    escaped = repr(char)
    for entry in _pf.get_netclass_patterns(data):
        if str(entry.get("pattern", "")):
            return UnsupportedNetclassPattern(
                str(entry.get("netclass")), str(entry["pattern"]), escaped, net=net, kind="net")
    legacy = (data.get("net_settings") or {}).get("netclass_assignments")
    if isinstance(legacy, dict) and legacy:
        member, names = next(iter(legacy.items()))
        return UnsupportedNetclassPattern(_legacy_class_name(names), str(member), escaped, net=net, kind="net")
    return None


def _legacy_class_name(names: object) -> str:
    """A legacy ``netclass_assignments`` value (a name or a list of names) as one class name."""
    return ",".join(str(n) for n in names) if isinstance(names, list) else str(names)


def _netclass_chain(data: dict, net: str) -> list[str]:
    """KiCad's constituent classes of a net, in the order their values apply: every class a
    pattern (wildcard or regex) or a legacy ``netclass_assignments`` entry gives the net,
    defined classes ranked by ``(_kicad_priority, name)``, then names the file does not
    define (they contribute no value), then Default when it matched or when the classes
    before it leave a ``_DEFAULT_FILL_KEYS`` value undefined. No match: ``["Default"]``, and
    so is the net named "" (net 0), which KiCad never classes, not even through ``*``.
    A project with a pattern outside the syntax allowlist raises
    ``UnsupportedNetclassPattern`` for every net, and so does a net KiCad would class through
    its bus members (``_bus_member_refusal``) or bus-vector members (``_bus_vector_refusal``), and
    so does a net holding a control character (``_control_net_refusal``)."""
    _check_patterns(data)
    if net == "":
        return [_DEFAULT_CLASS]
    refusal = _control_net_refusal(data, net)
    if refusal is not None:
        raise refusal
    matched = _direct_classes(data, net)
    if not matched:
        refusal = _bus_member_refusal(data, net) or _bus_vector_refusal(data, net)
        if refusal is not None:
            raise refusal
    defined = sorted(
        (n for n in matched if n != _DEFAULT_CLASS and _class_by_name(data, n) is not None),
        key=lambda n: (_kicad_priority(data, n), n),
    )
    undefined = sorted(n for n in matched if n != _DEFAULT_CLASS and _class_by_name(data, n) is None)
    chain = defined + undefined
    incomplete = any(not any(_defines(data, n, key) for n in defined) for key in _DEFAULT_FILL_KEYS)
    if not chain or _DEFAULT_CLASS in matched or incomplete:
        chain.append(_DEFAULT_CLASS)
    return chain


def _effective_netclass(data: dict, net: str) -> str:
    """The name KiCad gives a net's effective class: its constituents joined by commas
    ("A,B", "A,Default"); a single complete class keeps its own name."""
    return ",".join(_netclass_chain(data, net))


def _effective_class(data: dict, net: str) -> str:
    """The winning class of a net: the first of KiCad's constituents (``_netclass_chain``),
    whose values apply wherever it defines them. Unmatched nets are in Default. The order
    was also measured with real kicad-cli DRC
    (``test_equal_priority_tie_goes_to_the_smaller_class_name_like_kicad``,
    ``test_real_drc_priority_picks_the_class``)."""
    return _netclass_chain(data, net)[0]


def _net_value(data: dict, net: str, key: str) -> tuple[float | None, str | None]:
    """``(value, class)``: the value KiCad applies to ``net`` for a class key, from the first
    constituent that defines it, then Default; and the class it came from."""
    for name in [*_netclass_chain(data, net), _DEFAULT_CLASS]:
        value = (_class_by_name(data, name) or {}).get(key)
        if _is_number(value):
            return float(value), name
    return None, None


def _class_value(data: dict, name: str, key: str) -> float | None:
    """One class's own value; a class that does not define the key inherits Default's.
    A NET's value is ``_net_value``: other matching classes can supply it first."""
    for source in (name, _DEFAULT_CLASS):
        cls = _class_by_name(data, source)
        value = None if cls is None else cls.get(key)
        if _is_number(value):
            return float(value)
    return None


def _constraint_before(data: dict, key: str) -> tuple[float | None, str]:
    rules = _pf.get_design_settings(copy.deepcopy(data)).get("rules", {})
    value = rules.get(key)
    if _is_number(value):
        return float(value), "file"
    return KICAD_RULE_DEFAULTS.get(key), "kicad_default"


def _cli_version() -> str | None:
    """``kicad-cli version`` output (e.g. ``10.0.5``), or None when it cannot be run."""
    import subprocess

    from kicad_mcp import kicad_cli

    try:
        proc = subprocess.run(
            [str(kicad_cli.find_kicad_cli()), "version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except Exception:  # noqa: BLE001
        return None
    text = (proc.stdout or "").strip().splitlines()
    return text[0].strip() if proc.returncode == 0 and text else None


# --- custom rules (.kicad_dru) ----------------------------------------------------


class _Str(str):
    """A quoted string token (a rule name or condition), as opposed to a bare atom."""


def _tokenize_dru(text: str) -> list[Any]:
    """Parse ``.kicad_dru`` text into nested lists: ``#`` comments, double-quoted strings
    with backslash escapes, bare atoms. Unbalanced input is closed at end of file."""
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
            i += 1
        elif char == '"':
            i += 1
            chars: list[str] = []
            while i < n and text[i] != '"':
                if text[i] == "\\" and i + 1 < n:
                    i += 1
                chars.append(text[i])
                i += 1
            i += 1  # closing quote (or end of file)
            stack[-1].append(_Str("".join(chars)))
        else:
            start = i
            while i < n and not text[i].isspace() and text[i] not in '()"#':
                i += 1
            stack[-1].append(text[start:i])
    return root


def _atom(node: Any) -> str | None:
    return node.lower() if isinstance(node, str) and not isinstance(node, _Str) else None


def _render(node: Any) -> str:
    if isinstance(node, list):
        return "(" + " ".join(_render(c) for c in node) + ")"
    return f'"{node}"' if isinstance(node, _Str) else str(node)


def _constraints_in(node: Any) -> list[str]:
    found: list[str] = []
    if isinstance(node, list):
        if node and _atom(node[0]) == "constraint" and len(node) > 1:
            kind = _atom(node[1])
            if kind:
                found.append(kind)
        for child in node:
            found.extend(_constraints_in(child))
    return found


def _custom_rules(pcb_path: Path) -> tuple[str | None, list[dict]]:
    """``(file name, constraints that can override the rules this tool sets)``.

    The file is only read, never written; it is tokenized (comments, quoted strings with
    escapes, case-insensitive keywords) rather than scanned for parentheses.
    """
    dru = pcb_path.with_suffix(".kicad_dru")
    if not dru.is_file():
        return None, []
    tree = _tokenize_dru(dru.read_text(encoding="utf-8", errors="replace"))
    found: list[dict] = []
    for node in tree:
        if not (isinstance(node, list) and node and _atom(node[0]) == "rule"):
            continue
        name = str(node[1]) if len(node) > 1 and not isinstance(node[1], list) else "?"
        for kind in _constraints_in(node):
            key = _DRU_TARGETS.get(kind)
            if key:
                found.append(
                    {"rule": name, "constraint": kind, "overrides_key": key, "text": _render(node)}
                )
    return dru.name, found


def _custom_rule_warnings(file_name: str | None, rules: list[dict], wanted: dict) -> list[str]:
    if not file_name:
        return []
    out = []
    for key in sorted(wanted):
        hits = sorted({r["rule"] for r in rules if r["overrides_key"] == key})
        if hits:
            out.append(
                f"{file_name} has custom rule(s) {hits} constraining {key}: the value set here "
                "may be moot where those rules apply (the file is never edited by this tool)"
            )
    if file_name and not out:
        out.append(
            f"{file_name} exists next to the board: custom rules are graded on top of these "
            "settings; review them if they constrain anything this change touches"
        )
    return out


# --- planning --------------------------------------------------------------------


def _normalise_request(
    constraints: dict[str, float], netclasses: list[dict] | None
) -> tuple[dict[str, float], list[dict], str | None]:
    wanted: dict[str, float] = {}
    for arg, value in constraints.items():
        if value is None:
            continue
        if not _valid_mm(value):
            return {}, [], f"{arg} must be a finite number of millimetres in (0, {_MAX_MM:g}], got {value!r}"
        wanted[_CONSTRAINT_KEYS[arg]] = float(value)
    classes: list[dict] = []
    seen: set[str] = set()
    for raw in netclasses or []:
        name = raw.get("name") if isinstance(raw, dict) else None
        if not isinstance(name, str) or not name.strip():
            return {}, [], "every net class needs a non-empty name"
        if name in seen:
            return {}, [], f"net class {name!r} is listed twice"
        seen.add(name)
        values: dict[str, float] = {}
        for key in _CLASS_VALUE_KEYS:
            if raw.get(key) is None:
                continue
            if not _valid_mm(raw[key]):
                return {}, [], (
                    f"net class {name!r}: {key} must be a finite number of millimetres "
                    f"in (0, {_MAX_MM:g}], got {raw[key]!r}"
                )
            values[key] = float(raw[key])
        nets = raw.get("nets") or []
        if not isinstance(nets, list) or not all(isinstance(n, str) and n for n in nets):
            return {}, [], f"net class {name!r}: nets must be a list of net names"
        if nets and name == _DEFAULT_CLASS:
            return {}, [], "nets cannot be assigned to the Default class: every unassigned net already is in it"
        priority = raw.get("priority")
        if priority is not None and (
            not isinstance(priority, int) or isinstance(priority, bool) or not 0 <= priority < 10_000
        ):
            return {}, [], f"net class {name!r}: priority must be an integer in [0, 10000)"
        unknown_keys = set(raw) - {"name", "nets", "priority", *_CLASS_VALUE_KEYS}
        if unknown_keys:
            return {}, [], f"net class {name!r}: unknown keys {sorted(unknown_keys)}"
        classes.append(
            {"name": name, "values": values, "nets": list(dict.fromkeys(nets)), "priority": priority}
        )
    if not wanted and not classes:
        return {}, [], "nothing requested: give at least one constraint or net class"
    return wanted, classes, None


def _apply_request(data: dict, wanted: dict[str, float], classes: list[dict]) -> dict:
    """Return a deep copy of ``data`` with the request applied through the kicad_tools accessors."""
    new = copy.deepcopy(data)
    rules = _pf.get_design_settings(new).setdefault("rules", {})
    for key, value in wanted.items():
        rules[key] = value
    definitions = _pf.get_netclass_definitions(new)
    patterns = _pf.get_netclass_patterns(new)
    for spec in classes:
        target = _class_by_name(new, spec["name"])
        if target is None:
            template = _class_by_name(new, _DEFAULT_CLASS) or _pf.DEFAULT_NETCLASS_DEFINITION
            target = copy.deepcopy(template)
            target["name"] = spec["name"]
            others = [
                c["priority"]
                for c in definitions
                if c.get("name") != _DEFAULT_CLASS and _is_number(c.get("priority"))
            ]
            target["priority"] = (max(others) + 1) if others else 0
            definitions.append(target)
        target.update(spec["values"])
        if spec.get("priority") is not None:
            target["priority"] = spec["priority"]
        for net in spec["nets"]:
            for entry in patterns:
                if entry.get("pattern") == net:
                    entry["netclass"] = spec["name"]
                    break
            else:
                patterns.append({"netclass": spec["name"], "pattern": net})
    return new


def _project_refusal(exc: UnsupportedNetclassPattern) -> dict:
    """The refusal for a pattern, assignment or net the project itself holds."""
    refusal = {"status": "error", "reason": str(exc), "netclass": exc.netclass, "pattern": exc.pattern}
    return refusal if exc.net is None else {**refusal, "net": exc.net}


def _requested_net_refusal(exc: UnsupportedNetclassPattern) -> dict:
    """The refusal when the pattern at fault is one the request itself would add (a requested
    net's name), not one the project holds: the fix is to assign the class in KiCad, never to
    change a pattern the user does not have."""
    net = exc.pattern
    if exc.kind == "pattern":
        problem = (
            f"a name with {exc.why} is outside the pattern syntax this tool resolves as KiCad does. Nets whose "
            "names use such syntax (braces, bus vectors [a..b], bracket or escape forms beyond the plain ones) "
            "cannot be assigned through a pattern by this tool"
        )
    elif exc.kind == "bus_vector":
        problem = (
            f"the board net {exc.net!r} holds the bus vector {exc.why!r} (a range [a..b]): once the project "
            "has a net-class pattern, KiCad can class such a net through its expanded members, which is not "
            "reproduced here, so any request that adds a net-class pattern is refused while the board holds "
            "this net, unless the added pattern matches this net's whole name (KiCad then classes the net by "
            "that name). Give that net its class first with a pattern that matches its whole name"
        )
    elif exc.kind == "net":
        problem = (
            f"the board net {exc.net!r} holds the control character {exc.why} (KiCad's '.', '*' and '?' "
            "match it, which is not reproduced here), so any request that adds a net-class pattern is "
            "refused while the board holds this net. Rename that net in KiCad first"
        )
    else:
        problem = (
            f"that pattern would also match the text inside the braces of the board net {exc.net!r}, "
            "which KiCad can then class through those members as a bus group; that is not reproduced here. "
            "A net another net names inside its braces cannot be assigned through a pattern by this tool"
        )
    return {
        "status": "error",
        "reason": (
            f"The requested net {net!r} (class {exc.netclass!r}) cannot be assigned here: a request assigns "
            f"a net by adding its name as a net-class pattern, and {problem}. Assign the class to the net in KiCad "
            "instead, on the schematic (a net class directive on the net's label) or in Board Setup > Net "
            "Classes with a pattern in the plain syntax, or ask the user to; the .kicad_pro must not be "
            "edited by hand."
        ),
        "netclass": exc.netclass,
        "pattern": net,
        "net": net,
        **({} if exc.net is None else {"bus_net": exc.net}),
    }


def _coherence(data: dict) -> tuple[list[dict], list[dict]]:
    """``(incoherent, moot)``: class values that fight the board-wide minimums.

    ``incoherent``: a track/via/hole value below its board minimum, which DRC then flags
    on every item built from the class. ``moot``: a class clearance below the board-wide
    minimum clearance, which is harmless (the larger value applies) but never takes effect.
    """

    def limit(key: str) -> float:
        value, _ = _constraint_before(data, key)
        return float(value or 0.0)

    incoherent: list[dict] = []
    moot: list[dict] = []
    pairs = (
        ("track_width", "min_track_width"),
        ("via_diameter", "min_via_diameter"),
        ("via_drill", "min_through_hole_diameter"),
    )
    for cls in _pf.get_netclass_definitions(data):
        name = str(cls.get("name"))
        for key, limit_key in pairs:
            value = cls.get(key)
            if _is_number(value) and value < limit(limit_key) - 1e-9:
                incoherent.append(
                    {"class": name, "key": key, "value": value, "limit_key": limit_key, "limit": limit(limit_key)}
                )
        diameter, drill = cls.get("via_diameter"), cls.get("via_drill")
        if _is_number(diameter) and _is_number(drill):
            ring = (diameter - drill) / 2
            if ring < limit("min_via_annular_width") - 1e-9:
                incoherent.append(
                    {
                        "class": name,
                        "key": "via_annular_width",
                        "value": round(ring, 6),
                        "limit_key": "min_via_annular_width",
                        "limit": limit("min_via_annular_width"),
                    }
                )
        clearance = cls.get("clearance")
        if _is_number(clearance) and clearance < limit("min_clearance") - 1e-9:
            moot.append(
                {
                    "class": name,
                    "key": "clearance",
                    "value": clearance,
                    "limit_key": "min_clearance",
                    "limit": limit("min_clearance"),
                    "note": "the board-wide minimum clearance applies instead of this value",
                }
            )
    return incoherent, moot


def _new_findings(old_list: list[dict], new_list: list[dict]) -> list[dict]:
    seen = {(i["class"], i["key"], i["value"], i["limit"]) for i in old_list}
    return [i for i in new_list if (i["class"], i["key"], i["value"], i["limit"]) not in seen]


def _plan(
    old: dict, new: dict, wanted: dict[str, float], classes: list[dict], nets: set[str]
) -> tuple[list, list]:
    changes: list[dict] = []
    loosened: list[dict] = []
    for key, value in wanted.items():
        before, source = _constraint_before(old, key)
        rules = _pf.get_design_settings(copy.deepcopy(old)).get("rules", {})
        if source == "file" and before == value and key in rules:
            continue
        entry = {"where": "constraints", "key": key, "before": before, "after": value, "before_source": source}
        changes.append(entry)
        if before is not None and before > 0 and value < before:
            loosened.append(dict(entry))
    for spec in classes:
        where = f"netclass:{spec['name']}"
        existed = _class_by_name(old, spec["name"]) is not None
        if not existed:
            changes.append({"where": where, "key": "(new class)", "before": None, "after": spec["values"]})
        for key, value in spec["values"].items():
            before = _class_value(old, spec["name"], key) if existed else None
            if before == value:
                continue
            if existed:
                changes.append({"where": where, "key": key, "before": before, "after": value})
            if before is not None and value < before:
                loosened.append({"where": where, "key": key, "before": before, "after": value})
        if existed and spec.get("priority") is not None and spec["priority"] != _priority_of(old, spec["name"]):
            changes.append(
                {
                    "where": where,
                    "key": "priority",
                    "before": _priority_of(old, spec["name"]),
                    "after": spec["priority"],
                }
            )
    # Assignments are judged on every net of the board: a priority change can move nets
    # nobody named. A net's class is KiCad's effective class (all matching classes, "A,B"),
    # and its values are KiCad's per-key values: ANY smaller value is a loosening of that net.
    for net in sorted(nets):
        was, now = _effective_netclass(old, net), _effective_netclass(new, net)
        if was == now:
            continue
        changes.append({"where": "assignment", "key": net, "before": was, "after": now})
        for value_key in _CLASS_VALUE_KEYS:
            before = _net_value(old, net, value_key)[0]
            after = _net_value(new, net, value_key)[0]
            if before is not None and after is not None and after < before:
                loosened.append(
                    {
                        "kind": "assignment",
                        "where": "assignment",
                        "key": net,
                        "value_key": value_key,
                        "before": before,
                        "after": after,
                        "from_class": was,
                        "to_class": now,
                    }
                )
    return changes, loosened


# --- checkpoint ------------------------------------------------------------------


def _default_output_dir(pcb_path: Path) -> Path:
    resolved = pcb_path.resolve()
    return resolved.parent.parent / f"{resolved.parent.name}_rules"


def _output_dir_problem(src: Path, out_dir: Path) -> str | None:
    return checkpoint.output_dir_problem(src, out_dir, what="the result")


def propose_board_rules(
    pcb_path: str | Path,
    *,
    min_clearance: float | None = None,
    min_track_width: float | None = None,
    via_diameter: float | None = None,
    min_via_annular_width: float | None = None,
    min_through_hole_diameter: float | None = None,
    netclasses: list[dict] | None = None,
    allow_loosen: bool = False,
    allow_incoherent: bool = False,
    output_dir: str | Path | None = None,
) -> dict:
    """Dry run. Reports every value that would change, every loosening and every
    incoherence; writes nothing.

    Board-wide minimums (``board.design_settings.rules``): ``min_clearance``,
    ``min_track_width``, ``via_diameter`` (-> ``min_via_diameter``),
    ``min_via_annular_width`` and ``min_through_hole_diameter`` (the minimum for EVERY
    drilled hole, vias included; not a via setting). Per-class via drill is
    ``netclasses[].via_drill``.

    ``netclasses`` is a list of ``{"name", "clearance"?, "track_width"?, "via_diameter"?,
    "via_drill"?, "priority"?, "nets"?: [exact net names]}``; unset values of a NEW class
    inherit the Default class. A key absent from the project counts as KiCad's built-in
    default (``KICAD_RULE_DEFAULTS``), never as unset. A loosening is ``refused`` unless
    ``allow_loosen``; a class value below a board minimum (``incoherent``) is ``refused``
    unless ``allow_incoherent``. Every length is finite and in (0, 100] mm.
    """
    src = Path(pcb_path)
    if not src.is_file():
        return {"status": "error", "reason": f"file not found: {pcb_path}"}
    project = src.with_suffix(".kicad_pro")
    if not project.is_file():
        return {
            "status": "error",
            "reason": f"no project file next to the board ({project.name}): rules live there",
        }
    constraints = {
        "min_clearance": min_clearance,
        "min_track_width": min_track_width,
        "via_diameter": via_diameter,
        "min_via_annular_width": min_via_annular_width,
        "min_through_hole_diameter": min_through_hole_diameter,
    }
    wanted, classes, problem = _normalise_request(constraints, netclasses)
    if problem:
        return {"status": "error", "reason": problem}
    try:
        data = _read_project(project)
        known = _board_net_names(src)
        dru_name, dru_rules = _custom_rules(src)
    except Exception as exc:  # noqa: BLE001
        return {"status": "error", "reason": f"could not read the board or its project: {exc}"}
    unknown = sorted({n for spec in classes for n in spec["nets"]} - known)
    if unknown:
        return {"status": "error", "reason": f"nets not on the board: {unknown}", "unknown_nets": unknown}

    new = _apply_request(data, wanted, classes)
    # Before any net is judged, every board net resolves in the project as it is and then with the
    # request applied: a guessed class never is. A refusal of the project as it is belongs to the
    # project; one that only the request brings (new keeps every old pattern) belongs to the request.
    try:
        _check_patterns(data)
        for net in sorted(known):
            _netclass_chain(data, net)
    except UnsupportedNetclassPattern as exc:
        return _project_refusal(exc)
    try:
        _check_patterns(new)
        for net in sorted(known):
            _netclass_chain(new, net)
    except UnsupportedNetclassPattern as exc:
        # Every refusal here names a pattern the request added: the old ones resolved every net
        # above (a refusal names the first pattern in file order that causes it, and a requested
        # net equal to an old pattern reuses that entry with its text unchanged).
        return _requested_net_refusal(exc)
    for spec in classes:
        for net in spec["nets"]:
            winner = _effective_class(new, net)
            if winner != spec["name"]:
                return {
                    "status": "error",
                    "reason": (
                        f"net {net!r} would still resolve to class {winner!r}, not {spec['name']!r}: "
                        "a higher-priority class matches it too; raise the priority or narrow the pattern"
                    ),
                }
    changes, loosened = _plan(data, new, wanted, classes, known)
    old_incoherent, old_moot = _coherence(data)
    now_incoherent, now_moot = _coherence(new)
    incoherent = _new_findings(old_incoherent, now_incoherent)
    incoherent_existing = [i for i in now_incoherent if i not in incoherent]
    moot = _new_findings(old_moot, now_moot)
    version = _cli_version()
    unverified = version is None or not version.startswith(_MEASURED_ON)
    warnings = _custom_rule_warnings(dru_name, dru_rules, wanted)
    if unverified:
        warnings.append(
            f"KICAD_RULE_DEFAULTS were measured on kicad-cli {_MEASURED_ON}x; this is "
            f"{version or 'not available'}: an absent rule key may mean a different default, so a "
            "loosening against it can go undetected"
        )
    common = {
        "changes": changes,
        "loosened": loosened,
        "incoherent": incoherent,
        "incoherent_existing": incoherent_existing,
        "moot": moot,
        "kicad_cli_version": version,
        "defaults_unverified": unverified,
        "custom_rules_file": dru_name,
        "custom_rules": dru_rules,
        "warnings": warnings,
        "revision_token": None,
    }
    if not changes:
        return {"status": "nothing_to_do", **common}
    blocked = []
    if loosened and not allow_loosen:
        blocked.append("loosen existing rules (pass allow_loosen=true)")
    if incoherent and not allow_incoherent:
        requested = {spec["name"] for spec in classes}
        existing = sorted({i["class"] for i in incoherent if i["class"] not in requested})
        if existing:
            blocked.append(
                "raise a board-wide minimum above the values of existing class(es) "
                f"{existing} (set them in the same request via netclasses, or pass allow_incoherent=true)"
            )
        if any(i["class"] in requested for i in incoherent):
            blocked.append(
                "set class values below the board-wide minimums (pass allow_incoherent=true)"
            )
    if blocked:
        return {"status": "refused", "reason": "the request would " + " and ".join(blocked), **common}

    out_dir = Path(output_dir) if output_dir else _default_output_dir(src)
    problem = _output_dir_problem(src, out_dir)
    if problem:
        return {"status": "error", "reason": problem}

    try:
        token = _PENDING.issue(src, {"out_dir": str(out_dir), "wanted": wanted, "classes": classes})
    except checkpoint.Refused as exc:
        return {"status": "incomplete", "reason": str(exc)}
    return {
        "status": "proposed",
        **common,
        "revision_token": token,
        "output_pcb": str(out_dir / src.name),
        "note": (
            "creates a new project revision; the original is untouched. Call "
            "confirm_board_rules with this revision_token to apply."
        ),
    }


def _readback(project: Path, wanted: dict, classes: list[dict]) -> dict:
    """What a fresh parse of the project says, for exactly the touched settings."""
    data = _read_project(project)
    rules = _pf.get_design_settings(data).get("rules", {})
    out: dict[str, Any] = {
        "constraints": {key: rules.get(key) for key in wanted},
        "netclasses": {},
        "assignments": {},
    }
    for spec in classes:
        cls = _class_by_name(data, spec["name"]) or {}
        out["netclasses"][spec["name"]] = {
            key: cls.get(key) for key in spec["values"]
        } | {"present": bool(cls)}
        for net in spec["nets"]:
            out["assignments"][net] = _effective_class(data, net)
    return out


def _expected_readback(wanted: dict, classes: list[dict]) -> dict:
    return {
        "constraints": dict(wanted),
        "netclasses": {s["name"]: dict(s["values"]) | {"present": True} for s in classes},
        "assignments": {n: s["name"] for s in classes for n in s["nets"]},
    }


def _build_revision(src: Path, out_dir: Path, wanted: dict, classes: list[dict]) -> dict:
    return checkpoint.build_revision(
        out_dir,
        write=lambda tmp: _write(src, tmp, wanted, classes),
        verify=lambda tmp, state: _verify(src, state, wanted, classes),
        success="built",
        before_verified="writing the rules failed before a revision was verified",
    )


def _write(src: Path, tmp: Path, wanted: dict, classes: list[dict]) -> dict:
    tmp_pcb = checkpoint.copy_project(src, tmp, max_bytes=gate._MAX_COPY_BYTES)
    tmp_pro = tmp_pcb.with_suffix(".kicad_pro")
    original = _read_project(tmp_pro)
    updated = _apply_request(original, wanted, classes)
    _write_project(tmp_pro, updated)
    return {"pcb": tmp_pcb, "pro": tmp_pro, "updated": updated}


def _verify(src: Path, state: dict, wanted: dict, classes: list[dict]) -> dict:
    # Non-negotiable: verify from a fresh parse of the saved file, never the in-memory dict.
    tmp_pcb, tmp_pro, updated = state["pcb"], state["pro"], state["updated"]
    try:
        fresh = _read_project(tmp_pro)
        readback = _readback(tmp_pro, wanted, classes)
    except Exception as exc:  # noqa: BLE001
        raise checkpoint.NotVerified(
            f"CRITICAL: written project failed to re-parse: {exc}",
            [checkpoint.mismatch(tmp_pro.name, "project", "a project that parses", str(exc))],
        ) from exc
    expected = _expected_readback(wanted, classes)
    if readback != expected:
        raise checkpoint.NotVerified(
            "CRITICAL: the reloaded project does not carry the requested rules",
            [
                checkpoint.mismatch(tmp_pro.name, f"{section}/{key}", want, readback[section].get(key))
                for section, values in expected.items()
                for key, want in values.items()
                if readback[section].get(key) != want
            ],
            expected=expected,
            found=readback,
        )
    if fresh != updated:  # JSON-equal to exactly what was meant to be written, everywhere
        stray = _leaf_diff(updated, fresh)
        raise checkpoint.NotVerified(
            f"CRITICAL: the reloaded project differs from what was written: {stray[:10]}",
            [checkpoint.mismatch(tmp_pro.name, path, "as written", "differs after reload") for path in stray],
        )
    if tmp_pcb.read_bytes() != src.read_bytes():
        raise checkpoint.NotVerified(
            "CRITICAL: the board file changed during the copy",
            [checkpoint.mismatch(tmp_pcb.name, "file bytes", _sha256(src), _sha256(tmp_pcb))],
        )
    return {"readback": readback}


def _baseline_diff(before: dict, after: dict) -> dict[str, list[str]]:
    return {
        key: _leaf_diff(before.get(key), after.get(key))
        for key in sorted(set(before) | set(after))
        if before.get(key) != after.get(key)
    }


def confirm_board_rules(
    pcb_path: str | Path,
    revision_token: str,
    *,
    parity: str,
    parity_reason: str | None = None,
    drc: Any = None,
) -> dict:
    """Apply a proposal: write the new project revision, verify it from a fresh reload,
    then re-pin the rule baseline and grade the revision.

    ``parity`` / ``parity_reason`` go to ``verify_board`` unchanged. ``drc`` replaces the
    kicad-cli boundary (tests only). The token is consumed only by a successful write or
    by detected source drift.
    """
    src = Path(pcb_path)
    entry, refused = _PENDING.claim(revision_token, src, drift_reason=_DRIFT)
    if refused:
        return refused
    project = src.with_suffix(".kicad_pro")
    if not project.is_file():  # hashed at propose, so a missing project is drift
        _PENDING.spend(revision_token)
        return {"status": "error", "reason": _DRIFT}
    out_dir = Path(entry["out_dir"])
    problem = _output_dir_problem(src, out_dir)
    if problem:
        return {"status": "error", "reason": problem}

    built = _build_revision(src, out_dir, entry["wanted"], entry["classes"])
    if built["status"] != "built":
        return built
    _PENDING.spend(revision_token)
    out_pcb = out_dir / src.name
    # Recomputed from the source so the report never trusts the proposal's own numbers.
    data = _read_project(project)
    new = _apply_request(data, entry["wanted"], entry["classes"])
    changes, loosened = _plan(data, new, entry["wanted"], entry["classes"], _board_net_names(src))
    dru_name, dru_rules = _custom_rules(src)
    raw = project.read_bytes()
    has_newline = "yes" if raw.endswith(b"\n") else "no"
    has_non_ascii = "yes" if any(b > 127 for b in raw) else "no"
    common = {
        "output_pcb": str(out_pcb),
        "changes": changes,
        "loosened": loosened,
        "readback": built["readback"],
        "custom_rules_file": dru_name,
        "custom_rules": dru_rules,
        "warnings": _custom_rule_warnings(dru_name, dru_rules, entry["wanted"]),
        "file_format_note": (
            "the .kicad_pro revision is written by the write layer's save_project (json.dumps, indent 2): "
            "it has no trailing newline (KiCad writes one) and escapes non-ASCII characters as "
            "\\uXXXX (KiCad writes them raw); the parsed content is identical and KiCad reads "
            f"both. Source file: trailing newline {has_newline}, "
            f"non-ASCII characters {has_non_ascii}. "
            "Expect a text diff on those two points only."
        ),
    }
    try:
        before = gate.snapshot_rules(src, drc=drc)
        after = gate.snapshot_rules(out_pcb, drc=drc)
        graded = gate.verify_board(
            out_pcb, parity=parity, parity_reason=parity_reason, baseline=after, drc=drc
        )
    except Exception as exc:  # noqa: BLE001  (e.g. kicad-cli missing): the revision exists, grading did not run
        return {
            "status": "incomplete",
            "reason": f"revision written and verified, but the baseline could not be pinned or graded: {exc}",
            **common,
        }
    return {
        "status": "applied",
        **common,
        "baseline_before": before,
        "baseline": after,
        "baseline_changed": before != after,
        "baseline_diff": _baseline_diff(before, after),
        "grading": {
            # Not a verdict: only a verify_board call through the server records one.
            "status": graded["verdict"],
            "certified": False,
            "violations": graded["violations"],
            "unconnected": graded["unconnected"],
            "unstable": graded["unstable"],
            "list_complete": graded["list_complete"],
            "incomplete_reasons": graded["incomplete_reasons"],
            "notes": graded.get("notes", []),  # informational: never change the verdict
        },
        "note": (
            "the rules changed, so every earlier verdict and baseline pin is void: "
            "re-grade this revision and re-baseline before relying on it"
        ),
    }
