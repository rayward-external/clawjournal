"""Regression tests for issue #215 — workbench views stalling in "Loading".

Two SQLite behaviors combined into the stall:

- ``open_index()`` runs on every daemon API request. It ended with an
  unconditional ``UPDATE sessions ...`` cleanup, and a write statement takes
  SQLite's write lock even when it matches zero rows, so every request queued
  (up to the 30s busy timeout) behind whatever write transaction the
  background scanner held at that moment.
- The background scan re-parses every source file each tick and pushed every
  unchanged session through a value-identical ``ON CONFLICT DO UPDATE`` (and
  the checkpoint/subagent post-passes did the same), so on a large corpus the
  scanner held the write lock and dirtied the same pages for essentially the
  whole pass, every pass.

These tests pin the fixed behavior: the open path stays read-only on a
healthy database, and a steady-state scan pass over unchanged sessions makes
zero row changes.
"""

import sqlite3

import pytest

from clawjournal.workbench.index import (
    link_subagent_hierarchy,
    open_index,
    upsert_sessions,
)


@pytest.fixture
def index_env(tmp_path, monkeypatch):
    """Point the index at a temp directory (mirrors tests/workbench style)."""
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr(
        "clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config"
    )
    return tmp_path


def _make_session(session_id, *, content="Fix the login bug", parent=None):
    session = {
        "session_id": session_id,
        "project": "test-project",
        "source": "claude",
        "model": "claude-sonnet-4",
        "start_time": "2025-01-01T00:00:00+00:00",
        "end_time": "2025-01-01T00:10:00+00:00",
        "git_branch": "main",
        "messages": [
            {"role": "user", "content": content, "tool_uses": []},
            {"role": "assistant", "content": "Done.", "tool_uses": []},
        ],
        "stats": {
            "user_messages": 1,
            "assistant_messages": 1,
            "tool_uses": 0,
            "input_tokens": 500,
            "output_tokens": 100,
        },
    }
    if parent is not None:
        session["parent_session_id"] = parent
    return session


def _make_checkpoint(logical_id, index, *, sealed):
    session_id = logical_id if index == 0 else f"{logical_id}_seg-{index:04d}"
    checkpoint = _make_session(
        session_id, content=f"checkpoint {index} of {logical_id}"
    )
    checkpoint.update({
        "logical_session_id": logical_id,
        "segment_index": index,
        "segment_message_range": [index * 2, index * 2 + 1],
        "segment_reason": "bounded_checkpoint" if sealed else "active_tail",
        "segment_sealed": sealed,
        "raw_source_path": f"/tmp/{logical_id}.jsonl",
    })
    return checkpoint


class TestOpenIndexStaysReadOnly:
    def test_open_index_succeeds_while_another_connection_writes(self, index_env):
        """A request-path open must not need the write lock (#215).

        With the old unconditional cleanup UPDATE this call queued behind
        the in-flight write transaction for the full 30s busy timeout and
        then raised ``database is locked``.
        """
        setup = open_index()
        upsert_sessions(setup, [_make_session("sess-1")])
        setup.close()

        writer = sqlite3.connect(str(index_env / "index.db"), timeout=5)
        try:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute(
                "UPDATE sessions SET updated_at = updated_at "
                "WHERE session_id = 'sess-1'"
            )

            reader = open_index()
            try:
                count = reader.execute(
                    "SELECT COUNT(*) FROM sessions"
                ).fetchone()[0]
                assert count == 1
            finally:
                reader.close()
        finally:
            writer.rollback()
            writer.close()

    def test_open_index_still_cleans_invalid_outcome_badges(self, index_env):
        """The read-gated cleanup must still fire when bad values exist."""
        setup = open_index()
        upsert_sessions(setup, [_make_session("sess-1")])
        setup.execute(
            "UPDATE sessions SET ai_outcome_badge = 'not-a-real-badge' "
            "WHERE session_id = 'sess-1'"
        )
        setup.commit()
        setup.close()

        conn = open_index()
        try:
            row = conn.execute(
                "SELECT ai_outcome_badge FROM sessions WHERE session_id = 'sess-1'"
            ).fetchone()
            assert row["ai_outcome_badge"] is None
        finally:
            conn.close()


class TestSteadyStateScanPassWritesNothing:
    def test_unchanged_sessions_make_zero_row_changes(self, index_env):
        sessions = [
            _make_session("sess-parent"),
            _make_session("sess-child", content="Spawned task", parent="sess-parent"),
            _make_session("sess-plain", content="Another task"),
        ]

        conn = open_index()
        try:
            upsert_sessions(conn, sessions)
            link_subagent_hierarchy(conn)
            before = conn.total_changes

            stats = {}
            upsert_sessions(conn, [dict(s) for s in sessions], stats=stats)
            link_subagent_hierarchy(conn)

            assert stats == {"inserted": 0, "updated": 0, "unchanged": 3}
            assert conn.total_changes == before
        finally:
            conn.close()

    def test_unchanged_checkpoint_family_makes_zero_row_changes(self, index_env):
        family = [
            _make_checkpoint("fam-1", 0, sealed=True),
            _make_checkpoint("fam-1", 1, sealed=True),
            _make_checkpoint("fam-1", 2, sealed=False),
        ]

        conn = open_index()
        try:
            upsert_sessions(conn, family)
            link_subagent_hierarchy(conn)
            before = conn.total_changes

            stats = {}
            upsert_sessions(conn, [dict(s) for s in family], stats=stats)
            link_subagent_hierarchy(conn)

            assert stats == {"inserted": 0, "updated": 0, "unchanged": 3}
            assert conn.total_changes == before
        finally:
            conn.close()

    def test_changed_session_is_still_rewritten(self, index_env):
        """The no-op skip must never swallow a real content change."""
        conn = open_index()
        try:
            upsert_sessions(conn, [_make_session("sess-1")])

            changed = _make_session("sess-1")
            changed["messages"].append(
                {"role": "user", "content": "One more thing", "tool_uses": []}
            )
            changed["stats"]["user_messages"] = 2
            stats = {}
            upsert_sessions(conn, [changed], stats=stats)

            assert stats == {"inserted": 0, "updated": 1, "unchanged": 0}
            row = conn.execute(
                "SELECT user_messages FROM sessions WHERE session_id = 'sess-1'"
            ).fetchone()
            assert row["user_messages"] == 2
        finally:
            conn.close()

    def test_metadata_only_refresh_is_still_written(self, index_env):
        """Parser enrichment with unchanged transcript content must still
        land in the row (the skip only covers byte-identical no-ops)."""
        conn = open_index()
        try:
            upsert_sessions(conn, [_make_session("sess-1")])

            enriched = _make_session("sess-1")
            enriched["git_branch"] = "feature/enriched"
            stats = {}
            upsert_sessions(conn, [enriched], stats=stats)

            assert stats == {"inserted": 0, "updated": 0, "unchanged": 1}
            row = conn.execute(
                "SELECT git_branch FROM sessions WHERE session_id = 'sess-1'"
            ).fetchone()
            assert row["git_branch"] == "feature/enriched"
        finally:
            conn.close()
