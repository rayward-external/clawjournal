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


def test_objective_rate_uses_all_gated_sessions_not_scored_subset(index_conn, ins):
    ins(index_conn, "scored", fvs=5, learning="x")
    for i in range(3):
        ins(index_conn, f"unscored-{i}")
    corpus = select_skill_candidates(index_conn, now=NOW)
    corpus.objective_recurrence["tool error"] = 2

    assert corpus.eligible_scored == 1
    assert corpus.objective_session_count == 4
    assert abs(corpus.objective_rates()["tool error"] - 0.5) < 1e-9


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


def test_objective_snapshot_round_trip(index_conn):
    store.save_objective_snapshot(index_conn, {"user-rejected actions": 0.2}, 15)
    last = store.last_objective_snapshot(index_conn)
    assert last is not None
    _, n, rates = last
    assert n == 15 and abs(rates["user-rejected actions"] - 0.2) < 1e-9


def test_legacy_scored_denominator_objective_snapshot_is_not_reused(index_conn):
    index_conn.execute(
        "CREATE TABLE skill_objective_snapshots "
        "(recorded_at TEXT, n INTEGER, rates_json TEXT)"
    )
    index_conn.execute(
        "INSERT INTO skill_objective_snapshots VALUES (?,?,?)",
        ("2026-05-30T00:00:00+00:00", 10, '{"tool error": 0.9}'),
    )
    index_conn.commit()

    assert store.last_objective_snapshot(index_conn) is None


def test_generate_reports_objective_trend(index_conn, ins, monkeypatch):
    store.save_objective_snapshot(index_conn, {"user-rejected actions": 0.40}, 20)   # prior run
    for i in range(12):
        ins(index_conn, f"ok{i}", quality=5, outcome="resolved", learning="y")
    # `ins` seeds no message blobs, so inject a known objective signal directly
    def fake_env(conn, corpus, **kw):
        corpus.objective_recurrence["user-rejected actions"] = 3   # 3 of 12 gated = 25%
    monkeypatch.setattr("clawjournal.cli_skill._turns.add_env_candidates", fake_env)
    monkeypatch.setattr("clawjournal.cli_skill._turns.add_rejection_candidate", lambda *a, **k: None)
    fake = FakeCaller({"rules": [
        {"kind": "do", "trigger": "t", "guidance": "read source first", "why": "w"}]})
    res = generate_skill(index_conn, window_days=3650, caller=fake, now=NOW)
    assert "user-rejected actions" in res.objective_trend
    prev, cur = res.objective_trend["user-rejected actions"]
    assert prev == 0.40 and abs(cur - 3 / 12) < 1e-9


def test_sources_count_excludes_synthetic_candidates(index_conn, ins, monkeypatch):
    # the "sources=N" footer counts REAL source sessions, not the synthetic env/rejection
    # candidate placeholders appended after selection.
    from clawjournal.skill.select import SkillCandidate
    for i in range(3):
        ins(index_conn, f"ok{i}", quality=5, outcome="resolved", learning="y")

    def fake_env(conn, corpus, **kw):
        corpus.failures.append(SkillCandidate("env-signature-0", "p", "claude", "avoid",
                                              support_count=5))
    monkeypatch.setattr("clawjournal.cli_skill._turns.add_env_candidates", fake_env)
    monkeypatch.setattr("clawjournal.cli_skill._turns.add_rejection_candidate", lambda *a, **k: None)
    fake = FakeCaller({"rules": []})
    res = generate_skill(index_conn, window_days=3650, caller=fake, now=NOW)
    assert res.meta["sources"] == 3      # 3 real sessions, not 4 (synthetic excluded)
