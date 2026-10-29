"""Lifecycle for the live connection to a running KiCad GUI (copilot mode).

Licensing note, checked before a single line of integration code was
written (this project has been burned three times -- ``kiutils`` GPLv3,
``easyeda2kicad`` AGPL-3.0, Freerouting GPLv3 -- by trusting a research
summary over a LICENSE file):

    **``kicad-python`` / ``kipy`` is MIT.** Verified from the canonical
    source, ``https://gitlab.com/kicad/code/kicad-python/-/raw/main/LICENSE``
    ("Copyright The KiCad Developers / Permission is hereby granted, free
    of charge, ... without restriction"), which is the ``Repository`` URL
    published in its own PyPI metadata (``license_expression: MIT``). The
    installed wheel carries the same MIT header at the top of every
    module. This is *not* KiCad's own GPLv3 -- the bindings were
    deliberately licensed permissively so plugins need not be GPL.

    Consequence: unlike ``easyeda2kicad``, ``kipy`` may be **imported
    directly** with no subprocess isolation, and this project stays MIT.
    Its own runtime dependencies are permissive too: ``pynng`` (MIT),
    ``protobuf`` (BSD-3-Clause), ``sniffio`` (MIT/Apache-2.0).

Design notes:

* **Fail closed, never silently.** A copilot tool that quietly edited the
  file behind a live GUI's back is exactly the data-loss scenario this
  mode exists to prevent, so "not connected" is always an explicit
  ``status``/``reason``/``remedy`` triple, never a fallback.
* **No file-drift hashing here.** In copilot mode the human's own GUI
  edits *are* the current state -- every read goes to the live instance,
  so the autopilot-mode drift detection (docs/ARCHITECTURE.md, archived backlog P1-19)
  has nothing to detect and is deliberately absent.
* ``kipy`` is imported lazily inside functions so the toolset still
  imports (and reports a clean, actionable status) on a machine where
  ``kicad-python`` isn't installed.
"""

from __future__ import annotations

import os
import random
import string
from typing import Any

from kicad_mcp.ipc import discovery

DEFAULT_TIMEOUT_MS = 2000

# Reason codes are stable strings an agent can branch on without parsing prose.
REASON_KIPY_MISSING = "kipy_not_installed"
REASON_TRANSPORT_UNREACHABLE = "kicad_not_running_or_api_disabled"
REASON_API_ERROR = "kicad_api_error"
REASON_NO_DOCUMENT = "no_matching_document_open"
REASON_UNEXPECTED = "unexpected_error"


class IpcUnavailableError(RuntimeError):
    """A live KiCad connection could not be used, with an actionable reason.

    Carries a ready-to-return result dict so callers never have to invent
    wording, and never accidentally surface a bare traceback.
    """

    def __init__(
        self,
        reason: str,
        *,
        reason_code: str,
        remedy: str | None = None,
        details: dict | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.reason_code = reason_code
        self.remedy = remedy
        self.details = details or {}

    def to_dict(self) -> dict:
        result: dict[str, Any] = {
            "status": "not_connected",
            "reason": self.reason,
            "reason_code": self.reason_code,
        }
        if self.remedy:
            result["remedy"] = self.remedy
        if self.details:
            result["diagnostics"] = self.details
        return result


def _client_name() -> str:
    """A stable, identifiable client name (kipy's default is anonymous-XXXXXXXX).

    KiCad shows the client name in its API log; "kicad-mcp-<pid>" makes it
    obvious to a human which process is talking to their editor.
    """
    override = os.environ.get("KICAD_MCP_IPC_CLIENT_NAME")
    if override:
        return override
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"kicad-mcp-{os.getpid()}-{suffix}"


class IpcConnection:
    """Owns one connection to a running KiCad instance.

    Cheap to construct and lazy: nothing is dialed until :meth:`connect`
    (or an accessor) is called, so a server that never enters copilot mode
    never touches the network stack.
    """

    def __init__(
        self,
        socket_url: str | None = None,
        *,
        token: str | None = None,
        client_name: str | None = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self._requested_socket_url = socket_url
        self._requested_token = token
        self._client_name = client_name or _client_name()
        self._timeout_ms = timeout_ms
        self._kicad: Any = None
        self._resolution: discovery.SocketResolution | None = None
        self._version_info: dict | None = None

    # -- properties ----------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._kicad is not None

    @property
    def client_name(self) -> str:
        return self._client_name

    @property
    def socket_url(self) -> str | None:
        return self._resolution.socket_url if self._resolution else None

    # -- lifecycle -----------------------------------------------------

    def connect(self, *, force: bool = False) -> dict:
        """Establish (or reuse) a live connection. Returns a status dict.

        Never raises: an unreachable KiCad comes back as
        ``{"status": "not_connected", "reason": ..., "remedy": ...}`` with
        the full discovery diagnosis attached, because "KiCad isn't
        running" and "the API preference is off" need completely
        different fixes from the human.
        """
        if self.connected and not force:
            return self.status()
        if force:
            self.close()
        try:
            self._connect_or_raise()
        except IpcUnavailableError as exc:
            return exc.to_dict()
        return self.status()

    def _connect_or_raise(self) -> Any:
        if self._kicad is not None:
            return self._kicad

        try:
            from kipy import KiCad
            from kipy.client import KiCadClient
        except ImportError as exc:
            raise IpcUnavailableError(
                f"the KiCad IPC bindings are not installed ({exc})",
                reason_code=REASON_KIPY_MISSING,
                remedy=(
                    "Install the official MIT-licensed bindings: `pip install kicad-python` "
                    "(module name `kipy`). Copilot mode is unavailable without them; autopilot "
                    "(file-based) tools are unaffected."
                ),
            ) from None

        resolution = discovery.resolve_socket(self._requested_socket_url)
        token, token_source = discovery.resolve_token(self._requested_token)

        # KiCad.from_client rather than KiCad(socket_path=...) so we own the
        # client object explicitly -- kipy has no public accessor for the
        # client it builds internally, and the schematic API needs it.
        client = KiCadClient(resolution.socket_url, self._client_name, token, self._timeout_ms)
        kicad = KiCad.from_client(client)

        try:
            kicad.ping()
        except Exception as exc:  # noqa: BLE001 - transport/protocol errors both land here
            raise IpcUnavailableError(
                f"could not reach a running KiCad instance on {resolution.socket_url} ({exc})",
                reason_code=REASON_TRANSPORT_UNREACHABLE,
                remedy=discovery.ENABLE_API_INSTRUCTIONS,
                details={
                    **discovery.diagnose(),
                    "socket": resolution.to_dict(),
                    "token_source": token_source,
                },
            ) from None

        self._kicad = kicad
        self._resolution = resolution
        self._version_info = self._read_version_info(kicad)
        return kicad

    def _read_version_info(self, kicad: Any) -> dict:
        info: dict[str, Any] = {}
        try:
            version = kicad.get_version()
            info["kicad_version"] = str(version)
        except Exception as exc:  # noqa: BLE001 - version reporting is advisory
            info["kicad_version"] = None
            info["kicad_version_error"] = str(exc)
        try:
            api_version = kicad.get_api_version()
            info["bindings_built_against"] = str(api_version)
        except Exception as exc:  # noqa: BLE001
            info["bindings_built_against"] = None
            info["bindings_version_error"] = str(exc)
        # check_version() *raises* when KiCad is newer than the bindings.
        # That is a real compatibility warning but not a reason to refuse a
        # connection, so it is surfaced as a note rather than an error.
        try:
            kicad.check_version()
            info["version_compatible"] = True
        except Exception as exc:  # noqa: BLE001
            info["version_compatible"] = False
            info["version_note"] = str(exc)
        return info

    def close(self) -> dict:
        """Drop the connection. Idempotent."""
        was_connected = self._kicad is not None
        self._kicad = None
        self._resolution = None
        self._version_info = None
        return {"status": "disconnected", "was_connected": was_connected}

    # -- accessors -----------------------------------------------------

    def kicad(self) -> Any:
        """The live ``kipy.KiCad`` handle, connecting if needed.

        Raises :class:`IpcUnavailableError` (carrying an actionable dict)
        rather than a transport exception.
        """
        return self._connect_or_raise()

    def open_documents(self, doc_type: str) -> list[Any]:
        """Open documents of ``doc_type`` ("pcb", "schematic", "project", ...)."""
        from kipy.proto.common.types import DocumentType

        mapping = {
            "pcb": DocumentType.DOCTYPE_PCB,
            "schematic": DocumentType.DOCTYPE_SCHEMATIC,
            "symbol": DocumentType.DOCTYPE_SYMBOL,
            "footprint": DocumentType.DOCTYPE_FOOTPRINT,
            "project": DocumentType.DOCTYPE_PROJECT,
            "drawing_sheet": DocumentType.DOCTYPE_DRAWING_SHEET,
        }
        if doc_type not in mapping:
            raise IpcUnavailableError(
                f"unknown document type {doc_type!r}; expected one of {sorted(mapping)}",
                reason_code=REASON_UNEXPECTED,
            )
        kicad = self._connect_or_raise()
        try:
            return list(kicad.get_open_documents(mapping[doc_type]))
        except Exception as exc:  # noqa: BLE001
            raise IpcUnavailableError(
                f"KiCad rejected the get_open_documents({doc_type}) request: {exc}",
                reason_code=REASON_API_ERROR,
            ) from None

    def board(self) -> Any:
        """The live ``kipy.board.Board`` for the open PCB.

        Fails closed with ``no_matching_document_open`` when the human has
        KiCad open but no board in it -- distinct from "not connected", and
        it needs a different fix (open the PCB), so it gets its own code.
        """
        from kipy.board import Board

        docs = self.open_documents("pcb")
        if not docs:
            raise IpcUnavailableError(
                "KiCad is connected, but no PCB document is open in it",
                reason_code=REASON_NO_DOCUMENT,
                remedy="Open the board (.kicad_pcb) in KiCad's PCB Editor, then retry.",
            )
        return Board(self._connect_or_raise()._client, docs[0])  # noqa: SLF001

    def schematic(self) -> Any:
        """The live ``kipy.schematic.Schematic`` for the open schematic.

        Worth noting against archived research notes §2, which recorded the IPC API as
        "PCB-editor only in 9.0": KiCad 10's API *does* cover the
        schematic editor in source (``kipy.schematic.Schematic`` exposes
        get_symbols/get_labels/get_netlist/get_selection and a commit
        API) -- **but the published ``kicad-python`` 0.8.0 wheel on PyPI
        cannot actually import that module**, confirmed live on this
        machine: ``kipy/schematic.py`` does
        ``from kipy.common_types import ... PageSettings ...``, and that
        wheel's own ``kipy/common_types.py`` has no ``PageSettings`` class
        at all (added later on the GitLab ``main`` branch, per a
        source diff, but not yet in a release). It's a real
        release/packaging mismatch in kipy itself, not a bug in this
        project or a transport problem -- caught here and turned into the
        same actionable, fail-closed shape as an unreachable socket,
        rather than letting a raw ``ImportError`` escape a tool call.
        """
        try:
            from kipy.schematic import Schematic
        except ImportError as exc:
            raise IpcUnavailableError(
                f"the installed kicad-python cannot load its schematic API module ({exc})",
                reason_code=REASON_API_ERROR,
                remedy=(
                    "this is a known packaging defect in kicad-python 0.8.0 (PyPI): "
                    "kipy/schematic.py imports PageSettings from kipy.common_types, but that "
                    "release's own common_types.py doesn't define it yet (it exists on the "
                    "project's main branch but wasn't in the 0.8.0 release). Live schematic "
                    "reads are unavailable until a fixed kicad-python release ships; live PCB "
                    "reads/writes are unaffected. Track kicad-python's changelog for a fix."
                ),
            ) from None

        docs = self.open_documents("schematic")
        if not docs:
            raise IpcUnavailableError(
                "KiCad is connected, but no schematic document is open in it",
                reason_code=REASON_NO_DOCUMENT,
                remedy="Open the schematic (.kicad_sch) in KiCad's Schematic Editor, then retry.",
            )
        return Schematic(self._connect_or_raise()._client, docs[0])  # noqa: SLF001

    # -- reporting -----------------------------------------------------

    def status(self) -> dict:
        """Current connection status -- safe to call at any time, never raises."""
        if not self.connected:
            resolution = discovery.resolve_socket(self._requested_socket_url)
            return {
                "status": "not_connected",
                "reason": "no live connection has been established yet",
                "reason_code": REASON_TRANSPORT_UNREACHABLE,
                "remedy": discovery.ENABLE_API_INSTRUCTIONS,
                "diagnostics": {**discovery.diagnose(), "socket": resolution.to_dict()},
            }
        result: dict[str, Any] = {
            "status": "connected",
            "client_name": self._client_name,
            "socket": self._resolution.to_dict() if self._resolution else None,
            "timeout_ms": self._timeout_ms,
            **(self._version_info or {}),
        }
        # Which editors the human actually has open is the first thing a
        # copilot needs to know, and it is cheap -- fold it into status.
        for kind in ("pcb", "schematic"):
            try:
                docs = self.open_documents(kind)
            except IpcUnavailableError as exc:
                result[f"open_{kind}_documents"] = {"count": 0, "error": exc.reason}
                continue
            result[f"open_{kind}_documents"] = {
                "count": len(docs),
                "documents": [_document_summary(doc) for doc in docs],
            }
        return result


def _document_summary(doc: Any) -> dict:
    summary: dict[str, Any] = {}
    for field_name in ("board_filename", "schematic_filename", "project"):
        value = getattr(doc, field_name, None)
        if value:
            summary[field_name] = (
                str(value) if not hasattr(value, "name") else str(getattr(value, "name", value))
            )
    if not summary:
        summary["repr"] = str(doc).strip()
    return summary


# ---------------------------------------------------------------------------
# Process-wide connection, so repeated copilot tool calls in one session reuse
# a single socket instead of re-dialing per call.
# ---------------------------------------------------------------------------

_CONNECTION: IpcConnection | None = None


def get_connection(
    socket_url: str | None = None,
    *,
    token: str | None = None,
    timeout_ms: int | None = None,
    reset: bool = False,
) -> IpcConnection:
    """Return the shared connection, creating (or replacing) it as needed."""
    global _CONNECTION
    wants_new = (
        reset
        or _CONNECTION is None
        or (socket_url is not None and socket_url != _CONNECTION._requested_socket_url)  # noqa: SLF001
        or (token is not None and token != _CONNECTION._requested_token)  # noqa: SLF001
        or (timeout_ms is not None and timeout_ms != _CONNECTION._timeout_ms)  # noqa: SLF001
    )
    if wants_new:
        if _CONNECTION is not None:
            _CONNECTION.close()
        _CONNECTION = IpcConnection(
            socket_url,
            token=token,
            timeout_ms=timeout_ms if timeout_ms is not None else DEFAULT_TIMEOUT_MS,
        )
    return _CONNECTION


def reset_connection() -> None:
    """Drop the shared connection (used by tests and by ``copilot_disconnect``)."""
    global _CONNECTION
    if _CONNECTION is not None:
        _CONNECTION.close()
    _CONNECTION = None
