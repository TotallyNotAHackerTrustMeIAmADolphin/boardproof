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


def unqualified_clear_claims(text: str) -> list[str]:
    """Sentences that talk of clearing the strict-sizes flag without naming who does it (or denying it)."""
    sentences = re.split(r"(?<=[.!?])\s+", text.replace("\n", " "))
    return [
        s.strip()
        for s in sentences
        if re.search(r"strict[_-]sizes", s, re.IGNORECASE)
        and re.search(r"\bclear(s|ed|ing)?\b", s, re.IGNORECASE)
        and not any(q in s for q in _QUALIFIERS)
    ]


@pytest.mark.parametrize("doc", CLAIM_DOCS)
def test_docs_name_with_under_width_check_when_they_say_the_flag_is_cleared(doc):
    assert unqualified_clear_claims((ROOT / doc).read_text(encoding="utf-8")) == []


def test_the_mcp_routing_tools_do_not_contain_the_clearing_code():
    source = (ROOT / "src" / "kicad_mcp" / "toolsets" / "routing.py").read_text(encoding="utf-8")
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
