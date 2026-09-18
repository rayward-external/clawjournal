"""Local, immutable inputs for the manual Share review/packaging workflow.

A preview is not upload authority. The explicit create-share request chooses
these inputs; live source, hold, exclusion, consent and secret gates still apply.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any

MAX_SNAPSHOT_BYTES = 128 * 1024 * 1024


def sealed_jsonl_payload(artifact_path: str | None, artifact_sha256: str | None) -> bytes | None:
    """Read only JSONL from a bounded ZIP matching the durable auto-upload seal."""
    import hashlib
    import io
    import zipfile
    import zlib
    from pathlib import Path
    if not artifact_path or not artifact_sha256:
        return None
    try:
        with Path(artifact_path).open('rb') as handle:
            data = handle.read(MAX_SNAPSHOT_BYTES + 1)
        if len(data) > MAX_SNAPSHOT_BYTES or hashlib.sha256(data).hexdigest() != artifact_sha256:
            return None
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            if archive.namelist().count('sessions.jsonl') != 1:
                return None
            info = archive.getinfo('sessions.jsonl')
            if info.file_size > MAX_SNAPSHOT_BYTES:
                return None
            with archive.open(info) as handle:
                payload = handle.read(MAX_SNAPSHOT_BYTES + 1)
            return payload if len(payload) <= MAX_SNAPSHOT_BYTES else None
    except (OSError, ValueError, TypeError, KeyError, zipfile.BadZipFile, RuntimeError, zlib.error):
        return None


def copy_completed_share(conn: sqlite3.Connection, share_id: str, output_path: str | None):
    """Copy receipt-verified JSONL locally; never reconstruct or authorize upload.

    Completed snapshots deliberately lose their raw payload. The receipt's
    bundle_hash pins the exported JSONL independently of later scoring or trace
    changes. No raw snapshot, current session data or unverified report is read.
    """
    from pathlib import Path
    import os
    import tempfile
    from .index import CONFIG_DIR

    row = conn.execute('SELECT shared_at, bundle_hash, manifest, submission_channel, sealed_artifact_path, sealed_artifact_sha256 FROM shares WHERE share_id = ?', (share_id,)).fetchone()
    if row is None or not row['shared_at']:
        return None
    directory = Path(output_path).resolve() if output_path else CONFIG_DIR / 'shares' / share_id / 'local-copy'
    if directory == Path(directory.anchor):
        return None, {}
    try:
        manifest = json.loads(row['manifest'] or '{}')
        if not isinstance(manifest, dict):
            raise ValueError('invalid manifest')
        expected = str(row['bundle_hash'] or '')
        sealed_payload = None
        if row['submission_channel'] == 'auto_weekly':
            sealed_payload = sealed_jsonl_payload(row['sealed_artifact_path'], row['sealed_artifact_sha256'])
            if sealed_payload is not None and not expected:
                expected = hashlib.sha256(sealed_payload).hexdigest()
        if len(expected) != 64:
            raise ValueError('missing receipt hash')
        sources = [CONFIG_DIR / 'shares' / share_id / 'sessions.jsonl']
        if isinstance(manifest.get('export_path'), str):
            sources.append(Path(manifest['export_path']) / 'sessions.jsonl')
        payload = None
        for source in sources:
            try:
                with source.open('rb') as handle:
                    candidate = handle.read(MAX_SNAPSHOT_BYTES + 1)
            except OSError:
                continue
            if len(candidate) <= MAX_SNAPSHOT_BYTES and hashlib.sha256(candidate).hexdigest() == expected:
                payload = candidate
                break
        if payload is None and sealed_payload is not None and hashlib.sha256(sealed_payload).hexdigest() == expected:
            payload = sealed_payload
        if payload is None:
            raise ValueError('missing or changed archived JSONL')
        manifest = {**manifest, 'export_path': str(directory), 'local_copy_only': True}
        directory.mkdir(parents=True, exist_ok=True)
        # Validate the entire input before writing either destination file.
        for name, data in [('sessions.jsonl', payload),
                           ('manifest.json', json.dumps(manifest, ensure_ascii=True, indent=2).encode())]:
            with tempfile.NamedTemporaryFile(dir=directory, prefix=f'.{name}.', suffix='.tmp', delete=False) as handle:
                temp = Path(handle.name)
            try:
                temp.write_bytes(data)
                os.replace(temp, directory / name)
            finally:
                temp.unlink(missing_ok=True)
        return directory, manifest
    except (OSError, ValueError, TypeError):
        return directory, {'blocked': True, 'block_reason': 'completed_artifact_unavailable',
            'block_message': 'The saved completed export is missing or changed. Use the ZIP you previously downloaded. Current trace data cannot recreate the original export.'}


def review_identity(detail: dict[str, Any]) -> str:
    """Scope plus a digest of reviewed export metadata; never another raw copy."""
    from .index import EXPORT_FIELDS
    metadata = {key: detail[key] for key in EXPORT_FIELDS if key != 'messages' and key in detail}
    encoded = json.dumps(metadata, ensure_ascii=True, sort_keys=True, default=str).encode('utf-8')
    identity = json.dumps({**{key: detail.get(key) for key in ('source', 'project', 'logical_session_id')},
                           'metadata_sha256': hashlib.sha256(encoded).hexdigest()}, sort_keys=True)
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


def prune_review_snapshots(conn: sqlite3.Connection, *, reserve_bytes: int = 0,
                           keep_snapshot_ids: set[str] | None = None) -> None:
    """Release completed payloads without invalidating issued preview IDs.

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
    # Successful preview IDs remain valid until explicit cleanup or completion.
    # save_review_snapshot rejects new input before the byte budget is exceeded.


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


def save_review_snapshot(conn: sqlite3.Connection, detail: dict[str, Any], *,
                         keep_snapshot_ids: set[str] | None = None) -> str:
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
    exists = conn.execute("SELECT 1 FROM share_review_snapshots WHERE snapshot_id = ? AND payload != ''", (snapshot_id,)).fetchone()
    keep = (keep_snapshot_ids or set()) | {snapshot_id}
    prune_review_snapshots(conn, reserve_bytes=0 if exists else size, keep_snapshot_ids=keep_snapshot_ids)
    # Completion cleanup can clear a previously saved payload. Reserve its
    # bytes again if the user explicitly previews that input after completion.
    exists = conn.execute("SELECT 1 FROM share_review_snapshots WHERE snapshot_id = ? AND payload != ''", (snapshot_id,)).fetchone()
    total = conn.execute("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))), 0) FROM share_review_snapshots").fetchone()[0]
    if total + (0 if exists else size) > MAX_SNAPSHOT_BYTES:
        conn.commit()
        raise ReviewSnapshotError("The review cache is full. Existing previews are preserved. Finish pending shares, or go to Share > Queue > Clear saved reviews (CLI: clawjournal review-cache --clear --all). Clearing saved reviews invalidates unsubmitted reviews.")
    conn.execute(
        "INSERT OR IGNORE INTO share_review_snapshots (snapshot_id, session_id, content_revision, payload, created_at) VALUES (?, ?, ?, ?, ?)",
        (snapshot_id, detail["session_id"], revision, payload, _now_iso()),
    )
    # A cleared tombstone may be explicitly reviewed again; normal revision
    # duplicate checks still reject content already shared.
    conn.execute("UPDATE share_review_snapshots SET payload = ? WHERE snapshot_id = ? AND payload = ''", (payload, snapshot_id))
    conn.execute("UPDATE share_review_snapshots SET created_at = ? WHERE snapshot_id = ?", (_now_iso(), snapshot_id))
    identity = review_identity(detail)
    conn.execute("UPDATE share_review_snapshots SET identity = ? WHERE snapshot_id = ?", (identity, snapshot_id))
    prune_review_snapshots(conn, keep_snapshot_ids=keep)
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
    # A completed share no longer needs a second raw transcript copy. A local
    # re-export may reload the original source ONLY when both content and scope
    # still match. This cannot recover an appended revision or a cleared pending
    # review, and it does not grant any upload authority.
    saved = conn.execute("""SELECT r.payload, r.content_revision, r.identity, s.shared_at,
        ss.replaces_revision FROM share_review_snapshots r
        JOIN share_snapshot_links l ON l.snapshot_id = r.snapshot_id
        JOIN shares s ON s.share_id = l.share_id
        JOIN share_sessions ss ON ss.share_id = l.share_id AND ss.session_id = l.session_id
        WHERE l.share_id = ? AND l.session_id = ?""", (share_id, session_id)).fetchone()
    if saved is not None and saved['payload'] == '' and saved['shared_at'] and saved['identity']:
        from .index import compute_content_revision, get_session_detail
        detail = get_session_detail(conn, session_id)
        if (detail is not None and detail.get('checkpoint_active') and detail.get('review_status') != 'blocked'
                and detail.get('content_revision') == saved['content_revision']
                and compute_content_revision(detail) == saved['content_revision']
                and review_identity(detail) == saved['identity']):
            detail['_review_predecessor'] = saved['replaces_revision']
            return detail
    return load_review_snapshot(conn, row["snapshot_id"], session_id)
