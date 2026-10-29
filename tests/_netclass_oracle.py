"""KiCad's own answer to "which net class does this net get": run INSIDE KiCad's bundled Python.

``<KiCad's python> -I _netclass_oracle.py BOARD NETS_JSON`` loads BOARD with ``pcbnew.LoadBoard``
(which reads the ``.kicad_pro`` beside it, never saves anything) and prints one line
``NETCLASS_ORACLE {json}``: for every net name in NETS_JSON (a JSON list; the names need not exist
on the board) the name of ``NET_SETTINGS.GetEffectiveNetClass(net)`` and its values in mm.

The host (``tests/test_board_rules_netclass_oracle.py``) starts it in a throwaway working
directory that is also ``KICAD_CONFIG_HOME``; the script refuses any other profile, and routes wx
logging to stderr before ``pcbnew`` is imported, so no KiCad dialog can open. Test helper only:
nothing in ``kicad_mcp`` imports it.
"""

from __future__ import annotations

import json
import os
import sys

MARKER = "NETCLASS_ORACLE "

GETTERS = {
    "track_width": "GetTrackWidth",
    "via_diameter": "GetViaDiameter",
    "via_drill": "GetViaDrill",
    "microvia_diameter": "GetuViaDiameter",
    "microvia_drill": "GetuViaDrill",
    "clearance": "GetClearance",
}


def main(board_path: str, nets_json: str) -> dict:
    home = os.environ.get("KICAD_CONFIG_HOME")
    if not home or not os.path.isdir(home) or not os.path.samefile(home, os.getcwd()):
        raise RuntimeError(f"KICAD_CONFIG_HOME={home!r} is not this run's throwaway working directory")
    import wx

    wx.Log.SetActiveTarget(wx.LogStderr())
    import pcbnew

    board = pcbnew.LoadBoard(board_path)
    settings = board.GetDesignSettings().m_NetSettings
    out = {}
    for net in json.loads(nets_json):
        netclass = settings.GetEffectiveNetClass(net)
        out[net] = {"name": str(netclass.GetName())} | {
            key: pcbnew.ToMM(getattr(netclass, getter)()) for key, getter in GETTERS.items()
        }
    return {"ok": True, "pcbnew_version": str(pcbnew.Version()), "nets": out}


if __name__ == "__main__":
    try:
        result = main(sys.argv[1], sys.argv[2])
    except Exception as exc:  # noqa: BLE001 - reported to the host, which fails the test
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    sys.stdout.write(MARKER + json.dumps(result) + "\n")
