"""Edge placement and autoplacement on a rounded-rectangle outline, and place_footprints refusing a
courtyard inside a slot (GitHub issue 206).

Fixture: the golden board (outline (123.5, 85) to (173.5, 125)). A rounded outline is built through
``propose_board_outline`` / ``confirm_board_outline`` (the project's own tool); variants that the
tool cannot make (unequal radii, a stadium, a corner bulging inward) are made through the write
layer's tree API on a ``tmp_path`` copy. Every geometry fact is read from the raw ``(start)``,
``(mid)`` and ``(end)`` tokens or the raw ``(at)`` of the saved file, never from a ``kicad_tools``
convenience property.
"""

from __future__ import annotations

import math
import shutil
import types
import uuid
from pathlib import Path

import pytest
from kicad_tools.sexp import SExp
from mcp.server.mcpserver import MCPServer

from boardproof import board_outline, kicad_cli, placement_anchor, write_layer
from boardproof.toolsets import placement as placement_toolset
from tests.test_isolation import GOLDEN_DIR, GOLDEN_NAME, SQUARE, apply
from tests.test_board_outline import _no_outline_drc
from tests._fixture_copy import copy_fixture

X0, Y0, W, H = 123.5, 85.0, 50.0, 40.0
BOX = (X0, Y0, X0 + W, Y0 + H)


@pytest.fixture
def tools() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _golden(tmp_path: Path, name: str = "proj") -> Path:
    copy_fixture(GOLDEN_DIR, tmp_path / name, ignore=shutil.ignore_patterns("~*.lck"))  # a parallel worker may hold a KiCad lock in the fixture
    return tmp_path / name / GOLDEN_NAME


def _rounded(tmp_path: Path, radius: float, name: str = "proj") -> Path:
    """The golden board with its outline replaced by the project's rounded rectangle."""
    pcb = _golden(tmp_path, name)
    outline = {"x": X0, "y": Y0, "width": W, "height": H, "corner_radius": radius}
    proposal = board_outline.propose_board_outline(pcb, outline, output_dir=tmp_path / f"out-{name}")
    assert proposal["status"] == "proposed", proposal
    done = board_outline.confirm_board_outline(pcb, proposal["revision_token"], drc=_no_outline_drc)
    assert done["status"] == "built", done
    return Path(done["output_pcb"])


def _stroke() -> SExp:
    return SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default"))


def _line(a, b) -> SExp:
    return SExp.list("gr_line", SExp.list("start", *a), SExp.list("end", *b), _stroke(),
                     SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", str(uuid.uuid4())))


def _arc(a, m, b) -> SExp:
    return SExp.list("gr_arc", SExp.list("start", *a), SExp.list("mid", *m), SExp.list("end", *b), _stroke(),
                     SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", str(uuid.uuid4())))


def _replace_outline(pcb: Path, nodes: list[SExp]) -> Path:
    root = write_layer.load(pcb)
    for node in list(root.children):
        if not node.is_atom and node.name.startswith("gr_") and node.find("layer") is not None \
                and node.find("layer").values[0] == "Edge.Cuts":
            root.children.remove(node)
    for node in nodes:
        root.append(node)
    write_layer.save(root, pcb)
    return pcb


def _unequal(tmp_path: Path, radii: dict[str, float], name: str = "unequal") -> Path:
    """Axis-aligned rectangle, corners tl/tr/br/bl rounded with their own radius (0: sharp)."""
    x0, y0, x1, y1 = BOX
    r = {k: radii.get(k, 0.0) for k in ("tl", "tr", "br", "bl")}

    def d(c: float) -> float:  # the mid point's offset from the sharp corner
        return c * (1 - math.sqrt(0.5))

    nodes = [
        _line((x0 + r["tl"], y0), (x1 - r["tr"], y0)),
        _line((x1, y0 + r["tr"]), (x1, y1 - r["br"])),
        _line((x1 - r["br"], y1), (x0 + r["bl"], y1)),
        _line((x0, y1 - r["bl"]), (x0, y0 + r["tl"])),
    ]
    if r["tr"]:
        nodes.append(_arc((x1 - r["tr"], y0), (x1 - d(r["tr"]), y0 + d(r["tr"])), (x1, y0 + r["tr"])))
    if r["br"]:
        nodes.append(_arc((x1, y1 - r["br"]), (x1 - d(r["br"]), y1 - d(r["br"])), (x1 - r["br"], y1)))
    if r["bl"]:
        nodes.append(_arc((x0 + r["bl"], y1), (x0 + d(r["bl"]), y1 - d(r["bl"])), (x0, y1 - r["bl"])))
    if r["tl"]:
        nodes.append(_arc((x0, y0 + r["tl"]), (x0 + d(r["tl"]), y0 + d(r["tl"])), (x0 + r["tl"], y0)))
    return _replace_outline(_golden(tmp_path, name), [n for n in nodes if n is not None])


def _covered(point, outline) -> bool:
    return any(c[0] < point[0] < c[2] and c[1] < point[1] < c[3] for c in outline.cutouts)


def _centres(radii: dict[str, float]):
    x0, y0, x1, y1 = BOX
    return {"tl": (x0 + radii.get("tl", 0), y0 + radii.get("tl", 0), -1, -1),
            "tr": (x1 - radii.get("tr", 0), y0 + radii.get("tr", 0), 1, -1),
            "br": (x1 - radii.get("br", 0), y1 - radii.get("br", 0), 1, 1),
            "bl": (x0 + radii.get("bl", 0), y1 - radii.get("bl", 0), -1, 1)}


# --------------------------------------------------------------------------- outline_rect


@pytest.mark.parametrize("radius", [3.0, 8.0, 20.0])  # 20 = half the shorter side: the short sides have no line
def test_a_rounded_rectangle_is_the_bounding_box_plus_four_corner_cutouts(tmp_path, radius):
    pcb = _rounded(tmp_path, radius)
    outline, problem = placement_anchor.outline_rect(write_layer.load(pcb))
    assert problem is None and outline is not None, problem
    assert outline.box == pytest.approx(BOX, abs=1e-6)
    for name, (cx, cy, sx, sy) in _centres({k: radius for k in ("tl", "tr", "br", "bl")}).items():
        # a point 0.1 mm inside the board corner is material that is missing: covered
        corner = (cx + sx * radius, cy + sy * radius)
        assert _covered((corner[0] - sx * 0.1, corner[1] - sy * 0.1), outline), name
        # points 0.5 mm inside the arc (and the middle of the straight sides) are board: not covered
        for deg in (5, 25, 45, 65, 85):
            u, v = (radius - 0.5) * math.cos(math.radians(deg)), (radius - 0.5) * math.sin(math.radians(deg))
            if radius > 0.5:
                assert not _covered((cx + sx * u, cy + sy * v), outline), (name, deg)
        # points 0.3 mm outside the arc, still inside the corner square: covered
        for deg in (10, 45, 80):
            u, v = (radius + 0.3) * math.cos(math.radians(deg)), (radius + 0.3) * math.sin(math.radians(deg))
            if u < radius and v < radius:
                assert _covered((cx + sx * u, cy + sy * v), outline), (name, deg)
    # every cut-out lies inside its corner square: nothing is taken from the straight sides
    for cut in outline.cutouts:
        near = [(cut[0] - BOX[0]) < radius - 1e-6 or (BOX[2] - cut[2]) < radius - 1e-6]
        assert near[0]
    assert not _covered(((BOX[0] + BOX[2]) / 2, BOX[1] + 0.2), outline)


def test_unequal_radii_and_a_sharp_corner_are_read(tmp_path):
    radii = {"tl": 10.0, "tr": 2.0, "br": 6.0}  # bottom-left stays sharp
    outline, problem = placement_anchor.outline_rect(write_layer.load(_unequal(tmp_path, radii)))
    assert problem is None and outline.box == pytest.approx(BOX, abs=1e-6)
    for name, (cx, cy, sx, sy) in _centres(radii).items():
        r = radii.get(name, 0.0)
        if r:
            assert _covered((cx + sx * r * 0.99, cy + sy * r * 0.99), outline), name
    # the sharp bottom-left corner holds no cut-out at all
    assert not any(c[0] < BOX[0] + 1.0 and c[3] > BOX[3] - 1.0 for c in outline.cutouts)


def test_a_rounded_rectangle_with_a_slot_reports_both(tmp_path):
    pcb = _rounded(tmp_path, 6.0)
    slotted = Path(apply(pcb, tmp_path, slots=[SQUARE])["output_pcb"])
    outline, problem = placement_anchor.outline_rect(write_layer.load(slotted))
    assert problem is None and outline.box == pytest.approx(BOX, abs=1e-6)
    assert any(c[0] >= 168.0 and c[2] <= 170.0 and c[1] >= 99.0 for c in outline.cutouts)  # the slot
    assert any(c[2] <= BOX[0] + 6.0 + 1e-6 for c in outline.cutouts)  # a corner


def test_a_stadium_is_still_not_a_rectangle(tmp_path):
    x0, y0, x1, y1 = BOX
    r = H / 2
    nodes = [_line((x0 + r, y0), (x1 - r, y0)), _line((x1 - r, y1), (x0 + r, y1)),
             _arc((x1 - r, y0), (x1, y0 + r), (x1 - r, y1)), _arc((x0 + r, y1), (x0, y0 + r), (x0 + r, y0))]
    outline, problem = placement_anchor.outline_rect(write_layer.load(_replace_outline(_golden(tmp_path), nodes)))
    assert outline is None and problem is not None


def test_a_corner_that_bulges_inward_is_not_a_rectangle(tmp_path):
    x0, y0, x1, y1 = BOX
    c = 6.0
    # a concave arc at the top-left corner: its mid point lies on the board side of the chord
    nodes = [_line((x0 + c, y0), (x1, y0)), _line((x1, y0), (x1, y1)), _line((x1, y1), (x0, y1)),
             _line((x0, y1), (x0, y0 + c)), _arc((x0, y0 + c), (x0 + c * 0.2, y0 + c * 0.2), (x0 + c, y0))]
    outline, problem = placement_anchor.outline_rect(write_layer.load(_replace_outline(_golden(tmp_path), nodes)))
    assert outline is None and problem is not None


def test_a_three_quarter_arc_is_not_a_corner(tmp_path):
    x0, y0, x1, y1 = BOX
    c = 6.0
    far = (x0 + c - c * math.sqrt(0.5), y0 + c + c * math.sqrt(0.5))  # the long way round the corner centre
    nodes = [_line((x0 + c, y0), (x1, y0)), _line((x1, y0), (x1, y1)), _line((x1, y1), (x0, y1)),
             _line((x0, y1), (x0, y0 + c)), _arc((x0, y0 + c), far, (x0 + c, y0))]
    outline, problem = placement_anchor.outline_rect(write_layer.load(_replace_outline(_golden(tmp_path), nodes)))
    assert outline is None and problem is not None


def test_a_radius_over_half_the_shorter_side_is_refused(tmp_path):
    # corner arcs of radius 24 on a 40 mm high board overlap each other's sides: not a rounded rectangle
    x0, y0, x1, y1 = BOX
    c = 24.0
    d = c * (1 - math.sqrt(0.5))
    nodes = [_arc((x0, y0 + c), (x0 + d, y0 + d), (x0 + c, y0)), _line((x0 + c, y0), (x1, y0)),
             _line((x1, y0), (x1, y1)), _line((x1, y1), (x0, y1)), _line((x0, y1), (x0, y0 + c))]
    outline, problem = placement_anchor.outline_rect(write_layer.load(_replace_outline(_golden(tmp_path), nodes)))
    assert outline is None and problem is not None


# --------------------------------------------------------------------------- the tools


def test_edge_placement_in_a_rounded_corner_is_refused_and_writes_nothing(tmp_path, tools):
    pcb = _rounded(tmp_path, 10.0)
    before = pcb.read_bytes()
    refused = tools["place_footprints_at_edge"](
        str(pcb), [{"reference": "R1", "edge": "top", "offset_mm": 0.0, "along_mm": 2.5}])
    assert refused["status"] == "error", refused
    assert any(p["code"] == "in_cutout" for p in refused.get("problems", refused.get("errors", []))) or refused.get("code") == "in_cutout", refused
    assert pcb.read_bytes() == before
    ok = tools["place_footprints_at_edge"](
        str(pcb), [{"reference": "R1", "edge": "top", "offset_mm": 0.0, "along_mm": 25.0}])
    assert ok["status"] == "ok", ok


def test_edge_placement_still_works_on_a_rounded_rectangle_away_from_the_corners(tmp_path, tools):
    pcb = _rounded(tmp_path, 4.0)
    result = tools["place_footprints_at_edge"](
        str(pcb), [{"reference": "R1", "edge": "right", "offset_mm": 1.0}, {"reference": "C1", "edge": "left", "offset_mm": 1.0}])
    assert result["status"] == "ok", result


def test_autoplacement_keeps_parts_out_of_the_rounded_corners(tmp_path, tools):
    refs = ["C1", "R1", "D1"]

    def boxes(board: Path) -> tuple:
        proposal = tools["propose_autoplacement"](str(board), refs)
        assert proposal["status"] == "pending_confirmation", proposal
        root = write_layer.load(board)
        outline, _ = placement_anchor.outline_rect(root)
        parts = {p.reference: p for p in placement_anchor.read_parts(root)}
        placed = {
            ref: placement_anchor.shift(parts[ref].box, pos["x"] + outline.box[0] - parts[ref].at[0],
                                        pos["y"] + outline.box[1] - parts[ref].at[1])
            for ref, pos in proposal["proposed_positions"].items()
        }
        return proposal, placed, outline

    plain = _golden(tmp_path, "plain")
    rounded = _rounded(tmp_path, 12.0)
    _, _, outline = boxes(rounded)
    _, plain_boxes, _ = boxes(plain)
    assert any(placement_anchor.overlap(b, cut) for b in plain_boxes.values() for cut in outline.cutouts)
    proposal, rounded_boxes, outline = boxes(rounded)
    for ref, box in rounded_boxes.items():
        assert placement_anchor.inside(box, outline.box), ref
        assert not any(placement_anchor.overlap(box, cut) for cut in outline.cutouts), ref
    assert tools["confirm_autoplacement"](proposal["revision_token"])["status"] == "confirmed"


def test_confirm_autoplacement_refuses_a_part_a_rounded_corner_now_covers(tmp_path, tools):
    pcb = _golden(tmp_path)
    proposal = tools["propose_autoplacement"](str(pcb), ["C1"])
    assert proposal["status"] == "pending_confirmation", proposal
    rounded = _rounded(tmp_path, 14.0, name="later")
    # the board changes under the token: copy the rounded outline's edge items onto the proposed board
    root, source = write_layer.load(pcb), write_layer.load(rounded)
    for node in list(root.children):
        if not node.is_atom and node.name.startswith("gr_") and node.find("layer") is not None \
                and node.find("layer").values[0] == "Edge.Cuts":
            root.children.remove(node)
    for node in source.children:
        if not node.is_atom and node.name.startswith("gr_") and node.find("layer") is not None \
                and node.find("layer").values[0] == "Edge.Cuts":
            root.append(node)
    write_layer.save(root, pcb)
    out = tools["confirm_autoplacement"](proposal["revision_token"])
    assert out["status"] == "error" and "slot of the outline" in out["reason"], out


def test_the_circle_only_outline_stays_refused(tmp_path, tools):
    pcb = _replace_outline(_golden(tmp_path), [SExp.list(
        "gr_circle", SExp.list("center", 148.5, 105), SExp.list("end", 158.5, 105), _stroke(),
        SExp.list("fill", write_layer.symbol("no")), SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", str(uuid.uuid4())))])
    outline, problem = placement_anchor.outline_rect(write_layer.load(pcb))
    assert outline is None and problem
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "R1", "edge": "left"}])
    assert result["status"] == "error" and result["code"] == "outline_not_rectangular", result


# --------------------------------------------------------------------------- place_footprints and slots


@pytest.fixture
def slotted(tmp_path) -> Path:
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    return Path(apply(_golden(tmp_path), tmp_path, slots=[SQUARE])["output_pcb"])


def _at(pcb: Path, ref: str):
    for fp in write_layer.load(pcb).children:
        if not fp.is_atom and fp.name == "footprint" and any(
                not c.is_atom and c.name == "property" and c.values[:2] == ["Reference", ref] for c in fp.children):
            return [float(v) for v in fp.find("at").values]
    raise AssertionError(ref)


def test_place_footprints_refuses_a_courtyard_in_a_slot_and_writes_nothing(tmp_path, tools, slotted):
    before = slotted.read_bytes()
    # SQUARE is the page box (168.5, 100, 169.5, 110): local x 45, y 15 .. 25
    result = tools["place_footprints"](str(slotted), [
        {"reference": "R1", "x": 45.0, "y": 20.0},   # on the slot
        {"reference": "C1", "x": 10.0, "y": 20.0},   # a fine move: all or nothing
    ])
    assert result["status"] == "error" and result["code"] == "in_cutout", result
    assert [e["reference"] for e in result["errors"]] == ["R1"], result
    assert slotted.read_bytes() == before


def test_place_footprints_moves_a_part_beside_the_slot(tmp_path, tools, slotted):
    result = tools["place_footprints"](str(slotted), [{"reference": "R1", "x": 30.0, "y": 20.0}])
    assert result["status"] == "ok" and result["moved"] == ["R1"], result
    assert _at(slotted, "R1")[:2] == pytest.approx([X0 + 30.0, Y0 + 20.0], abs=1e-6)


def test_place_footprints_uses_the_rotated_courtyard(tmp_path, tools, slotted):
    part = next(p for p in placement_anchor.read_parts(write_layer.load(slotted)) if p.reference == "R1")
    w, h = part.box[2] - part.box[0], part.box[3] - part.box[1]
    assert abs(w - h) > 0.5, (w, h)
    wide, tall = (w, h) if w > h else (h, w)
    # park the centre so the part clears the slot (x 168.5..169.5) only while it is the narrow way round
    cx = 168.5 - 0.1 - tall / 2
    spec = {"reference": "R1", "x": cx - X0, "y": 105.0 - Y0}
    flat, upright = (0.0, 90.0) if w > h else (90.0, 0.0)
    flat_result = tools["place_footprints"](str(slotted), [{**spec, "rotation": flat}])
    upright_result = tools["place_footprints"](str(slotted), [{**spec, "rotation": upright}])
    outcomes = sorted([flat_result["status"], upright_result["status"]])
    assert outcomes == ["error", "ok"], (flat_result, upright_result, wide)
    refused = flat_result if flat_result["status"] == "error" else upright_result
    assert refused["code"] == "in_cutout", refused


def test_place_footprints_keeps_the_locked_refusal_and_a_locked_part_is_not_a_cutout_error(tmp_path, tools, slotted):
    assert tools["lock_footprints"](str(slotted), ["R1"])["status"] == "ok"
    before = slotted.read_bytes()
    result = tools["place_footprints"](str(slotted), [{"reference": "R1", "x": 45.0, "y": 20.0}])
    assert result["status"] == "error" and result["errors"][0]["code"] == "locked_footprint", result
    assert slotted.read_bytes() == before


def test_place_footprints_on_a_board_without_slots_is_unchanged(tmp_path, tools):
    pcb = _golden(tmp_path)
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 45.0, "y": 20.0}])
    assert result["status"] == "ok" and result["moved"] == ["R1"], result


def test_place_footprints_refuses_a_courtyard_in_a_rounded_corner(tmp_path, tools):
    pcb = _rounded(tmp_path, 10.0)
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 1.5, "y": 1.5}])  # the very corner
    assert result["status"] == "error" and result["code"] == "in_cutout", result
    assert pcb.read_bytes() == before
    assert tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 25.0, "y": 20.0}])["status"] == "ok"


def test_box_at_matches_the_raw_box_and_follows_a_rotation(tmp_path):
    root = write_layer.load(_golden(tmp_path))
    part = next(p for p in placement_anchor.read_parts(root) if p.reference == "R1")
    assert placement_anchor.box_at(part, part.at, part.angle) == pytest.approx(part.box, abs=1e-9)
    turned = placement_anchor.box_at(part, part.at, part.angle + 90.0)
    cx, cy = (part.box[0] + part.box[2]) / 2, (part.box[1] + part.box[3]) / 2  # symmetric courtyard
    w, h = part.box[2] - part.box[0], part.box[3] - part.box[1]
    assert turned == pytest.approx((cx - h / 2, cy - w / 2, cx + h / 2, cy + w / 2), abs=1e-6)


def test_box_at_of_a_part_without_courtyard_turns_its_pads(tmp_path):
    root = write_layer.load(_golden(tmp_path))
    node = next(p.node for p in placement_anchor.read_parts(root) if p.reference == "R1")
    for child in list(node.children):
        if not child.is_atom and (child.name or "").startswith("fp_") and child.find("layer") is not None \
                and child.find("layer").values[0] in ("F.CrtYd", "B.CrtYd"):
            node.children.remove(child)
    part = next(p for p in placement_anchor.read_parts(root) if p.reference == "R1")
    assert part.box_source == "pads"
    assert placement_anchor.box_at(part, part.at, part.angle) == pytest.approx(part.box, abs=1e-9)
    turned = placement_anchor.box_at(part, part.at, part.angle + 90.0)
    w, h = part.box[2] - part.box[0], part.box[3] - part.box[1]
    assert (turned[2] - turned[0], turned[3] - turned[1]) == pytest.approx((h, w), abs=1e-6)
    moved = placement_anchor.box_at(part, (part.at[0] + 3.0, part.at[1] - 2.0), part.angle)
    assert moved == pytest.approx(placement_anchor.shift(part.box, 3.0, -2.0), abs=1e-9)


# --------------------------------------------------------------------------- text an LLM reads (pinned)


def _descriptions() -> dict[str, str]:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t.description for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def test_the_tool_descriptions_state_the_rounded_corners_and_the_slot_refusal():
    d = _descriptions()
    assert "its corners optionally rounded by quarter arcs" in d["place_footprints_at_edge"]
    assert "overlap a slot or a rounded corner (in_cutout)" in d["place_footprints_at_edge"]
    assert "its corners optionally rounded by quarter arcs, else outline_not_rectangular; slots and rounded corners are obstacles" in d["propose_autoplacement"]
    assert "overlap a slot or a rounded corner of the outline refuses the whole call as in_cutout, nothing written" in d["place_footprints"]


def test_the_docstrings_state_the_same():
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    doc = {t.name: (t.fn.__doc__ or "") for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert "each corner optionally replaced by a quarter-circle gr_arc" in doc["place_footprints_at_edge"]
    assert "would overlap a slot or a rounded corner of the outline is refused as in_cutout" in " ".join(doc["place_footprints"].split())
    assert "rounded corners and slots are obstacles" in " ".join(doc["propose_autoplacement"].split())


def test_the_kicad_pcb_skill_states_the_rounded_corners_and_the_slot_refusal():
    skill = (Path(__file__).parents[1] / ".claude" / "skills" / "kicad-pcb" / "SKILL.md").read_text(encoding="utf-8")
    assert "its corners may be rounded by quarter arcs; rounded corners and slots are obstacles" in skill
    assert "`place_footprints` refuses a move of an existing footprint into one for the whole call" in skill


# --------------------------------------------------------------------------- review round 1


def _strip_measure(pcb: Path, ref: str) -> None:
    """Make ``ref`` a graphic-only footprint: no pad and no courtyard item (write layer tree edit)."""
    root = write_layer.load(pcb)
    node = next(n for n in root.children if not n.is_atom and n.name == "footprint" and any(
        not c.is_atom and c.name == "property" and c.values[:2] == ["Reference", ref] for c in n.children))
    for child in list(node.children):
        if child.is_atom:
            continue
        crtyd = (child.name or "").startswith("fp_") and child.find("layer") is not None \
            and child.find("layer").values[0] in ("F.CrtYd", "B.CrtYd")
        if child.name == "pad" or crtyd:
            node.children.remove(child)
    write_layer.save(root, pcb)


def test_a_graphic_only_footprint_elsewhere_does_not_block_place_footprints(tmp_path, tools):
    pcb = _rounded(tmp_path, 3.0)
    _strip_measure(pcb, "U1")
    ok = tools["place_footprints"](str(pcb), [{"reference": "C1", "x": 20.0, "y": 20.0}])
    assert ok["status"] == "ok" and ok["moved"] == ["C1"], ok
    before = pcb.read_bytes()
    corner = tools["place_footprints"](str(pcb), [{"reference": "C1", "x": 0.5, "y": 0.5}])
    assert corner["status"] == "error" and corner["code"] == "in_cutout", corner
    assert pcb.read_bytes() == before


def test_a_named_footprint_that_cannot_be_measured_is_refused_with_its_own_code(tmp_path, tools):
    pcb = _rounded(tmp_path, 3.0)
    _strip_measure(pcb, "U1")
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "C1", "x": 20.0, "y": 20.0},
                                                  {"reference": "U1", "x": 25.0, "y": 20.0}])
    assert result["status"] == "error" and result["code"] == "footprint_unmeasurable", result
    assert [e["reference"] for e in result["errors"]] == ["U1"], result
    assert pcb.read_bytes() == before


def test_unreadable_slots_refuse_and_write_nothing(tmp_path, tools, monkeypatch):
    pcb = _rounded(tmp_path, 3.0)
    before = pcb.read_bytes()

    def broken(root):
        raise RuntimeError("synthetic")

    monkeypatch.setattr(placement_anchor, "cutout_boxes", broken)
    result = tools["place_footprints"](str(pcb), [{"reference": "C1", "x": 20.0, "y": 20.0}])
    assert result["status"] == "error" and result["code"] == "slots_unreadable", result
    assert pcb.read_bytes() == before


def test_an_arc_slightly_off_a_quarter_circle_refuses_place_footprints(tmp_path, tools):
    pcb = _rounded(tmp_path, 6.0)
    root = write_layer.load(pcb)
    arc = next(n for n in root.children if not n.is_atom and n.name == "gr_arc")
    mid = arc.find("mid")
    write_layer.set_number(mid, 0, float(mid.values[0]) + 0.05)
    write_layer.save(root, pcb)
    assert placement_anchor.outline_rect(write_layer.load(pcb))[0] is None
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "C1", "x": 20.0, "y": 20.0}])
    assert result["status"] == "error" and result["code"] == "outline_arcs_unreadable", result
    assert pcb.read_bytes() == before


def test_a_circle_or_stadium_outline_still_takes_place_footprints(tmp_path, tools):
    x0, y0, x1, y1 = BOX
    r = H / 2
    stadium = [_line((x0 + r, y0), (x1 - r, y0)), _line((x1 - r, y1), (x0 + r, y1)),
               _arc((x1 - r, y0), (x1, y0 + r), (x1 - r, y1)), _arc((x0 + r, y1), (x0, y0 + r), (x0 + r, y0))]
    pcb = _replace_outline(_golden(tmp_path), stadium)
    assert tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 25.0, "y": 20.0}])["status"] == "ok"


def test_the_description_says_an_overlapping_footprint_is_refused_for_a_move_that_still_overlaps():
    sentence = "a footprint that already overlaps a cut-out is refused for any move whose new courtyard still overlaps one"
    assert sentence in _descriptions()["place_footprints"]
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    doc = {t.name: " ".join((t.fn.__doc__ or "").split()) for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    assert sentence in doc["place_footprints"]


def test_place_footprints_refuses_a_move_that_still_overlaps_a_cutout_from_an_overlapping_start(tmp_path, tools):
    pcb = _golden(tmp_path, "start")
    # R1 first stands where the corner will be cut away (no cut-out yet, so the move is fine) ...
    assert tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 2.5, "y": 1.5}])["status"] == "ok"
    outline = {"x": X0, "y": Y0, "width": W, "height": H, "corner_radius": 10.0}
    proposal = board_outline.propose_board_outline(pcb, outline, output_dir=tmp_path / "out-start")
    assert proposal["status"] == "proposed", proposal
    done = board_outline.confirm_board_outline(pcb, proposal["revision_token"], drc=_no_outline_drc)
    assert done["status"] == "built", done
    rounded = Path(done["output_pcb"])
    root = write_layer.load(rounded)
    r1 = placement_anchor.read_parts(root, {"R1"})[0]
    x0, y0, x1, y1 = r1.box
    cuts = placement_anchor.cutout_boxes(root)
    # ... so it starts from an overlap
    assert any(x0 < c[2] and c[0] < x1 and y0 < c[3] and c[1] < y1 for c in cuts), (r1.box, cuts[:3])
    # a move that stays in the corner area is refused even though no move out of it is needed
    result = tools["place_footprints"](str(rounded), [{"reference": "R1", "x": 2.6, "y": 1.6}])
    assert result["status"] == "error" and result["code"] == "in_cutout", result


# --------------------------------------------------------------------------- review round 2


def _notched_top(tmp_path: Path, name: str) -> Path:
    """Rectangle whose top side dips inward by an arc in the middle: an arc far from every corner."""
    x0, y0, x1, y1 = BOX
    xm = (x0 + x1) / 2
    nodes = [_line((x0, y0), (xm - 3, y0)), _arc((xm - 3, y0), (xm, y0 + 2), (xm + 3, y0)), _line((xm + 3, y0), (x1, y0)),
             _line((x1, y0), (x1, y1)), _line((x1, y1), (x0, y1)), _line((x0, y1), (x0, y0))]
    return _replace_outline(_golden(tmp_path, name), nodes)


def test_an_arc_far_from_every_corner_is_refused_as_unreadable(tmp_path, tools):
    """Issue 214: an arc in the middle of a side means the outline is not a rectangle and material may be
    missing anywhere; fail closed (issue 206 had let this board through)."""
    pcb = _notched_top(tmp_path, "notch")
    root = write_layer.load(pcb)
    with pytest.raises(placement_anchor.RoundedCornerUnreadable):
        placement_anchor.cutout_boxes(root)
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 25.0, "y": 20.0}])
    assert result["status"] == "error" and result["code"] == "outline_arcs_unreadable", result
    assert "several arcs" in result["reason"] and pcb.read_bytes() == before


def test_the_unreadable_test_does_not_depend_on_where_the_arc_sits(tmp_path):
    """Issue 214: a small arc 0.4 mm and one 0.6 mm from the loop's box corner are both unreadable."""
    x0, y0, x1, y1 = BOX

    def outline_with_corner_arc(gap: float) -> tuple:
        nodes = [_line((x0 + 6, y0), (x1, y0)), _line((x1, y0), (x1, y1)), _line((x1, y1), (x0, y1)),
                 _line((x0, y1), (x0, y0 + 6)), _line((x0, y0 + 6), (x0 + gap, y0 + 6)),
                 _arc((x0 + gap, y0 + 6), (x0 + gap + 1.0, y0 + gap + 1.0), (x0 + 6, y0 + gap)),
                 _line((x0 + 6, y0 + gap), (x0 + 6, y0))]
        root = write_layer.load(_replace_outline(_golden(tmp_path, f"gap{int(gap * 10)}"), nodes))
        return root, placement_anchor.edge_loops.classify(root)

    for gap in (0.4, 0.6):
        root, parts = outline_with_corner_arc(gap)
        assert parts.outer is not None and not parts.problems
        with pytest.raises(placement_anchor.RoundedCornerUnreadable):
            placement_anchor.cutout_boxes(root)


def test_a_corner_split_into_two_arcs_is_refused_by_every_placement_tool(tmp_path, tools):
    """Issue 214: a split corner is not read, so the outline fails closed (it was a documented known limit)."""
    x0, y0, x1, y1 = BOX
    r = 6.0
    cx, cy = x0 + r, y0 + r

    def at(deg: float) -> tuple[float, float]:
        return (cx + r * math.cos(math.radians(deg)), cy + r * math.sin(math.radians(deg)))

    nodes = [_line((x0 + r, y0), (x1, y0)), _line((x1, y0), (x1, y1)), _line((x1, y1), (x0, y1)), _line((x0, y1), (x0, y0 + r)),
             _arc(at(180), at(202.5), at(225)), _arc(at(225), at(247.5), at(270))]
    pcb = _replace_outline(_golden(tmp_path), nodes)
    root = write_layer.load(pcb)
    assert placement_anchor.outline_rect(root)[0] is None
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 1.0, "y": 1.0}])  # inside the cut-away corner
    assert result["status"] == "error" and result["code"] == "outline_arcs_unreadable", result
    assert "several arcs" in result["reason"] and pcb.read_bytes() == before
    edge = tools["place_footprints_at_edge"](str(pcb), [{"reference": "R1", "edge": "left"}])
    assert edge["status"] == "error", edge
    auto = tools["propose_autoplacement"](str(pcb), ["R1"])
    assert auto["status"] == "error", auto
    assert pcb.read_bytes() == before


def test_the_new_codes_are_named_in_the_description_and_the_skill():
    skill = (Path(__file__).parents[1] / ".claude" / "skills" / "kicad-pcb" / "SKILL.md").read_text(encoding="utf-8")
    text = _descriptions()["place_footprints"]
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    doc = " ".join(next(t.fn.__doc__ for t in mcp._tool_manager.list_tools() if t.name == "place_footprints").split())  # type: ignore[attr-defined]
    for code in ("outline_arcs_unreadable", "slots_unreadable", "footprint_unmeasurable"):
        assert code in text and code in skill and code in doc, code
    rule = "an outer loop of lines and arcs, or a gr_poly, that is neither a plain rectangle nor a rectangle with quarter-circle corner arcs is refused for the whole call, nothing written (outline_arcs_unreadable when it has any arc, such as a bite, a notch or a corner built from several arcs; outline_not_rectangular when it is lines only or a gr_poly, such as an L shape); circles, stadiums and well-formed slots are not refused; any other Edge.Cuts problem (a stray or open item, a slot that touches the outline or another slot, a duplicate outer loop, an item the tool cannot read) is refused as slots_unreadable; a board with no Edge.Cuts at all is not checked"
    assert rule in text and rule in doc and rule in skill
    assert "not recognised" not in text and "not recognised" not in skill


def test_architecture_states_the_arc_rule_and_no_longer_the_limit():
    arch = (Path(__file__).parents[1] / "docs" / ("ARCHITECTURE" + ".md")).read_text(encoding="utf-8")
    assert "or a single `gr_poly` that is not an axis-aligned rectangle, is refused whatever its arcs measure, so no size limit can leave a gap (issue 214, fail closed): `outline_arcs_unreadable` when it has any arc" in arch
    assert "`outline_not_rectangular` when it is lines only or a `gr_poly` (an L shape)" in arch
    assert "a corner built from two or more arcs (a split corner) is neither read" not in arch
    assert "kept as an explicit guard" not in arch
    assert "an arc at a corner within 0.5 mm" not in arch
    assert "whose box is at most half the shorter side across" not in arch
    assert ". a footprint added by `library_id`" not in arch


def _bite(tmp_path: Path, name: str, w: float, depth: float | None = None) -> Path:
    """The golden board with a circular-segment bite in the top side: chord 2w, mid point at depth."""
    x0, y0, x1, y1 = BOX
    xm = (x0 + x1) / 2
    d = w if depth is None else depth
    nodes = [_line((x0, y0), (xm - w, y0)), _arc((xm - w, y0), (xm, y0 + d), (xm + w, y0)), _line((xm + w, y0), (x1, y0)),
             _line((x1, y0), (x1, y1)), _line((x1, y1), (x0, y1)), _line((x0, y1), (x0, y0))]
    return _replace_outline(_golden(tmp_path, name), nodes)


@pytest.mark.parametrize("w,depth,at", [(9.0, None, (25.0, 2.0)), (10.04, None, (25.0, 2.0)), (12.0, None, (25.0, 3.0)),
                                        (24.0, None, (25.0, 3.0)), (15.0, 3.0, (25.0, 1.0))])
def test_a_bite_of_any_size_in_a_side_refuses_place_footprints(tmp_path, tools, w, depth, at):
    """``at`` is board-local mm, inside the removed segment (on the centre line x = W / 2, above the bite's apex)."""
    assert abs(at[0] - W / 2) < 1e-9 and at[1] < (w if depth is None else depth)
    pcb = _bite(tmp_path, f"bite{int(w * 100)}", w, depth)
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": at[0], "y": at[1]}])
    assert result["status"] == "error" and result["code"] == "outline_arcs_unreadable", result
    assert pcb.read_bytes() == before


def _l_nodes() -> list[SExp]:
    x0, y0, x1, y1 = BOX
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
    return [_line((x0, y0), (xm, y0)), _line((xm, y0), (xm, ym)), _line((xm, ym), (x1, ym)), _line((x1, ym), (x1, y1)),
            _line((x1, y1), (x0, y1)), _line((x0, y1), (x0, y0))]


def _poly(points: list[tuple[float, float]]) -> SExp:
    return SExp.list("gr_poly", SExp.list("pts", *[SExp.list("xy", *p) for p in points]), _stroke(),
                     SExp.list("fill", write_layer.symbol("no")), SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", str(uuid.uuid4())))


# board-local point in the missing top-right quarter of the L: x > W / 2, y < H / 2
L_MISSING = (W / 2 + 5.0, 3.0)


def test_a_lines_only_l_shape_refuses_place_footprints(tmp_path, tools):
    assert L_MISSING[0] > W / 2 and L_MISSING[1] < H / 2
    pcb = _replace_outline(_golden(tmp_path), _l_nodes())
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": L_MISSING[0], "y": L_MISSING[1]}])
    assert result["status"] == "error" and result["code"] == "outline_not_rectangular", result
    assert pcb.read_bytes() == before
    assert tools["place_footprints_at_edge"](str(pcb), [{"reference": "R1", "edge": "left"}])["code"] == "outline_not_rectangular"


def test_an_l_shaped_gr_poly_refuses_and_a_rectangular_one_is_accepted(tmp_path, tools):
    x0, y0, x1, y1 = BOX
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2
    pcb = _replace_outline(_golden(tmp_path, "lpoly"), [_poly([(x0, y0), (xm, y0), (xm, ym), (x1, ym), (x1, y1), (x0, y1)])])
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": L_MISSING[0], "y": L_MISSING[1]}])
    assert result["status"] == "error" and result["code"] == "outline_not_rectangular", result
    assert pcb.read_bytes() == before
    rect = _replace_outline(_golden(tmp_path, "rpoly"), [_poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])])
    assert tools["place_footprints"](str(rect), [{"reference": "R1", "x": 25.0, "y": 20.0}])["status"] == "ok"
    skew = _replace_outline(_golden(tmp_path, "spoly"), [_poly([(x0, y0), (x1, y0), (x1 - 5, y1), (x0, y1)])])
    assert tools["place_footprints"](str(skew), [{"reference": "R1", "x": 25.0, "y": 20.0}])["code"] == "outline_not_rectangular"


def _lookalike(tmp_path: Path, name: str, left_convex: bool = False, ratio: float = 1.0) -> Path:
    """A stadium-like outline: two semicircles with the shorter side as diameter, both concave (an hourglass)
    unless ``left_convex``; with ``ratio`` the left arc has another radius."""
    x0, y0, x1, y1 = BOX
    cy, r = (y0 + y1) / 2, H / 2
    right = _arc((x1, y0), (x1 - r, cy), (x1, y1))  # concave: bulges into the board
    lx = x0 + r if left_convex else x0
    left = _arc((lx, y1), (x0 + (0.0 if left_convex else r), cy), (lx, y0))
    extra = []
    if ratio != 1.0:  # a smaller left arc, joined to the corners by two short lines
        rr = r * ratio
        left = _arc((x0, cy + rr), (x0 + rr, cy), (x0, cy - rr))
        extra = [_line((x0, y1), (x0, cy + rr)), _line((x0, cy - rr), (x0, y0))]
    return _replace_outline(_golden(tmp_path, name), [_line((lx, y0), (x1, y0)), right, _line((x1, y1), (lx, y1)), left, *extra])


@pytest.mark.parametrize("kwargs,local", [({}, (4.0, H / 2)), ({"left_convex": True}, (W - 4.0, H / 2)), ({"ratio": 0.8}, (4.0, H / 2))],
                         ids=["hourglass", "one_concave", "unequal"])
def test_a_stadium_lookalike_is_refused(tmp_path, tools, kwargs, local):
    pcb = _lookalike(tmp_path, "hg" + "".join(kwargs), **kwargs)
    items = list(placement_anchor.raw_geometry.read_board_outline(write_layer.load(pcb)).items)
    assert not placement_anchor._is_stadium(items, BOX)
    before = pcb.read_bytes()
    # board-local point in a concave semicircle (radius H / 2 about the end's centre line point)
    centre = (0.0, H / 2) if local[0] < W / 2 else (W, H / 2)
    assert math.dist(local, centre) < H / 2
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": local[0], "y": local[1]}])
    assert result["status"] == "error" and result["code"] == "outline_arcs_unreadable", result
    assert pcb.read_bytes() == before


def test_a_stadium_whose_box_does_not_match_its_arcs_is_not_a_stadium(tmp_path):
    """Two convex semicircles of the right size but the box is wider than the arcs put it (centres off)."""
    x0, y0, x1, y1 = BOX
    r = H / 2
    stadium = [_line((x0 + r, y0), (x1 - r, y0)), _line((x1 - r, y1), (x0 + r, y1)),
               _arc((x1 - r, y0), (x1, y0 + r), (x1 - r, y1)), _arc((x0 + r, y1), (x0, y0 + r), (x0 + r, y0))]
    items = list(placement_anchor.raw_geometry.read_board_outline(write_layer.load(_replace_outline(_golden(tmp_path), stadium))).items)
    assert placement_anchor._is_stadium(items, BOX)
    assert not placement_anchor._is_stadium(items, (x0, y0, x1 + 5.0, y1))
    assert not placement_anchor._is_stadium(items, (x0 - 5.0, y0, x1, y1))


def test_a_valid_stadium_stays_accepted_with_and_without_a_slot(tmp_path, tools):
    x0, y0, x1, y1 = BOX
    r = H / 2
    stadium = [_line((x0 + r, y0), (x1 - r, y0)), _line((x1 - r, y1), (x0 + r, y1)),
               _arc((x1 - r, y0), (x1, y0 + r), (x1 - r, y1)), _arc((x0 + r, y1), (x0, y0 + r), (x0 + r, y0))]
    pcb = _replace_outline(_golden(tmp_path), stadium)
    assert placement_anchor._is_stadium(list(placement_anchor.raw_geometry.read_board_outline(write_layer.load(pcb)).items), BOX)
    assert tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 25.0, "y": 20.0}])["status"] == "ok"
    slot = [_line((x0 + 20, y0 + 18), (x0 + 30, y0 + 18)), _line((x0 + 30, y0 + 18), (x0 + 30, y0 + 22)),
            _line((x0 + 30, y0 + 22), (x0 + 20, y0 + 22)), _line((x0 + 20, y0 + 22), (x0 + 20, y0 + 18))]
    both = _replace_outline(_golden(tmp_path, "stad-slot"), stadium + slot)
    assert tools["place_footprints"](str(both), [{"reference": "R1", "x": 25.0, "y": 20.0}])["code"] == "in_cutout"
    assert tools["place_footprints"](str(both), [{"reference": "R1", "x": 8.0, "y": 20.0}])["status"] == "ok"


@pytest.mark.parametrize("radius", [0.3, 3.0, 20.0])
def test_the_accepted_classes_stay_accepted(tmp_path, tools, radius):
    pcb = _rounded(tmp_path, radius, f"r{int(radius * 10)}")
    assert tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 25.0, "y": 20.0}])["status"] == "ok"
    plain = _golden(tmp_path, "plain")
    assert tools["place_footprints"](str(plain), [{"reference": "R1", "x": 25.0, "y": 20.0}])["status"] == "ok"


# --------------------------------------------------------------------------- review round 3: any Edge.Cuts problem fails closed


def _rect_loop(x: float, y: float, w: float, h: float) -> list[SExp]:
    return [_line((x, y), (x + w, y)), _line((x + w, y), (x + w, y + h)), _line((x + w, y + h), (x, y + h)), _line((x, y + h), (x, y))]


def _no_uuid(nodes: list[SExp]) -> list[SExp]:
    for node in nodes:
        node.children.remove(node.find("uuid"))
    return nodes


def _bite_nodes(w: float = 9.0) -> list[SExp]:
    x0, y0, x1, y1 = BOX
    xm = (x0 + x1) / 2
    return [_line((x0, y0), (xm - w, y0)), _arc((xm - w, y0), (xm, y0 + w), (xm + w, y0)), _line((xm + w, y0), (x1, y0)),
            _line((x1, y0), (x1, y1)), _line((x1, y1), (x0, y1)), _line((x0, y1), (x0, y0))]


def _stadium_nodes() -> list[SExp]:
    x0, y0, x1, y1 = BOX
    r = H / 2
    return [_line((x0 + r, y0), (x1 - r, y0)), _line((x1 - r, y1), (x0 + r, y1)),
            _arc((x1 - r, y0), (x1, y0 + r), (x1 - r, y1)), _arc((x0 + r, y1), (x0, y0 + r), (x0 + r, y0))]


def _circle_node(cx: float, cy: float, r: float) -> SExp:
    return SExp.list("gr_circle", SExp.list("center", cx, cy), SExp.list("end", cx + r, cy), _stroke(),
                     SExp.list("fill", write_layer.symbol("no")), SExp.list("layer", "Edge.Cuts"), SExp.list("uuid", str(uuid.uuid4())))


def _problem_cases() -> dict:
    x0, y0, x1, y1 = BOX
    xm, ym = (x0 + x1) / 2, (y0 + y1) / 2

    def stray() -> SExp:
        return _line((x0 + 3, y0 + 30), (x0 + 8, y0 + 31))

    return {
        # name: (nodes, board-local point of the removed material or, for an outline that removes none, a point on the board)
        "l_stray_line": (lambda: _l_nodes() + [stray()], (40.0, 5.0)),
        "l_slot_touching_outer": (lambda: _l_nodes() + _rect_loop(x0, y1 - 8, 5, 4), (40.0, 5.0)),
        "l_two_touching_slots": (lambda: _l_nodes() + _rect_loop(x0 + 5, y1 - 10, 4, 4) + _rect_loop(x0 + 9, y1 - 10, 4, 4), (40.0, 5.0)),
        "bite_slot_touching": (lambda: _bite_nodes() + _rect_loop(x0, y1 - 8, 5, 4), (25.0, 2.0)),
        "stadium_slot_touching": (lambda: _stadium_nodes() + _rect_loop(xm - 3, y0, 6, 4), (25.0, 20.0)),
        "bow_tie": (lambda: [_line((x0, y0), (x1, y1)), _line((x1, y1), (x1, y0)), _line((x1, y0), (x0, y1)), _line((x0, y1), (x0, y0))], (25.0, 2.0)),
        "open_l": (lambda: _l_nodes()[:-1], (40.0, 5.0)),
        "rect_stray_arc": (lambda: _rect_loop(x0, y0, W, H) + [_arc((x0 + 3, y0 + 30), (x0 + 5, y0 + 32), (x0 + 8, y0 + 30))], (25.0, 20.0)),
        "l_no_uuid": (lambda: _no_uuid(_l_nodes()), (40.0, 5.0)),
        "bite_no_uuid": (lambda: _no_uuid(_bite_nodes()), (25.0, 2.0)),
        # siblings
        "l_two_stray_lines": (lambda: _l_nodes() + [stray(), _line((x0 + 20, y0 + 30), (x0 + 25, y0 + 31))], (40.0, 5.0)),
        "rect_two_stray_lines": (lambda: _rect_loop(x0, y0, W, H) + [_line((x1 + 3, y0), (x1 + 8, y0)), _line((x1 + 3, y0 + 4), (x1 + 8, y0 + 4))], (25.0, 20.0)),
        "l_stray_circle_outside": (lambda: _l_nodes() + [_circle_node(x1 + 10, ym, 3)], (40.0, 5.0)),
        "l_open_slot": (lambda: _l_nodes() + _rect_loop(x0 + 5, y1 - 10, 4, 4)[:-1], (40.0, 5.0)),
        "rect_open_slot": (lambda: _rect_loop(x0, y0, W, H) + _rect_loop(x0 + 5, y0 + 5, 4, 4)[:-1], (25.0, 20.0)),
        "l_duplicate_outer": (lambda: _l_nodes() + _l_nodes(), (40.0, 5.0)),
        "rect_duplicate_outer": (lambda: _rect_loop(x0, y0, W, H) + _rect_loop(x0, y0, W, H), (25.0, 20.0)),
    }


@pytest.mark.parametrize("name", sorted(_problem_cases()))
def test_any_edge_cuts_problem_makes_place_footprints_refuse(tmp_path, tools, name):
    build, local = _problem_cases()[name]
    pcb = _replace_outline(_golden(tmp_path, name), build())
    before = pcb.read_bytes()
    result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": local[0], "y": local[1]}])
    assert result["status"] == "error", result
    assert result["code"] in ("slots_unreadable", "outline_arcs_unreadable", "outline_not_rectangular"), result
    assert result.get("reason"), result
    assert pcb.read_bytes() == before


def test_the_removed_material_of_the_problem_cases_is_really_removed():
    """The L misses x > W / 2, y < H / 2; the bite misses the half disc of radius 9 about (W / 2, 0)."""
    assert 40.0 > W / 2 and 5.0 < H / 2
    assert math.dist((25.0, 2.0), (W / 2, 0.0)) < 9.0


def test_a_board_without_any_edge_cuts_keeps_its_behaviour(tmp_path, tools):
    pcb = _replace_outline(_golden(tmp_path, "none"), [])
    assert placement_anchor.cutout_boxes(write_layer.load(pcb)) == ()
    assert tools["place_footprints"](str(pcb), [{"reference": "R1", "x": 25.0, "y": 20.0}])["status"] == "ok"


def test_clean_slots_stay_accepted_with_every_readable_outer_loop(tmp_path, tools):
    x0, y0, x1, y1 = BOX
    slot = _rect_loop(x0 + 20, y0 + 18, 10, 4)
    for name, outer in (("rect", _rect_loop(x0, y0, W, H)), ("stadium", _stadium_nodes()), ("circle", [_circle_node((x0 + x1) / 2, (y0 + y1) / 2, 19.0)]),
                        ("poly", [_poly([(x0, y0), (x1, y0), (x1, y1), (x0, y1)])])):
        pcb = _replace_outline(_golden(tmp_path, "s" + name), outer + [n for n in slot])
        bx0, by0, *_ = placement_anchor.raw_geometry.read_board_outline(write_layer.load(pcb)).bbox_mm  # board-local origin
        assert tools["place_footprints"](str(pcb), [{"reference": "R1", "x": x0 + 12.0 - bx0, "y": y0 + 20.0 - by0}])["status"] == "ok", name
        slot = _rect_loop(x0 + 20, y0 + 18, 10, 4)
        assert tools["place_footprints"](str(pcb), [{"reference": "R1", "x": x0 + 25.0 - bx0, "y": y0 + 20.0 - by0}])["code"] == "in_cutout", name
    nouuid = _replace_outline(_golden(tmp_path, "snouuid"), _no_uuid(_rect_loop(x0, y0, W, H) + _rect_loop(x0 + 20, y0 + 18, 10, 4)))
    assert tools["place_footprints"](str(nouuid), [{"reference": "R1", "x": 25.0, "y": 20.0}])["code"] == "in_cutout"


# --------------------------------------------------------------------------- _is_stadium: every guard pinned by its own case


def _it(kind, *points):
    return types.SimpleNamespace(kind=kind, points=points)


def _stadium_items() -> list:
    x0, y0, x1, y1 = BOX
    r, cy = H / 2, (y0 + y1) / 2
    return [_it("line", (x0 + r, y0), (x1 - r, y0)), _it("line", (x1 - r, y1), (x0 + r, y1)),
            _it("arc", (x1 - r, y0), (x1, cy), (x1 - r, y1)), _it("arc", (x0 + r, y1), (x0, cy), (x0 + r, y0))]


def _diagonal_pair() -> list:
    """Two semicircles whose mid points point diagonally, opposite each other, at the right ends."""
    x0, y0, x1, y1 = BOX
    r, cy, q = H / 2, (y0 + y1) / 2, math.sqrt(0.5)
    base = _stadium_items()
    out = base[:2]
    for centre, sign in (((x0 + r, cy), -1), (((x1 - r), cy), 1)):
        a = (centre[0] + r * q, centre[1] - r * q)
        b = (centre[0] - r * q, centre[1] + r * q)
        m = (centre[0] + sign * r * q, centre[1] + sign * r * q)
        out.append(_it("arc", a, m, b) if sign < 0 else _it("arc", b, m, a))
    return out


def _stadium_variants() -> dict:
    x0, y0, x1, y1 = BOX
    r, cy = H / 2, (y0 + y1) / 2
    base = _stadium_items()
    c = (x0 + r, cy)
    k = math.cos(math.radians(135))
    skew_a, skew_b = (c[0] + r * k, c[1] + r * math.sin(math.radians(135))), (c[0] - r * k, c[1] - r * math.sin(math.radians(135)))
    return {
        "extra_line": base + [_it("line", (x0 + r, y0), (x0 + r + 1, y0))],
        "one_line": base[:1] + base[2:],
        "three_arcs": base + [_it("arc", (x0 + 5, y0), (x0 + 6, y0 + 1), (x0 + 7, y0))],
        "one_arc": base[:3],
        "small_radius": base[:2] + [_it("arc", (x1 - r, y0 + 2), (x1 - 2, cy), (x1 - r, y1 - 2)),
                                    _it("arc", (x0 + r, y1 - 2), (x0 + 2, cy), (x0 + r, y0 + 2))],
        "centre_off_chord": base[:3] + [_it("arc", (x0 + r, y1), (x0, cy), (x0 + r, y0 + 1))],
        "mid_off_axis": base[:3] + [_it("arc", (x0 + r, y1), (c[0] - r * 0.7071, c[1] - r * 0.7071), (x0 + r, y0))],
        "chord_not_perpendicular": base[:3] + [_it("arc", skew_a, (x0, cy), skew_b)],
        "both_at_one_end": base[:2] + [_it("arc", (x1 - 2 * r, cy), (x1 - r, y0), (x1, cy)), _it("arc", (x1, cy), (x1 - r, y1), (x1 - 2 * r, cy))],
        "minor_arc": base[:3] + [_it("arc", (c[0] - 5, c[1] + math.sqrt(r * r - 25)), (x0, cy), (c[0] - 5, c[1] - math.sqrt(r * r - 25)))],
        "diagonal_pair": _diagonal_pair(),
        "same_direction": base[:2] + [base[2], _it("arc", (x1 - r, y1), (x1, cy), (x1 - r, y0))],
        "concave_end": base[:3] + [_it("arc", (x0 + r, y1), (x0 + 2 * r, cy), (x0 + r, y0))],
        "centre_off_box": base[:3] + [_it("arc", (x0 + r + 2, y1), (x0 + 2, cy), (x0 + r + 2, y0))],
    }


def test_the_valid_stadium_items_are_a_stadium_and_every_variant_is_not():
    assert placement_anchor._is_stadium(_stadium_items(), BOX)
    for name, items in _stadium_variants().items():
        assert not placement_anchor._is_stadium(items, BOX), name


def test_a_stadium_with_a_notch_or_a_split_side_is_refused_by_place_footprints(tmp_path, tools):
    x0, y0, x1, y1 = BOX
    r = H / 2
    xm = (x0 + x1) / 2
    notched = [_line((x0 + r, y0), (xm - 3, y0)), _arc((xm - 3, y0), (xm, y0 + 3), (xm + 3, y0)), _line((xm + 3, y0), (x1 - r, y0)),
               *_stadium_nodes()[1:]]
    split = [_line((x0 + r, y0), (xm, y0)), _line((xm, y0), (x1 - r, y0)), *_stadium_nodes()[1:]]
    for name, nodes, local in (("notch", notched, (25.0 + 0.0, 2.0)), ("split", split, (25.0, 20.0))):
        pcb = _replace_outline(_golden(tmp_path, "st" + name), nodes)
        before = pcb.read_bytes()
        result = tools["place_footprints"](str(pcb), [{"reference": "R1", "x": local[0], "y": local[1]}])
        assert result["status"] == "error" and result["code"] == "outline_arcs_unreadable", (name, result)
        assert pcb.read_bytes() == before


def test_architecture_states_the_fail_closed_rule_and_the_circle_limit():
    arch = (Path(__file__).parents[1] / "docs" / ("ARCHITECTURE" + ".md")).read_text(encoding="utf-8")
    assert "is refused as `slots_unreadable` by `placement_anchor.cutout_boxes`, which never lets `edge_loops.classify` problems or missing uuids switch the check off" in arch
    assert "a board with no Edge.Cuts item at all is not measured and `place_footprints` places as before" in arch
    assert ("Known limit: for a circle or a stadium `place_footprints` does not check a part against the outline itself, "
            "so a part in the bounding-box corner outside the circle is accepted, as is a part outside the board") in arch
    assert "the check also runs on a board whose outer loop is not a rectangle when its slots are understood" not in arch
