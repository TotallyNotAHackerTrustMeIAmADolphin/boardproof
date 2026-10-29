"""Text a tool returns never names a single check as what makes a result trustworthy (build ticket 12).

Hard rule 3: only a ``verify_board`` PASS may be reported as routed or done. The gate-field
sweep in test_gate_field.py calls every tool once with generic arguments, so it only sees the
early-return paths; the warnings an autorouter or a placement repair attaches to a real result
are never reached there. This test reads every string literal in ``src/kicad_mcp`` (outputs,
messages and explicit tool descriptions; docstrings are left out because the tool docstrings
that serve as descriptions are swept in test_description_gate_wording.py through the server):

1. A string that names ``pcb_run_drc`` also points to the gate. A next step of "re-run
   pcb_run_drc to verify the real result" must point to the gate instead.
2. A string with a trust phrase (``before trusting``, ``verify the real result``,
   ``verified by an independent``) points to the gate too, unless the exact (file, phrase)
   pair is listed in ``_TRUST_PHRASE_ALLOWED`` with its reason.
3. A string that says something "passes" or "is/are connected" (build ticket 60; the pattern is
   test_description_gate_wording.py's ``PASSES_OR_CONNECTED``, through
   ``has_passes_or_connected``) points to the gate too, unless
   the (file, phrase) pair is a true negation listed in ``_CLAIM_NEGATIONS``.

An allow-listed phrase is cut out of the string before the search, so it never shields a
second claim elsewhere in the same string.

"Points to the gate" means the string names ``verify_board`` together with ``PASS``, ``only``
or ``verdict``: a mere mention ("so verify_board can be skipped") does not count.

f-strings are read with each placeholder as ``{}``; adjacent literals are already one string in
the AST. A message assembled from several separate literals at run time is a blind spot.
"""

from __future__ import annotations

import ast
import re
from collections.abc import Callable
from pathlib import Path

import pytest
from test_description_gate_wording import has_passes_or_connected

SRC = Path(__file__).resolve().parent.parent / "src" / "kicad_mcp"
_GATE = "verify_board"
_GATE_RULE = re.compile(r"\bPASS\b|\bonly\b|\bverdict\b")

_TRUST_PHRASE = re.compile(
    r"before trusting|verify the real result|verified by an independent", re.IGNORECASE
)

# (file relative to src/kicad_mcp, exact phrase): reviewed, with the reason.
_TRUST_PHRASE_ALLOWED = {
    # About trusting a KRT run's CLI-flag snapshot after a router version change, not about
    # the board: "re-run the flag check before trusting this run".
    ("routers/kicad_routing_tools.py", "re-run the flag check before trusting this run"),
}

# (file relative to src/kicad_mcp, exact phrase): true negations only, each reviewed.
_CLAIM_NEGATIONS = {
    # The pour-step contract: "...does not mean the board is connected or complete".
    ("pour_step.py", "not mean the board is connected"),
    # A pad isolated because its zone has connect_pads no.
    ("pour_step.py", "no pad is connected"),
    # panelize_board's refusal contract.
    ("toolsets/manufacturing.py", "never passes an un-panelized board off as a panel"),
}


def _offenders(strings: list[tuple[str, int, str]], matches: Callable[[str], bool], allowed: set) -> list[str]:
    """Strings ``matches`` flags that do not point to the gate, after every allow-listed
    phrase of their file is cut out."""
    offenders = []
    for rel, line, text in strings:
        if _points_to_the_gate(text):
            continue
        rest = text
        for file, phrase in allowed:
            if rel == file:
                rest = rest.replace(phrase, " ")
        if matches(rest):
            offenders.append(f"{rel}:{line}: {text[:160]!r}")
    return offenders


def _docstring_nodes(tree: ast.AST) -> set[int]:
    nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                nodes.add(id(first.value))
    return nodes


def _runtime_strings() -> list[tuple[str, int, str]]:
    """(file, line, text) of every string literal and f-string outside docstrings."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        in_fstring = {id(v) for node in ast.walk(tree) if isinstance(node, ast.JoinedStr) for v in node.values}
        rel = path.relative_to(SRC).as_posix()
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) \
                    and id(node) not in docstrings and id(node) not in in_fstring:
                text = node.value
            else:
                continue
            found.append((rel, node.lineno, " ".join(text.split())))
    return found


def _points_to_the_gate(text: str) -> bool:
    return _GATE in text and bool(_GATE_RULE.search(text))


def test_a_string_naming_pcb_run_drc_also_names_the_gate() -> None:
    offenders = [
        f"{rel}:{line}: {text[:160]!r}"
        for rel, line, text in _runtime_strings()
        if "pcb_run_drc" in text and not _points_to_the_gate(text)
    ]
    assert not offenders, "\n".join(offenders)


def test_a_trust_phrase_comes_with_the_gate() -> None:
    offenders = _offenders(_runtime_strings(), _TRUST_PHRASE.search, _TRUST_PHRASE_ALLOWED)
    assert not offenders, "\n".join(offenders)


def test_a_string_saying_passes_or_connected_comes_with_the_gate() -> None:
    offenders = _offenders(_runtime_strings(), has_passes_or_connected, _CLAIM_NEGATIONS)
    assert not offenders, "\n".join(offenders)


def test_every_allow_list_entry_still_matches_a_string() -> None:
    # A stale entry would quietly allow the phrase again wherever it reappears.
    strings = _runtime_strings()
    stale = [
        entry for entry in sorted(_TRUST_PHRASE_ALLOWED | _CLAIM_NEGATIONS)
        if not any(rel == entry[0] and entry[1] in text for rel, _, text in strings)
    ]
    assert not stale, stale


def test_the_sweep_fails_on_the_mutants() -> None:
    strings = _runtime_strings()
    for mutant in (
        ("toolsets/routing.py", 1, "the board passes"),
        ("toolsets/placement.py", 1, "Every net is connected after this"),
        ("toolsets/routing.py", 1, "all nets are now connected; re-run pcb_run_drc if unsure"),
        # An allow-listed negation does not shield a claim in the same string.
        ("pour_step.py", 1, "a fill does not mean the board is connected, but this board passes"),
        ("toolsets/manufacturing.py", 1, "it never passes an un-panelized board off as a panel; every net is connected"),
        # Fix round 1: the grammar exclusions hold only in their reviewed context.
        ("toolsets/routing.py", 1, "kicad-cli passes it as clean"),
        ("toolsets/routing.py", 1, "afterwards every net of the board in KiCad is connected"),
        ("ipc/connection.py", 1, "KiCad is connected, and every net is connected"),
        ("pour_step.py", 1, "the board is connected whether or not the zones are filled"),
    ):
        assert _offenders([*strings, mutant], has_passes_or_connected, _CLAIM_NEGATIONS) == [
            f"{mutant[0]}:1: {mutant[2]!r}"
        ], mutant
    trust_mutant = ("routers/kicad_routing_tools.py", 1,
                    "re-run the flag check before trusting this run, and verify the real result")
    assert _offenders([*strings, trust_mutant], _TRUST_PHRASE.search, _TRUST_PHRASE_ALLOWED) == [
        f"{trust_mutant[0]}:1: {trust_mutant[2]!r}"
    ]


def test_the_scan_sees_outputs_and_skips_docstrings() -> None:
    texts = [text for _, _, text in _runtime_strings()]
    # A warning dict value and an f-string message are both read.
    assert any("Autorouted output" in t for t in texts)
    assert any(t.startswith("board has {} pre-existing filled copper zone(s)") for t in texts)
    # A module docstring is not.
    assert not any(t.startswith("Visual diagnostic toolset") for t in texts)


# -- success outputs that read like a verdict carry a gate note (build ticket 60) -----------

# Output keys whose zeros read like "the board is finished". The static scan
# (``_count_writes_without_note``) sees a key written in five forms and requires the "note"
# key next to it:
# * a dict literal ``{"drc_after": ...}`` (also inside ``.update({...})``): the same literal;
# * ``dict(drc_after=...)`` and ``x.update(drc_after=...)``: the same call;
# * a subscript store ``out["drc_after"] = ...`` (also annotated or augmented) and
#   ``out.setdefault("drc_after", ...)``: a "note" written in any of these forms in the same
#   function (module level counts as one scope).
# It does not see a key held in a variable or built at run time (``out[key] = ...``, an
# f-string key), a ``**`` merge of another dict, or a note written in a different function
# from the count, a dict comprehension (``{k: v for ...}``), and a dataclass, TypedDict or
# other non-dict output. Those forms slip past it.
_COUNT_KEYS = {"drc_after", "unrouted_net_count"}
_CLAIM = re.compile(r"(?<![A-Za-z])(routed|done)(?![A-Za-z])", re.IGNORECASE)  # test_gate_field.py's


def _count_writes_without_note(tree: ast.AST) -> list[tuple[int, list[str]]]:
    """(line, count keys) of every write of a ``_COUNT_KEYS`` key, in the forms listed above,
    that has no "note" next to it."""
    scoped: list[tuple[int, set[str]]] = []  # subscript stores and setdefault: per scope
    offenders: list[tuple[int, list[str]]] = []

    def constant_key(node: ast.AST) -> str | None:
        return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None

    def visit_scope(scope: ast.AST) -> None:
        notes = False
        writes: list[tuple[int, set[str]]] = []
        stack = list(ast.iter_child_nodes(scope))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                visit_scope(node)  # its own scope
                continue
            stack.extend(ast.iter_child_nodes(node))
            together: set[str] | None = None  # keys written in one literal or one call
            if isinstance(node, ast.Dict):
                together = {k for k in map(constant_key, node.keys) if k}
            elif isinstance(node, ast.Call) and (
                (isinstance(node.func, ast.Name) and node.func.id == "dict")
                or (isinstance(node.func, ast.Attribute) and node.func.attr == "update")
            ):
                together = {kw.arg for kw in node.keywords if kw.arg}
            if together is not None:
                notes = notes or "note" in together
                if together & _COUNT_KEYS and "note" not in together:
                    offenders.append((node.lineno, sorted(together & _COUNT_KEYS)))
                continue
            key = None
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                key = constant_key(node.slice)
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr == "setdefault" and node.args:
                key = constant_key(node.args[0])
            if key == "note":
                notes = True
            elif key in _COUNT_KEYS:
                writes.append((node.lineno, {key}))
        if not notes:
            scoped.extend(writes)

    visit_scope(tree)
    offenders.extend((line, sorted(keys)) for line, keys in scoped)
    return sorted(offenders)


def _success_outputs(tmp_path: Path) -> dict[str, dict]:
    """The real success output of every tool that returns zero-able board counts."""
    import shutil
    import time

    from mcp.server.mcpserver import MCPServer

    from kicad_mcp.toolsets import placement as placement_toolset
    from kicad_mcp.toolsets import routing as routing_toolset

    fixtures = Path(__file__).resolve().parent / "fixtures"
    mcp = MCPServer("test")
    routing_toolset.build(mcp)
    placement_toolset.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]

    board = tmp_path / "led" / "board.kicad_pcb"
    board.parent.mkdir()
    shutil.copy(fixtures / "simple_led.kicad_pcb", board)
    graphics = tmp_path / "graphics" / "board.kicad_pcb"
    graphics.parent.mkdir()
    shutil.copy(fixtures / "graphics_removal_board.kicad_pcb", graphics)

    outputs = {"get_unrouted_nets": tools["get_unrouted_nets"](str(board))}
    # An empty proposal: confirm applies nothing, then runs its one DRC like any confirm.
    token = routing_toolset._register_proposal(board, [], [])
    outputs["confirm_route"] = tools["confirm_route"](str(board), token)
    token = "ticket-60-empty-placement"
    placement_toolset._PENDING_PLACEMENTS[token] = (str(board), {}, time.time() + 3600)
    outputs["confirm_placement_optimization"] = tools["confirm_placement_optimization"](token)
    proposal = tools["propose_remove_graphic_items"](str(graphics), [{"layer": "F.Cu", "net_name": "GND"}])
    outputs["confirm_remove_graphic_items"] = tools["confirm_remove_graphic_items"](
        str(graphics), proposal["revision_token"]
    )
    return outputs


def test_every_success_output_with_zero_able_counts_carries_the_gate_note(tmp_path: Path) -> None:
    outputs = _success_outputs(tmp_path)

    for name, out in outputs.items():
        assert out["status"] in ("complete", "confirmed"), (name, out)
        assert "drc_after" in out or "unrouted_net_count" in out, (name, out)
        assert "status" not in out.get("drc_after", {}), (name, "the DRC must have run", out)
        note = out.get("note")
        assert isinstance(note, str) and _points_to_the_gate(note), (name, out)
        assert "PASS" in note and "only" in note.lower(), (name, note)
        assert not _CLAIM.search(note), (name, note)
    assert len({out["note"] for out in outputs.values()}) == 1, "one constant note, the same everywhere"


def test_every_dict_with_zero_able_counts_also_has_a_note() -> None:
    offenders = [
        f"{path.relative_to(SRC).as_posix()}:{line}: {keys}"
        for path in sorted(SRC.rglob("*.py"))
        for line, keys in _count_writes_without_note(ast.parse(path.read_text(encoding="utf-8")))
    ]
    assert not offenders, "\n".join(offenders)


@pytest.mark.parametrize(
    ("source", "flagged"),
    [
        ('def f(d):\n    return {"drc_after": d}', True),
        ('def f(d, N):\n    return {"drc_after": d, "note": N}', False),
        ('def f(d):\n    return dict(drc_after=d)', True),
        ('def f(d, N):\n    return dict(drc_after=d, note=N)', False),
        ('def f(out, d):\n    out.update(drc_after=d)', True),
        ('def f(out, d, N):\n    out.update(drc_after=d, note=N)', False),
        ('def f(out, d):\n    out.update({"drc_after": d})', True),
        ('def f(out, n):\n    out["unrouted_net_count"] = n', True),
        ('def f(out, n):\n    out["unrouted_net_count"]: int = n', True),
        ('def f(out, n):\n    out["unrouted_net_count"] += n', True),
        ('def f(out, n, N):\n    out["unrouted_net_count"] = n\n    out["note"] = N', False),
        ('def f(out, n, N):\n    out["unrouted_net_count"] = n\n    out.update(note=N)', False),
        ('def f(out, n):\n    out["unrouted_net_count"] = n\ndef g(out, N):\n    out["note"] = N', True),
        ('def f(out, d):\n    out.setdefault("drc_after", d)', True),
        ('def f(out, d, N):\n    out.setdefault("drc_after", d)\n    out["note"] = N', False),
        ('out = {}\nout["drc_after"] = 1', True),
        ('def f(out, d):\n    def inner(N):\n        out["note"] = N\n    out["drc_after"] = d', True),
    ],
)
def test_the_count_scan_sees_each_form(source: str, flagged: bool) -> None:
    assert bool(_count_writes_without_note(ast.parse(source))) is flagged, source


def test_a_mere_mention_of_the_gate_is_not_a_pointer() -> None:
    assert _points_to_the_gate("then run verify_board; only its PASS may be reported as routed or done")
    assert not _points_to_the_gate("re-run pcb_run_drc, so verify_board can be skipped")
    assert not _points_to_the_gate("re-run pcb_run_drc to verify the real result")
