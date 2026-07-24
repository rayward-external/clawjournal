"""Local persistence and candidate selection for automatic daily uploads."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clawjournal.workbench.index import (
    EXACT_SCOPE_PAIRS_SCHEMA_VERSION,
    RECURRING_PROTOCOL_V2_SCHEMA_VERSION,
    WORKBENCH_SCHEMA_VERSION,
    already_shared_revision_blockers,
    create_share,
    get_auto_upload_candidate_report,
    get_auto_upload_enrollment,
    open_index,
    revision_review_blockers,
    save_auto_upload_enrollment,
    set_hold_state,
    update_auto_upload_enrollment,
    upsert_sessions,
)


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
ENROLLED_AT = "2026-07-10T12:00:00+00:00"


@pytest.fixture
def index_conn(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
    conn = open_index()
    yield conn
    conn.close()


def _session(
    session_id: str,
    *,
    source: str = "claude",
    project: str = "project-one",
    end_time: str = "2026-07-12T10:00:00+00:00",
    content: str | None = None,
    raw_source_path: str | None = None,
) -> dict:
    return {
        "session_id": session_id,
        "project": project,
        "source": source,
        "model": "test-model",
        "start_time": "2026-07-12T09:00:00+00:00",
        "end_time": end_time,
        "raw_source_path": __file__ if raw_source_path is None else raw_source_path,
        "messages": [
            {"role": "user", "content": content or session_id, "tool_uses": []},
            {"role": "assistant", "content": "done", "tool_uses": []},
        ],
        "stats": {
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_uses": 0,
            "input_tokens": 10,
            "output_tokens": 3,
        },
    }


def _enroll(
    conn,
    *,
    sources=("claude",),
    projects=("project-one",),
    scope_entries=None,
):
    return save_auto_upload_enrollment(
        conn,
        mode="enabled",
        health="ready",
        generation=1,
        enrolled_at=ENROLLED_AT,
        client_enrollment_id="client-enrollment-1",
        enrolled_sources=sources,
        enrolled_projects=projects,
        enrolled_scope_entries=scope_entries,
        server_enrollment_id="server-enrollment-1",
        authorization_revision=1,
        recurring_authorization_version="recurring-v1",
        retention_version="retention-v1",
        egress_profile_hash="profile-hash",
        hook_targets=("codex", "claude"),
    )


def _mark_shared(conn, session_id: str) -> str:
    share_id = create_share(conn, [session_id])
    conn.execute(
        "UPDATE shares SET status = 'shared', shared_at = ? WHERE share_id = ?",
        ("2026-07-13T00:00:00+00:00", share_id),
    )
    conn.commit()
    return share_id


def test_fresh_schema_has_auto_upload_foundation(index_conn):
    assert index_conn.execute("PRAGMA user_version").fetchone()[0] == WORKBENCH_SCHEMA_VERSION
    assert WORKBENCH_SCHEMA_VERSION == EXACT_SCOPE_PAIRS_SCHEMA_VERSION
    assert EXACT_SCOPE_PAIRS_SCHEMA_VERSION == 10
    assert RECURRING_PROTOCOL_V2_SCHEMA_VERSION == 9

    session_columns = {
        row[1] for row in index_conn.execute("PRAGMA table_info(sessions)")
    }
    assert "revision_stable_since" in session_columns

    share_columns = {
        row[1] for row in index_conn.execute("PRAGMA table_info(shares)")
    }
    assert {
        "submission_channel",
        "enrollment_id",
        "client_submission_id",
        "authorization_revision",
        "submission_state",
        "sealed_artifact_sha256",
        "sealed_artifact_path",
        "sealed_raw_fingerprints",
    } <= share_columns
    table = index_conn.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' "
        "AND name = 'auto_upload_enrollment'"
    ).fetchone()
    assert table is not None
    enrollment_columns = {
        row[1] for row in index_conn.execute("PRAGMA table_info(auto_upload_enrollment)")
    }
    assert {
        "claude_hook_observed_at",
        "codex_hook_observed_at",
        "ownership_certification_version",
        "server_scope_hash",
        "enrolled_scope_entries_json",
    } <= enrollment_columns
    indexes = {
        row[1] for row in index_conn.execute("PRAGMA index_list(shares)")
    }
    assert "idx_shares_client_submission_id" in indexes


def test_v6_migration_backfills_stability_and_adds_share_ledger_fields(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "index.db"
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", db_path)
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
    conn = open_index()
    upsert_sessions(conn, [_session("legacy")])
    conn.close()

    raw = sqlite3.connect(db_path)
    raw.execute("DROP INDEX idx_shares_client_submission_id")
    raw.execute("DROP TABLE auto_upload_enrollment")
    raw.execute("ALTER TABLE sessions DROP COLUMN revision_stable_since")
    for column in (
        "submission_channel",
        "enrollment_id",
        "client_submission_id",
        "authorization_revision",
        "submission_state",
        "sealed_artifact_sha256",
        "sealed_artifact_path",
        "sealed_raw_fingerprints",
    ):
        raw.execute(f"ALTER TABLE shares DROP COLUMN {column}")
    raw.execute("PRAGMA user_version = 6")
    raw.commit()
    raw.close()

    migration_time = "2026-07-14T12:34:56+00:00"
    monkeypatch.setattr("clawjournal.workbench.index._now_iso", lambda: migration_time)
    conn = open_index()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == WORKBENCH_SCHEMA_VERSION
        stable_since = conn.execute(
            "SELECT revision_stable_since FROM sessions WHERE session_id = 'legacy'"
        ).fetchone()[0]
        assert stable_since == migration_time
        assert get_auto_upload_enrollment(conn) is None
        share_columns = {row[1] for row in conn.execute("PRAGMA table_info(shares)")}
        assert "client_submission_id" in share_columns
        assert "sealed_artifact_sha256" in share_columns
        assert "sealed_raw_fingerprints" in share_columns
    finally:
        conn.close()


def test_v7_migration_adds_recovery_metadata_once(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", db_path)
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)

    conn = open_index()
    _enroll(conn)
    upsert_sessions(conn, [_session("migration-share")])
    share_id = create_share(conn, ["migration-share"])
    conn.close()

    raw = sqlite3.connect(db_path)
    raw.execute("ALTER TABLE auto_upload_enrollment DROP COLUMN claude_hook_observed_at")
    raw.execute("ALTER TABLE auto_upload_enrollment DROP COLUMN codex_hook_observed_at")
    raw.execute("ALTER TABLE shares DROP COLUMN sealed_raw_fingerprints")
    raw.execute("PRAGMA user_version = 7")
    raw.commit()
    raw.close()

    conn = open_index()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == WORKBENCH_SCHEMA_VERSION
        enrollment_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(auto_upload_enrollment)")
        }
        assert {"claude_hook_observed_at", "codex_hook_observed_at"} <= enrollment_columns
        share_columns = {row[1] for row in conn.execute("PRAGMA table_info(shares)")}
        assert "sealed_raw_fingerprints" in share_columns
        assert get_auto_upload_enrollment(conn)["client_enrollment_id"] == "client-enrollment-1"

        observed_at = "2026-07-14T13:00:00+00:00"
        assert update_auto_upload_enrollment(
            conn,
            claude_hook_observed_at=observed_at,
            codex_hook_observed_at=observed_at,
        )
        fingerprints = '{"migration-share":[1,2,3,4]}'
        conn.execute(
            "UPDATE shares SET sealed_raw_fingerprints = ? WHERE share_id = ?",
            (fingerprints, share_id),
        )
        conn.commit()
    finally:
        conn.close()

    # A second open is gated at v10 and must preserve the newly stored values.
    conn = open_index()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == WORKBENCH_SCHEMA_VERSION
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["claude_hook_observed_at"] == observed_at
        assert enrollment["codex_hook_observed_at"] == observed_at
        assert conn.execute(
            "SELECT sealed_raw_fingerprints FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()[0] == fingerprints
    finally:
        conn.close()


def test_v9_migration_preserves_existing_cross_product_scope(tmp_path, monkeypatch):
    db_path = tmp_path / "index.db"
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", db_path)
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)

    conn = open_index()
    _enroll(
        conn,
        sources=("claude", "codex"),
        projects=("alpha", "beta"),
    )
    conn.close()

    raw = sqlite3.connect(db_path)
    raw.execute(
        "ALTER TABLE auto_upload_enrollment "
        "DROP COLUMN enrolled_scope_entries_json"
    )
    raw.execute(f"PRAGMA user_version = {RECURRING_PROTOCOL_V2_SCHEMA_VERSION}")
    raw.commit()
    raw.close()

    readonly = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    readonly.row_factory = sqlite3.Row
    try:
        assert get_auto_upload_enrollment(readonly)["enrolled_scope_entries"] == [
            ("claude", "alpha"),
            ("claude", "beta"),
            ("codex", "alpha"),
            ("codex", "beta"),
        ]
    finally:
        readonly.close()

    conn = open_index()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == WORKBENCH_SCHEMA_VERSION
        enrollment = get_auto_upload_enrollment(conn)
        assert enrollment["enrolled_scope_entries"] == [
            ("claude", "alpha"),
            ("claude", "beta"),
            ("codex", "alpha"),
            ("codex", "beta"),
        ]
    finally:
        conn.close()


def test_revision_stability_clock_resets_only_when_content_changes(index_conn, monkeypatch):
    clock = iter((
        "2026-07-11T00:00:00+00:00",
        "2026-07-12T00:00:00+00:00",
        "2026-07-13T00:00:00+00:00",
    ))
    monkeypatch.setattr("clawjournal.workbench.index._now_iso", lambda: next(clock))

    original = _session("stable-clock", content="first")
    upsert_sessions(index_conn, [original])
    first = index_conn.execute(
        "SELECT revision_stable_since FROM sessions WHERE session_id = 'stable-clock'"
    ).fetchone()[0]
    assert first == "2026-07-11T00:00:00+00:00"

    upsert_sessions(index_conn, [dict(original, model="metadata-only")])
    unchanged = index_conn.execute(
        "SELECT revision_stable_since FROM sessions WHERE session_id = 'stable-clock'"
    ).fetchone()[0]
    assert unchanged == first

    changed = _session("stable-clock", content="appended content")
    upsert_sessions(index_conn, [changed])
    reset = index_conn.execute(
        "SELECT revision_stable_since FROM sessions WHERE session_id = 'stable-clock'"
    ).fetchone()[0]
    assert reset == "2026-07-13T00:00:00+00:00"


def test_enrollment_helpers_canonicalize_scope_and_support_generation_cas(index_conn):
    enrollment = _enroll(
        index_conn,
        sources=("codex", "claude", "codex"),
        projects=("project-two", "project-one", "project-one"),
        scope_entries=(
            ("codex", "project-two"),
            ("claude", "project-one"),
            ("codex", "project-two"),
        ),
    )
    assert enrollment["enrolled_sources"] == ["claude", "codex"]
    assert enrollment["enrolled_projects"] == ["project-one", "project-two"]
    assert enrollment["enrolled_scope_entries"] == [
        ("claude", "project-one"),
        ("codex", "project-two"),
    ]
    assert enrollment["hook_targets"] == ["claude", "codex"]
    assert enrollment["revocation_pending"] is False
    assert index_conn.execute(
        "SELECT COUNT(*) FROM auto_upload_enrollment"
    ).fetchone()[0] == 1

    assert not update_auto_upload_enrollment(
        index_conn,
        expected_generation=2,
        mode="paused",
        generation=2,
    )
    assert get_auto_upload_enrollment(index_conn)["mode"] == "enabled"

    assert update_auto_upload_enrollment(
        index_conn,
        expected_generation=1,
        mode="paused",
        health="action_required",
        generation=2,
        revocation_pending=True,
    )
    updated = get_auto_upload_enrollment(index_conn)
    assert updated["mode"] == "paused"
    assert updated["generation"] == 2
    assert updated["revocation_pending"] is True


def test_candidate_report_orders_stored_scores_and_caps_at_five(index_conn):
    _enroll(
        index_conn,
        sources=("claude", "codex"),
        projects=("project-one",),
    )
    sessions = [
        _session("score-5-old", end_time="2026-07-12T01:00:00+00:00"),
        _session("score-5-new", end_time="2026-07-12T02:00:00+00:00"),
        _session("score-3", end_time="2026-07-12T03:00:00+00:00"),
        _session("score-1", end_time="2026-07-12T04:00:00+00:00"),
        _session("stable", source="codex", end_time="2026-07-12T05:00:00+00:00"),
        _session("null-new", end_time="2026-07-12T06:00:00+00:00"),
        _session("null-old", end_time="2026-07-12T00:30:00+00:00"),
    ]
    upsert_sessions(index_conn, sessions)
    scores = {
        "score-5-old": 5,
        "score-5-new": 5,
        "score-3": 3,
        "score-1": 1,
        "stable": 2,
    }
    for session_id, score in scores.items():
        index_conn.execute(
            "UPDATE sessions SET ai_failure_value_score = ? WHERE session_id = ?",
            (score, session_id),
        )
    index_conn.execute(
        "UPDATE sessions SET revision_stable_since = ? WHERE session_id = 'stable'",
        ("2026-07-12T05:00:00+00:00",),
    )
    index_conn.commit()

    report = get_auto_upload_candidate_report(
        index_conn,
        current_sources=("claude", "codex"),
        current_projects=("project-one",),
        source_confirmed=True,
        projects_confirmed=True,
        completion_modes={"claude": "explicit_close", "codex": "stable_revision"},
        now=NOW,
    )

    assert [row["session_id"] for row in report["eligible"]] == [
        "score-5-new",
        "score-5-old",
        "score-3",
        "stable",
        "score-1",
        "null-new",
        "null-old",
    ]
    assert [row["session_id"] for row in report["selected"]] == [
        "score-5-new",
        "score-5-old",
        "score-3",
        "stable",
        "score-1",
    ]
    assert report["eligible_count"] == 7
    assert report["selected_count"] == 5
    assert report["deferred_by_cap"] == 2


def test_candidate_report_explains_each_safety_exclusion(index_conn):
    _enroll(
        index_conn,
        sources=("claude", "codex"),
        projects=("project-one",),
    )
    sessions = [
        _session("eligible"),
        _session("pre", end_time="2026-07-09T12:00:00+00:00"),
        _session("unsettled", source="codex"),
        _session("held"),
        _session("blocked"),
        _session("already"),
        _session("changed", content="first revision"),
        _session("source-out", source="opencode"),
        _session("project-out", project="project-two"),
        _session("missing-blob"),
        _session("missing-raw", raw_source_path=""),
    ]
    upsert_sessions(index_conn, sessions)
    set_hold_state(index_conn, "held", "pending_review", changed_by="user")
    index_conn.execute(
        "UPDATE sessions SET review_status = 'blocked' WHERE session_id = 'blocked'"
    )
    index_conn.commit()
    _mark_shared(index_conn, "already")
    _mark_shared(index_conn, "changed")
    upsert_sessions(index_conn, [_session("changed", content="second revision")])
    missing_path = index_conn.execute(
        "SELECT blob_path FROM sessions WHERE session_id = 'missing-blob'"
    ).fetchone()[0]
    Path(missing_path).unlink()

    report = get_auto_upload_candidate_report(
        index_conn,
        current_sources=("claude", "codex", "opencode"),
        current_projects=("project-one", "project-two"),
        source_confirmed=True,
        projects_confirmed=True,
        completion_modes={"claude": "explicit_close", "codex": "stable_revision"},
        now=NOW,
    )

    assert [row["session_id"] for row in report["selected"]] == ["eligible"]
    assert report["exclusion_counts"] == {
        "pre_enrollment": 1,
        "unsupported_unsettled": 1,
        "held_or_embargoed": 1,
        "blocked_review_status": 1,
        "changed_revision_needing_approval": 1,
        "already_shared": 1,
        "source_excluded": 1,
        "project_excluded": 1,
        "scope_pair_excluded": 0,
        "missing_blob": 1,
        "raw_source_unavailable": 1,
        "scope_confirmation_changed": 0,
    }
    assert {item["session_id"]: item["reason"] for item in report["exclusions"]} == {
        "pre": "pre_enrollment",
        "unsettled": "unsupported_unsettled",
        "held": "held_or_embargoed",
        "blocked": "blocked_review_status",
        "changed": "changed_revision_needing_approval",
        "already": "already_shared",
        "source-out": "source_excluded",
        "project-out": "project_excluded",
        "missing-blob": "missing_blob",
        "missing-raw": "raw_source_unavailable",
    }


def test_candidate_report_uses_cheap_blob_presence_not_full_parse(
    index_conn, monkeypatch
):
    # The report is polled frequently (status/preview); it must not full-parse
    # every eligible session's blob. A present blob should pass the missing_blob
    # gate via a cheap existence check, never via _read_blob_for_revision.
    from clawjournal.workbench import index as index_module

    _enroll(index_conn, sources=("claude",), projects=("project-one",))
    upsert_sessions(index_conn, [_session("eligible")])

    def forbidden(*_args, **_kwargs):
        raise AssertionError("candidate report must not full-parse blobs")

    monkeypatch.setattr(index_module, "_read_blob_for_revision", forbidden)

    report = get_auto_upload_candidate_report(
        index_conn,
        current_sources=("claude",),
        current_projects=("project-one",),
        source_confirmed=True,
        projects_confirmed=True,
        completion_modes={"claude": "explicit_close"},
        now=NOW,
    )

    assert [row["session_id"] for row in report["selected"]] == ["eligible"]
    assert report["exclusion_counts"]["missing_blob"] == 0


def test_candidate_report_rejects_unenrolled_source_project_combination(index_conn):
    _enroll(
        index_conn,
        sources=("claude", "codex"),
        projects=("alpha", "beta"),
        scope_entries=(("claude", "alpha"), ("codex", "beta")),
    )
    upsert_sessions(
        index_conn,
        [
            _session("authorized", source="claude", project="alpha"),
            _session("cross-product-only", source="claude", project="beta"),
        ],
    )

    report = get_auto_upload_candidate_report(
        index_conn,
        current_sources=("claude", "codex"),
        current_projects=("alpha", "beta"),
        source_confirmed=True,
        projects_confirmed=True,
        completion_modes={"claude": "explicit_close", "codex": "stable_revision"},
        now=NOW,
    )

    assert [row["session_id"] for row in report["selected"]] == ["authorized"]
    assert report["exclusion_counts"]["scope_pair_excluded"] == 1
    assert {
        item["session_id"]: item["reason"] for item in report["exclusions"]
    }["cross-product-only"] == "scope_pair_excluded"


def test_candidate_excludes_any_previously_shared_exact_revision(index_conn):
    _enroll(index_conn)
    upsert_sessions(index_conn, [_session("reverted", content="revision-a")])
    revision_a = index_conn.execute(
        "SELECT content_revision FROM sessions WHERE session_id = 'reverted'"
    ).fetchone()[0]
    _mark_shared(index_conn, "reverted")

    upsert_sessions(index_conn, [_session("reverted", content="revision-b")])
    revision_b = index_conn.execute(
        "SELECT content_revision FROM sessions WHERE session_id = 'reverted'"
    ).fetchone()[0]
    assert revision_b != revision_a
    _mark_shared(index_conn, "reverted")

    # Reverting to A must remain a duplicate even though B is the latest
    # successful predecessor and therefore controls fresh-review metadata.
    upsert_sessions(index_conn, [_session("reverted", content="revision-a")])
    review_blocker = revision_review_blockers(index_conn, ["reverted"])[0]
    assert review_blocker["revision_hash"] == revision_a
    assert review_blocker["last_shared_revision_hash"] == revision_b
    assert already_shared_revision_blockers(index_conn, ["reverted"]) == [{
        "session_id": "reverted",
        "revision_hash": revision_a,
        "last_shared_revision_hash": revision_b,
    }]

    report = get_auto_upload_candidate_report(
        index_conn,
        current_sources=("claude",),
        current_projects=("project-one",),
        source_confirmed=True,
        projects_confirmed=True,
        completion_modes={"claude": "explicit_close"},
        now=NOW,
    )

    assert report["selected"] == []
    assert report["exclusion_counts"]["already_shared"] == 1
    assert report["exclusions"] == [
        {"session_id": "reverted", "reason": "already_shared"}
    ]


def test_candidate_report_fails_closed_when_enrolled_scope_loses_confirmation(index_conn):
    _enroll(index_conn)
    upsert_sessions(index_conn, [_session("candidate")])

    report = get_auto_upload_candidate_report(
        index_conn,
        current_sources=("claude",),
        current_projects=(),
        source_confirmed=True,
        projects_confirmed=False,
        completion_modes={"claude": "explicit_close"},
        now=NOW,
    )

    assert report["selected"] == []
    assert report["scope_blockers"] == [
        "project_confirmation_missing",
        "enrolled_project_removed",
    ]
    assert report["exclusion_counts"]["scope_confirmation_changed"] == 1
