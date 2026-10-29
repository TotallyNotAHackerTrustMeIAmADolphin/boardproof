"""Claude Code PreToolUse hook: block force pushes and deleting ``main`` (build ticket 45).

Registered in ``.claude/settings.json`` for every tool that runs a shell command (Bash, PowerShell,
Monitor). Claude Code passes the tool call as JSON on stdin; this script exits 2 with a reason on
stderr to block the call and 0 to allow it. Input it cannot read is blocked too.

Threat model (coordinator decision, fix round 3): the guards defend against ACCIDENTAL or careless
destructive commands by an agent, including a frustrated agent that tries to make a hook "shut up"
in plain sight. They do NOT defend against deliberate obfuscation: an agent that hides a force
flag inside ``$(printf ...)`` or a Python one-liner breaks the hard rules on purpose, and no
command parser can stop that. So every literal, plausible form is blocked, unknown local delete
targets are refused, and obfuscated option injection through unresolved push words is not chased.

Division of labour (coordinator decision, fix round 2): the pre-push guard
(``scripts/git-hooks/pre-push``) is the authoritative guard for everything a push does; for pushes
this hook is best effort. For LOCAL destructive commands (deleting ``main`` with ``git branch``,
``git update-ref``, or through the files under ``.git``) this hook is the only guard, so there
anything it cannot resolve is refused. Normal pushes stay allowed (owner, 2026-10-19), including
pushes whose arguments come from variables (``git push origin "$BRANCH"``).

Blocked:
- ``git push`` with a visible force form: ``--force``, ``-f`` (also in a cluster such as ``-uf``),
  ``--force-with-lease``, ``--force-if-includes``, a ``+`` refspec, ``--mirror``, git's
  abbreviations of these (``--forc``, ``--m``); deleting ``main`` with ``--delete``/``-d``/``--de``
  or ``:main``.
- switching the identity guard (``scripts/git-hooks/commit-msg``) off: ``git commit --no-verify``
  (from ``--no-v``) and ``-n`` (also in a cluster such as ``-anm``), ``merge``/``rebase --no-verify``.
- switching the pre-push guard off: ``git push --no-verify`` (from ``--no-veri``), setting
  ``core.hooksPath`` (``-c``, ``--config-env``, ``git config`` in any scope, ``GIT_CONFIG_KEY_<n>``,
  ``GIT_CONFIG_PARAMETERS``; ``git config`` reads the key after value options such as
  ``-f <file>``, ``--blob``, ``--type``, ``--default`` and ``--comment``), and removing, moving,
  writing or chmod-ing ``hooks/pre-push`` or the
  ``.git/hooks`` directory (copying the reviewed ``scripts/git-hooks/pre-push`` in is the merge
  step and stays allowed).
- deleting ``main`` locally, with ``main`` compared case-insensitively (a case-insensitive file
  system stores ``refs/heads/Main`` in the file of main): ``git branch -d/-D main``,
  ``git branch -m/-M/-c/-C`` that renames ``main`` away or overwrites it, ``git update-ref -d`` on
  ``main`` or ``HEAD`` or with an all-zero new value, ``git update-ref --stdin`` unless its input is
  a literal here-document, here-string, ``echo`` or ``printf`` whose every line can be read and
  touches neither main nor HEAD; removing, moving or writing ``.git``, ``.git/refs``,
  ``.git/refs/heads``, ``refs/heads/main`` or ``packed-refs`` (``rm``, ``rmdir``, ``del``,
  ``Remove-Item``, ``mv``, ``Set-Content``, ``Out-File``, ``truncate``, ``>`` and the like, also
  after a ``cd`` within the same command line); a ``find`` that deletes (``-delete``, or ``-exec``,
  ``-execdir``, ``-ok``, ``-okdir`` with a remover, writer or shell) when it starts inside ``.git``,
  when a ``-name``/``-path``/``-regex`` test can match main, ``packed-refs``, ``pre-push``,
  ``hooks``, ``refs``, ``heads`` or ``.git``, when it starts at ``.`` or ``..`` with no such test,
  when it starts at an absolute folder with no such test and that folder holds ``.git``, lies above
  the project or a folder holding ``.git`` (a scan of at most 5000 entries and 3 seconds that, like
  find without ``-L``, does not enter junctions), or cannot be checked (a missing folder, ``/tmp``
  and other Git Bash mounts, ``~``, a folder the scan cannot list, the entry limit or the deadline
  passed), when it uses ``-regextype`` or a regex form Python reads differently (``\\(``, ``\\|``,
  ``\\{``, ``\\<``, ``\\>``, POSIX classes such as ``[[:alpha:]]``), or when a start point or test
  cannot be resolved (build ticket 63). A test after ``!``/``-not`` or inside a negated group widens, so it never counts as
  narrowing. The safe prune idiom (``find . -name .git -prune -o -type f -delete``) is refused too:
  it names ``.git``, and the guard fails closed. The command of a ``find -exec`` is checked like
  any other; and the same git commands when a branch or ref argument, a flag or the git
  subcommand
  cannot be resolved (variables the resolver does not understand, ``@{-1}``, command
  substitutions, brace expansion, PowerShell expressions, arguments appended by ``xargs``).
- deleting or force-updating through ``gh``: ``gh repo delete``, an API DELETE of the repository or
  of ``refs/heads/main``, renaming ``main``, forced REST ref updates, GraphQL ``deleteRef`` and
  ``updateRef(s)`` with ``force: true``.
- configuring a force push for later: ``git -c``/``git config`` with ``remote.*.mirror``, a
  ``remote.*.push`` value starting with ``+``, an alias whose expansion is a blocked command,
  ``git remote add --mirror[=push]``.

How commands are found: a quote-aware tokenizer splits on ``;``, ``&&``, ``||``, ``|``, ``&``,
newlines, standalone braces, parentheses, ``$(...)`` and backticks; bash keywords (``if``,
``then``, ``do``, ``else``, ``elif``, ``while``, ``until``, ``!``, ``coproc``) are skipped as
prefixes; wrappers (``env``, ``timeout``, ``time``, ``xargs``, ``sudo`` ... with their options)
are skipped; ``bash -c``, ``sh -c``, ``cmd /c``, ``powershell -Command``/``-EncodedCommand``,
``eval``, ``iex``, ``Start-Process``, bash ``alias`` definitions, and here-documents, here-strings
or ``echo``/``printf`` output fed to a shell are checked as commands. Variables assigned earlier in
the same command, bash ``for`` lists and PowerShell ``foreach`` lists of quoted literals are
resolved; everything else is unresolved. Here-document bodies given to other commands
(``git commit -F - <<'EOF'``) and PowerShell here-strings are data, so a commit message that
quotes a force push does not block the commit. Arithmetic (``let x=1<<2``, ``(( ... ))``) is not a
here-document.

Known limits: deliberate obfuscation (above); scripts and programs that run git or delete files
themselves (``python -c "os.remove(...)"``); other output piped into a shell; aliases and functions
from other sessions or config files; ``git branch -m <new>`` renaming the current branch;
commands that move ``main`` without deleting it (``git branch -f main``, ``git switch -C main``,
``git checkout -B main``; a later push of the result is non-fast-forward and the pre-push guard
refuses it), ``git symbolic-ref -d HEAD`` and ``git reflog delete``; PowerShell scripts run with
``-File``; a ``find`` whose ``-o`` branch deletes without a test (``find . -name x -o -delete``),
also inside a group (``find . \\( -name x -o -mtime +30 \\) -delete``: the ``-mtime`` branch has
no name test, but the ``-name`` test still counts as narrowing);
deleting through a pipe (``ls .git/refs/heads | xargs rm``, ``Get-ChildItem .git -Recurse |
Remove-Item``); editing the ref files in place with ``sed -i`` or ``dd of=`` (``packed-refs``,
``refs/heads/main``); a ``find -exec`` whose remover sits behind a wrapper (``-exec env rm``,
``-exec command rm``); ``.git/objects`` and ``.git/HEAD``, which are not protected paths. These
are outside the threat model's literal scope or follow-ups (ticket 63 fix round 1). For pushes the
pre-push guard covers all of these.
"""

from __future__ import annotations

import base64
import fnmatch
import json
import os
import posixpath
import re
import sys
import time
from pathlib import Path

PROTECTED_BRANCH = "main"
ROOT = Path(__file__).resolve().parents[1]  # the repository this guard lives in

_BLOCK_PREFIX = (
    "BLOCKED by the kicad-mcp git guardrail (build ticket 45; playbook fork P2 and P8): "
)
_BLOCK_SUFFIX = (
    " Normal pushes are allowed; force pushes and deleting main are not. Do not look for a way"
    " around this guard: stop and report to the coordinator if you think the block is wrong."
)

# Wrapper commands that run the next word as the command, with their options that take a value.
_WRAPPERS: dict[str, set[str]] = {
    "env": {"-u", "--unset", "-C", "--chdir"},
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "--class", "--classdata", "--pid"},
    "xargs": {"-I", "-n", "-L", "-P", "-d", "-E", "-s", "-a", "--max-args", "--max-procs",
              "--delimiter", "--arg-file", "--max-lines", "--max-chars", "--eof", "--replace"},
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "--user", "--group"},
    "stdbuf": {"-i", "-o", "-e"},
    "exec": {"-a"},
    "time": {"-f", "-o", "--format", "--output"},
    "command": set(), "nohup": set(), "builtin": set(), "unbuffer": set(), ".": set(),
    "call": set(), "nocorrect": set(), "noglob": set(),
}
# Bash reserved words that may stand before a command in the same segment.
_KEYWORDS = {"if", "then", "do", "else", "elif", "while", "until", "!", "coproc", "{"}
_SHELLS = {"bash", "sh", "zsh", "dash", "ksh", "bash.exe", "sh.exe", "zsh.exe"}
_POWERSHELLS = {"powershell", "powershell.exe", "pwsh", "pwsh.exe"}
_CMD = {"cmd", "cmd.exe"}
_EVALS = {"eval", "iex", "invoke-expression"}
_START_PROCESS = {"start-process", "saps", "start"}
_START_PROCESS_PARAMS = (
    "filepath", "argumentlist", "credential", "workingdirectory", "loaduserprofile",
    "nonewwindow", "passthru", "redirectstandarderror", "redirectstandardinput",
    "redirectstandardoutput", "verb", "windowstyle", "wait", "usenewenvironment", "environment",
)
_REMOVERS = {"rm", "del", "erase", "remove-item", "ri", "rmdir", "unlink", "mv", "move", "move-item"}
_ENV_ASSIGN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\[[^\]]*\])?\+?=.*", re.S)
_SIMPLE_REF = re.compile(r"\$\{([A-Za-z_]\w*)\}|\$([A-Za-z_]\w*(?::[A-Za-z_]\w*)?)")
_ZERO_OID = re.compile(r"0{40}(?:0{24})?")
_MAX_DEPTH = 8


class Word(str):
    """A shell word.

    ``var``: it holds a simple variable reference (``$X``, ``${X}``) not expanded yet, or, after
    expansion, one that could not be resolved. ``opaque``: it holds something the guard does not
    evaluate (command substitution, ``${x:-y}``, ``$1``, brace expansion, a PowerShell expression).
    ``quoted``: some of it came from quotes. ``bare``: some of it is unquoted literal text.
    """

    var: bool = False
    opaque: bool = False
    quoted: bool = False
    bare: bool = False

    @property
    def unresolved(self) -> bool:
        return self.var or self.opaque


def _word(text: str, *, var: bool = False, opaque: bool = False, quoted: bool = False,
          bare: bool = False) -> Word:
    w = Word(text)
    w.var, w.opaque, w.quoted, w.bare = var, opaque, quoted, bare
    return w


class Seg(list):
    """The words of one simple command, plus the here-document and here-string bodies fed to it."""

    def __init__(self) -> None:
        super().__init__()
        self.bodies: list[str] = []
        self.piped_from: Seg | None = None
        self.redirects: list[Word] = []  # targets of > and >>
        self.stdin_redirect = False  # < file: input the guard cannot see


# --------------------------------------------------------------------------- tokenizer


class _Tokens:
    def __init__(self) -> None:
        self.segments: list[Seg] = [Seg()]
        self.nested: list[str] = []  # command texts from $(...), backticks and expressions
        self._tok: str | None = None
        self._flags = {"var": False, "opaque": False, "quoted": False, "bare": False}
        self._skip_word = False  # the next word is a redirection target
        self._redirect_in = False  # ... of an input redirection
        self._capture_word = False  # the next word is a here-string body

    def add(self, s: str, *, quoted: bool) -> None:
        self._tok = (self._tok or "") + s
        self._flags["quoted" if quoted else "bare"] = True

    def start(self, **flags: bool) -> None:
        if self._tok is None:
            self._tok = ""
        for k, v in flags.items():
            self._flags[k] |= v

    def flush(self) -> None:
        if self._tok is not None:
            if self._capture_word:
                self.segments[-1].bodies.append(self._tok)
            elif self._skip_word and self._redirect_in:
                self.segments[-1].stdin_redirect = True
            elif self._skip_word:
                self.segments[-1].redirects.append(_word(self._tok, **self._flags))
            else:
                self.segments[-1].append(_word(self._tok, **self._flags))
            self._skip_word = self._capture_word = False
        self._tok = None
        self._flags = {"var": False, "opaque": False, "quoted": False, "bare": False}

    def new_segment(self) -> None:
        self.flush()
        self._skip_word = self._capture_word = False
        if self.segments[-1] or self.segments[-1].bodies:
            self.segments.append(Seg())

    def skip_next_word(self, capture: bool = False, redirect_in: bool = False) -> None:
        self.flush()
        self._skip_word, self._capture_word = not capture, capture
        self._redirect_in = redirect_in


def _heredoc_start(text: str, i: int) -> tuple[tuple[str, bool] | None, int]:
    """At ``<<`` (not ``<<<``): the (delimiter, strip_tabs) pair and the index after it."""
    i += 2
    strip_tabs = text.startswith("-", i)
    i += 1 if strip_tabs else 0
    while i < len(text) and text[i] in " \t":
        i += 1
    m = re.match(r"'([^']*)'|\"([^\"]*)\"|\\?([^\s;&|<>()]+)", text[i:])
    if not m:
        return None, i
    return (next(g for g in m.groups() if g is not None), strip_tabs), i + m.end()


def _read_heredoc_bodies(text: str, i: int, heredocs: list[tuple[str, bool]]) -> tuple[list[str], int]:
    """The bodies of the pending here-documents whose first line starts at ``i``, and the index after."""
    bodies = []
    for delimiter, strip_tabs in heredocs:
        lines = []
        while i < len(text):
            end = text.find("\n", i)
            end = len(text) if end < 0 else end
            line = text[i:end].rstrip("\r")
            i = end + 1
            if (line.lstrip("\t") if strip_tabs else line) == delimiter:
                break
            lines.append(line)
        bodies.append("\n".join(lines))
    return bodies, i


def _subst_end(text: str, i: int, bash: bool) -> int:
    """Index of the ``)`` that closes a ``(`` whose body starts at ``i`` (``len(text)`` if none).
    Quotes, escapes, nested ``$(`` and here-document bodies do not count as parentheses."""
    depth = 1
    heredocs: list[tuple[str, bool]] = []
    n = len(text)
    while i < n:
        c = text[i]
        if c == "\n" and heredocs:
            _, i = _read_heredoc_bodies(text, i + 1, heredocs)
            heredocs = []
            continue
        if bash and c == "\\":
            i += 2
            continue
        if c == "'":
            end = text.find("'", i + 1)
            i = n if end < 0 else end + 1
            continue
        if c == '"':
            j = i + 1
            while j < n and text[j] != '"':
                if bash and text[j] == "\\":
                    j += 2
                    continue
                if not bash and text[j] == "`":
                    j += 2
                    continue
                if text.startswith("$(", j):
                    j = _subst_end(text, j + 2, bash) + 1
                    continue
                j += 1
            i = j + 1
            continue
        if bash and text.startswith("<<", i) and not text.startswith("<<<", i):
            hd, i = _heredoc_start(text, i)
            if hd:
                heredocs.append(hd)
            continue
        if text.startswith("$(", i):
            i = _subst_end(text, i + 2, bash) + 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _dollar(text: str, i: int, bash: bool) -> tuple[int, str]:
    """At ``$``: (end index, kind) with kind ``var`` (a simple reference), ``opaque`` or ``literal``."""
    rest = text[i:]
    if bash and rest.startswith("$'"):
        j = i + 2
        while j < len(text) and text[j] != "'":
            j += 2 if text[j] == "\\" else 1
        return min(j + 1, len(text)), "opaque"
    if rest.startswith("${"):
        end = text.find("}", i)
        end = len(text) if end < 0 else end + 1
        inner = text[i + 2 : end - 1]
        simple = r"[A-Za-z_]\w*" if bash else r"[A-Za-z_][\w:]*"
        return end, "var" if re.fullmatch(simple, inner) else "opaque"
    m = re.match(r"\$[A-Za-z_]\w*" if bash else r"\$[A-Za-z_]\w*(?::[A-Za-z_]\w*)?", rest)
    if m:
        return i + m.end(), "var"
    if bash and re.match(r"\$[0-9@*#?$!-]", rest):
        return i + 2, "opaque"
    return i + 1, "literal"


def _brace_end(text: str, i: int) -> int:
    depth = 0
    j = i
    while j < len(text):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j + 1
        elif text[j] in " \t\n;|&":
            return j
        j += 1
    return j


def tokenize(text: str, shell: str = "bash") -> tuple[list[Seg], list[str]]:
    """Split ``text`` into command segments of words, quote-aware.

    Returns (segments, nested): ``nested`` holds the command texts of ``$(...)``, backtick
    substitutions and PowerShell expressions, to be checked on their own.
    """
    bash = shell != "powershell"
    t = _Tokens()
    heredocs: list[tuple[str, bool, Seg]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        seg = t.segments[-1]
        at_command_start = t._tok is None and not seg
        if c in " \t\r":
            t.flush()
            i += 1
        elif c == "\n" and heredocs:
            t.new_segment()
            bodies, i = _read_heredoc_bodies(text, i + 1, [(d, s) for d, s, _ in heredocs])
            for (_, _, owner), body in zip(heredocs, bodies, strict=True):
                owner.bodies.append(body)
            heredocs = []
        elif not bash and c == "@" and text[i + 1 : i + 2] in ("'", '"') and t._tok is None and (
            text[i + 2 : i + 3] == "\n" or text[i + 2 : i + 4] == "\r\n"
        ):
            # PowerShell here-string: data up to a line that starts with '@ or "@.
            close = "\n" + text[i + 1] + "@"
            end = text.find(close, i + 2)
            end = n if end < 0 else end
            body = text[i + 2 : end].lstrip("\r\n")
            t.start(opaque=text[i + 1] == '"' and "$" in body)
            t.add(body, quoted=True)
            i = end + len(close)
        elif bash and text.startswith("$((", i):
            end = text.find("))", i + 3)
            end = n if end < 0 else end + 2
            t.start(opaque=True)  # arithmetic expansion
            i = end
        elif text.startswith("$(", i):
            end = _subst_end(text, i + 2, bash)
            t.nested.append(text[i + 2 : end])
            t.start(opaque=True)  # the substitution's output is part of this word
            i = end + 1
        elif bash and at_command_start and text.startswith("((", i):
            end = text.find("))", i + 2)
            i = n if end < 0 else end + 2  # arithmetic command: no command, no here-document
        elif c == "(" and bash and t._tok is not None and t._tok.endswith("="):
            end = _subst_end(text, i + 1, bash)  # array assignment a=(...)
            t.add(text[i : end + 1], quoted=False)
            t.start(opaque=True)
            i = end + 1
        elif c == "(" and not bash and t._tok is None and len(seg) == 1 and seg[0].lower() == "foreach":
            # foreach ($t in 'a','b'): the loop variable and its list, like a bash for loop.
            end = _subst_end(text, i + 1, bash)
            m = re.fullmatch(r"\s*(\$[A-Za-z_]\w*)\s+in\s+(.*?)\s*", text[i + 1 : end], re.S | re.I)
            if m:
                seg.append(_word(m.group(1)))
                seg.append(_word("in"))
                inner, _ = tokenize(m.group(2), "powershell")
                seg.extend(w for s in inner for w in s)
            else:
                seg.append(_word("(...)", opaque=True))
            i = end + 1
        elif c == "(" and not bash and not at_command_start:
            end = _subst_end(text, i + 1, bash)  # PowerShell expression as an argument
            t.nested.append(text[i + 1 : end])
            t.start(opaque=True)  # also a method call on this word: 'x'.ToLower()
            i = end + 1
        elif not bash and c == "@" and text[i + 1 : i + 2] in ("(", "{"):
            close = ")" if text[i + 1] == "(" else "}"
            end = _subst_end(text, i + 2, bash) if close == ")" else _brace_end(text, i + 1) - 1
            t.nested.append(text[i + 2 : end])
            t.start(opaque=True)
            i = end + 1
        elif c == "{" and bash and not (t._tok is None and (i + 1 >= n or text[i + 1] in " \t\r\n")):
            end = _brace_end(text, i)  # brace expansion inside a word
            part = text[i:end]
            t.add(part, quoted=False)
            if "," in part or ".." in part:
                t.start(opaque=True)
            i = end
        elif c == "}" and bash and t._tok is not None:
            t.add(c, quoted=False)
            i += 1
        elif c in ";\n(){}":
            t.new_segment()
            i += 1
        elif bash and text.startswith("<<<", i):
            t.flush()
            i += 3
            while i < n and text[i] in " \t":
                i += 1
            t.skip_next_word(capture=True)
        elif bash and text.startswith("<<", i):
            t.flush()
            if seg and seg[0] == "let":
                t.add("<<", quoted=False)  # arithmetic shift, not a here-document
                i += 2
                continue
            hd, i = _heredoc_start(text, i)
            if hd:
                heredocs.append((hd[0], hd[1], seg))
        elif c in "&|":
            t.new_segment()
            if c == "|" and text[i : i + 2] != "||" and len(t.segments) >= 2 and not t.segments[-1]:
                t.segments[-1].piped_from = t.segments[-2]  # cmd | bash: its output is the input
            i += 2 if text[i : i + 2] in ("&&", "||") else 1
            if not bash and c == "&":
                while i < n and text[i] in " \t":
                    i += 1
                if text.startswith("(", i):
                    end = _subst_end(text, i + 1, bash)  # & (Get-Command git) ...
                    t.nested.append(text[i + 1 : end])
                    t.start(opaque=True)
                    i = end + 1
        elif c in "<>":
            # Redirection: drop an fd-number token right before it; keep the target aside (an
            # output target is checked like a written file, an input makes stdin unknown).
            if t._tok is not None and re.fullmatch(r"\d*|\*", t._tok):
                t._tok = None
            t.flush()
            redirect_in = c == "<"
            while i < n and text[i] in "<>&":
                i += 1
            while i < n and text[i].isdigit():
                i += 1
            if text[i - 1 : i].isdigit() and text[i - 2 : i - 1] == "&":
                continue  # 2>&1: no target word
            t.skip_next_word(redirect_in=redirect_in)
        elif c == "," and not bash:
            t.flush()  # PowerShell array: 'push','--force' are two arguments
            i += 1
        elif c == "`" and bash:
            end = text.find("`", i + 1)
            end = n if end < 0 else end
            t.nested.append(text[i + 1 : end])
            t.start(opaque=True)
            i = end + 1
        elif c == "`":  # PowerShell escape: next char literal
            if i + 1 < n:
                t.add(text[i + 1], quoted=True)
            i += 2
        elif c == "\\" and bash:
            if i + 1 < n and text[i + 1] == "\n":
                i += 2  # line continuation
                continue
            if i + 1 < n:
                t.add(text[i + 1], quoted=True)
            i += 2
        elif c == "$":
            end, kind = _dollar(text, i, bash)
            if kind == "literal":
                t.add("$", quoted=False)
            else:
                t.start(**{kind: True})
                t.add(text[i:end], quoted=False)
            i = end
        elif c == "@" and not bash and t._tok is None and re.match(r"@[A-Za-z_]\w*", text[i:]):
            m = re.match(r"@[A-Za-z_]\w*", text[i:])
            t.start(var=True)  # splatting: @args is resolved only from a known list
            t.add("$" + m.group(0)[1:], quoted=False)
            i += m.end()
        elif c == "~" and bash and t._tok is None:
            t.start(opaque=True)  # tilde expansion (~+, ~-, ~user)
            t.add(c, quoted=False)
            i += 1
        elif c == "'":
            t.start(quoted=True)
            j = i + 1
            while j < n:
                if text[j] == "'":
                    if not bash and text[j + 1 : j + 2] == "'":
                        t.add("'", quoted=True)
                        j += 2
                        continue
                    break
                t.add(text[j], quoted=True)
                j += 1
            i = j + 1
        elif c == '"':
            t.start(quoted=True)
            j = i + 1
            while j < n and text[j] != '"':
                ch = text[j]
                if bash and ch == "\\" and j + 1 < n and text[j + 1] in '"\\$`\n':
                    t.add(text[j + 1], quoted=True)
                    j += 2
                    continue
                if not bash and ch == "`" and j + 1 < n:
                    t.add(text[j + 1], quoted=True)
                    j += 2
                    continue
                if text.startswith("$(", j):
                    end = _subst_end(text, j + 2, bash)
                    t.nested.append(text[j + 2 : end])
                    t.start(opaque=True)
                    j = end + 1
                    continue
                if bash and ch == "`":
                    end = text.find("`", j + 1)
                    end = n if end < 0 else end
                    t.nested.append(text[j + 1 : end])
                    t.start(opaque=True)
                    j = end + 1
                    continue
                if ch == "$":
                    end, kind = _dollar(text, j, bash)
                    if kind == "literal" or (kind == "opaque" and text.startswith("$'", j)):
                        t.add("$", quoted=True)
                        j += 1
                        continue
                    t.start(**{kind: True})
                    t.add(text[j:end], quoted=True)
                    j = end
                    continue
                t.add(ch, quoted=True)
                j += 1
            i = j + 1
        else:
            t.add(c, quoted=False)
            i += 1
    t.flush()
    return [s for s in t.segments if s or s.bodies], t.nested


# --------------------------------------------------------------------------- variables


class _Ctx:
    def __init__(self, shell: str, depth: int) -> None:
        self.shell = shell
        self.depth = depth
        self.vars: dict[str, list[str]] = {}  # a missing name is unknown, never empty
        self.aliases: dict[str, str] = {}
        self.cwd: str | None = ""  # directory changes within this command line; None: unknown
        self.stdin: tuple[str, str] = ("none", "")  # the current segment's input: none/literal/unknown

    def key(self, name: str) -> str:
        return name.lower() if self.shell == "powershell" else name

    def forget(self, name: str) -> None:
        self.vars.pop(self.key(name.lstrip("$")), None)


_CONSTANTS = {"true": ["True"], "false": ["False"], "null": [""]}


def _lookup(ctx: _Ctx, name: str) -> list[str] | None:
    if ctx.shell == "powershell" and name.lower() in _CONSTANTS:
        return _CONSTANTS[name.lower()]
    return ctx.vars.get(ctx.key(name))


def _expand(word: Word, ctx: _Ctx) -> list[Word]:
    """Expand simple variable references; anything else unresolved stays marked."""
    if word.opaque:
        return [_word(word, var=True, opaque=True, quoted=word.quoted, bare=word.bare)]
    if not word.var:
        return [word]
    whole = _SIMPLE_REF.fullmatch(word)
    if whole:
        value = _lookup(ctx, whole.group(1) or whole.group(2))
        if value is None:
            return [_word(word, var=True, quoted=word.quoted, bare=word.bare)]
        return [_word(v, quoted=True) for v in value]
    unresolved = False

    def rep(m: re.Match) -> str:
        nonlocal unresolved
        value = _lookup(ctx, m.group(1) or m.group(2))
        if value is None:
            unresolved = True
            return m.group(0)
        return " ".join(value)

    return [_word(_SIMPLE_REF.sub(rep, word), var=unresolved, quoted=word.quoted, bare=word.bare)]


def _record_assignment(seg: Seg, ctx: _Ctx) -> bool:
    """Record or forget variables. True if ``seg`` only assigns (it then runs no command)."""
    if not seg:
        return False
    if ctx.shell == "powershell":
        m = re.fullmatch(r"\$([A-Za-z_]\w*(?::[A-Za-z_]\w*)?)(\W?)=(.*)", seg[0], re.S)
        if re.fullmatch(r"\$[A-Za-z_]\w*(?::[A-Za-z_]\w*)?", seg[0]) and len(seg) >= 2 and re.fullmatch(r"\W?=", seg[1]):
            name, op, values = seg[0][1:], seg[1][:-1], list(seg[2:])
        elif m:
            name, op = m.group(1), m.group(2)
            values = ([_word(m.group(3), quoted=seg[0].quoted, bare=seg[0].bare)] if m.group(3) else []) + list(seg[1:])
        elif seg[0].lower() == "foreach" and len(seg) >= 3 and seg[2].lower() == "in":
            name, op, values = seg[1].lstrip("$"), "", list(seg[3:])
        else:
            if seg[0].lower() in ("set-variable", "new-variable", "sv", "set") or seg[0].lower().startswith("clear-variable"):
                for w in seg[1:]:
                    if not w.startswith("-"):
                        ctx.forget(w)
            return False
        expanded = [v for w in values for v in _expand(w, ctx)]
        # Resolved only for a plain list of quoted literals: 'a' or 'a','b'.
        if op or not expanded or any(v.unresolved or v.bare or not v.quoted for v in expanded):
            ctx.forget(name)
        else:
            ctx.vars[ctx.key(name)] = [str(v) for v in expanded]
        return True
    words = list(seg)
    if words[0] in ("for", "select") and len(words) >= 2:
        name = words[1]
        if len(words) >= 3 and words[2] == "in":
            expanded = [v for w in words[3:] for v in _expand(w, ctx)]
            if any(v.unresolved for v in expanded):
                ctx.forget(name)
            else:
                ctx.vars[name] = [str(v) for v in expanded]
        else:
            ctx.forget(name)
        return True
    if words[0] in ("read", "mapfile", "readarray"):
        for w in words[1:]:
            if not w.startswith("-") and re.fullmatch(r"[A-Za-z_]\w*", w):
                ctx.forget(w)
        return False
    if words[0] == "printf":
        for k, w in enumerate(words[:-1]):
            if w == "-v":
                ctx.forget(words[k + 1])
        return False
    if words[0] in ("unset",):
        for w in words[1:]:
            ctx.forget(w)
        return True
    while words and words[0] in ("export", "local", "declare", "readonly", "typeset"):
        words = [w for w in words[1:] if not w.startswith("-")]
    if not words or not all(_ENV_ASSIGN.fullmatch(w) and w.bare for w in words):
        return not words
    for w in words:
        m = re.match(r"([A-Za-z_]\w*)(\[[^\]]*\])?(\+?)=(.*)", w, re.S)
        name, index, plus, value = m.group(1), m.group(2), m.group(3), m.group(4)
        if index or plus or w.opaque:
            ctx.forget(name)
            continue
        expanded = _expand(_word(value, var=w.var, quoted=w.quoted, bare=w.bare), ctx)
        if any(v.unresolved for v in expanded):
            ctx.forget(name)
        else:
            ctx.vars[name] = [p for v in expanded for p in (str(v).split() or [""])]
    return True


# --------------------------------------------------------------------------- checks


def _base(word: str) -> str:
    return re.split(r"[\\/]", word)[-1].lower()


def _is_main(ref: str) -> bool:
    ref = ref.lstrip("+").casefold()
    return ref in (PROTECTED_BRANCH, f"refs/heads/{PROTECTED_BRANCH}", f"heads/{PROTECTED_BRANCH}")


def _is_head(ref: str) -> bool:
    return ref.casefold() in ("head", "@")


def _unknown_ref(word: Word) -> bool:
    """A branch or ref argument whose target the guard cannot name: a variable or expression, or
    a reflog/previous-branch form such as @{-1}."""
    return word.unresolved or "@{" in word


# 1. The pre-push guard switched off in plain sight.
_HOOKS_PATH_KEY = "core.hookspath"


def _env_reason(name: str, value: str) -> str | None:
    """Environment assignments that set core.hooksPath for git (GIT_CONFIG_KEY_<n>,
    GIT_CONFIG_PARAMETERS)."""
    n = name.upper()
    if (n.startswith("GIT_CONFIG_KEY_") or n == "GIT_CONFIG_PARAMETERS") and _HOOKS_PATH_KEY in value.casefold():
        return f"{name} sets core.hooksPath, which switches the pre-push guard off"
    return None


def _segment_env_reason(seg: Seg) -> str | None:
    for k, w in enumerate(seg):
        m = re.fullmatch(r"(?:\$env:)?([A-Za-z_]\w*)=(.*)", w, re.S | re.I)
        if m and (w.bare or w.lower().startswith("$env:")):
            reason = _env_reason(m.group(1), m.group(2))
            if reason:
                return reason
        m = re.fullmatch(r"\$env:([A-Za-z_]\w*)", w, re.I)
        if m and k + 1 < len(seg) and seg[k + 1] == "=":
            reason = _env_reason(m.group(1), " ".join(seg[k + 2 :]))
            if reason:
                return reason
    return None


def _abbrev(name: str, full: str) -> bool:
    """True when ``name`` is ``full`` or one of git's abbreviations of it (any non-empty prefix)."""
    return bool(name) and full.startswith(name)


def _split_simple(text: str) -> list[Word]:
    segs, _ = tokenize(text, "bash")
    return [w for s in segs for w in s]


def _alias_reason(value: str, ctx: _Ctx) -> str | None:
    v = value.strip()
    if v.startswith("!"):
        return check_command(v[1:], "bash", ctx.depth + 1)
    words = _split_simple(v)
    if words and words[0].lower() == "push":
        return _check_push(words[1:])
    if words:
        return _check_git(words, ctx)
    return None


def _check_config_pair(key: str, value: str | None, ctx: _Ctx) -> str | None:
    k = key.lower()
    v = (value or "").strip()
    if k == _HOOKS_PATH_KEY:
        return "setting core.hooksPath switches the pre-push guard off"
    if re.fullmatch(r"remote\..+\.mirror", k) and v.lower() not in ("false", "no", "off", "0"):
        return f"setting {key} makes every push a mirror push (forced, with deletions)"
    if re.fullmatch(r"remote\..+\.push", k) and v.startswith("+"):
        return f"setting {key}={v} makes every push a force push"
    if k.startswith("alias.") and v:
        reason = _alias_reason(v, ctx)
        if reason:
            return f"alias {key[6:]} expands to a blocked command: {reason}"
    return None


_PUSH_FORCE = {
    "force": "force push (--force)",
    "force-with-lease": "force push (--force-with-lease)",
    "force-if-includes": "force push (--force-if-includes)",
    "mirror": "mirror push (forced, with deletions)",
}
_PUSH_VALUE_OPTS = ("repo", "receive-pack", "exec", "push-option")


def _check_push(args: list[Word]) -> str | None:
    """Best effort (the pre-push guard is authoritative): blocks the force and delete forms it can
    see; arguments it cannot resolve are allowed."""
    positional: list[str] = []
    delete = False
    i = 0
    while i < len(args):
        a = args[i]
        if a == "--":
            positional += args[i + 1 :]
            break
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0].lower()
            if len(name) >= len("no-veri") and "no-verify".startswith(name):
                return "push --no-verify switches the pre-push guard off"
            if name.startswith("no-"):
                i += 1  # --no-force-with-lease and friends only switch things off
                continue
            for full, reason in _PUSH_FORCE.items():
                if _abbrev(name, full):
                    return reason
            if _abbrev(name, "delete"):
                delete = True
            elif "=" not in a and any(_abbrev(name, o) and len(name) >= 2 for o in _PUSH_VALUE_OPTS):
                i += 1  # option value in the next word
        elif a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            for k, ch in enumerate(cluster):
                if ch == "f":
                    return "force push (-f)"
                if ch == "d":
                    delete = True
                if ch == "o":
                    if k == len(cluster) - 1:
                        i += 1  # -o <option>
                    break
        else:
            positional.append(a)
        i += 1
    for ref in positional:
        if ref.startswith("+"):
            return f"force push (refspec {ref})"
        if ":" in ref:
            src, dst = ref.split(":", 1)
            if src == "" and _is_main(dst):
                return f"deleting {PROTECTED_BRANCH} on the remote (refspec {ref})"
    if delete and any(_is_main(r) for r in positional[1:]):
        return f"deleting {PROTECTED_BRANCH} on the remote (push --delete)"
    return None


# `commit`'s options that take a value: the rest of a short cluster, or the next word, is that value.
_COMMIT_SHORT_VALUE = set("mFCctu")
_COMMIT_LONG_VALUE = ("message", "file", "reuse-message", "reedit-message", "template", "author", "date",
                      "cleanup", "fixup", "squash", "trailer", "pathspec-from-file")


def _check_no_verify(sub: str, args: list[Word]) -> str | None:
    """``commit --no-verify`` / ``-n`` (also ``merge`` and ``rebase`` ``--no-verify``) skips the
    identity guard (``scripts/git-hooks/commit-msg``) just as ``push --no-verify`` skips the
    pre-push guard. Best effort: words it cannot resolve are not chased."""
    i = 0
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            return None
        if a.unresolved:
            continue
        if a.startswith("--"):
            name = a[2:].split("=", 1)[0].lower()
            if len(name) >= len("no-v") and "no-verify".startswith(name):
                return f"{sub} --no-verify switches the identity guard off"
            if "=" not in a and sub == "commit" and any(_abbrev(name, o) and len(name) >= 2 for o in _COMMIT_LONG_VALUE):
                i += 1  # option value in the next word
        elif sub == "commit" and a.startswith("-") and len(a) > 1:
            cluster = a[1:]
            for k, ch in enumerate(cluster):
                if ch == "n":
                    return "commit -n (--no-verify) switches the identity guard off"
                if ch in _COMMIT_SHORT_VALUE:
                    if k == len(cluster) - 1:
                        i += 1  # -m <message>
                    break
    return None


def _check_branch(args: list[Word]) -> str | None:
    delete = move = copy = force = False
    positional: list[Word] = []
    for a in args:
        if _unknown_ref(a):
            positional.append(a)
        elif a.startswith("--"):
            name = a[2:].split("=", 1)[0].lower()
            delete |= _abbrev(name, "delete")
            move |= _abbrev(name, "move")
            copy |= _abbrev(name, "copy")
            force |= _abbrev(name, "force")
        elif a.startswith("-") and len(a) > 1:
            for ch in a[1:]:
                if ch in "dD":
                    delete = True
                    force |= ch == "D"
                elif ch in "mM":
                    move = True
                    force |= ch == "M"
                elif ch in "cC":
                    copy = True
                    force |= ch == "C"
                elif ch == "f":
                    force = True
        else:
            positional.append(a)
    unresolved = [p for p in positional if _unknown_ref(p)]
    if unresolved and (delete or move or copy or any(_is_main(p) for p in positional)):
        return (f"git branch with an argument the guard cannot resolve ({unresolved[0]}) where it could"
                f" delete or overwrite {PROTECTED_BRANCH}; write branch names and flags literally")
    if delete and any(_is_main(p) for p in positional):
        return f"deleting the local {PROTECTED_BRANCH} branch"
    if move and len(positional) >= 2 and _is_main(positional[0]):
        return f"renaming {PROTECTED_BRANCH} away (git branch -m {PROTECTED_BRANCH} ...)"
    if (move or copy) and force and positional and _is_main(positional[-1]):
        return f"overwriting {PROTECTED_BRANCH} with another branch (forced move/copy)"
    return None


_UPDATE_REF_STDIN = {"update", "create", "delete", "verify", "option", "start", "prepare", "commit",
                     "abort", "symref-update", "symref-create", "symref-delete", "symref-verify"}


def _check_update_ref_stdin(args: list[Word], stdin: tuple[str, str]) -> str | None:
    """``git update-ref --stdin``: only a literal input whose every line can be read and touches
    neither main nor HEAD passes."""
    kind, text = stdin
    if kind == "unknown":
        return "git update-ref --stdin with input the guard cannot see (pipe or file)"
    if kind == "none":
        return None
    if any(a == "-z" for a in args):
        tokens = text.replace("\0", " ").split()
        if any(_is_main(t) or _is_head(t) for t in tokens):
            return f"git update-ref --stdin -z input names {PROTECTED_BRANCH} or HEAD"
        return None
    for line in text.splitlines():
        parts = line.split()
        if not parts:
            continue
        command = parts[0].casefold()
        if command not in _UPDATE_REF_STDIN:
            return f"git update-ref --stdin input the guard cannot parse: {line.strip()!r}"
        if command in ("update", "create", "delete", "symref-update", "symref-create", "symref-delete"):
            if len(parts) < 2 or _is_main(parts[1]) or _is_head(parts[1]):
                return f"git update-ref --stdin changes or deletes {PROTECTED_BRANCH} or HEAD: {line.strip()!r}"
    return None


def _check_update_ref(args: list[Word], stdin: tuple[str, str] = ("none", "")) -> str | None:
    if any(not a.unresolved and a.startswith("--") and len(a) > 2 and _abbrev(a[2:], "stdin") for a in args):
        return _check_update_ref_stdin(args, stdin)
    delete = any(not a.unresolved and (a == "-d" or (a.startswith("--") and _abbrev(a[2:], "delete")))
                 for a in args)
    refs = [a for a in args if _unknown_ref(a) or not a.startswith("-")]
    targets_main = any(not _unknown_ref(r) and (_is_main(r) or _is_head(r)) for r in refs)
    if any(_unknown_ref(a) for a in args) and (delete or targets_main):
        return "git update-ref with an argument the guard cannot resolve; write refs and flags literally"
    if delete and targets_main:
        return f"deleting {PROTECTED_BRANCH} (or the branch HEAD points to) with update-ref -d"
    if targets_main and any(_ZERO_OID.fullmatch(r) for r in refs):
        return f"deleting {PROTECTED_BRANCH} (or the branch HEAD points to) with update-ref and a zero id"
    return None


# `git config` options that take a value in the next word (git 2.55 `git config -h` and
# `git config set -h`), and its long options without one, for git's unique-prefix abbreviations.
_CONFIG_VALUE_LONG = ("file", "blob", "type", "value", "comment", "default", "url")
_CONFIG_VALUE_SHORT = ("-f", "-t")
_CONFIG_FLAG_LONG = (
    "global", "system", "local", "worktree", "bool", "int", "bool-or-int", "bool-or-str", "path",
    "expiry-date", "all", "fixed-value", "includes", "null", "name-only", "show-origin",
    "show-scope", "regexp", "get", "get-all", "get-regexp", "get-urlmatch", "get-color",
    "get-colorbool", "replace-all", "add", "unset", "unset-all", "rename-section",
    "remove-section", "list", "edit",
)


def _config_takes_value(opt: str) -> bool:
    """True when ``opt`` is a `git config` option whose value is the next word."""
    if opt in _CONFIG_VALUE_SHORT:
        return True
    if not opt.startswith("--") or "=" in opt:
        return False
    name = opt[2:].lower()
    return (any(_abbrev(name, v) for v in _CONFIG_VALUE_LONG)
            and not any(_abbrev(name, f) for f in _CONFIG_FLAG_LONG))


def _check_config(args: list[Word], ctx: _Ctx) -> str | None:
    words: list[Word] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("-"):
            i += 2 if _config_takes_value(a) else 1  # skip the option and its value
            continue
        words.append(a)
        i += 1
    if words and words[0].lower() in ("set", "unset", "get", "list", "edit", "rename-section", "remove-section"):
        if words[0].lower() != "set":
            return None
        words = words[1:]
    if len(words) >= 2:
        return _check_config_pair(words[0], " ".join(words[1:]), ctx)
    return None


def _check_remote(args: list[Word]) -> str | None:
    if not args or args[0].lower() != "add":
        return None
    for a in args[1:]:
        if a.startswith("--"):
            name, _, value = a[2:].partition("=")
            if _abbrev(name.lower(), "mirror") and value.lower() != "fetch":
                return "git remote add --mirror makes every push to that remote a mirror push (forced)"
    return None


_GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env", "--super-prefix"}


def _check_git(argv: list[Word], ctx: _Ctx) -> str | None:
    aliases: dict[str, str] = {}
    i = 0
    while i < len(argv) and argv[i].startswith("-") and not argv[i].unresolved:
        opt = argv[i]
        value: str | None = None
        if opt in _GIT_VALUE_OPTS:
            value = argv[i + 1] if i + 1 < len(argv) else ""
            i += 1
        elif opt.startswith("-c") and len(opt) > 2:
            value, opt = opt[2:], "-c"
        elif opt.startswith("--config-env="):
            value, opt = opt.split("=", 1)[1], "--config-env"
        if opt == "--config-env" and value is not None and value.partition("=")[0].lower() == _HOOKS_PATH_KEY:
            return "--config-env sets core.hooksPath, which switches the pre-push guard off"
        if opt == "-c" and value is not None:
            key, _, val = value.partition("=")
            if key.lower().startswith("alias."):
                aliases[key[6:].lower()] = val
            reason = _check_config_pair(key, val, ctx)
            if reason:
                return reason
        i += 1
    if i >= len(argv):
        return None
    if argv[i].unresolved:
        return "git subcommand from a variable or expression the guard cannot resolve; write it literally"
    sub, rest = argv[i].lower(), argv[i + 1 :]
    if sub in aliases:
        if ctx.depth >= _MAX_DEPTH:
            return "alias expansion nests too deeply for the guard to check"
        value = aliases[sub].strip()
        inner = _Ctx(ctx.shell, ctx.depth + 1)
        if value.startswith("!"):
            return check_command(value[1:] + " " + " ".join(rest), "bash", ctx.depth + 1)
        return _check_git(_split_simple(value) + rest, inner)
    if sub == "push":
        return _check_push(rest)
    if sub in ("commit", "merge", "rebase"):
        return _check_no_verify(sub, rest)
    if sub == "branch":
        return _check_branch(rest)
    if sub == "update-ref":
        return _check_update_ref(rest, ctx.stdin)
    if sub == "config":
        return _check_config(rest, ctx)
    if sub == "remote":
        return _check_remote(rest)
    return None


_GH_VALUE_OPTS = {"-H", "--header", "--input", "-q", "--jq", "-t", "--template", "--hostname",
                  "--cache", "-p", "--preview"}


def _check_gh(argv: list[Word]) -> str | None:
    if not argv:
        return None
    a0 = argv[0].lower()
    if a0 == "repo" and len(argv) > 1 and argv[1].lower() == "delete":
        return f"deleting the repository (and {PROTECTED_BRANCH} with it)"
    if a0 != "api":
        return None
    method: str | None = None
    fields: list[str] = []
    endpoint: str | None = None
    i = 1
    while i < len(argv):
        a = argv[i]
        if a in ("-X", "--method") and i + 1 < len(argv):
            method = argv[i + 1].upper()
            i += 2
            continue
        if a.startswith("--method="):
            method = a.split("=", 1)[1].upper()
        elif a.startswith("-X") and len(a) > 2:
            method = a[2:].upper()
        elif a in ("-f", "-F", "--field", "--raw-field") and i + 1 < len(argv):
            fields.append(argv[i + 1])
            i += 2
            continue
        elif a.startswith(("--field=", "--raw-field=")):
            fields.append(a.split("=", 1)[1])
        elif a in _GH_VALUE_OPTS:
            i += 2
            continue
        elif not a.startswith("-") and endpoint is None:
            endpoint = a
        i += 1
    if endpoint is None:
        return None
    if endpoint.lower() == "graphql":
        q = re.sub(r"\s+", "", " ".join(fields)).lower()
        if "deleteref" in q:
            return "deleting a branch through the GitHub GraphQL API (deleteRef)"
        if re.search(r"updaterefs?\(", q) and "force:true" in q:
            return "force-updating a branch through the GitHub GraphQL API"
        return None
    method = method or ("POST" if fields else "GET")
    ep = endpoint.lower().strip("/")
    forced = any(re.sub(r"\s+", "", f).lower() == "force=true" for f in fields)
    if method == "DELETE" and re.fullmatch(r"repos/[^/]+/[^/]+", ep):
        return f"deleting the repository (and {PROTECTED_BRANCH} with it)"
    if method == "DELETE" and ep.endswith(f"git/refs/heads/{PROTECTED_BRANCH}"):
        return f"deleting {PROTECTED_BRANCH} through the GitHub API"
    if method in ("PATCH", "POST") and "/git/refs/" in "/" + ep and forced:
        return "force-updating a branch through the GitHub API (force=true)"
    if method == "POST" and ep.endswith(f"branches/{PROTECTED_BRANCH}/rename"):
        return f"renaming {PROTECTED_BRANCH} through the GitHub API"
    return None


def _skip_wrapper(name: str, words: list[Word], i: int, ctx: _Ctx) -> tuple[int, str | None]:
    """Skip a wrapper's options; returns (index of the wrapped command, reason from ``env -S``)."""
    opts = _WRAPPERS[name]
    duration_skipped = False
    while i < len(words):
        w = words[i]
        if name == "env" and (w in ("-S", "--split-string") or w.startswith(("--split-string=", "-S"))):
            if w in ("-S", "--split-string"):
                inner = words[i + 1] if i + 1 < len(words) else ""
                rest = words[i + 2 :]
            else:
                inner = w.split("=", 1)[1] if w.startswith("--") else w[2:]
                rest = words[i + 1 :]
            return len(words), check_command(inner + " " + " ".join(rest), "bash", ctx.depth + 1)
        if w.startswith("-") and len(w) > 1 and not w.quoted:
            i += 2 if w in opts else 1  # a flag, or an option with its value attached
            continue
        if _ENV_ASSIGN.fullmatch(w):
            i += 1
            continue
        if name == "timeout" and not duration_skipped:
            duration_skipped = True
            i += 1
            continue
        break
    return i, None


def _param(word: Word) -> str | None:
    """A PowerShell parameter name (lower case, without ``-``), or None."""
    if word.quoted or not word.startswith("-") or word.startswith("--") or len(word) < 2:
        return None
    return word[1:].rstrip(":").lower()


def _start_process(rest: list[Word], ctx: _Ctx) -> str | None:
    file: Word | None = None
    args: list[Word] = []
    collecting = False
    i = 0
    while i < len(rest):
        w = rest[i]
        name = _param(w)
        known = name is not None and any(p.startswith(name) for p in _START_PROCESS_PARAMS)
        if known:
            collecting = False
            if "filepath".startswith(name) or name == "path":
                file = rest[i + 1] if i + 1 < len(rest) else None
                i += 2
                continue
            if "argumentlist".startswith(name) or name == "args":
                collecting = True
                i += 1
                continue
            if name in ("workingdirectory", "redirectstandarderror", "redirectstandardinput",
                        "redirectstandardoutput", "verb", "windowstyle", "credential", "environment"):
                i += 2
                continue
            i += 1
            continue
        if collecting:
            args.append(w)
        elif file is None:
            file = w
        else:
            collecting = True
            args.append(w)
        i += 1
    if file is None:
        return None
    if len(args) == 1 and " " in args[0] and not args[0].unresolved:
        args = _split_simple(args[0])
    return _check_words([file] + args, ctx)


def _check_words(words: list[Word], ctx: _Ctx) -> str | None:
    """Check one simple command (keywords and wrappers already skipped)."""
    if not words:
        return None
    cmd, rest = words[0], words[1:]
    if cmd.unresolved:
        # The command itself cannot be resolved ("$G", & (Get-Command git)): treat it as git.
        return _check_git(rest, ctx)
    base = _base(cmd)
    depth = ctx.depth
    if base in ("git", "git.exe"):
        return _check_git(rest, ctx)
    if base in ("gh", "gh.exe"):
        return _check_gh(rest)
    if base in _CD:
        targets = [a for a in rest if not (a.startswith("-") and not a.unresolved)]
        if targets:
            ctx.cwd = _join(ctx.cwd, targets[0])
        return None
    if base in _FILE_COMMANDS:
        return _check_file_command(base, rest, ctx)
    if base in ("find", "find.exe"):
        return _check_find(rest, ctx)
    if base in _SHELLS:
        for k, a in enumerate(rest):
            if a == "-c" or (a.startswith("-") and not a.startswith("--") and a.endswith("c")):
                if k + 1 < len(rest):
                    return check_command(rest[k + 1], "bash", depth + 1)
        return None
    if base in _CMD:
        for k, a in enumerate(rest):
            if a.lower() in ("/c", "/k", "/r"):
                # cmd keeps backslashes: tokenize like PowerShell, not bash.
                return check_command(" ".join(rest[k + 1 :]), "powershell", depth + 1)
        return None
    if base in _POWERSHELLS:
        k = 0
        while k < len(rest):
            name = (rest[k][1:] if rest[k].startswith(("-", "/")) else "").lower()
            if name == "c" or (len(name) >= 3 and "command".startswith(name)):
                return check_command(" ".join(rest[k + 1 :]), "powershell", depth + 1)
            if name and (name in ("e", "ec") or (len(name) >= 2 and "encodedcommand".startswith(name))):
                payload = rest[k + 1] if k + 1 < len(rest) else ""
                try:
                    decoded = base64.b64decode(payload, validate=True).decode("utf-16-le")
                except (ValueError, UnicodeDecodeError):
                    return "powershell -EncodedCommand the guard cannot decode"
                return check_command(decoded, "powershell", depth + 1)
            if name and name in ("executionpolicy", "ex", "exe", "exec", "ep", "windowstyle", "w",
                                 "configurationname", "workingdirectory", "wd", "outputformat",
                                 "inputformat", "version", "v"):
                k += 2
                continue
            k += 1
        return None
    if base in _EVALS:
        args = [r for r in rest if not (_param(r) and "command".startswith(_param(r)))]
        return check_command(" ".join(args), ctx.shell if base == "eval" else "powershell", depth + 1)
    if base in _START_PROCESS:
        return _start_process(rest, ctx)
    return None


_CD = {"cd", "pushd", "chdir", "set-location", "sl", "push-location"}
_REMOVE_OR_MOVE = {"rm", "del", "erase", "rmdir", "rd", "remove-item", "ri", "unlink", "shred",
                   "mv", "move", "move-item", "mi", "rename-item", "rni", "ren", "rename"}
_WRITE = {"set-content", "sc", "add-content", "ac", "out-file", "clear-content", "clc", "new-item",
          "ni", "truncate", "tee", "tee-object", "chmod", "chown", "chattr", "attrib", "icacls", "touch"}
_COPY = {"cp", "copy", "copy-item", "cpi", "install", "ln", "xcopy", "robocopy"}
_FILE_COMMANDS = _REMOVE_OR_MOVE | _WRITE | _COPY
# The reviewed pre-push guard; copying it into the hooks directory is the merge step (fork P12).
_HOOK_SOURCE = re.compile(r"(^|[\\/])scripts[\\/]+git-hooks[\\/]+pre-push$", re.I)


def _join(cwd: str | None, path: Word) -> str | None:
    """``path`` as seen from the directory changes earlier in this command line (None: unknown)."""
    if path.unresolved and not re.search(r"[\\/]", path):
        return None
    p = str(path).replace("\\", "/")
    if p.startswith("/") or re.match(r"[A-Za-z]:", p) or path.unresolved or cwd == "":
        return posixpath.normpath(p) if p else p
    if cwd is None:
        return None
    return posixpath.normpath(posixpath.join(cwd, p))


def _protected_path(path: str) -> str | None:
    """The reason a path must not be removed, moved or written, or None."""
    q = path.replace("\\", "/").casefold().rstrip("/")
    if re.search(r"(^|/)hooks/+pre-push$", q):
        return "the pre-push guard (hooks/pre-push), which would switch it off"
    if re.search(r"(^|/)\.git/+hooks$", q):
        return "the git hooks directory, which would switch the pre-push guard off"
    if re.search(rf"(^|/)refs/+heads/+{PROTECTED_BRANCH}$", q):
        return f"the ref file of {PROTECTED_BRANCH}"
    if re.search(r"(^|/)packed-refs$", q):
        return f"the packed refs file, which holds {PROTECTED_BRANCH} after git pack-refs or gc"
    if re.search(r"(^|/)\.git(/+refs(/+heads)?)?$", q):
        return "the repository's .git directory or its refs, which hold main"
    return None


def _check_file_command(base: str, rest: list[Word], ctx: _Ctx) -> str | None:
    args = [a for a in rest if a.unresolved or not (a.startswith("-") or (a.startswith("/") and len(a) == 2))]
    if base in _COPY:
        if any(_HOOK_SOURCE.search(str(a).replace("\\", "/")) for a in args[:-1]):
            return None  # installing the reviewed pre-push guard
        args = args[-1:]  # only the destination is written
    for a in args:
        joined = _join(ctx.cwd, a)
        for candidate in {str(a), joined or ""}:
            reason = _protected_path(candidate) if candidate else None
            if reason:
                return f"{base} on {a}: {reason}"
            if a.unresolved and re.search(r"(^|[\\/])hooks[\\/]*$", str(a)):
                return f"{base} on {a}: the git hooks directory, which would switch the pre-push guard off"
    return None


# `find` deletes through -delete and -exec without naming the file (build ticket 63).
_FIND_LEADING_OPTS = {"-H", "-L", "-P"}
_FIND_NAME_TESTS = {"-name", "-iname", "-lname", "-ilname"}
_FIND_PATH_TESTS = {"-path", "-ipath", "-wholename", "-iwholename"}
_FIND_REGEX_TESTS = {"-regex", "-iregex"}
_FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
_FIND_FILE_OUT = {"-fprint", "-fprint0", "-fprintf", "-fls"}
# What a deleting find must not be able to match: main's ref file, packed-refs, the pre-push guard,
# and the directories above them.
_FIND_PROTECTED_NAMES = (PROTECTED_BRANCH, "packed-refs", "pre-push", "hooks", "refs", "heads", ".git")
_FIND_PROTECTED_PATHS = (f".git/refs/heads/{PROTECTED_BRANCH}", ".git/packed-refs", ".git/hooks/pre-push",
                         ".git/hooks", ".git/refs/heads", ".git/refs", ".git")


def _in_git_dir(path: str) -> bool:
    q = path.replace("\\", "/").casefold()
    return bool(re.search(r"(^|/)\.git(/|$)|(^|/)refs/+heads(/|$)", q)) or _protected_path(path) is not None


def _find_test_hits(test: str, pattern: str, starts: list[str]) -> bool:
    """True when a find name, path or regex test can match one of the protected files."""
    if test in _FIND_NAME_TESTS:
        return any(fnmatch.fnmatchcase(n, pattern.casefold()) for n in _FIND_PROTECTED_NAMES)
    prefixes = {"", "."} | {s.replace("\\", "/").casefold() for s in starts}
    paths = {posixpath.join(s, p) if s else p for s in prefixes for p in _FIND_PROTECTED_PATHS}
    if test in _FIND_PATH_TESTS:
        return any(fnmatch.fnmatchcase(p, pattern.casefold()) for p in paths)
    if _EMACS_REGEX_OPERATOR.search(pattern):
        return True  # GNU find's default emacs syntax: \( \| \{ are operators, not literals in Python
    try:
        regex = re.compile(pattern, re.I)  # not casefolded: that would turn \D into \d
    except re.error:
        return True  # a regex the guard cannot read could match anything
    return any(regex.fullmatch(p) for p in paths)


# Emacs operators (\( \) \{ \} \| \< \> \` \') and POSIX classes ([[:alpha:]]) that Python reads
# differently: such a regex is treated as unreadable.
_EMACS_REGEX_OPERATOR = re.compile(r"\\[(){}|<>`']|\[[^\]]*\[[:=.]")


def _reaches_git_dir(path: str) -> bool:
    """True for a start point the repository's .git lies below: ``.``, ``..`` and their parents."""
    q = posixpath.normpath(path.replace("\\", "/"))
    return q == "." or q == ".." or q.startswith("../")


_FIND_SCAN_LIMIT = 5000  # directory entries an absolute find start is scanned for .git
_FIND_SCAN_SECONDS = 3.0  # the scan's deadline, well inside the hook's 30 s timeout


def _norm(path: str) -> str:
    return posixpath.normpath(path.replace("\\", "/")).casefold()


def _is_absolute(path: str) -> bool:
    return bool(re.match(r"[\\/~]|[A-Za-z]:[\\/]", path))


def _absolute_start_holds_git(path: str, follow: bool = False) -> bool:
    """True when the absolute find start ``path`` is, or lies above, a directory holding ``.git``,
    or when the guard cannot tell (fix round 1, finding 4)."""
    p = path.replace("\\", "/")
    drive = re.match(r"/([A-Za-z])(/|$)", p)
    if drive:  # Git Bash /c/... is C:/...
        p = f"{drive.group(1)}:/{p[drive.end():]}"
    elif not re.match(r"[A-Za-z]:/", p) and os.name == "nt":
        return True  # /tmp, ~ and other mounts the guard cannot map to a folder
    start = _norm(p).rstrip("/")
    known = [os.environ.get("CLAUDE_PROJECT_DIR", ""), os.getcwd(), str(ROOT)]
    for repo in filter(None, known):
        r = _norm(repo)
        if r == start or r.startswith(start + "/"):
            return True
    if not os.path.isdir(p):
        return True
    errors: list[OSError] = []
    deadline = time.monotonic() + _FIND_SCAN_SECONDS
    seen = 0
    # Fail closed (fix round 2): a folder the walk cannot list may hide a .git (findings A), and the
    # hook must answer well inside its 30 s timeout (finding B). Like find without -L, the walk does
    # not enter junctions, which MSYS reports as symlinks (finding C); with -L it follows them all.
    for dirpath, dirnames, filenames in os.walk(p, onerror=errors.append, followlinks=follow):
        if any(n.casefold() == ".git" for n in dirnames + filenames):
            return True
        seen += len(dirnames) + len(filenames)
        if seen > _FIND_SCAN_LIMIT or errors or time.monotonic() > deadline:
            return True
        if not follow:
            dirnames[:] = [d for d in dirnames if not os.path.isjunction(os.path.join(dirpath, d))]
    return bool(errors)


def _check_find(rest: list[Word], ctx: _Ctx) -> str | None:
    i = 0
    while i < len(rest) and not rest[i].unresolved and (
            rest[i] in _FIND_LEADING_OPTS or rest[i] == "-D" or rest[i].startswith("-O")):
        i += 2 if rest[i] == "-D" else 1
    follow = "-L" in rest[:i] or "-follow" in rest[i:]  # find follows symlinks and junctions
    starts: list[Word] = []
    while i < len(rest) and (rest[i].unresolved or not (rest[i].startswith("-") or rest[i] in ("(", "!", ","))):
        starts.append(rest[i])
        i += 1
    expr = rest[i:]
    destructive: str | None = None
    regextype = False
    tests: list[tuple[str, Word]] = []
    negate_next = False  # after ! or -not
    negated_groups: list[bool] = []  # for each open (, whether it or an enclosing group is negated
    k = 0
    while k < len(expr):
        t = expr[k]
        negated = negate_next or (bool(negated_groups) and negated_groups[-1])
        negate_next = False
        if t in ("!", "-not"):
            negate_next = True
        elif t == "(":
            negated_groups.append(negated)
        elif t == ")":
            if negated_groups:
                negated_groups.pop()
        elif t == "-regextype":
            regextype = True
            k += 1
        elif t == "-delete":
            destructive = "-delete"
        elif t in _FIND_EXEC:
            j = k + 1
            while j < len(expr) and expr[j] not in (";", "+"):
                j += 1
            # {} is a file name the guard cannot see.
            cmd = [_word(str(w), opaque=True) if "{}" in w else w for w in expr[k + 1 : j]]
            reason = _check_words(cmd, ctx)
            if reason:
                return f"find {t} runs a blocked command: {reason}"
            # Removers, writers and shells (whose script the guard does not read here).
            if cmd and (cmd[0].unresolved or _base(cmd[0]) in _REMOVE_OR_MOVE | _WRITE | _SHELLS
                        | _POWERSHELLS | _CMD | _EVALS):
                destructive = f"{t} {cmd[0]}"
            k = j
        elif t in _FIND_FILE_OUT and k + 1 < len(expr):
            target = expr[k + 1]
            for candidate in {str(target), _join(ctx.cwd, target) or ""}:
                why = _protected_path(candidate) if candidate else None
                if why:
                    return f"find {t} {target} writes {why}"
            k += 1
        elif t in _FIND_NAME_TESTS | _FIND_PATH_TESTS | _FIND_REGEX_TESTS and k + 1 < len(expr):
            # A negated test widens what is deleted, so only a plain test narrows it.
            if not negated:
                tests.append((t, expr[k + 1]))
            k += 1
        k += 1
    if destructive is None:
        return None
    if regextype:
        return f"find {destructive} with -regextype, whose regex syntax the guard does not read"
    for start in starts or [_word(".")]:
        for candidate in {str(start), _join(ctx.cwd, start) or ""}:
            if candidate and _in_git_dir(candidate):
                return (f"find {destructive} under {start}: it deletes files inside the .git directory the"
                        f" guard cannot name; remove an explicit file instead")
    unresolved = [w for _, w in tests if w.unresolved] + ([s for s in starts if s.unresolved] if not tests else [])
    if unresolved:
        return (f"find {destructive} with a start point or test the guard cannot resolve ({unresolved[0]});"
                f" write them literally")
    plain_starts = [str(s) for s in starts]
    if not tests and any(_reaches_git_dir(s) for s in plain_starts or ["."]):
        return (f"find {destructive} over the repository root without a plain (not negated) name, path or"
                f" regex test reaches the .git directory; start it in the directory you mean to clean")
    for s in plain_starts if not tests else []:
        if _is_absolute(s) and _absolute_start_holds_git(s, follow):
            return (f"find {destructive} from {s} without a plain (not negated) name, path or regex test:"
                    f" that folder holds a .git directory, lies above one, or cannot be checked")
    for test, pattern in tests:
        if _find_test_hits(test, pattern, plain_starts):
            return (f"find {destructive} with {test} {pattern}, which can match {PROTECTED_BRANCH}'s ref,"
                    f" packed-refs or the pre-push guard")
    return None


def _decode_printf(text: str) -> str:
    return (text.replace("\\n", "\n").replace("\\t", "\t").replace("\\0", "\0")
            .replace("\\\\", "\\"))


def _stdin_of(seg: Seg) -> tuple[str, str]:
    """The input of a segment: ("none", ""), ("literal", text) or ("unknown", "")."""
    if seg.stdin_redirect:
        return "unknown", ""
    parts = list(seg.bodies)
    source = seg.piped_from
    if source is not None:
        head = source[0].casefold() if source else ""
        if head in ("echo", "write-output", "printf") and not any(w.unresolved for w in source[1:]):
            words = [w for w in source[1:] if not (w.startswith("-") and len(w) <= 3)]
            text = " ".join(words)
            parts.append(_decode_printf(text) if head == "printf" else text)
        elif source.bodies and head in ("cat", ""):
            parts += source.bodies
        else:
            return "unknown", ""
    if not parts:
        return "none", ""
    return "literal", "\n".join(parts)


def _shell_reading_stdin(words: list[Word]) -> str | None:
    """The shell kind when ``words`` start a shell that reads its commands from stdin."""
    if not words or words[0].unresolved:
        return None
    base = _base(words[0])
    rest = [w.lower() for w in words[1:]]
    if base in _SHELLS:
        has_c = any(a == "-c" or (a.startswith("-") and not a.startswith("--") and a.endswith("c")) for a in rest)
        return None if has_c else "bash"
    if base in _CMD:
        return None if any(a in ("/c", "/k", "/r") for a in rest) else "bash"
    if base in _POWERSHELLS:
        for k, a in enumerate(rest):
            name = a.lstrip("-/") if a.startswith(("-", "/")) else ""
            if name == "c" or (len(name) >= 3 and "command".startswith(name)):
                return "powershell" if rest[k + 1 : k + 2] == ["-"] else None
            if name in ("e", "ec", "f", "file") or (len(name) >= 2 and "encodedcommand".startswith(name)):
                return None
        return "powershell"
    return None


def _check_segment(seg: Seg, ctx: _Ctx) -> str | None:
    reason = _segment_env_reason(seg)
    if reason:
        return reason
    for target in seg.redirects:
        joined = _join(ctx.cwd, target)
        for candidate in {str(target), joined or ""}:
            why = _protected_path(candidate) if candidate else None
            if why:
                return f"writing to {target}: {why}"
    if _record_assignment(seg, ctx):
        return None
    words = [v for w in seg for v in _expand(w, ctx)]
    ctx.stdin = _stdin_of(seg)
    from_stdin = False
    i = 0
    while i < len(words):
        w = words[i]
        if ctx.shell != "powershell" and w in _KEYWORDS and not w.quoted:
            i += 1
            continue
        if _ENV_ASSIGN.fullmatch(w) and w.bare:
            i += 1
            continue
        base = _base(w)
        if base in _WRAPPERS and not w.unresolved:
            i, reason = _skip_wrapper(base, words, i + 1, ctx)
            if reason:
                return reason
            from_stdin |= base == "xargs"
            continue
        break
    command = words[i:]
    if from_stdin and command:
        # xargs appends words read from its input: arguments the guard cannot see.
        command = command + [_word("<xargs input>", opaque=True)]
    if not command:
        return None
    first = command[0]
    if ctx.shell != "powershell" and first == "alias" and not first.quoted:
        for w in command[1:]:
            name, eq, value = w.partition("=")
            if eq:
                ctx.aliases[name] = value
        return None
    if ctx.shell != "powershell" and not first.quoted and first in ctx.aliases:
        expanded = ctx.aliases[first] + " " + " ".join(command[1:])
        reason = check_command(expanded, "bash", ctx.depth + 1)
        if reason:
            return reason
    reason = _check_words(command, ctx)
    if reason:
        return reason
    kind = _shell_reading_stdin(command)
    if kind:
        # bash <<EOF, cat <<EOF | bash, echo 'cmd' | sh: the input is a script.
        source_kind, text = ctx.stdin
        if source_kind == "literal":
            reason = check_command(text, kind, ctx.depth + 1)
            if reason:
                return reason
    return None


def check_command(text: str, shell: str = "bash", depth: int = 0) -> str | None:
    """Return the reason to block ``text``, or None to allow it."""
    if depth > _MAX_DEPTH:
        return "command nests shells too deeply for the guard to check"
    ctx = _Ctx(shell, depth)
    segments, nested = tokenize(text, shell)
    for seg in segments:
        reason = _check_segment(seg, ctx)
        if reason:
            return reason
    for inner in nested:
        reason = check_command(inner, shell, depth + 1)
        if reason:
            return reason
    return None


def decide(raw: bytes) -> tuple[int, str]:
    """(exit code, stderr text) for one hook call."""
    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
        tool = str(payload.get("tool_name", ""))
        command = (payload.get("tool_input") or {}).get("command")
    except (ValueError, AttributeError) as exc:
        return 2, _BLOCK_PREFIX + f"unreadable hook input ({exc}); the command was not checked." + _BLOCK_SUFFIX
    if not isinstance(command, str):
        return 0, ""
    shell = "powershell" if tool.lower() == "powershell" else "bash"
    try:
        reason = check_command(command, shell)
    except Exception as exc:  # noqa: BLE001 - fail closed on git/gh commands only
        if re.search(r"\b(git|gh)(\.exe)?\b", command, re.I):
            reason = f"the guard could not parse this git command ({type(exc).__name__}: {exc})"
        else:
            return 0, ""
    if reason:
        return 2, _BLOCK_PREFIX + reason + "." + _BLOCK_SUFFIX
    return 0, ""


def main() -> int:
    rc, message = decide(sys.stdin.buffer.read())
    if message:
        print(message, file=sys.stderr)
    return rc


if __name__ == "__main__":
    sys.exit(main())
