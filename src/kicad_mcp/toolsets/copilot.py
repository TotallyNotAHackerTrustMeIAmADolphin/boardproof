"""Copilot toolset -- work alongside a human who has KiCad open.

ROADMAP Phase 6. The user's framing for this whole mode, verbatim:
*"humans should still be in charge if they want to."*

Autopilot mode operates on files with nobody watching. Copilot mode is the
opposite: a human is in KiCad right now, possibly mid-edit, and we are a
guest in their session. Three consequences run through every tool here:

1. **Never touch the file behind their back.** If the live connection
   isn't there, these tools fail closed with an actionable reason -- they
   do not quietly degrade into autopilot file writes. A live KiCad may
   hold unsaved changes; writing the file underneath it destroys the
   human's work. That refusal is the feature.
2. **The GUI selection is the highest-value signal.** ``copilot_get_selection``
   is how the agent learns what "this component" means. No file-based
   tool can answer that question at all.
3. **Smaller, more granular proposals than autopilot.** Autopilot batches
   aggressively because nobody is watching; here somebody is, so a
   proposal that moves 60 parts at once is bad copilot behaviour even
   though it is good autopilot behaviour. ``copilot_propose_moves``
   flags oversized batches and makes the caller acknowledge them.

Mode-awareness is **not** implemented with ``if mode == "copilot"``
branches in these tools -- it comes from :mod:`kicad_mcp.ipc.adapter`,
which hands back an ``IpcBackend`` or a ``FileBackend`` behind one
protocol. ``copilot_get_board_state``/``copilot_get_schematic_state`` are
the worked example of a tool that is genuinely mode-agnostic: the same
body serves both modes and the result carries ``mode``/``source`` so the
caller always knows which one answered.

State freshness: there is deliberately **no file-hash drift detection**
in this module. In copilot mode the human's own GUI edits *are* the
current state (docs/ARCHITECTURE.md), so the equivalent guard is a live
re-read -- ``copilot_apply_proposal`` re-reads the board and refuses if
anything it planned around has moved since the proposal was made.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from mcp.server.mcpserver import MCPServer

from kicad_mcp.ipc import adapter, connection, discovery

#: Above this many items in one proposal, a live-watching human loses the
#: thread of what is about to happen. Not a hard refusal -- an explicit
#: acknowledgement, so the caller has to mean it.
COPILOT_GRANULARITY_THRESHOLD = 12

#: Guide-recommended output ceiling (docs/ARCHITECTURE.md).
CHARACTER_LIMIT = adapter.CHARACTER_LIMIT

#: Proposals live only for this long; a stale plan against a live design
#: that the human has kept editing is worse than no plan.
PROPOSAL_TTL_SECONDS = 900

_PROPOSALS: dict[str, dict] = {}


def _truncate(result: dict) -> dict:
    """Keep a single tool result inside the character budget, honestly."""
    try:
        encoded = json.dumps(result, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return result
    if len(encoded) <= CHARACTER_LIMIT:
        return result
    trimmed = dict(result)
    for key in ("footprints", "symbols", "nets", "labels", "sheets"):
        section = trimmed.get(key)
        if isinstance(section, dict) and isinstance(section.get("items"), list):
            keep = max(1, len(section["items"]) // 4)
            section = dict(section)
            section["items"] = section["items"][:keep]
            section["count"] = keep
            section["has_more"] = True
            trimmed[key] = section
    trimmed["truncated"] = True
    trimmed["truncation_message"] = (
        f"result exceeded {CHARACTER_LIMIT} characters and was trimmed -- narrow the request "
        "with `include=[...]` for fewer sections, or page through with `limit`/`offset`"
    )
    return trimmed


def _prune_proposals() -> None:
    now = time.time()
    for token in [t for t, p in _PROPOSALS.items() if now - p["created_at"] > PROPOSAL_TTL_SECONDS]:
        _PROPOSALS.pop(token, None)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _copilot_status(socket_url: str | None = None) -> dict:
    """Report whether a live KiCad session is reachable, and if not, exactly why.

    Safe to call first, always: it never connects destructively and never
    raises. When KiCad isn't reachable this distinguishes the three cases
    that need different fixes -- bindings not installed, KiCad not
    running, or KiCad running with the API preference switched off -- and
    tells the human where the preference lives.

    Args:
        socket_url: Override the IPC socket URL (default: the
            KICAD_API_SOCKET environment variable, else the platform
            default socket path).
    """
    conn = connection.get_connection(socket_url)
    status = conn.connect()
    status["mode"] = adapter.MODE_COPILOT if status.get("status") == "connected" else None
    status["copilot_available"] = status.get("status") == "connected"
    status["autopilot_always_available"] = True
    status["socket_env_var"] = discovery.SOCKET_ENV_VAR
    return _truncate(status)


def _copilot_connect(
    socket_url: str | None = None, timeout_ms: int | None = None, force: bool = False
) -> dict:
    """Open (or re-open) the live connection to a running KiCad GUI.

    Args:
        socket_url: IPC socket URL override, e.g.
            ``ipc://<TEMP>\\kicad\\api.sock``.
            Needed when this server runs with a different TEMP directory
            than KiCad -- the usual cause of a failure to find an
            otherwise-running instance.
        timeout_ms: Per-request timeout. KiCad's API is synchronous and
            single-threaded; raise this if the board is very large.
        force: Drop and re-dial an existing connection.
    """
    conn = connection.get_connection(socket_url, timeout_ms=timeout_ms, reset=force)
    return _truncate(conn.connect(force=force))


def _copilot_disconnect() -> dict:
    """Close the live KiCad connection. Idempotent; never affects the human's session."""
    result = connection.get_connection().close()
    connection.reset_connection()
    _PROPOSALS.clear()
    result["discarded_proposals"] = True
    return result


def _copilot_get_board_state(
    mode: str = "auto",
    pcb_path: str | None = None,
    include: list[str] | None = None,
    limit: int = adapter.DEFAULT_LIMIT,
    offset: int = 0,
    socket_url: str | None = None,
) -> dict:
    """Read board state -- from the running KiCad in copilot mode, or from a file in autopilot mode.

    The same result shape either way; ``source`` in the result says which
    answered ("ipc" or "file"), so a live read can never be confused with
    a file read. This tool has no mode branch of its own -- see
    :mod:`kicad_mcp.ipc.adapter`.

    Args:
        mode: "copilot" (live only, fails closed), "autopilot" (file only),
            or "auto" (prefer live, fall back to the file for reads and say
            so).
        pcb_path: Path to a ``.kicad_pcb``. Required for autopilot; used as
            the fallback target in auto mode.
        include: Sections to return -- any of "counts", "footprints",
            "nets", "stackup", or "all". Defaults to counts + footprints.
        limit: Max rows per list section.
        offset: Row offset for paging.
        socket_url: IPC socket override (copilot/auto only).
    """
    choice = adapter.resolve_backend(mode, pcb_path=pcb_path, socket_url=socket_url)
    if not choice.ok:
        return _truncate(choice.error or {"status": "error", "reason": "no backend available"})
    return _truncate(
        choice.annotate(choice.backend.board_state(include=include, limit=limit, offset=offset))
    )


def _copilot_get_schematic_state(
    mode: str = "auto",
    sch_path: str | None = None,
    include: list[str] | None = None,
    limit: int = adapter.DEFAULT_LIMIT,
    offset: int = 0,
    socket_url: str | None = None,
) -> dict:
    """Read schematic state -- live from KiCad in copilot mode, or from a file in autopilot mode.

    Note for anyone working from the archived research notes' original survey: KiCad 9's
    IPC API was PCB-editor-only, but **KiCad 10's covers the schematic
    editor too**, so this tool really is live in copilot mode rather than
    silently file-only.

    Args:
        mode: "copilot", "autopilot", or "auto" (see copilot_get_board_state).
        sch_path: Path to a ``.kicad_sch``. Required for autopilot mode.
        include: Any of "counts", "symbols", "labels", "sheets", "all".
        limit: Max rows per list section.
        offset: Row offset for paging.
        socket_url: IPC socket override (copilot/auto only).
    """
    choice = adapter.resolve_backend(mode, sch_path=sch_path, socket_url=socket_url)
    if not choice.ok:
        return _truncate(choice.error or {"status": "error", "reason": "no backend available"})
    return _truncate(
        choice.annotate(choice.backend.schematic_state(include=include, limit=limit, offset=offset))
    )


def _copilot_get_selection(document: str = "both", socket_url: str | None = None) -> dict:
    """Read what the human currently has selected in KiCad -- the key copilot signal.

    This is how "move this one 2mm left" or "what is this part?" becomes
    actionable: the selection resolves the pronoun. Copilot-only by
    nature; a file on disk has no selection, and this tool says so
    explicitly rather than returning an empty list that would read as
    "nothing selected".

    Args:
        document: "pcb", "schematic", or "both" (default).
        socket_url: IPC socket override.
    """
    if document not in ("pcb", "schematic", "both"):
        return {
            "status": "error",
            "reason": f"document must be 'pcb', 'schematic' or 'both', got {document!r}",
        }
    choice = adapter.resolve_backend(adapter.MODE_COPILOT, socket_url=socket_url)
    if not choice.ok:
        return _truncate(choice.error or {"status": "error", "reason": "no live KiCad connection"})
    if adapter.CAP_SELECTION not in choice.backend.capabilities:  # pragma: no cover - guard
        return {
            "status": "unsupported",
            "reason": "the active backend cannot report a GUI selection",
        }
    return _truncate(choice.annotate(choice.backend.selection(document=document)))


def _copilot_highlight(
    references: list[str], document: str = "pcb", socket_url: str | None = None
) -> dict:
    """Select components in the human's KiCad window so they can see what we mean.

    The conversational inverse of ``copilot_get_selection``: instead of
    asking what they mean, show them. Batch by design -- pass every
    reference at once, e.g. all decoupling caps that failed a check.
    Changes only the GUI selection, never the design itself.

    Args:
        references: Reference designators to select, e.g. ["C1", "C2", "U1"].
        document: "pcb" (default) or "schematic".
        socket_url: IPC socket override.
    """
    if document not in ("pcb", "schematic"):
        return {"status": "error", "reason": f"document must be 'pcb' or 'schematic', got {document!r}"}
    if not references:
        return {"status": "error", "reason": "references must not be empty"}
    choice = adapter.resolve_backend(adapter.MODE_COPILOT, socket_url=socket_url, for_mutation=True)
    if not choice.ok:
        return _truncate(choice.error or {"status": "error", "reason": "no live KiCad connection"})
    return _truncate(choice.annotate(choice.backend.set_selection(references, document=document)))


def _copilot_propose_moves(
    moves: list[dict],
    message: str = "kicad-mcp copilot move",
    use_selection: bool = False,
    socket_url: str | None = None,
) -> dict:
    """Plan a batch of footprint moves against the LIVE board and return a revision token.

    Nothing changes yet. The plan records where each footprint is now and
    where it would go; ``copilot_apply_proposal`` re-reads the live board
    and refuses if the human moved anything in the meantime. That live
    re-read is copilot mode's equivalent of autopilot's file-drift
    hashing -- the running instance is the source of truth, so there is
    nothing on disk to hash.

    Args:
        moves: List of ``{"reference": "C1", "x_mm": 12.5, "y_mm": 30.0,
            "rotation_degrees": 90}``. ``x_mm``/``y_mm``/``rotation_degrees``
            are each optional; omitted ones are left as they are.
        message: The undo-history entry the human will see in KiCad.
        use_selection: If true, restrict the plan to footprints the human
            currently has selected, and report anything in ``moves`` that
            isn't selected as skipped. Use this when the human said "these".
        socket_url: IPC socket override.
    """
    _prune_proposals()
    if not moves:
        return {"status": "error", "reason": "moves must not be empty"}

    normalized: list[dict] = []
    for index, move in enumerate(moves):
        if not isinstance(move, dict) or not move.get("reference"):
            return {
                "status": "error",
                "reason": f"moves[{index}] must be a dict with a 'reference' key, got {move!r}",
            }
        if all(move.get(k) is None for k in ("x_mm", "y_mm", "rotation_degrees")):
            return {
                "status": "error",
                "reason": (
                    f"moves[{index}] ({move['reference']}) changes nothing -- give at least one "
                    "of x_mm, y_mm, rotation_degrees"
                ),
            }
        if (move.get("x_mm") is None) != (move.get("y_mm") is None):
            return {
                "status": "error",
                "reason": (
                    f"moves[{index}] ({move['reference']}) gives only one of x_mm/y_mm -- a "
                    "position needs both"
                ),
            }
        normalized.append(
            {
                "reference": str(move["reference"]),
                "x_mm": move.get("x_mm"),
                "y_mm": move.get("y_mm"),
                "rotation_degrees": move.get("rotation_degrees"),
            }
        )

    choice = adapter.resolve_backend(adapter.MODE_COPILOT, socket_url=socket_url, for_mutation=True)
    if not choice.ok:
        return _truncate(choice.error or {"status": "error", "reason": "no live KiCad connection"})
    backend = choice.backend

    before = backend.board_state(include=["footprints"], limit=None, offset=0)
    if before.get("status") != "ok":
        return _truncate(choice.annotate(before))
    current = {row["reference"]: row for row in before["footprints"]["items"] if row.get("reference")}

    skipped: list[dict] = []
    if use_selection:
        selection = backend.selection(document="pcb")
        selected_refs = {
            item.get("reference")
            for item in selection.get("documents", {}).get("pcb", {}).get("items", [])
            if item.get("reference")
        }
        if not selected_refs:
            return {
                "status": "error",
                "reason": "use_selection=true but nothing is selected in KiCad's PCB editor",
                "remedy": "ask the human to select the footprints they mean, then retry",
            }
        kept = [m for m in normalized if m["reference"] in selected_refs]
        skipped = [
            {"reference": m["reference"], "reason": "not in the human's current selection"}
            for m in normalized
            if m["reference"] not in selected_refs
        ]
        normalized = kept
        if not normalized:
            return {
                "status": "error",
                "reason": "none of the requested references are in the human's current selection",
                "selected_references": sorted(selected_refs),
            }

    unknown = [m["reference"] for m in normalized if m["reference"] not in current]
    if unknown:
        return {
            "status": "error",
            "reason": f"no footprint on the live board with reference(s): {sorted(unknown)}",
            "known_references": sorted(current),
        }

    changes = []
    for move in normalized:
        now = current[move["reference"]]
        changes.append(
            {
                "reference": move["reference"],
                "from": {
                    "x_mm": now.get("x_mm"),
                    "y_mm": now.get("y_mm"),
                    "rotation_degrees": now.get("rotation_degrees"),
                },
                "to": {
                    "x_mm": move["x_mm"] if move["x_mm"] is not None else now.get("x_mm"),
                    "y_mm": move["y_mm"] if move["y_mm"] is not None else now.get("y_mm"),
                    "rotation_degrees": (
                        move["rotation_degrees"]
                        if move["rotation_degrees"] is not None
                        else now.get("rotation_degrees")
                    ),
                },
                "locked": now.get("locked"),
            }
        )

    locked = [c["reference"] for c in changes if c.get("locked")]
    revision_token = f"copilot-move-{uuid.uuid4().hex[:12]}"
    _PROPOSALS[revision_token] = {
        "created_at": time.time(),
        "kind": "footprint_moves",
        "moves": normalized,
        "baseline": {
            c["reference"]: (c["from"]["x_mm"], c["from"]["y_mm"], c["from"]["rotation_degrees"])
            for c in changes
        },
        "message": message,
        "socket_url": socket_url,
    }

    result: dict[str, Any] = {
        "status": "proposed",
        "mode": adapter.MODE_COPILOT,
        "source": "ipc",
        "revision_token": revision_token,
        "expires_in_seconds": PROPOSAL_TTL_SECONDS,
        "change_count": len(changes),
        "changes": changes,
        "skipped": skipped,
        "undo_step_message": message,
        "next_step": (
            "nothing has changed yet. Show these changes to the human, then call "
            f"copilot_apply_proposal(revision_token='{revision_token}') to apply them as a "
            "single undo step in their KiCad session."
        ),
    }
    if locked:
        result["warnings"] = [
            f"locked on the live board and will be skipped by KiCad: {sorted(locked)}"
        ]
    if len(changes) > COPILOT_GRANULARITY_THRESHOLD:
        result["granularity_warning"] = (
            f"{len(changes)} changes in one step is a lot for someone watching live "
            f"(copilot guidance: keep batches under {COPILOT_GRANULARITY_THRESHOLD}). Applying "
            "this requires acknowledge_large_batch=true; consider splitting it into a few "
            "reviewable steps instead. Autopilot mode is where large batches belong."
        )
        result["requires_acknowledgement"] = True
    return _truncate(result)


def _copilot_apply_proposal(
    revision_token: str, acknowledge_large_batch: bool = False
) -> dict:
    """Apply a proposal from ``copilot_propose_moves``, if the live board still matches it.

    Re-reads the running KiCad first and refuses if any footprint in the
    plan has moved since the proposal was made -- the human is editing
    too, and their edits win. On success the whole batch lands as one
    undo step in their session, unsaved, so they keep the final say.

    Args:
        revision_token: The exact token returned by copilot_propose_moves.
        acknowledge_large_batch: Required when the proposal carried a
            granularity warning.
    """
    _prune_proposals()
    proposal = _PROPOSALS.get(revision_token)
    if proposal is None:
        return {
            "status": "error",
            "reason": (
                f"unknown or expired revision token {revision_token!r} (proposals expire after "
                f"{PROPOSAL_TTL_SECONDS}s, and are discarded on disconnect)"
            ),
            "remedy": "call copilot_propose_moves again to build a fresh plan",
        }

    choice = adapter.resolve_backend(
        adapter.MODE_COPILOT, socket_url=proposal.get("socket_url"), for_mutation=True
    )
    if not choice.ok:
        return _truncate(choice.error or {"status": "error", "reason": "no live KiCad connection"})
    backend = choice.backend

    if len(proposal["moves"]) > COPILOT_GRANULARITY_THRESHOLD and not acknowledge_large_batch:
        return {
            "status": "needs_acknowledgement",
            "reason": (
                f"this proposal changes {len(proposal['moves'])} footprints at once, above the "
                f"copilot granularity threshold of {COPILOT_GRANULARITY_THRESHOLD}"
            ),
            "remedy": (
                "confirm with the human, then retry with acknowledge_large_batch=true -- or "
                "propose a few smaller steps they can follow"
            ),
            "revision_token": revision_token,
        }

    now_state = backend.board_state(include=["footprints"], limit=None, offset=0)
    if now_state.get("status") != "ok":
        return _truncate(choice.annotate(now_state))
    current = {
        row["reference"]: (row.get("x_mm"), row.get("y_mm"), row.get("rotation_degrees"))
        for row in now_state["footprints"]["items"]
        if row.get("reference")
    }

    stale = []
    for reference, baseline in proposal["baseline"].items():
        live = current.get(reference)
        if live is None:
            stale.append({"reference": reference, "reason": "no longer on the board"})
        elif not _close(live, baseline):
            stale.append(
                {
                    "reference": reference,
                    "reason": "moved in KiCad since the proposal was made",
                    "expected": {"x_mm": baseline[0], "y_mm": baseline[1], "rotation_degrees": baseline[2]},
                    "actual": {"x_mm": live[0], "y_mm": live[1], "rotation_degrees": live[2]},
                }
            )
    if stale:
        _PROPOSALS.pop(revision_token, None)
        return {
            "status": "stale",
            "reason": (
                "the live board changed since this proposal was made -- the human's own edits "
                "take precedence, so nothing was applied"
            ),
            "stale_items": stale,
            "remedy": "re-read the board and call copilot_propose_moves again",
        }

    result = backend.apply_footprint_moves(proposal["moves"], message=proposal["message"])
    if result.get("status") == "applied":
        _PROPOSALS.pop(revision_token, None)
        result["revision_token"] = revision_token
    return _truncate(choice.annotate(result))


def _close(a: tuple, b: tuple, tolerance: float = 1e-4) -> bool:
    for left, right in zip(a, b):
        if left is None or right is None:
            if left is not right:
                return False
            continue
        if abs(float(left) - float(right)) > tolerance:
            return False
    return True


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def build(mcp: MCPServer) -> list[str]:
    """Register this toolset's tools onto `mcp`. Returns the registered tool names."""
    read_only = {
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        # Reaching a separate running application over an IPC socket is an
        # external entity as far as this hint is concerned, even though it
        # is local -- the response depends on a process we don't control.
        "openWorldHint": True,
    }
    session = {
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
    mutating = {
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    }

    specs: list[tuple[Any, str, str, dict]] = [
        (
            _copilot_status,
            "copilot_status",
            "Check whether a live KiCad GUI session is reachable, and if not, exactly why and how to fix it.",
            read_only,
        ),
        (
            _copilot_connect,
            "copilot_connect",
            "Open or re-open the live IPC connection to a running KiCad GUI.",
            session,
        ),
        (
            _copilot_disconnect,
            "copilot_disconnect",
            "Close the live KiCad connection and discard any pending copilot proposals.",
            session,
        ),
        (
            _copilot_get_board_state,
            "copilot_get_board_state",
            "Read board state live from a running KiCad (copilot) or from a .kicad_pcb file (autopilot) -- same shape either way.",
            read_only,
        ),
        (
            _copilot_get_schematic_state,
            "copilot_get_schematic_state",
            "Read schematic state live from a running KiCad (copilot) or from a .kicad_sch file (autopilot) -- same shape either way.",
            read_only,
        ),
        (
            _copilot_get_selection,
            "copilot_get_selection",
            "Read what the human currently has selected in KiCad -- resolves 'this component' to real items.",
            read_only,
        ),
        (
            _copilot_highlight,
            "copilot_highlight",
            "Select a batch of components in the human's KiCad window to show them what we mean (GUI selection only, design untouched).",
            session,
        ),
        (
            _copilot_propose_moves,
            "copilot_propose_moves",
            "Plan footprint moves against the live board and return a revision token -- changes nothing until confirmed.",
            read_only,
        ),
        (
            _copilot_apply_proposal,
            "copilot_apply_proposal",
            "Apply a copilot proposal to the running KiCad session as one undo step, refusing if the human has since moved anything.",
            mutating,
        ),
    ]
    for fn, name, description, annotations in specs:
        mcp.add_tool(fn, name=name, description=description, annotations=dict(annotations))
    return [name for _, name, _, _ in specs]
