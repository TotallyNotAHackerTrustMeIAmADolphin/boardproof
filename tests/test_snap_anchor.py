"""snap_footprints_to_grid: the anchor of the grid (issue 135).

Measured on simple_led (Edge.Cuts rect from page (136, 77.5); R1 at raw (148.5, 85.5)): the library
reports R1 as local (12.5, 8.0), the snap rounds the local value, so grid_mm 5 moved R1 to raw
(146.0, 87.5), which is not a multiple of 5 in page coordinates. That stays the default
(anchor "outline"). anchor "absolute" snaps the PAGE coordinate to the grid whose origin is the
board's user grid origin (raw (setup (grid_origin x y)), page (0, 0) when absent). Every
expectation here is a raw (at) read back with kicad_tools' S-expression parser.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import write_layer
from boardproof.toolsets import placement as placement_toolset

FIXTURE = Path(__file__).parent / "fixtures" / "simple_led.kicad_pcb"
REFS = ["J1", "R1", "D1"]


@pytest.fixture
def fns() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def board(tmp_path: Path) -> Path:
    dest = tmp_path / "simple_led.kicad_pcb"
    shutil.copy(FIXTURE, dest)
    return dest


def _raw_at(pcb: Path, ref: str) -> tuple[float, ...]:
    from kicad_tools.sexp import parse_file

    for fp in parse_file(pcb).find_all("footprint"):
        if any(t.values and t.values[0] == "reference" and t.values[1] == ref for t in fp.find_all("fp_text")):
            return tuple(float(v) for v in fp.find("at").values)
    raise AssertionError(f"{ref} not found")


def _edit(pcb: Path, mutate) -> None:
    from kicad_tools.schema import PCB

    board = PCB.load(pcb)
    mutate(board._sexp)
    write_layer.save_pcb(board, pcb)


def _set_grid_origin(pcb: Path, text: str, monkeypatch=None) -> None:
    """Add the token through the write layer. A malformed one is a file KiCad itself cannot load, so
    the layer's load check refuses it; for those cases (monkeypatch given) the check is stubbed out
    for this one save only, as test_line_breaks does."""
    from kicad_tools.sexp import parse_string

    if monkeypatch is not None:
        with monkeypatch.context() as m:
            m.setattr(write_layer, "parse_check", lambda *a, **k: [])
            _edit(pcb, lambda root: root.find("setup").append(parse_string(text)))
        return
    _edit(pcb, lambda root: root.find("setup").append(parse_string(text)))


def _drop_outline(pcb: Path) -> None:
    def mutate(root):
        for node in [n for n in root.children if n.name == "gr_rect"]:
            root.remove(node)

    _edit(pcb, mutate)


def _snap(fns: dict, board: Path, refs=None, grid=5.0, **kw) -> dict:
    return fns["snap_footprints_to_grid"].fn(pcb_path=str(board), refs=refs or REFS, grid_mm=grid, **kw)


# -- default: unchanged -----------------------------------------------------------------


def test_default_anchor_is_the_outline_corner_measured_in_raw_at(fns: dict, board: Path) -> None:
    assert _raw_at(board, "R1") == (148.5, 85.5, 0.0)
    assert _snap(fns, board)["status"] == "ok"
    # local (12.5, 8.0) -> (10.0, 10.0); page = corner (136, 77.5) + local
    assert _raw_at(board, "R1") == (146.0, 87.5, 0.0)
    assert _raw_at(board, "J1")[:2] == (141.0, 87.5)
    assert _raw_at(board, "D1")[:2] == (156.0, 87.5)


def test_default_and_explicit_outline_write_identical_bytes(fns: dict, board: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.kicad_pcb"
    shutil.copy(FIXTURE, other)
    _snap(fns, board)
    _snap(fns, other, anchor="outline")
    assert board.read_bytes() == other.read_bytes()


def test_outline_anchor_ignores_a_user_grid_origin(fns: dict, board: Path, tmp_path: Path) -> None:
    other = tmp_path / "other.kicad_pcb"
    shutil.copy(FIXTURE, other)
    _set_grid_origin(other, "(grid_origin 0.5 0.25)")
    _snap(fns, other)
    assert _raw_at(other, "R1") == (146.0, 87.5, 0.0)


# -- absolute ---------------------------------------------------------------------------


def test_absolute_anchor_puts_raw_at_on_multiples_of_the_grid(fns: dict, board: Path) -> None:
    result = _snap(fns, board, anchor="absolute")
    assert result["status"] == "ok", result
    assert _raw_at(board, "R1") == (150.0, 85.0, 0.0)
    for ref in REFS:
        x, y = _raw_at(board, ref)[:2]
        assert x % 5.0 == pytest.approx(0.0, abs=1e-6) and y % 5.0 == pytest.approx(0.0, abs=1e-6), ref
    assert result["anchor"] == "absolute"
    assert result["grid_anchor_mm"] == [0.0, 0.0]


def test_absolute_anchor_with_a_grid_that_is_not_a_divisor_of_the_corner(fns: dict, board: Path) -> None:
    _snap(fns, board, refs=["R1"], grid=1.27, anchor="absolute")
    x, y, rot = _raw_at(board, "R1")
    assert x == pytest.approx(117 * 1.27, abs=1e-6) and y == pytest.approx(67 * 1.27, abs=1e-6)
    assert rot == 0.0


def test_absolute_anchor_reports_clean_numbers_not_float_noise(fns: dict, board: Path) -> None:
    result = _snap(fns, board, grid=0.2, anchor="absolute")
    for entry in result["moved"]:
        assert entry["x"] == round(entry["x"], 6) and entry["y"] == round(entry["y"], 6), entry


def test_absolute_anchor_reports_local_positions_like_the_default(fns: dict, board: Path) -> None:
    result = _snap(fns, board, refs=["R1"], anchor="absolute")
    assert result["moved"] == [{"reference": "R1", "x": pytest.approx(14.0), "y": pytest.approx(7.5)}]
    assert result["origin_mm"] == [136.0, 77.5]


def test_absolute_anchor_honours_the_boards_user_grid_origin(fns: dict, board: Path) -> None:
    _set_grid_origin(board, "(grid_origin 0.5 0.25)")
    result = _snap(fns, board, refs=["R1"], anchor="absolute")
    assert result["status"] == "ok", result
    # x: 0.5 + round((148.5 - 0.5) / 5) * 5 = 150.5; y: 0.25 + round((85.5 - 0.25) / 5) * 5 = 85.25
    assert _raw_at(board, "R1") == (150.5, 85.25, 0.0)
    assert result["grid_anchor_mm"] == [0.5, 0.25]


def test_each_axis_rounds_against_its_own_grid_origin_y(fns: dict, board: Path) -> None:
    _set_grid_origin(board, "(grid_origin 0 3.5)")
    _snap(fns, board, refs=["R1"], anchor="absolute")
    # y: 3.5 + round((85.5 - 3.5) / 5) * 5 = 83.5; reading the x origin 0 anywhere in the term
    # gives 85.0 or 88.5
    assert _raw_at(board, "R1") == (150.0, 83.5, 0.0)


def test_each_axis_rounds_against_its_own_grid_origin_x(fns: dict, board: Path) -> None:
    _set_grid_origin(board, "(grid_origin 2.5 0)")
    _snap(fns, board, refs=["R1"], anchor="absolute")
    # x: 2.5 + round((148.5 - 2.5) / 5) * 5 = 147.5 (an x that read the y origin 0 would give 150.0)
    assert _raw_at(board, "R1") == (147.5, 85.0, 0.0)


def test_absolute_anchor_with_a_rotated_and_a_flipped_footprint(fns: dict, board: Path) -> None:
    def mutate(root):
        for fp in root.find_all("footprint"):
            ref = next(t.values[1] for t in fp.find_all("fp_text") if t.values[0] == "reference")
            if ref == "R1":
                write_layer.set_number(fp.find("at"), 2, 90)
            elif ref == "D1":
                write_layer.set_string(fp.find("layer"), 0, "B.Cu")

    _edit(board, mutate)
    assert _raw_at(board, "R1") == (148.5, 85.5, 90.0)
    assert _snap(fns, board, refs=["R1", "D1"], grid=2.0, anchor="absolute")["status"] == "ok"
    assert _raw_at(board, "R1") == (148.0, 86.0, 90.0)
    assert _raw_at(board, "D1")[:2] == (156.0, 88.0)


def test_absolute_anchor_without_an_outline_uses_page_coordinates(fns: dict, board: Path) -> None:
    _drop_outline(board)
    before = _raw_at(board, "R1")
    assert before[:2] == (148.5, 85.5)
    assert _snap(fns, board, refs=["R1"], anchor="absolute")["status"] == "ok"
    assert _raw_at(board, "R1") == (150.0, 85.0, 0.0)


def test_absolute_anchor_leaves_unlisted_footprints_alone(fns: dict, board: Path) -> None:
    _snap(fns, board, refs=["R1"], anchor="absolute")
    assert _raw_at(board, "J1")[:2] == (141.0, 87.5)
    assert _raw_at(board, "D1")[:2] == (156.0, 87.5)


# -- refusals write nothing -------------------------------------------------------------


@pytest.mark.parametrize("bad", ["Absolute", "", "grid", "page", None, 5, True, ["absolute"]])
def test_a_bad_anchor_is_refused_and_nothing_is_written(fns: dict, board: Path, bad) -> None:
    before = board.read_bytes()
    result = _snap(fns, board, anchor=bad)
    assert result["status"] == "error"
    assert "anchor" in result["reason"] and '"outline"' in result["reason"] and '"absolute"' in result["reason"]
    assert board.read_bytes() == before


@pytest.mark.parametrize(
    "text",
    ["(grid_origin 1)", "(grid_origin a b)", "(grid_origin 1 2 3)", "(grid_origin)", "(grid_origin nan 1)"],
)
def test_absolute_refuses_a_malformed_grid_origin_and_writes_nothing(
    fns: dict, board: Path, text: str, monkeypatch
) -> None:
    _set_grid_origin(board, text, monkeypatch)
    before = board.read_bytes()
    result = _snap(fns, board, anchor="absolute")
    assert result["status"] == "error"
    assert "grid_origin" in result["reason"]
    assert board.read_bytes() == before


def test_outline_never_reads_the_grid_origin(fns: dict, board: Path, monkeypatch) -> None:
    # a malformed grid_origin is a file KiCad cannot load, so the outline snap could not save it
    # anyway; what is pinned is that the default path does not consult the token at all.
    def boom(_path):
        raise AssertionError("the outline anchor must not read grid_origin")

    monkeypatch.setattr(placement_toolset, "_read_grid_origin", boom)
    assert _snap(fns, board)["status"] == "ok"
    assert _raw_at(board, "R1") == (146.0, 87.5, 0.0)


def test_absolute_still_refuses_an_unknown_reference_and_writes_nothing(fns: dict, board: Path) -> None:
    before = board.read_bytes()
    result = _snap(fns, board, refs=["R1", "NOPE"], anchor="absolute")
    assert result["status"] == "error" and "NOPE" in result["reason"]
    assert board.read_bytes() == before


def test_absolute_still_refuses_a_nonpositive_grid(fns: dict, board: Path) -> None:
    before = board.read_bytes()
    assert _snap(fns, board, grid=0.0, anchor="absolute")["status"] == "error"
    assert board.read_bytes() == before


# -- tool text --------------------------------------------------------------------------


def test_the_description_names_both_anchors_and_the_default(fns: dict) -> None:
    text = " ".join(fns["snap_footprints_to_grid"].description.split())
    assert "anchored at the outline corner" in text
    assert 'anchor="absolute"' in text and "grid_origin" in text
    assert "the grid origin KiCad stores in the board" in text
    assert "the editor's grid instead" not in text
    assert 'default "outline"' in text


def test_the_schema_has_an_optional_anchor_with_default_outline(fns: dict) -> None:
    schema = fns["snap_footprints_to_grid"].parameters
    assert "anchor" not in schema.get("required", [])
    assert schema["properties"]["anchor"]["default"] == "outline"
