"""Select skill candidates from the user's own *scored* sessions (Mode A).

Two pools, all pure SQL/Python (no LLM):
  - **avoid** = recurring failure modes with real impact (the ``ai_failure_modes``
    12-value enum), evidenced (a learning summary / score reason / labeled mode);
  - **do** = strong successes (``ai_outcome_badge`` resolved/trivial, high quality,
    clean recovery) and recovered failures (a failure mode present but
    ``self_recovered``/``user_corrected_recovery``) — the best "do this instead".

We carry a ``mode_recurrence`` count so the distill step can rank by recurrence,
and gate every candidate through the hold-state egress check (only
``SHAREABLE_HOLD_STATES`` may feed an AI call). Note: there is no ``resolution``
column — it is derived from ``ai_outcome_badge`` via ``_OUTCOME_NORMALIZE_SQL``.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..benchmark.select import _parse_json_list
from ..workbench.index import (
    FAILURE_VALUE_SOURCE_SCOPE,
    release_gate_blockers,
    session_matches_excluded_projects,
)

DEFAULT_WINDOW_DAYS = 7
DEFAULT_POOL_CAP = 5           # Mode A hard cap on candidates/rules reviewed per run
_CLEAN_RECOVERY = {"self_recovered", "user_corrected_recovery"}
_BAD_RECOVERY = {"unrecovered", "blocked"}
_RELEASE_GATE_CHUNK_SIZE = 500


@dataclass
class SkillCandidate:
    session_id: str
    project: str
    source: str
    kind: str                       # "avoid" | "do"
    failure_modes: list[str] = field(default_factory=list)
    recovery_labels: list[str] = field(default_factory=list)
    resolution: str | None = None   # derived (resolved/trivial/...)
    failure_value: int | None = None
    quality: int | None = None
    learning_summary: str | None = None
    score_reason: str | None = None
    title: str | None = None
    start_time: str | None = None
    support_count: int = 1
    impact: float = 0.0
    recency: float = 0.0
    rank_score: float = 0.0


@dataclass
class SkillCorpus:
    window_start: str
    window_end: str
    failures: list[SkillCandidate] = field(default_factory=list)
    successes: list[SkillCandidate] = field(default_factory=list)
    mode_recurrence: dict[str, int] = field(default_factory=dict)
    total_failures: int = 0
    total_successes: int = 0
    eligible_scored: int = 0   # scored sessions in the window (rate denominator)

    def mode_rates(self) -> dict[str, float]:
        """Per-failure-mode incidence rate over eligible scored sessions."""
        if self.eligible_scored <= 0:
            return {}
        return {m: n / self.eligible_scored for m, n in self.mode_recurrence.items()}

    @property
    def candidates(self) -> list[SkillCandidate]:
        return self.failures + self.successes

    @property
    def session_ids(self) -> list[str]:
        return [c.session_id for c in self.candidates]

    def is_empty(self) -> bool:
        return not self.failures and not self.successes


def _has_evidence(learning: str | None, reason: str | None, modes: list[str]) -> bool:
    return bool((learning and learning.strip()) or (reason and reason.strip()) or modes)


_FRACTION_RE = re.compile(r"\.(\d+)")  # fractional seconds only ('.' appears nowhere else in ISO)


def _pad_fraction(m: re.Match) -> str:
    # Python 3.10's fromisoformat accepts ONLY 3 or 6 fractional digits; normalize any
    # count (1/2/4/5/7+) to exactly 6 so it always parses.
    return "." + m.group(1)[:6].ljust(6, "0")


def _parse_start_time(value: str | None) -> datetime | None:
    if not value:
        return None
    text = _FRACTION_RE.sub(_pad_fraction, str(value).strip().replace("Z", "+00:00"))
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _recency_weight(start_time: str | None, *, now: datetime) -> float:
    parsed = _parse_start_time(start_time)
    if parsed is None:
        return 0.01
    age_days = max(0.0, (now - parsed).total_seconds() / 86400)
    return 1.0 / (1.0 + age_days)


def _candidate_rank(
    *,
    support: int,
    impact: float,
    recency: float,
) -> float:
    return max(1, support) * max(0.1, impact) * max(0.01, recency)


def _release_blocked_ids(
    conn: sqlite3.Connection,
    session_ids: list[str],
    *,
    now: datetime,
) -> set[str]:
    """Return session ids that cannot feed the AI-bound skill corpus."""
    blocked: set[str] = set()
    deduped = list(dict.fromkeys(sid for sid in session_ids if sid))
    for i in range(0, len(deduped), _RELEASE_GATE_CHUNK_SIZE):
        chunk = deduped[i:i + _RELEASE_GATE_CHUNK_SIZE]
        blocked.update(b["session_id"] for b in release_gate_blockers(conn, chunk, now=now))
    return blocked


def select_skill_candidates(
    conn: sqlite3.Connection,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    sources: tuple[str, ...] | list[str] | None = FAILURE_VALUE_SOURCE_SCOPE,
    excluded_projects: list[str] | None = None,
    pool_cap: int = DEFAULT_POOL_CAP,
) -> SkillCorpus:
    """Return the window's avoid + do candidates, hold-state-gated, capped.

    ``excluded_projects`` mirrors the export/share egress gate: a session whose
    project the user has ``--exclude``d never becomes a candidate and is never
    counted, so its content is not sent to the distill model or installed.
    """
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:  # _parse_start_time is always aware; don't crash on a naive now
        clock = clock.replace(tzinfo=timezone.utc)
    window_start_dt = clock - timedelta(days=window_days)
    window_start = window_start_dt.isoformat()
    window_end = clock.isoformat()
    # start_time is stored as an ISO string with a source-dependent offset, so a raw
    # `start_time >= window_start` lexicographic compare mis-filters mixed offsets. Use
    # a widened string prefilter (2 days > the 14h max offset skew) that can't exclude an
    # in-window row, then filter precisely on the parsed instant in Python.
    window_floor = (window_start_dt - timedelta(days=2)).isoformat()
    src = [s for s in (sources or []) if s]
    src_clause = f" AND source IN ({','.join('?' for _ in src)})" if src else ""

    def _in_window(start_time: str | None) -> bool:
        parsed = _parse_start_time(start_time)
        if parsed is not None:
            return parsed >= window_start_dt
        return bool(start_time) and str(start_time) >= window_start  # unparseable: prior behavior

    def _keep(row: Any) -> bool:
        if not _in_window(row["start_time"]):
            return False
        return not session_matches_excluded_projects(
            {"project": row["project"], "source": row["source"]}, excluded_projects)

    cols = (
        "session_id, project, source, ai_failure_value_score, ai_quality_score, "
        "ai_failure_modes, ai_recovery_labels, ai_outcome_badge, "
        "ai_learning_summary, ai_score_reason, "
        "COALESCE(ai_display_title, display_title) AS title, start_time"
    )
    base = f"FROM sessions WHERE start_time >= ? AND review_status != 'segmented'{src_clause}"

    # ---- failures pool (avoid) -------------------------------------------
    fail_sql = (
        f"SELECT {cols} {base} AND (ai_failure_value_score >= 3 "
        "OR ai_outcome_badge IN ('failed','abandoned')) "
        "ORDER BY ai_failure_value_score DESC, start_time DESC"
    )
    fail_rows = [r for r in conn.execute(fail_sql, [window_floor, *src]).fetchall() if _keep(r)]
    succ_sql = (
        f"SELECT {cols} {base} AND ("
        "(ai_outcome_badge IN ('resolved','trivial') AND ai_quality_score >= 4) "
        "OR (ai_failure_modes IS NOT NULL AND json_valid(ai_failure_modes) "
        "AND json_array_length(ai_failure_modes) > 0 AND ai_recovery_labels IS NOT NULL "
        "AND json_valid(ai_recovery_labels) "
        "AND EXISTS (SELECT 1 FROM json_each(ai_recovery_labels) "
        "            WHERE value IN ('self_recovered','user_corrected_recovery')))"
        ") ORDER BY ai_quality_score DESC, start_time DESC"
    )
    succ_rows = [r for r in conn.execute(succ_sql, [window_floor, *src]).fetchall() if _keep(r)]

    # eligible = the rate denominator; a SUPERSET of every session that can feed
    # mode_counter (the numerator), else a rate can exceed 100%. A candidate qualifies
    # by score, by outcome badge, or by failure_modes+recovery — so count any judge verdict.
    # Excluded projects are dropped here too (they're not candidates).
    eligible_sql = (
        f"SELECT session_id, start_time, project, source {base} AND (ai_failure_value_score IS NOT NULL "
        "OR ai_quality_score IS NOT NULL OR ai_outcome_badge IS NOT NULL "
        "OR ai_failure_modes IS NOT NULL)"
    )
    eligible_ids = [r["session_id"] for r in conn.execute(eligible_sql, [window_floor, *src]).fetchall()
                    if _keep(r)]

    candidate_ids = [row["session_id"] for row in list(fail_rows) + list(succ_rows)]
    # One hold-state gate pass over the union (candidates are a subset of eligible).
    blocked_ids = _release_blocked_ids(conn, candidate_ids + eligible_ids, now=clock)

    mode_counter: Counter[str] = Counter()
    recovery_counter: Counter[str] = Counter()
    outcome_counter: Counter[str] = Counter()
    _counted: set[str] = set()  # count each session once (a session can match both queries)
    for row in list(fail_rows) + list(succ_rows):
        sid = row["session_id"]
        if sid in blocked_ids or sid in _counted:
            continue
        _counted.add(sid)
        mode_counter.update(_parse_json_list(row["ai_failure_modes"]))
        recovery_counter.update(_parse_json_list(row["ai_recovery_labels"]))
        if row["ai_outcome_badge"]:
            outcome_counter.update([row["ai_outcome_badge"]])

    failures: list[SkillCandidate] = []
    failure_ids: set[str] = set()  # a session lands in exactly one pool (avoid wins)
    for row in fail_rows:
        if row["session_id"] in blocked_ids:
            continue
        modes = _parse_json_list(row["ai_failure_modes"])
        recovery = _parse_json_list(row["ai_recovery_labels"])
        if _CLEAN_RECOVERY & set(recovery):
            continue  # cleanly recovered -> teach it as a "do", not an "avoid"
        if not _has_evidence(row["ai_learning_summary"], row["ai_score_reason"], modes):
            continue  # never invent a confident rule from a bare low score
        support = max([mode_counter[m] for m in modes] or [1])
        impact = float(row["ai_failure_value_score"] or 3)
        recency = _recency_weight(row["start_time"], now=clock)
        failures.append(SkillCandidate(
            session_id=row["session_id"], project=row["project"], source=row["source"],
            kind="avoid", failure_modes=modes, recovery_labels=recovery,
            resolution=row["ai_outcome_badge"], failure_value=row["ai_failure_value_score"],
            quality=row["ai_quality_score"], learning_summary=row["ai_learning_summary"],
            score_reason=row["ai_score_reason"],
            title=row["title"], start_time=row["start_time"],
            support_count=support, impact=impact, recency=recency,
            rank_score=_candidate_rank(support=support, impact=impact, recency=recency),
        ))
        failure_ids.add(row["session_id"])

    # ---- successes / recoveries pool (do) --------------------------------
    successes: list[SkillCandidate] = []
    for row in succ_rows:
        if row["session_id"] in blocked_ids or row["session_id"] in failure_ids:
            continue  # already an "avoid" -> never double-book the same session
        recovery = _parse_json_list(row["ai_recovery_labels"])
        if _BAD_RECOVERY & set(recovery):
            continue  # not a clean "what worked"
        modes = _parse_json_list(row["ai_failure_modes"])
        # require teachable substance: a learning summary or a real recovery path
        if not (row["ai_learning_summary"] and row["ai_learning_summary"].strip()) and not (
            _CLEAN_RECOVERY & set(recovery)
        ):
            continue
        if modes:
            support = max([mode_counter[m] for m in modes] or [1])
        elif recovery:
            support = max([recovery_counter[r] for r in recovery] or [1])
        else:
            support = outcome_counter[row["ai_outcome_badge"]] or 1
        impact = float(row["ai_quality_score"] or 3)
        recency = _recency_weight(row["start_time"], now=clock)
        successes.append(SkillCandidate(
            session_id=row["session_id"], project=row["project"], source=row["source"],
            kind="do", failure_modes=modes, recovery_labels=recovery,
            resolution=row["ai_outcome_badge"], failure_value=row["ai_failure_value_score"],
            quality=row["ai_quality_score"], learning_summary=row["ai_learning_summary"],
            score_reason=row["ai_score_reason"],
            title=row["title"], start_time=row["start_time"],
            support_count=support, impact=impact, recency=recency,
            rank_score=_candidate_rank(support=support, impact=impact, recency=recency),
        ))

    # eligible_ids computed above; reuse the single blocked_ids pass (no second gate query).
    eligible_scored = len([sid for sid in eligible_ids if sid not in blocked_ids])

    ranked = sorted(
        failures + successes,
        key=lambda c: (c.rank_score, c.support_count, c.impact, c.start_time or ""),
        reverse=True,
    )
    selected = ranked[:pool_cap] if pool_cap and pool_cap > 0 else ranked
    selected_failures = [c for c in selected if c.kind == "avoid"]
    selected_successes = [c for c in selected if c.kind == "do"]

    return SkillCorpus(
        window_start=window_start, window_end=window_end,
        failures=selected_failures, successes=selected_successes,
        mode_recurrence=dict(mode_counter.most_common()),
        total_failures=len(failures), total_successes=len(successes),
        eligible_scored=eligible_scored,
    )
