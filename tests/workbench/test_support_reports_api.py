from __future__ import annotations

import json
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from threading import Thread

import pytest

from clawjournal import config, support_reports
from clawjournal.paths import ensure_api_token
from clawjournal.workbench import daemon, index


@pytest.fixture
def capability():
    return support_reports.SupportCapability(
        origin="https://support.example.test",
        reports_url="https://support.example.test/api/support/v1/reports",
        report_lookup_url=(
            "https://support.example.test/api/support/v1/reports/{client_report_id}"
        ),
        terms_url="https://support.example.test/api/support/v1/terms",
        max_report_bytes=32 * 1024,
        purpose="Troubleshoot and improve ClawJournal.",
        terms_version="support-v1",
        retention_policy_version="support-retention-v1",
        terms_text="Private support terms.",
        retention_text="Stored for 30 days.",
    )


@pytest.fixture
def support_api(tmp_path, monkeypatch, capability):
    monkeypatch.setattr(index, "INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "clawjournal-home")
    token = ensure_api_token(tmp_path)
    monkeypatch.setattr(
        daemon,
        "synchronize_index_health",
        lambda: {"status": "recovery_required"},
    )
    monkeypatch.setattr(
        daemon,
        "_resolve_support_report_capability",
        lambda **_kwargs: capability,
    )
    delivery_calls = []
    monkeypatch.setattr(
        daemon,
        "start_support_report_delivery",
        lambda identifier: delivery_calls.append(identifier),
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), daemon.WorkbenchHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1], token, delivery_calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _request(port, token, method, path, body=None, *, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    request_headers = dict(headers or {})
    if token is not None:
        request_headers["Authorization"] = f"Bearer {token}"
    raw = None
    if body is not None:
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    connection.request(method, path, body=raw, headers=request_headers)
    response = connection.getresponse()
    response_raw = response.read()
    response_headers = {key.lower(): value for key, value in response.getheaders()}
    parsed = (
        json.loads(response_raw)
        if response.headers.get("Content-Type", "").startswith("application/json")
        else response_raw.decode()
    )
    connection.close()
    return response.status, response_headers, parsed


def test_support_capability_is_authenticated_no_store_and_before_health_gate(
    support_api,
):
    port, token, _calls = support_api
    status, headers, body = _request(
        port, token, "GET", "/api/support-reports/capability"
    )
    assert status == 200
    assert headers["cache-control"] == "no-store"
    assert body == {
        "available": True,
        "purpose": "Troubleshoot and improve ClawJournal.",
        "terms_version": "support-v1",
        "retention_policy_version": "support-retention-v1",
        "terms_text": "Private support terms.",
        "retention_text": "Stored for 30 days.",
        "max_report_bytes": 32768,
        "message": None,
    }

    unauthenticated, _headers, raw = _request(
        port, None, "GET", "/api/support-reports/capability"
    )
    assert unauthenticated == 401
    assert raw == ""


def test_post_queues_only_exact_markdown_and_returns_no_secret(
    support_api,
):
    port, token, delivery_calls = support_api
    markdown = "# User-edited report\n\n私有正文 🐾"
    status, headers, body = _request(
        port,
        token,
        "POST",
        "/api/support-reports",
        {
            "report_markdown": markdown,
            "accepted_terms_version": "support-v1",
            "accepted_retention_policy_version": "support-retention-v1",
        },
    )
    assert status == 202
    assert headers["cache-control"] == "no-store"
    assert body["state"] == "queued"
    assert body["receipt_id"] is None
    assert "manage_secret" not in body
    assert "report_markdown" not in body
    assert delivery_calls == [body["client_report_id"]]
    stored = support_reports.load_report(body["client_report_id"])
    assert stored["report_markdown"] == markdown

    list_status, list_headers, listing = _request(
        port, token, "GET", "/api/support-reports"
    )
    assert list_status == 200
    assert list_headers["cache-control"] == "no-store"
    assert listing == {"reports": [body], "truncated": False}
    serialized = json.dumps(listing)
    assert markdown not in serialized
    assert stored["manage_secret"] not in serialized
    assert stored["reports_url"] not in serialized


def test_post_uses_strict_schema_and_bounded_reader(support_api):
    port, token, _calls = support_api
    status, headers, body = _request(
        port,
        token,
        "POST",
        "/api/support-reports",
        {
            "report_markdown": "text",
            "accepted_terms_version": "support-v1",
            "accepted_retention_policy_version": "support-retention-v1",
            "unexpected": "must fail closed",
        },
    )
    assert status == 400
    assert headers["cache-control"] == "no-store"
    assert body["code"] == "invalid_request"

    connection = HTTPConnection("127.0.0.1", port, timeout=5)
    connection.putrequest("POST", "/api/support-reports")
    connection.putheader("Authorization", f"Bearer {token}")
    connection.putheader("Content-Type", "application/json")
    connection.putheader(
        "Content-Length", str(support_reports.SUPPORT_LOCAL_REQUEST_MAX_BYTES + 1)
    )
    connection.endheaders()
    response = connection.getresponse()
    oversized = json.loads(response.read())
    assert response.status == 413
    assert response.getheader("Cache-Control") == "no-store"
    assert oversized["code"] == "report_too_large"
    connection.close()


def test_get_and_delete_receipt_routes_are_no_store_and_index_independent(
    support_api, monkeypatch
):
    port, token, _calls = support_api
    identifier = "870cce0b-2d9a-4b43-8755-6035f1bea6f9"
    public = {
        "client_report_id": identifier,
        "state": "accepted",
        "receipt_id": "receipt-1",
        "message": "received",
        "created_at": "2026-08-16T00:00:00Z",
        "expires_at": "2026-09-15T00:00:00Z",
    }
    monkeypatch.setattr(
        daemon,
        "reconcile_support_report",
        lambda candidate: {**public, "client_report_id": candidate},
    )
    monkeypatch.setattr(
        daemon,
        "support_report_public_status",
        lambda record: record,
    )
    monkeypatch.setattr(
        daemon,
        "delete_support_report",
        lambda candidate: {"client_report_id": candidate, "state": "deleted"},
    )

    get_status, get_headers, get_body = _request(
        port, token, "GET", f"/api/support-reports/{identifier}"
    )
    assert get_status == 200
    assert get_headers["cache-control"] == "no-store"
    assert get_body["receipt_id"] == "receipt-1"

    delete_status, delete_headers, delete_body = _request(
        port, token, "DELETE", f"/api/support-reports/{identifier}"
    )
    assert delete_status == 200
    assert delete_headers["cache-control"] == "no-store"
    assert delete_body == {"client_report_id": identifier, "state": "deleted"}
