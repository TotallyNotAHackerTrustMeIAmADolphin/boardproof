"""A hierarchical project with stray schematic copies beside it (build ticket 26).

``build_stray_project`` takes a copy of KiCad's ``complex_hierarchy`` demo (root sheet plus
``ampli_ht.kicad_sch``, used twice) and adds copies whose references, values and labels
differ from the live sheets, every change made through kicad_tools' ``Document`` API:

- ``ampli_ht_backup_20261019_120000.kicad_sch`` beside the root, as our tools' kicad_tools
  backups are named (references renumbered to ``<prefix>9<n>``);
- ``.history/ampli_ht.kicad_sch`` and ``.history/complex_hierarchy.kicad_sch``, as KiCad's
  local history keeps them (references unannotated, ``R?``);
- ``orphan.kicad_sch``, a sub-sheet the root does not reference (``<prefix>8<n>``);
- ``<outside>/cwd/ampli_ht.kicad_sch``, a same-named copy in a directory a test makes the
  working directory (``<prefix>7<n>``).

``legacy=True`` renames the root's sheet properties to KiCad 6's ``Sheet file`` and
``Sheet name``. kicad-cli reads both spellings (checked: the same 68 components and 52 nets).

The oracle is ``kicad-cli sch export netlist`` of the root, exported fresh in each test.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from kicad_mcp.kicad_cli import ensure_kicad_cli_on_path

DEMO = "complex_hierarchy"
STRAY_REF = re.compile(r"^[A-Z]+[789]\d{3}$|\?")  # what the stray copies hold, never a live ref


@dataclass
class StrayProject:
    dir: Path
    root: Path
    pcb: Path
    strays: list[Path]
    cwd: Path

    @property
    def unreferenced(self) -> list[str]:
        return sorted(p.relative_to(self.dir).as_posix() for p in self.strays if self.dir in p.parents)


def strayify(src: Path, dest: Path, *, mode: str) -> Path:
    """Copy ``src`` to ``dest`` and change every reference, value and label in the copy.

    ``mode`` is a digit (renumber ``C201`` to ``C<digit>201``) or ``"?"`` (unannotate)."""
    from kicad_tools.sexp import Document

    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    doc = Document.load(dest)

    def new_ref(ref: str) -> str:
        match = re.fullmatch(r"([A-Za-z]+)(\d+)", ref)
        if ref.startswith("#") or not match:
            return ref
        return match.group(1) + "?" if mode == "?" else f"{match.group(1)}{mode}{match.group(2)}"

    for symbol in doc.root.find_children("symbol"):
        for prop in symbol.find_children("property"):
            if prop.get_string(0) == "Reference":
                prop.set_value(1, new_ref(prop.get_string(1) or ""))
            elif prop.get_string(0) == "Value" and not (symbol.find("lib_id").get_string(0) or "").endswith(
                ("GND", "+12V", "-12V", "VCC", "PWR_FLAG", "+5V")
            ):
                prop.set_value(1, "STRAY")
        for instances in symbol.find_children("instances"):
            for project in instances.find_children("project"):
                for path in project.find_children("path"):
                    for ref in path.find_children("reference"):
                        ref.set_value(0, new_ref(ref.get_string(0) or ""))
    for label in doc.root.find_children("label"):
        label.set_value(0, "STRAY_" + (label.get_string(0) or ""))
    doc.save(dest)
    return dest


def use_legacy_sheet_keys(root: Path) -> None:
    """Rename the root's sheet properties to KiCad 6's spelling, through the Document API."""
    from kicad_tools.sexp import Document

    doc = Document.load(root)
    renamed = 0
    for sheet in doc.root.find_children("sheet"):
        for prop in sheet.find_children("property"):
            new = {"Sheetfile": "Sheet file", "Sheetname": "Sheet name"}.get(prop.get_string(0) or "")
            if new:
                prop.set_value(0, new)
                renamed += 1
    assert renamed, f"no sheet property renamed in {root}"
    doc.save(root)


def build_stray_project(kicad_demo, tmp_path: Path, *, legacy: bool = False) -> StrayProject:
    project = kicad_demo(DEMO)
    root = project / f"{DEMO}.kicad_sch"
    sub = project / "ampli_ht.kicad_sch"
    strays = [
        strayify(sub, project / "ampli_ht_backup_20261019_120000.kicad_sch", mode="9"),
        strayify(sub, project / ".history" / "ampli_ht.kicad_sch", mode="?"),
        strayify(root, project / ".history" / f"{DEMO}.kicad_sch", mode="?"),
        strayify(sub, project / "orphan.kicad_sch", mode="8"),
    ]
    cwd = tmp_path / "cwd"
    strays.append(strayify(sub, cwd / "ampli_ht.kicad_sch", mode="7"))
    if legacy:
        use_legacy_sheet_keys(root)
    return StrayProject(dir=project, root=root, pcb=project / f"{DEMO}.kicad_pcb", strays=strays, cwd=cwd)


@dataclass
class Oracle:
    refs: set[str]  # every component reference of the root's netlist
    bom_refs: set[str]  # the same without exclude_from_bom and DNP parts
    footprints: dict[str, str]
    nets: dict[str, list[str]]
    lcsc: dict[str, str]  # ref -> the netlist's "LCSC" or "LCSC Part" field, when set


def oracle_netlist(root: Path, out_dir: Path) -> Oracle:
    """``kicad-cli sch export netlist`` of the root, parsed from the raw S-expression."""
    from kicad_tools.sexp.parser import parse_file

    cli = ensure_kicad_cli_on_path()
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"oracle-{root.stem}.net"
    subprocess.run(
        [str(cli), "sch", "export", "netlist", "--format", "kicadsexpr", "--output", str(out), str(root)],
        check=True,
        capture_output=True,
        timeout=120,
    )
    parsed = parse_file(str(out))
    refs: set[str] = set()
    bom: set[str] = set()
    footprints: dict[str, str] = {}
    lcsc: dict[str, str] = {}
    for comps in parsed.find_children("components"):
        for comp in comps.find_children("comp"):
            ref = comp.find_children("ref")[0].get_string(0)
            refs.add(ref)
            fp = comp.find_children("footprint")
            footprints[ref] = fp[0].get_string(0) if fp else ""
            props = {p.find_children("name")[0].get_string(0) for p in comp.find_children("property")}
            for group in comp.find_children("fields"):
                for field in group.find_children("field"):
                    atoms = [v for v in field.values if isinstance(v, str)]
                    if field.find_children("name")[0].get_string(0) in ("LCSC", "LCSC Part") and atoms:
                        lcsc.setdefault(ref, atoms[0])
            if not props & {"exclude_from_bom", "dnp"}:
                bom.add(ref)
    nets: dict[str, list[str]] = {}
    for block in parsed.find_children("nets"):
        for net in block.find_children("net"):
            name = net.find_children("name")[0].get_string(0)
            nets[name] = sorted(
                f"{n.find_children('ref')[0].get_string(0)}.{n.find_children('pin')[0].get_string(0)}"
                for n in net.find_children("node")
            )
    assert refs and nets, f"the oracle netlist of {root} is empty"
    return Oracle(refs=refs, bom_refs=bom, footprints=footprints, nets=nets, lcsc=lcsc)


def set_reference(sheet: Path, old: str, new: str) -> None:
    """Rename every reference ``old`` of ``sheet`` (property and instance entries) to ``new``,
    through kicad_tools' Document API."""
    from kicad_tools.sexp import Document

    doc = Document.load(sheet)
    changed = 0
    for symbol in doc.root.find_children("symbol"):
        for prop in symbol.find_children("property"):
            if prop.get_string(0) == "Reference" and prop.get_string(1) == old:
                prop.set_value(1, new)
                changed += 1
        for instances in symbol.find_children("instances"):
            for project in instances.find_children("project"):
                for path in project.find_children("path"):
                    for ref in path.find_children("reference"):
                        if ref.get_string(0) == old:
                            ref.set_value(0, new)
                            changed += 1
    assert changed, f"no reference {old} in {sheet}"
    doc.save(sheet)


def add_instance_entry(sheet: Path, ref: str, *, project: str, path: str, new_ref: str, first: bool) -> None:
    """Add ``(project <project> (path <path> (reference <new_ref>) (unit 1)))`` to the
    instances of the symbol whose instance reference is ``ref``, through the Document API."""
    from kicad_tools.sexp import Document
    from kicad_tools.sexp.parser import SExp

    doc = Document.load(sheet)
    for symbol in doc.root.find_children("symbol"):
        for instances in symbol.find_children("instances"):
            refs = [r.get_string(0) for p in instances.find_children("project") for e in p.find_children("path") for r in e.find_children("reference")]
            if ref in refs:
                node = SExp.list(
                    "project",
                    SExp.quoted_atom(project),
                    SExp.list("path", SExp.quoted_atom(path), SExp.list("reference", SExp.quoted_atom(new_ref)), SExp.list("unit", 1)),
                )
                if first:
                    instances.insert(0, node)
                else:
                    instances.append(node)
                doc.save(sheet)
                return
    raise AssertionError(f"no symbol with reference {ref} in {sheet}")


def use_kicad6_instance_data(root: Path, sub: Path) -> None:
    """Rewrite the project the KiCad 6 way, through the Document API: format version
    20211123, no ``(instances ...)`` on the symbols, one root ``(symbol_instances ...)``
    table keyed by ``/<sheet uuid>/<symbol uuid>`` (no root uuid), and every non-power
    ``Reference`` property set to ``X1``, so only the table names the parts. kicad-cli 10
    reads it to the same 68 components (checked)."""
    from kicad_tools.sexp import Document
    from kicad_tools.sexp.parser import SExp

    root_doc = Document.load(root)
    root_uuid = root_doc.root.find_children("uuid")[0].get_string(0)
    table = SExp.list("symbol_instances")
    for doc in (root_doc, Document.load(sub)):
        for symbol in doc.root.find_children("symbol"):
            sym_uuid = symbol.find_children("uuid")[0].get_string(0)
            for instances in symbol.find_children("instances"):
                for project in instances.find_children("project"):
                    for path in project.find_children("path"):
                        legacy_path = path.get_string(0)[len("/" + root_uuid) :] + "/" + sym_uuid
                        table.append(
                            SExp.list(
                                "path",
                                SExp.quoted_atom(legacy_path),
                                SExp.list("reference", SExp.quoted_atom(path.find_children("reference")[0].get_string(0))),
                                SExp.list("unit", path.find_children("unit")[0].get_value(0)),
                            )
                        )
                symbol.remove(instances)
            for prop in symbol.find_children("property"):
                if prop.get_string(0) == "Reference" and not (prop.get_string(1) or "").startswith("#"):
                    prop.set_value(1, "X1")
        for version in doc.root.find_children("version"):
            version.set_value(0, 20211123)
        if doc is not root_doc:
            doc.save(sub)
    root_doc.root.append(table)
    root_doc.save(root)
