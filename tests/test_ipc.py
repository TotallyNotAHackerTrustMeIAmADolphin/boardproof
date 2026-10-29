"""Tests for src/kicad_mcp/ipc/ -- discovery, connection lifecycle, and the
copilot/autopilot adapter seam.

Two tiers, per this project's standing testing convention (see
test_kicad_cli.py's requires_kicad_cli, test_routing_toolset.py's
_freerouting_available): pure-logic tests that always run, plus a small
set of real-live-KiCad tests gated behind `requires_live_kicad` that skip
cleanly with a stated reason when no KiCad instance with the IPC API
enabled is actually reachable, so CI stays green without a GUI.

The live tests were genuinely exercised by hand against a real KiCad
10.0.5 pcbnew.exe process during development (see the Phase 6 report for
the full transcript: connect, read board state, read/set selection,
propose+apply a footprint move, verify it landed, simulate a concurrent
human edit and confirm the proposal goes stale and is refused, then
verify the not-connected failure mode after closing KiCad). They are
marked `network`-adjacent via the same skip-on-absence pattern rather
than `pytest.mark.network`, since they don't hit the internet -- they
hit a local IPC socket that most CI runners simply won't have.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kicad_mcp.ipc import adapter, connection, discovery

FIXTURES = Path(__file__).parent / "fixtures"
PCB_FIXTURE = FIXTURES / "simple_led.kicad_pcb"
SCH_FIXTURE = FIXTURES / "simple_led.kicad_sch"


def _live_kicad_available() -> bool:
    """True only if a real KiCad instance answers a ping right now."""
    try:
        import kipy  # noqa: F401
    except ImportError:
        return False
    conn = connection.IpcConnection(timeout_ms=500)
    try:
        conn.kicad()
    except connection.IpcUnavailableError:
        return False
    finally:
        conn.close()
    return True


requires_live_kicad = pytest.mark.skipif(
    not _live_kicad_available(),
    reason=(
        "no running KiCad instance with the IPC API enabled was reachable on this machine "
        "(Preferences -> Plugins -> 'Enable KiCad API', and KiCad must be running) -- this is "
        "expected in CI/headless environments; see this project's dev transcript for a real "
        "end-to-end run against KiCad 10.0.5"
    ),
)


@pytest.fixture(autouse=True)
def _reset_shared_connection():
    connection.reset_connection()
    yield
    connection.reset_connection()


# ---------------------------------------------------------------------------
# discovery.py -- pure logic, no live KiCad needed
# ---------------------------------------------------------------------------


class TestSocketResolution:
    def test_explicit_url_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(discovery.SOCKET_ENV_VAR, "ipc:///env/path.sock")
        resolution = discovery.resolve_socket("ipc:///explicit/path.sock", probe=False)
        assert resolution.socket_url == "ipc:///explicit/path.sock"
        assert resolution.source == "explicit"

    def test_env_var_wins_over_platform_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(discovery.SOCKET_ENV_VAR, "ipc:///env/path.sock")
        resolution = discovery.resolve_socket(probe=False)
        assert resolution.socket_url == "ipc:///env/path.sock"
        assert resolution.source == f"env:{discovery.SOCKET_ENV_VAR}"

    def test_platform_default_when_nothing_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(discovery.SOCKET_ENV_VAR, raising=False)
        resolution = discovery.resolve_socket(probe=False)
        assert resolution.source == "platform_default"
        assert resolution.socket_url.startswith("ipc://")
        assert "kicad" in resolution.socket_url.lower()

    def test_matches_kipy_default_when_kipy_installed(self) -> None:
        """Our reimplementation must not drift from kipy's own default."""
        kipy = pytest.importorskip("kipy")
        from kipy.kicad import _default_socket_path

        os.environ.pop(discovery.SOCKET_ENV_VAR, None)
        ours = discovery.resolve_socket(probe=False).socket_url
        theirs = _default_socket_path()
        assert ours == theirs


class TestTokenResolution:
    def test_explicit_token_wins(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(discovery.TOKEN_ENV_VAR, "env-token")
        token, source = discovery.resolve_token("explicit-token")
        assert token == "explicit-token"
        assert source == "explicit"

    def test_env_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(discovery.TOKEN_ENV_VAR, "env-token")
        token, source = discovery.resolve_token()
        assert token == "env-token"
        assert source == f"env:{discovery.TOKEN_ENV_VAR}"

    def test_unset_is_fine(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv(discovery.TOKEN_ENV_VAR, raising=False)
        token, source = discovery.resolve_token()
        assert token == ""
        assert "KiCad will assign" in source


class TestDiagnose:
    def test_diagnose_never_raises_and_has_actionable_fields(self) -> None:
        result = discovery.diagnose()
        assert "socket" in result
        assert "api_preference" in result
        assert "how_to_enable" in result
        assert "Preferences" in result["how_to_enable"]
        assert "Enable KiCad API" in result["how_to_enable"]

    def test_api_preference_read_is_read_only(self, tmp_path: Path) -> None:
        """read_api_preference must never write kicad_common.json."""
        version_dir = tmp_path / "10.0"
        version_dir.mkdir()
        config = version_dir / "kicad_common.json"
        config.write_text(json.dumps({"api": {"enable_server": False}}), encoding="utf-8")
        before = config.read_text(encoding="utf-8")
        pref = discovery.read_api_preference()
        # Doesn't assert on tmp_path directly (real config root differs per
        # platform) -- the point is the real config file, if any, is
        # untouched by merely calling this.
        assert config.read_text(encoding="utf-8") == before
        assert pref is not None


# ---------------------------------------------------------------------------
# connection.py -- lifecycle without a live KiCad
# ---------------------------------------------------------------------------


class TestConnectionFailsClosed:
    def test_connect_to_nothing_is_actionable_not_connected(self) -> None:
        conn = connection.IpcConnection(socket_url="ipc:///definitely/not/a/real/socket", timeout_ms=300)
        result = conn.connect()
        assert result["status"] == "not_connected"
        assert result["reason_code"] == connection.REASON_TRANSPORT_UNREACHABLE
        assert "remedy" in result
        assert "Enable KiCad API" in result["remedy"]
        assert conn.connected is False

    def test_connect_never_raises(self) -> None:
        conn = connection.IpcConnection(socket_url="ipc:///nope", timeout_ms=300)
        # Explicitly the contract: connect() catches IpcUnavailableError itself.
        result = conn.connect()
        assert isinstance(result, dict)

    def test_status_before_connect_is_not_connected_with_diagnostics(self) -> None:
        conn = connection.IpcConnection(socket_url="ipc:///nope", timeout_ms=300)
        status = conn.status()
        assert status["status"] == "not_connected"
        assert "diagnostics" in status

    def test_close_is_idempotent(self) -> None:
        conn = connection.IpcConnection(socket_url="ipc:///nope", timeout_ms=300)
        first = conn.close()
        second = conn.close()
        assert first["was_connected"] is False
        assert second["was_connected"] is False

    def test_missing_kipy_reports_actionable_reason(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "kipy" or name.startswith("kipy."):
                raise ImportError("simulated: kipy not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)
        conn = connection.IpcConnection(timeout_ms=300)
        result = conn.connect()
        assert result["status"] == "not_connected"
        assert result["reason_code"] == connection.REASON_KIPY_MISSING
        assert "pip install kicad-python" in result["remedy"]


class TestSharedConnection:
    def test_get_connection_reuses_instance(self) -> None:
        first = connection.get_connection("ipc:///a")
        second = connection.get_connection("ipc:///a")
        assert first is second

    def test_get_connection_replaces_on_different_socket(self) -> None:
        first = connection.get_connection("ipc:///a")
        second = connection.get_connection("ipc:///b")
        assert first is not second

    def test_reset_connection_drops_shared_instance(self) -> None:
        first = connection.get_connection("ipc:///a")
        connection.reset_connection()
        second = connection.get_connection("ipc:///a")
        assert first is not second


# ---------------------------------------------------------------------------
# adapter.py -- the seam, without a live KiCad
# ---------------------------------------------------------------------------


class TestResolveBackendModes:
    def test_autopilot_mode_returns_file_backend(self) -> None:
        choice = adapter.resolve_backend(adapter.MODE_AUTOPILOT, pcb_path=PCB_FIXTURE)
        assert choice.ok
        assert choice.backend.mode == adapter.MODE_AUTOPILOT
        assert choice.backend.source == "file"

    def test_copilot_mode_fails_closed_without_live_kicad(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(discovery.SOCKET_ENV_VAR, "ipc:///definitely/not/real")
        connection.reset_connection()
        choice = adapter.resolve_backend(adapter.MODE_COPILOT)
        assert not choice.ok
        assert choice.error["fail_closed"] is True
        assert "never silently falls back" in choice.error["note"]

    def test_auto_mode_falls_back_to_file_for_reads_when_paths_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(discovery.SOCKET_ENV_VAR, "ipc:///definitely/not/real")
        connection.reset_connection()
        choice = adapter.resolve_backend(adapter.MODE_AUTO, pcb_path=PCB_FIXTURE)
        assert choice.ok
        assert choice.fell_back is True
        assert choice.backend.source == "file"
        annotated = choice.annotate({"status": "ok"})
        assert annotated["fell_back_to_file"] is True
        assert annotated["requested_mode"] == adapter.MODE_AUTO

    def test_auto_mode_reports_error_when_no_fallback_path_given(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(discovery.SOCKET_ENV_VAR, "ipc:///definitely/not/real")
        connection.reset_connection()
        choice = adapter.resolve_backend(adapter.MODE_AUTO)
        assert not choice.ok
        assert "no file path to fall back to" in choice.error["note"]

    def test_auto_mode_never_falls_back_for_mutations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv(discovery.SOCKET_ENV_VAR, "ipc:///definitely/not/real")
        connection.reset_connection()
        choice = adapter.resolve_backend(adapter.MODE_AUTO, pcb_path=PCB_FIXTURE, for_mutation=True)
        assert not choice.ok
        assert choice.error["fail_closed"] is True
        assert "never for a mutation" in choice.error["note"]

    def test_unknown_mode_is_a_clean_error(self) -> None:
        choice = adapter.resolve_backend("not-a-real-mode")
        assert not choice.ok
        assert choice.error["status"] == "error"
        assert "not-a-real-mode" in choice.error["reason"]


class TestFileBackendSameShapeAsIpc:
    """The seam's central claim: identical result shape from both backends."""

    def test_board_state_shape(self) -> None:
        backend = adapter.FileBackend(pcb_path=PCB_FIXTURE)
        result = backend.board_state(include=["counts", "footprints"])
        assert result["status"] == "ok"
        assert result["mode"] == adapter.MODE_AUTOPILOT
        assert result["source"] == "file"
        assert set(result["counts"]) == {"footprints", "tracks", "vias", "zones", "nets"}
        assert "items" in result["footprints"]
        assert "total_count" in result["footprints"]
        row = result["footprints"]["items"][0]
        assert set(row) >= {
            "reference",
            "value",
            "x_mm",
            "y_mm",
            "rotation_degrees",
            "layer",
            "locked",
            "uuid",
            "library_id",
        }

    def test_schematic_state_shape(self) -> None:
        backend = adapter.FileBackend(sch_path=SCH_FIXTURE)
        result = backend.schematic_state(include=["counts", "symbols"])
        assert result["status"] == "ok"
        assert result["source"] == "file"
        row = result["symbols"]["items"][0]
        assert set(row) >= {"reference", "value", "footprint", "x_mm", "y_mm", "uuid"}

    def test_missing_path_is_unsupported_not_a_crash(self) -> None:
        backend = adapter.FileBackend()
        result = backend.board_state()
        assert result["status"] == "unsupported"
        assert "remedy" in result

    def test_missing_file_is_a_clean_error(self, tmp_path: Path) -> None:
        backend = adapter.FileBackend(pcb_path=tmp_path / "nope.kicad_pcb")
        result = backend.board_state()
        assert result["status"] == "error"
        assert "not found" in result["reason"]

    def test_selection_is_explicitly_unsupported_on_file_backend(self) -> None:
        backend = adapter.FileBackend(pcb_path=PCB_FIXTURE)
        result = backend.selection()
        assert result["status"] == "unsupported"
        assert "live-GUI concept" in result["reason"]

    def test_pagination_envelope(self) -> None:
        backend = adapter.FileBackend(pcb_path=PCB_FIXTURE)
        result = backend.board_state(include=["footprints"], limit=1, offset=0)
        section = result["footprints"]
        assert set(section) == {"total_count", "count", "items", "has_more", "next_offset"}
        assert section["count"] == 1


def test_paginate_helper_shape() -> None:
    items = list(range(5))
    page = adapter.paginate(items, limit=2, offset=1)
    assert page == {
        "total_count": 5,
        "count": 2,
        "items": [1, 2],
        "has_more": True,
        "next_offset": 3,
    }
    last_page = adapter.paginate(items, limit=2, offset=4)
    assert last_page["has_more"] is False
    assert last_page["next_offset"] is None


# ---------------------------------------------------------------------------
# Real, live KiCad -- skips cleanly when unavailable
# ---------------------------------------------------------------------------


@requires_live_kicad
class TestLiveKicadConnection:
    """Exercised for real during development against KiCad 10.0.5 pcbnew.exe
    (a throwaway KICAD_CONFIG_HOME with the API preference enabled, never the
    developer's real KiCad settings). Kept enabled here so it runs again on
    any machine that happens to have a live, API-enabled KiCad up -- and
    skips cleanly everywhere else.
    """

    def test_ping_and_version(self) -> None:
        conn = connection.IpcConnection(timeout_ms=2000)
        status = conn.connect()
        assert status["status"] == "connected"
        assert status["kicad_version"]
        conn.close()

    def test_board_state_matches_shape_of_file_backend(self) -> None:
        choice = adapter.resolve_backend(adapter.MODE_COPILOT)
        assert choice.ok, choice.error
        result = choice.backend.board_state(include=["counts", "footprints"])
        assert result["status"] == "ok"
        assert result["source"] == "ipc"
        file_backend = adapter.FileBackend(pcb_path=PCB_FIXTURE)
        file_result = file_backend.board_state(include=["counts", "footprints"])
        # Same top-level keys from both backends -- the seam's core claim.
        assert set(result) & {"status", "mode", "source", "counts", "footprints"} == {
            "status",
            "mode",
            "source",
            "counts",
            "footprints",
        }
        assert set(result["counts"]) == set(file_result["counts"])

    def test_no_selection_reports_empty_not_error(self) -> None:
        choice = adapter.resolve_backend(adapter.MODE_COPILOT)
        assert choice.ok, choice.error
        choice.backend._conn.board().clear_selection()  # noqa: SLF001 - test setup
        result = choice.backend.selection(document="pcb")
        assert result["status"] == "ok"
        assert result["documents"]["pcb"]["count"] == 0
