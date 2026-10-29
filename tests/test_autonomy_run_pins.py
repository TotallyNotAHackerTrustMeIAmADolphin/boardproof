"""Build ticket 51: rung pins on protocol 4; build ticket 67: rung 2 re-pinned on protocol 5.

* A pin is sha256 over file CONTENT with every CRLF pair read as LF. Nothing else is
  normalised: any other changed byte in the board, the project settings, the rules or a
  library changes the pin. KiCad reads tokens, not layout (docs/ARCHITECTURE.md, ticket 03), and
  git (autocrlf), the Windows installer and Linux packages hand out the same content with
  different line endings, so a raw byte hash pinned one platform only (build ticket 34).
* Rung 1 is pinned to ``evals/rung1/layout_run/07_poured`` as committed (board unchanged).
* Rung 2 is re-pinned to the KiCad 10.0.x release demo, staged through
  ``evals/rung2/stage_input.py``; every 10.0.0..10.0.7 tag carries the same
  ``demos/pic_programmer`` content (git blob ids, recorded in the ticket). Since protocol 5
  the staging writes through the write layer (build ticket 67).

Test inputs that differ only in line endings, or by one byte, are written as raw bytes into
``tmp_path`` copies. They are only hashed here, never loaded by KiCad.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_mcp import autonomy_run, gate_state, rung_runner
from tests import _kicad_faults as faults

from test_autonomy_run_review import _measure
from test_input_prep import GOLDEN_DIR, GOLDEN_NAME

ROOT = Path(__file__).resolve().parents[1]
RUNG1_DIR = ROOT / "evals" / "rung1" / "layout_run" / "07_poured"
RUNG1_NAME = "rung1_usbc_attiny1616.kicad_pcb"

RUNG1_BOARD_PIN = "82be86c09e3f5c34de60a5ad532241bc38331afe58ee55142b171702f6cf1b86"
RUNG1_PROJECT_PIN = "2e8e4be314d62201319a3c2697ee64159aacfcfc4066c6a6f01a9aa5df7cbaab"
# Protocol 5 (build ticket 67): the release demo staged through the write layer. Measured the
# same with kicad_tools 0.23.0 and 0.24.0 (the ticket's Answer).
RUNG2_BOARD_PIN = "30fa9aecaece938cc535c72ffbe23158bae145fa49484e4fe0bf8d43438ee3aa"
RUNG2_PROJECT_PIN = "4a0326e79d763ee8be59bf5c012d4e3838599609101788c50ddd3f31db9a12d6"
OLD_PINS = {  # protocol 3, recorded in version_history["4"]
    "2622aadf25cc43a13c7b05dc38441cd80ba01a5566592f1d9b5c3bfbb718fe65",
    "0bfb1b546ea8ee647b1f1e5b003b28a053b674e41b723e18a70a23f2e007360e",
}
PROTOCOL_4_RUNG2_PINS = {  # kicad_tools PCB.load + strip_traces + its serialiser; in history["5"]
    "ad92ef757db04ea230b5ecb4a1a0175fe24ad5cb621b16d81764a62caa530807",
    "3b5f338921118b92feae08e49e2acf8772db5f62533df3791fa586961fda9b00",
}
CLEAN = {"commit": "0" * 40, "dirty": False}


def _eol_copy(src: Path, dest: Path, eol: bytes) -> Path:
    """Copy a project tree, rewriting the line endings of every file the pin covers to
    ``eol`` (what a checkout or an installer does). Other files are copied as they are."""
    shutil.copytree(src, dest)
    for path in dest.rglob("*"):
        if path.is_file() and (path.suffix.startswith(".kicad_") or path.name.endswith("-lib-table")):
            faults.rewrite_bytes(path, lambda data: _with_eol(data, eol))
    return dest


def _with_eol(data: bytes, eol: bytes) -> bytes:
    lf = data.replace(b"\r\n", b"\n")
    return lf if eol == b"\n" else lf.replace(b"\n", eol)


def _pins(pcb: Path) -> tuple[str | None, str | None]:
    return autonomy_run.board_sha(pcb), autonomy_run.project_sha(pcb)


# --- the pin: line endings only ------------------------------------------------------------------


def test_lf_and_crlf_copies_of_a_project_give_the_same_pins(tmp_path):
    lf = _eol_copy(RUNG1_DIR, tmp_path / "lf", b"\n") / RUNG1_NAME
    crlf = _eol_copy(RUNG1_DIR, tmp_path / "crlf", b"\r\n") / RUNG1_NAME
    assert b"\r\n" in crlf.read_bytes() and b"\r\n" not in lf.read_bytes()
    assert rung_runner._sha256_file(lf) != rung_runner._sha256_file(crlf)  # raw bytes differ

    assert _pins(lf) == _pins(crlf)
    assert None not in _pins(lf)


def test_for_an_all_lf_project_the_pins_are_the_raw_and_gate_fingerprints(tmp_path):
    """Same file set, same order, same layout as gate_state's fingerprint: an LF project,
    where normalising changes nothing, gives exactly the gate's own values."""
    pcb = _eol_copy(RUNG1_DIR, tmp_path / "lf", b"\n") / RUNG1_NAME
    fingerprint = gate_state.GateState()._fingerprint(pcb, {})
    assert autonomy_run.board_sha(pcb) == rung_runner._sha256_file(pcb) == fingerprint.board_sha
    assert autonomy_run.project_sha(pcb) == fingerprint.project_sha


def _flip_one_byte(path: Path) -> None:
    faults.rewrite_bytes(path, _insert_one_byte)


def _insert_one_byte(data: bytes) -> bytes:
    i = data.index(b"(") if b"(" in data else data.index(b"{")
    return data[:i + 1] + b"x" + data[i + 1:]  # one inserted byte that is not a line ending


def _covered_files(project: Path) -> list[str]:
    scan = gate_state._scan(project)
    return sorted(rel for rel, _full, _info in scan.inputs)


def test_every_covered_file_is_named_in_the_project_pin():
    covered = _covered_files(RUNG1_DIR)
    assert "rung1_usbc_attiny1616.kicad_pro" in covered  # the rules (0.127 mm) live here
    assert "rung1_usbc_attiny1616.kicad_sch" in covered
    assert any(rel.endswith(".kicad_mod") for rel in covered)
    assert any(rel.endswith(".kicad_sym") for rel in covered)


@pytest.mark.parametrize("relative", _covered_files(RUNG1_DIR) + [RUNG1_NAME])
def test_any_other_byte_change_in_a_covered_file_changes_the_pin(tmp_path, relative):
    base = _eol_copy(RUNG1_DIR, tmp_path / "base", b"\r\n")
    changed = _eol_copy(RUNG1_DIR, tmp_path / "changed", b"\r\n")
    _flip_one_byte(changed / relative)
    board_pin, project_pin = _pins(base / RUNG1_NAME)
    new_board_pin, new_project_pin = _pins(changed / RUNG1_NAME)
    assert new_project_pin != project_pin
    if relative == RUNG1_NAME:
        assert new_board_pin != board_pin
    else:
        assert new_board_pin == board_pin


def test_a_board_moved_through_kicad_tools_changes_both_pins(tmp_path):
    from kicad_tools.schema import PCB

    from kicad_mcp import write_layer

    pcb = _eol_copy(RUNG1_DIR, tmp_path / "p", b"\n") / RUNG1_NAME
    before = _pins(pcb)
    board = PCB.load(pcb)
    footprint = next(c for c in board._sexp.children if c.name == "footprint")
    at = footprint.find("at")
    at.set_value(0, float(at.values[0]) + 0.01)
    write_layer.save_pcb(board, pcb)  # PCB.save fails on 0.23.0 / Windows (build ticket 42)
    after = _pins(pcb)
    assert after[0] != before[0] and after[1] != before[1]


@pytest.mark.parametrize("change", [
    ("a relaxed .kicad_dru added", "relaxed.kicad_dru",
     b"(version 1)\r\n(rule \"x\" (severity ignore) (condition \"A.Type == 'Track'\"))\r\n"),
    ("a lone CR in place of a CRLF", None, None),
    ("a trailing space", None, b" "),
])
def test_rules_and_whitespace_other_than_crlf_still_change_the_pin(tmp_path, change):
    label, name, payload = change
    base = _eol_copy(RUNG1_DIR, tmp_path / "base", b"\r\n")
    changed = _eol_copy(RUNG1_DIR, tmp_path / "changed", b"\r\n")
    pro = changed / "rung1_usbc_attiny1616.kicad_pro"
    if name:
        (changed / name).write_bytes(payload)
    elif payload:
        faults.rewrite_bytes(pro, lambda data: data + payload)
    else:
        assert b"\r\n" in pro.read_bytes()
        faults.rewrite_bytes(pro, lambda data: data.replace(b"\r\n", b"\r", 1))
    assert autonomy_run.project_sha(changed / RUNG1_NAME) != autonomy_run.project_sha(base / RUNG1_NAME), label


def test_content_sha_reads_only_crlf_pairs_as_lf():
    sha = rung_runner.content_sha256_bytes
    assert sha(b"a\r\nb") == sha(b"a\nb")
    assert sha(b"a\rb") != sha(b"ab")  # a lone CR is content, never stripped
    assert sha(b"a\r\r\nb") != sha(b"a\r\nb")  # an extra CR before a CRLF is content too
    assert sha(b"a\n\rb") != sha(b"a\nb")


def test_a_cr_inserted_inside_a_quoted_board_token_changes_both_pins(tmp_path):
    base = _eol_copy(RUNG1_DIR, tmp_path / "base", b"\r\n") / RUNG1_NAME
    changed = _eol_copy(RUNG1_DIR, tmp_path / "changed", b"\r\n") / RUNG1_NAME
    data = changed.read_bytes()
    assert b'"GND"' in data
    faults.rewrite_bytes(changed, lambda data: data.replace(b'"GND"', b'"G\rND"', 1))  # insert only
    assert autonomy_run.board_sha(changed) != autonomy_run.board_sha(base)
    assert autonomy_run.project_sha(changed) != autonomy_run.project_sha(base)


def test_a_cr_inserted_before_a_crlf_in_the_project_file_changes_the_project_pin(tmp_path):
    base = _eol_copy(RUNG1_DIR, tmp_path / "base", b"\r\n")
    changed = _eol_copy(RUNG1_DIR, tmp_path / "changed", b"\r\n")
    pro = changed / "rung1_usbc_attiny1616.kicad_pro"
    faults.rewrite_bytes(pro, lambda data: data.replace(b"\r\n", b"\r\r\n", 1))  # insert only
    assert autonomy_run.board_sha(changed / RUNG1_NAME) == autonomy_run.board_sha(base / RUNG1_NAME)
    assert autonomy_run.project_sha(changed / RUNG1_NAME) != autonomy_run.project_sha(base / RUNG1_NAME)


def test_the_relaxed_rule_value_in_the_project_changes_the_pin(tmp_path):
    """The 0.127 mm track/clearance rules sit in the .kicad_pro; relaxing one changes the pin."""
    base = _eol_copy(RUNG1_DIR, tmp_path / "base", b"\n")
    changed = _eol_copy(RUNG1_DIR, tmp_path / "changed", b"\n")
    pro = changed / "rung1_usbc_attiny1616.kicad_pro"
    text = pro.read_bytes()
    assert b"0.127" in text
    pro.write_bytes(text.replace(b"0.127", b"0.1", 1))
    assert autonomy_run.project_sha(changed / RUNG1_NAME) != autonomy_run.project_sha(base / RUNG1_NAME)


def test_a_project_that_cannot_be_fingerprinted_has_no_pin(tmp_path, monkeypatch):
    pcb = _eol_copy(RUNG1_DIR, tmp_path / "p", b"\n") / RUNG1_NAME
    monkeypatch.setattr(gate_state, "_MAX_FILES", 0)
    assert autonomy_run.project_sha(pcb) is None
    assert autonomy_run.board_sha(tmp_path / "missing.kicad_pcb") is None
    assert autonomy_run.project_sha(tmp_path / "missing.kicad_pcb") is None


def _unreadable(monkeypatch, name: str) -> None:
    """Make one covered file unreadable to the pin hash (as a locked or vanished file is)."""
    real = rung_runner.content_sha256

    def fake(path):
        if Path(path).name == name:
            raise OSError(f"cannot read {name}")
        return real(path)

    monkeypatch.setattr(rung_runner, "content_sha256", fake)


def test_an_unreadable_input_file_leaves_the_project_without_a_pin(tmp_path, monkeypatch):
    """Fail closed: an unreadable input never silently drops out of the project pin."""
    pcb = _eol_copy(RUNG1_DIR, tmp_path / "p", b"\r\n") / RUNG1_NAME
    _unreadable(monkeypatch, "rung1_usbc_attiny1616.kicad_sch")
    assert autonomy_run.board_sha(pcb) == RUNG1_BOARD_PIN
    assert autonomy_run.project_sha(pcb) is None


def test_a_rung1_run_with_an_unreadable_input_gets_the_project_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy_run, "DEFAULT_RUNS_ROOT", tmp_path / "runs")
    pcb = _eol_copy(RUNG1_DIR, tmp_path / "rung1", b"\r\n") / RUNG1_NAME
    _unreadable(monkeypatch, "rung1_usbc_attiny1616.kicad_sch")
    reasons = _measure(pcb, tmp_path, rung=1, repo_state=lambda: CLEAN)["report"]["unofficial_reasons"]
    assert reasons == ["source project (rules, project settings, libraries) does not match the pinned "
                       "rung fixture for rung 1"]


def test_a_missing_project_pin_is_a_mismatch_not_a_match():
    protocol = rung_runner.load_protocol()
    assert autonomy_run._rung_reasons(1, RUNG1_BOARD_PIN, None, protocol) == [
        "source project (rules, project settings, libraries) does not match the pinned rung fixture for rung 1"
    ]
    assert autonomy_run._rung_reasons(1, None, RUNG1_PROJECT_PIN, protocol) == [
        "source board does not match the pinned rung fixture for rung 1"
    ]


def test_the_protocol_sha_ignores_only_line_endings(tmp_path):
    data = rung_runner.PROTOCOL_FILE.read_bytes().replace(b"\r\n", b"\n")
    (tmp_path / "lf.json").write_bytes(data)
    (tmp_path / "crlf.json").write_bytes(data.replace(b"\n", b"\r\n"))
    (tmp_path / "other.json").write_bytes(data.replace(b'"attempts": 5', b'"attempts": 6'))
    lf, crlf, other = (rung_runner.protocol_sha256(tmp_path / n) for n in ("lf.json", "crlf.json", "other.json"))
    assert lf == crlf == rung_runner.protocol_sha256()
    assert other != lf


# --- protocols 4 and 5 --------------------------------------------------------------------------


def test_protocol_8_keeps_the_history_and_pins_rung_1_with_the_decoupling_capacitor_placed():
    protocol = rung_runner.load_protocol()
    assert protocol["protocol_version"] == "8"
    history = protocol["version_history"]
    assert set(history) == {"1", "2", "3", "4", "5", "6", "7", "8"}
    # Build ticket 114: the owner's design observation of 2026-10-29, before any official batch, equal for both routers.
    assert "build ticket 114" in history["8"] and "2026-10-29" in history["8"]
    assert "decoupling capacitor" in history["8"] and "equal for both routers" in history["8"]
    assert "before any official batch under protocols 7 and 8" in history["8"]
    assert "not to be re-tuned after a measurement" in history["8"]
    assert "(149.293, 108.239) rotation 180" in history["8"] and "(153.0, 111.2) rotation 0" in history["8"]
    assert "e933373f99e9c8e6251ea498e9ca6fd1bf0e99271e536cbb5e47f4c867f4292b" in history["8"]  # protocol 7's pins stay on record
    assert "e8b08b4061c597cf4f9b5a3f236b7fed50390ec80c1c0527bc703d772cb439b0" in history["8"]
    assert RUNG1_BOARD_PIN in history["8"] and RUNG1_PROJECT_PIN in history["8"]
    assert "official slot" in history["8"] and "is new and empty" in history["8"]
    assert "Protocol 7 stays on record" in history["8"] and "no official batch" in history["8"]
    # Publish-prep ticket 02a: asset replacement with identical pad geometry, no gaming (tickets 51, 109).
    assert "ticket 02a" in history["7"] and "build tickets 51 and 109" in history["7"]
    assert "identical pad geometry" in history["7"] and "no gaming" in history["7"]
    assert "ce369e93a4f60da6a7c3d34ecc7a81be05571d0711c1783225145564b9c297e6" in history["7"]  # protocol 6's pin stays on record
    assert "official batches recorded on protocol 6 stay as history" in history["7"]
    # Build ticket 109 (tickets 107 and 108): the necked pad entry and the Freerouting levers; the pins stay.
    assert "build ticket 109" in history["6"] and "107" in history["6"] and "108" in history["6"]
    assert "ticket 51" in history["6"] and "unchanged" in history["6"]
    assert "build ticket 51" in history["4"]
    for old in OLD_PINS:  # the protocol-3 rung-2 pins stay on record
        assert old in history["4"]
    assert "build ticket 67" in history["5"] and "write layer" in history["5"]
    # Ticket 36 folds its policy change into the same bump (coordinator, 2026-10-20).
    assert "delivered_below_requested" in history["5"] and "build ticket 36" in history["5"]
    for old in PROTOCOL_4_RUNG2_PINS:  # protocol 4's rung-2 pins stay on record
        assert old in history["5"]
    rungs = protocol["rungs"]
    assert (rungs["1"]["board_sha256"], rungs["1"]["project_sha256"]) == (RUNG1_BOARD_PIN, RUNG1_PROJECT_PIN)
    assert (rungs["2"]["board_sha256"], rungs["2"]["project_sha256"]) == (RUNG2_BOARD_PIN, RUNG2_PROJECT_PIN)
    assert not (OLD_PINS | PROTOCOL_4_RUNG2_PINS) & {rungs["2"]["board_sha256"], rungs["2"]["project_sha256"]}
    assert "write layer" in rungs["2"]["description"]
    # Pinning changed nothing a batch is graded or budgeted by.
    assert protocol["attempts"] == 5 and protocol["budget_s"] == 1800 and protocol["attempt_timeout_s"] == 300
    assert protocol["policies"]["delivered_below_requested"] == "fail"
    assert set(protocol["policies"]) == {"delivered_below_requested", "necked_pad_entry"}
    assert [p["lever"] for p in protocol["backends"]["krt"]["plan"]][0] == "baseline"


def test_the_committed_rung1_source_reproduces_the_rung1_pins():
    pcb = RUNG1_DIR / RUNG1_NAME
    assert _pins(pcb) == (RUNG1_BOARD_PIN, RUNG1_PROJECT_PIN)
    assert autonomy_run._rung_reasons(1, *_pins(pcb), rung_runner.load_protocol()) == []


def test_the_rung1_board_pin_is_the_committed_blob_content():
    """The LF content git stores: the pin does not depend on the checkout's autocrlf."""
    try:
        shown = subprocess.run(
            ["git", "show", f"HEAD:evals/rung1/layout_run/07_poured/{RUNG1_NAME}"],
            cwd=ROOT, capture_output=True,
        )
    except OSError as exc:
        pytest.skip(f"git is not available ({exc}); the pin is still checked against the files")
    if shown.returncode != 0:
        pytest.skip(f"not a git checkout with the rung-1 board at HEAD: {shown.stderr.decode(errors='replace').strip()}")
    assert hashlib.sha256(shown.stdout).hexdigest() == RUNG1_BOARD_PIN


def _copy_rung1(tmp_path: Path) -> Path:
    project = tmp_path / "rung1"
    shutil.copytree(RUNG1_DIR, project)
    return project / RUNG1_NAME


@pytest.mark.parametrize("eol", [b"\r\n", b"\n"], ids=["crlf-checkout", "lf-checkout"])
def test_a_rung1_run_on_a_clean_committed_tree_is_not_unofficial_for_its_pins(tmp_path, monkeypatch, eol):
    """Run on both line endings explicitly, so a raw-byte pin fails here on any checkout."""
    monkeypatch.setattr(autonomy_run, "DEFAULT_RUNS_ROOT", tmp_path / "runs")
    pcb = _eol_copy(RUNG1_DIR, tmp_path / "rung1", eol) / RUNG1_NAME
    outcome = _measure(pcb, tmp_path, rung=1, repo_state=lambda: CLEAN)
    assert outcome["status"] == "RAN"
    report = outcome["report"]
    assert report["unofficial_reasons"] == []
    assert report["unofficial"] is False
    setup = json.loads((Path(report["run_dir"]) / "manifest.json").read_text())["setup"]
    assert setup["source_board_pin_sha256"] == RUNG1_BOARD_PIN
    assert setup["source_project_pin_sha256"] == RUNG1_PROJECT_PIN
    raw = pcb.read_bytes()
    assert setup["source_board_sha256"] == hashlib.sha256(raw).hexdigest()  # raw bytes, as handed in
    assert (setup["source_board_sha256"] == RUNG1_BOARD_PIN) == (b"\r\n" not in raw)


def test_the_same_rung1_run_without_the_pins_is_unofficial(tmp_path, monkeypatch):
    """The state before this ticket: protocol 3 had rung 1 at null/null."""
    monkeypatch.setattr(autonomy_run, "DEFAULT_RUNS_ROOT", tmp_path / "runs")
    protocol = rung_runner.load_protocol()
    protocol["rungs"]["1"]["board_sha256"] = protocol["rungs"]["1"]["project_sha256"] = None
    outcome = _measure(_copy_rung1(tmp_path), tmp_path, rung=1, repo_state=lambda: CLEAN, protocol=protocol)
    assert outcome["report"]["unofficial_reasons"] == [
        "the protocol supplied differs from the committed protocol file",  # the edited copy itself
        "rung fixture not pinned: rung 1 needs both board_sha256 and project_sha256 in the protocol file",
    ]


def test_a_changed_rung1_rules_file_makes_the_run_unofficial(tmp_path, monkeypatch):
    monkeypatch.setattr(autonomy_run, "DEFAULT_RUNS_ROOT", tmp_path / "runs")
    pcb = _copy_rung1(tmp_path)
    (pcb.parent / "relaxed.kicad_dru").write_bytes(b"(version 1)\n")
    reasons = _measure(pcb, tmp_path, rung=1, repo_state=lambda: CLEAN)["report"]["unofficial_reasons"]
    assert reasons == ["source project (rules, project settings, libraries) does not match the pinned "
                       "rung fixture for rung 1"]


# --- rung 2 from the KiCad 10.0.x release demo -----------------------------------------------------


def _demos_dir() -> Path:
    """KICAD_DEMOS_DIR, else <prefix>/share/kicad/demos beside kicad-cli (build ticket 34's
    rule; its shared fixture replaces this helper once both land)."""
    override = os.environ.get("KICAD_DEMOS_DIR")
    if override:
        return Path(override)
    from kicad_mcp import kicad_cli

    try:
        cli = Path(kicad_cli.ensure_kicad_cli_on_path())
    except kicad_cli.KicadCliError as exc:
        return Path(f"<kicad-cli not found: {exc}>")
    return cli.parent.parent / "share" / "kicad" / "demos"


def _pic_programmer() -> Path:
    source = _demos_dir() / "pic_programmer"
    if not source.is_dir():
        reason = f"KiCad demo pic_programmer not found at {source}"
        if os.environ.get("KICAD_DEMOS_REQUIRED", "").strip() not in ("", "0"):
            pytest.fail(f"KICAD_DEMOS_REQUIRED is set: {reason}", pytrace=False)
        pytest.skip(reason)
    return source


def _stage(source: Path, work: Path) -> dict:
    spec = importlib.util.spec_from_file_location("stage_input", ROOT / "evals" / "rung2" / "stage_input.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.stage(source, work)


def test_the_staged_install_demo_reproduces_the_rung2_pins(tmp_path):
    source = _pic_programmer()
    staged = _stage(source, tmp_path / "staged")
    pcb = Path(staged["board"])
    assert (staged["board_sha256"], staged["project_sha256"]) == _pins(pcb)
    assert (staged["board_sha256"], staged["project_sha256"]) == (RUNG2_BOARD_PIN, RUNG2_PROJECT_PIN)
    assert autonomy_run._rung_reasons(2, *_pins(pcb), rung_runner.load_protocol()) == []


@pytest.mark.parametrize("eol", [b"\n", b"\r\n"], ids=["lf-like-linux", "crlf-like-windows"])
def test_the_demo_with_either_line_ending_stages_to_the_same_pins(tmp_path, eol):
    """A Linux package ships the demo with LF, the Windows installer with CRLF."""
    source = _eol_copy(_pic_programmer(), tmp_path / "src", eol)
    staged = _stage(source, tmp_path / "staged")
    assert (staged["board_sha256"], staged["project_sha256"]) == (RUNG2_BOARD_PIN, RUNG2_PROJECT_PIN)


def test_the_golden_fixture_is_not_any_rung(tmp_path):
    project = tmp_path / "golden"
    shutil.copytree(GOLDEN_DIR, project)
    pins = _pins(project / GOLDEN_NAME)
    protocol = rung_runner.load_protocol()
    for rung in ("1", "2"):
        assert autonomy_run._rung_reasons(rung, *pins, protocol) != []


# --- pic_programmer attribution (paradigm-review-2 ticket 15) --------------------------------------


def _eval_module(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "evals" / "rung2" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_pic_programmer_attribution_is_confirmed_and_cited():
    third_party = (ROOT / "THIRD_PARTY.md").read_text(encoding="utf-8")
    section = third_party.split("## Attribution: `pic_programmer`", 1)[1]
    assert "CC BY-SA 4.0" in section and "KiCad project and its contributors" in section
    assert "demos/pic_programmer" in section and "Changes made by this project" in section
    floor = _eval_module("measure_input_floor")
    record = _eval_module("record_first_batch")
    for version in ("3", "4"):
        assert floor.source(version)["attribution_confirmed"] is True
        assert "THIRD_PARTY.md" in floor.source(version)["attribution_note"]
        assert "CC BY-SA 4.0" in floor.source(version)["attribution_note"]
        source = record.source(version)
        assert source["attribution_confirmed"] is True
        assert "THIRD_PARTY.md" in source["attribution"]
    for module in (floor, record):  # the source each protocol pins, never master for protocol 4
        assert module.source("3")["commit"] == "6a26ce9df3067b2233b415ba2d4cbc3164272ccc"
        assert "commit" not in module.source("4") and "10.0" in module.source("4")["release"]
    assert "6a26ce9" not in json.dumps(floor.source(rung_runner.load_protocol()["protocol_version"]))
