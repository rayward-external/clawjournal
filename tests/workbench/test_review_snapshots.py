"""A manual share pins reviewed content, while live controls still win."""
import json

import pytest

from clawjournal.workbench import index
from clawjournal.workbench.daemon import _final_manual_share_egress_gate
from clawjournal.workbench.review_snapshots import (
    ReviewSnapshotError, load_review_snapshot, save_review_snapshot,
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr(index, "BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr(index, "CONFIG_DIR", tmp_path)
    db = index.open_index()
    yield db
    db.close()


def trace(content="Reviewed message for alice@example.com", session_id="test-trace"):
    return {
        "session_id": session_id, "source": "codex", "project": "synthetic-project",
        "messages": [{"role": "user", "content": content}],
        "stats": {"user_messages": 1},
    }


def preview(conn, session_id="test-trace"):
    return save_review_snapshot(conn, index.get_session_detail(conn, session_id))


def create(conn, snapshot):
    detail = load_review_snapshot(conn, snapshot, "test-trace")
    return index.create_share(
        conn, ["test-trace"],
        expected_revisions={"test-trace": detail["content_revision"]},
        review_snapshot_ids={"test-trace": snapshot},
    )


def test_preview_survives_restart_and_append_before_and_after_packaging(conn):
    original = trace()
    index.upsert_sessions(conn, [original])
    snapshot = preview(conn)
    expected = index.compute_content_revision(original)
    conn.execute("UPDATE share_review_snapshots SET created_at = '2000-01-01'")
    conn.commit()
    reopened = index.open_index()
    try:
        assert load_review_snapshot(reopened, snapshot, "test-trace")["content_revision"] == expected
    finally:
        reopened.close()
    index.upsert_sessions(conn, [trace("New unreviewed content must stay local")])
    share_id = create(conn, snapshot)
    output, manifest = index.export_share_to_disk(conn, share_id, index.get_share(conn, share_id))
    payload = json.loads((output / "sessions.jsonl").read_text())
    assert payload["revision_hash"] == expected
    assert "Reviewed message" in json.dumps(payload)
    assert "alice@example.com" not in json.dumps(payload)
    assert "New unreviewed content" not in json.dumps(payload)
    index.upsert_sessions(conn, [trace("Another later message")])
    assert not index.share_revision_blockers(conn, share_id)
    assert _final_manual_share_egress_gate(conn, share_id, ["test-trace"], ["codex"]) is None
    assert manifest["sessions"][0]["revision_hash"] == expected


@pytest.mark.parametrize("change", ["hold", "blocked", "project", "inactive", "corrupt", "missing"])
def test_later_controls_and_snapshot_integrity_block_egress(conn, change):
    index.upsert_sessions(conn, [trace()])
    snapshot = preview(conn)
    share_id = create(conn, snapshot)
    if change == "hold":
        index.set_hold_state(conn, "test-trace", "pending_review", changed_by="user")
    elif change == "blocked":
        index.update_session(conn, "test-trace", status="blocked")
    elif change == "project":
        conn.execute("UPDATE sessions SET project = 'different-project'")
    elif change == "inactive":
        conn.execute("UPDATE sessions SET checkpoint_active = 0")
    elif change == "corrupt":
        conn.execute("UPDATE share_review_snapshots SET payload = '{}' WHERE snapshot_id = ?", (snapshot,))
    else:
        # Simulate damaged/missing snapshot storage without changing its link.
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute("DELETE FROM share_review_snapshots WHERE snapshot_id = ?", (snapshot,))
    conn.commit()
    error = _final_manual_share_egress_gate(conn, share_id, ["test-trace"], ["codex"])
    assert error is not None
    assert error["status"] == 409


def test_changed_previously_shared_trace_can_use_explicit_preview_without_global_approval(conn):
    index.upsert_sessions(conn, [trace("Already uploaded")])
    first = index.create_share(conn, ["test-trace"])
    conn.execute("UPDATE shares SET shared_at = '2026-09-01', status = 'shared' WHERE share_id = ?", (first,))
    conn.commit()
    index.upsert_sessions(conn, [trace("Reviewed update")])
    assert index.revision_review_blockers(conn, ["test-trace"])
    snapshot = preview(conn)
    index.upsert_sessions(conn, [trace("Later content")])
    share_id = create(conn, snapshot)
    output, _ = index.export_share_to_disk(conn, share_id, index.get_share(conn, share_id))
    assert "Reviewed update" in (output / "sessions.jsonl").read_text()
    assert "Later content" not in (output / "sessions.jsonl").read_text()
    # A manual preview does not grant recurring authority or approve the live revision.
    assert index.auto_upload_review_blockers(conn, ["test-trace"])


def test_snapshot_cannot_be_substituted_for_a_different_trace(conn):
    index.upsert_sessions(conn, [trace(), trace("Different trace", "other")])
    snapshot = preview(conn)
    with pytest.raises(index.RevisionConflictError):
        index.create_share(conn, ["other"], review_snapshot_ids={"other": snapshot})
    assert conn.execute("SELECT COUNT(*) FROM shares").fetchone()[0] == 0


def test_later_checkpoint_stays_out_of_the_reviewed_share(conn):
    root = trace("Reviewed checkpoint")
    root.update({
        "logical_session_id": "test-trace", "segment_index": 0,
        "segment_message_range": [0, 0], "segment_reason": "bounded_checkpoint",
        "segment_sealed": True, "raw_source_path": "/tmp/test-trace.jsonl",
    })
    index.upsert_sessions(conn, [root])
    snapshot = preview(conn)
    logical_revision = index.query_logical_sessions(conn)[0]["logical_revision"]
    tail = {
        **root, **trace("Unreviewed checkpoint", "test-trace_seg-0001"),
        "segment_index": 1, "segment_message_range": [1, 1],
        "segment_reason": "active_tail", "segment_sealed": False,
    }
    index.upsert_sessions(conn, [root, tail])
    assert index.query_logical_sessions(conn)[0]["logical_revision"] != logical_revision
    share_id = index.create_share(
        conn, ["test-trace"],
        review_snapshot_ids={"test-trace": snapshot},
        expected_logical_revisions={"test-trace": logical_revision},
    )
    output, manifest = index.export_share_to_disk(conn, share_id, index.get_share(conn, share_id))
    assert not manifest.get("blocked")
    assert len(manifest["sessions"]) == 1
    assert "Reviewed checkpoint" in (output / "sessions.jsonl").read_text()
    assert "Unreviewed checkpoint" not in (output / "sessions.jsonl").read_text()


def test_snapshot_cannot_repeat_an_uploaded_version_or_replace_a_newer_share(conn):
    index.upsert_sessions(conn, [trace()])
    snapshot = preview(conn)
    first = create(conn, snapshot)
    conn.execute("UPDATE shares SET shared_at = '2026-09-01', status = 'shared' WHERE share_id = ?", (first,))
    conn.commit()
    with pytest.raises(index.RevisionConflictError):
        create(conn, snapshot)
    index.upsert_sessions(conn, [trace("Reviewed next version")])
    snapshot = preview(conn)
    index.upsert_sessions(conn, [trace("Newer version uploaded in another window")])
    second = index.create_share(conn, ["test-trace"])
    conn.execute("UPDATE shares SET shared_at = '2026-09-02', status = 'shared' WHERE share_id = ?", (second,))
    conn.commit()
    with pytest.raises(index.RevisionConflictError):
        create(conn, snapshot)


def test_capture_rejects_blob_that_does_not_match_the_index_revision(conn):
    index.upsert_sessions(conn, [trace()])
    detail = index.get_session_detail(conn, "test-trace")
    detail["messages"] = trace("Concurrent replacement")["messages"]
    with pytest.raises(ReviewSnapshotError):
        save_review_snapshot(conn, detail)


def test_upgrade_from_v13_preserves_existing_rows(conn):
    index.upsert_sessions(conn, [trace()])
    conn.execute("DROP TABLE share_snapshot_links")
    conn.execute("DROP TABLE share_review_snapshots")
    conn.execute("PRAGMA user_version = 13")
    conn.commit()
    reopened = index.open_index()
    try:
        assert reopened.execute("PRAGMA user_version").fetchone()[0] == index.WORKBENCH_SCHEMA_VERSION
        snapshot = preview(reopened)
        assert load_review_snapshot(reopened, snapshot, "test-trace")["messages"] == trace()["messages"]
    finally:
        reopened.close()
