"""Build ticket 58: the first official batch is chosen by index position, never by date.

``index.jsonl`` is append-only, so line order is the order of events. A clock that steps
back (NTP, VM resume, manual change) between two batches must not let a later passing batch
take the reported slot from an earlier failing one (the anti-cherry-pick rule). An orphan
(manifest, no index line) has no position; when its manifest date is not strictly later
than the first indexed official batch's, it cannot be ordered against it, and the orphan,
a crashed fail, is reported.

Also the two refusal-order gaps build ticket 56's review left open: refusals are numbered
by their position in the whole shared index, and cut at the REPORTED batch's first line.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from kicad_mcp import rung_runner

from test_rung_runner import PASS, fail_result, make_run, project  # noqa: F401

T = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)
DIRTY = lambda: {"commit": "abc", "dirty": True}  # noqa: E731
REFUSE = lambda board: "filled pour"  # noqa: E731


def _kinds(runs):
    return [(json.loads(x)["backend"], json.loads(x)["kind"])
            for x in (runs / "index.jsonl").read_text().splitlines()]


class _Kill(BaseException):
    pass


def _restamp(run_dir, created_utc):
    """Rewrite an orphan's manifest stamp (a JSON test fixture under tmp_path)."""
    manifest = Path(run_dir) / "manifest.json"
    data = json.loads(manifest.read_text())
    data["created_utc"] = created_utc
    manifest.chmod(0o666)
    manifest.write_text(json.dumps(data))


def _orphan(project, tmp_path, monkeypatch, runs, when, **kw):  # noqa: F811
    """A batch killed between its manifest freeze and its started line: an orphan."""
    real_append = rung_runner._append_line

    def append_or_kill(path, value):
        if value.get("kind") == "started":
            raise _Kill
        real_append(path, value)

    before = set((runs / "2" / "krt").glob("*/manifest.json"))
    monkeypatch.setattr(rung_runner, "_append_line", append_or_kill)
    with pytest.raises(_Kill):
        make_run(project, tmp_path, runs_root=runs, now=lambda: when, **kw)
    monkeypatch.setattr(rung_runner, "_append_line", real_append)
    (manifest,) = set((runs / "2" / "krt").glob("*/manifest.json")) - before
    return str(manifest.parent)


# --- reproduction: a backward clock step between two official batches ---------------------------


def test_a_backward_clock_step_does_not_report_the_later_passing_batch(project, tmp_path):  # noqa: F811
    # The reviewer's case: an official batch fails at T, then (clock stepped back) an official
    # batch passes at T-5 min. The first official batch by index position is the failing one.
    runs = tmp_path / "runs"
    failing, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T,
                          gate=lambda b: fail_result())
    passing, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T - timedelta(minutes=5))
    assert failing["unofficial"] is False and passing["unofficial"] is False
    assert (failing["attempts_passed"], passing["attempts_passed"]) == (0, 5)

    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == failing["run_dir"]
    assert result["reported"]["passed"] == 0
    assert [b["run_dir"] for b in result["other_batches"]] == [passing["run_dir"]]
    assert result["batches_with_pass"] == 1
    assert result["summary"] == "passed in 1 of 2 official batches; reported batch: 0/5"
    assert result["reported_chosen_by"] == "index_position"


def test_index_order_holds_across_several_backward_steps(project, tmp_path):  # noqa: F811
    # Distinct counts per batch (1, 3, 0, 5 of 5), each stamped earlier than the last:
    # dates would report the 5/5, the index order reports the 1/5 that came first.
    runs = tmp_path / "runs"
    for step, n in enumerate((1, 3, 0, 5)):
        calls = {"n": 0}

        def gate(board, n=n, calls=calls):
            calls["n"] += 1
            return dict(PASS) if calls["n"] <= n else fail_result()

        make_run(project, tmp_path, runs_root=runs, gate=gate,
                 now=lambda step=step: T - timedelta(minutes=step))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["passed"] == 1
    assert [b["passed"] for b in result["other_batches"]] == [3, 0, 5]


def test_a_later_unofficial_batch_with_an_earlier_stamp_does_not_displace_the_official_one(
        project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"
    official, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T, gate=lambda b: fail_result())
    make_run(project, tmp_path, runs_root=runs, now=lambda: T - timedelta(minutes=5), repo_state=DIRTY)
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == official["run_dir"]
    assert len(result["unofficial_batches"]) == 1


# --- orphans: no index line, ordered only when strictly later ------------------------------------


def test_an_orphan_strictly_later_than_the_first_indexed_official_batch_is_listed_beside_it(
        project, tmp_path, monkeypatch):  # noqa: F811
    runs = tmp_path / "runs"
    first, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T)
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T + timedelta(seconds=1))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == first["run_dir"]
    assert result["reported_chosen_by"] == "index_position"
    assert [b["run_dir"] for b in result["other_batches"]] == [orphan]
    assert result["other_batches"][0]["orphan"] is True
    assert "orphan" not in result["summary"]


@pytest.mark.parametrize("offset", [timedelta(0), -timedelta(minutes=5)], ids=["same_stamp", "earlier_stamp"])
def test_an_orphan_not_strictly_later_is_reported_as_the_crashed_fail(
        project, tmp_path, monkeypatch, offset):  # noqa: F811
    # The orphan may have been written after the indexed batch (a backward clock step) or
    # before it; with no index line nothing can tell. Conservative: report the crashed orphan.
    runs = tmp_path / "runs"
    first, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T)  # a clean 5/5
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T + offset)
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["reported"]["orphan"] is True and result["reported"]["crashed"] is True
    assert result["reported"]["passed"] == 0
    assert result["reported_chosen_by"] == "orphan_not_orderable"
    assert [b["run_dir"] for b in result["other_batches"]] == [first["run_dir"]]
    assert result["batches_with_pass"] == 1
    assert "cannot be ordered against the first indexed official batch" in result["summary"]
    assert "conservatively" in result["summary"]


def test_an_orphan_written_before_the_first_indexed_batch_is_reported(
        project, tmp_path, monkeypatch):  # noqa: F811
    # The orphan really did come first (its manifest exists before any index line).
    runs = tmp_path / "runs"
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T)
    make_run(project, tmp_path, runs_root=runs, now=lambda: T + timedelta(minutes=1))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["reported_chosen_by"] == "orphan_not_orderable"


def test_an_unofficial_orphan_never_takes_the_slot(project, tmp_path, monkeypatch):  # noqa: F811
    runs = tmp_path / "runs"
    first, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T)
    _orphan(project, tmp_path, monkeypatch, runs, T - timedelta(minutes=5), repo_state=DIRTY)
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == first["run_dir"]
    assert result["reported_chosen_by"] == "index_position"
    assert len(result["unofficial_batches"]) == 1 and result["unofficial_batches"][0]["orphan"] is True


def test_with_no_indexed_official_batch_the_earliest_orphan_is_reported(
        project, tmp_path, monkeypatch):  # noqa: F811
    runs = tmp_path / "runs"
    later = _orphan(project, tmp_path, monkeypatch, runs, T + timedelta(minutes=1))
    earlier = _orphan(project, tmp_path, monkeypatch, runs, T)
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == earlier
    assert [b["run_dir"] for b in result["other_batches"]] == [later]
    assert result["reported_chosen_by"] == "orphan_only"
    assert result["refusals_ordered_by"] == "date_orphan_fallback"


def test_an_unparsable_orphan_stamp_is_not_orderable(project, tmp_path, monkeypatch):  # noqa: F811
    runs = tmp_path / "runs"
    first, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T)
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T + timedelta(minutes=1))
    manifest = Path(orphan) / "manifest.json"
    data = json.loads(manifest.read_text())
    data["created_utc"] = "not a date"
    manifest.chmod(0o666)
    manifest.write_text(json.dumps(data))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["reported_chosen_by"] == "orphan_not_orderable"
    assert first["run_dir"] in [b["run_dir"] for b in result["other_batches"]]


# --- refusal order, gap 1: numbered by position in the WHOLE shared index ----------------------------


def test_refusals_are_numbered_by_their_position_in_the_shared_index(project, tmp_path):  # noqa: F811
    # Four freerouting refusals and a freerouting batch come first, then the krt batch, then
    # one krt refusal. Numbering refusals among themselves (krt: 0; all backends: 4) would put
    # the krt refusal before the krt batch's first line (6); its real position is 8.
    runs = tmp_path / "runs"
    fr_plan = rung_runner.load_protocol()["backends"]["freerouting"]["plan"]
    for _ in range(4):
        make_run(project, tmp_path, runs_root=runs, backend_name="freerouting", plan=fr_plan, preflight=REFUSE)
    make_run(project, tmp_path, runs_root=runs, backend_name="freerouting", plan=fr_plan)
    make_run(project, tmp_path, runs_root=runs)
    make_run(project, tmp_path, runs_root=runs, preflight=REFUSE)
    assert _kinds(runs) == [("freerouting", "refused")] * 4 + [
        ("freerouting", "started"), ("freerouting", "batch"),
        ("krt", "started"), ("krt", "batch"), ("krt", "refused")]

    krt = rung_runner.rung_result(runs, 2, "krt")
    assert krt["refusals_ordered_by"] == "index_position"
    assert len(krt["refused_batches"]) == 1
    assert krt["refusals_before_first_scored_batch"] == 0
    assert "refusal(s) before" not in krt["summary"]
    fr = rung_runner.rung_result(runs, 2, "freerouting")
    assert fr["refusals_before_first_scored_batch"] == 4


# --- refusal order, gap 2: the cut is the REPORTED batch's first line, not the first of any ---------


def test_a_refusal_after_an_unofficial_batch_and_before_the_official_one_counts(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs, repo_state=DIRTY)
    make_run(project, tmp_path, runs_root=runs, preflight=REFUSE)
    official, _ = make_run(project, tmp_path, runs_root=runs)
    assert [k for _, k in _kinds(runs)] == ["started", "batch", "refused", "started", "batch"]

    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == official["run_dir"]
    assert result["refusals_ordered_by"] == "index_position"
    assert result["refusals_before_first_scored_batch"] == 1
    assert "1 refusal(s) before the first scored batch" in result["summary"]


# --- fix round 1 (review of a08353b) ---------------------------------------------------------------


def test_the_orphan_anchor_is_the_first_indexed_official_batch_not_an_unofficial_one(
        project, tmp_path, monkeypatch):  # noqa: F811
    # Unofficial batch at T, official orphan at T+5 min, official 5/5 batch at T+10 min. The
    # orphan is not strictly later than the first OFFICIAL indexed batch (T+10), so it is
    # reported; anchoring on the unofficial batch (T) would wrongly order it after.
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs, now=lambda: T, repo_state=DIRTY)
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T + timedelta(minutes=5))
    official, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T + timedelta(minutes=10))
    assert official["unofficial"] is False and official["attempts_passed"] == 5
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["reported_chosen_by"] == "orphan_not_orderable"


def test_an_unorderable_orphan_keeps_the_refusals_before_the_first_indexed_official_batch(
        project, tmp_path, monkeypatch):  # noqa: F811
    # Ticket 56's count must not shrink when an orphan takes the slot: a refusal stamped
    # T+1 min at index position 0, then an official batch at T, then an orphan stamped T.
    # By position the refusal came before the official batch (main counted 1); by date alone
    # it is after the orphan. The union of both orders counts it.
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs, preflight=REFUSE, now=lambda: T + timedelta(minutes=1))
    make_run(project, tmp_path, runs_root=runs, now=lambda: T)
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T)
    assert [k for _, k in _kinds(runs)] == ["refused", "started", "batch"]
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["refusals_before_first_scored_batch"] == 1
    assert result["refusals_ordered_by"] == "index_position_or_date_orphan"
    assert "1 refusal(s) before the first scored batch" in result["summary"]
    assert ("(by index position before the first indexed official batch, or by date against "
            "the reported orphan") in result["summary"]


def test_an_unorderable_orphan_also_counts_a_later_line_refusal_stamped_before_it(
        project, tmp_path, monkeypatch):  # noqa: F811
    # The other half of the union: a refusal written after the official batch but stamped
    # no later than the orphan may have come before the orphan; it is shown as before.
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs, now=lambda: T)
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T)
    make_run(project, tmp_path, runs_root=runs, preflight=REFUSE, now=lambda: T - timedelta(minutes=1))
    make_run(project, tmp_path, runs_root=runs, preflight=REFUSE, now=lambda: T + timedelta(minutes=1))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["refusals_before_first_scored_batch"] == 1


def test_of_several_orphans_an_unparsable_one_is_reported(project, tmp_path, monkeypatch):  # noqa: F811
    # Orphan A is strictly later than the official batch; orphan B's stamp cannot be read,
    # so B cannot be ordered and is reported.
    runs = tmp_path / "runs"
    first, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T)
    a = _orphan(project, tmp_path, monkeypatch, runs, T + timedelta(minutes=1))
    b = _orphan(project, tmp_path, monkeypatch, runs, T + timedelta(minutes=2))
    _restamp(b, "not a date")
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == b
    assert result["reported_chosen_by"] == "orphan_not_orderable"
    assert [x["run_dir"] for x in result["other_batches"]] == [first["run_dir"], a]


def test_orphans_are_ordered_by_time_not_by_the_text_of_mixed_offset_stamps(
        project, tmp_path, monkeypatch):  # noqa: F811
    # 12:30+00:00 sorts before 13:00+01:00 as text, but 13:00+01:00 is 12:00 UTC, earlier.
    runs = tmp_path / "runs"
    later = _orphan(project, tmp_path, monkeypatch, runs, T + timedelta(minutes=30))
    earlier = _orphan(project, tmp_path, monkeypatch, runs, T.astimezone(timezone(timedelta(hours=1))))
    assert json.loads((Path(earlier) / "manifest.json").read_text())["created_utc"].endswith("+01:00")
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == earlier
    assert result["reported_chosen_by"] == "orphan_only"
    assert [x["run_dir"] for x in result["other_batches"]] == [later]


def test_a_mixed_offset_refusal_stamp_is_ordered_by_time_against_an_orphan(
        project, tmp_path, monkeypatch):  # noqa: F811
    # The orphan is stamped T (12:00Z) and only reachable by date. The refusal stamped
    # 12:30+01:00 is 11:30Z, before it, though its text sorts after "12:00:00+00:00".
    runs = tmp_path / "runs"
    _orphan(project, tmp_path, monkeypatch, runs, T)
    make_run(project, tmp_path, runs_root=runs, preflight=REFUSE,
             now=lambda: (T - timedelta(minutes=30)).astimezone(timezone(timedelta(hours=1))))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["refusals_ordered_by"] == "date_orphan_fallback"
    assert result["refusals_before_first_scored_batch"] == 1


def test_a_naive_orphan_stamp_is_not_orderable(project, tmp_path, monkeypatch):  # noqa: F811
    # A stamp with no time zone cannot be compared with an aware one; reading it as UTC
    # would order this orphan after the batch (fail-open). It is reported instead.
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs, now=lambda: T)
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T + timedelta(minutes=1))
    _restamp(orphan, (T + timedelta(minutes=1)).replace(tzinfo=None).isoformat())
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["reported_chosen_by"] == "orphan_not_orderable"


def test_an_unreadable_anchor_date_reports_the_orphan_and_says_why(project, tmp_path, monkeypatch):  # noqa: F811
    # The first indexed official batch's own stamp has no time zone: no orphan can be
    # ordered against it, so the orphan (stamped one minute later) is reported.
    runs = tmp_path / "runs"
    first, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T.replace(tzinfo=None))
    assert first["unofficial"] is False
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T + timedelta(minutes=1))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["reported_chosen_by"] == "anchor_unreadable"
    assert "first indexed official batch's date is unreadable" in result["summary"]
    assert "not strictly later" not in result["summary"]


def test_an_unreadable_anchor_without_orphans_changes_nothing(project, tmp_path):  # noqa: F811
    runs = tmp_path / "runs"
    first, _ = make_run(project, tmp_path, runs_root=runs, now=lambda: T.replace(tzinfo=None))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == first["run_dir"]
    assert result["reported_chosen_by"] == "index_position"


def test_of_two_unorderable_orphans_the_earliest_is_reported(project, tmp_path, monkeypatch):  # noqa: F811
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs, now=lambda: T)
    late = _orphan(project, tmp_path, monkeypatch, runs, T - timedelta(minutes=1))
    early = _orphan(project, tmp_path, monkeypatch, runs, T - timedelta(minutes=2))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == early
    assert result["reported_chosen_by"] == "orphan_not_orderable"
    assert late in [b["run_dir"] for b in result["other_batches"]]


# --- fix round 2 (delta review of a233cf5): unreadable stamps on the refusal side -------------------


def test_a_refusal_with_an_unreadable_stamp_counts_before_an_orphan(project, tmp_path, monkeypatch):  # noqa: F811
    # Orphans only: refusals are ordered by date. A refusal whose own stamp has no time zone
    # cannot be ordered against the orphan, so it is shown as before, not hidden.
    runs = tmp_path / "runs"
    _orphan(project, tmp_path, monkeypatch, runs, T)
    make_run(project, tmp_path, runs_root=runs, preflight=REFUSE,
             now=lambda: (T + timedelta(minutes=1)).replace(tzinfo=None))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["refusals_ordered_by"] == "date_orphan_fallback"
    assert result["refusals_before_first_scored_batch"] == 1


def test_an_unreadable_refusal_after_the_displaced_batch_counts_before_the_orphan(
        project, tmp_path, monkeypatch):  # noqa: F811
    # The orphan displaces an indexed official batch; the refusal comes after that batch's
    # line (not before it by position) and its stamp is unreadable: counted by the date half.
    runs = tmp_path / "runs"
    make_run(project, tmp_path, runs_root=runs, now=lambda: T)
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T)
    make_run(project, tmp_path, runs_root=runs, preflight=REFUSE,
             now=lambda: (T + timedelta(minutes=1)).replace(tzinfo=None))
    assert [k for _, k in _kinds(runs)] == ["started", "batch", "refused"]
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["refusals_ordered_by"] == "index_position_or_date_orphan"
    assert result["refusals_before_first_scored_batch"] == 1


def test_an_orphan_with_an_unreadable_stamp_counts_every_refusal_before_it(
        project, tmp_path, monkeypatch):  # noqa: F811
    # Orphans only, and the orphan's own stamp is unreadable: no refusal can be ordered
    # after it, so the (later stamped) refusal is shown as before.
    runs = tmp_path / "runs"
    orphan = _orphan(project, tmp_path, monkeypatch, runs, T)
    _restamp(orphan, "not a date")
    make_run(project, tmp_path, runs_root=runs, preflight=REFUSE, now=lambda: T + timedelta(minutes=1))
    result = rung_runner.rung_result(runs, 2, "krt")
    assert result["reported"]["run_dir"] == orphan
    assert result["refusals_ordered_by"] == "date_orphan_fallback"
    assert result["refusals_before_first_scored_batch"] == 1
