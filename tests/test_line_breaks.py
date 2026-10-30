"""No raw line break reaches a KiCad file we write (build ticket 25).

KiCad writes a line break inside a quoted value as ``\\n`` (``\\r``) and reads its files line
by line. A raw line feed inside a string leaves the token open: in a sub-sheet KiCad leaves
the whole sheet out of the design with no error (mixelpixx #423; on ``complex_hierarchy`` the
netlist falls from 68 components to 10 and ``kicad-cli`` still exits 0).

Every test here works on a copy of KiCad's own ``complex_hierarchy`` demo (``kicad_demo``
fixture, never vendored) and grades with ``kicad-cli`` output, never with a tool's return
value: the netlist component count (68 before, 68 after unless the write adds a part), the
values KiCad reads back (its netlist, or its own re-save via ``upgrade --force``), and the raw
bytes of every quoted string in the written files.

KiCad files are written only through the tools under test, ``kicad_tools``' object model or
the write layer, on ``tmp_path`` copies (CLAUDE.md hard rule 1).
"""

from __future__ import annotations

import shutil
import subprocess
import uuid
from pathlib import Path

import kicad_tools
import pytest
from kicad_tools.sexp import Document, SExp, parse_string
from mcp.server.mcpserver import MCPServer

from kicad_mcp import kicad_cli
from kicad_mcp import write_layer as wl
from tests import _kicad_faults as faults

# kicad_tools 0.25.0 escapes a CR itself (its changelog); up to 0.24 it wrote it raw.
_KT_WRITES_CR_RAW = tuple(int(p) for p in kicad_tools.__version__.split(".")[:2]) < (0, 25)

DEMO = "complex_hierarchy"
ROOT = "complex_hierarchy.kicad_sch"
SUB = "ampli_ht.kicad_sch"  # the sub-sheet, used twice by the root
COMPONENTS = 68  # kicad-cli 10.0.7 on the installed demo; asserted again before every write
BREAKS = {"LF": "\n", "CR": "\r", "CRLF": "\r\n"}


# --------------------------------------------------------------------------- oracles


def _cli() -> str:
    return str(kicad_cli.find_kicad_cli())


def netlist(project: Path) -> tuple[int, list[dict[str, str]]]:
    """``kicad-cli sch export netlist`` of the project's root: the component count and each
    component's ref, value and footprint as KiCad read them."""
    out = project.parent / f"{project.name}-netlist.net"
    proc = subprocess.run(
        [_cli(), "sch", "export", "netlist", "--format", "kicadsexpr", "-o", str(out), str(project / ROOT)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert out.is_file(), f"kicad-cli wrote no netlist (exit {proc.returncode}): {proc.stderr[-500:]}"
    root = parse_string(out.read_text(encoding="utf-8"))
    out.unlink()
    comps = [c for c in root.find("components").children if c.name == "comp"]
    fields = []
    for comp in comps:
        fields.append({
            key: (comp.find(key).get_string(0) if comp.find(key) is not None else "")
            for key in ("ref", "value", "footprint")
        })
    return len(comps), fields


def raw_breaks(path: Path) -> list[bytes]:
    """Every quoted string in ``path`` that holds a raw LF or CR byte (KiCad's escapes are
    ``\\n`` and ``\\r``, so a KiCad-written file has none)."""
    data = path.read_bytes()
    found, i = [], 0
    while i < len(data):
        if data[i] == 0x22:  # '"'
            j = i + 1
            while j < len(data) and data[j] != 0x22:
                j += 2 if data[j] == 0x5C else 1  # skip the character after a backslash
            text = data[i : j + 1]
            if b"\n" in text or b"\r" in text:
                found.append(text[:80])
            i = j + 1
        else:
            i += 1
    return found


def raw_breaks_in(directory: Path) -> dict[str, list[bytes]]:
    """``raw_breaks`` of every KiCad file and lib table under ``directory``."""
    out = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and (path.suffix.startswith(".kicad_") or path.name in ("fp-lib-table", "sym-lib-table")):
            if path.suffix == ".kicad_pro":
                continue  # JSON: a raw control character cannot be valid JSON, json.dumps escapes it
            hits = raw_breaks(path)
            if hits:
                out[str(path.relative_to(directory))] = hits
    return out


def _strings(node: SExp) -> list[str]:
    if node.is_atom:
        return [node.value] if isinstance(node.value, str) else []
    return [s for child in node.children for s in _strings(child)]


def kicad_reads(path: Path, kind: str, tmp_path: Path) -> list[str]:
    """The strings of ``path`` as KiCad reads them: KiCad loads a copy and saves it again
    (``kicad-cli <kind> upgrade --force``), and the strings of its own file are returned.
    KiCad's own save must hold no raw line break either: that encodes the fact that KiCad
    escapes every field these tests write (sheet-pin names are the exception, tested apart)."""
    scratch = tmp_path / f"kicad-read-{len(list(tmp_path.glob('kicad-read-*')))}"
    shutil.copytree(path.parent, scratch)
    copy = scratch / path.name
    proc = subprocess.run(
        [_cli(), kind, "upgrade", "--force", str(copy)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, f"KiCad could not load {path.name}: exit {proc.returncode} {proc.stderr[-500:]}"
    assert raw_breaks(copy) == [], f"KiCad's own save of {path.name} holds a raw line break"
    return _strings(wl.load(copy))


@pytest.fixture
def project(kicad_demo) -> Path:
    """A fresh copy of ``complex_hierarchy`` whose netlist holds the 68 components."""
    copy = kicad_demo(DEMO)
    count, _ = netlist(copy)
    assert count == COMPONENTS, f"the installed demo has {count} components, not {COMPONENTS}"
    return copy


def _first_symbol_value(root: SExp) -> SExp:
    for sym in root.find_all("symbol"):
        if sym.find("lib_id") is None:
            continue
        ref = next(p for p in sym.find_all("property") if p.get_string(0) == "Reference")
        if ref.get_string(1).startswith("#"):
            continue  # a power symbol: not in the netlist
        return next(p for p in sym.find_all("property") if p.get_string(0) == "Value")
    raise AssertionError("no placed symbol with a Value property")


# --------------------------------------------------------------------------- step 1: the facts


def test_the_oracle_sees_a_dropped_sub_sheet(project: Path) -> None:
    """Negative control: a sub-sheet KiCad cannot load leaves the netlist with 10 components
    and kicad-cli still writes it. The 68-count assertions below can therefore see a drop.
    (An unknown token after the symbols drops nothing: KiCad keeps what it read before.)"""
    sheet = project / SUB
    doc = Document.load(sheet)
    doc.root.children.insert(1, SExp.list("kicad_mcp_marker", 1))  # unknown token, not raw text
    doc.save(sheet)
    count, _ = netlist(project)
    assert count == 10


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_kicad_tools_high_level_schematic_escapes_lf_but_writes_cr_raw(project: Path, brk: str) -> None:
    """Step 1 on ``kicad_tools`` 0.23.0's high-level ``Schematic`` (what ``add_symbols``,
    ``add_wires``, ``add_junctions`` and ``add_labels`` write with): a LF is escaped, a CR
    is written raw. KiCad 10.0.7 still loads the sheet (its line reader splits on LF only)
    and reads the CR back, so a raw CR drops nothing, but it is not what KiCad writes, and
    ``kicad_tools``' universal-newline readers turn it into a LF. The tools refuse a CR."""
    from kicad_tools.schematic.models.schematic import Schematic

    value = f"a{BREAKS[brk]}b"
    sch = Schematic.load(project / SUB)
    sch.symbols[0].value = value
    sch.write(project / SUB)

    hits = raw_breaks(project / SUB)
    if "\r" in value and _KT_WRITES_CR_RAW:
        assert hits and all(b"\n" not in h for h in hits)  # the CR raw, the LF escaped
    else:
        assert hits == []
    count, fields = netlist(project)
    assert count == COMPONENTS
    assert sum(f["value"] == value for f in fields) == 2  # the sheet is used twice


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_kicad_tools_low_level_tree_escapes_lf_but_writes_cr_raw(project: Path, brk: str) -> None:
    """Step 1 on ``kicad_tools`` 0.23.0's low-level tree (``Document``/``SExp``, serialised by
    ``SExp.to_string``): the same as the high-level model. ``schema.Schematic.save`` itself
    cannot run on Windows (``OSError`` EBADF in ``atomic_write``, upstream #7478), which is why
    every save goes through the write layer."""
    value = f"a{BREAKS[brk]}b"
    doc = Document.load(project / SUB)
    _first_symbol_value(doc.root).set_value(1, value)
    doc.save(project / SUB)

    hits = raw_breaks(project / SUB)
    assert bool(hits) == ("\r" in value and _KT_WRITES_CR_RAW)
    count, fields = netlist(project)
    assert count == COMPONENTS
    assert sum(f["value"] == value for f in fields) == 2


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_the_write_layer_escapes_every_line_break_in_a_sub_sheet(project: Path, brk: str) -> None:
    value = f"a{BREAKS[brk]}b"
    root = wl.load(project / SUB)
    wl.set_string(_first_symbol_value(root), 1, value)
    wl.save(root, project / SUB)

    assert raw_breaks(project / SUB) == []
    count, fields = netlist(project)
    assert count == COMPONENTS
    assert sum(f["value"] == value for f in fields) == 2


# --------------------------------------------------------------------------- the tools


def _tools(module) -> dict:
    mcp = MCPServer("t25")
    module.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture(scope="module")
def sch_tools() -> dict:
    from kicad_mcp.toolsets import schematic

    return _tools(schematic)


def _unchanged(project: Path, before: dict[str, bytes]) -> None:
    after = {p.name: p.read_bytes() for p in project.glob("*.kicad_sch")}
    assert after == before


def _snapshot(project: Path) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in project.glob("*.kicad_sch")}


def test_add_symbols_escapes_a_line_feed_in_value_and_footprint(project: Path, sch_tools: dict) -> None:
    value, footprint = "10k\nprecision", "Resistor_SMD:R_0603\n_1608Metric"
    result = sch_tools["add_symbols"](
        str(project / SUB),
        [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R900", "value": value, "footprint": footprint}],
    )
    assert result["status"] == "ok", result

    assert raw_breaks_in(project) == {}
    count, fields = netlist(project)
    assert count == COMPONENTS + 2  # one new symbol on a sheet used twice, nothing lost
    added = [f for f in fields if f["value"] == value]
    assert len(added) == 2 and all(f["footprint"] == footprint for f in added)


@pytest.mark.parametrize("kind", ["label", "global", "hier"])
def test_add_labels_escapes_a_line_feed_in_the_label_text(project: Path, sch_tools: dict, kind: str, tmp_path: Path) -> None:
    text = f"{kind.upper()}_A\nB"
    result = sch_tools["add_labels"](str(project / SUB), [{"kind": kind, "text": text, "x": 50.8, "y": 50.8}])
    assert result["status"] == "ok", result

    assert raw_breaks_in(project) == {}
    count, _ = netlist(project)
    assert count == COMPONENTS
    assert text in kicad_reads(project / SUB, "sch", tmp_path)


@pytest.mark.parametrize(
    ("tool", "args", "field"),
    [
        ("add_symbols", lambda v: [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R900", "value": v}], "specs[0].value"),
        ("add_labels", lambda v: [{"kind": "hier", "text": v, "x": 50.8, "y": 50.8}], "specs[0].text"),
        ("add_labels", lambda v: [{"kind": "label", "text": v, "x": 50.8, "y": 50.8}], "specs[0].text"),
        ("add_labels", lambda v: [{"kind": "global", "text": v, "x": 50.8, "y": 50.8}], "specs[0].text"),
        ("add_wires", lambda v: [{"p1": [50.8, 50.8], "p2": [60.96, 50.8], "route": v}], "specs[0].route"),
    ],
    ids=["add_symbols", "add_labels-hier", "add_labels-label", "add_labels-global", "add_wires"],
)
def test_schematic_tools_refuse_a_carriage_return_by_name(project: Path, sch_tools: dict, tool: str, args, field: str) -> None:
    before = _snapshot(project)
    result = sch_tools[tool](str(project / SUB), args("A\rB"))
    assert result["status"] != "ok"
    assert field in result["reason"]
    _unchanged(project, before)
    assert netlist(project)[0] == COMPONENTS


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_assign_footprints_refuses_a_line_break_by_name(project: Path, sch_tools: dict, brk: str) -> None:
    """Before fix round 1, ``assign_footprints`` let ``kicad_tools``' ``run_set_footprint``
    substitute the id into the sheet's text, unescaped and without a parse check: a LF reached
    ``ampli_ht.kicad_sch`` raw and the sub-sheet fell out of the design (68 to 10 components;
    status ``ok`` when the tool was called on the sub-sheet itself). The tool now writes
    through the write layer, which would escape it, but a footprint id is one line: any line
    break is refused and the reference named."""
    before = _snapshot(project)
    result = sch_tools["assign_footprints"](str(project / ROOT), {"RV201": f"Lib:FP{BREAKS[brk]}x"})
    assert result["status"] != "ok"
    assert "mapping.RV201" in result["reason"]
    _unchanged(project, before)
    assert netlist(project)[0] == COMPONENTS


# The footprint id as kicad_tools' ``set_footprint_text`` used it: a ``re.sub`` replacement
# template (review round 1, blocker 1). ``\n`` and ``\012`` became a raw LF (68 to 10
# components), ``\r`` a raw CR, and ``\g<0>`` inserted the whole match and broke the quoting.
BACKSLASH_IDS = {
    "escape-n": "Lib:FP\\nx",
    "octal-012": "Lib:FP\\012x",
    "escape-r": "Lib:FP\\rx",
    "group-0": "Lib:FP\\g<0>x",
    "bad-escape": "Lib:FP\\qx",  # re.error in the template: the tool failed after its backup
    "quote": 'Lib:FP"x',  # the unescaped substitution closed the string early (68 to 10)
}


def _footprints_of(fields: list[dict[str, str]], ref: str) -> list[str]:
    return [f["footprint"] for f in fields if f["ref"] == ref]


@pytest.mark.parametrize("footprint", BACKSLASH_IDS.values(), ids=list(BACKSLASH_IDS))
def test_assign_footprints_writes_a_backslash_literally(project: Path, sch_tools: dict, footprint: str) -> None:
    """A backslash in a footprint id is text, not a template: KiCad reads back exactly the
    id that was asked for, and no sheet falls out of the design. (RV201 and RV301 are the two
    instances of one symbol on the sheet used twice: both are named, fix round 2.)"""
    result = sch_tools["assign_footprints"](str(project / ROOT), {"RV201": footprint, "RV301": footprint})

    assert result["status"] == "ok", result
    assert raw_breaks_in(project) == {}
    count, fields = netlist(project)
    assert count == COMPONENTS
    assert _footprints_of(fields, "RV201") == [footprint]
    assert _footprints_of(fields, "RV301") == [footprint]


def _placed_symbol(root: SExp, ref: str) -> SExp:
    for sym in root.find_all("symbol"):
        if sym.find("lib_id") is None:
            continue
        if any(p.get_string(0) == "Reference" and p.get_string(1) == ref for p in sym.find_all("property")):
            return sym
    raise AssertionError(f"no placed symbol {ref}")


def _footprint_property(root: SExp, ref: str) -> SExp:
    return next(p for p in _placed_symbol(root, ref).find_all("property") if p.get_string(0) == "Footprint")


def test_assign_footprints_self_check_fires_on_a_corrupted_write(project: Path, sch_tools: dict, monkeypatch) -> None:
    """The self-check (decision 4): the temporary sheet is reloaded and must serialise as the
    tree did. A write that lands a different footprint (still a file KiCad loads, so the
    parse check passes) is caught before the move. Decision 9: a failed self-check is a
    demonstrated tool fault, ``ok: false`` with ``write_not_verified`` (not ``incomplete``),
    and the result lists each mismatch: expected, actual, file location (fix round 2). Every
    sheet byte-identical, no temporary file left."""
    real = wl._write_temp

    def corrupted(text: str, path: Path) -> Path:
        return real(text.replace('"Lib:FP"', '"Lib:XX"'), path)

    monkeypatch.setattr(wl, "_write_temp", corrupted)
    before = _snapshot(project)

    result = sch_tools["assign_footprints"](str(project / ROOT), {"RV201": "Lib:FP", "RV301": "Lib:FP"})

    assert result["ok"] is False and result["status"] == "error", result
    assert result["error"] == "write_not_verified" and SUB in result["reason"]
    assert not result["reason"].startswith("write_not_verified: write_not_verified")
    mismatches = result["mismatches"]
    assert mismatches["count"] == 1
    (item,) = mismatches["items"]
    assert item["file"] == SUB and item["references"] == ["RV201", "RV301"]
    assert (item["expected"], item["actual"]) == ("Lib:FP", "Lib:XX")
    assert item["symbol"]  # the symbol's uuid: where in the file
    assert result["write_check"]["missing"]["count"] == 1  # the node-level view, as board_outline reports it
    assert result["written_sheets"] == []
    _unchanged(project, before)
    assert not list(project.glob(".*.tmp"))
    assert netlist(project)[0] == COMPONENTS


def test_assign_footprints_keeps_a_raw_carriage_return_already_in_a_sheet(project: Path, sch_tools: dict) -> None:
    """KiCad loads a raw CR inside a string, and ``kicad_tools``' own writers (and KiCad's
    sheet-pin save) produce one. ``run_set_footprint`` read and wrote the sheet with universal
    newlines and turned it into a raw LF: 68 to 10 components with status ``ok`` (review
    round 1, blocker 2). The write layer reads without newline translation, so the value
    survives; only the asked-for Footprint property changes, every other value of the sheet
    reads back the same, and the root (no symbol of it changed) is byte-identical."""
    value = "a\rb"
    doc = Document.load(project / SUB)
    _first_symbol_value(doc.root).set_value(1, value)  # kicad_tools writes the CR raw
    doc.save(project / SUB)
    if not _KT_WRITES_CR_RAW:  # 0.25.0 escapes it: make the raw-CR sheet 0.20 to 0.24 wrote
        faults.rewrite_bytes(project / SUB, lambda data: data.replace(b'"a\\rb"', b'"a\rb"'))
    assert raw_breaks(project / SUB), "the fixture must hold a raw CR"
    count, fields = netlist(project)
    assert count == COMPONENTS and sum(f["value"] == value for f in fields) == 2
    root_before = (project / ROOT).read_bytes()
    sub_before = wl.load(project / SUB)

    result = sch_tools["assign_footprints"](str(project / ROOT), {"RV201": "Lib:FP", "RV301": "Lib:FP"})

    assert result["status"] == "ok", result
    count, fields = netlist(project)
    assert count == COMPONENTS
    assert sum(f["value"] == value for f in fields) == 2
    assert _footprints_of(fields, "RV201") == ["Lib:FP"]
    assert _footprints_of(fields, "RV301") == ["Lib:FP"]
    assert (project / ROOT).read_bytes() == root_before
    sub_after = wl.load(project / SUB)
    assert _footprint_property(sub_after, "RV201").get_string(1) == "Lib:FP"
    wl.set_string(_footprint_property(sub_after, "RV201"), 1, _footprint_property(sub_before, "RV201").get_string(1))
    assert wl.serialise(sub_after, compact=True) == wl.serialise(sub_before, compact=True)


# --------------------------------------------------------------------------- assign_footprints on a sheet used twice
#
# Delta review 1 (fix round 2), blocker 1: ``ampli_ht.kicad_sch`` is used twice, so each of its
# symbols is placed as two instances (RV201 and RV301). KiCad keeps one ``Footprint`` property
# per symbol, shared by both, and the symbol's ``Reference`` property holds the first instance's
# reference only. Matching on that property set RV301 silently with RV201, called RV301 "not
# placed", and gave RV301 RV201's id when the two ids differed.


def _instance_symbols(root: SExp, ref: str) -> list[SExp]:
    """The placed symbols (one per unit) with an ``(instances (project (path (reference ref))))``
    entry for ``ref``, read from the raw S-expression."""
    found = []
    for sym in root.find_all("symbol"):
        if sym.find("lib_id") is None:
            continue
        refs = {
            r.get_string(0)
            for inst in sym.find_all("instances")
            for proj in inst.find_all("project")
            for path in proj.find_all("path")
            for r in path.find_all("reference")
        }
        if ref in refs:
            found.append(sym)
    return found


def _others(fields: list[dict[str, str]], refs: set[str]) -> dict[str, str]:
    return {f["ref"]: f["footprint"] for f in fields if f["ref"] not in refs}


@pytest.mark.parametrize(("named", "other"), [("RV201", "RV301"), ("RV301", "RV201")])
def test_assign_footprints_refuses_one_instance_of_a_shared_symbol(
    project: Path, sch_tools: dict, named: str, other: str
) -> None:
    """Naming one instance of a symbol on a reused sheet would change the other too: refused
    before anything is written, naming the instance left out. RV301 is a placed instance:
    never "no placed symbol"."""
    before = _snapshot(project)

    result = sch_tools["assign_footprints"](str(project / ROOT), {named: "Lib:FP"})

    assert result["status"] != "ok", result
    assert f"{named} shares its symbol with {other}" in result["reason"]
    assert "map both to the same id" in result["reason"]
    assert "no placed symbol" not in str(result)
    assert "backup_dir" not in result  # refused before the backup, before any write
    _unchanged(project, before)
    assert netlist(project)[0] == COMPONENTS


def test_assign_footprints_refuses_two_ids_for_one_symbol(project: Path, sch_tools: dict) -> None:
    """RV201 and RV301 share one Footprint property: two different ids cannot both hold."""
    before = _snapshot(project)

    result = sch_tools["assign_footprints"](str(project / ROOT), {"RV201": "Lib:A", "RV301": "Lib:B"})

    assert result["status"] != "ok", result
    assert "RV201" in result["reason"] and "RV301" in result["reason"]
    assert "'Lib:A'" in result["reason"] and "'Lib:B'" in result["reason"]
    assert "no placed symbol" not in str(result)
    _unchanged(project, before)


def test_assign_footprints_sets_every_instance_named_with_one_id(project: Path, sch_tools: dict) -> None:
    """Both instances named with one id: both get it, and no other component changes."""
    _, before = netlist(project)

    result = sch_tools["assign_footprints"](str(project / ROOT), {"RV201": "Lib:FP", "RV301": "Lib:FP"})

    assert result["status"] == "ok", result
    assert result["assigned"] == {"RV201": "Lib:FP", "RV301": "Lib:FP"}
    count, after = netlist(project)
    assert count == COMPONENTS
    assert _footprints_of(after, "RV201") == _footprints_of(after, "RV301") == ["Lib:FP"]
    assert _others(after, {"RV201", "RV301"}) == _others(before, {"RV201", "RV301"})


def test_assign_footprints_sets_every_unit_of_a_part(project: Path, sch_tools: dict) -> None:
    """U201/U301 is a two-unit part on the reused sheet: two placed symbols, each with its own
    ``Footprint`` property. Every unit's raw property changes; the netlist shows one footprint
    per reference and cannot see a unit left behind (delta review 1, medium 3: a mutant that
    set the first unit only passed every other test)."""
    sub = wl.load(project / SUB)
    units = _instance_symbols(sub, "U201")
    assert len(units) == 2 and _instance_symbols(sub, "U301") == units, "the fixture must hold a two-unit part"

    result = sch_tools["assign_footprints"](str(project / ROOT), {"U201": "Lib:FP", "U301": "Lib:FP"})

    assert result["status"] == "ok", result
    sub = wl.load(project / SUB)
    footprints = [
        p.get_string(1) for sym in _instance_symbols(sub, "U201") for p in sym.find_all("property") if p.get_string(0) == "Footprint"
    ]
    assert footprints == ["Lib:FP", "Lib:FP"]
    assert netlist(project)[0] == COMPONENTS


def test_assign_footprints_names_a_component_it_changed_outside_the_mapping(
    project: Path, sch_tools: dict, monkeypatch
) -> None:
    """Backstop: kicad-cli's netlist before and after the write. A component outside the
    mapping whose footprint changed is an error naming it, with the backup to go back to.
    Unreachable with the staging above, so the writer is patched to change R201's symbol too
    (it still saves and self-checks exactly the tree it was handed)."""
    from kicad_mcp.toolsets import schematic

    real = schematic._save_footprint_sheet

    def stowaway(sheet: Path, root: SExp, *rest) -> None:
        if sheet.name == SUB:
            wl.set_string(_footprint_property(root, "R201"), 1, "Lib:STOWAWAY")
        real(sheet, root, *rest)

    monkeypatch.setattr(schematic, "_save_footprint_sheet", stowaway)

    result = sch_tools["assign_footprints"](str(project / ROOT), {"RV201": "Lib:FP", "RV301": "Lib:FP"})

    assert result["ok"] is False and result["status"] == "error", result
    assert result["error"] == "footprint_changed_outside_mapping"
    assert "R201" in result["reason"] and "R301" in result["reason"]
    assert {c["ref"] for c in result["changed_outside_mapping"]["items"]} == {"R201", "R301"}
    assert Path(result["backup_dir"]).is_dir()
    _, fields = netlist(project)
    assert _footprints_of(fields, "R201") == ["Lib:STOWAWAY"]  # the fault was real, and reported


def test_assign_footprints_names_a_write_layer_code_once(project: Path, sch_tools: dict, monkeypatch) -> None:
    """Delta review 1, low: the reason read "parse_check_failed: parse_check_failed: ..."."""

    def refuses(written, kind, *, target=None):
        raise wl.WriteLayerError("parse_check_failed", "kicad-cli could not load it")

    monkeypatch.setattr(wl, "parse_check", refuses)
    before = _snapshot(project)

    result = sch_tools["assign_footprints"](str(project / ROOT), {"RV201": "Lib:FP", "RV301": "Lib:FP"})

    assert result["error"] == "parse_check_failed", result
    assert result["reason"].count("parse_check_failed") == 1, result["reason"]
    _unchanged(project, before)


# --------------------------------------------------------------------------- sheet names and sheet pins


def _effects(justify: str) -> SExp:
    return SExp.list(
        "effects",
        SExp.list("font", SExp.list("size", wl.number(1.27), wl.number(1.27))),
        SExp.list("justify", wl.symbol(justify)),
    )


def _sheet_pin(name: str, x: float, y: float) -> SExp:
    """A sheet pin on a sheet's right edge, built from typed atoms (KiCad 10's form)."""
    return SExp.list(
        "pin", wl.string(name), wl.symbol("input"),
        SExp.list("at", wl.number(x), wl.number(y), wl.number(0)),
        SExp.list("uuid", wl.string(str(uuid.uuid4()))),
        _effects("right"),
    )


def _hierarchical_label(name: str, x: float, y: float) -> SExp:
    return SExp.list(
        "hierarchical_label", wl.string(name),
        SExp.list("shape", wl.symbol("input")),
        SExp.list("at", wl.number(x), wl.number(y), wl.number(180)),
        _effects("right"),
        SExp.list("uuid", wl.string(str(uuid.uuid4()))),
    )


def _insert_before(root: SExp, name: str, node: SExp) -> None:
    index = next(i for i, child in enumerate(root.children) if child.name == name)
    root.children.insert(index, node)


def _root_with_sheet_pin(project: Path, pin_name: str) -> SExp:
    """The root of ``project`` with a sheet pin named ``pin_name`` on its first sheet's right
    edge, placed from the raw ``(at)`` and ``(size)`` of that sheet."""
    root = wl.load(project / ROOT)
    sheet = next(iter(root.find_all("sheet")))
    at, size = sheet.find("at"), sheet.find("size")
    assert at is not None and size is not None
    right_edge = float(at.children[0].value) + float(size.children[0].value)
    _insert_before(sheet, "instances", _sheet_pin(pin_name, right_edge, float(at.children[1].value) + 3.81))
    return root


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_a_sheet_name_with_a_line_break_is_escaped(project: Path, brk: str, tmp_path: Path) -> None:
    """No tool writes sheet names; a mutation without a tool goes through the write layer
    (docs/ARCHITECTURE.md decision 1), which escapes it. In the root a raw LF would fail the load
    and the layer's parse check. KiCad reads the name back and writes it escaped itself."""
    name = f"Sheet{BREAKS[brk]}name"
    root = wl.load(project / ROOT)
    sheet = next(iter(root.find_all("sheet")))
    sheetname = next(p for p in sheet.find_all("property") if p.get_string(0) == "Sheetname")
    wl.set_string(sheetname, 1, name)
    wl.save(root, project / ROOT)

    assert raw_breaks_in(project) == {}
    assert netlist(project)[0] == COMPONENTS
    assert name in kicad_reads(project / ROOT, "sch", tmp_path)


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_the_write_layer_refuses_a_line_break_in_a_sheet_pin_name(project: Path, brk: str) -> None:
    """A sheet-pin name is the one field where escaping is not enough: KiCad 10.0.7 writes it
    unescaped (next test), so the next save in KiCad breaks the root. The layer refuses it
    by name before any file is written. No tool writes sheet pins today."""
    before = _snapshot(project)
    root = _root_with_sheet_pin(project, f"PIN{BREAKS[brk]}name")
    with pytest.raises(wl.WriteLayerError) as refused:
        wl.save(root, project / ROOT)
    assert refused.value.code == "line_break_in_sheet_pin"
    assert refused.value.details["pins"] == [f"PIN{BREAKS[brk]}name"]
    _unchanged(project, before)
    assert not list(project.glob(".*.tmp"))


def test_kicad_itself_writes_a_sheet_pin_name_with_a_raw_line_break(project: Path, tmp_path: Path) -> None:
    """The KiCad fact behind the refusal (10.0.7). The root is written escaped, with the
    layer's serialiser and no parse check (``_kicad_faults.write_unchecked``: a tree the layer
    refuses on purpose):
    KiCad loads it (68 components), but its own save writes ``"PIN<newline>name"`` raw, and
    then KiCad cannot load its own file (exit 3). A CR is written raw too, and loads."""
    faults.write_unchecked(_root_with_sheet_pin(project, "PIN\nname"), project / ROOT)
    assert raw_breaks(project / ROOT) == []
    assert netlist(project)[0] == COMPONENTS

    scratch = tmp_path / "kicad-saved"
    shutil.copytree(project, scratch)
    first = subprocess.run([_cli(), "sch", "upgrade", "--force", str(scratch / ROOT)], capture_output=True, timeout=120)
    assert first.returncode == 0
    assert [b"PIN" in hit for hit in raw_breaks(scratch / ROOT)] == [True]
    again = subprocess.run([_cli(), "sch", "upgrade", "--force", str(scratch / ROOT)], capture_output=True, timeout=120)
    assert again.returncode == 3


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_a_hierarchical_label_with_a_line_break_is_escaped(project: Path, brk: str, tmp_path: Path) -> None:
    """The demo's sub-sheet has no hierarchical label; one is added through the write layer
    (``add_labels`` with ``kind: "hier"`` is the tool path, tested above)."""
    text = f"HIER{BREAKS[brk]}label"
    root = wl.load(project / SUB)
    _insert_before(root, "symbol", _hierarchical_label(text, 50.8, 50.8))
    wl.save(root, project / SUB)

    assert raw_breaks_in(project) == {}
    assert netlist(project)[0] == COMPONENTS
    assert text in kicad_reads(project / SUB, "sch", tmp_path)


# --------------------------------------------------------------------------- board texts


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_place_footprints_escapes_a_line_break_in_reference_and_value(project: Path, brk: str, tmp_path: Path) -> None:
    from kicad_mcp.toolsets import placement

    pcb = project / "complex_hierarchy.kicad_pcb"
    ref, value = f"R9{BREAKS[brk]}x", f"1k{BREAKS[brk]}1%"
    result = _tools(placement)["place_footprints"](
        str(pcb), [{"reference": ref, "library_id": "Resistor_SMD:R_0603_1608Metric", "x": 5, "y": 5, "value": value}]
    )
    assert result["status"] == "ok", result

    assert raw_breaks(pcb) == []
    read = kicad_reads(pcb, "pcb", tmp_path)
    assert ref in read and value in read


# --------------------------------------------------------------------------- libraries and projects


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_register_library_asset_never_writes_a_raw_line_break(project: Path, brk: str) -> None:
    """The lib tables are read by KiCad's own S-expression lexer. ``kicad_tools``' ``to_string``
    escapes a LF but writes a CR raw; a nickname is one token of a lib id, so the tool refuses
    any line break and names the parameter."""
    from kicad_mcp.toolsets import library

    shutil.copytree(project / "complex_hierarchy.pretty", project / "extra.pretty")
    table = project / "fp-lib-table"
    before = table.read_bytes()
    result = _tools(library)["register_library_asset"](
        str(project), footprint_lib_path=str(project / "extra.pretty"), nickname=f"extra{BREAKS[brk]}lib"
    )
    assert result["status"] == "error", result
    assert "nickname" in result["footprint"]["reason"]
    assert table.read_bytes() == before


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_register_library_asset_keeps_an_existing_escaped_line_break_escaped(project: Path, brk: str) -> None:
    """An existing entry's escaped ``\\r`` or ``\\n`` (as KiCad writes it) must survive escaped
    (review round 1, medium 4: the tool then rewrote the whole table, and ``kicad_tools``'
    ``to_string`` wrote it back raw). Since build ticket 76 the tool inserts its one row and
    keeps every other byte, so this guards that no rewrite comes back. The fixture table is
    written with the write layer's serialiser (no lib-table tool exists to put a line break
    there)."""
    from kicad_mcp.toolsets import library

    descr = f"a{BREAKS[brk]}b"
    table = project / "fp-lib-table"
    root = wl.load(table)
    wl.set_string(next(iter(root.find_all("lib"))).find("descr"), 0, descr)
    table.write_text(wl.serialise(root) + "\n", encoding="utf-8")
    assert raw_breaks(table) == []
    shutil.copytree(project / "complex_hierarchy.pretty", project / "extra.pretty")

    result = _tools(library)["register_library_asset"](
        str(project), footprint_lib_path=str(project / "extra.pretty"), nickname="extra"
    )

    assert result["status"] == "ok", result
    assert raw_breaks(table) == []
    assert descr in _strings(wl.load(table))


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_generate_footprint_refuses_a_line_break_by_name(tmp_path: Path, brk: str) -> None:
    """``kicad_tools``' ``Footprint.to_sexp`` escapes nothing: a LF or CR in ``name`` reached
    the ``.kicad_mod`` raw. A footprint name is one line, so the tool refuses it."""
    from kicad_mcp.toolsets import sourcing

    out = tmp_path / "fp" / "x.kicad_mod"
    result = _tools(sourcing)["generate_footprint"](
        package_type="chip",
        output_path=str(out),
        params=sourcing.FootprintParams(size="0603", name=f"R_0603{BREAKS[brk]}x"),
    )
    assert result["status"] != "ok"
    assert "name" in result["reason"]
    assert not out.exists()


@pytest.mark.parametrize("brk", BREAKS, ids=list(BREAKS))
def test_project_create_writes_no_raw_line_break(tmp_path: Path, brk: str) -> None:
    """The project name becomes three file names and the title. What this proves depends on
    the platform. On Windows the file system refuses a line break in a file name: the tool
    raises ``OSError`` before any file exists, and that is all this test shows there (nothing
    about escaping). Elsewhere a line break is a legal file-name character, the files are
    written, and no KiCad file may hold it raw; that side has not been run yet (this
    project's machines run Windows)."""
    import os

    from kicad_mcp.toolsets import project as project_toolset

    target = tmp_path / "new"
    raised = None
    try:
        _tools(project_toolset)["project_create"](directory=str(target), name=f"board{BREAKS[brk]}x")
    except OSError as exc:
        raised = exc
    if os.name == "nt":
        assert raised is not None, "Windows accepted a line break in a file name"
        assert not target.exists() or not [p for p in target.rglob("*") if p.is_file()]
    else:
        assert raw_breaks_in(target) == {}
