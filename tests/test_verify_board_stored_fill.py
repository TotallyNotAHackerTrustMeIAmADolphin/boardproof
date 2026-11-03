"""verify_board reports the STORED fill of the original file (issue 4, comparison in issue 34).

The gate refills every zone in a scratch copy, so its verdict is property A (the DRC passes after a
refill). Property B (the stored fill in the original file is correct) is another fact. The gate
reports the part of B it can read from the raw S-expression: a copper zone with no stored
``filled_polygon`` on an intended layer. It is a note and a ``stored_fill`` field, never a reason,
and it never changes the verdict.

Raw counts of the fixtures, taken from the file text (not through kicad_tools), 2026-11-03:
delivered rung-1 boards ``uw/r1fr/out`` and ``uw/r1krt/out``: 5 ``(zone`` nodes, 4 of them
keepout rule areas and 1 the B.Cu GND pour, 0 ``(filled_polygon``; ``golden_attiny1616_board``: 0
zone nodes; ``zone_obstacle``: 1 zone, 1 ``(filled_polygon``.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest
from kicad_tools.sexp import parse_string

from kicad_mcp import verify_board as gate
from kicad_mcp import write_layer

FIXTURES = Path(__file__).parent / "fixtures"
RUNG1 = [FIXTURES / "uw" / "r1fr" / "out", FIXTURES / "uw" / "r1krt" / "out"]
GOLDEN = FIXTURES / "golden_attiny1616_board"
OBSTACLE = FIXTURES / "zone_obstacle"

_CLEAN = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _drc(path, **_kwargs):
    return dict(_CLEAN)


def _pcb(directory: Path) -> Path:
    return next(directory.glob("*.kicad_pcb"))


def _raw_counts(pcb: Path) -> tuple[int, int]:
    text = pcb.read_text(encoding="utf-8")
    return len(re.findall(r"\(zone(?=\s)", text)), len(re.findall(r"\(filled_polygon", text))


def _copy(directory: Path, tmp_path: Path) -> Path:
    shutil.copytree(directory, tmp_path / "board")
    return _pcb(tmp_path / "board")


def _grade(pcb: Path) -> dict:
    return gate.verify_board(pcb, parity="not_applicable", parity_reason="fixture", drc=_drc)


def _stored_notes(result: dict) -> list[str]:
    return [n for n in result["notes"] if n.startswith("stored fill:")]


# --- the reproduction: raw counts, and the verdict beside an empty stored fill ----------------


@pytest.mark.parametrize("directory", RUNG1)
def test_delivered_rung1_boards_store_no_fill_in_the_raw_file(directory: Path) -> None:
    zones, fills = _raw_counts(_pcb(directory))
    assert (zones, fills) == (5, 0)  # 4 keepouts and the B.Cu GND pour


def test_golden_has_no_zone_and_zone_obstacle_stores_its_fill() -> None:
    assert _raw_counts(_pcb(GOLDEN)) == (0, 0)
    assert _raw_counts(_pcb(OBSTACLE)) == (1, 1)


@pytest.mark.parametrize("directory", RUNG1)
def test_an_empty_stored_fill_is_a_note_and_a_field(directory: Path, tmp_path: Path) -> None:
    result = _grade(_copy(directory, tmp_path))

    fill = result["stored_fill"]
    assert fill["status"] == "problems"
    assert [(z["net"], z["status"], z["intended_layers"]) for z in fill["zones"]] == [
        ("GND", "none", ["B.Cu"])
    ]
    notes = _stored_notes(result)
    assert len(notes) == 1 and "net GND" in notes[0] and "no stored fill on B.Cu" in notes[0]


@pytest.mark.parametrize("directory", RUNG1)
def test_the_note_never_changes_the_verdict_or_adds_a_reason(directory: Path, tmp_path: Path) -> None:
    """Same board with the fill in and out: the verdict, reasons and lists are identical."""
    pcb = _copy(directory, tmp_path)
    without = _grade(pcb)
    assert without["verdict"] == "INCOMPLETE"  # no pinned baseline: the only reason is that one
    assert not any("fill" in r for r in without["incomplete_reasons"])
    keys = ("verdict", "incomplete_reasons", "unconnected", "violations", "warnings", "runs")
    assert {k: without[k] for k in keys} == {k: _grade_without_fill_check(pcb)[k] for k in keys}


def _grade_without_fill_check(pcb: Path) -> dict:
    """The same grade with the stored-fill reader replaced by one that finds nothing to say."""
    original = gate.stored_fill_report
    gate.stored_fill_report = lambda _p: {"scope": gate.STORED_FILL_SCOPE, "status": "present", "zones": [], "problems": []}
    try:
        return _grade(pcb)
    finally:
        gate.stored_fill_report = original


def test_the_scope_text_says_a_pass_is_not_the_stored_fill(tmp_path: Path) -> None:
    result = _grade(_copy(OBSTACLE, tmp_path))
    scope = " ".join(result["stored_fill"]["scope"].split())
    assert "does not mean the stored fill is correct" in scope
    assert "kicad-cli refilled every zone in a scratch copy" in scope


# --- the contrast and the boards with nothing to report ----------------------------------------


def test_a_stored_fill_on_the_intended_layer_has_no_note(tmp_path: Path) -> None:
    result = _grade(_copy(OBSTACLE, tmp_path))

    assert result["stored_fill"]["status"] == "present"
    assert result["stored_fill"]["zones"][0]["filled_layers"] == ["F.Cu"]
    assert _stored_notes(result) == []


def test_a_board_without_copper_zones_reports_none_and_no_note(tmp_path: Path) -> None:
    result = _grade(_copy(GOLDEN, tmp_path))

    assert result["stored_fill"]["status"] == "no_copper_zones"
    assert _stored_notes(result) == []


def test_keepout_rule_areas_are_not_copper_zones(tmp_path: Path) -> None:
    zones = gate.stored_fill_report(_copy(RUNG1[0], tmp_path))["zones"]
    assert len(zones) == 1  # 5 zone nodes, 4 are keepouts


# --- mutations of the stored fill, made through the write layer on tmp copies ------------------


def _mutate_zone(pcb: Path, change) -> None:
    root = write_layer.load(pcb)
    zone = next(c for c in root.children if c.name == "zone")
    change(zone)
    write_layer.save(root, pcb)


def test_removing_the_stored_fill_turns_the_zone_to_none(tmp_path: Path) -> None:
    pcb = _copy(OBSTACLE, tmp_path)
    _mutate_zone(pcb, lambda zone: [zone.remove(fp) for fp in zone.find_all("filled_polygon")])
    assert _raw_counts(pcb) == (1, 0)

    result = _grade(pcb)

    assert result["stored_fill"]["zones"][0]["status"] == "none"
    assert len(_stored_notes(result)) == 1


def _make_two_layer(zone) -> None:
    index = list(zone.children).index(zone.find("layer"))
    zone.remove(zone.find("layer"))
    zone.insert(index, parse_string('(layers "F&B.Cu")'))


def test_a_two_layer_zone_needs_a_fill_on_every_intended_layer(tmp_path: Path) -> None:
    """F&B.Cu with fill on F.Cu only is partial: the note names B.Cu."""
    pcb = _copy(OBSTACLE, tmp_path)
    _mutate_zone(pcb, _make_two_layer)

    zone = gate.stored_fill_report(pcb)["zones"][0]

    assert zone["intended_layers"] == ["F.Cu", "B.Cu"]
    assert (zone["status"], zone["missing_layers"]) == ("partial", ["B.Cu"])
    assert "no stored fill on B.Cu" in _stored_notes(_grade(pcb))[0]


def test_a_fill_polygon_without_points_does_not_count(tmp_path: Path) -> None:
    pcb = _copy(OBSTACLE, tmp_path)

    def hollow(zone) -> None:
        for fp in zone.find_all("filled_polygon"):
            pts = fp.find("pts")
            for xy in list(pts.find_all("xy")):
                pts.remove(xy)

    _mutate_zone(pcb, hollow)

    assert gate.stored_fill_report(pcb)["zones"][0]["status"] == "none"


def test_a_fill_without_a_layer_is_undetermined_never_fine(tmp_path: Path) -> None:
    pcb = _copy(OBSTACLE, tmp_path)

    def anonymous(zone) -> None:
        for fp in zone.find_all("filled_polygon"):
            fp.remove(fp.find("layer"))

    _mutate_zone(pcb, anonymous)
    result = _grade(pcb)

    assert result["stored_fill"]["zones"][0]["status"] == "undetermined"
    assert "could not be determined" in _stored_notes(result)[0]


def test_a_zone_layer_that_is_not_copper_gives_no_intended_layer() -> None:
    """KiCad itself crashes on such a file, so the reader is fed the node directly."""
    zone = parse_string('(zone (net 1) (layer "F.Silkscreen"))')

    assert gate._zone_copper_layers(zone, ["F.Cu", "B.Cu"]) == []


def test_a_wildcard_zone_layer_expands_to_the_board_copper_layers() -> None:
    zone = parse_string('(zone (net 1) (layers "*.Cu"))')

    assert gate._zone_copper_layers(zone, ["F.Cu", "In1.Cu", "B.Cu"]) == ["F.Cu", "In1.Cu", "B.Cu"]


def test_an_unreadable_board_is_reported_as_unreadable_not_fine(tmp_path: Path) -> None:
    report = gate.stored_fill_report(tmp_path / "missing.kicad_pcb")

    assert report["status"] == "unreadable"
    assert report["problems"] and "could not be read" in report["problems"][0]


def test_an_incomplete_result_still_carries_the_stored_fill(tmp_path: Path) -> None:
    def failing(path, **_kwargs):
        raise gate.kicad_cli.KicadCliError("boom")

    pcb = _copy(RUNG1[0], tmp_path)
    result = gate.verify_board(pcb, parity="not_applicable", parity_reason="fixture", drc=failing)

    assert result["verdict"] == "INCOMPLETE"
    assert result["stored_fill"]["status"] == "problems"
    assert len(_stored_notes(result)) == 1


# --- real kicad-cli on a delivered board: the verdict beside the empty stored fill -------------


def test_the_real_gate_reports_the_empty_stored_fill_beside_its_verdict(tmp_path: Path) -> None:
    pcb = _copy(RUNG1[0], tmp_path)

    result = gate.verify_board(pcb, parity="not_applicable", parity_reason="fixture")

    assert result["verdict"] in {"PASS", "FAIL", "INCOMPLETE"}
    assert result["stored_fill"]["zones"][0]["status"] == "none"
    assert len(_stored_notes(result)) == 1
    assert _raw_counts(pcb) == (5, 0)  # the gate never wrote the fill into the original


# --- fix round: the description, footprint zones, wording, unexpected shapes -------------------


@pytest.mark.asyncio
async def test_the_tool_description_says_a_pass_is_not_the_stored_fill() -> None:
    from mcp.client.client import Client

    from kicad_mcp.server import build_server

    async with Client(build_server(), mode="auto") as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
    description = " ".join(tools["verify_board"].description.split())

    assert "not the fill stored in the file" in description
    assert "a PASS never means the stored fill is correct" in description
    assert 'a note starting "stored fill:"' in description


def test_the_docstring_says_a_pass_is_not_the_stored_fill() -> None:
    doc = " ".join((gate.verify_board.__doc__ or "").split())

    assert "a PASS never means the stored fill is correct" in doc
    assert "an empty stored fill is a note, never a reason" in doc




def test_a_zone_nested_in_a_footprint_is_examined(tmp_path: Path) -> None:
    pcb = _copy(GOLDEN, tmp_path)
    root = write_layer.load(pcb)
    footprint = next(c for c in root.children if c.name == "footprint")
    footprint.append(parse_string('(zone (net 0) (net_name "") (layer "F.Cu") (uuid "u-nested"))'))
    write_layer.save(root, pcb)

    result = _grade(pcb)

    assert result["stored_fill"]["status"] == "problems"
    assert [z["status"] for z in result["stored_fill"]["zones"]] == ["none"]
    assert len(_stored_notes(result)) == 1


def test_a_nested_zone_with_a_fill_is_present(tmp_path: Path) -> None:
    root = parse_string(
        '(kicad_pcb (footprint "x" (zone (net_name "N") (layer "F.Cu") (uuid "a")'
        ' (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 1 0) (xy 1 1))))))'
    )
    pcb = tmp_path / "n.kicad_pcb"
    # the path is never opened: load is replaced below
    original = gate.write_layer.load
    gate.write_layer.load = lambda _p: root
    try:
        report = gate.stored_fill_report(pcb)
    finally:
        gate.write_layer.load = original
    assert [z["status"] for z in report["zones"]] == ["present"]


def test_a_fill_on_another_layer_is_not_called_outline_only(tmp_path: Path) -> None:
    """F.Cu zone whose only fill is on B.Cu: the note must not say the file holds no fill."""
    pcb = _copy(OBSTACLE, tmp_path)

    def move_fill(zone) -> None:
        for fp in zone.find_all("filled_polygon"):
            write_layer.set_string(fp.find("layer"), 0, "B.Cu")

    _mutate_zone(pcb, move_fill)
    note = _stored_notes(_grade(pcb))[0]

    assert "no stored fill on F.Cu" in note
    assert "outline only" not in note
    assert "B.Cu" in note


def test_a_zone_with_no_fill_anywhere_says_outline_only(tmp_path: Path) -> None:
    note = _stored_notes(_grade(_copy(RUNG1[0], tmp_path)))[0]

    assert "no stored fill on B.Cu" in note and "outline only" in note


def test_an_unexpected_node_shape_is_unreadable_not_an_exception(tmp_path: Path, monkeypatch) -> None:
    def boom(*_a, **_k):
        raise AttributeError("unexpected shape")

    monkeypatch.setattr(gate, "_zone_copper_layers", boom)
    pcb = _copy(OBSTACLE, tmp_path)

    report = gate.stored_fill_report(pcb)
    result = _grade(pcb)

    assert report["status"] == "unreadable" and "could not be read" in report["problems"][0]
    assert result["stored_fill"]["status"] == "unreadable"
    assert len(_stored_notes(result)) == 1
