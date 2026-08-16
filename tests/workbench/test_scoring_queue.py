"""Durability, fairness, and privacy tests for the AI scoring queue."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

from clawjournal.workbench import index
from clawjournal.workbench import scoring_queue as queue


BASE = datetime(2026, 8, 16, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def queue_db(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr(index, "BLOBS_DIR", tmp_path / "blobs")
    conn = index.open_index()
    yield conn
    conn.close()


def _session(number: int, *, start_offset: int | None = None) -> dict:
    start = BASE + timedelta(minutes=start_offset if start_offset is not None else number)
    return {
        "session_id": f"session-{number:04d}",
        "project": "project",
        "source": "claude",
        "start_time": start.isoformat(),
        "end_time": (start + timedelta(minutes=1)).isoformat(),
        "messages": [
            {"role": "user", "content": f"task {number}"},
            {"role": "assistant", "content": "done"},
        ],
        "stats": {"user_messages": 1, "assistant_messages": 1},
    }


def _seed(conn, count: int) -> list[str]:
    index.upsert_sessions(conn, [_session(number) for number in range(count)])
    return [f"session-{number:04d}" for number in range(count)]


def test_v12_to_v13_migration_creates_queue_schema(queue_db):
    queue_db.execute("DROP TABLE scoring_jobs")
    queue_db.execute("DROP TABLE scoring_backend_state")
    queue_db.execute("PRAGMA user_version = 12")
    queue_db.commit()
    queue_db.close()

    reopened = index.open_index()
    try:
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == 13
        names = {
            row[0]
            for row in reopened.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        assert {"scoring_jobs", "scoring_backend_state"} <= names
    finally:
        reopened.close()


def test_400_revisions_enqueue_once(queue_db):
    session_ids = _seed(queue_db, 400)

    assert queue.enqueue_session_ids(queue_db, session_ids, now=BASE) == 400
    assert queue.enqueue_session_ids(queue_db, session_ids, now=BASE) == 0
    assert queue_db.execute("SELECT COUNT(*) FROM scoring_jobs").fetchone()[0] == 400
    assert queue_db.execute(
        "SELECT COUNT(DISTINCT session_id || ':' || content_revision) FROM scoring_jobs"
    ).fetchone()[0] == 400


def test_400_revision_backlog_drains_oldest_first(queue_db):
    session_ids = _seed(queue_db, 400)
    assert queue.enqueue_session_ids(queue_db, session_ids, now=BASE) == 400

    drained: list[str] = []
    for _ in range(400):
        job = queue.claim_next_job(queue_db, owner="worker", now=BASE)
        assert job is not None
        drained.append(job["session_id"])
        assert queue.complete_job(queue_db, job["job_id"], "worker", now=BASE)

    assert drained == session_ids
    assert queue.claim_next_job(queue_db, owner="worker", now=BASE) is None
    assert queue.queue_status(
        queue_db, backend="codex", enabled=True, now=BASE
    )["counts"] == {
        "pending": 0,
        "running": 0,
        "retry_wait": 0,
        "succeeded": 400,
        "failed": 0,
    }


def test_high_priority_failure_waits_while_old_healthy_job_progresses(queue_db):
    ids = _seed(queue_db, 3)
    queue.enqueue_session_ids(queue_db, ids, now=BASE)
    queue_db.execute(
        "UPDATE scoring_jobs SET priority = 100 WHERE session_id = ?", (ids[-1],)
    )
    queue_db.commit()

    failing = queue.claim_next_job(queue_db, owner="worker", now=BASE)
    assert failing["session_id"] == ids[-1]
    assert queue.retry_job_failure(
        queue_db, failing["job_id"], "worker", "score_failed", now=BASE
    ) == "retry_wait"

    healthy = queue.claim_next_job(queue_db, owner="worker", now=BASE)
    assert healthy["session_id"] == ids[0]


def test_claim_is_atomic_across_connections(queue_db):
    ids = _seed(queue_db, 1)
    queue.enqueue_session_ids(queue_db, ids, now=BASE)
    barrier = threading.Barrier(2)
    claimed: list[str | None] = []

    def worker(owner: str) -> None:
        conn = index.open_index()
        try:
            barrier.wait(timeout=5)
            job = queue.claim_next_job(conn, owner=owner, now=BASE)
            claimed.append(job["job_id"] if job else None)
        finally:
            conn.close()

    threads = [threading.Thread(target=worker, args=(f"worker-{i}",)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(claimed) == 2
    assert sum(value is not None for value in claimed) == 1


def test_expired_lease_is_recovered_after_restart(queue_db):
    ids = _seed(queue_db, 1)
    queue.enqueue_session_ids(queue_db, ids, now=BASE)
    first = queue.claim_next_job(
        queue_db, owner="old-process", now=BASE, lease_seconds=60
    )
    assert first is not None

    assert queue.claim_next_job(
        queue_db, owner="new-process", now=BASE + timedelta(seconds=59)
    ) is None
    recovered = queue.claim_next_job(
        queue_db, owner="new-process", now=BASE + timedelta(seconds=61)
    )
    assert recovered["job_id"] == first["job_id"]
    assert recovered["lease_owner"] == "new-process"


def test_explicit_score_reconciles_orphan_running_revision(queue_db):
    ids = _seed(queue_db, 1)
    queue.enqueue_session_ids(queue_db, ids, now=BASE)
    job = queue.claim_next_job(queue_db, owner="crashed-worker", now=BASE)

    assert queue.complete_revision_jobs(
        queue_db,
        ids[0],
        job["content_revision"],
        now=BASE + timedelta(seconds=1),
    ) == 1
    stored = queue.get_job(queue_db, job["job_id"])
    assert stored["state"] == "succeeded"
    assert stored["lease_owner"] is None
    assert stored["lease_expires_at"] is None
    assert stored["last_error_code"] is None


def test_bounded_retry_backoff_quarantines_fourth_failure(queue_db):
    ids = _seed(queue_db, 1)
    queue.enqueue_session_ids(queue_db, ids, now=BASE)
    clock = BASE
    expected_delays = [300, 1800, 21600]
    for attempt, delay in enumerate(expected_delays, start=1):
        job = queue.claim_next_job(queue_db, owner="worker", now=clock)
        assert job is not None
        assert queue.retry_job_failure(
            queue_db, job["job_id"], "worker", "score_timeout", now=clock
        ) == "retry_wait"
        stored = queue.get_job(queue_db, job["job_id"])
        assert stored["attempt_count"] == attempt
        clock += timedelta(seconds=delay)

    job = queue.claim_next_job(queue_db, owner="worker", now=clock)
    assert queue.retry_job_failure(
        queue_db, job["job_id"], "worker", "score_timeout", now=clock
    ) == "failed"
    assert queue.get_job(queue_db, job["job_id"])["next_attempt_at"] is None


def test_backend_cooldown_recovers_without_process_restart(queue_db):
    retry_at = queue.record_backend_cooldown(
        queue_db, "codex", "backend_rate_limited", now=BASE
    )
    assert retry_at == (BASE + timedelta(minutes=5)).isoformat()
    assert queue.backend_blocker(
        queue_db, "codex", now=BASE + timedelta(minutes=4)
    )["state"] == "cooldown"
    assert queue.backend_blocker(
        queue_db, "codex", now=BASE + timedelta(minutes=5)
    ) is None
    assert queue.get_backend_state(queue_db, "codex")["state"] == "ready"


def test_revision_change_cancels_old_job_and_enqueues_exactly_one_new_job(queue_db):
    ids = _seed(queue_db, 1)
    queue.enqueue_session_ids(queue_db, ids, now=BASE)
    old = queue_db.execute("SELECT * FROM scoring_jobs").fetchone()
    queue_db.execute(
        "UPDATE sessions SET content_revision = 'new-revision', "
        "ai_quality_score = NULL, ai_failure_value_score = NULL WHERE session_id = ?",
        (ids[0],),
    )
    queue_db.commit()

    assert queue.enqueue_session_ids(queue_db, ids, now=BASE) == 1
    assert queue.enqueue_session_ids(queue_db, ids, now=BASE) == 0
    rows = queue_db.execute(
        "SELECT content_revision, state FROM scoring_jobs ORDER BY created_at, job_id"
    ).fetchall()
    assert len(rows) == 2
    assert {tuple(row) for row in rows} == {
        (old["content_revision"], "cancelled"),
        ("new-revision", "pending"),
    }
    assert index.update_session(
        queue_db,
        ids[0],
        ai_quality_score=5,
        expected_content_revision=old["content_revision"],
    ) is False


def test_raw_backend_error_never_enters_queue_or_status(queue_db):
    ids = _seed(queue_db, 1)
    queue.enqueue_session_ids(queue_db, ids, now=BASE)
    job = queue.claim_next_job(queue_db, owner="worker", now=BASE)
    secret = "PRIVATE_TRANSCRIPT_VALUE_123"
    category, code = queue.classify_scoring_error(
        RuntimeError(f"judge exploded while reading {secret}")
    )
    assert (category, code) == ("job", "score_failed")
    queue.retry_job_failure(
        queue_db, job["job_id"], "worker", code, now=BASE
    )

    stored_text = " ".join(
        str(value)
        for row in queue_db.execute("SELECT * FROM scoring_jobs").fetchall()
        for value in row
        if value is not None
    )
    status_text = str(
        queue.queue_status(queue_db, backend="codex", enabled=True, now=BASE)
    )
    assert secret not in stored_text
    assert secret not in status_text
    assert ids[0] not in status_text


def test_retry_failed_only_resets_current_revisions(queue_db):
    ids = _seed(queue_db, 2)
    queue.enqueue_session_ids(queue_db, ids, now=BASE)
    queue_db.execute(
        "UPDATE scoring_jobs SET state = 'failed', attempt_count = 4, "
        "last_error_code = 'score_failed'"
    )
    queue_db.execute(
        "UPDATE sessions SET content_revision = 'changed' WHERE session_id = ?",
        (ids[1],),
    )
    queue_db.commit()

    assert queue.retry_failed_jobs(queue_db, now=BASE) == 1
    states = {
        row["session_id"]: row["state"]
        for row in queue_db.execute("SELECT session_id, state FROM scoring_jobs")
    }
    assert states == {ids[0]: "pending", ids[1]: "failed"}


@pytest.mark.parametrize("state", ["pending", "running", "retry_wait", "failed"])
def test_retired_checkpoint_job_is_hidden_unretryable_and_cancelled(queue_db, state):
    ids = _seed(queue_db, 1)
    queue.enqueue_session_ids(queue_db, ids, now=BASE)
    queue_db.execute(
        "UPDATE scoring_jobs SET state = ?, lease_owner = ?, lease_expires_at = ?, "
        "next_attempt_at = ?, last_error_code = ? WHERE session_id = ?",
        (
            state,
            "old-worker" if state == "running" else None,
            (BASE + timedelta(minutes=10)).isoformat() if state == "running" else None,
            (BASE + timedelta(minutes=5)).isoformat() if state == "retry_wait" else None,
            "score_failed" if state == "failed" else None,
            ids[0],
        ),
    )
    queue_db.execute(
        "UPDATE sessions SET checkpoint_active = 0 WHERE session_id = ?",
        (ids[0],),
    )
    queue_db.commit()

    status = queue.queue_status(queue_db, backend="codex", enabled=True, now=BASE)
    assert status["counts"] == {
        "pending": 0,
        "running": 0,
        "retry_wait": 0,
        "succeeded": 0,
        "failed": 0,
    }
    assert status["current"] is None
    assert status["next_retry_at"] is None
    assert queue.retry_failed_jobs(queue_db, now=BASE) == 0
    assert queue.claim_next_job(queue_db, owner="new-worker", now=BASE) is None

    stored = queue_db.execute(
        "SELECT state, last_error_code, lease_owner, lease_expires_at, "
        "next_attempt_at FROM scoring_jobs WHERE session_id = ?",
        (ids[0],),
    ).fetchone()
    assert tuple(stored) == ("cancelled", "ineligible", None, None, None)
