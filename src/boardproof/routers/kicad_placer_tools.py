"""KiCadRoutingTools' `py_placer/place_simple.py` as the placement-optimisation backend (issue 257).

It replaces the retired router-in-the-loop `place_route_loop.py` (upstream removed it, issue 247). The
placer runs as a subprocess on a throw-away copy of the board; nothing it writes is ever adopted. Only
the footprint poses are read back, from the raw ``(at x y angle)`` tokens (hard rule 2), and applied
through ``placement_anchor.write_poses``.

Inputs `place_simple` needs and what this module sets in each:

- ``groups.json`` (schema 1): ONE group named ``all`` listing every footprint reference of the board
  (locked ones included: they are fixed members). No ``edge``, ``side``, ``anchor``, ``near``,
  ``apart``, ``templates`` or ``flippable`` is set, so no face changes and no judgement about
  functional groups is invented; the placer then minimises wirelength, crossings and decap distance
  over one group.
- ``intent.json``: the starter intent READ OFF the copy by ``check_floorplan.py --emit-intent`` with
  its default flags: the envelope and the legality budget observed on the board, no blocks, no
  proximity claims, no keep-outs, no decap limit. It is observation only, so it declares no
  requirement the placer could steer toward.
- ``--seed N`` (default 0): one seed, so the same board and seed give the same poses.
- ``PLACEMENT_MANIFEST``: pointed into the run folder, so the placer's run record never leaves it.

Never reachable from the MCP surface: ``_run_process`` and ``_probe_imports`` are module-level
functions that tests replace; no tool parameter names them.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any

from boardproof import write_layer
from boardproof.routers.kicad_routing_tools import (
    KicadRoutingToolsError,
    find_kicad_routing_tools,
    find_kicad_routing_tools_python,
    load_cli_snapshot,
    router_version,
)

PLACER_SCRIPT = Path("py_placer") / "place_simple.py"
FLOORPLAN_SCRIPT = Path("py_tools") / "check_floorplan.py"
_REQUIRED_PACKAGES = ("numpy", "scipy", "shapely")
GROUP_NAME = "all"


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child and everything it started (the placer spawns workers that outlive a plain kill)."""
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, check=False, timeout=30)
        else:
            os.killpg(proc.pid, 9)
    except (OSError, subprocess.SubprocessError):
        pass
    proc.kill()


def _run_process(args: list[str], *, cwd: str, env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    """The one place a child process starts (tests replace it). On a timeout the whole process tree is killed."""
    group = {"start_new_session": True} if os.name != "nt" else {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    with subprocess.Popen(args, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                          encoding="utf-8", errors="replace", **group) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            proc.communicate()
            raise
    return subprocess.CompletedProcess(args, proc.returncode, out, err)


def _probe_imports(python: Path) -> list[str]:
    """The required packages the interpreter cannot import (tests replace it)."""
    code = "import importlib.util as u,sys;print(' '.join(m for m in sys.argv[1:] if u.find_spec(m) is None))"
    done = _run_process([str(python), "-c", code, *_REQUIRED_PACKAGES], cwd=str(Path(python).parent),
                        env=_minimal_env(), timeout=60)
    if done.returncode != 0:
        raise KicadRoutingToolsError(f"the interpreter {python} could not be probed: {done.stderr[-300:]}")
    return done.stdout.split()


_ENV_PASSED_THROUGH = ("SystemRoot", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE")


def _minimal_env(manifest: Path | None = None) -> dict[str, str]:
    env = {k: os.environ[k] for k in _ENV_PASSED_THROUGH if k in os.environ}
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    if manifest is not None:
        env["PLACEMENT_MANIFEST"] = str(manifest)
    return env


def placer_problem() -> str | None:
    """None when `place_simple` can run, else one plain sentence saying what is unavailable. Nothing is invented:
    a missing script, a version other than the pinned one, or a missing package all answer here."""
    try:
        checkout = find_kicad_routing_tools()
        python = find_kicad_routing_tools_python()
        pinned = load_cli_snapshot().get("router_version")
        installed = router_version(checkout)
        if not (checkout / PLACER_SCRIPT).is_file() or not (checkout / FLOORPLAN_SCRIPT).is_file():
            return (f"unavailable: {PLACER_SCRIPT.as_posix()} or {FLOORPLAN_SCRIPT.as_posix()} is missing in the installed "
                    f"KiCadRoutingTools {installed or 'unknown version'} ({checkout}). Nothing was changed.")
        if not pinned or installed != pinned:
            return (f"unavailable: the installed KiCadRoutingTools is {installed or 'of unknown version'}, the pinned version "
                    f"is {pinned or 'unknown'}; the placer is only run on the pinned version. Nothing was changed.")
        missing = _probe_imports(python)
    except KicadRoutingToolsError as exc:
        return f"unavailable: {exc}. Nothing was changed."
    if missing:
        return f"unavailable: the placer's interpreter {python} lacks the package(s) {', '.join(missing)}. Nothing was changed."
    return None


def groups_document(refs: list[str]) -> dict:
    """The groups file: one group holding every footprint (see the module docstring)."""
    return {"schema": 1, "groups": [{"name": GROUP_NAME, "refs": list(refs),
                                     "why": "one group: no functional grouping is judged here"}]}


def place_simple_args(board: Path, out: Path, groups: Path, intent: Path, seed: int) -> list[str]:
    return [str(board), str(out), "--groups", str(groups), "--intent", str(intent), "--seed", str(int(seed))]


def run_place_simple(board_copy: Path, run_dir: Path, refs: list[str], *, seed: int = 0, timeout: int = 300) -> dict:
    """Run `check_floorplan --emit-intent` then `place_simple` on ``board_copy`` (a disposable copy inside
    ``run_dir``). Raises ``KicadRoutingToolsError`` on a timeout, a non-zero exit or a missing output; returns
    ``{"output_pcb", "elapsed_s", "exit_code", "report"}`` otherwise. The output board is only a source of poses."""
    checkout = find_kicad_routing_tools()
    python = find_kicad_routing_tools_python()
    run_dir.mkdir(parents=True, exist_ok=True)
    groups, intent = run_dir / "groups.json", run_dir / "intent.json"
    output, report = run_dir / "placed.kicad_pcb", run_dir / "report.json"
    groups.write_text(json.dumps(groups_document(refs)), encoding="utf-8")
    env = _minimal_env(run_dir / "placement_manifest.json")
    started = time.time()
    steps = [
        ("check_floorplan --emit-intent", [str(python), str(checkout / FLOORPLAN_SCRIPT), str(board_copy), "--emit-intent", str(intent)]),
        ("place_simple", [str(python), str(checkout / PLACER_SCRIPT), *place_simple_args(board_copy, output, groups, intent, seed), "--json", str(report)]),
    ]
    for name, args in steps:
        try:
            done = _run_process(args, cwd=str(checkout), env=env, timeout=max(1.0, timeout - (time.time() - started)))
        except subprocess.TimeoutExpired as exc:
            raise KicadRoutingToolsError(f"{name} timed out after {timeout} s and was killed; nothing was adopted") from exc
        except OSError as exc:
            raise KicadRoutingToolsError(f"could not start {name} via {python}: {exc}") from exc
        if done.returncode != 0:
            raise KicadRoutingToolsError(f"{name} exited with code {done.returncode}; stderr tail: {(done.stderr or '')[-600:]}")
    if not output.is_file():
        raise KicadRoutingToolsError("place_simple exited 0 but wrote no board; nothing was adopted")
    try:
        summary: Any = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    except (OSError, ValueError):
        summary = {}
    return {"output_pcb": str(output), "elapsed_s": round(time.time() - started, 2), "exit_code": 0, "report": summary}


def _raw_board_origin(root: Any) -> tuple[float, float]:
    """The board origin from the raw Edge.Cuts geometry: the first ``gr_rect``'s
    start, otherwise the minimum corner of the Edge.Cuts ``gr_line`` segments."""
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
    """Reference designator of a raw footprint node (KiCad 8+ ``property`` or the older ``fp_text reference``)."""
    for prop in footprint.find_all("property"):
        if len(prop.values) >= 2 and str(prop.values[0]) == "Reference":
            return str(prop.values[1])
    for text in footprint.find_all("fp_text"):
        if len(text.values) >= 2 and str(text.values[0]) == "reference":
            return str(text.values[1])
    return None


def _raw_poses(path: str | Path) -> tuple[dict[str, tuple[float, float, float]], tuple[float, float]]:
    """``{ref: (x, y, rotation)}`` straight from each footprint's raw ``(at X Y rot)`` node (rotation is
    omitted by KiCad when it is 0), plus the raw board origin."""
    root = write_layer.load(path)
    poses: dict[str, tuple[float, float, float]] = {}
    for footprint in [*root.find_all("footprint"), *root.find_all("module")]:
        ref = _raw_reference(footprint)
        at = footprint.find("at")
        if ref is None or at is None or len(at.values) < 2:
            continue
        rotation = (float(at.values[2]) if len(at.values) > 2 else 0.0) % 360.0
        poses[ref] = (float(at.values[0]), float(at.values[1]), rotation)
    return poses, _raw_board_origin(root)


def placement_delta(original_pcb: str | Path, placed_pcb: str | Path) -> dict[str, tuple[float, float, float]]:
    """``{ref: (x, y, rotation)}`` for every footprint whose raw pose differs between the copy that went into
    the placer and the board it wrote. The poses are in the file's own page frame (what ``write_poses``
    takes); the caller subtracts the outline origin for the local frame it shows. Raises ``ValueError`` when the
    two boards do not hold the same footprints: a placer that adds or drops one is not trusted."""
    original, _ = _raw_poses(original_pcb)
    placed, _ = _raw_poses(placed_pcb)
    if set(original) != set(placed):
        diff = sorted(set(original) ^ set(placed))
        raise ValueError(f"the placer's board and the input hold different footprints: {diff}")
    delta: dict[str, tuple[float, float, float]] = {}
    for ref, (rx, ry, rrot) in placed.items():
        bx, by, brot = original[ref]
        if abs(bx - rx) > 1e-6 or abs(by - ry) > 1e-6 or min(abs(brot - rrot), 360.0 - abs(brot - rrot)) > 1e-6:
            delta[ref] = (rx, ry, rrot)
    return delta


__all__ = [
    "GROUP_NAME",
    "PLACER_SCRIPT",
    "groups_document",
    "placement_delta",
    "placer_problem",
    "run_place_simple",
]
