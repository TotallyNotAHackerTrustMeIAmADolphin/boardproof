# Workflow: planning and tickets are GitHub issues

Everything planned, decided or reviewed in this project is a GitHub issue of this repository. There
is no private planning folder. Write every issue and every comment so that it may become public:
facts only, no real name, no machine path, no candid judgement of a person or of another project.
The helper `scripts/issues.py` refuses a text that `scripts/check_publishable.py` flags (see
"The helper" below).

## Maps and tickets

- **Map**: one issue labelled `map`. Its body states the destination (what "done" means for the
  effort), the decisions so far (one line each, newest last) and a task list of its tickets
  (`- [ ] #12 short title`). A map is closed when its destination is reached.
- **Ticket**: one issue. Its body holds the question or the change, the acceptance list (the
  contract of whoever works it), the files it may touch for a build ticket, and
  `Blocked by #n` lines, one per blocking issue. A ticket is ready when every issue it is blocked
  by is closed.
- **Type labels** (exactly one): `build` (code or docs change, built test-first), `research` (a
  question answered from primary sources), `decision` (something to decide and record), `task`.
- **Flag labels**: `critical` (the change touches critical-class text or a gate: it needs an
  adversarial review before it is merged), `parked` (not scheduled; kept so the work is not lost),
  `needs-owner` (waits for the owner).
- **Status** is open or closed, nothing else. A ticket is closed when its Answer is posted and, for
  a build ticket, the branch is merged.
- **Old ids.** Code comments and older docs cite tickets as "build ticket 112" or "research ticket
  3". An issue migrated from the old planning records starts with "formerly build ticket 112" so
  that such a reference still resolves.

## Comments are the record

- **Answer**: when the work is done, the builder posts one comment: the reproduction, the changes
  per acceptance item, the test counts with the evidence line (`kicad_mcp.__file__` inside the
  worktree and pytest's `rootdir:`), the decisions with the default taken, the batch-gate verdict.
  A gate that went RED and was fixed gets a new comment, not an edit of the old one.
- **Review verdict**: after each adversarial review, the coordinator posts the reviewed (full) sha,
  the verdict (CLEAN or NOT CLEAN), the BLOCKS findings and the FOLLOW-UP findings, and the number
  of the delta round. The coordinator merges only the sha named in a CLEAN verdict.
- **Follow-ups** become new `build` issues at the merge, each linked from the closing comment.

## What does not change

The rules of the playbook (`docs/playbook/AGENT-WORKFLOW-PLAYBOOK.md` and its fork
`docs/playbook/PROJECT-KICAD-MCP-PLAYBOOK.md`) stay as they are: every build ticket gets an
adversarial review; every finding is classed BLOCKS or FOLLOW-UP; a BLOCKS finding becomes a failing
test before its fix; the delta is re-reviewed until CLEAN; after the third delta review the
coordinator stops and escalates; the stop rule never permits a merge without CLEAN; the batch gate
runs on the committed head. Only the place of the record moved, from files to issues. Worktrees need
no special handling for planning records: there is nothing to link or copy.

## The helper

`python scripts/issues.py [--repo OWNER/NAME] [--no-identity-check] <command>` wraps the `gh` CLI.
The repository must be named by `--repo` or the environment variable `BOARDPROOF_REPO`; without
one every command refuses (exit 2) instead of letting `gh` guess the repository of the current
directory, which may be an archive.

| Command | Does |
|---|---|
| `create --title T --body-file F [--label L]... [--blocked-by N]...` | creates an issue; appends `Blocked by #N` lines |
| `comment N --body-file F` | adds a comment (Answer, verdict) |
| `close N [--comment-file F]` | closes, optionally with a closing comment |
| `list [--label L] [--state open\|closed\|all]` | one line per issue |
| `show N` | the issue with all its comments (what an agent reads before it starts) |
| `label-sync` | creates or updates the labels above |
| `create-batch DIR [--dry-run]` | creates the issues of a folder of markdown files in dependency order, replacing old ids by the new numbers; checks every text and that every label exists (run `label-sync` first) before it creates the first issue |

Before anything is sent, every title, body, comment and label is scanned with the built-in patterns
of `scripts/check_publishable.py` (home paths, e-mail addresses, token shapes) and, when the
environment variable `BOARDPROOF_FORBIDDEN_FILE` names a pattern file, with those patterns too (the
file is private and never committed). A missing variable means the identity check was not done,
which is not a pass: `create`, `comment`, `close` and `create-batch` then refuse with exit code 2,
unless `--no-identity-check` (or `BOARDPROOF_ALLOW_UNCHECKED=1`) is given, which prints a loud
warning on stderr and stdout and still applies the built-in patterns. A hit refuses the whole command with exit code 2, names the
pattern and the line but not the matched text, and sends nothing. Reword the text; never bypass the
check. Exit code 3 means `gh` or the repository is not reachable (for example not created yet or not
logged in); nothing was sent.

`--blocked-by` takes only an issue number (`3` or `#3`). Write a body to a file first (outside the repository, or in an ignored folder), then pass it with
`--body-file`; this keeps shell quoting out of the text.
