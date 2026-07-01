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

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from ..benchmark.select import _parse_json_list
from ..workbench.index import (
    FAILURE_VALUE_SOURCE_SCOPE,
    release_gate_blockers,
)

DEFAULT_WINDOW_DAYS = 7
DEFAULT_POOL_CAP = 16          # representative sessions kept per pool for the prompt
_CLEAN_RECOVERY = {"self_recovered", "user_corrected_recovery"}
_BAD_RECOVERY = {"unrecovered", "blocked"}


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
    summary: str | None = None
    title: str | None = None
    start_time: str | None = None


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


def select_skill_candidates(
    conn: sqlite3.Connection,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    now: datetime | None = None,
    sources: tuple[str, ...] | list[str] | None = FAILURE_VALUE_SOURCE_SCOPE,
    pool_cap: int = DEFAULT_POOL_CAP,
) -> SkillCorpus:
    """Return the window's avoid + do candidates, hold-state-gated, capped."""
    clock = now or datetime.now(timezone.utc)
    window_start = (clock - timedelta(days=window_days)).isoformat()
    window_end = clock.isoformat()
    src = [s for s in (sources or []) if s]
    src_clause = f" AND source IN ({','.join('?' for _ in src)})" if src else ""

    cols = (
        "session_id, project, source, ai_failure_value_score, ai_quality_score, "
        "ai_failure_modes, ai_recovery_labels, ai_outcome_badge, "
        "ai_learning_summary, ai_score_reason, ai_summary, "
        "COALESCE(ai_display_title, display_title) AS title, start_time"
    )
    base = f"FROM sessions WHERE start_time >= ? AND review_status != 'segmented'{src_clause}"

    # ---- failures pool (avoid) -------------------------------------------
    fail_sql = (
        f"SELECT {cols} {base} AND (ai_failure_value_score >= 3 OR "
        "(ai_failure_modes IS NOT NULL AND json_valid(ai_failure_modes) "
        "AND json_array_length(ai_failure_modes) > 0)) "
        "ORDER BY ai_failure_value_score DESC, start_time DESC"
    )
    fail_rows = conn.execute(fail_sql, [window_start, *src]).fetchall()

    mode_counter: Counter[str] = Counter()
    failures: list[SkillCandidate] = []
    for row in fail_rows:
        modes = _parse_json_list(row["ai_failure_modes"])
        mode_counter.update(modes)
        recovery = _parse_json_list(row["ai_recovery_labels"])
        if _CLEAN_RECOVERY & set(recovery):
            continue  # cleanly recovered -> teach it as a "do", not an "avoid"
        if not _has_evidence(row["ai_learning_summary"], row["ai_score_reason"], modes):
            continue  # never invent a confident rule from a bare low score
        failures.append(SkillCandidate(
            session_id=row["session_id"], project=row["project"], source=row["source"],
            kind="avoid", failure_modes=modes, recovery_labels=recovery,
            resolution=row["ai_outcome_badge"], failure_value=row["ai_failure_value_score"],
            quality=row["ai_quality_score"], learning_summary=row["ai_learning_summary"],
            score_reason=row["ai_score_reason"], summary=row["ai_summary"],
            title=row["title"], start_time=row["start_time"],
        ))

    # ---- successes / recoveries pool (do) --------------------------------
    succ_sql = (
        f"SELECT {cols} {base} AND ("
        "(ai_outcome_badge IN ('resolved','trivial') AND ai_quality_score >= 4) "
        "OR (ai_failure_modes IS NOT NULL AND json_valid(ai_failure_modes) "
        "AND json_array_length(ai_failure_modes) > 0 AND ai_recovery_labels IS NOT NULL "
        "AND EXISTS (SELECT 1 FROM json_each(COALESCE(ai_recovery_labels,'[]')) "
        "            WHERE value IN ('self_recovered','user_corrected_recovery')))"
        ") ORDER BY ai_quality_score DESC, start_time DESC"
    )
    succ_rows = conn.execute(succ_sql, [window_start, *src]).fetchall()
    successes: list[SkillCandidate] = []
    for row in succ_rows:
        recovery = _parse_json_list(row["ai_recovery_labels"])
        if _BAD_RECOVERY & set(recovery):
            continue  # not a clean "what worked"
        modes = _parse_json_list(row["ai_failure_modes"])
        # require teachable substance: a learning summary or a real recovery path
        if not (row["ai_learning_summary"] and row["ai_learning_summary"].strip()) and not (
            _CLEAN_RECOVERY & set(recovery)
        ):
            continue
        successes.append(SkillCandidate(
            session_id=row["session_id"], project=row["project"], source=row["source"],
            kind="do", failure_modes=modes, recovery_labels=recovery,
            resolution=row["ai_outcome_badge"], failure_value=row["ai_failure_value_score"],
            quality=row["ai_quality_score"], learning_summary=row["ai_learning_summary"],
            score_reason=row["ai_score_reason"], summary=row["ai_summary"],
            title=row["title"], start_time=row["start_time"],
        ))

    # ---- hold-state egress gate (only shareable sessions feed an AI call) -
    all_ids = [c.session_id for c in failures + successes]
    if all_ids:
        blocked = {b["session_id"] for b in release_gate_blockers(conn, all_ids, now=clock)}
        if blocked:
            failures = [c for c in failures if c.session_id not in blocked]
            successes = [c for c in successes if c.session_id not in blocked]

    eligible_scored = conn.execute(
        f"SELECT COUNT(*) FROM sessions WHERE start_time >= ? AND review_status != 'segmented'"
        f"{src_clause} AND (ai_failure_value_score IS NOT NULL OR ai_quality_score IS NOT NULL)",
        [window_start, *src],
    ).fetchone()[0]

    return SkillCorpus(
        window_start=window_start, window_end=window_end,
        failures=failures[:pool_cap], successes=successes[:pool_cap],
        mode_recurrence=dict(mode_counter.most_common()),
        total_failures=len(failures), total_successes=len(successes),
        eligible_scored=int(eligible_scored or 0),
    )
