"""verify_board compares the stored fill with a fresh refill, per zone (issue 101, property B of issue 34).

The gate refilled every zone in a scratch copy but never compared the stored ``filled_polygon``
entries of the original with the refilled ones, so a stale stored fill got the verdict of a correct
one. ``stored_fill["comparison"]`` now reports ``matches`` or ``stale`` per zone, read from the raw
S-expressions, within a stated tolerance. It is a note: it never changes the verdict (the owner may
decide later that it should).

Fixtures (all through the write layer, on ``tmp_path`` copies; never hand-edited):
- issue 34 fixture 3: ``zone_obstacle`` with the zone outline moved 1 mm without a refill.
- tolerance cases: the stored fill shifted by 0.01 mm (inside) and by 0.5 mm (outside).
- issue 34 fixture 2: ``uw/r1fr/out`` (stored fill empty) -> ``none``.
- a correct board: ``zone_obstacle`` as shipped -> ``matches``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


from kicad_mcp import verify_board as gate
from kicad_mcp import write_layer

FIXTURES = Path(__file__).parent / "fixtures"
OBSTACLE = FIXTURES / "zone_obstacle"
R1FR = FIXTURES / "uw" / "r1fr" / "out"

_CLEAN = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}


def _drc(path, **_kwargs):
    return dict(_CLEAN)


def _copy(directory: Path, tmp_path: Path) -> Path:
    shutil.copytree(directory, tmp_path / "board")
    return next((tmp_path / "board").glob("*.kicad_pcb"))


def _shift_xy(nodes, dx: float) -> None:
    for xy in nodes:
        write_layer.set_number(xy, 0, float(xy.children[0].value) + dx)


def _move_outline(pcb: Path, dx: float) -> None:
    """Fixture 3: move the zone outline (its ``(polygon ...)``), keep the stored fill."""
    root = write_layer.load(pcb)
    for zone in root.find_all("zone"):
        _shift_xy(zone.find("polygon").find("pts").find_all("xy"), dx)
    write_layer.save(root, pcb)


def _shift_stored_fill(pcb: Path, dx: float) -> None:
    root = write_layer.load(pcb)
    for zone in root.find_all("zone"):
        for fp in zone.find_all("filled_polygon"):
            _shift_xy(fp.find("pts").find_all("xy"), dx)
    write_layer.save(root, pcb)


def _grade(pcb: Path, **kwargs) -> dict:
    return gate.verify_board(
        pcb, parity="not_applicable", parity_reason="fixture", drc=_drc,
        refill=gate.kicad_refill_in_place, **kwargs,
    )


def _comparison(result: dict) -> dict:
    return result["stored_fill"]["comparison"]


def _states(result: dict) -> list[str]:
    return [z["comparison"] for z in _comparison(result)["zones"]]


# --- the acceptance: fixture 3 stale, fixture 2 none, a correct board matches -----------------


def test_a_correct_board_matches(tmp_path: Path) -> None:
    result = _grade(_copy(OBSTACLE, tmp_path))
    assert _comparison(result)["status"] == "matches"
    assert _states(result) == ["matches"]
    assert not [n for n in result["notes"] if "is stale" in n]


def test_fixture_3_a_zone_moved_without_a_refill_is_stale(tmp_path: Path) -> None:
    pcb = _copy(OBSTACLE, tmp_path)
    _move_outline(pcb, 1.0)
    result = _grade(pcb)

    assert _comparison(result)["status"] == "stale"
    assert _states(result) == ["stale"]
    zone = _comparison(result)["zones"][0]
    assert any("F.Cu" in d for d in zone["differences"])
    stale = [n for n in result["notes"] if "is stale" in n]
    assert len(stale) == 1 and stale[0].startswith("stored fill:")


def test_fixture_2_a_stored_empty_fill_is_none_and_needs_no_refill() -> None:
    def never(_path):
        raise AssertionError("nothing is stored, so no refill may run")

    result = gate.verify_board(
        next(R1FR.glob("*.kicad_pcb")), parity="not_applicable", parity_reason="fixture",
        drc=_drc, refill=never,
    )
    assert _states(result) == ["none"]
    assert _comparison(result)["status"] == "partly_compared"  # nothing stored to compare


def test_stale_never_changes_the_verdict(tmp_path: Path) -> None:
    good = _grade(_copy(OBSTACLE, tmp_path / "good"))
    pcb = _copy(OBSTACLE, tmp_path / "stale")
    _move_outline(pcb, 1.0)
    stale = _grade(pcb)

    assert _comparison(stale)["status"] == "stale"
    for key in ("verdict", "incomplete_reasons", "violations", "unconnected"):
        assert stale[key] == good[key], key
    assert "never changes the verdict" in _comparison(stale)["verdict_effect"]


# --- the tolerance, inside and outside ---------------------------------------------------------


def test_a_shift_inside_the_tolerance_matches(tmp_path: Path) -> None:
    pcb = _copy(OBSTACLE, tmp_path)
    _shift_stored_fill(pcb, 0.01)
    assert _states(_grade(pcb)) == ["matches"]


def test_a_shift_outside_the_tolerance_is_stale(tmp_path: Path) -> None:
    pcb = _copy(OBSTACLE, tmp_path)
    _shift_stored_fill(pcb, 0.5)
    assert _states(_grade(pcb)) == ["stale"]


def test_the_output_states_the_tolerance_and_what_it_did_not_check(tmp_path: Path) -> None:
    comparison = _comparison(_grade(_copy(OBSTACLE, tmp_path)))
    assert "0.05 mm" in comparison["tolerance"] and "1%" in comparison["tolerance"]
    assert comparison["not_checked"]
    assert any("electrical" in item for item in comparison["not_checked"])


def test_area_alone_decides_when_the_outline_does_not_move() -> None:
    """Pure comparison: a ring with a notch has a smaller area and the same bounding box."""
    from kicad_tools.sexp import parse_string

    def zone(points: str):
        return parse_string(
            f'(zone (uuid "u") (filled_polygon (layer "F.Cu") (pts {points})))'
        )

    square = "(xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)"
    notched = "(xy 0 0) (xy 10 0) (xy 10 10) (xy 5 10) (xy 5 5) (xy 4 5) (xy 4 10) (xy 0 10)"
    same = gate.compare_zone_fill(zone(square), zone(square), ["F.Cu"])
    smaller = gate.compare_zone_fill(zone(notched), zone(square), ["F.Cu"])
    assert same["comparison"] == "matches"
    assert smaller["comparison"] == "stale"
    assert any("area" in d for d in smaller["differences"])


def test_a_different_polygon_count_is_stale() -> None:
    from kicad_tools.sexp import parse_string

    one = '(zone (uuid "u") (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 4 0) (xy 4 4))))'
    two = one[:-1] + ' (filled_polygon (layer "F.Cu") (pts (xy 0 0) (xy 0.001 0) (xy 0 0.001))))'
    result = gate.compare_zone_fill(parse_string(two), parse_string(one), ["F.Cu"])
    assert result["comparison"] == "stale"


# --- fail closed -------------------------------------------------------------------------------


def test_a_failed_refill_is_not_compared_never_matches(tmp_path: Path) -> None:
    def broken(_path):
        raise RuntimeError("kicad-cli exploded")

    result = gate.verify_board(
        _copy(OBSTACLE, tmp_path), parity="not_applicable", parity_reason="fixture",
        drc=_drc, refill=broken,
    )
    comparison = _comparison(result)
    assert comparison["status"] == "not_compared"
    assert "kicad-cli exploded" in comparison["reason"]
    assert _states(result) == ["not_compared"]


def test_a_refill_that_loses_the_zone_is_not_compared(tmp_path: Path) -> None:
    def drop_zones(path):
        root = write_layer.load(path)
        for zone in root.find_all("zone"):
            root.remove(zone)
        write_layer.save(root, path)

    result = gate.verify_board(
        _copy(OBSTACLE, tmp_path), parity="not_applicable", parity_reason="fixture",
        drc=_drc, refill=drop_zones,
    )
    assert _states(result) == ["not_compared"]
    assert _comparison(result)["status"] == "partly_compared"


def test_without_a_refill_the_comparison_says_it_did_not_run(tmp_path: Path) -> None:
    result = gate.verify_board(
        _copy(OBSTACLE, tmp_path), parity="not_applicable", parity_reason="fixture", drc=_drc
    )
    assert _comparison(result)["status"] == "not_compared"


def test_the_original_is_never_written(tmp_path: Path) -> None:
    pcb = _copy(OBSTACLE, tmp_path)
    before = pcb.read_bytes()
    _grade(pcb)
    assert pcb.read_bytes() == before


# --- review of b57ef82: the guards and the states the first tests did not reach ----------------


def _z(rings: str, extra: str = "", head: str = '(zone (uuid "u")') -> object:
    from kicad_tools.sexp import parse_string

    return parse_string(f"{head} {extra} {rings})")


def _fp(points: str, layer: str = "F.Cu") -> str:
    return f'(filled_polygon (layer "{layer}") (pts {points}))'


SQUARE = "(xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10)"


def test_f1_a_zone_filled_on_one_of_two_layers_is_partial_never_matches() -> None:
    zone = _z(_fp(SQUARE))
    result = gate.compare_zone_fill(zone, _z(_fp(SQUARE)), ["F.Cu", "B.Cu"])
    assert result["comparison"] == "partial"


def test_f1_partial_through_the_write_layer(tmp_path: Path) -> None:
    """A two-layer zone (layers F.Cu B.Cu) with fill stored on F.Cu only, refill as stored."""
    pcb = _copy(OBSTACLE, tmp_path)
    root = write_layer.load(pcb)
    zone = root.find("zone")
    layer = zone.find("layer")
    from kicad_tools.sexp import parse_string

    layers = parse_string('(layers "F.Cu" "B.Cu")')
    zone.children[zone.children.index(layer)] = layers
    write_layer.save(root, pcb)

    def keep(_path):
        return None  # the scratch copy keeps the stored fill: nothing differs

    result = gate.verify_board(
        pcb, parity="not_applicable", parity_reason="fixture", drc=_drc, refill=keep
    )
    assert _states(result) == ["partial"]
    assert _comparison(result)["status"] == "partly_compared"


def test_f2_a_refill_that_drops_a_stored_layer_is_stale(tmp_path: Path) -> None:
    def drop_fill(path):
        root = write_layer.load(path)
        for zone in root.find_all("zone"):
            for fp in zone.find_all("filled_polygon"):
                zone.remove(fp)
        write_layer.save(root, path)

    result = gate.verify_board(
        _copy(OBSTACLE, tmp_path), parity="not_applicable", parity_reason="fixture",
        drc=_drc, refill=drop_fill,
    )
    assert _states(result) == ["stale"]
    assert any("fill stored, none after the refill" in d for d in _comparison(result)["zones"][0]["differences"])


@pytest.mark.parametrize(
    "bad",
    [
        _fp("(xy 0 0) (xy 10 0) (arc (start 10 0) (mid 12 5) (end 10 10)) (xy 0 10)"),
        _fp("(xy 0 0) (xy 10 0)"),
        '(filled_polygon (pts (xy 0 0) (xy 10 0) (xy 10 10)))',
    ],
    ids=["arc_point", "fewer_than_3_points", "no_layer"],
)
def test_f3_an_unreadable_fill_is_not_compared(bad: str) -> None:
    good = _z(_fp(SQUARE))
    assert gate.compare_zone_fill(_z(bad), good, ["F.Cu"])["comparison"] == "not_compared"
    assert gate.compare_zone_fill(good, _z(bad), ["F.Cu"])["comparison"] == "not_compared"


def test_f3_an_unreadable_stored_fill_is_not_compared() -> None:
    def never(_path):
        raise AssertionError("no refill for an unreadable board")

    comparison = gate.stored_fill_comparison(
        Path("x.kicad_pcb"), {"status": "unreadable", "zones": [], "problems": ["x"]}, never
    )
    assert comparison["status"] == "not_compared"


def test_f4_keepouts_are_not_compared_and_footprint_zones_are() -> None:
    from kicad_tools.sexp import parse_string

    root = parse_string(
        '(kicad_pcb (zone (uuid "a") (keepout (tracks not_allowed))) (zone (uuid "b"))'
        ' (footprint "F" (zone (uuid "c")) (zone (uuid "d") (keepout (tracks not_allowed)))))'
    )
    assert sorted(gate._zone_nodes(root)) == ["b", "c"]


def test_f4_orientation_does_not_move_the_centroid() -> None:
    ccw = gate._fill_geometry(_z(_fp(SQUARE)))["F.Cu"]
    cw = gate._fill_geometry(_z(_fp("(xy 0 0) (xy 0 10) (xy 10 10) (xy 10 0)")))["F.Cu"]
    assert ccw["centroid"] == pytest.approx((5, 5)) and cw["centroid"] == pytest.approx((5, 5))
    assert cw["area"] == pytest.approx(100)


def test_f4_multi_polygon_multi_layer_and_hole() -> None:
    small = "(xy 20 0) (xy 22 0) (xy 22 2) (xy 20 2)"
    zone = _z(_fp(SQUARE) + _fp(small) + _fp(SQUARE, "B.Cu"))
    geometry = gate._fill_geometry(zone)
    front = geometry["F.Cu"]
    assert front["polygons"] == 2 and front["area"] == pytest.approx(104)
    assert front["bbox"] == (0, 0, 22, 10)
    assert front["centroid"][0] == pytest.approx((100 * 5 + 4 * 21) / 104)
    assert geometry["B.Cu"]["polygons"] == 1
    # a ring with a hole is one bridged ring: the hole is traversed the other way round
    holed = "(xy 0 0) (xy 10 0) (xy 10 10) (xy 0 10) (xy 0 0) (xy 4 4) (xy 4 6) (xy 6 6) (xy 6 4) (xy 4 4)"
    assert gate._fill_geometry(_z(_fp(holed)))["F.Cu"]["area"] == pytest.approx(96)


def _square(side: float) -> str:
    return f"(xy 0 0) (xy {side} 0) (xy {side} {side}) (xy 0 {side})"


def test_f5_a_few_mm2_on_a_large_pour_is_stale_and_refill_noise_is_not() -> None:
    """100 x 100 mm pour (10000 mm2): 1% would be 100 mm2. A clearance strip of 3 mm2 is stale."""
    fresh = _z(_fp(_square(100)))
    lost_strip = "(xy 0 0) (xy 100 0) (xy 100 99.97) (xy 0 99.97)"
    assert gate.compare_zone_fill(_z(_fp(lost_strip)), fresh, ["F.Cu"])["comparison"] == "stale"
    noisy = "(xy 0.01 0) (xy 100.01 0) (xy 100.01 100) (xy 0.01 100)"
    assert gate.compare_zone_fill(_z(_fp(noisy)), fresh, ["F.Cu"])["comparison"] == "matches"


def test_f5_the_output_states_the_area_cap() -> None:
    assert "1.0 mm2" in gate.FILL_COMPARISON_TOLERANCE


def test_f7_the_refill_has_a_shorter_bound_than_the_default(monkeypatch) -> None:
    from kicad_mcp import kicad_cli

    seen = {}

    def fake_run(args, timeout=None):
        seen["timeout"] = timeout
        raise kicad_cli.KicadCliError("kicad-cli timed out")

    monkeypatch.setattr(kicad_cli, "_run", fake_run)
    with pytest.raises(kicad_cli.KicadCliError):
        kicad_cli.refill_and_save(Path("x.kicad_pcb"))
    assert seen["timeout"] == 300 < kicad_cli._TIMEOUT_SECONDS


def test_f7_a_refill_timeout_is_not_compared_and_the_verdict_is_unchanged(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    from kicad_mcp import kicad_cli

    good = _grade(_copy(OBSTACLE, tmp_path / "good"))

    def timeout(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(kicad_cli.subprocess, "run", timeout)
    monkeypatch.setattr(kicad_cli, "find_kicad_cli", lambda: Path("kicad-cli"))
    slow = _grade(_copy(OBSTACLE, tmp_path / "slow"))
    assert _comparison(slow)["status"] == "not_compared"
    assert "timed out" in _comparison(slow)["reason"]
    for key in ("verdict", "incomplete_reasons", "violations", "unconnected"):
        assert slow[key] == good[key], key
