"""Tests for the workbench SQLite index."""

import json
import sqlite3

import pytest

from clawjournal.workbench.index import (
    _migrate_bundles_to_shares,
    add_policy,
    backfill_session_keys,
    create_share,
    get_effective_share_settings,
    get_dashboard_analytics,
    get_share,
    get_shares,
    get_policies,
    get_share_ready_stats,
    get_session_detail,
    get_stats,
    link_subagent_hierarchy,
    open_index,
    query_sessions,
    query_sessions_for_rescore,
    query_unscored_sessions,
    recompute_estimated_costs,
    remove_policy,
    search_fts,
    session_matches_excluded_projects,
    source_scope_blockers,
    set_hold_state,
    update_session,
    upsert_sessions,
)


@pytest.fixture
def index_conn(tmp_path, monkeypatch):
    """Open an index DB in a temp directory."""
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    conn = open_index()
    yield conn
    conn.close()


def _make_session(session_id="sess-1", project="test-project", source="claude",
                  model="claude-sonnet-4", content="Fix the login bug",
                  start_time="2025-01-01T00:00:00+00:00",
                  end_time="2025-01-01T00:10:00+00:00"):
    return {
        "session_id": session_id,
        "project": project,
        "source": source,
        "model": model,
        "start_time": start_time,
        "end_time": end_time,
        "git_branch": "main",
        "messages": [
            {"role": "user", "content": content, "tool_uses": []},
            {"role": "assistant", "content": "I'll fix it.", "tool_uses": [
                {"tool": "bash", "input": {"command": "pytest"}, "output": "1 passed", "status": "success"},
            ]},
        ],
        "stats": {
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_uses": 1,
            "input_tokens": 500,
            "output_tokens": 100,
        },
    }


class TestUpsertSessions:
    def test_insert_new_session(self, index_conn):
        sessions = [_make_session()]
        new_count = upsert_sessions(index_conn, sessions)
        assert new_count == 1

    def test_insert_multiple_sessions(self, index_conn):
        sessions = [
            _make_session("s1", content="First task"),
            _make_session("s2", content="Second task"),
        ]
        new_count = upsert_sessions(index_conn, sessions)
        assert new_count == 2

    @pytest.mark.parametrize("status", ["approved", "blocked"])
    def test_upsert_preserves_review_status(self, index_conn, status):
        upsert_sessions(index_conn, [_make_session()])
        update_session(index_conn, "sess-1", status=status)

        # Re-index same session
        upsert_sessions(index_conn, [_make_session()])

        row = index_conn.execute(
            "SELECT review_status FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["review_status"] == status

    def test_upsert_preserves_manual_review_metadata(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        update_session(
            index_conn,
            "sess-1",
            status="approved",
            notes="Keep this trace",
            reason="useful debugging arc",
        )
        reviewed_before = index_conn.execute(
            "SELECT reviewed_at FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()["reviewed_at"]

        upsert_sessions(index_conn, [_make_session()])

        row = index_conn.execute(
            "SELECT review_status, reviewer_notes, selection_reason, reviewed_at "
            "FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["review_status"] == "approved"
        assert row["reviewer_notes"] == "Keep this trace"
        assert row["selection_reason"] == "useful debugging arc"
        assert row["reviewed_at"] == reviewed_before

    def test_upsert_preserves_subagent_hierarchy_metadata(self, index_conn):
        upsert_sessions(index_conn, [_make_session("parent"), _make_session("child")])
        index_conn.execute(
            "UPDATE sessions SET subagent_session_ids = ? WHERE session_id = ?",
            (json.dumps(["child"]), "parent"),
        )
        index_conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE session_id = ?",
            ("parent", "child"),
        )
        index_conn.commit()

        upsert_sessions(index_conn, [_make_session("parent"), _make_session("child")])

        parent_row = index_conn.execute(
            "SELECT subagent_session_ids FROM sessions WHERE session_id = 'parent'"
        ).fetchone()
        child_row = index_conn.execute(
            "SELECT parent_session_id FROM sessions WHERE session_id = 'child'"
        ).fetchone()
        assert json.loads(parent_row["subagent_session_ids"]) == ["child"]
        assert child_row["parent_session_id"] == "parent"

    def test_upsert_preserves_estimated_cost_for_completed_session(self, index_conn, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.estimate_cost", lambda *_args, **_kwargs: 12.34)
        upsert_sessions(index_conn, [_make_session()])

        monkeypatch.setattr("clawjournal.workbench.index.estimate_cost", lambda *_args, **_kwargs: 56.78)
        upsert_sessions(index_conn, [_make_session()])

        row = index_conn.execute(
            "SELECT estimated_cost_usd FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["estimated_cost_usd"] == pytest.approx(12.34)

    def test_upsert_recomputes_estimated_cost_for_ongoing_session(self, index_conn, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.estimate_cost", lambda *_args, **_kwargs: 1.23)
        upsert_sessions(index_conn, [_make_session(end_time=None)])

        monkeypatch.setattr("clawjournal.workbench.index.estimate_cost", lambda *_args, **_kwargs: 4.56)
        upsert_sessions(index_conn, [_make_session(end_time=None)])

        row = index_conn.execute(
            "SELECT estimated_cost_usd FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["estimated_cost_usd"] == pytest.approx(4.56)

    def test_skips_session_without_id(self, index_conn):
        session = _make_session()
        del session["session_id"]
        assert upsert_sessions(index_conn, [session]) == 0


class TestCostAccounting:
    def test_recompute_estimated_costs_overrides_frozen(self, index_conn, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.workbench.index.estimate_cost", lambda *a, **k: 10.0)
        upsert_sessions(index_conn, [_make_session()])
        # Simulate a stale frozen cost left by an earlier (mis-)estimate.
        index_conn.execute(
            "UPDATE sessions SET estimated_cost_usd = 999.0 WHERE session_id = 'sess-1'")
        index_conn.commit()

        changed = recompute_estimated_costs(index_conn)
        assert changed == 1
        row = index_conn.execute(
            "SELECT estimated_cost_usd FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["estimated_cost_usd"] == pytest.approx(10.0)
        # Idempotent: a second pass changes nothing.
        assert recompute_estimated_costs(index_conn) == 0

    def test_dashboard_reports_priced_and_unpriced_counts(self, index_conn, monkeypatch):
        # Price only the "priced-model" session; the other returns None (unpriced).
        def fake_cost(model, *a, **k):
            return 5.0 if model == "priced-model" else None
        monkeypatch.setattr("clawjournal.workbench.index.estimate_cost", fake_cost)
        upsert_sessions(index_conn, [
            _make_session("sess-priced", model="priced-model"),
            _make_session("sess-unpriced", model="unpriced-model"),
        ])

        summary = get_dashboard_analytics(index_conn)["summary"]
        assert summary["total_sessions"] == 2
        assert summary["priced_sessions"] == 1
        assert summary["unpriced_sessions"] == 1
        assert summary["total_cost"] == pytest.approx(5.0)

    def test_empty_list(self, index_conn):
        assert upsert_sessions(index_conn, []) == 0

    def test_badges_computed(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        row = index_conn.execute(
            "SELECT outcome_badge, task_type, display_title FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["display_title"] == "Fix the login bug"
        assert row["outcome_badge"] is not None
        assert row["task_type"] is not None

    def test_display_title_redacts_secrets(self, index_conn):
        # The sessions row is a plaintext surface (API list views,
        # search results). A user prompt that happens to contain a
        # token must not leak verbatim into `display_title`.
        token = "ghp_abcdefghijklmnopqrstuvwxyzABCDEF0123"
        session = _make_session(content=f"deploy key: {token}")
        upsert_sessions(index_conn, [session])
        row = index_conn.execute(
            "SELECT display_title FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert token not in row["display_title"]
        assert "[REDACTED" in row["display_title"]

    def test_provenance_fields_stored(self, index_conn):
        session = _make_session()
        session["raw_source_path"] = "/path/to/session.jsonl"
        session["client_origin"] = "desktop"
        session["runtime_channel"] = "local-agent"
        session["outer_session_id"] = "local_abc123"
        upsert_sessions(index_conn, [session])

        row = index_conn.execute(
            "SELECT raw_source_path, client_origin, runtime_channel, outer_session_id "
            "FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["raw_source_path"] == "/path/to/session.jsonl"
        assert row["client_origin"] == "desktop"
        assert row["runtime_channel"] == "local-agent"
        assert row["outer_session_id"] == "local_abc123"

    def test_provenance_columns_nullable(self, index_conn):
        """Sessions without provenance fields should have NULL values."""
        upsert_sessions(index_conn, [_make_session()])
        row = index_conn.execute(
            "SELECT raw_source_path, session_key, client_origin, runtime_channel, outer_session_id "
            "FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["raw_source_path"] is None
        assert row["session_key"] is None
        assert row["client_origin"] is None
        assert row["runtime_channel"] is None
        assert row["outer_session_id"] is None

    @pytest.mark.parametrize(
        ("source", "raw_source_path", "expected"),
        [
            ("claude", "/tmp/claude/projects/demo-project/sess-1.jsonl", "claude:demo-project:sess-1"),
            ("claude", "/tmp/claude/projects/demo-project/subagent-only", "claude:demo-project:subagent-only"),
            # Real production native path: ~/.claude/projects/<proj>/<uuid>.jsonl.
            # `.claude` is in path.parts but no local-agent wrapper exists at
            # parents[3].json — must fall through to native stem derivation.
            ("claude", "/Users/alice/.claude/projects/demo-project/sess-1.jsonl", "claude:demo-project:sess-1"),
            ("codex", "/tmp/codex/sessions/2025/01/02/run.jsonl", "codex:/tmp/codex/sessions/2025/01/02/run.jsonl"),
            ("openclaw", "/tmp/openclaw/agents/a/sessions/demo.jsonl", "openclaw:/tmp/openclaw/agents/a/sessions/demo.jsonl"),
        ],
    )
    def test_upsert_derives_session_key_from_provenance(
        self, index_conn, source, raw_source_path, expected
    ):
        session = _make_session(source=source, project=f"{source}:demo")
        session["raw_source_path"] = raw_source_path
        upsert_sessions(index_conn, [session])

        row = index_conn.execute(
            "SELECT session_key FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["session_key"] == expected

    def test_backfill_session_keys_on_fresh_db_without_events_ingest(self, index_conn):
        """Calling backfill before any events ingest must not crash on missing
        `event_sessions` / `events` tables — it should ensure them and fall
        through to path derivation.
        """
        index_conn.execute(
            "INSERT INTO sessions (session_id, project, source, raw_source_path, indexed_at) "
            "VALUES ('legacy', 'p', 'claude', "
            "'/home/u/.claude/projects/p/uuid1.jsonl', '2026-01-01T00:00:00Z')"
        )
        index_conn.commit()

        updated = backfill_session_keys(index_conn)
        assert updated == 1

        row = index_conn.execute(
            "SELECT session_key FROM sessions WHERE session_id = 'legacy'"
        ).fetchone()
        assert row["session_key"] == "claude:p:uuid1"

    def test_upsert_derives_local_agent_session_key_from_wrapper(self, index_conn, tmp_path):
        workspace_dir = (
            tmp_path
            / "local_agent"
            / "11111111-2222-3333-4444-555555555555"
            / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        )
        workspace_dir.mkdir(parents=True)
        wrapper = workspace_dir / "local_demo.json"
        wrapper.write_text(
            json.dumps(
                {
                    "cliSessionId": "cli-42",
                    "sessionId": "sess-42",
                    "processName": "demo",
                    "userSelectedFolders": ["/Users/me/ws"],
                }
            )
        )
        transcript = wrapper.with_suffix("") / ".claude" / "projects" / "-sessions-demo" / "cli-42.jsonl"
        transcript.parent.mkdir(parents=True)
        transcript.write_text("{}\n")

        session = _make_session(project="claude:ws")
        session["raw_source_path"] = str(transcript)
        upsert_sessions(index_conn, [session])

        row = index_conn.execute(
            "SELECT session_key FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["session_key"] == "claude:-Users-me-ws:cli-42"

    def test_link_subagent_hierarchy_skips_conflicting_existing_parent(self, index_conn):
        parent_one = _make_session(
            "parent-1",
            project="proj",
            start_time="2025-01-01T00:00:00+00:00",
            end_time="2025-01-01T00:10:00+00:00",
        )
        parent_one["messages"].append({
            "role": "assistant",
            "tool": "Task",
            "input": {"description": "delegate child work"},
            "status": "success",
        })
        parent_two = _make_session(
            "parent-2",
            project="proj",
            start_time="2025-01-01T00:00:00+00:00",
            end_time="2025-01-01T00:10:00+00:00",
        )
        parent_two["messages"].append({
            "role": "assistant",
            "tool": "Task",
            "input": {"description": "also delegate child work"},
            "status": "success",
        })
        child = _make_session(
            "child",
            project="proj",
            start_time="2025-01-01T00:05:00+00:00",
            end_time="2025-01-01T00:06:00+00:00",
        )

        upsert_sessions(index_conn, [parent_one, parent_two, child])
        index_conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE session_id = ?",
            ("external-root-one", "parent-1"),
        )
        index_conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE session_id = ?",
            ("parent-1", "child"),
        )
        index_conn.execute(
            "UPDATE sessions SET parent_session_id = ? WHERE session_id = ?",
            ("external-parent", "parent-2"),
        )
        index_conn.commit()

        link_subagent_hierarchy(index_conn)

        parent_one_row = index_conn.execute(
            "SELECT subagent_session_ids FROM sessions WHERE session_id = 'parent-1'"
        ).fetchone()
        parent_two_row = index_conn.execute(
            "SELECT subagent_session_ids FROM sessions WHERE session_id = 'parent-2'"
        ).fetchone()
        child_row = index_conn.execute(
            "SELECT parent_session_id FROM sessions WHERE session_id = 'child'"
        ).fetchone()

        assert json.loads(parent_one_row["subagent_session_ids"]) == ["child"]
        assert parent_two_row["subagent_session_ids"] is None
        assert child_row["parent_session_id"] == "parent-1"


class TestQuerySessions:
    def test_query_all(self, index_conn):
        upsert_sessions(index_conn, [_make_session("s1"), _make_session("s2")])
        results = query_sessions(index_conn)
        assert len(results) == 2

    def test_filter_by_status(self, index_conn):
        upsert_sessions(index_conn, [_make_session("s1"), _make_session("s2")])
        update_session(index_conn, "s1", status="approved")

        results = query_sessions(index_conn, status="approved")
        assert len(results) == 1
        assert results[0]["session_id"] == "s1"

    def test_filter_by_multiple_statuses(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session("s1"),
            _make_session("s2"),
            _make_session("s3"),
        ])
        update_session(index_conn, "s1", status="shortlisted")
        update_session(index_conn, "s2", status="blocked")

        results = query_sessions(index_conn, status=["new", "shortlisted"])

        assert {row["session_id"] for row in results} == {"s1", "s3"}

    def test_filter_by_source(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session("s1", source="claude"),
            _make_session("s2", source="codex"),
        ])
        results = query_sessions(index_conn, source="codex")
        assert len(results) == 1
        assert results[0]["source"] == "codex"

    def test_limit_and_offset(self, index_conn):
        sessions = [_make_session(f"s{i}") for i in range(10)]
        upsert_sessions(index_conn, sessions)

        results = query_sessions(index_conn, limit=3, offset=0)
        assert len(results) == 3

        results2 = query_sessions(index_conn, limit=3, offset=3)
        assert len(results2) == 3
        assert results[0]["session_id"] != results2[0]["session_id"]


class TestGetSessionDetail:
    def test_returns_messages(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        detail = get_session_detail(index_conn, "sess-1")
        assert detail is not None
        assert len(detail["messages"]) == 2
        assert detail["messages"][0]["role"] == "user"

    def test_parses_failure_array_fields(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        update_session(
            index_conn,
            "sess-1",
            ai_recovery_labels=json.dumps(["user_corrected_recovery"]),
            ai_failure_modes=json.dumps(["reasoning_fabrication"]),
        )

        detail = get_session_detail(index_conn, "sess-1")

        assert detail is not None
        assert detail["ai_recovery_labels"] == ["user_corrected_recovery"]
        assert detail["ai_failure_modes"] == ["reasoning_fabrication"]

    def test_not_found(self, index_conn):
        assert get_session_detail(index_conn, "nonexistent") is None


class TestQueryUnscoredSessions:
    def test_since_filters_old_unscored_sessions(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session("old", start_time="2025-01-01T00:00:00+00:00"),
            _make_session("recent", start_time="2026-05-24T00:00:00+00:00"),
        ])

        results = query_unscored_sessions(
            index_conn,
            source=["claude"],
            since="2026-05-20T00:00:00+00:00",
        )

        assert [row["session_id"] for row in results] == ["recent"]

    def test_skips_segmented_parents(self, index_conn):
        """Segmented parent sessions are not directly scorable — their
        per-segment children carry the content. The daemon scanner and
        score --batch must skip them or they burn judge calls on the
        umbrella row."""
        upsert_sessions(index_conn, [
            _make_session("plain-unscored"),
            _make_session("parent-segmented"),
        ])
        update_session(index_conn, "parent-segmented", status="segmented")

        results = query_unscored_sessions(index_conn, source=["claude"])
        ids = [row["session_id"] for row in results]
        assert "plain-unscored" in ids
        assert "parent-segmented" not in ids

    def test_rescore_on_growth_reselects_stale_scored(self, index_conn):
        """Regression for the S16 bug: a session scored mid-flight (its
        ``end_time`` advanced past ``ai_scored_at``) must be re-selected when
        ``include_stale_scored=True`` so the stale early grade is corrected —
        but stay hidden by default."""
        from datetime import datetime, timezone
        upsert_sessions(index_conn, [
            _make_session(
                "grew",
                start_time="2026-05-24T00:00:00+00:00",
                end_time="2026-05-24T00:18:00+00:00",
            ),
        ])
        # Graded 16 minutes before the session's final activity.
        update_session(
            index_conn, "grew",
            ai_quality_score=2,
            ai_failure_value_score=2,
            ai_scored_at="2026-05-24T00:02:00+00:00",
        )
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        # Default contract: already-scored, so not returned.
        assert query_unscored_sessions(index_conn, source=["claude"], now=now) == []
        # Opt-in: returned for re-scoring.
        ids = [r["session_id"] for r in query_unscored_sessions(
            index_conn, source=["claude"], include_stale_scored=True, now=now)]
        assert ids == ["grew"]

    def test_rescore_skips_session_scored_after_it_finished(self, index_conn):
        """A session graded *after* its last activity is not stale and must not
        be re-selected (no churn on correctly-scored rows)."""
        from datetime import datetime, timezone
        upsert_sessions(index_conn, [
            _make_session(
                "done",
                start_time="2026-05-24T00:00:00+00:00",
                end_time="2026-05-24T00:18:00+00:00",
            ),
        ])
        update_session(
            index_conn, "done",
            ai_quality_score=5, ai_failure_value_score=4,
            ai_scored_at="2026-05-24T00:20:00+00:00",  # after end_time
        )
        now = datetime(2026, 5, 25, tzinfo=timezone.utc)
        assert query_unscored_sessions(
            index_conn, source=["claude"], include_stale_scored=True, now=now) == []

    def test_settle_seconds_defers_in_flight_session(self, index_conn):
        """An unscored session whose last activity is within the settle window
        is deferred (likely still running) so it isn't graded prematurely."""
        from datetime import datetime, timezone, timedelta
        now = datetime(2026, 5, 25, 12, 0, 0, tzinfo=timezone.utc)
        upsert_sessions(index_conn, [
            _make_session(
                "active",
                start_time=(now - timedelta(minutes=2)).isoformat(),
                end_time=(now - timedelta(seconds=30)).isoformat(),  # 30s ago
            ),
            _make_session(
                "settled",
                start_time=(now - timedelta(minutes=20)).isoformat(),
                end_time=(now - timedelta(minutes=10)).isoformat(),  # 10m ago
            ),
        ])
        ids = [r["session_id"] for r in query_unscored_sessions(
            index_conn, source=["claude"], settle_seconds=180, now=now)]
        assert ids == ["settled"]
        # With no settle window, both are immediately scorable.
        ids_all = {r["session_id"] for r in query_unscored_sessions(
            index_conn, source=["claude"], now=now)}
        assert ids_all == {"active", "settled"}


class TestQuerySessionsForRescore:
    def test_returns_sessions_regardless_of_existing_score(self, index_conn):
        """`clawjournal rescore --window` must overwrite, so the query
        returns scored sessions too — the difference from
        `query_unscored_sessions`."""
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        upsert_sessions(index_conn, [
            _make_session("scored", start_time=now_iso),
            _make_session("unscored", start_time=now_iso),
        ])
        # Mark "scored" as already scored under both rubrics.
        update_session(
            index_conn, "scored",
            ai_quality_score=4,
            ai_failure_value_score=4,
        )

        results = query_sessions_for_rescore(
            index_conn, window_days=7, source=["claude"],
        )
        ids = {row["session_id"] for row in results}
        assert ids == {"scored", "unscored"}

    def test_window_excludes_old_sessions(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session("old", start_time="2025-01-01T00:00:00+00:00"),
            _make_session("recent",
                          start_time="2026-05-24T00:00:00+00:00"),
        ])

        results = query_sessions_for_rescore(
            index_conn, window_days=7, source=["claude"],
        )
        # "recent" is from 2026-05-24; with a default current-time clock,
        # only sessions inside ``now - 7 days`` qualify. The old session
        # must never appear.
        assert "old" not in {row["session_id"] for row in results}

    def test_skips_segmented_parents(self, index_conn):
        from datetime import datetime, timezone
        now_iso = datetime.now(timezone.utc).isoformat()
        upsert_sessions(index_conn, [
            _make_session("plain", start_time=now_iso),
            _make_session("parent-segmented", start_time=now_iso),
        ])
        update_session(index_conn, "parent-segmented", status="segmented")

        results = query_sessions_for_rescore(
            index_conn, window_days=7, source=["claude"],
        )
        ids = {row["session_id"] for row in results}
        assert "plain" in ids
        assert "parent-segmented" not in ids


class TestSearchFts:
    def test_search_fts_matches_free_text_with_apostrophe(self, index_conn):
        upsert_sessions(index_conn, [_make_session(content="I'd like to examine these options carefully")])

        results = search_fts(index_conn, "I'd like to examine")

        assert len(results) == 1
        assert results[0]["session_id"] == "sess-1"

    def test_search_fts_returns_empty_for_punctuation_only(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])

        results = search_fts(index_conn, "!!! ??? '''")

        assert results == []


class TestUpdateSession:
    def test_update_status(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        ok = update_session(index_conn, "sess-1", status="shortlisted")
        assert ok is True

        row = index_conn.execute(
            "SELECT review_status, reviewed_at FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["review_status"] == "shortlisted"
        assert row["reviewed_at"] is not None

    def test_update_notes(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        update_session(index_conn, "sess-1", notes="Good trace", reason="strong debugging")

        row = index_conn.execute(
            "SELECT reviewer_notes, selection_reason FROM sessions WHERE session_id = 'sess-1'"
        ).fetchone()
        assert row["reviewer_notes"] == "Good trace"
        assert row["selection_reason"] == "strong debugging"

    def test_not_found(self, index_conn):
        assert update_session(index_conn, "nope", status="blocked") is False


class TestStats:
    def test_stats(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session("s1", source="claude"),
            _make_session("s2", source="codex"),
        ])
        stats = get_stats(index_conn)
        assert stats["total"] == 2
        assert stats["by_source"]["claude"] == 1
        assert stats["by_source"]["codex"] == 1
        assert stats["by_status"]["new"] == 2

    def test_stats_filters_by_date_range(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session(
                "old",
                start_time="2025-01-01T00:00:00+00:00",
                end_time="2025-01-01T00:10:00+00:00",
            ),
            _make_session(
                "new",
                source="codex",
                start_time="2025-01-10T00:00:00+00:00",
                end_time="2025-01-10T00:10:00+00:00",
            ),
        ])

        stats = get_stats(index_conn, start="2025-01-10", end="2025-01-10")

        assert stats["total"] == 1
        assert stats["by_source"] == {"codex": 1}
        assert stats["by_status"] == {"new": 1}


class TestDashboardAnalytics:
    def test_filters_by_date_range(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session(
                "old",
                start_time="2025-01-01T00:00:00+00:00",
                end_time="2025-01-01T00:10:00+00:00",
            ),
            _make_session(
                "new",
                source="codex",
                start_time="2025-01-10T00:00:00+00:00",
                end_time="2025-01-10T00:10:00+00:00",
            ),
        ])

        analytics = get_dashboard_analytics(index_conn, start="2025-01-10", end="2025-01-10")

        assert analytics["summary"]["total_sessions"] == 1
        assert analytics["summary"]["unique_sources"] == 1
        assert analytics["tokens_by_source"] == [
            {"source": "codex", "input_tokens": 500, "output_tokens": 100},
        ]

    def test_outcome_normalization_collapses_duplicates(self, index_conn):
        # AI `resolved` and heuristic `tests_passed` both normalize to
        # 'resolved'; heuristic `partial` (interrupted) and AI `partial`
        # (progress made) must stay separated.
        upsert_sessions(index_conn, [
            _make_session("a", start_time="2025-01-10T00:00:00+00:00",
                          end_time="2025-01-10T00:10:00+00:00"),
            _make_session("b", start_time="2025-01-10T00:00:00+00:00",
                          end_time="2025-01-10T00:10:00+00:00"),
            _make_session("c", start_time="2025-01-10T00:00:00+00:00",
                          end_time="2025-01-10T00:10:00+00:00"),
            _make_session("d", start_time="2025-01-10T00:00:00+00:00",
                          end_time="2025-01-10T00:10:00+00:00"),
            _make_session("e", start_time="2025-01-10T00:00:00+00:00",
                          end_time="2025-01-10T00:10:00+00:00"),
        ])
        # a: AI resolved, b: heuristic tests_passed  → both map to 'resolved'
        update_session(index_conn, "a", ai_outcome_badge="resolved")
        index_conn.execute("UPDATE sessions SET outcome_badge='tests_passed' WHERE session_id='b'")
        # c: heuristic 'completed' (no signal) → 'inconclusive'
        index_conn.execute("UPDATE sessions SET outcome_badge='completed' WHERE session_id='c'")
        # d: heuristic 'partial' (interrupted) vs e: AI 'partial' (progress)
        index_conn.execute("UPDATE sessions SET outcome_badge='partial' WHERE session_id='d'")
        update_session(index_conn, "e", ai_outcome_badge="partial")

        analytics = get_dashboard_analytics(index_conn)
        buckets = {r["outcome_label"]: r["count"] for r in analytics["by_outcome_label"]}
        assert buckets["resolved"] == 2
        assert buckets["inconclusive"] == 1
        assert buckets["interrupted"] == 1
        assert buckets["partial"] == 1
        # Resolve rate drops `completed` from the numerator: 2 resolved / 5 labeled.
        assert analytics["resolve_rate"] == 0.4

    def test_invalid_ai_outcome_gets_cleaned_on_open(self, tmp_path):
        from clawjournal.workbench.index import open_index, INDEX_DB
        import clawjournal.workbench.index as idx
        import clawjournal.config
        orig_dir = clawjournal.config.CONFIG_DIR
        orig_db = idx.INDEX_DB
        orig_blobs = idx.BLOBS_DIR
        try:
            clawjournal.config.CONFIG_DIR = tmp_path
            idx.CONFIG_DIR = tmp_path
            idx.INDEX_DB = tmp_path / "index.db"
            idx.BLOBS_DIR = tmp_path / "blobs"
            conn = open_index()
            upsert_sessions(conn, [_make_session("bad")])
            # Simulate a judge-era bug writing 'unknown' (not in valid set).
            conn.execute("UPDATE sessions SET ai_outcome_badge = 'unknown'")
            conn.commit()
            conn.close()

            conn2 = open_index()
            row = conn2.execute(
                "SELECT ai_outcome_badge FROM sessions WHERE session_id = 'bad'"
            ).fetchone()
            assert row["ai_outcome_badge"] is None
            conn2.close()
        finally:
            clawjournal.config.CONFIG_DIR = orig_dir
            idx.INDEX_DB = orig_db
            idx.BLOBS_DIR = orig_blobs

    def test_by_task_type_groups_by_coalesce_not_raw_column(self, index_conn):
        # Regression: `GROUP BY task_type` in SQLite with an alias that shadows
        # a real column resolves to the column, producing duplicate rows for
        # the same displayed COALESCE value. Confirm the fix groups by the
        # expression so each visible label appears exactly once.
        upsert_sessions(index_conn, [
            _make_session("a", start_time="2025-01-10T00:00:00+00:00",
                          end_time="2025-01-10T00:10:00+00:00"),
            _make_session("b", start_time="2025-01-10T00:00:00+00:00",
                          end_time="2025-01-10T00:10:00+00:00"),
            _make_session("c", start_time="2025-01-10T00:00:00+00:00",
                          end_time="2025-01-10T00:10:00+00:00"),
        ])
        # Induce the ai_task_type='unknown' / task_type!='unknown' divergence
        # that triggered the dashboard duplicate rows.
        index_conn.execute("UPDATE sessions SET task_type = 'refactor' WHERE session_id = 'a'")
        index_conn.execute("UPDATE sessions SET task_type = 'feature' WHERE session_id = 'b'")
        index_conn.execute("UPDATE sessions SET task_type = 'docs' WHERE session_id = 'c'")
        update_session(index_conn, "a", ai_task_type="unknown")
        update_session(index_conn, "b", ai_task_type="unknown")
        update_session(index_conn, "c", ai_task_type="unknown")
        analytics = get_dashboard_analytics(index_conn)
        task_types = [row["task_type"] for row in analytics["by_task_type"]]
        assert task_types == ["unknown"], (
            f"Expected one collapsed row, got {analytics['by_task_type']}"
        )
        assert analytics["by_task_type"][0]["count"] == 3


class TestShares:
    def test_create_and_get(self, index_conn):
        upsert_sessions(index_conn, [_make_session("s1"), _make_session("s2")])
        share_id = create_share(index_conn, ["s1", "s2"], note="Test share")

        share = get_share(index_conn, share_id)
        assert share is not None
        assert share["bundle_id"] == share_id
        assert share["session_count"] == 2
        assert share["submission_note"] == "Test share"
        assert len(share["sessions"]) == 2

    def test_list_shares(self, index_conn):
        upsert_sessions(index_conn, [_make_session()])
        create_share(index_conn, ["sess-1"])
        shares = get_shares(index_conn)
        assert len(shares) == 1
        assert shares[0]["bundle_id"] == shares[0]["share_id"]

    def test_nonexistent_sessions(self, index_conn):
        share_id = create_share(index_conn, ["nonexistent"])
        share = get_share(index_conn, share_id)
        assert share["session_count"] == 0

    def test_share_history_and_share_ready_recommendations(self, index_conn):
        # Recommendations require high failure value in the last 7 days. Use
        # `datetime.now()` so the window is always satisfied; the existing
        # in-memory DB has no clock dependency otherwise.
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        upsert_sessions(index_conn, [
            _make_session(
                "s1",
                start_time=(now - timedelta(days=3)).isoformat(),
                end_time=(now - timedelta(days=3, minutes=-10)).isoformat(),
            ),
            _make_session(
                "s2",
                start_time=(now - timedelta(days=2)).isoformat(),
                end_time=(now - timedelta(days=2, minutes=-10)).isoformat(),
            ),
            _make_session(
                "s3",
                start_time=(now - timedelta(days=1)).isoformat(),
                end_time=(now - timedelta(days=1, minutes=-10)).isoformat(),
            ),
        ])
        for sid in ("s1", "s2", "s3"):
            update_session(
                index_conn, sid,
                status="approved",
                ai_quality_score=5,
                ai_failure_value_score=5,
            )

        shared_share_id = create_share(index_conn, ["s1"])
        index_conn.execute(
            "UPDATE shares SET status = 'shared', shared_at = ? WHERE share_id = ?",
            (now.isoformat(), shared_share_id),
        )
        newer_share_id = create_share(index_conn, ["s1", "s2"])
        index_conn.commit()

        shared_share = get_share(index_conn, shared_share_id)
        assert [s["session_id"] for s in shared_share["sessions"]] == ["s1"]

        newer_share = get_share(index_conn, newer_share_id)
        assert [s["session_id"] for s in newer_share["sessions"]] == ["s1", "s2"]

        stats = get_share_ready_stats(index_conn)
        assert [s["session_id"] for s in stats["sessions"]] == ["s3", "s2"]
        # Recommendation is the same ordered list (recent high failure value, capped 5).
        assert stats["recommended_session_ids"] == ["s3", "s2"]

    def test_share_ready_does_not_recommend_legacy_productivity_only_scores(self, index_conn):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        upsert_sessions(index_conn, [
            _make_session(
                "legacy",
                start_time=(now - timedelta(days=1)).isoformat(),
                end_time=(now - timedelta(days=1, minutes=-10)).isoformat(),
            ),
            _make_session(
                "failure",
                start_time=now.isoformat(),
                end_time=(now + timedelta(minutes=10)).isoformat(),
            ),
        ])
        update_session(index_conn, "legacy", status="approved", ai_quality_score=5)
        update_session(
            index_conn, "failure",
            status="approved",
            ai_quality_score=3,
            ai_failure_value_score=4,
        )

        stats = get_share_ready_stats(index_conn)

        assert [s["session_id"] for s in stats["sessions"]] == ["failure", "legacy"]
        assert stats["recommended_session_ids"] == ["failure"]

    def test_share_ready_returns_normalized_outcome_labels(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session("raw-completed"),
            _make_session("raw-partial"),
            _make_session("ai-partial"),
        ])
        for sid in ("raw-completed", "raw-partial", "ai-partial"):
            update_session(
                index_conn,
                sid,
                status="approved",
                ai_quality_score=5,
                ai_failure_value_score=5,
            )
        index_conn.execute(
            "UPDATE sessions SET outcome_badge = 'completed' WHERE session_id = ?",
            ("raw-completed",),
        )
        index_conn.execute(
            "UPDATE sessions SET outcome_badge = 'partial' WHERE session_id = ?",
            ("raw-partial",),
        )
        index_conn.execute(
            "UPDATE sessions SET outcome_badge = 'completed' WHERE session_id = ?",
            ("ai-partial",),
        )
        update_session(index_conn, "ai-partial", ai_outcome_badge="partial")

        stats = get_share_ready_stats(index_conn)
        by_id = {s["session_id"]: s for s in stats["sessions"]}

        assert by_id["raw-completed"]["outcome_badge"] == "inconclusive"
        assert by_id["raw-partial"]["outcome_badge"] == "interrupted"
        assert by_id["ai-partial"]["outcome_badge"] == "partial"

    def test_share_ready_excludes_held_sessions(self, index_conn):
        """The queue must offer only shareable sessions: explicit holds
        (pending_review, active embargo) are dropped, but an auto-expired
        embargo passes through (treated as released)."""
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        upsert_sessions(index_conn, [
            _make_session(
                sid,
                start_time=now.isoformat(),
                end_time=(now + timedelta(minutes=10)).isoformat(),
            )
            for sid in ("ok", "held", "emb-active", "emb-expired")
        ])
        for sid in ("ok", "held", "emb-active", "emb-expired"):
            update_session(
                index_conn, sid,
                status="approved", ai_quality_score=5, ai_failure_value_score=5,
            )
        set_hold_state(index_conn, "held", "pending_review", changed_by="user", reason="test")
        set_hold_state(
            index_conn, "emb-active", "embargoed", changed_by="user", reason="test",
            embargo_until=(now + timedelta(days=30)).isoformat(),
        )
        # An expired embargo can't be set via set_hold_state (it requires a
        # future date), so write it directly to exercise the expiry path.
        index_conn.execute(
            "UPDATE sessions SET hold_state = 'embargoed', embargo_until = ? "
            "WHERE session_id = ?",
            ((now - timedelta(days=1)).isoformat(), "emb-expired"),
        )
        index_conn.commit()

        stats = get_share_ready_stats(index_conn)
        ids = {s["session_id"] for s in stats["sessions"]}
        assert "held" not in ids          # explicit pending_review hold
        assert "emb-active" not in ids     # active embargo
        assert "ok" in ids                 # default auto_redacted is shareable
        assert "emb-expired" in ids        # expired embargo treated as released
        assert "held" not in stats["recommended_session_ids"]
        assert "emb-active" not in stats["recommended_session_ids"]
        # The internal hold-state columns must not leak into the response shape.
        assert all(
            "hold_state" not in s and "embargo_until" not in s
            for s in stats["sessions"]
        )

    def test_share_ready_fills_default_queue_with_lower_failure_value_scores(self, index_conn):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        sessions = [
            _make_session(
                f"s{i}",
                start_time=(now - timedelta(minutes=i)).isoformat(),
                end_time=(now - timedelta(minutes=i - 10)).isoformat(),
            )
            for i in range(1, 13)
        ]
        upsert_sessions(index_conn, sessions)
        scores = {
            "s1": 5,
            "s2": 4,
            "s3": 3,
            "s4": 2,
            "s5": 1,
            "s6": 1,
            "s7": 1,
            "s8": 1,
            "s9": 1,
            "s10": 1,
            "s11": 1,
            "s12": 1,
        }
        for sid, score in scores.items():
            update_session(
                index_conn,
                sid,
                status="approved",
                ai_quality_score=5,
                ai_failure_value_score=score,
            )

        stats = get_share_ready_stats(index_conn)

        assert stats["recommended_session_ids"] == [
            "s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9", "s10",
        ]

    def test_share_ready_widened_pool_ranks_best_failure_examples_first(self, index_conn):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        upsert_sessions(index_conn, [
            _make_session(
                f"s{i}",
                start_time=(now - timedelta(minutes=i)).isoformat(),
                end_time=(now - timedelta(minutes=i - 10)).isoformat(),
            )
            for i in range(1, 12)
        ])
        for sid in ("s1", "s2"):
            update_session(
                index_conn,
                sid,
                status="approved",
                ai_quality_score=5,
                ai_failure_value_score=3,
            )
        for i in range(3, 6):
            update_session(
                index_conn,
                f"s{i}",
                status="new",
                ai_quality_score=4,
                ai_failure_value_score=4,
            )
        for i in range(6, 12):
            update_session(
                index_conn,
                f"s{i}",
                status="new",
                ai_quality_score=4,
                ai_failure_value_score=2,
            )

        stats = get_share_ready_stats(index_conn, include_unapproved=True)

        assert stats["recommended_session_ids"] == [
            "s3", "s4", "s5", "s1", "s2", "s6", "s7", "s8", "s9", "s10",
        ]

    def test_share_ready_widened_pool_uses_safe_review_status_allowlist(self, index_conn):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        session_ids = (
            "new-ok",
            "shortlisted-ok",
            "approved-ok",
            "blocked",
            "segmented",
            "held-new",
            "embargoed-shortlisted",
            "excluded-approved",
        )
        upsert_sessions(index_conn, [
            _make_session(
                sid,
                project=(
                    "claude:private-repo"
                    if sid == "excluded-approved"
                    else "claude:public-repo"
                ),
                start_time=now.isoformat(),
                end_time=(now + timedelta(minutes=10)).isoformat(),
            )
            for sid in session_ids
        ])
        statuses = {
            "new-ok": "new",
            "shortlisted-ok": "shortlisted",
            "approved-ok": "approved",
            "blocked": "blocked",
            "segmented": "segmented",
            "held-new": "new",
            "embargoed-shortlisted": "shortlisted",
            "excluded-approved": "approved",
        }
        for sid, status in statuses.items():
            update_session(
                index_conn,
                sid,
                status=status,
                ai_quality_score=5,
                ai_failure_value_score=5,
            )
        set_hold_state(
            index_conn,
            "held-new",
            "pending_review",
            changed_by="user",
            reason="test",
        )
        set_hold_state(
            index_conn,
            "embargoed-shortlisted",
            "embargoed",
            changed_by="user",
            reason="test",
            embargo_until=(now + timedelta(days=30)).isoformat(),
        )

        stats = get_share_ready_stats(
            index_conn,
            include_unapproved=True,
            excluded_projects=["claude:private-repo"],
        )

        assert {s["session_id"] for s in stats["sessions"]} == {
            "new-ok",
            "shortlisted-ok",
            "approved-ok",
        }
        assert set(stats["recommended_session_ids"]) == {
            "new-ok",
            "shortlisted-ok",
            "approved-ok",
        }

    def test_share_ready_respects_excluded_project_rules(self, index_conn):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        upsert_sessions(index_conn, [
            _make_session(
                "private", project="claude:private-repo",
                start_time=(now - timedelta(days=2)).isoformat(),
            ),
            _make_session(
                "public", project="claude:public-repo",
                start_time=(now - timedelta(days=1)).isoformat(),
            ),
        ])
        update_session(
            index_conn, "private",
            status="approved",
            ai_quality_score=5,
            ai_failure_value_score=5,
        )
        update_session(
            index_conn, "public",
            status="approved",
            ai_quality_score=5,
            ai_failure_value_score=5,
        )
        add_policy(index_conn, "exclude_project", "private-repo")

        settings = get_effective_share_settings(index_conn, {"excluded_projects": []})
        stats = get_share_ready_stats(
            index_conn,
            excluded_projects=settings["excluded_projects"],
        )

        assert [s["session_id"] for s in stats["sessions"]] == ["public"]

    def test_share_ready_respects_source_scope(self, index_conn):
        from datetime import datetime, timedelta, timezone
        now = datetime.now(timezone.utc)
        upsert_sessions(index_conn, [
            _make_session(
                "claude-ok",
                source="claude",
                start_time=(now - timedelta(days=2)).isoformat(),
            ),
            _make_session(
                "codex-ok",
                source="codex",
                start_time=(now - timedelta(days=1)).isoformat(),
            ),
            _make_session(
                "gemini-out",
                source="gemini",
                start_time=now.isoformat(),
            ),
        ])
        for sid in ("claude-ok", "codex-ok", "gemini-out"):
            update_session(
                index_conn,
                sid,
                status="approved",
                ai_quality_score=5,
                ai_failure_value_score=5,
            )

        stats = get_share_ready_stats(index_conn, source_filter=("claude", "codex"))

        assert {s["session_id"] for s in stats["sessions"]} == {"claude-ok", "codex-ok"}
        assert "gemini-out" not in stats["recommended_session_ids"]

    def test_create_share_can_enforce_source_scope(self, index_conn):
        upsert_sessions(index_conn, [
            _make_session("claude-ok", source="claude"),
            _make_session("gemini-out", source="gemini"),
        ])

        blockers = source_scope_blockers(
            index_conn,
            ["claude-ok", "gemini-out"],
            ("claude", "codex"),
        )
        share_id = create_share(
            index_conn,
            ["claude-ok", "gemini-out"],
            source_filter=("claude", "codex"),
        )

        assert [b["session_id"] for b in blockers] == ["gemini-out"]
        share = get_share(index_conn, share_id)
        assert [s["session_id"] for s in share["sessions"]] == ["claude-ok"]

    def test_exclusion_matches_legacy_claude_hyphenated_name(self):
        session = {
            "project": "claude:llm-gateway-infra",
            "source": "claude",
        }

        assert session_matches_excluded_projects(
            session,
            ["claude:Rayward-Codes-llm-gateway-infra"],
        )


class TestPolicies:
    def test_add_and_list(self, index_conn):
        pid = add_policy(index_conn, "redact_string", "my-secret", reason="API key")
        policies = get_policies(index_conn)
        assert len(policies) == 1
        assert policies[0]["policy_id"] == pid
        assert policies[0]["value"] == "my-secret"

    def test_remove(self, index_conn):
        pid = add_policy(index_conn, "exclude_project", "private-repo")
        assert remove_policy(index_conn, pid) is True
        assert len(get_policies(index_conn)) == 0

    def test_remove_nonexistent(self, index_conn):
        assert remove_policy(index_conn, "nope") is False


def _build_pre_migration_db() -> sqlite3.Connection:
    """Create an in-memory DB matching the pre-rename schema (bundles + bundle_id)."""
    conn = sqlite3.connect(":memory:", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(
        """CREATE TABLE bundles (
            bundle_id       TEXT PRIMARY KEY,
            created_at      TEXT NOT NULL,
            session_count   INTEGER,
            status          TEXT DEFAULT 'draft',
            attestation     TEXT,
            submission_note TEXT,
            bundle_hash     TEXT,
            manifest        TEXT,
            shared_at       TEXT,
            gcs_uri         TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE sessions (
            session_id         TEXT PRIMARY KEY,
            project            TEXT NOT NULL,
            source             TEXT NOT NULL,
            model              TEXT,
            start_time         TEXT,
            end_time           TEXT,
            duration_seconds   INTEGER,
            git_branch         TEXT,
            user_messages      INTEGER DEFAULT 0,
            assistant_messages INTEGER DEFAULT 0,
            tool_uses          INTEGER DEFAULT 0,
            input_tokens       INTEGER DEFAULT 0,
            output_tokens      INTEGER DEFAULT 0,
            display_title      TEXT,
            outcome_badge      TEXT,
            value_badges       TEXT,
            risk_badges        TEXT,
            sensitivity_score  REAL DEFAULT 0.0,
            task_type          TEXT,
            files_touched      TEXT,
            commands_run       TEXT,
            review_status      TEXT DEFAULT 'new',
            selection_reason   TEXT,
            reviewer_notes     TEXT,
            reviewed_at        TEXT,
            blob_path          TEXT,
            raw_source_path    TEXT,
            indexed_at         TEXT NOT NULL,
            updated_at         TEXT,
            bundle_id          TEXT REFERENCES bundles(bundle_id),
            ai_quality_score   INTEGER,
            ai_score_reason    TEXT,
            ai_display_title   TEXT,
            ai_summary         TEXT
        )"""
    )
    conn.execute(
        """CREATE TABLE bundle_sessions (
            bundle_id    TEXT NOT NULL REFERENCES bundles(bundle_id),
            session_id   TEXT NOT NULL REFERENCES sessions(session_id),
            added_at     TEXT NOT NULL,
            PRIMARY KEY (bundle_id, session_id)
        )"""
    )
    conn.execute(
        "CREATE INDEX idx_bundle_sessions_session_id ON bundle_sessions(session_id)"
    )
    return conn


class TestMigration:
    def test_migrates_bundles_to_shares(self):
        conn = _build_pre_migration_db()

        # Seed a row in each table with proper FK references.
        conn.execute(
            "INSERT INTO bundles (bundle_id, created_at, session_count, status,"
            " attestation, submission_note, bundle_hash, manifest, shared_at, gcs_uri)"
            " VALUES ('bundle-1', '2025-01-01T00:00:00+00:00', 1, 'draft',"
            " 'I attest', 'note', 'hash123', '{}', NULL, NULL)"
        )
        conn.execute(
            "INSERT INTO sessions (session_id, project, source, indexed_at,"
            " bundle_id, ai_summary)"
            " VALUES ('sess-1', 'proj', 'claude', 'now', 'bundle-1', 'summary')"
        )
        conn.execute(
            "INSERT INTO bundle_sessions (bundle_id, session_id, added_at)"
            " VALUES ('bundle-1', 'sess-1', 'now')"
        )

        _migrate_bundles_to_shares(conn)

        # shares has share_id, no bundle_id.
        shares_cols = [r[1] for r in conn.execute("PRAGMA table_info(shares)").fetchall()]
        assert "share_id" in shares_cols
        assert "bundle_id" not in shares_cols

        # share_sessions has share_id.
        ss_cols = [r[1] for r in conn.execute("PRAGMA table_info(share_sessions)").fetchall()]
        assert "share_id" in ss_cols
        assert "bundle_id" not in ss_cols

        # sessions has share_id, no bundle_id; other columns (ai_summary) preserved.
        sess_cols = [r[1] for r in conn.execute("PRAGMA table_info(sessions)").fetchall()]
        assert "share_id" in sess_cols
        assert "bundle_id" not in sess_cols
        assert "ai_summary" in sess_cols

        # Data survived.
        share_row = conn.execute("SELECT share_id FROM shares").fetchone()
        assert share_row[0] == "bundle-1"
        sess_row = conn.execute(
            "SELECT session_id, share_id, ai_summary FROM sessions"
        ).fetchone()
        assert sess_row[0] == "sess-1"
        assert sess_row[1] == "bundle-1"
        assert sess_row[2] == "summary"
        ss_row = conn.execute(
            "SELECT share_id, session_id FROM share_sessions"
        ).fetchone()
        assert ss_row[0] == "bundle-1"
        assert ss_row[1] == "sess-1"

        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

        # No dangling FKs after migration.
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []

        # create_share insert must work (this is what was broken pre-fix).
        new_id = create_share(conn, ["sess-1"], attestation="ok", note="n")
        assert new_id
        row = conn.execute(
            "SELECT share_id FROM shares WHERE share_id = ?", (new_id,)
        ).fetchone()
        assert row is not None

        conn.close()

    def test_migration_idempotent(self):
        conn = _build_pre_migration_db()
        conn.execute(
            "INSERT INTO bundles (bundle_id, created_at, session_count, status)"
            " VALUES ('bundle-1', '2025-01-01T00:00:00+00:00', 0, 'draft')"
        )

        _migrate_bundles_to_shares(conn)
        first_shares_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='shares'"
        ).fetchone()[0]
        first_sessions_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='sessions'"
        ).fetchone()[0]

        # Second call — must be a no-op.
        _migrate_bundles_to_shares(conn)

        second_shares_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='shares'"
        ).fetchone()[0]
        second_sessions_sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name='sessions'"
        ).fetchone()[0]
        assert first_shares_sql == second_shares_sql
        assert first_sessions_sql == second_sessions_sql
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 1

        conn.close()
