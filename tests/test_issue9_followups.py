"""Issue 9: small follow-ups of earlier reviews (docs, hooks, scripts). One section per item."""

from __future__ import annotations

import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

from boardproof.sourcing import jlcpcb_basic as jlc
from boardproof.toolsets import routing
from tests import _jlcpcb_library as jl
from tests._hermetic_git import run_git
from tests.test_jlcpcb_basic import _symbol, _write_symbol_lib

ROOT = Path(__file__).resolve().parents[1]

# --- item 4: a docstring and an error text named a Freerouting version the code does not pin ---------


def test_item4_find_java_texts_name_the_pinned_freerouting_not_2_3_0(monkeypatch):
    monkeypatch.delenv("JAVA_PATH", raising=False)
    monkeypatch.setattr(routing.shutil, "which", lambda *_a, **_k: None)
    monkeypatch.setattr(routing, "_JAVA_CANDIDATE_GLOBS", {})
    with pytest.raises(routing.FreeroutingError) as raised:
        routing.find_java()
    for text in (routing.find_java.__doc__ or "", str(raised.value)):
        assert "2.3.0" not in text
        assert "2.5.0" in text


# --- item 5: scripts/export_ladder_results.py fails closed and leaves URLs alone ----------------------
_spec = importlib.util.spec_from_file_location("export_ladder_results_i9", ROOT / "scripts" / "export_ladder_results.py")
export = importlib.util.module_from_spec(_spec)
sys.modules["export_ladder_results_i9"] = export
_spec.loader.exec_module(export)

_GOOD_ATTEMPT = {"index": 1, "status": "done", "gate": {"verdict": "FAIL", "unconnected": [], "violations": []}}


def _runs_root(tmp_path, report, *, extra_rows=()):
    """A runs root with one batch row; ``report`` is the report.json content (None: no file)."""
    root = tmp_path / "runs"
    run = root / "1" / "freerouting" / "20260101-000000"
    run.mkdir(parents=True)
    row = {"kind": "batch", "rung": 1, "backend": "freerouting", "run_dir": str(run), "date": "2026-01-01"}
    (root / "index.jsonl").write_text("\n".join(json.dumps(r) for r in [row, *extra_rows]) + "\n", encoding="utf-8")
    if report is not None:
        (run / "report.json").write_text(json.dumps(report), encoding="utf-8")
    return root


def test_item5_a_good_report_is_exported(tmp_path):
    root = _runs_root(tmp_path, {"attempts": [_GOOD_ATTEMPT], "best_attempt": 1})
    out = tmp_path / "out.json"
    assert export.main([str(root), "--out", str(out)]) == 0
    assert len(json.loads(out.read_text(encoding="utf-8"))["batches"]) == 1


@pytest.mark.parametrize(
    "report",
    [
        None,  # report.json missing
        {"best_attempt": 1},  # no attempts
        {"attempts": "x"},  # attempts is not a list
        {"attempts": [{"index": 1, "status": "done"}]},  # an attempt without a gate
        {"attempts": [{"index": 1, "gate": None}]},
        [1, 2],  # not an object
    ],
)
def test_item5_a_batch_row_without_a_usable_report_exits_non_zero_and_writes_nothing(tmp_path, report, capsys):
    root = _runs_root(tmp_path, report)
    out = tmp_path / "out.json"
    assert export.main([str(root), "--out", str(out)]) != 0
    assert not out.exists()
    assert "batch" in capsys.readouterr().err


def test_item5_an_existing_output_is_left_untouched_on_a_refusal(tmp_path):
    root = _runs_root(tmp_path, None)
    out = tmp_path / "out.json"
    out.write_text("old\n", encoding="utf-8")
    assert export.main([str(root), "--out", str(out)]) != 0
    assert out.read_text(encoding="utf-8") == "old\n"


def test_item5_a_url_passes_the_sanitiser_unchanged_and_a_drive_path_next_to_it_does_not():
    url = "Download a release (https://github.com/freerouting/freerouting/releases) and set it"
    assert export.sanitise(url) == url
    assert export.sanitise("see http://example.org/a and ftp://h/x") == "see http://example.org/a and ftp://h/x"
    mixed = "jar at " + chr(67) + ":" + chr(92) + "Users" + chr(92) + "x" + chr(92) + "f.jar from https://github.com/a/b"
    assert export.sanitise(mixed) == "jar at <path> from https://github.com/a/b"
    assert export.sanitise("D:/data/run.json now") == "<path> now"


def test_item5_the_committed_history_has_no_mangled_url():
    text = (ROOT / "evals" / "ladder_results.json").read_text(encoding="utf-8")
    assert "http<path>" not in text


def test_item5_the_playbook_fork_names_no_vanished_scratch_path_for_the_pic_programmer_attribution():
    fork = (ROOT / "docs" / "playbook" / "PROJECT-KICAD-MCP-PLAYBOOK.md").read_text(encoding="utf-8")
    assert ".scratch" + "/pivot-ladder" not in fork
    assert "ATTRIBUTION-pic_programmer" not in fork
    assert "THIRD_PARTY.md" in fork


# --- item 6: the JLCPCB library switch, the index header, the PCM branch -------------------------------


def _library_dir(root: Path) -> Path:
    (root / "symbols").mkdir(parents=True)
    _write_symbol_lib(root / "symbols" / "JLCPCB-A.kicad_sym", _symbol("R1", LCSC="C1", Class="Basic Component"))
    return root


def test_item6_without_the_switch_a_missing_library_skips_with_the_reason(tmp_path):
    with pytest.raises(pytest.skip.Exception) as raised:
        jl.real_library({}, checkout_root=tmp_path)
    assert "not initialised" in str(raised.value) and jl.REQUIRED_FLAG in str(raised.value)


@pytest.mark.parametrize("value", ["1", "yes", "true"])
def test_item6_with_the_switch_a_missing_library_fails(tmp_path, value):
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as raised:
        jl.real_library({jl.REQUIRED_FLAG: value}, checkout_root=tmp_path)
    assert raised.type is pytest.fail.Exception  # a skip here would let a stale index pass
    assert jl.REQUIRED_FLAG in str(raised.value)


@pytest.mark.parametrize("value", ["", "0", " 0 "])
def test_item6_an_empty_or_zero_switch_is_off(tmp_path, value):
    with pytest.raises(pytest.skip.Exception):
        jl.real_library({jl.REQUIRED_FLAG: value}, checkout_root=tmp_path)


def test_item6_the_environment_variable_names_the_library_and_wins_over_the_submodule(tmp_path):
    named = _library_dir(tmp_path / "named")
    sub = _library_dir(tmp_path / "co" / jlc.SUBMODULE_RELATIVE_PATH)
    assert jl.real_library({jlc.JLCPCB_LIBRARY_ENV_VAR: str(named)}, checkout_root=tmp_path / "co") == named
    assert jl.real_library({}, checkout_root=tmp_path / "co") == sub


def test_item6_a_dead_environment_variable_is_a_miss_not_a_fall_back_to_the_submodule(tmp_path):
    _library_dir(tmp_path / "co" / jlc.SUBMODULE_RELATIVE_PATH)
    env = {jlc.JLCPCB_LIBRARY_ENV_VAR: str(tmp_path / "nowhere")}
    with pytest.raises(pytest.skip.Exception):
        jl.real_library(env, checkout_root=tmp_path / "co")
    with pytest.raises((pytest.fail.Exception, pytest.skip.Exception)) as raised:
        jl.real_library({**env, jl.REQUIRED_FLAG: "1"}, checkout_root=tmp_path / "co")
    assert raised.type is pytest.fail.Exception


def test_item6_the_index_copyright_line_is_the_upstream_licence_line():
    licence_line = "Copyright (c) 2023 Chris"
    third_party = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
    assert f"\n{licence_line}\n" in third_party  # the licence text we ship, verbatim
    vendored = json.loads(jlc.VENDORED_INDEX_PATH.read_text(encoding="utf-8"))
    assert vendored["source"]["copyright"] == licence_line


def test_item6_build_index_writes_the_same_copyright_line(tmp_path):
    (tmp_path / "symbols").mkdir()
    _write_symbol_lib(tmp_path / "symbols" / "JLCPCB-A.kicad_sym", _symbol("R1", LCSC="C1", Class="Basic Component"))
    built = jlc.build_index(tmp_path, commit="a" * 40)
    assert built["source"]["copyright"] == "Copyright (c) 2023 Chris"


def test_item6_a_pcm_candidate_without_the_library_is_normal_and_never_a_note(tmp_path, monkeypatch):
    third_party = tmp_path / "home" / "Documents" / "KiCad" / "10.0" / "3rdparty"
    (third_party / "symbols" / "other").mkdir(parents=True)  # a PCM install of something else
    assert jlc._source_problem("pcm", third_party) is None
    monkeypatch.delenv(jlc.JLCPCB_LIBRARY_ENV_VAR, raising=False)
    monkeypatch.setattr(jlc, "_home_dir", lambda: tmp_path / "home")
    monkeypatch.setattr(jlc, "_checkout_root", lambda: None)
    assert jlc.library_notes() == []
    assert jlc.resolve_library_root() is None


def test_item6_an_unreadable_pcm_candidate_is_named(tmp_path, monkeypatch):
    third_party = tmp_path / "3rdparty"
    third_party.mkdir()

    def boom(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", boom)
    assert "cannot be read" in jlc._source_problem("pcm", third_party)


# --- item 7: .gitignore anchors build/ and dist/ to the repository root -----------------------------------


def _ignored(path: str) -> bool:
    return run_git("check-ignore", "-q", "--no-index", path, cwd=ROOT, check=False).returncode == 0


@pytest.mark.parametrize("path", ["build/x.txt", "dist/x.whl"])
def test_item7_the_top_level_build_and_dist_stay_ignored(path):
    assert _ignored(path)


@pytest.mark.parametrize("path", ["src/boardproof/build/x.py", "scripts/dist/x.py", "tests/build/x.py", "docs/dist/x.md"])
def test_item7_a_nested_build_or_dist_folder_is_not_ignored_silently(path):
    assert not _ignored(path)


# --- item 10: no document states a test count that can go stale -----------------------------------------

COUNT_DOCS = ["CLAUDE.md", "README.md", "docs/OVERVIEW.md", "docs/TESTING.md"]
# A number, then up to one word, then "test" or "tests" ("4,490 tests", "7,000+ tests", "4490 automated tests",
# "more than 4,000 test cases", "7,077 tests pass"). "1,090 mm of tests" (two words between) is not a count.
_COUNT_CLAIM = re.compile(
    r"\d[\d,.]*\+?[\s-]*(?:\w+[\s-]+){0,1}tests?\b"
    r"|\d[\d,.]*\s+(?:without the network|with them)",
    re.IGNORECASE,
)


def count_claims(text: str) -> list[str]:
    return [m.group(0) for m in _COUNT_CLAIM.finditer(text)]


@pytest.mark.parametrize("doc", COUNT_DOCS)
def test_item10_the_doc_states_no_test_count(doc):
    """The suite grew from 4,090 to over 7,000 collected tests while CLAUDE.md, README, OVERVIEW and TESTING
    each carried a dated figure. A figure is generated (`pytest --collect-only -q`), never typed."""
    assert count_claims((ROOT / doc).read_text(encoding="utf-8")) == []


@pytest.mark.parametrize("doc", COUNT_DOCS)
def test_item10_the_doc_points_at_the_command_that_prints_the_count(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert "--collect-only" in text


@pytest.mark.parametrize(
    "stale",
    ["around 4,090 tests as collected", "Around 4,490 tests (4,486 without the network-marked ones;", "~4.5k tests",
     "7077 tests collected", "about 7,000 test cases", "(4,486 without the network-marked ones, 4,489 with them"],
)
def test_item10_mutant_the_old_wordings_are_caught(stale):
    assert count_claims(stale) != []


@pytest.mark.parametrize(
    "fine",
    ["several thousand tests", "the tests that test code", "Tests: see docs/TESTING.md", "1,090 mm of tests",
     "pytest --collect-only -q prints the count", "two tests per tool"],
)
def test_item10_no_false_alarm_on_wording_without_a_figure(fine):
    # "1,090 mm of tests" has no figure directly before the word tests
    assert count_claims(fine) == []


# --- item 11: CONTRIBUTING.md documents the identity setup and every hook's install command ---------------
HOOKS_DIR = ROOT / "scripts" / "git-hooks"


def _contributing() -> str:
    return (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")


def _hook_names() -> list[str]:
    return sorted(p.name for p in HOOKS_DIR.iterdir() if p.is_file())


def test_item11_there_are_hooks_to_document():
    assert {"commit-msg", "pre-push"} <= set(_hook_names())


@pytest.mark.parametrize("hook", ["commit-msg", "pre-push"])
def test_item11_contributing_gives_the_install_command_of_each_hook(hook):
    expected = f'cp scripts/git-hooks/{hook} "$(git rev-parse --git-common-dir)/hooks/{hook}"'
    assert expected in _contributing()


def test_item11_every_hook_file_is_named_in_contributing():
    text = _contributing()
    assert [h for h in _hook_names() if f"scripts/git-hooks/{h}" not in text] == []


def test_item11_contributing_documents_the_identity_setup_with_the_address_the_hooks_accept():
    text = _contributing()
    allowed = re.search(r'allowed="([^"]+)"', (HOOKS_DIR / "commit-msg").read_text(encoding="utf-8")).group(1)
    assert allowed in text  # the exact address the hooks compare against
    assert f"git config user.email {allowed}" in text
    assert "git config user.name " in text
    assert "git config user.useConfigOnly true" in text  # git refuses to guess an identity
    assert "guess" in text and "operating-system account" in text  # why: what a fresh clone does
    assert "not `--global`" in text or "without `--global`" in text  # the repository's own config, shared by worktrees


def test_item11_contributing_does_not_suggest_core_hookspath():
    # scripts/git_guardrails.py refuses core.hooksPath in any form (it switches the pre-push guard off)
    text = _contributing()
    assert "git config core.hooksPath" not in text and "-c core.hooksPath" not in text
    assert "refuses every `core.hooksPath` setting" in text  # and says why it is not the install route


def test_item6_testing_md_says_where_the_library_switch_applies():
    text = (ROOT / "docs/TESTING.md").read_text(encoding="utf-8")
    assert jl.REQUIRED_FLAG in text
    assert "The batch gate does not set it" in text
    assert jlc.JLCPCB_LIBRARY_ENV_VAR in text


# --- review round: drive paths glued to letters, unreadable reports -------------------------------------
BS = chr(92)


@pytest.mark.parametrize(
    "dirty",
    [
        "cwdD:" + BS + "work" + BS + "secret" + BS + "proj",
        "foundE:/data/priv/x.kicad_pcb",
        "at C:" + BS + "Users" + BS + "x" + BS + "f.jar now",
        "tail9F:/a/b",
    ],
)
def test_item5_a_drive_path_is_removed_even_when_glued_to_letters(dirty):
    cleaned = export.sanitise(dirty)
    assert "<path>" in cleaned and "secret" not in cleaned and "data/priv" not in cleaned and "Users" not in cleaned
    assert export.has_machine_path(dirty) and not export.has_machine_path(cleaned)


@pytest.mark.parametrize(
    "url",
    ["https://github.com/a/b", "ftp://h/x", "git+ssh://host/x", "see (https://github.com/freerouting/freerouting/releases) and"],
)
def test_item5_a_url_scheme_is_the_only_exemption(url):
    assert export.sanitise(url) == url
    assert not export.has_machine_path(url)


def test_item5_a_url_and_a_glued_drive_path_in_one_string():
    mixed = "from https://github.com/a/b to cwdD:/work/x"
    assert export.sanitise(mixed) == "from https://github.com/a/b to cwd<path>"


@pytest.mark.parametrize("body", ["{{", "", "[1,", "\x00"])
def test_item5_an_unparsable_report_is_refused_with_a_message(tmp_path, capsys, body):
    root = _runs_root(tmp_path, None)
    (root / "1" / "freerouting" / "20260101-000000" / "report.json").write_text(body, encoding="utf-8")
    out = tmp_path / "out.json"
    assert export.main([str(root), "--out", str(out)]) == 2
    assert not out.exists()
    assert "refused, nothing written" in capsys.readouterr().err


@pytest.mark.parametrize(
    "claim",
    [
        "The suite has 4,490 tests.",
        "There are over 7,000 tests.",
        "7,077 tests pass.",
        "a suite of 7077 tests",
        "nearly 4500 tests",
        "more than 4,000 test cases",
        "7,000+ tests",
        "4490 automated tests",
    ],
)
def test_item10_mutant_more_phrasings_of_a_typed_count_are_caught(claim):
    assert count_claims(claim) != []


# --- delta round: only the scheme:// prefix of a URL is exempt, never its tail ---------------------------
@pytest.mark.parametrize(
    "leaky",
    [
        "x://C:" + BS + "Users" + BS + "secret",
        "https://h/p?f=C:" + BS + "Users" + BS + "s",
        "file:///C:/Users/x/proj",
        "see https://github.com/a/b then C:" + BS + "Users" + BS + "x" + BS + "my dir" + BS + "f.jar",
        "https://github.com/a/b" + "D:" + BS + "work" + BS + "secret",
    ],
)
def test_item5_a_windows_path_inside_or_after_a_url_is_still_removed(leaky):
    cleaned = export.sanitise(leaky)
    assert "Users" not in cleaned and "secret" not in cleaned and "work" not in cleaned, cleaned
    assert export.has_machine_path(leaky) and not export.has_machine_path(cleaned)


def test_item5_a_url_with_a_path_in_its_tail_keeps_its_scheme_and_host():
    assert export.sanitise("https://h/p?f=C:" + BS + "Users" + BS + "s") == "https://h/p?f=<path>"
    assert export.sanitise("path with spaces C:" + BS + "Users" + BS + "Jane Doe" + BS + "x" + BS + "f.jar ok") == "path with spaces <path> ok"
