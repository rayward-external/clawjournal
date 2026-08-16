"""Private, DB-free delivery of user-reviewed support reports.

Only the exact Markdown supplied by the workbench is sent.  A small durable
outbox makes an ambiguous POST recoverable without coupling bug reporting to
the workbench SQLite index (which may be the component that is broken).
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
import os
import random
import re
import secrets
import stat
import struct
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urljoin, urlsplit

from . import __version__
from . import config as config_module
from .auto_upload_credentials import (
    CredentialStoreError,
    _ensure_private_directory,
    _require_private_mode,
)


SUPPORT_REPORT_API_VERSION = 1
SUPPORT_REPORT_SCHEMA_VERSION = 1
SUPPORT_SCREENSHOT_API_VERSION = 1
SUPPORT_OUTBOX_SCHEMA_VERSION = 2
SUPPORT_REPORT_LOCAL_MAX_BYTES = 64 * 1024
SUPPORT_SCREENSHOT_LOCAL_MAX_BYTES = 2 * 1024 * 1024
SUPPORT_SCREENSHOT_LOCAL_MAX_DIMENSION = 4096
SUPPORT_SCREENSHOT_LOCAL_MAX_PIXELS = 4 * 1024 * 1024
SUPPORT_SCREENSHOT_BASE64_MAX_BYTES = (
    4 * ((SUPPORT_SCREENSHOT_LOCAL_MAX_BYTES + 2) // 3)
)
# JSON escaping can expand otherwise-valid text (for example, control
# characters become ``\u00xx``). Keep the transport bound finite while still
# allowing every report that passes the exact UTF-8 report-byte limit below.
SUPPORT_LOCAL_REQUEST_MAX_BYTES = (
    4 * SUPPORT_REPORT_LOCAL_MAX_BYTES + SUPPORT_SCREENSHOT_BASE64_MAX_BYTES + 16 * 1024
)
SUPPORT_REMOTE_RESPONSE_MAX_BYTES = 64 * 1024
SUPPORT_REPORTS_DIRNAME = "support-reports"
SUPPORT_OUTBOX_RECORD_MAX_BYTES = 4 * SUPPORT_REPORT_LOCAL_MAX_BYTES + 32 * 1024
SUPPORT_REPORT_LIST_LIMIT = 100
SUPPORT_REPORT_LIST_SCAN_LIMIT = 256
SUPPORT_PENDING_RETENTION_DAYS = 30
SUPPORT_RETRY_BASE_SECONDS = 5.0
SUPPORT_RETRY_MAX_SECONDS = 60.0 * 60.0
SUPPORT_RECOVERY_POLL_SECONDS = 5.0
SUPPORT_OUTBOX_LOCK_FILENAME = "support-reports.lock"
SUPPORT_ORPHAN_GRACE_SECONDS = 60 * 60
SUPPORT_ORPHAN_SCAN_LIMIT = 256

_PENDING_STATES = frozenset({"queued", "submitting", "ambiguous"})
_TERMINAL_STATES = frozenset({"accepted", "rejected"})
_ALL_STATES = _PENDING_STATES | _TERMINAL_STATES
_MANAGE_SECRET_RE = re.compile(r"[A-Za-z0-9_-]{43,128}\Z")
_RECEIPT_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_RECORD_FIELDS = frozenset({
    "accepted_retention_policy_version",
    "accepted_terms_version",
    "api_origin",
    "attempt_count",
    "client_report_id",
    "content_sha256",
    "created_at",
    "expires_at",
    "manage_secret",
    "message_code",
    "next_attempt_at",
    "outbox_schema_version",
    "plaintext_expired",
    "receipt_id",
    "report_lookup_url",
    "report_markdown",
    "reports_url",
    "screenshot",
    "state",
    "updated_at",
})
_V1_RECORD_FIELDS = _RECORD_FIELDS - {"screenshot"}
_SCREENSHOT_STATES = frozenset({"queued", "submitting", "ambiguous", "accepted", "rejected"})
_SCREENSHOT_RECORD_FIELDS = frozenset({
    "attempt_count",
    "input_bytes",
    "local_expires_at",
    "message_code",
    "next_attempt_at",
    "png_filename",
    "receipt",
    "source_sha256",
    "state",
    "upload_url",
    "width",
    "height",
})
_SCREENSHOT_RECEIPT_FIELDS = frozenset({
    "created_at",
    "height",
    "sanitized_bytes",
    "sanitized_sha256",
    "sanitizer_version",
    "source_sha256",
    "width",
})
_REMOTE_ERROR_CODES = frozenset({
    "content_hash_mismatch",
    "credential_invalid",
    "idempotency_conflict",
    "invalid_request",
    "payload_too_large",
    "rate_limited",
    "report_not_found",
    "request_rejected",
    "support_reports_closed",
    "support_screenshots_closed",
    "screenshot_not_authorized",
    "screenshot_hash_mismatch",
    "screenshot_already_attached",
    "invalid_screenshot",
    "screenshot_too_large",
    "screenshot_processing_failed",
    "support_service_unavailable",
})
_STORED_MESSAGE_CODES = _REMOTE_ERROR_CODES | frozenset({
    "invalid_service_response",
    "local_pending_expired",
    "local_screenshot_expired",
    "redirect_rejected",
})
_REPORT_LOCKS_GUARD = threading.Lock()
_REPORT_LOCKS: dict[str, threading.Lock] = {}

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


class SupportReportError(RuntimeError):
    """A bounded, user-safe local support-report failure."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        retryable: bool = False,
        ambiguous: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retryable = retryable
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class SupportScreenshotCapability:
    upload_url: str
    max_input_bytes: int
    max_output_bytes: int
    max_width: int
    max_height: int
    max_pixels: int
    sanitizer_version: str

    def as_ui_payload(self) -> dict[str, Any]:
        return {
            "available": True,
            "content_type": "image/png",
            "max_input_bytes": self.max_input_bytes,
            "max_output_bytes": self.max_output_bytes,
            "max_width": self.max_width,
            "max_height": self.max_height,
            "max_pixels": self.max_pixels,
            "sanitizer_version": self.sanitizer_version,
        }


@dataclass(frozen=True)
class SupportCapability:
    origin: str
    reports_url: str
    report_lookup_url: str
    terms_url: str
    max_report_bytes: int
    purpose: str
    terms_version: str
    retention_policy_version: str
    terms_text: str
    retention_text: str
    screenshots: SupportScreenshotCapability | None = None

    def as_ui_payload(self) -> dict[str, Any]:
        return {
            "available": True,
            "purpose": self.purpose,
            "terms_version": self.terms_version,
            "retention_policy_version": self.retention_policy_version,
            "terms_text": self.terms_text,
            "retention_text": self.retention_text,
            "max_report_bytes": self.max_report_bytes,
            "screenshots": (
                self.screenshots.as_ui_payload()
                if self.screenshots is not None
                else unavailable_screenshot_capability()
            ),
            "message": None,
        }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


_OPENER = urllib.request.build_opener(_NoRedirect())


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _utc_now() -> str:
    return _format_timestamp(_now_utc())


def _canonical_uuid4(value: Any) -> str:
    if not isinstance(value, str):
        raise SupportReportError("invalid_report_id", "Invalid support report id.", status=404)
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise SupportReportError("invalid_report_id", "Invalid support report id.", status=404) from exc
    if parsed.version != 4 or str(parsed) != value.lower():
        raise SupportReportError("invalid_report_id", "Invalid support report id.", status=404)
    return str(parsed)


def _normalized_origin(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SupportReportError(
            "capability_incompatible",
            "Private support reporting is not configured.",
            status=503,
        )
    try:
        parsed = urlsplit(value.strip())
        port = parsed.port
    except ValueError as exc:
        raise SupportReportError(
            "capability_incompatible",
            "Private support reporting is not configured.",
            status=503,
        ) from exc
    hostname = (parsed.hostname or "").lower()
    is_https = parsed.scheme.lower() == "https"
    is_loopback = (
        parsed.scheme.lower() == "http"
        and hostname in {"localhost", "127.0.0.1", "::1"}
    )
    if (
        not hostname
        or not (is_https or is_loopback)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise SupportReportError(
            "capability_incompatible",
            "Private support reporting requires HTTPS (or loopback HTTP for development).",
            status=503,
        )
    rendered_host = f"[{hostname}]" if ":" in hostname else hostname
    rendered_port = f":{port}" if port is not None else ""
    return f"{parsed.scheme.lower()}://{rendered_host}{rendered_port}"


def _same_origin_endpoint(origin: str, value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SupportReportError(
            "capability_incompatible",
            f"Private support capability is missing {field}.",
            status=503,
        )
    endpoint = urljoin(origin + "/", value.strip())
    if _normalized_origin(endpoint) != origin:
        raise SupportReportError(
            "capability_incompatible",
            "Private support endpoints must stay on the configured service origin.",
            status=503,
        )
    return endpoint


def _bounded_text(value: Any, *, field: str, maximum: int = 8192) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise SupportReportError(
            "capability_incompatible",
            f"Private support terms contain an invalid {field}.",
            status=503,
        )
    return value.strip()


def _version(value: Any, *, field: str) -> str:
    candidate = _bounded_text(value, field=field, maximum=128)
    if _VERSION_RE.fullmatch(candidate) is None:
        raise SupportReportError(
            "capability_incompatible",
            f"Private support terms contain an invalid {field}.",
            status=503,
        )
    return candidate


def _parse_timestamp(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str) or not value or len(value) > 64:
        raise SupportReportError(
            "invalid_service_response",
            f"The private support receipt has an invalid {field}.",
            status=502,
            retryable=True,
        )
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SupportReportError(
            "invalid_service_response",
            f"The private support receipt has an invalid {field}.",
            status=502,
            retryable=True,
        ) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise SupportReportError(
            "invalid_service_response",
            f"The private support receipt has an invalid {field}.",
            status=502,
            retryable=True,
        )
    return parsed.astimezone(timezone.utc)


def _timestamp(value: Any, *, field: str) -> str:
    _parse_timestamp(value, field=field)
    return value


def _decode_json_response(response: Any) -> dict[str, Any]:
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > SUPPORT_REMOTE_RESPONSE_MAX_BYTES:
                raise SupportReportError(
                    "invalid_service_response",
                    "The private support service returned an invalid response.",
                    status=502,
                )
        except ValueError as exc:
            raise SupportReportError(
                "invalid_service_response",
                "The private support service returned an invalid response.",
                status=502,
            ) from exc
    raw = response.read(SUPPORT_REMOTE_RESPONSE_MAX_BYTES + 1)
    if len(raw) > SUPPORT_REMOTE_RESPONSE_MAX_BYTES:
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid response.",
            status=502,
        )
    try:
        parsed = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid response.",
            status=502,
        ) from exc
    if not isinstance(parsed, dict):
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid response.",
            status=502,
        )
    return parsed


def _parse_http_error(exc: urllib.error.HTTPError, *, mutation: bool) -> SupportReportError:
    try:
        parsed = _decode_json_response(exc)
    except SupportReportError:
        parsed = {}
    code = parsed.get("code")
    if not isinstance(code, str) or code not in _REMOTE_ERROR_CODES:
        code = (
            "rate_limited" if exc.code == 429 else
            "support_reports_closed" if exc.code == 503 else
            "request_rejected" if exc.code < 500 else
            "support_service_unavailable"
        )
    retryable = exc.code == 429 or exc.code >= 500
    return SupportReportError(
        code,
        "The private support service is temporarily unavailable."
        if retryable else "The private support service rejected this report.",
        status=exc.code if exc.code in {400, 401, 403, 404, 409, 413, 429} else 502,
        retryable=retryable,
        ambiguous=mutation and exc.code >= 500,
    )


def _request_json(
    url: str,
    *,
    method: str = "GET",
    payload: Mapping[str, Any] | None = None,
    manage_secret: str | None = None,
    timeout: int = 20,
) -> tuple[int, dict[str, Any]]:
    data = None
    headers = {
        "Accept": "application/json",
        "User-Agent": f"clawjournal/{__version__}",
    }
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if manage_secret is not None:
        headers["X-ClawJournal-Report-Secret"] = manage_secret
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    mutation = method in {"POST", "PUT", "DELETE"}
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            try:
                parsed = _decode_json_response(response)
            except SupportReportError as exc:
                if not mutation:
                    raise
                raise SupportReportError(
                    exc.code,
                    exc.message,
                    status=exc.status,
                    retryable=True,
                    ambiguous=True,
                ) from exc
            return int(response.status), parsed
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SupportReportError(
                "redirect_rejected",
                "The private support service refused a redirected request.",
                status=502,
                retryable=True,
                ambiguous=mutation,
            ) from exc
        raise _parse_http_error(exc, mutation=mutation) from exc
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise SupportReportError(
            "support_service_unavailable",
            "The private support service is temporarily unavailable.",
            status=502,
            retryable=True,
            ambiguous=mutation,
        ) from exc


def _require_success_status(
    status: int,
    allowed: set[int],
    *,
    mutation: bool = False,
) -> None:
    if status not in allowed:
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid response status.",
            status=502,
            retryable=True,
            ambiguous=mutation,
        )


def discover_capability(
    discovery: Mapping[str, Any],
    *,
    origin: str,
) -> SupportCapability:
    """Validate discovery and fetch the separately versioned support terms."""

    normalized_origin = _normalized_origin(origin)
    raw = discovery.get("support_reports")
    if not isinstance(raw, Mapping):
        raise SupportReportError(
            "support_reports_unavailable",
            "Private support reporting is not available on this service.",
            status=503,
        )
    if raw.get("api_version") != SUPPORT_REPORT_API_VERSION or isinstance(
        raw.get("api_version"), bool
    ):
        raise SupportReportError(
            "capability_incompatible",
            "This private support service requires a newer ClawJournal client.",
            status=503,
        )
    if raw.get("open") is not True:
        raise SupportReportError(
            "support_reports_closed",
            "Private support reporting is temporarily closed.",
            status=503,
            retryable=True,
        )
    maximum = raw.get("max_report_bytes")
    if (
        not isinstance(maximum, int)
        or isinstance(maximum, bool)
        or maximum <= 0
    ):
        raise SupportReportError(
            "capability_incompatible",
            "Private support reporting has no valid report-size limit.",
            status=503,
        )
    maximum = min(maximum, SUPPORT_REPORT_LOCAL_MAX_BYTES)
    reports_url = _same_origin_endpoint(normalized_origin, raw.get("reports_url"), field="reports_url")
    lookup_url = _same_origin_endpoint(
        normalized_origin,
        raw.get("report_lookup_url"),
        field="report_lookup_url",
    )
    if "{client_report_id}" not in lookup_url:
        raise SupportReportError(
            "capability_incompatible",
            "Private support receipt lookup is not configured correctly.",
            status=503,
        )
    terms_url = _same_origin_endpoint(normalized_origin, raw.get("terms_url"), field="terms_url")
    terms_status, terms = _request_json(terms_url, timeout=15)
    _require_success_status(terms_status, {200})
    if terms.get("schema_version") != SUPPORT_REPORT_SCHEMA_VERSION or isinstance(
        terms.get("schema_version"), bool
    ):
        raise SupportReportError(
            "capability_incompatible",
            "This private support terms document is incompatible.",
            status=503,
        )
    screenshots: SupportScreenshotCapability | None = None
    raw_screenshots = raw.get("screenshots")
    if isinstance(raw_screenshots, Mapping) and raw_screenshots.get("open") is True:
        if (
            raw_screenshots.get("api_version") != SUPPORT_SCREENSHOT_API_VERSION
            or isinstance(raw_screenshots.get("api_version"), bool)
            or raw_screenshots.get("content_type") != "image/png"
        ):
            raise SupportReportError(
                "capability_incompatible",
                "This private support screenshot service requires a newer ClawJournal client.",
                status=503,
            )
        screenshot_limits: dict[str, int] = {}
        for field, local_maximum in (
            ("max_input_bytes", SUPPORT_SCREENSHOT_LOCAL_MAX_BYTES),
            ("max_output_bytes", SUPPORT_SCREENSHOT_LOCAL_MAX_BYTES),
            ("max_width", SUPPORT_SCREENSHOT_LOCAL_MAX_DIMENSION),
            ("max_height", SUPPORT_SCREENSHOT_LOCAL_MAX_DIMENSION),
            ("max_pixels", SUPPORT_SCREENSHOT_LOCAL_MAX_PIXELS),
        ):
            candidate = raw_screenshots.get(field)
            if (
                not isinstance(candidate, int)
                or isinstance(candidate, bool)
                or candidate <= 0
            ):
                raise SupportReportError(
                    "capability_incompatible",
                    "Private support screenshots have invalid limits.",
                    status=503,
                )
            screenshot_limits[field] = min(candidate, local_maximum)
        upload_url = _same_origin_endpoint(
            normalized_origin,
            raw_screenshots.get("upload_url"),
            field="screenshots.upload_url",
        )
        if "{client_report_id}" not in upload_url:
            raise SupportReportError(
                "capability_incompatible",
                "Private support screenshot upload is not configured correctly.",
                status=503,
            )
        screenshots = SupportScreenshotCapability(
            upload_url=upload_url,
            sanitizer_version=_version(
                raw_screenshots.get("sanitizer_version"),
                field="screenshots.sanitizer_version",
            ),
            **screenshot_limits,
        )

    return SupportCapability(
        origin=normalized_origin,
        reports_url=reports_url,
        report_lookup_url=lookup_url,
        terms_url=terms_url,
        max_report_bytes=maximum,
        purpose=_bounded_text(terms.get("purpose"), field="purpose", maximum=512),
        terms_version=_version(terms.get("terms_version"), field="terms_version"),
        retention_policy_version=_version(
            terms.get("retention_policy_version"),
            field="retention_policy_version",
        ),
        terms_text=_bounded_text(terms.get("terms_text"), field="terms_text"),
        retention_text=_bounded_text(terms.get("retention_text"), field="retention_text"),
        screenshots=screenshots,
    )


def fetch_capability(*, origin: str) -> SupportCapability:
    """Fetch the bounded canonical discovery document and current terms."""

    normalized_origin = _normalized_origin(origin)
    discovery_status, discovery = _request_json(
        f"{normalized_origin}/.well-known/clawjournal-share.json",
        timeout=15,
    )
    _require_success_status(discovery_status, {200})
    return discover_capability(discovery, origin=normalized_origin)


def unavailable_screenshot_capability() -> dict[str, Any]:
    return {
        "available": False,
        "content_type": "image/png",
        "max_input_bytes": 0,
        "max_output_bytes": 0,
        "max_width": 0,
        "max_height": 0,
        "max_pixels": 0,
        "sanitizer_version": "",
    }


def unavailable_capability(message: str = "Private support reporting is unavailable.") -> dict[str, Any]:
    return {
        "available": False,
        "purpose": "",
        "terms_version": "",
        "retention_policy_version": "",
        "terms_text": "",
        "retention_text": "",
        "max_report_bytes": 0,
        "screenshots": unavailable_screenshot_capability(),
        "message": message,
    }


def outbox_directory() -> Path:
    return Path(config_module.CONFIG_DIR) / SUPPORT_REPORTS_DIRNAME


def _record_path(client_report_id: str) -> Path:
    return outbox_directory() / f"{_canonical_uuid4(client_report_id)}.json"


def _screenshot_path(client_report_id: str) -> Path:
    return outbox_directory() / f"{_canonical_uuid4(client_report_id)}.png"


def _report_lock(client_report_id: str) -> threading.Lock:
    with _REPORT_LOCKS_GUARD:
        return _REPORT_LOCKS.setdefault(client_report_id, threading.Lock())


@contextmanager
def support_outbox_egress_lock(*, blocking: bool = True):
    """Serialize support lookup/POST/delete decisions across daemon processes."""

    directory = outbox_directory()
    file = None
    try:
        _ensure_private_directory(directory)
        path = directory / SUPPORT_OUTBOX_LOCK_FILENAME
        file = path.open("a+b")
        if os.name == "nt":
            os.chmod(path, stat.S_IREAD | stat.S_IWRITE)
        else:
            os.fchmod(file.fileno(), 0o600)
        if file.seek(0, os.SEEK_END) == 0:
            file.write(b"0")
            file.flush()
            os.fsync(file.fileno())
        _require_private_mode(path, 0o600)
        file.seek(0)
        if os.name == "nt":
            import msvcrt

            mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
            msvcrt.locking(file.fileno(), mode, 1)
        else:
            import fcntl

            mode = fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB
            fcntl.flock(file.fileno(), mode)
    except (OSError, CredentialStoreError) as exc:
        try:
            if file is not None:
                file.close()
        except OSError:
            pass
        raise SupportReportError(
            "outbox_busy" if not blocking else "outbox_unavailable",
            "The private support outbox is busy."
            if not blocking else "The private support outbox is unavailable.",
            status=409 if not blocking else 500,
        ) from exc
    try:
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt

                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()


def _atomic_write_record(record: Mapping[str, Any]) -> None:
    path = _record_path(str(record.get("client_report_id", "")))
    try:
        _ensure_private_directory(path.parent)
    except CredentialStoreError as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "Could not create the private support outbox.",
            status=500,
        ) from exc
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    if len(payload.encode("utf-8")) > SUPPORT_OUTBOX_RECORD_MAX_BYTES:
        raise SupportReportError(
            "outbox_unavailable",
            "The private support outbox record is unexpectedly large.",
            status=500,
        )
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".support-report-", suffix=".tmp")
        if os.name == "nt":
            os.chmod(temporary, stat.S_IREAD | stat.S_IWRITE)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        assert temporary is not None
        _require_private_mode(Path(temporary), 0o600)
        os.replace(temporary, path)
        temporary = None
        _require_private_mode(path, 0o600)
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except (OSError, CredentialStoreError) as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "Could not durably update the private support outbox.",
            status=500,
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _fsync_directory(directory: Path) -> None:
    if not directory.exists() or not hasattr(os, "O_DIRECTORY"):
        return
    directory_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _atomic_write_screenshot(client_report_id: str, screenshot: bytes) -> Path:
    path = _screenshot_path(client_report_id)
    try:
        _ensure_private_directory(path.parent)
    except CredentialStoreError as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "Could not create the private support outbox.",
            status=500,
        ) from exc
    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(
            dir=path.parent,
            prefix=".support-screenshot-",
            suffix=".tmp",
        )
        if os.name == "nt":
            os.chmod(temporary, stat.S_IREAD | stat.S_IWRITE)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(screenshot)
            handle.flush()
            os.fsync(handle.fileno())
        assert temporary is not None
        _require_private_mode(Path(temporary), 0o600)
        os.replace(temporary, path)
        temporary = None
        _require_private_mode(path, 0o600)
        _fsync_directory(path.parent)
        return path
    except (OSError, CredentialStoreError) as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "Could not durably store the private support screenshot.",
            status=500,
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def _hex_digest(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 64:
        return None
    return value if all(character in "0123456789abcdef" for character in value) else None


def _png_dimensions(png: bytes) -> tuple[int, int]:
    if len(png) < 33 or png[:8] != _PNG_SIGNATURE:
        raise SupportReportError("invalid_screenshot", "The screenshot must be a valid PNG image.")
    length = struct.unpack(">I", png[8:12])[0]
    if length != 13 or png[12:16] != b"IHDR":
        raise SupportReportError("invalid_screenshot", "The screenshot must be a valid PNG image.")
    expected_crc = struct.unpack(">I", png[29:33])[0]
    import zlib

    if zlib.crc32(png[12:29]) & 0xFFFFFFFF != expected_crc:
        raise SupportReportError("invalid_screenshot", "The screenshot must be a valid PNG image.")
    width, height = struct.unpack(">II", png[16:24])
    if width <= 0 or height <= 0:
        raise SupportReportError("invalid_screenshot", "The screenshot has invalid dimensions.")
    return width, height


def _decode_screenshot(
    *,
    encoded: Any,
    expected_sha256: Any,
    expected_width: Any,
    expected_height: Any,
    capability: SupportScreenshotCapability,
) -> tuple[bytes, str, int, int]:
    if (
        not isinstance(encoded, str)
        or len(encoded) == 0
        or len(encoded.encode("ascii", "ignore")) != len(encoded)
        or len(encoded) > SUPPORT_SCREENSHOT_BASE64_MAX_BYTES
    ):
        raise SupportReportError("invalid_screenshot", "The screenshot payload is invalid.")
    digest = _hex_digest(expected_sha256)
    if digest is None:
        raise SupportReportError("invalid_screenshot", "The screenshot hash is invalid.")
    try:
        png = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise SupportReportError("invalid_screenshot", "The screenshot payload is invalid.") from exc
    if len(png) > capability.max_input_bytes:
        raise SupportReportError(
            "screenshot_too_large",
            "The screenshot exceeds the private support size limit.",
            status=413,
        )
    if hashlib.sha256(png).hexdigest() != digest:
        raise SupportReportError("screenshot_hash_mismatch", "The screenshot hash does not match.")
    width, height = _png_dimensions(png)
    if (
        not isinstance(expected_width, int)
        or isinstance(expected_width, bool)
        or not isinstance(expected_height, int)
        or isinstance(expected_height, bool)
        or width != expected_width
        or height != expected_height
        or width > capability.max_width
        or height > capability.max_height
        or width * height > capability.max_pixels
    ):
        raise SupportReportError("invalid_screenshot", "The screenshot dimensions are invalid.")
    return png, digest, width, height


def _read_screenshot(record: Mapping[str, Any]) -> bytes:
    screenshot = record.get("screenshot")
    if not isinstance(screenshot, Mapping):
        raise SupportReportError("outbox_corrupt", "The private support screenshot is unreadable.", status=500)
    path = _screenshot_path(str(record["client_report_id"]))
    if path.is_symlink() or not path.exists():
        raise SupportReportError("outbox_corrupt", "The private support screenshot is unreadable.", status=500)
    fd = -1
    try:
        _require_private_mode(path.parent, 0o700)
        _require_private_mode(path, 0o600)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        file_stat = os.fstat(fd)
        maximum = min(int(screenshot["input_bytes"]), SUPPORT_SCREENSHOT_LOCAL_MAX_BYTES)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_size != maximum:
            raise SupportReportError("outbox_corrupt", "The private support screenshot is unreadable.", status=500)
        chunks: list[bytes] = []
        remaining = maximum + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        png = b"".join(chunks)
    except (OSError, CredentialStoreError) as exc:
        raise SupportReportError("outbox_corrupt", "The private support screenshot is unreadable.", status=500) from exc
    finally:
        if fd >= 0:
            os.close(fd)
    if len(png) != int(screenshot["input_bytes"]) or hashlib.sha256(png).hexdigest() != screenshot["source_sha256"]:
        raise SupportReportError("outbox_corrupt", "The private support screenshot is unreadable.", status=500)
    width, height = _png_dimensions(png)
    if width != screenshot["width"] or height != screenshot["height"]:
        raise SupportReportError("outbox_corrupt", "The private support screenshot is unreadable.", status=500)
    return png


def _validate_record(raw: Any, *, expected_id: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    schema_version = raw.get("outbox_schema_version")
    if schema_version == 1 and set(raw) == _V1_RECORD_FIELDS:
        raw = {**raw, "outbox_schema_version": SUPPORT_OUTBOX_SCHEMA_VERSION, "screenshot": None}
    elif schema_version != SUPPORT_OUTBOX_SCHEMA_VERSION or set(raw) != _RECORD_FIELDS:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    if _canonical_uuid4(raw.get("client_report_id")) != expected_id:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    state = raw.get("state")
    if state not in _ALL_STATES:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    attempt_count = raw.get("attempt_count")
    if (
        not isinstance(attempt_count, int)
        or isinstance(attempt_count, bool)
        or attempt_count < 0
        or attempt_count > 1_000_000
    ):
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    required_strings = (
        "created_at",
        "updated_at",
        "api_origin",
        "reports_url",
        "report_lookup_url",
        "content_sha256",
        "accepted_terms_version",
        "accepted_retention_policy_version",
        "manage_secret",
    )
    if any(not isinstance(raw.get(field), str) or not raw[field] for field in required_strings):
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    try:
        _timestamp(raw["created_at"], field="created_at")
        _timestamp(raw["updated_at"], field="updated_at")
    except SupportReportError as exc:
        raise SupportReportError(
            "outbox_corrupt", "The private support receipt is unreadable.", status=500
        ) from exc
    if (
        _VERSION_RE.fullmatch(raw["accepted_terms_version"]) is None
        or _VERSION_RE.fullmatch(raw["accepted_retention_policy_version"]) is None
    ):
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    origin = _normalized_origin(raw["api_origin"])
    if _normalized_origin(raw["reports_url"]) != origin or _normalized_origin(raw["report_lookup_url"]) != origin:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    if "{client_report_id}" not in raw["report_lookup_url"]:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    if _MANAGE_SECRET_RE.fullmatch(raw["manage_secret"]) is None:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    digest = raw["content_sha256"]
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    markdown = raw.get("report_markdown")
    plaintext_expired = raw.get("plaintext_expired")
    if not isinstance(plaintext_expired, bool):
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    message_code = raw.get("message_code")
    if message_code is not None and message_code not in _STORED_MESSAGE_CODES:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    if state in _PENDING_STATES:
        if plaintext_expired:
            if (
                state != "ambiguous"
                or markdown is not None
                or attempt_count < 1
                or message_code != "local_pending_expired"
            ):
                raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
            encoded_markdown = b""
        else:
            try:
                encoded_markdown = markdown.encode("utf-8") if isinstance(markdown, str) else b""
            except UnicodeEncodeError as exc:
                raise SupportReportError(
                    "outbox_corrupt", "The private support receipt is unreadable.", status=500
                ) from exc
            if (
                not isinstance(markdown, str)
                or len(encoded_markdown) > SUPPORT_REPORT_LOCAL_MAX_BYTES
                or any(
                    (ord(character) < 32 and character not in "\t\n\r")
                    or ord(character) == 127
                    for character in markdown
                )
                or hashlib.sha256(encoded_markdown).hexdigest() != digest
            ):
                raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    elif markdown is not None:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    elif plaintext_expired:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    receipt_id = raw.get("receipt_id")
    expires_at = raw.get("expires_at")
    next_attempt_at = raw.get("next_attempt_at")
    if state in _PENDING_STATES:
        try:
            _timestamp(next_attempt_at, field="next_attempt_at")
        except SupportReportError as exc:
            raise SupportReportError(
                "outbox_corrupt", "The private support receipt is unreadable.", status=500
            ) from exc
    elif next_attempt_at is not None:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    if state == "accepted":
        if not isinstance(receipt_id, str) or _RECEIPT_ID_RE.fullmatch(receipt_id) is None:
            raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        try:
            _timestamp(expires_at, field="expires_at")
        except SupportReportError as exc:
            raise SupportReportError(
                "outbox_corrupt", "The private support receipt is unreadable.", status=500
            ) from exc
    elif receipt_id is not None or expires_at is not None:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    screenshot = raw.get("screenshot")
    if screenshot is not None:
        if not isinstance(screenshot, dict) or set(screenshot) != _SCREENSHOT_RECORD_FIELDS:
            raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        screenshot_state = screenshot.get("state")
        screenshot_attempts = screenshot.get("attempt_count")
        source_digest = _hex_digest(screenshot.get("source_sha256"))
        if (
            screenshot_state not in _SCREENSHOT_STATES
            or not isinstance(screenshot_attempts, int)
            or isinstance(screenshot_attempts, bool)
            or screenshot_attempts < 0
            or screenshot_attempts > 1_000_000
            or source_digest is None
            or screenshot.get("png_filename") != f"{expected_id}.png"
        ):
            raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        for field, maximum in (
            ("input_bytes", SUPPORT_SCREENSHOT_LOCAL_MAX_BYTES),
            ("width", SUPPORT_SCREENSHOT_LOCAL_MAX_DIMENSION),
            ("height", SUPPORT_SCREENSHOT_LOCAL_MAX_DIMENSION),
        ):
            candidate = screenshot.get(field)
            if (
                not isinstance(candidate, int)
                or isinstance(candidate, bool)
                or candidate <= 0
                or candidate > maximum
            ):
                raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        if int(screenshot["width"]) * int(screenshot["height"]) > SUPPORT_SCREENSHOT_LOCAL_MAX_PIXELS:
            raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        try:
            _timestamp(screenshot.get("local_expires_at"), field="screenshot.local_expires_at")
        except SupportReportError as exc:
            raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500) from exc
        upload_url = screenshot.get("upload_url")
        if not isinstance(upload_url, str) or _normalized_origin(upload_url) != origin:
            raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        screenshot_code = screenshot.get("message_code")
        if screenshot_code is not None and screenshot_code not in _STORED_MESSAGE_CODES:
            raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        screenshot_next = screenshot.get("next_attempt_at")
        screenshot_receipt = screenshot.get("receipt")
        if screenshot_state in {"queued", "submitting", "ambiguous"}:
            try:
                _timestamp(screenshot_next, field="screenshot.next_attempt_at")
            except SupportReportError as exc:
                raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500) from exc
            if screenshot_receipt is not None:
                raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        elif screenshot_next is not None:
            raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        if screenshot_state == "accepted":
            if not isinstance(screenshot_receipt, dict) or set(screenshot_receipt) != _SCREENSHOT_RECEIPT_FIELDS:
                raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
            if (
                screenshot_receipt.get("source_sha256") != source_digest
                or _hex_digest(screenshot_receipt.get("sanitized_sha256")) is None
                or not isinstance(screenshot_receipt.get("sanitizer_version"), str)
                or _VERSION_RE.fullmatch(screenshot_receipt["sanitizer_version"]) is None
            ):
                raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
            try:
                _timestamp(screenshot_receipt.get("created_at"), field="screenshot.created_at")
            except SupportReportError as exc:
                raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500) from exc
            for field, maximum in (
                ("sanitized_bytes", SUPPORT_SCREENSHOT_LOCAL_MAX_BYTES),
                ("width", SUPPORT_SCREENSHOT_LOCAL_MAX_DIMENSION),
                ("height", SUPPORT_SCREENSHOT_LOCAL_MAX_DIMENSION),
            ):
                candidate = screenshot_receipt.get(field)
                if (
                    not isinstance(candidate, int)
                    or isinstance(candidate, bool)
                    or candidate <= 0
                    or candidate > maximum
                ):
                    raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
            if (
                int(screenshot_receipt["width"]) != int(screenshot["width"])
                or int(screenshot_receipt["height"]) != int(screenshot["height"])
                or int(screenshot_receipt["width"]) * int(screenshot_receipt["height"])
                > SUPPORT_SCREENSHOT_LOCAL_MAX_PIXELS
            ):
                raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
        elif screenshot_receipt is not None:
            raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500)
    return dict(raw)


def load_report(client_report_id: str) -> dict[str, Any]:
    identifier = _canonical_uuid4(client_report_id)
    path = _record_path(identifier)
    if not path.exists():
        raise SupportReportError("report_not_found", "Support report not found.", status=404)
    if path.parent.is_symlink() or path.is_symlink():
        raise SupportReportError(
            "outbox_corrupt", "The private support receipt is unreadable.", status=500
        )
    fd = -1
    try:
        _require_private_mode(path.parent, 0o700)
        _require_private_mode(path, 0o600)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(path, flags)
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise SupportReportError(
                "outbox_corrupt",
                "The private support receipt is unreadable.",
                status=500,
            )
        if file_stat.st_size > SUPPORT_OUTBOX_RECORD_MAX_BYTES:
            raise SupportReportError(
                "outbox_corrupt",
                "The private support receipt is unreadable.",
                status=500,
            )
        chunks: list[bytes] = []
        remaining = SUPPORT_OUTBOX_RECORD_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > SUPPORT_OUTBOX_RECORD_MAX_BYTES:
            raise SupportReportError(
                "outbox_corrupt",
                "The private support receipt is unreadable.",
                status=500,
            )
        raw = json.loads(encoded.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, CredentialStoreError) as exc:
        raise SupportReportError("outbox_corrupt", "The private support receipt is unreadable.", status=500) from exc
    finally:
        if fd >= 0:
            os.close(fd)
    return _validate_record(raw, expected_id=identifier)


def list_public_reports(
    *,
    limit: int = SUPPORT_REPORT_LIST_LIMIT,
) -> dict[str, Any]:
    """Return a bounded newest-first projection of private outbox metadata.

    Individual malformed, oversized, non-regular, or symlink entries are
    skipped. A broken privacy boundary on the outbox directory itself fails
    with one fixed error instead of scanning an unsafe location.
    """

    directory = outbox_directory()
    if directory.is_symlink():
        raise SupportReportError(
            "outbox_unavailable",
            "The private support outbox is unavailable.",
            status=500,
        )
    if not directory.exists():
        return {"reports": [], "truncated": False}
    cleanup_orphaned_screenshot_files()
    try:
        _require_private_mode(directory, 0o700)
    except (OSError, CredentialStoreError) as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "The private support outbox is unavailable.",
            status=500,
        ) from exc

    safe_limit = max(1, min(int(limit), SUPPORT_REPORT_LIST_LIMIT))
    reports: list[dict[str, Any]] = []
    scanned = 0
    truncated = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > SUPPORT_REPORT_LIST_SCAN_LIMIT:
                    truncated = True
                    break
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.endswith(".json"):
                    continue
                try:
                    identifier = _canonical_uuid4(entry.name[:-5])
                    record = load_report(identifier)
                    if _pending_expired(record):
                        if expire_pending_report(
                            identifier,
                            respect_schedule=True,
                            attempt_remote=False,
                            blocking=False,
                        ):
                            continue
                        record = load_report(identifier)
                    reports.append(public_status(record))
                except (OSError, SupportReportError):
                    continue
    except OSError as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "The private support outbox is unavailable.",
            status=500,
        ) from exc
    reports.sort(key=lambda item: str(item["created_at"]), reverse=True)
    if len(reports) > safe_limit:
        truncated = True
        reports = reports[:safe_limit]
    return {"reports": reports, "truncated": truncated}


def _safe_message(state: str, code: Any = None) -> str | None:
    if state == "queued":
        return "Report queued for private delivery."
    if state == "submitting":
        return "Sending the private report."
    if state == "ambiguous":
        if code == "local_pending_expired":
            return "Local plaintext expired; ClawJournal is confirming remote deletion."
        if code == "local_screenshot_expired":
            return "The local screenshot expired; ClawJournal is confirming whole-report deletion."
        return "Delivery is being confirmed; the exact report will not be duplicated."
    if state == "accepted":
        return "The private support service received the report."
    if state == "rejected":
        if code == "local_screenshot_expired":
            return "The local screenshot expired and its bytes were removed."
        if code == "idempotency_conflict":
            return "The support service rejected a conflicting retry."
        return "The private support service rejected the report."
    return None


def public_status(record: Mapping[str, Any]) -> dict[str, Any]:
    state = str(record["state"])
    raw_screenshot = record.get("screenshot")
    screenshot_status = None
    if isinstance(raw_screenshot, Mapping):
        screenshot_state = str(raw_screenshot["state"])
        raw_receipt = raw_screenshot.get("receipt")
        screenshot_status = {
            "state": screenshot_state,
            "source_sha256": str(raw_screenshot["source_sha256"]),
            "source_bytes": int(raw_screenshot["input_bytes"]),
            "width": int(raw_screenshot["width"]),
            "height": int(raw_screenshot["height"]),
            "message": _safe_screenshot_message(
                screenshot_state,
                raw_screenshot.get("message_code"),
            ),
            "sanitized_sha256": (
                str(raw_receipt["sanitized_sha256"])
                if isinstance(raw_receipt, Mapping) else None
            ),
            "sanitized_bytes": (
                int(raw_receipt["sanitized_bytes"])
                if isinstance(raw_receipt, Mapping) else None
            ),
            "sanitizer_version": (
                str(raw_receipt["sanitizer_version"])
                if isinstance(raw_receipt, Mapping) else None
            ),
        }
    return {
        "client_report_id": str(record["client_report_id"]),
        "state": state,
        "receipt_id": record.get("receipt_id") if isinstance(record.get("receipt_id"), str) else None,
        "message": _safe_message(state, record.get("message_code")),
        "created_at": str(record["created_at"]),
        "expires_at": record.get("expires_at") if isinstance(record.get("expires_at"), str) else None,
        "screenshot": screenshot_status,
    }


def _safe_screenshot_message(state: str, code: Any = None) -> str | None:
    if state == "queued":
        return "Screenshot queued for private delivery."
    if state == "submitting":
        return "Sending the exact reviewed screenshot."
    if state == "ambiguous":
        return "Screenshot delivery is being confirmed before retrying."
    if state == "accepted":
        return "Private support received and sanitized the screenshot."
    if state == "rejected":
        if code == "support_screenshots_closed":
            return "Private screenshot intake was closed before the report could be created."
        return "Private support did not accept the screenshot."
    return None


def _pending_expired(
    record: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    if record.get("state") not in _PENDING_STATES:
        return False
    created_at = _parse_timestamp(record.get("created_at"), field="created_at")
    return (now or _now_utc()) >= created_at + timedelta(
        days=SUPPORT_PENDING_RETENTION_DAYS
    )


def _attempt_due(
    record: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    if record.get("state") not in _PENDING_STATES:
        return False
    next_attempt = _parse_timestamp(
        record.get("next_attempt_at"), field="next_attempt_at"
    )
    return (now or _now_utc()) >= next_attempt


def _next_retry_at(record: Mapping[str, Any]) -> str:
    attempt_count = max(1, int(record.get("attempt_count", 1)))
    exponent = min(attempt_count - 1, 20)
    bounded = min(
        SUPPORT_RETRY_MAX_SECONDS,
        SUPPORT_RETRY_BASE_SECONDS * (2 ** exponent),
    )
    jittered = max(1.0, bounded * random.uniform(0.8, 1.2))
    return _format_timestamp(_now_utc() + timedelta(seconds=jittered))


def _unlink_local_record(client_report_id: str) -> None:
    path = _record_path(client_report_id)
    screenshot_path = _screenshot_path(client_report_id)
    try:
        screenshot_path.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "Could not remove the private support receipt.",
            status=500,
        ) from exc


def _unlink_local_screenshot(client_report_id: str) -> None:
    path = _screenshot_path(client_report_id)
    try:
        path.unlink(missing_ok=True)
        _fsync_directory(path.parent)
    except OSError as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "Could not remove the private support screenshot.",
            status=500,
        ) from exc


def _cleanup_orphaned_screenshots_locked(
    *,
    now_epoch: float,
    grace_seconds: float,
) -> int:
    directory = outbox_directory()
    if not directory.exists() or directory.is_symlink():
        return 0
    try:
        _require_private_mode(directory, 0o700)
    except (OSError, CredentialStoreError):
        return 0
    removed = 0
    scanned = 0
    changed = False
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > SUPPORT_ORPHAN_SCAN_LIMIT:
                    break
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                name = entry.name
                temporary = (
                    name.startswith(".support-screenshot-")
                    or name.startswith(".support-report-")
                ) and name.endswith(".tmp")
                screenshot_id: str | None = None
                if name.endswith(".png"):
                    try:
                        screenshot_id = _canonical_uuid4(name[:-4])
                    except SupportReportError:
                        continue
                elif not temporary:
                    continue
                try:
                    file_stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if now_epoch - file_stat.st_mtime < max(1.0, grace_seconds):
                    continue
                keep = False
                if screenshot_id is not None:
                    try:
                        record = load_report(screenshot_id)
                        screenshot = record.get("screenshot")
                        keep = (
                            isinstance(screenshot, Mapping)
                            and screenshot.get("png_filename") == name
                            and screenshot.get("state") in {"queued", "submitting", "ambiguous"}
                        )
                    except SupportReportError:
                        keep = False
                if keep:
                    continue
                try:
                    Path(entry.path).unlink()
                    removed += 1
                    changed = True
                except OSError:
                    continue
    except OSError:
        return removed
    if changed:
        try:
            _fsync_directory(directory)
        except OSError:
            pass
    return removed


def cleanup_orphaned_screenshot_files(
    *,
    now_epoch: float | None = None,
    grace_seconds: float = SUPPORT_ORPHAN_GRACE_SECONDS,
) -> int:
    """Non-blocking, bounded cleanup of crash-window PNG/temp artifacts."""

    if not outbox_directory().exists():
        return 0
    try:
        with support_outbox_egress_lock(blocking=False):
            return _cleanup_orphaned_screenshots_locked(
                now_epoch=time.time() if now_epoch is None else float(now_epoch),
                grace_seconds=max(1.0, float(grace_seconds)),
            )
    except SupportReportError as exc:
        if exc.code == "outbox_busy":
            return 0
        return 0


def expire_pending_report(
    client_report_id: str,
    *,
    respect_schedule: bool = True,
    attempt_remote: bool = True,
    blocking: bool = True,
) -> bool:
    """Expire plaintext and preserve uncertain remote deletion capability."""

    identifier = _canonical_uuid4(client_report_id)
    report_lock = _report_lock(identifier)
    acquired = report_lock.acquire(blocking=blocking)
    if not acquired:
        raise SupportReportError(
            "outbox_busy",
            "The private support outbox is busy.",
            status=409,
        )
    try:
        with support_outbox_egress_lock(blocking=blocking):
            try:
                record = load_report(identifier)
            except SupportReportError as exc:
                if exc.code == "report_not_found":
                    return False
                raise
            if not _pending_expired(record):
                return False
            screenshot = record.get("screenshot")
            if (
                isinstance(screenshot, Mapping)
                and screenshot.get("state") in {"queued", "submitting", "ambiguous"}
            ):
                return _expire_screenshot_locked(
                    record,
                    attempt_remote=attempt_remote,
                ) is None
            if record["state"] == "queued" and record["attempt_count"] == 0:
                _unlink_local_record(identifier)
                return True
            if record.get("plaintext_expired") is not True:
                record.update({
                    "state": "ambiguous",
                    "report_markdown": None,
                    "plaintext_expired": True,
                    "message_code": "local_pending_expired",
                    "updated_at": _utc_now(),
                })
                _atomic_write_record(record)
            if not attempt_remote:
                return False
            if respect_schedule and not _attempt_due(record):
                return False
            return _reconcile_expired_pending(record) is None
    finally:
        report_lock.release()


def enqueue_report(
    *,
    report_markdown: Any,
    accepted_terms_version: Any,
    accepted_retention_policy_version: Any,
    capability: SupportCapability,
    screenshot_png_base64: Any = None,
    screenshot_source_sha256: Any = None,
    screenshot_width: Any = None,
    screenshot_height: Any = None,
) -> dict[str, Any]:
    if not isinstance(report_markdown, str) or not report_markdown.strip():
        raise SupportReportError("invalid_request", "A non-empty Markdown report is required.")
    if any(
        (ord(character) < 32 and character not in "\t\n\r")
        or ord(character) == 127
        for character in report_markdown
    ):
        raise SupportReportError(
            "invalid_request",
            "The Markdown report contains unsupported control characters.",
        )
    try:
        encoded = report_markdown.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise SupportReportError(
            "invalid_request",
            "The Markdown report is not valid Unicode text.",
        ) from exc
    if len(encoded) > capability.max_report_bytes:
        raise SupportReportError(
            "report_too_large",
            f"The report exceeds the {capability.max_report_bytes}-byte private submission limit.",
            status=413,
        )
    if accepted_terms_version != capability.terms_version:
        raise SupportReportError("terms_mismatch", "Please review and accept the current support terms.", status=409)
    if accepted_retention_policy_version != capability.retention_policy_version:
        raise SupportReportError("terms_mismatch", "Please review and accept the current retention policy.", status=409)
    identifier = str(uuid.uuid4())
    now = _utc_now()
    screenshot_record: dict[str, Any] | None = None
    screenshot_png: bytes | None = None
    screenshot_values = (
        screenshot_png_base64,
        screenshot_source_sha256,
        screenshot_width,
        screenshot_height,
    )
    if any(value is not None for value in screenshot_values):
        if any(value is None for value in screenshot_values) or capability.screenshots is None:
            raise SupportReportError(
                "support_screenshots_closed",
                "Private screenshot submission is not available.",
                status=503,
                retryable=True,
            )
        screenshot_png, screenshot_digest, width, height = _decode_screenshot(
            encoded=screenshot_png_base64,
            expected_sha256=screenshot_source_sha256,
            expected_width=screenshot_width,
            expected_height=screenshot_height,
            capability=capability.screenshots,
        )
        screenshot_record = {
            "state": "queued",
            "attempt_count": 0,
            "next_attempt_at": now,
            "message_code": None,
            "source_sha256": screenshot_digest,
            "input_bytes": len(screenshot_png),
            "width": width,
            "height": height,
            "local_expires_at": _format_timestamp(
                _parse_timestamp(now, field="created_at")
                + timedelta(days=SUPPORT_PENDING_RETENTION_DAYS)
            ),
            "upload_url": capability.screenshots.upload_url.replace(
                "{client_report_id}", quote(identifier, safe="")
            ),
            "png_filename": f"{identifier}.png",
            "receipt": None,
        }
    record: dict[str, Any] = {
        "outbox_schema_version": SUPPORT_OUTBOX_SCHEMA_VERSION,
        "client_report_id": identifier,
        "state": "queued",
        "attempt_count": 0,
        "next_attempt_at": now,
        "created_at": now,
        "updated_at": now,
        "api_origin": capability.origin,
        "reports_url": capability.reports_url,
        "report_lookup_url": capability.report_lookup_url,
        "content_sha256": hashlib.sha256(encoded).hexdigest(),
        "report_markdown": report_markdown,
        "plaintext_expired": False,
        "accepted_terms_version": capability.terms_version,
        "accepted_retention_policy_version": capability.retention_policy_version,
        "manage_secret": secrets.token_urlsafe(32),
        "receipt_id": None,
        "expires_at": None,
        "message_code": None,
        "screenshot": screenshot_record,
    }
    if screenshot_png is not None:
        _atomic_write_screenshot(identifier, screenshot_png)
    try:
        _atomic_write_record(record)
    except SupportReportError:
        if screenshot_png is not None:
            try:
                _unlink_local_screenshot(identifier)
            except SupportReportError:
                pass
        raise
    return record


def _lookup_url(record: Mapping[str, Any]) -> str:
    return str(record["report_lookup_url"]).replace(
        "{client_report_id}",
        quote(str(record["client_report_id"]), safe=""),
    )


def _request_png(
    record: Mapping[str, Any],
    screenshot: Mapping[str, Any],
    png: bytes,
    *,
    timeout: int = 20,
) -> tuple[int, dict[str, Any]]:
    headers = {
        "Accept": "application/json",
        "Content-Type": "image/png",
        "Content-Length": str(len(png)),
        "User-Agent": f"clawjournal/{__version__}",
        "X-ClawJournal-Report-Secret": str(record["manage_secret"]),
        "X-ClawJournal-Screenshot-SHA256": str(screenshot["source_sha256"]),
        "X-ClawJournal-Accepted-Terms-Version": str(record["accepted_terms_version"]),
        "X-ClawJournal-Accepted-Retention-Policy-Version": str(
            record["accepted_retention_policy_version"]
        ),
    }
    request = urllib.request.Request(
        str(screenshot["upload_url"]),
        data=png,
        headers=headers,
        method="PUT",
    )
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            try:
                parsed = _decode_json_response(response)
            except SupportReportError as exc:
                raise SupportReportError(
                    exc.code,
                    exc.message,
                    status=exc.status,
                    retryable=True,
                    ambiguous=True,
                ) from exc
            return int(response.status), parsed
    except urllib.error.HTTPError as exc:
        if 300 <= exc.code < 400:
            raise SupportReportError(
                "redirect_rejected",
                "The private support service refused a redirected screenshot request.",
                status=502,
                retryable=True,
                ambiguous=True,
            ) from exc
        raise _parse_http_error(exc, mutation=True) from exc
    except (TimeoutError, urllib.error.URLError, OSError, http.client.HTTPException) as exc:
        raise SupportReportError(
            "support_service_unavailable",
            "The private support service is temporarily unavailable.",
            status=502,
            retryable=True,
            ambiguous=True,
        ) from exc


def _screenshot_receipt(
    record: Mapping[str, Any],
    response: Mapping[str, Any],
) -> dict[str, Any] | None:
    screenshot = record.get("screenshot")
    if not isinstance(screenshot, Mapping):
        return None
    raw = response.get("screenshot")
    if raw is None:
        return None
    if not isinstance(raw, Mapping):
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid screenshot receipt.",
            status=502,
            retryable=True,
        )
    projected = {
        "source_sha256": raw.get("source_sha256"),
        "sanitized_sha256": raw.get("sanitized_sha256"),
        "width": raw.get("width"),
        "height": raw.get("height"),
        "sanitized_bytes": raw.get("sanitized_bytes"),
        "sanitizer_version": raw.get("sanitizer_version"),
        "created_at": raw.get("created_at"),
    }
    if (
        projected["source_sha256"] != screenshot["source_sha256"]
        or _hex_digest(projected["sanitized_sha256"]) is None
        or not isinstance(projected["sanitizer_version"], str)
        or _VERSION_RE.fullmatch(projected["sanitizer_version"]) is None
    ):
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid screenshot receipt.",
            status=502,
            retryable=True,
        )
    for field, maximum in (
        ("sanitized_bytes", SUPPORT_SCREENSHOT_LOCAL_MAX_BYTES),
        ("width", SUPPORT_SCREENSHOT_LOCAL_MAX_DIMENSION),
        ("height", SUPPORT_SCREENSHOT_LOCAL_MAX_DIMENSION),
    ):
        candidate = projected[field]
        if (
            not isinstance(candidate, int)
            or isinstance(candidate, bool)
            or candidate <= 0
            or candidate > maximum
        ):
            raise SupportReportError(
                "invalid_service_response",
                "The private support service returned an invalid screenshot receipt.",
                status=502,
                retryable=True,
            )
    if (
        int(projected["width"]) != int(screenshot["width"])
        or int(projected["height"]) != int(screenshot["height"])
        or int(projected["width"]) * int(projected["height"])
        > SUPPORT_SCREENSHOT_LOCAL_MAX_PIXELS
    ):
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid screenshot receipt.",
            status=502,
            retryable=True,
        )
    _timestamp(projected["created_at"], field="screenshot.created_at")
    return projected


def _validate_received(record: Mapping[str, Any], response: Mapping[str, Any]) -> tuple[str, str]:
    if (
        response.get("schema_version") != SUPPORT_REPORT_SCHEMA_VERSION
        or response.get("client_report_id") != record["client_report_id"]
        or response.get("status") != "received"
        or response.get("content_sha256") != record["content_sha256"]
    ):
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid receipt.",
            status=502,
            retryable=True,
        )
    receipt_id = response.get("receipt_id")
    expires_at = response.get("expires_at")
    if (
        not isinstance(receipt_id, str)
        or _RECEIPT_ID_RE.fullmatch(receipt_id) is None
    ):
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid receipt.",
            status=502,
            retryable=True,
        )
    return receipt_id, _timestamp(expires_at, field="expires_at")


def _validate_deleted(record: Mapping[str, Any], response: Mapping[str, Any]) -> None:
    if (
        response.get("schema_version") != SUPPORT_REPORT_SCHEMA_VERSION
        or response.get("client_report_id") != record["client_report_id"]
        or response.get("status") != "deleted"
    ):
        raise SupportReportError(
            "invalid_service_response",
            "The private support service returned an invalid deletion receipt.",
            status=502,
            retryable=True,
            ambiguous=True,
        )


def _is_deleted_response(record: Mapping[str, Any], response: Mapping[str, Any]) -> bool:
    return (
        response.get("schema_version") == SUPPORT_REPORT_SCHEMA_VERSION
        and response.get("client_report_id") == record["client_report_id"]
        and response.get("status") == "deleted"
    )


def _retain_expired_capability(record: dict[str, Any]) -> dict[str, Any]:
    record.update({
        "state": "ambiguous",
        "report_markdown": None,
        "plaintext_expired": True,
        "message_code": "local_pending_expired",
        "updated_at": _utc_now(),
        "next_attempt_at": _next_retry_at(record),
    })
    _atomic_write_record(record)
    return record


def _reconcile_expired_pending(record: dict[str, Any]) -> dict[str, Any] | None:
    """Delete a possibly accepted remote report while retaining its capability."""

    now = _utc_now()
    record.update({
        "state": "ambiguous",
        "attempt_count": int(record["attempt_count"]) + 1,
        "next_attempt_at": now,
        "updated_at": now,
        "report_markdown": None,
        "plaintext_expired": True,
        "message_code": "local_pending_expired",
    })
    # The plaintext is durably removed before any expiry-time network call.
    _atomic_write_record(record)
    try:
        status, receipt = _request_json(
            _lookup_url(record),
            manage_secret=str(record["manage_secret"]),
            timeout=15,
        )
        _require_success_status(status, {200})
        if _is_deleted_response(record, receipt):
            _unlink_local_record(str(record["client_report_id"]))
            return None
        _validate_received(record, receipt)
    except SupportReportError as exc:
        if exc.status == 404 and exc.code == "report_not_found":
            _unlink_local_record(str(record["client_report_id"]))
            return None
        return _retain_expired_capability(record)

    try:
        status, deleted = _request_json(
            _lookup_url(record),
            method="DELETE",
            manage_secret=str(record["manage_secret"]),
            timeout=15,
        )
        _require_success_status(status, {200}, mutation=True)
        _validate_deleted(record, deleted)
    except SupportReportError as exc:
        if exc.status == 404 and exc.code == "report_not_found":
            _unlink_local_record(str(record["client_report_id"]))
            return None
        return _retain_expired_capability(record)
    _unlink_local_record(str(record["client_report_id"]))
    return None


def _mark_accepted(record: dict[str, Any], response: Mapping[str, Any]) -> dict[str, Any]:
    receipt_id, expires_at = _validate_received(record, response)
    screenshot_receipt = _screenshot_receipt(record, response)
    record.update({
        "state": "accepted",
        "updated_at": _utc_now(),
        "next_attempt_at": None,
        "report_markdown": None,
        "plaintext_expired": False,
        "receipt_id": receipt_id,
        "expires_at": expires_at,
        "message_code": None,
    })
    if screenshot_receipt is not None and isinstance(record.get("screenshot"), dict):
        record["screenshot"].update({
            "state": "accepted",
            "next_attempt_at": None,
            "message_code": None,
            "receipt": screenshot_receipt,
        })
    if isinstance(record.get("screenshot"), dict):
        local_expiry = _parse_timestamp(
            record["screenshot"]["local_expires_at"],
            field="screenshot.local_expires_at",
        )
        remote_expiry = _parse_timestamp(expires_at, field="expires_at")
        record["screenshot"]["local_expires_at"] = _format_timestamp(
            min(local_expiry, remote_expiry)
        )
    _atomic_write_record(record)
    if screenshot_receipt is not None:
        _unlink_local_screenshot(str(record["client_report_id"]))
    return record


def _submit(record: dict[str, Any]) -> dict[str, Any]:
    if record.get("plaintext_expired") is True:
        raise SupportReportError(
            "outbox_corrupt",
            "An expired plaintext report cannot be submitted again.",
            status=500,
        )
    now = _utc_now()
    record.update({
        "state": "submitting",
        "attempt_count": int(record["attempt_count"]) + 1,
        "next_attempt_at": now,
        "updated_at": now,
        "message_code": None,
    })
    _atomic_write_record(record)
    payload = {
        "schema_version": SUPPORT_REPORT_SCHEMA_VERSION,
        "client_report_id": record["client_report_id"],
        "content_sha256": record["content_sha256"],
        "report_markdown": record["report_markdown"],
        "accepted_terms_version": record["accepted_terms_version"],
        "accepted_retention_policy_version": record["accepted_retention_policy_version"],
    }
    if isinstance(record.get("screenshot"), Mapping):
        payload["screenshot_source_sha256"] = record["screenshot"]["source_sha256"]
    try:
        status, response = _request_json(
            str(record["reports_url"]),
            method="POST",
            payload=payload,
            manage_secret=str(record["manage_secret"]),
        )
        _require_success_status(status, {200, 201}, mutation=True)
        return _mark_accepted(record, response)
    except SupportReportError as exc:
        if exc.ambiguous or exc.code == "invalid_service_response":
            state = "ambiguous"
        elif exc.retryable:
            state = "queued"
        else:
            state = "rejected"
        record.update({
            "state": state,
            "updated_at": _utc_now(),
            "message_code": exc.code,
        })
        if state == "rejected":
            record["report_markdown"] = None
            record["next_attempt_at"] = None
            if isinstance(record.get("screenshot"), dict):
                record["screenshot"].update({
                    "state": "rejected",
                    "next_attempt_at": None,
                    "message_code": exc.code,
                })
        else:
            record["next_attempt_at"] = _next_retry_at(record)
        _atomic_write_record(record)
        if state == "rejected" and isinstance(record.get("screenshot"), dict):
            _unlink_local_screenshot(str(record["client_report_id"]))
        return record


def _lookup(record: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    now = _utc_now()
    record.update({
        "attempt_count": int(record["attempt_count"]) + 1,
        "next_attempt_at": now,
        "updated_at": now,
    })
    _atomic_write_record(record)
    try:
        status, response = _request_json(
            _lookup_url(record),
            manage_secret=str(record["manage_secret"]),
            timeout=15,
        )
        _require_success_status(status, {200})
    except SupportReportError as exc:
        if exc.status == 404 and exc.code == "report_not_found":
            return False, record
        if not exc.retryable:
            record.update({
                "state": "rejected",
                "updated_at": _utc_now(),
                "message_code": exc.code,
                "report_markdown": None,
                "next_attempt_at": None,
            })
            _atomic_write_record(record)
            return True, record
        record.update({
            "state": "ambiguous",
            "updated_at": _utc_now(),
            "message_code": exc.code,
            "next_attempt_at": _next_retry_at(record),
        })
        _atomic_write_record(record)
        return True, record
    if _is_deleted_response(record, response):
        _unlink_local_record(str(record["client_report_id"]))
        raise SupportReportError(
            "report_not_found", "Support report not found.", status=404
        )
    return True, _mark_accepted(record, response)


def _screenshot_pending(record: Mapping[str, Any]) -> bool:
    screenshot = record.get("screenshot")
    return (
        record.get("state") == "accepted"
        and isinstance(screenshot, Mapping)
        and screenshot.get("state") in {"queued", "submitting", "ambiguous"}
    )


def _screenshot_expired(
    record: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    screenshot = record.get("screenshot")
    if not _screenshot_pending(record) and not (
        isinstance(screenshot, Mapping)
        and screenshot.get("state") in {"queued", "submitting", "ambiguous"}
    ):
        return False
    assert isinstance(screenshot, Mapping)
    expires_at = _parse_timestamp(
        screenshot.get("local_expires_at"),
        field="screenshot.local_expires_at",
    )
    return (now or _now_utc()) >= expires_at


def _expire_screenshot_locked(
    record: dict[str, Any],
    *,
    attempt_remote: bool,
) -> dict[str, Any] | None:
    """Forget expired PNG bytes first, then best-effort delete its parent."""

    screenshot = record.get("screenshot")
    if not isinstance(screenshot, dict) or screenshot.get("state") not in {
        "queued", "submitting", "ambiguous"
    }:
        return record
    screenshot.update({
        "state": "rejected",
        "next_attempt_at": None,
        "message_code": "local_screenshot_expired",
        "receipt": None,
    })
    remote_may_exist = not (
        record.get("state") == "queued" and int(record.get("attempt_count", 0)) == 0
    )
    if remote_may_exist:
        expired_at = _utc_now()
        record.update({
            "state": "ambiguous",
            "report_markdown": None,
            "plaintext_expired": True,
            "receipt_id": None,
            "expires_at": None,
            "message_code": "local_pending_expired",
            "updated_at": expired_at,
            "next_attempt_at": expired_at,
        })
    _atomic_write_record(record)
    _unlink_local_screenshot(str(record["client_report_id"]))
    if not remote_may_exist:
        _unlink_local_record(str(record["client_report_id"]))
        return None
    if not attempt_remote:
        return record
    return _reconcile_expired_pending(record)


def _screenshot_attempt_due(record: Mapping[str, Any]) -> bool:
    screenshot = record.get("screenshot")
    if not isinstance(screenshot, Mapping) or not _screenshot_pending(record):
        return False
    next_attempt = _parse_timestamp(
        screenshot.get("next_attempt_at"),
        field="screenshot.next_attempt_at",
    )
    return _now_utc() >= next_attempt


def _next_screenshot_retry_at(screenshot: Mapping[str, Any]) -> str:
    attempts = max(1, int(screenshot.get("attempt_count", 1)))
    exponent = min(attempts - 1, 20)
    bounded = min(
        SUPPORT_RETRY_MAX_SECONDS,
        SUPPORT_RETRY_BASE_SECONDS * (2 ** exponent),
    )
    jittered = max(1.0, bounded * random.uniform(0.8, 1.2))
    return _format_timestamp(_now_utc() + timedelta(seconds=jittered))


def _mark_screenshot_failure(
    record: dict[str, Any],
    exc: SupportReportError,
) -> dict[str, Any]:
    screenshot = record.get("screenshot")
    if not isinstance(screenshot, dict):
        return record
    if exc.ambiguous or exc.code == "invalid_service_response":
        state = "ambiguous"
    elif exc.retryable:
        state = "queued"
    else:
        state = "rejected"
    screenshot.update({
        "state": state,
        "message_code": exc.code,
        "next_attempt_at": (
            None if state == "rejected" else _next_screenshot_retry_at(screenshot)
        ),
    })
    _atomic_write_record(record)
    if state == "rejected":
        _unlink_local_screenshot(str(record["client_report_id"]))
    return record


def _deliver_screenshot(
    record: dict[str, Any],
    *,
    respect_schedule: bool,
) -> dict[str, Any]:
    screenshot = record.get("screenshot")
    if not isinstance(screenshot, dict) or screenshot["state"] in {"accepted", "rejected"}:
        return record
    if respect_schedule and not _screenshot_attempt_due(record):
        return record

    if screenshot["state"] in {"submitting", "ambiguous"}:
        screenshot["attempt_count"] = int(screenshot["attempt_count"]) + 1
        screenshot["next_attempt_at"] = _utc_now()
        _atomic_write_record(record)
        try:
            status, response = _request_json(
                _lookup_url(record),
                manage_secret=str(record["manage_secret"]),
                timeout=15,
            )
            _require_success_status(status, {200})
            _validate_received(record, response)
            screenshot_receipt = _screenshot_receipt(record, response)
            if screenshot_receipt is not None:
                screenshot.update({
                    "state": "accepted",
                    "next_attempt_at": None,
                    "message_code": None,
                    "receipt": screenshot_receipt,
                })
                _atomic_write_record(record)
                _unlink_local_screenshot(str(record["client_report_id"]))
                return record
        except SupportReportError as exc:
            return _mark_screenshot_failure(record, exc)

    try:
        png = _read_screenshot(record)
    except SupportReportError as exc:
        screenshot.update({
            "state": "rejected",
            "next_attempt_at": None,
            "message_code": "invalid_screenshot",
        })
        _atomic_write_record(record)
        try:
            _unlink_local_screenshot(str(record["client_report_id"]))
        except SupportReportError:
            pass
        raise SupportReportError(
            "outbox_corrupt",
            "The private support screenshot is unreadable.",
            status=500,
        ) from exc

    screenshot.update({
        "state": "submitting",
        "attempt_count": int(screenshot["attempt_count"]) + 1,
        "next_attempt_at": _utc_now(),
        "message_code": None,
    })
    _atomic_write_record(record)
    try:
        status, response = _request_png(record, screenshot, png)
        _require_success_status(status, {200, 201}, mutation=True)
        _validate_received(record, response)
        screenshot_receipt = _screenshot_receipt(record, response)
        if screenshot_receipt is None:
            raise SupportReportError(
                "invalid_service_response",
                "The private support service returned no screenshot receipt.",
                status=502,
                retryable=True,
                ambiguous=True,
            )
        screenshot.update({
            "state": "accepted",
            "next_attempt_at": None,
            "message_code": None,
            "receipt": screenshot_receipt,
        })
        _atomic_write_record(record)
        _unlink_local_screenshot(str(record["client_report_id"]))
        return record
    except SupportReportError as exc:
        return _mark_screenshot_failure(record, exc)


def deliver_report(
    client_report_id: str,
    *,
    respect_schedule: bool = False,
) -> dict[str, Any]:
    """Deliver or reconcile one durable entry without ever changing its bytes."""

    identifier = _canonical_uuid4(client_report_id)
    with _report_lock(identifier):
        with support_outbox_egress_lock():
            record = load_report(identifier)
            if _screenshot_expired(record):
                expired = _expire_screenshot_locked(record, attempt_remote=True)
                if expired is None:
                    raise SupportReportError(
                        "report_not_found", "Support report not found.", status=404
                    )
                return expired
            screenshot = record.get("screenshot")
            if (
                record.get("plaintext_expired") is True
                and isinstance(screenshot, Mapping)
                and screenshot.get("message_code") == "local_screenshot_expired"
            ):
                # A process may have stopped after fsyncing the terminal
                # screenshot state but before unlinking the PNG.
                _unlink_local_screenshot(identifier)
                if respect_schedule and not _attempt_due(record):
                    return record
                reconciled = _reconcile_expired_pending(record)
                if reconciled is None:
                    raise SupportReportError(
                        "report_not_found", "Support report not found.", status=404
                    )
                return reconciled
            if record["state"] == "rejected":
                return record
            if record["state"] == "accepted":
                return _deliver_screenshot(record, respect_schedule=respect_schedule)
            if _pending_expired(record):
                if record["state"] == "queued" and record["attempt_count"] == 0:
                    _unlink_local_record(identifier)
                    raise SupportReportError(
                        "report_not_found", "Support report not found.", status=404
                    )
                if record.get("plaintext_expired") is not True:
                    record.update({
                        "state": "ambiguous",
                        "report_markdown": None,
                        "plaintext_expired": True,
                        "message_code": "local_pending_expired",
                        "updated_at": _utc_now(),
                    })
                    _atomic_write_record(record)
                if respect_schedule and not _attempt_due(record):
                    return record
                reconciled = _reconcile_expired_pending(record)
                if reconciled is None:
                    raise SupportReportError(
                        "report_not_found", "Support report not found.", status=404
                    )
                return reconciled
            if respect_schedule and not _attempt_due(record):
                return record
            if record["state"] in {"submitting", "ambiguous"}:
                resolved, record = _lookup(record)
                if resolved:
                    if record["state"] == "accepted":
                        return _deliver_screenshot(record, respect_schedule=respect_schedule)
                    return record
            record = _submit(record)
            if record["state"] == "accepted":
                return _deliver_screenshot(record, respect_schedule=respect_schedule)
            return record


def start_delivery(client_report_id: str) -> threading.Thread:
    """Start best-effort delivery; the outbox remains authoritative on exit."""

    def run() -> None:
        try:
            deliver_report(client_report_id)
        except SupportReportError:
            # The caller or next daemon startup can retry from the durable state.
            return

    thread = threading.Thread(
        target=run,
        daemon=True,
        name="support-report-delivery",
    )
    thread.start()
    return thread


def reconcile_report(client_report_id: str) -> dict[str, Any]:
    record = load_report(client_report_id)
    if _screenshot_expired(record):
        return deliver_report(client_report_id)
    if _pending_expired(record):
        removed = expire_pending_report(client_report_id, respect_schedule=False)
        if removed:
            raise SupportReportError(
                "report_not_found", "Support report not found.", status=404
            )
        return load_report(client_report_id)
    if record["state"] in {"submitting", "ambiguous"}:
        return deliver_report(client_report_id)
    if record["state"] == "queued" or _screenshot_pending(record):
        start_delivery(client_report_id)
    return record


def list_pending_report_ids() -> list[str]:
    directory = outbox_directory()
    if directory.is_symlink():
        raise SupportReportError(
            "outbox_unavailable",
            "The private support outbox is unavailable.",
            status=500,
        )
    if not directory.exists():
        return []
    cleanup_orphaned_screenshot_files()
    try:
        _require_private_mode(directory, 0o700)
    except (OSError, CredentialStoreError) as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "The private support outbox is unavailable.",
            status=500,
        ) from exc
    identifiers: list[str] = []
    scanned = 0
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned += 1
                if scanned > SUPPORT_REPORT_LIST_SCAN_LIMIT:
                    break
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    continue
                if not entry.name.endswith(".json"):
                    continue
                try:
                    identifier = _canonical_uuid4(entry.name[:-5])
                    record = load_report(identifier)
                    if record["state"] not in _PENDING_STATES and not _screenshot_pending(record):
                        continue
                    if _pending_expired(record):
                        if expire_pending_report(
                            identifier,
                            respect_schedule=True,
                            attempt_remote=False,
                        ):
                            continue
                    identifiers.append(identifier)
                except SupportReportError:
                    continue
    except OSError as exc:
        raise SupportReportError(
            "outbox_unavailable",
            "The private support outbox is unavailable.",
            status=500,
        ) from exc
    return sorted(identifiers)


def recover_pending_reports() -> None:
    try:
        identifiers = list_pending_report_ids()
    except SupportReportError:
        return
    for identifier in identifiers:
        try:
            deliver_report(identifier, respect_schedule=True)
        except SupportReportError:
            continue


def run_recovery_loop(
    stop_event: threading.Event,
    *,
    poll_seconds: float = SUPPORT_RECOVERY_POLL_SECONDS,
) -> None:
    """Retry due pending entries until daemon shutdown wakes the loop."""

    bounded_poll = max(0.1, min(float(poll_seconds), 60.0))
    while not stop_event.is_set():
        recover_pending_reports()
        if stop_event.wait(bounded_poll):
            break


def delete_report(client_report_id: str) -> dict[str, str]:
    identifier = _canonical_uuid4(client_report_id)
    with _report_lock(identifier):
        with support_outbox_egress_lock():
            record = load_report(identifier)
            if record["state"] in {"accepted", "submitting", "ambiguous"}:
                try:
                    status, response = _request_json(
                        _lookup_url(record),
                        method="DELETE",
                        manage_secret=str(record["manage_secret"]),
                        timeout=15,
                    )
                    _require_success_status(status, {200}, mutation=True)
                    _validate_deleted(record, response)
                except SupportReportError as exc:
                    # A 404 is indistinguishable by design and means no remote
                    # report is available under this capability.
                    if not (exc.status == 404 and exc.code == "report_not_found"):
                        raise SupportReportError(
                            "support_service_unavailable",
                            "Could not confirm deletion with the private support service.",
                            status=502,
                            retryable=True,
                        ) from exc
            _unlink_local_record(identifier)
    return {"client_report_id": identifier, "state": "deleted"}
