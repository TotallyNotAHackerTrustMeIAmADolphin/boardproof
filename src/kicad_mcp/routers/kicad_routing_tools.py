"""KiCadRoutingTools (`drandyhaas/KiCadRoutingTools`, MIT) as a second
autorouting backend -- archived backlog P4-05, evaluated in docs/ARCHITECTURE.md
("Second routing backend adopted").

What this module is
-------------------
A subprocess wrapper around KRT's `py_router/route.py` plus the three pieces
the evaluation said the integration had to get right:

1. **Never adopt KRT's rewritten `.kicad_pro`; always grade against the
   board's ORIGINAL rules.** `run_kicad_routing_tools` routes a trial copy
   and the caller (toolsets/routing.py) copies the original project file
   back beside the output before any DRC. Measured while building this
   (2026-09-11), and stronger than the evaluation assumed: even with
   `--no-fix-drc-settings`, KRT v0.22.0 still rewrites the output's sibling
   `.kicad_pro` ("In-run DRC floors (#650): lowered 1 value(s) ...
   rules.min_hole_clearance: 0.25 -> 0.2 mm") -- so restoring the original
   project is mandatory, not defensive. `route.py --help` documents
   `--no-fix-drc-settings` as "Do not adjust the output's .kicad_pro DRC
   constraints" -- upstream-reportable.

2. **The clearance gap, measured.** docs/ARCHITECTURE.md's evaluation found that
   under `--escalation board` KRT bounds track/via descents at the board's
   `rules.min_*` but not clearance (KiCad writes `rules.min_clearance = 0.0`
   and keeps clearance in the netclass; KRT treats 0.0 as unset and falls
   back to its fab-tier floor), and asked P4-05 to try `--clearance` first,
   then `--fab-overrides`, and report which one actually closed the gap.
   Real `kicad-cli` DRC on the golden-path board, graded against the
   ORIGINAL project rules, all `--escalation board --no-fix-drc-settings`:

     * plain                              10 violations (all clearance) / 1 unconnected, min_clearance_used 0.1635
     * `--clearance 0.2`                   8 violations (all clearance) / 1 unconnected, min_clearance_used 0.1635
     * `--fab-overrides` (clearance=0.2)   0 violations               / 3 unconnected, min_clearance_used 0.2
     * `--clearance 0.2` + overrides       0 violations               / 3 unconnected, min_clearance_used 0.2

   So `--clearance` sets the run's nominal Default-class clearance but does
   NOT bound the per-net rescue descents -- the rescue pass ignores it
   ("rescued a gap: grid 0.025, clearance 0.1635") exactly as it ignored the
   netclass. A `--fab-overrides` file, which KRT documents as pinning the
   floor to "exactly the file + base tier", is what makes it hard. This
   module therefore generates an overrides file from the board's own
   effective rules (P1-18's `board_rules`) for `board` policy, and passes
   `--clearance` as well so the nominal value is the board's, not a guess.
   The price is honest: with the floor pinned, the golden board's VQFN pads
   genuinely cannot be reached at 0.2/0.2 (the meta-finding both routers
   agree on), so those nets stay open and are reported as unconnected by
   the real DRC rather than reached by breaking the rules. A finer grid
   (`--grid-step 0.05`, KRT's own hint for boxed-in fine-pitch pads)
   recovers one of the three: 0 violations / 2 unconnected.

3. **Coordinates.** KRT writes page-absolute coordinates into the
   `.kicad_pcb` (same as KiCad itself), but `kicad_tools.schema.PCB.load`
   subtracts `board_origin` from every segment/via on load, so
   `route_delta` -- which diffs a KRT-routed trial copy against the
   original through `kicad_tools` -- yields board-local specs already in
   the frame `PCB.add_trace`/`add_via` expect (routing.py bug #3). The
   delta is computed in page-absolute space and re-based on the ORIGINAL
   board's origin, so it is correct even if the two loads detected
   different origins. Verified round-trip on the evaluation's default-
   policy trial file: 82 segments / 7 vias diffed, applied, reloaded.

4. **The trial copy must be a full sibling set, not a bare `.kicad_pcb`.**
   archived backlog P4-09: routing a bare `.kicad_pcb` copy (no `.kicad_pro`
   alongside it) makes KRT resolve clearances from its generic STOCK
   netclass instead of the board's real per-netclass rules -- silently, no
   error, just a wrong number. Confirmed on a real board: 18 hidden
   `Power_20A`-netclass clearance violations (0.5mm declared, as little as
   0.2mm delivered) under a "clean-looking" bare-copy run. `toolsets/
   routing.py`'s `_trial_copy` now copies `.kicad_pro`/`.kicad_prl`/
   `.kicad_dru` plus `fp-lib-table`/`sym-lib-table` alongside every trial
   `.kicad_pcb` this module is handed, for both the KRT input and the
   post-route DRC-scoring copy -- this module itself never constructs a
   trial directory, it only ever runs against whatever path it's given, so
   the fix lives entirely in the caller.

Everything here is evidence-gated: discovery/availability problems come
back as `status: incomplete` dicts naming exactly what is missing and the
install command; the only exceptions raised are `KicadRoutingToolsError`
from the low-level `find_*`/`run_*` functions, which the toolset catches.

License note: KRT is MIT. It is still invoked only as a subprocess on a
trial copy -- not for licensing reasons, but because its CLI bootstraps its
own `sys.path` (must run from its checkout root as a script) and pulls
numpy/scipy/shapely/Pillow plus a prebuilt Rust `.pyd`/`.so` that this
project deliberately does not vendor or depend on (archived backlog P4-05: no
new core dependency in `pyproject.toml`).

4. **Archived backlog P4-06 (2026-09-12): KRT's signal router does not treat a
   pre-existing FILLED COPPER ZONE as an obstacle for a foreign net.**
   `py_router/obstacle_map.py`'s `build_base_obstacle_map` -- the function
   that builds the grid cells KRT's A* router will not enter -- stamps BGA
   exclusion boxes, other nets' existing segments/vias/pads, board-edge
   clearance, native `(zone (keepout ...))` rule areas, and drill-hole
   clearance; a copper-pour `Zone`'s fill polygon is never one of them
   (confirmed against KRT's own `docs/routing-architecture.md`, which
   enumerates the identical 7 obstacle types and likewise omits pours).
   Reproduced directly: a LED_ANODE trace routed straight onto a
   pre-existing GND pour landed at 0.0mm actual clearance per real
   `kicad-cli` DRC -- the same failure shape as openMPPT v1.3's 12 real
   shorts. `toolsets/routing.py`'s `propose_route_with_retry` now refuses
   up front (both backends -- `kicad_tools.export.dsn`'s Freerouting path
   has the same exposure, see routing.py finding #13) whenever the board
   has a pre-existing filled zone; see `_pre_existing_filled_zone_report`
   and routing.py's P4-06 docstring section for the full evidence and the
   guard. Upstream-PR candidate against KRT (treat foreign zone fill the
   way keepout rule areas already are), not filed yet.
"""

from __future__ import annotations

import glob
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ENV_PATH = "KICAD_ROUTING_TOOLS_PATH"
ENV_PYTHON = "KICAD_ROUTING_TOOLS_PYTHON"
ROUTE_SCRIPT = Path("py_router") / "route.py"

# (pip name, import name) -- the four runtime deps KRT's own startup_checks
# and README name, plus the Rust binary probed separately below.
REQUIRED_PACKAGES: tuple[tuple[str, str], ...] = (
    ("numpy", "numpy"),
    ("scipy", "scipy"),
    ("shapely", "shapely"),
    ("Pillow", "PIL"),
)
INSTALL_DEPS_HINT = "pip install numpy scipy shapely Pillow  (into the interpreter named by KICAD_ROUTING_TOOLS_PYTHON)"
INSTALL_BINARY_HINT = (
    "cd <KiCadRoutingTools checkout> && python build_router.py  "
    "(downloads the prebuilt grid_router binary for this platform/Python; no Rust toolchain needed)"
)
INSTALL_CHECKOUT_HINT = (
    "git clone https://github.com/drandyhaas/KiCadRoutingTools and set "
    f"{ENV_PATH} to the checkout directory (the one containing py_router/route.py), or install it "
    "through KiCad's Plugin and Content Manager"
)

# KRT is not a console script and never on PATH: it is either a git checkout
# (env override) or a PCM install under KiCad's per-version user directory
# `<user dir>/<version>/3rdparty/plugins/<package>/` (KRT's own
# install_plugin.py documents that layout; PCM installs use the identifier
# `com_github_drandyhaas_kicadroutingtools`, dev installs `KiCadRoutingTools`,
# hence the `*` at the package level). Newest KiCad version dir first.
_PLUGIN_GLOBS: dict[str, list[str]] = {
    "win32": [
        "~/Documents/KiCad/*/3rdparty/plugins/*/py_router/route.py",
        "~/OneDrive/Documents/KiCad/*/3rdparty/plugins/*/py_router/route.py",
    ],
    "darwin": ["~/Documents/KiCad/*/3rdparty/plugins/*/py_router/route.py"],
    "linux": ["~/.local/share/kicad/*/3rdparty/plugins/*/py_router/route.py"],
}

VALID_ESCALATIONS = ("off", "board", "fab")

# `.kicad_pro` rules.min_* key and netclass canonical key that define the
# board's own floor for each fab-override key KRT accepts
# (fab_overrides.example.txt documents the keys). `board_edge` maps to
# min_copper_edge_clearance; `pad_hole_to_hole` has no KiCad rule and is
# deliberately absent. Precedence: the project's explicit minimum (what
# KiCad's DRC enforces), else the tightest netclass value (conservative: a
# floor at the nominal value can never produce a DRC violation), else omit
# and let KRT fall back to its tier -- reported as such.
_OVERRIDE_SOURCES: dict[str, tuple[str, str | None]] = {
    "clearance": ("min_clearance", "clearance_mm"),
    "track_width": ("min_track_width", "track_width_mm"),
    "via_diameter": ("min_via_diameter", "via_diameter_mm"),
    "via_drill": ("min_through_hole_diameter", "via_drill_mm"),
    "hole_to_hole": ("min_hole_to_hole", None),
    "annular": ("min_via_annular_width", None),
    "board_edge": ("min_copper_edge_clearance", None),
}

# KRT `narrowed[].kind` -> this project's canonical rule name (P1-18).
# "annular" here is always KRT's VIA annular ring (it only ever narrows
# vias, never a through-hole component pad) -- archived backlog P1-23 split the
# canonical `annular_ring_mm` bucket into a PTH-only number and a separate
# `via_annular_ring_mm`; this must point at the via one, never the PTH one.
_NARROWED_KIND_TO_CANONICAL: dict[str, str] = {
    "track_width": "track_width_mm",
    "via_diameter": "via_diameter_mm",
    "via_drill": "via_drill_mm",
    "annular": "via_annular_ring_mm",
}
_OVERRIDE_KEY_TO_CANONICAL: dict[str, str] = {
    "clearance": "clearance_mm",
    "track_width": "track_width_mm",
    "via_diameter": "via_diameter_mm",
    "via_drill": "via_drill_mm",
    "hole_to_hole": "hole_to_hole_mm",
    "annular": "via_annular_ring_mm",
    "board_edge": "edge_clearance_mm",
}


class KicadRoutingToolsError(RuntimeError):
    """Raised when KiCadRoutingTools can't be found or its subprocess fails/times out.

    When raised from a router run, the project-file disclosure is attached:
    `router_overwrote_project`, `project_restored`,
    `project_removed_router_created` (all None when not applicable)."""

    router_overwrote_project: bool | None = None
    project_restored: bool | None = None
    project_removed_router_created: bool | None = None


class KicadRoutingToolsTimeout(KicadRoutingToolsError):
    """The router subprocess exceeded its timeout and was killed. Raised only for a real
    timeout, never because some failure output contains the words "timed out". Carries the
    configured `timeout` and the measured `elapsed_s` plus the project-file disclosure."""

    timeout: float | None = None
    elapsed_s: float | None = None


# ---------------------------------------------------------------------------
# Discovery (mirrors kicad_cli.find_kicad_cli's shape: env override, then
# per-platform globs; there is no PATH step because it is not a console
# script).
# ---------------------------------------------------------------------------


def find_kicad_routing_tools() -> Path:
    """Resolve the KiCadRoutingTools checkout/install directory (the one
    containing `py_router/route.py`)."""
    env_override = os.environ.get(ENV_PATH)
    if env_override:
        path = Path(env_override)
        if (path / ROUTE_SCRIPT).is_file():
            return path
        raise KicadRoutingToolsError(
            f"{ENV_PATH}={env_override!r} does not contain {ROUTE_SCRIPT.as_posix()} -- it must point at "
            "the KiCadRoutingTools checkout directory itself, not at route.py or py_router/"
        )

    for pattern in _PLUGIN_GLOBS.get(sys.platform, []):
        matches = sorted(glob.glob(os.path.expanduser(pattern)), key=_natural_key, reverse=True)
        if matches:
            return Path(matches[0]).parent.parent

    raise KicadRoutingToolsError(f"KiCadRoutingTools not found. {INSTALL_CHECKOUT_HINT}.")


def _natural_key(path: str) -> list:
    """Sort key that ranks `KiCad/10.0/...` above `KiCad/9.0/...` -- a plain
    string sort puts "9" after "10" and would pick the older install (the
    same latent issue `kicad_cli.find_kicad_cli()`'s glob has once two
    major KiCad versions are installed side by side)."""
    return [int(tok) if tok.isdigit() else tok.lower() for tok in re.split(r"(\d+)", path)]


def find_kicad_routing_tools_python() -> Path:
    """The interpreter that runs route.py: `KICAD_ROUTING_TOOLS_PYTHON`, else
    this process's own `sys.executable`. It needs numpy/scipy/shapely/Pillow
    and a matching prebuilt `grid_router` binary -- see `check_availability`."""
    env_override = os.environ.get(ENV_PYTHON)
    if env_override:
        path = Path(env_override)
        if path.is_file():
            return path
        raise KicadRoutingToolsError(f"{ENV_PYTHON}={env_override!r} does not point to a file")
    return Path(sys.executable)


# Runs inside the KRT interpreter, never this process: KRT's route.py
# bootstraps sys.path from its own location, so the probe reproduces that
# layout rather than importing anything from the checkout into our process.
_PROBE_SCRIPT = r"""
import json, sys
checkout = sys.argv[1]
sys.path.insert(0, checkout + "/rust_router")
sys.path.insert(0, checkout + "/py_router")
out = {"python": sys.executable, "python_version": sys.version.split()[0], "missing_packages": [],
       "grid_router_error": None, "grid_router_version": None}
for pkg, mod in [("numpy", "numpy"), ("scipy", "scipy"), ("shapely", "shapely"), ("Pillow", "PIL")]:
    try:
        __import__(mod)
    except Exception as exc:  # noqa: BLE001
        out["missing_packages"].append(pkg)
try:
    import grid_router
    out["grid_router_version"] = getattr(grid_router, "__version__", "unknown")
except Exception as exc:  # noqa: BLE001
    out["grid_router_error"] = f"{type(exc).__name__}: {exc}"
print(json.dumps(out))
"""


def check_availability(*, timeout: int = 120) -> dict:
    """Evidence-gated availability check. Never raises.

    Returns `status: complete` with the resolved checkout/interpreter, or
    `status: incomplete` naming exactly what is missing (checkout, packages,
    or the prebuilt `grid_router` binary) and the command that installs it.
    """
    try:
        checkout = find_kicad_routing_tools()
    except KicadRoutingToolsError as exc:
        return {"status": "incomplete", "reason": str(exc), "install_hints": [INSTALL_CHECKOUT_HINT]}
    try:
        python = find_kicad_routing_tools_python()
    except KicadRoutingToolsError as exc:
        return {"status": "incomplete", "reason": str(exc), "checkout": str(checkout)}

    base = {"checkout": str(checkout), "python": str(python), "route_script": str(checkout / ROUTE_SCRIPT)}
    try:
        proc = subprocess.run(
            [str(python), "-c", _PROBE_SCRIPT, str(checkout)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"status": "incomplete", "reason": f"could not run {python}: {exc}", **base}

    probe: dict | None = None
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                probe = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if probe is None:
        return {
            "status": "incomplete",
            "reason": f"dependency probe produced no result (exit {proc.returncode}): {(proc.stderr or '').strip()[-500:]}",
            **base,
        }

    missing = list(probe.get("missing_packages") or [])
    grid_error = probe.get("grid_router_error")
    hints: list[str] = []
    reasons: list[str] = []
    if missing:
        reasons.append(f"missing Python package(s) in {python}: {', '.join(missing)}")
        hints.append(f"pip install {' '.join(missing)}  (into {python})")
    if grid_error:
        reasons.append(f"KRT's prebuilt Rust router (grid_router) failed to import: {grid_error}")
        hints.append(INSTALL_BINARY_HINT)
    result = {
        **base,
        "python_version": probe.get("python_version"),
        "missing_packages": missing,
        "grid_router_version": probe.get("grid_router_version"),
        "grid_router_error": grid_error,
    }
    if reasons:
        return {"status": "incomplete", "reason": "; ".join(reasons), "install_hints": hints, **result}
    return {"status": "complete", **result}


# ---------------------------------------------------------------------------
# Board floors -> --fab-overrides (the measured fix for the clearance gap).
# ---------------------------------------------------------------------------


def _mm(raw: Any) -> float | None:
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    text = str(raw).strip()
    if text.endswith("mm"):
        text = text[:-2]
    try:
        return float(text)
    except ValueError:
        return None


def _positive(value: float | None) -> float | None:
    return value if value is not None and value > 0 else None


def board_floor_overrides(pcb_path: str | Path) -> dict:
    """Derive KRT fab-override floors from the board's OWN rules (P1-18's
    `board_rules`, read-only), one entry per KRT override key.

    Returns `{"floors": {key: mm}, "sources": {key: source}, "omitted": [key, ...],
    "board_rules_status": ...}`. `omitted` keys have no board value at all
    and are left to KRT's tier -- reported, never silently defaulted.
    """
    from kicad_mcp.manufacturing_profiles.board_rules import read_board_design_rules

    rules = read_board_design_rules(pcb_path)
    project = rules.get("project_rules_raw") or {}
    netclasses = rules.get("netclasses") or []
    floors: dict[str, float] = {}
    sources: dict[str, str] = {}
    omitted: list[str] = []
    for key, (project_key, netclass_key) in _OVERRIDE_SOURCES.items():
        value = _positive(_mm(project.get(project_key)))
        if value is not None:
            floors[key] = value
            sources[key] = f"board_project_rules:{project_key}"
            continue
        if netclass_key:
            candidates = [
                (_positive(_mm(nc.get(netclass_key))), nc.get("name"))
                for nc in netclasses
            ]
            candidates = [(v, n) for v, n in candidates if v is not None]
            if candidates:
                # P4-07: this floor feeds --fab-overrides / --clearance under
                # escalation="board", whose whole contract is "a descent may
                # never cross what the board's own DRC enforces" -- and
                # KiCad's own DRC enforces the STRICTEST declared net class
                # for any pair involving it (max(classA, classB)), not the
                # loosest. `min()` here silently floored a multi-class board
                # (e.g. a Power_20A/HV class at 0.5mm alongside Default's
                # 0.2mm) at the loosest class's value, defeating this exact
                # workaround for exactly the boards it exists to protect --
                # confirmed on a real board (openMPPT v1.3): KRT then placed
                # copper as close as 0.04mm to a Power_20A-class pad, a real
                # kicad-cli DRC violation, while this function reported "0
                # violations" on the single-class golden-path board the
                # original fix was evaluated against (every candidate was
                # equal, so min()==max() there -- the bug was invisible until
                # a board with real class diversity exercised it).
                value, name = max(candidates, key=lambda c: c[0])
                floors[key] = value
                sources[key] = f"board_netclass:{name}"
                continue
        omitted.append(key)
    return {
        "floors": floors,
        "sources": sources,
        "omitted": omitted,
        "board_rules_status": rules.get("status"),
        "board_rules_warnings": rules.get("warnings", []),
    }


def write_fab_overrides(floors: dict[str, float], path: str | Path) -> Path:
    """Write a KRT `--fab-overrides` file (plain `key = value` text, not a
    KiCad file) pinning the floor to the board's own rules."""
    path = Path(path)
    lines = [
        "# Generated by kicad-mcp (P4-05): the board's OWN design rules as a hard fab floor,",
        "# so KiCadRoutingTools' rescue descents cannot go below what the board's DRC enforces.",
    ]
    for key, value in floors.items():
        lines.append(f"{key} = {value:g}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Invocation.
# ---------------------------------------------------------------------------


# Router scripts this wrapper may run. Each is followed by restoring the
# original project file (see `run_router_script`) and each is pinned in the
# CLI snapshot, so an upgrade that drops one fails a test, not a run.
ROUTER_SCRIPTS: tuple[str, ...] = ("route.py", "route_planes.py", "repair_planes.py")
SNAPSHOT_PATH = Path(__file__).with_name("krt_cli_snapshot.json")
SNAPSHOT_SCHEMA = 1

# Flags the wrapper itself owns. A caller cannot pass these through
# `advanced_flags`: strict sizes and the project-file guard are never
# optional, and the rest have first-class parameters or are derived.
_MANAGED_FLAGS = frozenset(
    {
        "--nets", "--escalation", "--no-fix-drc-settings", "--strict-sizes", "--clearance",
        "--fab-overrides", "--grid-step", "--max-iterations", "--json-out", "--layers",
        "--layer-costs", "--output", "--overwrite", "--help", "--capabilities",
    }
)
# Flags that take arbitrary output paths, write other project files, or
# rip/force existing copper outside the acceptance protocol. They exist in
# the router (so the snapshot lists them) but are REJECTED as advanced flags.
_DENIED_FLAGS: dict[str, str] = {
    "--preview-png": "writes an image to an arbitrary path",
    "--schematic-dir": "lets the router edit schematic files (pad-swap sync)",
    "--rip-existing-nets": "rips existing copper, outside the dry-run/confirm protocol",
    "--force-reroute": "force-reroutes already-connected nets, outside the protocol",
    "--relax-drc-severities": "weakens the DRC the board is graded against",
    "--undo": "reverts router output, not a routing lever",
    "--preview": "produces a preview instead of a routed board",
}
DEFAULT_POUR_LAYER_COST = 4.0  # KRT inventory: 3.0-6.0 for solid planes; signals 1.0.
STRICT_SIZES_EXIT_CODE = 3  # documented in `route.py --help`


def _top_level_forms(text: str, name: str) -> list[str]:
    """Raw text of every depth-1 `(name ...)` form of a `.kicad_pcb`
    (read-only; quoted strings are skipped so a paren inside a net name
    cannot unbalance the scan)."""
    forms: list[str] = []
    depth = 0
    start: int | None = None
    opener = re.compile(r"\(" + re.escape(name) + r"[\s)]")
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == '"':
            i += 1
            while i < n and text[i] != '"':
                i += 2 if text[i] == "\\" else 1
        elif c == "(":
            depth += 1
            if depth == 2 and opener.match(text, i):
                start = i
        elif c == ")":
            if depth == 2 and start is not None:
                forms.append(text[start:i + 1])
                start = None
            depth -= 1
        i += 1
    return forms


def _expand_layer_token(token: str, copper: list[str]) -> list[str]:
    """One zone layer token -> concrete copper layers of THIS board.
    KiCad writes `F&B.Cu` (front and back) and `*.Cu` (all copper); any
    other glob is matched against the board's copper layers. A token that
    names no copper layer of this board expands to nothing."""
    import fnmatch

    if "&" in token and token.endswith(".Cu"):
        names = [f"{part}.Cu" for part in token[: -len(".Cu")].split("&")]
        return [layer for layer in copper if layer in names]
    if any(ch in token for ch in "*?["):
        return [layer for layer in copper if fnmatch.fnmatchcase(layer, token)]
    return [token] if token in copper else []


def board_pour_info(pcb_path: str | Path) -> dict:
    """Read-only, raw-S-expression answer to "does this board have a copper
    pour, and on which layers": a board-level `(zone ...)` that is not a
    keepout rule area and carries a real (non-zero) net, on at least one
    layer that is copper on THIS board after wildcard expansion.
    Footprint-level zones are deliberately ignored. Also returns the
    board's copper layers in file order (the order KRT uses when `--layers`
    is omitted)."""
    text = Path(pcb_path).read_text(encoding="utf-8", errors="replace")
    copper: list[str] = []
    for layers_form in _top_level_forms(text, "layers"):
        copper = re.findall(r'\(\s*\d+\s+"?([^"\s)]+\.Cu)"?\s+(?:signal|power|mixed|jumper)', layers_form)
        break
    pour: set[str] = set()
    for zone in _top_level_forms(text, "zone"):
        bare = re.sub(r'"[^"]*"', '""', zone)  # a quoted name can never fake a keyword
        if "(keepout" in bare:
            continue
        net = re.search(r'\(net\s+(?:"([^"]*)"|(\d+))\s*\)', zone)
        if net is None or (net.group(1) is not None and not net.group(1)) or net.group(2) == "0":
            continue
        for fragment in re.findall(r"\(layers?\s+[^)]*\)", zone):
            tokens = [q or u for q, u in re.findall(r'"([^"]*)"|([^\s()"]+)', fragment)][1:]
            for token in tokens:
                pour.update(_expand_layer_token(token, copper))
    pour_layers = [layer for layer in copper if layer in pour]  # board order, intersected with copper
    return {"has_pour": bool(pour_layers), "pour_layers": pour_layers, "copper_layers": copper}


def _check_pour_layer_cost(value: float) -> float:
    """KRT treats a negative layer cost as FORBIDDEN (no track copper at all);
    that must never come from a typo here. Valid range is KRT's 1.0-1000."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 1.0 <= value <= 1000.0
    ):
        raise ValueError(f"pour_layer_cost must be a finite number in [1.0, 1000], got {value!r}")
    return float(value)


def layer_cost_plan(pour_info: dict | None, pour_layer_cost: float = DEFAULT_POUR_LAYER_COST) -> dict | None:
    """`{"layers": [...], "costs": [...]}` for `--layers`/`--layer-costs`, or
    None when the board has no pour: with nothing to shred there is no
    reason to emit layer costs."""
    pour_layer_cost = _check_pour_layer_cost(pour_layer_cost)
    if not pour_info or not pour_info.get("has_pour") or not pour_info.get("copper_layers"):
        return None
    pour = set(pour_info.get("pour_layers") or [])
    layers = list(pour_info["copper_layers"])
    return {"layers": layers, "costs": [pour_layer_cost if layer in pour else 1.0 for layer in layers]}


_PLAIN_NUMBER = re.compile(r"-?[0-9]+(?:\.[0-9]+)?")


def _flag_value_tokens(flag: str, value: Any) -> list[str]:
    """The argv tokens for one advanced flag's VALUE, validated. A token that
    starts with `-` is rejected unless it is a plain number: a value must
    never be able to introduce a flag (`--overwrite`, `--nets`, ...)."""
    items = list(value) if isinstance(value, (list, tuple)) else [value]
    tokens: list[str] = []
    for item in items:
        if isinstance(item, (bool, dict, list, tuple, set)) or item is None:
            raise ValueError(f"{flag}: value {item!r} must be a string or a finite number")
        if isinstance(item, float) and not math.isfinite(item):
            raise ValueError(f"{flag}: value {item!r} is not finite")
        token = str(item)
        if not token.strip():
            raise ValueError(f"{flag}: empty value")
        if token.startswith("-") and _PLAIN_NUMBER.fullmatch(token) is None:
            raise ValueError(f"{flag}: value {token!r} looks like a flag and cannot be passed as an advanced-flag value")
        tokens.append(token)
    return tokens


def _validate_advanced_flags(advanced_flags: dict[str, Any] | None) -> dict[str, list[str]]:
    """flag -> argv tokens after it (empty list = bare switch; omitted =
    False/None). Checks the flag against the pinned snapshot, the value's
    tokens, and the value count against the snapshot's recorded arity."""
    out: dict[str, list[str]] = {}
    if not advanced_flags:
        return out
    snapshot = load_cli_snapshot()
    route = snapshot["modules"]["route.py"]
    known = set(route["flags"])
    arity = route.get("arity", {})
    for flag, value in advanced_flags.items():
        if flag in _DENIED_FLAGS:
            raise ValueError(f"{flag} is denied as an advanced flag: {_DENIED_FLAGS[flag]}")
        if flag in _MANAGED_FLAGS:
            raise ValueError(f"{flag} is managed by the wrapper and cannot be passed as an advanced flag")
        if flag not in known:
            raise ValueError(
                f"{flag} is not a route.py flag in the pinned router snapshot "
                f"(version {snapshot.get('router_version')}); re-capture the snapshot after an upgrade"
            )
        if value is False or value is None:
            continue
        shape = arity.get(flag)
        if value is True:
            if shape in ("1", "+"):
                raise ValueError(f"{flag} needs a value, got a bare switch (arity {shape})")
            out[flag] = []
            continue
        tokens = _flag_value_tokens(flag, value)
        n = len(tokens)
        if shape == "0":
            raise ValueError(f"{flag} is a bare switch and takes no value, got {tokens}")
        if shape == "1" and n != 1:
            raise ValueError(f"{flag} takes exactly one value, got {n}")
        if shape == "+" and n < 1:
            raise ValueError(f"{flag} needs at least one value")
        out[flag] = tokens
    return out


def logged_parameters(
    *,
    escalation: str = "board",
    clearance_mm: float | None = None,
    fab_overrides: str | Path | None = None,
    grid_step_mm: float | None = None,
    max_iterations: int | None = None,
    nets: tuple[str, ...] = ("*",),
    pour_info: dict | None = None,
    pour_layer_cost: float = DEFAULT_POUR_LAYER_COST,
    advanced_flags: dict[str, Any] | None = None,
) -> dict:
    """Every parameter that shaped the router's argument list, for the run
    result. Layer costs say why they are absent when they are."""
    plan = layer_cost_plan(pour_info, pour_layer_cost)
    return {
        "escalation": escalation,
        "clearance_mm": clearance_mm,
        "fab_overrides": str(fab_overrides) if fab_overrides is not None else None,
        "grid_step_mm": grid_step_mm,
        "max_iterations": max_iterations,
        "nets": list(nets),
        "strict_sizes": True,
        "layer_costs": plan,
        "layer_costs_reason": "board has a copper pour" if plan else "no copper pour on the board; layer costs not emitted",
        "advanced_flags": dict(advanced_flags or {}),
    }


def build_route_args(
    input_pcb: str | Path,
    output_pcb: str | Path,
    *,
    escalation: str = "board",
    clearance_mm: float | None = None,
    fab_overrides: str | Path | None = None,
    grid_step_mm: float | None = None,
    max_iterations: int | None = None,
    json_out: str | Path | None = None,
    nets: tuple[str, ...] = ("*",),
    pour_info: dict | None = None,
    pour_layer_cost: float = DEFAULT_POUR_LAYER_COST,
    advanced_flags: dict[str, Any] | None = None,
) -> list[str]:
    """The argument list after the interpreter and `route.py`.

    `--no-fix-drc-settings` and `--strict-sizes` are unconditional (we never
    adopt KRT's project file, and a feature delivered below its requested
    size must exit 3 instead of passing silently). `--escalation fab` is
    the caller's explicit opt-in; `board` (default) pairs with a generated
    `--fab-overrides` floor, the measured fix for the clearance gap.
    `--layers`/`--layer-costs` appear only when `pour_info` says the board
    has a pour. Every other router flag goes through `advanced_flags`
    (flag -> value, `True` for a bare switch), is validated against the
    pinned CLI snapshot, and is echoed by `logged_parameters`.
    """
    if escalation not in VALID_ESCALATIONS:
        raise ValueError(f"escalation must be one of {VALID_ESCALATIONS}, got {escalation!r}")
    _check_pour_layer_cost(pour_layer_cost)
    for net in nets:
        if not isinstance(net, str) or not net.strip():
            raise ValueError(f"net pattern {net!r} must be a non-empty string")
        if net.startswith("-"):
            raise ValueError(f"net pattern {net!r} starts with '-' and would be parsed as a router flag")
    extra = _validate_advanced_flags(advanced_flags)
    args = [
        str(input_pcb), str(output_pcb), "--nets", *nets, "--escalation", escalation,
        "--no-fix-drc-settings", "--strict-sizes",
    ]
    if clearance_mm is not None:
        args += ["--clearance", f"{clearance_mm:g}"]
    if fab_overrides is not None:
        args += ["--fab-overrides", str(fab_overrides)]
    if grid_step_mm is not None:
        args += ["--grid-step", f"{grid_step_mm:g}"]
    if max_iterations is not None:
        args += ["--max-iterations", str(int(max_iterations))]
    if json_out is not None:
        args += ["--json-out", str(json_out)]
    plan = layer_cost_plan(pour_info, pour_layer_cost)
    if plan:
        args += ["--layers", *plan["layers"], "--layer-costs", *(f"{c:g}" for c in plan["costs"])]
    for flag, tokens in extra.items():
        args += [flag, *tokens]
    return args


# ---------------------------------------------------------------------------
# CLI-surface snapshot: version-keyed, one-way check of every emitted flag.
# ---------------------------------------------------------------------------


def load_cli_snapshot(path: str | Path | None = None) -> dict:
    """The pinned router CLI surface (`krt_cli_snapshot.json`). A missing or
    unparsable snapshot raises `KicadRoutingToolsError` (callers load it
    BEFORE a run so a finished run is never lost to it)."""
    target = Path(path or SNAPSHOT_PATH)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KicadRoutingToolsError(
            f"router CLI snapshot {target} is missing or unreadable ({exc}); "
            "re-capture it with capture_cli_snapshot() against the installed router"
        ) from exc


def router_version(checkout: str | Path) -> str | None:
    """The checkout's own `VERSION` file (the router's `--capabilities` output
    does not carry a version), or None if it has none."""
    try:
        text = (Path(checkout) / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return text or None


def compare_router_version(snapshot: dict, live_version: str | None) -> dict:
    pinned = snapshot.get("router_version")
    if live_version is None:
        return {"matches": False, "pinned": pinned, "live": None,
                "message": f"router version unknown (no VERSION file); snapshot pins {pinned}"}
    if live_version != pinned:
        return {"matches": False, "pinned": pinned, "live": live_version,
                "message": f"router version {live_version} differs from the snapshot's {pinned}: "
                           "re-capture the snapshot and re-run the flag check before trusting this run"}
    return {"matches": True, "pinned": pinned, "live": live_version, "message": "router version matches snapshot"}


def emittable_flags() -> set[str]:
    """Every `--flag` `build_route_args` can emit on its own, found by
    building an argument list with every optional input set (no hand-kept
    list to drift). `advanced_flags` are validated against the snapshot at
    call time instead."""
    probe = build_route_args(
        "in", "out", escalation="fab", clearance_mm=0.1, fab_overrides="f", grid_step_mm=0.1,
        max_iterations=1, json_out="j",
        pour_info={"has_pour": True, "pour_layers": ["F.Cu"], "copper_layers": ["F.Cu", "B.Cu"]},
    )
    return {a for a in probe if a.startswith("--")}


def check_emitted_flags(snapshot: dict) -> list[dict]:
    """One-way check: flags the wrapper can emit that the snapshot's
    `route.py` surface lacks, each `{"module", "flag"}` (empty list = ok)."""
    have = set(snapshot.get("modules", {}).get("route.py", {}).get("flags", []))
    return [{"module": "route.py", "flag": f} for f in sorted(emittable_flags() - have)]


_USAGE_OPTION = re.compile(r"\[(--[\w-]+)((?: [^\[\]]+)?(?: \[[^\[\]]+\.\.\.\])?)\]")


def parse_usage_arity(help_text: str) -> dict[str, str]:
    """flag -> "0" (bare switch), "1" (one value), "+" (one or more) or "*"
    (zero or more), read from argparse's `usage:` block. Everything before
    `usage:` (shims sometimes print a preamble) is ignored."""
    start = help_text.find("usage:")
    if start < 0:
        return {}
    block = help_text[start:]
    end = block.find("\n\n")
    usage = " ".join((block if end < 0 else block[:end]).split())
    arity: dict[str, str] = {}
    for first, second in re.findall(r"\[(--[\w-]+) \| (--[\w-]+)\]", usage):  # mutually exclusive switches
        arity[first] = arity[second] = "0"
    for flag, rest in _USAGE_OPTION.findall(usage):
        if flag in arity:  # already read as one half of an exclusive pair
            continue
        rest = rest.strip()
        if not rest:
            arity[flag] = "0"
        elif rest.startswith("["):
            arity[flag] = "*"
        elif "[" in rest:
            arity[flag] = "+"
        else:
            arity[flag] = "1"
    return arity


def capture_cli_snapshot(checkout: str | Path, python: str | Path, *, timeout: int = 120) -> dict:
    """Capture the router's CLI surface from the REAL install via
    `route.py --capabilities`. Run this (and save the result as
    `krt_cli_snapshot.json`) after a router upgrade; never write it by hand."""
    checkout = Path(checkout)
    proc = subprocess.run(
        [str(python), str(checkout / ROUTE_SCRIPT), "--capabilities"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        timeout=timeout, cwd=str(checkout),
    )
    out = proc.stdout or ""
    start = out.find("{")
    if start < 0:
        raise KicadRoutingToolsError(
            f"`route.py --capabilities` printed no JSON (exit {proc.returncode}): {(proc.stderr or '')[-500:]}"
        )
    caps = json.loads(out[start:])
    modules = {}
    for name in ROUTER_SCRIPTS:
        if name not in caps.get("flags", {}):
            raise KicadRoutingToolsError(f"router does not offer {name}; the wrapper runs it")
        modules[name] = {"flags": sorted(caps["flags"][name])}
    helped = subprocess.run(
        [str(python), str(checkout / ROUTE_SCRIPT), "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace", check=False,
        timeout=timeout, cwd=str(checkout),
    )
    arity = parse_usage_arity(helped.stdout or "")
    if not arity:
        raise KicadRoutingToolsError("could not read argument arity from `route.py --help`")
    modules["route.py"]["arity"] = {f: a for f, a in sorted(arity.items()) if f in modules["route.py"]["flags"]}
    lacking = [f for f in modules["route.py"]["flags"] if f not in modules["route.py"]["arity"]]
    if lacking:
        raise KicadRoutingToolsError(f"capture failed: no argument arity could be read for {lacking}")
    return {
        "snapshot_schema": SNAPSHOT_SCHEMA,
        "router_version": router_version(checkout),
        "captured_from": "route.py --capabilities",
        "modules": modules,
    }


_JSON_LINE_RE = re.compile(r"^(JSON_SUMMARY|JSON_SUMMARY_MIN|PROJECT_WRITES_JSON):\s*(\{.*\})\s*$", re.MULTILINE)
_PROJECT_WRITE_RE = re.compile(r"^\s+((?:rules|net_class\[[^\]]*\])\.[A-Za-z_]+):\s*(\S+)\s*->\s*(\S+)\s*mm", re.MULTILINE)


def parse_route_log(stdout: str, json_out_text: str | None = None) -> dict:
    """Extract KRT's self-report from its stdout (and its `--json-out` file
    when available).

    Diagnostics and disclosure only -- **never a ranking signal** (the real
    `kicad-cli` DRC scorer in toolsets/routing.py is). Real-log gotcha KRT
    itself documents in `--help`: when its end-of-run reconciliation fires
    it prints a SECOND `JSON_SUMMARY` scoped to the retried subset; the
    whole-run one carries `scope: "run"` and the `--json-out` file is the
    merged tally, so that file is preferred and the stdout fallback picks
    the `scope == "run"` summary, never the last line.
    """
    text = stdout or ""
    summaries: list[dict] = []
    summary_min: dict | None = None
    project_writes: dict | None = None
    for match in _JSON_LINE_RE.finditer(text):
        tag, payload = match.group(1), match.group(2)
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            continue
        if tag == "JSON_SUMMARY":
            summaries.append(data)
        elif tag == "JSON_SUMMARY_MIN":
            summary_min = data
        else:
            project_writes = data

    run_summary = next((s for s in summaries if s.get("scope") == "run"), summaries[0] if summaries else None)
    merged: dict | None = None
    if json_out_text:
        try:
            merged = json.loads(json_out_text)
        except json.JSONDecodeError:
            merged = None
    summary = merged or run_summary or {}

    failed_nets = list(summary.get("failed_single") or [])
    for entry in summary.get("failed_multipoint") or []:
        name = entry.get("net_name") if isinstance(entry, dict) else None
        if name and name not in failed_nets:
            failed_nets.append(name)
    design_rules = summary.get("design_rules") or {}
    narrowed_min: dict[str, float] = {}
    for entry in design_rules.get("narrowed") or []:
        kind = entry.get("kind")
        delivered = entry.get("delivered")
        if kind and isinstance(delivered, (int, float)):
            narrowed_min[kind] = min(narrowed_min.get(kind, delivered), float(delivered))
    delivered_below_requested = [
        {
            "net_name": e.get("net_name"),
            "kind": e.get("kind"),
            "requested_mm": e.get("requested"),
            "delivered_mm": e.get("delivered"),
            # The KRT mechanism that took the feature below size ("terminal neck",
            # "fine-pitch tap", "terminal escalation", "multipoint via kept at the
            # run size", ...): the cause per value (build ticket 37). Kept only when KRT
            # names it.
            **({"site": e["site"]} if e.get("site") else {}),
        }
        for e in (design_rules.get("narrowed") or [])
        if isinstance(e, dict)
        and isinstance(e.get("requested"), (int, float))
        and isinstance(e.get("delivered"), (int, float))
        and e["delivered"] < e["requested"] - 1e-9
    ]
    boxed_in = [b.get("net") for b in (summary.get("boxed_in") or []) if isinstance(b, dict) and b.get("net")]

    # Every `rules.X: a -> b mm` line KRT prints, whether from the in-run
    # floor pass (#650, fires even with --no-fix-drc-settings) or the
    # post-route "DRC settings: wrote" block -- all of them are writes to the
    # OUTPUT's sibling .kicad_pro that this project never adopts.
    project_rule_writes = [
        {"key": key, "from": before, "to": after} for key, before, after in _PROJECT_WRITE_RE.findall(text)
    ]
    return {
        "source": "json_out" if merged else ("stdout:scope=run" if run_summary else "none"),
        "summary_count_on_stdout": len(summaries),
        "router_reported_nets": list(summary.get("routed_single") or []),  # the router's own claim, not evidence of connectivity
        "failed_nets": failed_nets,
        "boxed_in_nets": boxed_in,
        "blockers": summary.get("blockers") or [],
        "total_vias": summary.get("total_vias"),
        "total_time_s": summary.get("total_time"),
        "min_clearance_used_mm": summary.get("min_clearance_used"),
        "escalation_policy": design_rules.get("escalation_policy"),
        "fab_tier": design_rules.get("fab_tier"),
        "board_floors_seen_by_router": design_rules.get("board_floors") or {},
        "narrowed_count": design_rules.get("count"),
        "delivered_below_requested": delivered_below_requested,
        "narrowed_min_delivered_mm": narrowed_min,
        "min_delivered_mm": design_rules.get("min_delivered") or {},
        "fab_tier_escalations": design_rules.get("fab_tier_escalations"),
        "rescue": {k: summary.get("rescue", {}).get(k) for k in ("attempted", "recovered", "unchanged", "improved")}
        if isinstance(summary.get("rescue"), dict)
        else {},
        "pad_pairs_open": summary.get("pad_pairs_open"),
        "summary_min": summary_min,
        "project_writes": (project_writes or {}).get("writes") if project_writes else [],
        "project_rule_writes": project_rule_writes,
        "fab_floor_relaxed": "FAB FLOOR RELAXED" in text,
    }


def stop_file_for(output_pcb: str | Path) -> Path:
    """Deterministic path for the graceful-stop request file a
    `run_kicad_routing_tools` call targeting `output_pcb` will watch.

    Named alongside `output_pcb` the same way `.krt_route.log`/
    `.krt_summary.json` already are, so it's derivable purely from the
    `output_pcb` argument already visible on the subprocess's own command
    line -- no new plumbing needed for an external process (another
    session, a person, a monitor script) to find and touch it while a run
    is in flight, without going through this Python function at all.
    """
    output_pcb = Path(output_pcb)
    return output_pcb.with_name(output_pcb.stem + ".krt_stop_request")


def stdout_log_for(output_pcb: str | Path) -> Path:
    """Deterministic path for the real, live-streamed stdout log a
    `run_kicad_routing_tools` call targeting `output_pcb` writes to (the
    `Popen`-based streaming added so a long run isn't buffered blind --
    see `run_kicad_routing_tools`'s own comment). Same derivable-from-
    `output_pcb`-alone convention as `stop_file_for`.
    """
    output_pcb = Path(output_pcb)
    return output_pcb.with_name(output_pcb.stem + ".krt_route.log")


# Net-level progress: "[i/n] Routing <NetName> (id=X)", optionally with a
# "(K failed)" qualifier inside the brackets -- which net (of the whole
# run's net list) KRT is currently attempting. Named `idx`/`total` groups
# added here (kept backward compatible -- `parse_krt_blocking_lines` in
# toolsets/routing.py only ever reads `.group("net")` off the same pattern)
# so this module doesn't need a second, drifting copy of it.
_KRT_NET_PROGRESS_RE = re.compile(
    r"^\[(?P<idx>\d+)/(?P<total>\d+)(?:\s*\(\d+\s*failed\))?\]\s*Routing\s+(?P<net>.+?)\s*\(id=\d+\)\s*$"
)
# Edge-level progress WITHIN the current net: "[N/M] (K failed) Routing MST
# edge: pad A -> pad B (length=...mm) target=(...)" -- confirmed real,
# 2026-09-13/14, on actual openMPPT and krt_stress_congestion runs. A large
# multi-pad net (e.g. a 248-edge power/GND net) can spend most of a run's
# wall-clock time here -- exactly the case krt-capability-suite's ticket 06
# found defeats a quick `request_graceful_stop()` (KRT only checks its stop
# file between nets, never between MST edges).
#
# The "(K failed)" qualifier is CONDITIONAL, same as the net-level line's --
# confirmed by a real run against `krt_stress_congestion.kicad_pcb` (2026-
# 09-14): every edge line before the first in-net failure reads plain
# "[1/8] Routing MST edge: ..." with no qualifier at all; it only appears
# once K>0. An earlier version of this regex required it unconditionally,
# which silently matched zero edge lines on any net that hadn't failed yet
# -- caught by this real run, not by `--help` reading.
_KRT_EDGE_PROGRESS_RE = re.compile(
    r"^\[(?P<idx>\d+)/(?P<total>\d+)\]\s*(?:\((?P<failed>\d+)\s*failed\)\s*)?Routing MST edge:"
)


def parse_krt_progress(log_text: str) -> dict:
    """Extract the most recent real progress signal from KRT's streamed
    router log text -- "which net, and how far into that net's MST edges"
    -- built (krt-capability-suite ticket 10) so a caller deciding whether
    to `request_graceful_stop()` an in-flight run can see real position-in-
    progress instead of waiting blind.

    Two distinct, independently-occurring progress lines exist in KRT's
    real log (confirmed by directly reading real log text, not assumed):
    net-level (`[i/n] Routing <NetName> (id=X)`) and edge-level (`[N/M]
    (K failed) Routing MST edge: ...`, only present for nets with more than
    one internal connection -- a single-pad or two-pad net may log no edge
    lines at all). `edge` resets to `None` each time a new net-level line
    is seen, since a leftover edge count from the PREVIOUS net would
    misrepresent progress on the current one.

    Deliberately reports position only, never a fabricated ETA -- KRT gives
    no reliable time-per-edge guarantee; this same project's own real log
    evidence shows a single edge search ranging from tens to hundreds of
    thousands of iterations depending on how contested the cell is.

    Returns `{"net": {...} | None, "edge": {...} | None}`. Both `None` on
    text with no recognizable progress lines yet (e.g. an empty or
    just-started log).
    """
    net_progress: dict | None = None
    edge_progress: dict | None = None
    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        net_match = _KRT_NET_PROGRESS_RE.match(line)
        if net_match:
            net_progress = {
                "net_index": int(net_match.group("idx")),
                "net_total": int(net_match.group("total")),
                "net_name": net_match.group("net").strip(),
            }
            edge_progress = None
            continue
        edge_match = _KRT_EDGE_PROGRESS_RE.match(line)
        if edge_match:
            failed = edge_match.group("failed")
            edge_progress = {
                "edge_index": int(edge_match.group("idx")),
                "edge_total": int(edge_match.group("total")),
                "edges_failed": int(failed) if failed is not None else 0,
            }
    return {"net": net_progress, "edge": edge_progress}


def krt_run_progress(output_pcb: str | Path) -> dict:
    """Real-time progress snapshot for a `run_kicad_routing_tools` run
    targeting `output_pcb`, read from its live streamed stdout log
    (`stdout_log_for`) -- safe to call whether that run is still in
    flight, already finished, or hasn't started yet.

    Pairs with `request_graceful_stop`: check this first to see whether a
    stop request is likely to land quickly (between nets) or sit for a
    while (mid-way through a large net's MST edges) before deciding to
    wait it out or escalate to a hard kill.

    Returns `{"log_found": bool, "net": {...} | None, "edge": {...} | None}`
    -- `log_found=False` (with `net`/`edge` both `None`) when the log
    doesn't exist yet (run hasn't started, or hasn't been given this exact
    `output_pcb` path). See `parse_krt_progress` for what `net`/`edge`
    actually mean.
    """
    log_path = stdout_log_for(output_pcb)
    if not log_path.is_file():
        return {"log_found": False, "net": None, "edge": None}
    text = log_path.read_text(encoding="utf-8", errors="replace")
    return {"log_found": True, **parse_krt_progress(text)}


def request_graceful_stop(output_pcb: str | Path, reason: str = "") -> Path:
    """Request a graceful stop of an in-progress `run_kicad_routing_tools`
    run targeting `output_pcb`, instead of waiting for (or forcing) a hard
    kill.

    KRT's own `route.py` checks for this file before starting each next
    net (real behavior, confirmed 2026-09-13 by running it directly with a
    pre-created stop file against `tests/fixtures/simple_led.kicad_pcb`:
    it printed "CHECKPOINT STOP (KICAD_STOP_FILE): ... exists; writing
    partial board", exited 0, and the output loaded cleanly via
    `kicad_tools.schema.PCB.load` -- a real, valid, non-corrupted board,
    not an error). Whatever was routed before the request lands is kept;
    nothing already committed gets thrown away, unlike `timeout`'s hard
    `proc.kill()`.

    Safe to call whether or not a run is currently in progress against
    this `output_pcb` -- it just writes a file KRT checks for
    opportunistically; if no run is watching it (yet, or ever), it sits
    inert until one starts and notices it. NOTE: `run_kicad_routing_tools`
    does NOT clear a leftover file from a previous run at the same output
    path before starting a new one -- a stale request there WILL cause the
    new run to also stop early. This project's own trial-copy discipline
    already gives every attempt a fresh temp directory, so this shouldn't
    come up in practice; a caller that deliberately reuses an output path
    across separate runs should delete `stop_file_for(output_pcb)` itself
    first if that's not the intent.
    """
    path = stop_file_for(output_pcb)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"graceful stop requested{f': {reason}' if reason else ''}\n", encoding="utf-8")
    return path


def run_kicad_routing_tools(
    input_pcb: str | Path,
    output_pcb: str | Path,
    *,
    escalation: str = "board",
    clearance_mm: float | None = None,
    fab_overrides: str | Path | None = None,
    grid_step_mm: float | None = None,
    max_iterations: int | None = None,
    timeout: int = 300,
    nets: tuple[str, ...] = ("*",),
    stop_after: str | None = None,
    advanced_flags: dict[str, Any] | None = None,
    pour_layer_cost: float = DEFAULT_POUR_LAYER_COST,
) -> dict:
    """Run KRT's `route.py` on `input_pcb` through `run_router_script`.

    `input_pcb` must be a disposable copy: KRT reads its sibling `.kicad_pro`
    for netclass defaults and rewrites the OUTPUT's sibling `.kicad_pro`.
    The original project file is restored before this returns or raises.

    What goes on the command line: `--strict-sizes` always (exit 3, surfaced
    as `strict_sizes_violated`); `--layers/--layer-costs` only when the board
    has a pour (`pour_layer_cost` in [1, 1000]); `advanced_flags` explicit,
    validated against the pinned snapshot and echoed under
    `logged_parameters`. The snapshot is loaded BEFORE the run so a missing
    one can never cost a finished run.

    Bounded by `timeout`; on expiry the child is killed (all progress lost).
    Call `request_graceful_stop(output_pcb)` (from this process, another
    session or an operator) to ask KRT to finish cleanly before its next net
    and write out a real, loadable partial board; `stop_after` sets the
    checkpoint granularity (`None`: before the very next net, the finest;
    `"bus"`: after the current bus-net group).

    Raises `KicadRoutingToolsError` on discovery failure, missing snapshot,
    timeout or no output file; otherwise returns the parsed self-report.
    """
    snapshot = load_cli_snapshot()  # first: a missing snapshot must not cost a finished run
    checkout = find_kicad_routing_tools()
    input_pcb = Path(input_pcb)
    output_pcb = Path(output_pcb)
    json_out = output_pcb.with_name(output_pcb.stem + ".krt_summary.json")
    pour_info = board_pour_info(input_pcb) if input_pcb.is_file() else None
    route_args = build_route_args(
        input_pcb,
        output_pcb,
        escalation=escalation,
        clearance_mm=clearance_mm,
        fab_overrides=fab_overrides,
        grid_step_mm=grid_step_mm,
        max_iterations=max_iterations,
        json_out=json_out,
        nets=nets,
        pour_info=pour_info,
        pour_layer_cost=pour_layer_cost,
        advanced_flags=advanced_flags,
    )
    run = run_router_script(
        "route.py", route_args, output_pcb=output_pcb, original_pcb=input_pcb, timeout=timeout, stop_after=stop_after,
    )
    json_text = json_out.read_text(encoding="utf-8") if json_out.is_file() else None
    log = parse_route_log(run.pop("stdout_text"), json_text)
    version = router_version(checkout)
    run.update(
        {
            "log": log,
            "logged_parameters": logged_parameters(
                escalation=escalation, clearance_mm=clearance_mm, fab_overrides=fab_overrides,
                grid_step_mm=grid_step_mm, max_iterations=max_iterations, nets=nets,
                pour_info=pour_info, pour_layer_cost=pour_layer_cost, advanced_flags=advanced_flags,
            ),
            "router_version": version,
            "version_check": compare_router_version(snapshot, version),
            # `--strict-sizes` exits 3 when a feature was delivered below its
            # requested size or a fab-tier escalation fired. Not an error
            # (the board was still written); it is a disclosure.
            "strict_sizes_violated": run["exit_code"] == STRICT_SIZES_EXIT_CODE
            or bool(log.get("delivered_below_requested")),
            "delivered_below_requested": log.get("delivered_below_requested", []),
        }
    )
    return run


# ---------------------------------------------------------------------------
# KRT_PARSE_GAP (issue 2): footprints whose pads KRT's parser did not read.
#
# With unquoted pad names an old KRT parser read 0 pads for a connector, logged
# "Skipping N already-routed net(s): Only 1 pad(s)" and reported `failed_nets []`: it never
# attempted those nets and said nothing. The guard compares, per footprint, the raw `(pad ...)`
# count in the file's own S-expressions with the count KRT's `parse_kicad_pcb` returned. It only
# reports; the board is never edited and the router is never stopped.
# ---------------------------------------------------------------------------

KRT_PARSE_GAP = "KRT_PARSE_GAP"

# Runs inside the KRT interpreter (same sys.path layout as route.py), never this process.
_PARSE_PADS_SCRIPT = r"""
import json, sys
checkout, pcb = sys.argv[1], sys.argv[2]
sys.path.insert(0, checkout + "/rust_router")
sys.path.insert(0, checkout + "/py_router")
from kicad_parser import parse_kicad_pcb
data = parse_kicad_pcb(pcb)
print(json.dumps({"footprints": [[fp.reference, len(fp.pads)] for fp in data.footprints.values()]}))
"""


def _group(pairs: list[tuple[str, int]]) -> dict[str, list[int]]:
    out: dict[str, list[int]] = {}
    for reference, count in pairs:
        out.setdefault(reference, []).append(count)
    return {reference: sorted(counts, reverse=True) for reference, counts in out.items()}


def raw_pad_counts(pcb_path: str | Path) -> dict[str, list[int]]:
    """Per footprint reference, the number of ``(pad ...)`` nodes in the raw file (one entry per
    footprint carrying that reference, largest first). Read from the S-expression tree, never from
    a ``kicad_tools`` model."""
    from kicad_mcp import raw_geometry, write_layer

    root = write_layer.load(pcb_path)
    pairs = [
        (raw_geometry._reference(fp), len([c for c in fp.children if not c.is_atom and c.name == "pad"]))
        for fp in root.children
        if not fp.is_atom and fp.name == "footprint"
    ]
    return _group(pairs)


def parsed_pad_counts(pcb_path: str | Path, *, timeout: int = 120) -> dict[str, list[int]]:
    """Per footprint reference, the number of pads KRT's own ``parse_kicad_pcb`` returned.
    Raises ``KicadRoutingToolsError`` when KRT cannot be run or answers nothing usable."""
    checkout = find_kicad_routing_tools()
    python = find_kicad_routing_tools_python()
    try:
        proc = subprocess.run(
            [str(python), "-c", _PARSE_PADS_SCRIPT, str(checkout), str(pcb_path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", check=False, timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise KicadRoutingToolsError(f"KRT pad parse could not run: {exc}") from exc
    for line in reversed((proc.stdout or "").splitlines()):
        line = line.strip()
        if line.startswith("{"):
            try:
                return _group([(str(r), int(n)) for r, n in json.loads(line)["footprints"]])
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                break
    tail = (proc.stderr or "").strip()[-300:]
    raise KicadRoutingToolsError(f"KRT pad parse answered nothing usable (exit {proc.returncode}): {tail}")


def krt_parse_gap(raw: dict[str, list[int]], parsed: dict[str, list[int]]) -> list[dict]:
    """The footprints KRT read fewer pads of than the raw file holds, one named gap each. A
    footprint KRT did not return at all counts as 0 parsed pads. More parsed than raw is no gap."""
    gaps = []
    for reference in sorted(raw):
        seen = parsed.get(reference, [])
        for i, raw_n in enumerate(raw[reference]):
            parsed_n = seen[i] if i < len(seen) else 0
            if parsed_n < raw_n:
                gaps.append({"code": KRT_PARSE_GAP, "reference": reference, "raw_pads": raw_n, "parsed_pads": parsed_n})
    return gaps


def _file_state(path: Path) -> tuple[int, int] | None:
    """(mtime_ns, size) of a file, or None if absent -- the pre-run snapshot
    used to tell a router-created file from one that was already there."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _restore_original_project(
    original_project: Path,
    original_bytes: bytes | None,
    output_project: Path,
    output_state_before: tuple[int, int] | None = None,
) -> dict:
    """Put the original `.kicad_pro` back beside the output and the input.
    Never rewrites a file whose bytes already match. With no original, a
    project file the router CREATED is removed (and disclosed): nothing may
    adopt it. "Created" means it did not exist, or differs in mtime/size from
    `output_state_before`, at the pre-run snapshot -- a pre-existing,
    unchanged file is never unlinked. Never raises: a failure is returned as
    `restore_error`."""
    present_before_restore = output_project.is_file()
    overwrote = False
    removed = False
    error: str | None = None
    try:
        if original_bytes is not None:
            for target in (output_project, original_project):
                exists = target.is_file()
                if exists and target.read_bytes() == original_bytes:
                    continue
                if exists or target == original_project:
                    overwrote = True  # router changed or deleted a file that had to stay
                target.write_bytes(original_bytes)
        else:
            # the input's sibling did not exist before the run, so anything
            # there now is router-made; the output's is router-made unless it
            # was already there and is unchanged.
            for target in (output_project, original_project):
                if not target.is_file():
                    continue
                if target == output_project and output_state_before is not None:
                    # pre-existing file: never unlink it (no bytes to restore
                    # either) -- only disclose that the router changed it.
                    if _file_state(target) != output_state_before:
                        overwrote = True
                    continue
                overwrote = True
                removed = True
                target.unlink()
    except OSError as exc:  # pragma: no cover - disk-level failure
        error = f"{type(exc).__name__}: {exc}"
    return {
        "project_file_written_by_router": present_before_restore,
        "router_overwrote_project": overwrote,
        "project_restored": error is None,
        "project_removed_router_created": removed,
        "restore_error": error,
    }


def _reap(proc: "subprocess.Popen") -> None:
    """Terminate and reap a router child; never leaves it running."""
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait()
    except Exception:  # noqa: BLE001 - reaping must not mask the real error
        pass


def run_router_script(
    script: str,
    script_args: list[str],
    *,
    output_pcb: str | Path,
    original_pcb: str | Path,
    timeout: int = 300,
    stop_after: str | None = None,
) -> dict:
    """Run one pinned KRT script (`ROUTER_SCRIPTS`) as a subprocess, then put
    the original project file back before returning -- for EVERY script, not
    just `route.py`. KRT rewrites the OUTPUT's sibling `.kicad_pro` (and may
    touch the input's) even with `--no-fix-drc-settings`; the bytes of the
    input's `.kicad_pro` are captured before the run and written back beside
    the output (and the input) afterwards, so nothing downstream ever grades
    against the router's rewritten rules.

    Returns the raw run record plus `stdout_text` (callers parse it).
    """
    if script not in ROUTER_SCRIPTS:
        raise ValueError(f"{script} is not a pinned router script; expected one of {ROUTER_SCRIPTS}")
    checkout = find_kicad_routing_tools()
    python = find_kicad_routing_tools_python()
    original_pcb = Path(original_pcb)
    output_pcb = Path(output_pcb)
    output_pcb.parent.mkdir(parents=True, exist_ok=True)
    args = [str(python), str(checkout / "py_router" / script), *script_args]
    original_project = original_pcb.with_suffix(".kicad_pro")
    original_project_bytes = original_project.read_bytes() if original_project.is_file() else None
    output_state_before = _file_state(output_pcb.with_suffix(".kicad_pro"))
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("PYTHONUTF8", "1")

    # Wire up the graceful-stop path (see request_graceful_stop()'s
    # docstring for the real, verified evidence this works). Always set,
    # inert unless something actually touches the file. Deliberately NOT
    # cleared here: a caller that wants THIS run to start already resigned
    # to stopping quickly (`request_graceful_stop()` before calling this
    # function) needs it honored, and that's indistinguishable from a
    # genuinely stale leftover from an unrelated past run at the same
    # path -- this project's own trial-copy discipline already gives every
    # attempt a fresh temp directory (see `_trial_copy`), so path reuse
    # across unrelated runs shouldn't happen in practice; if a caller does
    # reuse a path on purpose, clearing `stop_file_for(output_pcb)` first is
    # on them.
    stop_file = stop_file_for(output_pcb)
    env["KICAD_STOP_FILE"] = str(stop_file)
    env.setdefault("KICAD_STOP_CLEANUP", "1")
    if stop_after:
        env["KICAD_STOP_AFTER"] = stop_after

    # Stream stdout/stderr to real files on disk instead of buffering them in
    # memory via `capture_output=True` -- a run against a hard board can take
    # 17-90+ minutes (real, measured on openMPPT v1.3), during which the old
    # buffered approach gave zero visibility: nothing was readable until the
    # process exited, so a caller/session could only watch OS-level CPU time
    # and had no way to tell real progress from a silent stall. `route.py`
    # itself is genuinely verbose (bus-corridor planning, blocking-analysis
    # breakdowns, GND-via placement stats print as it runs) -- that output
    # now lands in `stdout_log`/`stderr_log` live, named predictably next to
    # the trial output so a session can find and tail them without knowing an
    # internal temp path in advance. Also survives a timeout kill, where the
    # old buffered call could lose whatever output existed at kill time.
    if script == "route.py":
        stdout_log = stdout_log_for(output_pcb)
        stderr_log = output_pcb.with_name(output_pcb.stem + ".krt_route.stderr.log")
    else:
        tag = Path(script).stem
        stdout_log = output_pcb.with_name(f"{output_pcb.stem}.krt_{tag}.log")
        stderr_log = output_pcb.with_name(f"{output_pcb.stem}.krt_{tag}.stderr.log")
    started = time.monotonic()  # a duration: never the adjustable wall clock (build ticket 54)
    returncode: int | None = None
    start_error: OSError | None = None
    restore: dict = {}
    try:
        try:
            with open(stdout_log, "w", encoding="utf-8", errors="replace") as out_f, \
                 open(stderr_log, "w", encoding="utf-8", errors="replace") as err_f:
                # route.py must run from the checkout root as a script (it
                # bootstraps its own sys.path relative to itself); cwd=checkout
                # matches the invocation the evaluation verified.
                proc = subprocess.Popen(
                    args, cwd=str(checkout), stdout=out_f, stderr=err_f, env=env,
                )
                try:
                    returncode = proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    _reap(proc)  # returncode stays None to signal "timed out"
                except BaseException:
                    _reap(proc)  # KeyboardInterrupt etc.: never orphan the router
                    raise
        except OSError as exc:
            start_error = exc
    finally:
        # Restore the original project file on EVERY exit path (success,
        # timeout, missing output, KeyboardInterrupt, any exception), before
        # anything downstream can read it. Disclosure rides on the result or
        # on the raised KicadRoutingToolsError.
        restore = _restore_original_project(
            original_project, original_project_bytes, output_pcb.with_suffix(".kicad_pro"), output_state_before
        )
    elapsed = time.monotonic() - started

    def _failure(message: str, cls: type = KicadRoutingToolsError) -> KicadRoutingToolsError:
        error = cls(
            message
            + f"\nproject file: router_overwrote_project={restore['router_overwrote_project']}, "
            f"restored={restore['project_restored']}, removed_router_created={restore['project_removed_router_created']}"
        )
        error.router_overwrote_project = restore["router_overwrote_project"]
        error.project_restored = restore["project_restored"]
        error.project_removed_router_created = restore["project_removed_router_created"]
        return error

    if start_error is not None:
        raise _failure(f"could not start KiCadRoutingTools via {python}: {start_error}") from start_error

    stdout_text = stdout_log.read_text(encoding="utf-8", errors="replace") if stdout_log.is_file() else ""
    stderr_text = stderr_log.read_text(encoding="utf-8", errors="replace") if stderr_log.is_file() else ""

    if returncode is None:
        tail = "\n".join(stdout_text.splitlines()[-25:])
        timeout_error = _failure(
            f"KiCadRoutingTools timed out after {timeout}s (process killed).\n"
            f"stdout log ({stdout_log}) tail:\n{tail}\nstderr log ({stderr_log}) tail:\n{stderr_text[-2000:]}",
            KicadRoutingToolsTimeout,
        )
        timeout_error.timeout = timeout
        timeout_error.elapsed_s = elapsed
        raise timeout_error

    if not output_pcb.is_file():
        tail = "\n".join(stdout_text.splitlines()[-25:])
        raise _failure(
            f"KiCadRoutingTools did not produce {output_pcb} (exit {returncode}).\n"
            f"stdout tail:\n{tail}\nstderr:\n{stderr_text[-2000:]}"
        )
    return {
        "script": script,
        "exit_code": returncode,
        "elapsed_s": round(elapsed, 2),
        "args": args[2:],
        "output_pcb": str(output_pcb),
        "project_file_written_by_router": restore["project_file_written_by_router"],
        "router_overwrote_project": restore["router_overwrote_project"],
        "project_restored": restore["project_restored"],
        "project_removed_router_created": restore["project_removed_router_created"],
        "stdout_text": stdout_text,
        "stderr_tail": stderr_text[-1000:],
        "stdout_log": str(stdout_log),
        "stderr_log": str(stderr_log),
        # Where an external caller (this session, another one, a person)
        # can `request_graceful_stop()` this run while it's in flight --
        # present even on success, so the path is discoverable before a
        # NEXT run at this same output path, not just reactively.
        "stop_file": str(stop_file),
        # Whether a graceful stop was actually requested and honored
        # during THIS run (vs. running to natural completion or being
        # hard-killed on timeout) -- real signal, not inferred from
        # exit_code alone, since a stopped-early run still exits 0.
        "graceful_stop_honored": "CHECKPOINT STOP (KICAD_STOP_FILE)" in stdout_text,
    }


# ---------------------------------------------------------------------------
# Delta: what KRT added, as add_trace/add_via specs -- confirm never copies
# KRT's file over the board.
# ---------------------------------------------------------------------------


def _round_point(x: float, y: float, ndigits: int = 4) -> tuple[float, float]:
    return (round(x, ndigits), round(y, ndigits))


def _segment_key(seg: Any, origin: tuple[float, float]) -> tuple:
    ox, oy = origin
    a = _round_point(seg.start[0] + ox, seg.start[1] + oy)
    b = _round_point(seg.end[0] + ox, seg.end[1] + oy)
    lo, hi = (a, b) if a <= b else (b, a)
    return (seg.layer, lo, hi, round(float(seg.width), 4), seg.net_name or "")


def _via_key(via: Any, origin: tuple[float, float]) -> tuple:
    ox, oy = origin
    return (_round_point(via.position[0] + ox, via.position[1] + oy), via.net_name or "")


def route_delta(original_pcb: str | Path, routed_pcb: str | Path) -> dict:
    """Diff a KRT-routed trial copy against the original board through
    `kicad_tools` and return the added copper as board-local specs in the
    ORIGINAL board's frame -- the exact inputs `PCB.add_trace`/`add_via`
    take (see module docstring point 3 for the coordinate reasoning).

    Also reports copper that vanished (`removed_*`): KRT is not asked to rip
    existing nets, so anything here would be a real finding, not an
    expected effect.
    """
    from kicad_tools.schema import PCB

    original = PCB.load(original_pcb)
    routed = PCB.load(routed_pcb)
    orig_origin = tuple(original.board_origin)
    routed_origin = tuple(routed.board_origin)

    original_segments = {_segment_key(s, orig_origin) for s in original.segments}
    original_vias = {_via_key(v, orig_origin) for v in original.vias}
    routed_segments: set[tuple] = set()
    routed_vias: set[tuple] = set()

    ox, oy = orig_origin
    rx, ry = routed_origin
    trace_specs: list[dict] = []
    for seg in routed.segments:
        key = _segment_key(seg, routed_origin)
        routed_segments.add(key)
        if key in original_segments:
            continue
        trace_specs.append(
            {
                "start": [seg.start[0] + rx - ox, seg.start[1] + ry - oy],
                "end": [seg.end[0] + rx - ox, seg.end[1] + ry - oy],
                "width_mm": float(seg.width),
                "layer": seg.layer,
                "net": seg.net_name or None,
            }
        )
    via_specs: list[dict] = []
    for via in routed.vias:
        key = _via_key(via, routed_origin)
        routed_vias.add(key)
        if key in original_vias:
            continue
        via_specs.append(
            {
                "x": via.position[0] + rx - ox,
                "y": via.position[1] + ry - oy,
                "net": via.net_name or None,
                "size_mm": float(via.size),
                "drill_mm": float(via.drill),
                "layers": list(via.layers) if getattr(via, "layers", None) else ["F.Cu", "B.Cu"],
            }
        )
    return {
        "trace_specs": trace_specs,
        "via_specs": via_specs,
        "removed_segment_count": len(original_segments - routed_segments),
        "removed_via_count": len(original_vias - routed_vias),
        "original_board_origin": list(orig_origin),
        "routed_board_origin": list(routed_origin),
    }


def descents_below_floor(log: dict, floors: dict[str, float]) -> list[dict]:
    """Where KRT's own self-report says it delivered copper below the board's
    floor -- clearance from `min_clearance_used`, sizes from the `narrowed`
    list. Real signal from the tool, used for diagnosis/disclosure."""
    descents: list[dict] = []
    used = log.get("min_clearance_used_mm")
    floor = floors.get("clearance")
    if isinstance(used, (int, float)) and floor is not None and used < floor - 1e-6:
        descents.append({"rule": "clearance", "board_floor_mm": floor, "delivered_mm": used})
    for kind, delivered in (log.get("narrowed_min_delivered_mm") or {}).items():
        floor = floors.get(kind)
        if floor is not None and delivered < floor - 1e-6:
            descents.append({"rule": kind, "board_floor_mm": floor, "delivered_mm": delivered})
    return descents


def delivered_rules_overlay(board_rules_result: dict, log: dict) -> dict:
    """A `board_rules`-shaped result whose effective rules are lowered to what
    KRT actually delivered (per its self-report), so `compare_to_profile` can
    grade the ROUTED COPPER against a fab floor rather than the untouched
    project rules. Sources are labelled so nothing reads as a designer's
    choice. Used for the `fab` policy's mandatory manufacturability check."""
    overlay = dict(board_rules_result)
    effective = {k: dict(v) for k, v in (board_rules_result.get("effective_rules") or {}).items()}
    delivered: dict[str, float] = {}
    used = log.get("min_clearance_used_mm")
    if isinstance(used, (int, float)):
        delivered["clearance_mm"] = float(used)
    for kind, value in (log.get("narrowed_min_delivered_mm") or {}).items():
        canonical = _NARROWED_KIND_TO_CANONICAL.get(kind)
        if canonical:
            delivered[canonical] = float(value)
    for canonical, value in delivered.items():
        current = effective.get(canonical, {}).get("value_mm")
        if current is None or value < current - 1e-9:
            candidates = dict(effective.get(canonical, {}).get("candidates_mm") or {})
            candidates["kicad_routing_tools:delivered_copper"] = value
            effective[canonical] = {
                "value_mm": value,
                "source": "kicad_routing_tools:delivered_copper",
                "candidates_mm": candidates,
            }
    overlay["effective_rules"] = effective
    overlay["delivered_overlay"] = delivered
    return overlay


__all__ = [
    "ENV_PATH",
    "ENV_PYTHON",
    "KicadRoutingToolsError",
    "VALID_ESCALATIONS",
    "board_floor_overrides",
    "build_route_args",
    "check_availability",
    "delivered_rules_overlay",
    "descents_below_floor",
    "find_kicad_routing_tools",
    "find_kicad_routing_tools_python",
    "ROUTER_SCRIPTS",
    "board_pour_info",
    "capture_cli_snapshot",
    "check_emitted_flags",
    "parse_usage_arity",
    "compare_router_version",
    "layer_cost_plan",
    "load_cli_snapshot",
    "logged_parameters",
    "parse_route_log",
    "route_delta",
    "router_version",
    "run_kicad_routing_tools",
    "run_router_script",
    "write_fab_overrides",
]
