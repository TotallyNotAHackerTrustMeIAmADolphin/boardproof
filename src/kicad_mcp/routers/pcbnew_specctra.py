"""KiCad's own Specctra DSN export and SES import, run INSIDE KiCad's bundled Python.

This file is a script, not a module of the package: the host starts it as
``<KiCad's python> -I pcbnew_specctra.py <command> ...`` (``routing.run_kicad_specctra``).
Nothing in ``kicad_mcp`` imports it, because ``pcbnew`` and ``wx`` exist only in KiCad's
interpreter. Build ticket 46 (``paradigm-review-2`` ticket 17, docs/ARCHITECTURE.md decision 3).

Commands (every one prints exactly one result line ``KICAD_MCP_RESULT {json}`` on stdout):

* ``probe``: the pcbnew version; proves the interpreter can import ``pcbnew``.
* ``export BOARD DSN``: ``pcbnew.ExportSpecctraDSN(board, DSN)``. The board is never saved.
* ``import BOARD SES``: ``pcbnew.ImportSpecctraSES(board, SES)``, then ``pcbnew.SaveBoard``
  over BOARD. Only ever pass a scratch copy: KiCad's importer deletes every unlocked track
  and via before it adds the session's.
* ``pads BOARD``: every pad's position as KiCad computes it (test oracle).

In ``export`` and ``import`` every track, arc and via already on the board is locked in
memory first. KiCad then exports it as ``fix`` wiring (Freerouting keeps it as an
unmovable obstacle and does not send it back) and its importer keeps it instead of deleting
it, so the routed copy differs from the input only by the router's new copper.

``ImportSpecctraSES`` returns ``True`` even when it skipped items (KiCad 10.0.7
``specctra_import.cpp``: unknown layer or padstack are counted and only reported through
``wxLogWarning``; a net name the board does not know silently lands on net 0). So ``import``
never trusts it: it counts what the session holds (``expected``), what the board gained
(``imported``) and new copper on no net, and reports the importer's own warnings.

Two guards against a modal dialog on the user's desktop (build ticket 61, brief rules 7 and
8), both in ``_setup`` before ``pcbnew`` is imported: ``KICAD_CONFIG_HOME`` must be the
throwaway working directory the host made for this run (``run_kicad_specctra``'s
``TemporaryDirectory``, never the user's KiCad profile; the script refuses otherwise), and
wx logging goes to stderr (a font substitution message otherwise opens a message box that
blocks the run). Importing this file has no side effects: ``_setup`` runs only under
``__main__``, so the host's tests can import the pure helpers (review round 1, finding 4).
"""

from __future__ import annotations

import importlib
import json
import os
import sys
import tempfile

RESULT_MARKER = "KICAD_MCP_RESULT "

pcbnew = None  # imported by _setup, in KiCad's interpreter only

_LOG: list[dict] = []


def _route_wx_log_to_stderr() -> None:
    import wx

    wx.Log.SetActiveTarget(wx.LogStderr())

    class _Capture(wx.Log):
        """Every wx log message goes to stderr and into ``_LOG``; nothing opens a window."""

        def DoLogTextAtLevel(self, level, msg):  # noqa: N802 - wx's virtual
            _LOG.append({"level": int(level), "message": str(msg)})
            sys.stderr.write(f"wx[{int(level)}]: {msg}\n")

    try:
        wx.Log.SetActiveTarget(_Capture())
    except Exception as exc:  # noqa: BLE001 - LogStderr stays active
        sys.stderr.write(f"wx log capture unavailable, stderr only: {exc}\n")


def _require_throwaway_profile() -> None:
    """``KICAD_CONFIG_HOME`` must be the working directory the host created for this run."""
    home = os.environ.get("KICAD_CONFIG_HOME")
    if not home or not os.path.isdir(home) or not os.path.samefile(home, os.getcwd()):
        raise RuntimeError(
            f"KICAD_CONFIG_HOME={home!r} is not this run's throwaway working directory {os.getcwd()!r}; "
            "start the runner through routing.run_kicad_specctra"
        )


def _setup() -> None:
    """Throwaway profile, wx log to stderr, then ``import pcbnew``: in this order."""
    global pcbnew
    _require_throwaway_profile()
    _route_wx_log_to_stderr()
    pcbnew = importlib.import_module("pcbnew")


def _flush_log() -> None:
    try:
        import wx

        wx.Log.FlushActive()
    except Exception:  # noqa: BLE001
        pass


def _emit(result: dict) -> None:
    _flush_log()
    if pcbnew is not None:
        result.setdefault("pcbnew_version", str(pcbnew.GetBuildVersion()))
    result["wx_log"] = list(_LOG)
    sys.stdout.write(RESULT_MARKER + json.dumps(result) + "\n")
    sys.stdout.flush()


def _copper(board) -> dict:
    counts = {"tracks": 0, "arcs": 0, "vias": 0}
    for item in board.GetTracks():
        kind = item.GetClass()
        if kind == "PCB_VIA":
            counts["vias"] += 1
        elif kind == "PCB_ARC":
            counts["arcs"] += 1
        else:
            counts["tracks"] += 1
    return counts


def _lock_existing_copper(board) -> int:
    locked = 0
    for item in board.GetTracks():
        if not item.IsLocked():
            item.SetLocked(True)
            locked += 1
    return locked


# --- a minimal S-expression reader, used only to COUNT what a session holds ------------------


def _tokens(text: str):
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in "()":
            yield c
            i += 1
        elif c.isspace():
            i += 1
        elif c == '"':
            j = text.find('"', i + 1)
            if j < 0:
                raise ValueError("unterminated string in the session file")
            yield ("str", text[i + 1:j])
            i = j + 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in '()"':
                j += 1
            yield text[i:j]
            i = j


def _tree(text: str) -> list:
    stack: list[list] = [[]]
    for tok in _tokens(text):
        if tok == "(":
            stack.append([])
        elif tok == ")":
            if len(stack) == 1:
                raise ValueError("unbalanced ')' in the session file")
            done = stack.pop()
            stack[-1].append(done)
        else:
            stack[-1].append(tok[1] if isinstance(tok, tuple) else tok)
    if len(stack) != 1:
        raise ValueError("unbalanced '(' in the session file")
    return stack[0]


def _head(node) -> str | None:
    return node[0] if isinstance(node, list) and node and isinstance(node[0], str) else None


def _children(node, name: str) -> list:
    return [c for c in node[1:] if _head(c) == name] if isinstance(node, list) else []


def _is_number(atom) -> bool:
    if not isinstance(atom, str):
        return False
    try:
        float(atom)
    except ValueError:
        return False
    return True


def session_counts(text: str) -> dict:
    """What KiCad's importer should add for this session: one track per path step, one arc
    per ``qarc``, one via per via vertex. Polygon wires are ignored by KiCad (counted)."""
    top = _tree(text)
    sessions = [n for n in top if _head(n) == "session"]
    if len(sessions) != 1:
        raise ValueError(f"expected one (session ...), found {len(sessions)}")
    session = sessions[0]
    expected = {"tracks": 0, "arcs": 0, "vias": 0}
    other_wires = 0
    nets = 0
    for routes in _children(session, "routes"):
        for network_out in _children(routes, "network_out"):
            for net in _children(network_out, "net"):
                nets += 1
                for wire in _children(net, "wire"):
                    shapes = [c for c in wire[1:] if isinstance(c, list)]
                    shape = shapes[0] if shapes else None
                    if _head(shape) == "path":
                        points = sum(1 for a in shape[3:] if _is_number(a)) // 2
                        expected["tracks"] += max(points - 1, 0)
                    elif _head(shape) == "qarc":
                        expected["arcs"] += 1
                    else:
                        other_wires += 1
                for via in _children(net, "via"):
                    coords = [a for a in via[2:] if _is_number(a)]
                    expected["vias"] += len(coords) // 2
    placements = sum(
        len(_children(component, "place"))
        for placement in _children(session, "placement")
        for component in _children(placement, "component")
    )
    return {"expected": expected, "ignored_wire_shapes": other_wires, "nets": nets,
            "placement_entries": placements}


# --- commands -----------------------------------------------------------------------------------


def cmd_probe() -> dict:
    return {"ok": True}


def cmd_export(board_path: str, dsn_path: str) -> dict:
    board = pcbnew.LoadBoard(board_path)
    existing = _copper(board)
    locked = _lock_existing_copper(board)
    returned = bool(pcbnew.ExportSpecctraDSN(board, dsn_path))
    return {
        "ok": returned and os.path.isfile(dsn_path),
        "returned": returned,
        "dsn_exists": os.path.isfile(dsn_path),
        "existing_copper": existing,
        "locked_for_export": locked,
    }


def cmd_import(board_path: str, ses_path: str) -> dict:
    with open(ses_path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    try:
        counted = session_counts(text)
    except ValueError as exc:
        return {"ok": False, "error": f"the session file is not readable: {exc}"}
    board = pcbnew.LoadBoard(board_path)
    before = _copper(board)
    locked = _lock_existing_copper(board)
    ids_before = {item.m_Uuid.AsString() for item in board.GetTracks()}
    returned = bool(pcbnew.ImportSpecctraSES(board, ses_path))
    _flush_log()
    after = _copper(board)
    new_items = [item for item in board.GetTracks() if item.m_Uuid.AsString() not in ids_before]
    imported = {k: after[k] - before[k] for k in after}
    no_net = sum(1 for item in new_items if item.GetNetCode() <= 0)
    expected = counted["expected"]
    missing = {k: max(expected[k] - imported[k], 0) for k in expected}
    unexpected = {k: max(imported[k] - expected[k], 0) for k in expected}
    result = {
        "returned": returned,
        "existing_copper": before,
        "locked_for_import": locked,
        "session": counted,
        "imported": imported,
        "missing": missing,
        "unexpected": unexpected,
        "new_items_on_no_net": no_net,
        "importer_warnings": [e["message"] for e in _LOG if e["level"] <= 2],
    }
    if not returned:
        return {**result, "ok": False, "error": "ImportSpecctraSES failed (it threw before or while importing)"}
    saved = pcbnew.SaveBoard(board_path, board)
    if saved is False:
        return {**result, "ok": False, "error": "SaveBoard failed"}
    return {**result, "ok": True}


def cmd_pads(board_path: str) -> dict:
    board = pcbnew.LoadBoard(board_path)
    rows = []
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            p = pad.GetPosition()
            rows.append({
                "ref": fp.GetReference(), "pad": pad.GetNumber(),
                "x_mm": pcbnew.ToMM(p.x), "y_mm": pcbnew.ToMM(p.y),
                "fp_rotation": fp.GetOrientationDegrees(),
                "side": "B" if fp.GetLayer() == pcbnew.B_Cu else "F",
                "net": pad.GetNetname(),
            })
    return {"ok": True, "pads": rows}


_COMMANDS = {"probe": (cmd_probe, 0), "export": (cmd_export, 2), "import": (cmd_import, 2), "pads": (cmd_pads, 1)}
# The argument each command writes: export writes the DSN, import saves over the board.
_WRITES = {"export": 1, "import": 0}


def _inside_temp(path: str) -> bool:
    """True when ``path`` lies inside the system temp directory (both resolved)."""
    temp = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
    target = os.path.normcase(os.path.realpath(os.path.abspath(path)))
    try:
        return os.path.commonpath([temp, target]) == temp and target != temp
    except ValueError:  # another drive
        return False


def main(argv: list[str]) -> int:
    if not argv or argv[0] not in _COMMANDS or len(argv) - 1 != _COMMANDS[argv[0]][1]:
        _emit({"ok": False, "error": f"usage: {sorted(_COMMANDS)} with their arguments, got {argv!r}"})
        return 2
    if argv[0] in _WRITES and not _inside_temp(argv[1 + _WRITES[argv[0]]]):
        _emit({"ok": False, "error": f"{argv[0]} writes only inside a temporary directory "
                                     f"({tempfile.gettempdir()}); refused {argv[1 + _WRITES[argv[0]]]!r}"})
        return 2
    func, _ = _COMMANDS[argv[0]]
    try:
        result = func(*argv[1:])
    except Exception as exc:  # noqa: BLE001 - reported to the host, never swallowed
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    _emit(result)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    try:
        _setup()
    except Exception as exc:  # noqa: BLE001 - reported to the host as the result line
        _emit({"ok": False, "error": f"setup failed: {type(exc).__name__}: {exc}"})
        sys.exit(3)
    sys.exit(main(sys.argv[1:]))
