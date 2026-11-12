"""Publish-prep ticket 02: every relative link in the public docs resolves, and none points into `.scratch/`.

The maintainers' planning records live in `.scratch/` and are not part of a public copy of the
repository, so a link into it would be dead there. Public docs state a fact in words and cite the
ticket as a plain reference. The walker reads inline links `[text](target)` and reference
definitions `[label]: target` outside fenced code blocks and code spans. External links
(`http`, `https`, `mailto`) are not followed; a `#fragment` is dropped before the target is checked.
"""

from __future__ import annotations

import os
import posixpath
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

PUBLIC_DOCS = [
    "README.md",
    "docs/OVERVIEW.md",
    "docs/ARCHITECTURE.md",
    "docs/ROADMAP.md",
    "docs/TESTING.md",
    "CONTRIBUTING.md",
    "THIRD_PARTY.md",
    "docs/CONTEXT.md",
    "CLAUDE.md",
]

_INLINE = re.compile(r"(?<!\\)\]\(\s*<?([^)\s>]+)>?(?:\s+\"[^\"]*\")?\s*\)")
_REFERENCE = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*<?(\S+?)>?(?:\s+.*)?$")
_HTML = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_FENCE = re.compile(r"^\s*(```|~~~)")
_CODE_SPAN = re.compile(r"`[^`]*`")


def _link_targets(text: str) -> list[tuple[int, str]]:
    """Every (line number, target) of an inline or reference link outside code."""
    found: list[tuple[int, str]] = []
    in_fence = False
    for number, line in enumerate(text.splitlines(), start=1):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        prose = _CODE_SPAN.sub("", line)
        found.extend((number, m.group(1)) for m in _INLINE.finditer(prose))
        found.extend((number, m.group(1)) for m in _HTML.finditer(prose))
        ref = _REFERENCE.match(prose)
        if ref:
            found.append((number, ref.group(1)))
    return found


def _exists_with_exact_case(root: Path, parts: list[str]) -> bool:
    """True if `root/parts` exists with every component spelled exactly as on disk (GitHub is case-sensitive, Windows is not)."""
    current = root
    for part in parts:
        if part in ("", "."):
            continue
        try:
            names = os.listdir(current)
        except OSError:
            return False
        if part not in names:
            return False
        current = current / part
    return True


def link_problems(doc: Path, root: Path) -> list[str]:
    """One message per relative link in `doc` that does not resolve inside `root`, differs from the on-disk case, or points into `.scratch/`.

    A target starting with `/` is taken from the repository root, as GitHub does.
    """
    problems: list[str] = []
    doc_rel = doc.relative_to(root).as_posix()
    for number, target in _link_targets(doc.read_text(encoding="utf-8")):
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target):  # http:, https:, mailto: ...
            continue
        path_part = target.split("#", 1)[0].split("?", 1)[0]
        if not path_part:  # a bare in-page anchor
            continue
        where = f"{doc_rel}:{number}: {target}"
        if path_part.startswith("/"):
            joined = posixpath.normpath(path_part.lstrip("/"))
        else:
            joined = posixpath.normpath(posixpath.join(posixpath.dirname(doc_rel), path_part))
        if joined == ".." or joined.startswith("../"):
            problems.append(f"{where} leaves the repository")
            continue
        parts = [] if joined == "." else joined.split("/")
        if parts and parts[0].lower() == ".scratch":
            problems.append(f"{where} points into .scratch/")
        elif not (root / joined).exists():
            problems.append(f"{where} does not exist")
        elif not _exists_with_exact_case(root, parts):
            problems.append(f"{where} differs in case from the file on disk")
    return problems


@pytest.mark.parametrize("doc", PUBLIC_DOCS)
def test_every_relative_link_resolves_and_none_points_into_scratch(doc):
    path = ROOT / doc
    assert path.is_file(), f"{doc} is listed as a public doc but does not exist"
    assert link_problems(path, ROOT) == []


def test_the_public_doc_list_has_the_docs_the_ticket_names():
    # Guards the list itself: dropping a doc from it would silence its links.
    assert {"README.md", "docs/OVERVIEW.md", "docs/ARCHITECTURE.md", "docs/ROADMAP.md", "docs/TESTING.md",
            "CONTRIBUTING.md", "THIRD_PARTY.md"} <= set(PUBLIC_DOCS)


# --- the walker itself: mutants -----------------------------------------------------------------

def _doc(tmp_path: Path, body: str) -> Path:
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "other.md").write_text("# other\n", encoding="utf-8")
    doc = tmp_path / "docs" / "page.md"
    doc.write_text(body, encoding="utf-8")
    return doc


def test_a_clean_page_has_no_problems(tmp_path):
    doc = _doc(tmp_path, "See [other](../other.md#top) and [site](https://example.org/x).\n")
    assert link_problems(doc, tmp_path) == []


def test_mutant_a_link_into_scratch_is_reported_even_when_the_file_exists(tmp_path):
    (tmp_path / ".scratch" / "map").mkdir(parents=True)
    (tmp_path / ".scratch" / "map" / "ticket.md").write_text("x\n", encoding="utf-8")
    doc = _doc(tmp_path, "See [ticket](../.scratch/map/ticket.md).\n")
    problems = link_problems(doc, tmp_path)
    assert len(problems) == 1 and "points into .scratch/" in problems[0]


def test_mutant_a_reference_style_link_into_scratch_is_reported(tmp_path):
    doc = _doc(tmp_path, "See [ticket][t].\n\n[t]: ../.scratch/map/ticket.md\n")
    problems = link_problems(doc, tmp_path)
    assert len(problems) == 1 and "points into .scratch/" in problems[0]


def test_mutant_a_missing_target_is_reported(tmp_path):
    doc = _doc(tmp_path, "See [gone](../missing.md).\n")
    problems = link_problems(doc, tmp_path)
    assert len(problems) == 1 and "does not exist" in problems[0]


def test_mutant_a_link_that_leaves_the_repository_is_reported(tmp_path):
    doc = _doc(tmp_path, "See [up](../../elsewhere.md).\n")
    problems = link_problems(doc, tmp_path)
    assert len(problems) == 1 and "leaves the repository" in problems[0]


def test_links_in_code_blocks_and_code_spans_are_not_followed(tmp_path):
    body = "Run `[x](.scratch/a.md)`.\n\n```\n[y](.scratch/b.md)\n```\n"
    assert link_problems(_doc(tmp_path, body), tmp_path) == []


def test_mutant_a_link_with_the_wrong_case_is_reported(tmp_path):
    # `Other.md` exists as `other.md`; on Windows the file system would accept it, on GitHub it is dead.
    doc = _doc(tmp_path, "See [other](../Other.md).\n")
    problems = link_problems(doc, tmp_path)
    assert len(problems) == 1 and ("differs in case" in problems[0] or "does not exist" in problems[0])


def test_mutant_an_html_href_into_scratch_or_to_a_missing_file_is_reported(tmp_path):
    doc = _doc(tmp_path, '<a href="../.scratch/x.md">a</a> and <a href="../gone.md">b</a>\n')
    problems = link_problems(doc, tmp_path)
    assert len(problems) == 2
    assert "points into .scratch/" in problems[0] and "does not exist" in problems[1]


def test_an_absolute_link_is_taken_from_the_repository_root(tmp_path):
    ok = _doc(tmp_path, "See [other](/other.md) and [page](/docs/page.md).\n")
    assert link_problems(ok, tmp_path) == []
    bad = _doc(tmp_path, "See [x](/.scratch/a.md) and [y](/docs/nope.md).\n")
    problems = link_problems(bad, tmp_path)
    assert len(problems) == 2
    assert "points into .scratch/" in problems[0] and "does not exist" in problems[1]


# --- a claim about who clears KRT's strict-sizes flag -------------------------------------------
# The flag `strict_sizes_violated` is cleared (when the raw board check finds nothing) only by
# `rung_backends.with_under_width_check`, which the rung runner and `autonomy_run` use. The MCP
# routing tools never clear it: `confirm_route` refuses until `acknowledge_router_flags=True`.
# A doc sentence that says the flag is cleared must name that function.

CLAIM_DOCS = PUBLIC_DOCS + [
    ".claude/skills/kicad-pcb/SKILL.md",
    ".claude/skills/kicad-verification/SKILL.md",
]
_QUALIFIERS = ("with_under_width_check", "never clear", "nothing ever clears", "never clears")


# Verbs that say the flag goes away (issue 9 item 1): "clear" alone let "resets", "drops" or "removes" pass.
_CLEAR_VERBS = (r"clear(?:s|ed|ing)?|re-?set(?:s|ting)?|drop(?:s|ped|ping)?|remov(?:e|es|ed|ing)|unset(?:s|ting)?|"
                r"un-?flag(?:s|ged|ging)?|lift(?:s|ed|ing)?|waiv(?:e|es|ed|ing)|suppress(?:es|ed|ing)?|"
                r"wip(?:e|es|ed|ing)|eras(?:e|es|ed|ing)|overrid(?:e|es|ing)|overrode|overridden|flip(?:s|ped|ping)?|"
                r"no\s+longer\s+applies|(?:goes|go|went|gone)\s+away|discard(?:s|ed|ing)?|ignor(?:e|es|ed|ing)|turn(?:s|ed|ing)?\s+off|set(?:s|ting)?\s+(?:it\s+)?to\s+false")
# This list can never be complete: known misses are "no longer set after", "disappears", "toggled off". A reviewer
# who finds another way to say it adds the verb here with a test; the list is a net, not a proof.
# A `--flag` such as --fab-overrides is removed before the search (not a verb); "auto-clears" still is one.
_VERB = re.compile(rf"(?<!\w)(?<!--)(?:{_CLEAR_VERBS})\b", re.IGNORECASE)
_CLAUSE_SPLIT = re.compile(r";|:|\s[-–—]{1,2}\s|,\s+(?:and|but|while|whereas)\s")
_FLAG = re.compile(r"(?<!\w)--[\w-]+")  # a --command-line-flag is never a verb
_NEGATION = re.compile(r"\b(?:not|never|nothing|no)\b|n't", re.IGNORECASE)


def unqualified_clear_claims(text: str) -> list[str]:
    """Sentences that talk of clearing the strict-sizes flag without naming who does it (or denying it).

    The qualifier has to stand in the same clause as the verb (clauses split at ; : dashes and
    ", and/but"), so a sentence that merely mentions `with_under_width_check` elsewhere does not excuse it.
    One elliptical form is allowed: a NEGATED verb clause followed by a clause that begins with the qualifier
    ("Retry does not clear the flag; `with_under_width_check` does."). The qualifier before the verb never counts.
    """
    sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
    found = []
    for s in sentences:
        if not re.search(r"strict[_-]sizes", s, re.IGNORECASE):
            continue
        clauses = _CLAUSE_SPLIT.split(s)
        for index, clause in enumerate(clauses):
            if not _VERB.search(_FLAG.sub(" ", clause)) or any(q in clause.lower() for q in _QUALIFIERS):
                continue
            following = clauses[index + 1].lower().lstrip(" `*_(\"'") if index + 1 < len(clauses) else ""
            if _NEGATION.search(clause) and following.removeprefix("only ").lstrip(" `").startswith(_QUALIFIERS[0]):
                continue
            found.append(s.strip())
            break
    return found


@pytest.mark.parametrize("doc", CLAIM_DOCS)
def test_docs_name_with_under_width_check_when_they_say_the_flag_is_cleared(doc):
    assert unqualified_clear_claims((ROOT / doc).read_text(encoding="utf-8")) == []


def test_the_mcp_routing_tools_do_not_contain_the_clearing_code():
    source = (ROOT / "src" / "boardproof" / "toolsets" / "routing.py").read_text(encoding="utf-8")
    assert "with_under_width_check" not in source
    assert "_clear_stale_strict_sizes_flag" not in source


def test_the_skill_states_that_the_mcp_tools_never_clear_the_flag():
    skill = (ROOT / ".claude" / "skills" / "kicad-pcb" / "SKILL.md").read_text(encoding="utf-8")
    assert "nothing ever clears that flag" in skill
    assert "acknowledge_router_flags=True" in skill


def test_mutant_the_old_unqualified_wording_is_caught():
    old = ("The wrapper clears that flag (keeping the router's claim as `strict_sizes_reported_by_router`) "
           "only when the raw check finds no under-width copper.")
    assert len(unqualified_clear_claims(old)) == 1
    new = "Only the rung runner (`rung_backends.with_under_width_check`) clears a stale `strict_sizes_violated` flag."
    assert unqualified_clear_claims(new) == []


@pytest.mark.parametrize("verb", ["resets", "drops", "removes", "unsets", "lifts", "suppresses", "ignores", "turns off"])
def test_mutant_other_verbs_for_clearing_the_flag_are_caught(verb):
    assert len(unqualified_clear_claims(f"The wrapper {verb} the `strict_sizes_violated` flag after the raw check.")) == 1


def test_mutant_a_qualifier_in_another_clause_does_not_excuse_the_claim():
    text = ("The rung runner uses `with_under_width_check`, and the wrapper also resets the `strict_sizes_violated` flag "
            "when the check passes.")
    assert len(unqualified_clear_claims(text)) == 1
    ok = "The wrapper resets the `strict_sizes_violated` flag only through `with_under_width_check`."
    assert unqualified_clear_claims(ok) == []


@pytest.mark.parametrize(
    "phrase",
    [
        "The wrapper wipes the `strict_sizes_violated` flag.",
        "The wrapper erases the `strict_sizes_violated` flag.",
        "A later step overrides the `strict_sizes_violated` flag.",
        "The router flips the `strict_sizes_violated` flag back.",
        "After the check the `strict_sizes_violated` flag no longer applies.",
        "The `strict_sizes_violated` flag goes away once the check passes.",
        "The wrapper RESETS the `strict_sizes_violated` flag.",
    ],
)
def test_more_verbs_without_a_qualifier_are_caught(phrase):
    assert len(unqualified_clear_claims(phrase)) == 1


@pytest.mark.parametrize(
    "phrase",
    [
        "Nothing ever clears `strict_sizes_violated`.",
        "NOTHING EVER CLEARS `strict_sizes_violated`.",
        "Retry does not clear the `strict_sizes_violated` flag; `with_under_width_check` does.",
        "Only `with_under_width_check` wipes the `strict_sizes_violated` flag.",
        "The tools never clear `strict_sizes_violated`.",
        "`strict_sizes_violated` is reported and kept.",
    ],
)
def test_qualified_or_unrelated_phrasings_pass(phrase):
    assert unqualified_clear_claims(phrase) == []


def test_a_command_line_flag_is_not_a_verb():
    assert unqualified_clear_claims("Run it with --strict-sizes --fab-overrides F and read `strict_sizes_violated`.") == []


def test_semicolon_clauses_the_qualifier_before_the_semicolon_does_not_excuse_the_verb_after_it():
    text = ("The runner uses `with_under_width_check`; the wrapper also resets the `strict_sizes_violated` flag "
            "when the check passes.")
    assert len(unqualified_clear_claims(text)) == 1


@pytest.mark.parametrize(
    "phrase",
    [
        "Retry does not clear the `strict_sizes_violated` flag; `with_under_width_check` does.",
        "Nothing clears the `strict_sizes_violated` flag; only `with_under_width_check` does.",
        "The tools never reset `strict_sizes_violated`; `with_under_width_check` does.",
    ],
)
def test_an_elliptical_qualifier_after_a_negated_clause_passes(phrase):
    assert unqualified_clear_claims(phrase) == []


@pytest.mark.parametrize(
    "phrase",
    [
        "Retry clears the `strict_sizes_violated` flag; `with_under_width_check` does too.",  # no negation: affirmative claim
        "Retry does not clear the `strict_sizes_violated` flag; the wrapper does.",  # the qualifier is not named
        "Retry does not clear the `strict_sizes_violated` flag; the wrapper does; `with_under_width_check` does too.",  # three clauses: only the next clause counts
    ],
)
def test_an_elliptical_continuation_needs_a_negated_first_clause_and_the_qualifier_itself(phrase):
    assert len(unqualified_clear_claims(phrase)) == 1


@pytest.mark.parametrize("verb", ["auto-clears", "re-sets", "self-resets", "auto-removes"])
def test_a_hyphenated_verb_is_still_a_verb(verb):
    assert len(unqualified_clear_claims(f"The wrapper {verb} the `strict_sizes_violated` flag.")) == 1


def test_a_double_dash_flag_is_not_a_verb():
    assert unqualified_clear_claims("Pass --fab-overrides and read `strict_sizes_violated`.") == []
    assert unqualified_clear_claims("Pass --fab-overrides F, then it auto-clears `strict_sizes_violated`.") != []
