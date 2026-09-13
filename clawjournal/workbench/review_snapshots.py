"""Local, immutable inputs for the manual Share review/packaging workflow.

A preview is not upload authority. The explicit create-share request chooses
these inputs; live source, hold, exclusion, consent and secret gates still apply.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

MAX_UNLINKED_SNAPSHOTS = 100
MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024


def prune_review_snapshots(conn: sqlite3.Connection, *, reserve_bytes: int = 0) -> None:
    """Bound raw preview storage without changing in-flight share inputs.

    Keep links as tombstones after success: an old share must never silently
    fall back to a newer live blob. No approval expires just because time passes.
    """
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("""UPDATE share_review_snapshots SET payload = ''
        WHERE payload != '' AND EXISTS (
            SELECT 1 FROM share_snapshot_links l WHERE l.snapshot_id = share_review_snapshots.snapshot_id
        ) AND NOT EXISTS (
            SELECT 1 FROM share_snapshot_links l JOIN shares s ON s.share_id = l.share_id
            WHERE l.snapshot_id = share_review_snapshots.snapshot_id AND s.shared_at IS NULL
        )""")
    rows = conn.execute("""SELECT r.snapshot_id, length(CAST(r.payload AS BLOB)) AS size
        FROM share_review_snapshots r WHERE NOT EXISTS (
            SELECT 1 FROM share_snapshot_links l WHERE l.snapshot_id = r.snapshot_id
        ) ORDER BY r.created_at, r.rowid""").fetchall()
    total = conn.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))), 0) FROM share_review_snapshots").fetchone()[0]
    remaining = len(rows)
    for row in rows:
        if remaining <= MAX_UNLINKED_SNAPSHOTS and total + reserve_bytes <= MAX_SNAPSHOT_BYTES:
            break
        conn.execute("DELETE FROM share_review_snapshots WHERE snapshot_id = ?", (row['snapshot_id'],))
        total -= row['size']
        remaining -= 1


def clear_review_cache(conn: sqlite3.Connection, *, include_linked: bool = False) -> None:
    """Remove unused previews; explicit all also invalidates pending reviews."""
    conn.execute("PRAGMA secure_delete = ON")
    conn.execute("""DELETE FROM share_review_snapshots WHERE NOT EXISTS (
        SELECT 1 FROM share_snapshot_links l WHERE l.snapshot_id = share_review_snapshots.snapshot_id
    )""")
    if include_linked:
        conn.execute("UPDATE share_review_snapshots SET payload = ''")
    conn.commit()


class ReviewSnapshotError(ValueError):
    pass


def create_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TABLE IF NOT EXISTS share_review_snapshots (
        snapshot_id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        content_revision TEXT NOT NULL,
        payload TEXT NOT NULL,
        created_at TEXT NOT NULL
    )""")
    conn.execute("""CREATE TABLE IF NOT EXISTS share_snapshot_links (
        share_id TEXT NOT NULL REFERENCES shares(share_id) ON DELETE CASCADE,
        session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
        snapshot_id TEXT NOT NULL REFERENCES share_review_snapshots(snapshot_id),
        PRIMARY KEY (share_id, session_id)
    )""")


def install_cleanup_trigger(conn: sqlite3.Connection) -> None:
    conn.execute("""CREATE TRIGGER IF NOT EXISTS clear_completed_review_payloads
        AFTER UPDATE OF shared_at ON shares WHEN NEW.shared_at IS NOT NULL
        BEGIN
            UPDATE share_review_snapshots SET payload = ''
            WHERE snapshot_id IN (SELECT snapshot_id FROM share_snapshot_links WHERE share_id = NEW.share_id)
            AND NOT EXISTS (
                SELECT 1 FROM share_snapshot_links l JOIN shares s ON s.share_id = l.share_id
                WHERE l.snapshot_id = share_review_snapshots.snapshot_id AND s.shared_at IS NULL
            );
        END""")


def save_review_snapshot(conn: sqlite3.Connection, detail: dict[str, Any]) -> str:
    """Persist a successful preview within a bounded local cache."""
    from .index import _latest_successful_revision, _now_iso, compute_content_revision

    revision = compute_content_revision(detail)
    if revision != detail.get("content_revision"):
        raise ReviewSnapshotError("The trace changed while loading. Refresh its preview.")
    detail = {**detail, "_review_predecessor": _latest_successful_revision(conn, detail["session_id"])}
    payload = json.dumps(detail, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    snapshot_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    size = len(payload.encode('utf-8'))
    if size > MAX_SNAPSHOT_BYTES:
        raise ReviewSnapshotError("This trace exceeds the review cache limit. Select a smaller trace.")
    # Replace abandoned revisions for this trace, but never inputs linked to a
    # pending share. A second tab using the old preview must refresh explicitly.
    conn.execute("""DELETE FROM share_review_snapshots
        WHERE session_id = ? AND snapshot_id != ? AND NOT EXISTS (
            SELECT 1 FROM share_snapshot_links l WHERE l.snapshot_id = share_review_snapshots.snapshot_id
        )""", (detail['session_id'], snapshot_id))
    exists = conn.execute("SELECT 1 FROM share_review_snapshots WHERE snapshot_id = ? AND payload != ''", (snapshot_id,)).fetchone()
    prune_review_snapshots(conn, reserve_bytes=0 if exists else size)
    # Pruning can remove an existing unused copy after a cache-limit change.
    # Recheck before reserving space; never return an immediately evicted ID.
    exists = conn.execute("SELECT 1 FROM share_review_snapshots WHERE snapshot_id = ? AND payload != ''", (snapshot_id,)).fetchone()
    total = conn.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))), 0) FROM share_review_snapshots").fetchone()[0]
    if total + (0 if exists else size) > MAX_SNAPSHOT_BYTES:
        conn.commit()
        raise ReviewSnapshotError("The review cache is full. Finish pending shares or run clawjournal review-cache --clear --all, then refresh previews.")
    conn.execute(
        "INSERT OR IGNORE INTO share_review_snapshots VALUES (?, ?, ?, ?, ?)",
        (snapshot_id, detail["session_id"], revision, payload, _now_iso()),
    )
    # A cleared tombstone may be explicitly reviewed again; normal revision
    # duplicate checks still reject content already shared.
    conn.execute("UPDATE share_review_snapshots SET payload = ? WHERE snapshot_id = ? AND payload = ''", (payload, snapshot_id))
    conn.execute("UPDATE share_review_snapshots SET created_at = ? WHERE snapshot_id = ?", (_now_iso(), snapshot_id))
    prune_review_snapshots(conn)
    if conn.execute("SELECT 1 FROM share_review_snapshots WHERE snapshot_id = ? AND payload != ''", (snapshot_id,)).fetchone() is None:
        conn.commit()
        raise ReviewSnapshotError("The saved review is no longer available. Refresh the trace preview.")
    conn.commit()
    return snapshot_id


def load_review_snapshot(
    conn: sqlite3.Connection, snapshot_id: str, session_id: str,
) -> dict[str, Any]:
    from .index import compute_content_revision

    row = conn.execute(
        "SELECT * FROM share_review_snapshots WHERE snapshot_id = ? AND session_id = ?",
        (snapshot_id, session_id),
    ).fetchone()
    error = "The saved review is missing or changed. Refresh the trace preview."
    if row is None or hashlib.sha256(row["payload"].encode("utf-8")).hexdigest() != snapshot_id:
        raise ReviewSnapshotError(error)
    try:
        detail = json.loads(row["payload"])
        if (
            detail["session_id"] != session_id
            or compute_content_revision(detail) != row["content_revision"]
            or detail.get("content_revision") != row["content_revision"]
        ):
            raise ReviewSnapshotError(error)
    except (TypeError, KeyError, json.JSONDecodeError) as exc:
        raise ReviewSnapshotError(error) from exc
    current = conn.execute("SELECT * FROM sessions WHERE session_id = ?", (session_id,)).fetchone()
    if current is None or not current["checkpoint_active"]:
        raise ReviewSnapshotError("The reviewed trace is no longer active. Refresh its preview.")
    if any(current[key] != detail.get(key) for key in ("source", "project", "logical_session_id")):
        raise ReviewSnapshotError("The reviewed trace's source or project changed. Refresh its preview.")
    if current["review_status"] == "blocked":
        raise ReviewSnapshotError("The reviewed trace is now blocked from sharing.")
    return detail


def load_share_snapshot(
    conn: sqlite3.Connection, share_id: str, session_id: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT snapshot_id FROM share_snapshot_links WHERE share_id = ? AND session_id = ?",
        (share_id, session_id),
    ).fetchone()
    if row is None:
        return None
    return load_review_snapshot(conn, row["snapshot_id"], session_id)
