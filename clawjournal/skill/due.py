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

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from ..workbench.index import (
    FAILURE_VALUE_SOURCE_SCOPE,
    SHAREABLE_HOLD_STATES,
    session_matches_excluded_projects,
)
from .select import _parse_start_time, _release_blocked_ids

# Never nudge within a week of the last skill run. After two weeks, sufficient
# new activity is enough; in between, failure evidence or an unusually large
# session burst must also justify the nudge.
NUDGE_MIN_DAYS = 7.0
NUDGE_STALE_DAYS = 14.0
NUDGE_MIN_NEW_SESSIONS = 5
NUDGE_BURST_SESSIONS = 25
NUDGE_FAILURE_SESSIONS = 3
NUDGE_COOLDOWN_DAYS = 3.0   # an emitted nudge suppresses repeats across sessions

# Scanning is paged and bounded. SQL removes source/project/hold-state rows
# before LIMIT; the smaller set of Python-only legacy filters is checked page by
# page. The scan stops early once the count settles every threshold; a count
# that is a floor rather than a total is reported as "N+".
_PAGE_SIZE = 100
_SCAN_BUDGET = 2000

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


def nudge_hook_ownership(conn: sqlite3.Connection) -> bool | None:
    """Read hook ownership strictly: ``None`` means the DB answer is unknown."""
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_STATE_TABLE,),
        ).fetchone()
        if table is None:
            return False
        row = conn.execute(
            f"SELECT value FROM {_STATE_TABLE} WHERE key = ?",
            (_HOOK_REQUESTED_KEY,),
        ).fetchone()
        if row is None:
            return False
        return True if _parse_ts(row[0]) is not None else None
    except sqlite3.Error:
        return None


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
        # An absent table means there is no marker to clear.  Every other
        # SQLite failure is material: swallowing a failed DELETE would make the
        # CLI claim the nudge was disabled while the durable opt-in remained.
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (_STATE_TABLE,),
        ).fetchone()
        if table is not None:
            conn.execute(
                f"DELETE FROM {_STATE_TABLE} WHERE key = ?", (_HOOK_REQUESTED_KEY,)
            )
    conn.commit()


def _scope_and_exclusions(
    conn: sqlite3.Connection,
) -> tuple[list[str], list[str], str | None]:
    """Read the exact confirmed scope without warnings or implicit migration.

    The nudge must advertise activity the suggested ``clawjournal skill`` run
    can actually see: mirroring ``cli_skill._config_sources`` /
    ``_config_excluded_projects`` keeps the count from touting sessions the
    corpus scope would then drop.  Unlike the interactive CLI, a SessionStart
    hook must not call ``load_config``: its warning includes the config path and
    becomes agent context on hosts that surface hook stderr.  Any unreadable or
    unconfirmed scope therefore fails closed to no nudge.
    """
    try:
        from ..config import CONFIG_FILE, DEFAULT_CONFIG, source_scope_sources

        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return [], [], "scope-unavailable"
        cfg = {**DEFAULT_CONFIG, **raw}
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return [], [], "scope-unavailable"

    if cfg.get("projects_confirmed") is not True:
        return [], [], "scope-unconfirmed"
    source = cfg.get("source")
    excluded_config = cfg.get("excluded_projects", [])
    if (
        (source is not None and not isinstance(source, str))
        or not isinstance(excluded_config, list)
        or any(not isinstance(value, str) for value in excluded_config)
    ):
        return [], [], "scope-unavailable"

    try:
        scope = source_scope_sources(source)
        sources = (
            list(scope) if scope is not None else list(FAILURE_VALUE_SOURCE_SCOPE)
        )
        from ..workbench.index import get_effective_share_settings

        excluded = list(
            get_effective_share_settings(conn, cfg).get("excluded_projects") or []
        )
    except Exception:
        return [], [], "scope-unavailable"
    return sources, excluded, None


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
    # A rule-less run (empty distill / gate block) writes only the run marker;
    # it still counts as conscious skill activity, including when it was the
    # FIRST run and ``skill_rules`` has no row (or no table) yet.
    last_run = _read_state_ts(conn, _LAST_RUN_KEY)
    anchors = [last_run] if last_run is not None else []
    try:
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'skill_rules'"
        ).fetchone()
        if table is not None:
            row = conn.execute(
                "WITH timestamps(value) AS ("
                "  SELECT installed_at FROM skill_rules WHERE installed_at IS NOT NULL "
                "  UNION ALL "
                "  SELECT last_seen_at FROM skill_rules WHERE last_seen_at IS NOT NULL"
                "), latest(value) AS ("
                "  SELECT value FROM timestamps WHERE julianday(value) IS NOT NULL "
                "  ORDER BY julianday(value) DESC LIMIT 1"
                ") "
                "SELECT EXISTS("
                "  SELECT 1 FROM timestamps WHERE julianday(value) IS NULL"
                ") AS invalid, latest.value FROM (SELECT 1) LEFT JOIN latest ON 1 = 1"
            ).fetchone()
            if row[0]:
                # A malformed timestamp might represent more recent activity.
                # Never choose an older anchor and nudge early merely because
                # corrupted state cannot be ordered.
                return DueStatus(False, "skill-state-unavailable")
            if row[1]:
                parsed = _parse_ts(row[1])
                if parsed is None:
                    return DueStatus(False, "skill-state-unavailable")
                anchors.append(parsed)
    except sqlite3.Error:
        return DueStatus(False, "skill-state-unavailable")
    if not anchors:
        return DueStatus(False, "never-distilled")
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
    sources, excluded, scope_error = _scope_and_exclusions(conn)
    if scope_error is not None:
        return DueStatus(False, scope_error, days_since=days_since)
    placeholders = ",".join("?" for _ in sources)
    # The FULL hold gate is expressible in SQL — `release_gate_blockers` decides
    # purely on `effective_hold_state`, i.e. shareable states plus an embargo
    # whose `embargo_until` has passed. Encoding it here means no held or
    # actively-embargoed row consumes the scan budget at all (they used to, and
    # 2000 of them starved six real sessions). `_release_blocked_ids` still runs
    # on the survivors as the authoritative check.
    hold_states = sorted(SHAREABLE_HOLD_STATES)
    hold_placeholders = ",".join("?" for _ in hold_states)
    hold_clause = (
        f"AND (hold_state IN ({hold_placeholders}) OR hold_state IS NULL "
        "OR (hold_state = 'embargoed' AND embargo_until IS NOT NULL "
        "AND julianday(embargo_until) IS NOT NULL "
        "AND julianday(embargo_until) <= julianday(?))) "
    )
    exclusion_clause = ""
    exclusion_params: list[str] = []
    if excluded:
        ex_placeholders = ",".join("?" for _ in excluded)
        exclusion_clause = (
            f" AND project NOT IN ({ex_placeholders})"
            f" AND (source || ':' || project) NOT IN ({ex_placeholders})"
        )
        exclusion_params = [*excluded, *excluded]
    # Lexical time bounds are WIDENED by 2 days (> the ~14h max UTC-offset skew)
    # exactly as select.py does: a raw string compare on mixed-offset timestamps
    # would otherwise drop genuinely in-window rows (a -12:00 session read as
    # out of range). The precise instant check happens on the parsed value.
    lex_lo = (anchor - timedelta(days=2)).isoformat()
    lex_hi = (now + timedelta(days=2)).isoformat()
    sql = (
        "SELECT session_id, project, source, start_time, "
        "ai_failure_value_score, ai_outcome_badge "
        "FROM sessions WHERE review_status != 'segmented' "
        f"AND source IN ({placeholders}) "
        f"{hold_clause}"
        "AND start_time > ? AND start_time <= ? "
        "AND start_time GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*' "
        f"{exclusion_clause}"
        f"ORDER BY start_time DESC LIMIT {_PAGE_SIZE} OFFSET ?"
    )
    base_params = [*sources, *hold_states, now.isoformat(), lex_lo, lex_hi,
                   *exclusion_params]
    # Page rather than cap: the filters SQL cannot express (precise timestamp
    # parse and legacy claude: project forms) would otherwise
    # run after a single LIMIT and let a pile of ineligible rows hide real
    # activity behind the first page. The remaining work is bounded by
    # _SCAN_BUDGET rows and stops as soon as the count settles every threshold.
    kept: list[Any] = []
    scanned = 0
    budget_exhausted = False
    while scanned < _SCAN_BUDGET and len(kept) < NUDGE_BURST_SESSIONS:
        page = conn.execute(sql, [*base_params, scanned]).fetchall()
        if not page:
            break
        scanned += len(page)
        page_kept = []
        for r in page:
            parsed = _parse_start_time(r["start_time"])
            if parsed is None or not (anchor < parsed <= now):
                continue
            if excluded and session_matches_excluded_projects(
                    {"project": r["project"], "source": r["source"]}, excluded):
                continue
            page_kept.append(r)
        # Hold-state gate: the nudge line reaches agent context (and so the
        # model provider), so even aggregate counts must not derive from
        # held/embargoed sessions — mirror the corpus egress gate rather than
        # counting sessions selection could never use. Applied per page so an
        # active embargo cannot occupy the budget either.
        if page_kept:
            blocked = _release_blocked_ids(
                conn, [r["session_id"] for r in page_kept], now=now)
            page_kept = [r for r in page_kept if r["session_id"] not in blocked]
        kept.extend(page_kept)
        if len(page) < _PAGE_SIZE:
            break   # exhausted the matching rows
    else:
        budget_exhausted = scanned >= _SCAN_BUDGET
    # The count is a FLOOR whenever scanning stopped early — either the budget
    # ran out, or enough rows accumulated to settle every threshold (which can
    # also happen on a short final page, so this must not be tied to the loop
    # exit path).
    saturated = budget_exhausted or len(kept) >= NUDGE_BURST_SESSIONS
    if len(kept) > NUDGE_BURST_SESSIONS:
        kept = kept[:NUDGE_BURST_SESSIONS]
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
