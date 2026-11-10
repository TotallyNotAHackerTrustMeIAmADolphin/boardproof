"""A read-only view of a ``.kicad_pcb`` that parses only the top-level blocks a reader asks for.

Build ticket 153. The pure-Python S-expression parser spends its time building nodes, not
finding the end of a block: on the 71 MB vme-wren board one full ``parse_file`` is about 8 s.
Tracks, vias, zones (with their filled polygons), graphics and the per-footprint properties are
most of such a file, and a reader of footprint references, positions and pad nets, the net
table and the board outline never looks at them.

``read_board_view(path)`` finds the top-level blocks (and the children of each footprint) with
regular expressions that run in C, then parses the eager blocks (``footprint``, the ``gr_*``
outline shapes and ``net``) in ONE parse of a reduced document ``(kicad_pcb <blocks>)``. A
footprint keeps its head and only the children the readers use (see ``_eager_text``). The nodes
are built by the same parser from the same text, so they equal the nodes of a full parse where
they are kept (the equality tests compare them on real boards). Copper (``segment``, ``arc``,
``via``, ``zone``) is parsed only when ``copper_children()`` is called.

Two block finders give the same answer: a nested-regex one (fast) and a token loop (slow,
exact). The fast one declines anything it does not match whole (a comment character at a token
start, a block nested deeper than the unrolled pattern, an unterminated string), and the loop
then decides. Anything the loop is not sure about (a comment character outside a string, an
unterminated string, unbalanced parentheses, content before or after the root, a root that is not
``kicad_pcb``) makes the reader parse the whole file instead, so the answer is the full
parse's, errors included. The view holds parsed nodes of one read; nothing is cached across
calls, so a changed file can never be served stale.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any

# The Edge.Cuts shapes the import measures; schematic_import._outline_bbox reads this same tuple.
OUTLINE_SHAPES = ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly", "gr_curve")
EAGER = frozenset({"footprint", "net", *OUTLINE_SHAPES})
COPPER = frozenset({"segment", "arc", "via", "zone"})
FOOTPRINT_KEEP = frozenset({"at", "fp_text", "pad"})  # plus the Reference property, see _eager_text

# Lexical classes of kicad_tools' own parser: a quoted string (a backslash escapes the next
# character), an atom (starts with none of whitespace ( ) " # ;), whitespace, and a comment (# or ;
# at the start of a token) which neither finder accepts.
_STR = r'"(?:[^"\\]|\\.)*+"'
_ATOM = r"[^ \t\n\r()\"#;][^ \t\n\r()]*+"
_WS = r"[ \t\n\r]++"
_NEST = 14  # how many levels of parentheses one fast match may span below a top-level block


def _unrolled(depth: int) -> str:
    inner = r"\((?:" + _WS + "|" + _STR + "|" + _ATOM + r")*+\)"
    for _ in range(depth):
        inner = r"\((?:" + _WS + "|" + _STR + "|" + _ATOM + "|" + inner + r")*+\)"
    return inner


_BLOCK = re.compile(_unrolled(_NEST), re.DOTALL)
_ITEM = re.compile(r"[ \t\n\r]*+(?:(\()|(\))|" + _STR + "|" + _ATOM + ")", re.DOTALL)
_TOKEN = re.compile(r'(\()|(\))|"(?:[^"\\]|\\.)*"|([#;"])', re.DOTALL)
_HAS_AT = re.compile(r"\(\s*at[\s)]")
_HEAD = re.compile(r"[ \t\n\r]*([^ \t\n\r()\"#;]+)")
_PROP_NAME = re.compile(r'\(property[ \t\n\r]+(?:"([^"\\]*)"|([^ \t\n\r()"#;\\]+)(?=[ \t\n\r(]))')
_LEADING = re.compile(r"[ \t\n\r]*\Z")
_TRAILING = re.compile(r"[ \t\n\r]*\Z")

Block = tuple[str, int, int, "list[tuple[int, int]] | None"]


def _parse_text(text: str) -> Any:
    from kicad_tools.sexp import parse_string

    return parse_string(text)


def _children_fast(text: str, pos: int) -> tuple[list[tuple[int, int]], int] | None:
    """Spans of the list children of the list whose head atom starts at ``pos`` (just after its
    opening parenthesis), and the position after its closing parenthesis; None when a child is
    not matched whole."""
    out: list[tuple[int, int]] = []
    head = _ITEM.match(text, pos)
    if head is None or head.lastindex is not None:
        return None
    pos = head.end()
    while True:
        m = _ITEM.match(text, pos)
        if m is None:
            return None
        kind = m.lastindex
        if kind == 2:
            return out, m.end()
        if kind == 1:
            start = m.end() - 1
            b = _BLOCK.match(text, start)
            if b is None:
                return None
            out.append((start, b.end()))
            pos = b.end()
        else:
            pos = m.end()


def _split_fast(text: str) -> tuple[str, list[Block]] | None:
    m = _ITEM.match(text, 0)
    if m is None or m.lastindex != 1:
        return None
    head = _HEAD.match(text, m.end())
    if head is None:
        return None
    root_name = head.group(1)
    top = _children_fast(text, m.end())
    if top is None or _TRAILING.match(text, top[1]) is None:
        return None
    blocks: list[Block] = []
    for start, end in top[0]:
        h = _HEAD.match(text, start + 1)
        name = h.group(1) if h else ""
        kids = None
        if name == "footprint":
            sub = _children_fast(text, start + 1)
            if sub is None:
                return None
            kids = sub[0]
        blocks.append((name, start, end, kids))
    return root_name, blocks


def _split_slow(text: str) -> tuple[str, list[Block]] | None:
    """The exact token loop: ``(root name, blocks)`` or None when the text is not a plain
    document this tokenizer fully understands."""
    depth = 0
    blocks: list[Block] = []
    open_at = -1
    child_at = -1
    root_name = ""
    block_name = ""
    kids: list[tuple[int, int]] | None = None
    for m in _TOKEN.finditer(text):
        kind = m.lastindex
        if kind == 1:
            depth += 1
            if depth == 3:
                child_at = m.start()
            elif depth == 2:
                open_at = m.start()
                head = _HEAD.match(text, m.end())
                block_name = head.group(1) if head else ""
                kids = [] if block_name == "footprint" else None
            elif depth == 1:
                head = _HEAD.match(text, m.end())
                if head is None or _LEADING.match(text, 0, m.start()) is None:
                    return None
                root_name = head.group(1)
        elif kind == 2:
            depth -= 1
            if depth == 2:
                if kids is not None:
                    kids.append((child_at, m.end()))
            elif depth == 1:
                blocks.append((block_name, open_at, m.end(), kids))
            elif depth == 0:
                if _TRAILING.match(text, m.end()) is None:
                    return None
        elif kind == 3:
            return None
    if depth != 0 or not root_name:
        return None
    return root_name, blocks


def split_top_level(text: str) -> tuple[str, list[Block]] | None:
    """``(root name, [(block name, start, end, children), ...])`` for a document
    ``(root block block ...)``, or None when the text is not a plain document this tokenizer
    fully understands. ``children`` is, for a ``footprint`` block, the ``(start, end)`` of each of
    its list children in order (so a reader can drop the ones it does not need), else None."""
    fast = _split_fast(text)
    return fast if fast is not None else _split_slow(text)


class BoardView:
    """``children`` are the parsed eager blocks in file order (a full parse's children when the
    fast reader declined); ``copper_children()`` the parsed copper blocks in file order."""

    def __init__(self, children: list[Any], copper: Any, fast: bool) -> None:
        self.children = children
        self._copper = copper
        self.fast = fast

    def copper_children(self) -> list[Any]:
        if callable(self._copper):
            self._copper = self._copper()
        return self._copper


def _eager_text(text: str, name: str, start: int, end: int, kids: list[tuple[int, int]] | None) -> str:
    """The text of one eager block. A footprint keeps its head (the opening parenthesis, the name
    and the library id) and the children a reader of footprint facts uses, in order: ``at``,
    ``fp_text``, ``pad`` and the ``Reference`` property (every other property is dropped only
    when its quoted or bare name says so). It is kept whole when a dropped child before the first
    direct ``at`` child (or any dropped child, if there is none) holds an ``(at`` anywhere inside:
    ``find("at")`` is a pre-order search, so such a child could change which ``at`` a reader gets."""
    if kids is None or not kids:
        return text[start:end]
    kept: list[str] = []
    seen_at = False
    for cs, ce in kids:
        head = _HEAD.match(text, cs + 1)
        child = head.group(1) if head is not None else ""
        if child == "at":
            seen_at = True
            kept.append(text[cs:ce])
        elif child in FOOTPRINT_KEEP:
            kept.append(text[cs:ce])
        elif child == "property" and not _other_property(text, cs):
            kept.append(text[cs:ce])
        elif not seen_at and _HAS_AT.search(text, cs, ce) is not None:
            return text[start:end]
    return text[start : kids[0][0]] + chr(10).join(kept) + ")"


def _other_property(text: str, start: int) -> bool:
    """True when the property block at ``start`` is positively named something but ``Reference``."""
    m = _PROP_NAME.match(text, start)
    return m is not None and (m.group(1) if m.group(1) is not None else m.group(2)) != "Reference"


def view_of_text(text: str) -> BoardView:
    """The view of already-read board text (the tests drive this directly)."""
    split = split_top_level(text)
    if split is None or split[0] != "kicad_pcb":
        root = _parse_text(text)
        kids = list(root.children)
        return BoardView(kids, [c for c in kids if c.name in COPPER], fast=False)
    _, blocks = split
    eager = [_eager_text(text, name, s, e, kids) for name, s, e, kids in blocks if name in EAGER]
    copper = [text[s:e] for name, s, e, _ in blocks if name in COPPER]

    def doc(parts: Iterable[str]) -> str:
        return "(kicad_pcb " + chr(10).join(parts) + ")"

    def load_copper() -> list[Any]:
        return list(_parse_text(doc(copper)).children) if copper else []

    kids = list(_parse_text(doc(eager)).children) if eager else []
    return BoardView(kids, load_copper, fast=True)


def read_board_view(path: str | Path) -> BoardView:
    """Read ``path`` once (the same decoding as ``parse_file``) and return its view."""
    return view_of_text(Path(path).read_text(encoding="utf-8"))
