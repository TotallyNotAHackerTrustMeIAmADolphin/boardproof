"""Edge anchoring, locked footprints and the inside-the-outline autoplacement (GitHub issue 133).

Every position claim here is measured on the raw tokens of the saved file: the footprint's
``(at x y angle)`` and the ``(fp_rect (start ..) (end ..) (layer "F.CrtYd"))`` it holds, rotated by
hand for the angles used, never through a ``kicad_tools`` convenience property. Fixtures are built
through ``kicad_tools``' object model and saved through the write layer.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import write_layer
from boardproof.sourcing.footprint_gen import generate_footprint
from boardproof.toolsets import placement as placement_toolset

BOARD_W, BOARD_H = 100.0, 60.0


def _tools() -> dict:
    mcp = MCPServer("test")
    placement_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


@pytest.fixture
def tools() -> dict:
    return _tools()


@pytest.fixture
def chips(tmp_path) -> dict[str, Path]:
    out = {}
    for size in ("0805", "1206"):
        path = tmp_path / f"{size}.kicad_mod"
        assert generate_footprint("chip", str(path), size=size)["status"] == "ok"
        out[size] = path
    return out


def _board(tmp_path: Path, chips: dict, parts: list[tuple]) -> Path:
    """A 100 x 60 board whose outline corner sits at page (98.5, 75), with ``parts``
    ``(reference, size, local x, local y[, rotation])`` placed through kicad_tools."""
    from kicad_tools.schema import PCB

    board = PCB.create(width=BOARD_W, height=BOARD_H)
    for ref, size, x, y, *rot in parts:
        board.add_footprint_from_file(chips[size], ref, x, y, rotation=rot[0] if rot else 0.0)
    path = tmp_path / "b.kicad_pcb"
    write_layer.save_pcb(board, path)
    return path


# --------------------------------------------------------------------------- raw readers


def _root(path):
    return write_layer.load(path)


def _fp(root, ref):
    for node in root.children:
        if not node.is_atom and node.name == "footprint":
            for child in node.children:
                if not child.is_atom and child.name == "property" and child.children[0].value == "Reference" \
                        and child.children[1].value == ref:
                    return node
    raise AssertionError(f"no footprint {ref}")


def _raw_at(path, ref):
    at = _fp(_root(path), ref).find("at")
    values = [float(v) for v in at.values]
    return values[0], values[1], (values[2] if len(values) > 2 else 0.0)


def _is_locked(path, ref):
    return any(not c.is_atom and c.name == "locked" and c.values[0] == "yes" for c in _fp(_root(path), ref).children)


def _raw_outline(path):
    xs, ys = [], []
    for node in _root(path).children:
        if not node.is_atom and node.name == "gr_line":
            for tag in ("start", "end"):
                xs.append(float(node.find(tag).values[0]))
                ys.append(float(node.find(tag).values[1]))
    return min(xs), min(ys), max(xs), max(ys)


def _raw_courtyard(path, ref):
    """Page box of the courtyard rectangle, rotated by hand (angles that are multiples of 90)."""
    fp = _fp(_root(path), ref)
    x, y, angle = _raw_at(path, ref)
    pts = []
    for node in fp.children:
        if not node.is_atom and node.name == "fp_rect" and node.find("layer").values[0] == "F.CrtYd":
            (sx, sy), (ex, ey) = ([float(v) for v in node.find(t).values] for t in ("start", "end"))
            for lx, ly in ((sx, sy), (ex, sy), (ex, ey), (sx, ey)):
                c, s = round(math.cos(math.radians(angle))), round(math.sin(math.radians(angle)))
                pts.append((x + lx * c + ly * s, y - lx * s + ly * c))
    return min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)


def _overlap(a, b):
    return a[0] < b[2] - 1e-6 and b[0] < a[2] - 1e-6 and a[1] < b[3] - 1e-6 and b[1] < a[3] - 1e-6


def _refs(path):
    out = []
    for node in _root(path).children:
        if not node.is_atom and node.name == "footprint":
            out += [c.children[1].value for c in node.children
                    if not c.is_atom and c.name == "property" and c.children[0].value == "Reference"]
    return out


# --------------------------------------------------------------------------- lock_footprints


def test_lock_sets_the_raw_token_and_touches_nothing_else(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "0805", 10, 10), ("R2", "0805", 30, 10)])
    before = {ref: _raw_at(pcb, ref) for ref in ("R1", "R2")}
    result = tools["lock_footprints"](str(pcb), ["R1"])
    assert result["status"] == "ok" and result["changed"] == ["R1"] and result["locked"] is True, result
    assert _is_locked(pcb, "R1") and not _is_locked(pcb, "R2")
    assert {ref: _raw_at(pcb, ref) for ref in ("R1", "R2")} == before
    again = tools["lock_footprints"](str(pcb), ["R1", "R2"])
    assert again["changed"] == ["R2"] and again["unchanged"] == ["R1"], again
    assert _is_locked(pcb, "R2")


def test_unlock_removes_the_token(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "0805", 10, 10)])
    tools["lock_footprints"](str(pcb), ["R1"])
    result = tools["lock_footprints"](str(pcb), ["R1"], locked=False)
    assert result["status"] == "ok" and result["changed"] == ["R1"], result
    assert not _is_locked(pcb, "R1")
    assert "(locked" not in pcb.read_text(encoding="utf-8")


def test_lock_with_an_unknown_reference_writes_nothing(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "0805", 10, 10)])
    before = pcb.read_bytes()
    result = tools["lock_footprints"](str(pcb), ["R1", "NOPE"])
    assert result["status"] == "error" and result["code"] == "unknown_reference", result
    assert pcb.read_bytes() == before


def test_lock_is_read_by_kicad_cli(tmp_path, chips, tools):
    """kicad-cli parses the file with the token where this tool put it (the write layer's parse check)."""
    pcb = _board(tmp_path, chips, [("R1", "0805", 10, 10)])
    assert tools["lock_footprints"](str(pcb), ["R1"])["status"] == "ok"
    from boardproof import kicad_cli

    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    assert write_layer.parse_check(pcb, "pcb") == ["b.kicad_pcb"]  # raises when KiCad cannot load it


# --------------------------------------------------------------------------- place_footprints_at_edge


@pytest.mark.parametrize("edge", ["left", "right", "top", "bottom"])
def test_a_part_lands_at_the_requested_offset_from_each_edge(tmp_path, chips, tools, edge):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": edge, "offset_mm": 3.0}])
    assert result["status"] == "ok", result
    outline = _raw_outline(pcb)
    box = _raw_courtyard(pcb, "J1")
    gap = {"left": box[0] - outline[0], "right": outline[2] - box[2],
           "top": box[1] - outline[1], "bottom": outline[3] - box[3]}[edge]
    assert gap == pytest.approx(3.0, abs=1e-6)
    assert result["moved"][0]["gap_mm"] == pytest.approx(3.0, abs=1e-6)
    # centred along the edge
    if edge in ("left", "right"):
        assert (box[1] + box[3]) / 2 == pytest.approx((outline[1] + outline[3]) / 2, abs=1e-6)
    else:
        assert (box[0] + box[2]) / 2 == pytest.approx((outline[0] + outline[2]) / 2, abs=1e-6)
    assert _inside(box, outline)


def _inside(box, rect):
    return box[0] >= rect[0] - 1e-6 and box[1] >= rect[1] - 1e-6 and box[2] <= rect[2] + 1e-6 and box[3] <= rect[3] + 1e-6


def test_along_mm_positions_the_courtyard_centre_from_the_corner(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    result = tools["place_footprints_at_edge"](
        str(pcb), [{"reference": "J1", "edge": "top", "offset_mm": 1.0, "along_mm": 25.0}])
    assert result["status"] == "ok", result
    outline, box = _raw_outline(pcb), _raw_courtyard(pcb, "J1")
    assert (box[0] + box[2]) / 2 == pytest.approx(outline[0] + 25.0, abs=1e-6)
    assert box[1] - outline[1] == pytest.approx(1.0, abs=1e-6)
    assert result["moved"][0]["x"] == pytest.approx(25.0 + (box[0] + box[2]) / 2 - (box[0] + box[2]) / 2, abs=1e-6)


def test_a_rotated_part_is_measured_by_its_rotated_courtyard(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30, 90.0)])
    angle_before = _raw_at(pcb, "J1")[2]
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left", "offset_mm": 2.0}])
    assert result["status"] == "ok", result
    outline, box = _raw_outline(pcb), _raw_courtyard(pcb, "J1")
    assert box[0] - outline[0] == pytest.approx(2.0, abs=1e-6)
    assert box[2] - box[0] == pytest.approx(2.1, abs=1e-6)  # the 1206 courtyard is 4.4 x 2.1: turned, 2.1 wide
    assert _raw_at(pcb, "J1")[2] == angle_before  # rotation kept


def test_only_the_placed_footprint_and_nothing_else_changes(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30), ("R1", "0805", 10, 10)])
    before = _raw_at(pcb, "R1")
    text_before = pcb.read_text(encoding="utf-8")
    assert tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left"}])["status"] == "ok"
    assert _raw_at(pcb, "R1") == before
    # the file differs from the original in the footprint J1's (at) line and nowhere else
    changed = [(a, b) for a, b in zip(text_before.splitlines(), pcb.read_text(encoding="utf-8").splitlines(), strict=True) if a != b]
    assert len(changed) == 1 and changed[0][0].lstrip().startswith("(at"), changed


def test_a_batch_places_several_parts_at_once(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30), ("J2", "1206", 60, 30)])
    result = tools["place_footprints_at_edge"](str(pcb), [
        {"reference": "J1", "edge": "left", "offset_mm": 2.0, "along_mm": 15.0},
        {"reference": "J2", "edge": "left", "offset_mm": 2.0, "along_mm": 40.0},
    ])
    assert result["status"] == "ok" and [m["reference"] for m in result["moved"]] == ["J1", "J2"], result
    outline = _raw_outline(pcb)
    for ref in ("J1", "J2"):
        assert _raw_courtyard(pcb, ref)[0] - outline[0] == pytest.approx(2.0, abs=1e-6)


@pytest.mark.parametrize("spec, code", [
    ({"reference": "J1", "edge": "left", "offset_mm": 98.0}, "outside_outline"),
    ({"reference": "J1", "edge": "left", "offset_mm": -1.0}, "invalid_spec"),
    ({"reference": "J1", "edge": "middle"}, "invalid_spec"),
    ({"reference": "J1", "edge": "left", "along_mm": 500.0}, "outside_outline"),
    ({"reference": "NOPE", "edge": "left"}, "unknown_reference"),
])
def test_a_bad_spec_refuses_the_whole_call_and_writes_nothing(tmp_path, chips, tools, spec, code):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30), ("J2", "1206", 60, 30)])
    before = pcb.read_bytes()
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J2", "edge": "right"}, spec])
    assert result["status"] == "error" and result["problems"][0]["code"] == code, result
    assert pcb.read_bytes() == before


def test_a_locked_part_is_never_moved(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    tools["lock_footprints"](str(pcb), ["J1"])
    before = _raw_at(pcb, "J1")
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left"}])
    assert result["status"] == "error" and result["problems"][0]["code"] == "locked_footprint", result
    assert _raw_at(pcb, "J1") == before


def test_a_collision_with_another_courtyard_refuses_the_call(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30), ("R1", "1206", 2.5, 30)])
    before = pcb.read_bytes()
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left", "offset_mm": 0.0}])
    assert result["status"] == "error" and result["problems"][0]["code"] == "collision", result
    assert pcb.read_bytes() == before
    # two placed parts that would meet each other
    result = tools["place_footprints_at_edge"](str(pcb), [
        {"reference": "J1", "edge": "right", "along_mm": 30.0}, {"reference": "R1", "edge": "right", "along_mm": 30.5}])
    assert result["status"] == "error" and result["problems"][0]["code"] == "collision", result


def _outline_variant(tmp_path, chips, kind):
    from kicad_tools.sexp import SExp

    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    root = write_layer.load(pcb)
    lines = [n for n in root.children if not n.is_atom and n.name == "gr_line"]
    if kind == "none":
        for n in lines:
            root.children.remove(n)
    elif kind == "circle":
        # the whole outline is one circle (a circle INSIDE the rectangle is a round cut-out since issue 132)
        for n in lines:
            root.children.remove(n)
        root.children.append(SExp.list(
            "gr_circle", SExp.list("center", 120, 100), SExp.list("end", 125, 100),
            SExp.list("stroke", SExp.list("width", 0.1), SExp.list("type", "default")),
            SExp.list("fill", write_layer.symbol("no")), SExp.list("layer", "Edge.Cuts")))
    elif kind == "slanted":
        end = lines[0].find("end")
        write_layer.set_number(end, 1, float(end.values[1]) + 3.0)
    write_layer.save(root, pcb)
    return pcb


@pytest.mark.parametrize("kind, code", [("none", "no_outline"), ("circle", "outline_not_rectangular"),
                                         ("slanted", "outline_not_rectangular")])
def test_without_a_rectangular_outline_edge_placement_refuses(tmp_path, chips, tools, kind, code):
    pcb = _outline_variant(tmp_path, chips, kind)
    before = pcb.read_bytes()
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left"}])
    assert result["status"] == "error" and result["code"] == code, result
    assert pcb.read_bytes() == before


# --------------------------------------------------------------------------- the self-check fires


def _corrupt_write(monkeypatch, edit):
    original = write_layer._write_temp

    def corrupting(text, path):
        return original(edit(text), path)

    monkeypatch.setattr(write_layer, "_write_temp", corrupting)


def _shift_first_footprint_at(text: str) -> str:
    def bump(m):
        return f"\n\t\t(at {float(m.group(1)) + 0.5:g} {m.group(2)}"

    # a footprint's own (at ...) sits two tabs deep; its pads' and texts' are deeper
    return re.sub(r"\n\t\t\(at ([-\d.]+) ([-\d.]+)", bump, text, count=1)


def test_edge_placement_self_check_fires_on_a_corrupted_write(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    before = pcb.read_bytes()
    _corrupt_write(monkeypatch, _shift_first_footprint_at)
    result = tools["place_footprints_at_edge"](str(pcb), [{"reference": "J1", "edge": "left", "offset_mm": 3.0}])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert pcb.read_bytes() == before


def test_lock_self_check_fires_when_the_token_is_not_written(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("J1", "1206", 40, 30)])
    before = pcb.read_bytes()
    _corrupt_write(monkeypatch, lambda text: text.replace("(locked yes)", ""))
    result = tools["lock_footprints"](str(pcb), ["J1"])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert pcb.read_bytes() == before


# --------------------------------------------------------------------------- autoplacement inside the outline


def _parts_grid(n, size="1206"):
    return [(f"R{i}", size, 5 + 3 * (i % 5), 5 + 3 * (i // 5)) for i in range(n)]


def _propose(tools, pcb, refs=None, **kw):
    result = tools["propose_autoplacement"](str(pcb), refs, **kw)
    assert result["status"] == "pending_confirmation", result
    return result


def test_autoplacement_stays_inside_the_outline_and_keeps_clear_of_locked_parts(tmp_path, chips, tools):
    parts = _parts_grid(12) + [("L1", "1206", 20, 12)]
    pcb = _board(tmp_path, chips, parts)
    tools["lock_footprints"](str(pcb), ["L1"])
    locked_before = _raw_at(pcb, "L1")
    locked_box = _raw_courtyard(pcb, "L1")
    proposal = _propose(tools, pcb)
    assert proposal["locked_skipped"] == ["L1"] and "L1" not in proposal["proposed_positions"]
    confirm = tools["confirm_autoplacement"](proposal["revision_token"])
    assert confirm["status"] == "confirmed", confirm
    outline = _raw_outline(pcb)
    boxes = {ref: _raw_courtyard(pcb, ref) for ref in _refs(pcb)}
    assert _raw_at(pcb, "L1") == locked_before  # the locked part is where it was
    for ref, box in boxes.items():
        assert _inside(box, outline), (ref, box, outline)
        if ref != "L1":
            assert not _overlap(box, locked_box), ref
            assert box[0] - outline[0] >= 0.5 - 1e-6 and box[1] - outline[1] >= 0.5 - 1e-6
    names = sorted(boxes)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            assert not _overlap(boxes[a], boxes[b]), (a, b)


def test_autoplacement_keeps_clear_of_footprints_that_are_not_in_refs(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "1206", 3, 3), ("R2", "1206", 50, 40), ("J1", "1206", 3.2, 3.2)])
    proposal = _propose(tools, pcb, ["R1"])
    assert tools["confirm_autoplacement"](proposal["revision_token"])["status"] == "confirmed"
    assert not _overlap(_raw_courtyard(pcb, "R1"), _raw_courtyard(pcb, "J1"))
    assert not _overlap(_raw_courtyard(pcb, "R1"), _raw_courtyard(pcb, "R2"))
    assert _raw_at(pcb, "J1")[:2] == pytest.approx((98.5 + 3.2, 75.0 + 3.2))  # untouched, raw page position


def test_autoplacement_keeps_each_rotation_and_writes_only_x_and_y(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "1206", 50, 30, 90.0), ("R2", "1206", 20, 20)])
    proposal = _propose(tools, pcb)
    assert proposal["proposed_positions"]["R1"]["rotation"] == 90.0
    assert tools["confirm_autoplacement"](proposal["revision_token"])["status"] == "confirmed"
    assert _raw_at(pcb, "R1")[2] == 90.0
    assert _inside(_raw_courtyard(pcb, "R1"), _raw_outline(pcb))


def test_autoplacement_that_does_not_fit_proposes_nothing(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, _parts_grid(60))
    result = tools["propose_autoplacement"](str(pcb))
    # 60 parts of 4.4 x 2.1 with 2 mm spacing need far more than 100 x 60 mm? they fit: shrink the clearance question
    if result["status"] == "pending_confirmation":
        result = tools["propose_autoplacement"](str(pcb), None, edge_clearance_mm=0.5, spacing_mm=20.0)
    assert result["status"] == "error" and result["code"] == "does_not_fit" and result["unplaced"], result
    assert "revision_token" not in result


def test_autoplacement_refuses_an_outline_that_is_not_a_rectangle(tmp_path, chips, tools):
    pcb = _outline_variant(tmp_path, chips, "circle")
    result = tools["propose_autoplacement"](str(pcb), ["J1"])
    assert result["status"] == "error" and result["code"] == "outline_not_rectangular", result


def test_a_proposal_for_a_part_locked_since_is_refused_and_keeps_its_token(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "1206", 50, 30), ("R2", "1206", 20, 20)])
    proposal = _propose(tools, pcb)
    tools["lock_footprints"](str(pcb), ["R1"])
    before = pcb.read_bytes()
    refused = tools["confirm_autoplacement"](proposal["revision_token"])
    assert refused["status"] == "error" and refused["code"] == "locked_footprint", refused
    assert pcb.read_bytes() == before
    tools["lock_footprints"](str(pcb), ["R1"], locked=False)
    assert tools["confirm_autoplacement"](proposal["revision_token"])["status"] == "confirmed"


def test_a_proposal_that_no_longer_fits_the_board_is_stale(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "1206", 50, 30), ("R2", "1206", 20, 20)])
    proposal = _propose(tools, pcb, ["R1"])
    r1 = _raw_courtyard(pcb, "R1")
    # a part placed where the proposal puts R1 after the proposal was made
    target = proposal["proposed_positions"]["R1"]
    placed = tools["place_footprints"](str(pcb), [{"reference": "R2", "x": target["x"], "y": target["y"]}])
    assert placed["status"] == "ok", placed
    before = pcb.read_bytes()
    refused = tools["confirm_autoplacement"](proposal["revision_token"])
    assert refused["status"] == "error" and refused["code"] == "proposal_stale", (refused, r1)
    assert pcb.read_bytes() == before


def test_a_proposal_that_leaves_a_shrunk_outline_is_stale(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, _parts_grid(6))
    proposal = _propose(tools, pcb)
    root = write_layer.load(pcb)
    for node in root.children:  # the board is made narrower after the proposal (its corner stays)
        if not node.is_atom and node.name == "gr_line":
            for tag in ("start", "end"):
                point = node.find(tag)
                if float(point.values[0]) == 98.5 + BOARD_W:
                    write_layer.set_number(point, 0, 98.5 + 20.0)
    write_layer.save(root, pcb)
    before = pcb.read_bytes()
    refused = tools["confirm_autoplacement"](proposal["revision_token"])
    assert refused["status"] == "error" and refused["code"] == "proposal_stale", refused
    assert "outside the outline" in refused["reason"]
    assert pcb.read_bytes() == before


def test_autoplacement_confirm_self_check_fires_on_a_corrupted_write(tmp_path, chips, tools, monkeypatch):
    pcb = _board(tmp_path, chips, [("R1", "1206", 50, 30)])
    proposal = _propose(tools, pcb)
    before = pcb.read_bytes()
    _corrupt_write(monkeypatch, _shift_first_footprint_at)
    result = tools["confirm_autoplacement"](proposal["revision_token"])
    assert result.get("ok") is False and result["code"] == "write_not_verified", result
    assert pcb.read_bytes() == before
    monkeypatch.undo()
    assert tools["confirm_autoplacement"](proposal["revision_token"])["status"] == "confirmed"  # the token stayed valid


# --------------------------------------------------------------------------- the other tools honour a lock


def test_the_other_placement_tools_leave_a_locked_part_alone(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "1206", 10, 10), ("R2", "1206", 30, 14), ("R3", "1206", 52.3, 22.2)])
    tools["lock_footprints"](str(pcb), ["R2"])
    before = _raw_at(pcb, "R2")
    aligned = tools["align_footprints"](str(pcb), ["R1", "R2", "R3"], "y", "center")
    assert aligned["status"] == "ok" and aligned["locked_skipped"] == ["R2"], aligned
    assert _raw_at(pcb, "R2") == before
    assert _raw_at(pcb, "R1")[1] == pytest.approx(85.0, abs=1e-6)  # the anchor (first ref) stays
    assert _raw_at(pcb, "R3")[1] == pytest.approx(85.0, abs=1e-6)  # the movable one joins it
    dist = tools["distribute_footprints"](str(pcb), ["R1", "R2", "R3"], "x")
    assert dist["status"] == "ok" and dist["locked_skipped"] == ["R2"], dist
    assert _raw_at(pcb, "R2") == before
    snapped = tools["snap_footprints_to_grid"](str(pcb), ["R1", "R2", "R3"], 2.54)
    assert snapped["status"] == "ok" and snapped["locked_skipped"] == ["R2"], snapped
    assert _raw_at(pcb, "R2") == before
    placed = tools["place_footprints"](str(pcb), [{"reference": "R2", "x": 1.0, "y": 1.0}, {"reference": "R3", "x": 60.0, "y": 40.0}])
    assert placed["status"] == "partial" and placed["moved"] == ["R3"], placed
    assert placed["errors"][0]["code"] == "locked_footprint"
    assert _raw_at(pcb, "R2") == before
    packed = tools["pack_footprints_densely"](str(pcb), ["R1", "R2", "R3"])
    assert packed["status"] == "error" and packed["code"] == "locked_footprint", packed
    assert _raw_at(pcb, "R2") == before


def test_a_locked_anchor_still_anchors_the_alignment(tmp_path, chips, tools):
    pcb = _board(tmp_path, chips, [("R1", "1206", 10, 10), ("R2", "1206", 30, 14)])
    tools["lock_footprints"](str(pcb), ["R1"])
    before = _raw_at(pcb, "R1")
    aligned = tools["align_footprints"](str(pcb), ["R1", "R2"], "y", "center")
    assert aligned["status"] == "ok", aligned
    assert _raw_at(pcb, "R1") == before
    assert _raw_at(pcb, "R2")[1] == pytest.approx(before[1], abs=1e-6)
