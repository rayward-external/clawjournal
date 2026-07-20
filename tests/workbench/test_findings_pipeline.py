"""Tests for the scan-time findings pipeline driver."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from clawjournal.findings import (
    SESSION_SETTLE_SECONDS,
    compute_findings_revision,
    reset_salt_cache,
)
from clawjournal.workbench.findings_pipeline import (
    _session_is_settled,
    drain_findings_backfill,
    run_findings_pipeline,
)
from clawjournal.workbench.index import BLOBS_DIR, open_index, upsert_sessions


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    reset_salt_cache()
    connection = open_index()
    yield connection
    connection.close()
    reset_salt_cache()


_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUifQ"
    ".abcdefghijABCDEFGH0123456789"
)


def _settled_session(session_id="sess-1", content=None):
    # end_time well past the settle window.
    old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return {
        "session_id": session_id,
        "project": "demo",
        "source": "claude",
        "model": "claude-sonnet-4",
        "start_time": old,
        "end_time": old,
        "git_branch": "main",
        "display_title": "t",
        "messages": [
            {"role": "user", "content": content or f"The token is {_FAKE_JWT}",
             "thinking": "", "tool_uses": []},
        ],
        "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0,
                  "input_tokens": 1, "output_tokens": 0},
    }


def _active_session():
    # end_time inside the settle window.
    now_iso = datetime.now(timezone.utc).isoformat()
    return {
        **_settled_session(),
        "end_time": now_iso,
    }


class TestSettleCheck:
    def test_null_end_time_is_not_settled(self):
        assert _session_is_settled(None) is False
        assert _session_is_settled("") is False

    def test_unparseable_is_not_settled(self):
        assert _session_is_settled("not-a-timestamp") is False

    def test_recent_is_not_settled(self):
        recent = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        assert _session_is_settled(recent) is False

    def test_past_settle_threshold_is_settled(self):
        old = (datetime.now(timezone.utc) - timedelta(seconds=SESSION_SETTLE_SECONDS + 10)).isoformat()
        assert _session_is_settled(old) is True

    def test_naive_timestamp_treated_as_utc(self):
        naive = (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=1)).isoformat()
        assert _session_is_settled(naive) is True


class TestRunFindingsPipeline:
    def test_active_session_is_skipped(self, conn):
        blob = _active_session()
        upsert_sessions(conn, [blob])
        conn.commit()
        result = run_findings_pipeline(conn, blob["session_id"], blob)
        assert result["status"] == "active_skip"
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0

    def test_rebuild_persists_findings_and_revision(self, conn):
        blob = _settled_session()
        upsert_sessions(conn, [blob])
        conn.commit()
        result = run_findings_pipeline(conn, blob["session_id"], blob)
        assert result["status"] == "rebuilt"
        assert result["count"] >= 1
        assert result["revision"].startswith("v1:")
        persisted = conn.execute(
            "SELECT findings_revision FROM sessions WHERE session_id = ?",
            (blob["session_id"],),
        ).fetchone()["findings_revision"]
        assert persisted == result["revision"]

    def test_unchanged_revision_is_no_op(self, conn):
        blob = _settled_session()
        upsert_sessions(conn, [blob])
        conn.commit()
        first = run_findings_pipeline(conn, blob["session_id"], blob)
        assert first["status"] == "rebuilt"

        # Capture current row count + decided_at values, run again.
        before = conn.execute(
            "SELECT finding_id, decided_at, created_at FROM findings"
        ).fetchall()
        result = run_findings_pipeline(conn, blob["session_id"], blob)
        assert result["status"] == "unchanged"
        after = conn.execute(
            "SELECT finding_id, decided_at, created_at FROM findings"
        ).fetchall()
        assert [dict(r) for r in before] == [dict(r) for r in after]

    def test_zero_findings_revision_short_circuits(self, conn):
        blob = _settled_session(content="no secrets here")
        upsert_sessions(conn, [blob])
        conn.commit()
        result = run_findings_pipeline(conn, blob["session_id"], blob)
        assert result["status"] == "rebuilt"
        assert result["count"] == 0
        # Revision is persisted even with zero findings.
        assert conn.execute(
            "SELECT findings_revision FROM sessions WHERE session_id = ?",
            (blob["session_id"],),
        ).fetchone()["findings_revision"] is not None
        # Second call short-circuits — no rebuild.
        again = run_findings_pipeline(conn, blob["session_id"], blob)
        assert again["status"] == "unchanged"

    def test_blob_change_flips_revision_and_rebuilds(self, conn):
        blob = _settled_session()
        upsert_sessions(conn, [blob])
        conn.commit()
        run_findings_pipeline(conn, blob["session_id"], blob)

        # Append a new message that contains a new secret.
        mutated = dict(blob)
        mutated["messages"] = list(blob["messages"]) + [
            {"role": "assistant", "content": "ghp_abcdefghijklmnopqrstuvwxyzABCDEF0123",
             "thinking": "", "tool_uses": []},
        ]
        result = run_findings_pipeline(conn, blob["session_id"], mutated)
        assert result["status"] == "rebuilt"
        engines = {
            r["engine"] for r in conn.execute("SELECT engine FROM findings").fetchall()
        }
        assert engines == {"regex_secrets"}

    def test_force_bypasses_settle_and_revision(self, conn):
        blob = _active_session()
        upsert_sessions(conn, [blob])
        conn.commit()
        # Without force, skipped due to active session.
        assert run_findings_pipeline(conn, blob["session_id"], blob)["status"] == "active_skip"
        forced = run_findings_pipeline(conn, blob["session_id"], blob, force=True)
        assert forced["status"] == "rebuilt"

    def test_both_engines_emit_rows_for_mixed_session(self, conn):
        # Single message that contains a github_token (regex_secrets) and
        # an email (regex_pii). The pipeline must persist both rows with
        # the right `engine` identifier so apply / allowlist / preview
        # all dispatch correctly.
        old = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        blob = {
            "session_id": "two-engines",
            "project": "demo",
            "source": "claude",
            "model": "claude-sonnet-4",
            "start_time": old,
            "end_time": old,
            "git_branch": "main",
            "display_title": "mixed",
            "messages": [
                {"role": "user",
                 "content": "deploy ghp_abcdefghijklmnopqrstuvwxyzABCDEF0123 ping alice@example.com",
                 "thinking": "", "tool_uses": []},
            ],
            "stats": {"user_messages": 1, "assistant_messages": 0,
                      "tool_uses": 0, "input_tokens": 1, "output_tokens": 0},
        }
        upsert_sessions(conn, [blob])
        conn.commit()
        result = run_findings_pipeline(conn, blob["session_id"], blob)
        assert result["status"] == "rebuilt"
        engines = {
            r["engine"]
            for r in conn.execute(
                "SELECT engine FROM findings WHERE session_id = ?",
                (blob["session_id"],),
            ).fetchall()
        }
        assert engines == {"regex_secrets", "regex_pii"}

    def test_missing_session_row_is_skipped(self, conn):
        # Scanner iterates over parsed sessions but upsert_sessions may
        # filter some out (e.g. slash-command-only titles). The pipeline
        # must not attempt to write findings rows that would violate the
        # FK to sessions.
        blob = _settled_session("never-inserted")
        result = run_findings_pipeline(conn, blob["session_id"], blob)
        assert result == {"status": "missing_session"}
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
        # force=True must also respect the invariant.
        forced = run_findings_pipeline(conn, blob["session_id"], blob, force=True)
        assert forced == {"status": "missing_session"}

    def test_rebuild_replaces_prior_findings(self, conn):
        blob = _settled_session()
        upsert_sessions(conn, [blob])
        conn.commit()
        run_findings_pipeline(conn, blob["session_id"], blob)
        initial_count = conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0]

        # Remove the secret entirely; rebuild should delete prior findings.
        mutated = dict(blob)
        mutated["messages"] = [
            {"role": "user", "content": "clean text", "thinking": "", "tool_uses": []},
        ]
        result = run_findings_pipeline(conn, blob["session_id"], mutated)
        assert result["status"] == "rebuilt"
        assert initial_count > 0
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0


class TestDrainBackfill:
    def test_processes_flagged_sessions(self, conn, tmp_path, monkeypatch):
        # Two settled sessions with blobs on disk; flag both.
        sessions = [_settled_session(f"sess-{i}") for i in range(2)]
        upsert_sessions(conn, sessions)
        conn.execute("UPDATE sessions SET findings_backfill_needed = 1")
        conn.commit()

        report = drain_findings_backfill(conn, config={})
        assert report["processed"] == 2
        assert report["missing_blob"] == 0
        assert report["errored"] == 0
        # Flags cleared; findings written.
        remaining = conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE findings_backfill_needed = 1"
        ).fetchone()[0]
        assert remaining == 0
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] >= 2

    def test_missing_blob_clears_flag(self, conn, tmp_path):
        """A flagged row whose blob is gone off disk still gets the flag cleared."""
        blob = _settled_session("orphan")
        upsert_sessions(conn, [blob])
        # Delete the blob file after upsert wrote it.
        (tmp_path / "blobs" / "orphan.json").unlink()
        conn.execute("UPDATE sessions SET findings_backfill_needed = 1")
        conn.commit()

        report = drain_findings_backfill(conn, config={})
        assert report["processed"] == 0
        assert report["missing_blob"] == 1
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE findings_backfill_needed = 1"
        ).fetchone()[0] == 0

    def test_empty_when_nothing_flagged(self, conn):
        report = drain_findings_backfill(conn, config={})
        assert report == {"processed": 0, "missing_blob": 0, "errored": 0}

    def test_safe_logging_omits_session_path_exception_and_traceback(
        self, conn, monkeypatch, caplog
    ):
        session = _settled_session("RAW_BACKFILL_SESSION_SENTINEL")
        session["project"] = "/private/backfill/project-sentinel"
        upsert_sessions(conn, [session])
        conn.execute("UPDATE sessions SET findings_backfill_needed = 1")
        conn.commit()

        def fail_pipeline(*args, **kwargs):
            assert kwargs["safe_logging"] is True
            raise RuntimeError(
                "RAW_BACKFILL_EXCEPTION_SENTINEL /private/backfill/project-sentinel"
            )

        monkeypatch.setattr(
            "clawjournal.workbench.findings_pipeline.run_findings_pipeline",
            fail_pipeline,
        )
        caplog.set_level(logging.DEBUG)

        report = drain_findings_backfill(
            conn, config={}, progress_every=1, safe_logging=True
        )

        assert report == {"processed": 0, "missing_blob": 0, "errored": 1}
        assert "backfill_failed" in caplog.text
        assert "RAW_BACKFILL_SESSION_SENTINEL" not in caplog.text
        assert "RAW_BACKFILL_EXCEPTION_SENTINEL" not in caplog.text
        assert "/private/backfill/project-sentinel" not in caplog.text
        assert "Traceback" not in caplog.text
        assert all(record.exc_info is None for record in caplog.records)
