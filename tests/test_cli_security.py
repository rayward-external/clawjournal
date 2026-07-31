"""Tests for the security-refactor CLI handlers in clawjournal.cli_security."""

from __future__ import annotations

import argparse
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone

import pytest

from clawjournal import cli_security
from clawjournal.findings import (
    hash_entity,
    reset_salt_cache,
    write_findings_to_db,
)
from clawjournal.redaction.secrets import scan_session_for_findings
from clawjournal.workbench.index import (
    effective_hold_state,
    get_hold_history,
    open_index,
    set_hold_state,
    upsert_sessions,
)


_FAKE_JWT = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    ".eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkphbmUifQ"
    ".abcdefghijABCDEFGH0123456789"
)


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    reset_salt_cache()
    connection = open_index()
    yield connection
    connection.close()
    reset_salt_cache()


def _settled_session(session_id="sess-1", content=None):
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
            {"role": "user", "content": content or f"secret: {_FAKE_JWT}",
             "thinking": "", "tool_uses": []},
        ],
        "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0,
                  "input_tokens": 1, "output_tokens": 0},
    }


def _seed(conn, session_id="sess-1"):
    sess = _settled_session(session_id)
    upsert_sessions(conn, [sess])
    raw = scan_session_for_findings(sess)
    write_findings_to_db(conn, session_id, raw, revision="v1:test")
    conn.commit()


def _run(handler, **ns_kwargs) -> dict:
    buf = io.StringIO()
    args = argparse.Namespace(**ns_kwargs)
    with redirect_stdout(buf):
        handler(args)
    return json.loads(buf.getvalue())


class TestHoldVerbs:
    def test_hold_transitions_and_writes_history(self, conn):
        _seed(conn)
        result = _run(cli_security.run_hold, session_id="sess-1", reason="reviewing")
        assert result["hold_state"] == "pending_review"
        history = get_hold_history(conn, "sess-1")
        assert [h["to_state"] for h in history] == ["auto_redacted", "pending_review"]
        assert history[-1]["changed_by"] == "user"
        assert history[-1]["reason"] == "reviewing"

    def test_release(self, conn):
        _seed(conn)
        result = _run(cli_security.run_release, session_id="sess-1", reason=None)
        assert result["hold_state"] == "released"

    def test_embargo_accepts_date_and_validates_future(self, conn):
        _seed(conn)
        future = (datetime.now(timezone.utc) + timedelta(days=14)).strftime("%Y-%m-%d")
        result = _run(
            cli_security.run_embargo,
            session_id="sess-1", until=future, reason="Q2 release",
        )
        assert result["hold_state"] == "embargoed"
        assert result["embargo_until"].startswith(future)

    def test_embargo_past_date_is_rejected(self, conn):
        _seed(conn)
        past = "2020-01-01"
        buf = io.StringIO()
        with pytest.raises(SystemExit):
            with redirect_stdout(buf):
                cli_security.run_embargo(argparse.Namespace(
                    session_id="sess-1", until=past, reason=None,
                ))
        payload = json.loads(buf.getvalue())
        assert "future" in payload["error"]

    def test_missing_session_fails(self, conn):
        with pytest.raises(SystemExit):
            with redirect_stdout(io.StringIO()):
                cli_security.run_hold(argparse.Namespace(
                    session_id="nope", reason=None,
                ))


class TestHoldHistoryVerb:
    def test_prints_timeline(self, conn):
        _seed(conn)
        set_hold_state(conn, "sess-1", "pending_review", changed_by="user", reason="r")
        set_hold_state(conn, "sess-1", "released", changed_by="user", reason="ok")
        result = _run(cli_security.run_hold_history, session_id="sess-1")
        states = [h["to_state"] for h in result["history"]]
        assert states == ["auto_redacted", "pending_review", "released"]


class TestEffectiveHoldState:
    def test_embargo_past_due_is_released(self):
        past = "2020-01-01T00:00:00+00:00"
        assert effective_hold_state("embargoed", past) == "released"

    def test_embargo_future_stays_embargoed(self):
        future = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
        assert effective_hold_state("embargoed", future) == "embargoed"

    def test_non_embargo_state_passes_through(self):
        assert effective_hold_state("released", None) == "released"

    def test_missing_state_fails_closed(self):
        assert effective_hold_state(None, None) == "pending_review"


class TestFindingsVerb:
    def test_list_default_hides_decided(self, conn):
        _seed(conn)
        # Flip everything to ignored first.
        conn.execute("UPDATE findings SET status='ignored', decided_by='user'")
        conn.commit()
        result = _run(cli_security.run_findings,
                      session_id="sess-1", all=False,
                      accept=None, ignore=None,
                      accept_all=False, ignore_all=False,
                      accept_engine=None, ignore_engine=None,
                      reason=None, global_=False)
        assert result["total"] == 0

    def test_list_all_shows_decided(self, conn):
        _seed(conn)
        conn.execute("UPDATE findings SET status='ignored', decided_by='user'")
        conn.commit()
        result = _run(cli_security.run_findings,
                      session_id="sess-1", all=True,
                      accept=None, ignore=None,
                      accept_all=False, ignore_all=False,
                      accept_engine=None, ignore_engine=None,
                      reason=None, global_=False)
        assert result["total"] >= 1
        assert "entity_hash_prefix" in result["entities"][0]
        assert "preview" in result["entities"][0]

    def test_accept_by_hash_prefix(self, conn):
        _seed(conn)
        prefix = hash_entity(_FAKE_JWT)[:10]
        result = _run(cli_security.run_findings,
                      session_id="sess-1", all=False,
                      accept=[prefix], ignore=None,
                      accept_all=False, ignore_all=False,
                      accept_engine=None, ignore_engine=None,
                      reason="mine", global_=False)
        assert result["status"] == "accepted"
        assert result["updated"] >= 1
        rows = conn.execute("SELECT status FROM findings").fetchall()
        assert all(r["status"] == "accepted" for r in rows)

    def test_accept_all(self, conn):
        _seed(conn)
        result = _run(cli_security.run_findings,
                      session_id="sess-1", all=False,
                      accept=None, ignore=None,
                      accept_all=True, ignore_all=False,
                      accept_engine=None, ignore_engine=None,
                      reason=None, global_=False)
        assert result["status"] == "accepted"
        assert result["updated"] >= 1

    def test_ignore_engine_only(self, conn):
        _seed(conn)
        result = _run(cli_security.run_findings,
                      session_id="sess-1", all=False,
                      accept=None, ignore=None,
                      accept_all=False, ignore_all=False,
                      accept_engine=None, ignore_engine="regex_secrets",
                      reason=None, global_=False)
        assert result["status"] == "ignored"
        statuses = {r["status"] for r in conn.execute("SELECT status FROM findings").fetchall()}
        assert statuses == {"ignored"}

    def test_conflicting_flags_rejected(self, conn):
        _seed(conn)
        with pytest.raises(SystemExit):
            with redirect_stdout(io.StringIO()):
                cli_security.run_findings(argparse.Namespace(
                    session_id="sess-1", all=False,
                    accept=["abcd"], ignore=None,
                    accept_all=True, ignore_all=False,
                    accept_engine=None, ignore_engine=None,
                    reason=None, global_=False,
                ))

    def test_unknown_ref_errors(self, conn):
        _seed(conn)
        with pytest.raises(SystemExit):
            with redirect_stdout(io.StringIO()):
                cli_security.run_findings(argparse.Namespace(
                    session_id="sess-1", all=False,
                    accept=["ffffffffffff"], ignore=None,
                    accept_all=False, ignore_all=False,
                    accept_engine=None, ignore_engine=None,
                    reason=None, global_=False,
                ))


class TestAllowlistVerb:
    def test_add_then_list_then_remove(self, conn):
        added = _run(cli_security.run_allowlist,
                     op="add", entity_text="noreply@example.com",
                     type="email", label="bot", reason="org policy")
        allowlist_id = added["entry"]["allowlist_id"]
        listed = _run(cli_security.run_allowlist,
                      op="list")
        assert allowlist_id in {e["allowlist_id"] for e in listed["entries"]}

        removed = _run(cli_security.run_allowlist,
                       op="remove", allowlist_id=allowlist_id,
                       by_text=None, type=None)
        assert removed["removed"] is True

    def test_remove_by_text(self, conn):
        _run(cli_security.run_allowlist,
             op="add", entity_text="x@y.z",
             type="email", label=None, reason=None)
        out = _run(cli_security.run_allowlist,
                   op="remove", allowlist_id=None,
                   by_text="x@y.z", type="email")
        assert len(out["removed"]) == 1


class TestScanForce:
    def test_force_rebuilds_by_id(self, conn, tmp_path):
        _seed(conn)
        # Drop findings, then force-rebuild; row count should come back.
        conn.execute("DELETE FROM findings")
        conn.execute("UPDATE sessions SET findings_revision = NULL")
        conn.commit()
        result = _run(cli_security.run_scan_force,
                      session_ids=["sess-1"], all=False)
        assert result["processed"] == 1
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] >= 1

    def test_force_all(self, conn):
        _seed(conn, "sess-a")
        _seed(conn, "sess-b")
        conn.execute("DELETE FROM findings")
        conn.commit()
        result = _run(cli_security.run_scan_force,
                      session_ids=[], all=True)
        assert result["processed"] == 2

    def test_requires_target(self, conn):
        with pytest.raises(SystemExit):
            with redirect_stdout(io.StringIO()):
                cli_security.run_scan_force(argparse.Namespace(
                    session_ids=[], all=False,
                ))


class TestLegacyPIINotice:
    def test_notice_is_emitted_to_stderr(self):
        buf = io.StringIO()
        with redirect_stderr(buf):
            cli_security.emit_legacy_pii_notice()
        assert "pii-review/pii-apply" in buf.getvalue()
        assert "LLM-PII" in buf.getvalue()

    def test_notice_goes_nowhere_near_stdout(self):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            cli_security.emit_legacy_pii_notice()
        assert out.getvalue() == ""
        assert err.getvalue().strip() != ""
