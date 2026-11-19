# Issue tracker: GitHub issues, through `scripts/issues.py`

Issues and specs for this repository live as GitHub issues. The convention (map, ticket, labels,
Answer and review comments) is `docs/WORKFLOW.md`; read it first.

## Conventions

- **Read an issue with its comments**: `python scripts/issues.py show <n>`.
- **Create an issue**: `python scripts/issues.py create --title T --body-file F [--label L]... [--blocked-by N]...`.
- **List issues**: `python scripts/issues.py list [--label L] [--state open|closed|all]`.
- **Comment on an issue** (Answer, review verdict): `python scripts/issues.py comment <n> --body-file F`.
- **Close**: `python scripts/issues.py close <n> [--comment-file F]` (the coordinator closes a build ticket at the merge).
- The script wraps `gh`, names the repository itself and refuses a text that
  `scripts/check_publishable.py` flags (home paths, e-mail addresses, token shapes, the private
  forbidden-patterns file). Write every text so that it may become public.
- Labels: one type (`build`, `research`, `decision`, `task`), plus `critical`, `parked`,
  `needs-owner` where they apply, and `map` for a map. Blocking is a `Blocked by #n` line in the
  body. Status is open or closed.

**PRs as a request surface: no.** There are no pull requests in this project; merges are local
squash merges by the coordinator after a CLEAN review (CLAUDE.md, P12).

## When a skill says "publish to the issue tracker"

Create a GitHub issue with `scripts/issues.py create`.

## When a skill says "fetch the relevant ticket"

Read it with `scripts/issues.py show <n>`.
