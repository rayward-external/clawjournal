"""Shared fixtures + a session-seeding helper for skill (Mode A) tests."""

import pytest

from clawjournal.workbench.index import open_index


@pytest.fixture
def index_conn(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    conn = open_index()
    yield conn
    conn.close()


@pytest.fixture
def ins():
    """Return a helper that inserts one scored session with Mode A's columns."""
    def _ins(conn, sid, *, source="codex", project="proj",
             start_time="2026-05-28T00:00:00+00:00", review_status="new",
             fvs=None, quality=None, modes=None, recovery=None, outcome=None,
             learning=None, reason=None, hold_state="auto_redacted"):
        conn.execute(
            "INSERT INTO sessions (session_id, project, source, indexed_at, start_time, "
            "review_status, ai_failure_value_score, ai_quality_score, ai_failure_modes, "
            "ai_recovery_labels, ai_outcome_badge, ai_learning_summary, ai_score_reason, hold_state) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (sid, project, source, "2026-05-28T00:00:00+00:00", start_time, review_status,
             fvs, quality, modes, recovery, outcome, learning, reason, hold_state),
        )
        conn.commit()
    return _ins
