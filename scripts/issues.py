"""Tickets as GitHub issues: a thin, checked wrapper around the `gh` CLI (docs/WORKFLOW.md).

    python scripts/issues.py [--repo OWNER/NAME] [--no-identity-check] <command> ...

Commands:
  create --title T --body-file F [--label L]... [--blocked-by N]...   create one issue
  comment N --body-file F                                              add a comment
  close N [--comment-file F]                                           close, optionally with a comment
  list [--label L] [--state open|closed|all]                           list issues
  show N                                                               one issue with its comments
  label-sync                                                           create or update the labels
  create-batch DIR [--dry-run]                                         create the issues of a folder
                                                                       in dependency order

Every title, body, comment and label the script is about to send is first scanned with
`scripts/check_publishable.py` (its built-in patterns always; the owner's identity patterns from the
file named by the environment variable BOARDPROOF_FORBIDDEN_FILE). A hit refuses the whole
command with exit code 2 and sends nothing; the message names the pattern and the place, never the
matched text. A missing variable means the identity check was NOT done, which is not a pass: every
writing command (create, comment, close, create-batch) then refuses with exit 2, unless the global
flag --no-identity-check or BOARDPROOF_ALLOW_UNCHECKED=1 is given; the run then prints a loud
WARNING on stderr and on stdout, and the built-in patterns still apply.
The repository must be named by --repo or BOARDPROOF_REPO; without one every command refuses with
exit 2 (gh would otherwise take the repository of the current directory, which may be an archive).

Exit codes: 0 done; 1 gh failed; 2 refused (a hit, a bad input, an unreadable pattern file);
3 `gh` or the repository is not reachable (said plainly; nothing was sent).

`create-batch` first checks that every label it uses exists in the repository (run
`label-sync` first) and creates nothing unless all texts pass. It reads one markdown file per issue. The file starts with header lines (`Id:`,
`Title:`, `Labels:` comma separated, `Blocked by:` comma separated), then a blank line, then the
body. `Id` is the issue's old id (for example `build-111`). A `Blocked by` entry is another file's
`Id` or an existing `#N`; in the body, `#ref:<Id>` and `Blocked by <Id>` become the new number. The
files are created in dependency order; a cycle or an unknown id refuses the whole batch before any
issue is created. It prints `<Id> -> #<number>` for every issue.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

EXIT_OK, EXIT_GH_FAILED, EXIT_REFUSED, EXIT_UNREACHABLE = 0, 1, 2, 3

LABELS: list[tuple[str, str, str]] = [
    ("map", "5319e7", "A map: one issue with a task list of its tickets"),
    ("build", "0e8a16", "Ticket: code or docs change, built test-first"),
    ("research", "1d76db", "Ticket: a question answered from primary sources"),
    ("decision", "fbca04", "Ticket: a decision to take and record"),
    ("task", "c5def5", "Ticket: a plain task"),
    ("critical", "b60205", "Critical class: needs an adversarial review"),
    ("parked", "cccccc", "Parked: not scheduled"),
    ("needs-owner", "d93f0b", "Waits for the owner"),
]


class Refused(Exception):
    pass


class Unreachable(Exception):
    pass


def _load_checker():
    here = Path(__file__).resolve().parent
    spec = importlib.util.spec_from_file_location("check_publishable", here / "check_publishable.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("check_publishable", module)
    spec.loader.exec_module(module)
    return module


_UNCHECKED = {"allowed": False}


def identity_check_mode() -> str:
    """Return "checked" (pattern file set) or "unchecked" (explicit opt-out); else raise Refused."""
    if os.environ.get("BOARDPROOF_FORBIDDEN_FILE"):
        return "checked"
    if _UNCHECKED["allowed"] or os.environ.get("BOARDPROOF_ALLOW_UNCHECKED") == "1":
        return "unchecked"
    raise Refused(
        "refused: BOARDPROOF_FORBIDDEN_FILE is not set, so the identity check cannot be done "
        "(a missing check is not a pass). Set it to the private pattern file, or pass "
        "--no-identity-check (or BOARDPROOF_ALLOW_UNCHECKED=1) to send with the built-in "
        "patterns only. Nothing was sent.")


def check_text(label: str, text: str) -> None:
    """Raise Refused when the text hits a built-in pattern or the forbidden file's patterns."""
    checker = _load_checker()
    forbidden = []
    path = os.environ.get("BOARDPROOF_FORBIDDEN_FILE")
    if path:
        try:
            forbidden = checker.load_forbidden(Path(path))
        except checker.CannotRun as exc:
            raise Refused(f"cannot run the publishability check: {exc}") from exc
    found = []
    for lineno, line in enumerate(text.splitlines() or [""], start=1):
        for name, _span in checker._scan_line(line, forbidden):
            found.append(f"{label} line {lineno}: [{name}]")
    if found:
        raise Refused("refused, not publishable (nothing was sent):\n  " + "\n  ".join(found))


class Gh:
    def __init__(self, repo: str | None):
        self.repo = repo
        self.exe = shutil.which("gh")

    def run(self, args: list[str], stdin: str | None = None, scoped: bool = True):
        if self.exe is None:
            raise Unreachable("the `gh` CLI was not found on PATH")
        argv = [self.exe, *args]
        if scoped and self.repo:
            argv += ["--repo", self.repo]
        return subprocess.run(argv, input=stdin, capture_output=True, text=True, encoding="utf-8")

    def require_repo(self) -> None:
        if not self.repo:
            raise Refused("refused: no repository named. Pass --repo OWNER/NAME or set "
                          "BOARDPROOF_REPO (gh would otherwise use the repository of the current "
                          "directory, which may be the wrong one). Nothing was sent.")

    def preflight(self) -> None:
        self.require_repo()
        if self.exe is None:
            raise Unreachable("the `gh` CLI was not found on PATH")
        args = ["repo", "view"] + ([self.repo] if self.repo else []) + ["--json", "name"]
        done = self.run(args, scoped=False)
        if done.returncode != 0:
            reason = (done.stderr or "").strip().splitlines()[:1]
            raise Unreachable("the repository is not reachable" + (f" ({reason[0]})" if reason else ""))


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise Refused(f"cannot read {path}: {type(exc).__name__}") from exc


def _emit(done) -> int:
    if done.stdout:
        sys.stdout.write(done.stdout)
    if done.returncode != 0:
        sys.stderr.write(done.stderr or "")
        return EXIT_GH_FAILED
    return EXIT_OK


def _body_with_blocked(body: str, blocked: list[str]) -> str:
    lines = [f"Blocked by #{str(n).lstrip('#')}" for n in blocked]
    return body.rstrip("\n") + ("\n\n" + "\n".join(lines) if lines else "") + "\n"


def _create(gh: Gh, title: str, body: str, labels: list[str], blocked: list[str]) -> tuple[int, int | None]:
    full = _body_with_blocked(body, blocked)
    args = ["issue", "create", "--title", title, "--body-file", "-"]
    for label in labels:
        args += ["--label", label]
    done = gh.run(args, stdin=full)
    code = _emit(done)
    match = re.search(r"/issues/(\d+)", done.stdout or "")
    return code, int(match.group(1)) if match else None


def _start_write(gh: Gh) -> None:
    """Repository named and identity check possible, else Refused; announce an opt-out loudly."""
    gh.require_repo()
    if identity_check_mode() == "unchecked":
        warning = ("WARNING: identity check skipped (no BOARDPROOF_FORBIDDEN_FILE); only the "
                   "built-in patterns were applied.")
        print(warning)
        print(warning, file=sys.stderr)


_BLOCKED_BY = re.compile(r"#?[0-9]+")


def cmd_create(gh: Gh, a) -> int:
    for value in a.blocked_by:
        if not _BLOCKED_BY.fullmatch(value):
            raise Refused(f"--blocked-by takes an issue number (3 or #3), not {value!r}")
    _start_write(gh)
    body = _read(a.body_file)
    check_text("title", a.title)
    check_text("body", body)
    for label in a.label:
        check_text("label", label)
    gh.preflight()
    return _create(gh, a.title, body, a.label, a.blocked_by)[0]


def cmd_comment(gh: Gh, a) -> int:
    _start_write(gh)
    body = _read(a.body_file)
    check_text("comment", body)
    gh.preflight()
    return _emit(gh.run(["issue", "comment", str(a.number), "--body-file", "-"], stdin=body))


def cmd_close(gh: Gh, a) -> int:
    _start_write(gh)
    args = ["issue", "close", str(a.number)]
    if a.comment_file:
        text = _read(a.comment_file)
        check_text("comment", text)
        args += ["--comment", text]
    gh.preflight()
    return _emit(gh.run(args))


def cmd_list(gh: Gh, a) -> int:
    gh.preflight()
    args = ["issue", "list", "--state", a.state, "--limit", "200",
            "--json", "number,title,state,labels"]
    if a.label:
        args += ["--label", a.label]
    done = gh.run(args)
    if done.returncode != 0:
        sys.stderr.write(done.stderr or "")
        return EXIT_GH_FAILED
    for item in json.loads(done.stdout or "[]"):
        names = ",".join(label["name"] for label in item.get("labels", []))
        print(f"#{item['number']}\t{item['state'].lower()}\t{names}\t{item['title']}")
    return EXIT_OK


def cmd_show(gh: Gh, a) -> int:
    gh.preflight()
    return _emit(gh.run(["issue", "view", str(a.number), "--comments"]))


def cmd_label_sync(gh: Gh, a) -> int:
    gh.preflight()
    for name, color, description in LABELS:
        done = gh.run(["label", "create", name, "--color", color,
                       "--description", description, "--force"])
        if done.returncode != 0:
            sys.stderr.write(done.stderr or "")
            return EXIT_GH_FAILED
        print(f"label {name}")
    return EXIT_OK


_ID = r"[A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*"
_ID_REF = rf"#ref:({_ID})"
_ID_BLOCKED = rf"Blocked by ({_ID})"
_HEADER = re.compile(r"^(Id|Title|Labels|Blocked by):\s*(.*)$")


def parse_issue_file(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    head, _, body = text.partition("\n\n")
    fields: dict[str, str] = {}
    for line in head.splitlines():
        m = _HEADER.match(line)
        if not m:
            raise Refused(f"{path.name}: header line not understood: {line[:40]!r}")
        fields[m.group(1)] = (fields.get(m.group(1), "") + "," + m.group(2)).strip(",")
    for key in ("Id", "Title"):
        if not fields.get(key):
            raise Refused(f"{path.name}: header lacks {key}")

    def split(value: str) -> list[str]:
        return [p.strip() for p in value.split(",") if p.strip()]

    return {"id": fields["Id"], "title": fields["Title"], "labels": split(fields.get("Labels", "")),
            "blocked": split(fields.get("Blocked by", "")), "body": body, "file": path.name}


def plan_batch(directory: Path) -> list[dict]:
    files = sorted(directory.glob("*.md"))
    if not files:
        raise Refused(f"no .md file in {directory}")
    items = [parse_issue_file(f) for f in files]
    ids = {i["id"] for i in items}
    if len(ids) != len(items):
        raise Refused("two files carry the same Id")
    for item in items:
        refs = set(re.findall(_ID_REF, item["body"]))
        for word in re.findall(_ID_BLOCKED, item["body"]):
            # Prose ("Blocked by every other ...") is not a dependency; an unknown id-shaped token
            # (a hyphen or a digit in it) is an error.
            if word in ids or re.search(r"[-0-9]", word):
                refs.add(word)
        item["deps"] = [d for d in item["blocked"] if not d.startswith("#")] + sorted(refs)
        for dep in item["deps"]:
            if dep not in ids:
                raise Refused(f"{item['file']}: unknown id {dep!r}")
    ordered: list[dict] = []
    done: set[str] = set()
    pending = items[:]
    while pending:
        ready = [i for i in pending if all(d in done for d in i["deps"])]
        if not ready:
            raise Refused("dependency cycle among: " + ", ".join(i["id"] for i in pending))
        ready.sort(key=lambda i: (i["id"].startswith("map"), i["id"]))
        nxt = ready[0]
        ordered.append(nxt)
        done.add(nxt["id"])
        pending.remove(nxt)
    return ordered


def _existing_labels(gh: Gh) -> set[str]:
    done = gh.run(["label", "list", "--limit", "200", "--json", "name"])
    if done.returncode != 0:
        sys.stderr.write(done.stderr or "")
        raise Refused("refused: the labels of the repository could not be listed. Nothing was sent.")
    return {item["name"] for item in json.loads(done.stdout or "[]")}


def cmd_create_batch(gh: Gh, a) -> int:
    _start_write(gh)
    ordered = plan_batch(Path(a.directory))
    for item in ordered:
        check_text(f"{item['file']} title", item["title"])
        check_text(f"{item['file']} body", item["body"])
        for label in item["labels"]:
            check_text(f"{item['file']} label", label)
    if a.dry_run:
        for item in ordered:
            print(f"{item['id']}\t{','.join(item['labels'])}\t{item['title']}")
        return EXIT_OK
    gh.preflight()
    used = {label for item in ordered for label in item["labels"]}
    missing = sorted(used - _existing_labels(gh))
    if missing:
        raise Refused("refused: labels missing in the repository: " + ", ".join(missing)
                      + ". Run `label-sync` first. Nothing was created.")
    numbers: dict[str, int] = {}
    for item in ordered:
        body = re.sub(_ID_REF, lambda m: f"#{numbers[m.group(1)]}", item["body"])
        body = re.sub(_ID_BLOCKED, lambda m: f"Blocked by #{numbers[m.group(1)]}"
                      if m.group(1) in numbers else m.group(0), body)
        blocked = [str(numbers[d]) if d in numbers else d.lstrip("#") for d in item["blocked"]]
        blocked = [b for b in blocked if f"Blocked by #{b}" not in body]
        code, number = _create(gh, item["title"], body, item["labels"], blocked)
        if code != 0 or number is None:
            sys.stderr.write(f"stopped at {item['id']}; created so far: {numbers}\n")
            return EXIT_GH_FAILED
        numbers[item["id"]] = number
        print(f"{item['id']} -> #{number}")
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--repo", default=os.environ.get("BOARDPROOF_REPO"))
    p.add_argument("--no-identity-check", action="store_true",
                   help="send without the private pattern file (loud warning; built-in patterns apply)")
    sub = p.add_subparsers(dest="command", required=True)
    c = sub.add_parser("create")
    c.add_argument("--title", required=True)
    c.add_argument("--body-file", required=True)
    c.add_argument("--label", action="append", default=[])
    c.add_argument("--blocked-by", action="append", default=[])
    c.set_defaults(fn=cmd_create)
    c = sub.add_parser("comment")
    c.add_argument("number", type=int)
    c.add_argument("--body-file", required=True)
    c.set_defaults(fn=cmd_comment)
    c = sub.add_parser("close")
    c.add_argument("number", type=int)
    c.add_argument("--comment-file")
    c.set_defaults(fn=cmd_close)
    c = sub.add_parser("list")
    c.add_argument("--label")
    c.add_argument("--state", default="open", choices=["open", "closed", "all"])
    c.set_defaults(fn=cmd_list)
    c = sub.add_parser("show")
    c.add_argument("number", type=int)
    c.set_defaults(fn=cmd_show)
    c = sub.add_parser("label-sync")
    c.set_defaults(fn=cmd_label_sync)
    c = sub.add_parser("create-batch")
    c.add_argument("directory")
    c.add_argument("--dry-run", action="store_true")
    c.set_defaults(fn=cmd_create_batch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    _UNCHECKED["allowed"] = args.no_identity_check
    try:
        return args.fn(Gh(args.repo), args)
    except Refused as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_REFUSED
    except Unreachable as exc:
        print(f"cannot reach GitHub: {exc}. Nothing was sent.", file=sys.stderr)
        return EXIT_UNREACHABLE


if __name__ == "__main__":
    sys.exit(main())
