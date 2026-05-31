"""Select the week's failure-signal sessions to feed benchmark generation.

Reuses the failure-mode substrate already computed by scoring
(``ai_failure_value_score``, ``ai_failure_modes``, ``ai_learning_summary``, …),
which is now trustworthy thanks to the re-score-on-growth fix (mid-flight grades
no longer stick). This module does no LLM work — it just picks and ranks the
candidates a later deep-read pass will read.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..workbench.index import FAILURE_VALUE_SOURCE_SCOPE

DEFAULT_WINDOW_DAYS = 7
# Cap on how many sessions get deep-read, to bound generation cost. The rest are
# counted as ``dropped_for_cost`` so coverage is reported honestly.
DEFAULT_DEEPREAD_CAP = 15


@dataclass
class FailureCandidate:
    session_id: str
    project: str
    source: str
    failure_value_score: int | None
    failure_modes: list[str] = field(default_factory=list)
    failure_attribution: str | None = None
    recovery_labels: list[str] = field(default_factory=list)
    learning_summary: str | None = None
    score_reason: str | None = None
    summary: str | None = None
    title: str | None = None
    blob_path: str | None = None
    start_time: str | None = None
    # True when the trace carries a concrete, trace-backed failure signal (a
    # learning summary, a score reason, or labeled failure modes). Generators
    # must not invent a failure from a bare low score — un-evidenced candidates
    # become ``needs_review`` tasks rather than confident ones.
    has_trace_evidence: bool = False


@dataclass
class WeekSlice:
    window_start: str
    window_end: str
    candidates: list[FailureCandidate]
    total_candidates: int
    dropped_for_cost: int

    @property
    def session_ids(self) -> list[str]:
        return [c.session_id for c in self.candidates]


def _parse_json_list(raw: Any) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return [str(x) for x in value] if isinstance(value, list) else []


def select_week_failures(
    conn: sqlite3.Connection,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    cap: int = DEFAULT_DEEPREAD_CAP,
    now: datetime | None = None,
    sources: tuple[str, ...] | list[str] | None = FAILURE_VALUE_SOURCE_SCOPE,
) -> WeekSlice:
    """Return the window's failure-signal sessions, ranked, capped for cost.

    A session qualifies if it has ``ai_failure_value_score >= 3`` OR a non-empty
    ``ai_failure_modes`` list. Segmented parent rows are skipped (their content
    lives in the per-segment children). Ranked by failure value (desc), then
    recency, then capped to ``cap`` — the remainder reported as
    ``dropped_for_cost``.
    """
    clock = now or datetime.now(timezone.utc)
    window_start = (clock - timedelta(days=window_days)).isoformat()
    window_end = clock.isoformat()

    params: list[Any] = [window_start]
    sql = (
        "SELECT session_id, project, source, ai_failure_value_score, ai_failure_modes, "
        "ai_failure_attribution, ai_recovery_labels, ai_learning_summary, ai_score_reason, "
        "ai_summary, COALESCE(ai_display_title, display_title) AS title, blob_path, start_time "
        "FROM sessions WHERE start_time >= ? AND review_status != 'segmented' "
        "AND (ai_failure_value_score >= 3 OR (ai_failure_modes IS NOT NULL "
        "AND json_valid(ai_failure_modes) AND json_array_length(ai_failure_modes) > 0))"
    )
    src = [s for s in (sources or []) if s]
    if src:
        sql += f" AND source IN ({','.join('?' for _ in src)})"
        params.extend(src)
    # NULL failure_value sorts last under DESC in SQLite, so modes-only rows
    # rank below scored ones — intended.
    sql += " ORDER BY ai_failure_value_score DESC, start_time DESC"

    rows = conn.execute(sql, params).fetchall()
    candidates: list[FailureCandidate] = []
    for row in rows:
        modes = _parse_json_list(row["ai_failure_modes"])
        learning = row["ai_learning_summary"]
        reason = row["ai_score_reason"]
        has_evidence = bool((learning and learning.strip()) or (reason and reason.strip()) or modes)
        candidates.append(
            FailureCandidate(
                session_id=row["session_id"],
                project=row["project"],
                source=row["source"],
                failure_value_score=row["ai_failure_value_score"],
                failure_modes=modes,
                failure_attribution=row["ai_failure_attribution"],
                recovery_labels=_parse_json_list(row["ai_recovery_labels"]),
                learning_summary=learning,
                score_reason=reason,
                summary=row["ai_summary"],
                title=row["title"],
                blob_path=row["blob_path"],
                start_time=row["start_time"],
                has_trace_evidence=has_evidence,
            )
        )

    total = len(candidates)
    selected = candidates[:cap] if cap and cap > 0 else candidates
    return WeekSlice(
        window_start=window_start,
        window_end=window_end,
        candidates=selected,
        total_candidates=total,
        dropped_for_cost=max(0, total - len(selected)),
    )
