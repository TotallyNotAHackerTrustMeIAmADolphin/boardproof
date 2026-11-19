#!/usr/bin/env bash
# The playbook builder's hand-back check (env.PLAYBOOK_BUILDER_CHECK, CLAUDE.md P6): the linter and
# the guard tests, run in THIS tree against its own src. Never the full suite and never the batch
# gate (docs/TESTING.md): the plugin allows this command 9 minutes. The ticket's own targeted tests
# are the builder's job (docs/dispatch/builder.md, Tests).
set -euo pipefail
here=$(pwd -W 2>/dev/null || pwd)
main=$(cd "$(git rev-parse --git-common-dir)/.." && pwd)
py="$main/.venv/Scripts/python.exe"
[ -x "$py" ] || py="$main/.venv/bin/python"
export PYTHONPATH="$here/src"
"$py" -c "import boardproof, pathlib, sys; p = pathlib.Path(boardproof.__file__).resolve(); r = pathlib.Path(sys.argv[1]).resolve(); sys.exit(0 if r in p.parents else 'boardproof is imported from ' + str(p) + ', not from ' + str(r))" "$here"
"$py" scripts/lint_gate.py
"$py" -m pytest -p no:cacheprovider tests/test_ratchets.py tests/test_write_guard.py \
  tests/test_agent_definitions.py tests/test_playbook_briefs.py tests/test_workflow_docs.py \
  tests/test_docs_layout.py tests/test_skills_tracked.py
