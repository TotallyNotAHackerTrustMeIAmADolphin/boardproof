"""Issue 266: the box find_overlaps gives a local label follows the stored justify (both words) at every angle, as kicad-cli draws it.

Measured with kicad-cli 10.0.7 ``sch export svg`` on one sheet of labels, angle 0, 90, 180, 270 times nine justify forms: the text
anchor says which way the text runs from the anchor (start runs right or up, end runs left or down, middle is centred on it) and the
baseline offset says which side of the anchor it lies on (bottom above or, turned 90, left; top below or right; none centred).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_overlaps, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

JUSTIFY = ["left bottom", "right bottom", "left top", "right top", "left", "right", "bottom", "top", ""]
ANGLES = (0, 90, 180, 270)


@pytest.fixture(scope="module")
def sheet(tmp_path_factory):
    try:
        cli = kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")
    tmp = tmp_path_factory.mktemp("justify266")
    mcp = MCPServer("test")
    schematic_toolset.build(mcp)
    tools = {t.name: t.fn for t in mcp._tool_manager.list_tools()}  # type: ignore[attr-defined]
    sch = Path(project_toolset._project_create(str(tmp / "proj"), "demo")["schematic_path"])
    cases = [(f"L{a}J{j}", 30 + j * 20, 30 + i * 30, a, w) for i, a in enumerate(ANGLES) for j, w in enumerate(JUSTIFY)]
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": x, "y": y, "text": n, "rotation": a, "snap": False} for n, x, y, a, _w in cases])["status"] == "ok"
    root = write_layer.load(sch)
    nodes = {c.get_string(0): c for c in root.children if not c.is_atom and c.name == "label"}
    for n, _x, _y, _a, words in cases:  # the stored justify is changed in memory and written by the write layer's serialiser
        effects = nodes[n].find_children("effects")[0]
        old = effects.find_children("justify")
        for o in old:
            effects.children.remove(o)
        if words:
            effects.children.insert(1, type(old[0]).list("justify", *(write_layer.symbol(w) for w in words.split())))
    write_layer.save(root, sch)
    r = subprocess.run([str(cli), "sch", "export", "svg", str(sch), "-o", str(tmp / "svg")], capture_output=True, text=True, timeout=120, check=False)
    assert r.returncode == 0, r.stderr
    svg = next((tmp / "svg").glob("*.svg")).read_text(encoding="utf-8")
    drawn = {}
    for m in re.finditer(r'(<g transform="rotate\(-90[^"]*"\s*>\s*)?<text x="([\d.-]+)" y="([\d.-]+)"([^>]*)>(L\d+J\d)', svg):
        attrs = m.group(4)
        drawn[m.group(5)] = (m.group(1) is not None, float(m.group(2)), float(m.group(3)), re.search(r'text-anchor="(\w+)"', attrs).group(1),
                             float(re.search(r'textLength="([\d.]+)"', attrs).group(1)),
                             float(re.search(r'rotate\(-90[\d.]*\s+[\d.-]+\s+([\d.-]+)\)', m.group(1)).group(1)) if m.group(1) else 0.0)
    boxes = {t.text: t.box for t in sheet_overlaps._read(write_layer.load(sch))[1]}
    assert len(drawn) == len(cases) and len(boxes) == len(cases)
    return cases, drawn, boxes, sch


def _case(sheet, name):
    cases, drawn, boxes, _sch = sheet
    _n, x, y, angle, words = next(c for c in cases if c[0] == name)
    return x, y, angle, words, drawn[name], boxes[name]


@pytest.mark.parametrize("name", [f"L{a}J{j}" for a in ANGLES for j in range(len(JUSTIFY))])
def test_the_label_box_lies_where_kicad_draws_the_text(sheet, name):
    x, y, angle, words, (vertical, _sx, sy, anchor, _length, _pivot_y), box = _case(sheet, name)
    assert vertical is (angle in (90, 270))
    off = sy - y  # the baseline offset: bottom -0.35 (0 turned), none 0.29 (0.64 turned), top 0.92 (1.27 turned)
    side = "bottom" if off < (0.3 if vertical else 0.0) else "top" if off > (1.0 if vertical else 0.6) else "centre"
    assert side == next((w for w in ("bottom", "top") if w in words.split()), "centre")
    # along the text: start runs right (turned: up), end runs left (turned: down), middle is centred on the anchor
    lo, hi, a0 = (box[1], box[3], y) if vertical else (box[0], box[2], x)
    if anchor == "start":
        assert (hi if vertical else lo) == pytest.approx(a0)
    elif anchor == "end":
        assert (lo if vertical else hi) == pytest.approx(a0)
    else:
        assert (lo + hi) / 2 == pytest.approx(a0)
    # across the text: the text frame's up is the page's up, turned 90 the page's left
    c0, c1, p0 = (box[0], box[2], x) if vertical else (box[1], box[3], y)
    if side == "bottom":
        assert c1 == pytest.approx(p0)
    elif side == "top":
        assert c0 == pytest.approx(p0)
    else:
        assert (c0 + c1) / 2 == pytest.approx(p0)
