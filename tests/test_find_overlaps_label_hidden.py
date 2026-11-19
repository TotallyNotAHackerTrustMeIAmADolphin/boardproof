"""find_overlaps kinds label_over_own_symbol and text_hidden_behind_symbol (GitHub issue 222), read-only.

Sheets are built through the project's tools; every position is cross-checked against the raw
``(at ...)`` of the sheet file, never a kicad_tools property. Every direction of the label and every
side of the body that matters is pinned on its own.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest
from mcp.server.mcpserver import MCPServer

from boardproof import kicad_cli, sheet_overlaps, write_layer
from boardproof.toolsets import project as project_toolset
from boardproof.toolsets import schematic as schematic_toolset

LABEL = ["label_over_own_symbol"]
HIDDEN = ["text_hidden_behind_symbol"]
NAME = "LONGNAME"  # 8 characters: a box about 6.1 mm long


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


def _part(tools, sch, ref="R1", x=100.0, y=100.0, rotation=0, **kw):
    spec = {"lib_id": "Device:R", "x": x, "y": y, "ref": ref, "value": "1k", "rotation": rotation, **kw}
    assert tools["add_symbols"](str(sch), [spec])["status"] == "ok"


def _label(tools, sch, x, y, rotation, text=NAME):
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": x, "y": y, "text": text, "rotation": rotation, "snap": False}])["status"] == "ok"


def _find(tools, sch, kinds):
    out = tools["find_overlaps"](str(sch), kinds=kinds)
    assert out["status"] == "ok", out
    return out


def _count(tools, sch, kinds=LABEL):
    return _find(tools, sch, kinds)["found"]


# add_labels writes (justify left bottom) at every angle. KiCad draws a label's text with the stored justify at the stored
# angle, 180 as 0 and 270 as 90, no flip (measured with kicad-cli 10.0.7, test_the_label_box_matches_the_svg_kicad_draws):
# left runs right (angle 0 or 180) or up (90 or 270).
# (label x, y, rotation, runs into the part). Vertical R1 at (100, 100): pins (100, 96.19) and (100, 103.81).
# Horizontal R1 (rotation 90): pins (96.19, 100) and (103.81, 100).
VERTICAL = [("top pin, 270", 100.0, 96.19, 270, False), ("top pin, 90", 100.0, 96.19, 90, False),
            ("bottom pin, 90", 100.0, 103.81, 90, True), ("bottom pin, 270", 100.0, 103.81, 270, True)]
HORIZONTAL = [("left pin, 0", 96.19, 100.0, 0, True), ("left pin, 180", 96.19, 100.0, 180, True),
              ("right pin, 180", 103.81, 100.0, 180, False), ("right pin, 0", 103.81, 100.0, 0, False)]


@pytest.mark.parametrize("case", VERTICAL, ids=[c[0] for c in VERTICAL])
def test_a_label_on_a_pin_of_a_vertical_part_is_reported_only_when_its_text_runs_into_it(tmp_path, case):
    _name, x, y, rotation, runs_in = case
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch)
    _label(tools, sch, x, y, rotation)
    assert _count(tools, sch) == (1 if runs_in else 0)


@pytest.mark.parametrize("case", HORIZONTAL, ids=[c[0] for c in HORIZONTAL])
def test_a_label_on_a_pin_of_a_horizontal_part_is_reported_only_when_its_text_runs_into_it(tmp_path, case):
    _name, x, y, rotation, runs_in = case
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch, rotation=90)
    _label(tools, sch, x, y, rotation)
    assert _count(tools, sch) == (1 if runs_in else 0)


def test_the_finding_names_the_label_the_part_and_the_overlap_and_text_symbol_does_not_repeat_it(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch, rotation=90)
    _label(tools, sch, 96.19, 100.0, 0)
    out = _find(tools, sch, None)
    f = next(g for g in out["findings"] if g["kind"] == "label_over_own_symbol")
    assert f["estimated"] is True and f["sheet"] == "demo.kicad_sch"
    assert [i["kind"] for i in f["items"]] == ["label", "symbol"] and f["items"][0]["text"] == NAME and f["items"][1]["reference"] == "R1"
    raw = write_layer.load(sch)
    label = next(c for c in raw.children if not c.is_atom and c.name == "label")
    at = label.find_children("at")[0]
    assert (at.get_float(0), at.get_float(1)) == (96.19, 100.0) and f["at"] == [96.19, 100.0]   # the raw anchor
    assert f["items"][0]["box"][0] == pytest.approx(96.19)                                       # runs right from the anchor
    px, py = f["overlap"]
    assert px > 6 and 0.5 < py < 1.5
    assert "move_labels" in f["fix"]
    assert out["counts"]["text_symbol"] == 0 and out["counts"]["label_over_own_symbol"] == 1


def test_a_label_on_a_pin_of_another_part_or_off_every_pin_keeps_its_old_kind(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch)
    _label(tools, sch, 100.0, 100.0, 0, "MID")                   # on the body, on no pin: text_symbol, not own
    out = _find(tools, sch, None)
    assert out["counts"]["label_over_own_symbol"] == 0 and out["counts"]["text_symbol"] == 1


def test_a_label_clear_of_its_part_is_not_reported(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch)
    _label(tools, sch, 100.0, 96.19, 90)
    assert _count(tools, sch) == 0


def test_a_label_at_the_pin_of_a_sheet_symbol_whose_text_runs_across_it_is_reported(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    assert tools["create_sheets"](str(sch), [{"file": "a.kicad_sch"}])["status"] == "ok"
    assert tools["add_sheet_symbols"](str(sch), [{"file": "a.kicad_sch", "x": 101.6, "y": 101.6, "pins": [{"name": "X", "direction": "input"}], "name": "A"}])["status"] == "ok"
    root = write_layer.load(sch)
    pin = next(c for c in root.find_children("sheet")[0].children if not c.is_atom and c.name == "pin")
    px, py = pin.find_children("at")[0].get_float(0), pin.find_children("at")[0].get_float(1)
    _label(tools, sch, px, py, 180 if px > 102 else 0, "LONGNAMELONGNAME")
    out = _find(tools, sch, LABEL)
    assert out["found"] == 1 and out["findings"][0]["items"][1]["kind"] == "sheet"


# --------------------------------------------------------------------------- text_hidden_behind_symbol


def test_a_free_text_partly_behind_a_part_is_reported_with_the_covered_share(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch)
    assert tools["add_text"](str(sch), [{"x": 100.0, "y": 100.0, "text": "AB"}], snap=False)["status"] == "ok"
    f = _find(tools, sch, HIDDEN)["findings"][0]
    assert [i["kind"] for i in f["items"]] == ["text", "symbol"] and f["estimated"] is True and f["dnp"] is False
    # raw: R1 at (100 100), library rectangle x +-1.016; the text "AB" runs from its raw (at 100 100) to +1.524
    tb = f["items"][0]["box"]
    assert tb[0] == pytest.approx(100.0) and tb[2] == pytest.approx(101.524)
    assert f["covered_fraction"] == pytest.approx((101.016 - 100.0) / 1.524, abs=0.01)
    assert "move" in f["fix"]


def test_a_text_wholly_inside_the_body_is_covered_in_full(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch)
    assert tools["add_text"](str(sch), [{"x": 99.5, "y": 100.0, "text": "A"}], snap=False)["status"] == "ok"
    f = _find(tools, sch, HIDDEN)["findings"][0]
    assert f["covered_fraction"] == pytest.approx(1.0)


def test_a_reference_of_another_part_behind_a_dnp_part_is_reported_and_says_dnp(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch, "K1", dnp=True)
    _part(tools, sch, "R2", x=130.0, y=100.0)
    assert tools["set_property_positions"](str(sch), [{"references": ["R2"], "properties": {"Reference": {"at": [100.0, 100.0], "angle": 0}}}])["status"] == "ok"
    out = _find(tools, sch, HIDDEN)
    f = next(g for g in out["findings"] if g["items"][0].get("owner_uuid"))
    assert f["items"][0]["property"] == "Reference" and f["items"][0]["text"] == "R2" and f["items"][1]["reference"] == "K1"
    assert f["dnp"] is True


def test_the_same_text_behind_a_part_that_is_not_dnp_says_not_dnp(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch, "K1")
    _part(tools, sch, "R2", x=130.0, y=100.0)
    assert tools["set_property_positions"](str(sch), [{"references": ["R2"], "properties": {"Reference": {"at": [100.0, 100.0], "angle": 0}}}])["status"] == "ok"
    assert _find(tools, sch, HIDDEN)["findings"][0]["dnp"] is False


def test_a_part_s_own_texts_and_labels_are_not_hidden_behind_it(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch)
    assert tools["set_property_positions"](str(sch), [{"references": ["R1"], "properties": {"Value": {"at": [100.0, 100.0], "angle": 0}}}])["status"] == "ok"
    _label(tools, sch, 100.0, 100.0, 0, "MID")
    assert _count(tools, sch, HIDDEN) == 0


def test_a_text_clear_of_the_body_and_a_power_symbol_are_not_reported(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch)
    assert tools["add_text"](str(sch), [{"x": 110.0, "y": 100.0, "text": "FAR"}], snap=False)["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "power", "x": 130.0, "y": 100.0, "text": "power:GND"}])["status"] == "ok"
    assert tools["add_text"](str(sch), [{"x": 129.5, "y": 101.0, "text": "AB"}], snap=False)["status"] == "ok"
    assert _count(tools, sch, HIDDEN) == 0


def test_nothing_is_written_and_the_kinds_can_be_chosen_alone(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch, rotation=90)
    _label(tools, sch, 96.19, 100.0, 0)
    assert tools["add_text"](str(sch), [{"x": 100.0, "y": 100.0, "text": "AB"}], snap=False)["status"] == "ok"
    before = sch.read_bytes()
    both = _find(tools, sch, LABEL + HIDDEN)
    assert both["kinds"] == LABEL + HIDDEN and set(both["counts"]) == set(LABEL + HIDDEN)
    assert both["counts"] == {"label_over_own_symbol": 1, "text_hidden_behind_symbol": 1}
    assert sch.read_bytes() == before


# --------------------------------------------------------------------------- fail closed and pins


def test_a_part_without_geometry_is_a_named_gap_not_a_clean_sheet(tmp_path, monkeypatch):
    tools = _tools()
    sch = _project(tmp_path)
    _part(tools, sch, rotation=90)
    _label(tools, sch, 96.19, 100.0, 0)
    from boardproof import sheet_align

    monkeypatch.setattr(sheet_align, "symbol_box", lambda root, node: None)
    out = _find(tools, sch, LABEL + HIDDEN)
    assert out["found"] == 0 and any(s["reason"] == "no_geometry" and s["reference"] == "R1" for s in out["skipped"])


def test_the_tool_text_pins_both_kinds_and_their_limits():
    doc = " ".join((inspect.getdoc(_tools()["find_overlaps"]) or "").split())
    for kind in ("label_over_own_symbol", "text_hidden_behind_symbol"):
        assert kind in doc and kind in sheet_overlaps.KINDS
    for phrase in ("on a pin of the symbol", "text runs into", "covered_fraction", "dnp", "also reported as text_symbol",
                   "not a label", "own texts are not read", "stored justification and angle"):
        assert phrase in doc, phrase


def test_the_schematic_skill_names_both_kinds():
    skill = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "kicad-schematic" / "SKILL.md"
    line = next(ln for ln in skill.read_text(encoding="utf-8").splitlines() if ln.startswith("11. "))
    for phrase in ("label_over_own_symbol", "text_hidden_behind_symbol", "DNP", "a label on a pin of its own part", "also listed as `text_symbol`"):
        assert phrase in line, phrase


# --------------------------------------------------------------------------- the justify rule (review of issue 222)


def _expected_runs_in(angle: int, justify: str, pin: str) -> bool:
    """KiCad draws the stored justify at the stored angle, 180 as 0 and 270 as 90: left runs right or up, right runs left or down."""
    if angle in (0, 180):
        return justify == "left" if pin == "L" else justify == "right"   # pin L: the part lies to the right of the anchor
    return justify == "left" if pin == "B" else justify == "right"       # pin B: the part lies above the anchor


@pytest.fixture(scope="module")
def justify_sheet(tmp_path_factory):
    """Eight parts, a label on each of their two pins at the part's angle, then (in memory only, never written) the justify of
    the 'right' ones swapped from the add_labels default 'left bottom' to the KiCad-native 'right bottom'."""
    tools = _tools()
    sch = _project(tmp_path_factory.mktemp("justify"))
    plan = [(0, "left"), (0, "right"), (180, "left"), (180, "right")]
    parts, labels = [], []
    for i, (angle, justify) in enumerate(plan):
        x = 30.0 + 30 * i
        parts += [{"lib_id": "Device:R", "x": x, "y": 100.0, "ref": f"RH{i}", "value": "1k", "rotation": 90},
                  {"lib_id": "Device:R", "x": x, "y": 50.0, "ref": f"RV{i}", "value": "1k", "rotation": 0}]
        for pin, (px, py) in (("L", (x - 3.81, 100.0)), ("R", (x + 3.81, 100.0))):
            labels.append((f"H{i}{pin}", px, py, angle, justify, _expected_runs_in(angle, justify, pin)))
        for pin, (px, py) in (("T", (x, 46.19)), ("B", (x, 53.81))):
            labels.append((f"V{i}{pin}", px, py, angle + 90, justify, _expected_runs_in(angle + 90, justify, pin)))
    assert tools["add_symbols"](str(sch), parts)["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": x, "y": y, "text": n, "rotation": a, "snap": False} for n, x, y, a, _j, _e in labels])["status"] == "ok"
    root = write_layer.load(sch)
    nodes = {c.get_string(0): c for c in root.children if not c.is_atom and c.name == "label"}
    for n, _x, _y, _a, justify, _e in labels:
        assert nodes[n].find_children("effects")[0].find_children("justify")[0].children[0].value == "left"   # the add_labels default
        if justify == "right":
            nodes[n].find_children("effects")[0].find_children("justify")[0].children[0] = write_layer.symbol("right")
    found, _skipped = sheet_overlaps._findings("demo.kicad_sch", root, sheet_overlaps.FRAME_MARGIN_MM, False)
    return {f["items"][0]["text"] for f in found if f["kind"] == "label_over_own_symbol"}, labels


@pytest.mark.parametrize("name", [f"{k}{i}{p}" for i in range(4) for k, ps in (("H", "LR"), ("V", "TB")) for p in ps])
def test_the_stored_justify_decides_which_way_a_label_runs_at_every_angle_and_pin(justify_sheet, name):
    got, labels = justify_sheet
    angle, justify, expected = next((a, j, e) for n, _x, _y, a, j, e in labels if n == name)
    assert (name in got) is expected, (name, angle, justify)


def test_the_justify_matrix_has_cases_on_both_sides_of_every_angle(justify_sheet):
    _got, labels = justify_sheet
    assert {(a % 360, j, e) for _n, _x, _y, a, j, e in labels} == {(a, j, e) for a in (0, 90, 180, 270) for j in ("left", "right") for e in (True, False)}


def test_the_label_box_matches_the_svg_kicad_draws(tmp_path):
    """kicad-cli 10.0.7: a left-justified label at 0 or 180 starts at its anchor and runs right, at 90 or 270 it starts there and runs up (rotate -90)."""
    import re
    import subprocess

    tools = _tools()
    sch = _project(tmp_path)
    spots = {0: (50.0, 50.0), 90: (100.0, 50.0), 180: (150.0, 50.0), 270: (200.0, 50.0)}
    assert tools["add_labels"](str(sch), [{"kind": "label", "x": x, "y": y, "text": "LONGNAME", "rotation": a, "snap": False} for a, (x, y) in spots.items()])["status"] == "ok"
    r = subprocess.run([str(kicad_cli.find_kicad_cli()), "sch", "export", "svg", str(sch), "-o", str(tmp_path / "svg")], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    svg = next((tmp_path / "svg").glob("*.svg")).read_text(encoding="utf-8")
    drawn = []
    for m in re.finditer(r'(<g transform="rotate\(-90[^"]*"\s*>\s*)?<text x="([\d.]+)" y="([\d.]+)"[^>]*text-anchor="(\w+)"[^>]*>LONGNAME', svg):
        drawn.append((m.group(1) is not None, float(m.group(2)), float(m.group(3)), m.group(4)))
    assert len(drawn) == 4 and all(d[3] == "start" for d in drawn)
    root = write_layer.load(sch)
    _bodies, texts, *_ = sheet_overlaps._read(root)
    for text in texts:
        angle = next(a for a, xy in spots.items() if xy == text.at)
        vertical, tx, ty, _anchor = next(d for d in drawn if abs(d[1] - text.at[0]) < 1.0 and abs(d[2] - text.at[1]) < 1.0)
        assert vertical is (angle in (90, 270))
        if vertical:       # starts at the anchor and runs up: the box ends at the anchor row and lies to its left
            assert text.box[3] == pytest.approx(text.at[1]) and text.box[1] < text.at[1] - 3 and text.box[2] == pytest.approx(text.at[0])
        else:              # starts at the anchor and runs right: the box begins at the anchor column and lies above it
            assert text.box[0] == pytest.approx(text.at[0]) and text.box[2] > text.at[0] + 3 and text.box[3] == pytest.approx(text.at[1])


def test_the_tool_text_states_the_justify_rule_the_double_count_and_the_rotated_symbol_limit():
    doc = " ".join((inspect.getdoc(_tools()["find_overlaps"]) or "").split())
    for phrase in ("180 is drawn as 0 and 270 as 90", "left runs right or up", "right runs left or down",
                   "max_findings count such a pair under both kinds",
                   "turned 90 or 270 degrees"):
        assert phrase in doc, phrase
    skill = Path(__file__).resolve().parents[1] / ".claude" / "skills" / "kicad-schematic" / "SKILL.md"
    line = next(ln for ln in skill.read_text(encoding="utf-8").splitlines() if ln.startswith("11. "))
    assert "180 drawn as 0 and 270 as 90" in line


def test_global_and_hierarchical_labels_on_a_pin_are_attached_like_local_ones(tmp_path):
    tools = _tools()
    sch = _project(tmp_path)
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 100.0, "y": 100.0, "ref": "R1", "value": "1k", "rotation": 90},
                                           {"lib_id": "Device:R", "x": 150.0, "y": 100.0, "ref": "R2", "value": "1k", "rotation": 90}])["status"] == "ok"
    assert tools["add_labels"](str(sch), [{"kind": "global", "x": 96.19, "y": 100.0, "text": "GLOBAL_NAME_X", "rotation": 0, "snap": False},
                                          {"kind": "hier", "x": 146.19, "y": 100.0, "text": "HIER_NAME_X", "rotation": 0, "snap": False}])["status"] == "ok"
    out = _find(tools, sch, LABEL)
    assert sorted(f["items"][0]["text"] for f in out["findings"]) == ["GLOBAL_NAME_X", "HIER_NAME_X"]
    assert out["counts"] == {"label_over_own_symbol": 2}
