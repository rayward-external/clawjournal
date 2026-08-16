"""Durable, revision-keyed scheduling primitives for background scoring.

The queue stores control-plane metadata only.  Transcript content and raw
backend errors never belong in these tables: workers persist a small fixed
error code and keep all session content in the existing private blob store.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Sequence


SCORING_JOB_STATES = frozenset(
    {"pending", "running", "retry_wait", "succeeded", "failed", "cancelled"}
)
SCORING_BACKEND_STATES = frozenset({"ready", "cooldown", "action_required"})
SCORING_LEASE_SECONDS = 10 * 60
SCORING_SESSION_BACKOFF_SECONDS = (5 * 60, 30 * 60, 6 * 60 * 60)
SCORING_MAX_SESSION_ATTEMPTS = len(SCORING_SESSION_BACKOFF_SECONDS) + 1
SCORING_BACKEND_BACKOFF_SECONDS = (5 * 60, 30 * 60, 6 * 60 * 60)

JOB_ERROR_CODES = frozenset(
    {
        "lease_expired",
        "stale_revision",
        "ineligible",
        "already_scored",
        "score_timeout",
        "score_failed",
        "transcript_unavailable",
        "backend_cooldown",
        "backend_action_required",
    }
)
BACKEND_ERROR_CODES = frozenset(
    {
        "backend_rate_limited",
        "backend_temporary",
        "backend_missing",
        "backend_auth",
        "backend_unavailable",
    }
)
ACTION_REQUIRED_CODES = frozenset(
    {"backend_missing", "backend_auth", "backend_unavailable"}
)


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    return _utc(value).isoformat()


def _chunks(values: Sequence[str], size: int = 400) -> list[list[str]]:
    return [list(values[start:start + size]) for start in range(0, len(values), size)]


def enqueue_session_ids(
    conn: sqlite3.Connection,
    session_ids: Sequence[str],
    *,
    priority: int = 0,
    now: datetime | None = None,
) -> int:
    """Idempotently queue each current non-empty content revision.

    A cancelled revision may become eligible again after a hold or scope
    change, so a fresh eligibility pass reactivates it.  Failed jobs require
    an explicit retry and succeeded jobs remain immutable audit rows.
    """

    requested = list(dict.fromkeys(
        session_id for session_id in session_ids
        if isinstance(session_id, str) and session_id
    ))
    if not requested:
        return 0
    if not isinstance(priority, int) or isinstance(priority, bool):
        raise ValueError("priority must be an integer")
    if conn.in_transaction:
        raise RuntimeError("queue enqueue requires a connection without an active transaction")

    stamp = _iso(now)
    rows: list[sqlite3.Row] = []
    for batch in _chunks(requested):
        placeholders = ",".join("?" for _ in batch)
        rows.extend(conn.execute(
            "SELECT session_id, content_revision FROM sessions "
            f"WHERE session_id IN ({placeholders}) "
            "AND content_revision IS NOT NULL AND content_revision != '' "
            "AND COALESCE(checkpoint_active, 1) = 1",
            batch,
        ).fetchall())
    current = {
        str(row["session_id"]): str(row["content_revision"])
        for row in rows
    }
    if not current:
        return 0

    existing: dict[tuple[str, str], str] = {}
    for batch in _chunks(list(current)):
        placeholders = ",".join("?" for _ in batch)
        for row in conn.execute(
            "SELECT session_id, content_revision, state FROM scoring_jobs "
            f"WHERE session_id IN ({placeholders})",
            batch,
        ).fetchall():
            existing[(str(row["session_id"]), str(row["content_revision"]))] = str(
                row["state"]
            )

    queued = sum(
        1
        for session_id, revision in current.items()
        if existing.get((session_id, revision)) in (None, "cancelled")
    )
    conn.execute("BEGIN IMMEDIATE")
    try:
        for session_id, revision in current.items():
            conn.execute(
                "UPDATE scoring_jobs SET state = 'cancelled', "
                "lease_owner = NULL, lease_expires_at = NULL, "
                "next_attempt_at = NULL, stage = 'queued', "
                "stage_current = NULL, stage_total = NULL, "
                "last_error_code = 'stale_revision', last_error_at = ?, updated_at = ? "
                "WHERE session_id = ? AND content_revision != ? "
                "AND state IN ('pending', 'running', 'retry_wait')",
                (stamp, stamp, session_id, revision),
            )
            conn.execute(
                "INSERT INTO scoring_jobs "
                "(job_id, session_id, content_revision, state, priority, attempt_count, "
                " stage, created_at, updated_at) "
                "VALUES (?, ?, ?, 'pending', ?, 0, 'queued', ?, ?) "
                "ON CONFLICT(session_id, content_revision) DO UPDATE SET "
                "priority = MAX(scoring_jobs.priority, excluded.priority), "
                "state = CASE WHEN scoring_jobs.state = 'cancelled' "
                "             THEN 'pending' ELSE scoring_jobs.state END, "
                "next_attempt_at = CASE WHEN scoring_jobs.state = 'cancelled' "
                "                       THEN NULL ELSE scoring_jobs.next_attempt_at END, "
                "lease_owner = CASE WHEN scoring_jobs.state = 'cancelled' "
                "                   THEN NULL ELSE scoring_jobs.lease_owner END, "
                "lease_expires_at = CASE WHEN scoring_jobs.state = 'cancelled' "
                "                        THEN NULL ELSE scoring_jobs.lease_expires_at END, "
                "last_error_code = CASE WHEN scoring_jobs.state = 'cancelled' "
                "                       THEN NULL ELSE scoring_jobs.last_error_code END, "
                "last_error_at = CASE WHEN scoring_jobs.state = 'cancelled' "
                "                     THEN NULL ELSE scoring_jobs.last_error_at END, "
                "stage = CASE WHEN scoring_jobs.state = 'cancelled' "
                "             THEN 'queued' ELSE scoring_jobs.stage END, "
                "updated_at = excluded.updated_at",
                (str(uuid.uuid4()), session_id, revision, priority, stamp, stamp),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return queued


def recover_expired_leases(
    conn: sqlite3.Connection, *, now: datetime | None = None, commit: bool = True
) -> int:
    """Return expired running jobs to the due queue without consuming a retry."""

    stamp = _iso(now)
    cursor = conn.execute(
        "UPDATE scoring_jobs SET state = 'retry_wait', next_attempt_at = ?, "
        "lease_owner = NULL, lease_expires_at = NULL, stage = 'queued', "
        "stage_current = NULL, stage_total = NULL, "
        "last_error_code = 'lease_expired', last_error_at = ?, updated_at = ? "
        "WHERE state = 'running' AND lease_expires_at IS NOT NULL "
        "AND lease_expires_at <= ?",
        (stamp, stamp, stamp, stamp),
    )
    if commit:
        conn.commit()
    return max(cursor.rowcount, 0)


def _cancel_noncurrent_or_inactive_jobs(
    conn: sqlite3.Connection, *, now: datetime | None = None
) -> int:
    """Fail closed jobs whose exact checkpoint can no longer be scored.

    Logical-session reconciliation can retire a checkpoint without changing
    its content revision.  Such a job must not remain visible forever merely
    because claim eligibility correctly skips it.  Keep succeeded rows as an
    audit trail, but cancel every unfinished/quarantined job that is stale or
    belongs to an inactive checkpoint.
    """

    stamp = _iso(now)
    cursor = conn.execute(
        "UPDATE scoring_jobs SET state = 'cancelled', "
        "lease_owner = NULL, lease_expires_at = NULL, next_attempt_at = NULL, "
        "stage = 'queued', stage_current = NULL, stage_total = NULL, "
        "last_error_code = CASE WHEN EXISTS ("
        " SELECT 1 FROM sessions current_session "
        " WHERE current_session.session_id = scoring_jobs.session_id "
        " AND current_session.content_revision = scoring_jobs.content_revision"
        ") THEN 'ineligible' ELSE 'stale_revision' END, "
        "last_error_at = ?, updated_at = ? "
        "WHERE (state IN ('pending', 'running', 'retry_wait') "
        " AND NOT EXISTS ("
        "  SELECT 1 FROM sessions eligible_session "
        "  WHERE eligible_session.session_id = scoring_jobs.session_id "
        "  AND eligible_session.content_revision = scoring_jobs.content_revision "
        "  AND COALESCE(eligible_session.checkpoint_active, 1) = 1"
        " )) OR (state = 'failed' AND EXISTS ("
        "  SELECT 1 FROM sessions retired_session "
        "  WHERE retired_session.session_id = scoring_jobs.session_id "
        "  AND retired_session.content_revision = scoring_jobs.content_revision "
        "  AND COALESCE(retired_session.checkpoint_active, 1) = 0"
        " ))",
        (stamp, stamp),
    )
    return max(cursor.rowcount, 0)


def claim_next_job(
    conn: sqlite3.Connection,
    *,
    owner: str,
    now: datetime | None = None,
    lease_seconds: int = SCORING_LEASE_SECONDS,
) -> dict[str, Any] | None:
    """Atomically lease the oldest due job within the highest priority."""

    if not isinstance(owner, str) or not owner:
        raise ValueError("owner must be a non-empty string")
    if lease_seconds <= 0:
        raise ValueError("lease_seconds must be positive")
    if conn.in_transaction:
        raise RuntimeError("queue claim requires a connection without an active transaction")
    clock = _utc(now)
    stamp = clock.isoformat()
    expires = (clock + timedelta(seconds=lease_seconds)).isoformat()

    conn.execute("BEGIN IMMEDIATE")
    try:
        recover_expired_leases(conn, now=clock, commit=False)
        _cancel_noncurrent_or_inactive_jobs(conn, now=clock)
        row = conn.execute(
            "SELECT j.* FROM scoring_jobs j "
            "JOIN sessions s ON s.session_id = j.session_id "
            " AND s.content_revision = j.content_revision "
            "WHERE (j.state = 'pending' OR (j.state = 'retry_wait' "
            " AND (j.next_attempt_at IS NULL OR j.next_attempt_at <= ?))) "
            "AND COALESCE(s.checkpoint_active, 1) = 1 "
            "ORDER BY j.priority DESC, "
            "CASE WHEN s.start_time IS NULL OR s.start_time = '' "
            "     THEN j.created_at ELSE s.start_time END ASC, "
            "j.created_at ASC, j.job_id ASC LIMIT 1",
            (stamp,),
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        cursor = conn.execute(
            "UPDATE scoring_jobs SET state = 'running', lease_owner = ?, "
            "lease_expires_at = ?, next_attempt_at = NULL, stage = 'preparing', "
            "stage_current = NULL, stage_total = NULL, updated_at = ? "
            "WHERE job_id = ? AND state IN ('pending', 'retry_wait')",
            (owner, expires, stamp, row["job_id"]),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            return None
        claimed = conn.execute(
            "SELECT * FROM scoring_jobs WHERE job_id = ?", (row["job_id"],)
        ).fetchone()
        conn.commit()
        return dict(claimed) if claimed is not None else None
    except Exception:
        conn.rollback()
        raise


def update_job_stage(
    conn: sqlite3.Connection,
    job_id: str,
    owner: str,
    stage: str,
    *,
    current: int | None = None,
    total: int | None = None,
    now: datetime | None = None,
) -> bool:
    if stage not in {"queued", "preparing", "locating_evidence", "final_scoring", "persisting"}:
        raise ValueError("invalid scoring stage")
    if current is not None and current < 0:
        raise ValueError("current must be non-negative")
    if total is not None and total < 0:
        raise ValueError("total must be non-negative")
    cursor = conn.execute(
        "UPDATE scoring_jobs SET stage = ?, stage_current = ?, stage_total = ?, "
        "updated_at = ? WHERE job_id = ? AND state = 'running' AND lease_owner = ?",
        (stage, current, total, _iso(now), job_id, owner),
    )
    conn.commit()
    return cursor.rowcount == 1


def complete_job(
    conn: sqlite3.Connection,
    job_id: str,
    owner: str,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> bool:
    stamp = _iso(now)
    cursor = conn.execute(
        "UPDATE scoring_jobs SET state = 'succeeded', lease_owner = NULL, "
        "lease_expires_at = NULL, next_attempt_at = NULL, stage = 'persisting', "
        "stage_current = NULL, stage_total = NULL, last_error_code = NULL, "
        "last_error_at = NULL, updated_at = ? "
        "WHERE job_id = ? AND state = 'running' AND lease_owner = ?",
        (stamp, job_id, owner),
    )
    if commit:
        conn.commit()
    return cursor.rowcount == 1


def complete_revision_jobs(
    conn: sqlite3.Connection,
    session_id: str,
    content_revision: str,
    *,
    now: datetime | None = None,
    commit: bool = True,
) -> int:
    """Reconcile explicit scoring success into the durable queue atomically.

    Callers must hold the installation-wide scoring egress lock. Therefore a
    ``running`` row for this revision cannot belong to a live peer; it is an
    orphan from a crashed process and can safely be completed by the explicit
    score that just succeeded.
    """

    stamp = _iso(now)
    cursor = conn.execute(
        "UPDATE scoring_jobs SET state = 'succeeded', next_attempt_at = NULL, "
        "lease_owner = NULL, lease_expires_at = NULL, stage = 'persisting', "
        "stage_current = NULL, stage_total = NULL, last_error_code = NULL, "
        "last_error_at = NULL, updated_at = ? "
        "WHERE session_id = ? AND content_revision = ? "
        "AND state IN ('pending', 'running', 'retry_wait', 'failed')",
        (stamp, session_id, content_revision),
    )
    if commit:
        conn.commit()
    return max(cursor.rowcount, 0)


def cancel_job(
    conn: sqlite3.Connection,
    job_id: str,
    owner: str,
    code: str,
    *,
    now: datetime | None = None,
) -> bool:
    if code not in JOB_ERROR_CODES:
        raise ValueError("invalid scoring job error code")
    stamp = _iso(now)
    cursor = conn.execute(
        "UPDATE scoring_jobs SET state = 'cancelled', lease_owner = NULL, "
        "lease_expires_at = NULL, next_attempt_at = NULL, stage = 'queued', "
        "stage_current = NULL, stage_total = NULL, last_error_code = ?, "
        "last_error_at = ?, updated_at = ? "
        "WHERE job_id = ? AND state = 'running' AND lease_owner = ?",
        (code, stamp, stamp, job_id, owner),
    )
    conn.commit()
    return cursor.rowcount == 1


def retry_job_failure(
    conn: sqlite3.Connection,
    job_id: str,
    owner: str,
    code: str,
    *,
    now: datetime | None = None,
) -> str | None:
    """Apply bounded per-session backoff; return the resulting state."""

    if code not in {"score_timeout", "score_failed", "transcript_unavailable"}:
        raise ValueError("invalid retryable scoring error code")
    row = conn.execute(
        "SELECT attempt_count FROM scoring_jobs "
        "WHERE job_id = ? AND state = 'running' AND lease_owner = ?",
        (job_id, owner),
    ).fetchone()
    if row is None:
        return None
    attempt = int(row["attempt_count"]) + 1
    clock = _utc(now)
    if attempt >= SCORING_MAX_SESSION_ATTEMPTS:
        state = "failed"
        next_attempt = None
    else:
        state = "retry_wait"
        delay = SCORING_SESSION_BACKOFF_SECONDS[attempt - 1]
        next_attempt = (clock + timedelta(seconds=delay)).isoformat()
    stamp = clock.isoformat()
    cursor = conn.execute(
        "UPDATE scoring_jobs SET state = ?, attempt_count = ?, next_attempt_at = ?, "
        "lease_owner = NULL, lease_expires_at = NULL, stage = 'queued', "
        "stage_current = NULL, stage_total = NULL, last_error_code = ?, "
        "last_error_at = ?, updated_at = ? "
        "WHERE job_id = ? AND state = 'running' AND lease_owner = ?",
        (state, attempt, next_attempt, code, stamp, stamp, job_id, owner),
    )
    conn.commit()
    return state if cursor.rowcount == 1 else None


def defer_job_for_backend(
    conn: sqlite3.Connection,
    job_id: str,
    owner: str,
    *,
    next_attempt_at: str | None,
    action_required: bool,
    now: datetime | None = None,
) -> bool:
    stamp = _iso(now)
    cursor = conn.execute(
        "UPDATE scoring_jobs SET state = 'retry_wait', next_attempt_at = ?, "
        "lease_owner = NULL, lease_expires_at = NULL, stage = 'queued', "
        "stage_current = NULL, stage_total = NULL, last_error_code = ?, "
        "last_error_at = ?, updated_at = ? "
        "WHERE job_id = ? AND state = 'running' AND lease_owner = ?",
        (
            next_attempt_at,
            "backend_action_required" if action_required else "backend_cooldown",
            stamp,
            stamp,
            job_id,
            owner,
        ),
    )
    conn.commit()
    return cursor.rowcount == 1


def get_backend_state(conn: sqlite3.Connection, backend: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM scoring_backend_state WHERE backend = ?", (backend,)
    ).fetchone()
    return dict(row) if row is not None else None


def clear_backend_state(
    conn: sqlite3.Connection, backend: str, *, now: datetime | None = None
) -> None:
    stamp = _iso(now)
    conn.execute(
        "INSERT INTO scoring_backend_state "
        "(backend, state, next_attempt_at, consecutive_failures, last_error_code, updated_at) "
        "VALUES (?, 'ready', NULL, 0, NULL, ?) "
        "ON CONFLICT(backend) DO UPDATE SET state = 'ready', next_attempt_at = NULL, "
        "consecutive_failures = 0, last_error_code = NULL, updated_at = excluded.updated_at",
        (backend, stamp),
    )
    conn.commit()


def record_backend_cooldown(
    conn: sqlite3.Connection,
    backend: str,
    code: str,
    *,
    now: datetime | None = None,
) -> str:
    if code not in {"backend_rate_limited", "backend_temporary"}:
        raise ValueError("invalid backend cooldown code")
    clock = _utc(now)
    current = get_backend_state(conn, backend)
    failures = int(current.get("consecutive_failures") or 0) + 1 if current else 1
    delay = SCORING_BACKEND_BACKOFF_SECONDS[
        min(failures - 1, len(SCORING_BACKEND_BACKOFF_SECONDS) - 1)
    ]
    retry_at = (clock + timedelta(seconds=delay)).isoformat()
    conn.execute(
        "INSERT INTO scoring_backend_state "
        "(backend, state, next_attempt_at, consecutive_failures, last_error_code, updated_at) "
        "VALUES (?, 'cooldown', ?, ?, ?, ?) "
        "ON CONFLICT(backend) DO UPDATE SET state = 'cooldown', "
        "next_attempt_at = excluded.next_attempt_at, "
        "consecutive_failures = excluded.consecutive_failures, "
        "last_error_code = excluded.last_error_code, updated_at = excluded.updated_at",
        (backend, retry_at, failures, code, clock.isoformat()),
    )
    conn.commit()
    return retry_at


def record_backend_action_required(
    conn: sqlite3.Connection,
    backend: str,
    code: str,
    *,
    now: datetime | None = None,
) -> None:
    if code not in ACTION_REQUIRED_CODES:
        raise ValueError("invalid backend action-required code")
    stamp = _iso(now)
    conn.execute(
        "INSERT INTO scoring_backend_state "
        "(backend, state, next_attempt_at, consecutive_failures, last_error_code, updated_at) "
        "VALUES (?, 'action_required', NULL, 0, ?, ?) "
        "ON CONFLICT(backend) DO UPDATE SET state = 'action_required', "
        "next_attempt_at = NULL, last_error_code = excluded.last_error_code, "
        "updated_at = excluded.updated_at",
        (backend, code, stamp),
    )
    conn.commit()


def backend_blocker(
    conn: sqlite3.Connection, backend: str, *, now: datetime | None = None
) -> dict[str, Any] | None:
    state = get_backend_state(conn, backend)
    if not state or state["state"] == "ready":
        return None
    if state["state"] == "cooldown":
        retry_at = state.get("next_attempt_at")
        if not retry_at or retry_at <= _iso(now):
            clear_backend_state(conn, backend, now=now)
            return None
    return state


def retry_failed_jobs(conn: sqlite3.Connection, *, now: datetime | None = None) -> int:
    stamp = _iso(now)
    if conn.in_transaction:
        raise RuntimeError("queue retry requires a connection without an active transaction")
    conn.execute("BEGIN IMMEDIATE")
    try:
        _cancel_noncurrent_or_inactive_jobs(conn, now=now)
        cursor = conn.execute(
            "UPDATE scoring_jobs SET state = 'pending', attempt_count = 0, "
            "next_attempt_at = NULL, lease_owner = NULL, lease_expires_at = NULL, "
            "stage = 'queued', stage_current = NULL, stage_total = NULL, "
            "last_error_code = NULL, last_error_at = NULL, updated_at = ? "
            "WHERE state = 'failed' AND EXISTS ("
            " SELECT 1 FROM sessions s WHERE s.session_id = scoring_jobs.session_id "
            " AND s.content_revision = scoring_jobs.content_revision "
            " AND COALESCE(s.checkpoint_active, 1) = 1"
            ")",
            (stamp,),
        )
        conn.commit()
        return max(cursor.rowcount, 0)
    except Exception:
        conn.rollback()
        raise


def queue_status(
    conn: sqlite3.Connection,
    *,
    backend: str | None,
    enabled: bool,
    backends: Sequence[str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    counts = {state: 0 for state in ("pending", "running", "retry_wait", "succeeded", "failed")}
    rows = conn.execute(
        "SELECT j.state, COUNT(*) AS count FROM scoring_jobs j "
        "JOIN sessions s ON s.session_id = j.session_id "
        " AND s.content_revision = j.content_revision "
        "WHERE j.state != 'cancelled' "
        "AND COALESCE(s.checkpoint_active, 1) = 1 GROUP BY j.state"
    ).fetchall()
    for row in rows:
        state = str(row["state"])
        if state in counts:
            counts[state] = int(row["count"])
    active = conn.execute(
        "SELECT job_id, stage, stage_current, stage_total FROM scoring_jobs j "
        "WHERE state = 'running' AND EXISTS ("
        " SELECT 1 FROM sessions s WHERE s.session_id = j.session_id "
        " AND s.content_revision = j.content_revision "
        " AND COALESCE(s.checkpoint_active, 1) = 1"
        ") ORDER BY updated_at ASC, job_id ASC LIMIT 1"
    ).fetchone()
    current = None
    if active is not None:
        current = {
            "job_id": str(active["job_id"]),
            "stage": str(active["stage"]),
            "progress_current": active["stage_current"],
            "progress_total": active["stage_total"],
        }
    retry_row = conn.execute(
        "SELECT MIN(next_attempt_at) AS next_retry_at FROM scoring_jobs j "
        "WHERE state = 'retry_wait' AND next_attempt_at IS NOT NULL AND EXISTS ("
        " SELECT 1 FROM sessions s WHERE s.session_id = j.session_id "
        " AND s.content_revision = j.content_revision "
        " AND COALESCE(s.checkpoint_active, 1) = 1"
        ")"
    ).fetchone()
    next_retry = retry_row["next_retry_at"] if retry_row is not None else None
    backend_names = list(dict.fromkeys(
        candidate for candidate in (backends or ([backend] if backend else []))
        if candidate
    ))
    clock = _iso(now)
    backend_states = [
        state
        for candidate in backend_names
        if (state := get_backend_state(conn, candidate)) is not None
    ]
    state_by_backend = {
        str(state["backend"]): state for state in backend_states
    }
    usable_backend = False
    future_cooldowns: list[dict[str, Any]] = []
    action_blockers: list[dict[str, Any]] = []
    for candidate in backend_names:
        state = state_by_backend.get(candidate)
        if state is None or state["state"] == "ready":
            usable_backend = True
            continue
        if state["state"] == "cooldown":
            retry_at = state.get("next_attempt_at")
            if not retry_at or retry_at <= clock:
                usable_backend = True
            else:
                future_cooldowns.append(state)
                if next_retry is None or retry_at < next_retry:
                    next_retry = retry_at
            continue
        action_blockers.append(state)

    if not enabled or backend is None:
        worker_state = "disabled"
    elif counts["running"]:
        worker_state = "running"
    elif usable_backend:
        worker_state = "idle"
    elif future_cooldowns:
        worker_state = "cooldown"
    elif action_blockers:
        worker_state = "action_required"
    else:
        worker_state = "idle"

    action_code = None
    if worker_state == "action_required":
        for state in action_blockers:
            if state.get("last_error_code") in ACTION_REQUIRED_CODES:
                action_code = state["last_error_code"]
                break
    return {
        "enabled": enabled,
        "backend": backend,
        "worker_state": worker_state,
        "counts": counts,
        "current": current,
        "next_retry_at": next_retry,
        "action_required_code": action_code,
    }


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM scoring_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row is not None else None


def classify_scoring_error(error: BaseException) -> tuple[str, str]:
    """Map an arbitrary backend exception to a fixed, content-free code.

    The first element is ``job``, ``cooldown`` or ``action_required``.  The
    raw message is inspected in memory only and must never be persisted.
    """

    message = str(error).lower()
    if "timed out" in message or "timeout" in message:
        return "job", "score_timeout"
    if any(marker in message for marker in (
        "transcript is unavailable",
        "transcript is unreadable",
        "transcript is invalid",
    )):
        return "job", "transcript_unavailable"
    if any(marker in message for marker in (
        "cli not found",
        "command not found",
        "not installed",
        "could not detect a supported scoring backend",
    )):
        return "action_required", "backend_missing"
    if any(marker in message for marker in (
        "not logged in",
        "not signed in",
        "unauthoriz",
        "authentication required",
        "forbidden",
        " 401",
        " 403",
    )):
        return "action_required", "backend_auth"
    if any(marker in message for marker in (
        "429",
        "rate limit",
        "too many requests",
        "quota",
        "out of credits",
        "usage limit",
        "limit reached",
    )):
        return "cooldown", "backend_rate_limited"
    if any(marker in message for marker in (
        "network is unreachable",
        "could not reach",
        "could not resolve",
        "name resolution",
        "temporary failure",
        "connection reset",
        "service unavailable",
    )):
        return "cooldown", "backend_temporary"
    if "unsupported backend" in message or "unsupported clawjournal_scorer_backend" in message:
        return "action_required", "backend_unavailable"
    return "job", "score_failed"
