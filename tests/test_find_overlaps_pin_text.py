"""find_overlaps, issue 225: global and hierarchical label shapes, pin numbers and names as obstacles, the measured character width.

Every box rule is checked against the svg that kicad-cli 10.0.7 draws (``sch export svg``); the fixtures are built with the
schematic tools. The justify of a label written by ``add_labels`` is always ``left``; the KiCad-native ``right`` is set on the
parsed tree and written back with the write layer's serialiser (test-local, under ``tmp_path``).
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, schematic_readability as readability, sheet_overlaps, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


def _tools() -> dict:
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    return {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]


def _project(tmp_path: Path) -> Path:
    return Path(project_toolset._project_create(str(tmp_path / "proj"), "demo")["schematic_path"])


def _svg_texts(svg: Path, height: float = 1.27) -> list[tuple[str, tuple[float, float, float, float], float]]:
    """(text, box on the sheet, textLength) of every text kicad-cli draws; the box is textLength along the text and 1.27 across,
    ending at the baseline (the convention of tests/test_find_overlaps_turned.py); rotate(...) ancestors are applied."""
    tree = ET.parse(svg)
    parent = {c: p for p in tree.iter() for c in p}
    out = []
    for el in tree.iter():
        if not (el.tag.endswith("text") and (el.text or "").strip() and el.get("textLength")):
            continue
        x, y, length = float(el.get("x")), float(el.get("y")), float(el.get("textLength"))
        x0 = {"start": x, "middle": x - length / 2, "end": x - length}[el.get("text-anchor", "start")]
        pts = [(x0, y - height), (x0 + length, y - height), (x0, y), (x0 + length, y)]
        cur = el
        while cur in parent:
            cur = parent[cur]
            for a, cx, cy in re.findall(r"rotate\(([-\d.]+) ([-\d.]+) ([-\d.]+)\)", cur.get("transform") or ""):
                r = math.radians(float(a))
                px, py = float(cx), float(cy)
                pts = [(px + (u - px) * math.cos(r) - (v - py) * math.sin(r), py + (u - px) * math.sin(r) + (v - py) * math.cos(r)) for u, v in pts]
        out.append((el.text.strip(), (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)), length))
    return out


def _export(sch: Path, out: Path) -> Path:
    kicad_cli.export_svg(sch, out)
    return next(out.glob("*.svg"))


def _overlap(a, b) -> tuple[float, float]:
    return min(a[2], b[2]) - max(a[0], b[0]), min(a[3], b[3]) - max(a[1], b[1])


# --------------------------------------------------------------------------- global and hierarchical label shapes (step c)

SHAPES = ["input", "output", "bidirectional", "tri_state", "passive"]


@pytest.fixture(scope="module")
def label_sheet(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("labels")
    sch = _project(tmp)
    specs, meta = [], []
    for kind in ("global", "hier"):
        for shape in SHAPES:
            for angle in (0, 90, 180, 270):
                i = len(specs)
                x, y = 30.0 + 30.0 * (i % 8), 30.0 + 30.0 * (i // 8)
                specs.append({"kind": kind, "x": x, "y": y, "text": f"AB{i}", "rotation": angle, "shape": shape, "snap": False})
                meta.append((f"AB{i}", kind, shape, angle, x, y))
    assert _tools()["add_labels"](str(sch), specs)["status"] == "ok"
    root = write_layer.load(sch)
    right = tmp / "right.kicad_sch"
    swapped = write_layer.load(sch)
    for node in swapped.children:
        if not node.is_atom and node.name in ("global_label", "hierarchical_label"):
            node.find("effects").find_children("justify")[0].children[0] = write_layer.symbol("right")
    write_layer.save(swapped, right)
    big = write_layer.load(sch)   # the same labels at font size 2.54
    for node in big.children:
        if not node.is_atom and node.name in ("global_label", "hierarchical_label"):
            size = node.find("effects").find("font").find("size")
            size.children[0], size.children[1] = write_layer.number(2.54), write_layer.number(2.54)
    write_layer.save(big, tmp / "big.kicad_sch")
    return {"left": (sch, root), "right": (right, write_layer.load(right)), "big": (tmp / "big.kicad_sch", write_layer.load(tmp / "big.kicad_sch"))}, meta, tmp


def _drawn_union(svg: Path, text: str, anchor: tuple[float, float], height: float = 1.27) -> tuple[float, float, float, float]:
    """The drawn shape polygon (a closed path with a vertex on the anchor, when there is one) and text of one label, and the anchor itself."""
    box = next(b for t, b, _l in _svg_texts(svg, height) if t == text)
    xs, ys = [box[0], box[2], anchor[0]], [box[1], box[3], anchor[1]]
    for m in re.finditer(r'd="(M[^"]*?Z)"', svg.read_text(encoding="utf-8")):
        pts = [tuple(map(float, p.split(","))) for p in re.findall(r"[-\d.]+,[-\d.]+", m.group(1))]
        if 3 <= len(pts) <= 8 and any(abs(a - anchor[0]) < 0.01 and abs(b - anchor[1]) < 0.01 for a, b in pts):
            xs += [a for a, _b in pts]
            ys += [b for _a, b in pts]
    return min(xs), min(ys), max(xs), max(ys)


@pytest.mark.parametrize("side", ["left", "right", "big"])
@pytest.mark.parametrize("kind", ["global", "hier"])
@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("angle", [0, 90, 180, 270])
def test_the_box_of_a_global_or_hierarchical_label_is_its_shape_and_text_as_drawn(label_sheet, side, kind, shape, angle):
    files, meta, tmp = label_sheet
    sch, root = files[side]
    name, _k, _s, _a, x, y = next(m for m in meta if m[1:4] == (kind, shape, angle))
    svg = _export(sch, tmp / f"svg_{side}") if not list((tmp / f"svg_{side}").glob("*.svg")) else next((tmp / f"svg_{side}").glob("*.svg"))
    size = 2.54 if side == "big" else 1.27
    drawn = _drawn_union(svg, name, (x, y), size)
    got = next(t.box for t in sheet_overlaps._read(root)[1] if t.text == name)
    horizontal = abs(drawn[2] - drawn[0]) > abs(drawn[3] - drawn[1])
    near, far, cross0, cross1 = (0, 2, 1, 3) if horizontal else (1, 3, 0, 2)
    slack = 0.28 * size   # the far end: the estimated text length (within 0.3 mm or 3 percent) and the frame tail, each a fixed small figure
    # the edge at the anchor is exact, the cross extent within 0.12 text heights of the drawn one
    assert abs(got[cross0] - drawn[cross0]) < 0.1 * size and abs(got[cross1] - drawn[cross1]) < 0.1 * size, (got, drawn)
    at_anchor = (x, y)[0 if horizontal else 1]
    if abs(drawn[near] - at_anchor) < 0.01:                 # the label runs towards +x or +y
        assert abs(got[near] - at_anchor) < 0.01 and abs(got[far] - drawn[far]) < slack, (got, drawn)
    else:                                                   # towards -x or -y
        assert abs(got[far] - at_anchor) < 0.01 and abs(got[near] - drawn[near]) < slack, (got, drawn)


def test_the_direction_of_a_global_or_hierarchical_label_follows_its_stored_justify(label_sheet):
    """The measured direction table: add_labels stores left at 0, 90 and 270 and right at 180 (runs left), and with right stored at
    all four angles the labels run left at 0 and 180 and down at 90 and 270."""
    files, meta, _tmp = label_sheet
    for side, expect in (("left", {0: (1, 0), 90: (0, -1), 180: (-1, 0), 270: (0, -1)}), ("right", {0: (-1, 0), 90: (0, 1), 180: (-1, 0), 270: (0, 1)})):
        _sch, root = files[side]
        boxes = {t.text: t.box for t in sheet_overlaps._read(root)[1]}
        for name, _k, _s, angle, x, y in meta:
            b = boxes[name]
            dx, dy = expect[angle]
            assert (b[0] >= x - 0.01 if dx > 0 else b[2] <= x + 0.01 if dx < 0 else b[1] >= y - 0.01 if dy > 0 else b[3] <= y + 0.01), (name, side, angle, b)


def test_the_frame_of_a_global_label_is_twice_the_text_height_and_a_hierarchical_marker_is_the_text_height(label_sheet):
    files, meta, _tmp = label_sheet
    boxes = {t.text: t.box for t in sheet_overlaps._read(files["left"][1])[1]}
    glob = next(boxes[m[0]] for m in meta if m[1:4] == ("global", "input", 0))
    hier = next(boxes[m[0]] for m in meta if m[1:4] == ("hier", "input", 0))
    assert glob[3] - glob[1] == pytest.approx(2.54) and hier[3] - hier[1] == pytest.approx(1.27)


# --------------------------------------------------------------------------- the character width (step d)


def test_the_character_width_estimate_agrees_with_the_drawn_text_length(tmp_path):
    """textLength of kicad-cli's svg over digits, capitals, small letters, narrow and wide ones, punctuation runs and KiCad markup
    (`{slash}`, `~{x}`, `_{x}`, `^{x}`), at two font sizes: every string within 0.3 mm or 3 percent of the drawn length."""
    sch = _project(tmp_path)
    words = ["0123456789", "ABCDEFGHIJ", "KLMNOPQRST", "UVWXYZ", "abcdefghij", "klmnopqrst", "uvwxyz", "illliii", "WWWMMM", "mmmwww",
             "R_SENSE", "VDD_3V3", "nFAULT", "TEMP_K2", "I2C.SDA", "10k", "1N4148WS", "Cl+_ext", "((((", "----", "++==<>", "@@&&mMW",
             "VPP{slash}MCLR", "I2C_~{RST}", "~{CS}", "A{slash}B", "SDA{slash}SCL{slash}X", "~{RESET}/PA0", "H_{22}O", "x^{2}",
             "{quote}A{lt}B{gt}", "{brace}{comma}{colon}", "a_{b", "{dot}{bslash}",
             "{dollar}5{dollar}", "A{space}B{space}C", "A{tab}B", "AB{dblquote}{backslash}{bar}", "µΩ°±ß", "AäÜ"]
    # a row per label: the svg carries the resolved text for escapes, so the labels are matched by their row, not by their text
    specs = [{"kind": "label", "x": 20.0, "y": 20.0 + 6.0 * i, "text": w, "rotation": 0, "snap": False} for i, w in enumerate(words)]
    assert _tools()["add_labels"](str(sch), specs)["status"] == "ok"

    def rows(svg: Path) -> dict[int, float]:
        return {round((b[3] - 20.0) / 6.0): length for _t, b, length in _svg_texts(svg) if 18.0 < b[3] < 20.0 + 6.0 * len(words) + 2.0 and b[0] < 40.0}

    drawn = rows(_export(sch, tmp_path / "svg"))
    for i, w in enumerate(words):
        est = readability._text_width(w, (1.27, 1.27))
        assert abs(est - drawn[i]) <= max(0.3, 0.03 * drawn[i]), (w, est, drawn[i])
    big = write_layer.load(sch)  # the same labels at font size 2.54, set on the parsed tree and written with the write layer's serialiser
    for node in big.children:
        if not node.is_atom and node.name == "label":
            size = node.find("effects").find("font").find("size")
            size.children[0], size.children[1] = write_layer.number(2.54), write_layer.number(2.54)
    (tmp_path / "big").mkdir()
    big_sch = tmp_path / "big" / "big.kicad_sch"
    write_layer.save(big, big_sch)
    drawn_big = rows(_export(big_sch, tmp_path / "svg_big"))
    for i, w in enumerate(words):
        est = readability._text_width(w, (2.54, 2.54))
        assert abs(est - drawn_big[i]) <= max(0.3, 0.03 * drawn_big[i]), (w, est, drawn_big[i])


def test_a_character_outside_the_table_counts_the_default_advance_a_stated_limit_not_a_measurement():
    """Not a measurement: a character that is neither ASCII nor one of the measured few (here the euro sign) counts
    `_DEFAULT_ADVANCE` of the font size; KiCad's real advance for it was not measured. This pins what the code does."""
    assert readability._DEFAULT_ADVANCE == 1.0 and "€" not in readability._ADVANCE
    assert readability._text_width("€", (1.27, 1.27)) == pytest.approx(1.27)
    assert readability._text_width("A€A", (2.0, 2.0)) == pytest.approx(2.0 * (2 * readability._ADVANCE["A"] + 1.0))
