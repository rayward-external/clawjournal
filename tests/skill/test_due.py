"""Distill-due nudge (plan §16 CH-1): cheap, bounded, fail-open, never auto-runs."""

from datetime import datetime, timedelta, timezone

from clawjournal.skill import store as _store
from clawjournal.skill.due import (
    NUDGE_BURST_SESSIONS,
    distill_due_on_connection,
    emit_session_start_nudge,
)

NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)


def _seed_skill_state(conn, *, installed_days_ago=15.0, last_seen_days_ago=None):
    """Insert one skill_rules row anchoring the user's last skill activity."""
    _store.ensure_table(conn)
    installed = (NOW - timedelta(days=installed_days_ago)).isoformat()
    seen = (NOW - timedelta(days=last_seen_days_ago)).isoformat() \
        if last_seen_days_ago is not None else installed
    conn.execute(
        "INSERT INTO skill_rules (fingerprint, kind, guidance, state, "
        "installed_at, last_seen_at) VALUES ('fp1', 'avoid', 'g', 'kept', ?, ?)",
        (installed, seen),
    )
    conn.commit()


def _seed_sessions(conn, ins, n, *, failures=0, days_ago=2.0):
    start = (NOW - timedelta(days=days_ago)).isoformat()
    for i in range(n):
        ins(conn, f"s{i}", start_time=start,
            fvs=4 if i < failures else None,
            learning="x" if i < failures else None)


def test_never_distilled_is_never_nudged(index_conn):
    # no skill_rules table at all (fresh DB) -> the user never opted in
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "never-distilled"


def test_fresh_install_not_due(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=3)
    _seed_sessions(index_conn, ins, 30)
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "fresh"


def test_preview_run_counts_as_activity(index_conn, ins):
    # installed long ago but the user ran --preview 2d ago (last_seen bumped)
    _seed_skill_state(index_conn, installed_days_ago=40, last_seen_days_ago=2)
    _seed_sessions(index_conn, ins, 30)
    assert not distill_due_on_connection(index_conn, NOW).due


def test_stale_with_activity_is_due(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.new_sessions == 6
    assert "clawjournal skill --preview" in status.message
    assert "6 new sessions" in status.message


def test_quiet_window_not_due(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=20)
    _seed_sessions(index_conn, ins, 3)   # below NUDGE_MIN_NEW_SESSIONS
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_between_min_and_stale_needs_an_event(index_conn, ins):
    # 8 days: staleness alone isn't enough — a failure event or a burst is needed
    _seed_skill_state(index_conn, installed_days_ago=8)
    _seed_sessions(index_conn, ins, 6)
    assert distill_due_on_connection(index_conn, NOW).reason == "waiting"

    # failure evidence crosses the event bar
    for i in range(3):
        index_conn.execute(
            "UPDATE sessions SET ai_failure_value_score = 4, ai_learning_summary='x' "
            "WHERE session_id = ?", (f"s{i}",))
    index_conn.commit()
    status = distill_due_on_connection(index_conn, NOW)
    assert status.due and status.failure_sessions == 3
    assert "failure evidence" in status.message


def test_burst_of_sessions_is_an_event(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=8)
    _seed_sessions(index_conn, ins, NUDGE_BURST_SESSIONS)
    assert distill_due_on_connection(index_conn, NOW).due


def test_sessions_before_anchor_do_not_count(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 30, days_ago=20)   # all predate the install
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_out_of_scope_sources_do_not_count(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=15)
    start = (NOW - timedelta(days=2)).isoformat()
    for i in range(10):
        ins(index_conn, f"cur{i}", source="cursor", start_time=start)
    assert distill_due_on_connection(index_conn, NOW).reason == "quiet"


def test_emit_prints_once_then_cools_down(index_conn, ins):
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    lines: list[str] = []
    emitted = emit_session_start_nudge(
        "claude", now=NOW, conn_factory=lambda: index_conn, printer=lines.append)
    assert emitted and len(lines) == 1
    # the emit closed the connection; reopen for the second check
    from clawjournal.workbench.index import open_index
    conn2 = open_index()
    try:
        status = distill_due_on_connection(conn2, NOW + timedelta(hours=6))
        assert not status.due and status.reason == "cooldown"
    finally:
        conn2.close()


def test_emit_fails_open_without_db():
    assert emit_session_start_nudge("claude", now=NOW, conn_factory=lambda: None) is False


def test_emit_fails_open_on_factory_error():
    def boom():
        raise RuntimeError("no db")
    assert emit_session_start_nudge("claude", now=NOW, conn_factory=boom) is False


def test_not_due_prints_nothing(index_conn):
    lines: list[str] = []
    assert emit_session_start_nudge(
        "claude", now=NOW, conn_factory=lambda: index_conn, printer=lines.append) is False
    assert lines == []


def test_skill_rules_table_present_but_empty_is_never_distilled(index_conn):
    _store.ensure_table(index_conn)   # table exists, no rows (e.g. everything rejected)
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "never-distilled"


def test_unparseable_start_time_never_counts_as_activity(index_conn, ins):
    # 'unknown' compares lexicographically greater than any ISO anchor; select.py
    # excludes such rows from the corpus and the nudge must not count them either
    # (else it re-nudges forever about sessions the run can never use).
    _seed_skill_state(index_conn, installed_days_ago=15)
    for i in range(6):
        ins(index_conn, f"bad{i}", start_time="unknown")
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_confirmed_source_scope_bounds_the_count(index_conn, ins):
    # a user scoped to claude must not be nudged about codex-only activity the
    # suggested `clawjournal skill` run would then exclude from selection
    from clawjournal.config import load_config, save_config
    cfg = load_config()
    cfg["source"] = "claude"
    save_config(cfg)
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)   # default source=codex
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_excluded_projects_do_not_count(index_conn, ins):
    # config `--exclude proj` normalizes to claude:proj — same semantics as the
    # select/export gates, so only the matching claude sessions stop counting
    from clawjournal.config import load_config, save_config
    cfg = load_config()
    cfg["excluded_projects"] = ["proj"]
    save_config(cfg)
    _seed_skill_state(index_conn, installed_days_ago=15)
    start = (NOW - timedelta(days=2)).isoformat()
    for i in range(6):
        ins(index_conn, f"c{i}", source="claude", project="proj", start_time=start)
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "quiet"


def test_rule_less_run_still_cools_the_nudge(index_conn, ins):
    # a gate-blocked / empty-distill run writes no skill_rules row; the run
    # marker must still count as conscious activity so the user is not re-nagged
    # every cooldown about a pipeline they just tried
    from clawjournal.skill.due import record_skill_run
    _seed_skill_state(index_conn, installed_days_ago=15)
    _seed_sessions(index_conn, ins, 6)
    assert distill_due_on_connection(index_conn, NOW).due
    record_skill_run(index_conn, now=NOW - timedelta(days=1))
    status = distill_due_on_connection(index_conn, NOW)
    assert not status.due and status.reason == "fresh"
