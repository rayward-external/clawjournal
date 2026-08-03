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
``skill_rules`` state plus a run marker); a fresh install nudges nobody. The
printed counts reach agent context (and therefore the model provider when the
agent loads them), so they honor the same boundaries as the corpus itself:
hold-state gated via ``_release_blocked_ids`` and scoped to the confirmed
sources/projects — the nudge never advertises, even in aggregate, sessions the
suggested run could not select.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from ..workbench.index import (
    FAILURE_VALUE_SOURCE_SCOPE,
    SHAREABLE_HOLD_STATES,
    session_matches_excluded_projects,
)
from .select import _parse_start_time, _release_blocked_ids

# Never nudge within a week of the last skill run; past two weeks staleness
# alone is enough. In between, an event must justify the nudge: failure
# evidence accumulating, or an unusually large batch of new sessions.
NUDGE_MIN_DAYS = 7.0
NUDGE_STALE_DAYS = 14.0
NUDGE_MIN_NEW_SESSIONS = 5
NUDGE_BURST_SESSIONS = 25
NUDGE_FAILURE_SESSIONS = 3
NUDGE_COOLDOWN_DAYS = 3.0   # an emitted nudge suppresses repeats across sessions

# Rows examined per check. The SQL narrows to plausibly-eligible rows FIRST
# (source scope, shareable hold state, well-formed timestamp) so a pile of
# held/foreign rows can no longer consume the cap and hide real activity; the
# remaining precise filters run in Python. Sits far above every threshold, and
# a saturated count is reported as "N+".
_COUNT_CAP = 300

_STATE_TABLE = "skill_nudge_state"
_LAST_NUDGED_KEY = "last_nudged_at"
_LAST_RUN_KEY = "last_skill_run_at"
_HOOK_REQUESTED_KEY = "nudge_hook_requested_at"


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


def nudge_hook_requested(conn: sqlite3.Connection) -> bool:
    """True when the user explicitly enabled the nudge via ``--install-nudge``."""
    return _read_state_ts(conn, _HOOK_REQUESTED_KEY) is not None


def set_nudge_hook_requested(
    conn: sqlite3.Connection, requested: bool, *, now: datetime | None = None
) -> None:
    """Durable marker that the user explicitly owns the SessionStart hook.

    The hook file is shared with the auto-upload scheduler; this flag is what
    lets each feature's teardown know the other still needs it.
    """
    if requested:
        _write_state_ts(conn, _HOOK_REQUESTED_KEY, now or datetime.now(timezone.utc))
    else:
        try:
            conn.execute(
                f"DELETE FROM {_STATE_TABLE} WHERE key = ?", (_HOOK_REQUESTED_KEY,)
            )
        except sqlite3.Error:   # table never created -> nothing to clear
            pass
    conn.commit()


def nudge_hook_active() -> bool:
    """Fail-open(False) flag read for auto-upload teardown paths.

    Opens the existing index read-write like the hook does; any error means
    "not requested" so an unreadable flag can never block hook removal.
    """
    try:
        from ..auto_upload import _open_existing_hook_index

        conn = _open_existing_hook_index()
        if conn is None:
            return False
        try:
            return nudge_hook_requested(conn)
        finally:
            conn.close()
    except Exception:
        return False


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
    # The nudge is opt-in: the SessionStart hook file is shared with the
    # auto-upload scheduler, so hook presence alone is not consent. Only an
    # explicit `clawjournal skill --install-nudge` enables it, and
    # `--uninstall-nudge` disables it even while the hook stays installed for
    # uploads.
    if not nudge_hook_requested(conn):
        return DueStatus(False, "nudge-not-enabled")
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
    # run would use. The SQL is a cheap narrowing pass (the GLOB drops obvious
    # non-ISO strings like 'unknown' that compare greater than any anchor); the
    # precise instant check happens on the parsed timestamp below, bounding both
    # ends like select.py so malformed ISO-lookalikes ('9999-99-99garbage') and
    # future-dated rows can never count as new activity forever.
    sources, excluded = _scope_and_exclusions(conn)
    placeholders = ",".join("?" for _ in sources)
    hold_placeholders = ",".join("?" for _ in sorted(SHAREABLE_HOLD_STATES))
    # The hold-state prefilter is what keeps the cap honest (a pile of held rows
    # can no longer crowd out eligible ones); `_release_blocked_ids` below is
    # still authoritative for the rest of the gate. Rows whose embargo has since
    # expired are undercounted here — conservative, never over-reporting.
    rows = conn.execute(
        "SELECT session_id, project, source, start_time, "
        "ai_failure_value_score, ai_outcome_badge "
        "FROM sessions WHERE review_status != 'segmented' "
        f"AND source IN ({placeholders}) "
        f"AND hold_state IN ({hold_placeholders}) AND start_time > ? "
        "AND start_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
        f"ORDER BY start_time DESC LIMIT {_COUNT_CAP}",
        [*sources, *sorted(SHAREABLE_HOLD_STATES), anchor.isoformat()],
    ).fetchall()
    saturated = len(rows) >= _COUNT_CAP
    kept = []
    for r in rows:
        parsed = _parse_start_time(r["start_time"])
        if parsed is None or not (anchor < parsed <= now):
            continue
        if excluded and session_matches_excluded_projects(
                {"project": r["project"], "source": r["source"]}, excluded):
            continue
        kept.append(r)
    # Hold-state gate: the nudge line reaches agent context (and so the model
    # provider), so even aggregate counts must not derive from held/embargoed
    # sessions — mirror the corpus egress gate rather than counting sessions
    # selection could never use.
    if kept:
        blocked = _release_blocked_ids(conn, [r["session_id"] for r in kept], now=now)
        kept = [r for r in kept if r["session_id"] not in blocked]
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
    shown = f"{new_sessions}+" if saturated else str(new_sessions)
    message = (
        f"ClawJournal lessons refresher: {shown} new sessions since the "
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
