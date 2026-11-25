"""Courtyard must enclose every pad (issue 228): the raw check, the adoption fix, validate_klc, the
list of already adopted parts, and pack/align/distribute by the hull of courtyard and pads.

The measured case of a peer board: a 1206 capacitor footprint with a courtyard of 3.2 x 1.6 mm and
pads that reach 4.67 x 1.73 mm. Footprints are built through ``kicad_tools``' object model and saved
through the write layer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from kicad_tools.schema import PCB
from kicad_tools.sexp import SExp
from mcp.server.mcpserver import MCPServer

from boardproof import footprint_courtyard as fc
from boardproof import part_adoption as pa
from boardproof import write_layer
from boardproof.toolsets import library as library_toolset
from boardproof.toolsets import placement as placement_toolset

from tests.test_part_adoption import _part_assets, _propose, needs_cli


@pytest.fixture
def project(tmp_path: Path) -> Path:
    path = tmp_path / "proj" / "adopt.kicad_pcb"
    path.parent.mkdir()
    write_layer.save_pcb(PCB.create(width=40.0, height=30.0, title="adopt"), path)
    return path


def _s(value: str) -> SExp:
    return write_layer.string(value)


def _n(value: float) -> SExp:
    return write_layer.number(value)


def _pad(number: str, x: float, w: float = 1.5, h: float = 1.73, angle: float | None = None) -> SExp:
    at = [_n(x), _n(0)] + ([_n(angle)] if angle is not None else [])
    return SExp.list(
        "pad", _s(number), write_layer.symbol("smd"), write_layer.symbol("rect"),
        SExp.list("at", *at), SExp.list("size", _n(w), _n(h)),
        SExp.list("layers", _s("F.Cu"), _s("F.Mask"), _s("F.Paste")),
    )  # fmt: skip


def _crtyd(x0: float, y0: float, x1: float, y1: float) -> SExp:
    return SExp.list(
        "fp_rect", SExp.list("start", _n(x0), _n(y0)), SExp.list("end", _n(x1), _n(y1)),
        SExp.list("stroke", SExp.list("width", _n(0.05)), SExp.list("type", write_layer.symbol("solid"))),
        SExp.list("fill", write_layer.symbol("none")), SExp.list("layer", _s("F.CrtYd")),
    )  # fmt: skip


def _text(kind: str, text: str, y: float, layer: str) -> SExp:
    return SExp.list(
        "property", _s(kind), _s(text), SExp.list("at", _n(0), _n(y), _n(0)), SExp.list("layer", _s(layer)),
        SExp.list("effects", SExp.list("font", SExp.list("size", _n(1), _n(1)))),
    )  # fmt: skip


def c1206(path: Path, name: str = "C_1206", *, courtyard: tuple[float, float, float, float] | None = (-1.6, -0.8, 1.6, 0.8)) -> Path:
    """The peer's footprint: pads 1.5 x 1.73 at x = -1.585 and 1.585 (a 4.67 mm reach), a courtyard 3.2 x 1.6."""
    items = [
        _s(name), SExp.list("version", _n(20241229)), SExp.list("generator", _s("pcbnew")),
        SExp.list("layer", _s("F.Cu")), _text("Reference", "REF**", -2, "F.SilkS"),
        _text("Value", name, 2, "F.Fab"), SExp.list("attr", write_layer.symbol("smd")),
        _pad("1", -1.585), _pad("2", 1.585),
    ]  # fmt: skip
    if courtyard is not None:
        items.append(_crtyd(*courtyard))
    path.parent.mkdir(parents=True, exist_ok=True)
    write_layer.save(SExp.list("footprint", *items), path)
    return path


# --------------------------------------------------------------------------- the raw measurement


def test_the_peers_1206_courtyard_does_not_enclose_its_pads(tmp_path: Path) -> None:
    root = write_layer.load(c1206(tmp_path / "C_1206.kicad_mod"))
    result = fc.check(root)
    assert result["encloses"] is False
    assert result["courtyard_box"] == [-1.6, -0.8, 1.6, 0.8]
    assert result["pads_box"] == pytest.approx([-2.335, -0.865, 2.335, 0.865])  # 4.67 x 1.73
    assert result["uncovered_pads"] == ["1", "2"]


def test_a_courtyard_around_the_pads_passes_and_a_footprint_without_one_is_reported(tmp_path: Path) -> None:
    ok = fc.check(write_layer.load(c1206(tmp_path / "a.kicad_mod", courtyard=(-2.6, -1.1, 2.6, 1.1))))
    assert ok["encloses"] is True and ok["uncovered_pads"] == []
    none = fc.check(write_layer.load(c1206(tmp_path / "b.kicad_mod", courtyard=None)))
    assert none["encloses"] is False and none["courtyard_box"] is None


def test_a_turned_pad_is_measured_by_its_turned_extent(tmp_path: Path) -> None:
    root = write_layer.load(c1206(tmp_path / "c.kicad_mod", courtyard=(-2.6, -1.1, 2.6, 1.1)))
    pads = [c for c in root.children if not c.is_atom and c.name == "pad"]
    pads[1].find("at").children.append(_n(90))  # 1.5 x 1.73 turned: 1.73 wide, 1.5 high
    assert fc.check(root)["pads_box"] == pytest.approx([-2.335, -0.865, 1.585 + 0.865, 0.865])


def test_fix_sets_the_courtyard_to_pads_plus_margin_and_keeps_a_bigger_body(tmp_path: Path) -> None:
    root = write_layer.load(c1206(tmp_path / "C_1206.kicad_mod"))
    fixed = fc.fix(root)
    assert fixed == [-2.59, -1.12, 2.59, 1.12]  # pads 2.335 / 0.865 plus 0.25, on the 0.01 grid
    after = fc.check(root)
    assert after["encloses"] is True and after["courtyard_box"] == fixed
    layers = [c.find("layer").get_string(0) for c in root.children if not c.is_atom and c.name == "fp_rect"]
    assert layers == ["F.CrtYd"]  # the old rectangle is replaced, not kept beside the new one
    big = write_layer.load(c1206(tmp_path / "big.kicad_mod", courtyard=(-4, -3, 4, 3)))
    assert fc.fix(big) is None  # a bigger body that already encloses the pads is left as it is
    narrow = write_layer.load(c1206(tmp_path / "narrow.kicad_mod", courtyard=(-4, -0.5, 4, 0.5)))
    assert fc.fix(narrow) == [-4.0, -1.12, 4.0, 1.12]  # grown only where the pads reach, never smaller than the old one


# --------------------------------------------------------------------------- validate_klc


def _tools() -> dict:
    mcp = MCPServer("test")
    library_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_validate_klc_fails_a_courtyard_that_misses_a_pad(tmp_path: Path) -> None:
    result = _tools()["validate_klc"](str(c1206(tmp_path / "C_1206.kicad_mod")))
    assert result["checks"]["courtyard_encloses_pads"] is False
    assert "courtyard_encloses_pads" in result["failed_checks"]
    assert result["status"] == "violations_found"
    assert result["courtyard"]["uncovered_pads"] == ["1", "2"]
    good = _tools()["validate_klc"](str(c1206(tmp_path / "ok.kicad_mod", courtyard=(-2.6, -1.1, 2.6, 1.1))))
    assert good["checks"]["courtyard_encloses_pads"] is True


# --------------------------------------------------------------------------- adoption


def _bad_courtyard_part(tmp_path: Path, lcsc: str) -> dict:
    part = _part_assets(tmp_path, lcsc)
    fp_file = next(Path(part["footprint_dir"]).glob("*.kicad_mod"))
    root = write_layer.load(fp_file)
    for child in [c for c in root.children if not c.is_atom and c.name == "pad"]:
        child.find("at").children[0] = _n(float(child.find("at").values[0]) * 3)  # pads far outside
    write_layer.save(root, fp_file)
    return part


def test_propose_reports_the_error_and_the_fix_and_writes_nothing(project: Path, tmp_path: Path) -> None:
    part = _bad_courtyard_part(tmp_path, "C100")
    proposed = _propose(project, [part], tmp_path / "rev")
    entry = proposed["parts"][0]
    assert entry["status"] == "ready", entry
    check = entry["courtyard_check"]
    assert check["severity"] == "error" and check["encloses"] is False and check["uncovered_pads"]
    assert check["will_be_set_to"][0] < check["courtyard_box"][0]
    assert any("courtyard" in line for line in entry["normalised"])


@needs_cli
def test_confirm_writes_a_courtyard_that_encloses_every_pad(project: Path, tmp_path: Path) -> None:
    proposed = _propose(project, [_bad_courtyard_part(tmp_path, "C100"), _part_assets(tmp_path, "C200")], tmp_path / "rev")
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "adopted", result
    for part in result["parts"]:
        checks = {c["check"]: c["status"] for c in part["checks"]}
        assert checks["courtyard_encloses_pads"] == "pass"
    for name in ("R_C100", "R_C200"):
        written = write_layer.load(tmp_path / "rev" / "lib" / "project_parts.pretty" / f"{name}.kicad_mod")
        assert fc.check(written)["encloses"] is True


@needs_cli
def test_the_gate_check_fires_when_the_fix_is_broken(project: Path, tmp_path: Path, monkeypatch) -> None:
    proposed = _propose(project, [_bad_courtyard_part(tmp_path, "C100")], tmp_path / "rev")
    monkeypatch.setattr(fc, "fix", lambda root, margin=fc.MARGIN_MM: None)  # the fix silently does nothing
    result = pa.confirm_adopt_parts(project, proposed["revision_token"])
    assert result["status"] == "failed", result
    assert any(p["check"] == "courtyard_encloses_pads" for p in result["problems"])


# --------------------------------------------------------------------------- list of adopted parts


def test_list_reports_every_adopted_footprint_whose_courtyard_misses_a_pad(tmp_path: Path) -> None:
    pretty = tmp_path / "proj" / "lib" / "project_parts.pretty"
    c1206(pretty / "C_BAD.kicad_mod", "C_BAD")
    c1206(pretty / "C_OK.kicad_mod", "C_OK", courtyard=(-2.6, -1.1, 2.6, 1.1))
    before = {p.name: p.read_bytes() for p in pretty.iterdir()}
    result = fc.list_adopted(tmp_path / "proj" / "x.kicad_pcb")
    assert result["status"] == "ok"
    assert [f["footprint"] for f in result["violations"]] == ["C_BAD"]
    assert result["checked"] == 2
    assert {p.name: p.read_bytes() for p in pretty.iterdir()} == before  # read-only
    tool = _tools()["list_adopted_courtyard_errors"]
    assert tool(str(tmp_path / "proj" / "x.kicad_pcb"))["violations"][0]["footprint"] == "C_BAD"


def test_list_with_no_library_says_so(tmp_path: Path) -> None:
    result = fc.list_adopted(tmp_path / "x.kicad_pcb")
    assert result["status"] == "incomplete" and "project_parts.pretty" in result["reason"]


# --------------------------------------------------------------------------- pack, align, distribute


@pytest.fixture
def board_with_wide_pads(tmp_path: Path) -> str:
    fp = c1206(tmp_path / "C_1206.kicad_mod")
    board = PCB.create(width=100.0, height=100.0)
    board.add_footprint_from_file(fp, "C1", 10.0, 10.0)
    board.add_footprint_from_file(fp, "C2", 20.0, 10.0)
    board.add_footprint_from_file(fp, "C3", 40.0, 10.0)
    path = tmp_path / "board.kicad_pcb"
    write_layer.save_pcb(board, path)
    return str(path)


def _fns() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _raw_xs(path: str) -> dict[str, float]:
    root = write_layer.load(path)
    out = {}
    for fp in (c for c in root.children if not c.is_atom and c.name == "footprint"):
        ref = next(p for p in fp.find_all("property") if p.get_string(0) == "Reference").get_string(1)
        out[ref] = float(fp.find("at").values[0])
    return out


def test_pack_keeps_the_pads_apart_not_only_the_courtyards(board_with_wide_pads: str) -> None:
    result = _fns()["pack_footprints_densely"](board_with_wide_pads, ["C1", "C2", "C3"], min_clearance_mm=0.2)
    assert result["status"] in ("ok", "partial"), result  # partial: a pad gap of exactly 0.2 mm reads 0.1999.. in the DRC
    xs = _raw_xs(board_with_wide_pads)
    # a pitch of the pad hull (4.67) plus the clearance, never the 3.2 mm courtyard plus the clearance
    assert xs["C2"] - xs["C1"] == pytest.approx(4.67 + 0.2, abs=1e-3)
    assert xs["C3"] - xs["C2"] == pytest.approx(4.67 + 0.2, abs=1e-3)


def test_align_uses_the_hull_edge(board_with_wide_pads: str) -> None:
    path = board_with_wide_pads
    root = write_layer.load(path)
    fp = [c for c in root.children if not c.is_atom and c.name == "footprint"][1]
    pad = fp.find_all("pad")[1]
    pad.find("size").children[0] = _n(4.0)  # C2's second pad is 4 mm wide: its hull reaches 3.585 right of the origin
    write_layer.save(root, path)
    assert _fns()["align_footprints"](path, ["C1", "C2"], "x", "max")["status"] == "ok"
    xs = _raw_xs(path)
    assert xs["C1"] + 2.335 == pytest.approx(xs["C2"] + 1.585 + 2.0, abs=1e-3)


def test_distribute_spaces_the_hull_centres(board_with_wide_pads: str) -> None:
    path = board_with_wide_pads
    root = write_layer.load(path)
    fp = [c for c in root.children if not c.is_atom and c.name == "footprint"][1]
    fp.find_all("pad")[1].find("size").children[0] = _n(5.5)  # C2's hull reaches 2 mm further right: its centre is 1 mm right of the origin
    write_layer.save(root, path)
    assert _fns()["distribute_footprints"](path, ["C1", "C2", "C3"], "x")["status"] == "ok"
    xs = _raw_xs(path)
    assert (xs["C2"] + 1.0) - xs["C1"] == pytest.approx(xs["C3"] - (xs["C2"] + 1.0), abs=1e-3)
