from __future__ import annotations

import json
import urllib.error

import pytest

from clawjournal import auto_upload_client as client


def _caps(**overrides):
    caps = {
        "recurring_upload_api_version": 2,
        "manual_share_enrollment_grant_version": 1,
        "recurring_cadence_days": 1,
        "recurring_enrollment_open": True,
        "maximum_recurring_sessions": 5,
        "maximum_bundle_size": 1_000_000,
        "exact_artifact_idempotency": True,
        "duplicate_revision_enforcement": True,
        "supported_recurring_client_versions": ["2"],
        "recurring_authorization_url": "/api/recurring-authorization",
        "recurring_enrollment_url": "/api/recurring-enrollments",
        "recurring_submission_url": "/api/recurring-submissions",
        "recurring_receipt_lookup_url": (
            "/api/recurring-receipts/{client_submission_id}"
        ),
    }
    caps.update(overrides)
    return caps


def test_capabilities_require_every_safety_contract():
    validated = client.validate_capabilities(
        _caps(), origin="https://data.rayward.ai"
    )
    assert validated["recurring_submission_url"] == (
        "https://data.rayward.ai/api/recurring-submissions"
    )

    with pytest.raises(client.CapabilityError, match="duplicate_revision_enforcement"):
        client.validate_capabilities(
            _caps(duplicate_revision_enforcement=False),
            origin="https://data.rayward.ai",
        )

    with pytest.raises(client.CapabilityError, match="recurring_cadence_days"):
        client.validate_capabilities(
            _caps(recurring_cadence_days=7),
            origin="https://data.rayward.ai",
        )

    with pytest.raises(client.CapabilityError, match="recurring_cadence_days"):
        client.validate_capabilities(
            _caps(recurring_cadence_days=True),
            origin="https://data.rayward.ai",
        )


def test_capabilities_reject_cross_origin_endpoint():
    with pytest.raises(client.CapabilityError, match="different origin"):
        client.validate_capabilities(
            _caps(recurring_submission_url="https://evil.example/upload"),
            origin="https://data.rayward.ai",
        )


def test_capabilities_reject_malformed_origin_port_with_typed_error():
    with pytest.raises(client.CapabilityError) as exc_info:
        client.validate_capabilities(
            _caps(),
            origin="https://data.rayward.ai:not-a-port",
        )

    assert exc_info.value.code == "invalid_destination"


@pytest.mark.parametrize(
    "origin",
    (
        "http://localhost:18000",
        "http://127.0.0.1:18000",
        "http://[::1]:18000",
    ),
)
def test_explicit_local_mode_allows_exact_loopback_http(monkeypatch, origin):
    monkeypatch.setenv(client.ALLOW_INSECURE_LOOPBACK_ENV, "1")

    validated = client.validate_capabilities(_caps(), origin=origin)
    recovery = client.recovery_capabilities(origin)

    assert validated["origin"] == origin
    assert validated["recurring_submission_url"] == (
        f"{origin}/api/recurring-submissions"
    )
    assert recovery["recurring_enrollment_url"] == (
        f"{origin}/api/recurring-enrollments"
    )


@pytest.mark.parametrize(
    "origin",
    (
        "http://localhost:18000",
        "http://127.0.0.1:18000",
        "http://[::1]:18000",
    ),
)
def test_loopback_http_requires_explicit_local_mode(origin):
    with pytest.raises(client.CapabilityError) as exc_info:
        client.validate_capabilities(_caps(), origin=origin)

    assert exc_info.value.code == "invalid_destination"


@pytest.mark.parametrize(
    "origin",
    (
        "http://data.rayward.ai",
        "http://192.168.1.10:18000",
        "http://127.0.0.1.evil.example:18000",
        "http://user@127.0.0.1:18000",
        "http://127.0.0.1:18000/path",
        "http://127.0.0.1:18000?query=yes",
        "http://127.0.0.1:18000#fragment",
    ),
)
def test_explicit_local_mode_still_rejects_non_exact_or_non_loopback_http(
    monkeypatch, origin
):
    monkeypatch.setenv(client.ALLOW_INSECURE_LOOPBACK_ENV, "1")

    with pytest.raises(client.CapabilityError) as exc_info:
        client.validate_capabilities(_caps(), origin=origin)

    assert exc_info.value.code == "invalid_destination"


def test_explicit_local_mode_preserves_exact_origin_matching(monkeypatch):
    monkeypatch.setenv(client.ALLOW_INSECURE_LOOPBACK_ENV, "1")

    with pytest.raises(client.CapabilityError, match="different origin"):
        client.validate_capabilities(
            _caps(recurring_submission_url="http://localhost:18001/upload"),
            origin="http://localhost:18000",
        )
    with pytest.raises(client.CapabilityError, match="different origin"):
        client.validate_capabilities(
            _caps(recurring_submission_url="http://127.0.0.1:18000/upload"),
            origin="http://localhost:18000",
        )


def test_closed_discovery_blocks_new_enrollment_but_not_existing_protocol():
    closed = _caps(recurring_enrollment_open=False)
    with pytest.raises(client.CapabilityError) as exc_info:
        client.validate_capabilities(closed, origin="https://data.rayward.ai")
    assert exc_info.value.code == "enrollment_closed"
    assert exc_info.value.retryable is True

    validated = client.validate_capabilities(
        closed,
        origin="https://data.rayward.ai",
        require_enrollment_open=False,
    )
    assert validated["recurring_enrollment_open"] is False


def test_recovery_surface_is_derived_only_from_pinned_origin():
    recovery = client.recovery_capabilities("https://data.rayward.ai")
    assert recovery["recurring_enrollment_url"] == (
        "https://data.rayward.ai/api/recurring-enrollments"
    )
    assert recovery["recurring_receipt_lookup_url"].endswith(
        "/api/recurring-receipts/{client_submission_id}"
    )
    with pytest.raises(client.CapabilityError):
        client.recovery_capabilities("https://user@data.rayward.ai")


def test_json_request_sends_bearer_without_following_redirect(monkeypatch):
    seen = {}

    def fake_open(request, *, timeout):
        seen["url"] = request.full_url
        seen["authorization"] = request.headers.get("Authorization")
        seen["body"] = json.loads(request.data)
        return b'{"enrollment_id":"enrollment-1"}'

    monkeypatch.setattr(client, "_open_request", fake_open)
    result = client.create_enrollment(
        client.validate_capabilities(_caps(), origin="https://data.rayward.ai"),
        upload_token="one-shot",
        client_enrollment_id="intent-1",
        scope_entries=[("claude", "clawjournal")],
        authorization_version="auth-v1",
        retention_version="ret-v1",
        ownership_certification=True,
    )

    assert result["enrollment_id"] == "enrollment-1"
    assert seen["authorization"] is None
    assert seen["body"]["upload_token"] == "one-shot"
    assert seen["body"]["client_enrollment_id"] == "intent-1"
    assert seen["body"]["scope"] == [
        {"source": "claude", "project": "clawjournal"}
    ]
    assert seen["body"]["ownership_certification"] is True
    assert seen["body"]["client_version"] == "2"
    assert "scope_hash" not in seen["body"]


def test_create_enrollment_accepts_manual_share_grant_without_upload_token(
    monkeypatch,
):
    seen = {}

    def fake_open(request, *, timeout):
        seen["body"] = json.loads(request.data)
        return b'{"enrollment_id":"enrollment-1"}'

    monkeypatch.setattr(client, "_open_request", fake_open)
    result = client.create_enrollment(
        client.validate_capabilities(_caps(), origin="https://data.rayward.ai"),
        enrollment_grant="cj_enroll_one-shot",
        client_enrollment_id="intent-1",
        scope_entries=[("codex", "clawjournal")],
        authorization_version="auth-v1",
        retention_version="ret-v1",
        ownership_certification=True,
    )

    assert result["enrollment_id"] == "enrollment-1"
    assert seen["body"]["enrollment_grant"] == "cj_enroll_one-shot"
    assert "upload_token" not in seen["body"]


def test_create_enrollment_requires_exactly_one_identity_credential():
    capabilities = client.validate_capabilities(
        _caps(), origin="https://data.rayward.ai"
    )
    common = {
        "client_enrollment_id": "intent-1",
        "scope_entries": [("codex", "clawjournal")],
        "authorization_version": "auth-v1",
        "retention_version": "ret-v1",
        "ownership_certification": True,
    }
    with pytest.raises(client.CapabilityError) as missing:
        client.create_enrollment(capabilities, **common)
    assert missing.value.code == "email_verification_required"

    with pytest.raises(client.CapabilityError) as duplicate:
        client.create_enrollment(
            capabilities,
            upload_token="upload",
            enrollment_grant="grant",
            **common,
        )
    assert duplicate.value.code == "email_verification_required"


def test_typed_http_error_preserves_code_and_retryability():
    exc = urllib.error.HTTPError(
        "https://data.rayward.ai/api/recurring-submissions",
        429,
        "too many",
        {"Retry-After": "9"},
        None,
    )
    exc.fp = type("Body", (), {"read": lambda self: b'{"error":"later","code":"rate_limited","retryable":true}'})()

    parsed = client._parse_error(exc)

    assert parsed.code == "rate_limited"
    assert parsed.retryable is True
    assert parsed.retry_after == 9



import hashlib
import http.client
import urllib.request


def test_open_request_classifies_http_exception_as_server_unavailable(monkeypatch):
    def boom(*_args, **_kwargs):
        raise http.client.BadStatusLine("garbage status line")

    monkeypatch.setattr(client._OPENER, "open", boom)
    request = urllib.request.Request("https://data.rayward.ai/api/x")
    with pytest.raises(client.RecurringServiceError) as exc:
        client._open_request(request, timeout=5)
    assert exc.value.code == "server_unavailable"
    assert exc.value.retryable is True


@pytest.mark.parametrize("method", ["POST", "PATCH"])
def test_open_request_marks_lost_mutation_response_ambiguous(monkeypatch, method):
    def boom(*_args, **_kwargs):
        raise http.client.IncompleteRead(b"partial")

    monkeypatch.setattr(client._OPENER, "open", boom)
    request = urllib.request.Request(
        "https://data.rayward.ai/api/recurring-enrollments",
        data=b"{}",
        method=method,
    )

    with pytest.raises(client.RecurringServiceError) as exc:
        client._open_request(request, timeout=5)

    assert exc.value.code == "server_unavailable"
    assert exc.value.retryable is True
    assert exc.value.ambiguous is True


def test_mutating_malformed_json_response_is_ambiguous(monkeypatch):
    monkeypatch.setattr(client, "_open_request", lambda *_a, **_k: b"not-json")

    with pytest.raises(client.RecurringServiceError) as exc:
        client._request_json(
            "https://data.rayward.ai/api/recurring-enrollments",
            method="POST",
            payload={"request": "body"},
        )

    assert exc.value.code == "malformed_response"
    assert exc.value.ambiguous is True


def test_submit_redirect_after_body_is_ambiguous(tmp_path, monkeypatch):
    artifact = tmp_path / "bundle.zip"
    artifact.write_bytes(b"sealed-bytes")
    digest = hashlib.sha256(b"sealed-bytes").hexdigest()

    def redirect(*_args, **_kwargs):
        raise client.RecurringServiceError(
            "redirect_rejected", "unexpected redirect", status=307
        )

    monkeypatch.setattr(client, "_open_request", redirect)
    with pytest.raises(client.RecurringServiceError) as exc:
        client.submit_artifact(
            {"recurring_submission_url": "https://data.rayward.ai/api/submissions"},
            active_token="tok",
            artifact_path=artifact,
            client_submission_id="cs-1",
            authorization_revision=1,
            trace_revision_keys=["k1"],
            artifact_sha256=digest,
        )
    # The body already crossed egress, so a redirect must reconcile receipts,
    # not terminally reject a bundle the server may have committed.
    assert exc.value.code == "redirect_rejected"
    assert exc.value.ambiguous is True
    assert exc.value.retryable is True
