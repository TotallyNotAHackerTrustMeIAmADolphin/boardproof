"""A small board for the pair-clearance tests (issue 131), built through the project's own tools.

The golden board gets two parallel 0.25 mm tracks 0.25 mm apart (kicad_tools' object model through the
write layer, as ``test_board_rules._build``), on the nets VCC and GND; the net classes ``Mains`` (VCC) and
``Sense`` (GND) are then made by ``propose_board_rules`` and ``confirm_board_rules`` themselves.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from kicad_tools.schema import PCB
from kicad_tools.sexp import parse_string

from boardproof import board_rules, write_layer

FIXTURES = Path(__file__).parent / "fixtures"
GOLDEN_DIR = FIXTURES / "golden_attiny1616_board"
GOLDEN_NAME = "golden_attiny1616_board.kicad_pcb"
GAP_MM = 0.25
EMPTY_REPORT = {"violations": [], "unconnected_items": [], "schematic_parity": [], "ignored_checks": []}
TRACK_UUID_PREFIX = "66666666"


def fake_drc(path, **_kwargs):
    return EMPTY_REPORT


def build_pair_board(dest: Path, *, tracks: bool = True, classes: bool = True) -> Path:
    shutil.copytree(GOLDEN_DIR, dest / "proj")
    pcb = dest / "proj" / GOLDEN_NAME
    if tracks:
        board = PCB.load(pcb)
        for i, (y, net) in enumerate(((88.0, 5), (88.5, 1))):  # VCC, GND
            board._sexp.append(
                parse_string(
                    f'(segment (start 130 {y}) (end 140 {y}) (width 0.25) (layer "F.Cu") (net {net}) '
                    f'(uuid "{TRACK_UUID_PREFIX}-0000-4000-8000-00000000000{i}"))'
                )
            )
        write_layer.save_pcb(board, pcb)
    if classes:
        proposal = board_rules.propose_board_rules(
            pcb,
            netclasses=[
                {"name": "Mains", "clearance": 0.2, "nets": ["VCC"], "priority": 1},
                {"name": "Sense", "clearance": 0.2, "nets": ["GND"], "priority": 2},
            ],
            output_dir=dest / "classes_rev",
        )
        assert proposal["status"] == "proposed", proposal
        done = board_rules.confirm_board_rules(pcb, proposal["revision_token"], parity="required", drc=fake_drc)
        assert done["status"] == "applied", done
        pcb = Path(done["output_pcb"])
    return pcb


def pair_violations(report: dict) -> list[dict]:
    """The clearance violations between the two fixture tracks (both items carry the fixture's uuids)."""
    return [
        v
        for v in report["violations"]
        if v["type"] == "clearance" and all(i["uuid"].startswith(TRACK_UUID_PREFIX) for i in v["items"])
    ]
