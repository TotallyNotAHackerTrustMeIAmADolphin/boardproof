"""Live KiCad IPC support and the copilot/autopilot mode-adapter seam.

Three modules, in dependency order:

* :mod:`kicad_mcp.ipc.discovery` -- how we find a running KiCad's IPC
  socket as an *external* process (``KICAD_API_SOCKET`` override ->
  platform default), plus the diagnosis that turns "connection refused"
  into an actionable message.
* :mod:`kicad_mcp.ipc.connection` -- the connection lifecycle, and the
  ``kipy`` licensing verification (MIT -- see that module's docstring).
* :mod:`kicad_mcp.ipc.adapter` -- the seam itself: one ``DesignBackend``
  protocol, an ``IpcBackend`` (copilot) and a ``FileBackend`` (autopilot)
  behind it, and ``resolve_backend()`` as the single entry point every
  mode-aware tool calls.
"""

from kicad_mcp.ipc.adapter import (
    CAP_BOARD_READ,
    CAP_LIVE_MUTATE,
    CAP_SCHEMATIC_READ,
    CAP_SELECTION,
    MODE_AUTO,
    MODE_AUTOPILOT,
    MODE_COPILOT,
    BackendChoice,
    DesignBackend,
    FileBackend,
    IpcBackend,
    resolve_backend,
)
from kicad_mcp.ipc.connection import (
    IpcConnection,
    IpcUnavailableError,
    get_connection,
    reset_connection,
)
from kicad_mcp.ipc.discovery import (
    ENABLE_API_INSTRUCTIONS,
    SOCKET_ENV_VAR,
    TOKEN_ENV_VAR,
    diagnose,
    resolve_socket,
)

__all__ = [
    "BackendChoice",
    "CAP_BOARD_READ",
    "CAP_LIVE_MUTATE",
    "CAP_SCHEMATIC_READ",
    "CAP_SELECTION",
    "DesignBackend",
    "ENABLE_API_INSTRUCTIONS",
    "FileBackend",
    "IpcBackend",
    "IpcConnection",
    "IpcUnavailableError",
    "MODE_AUTO",
    "MODE_AUTOPILOT",
    "MODE_COPILOT",
    "SOCKET_ENV_VAR",
    "TOKEN_ENV_VAR",
    "diagnose",
    "get_connection",
    "reset_connection",
    "resolve_backend",
    "resolve_socket",
]
