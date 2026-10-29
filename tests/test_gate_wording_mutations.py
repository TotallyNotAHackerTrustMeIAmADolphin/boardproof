"""Mutation table for the two gate-wording regexes (build ticket 60, fix round 4).

``PASSES_OR_CONNECTED`` and ``_QUESTION`` (test_description_gate_wording.py) decide which
descriptions and outputs count as a "passes" or "is connected" claim. Rounds 1 to 3 pinned the
fragments a reviewer named, one by one, and each review found more. This file is the single
place that owns them all.

* Each row names one fragment: ``anchor`` is a unique piece of the regex source, ``old`` is the
  part of it that is mutated (removed, loosened or narrowed into ``new``). The test replaces it
  once, recompiles, and asserts that the row's own sentence gives ``before`` with the real
  regexes and ``after`` with the mutant. The sentence depends on that fragment alone.
* The completeness check tokenises both sources and lists every alternative separator, every
  lookahead and lookbehind, every scoped flag group, every optional or repeated atom, every
  anchor (``^``, ``\\b``), every class member and every ``\\s`` or ``\\w``. Each of those must lie
  inside the ``old`` text of at least one row. A fragment added later without a row fails;
  a fragment that no row can flip is dead and is removed from the regex instead (``a-z`` in the
  classes was: both regexes are compiled with IGNORECASE).
"""

from __future__ import annotations

import re

import pytest

from test_description_gate_wording import (
    PASSES_OR_CONNECTED,
    _QUESTION,
    has_passes_or_connected,
)

_P = PASSES_OR_CONNECTED
_Q = _QUESTION.pattern
_SOURCES = {"P": _P, "Q": _Q}

_LOOK = re.compile(r"\(\?(<!|<=|!|=|-i:|:)")


def _sites(src: str) -> list[tuple[int, str, str]]:
    """Every fragment of ``src`` that can change what it matches: (offset, kind, text)."""
    out: list[tuple[int, str, str]] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            t = src[i : i + 6] if src[i + 1] == "u" else src[i : i + 2]
            out.append((i, "anchor" if t in ("\\b", "\\B") else "escape", t))
            i += len(t)
        elif c == "(":
            m = _LOOK.match(src, i)
            if m:
                if m.group(1) != ":":
                    out.append((i, "flag" if m.group(1) == "-i:" else "look", m.group(0)))
                i = m.end()
            else:
                i += 1
        elif c == "|":
            out.append((i, "alt", c))
            i += 1
        elif c in "^$":
            out.append((i, "anchor", c))
            i += 1
        elif c in "?*+":
            out.append((i, "quant", c))
            i += 1
        elif c == "{":
            j = src.index("}", i)
            out.append((i, "quant", src[i : j + 1]))
            i = j + 1
        elif c == "[":
            j = i + 1
            if src[j] == "^":
                out.append((j, "member", "^"))
                j += 1
            while src[j] != "]":
                t = (src[j : j + 6] if src[j + 1] == "u" else src[j : j + 2]) if src[j] == "\\" else src[j]
                k = j + len(t)
                if src[k] == "-" and src[k + 1] != "]":
                    e = src[k + 1 : k + 7] if src[k + 1] == "\\" and src[k + 2] == "u" else src[k + 1]
                    t, k = src[j : k + 1 + len(e)], k + 1 + len(e)
                out.append((j, "member", t))
                j = k
            i = j + 1
        else:
            i += 1
    return out


class Row:
    def __init__(self, target, name, anchor, old, new, sentence, before, after):
        self.target, self.name, self.anchor, self.old, self.new = target, name, anchor, old, new
        self.sentence, self.before, self.after = sentence, before, after

    def __repr__(self) -> str:
        return f"{self.target}:{self.name}"

    def span(self, src: str) -> tuple[int, int]:
        assert src.count(self.anchor) == 1, (self, "anchor is not unique in the regex source")
        assert self.anchor.count(self.old) == 1, (self, "old is not unique in its anchor")
        start = src.index(self.anchor) + self.anchor.index(self.old)
        return start, start + len(self.old)

    def mutant(self, src: str) -> str:
        self.span(src)
        return src.replace(self.anchor, self.anchor.replace(self.old, self.new))


def _verdict(sentence: str, p: str = _P, q: str = _Q) -> bool:
    cut = re.compile(q, re.IGNORECASE).sub(" ", sentence)
    return bool(re.compile(p, re.IGNORECASE).search(cut))


def _p(name, anchor, old, new, sentence, before, after):
    return Row("P", name, anchor, old, new, sentence, before, after)


def _q(name, anchor, old, new, sentence, before, after):
    return Row("Q", name, anchor, old, new, sentence, before, after)


_LB = "(?<![A-Z_])"
_RES = "(?:(?<!the result and )|(?!passes it as `baseline`))"
_KC = "(?:(?<!^(?-i:KiCad) )|(?!(?-i:is connected, but no (?:PCB|schematic) document is open)))"
_BS = chr(92)  # the tooling that wrote this file turns a typed backslash-u into the character
_SEPS = "[:;,{0}u2013{0}u2014-]".format(_BS)
_ANS = "(?:yes|true|it is|they are|so)"
_KICAD_OPEN = "KiCad is connected, but no PCB document is open"
_KICAD_LATE = "we say KiCad is connected, but no PCB document is open"
_BASE = "the board passes it as `baseline`"
_RESULT_BASE = "the result and passes it as `baseline`"

ROWS = [
    # ---- PASSES_OR_CONNECTED, the noun "passes" -----------------------------------------
    _p("lookbehind: letter or _ before passes (removed)", _LB + "(?<!with", _LB, "",
       "the board bypasses it", False, True),
    _p("lookbehind class: A-Z (any letter, case-blind)", _LB + "(?<!with", "A-Z", "",
       "the board bypasses it", False, True),
    _p("lookbehind class: _ (max_passes)", _LB + "(?<!with", "_", "",
       "the board has max_passes", False, True),
    _p("lookbehind: with more (removed)", "(?<!with more )", "(?<!with more )", "",
       "with more passes", False, True),
    _p("lookbehind: so more (removed)", "(?<!so more )", "(?<!so more )", "",
       "so more passes", False, True),
    _p("lookbehind: counted as (removed)", "(?<!counted as )", "(?<!counted as )", "",
       "counted as passes", False, True),
    _p("result-and group: lookbehind half removed", _RES, "(?<!the result and )", "",
       _RESULT_BASE, False, True),
    _p("result-and group: lookbehind half narrowed to lookbehind-positive", _RES,
       "(?<!the result and", "(?<=the result and", _BASE, True, False),
    _p("result-and group: lookbehind text loosened", _RES, "the result and )", "the result andX )",
       _RESULT_BASE, False, True),
    _p("result-and group: lookahead half removed", _RES, "(?!passes it as `baseline`)", "",
       _RESULT_BASE, False, True),
    _p("result-and group: lookahead half narrowed to lookahead-positive", _RES,
       "(?!passes it", "(?=passes it", "the result and passes them on", True, False),
    _p("result-and group: lookahead text loosened", _RES, "as `baseline`)", "as `baselineX`)",
       _RESULT_BASE, False, True),
    _p("result-and group: alternation made a conjunction", _RES, ")|(?!", ")(?!", _BASE, True, False),
    _p("passes: trailing lookahead (removed)", "passes(?![A-Z_])|", "(?![A-Z_])", "",
       "the board passesfoo", False, True),
    _p("passes: trailing lookahead class A-Z", "passes(?![A-Z_])|", "A-Z", "",
       "the board passesfoo", False, True),
    _p("passes: trailing lookahead class _", "passes(?![A-Z_])|", "_", "",
       "the board passes_foo", False, True),
    _p("top-level alternation: passes branch and connected branch (made a sequence)",
       "(?![A-Z_])|(?:(?<!^", "|", "", "the board passes", True, False),
    # ---- PASSES_OR_CONNECTED, "KiCad is connected, but no PCB|schematic document is open" ---
    _p("KiCad exclusion: first lookbehind (removed)", _KC, "(?<!^(?-i:KiCad) )", "",
       _KICAD_OPEN, False, True),
    _p("KiCad exclusion: lookbehind made positive", _KC, "(?<!^", "(?<=^", _KICAD_LATE, True, False),
    _p("KiCad exclusion: ^ anchor (removed)", _KC, "^", "", _KICAD_LATE, True, False),
    _p("KiCad exclusion: lookbehind case lock", _KC, "(?-i:KiCad)", "KiCad",
       "kicad is connected, but no PCB document is open", True, False),
    _p("KiCad exclusion: alternation made a conjunction", _KC, " )|(?!", " )(?!", _KICAD_LATE, True, False),
    _p("KiCad exclusion: lookahead made positive", _KC, "|(?!(?-i:is connected", "|(?=(?-i:is connected",
       "KiCad is connected", True, False),
    _p("KiCad exclusion: lookahead case lock", _KC, "(?-i:is connected", "(?:is connected",
       "KiCad is connected, but no pcb document is open", True, False),
    _p("KiCad exclusion: PCB alternative (removed)", _KC, "PCB|", "",
       _KICAD_OPEN, False, True),
    _p("KiCad exclusion: schematic alternative (removed)", _KC, "|schematic", "",
       "KiCad is connected, but no schematic document is open", False, True),
    _p("KiCad exclusion: ' document is open' (removed)", _KC, " document is open", "",
       "KiCad is connected, but no PCB window is open", True, False),
    _p("KiCad exclusion: ' document is open' narrowed", _KC, "document is open", "document is opened",
       _KICAD_OPEN, False, True),
    # ---- PASSES_OR_CONNECTED, "is/are (now|all|fully|then) connected" ---------------------
    _p("connected: word boundary before is/are", r")))\b(?:is", r"\b", "",
       "the axis connected", False, True),
    _p("connected: 'is' alternative", "(?:is|are)", "is|", "", "every net is connected", True, False),
    _p("connected: 'are' alternative", "(?:is|are)", "|are", "", "the nets are connected", True, False),
    _p("connected: \\s after is/are (to a space)", r"are)\s+(?:(?:now", r"\s", " ",
       "every net is\tconnected", True, False),
    _p("connected: + after is/are whitespace", r"are)\s+(?:(?:now", "+", "",
       "every net is  connected", True, False),
    _p("connected: optional adverb group", r"then)\s+)?connected", ")?connected", ")connected",
       "every net is connected", True, False),
    _p("connected: adverb 'now'", "(?:now|all", "now|", "", "every net is now connected", True, False),
    _p("connected: adverb 'all'", "now|all|fully", "all|", "", "every net is all connected", True, False),
    _p("connected: adverb 'fully'", "all|fully|then", "fully|", "",
       "every net is fully connected", True, False),
    _p("connected: adverb 'then'", "fully|then)", "|then", "", "every net is then connected", True, False),
    _p("connected: \\s after the adverb (to a space)", r"then)\s+)?connected", r"\s", " ",
       "every net is now\tconnected", True, False),
    _p("connected: + after the adverb", r"then)\s+)?connected", "+", "",
       "every net is now  connected", True, False),
    _p("connected: trailing word boundary", ")?connected" + r"\b", r"\b", "",
       "every net is connectedness", False, True),
    # ---- _QUESTION: the cut "whether <one or two words> is/are connected" -------------------
    _q("question: word boundary before whether", r"\bwhether\s+(?!or", r"\b", "",
       "somewhether pads are connected", True, False),
    _q("question: \\s after whether (to a space)", r"whether\s+(?!or", r"\s", " ",
       "whether\tpads are connected", False, True),
    _q("question: + after whether", r"whether\s+(?!or", "+", "", "whether  pads are connected", False, True),
    _q("question: + after whether loosened to *", r"whether\s+(?!or", "+", "*",
       "whethers are connected", True, False),
    _q("question: first or-guard (removed)", r"whether\s+(?!or\b)\w+", r"(?!or\b)", "",
       "whether or board is connected", True, False),
    _q("question: first or-guard boundary", r"whether\s+(?!or\b)\w+", r"or\b", "or",
       "whether orchid is connected", False, True),
    _q("question: first word \\w+ narrowed to one character", r"\w+(?:\s+(?!or", r"\w+", r"\w",
       "whether pads are connected", False, True),
    _q("question: first word \\w narrowed to letters", r"\w+(?:\s+(?!or", r"\w", "[a-z]",
       "whether n1 are connected", False, True),
    _q("question: second word group optional (removed ?)", r"\w+)?\s+(?:is", ")?", ")",
       "whether pads are connected", False, True),
    _q("question: second word group bound widened to {0,2}", r"\w+)?\s+(?:is", ")?", "){0,2}",
       "whether the whole board is connected", True, False),
    _q("question: second word group bound widened to *", r"\w+)?\s+(?:is", ")?", ")*",
       "whether the whole board is connected", True, False),
    _q("question: second word group narrowed to exactly two", r"\w+)?\s+(?:is", ")?", "){1,2}",
       "whether pads are connected", False, True),
    _q("question: \\s between the words narrowed to +", r"(?:\s+(?!or", r"\s+", r"\s",
       "whether the  board is connected", False, True),
    _q("question: \\s between the words (to a space)", r"(?:\s+(?!or", r"\s", " ",
       "whether the\tboard is connected", False, True),
    _q("question: second or-guard (removed)", r"(?:\s+(?!or\b)\w+)?", r"(?!or\b)", "",
       "whether board or is connected", True, False),
    _q("question: second or-guard boundary", r"(?:\s+(?!or\b)\w+)?", r"or\b", "or",
       "whether the orchid is connected", False, True),
    _q("question: second word \\w+ narrowed to one character", r"(?!or\b)\w+)?\s+(?:is", r"\w+", r"\w",
       "whether the board is connected", False, True),
    _q("question: \\s before is/are narrowed", r"\w+)?\s+(?:is", r"\s+", r"\s",
       "whether pads  are connected", False, True),
    _q("question: \\s before is/are (to a space)", r"\w+)?\s+(?:is", r"\s", " ",
       "whether pads\tare connected", False, True),
    _q("question: 'is' alternative", r"(?:is|are)\s+connected", "is|", "",
       "whether pads is connected", False, True),
    _q("question: 'are' alternative", r"(?:is|are)\s+connected", "|are", "",
       "whether pads are connected", False, True),
    _q("question: \\s before connected narrowed", r"are)\s+connected", r"\s+", r"\s",
       "whether pads are  connected", False, True),
    _q("question: \\s before connected (to a space)", r"are)\s+connected", r"\s", " ",
       "whether pads are\tconnected", False, True),
    _q("question: boundary after connected", r"connected\b(?!", r"\b", "",
       "whether passes are connectedly", True, False),
    # ---- _QUESTION: the answered continuation is not cut ---------------------------------
    _q("answer: negative lookahead made positive", r"connected\b(?!\s*[", "(?!", "(?=",
       "whether pads are connected", False, True),
    _q("answer: \\s before the separator (removed)", r"(?!\s*[:", r"\s*", "",
       "whether pads are connected : yes", True, False),
    _q("answer: \\s before the separator (to a space)", r"(?!\s*[:", r"\s", " ",
       "whether pads are connected\t: yes", True, False),
    _q("answer: * before the separator narrowed to ?", r"(?!\s*[:", "*", "?",
       "whether pads are connected  : yes", True, False),
    _q("answer: separator ':'", _SEPS, ":", "", "whether pads are connected: yes", True, False),
    _q("answer: separator ';'", _SEPS, ";", "", "whether pads are connected; yes", True, False),
    _q("answer: separator ','", _SEPS, ",", "", "whether pads are connected, yes", True, False),
    _q("answer: separator en dash", _SEPS, _BS + "u2013", "", "whether pads are connected – yes", True, False),
    _q("answer: separator em dash", _SEPS, _BS + "u2014", "", "whether pads are connected — yes", True, False),
    _q("answer: separator hyphen", _SEPS, "-]", "]", "whether pads are connected - yes", True, False),
    _q("answer: + after the separators narrowed", r"]+\s*(?:yes", "+", "", "whether pads are connected:; yes", True, False),
    _q("answer: \\s after the separators (removed)", r"]+\s*(?:yes", r"\s*", "",
       "whether pads are connected: yes", True, False),
    _q("answer: \\s after the separators (to a space)", r"]+\s*(?:yes", r"\s", " ",
       "whether pads are connected:\tyes", True, False),
    _q("answer: * after the separators narrowed to ?", r"]+\s*(?:yes", "*", "?",
       "whether pads are connected:  yes", True, False),
    _q("answer: 'yes'", _ANS, "yes|", "", "whether pads are connected: yes", True, False),
    _q("answer: 'true'", _ANS, "true|", "", "whether pads are connected, true", True, False),
    _q("answer: 'it is'", _ANS, "it is|", "", "whether pads are connected — it is", True, False),
    _q("answer: 'they are'", _ANS, "they are|", "", "whether pads are connected: they are", True, False),
    _q("answer: 'so'", _ANS, "|so", "", "whether pads are connected - so every net is fine", True, False),
    _q("answer: boundary after the answer", _ANS + r"\b)", r"\b", "",
       "whether pads are connected: yesterday", False, True),
]


def _ids(rows):
    return [repr(r) for r in rows]


def test_the_table_has_unique_row_names():
    assert len({repr(r) for r in ROWS}) == len(ROWS)


@pytest.mark.parametrize("row", ROWS, ids=_ids(ROWS))
def test_each_fragment_flips_exactly_its_own_sentence(row):
    src = _SOURCES[row.target]
    assert row.before != row.after, row
    # the real regexes give the row's verdict, through the same function the sweeps use
    assert _verdict(row.sentence) is row.before, row
    assert has_passes_or_connected(row.sentence) is row.before, row
    mutant = row.mutant(src)
    assert mutant != src, row
    p, q = (mutant, _Q) if row.target == "P" else (_P, mutant)
    assert _verdict(row.sentence, p, q) is row.after, (row, mutant)


def _uncovered(src: str, target: str, rows=ROWS) -> list[tuple[int, str, str]]:
    spans = [r.span(src) for r in rows if r.target == target]
    return [s for s in _sites(src) if not any(a <= s[0] < b for a, b in spans)]


@pytest.mark.parametrize("target", ["P", "Q"])
def test_every_fragment_of_the_regex_has_a_row(target):
    src = _SOURCES[target]
    assert _sites(src), "the tokeniser found no fragments"
    assert _uncovered(src, target) == [], "fragments without a row; add one to ROWS"


@pytest.mark.parametrize("row", ROWS, ids=_ids(ROWS))
def test_a_row_names_few_fragments(row):
    # a row spanning half the regex would cover everything and prove nothing
    a, b = row.span(_SOURCES[row.target])
    covered = [s for s in _sites(_SOURCES[row.target]) if a <= s[0] < b]
    assert len(covered) <= 4, (row, covered)


@pytest.mark.parametrize(
    ("target", "extra"),
    [
        ("P", "|zzz"),  # a new top-level alternative
        ("P", "(?!zzz)"),  # a new lookahead
        ("Q", "(?:yes|true)?"),  # a new optional group
        ("Q", r"\b"),  # a new anchor
    ],
)
def test_the_completeness_check_fails_on_a_fragment_without_a_row(target, extra):
    src = _SOURCES[target] + extra
    missing = _uncovered(src, target)
    assert missing, "the completeness check did not notice the new fragment"
    assert missing[0][0] >= len(_SOURCES[target])


def test_the_completeness_check_fails_on_a_new_alternative_inside_a_group():
    # an alternative added in the middle of the real source: rows around it lose their unique anchor
    mutated = _P.replace("(?:is|are)", "(?:is|are|zzz)")
    assert mutated != _P
    with pytest.raises(AssertionError):
        _uncovered(mutated, "P")
