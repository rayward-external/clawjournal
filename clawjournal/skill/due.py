"""Distill-due nudge for the ``SessionStart`` hook (plan §16 CH-1).

Distillation is manual by design (the LLM egress must stay user-initiated), but
a manual-only cadence goes stale in practice. This module computes a *cheap,
bounded, fail-open* "is a lessons refresh due?" decision from the existing index
DB and, when due, emits ONE printed line from the agent hook — a Claude Code
``SessionStart`` hook's stdout is surfaced into the session, so the agent can
suggest ``clawjournal skill --preview`` to the user. Nothing is ever run
automatically: the nudge is text, the distill call and install remain manual
and confirmed.

Only users who have already run the skill pipeline are nudged (the anchor is
``skill_rules`` state plus a run marker); a fresh install nudges nobody. Session
counts here are an *activity metric* for a locally printed line — nothing leaves
the machine — so they deliberately skip the per-session release-gate pass the
egress paths run (a held session still counts as "new activity"; it just can
never feed the distill corpus itself). They DO mirror the confirmed
source/project scope, so the nudge never advertises sessions the suggested run
would not even select.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..workbench.index import (
    FAILURE_VALUE_SOURCE_SCOPE,
    session_matches_excluded_projects,
)

# Never nudge within a week of the last skill run; past two weeks staleness
# alone is enough. In between, an event must justify the nudge: failure
# evidence accumulating, or an unusually large batch of new sessions.
NUDGE_MIN_DAYS = 7.0
NUDGE_STALE_DAYS = 14.0
NUDGE_MIN_NEW_SESSIONS = 5
NUDGE_BURST_SESSIONS = 25
NUDGE_FAILURE_SESSIONS = 3
NUDGE_COOLDOWN_DAYS = 3.0   # an emitted nudge suppresses repeats across sessions

_COUNT_CAP = 1000           # counts saturate here; thresholds sit far below it

_STATE_TABLE = "skill_nudge_state"
_LAST_NUDGED_KEY = "last_nudged_at"
_LAST_RUN_KEY = "last_skill_run_at"


@dataclass(frozen=True)
class DueStatus:
    due: bool
    reason: str
    message: str = ""
    new_sessions: int = 0
    failure_sessions: int = 0
    days_since: float | None = None


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _read_state_ts(conn: sqlite3.Connection, key: str) -> datetime | None:
    try:
        row = conn.execute(
            f"SELECT value FROM {_STATE_TABLE} WHERE key = ?", (key,)
        ).fetchone()
    except sqlite3.Error:   # table absent until the first write — that's fine
        return None
    return _parse_ts(row[0] if row else None)


def _write_state_ts(conn: sqlite3.Connection, key: str, now: datetime) -> None:
    conn.execute(
        f"CREATE TABLE IF NOT EXISTS {_STATE_TABLE} (key TEXT PRIMARY KEY, value TEXT)"
    )
    conn.execute(
        f"INSERT INTO {_STATE_TABLE} (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, now.isoformat()),
    )


def record_skill_run(conn: sqlite3.Connection, *, now: datetime | None = None) -> None:
    """Record a conscious ``clawjournal skill`` run as nudge-anchor activity.

    A run that ends rule-less (empty distill, gate block) never touches
    ``skill_rules``, so without this the anchor would stay stale and the nudge
    would re-nag every cooldown about a pipeline the user just tried.
    """
    _write_state_ts(conn, _LAST_RUN_KEY, now or datetime.now(timezone.utc))
    conn.commit()


def _scope_and_exclusions(conn: sqlite3.Connection) -> tuple[list[str], list[str]]:
    """The user's confirmed source scope + excluded projects, fail-open to wide.

    The nudge must advertise activity the suggested ``clawjournal skill`` run
    can actually see: mirroring ``cli_skill._config_sources`` /
    ``_config_excluded_projects`` keeps the count from touting sessions the
    corpus scope would then drop.
    """
    sources = list(FAILURE_VALUE_SOURCE_SCOPE)
    excluded: list[str] = []
    try:
        from ..config import load_config, source_scope_sources

        cfg = load_config()
        scope = source_scope_sources(cfg.get("source"))
        if scope is not None:
            sources = list(scope)
        from ..workbench.index import get_effective_share_settings

        excluded = list(
            get_effective_share_settings(conn, cfg).get("excluded_projects") or []
        )
    except Exception:   # config problems must never break a session start
        pass
    return sources, excluded


def distill_due_on_connection(conn: sqlite3.Connection, now: datetime) -> DueStatus:
    """Decide due-ness on an open index connection. Cheap reads only.

    The anchor is the last skill activity — MAX over ``installed_at`` (real
    install) and ``last_seen_at`` (a ``--preview`` run bumps it via
    ``upsert_seen``) — so a user who consciously previewed recently is not
    nagged again for a full cycle.
    """
    try:
        row = conn.execute(
            "SELECT MAX(installed_at), MAX(last_seen_at) FROM skill_rules"
        ).fetchone()
    except sqlite3.Error:   # table never created -> the user never opted in
        return DueStatus(False, "never-distilled")
    anchors = [ts for ts in (_parse_ts(row[0]), _parse_ts(row[1])) if ts is not None]
    if not anchors:
        return DueStatus(False, "never-distilled")
    # A rule-less run (empty distill / gate block) writes only the run marker;
    # it still counts as conscious skill activity.
    last_run = _read_state_ts(conn, _LAST_RUN_KEY)
    if last_run is not None:
        anchors.append(last_run)
    anchor = max(anchors)
    days_since = max(0.0, (now - anchor).total_seconds() / 86400)
    if days_since < NUDGE_MIN_DAYS:
        return DueStatus(False, "fresh", days_since=days_since)

    last_nudged = _read_state_ts(conn, _LAST_NUDGED_KEY)
    if last_nudged is not None and (
        (now - last_nudged).total_seconds() / 86400 < NUDGE_COOLDOWN_DAYS
    ):
        return DueStatus(False, "cooldown", days_since=days_since)

    # Activity since the anchor, in the same source/project scope the suggested
    # run would use. start_time is an ISO string with per-source offsets; a
    # lexicographic compare can skew by up to ~14h, which is noise at nudge
    # granularity (thresholds are days, not hours). The GLOB guard drops
    # unparseable timestamps ('unknown', raw vendor stamps) that would compare
    # greater than any ISO anchor and count as new activity forever —
    # select.py excludes exactly these from the corpus.
    sources, excluded = _scope_and_exclusions(conn)
    placeholders = ",".join("?" for _ in sources)
    rows = conn.execute(
        "SELECT project, source, ai_failure_value_score, ai_outcome_badge "
        "FROM sessions WHERE review_status != 'segmented' "
        f"AND source IN ({placeholders}) AND start_time > ? "
        "AND start_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
        f"LIMIT {_COUNT_CAP}",
        [*sources, anchor.isoformat()],
    ).fetchall()
    kept = [
        r for r in rows
        if not excluded or not session_matches_excluded_projects(
            {"project": r["project"], "source": r["source"]}, excluded)
    ]
    new_sessions = len(kept)
    if new_sessions < NUDGE_MIN_NEW_SESSIONS:
        return DueStatus(False, "quiet", new_sessions=new_sessions, days_since=days_since)
    failure_sessions = sum(
        1 for r in kept
        if (r["ai_failure_value_score"] is not None and r["ai_failure_value_score"] >= 3)
        or (r["ai_outcome_badge"] or "") in ("failed", "abandoned")
    )

    if not (
        failure_sessions >= NUDGE_FAILURE_SESSIONS
        or new_sessions >= NUDGE_BURST_SESSIONS
        or days_since >= NUDGE_STALE_DAYS
    ):
        return DueStatus(False, "waiting", new_sessions=new_sessions,
                         failure_sessions=failure_sessions, days_since=days_since)

    fail_part = (f", {failure_sessions} with failure evidence"
                 if failure_sessions else "")
    message = (
        f"ClawJournal lessons refresher: {new_sessions} new sessions since the "
        f"last clawjournal-lessons refresh {int(days_since)}d ago{fail_part}. "
        "Suggest running `clawjournal skill --preview` to refresh the skill "
        "(the user reviews before install; nothing runs automatically)."
    )
    return DueStatus(True, "due", message=message, new_sessions=new_sessions,
                     failure_sessions=failure_sessions, days_since=days_since)


def emit_session_start_nudge(
    client: str,
    *,
    now: datetime | None = None,
    conn_factory: Callable[[], sqlite3.Connection | None] | None = None,
    printer: Callable[[str], Any] = print,
) -> bool:
    """Print the one-line nudge when due; record it so repeats cool down.

    Fail-open everywhere: a missing/busy DB, a locked write, or any parse error
    must never delay or break an agent session start. The nudge is recorded
    BEFORE printing so a write failure cannot spam a nudge every session.
    """
    del client   # same decision for every agent surface, kept for the hook seam
    clock = now or datetime.now(timezone.utc)
    try:
        if conn_factory is None:
            from ..auto_upload import _open_existing_hook_index
            conn_factory = _open_existing_hook_index
        conn = conn_factory()
        if conn is None:
            return False
        try:
            conn.execute("BEGIN IMMEDIATE")
            status = distill_due_on_connection(conn, clock)
            if status.due:
                _write_state_ts(conn, _LAST_NUDGED_KEY, clock)
                conn.commit()
            else:
                conn.rollback()
        except sqlite3.Error:
            conn.rollback()
            return False
        finally:
            conn.close()
    except Exception:
        return False
    if status.due:
        printer(status.message)
        return True
    return False
