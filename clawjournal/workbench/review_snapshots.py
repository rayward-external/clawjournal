"""Local, immutable inputs for the manual Share review/packaging workflow.

A preview is not upload authority. The explicit create-share request chooses
these inputs; live source, hold, exclusion, consent and secret gates still apply.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any


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


def save_review_snapshot(conn: sqlite3.Connection, detail: dict[str, Any]) -> str:
    """Persist the exact input used to render a preview, without a time limit."""
    from .index import _latest_successful_revision, _now_iso, compute_content_revision

    revision = compute_content_revision(detail)
    if revision != detail.get("content_revision"):
        raise ReviewSnapshotError("The trace changed while loading. Refresh its preview.")
    detail = {**detail, "_review_predecessor": _latest_successful_revision(conn, detail["session_id"])}
    payload = json.dumps(detail, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    snapshot_id = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT OR IGNORE INTO share_review_snapshots VALUES (?, ?, ?, ?, ?)",
        (snapshot_id, detail["session_id"], revision, payload, _now_iso()),
    )
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
