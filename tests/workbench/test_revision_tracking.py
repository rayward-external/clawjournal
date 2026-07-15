"""Revision tracking for long-running traces that grow after sharing."""

from __future__ import annotations

import json
import sqlite3
from copy import deepcopy

import pytest

from clawjournal.workbench.index import (
    REVISION_TRACKING_SCHEMA_VERSION,
    WORKBENCH_SCHEMA_VERSION,
    RevisionConflictError,
    already_shared_revision_blockers,
    compute_content_revision,
    create_share,
    export_share_to_disk,
    get_share,
    get_share_ready_stats,
    open_index,
    revision_review_blockers,
    set_hold_state,
    share_predecessor_blockers,
    share_revision_blockers,
    update_session,
    upsert_sessions,
)


@pytest.fixture
def index_conn(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
    conn = open_index()
    yield conn
    conn.close()


def _session(session_id: str = "trace-1", content: str = "first message") -> dict:
    return {
        "session_id": session_id,
        "project": "science-project",
        "source": "codex",
        "model": "gpt-5",
        "start_time": "2026-07-01T00:00:00+00:00",
        "end_time": "2026-07-01T01:00:00+00:00",
        "git_branch": "main",
        "messages": [
            {"role": "user", "content": content, "tool_uses": []},
            {"role": "assistant", "content": "working", "tool_uses": []},
        ],
        "stats": {
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_uses": 0,
            "input_tokens": 10,
            "output_tokens": 5,
        },
    }


def _mark_shared(conn, share_id: str, when: str) -> None:
    conn.execute(
        "UPDATE shares SET status = 'shared', shared_at = ? WHERE share_id = ?",
        (when, share_id),
    )
    conn.commit()


def _approve(conn, session_id: str = "trace-1") -> None:
    assert update_session(
        conn,
        session_id,
        status="approved",
        notes="reviewed",
        reason="useful",
        ai_quality_score=5,
        ai_score_reason="strong",
        ai_effort_estimate=0.8,
        ai_summary="summary",
        ai_scoring_detail="{}",
        ai_task_type="research",
        ai_outcome_badge="resolved",
        ai_value_badges="[]",
        ai_risk_badges="[]",
        ai_display_title="Reviewed trace",
        ai_failure_value_score=4,
        ai_recovery_labels="[]",
        ai_failure_attribution="agent",
        ai_failure_modes="[]",
        ai_learning_summary="lesson",
        ai_scorer_backend="codex",
        ai_scorer_model="gpt-5",
        ai_rubric_git_sha="abc",
        ai_scored_at="2026-07-01T02:00:00+00:00",
    )


def test_content_revision_hashes_messages_only():
    original = _session()
    metadata_only = deepcopy(original)
    metadata_only["project"] = "renamed-project"
    metadata_only["model"] = "new-parser-model-label"
    metadata_only["stats"]["input_tokens"] = 999

    assert compute_content_revision(original) == compute_content_revision(metadata_only)

    appended = deepcopy(original)
    appended["messages"].append({"role": "user", "content": "new result"})
    assert compute_content_revision(original) != compute_content_revision(appended)


def test_upsert_distinguishes_updates_and_resets_review_scoring(index_conn):
    first = _session()
    stats: dict[str, int] = {}
    assert upsert_sessions(index_conn, [first], stats=stats) == 1
    assert stats == {"inserted": 1, "updated": 0, "unchanged": 0}
    _approve(index_conn)
    set_hold_state(index_conn, "trace-1", "released", changed_by="user")
    index_conn.execute(
        "UPDATE sessions SET ai_episode_quality = 0.9, ai_quality_tier = 'high' "
        "WHERE session_id = 'trace-1'"
    )
    index_conn.commit()
    before = index_conn.execute(
        "SELECT content_revision, updated_at FROM sessions WHERE session_id = 'trace-1'"
    ).fetchone()

    assert upsert_sessions(index_conn, [deepcopy(first)], stats=stats) == 0
    assert stats == {"inserted": 0, "updated": 0, "unchanged": 1}
    unchanged = index_conn.execute(
        "SELECT review_status, ai_quality_score, updated_at FROM sessions "
        "WHERE session_id = 'trace-1'"
    ).fetchone()
    assert tuple(unchanged) == ("approved", 5, before["updated_at"])

    extended = deepcopy(first)
    extended["messages"].append({
        "role": "user",
        "content": "the experiment produced another result",
        "tool_uses": [],
    })
    extended["stats"]["user_messages"] = 2
    assert upsert_sessions(index_conn, [extended], stats=stats) == 0
    assert stats == {"inserted": 0, "updated": 1, "unchanged": 0}

    row = index_conn.execute(
        "SELECT * FROM sessions WHERE session_id = 'trace-1'"
    ).fetchone()
    assert row["content_revision"] != before["content_revision"]
    assert row["review_status"] == "new"
    assert row["hold_state"] == "released"
    assert row["selection_reason"] is None
    assert row["reviewer_notes"] is None
    assert row["reviewed_at"] is None
    for field in (
        "ai_quality_score", "ai_score_reason", "ai_episode_quality",
        "ai_quality_tier", "ai_scoring_detail", "ai_task_type",
        "ai_outcome_badge", "ai_value_badges", "ai_risk_badges",
        "ai_display_title", "ai_effort_estimate", "ai_summary",
        "ai_failure_value_score", "ai_recovery_labels",
        "ai_failure_attribution", "ai_failure_modes", "ai_learning_summary",
        "ai_scorer_backend", "ai_scorer_model", "ai_rubric_git_sha",
        "ai_scored_at",
    ):
        assert row[field] is None, field


def test_content_update_preserves_segmented_parent_status(index_conn):
    original = _session()
    upsert_sessions(index_conn, [original])
    index_conn.execute(
        "UPDATE sessions SET review_status = 'segmented' WHERE session_id = 'trace-1'"
    )
    index_conn.commit()

    extended = deepcopy(original)
    extended["messages"].append({
        "role": "user",
        "content": "continue the outer trace after child segmentation",
    })
    stats: dict[str, int] = {}
    upsert_sessions(index_conn, [extended], stats=stats)

    row = index_conn.execute(
        "SELECT review_status FROM sessions WHERE session_id = 'trace-1'"
    ).fetchone()
    assert row["review_status"] == "segmented"
    assert stats["updated"] == 1
    assert get_share_ready_stats(index_conn, include_unapproved=True)["sessions"] == []


def test_metadata_only_reparse_refreshes_blob_without_invalidating_review(index_conn):
    original = _session()
    upsert_sessions(index_conn, [original])
    _approve(index_conn)
    before = index_conn.execute(
        "SELECT content_revision, updated_at FROM sessions WHERE session_id = 'trace-1'"
    ).fetchone()

    enriched = deepcopy(original)
    enriched["project"] = "renamed-science-project"
    enriched["model"] = "gpt-5-enriched"
    enriched["segment_title"] = "Updated parser title"
    enriched["stats"]["input_tokens"] = 123
    stats: dict[str, int] = {}
    upsert_sessions(index_conn, [enriched], stats=stats)

    row = index_conn.execute(
        "SELECT project, model, display_title, review_status, content_revision, "
        "updated_at, blob_path FROM sessions WHERE session_id = 'trace-1'"
    ).fetchone()
    assert stats == {"inserted": 0, "updated": 0, "unchanged": 1}
    assert row["project"] == "renamed-science-project"
    assert row["model"] == "gpt-5-enriched"
    assert row["display_title"] == "Updated parser title"
    assert row["review_status"] == "approved"
    assert row["content_revision"] == before["content_revision"]
    assert row["updated_at"] == before["updated_at"]
    with open(row["blob_path"]) as f:
        blob = json.load(f)
    assert blob["project"] == "renamed-science-project"
    fts_title = index_conn.execute(
        "SELECT display_title FROM sessions_fts WHERE session_id = 'trace-1'"
    ).fetchone()[0]
    assert fts_title == "Updated parser title"


def test_revision_aware_eligibility_requires_fresh_review(index_conn):
    upsert_sessions(index_conn, [_session()])
    _approve(index_conn)
    first_revision = index_conn.execute(
        "SELECT content_revision FROM sessions WHERE session_id = 'trace-1'"
    ).fetchone()[0]
    first_share = create_share(index_conn, ["trace-1"])
    _mark_shared(index_conn, first_share, "2026-07-02T00:00:00+00:00")

    assert get_share_ready_stats(index_conn)["sessions"] == []
    assert already_shared_revision_blockers(index_conn, ["trace-1"]) == [{
        "session_id": "trace-1",
        "revision_hash": first_revision,
        "last_shared_revision_hash": first_revision,
    }]

    upsert_sessions(index_conn, [_session(content="extended result")])
    assert revision_review_blockers(index_conn, ["trace-1"])[0]["review_status"] == "new"
    assert get_share_ready_stats(index_conn, include_unapproved=True)["sessions"] == []

    _approve(index_conn)
    ready = get_share_ready_stats(index_conn)["sessions"]
    assert len(ready) == 1
    assert ready[0]["revision_hash"] != first_revision
    assert ready[0]["last_shared_revision_hash"] == first_revision
    assert ready[0]["updated_since_last_share"] is True


def test_create_share_expected_revisions_are_exact_and_atomic(index_conn):
    upsert_sessions(index_conn, [_session(), _session("trace-2")])
    revisions = {
        row["session_id"]: row["content_revision"]
        for row in index_conn.execute(
            "SELECT session_id, content_revision FROM sessions ORDER BY session_id"
        )
    }

    with pytest.raises(RevisionConflictError) as exc_info:
        create_share(
            index_conn,
            ["trace-1", "trace-2"],
            expected_revisions={"trace-1": revisions["trace-1"]},
        )
    assert exc_info.value.blockers == [{
        "session_id": "trace-2",
        "expected_revision_hash": None,
        "current_revision_hash": revisions["trace-2"],
    }]
    assert index_conn.execute("SELECT COUNT(*) FROM shares").fetchone()[0] == 0

    share_id = create_share(
        index_conn,
        ["trace-1", "trace-2"],
        expected_revisions=revisions,
    )
    snapshots = index_conn.execute(
        "SELECT session_id, content_revision FROM share_sessions "
        "WHERE share_id = ? ORDER BY session_id",
        (share_id,),
    ).fetchall()
    assert {row["session_id"]: row["content_revision"] for row in snapshots} == revisions


def test_export_uses_snapshot_and_stale_reexport_preserves_artifact(index_conn):
    upsert_sessions(index_conn, [_session()])
    _approve(index_conn)
    first_share_id = create_share(index_conn, ["trace-1"])
    first_share = get_share(index_conn, first_share_id)
    export_dir, manifest = export_share_to_disk(index_conn, first_share_id, first_share)
    assert manifest.get("blocked") is not True
    first_revision = manifest["sessions"][0]["revision_hash"]
    assert manifest["sessions"][0]["replaces_revision_hash"] is None
    payload = json.loads((export_dir / "sessions.jsonl").read_text().splitlines()[0])
    assert payload["revision_hash"] == first_revision
    assert payload["replaces_revision_hash"] is None
    sessions_bytes = (export_dir / "sessions.jsonl").read_bytes()
    manifest_bytes = (export_dir / "manifest.json").read_bytes()
    persisted_manifest = index_conn.execute(
        "SELECT manifest FROM shares WHERE share_id = ?", (first_share_id,)
    ).fetchone()[0]
    _mark_shared(index_conn, first_share_id, "2026-07-02T00:00:00+00:00")

    upsert_sessions(index_conn, [_session(content="new unreviewed revision")])
    blockers = share_revision_blockers(index_conn, first_share_id)
    assert len(blockers) == 1
    _, blocked = export_share_to_disk(
        index_conn,
        first_share_id,
        get_share(index_conn, first_share_id),
    )
    assert blocked["block_reason"] == "revision_conflict"
    assert (export_dir / "sessions.jsonl").read_bytes() == sessions_bytes
    assert (export_dir / "manifest.json").read_bytes() == manifest_bytes
    assert index_conn.execute(
        "SELECT manifest FROM shares WHERE share_id = ?", (first_share_id,)
    ).fetchone()[0] == persisted_manifest


def test_finalized_bundle_stays_pinned_after_append_and_cache_miss(
    index_conn, monkeypatch
):
    from clawjournal.redaction import trufflehog
    from clawjournal.workbench import index as index_module
    from clawjournal.workbench.daemon import _prepare_share_export_for_upload

    monkeypatch.setattr(
        "clawjournal.workbench.daemon.CONFIG_DIR", index_module.CONFIG_DIR
    )
    monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(trufflehog, "is_available", lambda: True)
    monkeypatch.setattr(
        trufflehog,
        "scan_file",
        lambda path: trufflehog.TruffleHogReport(
            scanned_path=str(path),
            scanned_sha256="sha256:clean",
        ),
    )
    monkeypatch.setattr(trufflehog, "_scan_text_for_raw_matches", lambda text: [])

    upsert_sessions(index_conn, [_session(content="reviewed revision")])
    _approve(index_conn)
    share_id = create_share(index_conn, ["trace-1"])
    settings = {
        "custom_strings": [],
        "extra_usernames": [],
        "excluded_projects": [],
        "blocked_domains": [],
        "allowlist_entries": [],
        "source_filter": None,
        "ai_pii_review_enabled": False,
    }
    export_dir, _manifest, error = _prepare_share_export_for_upload(
        index_conn,
        share_id,
        get_share(index_conn, share_id),
        settings,
        reuse_finalized=True,
    )
    assert error is None
    pinned_sessions = (export_dir / "sessions.jsonl").read_bytes()
    pinned_manifest = (export_dir / "manifest.json").read_bytes()

    upsert_sessions(index_conn, [_session(content="newer local revision")])

    # Same settings reuse the finalized point-in-time artifact even though the
    # live trace has moved on.
    reused_dir, _reused_manifest, reused_error = _prepare_share_export_for_upload(
        index_conn,
        share_id,
        get_share(index_conn, share_id),
        settings,
        reuse_finalized=True,
    )
    assert reused_error is None
    assert reused_dir == export_dir
    assert (export_dir / "sessions.jsonl").read_bytes() == pinned_sessions

    # A settings change invalidates the cache, but the attempted rebuild must
    # fail closed without destroying the reviewed old artifact.
    changed_settings = {**settings, "custom_strings": ["new local rule"]}
    _blocked_dir, _blocked_manifest, blocked_error = _prepare_share_export_for_upload(
        index_conn,
        share_id,
        get_share(index_conn, share_id),
        changed_settings,
        reuse_finalized=True,
    )
    assert blocked_error["status"] == 409
    assert blocked_error["block_reason"] == "revision_conflict"
    assert (export_dir / "sessions.jsonl").read_bytes() == pinned_sessions
    assert (export_dir / "manifest.json").read_bytes() == pinned_manifest


def test_share_predecessor_detects_newer_successful_revision(index_conn):
    upsert_sessions(index_conn, [_session(content="r1")])
    _approve(index_conn)
    share_r1 = create_share(index_conn, ["trace-1"])
    _mark_shared(index_conn, share_r1, "2026-07-02T00:00:00+00:00")
    revision_r1 = index_conn.execute(
        "SELECT content_revision FROM sessions WHERE session_id = 'trace-1'"
    ).fetchone()[0]

    upsert_sessions(index_conn, [_session(content="r2")])
    _approve(index_conn)
    share_r2 = create_share(index_conn, ["trace-1"])
    snapshot_r2 = index_conn.execute(
        "SELECT content_revision, replaces_revision FROM share_sessions "
        "WHERE share_id = ?",
        (share_r2,),
    ).fetchone()
    assert snapshot_r2["replaces_revision"] == revision_r1

    upsert_sessions(index_conn, [_session(content="r3")])
    _approve(index_conn)
    share_r3 = create_share(index_conn, ["trace-1"])
    _mark_shared(index_conn, share_r3, "2026-07-03T00:00:00+00:00")
    revision_r3 = index_conn.execute(
        "SELECT content_revision FROM sessions WHERE session_id = 'trace-1'"
    ).fetchone()[0]

    assert share_predecessor_blockers(index_conn, share_r2) == [{
        "session_id": "trace-1",
        "revision_hash": snapshot_r2["content_revision"],
        "replaces_revision_hash": revision_r1,
        "latest_shared_revision_hash": revision_r3,
        "reason": "stale_predecessor",
    }]


def test_share_predecessor_blocks_same_revision_uploaded_by_another_share(index_conn):
    upsert_sessions(index_conn, [_session(content="r1")])
    _approve(index_conn)
    first_draft = create_share(index_conn, ["trace-1"])
    second_draft = create_share(index_conn, ["trace-1"])
    revision = index_conn.execute(
        "SELECT content_revision FROM sessions WHERE session_id = 'trace-1'"
    ).fetchone()[0]

    _mark_shared(index_conn, first_draft, "2026-07-02T00:00:00+00:00")

    assert share_predecessor_blockers(index_conn, second_draft) == [{
        "session_id": "trace-1",
        "revision_hash": revision,
        "replaces_revision_hash": None,
        "latest_shared_revision_hash": revision,
        "reason": "already_shared_revision",
    }]

    duplicate_after_upload = create_share(index_conn, ["trace-1"])
    duplicate_snapshot = index_conn.execute(
        "SELECT replaces_revision FROM share_sessions WHERE share_id = ?",
        (duplicate_after_upload,),
    ).fetchone()[0]
    assert duplicate_snapshot == revision
    assert share_predecessor_blockers(index_conn, duplicate_after_upload)[0][
        "reason"
    ] == "already_shared_revision"


def test_v5_migration_backfills_blob_and_historical_share_baseline(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "index.db"
    blobs_path = tmp_path / "blobs"
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", db_path)
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", blobs_path)
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
    conn = open_index()
    upsert_sessions(conn, [_session()])
    _approve(conn)
    share_id = create_share(conn, ["trace-1"])
    _mark_shared(conn, share_id, "2026-07-02T00:00:00+00:00")
    expected = compute_content_revision(_session())
    conn.close()

    raw = sqlite3.connect(db_path)
    raw.execute("ALTER TABLE sessions DROP COLUMN content_revision")
    raw.execute("ALTER TABLE share_sessions DROP COLUMN content_revision")
    raw.execute("ALTER TABLE share_sessions DROP COLUMN replaces_revision")
    raw.execute("PRAGMA user_version = 5")
    raw.commit()
    raw.close()

    conn = open_index()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == WORKBENCH_SCHEMA_VERSION
        session_revision = conn.execute(
            "SELECT content_revision FROM sessions WHERE session_id = 'trace-1'"
        ).fetchone()[0]
        share_revision = conn.execute(
            "SELECT content_revision FROM share_sessions WHERE share_id = ?",
            (share_id,),
        ).fetchone()[0]
        assert session_revision == share_revision == expected
        assert get_share_ready_stats(conn)["sessions"] == []
    finally:
        conn.close()


def test_v5_migration_missing_blob_uses_safe_legacy_baseline(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    blobs_path = tmp_path / "blobs"
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", db_path)
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", blobs_path)
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
    conn = open_index()
    upsert_sessions(conn, [_session()])
    _approve(conn)
    share_id = create_share(conn, ["trace-1"])
    _mark_shared(conn, share_id, "2026-07-02T00:00:00+00:00")
    conn.close()
    (blobs_path / "trace-1.json").unlink()

    raw = sqlite3.connect(db_path)
    raw.execute("ALTER TABLE sessions DROP COLUMN content_revision")
    raw.execute("ALTER TABLE share_sessions DROP COLUMN content_revision")
    raw.execute("ALTER TABLE share_sessions DROP COLUMN replaces_revision")
    raw.execute("PRAGMA user_version = 5")
    raw.commit()
    raw.close()

    conn = open_index()
    try:
        session_revision = conn.execute(
            "SELECT content_revision FROM sessions WHERE session_id = 'trace-1'"
        ).fetchone()[0]
        share_revision = conn.execute(
            "SELECT content_revision FROM share_sessions WHERE share_id = ?",
            (share_id,),
        ).fetchone()[0]
        assert session_revision.startswith("legacy:")
        assert share_revision == session_revision
        assert get_share_ready_stats(conn)["sessions"] == []
    finally:
        conn.close()
