"""The place_simple measurement record parses, and the script refuses when KiCadRoutingTools is missing (issue 259)."""
import importlib.util
import re
from pathlib import Path

import pytest

EVALS = Path(__file__).resolve().parent.parent / "evals"


def test_record_table_parses_with_six_seeds_per_board():
    rows = [line.split("|")[1:-1] for line in (EVALS / "placement_measurements.md").read_text(encoding="utf-8").splitlines()
            if re.match(r"\| \w+ \| \d \|", line)]
    boards: dict[str, list[int]] = {}
    for cells in rows:
        cells = [c.strip() for c in cells]
        assert len(cells) == 8, cells
        assert re.fullmatch(r"\d+/\d+/\d+", cells[4]) and re.fullmatch(r"\d+/\d+/\d+", cells[5]), cells
        float(cells[6])
        float(cells[7])
        boards.setdefault(cells[0], []).append(int(cells[1]))
    assert len(boards) == 3
    assert all(seeds == list(range(6)) for seeds in boards.values())


def test_script_refuses_without_krt(monkeypatch):
    monkeypatch.setenv("KICAD_ROUTING_TOOLS_PATH", str(EVALS / "no_such_krt"))
    spec = importlib.util.spec_from_file_location("measure_placement", EVALS / "measure_placement.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    with pytest.raises(SystemExit) as exc:
        mod.refuse_unavailable()
    assert "cannot measure" in str(exc.value)
