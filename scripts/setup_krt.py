"""Restore the KiCadRoutingTools (KRT) checkout and its prebuilt router binary (issue 64).

    python scripts/setup_krt.py [--root DIR] [--dest DIR] [--allow-unverified-binary]

Puts the router into ``<root>/.tools/KiCadRoutingTools`` (gitignored, like the other tools):

1. clones the upstream repository when the folder is absent, or moves an existing clean checkout to
   the pinned full sha (``PINNED_SHA``, the release commit of ``TAG``, the same commit
   ``src/kicad_mcp/routers/krt_cli_snapshot.json`` pins) and reads HEAD back: a checkout that does not
   land on the sha is refused;
2. fetches the prebuilt ``grid_router`` binary of the release for this platform and checks its SHA-256
   against the value recorded below before anything is written; a wrong hash is refused and leaves no
   file. A platform with no recorded hash cannot be verified and is refused unless
   ``--allow-unverified-binary`` is given. There is no other override.

Running it twice changes nothing: a checkout already at the pin and a binary that already has the
recorded hash are reported as unchanged and nothing is fetched. A checkout with local changes (also one at the pin), one whose origin is
not the upstream URL, or a folder that is not a git checkout is refused and never touched. The binary is written to a temporary sibling and moved with
``os.replace``.

The prebuilt binary of release v0.23.8 reports version 0.23.7 because the crate version was not
bumped for that release; the Rust code is identical to v0.23.7. That is not a mismatch.

Set ``KICAD_ROUTING_TOOLS_PATH`` to the printed folder, or leave it unset: the batch gate and the
router discovery use ``.tools/KiCadRoutingTools`` of the checkout by default; the variable wins.

Exit codes: 0 done (or already done); 2 refused (message on stderr).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

REPO_URL = "https://github.com/drandyhaas/KiCadRoutingTools"
TAG = "v0.23.8"
PINNED_SHA = "c64c0db25e6cf23afa6769ee05665c7ea22b3ff2"
RELATIVE_DEST = Path(".tools") / "KiCadRoutingTools"


@dataclass(frozen=True)
class Asset:
    """A release asset: its name on the release page, its file name in ``rust_router/`` and its
    SHA-256 (None: no hash was measured for this platform, so it cannot be verified)."""

    name: str
    dst: str
    sha256: str | None


# Hashes are recorded only where they were measured against a binary known to work. The Windows value
# is the SHA-256 of the asset of release v0.23.8, identical to the binary of v0.23.7 (same Rust code).
ASSETS: dict[str, Asset] = {
    "win32": Asset(
        "grid_router-windows-x86_64.pyd",
        "grid_router.pyd",
        "4bc6e1ec3f6145fb75cbfd091619b6ab29b7fa62cc53cb2d2dc7ba54bdfabfc4",
    ),
    "darwin-arm64": Asset("grid_router-macos-arm64.so", "grid_router.so", None),
    "darwin-x86_64": Asset("grid_router-macos-x86_64.so", "grid_router.so", None),
    "linux-x86_64": Asset("grid_router-linux-x86_64.so", "grid_router.so", None),
}


class Refused(Exception):
    """The setup cannot continue; the message is printed on stderr and the exit code is 2."""


def destination(root: Path) -> Path:
    return Path(root) / RELATIVE_DEST


def detect_platform() -> str:
    import platform

    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "win32" if machine in ("amd64", "x86_64") else f"win32-{machine}"
    arch = "arm64" if machine in ("arm64", "aarch64") else machine
    if sys.platform == "darwin":
        return f"darwin-{'x86_64' if arch == 'amd64' else arch}"
    if sys.platform.startswith("linux"):
        return f"linux-{'x86_64' if arch == 'amd64' else arch}"
    return sys.platform


def run_git(args: list[str], cwd: Path | None = None) -> tuple[int, str]:
    """Run git with an argument list (never a shell string); return (exit code, stdout)."""
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        encoding="utf-8", errors="replace", check=False, timeout=900,
    )
    if proc.returncode != 0 and not proc.stdout:
        return proc.returncode, proc.stderr or ""
    return proc.returncode, proc.stdout


def download(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:  # noqa: S310  (fixed https URL)
        return response.read()


def _git(args: list[str], cwd: Path | None = None) -> str:
    code, out = run_git(args, cwd)
    if code != 0:
        raise Refused(f"git {' '.join(args[:2])} failed (exit {code}): {out.strip()[-300:]}")
    return out


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _normal_url(url: str) -> str:
    url = url.strip().lower().rstrip("/")
    return url[:-4] if url.endswith(".git") else url


def ensure_checkout(dest: Path, say) -> None:
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git(["clone", REPO_URL, str(dest)])
        say(f"checkout: cloned {REPO_URL} into {dest}")
        fresh = True
    elif not (dest / ".git").exists():
        raise Refused(f"{dest} exists and is not a git checkout; move it away or choose another --dest")
    else:
        fresh = False

    if not fresh:
        origin = _git(["remote", "get-url", "origin"], dest).strip()
        if _normal_url(origin) != _normal_url(REPO_URL):
            raise Refused(f"{dest} has origin {origin}, not the upstream {REPO_URL}; it is not touched")
    head = _git(["rev-parse", "HEAD"], dest).strip()
    dirty = "" if fresh else _git(["status", "--porcelain"], dest).strip()
    if dirty:
        listed = "; ".join(line.strip() for line in dirty.splitlines()[:10])
        raise Refused(
            f"{dest} has uncommitted changes (modified or untracked: {listed}) and is at {head}; it is not touched"
        )
    if head == PINNED_SHA:
        say(f"checkout: unchanged, already at {PINNED_SHA} ({TAG}), working tree clean")
        return
    if not fresh:
        _git(["fetch", "--tags", "origin"], dest)
    _git(["checkout", "--detach", PINNED_SHA], dest)
    landed = _git(["rev-parse", "HEAD"], dest).strip()
    if landed != PINNED_SHA:
        raise Refused(f"checkout of {dest} is at {landed} after checking out, expected {PINNED_SHA}")
    say(f"checkout: moved from {head} to {PINNED_SHA} ({TAG})")


def ensure_binary(dest: Path, key: str, allow_unverified: bool, say) -> None:
    asset = ASSETS.get(key)
    if asset is None:
        raise Refused(f"no prebuilt binary is published for platform {key}; build the router from source")
    target = dest / "rust_router" / asset.dst
    if asset.sha256 is None:
        if not allow_unverified:
            raise Refused(
                f"cannot verify the binary for platform {key}: no SHA-256 is recorded for {asset.name}. "
                "Nothing was downloaded. Re-run with --allow-unverified-binary to install it anyway."
            )
    elif target.is_file() and _sha256_file(target) == asset.sha256:
        say(f"binary: unchanged, {target.name} already has the recorded SHA-256 {asset.sha256}")
        return
    if asset.sha256 is None and target.is_file():
        say(f"binary: {target.name} already present, not verified (no SHA-256 recorded for {key})")
        return

    url = f"{REPO_URL}/releases/download/{TAG}/{asset.name}"
    data = download(url)
    digest = hashlib.sha256(data).hexdigest()
    if asset.sha256 is not None and digest != asset.sha256:
        raise Refused(
            f"downloaded {asset.name} has SHA-256 {digest}, the recorded value is {asset.sha256}; nothing was written"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + ".part")
    try:
        temp.write_bytes(data)
        if _sha256_file(temp) != digest:
            raise Refused(f"{temp} does not read back as the bytes downloaded; nothing was installed")
        os.replace(temp, target)
    finally:
        if temp.exists():
            temp.unlink()
    state = "SHA-256 verified" if asset.sha256 is not None else "NOT verified (--allow-unverified-binary)"
    say(f"binary: downloaded {asset.name} -> {target}, {state}, SHA-256 {digest}")


def main(argv: list[str] | None = None, *, platform_key: str | None = None) -> int:
    parser = argparse.ArgumentParser(description="Restore the pinned KiCadRoutingTools checkout and binary.")
    parser.add_argument("--root", type=Path, default=ROOT, help="repository root (default: this checkout)")
    parser.add_argument("--dest", type=Path, default=None, help="folder to use instead of <root>/.tools/KiCadRoutingTools")
    parser.add_argument("--allow-unverified-binary", action="store_true",
                        help="install the binary of a platform with no recorded SHA-256 (it stays unverified)")
    args = parser.parse_args(argv)
    key = platform_key if platform_key is not None else detect_platform()
    dest = args.dest if args.dest is not None else destination(args.root)
    try:
        ensure_checkout(dest, print)
        ensure_binary(dest, key, args.allow_unverified_binary, print)
    except Refused as exc:
        print(f"setup_krt: refused: {exc}", file=sys.stderr)
        return 2
    print(f"setup_krt: done. KRT {TAG} at {dest}. KICAD_ROUTING_TOOLS_PATH may stay unset (default) or name this folder.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
