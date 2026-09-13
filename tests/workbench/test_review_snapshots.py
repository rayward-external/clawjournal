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


def test_ambiguous_boundary_blocks_manual_share_without_changing_reviewed_data(conn):
    from clawjournal.workbench.daemon import finalize_share_export_for_upload

    content = "Intro <" + "A" * 1000 + "alice@audit.test> ordinary tail"
    original = trace(content)
    index.upsert_sessions(conn, [original])
    snapshot = preview(conn)
    share_id = create(conn, snapshot)
    output, manifest = index.export_share_to_disk(conn, share_id, index.get_share(conn, share_id))
    assert manifest["blocked"] is True
    assert manifest["block_reason"] == "redaction_boundary"
    assert not (output / "sessions.jsonl").exists()
    assert not list(output.glob("*.tmp"))
    assert index.get_session_detail(conn, "test-trace")["messages"][0]["content"] == content
    assert load_review_snapshot(conn, snapshot, "test-trace")["messages"][0]["content"] == content
    assert index.get_share(conn, share_id)["status"] == "draft"
    error, _ = finalize_share_export_for_upload(output, manifest, conn=conn)
    assert error["status"] == 422
    assert error["block_reason"] == "redaction_boundary"
    assert content not in str(error)


def test_explicit_blocked_domain_removes_even_oversized_labels(conn):
    detail = trace("a" * 1000 + ".example.test")
    result, _, _ = index.apply_share_redactions(conn, detail, blocked_domains=["*.example.test"])
    assert result['messages'][0]['content'] == '[REDACTED_DOMAIN]'


def test_explicit_custom_redaction_can_resolve_an_ambiguous_boundary(conn):
    value = "A" * 1000 + "alice@audit.test"
    detail = trace("before<" + value + ">after")
    redacted, _count, _log = index.apply_share_redactions(conn, detail, custom_strings=[value])
    assert redacted["messages"][0]["content"] == "before<[REDACTED_CUSTOM]>after"


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
        index.create_share(conn, ["test-trace"], review_snapshot_ids={"test-trace": snapshot})
    index.upsert_sessions(conn, [trace("Reviewed next version")])
    snapshot = preview(conn)
    index.upsert_sessions(conn, [trace("Newer version uploaded in another window")])
    second = index.create_share(conn, ["test-trace"])
    conn.execute("UPDATE shares SET shared_at = '2026-09-02', status = 'shared' WHERE share_id = ?", (second,))
    conn.commit()
    with pytest.raises(index.RevisionConflictError):
        index.create_share(conn, ["test-trace"], review_snapshot_ids={"test-trace": snapshot})


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


def test_abandoned_revisions_are_replaced_but_linked_inputs_survive(conn):
    from clawjournal.workbench.review_snapshots import prune_review_snapshots
    index.upsert_sessions(conn, [trace('pinned original')])
    pinned = preview(conn)
    share_id = create(conn, pinned)
    for i in range(25):
        index.upsert_sessions(conn, [trace(f'new revision {i}')])
        latest = preview(conn)
    assert conn.execute('SELECT COUNT(*) FROM share_review_snapshots').fetchone()[0] == 2
    assert load_review_snapshot(conn, pinned, 'test-trace')['messages'][0]['content'] == 'pinned original'
    assert load_review_snapshot(conn, latest, 'test-trace')['messages'][0]['content'] == 'new revision 24'
    conn.execute("UPDATE shares SET shared_at = '2026-09-12', status = 'shared' WHERE share_id = ?", (share_id,))
    prune_review_snapshots(conn)
    assert conn.execute('SELECT payload FROM share_review_snapshots WHERE snapshot_id = ?', (pinned,)).fetchone()[0] == ''
    assert conn.execute('SELECT COUNT(*) FROM share_snapshot_links').fetchone()[0] == 1
    with pytest.raises(ReviewSnapshotError):
        load_review_snapshot(conn, pinned, 'test-trace')


def test_cache_limits_and_explicit_clear_keep_pending_links_safe(conn, monkeypatch):
    from clawjournal.workbench import review_snapshots as cache
    monkeypatch.setattr(cache, 'MAX_UNLINKED_SNAPSHOTS', 2)
    for i in range(4):
        sid = f'synthetic-{i}'
        index.upsert_sessions(conn, [trace('raw credential example', sid)])
        preview(conn, sid)
    assert conn.execute('SELECT COUNT(*) FROM share_review_snapshots').fetchone()[0] == 2
    index.upsert_sessions(conn, [trace('pinned')])
    pinned = preview(conn)
    share_id = create(conn, pinned)
    cache.clear_review_cache(conn)
    assert load_review_snapshot(conn, pinned, 'test-trace')
    size = conn.execute('SELECT length(CAST(payload AS BLOB)) FROM share_review_snapshots').fetchone()[0]
    monkeypatch.setattr(cache, 'MAX_SNAPSHOT_BYTES', size + 1)
    index.upsert_sessions(conn, [trace('next', 'other')])
    with pytest.raises(ReviewSnapshotError, match='cache is full'):
        preview(conn, 'other')
    assert load_review_snapshot(conn, pinned, 'test-trace')
    cache.clear_review_cache(conn, include_linked=True)
    assert index.share_revision_blockers(conn, share_id)
    assert conn.execute("SELECT COALESCE(SUM(length(payload)), 0) FROM share_review_snapshots").fetchone()[0] == 0


def test_snapshot_cannot_reshare_an_older_successful_revision(conn):
    for content, date in [('version one', '2026-09-01'), ('version two', '2026-09-02')]:
        index.upsert_sessions(conn, [trace(content)])
        sid = index.create_share(conn, ['test-trace'])
        conn.execute("UPDATE shares SET shared_at = ?, status = 'shared' WHERE share_id = ?", (date, sid))
        conn.commit()
    index.upsert_sessions(conn, [trace('version one')])
    snapshot = preview(conn)
    with pytest.raises(index.RevisionConflictError):
        create(conn, snapshot)


def test_cache_reserves_space_again_after_evicting_an_existing_preview(conn, monkeypatch):
    from clawjournal.workbench import review_snapshots as cache
    index.upsert_sessions(conn, [trace('pinned')])
    pinned = preview(conn)
    create(conn, pinned)
    pinned_size = conn.execute('SELECT length(CAST(payload AS BLOB)) FROM share_review_snapshots').fetchone()[0]
    index.upsert_sessions(conn, [trace('unused', 'other')])
    unused = preview(conn, 'other')
    monkeypatch.setattr(cache, 'MAX_SNAPSHOT_BYTES', pinned_size + 1)
    with pytest.raises(ReviewSnapshotError, match='cache is full'):
        preview(conn, 'other')
    assert load_review_snapshot(conn, pinned, 'test-trace')
    with pytest.raises(ReviewSnapshotError):
        load_review_snapshot(conn, unused, 'other')


def test_engine_change_flags_stored_blobs_without_repeated_backfill(conn, monkeypatch):
    from clawjournal import findings
    index.upsert_sessions(conn, [trace('stored blob with no original transcript')])
    conn.execute("UPDATE sessions SET findings_backfill_needed = 0, findings_revision = 'old-engine-revision'")
    conn.commit()
    monkeypatch.setattr(findings, 'ENGINE_VERSION', findings.ENGINE_VERSION + 1)
    index._migrate_redaction_cache_state(conn)
    assert conn.execute('SELECT findings_backfill_needed FROM sessions').fetchone()[0] == 1
    conn.execute('UPDATE sessions SET findings_backfill_needed = 0')
    conn.commit()
    index._migrate_redaction_cache_state(conn)
    assert conn.execute('SELECT findings_backfill_needed FROM sessions').fetchone()[0] == 0


def test_open_index_does_not_take_a_write_lock_for_an_unchanged_cache(conn):
    # All API requests call open_index. An unconditional cache UPDATE would
    # serialize otherwise read-only requests behind another writer.
    conn.execute('BEGIN IMMEDIATE')
    other = index.open_index()
    other.close()
    conn.rollback()


def test_completed_share_reexports_unchanged_source_without_restoring_raw_cache(conn, tmp_path):
    index.upsert_sessions(conn, [trace()])
    snapshot = preview(conn)
    share_id = create(conn, snapshot)
    first_dir, first = index.export_share_to_disk(conn, share_id, index.get_share(conn, share_id))
    first_bytes = (first_dir / 'sessions.jsonl').read_bytes()
    conn.execute("UPDATE shares SET status = 'shared', shared_at = '2026-09-13' WHERE share_id = ?", (share_id,))
    conn.commit()
    assert conn.execute('SELECT payload FROM share_review_snapshots WHERE snapshot_id = ?', (snapshot,)).fetchone()[0] == ''
    second_dir, second = index.export_share_to_disk(conn, share_id, index.get_share(conn, share_id), output_path=str(tmp_path / 'reexport'))
    assert not first.get('blocked') and not second.get('blocked')
    assert json.loads((second_dir / 'sessions.jsonl').read_bytes()) == json.loads(first_bytes)
    assert conn.execute('SELECT payload FROM share_review_snapshots WHERE snapshot_id = ?', (snapshot,)).fetchone()[0] == ''


@pytest.mark.parametrize('change', ['append', 'scope', 'metadata', 'inactive', 'blocked', 'pending-cleared', 'legacy-identity'])
def test_cleared_snapshot_recovery_never_authorizes_different_inputs(conn, change):
    from clawjournal.workbench.review_snapshots import clear_review_cache
    index.upsert_sessions(conn, [trace()])
    snapshot = preview(conn)
    share_id = create(conn, snapshot)
    if change == 'pending-cleared':
        clear_review_cache(conn, include_linked=True)
    else:
        conn.execute("UPDATE shares SET shared_at = '2026-09-13' WHERE share_id = ?", (share_id,))
    if change == 'append':
        index.upsert_sessions(conn, [trace('Later unreviewed content')])
    elif change == 'scope':
        conn.execute("UPDATE sessions SET source = 'claude'")
    elif change == 'metadata':
        conn.execute("UPDATE sessions SET display_title = 'New unreviewed title'")
    elif change == 'inactive':
        conn.execute('UPDATE sessions SET checkpoint_active = 0')
    elif change == 'blocked':
        conn.execute("UPDATE sessions SET review_status = 'blocked'")
    elif change == 'legacy-identity':
        conn.execute('UPDATE share_review_snapshots SET identity = NULL')
    conn.commit()
    assert index.share_revision_blockers(conn, share_id)


def test_active_cli_selection_cannot_evict_its_own_earlier_previews(conn, monkeypatch):
    from clawjournal import share_cli
    from clawjournal.workbench import review_snapshots as cache
    monkeypatch.setattr(cache, 'MAX_UNLINKED_SNAPSHOTS', 100)
    for engine in ('betterleaks', 'trufflehog'):
        monkeypatch.setattr(f'clawjournal.redaction.{engine}.{engine}_secret_map_from_blob', lambda *a, **kw: {})
    rows = [trace('A synthetic trace', f'selection-{i}') for i in range(101)]
    index.upsert_sessions(conn, rows)
    settings = dict(custom_strings=[], allowlist_entries=[], extra_usernames=[], blocked_domains=[])
    records = share_cli._build_records(conn, settings, rows, False)
    share_id = index.create_share(conn, [r['session_id'] for r in rows],
        review_snapshot_ids={r['row']['session_id']: r['review_snapshot_id'] for r in records})
    assert len(index.get_share(conn, share_id)['sessions']) == 101
    # A different preview still cannot evict the now-linked selection.
    index.upsert_sessions(conn, [trace('Next trace', 'next')])
    preview(conn, 'next')
    assert not index.share_revision_blockers(conn, share_id)


def test_review_identity_migration_retains_scope_and_runs_once(conn):
    index.upsert_sessions(conn, [trace()])
    snapshot = preview(conn)
    conn.execute('ALTER TABLE share_review_snapshots DROP COLUMN identity')
    conn.execute('PRAGMA user_version = 15')
    conn.commit()
    index._migrate_review_snapshot_identity(conn)
    identity = conn.execute('SELECT identity FROM share_review_snapshots WHERE snapshot_id = ?', (snapshot,)).fetchone()[0]
    from clawjournal.workbench.review_snapshots import review_identity
    assert identity == review_identity(index.get_session_detail(conn, 'test-trace'))
    assert len(identity) == 64 and 'synthetic-project' not in identity
    index._migrate_review_snapshot_identity(conn)
