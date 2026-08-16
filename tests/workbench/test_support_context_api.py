from __future__ import annotations

import json
import sqlite3
import subprocess
import urllib.request
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Event, Thread
from unittest.mock import MagicMock

import pytest

from clawjournal import filesystem, support_diagnostics
from clawjournal.paths import ensure_api_token
from clawjournal.workbench import daemon, index, index_recovery


@pytest.fixture
def support_server(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "INDEX_DB", tmp_path / "index.db")
    token = ensure_api_token(tmp_path)
    environment = daemon.capture_support_environment(
        revision="b" * 40,
        expected_user_version=index.WORKBENCH_SCHEMA_VERSION,
        sqlite_version=sqlite3.sqlite_version,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.WorkbenchHandler)
    server._support_environment = environment
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], token
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _get(port: int, token: str | None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    connection.request("GET", "/api/support-context", headers=headers)
    response = connection.getresponse()
    raw = response.read()
    content_type = response.getheader("Content-Type", "")
    body = json.loads(raw) if content_type.startswith("application/json") else raw.decode()
    result = (
        response.status,
        {key.lower(): value for key, value in response.getheaders()},
        body,
    )
    connection.close()
    return result


def test_support_context_requires_bearer_auth(support_server):
    port, token = support_server

    status, _headers, body = _get(port, None)
    assert status == 401
    assert body == ""

    status, _headers, body = _get(port, f"wrong-{token}")
    assert status == 401
    assert body == ""


def test_support_context_skips_diagnostic_io_before_the_health_gate(
    support_server,
    monkeypatch,
):
    port, token = support_server
    canary = "private /home/alice/session-123?token=secret"

    def forbidden(*_args, **_kwargs):
        raise AssertionError("support context attempted forbidden I/O")

    monkeypatch.setattr(
        daemon,
        "try_current_index_health",
        lambda: {
            "status": "recovery_required",
            "filesystem_type": "ext4",
            "storage_risk": "local",
            "storage_migration_required": False,
            "message": canary,
            "detail": canary,
            "database_path": canary,
            "backup_path": canary,
            "session_id": canary,
        },
    )
    monkeypatch.setattr(daemon, "synchronize_index_health", forbidden)
    monkeypatch.setattr(daemon, "open_index", forbidden)
    monkeypatch.setattr(support_diagnostics, "collect_index_diagnostics", forbidden)
    monkeypatch.setattr(filesystem, "classify_filesystem", forbidden)
    monkeypatch.setattr(filesystem, "sanitized_filesystem_type", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)

    status, headers, body = _get(port, token)

    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert body["index"] == {
        "status": "recovery_required",
        "condition": "recovery_required",
    }
    assert body["storage"] == {
        "filesystem_type": "ext4",
        "storage_risk": "local",
        "storage_migration_required": False,
    }
    assert body["collection"]["status"] == "complete"
    serialized = json.dumps(body)
    assert canary not in serialized
    assert "database_path" not in serialized
    assert "session_id" not in serialized


def test_support_context_collection_failure_returns_safe_partial(
    support_server,
    monkeypatch,
):
    port, token = support_server
    canary = "collector failed at /home/alice/private"

    def fail(*_args, **_kwargs):
        raise RuntimeError(canary)

    monkeypatch.setattr(daemon, "collect_support_context", fail)

    status, headers, body = _get(port, token)

    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert body == daemon.unavailable_support_context()
    assert body["collection"]["status"] == "partial"
    assert canary not in json.dumps(body)


@pytest.mark.parametrize(
    ("health_status", "expected_condition"),
    [
        ("checking", None),
        ("rebuilding", None),
        ("unavailable", "unavailable"),
    ],
)
def test_support_context_projects_each_cached_nonready_health_state(
    support_server,
    monkeypatch,
    health_status,
    expected_condition,
):
    port, token = support_server
    monkeypatch.setattr(
        daemon,
        "try_current_index_health",
        lambda: {
            "status": health_status,
            "filesystem_type": "unknown",
            "storage_risk": "unknown",
            "storage_migration_required": False,
        },
    )

    status, _headers, body = _get(port, token)

    assert status == 200
    assert body["index"] == {
        "status": health_status,
        "condition": expected_condition,
    }


def test_support_context_does_not_wait_for_busy_recovery_health_lock(
    support_server,
):
    port, token = support_server
    finished = Event()
    results = []

    def request_context():
        try:
            results.append(_get(port, token))
        finally:
            finished.set()

    index_recovery._STATE_LOCK.acquire()
    request_thread = Thread(target=request_context, daemon=True)
    try:
        request_thread.start()
        completed_without_lock = finished.wait(1.0)
    finally:
        index_recovery._STATE_LOCK.release()
    request_thread.join(timeout=2)

    assert completed_without_lock is True
    assert len(results) == 1
    status, headers, body = results[0]
    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert body["index"] == {"status": "unknown", "condition": None}
    assert body["collection"]["status"] == "partial"
    assert body["collection"]["unavailable_sections"] == [
        "cached_index_health"
    ]


def test_run_server_shares_one_startup_environment_with_ipv4_and_ipv6(
    monkeypatch,
):
    from clawjournal import selfupdate

    environment = daemon.capture_support_environment(
        revision="c" * 40,
        expected_user_version=index.WORKBENCH_SCHEMA_VERSION,
        sqlite_version=sqlite3.sqlite_version,
    )
    primary = MagicMock()
    primary.server_address = ("127.0.0.1", 48123)
    primary.serve_forever.side_effect = KeyboardInterrupt
    scanner = MagicMock()
    scanner._stop_event = Event()
    captured = {}

    def capture_ipv6(port, candidate_scanner, snapshot, candidate_environment):
        captured.update({
            "port": port,
            "scanner": candidate_scanner,
            "snapshot": snapshot,
            "environment": candidate_environment,
        })
        return None

    capture_calls = []

    def capture_environment(**kwargs):
        capture_calls.append(kwargs)
        return environment

    monkeypatch.setattr(daemon, "Scanner", lambda **_kwargs: scanner)
    monkeypatch.setattr(daemon, "ThreadingHTTPServer", lambda *_args: primary)
    monkeypatch.setattr(daemon, "capture_support_environment", capture_environment)
    monkeypatch.setattr(daemon, "_try_serve_ipv6_loopback", capture_ipv6)
    monkeypatch.setattr(daemon, "begin_index_health_check", lambda: None)
    monkeypatch.setattr(
        daemon,
        "initialize_index_health",
        lambda: {"status": "unavailable", "message": "test"},
    )
    monkeypatch.setattr(daemon, "_warn_if_frontend_stale", lambda **_kwargs: None)
    monkeypatch.setattr(selfupdate, "_package_repo_root", lambda: None)

    daemon.run_server(
        port=0,
        open_browser=False,
        startup_head="c" * 40,
        frontend_snapshot=None,
    )

    assert primary._support_environment is environment
    assert captured["environment"] is environment
    assert captured["scanner"] is scanner
    assert capture_calls == [{
        "revision": "c" * 40,
        "expected_user_version": index.WORKBENCH_SCHEMA_VERSION,
        "sqlite_version": sqlite3.sqlite_version,
    }]
