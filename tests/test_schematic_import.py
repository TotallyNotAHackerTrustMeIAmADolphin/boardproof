"""Ticket 17: import footprints and nets from the schematic (propose/confirm).

Fixture: a tmp copy of evals/rung1/project (never edited in place). Expected
nets come from evals/rung1/nets.json (kicad-cli netlist, ticket 7); expectations
about the written board are read from the raw S-expression after a fresh reload.

Variants of the schematic and board are built through the write layer (CLAUDE.md hard rule 1,
build ticket 74): load, change the tree with typed atoms, save (parse-checked by kicad-cli). A
change on disk after a proposal goes through ``tests/_kicad_faults.drift``.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp

from kicad_mcp import checkpoint
from kicad_mcp import schematic_import as si
from kicad_mcp import write_layer
from tests import _kicad_faults as faults

REPO = Path(__file__).resolve().parents[1]
RUNG1 = REPO / "evals" / "rung1"
NAME = "rung1_usbc_attiny1616"


def _kicad_cli_available() -> bool:
    try:
        from kicad_mcp.kicad_cli import find_kicad_cli

        find_kicad_cli()
        return True
    except Exception:  # noqa: BLE001
        return False


pytestmark = pytest.mark.skipif(not _kicad_cli_available(), reason="kicad-cli not installed")


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    dest = tmp_path / "proj"
    shutil.copytree(RUNG1 / "project", dest)
    return dest


@pytest.fixture()
def pcb(project: Path) -> Path:
    return project / f"{NAME}.kicad_pcb"


def _tree_digest(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _footprints(pcb_path: Path) -> dict[str, dict]:
    """ref -> {name, at, layer, pads: {number: net name}} from the raw S-expression."""
    from kicad_tools.sexp import parse_file

    root = parse_file(str(pcb_path))
    out: dict[str, dict] = {}
    for fp in root.children:
        if fp.name != "footprint":
            continue
        ref = next(  # KiCad 8+ property form, or the legacy fp_text form of EasyEDA libraries
            str(p.values[1])
            for p in fp.children
            if (p.name == "property" and str(p.values[0]) == "Reference")
            or (p.name == "fp_text" and str(p.values[0]) == "reference")
        )
        pads: dict[str, set[str]] = {}
        for pad in fp.children:
            if pad.name != "pad":
                continue
            net = pad.find("net")
            pads.setdefault(str(pad.values[0]), set()).add(
                str(net.values[1]) if net is not None and len(net.values) > 1 else ""
            )
        at = fp.find("at")
        out[ref] = {
            "name": str(fp.values[0]),
            "at": [float(v) for v in at.values[:2]],
            "layer": str(fp.find("layer").values[0]),
            "pads": pads,
        }
    return out


def _expected_nets() -> dict[str, list[str]]:
    return json.loads((RUNG1 / "nets.json").read_text(encoding="utf-8"))


def _outline_bbox(pcb_path: Path) -> tuple[float, float, float, float]:
    from kicad_tools.sexp import parse_file

    xs, ys = [], []
    for n in parse_file(str(pcb_path)).children:
        if n.name == "gr_line" and str(n.find("layer").values[0]) == "Edge.Cuts":
            for k in ("start", "end"):
                xs.append(float(n.find(k).values[0]))
                ys.append(float(n.find(k).values[1]))
    return min(xs), min(ys), max(xs), max(ys)


# --- slice 1: dry run ------------------------------------------------------


def test_dry_run_lists_every_footprint_and_writes_nothing(project: Path, pcb: Path, tmp_path: Path) -> None:
    before = _tree_digest(tmp_path)
    out = tmp_path / "out"
    result = si.propose_schematic_import(pcb, output_dir=out)
    assert result["status"] == "proposed", result
    assert result["revision_token"]
    refs = sorted(f["reference"] for f in result["footprints"])
    assert refs == ["C1", "C2", "C3", "D1", "D2", "F1", "J1", "J2", "R1", "R2", "R3", "U1", "U2"]
    j1 = next(f for f in result["footprints"] if f["reference"] == "J1")
    assert j1["library_id"] == "Rung1_USBC:USB_C_Receptacle_Shouhan_TYPE-C-6P"
    assert j1["source"] == "project"  # resolved through the project fp-lib-table
    assert result["unresolved"] == []
    assert not out.exists()
    assert _tree_digest(tmp_path) == before  # no netlist file next to the schematic either


def test_staging_grid_is_outside_the_outline_and_deterministic(pcb: Path, tmp_path: Path) -> None:
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out", columns=4, spacing=12.0)
    x0, y0, x1, y1 = _outline_bbox(pcb)
    pos = {f["reference"]: f["staging_at"] for f in result["footprints"]}
    assert all(x > x1 for x, _ in pos.values()), "every staged footprint lies right of the outline"
    assert pos["C1"][1] == pos["C2"][1] == pos["C3"][1] == pos["D1"][1]  # first row of 4
    assert pos["C1"][0] < pos["C2"][0] < pos["C3"][0] < pos["D1"][0]
    assert pos["D2"][1] == pos["C1"][1] + 12.0  # natural order, row 2
    again = si.propose_schematic_import(pcb, output_dir=tmp_path / "out", columns=4, spacing=12.0)
    assert {f["reference"]: f["staging_at"] for f in again["footprints"]} == pos
    assert result["staging"]["origin"] == [x1 + 10.0, y0]


def test_declared_staging_origin_is_used(pcb: Path, tmp_path: Path) -> None:
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "o", staging_origin=(200.0, 50.0))
    pos = {f["reference"]: f["staging_at"] for f in result["footprints"]}
    assert pos["C1"] == [200.0, 50.0]
    assert result["staging"]["origin"] == [200.0, 50.0]
    assert result["staging"]["declared"] is True


def test_unresolved_footprint_is_named_at_propose_and_issues_no_token(pcb: Path, project: Path, tmp_path: Path) -> None:
    (project / "lib" / "Rung1_USBC" / "Rung1_USBC.pretty" / "USB_C_Receptacle_Shouhan_TYPE-C-6P.kicad_mod").unlink()
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "o")
    assert result["status"] == "blocked"
    assert result["revision_token"] is None
    assert [u["reference"] for u in result["unresolved"]] == ["J1"]
    assert "J1" in result["reason"] and "Rung1_USBC:USB_C_Receptacle_Shouhan_TYPE-C-6P" in result["reason"]


# --- slice 2: confirm, happy path -----------------------------------------------


def _import(pcb: Path, out: Path, **kw) -> dict:
    proposal = si.propose_schematic_import(pcb, output_dir=out, **kw)
    assert proposal["status"] == "proposed", proposal
    return si.confirm_schematic_import(pcb, proposal["revision_token"])


def test_confirm_places_all_13_footprints_and_every_expected_net(pcb: Path, project: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    result = _import(pcb, out)
    assert result["status"] == "imported", result
    new_pcb = out / pcb.name
    assert result["output_pcb"] == str(new_pcb)

    fps = _footprints(new_pcb)  # fresh parse from disk, raw nodes
    assert sorted(fps) == ["C1", "C2", "C3", "D1", "D2", "F1", "J1", "J2", "R1", "R2", "R3", "U1", "U2"]
    assert fps["J1"]["name"] == "Rung1_USBC:USB_C_Receptacle_Shouhan_TYPE-C-6P"
    assert {p for p in fps["J1"]["pads"]} >= {"A5", "B5", "A9", "B9", "A12", "B12", "7"}

    # nets.json is kicad-cli's netlist; the only difference is the sheet-path "/" on local labels.
    expected = _expected_nets()
    found: dict[str, set[str]] = {}
    for ref, fp in fps.items():
        for number, nets in fp["pads"].items():
            assert len(nets) == 1, f"{ref}.{number} carries several nets {nets}"
            (net,) = nets
            if net:
                found.setdefault(net.lstrip("/"), set()).add(f"{ref}.{number}")
    assert {k: sorted(v) for k, v in found.items()} == {k: sorted(v) for k, v in expected.items()}
    assert not any("PWR_FLAG" in n or re.match(r"Net-\(", n) for n in found)
    # local labels keep KiCad's own sheet-path spelling
    assert fps["R1"]["pads"]["1"] == {"/CC1"}


def test_staged_positions_in_the_file_match_the_plan(pcb: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    proposal = si.propose_schematic_import(pcb, output_dir=out)
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "imported", result
    fps = _footprints(out / pcb.name)
    x0, y0, x1, y1 = _outline_bbox(pcb)
    for planned in proposal["footprints"]:
        assert fps[planned["reference"]]["at"] == pytest.approx(planned["staging_at"], abs=1e-6)
        assert fps[planned["reference"]]["at"][0] > x1
    assert _outline_bbox(out / pcb.name) == (x0, y0, x1, y1)  # outline untouched


def test_source_untouched_and_result_is_a_new_revision(pcb: Path, project: Path, tmp_path: Path) -> None:
    before = _tree_digest(project)
    out = tmp_path / "out"
    result = _import(pcb, out)
    assert result["status"] == "imported"
    assert _tree_digest(project) == before
    assert (out / "lib" / "Rung1_USBC" / "Rung1_USBC.pretty").is_dir()  # project libraries travel with the revision
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out", "proj"]  # no temp dir left behind


def test_second_import_into_the_result_adds_nothing(pcb: Path, tmp_path: Path) -> None:
    first = _import(pcb, tmp_path / "out")
    assert first["status"] == "imported"
    again = si.propose_schematic_import(tmp_path / "out" / pcb.name, output_dir=tmp_path / "out2")
    assert again["status"] == "proposed"
    assert again["footprints"] == []
    assert len(again["already_present"]) == 13


# --- slice 3: failure, cleanup, checkpoint -------------------------------------------

_J1_MOD = Path("lib") / "Rung1_USBC" / "Rung1_USBC.pretty" / "USB_C_Receptacle_Shouhan_TYPE-C-6P.kicad_mod"


def _stray(tmp_path: Path) -> list[str]:
    return sorted(p.name for p in tmp_path.iterdir() if p.name not in ("proj",))


def test_unresolved_at_confirm_fails_whole_import_names_part_and_writes_nothing(
    pcb: Path, tmp_path: Path, tmp_path_factory: pytest.TempPathFactory, monkeypatch
) -> None:
    # Build ticket 22: the token carries the composite project hash, so a library file INSIDE the
    # project tree that changes is drift (next test). The re-plan at confirm still catches a
    # library OUTSIDE the tree, here one a global fp-lib-table points at.
    from kicad_mcp.schematic_import import _installed_footprint_dirs

    lib = tmp_path / "globallib" / "Resistor_SMD.pretty"
    shutil.copytree(_installed_footprint_dirs()[0] / "Resistor_SMD.pretty", lib)
    appdata = tmp_path / "appdata"
    (appdata / "kicad" / "10.0").mkdir(parents=True)
    table = _node("fp_lib_table", _node("version", 7), _node(
        "lib", _node("name", "Resistor_SMD"), _node("type", "KiCad"), _node("uri", lib.as_posix()),
        _node("options", ""), _node("descr", ""),
    ))
    (appdata / "kicad" / "10.0" / "fp-lib-table").write_text(write_layer.serialise(table) + "\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    out = tmp_path / "out"
    proposal = si.propose_schematic_import(pcb, output_dir=out)
    assert proposal["status"] == "proposed"
    r1 = next(f for f in proposal["footprints"] if f["reference"] == "R1")
    assert r1["source"] == "global"
    users = sorted(f["reference"] for f in proposal["footprints"] if f["library_id"] == r1["library_id"])
    mod = lib / f"{r1['library_id'].split(':', 1)[1]}.kicad_mod"
    aside = tmp_path_factory.mktemp("aside") / mod.name  # outside tmp_path: _stray stays exact
    shutil.move(mod, aside)  # the library changes between propose and confirm
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "error"
    assert "R1" in result["reason"] and r1["library_id"] in result["reason"]
    assert sorted(u["reference"] for u in result["unresolved"]) == users
    # no out dir, no temp dir: the other parts were not written either
    assert _stray(tmp_path) == ["appdata", "globallib"]
    shutil.move(aside, mod)  # token stays usable once the library is fixed
    assert si.confirm_schematic_import(pcb, proposal["revision_token"])["status"] == "imported"


def test_a_library_inside_the_project_that_changes_is_drift_and_spends_the_token(
    pcb: Path, project: Path, tmp_path: Path
) -> None:
    out = tmp_path / "out"
    proposal = si.propose_schematic_import(pcb, output_dir=out)
    assert proposal["status"] == "proposed"
    mod = project / _J1_MOD
    aside = tmp_path.parent / f"{tmp_path.name}-aside" / mod.name
    aside.parent.mkdir()
    shutil.move(mod, aside)  # a project input file changes between propose and confirm
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "error" and "changed since the proposal was made" in result["reason"]
    assert _stray(tmp_path) == []
    shutil.move(aside, mod)  # restoring the file does not revive the proposal
    again = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert again["reason"] == "unknown or expired revision_token"


def test_footprint_with_empty_field_or_unknown_library_is_unresolved(pcb: Path, tmp_path: Path) -> None:
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "o", footprint_dirs=[tmp_path / "nowhere"])
    # With no installed libraries only the project library resolves: every stock part is named.
    assert result["status"] == "blocked"
    assert sorted(u["reference"] for u in result["unresolved"]) == [
        "C1", "C2", "C3", "D1", "D2", "F1", "J2", "R1", "R2", "R3", "U1", "U2",
    ]
    assert "J1" not in result["reason"]


def test_netlist_comes_from_kicad_cli_and_cli_is_put_on_path_first(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    import kicad_mcp.kicad_cli as cli

    calls: list[str] = []
    real = cli.ensure_kicad_cli_on_path

    def spy():
        calls.append("ensure")
        return real()

    monkeypatch.setattr(cli, "ensure_kicad_cli_on_path", spy)
    assert si.propose_schematic_import(pcb, output_dir=tmp_path / "o")["status"] == "proposed"
    assert calls == ["ensure"]


def test_exporter_failure_is_incomplete_not_a_silent_fallback(pcb: Path, tmp_path: Path) -> None:
    def boom(sch, out):
        raise RuntimeError("kicad-cli failed: boom")

    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "o", netlist_exporter=boom)
    assert result["status"] == "incomplete"
    assert "boom" in result["reason"]
    assert "revision_token" not in result


def test_token_is_single_use(pcb: Path, tmp_path: Path) -> None:
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    assert si.confirm_schematic_import(pcb, proposal["revision_token"])["status"] == "imported"
    again = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert again["status"] == "error"


def test_source_drift_after_proposal_is_rejected(pcb: Path, project: Path, tmp_path: Path) -> None:
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    sch = project / f"{NAME}.kicad_sch"
    faults.drift(sch)  # test setup only: simulate a change on disk
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "error" and "changed" in result["reason"]
    assert _stray(tmp_path) == []


def test_token_expires_by_injected_clock(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    now = [1000.0]
    monkeypatch.setattr(si, "_clock", lambda: now[0])
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    now[0] += checkpoint.TTL_SECONDS + 1
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "error" and "expired" in result["reason"]


def test_confirm_with_a_different_pcb_path_is_refused_and_token_survives(
    pcb: Path, project: Path, tmp_path: Path
) -> None:
    other = tmp_path / "other"
    shutil.copytree(project, other)
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    refused = si.confirm_schematic_import(other / pcb.name, proposal["revision_token"])
    assert refused["status"] == "error"
    assert si.confirm_schematic_import(pcb, proposal["revision_token"])["status"] == "imported"


def test_output_dir_must_be_a_new_revision(pcb: Path, project: Path, tmp_path: Path) -> None:
    assert si.propose_schematic_import(pcb, output_dir=project)["status"] == "error"
    assert si.propose_schematic_import(pcb, output_dir=project / "sub")["status"] == "error"
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "x").write_text("x")
    assert si.propose_schematic_import(pcb, output_dir=busy)["status"] == "error"


def test_failed_save_leaves_nothing_and_token_stays_usable(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    real = write_layer.save_pcb

    def boom(pcb, path=None):
        raise OSError("disk full")

    out = tmp_path / "deep" / "er" / "out"
    proposal = si.propose_schematic_import(pcb, output_dir=out)
    monkeypatch.setattr(write_layer, "save_pcb", boom)
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "incomplete" and "disk full" in result["reason"]
    assert _stray(tmp_path) == []  # created parents removed too
    monkeypatch.setattr(write_layer, "save_pcb", real)
    assert si.confirm_schematic_import(pcb, proposal["revision_token"])["status"] == "imported"


def test_a_wrong_net_in_the_saved_file_is_caught_after_reload_and_cleaned(
    pcb: Path, tmp_path: Path, monkeypatch
) -> None:
    from kicad_tools.schema import PCB

    real = PCB.assign_nets_from_netlist

    def wrong(self, netlist, pin_to_pad_map=None):
        stats = real(self, netlist, pin_to_pad_map)
        self.assign_net_to_footprint_pad("R1", "1", "PWR_FLAG")  # the mislabel ticket 10 saw
        return stats

    monkeypatch.setattr(PCB, "assign_nets_from_netlist", wrong)
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "error" and "CRITICAL" in result["reason"]
    assert "R1.1" in result["reason"]
    assert _stray(tmp_path) == []


def test_a_pin_without_a_pad_fails_the_import(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    from kicad_tools.operations.netlist import NetNode

    def exporter(sch, out):
        netlist = si._kicad_cli_netlist(sch, out)
        netlist.nets[-1].nodes.append(NetNode(reference="R1", pin="99"))
        return netlist

    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out", netlist_exporter=exporter)
    assert result["status"] == "proposed"
    failed = si.confirm_schematic_import(pcb, result["revision_token"], netlist_exporter=exporter)
    assert failed["status"] == "error" and "R1.99" in failed["reason"]
    assert _stray(tmp_path) == []


def test_imported_board_loads_in_kicad_cli(pcb: Path, tmp_path: Path) -> None:
    from kicad_mcp.kicad_cli import run_drc

    out = tmp_path / "out"
    assert _import(pcb, out)["status"] == "imported"
    report = run_drc(out / pcb.name)
    assert "violations" in report or "unconnected_items" in report
    # unrouted: every net with two or more pads is reported unconnected, not hidden
    assert report.get("unconnected_items")


# --- slice 4: review round (fail closed, symbol link, re-import contract, plan hash) ----------


def _node(name: str, *children) -> SExp:
    """A list node with typed atoms: a str is a quoted string, an int or float a number, an
    SExp (``write_layer.symbol`` for a bare keyword, or a nested node) goes in as it is."""
    atoms = []
    for c in children:
        if isinstance(c, SExp):
            atoms.append(c)
        elif isinstance(c, str):
            atoms.append(write_layer.string(c))
        elif isinstance(c, (int, float)) and not isinstance(c, bool):
            atoms.append(write_layer.number(c))
        else:
            raise TypeError(f"no KiCad atom for {c!r}")
    return SExp.list(name, *atoms)


def _walk(node: SExp):
    yield node
    for child in node.children:
        if not child.is_atom:
            yield from _walk(child)


def _value(node: SExp, index: int = 0):
    child = node.children[index] if len(node.children) > index else None
    return None if child is None or not child.is_atom else child.value


def _symbol_with_ref(root: SExp, ref: str) -> SExp:
    found = [
        s for s in root.children
        if s.name == "symbol" and any(p.name == "property" and _value(p) == "Reference" and _value(p, 1) == ref for p in s.children)
    ]
    assert len(found) == 1, f"{len(found)} placed symbols with reference {ref!r}"
    return found[0]


def _edit_sch(project: Path, edit) -> None:
    """Test setup only: derive a variant schematic inside a tmp copy, through the write layer
    (load, ``edit(root)`` changes the tree in place, save with kicad-cli's parse check)."""
    sch = project / f"{NAME}.kicad_sch"
    root = write_layer.load(sch)
    edit(root)
    write_layer.save(root, sch)


def _rename_ref(old: str, new: str):
    """Every ``(property "Reference" old)`` and instance ``(reference old)``, renamed."""

    def edit(root: SExp) -> None:
        props = [n for n in _walk(root) if n.name == "property" and _value(n) == "Reference" and _value(n, 1) == old]
        refs = [n for n in _walk(root) if n.name == "reference" and _value(n) == old]
        assert props and refs, f"no symbol {old} to rename"
        for n in props:
            write_layer.set_string(n, 1, new)
        for n in refs:
            write_layer.set_string(n, 0, new)

    return edit


def _symbol_uuids(sch_path: Path) -> dict[str, str]:
    """ref -> symbol uuid, from the raw schematic (independent of the netlist)."""
    out: dict[str, str] = {}
    for sym in _parse_sch(sch_path).children:
        if sym.name != "symbol":
            continue
        ref = next(
            (str(p.values[1]) for p in sym.children if p.name == "property" and str(p.values[0]) == "Reference"),
            None,
        )
        uuid = sym.find("uuid")
        if ref and uuid is not None:
            out[ref] = str(uuid.values[0])
    return out


def _parse_sch(path: Path):
    from kicad_tools.sexp import parse_file

    return parse_file(str(path))


def _schematic(sheet_uuid: str, *items: SExp, generator: str = "eeschema", version: int = 20231120) -> SExp:
    """A schematic with no symbols: header, empty lib_symbols, ``items``, sheet instances."""
    return _node(
        "kicad_sch", _node("version", version), _node("generator", generator), _node("generator_version", "10.0"),
        _node("uuid", sheet_uuid), _node("paper", "A4"), _node("lib_symbols"),
        *items,
        _node("sheet_instances", _node("path", "/", _node("page", "1"))),
    )


def _sheet(x: float, y: float, w: float, h: float, sheet_uuid: str, name: str, file: str, *more: SExp) -> SExp:
    return _node(
        "sheet", _node("at", x, y), _node("size", w, h), _node("uuid", sheet_uuid),
        _node("property", "Sheetname", name, _node("at", x, y - 1, 0)),
        _node("property", "Sheetfile", file, _node("at", x, y + h + 1, 0)),
        *more,
    )


def test_missing_sub_sheet_is_blocked_not_an_empty_import(pcb: Path, project: Path, tmp_path: Path) -> None:
    sub = project / "missing_sub.kicad_sch"  # saved so the layer can parse-check the root, then removed
    write_layer.save(_schematic("66666666-2222-4333-8444-555555555555"), sub)
    sheet = _sheet(10, 10, 20, 20, "11111111-2222-3333-4444-555555555555", "sub", sub.name)
    _edit_sch(project, lambda root: root.append(sheet))
    sub.unlink()
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    assert result["status"] == "blocked", result
    assert result["revision_token"] is None
    assert "missing_sub.kicad_sch" in result["reason"]
    assert not (tmp_path / "out").exists()


def test_empty_schematic_is_blocked(pcb: Path, project: Path, tmp_path: Path) -> None:
    empty = _schematic("432d0ab3-e039-4b50-b190-b3042fcc4302", generator="test", version=20250114)
    write_layer.save(empty, project / f"{NAME}.kicad_sch")
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    assert result["status"] == "blocked" and result["revision_token"] is None
    assert "no components" in result["reason"]


def test_unannotated_references_block_and_are_named(pcb: Path, project: Path, tmp_path: Path) -> None:
    _edit_sch(project, _rename_ref("R1", "R?"))
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    assert result["status"] == "blocked" and result["revision_token"] is None
    assert "R?" in result["reason"]


def test_duplicate_references_block_and_name_both_symbols(pcb: Path, project: Path, tmp_path: Path) -> None:
    _edit_sch(project, _rename_ref("R2", "R1"))
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    assert result["status"] == "blocked" and result["revision_token"] is None
    assert result["reason"].count("value ") == 2, result["reason"]
    assert "R1 unit 1 value '5.1k'" in result["reason"]


def test_footprints_carry_the_symbol_path_sheetname_and_sheetfile(pcb: Path, tmp_path: Path) -> None:
    from kicad_tools.sexp import parse_file

    out = tmp_path / "out"
    assert _import(pcb, out)["status"] == "imported"
    uuids = _symbol_uuids(pcb.with_suffix(".kicad_sch"))
    assert len(uuids) >= 13
    seen = 0
    for fp in parse_file(str(out / pcb.name)).children:
        if fp.name != "footprint":
            continue
        ref = next(str(p.values[1]) for p in fp.children if p.name == "fp_text" and str(p.values[0]) == "reference") \
            if fp.find("path") is None else None
        path = fp.find("path")
        assert path is not None, f"footprint {ref or fp.values[0]} has no (path ...)"
        sym = str(path.values[0])
        assert sym.startswith("/") and sym.count("/") == 1
        assert sym[1:] in uuids.values()
        assert str(fp.find("sheetfile").values[0]) == f"{NAME}.kicad_sch"
        assert fp.find("sheetname") is not None
        seen += 1
    assert seen == 13


def test_dnp_symbol_sets_the_dnp_attribute_only_on_that_footprint(pcb: Path, project: Path, tmp_path: Path) -> None:
    def dnp(root: SExp) -> None:
        flag = _symbol_with_ref(root, "R3").find("dnp")
        assert _value(flag) == "no"
        write_layer.set_symbol(flag, 0, "yes")

    _edit_sch(project, dnp)
    out = tmp_path / "out"
    assert _import(pcb, out)["status"] == "imported"
    from kicad_tools.sexp import parse_file

    flagged = []
    for fp in parse_file(str(out / pcb.name)).children:
        attr = fp.find("attr") if fp.name == "footprint" else None
        if attr is not None and "dnp" in [str(v) for v in attr.values]:
            ref = next(str(p.values[1]) for p in fp.children if p.name in ("fp_text", "property"))
            flagged.append(ref)
    assert flagged == ["R3"]


def test_slow_exporter_becomes_blocked_with_a_reason(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    import time

    monkeypatch.setattr(si, "_EXPORT_TIMEOUT", 0.3)

    def slow(sch, out):
        time.sleep(5)

    started = time.time()
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out", netlist_exporter=slow)
    assert time.time() - started < 3
    assert result["status"] == "blocked" and result["revision_token"] is None
    assert "did not finish" in result["reason"]


def test_kicad_cli_subprocess_timeout_is_blocked(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    import subprocess

    def hang(cmd, **kw):
        assert kw.get("timeout")  # the call is made with a timeout
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(si.subprocess, "run", hang)
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    assert result["status"] == "blocked" and "timed out" in result["reason"]


def test_later_import_never_lands_on_an_existing_footprint(pcb: Path, tmp_path: Path) -> None:
    first = tmp_path / "out"
    assert _import(pcb, first)["status"] == "imported"
    rev = first / pcb.name
    _edit_sch(first, _rename_ref("R3", "R9"))  # R3 -> R9: one new reference, R3 now an orphan
    existing = [fp["at"] for fp in _footprints(rev).values()]
    proposal = si.propose_schematic_import(rev, output_dir=tmp_path / "out2")
    assert proposal["status"] == "proposed", proposal
    assert [f["reference"] for f in proposal["footprints"]] == ["R9"]
    (x, y) = proposal["footprints"][0]["staging_at"]
    assert all(abs(x - ex) >= 7.5 or abs(y - ey) >= 7.5 for ex, ey in existing)
    assert proposal["staging"]["cells_skipped"] > 0
    assert proposal["orphans"] == ["R3"]


def test_changed_library_between_propose_and_confirm_is_refused(pcb: Path, project: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    proposal = si.propose_schematic_import(pcb, output_dir=out)
    assert proposal["status"] == "proposed"
    mod = project / _J1_MOD
    faults.drift(mod)  # same footprint, different file content
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "error" and "re-run propose" in result["reason"]
    assert not out.exists() and _stray(tmp_path) == []


def test_global_table_nickname_beats_the_installed_directory(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    lib = tmp_path / "globallib" / "Resistor_SMD.pretty"
    lib.mkdir(parents=True)
    from kicad_mcp.schematic_import import _installed_footprint_dirs

    stock = _installed_footprint_dirs()[0] / "Resistor_SMD.pretty" / "R_0603_1608Metric.kicad_mod"
    shutil.copy(stock, lib / "R_0603_1608Metric.kicad_mod")
    appdata = tmp_path / "appdata"
    (appdata / "kicad" / "10.0").mkdir(parents=True)
    # The write layer has no library-table kind (no kicad-cli parse check exists for one): the
    # table is built from typed atoms and written with the layer's serialiser.
    table = _node("fp_lib_table", _node("version", 7), _node(
        "lib", _node("name", "Resistor_SMD"), _node("type", "KiCad"), _node("uri", lib.as_posix()),
        _node("options", ""), _node("descr", ""),
    ))
    (appdata / "kicad" / "10.0" / "fp-lib-table").write_text(write_layer.serialise(table) + "\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    r1 = next(f for f in result["footprints"] if f["reference"] == "R1")
    assert r1["source"] == "global"
    assert Path(r1["file"]).parent == tmp_path / "globallib" / "Resistor_SMD.pretty"


def test_a_wrong_raw_position_in_the_saved_file_is_caught(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    from kicad_tools.schema import PCB

    real = PCB.assign_nets_from_netlist

    def shift(self, netlist, pin_to_pad_map=None):
        stats = real(self, netlist, pin_to_pad_map)
        node = self.get_footprint("J1")._sexp_node
        at = node.find("at")
        at.set_value(0, float(at.values[0]) + 3.0)  # the raw (at) now disagrees with the plan
        return stats

    monkeypatch.setattr(PCB, "assign_nets_from_netlist", shift)
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "error" and "CRITICAL" in result["reason"]
    assert "J1" in result["reason"] and "(at" in result["reason"]
    assert _stray(tmp_path) == []


def test_keyboard_interrupt_leaves_nothing_behind_and_token_usable(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    real = write_layer.save_pcb
    out = tmp_path / "new" / "parent" / "out"
    proposal = si.propose_schematic_import(pcb, output_dir=out)

    def interrupt(pcb, path=None):
        raise KeyboardInterrupt

    monkeypatch.setattr(write_layer, "save_pcb", interrupt)
    with pytest.raises(KeyboardInterrupt):
        si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert _stray(tmp_path) == []
    monkeypatch.setattr(write_layer, "save_pcb", real)
    assert si.confirm_schematic_import(pcb, proposal["revision_token"])["status"] == "imported"


# --- slice 5: delta review (copper, net table, rounding, temp dirs, project instances, reuse) ----


_LABELS = ("global_label", "label", "hierarchical_label")


def _relabel(names: dict[str, str]):
    """Every label whose text is a key of ``names`` gets the mapped text; each key must occur."""

    def edit(root: SExp) -> None:
        labels = [n for n in _walk(root) if n.name in _LABELS and _value(n) in names]
        assert {_value(n) for n in labels} == set(names), f"labels {sorted(names)} not all found"
        for n in labels:
            write_layer.set_string(n, 0, names[_value(n)])

    return edit


def _swap_labels(a: str, b: str):
    """Real schematic change: two nets swap their label text, so pads change net."""
    return _relabel({a: b, b: a})


def _rename_label(old: str, new: str):
    return _relabel({old: new})


def _first_revision(pcb: Path, tmp_path: Path, name: str = "out") -> Path:
    out = tmp_path / name
    assert _import(pcb, out)["status"] == "imported"
    return out


def test_reimport_contract_with_a_real_changed_schematic(pcb: Path, tmp_path: Path) -> None:
    first = _first_revision(pcb, tmp_path)
    rev = first / pcb.name
    before = _footprints(rev)

    def change(root: SExp) -> None:
        _swap_labels("UPDI", "LED_CTL")(root)  # real renet: pads of two nets trade nets
        _rename_ref("R3", "R9")(root)  # R3 gone from the schematic, R9 new
        fields = [
            n for n in _walk(root)
            if n.name == "property" and _value(n) == "Footprint" and _value(n, 1) == "Capacitor_SMD:C_0805_2012Metric"
        ]
        assert fields, "no 0805 capacitor footprint field to change"
        for n in fields:
            write_layer.set_string(n, 1, "Capacitor_SMD:C_1206_3216Metric")

    _edit_sch(first, change)
    proposal = si.propose_schematic_import(rev, output_dir=tmp_path / "out2")
    assert proposal["status"] == "proposed", proposal
    assert [f["reference"] for f in proposal["footprints"]] == ["R9"]
    assert proposal["orphans"] == ["R3"]
    assert [m["reference"] for m in proposal["footprint_mismatches"]] == ["C1"]
    assert {(r["reference"], r["pad"], r["board_net"], r["schematic_net"]) for r in proposal["renetted"]} == {
        ("J2", "2", "/UPDI", "/LED_CTL"),
        ("U1", "19", "/UPDI", "/LED_CTL"),
        ("U1", "8", "/LED_CTL", "/UPDI"),
    }
    result = si.confirm_schematic_import(rev, proposal["revision_token"])
    assert result["status"] == "imported", result
    after = _footprints(tmp_path / "out2" / pcb.name)
    assert after["J2"]["pads"]["2"] == {"/LED_CTL"} and after["U1"]["pads"]["8"] == {"/UPDI"}
    assert after["R9"]["pads"]["1"] == {"/UPDI"}  # a new part takes the schematic's net
    for ref, was in before.items():  # existing lib id and position never change; orphans stay
        assert after[ref]["name"] == was["name"] and after[ref]["at"] == was["at"]
    assert "R3" in after and after["C1"]["name"] == before["C1"]["name"]


def test_renet_on_a_board_with_copper_on_the_nets_is_blocked(pcb: Path, tmp_path: Path) -> None:
    from kicad_tools.schema import PCB

    first = _first_revision(pcb, tmp_path)
    rev = first / pcb.name
    board = PCB.load(rev)
    board.add_trace((10.0, 10.0), (20.0, 10.0), width=0.2, layer="F.Cu", net="/UPDI")
    write_layer.save_pcb(board, rev)
    _edit_sch(first, _swap_labels("UPDI", "LED_CTL"))
    result = si.propose_schematic_import(rev, output_dir=tmp_path / "out2")
    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "segment" in result["reason"] and "(/UPDI)" in result["reason"]
    assert "J2.2" in result["reason"] and "U1.19" in result["reason"]
    assert not (tmp_path / "out2").exists()


def test_renet_without_copper_on_those_nets_is_allowed_even_with_other_copper(pcb: Path, tmp_path: Path) -> None:
    from kicad_tools.schema import PCB

    first = _first_revision(pcb, tmp_path)
    rev = first / pcb.name
    board = PCB.load(rev)
    board.add_trace((10.0, 10.0), (20.0, 10.0), width=0.2, layer="F.Cu", net="GND")
    write_layer.save_pcb(board, rev)
    _edit_sch(first, _swap_labels("UPDI", "LED_CTL"))
    assert si.propose_schematic_import(rev, output_dir=tmp_path / "out2")["status"] == "proposed"


def test_renamed_net_is_predicted_at_propose_not_failed_at_confirm(pcb: Path, tmp_path: Path) -> None:
    first = _first_revision(pcb, tmp_path)
    _edit_sch(first, _rename_label("UPDI", "UPDI_NEW"))
    result = si.propose_schematic_import(first / pcb.name, output_dir=tmp_path / "out2")
    assert result["status"] == "blocked" and result["revision_token"] is None, result
    assert "net renamed or removed in the schematic: reconcile the net table" in result["reason"]
    assert "/UPDI -> /UPDI_NEW" in result["reason"]


def test_staging_survives_outline_coordinates_with_many_decimals(pcb: Path, tmp_path: Path) -> None:
    board = write_layer.load(pcb)  # test setup: an outline corner with a long fractional part
    board.append(_node(
        "gr_line", _node("start", 170.1234567, 100.7654321), _node("end", 190.12345678, 140.5),
        _node("stroke", _node("width", 0.1), _node("type", write_layer.symbol("default"))),
        _node("layer", "Edge.Cuts"), _node("uuid", "bbbbbbbb-0000-4000-8000-000000000001"),
    ))
    write_layer.save(board, pcb)
    assert _outline_bbox(pcb)[2] == 190.12345678  # the long fraction is on disk, not rounded
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    assert proposal["status"] == "proposed", proposal
    for f in proposal["footprints"]:
        assert all(round(v, 3) == v for v in f["staging_at"])
    assert si.confirm_schematic_import(pcb, proposal["revision_token"])["status"] == "imported"


def test_declared_origin_with_many_decimals_is_rounded_and_survives(pcb: Path, tmp_path: Path) -> None:
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out", staging_origin=(200.123456789, 50.987654321))
    assert proposal["staging"]["origin"] == [200.123, 50.988]
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "imported", result


def test_timeout_paths_leave_no_temp_dirs(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    import subprocess
    import tempfile
    import time

    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))

    def hang(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw["timeout"])

    monkeypatch.setattr(si.subprocess, "run", hang)
    assert si.propose_schematic_import(pcb, output_dir=tmp_path / "o1")["status"] == "blocked"
    assert list(scratch.iterdir()) == []

    monkeypatch.undo()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setattr(si, "_EXPORT_TIMEOUT", 0.2)
    monkeypatch.setattr(si, "_JOIN_GRACE", 0.2)
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "o2", netlist_exporter=lambda s, o: time.sleep(3))
    assert result["status"] == "blocked"
    assert list(scratch.iterdir()) == []


def test_watcher_waits_longer_than_the_subprocess_timeout() -> None:
    assert si._JOIN_GRACE > 0  # the child is killed by its own timeout before the watcher gives up


def test_only_this_projects_instances_count_for_duplicates(pcb: Path, project: Path, tmp_path: Path) -> None:
    def other_project(root: SExp) -> None:
        projects = [n for n in _walk(root) if n.name == "project" and _value(n) == "project"]
        assert projects
        for n in projects:
            write_layer.set_string(n, 0, NAME)
        instances = _symbol_with_ref(root, "R2").find("instances")
        assert [_value(p) for p in instances.children] == [NAME]
        instances.append(_node("project", "other", _node("path", "/aaaaaaaa-aaaa-4aaa-aaaa-aaaaaaaaaaaa", _node("reference", "R1"), _node("unit", 1))))

    _edit_sch(project, other_project)
    result = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    assert result["status"] == "proposed", result
    # control: a real duplicate under this project is still found with matching instance names
    _edit_sch(project, _rename_ref("R2", "R1"))
    assert si.propose_schematic_import(pcb, output_dir=tmp_path / "out2")["status"] == "blocked"


def test_annotation_gate_contract_is_pinned(project: Path) -> None:
    import inspect

    from kicad_mcp import verify_board as gate

    assert list(inspect.signature(gate._annotation_problems).parameters) == ["root_sheet"]
    sch = project / f"{NAME}.kicad_sch"
    clean = gate._annotation_problems(sch)
    assert isinstance(clean, list) and clean == []
    _edit_sch(project, _rename_ref("R1", "R?"))
    problems = gate._annotation_problems(sch)
    assert problems and all(isinstance(p, str) for p in problems) and any("R?" in p or "?" in p for p in problems)


def test_reused_sheet_gives_each_instance_its_own_path(project: Path, tmp_path: Path) -> None:
    from kicad_tools.sexp import parse_file

    root_uuid = "11111111-1111-4111-8111-111111111111"
    sh_a = "22222222-2222-4222-8222-222222222222"
    sh_b = "33333333-3333-4333-8333-333333333333"
    sub = write_layer.load(project / f"{NAME}.kicad_sch")
    own_root = f"/{_value(sub.find('uuid'))}"  # the schematic's own root uuid (new at every rebuild of the rung-1 project)
    paths = [
        (project_node, path)
        for project_node in _walk(sub) if project_node.name == "project"
        for path in project_node.children
        if path.name == "path" and _value(path) == own_root and path.find("reference") is not None
    ]
    assert len(paths) >= 13
    for project_node, path in paths:  # each symbol gets one instance per sheet: ref and ref+100
        ref, unit = _value(path.find("reference")), _value(path.find("unit"))
        other = re.sub(r"(\d+)$", lambda k: str(int(k.group(1)) + 100), ref)
        write_layer.set_string(path, 0, f"/{root_uuid}/{sh_a}")
        second = _node("path", f"/{root_uuid}/{sh_b}", _node("reference", other), _node("unit", unit))
        project_node.children.insert(next(i for i, c in enumerate(project_node.children) if c is path) + 1, second)
    write_layer.save(sub, project / "power.kicad_sch")

    def sheet(x: float, u: str, n: str, pg: str) -> SExp:
        here = _node("instances", _node("project", "root", _node("path", f"/{root_uuid}", _node("page", pg))))
        return _sheet(x, 50, 30, 20, u, n, "power.kicad_sch", here)

    root = _schematic(root_uuid, sheet(50, sh_a, "PowerA", "2"), sheet(100, sh_b, "PowerB", "3"))
    write_layer.save(root, project / "root.kicad_sch")  # parse-checks power.kicad_sch with it
    shutil.copy(project / f"{NAME}.kicad_pcb", project / "root.kicad_pcb")
    root_pcb = project / "root.kicad_pcb"
    out = tmp_path / "out"
    result = _import(root_pcb, out)
    assert result["status"] == "imported", result
    symbol_uuid = _symbol_uuids(project / "power.kicad_sch")
    got: dict[str, str] = {}
    for fp in parse_file(str(out / "root.kicad_pcb")).children:
        if fp.name != "footprint":
            continue
        ref = si._reference_of(fp)
        got[ref] = str(fp.find("path").values[0])
    assert len(got) == 26
    for ref, uuid in symbol_uuid.items():
        if ref.startswith("#"):
            continue
        other = re.sub(r"(\d+)$", lambda k: str(int(k.group(1)) + 100), ref)
        assert got[ref] == f"/{sh_a}/{uuid}", (ref, got[ref])
        assert got[other] == f"/{sh_b}/{uuid}", (other, got[other])


def test_pcb_changed_between_propose_and_confirm_is_refused(pcb: Path, tmp_path: Path) -> None:
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    faults.drift(pcb)  # test setup: the board moves on after the proposal
    result = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert result["status"] == "error" and "changed" in result["reason"]
    assert _stray(tmp_path) == []
    again = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert again["status"] == "error" and "unknown or expired" in again["reason"]  # drift spends the token


def test_a_stale_plan_at_confirm_spends_the_token(pcb: Path, tmp_path: Path, monkeypatch) -> None:
    """A re-plan with another plan_hash refuses, and the note is true: the token is spent."""
    proposal = si.propose_schematic_import(pcb, output_dir=tmp_path / "out")
    real_plan = si._plan
    calls = [0]

    def other_hash(*args, **kwargs):
        plan = real_plan(*args, **kwargs)
        calls[0] += 1
        return {**plan, "plan_hash": "0" * 64} if calls[0] == 1 else plan

    monkeypatch.setattr(si, "_plan", other_hash)
    stale = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert stale["status"] == "error" and stale["stale"] is True
    assert "revision_token is spent" in stale["note"]
    again = si.confirm_schematic_import(pcb, proposal["revision_token"])
    assert again["status"] == "error" and again["reason"] == "unknown or expired revision_token"
    assert _stray(tmp_path) == []
