from __future__ import annotations

import base64
import hashlib
import json
import multiprocessing
import stat
import struct
import time
import zlib
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Event, Thread

import pytest

from clawjournal import config, support_reports


def _hold_support_lock(config_dir, acquired, release):
    support_reports.config_module.CONFIG_DIR = Path(config_dir)
    with support_reports.support_outbox_egress_lock():
        acquired.set()
        release.wait(10)


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "clawjournal-home")
    return tmp_path / "clawjournal-home" / support_reports.SUPPORT_REPORTS_DIRNAME


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
        terms_text="Private support report terms.",
        retention_text="Stored for 30 days.",
    )


def _enqueue(capability, markdown="# Exact\n\n用户正文 🐾"):
    return support_reports.enqueue_report(
        report_markdown=markdown,
        accepted_terms_version=capability.terms_version,
        accepted_retention_policy_version=capability.retention_policy_version,
        capability=capability,
    )


def _png() -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", 2, 1, 8, 6, 0, 0, 0)
    pixels = bytes((255, 0, 0, 255, 0, 0, 255, 255))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(b"\x00" + pixels))
        + chunk(b"IEND", b"")
    )


def _with_screenshots(capability):
    return support_reports.SupportCapability(
        **{
            **capability.__dict__,
            "screenshots": support_reports.SupportScreenshotCapability(
                upload_url=(
                    "https://support.example.test/api/support/v1/reports/"
                    "{client_report_id}/screenshot"
                ),
                max_input_bytes=2 * 1024 * 1024,
                max_output_bytes=2 * 1024 * 1024,
                max_width=4096,
                max_height=4096,
                max_pixels=4 * 1024 * 1024,
                sanitizer_version="png-rgb-v1",
            ),
        }
    )


def _enqueue_screenshot(capability, png: bytes | None = None):
    png = png or _png()
    return support_reports.enqueue_report(
        report_markdown="# Screenshot report",
        accepted_terms_version=capability.terms_version,
        accepted_retention_policy_version=capability.retention_policy_version,
        capability=capability,
        screenshot_png_base64=base64.b64encode(png).decode("ascii"),
        screenshot_source_sha256=hashlib.sha256(png).hexdigest(),
        screenshot_width=2,
        screenshot_height=1,
    )


def _receipt(record):
    return {
        "schema_version": 1,
        "client_report_id": record["client_report_id"],
        "receipt_id": "support_receipt_123",
        "status": "received",
        "created_at": "2026-08-16T00:00:00Z",
        "expires_at": "2026-09-15T00:00:00Z",
        "content_sha256": record["content_sha256"],
        "lookup_url": "/private",
        "delete_url": "/private",
        "idempotent_replay": False,
    }


def _screenshot_receipt(record):
    response = _receipt(record)
    response["screenshot"] = {
        "source_sha256": record["screenshot"]["source_sha256"],
        "sanitized_sha256": "a" * 64,
        "width": 2,
        "height": 1,
        "sanitized_bytes": 70,
        "sanitizer_version": "png-rgb-v1",
        "created_at": "2026-08-16T00:00:01Z",
    }
    return response


def test_screenshot_outbox_posts_intent_then_exact_png_and_minimizes_bytes(
    outbox, capability, monkeypatch
):
    capability = _with_screenshots(capability)
    png = _png()
    record = _enqueue_screenshot(capability, png)
    stored_path = outbox / f"{record['client_report_id']}.png"
    assert stored_path.read_bytes() == png
    assert record["outbox_schema_version"] == 2
    assert record["screenshot"]["state"] == "queued"
    assert support_reports.public_status(record)["screenshot"]["source_sha256"] == hashlib.sha256(png).hexdigest()
    assert base64.b64encode(png).decode("ascii") not in json.dumps(
        support_reports.public_status(record)
    )

    post_calls = []
    put_calls = []

    def request(url, **kwargs):
        post_calls.append((url, kwargs))
        return 201, {**_receipt(record), "screenshot": None}

    def put(current_record, screenshot, exact_png, **kwargs):
        put_calls.append((current_record, screenshot, exact_png, kwargs))
        return 201, _screenshot_receipt(current_record)

    monkeypatch.setattr(support_reports, "_request_json", request)
    monkeypatch.setattr(support_reports, "_request_png", put)
    accepted = support_reports.deliver_report(record["client_report_id"])

    assert accepted["state"] == "accepted"
    assert accepted["screenshot"]["state"] == "accepted"
    assert accepted["screenshot"]["receipt"]["source_sha256"] == hashlib.sha256(png).hexdigest()
    assert not stored_path.exists()
    assert post_calls[0][1]["payload"]["screenshot_source_sha256"] == hashlib.sha256(png).hexdigest()
    assert put_calls[0][2] == png
    assert "manage_secret" not in json.dumps(support_reports.public_status(accepted))


def test_ambiguous_screenshot_put_recovers_by_receipt_hash_after_restart(
    outbox, capability, monkeypatch
):
    capability = _with_screenshots(capability)
    png = _png()
    record = _enqueue_screenshot(capability, png)
    identifier = record["client_report_id"]
    requests = []

    def request(url, **kwargs):
        requests.append((url, kwargs))
        if kwargs.get("method") == "POST":
            return 201, {**_receipt(record), "screenshot": None}
        return 200, _screenshot_receipt(support_reports.load_report(identifier))

    put_count = 0

    def ambiguous_put(*_args, **_kwargs):
        nonlocal put_count
        put_count += 1
        raise support_reports.SupportReportError(
            "support_service_unavailable",
            "temporary",
            status=502,
            retryable=True,
            ambiguous=True,
        )

    monkeypatch.setattr(support_reports, "_request_json", request)
    monkeypatch.setattr(support_reports, "_request_png", ambiguous_put)
    uncertain = support_reports.deliver_report(identifier)
    assert uncertain["state"] == "accepted"
    assert uncertain["screenshot"]["state"] == "ambiguous"
    assert (outbox / f"{identifier}.png").read_bytes() == png
    assert identifier in support_reports.list_pending_report_ids()

    # Simulate the next daemon process after the scheduled retry becomes due.
    monkeypatch.setattr(support_reports, "_screenshot_attempt_due", lambda _record: True)
    support_reports.recover_pending_reports()
    recovered = support_reports.load_report(identifier)
    assert recovered["screenshot"]["state"] == "accepted"
    assert put_count == 1
    assert not (outbox / f"{identifier}.png").exists()
    assert any(call[1].get("method") is None for call in requests[1:])


def test_screenshot_requires_open_capability_and_delete_removes_exact_local_file(
    outbox, capability
):
    png = _png()
    with pytest.raises(support_reports.SupportReportError) as error:
        _enqueue_screenshot(capability, png)
    assert error.value.code == "support_screenshots_closed"
    assert not outbox.exists()

    capability = _with_screenshots(capability)
    record = _enqueue_screenshot(capability, png)
    assert (outbox / f"{record['client_report_id']}.png").exists()
    deleted = support_reports.delete_report(record["client_report_id"])
    assert deleted["state"] == "deleted"
    assert not list(outbox.glob(f"{record['client_report_id']}.*"))


@pytest.mark.parametrize(
    ("encoded", "digest", "width", "expected_code"),
    [
        ("not base64", "0" * 64, 2, "invalid_screenshot"),
        (base64.b64encode(_png()).decode("ascii"), "0" * 64, 2, "screenshot_hash_mismatch"),
        (base64.b64encode(_png()).decode("ascii"), hashlib.sha256(_png()).hexdigest(), 3, "invalid_screenshot"),
    ],
)
def test_screenshot_local_validation_fails_before_outbox(
    outbox, capability, encoded, digest, width, expected_code
):
    capability = _with_screenshots(capability)
    with pytest.raises(support_reports.SupportReportError) as error:
        support_reports.enqueue_report(
            report_markdown="# report",
            accepted_terms_version=capability.terms_version,
            accepted_retention_policy_version=capability.retention_policy_version,
            capability=capability,
            screenshot_png_base64=encoded,
            screenshot_source_sha256=digest,
            screenshot_width=width,
            screenshot_height=1,
        )
    assert error.value.code == expected_code
    assert not outbox.exists()


def test_queued_screenshot_ttl_removes_png_and_never_attempts_egress(
    outbox, capability, monkeypatch
):
    capability = _with_screenshots(capability)
    record = _enqueue_screenshot(capability)
    identifier = record["client_report_id"]
    record["screenshot"]["local_expires_at"] = "2026-08-15T00:00:00Z"
    support_reports._atomic_write_record(record)
    monkeypatch.setattr(
        support_reports,
        "_request_json",
        lambda *_args, **_kwargs: pytest.fail("expired queued bytes must not egress"),
    )
    monkeypatch.setattr(
        support_reports,
        "_request_png",
        lambda *_args, **_kwargs: pytest.fail("expired queued bytes must not egress"),
    )

    with pytest.raises(support_reports.SupportReportError) as error:
        support_reports.deliver_report(identifier)
    assert error.value.code == "report_not_found"
    assert not list(outbox.glob(f"{identifier}.*"))


def test_accepted_parent_ambiguous_screenshot_ttl_forgets_png_then_recovers_delete(
    outbox, capability, monkeypatch
):
    capability = _with_screenshots(capability)
    record = _enqueue_screenshot(capability)
    identifier = record["client_report_id"]

    def post(url, **kwargs):
        assert kwargs.get("method") == "POST"
        return 201, {**_receipt(record), "screenshot": None}

    monkeypatch.setattr(support_reports, "_request_json", post)
    monkeypatch.setattr(
        support_reports,
        "_request_png",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            support_reports.SupportReportError(
                "support_service_unavailable",
                "temporary",
                status=502,
                retryable=True,
                ambiguous=True,
            )
        ),
    )
    uncertain = support_reports.deliver_report(identifier)
    assert uncertain["state"] == "accepted"
    assert uncertain["screenshot"]["state"] == "ambiguous"
    uncertain["screenshot"]["local_expires_at"] = "2026-08-15T00:00:00Z"
    support_reports._atomic_write_record(uncertain)

    monkeypatch.setattr(
        support_reports,
        "_request_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            support_reports.SupportReportError(
                "support_service_unavailable",
                "temporary",
                status=502,
                retryable=True,
                ambiguous=False,
            )
        ),
    )
    retained = support_reports.deliver_report(identifier)
    assert retained["state"] == "ambiguous"
    assert retained["plaintext_expired"] is True
    assert retained["screenshot"]["state"] == "rejected"
    assert retained["screenshot"]["message_code"] == "local_screenshot_expired"
    assert retained["manage_secret"] == record["manage_secret"]
    assert not (outbox / f"{identifier}.png").exists()
    assert support_reports.load_report(identifier)["screenshot"]["state"] == "rejected"

    calls = []

    def delete_remote(url, **kwargs):
        calls.append(kwargs)
        if kwargs.get("method") == "DELETE":
            return 200, {
                "schema_version": 1,
                "client_report_id": identifier,
                "status": "deleted",
            }
        return 200, _receipt(retained)

    monkeypatch.setattr(support_reports, "_request_json", delete_remote)
    with pytest.raises(support_reports.SupportReportError) as error:
        support_reports.deliver_report(identifier)
    assert error.value.code == "report_not_found"
    assert [call.get("method") for call in calls] == [None, "DELETE"]
    assert all(call["manage_secret"] == record["manage_secret"] for call in calls)
    assert not list(outbox.glob(f"{identifier}.*"))


def test_expiry_crash_point_record_reloads_and_restart_removes_png(
    outbox, capability, monkeypatch
):
    capability = _with_screenshots(capability)
    record = _enqueue_screenshot(capability)
    identifier = record["client_report_id"]
    record.update({
        "state": "accepted",
        "attempt_count": 1,
        "next_attempt_at": None,
        "report_markdown": None,
        "receipt_id": "support_receipt_123",
        "expires_at": "2026-09-15T00:00:00Z",
    })
    record["screenshot"]["state"] = "ambiguous"
    record["screenshot"]["attempt_count"] = 1
    record["screenshot"]["local_expires_at"] = "2026-08-15T00:00:00Z"
    support_reports._atomic_write_record(record)
    original_unlink = support_reports._unlink_local_screenshot
    monkeypatch.setattr(
        support_reports,
        "_unlink_local_screenshot",
        lambda _identifier: (_ for _ in ()).throw(SystemExit("crash after record fsync")),
    )
    with pytest.raises(SystemExit):
        support_reports._expire_screenshot_locked(record, attempt_remote=False)

    persisted = support_reports.load_report(identifier)
    assert persisted["state"] == "ambiguous"
    assert persisted["plaintext_expired"] is True
    assert persisted["message_code"] == "local_pending_expired"
    assert persisted["screenshot"]["message_code"] == "local_screenshot_expired"
    assert (outbox / f"{identifier}.png").exists()

    monkeypatch.setattr(support_reports, "_unlink_local_screenshot", original_unlink)
    monkeypatch.setattr(
        support_reports,
        "_request_json",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            support_reports.SupportReportError(
                "support_service_unavailable",
                "temporary",
                status=502,
                retryable=True,
            )
        ),
    )
    recovered = support_reports.deliver_report(identifier)
    assert recovered["manage_secret"] == record["manage_secret"]
    assert not (outbox / f"{identifier}.png").exists()
    assert support_reports.load_report(identifier)["state"] == "ambiguous"


def test_bounded_orphan_cleanup_respects_grace_and_valid_pending_record(
    outbox, capability
):
    capability = _with_screenshots(capability)
    valid = _enqueue_screenshot(capability)
    valid_png = outbox / f"{valid['client_report_id']}.png"
    old_epoch = time.time() - 10_000
    support_reports.os.utime(valid_png, (old_epoch, old_epoch))

    orphan_id = "00000000-0000-4000-8000-000000000099"
    orphan = outbox / f"{orphan_id}.png"
    orphan.write_bytes(_png())
    temporary = outbox / ".support-screenshot-crash.tmp"
    temporary.write_bytes(b"private partial bytes")
    recent = outbox / "00000000-0000-4000-8000-000000000098.png"
    recent.write_bytes(_png())
    for path in (orphan, temporary):
        support_reports.os.utime(path, (old_epoch, old_epoch))

    removed = support_reports.cleanup_orphaned_screenshot_files(
        now_epoch=time.time(),
        grace_seconds=60,
    )
    assert removed == 2
    assert valid_png.exists()
    assert recent.exists()
    assert not orphan.exists()
    assert not temporary.exists()


def test_enqueue_persists_exact_markdown_and_private_secret(outbox, capability):
    markdown = "# Exact\n\n用户正文 🐾\n"
    record = _enqueue(capability, markdown)
    stored = support_reports.load_report(record["client_report_id"])

    assert stored["report_markdown"] == markdown
    assert stored["content_sha256"] == hashlib.sha256(markdown.encode("utf-8")).hexdigest()
    assert 43 <= len(stored["manage_secret"]) <= 128
    public = support_reports.public_status(stored)
    assert "manage_secret" not in public
    assert "report_markdown" not in public
    serialized_public = json.dumps(public)
    assert markdown not in serialized_public
    if stat.S_IMODE(outbox.stat().st_mode):
        # Windows stat does not expose the effective ACL; the store itself
        # verifies a protected current-user-only ACL before returning.
        if support_reports.os.name != "nt":
            assert stat.S_IMODE(outbox.stat().st_mode) == 0o700
            assert stat.S_IMODE(next(outbox.glob("*.json")).stat().st_mode) == 0o600


def test_v1_text_only_outbox_records_remain_recoverable(outbox, capability):
    record = _enqueue(capability, "# legacy pending")
    path = outbox / f"{record['client_report_id']}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["outbox_schema_version"] = 1
    raw.pop("screenshot")
    path.write_text(json.dumps(raw), encoding="utf-8")
    if support_reports.os.name != "nt":
        path.chmod(0o600)

    loaded = support_reports.load_report(record["client_report_id"])
    assert loaded["outbox_schema_version"] == 2
    assert loaded["screenshot"] is None
    assert loaded["report_markdown"] == "# legacy pending"


def test_enqueue_enforces_utf8_byte_limit_not_character_count(outbox, capability):
    tight = support_reports.SupportCapability(
        **{**capability.__dict__, "max_report_bytes": 4}
    )
    with pytest.raises(support_reports.SupportReportError) as error:
        _enqueue(tight, "🐾🐾")
    assert error.value.code == "report_too_large"
    assert not outbox.exists()


@pytest.mark.parametrize("markdown", ["text\x00secret", "\ud800"])
def test_enqueue_accepts_only_valid_plain_unicode_text(outbox, capability, markdown):
    with pytest.raises(support_reports.SupportReportError) as error:
        _enqueue(capability, markdown)
    assert error.value.code == "invalid_request"
    assert not outbox.exists()


def test_delivery_posts_exact_bytes_then_minimizes_accepted_record(
    outbox, capability, monkeypatch
):
    markdown = "# Edited report\n\nOnly this text leaves."
    record = _enqueue(capability, markdown)
    calls = []

    def request(url, **kwargs):
        calls.append((url, kwargs))
        return 201, _receipt(record)

    monkeypatch.setattr(support_reports, "_request_json", request)
    accepted = support_reports.deliver_report(record["client_report_id"])

    assert accepted["state"] == "accepted"
    assert accepted["report_markdown"] is None
    assert accepted["receipt_id"] == "support_receipt_123"
    assert len(calls) == 1
    url, request_args = calls[0]
    assert url == capability.reports_url
    assert request_args["method"] == "POST"
    assert request_args["manage_secret"] == record["manage_secret"]
    payload = request_args["payload"]
    assert payload["report_markdown"] == markdown
    assert payload["content_sha256"] == hashlib.sha256(markdown.encode()).hexdigest()
    disk_text = next(outbox.glob("*.json")).read_text(encoding="utf-8")
    assert markdown not in disk_text


def test_ambiguous_post_recovers_by_lookup_without_duplicate_post(
    outbox, capability, monkeypatch
):
    record = _enqueue(capability)
    calls = []

    def ambiguous(url, **kwargs):
        calls.append((url, kwargs.get("method", "GET")))
        raise support_reports.SupportReportError(
            "support_service_unavailable",
            "safe",
            status=502,
            retryable=True,
            ambiguous=True,
        )

    monkeypatch.setattr(support_reports, "_request_json", ambiguous)
    first = support_reports.deliver_report(record["client_report_id"])
    assert first["state"] == "ambiguous"

    def receipt_lookup(url, **kwargs):
        calls.append((url, kwargs.get("method", "GET")))
        return 200, _receipt(record)

    monkeypatch.setattr(support_reports, "_request_json", receipt_lookup)
    recovered = support_reports.deliver_report(record["client_report_id"])
    assert recovered["state"] == "accepted"
    assert [method for _url, method in calls] == ["POST", "GET"]


def test_submitting_after_crash_looks_up_then_retries_same_payload(
    outbox, capability, monkeypatch
):
    markdown = "same bytes"
    original = _enqueue(capability, markdown)
    interrupted = dict(original, state="submitting")
    support_reports._atomic_write_record(interrupted)
    calls = []

    def lookup_then_submit(url, **kwargs):
        method = kwargs.get("method", "GET")
        calls.append((method, kwargs.get("payload")))
        if method == "GET":
            raise support_reports.SupportReportError(
                "report_not_found", "not found", status=404
            )
        return 200, _receipt(original)

    monkeypatch.setattr(support_reports, "_request_json", lookup_then_submit)
    accepted = support_reports.deliver_report(original["client_report_id"])

    assert accepted["state"] == "accepted"
    assert [method for method, _payload in calls] == ["GET", "POST"]
    assert calls[1][1]["report_markdown"] == markdown
    assert calls[1][1]["client_report_id"] == original["client_report_id"]


@pytest.mark.parametrize(
    ("code", "status"),
    [("request_rejected", 404), ("credential_invalid", 403)],
)
def test_lookup_other_4xx_never_resubmits(
    outbox, capability, monkeypatch, code, status
):
    original = _enqueue(capability, "must not be resent")
    support_reports._atomic_write_record(dict(original, state="ambiguous"))
    calls = []

    def reject_lookup(url, **kwargs):
        calls.append(kwargs.get("method", "GET"))
        raise support_reports.SupportReportError(code, "safe", status=status)

    monkeypatch.setattr(support_reports, "_request_json", reject_lookup)
    result = support_reports.deliver_report(original["client_report_id"])

    assert calls == ["GET"]
    assert result["state"] == "rejected"
    assert result["report_markdown"] is None


def test_lookup_deleted_receipt_removes_local_capability_without_post(
    outbox, capability, monkeypatch
):
    original = _enqueue(capability, "already deleted remotely")
    support_reports._atomic_write_record(dict(original, state="ambiguous"))
    calls = []

    def deleted(_url, **kwargs):
        calls.append(kwargs.get("method", "GET"))
        return 200, {
            "schema_version": 1,
            "client_report_id": original["client_report_id"],
            "receipt_id": "support_receipt_123",
            "status": "deleted",
            "created_at": "2026-08-16T00:00:00Z",
            "expires_at": "2026-09-15T00:00:00Z",
            "content_sha256": original["content_sha256"],
        }

    monkeypatch.setattr(support_reports, "_request_json", deleted)
    with pytest.raises(support_reports.SupportReportError) as error:
        support_reports.deliver_report(original["client_report_id"])
    assert error.value.code == "report_not_found"
    assert calls == ["GET"]
    assert not (outbox / f"{original['client_report_id']}.json").exists()


def test_permanent_rejection_is_terminal_and_clears_body(outbox, capability, monkeypatch):
    record = _enqueue(capability, "private body")

    def reject(*_args, **_kwargs):
        raise support_reports.SupportReportError(
            "idempotency_conflict", "safe", status=409
        )

    monkeypatch.setattr(support_reports, "_request_json", reject)
    rejected = support_reports.deliver_report(record["client_report_id"])
    assert rejected["state"] == "rejected"
    assert rejected["report_markdown"] is None
    assert "private body" not in next(outbox.glob("*.json")).read_text(encoding="utf-8")


def test_recover_pending_reports_uses_file_outbox_without_sqlite(
    outbox, capability, monkeypatch
):
    first = _enqueue(capability, "first")
    second = _enqueue(capability, "second")
    delivered = []

    monkeypatch.setattr(
        support_reports,
        "deliver_report",
        lambda identifier, **_kwargs: delivered.append(identifier),
    )
    support_reports.recover_pending_reports()
    assert delivered == sorted([first["client_report_id"], second["client_report_id"]])


def test_retry_schedule_is_exponential_bounded_and_due_time_controlled(
    outbox, capability, monkeypatch
):
    clock = [datetime(2026, 8, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(support_reports, "_now_utc", lambda: clock[0])
    monkeypatch.setattr(support_reports.random, "uniform", lambda _a, _b: 1.0)
    record = _enqueue(capability, "retry me")
    calls = []

    def unavailable(*_args, **_kwargs):
        calls.append(clock[0])
        raise support_reports.SupportReportError(
            "rate_limited", "safe", status=429, retryable=True
        )

    monkeypatch.setattr(support_reports, "_request_json", unavailable)
    first = support_reports.deliver_report(record["client_report_id"])
    assert first["state"] == "queued"
    assert first["attempt_count"] == 1
    assert support_reports._parse_timestamp(
        first["next_attempt_at"], field="next_attempt_at"
    ) == clock[0] + timedelta(seconds=5)

    clock[0] += timedelta(seconds=4)
    support_reports.recover_pending_reports()
    assert len(calls) == 1

    clock[0] += timedelta(seconds=1)
    support_reports.recover_pending_reports()
    second = support_reports.load_report(record["client_report_id"])
    assert len(calls) == 2
    assert second["attempt_count"] == 2
    assert support_reports._parse_timestamp(
        second["next_attempt_at"], field="next_attempt_at"
    ) == clock[0] + timedelta(seconds=10)


def test_recovery_loop_retries_without_user_status_request(monkeypatch):
    stop = Event()
    calls = []

    def recover():
        calls.append(True)
        if len(calls) == 2:
            stop.set()

    monkeypatch.setattr(support_reports, "recover_pending_reports", recover)
    support_reports.run_recovery_loop(stop, poll_seconds=0.01)
    assert len(calls) == 2


def test_never_submitted_plaintext_expires_locally_without_network(
    outbox, capability, monkeypatch
):
    clock = [datetime(2026, 7, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(support_reports, "_now_utc", lambda: clock[0])
    record = _enqueue(capability, "local-only pending text")
    clock[0] += timedelta(days=support_reports.SUPPORT_PENDING_RETENTION_DAYS)
    monkeypatch.setattr(
        support_reports,
        "_request_json",
        lambda *_args, **_kwargs: pytest.fail("listing attempted remote I/O"),
    )

    assert support_reports.list_public_reports() == {
        "reports": [],
        "truncated": False,
    }
    assert not (outbox / f"{record['client_report_id']}.json").exists()


def test_day29_lost_post_day30_clears_plaintext_then_lookup_deletes_remote(
    outbox, capability, monkeypatch
):
    clock = [datetime(2026, 7, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(support_reports, "_now_utc", lambda: clock[0])
    monkeypatch.setattr(support_reports.random, "uniform", lambda _a, _b: 1.0)
    record = _enqueue(capability, "must expire locally")
    clock[0] += timedelta(days=29)

    def lost_post(*_args, **_kwargs):
        raise support_reports.SupportReportError(
            "support_service_unavailable",
            "safe",
            status=502,
            retryable=True,
            ambiguous=True,
        )

    monkeypatch.setattr(support_reports, "_request_json", lost_post)
    ambiguous = support_reports.deliver_report(record["client_report_id"])
    assert ambiguous["state"] == "ambiguous"
    assert ambiguous["report_markdown"] == "must expire locally"

    clock[0] += timedelta(days=1)
    calls = []

    def reconcile(url, **kwargs):
        method = kwargs.get("method", "GET")
        calls.append(method)
        on_disk = support_reports.load_report(record["client_report_id"])
        assert on_disk["report_markdown"] is None
        assert on_disk["plaintext_expired"] is True
        if method == "GET":
            return 200, _receipt(record)
        return 200, {
            "schema_version": 1,
            "client_report_id": record["client_report_id"],
            "receipt_id": "support_receipt_123",
            "status": "deleted",
            "deleted_at": "2026-08-01T00:00:00Z",
        }

    monkeypatch.setattr(support_reports, "_request_json", reconcile)
    support_reports.recover_pending_reports()
    assert calls == ["GET", "DELETE"]
    assert not (outbox / f"{record['client_report_id']}.json").exists()


def test_expired_ambiguous_retains_capability_when_remote_is_unavailable(
    outbox, capability, monkeypatch
):
    clock = [datetime(2026, 7, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(support_reports, "_now_utc", lambda: clock[0])
    monkeypatch.setattr(support_reports.random, "uniform", lambda _a, _b: 1.0)
    record = _enqueue(capability, "clear but retain capability")
    support_reports._atomic_write_record(dict(
        record,
        state="ambiguous",
        attempt_count=1,
    ))
    clock[0] += timedelta(days=30)

    def unavailable(*_args, **_kwargs):
        raise support_reports.SupportReportError(
            "support_service_unavailable", "safe", status=502, retryable=True
        )

    monkeypatch.setattr(support_reports, "_request_json", unavailable)
    support_reports.recover_pending_reports()
    retained = support_reports.load_report(record["client_report_id"])
    assert retained["report_markdown"] is None
    assert retained["plaintext_expired"] is True
    assert retained["manage_secret"] == record["manage_secret"]
    assert retained["state"] == "ambiguous"


def test_listing_expired_ambiguous_is_local_only_and_clears_plaintext(
    outbox, capability, monkeypatch
):
    clock = [datetime(2026, 7, 1, tzinfo=timezone.utc)]
    monkeypatch.setattr(support_reports, "_now_utc", lambda: clock[0])
    record = _enqueue(capability, "listing must clear this")
    support_reports._atomic_write_record(dict(
        record,
        state="ambiguous",
        attempt_count=1,
    ))
    clock[0] += timedelta(days=30)
    monkeypatch.setattr(
        support_reports,
        "_request_json",
        lambda *_args, **_kwargs: pytest.fail("listing attempted remote I/O"),
    )

    listing = support_reports.list_public_reports()
    assert len(listing["reports"]) == 1
    retained = support_reports.load_report(record["client_report_id"])
    assert retained["report_markdown"] is None
    assert retained["plaintext_expired"] is True
    assert retained["manage_secret"] == record["manage_secret"]


def test_public_listing_is_newest_first_and_never_exposes_private_fields(
    outbox, capability, monkeypatch
):
    timestamps = iter([
        "2026-08-16T00:00:00.000001Z",
        "2026-08-16T00:00:00.000002Z",
    ])
    monkeypatch.setattr(support_reports, "_utc_now", lambda: next(timestamps))
    first = _enqueue(capability, "first private body")
    second = _enqueue(capability, "second private body")

    result = support_reports.list_public_reports()

    assert result["truncated"] is False
    assert [item["client_report_id"] for item in result["reports"]] == [
        second["client_report_id"],
        first["client_report_id"],
    ]
    serialized = json.dumps(result)
    for forbidden in (
        "first private body",
        "second private body",
        first["manage_secret"],
        capability.reports_url,
        capability.report_lookup_url,
    ):
        assert forbidden not in serialized
    assert set(result["reports"][0]) == {
        "client_report_id",
        "state",
        "receipt_id",
        "message",
        "created_at",
            "expires_at",
            "screenshot",
        }


def test_public_listing_skips_corrupt_and_symlink_entries(
    outbox, capability
):
    valid = _enqueue(capability, "safe")
    corrupt = outbox / "00000000-0000-4000-8000-000000000000.json"
    corrupt.write_text("{not-json", encoding="utf-8")
    symlink = outbox / "00000000-0000-4000-8000-000000000001.json"
    try:
        symlink.symlink_to(next(outbox.glob(f"{valid['client_report_id']}.json")))
    except OSError:
        # Windows environments may deny symlink creation to an unprivileged
        # process; corrupt-entry coverage remains valid there.
        pass

    result = support_reports.list_public_reports()

    assert [item["client_report_id"] for item in result["reports"]] == [
        valid["client_report_id"]
    ]


def test_public_listing_fails_with_fixed_error_for_unsafe_directory(
    outbox, capability, monkeypatch
):
    _enqueue(capability, "safe")

    def reject_directory(*_args, **_kwargs):
        raise support_reports.CredentialStoreError("private path canary")

    monkeypatch.setattr(support_reports, "_require_private_mode", reject_directory)
    with pytest.raises(support_reports.SupportReportError) as error:
        support_reports.list_public_reports()
    assert error.value.code == "outbox_unavailable"
    assert error.value.message == "The private support outbox is unavailable."
    assert "canary" not in error.value.message


def test_delete_accepted_report_requires_remote_deletion_receipt(
    outbox, capability, monkeypatch
):
    record = _enqueue(capability)
    monkeypatch.setattr(
        support_reports,
        "_request_json",
        lambda *_args, **_kwargs: (201, _receipt(record)),
    )
    support_reports.deliver_report(record["client_report_id"])
    calls = []

    def delete(url, **kwargs):
        calls.append((url, kwargs))
        return 200, {
            "schema_version": 1,
            "client_report_id": record["client_report_id"],
            "receipt_id": "support_receipt_123",
            "status": "deleted",
            "deleted_at": "2026-08-16T00:01:00Z",
        }

    monkeypatch.setattr(support_reports, "_request_json", delete)
    result = support_reports.delete_report(record["client_report_id"])
    assert result == {"client_report_id": record["client_report_id"], "state": "deleted"}
    assert calls[0][1]["method"] == "DELETE"
    assert calls[0][1]["manage_secret"] == record["manage_secret"]
    assert not list(outbox.glob("*.json"))


def test_support_egress_file_lock_serializes_two_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    acquired_first = context.Event()
    acquired_second = context.Event()
    release_first = context.Event()
    release_second = context.Event()
    config_dir = str(tmp_path / "multiprocess-home")
    first = context.Process(
        target=_hold_support_lock,
        args=(config_dir, acquired_first, release_first),
    )
    second = context.Process(
        target=_hold_support_lock,
        args=(config_dir, acquired_second, release_second),
    )
    first.start()
    try:
        assert acquired_first.wait(5)
        second.start()
        assert not acquired_second.wait(0.5)
        release_first.set()
        assert acquired_second.wait(5)
        release_second.set()
    finally:
        release_first.set()
        release_second.set()
        first.join(10)
        if second.pid is not None:
            second.join(10)
        if first.is_alive():
            first.terminate()
        if second.pid is not None and second.is_alive():
            second.terminate()
    assert first.exitcode == 0
    assert second.exitcode == 0


def test_delete_cannot_interleave_with_inflight_post(
    outbox, capability, monkeypatch
):
    record = _enqueue(capability, "serialize me")
    post_entered = Event()
    allow_post = Event()
    calls = []

    def remote(url, **kwargs):
        method = kwargs.get("method", "GET")
        calls.append(method)
        if method == "POST":
            post_entered.set()
            assert allow_post.wait(5)
            return 201, _receipt(record)
        return 200, {
            "schema_version": 1,
            "client_report_id": record["client_report_id"],
            "receipt_id": "support_receipt_123",
            "status": "deleted",
            "deleted_at": "2026-08-16T00:01:00Z",
        }

    monkeypatch.setattr(support_reports, "_request_json", remote)
    deliver = Thread(
        target=support_reports.deliver_report,
        args=(record["client_report_id"],),
    )
    deleted = []
    remove = Thread(
        target=lambda: deleted.append(
            support_reports.delete_report(record["client_report_id"])
        ),
    )
    deliver.start()
    assert post_entered.wait(5)
    remove.start()
    time.sleep(0.1)
    assert calls == ["POST"]
    allow_post.set()
    deliver.join(5)
    remove.join(5)

    assert calls == ["POST", "DELETE"]
    assert deleted == [{
        "client_report_id": record["client_report_id"],
        "state": "deleted",
    }]


def test_discovery_rejects_cross_origin_endpoints(monkeypatch):
    discovery = {
        "support_reports": {
            "api_version": 1,
            "open": True,
            "reports_url": "https://attacker.example/reports",
            "report_lookup_url": "/api/reports/{client_report_id}",
            "terms_url": "/api/terms",
            "max_report_bytes": 100,
        }
    }
    with pytest.raises(support_reports.SupportReportError) as error:
        support_reports.discover_capability(
            discovery, origin="https://support.example.test"
        )
    assert error.value.code == "capability_incompatible"


def test_discovery_fetches_strict_versioned_terms(monkeypatch):
    discovery = {
        "support_reports": {
            "api_version": 1,
            "open": True,
            "reports_url": "/api/support/v1/reports",
            "report_lookup_url": "/api/support/v1/reports/{client_report_id}",
            "terms_url": "/api/support/v1/terms",
            "max_report_bytes": 999999,
        }
    }
    monkeypatch.setattr(
        support_reports,
        "_request_json",
        lambda *_args, **_kwargs: (
            200,
            {
                "schema_version": 1,
                "purpose": "Private troubleshooting only.",
                "terms_version": "terms-v1",
                "retention_policy_version": "retention-v1",
                "terms_text": "Terms.",
                "retention_text": "Thirty days.",
                "retention_days": 30,
                "contact_email": "support@example.test",
            },
        ),
    )
    result = support_reports.discover_capability(
        discovery, origin="https://support.example.test/share"
    )
    assert result.max_report_bytes == support_reports.SUPPORT_REPORT_LOCAL_MAX_BYTES
    assert result.reports_url == "https://support.example.test/api/support/v1/reports"
    assert result.terms_version == "terms-v1"
    assert result.screenshots is None


def test_discovery_additively_validates_open_screenshot_capability(monkeypatch):
    discovery = {
        "support_reports": {
            "api_version": 1,
            "open": True,
            "reports_url": "/api/support/v1/reports",
            "report_lookup_url": "/api/support/v1/reports/{client_report_id}",
            "terms_url": "/api/support/v1/terms",
            "max_report_bytes": 64 * 1024,
            "screenshots": {
                "api_version": 1,
                "open": True,
                "upload_url": "/api/support/v1/reports/{client_report_id}/screenshot",
                "content_type": "image/png",
                "max_input_bytes": 2 * 1024 * 1024,
                "max_output_bytes": 2 * 1024 * 1024,
                "max_width": 4096,
                "max_height": 4096,
                "max_pixels": 4 * 1024 * 1024,
                "sanitizer_version": "png-rgb-v1",
            },
        }
    }
    monkeypatch.setattr(
        support_reports,
        "_request_json",
        lambda *_args, **_kwargs: (
            200,
            {
                "schema_version": 1,
                "purpose": "Private troubleshooting only.",
                "terms_version": "terms-v2",
                "retention_policy_version": "retention-v2",
                "terms_text": "Terms including optional screenshots.",
                "retention_text": "Thirty days.",
            },
        ),
    )
    result = support_reports.discover_capability(
        discovery,
        origin="https://support.example.test",
    )
    assert result.screenshots is not None
    assert result.screenshots.upload_url == (
        "https://support.example.test/api/support/v1/reports/"
        "{client_report_id}/screenshot"
    )
    assert result.as_ui_payload()["screenshots"]["available"] is True

    discovery["support_reports"]["screenshots"] = {"open": False, "bad": "ignored"}
    assert support_reports.discover_capability(
        discovery,
        origin="https://support.example.test",
    ).screenshots is None


def test_loopback_protocol_sends_exact_markdown_and_secret_header(outbox):
    observed = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def respond(self, status, payload):
            raw = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            if self.path == "/.well-known/clawjournal-share.json":
                self.respond(
                    200,
                    {
                        "support_reports": {
                            "api_version": 1,
                            "open": True,
                            "reports_url": "/api/support/v1/reports",
                            "report_lookup_url": (
                                "/api/support/v1/reports/{client_report_id}"
                            ),
                            "terms_url": "/api/support/v1/terms",
                            "max_report_bytes": 32768,
                        }
                    },
                )
            else:
                self.respond(
                    200,
                    {
                        "schema_version": 1,
                        "purpose": "Private troubleshooting only.",
                        "terms_version": "terms-v1",
                        "retention_policy_version": "retention-v1",
                        "terms_text": "Terms.",
                        "retention_text": "Thirty days.",
                        "retention_days": 30,
                        "contact_email": "support@example.test",
                    },
                )

        def do_POST(self):
            length = int(self.headers["Content-Length"])
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            observed.update({
                "payload": payload,
                "secret": self.headers.get("X-ClawJournal-Report-Secret"),
            })
            self.respond(
                201,
                {
                    "schema_version": 1,
                    "client_report_id": payload["client_report_id"],
                    "receipt_id": "receipt-loopback",
                    "status": "received",
                    "created_at": "2026-08-16T00:00:00Z",
                    "expires_at": "2026-09-15T00:00:00Z",
                    "content_sha256": payload["content_sha256"],
                    "lookup_url": self.path,
                    "delete_url": self.path,
                    "idempotent_replay": False,
                },
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        origin = f"http://127.0.0.1:{server.server_address[1]}"
        discovered = support_reports.fetch_capability(origin=origin)
        markdown = "# Exact over HTTP\n\n用户编辑 🐾"
        record = support_reports.enqueue_report(
            report_markdown=markdown,
            accepted_terms_version="terms-v1",
            accepted_retention_policy_version="retention-v1",
            capability=discovered,
        )
        accepted = support_reports.deliver_report(record["client_report_id"])
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert accepted["state"] == "accepted"
    assert observed["payload"]["report_markdown"] == markdown
    assert observed["payload"]["content_sha256"] == hashlib.sha256(
        markdown.encode("utf-8")
    ).hexdigest()
    assert observed["secret"] == record["manage_secret"]
    assert "manage_secret" not in observed["payload"]
