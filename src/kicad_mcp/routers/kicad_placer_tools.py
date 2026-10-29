"""KiCadRoutingTools' `py_placer/` package as a real placement-optimization
backend -- ticket 08 of the `krt-capability-suite` Wayfinder map.

Built on the real survey findings from tickets 02/03/04/06 of that map (see
each ticket's `## Answer` for full detail):

- `place_route_loop.py` ("router-in-the-loop placement repair") is the real
  entry point: it runs `route.py`, reads which nets actually failed, moves
  just those (or their blockers), and re-routes, iterating across
  `--rounds`. This automates exactly the manual "fix one bottleneck, retry,
  find the next" loop this project did by hand on openMPPT (tickets 05/12,
  `openmppt-ship`).
- `--route-args` is REQUIRED by `place_route_loop.py` itself unless
  `--suggest-locks` is also given (confirmed by a real invocation: omitting
  it entirely is an argparse error, "--route-args is required (except with
  --suggest-locks)" -- `--help`'s square brackets around it are misleading).
  An empty string does NOT satisfy this either (`place_route_loop.py`'s own
  check is `if not args.route_args`, and `""` is falsy) -- confirmed by a
  second real invocation after the first fix attempt still failed the same
  way. This module defaults to `"--escalation board"`, matching
  `run_kicad_routing_tools`'s own default escalation policy so behavior is
  consistent between the two wrappers.
- `--group-by` needs `sheet` in the list or it derives zero rigid blocks on
  a real board (ticket 02's finding, confirmed again on `place_route_loop`
  by ticket 04) -- this module defaults to `"auto,sheet,netprefix,decap"`,
  never bare `"auto"`.
- `--relocate` is measured NET-NEGATIVE by KRT's own authors' corpus study
  (ticket 04) -- left off by default here, never silently enabled.
- `--ratsnest-screen` is a real, free pre-route regression filter (skip a
  candidate placement's routing attempt if its airwire crossings/HPWL
  regressed) that ticket 04 found unused by any of this project's prior
  invocations -- defaulted ON here (15% threshold, a conservative middle
  value; not independently tuned, revisit if it screens out real wins).
- `--movie` is ON by default in the underlying tool and genuinely works
  (ticket 04 verified a real rendered output) -- left on, its path surfaced
  in the return dict as a human-review aid, never treated as evidence.
- No graceful-stop mechanism exists here (checked directly: neither
  `place_route_loop.py` nor `place_optimize.py` reference `KICAD_STOP_FILE`
  or any equivalent env var/flag, unlike `route.py`'s real one -- see
  `kicad_routing_tools.stop_file_for`/`request_graceful_stop`). `timeout`
  is therefore the ONLY way to bound a run; say so plainly rather than
  silently omitting the capability ticket 08 asked to check for.

Discovery is shared with the router (same checkout, same interpreter
requirements) -- this module does not re-implement `find_kicad_routing_tools`/
`find_kicad_routing_tools_python`, it imports them.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

from kicad_mcp.routers.kicad_routing_tools import (
    KicadRoutingToolsError,
    find_kicad_routing_tools,
    find_kicad_routing_tools_python,
)

PLACER_SCRIPT = Path("py_placer") / "place_route_loop.py"

# Bare "auto" alone derives zero rigid blocks on a real board (ticket 02's
# finding) -- "sheet" must be present for --group-by to do anything useful.
DEFAULT_GROUP_BY = "auto,sheet,netprefix,decap"
# A conservative middle value, not independently tuned against a corpus the
# way KRT's own defaults are -- revisit if it turns out to screen out real
# improvements on a board this project actually cares about.
DEFAULT_RATSNEST_SCREEN_PERCENT = 15.0


def build_placer_args(
    input_pcb: str | Path,
    output_pcb: str | Path,
    *,
    route_args: str = "--escalation board",
    rounds: int | None = None,
    group_by: str | None = DEFAULT_GROUP_BY,
    ratsnest_screen: float | None = DEFAULT_RATSNEST_SCREEN_PERCENT,
    lock: tuple[str, ...] = (),
    work_dir: str | Path | None = None,
    movie: bool = True,
    verbose: bool = True,
) -> list[str]:
    """The argument list after the interpreter and `place_route_loop.py`.

    `--relocate` is never passed (ticket 04: measured net-negative by KRT's
    own authors). `--target-select` is left at its default (`pins`) --
    ticket 04 found `diagnosis` mode needs an explicit `--group-by` to do
    anything and is not proven better than the default in KRT's own docs.
    """
    args = [str(input_pcb), str(output_pcb), "--route-args", route_args]
    if rounds is not None:
        args += ["--rounds", str(int(rounds))]
    if group_by is not None:
        args += ["--group-by", group_by]
    if ratsnest_screen is not None:
        args += ["--ratsnest-screen", f"{ratsnest_screen:g}"]
    for ref in lock:
        args += ["--lock", ref]
    if work_dir is not None:
        args += ["--work-dir", str(work_dir)]
    args.append("--movie" if movie else "--no-movie")
    if verbose:
        args.append("--verbose")
    return args


def run_placement_repair(
    input_pcb: str | Path,
    output_pcb: str | Path,
    *,
    route_args: str = "--escalation board",
    rounds: int | None = None,
    group_by: str | None = DEFAULT_GROUP_BY,
    ratsnest_screen: float | None = DEFAULT_RATSNEST_SCREEN_PERCENT,
    lock: tuple[str, ...] = (),
    movie: bool = True,
    timeout: int = 1800,
) -> dict:
    """Run KRT's `place_route_loop.py` as a local subprocess on `input_pcb`
    (a disposable trial copy -- same discipline as `run_kicad_routing_tools`,
    the caller must have already copied the sibling `.kicad_pro`/`.kicad_dru`/
    lib-tables alongside it, since the loop's own internal `route.py`
    sub-invocations need them for the same reason archived backlog P4-09 found).

    No graceful-stop exists for this tool (see module docstring) --
    `timeout` is a hard `proc.kill()` backstop with no softer option, and a
    killed run's partial state is whatever `--work-dir` already has on disk
    (this function does not guarantee `output_pcb` exists after a timeout).

    Streams stdout/stderr to real files next to `output_pcb`, same pattern
    as `run_kicad_routing_tools` (ticket 08 of `openmppt-ship`) -- survives
    a kill, and this tool is genuinely verbose per-round.

    Raises `KicadRoutingToolsError` on discovery failure or a hard timeout.
    Returns a dict describing the run otherwise -- `output_pcb` may still
    not exist if every round failed to improve on the input (check
    `Path(result["output_pcb"]).is_file()` before treating it as usable).
    """
    checkout = find_kicad_routing_tools()
    python = find_kicad_routing_tools_python()
    input_pcb = Path(input_pcb)
    output_pcb = Path(output_pcb)
    output_pcb.parent.mkdir(parents=True, exist_ok=True)
    work_dir = output_pcb.parent

    args = [
        str(python),
        str(checkout / PLACER_SCRIPT),
        *build_placer_args(
            input_pcb, output_pcb,
            route_args=route_args, rounds=rounds, group_by=group_by,
            ratsnest_screen=ratsnest_screen, lock=lock, work_dir=work_dir, movie=movie,
        ),
    ]
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    stdout_log = output_pcb.with_name(output_pcb.stem + ".placer_repair.log")
    stderr_log = output_pcb.with_name(output_pcb.stem + ".placer_repair.stderr.log")
    started = time.time()
    returncode: int | None = None
    try:
        with open(stdout_log, "w", encoding="utf-8", errors="replace") as out_f, \
             open(stderr_log, "w", encoding="utf-8", errors="replace") as err_f:
            # place_route_loop.py, like route.py, bootstraps its own sys.path
            # relative to itself -- run from the checkout root.
            proc = subprocess.Popen(args, cwd=str(checkout), stdout=out_f, stderr=err_f, env=env)
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
    except OSError as exc:
        raise KicadRoutingToolsError(f"could not start place_route_loop.py via {python}: {exc}") from exc
    elapsed = time.time() - started

    stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace") if stdout_log.is_file() else ""
    stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace") if stderr_log.is_file() else ""

    if returncode is None:
        tail = "\n".join(stdout_text.splitlines()[-25:])
        raise KicadRoutingToolsError(
            f"place_route_loop.py timed out after {timeout}s (process killed) -- no graceful-stop "
            f"mechanism exists for this tool.\nstdout log ({stdout_log}) tail:\n{tail}\n"
            f"stderr log ({stderr_log}) tail:\n{stderr_text[-2000:]}"
        )

    movie_path = work_dir / "placement.mp4"
    return {
        "exit_code": returncode,
        "elapsed_s": round(elapsed, 2),
        "args": args[2:],
        "output_pcb": str(output_pcb),
        "output_produced": output_pcb.is_file(),
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        "stderr_tail": stderr_text[-1000:],
        "movie_path": str(movie_path) if movie_path.is_file() else None,
        "rounds_summary": _parse_round_summary(stdout_text),
    }


_ROUND_LINE_RE = re.compile(r"^Round\s+(\d+)\D.*?(\d+)\s+(?:failed|target)", re.MULTILINE | re.IGNORECASE)


def _parse_round_summary(stdout_text: str) -> list[dict]:
    """Best-effort per-round progress lines for human review -- NOT used as
    a scoring signal anywhere (real `kicad-cli` DRC before/after is, per
    this project's standing rule). If the real log format doesn't match
    this pattern, returns an empty list rather than guessing -- the raw log
    file is always there to read directly."""
    rounds: list[dict] = []
    for match in _ROUND_LINE_RE.finditer(stdout_text):
        rounds.append({"round": int(match.group(1)), "raw": match.group(0)})
    return rounds


def _raw_board_origin(root: Any) -> tuple[float, float]:
    """The board origin from the raw Edge.Cuts geometry: the first ``gr_rect``'s
    start, otherwise the minimum corner of the Edge.Cuts ``gr_line`` segments.

    This is the same rule ``update_footprint_position`` applies when it adds the
    origin back, read from the file so no convenience property is involved.
    """
    for rect in root.find_all("gr_rect"):
        layer = rect.find("layer")
        start = rect.find("start")
        if layer is not None and start is not None and "Edge.Cuts" in layer.values:
            return float(start.values[0]), float(start.values[1])
    xs: list[float] = []
    ys: list[float] = []
    for line in root.find_all("gr_line"):
        layer = line.find("layer")
        if layer is None or "Edge.Cuts" not in layer.values:
            continue
        for end_name in ("start", "end"):
            point = line.find(end_name)
            if point is not None:
                xs.append(float(point.values[0]))
                ys.append(float(point.values[1]))
    if xs and ys:
        return min(xs), min(ys)
    return 0.0, 0.0


def _raw_reference(footprint: Any) -> str | None:
    """Reference designator of a raw footprint node (KiCad 8+ ``property`` or
    the older ``fp_text reference``)."""
    for prop in footprint.find_all("property"):
        if len(prop.values) >= 2 and str(prop.values[0]) == "Reference":
            return str(prop.values[1])
    for text in footprint.find_all("fp_text"):
        if len(text.values) >= 2 and str(text.values[0]) == "reference":
            return str(text.values[1])
    return None


def _raw_poses(path: str | Path) -> tuple[dict[str, tuple[float, float, float]], tuple[float, float]]:
    """``{ref: (x, y, rotation)}`` straight from each footprint's raw ``(at X Y rot)``
    node (rotation is omitted by KiCad when it is 0), plus the raw board origin."""
    from kicad_tools.sexp import parse_file

    root = parse_file(str(path))
    poses: dict[str, tuple[float, float, float]] = {}
    for footprint in [*root.find_all("footprint"), *root.find_all("module")]:
        ref = _raw_reference(footprint)
        at = footprint.find("at")
        if ref is None or at is None or len(at.values) < 2:
            continue
        rotation = (float(at.values[2]) if len(at.values) > 2 else 0.0) % 360.0
        poses[ref] = (float(at.values[0]), float(at.values[1]), rotation)
    return poses, _raw_board_origin(root)


def placement_delta(original_pcb: str | Path, repaired_pcb: str | Path) -> dict[str, tuple[float, float, float]]:
    """Diff footprint poses between the original board and a placer-repaired
    trial copy. Returns `{ref: (x, y, rotation)}` for every footprint whose
    position or rotation actually changed -- the exact shape `placement.py`'s
    `_PENDING_PLACEMENTS`/`confirm_autoplacement` already use, so a caller can
    register this delta the same way.

    Every pose is read from the raw ``(at X Y rot)`` S-expression, never from
    ``Footprint.position`` / ``.rotation`` (CLAUDE.md hard rule 2: those have
    returned wrong board-absolute coordinates). Change detection compares raw
    values directly, so it is independent of any origin. The returned
    coordinates are board-local mm (raw minus the original board's origin),
    the frame ``update_footprint_position`` expects.
    """
    original, origin = _raw_poses(original_pcb)
    repaired, _ = _raw_poses(repaired_pcb)
    ox, oy = origin

    delta: dict[str, tuple[float, float, float]] = {}
    for ref, (rx, ry, rrot) in repaired.items():
        before = original.get(ref)
        if before is None:
            continue  # placer doesn't add/remove footprints; a mismatch here is a different problem
        bx, by, brot = before
        if abs(bx - rx) > 1e-6 or abs(by - ry) > 1e-6 or abs(brot - rrot) > 1e-6:
            delta[ref] = (rx - ox, ry - oy, rrot)
    return delta


__all__ = [
    "DEFAULT_GROUP_BY",
    "DEFAULT_RATSNEST_SCREEN_PERCENT",
    "PLACER_SCRIPT",
    "build_placer_args",
    "placement_delta",
    "run_placement_repair",
]
