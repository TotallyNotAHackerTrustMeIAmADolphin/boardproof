"""The copilot/autopilot **mode adapter seam**.

docs/ARCHITECTURE.md, "Two operating modes": *"Both modes share the same tool
implementations wherever possible ... The IPC-vs-file distinction lives in
an adapter layer, not scattered through every tool."* This module is that
adapter layer, and it is the load-bearing piece of Phase 6 -- more so than
any individual tool.

The seam in one sentence: **a tool asks for a backend and reads normalized
state from it; it never asks which mode it is in.**

    backend = resolve_backend(mode="auto", pcb_path=...)   # returns a
    state = backend.board_state()                          # BackendChoice
    # -> {"status": "ok", "mode": "copilot", "source": "ipc", ...}
    # -> identical key shape whether it came from a live GUI or a file

Two backends implement one protocol:

* :class:`IpcBackend` -- reads the *running* KiCad GUI over the IPC API
  (``kipy``). This is copilot mode. There is deliberately **no file-drift
  detection** on this path: the human's own GUI edits simply *are* the
  current state, so there is nothing to diff against (docs/ARCHITECTURE.md's
  drift-hashing requirement applies to autopilot only).
* :class:`FileBackend` -- reads ``.kicad_pcb``/``.kicad_sch`` through
  ``kicad_tools``. This is autopilot mode, and it is exactly what the ten
  existing toolsets already do inline today.

Capabilities, not mode strings
------------------------------
Tools that need something mode-specific (the GUI selection, say) ask
``CAP_SELECTION in backend.capabilities`` rather than testing
``mode == "copilot"``. A backend that cannot do something returns an
honest ``{"status": "unsupported", "reason": ...}`` rather than raising or
pretending. This is what keeps ``if mode == ...`` out of tool bodies.

Fallback policy
---------------
``mode="auto"`` prefers the live instance and may fall back to the file
backend for **reads** when the transport is unreachable -- but it always
reports ``source`` and ``fell_back_reason``, so a caller can never mistake
a file read for a live one. ``for_mutation=True`` disables fallback
entirely: a write must be either provably live or explicitly file-based,
because silently editing a file behind a live GUI's back is the data-loss
scenario copilot mode exists to prevent (Konnect's ``attempt_ipc_write``
reaches the same conclusion; docs/ARCHITECTURE.md records it).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Protocol, Sequence, runtime_checkable

from kicad_mcp.ipc import connection as ipc_connection
from kicad_mcp.ipc.connection import IpcConnection, IpcUnavailableError

MODE_COPILOT = "copilot"
MODE_AUTOPILOT = "autopilot"
MODE_AUTO = "auto"

CAP_BOARD_READ = "board_read"
CAP_SCHEMATIC_READ = "schematic_read"
CAP_SELECTION = "selection"
CAP_LIVE_MUTATE = "live_mutate"

NM_PER_MM = 1_000_000.0

#: Guide-recommended output ceiling (docs/ARCHITECTURE.md, "Adopted from the
#: mcp-builder skill guide"). Lists are paginated before this bites, but it
#: remains the backstop for pathologically large boards.
CHARACTER_LIMIT = 25_000

DEFAULT_LIMIT = 200


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _nm_to_mm(value: int | float | None) -> float | None:
    return None if value is None else round(value / NM_PER_MM, 6)


def _layer_name(layer_value: Any) -> str:
    """Turn a ``BoardLayer`` enum value into KiCad's own layer name."""
    try:
        from kipy.proto.board.board_types_pb2 import BoardLayer

        raw = BoardLayer.Name(layer_value)
    except Exception:  # noqa: BLE001 - naming is cosmetic, never fatal
        return str(layer_value)
    if raw.startswith("BL_"):
        raw = raw[3:]
    return raw.replace("_", ".", 1)


def _kiid(value: Any) -> str | None:
    if value is None:
        return None
    inner = getattr(value, "value", None)
    return str(inner) if inner else (str(value).strip() or None)


def _field_text(field: Any) -> str | None:
    text = getattr(field, "text", None)
    value = getattr(text, "value", None)
    if value is None:
        value = getattr(field, "value", None)
    return str(value) if value is not None else None


def paginate(items: Sequence[Any], limit: int | None, offset: int) -> dict:
    """House-convention pagination envelope.

    docs/ARCHITECTURE.md: list-returning tools return
    ``{total_count, count, items, has_more, next_offset}`` and never
    silently load everything.
    """
    total = len(items)
    offset = max(0, offset)
    effective = DEFAULT_LIMIT if limit is None else max(0, limit)
    window = list(items[offset : offset + effective])
    next_offset = offset + len(window)
    return {
        "total_count": total,
        "count": len(window),
        "items": window,
        "has_more": next_offset < total,
        "next_offset": next_offset if next_offset < total else None,
    }


def _unsupported(reason: str, *, remedy: str | None = None) -> dict:
    result = {"status": "unsupported", "reason": reason}
    if remedy:
        result["remedy"] = remedy
    return result


# ---------------------------------------------------------------------------
# The protocol every backend implements
# ---------------------------------------------------------------------------


@runtime_checkable
class DesignBackend(Protocol):
    """One uniform view of a KiCad design, live or on disk.

    Every method returns a JSON-shaped dict with a ``status`` key and never
    raises -- the evidence-gated-return contract this project applies to
    tools applies one layer down, here, so a tool can pass a backend result
    straight through without inventing error wording.
    """

    mode: str
    source: str
    capabilities: frozenset[str]

    def describe(self) -> dict: ...

    def board_state(
        self, *, include: Sequence[str] | None = ..., limit: int | None = ..., offset: int = ...
    ) -> dict: ...

    def schematic_state(
        self, *, include: Sequence[str] | None = ..., limit: int | None = ..., offset: int = ...
    ) -> dict: ...

    def selection(self, *, document: str = ...) -> dict: ...


# ---------------------------------------------------------------------------
# Copilot: live IPC backend
# ---------------------------------------------------------------------------


BOARD_SECTIONS = ("counts", "footprints", "nets", "stackup")
SCHEMATIC_SECTIONS = ("counts", "symbols", "labels", "sheets")


class IpcBackend:
    """Reads a **running** KiCad instance. Copilot mode."""

    mode = MODE_COPILOT
    source = "ipc"
    capabilities = frozenset({CAP_BOARD_READ, CAP_SCHEMATIC_READ, CAP_SELECTION, CAP_LIVE_MUTATE})

    def __init__(self, conn: IpcConnection) -> None:
        self._conn = conn

    # -- description ---------------------------------------------------

    def describe(self) -> dict:
        return {
            "status": "ok",
            "mode": self.mode,
            "source": self.source,
            "capabilities": sorted(self.capabilities),
            "drift_detection": "not applicable",
            "drift_detection_reason": (
                "state is read live from the running KiCad instance, so the human's own GUI "
                "edits are simply the current state -- there is no on-disk snapshot to diff "
                "against (docs/ARCHITECTURE.md: file-drift hashing is an autopilot-mode concern)"
            ),
            "connection": self._conn.status(),
        }

    # -- board ---------------------------------------------------------

    def board_state(
        self,
        *,
        include: Sequence[str] | None = None,
        limit: int | None = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict:
        sections = _normalize_sections(include, BOARD_SECTIONS, default=("counts", "footprints"))
        try:
            board = self._conn.board()
        except IpcUnavailableError as exc:
            return exc.to_dict()

        result: dict[str, Any] = {
            "status": "ok",
            "mode": self.mode,
            "source": self.source,
            "sections": sorted(sections),
            "board": {"name": getattr(board, "name", None), "path": None},
        }
        try:
            footprints = list(board.get_footprints())
            if "counts" in sections:
                result["counts"] = {
                    "footprints": len(footprints),
                    "tracks": len(list(board.get_tracks())),
                    "vias": len(list(board.get_vias())),
                    "zones": len(list(board.get_zones())),
                    "nets": len(list(board.get_nets())),
                }
            if "footprints" in sections:
                rows = [_ipc_footprint_row(fp) for fp in footprints]
                rows.sort(key=lambda r: (r.get("reference") or "~", r.get("uuid") or ""))
                result["footprints"] = paginate(rows, limit, offset)
            if "nets" in sections:
                nets = [
                    {"name": getattr(n, "name", None), "net_code": getattr(n, "code", None)}
                    for n in board.get_nets()
                ]
                nets.sort(key=lambda r: str(r.get("name") or ""))
                result["nets"] = paginate(nets, limit, offset)
            if "stackup" in sections:
                result["stackup"] = _ipc_stackup(board)
        except IpcUnavailableError as exc:
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001 - evidence-gated return
            return {
                "status": "partial",
                "mode": self.mode,
                "source": self.source,
                "reason": f"live board read failed part-way through: {exc}",
                "partial_result": result,
            }
        return result

    # -- schematic -----------------------------------------------------

    def schematic_state(
        self,
        *,
        include: Sequence[str] | None = None,
        limit: int | None = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict:
        sections = _normalize_sections(include, SCHEMATIC_SECTIONS, default=("counts", "symbols"))
        try:
            schematic = self._conn.schematic()
        except IpcUnavailableError as exc:
            return exc.to_dict()

        result: dict[str, Any] = {
            "status": "ok",
            "mode": self.mode,
            "source": self.source,
            "sections": sorted(sections),
            "schematic": {"name": getattr(schematic, "name", None), "path": None},
        }
        try:
            symbols = list(schematic.get_symbols())
            labels = list(schematic.get_labels()) if "labels" in sections or "counts" in sections else []
            sheets = (
                list(schematic.get_sheet_symbols())
                if "sheets" in sections or "counts" in sections
                else []
            )
            if "counts" in sections:
                result["counts"] = {
                    "symbols": len(symbols),
                    "labels": len(labels),
                    "sheet_symbols": len(sheets),
                }
            if "symbols" in sections:
                rows = [_ipc_symbol_row(sym) for sym in symbols]
                rows.sort(key=lambda r: (r.get("reference") or "~", r.get("uuid") or ""))
                result["symbols"] = paginate(rows, limit, offset)
            if "labels" in sections:
                rows = [
                    {
                        "text": _field_text(lbl) or getattr(lbl, "text", None),
                        "uuid": _kiid(getattr(lbl, "id", None)),
                        **_ipc_position(getattr(lbl, "position", None)),
                    }
                    for lbl in labels
                ]
                result["labels"] = paginate(rows, limit, offset)
            if "sheets" in sections:
                rows = [
                    {
                        "uuid": _kiid(getattr(sheet, "id", None)),
                        "name": _field_text(getattr(sheet, "name_field", None)),
                        "filename": _field_text(getattr(sheet, "filename_field", None)),
                    }
                    for sheet in sheets
                ]
                result["sheets"] = paginate(rows, limit, offset)
        except IpcUnavailableError as exc:
            return exc.to_dict()
        except Exception as exc:  # noqa: BLE001 - evidence-gated return
            return {
                "status": "partial",
                "mode": self.mode,
                "source": self.source,
                "reason": f"live schematic read failed part-way through: {exc}",
                "partial_result": result,
            }
        return result

    # -- selection (the copilot-only signal) ---------------------------

    def selection(self, *, document: str = "both") -> dict:
        """What the human currently has selected in the GUI.

        This is the highest-value copilot signal there is: it is how the
        agent knows what "this component" means when the human says it.
        """
        wanted = ("pcb", "schematic") if document == "both" else (document,)
        result: dict[str, Any] = {
            "status": "ok",
            "mode": self.mode,
            "source": self.source,
            "documents": {},
        }
        any_ok = False
        for kind in wanted:
            try:
                doc = self._conn.board() if kind == "pcb" else self._conn.schematic()
                items = [_ipc_selected_item(i, kind) for i in doc.get_selection()]
                result["documents"][kind] = {"count": len(items), "items": items}
                any_ok = True
            except IpcUnavailableError as exc:
                result["documents"][kind] = {
                    "status": "unavailable",
                    "reason": exc.reason,
                    "reason_code": exc.reason_code,
                }
            except Exception as exc:  # noqa: BLE001 - evidence-gated return
                result["documents"][kind] = {"status": "unavailable", "reason": str(exc)}
        if not any_ok:
            result["status"] = "incomplete"
            result["reason"] = (
                "no open document could be queried for its selection; see per-document reasons"
            )
        else:
            total = sum(
                d.get("count", 0) for d in result["documents"].values() if isinstance(d, dict)
            )
            result["selected_item_count"] = total
            if total == 0:
                result["note"] = (
                    "nothing is selected in KiCad right now -- ask the human to select the "
                    "item(s) they mean, or address items by reference designator instead"
                )
        return result

    # -- mutation ------------------------------------------------------

    def apply_footprint_moves(self, moves: Sequence[dict], *, message: str) -> dict:
        """Apply a batch of absolute footprint placements as one undo step.

        Wrapped in a single KiCad commit so the human gets exactly one entry
        in their undo history for the whole batch -- important when someone
        is watching live and may want to take it straight back out.
        """
        try:
            board = self._conn.board()
        except IpcUnavailableError as exc:
            return exc.to_dict()

        from kipy.geometry import Angle, Vector2

        by_reference = {}
        for fp in board.get_footprints():
            ref = _field_text(getattr(fp, "reference_field", None))
            if ref:
                by_reference[ref] = fp

        missing = [m["reference"] for m in moves if m["reference"] not in by_reference]
        if missing:
            return {
                "status": "error",
                "reason": f"no footprint on the live board with reference(s): {sorted(missing)}",
                "known_references": sorted(by_reference),
            }

        commit = None
        try:
            commit = board.begin_commit()
            changed = []
            for move in moves:
                fp = by_reference[move["reference"]]
                if move.get("x_mm") is not None and move.get("y_mm") is not None:
                    fp.position = Vector2.from_xy_mm(float(move["x_mm"]), float(move["y_mm"]))
                if move.get("rotation_degrees") is not None:
                    fp.orientation = Angle.from_degrees(float(move["rotation_degrees"]))
                changed.append(fp)
            board.update_items(changed)
            board.push_commit(commit, message)
        except Exception as exc:  # noqa: BLE001 - evidence-gated return
            if commit is not None:
                try:
                    board.drop_commit(commit)
                except Exception:  # noqa: BLE001 - best-effort rollback
                    pass
            return {
                "status": "error",
                "reason": f"KiCad rejected the live edit and it was rolled back: {exc}",
                "applied": False,
            }
        return {
            "status": "applied",
            "mode": self.mode,
            "source": self.source,
            "applied_count": len(moves),
            "undo_step_message": message,
            "note": (
                "applied as a single KiCad commit -- the human can undo the whole batch with "
                "one Ctrl+Z. Changes are in KiCad's editor, not yet saved to disk; the human "
                "decides when to save."
            ),
        }

    def set_selection(self, references: Iterable[str], *, document: str = "pcb") -> dict:
        """Point the human's eyes at specific components by selecting them."""
        refs = list(references)
        try:
            doc = self._conn.board() if document == "pcb" else self._conn.schematic()
        except IpcUnavailableError as exc:
            return exc.to_dict()
        try:
            if document == "pcb":
                items = {
                    _field_text(getattr(fp, "reference_field", None)): fp
                    for fp in doc.get_footprints()
                }
            else:
                items = {
                    _field_text(getattr(sym, "reference_field", None)): sym
                    for sym in doc.get_symbols()
                }
            matched = [items[r] for r in refs if r in items]
            missing = [r for r in refs if r not in items]
            doc.clear_selection()
            if matched:
                doc.add_to_selection(matched)
        except Exception as exc:  # noqa: BLE001 - evidence-gated return
            return {"status": "error", "reason": f"could not change the GUI selection: {exc}"}
        return {
            "status": "ok",
            "document": document,
            "selected_count": len(matched),
            "selected": [r for r in refs if r not in missing],
            "not_found": missing,
        }


def _ipc_position(position: Any) -> dict:
    if position is None:
        return {"x_mm": None, "y_mm": None}
    return {
        "x_mm": _nm_to_mm(getattr(position, "x", None)),
        "y_mm": _nm_to_mm(getattr(position, "y", None)),
    }


def _ipc_footprint_row(fp: Any) -> dict:
    definition = getattr(fp, "definition", None)
    fp_id = getattr(definition, "id", None)
    # LibraryIdentifier's own __str__ already renders "nickname:entry" (and
    # correctly handles a blank nickname for a board-embedded/orphaned
    # footprint, e.g. ":C_0402_1005Metric") -- confirmed against a real
    # live board rather than guessing at the wrapper's field names.
    return {
        "reference": _field_text(getattr(fp, "reference_field", None)),
        "value": _field_text(getattr(fp, "value_field", None)),
        **_ipc_position(getattr(fp, "position", None)),
        "rotation_degrees": getattr(getattr(fp, "orientation", None), "degrees", None),
        "layer": _layer_name(getattr(fp, "layer", None)),
        "locked": getattr(fp, "locked", None),
        "uuid": _kiid(getattr(fp, "id", None)),
        "library_id": str(fp_id) if fp_id is not None else None,
    }


def _ipc_symbol_row(sym: Any) -> dict:
    return {
        "reference": _field_text(getattr(sym, "reference_field", None)),
        "value": _field_text(getattr(sym, "value_field", None)),
        "footprint": _field_text(getattr(sym, "footprint_field", None)),
        **_ipc_position(getattr(sym, "position", None)),
        "uuid": _kiid(getattr(sym, "id", None)),
    }


def _ipc_selected_item(item: Any, document: str) -> dict:
    row: dict[str, Any] = {
        "item_type": type(item).__name__,
        "document": document,
        "uuid": _kiid(getattr(item, "id", None)),
    }
    reference = _field_text(getattr(item, "reference_field", None))
    if reference:
        row["reference"] = reference
    value = _field_text(getattr(item, "value_field", None))
    if value:
        row["value"] = value
    position = getattr(item, "position", None)
    if position is not None:
        row.update(_ipc_position(position))
    layer = getattr(item, "layer", None)
    if layer is not None:
        row["layer"] = _layer_name(layer)
    net = getattr(item, "net", None)
    if net is not None:
        row["net"] = getattr(net, "name", None) or str(net)
    return row


def _ipc_stackup(board: Any) -> dict:
    try:
        return {
            "copper_layer_count": board.get_copper_layer_count(),
            "enabled_layers": [_layer_name(v) for v in board.get_enabled_layers()],
        }
    except Exception as exc:  # noqa: BLE001 - advisory section
        return {"status": "unavailable", "reason": str(exc)}


# ---------------------------------------------------------------------------
# Autopilot: file backend
# ---------------------------------------------------------------------------


class FileBackend:
    """Reads ``.kicad_pcb``/``.kicad_sch`` from disk via ``kicad_tools``.

    Deliberately thin: it exists so a tool written against the seam works
    unchanged in autopilot mode, and so the "same shape from both sides"
    claim is testable without a running KiCad. The existing ten toolsets
    still call ``kicad_tools`` directly -- see this module's docstring and
    the Phase 6 report for the (small, mechanical) retrofit that would
    route them through here.
    """

    mode = MODE_AUTOPILOT
    source = "file"
    capabilities = frozenset({CAP_BOARD_READ, CAP_SCHEMATIC_READ})

    def __init__(self, pcb_path: str | Path | None = None, sch_path: str | Path | None = None) -> None:
        self.pcb_path = Path(pcb_path) if pcb_path else None
        self.sch_path = Path(sch_path) if sch_path else None

    def describe(self) -> dict:
        return {
            "status": "ok",
            "mode": self.mode,
            "source": self.source,
            "capabilities": sorted(self.capabilities),
            "pcb_path": str(self.pcb_path) if self.pcb_path else None,
            "sch_path": str(self.sch_path) if self.sch_path else None,
            "drift_detection": "required (not yet implemented)",
            "drift_detection_reason": (
                "on-disk files can change underneath us between calls -- docs/ARCHITECTURE.md's "
                "hash/diff-before-mutate requirement applies to this backend only "
                "(archived backlog P1-19)"
            ),
        }

    def board_state(
        self,
        *,
        include: Sequence[str] | None = None,
        limit: int | None = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict:
        sections = _normalize_sections(include, BOARD_SECTIONS, default=("counts", "footprints"))
        if self.pcb_path is None:
            return _unsupported(
                "no pcb_path was given, and the file backend has no live instance to ask",
                remedy="pass pcb_path, or connect to a running KiCad for copilot mode",
            )
        if not self.pcb_path.is_file():
            return {"status": "error", "reason": f"file not found: {self.pcb_path}"}
        try:
            from kicad_tools.schema import PCB

            board = PCB.load(self.pcb_path)
        except Exception as exc:  # noqa: BLE001 - evidence-gated return
            return {"status": "error", "reason": f"could not read {self.pcb_path}: {exc}"}

        result: dict[str, Any] = {
            "status": "ok",
            "mode": self.mode,
            "source": self.source,
            "sections": sorted(sections),
            "board": {"name": self.pcb_path.stem, "path": str(self.pcb_path)},
        }
        try:
            if "counts" in sections:
                result["counts"] = {
                    "footprints": board.footprint_count,
                    "tracks": board.segment_count,
                    "vias": board.via_count,
                    "zones": board.zone_count,
                    "nets": board.net_count,
                }
            if "footprints" in sections:
                rows = [_file_footprint_row(fp) for fp in board.footprints]
                rows.sort(key=lambda r: (r.get("reference") or "~", r.get("uuid") or ""))
                result["footprints"] = paginate(rows, limit, offset)
            if "nets" in sections:
                nets = [
                    {
                        "name": getattr(n, "name", None),
                        "net_code": getattr(n, "number", getattr(n, "code", None)),
                    }
                    for n in board.nets
                ]
                nets.sort(key=lambda r: str(r.get("name") or ""))
                result["nets"] = paginate(nets, limit, offset)
            if "stackup" in sections:
                result["stackup"] = {
                    "copper_layer_count": len(list(board.copper_layers)),
                    "enabled_layers": [str(layer) for layer in board.layers],
                }
        except Exception as exc:  # noqa: BLE001 - evidence-gated return
            return {
                "status": "partial",
                "mode": self.mode,
                "source": self.source,
                "reason": f"file board read failed part-way through: {exc}",
                "partial_result": result,
            }
        return result

    def schematic_state(
        self,
        *,
        include: Sequence[str] | None = None,
        limit: int | None = DEFAULT_LIMIT,
        offset: int = 0,
    ) -> dict:
        sections = _normalize_sections(include, SCHEMATIC_SECTIONS, default=("counts", "symbols"))
        if self.sch_path is None:
            return _unsupported(
                "no sch_path was given, and the file backend has no live instance to ask",
                remedy="pass sch_path, or connect to a running KiCad for copilot mode",
            )
        if not self.sch_path.is_file():
            return {"status": "error", "reason": f"file not found: {self.sch_path}"}
        try:
            from kicad_tools.schema import Schematic

            schematic = Schematic.load(self.sch_path)
        except Exception as exc:  # noqa: BLE001 - evidence-gated return
            return {"status": "error", "reason": f"could not read {self.sch_path}: {exc}"}

        result: dict[str, Any] = {
            "status": "ok",
            "mode": self.mode,
            "source": self.source,
            "sections": sorted(sections),
            "schematic": {"name": self.sch_path.stem, "path": str(self.sch_path)},
        }
        try:
            symbols = list(schematic.symbols)
            labels = list(schematic.labels)
            sheets = list(schematic.sheets)
            if "counts" in sections:
                result["counts"] = {
                    "symbols": len(symbols),
                    "labels": len(labels),
                    "sheet_symbols": len(sheets),
                }
            if "symbols" in sections:
                rows = [_file_symbol_row(sym) for sym in symbols]
                rows.sort(key=lambda r: (r.get("reference") or "~", r.get("uuid") or ""))
                result["symbols"] = paginate(rows, limit, offset)
            if "labels" in sections:
                rows = [
                    {
                        "text": getattr(lbl, "text", None),
                        "uuid": str(getattr(lbl, "uuid", "")) or None,
                        "x_mm": _xy(getattr(lbl, "position", None))[0],
                        "y_mm": _xy(getattr(lbl, "position", None))[1],
                    }
                    for lbl in labels
                ]
                result["labels"] = paginate(rows, limit, offset)
            if "sheets" in sections:
                # Name and file from the raw node, both spellings (KiCad 6 wrote "Sheet file";
                # kicad_tools reads only "Sheetfile" and left the KiCad 6 file empty, ticket 26).
                from kicad_mcp import sheet_tree

                raw = {}
                for node in schematic.sexp.find_children("sheet"):
                    uuid_nodes = node.find_children("uuid")
                    uuid = (uuid_nodes[0].get_string(0) or "") if uuid_nodes else ""
                    raw[uuid] = (sheet_tree.sheet_name_property(node), sheet_tree.sheet_file_property(node)[0])
                rows = []
                for sheet in sheets:
                    uuid = str(getattr(sheet, "uuid", "")) or ""
                    raw_name, raw_file = raw.get(uuid, (None, None))
                    rows.append(
                        {
                            "uuid": uuid or None,
                            "name": getattr(sheet, "name", None) or raw_name,
                            "filename": getattr(sheet, "filename", None) or raw_file,
                        }
                    )
                result["sheets"] = paginate(rows, limit, offset)
        except Exception as exc:  # noqa: BLE001 - evidence-gated return
            return {
                "status": "partial",
                "mode": self.mode,
                "source": self.source,
                "reason": f"file schematic read failed part-way through: {exc}",
                "partial_result": result,
            }
        return result

    def selection(self, *, document: str = "both") -> dict:
        return _unsupported(
            "'what is selected' is a live-GUI concept and has no meaning for a file on disk",
            remedy=(
                "connect to a running KiCad (copilot mode) to read the human's selection, or "
                "address items by reference designator, which works in both modes"
            ),
        )


def _xy(position: Any) -> tuple[float | None, float | None]:
    if position is None:
        return None, None
    if isinstance(position, (tuple, list)) and len(position) >= 2:
        return float(position[0]), float(position[1])
    x = getattr(position, "x", None)
    y = getattr(position, "y", None)
    return (float(x) if x is not None else None, float(y) if y is not None else None)


def _file_footprint_row(fp: Any) -> dict:
    x_mm, y_mm = _xy(getattr(fp, "position", None))
    return {
        "reference": getattr(fp, "reference", None),
        "value": getattr(fp, "value", None),
        "x_mm": x_mm,
        "y_mm": y_mm,
        "rotation_degrees": getattr(fp, "rotation", None),
        "layer": getattr(fp, "layer", None),
        "locked": getattr(fp, "locked", None),
        "uuid": str(getattr(fp, "uuid", "")) or None,
        "library_id": getattr(fp, "name", None),
    }


def _file_symbol_row(sym: Any) -> dict:
    x_mm, y_mm = _xy(getattr(sym, "position", None))
    return {
        "reference": getattr(sym, "reference", None),
        "value": getattr(sym, "value", None),
        "footprint": getattr(sym, "footprint", None) or None,
        "x_mm": x_mm,
        "y_mm": y_mm,
        "uuid": str(getattr(sym, "uuid", "")) or None,
    }


def _normalize_sections(
    include: Sequence[str] | None, allowed: Sequence[str], *, default: Sequence[str]
) -> set[str]:
    if not include:
        return set(default)
    if any(str(s).lower() == "all" for s in include):
        return set(allowed)
    return {str(s).lower() for s in include if str(s).lower() in allowed} or set(default)


# ---------------------------------------------------------------------------
# Resolution: the single entry point every mode-aware tool calls
# ---------------------------------------------------------------------------


@dataclass
class BackendChoice:
    """Either a usable backend, or an actionable explanation of why not."""

    backend: Any | None
    requested_mode: str
    error: dict | None = None
    fell_back: bool = False
    fell_back_reason: str | None = None

    @property
    def ok(self) -> bool:
        return self.backend is not None

    def annotate(self, result: dict) -> dict:
        """Stamp provenance onto a backend result so it can never be mistaken.

        A caller reading ``source: "file"`` on a result they asked for in
        ``auto`` mode learns immediately that they are *not* looking at the
        live GUI -- which is the whole point of refusing a silent fallback.
        """
        if not isinstance(result, dict):
            return result
        result.setdefault("mode", getattr(self.backend, "mode", None))
        result.setdefault("source", getattr(self.backend, "source", None))
        result["requested_mode"] = self.requested_mode
        if self.fell_back:
            result["fell_back_to_file"] = True
            result["fell_back_reason"] = self.fell_back_reason
        return result


def resolve_backend(
    mode: str = MODE_AUTO,
    *,
    pcb_path: str | Path | None = None,
    sch_path: str | Path | None = None,
    socket_url: str | None = None,
    timeout_ms: int | None = None,
    for_mutation: bool = False,
) -> BackendChoice:
    """Pick the backend for this call. **The whole seam is this function.**

    ``mode``:
      * ``"copilot"``  -- live KiCad only. Fails closed if unreachable.
      * ``"autopilot"`` -- files only.
      * ``"auto"`` (default) -- prefer live; for *reads*, fall back to files
        when the transport is unreachable **and** paths were supplied,
        always reporting that it happened. For mutations
        (``for_mutation=True``) there is no fallback at all.
    """
    mode = (mode or MODE_AUTO).lower()
    if mode not in (MODE_COPILOT, MODE_AUTOPILOT, MODE_AUTO):
        return BackendChoice(
            backend=None,
            requested_mode=mode,
            error={
                "status": "error",
                "reason": f"unknown mode {mode!r}",
                "valid_modes": [MODE_COPILOT, MODE_AUTOPILOT, MODE_AUTO],
            },
        )

    if mode == MODE_AUTOPILOT:
        return BackendChoice(FileBackend(pcb_path, sch_path), requested_mode=mode)

    conn = ipc_connection.get_connection(socket_url, timeout_ms=timeout_ms)
    try:
        conn.kicad()
    except IpcUnavailableError as exc:
        if mode == MODE_COPILOT:
            error = exc.to_dict()
            error["mode_requested"] = MODE_COPILOT
            error["fail_closed"] = True
            error["note"] = (
                "copilot mode never silently falls back to editing files: a live KiCad may "
                "have the design open with unsaved changes, and writing the file behind it "
                "would lose the human's work. Either start/enable KiCad, or call again with "
                "mode='autopilot' to work on the files deliberately."
            )
            return BackendChoice(backend=None, requested_mode=mode, error=error)
        # mode == auto
        if for_mutation:
            error = exc.to_dict()
            error["mode_requested"] = MODE_AUTO
            error["fail_closed"] = True
            error["note"] = (
                "mode='auto' will fall back to files for reads, but never for a mutation -- "
                "choose mode='autopilot' explicitly to write to files."
            )
            return BackendChoice(backend=None, requested_mode=mode, error=error)
        if pcb_path is None and sch_path is None:
            error = exc.to_dict()
            error["mode_requested"] = MODE_AUTO
            error["note"] = (
                "no live KiCad, and no file path to fall back to -- supply pcb_path/sch_path "
                "or start KiCad with the API enabled."
            )
            return BackendChoice(backend=None, requested_mode=mode, error=error)
        return BackendChoice(
            FileBackend(pcb_path, sch_path),
            requested_mode=mode,
            fell_back=True,
            fell_back_reason=exc.reason,
        )
    return BackendChoice(IpcBackend(conn), requested_mode=mode)
