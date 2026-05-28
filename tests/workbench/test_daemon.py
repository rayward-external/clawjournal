"""Tests for the workbench daemon HTTP API."""

import json
import time
import urllib.error
import zipfile
from datetime import datetime, timedelta, timezone
from http.client import HTTPConnection
from io import BytesIO
from threading import Lock, Thread
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from clawjournal.workbench.daemon import (
    Scanner,
    WorkbenchHandler,
    run_server,
    _SHARE_COOLDOWN_SECONDS,
    _apply_upload_pii_redactions,
)
from clawjournal.workbench.index import open_index, upsert_sessions


@pytest.fixture
def index_setup(tmp_path, monkeypatch):
    """Set up an index DB in a temp directory and seed it."""
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
    monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")
    monkeypatch.setattr("clawjournal.workbench.daemon.FRONTEND_DIST", tmp_path / "nonexistent_dist")
    monkeypatch.setattr("clawjournal.workbench.daemon._SHARE_INGEST_URL", "https://test-ingest.example.com")
    monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "")
    monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
    # Mock PII review in share tests — no AI backend available in test env
    monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_hybrid", lambda session, **kw: ([], "full") if kw.get("return_coverage") else [])

    conn = open_index()
    sessions = [
        {
            "session_id": f"sess-{i}",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "start_time": f"2025-01-0{i+1}T00:00:00+00:00",
            "end_time": f"2025-01-0{i+1}T00:10:00+00:00",
            "messages": [
                {"role": "user", "content": f"Task {i}: fix the bug", "tool_uses": []},
                {"role": "assistant", "content": "Done.", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1, "assistant_messages": 1,
                "tool_uses": 0, "input_tokens": 100, "output_tokens": 50,
            },
        }
        for i in range(3)
    ]
    upsert_sessions(conn, sessions)
    conn.close()
    return tmp_path


@pytest.fixture
def server(index_setup):
    """Start a test HTTP server."""
    from http.server import ThreadingHTTPServer
    srv = ThreadingHTTPServer(("127.0.0.1", 0), WorkbenchHandler)
    port = srv.server_address[1]
    thread = Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield port
    srv.shutdown()


def _api_auth_headers() -> dict[str, str]:
    """Read the per-install API token from ~/.clawjournal/api_token.

    The test fixture monkeypatches `INDEX_DB` to the tmp path, and
    `open_index()` bootstraps the token file there. We read it
    directly — same path the daemon's auth check uses.
    """
    from pathlib import Path
    from clawjournal.paths import API_TOKEN_FILENAME
    from clawjournal.workbench.index import INDEX_DB
    token_path = Path(str(INDEX_DB)).parent / API_TOKEN_FILENAME
    return {"Authorization": f"Bearer {token_path.read_text().strip()}"}


def _get(port, path, *, skip_auth=False):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {} if skip_auth else _api_auth_headers()
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    body = resp.read().decode()
    return resp.status, json.loads(body) if resp.getheader("Content-Type", "").startswith("application/json") else body


def _get_raw(port, path, *, skip_auth=False):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {} if skip_auth else _api_auth_headers()
    conn.request("GET", path, headers=headers)
    resp = conn.getresponse()
    return resp.status, resp.getheader("Content-Type", ""), resp.read()


def _post(port, path, data=None, *, skip_auth=False):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(data or {}).encode()
    headers = {"Content-Type": "application/json"}
    if not skip_auth:
        headers.update(_api_auth_headers())
    conn.request("POST", path, body=body, headers=headers)
    resp = conn.getresponse()
    resp_body = resp.read().decode()
    return resp.status, json.loads(resp_body) if resp.getheader("Content-Type", "").startswith("application/json") else resp_body


def _patch(port, path, data=None, *, skip_auth=False):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    body = json.dumps(data or {}).encode()
    headers = {"Content-Type": "application/json"}
    if not skip_auth:
        headers.update(_api_auth_headers())
    conn.request("PATCH", path, body=body, headers=headers)
    resp = conn.getresponse()
    resp_body = resp.read().decode()
    return resp.status, json.loads(resp_body) if resp.getheader("Content-Type", "").startswith("application/json") else resp_body


def _delete(port, path, *, skip_auth=False):
    conn = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {} if skip_auth else _api_auth_headers()
    conn.request("DELETE", path, headers=headers)
    resp = conn.getresponse()
    resp_body = resp.read().decode()
    return resp.status, json.loads(resp_body) if resp.getheader("Content-Type", "").startswith("application/json") else resp_body


def _seed_timeline(index_setup):
    from urllib.parse import quote

    from clawjournal.events.cost.schema import ensure_cost_schema
    from clawjournal.events.incidents.schema import ensure_incidents_schema
    from clawjournal.events.schema import ensure_schema as ensure_events_schema
    from clawjournal.events.view import ensure_view_schema

    conn = open_index()
    try:
        ensure_events_schema(conn)
        ensure_view_schema(conn)
        ensure_cost_schema(conn)
        ensure_incidents_schema(conn)

        vendor_file = index_setup / "timeline_vendor.jsonl"
        vendor_file.write_text(
            '{"type":"user_message","message":{"content":"debug the failing cache auth flow"}}\n'
            '{"type":"tool_call","tool_name":"Bash","command":"pytest tests/test_auth.py"}\n'
            '{"type":"tool_result","output":"401 unauthorized"}\n',
            encoding="utf-8",
        )
        root_key = "claude:demo-proj:parent-root"
        child_key = "claude:demo-proj:child-agent"
        conn.execute(
            "UPDATE sessions SET session_key = ?, display_title = ? WHERE session_id = ?",
            (root_key, "Demo timeline session", "sess-0"),
        )
        root_id = conn.execute(
            """
            INSERT INTO event_sessions (
                session_key, parent_session_key, client, started_at, ended_at, status
            ) VALUES (?, NULL, 'claude', '2026-04-22T10:00:00Z', '2026-04-22T10:08:00Z', 'closed')
            """,
            (root_key,),
        ).lastrowid
        child_id = conn.execute(
            """
            INSERT INTO event_sessions (
                session_key, parent_session_key, parent_session_id, client,
                started_at, ended_at, status
            ) VALUES (?, ?, ?, 'claude', '2026-04-22T10:04:00Z', '2026-04-22T10:05:00Z', 'closed')
            """,
            (child_key, root_key, root_id),
        ).lastrowid
        first_id = conn.execute(
            """
            INSERT INTO events (
                session_id, type, event_key, event_at, ingested_at, source,
                source_path, source_offset, seq, client, confidence, lossiness, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                "user_message",
                "user_message:1",
                "2026-04-22T10:00:00Z",
                "2026-04-22T10:00:01Z",
                "claude-jsonl",
                str(vendor_file),
                0,
                0,
                "claude",
                "high",
                "none",
                '{"message":{"content":"debug the failing cache auth flow"}}',
            ),
        ).lastrowid
        second_id = conn.execute(
            """
            INSERT INTO events (
                session_id, type, event_key, event_at, ingested_at, source,
                source_path, source_offset, seq, client, confidence, lossiness, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                "tool_call",
                "tool_call:1",
                "2026-04-22T10:00:03Z",
                "2026-04-22T10:00:04Z",
                "claude-jsonl",
                str(vendor_file),
                82,
                0,
                "claude",
                "high",
                "partial",
                '{"tool_name":"Bash","command":"pytest tests/test_auth.py"}',
            ),
        ).lastrowid
        conn.execute(
            """
            INSERT INTO events (
                session_id, type, event_key, event_at, ingested_at, source,
                source_path, source_offset, seq, client, confidence, lossiness, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                "tool_result",
                "tool_result:1",
                "2026-04-22T10:00:05Z",
                "2026-04-22T10:00:06Z",
                "claude-jsonl",
                str(vendor_file),
                153,
                0,
                "claude",
                "medium",
                "none",
                '{"output":"401 unauthorized"}',
            ),
        )
        conn.execute(
            """
            INSERT INTO events (
                session_id, type, event_key, event_at, ingested_at, source,
                source_path, source_offset, seq, client, confidence, lossiness, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                child_id,
                "tool_call",
                "tool_call:child",
                "2026-04-22T10:04:01Z",
                "2026-04-22T10:04:02Z",
                "hook",
                str(vendor_file),
                200,
                0,
                "claude",
                "high",
                "none",
                '{"tool_name":"Read","path":"README.md"}',
            ),
        )
        conn.execute(
            """
            INSERT INTO token_usage (
                event_id, session_id, model, service_tier, data_source, input, output,
                cache_read, cache_write, reasoning, cost_estimate, event_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                second_id,
                root_id,
                "claude-sonnet-4",
                "standard",
                "api",
                120,
                42,
                10,
                0,
                8,
                0.0137,
                "2026-04-22T10:00:03Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO cost_anomalies (
                session_id, turn_event_id, kind, confidence, evidence_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                second_id,
                "cache_read_collapse",
                "medium",
                '{"before": 200, "after": 10}',
                "2026-04-22T10:00:08Z",
            ),
        )
        conn.execute(
            """
            INSERT INTO incidents (
                session_id, kind, first_event_id, last_event_id,
                evidence_json, count, confidence, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                root_id,
                "loop_exact_repeat",
                second_id,
                second_id,
                '{"fingerprint":"pytest tests/test_auth.py"}',
                3,
                "medium",
                "2026-04-22T10:00:09Z",
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "root_key": root_key,
        "child_key": child_key,
        "root_url": f"/timeline/{quote(root_key, safe='')}",
        "child_url": f"/timeline/{quote(child_key, safe='')}",
        "legacy_url": "/timeline/sess-0",
        "spa_session_url": "/session/sess-0",
        "first_event_id": int(first_id),
        "second_event_id": int(second_id),
    }


class TestSessionsAPI:
    def test_list_sessions(self, server):
        status, data = _get(server, "/api/sessions")
        assert status == 200
        assert len(data) == 3

    def test_list_sessions_with_limit(self, server):
        status, data = _get(server, "/api/sessions?limit=2")
        assert status == 200
        assert len(data) == 2

    def test_get_session_detail(self, server):
        status, data = _get(server, "/api/sessions/sess-0")
        assert status == 200
        assert data["session_id"] == "sess-0"
        assert "messages" in data

    def test_get_session_not_found(self, server):
        status, data = _get(server, "/api/sessions/nonexistent")
        assert status == 404

    def test_redaction_report_applies_policy_rules(self, server, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})

        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "policy-sess",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "MySecretName and PartnerDev use api.foo.internal", "tool_uses": []},
                {"role": "assistant", "content": "PartnerDev confirmed api.foo.internal is live.", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1,
                "assistant_messages": 1,
                "tool_uses": 0,
                "input_tokens": 100,
                "output_tokens": 50,
            },
        }])
        conn.close()

        assert _post(server, "/api/policies", {
            "policy_type": "redact_string",
            "value": "MySecretName",
        })[0] == 201
        assert _post(server, "/api/policies", {
            "policy_type": "redact_username",
            "value": "PartnerDev",
        })[0] == 201
        assert _post(server, "/api/policies", {
            "policy_type": "block_domain",
            "value": "*.internal",
        })[0] == 201

        status, data = _get(server, "/api/sessions/policy-sess/redaction-report")
        assert status == 200
        redacted = json.dumps(data["redacted_session"])
        assert "MySecretName" not in redacted
        assert "PartnerDev" not in redacted
        assert "foo.internal" not in redacted
        assert any(entry["type"] == "blocked_domain" for entry in data["redaction_log"])

    def test_update_session_status(self, server):
        status, data = _post(server, "/api/sessions/sess-0", {"status": "approved"})
        assert status == 200
        assert data["ok"] is True

        # Verify it persisted
        status, detail = _get(server, "/api/sessions/sess-0")
        assert detail["review_status"] == "approved"

    def test_update_session_requires_evidence_for_high_failure_value(self, server):
        status, data = _post(server, "/api/sessions/sess-0", {"ai_failure_value_score": 4})

        assert status == 400
        assert "require evidence" in data["error"]

    def test_update_session_stores_failure_evidence_for_high_failure_value(self, server):
        status, data = _post(server, "/api/sessions/sess-0", {
            "ai_failure_value_score": 4,
            "ai_failure_evidence": ["The user corrected a fabricated API call."],
        })
        assert status == 200
        assert data["ok"] is True

        status, detail = _get(server, "/api/sessions/sess-0")
        assert status == 200
        assert detail["ai_failure_value_score"] == 4
        assert json.loads(detail["ai_scoring_detail"]) == {
            "ai_failure_evidence": ["The user corrected a fabricated API call."],
        }

    def test_score_session_endpoint_updates_session(self, server, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.scoring.scoring.score_session",
            lambda conn, session_id, model=None, backend="auto": SimpleNamespace(
                quality=4,
                reason="Solid debugging session",
                detail_json='{"substance": 4}',
                task_type="debugging",
                outcome_label="completed",
                value_labels=["tool_rich"],
                risk_level=[],
                display_title="Scored title",
                effort_estimate=2.0,
                summary="Good progress",
            ),
        )

        status, data = _post(server, "/api/sessions/sess-0/score", {"backend": "auto"})
        assert status == 200
        assert data["ok"] is True
        assert data["ai_quality_score"] == 4

        status, detail = _get(server, "/api/sessions/sess-0")
        assert status == 200
        assert detail["ai_quality_score"] == 4
        assert detail["ai_summary"] == "Good progress"

    def test_score_session_endpoint_rejects_missing_transcript_blob(self, server, index_setup):
        (index_setup / "blobs" / "sess-0.json").unlink()

        status, data = _post(server, "/api/sessions/sess-0/score")
        assert status == 503
        assert "Re-run `clawjournal scan`" in data["error"]


class TestStatsAPI:
    def test_stats(self, server):
        status, data = _get(server, "/api/stats")
        assert status == 200
        assert data["total"] == 3
        assert "by_status" in data
        assert "by_source" in data


class TestScanner:
    def test_scan_once_links_subagent_hierarchy(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        seen = {}

        def fake_discover_projects(source_filter=None):
            seen["source_filter"] = source_filter
            return []

        monkeypatch.setattr("clawjournal.workbench.daemon.discover_projects", fake_discover_projects)

        called = {}

        def fake_link(conn):
            called["linked"] = True
            return 3

        monkeypatch.setattr("clawjournal.workbench.daemon.link_subagent_hierarchy", fake_link)

        scanner = Scanner(source_filter="cursor")
        results = scanner.scan_once()

        assert results == {}
        assert seen["source_filter"] == "cursor"
        assert called["linked"] is True
        assert scanner.last_linked_count == 3

    def test_score_unscored_once_uses_default_agent_scoring(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")

        conn = open_index()
        now = datetime.now(timezone.utc)
        upsert_sessions(conn, [{
            "session_id": "sess-1",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "start_time": now.isoformat(),
            "end_time": (now + timedelta(minutes=10)).isoformat(),
            "messages": [
                {"role": "user", "content": "Fix it", "tool_uses": []},
                {"role": "assistant", "content": "Done", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1, "assistant_messages": 1,
                "tool_uses": 0, "input_tokens": 100, "output_tokens": 50,
            },
        }])
        conn.close()

        monkeypatch.setattr(
            "clawjournal.scoring.scoring.score_session",
            lambda conn, session_id, model=None, backend="auto": SimpleNamespace(
                quality=5,
                reason="Strong trace",
                detail_json='{"substance": 5}',
                task_type="debugging",
                outcome_label="resolved",
                value_labels=["tool_rich"],
                risk_level=[],
                display_title="Great trace",
                effort_estimate=0.8,
                summary="Useful fix",
                failure_value_score=5,
                recovery_labels=["user_corrected_recovery"],
                failure_attribution="agent_caused",
                failure_modes=["reasoning_fabrication"],
                learning_summary="Useful failure trace",
                scorer_backend="test",
                scorer_model="test-model",
                rubric_git_sha="test-sha",
                scored_at=now.isoformat(),
            ),
        )

        scanner = Scanner(source_filter="claude")
        scored = scanner.score_unscored_once(limit=5)
        assert scored == 1

        conn = open_index()
        row = conn.execute(
            "SELECT ai_quality_score, ai_score_reason, ai_summary FROM sessions WHERE session_id = ?",
            ("sess-1",),
        ).fetchone()
        conn.close()
        assert row["ai_quality_score"] == 5
        assert row["ai_score_reason"] == "Strong trace"
        assert row["ai_summary"] == "Useful fix"

    def test_score_unscored_once_skips_sessions_outside_recent_window(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")

        old = datetime.now(timezone.utc) - timedelta(days=30)
        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "old-sess",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "start_time": old.isoformat(),
            "end_time": (old + timedelta(minutes=10)).isoformat(),
            "messages": [
                {"role": "user", "content": "Fix it", "tool_uses": []},
                {"role": "assistant", "content": "Done", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1, "assistant_messages": 1,
                "tool_uses": 0, "input_tokens": 100, "output_tokens": 50,
            },
        }])
        conn.close()

        calls = {"count": 0}

        def fake_score(*args, **kwargs):
            calls["count"] += 1
            raise AssertionError("old sessions should not be scored")

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", fake_score)

        scanner = Scanner(source_filter="claude")
        assert scanner.score_unscored_once(limit=5) == 0
        assert calls["count"] == 0


class TestProjectsAPI:
    def test_projects(self, server):
        status, data = _get(server, "/api/projects")
        assert status == 200
        assert len(data) >= 1
        assert data[0]["project"] == "test-project"


class TestShareDestinationAPI:
    def test_packaged_default_points_to_rayward_research(self):
        from clawjournal.workbench.daemon import _HOSTED_SHARE_URL_DEFAULT

        assert _HOSTED_SHARE_URL_DEFAULT == "https://data.rayward.ai/share"

    def test_unconfigured_share_destination(self, server, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "")

        status, data = _get(server, "/api/share-destination")

        assert status == 200
        assert data["configured"] is False
        assert data["preferred_upload_flow"] == "browser_zip"
        assert data["cli_ingest_supported"] is False
        assert data["share_page_url"] is None

    def test_configured_share_destination(self, server, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.workbench.daemon._HOSTED_SHARE_URL",
            "https://data.rayward.ai/share",
        )

        status, data = _get(server, "/api/share-destination")

        assert status == 200
        assert data["configured"] is True
        assert data["preferred_upload_flow"] == "browser_zip"
        assert data["cli_ingest_supported"] is False
        assert data["share_page_url"] == "https://data.rayward.ai/share"

    def test_configured_share_destination_uses_hosted_capabilities(self, server, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.workbench.daemon._HOSTED_SHARE_URL",
            "https://hosted.example.test/share",
        )
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)
        capabilities = {
            "submissions_open": False,
            "preferred_upload_flow": "daemon_zip",
            "cli_ingest_supported": False,
            "share_page_url": "https://hosted.example.test/share",
            "submit_page_url": "https://hosted.example.test/submit",
            "maximum_bundle_size": 12345,
            "accepted_manifest_schema_versions": ["1.0.0", "1.1.0"],
            "contact_email": "support@example.test",
            "cache_seconds": 0,
        }

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(capabilities=capabilities),
        ):
            status, data = _get(server, "/api/share-destination")

        assert status == 200
        assert data["configured"] is True
        assert data["daemon_upload_supported"] is True
        assert data["submissions_open"] is False
        assert data["preferred_upload_flow"] == "daemon_zip"
        assert data["submit_page_url"] == "https://hosted.example.test/submit"
        assert data["maximum_bundle_size"] == 12345
        assert data["accepted_manifest_schema_versions"] == ["1.0.0", "1.1.0"]
        assert data["support_contact"] == "support@example.test"

    def test_invalid_share_destination_is_disabled(self, server, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.workbench.daemon._HOSTED_SHARE_URL",
            "data.rayward.ai/share",
        )

        status, data = _get(server, "/api/share-destination")

        assert status == 200
        assert data["configured"] is False
        assert data["share_page_url"] is None
        assert "HTTPS" in data["message"]

    def test_prefix_lookalike_localhost_share_destination_is_disabled(self, server, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.workbench.daemon._HOSTED_SHARE_URL",
            "http://localhost.evil.test/share",
        )

        status, data = _get(server, "/api/share-destination")

        assert status == 200
        assert data["configured"] is False
        assert data["share_page_url"] is None


class TestSharesAPI:
    def test_create_and_list(self, server):
        status, data = _post(server, "/api/shares", {
            "session_ids": ["sess-0", "sess-1"],
            "note": "Test share",
        })
        assert status == 201
        assert "share_id" in data
        assert data["bundle_id"] == data["share_id"]

        status, shares = _get(server, "/api/shares")
        assert status == 200
        assert len(shares) == 1
        assert shares[0]["bundle_id"] == shares[0]["share_id"]

    def test_legacy_bundle_routes_remain_available(self, server):
        status, created = _post(server, "/api/bundles", {
            "session_ids": ["sess-0"],
            "note": "Legacy bundle route",
        })
        assert status == 201
        assert created["bundle_id"] == created["share_id"]

        share_id = created["share_id"]
        status, bundles = _get(server, "/api/bundles")
        assert status == 200
        assert bundles[0]["bundle_id"] == share_id

        status, detail = _get(server, f"/api/bundles/{share_id}")
        assert status == 200
        assert detail["bundle_id"] == share_id

    def test_create_empty_fails(self, server):
        status, data = _post(server, "/api/shares", {"session_ids": []})
        assert status == 400


class TestPoliciesAPI:
    def test_add_and_list(self, server):
        status, data = _post(server, "/api/policies", {
            "policy_type": "redact_string",
            "value": "my-secret",
            "reason": "API key",
        })
        assert status == 201

        status, policies = _get(server, "/api/policies")
        assert status == 200
        assert len(policies) == 1

    def test_add_missing_fields(self, server):
        status, data = _post(server, "/api/policies", {"policy_type": "redact_string"})
        assert status == 400


class TestStaticServing:
    def test_placeholder_when_no_frontend(self, server):
        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200
        assert "ClawJournal Workbench" in body

    def test_serves_built_frontend_and_spa_fallback(self, server, index_setup, monkeypatch):
        dist = index_setup / "frontend_dist"
        dist.mkdir()
        (dist / "index.html").write_text("<!DOCTYPE html><title>Built UI</title>", encoding="utf-8")
        (dist / "app.js").write_text("console.log('ok');", encoding="utf-8")
        monkeypatch.setattr("clawjournal.workbench.daemon.FRONTEND_DIST", dist)
        seeded = _seed_timeline(index_setup)

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200
        assert "Built UI" in body

        # `/session/<id>` is owned by the SPA's client-side router — the
        # daemon must keep serving the SPA shell there, not the timeline.
        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", seeded["spa_session_url"])
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200
        assert "Built UI" in body

        # `/timeline/<legacy_workbench_id>` is the timeline's own surface;
        # the legacy id is resolved to its canonical session_key and the
        # client is redirected to the canonical timeline URL.
        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", seeded["legacy_url"], headers=_api_auth_headers())
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 302
        assert resp.getheader("Location") == seeded["root_url"]

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", seeded["root_url"], headers=_api_auth_headers())
        resp = conn.getresponse()
        body = resp.read().decode()
        assert resp.status == 200
        assert "Session Timeline" in body
        assert "Built UI" not in body


class TestTimelineRoute:
    def test_session_timeline_renders_cost_incidents_and_subagents(self, server, index_setup):
        seeded = _seed_timeline(index_setup)

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", seeded["root_url"], headers=_api_auth_headers())
        resp = conn.getresponse()
        body = resp.read().decode()

        assert resp.status == 200
        assert "Session Timeline" in body
        assert f'id="event-{seeded["second_event_id"]}"' in body
        assert "cache_read_collapse" in body
        assert "loop_exact_repeat" in body
        assert "Subagent session" in body
        assert "Not captured by this client" in body
        assert "Captured but lossy" in body
        assert "Captured directly" in body

    def test_child_session_route_redirects_to_parent_timeline(self, server, index_setup):
        seeded = _seed_timeline(index_setup)

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", seeded["child_url"], headers=_api_auth_headers())
        resp = conn.getresponse()
        resp.read()

        assert resp.status == 302
        assert resp.getheader("Location") == seeded["root_url"]

    def test_unknown_session_key_returns_404_with_not_found_page(self, server, index_setup):
        _seed_timeline(index_setup)

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request(
            "GET",
            "/timeline/claude:nobody:unknown",
            headers=_api_auth_headers(),
        )
        resp = conn.getresponse()
        body = resp.read().decode()

        assert resp.status == 404
        assert "claude:nobody:unknown" in body

    def test_legacy_workbench_id_without_session_key_returns_404(
        self, server, index_setup
    ):
        _seed_timeline(index_setup)

        conn = open_index()
        try:
            conn.execute(
                "UPDATE sessions SET session_key = NULL WHERE session_id = 'sess-1'"
            )
            conn.commit()
        finally:
            conn.close()

        http_conn = HTTPConnection("127.0.0.1", server, timeout=5)
        http_conn.request("GET", "/timeline/sess-1", headers=_api_auth_headers())
        resp = http_conn.getresponse()
        body = resp.read().decode()

        assert resp.status == 404
        assert "sess-1" in body

    def test_html_escapes_event_content_to_block_xss(self, server, index_setup):
        seeded = _seed_timeline(index_setup)

        conn = open_index()
        try:
            conn.execute(
                """
                INSERT INTO events (
                    session_id, type, event_key, event_at, ingested_at, source,
                    source_path, source_offset, seq, client, confidence,
                    lossiness, raw_json
                ) VALUES (
                    (SELECT id FROM event_sessions WHERE session_key = ?),
                    'tool_call', 'tool_call:xss', '2026-04-22T10:01:00Z',
                    '2026-04-22T10:01:01Z', 'claude-jsonl', 'vendor.jsonl',
                    900, 0, 'claude', 'high', 'none',
                    ?
                )
                """,
                (
                    seeded["root_key"],
                    '{"text":"<script>alert(1)</script>"}',
                ),
            )
            conn.commit()
        finally:
            conn.close()

        http_conn = HTTPConnection("127.0.0.1", server, timeout=5)
        http_conn.request("GET", seeded["root_url"], headers=_api_auth_headers())
        resp = http_conn.getresponse()
        body = resp.read().decode()

        assert resp.status == 200
        assert "<script>alert(1)</script>" not in body
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body

    def test_partially_ingested_child_session_renders_in_place(self, server, index_setup):
        from urllib.parse import quote

        conn = open_index()
        try:
            from clawjournal.events.schema import ensure_schema as ensure_events_schema
            from clawjournal.events.view import ensure_view_schema

            ensure_events_schema(conn)
            ensure_view_schema(conn)
            child_key = "claude:demo-proj:orphan-child"
            missing_parent_key = "claude:demo-proj:missing-parent"
            child_id = conn.execute(
                """
                INSERT INTO event_sessions (
                    session_key, parent_session_key, client, started_at, status
                ) VALUES (?, ?, 'claude', '2026-04-22T11:00:00Z', 'active')
                """,
                (child_key, missing_parent_key),
            ).lastrowid
            conn.execute(
                """
                INSERT INTO events (
                    session_id, type, event_key, event_at, ingested_at, source,
                    source_path, source_offset, seq, client, confidence, lossiness, raw_json
                ) VALUES (?, 'tool_call', 'tool_call:orphan', '2026-04-22T11:00:01Z',
                          '2026-04-22T11:00:02Z', 'hook', 'orphan.jsonl', 0, 0,
                          'claude', 'high', 'none', '{"tool_name":"Read","path":"README.md"}')
                """,
                (child_id,),
            )
            conn.commit()
        finally:
            conn.close()

        http_conn = HTTPConnection("127.0.0.1", server, timeout=5)
        http_conn.request(
            "GET",
            f"/timeline/{quote(child_key, safe='')}",
            headers=_api_auth_headers(),
        )
        resp = http_conn.getresponse()
        body = resp.read().decode()

        assert resp.status == 200
        assert child_key in body
        assert missing_parent_key not in body

    def test_spa_html_sets_httponly_session_cookie(self, server):
        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()

        set_cookie = resp.getheader("Set-Cookie") or ""
        assert "clawjournal_token=" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "SameSite=Strict" in set_cookie
        assert "Path=/timeline" in set_cookie

    def test_timeline_route_accepts_cookie_auth(self, server, index_setup):
        seeded = _seed_timeline(index_setup)

        from clawjournal.paths import API_TOKEN_FILENAME
        from clawjournal.workbench.index import INDEX_DB
        from pathlib import Path

        token = (
            Path(str(INDEX_DB)).parent / API_TOKEN_FILENAME
        ).read_text().strip()

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request(
            "GET",
            seeded["root_url"],
            headers={"Cookie": f"clawjournal_token={token}"},
        )
        resp = conn.getresponse()
        body = resp.read().decode()

        assert resp.status == 200
        assert "Session Timeline" in body

    def test_timeline_route_rejects_wrong_cookie_value(self, server, index_setup):
        seeded = _seed_timeline(index_setup)

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request(
            "GET",
            seeded["root_url"],
            headers={"Cookie": "clawjournal_token=not-the-real-token"},
        )
        resp = conn.getresponse()
        body = resp.read()

        assert resp.status == 401
        assert body == b""

    def test_hook_only_events_are_reordered_before_turn_assignment(
        self, server, index_setup
    ):
        seeded = _seed_timeline(index_setup)

        from clawjournal.events.view import write_hook_override

        conn = open_index()
        try:
            write_hook_override(
                conn,
                session_key=seeded["root_key"],
                event_key="user_message:hook-early",
                event_type="user_message",
                source="hook",
                confidence="high",
                lossiness="none",
                event_at="2026-04-22T09:59:59Z",
                payload_json='{"message":{"content":"hook only first turn"}}',
                origin="hook:test",
            )
        finally:
            conn.close()

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", seeded["root_url"], headers=_api_auth_headers())
        resp = conn.getresponse()
        body = resp.read().decode()

        assert resp.status == 200
        assert body.index("hook only first turn") < body.index("pytest tests/test_auth.py")
        assert "Turn 2" in body

    def test_hook_only_anchor_ids_are_namespaced_by_session(
        self, server, index_setup
    ):
        seeded = _seed_timeline(index_setup)

        from clawjournal.events.view import write_hook_override

        conn = open_index()
        try:
            write_hook_override(
                conn,
                session_key=seeded["root_key"],
                event_key="tool_call:shared-hook",
                event_type="tool_call",
                source="hook",
                confidence="high",
                lossiness="none",
                event_at="2026-04-22T10:01:30Z",
                payload_json='{"tool_name":"Read","path":"root.txt"}',
                origin="hook:test",
            )
            write_hook_override(
                conn,
                session_key=seeded["child_key"],
                event_key="tool_call:shared-hook",
                event_type="tool_call",
                source="hook",
                confidence="high",
                lossiness="none",
                event_at="2026-04-22T10:04:30Z",
                payload_json='{"tool_name":"Read","path":"child.txt"}',
                origin="hook:test",
            )
        finally:
            conn.close()

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", seeded["root_url"], headers=_api_auth_headers())
        resp = conn.getresponse()
        body = resp.read().decode()

        assert resp.status == 200
        assert (
            'id="event-key-claude-demo-proj-parent-root-tool-call-shared-hook"'
            in body
        )
        assert (
            'id="event-key-claude-demo-proj-child-agent-tool-call-shared-hook"'
            in body
        )


class TestRunServerPortFallback:
    def test_fallback_to_free_port_on_oserror(self, index_setup):
        """If the default port is busy, run_server falls back to port 0 and opens the browser."""
        from http.server import ThreadingHTTPServer

        real_server = MagicMock()
        real_server.server_address = ("127.0.0.1", 9999)
        real_server.serve_forever.side_effect = KeyboardInterrupt

        call_count = 0

        def fake_init(addr, handler):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("Address already in use")
            return real_server

        with patch("clawjournal.workbench.daemon.ThreadingHTTPServer", side_effect=fake_init), \
             patch("clawjournal.workbench.daemon.Scanner"), \
             patch("webbrowser.open") as mock_open:
            run_server(port=8384, open_browser=True)

        mock_open.assert_called_once_with("http://localhost:9999/")


def _mock_urlopen_factory(upload_response=None, upload_error=None, upload_assert=None, capabilities=None):
    """Create a mock urlopen that handles the hosted research API."""
    upload_resp = upload_response or {"receipt_id": "rcpt-test-123", "status": "received"}
    cap_resp = capabilities or {
        "submissions_open": True,
        "preferred_upload_flow": "browser_zip",
        "cli_ingest_supported": False,
        "share_page_url": "https://hosted.example.test/share",
        "submit_page_url": "https://hosted.example.test/share",
        "maximum_bundle_size": 52_428_800,
        "accepted_manifest_schema_versions": ["1.0.0"],
        "supported_institution_email_policy": {"domain_suffixes": [".edu", "rayward.ai"]},
        "contact_email": "contact@example.test",
        "cache_seconds": 0,
    }

    def _resp(payload):
        resp = MagicMock()
        resp.read.return_value = json.dumps(payload).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def mock_urlopen(req, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "/.well-known/clawjournal-share.json" in url:
            return _resp(cap_resp)
        if "/api/consent" in url:
            return _resp({
                "consent_text": "Consent text",
                "retention_text": "Retention text",
                "consent_version": "consent-v1",
                "retention_policy_version": "retention-v1",
                "support_contact": "contact@example.test",
            })
        if "/api/verify-email/confirm" in url:
            return _resp({
                "upload_token": "upload-token-123",
                "upload_token_expires_at": int(time.time()) + 3600,
            })
        if "/api/verify-email" in url:
            return _resp({
                "verification_id": "verify-123",
                "expires_at": "2026-01-01T00:00:00+00:00",
            })
        if "/api/submissions" in url:
            if upload_assert is not None:
                upload_assert(req)
            if upload_error:
                raise upload_error
            return _resp(upload_resp)
        raise ValueError(f"Unexpected URL: {url}")

    return mock_urlopen


def _share_config(**overrides):
    """Return a standard mock config for share tests with valid (non-expired) upload token."""
    config = {
        "verified_email": "test@university.edu",
        "verified_email_token": "test-upload-token",
        "verified_email_token_expires_at": int(time.time()) + 3600,
    }
    config.update(overrides)
    return config


def _mock_trufflehog_clean(monkeypatch):
    """Share-upload tests need to simulate a real, clean TruffleHog scan.

    The suite-wide autouse fixture bypasses TruffleHog for every test,
    and the upload path now (correctly) refuses bypassed shares. Unset
    the bypass and install a mock scan that reports zero findings.
    """
    from clawjournal.redaction import trufflehog

    monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(trufflehog, "is_available", lambda: True)
    monkeypatch.setattr(
        trufflehog,
        "scan_file",
        lambda path: trufflehog.TruffleHogReport(
            scanned_path=str(path),
            scanned_sha256="sha256:0",
        ),
    )
    monkeypatch.setattr(trufflehog, "_scan_text_for_raw_matches", lambda text: [])


def test_upload_pii_redaction_runs_sessions_in_parallel(tmp_path, monkeypatch):
    sessions_file = tmp_path / "sessions.jsonl"
    sessions = [
        {
            "session_id": f"s{i}",
            "messages": [{"role": "user", "content": f"Alice{i} should be hidden"}],
        }
        for i in range(4)
    ]
    sessions_file.write_text(
        "\n".join(json.dumps(session) for session in sessions) + "\n",
        encoding="utf-8",
    )

    lock = Lock()
    active = 0
    max_active = 0

    def fake_review(
        session,
        *,
        ignore_llm_errors=True,
        return_coverage=False,
        timeout_seconds=180,
        **_kw,
    ):
        nonlocal active, max_active
        assert ignore_llm_errors is True
        assert return_coverage is True
        assert timeout_seconds == 23
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.05)
        with lock:
            active -= 1
        sid = session["session_id"]
        suffix = sid.removeprefix("s")
        return ([{
            "session_id": sid,
            "message_index": 0,
            "field": "content",
            "entity_text": f"Alice{suffix}",
            "entity_type": "person_name",
            "confidence": 0.95,
            "reason": "test name",
            "replacement": "[REDACTED_NAME]",
            "source": "test",
        }], "full")

    monkeypatch.setenv("CLAWJOURNAL_UPLOAD_PII_WORKERS", "4")
    monkeypatch.setenv("CLAWJOURNAL_UPLOAD_PII_TIMEOUT_SECONDS", "23")
    monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_hybrid", fake_review)

    summary = _apply_upload_pii_redactions(sessions_file)

    assert summary["workers"] == 4
    assert summary["agent_timeout_seconds"] == 23
    assert summary["finding_count"] == 4
    assert summary["replacement_count"] == 4
    assert summary["coverage"] == {"full": 4, "rules_only": 0}
    assert max_active > 1

    redacted = [
        json.loads(line)
        for line in sessions_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [session["session_id"] for session in redacted] == ["s0", "s1", "s2", "s3"]
    assert all(
        session["messages"][0]["content"] == "[REDACTED_NAME] should be hidden"
        for session in redacted
    )


class TestVerifyEmailAPI:
    def test_request_email_verification_stores_pending_id(self, monkeypatch):
        from clawjournal.workbench.daemon import request_email_verification

        saved = {}
        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", lambda config: saved.update(config))

        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            result = request_email_verification("Test@University.edu")

        assert result["verification_id"] == "verify-123"
        assert saved["pending_verification_id"] == "verify-123"
        assert saved["pending_verification_email"] == "test@university.edu"

    def test_confirm_email_verification_persists_upload_token_and_expiry(self, monkeypatch):
        from clawjournal.workbench.daemon import confirm_email_verification

        saved = {}

        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {
            "pending_verification_id": "verify-123",
            "pending_verification_email": "test@university.edu",
        })
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", lambda config: saved.update(config))

        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            result = confirm_email_verification("Test@University.edu", "123456")

        assert result["upload_token"] == "upload-token-123"
        assert saved["verified_email"] == "test@university.edu"
        assert saved["verified_email_token"] == "upload-token-123"
        assert "pending_verification_id" not in saved

    def test_verify_endpoints_do_not_return_upload_token(self, server, monkeypatch):
        state = {}

        def load_state():
            return dict(state)

        def save_state(updated):
            state.clear()
            state.update(updated)

        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", load_state)
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", save_state)

        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            status, data = _post(server, "/api/share/verify-email", {"email": "Test@University.edu"})
            assert status == 200
            assert data["ok"] is True
            assert data["email"] == "test@university.edu"
            assert "upload_token" not in data
            assert state["pending_verification_id"] == "verify-123"

            status, data = _post(server, "/api/share/verify-confirm", {"code": "123456"})

        assert status == 200
        assert data["verified"] is True
        assert data["verified_email"] == "test@university.edu"
        assert "upload_token" not in json.dumps(data)
        assert state["verified_email_token"] == "upload-token-123"
        assert "pending_verification_id" not in state

    def test_verify_email_network_failure_returns_502(self, server, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ):
            status, data = _post(server, "/api/share/verify-email", {"email": "test@university.edu"})

        assert status == 502
        assert "connection refused" in data["error"]

    def test_verify_email_preserves_hosted_rate_limit_status(self, server, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)
        error_resp = BytesIO(json.dumps({"error": "Too many verification attempts"}).encode())
        http_error = urllib.error.HTTPError(
            url="https://hosted.example.test/api/verify-email",
            code=429,
            msg="Too Many Requests",
            hdrs={},
            fp=error_resp,  # type: ignore[arg-type]
        )

        def fail_verify(req, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/.well-known/clawjournal-share.json" in url:
                return _mock_urlopen_factory()(req, **kwargs)
            raise http_error

        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=fail_verify):
            status, data = _post(server, "/api/share/verify-email", {"email": "test@university.edu"})

        assert status == 429
        assert data["error"] == "Too many verification attempts"


class TestShareAPI:
    """Tests for the hosted research submission flow."""

    @pytest.fixture(autouse=True)
    def _trufflehog_clean_for_uploads(self, monkeypatch, index_setup):
        """The upload path refuses bypassed TruffleHog scans by design.
        All share-API tests want the clean-upload scenario, so install
        a no-op mock here rather than repeating it in every test."""
        _mock_trufflehog_clean(monkeypatch)
        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)

    def _create_and_export_share(self, port):
        """Helper: create a share and export it, return share_id.

        Releases the underlying sessions (`hold_state='released'`) so
        the centralized upload gate in `submit_share_to_hosted` lets the share
        through. Hosted upload requires released sessions since the
        security refactor.
        """
        from clawjournal.workbench.index import open_index, set_hold_state
        conn = open_index()
        try:
            for sid in ("sess-0", "sess-1"):
                set_hold_state(conn, sid, "released", changed_by="user", reason="test")
        finally:
            conn.close()

        status, data = _post(port, "/api/shares", {
            "session_ids": ["sess-0", "sess-1"],
            "note": "Share test",
        })
        assert status == 201
        share_id = data["share_id"]

        status, data = _post(port, f"/api/shares/{share_id}/export")
        assert status == 200
        assert data["ok"] is True
        return share_id

    def _consent_body(self):
        return {
            "accept_terms": True,
            "ownership_certification": True,
            "consent_version": "consent-v1",
            "retention_policy_version": "retention-v1",
        }

    def test_upload_refuses_when_trufflehog_bypassed(self, server, monkeypatch):
        """CLAWJOURNAL_SKIP_TRUFFLEHOG is a dev/CI escape hatch for
        local bundle-export. Uploading an unscanned share to a remote
        endpoint must fail closed — otherwise the escape hatch is a
        one-flag ``--ship-secrets-anyway``."""
        from clawjournal.redaction import trufflehog

        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        # Unwind the class-level clean mock: simulate an actual bypass.
        monkeypatch.setenv(trufflehog.SKIP_ENV_VAR, "1")
        # Ensure scan_file observes the bypass by exercising the real
        # path (short-circuits to bypassed=True) rather than the clean
        # stub from the autouse fixture.
        from pathlib import Path as _Path

        def _real_bypass_scan(path):
            return trufflehog.TruffleHogReport(
                scanned_path=str(path),
                scanned_sha256="sha256:0",
                bypassed=True,
            )

        monkeypatch.setattr(trufflehog, "scan_file", _real_bypass_scan)

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 422, data
        assert data.get("block_reason") == "trufflehog-bypassed"
        assert "CLAWJOURNAL_SKIP_TRUFFLEHOG" in data.get("error", "")

    def test_share_success(self, server, monkeypatch):
        """Full success path: create, export, share via HTTP."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 200
        assert data["ok"] is True
        assert "gcs_uri" not in data
        assert data["receipt_id"] == "rcpt-test-123"
        assert "shared_at" in data
        assert data["bundle_hash"]
        assert "redaction_summary" in data
        assert isinstance(data["redaction_summary"]["total_redactions"], int)
        assert isinstance(data["redaction_summary"]["by_type"], dict)
        conn = open_index()
        row = conn.execute(
            "SELECT hosted_receipt_id, gcs_uri FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        conn.close()
        assert row["hosted_receipt_id"] == "rcpt-test-123"
        assert row["gcs_uri"] is None

        status, detail = _get(server, f"/api/shares/{share_id}")
        assert status == 200
        assert detail["hosted_receipt_id"] == "rcpt-test-123"
        assert "gcs_uri" not in detail

        status, share_list = _get(server, "/api/shares")
        assert status == 200
        listed = next(share for share in share_list if share["share_id"] == share_id)
        assert listed["hosted_receipt_id"] == "rcpt-test-123"
        assert "gcs_uri" not in listed

    def test_share_success_clears_cached_upload_token(self, server, monkeypatch):
        """Successful upload should clear the cached single-use token."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        config = _share_config()
        saved = {}

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: config)
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", lambda updated: saved.update(updated))

        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 200
        assert data["ok"] is True
        assert "verified_email_token" not in saved
        assert "verified_email_token_expires_at" not in saved

    def test_share_rate_limiting(self, server, monkeypatch):
        """Two shares within cooldown → second gets 429."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())
        assert status == 200

        status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())
        assert status == 429
        assert "Rate limited" in data["error"]

    def test_share_duplicate_prevention(self, server, monkeypatch):
        """Already-submitted bundle → 409; force surfaces a clearer message."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        mock_urlopen = _mock_urlopen_factory()
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=mock_urlopen):
            status, _ = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())
        assert status == 200

        WorkbenchHandler._last_share_time = 0.0
        status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())
        assert status == 409
        assert "already submitted" in data["error"]
        assert data["receipt_id"] == "rcpt-test-123"

        WorkbenchHandler._last_share_time = 0.0
        body = {**self._consent_body(), "force": True}
        status, data = _post(server, f"/api/shares/{share_id}/upload", body)
        assert status == 409
        assert "cannot be overwritten" in data["error"]
        assert data["receipt_id"] == "rcpt-test-123"

    def test_force_on_fresh_share_submits(self, server, monkeypatch):
        """`force: true` on a never-submitted share submits normally (force is ignored)."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        body = {**self._consent_body(), "force": True}
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            status, data = _post(server, f"/api/shares/{share_id}/upload", body)

        assert status == 200, data
        assert data["ok"] is True
        assert data["receipt_id"] == "rcpt-test-123"

    def test_self_hosted_ingest_share_rejects_hosted_submit(self, server, monkeypatch):
        """A share previously uploaded via self-hosted ingest cannot be re-submitted to hosted research."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        # Simulate a legacy self-hosted upload: shared_at is set but
        # there's no hosted_receipt_id.
        conn = open_index()
        conn.execute(
            "UPDATE shares SET status = 'shared', shared_at = ?, "
            "hosted_receipt_id = NULL WHERE share_id = ?",
            ("2026-01-01T00:00:00+00:00", share_id),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 409
        assert "self-hosted ingest" in data["error"]
        assert data.get("shared_at") == "2026-01-01T00:00:00+00:00"
        assert "receipt_id" not in data

    def test_share_http_error(self, server, monkeypatch):
        """HTTP error from ingest → daemon returns 502."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        error_resp = BytesIO(json.dumps({"error": "Internal server error"}).encode())
        http_error = urllib.error.HTTPError(
            url="http://test/api/submissions", code=500, msg="Internal Server Error",
            hdrs={}, fp=error_resp,  # type: ignore[arg-type]
        )

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory(upload_error=http_error)):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 502
        assert "error" in data

    def test_hosted_409_is_returned_as_conflict(self, server, monkeypatch):
        """Hosted conflicts are not treated as GCS idempotency successes."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        error_resp = BytesIO(json.dumps({"error": "Duplicate submission"}).encode())
        http_error = urllib.error.HTTPError(
            url="http://test/api/submissions", code=409, msg="Conflict",
            hdrs={}, fp=error_resp,  # type: ignore[arg-type]
        )

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory(upload_error=http_error)):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 409
        assert data["error"] == "Duplicate submission"

    def test_share_network_failure(self, server, monkeypatch):
        """Network failure → daemon returns 502 with friendly message."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory(upload_error=urllib.error.URLError("Connection refused"))):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 502
        assert "Could not reach hosted submission service" in data["error"]

    def test_share_verification_error_passthrough(self, server, monkeypatch):
        """Verification failures from the ingest service should remain 403."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        error_resp = BytesIO(json.dumps({"error": "Invalid or expired upload token"}).encode())
        http_error = urllib.error.HTTPError(
            url="http://test/api/submissions", code=403, msg="Forbidden",
            hdrs={}, fp=error_resp,  # type: ignore[arg-type]
        )

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory(upload_error=http_error)):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 403
        assert data["error"] == "Invalid or expired upload token"

    def test_share_verification_error_clears_cached_upload_token(self, server, monkeypatch):
        """Invalid token responses should clear the cached token locally."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        config = _share_config()
        saved = {}

        error_resp = BytesIO(json.dumps({"error": "Invalid or expired upload token"}).encode())
        http_error = urllib.error.HTTPError(
            url="http://test/api/submissions", code=403, msg="Forbidden",
            hdrs={}, fp=error_resp,  # type: ignore[arg-type]
        )

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: config)
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", lambda updated: saved.update(updated))
        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory(upload_error=http_error)):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 403
        assert data["error"] == "Invalid or expired upload token"
        assert "verified_email_token" not in saved
        assert "verified_email_token_expires_at" not in saved

    def test_share_requires_verified_email_token(self, server, monkeypatch):
        """Config has email but no token → 403."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {
            "verified_email": "test@university.edu",
        })

        status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())
        assert status == 403
        assert "needs to be refreshed" in data["error"]

    def test_share_fails_with_expired_token(self, server, monkeypatch):
        """Expired upload token → 403 with re-verification message."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config(
            verified_email_token_expires_at=int(time.time()) - 100,
        ))

        status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())
        assert status == 403
        assert "expired" in data["error"].lower()

    def test_share_upload_sends_only_upload_token(self, server, monkeypatch):
        """Upload form should contain upload_token but NOT verified_email or device_id."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        def assert_upload_fields(req):
            body = req.data.decode("utf-8", errors="replace")
            assert 'name="upload_token"' in body
            assert "test-upload-token" in body
            assert 'name="bundle"; filename="' in body
            assert 'name="consent_version"' in body
            assert "consent-v1" in body
            assert 'name="ownership_certification"' in body
            # These must NOT be sent as form fields
            assert 'name="verified_email"' not in body
            assert 'name="device_id"' not in body

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(upload_assert=assert_upload_fields),
        ):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 200
        assert data["ok"] is True

    def test_share_upload_submits_hosted_zip_with_validator_fields(self, server, monkeypatch):
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        def assert_zip(req):
            body = req.data
            assert b'name="sessions"' not in body
            marker = b"PK\x03\x04"
            start = body.index(marker)
            end = body.rfind(b"\r\n--")
            with zipfile.ZipFile(BytesIO(body[start:end])) as archive:
                names = set(archive.namelist())
                assert {"manifest.json", "sessions.jsonl", "trufflehog.post-pii.json"}.issubset(names)
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                summary = manifest["redaction_summary"]
                assert isinstance(summary["pii_review"]["finding_count"], int)
                post = summary["trufflehog_post_pii"]
                assert post["findings"] == 0
                assert post["bypassed"] is False
                assert post["binary_missing"] is False
                assert not post.get("scan_error")

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(upload_assert=assert_zip),
        ):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 200
        assert data["receipt_id"] == "rcpt-test-123"

    def test_share_upload_requires_consent_body(self, server, monkeypatch):
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        status, data = _post(server, f"/api/shares/{share_id}/upload")

        assert status == 400
        assert "requires consent fields" in data["error"]

    def test_share_upload_rejects_oversize_zip_before_network(self, server, monkeypatch):
        """The daemon refuses oversize zips locally so we never hit the hosted limit."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)

        tiny_capabilities = {
            "submissions_open": True,
            "preferred_upload_flow": "browser_zip",
            "cli_ingest_supported": False,
            "share_page_url": "https://hosted.example.test/share",
            "submit_page_url": "https://hosted.example.test/share",
            "maximum_bundle_size": 16,  # bytes — guaranteed to be smaller than any real share zip
            "accepted_manifest_schema_versions": ["1.0.0"],
            "supported_institution_email_policy": {"domain_suffixes": [".edu", "rayward.ai"]},
            "contact_email": "contact@example.test",
            "cache_seconds": 0,
        }

        submissions_called = {"count": 0}

        def upload_assert(_req):
            submissions_called["count"] += 1

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(capabilities=tiny_capabilities, upload_assert=upload_assert),
        ):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 413, data
        assert "exceeds the hosted limit" in data["error"]
        assert submissions_called["count"] == 0  # never reached /api/submissions

    def test_share_upload_surfaces_stale_consent_rejection(self, server, monkeypatch):
        """Hosted 400 with consent-version error is forwarded verbatim so the UI can refresh."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        stale_resp = BytesIO(json.dumps({
            "error": "consent_version 'consent-v1' is stale; current is 'consent-v2'.",
        }).encode())
        stale_error = urllib.error.HTTPError(
            url="http://test/api/submissions", code=400, msg="Bad Request",
            hdrs={}, fp=stale_resp,  # type: ignore[arg-type]
        )

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(upload_error=stale_error),
        ):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 400, data
        assert "consent_version" in data["error"]
        assert "stale" in data["error"]
        # Stale-consent must not leave the share marked shared in the local DB.
        conn = open_index()
        row = conn.execute(
            "SELECT status, hosted_receipt_id FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        conn.close()
        assert row["status"] != "shared"
        assert row["hosted_receipt_id"] is None

    def test_legacy_share_alias_requires_consent_body(self, server, monkeypatch):
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        status, data = _post(server, f"/api/shares/{share_id}/share")

        assert status == 400
        assert "requires consent fields" in data["error"]

    def test_quick_share_packages_only(self, server):
        WorkbenchHandler._last_share_time = 0.0

        status, data = _post(server, "/api/quick-share", {
            "session_ids": ["sess-0", "sess-1"],
            "note": "Quick package",
        })

        assert status == 200
        assert data["ok"] is True
        assert data["next_step"] == "submit"
        assert data["share_id"] == data["bundle_id"]
        assert "shared_at" not in data
        conn = open_index()
        row = conn.execute(
            "SELECT status, shared_at, hosted_receipt_id FROM shares WHERE share_id = ?",
            (data["share_id"],),
        ).fetchone()
        conn.close()
        assert row["status"] == "exported"
        assert row["shared_at"] is None
        assert row["hosted_receipt_id"] is None

    def test_download_preserves_shared_status(self, server):
        """Downloading an already-shared archive must not downgrade it to exported."""
        status, data = _post(server, "/api/shares", {
            "session_ids": ["sess-0", "sess-1"],
            "note": "Status preservation test",
        })
        assert status == 201
        share_id = data["share_id"]

        conn = open_index()
        conn.execute(
            "UPDATE shares SET status = 'shared', shared_at = ? WHERE share_id = ?",
            ("2025-01-05T00:00:00+00:00", share_id),
        )
        conn.commit()
        conn.close()

        status, content_type, body = _get_raw(server, f"/api/shares/{share_id}/download")
        assert status == 200
        assert content_type == "application/zip"
        assert body

        conn = open_index()
        row = conn.execute(
            "SELECT status FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        conn.close()
        assert row["status"] == "shared"

    def test_download_bundle_includes_trufflehog_report(self, server):
        status, data = _post(server, "/api/shares", {
            "session_ids": ["sess-0"],
            "note": "Download report test",
        })
        assert status == 201
        share_id = data["share_id"]

        status, content_type, body = _get_raw(server, f"/api/shares/{share_id}/download")
        assert status == 200
        assert content_type == "application/zip"

        with zipfile.ZipFile(BytesIO(body)) as archive:
            names = set(archive.namelist())
            assert "sessions.jsonl" in names
            assert "manifest.json" in names
            assert "trufflehog.json" in names
            assert "trufflehog.post-pii.json" in names
            report = json.loads(archive.read("trufflehog.json").decode("utf-8"))

        assert report["summary"]["findings"] == 0
        assert report["summary"]["bypassed"] is False

    def test_download_bundle_applies_final_ai_pii_redaction(self, server, monkeypatch):
        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "download-ai-pii",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Alice Example should not ship", "tool_uses": []},
                {"role": "assistant", "content": "Done.", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1,
                "assistant_messages": 1,
                "tool_uses": 0,
                "input_tokens": 100,
                "output_tokens": 50,
            },
        }])
        conn.close()

        def fake_review(session, **kw):
            assert kw.get("return_coverage") is True
            return ([{
                "session_id": session["session_id"],
                "message_index": 0,
                "field": "content",
                "entity_text": "Alice Example",
                "entity_type": "person_name",
                "confidence": 0.95,
                "reason": "test name",
                "replacement": "[REDACTED_NAME]",
                "source": "test",
            }], "full")

        monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_hybrid", fake_review)

        status, data = _post(server, "/api/shares", {
            "session_ids": ["download-ai-pii"],
            "note": "Download AI PII test",
        })
        assert status == 201
        share_id = data["share_id"]

        status, content_type, body = _get_raw(server, f"/api/shares/{share_id}/download")
        assert status == 200
        assert content_type == "application/zip"

        with zipfile.ZipFile(BytesIO(body)) as archive:
            sessions_content = archive.read("sessions.jsonl").decode("utf-8")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

        assert "Alice Example" not in sessions_content
        assert "[REDACTED_NAME]" in sessions_content
        assert manifest["redaction_summary"]["pii_review"]["finding_count"] == 1
        assert manifest["redaction_summary"]["coverage"] == {"full": 1, "rules_only": 0}

    def test_seal_is_idempotent_and_download_reuses_export(self, server, monkeypatch):
        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "seal-ai-pii",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Alice Example should not ship", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1,
                "assistant_messages": 0,
                "tool_uses": 0,
                "input_tokens": 100,
                "output_tokens": 0,
            },
        }])
        conn.close()

        calls = 0

        def fake_review(session, **kw):
            nonlocal calls
            calls += 1
            assert kw.get("return_coverage") is True
            return ([{
                "session_id": session["session_id"],
                "message_index": 0,
                "field": "content",
                "entity_text": "Alice Example",
                "entity_type": "person_name",
                "confidence": 0.95,
                "reason": "test name",
                "replacement": "[REDACTED_NAME]",
                "source": "test",
            }], "full")

        monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_hybrid", fake_review)

        status, data = _post(server, "/api/shares", {
            "session_ids": ["seal-ai-pii"],
            "note": "Seal AI PII test",
        })
        assert status == 201
        share_id = data["share_id"]

        status, data = _post(server, f"/api/shares/{share_id}/seal")
        assert status == 200
        assert data["ok"] is True
        assert data["redaction_summary"]["pii_review"]["finding_count"] == 1
        assert calls == 1

        status, data = _post(server, f"/api/shares/{share_id}/seal")
        assert status == 200
        assert data["ok"] is True
        assert data["redaction_summary"]["pii_review"]["finding_count"] == 1
        assert calls == 1

        status, content_type, body = _get_raw(server, f"/api/shares/{share_id}/download")
        assert status == 200
        assert content_type == "application/zip"
        assert calls == 1

        with zipfile.ZipFile(BytesIO(body)) as archive:
            sessions_content = archive.read("sessions.jsonl").decode("utf-8")

        assert "Alice Example" not in sessions_content
        assert "[REDACTED_NAME]" in sessions_content

    def test_download_applies_configured_custom_redactions(self, server, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.load_config",
            lambda: {"redact_strings": ["MySecretName"]},
        )

        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "download-redact",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "MySecretName appears in this trace", "tool_uses": []},
                {"role": "assistant", "content": "Acknowledged, MySecretName.", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1,
                "assistant_messages": 1,
                "tool_uses": 0,
                "input_tokens": 100,
                "output_tokens": 50,
            },
        }])
        conn.close()

        status, data = _post(server, "/api/shares", {
            "session_ids": ["download-redact"],
            "note": "Download redaction test",
        })
        assert status == 201
        share_id = data["share_id"]

        status, content_type, body = _get_raw(server, f"/api/shares/{share_id}/download")
        assert status == 200
        assert content_type == "application/zip"

        with zipfile.ZipFile(BytesIO(body)) as archive:
            sessions_content = archive.read("sessions.jsonl").decode("utf-8")

        assert "MySecretName" not in sessions_content
