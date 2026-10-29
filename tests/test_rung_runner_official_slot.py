"""Policy: only an OFFICIAL scored batch fills the reported slot for a (rung, backend,
protocol sha); unofficial batches are history listed beside it and never a pass."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_mcp import rung_runner

from test_rung_runner import fail_result, make_run, project  # noqa: F401

DIRTY = lambda: {"commit": "abc", "dirty": True}  # noqa: E731


def test_an_unofficial_batch_does_not_lock_the_reported_slot(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"
    unofficial, _ = make_run(project, tmp_path, runs_root=runs, repo_state=DIRTY, gate=lambda b: fail_result())
    official, _ = make_run(project, tmp_path, runs_root=runs)
    assert unofficial["unofficial"] is True and official["unofficial"] is False

    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["status"] == "SCORED"
    assert result["reported"]["run_dir"] == official["run_dir"]
    assert result["batches_with_pass"] == 1
    assert [b["run_dir"] for b in result["unofficial_batches"]] == [unofficial["run_dir"]]
    assert "dirty" in " ".join(result["unofficial_batches"][0]["unofficial_reasons"])
    assert "1 unofficial batch" in result["summary"]


def test_unofficial_batches_are_never_counted_as_passes(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs, repo_state=DIRTY)  # 5/5 gate PASS, but unofficial
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["batches_with_pass"] == 0
    assert result["reported"] is None
    assert result["status"] == "NO_OFFICIAL_BATCH"
    assert "no official batch yet" in result["summary"]
    assert len(result["unofficial_batches"]) == 1
    assert result["unofficial_batches"][0]["passed"] == 5  # shown as history, labelled, not counted


def test_a_crashed_official_batch_still_locks_the_slot(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"

    def dies(board, params, timeout_s, log):
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        make_run(project, tmp_path, runs_root=runs, backend=dies)
    good, _ = make_run(project, tmp_path, runs_root=runs)
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["crashed"] is True
    assert result["reported"]["run_dir"] != good["run_dir"]
    assert result["batches_with_pass"] == 1


def test_a_crashed_unofficial_batch_does_not_lock_the_slot(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"

    def dies(board, params, timeout_s, log):
        raise SystemExit(1)

    with pytest.raises(SystemExit):
        make_run(project, tmp_path, runs_root=runs, backend=dies, repo_state=DIRTY)
    good, _ = make_run(project, tmp_path, runs_root=runs)
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == good["run_dir"]
    assert len(result["unofficial_batches"]) == 1 and result["unofficial_batches"][0]["crashed"] is True


# History is the committed, sanitised results file (``rung_runner.HISTORY_FILE``, written once by
# ``scripts/export_ladder_results.py`` from the old runs folder). The live state is the local index of
# the runs root. ``rung_result`` reads both: history first (those events came earlier), then the index.
FIRST_OFFICIAL_DAY = "2026-10-28"  # protocol 5, the first official batches


def history_rows() -> list[dict]:
    return json.loads(Path(rung_runner.HISTORY_FILE).read_text(encoding="utf-8"))["rows"]


def history_batch(rung, backend, version) -> dict:
    return next(r for r in history_rows()
                if r["kind"] == "batch" and (r["rung"], r["backend"], r["protocol_version"]) == (rung, backend, version))


def write_index(runs: Path, rows: list[dict]) -> None:
    runs.mkdir(parents=True, exist_ok=True)
    (runs / "index.jsonl").write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in rows), encoding="utf-8")


def test_the_history_file_is_committed_and_carries_no_machine_path():
    text = Path(rung_runner.HISTORY_FILE).read_text(encoding="utf-8")
    assert re.search(r"[A-Za-z]:[/\x5c]", text) is None and "/Users/" not in text and "/home/" not in text
    assert ".scratch" not in text
    assert all(r["run_dir"].startswith("history/") for r in history_rows() if "run_dir" in r)


def test_the_first_measurement_is_read_as_unofficial_history(tmp_path):
    """Build ticket 110's meaning, read from the committed history file and an EMPTY runs root."""
    before = [r for r in history_rows() if r["date"][:10] < FIRST_OFFICIAL_DAY]
    assert any(r["kind"] == "batch" and r["backend"] == "krt" for r in before)
    assert all(r.get("unofficial", True) for r in before if r["kind"] == "batch")
    batch = next(r for r in before if r["kind"] == "batch" and r["backend"] == "krt")

    old = rung_runner.rung_result(
        tmp_path / "runs", batch["rung"], "krt", protocol_version=batch["protocol_version"],
        protocol_sha=batch["protocol_sha256"])
    assert old["status"] == "NO_OFFICIAL_BATCH"
    assert old["reported"] is None
    assert old["batches_with_pass"] == 0
    assert len(old["unofficial_batches"]) >= 1
    assert all(b["unofficial"] for b in old["unofficial_batches"])
    assert "no official batch yet" in old["summary"]

    current = rung_runner.rung_result(tmp_path / "runs", batch["rung"], "krt")  # the committed (newer) protocol
    assert current["reported"] is None
    assert any(b["protocol_version"] == batch["protocol_version"] for b in current["other_protocol_batches"])


@pytest.mark.parametrize("rung,backend", [(1, "krt"), (1, "freerouting"), (2, "krt"), (2, "freerouting")])
def test_protocols_5_and_6_are_history_with_their_official_slot_filled(tmp_path, rung, backend):
    """The official batches already measured stay what they were: read from the committed file,
    keyed by their own protocol sha, reported as history with the recorded counts."""
    for version in ("5", "6"):
        if not any(r["kind"] == "batch" and (r["rung"], r["backend"], r["protocol_version"]) == (rung, backend, version)
                   for r in history_rows()):
            continue  # rung 2 has no protocol 6 batch
        row = history_batch(rung, backend, version)
        assert row["unofficial"] is False
        result = rung_runner.rung_result(
            tmp_path / "runs", rung, backend, protocol_version=version, protocol_sha=row["protocol_sha256"])
        assert result["status"] == "SCORED"
        assert result["reported"]["run_dir"] == row["run_dir"] and result["reported"]["run_dir"].startswith("history/")
        assert (result["reported"]["passed"], result["reported"]["planned"]) == (row["passed"], row["planned"])
        assert result["batches_with_pass"] == (1 if row["autonomous_pass"] else 0)


def test_protocol_8_has_no_official_batch_yet_and_shows_5_and_6_beside_it(tmp_path):
    """The current protocol's slots are empty for every rung and backend; the older official
    batches are listed as other protocols, never pooled. Update when protocol 8's batches are exported (protocols 7 and 8 never had one)."""
    assert rung_runner.PROTOCOL_VERSION == "8"
    assert not [r for r in history_rows() if r["protocol_version"] in ("7", "8")]
    for rung in (1, 2):
        for backend in ("krt", "freerouting"):
            result = rung_runner.rung_result(tmp_path / "runs", rung, backend)
            assert result["status"] in ("NOT_RUN", "UNAVAILABLE", "REFUSED")
            assert result["reported"] is None and result["batches_with_pass"] == 0
            assert {b["protocol_version"] for b in result["other_protocol_batches"]} >= {"5"}


def test_a_live_local_batch_fills_the_current_slot_and_history_stays_beside_it(tmp_path):
    """The live index of the runs root and the committed history together: one official row under the
    current protocol is reported, the history of the older protocols is listed next to it."""
    runs = tmp_path / "runs"
    template = history_batch(1, "krt", "6")
    official = dict(
        template, unofficial=False, date="2099-01-01T00:00:00+00:00", run_dir="synthetic/official",
        protocol_version=rung_runner.PROTOCOL_VERSION, protocol_sha256=rung_runner.protocol_sha256())
    write_index(runs, [official])

    current = rung_runner.rung_result(runs, 1, "krt")
    assert current["reported"] is not None and current["reported"]["run_dir"] == "synthetic/official"
    assert current["status"] != "NO_OFFICIAL_BATCH"
    assert {"5", "6"} <= {b["protocol_version"] for b in current["other_protocol_batches"]}


def test_a_local_index_cannot_rewrite_a_protocol_whose_history_is_committed(tmp_path):
    """A second official row for protocol 6 in the local index does not replace the first one:
    first by position, and the committed history comes first."""
    template = history_batch(1, "freerouting", "6")
    runs = tmp_path / "runs"
    write_index(runs, [dict(template, run_dir="synthetic/second", passed=0, autonomous_pass=False)])
    result = rung_runner.rung_result(
        runs, 1, "freerouting", protocol_version="6", protocol_sha=template["protocol_sha256"])
    assert result["reported"]["run_dir"] == template["run_dir"]
    assert result["batches"] == 2 and result["reported_chosen_by"] == "index_position"


@pytest.mark.usefixtures("project")
def test_an_official_row_with_a_wrong_protocol_sha_is_never_the_reported_batch(request, tmp_path):
    """Build ticket 110 review: the pool is the protocol file's CONTENT, not its version label. An
    official batch under the current protocol version but another sha is counted aside. Built from
    a real run (no .scratch input), then every index row gets a sha the current file does not have."""
    runs = tmp_path / "runs"
    make_run(request.getfixturevalue("project"), tmp_path, runs_root=runs)
    own = rung_runner.rung_result(runs, 2, "krt")
    assert own["reported"] is not None and own["different_protocol_sha_batches"] == 0  # the control

    rows = [json.loads(x) for x in (runs / "index.jsonl").read_text(encoding="utf-8").splitlines() if x]
    assert rows and all(r["protocol_version"] == rung_runner.PROTOCOL_VERSION for r in rows)
    wrong = "0" * 64
    assert wrong != rung_runner.protocol_sha256()
    # Only the index goes to a fresh runs root: the run directory's manifest (with the true sha)
    # would otherwise be found on disk as an orphan and reported.
    elsewhere = tmp_path / "runs-index-only"
    write_index(elsewhere, [dict(r, protocol_sha256=wrong) for r in rows])

    result = rung_runner.rung_result(elsewhere, 2, "krt")
    assert result["reported"] is None
    assert result["different_protocol_sha_batches"] == 1


@pytest.mark.usefixtures("project")
def test_an_orphan_manifest_with_a_wrong_protocol_sha_is_counted_aside_not_reported(request, tmp_path):
    """The orphan scan (a run directory with no index line) pools by the manifest's sha too."""
    runs = tmp_path / "runs"
    good, _ = make_run(request.getfixturevalue("project"), tmp_path, runs_root=runs)
    (runs / "index.jsonl").unlink()  # the run is now an orphan: a manifest with no index line
    own = rung_runner.rung_result(runs, 2, "krt")
    assert own["reported"] is not None and own["reported"]["kind"] == "orphan"  # the control
    assert own["different_protocol_sha_batches"] == 0

    manifest_path = Path(good["run_dir"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["protocol_sha256"] == rung_runner.protocol_sha256()
    manifest["protocol_sha256"] = "0" * 64
    manifest_path.chmod(0o666)  # a finished run's manifest is read-only
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"] is None
    assert result["different_protocol_sha_batches"] == 1
