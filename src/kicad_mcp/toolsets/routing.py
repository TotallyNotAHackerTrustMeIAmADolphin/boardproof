"""Routing toolset: track/via placement, ratsnest queries, and a
Freerouting-backed autorouting proposal/confirm checkpoint.

See archived backlog "Phase 4 -- Routing" (P4-01, P4-02) and docs/ARCHITECTURE.md's
"Human-in-the-loop" section -- Freerouting output is a **proposal**,
never committed directly, per the dry-run+revision-token pattern (the
first tool in this codebase to actually implement that pattern for a
real "require a human checkpoint" case, rather than just describing it
-- see docs/ARCHITECTURE.md's 2026-09-02 status-honesty note).

Freerouting is GPLv3 -- invoked only as a loopback-only local
subprocess (java -jar freerouting.jar), never imported as a library,
same arm's-length pattern already used for kicad-cli and easyeda2kicad.

**Since build ticket 46 (2026-10-20) the DSN and the SES go through KiCad itself:**
`pcbnew.ExportSpecctraDSN` and `pcbnew.ImportSpecctraSES`, run in KiCad's bundled
Python (`routers/pcbnew_specctra.py`) on scratch copies, and the routed copy's new
copper reaches the board as a delta (`route_delta`) through the write layer's verified
save (`_apply_specs`). Our `kicad_tools` DSN export, its text patches, our SES parser,
the `board_origin` mapping and the y convention are gone. Bugs #1-#6 below describe
that removed path and stay as history: on rung 2 its DSN put 36 of 247 pin entries in the
wrong place (rotated and back-side footprints mirrored), KiCad's places all of them
(`tests/test_pcbnew_specctra.py`).

Two real bugs were found and worked around while building this against
a real Freerouting 2.3.0 run (verified end-to-end: DSN export -> real
Freerouting subprocess -> SES import -> add_trace/add_via -> real
kicad-cli DRC, going from 9 violations down to the 3 pre-existing,
unrelated ones on the simple_led fixture):

1. kicad_tools.export.ses.SESToKiCadImporter's wire/via parser
   (_parse_wire/_parse_via) requires each (wire ...)/(via ...) element
   to contain its own inline `(net NAME)` child, per its docstring
   example. Real Freerouting 2.3.0 output instead nests wires/vias
   *structurally* inside `(net NAME (wire ...) (wire ...))` blocks with
   no inline net tag at all -- so every wire/via was silently dropped
   (0 parsed despite real routed nets in the file). Worked around here
   with `_annotate_ses_wires_with_nets`, which rewrites the SES text to
   inject the inline `(net NAME)` tag the upstream parser expects,
   before handing it to `SESToKiCadImporter`. Candidate for an upstream
   PR per docs/ARCHITECTURE.md's kicad-tools contribution policy -- not
   filed yet.
2. `SESToKiCadImporter.merge_into`'s own coordinate/width conversion is
   off by exactly 10x (doesn't fully account for the SES
   `(resolution um 10)` sub-grid factor -- divides by 1000 where the
   correct factor is `resolution * 1000`). Rather than patch that
   S-expression-merge internals further, this module bypasses
   `merge_into` entirely: once wires/vias parse correctly (fix #1
   above), their points are converted to mm ourselves
   (`raw / (resolution * 1000)`) and applied via `kicad_tools.schema.
   PCB.add_trace`/`add_via` directly -- APIs already well-tested
   elsewhere in this codebase.
3. (Not a kicad_tools bug, a calling-convention gotcha worth recording
   loudly since it cost real debugging time): `PCB.add_trace`/`add_via`
   expect **board-local** coordinates -- the same frame as
   `Footprint.position` (which subtracts `board.board_origin` from the
   raw absolute page position stored in the file) -- not the absolute
   page coordinates that both the DSN export and the SES route data use.
   Every coordinate from the Freerouting pipeline must have
   `board.board_origin` subtracted before being passed to `add_trace`/
   `add_via`, or every track lands `board_origin` mm away from the pads
   it's meant to connect to (which KiCad's real DRC correctly flags as
   "track has unconnected end" -- this is exactly what happened before
   the fix was found, going from 9 violations down to 3 once corrected).

A fourth, minor discrepancy (not fixed, reported honestly in
tests/test_routing_toolset.py rather than papered over): after a
confirmed route where the real `kicad-cli` DRC reports 0 unconnected
items, `kicad_tools.schema.PCB.get_ratsnest()` (which `get_unrouted_nets`
wraps) sometimes still reports the same nets as unrouted -- an apparent
floating-point-tolerance mismatch internal to kicad_tools' own
connectivity computation, distinct from bugs #1/#2 above. Since this
project treats `kicad-cli`'s real DRC as the authoritative signal
(docs/ARCHITECTURE.md), not kicad_tools' internal ratsnest, `confirm_route`
already reports the real DRC result directly -- callers should trust
that over a subsequent `get_unrouted_nets` call on the same board. Not
investigated further or filed upstream -- P4-02 was already flagged in
archived backlog as the hardest part of this phase; two confirmed, worked-
around bugs was enough for one session's budget.

----------------------------------------------------------------------
P4-03/P4-04 session (2026-09-04): the real reason autorouting quality
was bad, found by inspecting the actual exported bytes
----------------------------------------------------------------------

The standing conclusion going into this ticket was "Freerouting's
routing QUALITY on the golden-path board is inherently poor" -- two
clean runs had produced real shorts, 35-36 DRC violations, and **zero
vias** in Freerouting's own SES output. Zero vias on a two-layer board
with crossing nets was the smell that turned out to be a real bug, not
an autorouting-quality limit:

5. **`kicad_tools.export.dsn.KiCadToDSNExporter` reads the reference
   designator only from `(fp_text reference ...)`**, the KiCad 7-era
   spelling. Every KiCad 8/9/10 board -- including every board this
   project generates via `PCB.from_schematic()` -- stores it as
   `(property "Reference" "U1" ...)` instead, so `_extract_footprints`
   falls through to its `ref = "?"` default for **every** footprint.
   That poisons the whole DSN: `_generate_placement` emits
   `(place ? ...)` for all of them, and `_generate_network` emits pin
   references as `f"{fp.reference}-{pad.number}"`, i.e. `?-1`, `?-2`,
   ... So on the golden-path board the exported netlist read:

       (net GND  (pins ?-2 ?-2 ?-2 ?-21 ?-3 ?-3))
       (net VCC  (pins ?-1 ?-1 ?-1 ?-4))
       (net "Net-(D1-K)" (pins ?-1 ?-2))

   -- the same token `?-1` belongs to VCC *and* `Net-(D1-K)` *and*
   `unconnected-(U1-PA2-Pad1)`, and GND lists `?-2` three times. Given a
   netlist where one pin is a member of three different nets,
   Freerouting produced exactly what you would expect: tracks crossing
   between nets, i.e. the "real shorts" the previous session correctly
   observed but attributed to autorouter quality.

   Why this was missed until now: `tests/fixtures/simple_led.kicad_pcb`
   is a hand-written KiCad-7-style fixture that *does* use
   `(fp_text reference ...)`, so it exported correctly and P4-02's
   end-to-end verification genuinely passed. The bug only bites real,
   modern boards -- which is to say every board a user would actually
   bring to this tool.

   Worked around by `_repair_dsn_footprint_references`, which fills in
   `FootprintInfo.reference` from `(property "Reference" ...)` -- keyed
   by footprint uuid, using kicad_tools' own already-parsed S-expression
   tree, not a text edit of the `.kicad_pcb` -- between `_load()` and
   `export()`. Upstream-PR candidate (the real fix is for
   `_extract_footprints` to read both spellings), not filed yet.

   Measured effect on the golden-path board, three real Freerouting
   2.3.0 runs, all else identical:
     - as-exported:            1 via,  33 unrouted / 12 violations (72s)
     - reference designators repaired: 5 vias, 3 unrouted / 12 violations (7s)
   and after applying, real `kicad-cli` DRC went from 35-36 violations
   **with real shorts** to **5 violations, 0 shorts, 3 unconnected**.

6. **Every layer is emitted with `(index 0)`** -- `_generate_structure`
   hardcodes the string rather than enumerating, so F.Cu and B.Cu both
   claim stack index 0. This *is* a real defect and is fixed here by
   `_fix_dsn_layer_indices`, but it must be reported honestly: it was
   the first hypothesis for the zero-vias symptom and it turned out
   **not to be load-bearing**. Isolated by running Freerouting against a
   DSN with the reference designators repaired but the indices left
   wrong: byte-identical outcome (3 unrouted / 12 violations, 5 vias) to
   the fully-fixed DSN. Freerouting derives layer order from the order
   of the `(layer ...)` declarations, not from the `index` property. The
   index is corrected anyway because an incorrect DSN is worth fixing on
   its own terms and other Specctra consumers may not be as forgiving --
   not because it changed this board's result.

Two further real findings from the same session, both load-bearing for
the P4-03 retry loop's design rather than bugs:

7. **Freerouting's own violation score anti-correlates with real KiCad
   DRC.** Its self-reported best configuration on this board
   (`width 0.20mm`: "1 unrouted and 12 violations") produced **27** real
   DRC violations including **2 real shorts**, while its self-reported
   worse configuration (`width 0.25mm`: "3 unrouted and 12 violations")
   produced 5 violations and no shorts. So the retry loop scores every
   attempt on a real `kicad-cli` DRC run against a throwaway copy of the
   board -- never on Freerouting's own log -- and keeps the best attempt
   by that measure, not the last one. Its log is still parsed, but only
   for signals it is authoritative about (its own plateau, layers it
   ignored).

8. **Freerouting 2.3.0 has no partial/selective routing mode.**
   Confirmed by running `-help` rather than assuming: the entire CLI is
   `-de -di -dr -do -mp -l -mt -us -hr -is -h`. There is no "route only
   these nets" option and no via-count limit option. So P4-03's retry
   step is necessarily a full re-route with adjusted global parameters,
   which is the fallback its acceptance criteria explicitly allow.

----------------------------------------------------------------------
P4-05 (2026-09-11): KiCadRoutingTools as a second backend
----------------------------------------------------------------------

`propose_route_with_retry(backend="kicad_routing_tools")` routes through
`drandyhaas/KiCadRoutingTools` (MIT, ~50x faster than Freerouting, reads
and writes `.kicad_pcb` directly -- no DSN/SES, so none of bugs #1-#6
above apply to it). The backend lives in
`kicad_mcp.routers.kicad_routing_tools`; this module owns what is shared
between the two backends and must stay uniform: the dry-run +
revision-token checkpoint, the real-`kicad-cli`-DRC scorer
(`_route_score`, never a router's own log), the diagnosis categories, the
trial-copy discipline, and `confirm_route`'s apply path through
`kicad_tools.PCB.add_trace`/`add_via` (KRT's written file is never copied
over the caller's board; its added copper is diffed out via
`route_delta` and re-applied as specs).

Findings while landing it, all measured with real `kicad-cli` DRC against
the board's ORIGINAL project rules (full numbers in the backend module's
docstring):

9. `--clearance` does NOT close the evaluation's clearance gap -- KRT's
   per-net rescue pass descends below it exactly as it descended below the
   netclass (8 clearance violations either way). A generated
   `--fab-overrides` file pinning the board's own effective rules as the
   fab floor does: 0 violations. So `escalation="board"` always pins that
   floor. The honest cost on the golden board is 3 open nets (2 with a
   finer grid) instead of rule-breaking copper -- the same wall
   Freerouting hits, see docs/ARCHITECTURE.md's meta-finding.
10. `--no-fix-drc-settings` does not stop KRT rewriting the output's
   sibling `.kicad_pro` (its "In-run DRC floors (#650)" pass lowers
   `rules.min_hole_clearance` regardless). Every attempt therefore copies
   the ORIGINAL `.kicad_pro` back beside the routed trial before anything
   reads it -- and the real DRC that scores the attempt runs on a
   separate trial copy that received the delta through `kicad_tools`, so
   what is graded is exactly what `confirm_route` will apply.
11. `--escalation off` leaves a dangling via from an abandoned rescue
   (`via_dangling`, 1 violation on the golden board). No special
   handling: real DRC reports it and the scorer counts it.
12. **`kicad-cli pcb drc`'s `clearance` count is not deterministic on an
   identical file** (KiCad 10.0 on this machine): three consecutive runs
   on the same confirmed board gave 61 / 59 / 64 violations, every other
   category byte-identical (annular_width 5, drill_out_of_range 5,
   hole_clearance 3, track_width 24, via_diameter 5) and only `clearance`
   moving (19 / 17 / 22). That is what the evaluation's "65" vs this
   session's "55"/"59" on the same KRT output are, and why a trial-copy
   DRC and the post-confirm DRC of the same specs can differ by a few
   clearance items. A board with zero clearance violations (the `board`
   policy's result here) is stable. Consequences: the scorer still ranks
   shorts and unconnected first, where the signal is stable; no test pins
   an exact non-zero clearance count; and P4-03's historical "5
   violations" for Freerouting should be read as "about 5".

----------------------------------------------------------------------
P4-06 (2026-09-12): pre-existing filled copper zones are invisible to
both backends -- confirmed the hard way, on a real board
----------------------------------------------------------------------

Routing openMPPT v1.3 (7 real filled zones from P3-07's pour work,
already on the board) through `kicad_routing_tools` produced a proposal
with 12 real shorts, including one power rail shorted directly to
another -- correctly declined at the checkpoint, never applied. Root-
caused by reading both backends' real obstacle/export code, then
reproducing it directly (not assumed):

13. **Neither backend's router ever sees filled zone copper as terrain
   to avoid for a foreign net.** KiCadRoutingTools'
   `obstacle_map.build_base_obstacle_map` -- the function that decides
   which grid cells its A* router may not enter -- stamps BGA exclusion
   boxes, other nets' existing segments/vias/pads, board-edge clearance,
   KiCad native `(zone (keepout ...))` rule areas, and drill-hole
   clearance; a copper-POUR `Zone`'s fill polygon is never one of them.
   KRT's own `docs/routing-architecture.md` independently confirms this
   -- it lists the identical 7 obstacle types the code builds and
   likewise omits copper pours. Every other place KRT touches
   `pcb_data.zones` is connectivity bookkeeping for the zone's OWN net
   (`routing_common.filter_already_routed`, `check_connected`) or a soft
   steering cost near a zone's edge (`plane_fragility`), never a hard
   block for a different net. `kicad_tools.export.dsn.
   KiCadToDSNExporter` (the Freerouting path) is, if anything, worse: it
   never mentions zones/pours/keepouts anywhere in its source at all
   (`_generate_structure`/`_generate_placement`/`_generate_wiring`,
   verified by reading all three) -- a filled zone doesn't even exist as
   far as the DSN Freerouting receives is concerned, so it has the same
   exposure as KRT, not a smaller one.
14. **Reproduced directly** (not just read): a 3-pad fixture
   (`tests/fixtures/zone_obstacle/`, built from `simple_led.kicad_pcb`
   plus a real `kicad_tools.zones.ZoneGenerator` GND pour straddling the
   direct path between two LED_ANODE pads, filled for real via
   `kicad-cli pcb drc --refill-zones --save-board` -- never a hand-
   edited file) routed through KRT (`--escalation board`) produced a
   LED_ANODE track AND via landing with **0.0mm actual clearance**
   against the GND zone, confirmed by real `kicad-cli pcb drc` against
   the board's own 0.3mm zone-clearance rule. Same failure shape as
   openMPPT's 12 shorts, on a fixture two orders of magnitude smaller.
15. **The guard**: `propose_route_with_retry` now refuses up front
   (`status: incomplete`, naming every filled zone) whenever the board
   has any pre-existing filled copper-pour zone, for EITHER backend --
   see `_pre_existing_filled_zone_report`. `acknowledge_unmodeled_zone_
   risk=True` is the explicit, loud opt-in (never silent -- the returned
   result always carries `zone_obstacle_warning` once acknowledged).
   This is an upstream-PR candidate against KiCadRoutingTools (its
   signal obstacle map should treat other nets' zone fill the way it
   already treats keepout rule areas) and, separately, against
   `kicad_tools.export.dsn` (the DSN exporter should at minimum emit
   zone outlines as a Specctra `(keepout ...)` so Freerouting can avoid
   them) -- neither filed yet, per this project's policy of reporting a
   finding before forking anything.

----------------------------------------------------------------------
P4-09 (2026-09-13): a bare `.kicad_pcb` trial copy silently drops
per-netclass clearance -- confirmed, and now fixed (the hang/thrashing
half of P4-09 stays open, see archived backlog)
----------------------------------------------------------------------

Chasing a real full-board routing hang on openMPPT v1.3, `_trial_copy`
(used for both the KRT input and every post-route DRC-scoring copy) was
found to copy only `.kicad_pcb`/`.kicad_pro`/`.kicad_prl`, not
`.kicad_dru`/`fp-lib-table`/`sym-lib-table`. KRT's own CLI prints a real
warning for a project file it can't fully resolve ("NO .kicad_pro NEXT
TO ... resolves clearances from the STOCK netclass") that had been read
as advisory noise, not acted on.

16. **Confirmed as a real correctness gap, not cosmetic.** Routing the
   same real board twice from identical input: once against the old
   partial trial copy (KRT finished fast, "clean-looking" DRC), once
   with the board's real `.kicad_pro`/`.kicad_dru` genuinely alongside
   it. Real `kicad-cli pcb drc` -- which always grades against the true
   project rules regardless of what KRT itself saw while routing --
   found **18 real `clearance` violations** in the first case, all on
   the board's wider-clearance `Power_20A` netclass (0.5mm declared,
   as little as 0.2mm delivered), invisible to the "clean-looking"
   partial-sibling run.
17. **The fix**: `_trial_copy` now copies `.kicad_dru` alongside the
   existing `.kicad_pro`/`.kicad_prl` (all four share the board's own
   basename, so a `with_suffix` loop covers them), plus `fp-lib-table`/
   `sym-lib-table` (fixed filenames in the project directory, not
   suffix-derived -- copied separately). One shared helper, so the fix
   applies uniformly to every KRT invocation and every DRC-scoring trial
   copy this module makes.
18. **What this fix does NOT claim to solve**: a *separate*, still-open
   half of P4-09 -- a full-board routing attempt against a board WITH
   its real `.kicad_pro`/`.kicad_dru` present hung/thrashed for 70+
   minutes without converging (a `--nets`-scoped repair pass on the
   same real rules genuinely thrashed: nets dropping out of the
   violation list then reappearing in later rounds, net count worsening
   before being stopped). Whether more-complete rule visibility is the
   router working *harder* to satisfy a real, wider constraint
   (plausible) or a distinct KRT behavior was never root-caused. This
   fix removes the "silently wrong" failure mode; it does not by itself
   claim to remove the "doesn't converge" one -- that is exactly what a
   real re-attempt with this fix in place needs to establish.
"""

from __future__ import annotations

import glob
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections import Counter
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from kicad_mcp import write_layer
from kicad_mcp.routers import kicad_routing_tools as krt_backend

ROUTING_BACKENDS = ("freerouting", "kicad_routing_tools")

# ---------------------------------------------------------------------------
# Freerouting subprocess resolution (mirrors kicad_cli.py's house pattern:
# env var override, then PATH, then common per-platform install globs).
# ---------------------------------------------------------------------------


class FreeroutingError(RuntimeError):
    """Raised when Freerouting or a compatible JVM can't be found, or the
    autorouting subprocess fails/times out."""


class FreeroutingTimeout(FreeroutingError):
    """The Freerouting subprocess exceeded its timeout. Raised only for a real timeout;
    carries the configured `timeout` and the measured `elapsed_s`."""

    timeout: float | None = None
    elapsed_s: float | None = None


_JAVA_CANDIDATE_GLOBS: dict[str, list[str]] = {
    "win32": [
        "C:/Program Files/Eclipse Adoptium/jre-25*/bin/java.exe",
        "C:/Program Files/Eclipse Adoptium/jdk-25*/bin/java.exe",
        "C:/Program Files/Eclipse Adoptium/jre-*/bin/java.exe",
        "C:/Program Files/Eclipse Adoptium/jdk-*/bin/java.exe",
        "C:/Program Files/Java/jdk-*/bin/java.exe",
    ],
    "darwin": ["/Library/Java/JavaVirtualMachines/*/Contents/Home/bin/java"],
}


def find_java() -> Path:
    """Resolve a `java` executable. Freerouting 2.3.0 requires a JVM built
    with a sufficiently recent class-file version (confirmed: JRE 21 is NOT
    enough -- UnsupportedClassVersionError, class file version 69 needed;
    JRE 25 works) -- an older JVM being present on PATH doesn't mean it's
    usable for this specific Freerouting version, so a caller that hits
    FreeroutingError should check the JVM version, not just its presence.
    """
    env_override = os.environ.get("JAVA_PATH")
    if env_override:
        path = Path(env_override)
        if path.is_file():
            return path
        raise FreeroutingError(f"JAVA_PATH={env_override!r} does not point to a file")

    which = shutil.which("java")
    if which:
        return Path(which)

    for pattern in _JAVA_CANDIDATE_GLOBS.get(sys.platform, []):
        matches = sorted(glob.glob(pattern), reverse=True)
        if matches:
            return Path(matches[0])

    raise FreeroutingError(
        "java not found. Set JAVA_PATH, add java to PATH, or install a JRE "
        "(e.g. `winget install EclipseAdoptium.Temurin.25.JRE`). Freerouting "
        "2.3.0 needs a fairly recent JVM (JRE 21 is confirmed NOT sufficient)."
    )


def find_freerouting_jar() -> Path:
    """Resolve the Freerouting jar. No auto-download -- set FREEROUTING_JAR_PATH
    to an existing download (https://github.com/freerouting/freerouting/releases).
    """
    env_override = os.environ.get("FREEROUTING_JAR_PATH")
    if env_override:
        path = Path(env_override)
        if path.is_file():
            return path
        raise FreeroutingError(f"FREEROUTING_JAR_PATH={env_override!r} does not point to a file")

    raise FreeroutingError(
        "FREEROUTING_JAR_PATH is not set. Download a Freerouting release jar "
        "(https://github.com/freerouting/freerouting/releases) and set "
        "FREEROUTING_JAR_PATH to it. Freerouting is GPLv3 -- deliberately not "
        "bundled with or auto-downloaded by this project, see docs/ARCHITECTURE.md."
    )


# ---------------------------------------------------------------------------
# KiCad's own Specctra exporter and importer, run in KiCad's bundled Python
# (build ticket 46; docs/ARCHITECTURE.md decision 3). `pcbnew` only ever touches a
# scratch copy; the routed copper reaches the real board as a delta through the
# write layer (`_apply_specs`).
# ---------------------------------------------------------------------------

KICAD_PYTHON_ENV = "KICAD_PYTHON_PATH"
KICAD_SPECCTRA_TIMEOUT_S = 180
_SPECCTRA_RUNNER = Path(__file__).resolve().parents[1] / "routers" / "pcbnew_specctra.py"
_SPECCTRA_RESULT_MARKER = "KICAD_MCP_RESULT "


class KicadSpecctraError(RuntimeError):
    """KiCad's Specctra export or SES import did not deliver a usable result."""


class KicadPythonUnavailable(KicadSpecctraError):
    """No KiCad bundled Python that can import ``pcbnew``: the Freerouting path is
    unavailable (``incomplete``), never replaced by another DSN writer."""


def _kicad_python_beside(cli: Path) -> list[Path]:
    """Where KiCad's own interpreter sits relative to ``kicad-cli`` on each platform."""
    if sys.platform == "win32":
        return [cli.parent / "python.exe"]
    if sys.platform == "darwin":  # KiCad.app/Contents/MacOS/kicad-cli
        return [cli.parents[1] / "Frameworks" / "Python.framework" / "Versions" / "Current" / "bin" / "python3"]
    return [cli.parent / "python3"]  # Linux packages put pcbnew into the system Python


def find_kicad_python() -> Path:
    """KiCad's bundled Python: ``KICAD_PYTHON_PATH``, else the interpreter next to the
    ``kicad-cli`` that ``kicad_cli.find_kicad_cli`` resolves (env, PATH, install glob), so
    the exporter is the same KiCad that grades the board. Whether it can import ``pcbnew``
    is checked by ``kicad_specctra_availability``."""
    from kicad_mcp import kicad_cli

    override = os.environ.get(KICAD_PYTHON_ENV)
    if override:
        path = Path(override)
        if path.is_file():
            return path
        raise KicadPythonUnavailable(f"{KICAD_PYTHON_ENV}={override!r} does not point to a file")
    try:
        cli = kicad_cli.find_kicad_cli()
    except kicad_cli.KicadCliError as exc:
        raise KicadPythonUnavailable(f"KiCad's bundled Python is looked up next to kicad-cli: {exc}") from exc
    for candidate in _kicad_python_beside(cli):
        if candidate.is_file():
            return candidate
    raise KicadPythonUnavailable(
        f"KiCad's bundled Python was not found next to {cli}; set {KICAD_PYTHON_ENV} to the "
        "interpreter that can `import pcbnew` (Windows: <KiCad>/bin/python.exe)."
    )


def run_kicad_specctra(command: str, *args: str | Path, timeout: float = KICAD_SPECCTRA_TIMEOUT_S) -> dict:
    """Run one command of ``routers/pcbnew_specctra.py`` in KiCad's bundled Python and
    return its result dict (``ok`` may be False: the caller decides). Isolated (``-I``),
    UTF-8, in a throwaway working directory and ``KICAD_CONFIG_HOME`` (the script sets its
    own as well, and routes wx logging to stderr, so no KiCad dialog can open)."""
    python = find_kicad_python()
    # The runner's cwd is its throwaway directory: every path argument goes in absolute.
    argv = [str(python), "-I", "-X", "utf8", str(_SPECCTRA_RUNNER), command, *(str(Path(a).resolve()) for a in args)]
    with tempfile.TemporaryDirectory(prefix="kicad_mcp_pcbnew_", ignore_cleanup_errors=True) as tmp:
        env = dict(os.environ, KICAD_CONFIG_HOME=tmp)
        try:
            proc = subprocess.run(argv, capture_output=True, timeout=timeout, cwd=tmp, env=env, check=False)
        except subprocess.TimeoutExpired as exc:
            raise KicadSpecctraError(f"KiCad's {command} step timed out after {timeout}s") from exc
        except OSError as exc:
            raise KicadPythonUnavailable(f"could not start {python}: {exc}") from exc
    stdout = proc.stdout.decode("utf-8", errors="replace")
    stderr = proc.stderr.decode("utf-8", errors="replace")
    for line in reversed(stdout.splitlines()):
        if line.startswith(_SPECCTRA_RESULT_MARKER):
            import json

            result = json.loads(line[len(_SPECCTRA_RESULT_MARKER):])
            result["exit_code"] = proc.returncode
            return result
    tail = " ".join(f"{stdout}\n{stderr}".split())[-600:]
    if command == "probe":
        raise KicadPythonUnavailable(f"{python} cannot run KiCad's pcbnew module (exit {proc.returncode}): {tail}")
    raise KicadSpecctraError(f"KiCad's {command} step gave no result (exit {proc.returncode}): {tail}")


def kicad_specctra_availability() -> dict:
    """``{"available", "reason", "python", "pcbnew_version"}``: can KiCad's bundled Python
    import ``pcbnew`` here. Never raises."""
    try:
        python = find_kicad_python()
        probe = run_kicad_specctra("probe", timeout=60)
    except KicadSpecctraError as exc:
        return {"available": False, "reason": str(exc), "python": None, "pcbnew_version": None}
    if not probe.get("ok"):
        return {"available": False, "reason": f"pcbnew probe failed: {probe.get('error')}",
                "python": str(python), "pcbnew_version": None}
    return {"available": True, "reason": None, "python": str(python), "pcbnew_version": probe.get("pcbnew_version")}


_COPPER_KINDS = ("tracks", "arcs", "vias")


def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_copper_count(value: Any) -> bool:
    return isinstance(value, dict) and all(_is_count(value.get(k)) for k in _COPPER_KINDS)


def _import_report_problems(report: dict) -> list[str]:
    """Every count the runner's import result must carry and does not (or not as a count):
    a missing count is never read as zero (review round 1, finding 9)."""
    problems = []
    if report.get("returned") is not True:
        problems.append("returned")
    session = report.get("session")
    if not isinstance(session, dict) or not _is_copper_count(session.get("expected")):
        problems.append("session.expected")
    if not isinstance(session, dict) or not _is_count(session.get("ignored_wire_shapes")):
        problems.append("session.ignored_wire_shapes")
    problems += [key for key in ("imported", "missing", "unexpected") if not _is_copper_count(report.get(key))]
    if not _is_count(report.get("new_items_on_no_net")):
        problems.append("new_items_on_no_net")
    return problems


BOARD_CHANGED_DURING_ROUTING = "board_changed_during_routing"


class BoardChangedDuringRouting(KicadSpecctraError):
    """The board's files changed between the export snapshot and the import: the routed
    copper was made for a board that no longer exists. Refused by name; never re-based."""

    code = BOARD_CHANGED_DURING_ROUTING


def _board_digest(pcb_path: Path) -> str:
    """sha256 over the board, its ``.kicad_pro`` and ``.kicad_dru`` and its library tables
    (name and bytes), so a change to the board, its project rules or its library tables
    changes it. ``.kicad_prl`` is left out: it holds local view state KiCad writes when it
    closes, not design content, and must not refuse a run."""
    digest = hashlib.sha256()
    files = [pcb_path, *(pcb_path.with_suffix(s) for s in (".kicad_pro", ".kicad_dru")),
             *(pcb_path.parent / name for name in ("fp-lib-table", "sym-lib-table"))]
    for path in files:
        if path.is_file():
            digest.update(path.name.encode("utf-8") + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def snapshot_board(pcb_path: str | Path, work_dir: str | Path) -> dict:
    """Copy the board and its project files into ``work_dir`` once, at the start of a
    routing run: ``{"source_pcb", "snapshot_pcb", "source_sha256"}``. The DSN is exported
    from it and the session imported into a copy of it, so both sides of the diff are the
    same board; ``source_sha256`` (of the copies) is what ``import_ses_with_kicad`` checks
    the live board against."""
    pcb_path = Path(pcb_path).resolve()
    snapshot = _trial_copy(pcb_path, Path(work_dir))
    return {"source_pcb": str(pcb_path), "snapshot_pcb": str(snapshot), "source_sha256": _board_digest(snapshot)}


def export_dsn_with_kicad(pcb_path: str | Path, work_dir: str | Path) -> dict:
    """Specctra DSN of ``pcb_path`` by KiCad's own exporter: the board is snapshot into
    ``work_dir/snapshot`` (``snapshot_board``; the result carries its keys for
    ``import_ses_with_kicad``) and exported from a copy of that in ``work_dir/export`` (the
    board itself is only read). Copper already on the board goes out as ``fix`` wiring.
    Raises ``KicadSpecctraError`` (``KicadPythonUnavailable`` without a usable KiCad
    Python); there is no other DSN path."""
    work_dir = Path(work_dir)
    snapshot = snapshot_board(pcb_path, work_dir / "snapshot")
    scratch = _trial_copy(Path(snapshot["snapshot_pcb"]), work_dir / "export")
    dsn = work_dir / "board.dsn"
    report = run_kicad_specctra("export", scratch, dsn)
    if not report.get("ok") or not dsn.is_file():
        raise KicadSpecctraError(f"KiCad's Specctra export failed: {report.get('error') or report}")
    return {
        **snapshot,
        "dsn_path": str(dsn),
        "exporter": f"pcbnew {report.get('pcbnew_version')} ExportSpecctraDSN",
        "existing_copper_kept_fixed": report.get("existing_copper"),
    }


def import_ses_with_kicad(snapshot: dict, ses_path: str | Path, work_dir: str | Path) -> dict:
    """Import Freerouting's SES with KiCad's own importer into a copy of the export's
    snapshot (``snapshot_board`` or ``export_dsn_with_kicad``'s result) in ``work_dir`` and
    diff it against the snapshot: the router's new copper as specs (KRT's ``route_delta``,
    with via size and drill) plus ``expected_copper``, the same copper from the raw
    S-expressions, which ``_apply_specs`` checks the written board against.
    ``ImportSpecctraSES``' return value is recorded, never trusted: ``import`` holds what
    the session asked for, what KiCad added, what it skipped (``skipped_items``) and new
    copper on no net. Raises ``KicadSpecctraError`` when KiCad could not import or save, or
    when the two diffs disagree, and ``BoardChangedDuringRouting`` when the live board's
    files are no longer the snapshot's."""
    source = Path(snapshot["source_pcb"])
    base = Path(snapshot["snapshot_pcb"])
    routed = _trial_copy(base, Path(work_dir))
    report = run_kicad_specctra("import", routed, ses_path)
    summary = {
        "importer_returned": report.get("returned"),
        "session_expected": (report.get("session") or {}).get("expected"),
        "imported": report.get("imported"),
        "missing": report.get("missing"),
        "unexpected": report.get("unexpected"),
        "new_items_on_no_net": report.get("new_items_on_no_net"),
        "ignored_wire_shapes": (report.get("session") or {}).get("ignored_wire_shapes"),
        "placement_entries_ignored": (report.get("session") or {}).get("placement_entries"),
        "importer_warnings": report.get("importer_warnings", []),
    }
    if not report.get("ok"):
        raise KicadSpecctraError(f"KiCad's SES import failed: {report.get('error')}; {summary}")
    summary["report_problems"] = _import_report_problems(report)
    missing = report.get("missing")
    summary["skipped_items"] = (sum(missing[k] for k in _COPPER_KINDS)
                                if _is_copper_count(missing) else None)
    live = _board_digest(source)
    if live != snapshot["source_sha256"]:
        raise BoardChangedDuringRouting(
            f"{source} changed while it was being routed (sha256 {snapshot['source_sha256'][:12]} at the "
            f"export, {live[:12]} now): the routed copper was made for the board as it was; route it again"
        )

    delta = krt_backend.route_delta(base, routed)
    raw = raw_copper_delta(write_layer.load(base), write_layer.load(routed))
    added_kinds = Counter(key[0] for key in raw["added"].elements())
    if (added_kinds["segment"], added_kinds["via"]) != (len(delta["trace_specs"]), len(delta["via_specs"])):
        raise KicadSpecctraError(
            f"the routed copy's new copper does not diff the same two ways: raw S-expressions "
            f"{dict(added_kinds)}, route_delta {len(delta['trace_specs'])} segments and "
            f"{len(delta['via_specs'])} vias"
        )
    # The apply writes segments and vias only: the routed copy's arcs are not applied, so they
    # are left out of the expected copper and flagged (``arcs_not_applied``); those
    # connections stay unrouted on the board (review round 1, finding 6).
    summary["arcs_not_applied"] = added_kinds["arc"]
    return {
        "trace_specs": delta["trace_specs"],
        "via_specs": delta["via_specs"],
        "expected_copper": sorted(key for key in raw["added"].elements() if key[0] != "arc"),
        "removed_copper": sum(raw["removed"].values()),
        "routed_pcb": str(routed),
        "source_sha256": snapshot["source_sha256"],
        "import": summary,
    }


# --- copper from the raw S-expressions (CLAUDE.md hard rule 2) -------------------------------

_COPPER_TAGS = ("segment", "arc", "via")


def _number(node: Any, tag: str, index: int = 0) -> float | None:
    child = node.find(tag)
    if child is None or len(child.values) <= index:
        return None
    return round(float(str(child.values[index])), 6)  # 1 nm: KiCad's internal unit


def _net_names(root: Any) -> dict[str, str]:
    table = {}
    for child in root.children:
        if not child.is_atom and child.name == "net" and len(child.values) >= 2:
            table[str(child.values[0])] = str(child.values[1])
    return table


def _copper_key(node: Any, nets: dict[str, str]) -> tuple:
    net = node.find("net")
    values = [str(v) for v in net.values] if net is not None else []
    if len(values) >= 2:
        name = values[1]
    elif values:
        name = nets.get(values[0], values[0])
    else:
        name = ""
    if node.name == "via":
        layers = node.find("layers")
        kind = node.find("type")
        return ("via", _number(node, "at"), _number(node, "at", 1), _number(node, "size"), _number(node, "drill"),
                tuple(str(v) for v in layers.values) if layers is not None else (),
                str(kind.values[0]) if kind is not None and kind.values else "", name)
    layer = node.find("layer")
    points = tuple(_number(node, tag, i) for tag in ("start", "mid", "end") for i in (0, 1) if node.find(tag) is not None)
    return (str(node.name), points, _number(node, "width"),
            str(layer.values[0]) if layer is not None and layer.values else "", name)


def raw_copper(root: Any) -> Counter:
    """Every top-level segment, arc and via of a board root as a key of its raw tokens
    (positions, width, size, drill, layers, net name), uuid and lock state left out."""
    nets = _net_names(root)
    return Counter(_copper_key(c, nets) for c in root.children if not c.is_atom and c.name in _COPPER_TAGS)


def raw_copper_delta(original: Any, routed: Any) -> dict:
    """``{"added", "removed"}``: copper in ``routed`` that ``original`` lacks, and back."""
    before, after = raw_copper(original), raw_copper(routed)
    return {"added": after - before, "removed": before - after}


_FREEROUTING_SCORE_RE = re.compile(r"score(?:\s+of|:)\s+([\d.]+)\s*\((\d+)\s+unrouted and (\d+)\s+violations?\)")
_FREEROUTING_MISSING_LAYER_RE = re.compile(r"layer with name '([^']+)' not found in layer structure")


def _parse_freerouting_log(log_text: str) -> dict:
    """Extract the signals Freerouting's own log is actually authoritative
    about. Deliberately narrow -- see module docstring finding #7: its
    self-reported violation count anti-correlates with real KiCad DRC, so it
    is recorded for the audit trail but never used to pick a winner.

    `optimizer_plateaued` is the genuinely useful one: when Freerouting says
    it stopped because the improvement fell below its threshold, spending a
    retry attempt on "more passes" is provably wasted.
    """
    scores = _FREEROUTING_SCORE_RE.findall(log_text or "")
    final = scores[-1] if scores else None
    return {
        "self_reported_score": float(final[0]) if final else None,
        "self_reported_unrouted": int(final[1]) if final else None,
        "self_reported_violations": int(final[2]) if final else None,
        "optimizer_plateaued": "improvement in this pass" in (log_text or ""),
        "ignored_layers": sorted(set(_FREEROUTING_MISSING_LAYER_RE.findall(log_text or ""))),
    }


@contextmanager
def freerouting_sandbox():
    """A throwaway directory for one Freerouting process: its working directory and its
    ``--user_data_path``, removed afterwards.

    Freerouting 2.5.0 reads ``-l <x>`` as a log location too: run from the caller's
    directory, it created ``<cwd>/en/freerouting.log`` and saved that path into
    ``%APPDATA%/freerouting/freerouting.json``, together with the last ``-mp`` value and
    usage counters (paradigm-review-2 ticket 01). With this directory as the working
    directory and the user-data path, ``freerouting.json`` and every log stay in here, and
    each run starts from Freerouting's defaults instead of the user's saved settings
    (build ticket 33).
    """
    with tempfile.TemporaryDirectory(prefix="kicad_mcp_freerouting_", ignore_cleanup_errors=True) as tmp:
        yield Path(tmp)


def freerouting_command(java: Path, jar: Path, sandbox: Path, *args: str) -> list[str]:
    """The ``java`` command line for one Freerouting process confined to ``sandbox``.

    The JVM writes its console in the OS code page (Cp1252 on a German Windows), which a
    Python in UTF-8 mode cannot decode; ``stdout.encoding``/``stderr.encoding`` (JDK 19+)
    make it UTF-8, and ``decode_jvm_output`` replaces whatever still is not.
    ``-XX:-UsePerfData`` stops HotSpot creating ``hsperfdata_<user>`` in the OS temp
    directory, and ``java.io.tmpdir`` keeps the JVM's and Freerouting's own temp files
    (including its legacy-settings migration source) inside the sandbox.

    ``--gui.enabled=false`` (build ticket 117) is part of every command: Freerouting 2.5.0's
    configuration has ``gui.enabled: true``, so without it every run opened the main window
    and an "Autorouter" confirmation dialog (measured per process id with the window title);
    with it the process has no window and writes the same session file and score. It is fixed
    here, not a lever of ``freerouting_router_options``, and it sets a command-line value
    only: ``freerouting.json`` is never written."""
    return [
        str(java), "-XX:-UsePerfData", f"-Djava.io.tmpdir={sandbox}",
        "-Dstdout.encoding=UTF-8", "-Dstderr.encoding=UTF-8", "-jar", str(jar),
        f"--user_data_path={sandbox}", "--gui.enabled=false", *args,
    ]


def decode_jvm_output(data: bytes | None) -> str:
    """JVM console bytes as text, never raising: undecodable bytes become U+FFFD."""
    return (data or b"").decode("utf-8", errors="replace")


# Build ticket 109 (ticket 107's measurements, Freerouting 2.5.0): the only `--router.*` options the
# wrapper will ever pass. The fanout stage necks tracks to 3/4 of the class width at pad entries
# (`insertFanoutMicroNeckdown`), where it is mostly not needed; `neck_width_um` is the legal width the
# router may use where the class width physically cannot enter a pad.
_NECK_WIDTH_UM_MAX = 1000


def freerouting_router_options(*, fanout_enabled: Any = None, neck_width_um: Any = None) -> list[str]:
    """The `--router.*` command-line options for the two protocol-6 levers, from a fixed allow-list.
    Anything else raises `FreeroutingError` before a process starts."""
    options: list[str] = []
    if fanout_enabled is not None:
        if fanout_enabled is not False:
            raise FreeroutingError(f"fanout_enabled must be None or False (the router's default is on), "
                                   f"not {fanout_enabled!r}")
        options.append("--router.fanout.enabled=false")
    if neck_width_um is not None:
        if (not isinstance(neck_width_um, int) or isinstance(neck_width_um, bool)
                or not 0 < neck_width_um <= _NECK_WIDTH_UM_MAX):
            raise FreeroutingError(f"neck_width_um must be a whole number of um in 1..{_NECK_WIDTH_UM_MAX}, "
                                   f"not {neck_width_um!r}")
        options.append(f"--router.neck_width_um={neck_width_um}")
    return options


def run_freerouting(
    dsn_path: str | Path,
    ses_output_path: str | Path,
    *,
    max_passes: int = 10,
    timeout: int = 300,
    optimization_strategy: str | None = None,
    selection_strategy: str | None = None,
    fanout_enabled: bool | None = None,
    neck_width_um: int | None = None,
) -> dict:
    """Run Freerouting without a window (``--gui.enabled=false``, see ``freerouting_command``)
    as a loopback-only local subprocess.

    `-mt 1` is deliberate, not a default: Freerouting 2.3.0 itself warns
    "Multi-threaded route optimization is broken and it is known to
    generate clearance violations" and recommends single-threaded
    optimization -- confirmed by running it, not assumed from docs.

    `optimization_strategy` (`-us`: greedy/global/hybrid) and
    `selection_strategy` (`-is`: sequential/random/prioritized) are the only
    other routing-behaviour knobs the 2.3.0 CLI has -- verified by running
    `-help`, see module docstring finding #8. Returns the parsed log summary.

    The process runs inside `freerouting_sandbox()` (its own working directory and
    user-data path), so it leaves no `en/` folder in the caller's directory and never
    rewrites the user's `freerouting.json`. Relative paths are resolved against the
    caller's directory first.

    `fanout_enabled` (only `False` is a value: it adds `--router.fanout.enabled=false`) and
    `neck_width_um` (a positive whole number of micrometres: `--router.neck_width_um=<n>`) are
    the protocol-6 levers of build ticket 109. They are built from `freerouting_router_options`,
    a fixed allow-list, into the command line; nothing writes `freerouting.json`.
    """
    router_options = freerouting_router_options(fanout_enabled=fanout_enabled, neck_width_um=neck_width_um)
    java = find_java()
    jar = find_freerouting_jar()
    dsn_path = Path(dsn_path).resolve()
    ses_output_path = Path(ses_output_path).resolve()
    ses_output_path.parent.mkdir(parents=True, exist_ok=True)

    options = [
        "-de", str(dsn_path),
        "-do", str(ses_output_path),
        "-mp", str(max_passes),
        "-mt", "1",  # see docstring -- multi-threaded optimization is known-broken
        # No "-l": Freerouting 2.5.0 reads it as a log location too and created `<cwd>/en/`
        # (build tickets 33 and 46); its log language is not needed, the score regex is English.
    ]
    if optimization_strategy:
        options += ["-us", optimization_strategy]
    if selection_strategy:
        options += ["-is", selection_strategy]
    options += router_options
    started = time.monotonic()
    with freerouting_sandbox() as sandbox:
        args = freerouting_command(java, jar, sandbox, *options)
        try:
            proc = subprocess.run(args, capture_output=True, check=False, timeout=timeout, cwd=sandbox)
        except subprocess.TimeoutExpired as exc:
            timeout_error = FreeroutingTimeout(f"Freerouting timed out after {timeout}s: {exc}")
            timeout_error.timeout = timeout
            timeout_error.elapsed_s = time.monotonic() - started
            raise timeout_error from exc
    stdout, stderr = decode_jvm_output(proc.stdout), decode_jvm_output(proc.stderr)
    if not ses_output_path.exists():
        raise FreeroutingError(
            f"Freerouting did not produce {ses_output_path} (exit {proc.returncode}):\n"
            f"stdout: {stdout}\nstderr: {stderr}"
        )
    return _parse_freerouting_log(f"{stdout}\n{stderr}")


# ---------------------------------------------------------------------------
# P4-03 diagnosis: categorize *why* a route is bad, from signal the tools
# actually emit.
#
# Every category below is backed by a real `kicad-cli` DRC `type` string or a
# real `kicad_tools` routing_status field observed on the golden-path board
# during this ticket -- deliberately NOT the richer taxonomy P4-03's
# acceptance criteria speculated about. Categories that were hoped for but
# have no real diagnostic signal behind them, and are therefore absent:
#
#   * "a keepout/zone blocks the only viable path" -- neither `kicad-cli`'s
#     DRC JSON nor Freerouting's log names an obstructing object; a net
#     blocked by a zone is indistinguishable from a net that simply failed
#     to route.
#   * "this net needs more layers than were made available" -- Freerouting
#     reports an unroutable net identically whether or not more layers would
#     have helped. There is no signal to key on, and inventing one would mean
#     guessing.
#   * "via limit exceeded" -- Freerouting 2.3.0's CLI has no via-budget
#     option at all (`-help`, finding #8), so nothing can produce this.
#
# Reporting those three as "couldn't map to real signal" is the honest
# outcome, per P4-03's own instruction not to invent categories.
# ---------------------------------------------------------------------------

_SHORT_VIOLATION_TYPES = {"shorting_items", "shorting_zones"}

# type -> (category, is_addressable_by_routing_parameters, adjustment)
_VIOLATION_CATEGORIES: dict[str, tuple[str, bool, str]] = {
    "shorting_items": (
        "short_circuit",
        True,
        "Routed copper of two different nets touches. Back off any clearance/width "
        "relaxation -- on fine-pitch boards relaxing constraints reliably produces these.",
    ),
    "shorting_zones": ("short_circuit", True, "Same as short_circuit, between filled zones."),
    "clearance": (
        "clearance_violation",
        True,
        "Retry with a narrower trace width, which buys clearance room. Note: RAISING the "
        "DSN clearance is usually counterproductive on a fine-pitch board -- measured on "
        "the golden-path board, 0.25mm/0.30mm clearance went from 12 to 44/60 violations.",
    ),
    "hole_clearance": (
        "hole_clearance_violation",
        True,
        "A via hole sits too close to other copper. Retry with a narrower trace width so "
        "vias are placed with more room.",
    ),
    "solder_mask_bridge": (
        "solder_mask_bridge",
        True,
        "Mask slivers between adjacent copper; co-occurs with over-tight routing. Same "
        "adjustment as clearance_violation.",
    ),
    "track_dangling": (
        "dangling_track",
        True,
        "The router left a stub with a free end. Retry with different pass/strategy settings.",
    ),
    "track_width": (
        "trace_narrower_than_board_minimum",
        False,
        "NOT fixable by re-routing: the autorouter must neck down to enter a pad that is "
        "itself narrower than the board's own min_track_width constraint, so no legal "
        "trace can reach that pad. This is a board design-rule/footprint conflict -- "
        "either lower min_track_width or use a footprint with larger pads. Escalated.",
    ),
    "lib_footprint_mismatch": (
        "library_footprint_mismatch",
        False,
        "Pre-existing and unrelated to routing: the board's embedded footprint differs "
        "from the library's. Escalated, never retried against.",
    ),
    # Observed for real on KRT's `fab` policy output (P4-05): the router
    # delivered vias/drills/annular rings below the board's own minimums.
    # Same shape as track_width above -- a rule-vs-delivered conflict, not
    # something a re-route at the same policy can fix.
    "via_diameter": (
        "via_smaller_than_board_minimum",
        False,
        "A via was placed smaller than the board's min_via_diameter. With KiCadRoutingTools this is "
        "escalation='fab' doing what it was asked (relaxing the board's floors); re-run with "
        "escalation='board' for rule-respecting copper, or lower the board's minimum deliberately.",
    ),
    "drill_out_of_range": (
        "via_smaller_than_board_minimum",
        False,
        "A via drill is below the board's min_through_hole_diameter -- same cause and remedy as via_diameter.",
    ),
    "annular_width": (
        "via_smaller_than_board_minimum",
        False,
        "A via's annular ring is below the board's min_via_annular_width -- same cause and remedy as via_diameter.",
    ),
}


def _read_board_constraints(pcb_path: str | Path) -> dict:
    """Read the design constraints DRC will actually enforce, from the
    sibling `.kicad_pro`. Read-only -- this never writes a KiCad file.
    """
    project = Path(pcb_path).with_suffix(".kicad_pro")
    if not project.is_file():
        return {}
    try:
        import json

        data = json.loads(project.read_text(encoding="utf-8"))
        return data.get("board", {}).get("design_settings", {}).get("rules", {}) or {}
    except Exception:  # noqa: BLE001 - a missing/odd project file must not break routing
        return {}


def _pre_existing_filled_zone_report(board: Any) -> dict | None:
    """archived backlog P4-06: neither routing backend treats a pre-existing
    FILLED COPPER ZONE (a pour/plane; a KiCad keepout rule area is a
    different `Zone.keepout is not None` case and both backends DO honor
    those) as a routing obstacle for a net other than the zone's own.

    Confirmed by reading both backends' real obstacle/export code, then
    reproducing it directly (never assumed):

    * KiCadRoutingTools' `obstacle_map.build_base_obstacle_map` -- the
      function that turns board state into the grid cells its A* router
      will not cross -- stamps BGA exclusion boxes, existing segments/
      vias/pads belonging to nets NOT currently being routed, board-edge
      clearance, KiCad native `(zone (keepout ...))` rule areas, and
      drill-hole clearance. A copper-pour `Zone`'s fill polygon never
      appears in that function, and KRT's own
      `docs/routing-architecture.md` independently confirms the omission
      -- it enumerates the identical 7 obstacle types the code builds and
      likewise never lists copper pours. Every other place KRT reads
      `pcb_data.zones` (`routing_common.filter_already_routed`,
      `check_connected`, `single_ended_routing`'s fill-model credit) is
      bookkeeping for "is THIS zone's OWN net already connected" or
      "steer softly near this zone's edge" (`plane_fragility`'s cost
      field) -- never a hard block fed to the grid for a DIFFERENT net.
    * `kicad_tools.export.dsn.KiCadToDSNExporter` (the Freerouting path)
      never mentions zones, pours, or keepouts anywhere in its source
      (`_generate_structure`/`_generate_placement`/`_generate_wiring` --
      verified by reading all three, zero matches) -- a filled zone is
      invisible to Freerouting too. It is not merely unprotected: it is
      never told the zone exists, so the DSN/SES path has the same
      exposure as KRT, not a smaller one.
    * Reproduced directly (2026-09-12): a 3-pad fixture with a real,
      `kicad-cli`-filled GND zone straddling the direct path between two
      LED_ANODE pads, routed through KRT with `--escalation board`,
      produced a track AND a via landing with **0.0mm actual clearance**
      against the GND zone -- confirmed by real `kicad-cli pcb drc`
      against the board's own 0.3mm zone-clearance rule. Same failure
      shape as openMPPT v1.3's 12 real shorts, including a power rail
      shorted directly to another.

    Returns None if the board has no filled copper-pour zone (a board
    with only keepout rule areas, or only unfilled zone definitions, is
    fine). Otherwise a dict naming every such zone, for an evidence-gated
    refusal -- never a silent skip.
    """
    try:
        zones = list(getattr(board, "zones", []) or [])
    except Exception:  # noqa: BLE001 - detection must never crash routing
        return None
    pours = [
        zone
        for zone in zones
        if getattr(zone, "keepout", None) is None
        and (getattr(zone, "is_filled", False) or getattr(zone, "filled_polygons", None))
    ]
    if not pours:
        return None
    return {
        "filled_zone_count": len(pours),
        "filled_zones": [
            {"net": zone.net_name, "layer": zone.layer, "priority": getattr(zone, "priority", 0)}
            for zone in pours
        ],
    }


def _unrouted_nets_from_status(status: dict) -> list[dict]:
    """Group `routing_status()['unrouted_pads']` into real per-net routing
    failures.

    Filters out single-pad nets: KiCad auto-names every unconnected IC pin
    into its own `unconnected-(U1-PA2-Pad1)` net, which has exactly one pad
    and by definition needs no routing. Counting those as routing failures
    is what made the raw ratsnest read as 18 "unrouted pads" on a board with
    exactly one genuinely unrouted connection.
    """
    by_net: dict[str, list[str]] = {}
    for entry in status.get("unrouted_pads", []) or []:
        if len(entry) < 3:
            continue
        reference, pad, net = entry[0], entry[1], entry[2]
        by_net.setdefault(str(net), []).append(f"{reference}-{pad}")
    return [
        {"net": net, "pads": sorted(pads), "pad_count": len(pads)}
        for net, pads in sorted(by_net.items())
        if len(pads) >= 2
    ]


def _diagnose_routing(drc_summary: dict, status: dict, constraints: dict, freerouting: dict | None) -> dict:
    """Turn a real DRC report + ratsnest into categorized, actionable causes."""
    counts: dict[str, int] = {}
    examples: dict[str, str] = {}
    for violation in drc_summary.get("violations", []):
        vtype = violation.get("type") or "unknown"
        counts[vtype] = counts.get(vtype, 0) + 1
        examples.setdefault(vtype, violation.get("description") or "")

    categories: list[dict] = []
    for vtype, count in sorted(counts.items(), key=lambda kv: -kv[1]):
        category, adjustable, adjustment = _VIOLATION_CATEGORIES.get(
            vtype, ("uncategorized_violation", False, "No known routing-parameter adjustment for this DRC type.")
        )
        categories.append(
            {
                "category": category,
                "drc_type": vtype,
                "count": count,
                "example": examples.get(vtype, "")[:300],
                "routing_adjustable": adjustable,
                "suggested_adjustment": adjustment,
            }
        )

    unrouted = _unrouted_nets_from_status(status)
    if unrouted:
        plateaued = bool(freerouting and freerouting.get("optimizer_plateaued"))
        categories.append(
            {
                "category": "unrouted_net",
                "drc_type": "unconnected_items",
                "count": len(unrouted),
                "example": ", ".join(f"{u['net']} ({'/'.join(u['pads'])})" for u in unrouted[:5]),
                "routing_adjustable": True,
                "suggested_adjustment": (
                    "Freerouting reported its optimizer plateaued, so more passes provably will "
                    "not help -- try a different optimization/selection strategy instead."
                    if plateaued
                    else "Retry with more passes, then with a different optimization strategy."
                ),
            }
        )

    if freerouting and freerouting.get("ignored_layers"):
        categories.append(
            {
                "category": "padstack_on_non_copper_layer_ignored",
                "drc_type": None,
                "count": len(freerouting["ignored_layers"]),
                "example": (
                    "Freerouting could not place padstacks declared on "
                    f"{', '.join(freerouting['ignored_layers'])}: the DSN names a layer that is "
                    "not in its layer structure. Reported rather than hidden."
                ),
                "routing_adjustable": False,
                "suggested_adjustment": "Export-side issue; no routing parameter affects it.",
            }
        )

    return {
        "categories": categories,
        "unrouted_nets": unrouted,
        "board_constraints": {
            key: constraints.get(key)
            for key in ("min_track_width", "min_clearance", "min_hole_clearance", "min_via_diameter")
            if key in constraints
        },
        "freerouting_self_report": freerouting or {},
    }


# ---------------------------------------------------------------------------
# P4-03 retry loop.
# ---------------------------------------------------------------------------


def _drc_summary(pcb_path: str | Path) -> dict:
    from kicad_mcp import kicad_cli

    kicad_cli.ensure_kicad_cli_on_path()
    report = kicad_cli.run_drc(pcb_path)
    violations = report.get("violations", []) or []
    unconnected = report.get("unconnected_items", []) or []
    return {
        "violations": violations,
        "violation_count": len(violations),
        "unconnected_count": len(unconnected),
        "short_count": sum(1 for v in violations if v.get("type") in _SHORT_VIOLATION_TYPES),
    }


def _route_score(summary: dict) -> tuple[int, int, int]:
    """Lower is better. Shorts dominate (a short is a hard failure), then
    missing connections, then remaining violations.

    This exists because Freerouting's own score cannot be trusted to rank
    attempts -- module docstring finding #7.
    """
    return (summary.get("short_count", 0), summary.get("unconnected_count", 0), summary.get("violation_count", 0))


def _apply_specs_to_board(board: Any, trace_specs: list[dict], via_specs: list[dict]) -> dict:
    """The one apply path both backends and confirm_route share. Via specs
    carry `size_mm`/`drill_mm`/`layers` from the routed copy (`route_delta`, for KRT and,
    since build ticket 46, for Freerouting), so the via the router sized is what lands; a
    spec without them falls back to kicad_tools' 0.6/0.3 mm defaults. kicad_tools skips an
    exact duplicate of copper already on the board; returns how many specs it skipped and
    the via specs it wrote, in order."""
    skipped = {"segments": 0, "vias": 0}
    written_vias: list[dict] = []
    for spec in trace_specs:
        added = board.add_trace(
            tuple(spec["start"]), tuple(spec["end"]), width=spec["width_mm"], layer=spec["layer"], net=spec["net"]
        )
        if not added:
            skipped["segments"] += 1
    for spec in via_specs:
        kwargs: dict[str, Any] = {"net": spec["net"]}
        if spec.get("size_mm") is not None:
            kwargs["size"] = spec["size_mm"]
        if spec.get("drill_mm") is not None:
            kwargs["drill"] = spec["drill_mm"]
        if spec.get("layers"):
            kwargs["layers"] = tuple(spec["layers"])
        if board.add_via(spec["x"], spec["y"], **kwargs) is None:
            skipped["vias"] += 1
        else:
            written_vias.append(spec)
    return {"duplicates_skipped": skipped, "written_via_specs": written_vias}


WRITE_NOT_VERIFIED = "write_not_verified"

# The `note` of every success output whose counts can all be 0 (get_unrouted_nets,
# confirm_route, and placement's confirm_placement_optimization and
# confirm_remove_graphic_items): a 0 there reads like a verdict and is not one (build
# ticket 60). tests/test_output_gate_wording.py checks these four outputs and scans src for
# the two keys written without a note, in the forms it lists there (not every form).
GATE_NOTE = (
    "A count of 0 here (drc_after, unrouted_net_count) comes from a single check, not the "
    "acceptance gate, and is not a verdict on this board. Only a verify_board PASS is a "
    "verdict; run verify_board."
)


def _apply_specs(
    board_path: str | Path,
    trace_specs: list[dict],
    via_specs: list[dict],
    *,
    expected_copper: list | None = None,
) -> dict:
    """Add the specs to the board through the write layer, verified before it replaces
    (docs/ARCHITECTURE.md decision 4). The check runs on the temporary sibling, from a fresh
    parse of it:

    * the write landed: the reload is the saved tree, and is the source plus exactly the
      nodes added (``_self_check.reload_differences``);
    * the intended change: one ``(segment ...)`` per trace spec and one ``(via ...)`` per via
      spec (less exact duplicates kicad_tools skipped, reported), each via with the size and
      drill its spec names;
    * with ``expected_copper`` (the routed copy's new copper from its raw S-expressions,
      ``import_ses_with_kicad``): the board's new copper is exactly that set, token for
      token (that nothing was removed is the first check's).

    On a mismatch the board is untouched and ``WriteLayerError(WRITE_NOT_VERIFIED, ...)``
    names the differences. Returns the bounded check and the skipped duplicates."""
    from kicad_tools.schema import PCB

    from kicad_mcp import _self_check

    board_path = Path(board_path)
    source = write_layer.load(board_path)
    board = PCB.load(board_path)
    before = {id(c) for c in board._sexp.children}
    applied = _apply_specs_to_board(board, trace_specs, via_specs)
    written = [c for c in board._sexp.children if id(c) not in before]
    skipped = applied["duplicates_skipped"]
    check: dict = {"duplicates_skipped": skipped}

    def verify(temp: Path) -> None:
        reloaded = write_layer.load(temp)
        differences = _self_check.reload_differences(
            source=source, removed=[], written=written, memory=board._sexp, reloaded=reloaded
        )
        problems = []
        if differences["missing"] or differences["unexpected"]:
            problems.append(_self_check.describe(differences))
        new_segments = [n for n in written if n.name == "segment"]
        new_vias = [n for n in written if n.name == "via"]
        want_segments = len(trace_specs) - skipped["segments"]
        want_vias = len(via_specs) - skipped["vias"]
        if (len(new_segments), len(new_vias)) != (want_segments, want_vias):
            problems.append(f"{len(new_segments)} segments and {len(new_vias)} vias written, "
                            f"{want_segments} and {want_vias} intended")
        for spec, node in zip(applied["written_via_specs"], new_vias, strict=False):
            for tag, key in (("size", "size_mm"), ("drill", "drill_mm")):
                if spec.get(key) is not None and _number(node, tag) != round(float(spec[key]), 6):
                    problems.append(f"via at ({spec['x']}, {spec['y']}): {tag} {_number(node, tag)} written, "
                                    f"{spec[key]} asked")
        if expected_copper is not None:
            # Copper removed from the board needs no check here: reload_differences above
            # already reports every source node missing from the reload.
            have = set(raw_copper_delta(source, reloaded)["added"])
            want = {tuple(tuple(v) if isinstance(v, list) else v for v in key) for key in expected_copper}
            if have != want:
                problems.append(
                    f"the board's new copper is not the routed copy's: {len(want - have)} routed items "
                    f"missing (e.g. {list(want - have)[:2]}), {len(have - want)} not routed "
                    f"(e.g. {list(have - want)[:2]})"
                )
        check.update(_self_check.bounded(differences))
        if problems:
            raise write_layer.WriteLayerError(
                WRITE_NOT_VERIFIED, "the written board is not the intended change: " + "; ".join(problems),
                mismatches=problems, file=str(board_path),
            )

    write_layer.save_pcb(board, board_path, verify=verify)
    return check


def _trial_copy(pcb_path: Path, dest_dir: Path) -> Path:
    """Copy a board plus its sibling project/rules/library files into a
    scratch directory.

    The `.kicad_pro` matters: it carries `min_track_width`/`min_clearance`,
    so a DRC run (or a KRT invocation -- KRT reads the sibling `.kicad_pro`
    for netclass defaults) against a bare copied `.kicad_pcb` would silently
    score against KiCad's/the STOCK netclass's defaults instead of the
    board's real per-netclass constraints (archived backlog P4-09: this produced
    18 real, hidden `Power_20A`-netclass clearance violations on a real
    board). `.kicad_dru` (custom design rules) and `fp-lib-table`/
    `sym-lib-table` (library resolution) matter for the same reason --
    without them, KRT/kicad-cli only ever see the board's built-in-default
    view of itself, never its real, fully-configured one.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    trial = dest_dir / pcb_path.name
    shutil.copy(pcb_path, trial)
    for suffix in (".kicad_pro", ".kicad_prl", ".kicad_dru"):
        sibling = pcb_path.with_suffix(suffix)
        if sibling.is_file():
            shutil.copy(sibling, trial.with_suffix(suffix))
    for table_name in ("fp-lib-table", "sym-lib-table"):
        sibling = pcb_path.parent / table_name
        if sibling.is_file():
            shutil.copy(sibling, dest_dir / table_name)
    return trial


_PRIORITIZED_SELECTION = {
    "label": "prioritized_selection",
    "reason": (
        "A different net order: Freerouting's prioritized selection strategy. The board's "
        "net-class widths and clearances stay as KiCad exported them (build ticket 46)."
    ),
    "optimization_strategy": None,
    "selection_strategy": "prioritized",
}


def _plan_next_attempt(categories: list[dict], freerouting: dict, already_tried: list[dict]) -> dict | None:
    """Pick the next parameter adjustment from the diagnosis -- the part that
    makes this a diagnose-and-retry loop rather than "re-run and hope".

    The levers are Freerouting's own: passes, optimization strategy, selection
    strategy (the protocol's Freerouting levers). Widths and clearances are never a
    lever: KiCad's DSN carries every net class's own rules, and the earlier
    "narrower_traces" retry rewrote every `(width)` and `(clearance)` of the DSN to
    one value, flattening the classes (build ticket 46 removed it).

    Returns None when nothing left is worth trying, so the loop stops early
    instead of burning its remaining budget (e.g. a board whose only
    remaining problems are design-rule conflicts routing cannot fix).
    """
    present = {c["category"] for c in categories if c.get("routing_adjustable")}
    tried_labels = {a["label"] for a in already_tried}
    candidates: list[dict] = []

    tight = {"clearance_violation", "short_circuit", "hole_clearance_violation", "solder_mask_bridge"}
    if present & tight:
        candidates.append({
            "label": "global_optimization_strategy",
            "reason": (
                "Clearance/short/hole-clearance violations present: retry with Freerouting's "
                "global optimization strategy; the net-class rules stay as the board defines them."
            ),
            "optimization_strategy": "global",
            "selection_strategy": None,
        })
        candidates.append(_PRIORITIZED_SELECTION)

    if "unrouted_net" in present or "dangling_track" in present:
        if freerouting.get("optimizer_plateaued"):
            candidates.append({
                "label": "global_optimization_strategy",
                "reason": (
                    "Nets remain unrouted and Freerouting reported its optimizer plateaued, so "
                    "more passes provably will not help -- switching optimization strategy instead."
                ),
                "optimization_strategy": "global",
                "selection_strategy": None,
            })
        else:
            candidates.append({
                "label": "more_passes",
                "reason": "Nets remain unrouted and the optimizer had not plateaued -- retry with more passes.",
                "optimization_strategy": None,
                "selection_strategy": None,
                "pass_multiplier": 4,
            })
        candidates.append(_PRIORITIZED_SELECTION)

    for candidate in candidates:
        if candidate["label"] not in tried_labels:
            return dict(candidate)
    return None


# ---------------------------------------------------------------------------
# P4-05: KiCadRoutingTools backend -- shares the scorer, the diagnosis
# categories, the trial-copy discipline and the checkpoint above; only the
# router-specific levers differ.
# ---------------------------------------------------------------------------

_KRT_FINER_GRID_MM = 0.05
_KRT_MORE_ITERATIONS = 800_000  # 4x KRT's default --max-iterations (200000)


def _plan_next_krt_attempt(categories: list[dict], router_log: dict, already_tried: list[dict]) -> dict | None:
    """KRT's retry levers, each backed by a signal the tool actually emits.

    * `finer_grid` -- KRT's own hint for fine-pitch pads ("boxed in by static
      obstacles ... try a finer grid", plus a `boxed_in` verdict per net in
      its JSON summary). Measured on the golden board under the pinned
      board floor: 3 open nets -> 2, still 0 violations.
    * `more_iterations` -- only when a net failed WITHOUT a boxed-in
      verdict; KRT states that when the search is exhausted against static
      obstacles a bigger budget provably does not help, so it is not spent
      there.

    Clearance/short violations are deliberately not retried against:
    under `board` policy the floor is already pinned (a violation there is
    a KRT defect worth reporting, not a knob), `fab` is the caller's
    explicit opt-in to descents, and `off` routes at exact sizes.
    """
    present = {c["category"] for c in categories if c.get("routing_adjustable")}
    tried_labels = {a["label"] for a in already_tried}
    last_plan = already_tried[-1].get("plan", {}) if already_tried else {}
    if "unrouted_net" not in present and "dangling_track" not in present:
        return None
    failed = set(router_log.get("failed_nets") or [])
    boxed = set(router_log.get("boxed_in_nets") or [])
    if "finer_grid" not in tried_labels and (boxed or failed or "unrouted_net" in present):
        return {
            "label": "finer_grid",
            "reason": (
                "Nets remain open and KiCadRoutingTools reports them boxed in by static obstacles "
                f"({', '.join(sorted(boxed)) or 'per its failed-net list'}); its own hint for fine-pitch pads is a "
                f"finer routing grid -- retrying with --grid-step {_KRT_FINER_GRID_MM}."
            ),
            "grid_step_mm": _KRT_FINER_GRID_MM,
            "max_iterations": last_plan.get("max_iterations"),
        }
    if "more_iterations" not in tried_labels and (failed - boxed):
        return {
            "label": "more_iterations",
            "reason": (
                f"Net(s) {', '.join(sorted(failed - boxed))} failed without a boxed-in verdict, so the search "
                f"budget, not static geometry, is the limit -- retrying with --max-iterations {_KRT_MORE_ITERATIONS}."
            ),
            "grid_step_mm": last_plan.get("grid_step_mm"),
            "max_iterations": _KRT_MORE_ITERATIONS,
        }
    return None


def _diagnose_krt_routing(
    drc_summary: dict, status: dict, constraints: dict, router_log: dict, floors: dict, escalation: str
) -> dict:
    """The shared P4-03 diagnosis, with KRT's real signals in place of
    Freerouting's: `boxed_in` verdicts steer the unrouted-net suggestion,
    and a `router_relaxed_board_rules` category is added whenever KRT's own
    self-report shows copper delivered below the board's floor."""
    diagnosis = _diagnose_routing(drc_summary, status, constraints, None)
    boxed = set(router_log.get("boxed_in_nets") or [])
    for category in diagnosis["categories"]:
        if category["category"] == "unrouted_net":
            category["suggested_adjustment"] = (
                "KiCadRoutingTools reports these pads boxed in by static obstacles (neighbouring pads + "
                "clearance) -- retry with a finer grid; if that fails too, the board's own rules do not fit "
                "the footprint pitch (see docs/ARCHITECTURE.md's meta-finding) and only a rule/footprint change helps."
                if boxed
                else "Retry with a finer grid, then a larger search budget."
            )
        elif category["category"] in ("clearance_violation", "short_circuit", "hole_clearance_violation"):
            if escalation == "board":
                category["suggested_adjustment"] = (
                    "Unexpected under escalation='board': the board's own rules were pinned as a hard "
                    "--fab-overrides floor, so KiCadRoutingTools should not have produced this. Report upstream "
                    "with the attempt's router self-report; not retried against."
                )
                category["routing_adjustable"] = False
            elif escalation == "fab":
                category["suggested_adjustment"] = (
                    "Expected consequence of escalation='fab' (explicit opt-in): the router relaxed the "
                    "board's floors to reach connectivity. Re-run with escalation='board' for rule-respecting copper."
                )
                category["routing_adjustable"] = False
    descents = krt_backend.descents_below_floor(router_log, floors)
    if descents:
        diagnosis["categories"].append(
            {
                "category": "router_relaxed_board_rules",
                "drc_type": None,
                "count": len(descents),
                "example": "; ".join(
                    f"{d['rule']}: board floor {d['board_floor_mm']} mm, delivered {d['delivered_mm']} mm" for d in descents
                ),
                "routing_adjustable": escalation != "fab",
                "suggested_adjustment": (
                    "KiCadRoutingTools' own self-report shows copper below the board's floor. Under 'fab' this is "
                    "the opted-in behaviour; under 'board' it means the generated --fab-overrides floor was not "
                    "honoured -- a KRT defect to report upstream."
                ),
            }
        )
    diagnosis["kicad_routing_tools_self_report"] = {
        key: router_log.get(key)
        for key in (
            "failed_nets", "boxed_in_nets", "min_clearance_used_mm", "min_delivered_mm",
            "narrowed_count", "fab_tier_escalations", "rescue", "pad_pairs_open", "project_rule_writes",
            "fab_floor_relaxed",
        )
    }
    return diagnosis


def _krt_manufacturability(trial_pcb: Path, router_log: dict, manufacturer: str, layer_count: int) -> dict:
    """The mandatory check for `escalation='fab'`: grade what KRT DELIVERED
    (its self-reported minimum clearance/track/via, overlaid onto the
    board's own rules) against the fab's published floor. Evidence-gated."""
    try:
        from kicad_mcp.manufacturing_profiles import compare_to_profile
        from kicad_mcp.manufacturing_profiles.board_rules import read_board_design_rules

        board_rules = read_board_design_rules(trial_pcb)
        overlay = krt_backend.delivered_rules_overlay(board_rules, router_log)
        result = compare_to_profile(trial_pcb, manufacturer, layer_count, 1.0, board_result=overlay)
        result["graded"] = (
            "the router's DELIVERED minimums (self-reported) overlaid on the board's own rules -- "
            "not the untouched project file, which would hide every descent"
        )
        result["delivered_overlay_mm"] = overlay.get("delivered_overlay", {})
        return result
    except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
        return _incomplete(f"manufacturability check failed: {exc}")


_KRT_ROUTING_NET_RE = re.compile(r"^\[\d+/\d+(?:\s*\(\d+\s*failed\))?\]\s*Routing\s+(?P<net>.+?)\s*\(id=\d+\)\s*$")
_KRT_BLOCKING_LINE_RE = re.compile(r"Blocking obstacles:\s*(?P<items>.+?)\s*(?:\[x(?P<repeat>\d+)\])?\s*$")
_KRT_BLOCKER_ITEM_RE = re.compile(r"^(?P<name>.+)\((?P<count>\d+)\s*(?:pad|track)\)$")


def parse_krt_blocking_lines(log_text: str) -> dict[str, "Counter[str]"]:
    """Parse KRT's real, verbose per-attempt routing log for "Blocking
    obstacles: NetA(N pad), NetB(M track) [xK]" lines, attributing each to
    the net being routed when they appeared (the preceding "[i/n] Routing
    <net> (id=X)" line).

    This is the same textual evidence a human would grep/read by hand to
    diagnose a stuck route (tickets 05/12 both did exactly this) -- doing it
    here means it doesn't have to be redone by hand on every future attempt.
    Distinct from, and does not replace, KRT's own structured `--json-out`
    `blockers` field (already parsed by `kicad_routing_tools.parse_route_log`
    into `router_log["blockers"]`, but never consumed downstream today):
    that one is net-name-only and reflects only nets STILL failed at the end
    of the whole run; this one is per-attempt and captures every stuck
    frontier along the way, weighted by KRT's own `[xN]` repeat-count for
    identical consecutive log lines (a stronger, repeated signal than a
    single occurrence).

    Returns: {failing_net_name: Counter({blocking_net_name: weighted_count})}
    """
    from collections import Counter

    current_net: str | None = None
    result: dict[str, Counter] = {}
    for raw_line in log_text.splitlines():
        line = raw_line.strip()
        net_match = _KRT_ROUTING_NET_RE.match(line)
        if net_match:
            current_net = net_match.group("net").strip()
            continue
        block_match = _KRT_BLOCKING_LINE_RE.search(line)
        if not block_match or current_net is None:
            continue
        repeat = int(block_match.group("repeat")) if block_match.group("repeat") else 1
        counter = result.setdefault(current_net, Counter())
        for item in block_match.group("items").split(", "):
            item_match = _KRT_BLOCKER_ITEM_RE.match(item.strip())
            if item_match:
                counter[item_match.group("name")] += repeat
    return result


def _absolute_footprint_positions_by_net(board: Any) -> dict[str, list[tuple[str, float, float]]]:
    """Real (ref, x_mm, y_mm) footprint anchors per net name, keyed by every
    net each footprint has at least one pad on.

    Uses `footprint.position` (the whole-footprint placement anchor every
    other tool in this project's placement.py already trusts and mutates)
    rather than individual `Pad.position` -- kicad_tools has repeatedly been
    found to report per-pad positions as footprint-local rather than
    absolute for some footprints (docs/ARCHITECTURE.md's "Eighth kicad_tools
    finding"; the M-02 bulk-cap and M-03 SMD-pad cases on this exact board).
    A footprint's own anchor doesn't have that problem, and is precise
    enough for "which footprint is near this congestion" -- exact per-pad
    geometry isn't needed for a placement suggestion.
    """
    by_net: dict[str, list[tuple[str, float, float]]] = {}
    for fp in board.footprints:
        nets_here = {pad.net_name for pad in fp.pads if getattr(pad, "net_name", None)}
        if not nets_here:
            continue
        x, y = fp.position
        ref = getattr(fp, "reference", None) or getattr(fp, "ref", None) or "?"
        for net in nets_here:
            by_net.setdefault(net, []).append((ref, x, y))
    return by_net


def suggest_placement_fixes_for_congestion(
    pcb_path: str | Path,
    krt_log_text: str,
    *,
    proximity_mm: float = 15.0,
    max_suggestions: int = 10,
) -> dict:
    """Turn KRT's real per-attempt blocking evidence into specific,
    evidence-backed footprint-move suggestions -- automates the diagnosis
    step tickets 05/12 both did by hand (grep the log, eyeball which
    footprints are near the stuck cell, relocate them).

    Every suggestion cites real evidence: the failing net, the blocking net
    name, how many times KRT's search reported it as a frontier obstacle
    (`evidence_weight`), and the real board distance between the nearest
    footprint on each net -- never an unexplained recommendation. Diagnostic
    only: never moves anything itself, so it carries no dry-run/
    revision-token state (nothing to confirm).
    """
    from kicad_tools.schema import PCB

    blocking_by_net = parse_krt_blocking_lines(krt_log_text)
    if not blocking_by_net:
        return {
            "status": "complete",
            "suggestion_count": 0,
            "suggestions": [],
            "note": "no 'Routing <net>' + 'Blocking obstacles:' pairs found in the given log text",
        }

    path = Path(pcb_path)
    if not path.is_file():
        return _incomplete(f"file not found: {pcb_path}")
    try:
        board = PCB.load(path)
    except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
        return _incomplete(f"could not load board: {exc}")

    positions_by_net = _absolute_footprint_positions_by_net(board)

    suggestions: list[dict] = []
    for failing_net, blockers in blocking_by_net.items():
        failing_positions = positions_by_net.get(failing_net, [])
        if not failing_positions:
            continue  # net name not resolvable on the CURRENT board (renamed/removed since the log was captured)
        for blocking_net, weight in blockers.most_common():
            if blocking_net == failing_net:
                continue
            blocker_positions = positions_by_net.get(blocking_net)
            if not blocker_positions:
                continue
            # Nearest (failing-net footprint, blocking-net footprint) pair -- the real,
            # board-accurate localization step. KRT's log prints grid cell coordinates,
            # not board-absolute mm with a known origin, so they aren't used here;
            # this real-footprint-distance approach sidesteps that ambiguity entirely.
            best: tuple[float, str, float, float, str, float, float] | None = None
            for f_ref, fx, fy in failing_positions:
                for b_ref, bx, by in blocker_positions:
                    if f_ref == b_ref:
                        continue
                    d = ((fx - bx) ** 2 + (fy - by) ** 2) ** 0.5
                    if best is None or d < best[0]:
                        best = (d, f_ref, fx, fy, b_ref, bx, by)
            if best is None or best[0] > proximity_mm:
                continue
            dist, f_ref, fx, fy, b_ref, bx, by = best
            suggestions.append(
                {
                    "failing_net": failing_net,
                    "blocking_net": blocking_net,
                    "evidence_weight": weight,
                    "near_footprint_on_failing_net": {"ref": f_ref, "x_mm": round(fx, 3), "y_mm": round(fy, 3)},
                    "candidate_to_move": {"ref": b_ref, "x_mm": round(bx, 3), "y_mm": round(by, 3)},
                    "distance_mm": round(dist, 3),
                    "reason": (
                        f"KiCadRoutingTools' real search reported net '{blocking_net}' as a frontier "
                        f"obstacle {weight}x while routing '{failing_net}'; the nearest real footprint "
                        f"on '{blocking_net}' ({b_ref}) is {dist:.2f}mm from a footprint on '{failing_net}' "
                        f"({f_ref}) -- consider relocating {b_ref} further away to relieve this."
                    ),
                }
            )

    suggestions.sort(key=lambda s: (-s["evidence_weight"], s["distance_mm"]))
    suggestions = suggestions[:max_suggestions]
    return {
        "status": "complete",
        "suggestion_count": len(suggestions),
        "suggestions": suggestions,
        "note": (
            "Diagnostic only -- does not move anything itself. Apply via place_footprints "
            "(pass the target's real current rotation explicitly, see archived backlog P3-08) and "
            "re-verify with pcb_check_collisions afterward (watch for archived backlog P3-11 too)."
        ),
    }


def _router_exit_status(exit_code: int | None) -> str:
    """Interpret KRT's exit code: 0 clean; 3 is `--strict-sizes` ("something
    was delivered below its requested size"), a disclosure rather than a
    crash; anything else is a router failure and never reads as success."""
    if exit_code == 0:
        return "ok"
    if exit_code == krt_backend.STRICT_SIZES_EXIT_CODE:
        return "delivered_below_requested_size"
    return f"router_exit_nonzero:{exit_code}"


def _router_flags(record: dict) -> list[str]:
    """Reasons an attempt must not be confirmed silently: delivered below the
    requested size (exit 3 or a narrowed feature), an abnormal router exit,
    or a router version that differs from the pinned CLI snapshot."""
    flags: list[str] = []
    if record.get("strict_sizes_violated"):
        flags.append("strict_sizes_violated")
    status = str(record.get("router_exit_status") or "")
    if status.startswith("router_exit_nonzero"):
        flags.append(status)
    if not (record.get("version_check") or {}).get("matches", True):
        flags.append("router_version_mismatch")
    return flags


def _attempt_rank(score: tuple, flags: list[str]) -> tuple:
    """Best-attempt ordering (lower wins). The real-DRC score decides first:
    a flagged attempt that is strictly better on shorts/unconnected/violations
    still wins (and then needs acknowledgement). Only on an EQUAL score does
    an unflagged attempt beat a flagged one."""
    return (tuple(score), 1 if flags else 0)


def _propose_with_kicad_routing_tools(
    path: Path,
    *,
    constraints: dict,
    unrouted_before: int,
    layer_count: int,
    max_attempts: int,
    timeout_seconds: int,
    escalation: str,
    manufacturer: str,
) -> dict:
    """The KRT attempt loop. Same contract as the Freerouting loop in
    `propose_route_with_retry`: never writes `path`; every attempt routes a
    throwaway copy, its added copper is diffed out through `kicad_tools`,
    re-applied to a second throwaway copy and scored by real DRC there --
    so the scored board is byte-for-byte what `confirm_route` will build."""
    from kicad_tools.schema import PCB

    availability = krt_backend.check_availability()
    if availability.get("status") != "complete":
        return _incomplete(
            f"KiCadRoutingTools is not usable: {availability.get('reason')}",
            router_availability=availability,
            install_hints=availability.get("install_hints", []),
        )

    floor_report = krt_backend.board_floor_overrides(path)
    floors = floor_report["floors"]
    clearance_mm = floors.get("clearance")
    attempts: list[dict] = []
    best: dict | None = None

    with tempfile.TemporaryDirectory() as tmp_dir:
        tmp_root = Path(tmp_dir)
        overrides_path: Path | None = None
        if escalation == "board" and floors:
            overrides_path = krt_backend.write_fab_overrides(floors, tmp_root / "board_floor_overrides.txt")
        clearance_policy = {
            "escalation": escalation,
            "clearance_mm_passed": clearance_mm,
            "clearance_source": floor_report["sources"].get("clearance"),
            "hard_floor_pinned": overrides_path is not None,
            "hard_floor_mm": floors if overrides_path is not None else {},
            "hard_floor_sources": floor_report["sources"] if overrides_path is not None else {},
            "floors_left_to_router_tier": floor_report["omitted"],
            "why": (
                "Measured (P4-05): --clearance alone does not bound KRT's rescue descents (8 clearance violations "
                "either way on the golden board); a --fab-overrides file pinning the board's own rules does "
                "(0 violations). Under 'board' both are passed."
                if escalation == "board"
                else "escalation='fab' is an explicit opt-in to descend below the board's floors; 'off' routes at exact sizes."
            ),
        }

        for attempt_index in range(1, max_attempts + 1):
            if attempt_index == 1:
                plan = {
                    "label": "baseline",
                    "reason": f"First attempt -- escalation={escalation}, grid 0.1 mm, KRT default search budget.",
                    "grid_step_mm": None,
                    "max_iterations": None,
                }
            else:
                last = attempts[-1]
                plan = _plan_next_krt_attempt(last["diagnosis"]["categories"], last["router_log"], attempts)
                if plan is None:
                    break

            attempt_dir = tmp_root / f"attempt_{attempt_index}"
            router_input = _trial_copy(path, attempt_dir / "input")  # KRT reads the sibling .kicad_pro for netclass defaults
            routed_pcb = attempt_dir / "routed" / path.name
            base_record = {
                "attempt": attempt_index,
                "label": plan["label"],
                "reason": plan["reason"],
                "plan": {k: plan.get(k) for k in ("grid_step_mm", "max_iterations")},
                "parameters": {"escalation": escalation, "clearance_mm": clearance_mm,
                               "fab_overrides": str(overrides_path) if overrides_path else None,
                               **{k: plan.get(k) for k in ("grid_step_mm", "max_iterations")}},
            }

            try:
                run = krt_backend.run_kicad_routing_tools(
                    router_input,
                    routed_pcb,
                    escalation=escalation,
                    clearance_mm=clearance_mm,
                    fab_overrides=overrides_path,
                    grid_step_mm=plan.get("grid_step_mm"),
                    max_iterations=plan.get("max_iterations"),
                    timeout=timeout_seconds,
                )
            except krt_backend.KicadRoutingToolsError as exc:
                attempts.append({**base_record, "error": f"KiCadRoutingTools failed: {exc}"})
                break

            # The router wrapper restored the ORIGINAL .kicad_pro beside the
            # routed copy before returning (module docstring finding #10);
            # `run["router_overwrote_project"]` discloses what KRT did to it.

            try:
                delta = krt_backend.route_delta(path, routed_pcb)
                trace_specs, via_specs = delta["trace_specs"], delta["via_specs"]
                trial_pcb = _trial_copy(path, attempt_dir / "trial")
                _apply_specs(trial_pcb, trace_specs, via_specs)
                drc_summary = _drc_summary(trial_pcb)
                trial_status = PCB.load(trial_pcb).routing_status()
            except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
                attempts.append({**base_record, "error": f"could not diff/apply/verify trial route: {exc}",
                                 "router_log": run["log"]})
                break

            diagnosis = _diagnose_krt_routing(drc_summary, trial_status, constraints, run["log"], floors, escalation)
            score = _route_score(drc_summary)
            record = {
                **base_record,
                "router_elapsed_s": run["elapsed_s"],
                "router_exit_code": run["exit_code"],
                "router_exit_status": _router_exit_status(run["exit_code"]),
                "strict_sizes_violated": bool(run.get("strict_sizes_violated")),
                "delivered_below_requested": list(run.get("delivered_below_requested") or []),
                "version_check": run.get("version_check"),
                "router_version": run.get("router_version"),
                "logged_parameters": run.get("logged_parameters"),
                "router_log": run["log"],
                "router_rewrote_project_file": bool(run["log"].get("project_rule_writes"))
                or bool(run.get("router_overwrote_project"))
                or bool(run.get("project_file_written_by_router")),
                "delta": {k: delta[k] for k in ("removed_segment_count", "removed_via_count")},
                "drc_summary": {k: v for k, v in drc_summary.items() if k != "violations"},
                "diagnosis": diagnosis,
                "score": {"shorts": score[0], "unconnected": score[1], "violations": score[2]},
                "trace_count": len(trace_specs),
                "via_count": len(via_specs),
            }
            if escalation == "fab":
                record["manufacturability"] = _krt_manufacturability(trial_pcb, run["log"], manufacturer, layer_count)
            attempts.append(record)

            record["router_flags"] = _router_flags(record)
            rank = _attempt_rank(score, record["router_flags"])
            if best is None or rank < best["_rank"]:
                best = {"_score": score, "_rank": rank, "trace_specs": trace_specs, "via_specs": via_specs,
                        "attempt": attempt_index}

            if score[0] == 0 and score[1] == 0 and score[2] == 0 and not record["router_flags"]:
                break  # nothing left to retry for

    if best is None:
        return _incomplete("no attempt produced a usable route", attempts=attempts, backend="kicad_routing_tools",
                           router_availability=availability)

    best_flags = attempts[best["attempt"] - 1].get("router_flags") or []
    token = _register_proposal(path, best["trace_specs"], best["via_specs"], router_flags=best_flags)
    best_record = attempts[best["attempt"] - 1]
    result = {
        "status": "pending_confirmation",
        "backend": "kicad_routing_tools",
        "revision_token": token,
        "best_attempt": best["attempt"],
        "attempts_tried": len(attempts),
        "trace_count": len(best["trace_specs"]),
        "via_count": len(best["via_specs"]),
        "unrouted_nets_before": unrouted_before,
        "requires_acknowledgement": bool(best_flags),
        "router_flags": best_flags,
        # What the router wrapper knows about the BEST attempt's run:
        "strict_sizes_violated": bool(best_record.get("strict_sizes_violated")),
        "delivered_below_requested": best_record.get("delivered_below_requested") or [],
        "router_exit_status": best_record.get("router_exit_status"),
        "version_check": best_record.get("version_check"),
        "logged_parameters": best_record.get("logged_parameters"),
        "best_attempt_drc_summary": best_record.get("drc_summary"),
        "best_attempt_diagnosis": best_record.get("diagnosis"),
        "clearance_policy": clearance_policy,
        "project_file_policy": (
            "KiCadRoutingTools' rewritten .kicad_pro was discarded on every attempt; all DRC numbers above are "
            "against the board's ORIGINAL rules, and confirm_route applies only traces/vias through kicad_tools."
        ),
        "router_availability": {k: availability.get(k) for k in ("checkout", "python", "grid_router_version")},
        "attempts": attempts,
        "warning": (
            "Autorouted output -- review before trusting, even the best-scoring attempt. "
            "Call confirm_route with this revision_token to actually apply it, then run "
            "verify_board on the board: only its PASS is a verdict (pcb_run_drc and the DRC "
            "counts here are single runs, not the gate)."
        ),
    }
    if best_flags:
        result["warning"] = (
            f"This proposal carries router flags {best_flags}: confirm_route will REFUSE unless called with "
            "acknowledge_router_flags=True. " + result["warning"]
        )
    if result["strict_sizes_violated"]:
        result["warning"] = (
            "The router delivered below the requested size on "
            f"{len(result['delivered_below_requested'])} feature(s) (exit status "
            f"{result['router_exit_status']}); see delivered_below_requested. " + result["warning"]
        )
    if str(result["router_exit_status"]).startswith("router_exit_nonzero"):
        result["warning"] = (
            f"The router exited abnormally ({result['router_exit_status']}); the board it wrote is "
            "not a clean result. " + result["warning"]
        )
    version_check = result["version_check"] or {}
    if version_check and not version_check.get("matches", True):
        result["warning"] = (
            f"Router version check FAILED: {version_check.get('message')}. " + result["warning"]
        )
    if escalation == "fab":
        result["manufacturability"] = best_record.get("manufacturability")
        result["warning"] = (
            "escalation='fab' let the router descend BELOW the board's own design rules to reach connectivity; "
            "the DRC numbers are against the ORIGINAL rules, so expect violations, and read `manufacturability` "
            "before accepting. " + result["warning"]
        )
    elif escalation == "off":
        result["warning"] = (
            "escalation='off' is known to leave a dangling via from an abandoned rescue attempt (real DRC reports "
            "it as via_dangling and it is counted in the score). " + result["warning"]
        )
    return result


# ---------------------------------------------------------------------------
# Pending route proposals -- same bounded+TTL'd pattern as
# toolsets/sourcing.py's _PENDING_MATCHES (not literally shared code across
# toolsets, but the same design, per docs/ARCHITECTURE.md's dry-run+revision-token
# mechanism: each pending proposal is (data, expires_at)).
# ---------------------------------------------------------------------------

_PENDING_ROUTES: dict[str, tuple[dict, float]] = {}
_PENDING_ROUTE_TTL_SECONDS = 60 * 60
_PENDING_ROUTE_MAX_ENTRIES = 1000


def _evict_stale_pending_routes() -> None:
    now = time.time()
    expired = [token for token, (_, expires_at) in _PENDING_ROUTES.items() if expires_at <= now]
    for token in expired:
        del _PENDING_ROUTES[token]
    if len(_PENDING_ROUTES) > _PENDING_ROUTE_MAX_ENTRIES:
        oldest_first = sorted(_PENDING_ROUTES.items(), key=lambda kv: kv[1][1])
        overflow = len(_PENDING_ROUTES) - _PENDING_ROUTE_MAX_ENTRIES
        for token, _ in oldest_first[:overflow]:
            del _PENDING_ROUTES[token]


def _incomplete(reason: str, **extra: Any) -> dict:
    return {"status": "incomplete", "reason": reason, **extra}


def _board_changed(exc: BoardChangedDuringRouting, **extra: Any) -> dict:
    """A board edited while it was being routed: refused by name, no proposal parked."""
    return {"status": "refused", "ok": False, "code": exc.code, "reason": str(exc), **extra}


def _register_proposal(
    path: Path,
    trace_specs: list[dict],
    via_specs: list[dict],
    router_flags: list[str] | None = None,
    *,
    expected_copper: list | None = None,
) -> str:
    """Park a proposal behind a fresh revision token (shared by every backend).
    `router_flags` makes confirm_route refuse without an explicit acknowledgement;
    `expected_copper` (Freerouting, build ticket 46) is what confirm_route's self-check
    compares the written board with."""
    _evict_stale_pending_routes()
    token = uuid.uuid4().hex
    proposal = {"pcb_path": str(path), "trace_specs": trace_specs, "via_specs": via_specs,
                "router_flags": list(router_flags or []), "expected_copper": expected_copper}
    _PENDING_ROUTES[token] = (proposal, time.time() + _PENDING_ROUTE_TTL_SECONDS)
    return token


def _freerouting_attempt(
    export: dict,
    attempt_dir: Path,
    *,
    max_passes: int,
    timeout: int,
    optimization_strategy: str | None = None,
    selection_strategy: str | None = None,
) -> dict:
    """One Freerouting run on KiCad's DSN (``export_dsn_with_kicad``'s result), its SES
    imported by KiCad into a copy of the export's snapshot and diffed against it
    (``import_ses_with_kicad``). Raises ``FreeroutingError`` or ``KicadSpecctraError``
    (``BoardChangedDuringRouting`` when the board changed meanwhile)."""
    ses = attempt_dir / "board.ses"
    log = run_freerouting(export["dsn_path"], ses, max_passes=max_passes, timeout=timeout,
                          optimization_strategy=optimization_strategy, selection_strategy=selection_strategy)
    imported = import_ses_with_kicad(export, ses, attempt_dir / "routed")
    return {"freerouting_log": log, **imported}


_FREEROUTING_FLAG_MEANINGS = {
    "ses_import_incomplete": "KiCad's SES import is not the session: it skipped items, put copper on no net, "
                             "ignored polygon wires, added more copper than the session holds, or a count "
                             "was unreadable",
    "arcs_not_applied": "the routed copy holds arcs; confirm_route applies its segments and vias only, so "
                        "those connections stay unrouted",
    "existing_copper_removed": "copper already on the board is missing from the routed copy",
}


def _freerouting_flags(attempt: dict) -> list[str]:
    """Router flags for a Freerouting proposal (confirm_route refuses without an
    acknowledgement): KiCad's import did not deliver the whole session
    (``ses_import_incomplete``), the routed copy holds arcs that are not applied
    (``arcs_not_applied``), or copper already on the board is gone from the routed copy
    (``existing_copper_removed``). Fails closed: a count that is missing or not a count is
    ``ses_import_incomplete``, never clean."""
    imported = attempt.get("import")
    imported = imported if isinstance(imported, dict) else {}
    counts = {key: imported.get(key) for key in
              ("skipped_items", "new_items_on_no_net", "ignored_wire_shapes", "arcs_not_applied")}
    counts["removed_copper"] = attempt.get("removed_copper")
    unexpected = imported.get("unexpected")
    unreadable = (bool(imported.get("report_problems")) or not all(_is_count(v) for v in counts.values())
                  or not _is_copper_count(unexpected))
    flags = []
    if (unreadable or counts["skipped_items"] or counts["new_items_on_no_net"] or counts["ignored_wire_shapes"]
            or any(unexpected[k] for k in _COPPER_KINDS)):
        flags.append("ses_import_incomplete")
    if _is_count(counts["arcs_not_applied"]) and counts["arcs_not_applied"]:
        flags.append("arcs_not_applied")
    if _is_count(counts["removed_copper"]) and counts["removed_copper"]:
        flags.append("existing_copper_removed")
    return flags


def build(mcp: MCPServer) -> list[str]:
    """Register the routing toolset. See toolsets/registry.py for the contract."""

    def add_traces(pcb_path: str, specs: list[dict]) -> dict:
        """Add multiple traces to a PCB in one call -- batch tool, never
        loop this one segment at a time.

        Each spec: {"start": [x_mm, y_mm], "end": [x_mm, y_mm],
        "width_mm": 0.25, "layer": "F.Cu", "net": "GND"}. Coordinates are
        board-local mm (same frame as footprint positions this project's
        other tools already use, e.g. pcb_validate_proposed_placements).
        Evidence-gated: a spec that fails to add is reported by index,
        the rest still get attempted.
        """
        path = Path(pcb_path)
        if not path.is_file():
            return _incomplete(f"file not found: {pcb_path}")
        try:
            from kicad_tools.schema import PCB

            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"could not load board: {exc}")

        added = 0
        errors: list[dict] = []
        for i, spec in enumerate(specs):
            try:
                board.add_trace(
                    tuple(spec["start"]),
                    tuple(spec["end"]),
                    width=spec.get("width_mm", 0.25),
                    layer=spec.get("layer", "F.Cu"),
                    net=spec.get("net"),
                )
                added += 1
            except Exception as exc:  # noqa: BLE001 - evidence-gated per-spec
                errors.append({"index": i, "spec": spec, "error": str(exc)})

        if added:
            try:
                write_layer.save_pcb(board, path)
            except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
                return _incomplete(f"traces added in memory but save failed: {exc}", added=added)

        status = "complete" if not errors else ("partial" if added else "error")
        return {"status": status, "added": added, "errors": errors}

    def get_unrouted_nets(pcb_path: str) -> dict:
        """Report every net that still needs routing, via the board's real
        ratsnest -- the direct answer to "what's left to route", as
        structured data rather than a rendered image.
        """
        path = Path(pcb_path)
        if not path.is_file():
            return _incomplete(f"file not found: {pcb_path}")
        try:
            from kicad_tools.schema import PCB

            board = PCB.load(path)
            ratsnest = board.get_ratsnest()
            status = board.routing_status()
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"could not compute ratsnest: {exc}")
        return {
            "status": "complete",
            "unrouted_net_count": len(ratsnest),
            "unrouted_nets": ratsnest,
            "segment_count": status.get("segments", 0),
            "via_count": status.get("vias", 0),
            "trace_length_mm": status.get("trace_length_mm", 0.0),
            "unrouted_pads": [list(p) for p in status.get("unrouted_pads", [])],
            "note": GATE_NOTE,
        }

    def propose_route_with_freerouting(pcb_path: str, max_passes: int = 10, timeout_seconds: int = 300) -> dict:
        """Run Freerouting (a real local subprocess, GPLv3, never imported
        as a library) against a board and PROPOSE the result -- does not
        modify the board file. Returns a revision_token; call
        confirm_route with it to actually apply the routing.

        Mandatory human checkpoint per docs/ARCHITECTURE.md: autorouting quality
        is inconsistent industry-wide (see archived research notes) -- never trust this
        output as final without review. Requires FREEROUTING_JAR_PATH to be
        set to a downloaded Freerouting release jar and a JVM capable of
        running it (confirmed: Freerouting 2.3.0 needs JRE >= ~25, JRE 21
        is NOT enough -- see find_java()'s docstring). The DSN is written and
        the result read back by KiCad's own Specctra exporter and importer in
        KiCad's bundled Python (KICAD_PYTHON_PATH, else the one next to
        kicad-cli); without it this returns `status: incomplete`.

        A board (or its .kicad_pro, .kicad_dru or library tables) saved while
        Freerouting runs is refused: `status: refused`, `ok: false`,
        `code: board_changed_during_routing`, no proposal. Run it again on the
        saved board.
        """
        path = Path(pcb_path)
        if not path.is_file():
            return _incomplete(f"file not found: {pcb_path}")
        try:
            from kicad_tools.schema import PCB

            board = PCB.load(path)
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"could not load board: {exc}")

        with tempfile.TemporaryDirectory() as tmp_dir:
            work = Path(tmp_dir)
            try:
                export = export_dsn_with_kicad(path, work / "dsn")
                attempt = _freerouting_attempt(export, work, max_passes=max_passes, timeout=timeout_seconds)
            except BoardChangedDuringRouting as exc:
                return _board_changed(exc)
            except KicadSpecctraError as exc:
                return _incomplete(f"KiCad's Specctra export or SES import failed: {exc}")
            except FreeroutingError as exc:
                return _incomplete(f"Freerouting failed: {exc}")

        unrouted_before = len(board.get_ratsnest())
        flags = _freerouting_flags(attempt)
        token = _register_proposal(path, attempt["trace_specs"], attempt["via_specs"], flags,
                                   expected_copper=attempt["expected_copper"])

        return {
            "status": "pending_confirmation",
            "revision_token": token,
            "trace_count": len(attempt["trace_specs"]),
            "via_count": len(attempt["via_specs"]),
            "unrouted_nets_before": unrouted_before,
            "dsn_exporter": export["exporter"],
            "ses_import": attempt["import"],
            "router_flags": flags,
            "requires_acknowledgement": bool(flags),
            "freerouting_log": attempt["freerouting_log"],
            "warning": (
                "Autorouted output -- review before trusting. Freerouting's routing "
                "quality is inconsistent, especially on dense/high-speed/differential-pair "
                "boards (see archived research notes). Call confirm_route with this revision_token to "
                "actually apply it, then run verify_board on the board: only its PASS is a "
                "verdict (a pcb_run_drc run is not the gate)."
            ),
        }

    def propose_route_with_retry(
        pcb_path: str,
        max_attempts: int = 3,
        max_passes: int = 10,
        timeout_seconds: int = 300,
        backend: str = "freerouting",
        escalation: str = "board",
        manufacturer: str = "jlcpcb",
        acknowledge_unmodeled_zone_risk: bool = False,
    ) -> dict:
        """Diagnose-and-retry autorouting (archived backlog P4-03/P4-05): run the
        chosen autorouter, score the result against real `kicad-cli` DRC
        (never the router's own self-reported score -- see module docstring
        finding #7; KRT's default policy has the same shape of problem),
        categorize WHY specific violations/unrouted nets exist, and if a
        category maps to a known parameter adjustment, retry with that
        adjustment. Bounded to `max_attempts` (default 3). Every attempt and
        adjustment tried is returned in `attempts`, so the reasoning is
        auditable, not a black box.

        `backend`: "freerouting" (default, unchanged behaviour; GPLv3 local
        subprocess, DSN/SES round trip, `max_passes` applies) or
        "kicad_routing_tools" (drandyhaas/KiCadRoutingTools, MIT, ~50x
        faster, routes .kicad_pcb directly; `max_passes` is not a KRT
        concept and is ignored). KRT is discovered via KICAD_ROUTING_TOOLS_PATH
        (a checkout containing py_router/route.py) or KiCad's PCM plugin dir,
        and run with KICAD_ROUTING_TOOLS_PYTHON (default: this interpreter),
        which needs numpy/scipy/shapely/Pillow and KRT's prebuilt grid_router
        binary (`python build_router.py`); anything missing comes back as
        `status: incomplete` naming it.

        `escalation` (KRT only): how far below a requested size a failing net
        may be narrowed. "board" (default) = the board's own design rules,
        pinned as a hard floor via a generated --fab-overrides file (measured:
        --clearance alone does NOT hold, see module docstring finding #9).
        "fab" = explicit opt-in to descend to the fab tier below the board's
        rules -- reaches connectivity by relaxing rules, so the result is
        additionally graded by `pcb_check_manufacturability` against
        `manufacturer` (default jlcpcb, whose tiers KRT's ladder mirrors) and
        surfaced under `manufacturability`. "off" = exact sizes, no rescue
        (leaves a dangling via on the golden board; real DRC reports it).
        KRT's rewritten `.kicad_pro` is never adopted under any policy; every
        DRC number is against the board's ORIGINAL rules.

        Same checkpoint contract as propose_route_with_freerouting -- this
        NEVER writes to `pcb_path`. Every attempt routes and DRCs a throwaway
        copy in a temp directory; only the best-scoring attempt's traces/vias
        become the pending proposal, applied only by a subsequent
        confirm_route call with the returned revision_token.

        Freerouting 2.3.0 has no partial/selective-routing mode (confirmed via
        `-help`, module docstring finding #8) -- every retry is necessarily a
        full re-route with another pass count, optimization strategy or
        selection strategy, not a re-route of just the failing nets. The DSN
        comes from KiCad's own exporter (KiCad's bundled Python, KICAD_PYTHON_PATH
        or next to kicad-cli; missing = `status: incomplete`) and keeps every
        net class's width and clearance as the board defines them; a retry
        never rewrites them. As with propose_route_with_freerouting, a board
        saved while Freerouting runs is refused with
        `code: board_changed_during_routing` and no proposal.

        `acknowledge_unmodeled_zone_risk` (archived backlog P4-06): both backends
        route this board's file as-is; neither treats a pre-existing FILLED
        COPPER ZONE (a pour/plane, not a KiCad keepout rule area -- both
        backends do honor those) as an obstacle for a net other than the
        zone's own -- confirmed by reading KRT's `obstacle_map.
        build_base_obstacle_map` and the earlier `kicad_tools` DSN exporter (KiCad's
        own exporter, used since build ticket 46, sends a zone's outline as a
        plane, not its fill; whether Freerouting then keeps clear is not
        measured), then reproducing it directly: a
        LED_ANODE trace routed straight onto a pre-existing GND pour with
        0.0mm actual clearance, a real DRC-confirmed short. This is exactly
        what produced 12 real shorts routing a real board (openMPPT v1.3)
        through this backend. Default False: if the board carries any
        pre-existing filled copper zone, this call refuses up front (status
        `incomplete`, naming every such zone) rather than risk silently
        proposing a shorted board again. Pass True only after verifying out
        of band that it's actually safe here (e.g. the nets left to route
        can't reach any zone, or you will re-run `pcb_run_drc` on the
        confirmed result and treat every zone-adjacent violation as
        load-bearing, not noise) -- the returned result still carries a loud
        `zone_obstacle_warning` either way.
        """
        path = Path(pcb_path)
        if not path.is_file():
            return _incomplete(f"file not found: {pcb_path}")
        if backend not in ROUTING_BACKENDS:
            return _incomplete(f"unknown backend {backend!r}; choose one of {list(ROUTING_BACKENDS)}")
        if escalation not in krt_backend.VALID_ESCALATIONS:
            return _incomplete(f"unknown escalation {escalation!r}; choose one of {list(krt_backend.VALID_ESCALATIONS)}")
        try:
            from kicad_tools.schema import PCB

            board = PCB.load(path)
            unrouted_before = len(board.get_ratsnest())
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"could not load board: {exc}")

        zone_report = _pre_existing_filled_zone_report(board)
        if zone_report and not acknowledge_unmodeled_zone_risk:
            return _incomplete(
                f"board has {zone_report['filled_zone_count']} pre-existing filled copper "
                f"zone(s) -- refusing to route (archived backlog P4-06). Neither the "
                f"kicad_routing_tools backend nor the freerouting/DSN backend treats filled "
                f"zone copper as a routing obstacle for a different net (confirmed by reading "
                f"both backends' code and reproducing a real 0.0mm-clearance short); routing "
                f"this board as-is risks the same failure that produced 12 real shorts on "
                f"openMPPT v1.3, including a power rail shorted directly to another. Pass "
                f"acknowledge_unmodeled_zone_risk=True only once you've verified out of band "
                f"that this is safe, and run verify_board on the confirmed result: only its "
                f"PASS is a verdict (a pcb_run_drc run is not the gate).",
                **zone_report,
            )

        constraints = _read_board_constraints(path)

        # Reached only when there's nothing to warn about (zone_report is
        # None) or the caller explicitly accepted the risk above -- either
        # way, attach a loud, non-silent marker to whatever this call
        # returns rather than letting the acknowledgement be forgotten by
        # the time the result is read.
        zone_obstacle_warning = None
        if zone_report:
            zone_names = ", ".join(f"{z['net']}/{z['layer']}" for z in zone_report["filled_zones"])
            zone_obstacle_warning = (
                f"acknowledge_unmodeled_zone_risk=True was passed with {zone_report['filled_zone_count']} "
                f"pre-existing filled copper zone(s) on the board ({zone_names}). Neither backend can be "
                "trusted to route around them (archived backlog P4-06) -- run verify_board on the confirmed "
                "result and check every clearance/short violation near these zones in its complete "
                "violation list: only its PASS is a verdict (a pcb_run_drc run is not the gate)."
            )

        if backend == "kicad_routing_tools":
            try:
                layer_count = len(board.copper_layers) or 2
            except Exception:  # noqa: BLE001 - only used to pick a fab profile config
                layer_count = 2
            result = _propose_with_kicad_routing_tools(
                path,
                constraints=constraints,
                unrouted_before=unrouted_before,
                layer_count=layer_count,
                max_attempts=max_attempts,
                timeout_seconds=timeout_seconds,
                escalation=escalation,
                manufacturer=manufacturer,
            )
            if zone_obstacle_warning:
                result.setdefault("zone_obstacle_warning", zone_obstacle_warning)
            return result

        attempts: list[dict] = []
        best: dict | None = None

        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_root = Path(tmp_dir)
            # One DSN for every attempt: KiCad's exporter with the board's own net classes.
            # The retry levers are Freerouting's (passes, optimization and selection
            # strategy); no attempt rewrites a width or clearance (build ticket 46).
            try:
                export = export_dsn_with_kicad(path, tmp_root / "dsn")
            except KicadSpecctraError as exc:
                return _incomplete(f"KiCad's Specctra export failed: {exc}", attempts=attempts)

            for attempt_index in range(1, max_attempts + 1):
                if attempt_index == 1:
                    plan = {"label": "baseline", "reason": "First attempt -- Freerouting's defaults on KiCad's DSN.",
                            "optimization_strategy": None, "selection_strategy": None, "pass_multiplier": 1}
                else:
                    last = attempts[-1]
                    plan = _plan_next_attempt(last["diagnosis"]["categories"], last["freerouting_log"], attempts)
                    if plan is None:
                        break

                attempt_dir = tmp_root / f"attempt_{attempt_index}"
                attempt_dir.mkdir(parents=True, exist_ok=True)
                parameters = {
                    "max_passes": max_passes * plan.get("pass_multiplier", 1),
                    "optimization_strategy": plan.get("optimization_strategy"),
                    "selection_strategy": plan.get("selection_strategy"),
                }
                head = {"attempt": attempt_index, "label": plan["label"], "reason": plan["reason"],
                        "parameters": parameters}

                try:
                    attempt = _freerouting_attempt(export, attempt_dir, timeout=timeout_seconds, **parameters)
                except BoardChangedDuringRouting as exc:
                    return _board_changed(exc, attempts=attempts)
                except FreeroutingError as exc:
                    attempts.append({**head, "error": f"Freerouting failed: {exc}"})
                    break
                except KicadSpecctraError as exc:
                    attempts.append({**head, "error": f"KiCad's SES import failed: {exc}"})
                    break

                trial_pcb = _trial_copy(Path(export["snapshot_pcb"]), attempt_dir / "trial")
                try:
                    _apply_specs(trial_pcb, attempt["trace_specs"], attempt["via_specs"],
                                 expected_copper=attempt["expected_copper"])
                    drc_summary = _drc_summary(trial_pcb)
                    trial_status = PCB.load(trial_pcb).routing_status()
                except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
                    attempts.append({**head, "error": f"could not apply/verify trial route: {exc}",
                                     "code": getattr(exc, "code", None)})
                    break

                diagnosis = _diagnose_routing(drc_summary, trial_status, constraints, attempt["freerouting_log"])
                score = _route_score(drc_summary)
                flags = _freerouting_flags(attempt)
                record = {
                    **head,
                    "dsn_exporter": export["exporter"],
                    "ses_import": attempt["import"],
                    "router_flags": flags,
                    "drc_summary": {k: v for k, v in drc_summary.items() if k != "violations"},
                    "diagnosis": diagnosis,
                    "freerouting_log": attempt["freerouting_log"],
                    "score": {"shorts": score[0], "unconnected": score[1], "violations": score[2]},
                    "trace_count": len(attempt["trace_specs"]),
                    "via_count": len(attempt["via_specs"]),
                }
                attempts.append(record)

                if best is None or score < best["_score"]:
                    best = {"_score": score, "trace_specs": attempt["trace_specs"], "via_specs": attempt["via_specs"],
                            "expected_copper": attempt["expected_copper"], "router_flags": flags,
                            "attempt": attempt_index}

                if score[0] == 0 and score[1] == 0:
                    break  # no shorts, everything connected -- nothing left worth retrying for

        if best is None:
            return _incomplete("no attempt produced a usable route", attempts=attempts)

        token = _register_proposal(path, best["trace_specs"], best["via_specs"], best["router_flags"],
                                   expected_copper=best["expected_copper"])

        best_record = attempts[best["attempt"] - 1]
        result = {
            "status": "pending_confirmation",
            "backend": "freerouting",
            "revision_token": token,
            "best_attempt": best["attempt"],
            "attempts_tried": len(attempts),
            "trace_count": len(best["trace_specs"]),
            "via_count": len(best["via_specs"]),
            "unrouted_nets_before": unrouted_before,
            "best_attempt_drc_summary": best_record.get("drc_summary"),
            "best_attempt_diagnosis": best_record.get("diagnosis"),
            "router_flags": best["router_flags"],
            "requires_acknowledgement": bool(best["router_flags"]),
            "attempts": attempts,
            "warning": (
                "Autorouted output -- review before trusting, even the best-scoring attempt. "
                "Call confirm_route with this revision_token to actually apply it, then run "
                "verify_board on the board: only its PASS is a verdict (pcb_run_drc and the DRC "
                "counts here are single runs, not the gate)."
            ),
        }
        if zone_obstacle_warning:
            result["zone_obstacle_warning"] = zone_obstacle_warning
        return result

    def confirm_route(pcb_path: str, revision_token: str, acknowledge_router_flags: bool = False) -> dict:
        """Apply a previously-proposed autoroute result. Consumes the
        token -- a second confirm with the same token fails rather than
        re-applying, same pattern as sourcing.py's confirm_component_match.
        Runs the real pcb_run_drc against the result before returning, so
        the caller immediately knows whether the applied routing is
        actually clean, not just "some traces got added".

        acknowledge_router_flags: leave False (the default). A proposal whose
        propose result said `requires_acknowledgement: true` is REFUSED unless
        this is True, and the revision_token stays valid after the refusal.
        The flags: KRT `strict_sizes_violated` (a feature below its requested
        size), `router_exit_nonzero:<code>`, `router_version_mismatch`;
        Freerouting `ses_import_incomplete` (KiCad's SES import skipped items,
        put copper on no net, ignored polygon wires, added more copper than the
        session holds, or a count was unreadable), `arcs_not_applied` (the
        routed arcs are not applied, so those connections stay unrouted),
        `existing_copper_removed` (copper already on the board is missing from
        the routed copy). Set True only after the human has read the
        proposal's `router_flags` and their evidence and accepted them; never
        set it to get past a refusal on your own.

        A write that is not the routed copy's copper is refused with
        `code: write_not_verified`; the board is unchanged and the token stays
        valid.
        """
        _evict_stale_pending_routes()
        entry = _PENDING_ROUTES.pop(revision_token, None)
        if entry is None:
            return {"status": "error", "reason": "unknown or expired revision_token"}
        proposal, _expires_at = entry

        if proposal["pcb_path"] != str(Path(pcb_path)):
            _PENDING_ROUTES[revision_token] = entry  # put back -- caller mismatch, not a decision to discard
            return {"status": "error", "reason": "pcb_path does not match the path this proposal was made for"}

        router_flags = list(proposal.get("router_flags") or [])
        if router_flags and not acknowledge_router_flags:
            _PENDING_ROUTES[revision_token] = entry  # refusal is not a decision: the token stays valid
            meanings = "; ".join(f"{flag}: {_FREEROUTING_FLAG_MEANINGS[flag]}" for flag in router_flags
                                 if flag in _FREEROUTING_FLAG_MEANINGS)
            return {
                "status": "refused",
                "reason": (
                    f"this proposal carries router flags {router_flags}"
                    + (f" ({meanings})" if meanings else "")
                    + "; review the propose result's evidence (KRT: delivered_below_requested, version_check; "
                    "Freerouting: ses_import, or attempts[best_attempt - 1].ses_import from "
                    "propose_route_with_retry) and call confirm_route again with "
                    "acknowledge_router_flags=True to apply it anyway. "
                    "The revision_token is still valid."
                ),
                "router_flags": router_flags,
            }

        try:
            write_check = _apply_specs(pcb_path, proposal["trace_specs"], proposal["via_specs"],
                                       expected_copper=proposal.get("expected_copper"))
        except write_layer.WriteLayerError as exc:
            _PENDING_ROUTES[revision_token] = entry  # nothing was written: the token stays valid
            return {"status": "error", "ok": False, "code": exc.code, "reason": f"failed to apply proposed route: {exc}",
                    "mismatches": exc.details.get("mismatches", []), "original_unchanged": True}
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return {"status": "error", "reason": f"failed to apply proposed route: {exc}"}

        from kicad_mcp import kicad_cli

        try:
            kicad_cli.ensure_kicad_cli_on_path()
            drc_report = kicad_cli.run_drc(pcb_path)
            drc_summary = {
                "violation_count": len(drc_report.get("violations", [])),
                "unconnected_count": len(drc_report.get("unconnected_items", [])),
            }
        except Exception as exc:  # noqa: BLE001 - evidence-gated: applying succeeded even if the verify step didn't
            drc_summary = {"status": "incomplete", "reason": f"applied but DRC could not run: {exc}"}

        confirmed = {
            "status": "confirmed",
            "traces_applied": len(proposal["trace_specs"]),
            "vias_applied": len(proposal["via_specs"]),
            "write_check": write_check,
            "drc_after": drc_summary,
            "note": GATE_NOTE,
        }
        if router_flags:
            confirmed["router_flags_acknowledged"] = router_flags
        return confirmed

    def diagnose_placement_congestion(
        pcb_path: str, krt_log_path: str, proximity_mm: float = 15.0, max_suggestions: int = 10
    ) -> dict:
        """Turn a real KiCadRoutingTools route log into specific,
        evidence-backed footprint-move suggestions, instead of leaving a
        human to grep/read the log by hand (as tickets 05/12 both did on
        this project's own openMPPT board -- the manual process this tool
        automates).

        `krt_log_path` is the real streamed log file `run_kicad_routing_tools`
        already writes (the `stdout_log` path in its return dict, or
        `propose_route_with_retry`'s per-attempt `router_log`) -- pass that
        path, not a copy of its text. Every suggestion cites its real
        evidence: which net KRT's search reported as blocking, how many
        times, and the real board distance to the nearest footprint on each
        of the two nets involved. Diagnostic only -- never moves anything
        itself; apply a suggestion via place_footprints and re-verify with
        pcb_check_collisions.
        """
        log_file = Path(krt_log_path)
        if not log_file.is_file():
            return _incomplete(f"log file not found: {krt_log_path}")
        try:
            log_text = log_file.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:  # noqa: BLE001 - evidence-gated verdict
            return _incomplete(f"could not read log file: {exc}")
        return suggest_placement_fixes_for_congestion(
            pcb_path, log_text, proximity_mm=proximity_mm, max_suggestions=max_suggestions
        )

    _READ_ONLY_LOCAL = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False}
    mcp.add_tool(
        diagnose_placement_congestion,
        description=(
            "Diagnose PLACEMENT congestion from a real KiCadRoutingTools route log: parses its "
            "'Blocking obstacles' evidence and correlates blocking nets with real footprint "
            "positions on the board, producing specific, evidence-cited footprint-move "
            "suggestions (which ref, how far, why) -- does NOT modify the board."
        ),
        annotations=dict(_READ_ONLY_LOCAL),
    )
    mcp.add_tool(
        add_traces,
        description=(
            "Add multiple traces to a PCB in one batch call -- never loop this one segment at a time. "
            "Adding traces checks nothing: only a verify_board PASS may be reported as routed or done."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        get_unrouted_nets,
        description=(
            "List the nets kicad_tools' ratsnest still shows as needing routing, as structured data: "
            "a planning aid for what is left to route. It is kicad_tools' own ratsnest, not kicad-cli, "
            "so an empty list proves nothing; only a verify_board PASS may be reported as routed or done."
        ),
        annotations=dict(_READ_ONLY_LOCAL),
    )
    mcp.add_tool(
        propose_route_with_freerouting,
        description=(
            "Run Freerouting (real local subprocess) against a board and propose the result -- "
            "does NOT modify the board. Mandatory human checkpoint: call confirm_route with the "
            "returned revision_token to actually apply it. Autorouting quality is inconsistent, "
            "always review before trusting. A board saved while Freerouting runs is refused "
            "(code board_changed_during_routing, no proposal). Neither the proposal nor confirm_route's "
            "DRC counts are evidence: only a verify_board PASS on the confirmed board may be reported as "
            "routed or done."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    )
    mcp.add_tool(
        confirm_route,
        description=(
            "Apply a previously-proposed autoroute (either backend) by its revision_token through "
            "kicad_tools, then run one kicad-cli DRC on the result and report its counts (drc_after). "
            "That single run is not the acceptance gate: run verify_board on the board, and only its "
            "PASS may be reported as routed or done. A proposal flagged requires_acknowledgement is "
            "refused (token stays valid) unless acknowledge_router_flags=True. Flags: KRT "
            "strict_sizes_violated, router_exit_nonzero:<code>, router_version_mismatch; Freerouting "
            "ses_import_incomplete (KiCad's import skipped items, put copper on no net, ignored polygon "
            "wires, added more copper than the session holds, or a count was unreadable), "
            "arcs_not_applied (the router's arcs are not applied; those connections stay open), "
            "existing_copper_removed."
        ),
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False, "openWorldHint": False},
    )
    mcp.add_tool(
        propose_route_with_retry,
        description=(
            "Diagnose-and-retry autorouting: run the chosen backend (freerouting, default, or "
            "kicad_routing_tools -- ~50x faster, routes .kicad_pcb directly), score against real "
            "kicad-cli DRC against the board's ORIGINAL rules (never the router's own score), "
            "categorize failures, retry with a targeted parameter adjustment (bounded, default 3 "
            "attempts), and propose the best-scoring attempt -- does NOT modify the board. "
            "escalation='board' (default) pins the board's own rules as a hard floor; 'fab' is an "
            "explicit opt-in to relax them and is graded for manufacturability. Call confirm_route "
            "with the returned revision_token to apply it. With freerouting, a board saved during the "
            "run is refused (code board_changed_during_routing, no proposal). The score only ranks "
            "attempts; it is not a verdict. Only a verify_board PASS on the confirmed board may be "
            "reported as routed or done."
        ),
        annotations={"readOnlyHint": True, "destructiveHint": False, "idempotentHint": False, "openWorldHint": True},
    )
    return [
        "add_traces",
        "get_unrouted_nets",
        "propose_route_with_freerouting",
        "confirm_route",
        "propose_route_with_retry",
        "diagnose_placement_congestion",
    ]
