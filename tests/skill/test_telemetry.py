"""Recurrence telemetry (§9/D9): eligible denominator, snapshot, week-over-week trend."""

from datetime import datetime, timezone

from clawjournal.cli_skill import generate_skill
from clawjournal.skill import store
from clawjournal.skill.select import select_skill_candidates

NOW = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)


class FakeCaller:
    def __init__(self, payload):
        self.payload = payload

    def __call__(self, *, system_prompt, task_prompt):
        return self.payload


def test_eligible_denominator_and_rate(index_conn, ins):
    ins(index_conn, "f1", fvs=5, modes='["verification_skipped"]', learning="x")
    ins(index_conn, "f2", fvs=4, modes='["verification_skipped"]', learning="x")
    ins(index_conn, "ok1", quality=5, outcome="resolved", learning="y")
    ins(index_conn, "ok2", quality=5, outcome="resolved", learning="y")
    corpus = select_skill_candidates(index_conn, now=NOW)
    assert corpus.eligible_scored == 4
    assert abs(corpus.mode_rates()["verification_skipped"] - 0.5) < 1e-9


def test_snapshot_round_trip(index_conn):
    store.save_mode_snapshot(index_conn, {"verification_skipped": 0.9}, 20)
    last = store.last_mode_snapshot(index_conn)
    assert last is not None
    _, n, rates = last
    assert n == 20 and abs(rates["verification_skipped"] - 0.9) < 1e-9


def test_generate_reports_week_over_week_trend(index_conn, ins):
    store.save_mode_snapshot(index_conn, {"verification_skipped": 0.90}, 20)   # prior week
    for i in range(3):
        ins(index_conn, f"f{i}", fvs=5, modes='["verification_skipped"]', learning="x")
    for i in range(9):
        ins(index_conn, f"ok{i}", quality=5, outcome="resolved", learning="y")
    fake = FakeCaller({"rules": [
        {"kind": "avoid", "trigger": "t", "guidance": "run tests first", "why": "w",
         "taxonomy": "verification_skipped"}]})
    res = generate_skill(index_conn, window_days=3650, caller=fake, now=NOW)
    assert "verification_skipped" in res.trend
    prev, cur = res.trend["verification_skipped"]
    assert prev == 0.90                       # from last week's snapshot
    assert abs(cur - 3 / 12) < 1e-9           # this window: 3 of 12 scored
