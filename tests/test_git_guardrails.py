"""Build ticket 45: the git guardrails.

Two layers: the Claude Code PreToolUse hook (`scripts/git_guardrails.py`, text inspection of the
Bash and PowerShell tool commands) and the git pre-push hook (`scripts/git-hooks/pre-push`, which
sees the real ref updates). Normal pushes must stay allowed (owner, 2026-10-19). The pre-push hook
is exercised against a throwaway bare remote under `tmp_path`, never against `origin`.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
HOOK = ROOT / "scripts" / "git_guardrails.py"
PRE_PUSH = ROOT / "scripts" / "git-hooks" / "pre-push"
SETTINGS = ROOT / ".claude" / "settings.json"

_spec = importlib.util.spec_from_file_location("git_guardrails", HOOK)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


# Review round 2, finding 1: bash keywords used to hide the command after them.
KEYWORD_CASES = [
    "if true; then {cmd}; fi",
    "for b in a; do {cmd}; done",
    "if false; then :; else {cmd}; fi",
    "if false; then :; elif {cmd}; then :; fi",
    "if {cmd}; then :; fi",
    "while {cmd}; do :; done",
    "until {cmd}; do :; done",
    "! {cmd}",
    "if ! {cmd}; then :; fi",
    "coproc {cmd}",
    "{{ {cmd}; }}",
    "time {cmd}",
]
KEYWORD_BLOCKED = [
    case.format(cmd=cmd)
    for case in KEYWORD_CASES
    for cmd in ("git push -f origin main", "git branch -D main")
]

# Review round 2, finding 2: what the resolver does not understand is unresolved, which refuses
# local destructive commands (the pre-push guard cannot see them) and the git subcommand.
UNRESOLVED_BLOCKED_BASH = [
    'git branch -D "$1"',
    'git branch -D "$@"',
    "git branch -D ${B:-main}",
    'a=(main); git branch -D "${a[@]}"',
    "git branch -D $'main'",
    "B=x; B+=main; git branch -D $B",
    "B=$(echo main); git branch -D $B",
    "B=`echo main`; git branch -D $B",
    "B=ticket; read B; git branch -D $B",
    "B=ok; printf -v B main; git branch -D $B",
    "git branch -D ma{in,}",
    "git branch -D ~+",
    "git $SUB -D main",
    'git update-ref -d "$REF"',
    "git update-ref $OPT refs/heads/main",
    "git branch $FLAG main",
    "git update-ref refs/heads/main 0000000000000000000000000000000000000000",
    "rm .git/refs/heads/main",
    "bash <<'EOF'\ngit branch -D main\nEOF",
    "bash <<'EOF'\ngit push --force origin main\nEOF",
    "sh -s <<EOF\ngit branch -D main\nEOF",
    "bash <<< 'git branch -D main'",
    "cat <<EOF | bash\ngit branch -D main\nEOF",
    "echo 'git branch -D main' | sh",
    "alias gb='git branch -D'; gb main",
    "alias gp='git push --force'; gp origin main",
    'for b in ticket-1 main; do git branch -D "$b"; done',
    '"$G" branch -D main',
    # finding 7: arithmetic is not a here-document
    "let x=1<<2\ngit branch -D main",
    "(( x = 1<<2 ))\ngit push --force origin main",
]

# Fix round 3, finding 1: switching the pre-push guard off in plain sight.
HOOK_BYPASS_BLOCKED_BASH = [
    "git push --no-verify origin main",
    "git push --no-veri origin ticket-45",
    "git push origin ticket-45 --no-verify",
    "git -c core.hooksPath=/dev/null push origin main",
    "git -c core.hookspath= push origin main",
    "git --config-env=core.hooksPath=X push origin main",
    "git config core.hooksPath /tmp/none",
    "git config --global core.hooksPath ''",
    "git config --system core.hookspath x",
    "git config set core.hooksPath x",
    "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=core.hooksPath GIT_CONFIG_VALUE_0=x git push origin main",
    "export GIT_CONFIG_KEY_0=core.hooksPath",
    "GIT_CONFIG_PARAMETERS=\"'core.hooksPath'='x'\" git push origin main",
    "rm .git/hooks/pre-push",
    "rm -rf .git/hooks",
    "mv .git/hooks/pre-push .git/hooks/pre-push.off",
    "chmod -x .git/hooks/pre-push",
    "echo exit 0 > .git/hooks/pre-push",
    "cp /dev/null .git/hooks/pre-push",
    'rm "$(git rev-parse --git-common-dir)/hooks/pre-push"',
    "truncate -s 0 .git/hooks/pre-push",
]

# Fix round 3, findings 2 and 3: case variants of main and more local deletions.
LOCAL_DELETE_BLOCKED_BASH = [
    "git branch -D Main",
    "git branch -D MAIN",
    "git update-ref -d refs/HEADS/main",
    "git update-ref -d refs/heads/Main",
    "git push origin :Main",
    "git branch -D @{-1}",
    "git update-ref -d @{-1}",
    "git branch --format='%(refname:short)' | xargs git branch -D",
    "git branch | xargs -n1 git branch -D",
    "git for-each-ref --format='%(refname)' refs/heads | xargs -n1 git update-ref -d",
    "echo main | xargs git branch -D",
    "echo 'delete refs/heads/main' | git update-ref --stdin",
    "git update-ref --stdin <<EOF\ndelete refs/heads/main\nEOF",
    "git update-ref --stdin <<< 'delete refs/heads/main'",
    "printf 'delete refs/heads/main\\n' | git update-ref --stdin -z",
    "git update-ref --stdin <<EOF\nupdate refs/heads/MAIN abc\nEOF",
    "git update-ref --stdin <<EOF\nfrobnicate x\nEOF",
    "git update-ref --stdin < cmds.txt",
    "some-generator | git update-ref --stdin",
    "rm -rf .git",
    "rm -rf .git/",
    "rm -r .git/refs",
    "rm -rf .git/refs/heads",
    "rmdir .git/refs/heads",
    "echo > .git/refs/heads/main",
    "printf x > .git/refs/heads/main",
    "mv .git/refs/heads/main /tmp/x",
    "cd .git/refs/heads && rm main",
    "cd .git && rm -rf refs",
    "cd .git/refs; rm -r heads",
    "printf 'git branch -D main\\n' | bash",
]

BLOCKED_BASH = [
    "git push --force origin main",
    "git push -f",
    "git push origin main -f",
    "git push -uf origin ticket-45",
    "git push --force-with-lease origin ticket-45",
    "git push --force-with-lease=ticket-45:abc123 --force-if-includes origin ticket-45",
    "git push --forc origin main",
    "git push origin +main",
    "git push origin +refs/heads/ticket-45:refs/heads/ticket-45",
    "git push --mirror origin",
    "git push origin --delete main",
    "git push -d origin main",
    "git push origin :main",
    "git push origin :refs/heads/main",
    "git push --delete origin refs/heads/main",
    "git -C /some/repo push --force",
    "git -c core.quotepath=off push -f origin x",
    "git -c alias.p=push p -f",
    "git -c remote.origin.mirror=true push",
    "git -c 'remote.origin.push=+refs/heads/*:refs/heads/*' push",
    "git config alias.yolo 'push --force'",
    "git config remote.origin.push '+refs/heads/main:refs/heads/main'",
    "git config --local remote.origin.mirror true",
    "git branch -D main",
    "git branch -d main",
    "git branch --delete --force main",
    "git branch -m main old-main",
    "git branch -M ticket-45 main",
    "git branch -C other main",
    "git update-ref -d refs/heads/main",
    "git status && git push --force",
    "git status; git push -f origin main",
    "git status\ngit push --force origin main",
    "true || git push -f",
    "(cd /x && git push -f)",
    "echo \"$(git push --force)\"",
    "echo `git push -f`",
    "GIT_TRACE=1 git push -f",
    "env GIT_TRACE=1 git push --force",
    "timeout 60 git push --force origin main",
    "/usr/bin/git push --force",
    "\"C:/Program Files/Git/cmd/git.exe\" push -f",
    "bash -c 'git push --force origin main'",
    "bash -lc \"git status && git push -f\"",
    "sh -c 'git push origin :main'",
    "cmd /c git push -f origin main",
    "powershell -Command \"git push --force\"",
    "eval 'git push -f'",
    "git push origin main --force 2>&1 | tail -5",
    "gh api -X DELETE repos/o/r/git/refs/heads/main",
    "gh api --method PATCH repos/o/r/git/refs/heads/main -F sha=abc -F force=true",
    # review round 1: abbreviations git accepts
    "git push --m origin",
    "git push --mi origin",
    "git push origin --de main",
    "git push --f origin main",
    # review round 1: wrapper options
    "env -u HOME git push --force",
    "timeout -s KILL 60 git push -f",
    "timeout --signal=KILL 60 git push -f",
    "nice -n 5 git push --force",
    "env -S 'git push -f'",
    "sudo -u builder git push -f",
    # review round 1: variables
    "F=--force; git push $F origin main",
    "F=--force\ngit push \"$F\" origin main",
    "export F=-f && git push ${F} origin main",
    "git $SUB -f origin main",
    "$G push -f",
    # review round 1: aliases that are force pushes
    "git -c alias.p='push -f' p",
    "git -c 'alias.p=!git push --force' p",
    "git -c alias.p=push p --force origin main",
    # review round 1: more ways to delete main or force-push
    "gh api graphql -f query='mutation { deleteRef(input: {refId: \"x\"}) { clientMutationId } }'",
    "gh api graphql -f query='mutation { updateRef(input: {refId: \"x\", oid: \"y\", force: true}) { ref { name } } }'",
    "gh repo delete o/r --yes",
    "gh api -X DELETE repos/o/r",
    "gh api -X PATCH repos/o/r/git/refs/heads/ticket-1 -F sha=abc -F force=true",
    "gh api -X POST repos/o/r/branches/main/rename -f new_name=old",
    "git remote add --mirror=push backup https://example.invalid/r.git",
    "git remote add --mirror backup https://example.invalid/r.git",
    "git update-ref -d HEAD",
    "cat <<EOF > notes.txt\ngit push -f\nEOF\ngit push --force origin main",
    "git commit -F - <<'EOF'\nmsg\nEOF\ngit push -f",
]

ALLOWED_BASH = [
    "git push",
    "git push origin main",
    "git push -u origin ticket-45",
    "git push origin ticket-45:ticket-45",
    "git push --set-upstream origin research/foo",
    "git push origin --delete ticket-45",
    "git push origin :ticket-45",
    "git push --tags origin",
    "git push --follow-tags origin main",
    "git push -o ci.skip origin main",
    "git push --no-force-with-lease origin main",
    "git push origin main 2>&1 | tail -5",
    "git commit -m 'blocks git push --force and deleting main'",
    "git commit -m \"docs: git push -f is refused; git branch -D main too\"",
    "echo git push --force",
    "git log --oneline -3 && git status",
    "git merge --squash ticket-45",
    "git branch -D ticket-45",
    "git branch -d ticket-45 && git worktree prune",
    "git branch -m ticket-45 ticket-45-old",
    "git branch -f ticket-45 main",
    "git branch main-copy main",
    "git update-ref -d refs/heads/ticket-45",
    "git config user.name builder",
    "git config alias.st status",
    # not force pushes and not deleting main (owner's rule, review round 1)
    "git push --prune origin 'refs/heads/*:refs/heads/*'",
    "git config alias.p push",
    "git -c alias.p=push p origin main",
    "git remote add --mirror=fetch backup https://example.invalid/r.git",
    "gh api graphql -f query='{ viewer { login } }'",
    "gh api -X DELETE repos/o/r/git/refs/heads/ticket-45",
    "git push -u origin \"$(git branch --show-current)\"",
    "B=ticket-45; git push origin $B",
    # fix round 2: unresolved push arguments are allowed (the pre-push guard decides)
    'git push origin "$BRANCH"',
    'for b in research/a research/b; do git push origin "$b"; done',
    "git push origin $(git branch --show-current)",
    'for b in ticket-1 ticket-2; do git branch -D "$b"; done',
    'git -C "$WT" branch -D ticket-45',
    "B=ticket-45; git branch -D $B",
    'git branch "$NEW"',
    "if true; then git push origin main; fi",
    "let x=1<<2; git push origin main",
    "(( n = 1 << 2 )); git status",
    "for b in a b; do echo $b; done",
    # fix round 3: plausible neighbours of the blocked forms stay allowed
    "git push --no-thin origin main",
    "git config core.hooksPath",
    "git config --get core.hooksPath",
    "cp scripts/git-hooks/pre-push \"$(git rev-parse --git-common-dir)/hooks/\"",
    "cp scripts/git-hooks/pre-push .git/hooks/pre-push",
    "cp scripts/git-hooks/commit-msg \"$(git rev-parse --git-common-dir)/hooks/commit-msg\"",
    "rm -f .git/index.lock",
    "rm -rf build/ dist/",
    "rm -rf .pytest_cache",
    "rm scripts/git-hooks/pre-push.bak",
    "cat .git/hooks/pre-push",
    "ls .git/refs/heads",
    "git branch -D ticket-45 && git branch -d ticket-46",
    "git update-ref --stdin <<EOF\nupdate refs/heads/ticket-1 abc\ndelete refs/heads/ticket-2\nEOF",
    "echo 'delete refs/heads/research-x' | git update-ref --stdin",
    "cd .git/refs/heads && ls",
    "cd .git/refs/heads && cd /tmp && rm main",
    "echo main > notes.txt",
    "git branch | xargs echo",
    "git log --format=%H | xargs -n1 git show --stat",
    "echo 'git push origin main' | bash",
    "git log --oneline | cat",
    "cat <<EOF > notes.md\ngit branch -D main is refused\nEOF",
    "git commit -m \"$(cat <<'EOF'\nGuard fix, step 1) the \"x\ngit push --force origin main\nEOF\n)\" && git push origin ticket-45",
    "env -u HOME git push origin main",
    "timeout -s KILL 60 git push origin ticket-45",
    # review round 1: commit messages through a here-document inside a substitution
    "git commit -m \"$(cat <<'EOF'\nGuard (part 1 of the fix\ngit push --force is refused :)\nEOF\n)\" && git push origin ticket-45",
    "git -C 'D:/work/wt-45' push origin ticket-45",
    "gh api repos/o/r/git/refs/heads/main",
    "gh pr list",
    "grep -rn 'push --force' docs/",
    "python -m pytest tests/test_git_guardrails.py",
    "ls -la",
    "git commit -q -F - <<'EOF'\nGuard: git push --force and git branch -D main are refused\n"
    "git push -f origin main\nEOF\ngit push origin ticket-45",
    "git commit -F - <<-EOF\n\tgit push -f\n\tEOF\ngit log -1",
]

HOOK_AND_LOCAL_BLOCKED_POWERSHELL = [
    "git push --no-verify origin main",
    "git -c core.hooksPath=NUL push origin main",
    "$env:GIT_CONFIG_KEY_0 = 'core.hooksPath'",
    "Remove-Item .git/hooks/pre-push",
    "Remove-Item -Recurse -Force .git\\hooks",
    "Set-Content .git\\hooks\\pre-push 'exit 0'",
    "Move-Item .git/hooks/pre-push .git/hooks/x",
    "git branch -D MaIn",
    "git update-ref -d refs/Heads/MAIN",
    "Remove-Item -Recurse -Force .git",
    "cmd /c del .git\\refs\\heads\\main",
    "cmd /c rmdir /s /q .git\\refs",
    "Set-Content .git/refs/heads/main ''",
    "'' | Out-File .git/refs/heads/main",
    "Out-File -FilePath .git\\refs\\heads\\main",
    "Set-Location .git/refs/heads; Remove-Item main",
    "git branch -D @{-1}",
]

UNRESOLVED_BLOCKED_POWERSHELL = [
    "& (Get-Command git) branch -D main",
    "& (Get-Command git) push --force origin main",
    "& ('gi'+'t') branch -D main",
    "git branch -D ('ma'+'in')",
    "git branch -D @('main')",
    'git branch -D $("main")',
    "$B = 'MAIN'.ToLower(); git branch -D $B",
    "$B = 'ma' + 'in'; git branch -D $B",
    "$B = \"ma$('in')\"; git branch -D $B",
    "$B = 'x'; Set-Variable B 'main'; git branch -D $B",
    "iex @'\ngit branch -D main\n'@",
    "Remove-Item .git/refs/heads/main",
]

BLOCKED_POWERSHELL = [
    "git push --force origin main",
    "& git push -f",
    "git status; git push --force",
    "git push origin main --force 2>$null",
    "& 'C:\\Program Files\\Git\\cmd\\git.exe' push -f",
    "git branch -D main",
    "Invoke-Expression 'git push -f'",
    "cmd /c \"git push --force\"",
    "Write-Output \"$(git push -f)\"",
    # review round 1
    "Invoke-Expression -Command 'git push -f'",
    "Start-Process git -ArgumentList 'push','--force' -Wait",
    "Start-Process -FilePath git -ArgumentList 'push --force origin main' -NoNewWindow",
    "Start-Process git 'push -f'",
    "saps git -ArgumentList 'push','origin',':main'",
    "$a = 'push','--force'; git @a",
    "$f = '--force'; git push $f origin main",
    "& $git push -f",
    "powershell -EncodedCommand " + base64.b64encode("git push -f".encode("utf-16-le")).decode(),
    "pwsh -enc " + base64.b64encode("git branch -D main".encode("utf-16-le")).decode(),
]

ALLOWED_POWERSHELL = [
    "git push origin main",
    "git push -u origin ticket-45; git status",
    "git commit -m 'note: git push --force is blocked'",
    "Get-ChildItem D:\\work",
    "git -C \"D:\\work\\wt 45\" push origin ticket-45",
    # review round 1
    "git commit -m @'\nGuard: git push --force is refused (and git branch -D main)\n'@",
    "git commit -m @\"\nGuard: git push -f is refused\n\"@; git push origin ticket-45",
    "Start-Process git -ArgumentList 'push','origin','main' -Wait",
    "$b = 'ticket-45'; git push origin $b",
    "git push origin $branch",
    "foreach ($b in 'research/a','research/b') { git push origin $b }",
    # fix round 3, finding 4: literal foreach lists are resolved like bash for lists
    "foreach ($t in 'ticket-1','ticket-2') { git branch -D $t }",
    "Remove-Item .git/index.lock",
    "Set-Content notes.txt 'git branch -D main'",
    "$b = 'ticket-45'; git branch -D $b",
    "git commit -m @'\nfix: don't\ngit push --force origin main\n'@",
]


@pytest.mark.parametrize("command", BLOCKED_BASH)
def test_bash_blocks(command):
    assert guard.check_command(command, "bash"), command


@pytest.mark.parametrize("command", ALLOWED_BASH)
def test_bash_allows(command):
    assert guard.check_command(command, "bash") is None, command


@pytest.mark.parametrize("command", KEYWORD_BLOCKED)
def test_bash_keywords_do_not_hide_commands(command):
    assert guard.check_command(command, "bash"), command


@pytest.mark.parametrize("command", UNRESOLVED_BLOCKED_BASH)
def test_bash_unresolved_or_hidden_local_destruction_blocks(command):
    assert guard.check_command(command, "bash"), command


@pytest.mark.parametrize("command", HOOK_BYPASS_BLOCKED_BASH + LOCAL_DELETE_BLOCKED_BASH)
def test_bash_hook_bypass_and_local_deletion_blocks(command):
    assert guard.check_command(command, "bash"), command


@pytest.mark.parametrize("command", HOOK_AND_LOCAL_BLOCKED_POWERSHELL)
def test_powershell_hook_bypass_and_local_deletion_blocks(command):
    assert guard.check_command(command, "powershell"), command


def test_powershell_foreach_with_main_in_the_list_blocks():
    assert guard.check_command("foreach ($t in 'ticket-1','main') { git branch -D $t }", "powershell")


@pytest.mark.parametrize("command", UNRESOLVED_BLOCKED_POWERSHELL)
def test_powershell_unresolved_or_hidden_local_destruction_blocks(command):
    assert guard.check_command(command, "powershell"), command


@pytest.mark.parametrize("command", BLOCKED_POWERSHELL)
def test_powershell_blocks(command):
    assert guard.check_command(command, "powershell"), command


@pytest.mark.parametrize("command", ALLOWED_POWERSHELL)
def test_powershell_allows(command):
    assert guard.check_command(command, "powershell") is None, command


# Build ticket 63, gap 1: the value of a value-taking `git config` option was read as the key, so
# `git config -f .git/config core.hooksPath x` passed as "set .git/config to core.hooksPath".
CONFIG_VALUE_OPTION_BLOCKED = [
    "git config -f .git/config core.hooksPath x",
    "git config --file .git/config core.hooksPath x",
    "git config --fil .git/config core.hooksPath x",
    "git config -f.git/config core.hooksPath x",
    "git config --blob HEAD:cfg core.hooksPath x",
    "git config --type path core.hooksPath x",
    "git config -t path core.hooksPath x",
    "git config --default y core.hooksPath x",
    "git config --comment why core.hooksPath x",
    "git config --value old core.hooksPath x",
    "git config set --file .git/config core.hooksPath x",
    "git config set --comment why --type path core.hooksPath x",
    "git config -f .git/config --add core.hooksPath x",
    "git config --file ../.git/config remote.origin.push +refs/heads/*",
    "git config -f .git/config alias.p 'push --force'",
    # an abbreviation shared with a flag option (--fixed-value, --bool) takes no value
    "git config --fi core.hooksPath x",
    "git config --b core.hooksPath x",
]
CONFIG_VALUE_OPTION_ALLOWED = [
    "git config -f .git/config core.hooksPath",
    "git config --file .git/config --get core.hooksPath",
    "git config -f other.cfg user.name builder",
    "git config --type bool core.autocrlf false",
    "git config --comment 'hooks stay on' user.name builder",
    "git config --file .git/config --list",
]


@pytest.mark.parametrize("command", CONFIG_VALUE_OPTION_BLOCKED)
def test_config_value_options_do_not_hide_the_key(command):
    assert guard.check_command(command, "bash"), command
    assert guard.check_command(command, "powershell"), command


@pytest.mark.parametrize("command", CONFIG_VALUE_OPTION_ALLOWED)
def test_config_value_options_keep_harmless_config_allowed(command):
    assert guard.check_command(command, "bash") is None, command


# Build ticket 63, gap 3: after `git pack-refs` (or a gc) main lives only in `.git/packed-refs`;
# removing or truncating that file deletes main (reproduced in a throwaway repo).
PACKED_REFS_BLOCKED_BASH = [
    "rm .git/packed-refs",
    "rm -f .git/packed-refs",
    "mv .git/packed-refs /tmp/x",
    "truncate -s 0 .git/packed-refs",
    "echo > .git/packed-refs",
    ": > .git/packed-refs",
    "cp /dev/null .git/packed-refs",
    "cd .git && rm packed-refs",
    'rm "$(git rev-parse --git-common-dir)/packed-refs"',
    "rm 'D:/work/repo/.git/packed-refs'",
]
PACKED_REFS_BLOCKED_POWERSHELL = [
    "Remove-Item .git/packed-refs",
    "Remove-Item -Force .git\\packed-refs",
    "Clear-Content .git\\packed-refs",
    "Set-Content .git/packed-refs ''",
    "'' | Out-File .git/packed-refs",
]
PACKED_REFS_ALLOWED = [
    "cat .git/packed-refs",
    "grep main .git/packed-refs",
    "cp .git/packed-refs /tmp/packed-refs.bak",
    "git pack-refs --all",
]


@pytest.mark.parametrize("command", PACKED_REFS_BLOCKED_BASH)
def test_removing_or_truncating_packed_refs_blocks(command):
    assert guard.check_command(command, "bash"), command


@pytest.mark.parametrize("command", PACKED_REFS_BLOCKED_POWERSHELL)
def test_powershell_removing_or_truncating_packed_refs_blocks(command):
    assert guard.check_command(command, "powershell"), command


@pytest.mark.parametrize("command", PACKED_REFS_ALLOWED)
def test_reading_packed_refs_stays_allowed(command):
    assert guard.check_command(command, "bash") is None, command


# Build ticket 63, gap 2: `find` deletes through -delete and -exec without naming the file, so the
# protected-path check of rm/mv never saw it. A deleting find is refused when it starts inside a
# .git directory (the guard cannot enumerate what it deletes there), when a name, path or regex test
# can match main, packed-refs, the pre-push hook, the hooks or refs directories or .git itself, or
# when a start point or a test cannot be resolved.
FIND_BLOCKED = [
    "find .git/refs -name main -delete",
    "find .git/refs -name main -exec rm {} \\;",
    "find .git/refs -name main -exec rm -f '{}' ';'",
    "find .git/refs -name main -exec rm -f {} +",
    "find .git/refs -name main -execdir rm {} +",
    "find .git/refs -name main -ok rm {} \\;",
    "find .git/refs -name main -exec /usr/bin/rm {} \\;",
    "find .git/refs -name main -exec mv {} /tmp \\;",
    "find .git/refs -name main -exec sh -c 'rm \"$1\"' _ {} \\;",
    "find -L .git/refs -name main -delete",
    "find .git/refs/heads -type f -delete",
    "find .git -name packed-refs -delete",
    "find .git/hooks -name pre-push -delete",
    "find .git -name '*.lock' -delete",  # inside .git every deleting find is refused
    "find . -name main -delete",
    "find . -iname MAIN -delete",
    "find . -name 'ma*' -delete",
    "find . -name '*' -delete",
    "find . -name packed-refs -exec rm {} \\;",
    "find . -type d -name hooks -exec rm -rf {} +",
    "find . -name .git -exec rm -rf {} +",
    "find . -path '*/refs/heads/main' -delete",
    "find . -wholename './.git/packed-refs' -delete",
    "find . -regex '.*/heads/main' -delete",
    "find . \\( -name '*.pyc' -o -name main \\) -delete",
    "cd .git && find refs -name main -delete",
    "cd .git/refs; find . -type f -delete",
    'find "$D" -type f -delete',
    'find . -name "$N" -delete',
    "find . -fprint .git/packed-refs",
    "git branch -D x; find .git/refs -name main -delete",
    "find . -iregex '.*/HEADS/\\D+' -delete",
    # the command of -exec is checked as a command; {} is a file the guard cannot name
    "find . -name x -exec git branch -D main \\;",
    "find . -name 'ticket-*' -exec git branch -D {} \\;",
    # a deleting find over the repository root without a name, path or regex test reaches .git
    "find . -type f -delete",
    "find -delete",
    "find .. -mtime +30 -delete",
    "find ./ -type f -exec rm {} +",
    "find . -type d -empty -delete",
    # fix round 1, finding 1: a negated test does not narrow, it widens (each deletes main)
    "find . -type f ! -name '*.py' -delete",
    "find . -type f -not -name '*.kicad_pcb' -delete",
    "find . -type f -not -path '*/node_modules/*' -delete",
    "find . -mindepth 1 ! -name keep.txt -exec rm -rf {} +",
    "find . ! \\( -name '*.py' -o -name '*.md' \\) -delete",
    "find . -not \\( -type d \\( -name a \\) \\) -type f -delete",
    # fix round 1, finding 2: GNU find reads -regex as emacs syntax, not Python syntax
    "find . -regex '.*/heads/\\(main\\)' -delete",
    "find . -regex '.*/\\(main\\|dev\\)' -delete",
    "find . -regex '.*/ma\\{1\\}in' -delete",
    "find . -regextype posix-extended -regex '.*/(main|dev)' -delete",
    "find . -regextype emacs -regex '.*\\.pyc' -delete",
    # fix round 2: more emacs and POSIX forms Python reads differently
    "find . -regex '.*/\\<main\\>' -delete",
    "find . -regex '.*/heads/[[:alpha:]]*' -delete",
    # fix round 1, finding 3: one case per guard branch that had none
    "find . -regex '.*[' -delete",  # a regex Python cannot compile
    'find . -name main -exec "$CMD" {} +',
    'find . -exec "$CMD" \\;',  # an unresolved -exec command counts as deleting
    "find refs/heads -delete",
    "cd .git && find . -name '*.lock' -delete",
    "find -L .git -name '*.lock' -delete",
    "find D:/work/repo -path 'D:/work/repo/.git/refs/heads/main' -delete",
    "find /srv/repo -wholename '/srv/repo/.git/packed-refs' -delete",
    # the prune idiom is safe but names .git; refused on purpose (it fails closed), see docstring
    "find . -name .git -prune -o -type f -delete",
    "find . -path ./.git -prune -o -type f -delete",
]
FIND_ALLOWED = [
    # fix round 1: neighbours of the negation and regex fixes stay allowed
    "find build ! -name keep.txt -delete",
    "find . -name '*.pyc' ! -path './.venv/*' -delete",
    "find . -regex '.*\\.pyc' -delete",
    "find . -regextype posix-extended -regex '.*/(main|dev)'",
    "find . -name '*.pyc' -delete",
    "find . -name __pycache__ -type d -exec rm -rf {} +",
    "find build -type f -delete",
    "find src -name '*.orig' -exec rm {} \\;",
    'find "$WT" -name "*.pyc" -delete',
    "find .git/refs -name main",
    "find .git/refs -name main -print",
    "find .git -name '*.lock'",
    "find . -name main -exec cat {} \\;",
    "find . -name '*.py' -exec grep -l push {} +",
    "find . -name main -fprint found.txt",
    "find build -type d -empty -delete",
    "find /tmp/scratch -name '*.tmp' -type f -delete",
    "find . -name '*.py' -exec git log -1 --format=%H -- {} \\;",
]


@pytest.mark.parametrize("command", FIND_BLOCKED)
def test_deleting_find_that_can_reach_main_or_the_hook_blocks(command):
    assert guard.check_command(command, "bash"), command


@pytest.mark.parametrize("command", ["find .git/refs -name main -delete",
                                     "find .git/refs -name main -exec rm '{}' ';'"])
def test_powershell_deleting_find_blocks(command):
    assert guard.check_command(command, "powershell"), command


@pytest.mark.parametrize("command", FIND_ALLOWED)
def test_find_that_cannot_reach_main_stays_allowed(command):
    assert guard.check_command(command, "bash") is None, command


# Fix round 1, finding 4: an absolute start without a narrowing test is refused when it is, or lies
# above, a directory holding .git, and whenever the guard cannot tell.
def _find_all(path: Path) -> str:
    return f"find '{path.as_posix()}' -type f -delete"


def test_absolute_find_over_a_plain_directory_stays_allowed(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "a.txt").write_text("x")
    assert guard.check_command(_find_all(tmp_path), "bash") is None


@pytest.mark.parametrize("git_entry", ["dir", "file"])
def test_absolute_find_over_a_directory_holding_git_blocks(tmp_path, git_entry):
    repo = tmp_path / "work" / "repo"
    repo.mkdir(parents=True)
    if git_entry == "dir":
        (repo / ".git").mkdir()
    else:  # a linked worktree has a .git file
        (repo / ".git").write_text("gitdir: elsewhere\n")
    assert guard.check_command(_find_all(tmp_path), "bash")
    assert guard.check_command(_find_all(repo), "bash")
    assert guard.check_command(f"find '{repo.as_posix()}' -name '*.pyc' -delete", "bash") is None


def test_absolute_find_over_this_repository_or_above_blocks():
    assert guard.check_command(_find_all(ROOT), "bash")
    assert guard.check_command(_find_all(ROOT.parent), "bash")
    assert guard.check_command(f"find '{ROOT.as_posix()}' -name '*.pyc' -delete", "bash") is None


@pytest.mark.parametrize("command", [
    "find /tmp/scratch -type f -delete",  # a Git Bash mount the guard cannot map to a folder
    "find ~ -type f -delete",
    "find ~/notes -mtime +30 -delete",
    "find D:/no/such/folder/63 -type f -delete",
])
def test_absolute_find_the_guard_cannot_check_blocks(command):
    assert guard.check_command(command, "bash"), command


def test_absolute_find_over_a_directory_too_large_to_scan_blocks(tmp_path, monkeypatch):
    for n in range(10):
        (tmp_path / f"f{n}.txt").write_text("x")
    monkeypatch.setattr(guard, "_FIND_SCAN_LIMIT", 5)
    assert guard.check_command(_find_all(tmp_path), "bash")


# Fix round 2, finding A: a folder the scan cannot list may hide a .git; the scan fails closed.
def test_absolute_find_over_a_folder_the_scan_cannot_list_blocks(tmp_path, monkeypatch):
    (tmp_path / "locked").mkdir()
    real_scandir = os.scandir
    locked = str(tmp_path / "locked")

    def scandir(path="."):
        if os.fspath(path).replace("\\", "/").rstrip("/") == locked.replace("\\", "/"):
            raise PermissionError(13, "Access is denied", os.fspath(path))
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", scandir)
    assert guard.check_command(_find_all(tmp_path), "bash")


# Fix round 2, finding B: the hook times out after 30 s, and a timed-out hook may let the call
# through, so the scan stops at a deadline and then refuses.
def test_absolute_find_whose_scan_passes_the_deadline_blocks(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("x")
    real_scandir = os.scandir

    def slow_scandir(path="."):
        time.sleep(0.2)
        return real_scandir(path)

    monkeypatch.setattr(os, "scandir", slow_scandir)
    monkeypatch.setattr(guard, "_FIND_SCAN_SECONDS", 0.05)
    assert guard.check_command(_find_all(tmp_path), "bash")


def test_the_scan_deadline_stays_well_below_the_hook_timeout():
    assert 0 < guard._FIND_SCAN_SECONDS <= 5


# Fix round 2, finding C: find does not descend into a junction (MSYS reports it as a symlink), so
# the scan does not follow one either.
@pytest.mark.skipif(sys.platform != "win32", reason="directory junctions exist on Windows only")
def test_the_scan_does_not_follow_a_junction(tmp_path):
    import _winapi

    other = tmp_path / "other"
    (other / "repo" / ".git").mkdir(parents=True)
    start = tmp_path / "start"
    start.mkdir()
    _winapi.CreateJunction(str(other), str(start / "link"))
    assert os.path.isjunction(start / "link")
    assert guard.check_command(_find_all(start), "bash") is None
    assert guard.check_command(_find_all(other), "bash")
    # with -L (or -follow) find enters the junction, and so does the scan
    assert guard.check_command(f"find -L '{start.as_posix()}' -type f -delete", "bash")
    assert guard.check_command(f"find '{start.as_posix()}' -follow -type f -delete", "bash")


def test_a_bracket_dot_regex_stays_readable():
    # `[.]` is a plain bracket expression in both syntaxes; only [[:class:]] and the like are not.
    assert guard.check_command("find . -regex '.*[.]pyc' -delete", "bash") is None


def _run_hook(payload) -> subprocess.CompletedProcess:
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return subprocess.run(
        [sys.executable, str(HOOK)], input=data, capture_output=True, timeout=60
    )


def test_hook_process_blocks_with_exit_2_and_reason():
    r = _run_hook({"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}})
    assert r.returncode == 2
    assert b"BLOCKED by the kicad-mcp git guardrail" in r.stderr
    assert b"force push" in r.stderr


def test_hook_process_allows_normal_push():
    r = _run_hook({"tool_name": "Bash", "tool_input": {"command": "git push origin main"}})
    assert r.returncode == 0, r.stderr
    assert r.stderr == b""


def test_hook_process_checks_powershell_tool():
    r = _run_hook({"tool_name": "PowerShell", "tool_input": {"command": "& git push -f"}})
    assert r.returncode == 2


def test_hook_process_ignores_other_tools():
    r = _run_hook({"tool_name": "Read", "tool_input": {"file_path": "x"}})
    assert r.returncode == 0


def test_hook_process_unreadable_input_is_blocked():
    # Review round 1: unreadable input used to exit 1, which Claude Code treats as "allow".
    r = _run_hook(b"not json")
    assert r.returncode == 2
    assert b"not checked" in r.stderr


def test_hook_process_checks_monitor_tool():
    # Review round 1: Monitor runs a shell command too.
    r = _run_hook({"tool_name": "Monitor", "tool_input": {"command": "git push --force origin main"}})
    assert r.returncode == 2


def test_parser_crash_blocks_git_commands_and_allows_others(monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("parser bug")

    monkeypatch.setattr(guard, "check_command", boom)
    rc, msg = guard.decide(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push"}}).encode())
    assert rc == 2 and "could not parse" in msg
    rc, msg = guard.decide(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls -la"}}).encode())
    assert (rc, msg) == (0, "")


def test_pre_push_hook_is_lf_only_and_pinned_by_gitattributes():
    # A CRLF checkout (core.autocrlf=true on Windows) would break the shebang and every line.
    assert b"\r" not in PRE_PUSH.read_bytes()
    attrs = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert "scripts/git-hooks/* text eol=lf" in attrs


def test_settings_register_the_hook_for_every_shell_tool():
    settings = json.loads(SETTINGS.read_text(encoding="utf-8"))
    entries = settings["hooks"]["PreToolUse"]
    matched = [e for e in entries if "git_guardrails.py" in json.dumps(e)]
    assert matched, "git_guardrails.py is not registered as a PreToolUse hook"
    import re

    matcher = re.compile(f"^(?:{matched[0]['matcher']})$")
    assert matcher.match("Bash") and matcher.match("PowerShell") and matcher.match("Monitor")
    command = matched[0]["hooks"][0]["command"]
    assert "$CLAUDE_PROJECT_DIR" in command and "scripts/git_guardrails.py" in command


# --------------------------------------------------------------------------- pre-push hook


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=60357333+TotallyNotAHackerTrustMeIAmADolphin@users.noreply.github.com", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
        check=check,
    )


@pytest.fixture
def remote_and_clone(tmp_path):
    if shutil.which("git") is None:
        pytest.fail("git is not on PATH; the pre-push guard cannot be tested")
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    # Let the bare remote accept deleting its HEAD branch, so only our hook can refuse it.
    _git(remote, "config", "receive.denyDeleteCurrent", "ignore")
    clone = tmp_path / "clone"
    _git(tmp_path, "init", "-q", "-b", "main", str(clone))
    _git(clone, "remote", "add", "origin", str(remote))
    (clone / "a.txt").write_text("one\n")
    _git(clone, "add", "a.txt")
    _git(clone, "commit", "-qm", "one")
    _git(clone, "push", "-q", "origin", "main")
    hooks = Path(_git(clone, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout.strip()) / "hooks"
    hooks.mkdir(exist_ok=True)
    shutil.copyfile(PRE_PUSH, hooks / "pre-push")
    (hooks / "pre-push").chmod(0o755)
    return remote, clone


def _remote_head(remote: Path, ref: str = "refs/heads/main") -> str:
    r = _git(remote, "rev-parse", "--verify", "-q", ref, check=False)
    return r.stdout.strip()


def test_pre_push_allows_normal_push_and_new_branch(remote_and_clone):
    remote, clone = remote_and_clone
    (clone / "a.txt").write_text("two\n")
    _git(clone, "commit", "-qam", "two")
    assert _git(clone, "push", "origin", "main", check=False).returncode == 0
    assert _remote_head(remote) == _git(clone, "rev-parse", "HEAD").stdout.strip()
    _git(clone, "branch", "research/x")
    assert _git(clone, "push", "origin", "research/x", check=False).returncode == 0
    assert _git(clone, "push", "origin", "--delete", "research/x", check=False).returncode == 0
    assert _remote_head(remote, "refs/heads/research/x") == ""


@pytest.mark.parametrize(
    "push_args",
    [
        ["push", "--force", "origin", "main"],
        ["push", "origin", "+main"],
        ["push", "--force-with-lease", "origin", "main"],
        ["-c", "alias.p=push", "p", "-f", "origin", "main"],
    ],
)
def test_pre_push_blocks_force_push(remote_and_clone, push_args):
    remote, clone = remote_and_clone
    before = _remote_head(remote)
    _git(clone, "commit", "-q", "--amend", "-m", "rewritten")
    r = _git(clone, *push_args, check=False)
    assert r.returncode != 0
    assert "pre-push guard: BLOCKED non-fast-forward" in r.stderr
    assert _remote_head(remote) == before


@pytest.mark.parametrize("push_args", [["push", "origin", "--delete", "main"], ["push", "origin", ":main"]])
def test_pre_push_blocks_deleting_main(remote_and_clone, push_args):
    remote, clone = remote_and_clone
    before = _remote_head(remote)
    r = _git(clone, *push_args, check=False)
    assert r.returncode != 0
    assert "pre-push guard: BLOCKED deleting refs/heads/main" in r.stderr
    assert _remote_head(remote) == before


def test_pre_push_blocks_force_push_of_any_branch(remote_and_clone):
    remote, clone = remote_and_clone
    _git(clone, "checkout", "-q", "-b", "ticket-1")
    (clone / "b.txt").write_text("b\n")
    _git(clone, "add", "b.txt")
    _git(clone, "commit", "-qm", "b")
    _git(clone, "push", "-q", "origin", "ticket-1")
    before = _remote_head(remote, "refs/heads/ticket-1")
    _git(clone, "commit", "-q", "--amend", "-m", "b2")
    r = _git(clone, "push", "-f", "origin", "ticket-1", check=False)
    assert r.returncode != 0
    assert _remote_head(remote, "refs/heads/ticket-1") == before


def test_pre_push_blocks_force_update_of_a_tag(remote_and_clone):
    remote, clone = remote_and_clone
    (clone / "a.txt").write_text("three" + chr(10))
    _git(clone, "commit", "-qam", "three")
    _git(clone, "tag", "v1")
    _git(clone, "push", "-q", "origin", "v1")
    before = _remote_head(remote, "refs/tags/v1")
    _git(clone, "commit", "-q", "--amend", "-m", "three rewritten")
    _git(clone, "tag", "-f", "v1")
    r = _git(clone, "push", "--force", "origin", "v1", check=False)
    assert r.returncode != 0
    assert "pre-push guard: BLOCKED non-fast-forward" in r.stderr
    assert _remote_head(remote, "refs/tags/v1") == before


def test_pre_push_blocks_what_the_command_guard_cannot_see(remote_and_clone):
    # An alias from the repository's config file and a flag from a shell variable are invisible to
    # the PreToolUse guard; the pre-push guard sees the real ref update.
    remote, clone = remote_and_clone
    before = _remote_head(remote)
    _git(clone, "config", "alias.fp", "push --force")
    _git(clone, "commit", "-q", "--amend", "-m", "rewritten")
    r = _git(clone, "fp", "origin", "main", check=False)
    assert r.returncode != 0 and "pre-push guard: BLOCKED" in r.stderr
    sh = shutil.which("sh") or shutil.which("bash")
    if sh:
        r = subprocess.run([sh, "-c", "F=--force; git push origin main $F"], cwd=clone,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode != 0 and "pre-push guard: BLOCKED" in r.stderr
    assert _remote_head(remote) == before


def test_pre_push_blocks_deleting_main_through_a_mirror_push(remote_and_clone):
    remote, clone = remote_and_clone
    _git(clone, "checkout", "-q", "-b", "other")
    _git(clone, "branch", "-D", "main")
    before = _remote_head(remote)
    # Measured with git 2.55: a mirror push does not show its deletions to the pre-push hook, so
    # the guard refuses any push from a repository that has no refs/heads/main.
    r = _git(clone, "push", "--mirror", "origin", check=False)
    assert r.returncode != 0
    assert "pre-push guard: BLOCKED" in r.stderr and "no refs/heads/main" in r.stderr
    assert _remote_head(remote) == before


def test_pre_push_blocks_pushes_to_a_mirror_remote(remote_and_clone):
    remote, clone = remote_and_clone
    _git(clone, "config", "remote.origin.mirror", "true")
    r = _git(clone, "push", "origin", check=False)
    assert r.returncode != 0
    assert "pre-push guard: BLOCKED" in r.stderr and "mirror" in r.stderr


def test_pre_push_blocks_deleting_main_in_another_case(remote_and_clone):
    # On a case-insensitive file system refs/heads/Main is the file of main.
    remote, clone = remote_and_clone
    sh = shutil.which("sh") or shutil.which("bash")
    if sh is None:
        pytest.fail("no sh to run the pre-push hook with")
    zero = "0" * 40
    for ref in ("refs/heads/Main", "refs/HEADS/MAIN"):
        line = f"(delete) {zero} {ref} {_remote_head(remote)}" + chr(10)
        r = subprocess.run([sh, str(PRE_PUSH), "origin", str(remote)], cwd=clone, input=line,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode != 0, ref
        assert "pre-push guard: BLOCKED deleting" in r.stderr


# --------------------------------------------------------------------------- commit --no-verify
# Publish-prep ticket 03a review: `commit --no-verify` / `-n` skips the identity guard
# (scripts/git-hooks/commit-msg) exactly as `push --no-verify` skips the pre-push guard.

COMMIT_NO_VERIFY_BLOCKED = [
    "git commit --no-verify -m x",
    "git commit -m x --no-verify",
    "git commit --no-veri -m x",
    "git commit --no-v -m x",
    "git commit -n -m x",
    "git commit -nm x",
    "git commit -anm x",
    "git commit -qn -m x",
    "git commit --amend -n --no-edit",
    "git merge --no-verify topic",
    "git rebase --no-verify main",
    "git -c user.name=x commit -n -m x",
]
COMMIT_NO_VERIFY_ALLOWED = [
    "git commit -m 'message with -n and --no-verify inside'",
    "git commit -am x",
    "git commit -m x -- file-n",
    "git commit --no-edit --amend",
    "git commit --no-gpg-sign -m x",
    "git commit -F msg.txt",
    "git commit -mn",
    "git merge --no-ff topic",
    "git merge -n topic",
    "git rebase -n main",
    "git log -n 5",
    "git push origin ticket-45",
]


@pytest.mark.parametrize("command", COMMIT_NO_VERIFY_BLOCKED)
def test_commit_no_verify_blocks(command):
    assert guard.check_command(command, "bash"), command


@pytest.mark.parametrize("command", COMMIT_NO_VERIFY_ALLOWED)
def test_commit_without_no_verify_stays_allowed(command):
    assert guard.check_command(command, "bash") is None, command


def test_commit_no_verify_blocks_in_powershell():
    assert guard.check_command("git commit --no-verify -m x", "powershell")
