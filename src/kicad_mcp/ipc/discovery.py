"""Discovery of a running KiCad instance's IPC API endpoint.

This is the "connection problem" archived research notes §2 flags: we are an
**external** process, not a plugin launched by KiCad, so KiCad never sets
``KICAD_API_SOCKET``/``KICAD_API_TOKEN`` in *our* environment the way it
does for plugins it spawns itself. Everything here exists to turn that
into an explicit, inspectable resolution rather than a mystery
"connection refused".

Resolution order follows the same house pattern as
``kicad_cli.find_kicad_cli()`` (env-var override -> conventional
location -> platform-specific fallback):

1. an explicit ``socket_url`` argument (highest precedence),
2. the ``KICAD_API_SOCKET`` environment variable,
3. the platform default (``ipc://<tempdir>/kicad/api.sock`` on Windows,
   the flatpak cache path if present, else ``ipc:///tmp/kicad/api.sock``).

``KICAD_API_SOCKET`` and ``KICAD_API_TOKEN`` are the real variable names,
confirmed against the installed ``kipy`` 0.8.0 source
(``kipy/kicad.py::_default_socket_path``/``_default_kicad_token``), not
from memory.

This module never writes anything -- in particular it *reads*
``kicad_common.json`` to report whether the user has the API server
switched on, and never modifies it. Flipping that preference is the
human's call, made in KiCad's own UI.
"""

from __future__ import annotations

import glob
import json
import os
import platform
import sys
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import gettempdir

SOCKET_ENV_VAR = "KICAD_API_SOCKET"
TOKEN_ENV_VAR = "KICAD_API_TOKEN"

#: Where the human turns the API server on. KiCad 9.0+ ships the IPC API
#: **disabled by default**; there is no way for us to enable it remotely
#: (and we deliberately don't try -- see module docstring).
ENABLE_API_INSTRUCTIONS = (
    "In the running KiCad window, open Preferences -> Preferences... -> Plugins and tick "
    "'Enable KiCad API', then click OK. (KiCad 9.0+ ships the IPC API disabled by default.) "
    "The setting is stored as \"api\": {\"enable_server\": true} in kicad_common.json. "
    "KiCad must also actually be running -- the API server only exists inside a live "
    "GUI process, so a headless/closed KiCad has no socket to connect to."
)


@dataclass
class SocketResolution:
    """How the IPC socket URL was arrived at, and what we can see of it."""

    socket_url: str
    source: str  # "explicit" | "env:KICAD_API_SOCKET" | "platform_default"
    endpoint_path: str | None = None
    endpoint_visible: bool | None = None
    endpoint_probe: str = "unknown"

    def to_dict(self) -> dict:
        return {
            "socket_url": self.socket_url,
            "source": self.source,
            "endpoint_path": self.endpoint_path,
            "endpoint_visible": self.endpoint_visible,
            "endpoint_probe": self.endpoint_probe,
        }


@dataclass
class ApiPreference:
    """What ``kicad_common.json`` says about the API server preference."""

    config_path: str | None = None
    enable_server: bool | None = None
    interpreter_path: str | None = None
    read_error: str | None = None
    versions_found: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "config_path": self.config_path,
            "enable_server": self.enable_server,
            "interpreter_path": self.interpreter_path,
            "read_error": self.read_error,
            "kicad_config_versions_found": self.versions_found,
        }


def _platform_default_socket_url() -> str:
    """Mirror of ``kipy.kicad._default_socket_path`` minus the env lookup.

    Reimplemented rather than called because kipy's version silently
    folds the env var in, and this module needs to report *which* of the
    two produced the answer.
    """
    if platform.system() == "Windows":
        return f"ipc://{gettempdir()}\\kicad\\api.sock"

    home = os.environ.get("HOME")
    if home:
        flatpak = f"{home}/.var/app/org.kicad.KiCad/cache/tmp/kicad/api.sock"
        if os.path.exists(flatpak):
            return f"ipc://{flatpak}"
    return "ipc:///tmp/kicad/api.sock"


def _strip_scheme(socket_url: str) -> str | None:
    if socket_url.startswith("ipc://"):
        return socket_url[len("ipc://") :]
    return None


def _probe_endpoint(endpoint_path: str | None) -> tuple[bool | None, str]:
    """Best-effort "does this endpoint exist yet" check.

    On POSIX an ``ipc://`` NNG endpoint is a real filesystem socket, so
    ``os.path.exists`` is meaningful. On Windows NNG maps ``ipc://`` onto
    a **named pipe**, which never appears on disk -- so we enumerate
    ``\\\\.\\pipe\\`` instead. Either probe is advisory only: a truthful
    "can we talk to KiCad" answer only ever comes from actually dialing
    the socket (see ``connection.py``). Returning ``None`` means "could
    not tell", which is reported honestly rather than guessed at.
    """
    if not endpoint_path:
        return None, "no_endpoint_path"
    if sys.platform == "win32":
        try:
            entries = os.listdir(r"\\.\pipe")
        except OSError as exc:  # pragma: no cover - depends on host policy
            return None, f"named_pipe_listing_failed: {exc}"
        needle = endpoint_path.replace("/", "\\").lstrip("\\").lower()
        tail = needle.rsplit("\\kicad\\", 1)[-1]
        for entry in entries:
            low = entry.lower()
            if needle in low or ("kicad" in low and tail in low):
                return True, "named_pipe_listing"
        return False, "named_pipe_listing"
    try:
        return os.path.exists(endpoint_path), "filesystem"
    except OSError as exc:  # pragma: no cover - defensive
        return None, f"filesystem_probe_failed: {exc}"


def resolve_socket(socket_url: str | None = None, *, probe: bool = True) -> SocketResolution:
    """Resolve the IPC socket URL, reporting how it was resolved.

    See the module docstring for the precedence order. ``probe=False``
    skips the (advisory, platform-dependent) endpoint-existence check.
    """
    if socket_url:
        resolution = SocketResolution(socket_url=socket_url, source="explicit")
    else:
        env_value = os.environ.get(SOCKET_ENV_VAR)
        if env_value:
            resolution = SocketResolution(socket_url=env_value, source=f"env:{SOCKET_ENV_VAR}")
        else:
            resolution = SocketResolution(
                socket_url=_platform_default_socket_url(), source="platform_default"
            )

    resolution.endpoint_path = _strip_scheme(resolution.socket_url)
    if probe:
        visible, how = _probe_endpoint(resolution.endpoint_path)
        resolution.endpoint_visible = visible
        resolution.endpoint_probe = how
    return resolution


def resolve_token(token: str | None = None) -> tuple[str, str]:
    """Resolve the KiCad instance token. Returns ``(token, source)``.

    An empty token is normal and fine for an external client: KiCad hands
    one back on the first successful exchange (``KiCadClient.send``
    latches ``reply.header.kicad_token``). The env var only matters when
    several KiCad instances are running and the caller wants a specific
    one.
    """
    if token:
        return token, "explicit"
    env_value = os.environ.get(TOKEN_ENV_VAR)
    if env_value:
        return env_value, f"env:{TOKEN_ENV_VAR}"
    return "", "unset (KiCad will assign one on first exchange)"


def _config_root() -> Path | None:
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        return Path(appdata) / "kicad" if appdata else None
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "kicad"
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "kicad"


def read_api_preference() -> ApiPreference:
    """Read (never write) ``kicad_common.json`` to see if the API is enabled.

    This is what makes a failed connection *actionable* rather than a
    bare "connection refused": when the preference is readable and off,
    we can say precisely that the human needs to tick 'Enable KiCad API',
    instead of making them guess between "KiCad isn't running" and "the
    API is switched off".
    """
    pref = ApiPreference()
    root = _config_root()
    if root is None or not root.is_dir():
        pref.read_error = f"KiCad config directory not found (looked for {root})"
        return pref

    version_dirs = sorted(
        (p for p in root.iterdir() if p.is_dir() and p.name[:1].isdigit()),
        key=lambda p: p.name,
        reverse=True,
    )
    pref.versions_found = [p.name for p in version_dirs]
    for version_dir in version_dirs:
        candidate = version_dir / "kicad_common.json"
        if not candidate.is_file():
            continue
        pref.config_path = str(candidate)
        try:
            data = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            pref.read_error = f"could not read {candidate}: {exc}"
            return pref
        api = data.get("api") or {}
        pref.enable_server = api.get("enable_server")
        pref.interpreter_path = api.get("interpreter_path")
        return pref

    pref.read_error = f"no kicad_common.json found under {root}"
    return pref


def find_kicad_gui_processes() -> list[dict]:
    """Best-effort list of running KiCad GUI processes (no new dependency).

    Purely diagnostic: distinguishing "KiCad isn't open" from "KiCad is
    open but the API is off" is the single most useful thing a failure
    message can tell a human here. Failure to enumerate is reported as an
    empty list plus a note by the caller, never as a claim that KiCad
    isn't running.
    """
    import subprocess

    names = ("kicad.exe", "pcbnew.exe", "eeschema.exe") if sys.platform == "win32" else ("kicad",)
    found: list[dict] = []
    try:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                check=False,
                timeout=15,
            )
            for line in proc.stdout.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if not parts:
                    continue
                image = parts[0].strip('"').lower()
                if image in names:
                    found.append({"name": image, "pid": parts[1] if len(parts) > 1 else None})
        else:  # pragma: no cover - not exercised on the dev machine
            proc = subprocess.run(
                ["pgrep", "-a", "kicad"], capture_output=True, text=True, check=False, timeout=15
            )
            for line in proc.stdout.splitlines():
                pid, _, cmd = line.partition(" ")
                found.append({"name": cmd or "kicad", "pid": pid})
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - defensive
        return []
    return found


def find_kicad_binary(name: str = "kicad") -> Path | None:
    """Locate the KiCad **GUI** executable, reusing the kicad-cli resolver.

    ``kicad_cli.find_kicad_cli()`` already solves KiCad-install discovery
    on this platform properly (docs/ARCHITECTURE.md records four separate
    places where ``kicad_tools``' own discovery falls short on Windows) --
    the GUI binary lives in the same ``bin/`` directory, so derive it
    from that rather than adding a fifth independent search.
    """
    from kicad_mcp import kicad_cli

    try:
        cli_path = kicad_cli.find_kicad_cli()
    except Exception:  # noqa: BLE001 - discovery helper, never raises upward
        return None
    exe = f"{name}.exe" if sys.platform == "win32" else name
    candidate = cli_path.parent / exe
    if candidate.is_file():
        return candidate
    matches = sorted(glob.glob(str(cli_path.parent / f"{name}*")))
    return Path(matches[0]) if matches else None


def diagnose() -> dict:
    """One structured answer to "why can't we reach KiCad?".

    Everything a human needs to fix the situation, gathered without
    needing a successful connection first.
    """
    resolution = resolve_socket()
    token, token_source = resolve_token()
    pref = read_api_preference()
    processes = find_kicad_gui_processes()
    return {
        "socket": resolution.to_dict(),
        "token_source": token_source,
        "token_present": bool(token),
        "api_preference": pref.to_dict(),
        "kicad_gui_processes": {"count": len(processes), "processes": processes},
        "kicad_binary": str(find_kicad_binary() or "") or None,
        "how_to_enable": ENABLE_API_INSTRUCTIONS,
    }
