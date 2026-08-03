"""Execution-recorder coverage for the guided workbench-index rebuild."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from clawjournal.capture.cursors import ensure_schema as ensure_capture_schema
from clawjournal.events.cost.schema import ensure_cost_schema
from clawjournal.events.export.schema import ensure_export_schema
from clawjournal.events.incidents.schema import ensure_incidents_schema
from clawjournal.events.schema import ensure_schema as ensure_events_schema
from clawjournal.events.search.schema import ensure_search_schema
from clawjournal.events.view import ensure_view_schema
from clawjournal.events.view import write_hook_override
from clawjournal.workbench import index as index_module
from clawjournal.workbench import index_recovery


PARENT_ID = 40
CHILD_ID = 10
PARENT_KEY = "claude:project:session-recovery"
CHILD_KEY = "claude:project:child-recovery"
PARENT_SOURCE = "C:/recovery/project/session-recovery.jsonl"
CHILD_SOURCE = "C:/recovery/project/child-recovery.jsonl"
TS = "2026-07-30T10:00:00Z"


@pytest.fixture
def recovery_install_events(tmp_path, monkeypatch) -> Path:
    """Keep the source, rebuilt database, and recovery backup test-local."""

    install_dir = tmp_path / "state"
    monkeypatch.setattr(index_module, "CONFIG_DIR", install_dir)
    monkeypatch.setattr(index_module, "INDEX_DB", install_dir / "index.db")
    monkeypatch.setattr(index_module, "BLOBS_DIR", install_dir / "blobs")
    index_recovery._set_health(
        {"status": "ready", "message": "Test index is ready."}
    )
    yield install_dir
    index_recovery._set_health(
        {"status": "ready", "message": "Test index is ready."}
    )


def _workbench_session() -> dict:
    return {
        "session_id": "session-recovery",
        "project": "recovery-project",
        "source": "claude",
        "raw_source_path": PARENT_SOURCE,
        "model": "claude-sonnet-4",
        "start_time": "2026-07-30T10:00:00Z",
        "end_time": "2026-07-30T10:10:00Z",
        "messages": [
            {"role": "user", "content": "Recover recorder data", "tool_uses": []},
            {"role": "assistant", "content": "Recovered.", "tool_uses": []},
        ],
        "stats": {
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_uses": 0,
            "input_tokens": 100,
            "output_tokens": 25,
        },
    }


def _recovery_scan() -> dict[str, object]:
    conn = index_module.open_index()
    try:
        index_module.upsert_sessions(conn, [_workbench_session()])
        # Exercise the post-recorder bridge instead of relying on upsert's
        # path-derived session key.
        conn.execute(
            "UPDATE sessions SET session_key = NULL WHERE session_id = ?",
            ("session-recovery",),
        )
        conn.commit()
    finally:
        conn.close()
    return {"ok": True}


def _assistant_usage(cache_read: int, *, marker: str | None = None) -> str:
    payload = {
        "type": "assistant",
        "message": {
            "model": "claude-opus-4-6",
            "usage": {
                "input_tokens": 1_000,
                "output_tokens": 50,
                "cache_read_input_tokens": cache_read,
            },
        },
    }
    if marker is not None:
        payload["recovery_marker"] = marker
    return json.dumps(payload, sort_keys=True)


def _command_raw(tool_id: str) -> str:
    return json.dumps(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": "Bash",
                        "input": {"command": "npm test"},
                    }
                ]
            },
        },
        sort_keys=True,
    )


def _result_raw(tool_id: str) -> str:
    return json.dumps(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": tool_id,
                        "content": [
                            {
                                "type": "text",
                                "text": "FAIL auth.test.ts\nTests: 1 failed, 0 passed",
                            }
                        ],
                    }
                ]
            },
        },
        sort_keys=True,
    )


def _event_row(
    event_id: int,
    session_id: int,
    event_type: str,
    event_key: str,
    event_at: str,
    source_path: str,
    source_offset: int,
    raw_json: str,
) -> tuple:
    return (
        event_id,
        session_id,
        event_type,
        event_key,
        event_at,
        TS,
        "claude-jsonl",
        source_path,
        source_offset,
        0,
        "claude",
        "high",
        "none",
        raw_json,
    )


def _seed_source_index(conn: sqlite3.Connection) -> None:
    index_module.upsert_sessions(conn, [_workbench_session()])
    index_module.update_session(
        conn,
        "session-recovery",
        status="approved",
        notes="Keep the recorder review",
        reason="Recorder recovery fixture",
    )
    index_module.set_hold_state(
        conn,
        "session-recovery",
        "pending_review",
        changed_by="user",
        reason="Do not share during recovery",
    )

    ensure_capture_schema(conn)
    ensure_events_schema(conn)
    ensure_view_schema(conn)
    ensure_export_schema(conn)
    ensure_cost_schema(conn)
    ensure_incidents_schema(conn)
    ensure_search_schema(conn)
    conn.commit()

    # The child has the lower rowid, so an unordered bulk copy encounters it
    # before its parent. Recovery must still restore the self-FK correctly.
    conn.execute(
        """INSERT INTO event_sessions (
               id, session_key, parent_session_key, parent_session_id,
               client, client_version, started_at, ended_at, status
           ) VALUES (?, ?, NULL, NULL, 'claude', '1.0', ?, ?, 'ended')""",
        (PARENT_ID, PARENT_KEY, TS, "2026-07-30T10:10:00Z"),
    )
    conn.execute(
        """INSERT INTO event_sessions (
               id, session_key, parent_session_key, parent_session_id,
               client, client_version, started_at, ended_at, status
           ) VALUES (?, ?, ?, ?, 'claude', '1.0', ?, ?, 'ended')""",
        (
            CHILD_ID,
            CHILD_KEY,
            PARENT_KEY,
            PARENT_ID,
            "2026-07-30T10:01:00Z",
            "2026-07-30T10:09:00Z",
        ),
    )

    events = [
        _event_row(
            100,
            PARENT_ID,
            "assistant_message",
            "assistant:usage-1",
            "2026-07-30T10:00:00Z",
            PARENT_SOURCE,
            0,
            _assistant_usage(10_000, marker="recoveryneedle"),
        ),
        _event_row(
            101,
            PARENT_ID,
            "assistant_message",
            "assistant:usage-2",
            "2026-07-30T10:00:01Z",
            PARENT_SOURCE,
            1,
            _assistant_usage(100),
        ),
    ]
    for index in range(3):
        tool_id = f"loop-{index}"
        event_id = 200 + (index * 2)
        events.extend(
            [
                _event_row(
                    event_id,
                    CHILD_ID,
                    "command_start",
                    f"command_start:{tool_id}",
                    f"2026-07-30T10:01:0{index * 2}Z",
                    CHILD_SOURCE,
                    index * 2,
                    _command_raw(tool_id),
                ),
                _event_row(
                    event_id + 1,
                    CHILD_ID,
                    "tool_result",
                    f"tool_result:{tool_id}",
                    f"2026-07-30T10:01:0{index * 2 + 1}Z",
                    CHILD_SOURCE,
                    index * 2 + 1,
                    _result_raw(tool_id),
                ),
            ]
        )
    conn.executemany(
        """INSERT INTO events (
               id, session_id, type, event_key, event_at, ingested_at,
               source, source_path, source_offset, seq, client, confidence,
               lossiness, raw_json
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        events,
    )

    conn.execute(
        """INSERT INTO event_overrides (
               session_id, event_key, type, source, confidence, lossiness,
               event_at, payload_json, origin, created_at, write_seq
           ) VALUES (?, ?, 'assistant_message', 'hook', 'high', 'none',
                     ?, ?, 'hook:recovery-test', ?, 7)""",
        (
            PARENT_ID,
            "assistant:usage-1",
            "2026-07-30T10:00:00Z",
            '{"precise":true}',
            TS,
        ),
    )
    conn.execute(
        """INSERT INTO event_source_snippets (
               source_path, source_offset, seq, text, imported_at
           ) VALUES (?, 17, 0, ?, ?)""",
        (
            "/imported/redacted/session.jsonl",
            '{"redacted":"snippet-recovery"}',
            TS,
        ),
    )
    conn.executemany(
        """INSERT INTO capture_cursors (
               consumer_id, source_path, inode, last_offset, last_modified,
               client, first_seen, last_seen
           ) VALUES ('events', ?, ?, ?, 1234.5, 'claude', ?, ?)""",
        [
            (PARENT_SOURCE, 111, 901, TS, TS),
            (CHILD_SOURCE, 222, 902, TS, TS),
        ],
    )

    # Seed the derived rows and their old cursors as a realistic source. The
    # recovery path may rebuild these tables, but the recovered semantics and
    # new cursor frontiers must remain complete.
    conn.executemany(
        """INSERT INTO token_usage (
               event_id, session_id, model, input, output, cache_read,
               data_source, pricing_table_version, event_at
           ) VALUES (?, ?, 'claude-opus-4-6', 1000, 50, ?, 'api',
                     'source-pricing', ?)""",
        [
            (100, PARENT_ID, 10_000, "2026-07-30T10:00:00Z"),
            (101, PARENT_ID, 100, "2026-07-30T10:00:01Z"),
        ],
    )
    conn.execute(
        """INSERT INTO cost_anomalies (
               id, session_id, turn_event_id, kind, confidence,
               evidence_json, created_at
           ) VALUES (1, ?, 101, 'cache_read_collapse', 'high', '{}', ?)""",
        (PARENT_ID, TS),
    )
    conn.execute(
        "INSERT INTO cost_ingest_state VALUES ('cost_ledger', 205)"
    )
    conn.execute(
        """INSERT INTO incidents (
               id, session_id, kind, first_event_id, last_event_id,
               evidence_json, count, confidence, created_at
           ) VALUES (1, ?, 'loop_exact_repeat', 200, 204, '{}', 3, 'high', ?)""",
        (CHILD_ID, TS),
    )
    conn.execute(
        """INSERT INTO loop_ingest_state (
               consumer_id, last_event_id, last_override_write_seq,
               last_override_created_at, last_override_session_id,
               last_override_event_key
           ) VALUES ('loop_detector', 205, 7, ?, ?, 'assistant:usage-1')""",
        (TS, PARENT_ID),
    )

    conn.execute(
        """INSERT INTO benchmarks (
               benchmark_id, window_start, window_end, generated_at,
               status, backend, payload_json
           ) VALUES ('benchmark-recovery', ?, ?, ?, 'ready', 'test-backend', ?)""",
        (
            "2026-07-20T00:00:00Z",
            "2026-07-27T00:00:00Z",
            TS,
            '{"title":"Recovery benchmark"}',
        ),
    )
    conn.execute(
        """INSERT INTO benchmark_tasks (
               task_id, benchmark_id, title, readiness, points
           ) VALUES ('benchmark-task-recovery', 'benchmark-recovery',
                     'Preserve recorder state', 'ready', 5)"""
    )
    conn.execute(
        """INSERT INTO benchmark_exports (
               export_id, benchmark_id, kind, path, created_at,
               redaction_summary_json
           ) VALUES ('benchmark-export-recovery', 'benchmark-recovery',
                     'markdown', '/tmp/recovery.md', ?, '{}')""",
        (TS,),
    )

    # Deliberately stale FTS state must not be copied from the backup.
    conn.execute("INSERT INTO events_fts(events_fts) VALUES('delete-all')")
    conn.commit()
    assert conn.execute(
        "SELECT COUNT(*) FROM events_fts_docsize"
    ).fetchone()[0] == 0


def test_guided_rebuild_restores_execution_recorder_and_benchmarks(
    recovery_install_events,
):
    source = index_module.open_index()
    try:
        _seed_source_index(source)
    finally:
        source.close()

    result = index_recovery.guided_rebuild(_recovery_scan)

    assert result["status"] == "ready"
    assert result["warnings"] == []
    assert Path(result["backup_path"], "index.db").is_file()
    counts = result["restored_state_counts"]
    assert {
        key: counts[key]
        for key in (
            "event_sessions",
            "events",
            "event_overrides",
            "event_source_snippets",
            "event_capture_cursors",
            "token_usage",
            "cost_anomalies",
            "incidents",
            "benchmarks",
            "benchmark_tasks",
            "benchmark_exports",
        )
    } == {
        "event_sessions": 2,
        "events": 8,
        "event_overrides": 1,
        "event_source_snippets": 1,
        "event_capture_cursors": 2,
        "token_usage": 2,
        "cost_anomalies": 1,
        "incidents": 1,
        "benchmarks": 1,
        "benchmark_tasks": 1,
        "benchmark_exports": 1,
    }

    rebuilt = index_module.open_index()
    try:
        assert [row[0] for row in rebuilt.execute("PRAGMA quick_check(1)")] == [
            "ok"
        ]
        assert rebuilt.execute("PRAGMA foreign_key_check").fetchall() == []

        sessions = rebuilt.execute(
            "SELECT id, session_key, parent_session_key, parent_session_id "
            "FROM event_sessions ORDER BY id"
        ).fetchall()
        assert [tuple(row) for row in sessions] == [
            (CHILD_ID, CHILD_KEY, PARENT_KEY, PARENT_ID),
            (PARENT_ID, PARENT_KEY, None, None),
        ]
        assert [row[0] for row in rebuilt.execute(
            "SELECT id FROM events ORDER BY id"
        )] == [100, 101, 200, 201, 202, 203, 204, 205]

        override = rebuilt.execute(
            "SELECT session_id, event_key, payload_json, write_seq "
            "FROM event_overrides"
        ).fetchone()
        assert tuple(override) == (
            PARENT_ID,
            "assistant:usage-1",
            '{"precise":true}',
            7,
        )
        snippet = rebuilt.execute(
            "SELECT source_path, source_offset, seq, text "
            "FROM event_source_snippets"
        ).fetchone()
        assert tuple(snippet) == (
            "/imported/redacted/session.jsonl",
            17,
            0,
            '{"redacted":"snippet-recovery"}',
        )

        token_rows = rebuilt.execute(
            "SELECT event_id, session_id, model, input, cache_read, data_source, "
            "pricing_table_version "
            "FROM token_usage ORDER BY event_id"
        ).fetchall()
        assert [tuple(row) for row in token_rows] == [
            (
                100,
                PARENT_ID,
                "claude-opus-4-6",
                1_000,
                10_000,
                "api",
                "source-pricing",
            ),
            (
                101,
                PARENT_ID,
                "claude-opus-4-6",
                1_000,
                100,
                "api",
                "source-pricing",
            ),
        ]
        assert rebuilt.execute(
            "SELECT kind FROM cost_anomalies"
        ).fetchone()[0] == "cache_read_collapse"
        incident = rebuilt.execute(
            "SELECT session_id, kind, first_event_id, last_event_id, count "
            "FROM incidents"
        ).fetchone()
        assert tuple(incident) == (
            CHILD_ID,
            "loop_exact_repeat",
            200,
            204,
            3,
        )

        capture_rows = rebuilt.execute(
            "SELECT source_path, inode, last_offset FROM capture_cursors "
            "WHERE consumer_id = 'events' ORDER BY source_path"
        ).fetchall()
        assert [tuple(row) for row in capture_rows] == [
            (CHILD_SOURCE, 222, 902),
            (PARENT_SOURCE, 111, 901),
        ]
        assert rebuilt.execute(
            "SELECT last_event_id FROM cost_ingest_state "
            "WHERE consumer_id = 'cost_ledger'"
        ).fetchone()[0] == 205
        loop_cursor = rebuilt.execute(
            "SELECT last_event_id, last_override_write_seq, "
            "last_override_session_id, last_override_event_key "
            "FROM loop_ingest_state WHERE consumer_id = 'loop_detector'"
        ).fetchone()
        assert tuple(loop_cursor) == (
            205,
            7,
            PARENT_ID,
            "assistant:usage-1",
        )

        assert rebuilt.execute(
            "SELECT COUNT(*) FROM events_fts_docsize"
        ).fetchone()[0] == 8
        assert [row[0] for row in rebuilt.execute(
            "SELECT rowid FROM events_fts WHERE events_fts MATCH 'recoveryneedle'"
        )] == [100]

        workbench = rebuilt.execute(
            "SELECT review_status, reviewer_notes, hold_state, session_key "
            "FROM sessions WHERE session_id = 'session-recovery'"
        ).fetchone()
        assert tuple(workbench) == (
            "approved",
            "Keep the recorder review",
            "pending_review",
            PARENT_KEY,
        )
        benchmark = rebuilt.execute(
            "SELECT status, backend, payload_json FROM benchmarks "
            "WHERE benchmark_id = 'benchmark-recovery'"
        ).fetchone()
        assert tuple(benchmark) == (
            "ready",
            "test-backend",
            '{"title":"Recovery benchmark"}',
        )
        assert rebuilt.execute(
            "SELECT title FROM benchmark_tasks "
            "WHERE task_id = 'benchmark-task-recovery'"
        ).fetchone()[0] == "Preserve recorder state"
        assert rebuilt.execute(
            "SELECT path FROM benchmark_exports "
            "WHERE export_id = 'benchmark-export-recovery'"
        ).fetchone()[0] == "/tmp/recovery.md"
    finally:
        rebuilt.close()


def test_malformed_recorder_stays_in_backup_without_downgrading_review_state(
    recovery_install_events,
):
    source = index_module.open_index()
    try:
        index_module.upsert_sessions(source, [_workbench_session()])
        index_module.update_session(
            source,
            "session-recovery",
            status="approved",
            notes="Keep this approval",
        )
        source.execute(
            "CREATE TABLE event_sessions ("
            "id INTEGER PRIMARY KEY, session_key TEXT NOT NULL, client TEXT NOT NULL)"
        )
        # Deliberately omit raw_json, a required recovery column.
        source.execute(
            "CREATE TABLE events ("
            "id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, type TEXT NOT NULL, "
            "ingested_at TEXT NOT NULL, source TEXT NOT NULL, source_path TEXT NOT NULL, "
            "source_offset INTEGER NOT NULL, client TEXT NOT NULL, "
            "confidence TEXT NOT NULL, lossiness TEXT NOT NULL)"
        )
        source.execute(
            "INSERT INTO event_sessions VALUES (1, 'broken:session', 'claude')"
        )
        source.execute(
            "INSERT INTO events VALUES "
            "(1, 1, 'assistant_message', ?, 'claude-jsonl', 'broken.jsonl', "
            "0, 'claude', 'high', 'none')",
            (TS,),
        )
        source.execute("DROP TABLE benchmark_exports")
        source.execute("DROP TABLE benchmark_tasks")
        source.execute("DROP TABLE benchmarks")
        source.execute("CREATE TABLE benchmarks (benchmark_id TEXT PRIMARY KEY)")
        source.execute("INSERT INTO benchmarks VALUES ('broken-benchmark')")
        source.commit()
    finally:
        source.close()

    result = index_recovery.guided_rebuild(_recovery_scan)

    assert result["status"] == "ready"
    assert any(
        "Execution-recorder state could not be restored" in warning
        for warning in result["warnings"]
    )
    assert any(
        "Historical benchmark state could not be restored" in warning
        for warning in result["warnings"]
    )
    rebuilt = index_module.open_index()
    try:
        review = rebuilt.execute(
            "SELECT review_status, reviewer_notes FROM sessions "
            "WHERE session_id = 'session-recovery'"
        ).fetchone()
        assert tuple(review) == ("approved", "Keep this approval")
        assert rebuilt.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    finally:
        rebuilt.close()


def test_failed_override_restore_resets_loop_frontier_for_future_hooks(
    recovery_install_events,
):
    source = index_module.open_index()
    try:
        _seed_source_index(source)
        source.execute("DROP TABLE event_overrides")
        source.execute(
            "CREATE TABLE event_overrides ("
            "session_id INTEGER NOT NULL, event_key TEXT NOT NULL, type TEXT NOT NULL, "
            "source TEXT NOT NULL, confidence TEXT NOT NULL, lossiness TEXT NOT NULL, "
            "event_at TEXT, origin TEXT, created_at TEXT NOT NULL, "
            "write_seq INTEGER NOT NULL DEFAULT 0, "
            "PRIMARY KEY (session_id, event_key))"
        )
        source.commit()
    finally:
        source.close()

    result = index_recovery.guided_rebuild(_recovery_scan)

    assert any(
        "overrides could not be restored" in warning
        for warning in result["warnings"]
    )
    assert any(
        "override cursor was reset" in warning
        for warning in result["warnings"]
    )
    rebuilt = index_module.open_index()
    try:
        assert rebuilt.execute("SELECT COUNT(*) FROM incidents").fetchone()[0] == 1
        cursor = rebuilt.execute(
            "SELECT last_override_write_seq, last_override_created_at, "
            "last_override_session_id, last_override_event_key "
            "FROM loop_ingest_state WHERE consumer_id = 'loop_detector'"
        ).fetchone()
        assert tuple(cursor) == (0, None, 0, "")

        assert write_hook_override(
            rebuilt,
            session_key=PARENT_KEY,
            event_key="future-hook",
            event_type="assistant_message",
            source="hook",
            confidence="high",
            lossiness="none",
            event_at="2026-07-30T10:20:00Z",
            payload_json='{"future":true}',
            origin="hook:after-recovery",
        )
        rebuilt.commit()
        assert rebuilt.execute(
            "SELECT write_seq FROM event_overrides "
            "WHERE event_key = 'future-hook'"
        ).fetchone()[0] == 1
    finally:
        rebuilt.close()


def test_legacy_overrides_reset_loop_frontier_before_sequence_backfill(
    recovery_install_events,
):
    source = index_module.open_index()
    try:
        _seed_source_index(source)
        source.execute("DROP TABLE event_overrides")
        source.execute(
            "CREATE TABLE event_overrides ("
            "session_id INTEGER NOT NULL, event_key TEXT NOT NULL, type TEXT NOT NULL, "
            "source TEXT NOT NULL, confidence TEXT NOT NULL, lossiness TEXT NOT NULL, "
            "event_at TEXT, payload_json TEXT NOT NULL, origin TEXT, "
            "created_at TEXT NOT NULL, PRIMARY KEY (session_id, event_key))"
        )
        source.execute(
            "INSERT INTO event_overrides ("
            "session_id, event_key, type, source, confidence, lossiness, event_at, "
            "payload_json, origin, created_at) VALUES "
            "(?, 'assistant:usage-1', 'assistant_message', 'hook', 'high', "
            "'none', ?, '{\"precise\":true}', 'hook:legacy', ?)",
            (PARENT_ID, "2026-07-30T10:00:00Z", TS),
        )
        source.commit()
    finally:
        source.close()

    result = index_recovery.guided_rebuild(_recovery_scan)

    assert any(
        "override cursor was reset" in warning
        for warning in result["warnings"]
    )
    rebuilt = index_module.open_index()
    try:
        cursor = rebuilt.execute(
            "SELECT last_override_write_seq, last_override_created_at, "
            "last_override_session_id, last_override_event_key "
            "FROM loop_ingest_state WHERE consumer_id = 'loop_detector'"
        ).fetchone()
        assert tuple(cursor) == (0, None, 0, "")
        assert rebuilt.execute(
            "SELECT write_seq FROM event_overrides "
            "WHERE event_key = 'assistant:usage-1'"
        ).fetchone()[0] == 1

        assert write_hook_override(
            rebuilt,
            session_key=PARENT_KEY,
            event_key="future-legacy-hook",
            event_type="assistant_message",
            source="hook",
            confidence="high",
            lossiness="none",
            event_at="2026-07-30T10:20:00Z",
            payload_json='{\"future\":true}',
            origin="hook:after-legacy-recovery",
        )
        rebuilt.commit()
        assert rebuilt.execute(
            "SELECT write_seq FROM event_overrides "
            "WHERE event_key = 'future-legacy-hook'"
        ).fetchone()[0] == 2
    finally:
        rebuilt.close()
