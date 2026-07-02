"""Minimal durable state for Mode A skills (§9).

A ``skill_rules`` table keyed by a stable ``fingerprint`` (kind + normalized
guidance). Tracks approval/rejection/install timestamps so weekly runs can
**merge** into the existing set (replace the weakest) and **never re-propose a
rejected fingerprint** unless its guidance materially changes. This is the
"minimal durable state now" the plan requires — full lifecycle (mute/pin/version)
stays deferred.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone

from .schema import SkillRule

_ACTIVE = ("proposed", "kept")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fingerprint(rule: SkillRule) -> str:
    """Stable id from kind + normalized guidance (whitespace/case-insensitive)."""
    norm = re.sub(r"\s+", " ", f"{rule.kind}|{rule.guidance}".strip().lower())
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def ensure_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """CREATE TABLE IF NOT EXISTS skill_rules (
            fingerprint  TEXT PRIMARY KEY,
            kind         TEXT NOT NULL,
            title        TEXT,
            trigger      TEXT,
            guidance     TEXT NOT NULL,
            why          TEXT,
            taxonomy     TEXT,
            support      INTEGER DEFAULT 0,
            evidence_json TEXT,
            state        TEXT DEFAULT 'proposed',
            created_at   TEXT,
            approved_at  TEXT,
            rejected_at  TEXT,
            installed_at TEXT,
            last_seen_at TEXT
        )"""
    )
    # migrate pre-title tables (the skill_rules table is new; no historical gate)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(skill_rules)")}
    if "title" not in cols:
        conn.execute("ALTER TABLE skill_rules ADD COLUMN title TEXT")
    conn.commit()


def _row_to_rule(row: sqlite3.Row) -> SkillRule:
    try:
        ev = json.loads(row["evidence_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        ev = []
    return SkillRule(
        kind=row["kind"], trigger=row["trigger"] or "", guidance=row["guidance"],
        why=row["why"] or "", title=(row["title"] or ""),
        evidence_session_ids=[str(x) for x in ev],
        taxonomy=row["taxonomy"] or "", support=int(row["support"] or 0),
        last_seen=(row["last_seen_at"] or ""),
    )


def rejected_fingerprints(conn: sqlite3.Connection) -> set[str]:
    ensure_table(conn)
    return {r[0] for r in conn.execute("SELECT fingerprint FROM skill_rules WHERE state = 'rejected'")}


def load_kept(conn: sqlite3.Connection) -> list[SkillRule]:
    """Currently active (proposed/kept) rules, most-supported first."""
    ensure_table(conn)
    rows = conn.execute(
        "SELECT * FROM skill_rules WHERE state IN ('proposed','kept') "
        "ORDER BY support DESC, last_seen_at DESC"
    ).fetchall()
    return [_row_to_rule(r) for r in rows]


def installed_fingerprints(conn: sqlite3.Connection) -> set[str]:
    ensure_table(conn)
    return {r[0] for r in conn.execute(
        "SELECT fingerprint FROM skill_rules WHERE installed_at IS NOT NULL AND state = 'kept'")}


def upsert_seen(conn: sqlite3.Connection, rule: SkillRule, *, now: str | None = None) -> str:
    """Insert or refresh a rule (bumps last_seen + support); returns fingerprint.

    Never revives a rejected fingerprint unless the guidance materially changed
    (a new fingerprint is a different rule, so this is automatic).
    """
    ts = now or now_iso()
    fp = fingerprint(rule)
    ensure_table(conn)
    existing = conn.execute("SELECT state, support, created_at FROM skill_rules WHERE fingerprint = ?", (fp,)).fetchone()
    ev = json.dumps(list(rule.evidence_session_ids))
    # last_seen tracks when the rule was last SEEN in distillation, NOT when it was
    # installed: a carried-over rule (rule.last_seen already set, not re-distilled this
    # run) keeps its original timestamp so _decayed_support can actually age it out.
    # A freshly distilled rule has last_seen == "" -> stamped now.
    seen_ts = rule.last_seen or ts
    if existing is None:
        conn.execute(
            "INSERT INTO skill_rules (fingerprint, kind, title, trigger, guidance, why, taxonomy, "
            "support, evidence_json, state, created_at, last_seen_at) "
            "VALUES (?,?,?,?,?,?,?,?,?, 'proposed', ?, ?)",
            (fp, rule.kind, rule.title, rule.trigger, rule.guidance, rule.why, rule.taxonomy,
             rule.support, ev, ts, seen_ts),
        )
    elif existing["state"] != "rejected":
        # Refresh content + support, and revive a previously-'dropped' fingerprint back
        # to 'proposed' so a re-distilled rule reloads via load_kept next run instead of
        # being re-distilled from scratch every time.
        conn.execute(
            "UPDATE skill_rules SET support = MAX(support, ?), evidence_json = ?, "
            "why = ?, title = ?, trigger = ?, taxonomy = ?, last_seen_at = ?, "
            "state = CASE WHEN state = 'dropped' THEN 'proposed' ELSE state END "
            "WHERE fingerprint = ?",
            (rule.support, ev, rule.why, rule.title, rule.trigger, rule.taxonomy, seen_ts, fp),
        )
    conn.commit()
    return fp


def mark_installed(conn: sqlite3.Connection, rules: list[SkillRule], *, now: str | None = None) -> None:
    ts = now or now_iso()
    ensure_table(conn)
    selected_fps: list[str] = []
    for r in rules:
        fp = upsert_seen(conn, r, now=ts)
        selected_fps.append(fp)
        # NB: do NOT touch last_seen_at here — installation is not a "sighting". upsert_seen
        # already set it (now for fresh, preserved for carried) so decay keeps working.
        conn.execute(
            "UPDATE skill_rules SET state = 'kept', installed_at = ?, "
            "approved_at = COALESCE(approved_at, ?) WHERE fingerprint = ? AND state != 'rejected'",
            (ts, ts, fp),
        )
    if selected_fps:
        placeholders = ",".join("?" for _ in selected_fps)
        conn.execute(
            "UPDATE skill_rules SET state = 'dropped', installed_at = NULL, last_seen_at = ? "
            f"WHERE state IN ('proposed','kept') AND fingerprint NOT IN ({placeholders})",
            (ts, *selected_fps),
        )
    conn.commit()


def _ensure_snapshots(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS skill_mode_snapshots ("
        "recorded_at TEXT, n INTEGER, rates_json TEXT)"
    )
    conn.commit()


def save_mode_snapshot(conn: sqlite3.Connection, rates: dict[str, float], n: int,
                       *, now: str | None = None) -> None:
    """Record the window's per-mode incidence rates (for the week-over-week signal)."""
    _ensure_snapshots(conn)
    conn.execute(
        "INSERT INTO skill_mode_snapshots (recorded_at, n, rates_json) VALUES (?,?,?)",
        (now or now_iso(), int(n), json.dumps(rates)),
    )
    conn.commit()


def last_mode_snapshot(conn: sqlite3.Connection) -> tuple[str, int, dict[str, float]] | None:
    """Return the most recent (recorded_at, n, rates) snapshot, or None."""
    _ensure_snapshots(conn)
    row = conn.execute(
        "SELECT recorded_at, n, rates_json FROM skill_mode_snapshots ORDER BY recorded_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    try:
        rates = {str(k): float(v) for k, v in json.loads(row[2] or "{}").items()}
    except (TypeError, ValueError, json.JSONDecodeError):
        rates = {}
    return (row[0], int(row[1] or 0), rates)


def reject(conn: sqlite3.Connection, fp: str, *, now: str | None = None) -> bool:
    ensure_table(conn)
    ts = now or now_iso()
    cur = conn.execute(
        "UPDATE skill_rules SET state = 'rejected', rejected_at = ?, installed_at = NULL "
        "WHERE fingerprint = ?", (ts, fp))
    conn.commit()
    return cur.rowcount > 0
