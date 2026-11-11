"""Issue 64: `scripts/setup_krt.py` restores the router checkout and its binary, network and git stubbed.

The script clones the upstream repository into `.tools/KiCadRoutingTools`, checks out the pinned full
sha, fetches the prebuilt binary of the matching release and verifies its SHA-256 against a value
recorded in the script. No test here touches the network or a real git: `run_git` and `download`
are replaced.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("setup_krt", ROOT / "scripts" / "setup_krt.py")
setup = importlib.util.module_from_spec(_spec)
sys.modules["setup_krt"] = setup
_spec.loader.exec_module(setup)

PIN = "c64c0db25e6cf23afa6769ee05665c7ea22b3ff2"
OTHER = "299ad57081d303ac60cbc84cd374f6e3800e9158"
BINARY = b"fake grid_router binary"


class FakeGit:
    """A stand-in for `setup.run_git`: one repository whose HEAD is a sha, and a call log."""

    def __init__(self, *, checkout_lands_on: str = PIN, dirty: str = ""):
        self.remote = setup.REPO_URL
        self.calls: list[list[str]] = []
        self.head = OTHER
        self.checkout_lands_on = checkout_lands_on
        self.dirty = dirty

    def __call__(self, args, cwd=None):
        self.calls.append(list(args))
        verb = args[0]
        if verb == "clone":
            dest = Path(args[-1])
            (dest / ".git").mkdir(parents=True)
            (dest / "rust_router").mkdir()
            return 0, ""
        if verb == "remote":
            return 0, self.remote + "\n"
        if verb == "rev-parse":
            return 0, self.head + "\n"
        if verb == "status":
            return 0, self.dirty
        if verb == "fetch":
            return 0, ""
        if verb == "checkout":
            self.head = self.checkout_lands_on
            return 0, ""
        raise AssertionError(f"unexpected git call {args}")

    def verbs(self) -> list[str]:
        return [c[0] for c in self.calls]


class FakeNet:
    def __init__(self, payload: bytes = BINARY):
        self.payload = payload
        self.urls: list[str] = []

    def __call__(self, url):
        self.urls.append(url)
        return self.payload


@pytest.fixture
def env(tmp_path, monkeypatch):
    git, net = FakeGit(), FakeNet()
    monkeypatch.setattr(setup, "run_git", git)
    monkeypatch.setattr(setup, "download", net)
    monkeypatch.setitem(setup.ASSETS, "win32", setup.Asset("fake.pyd", "grid_router.pyd", hashlib.sha256(BINARY).hexdigest()))
    monkeypatch.setitem(setup.ASSETS, "nohash", setup.Asset("fake.so", "grid_router.so", None))
    return tmp_path, git, net


def run(tmp_path, *extra, platform="win32"):
    return setup.main(["--root", str(tmp_path), *extra], platform_key=platform)


def test_default_destination_is_the_gitignored_tools_folder(tmp_path):
    assert setup.destination(tmp_path) == tmp_path / ".tools" / "KiCadRoutingTools"


def test_fresh_run_clones_checks_out_the_pin_and_installs_the_verified_binary(env, capsys):
    tmp_path, git, net = env
    assert run(tmp_path) == 0
    assert git.verbs()[0] == "clone" and "checkout" in git.verbs()
    assert git.head == PIN
    binary = tmp_path / ".tools" / "KiCadRoutingTools" / "rust_router" / "grid_router.pyd"
    assert binary.read_bytes() == BINARY
    assert net.urls == [f"{setup.REPO_URL}/releases/download/{setup.TAG}/fake.pyd"]
    out = capsys.readouterr().out
    assert "cloned" in out and PIN in out and "downloaded" in out and "sha-256" in out.lower()


def test_second_run_changes_nothing(env, capsys):
    tmp_path, git, net = env
    assert run(tmp_path) == 0
    binary = tmp_path / ".tools" / "KiCadRoutingTools" / "rust_router" / "grid_router.pyd"
    mtime = binary.stat().st_mtime_ns
    git.calls.clear()
    net.urls.clear()
    capsys.readouterr()
    assert run(tmp_path) == 0
    assert set(git.verbs()) <= {"rev-parse", "status", "remote"}, git.calls  # read-only
    assert net.urls == []
    assert binary.stat().st_mtime_ns == mtime
    out = capsys.readouterr().out
    assert "unchanged" in out and "already" in out.lower()


def test_existing_checkout_on_another_commit_is_moved_to_the_pin(env):
    tmp_path, git, _ = env
    (tmp_path / ".tools" / "KiCadRoutingTools" / ".git").mkdir(parents=True)
    assert run(tmp_path) == 0
    assert "clone" not in git.verbs()
    assert git.verbs().index("fetch") < git.verbs().index("checkout")
    assert git.head == PIN


def test_a_checkout_that_does_not_land_on_the_pinned_sha_is_refused(env, capsys):
    tmp_path, git, net = env
    git.checkout_lands_on = OTHER
    assert run(tmp_path) == 2
    assert net.urls == [], "no binary is fetched for a wrong checkout"
    err = capsys.readouterr().err
    assert PIN in err and OTHER in err


def test_a_dirty_checkout_is_never_moved(env, capsys):
    tmp_path, git, net = env
    (tmp_path / ".tools" / "KiCadRoutingTools" / ".git").mkdir(parents=True)
    git.dirty = " M py_router/route.py\n"
    assert run(tmp_path) == 2
    assert "checkout" not in git.verbs() and net.urls == []
    assert "uncommitted" in capsys.readouterr().err.lower()


def test_a_folder_that_is_not_a_git_checkout_is_not_overwritten(env, capsys):
    tmp_path, git, _ = env
    dest = tmp_path / ".tools" / "KiCadRoutingTools"
    dest.mkdir(parents=True)
    (dest / "keep.txt").write_text("x", encoding="utf-8")
    assert run(tmp_path) == 2
    assert git.calls == [] and (dest / "keep.txt").exists()
    assert "not a git checkout" in capsys.readouterr().err


def test_a_download_with_the_wrong_hash_is_refused_and_nothing_is_written(env, capsys):
    tmp_path, _, net = env
    net.payload = b"tampered"
    assert run(tmp_path) == 2
    rust = tmp_path / ".tools" / "KiCadRoutingTools" / "rust_router"
    assert list(rust.iterdir()) == [], "neither the binary nor a temporary file is left"
    err = capsys.readouterr().err
    assert "sha-256" in err.lower() and hashlib.sha256(b"tampered").hexdigest() in err


def test_an_installed_binary_with_the_wrong_hash_is_replaced_by_the_verified_one(env):
    tmp_path, _, net = env
    assert run(tmp_path) == 0
    binary = tmp_path / ".tools" / "KiCadRoutingTools" / "rust_router" / "grid_router.pyd"
    binary.write_bytes(b"corrupt")
    net.urls.clear()
    assert run(tmp_path) == 0
    assert binary.read_bytes() == BINARY and len(net.urls) == 1


def test_a_platform_without_a_recorded_hash_cannot_be_verified_and_is_refused(env, capsys):
    tmp_path, _, net = env
    assert run(tmp_path, platform="nohash") == 2
    assert net.urls == []
    assert "cannot verify" in capsys.readouterr().err.lower()


def test_the_unverified_flag_installs_and_says_so(env, capsys):
    tmp_path, _, net = env
    assert run(tmp_path, "--allow-unverified-binary", platform="nohash") == 0
    binary = tmp_path / ".tools" / "KiCadRoutingTools" / "rust_router" / "grid_router.so"
    assert binary.read_bytes() == BINARY
    assert "not verified" in capsys.readouterr().out.lower()


def test_a_platform_with_no_published_binary_is_refused(env, capsys):
    tmp_path, _, net = env
    assert run(tmp_path, platform="plan9") == 2
    assert net.urls == [] and "no prebuilt binary" in capsys.readouterr().err.lower()


# --- the values recorded in the repository --------------------------------------------------------

def test_the_script_pins_the_same_commit_as_the_cli_snapshot():
    snapshot = json.loads((ROOT / "src/boardproof/routers/krt_cli_snapshot.json").read_text(encoding="utf-8"))
    assert setup.PINNED_SHA == snapshot["router_commit"] == PIN
    assert setup.TAG == "v0.23.8"


def test_the_windows_asset_hash_is_recorded_in_full():
    asset = setup.ASSETS["win32"]
    assert asset.name == "grid_router-windows-x86_64.pyd"
    assert re.fullmatch(r"4bc6e1ec[0-9a-f]{50}fabfc4", asset.sha256)


def test_platforms_without_a_measured_hash_record_none():
    for key in ("darwin-arm64", "darwin-x86_64", "linux-x86_64"):
        assert setup.ASSETS[key].sha256 is None, key


def test_the_script_has_no_force_or_skip_verification_switch():
    text = (ROOT / "scripts" / "setup_krt.py").read_text(encoding="utf-8")
    assert "--force" not in text
    assert "--allow-unverified-binary" in text


# --- where the other pieces name the script -------------------------------------------------------

def test_the_missing_checkout_hint_names_the_script():
    from boardproof.routers import kicad_routing_tools as krt

    assert "scripts/setup_krt.py" in krt.INSTALL_CHECKOUT_HINT
    assert ".tools/KiCadRoutingTools" in krt.INSTALL_CHECKOUT_HINT


def test_discovery_falls_back_to_the_tools_folder_of_the_repository(tmp_path, monkeypatch):
    from boardproof.routers import kicad_routing_tools as krt

    monkeypatch.delenv(krt.ENV_PATH, raising=False)
    monkeypatch.setattr(krt, "_PLUGIN_GLOBS", {})
    monkeypatch.setattr(krt, "REPO_ROOT", tmp_path)
    with pytest.raises(krt.KicadRoutingToolsError, match="setup_krt.py"):
        krt.find_kicad_routing_tools()
    route = tmp_path / ".tools" / "KiCadRoutingTools" / "py_router" / "route.py"
    route.parent.mkdir(parents=True)
    route.write_text("", encoding="utf-8")
    assert krt.find_kicad_routing_tools() == tmp_path / ".tools" / "KiCadRoutingTools"


def test_the_environment_variable_still_wins(tmp_path, monkeypatch):
    from boardproof.routers import kicad_routing_tools as krt

    other = tmp_path / "other"
    (other / "py_router").mkdir(parents=True)
    (other / "py_router" / "route.py").write_text("", encoding="utf-8")
    default = tmp_path / ".tools" / "KiCadRoutingTools" / "py_router"
    default.mkdir(parents=True)
    (default / "route.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(krt, "REPO_ROOT", tmp_path)
    monkeypatch.setenv(krt.ENV_PATH, str(other))
    assert krt.find_kicad_routing_tools() == other


# --- review fixes (issue 64) ----------------------------------------------------------------------

def test_a_dirty_tree_at_the_pin_is_never_reported_unchanged(env, capsys):
    tmp_path, git, net = env
    assert run(tmp_path) == 0
    git.head = PIN
    git.dirty = " M py_router/route.py\n?? stray.txt\n"
    net.urls.clear()
    capsys.readouterr()
    assert run(tmp_path) == 2
    captured = capsys.readouterr()
    assert "unchanged" not in captured.out
    assert "py_router/route.py" in captured.err and "modified" in captured.err.lower()
    assert net.urls == []


@pytest.mark.parametrize("remote", ["https://example.org/other/KiCadRoutingTools", "ssh://github.com/someone/KiCadRoutingTools.git"])
def test_a_checkout_whose_origin_is_not_upstream_is_refused(env, capsys, remote):
    tmp_path, git, net = env
    (tmp_path / ".tools" / "KiCadRoutingTools" / ".git").mkdir(parents=True)
    git.remote = remote
    assert run(tmp_path) == 2
    assert "checkout" not in git.verbs() and net.urls == []
    err = capsys.readouterr().err
    assert "origin" in err and remote in err and setup.REPO_URL in err


@pytest.mark.parametrize("remote", [setup.REPO_URL, setup.REPO_URL + ".git", setup.REPO_URL + "/", setup.REPO_URL.upper()])
def test_the_upstream_url_is_accepted_in_its_usual_spellings(env, remote):
    tmp_path, git, _ = env
    (tmp_path / ".tools" / "KiCadRoutingTools" / ".git").mkdir(parents=True)
    git.remote = remote
    assert run(tmp_path) == 0


def test_the_windows_hash_is_the_full_recorded_value():
    """The real constant, not a faked entry: any one-nibble change turns this red."""
    assert setup.ASSETS["win32"].sha256 == "4bc6e1ec3f6145fb75cbfd091619b6ab29b7fa62cc53cb2d2dc7ba54bdfabfc4"


def test_the_krt_skip_text_names_the_setup_script(monkeypatch):
    import _krt  # tests/ is on sys.path (conftest imports it the same way)

    monkeypatch.delenv(_krt.REQUIRED_FLAG, raising=False)
    with pytest.raises(pytest.skip.Exception) as info:
        _krt.missing("nothing here", {})
    assert "restore the default one with `python scripts/setup_krt.py`" in str(info.value)


def _git_dirs(tmp_path):
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {(main / '.git' / 'worktrees' / 'wt').as_posix()}\n", encoding="utf-8")
    route = main / ".tools" / "KiCadRoutingTools" / "py_router" / "route.py"
    route.parent.mkdir(parents=True)
    route.write_text("", encoding="utf-8")
    return main, wt


def test_discovery_in_a_linked_worktree_finds_the_main_checkouts_router(tmp_path, monkeypatch):
    from boardproof.routers import kicad_routing_tools as krt

    main, wt = _git_dirs(tmp_path)
    monkeypatch.delenv(krt.ENV_PATH, raising=False)
    monkeypatch.setattr(krt, "_PLUGIN_GLOBS", {})
    monkeypatch.setattr(krt, "REPO_ROOT", wt)
    assert krt.find_kicad_routing_tools() == main / ".tools" / "KiCadRoutingTools"


def test_the_tool_text_names_the_discovery_order():
    import ast

    tree = ast.parse((ROOT / "src/boardproof/toolsets/routing.py").read_text(encoding="utf-8"))
    docs = [n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)
            and "KICAD_ROUTING_TOOLS_PATH" in n.value and "PCM plugin dir" in n.value]
    assert docs, "the routing tool text names the discovery"
    text = " ".join(docs[0].split())
    order = ["KRT is discovered via KICAD_ROUTING_TOOLS_PATH",
             "else .tools/KiCadRoutingTools of the main checkout (restored by `python scripts/setup_krt.py`)",
             "else KiCad PCM plugin dir"]
    positions = [text.find(x) for x in order]
    assert all(p >= 0 for p in positions), positions
    assert positions == sorted(positions)


def test_the_krt_checkout_fixture_docstring_names_the_discovery_order():
    import ast

    tree = ast.parse((ROOT / "tests/conftest.py").read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "krt_checkout")
    text = " ".join(ast.get_docstring(fn).split())
    assert "``KICAD_ROUTING_TOOLS_PATH``, else ``.tools/KiCadRoutingTools``, else the KiCad plugin directory" in text


def test_an_ordinary_test_does_not_see_the_default_router_folder_of_this_machine():
    """Issue 64: a real .tools/KiCadRoutingTools must not change what tests of 'KRT missing' or of the
    platform glob see; conftest points the default-folder discovery at an empty directory."""
    from boardproof.routers import kicad_routing_tools as krt

    real_root = Path(krt.__file__).resolve().parents[3]
    assert krt.REPO_ROOT.resolve() != real_root
    assert not any(c.exists() for c in krt.default_checkout_candidates(krt.REPO_ROOT))
