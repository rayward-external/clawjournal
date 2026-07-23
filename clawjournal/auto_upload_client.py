"""Typed client for the hosted recurring-upload protocol (v2)."""

from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import mimetypes
import os
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urljoin, urlsplit

from . import __version__

# Protocol v2 (hosted logical_sessions_v1 rollout): enrollment sends explicit
# {source, project} scope entries plus an ownership certification the user
# explicitly accepted; the server computes and owns the scope hash.
RECURRING_UPLOAD_API_VERSION = 2
RECURRING_CLIENT_PROTOCOL_VERSION = "2"
RECURRING_CADENCE_DAYS = 1
MANUAL_SHARE_ENROLLMENT_GRANT_VERSION = 1
# Mirrors the hosted RECURRING_SCOPE_MAX_ENTRIES contract limit so oversized
# scopes fail fast locally instead of as a server-worded enrollment rejection.
MAX_SCOPE_ENTRIES = 200
MAX_RECURRING_SESSIONS = 5
ALLOW_INSECURE_LOOPBACK_ENV = "CLAWJOURNAL_ALLOW_INSECURE_LOOPBACK_RECURRING"


@dataclass(frozen=True)
class RecurringServiceError(RuntimeError):
    code: str
    message: str
    retryable: bool = False
    retry_after: int | None = None
    status: int | None = None
    ambiguous: bool = False

    def __str__(self) -> str:
        return self.message

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
            "status": self.status,
            "ambiguous": self.ambiguous,
        }


class CapabilityError(RecurringServiceError):
    pass


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _allow_insecure_loopback_http() -> bool:
    """Return whether this process explicitly enabled local HTTP testing."""

    return os.environ.get(ALLOW_INSECURE_LOOPBACK_ENV) == "1"


def _normalized_origin(
    value: str,
    *,
    allow_local_http: bool = False,
    require_exact_origin: bool = False,
) -> str:
    try:
        parsed = urlsplit(value)
        parsed_port = parsed.port
    except ValueError as exc:
        raise CapabilityError(
            "invalid_destination",
            "Recurring upload requires an exact HTTPS destination origin.",
        ) from exc
    hostname = (parsed.hostname or "").lower()
    scheme = parsed.scheme.lower()
    local = hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or (
            require_exact_origin
            and (
                parsed.path not in ("", "/")
                or bool(parsed.query)
                or bool(parsed.fragment)
            )
        )
        or (scheme != "https" and not (allow_local_http and local and scheme == "http"))
    ):
        raise CapabilityError(
            "invalid_destination",
            "Recurring upload requires an exact HTTPS destination origin.",
        )
    port = f":{parsed_port}" if parsed_port is not None else ""
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    return f"{scheme}://{rendered_host}{port}"


def comparable_origin(value: str) -> str:
    """Render an origin so two spellings of the same host compare equal.

    The enrollment-grant issuer is derived from the configured share URL, which
    preserves whatever casing and explicit port the operator wrote, while
    capability validation lowercases the host and re-renders the port. Comparing
    those two spellings directly makes a perfectly valid grant silently
    unusable, so both sides normalize through here first. A value this module
    cannot parse as an origin is returned trimmed rather than raising: the
    caller is comparing two strings for equality, not authorizing egress.
    """

    try:
        return _normalized_origin(
            value, allow_local_http=_allow_insecure_loopback_http()
        )
    except CapabilityError:
        return value.strip().rstrip("/")


def _endpoint(
    origin: str,
    value: Any,
    *,
    field: str,
    allow_local_http: bool = False,
) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CapabilityError("capability_incompatible", f"Missing hosted capability: {field}.")
    endpoint = urljoin(origin + "/", value.strip())
    if _normalized_origin(endpoint, allow_local_http=allow_local_http) != _normalized_origin(
        origin,
        allow_local_http=allow_local_http,
        require_exact_origin=True,
    ):
        raise CapabilityError(
            "cross_origin_endpoint",
            f"Hosted capability {field} points to a different origin.",
        )
    return endpoint


def validate_capabilities(
    capabilities: Mapping[str, Any],
    *,
    origin: str,
    client_version: str = RECURRING_CLIENT_PROTOCOL_VERSION,
    require_enrollment_open: bool = True,
) -> dict[str, Any]:
    """Validate every security capability before recurring egress is enabled."""

    allow_local_http = _allow_insecure_loopback_http()
    normalized_origin = _normalized_origin(
        origin,
        allow_local_http=allow_local_http,
        require_exact_origin=True,
    )
    required_equals = {
        "recurring_upload_api_version": RECURRING_UPLOAD_API_VERSION,
        "recurring_cadence_days": RECURRING_CADENCE_DAYS,
        "maximum_recurring_sessions": MAX_RECURRING_SESSIONS,
        "exact_artifact_idempotency": True,
        "duplicate_revision_enforcement": True,
    }
    for field, expected in required_equals.items():
        actual = capabilities.get(field)
        if actual != expected or (
            isinstance(expected, int)
            and not isinstance(expected, bool)
            and (not isinstance(actual, int) or isinstance(actual, bool))
        ):
            raise CapabilityError(
                "capability_incompatible",
                f"Hosted recurring-upload capability {field} is unavailable or incompatible.",
            )
    grant_version = capabilities.get("manual_share_enrollment_grant_version")
    if grant_version is not None and (
        not isinstance(grant_version, int)
        or isinstance(grant_version, bool)
        or grant_version != MANUAL_SHARE_ENROLLMENT_GRANT_VERSION
    ):
        raise CapabilityError(
            "capability_incompatible",
            "Hosted recurring-upload enrollment-grant capability is incompatible.",
        )
    if require_enrollment_open and capabilities.get("recurring_enrollment_open") is not True:
        raise CapabilityError(
            "enrollment_closed",
            "Hosted recurring enrollment is currently closed.",
            retryable=True,
        )
    max_size = capabilities.get("maximum_bundle_size")
    if not isinstance(max_size, int) or isinstance(max_size, bool) or max_size <= 0:
        raise CapabilityError(
            "capability_incompatible",
            "Hosted recurring-upload capability has no valid bundle-size limit.",
        )
    versions = capabilities.get("supported_recurring_client_versions")
    if not isinstance(versions, list) or not all(isinstance(item, str) for item in versions):
        raise CapabilityError(
            "capability_incompatible",
            "Hosted recurring-upload capability has no supported-client list.",
        )
    if "*" not in versions and client_version not in versions:
        raise CapabilityError(
            "client_version_unsupported",
            "This ClawJournal version is not supported for recurring upload.",
        )
    endpoint_fields = (
        "recurring_authorization_url",
        "recurring_enrollment_url",
        "recurring_submission_url",
        "recurring_receipt_lookup_url",
    )
    endpoints = {
        field: _endpoint(
            normalized_origin,
            capabilities.get(field),
            field=field,
            allow_local_http=allow_local_http,
        )
        for field in endpoint_fields
    }
    return {
        **dict(capabilities),
        **endpoints,
        "origin": normalized_origin,
        "maximum_bundle_size": max_size,
    }


def fetch_capabilities(
    *, force: bool = True, require_enrollment_open: bool = True
) -> dict[str, Any]:
    """Fetch and validate the canonical hosted capability document."""

    from .workbench.daemon import _fetch_hosted_share_capabilities, _hosted_api_base

    origin = _hosted_api_base()
    try:
        raw = _fetch_hosted_share_capabilities(force=force)
    except Exception as exc:
        raise RecurringServiceError(
            "server_unavailable",
            "Could not reach the hosted recurring-upload capability service.",
            retryable=True,
        ) from exc
    return validate_capabilities(
        raw,
        origin=origin,
        require_enrollment_open=require_enrollment_open,
    )


def recovery_capabilities(origin: str) -> dict[str, Any]:
    """Build the fixed recovery-only surface from an already pinned origin.

    Revocation and receipt reconciliation must remain possible when public
    recurring discovery is closed or deliberately dark.  These endpoints are
    part of protocol v1, and this helper grants no submission authority.
    """

    allow_local_http = _allow_insecure_loopback_http()
    normalized = _normalized_origin(
        origin,
        allow_local_http=allow_local_http,
        require_exact_origin=True,
    )
    return {
        "origin": normalized,
        "recurring_enrollment_url": _endpoint(
            normalized,
            "/api/recurring-enrollments",
            field="recurring_enrollment_url",
            allow_local_http=allow_local_http,
        ),
        "recurring_receipt_lookup_url": _endpoint(
            normalized,
            "/api/recurring-receipts/{client_submission_id}",
            field="recurring_receipt_lookup_url",
            allow_local_http=allow_local_http,
        ),
    }


def _parse_error(exc: urllib.error.HTTPError) -> RecurringServiceError:
    try:
        body = exc.read().decode("utf-8", errors="replace")
        parsed = json.loads(body) if body else {}
    except (OSError, http.client.HTTPException, json.JSONDecodeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    retry_after = parsed.get("retry_after")
    if retry_after is None:
        retry_after = exc.headers.get("Retry-After") if exc.headers else None
    try:
        retry_after_int = int(retry_after) if retry_after is not None else None
    except (TypeError, ValueError):
        retry_after_int = None
    code = str(parsed.get("code") or (
        "rate_limited" if exc.code == 429 else
        "payload_too_large" if exc.code == 413 else
        "credential_invalid" if exc.code in (401, 403) else
        "server_unavailable" if exc.code >= 500 else
        "request_rejected"
    ))
    retryable = bool(parsed.get("retryable", exc.code == 429 or exc.code >= 500))
    message = str(parsed.get("error") or parsed.get("message") or "Hosted recurring request failed.")
    return RecurringServiceError(
        code,
        message,
        retryable=retryable,
        retry_after=retry_after_int,
        status=exc.code,
    )


def _open_request(request: urllib.request.Request, *, timeout: int) -> bytes:
    mutation_may_have_committed = request.get_method().upper() in {"POST", "PATCH"}
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return response.read()
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise RecurringServiceError(
                "redirect_rejected",
                "Hosted recurring upload refused a redirect.",
                status=exc.code,
                ambiguous=mutation_may_have_committed,
            ) from exc
        parsed = _parse_error(exc)
        if mutation_may_have_committed and exc.code >= 500:
            parsed = RecurringServiceError(
                parsed.code,
                parsed.message,
                retryable=parsed.retryable,
                retry_after=parsed.retry_after,
                status=parsed.status,
                ambiguous=True,
            )
        raise parsed from exc
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        # http.client.HTTPException (BadStatusLine, IncompleteRead, …) is raised
        # by getresponse()/read() on a truncated or malformed reply and is NOT
        # an OSError, so it must be classified here too — otherwise disable()'s
        # revoke/lookup crashes unclassified and submit skips ambiguous marking.
        raise RecurringServiceError(
            "server_unavailable",
            "Could not reach the hosted recurring-upload service.",
            retryable=True,
            ambiguous=mutation_may_have_committed,
        ) from exc


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    bearer: str | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    headers = {"User-Agent": f"clawjournal/{__version__}"}
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    body = _open_request(request, timeout=timeout)
    if not body:
        return {}
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecurringServiceError(
            "malformed_response",
            "Hosted recurring-upload service returned malformed JSON.",
            retryable=True,
            ambiguous=method.upper() in {"POST", "PATCH"},
        ) from exc
    if not isinstance(parsed, dict):
        raise RecurringServiceError(
            "malformed_response",
            "Hosted recurring-upload service returned an invalid response.",
            retryable=True,
            ambiguous=method.upper() in {"POST", "PATCH"},
        )
    return parsed


def fetch_authorization(capabilities: Mapping[str, Any]) -> dict[str, Any]:
    return _request_json(str(capabilities["recurring_authorization_url"]))


def _scope_payload(scope_entries: Any) -> list[dict[str, str]]:
    """Encode (source, project) pairs as the protocol-v2 scope entry list."""

    payload: list[dict[str, str]] = []
    for entry in scope_entries:
        source, project = entry
        payload.append({"source": str(source), "project": str(project)})
    if not payload:
        raise CapabilityError(
            "scope_invalid", "A recurring enrollment requires at least one scope entry."
        )
    return payload


def create_enrollment(
    capabilities: Mapping[str, Any],
    *,
    client_enrollment_id: str,
    scope_entries: Any,
    authorization_version: str,
    retention_version: str,
    ownership_certification: bool,
    upload_token: str | None = None,
    enrollment_grant: str | None = None,
) -> dict[str, Any]:
    if ownership_certification is not True:
        raise CapabilityError(
            "ownership_certification_required",
            "Recurring enrollment requires the explicit ownership certification.",
        )
    normalized_upload_token = (upload_token or "").strip()
    normalized_enrollment_grant = (enrollment_grant or "").strip()
    if bool(normalized_upload_token) == bool(normalized_enrollment_grant):
        raise CapabilityError(
            "email_verification_required",
            "Recurring enrollment requires exactly one verified identity credential.",
        )
    identity_payload = (
        {"enrollment_grant": normalized_enrollment_grant}
        if normalized_enrollment_grant
        else {"upload_token": normalized_upload_token}
    )
    return _request_json(
        str(capabilities["recurring_enrollment_url"]),
        method="POST",
        payload={
            **identity_payload,
            "client_enrollment_id": client_enrollment_id,
            "scope": _scope_payload(scope_entries),
            "authorization_version": authorization_version,
            "retention_policy_version": retention_version,
            "client_version": RECURRING_CLIENT_PROTOCOL_VERSION,
            "accept_terms": True,
            "ownership_certification": True,
        },
    )


def _enrollment_endpoint(capabilities: Mapping[str, Any], enrollment_id: str) -> str:
    return str(capabilities["recurring_enrollment_url"]).rstrip("/") + "/" + quote(
        enrollment_id, safe=""
    )


def get_enrollment(
    capabilities: Mapping[str, Any], *, enrollment_id: str, active_token: str
) -> dict[str, Any]:
    return _request_json(
        _enrollment_endpoint(capabilities, enrollment_id), bearer=active_token
    )


def update_enrollment(
    capabilities: Mapping[str, Any],
    *,
    enrollment_id: str,
    active_token: str,
    scope_entries: Any,
    authorization_version: str,
    retention_version: str,
    ownership_certification: bool,
) -> dict[str, Any]:
    if ownership_certification is not True:
        raise CapabilityError(
            "ownership_certification_required",
            "Recurring reauthorization requires the explicit ownership certification.",
        )
    return _request_json(
        _enrollment_endpoint(capabilities, enrollment_id),
        method="PATCH",
        bearer=active_token,
        payload={
            "scope": _scope_payload(scope_entries),
            "authorization_version": authorization_version,
            "retention_policy_version": retention_version,
            "client_version": RECURRING_CLIENT_PROTOCOL_VERSION,
            "accept_terms": True,
            "ownership_certification": True,
        },
    )


def revoke_enrollment(
    capabilities: Mapping[str, Any], *, enrollment_id: str, recovery_token: str
) -> dict[str, Any]:
    return _request_json(
        _enrollment_endpoint(capabilities, enrollment_id),
        method="DELETE",
        bearer=recovery_token,
    )


def lookup_receipt(
    capabilities: Mapping[str, Any],
    *,
    client_submission_id: str,
    token: str,
) -> dict[str, Any]:
    template = str(capabilities["recurring_receipt_lookup_url"])
    encoded = quote(client_submission_id, safe="")
    endpoint = (
        template.replace("{client_submission_id}", encoded)
        if "{client_submission_id}" in template
        else template.rstrip("/") + "/" + encoded
    )
    return _request_json(endpoint, bearer=token)


def _multipart_body(
    *,
    fields: Mapping[str, str],
    file_field: str,
    file_path: Path,
    file_bytes: bytes | None = None,
) -> tuple[bytes, str]:
    boundary = f"clawjournal-{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for name, value in fields.items():
        chunks.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
                f"{value}\r\n"
            ).encode("utf-8")
        )
    filename = file_path.name
    content_type = mimetypes.guess_type(filename)[0] or "application/zip"
    chunks.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{file_field}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode("utf-8")
        + (file_path.read_bytes() if file_bytes is None else file_bytes)
        + b"\r\n"
    )
    chunks.append(f"--{boundary}--\r\n".encode("ascii"))
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def submit_artifact(
    capabilities: Mapping[str, Any],
    *,
    active_token: str,
    artifact_path: Path,
    client_submission_id: str,
    authorization_revision: int,
    trace_revision_keys: list[str],
    artifact_sha256: str,
) -> dict[str, Any]:
    try:
        artifact_bytes = artifact_path.read_bytes()
        actual_sha256 = hashlib.sha256(artifact_bytes).hexdigest()
    except OSError as exc:
        raise RecurringServiceError(
            "sealed_artifact_missing",
            "The exact sealed recurring ZIP is unavailable.",
        ) from exc
    if not hmac.compare_digest(actual_sha256, artifact_sha256):
        raise RecurringServiceError(
            "sealed_artifact_changed",
            "The exact sealed recurring ZIP no longer matches its ledger hash.",
        )
    body, content_type = _multipart_body(
        fields={
            "client_submission_id": client_submission_id,
            "authorization_revision": str(authorization_revision),
            "trace_revision_keys": json.dumps(trace_revision_keys, separators=(",", ":")),
        },
        file_field="bundle",
        file_path=artifact_path,
        file_bytes=artifact_bytes,
    )
    request = urllib.request.Request(
        str(capabilities["recurring_submission_url"]),
        data=body,
        headers={
            "Authorization": f"Bearer {active_token}",
            "Content-Type": content_type,
            "User-Agent": f"clawjournal/{__version__}",
        },
        method="POST",
    )
    try:
        raw = _open_request(request, timeout=120)
    except RecurringServiceError as exc:
        # The multipart body is fully sent before any response is read, so a
        # transport failure (unreachable) or an unexpected redirect answered
        # after the body crossed egress leaves the submission unconfirmed. Both
        # must be ambiguous so the runner reconciles receipts before retrying,
        # never terminally 'rejected' with a bundle possibly committed server-side.
        if exc.code in ("server_unavailable", "redirect_rejected"):
            raise RecurringServiceError(
                exc.code,
                exc.message,
                retryable=True,
                retry_after=exc.retry_after,
                status=exc.status,
                ambiguous=True,
            ) from exc
        raise
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecurringServiceError(
            "malformed_response",
            "Hosted service may have received the bundle but returned malformed JSON.",
            retryable=True,
            ambiguous=True,
        ) from exc
    if not isinstance(parsed, dict) or not isinstance(parsed.get("receipt_id"), str):
        raise RecurringServiceError(
            "malformed_response",
            "Hosted service may have received the bundle but returned no receipt.",
            retryable=True,
            ambiguous=True,
        )
    return parsed
