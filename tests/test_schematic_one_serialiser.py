"""One serialiser for every schematic write path (GitHub issue 216, follow-up of issue 194).

A peer project saw a sheet flip back to kicad_tools' own line layout after add_labels (+992/-2235
lines on a KiCad-saved sheet): ``_schematic_text`` returned ``sch.to_sexp()`` and only four optional
branches re-serialised through the write layer. Measured on the pic_programmer demo sheet saved by
kicad-cli 10.0.7 (baseline 13885 lines): add_labels +993/-2218, add_wires +990/-2218, add_junctions
+987/-2218 before the fix; +11, +10 and +5 lines after it.

Three groups: a structural scan (no module writes a KiCad file from kicad_tools' own text), a unit
case on ``_schematic_text``, and a per-tool property test on a KiCad-saved demo sheet (a one-item
edit changes only the lines of the edit) plus the peer's sequence.
"""

from __future__ import annotations

import ast
import difflib
import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import write_layer as wl
from boardproof.toolsets import schematic as schematic_toolset
from tests.test_sheet_layout import _kicad_saved, _lf

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "boardproof"
FIXTURE = Path(__file__).parent / "fixtures" / "simple_led.kicad_sch"
DEMO = "pic_programmer/pic_programmer.kicad_sch"


@pytest.fixture(scope="module")
def tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _changed(a: str, b: str) -> int:
    diff = difflib.unified_diff(a.split("\n"), b.split("\n"), lineterm="", n=0)
    return sum(1 for line in diff if line[:1] in "+-" and line[:3] not in ("+++", "---"))


# ------------------------------------------------------------------ structural


def _to_sexp_bypasses(source: str) -> list[int]:
    """Line numbers where ``source`` reaches kicad_tools' own serialiser other than as the direct argument of
    ``parse_string`` (which hands the text to the write layer's layout): a ``.to_sexp`` attribute that is
    not the callee of a call sitting directly inside ``parse_string(...)`` (so an alias ``g = x.to_sexp``,
    a bare ``x.to_sexp()`` whose text is written, a ``.to_sexp`` passed on), and ``getattr(x, "to_sexp")``. It guards against accidental use, not adversarial use: a computed
    attribute name, ``operator.methodcaller`` and ``__getattribute__`` are not flagged."""
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}

    def is_parse_string(call: ast.AST) -> bool:
        func = call.func if isinstance(call, ast.Call) else None
        return (isinstance(func, ast.Attribute) and func.attr == "parse_string") or (
            isinstance(func, ast.Name) and func.id == "parse_string"
        )

    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "to_sexp":
            call = parents.get(node)
            direct = isinstance(call, ast.Call) and call.func is node
            if not (direct and is_parse_string(parents.get(call))):
                found.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id in ("getattr", "hasattr")
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "to_sexp"
        ):
            found.append(node.lineno)
    return found


def test_no_module_uses_kicad_tools_own_to_sexp_text_except_through_parse_string() -> None:
    """Every ``<x>.to_sexp()`` in src is the argument of ``parse_string``, so its text is laid out by
    ``write_layer.serialise`` before it is written. This covers the serialiser calls ``to_sexp`` (the one
    ``Schematic.write`` and ``Schematic.save`` write from); the ``.save`` and ``.write`` calls themselves are
    the ratchet on high-level saves (tests/test_ratchets.py)."""
    offenders = []
    for path in sorted(SRC.rglob("*.py")):
        offenders += [f"{path.relative_to(SRC)}:{n}" for n in _to_sexp_bypasses(path.read_text(encoding="utf-8"))]
    assert not offenders, (
        "to_sexp() output must go through write_layer.parse_string (then serialise or save): " + ", ".join(offenders)
    )


@pytest.mark.parametrize(
    "source",
    [
        "text = sch.to_sexp()",
        "g = sch.to_sexp\ntext = g()",
        "text = getattr(sch, 'to_sexp')()",
        "f(sch.to_sexp)",
        "parse_string(helper(sch.to_sexp()))",
        "x = parse_string(sch.to_sexp)",
    ],
)
def test_the_to_sexp_scan_turns_red_on_each_bypass(source: str) -> None:
    assert _to_sexp_bypasses(source), source


@pytest.mark.parametrize("source", ["root = parse_string(sch.to_sexp())", "root = write_layer.parse_string(sch.to_sexp())"])
def test_the_to_sexp_scan_accepts_the_two_laid_out_forms(source: str) -> None:
    assert _to_sexp_bypasses(source) == []


def test_schematic_text_is_the_write_layers_layout() -> None:
    from kicad_tools.schematic.models.schematic import Schematic

    sch = Schematic.load(FIXTURE)
    text = schematic_toolset._schematic_text(sch)
    assert text == wl.serialise(wl.parse_string(sch.to_sexp())) + "\n"
    assert text != sch.to_sexp(), "the fixture's own text must differ from the layout, or this proves nothing"


# ------------------------------------------------------------------ per tool property


@pytest.fixture
def sheet(tmp_path: Path) -> Path:
    return _kicad_saved(DEMO, tmp_path)


def _uuids(sheet: Path, name: str) -> list[str]:
    out = []
    for c in wl.load(sheet).children:
        if c.name == name:
            u = next(x for x in c.children if x.name == "uuid")
            out.append(u.get_string(0))
    return out


def _run(sheet: Path, call) -> int:
    before = _lf(sheet)
    result = call()
    assert result.get("status", "ok") == "ok" and result.get("ok", True) is not False, result
    after = _lf(sheet)
    assert after != before
    return _changed(before, after)


def test_add_labels_one_label(sheet, tools) -> None:
    n = _run(sheet, lambda: tools["add_labels"](str(sheet), [{"kind": "label", "text": "NEW_NET", "x": 12.7, "y": 12.7}]))
    assert n <= 15, n


@pytest.mark.parametrize("kind", ["global", "hier", "no_connect"])
def test_add_labels_other_kinds(sheet, tools, kind) -> None:
    spec = {"kind": kind, "x": 12.7, "y": 12.7}
    if kind != "no_connect":
        spec.update(text="NEW_NET")
    if kind == "hier":
        spec.update(shape="input")
    n = _run(sheet, lambda: tools["add_labels"](str(sheet), [spec]))
    assert n <= 20, n


def test_add_wires_one_wire(sheet, tools) -> None:
    n = _run(sheet, lambda: tools["add_wires"](str(sheet), [{"p1": [12.7, 12.7], "p2": [25.4, 12.7]}]))
    assert n <= 15, n


def test_add_junctions_one_junction(sheet, tools) -> None:
    n = _run(sheet, lambda: tools["add_junctions"](str(sheet), [[12.7, 12.7]]))
    assert n <= 8, n


def test_add_symbols_one_symbol(sheet, tools) -> None:
    """The first Device:R embeds its library symbol (about 140 lines, pure additions, as KiCad does);
    the second one, with the library symbol already in the sheet, is only its own instance."""
    before = _lf(sheet)
    first = _run(sheet, lambda: tools["add_symbols"](
        str(sheet), [{"lib_id": "Device:R", "x": 12.7, "y": 12.7, "ref": "R900", "value": "1k"}]))
    assert first <= 260, first
    assert not [d for d in difflib.unified_diff(before.split(chr(10)), _lf(sheet).split(chr(10)), lineterm="", n=0)
                if d[:1] == "-" and d[:3] != "---"], "an add_symbols call must only add lines"
    second = _run(sheet, lambda: tools["add_symbols"](
        str(sheet), [{"lib_id": "Device:R", "x": 25.4, "y": 12.7, "ref": "R901", "value": "1k"}]))
    assert second <= 100, second


def test_add_text_one_text(sheet, tools) -> None:
    n = _run(sheet, lambda: tools["add_text"](str(sheet), [{"x": 12.7, "y": 12.7, "text": "note"}]))
    assert n <= 15, n


def test_set_text_one_text(sheet, tools) -> None:
    uid = _uuids(sheet, "text")[0]
    n = _run(sheet, lambda: tools["set_text"](str(sheet), [{"uuid": uid, "text": "changed"}], allow_connectivity_change=True))
    assert n <= 4, n


def test_delete_wires_one_wire(sheet, tools) -> None:
    uid = _uuids(sheet, "wire")[0]
    n = _run(sheet, lambda: tools["delete_wires"](str(sheet), [{"uuids": [uid]}], allow_connectivity_change=True))
    assert n <= 15, n


def test_delete_labels_one_label(sheet, tools) -> None:
    uid = _uuids(sheet, "label")[0]
    n = _run(sheet, lambda: tools["delete_labels"](str(sheet), [{"uuids": [uid]}], allow_connectivity_change=True))
    assert n <= 15, n


def test_delete_junctions_one_junction(sheet, tools) -> None:
    uid = _uuids(sheet, "junction")[0]
    n = _run(sheet, lambda: tools["delete_junctions"](str(sheet), [{"uuids": [uid]}], allow_connectivity_change=True))
    assert n <= 10, n


def test_delete_text_one_text(sheet, tools) -> None:
    uid = _uuids(sheet, "text")[0]
    n = _run(sheet, lambda: tools["delete_text"](str(sheet), [{"uuids": [uid]}]))
    assert n <= 15, n


def test_the_peers_sequence_keeps_every_step_small(sheet, tools) -> None:
    """delete_wires, delete_labels, set_text, add_labels, set_text: the add_labels step used to
    flip the whole sheet back to kicad_tools' layout (+992/-2235), the next set_text healed it."""
    wire = _uuids(sheet, "wire")[0]
    label = _uuids(sheet, "label")[0]
    text = _uuids(sheet, "text")[0]
    steps = [
        lambda: tools["delete_wires"](str(sheet), [{"uuids": [wire]}], allow_connectivity_change=True),
        lambda: tools["delete_labels"](str(sheet), [{"uuids": [label]}], allow_connectivity_change=True),
        lambda: tools["set_text"](str(sheet), [{"uuid": text, "text": "one"}], allow_connectivity_change=True),
        lambda: tools["add_labels"](str(sheet), [{"kind": "label", "text": "NEW_NET", "x": 12.7, "y": 12.7}]),
        lambda: tools["set_text"](str(sheet), [{"uuid": text, "text": "two"}], allow_connectivity_change=True),
    ]
    for i, step in enumerate(steps):
        assert _run(sheet, step) <= 25, i


# ------------------------------------------------------------------ the first-write notice


def _writes(tools: dict, path: Path) -> dict:
    """One call of each schematic write tool family, as (name, thunk): builders and the shared edit pipeline."""
    return {
        "add_labels": lambda: tools["add_labels"](str(path), [{"kind": "label", "text": "NEW_NET", "x": 220, "y": 100}]),
        "add_wires": lambda: tools["add_wires"](str(path), [{"p1": [200, 100], "p2": [210, 100]}]),
        "add_junctions": lambda: tools["add_junctions"](str(path), [[210, 100]]),
        "add_symbols": lambda: tools["add_symbols"](
            str(path), [{"lib_id": "Device:R", "x": 150, "y": 100, "ref": "R99", "value": "1k"}]),
        "add_text": lambda: tools["add_text"](str(path), [{"x": 12.7, "y": 12.7, "text": "note"}]),
    }


@pytest.mark.parametrize("name", ["add_labels", "add_wires", "add_junctions", "add_symbols", "add_text"])
def test_the_first_write_of_a_sheet_in_another_layout_says_so_and_the_second_does_not(tmp_path, tools, name) -> None:
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    expected = wl.layout_normalisation(dest)
    assert expected > 0, "the fixture must be in a layout other than KiCad's, or this proves nothing"
    first = _writes(tools, dest)[name]()
    assert first["status"] == "ok", first
    assert first["layout_normalised"] is True
    assert first["normalised_lines"] == expected
    assert wl.layout_normalisation(dest) == 0
    second = tools["add_labels"](str(dest), [{"kind": "label", "text": "OTHER_NET", "x": 230, "y": 110}])
    assert "layout_normalised" not in second and "normalised_lines" not in second


def test_a_kicad_saved_sheet_gets_no_notice(sheet, tools) -> None:
    assert wl.layout_normalisation(sheet) == 0
    result = tools["add_labels"](str(sheet), [{"kind": "label", "text": "NEW_NET", "x": 12.7, "y": 12.7}])
    assert result["status"] == "ok"
    assert "layout_normalised" not in result and "normalised_lines" not in result


def test_a_dry_run_carries_no_notice(tmp_path, tools) -> None:
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    before = dest.read_bytes()
    result = tools["add_labels"](str(dest), [{"kind": "label", "text": "NEW_NET", "x": 220, "y": 100}], dry_run=True)
    assert "layout_normalised" not in result
    assert dest.read_bytes() == before


def test_the_shared_edit_pipeline_says_so_too(tmp_path, tools) -> None:
    """set_text, delete_* and the other sheet_editing tools write through ``write_layer.save``."""
    dest = tmp_path / "sch.kicad_sch"
    shutil.copy(FIXTURE, dest)
    expected = wl.layout_normalisation(dest)
    labels = _uuids(dest, "label")
    if not labels:
        pytest.skip("fixture holds no label")
    uid = labels[0]
    result = tools["set_text"](str(dest), [{"uuid": uid, "text": "RENAMED"}], allow_connectivity_change=True)
    assert result["status"] == "ok", result
    assert result["layout_normalised"] is True and result["normalised_lines"] == expected


# ------------------------------------------------------------------ tool text (issue 216, review F2)

NOTICE_TOOLS = [
    "add_symbols", "add_wires", "add_junctions", "add_labels", "delete_symbols", "delete_wires", "delete_labels",
    "delete_junctions", "delete_no_connects", "add_no_connects", "move_symbols", "move_labels", "set_text", "add_text",
    "move_text", "delete_text", "replicate_group", "set_property_positions", "align_items", "snap_to_grid",
    "distribute_items", "rotate_items", "mirror_items", "replace_symbols", "refresh_lib_symbols", "set_properties",
    "connect_pins",
]
NOTICE_SENTENCE = (
    "A sheet that was in another layout than KiCad's is laid out in KiCad's layout as a whole by its first write; "
    "the result of that write then carries layout_normalised: true and normalised_lines: N (the lines that changed, "
    "a count, not an error), and both keys are absent when the sheet already had KiCad's layout and for a dry run."
)


@pytest.mark.parametrize("name", NOTICE_TOOLS)
def test_the_description_of_a_tool_that_returns_the_layout_notice_says_so(name) -> None:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    tool = next(t for t in mcp._tool_manager.list_tools() if t.name == name)  # type: ignore[attr-defined]
    assert NOTICE_SENTENCE in " ".join(tool.description.split()), name
