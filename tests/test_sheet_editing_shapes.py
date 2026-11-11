"""Input shapes of the move_* / delete_* / set_* sheet tools (GitHub issue 188).

An angle-only move_labels item passed validation and then raised a raw TypeError. These tests feed
every move, delete and set tool the odd shapes an agent can send (an item with only optional keys, a
key present with None, a key of the wrong type, a list where a string belongs, NaN and inf) and
demand a structured refusal with the file bytes unchanged, never an exception. One sheet is built
once per module; refusals do not change it, and the tests that write work on a copy.
"""

from __future__ import annotations

import math
import shutil
from pathlib import Path

import pytest

from boardproof import kicad_cli, sheet_editing, write_layer
from boardproof.toolsets import project as project_toolset

from tests.test_sheet_editing import _tools, _wire

NAN = float("nan")
INF = float("inf")


@pytest.fixture(autouse=True)
def _need_kicad_cli():
    try:
        kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001
        pytest.skip("kicad-cli not installed")


@pytest.fixture(scope="module")
def tools() -> dict:
    return _tools()


@pytest.fixture(scope="module")
def shared(tmp_path_factory, tools) -> Path:
    """R1 at (50.8, 50.8) with a wire stub on pin 1 labelled NETA at its end, and a label OFFGRID that
    sits off the 1.27 mm grid. Used read-only: every test that writes copies it first."""
    made = project_toolset._project_create(str(tmp_path_factory.mktemp("shapes") / "proj"), "demo")
    sch = Path(made["schematic_path"])
    assert tools["add_symbols"](str(sch), [{"lib_id": "Device:R", "x": 50.8, "y": 50.8, "ref": "R1", "value": "10k"}])["status"] == "ok"
    assert tools["add_wires"](str(sch), [_wire((50.8, 46.99), (50.8, 40.64))])["status"] == "ok"
    assert tools["add_labels"](str(sch), [
        {"kind": "label", "x": 50.8, "y": 40.64, "text": "NETA", "snap": False},
        {"kind": "label", "x": 80.1, "y": 70.3, "text": "OFFGRID", "snap": False},
    ])["status"] == "ok"
    return sch


@pytest.fixture
def copy(shared, tmp_path) -> Path:
    target = tmp_path / "proj"
    shutil.copytree(shared.parent, target)
    return target / shared.name


def _label(sch: Path, text: str):
    for node in write_layer.load(sch).children:
        if not node.is_atom and node.name == "label" and node.get_string(0) == text:
            x, y, a = (float(v.value) for v in node.find_children("at")[0].children[:3])
            return x, y, a
    raise AssertionError(text)


# ------------------------------------------------------------------ the reported crash


def test_an_angle_only_label_move_sets_the_angle_and_leaves_the_position_alone(tools, copy):
    before = copy.read_bytes()
    dry = tools["move_labels"](str(copy), [{"texts": ["OFFGRID"], "angle": 90}], dry_run=True)
    assert dry["ok"] is True and dry["written"] is False
    assert dry["would_move"][0]["angle"] == 90 and dry["would_move"][0]["from"] == dry["would_move"][0]["to"]
    assert copy.read_bytes() == before
    done = tools["move_labels"](str(copy), [{"texts": ["OFFGRID"], "angle": 90}])
    assert done["ok"] is True and done["written"] is True, done
    # the label stays exactly where it was (80.1, 70.3 is off the grid: no snapping on an angle-only item)
    assert _label(copy, "OFFGRID") == (80.1, 70.3, 90.0)
    assert _label(copy, "NETA") == (50.8, 40.64, 0.0)


def test_an_angle_only_item_on_a_label_picked_by_pattern_turns_every_match(tools, copy):
    done = tools["move_labels"](str(copy), [{"pattern": "*", "angle": 180}])
    assert done["ok"] is True and done["count"] == 2, done
    assert _label(copy, "OFFGRID")[2] == 180.0 and _label(copy, "NETA")[2] == 180.0


@pytest.mark.parametrize("item", [
    {"texts": ["NETA"], "angle": 45},
    {"texts": ["NETA"], "angle": "90"},
    {"texts": ["NETA"], "angle": [90]},
    {"texts": ["NETA"], "angle": True},
    {"texts": ["NETA"], "angle": False},
    {"texts": ["NETA"], "angle": NAN},
    {"texts": ["NETA"], "angle": None},
    {"texts": ["NETA"], "to": None, "offset": None},
    {"texts": ["NETA"], "to": None, "offset": None, "angle": None},
    {"texts": ["NETA"]},
    {"texts": ["NETA"], "to": [1, 1], "offset": [1, 1]},
    {"texts": ["NETA"], "to": "ab"},
    {"texts": ["NETA"], "offset": [NAN, 1]},
    {"texts": ["NETA"], "offset": [INF, 1]},
    {"texts": ["NETA"], "to": [1e308, 1]},
    {"texts": ["NETA"], "to": [None, None]},
    {},
])
def test_odd_move_items_are_refused_in_a_structured_way_with_the_file_unchanged(tools, shared, item):
    before = shared.read_bytes()
    result = tools["move_labels"](str(shared), [item])
    assert result["ok"] is False and result["code"] in ("invalid_spec", "selector_matched_nothing"), result
    assert shared.read_bytes() == before


def test_an_angle_on_a_symbol_move_is_refused_with_invalid_spec(tools, shared):
    before = shared.read_bytes()
    for item in ({"references": ["R1"], "angle": 90}, {"references": ["R1"]}, {"references": ["R1"], "offset": [NAN, 0]}):
        result = tools["move_symbols"](str(shared), [item])
        assert result["ok"] is False and result["code"] == "invalid_spec", (item, result)
    assert shared.read_bytes() == before


@pytest.mark.parametrize("kwargs", [{"move_wires": "x"}, {"snap": None}, {"grid": NAN}, {"grid": "a"}, {"grid": 0}, {"grid": INF}])
def test_odd_move_flags_are_refused(tools, shared, kwargs):
    before = shared.read_bytes()
    result = tools["move_labels"](str(shared), [{"texts": ["NETA"], "offset": [1, 0]}], **kwargs)
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert shared.read_bytes() == before


# ------------------------------------------------------------------ selector shapes


_BAD_SELECTORS = [
    {"texts": [[1]]},
    {"texts": [None]},
    {"texts": [1]},
    {"texts": [{}]},
    {"uuids": [[1]]},
    {"uuids": [None]},
    {"references": [[1]]},
    {"references": [None]},
    {"kind": ["label"]},
    {"kind": {}},
    {"kind": None},
    {"pattern": 3},
    {"pattern": ["a"]},
    {"region": "abcd"},
    {"region": [NAN, 0, 1, 1]},
    {"region": [0, 0, INF, 1]},
    {"region": [None] * 4},
    {"region": [1]},
    {"points": [[1]]},
    {"points": [["a", "b"]]},
    {"points": [[NAN, 1]]},
    {"points": ["ab"]},
    {"points": [None]},
    {"net": 3},
    {"net": ["a"]},
    None,
    "abc",
    {},
]


@pytest.mark.parametrize("tool", ["delete_symbols", "delete_labels", "delete_wires", "delete_junctions", "delete_no_connects"])
def test_every_delete_tool_refuses_odd_selectors_without_raising(tools, shared, tool):
    before = shared.read_bytes()
    for selector in _BAD_SELECTORS:
        for given in ([selector], selector):
            result = tools[tool](str(shared), given)
            assert result["ok"] is False and result["code"] == "invalid_spec", (tool, given, result)
    assert shared.read_bytes() == before


def test_a_selector_key_the_kind_does_not_know_is_still_invalid_spec(tools, shared):
    result = tools["delete_labels"](str(shared), [{"references": ["R1"]}])
    assert result["ok"] is False and result["code"] == "invalid_spec"


@pytest.mark.parametrize("tool,key", [("move_labels", "texts"), ("move_symbols", "references")])
def test_move_tools_refuse_odd_selectors(tools, shared, tool, key):  # noqa: ARG001
    before = shared.read_bytes()
    for selector in _BAD_SELECTORS:
        item = {**selector, "offset": [1, 0]} if isinstance(selector, dict) else selector
        result = tools[tool](str(shared), [item])
        assert result["ok"] is False and result["code"] == "invalid_spec", (tool, selector, result)
    assert shared.read_bytes() == before


# ------------------------------------------------------------------ set_* tools


@pytest.mark.parametrize("change", [
    {"offset": [NAN, 0]}, {"at": [INF, 0]}, {"at": [-INF, 0]}, {"offset": [1e308, 0]}, {"at": [None, 1]},
    {"angle": "90"}, {"angle": [1]}, {"angle": NAN}, {"angle": True}, {"angle": False}, {"justify": None}, {"justify": 3}, {"justify": ["left"]}, None, [], "x",
])
def test_set_property_positions_refuses_odd_changes(tools, shared, change):
    before = shared.read_bytes()
    result = tools["set_property_positions"](str(shared), [{"references": ["R1"], "properties": {"Value": change}}])
    assert result["ok"] is False and result["code"] == "invalid_spec", result
    assert shared.read_bytes() == before


@pytest.mark.parametrize("item", [
    {"references": None, "properties": {"Value": {"angle": 0}}},
    {"references": ["R1"], "properties": [1]},
    {"references": ["R1"], "properties": None},
    {"references": [[1]], "properties": {"Value": {"angle": 0}}},
    {"references": ["R1"], "properties": {1: {"angle": 0}}},
    None, 5,
])
def test_set_property_positions_refuses_odd_items(tools, shared, item):
    before = shared.read_bytes()
    result = tools["set_property_positions"](str(shared), [item])
    assert result["ok"] is False and result["code"] in ("invalid_spec", "property_not_found"), result
    assert shared.read_bytes() == before


@pytest.mark.parametrize("item", [
    {"uuid": None, "text": "a"},
    {"uuid": 3, "text": "a"},
    {"uuid": [1], "text": "a"},
    {"kind": ["label"], "old_text": "NETA", "text": "a"},
    {"kind": {}, "old_text": "NETA", "text": "a"},
    {"kind": "label", "old_text": "NETA", "text": None},
    {"kind": "label", "old_text": "NETA", "text": 3},
    {"kind": "label", "old_text": 3, "text": "a"},
    {"kind": "label", "old_text": ["NETA"], "text": "a"},
    {"kind": "label", "old_text": "NETA", "point": [NAN, 1], "text": "a"},
    {"kind": "label", "old_text": "NETA", "point": "ab", "text": "a"},
    {"kind": "label", "old_text": "NETA", "point": None, "text": "a"},
    {"kind": "label", "old_text": "NETA", "point": [None, None], "text": "a"},
    {"kind": "label", "old_text": "NETA"},
    {"text": "a"},
    {}, None, 1,
])
def test_set_text_refuses_odd_items(tools, shared, item):
    before = shared.read_bytes()
    result = tools["set_text"](str(shared), [item])
    assert result["ok"] is False and result["code"] in ("invalid_spec", "empty_text", "not_found"), result
    assert shared.read_bytes() == before


@pytest.mark.parametrize("tool,args", [
    ("replace_symbols", [None]), ("replace_symbols", [{}]), ("replace_symbols", [{"references": 3, "lib_id": "Device:C"}]),
    ("replace_symbols", [{"references": [[1]], "lib_id": "Device:C"}]), ("replace_symbols", [{"references": ["R1"], "lib_id": ["Device:C"]}]),
    ("set_properties", [{"references": ["R1"], "properties": {"Value": None}}]),
    ("set_properties", [{"references": ["R1"], "properties": {"Value": NAN}}]),
    ("set_properties", [{"references": ["R1"], "properties": {1: "a"}}]),
    ("set_properties", [{"references": 3, "properties": {"Value": "a"}}]),
    ("set_properties", [{"references": [[1]], "properties": {"Value": "a"}}]),
])
def test_replace_and_set_properties_refuse_odd_items(tools, shared, tool, args):
    before = shared.read_bytes()
    result = tools[tool](str(shared), args)
    assert result["ok"] is False and isinstance(result.get("code"), str), result
    assert shared.read_bytes() == before


# ------------------------------------------------------------------ the run function


def test_a_kicad_cli_failure_in_the_netlist_is_a_structured_refusal(tools, copy, monkeypatch):
    before = copy.read_bytes()

    def broken(*_a, **_k):
        raise kicad_cli.KicadCliError("kicad-cli sch export netlist failed (simulated)")

    monkeypatch.setattr(kicad_cli, "export_netlist", broken)
    result = tools["move_labels"](str(copy), [{"texts": ["NETA"], "offset": [2.54, 0]}])
    assert result["ok"] is False and result["code"] == "kicad_cli_failed" and "simulated" in result["reason"], result
    assert result["written"] is False
    assert copy.read_bytes() == before


def test_a_kicad_cli_failure_in_the_erc_census_is_a_structured_refusal(tools, copy, monkeypatch):
    before = copy.read_bytes()

    def broken(*_a, **_k):
        raise kicad_cli.KicadCliError("kicad-cli sch erc failed (simulated)")

    monkeypatch.setattr(kicad_cli, "run_erc", broken)
    result = tools["add_text"](str(copy), [{"x": 10.16, "y": 10.16, "text": "note"}])
    assert result["ok"] is False and result["code"] == "kicad_cli_failed", result
    assert result["written"] is False
    assert copy.read_bytes() == before


def test_the_finite_catch_does_not_depend_on_the_message_text(copy, monkeypatch):
    real = write_layer.NonFiniteNumberError

    def reworded(value, *a, **k):
        raise real("completely different words")

    monkeypatch.setattr(write_layer, "format_number", reworded)

    def mutate(sheet, root):
        sheet_editing._set_at(sheet.labels[0], NAN, 1.0)
        return sheet_editing.Outcome(check_nets=False)

    result = sheet_editing.run(str(copy), None, mutate, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is False and result["code"] == "invalid_spec", result


def test_an_integral_float_angle_is_echoed_as_an_int(tools, copy):
    done = tools["move_labels"](str(copy), [{"texts": ["NETA"], "angle": 90.0}], dry_run=True)
    assert done["would_move"][0]["angle"] == 90 and isinstance(done["would_move"][0]["angle"], int)


@pytest.mark.parametrize("kind", ["global", "hier"])
def test_an_angle_only_move_turns_global_and_hierarchical_labels_without_touching_connectivity(tools, copy, kind):
    assert tools["add_wires"](str(copy), [_wire((60.96, 60.96), (71.12, 60.96))])["status"] == "ok"
    assert tools["add_labels"](str(copy), [{"kind": kind, "x": 71.12, "y": 60.96, "text": "XLBL", "snap": False}])["status"] == "ok"
    node = {"global": "global_label", "hier": "hierarchical_label"}[kind]
    done = tools["move_labels"](str(copy), [{"texts": ["XLBL"], "angle": 90}])
    assert done["ok"] is True and done["written"] is True and done["connectivity"]["changed"] is False, done
    for n in write_layer.load(copy).children:
        if not n.is_atom and n.name == node and n.get_string(0) == "XLBL":
            x, y, a = (float(v.value) for v in n.find_children("at")[0].children[:3])
            assert (x, y, a) == (71.12, 60.96, 90.0)
            return
    raise AssertionError("label missing")


def test_a_non_finite_number_set_inside_a_mutation_is_a_structured_refusal(copy):
    before = copy.read_bytes()

    def mutate(sheet, root):
        sheet_editing._set_at(sheet.labels[0], NAN, 1.0)
        return sheet_editing.Outcome(check_nets=False)

    result = sheet_editing.run(str(copy), None, mutate, allow_connectivity_change=False, dry_run=False)
    assert result["ok"] is False and result["code"] == "invalid_spec" and "finite" in result["reason"], result
    assert math.isfinite(_label(copy, "NETA")[0])
    assert copy.read_bytes() == before


@pytest.mark.parametrize("point", [[NAN, 1], [INF, 1], [1e308, 1]])
def test_set_text_point_must_be_finite_and_on_the_page(tools, shared, point):
    result = tools["set_text"](str(shared), [{"kind": "label", "old_text": "NETA", "point": point, "text": "a"}])
    assert result["ok"] is False and result["code"] == "invalid_spec", result


def test_the_move_labels_text_says_an_angle_alone_turns_the_label_in_place(tools):
    text = " ".join((tools["move_labels"].__doc__ or "").split())
    assert "An item with `angle` and neither `to` nor `offset` only turns the label where it is (position unchanged, not snapped" in text
    assert "refused with invalid_spec and nothing is written" in text
