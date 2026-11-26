"""Measure propose_placement_optimization (KiCadRoutingTools place_simple) on fixture copies (issue 259).

Usage: python evals/measure_placement.py <work_dir> [board ...] > evals/placement_measurements.table
Each board is a path under tests/fixtures (default: the three recorded in evals/placement_measurements.md);
every seed 0..5 runs on its own fresh copy through the tool's own code path (propose, then confirm), and
the kicad-cli DRC counts before and after come from the confirm's own drc_before and drc_after. Prints one
markdown table row per run. A refused, failed or timed-out run is a row with its status, never retried.
Measurement only: no routed or done claim; the frozen openMPPT board is refused by name.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SEEDS = range(6)
TIMEOUT_S = 180  # place_simple's own kill timeout per run (the tool kills the process tree)
BOARDS = (
    "tests/fixtures/golden_attiny1616_board/golden_attiny1616_board.kicad_pcb",
    "tests/fixtures/simple_led.kicad_pcb",
    "tests/fixtures/krt_stress_congestion.kicad_pcb",
)
HEADER = ("| board | seed | status | footprints moved | DRC before (violations/errors/unconnected) "
          "| DRC after | placer s | propose s |\n|---|---|---|---|---|---|---|---|")


class _Collect:
    """Stands in for the MCP server: keeps the registered tool functions so the tool's own code runs."""

    def __init__(self) -> None:
        self.fns: dict = {}

    def add_tool(self, fn, *, name, **_kw) -> None:
        self.fns[name] = fn


def _counts(drc: dict | None) -> str:
    if not isinstance(drc, dict) or "violation_count" not in drc:
        return "n/a"
    return f"{drc['violation_count']}/{drc['error_count']}/{drc['unconnected_count']}"


def refuse_unavailable() -> None:
    """Exit with the reason when place_simple cannot run (no KiCadRoutingTools, wrong version, missing packages)."""
    from boardproof.routers import kicad_placer_tools

    if (problem := kicad_placer_tools.placer_problem()) is not None:
        sys.exit(f"cannot measure: {problem}")


def run_one(tools: dict, board: Path, seed: int, work: Path) -> str:
    from boardproof.toolsets.routing import _trial_copy

    copy = _trial_copy(board, work / f"{board.stem}_seed{seed}")
    started = time.monotonic()
    proposal = tools["propose_placement_optimization"](str(copy), seed=seed, timeout_seconds=TIMEOUT_S)
    wall = f"{time.monotonic() - started:.1f}"
    name = f"{board.stem} | {seed}"
    if proposal.get("status") != "pending_confirmation":
        why = proposal.get("code") or proposal.get("status")
        text = str(proposal.get("reason", ""))
        timed_out = " (timed out)" if "timed out" in text.lower() or "timeout" in text.lower() else ""
        return f"| {name} | {why}{timed_out} | 0 | n/a | n/a | n/a | {wall} |"
    confirmed = tools["confirm_placement_optimization"](proposal["revision_token"])
    placer_s = (proposal.get("placer") or {}).get("elapsed_s")
    placer = f"{placer_s:.1f}" if isinstance(placer_s, (int, float)) else "n/a"
    return (f"| {name} | {confirmed.get('status')} | {proposal.get('footprints_moved')} "
            f"| {_counts(confirmed.get('drc_before'))} | {_counts(confirmed.get('drc_after'))} | {placer} | {wall} |")


def main(argv: list[str]) -> None:
    if not argv:
        sys.exit(__doc__)
    from boardproof.toolsets import placement

    refuse_unavailable()
    work = Path(argv[0])
    boards = [ROOT / b for b in (argv[1:] or BOARDS)]
    for b in boards:
        if "openmppt" in str(b).lower():
            sys.exit("the frozen openMPPT board is never measured")
    collect = _Collect()
    placement.build(collect)
    print(HEADER)
    for board in boards:
        for seed in SEEDS:
            print(run_one(collect.fns, board, seed, work), flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
