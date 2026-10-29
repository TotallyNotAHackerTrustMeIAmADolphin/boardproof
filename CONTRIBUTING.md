# Contributing

Thank you for looking at boardproof. This page says how to propose a change so that it can be reviewed quickly. Start with [README.md](README.md) and [docs/OVERVIEW.md](docs/OVERVIEW.md) if you have not yet.

## Four rules that are not negotiable

These rules protect against failures that already happened. They apply to code, tests and docs alike.

1. **Never edit a KiCad file by hand.** `.kicad_sch`, `.kicad_pcb`, `.kicad_sym`, `.kicad_mod`, `.kicad_pro` and `.kicad_dru` are not patched with a text editor or a script that writes raw text, not even for a small fix. A stray line break inside a string, a reordered token or an unquoted number can silently drop a sheet, strip nets from pads or make the file unloadable. Changes go through this project's tools, or through the write layer (`kicad_mcp.write_layer`). Test fixtures are built through `kicad_tools`' object model, never typed.
2. **Do not trust position or geometry convenience properties.** `kicad_tools` properties such as `Pad.position` and `Footprint.position` have returned coordinates wrong by the board origin, and `pad.size = ...` is silently not saved. Before you state or act on a position or size, read the raw `(at ...)`, `(size ...)` or `(polygon ...)` S-expression or cross-check against it.
3. **A board is routed or done only with a `verify_board` PASS.** Sample counts, "0 shorts", a router's own score and library properties are not evidence. A change that touches routing or verification reports the complete per-net unconnected list and the complete violation list, not a summary.
4. **The openMPPT reference board is frozen** until the first two rungs of the acceptance ladder pass. Do not route, place or "fix" it.

## How changes are planned and reviewed

Work is split into small tickets, each with a written acceptance list. Every change gets an adversarial review: each finding is classed as BLOCKS (the change cannot merge until it is fixed, and a failing test comes before the fix) or FOLLOW-UP (a new ticket). A change merges only when the review has no BLOCKS finding. If you want to work on something, open an issue first so that the scope and the acceptance list are agreed before code is written.

## Running the tests

```
python -m venv .venv
.venv/Scripts/pip install -e ".[dev,krt]"
python -m pytest -m "not network"
```

Tests that need an external tool (KiCad's demos, KiCadRoutingTools, ngspice, Freerouting) skip with a stated reason when it is absent. Before a change is merged, the maintainers run the whole gate on a clean committed tree: `python scripts/batch_gate.py`. It runs the full suite and the linter and ends with one `GREEN` or `RED` line (about 45 minutes; see [docs/TESTING.md](docs/TESTING.md)). Known failing tests are exactly those in `scripts/known-red.txt`; any other failure blocks a merge. The linter's findings are frozen in `scripts/ruff-baseline.json`: fix new findings, never add to the baseline.

## Git hooks (maintainers)

The maintainers' clone installs two reviewed hooks from `scripts/git-hooks/` into the common git directory, so that every worktree has them: `cp scripts/git-hooks/pre-push "$(git rev-parse --git-common-dir)/hooks/pre-push"` (refuses force pushes, deleting `main`, and a push that adds a commit with a private author or committer e-mail) and `cp scripts/git-hooks/commit-msg "$(git rev-parse --git-common-dir)/hooks/commit-msg"` (refuses a commit whose author or committer e-mail is not the public alias's noreply address). `python scripts/batch_gate.py --publishable-only` runs the publishability check on the tracked files before a push; it needs the private pattern file named by `BOARDPROOF_FORBIDDEN_FILE`. Details: [docs/TESTING.md](docs/TESTING.md).

## What a good pull request looks like

- **Focused.** One change, one purpose. Unrelated clean-ups go in their own pull request.
- **Tests first.** A bug fix starts with a test that fails for the right reason. A new tool has a unit test, a protocol test through the MCP client and, if it writes a KiCad file, a test of the reloaded file and a test that feeds its self-check a corrupted write and shows it fires.
- **No new public API without discussion.** A new tool, a new tool parameter or a new environment variable is a public change: raise it in an issue first. Tools operate on collections, not on one item per call, and follow the naming rules in docs/ARCHITECTURE.md decision 9.
- **Every mutation goes through the write layer** and, if it is destructive or unverified, through the propose, revision token and confirm pattern. The reference implementation is `src/kicad_mcp/input_prep.py`.
- **A rule that was broken once becomes a test.** If your change fixes a mistake of a kind that could recur, add the test or ratchet that would have caught it.
- **Docs follow code.** Update the doc that states the fact you changed. Numbers in docs are checked against a run, not copied.
- **No machine paths, no personal data.** `python scripts/check_publishable.py <directory>` scans a tree for home paths, e-mail addresses and token shapes.

## Sign-off (DCO)

Sign every commit with `git commit -s`. The sign-off line, `Signed-off-by: Your Name <your@email>`, certifies the [Developer Certificate of Origin 1.1](https://developercertificate.org/): you wrote the change or have the right to submit it under the project's MIT licence.

## Rules for AI agents

[CLAUDE.md](CLAUDE.md) holds the rules that AI coding agents follow in this repository, including the working convention (tickets, parallel branches and worktrees, review loop). A human contributor does not need to follow its process in full, but the four rules above are the same.
