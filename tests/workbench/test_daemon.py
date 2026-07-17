"""Tests for the workbench daemon HTTP API."""

import json
import logging
import os
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
    _build_share_zip,
    _reload_child_command,
    _missing_ingest_url_error,
    _warn_if_frontend_stale,
    trigger_scoring_warmup,
)
from clawjournal.workbench.index import add_policy, open_index, set_hold_state, upsert_sessions


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


@pytest.fixture
def server_with_scanner(index_setup):
    """Start a test HTTP server with a controllable background scanner."""
    from http.server import ThreadingHTTPServer

    scanner = SimpleNamespace(calls=[], status="started")

    def trigger_auto_score(**kwargs):
        scanner.calls.append(kwargs)
        return {"status": scanner.status, **kwargs}

    scanner.trigger_auto_score = trigger_auto_score
    srv = ThreadingHTTPServer(("127.0.0.1", 0), WorkbenchHandler)
    srv._scanner = scanner
    port = srv.server_address[1]
    thread = Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield port, scanner
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


def _write_with_mtime(path, content, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    os.utime(path, (mtime, mtime))


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


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("get", "/api/auto-upload/status"),
        ("get", "/api/auto-upload/preview"),
        ("post", "/api/auto-upload/preview"),
        ("post", "/api/auto-upload/enable"),
        ("post", "/api/auto-upload/run"),
        ("post", "/api/auto-upload/pause"),
        ("post", "/api/auto-upload/resume"),
        ("post", "/api/auto-upload/disable"),
    ],
)
def test_auto_upload_endpoints_require_api_auth(server, method, path):
    request = _get if method == "get" else _post

    status, body = request(server, path, skip_auth=True)

    assert status == 401
    assert body == ""


def test_auto_upload_status_preview_and_control_routes(server, monkeypatch):
    from clawjournal import auto_upload

    preview_calls = []
    pause_calls = []
    monkeypatch.setattr(
        auto_upload,
        "status",
        lambda: {"ok": True, "mode": "off", "overlay": None},
    )
    monkeypatch.setattr(
        auto_upload,
        "preview",
        lambda *, refresh: preview_calls.append(refresh)
        or {"ok": True, "refresh": refresh},
    )
    monkeypatch.setattr(
        auto_upload,
        "pause",
        lambda: pause_calls.append(True) or {"ok": True, "mode": "paused"},
    )

    status_code, body = _get(server, "/api/auto-upload/status")
    assert status_code == 200
    assert body["mode"] == "off"

    status_code, body = _get(server, "/api/auto-upload/preview")
    assert status_code == 200
    assert body["refresh"] is False

    status_code, body = _post(server, "/api/auto-upload/preview")
    assert status_code == 200
    assert body["refresh"] is True

    status_code, body = _post(server, "/api/auto-upload/pause")
    assert status_code == 200
    assert body["mode"] == "paused"
    assert preview_calls == [False, True]
    assert pause_calls == [True]


def test_auto_upload_enable_forwards_authorization_profile_hash(server, monkeypatch):
    from clawjournal import auto_upload

    calls = []
    monkeypatch.setattr(
        auto_upload,
        "enable",
        lambda **kwargs: calls.append(kwargs)
        or {"ok": True, "mode": "enabled", "health": "ready"},
    )

    status_code, body = _post(
        server,
        "/api/auto-upload/enable",
        {
            "agent": "codex",
            "accepted_authorization_version": "auth-v1",
            "accepted_retention_version": "ret-v1",
            "accepted_authorization_profile_hash": "profile-sha256",
        },
    )

    assert status_code == 200
    assert body["mode"] == "enabled"
    assert calls == [
        {
            "agent": "codex",
            "accepted_authorization_version": "auth-v1",
            "accepted_retention_version": "ret-v1",
            "accepted_authorization_profile_hash": "profile-sha256",
            "challenge_only": False,
        }
    ]


def test_auto_upload_get_status_is_local_only(server, monkeypatch):
    from clawjournal import auto_upload
    from clawjournal.workbench import daemon

    def unexpected_network(*args, **kwargs):
        raise AssertionError("GET status must not invoke network")

    monkeypatch.setattr(auto_upload, "fetch_capabilities", unexpected_network)
    monkeypatch.setattr(auto_upload, "_load_config_readonly", lambda: {})
    monkeypatch.setattr(auto_upload, "hook_diagnostics", lambda *args, **kwargs: {})
    monkeypatch.setattr(daemon.urllib.request, "urlopen", unexpected_network)

    status_code, body = _get(server, "/api/auto-upload/status")

    assert status_code == 200
    assert body["ok"] is True
    assert body["mode"] == "off"


def test_config_allowlist_does_not_report_success_when_persistence_fails(
    server, monkeypatch
):
    monkeypatch.setattr(
        "clawjournal.config.load_config", lambda: {"allowlist_entries": []}
    )
    monkeypatch.setattr("clawjournal.config.save_config", lambda _config: False)

    status_code, body = _post(
        server,
        "/api/allowlist",
        {"type": "exact", "text": "private value"},
    )

    assert status_code == 500
    assert "persistence could not be confirmed" in body["error"]


@pytest.mark.parametrize(
    ("current", "lock_available", "expected_code"),
    [
        ({"ok": True, "mode": "off", "overlay": None}, None, "not_enabled"),
        ({"ok": True, "mode": "paused", "overlay": None}, None, "paused"),
        (
            {
                "ok": True,
                "mode": "enabled",
                "health": "action_required",
                "overlay": None,
            },
            None,
            "action_required",
        ),
        ({"ok": True, "mode": "enabled", "overlay": None}, False, "already_running"),
    ],
)
def test_auto_upload_run_rejects_ineligible_state_before_spawning(
    server, monkeypatch, current, lock_available, expected_code
):
    from clawjournal import auto_upload
    from clawjournal.workbench import daemon

    def unexpected_thread(*args, **kwargs):
        raise AssertionError("ineligible run must not construct a worker thread")

    def unexpected_cycle(*args, **kwargs):
        raise AssertionError("ineligible run must not execute a cycle")

    class LockProbe:
        def __enter__(self):
            if lock_available is None:
                raise AssertionError("ineligible run must not probe the runner lock")
            return lock_available

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(auto_upload, "status", lambda: dict(current))
    monkeypatch.setattr(auto_upload, "run_cycle", unexpected_cycle)
    monkeypatch.setattr(
        auto_upload,
        "whole_run_lock",
        lambda *, blocking: LockProbe(),
    )
    monkeypatch.setattr(daemon, "_auto_upload_run_thread", None)
    monkeypatch.setattr(daemon, "threading", SimpleNamespace(Thread=unexpected_thread))

    status_code, body = _post(server, "/api/auto-upload/run")

    assert status_code == 409
    assert body["ok"] is False
    assert body["code"] == expected_code
    assert daemon._auto_upload_run_thread is None


def test_auto_upload_run_allows_hook_only_action_with_stale_overlay_if_lock_free(
    server, monkeypatch
):
    from clawjournal import auto_upload
    from clawjournal.workbench import daemon

    threads = []

    class AvailableLock:
        def __enter__(self):
            return True

        def __exit__(self, *args):
            return False

    class DeferredThread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.started = False
            threads.append(self)

        def is_alive(self):
            return self.started

        def start(self):
            self.started = True

    monkeypatch.setattr(
        auto_upload,
        "status",
        lambda: {
            "ok": True,
            "mode": "enabled",
            "health": "action_required",
            "run_now_allowed": True,
            "overlay": "running",
        },
    )
    monkeypatch.setattr(
        auto_upload,
        "whole_run_lock",
        lambda *, blocking: AvailableLock(),
    )
    monkeypatch.setattr(daemon, "_auto_upload_run_thread", None)
    monkeypatch.setattr(daemon, "threading", SimpleNamespace(Thread=DeferredThread))

    status_code, body = _post(server, "/api/auto-upload/run")

    assert status_code == 200
    assert body["overlay"] == "running"
    assert len(threads) == 1
    assert threads[0].started is True


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

    def test_list_sessions_with_multiple_statuses(self, server):
        status, data = _post(server, "/api/sessions/sess-0", {"status": "shortlisted"})
        assert status == 200
        assert data["ok"] is True

        status, data = _post(server, "/api/sessions/sess-1", {"status": "blocked"})
        assert status == 200
        assert data["ok"] is True

        status, data = _get(server, "/api/sessions?status=new&status=shortlisted")
        assert status == 200
        assert {s["session_id"] for s in data} == {"sess-0", "sess-2"}

    def test_get_session_detail(self, server):
        status, data = _get(server, "/api/sessions/sess-0")
        assert status == 200
        assert data["session_id"] == "sess-0"
        assert "messages" in data

    def test_encoded_session_id_routes(self, server, monkeypatch):
        from urllib.parse import quote

        session_id = "claude-science:org-1:frame-1"
        conn = open_index()
        try:
            upsert_sessions(conn, [{
                "session_id": session_id,
                "project": "claude-science:lab",
                "source": "claude-science",
                "model": "claude-science",
                "messages": [
                    {"role": "user", "content": "Inspect the trace", "tool_uses": []},
                ],
                "stats": {
                    "user_messages": 1,
                    "assistant_messages": 0,
                    "tool_uses": 0,
                    "input_tokens": 1,
                    "output_tokens": 0,
                },
            }])
        finally:
            conn.close()

        encoded = quote(session_id, safe="")
        status, data = _get(server, f"/api/sessions/{encoded}")
        assert status == 200
        assert data["session_id"] == session_id

        status, data = _post(server, f"/api/sessions/{encoded}", {"status": "approved"})
        assert status == 200
        assert data["ok"] is True

        monkeypatch.setattr(
            "clawjournal.scoring.scoring.score_session",
            lambda conn, sid, model=None, backend="auto": SimpleNamespace(
                quality=4,
                reason=f"scored {sid}",
                detail_json='{"substance": 4}',
                task_type="analysis",
                outcome_label="completed",
                value_labels=[],
                risk_level=[],
                display_title="Encoded route scored",
                effort_estimate=1.0,
                summary="Scored through encoded API route",
            ),
        )
        status, data = _post(server, f"/api/sessions/{encoded}/score")
        assert status == 200
        assert data["ok"] is True

        status, detail = _get(server, f"/api/sessions/{encoded}")
        assert status == 200
        assert detail["review_status"] == "approved"
        assert detail["ai_summary"] == "Scored through encoded API route"

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
        assert data["ai_coverage"] == "disabled"
        assert any(entry["type"] == "blocked_domain" for entry in data["redaction_log"])

    def test_update_session_status(self, server):
        status, data = _post(server, "/api/sessions/sess-0", {"status": "approved"})
        assert status == 200
        assert data["ok"] is True

        # Verify it persisted
        status, detail = _get(server, "/api/sessions/sess-0")
        assert detail["review_status"] == "approved"

    def test_skipped_session_stays_out_of_new_queue_after_reindex(self, server):
        status, data = _post(server, "/api/sessions/sess-0", {"status": "blocked"})
        assert status == 200
        assert data["ok"] is True

        conn = open_index()
        try:
            upsert_sessions(conn, [{
                "session_id": "sess-0",
                "project": "test-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "start_time": "2025-01-01T00:00:00+00:00",
                "end_time": "2025-01-01T00:10:00+00:00",
                "messages": [
                    {"role": "user", "content": "Task 0: fix the bug", "tool_uses": []},
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
        finally:
            conn.close()

        status, queue = _get(server, "/api/sessions?status=new")
        assert status == 200
        assert {s["session_id"] for s in queue} == {"sess-1", "sess-2"}

        status, detail = _get(server, "/api/sessions/sess-0")
        assert status == 200
        assert detail["review_status"] == "blocked"

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

    def test_score_session_endpoint_applies_policy_redaction(self, server, monkeypatch):
        """Manual scoring threads workbench-policy redaction into the prompt."""
        conn = open_index()
        add_policy(conn, "redact_string", "SecretProject")
        add_policy(conn, "redact_username", "kai")
        add_policy(conn, "block_domain", "api.internal")
        conn.close()

        seen_kwargs = {}

        def fake_score(conn, session_id, **kwargs):
            seen_kwargs.update(kwargs)
            return SimpleNamespace(
                quality=4, reason="ok", detail_json="{}", task_type="t",
                outcome_label="resolved", value_labels=[], risk_level=[],
                display_title="T", effort_estimate=0.5, summary="s",
            )

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", fake_score)

        status, data = _post(server, "/api/sessions/sess-0/score", {"backend": "auto"})
        assert status == 200
        settings = seen_kwargs["redaction_settings"]
        assert settings["custom_strings"] == ["SecretProject"]
        assert settings["extra_usernames"] == ["kai"]
        assert settings["blocked_domains"] == ["api.internal"]

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

    def test_scan_once_tracks_updated_and_unchanged_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr(
            "clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config"
        )
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.discover_projects",
            lambda source_filter=None: [
                {"source": "codex", "dir_name": "project", "locator": None}
            ],
        )
        parsed = [{"session_id": "existing-session"}]
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.parse_project_sessions",
            lambda *args, **kwargs: parsed,
        )

        def fake_upsert(conn, sessions, *, stats=None):
            assert sessions is parsed
            assert stats is not None
            stats.update(inserted=0, updated=1, unchanged=2)
            return 0

        monkeypatch.setattr("clawjournal.workbench.daemon.upsert_sessions", fake_upsert)
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.run_findings_pipeline",
            lambda *args, **kwargs: None,
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.link_subagent_hierarchy", lambda conn: 0
        )

        scanner = Scanner(source_filter="codex")
        results = scanner.scan_once()

        assert results == {"codex": 0}
        assert scanner.last_updated_count == 1
        assert scanner.last_unchanged_count == 2
        assert scanner.last_updated_by_source == {"codex": 1}
        assert scanner.last_unchanged_by_source == {"codex": 2}

    def test_strict_scan_reports_partial_enrolled_source_failure(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr(
            "clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config"
        )
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.discover_projects",
            lambda source_filter=None: [
                {
                    "source": "claude",
                    "dir_name": "PRIVATE_PROJECT_GOOD_SENTINEL",
                    "locator": None,
                },
                {
                    "source": "claude",
                    "dir_name": "PRIVATE_PROJECT_BAD_SENTINEL",
                    "locator": None,
                },
                {
                    "source": "codex",
                    "dir_name": "UNRELATED_PROJECT_SENTINEL",
                    "locator": None,
                },
            ],
        )
        parsed_projects = []

        def parse_project(dir_name, **kwargs):
            parsed_projects.append(dir_name)
            logging.getLogger("clawjournal.parsing.parser").warning(
                "lower parser project=%s", dir_name
            )
            if dir_name == "PRIVATE_PROJECT_BAD_SENTINEL":
                raise RuntimeError(
                    "PRIVATE_EXCEPTION_SENTINEL /private/project/path"
                )
            return []

        monkeypatch.setattr(
            "clawjournal.workbench.daemon.parse_project_sessions", parse_project
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.link_subagent_hierarchy", lambda conn: 0
        )
        caplog.set_level(logging.DEBUG)

        report = Scanner().scan_once_strict(["claude"])

        assert report["ok"] is False
        assert report["discovered_sources"] == ["claude"]
        assert report["failures"] == [
            {"source": "claude", "stage": "parse", "code": "parse_failed"}
        ]
        assert parsed_projects == [
            "PRIVATE_PROJECT_GOOD_SENTINEL",
            "PRIVATE_PROJECT_BAD_SENTINEL",
        ]
        combined = json.dumps(report) + caplog.text
        assert "PRIVATE_PROJECT" not in combined
        assert "UNRELATED_PROJECT_SENTINEL" not in combined
        assert "PRIVATE_EXCEPTION_SENTINEL" not in combined
        assert "/private/project/path" not in combined
        assert "Traceback" not in caplog.text
        assert "parse_failed" in caplog.text

    @pytest.mark.parametrize("source", ["claude", "codex"])
    def test_strict_scan_rejects_malformed_jsonl_without_partial_upsert(
        self, tmp_path, monkeypatch, source
    ):
        monkeypatch.setattr(
            "clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db"
        )
        monkeypatch.setattr(
            "clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs"
        )
        monkeypatch.setattr(
            "clawjournal.workbench.index.CONFIG_DIR", tmp_path / "config"
        )
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.link_subagent_hierarchy", lambda conn: 0
        )
        raw_file = tmp_path / f"{source}-session.jsonl"
        raw_file.write_text(
            '{"type":"session_meta","timestamp":"2026-07-15T00:00:00Z"}\n'
            '{"truncated":',
            encoding="utf-8",
        )
        if source == "claude":
            project_dir = tmp_path / "claude-project"
            project_dir.mkdir()
            raw_file.rename(project_dir / raw_file.name)
            locator = {
                "native_project_dir": project_dir,
                "local_agent_sessions": [],
            }
        else:
            locator = None
            monkeypatch.setattr(
                "clawjournal.parsing.parser._get_codex_project_index",
                lambda: {"test-project": [raw_file]},
            )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.discover_projects",
            lambda source_filter=None: [{
                "source": source,
                "dir_name": "test-project",
                "locator": locator,
            }],
        )

        report = Scanner().scan_once_strict([source])

        assert report["ok"] is False
        assert report["failures"] == [
            {"source": source, "stage": "parse", "code": "parse_failed"}
        ]
        conn = open_index()
        try:
            assert conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] == 0
        finally:
            conn.close()

    def test_strict_scan_logs_no_raw_session_or_findings_exception(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr(
            "clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config"
        )
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.discover_projects",
            lambda source_filter=None: [
                {
                    "source": "claude",
                    "dir_name": "RAW_PROJECT_PATH_SENTINEL",
                    "locator": None,
                }
            ],
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.parse_project_sessions",
            lambda *args, **kwargs: [{
                "session_id": "RAW_SESSION_ID_SENTINEL",
                "raw_source_path": "/private/raw/session.jsonl",
                "_raw_source_fingerprint": (1, 2, 3, 4, "a" * 64),
            }],
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.upsert_sessions",
            lambda conn, sessions, *, stats: 1,
        )

        def fail_findings(*args, **kwargs):
            assert kwargs["safe_logging"] is True
            logging.getLogger("clawjournal.workbench.findings_pipeline").error(
                "raw id=%s path=%s",
                "RAW_SESSION_ID_SENTINEL",
                "RAW_PROJECT_PATH_SENTINEL",
            )
            raise RuntimeError("RAW_FINDINGS_EXCEPTION_SENTINEL")

        monkeypatch.setattr(
            "clawjournal.workbench.daemon.run_findings_pipeline", fail_findings
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.link_subagent_hierarchy", lambda conn: 0
        )
        caplog.set_level(logging.DEBUG)

        report = Scanner().scan_once_strict(["claude"])

        assert report["failures"] == [
            {"source": "claude", "stage": "findings", "code": "findings_failed"}
        ]
        assert "RAW_SESSION_ID_SENTINEL" not in caplog.text
        assert "RAW_PROJECT_PATH_SENTINEL" not in caplog.text
        assert "RAW_FINDINGS_EXCEPTION_SENTINEL" not in caplog.text
        assert "Traceback" not in caplog.text
        assert "findings_failed" in caplog.text

    def test_strict_scan_backfill_logs_only_bounded_failure(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr(
            "clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config"
        )
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})

        def fail_backfill(conn, *, config, safe_logging):
            assert safe_logging is True
            try:
                raise RuntimeError("RAW_BACKFILL_EXCEPTION_SENTINEL")
            except RuntimeError:
                logging.getLogger(
                    "clawjournal.workbench.findings_pipeline"
                ).exception(
                    "raw id=%s path=%s",
                    "RAW_BACKFILL_SESSION_SENTINEL",
                    "/private/backfill/project",
                )
            raise RuntimeError("RAW_OUTER_BACKFILL_SENTINEL")

        monkeypatch.setattr(
            "clawjournal.workbench.daemon.drain_findings_backfill", fail_backfill
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.discover_projects",
            lambda source_filter=None: [],
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.link_subagent_hierarchy", lambda conn: 0
        )
        caplog.set_level(logging.DEBUG)

        report = Scanner().scan_once_strict(["claude"])

        assert report["ok"] is False
        assert "backfill_failed" in caplog.text
        assert "RAW_BACKFILL" not in caplog.text
        assert "RAW_OUTER_BACKFILL_SENTINEL" not in caplog.text
        assert "/private/backfill/project" not in caplog.text
        assert "Traceback" not in caplog.text


    def test_transport_zip_omits_local_export_path_but_keeps_disk_manifest(
        self, tmp_path
    ):
        sentinel = "/Users/alice/.clawjournal/shares/private-bundle"
        manifest_path = tmp_path / "manifest.json"
        manifest_path.write_text(
            json.dumps({
                "share_id": "share-1",
                "export_path": sentinel,
                "session_count": 1,
            }),
            encoding="utf-8",
        )
        (tmp_path / "sessions.jsonl").write_text("{}\n", encoding="utf-8")
        (tmp_path / "trufflehog.post-pii.json").write_text(
            "{}\n", encoding="utf-8"
        )

        zip_bytes = _build_share_zip(tmp_path)

        with zipfile.ZipFile(BytesIO(zip_bytes)) as archive:
            transported_bytes = archive.read("manifest.json")
            transported = json.loads(transported_bytes)
        assert "export_path" not in transported
        assert sentinel.encode("utf-8") not in transported_bytes
        assert (
            json.loads(manifest_path.read_text(encoding="utf-8"))["export_path"]
            == sentinel
        )

    def test_strict_scan_fails_when_enrolled_source_is_not_discovered(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr(
            "clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config"
        )
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.discover_projects",
            lambda source_filter=None: [],
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.link_subagent_hierarchy", lambda conn: 0
        )

        report = Scanner().scan_once_strict(["codex"])

        assert report["ok"] is False
        assert report["missing_sources"] == ["codex"]
        assert report["failures"] == [
            {
                "source": "codex",
                "stage": "discovery",
                "code": "source_not_discovered",
            }
        ]

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
            # Settled session (last activity well before the settle window),
            # so the auto-scorer grades it rather than deferring it as in-flight.
            "start_time": (now - timedelta(minutes=20)).isoformat(),
            "end_time": (now - timedelta(minutes=10)).isoformat(),
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

    def test_score_unscored_once_disables_on_no_backend_error(self, tmp_path, monkeypatch):
        """A 'no usable backend' RuntimeError must trip the circuit breaker so the
        scan loop stops retrying. Guards against the disable sentinel drifting away
        from resolve_backend's message."""
        from clawjournal.scoring.backends import NO_BACKEND_DETECTED_ERROR

        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")

        now = datetime.now(timezone.utc)
        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "sess-nb",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "start_time": now.isoformat(),
            "messages": [{"role": "user", "content": "Fix it", "tool_uses": []}],
            "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0},
        }])
        conn.close()

        def boom(conn, session_id, model=None, backend="auto"):
            raise RuntimeError(
                f"{NO_BACKEND_DETECTED_ERROR}. Install a supported agent CLI, "
                "set CLAWJOURNAL_SCORER_BACKEND, or pass --backend explicitly."
            )

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", boom)

        scanner = Scanner(source_filter="claude")
        assert scanner.score_unscored_once(limit=5) == 0
        assert scanner._auto_score_disabled_reason is not None
        assert NO_BACKEND_DETECTED_ERROR in scanner._auto_score_disabled_reason

    def _settled_session(self, sid, now):
        return {
            "session_id": sid, "project": "p", "source": "claude", "model": "m",
            "start_time": (now - timedelta(minutes=20)).isoformat(),
            "end_time": (now - timedelta(minutes=10)).isoformat(),
            "messages": [{"role": "user", "content": "Fix it", "tool_uses": []},
                         {"role": "assistant", "content": "Done", "tool_uses": []}],
            "stats": {"user_messages": 1, "assistant_messages": 1, "tool_uses": 0,
                      "input_tokens": 100, "output_tokens": 50},
        }

    def _ok_result(self, now):
        return SimpleNamespace(
            quality=4, reason="ok", detail_json="{}", task_type="t",
            outcome_label="resolved", value_labels=[], risk_level=[], display_title="T",
            effort_estimate=0.5, summary="s", failure_value_score=4, recovery_labels=[],
            failure_attribution="agent_caused", failure_modes=[], learning_summary="l",
            scorer_backend="claude", scorer_model="haiku", rubric_git_sha="sha",
            scored_at=now.isoformat())

    def test_score_unscored_once_falls_back_to_next_backend(self, tmp_path, monkeypatch):
        """codex out of credits -> auto-switch to the next installed backend."""
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")

        now = datetime.now(timezone.utc)
        conn = open_index()
        upsert_sessions(conn, [self._settled_session("sess-fb", now)])
        conn.close()

        monkeypatch.setattr("clawjournal.workbench.daemon.resolve_backend", lambda b: "codex")
        monkeypatch.setattr("clawjournal.workbench.daemon.installed_fallback_chain",
                            lambda primary: ["codex", "claude"])
        seen = []

        def fake_score(conn, session_id, model=None, backend="auto"):
            seen.append(backend)
            if backend == "codex":
                raise RuntimeError("codex exited 1: ERROR: Your workspace is out of credits.")
            return self._ok_result(now)

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", fake_score)

        scanner = Scanner(source_filter="claude")
        assert scanner.score_unscored_once(limit=5) == 1
        assert seen == ["codex", "claude"]  # tried codex, fell back to claude
        assert scanner._auto_score_disabled_reason is None

    def test_score_unscored_once_disables_when_all_backends_unavailable(self, tmp_path, monkeypatch):
        """Every backend out of credits -> arm the circuit breaker, don't loop."""
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")

        now = datetime.now(timezone.utc)
        conn = open_index()
        upsert_sessions(conn, [self._settled_session("sess-all", now)])
        conn.close()

        monkeypatch.setattr("clawjournal.workbench.daemon.resolve_backend", lambda b: "codex")
        monkeypatch.setattr("clawjournal.workbench.daemon.installed_fallback_chain",
                            lambda primary: ["codex", "claude"])

        def boom(conn, session_id, model=None, backend="auto"):
            raise RuntimeError("ERROR: out of credits.")

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", boom)

        scanner = Scanner(source_filter="claude")
        assert scanner.score_unscored_once(limit=5) == 0
        assert scanner._auto_score_disabled_reason is not None

    def test_score_unscored_once_respects_since_window(self, tmp_path, monkeypatch):
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
        since = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        assert scanner.score_unscored_once(limit=5, since=since) == 0
        assert calls["count"] == 0

    def test_score_unscored_once_uses_failure_corpus_even_with_scan_filter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")

        now = datetime.now(timezone.utc)
        conn = open_index()
        upsert_sessions(conn, [
            {
                "session_id": "cursor-sess",
                "project": "test-project",
                "source": "cursor",
                "model": "cursor",
                "start_time": now.isoformat(),
                "messages": [{"role": "user", "content": "Cursor task"}],
                "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0},
            },
            {
                "session_id": "codex-sess",
                "project": "test-project",
                "source": "codex",
                "model": "gpt-5",
                "start_time": (now - timedelta(minutes=1)).isoformat(),
                "messages": [{"role": "user", "content": "Codex task"}],
                "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0},
            },
        ])
        conn.close()

        scored_ids = []

        def fake_score(conn, session_id, model=None, backend="auto"):
            scored_ids.append(session_id)
            return SimpleNamespace(
                quality=4,
                reason="Good trace",
                detail_json='{"substance": 4}',
                task_type="debugging",
                outcome_label="resolved",
                value_labels=[],
                risk_level=[],
                display_title="Good trace",
                effort_estimate=0.5,
                summary="Useful trace",
                failure_value_score=4,
                recovery_labels=[],
                failure_attribution="agent_caused",
                failure_modes=[],
                learning_summary="Useful failure trace",
                scorer_backend="test",
                scorer_model="test-model",
                rubric_git_sha="test-sha",
                scored_at=now.isoformat(),
            )

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", fake_score)

        scanner = Scanner(source_filter="cursor")
        assert scanner.score_unscored_once(limit=5) == 1
        assert scored_ids == ["codex-sess"]

    def test_score_unscored_once_respects_confirmed_source_scope(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {"source": "claude"})

        now = datetime.now(timezone.utc)
        claude = self._settled_session("claude-sess", now)
        codex = {
            **self._settled_session("codex-sess", now),
            "source": "codex",
            "model": "gpt-5",
            "start_time": (now - timedelta(minutes=5)).isoformat(),
            "end_time": (now - timedelta(minutes=4)).isoformat(),
        }
        conn = open_index()
        upsert_sessions(conn, [claude, codex])
        conn.close()

        scored_ids = []

        def fake_score(conn, session_id, **kwargs):
            scored_ids.append(session_id)
            return self._ok_result(now)

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", fake_score)

        scanner = Scanner()
        assert scanner.score_unscored_once(limit=5) == 1
        assert scored_ids == ["claude-sess"]

    def test_score_unscored_once_does_not_widen_unsupported_confirmed_source(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {"source": "gemini"})

        now = datetime.now(timezone.utc)
        codex = {
            **self._settled_session("codex-sess", now),
            "source": "codex",
            "model": "gpt-5",
        }
        conn = open_index()
        upsert_sessions(conn, [codex])
        conn.close()

        def fake_score(*args, **kwargs):
            raise AssertionError("codex must not be scored under a gemini source scope")

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", fake_score)

        scanner = Scanner()
        assert scanner.score_unscored_once(limit=5) == 0

    def test_score_unscored_once_skips_held_and_excluded_without_starving(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")

        now = datetime.now(timezone.utc)
        held = self._settled_session("held", now)
        held["start_time"] = (now - timedelta(minutes=20)).isoformat()
        held["end_time"] = (now - timedelta(minutes=10)).isoformat()
        excluded = self._settled_session("excluded", now)
        excluded["project"] = "claude:private-repo"
        excluded["start_time"] = (now - timedelta(minutes=21)).isoformat()
        excluded["end_time"] = (now - timedelta(minutes=11)).isoformat()
        ok = self._settled_session("ok", now)
        ok["start_time"] = (now - timedelta(minutes=22)).isoformat()
        ok["end_time"] = (now - timedelta(minutes=12)).isoformat()

        conn = open_index()
        upsert_sessions(conn, [held, excluded, ok])
        set_hold_state(conn, "held", "pending_review", changed_by="test", reason="test")
        add_policy(conn, "exclude_project", "private-repo")
        conn.close()

        scored_ids = []

        def fake_score(conn, session_id, **kwargs):
            scored_ids.append(session_id)
            return self._ok_result(now)

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", fake_score)

        scanner = Scanner(source_filter="claude")
        assert scanner.score_unscored_once(limit=1) == 1
        assert scored_ids == ["ok"]

    def test_score_unscored_once_passes_policy_redaction_settings(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
        monkeypatch.setattr("clawjournal.workbench.daemon.CONFIG_DIR", tmp_path / "clawjournal_config")

        now = datetime.now(timezone.utc)
        conn = open_index()
        upsert_sessions(conn, [self._settled_session("redact-me", now)])
        add_policy(conn, "redact_string", "SecretProject")
        add_policy(conn, "redact_username", "kai")
        add_policy(conn, "block_domain", "api.internal")
        conn.close()

        seen_kwargs = []

        def fake_score(conn, session_id, **kwargs):
            seen_kwargs.append(kwargs)
            return self._ok_result(now)

        monkeypatch.setattr("clawjournal.scoring.scoring.score_session", fake_score)

        scanner = Scanner(source_filter="claude")
        assert scanner.score_unscored_once(limit=1) == 1
        settings = seen_kwargs[0]["redaction_settings"]
        assert settings["custom_strings"] == ["SecretProject"]
        assert settings["extra_usernames"] == ["kai"]
        assert settings["blocked_domains"] == ["api.internal"]


class TestScanAPI:
    def test_endpoint_reports_new_updated_and_unchanged_by_source(
        self, server_with_scanner, monkeypatch
    ):
        port, scanner = server_with_scanner
        scanner.scan_once = lambda: {"codex": 0, "claude": 2}
        scanner.last_updated_by_source = {"codex": 1}
        scanner.last_unchanged_by_source = {"codex": 4, "claude": 3}
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.trigger_scoring_warmup",
            lambda active_scanner: {"status": "disabled"},
        )

        status, data = _post(port, "/api/scan")

        assert status == 200
        assert data["new_sessions"] == {"codex": 0, "claude": 2}
        assert data["updated_sessions"] == {"codex": 1}
        assert data["unchanged_sessions"] == {"codex": 4, "claude": 3}


class TestScoringWarmupAPI:
    def test_endpoint_returns_disabled_without_scanner(self, server):
        status, data = _post(server, "/api/scoring/warmup")

        assert status == 200
        assert data["status"] == "disabled"

    def test_endpoint_requires_confirmation_for_detected_backend(self, server_with_scanner, monkeypatch):
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.workbench.daemon.detect_available_backend", lambda: "codex")
        monkeypatch.setattr("clawjournal.workbench.daemon.require_backend_command", lambda backend: backend)
        port, scanner = server_with_scanner

        status, data = _post(port, "/api/scoring/warmup")

        assert status == 200
        assert data["status"] == "needs_confirmation"
        assert data["backend"] == "codex"
        assert scanner.calls == []

    def test_endpoint_starts_after_confirmation_and_persists_backend(self, server_with_scanner, monkeypatch):
        config: dict[str, object] = {}
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: dict(config))
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", lambda cfg: config.update(cfg))
        monkeypatch.setattr("clawjournal.workbench.daemon.detect_available_backend", lambda: "codex")
        monkeypatch.setattr("clawjournal.workbench.daemon.require_backend_command", lambda backend: backend)
        port, scanner = server_with_scanner

        status, data = _post(
            port,
            "/api/scoring/warmup",
            {"confirm_backend": True, "backend": "codex"},
        )

        assert status == 200
        assert data["status"] == "started"
        assert data["backend"] == "codex"
        assert data["limit"] == 20
        assert scanner.calls == [{"limit": 20, "backend": "codex"}]
        assert config["scorer_backend"] == "codex"
        assert "scorer_backend_confirmed_at" in config

    def test_endpoint_reports_already_running_from_scanner(self, server_with_scanner, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.load_config",
            lambda: {"scorer_backend": "codex"},
        )
        monkeypatch.setattr("clawjournal.workbench.daemon.require_backend_command", lambda backend: backend)
        port, scanner = server_with_scanner
        scanner.status = "already_running"

        status, data = _post(port, "/api/scoring/warmup")

        assert status == 200
        assert data["status"] == "already_running"
        assert scanner.calls == [{"limit": 20, "backend": "codex"}]

    def test_helper_starts_when_confirmed_backend_missing_but_fallback_installed(self, monkeypatch):
        calls = []
        scanner = SimpleNamespace(
            trigger_auto_score=lambda **kw: calls.append(kw) or {"status": "started", **kw}
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.load_config",
            lambda: {"scorer_backend": "codex"},
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.installed_fallback_chain",
            lambda primary: ["codex", "claude"],
        )
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.resolve_backend",
            lambda backend: backend,
        )

        def require_backend(backend):
            if backend == "codex":
                raise RuntimeError("codex CLI not found. Install it.")
            return backend

        monkeypatch.setattr("clawjournal.workbench.daemon.require_backend_command", require_backend)

        result = trigger_scoring_warmup(scanner)

        assert result == {"status": "started", "limit": 20, "backend": "codex"}
        assert calls == [{"limit": 20, "backend": "codex"}]

    def test_helper_disables_when_no_backend_detected(self, monkeypatch):
        scanner = SimpleNamespace(trigger_auto_score=lambda **kw: {"status": "started"})
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.workbench.daemon.detect_available_backend", lambda: None)

        result = trigger_scoring_warmup(scanner)

        assert result["status"] == "disabled"


class TestProjectsAPI:
    def test_projects(self, server):
        status, data = _get(server, "/api/projects")
        assert status == 200
        assert len(data) >= 1
        assert data[0]["project"] == "test-project"


class TestShareDestinationAPI:
    def test_missing_ingest_url_error_points_to_workbench_submit(self):
        message = _missing_ingest_url_error()

        assert "Share tab's Submit step" in message
        assert "bundle-export <bundle_id> --zip" in message
        assert "CLAWJOURNAL_INGEST_URL" in message

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
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)

        # `_handle_share_destination` fetches hosted capabilities; without a
        # mock this test would hit the real `data.rayward.ai`. Simulate an
        # unreachable hosted service so the handler degrades to its
        # "configured but capabilities unavailable" branch.
        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=urllib.error.URLError("hosted unreachable"),
        ):
            status, data = _get(server, "/api/share-destination")

        assert status == 200
        assert data["configured"] is True
        assert data["daemon_upload_supported"] is False
        assert data["preferred_upload_flow"] == "browser_zip"
        assert data["cli_ingest_supported"] is False
        assert data["share_page_url"] == "https://data.rayward.ai/share"
        assert "capabilities could not be loaded" in (data.get("message") or "")

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

    def test_ipv6_loopback_share_destination_is_allowed(self, monkeypatch):
        from clawjournal.workbench import daemon

        monkeypatch.setattr(
            daemon,
            "_HOSTED_SHARE_URL",
            "http://[::1]:8080/share",
        )

        share_url, _message = daemon._validated_hosted_share_url()

        assert share_url == "http://[::1]:8080/share"


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

    def test_create_rejects_revision_changed_after_ui_review(self, server):
        conn = open_index()
        try:
            reviewed_revision = conn.execute(
                "SELECT content_revision FROM sessions WHERE session_id = 'sess-0'"
            ).fetchone()[0]
            session = {
                "session_id": "sess-0",
                "project": "test-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "messages": [
                    {"role": "user", "content": "Task 0: extended result", "tool_uses": []},
                    {"role": "assistant", "content": "Done.", "tool_uses": []},
                ],
                "stats": {
                    "user_messages": 1,
                    "assistant_messages": 1,
                    "tool_uses": 0,
                    "input_tokens": 100,
                    "output_tokens": 50,
                },
            }
            upsert_sessions(conn, [session])
        finally:
            conn.close()

        status, data = _post(server, "/api/shares", {
            "session_ids": ["sess-0"],
            "expected_revisions": {"sess-0": reviewed_revision},
        })

        assert status == 409
        assert data["block_reason"] == "revision_conflict"
        assert data["blockers"][0]["session_id"] == "sess-0"

    def test_create_rejects_exact_revision_already_shared(self, server):
        from clawjournal.workbench.index import create_share

        conn = open_index()
        try:
            first_share = create_share(conn, ["sess-0"])
            conn.execute(
                "UPDATE shares SET status = 'shared', shared_at = ? WHERE share_id = ?",
                ("2026-07-01T00:00:00+00:00", first_share),
            )
            conn.commit()
        finally:
            conn.close()

        status, data = _post(server, "/api/shares", {
            "session_ids": ["sess-0"],
        })

        assert status == 409
        assert "already shared" in data["error"]
        assert data["blockers"][0]["session_id"] == "sess-0"

    def test_create_rejects_unapproved_extension_of_shared_trace(self, server):
        from clawjournal.workbench.index import create_share

        conn = open_index()
        try:
            first_share = create_share(conn, ["sess-0"])
            conn.execute(
                "UPDATE shares SET status = 'shared', shared_at = ? WHERE share_id = ?",
                ("2026-07-01T00:00:00+00:00", first_share),
            )
            conn.commit()
            upsert_sessions(conn, [{
                "session_id": "sess-0",
                "project": "test-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "messages": [
                    {"role": "user", "content": "Task 0: extended result", "tool_uses": []},
                    {"role": "assistant", "content": "A new iteration.", "tool_uses": []},
                ],
                "stats": {
                    "user_messages": 1,
                    "assistant_messages": 1,
                    "tool_uses": 0,
                    "input_tokens": 120,
                    "output_tokens": 60,
                },
            }])
        finally:
            conn.close()

        status, data = _post(server, "/api/shares", {
            "session_ids": ["sess-0"],
        })

        assert status == 409
        assert "fresh approval" in data["error"]
        assert data["blockers"][0]["review_status"] == "new"

    def test_create_rejects_sessions_outside_configured_source_scope(self, server, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.load_config",
            lambda: {"source": "both", "projects_confirmed": True},
        )
        conn = open_index()
        try:
            upsert_sessions(conn, [{
                "session_id": "gemini-out",
                "project": "gemini:private",
                "source": "gemini",
                "model": "gemini-cli",
                "start_time": "2025-01-04T00:00:00+00:00",
                "end_time": "2025-01-04T00:10:00+00:00",
                "messages": [{"role": "user", "content": "private", "tool_uses": []}],
                "stats": {
                    "user_messages": 1,
                    "assistant_messages": 0,
                    "tool_uses": 0,
                    "input_tokens": 10,
                    "output_tokens": 0,
                },
            }])
        finally:
            conn.close()

        status, data = _post(server, "/api/shares", {
            "session_ids": ["sess-0", "gemini-out"],
            "note": "Source scope test",
        })

        assert status == 409
        assert "source scope" in data["error"]
        assert [b["session_id"] for b in data["blockers"]] == ["gemini-out"]


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


class TestFrontendStaleWarning:
    @pytest.mark.parametrize("changed_input", [
        "index.html",
        "public/icons.svg",
        "vite.config.ts",
    ])
    def test_warns_when_build_input_outside_src_is_newer(
        self, tmp_path, monkeypatch, capsys, changed_input
    ):
        frontend = tmp_path / "frontend"
        dist = frontend / "dist"
        monkeypatch.setattr("clawjournal.workbench.daemon.FRONTEND_DIST", dist)

        _write_with_mtime(dist / "index.html", "<!doctype html>", 200)
        _write_with_mtime(frontend / "src" / "App.tsx", "export {}", 100)
        _write_with_mtime(frontend / changed_input, "changed", 300)

        _warn_if_frontend_stale()

        err = capsys.readouterr().err
        assert "frontend bundle is STALE" in err
        assert "build inputs are newer than dist" in err

    def test_silent_when_src_tree_missing_like_packaged_wheel(
        self, tmp_path, monkeypatch, capsys
    ):
        frontend = tmp_path / "frontend"
        dist = frontend / "dist"
        monkeypatch.setattr("clawjournal.workbench.daemon.FRONTEND_DIST", dist)

        _write_with_mtime(dist / "index.html", "<!doctype html>", 100)
        _write_with_mtime(frontend / "public" / "icons.svg", "<svg />", 200)

        _warn_if_frontend_stale()

        assert capsys.readouterr().err == ""


class TestReloadSupervisor:
    def test_reload_child_command_uses_module_invocation(self, monkeypatch):
        import clawjournal.workbench.daemon as daemon

        monkeypatch.setattr(daemon.sys, "executable", "/venv/bin/python")
        monkeypatch.setattr(
            daemon.sys,
            "argv",
            ["clawjournal/cli.py", "serve", "--reload", "--port", "9999"],
        )

        assert _reload_child_command() == [
            "/venv/bin/python",
            "-m",
            "clawjournal.cli",
            "serve",
            "--reload",
            "--port",
            "9999",
        ]


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

    def test_index_html_no_store_and_assets_cacheable(self, server, tmp_path, monkeypatch):
        # Point the daemon at a real built-shaped dist so the _serve_static
        # text/html branch runs (the index_setup fixture's nonexistent dist would
        # serve the placeholder instead). index.html must be no-store — it
        # references content-hashed asset names, so a cached copy pins the browser
        # to a stale bundle after a rebuild — while hashed /assets/* must stay
        # implicitly cacheable (no Cache-Control), or no-store would defeat
        # content-hash caching.
        dist = tmp_path / "dist"
        (dist / "assets").mkdir(parents=True)
        (dist / "index.html").write_text("<!doctype html><html><body>cj</body></html>")
        (dist / "assets" / "app-abc123.js").write_text("console.log('cj')")
        monkeypatch.setattr("clawjournal.workbench.daemon.FRONTEND_DIST", dist)

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", "/")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 200
        assert resp.getheader("Content-Type") == "text/html"
        assert resp.getheader("Cache-Control") == "no-store, must-revalidate"

        conn = HTTPConnection("127.0.0.1", server, timeout=5)
        conn.request("GET", "/assets/app-abc123.js")
        resp = conn.getresponse()
        resp.read()
        assert resp.status == 200
        assert resp.getheader("Cache-Control") is None

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


def test_redaction_settings_fingerprint_covers_all_inputs():
    """Every redaction-affecting input changes the fingerprint; ai_pii does not;
    and list order within a field does not — guards against a typo dropping a
    field from the hash or making it order-sensitive."""
    from clawjournal.workbench.daemon import _redaction_settings_fingerprint

    base = {
        "custom_strings": ["a"],
        "extra_usernames": ["u"],
        "excluded_projects": ["/p"],
        "blocked_domains": ["x.com"],
        "allowlist_entries": [{"value": "v"}],
    }
    base_fp = _redaction_settings_fingerprint(base)

    for key, changed in [
        ("custom_strings", ["a", "b"]),
        ("extra_usernames", ["u", "w"]),
        ("excluded_projects", ["/p", "/q"]),
        ("blocked_domains", ["x.com", "y.com"]),
        ("allowlist_entries", [{"value": "v2"}]),
    ]:
        assert _redaction_settings_fingerprint({**base, key: changed}) != base_fp, \
            f"changing {key} must invalidate the fingerprint"

    # ai_pii is intentionally NOT part of the fingerprint (gated separately).
    assert _redaction_settings_fingerprint({**base, "ai_pii_review_enabled": True}) == base_fp

    # Order within a list field does not change the hash.
    assert (
        _redaction_settings_fingerprint({**base, "custom_strings": ["b", "a", "c"]})
        == _redaction_settings_fingerprint({**base, "custom_strings": ["c", "a", "b"]})
    )


class TestSelfHostedRevisionConflicts:
    def _share(self, monkeypatch):
        from clawjournal.workbench.index import create_share

        _mock_trufflehog_clean(monkeypatch)
        conn = open_index()
        set_hold_state(conn, "sess-0", "released", changed_by="user")
        share_id = create_share(conn, ["sess-0"])
        monkeypatch.setattr(
            "clawjournal.workbench.daemon._ensure_self_hosted_upload_credentials",
            lambda: ("test@university.edu", "token"),
        )
        return conn, share_id

    @staticmethod
    def _conflict_response(req, *, matching: bool):
        import re

        body = req.data.decode("utf-8", errors="replace")
        match = re.search(
            r'name="bundle_hash"\r\n\r\n([0-9a-f]{64})\r\n',
            body,
        )
        assert match is not None
        confirmed_hash = match.group(1) if matching else "0" * 64
        payload = json.dumps({
            "error": "already exists",
            "idempotent": True,
            "bundle_hash": confirmed_hash,
            "gcs_uri": "gs://test/existing/sessions.jsonl",
        }).encode()
        raise urllib.error.HTTPError(
            req.full_url,
            409,
            "Conflict",
            hdrs=None,
            fp=BytesIO(payload),
        )

    def test_accepts_only_proven_same_bundle_409(self, index_setup, monkeypatch):
        from clawjournal.workbench.daemon import upload_share_to_self_hosted_ingest

        conn, share_id = self._share(monkeypatch)
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            lambda req, timeout: self._conflict_response(req, matching=True),
        )
        try:
            result = upload_share_to_self_hosted_ingest(conn, share_id)
            row = conn.execute(
                "SELECT status, shared_at FROM shares WHERE share_id = ?",
                (share_id,),
            ).fetchone()
        finally:
            conn.close()

        assert result["ok"] is True
        assert row["status"] == "shared"
        assert row["shared_at"] is not None

    def test_rejects_mismatched_409_without_marking_shared(
        self, index_setup, monkeypatch
    ):
        from clawjournal.workbench.daemon import upload_share_to_self_hosted_ingest

        conn, share_id = self._share(monkeypatch)
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            lambda req, timeout: self._conflict_response(req, matching=False),
        )
        try:
            result = upload_share_to_self_hosted_ingest(conn, share_id)
            row = conn.execute(
                "SELECT status, shared_at FROM shares WHERE share_id = ?",
                (share_id,),
            ).fetchone()
        finally:
            conn.close()

        assert result["status"] == 409
        assert tuple(row) == ("exported", None)

    @pytest.mark.parametrize("mutation", ["hold", "revision", "source"])
    def test_final_egress_gate_rechecks_mutable_gates(
        self, index_setup, monkeypatch, mutation
    ):
        from clawjournal.workbench import daemon

        conn, share_id = self._share(monkeypatch)
        original_build = daemon._build_multipart_body

        def build_then_mutate(*args, **kwargs):
            result = original_build(*args, **kwargs)
            if mutation == "hold":
                set_hold_state(
                    conn,
                    "sess-0",
                    "pending_review",
                    changed_by="test",
                    reason="inject hold immediately before egress",
                )
            elif mutation == "revision":
                conn.execute(
                    "UPDATE sessions SET content_revision = ? WHERE session_id = ?",
                    ("changed-after-review", "sess-0"),
                )
                conn.commit()
            else:
                conn.execute(
                    "UPDATE sessions SET source = ? WHERE session_id = ?",
                    ("codex", "sess-0"),
                )
                conn.commit()
            return result

        monkeypatch.setattr(daemon, "_build_multipart_body", build_then_mutate)
        egress_calls = 0

        def unexpected_egress(*args, **kwargs):
            nonlocal egress_calls
            egress_calls += 1
            raise AssertionError("self-hosted upload egress must be blocked")

        monkeypatch.setattr(daemon.urllib.request, "urlopen", unexpected_egress)
        try:
            result = daemon.upload_share_to_self_hosted_ingest(
                conn, share_id, source_filter="claude"
            )
        finally:
            conn.close()

        assert result["status"] == 409
        assert egress_calls == 0
        if mutation == "hold":
            assert result["blockers"] == [
                {
                    "session_id": "sess-0",
                    "hold_state": "pending_review",
                    "embargo_until": None,
                }
            ]
        elif mutation == "revision":
            assert result["block_reason"] == "revision_conflict"
            assert result["blocked_sessions"][0]["session_id"] == "sess-0"
        else:
            assert result["blockers"][0]["source"] == "codex"
            assert result["blockers"][0]["allowed_sources"] == "claude"


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
        assert _kw.get("backend") == "codex"
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

    summary = _apply_upload_pii_redactions(
        sessions_file,
        ai_pii=True,
        backend="codex",
    )

    assert summary["workers"] == 4
    assert summary["agent_timeout_seconds"] == 23
    assert summary["ai_enabled"] is True
    assert summary["backend"] == "codex"
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


def test_upload_pii_redaction_defaults_to_rules_only(tmp_path, monkeypatch):
    sessions_file = tmp_path / "sessions.jsonl"
    sessions_file.write_text(
        json.dumps({
            "session_id": "rules-only",
            "messages": [{"role": "user", "content": "Email alice@example.com"}],
        }) + "\n",
        encoding="utf-8",
    )

    def fail_hybrid(*_args, **_kwargs):
        raise AssertionError("AI PII review should be opt-in")

    monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_hybrid", fail_hybrid)

    summary = _apply_upload_pii_redactions(sessions_file)

    assert summary["ai_enabled"] is False
    assert summary["workers"] == 0
    assert summary["agent_timeout_seconds"] == 0
    assert summary["finding_count"] == 1
    assert summary["replacement_count"] == 1
    assert summary["coverage"] == {"full": 0, "rules_only": 1}
    assert "alice@example.com" not in sessions_file.read_text(encoding="utf-8")


@pytest.mark.parametrize("ai_pii", [False, True])
def test_upload_pii_summary_reports_backend_for_zero_row_export(tmp_path, ai_pii):
    # An all-excluded share writes an empty sessions.jsonl. Callers read
    # pii_summary["backend"] unconditionally, so the empty-sessions early
    # return must include the key or finalize raises KeyError('backend').
    sessions_file = tmp_path / "sessions.jsonl"
    sessions_file.write_text("", encoding="utf-8")

    summary = _apply_upload_pii_redactions(
        sessions_file, ai_pii=ai_pii, backend="codex"
    )

    assert summary["session_count"] == 0
    assert "backend" in summary
    assert summary["backend"] is None


@pytest.mark.parametrize("raised", ["control_changed", "wrapped_gate_error"])
def test_finalize_reraises_control_gate_instead_of_swallowing(
    tmp_path, monkeypatch, raised
):
    # A before_ai_call control gate firing during AI-PII review must propagate
    # as ControlChanged (so the runner records a clean control stop), not be
    # collapsed into a generic retryable packaging failure by the blanket except.
    #
    # The realistic shape is a *bare* ControlChanged: review_session_pii_hybrid
    # unwraps _AgentCallGateError to its .cause before it can reach finalize, so
    # _apply_upload_pii_redactions raises ControlChanged, never the wrapper.
    # (The wrapped case is exercised too, as defense in depth.)
    from clawjournal.workbench import daemon as daemon_module
    from clawjournal.redaction.pii import _AgentCallGateError
    from clawjournal.auto_upload import ControlChanged

    export_dir = tmp_path / "share"
    export_dir.mkdir()
    (export_dir / "sessions.jsonl").write_text('{"session_id":"s1"}\n', encoding="utf-8")

    control = ControlChanged("paused mid-review")

    def gate_fires(*_args, **_kwargs):
        if raised == "control_changed":
            raise control
        raise _AgentCallGateError(control)

    monkeypatch.setattr(daemon_module, "_apply_upload_pii_redactions", gate_fires)

    with pytest.raises(ControlChanged):
        daemon_module.finalize_share_export_for_upload(
            export_dir,
            {"redaction_summary": {}},
            ai_pii=True,
            ai_backend="codex",
            before_ai_call=lambda: None,
        )


def test_finalized_ai_manifest_requires_complete_nested_coverage():
    from clawjournal.workbench.daemon import _manifest_is_finalized_for_upload

    base = {
        "redaction_summary": {
            "pii_review": {
                "session_count": 2,
                "ai_enabled": True,
                "backend": "codex",
                "coverage": {"full": 2, "rules_only": 0},
            },
            "trufflehog_post_pii": {
                "findings": 0,
                "bypassed": False,
                "binary_missing": False,
                "scan_error": None,
            },
        }
    }

    assert _manifest_is_finalized_for_upload(
        base,
        ai_pii=True,
        ai_backend="codex",
    )
    partial = json.loads(json.dumps(base))
    partial["redaction_summary"]["pii_review"]["coverage"] = {
        "full": 1,
        "rules_only": 1,
    }
    assert not _manifest_is_finalized_for_upload(
        partial,
        ai_pii=True,
        ai_backend="codex",
    )
    legacy = json.loads(json.dumps(base))
    del legacy["redaction_summary"]["pii_review"]["coverage"]
    assert not _manifest_is_finalized_for_upload(legacy, ai_pii=True)


def test_finalize_blocks_rules_only_fallback_when_ai_configured(tmp_path, monkeypatch):
    from clawjournal.workbench import daemon

    (tmp_path / "sessions.jsonl").write_text("{}\n", encoding="utf-8")
    (tmp_path / "manifest.json").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        daemon,
        "_apply_upload_pii_redactions",
        lambda *_args, **_kwargs: {
            "session_count": 1,
            "finding_count": 0,
            "replacement_count": 0,
            "coverage": {"full": 0, "rules_only": 1},
            "workers": 1,
            "agent_timeout_seconds": 180,
            "ai_enabled": True,
            "backend": "codex",
        },
    )

    error, manifest = daemon.finalize_share_export_for_upload(
        tmp_path,
        {},
        ai_pii=True,
        ai_backend="codex",
    )

    assert error is not None
    assert error["block_reason"] == "ai-pii-incomplete"
    assert manifest["redaction_summary"]["pii_review"]["coverage"] == {
        "full": 0,
        "rules_only": 1,
    }
    persisted = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert persisted["redaction_summary"]["pii_review"]["backend"] == "codex"


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

    def test_request_verification_falls_back_when_capabilities_unavailable(self, monkeypatch):
        """A momentarily unreachable capabilities doc must not block requesting a
        code: domain validation falls back to the built-in default suffixes and
        the verify-email POST still proceeds."""
        from clawjournal.workbench.daemon import request_email_verification

        saved = {}
        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", lambda config: saved.update(config))

        def mock_urlopen(req, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/.well-known/clawjournal-share.json" in url:
                raise urllib.error.URLError("capabilities down")
            if "/api/verify-email" in url:
                resp = MagicMock()
                resp.read.return_value = json.dumps({
                    "verification_id": "verify-123",
                    "expires_at": "2026-01-01T00:00:00+00:00",
                }).encode()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                return resp
            raise ValueError(f"Unexpected URL: {url}")

        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=mock_urlopen):
            result = request_email_verification("test@university.edu")

        assert result["verification_id"] == "verify-123"
        assert saved["pending_verification_id"] == "verify-123"

    def test_request_verification_still_rejects_bad_domain_when_capabilities_down(self, monkeypatch):
        """The capabilities fallback must not weaken domain validation: a
        non-academic address is still rejected using the default suffixes."""
        from clawjournal.workbench.daemon import request_email_verification

        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})

        def mock_urlopen(req, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/.well-known/clawjournal-share.json" in url:
                raise urllib.error.URLError("capabilities down")
            raise AssertionError("verify-email POST should not be reached for a rejected domain")

        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=mock_urlopen):
            with pytest.raises(ValueError):
                request_email_verification("someone@gmail.com")

    def test_request_verification_clears_stale_token_on_email_switch(self, monkeypatch):
        """Requesting a code for a different email must drop the previously
        verified email's upload token, so a later submit cannot upload under
        the old identity while the UI shows the new email."""
        from clawjournal.workbench.daemon import request_email_verification

        state = {
            "verified_email": "old@university.edu",
            "verified_email_token": "old-token",
            "verified_email_token_expires_at": int(time.time()) + 3600,
        }
        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: dict(state))

        def save_state(updated):
            state.clear()
            state.update(updated)

        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", save_state)

        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            request_email_verification("new@university.edu")

        assert "verified_email" not in state
        assert "verified_email_token" not in state
        assert "verified_email_token_expires_at" not in state
        assert state["pending_verification_email"] == "new@university.edu"

    def test_request_verification_keeps_token_when_same_email(self, monkeypatch):
        """Re-verifying the SAME email keeps the existing token until the new
        one is confirmed (a token refresh, not an identity switch)."""
        from clawjournal.workbench.daemon import request_email_verification

        state = {
            "verified_email": "same@university.edu",
            "verified_email_token": "keep-token",
            "verified_email_token_expires_at": int(time.time()) + 3600,
        }
        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "https://hosted.example.test/share")
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: dict(state))

        def save_state(updated):
            state.clear()
            state.update(updated)

        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", save_state)

        with patch("clawjournal.workbench.daemon.urllib.request.urlopen", side_effect=_mock_urlopen_factory()):
            request_email_verification("Same@University.edu")

        assert state["verified_email_token"] == "keep-token"
        assert state["pending_verification_email"] == "same@university.edu"


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
        assert detail["zip_size_bytes"] > 0
        assert "gcs_uri" not in detail

        status, share_list = _get(server, "/api/shares")
        assert status == 200
        listed = next(share for share in share_list if share["share_id"] == share_id)
        assert listed["hosted_receipt_id"] == "rcpt-test-123"
        assert "gcs_uri" not in listed

    @pytest.mark.parametrize("mutation", ["hold", "revision", "source"])
    def test_hosted_final_egress_gate_rechecks_mutable_gates(
        self, monkeypatch, mutation
    ):
        from clawjournal.workbench import daemon
        from clawjournal.workbench.index import create_share

        conn = open_index()
        set_hold_state(conn, "sess-0", "released", changed_by="user")
        share_id = create_share(conn, ["sess-0"])
        monkeypatch.setattr(daemon, "load_config", lambda: _share_config())

        original_build = daemon._build_multipart_body

        def build_then_mutate(*args, **kwargs):
            result = original_build(*args, **kwargs)
            if mutation == "hold":
                set_hold_state(
                    conn,
                    "sess-0",
                    "pending_review",
                    changed_by="test",
                    reason="inject hold immediately before egress",
                )
            elif mutation == "revision":
                conn.execute(
                    "UPDATE sessions SET content_revision = ? WHERE session_id = ?",
                    ("changed-after-review", "sess-0"),
                )
                conn.commit()
            else:
                conn.execute(
                    "UPDATE sessions SET source = ? WHERE session_id = ?",
                    ("codex", "sess-0"),
                )
                conn.commit()
            return result

        monkeypatch.setattr(daemon, "_build_multipart_body", build_then_mutate)
        base_urlopen = _mock_urlopen_factory()
        submission_calls = 0

        def urlopen(req, **kwargs):
            nonlocal submission_calls
            if "/api/submissions" in req.full_url:
                submission_calls += 1
            return base_urlopen(req, **kwargs)

        monkeypatch.setattr(daemon.urllib.request, "urlopen", urlopen)
        try:
            result = daemon.submit_share_to_hosted(
                conn,
                share_id,
                accept_terms=True,
                ownership_certification=True,
                consent_version="consent-v1",
                retention_policy_version="retention-v1",
                settings={
                    "custom_strings": [],
                    "extra_usernames": [],
                    "excluded_projects": [],
                    "blocked_domains": [],
                    "allowlist_entries": [],
                    "source_filter": "claude",
                },
            )
        finally:
            conn.close()

        assert result["status"] == 409
        assert submission_calls == 0
        if mutation == "hold":
            assert result["blockers"][0]["hold_state"] == "pending_review"
        elif mutation == "revision":
            assert result["block_reason"] == "revision_conflict"
            assert result["blocked_sessions"][0]["session_id"] == "sess-0"
        else:
            assert result["blockers"][0]["source"] == "codex"
            assert result["blockers"][0]["allowed_sources"] == "claude"

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

    def test_share_upload_timeout_is_ambiguous_and_preserves_state(self, server, monkeypatch):
        """A timeout after the bundle bytes were sent is ambiguous: the server
        may have accepted it. Don't clear the token or mark the share shared,
        and warn against a duplicate-causing blind retry."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        saved = {}
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", lambda updated: saved.update(updated))

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(upload_error=TimeoutError("timed out")),
        ):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 504, data
        assert data.get("ambiguous") is True
        assert "duplicate" in data["error"].lower()
        # Token not cleared (single-use; the submission may actually have landed).
        assert saved == {}
        conn = open_index()
        row = conn.execute(
            "SELECT status, hosted_receipt_id FROM shares WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        conn.close()
        assert row["status"] != "shared"
        assert row["hosted_receipt_id"] is None

    def test_share_upload_connection_refused_is_safe_retry(self, server, monkeypatch):
        """A pre-send connection failure never reached the server, so it stays a
        plain 502 'try again' without the ambiguous-duplicate warning."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(upload_error=urllib.error.URLError("connection refused")),
        ):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 502, data
        assert not data.get("ambiguous")
        assert "try again" in data["error"].lower()

    def test_share_upload_urlerror_wrapping_timeout_is_ambiguous(self, server, monkeypatch):
        """A URLError whose reason is a timeout (a timeout raised while writing
        the request body) is treated like a bare TimeoutError: ambiguous 504,
        not a safe-retry 502 — exercises the wrapped-timeout branch."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(upload_error=urllib.error.URLError(TimeoutError("timed out"))),
        ):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 504, data
        assert data.get("ambiguous") is True
        assert "duplicate" in data["error"].lower()

    def test_failed_submit_still_starts_cooldown(self, server, monkeypatch):
        """A failed submit marks in-flight, so an immediate retry is rate-limited
        — previously the cooldown only advanced on success, leaving a retry-loop
        / concurrent-submit window open."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(upload_error=TimeoutError("timed out")),
        ):
            status, _ = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())
        assert status == 504

        status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())
        assert status == 429
        assert "Rate limited" in data["error"]

    def test_missing_consent_does_not_consume_cooldown(self, server, monkeypatch):
        """A malformed upload (missing consent fields) is rejected 400 BEFORE the
        rate-limit gate, so it must not start the cooldown — a corrected submit
        immediately afterwards proceeds rather than getting a 429."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        status, data = _post(server, f"/api/shares/{share_id}/upload", {})
        assert status == 400
        assert "missing" in data

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(),
        ):
            status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())
        assert status == 200, data
        assert data["ok"] is True

    def _verify_email_mock_with_dev_code(self, dev_code):
        """A urlopen mock that returns ``dev_code`` from /api/verify-email."""
        base = _mock_urlopen_factory()

        def mock_urlopen(req, **kwargs):
            url = req.full_url if hasattr(req, "full_url") else str(req)
            if "/api/verify-email" in url and "/confirm" not in url:
                resp = MagicMock()
                resp.read.return_value = json.dumps({
                    "verification_id": "verify-123",
                    "expires_at": "2026-01-01T00:00:00+00:00",
                    "dev_code": dev_code,
                }).encode()
                resp.__enter__ = lambda s: s
                resp.__exit__ = MagicMock(return_value=False)
                return resp
            return base(req, **kwargs)

        return mock_urlopen

    def test_verify_email_suppresses_dev_code_on_prod_url(self, server, monkeypatch):
        """dev_code must never reach the browser against a production hosted URL,
        even if the server returns it."""
        # The class autouse fixture points _HOSTED_SHARE_URL at a prod-like https URL.
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", lambda updated: None)

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=self._verify_email_mock_with_dev_code("000000"),
        ):
            status, data = _post(server, "/api/share/verify-email", {"email": "test@university.edu"})

        assert status == 200
        assert "dev_code" not in data

    def test_verify_email_surfaces_dev_code_on_local_url(self, server, monkeypatch):
        """On a localhost (dev) hosted URL, dev_code is surfaced for convenience."""
        monkeypatch.setattr("clawjournal.workbench.daemon._HOSTED_SHARE_URL", "http://localhost:8799/share")
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_capabilities_cache", None)
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.workbench.daemon.save_config", lambda updated: None)

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=self._verify_email_mock_with_dev_code("424242"),
        ):
            status, data = _post(server, "/api/share/verify-email", {"email": "test@university.edu"})

        assert status == 200
        assert data.get("dev_code") == "424242"

    def test_quick_share_blocks_held_session(self, server, monkeypatch):
        """quick-share leads straight to submit, so it must fail fast (409) when
        a selected session is on hold instead of packaging an unsubmittable
        bundle."""
        from clawjournal.workbench.index import open_index, set_hold_state, upsert_sessions

        WorkbenchHandler._last_share_time = 0.0
        conn = open_index()
        try:
            upsert_sessions(conn, [{
                "session_id": "qs-held",
                "project": "test-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "messages": [{"role": "user", "content": "held content", "tool_uses": []}],
                "stats": {
                    "user_messages": 1, "assistant_messages": 0,
                    "tool_uses": 0, "input_tokens": 100, "output_tokens": 0,
                },
            }])
            set_hold_state(conn, "qs-held", "pending_review", changed_by="user", reason="test")
        finally:
            conn.close()

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})
        status, data = _post(server, "/api/quick-share", {"session_ids": ["qs-held"]})

        assert status == 409, data
        assert data["blockers"][0]["session_id"] == "qs-held"
        assert data["blockers"][0]["hold_state"] == "pending_review"

    def test_quick_share_rejects_exact_revision_already_shared(self, server):
        from clawjournal.workbench.index import create_share

        WorkbenchHandler._last_share_time = 0.0
        conn = open_index()
        try:
            share_id = create_share(conn, ["sess-0"])
            conn.execute(
                "UPDATE shares SET status='shared', shared_at=? WHERE share_id=?",
                ("2026-07-01T00:00:00+00:00", share_id),
            )
            conn.commit()
        finally:
            conn.close()

        status, data = _post(server, "/api/quick-share", {
            "session_ids": ["sess-0"],
        })

        assert status == 409
        assert "already shared" in data["error"]

    def test_quick_share_rejects_unapproved_extension(self, server):
        from clawjournal.workbench.index import create_share

        WorkbenchHandler._last_share_time = 0.0
        conn = open_index()
        try:
            share_id = create_share(conn, ["sess-0"])
            conn.execute(
                "UPDATE shares SET status='shared', shared_at=? WHERE share_id=?",
                ("2026-07-01T00:00:00+00:00", share_id),
            )
            conn.commit()
            upsert_sessions(conn, [{
                "session_id": "sess-0",
                "project": "test-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "messages": [
                    {"role": "user", "content": "extended iteration", "tool_uses": []},
                ],
                "stats": {
                    "user_messages": 1,
                    "assistant_messages": 0,
                    "tool_uses": 0,
                    "input_tokens": 100,
                    "output_tokens": 0,
                },
            }])
        finally:
            conn.close()

        status, data = _post(server, "/api/quick-share", {
            "session_ids": ["sess-0"],
        })

        assert status == 409
        assert "fresh approval" in data["error"]

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

    def test_hosted_upload_rejects_stale_replacement_predecessor(
        self, server, monkeypatch
    ):
        from clawjournal.workbench.index import create_share, update_session

        WorkbenchHandler._last_share_time = 0.0
        conn = open_index()
        try:
            set_hold_state(conn, "sess-0", "released", changed_by="user")
            share_r1 = create_share(conn, ["sess-0"])
            conn.execute(
                "UPDATE shares SET status='shared', shared_at=? WHERE share_id=?",
                ("2026-07-01T00:00:00+00:00", share_r1),
            )
            conn.commit()

            upsert_sessions(conn, [{
                "session_id": "sess-0",
                "project": "test-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "messages": [{"role": "user", "content": "revision two"}],
                "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0},
            }])
            update_session(conn, "sess-0", status="approved")
            share_r2 = create_share(conn, ["sess-0"])

            upsert_sessions(conn, [{
                "session_id": "sess-0",
                "project": "test-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "messages": [{"role": "user", "content": "revision three"}],
                "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0},
            }])
            update_session(conn, "sess-0", status="approved")
            share_r3 = create_share(conn, ["sess-0"])
            conn.execute(
                "UPDATE shares SET status='shared', shared_at=? WHERE share_id=?",
                ("2026-07-03T00:00:00+00:00", share_r3),
            )
            conn.commit()
        finally:
            conn.close()

        monkeypatch.setattr(
            "clawjournal.workbench.daemon.load_config", lambda: _share_config()
        )
        status, data = _post(
            server,
            f"/api/shares/{share_r2}/upload",
            self._consent_body(),
        )

        assert status == 409
        assert data["blockers"][0]["reason"] == "stale_predecessor"

    def test_duplicate_receipt_returned_even_without_valid_token(self, server, monkeypatch):
        """Receipt hydration/retry should not require a still-valid upload token."""
        WorkbenchHandler._last_share_time = 0.0
        share_id = self._create_and_export_share(server)
        conn = open_index()
        conn.execute(
            "UPDATE shares SET status = 'shared', shared_at = ?, "
            "hosted_receipt_id = ?, hosted_status = ? WHERE share_id = ?",
            (
                "2026-01-01T00:00:00+00:00",
                "rcpt-existing-123",
                "received",
                share_id,
            ),
        )
        conn.commit()
        conn.close()

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})

        status, data = _post(server, f"/api/shares/{share_id}/upload", self._consent_body())

        assert status == 409
        assert data["receipt_id"] == "rcpt-existing-123"
        assert data["hosted_status"] == "received"
        assert "already submitted" in data["error"]

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

        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: {})

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

    def _seed_released_session(self, session_id, content):
        """Upsert a single session containing `content` and release it so the
        hosted upload gate (release_gate_blockers) lets the share through."""
        from clawjournal.workbench.index import open_index, set_hold_state, upsert_sessions
        conn = open_index()
        try:
            upsert_sessions(conn, [{
                "session_id": session_id,
                "project": "test-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "messages": [{"role": "user", "content": content, "tool_uses": []}],
                "stats": {
                    "user_messages": 1, "assistant_messages": 0,
                    "tool_uses": 0, "input_tokens": 100, "output_tokens": 0,
                },
            }])
            set_hold_state(conn, session_id, "released", changed_by="user", reason="test")
        finally:
            conn.close()

    @staticmethod
    def _manifest_from_upload(req):
        """Extract manifest.json from the multipart upload request body."""
        body = req.data
        start = body.index(b"PK\x03\x04")
        end = body.rfind(b"\r\n--")
        with zipfile.ZipFile(BytesIO(body[start:end])) as archive:
            return json.loads(archive.read("manifest.json").decode("utf-8"))

    def test_share_upload_runs_ai_pii_when_opted_in(self, server, monkeypatch):
        """ai_pii=true in the upload body must run the AI hybrid pass and mark
        the uploaded manifest ai_enabled=true (the highest-stakes share path)."""
        WorkbenchHandler._last_share_time = 0.0
        self._seed_released_session("upload-ai-on", "Alice Example should not ship")
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

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
            "session_ids": ["upload-ai-on"], "note": "Upload AI on test",
        })
        assert status == 201
        share_id = data["share_id"]

        captured = {}

        def assert_zip(req):
            captured["manifest"] = self._manifest_from_upload(req)

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(upload_assert=assert_zip),
        ):
            status, data = _post(
                server, f"/api/shares/{share_id}/upload",
                {**self._consent_body(), "ai_pii": True},
            )

        assert status == 200, data
        assert calls >= 1  # the AI hybrid pass actually ran
        summary = captured["manifest"]["redaction_summary"]
        assert summary["pii_review"]["ai_enabled"] is True
        assert summary["pii_review"]["finding_count"] == 1

    def test_share_upload_leaves_ai_pii_off_by_default(self, server, monkeypatch):
        """Omitting ai_pii must NOT run the AI hybrid pass; the uploaded manifest
        records ai_enabled=false (deterministic rules still run + gate)."""
        WorkbenchHandler._last_share_time = 0.0
        self._seed_released_session("upload-ai-off", "Alice Example stays for default")
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: _share_config())

        def fail_review(*_args, **_kwargs):
            raise AssertionError("AI PII review must be opt-in on the upload path")

        monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_hybrid", fail_review)

        status, data = _post(server, "/api/shares", {
            "session_ids": ["upload-ai-off"], "note": "Upload AI off test",
        })
        assert status == 201
        share_id = data["share_id"]

        captured = {}

        def assert_zip(req):
            captured["manifest"] = self._manifest_from_upload(req)

        with patch(
            "clawjournal.workbench.daemon.urllib.request.urlopen",
            side_effect=_mock_urlopen_factory(upload_assert=assert_zip),
        ):
            status, data = _post(
                server, f"/api/shares/{share_id}/upload", self._consent_body(),
            )

        assert status == 200, data
        assert captured["manifest"]["redaction_summary"]["pii_review"]["ai_enabled"] is False

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

    def test_download_bundle_applies_final_ai_pii_redaction_when_opted_in(self, server, monkeypatch):
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

        status, content_type, body = _get_raw(server, f"/api/shares/{share_id}/download?ai_pii=1")
        assert status == 200
        assert content_type == "application/zip"

        with zipfile.ZipFile(BytesIO(body)) as archive:
            sessions_content = archive.read("sessions.jsonl").decode("utf-8")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

        assert "Alice Example" not in sessions_content
        assert "[REDACTED_NAME]" in sessions_content
        assert manifest["redaction_summary"]["pii_review"]["ai_enabled"] is True
        assert manifest["redaction_summary"]["pii_review"]["finding_count"] == 1
        assert manifest["redaction_summary"]["coverage"] == {"full": 1, "rules_only": 0}

    def test_download_bundle_leaves_ai_pii_off_by_default(self, server, monkeypatch):
        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "download-ai-off",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Alice Example should remain without AI", "tool_uses": []},
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

        def fail_review(*_args, **_kwargs):
            raise AssertionError("AI PII review should be opt-in")

        monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_hybrid", fail_review)

        status, data = _post(server, "/api/shares", {
            "session_ids": ["download-ai-off"],
            "note": "Download AI off test",
        })
        assert status == 201
        share_id = data["share_id"]

        status, content_type, body = _get_raw(server, f"/api/shares/{share_id}/download")
        assert status == 200
        assert content_type == "application/zip"

        with zipfile.ZipFile(BytesIO(body)) as archive:
            sessions_content = archive.read("sessions.jsonl").decode("utf-8")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

        assert "Alice Example" in sessions_content
        assert manifest["redaction_summary"]["pii_review"]["ai_enabled"] is False
        assert manifest["redaction_summary"]["coverage"] == {"full": 0, "rules_only": 1}

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

        status, data = _post(server, f"/api/shares/{share_id}/seal", {"ai_pii": True})
        assert status == 200
        assert data["ok"] is True
        assert data["redaction_summary"]["pii_review"]["ai_enabled"] is True
        assert data["redaction_summary"]["pii_review"]["finding_count"] == 1
        assert calls == 1

        status, data = _post(server, f"/api/shares/{share_id}/seal", {"ai_pii": True})
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

    def test_seal_rebuilds_when_ai_pii_choice_changes(self, server, monkeypatch):
        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "seal-mode-change",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Alice Example mode switch", "tool_uses": []},
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
            "session_ids": ["seal-mode-change"],
            "note": "Seal mode change test",
        })
        assert status == 201
        share_id = data["share_id"]

        status, data = _post(server, f"/api/shares/{share_id}/seal", {"ai_pii": False})
        assert status == 200
        assert data["redaction_summary"]["pii_review"]["ai_enabled"] is False
        assert calls == 0

        status, data = _post(server, f"/api/shares/{share_id}/seal", {"ai_pii": True})
        assert status == 200
        assert data["redaction_summary"]["pii_review"]["ai_enabled"] is True
        assert data["redaction_summary"]["pii_review"]["finding_count"] == 1
        assert calls == 1

    def test_seal_rebuilds_when_redaction_settings_change(self, server, monkeypatch):
        """Editing the redaction settings (custom strings / allowlist / etc.)
        must invalidate the finalized-export cache so a later seal/submit/
        download never ships content redacted under the stale settings."""
        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "seal-settings-change",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Codename SHIPWRECK and Alice Example", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1, "assistant_messages": 0,
                "tool_uses": 0, "input_tokens": 100, "output_tokens": 0,
            },
        }])
        conn.close()

        calls = 0

        def fake_review(session, **kw):
            nonlocal calls
            calls += 1
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

        cfg: dict = {}
        monkeypatch.setattr("clawjournal.workbench.daemon.load_config", lambda: dict(cfg))

        status, data = _post(server, "/api/shares", {
            "session_ids": ["seal-settings-change"],
            "note": "settings change",
        })
        assert status == 201
        share_id = data["share_id"]

        # First seal: no custom redaction strings configured.
        status, data = _post(server, f"/api/shares/{share_id}/seal", {"ai_pii": True})
        assert status == 200
        assert calls == 1

        # Re-seal with identical settings reuses the cache (no rebuild).
        status, data = _post(server, f"/api/shares/{share_id}/seal", {"ai_pii": True})
        assert status == 200
        assert calls == 1

        # Prove the cached export is genuinely stale once the setting changes:
        # before adding the custom string, the downloaded zip still contains the
        # raw 'SHIPWRECK' (it isn't a redaction target yet), and the download
        # reuses the cache (no rebuild). This makes the rebuild assertion below
        # a real content change, not just a call-count artifact.
        status, _ct, before = _get_raw(server, f"/api/shares/{share_id}/download")
        assert status == 200
        with zipfile.ZipFile(BytesIO(before)) as archive:
            before_content = archive.read("sessions.jsonl").decode("utf-8")
        assert "SHIPWRECK" in before_content
        assert calls == 1

        # Add a custom redaction string → settings fingerprint changes.
        cfg["redact_strings"] = ["SHIPWRECK"]

        status, data = _post(server, f"/api/shares/{share_id}/seal", {"ai_pii": True})
        assert status == 200
        # Cache invalidated by the settings change → the export was rebuilt.
        assert calls == 2

        # The newly added custom string is now redacted in the downloaded zip.
        status, content_type, body = _get_raw(server, f"/api/shares/{share_id}/download")
        assert status == 200
        with zipfile.ZipFile(BytesIO(body)) as archive:
            sessions_content = archive.read("sessions.jsonl").decode("utf-8")
        assert "SHIPWRECK" not in sessions_content
        assert "[REDACTED_NAME]" in sessions_content
        # The download reused the freshly rebuilt artifact (no extra AI pass).
        assert calls == 2

    def test_seal_with_no_override_uses_config_default(self, server, monkeypatch):
        """When the request omits ai_pii, the persisted config default
        (ai_pii_review_enabled) decides whether the AI pass runs."""
        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "seal-config-default",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "Alice Example config default", "tool_uses": []},
            ],
            "stats": {
                "user_messages": 1, "assistant_messages": 0,
                "tool_uses": 0, "input_tokens": 100, "output_tokens": 0,
            },
        }])
        conn.close()

        def fake_review(session, **kw):
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
        # Persisted config opts AI review on; the seal request omits ai_pii.
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.load_config",
            lambda: {"ai_pii_review_enabled": True},
        )

        status, data = _post(server, "/api/shares", {
            "session_ids": ["seal-config-default"],
            "note": "Seal config default test",
        })
        assert status == 201
        share_id = data["share_id"]

        status, data = _post(server, f"/api/shares/{share_id}/seal")
        assert status == 200
        assert data["redaction_summary"]["pii_review"]["ai_enabled"] is True
        assert data["redaction_summary"]["pii_review"]["finding_count"] == 1

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
