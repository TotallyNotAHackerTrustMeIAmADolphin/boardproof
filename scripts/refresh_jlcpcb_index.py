"""Regenerate the vendored JLCPCB part-tier index from the library submodule (publish-prep ticket 03b).

    python scripts/refresh_jlcpcb_index.py [--library DIR] [--commit SHA] [--index FILE] [--dry-run]
                                           [--sample N]

Reads the symbol libraries of CDFER/JLCPCB-Kicad-Library (default: the git submodule at
``third_party/JLCPCB-Kicad-Library``) through ``sourcing.jlcpcb_basic.build_index``, prints how the
index changes (commit, part counts, tier moves with a sample, parts added or removed) and rewrites
``src/kicad_mcp/sourcing/data/jlcpcb_basic_index.json``. ``Stock`` and ``Price`` are never read into
the index: upstream rewrites them daily. The library is only read, never modified or executed.

Commit: with the default library the pinned commit is the submodule's gitlink in this repository
(``git rev-parse HEAD:third_party/JLCPCB-Kicad-Library``), and a checkout that differs from it is
refused. With ``--library`` you must name the commit with ``--commit`` (40 hex digits).

The new index is written to a temporary sibling, read back and compared with what was built, and
only then moved over the old one with ``os.replace``. A mismatch ends with ``write_not_verified``
and leaves the old index untouched.

A new index with fewer than 80% of the old parts is refused (``index_would_shrink``) unless
``--allow-shrink`` is given: upstream commit de4e6c4 (2026-09-21) emptied 17 of the 20 symbol
libraries, and following it would have turned 868 known Basic and Preferred parts into "unknown".

Exit codes: 0 done (or dry run done); 2 the refresh could not run (library missing or empty, no
commit, no parts, shrink refused, write not verified).
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
# Use this checkout's own package, not whichever copy the interpreter has installed.
sys.path.insert(0, str(ROOT / "src"))

# importlib, not an import statement: the path above must come first.
jlc = importlib.import_module("kicad_mcp.sourcing.jlcpcb_basic")

DEFAULT_LIBRARY = ROOT / jlc.SUBMODULE_RELATIVE_PATH
_SHA = re.compile(r"^[0-9a-f]{40}$")
#: A new index below this fraction of the old part count is refused without --allow-shrink.
#: Upstream commit de4e6c4 (2026-09-21) emptied 17 of the 20 symbol libraries: 889 parts became 21.
SHRINK_LIMIT = 0.8


def _key(part: dict) -> str:
    return part.get("lcsc") or f"{part.get('symbol_lib')}/{part.get('symbol')}"


def diff_payloads(old: dict | None, new: dict) -> dict:
    """Compare two index payloads by LCSC number. ``old`` may be None (no index yet)."""
    old_parts = {_key(p): p for p in (old or {}).get("parts", [])}
    new_parts = {_key(p): p for p in new.get("parts", [])}
    added = sorted(set(new_parts) - set(old_parts))
    removed = sorted(set(old_parts) - set(new_parts))
    tier_changes = [
        (k, old_parts[k]["tier"], new_parts[k]["tier"])
        for k in sorted(set(old_parts) & set(new_parts))
        if old_parts[k].get("tier") != new_parts[k].get("tier")
    ]
    other_changes = [
        k
        for k in sorted(set(old_parts) & set(new_parts))
        if old_parts[k].get("tier") == new_parts[k].get("tier")
        and {f: v for f, v in old_parts[k].items()} != {f: v for f, v in new_parts[k].items()}
    ]
    return {
        "old_commit": ((old or {}).get("source") or {}).get("commit"),
        "new_commit": (new.get("source") or {}).get("commit"),
        "old_counts": (old or {}).get("counts") or {},
        "new_counts": new.get("counts") or {},
        "added": added,
        "removed": removed,
        "tier_changes": tier_changes,
        "other_changes": other_changes,
        "new_parts": new_parts,
        "old_parts": old_parts,
    }


def format_diff(diff: dict, sample: int) -> str:
    def short(c: str | None) -> str:
        return (c or "none")[:7]

    lines = [f"commit: {short(diff['old_commit'])} -> {short(diff['new_commit'])}"]
    oc, nc = diff["old_counts"], diff["new_counts"]
    for name in ("total", "basic", "preferred", "extended"):
        lines.append(f"{'parts' if name == 'total' else name}: {oc.get(name, 0)} -> {nc.get(name, 0)}")
    changes = diff["tier_changes"]
    lines.append(f"tier changes: {len(changes)}")
    for (a, b), n in sorted(Counter((a, b) for _, a, b in changes).items()):
        lines.append(f"  {a} -> {b}: {n}")
    for key, a, b in changes[:sample]:
        part = diff["new_parts"][key]
        lines.append(f"    {key} {part.get('mpn')}: {a} -> {b}")
    if len(changes) > sample:
        lines.append(f"    ... and {len(changes) - sample} more")
    for label in ("added", "removed"):
        keys = diff[label]
        lines.append(f"{label}: {len(keys)}")
        source = diff["new_parts"] if label == "added" else diff["old_parts"]
        for key in keys[:sample]:
            part = source[key]
            lines.append(f"    {key} {part.get('mpn')} ({part.get('tier')})")
        if len(keys) > sample:
            lines.append(f"    ... and {len(keys) - sample} more")
    lines.append(f"other field changes (same tier): {len(diff['other_changes'])}")
    return "\n".join(lines)


def _fail(message: str) -> int:
    print(f"refresh_jlcpcb_index: {message}", file=sys.stderr)
    return 2


def _gitlink_commit() -> tuple[str | None, str]:
    """(pinned commit, problem). The commit comes from this repository's own gitlink."""
    git = ["git", "-C", str(ROOT)]
    pinned = subprocess.run(
        [*git, "rev-parse", f"HEAD:{jlc.SUBMODULE_RELATIVE_PATH}"], capture_output=True, text=True, check=False
    )
    if pinned.returncode != 0 or not _SHA.match(pinned.stdout.strip()):
        return None, f"no committed gitlink at {jlc.SUBMODULE_RELATIVE_PATH}: {pinned.stderr.strip()}"
    status = subprocess.run(
        [*git, "submodule", "status", "--", jlc.SUBMODULE_RELATIVE_PATH], capture_output=True, text=True, check=False
    )
    marker = status.stdout[:1]
    if marker == "+":
        return None, "the submodule checkout differs from the pinned commit; run `git submodule update` first"
    if marker == "-":
        return None, f"the submodule is not initialised; fetch it with: {jlc.SUBMODULE_FETCH_COMMAND}"
    return pinned.stdout.strip(), ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--library", type=Path, default=None, help="library checkout (default: the submodule)")
    parser.add_argument("--commit", default=None, help="commit of the library checkout (40 hex digits)")
    parser.add_argument("--index", type=Path, default=None, help="index file to rewrite")
    parser.add_argument("--dry-run", action="store_true", help="print the diff, write nothing")
    parser.add_argument("--allow-shrink", action="store_true",
                        help=f"accept a new index with fewer than {SHRINK_LIMIT:.0%} of the old parts")
    parser.add_argument("--sample", type=int, default=10, metavar="N", help="parts to name per list (default 10)")
    args = parser.parse_args(argv)

    library = args.library if args.library is not None else DEFAULT_LIBRARY
    commit = args.commit
    if commit is not None and not _SHA.match(commit):
        return _fail("--commit must be a full 40-digit lowercase hex commit id")
    if args.library is None and commit is None:
        commit, problem = _gitlink_commit()
        if commit is None:
            return _fail(problem)
    if commit is None:
        return _fail("with --library you must also pass --commit (the index records its source commit)")

    if _problem := jlc._source_problem("submodule", library):
        return _fail(_problem)

    built = jlc.build_index(library, commit=commit)
    if built.get("status") != "ok":
        return _fail(f"the library gave no usable index: {built.get('reason')}")

    target = args.index if args.index is not None else jlc.VENDORED_INDEX_PATH
    old = None
    if target.is_file():
        try:
            old = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            print(f"note: the existing index cannot be read ({type(exc).__name__}); diffing against none")
    print(format_diff(diff_payloads(old, built), max(args.sample, 0)))
    old_total = (old or {}).get("counts", {}).get("total", 0)
    if not args.allow_shrink and built["counts"]["total"] < SHRINK_LIMIT * old_total:
        return _fail(
            f"index_would_shrink: {old_total} -> {built['counts']['total']} parts (below {SHRINK_LIMIT:.0%}). "
            "The library at this commit may be broken upstream; nothing written. "
            "Pass --allow-shrink if the loss is real."
        )
    if args.dry_run:
        print("dry run: nothing written")
        return 0

    tmp = target.with_name(target.name + f".{os.getpid()}.tmp")
    try:
        written = jlc.write_index(built, tmp)
        verified = False
        if written.get("status") == "ok":
            back = json.loads(tmp.read_text(encoding="utf-8"))
            verified = (
                back.get("parts") == built["parts"]
                and (back.get("source") or {}).get("commit") == commit
                and back.get("counts") == built["counts"]
            )
        if not verified:
            return _fail("write_not_verified: the index read back differs from the one built; nothing replaced")
        os.replace(tmp, target)
    except (OSError, ValueError) as exc:
        return _fail(f"write_not_verified: {type(exc).__name__}: {exc}")
    finally:
        tmp.unlink(missing_ok=True)
    print(f"wrote {target} ({built['counts']['total']} parts, commit {commit[:7]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
