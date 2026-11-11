"""Autorouter backends.

Each backend is a subprocess wrapper that routes a **trial copy** of a board
and hands back a delta of traces/vias as plain specs; the routing toolset
(`toolsets/routing.py`) owns the shared dry-run + revision-token checkpoint,
the real-`kicad-cli`-DRC scorer, and the confirm/apply path through
`kicad_tools`. Backends never write to the caller's board.

Backends:

* Freerouting -- lives in `toolsets/routing.py` (P4-02/P4-03; DSN/SES round
  trip with six worked-around `kicad_tools` export bugs).
* KiCadRoutingTools -- `kicad_routing_tools.py` (P4-05; routes
  `.kicad_pcb` -> `.kicad_pcb` directly, no DSN/SES).
"""
