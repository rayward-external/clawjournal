"""Automatic weekly sharing service and crash-safe runner.

SQLite is the sole local authority.  Hooks only perform the bounded due check
at the bottom of this module and detach a runner; all scanning, privacy work,
artifact sealing, network submission, and recovery happens here.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

from . import __version__, config as config_module
from .agent_hooks import (
    AgentName,
    DueDecision,
    hook_diagnostics,
    install_hooks,
    spawn_detached,
    uninstall_agent_hook,
)
from .auto_upload_client import (
    MAX_SCOPE_ENTRIES,
    CapabilityError,
    RecurringServiceError,
    create_enrollment,
    fetch_authorization,
    fetch_capabilities,
    get_enrollment,
    lookup_receipt,
    recovery_capabilities,
    revoke_enrollment,
    submit_artifact,
    update_enrollment,
)
from .auto_upload_credentials import (
    CredentialStoreError,
    _require_private_mode,
    delete_credentials,
    load_credentials,
    remove_active_token,
    write_credentials,
)
from .config import load_config, save_config, source_scope_sources
from .paths import atomic_write_text, ensure_hash_salt
from .raw_sources import (
    RawFingerprint,
    RawSourceChanged,
    fingerprint_raw_source,
    stat_raw_source,
)
from .scoring.backends import resolve_backend
from .share_flow import build_zip, package, verify_coverage
from .workbench.index import (
    auto_upload_review_blockers,
    apply_share_redactions,
    get_auto_upload_candidate_report,
    get_auto_upload_enrollment,
    get_effective_share_settings,
    get_session_detail,
    get_share,
    open_index,
    release_gate_blockers,
    save_auto_upload_enrollment,
    session_matches_excluded_projects,
    set_hold_state,
    share_revision_blockers,
    update_auto_upload_enrollment,
)

CADENCE_DAYS = 7
MAX_SESSIONS = 5
BACKOFF_BASE_SECONDS = 15 * 60
BACKOFF_MAX_SECONDS = 24 * 60 * 60
RUN_LOCK_FILENAME = "auto-upload.lock"
CONTROL_LOCK_FILENAME = "auto-upload-control.lock"
SEALED_ZIP_FILENAME = "auto-upload.sealed.zip"
TELEMETRY_MAX_BYTES = 512 * 1024
TELEMETRY_BACKUPS = 2
SUPPORTED_HOOK_TARGETS = ("claude", "codex")
HOOK_DB_BUSY_TIMEOUT_MS = 0
AUTO_UPLOAD_UI_ENV = "CLAWJOURNAL_ENABLE_AUTO_UPLOAD_UI"
_CONTROL_THREAD_LOCK = threading.Lock()

# V1 supports only sources with both a SessionStart trigger and an audited,
# content-bound raw-input resolver.  Other parsers remain available for manual
# review/share, but cannot enter unattended selection until they implement the
# same strict snapshot and mutation contract.
COMPLETION_MODES = {
    "claude": "stable_revision",
    "codex": "stable_revision",
}


class AutoUploadError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after

    def as_result(self) -> dict[str, Any]:
        return {
            "ok": False,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
            "retry_after": self.retry_after,
        }


class ControlChanged(AutoUploadError):
    def __init__(self, message: str = "Automatic upload controls changed during the run."):
        super().__init__("control_changed", message)


def _telemetry_path() -> Path:
    return Path(config_module.CONFIG_DIR) / "logs" / "auto-upload.jsonl"


def _stable_telemetry_code(value: Any) -> str:
    code = str(value or "error")
    if len(code) > 64 or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in code):
        return "unrecognized_error"
    return code


def _write_telemetry(event: str, **fields: Any) -> None:
    """Write bounded private operational telemetry with a strict safe schema."""

    allowed = {"stage", "code", "count", "duration_ms", "scheduled_client"}
    if set(fields) - allowed:
        return
    payload: dict[str, Any] = {
        "timestamp": _iso(_now()),
        "event": str(event),
    }
    for key, value in fields.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            payload[key] = value
        else:
            return
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )
    path = _telemetry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
        else:
            _require_private_mode(path.parent, 0o700)
        if path.exists() and path.stat().st_size + len(encoded) > TELEMETRY_MAX_BYTES:
            oldest = path.with_suffix(path.suffix + f".{TELEMETRY_BACKUPS}")
            oldest.unlink(missing_ok=True)
            for index in range(TELEMETRY_BACKUPS - 1, 0, -1):
                source = path.with_suffix(path.suffix + f".{index}")
                if source.exists():
                    os.replace(source, path.with_suffix(path.suffix + f".{index + 1}"))
            os.replace(path, path.with_suffix(path.suffix + ".1"))
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            if os.name != "nt":
                os.fchmod(fd, 0o600)
            os.write(fd, encoded)
            os.fsync(fd)
        finally:
            os.close(fd)
        _require_private_mode(path, 0o600)
    except (OSError, CredentialStoreError):
        # Telemetry is diagnostic only and must never affect privacy gates or
        # the user's agent startup.
        return


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _due_at(enrollment: Mapping[str, Any]) -> datetime | None:
    base = _parse_time(enrollment.get("last_completed_at")) or _parse_time(
        enrollment.get("enrolled_at")
    )
    return base + timedelta(days=CADENCE_DAYS) if base is not None else None


def due_decision(
    enrollment: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
) -> DueDecision:
    current = now or _now()
    if enrollment is None:
        return DueDecision(False, "not-enrolled")
    if enrollment.get("mode") != "enabled":
        return DueDecision(False, f"mode-{enrollment.get('mode', 'off')}")
    # ``current_run_id`` is a display/recovery overlay, not a lock.  It can
    # survive process death.  The OS-owned whole-run lock below is the only
    # authoritative proof that another runner is alive.
    retry_at = _parse_time(enrollment.get("next_retry_at"))
    if retry_at is not None:
        return DueDecision(current >= retry_at, "retry-due" if current >= retry_at else "retry-wait")
    due_at = _due_at(enrollment)
    if due_at is None:
        return DueDecision(False, "invalid-cadence-state")
    return DueDecision(current >= due_at, "due" if current >= due_at else "not-due")


def _run_lock_path() -> Path:
    return Path(config_module.CONFIG_DIR) / RUN_LOCK_FILENAME


def _control_lock_path() -> Path:
    return Path(config_module.CONFIG_DIR) / CONTROL_LOCK_FILENAME


@contextmanager
def control_mutation_lock() -> Iterator[None]:
    """Serialize the short DB/credential handoff for Enable and Disable.

    Hosted requests and config writes stay outside this lock so Disable remains
    responsive and lock ordering stays acyclic. Each generation/credential
    phase is ordered locally; Enable's recovery-only then Enabled+active phases
    make every crash boundary fail-closed. The OS releases the file lock if a
    process dies.
    """

    path = _control_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _CONTROL_THREAD_LOCK:
        file = path.open("a+b")
        try:
            if os.name == "nt":
                import msvcrt

                file.seek(0, os.SEEK_END)
                if file.tell() == 0:
                    file.write(b"0")
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)
        finally:
            file.close()


@contextmanager
def whole_run_lock(*, blocking: bool = False) -> Iterator[bool]:
    """Acquire a process-owned lock that the OS releases on process death."""

    path = _run_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("a+b")
    acquired = False
    try:
        if os.name == "nt":
            import msvcrt

            try:
                file.seek(0)
                if file.tell() == 0:
                    file.write(b"0")
                    file.flush()
                file.seek(0)
                mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
                msvcrt.locking(file.fileno(), mode, 1)
                acquired = True
            except OSError:
                acquired = False
        else:
            import fcntl

            try:
                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                fcntl.flock(file.fileno(), flags)
                acquired = True
            except BlockingIOError:
                acquired = False
        yield acquired
    finally:
        if acquired:
            try:
                if os.name == "nt":
                    import msvcrt

                    file.seek(0)
                    msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        file.close()


def _current_scope(
    conn: sqlite3.Connection,
    config: Mapping[str, Any],
) -> dict[str, Any]:
    source_value = str(config.get("source") or "").strip().lower()
    source_confirmed = bool(source_value and source_value != "auto")
    projects_confirmed = config.get("projects_confirmed") is True
    configured_sources = source_scope_sources(source_value)
    rows = [dict(row) for row in conn.execute(
        "SELECT DISTINCT source, project FROM sessions ORDER BY source, project"
    ).fetchall()]
    if configured_sources is None:
        sources = sorted({str(row["source"]) for row in rows}) if source_confirmed else []
    else:
        sources = sorted(configured_sources)
    # Candidate scope must use the same merged policy surface as packaging.
    # Otherwise a DB-level exclude policy can silently remove a disclosed
    # project only after the automatic share has already been selected.
    excluded = list(get_effective_share_settings(conn, dict(config))["excluded_projects"])
    scoped_rows = [
        row
        for row in rows
        if row.get("source") in sources
        and not session_matches_excluded_projects(row, excluded)
    ]
    projects = sorted({str(row["project"]) for row in scoped_rows if row.get("project")})
    # Protocol v2 enrolls explicit (source, project) entries. Certify the FULL
    # cross product of the confirmed sources and projects — exactly the scope
    # the candidate filter enforces (source IN sources AND project IN
    # projects) and exactly what the consent surfaces display as two lists.
    # Enrolling only the currently-observed pairs would let a later session in
    # a new combination of already-enrolled source and project egress outside
    # the server-certified scope.
    entries = sorted((source, project) for source in sources for project in projects)
    blockers: list[str] = []
    if len(entries) > MAX_SCOPE_ENTRIES:
        # The hosted service caps enrollment scope entries; surface it before
        # consent, network, or the strict scan rather than as a server-worded
        # rejection after all three.
        blockers.append("scope_too_large")
    if not source_confirmed:
        blockers.append("source_confirmation_missing")
    if not projects_confirmed:
        blockers.append("project_confirmation_missing")
    if not sources:
        blockers.append("source_scope_empty")
    if not projects:
        blockers.append("project_scope_empty")
    unsupported = sorted(set(sources) - set(COMPLETION_MODES))
    if unsupported:
        blockers.append("unsupported_source")
    return {
        "sources": sources,
        "projects": projects,
        "entries": entries,
        "source_confirmed": source_confirmed,
        "projects_confirmed": projects_confirmed,
        "blockers": blockers,
        "unsupported_sources": unsupported,
    }


def _keyed_digest(domain: str, payload: Any) -> str:
    install_dir = Path(config_module.CONFIG_DIR)
    salt = ensure_hash_salt(install_dir)
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hmac.new(salt, domain.encode("utf-8") + b"\0" + encoded, hashlib.sha256).hexdigest()


def trace_revision_key(session_id: str, content_revision: str) -> str:
    return _keyed_digest(
        "clawjournal-recurring-trace-revision-v1",
        {"session_id": session_id, "content_revision": content_revision},
    )


def _resolved_ai_backend(config: Mapping[str, Any]) -> str | None:
    if not config.get("ai_pii_review_enabled"):
        return None
    requested = str(config.get("scorer_backend") or "auto")
    try:
        return resolve_backend(requested)
    except Exception as exc:
        raise AutoUploadError(
            "ai_backend_unavailable",
            "The configured AI-PII provider is not available.",
        ) from exc


def egress_profile_hash(
    conn: sqlite3.Connection,
    *,
    enrollment_scope: Mapping[str, Sequence[str]],
    api_origin: str,
    ai_backend: str | None,
    config: Mapping[str, Any] | None = None,
) -> str:
    current = dict(load_config()) if config is None else dict(config)
    settings = get_effective_share_settings(conn, current)
    policy_digest = hashlib.sha256(
        json.dumps(
            {
                "custom_strings": settings["custom_strings"],
                "extra_usernames": settings["extra_usernames"],
                "allowlist_entries": settings["allowlist_entries"],
                "blocked_domains": settings["blocked_domains"],
                "excluded_projects": settings["excluded_projects"],
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return _keyed_digest(
        "clawjournal-recurring-egress-profile-v1",
        {
            "source_selection": current.get("source"),
            "projects_confirmed": current.get("projects_confirmed") is True,
            "sources": sorted(enrollment_scope["sources"]),
            "projects": sorted(enrollment_scope["projects"]),
            "policy_digest": policy_digest,
            "ai_pii_enabled": bool(current.get("ai_pii_review_enabled")),
            "ai_backend": ai_backend,
            "api_origin": api_origin.rstrip("/").lower(),
        },
    )


def _candidate_report(
    conn: sqlite3.Connection,
    enrollment: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    config: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    current_config = _load_config_readonly() if config is None else dict(config)
    scope = _current_scope(conn, current_config)
    if enrollment is None:
        return {
            "eligible": [],
            "selected": [],
            "eligible_count": 0,
            "selected_count": 0,
            "deferred_by_cap": 0,
            "deferred_by_size": 0,
            "exclusion_counts": {},
            "exclusions": [],
            "scope_blockers": ["not_enrolled"],
            "limit": MAX_SESSIONS,
        }
    report = get_auto_upload_candidate_report(
        conn,
        current_sources=scope["sources"],
        current_projects=scope["projects"],
        source_confirmed=scope["source_confirmed"],
        projects_confirmed=scope["projects_confirmed"],
        completion_modes={
            source: COMPLETION_MODES[source]
            for source in enrollment["enrolled_sources"]
            if source in COMPLETION_MODES
        },
        now=now,
        limit=MAX_SESSIONS,
    )
    report.setdefault("deferred_by_size", 0)
    return report


def _load_config_readonly() -> dict[str, Any]:
    """Read config without running migrations or persisting repairs."""

    result = dict(config_module.DEFAULT_CONFIG)
    path = Path(config_module.CONFIG_DIR) / "config.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return result
    if isinstance(raw, dict):
        result.update(raw)
    return result


def _safe_hook_diagnostic(
    target: AgentName, *, last_observed_at: str | None
) -> dict[str, Any]:
    try:
        return hook_diagnostics(target, last_observed_at=last_observed_at)
    except Exception:
        return {
            "agent": target,
            "configured": False,
            "installed": False,
            "last_observed_at": last_observed_at,
            "diagnostic": "Hook configuration could not be read safely.",
        }


def _selected_hook_missing(enrollment: Mapping[str, Any]) -> bool:
    """Return whether any selected hook is no longer safely configured."""

    targets = enrollment.get("hook_targets", [])
    for target in targets:
        if target not in SUPPORTED_HOOK_TARGETS:
            return True
        observed = enrollment.get(f"{target}_hook_observed_at")
        if not _safe_hook_diagnostic(target, last_observed_at=observed).get(
            "configured"
        ):
            return True
    return False


def _has_successful_manual_receipt(conn: sqlite3.Connection) -> bool:
    """Return whether this installation completed a hosted manual share."""

    return (
        conn.execute(
            "SELECT 1 FROM shares WHERE hosted_receipt_id IS NOT NULL "
            "AND COALESCE(submission_channel, 'manual') != 'auto_weekly' LIMIT 1"
        ).fetchone()
        is not None
    )


def _auto_upload_ui_visible(
    config: Mapping[str, Any],
    enrollment: Mapping[str, Any] | None = None,
) -> bool:
    explicitly_enabled = (
        os.environ.get(AUTO_UPLOAD_UI_ENV) == "1"
        or config.get("auto_upload_ui_enabled") is True
    )
    existing_authority = bool(
        enrollment
        and (
            enrollment.get("mode") != "off"
            or enrollment.get("server_enrollment_id")
            or enrollment.get("revocation_pending")
        )
    )
    return explicitly_enabled or existing_authority


def _off_status(config: Mapping[str, Any]) -> dict[str, Any]:
    hooks = []
    for target in SUPPORTED_HOOK_TARGETS:
        diagnostic = _safe_hook_diagnostic(target, last_observed_at=None)
        diagnostic["selected"] = False
        hooks.append(diagnostic)
    return {
        "ok": True,
        "mode": "off",
        "health": "ready",
        "run_now_allowed": False,
        "overlay": None,
        "pending_submission_state": None,
        "ui_visible": _auto_upload_ui_visible(config),
        "offer_available": False,
        "scope": {"sources": [], "projects": []},
        "cap": MAX_SESSIONS,
        "cadence_days": CADENCE_DAYS,
        "ai": {
            "enabled": bool(config.get("ai_pii_review_enabled")),
            "backend": (
                config.get("scorer_backend") or "auto"
                if config.get("ai_pii_review_enabled")
                else None
            ),
        },
        "authorization": {"version": None, "text": None},
        "retention": {"version": None, "text": None},
        "enrolled_at": None,
        "next_due_at": None,
        "next_retry_at": None,
        "hooks": hooks,
        "eligibility": {
            "selected_count": 0,
            "eligible_count": 0,
            "exclusion_counts": {},
            "scope_blockers": ["not_enrolled"],
        },
        "last_result": None,
        "generation": None,
    }


def status(*, conn: sqlite3.Connection | None = None) -> dict[str, Any]:
    """Return side-effect-free local status; this performs no network call."""

    config = _load_config_readonly()
    own_connection = False
    if conn is None:
        from .workbench import index as index_module

        path = Path(index_module.INDEX_DB)
        if not path.exists():
            return _off_status(config)
        try:
            db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
        except sqlite3.Error:
            return _off_status(config)
        db.row_factory = sqlite3.Row
        own_connection = True
    else:
        db = conn
    try:
        try:
            enrollment = get_auto_upload_enrollment(db)
        except sqlite3.DatabaseError:
            return _off_status(config)
        report = _candidate_report(db, enrollment, config=config)
        successful_manual = _has_successful_manual_receipt(db)
        mode = enrollment.get("mode", "off") if enrollment else "off"
        stored_health = enrollment.get("health", "ready") if enrollment else "ready"
        overlay = None
        if enrollment and enrollment.get("revocation_pending"):
            overlay = "revocation_pending"
        elif enrollment and enrollment.get("current_run_id"):
            overlay = "running"
        pending_share = _pending_submission(db)
        pending_submission_state = (
            str(pending_share["submission_state"]) if pending_share else None
        )
        hook_rows: list[dict[str, Any]] = []
        targets = enrollment.get("hook_targets", []) if enrollment else []
        for target in SUPPORTED_HOOK_TARGETS:
            observed = enrollment.get(f"{target}_hook_observed_at") if enrollment else None
            diagnostic = _safe_hook_diagnostic(target, last_observed_at=observed)
            diagnostic["selected"] = target in targets
            hook_rows.append(diagnostic)
        hook_action_required = bool(
            mode == "enabled"
            and any(
                row.get("selected") and not row.get("configured")
                for row in hook_rows
            )
        )
        health = "action_required" if hook_action_required else stored_health
        due_at = _due_at(enrollment) if enrollment else None
        last_result = None
        if enrollment and enrollment.get("last_result_code"):
            last_result = {
                "code": enrollment.get("last_result_code"),
                "count": enrollment.get("last_result_count"),
                "receipt_reference": enrollment.get("last_receipt_reference"),
            }
        return {
            "ok": True,
            "mode": mode,
            "health": health,
            # A missing hook blocks scheduled work but is deliberately not an
            # egress/privacy failure: an explicit Run now may still proceed.
            # Durable action_required state never gets that exception.
            "run_now_allowed": bool(
                mode == "enabled" and stored_health != "action_required"
            ),
            "overlay": overlay,
            "pending_submission_state": pending_submission_state,
            "ui_visible": _auto_upload_ui_visible(config, enrollment),
            "offer_available": bool(
                mode == "off"
                and successful_manual
                and config.get("auto_upload_capability_available") is True
            ),
            "scope": {
                "sources": list(enrollment.get("enrolled_sources", [])) if enrollment else [],
                "projects": list(enrollment.get("enrolled_projects", [])) if enrollment else [],
            },
            "cap": MAX_SESSIONS,
            "cadence_days": CADENCE_DAYS,
            "ai": {
                "enabled": bool(config.get("ai_pii_review_enabled")),
                "backend": (
                    config.get("scorer_backend") or "auto"
                    if config.get("ai_pii_review_enabled")
                    else None
                ),
            },
            "authorization": {
                "version": enrollment.get("recurring_authorization_version") if enrollment else None,
                "text": None,
            },
            "retention": {
                "version": enrollment.get("retention_version") if enrollment else None,
                "text": None,
            },
            "enrolled_at": enrollment.get("enrolled_at") if enrollment else None,
            "next_due_at": _iso(due_at) if due_at else None,
            "next_retry_at": enrollment.get("next_retry_at") if enrollment else None,
            "hooks": hook_rows,
            "eligibility": {
                "selected_count": report.get("selected_count", 0),
                "eligible_count": report.get("eligible_count", 0),
                "exclusion_counts": report.get("exclusion_counts", {}),
                "scope_blockers": report.get("scope_blockers", []),
            },
            "last_result": last_result,
            "generation": enrollment.get("generation") if enrollment else None,
        }
    finally:
        if own_connection:
            db.close()


def preview(*, refresh: bool = False, now: datetime | None = None) -> dict[str, Any]:
    if not refresh:
        # GET preview is a read-only surface just like GET status: do not create
        # the index, run migrations, or repair state merely because Settings
        # was opened.
        from .workbench import index as index_module

        path = Path(index_module.INDEX_DB)
        if not path.exists():
            return {
                "ok": True,
                "eligible": [],
                "selected": [],
                "eligible_count": 0,
                "selected_count": 0,
                "deferred_by_cap": 0,
                "deferred_by_size": 0,
                "exclusion_counts": {},
                "exclusions": [],
                "scope_blockers": ["not_enrolled"],
                "limit": MAX_SESSIONS,
            }
        try:
            readonly = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
            readonly.row_factory = sqlite3.Row
            try:
                enrollment = get_auto_upload_enrollment(readonly)
                report = _candidate_report(readonly, enrollment, now=now)
                return {"ok": True, **report}
            finally:
                readonly.close()
        except sqlite3.DatabaseError:
            return AutoUploadError(
                "index_upgrade_required",
                "Refresh the local index before previewing automatic uploads.",
            ).as_result()

    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        if refresh:
            if enrollment is None:
                return AutoUploadError("not_enrolled", "Automatic upload is not enabled.").as_result()
            from .workbench.daemon import Scanner

            scan = Scanner().scan_once_strict(list(enrollment["enrolled_sources"]))
            if not scan["ok"]:
                return {
                    "ok": False,
                    "code": "strict_scan_incomplete",
                    "message": "The enrolled source refresh was incomplete.",
                    "retryable": True,
                    "scan": scan,
                }
        report = _candidate_report(conn, enrollment, now=now)
        return {"ok": True, **report}
    finally:
        conn.close()


def _authorization_profile_hash(challenge: Mapping[str, Any]) -> str:
    """Bind acceptance to the complete profile shown to the user.

    This is deliberately an unkeyed content digest: loading an authorization
    challenge must remain read-only and must not bootstrap the install salt.
    The digest is only an opaque local challenge token; the profile itself is
    already returned to the local CLI/browser for review.
    """

    payload = {
        "authorization": challenge["authorization"],
        "retention": challenge["retention"],
        "ownership_certification": challenge["ownership_certification"],
        "scope": challenge["scope"],
        "ai": challenge["ai"],
        "cap": challenge["cap"],
        "cadence_days": challenge["cadence_days"],
        "maximum_bundle_size": challenge["maximum_bundle_size"],
        "destination_origin": challenge["destination_origin"],
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _authorization_challenge(
    capabilities: Mapping[str, Any],
    terms: Mapping[str, Any],
    scope: Mapping[str, Any],
    ai_backend: str | None,
) -> dict[str, Any]:
    authorization_version = terms.get("authorization_version")
    authorization_text = terms.get("authorization_text")
    retention_version = terms.get("retention_policy_version")
    retention_text = terms.get("retention_text")
    ownership_version = terms.get("ownership_certification_version")
    ownership_text = terms.get("ownership_certification_text")
    if not all(
        isinstance(value, str) and value.strip()
        for value in (
            authorization_version,
            authorization_text,
            retention_version,
            retention_text,
            ownership_version,
            ownership_text,
        )
    ):
        raise AutoUploadError(
            "malformed_authorization",
            "Hosted service returned incomplete recurring authorization terms.",
            retryable=True,
        )
    challenge = {
        "ok": False,
        "status": 409,
        "code": "authorization_required",
        "message": (
            "Review and accept the exact recurring authorization, retention, "
            "and ownership-certification terms."
        ),
        "authorization": {
            "version": authorization_version,
            "text": authorization_text,
        },
        "retention": {
            "version": retention_version,
            "text": retention_text,
        },
        "ownership_certification": {
            "version": ownership_version,
            "text": ownership_text,
        },
        "scope": {
            "sources": list(scope["sources"]),
            "projects": list(scope["projects"]),
            "entries": [list(entry) for entry in scope["entries"]],
        },
        "ai": {"enabled": ai_backend is not None, "backend": ai_backend},
        "cap": MAX_SESSIONS,
        "cadence_days": CADENCE_DAYS,
        "maximum_bundle_size": capabilities["maximum_bundle_size"],
        "destination_origin": capabilities["origin"],
    }
    challenge["authorization_profile_hash"] = _authorization_profile_hash(challenge)
    return challenge


def _hook_targets(agent: str) -> list[AgentName]:
    normalized = (agent or "all").strip().lower()
    if normalized == "all":
        return ["claude", "codex"]
    if normalized in SUPPORTED_HOOK_TARGETS:
        return [normalized]  # type: ignore[list-item]
    raise AutoUploadError(
        "invalid_hook_target",
        "Choose Claude Code, Codex, or both for SessionStart scheduling.",
    )


def _snapshot_hook_files(targets: Sequence[AgentName]) -> dict[Path, str | None]:
    from . import agent_hooks

    snapshots: dict[Path, str | None] = {}
    for target in targets:
        path = agent_hooks._hook_path(target)
        snapshots[path] = path.read_text(encoding="utf-8") if path.exists() else None
    return snapshots


def _restore_hook_files(snapshots: Mapping[Path, str | None]) -> None:
    for path, previous in snapshots.items():
        if previous is None:
            path.unlink(missing_ok=True)
        else:
            atomic_write_text(path, previous, parents=True)


def _reconcile_explicit_pause_after_reauthorization(
    conn: sqlite3.Connection,
    *,
    intent_generation: int,
    client_enrollment_id: str,
    server_enrollment_id: str,
    enrolled_at: datetime,
    enrolled_sources: Sequence[str],
    enrolled_projects: Sequence[str],
    authorization_revision: int,
    authorization_version: str,
    retention_version: str,
    ownership_certification_version: str,
    server_scope_hash: str,
    egress_profile: str,
    hook_targets: Sequence[AgentName],
    restore_credentials: Mapping[str, Any] | None = None,
) -> bool:
    """Commit a definite hosted reauthorization without undoing a racing Pause.

    Pause is the only control this recovery may merge with.  Every identity,
    generation, and audit marker must match the one-step Pause winner so a
    stale reauthorization can never overwrite Disable, Resume, or a profile
    mutation.  The extra generation records the local reconciliation while
    preserving the user's paused authority boundary.
    """

    with control_mutation_lock():
        current = get_auto_upload_enrollment(conn)
        if (
            current is None
            or int(current.get("generation", 0)) != intent_generation + 1
            or current.get("mode") != "paused"
            or current.get("last_result_code") != "paused"
            or current.get("client_enrollment_id") != client_enrollment_id
            or current.get("server_enrollment_id") != server_enrollment_id
            or current.get("revocation_pending")
        ):
            return False
        reconciled_generation = int(current["generation"]) + 1
        if not update_auto_upload_enrollment(
            conn,
            expected_generation=int(current["generation"]),
            generation=reconciled_generation,
            mode="paused",
            health="ready",
            enrolled_at=_iso(enrolled_at),
            client_enrollment_id=client_enrollment_id,
            enrolled_sources=enrolled_sources,
            enrolled_projects=enrolled_projects,
            server_enrollment_id=server_enrollment_id,
            authorization_revision=authorization_revision,
            recurring_authorization_version=authorization_version,
            retention_version=retention_version,
            ownership_certification_version=ownership_certification_version,
            server_scope_hash=server_scope_hash,
            egress_profile_hash=egress_profile,
            hook_targets=hook_targets,
            consecutive_failures=0,
            next_retry_at=None,
            revocation_pending=False,
            current_run_id=None,
            current_run_stage=None,
            last_result_code="paused",
        ):
            return False
        if restore_credentials is not None:
            try:
                write_credentials(restore_credentials)
            except CredentialStoreError:
                # The hosted/local authorization revision is already
                # reconciled and remains Paused. Surface that the active
                # credential could not be restored instead of reporting a
                # misleading healthy Pause over recovery-only authority.
                update_auto_upload_enrollment(
                    conn,
                    expected_generation=reconciled_generation,
                    health="action_required",
                    last_result_code="credential_store_failed",
                )
                raise
        return True


def enable(
    *,
    agent: str = "all",
    accepted_authorization_version: str | None = None,
    accepted_retention_version: str | None = None,
    accepted_ownership_certification_version: str | None = None,
    accepted_authorization_profile_hash: str | None = None,
    challenge_only: bool = False,
) -> dict[str, Any]:
    """Review or transactionally create/update a recurring enrollment.

    ``challenge_only`` always returns the authorization challenge and can
    never enroll, so clients that only want to display the current terms
    are not depending on version mismatch to keep the call non-mutating.
    Protocol v2 requires the ownership certification to be accepted with the
    same exact-version discipline as the authorization and retention terms.
    """

    targets = _hook_targets(agent)
    lock_context: Any | None = None
    conn = open_index()
    try:
        config = load_config()
        scope = _current_scope(conn, config)
        if scope["blockers"]:
            first_blocker = scope["blockers"][0]
            if first_blocker == "scope_too_large":
                message = (
                    "The source x project scope exceeds the hosted limit of "
                    f"{MAX_SCOPE_ENTRIES} entries; exclude projects "
                    "(config --exclude) or narrow the source scope first."
                )
            else:
                message = "Confirm a non-empty source and project scope before enabling."
            return {
                "ok": False,
                "code": first_blocker,
                "message": message,
                "scope_blockers": scope["blockers"],
                "unsupported_sources": scope["unsupported_sources"],
            }
        if not _has_successful_manual_receipt(conn):
            return AutoUploadError(
                "manual_share_required",
                "Complete one successful hosted manual share before enabling automatic uploads.",
            ).as_result()
        # Fail fast on the cheap network checks BEFORE the strict scan: the
        # scan re-parses every enrolled source log (minutes of CPU on a large
        # history), so an incompatible protocol, closed enrollment, or
        # unreachable host must be surfaced without paying it. Later steps
        # (server create/PATCH, the runner's enrollment gate) re-enforce
        # capability freshness, so a closure during the scan still loses
        # nothing.
        capabilities = fetch_capabilities(force=True)
        terms = fetch_authorization(capabilities)
        from .workbench.daemon import Scanner

        scan = Scanner().scan_once_strict(list(scope["sources"]))
        if not scan["ok"]:
            return {
                "ok": False,
                "code": "strict_scan_incomplete",
                "message": "The selected source scope could not be refreshed completely.",
                "retryable": True,
                "scan": scan,
            }
        # Recompute the exact project snapshot after the strict refresh.
        scope = _current_scope(conn, config)
        if scope["blockers"]:
            return {
                "ok": False,
                "code": scope["blockers"][0],
                "message": "The refreshed source/project scope is not eligible.",
                "scope_blockers": scope["blockers"],
            }
        ai_backend = _resolved_ai_backend(config)
        challenge = _authorization_challenge(
            capabilities, terms, scope, ai_backend
        )
        expected_auth = challenge["authorization"]["version"]
        expected_retention = challenge["retention"]["version"]
        expected_ownership = challenge["ownership_certification"]["version"]
        expected_profile = challenge["authorization_profile_hash"]
        if (
            challenge_only
            or accepted_authorization_version != expected_auth
            or accepted_retention_version != expected_retention
            or accepted_ownership_certification_version != expected_ownership
            or accepted_authorization_profile_hash != expected_profile
        ):
            # Non-mutating challenge response.  The HTTP adapter maps this to
            # 409 so CLI/GUI cannot replace exact-version acceptance with --yes.
            return challenge

        # Serialize enrollment mutations with running cycles.  Controls still
        # use generation CAS and can win immediately without waiting for this
        # network transaction.
        lock_context = whole_run_lock(blocking=False)
        acquired = lock_context.__enter__()
        if not acquired:
            return AutoUploadError(
                "already_running",
                "An automatic-upload cycle or enrollment update is already running.",
                retryable=True,
            ).as_result()

        existing = get_auto_upload_enrollment(conn)
        if existing and existing.get("revocation_pending"):
            return AutoUploadError(
                "revocation_pending",
                "Finish the prior enrollment revocation before enabling again.",
                retryable=True,
            ).as_result()
        pending_rows = conn.execute(
            "SELECT share_id, enrollment_id, submission_state FROM shares "
            "WHERE submission_channel = 'auto_weekly' "
            "AND submission_state IN ('sealed', 'submitting') "
            "ORDER BY created_at, share_id"
        ).fetchall()
        if pending_rows and (existing is None or existing.get("mode") == "off"):
            return AutoUploadError(
                "receipt_reconciliation_pending",
                "Resolve the prior ambiguous submission before enabling again.",
                retryable=True,
            ).as_result()

        updating = bool(
            existing
            and existing.get("mode") in {"enabled", "paused"}
            and existing.get("server_enrollment_id")
        )
        if updating and any(
            row["submission_state"] == "submitting"
            or row["enrollment_id"] != existing.get("server_enrollment_id")
            for row in pending_rows
        ):
            # Reauthorizing while a request is ambiguous can invalidate the
            # authorization revision needed to retry its exact sealed bytes.
            # Receipt reconciliation has priority over every scope/terms
            # update; a paused runner can still perform that recovery.
            return AutoUploadError(
                "receipt_reconciliation_pending",
                "Reconcile the in-flight recurring submission before reviewing scope or terms.",
                retryable=True,
            ).as_result()
        credentials = load_credentials(required=False)
        if updating and credentials is not None:
            if (
                credentials.get("api_origin") != capabilities.get("origin")
                or credentials.get("issuer") != capabilities.get("origin")
            ):
                raise AutoUploadError(
                    "destination_changed",
                    "The configured hosted origin no longer matches the pinned credential issuer.",
                )
        active_expiry = (
            _parse_time(credentials.get("active_token_expires_at"))
            if credentials is not None
            else None
        )
        rotating_credentials = bool(
            updating
            and (
                credentials is None
                or not credentials.get("active_token")
                or active_expiry is None
                or active_expiry <= _now()
            )
        )
        generation = int(existing.get("generation", 0) if existing else 0) + 1
        client_enrollment_id = (
            str(existing["client_enrollment_id"])
            if existing
            and (
                updating
                or (
                    existing.get("mode") == "off"
                    and not existing.get("server_enrollment_id")
                )
            )
            else str(uuid.uuid4())
        )
        profile = egress_profile_hash(
            conn,
            enrollment_scope=scope,
            api_origin=str(capabilities["origin"]),
            ai_backend=ai_backend,
            config=config,
        )
        intent_enrolled_at = (
            _parse_time(existing.get("enrolled_at"))
            if updating and existing is not None
            else _now()
        )
        if intent_enrolled_at is None:
            raise AutoUploadError(
                "enrollment_state_invalid",
                "The existing future-only enrollment boundary is invalid.",
            )
        intent_enrolled_at_text = _iso(intent_enrolled_at)

        # Persist a stable create intent before the network request.  It is Off
        # and therefore grants no egress authority if the request fails.
        if updating:
            assert existing is not None
            if not update_auto_upload_enrollment(
                conn,
                expected_generation=int(existing["generation"]),
                generation=generation,
                health="retrying",
                last_result_code="enrollment_pending",
                current_run_id=None,
                current_run_stage=None,
            ):
                raise ControlChanged("Automatic upload controls changed before enrollment.")
        elif existing is not None:
            if not update_auto_upload_enrollment(
                conn,
                expected_generation=int(existing["generation"]),
                mode="off",
                health="retrying",
                generation=generation,
                enrolled_at=intent_enrolled_at_text,
                client_enrollment_id=client_enrollment_id,
                enrolled_sources=scope["sources"],
                enrolled_projects=scope["projects"],
                server_enrollment_id=None,
                authorization_revision=None,
                recurring_authorization_version=str(expected_auth),
                retention_version=str(expected_retention),
                egress_profile_hash=profile,
                hook_targets=targets,
                revocation_pending=False,
                last_result_code="enrollment_pending",
            ):
                raise ControlChanged("Automatic upload controls changed before enrollment.")
        else:
            save_auto_upload_enrollment(
                conn,
                mode="off",
                health="retrying",
                generation=generation,
                enrolled_at=intent_enrolled_at_text,
                client_enrollment_id=client_enrollment_id,
                enrolled_sources=scope["sources"],
                enrolled_projects=scope["projects"],
                recurring_authorization_version=str(expected_auth),
                retention_version=str(expected_retention),
                egress_profile_hash=profile,
                hook_targets=targets,
                last_result_code="enrollment_pending",
            )

        snapshots: dict[Path, str | None] = {}
        committed = False
        # Assigned inside the try; the failure handler below distinguishes
        # "server enrollment was created" from earlier failures by these.
        response: dict[str, Any] | None = None
        credential_record: dict[str, Any] | None = None
        server_scope_hash: str | None = None
        # True only once the read-back confirmed the server recorded exactly
        # the certification version the user accepted. Until then, a definite
        # PATCH is in an UNVERIFIABLE consent state (the request carries only
        # a boolean), and any failure must revoke rather than pause.
        certification_verified = False
        server_reauthorization_succeeded = False
        recovery_only_written = False
        try:
            snapshots = _snapshot_hook_files(SUPPORTED_HOOK_TARGETS)
            hook_results = install_hooks(agent=agent)
            if not all(result.get("configured") for result in hook_results):
                raise AutoUploadError(
                    "hook_install_failed",
                    "A selected SessionStart hook could not be installed.",
                )
            for target in SUPPORTED_HOOK_TARGETS:
                if target not in targets:
                    uninstall_agent_hook(target)

            if updating and not rotating_credentials:
                assert credentials is not None
                active_token = credentials.get("active_token")
                if not active_token:
                    raise AutoUploadError(
                        "credential_invalid",
                        "The active recurring-upload credential is unavailable.",
                    )
                response = update_enrollment(
                    capabilities,
                    enrollment_id=str(existing["server_enrollment_id"]),
                    active_token=active_token,
                    scope_entries=scope["entries"],
                    authorization_version=str(expected_auth),
                    retention_version=str(expected_retention),
                    # Reached only after the exact-version acceptance check
                    # above matched accepted_ownership_certification_version.
                    ownership_certification=True,
                )
            else:
                upload_token = str(config.get("verified_email_token") or "").strip()
                if not upload_token:
                    raise AutoUploadError(
                        "email_verification_required",
                        "Verify your email again before creating a recurring enrollment.",
                    )
                response = create_enrollment(
                    capabilities,
                    upload_token=upload_token,
                    client_enrollment_id=client_enrollment_id,
                    scope_entries=scope["entries"],
                    authorization_version=str(expected_auth),
                    retention_version=str(expected_retention),
                    ownership_certification=True,
                )

            # The server mutation is definite the moment the create/PATCH
            # returns 2xx — BEFORE the response body is validated below. On the
            # updating path a malformed body then leaves a definitely-modified
            # (and, until the read-back, unverified) hosted enrollment that must
            # be revoked, not paused; mark it definite here so the failure
            # handler routes correctly even when the validation just below
            # raises. (Create keeps this False: the client learns a new
            # enrollment's id only from the response, so it cannot revoke a
            # malformed create — the server-expiry / client-id idempotency
            # path recovers that.)
            server_reauthorization_succeeded = bool(updating)

            enrollment_id = response.get("enrollment_id")
            enrolled_at = response.get("enrolled_at")
            parsed_server_enrolled_at = _parse_time(enrolled_at)
            authorization_revision = response.get("authorization_revision")
            if (
                not isinstance(enrollment_id, str)
                or not enrollment_id
                or parsed_server_enrolled_at is None
                or not isinstance(authorization_revision, int)
                or isinstance(authorization_revision, bool)
                or authorization_revision < 1
            ):
                raise AutoUploadError(
                    "malformed_enrollment_response",
                    "Hosted service returned incomplete recurring enrollment state.",
                    retryable=True,
                )
            if updating and enrollment_id != existing.get("server_enrollment_id"):
                raise AutoUploadError(
                    "malformed_enrollment_response",
                    "Hosted service changed the enrollment identity during an update.",
                )
            # The create clamp below stores max(local_intent, server), so a
            # stable enrollment's stored boundary is >= the server's own value.
            # On an update the server returns that same (<= stored) original
            # boundary; requiring exact equality would permanently fail
            # reauthorization whenever the local clock led the server at create
            # time. Reject only a server attempt to move the boundary *later*
            # than what we committed — an unexpected forward move for a fixed
            # enrollment. An earlier/equal server value is the normal
            # clock-skew case; we keep our own, more conservative boundary and
            # never backdate it.
            if updating and parsed_server_enrolled_at > intent_enrolled_at:
                raise AutoUploadError(
                    "malformed_enrollment_response",
                    "Hosted service moved the future-only enrollment boundary later during an update.",
                )

            # Never let a skewed or malformed hosted clock backdate the local
            # future-only boundary.  On first create the conservative cutoff is
            # the later of the durable local intent and server acceptance.  A
            # review/update preserves the original stored boundary exactly.
            effective_enrolled_at = (
                intent_enrolled_at
                if updating
                else max(intent_enrolled_at, parsed_server_enrolled_at)
            )

            if updating:
                # A sealed artifact is durably pre-send.  The successful PATCH
                # above replaces its authorization revision, so retaining that
                # old ledger would strand every future cycle on a permanent
                # revision mismatch.  Delete only same-enrollment sealed
                # drafts after the server update is definite; submitting rows
                # were rejected before the PATCH and are never discarded.
                for pending in pending_rows:
                    if (
                        pending["submission_state"] == "sealed"
                        and pending["enrollment_id"] == enrollment_id
                    ):
                        _cleanup_unsent_draft(
                            conn, str(pending["share_id"]), allow_sealed=True
                        )

            if updating and not rotating_credentials:
                assert credentials is not None
                credential_record = dict(credentials)
            else:
                required_credentials = (
                    "active_token",
                    "active_token_expires_at",
                    "recovery_token",
                    "recovery_token_expires_at",
                )
                if not all(isinstance(response.get(field), str) and response.get(field)
                           for field in required_credentials):
                    raise AutoUploadError(
                        "malformed_enrollment_response",
                        "Hosted service did not return both recurring credentials.",
                        retryable=True,
                    )
                credential_record = {
                    "issuer": capabilities["origin"],
                    "api_origin": capabilities["origin"],
                    "enrollment_id": enrollment_id,
                    **{field: response[field] for field in required_credentials},
                }

            assert credential_record is not None
            recovery_record = {
                **credential_record,
                "active_token": None,
                "active_token_expires_at": None,
            }

            # Phase 1 persists recovery-only authority. The hosted create stays
            # outside this short lock so Disable remains responsive, but once a
            # response exists Disable and Enable have a definite local order.
            # No crash point before the DB commit can leave Off + active bearer.
            with control_mutation_lock():
                current_before_recovery = get_auto_upload_enrollment(conn)
                if (
                    current_before_recovery is None
                    or int(current_before_recovery.get("generation", 0))
                    != generation
                ):
                    raise ControlChanged(
                        "Automatic upload controls changed before recovery state was saved."
                    )
                write_credentials(recovery_record)
                recovery_only_written = True

            # Protocol v2: the server owns the scope hash (an HMAC with its
            # own key), so pin the authorized scope by reading it back from
            # the definite enrollment state; the runner's scope gate compares
            # against this stored value. This network read runs AFTER Phase 1
            # deliberately: the freshest recovery token (including one just
            # reissued by a rotating reauthorization) is already durably
            # persisted, so a failed or crashed read leaves revocable
            # recovery-only authority rather than dropping live server tokens
            # on the floor. A failed read aborts enrollment fail-closed; the
            # create path's compensating revoke still applies.
            remote_state = get_enrollment(
                capabilities,
                enrollment_id=str(enrollment_id),
                active_token=str(credential_record["active_token"]),
            )
            server_scope_hash = remote_state.get("scope_hash")
            if not isinstance(server_scope_hash, str) or not server_scope_hash:
                raise AutoUploadError(
                    "malformed_enrollment_response",
                    "Hosted service returned no authoritative scope hash.",
                    retryable=True,
                )
            # The enrollment request carries only a certification BOOLEAN; the
            # server records whatever certification version is current at
            # POST/PATCH time. A rotation between fetch_authorization and the
            # enrollment request would otherwise commit authority under terms
            # the user never reviewed — and pin a stale version the runner
            # gate could never match. Verify the recorded version equals the
            # exact one the user accepted before any commit; the create
            # path's compensating revoke removes the just-created enrollment.
            # (authorization/retention versions need no read-back check: the
            # request SENDS them and the server rejects stale values.)
            if remote_state.get("ownership_certification_version") != str(
                expected_ownership
            ):
                # Distinct code: the failure handler must REVOKE the hosted
                # enrollment for this case — on the update path too, where the
                # PATCH has already been applied and the server-side consent
                # record now misrepresents the user. Merely pausing would
                # leave that record live.
                raise AutoUploadError(
                    "certification_not_accepted",
                    "The hosted ownership certification changed while enrolling; "
                    "review the updated terms and enable again.",
                )
            certification_verified = True

            config = load_config()
            config["auto_upload_capability_available"] = True
            # Only the create / credential-rotation path sends the one-shot
            # verified_email_token to the server; the non-rotating update path
            # reuses the pinned active credential and never sends it. Deleting it
            # there would silently destroy a still-valid manual-share credential
            # the user just re-verified, so gate the removal to the paths that
            # actually consumed it.
            consumed_one_shot_token = not (updating and not rotating_credentials)
            if consumed_one_shot_token:
                config.pop("verified_email_token", None)
                config.pop("verified_email_token_expires_at", None)
            if save_config(config) is False:
                raise AutoUploadError(
                    "config_persistence_failed",
                    "Recurring enrollment could not persist its local config safely.",
                )
            if consumed_one_shot_token:
                persisted_config = _load_config_readonly()
                if persisted_config.get("verified_email_token"):
                    raise AutoUploadError(
                        "config_persistence_failed",
                        "The consumed one-shot verification credential could not be removed durably.",
                    )

            # Phase 2 commits Enabled before making the active bearer visible.
            # A crash between these writes is Enabled + recovery-only, which is
            # fail-closed and can be revoked by Disable on the next attempt.
            with control_mutation_lock():
                current_before_commit = get_auto_upload_enrollment(conn)
                if (
                    current_before_commit is None
                    or int(current_before_commit.get("generation", 0)) != generation
                ):
                    raise ControlChanged(
                        "Automatic upload controls changed before commit."
                    )
                if not update_auto_upload_enrollment(
                    conn,
                    expected_generation=generation,
                    mode="enabled",
                    health="ready",
                    enrolled_at=_iso(effective_enrolled_at),
                    client_enrollment_id=client_enrollment_id,
                    enrolled_sources=scope["sources"],
                    enrolled_projects=scope["projects"],
                    server_enrollment_id=enrollment_id,
                    authorization_revision=authorization_revision,
                    recurring_authorization_version=str(expected_auth),
                    retention_version=str(expected_retention),
                    ownership_certification_version=str(expected_ownership),
                    server_scope_hash=server_scope_hash,
                    egress_profile_hash=profile,
                    hook_targets=targets,
                    last_result_code="enabled",
                    # Reauthorization keeps the cadence of the same durable
                    # enrollment. A true create after Disable is new authority
                    # and schedules from its new enrolled_at boundary.
                    last_completed_at=(
                        existing.get("last_completed_at")
                        if updating and existing is not None
                        else None
                    ),
                    last_result_count=(
                        existing.get("last_result_count")
                        if updating and existing is not None
                        else None
                    ),
                    last_receipt_reference=(
                        existing.get("last_receipt_reference")
                        if updating and existing is not None
                        else None
                    ),
                    consecutive_failures=0,
                    next_retry_at=None,
                    revocation_pending=False,
                    current_run_id=None,
                    current_run_stage=None,
                ):
                    raise ControlChanged(
                        "Automatic upload controls changed before commit."
                    )
                write_credentials(credential_record)
                committed = True
        except Exception as exc:
            current_after_failure = get_auto_upload_enrollment(conn)
            if (
                server_reauthorization_succeeded
                and certification_verified
                and isinstance(exc, ControlChanged)
                and isinstance(response, dict)
                and isinstance(response.get("enrollment_id"), str)
                and isinstance(response.get("authorization_revision"), int)
                and isinstance(server_scope_hash, str)
                and _reconcile_explicit_pause_after_reauthorization(
                    conn,
                    intent_generation=generation,
                    client_enrollment_id=client_enrollment_id,
                    server_enrollment_id=str(response["enrollment_id"]),
                    enrolled_at=effective_enrolled_at,
                    enrolled_sources=scope["sources"],
                    enrolled_projects=scope["projects"],
                    authorization_revision=int(response["authorization_revision"]),
                    authorization_version=str(expected_auth),
                    retention_version=str(expected_retention),
                    ownership_certification_version=str(expected_ownership),
                    server_scope_hash=str(server_scope_hash),
                    egress_profile=profile,
                    hook_targets=targets,
                    restore_credentials=(
                        credential_record
                        if recovery_only_written or rotating_credentials
                        else None
                    ),
                )
            ):
                return status(conn=conn)
            if (
                updating
                and current_after_failure is not None
                and current_after_failure.get("mode") == "off"
            ):
                # Disable may win after a PATCH response but before this stale
                # updater commits.  The updater can already have rewritten the
                # credential file, so reassert Off's authority boundary here:
                # never leave a resurrected active token behind.  A pending
                # revoke retains recovery only; a definite revoke retains no
                # credential material.
                try:
                    remove_active_token()
                    if not current_after_failure.get("revocation_pending"):
                        delete_credentials()
                except CredentialStoreError:
                    pass
            if (
                current_after_failure is not None
                and int(current_after_failure.get("generation", 0)) == generation
            ):
                try:
                    _restore_hook_files(snapshots)
                except OSError:
                    pass
            if isinstance(exc, RecurringServiceError):
                if updating and exc.code in {
                    "credential_invalid",
                    "credential_expired",
                    "credential_revoked",
                }:
                    try:
                        remove_active_token()
                    except CredentialStoreError:
                        pass
                    error = AutoUploadError(
                        "email_verification_required",
                        "Verify your email again to rotate recurring credentials.",
                    )
                else:
                    error = AutoUploadError(
                        exc.code,
                        exc.message,
                        retryable=exc.retryable,
                        retry_after=exc.retry_after,
                    )
            elif isinstance(exc, (AutoUploadError, CredentialStoreError)):
                error = (
                    exc
                    if isinstance(exc, AutoUploadError)
                    else AutoUploadError("credential_store_failed", str(exc))
                )
            else:
                error = AutoUploadError(
                    "enrollment_failed",
                    "Recurring enrollment could not be completed.",
                    retryable=True,
                )

            if committed:
                return AutoUploadError(
                    "status_failed",
                    "Enrollment succeeded, but its status could not be rendered.",
                    retryable=True,
                ).as_result()

            # If the server created an enrollment but local persistence failed,
            # revoke with recovery authority.  A failed revoke retains only the
            # recovery tombstone and records revocation_pending.
            response_local = response
            record_local = credential_record
            # After a DEFINITE server mutation on the updating path the request's
            # certification boolean gives no way to know which certification
            # version the server recorded; only a successful, matching read-back
            # establishes it. Until that verification, any failure — an explicit
            # mismatch, a malformed 2xx body that raised before the read-back, a
            # failed/malformed read-back, or a control race — leaves a live
            # hosted enrollment whose consent record cannot be shown to match
            # what the user accepted, and the client cannot un-PATCH. Revoke it.
            #
            # The revoke identity must NOT come from the (possibly malformed)
            # response: on the updating path the enrollment id is the one we
            # PATCHed (existing.server_enrollment_id) and the recovery token is
            # the reissued one when it validated, else the pinned credential.
            updating_revoke_id: str | None = None
            updating_revoke_recovery: str | None = None
            updating_revoke_recovery_expiry: str | None = None
            if updating and existing is not None:
                known_id = str(existing.get("server_enrollment_id") or "")
                reissued = (
                    response_local.get("recovery_token")
                    if isinstance(response_local, dict)
                    else None
                )
                reissued_expiry = (
                    response_local.get("recovery_token_expires_at")
                    if isinstance(response_local, dict)
                    else None
                )
                pinned = (
                    credentials.get("recovery_token")
                    if isinstance(credentials, dict)
                    else None
                )
                pinned_expiry = (
                    credentials.get("recovery_token_expires_at")
                    if isinstance(credentials, dict)
                    else None
                )
                updating_revoke_id = known_id or None
                if isinstance(reissued, str) and reissued:
                    updating_revoke_recovery = reissued
                    updating_revoke_recovery_expiry = (
                        str(reissued_expiry) if reissued_expiry else None
                    )
                elif isinstance(pinned, str) and pinned:
                    updating_revoke_recovery = pinned
                    updating_revoke_recovery_expiry = (
                        str(pinned_expiry) if pinned_expiry else None
                    )
            revoke_unverified_certification = (
                updating
                and server_reauthorization_succeeded
                and not certification_verified
                and updating_revoke_id is not None
                and updating_revoke_recovery is not None
            )
            # Identity for revoking a definite server mutation: known values on
            # the updating path, the response on the create path (the only path
            # where the client cannot know the id ahead of the response).
            if revoke_unverified_certification:
                revoke_id = updating_revoke_id
                revoke_recovery = updating_revoke_recovery
                revoke_recovery_expiry = updating_revoke_recovery_expiry
            elif (
                not updating
                and isinstance(response_local, dict)
                and response_local.get("enrollment_id")
            ):
                revoke_id = str(response_local["enrollment_id"])
                revoke_recovery = (
                    record_local.get("recovery_token")
                    if isinstance(record_local, dict)
                    else response_local.get("recovery_token")
                )
                expiry_source = (
                    (record_local or {}).get("recovery_token_expires_at")
                    or response_local.get("recovery_token_expires_at")
                )
                revoke_recovery_expiry = (
                    str(expiry_source) if expiry_source else None
                )
            else:
                revoke_id = None
                revoke_recovery = None
                revoke_recovery_expiry = None

            if updating and not revoke_unverified_certification:
                update_auto_upload_enrollment(
                    conn,
                    expected_generation=generation,
                    mode="paused",
                    health="action_required",
                    last_result_code=error.code,
                )
            elif revoke_id and isinstance(revoke_recovery, str) and revoke_recovery:
                recovery = revoke_recovery
                if recovery:
                    try:
                        revoke_enrollment(
                            capabilities,
                            enrollment_id=revoke_id,
                            recovery_token=recovery,
                        )
                        with control_mutation_lock():
                            revoked_enrollment = get_auto_upload_enrollment(conn)
                            if (
                                revoked_enrollment is None
                                or revoked_enrollment.get("client_enrollment_id")
                                != client_enrollment_id
                            ):
                                raise ControlChanged(
                                    "Newer enrollment authority owns the credential store."
                                )
                            revoked_generation = int(
                                revoked_enrollment["generation"]
                            )
                            if revoked_generation == generation:
                                if not update_auto_upload_enrollment(
                                    conn,
                                    expected_generation=revoked_generation,
                                    mode="off",
                                    health="ready",
                                    client_enrollment_id=str(uuid.uuid4()),
                                    server_enrollment_id=None,
                                    authorization_revision=None,
                                    revocation_pending=False,
                                    last_result_code=error.code,
                                ):
                                    raise ControlChanged(
                                        "Controls changed before revocation cleanup."
                                    )
                                delete_credentials()
                            elif revoked_enrollment.get("mode") != "off":
                                raise ControlChanged(
                                    "Newer active enrollment owns the credential store."
                                )
                            elif not revoked_enrollment.get("revocation_pending"):
                                # Disable already won and recorded its own final
                                # outcome. Preserve that newer audit row; the
                                # stale hosted enrollment is now revoked. Rotate
                                # its spent create key before another Enable.
                                if not update_auto_upload_enrollment(
                                    conn,
                                    expected_generation=revoked_generation,
                                    client_enrollment_id=str(uuid.uuid4()),
                                ):
                                    raise ControlChanged(
                                        "Controls changed before create-key rotation."
                                    )
                                delete_credentials()
                    except Exception:
                        try:
                            tombstone = {
                                "issuer": capabilities["origin"],
                                "api_origin": capabilities["origin"],
                                "enrollment_id": revoke_id,
                                "active_token": None,
                                "active_token_expires_at": None,
                                "recovery_token": recovery,
                                # Expiry is resolved with the revoke identity:
                                # the reissued token's expiry when present, else
                                # the pinned credential's — never the malformed
                                # response, which may carry neither.
                                "recovery_token_expires_at": revoke_recovery_expiry
                                or "unknown",
                            }
                            with control_mutation_lock():
                                rollback_enrollment = get_auto_upload_enrollment(conn)
                                if (
                                    rollback_enrollment is None
                                    or rollback_enrollment.get("client_enrollment_id")
                                    != client_enrollment_id
                                    or (
                                        int(rollback_enrollment["generation"])
                                        != generation
                                        and rollback_enrollment.get("mode") != "off"
                                    )
                                ):
                                    raise ControlChanged(
                                        "Newer enrollment authority owns the credential store."
                                    )
                                rollback_generation = int(
                                    rollback_enrollment["generation"]
                                )
                                write_credentials(tombstone)
                                # Publishing new recovery work advances the
                                # generation. A Disable that started before
                                # this handoff can no longer clear it with a
                                # stale no-credential snapshot.
                                if not update_auto_upload_enrollment(
                                    conn,
                                    expected_generation=rollback_generation,
                                    generation=rollback_generation + 1,
                                    mode="off",
                                    health="retrying",
                                    server_enrollment_id=revoke_id,
                                    revocation_pending=True,
                                    last_result_code="revocation_pending",
                                ):
                                    raise ControlChanged(
                                        "Controls changed before recovery was recorded."
                                    )
                        except Exception:
                            pass
            else:
                update_auto_upload_enrollment(
                    conn,
                    expected_generation=generation,
                    mode="off",
                    health="action_required" if error.code == "control_changed" else "ready",
                    revocation_pending=False,
                    last_result_code=error.code,
                )
            return error.as_result()
        return status(conn=conn)
    except (AutoUploadError, CapabilityError, RecurringServiceError, CredentialStoreError) as exc:
        if isinstance(exc, (CapabilityError, RecurringServiceError)):
            return exc.as_result()
        if isinstance(exc, CredentialStoreError):
            return AutoUploadError("credential_store_failed", str(exc)).as_result()
        return exc.as_result()
    finally:
        if lock_context is not None:
            lock_context.__exit__(None, None, None)
        conn.close()


def pause() -> dict[str, Any]:
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        if enrollment is None or enrollment["mode"] == "off":
            return AutoUploadError("not_enabled", "Automatic upload is not enabled.").as_result()
        if enrollment["mode"] != "paused":
            generation = int(enrollment["generation"]) + 1
            if not update_auto_upload_enrollment(
                conn,
                expected_generation=int(enrollment["generation"]),
                mode="paused",
                generation=generation,
                last_result_code="paused",
            ):
                return AutoUploadError("control_conflict", "Automatic upload state changed.").as_result()
        return status(conn=conn)
    finally:
        conn.close()


def resume() -> dict[str, Any]:
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        if enrollment is None or enrollment["mode"] == "off":
            return AutoUploadError("not_enabled", "Automatic upload is not enabled.").as_result()
        credentials = load_credentials(required=True)
        if not credentials or not credentials.get("active_token"):
            return AutoUploadError(
                "credential_invalid",
                "Review scope and terms to restore upload authority.",
            ).as_result()
        config = load_config()
        backend = _resolved_ai_backend(config)
        try:
            profile = egress_profile_hash(
                conn,
                enrollment_scope={
                    "sources": enrollment["enrolled_sources"],
                    "projects": enrollment["enrolled_projects"],
                },
                api_origin=str(credentials["api_origin"]),
                ai_backend=backend,
                config=config,
            )
        except AutoUploadError as exc:
            return exc.as_result()
        if profile != enrollment.get("egress_profile_hash"):
            return AutoUploadError(
                "profile_changed",
                "Review and accept the current scope and privacy profile before resuming.",
            ).as_result()
        generation = int(enrollment["generation"]) + 1
        if not update_auto_upload_enrollment(
            conn,
            expected_generation=int(enrollment["generation"]),
            mode="enabled",
            health="ready",
            generation=generation,
            last_result_code="resumed",
        ):
            return AutoUploadError("control_conflict", "Automatic upload state changed.").as_result()
        return status(conn=conn)
    except CredentialStoreError as exc:
        return AutoUploadError("credential_store_failed", str(exc)).as_result()
    finally:
        conn.close()


def disable() -> dict[str, Any]:
    """Remove local upload authority first, then revoke with recovery authority."""

    conn = open_index()
    try:
        # Establish Off and strip active authority as one ordered local phase.
        # Hosted revocation stays outside the lock; every later DB write is a
        # CAS against the generation this Disable owns.
        with control_mutation_lock():
            enrollment = get_auto_upload_enrollment(conn)
            disable_generation = (
                int(enrollment["generation"])
                if enrollment is not None
                else None
            )
            pending_create_intent = bool(
                enrollment is not None
                and enrollment.get("mode") == "off"
                and enrollment.get("last_result_code") == "enrollment_pending"
            )
            revocation_needed = bool(
                enrollment is not None
                and (
                    enrollment.get("mode") != "off"
                    or enrollment.get("revocation_pending")
                )
            )
            if enrollment is not None and (
                revocation_needed or pending_create_intent
            ):
                old_generation = int(enrollment["generation"])
                disable_generation = old_generation + 1
                if not update_auto_upload_enrollment(
                    conn,
                    expected_generation=old_generation,
                    mode="off",
                    health="ready",
                    generation=disable_generation,
                    # A first-create intent has no remote authority yet. Its
                    # enable owner revokes if the request later succeeds.
                    revocation_pending=revocation_needed,
                    current_run_id=None,
                    current_run_stage=None,
                    last_result_code="disabling",
                ):
                    return AutoUploadError(
                        "control_conflict", "Automatic upload state changed."
                    ).as_result()

            # Remove local upload authority before touching hooks or network.
            try:
                credentials = remove_active_token()
            except CredentialStoreError as exc:
                if enrollment is not None:
                    update_auto_upload_enrollment(
                        conn,
                        expected_generation=disable_generation,
                        health="action_required",
                        last_result_code="credential_store_failed",
                    )
                return AutoUploadError(
                    "credential_store_failed", str(exc)
                ).as_result()

        hook_error = False
        for target in SUPPORTED_HOOK_TARGETS:
            try:
                uninstall_agent_hook(target)
            except Exception:
                hook_error = True

        enrollment_id = (
            str(credentials.get("enrollment_id"))
            if credentials and credentials.get("enrollment_id")
            else str(enrollment.get("server_enrollment_id") or "")
            if enrollment is not None
            else ""
        )
        if credentials and credentials.get("recovery_token"):
            # A recovery tombstone is itself evidence that revocation or
            # receipt reconciliation is not durably complete, even if a prior
            # crash left the SQLite overlay stale.
            revocation_needed = True
            if enrollment is not None and not enrollment.get("revocation_pending"):
                update_auto_upload_enrollment(
                    conn,
                    expected_generation=disable_generation,
                    revocation_pending=True,
                    health="retrying",
                    last_result_code="revocation_pending",
                )

        # Sealed is definitely pre-send, so disabling deletes it rather than
        # carrying bytes across an authorization boundary.  Submitting remains
        # ambiguous and is reconciled with recovery-only authority below.
        if enrollment_id:
            sealed_rows = conn.execute(
                "SELECT share_id FROM shares WHERE submission_channel = 'auto_weekly' "
                "AND submission_state = 'sealed' AND enrollment_id = ?",
                (enrollment_id,),
            ).fetchall()
            for row in sealed_rows:
                _cleanup_unsent_draft(conn, str(row["share_id"]), allow_sealed=True)

        if credentials is None or not credentials.get("recovery_token"):
            if enrollment is not None and revocation_needed:
                update_auto_upload_enrollment(
                    conn,
                    expected_generation=disable_generation,
                    health="action_required",
                    revocation_pending=True,
                    last_result_code="recovery_credential_missing",
                )
            elif enrollment is not None:
                # Successful disable is idempotent.  Re-running it may still
                # repair a previously failed hook cleanup, but the absence of
                # a recovery credential is expected once remote revocation is
                # definite and must not resurrect Revocation pending.
                update_auto_upload_enrollment(
                    conn,
                    expected_generation=disable_generation,
                    health="action_required" if hook_error else "ready",
                    revocation_pending=False,
                    next_retry_at=None,
                    last_result_code=(
                        "hook_cleanup_failed" if hook_error else "disabled"
                    ),
                )
            return status(conn=conn)

        pinned_origin = str(credentials["api_origin"])
        capabilities = recovery_capabilities(pinned_origin)
        unresolved = False
        try:
            # Revoke before deciding that an in-flight key has no receipt.
            # A runner may already have committed ``submitting`` and be inside
            # its POST when Disable removes the local active token.  The
            # hosted revoke boundary serializes with that POST: once revoke
            # returns, the request either committed a receipt or can no longer
            # be accepted.  A pre-revoke 404 cannot provide that guarantee.
            revoke_enrollment(
                capabilities,
                enrollment_id=enrollment_id,
                recovery_token=str(credentials["recovery_token"]),
            )
            submitting_rows = conn.execute(
                "SELECT * FROM shares WHERE submission_channel = 'auto_weekly' "
                "AND submission_state = 'submitting' AND enrollment_id = ?",
                (enrollment_id,),
            ).fetchall()
            for raw_row in submitting_rows:
                share = dict(raw_row)
                try:
                    receipt = lookup_receipt(
                        capabilities,
                        client_submission_id=str(share["client_submission_id"]),
                        token=str(credentials["recovery_token"]),
                    )
                except RecurringServiceError as exc:
                    if exc.code == "receipt_not_found":
                        conn.execute(
                            "UPDATE shares SET submission_state = 'not_found' "
                            "WHERE share_id = ? AND submission_state = 'submitting'",
                            (share["share_id"],),
                        )
                        conn.commit()
                    else:
                        unresolved = True
                else:
                    _commit_receipt(conn, share_id=str(share["share_id"]), receipt=receipt)

            if not unresolved:
                with control_mutation_lock():
                    cleared = enrollment is None or update_auto_upload_enrollment(
                        conn,
                        expected_generation=disable_generation,
                        revocation_pending=False,
                        health="action_required" if hook_error else "ready",
                        next_retry_at=None,
                        last_result_code=(
                            "hook_cleanup_failed" if hook_error else "disabled"
                        ),
                    )
                    # Delete the sole recovery credential only after the CAS
                    # proves no newer recovery handoff superseded this Disable.
                    if cleared:
                        delete_credentials()
            else:
                if enrollment is not None:
                    update_auto_upload_enrollment(
                        conn,
                        expected_generation=disable_generation,
                        health="retrying",
                        last_result_code="receipt_reconciliation_pending",
                        next_retry_at=_iso(_now() + timedelta(minutes=15)),
                    )
        except (
            AutoUploadError,
            RecurringServiceError,
            CapabilityError,
            CredentialStoreError,
        ):
            if enrollment is not None:
                update_auto_upload_enrollment(
                    conn,
                    expected_generation=disable_generation,
                    health="retrying",
                    revocation_pending=True,
                    last_result_code="revocation_pending",
                    next_retry_at=_iso(_now() + timedelta(minutes=15)),
                )
        return status(conn=conn)
    finally:
        conn.close()


def _raw_fingerprints(
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, RawFingerprint]:
    fingerprints: dict[str, RawFingerprint] = {}
    for candidate in candidates:
        session_id = str(candidate.get("session_id") or "")
        raw_path = candidate.get("raw_source_path")
        if not session_id or not isinstance(raw_path, str) or not raw_path:
            raise AutoUploadError(
                "raw_source_unavailable",
                "A selected trace no longer has a verifiable raw source.",
            )
        try:
            fingerprints[session_id] = fingerprint_raw_source(raw_path)
        except (OSError, RawSourceChanged) as exc:
            raise AutoUploadError(
                "raw_source_unavailable",
                "A selected trace's raw source is unavailable.",
            ) from exc
    return fingerprints


def _bind_scan_fingerprints(
    scan: Mapping[str, Any],
    candidates: Sequence[Mapping[str, Any]],
    current: Mapping[str, RawFingerprint],
) -> None:
    """Bind packaging to the exact raw snapshots returned by strict parsing."""

    encoded = scan.get("raw_fingerprints")
    if encoded is None:
        # Test doubles written before the strict-snapshot contract omit this
        # diagnostic.  Production Scanner reports it on every strict pass.
        return
    if not isinstance(encoded, Mapping):
        raise ControlChanged("The strict scan returned invalid raw-source snapshots.")
    selected_ids = [str(item.get("session_id") or "") for item in candidates]
    expected: dict[str, RawFingerprint] = {}
    for session_id in selected_ids:
        value = encoded.get(session_id)
        if (
            not session_id
            or not isinstance(value, (list, tuple))
            or len(value) != 5
            or not isinstance(value[4], str)
        ):
            raise ControlChanged(
                "A selected trace was not bound to the strict parser snapshot."
            )
        expected[session_id] = (
            int(value[0]),
            int(value[1]),
            int(value[2]),
            int(value[3]),
            value[4],
        )
    if dict(current) != expected:
        raise ControlChanged("A selected raw source changed after the strict parse.")


def _ranked_size_prefix(
    conn: sqlite3.Connection,
    candidates: Sequence[Mapping[str, Any]],
    *,
    settings: Mapping[str, Any],
    maximum_bundle_size: int,
) -> tuple[list[dict[str, Any]], int, int]:
    """Choose the largest conservative ranked prefix before any AI call.

    Returns ``(selected, deferred_by_size, missing)`` — ``missing`` counts
    candidates whose session row vanished before the size ``break``.
    """

    # ZIP can fail to compress. Apply the exact deterministic share redaction
    # locally before any AI call, then budget those serialized bytes plus
    # fixed manifest/scan and per-entry JSON/ZIP overhead. The final sealed ZIP
    # remains authoritative and never triggers a second repack.
    fixed_overhead = 256 * 1024
    per_trace_overhead = 64 * 1024
    used = fixed_overhead
    selected: list[dict[str, Any]] = []
    missing = 0
    for candidate in candidates:
        detail = get_session_detail(conn, str(candidate.get("session_id") or ""))
        if detail is None:
            # The session row vanished between the candidate report and this
            # read (e.g. the scanner pruned a deleted raw log). It cannot be
            # packaged; skip it so the remaining ranked candidates can still
            # ship, and let the caller tell "vanished" apart from "oversized".
            missing += 1
            continue
        redacted, _count, _log = apply_share_redactions(
            conn,
            detail,
            custom_strings=list(settings.get("custom_strings") or []),
            user_allowlist=list(settings.get("allowlist_entries") or []),
            extra_usernames=list(settings.get("extra_usernames") or []),
            blocked_domains=list(settings.get("blocked_domains") or []),
        )
        serialized_size = len(
            json.dumps(redacted, default=str, separators=(",", ":")).encode("utf-8")
        )
        projected = used + serialized_size + per_trace_overhead
        if projected > maximum_bundle_size:
            break
        selected.append(dict(candidate))
        used = projected
    deferred_by_size = max(0, len(candidates) - len(selected) - missing)
    return selected, deferred_by_size, missing


def _current_revisions(
    conn: sqlite3.Connection, session_ids: Sequence[str]
) -> dict[str, str | None]:
    if not session_ids:
        return {}
    placeholders = ",".join("?" for _ in session_ids)
    return {
        str(row["session_id"]): row["content_revision"]
        for row in conn.execute(
            f"SELECT session_id, content_revision FROM sessions WHERE session_id IN ({placeholders})",
            list(session_ids),
        ).fetchall()
    }


def _assert_control_state(
    *,
    expected_generation: int,
    expected_profile_hash: str,
    expected_revisions: Mapping[str, str],
    api_origin: str,
    ai_backend: str | None,
    require_due: bool = False,
) -> None:
    conn = open_index()
    try:
        enrollment = get_auto_upload_enrollment(conn)
        if (
            enrollment is None
            or enrollment.get("mode") != "enabled"
            or int(enrollment.get("generation", 0)) != expected_generation
        ):
            raise ControlChanged()
        config = _load_config_readonly()
        current_scope = _current_scope(conn, config)
        if current_scope["blockers"] or not set(enrollment["enrolled_sources"]).issubset(
            set(current_scope["sources"])
        ) or not set(enrollment["enrolled_projects"]).issubset(
            set(current_scope["projects"])
        ):
            raise ControlChanged("The confirmed source/project scope changed during the run.")
        current_profile = egress_profile_hash(
            conn,
            enrollment_scope={
                "sources": enrollment["enrolled_sources"],
                "projects": enrollment["enrolled_projects"],
            },
            api_origin=api_origin,
            ai_backend=ai_backend,
            config=config,
        )
        if current_profile != expected_profile_hash:
            raise ControlChanged("The accepted egress profile changed during the run.")
        session_ids = list(expected_revisions)
        if release_gate_blockers(conn, session_ids):
            raise ControlChanged("A selected trace is no longer released for sharing.")
        if auto_upload_review_blockers(conn, session_ids):
            raise ControlChanged("A selected trace no longer clears automatic review gates.")
        if _current_revisions(conn, session_ids) != dict(expected_revisions):
            raise ControlChanged("A selected trace revision changed during the run.")
        if require_due and not due_decision(enrollment).due:
            raise ControlChanged("The scheduled cycle is no longer due.")
    finally:
        conn.close()


def _start_run(
    conn: sqlite3.Connection,
    enrollment: Mapping[str, Any],
    *,
    run_id: str,
) -> bool:
    return update_auto_upload_enrollment(
        conn,
        expected_generation=int(enrollment["generation"]),
        current_run_id=run_id,
        current_run_stage="starting",
    )


def _set_run_stage(
    conn: sqlite3.Connection,
    *,
    generation: int,
    run_id: str,
    stage: str,
) -> bool:
    cursor = conn.execute(
        "UPDATE auto_upload_enrollment SET current_run_stage = ?, updated_at = ? "
        "WHERE singleton_id = 1 AND generation = ? AND current_run_id = ?",
        (stage, _iso(_now()), generation, run_id),
    )
    conn.commit()
    updated = cursor.rowcount == 1
    if updated:
        _write_telemetry("stage", stage=stage)
    return updated


def _clear_run_overlay(run_id: str) -> None:
    conn = open_index()
    try:
        conn.execute(
            "UPDATE auto_upload_enrollment SET current_run_id = NULL, "
            "current_run_stage = NULL, updated_at = ? "
            "WHERE singleton_id = 1 AND current_run_id = ?",
            (_iso(_now()), run_id),
        )
        conn.commit()
    finally:
        conn.close()


def _record_cycle_result(
    conn: sqlite3.Connection,
    *,
    generation: int,
    code: str,
    count: int | None = None,
    receipt_reference: str | None = None,
    success: bool = False,
    retryable: bool = False,
    retry_after: int | None = None,
    action_required: bool = False,
) -> None:
    enrollment = get_auto_upload_enrollment(conn)
    if enrollment is None or int(enrollment["generation"]) != generation:
        return
    changes: dict[str, Any] = {
        "last_result_code": code,
        "last_result_count": count,
        "last_receipt_reference": receipt_reference,
    }
    if success:
        changes.update(
            health="ready",
            last_completed_at=_iso(_now()),
            next_retry_at=None,
            consecutive_failures=0,
        )
    elif action_required:
        changes.update(health="action_required", next_retry_at=None)
    elif retryable:
        failures = int(enrollment.get("consecutive_failures") or 0) + 1
        delay = retry_after or min(
            BACKOFF_MAX_SECONDS,
            BACKOFF_BASE_SECONDS * (2 ** min(failures - 1, 8)),
        )
        changes.update(
            health="retrying",
            consecutive_failures=failures,
            next_retry_at=_iso(_now() + timedelta(seconds=delay)),
        )
    update_auto_upload_enrollment(
        conn,
        expected_generation=generation,
        **changes,
    )


def _record_recovery_backoff(
    conn: sqlite3.Connection,
    *,
    generation: int,
    retry_after: int | None = None,
) -> None:
    """Pace a failed receipt recovery without touching durable status fields.

    The hook due-check forces a 'submitting' share immediately due unless
    ``next_retry_at`` is in the future, so a failing (or unresolvable) receipt
    lookup must stamp the retry clock or every SessionStart relaunches a
    network-calling runner. But receipt recovery deliberately runs before
    every lifecycle gate — on paused, off, and ``action_required`` enrollments
    — where ``health``/``last_result_code``/``last_receipt_reference`` carry
    durable overlays a read-only probe must never rewrite: a durable
    ``action_required`` gates automatic egress until the user reviews it,
    Disable owns its revocation overlay, and the Pause-reauthorization merge
    keys off ``last_result_code``. Stamp ONLY ``consecutive_failures`` and
    ``next_retry_at`` (the two fields the throttle needs), unlike
    ``_record_cycle_result`` which rewrites the user-facing status.
    """

    enrollment = get_auto_upload_enrollment(conn)
    if enrollment is None or int(enrollment["generation"]) != generation:
        return
    failures = int(enrollment.get("consecutive_failures") or 0) + 1
    delay = retry_after or min(
        BACKOFF_MAX_SECONDS,
        BACKOFF_BASE_SECONDS * (2 ** min(failures - 1, 8)),
    )
    update_auto_upload_enrollment(
        conn,
        expected_generation=generation,
        consecutive_failures=failures,
        next_retry_at=_iso(_now() + timedelta(seconds=delay)),
    )


def _checkpoint_recovered_receipt(
    conn: sqlite3.Connection,
    result: Mapping[str, Any],
) -> None:
    """Advance cadence after receipt recovery without clearing a safety action."""

    current = get_auto_upload_enrollment(conn)
    if current is None or current.get("mode") not in {"enabled", "paused"}:
        # Disable owns its revocation/reconciliation retry overlay. The atomic
        # receipt commit already preserves the receipt reference while Off.
        return
    changes: dict[str, Any] = {
        "last_completed_at": _iso(_now()),
        "next_retry_at": None,
        "consecutive_failures": 0,
        "last_result_count": int(result.get("count") or 0),
        "last_receipt_reference": result.get("receipt_reference"),
    }
    if current.get("health") != "action_required":
        changes.update(health="ready", last_result_code="uploaded")
    update_auto_upload_enrollment(
        conn,
        expected_generation=int(current["generation"]),
        **changes,
    )


def _cleanup_unsent_draft(
    conn: sqlite3.Connection,
    share_id: str,
    *,
    allow_sealed: bool = False,
) -> None:
    row = conn.execute(
        "SELECT status, shared_at, hosted_receipt_id, submission_state, sealed_artifact_path "
        "FROM shares WHERE share_id = ?",
        (share_id,),
    ).fetchone()
    if row is None:
        return
    protected_states = {"submitting", "accepted"}
    if not allow_sealed:
        protected_states.add("sealed")
    if (
        row["shared_at"] is not None
        or row["hosted_receipt_id"] is not None
        or row["submission_state"] in protected_states
    ):
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            "UPDATE sessions SET share_id = NULL WHERE share_id = ?", (share_id,)
        )
        conn.execute("DELETE FROM share_sessions WHERE share_id = ?", (share_id,))
        conn.execute("DELETE FROM shares WHERE share_id = ?", (share_id,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    artifact_dir = Path(config_module.CONFIG_DIR) / "shares" / share_id
    try:
        if artifact_dir.resolve().is_relative_to(
            (Path(config_module.CONFIG_DIR) / "shares").resolve()
        ):
            shutil.rmtree(artifact_dir, ignore_errors=True)
    except (OSError, ValueError):
        pass


def _write_sealed_zip(export_dir: Path, zip_bytes: bytes) -> tuple[Path, str]:
    guarded_root = (Path(config_module.CONFIG_DIR) / "shares").resolve()
    resolved_export = export_dir.resolve()
    if not resolved_export.is_relative_to(guarded_root):
        raise AutoUploadError(
            "artifact_path_invalid",
            "The recurring share artifact path is outside the guarded share area.",
        )
    artifact_path = resolved_export / SEALED_ZIP_FILENAME
    fd = -1
    temp_path: str | None = None
    try:
        fd, temp_path = tempfile.mkstemp(
            dir=resolved_export, prefix=".auto-upload-seal-", suffix=".tmp"
        )
        if os.name == "nt":
            os.chmod(temp_path, 0o600)
        else:
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as file:
            fd = -1
            file.write(zip_bytes)
            file.flush()
            os.fsync(file.fileno())
        assert temp_path is not None
        os.replace(temp_path, artifact_path)
        temp_path = None
        if hasattr(os, "O_DIRECTORY"):
            dir_fd = os.open(resolved_export, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
    except OSError as exc:
        raise AutoUploadError(
            "artifact_seal_failed",
            "The exact recurring ZIP could not be persisted durably.",
        ) from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if temp_path is not None:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    digest = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
    return artifact_path, digest


def _seal_share_ledger(
    conn: sqlite3.Connection,
    *,
    share_id: str,
    enrollment: Mapping[str, Any],
    artifact_path: Path,
    artifact_sha256: str,
    raw_fingerprints: Mapping[str, RawFingerprint],
) -> str:
    client_submission_id = str(uuid.uuid4())
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = conn.execute(
            "SELECT mode, generation, egress_profile_hash FROM auto_upload_enrollment "
            "WHERE singleton_id = 1"
        ).fetchone()
        if (
            current is None
            or current["mode"] != "enabled"
            or int(current["generation"]) != int(enrollment["generation"])
            or current["egress_profile_hash"] != enrollment["egress_profile_hash"]
        ):
            raise ControlChanged()
        session_ids = [
            row["session_id"]
            for row in conn.execute(
                "SELECT session_id FROM share_sessions WHERE share_id = ?",
                (share_id,),
            ).fetchall()
        ]
        if (
            release_gate_blockers(conn, session_ids)
            or auto_upload_review_blockers(conn, session_ids)
            or share_revision_blockers(conn, share_id)
        ):
            raise ControlChanged("A share gate changed before artifact sealing.")
        cursor = conn.execute(
            "UPDATE shares SET submission_channel = 'auto_weekly', enrollment_id = ?, "
            "client_submission_id = ?, authorization_revision = ?, "
            "submission_state = 'sealed', sealed_artifact_sha256 = ?, "
            "sealed_artifact_path = ?, sealed_raw_fingerprints = ? "
            "WHERE share_id = ? AND shared_at IS NULL "
            "AND hosted_receipt_id IS NULL",
            (
                enrollment["server_enrollment_id"],
                client_submission_id,
                enrollment["authorization_revision"],
                artifact_sha256,
                str(artifact_path),
                json.dumps(
                    {key: list(value) for key, value in sorted(raw_fingerprints.items())},
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                share_id,
            ),
        )
        if cursor.rowcount != 1:
            raise AutoUploadError(
                "ledger_conflict", "The recurring share ledger changed before sealing."
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return client_submission_id


def _transition_submission(
    conn: sqlite3.Connection,
    *,
    share_id: str,
    from_state: str,
    to_state: str,
    generation: int,
) -> bool:
    conn.execute("BEGIN IMMEDIATE")
    try:
        enrollment = conn.execute(
            "SELECT mode, generation FROM auto_upload_enrollment WHERE singleton_id = 1"
        ).fetchone()
        if (
            enrollment is None
            or enrollment["mode"] != "enabled"
            or int(enrollment["generation"]) != generation
        ):
            conn.rollback()
            return False
        session_ids = [
            row["session_id"]
            for row in conn.execute(
                "SELECT session_id FROM share_sessions WHERE share_id = ?",
                (share_id,),
            ).fetchall()
        ]
        if (
            release_gate_blockers(conn, session_ids)
            or auto_upload_review_blockers(conn, session_ids)
            or share_revision_blockers(conn, share_id)
        ):
            conn.rollback()
            return False
        cursor = conn.execute(
            "UPDATE shares SET submission_state = ? WHERE share_id = ? "
            "AND submission_state = ?",
            (to_state, share_id, from_state),
        )
        if to_state == "submitting" and cursor.rowcount == 1:
            # Entering 'submitting' means every gate just passed, so any stale
            # next_retry_at (and the failure exponent behind it) from earlier
            # unrelated failures is obsolete — clear both in the same
            # transaction. A crash mid-POST then leaves 'submitting' with no
            # backoff, keeping the first receipt reconcile immediately due;
            # only a failure of THIS share's submit/reconcile can stamp a
            # fresh backoff afterwards (starting from a fresh exponent), and
            # the hook due-check rightly waits that one out.
            conn.execute(
                "UPDATE auto_upload_enrollment SET next_retry_at = NULL, "
                "consecutive_failures = 0, updated_at = ? "
                "WHERE singleton_id = 1 AND generation = ?",
                (_iso(_now()), generation),
            )
        conn.commit()
        return cursor.rowcount == 1
    except Exception:
        conn.rollback()
        raise


def _artifact_revision_keys(conn: sqlite3.Connection, share_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT session_id, content_revision FROM share_sessions "
        "WHERE share_id = ? ORDER BY session_id",
        (share_id,),
    ).fetchall()
    keys: list[str] = []
    for row in rows:
        if not row["content_revision"]:
            raise AutoUploadError(
                "revision_missing", "A sealed share is missing its revision identity."
            )
        keys.append(trace_revision_key(row["session_id"], row["content_revision"]))
    return keys


def _commit_receipt(
    conn: sqlite3.Connection,
    *,
    share_id: str,
    receipt: Mapping[str, Any],
) -> str:
    receipt_id = receipt.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        raise AutoUploadError(
            "malformed_response", "Hosted recurring upload returned no receipt."
        )
    accepted_at = receipt.get("accepted_at") or receipt.get("shared_at")
    shared_at = str(accepted_at) if _parse_time(accepted_at) else _iso(_now())
    checkpoint_at = _iso(_now())
    conn.execute("BEGIN IMMEDIATE")
    try:
        share = conn.execute(
            "SELECT enrollment_id, submission_state, hosted_receipt_id FROM shares "
            "WHERE share_id = ?",
            (share_id,),
        ).fetchone()
        if share is None:
            raise AutoUploadError(
                "receipt_commit_conflict", "The hosted receipt could not be committed."
            )
        if share["submission_state"] in {"sealed", "submitting"}:
            conn.execute(
                "UPDATE shares SET status = 'shared', shared_at = ?, hosted_receipt_id = ?, "
                "hosted_status = ?, submission_state = 'accepted' WHERE share_id = ?",
                (
                    shared_at,
                    receipt_id,
                    str(receipt.get("status") or "accepted"),
                    share_id,
                ),
            )
        elif share["hosted_receipt_id"] != receipt_id:
            raise AutoUploadError(
                "receipt_commit_conflict", "The hosted receipt could not be committed."
            )

        count = int(
            conn.execute(
                "SELECT COUNT(*) FROM share_sessions WHERE share_id = ?", (share_id,)
            ).fetchone()[0]
        )
        enrollment = conn.execute(
            "SELECT mode, health, last_result_code FROM auto_upload_enrollment "
            "WHERE singleton_id = 1 "
            "AND server_enrollment_id = ?",
            (share["enrollment_id"],),
        ).fetchone()
        if enrollment is not None:
            # The receipt and cadence checkpoint are one crash-atomic unit.
            # Otherwise a process death here could leave the cycle due and
            # submit another capped batch immediately after restart.
            if enrollment["mode"] in {"enabled", "paused"}:
                preserve_action = enrollment["health"] == "action_required"
                conn.execute(
                    "UPDATE auto_upload_enrollment SET last_completed_at = ?, "
                    "next_retry_at = NULL, consecutive_failures = 0, "
                    "last_result_count = ?, last_receipt_reference = ?, "
                    "health = ?, last_result_code = ?, updated_at = ? "
                    "WHERE singleton_id = 1 AND server_enrollment_id = ?",
                    (
                        checkpoint_at,
                        count,
                        receipt_id,
                        "action_required" if preserve_action else "ready",
                        enrollment["last_result_code"] if preserve_action else "uploaded",
                        checkpoint_at,
                        share["enrollment_id"],
                    ),
                )
            else:
                # Disable owns the revocation retry fields, but a receipt found
                # during that flow is still durable status information.
                conn.execute(
                    "UPDATE auto_upload_enrollment SET last_result_count = ?, "
                    "last_receipt_reference = ?, updated_at = ? "
                    "WHERE singleton_id = 1 AND server_enrollment_id = ?",
                    (count, receipt_id, checkpoint_at, share["enrollment_id"]),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return receipt_id


def _pending_submission(
    conn: sqlite3.Connection,
    *,
    enrollment_id: str | None = None,
) -> dict[str, Any] | None:
    enrollment_clause = " AND enrollment_id = ?" if enrollment_id is not None else ""
    params: tuple[Any, ...] = (enrollment_id,) if enrollment_id is not None else ()
    row = conn.execute(
        "SELECT * FROM shares WHERE submission_channel = 'auto_weekly' "
        "AND submission_state IN ('sealed', 'submitting') "
        f"{enrollment_clause} ORDER BY created_at LIMIT 1",
        params,
    ).fetchone()
    return dict(row) if row is not None else None


def _validate_sealed_artifact(share: Mapping[str, Any]) -> tuple[Path, str]:
    path_value = share.get("sealed_artifact_path")
    expected_hash = share.get("sealed_artifact_sha256")
    if not isinstance(path_value, str) or not isinstance(expected_hash, str):
        raise AutoUploadError(
            "sealed_artifact_missing", "The recurring recovery ledger is incomplete."
        )
    path = Path(path_value)
    try:
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise AutoUploadError(
            "sealed_artifact_missing", "The exact sealed recurring ZIP is unavailable."
        ) from exc
    if not hmac.compare_digest(actual, expected_hash):
        raise AutoUploadError(
            "sealed_artifact_changed", "The sealed recurring ZIP no longer matches its ledger hash."
        )
    return path, actual


def _submit_pending_artifact(
    conn: sqlite3.Connection,
    *,
    share: Mapping[str, Any],
    enrollment: Mapping[str, Any],
    credentials: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    ai_backend: str | None = None,
) -> dict[str, Any]:
    if share.get("enrollment_id") != enrollment.get("server_enrollment_id"):
        raise AutoUploadError(
            "enrollment_mismatch",
            "A sealed artifact belongs to a different recurring enrollment.",
        )
    if int(share.get("authorization_revision") or 0) != int(
        enrollment.get("authorization_revision") or 0
    ):
        raise AutoUploadError(
            "authorization_version_mismatch",
            "A sealed artifact belongs to a different authorization revision.",
        )
    artifact_path, artifact_hash = _validate_sealed_artifact(share)
    _validate_raw_fingerprint_ledger(conn, share)
    active_token = credentials.get("active_token")
    if not isinstance(active_token, str) or not active_token:
        raise AutoUploadError(
            "credential_invalid", "The active recurring-upload credential is unavailable."
        )
    # Refresh public capability and protected enrollment state at the final
    # egress boundary.  A closure, terms change, origin drift, or smaller size
    # limit wins without sending content.
    capabilities = fetch_capabilities(force=True, require_enrollment_open=False)
    if (
        credentials.get("api_origin") != capabilities.get("origin")
        or credentials.get("issuer") != capabilities.get("origin")
    ):
        raise AutoUploadError(
            "destination_changed", "The exact hosted recurring-upload origin changed."
        )
    try:
        artifact_size = artifact_path.stat().st_size
    except OSError as exc:
        raise AutoUploadError(
            "sealed_artifact_missing", "The exact sealed recurring ZIP is unavailable."
        ) from exc
    if artifact_size > int(capabilities["maximum_bundle_size"]):
        raise AutoUploadError(
            "payload_too_large", "The sealed recurring ZIP exceeds the current hosted limit."
        )
    _server_enrollment_gate(capabilities, enrollment, credentials)
    # Re-validate the raw fingerprint ledger immediately BEFORE the locked
    # section, never inside it: raw session files are unrelated to privacy-
    # config writes, so hashing up to five (potentially very large) session
    # logs while holding the egress lock would stall every concurrent
    # save_config for the full, size-unbounded re-hash. Only the profile-hash
    # recompute and the submitting transition need the lock for atomicity vs
    # profile writers.
    # Capture the cheap, read-free stat baseline BEFORE the final fingerprint
    # validation below, so the baseline describes — at the latest — the very
    # snapshot validation confirms against the sealed ledger. A raw append or
    # replace after that snapshot (including one that lands during the possibly
    # long wait to acquire the egress lock, when the indexed revision stays
    # unchanged) is then visible to the post-lock stat comparison. Capturing the
    # baseline after validation would instead bake a change made in that gap
    # into the baseline, and check_raw_fingerprints=False would let it ship.
    pre_lock_raw_stats = _raw_stat_signatures(conn, share)
    _validate_raw_fingerprint_ledger(conn, share)
    # The protected server check above can take long enough for a local pause,
    # hold, revision, or privacy-profile edit to win. Profile writers share
    # this short cross-process boundary lock; DB policy writers atomically bump
    # the enrollment generation in their own transaction. Therefore the final
    # recompute and submitting transition have a definite order relative to
    # every supported privacy-setting mutation.
    with config_module.auto_upload_egress_lock():
        # A raw source that changed after the validated snapshot — including one
        # that lands while we wait for the lock — must abort before egress. The
        # comparison is stat-only (no file reads), so the size-unbounded raw
        # re-hash never runs under the lock (which is why
        # check_raw_fingerprints=False below).
        if _raw_stat_signatures(conn, share) != pre_lock_raw_stats:
            raise ControlChanged(
                "A selected raw trace changed after its validated snapshot."
            )
        _validate_pending_for_submission(
            conn,
            share=share,
            enrollment=enrollment,
            expected_profile_hash=str(enrollment["egress_profile_hash"]),
            api_origin=str(credentials["api_origin"]),
            ai_backend=ai_backend,
            check_raw_fingerprints=False,
        )
        state = str(share.get("submission_state"))
        if state not in {"sealed", "submitting"} or not _transition_submission(
            conn,
            share_id=str(share["share_id"]),
            from_state=state,
            to_state="submitting",
            generation=int(enrollment["generation"]),
        ):
            raise ControlChanged("Controls or share gates changed before submission.")
    try:
        receipt = submit_artifact(
            capabilities,
            active_token=active_token,
            artifact_path=artifact_path,
            client_submission_id=str(share["client_submission_id"]),
            authorization_revision=int(share["authorization_revision"]),
            trace_revision_keys=_artifact_revision_keys(conn, str(share["share_id"])),
            artifact_sha256=artifact_hash,
        )
    except RecurringServiceError as exc:
        if exc.ambiguous:
            raise
        next_state = "sealed" if exc.retryable else "rejected"
        conn.execute(
            "UPDATE shares SET submission_state = ? WHERE share_id = ? "
            "AND submission_state = 'submitting'",
            (next_state, share["share_id"]),
        )
        conn.commit()
        raise
    receipt_id = _commit_receipt(
        conn, share_id=str(share["share_id"]), receipt=receipt
    )
    return {
        "ok": True,
        "code": "uploaded",
        "count": len(_artifact_revision_keys(conn, str(share["share_id"]))),
        "receipt_reference": receipt_id,
    }


def _reconcile_pending(
    conn: sqlite3.Connection,
    *,
    enrollment: Mapping[str, Any],
    credentials: Mapping[str, Any],
    capabilities: Mapping[str, Any],
    allow_submit: bool = True,
    ai_backend: str | None = None,
) -> dict[str, Any] | None:
    share = _pending_submission(
        conn, enrollment_id=str(enrollment.get("server_enrollment_id") or "")
    )
    if share is None:
        return None
    if share.get("submission_state") == "submitting":
        token = credentials.get("active_token") or credentials.get("recovery_token")
        if not isinstance(token, str) or not token:
            raise AutoUploadError(
                "credential_invalid", "No credential is available to reconcile the receipt."
            )
        try:
            receipt = lookup_receipt(
                capabilities,
                client_submission_id=str(share["client_submission_id"]),
                token=token,
            )
        except RecurringServiceError as exc:
            if exc.code != "receipt_not_found":
                raise
            # Serialize this collapse with Disable. Disable is deliberately
            # allowed to race the whole-run lock, and it owns the only safe
            # ``not_found`` transition after the hosted revoke boundary. A
            # stale runner must not recreate a sealed artifact after Disable
            # has swept drafts and then strand it without recovery authority.
            conn.execute("BEGIN IMMEDIATE")
            try:
                current = conn.execute(
                    "SELECT mode, server_enrollment_id FROM auto_upload_enrollment "
                    "WHERE singleton_id = 1"
                ).fetchone()
                if (
                    current is None
                    or current["server_enrollment_id"] != share.get("enrollment_id")
                ):
                    raise ControlChanged(
                        "The recurring enrollment changed during receipt reconciliation."
                    )
                live_mode = str(current["mode"])
                if live_mode == "off":
                    conn.commit()
                    return {
                        "ok": False,
                        "code": "disabled",
                        "message": "Disable is reconciling the in-flight receipt.",
                    }
                cursor = conn.execute(
                    "UPDATE shares SET submission_state = 'sealed' WHERE share_id = ? "
                    "AND submission_state = 'submitting'",
                    (share["share_id"],),
                )
                if cursor.rowcount != 1:
                    raise ControlChanged(
                        "The pending receipt ledger changed during reconciliation."
                    )
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            # Definite not-found collapses the ambiguous in-flight boundary.
            # Keep the exact bytes/key, but return the ledger to ``sealed`` so
            # fresh gates may either retry it or discard it during an explicit
            # reauthorization.  Leaving it ``submitting`` would permanently
            # block the only flow that can repair profile/terms drift.
            share = {**share, "submission_state": "sealed"}
            if live_mode != "enabled":
                return {
                    "ok": False,
                    "code": "paused",
                    "message": "The exact sealed artifact remains paused after receipt lookup.",
                }
        else:
            receipt_id = _commit_receipt(
                conn, share_id=str(share["share_id"]), receipt=receipt
            )
            return {
                "ok": True,
                "code": "uploaded",
                "count": len(_artifact_revision_keys(conn, str(share["share_id"]))),
                "receipt_reference": receipt_id,
            }
    if enrollment.get("mode") != "enabled":
        return {
            "ok": False,
            "code": "disabled",
            "message": "The sealed artifact remains unsent because automation is off.",
        }
    if not allow_submit:
        return {
            "ok": False,
            "code": "submission_requires_fresh_gates",
            "message": "The pending artifact requires fresh local and hosted gates.",
            "retryable": True,
        }
    try:
        return _submit_pending_artifact(
            conn,
            share=share,
            enrollment=enrollment,
            credentials=credentials,
            capabilities=capabilities,
            ai_backend=ai_backend,
        )
    except ControlChanged:
        # A sealed artifact is definitely unsent.  If its hold, indexed
        # revision, raw fingerprint, or another local gate changed, discard
        # only that draft so a later cycle can consider the current revision.
        # ``_cleanup_unsent_draft`` re-reads the state and will never delete an
        # artifact that crossed into ``submitting``.
        if share.get("submission_state") == "sealed":
            _cleanup_unsent_draft(
                conn, str(share["share_id"]), allow_sealed=True
            )
        raise


def _validate_pending_for_submission(
    conn: sqlite3.Connection,
    *,
    share: Mapping[str, Any],
    enrollment: Mapping[str, Any],
    expected_profile_hash: str,
    api_origin: str,
    ai_backend: str | None,
    check_raw_fingerprints: bool = True,
) -> None:
    """Re-establish every local safety gate before a recovery POST.

    ``check_raw_fingerprints`` re-hashes the raw parser inputs, which is
    size-unbounded. The locked submit path already validates the ledger
    immediately before it acquires the egress lock, so it passes ``False``
    here to keep that re-hash out of the lock — running it while the lock is
    held would stall every concurrent ``save_config`` for the full hash of up
    to five (potentially very large) session logs.
    """

    if share.get("enrollment_id") != enrollment.get("server_enrollment_id"):
        raise AutoUploadError(
            "enrollment_mismatch", "The sealed artifact belongs to another enrollment."
        )
    if int(share.get("authorization_revision") or 0) != int(
        enrollment.get("authorization_revision") or 0
    ):
        raise AutoUploadError(
            "authorization_version_mismatch",
            "The sealed artifact belongs to another authorization revision.",
        )
    rows = conn.execute(
        "SELECT ss.session_id, ss.content_revision "
        "FROM share_sessions ss JOIN sessions s ON s.session_id = ss.session_id "
        "WHERE ss.share_id = ? ORDER BY ss.session_id",
        (share["share_id"],),
    ).fetchall()
    if not (1 <= len(rows) <= MAX_SESSIONS):
        raise AutoUploadError(
            "invalid_session_count", "A sealed recurring artifact must contain one to five traces."
        )
    expected_revisions: dict[str, str] = {}
    for row in rows:
        if not row["content_revision"]:
            raise AutoUploadError(
                "revision_missing", "A sealed share is missing its revision identity."
            )
        expected_revisions[str(row["session_id"])] = str(row["content_revision"])
    _assert_control_state(
        expected_generation=int(enrollment["generation"]),
        expected_profile_hash=expected_profile_hash,
        expected_revisions=expected_revisions,
        api_origin=api_origin,
        ai_backend=ai_backend,
    )
    if check_raw_fingerprints:
        _validate_raw_fingerprint_ledger(conn, share)


def _validate_raw_fingerprint_ledger(
    conn: sqlite3.Connection, share: Mapping[str, Any]
) -> None:
    rows = conn.execute(
        "SELECT s.session_id, s.raw_source_path FROM share_sessions ss "
        "JOIN sessions s ON s.session_id = ss.session_id WHERE ss.share_id = ? "
        "ORDER BY s.session_id",
        (share["share_id"],),
    ).fetchall()
    if not (1 <= len(rows) <= MAX_SESSIONS):
        raise AutoUploadError(
            "invalid_session_count", "A sealed recurring artifact must contain one to five traces."
        )
    candidates = [
        {"session_id": str(row["session_id"]), "raw_source_path": row["raw_source_path"]}
        for row in rows
    ]
    encoded_fingerprints = share.get("sealed_raw_fingerprints")
    if not isinstance(encoded_fingerprints, str) or not encoded_fingerprints:
        raise AutoUploadError(
            "recovery_ledger_incomplete",
            "The sealed artifact has no raw-input recovery fingerprint.",
        )
    try:
        stored_raw = json.loads(encoded_fingerprints)
    except json.JSONDecodeError as exc:
        raise AutoUploadError(
            "recovery_ledger_incomplete",
            "The sealed artifact raw-input recovery fingerprint is corrupt.",
        ) from exc
    current_raw = {key: list(value) for key, value in _raw_fingerprints(candidates).items()}
    if stored_raw != current_raw:
        raise ControlChanged("A selected raw trace changed after artifact sealing.")


def _raw_stat_signatures(
    conn: sqlite3.Connection, share: Mapping[str, Any]
) -> dict[str, tuple]:
    """Cheap stat-only signatures for a sealed share's raw inputs.

    Detects a raw append/replace that happens while the egress lock is being
    acquired, without the size-unbounded re-hash of the fingerprint ledger. A
    vanished or unreadable source counts as a change (raises ``ControlChanged``).
    """

    rows = conn.execute(
        "SELECT s.session_id, s.raw_source_path FROM share_sessions ss "
        "JOIN sessions s ON s.session_id = ss.session_id WHERE ss.share_id = ? "
        "ORDER BY s.session_id",
        (share["share_id"],),
    ).fetchall()
    signatures: dict[str, tuple] = {}
    for row in rows:
        session_id = str(row["session_id"])
        raw_path = row["raw_source_path"]
        if not isinstance(raw_path, str) or not raw_path:
            raise ControlChanged(
                "A selected trace no longer has a verifiable raw source."
            )
        try:
            signatures[session_id] = stat_raw_source(raw_path)
        except (OSError, RawSourceChanged) as exc:
            raise ControlChanged(
                "A selected raw trace became unavailable while acquiring the egress lock."
            ) from exc
    return signatures


def _server_enrollment_gate(
    capabilities: Mapping[str, Any],
    enrollment: Mapping[str, Any],
    credentials: Mapping[str, Any],
) -> None:
    active = credentials.get("active_token")
    if not isinstance(active, str) or not active:
        raise AutoUploadError(
            "credential_invalid", "The active recurring-upload credential is unavailable."
        )
    active_expiry = _parse_time(credentials.get("active_token_expires_at"))
    if active_expiry is None or active_expiry <= _now():
        raise AutoUploadError(
            "credential_expired",
            "The active recurring-upload credential expired; verify your email to rotate it.",
        )
    remote = get_enrollment(
        capabilities,
        enrollment_id=str(enrollment["server_enrollment_id"]),
        active_token=active,
    )
    if remote.get("enrollment_id") != enrollment.get("server_enrollment_id"):
        raise AutoUploadError(
            "enrollment_mismatch", "Hosted enrollment identity no longer matches local state."
        )
    if remote.get("revoked_at"):
        raise AutoUploadError(
            "credential_revoked", "The hosted recurring enrollment was revoked."
        )
    if remote.get("submissions_open") is not True:
        raise AutoUploadError(
            "submissions_closed",
            "Hosted recurring submissions are currently closed.",
            retryable=True,
        )
    if remote.get("terms_current") is not True:
        raise AutoUploadError(
            "authorization_version_mismatch",
            "Recurring authorization or retention terms changed.",
        )
    # Protocol v2: the scope hash is server-computed (keyed with the server's
    # secret), so the client pins the value read back at enrollment time and
    # requires it to be unchanged. A missing stored hash (a pre-v2 enrollment
    # row) fails closed under its own code so UIs can point the user at
    # reauthorization rather than implying hosted state drifted.
    expected_scope_hash = enrollment.get("server_scope_hash")
    if not isinstance(expected_scope_hash, str) or not expected_scope_hash:
        raise AutoUploadError(
            "reauthorization_required",
            "Reauthorize automatic uploads to accept the updated hosted terms "
            "(run: clawjournal auto-upload enable).",
        )
    if remote.get("scope_hash") != expected_scope_hash:
        raise AutoUploadError(
            "authorization_version_mismatch",
            "The hosted recurring scope no longer matches local state.",
        )
    if remote.get("authorization_version") != enrollment.get(
        "recurring_authorization_version"
    ) or remote.get("retention_policy_version") != enrollment.get("retention_version"):
        raise AutoUploadError(
            "authorization_version_mismatch",
            "The hosted recurring terms no longer match local state.",
        )
    if remote.get("ownership_certification_version") != enrollment.get(
        "ownership_certification_version"
    ):
        raise AutoUploadError(
            "authorization_version_mismatch",
            "The hosted ownership certification no longer matches local state.",
        )
    if int(remote.get("authorization_revision") or 0) != int(
        enrollment.get("authorization_revision") or 0
    ):
        raise AutoUploadError(
            "authorization_version_mismatch",
            "The hosted authorization revision no longer matches local state.",
        )


def _run_cycle_impl(
    *,
    force: bool = False,
    scheduled_client: AgentName | None = None,
) -> dict[str, Any]:
    """Run or recover one capped cycle without ever rebuilding an ambiguous ZIP."""

    with whole_run_lock(blocking=False) as acquired:
        if not acquired:
            return AutoUploadError(
                "already_running", "An automatic-upload cycle is already running.", retryable=True
            ).as_result()
        run_id = str(uuid.uuid4())
        conn = open_index()
        generation: int | None = None
        try:
            # The OS lock proves no prior runner is alive.  Clear a display
            # overlay orphaned by process death without reclaiming any lock by
            # age.
            conn.execute(
                "UPDATE auto_upload_enrollment SET current_run_id = NULL, "
                "current_run_stage = NULL, updated_at = ? "
                "WHERE singleton_id = 1 AND current_run_id IS NOT NULL",
                (_iso(_now()),),
            )
            conn.commit()
            enrollment = get_auto_upload_enrollment(conn)
            if enrollment is None:
                return AutoUploadError(
                    "not_enabled", "Automatic upload is not enabled."
                ).as_result()
            pending_at_start = _pending_submission(conn)

            # Receipt recovery comes before every lifecycle and scheduling
            # gate. A submitting request may already have crossed egress, so
            # pause, disable, profile changes, missing hooks, and due time may
            # stop new work but must not make the authoritative receipt
            # unreachable. Recovery authority can only look up receipts.
            receipt_probe: dict[str, Any] | None = None
            if (
                pending_at_start is not None
                and pending_at_start.get("submission_state") == "submitting"
            ):
                try:
                    recovery_credentials = load_credentials(required=True)
                    if recovery_credentials is None:
                        raise AutoUploadError(
                            "credential_invalid",
                            "Recurring recovery credentials are unavailable.",
                        )
                    credential_enrollment_id = recovery_credentials.get("enrollment_id")
                    if pending_at_start.get("enrollment_id") != credential_enrollment_id:
                        raise AutoUploadError(
                            "receipt_reconciliation_pending",
                            "A pending artifact belongs to another recurring enrollment.",
                        )
                    recovery = recovery_capabilities(
                        str(recovery_credentials["api_origin"])
                    )
                    receipt_credentials = dict(recovery_credentials)
                    if receipt_credentials.get("recovery_token"):
                        receipt_credentials["active_token"] = None
                    recovery_enrollment = dict(enrollment)
                    recovery_enrollment["server_enrollment_id"] = credential_enrollment_id
                    receipt_probe = _reconcile_pending(
                        conn,
                        enrollment=recovery_enrollment,
                        credentials=receipt_credentials,
                        capabilities=recovery,
                        allow_submit=False,
                    )
                    if receipt_probe is None:
                        raise AutoUploadError(
                            "ledger_missing",
                            "The pending share ledger disappeared.",
                        )
                    if receipt_probe.get("code") == "uploaded":
                        _checkpoint_recovered_receipt(conn, receipt_probe)
                        return receipt_probe
                    if enrollment.get("mode") != "enabled":
                        still_pending = _pending_submission(conn)
                        if (
                            still_pending is not None
                            and still_pending.get("submission_state") == "submitting"
                        ):
                            # A success-shaped probe can leave the share
                            # 'submitting' on a non-enabled enrollment (e.g.
                            # receipt_not_found while Off defers the collapse
                            # to Disable). With leftover hooks nothing else
                            # would stamp pacing, so the forced due-check
                            # would relaunch a runner every SessionStart.
                            _record_recovery_backoff(
                                conn,
                                generation=int(enrollment["generation"]),
                            )
                        return receipt_probe
                except RecurringServiceError as exc:
                    # A failed receipt lookup must pace itself: the hook
                    # due-check forces 'submitting' immediately due, so without
                    # next_retry_at every SessionStart would relaunch a
                    # network-calling runner (a spawn storm). Recovery runs
                    # before every lifecycle gate, so ONLY the retry clock is
                    # stamped — health/result codes may carry durable overlays
                    # (a safety action_required, Disable's revocation state)
                    # that a read-only probe must never rewrite.
                    _record_recovery_backoff(
                        conn,
                        generation=int(enrollment["generation"]),
                        retry_after=exc.retry_after,
                    )
                    return exc.as_result()
                except (AutoUploadError, CredentialStoreError) as exc:
                    error = (
                        exc
                        if isinstance(exc, AutoUploadError)
                        else AutoUploadError("credential_store_failed", str(exc))
                    )
                    # Same pacing guard as above: throttle the forced
                    # 'submitting' due-check without touching durable status.
                    _record_recovery_backoff(
                        conn,
                        generation=int(enrollment["generation"]),
                    )
                    return error.as_result()

            if enrollment.get("mode") == "off":
                return AutoUploadError(
                    "not_enabled", "Automatic upload is not enabled."
                ).as_result()
            if enrollment.get("mode") == "paused":
                return AutoUploadError(
                    "paused", "Automatic upload is paused."
                ).as_result()
            if (
                scheduled_client is not None
                and scheduled_client not in enrollment.get("hook_targets", [])
            ):
                return AutoUploadError(
                    "hook_not_authorized",
                    "This agent hook is not selected for automatic upload.",
                ).as_result()
            scheduled_hook_missing = bool(
                scheduled_client is not None and _selected_hook_missing(enrollment)
            )

            # Durable ``action_required`` blocks both scheduled and explicit
            # cycles. A missing selected hook blocks scheduled work but not an
            # explicit Run now. In either case, the sole scheduled exception
            # is receipt lookup for a request already durably marked
            # ``submitting``; it may have crossed the egress boundary earlier.
            durable_action_required = enrollment.get("health") == "action_required"
            if durable_action_required or scheduled_hook_missing:
                if receipt_probe is not None:
                    return receipt_probe
                return AutoUploadError(
                    "action_required" if durable_action_required else "hook_missing",
                    (
                        "Review the automatic-upload status before running again."
                        if durable_action_required
                        else "A selected SessionStart hook is missing; reinstall it or use Run now."
                    ),
                ).as_result()

            decision = due_decision(enrollment)
            if not force and not decision.due:
                # A 'submitting' artifact already had its receipt reconciled
                # above; surface that. Otherwise honor cadence/backoff: a
                # pending 'sealed' artifact from a retryable failure must wait
                # out its next_retry_at rather than re-submit on every wake-up.
                if receipt_probe is not None:
                    return receipt_probe
                return {
                    "ok": True,
                    "code": decision.reason,
                    "message": "No automatic-upload cycle is due.",
                }
            generation = int(enrollment["generation"])
            if not _start_run(conn, enrollment, run_id=run_id):
                return AutoUploadError(
                    "control_conflict", "Automatic upload state changed."
                ).as_result()

            try:
                credentials = load_credentials(required=True)
                if credentials is None:
                    raise AutoUploadError(
                        "credential_invalid", "Recurring credentials are unavailable."
                    )
                if credentials.get("enrollment_id") != enrollment.get("server_enrollment_id"):
                    raise AutoUploadError(
                        "enrollment_mismatch",
                        "Recurring credentials belong to a different enrollment.",
                    )
                config = load_config()
                ai_backend = _resolved_ai_backend(config)
                expected_profile = egress_profile_hash(
                    conn,
                    enrollment_scope={
                        "sources": enrollment["enrolled_sources"],
                        "projects": enrollment["enrolled_projects"],
                    },
                    api_origin=str(credentials["api_origin"]),
                    ai_backend=ai_backend,
                    config=config,
                )
                if expected_profile != enrollment.get("egress_profile_hash"):
                    raise AutoUploadError(
                        "profile_changed",
                        "Review the changed scope/privacy profile before automatic upload.",
                    )

                any_pending = _pending_submission(conn)
                if (
                    any_pending is not None
                    and any_pending.get("enrollment_id")
                    != enrollment.get("server_enrollment_id")
                ):
                    raise AutoUploadError(
                        "receipt_reconciliation_pending",
                        "A pending artifact belongs to a prior enrollment.",
                    )

                # A clean empty cycle is entirely local: refresh the enrolled
                # sources and evaluate the shared candidate contract before
                # contacting the hosted service. Pending receipt recovery is
                # the exception because its first job is to reconcile a
                # request that may already have crossed the egress boundary.
                from .workbench.daemon import Scanner

                initial_report: dict[str, Any] | None = None
                if any_pending is None:
                    _set_run_stage(
                        conn, generation=generation, run_id=run_id, stage="scanning"
                    )
                    strict = Scanner().scan_once_strict(
                        list(enrollment["enrolled_sources"])
                    )
                    if not strict["ok"]:
                        _record_cycle_result(
                            conn,
                            generation=generation,
                            code="strict_scan_incomplete",
                            retryable=True,
                        )
                        return {
                            "ok": False,
                            "code": "strict_scan_incomplete",
                            "message": "The enrolled source refresh was incomplete.",
                            "retryable": True,
                            "scan": strict,
                        }
                    initial_report = _candidate_report(conn, enrollment)
                    if initial_report.get("scope_blockers"):
                        raise AutoUploadError(
                            "scope_changed",
                            "The enrolled source/project confirmation no longer matches.",
                        )
                    if not initial_report["selected"]:
                        _record_cycle_result(
                            conn,
                            generation=generation,
                            code="nothing_new",
                            count=0,
                            success=True,
                        )
                        return {"ok": True, "code": "nothing_new", "count": 0}

                try:
                    capabilities = fetch_capabilities(
                        force=True, require_enrollment_open=False
                    )
                except (CapabilityError, RecurringServiceError) as discovery_error:
                    # Dark/closed discovery must not prevent receipt lookup for
                    # an already ambiguous request.  Recovery endpoints are
                    # fixed by v1 and derived only from the pinned issuer.
                    if any_pending is not None:
                        recovery = recovery_capabilities(str(credentials["api_origin"]))
                        receipt_result = _reconcile_pending(
                            conn,
                            enrollment=enrollment,
                            credentials=credentials,
                            capabilities=recovery,
                            allow_submit=False,
                            ai_backend=ai_backend,
                        )
                        if receipt_result and receipt_result.get("code") == "uploaded":
                            _record_cycle_result(
                                conn,
                                generation=generation,
                                code="uploaded",
                                count=int(receipt_result.get("count") or 0),
                                receipt_reference=receipt_result.get("receipt_reference"),
                                success=True,
                            )
                            return receipt_result
                    raise discovery_error
                if (
                    credentials.get("api_origin") != capabilities.get("origin")
                    or credentials.get("issuer") != capabilities.get("origin")
                ):
                    raise AutoUploadError(
                        "destination_changed",
                        "The exact hosted recurring-upload origin changed.",
                    )

                _set_run_stage(
                    conn, generation=generation, run_id=run_id, stage="reconciling"
                )
                pending_result = _reconcile_pending(
                    conn,
                    enrollment=enrollment,
                    credentials=credentials,
                    capabilities=capabilities,
                    allow_submit=False,
                    ai_backend=ai_backend,
                )
                if pending_result is not None and pending_result.get("code") == "uploaded":
                    if pending_result.get("ok"):
                        _record_cycle_result(
                            conn,
                            generation=generation,
                            code="uploaded",
                            count=int(pending_result.get("count") or 0),
                            receipt_reference=pending_result.get("receipt_reference"),
                            success=True,
                        )
                    return pending_result

                _server_enrollment_gate(capabilities, enrollment, credentials)

                if pending_result is not None:
                    _set_run_stage(
                        conn, generation=generation, run_id=run_id, stage="scanning"
                    )
                    strict = Scanner().scan_once_strict(
                        list(enrollment["enrolled_sources"])
                    )
                    if not strict["ok"]:
                        _record_cycle_result(
                            conn,
                            generation=generation,
                            code="strict_scan_incomplete",
                            retryable=True,
                        )
                        return {
                            "ok": False,
                            "code": "strict_scan_incomplete",
                            "message": "The enrolled source refresh was incomplete.",
                            "retryable": True,
                            "scan": strict,
                        }
                    pending_share = _pending_submission(
                        conn,
                        enrollment_id=str(enrollment["server_enrollment_id"]),
                    )
                    if pending_share is None:
                        raise AutoUploadError(
                            "ledger_missing", "The pending share ledger disappeared."
                        )
                    try:
                        _validate_pending_for_submission(
                            conn,
                            share=pending_share,
                            enrollment=enrollment,
                            expected_profile_hash=expected_profile,
                            api_origin=str(credentials["api_origin"]),
                            ai_backend=ai_backend,
                        )
                    except ControlChanged:
                        if pending_share.get("submission_state") == "sealed":
                            _cleanup_unsent_draft(
                                conn,
                                str(pending_share["share_id"]),
                                allow_sealed=True,
                            )
                        raise
                    recovered = _reconcile_pending(
                        conn,
                        enrollment=enrollment,
                        credentials=credentials,
                        capabilities=capabilities,
                        allow_submit=True,
                        ai_backend=ai_backend,
                    )
                    if recovered is None:
                        raise AutoUploadError(
                            "ledger_missing", "The pending share ledger disappeared."
                        )
                    if recovered.get("ok") and recovered.get("code") == "uploaded":
                        _record_cycle_result(
                            conn,
                            generation=generation,
                            code="uploaded",
                            count=int(recovered.get("count") or 0),
                            receipt_reference=recovered.get("receipt_reference"),
                            success=True,
                        )
                    return recovered

                assert initial_report is not None
                report = initial_report

                # One fresh parse immediately before binding revisions to the
                # package.  This intentionally reruns the enrolled scope rather
                # than relying on a stale index selection.
                second_scan = Scanner().scan_once_strict(
                    list(enrollment["enrolled_sources"])
                )
                if not second_scan["ok"]:
                    _record_cycle_result(
                        conn,
                        generation=generation,
                        code="strict_scan_incomplete",
                        retryable=True,
                    )
                    return {
                        "ok": False,
                        "code": "strict_scan_incomplete",
                        "message": "The final source refresh was incomplete.",
                        "retryable": True,
                        "scan": second_scan,
                    }
                report = _candidate_report(conn, enrollment)
                if not report["selected"]:
                    # The candidate set became empty between the two strict
                    # scans (e.g. the only candidate's revision changed and its
                    # 24h stability clock reset). That is benign nothing_new —
                    # not an oversized bundle — and must not become a durable
                    # action_required that blocks every future cycle.
                    _record_cycle_result(
                        conn,
                        generation=generation,
                        code="nothing_new",
                        count=0,
                        success=True,
                    )
                    return {"ok": True, "code": "nothing_new", "count": 0}
                settings = get_effective_share_settings(conn, config)
                settings["source_filter"] = list(enrollment["enrolled_sources"])
                selected, deferred_by_size, missing_candidates = _ranked_size_prefix(
                    conn,
                    report["selected"],
                    settings=settings,
                    maximum_bundle_size=int(capabilities["maximum_bundle_size"]),
                )
                report["deferred_by_size"] = deferred_by_size
                report["exclusion_counts"]["deferred_by_size"] = deferred_by_size
                if not selected:
                    if deferred_by_size:
                        # Candidates existed but none fit the hosted size budget
                        # — a genuine oversize condition worth surfacing to the
                        # user as a durable action.
                        raise AutoUploadError(
                            "payload_too_large",
                            "The highest-ranked trace cannot fit the hosted size limit.",
                        )
                    # Every candidate's session row vanished between the report
                    # and the size pass — the index changed mid-cycle. That is
                    # a transient state change, not an oversize condition: back
                    # off and retry instead of stamping a durable
                    # action_required that blocks all future cycles.
                    raise ControlChanged(
                        "Selected traces disappeared while sizing the bundle."
                    )
                session_ids = [str(item["session_id"]) for item in selected]
                expected_revisions = {
                    str(item["session_id"]): str(item["revision_hash"])
                    for item in selected
                }
                fingerprints = _raw_fingerprints(selected)
                _bind_scan_fingerprints(second_scan, selected, fingerprints)
                _assert_control_state(
                    expected_generation=generation,
                    expected_profile_hash=expected_profile,
                    expected_revisions=expected_revisions,
                    api_origin=str(credentials["api_origin"]),
                    ai_backend=ai_backend,
                )
                _set_run_stage(conn, generation=generation, run_id=run_id, stage="packaging")

                def before_ai_call() -> None:
                    _assert_control_state(
                        expected_generation=generation,
                        expected_profile_hash=expected_profile,
                        expected_revisions=expected_revisions,
                        api_origin=str(credentials["api_origin"]),
                        ai_backend=ai_backend,
                    )

                packaged = package(
                    conn,
                    session_ids,
                    settings,
                    ai_pii=bool(config.get("ai_pii_review_enabled")),
                    ai_backend=ai_backend or "auto",
                    expected_revisions=expected_revisions,
                    note="Automatic weekly share",
                    before_ai_call=before_ai_call,
                )
                share_id = packaged.get("share_id")
                if not packaged.get("ok"):
                    blocked = packaged.get("blocked_sessions") or []
                    mapped_ids: list[str] = []
                    for item in blocked:
                        candidate_id = (
                            item.get("session_id") if isinstance(item, dict) else item
                        )
                        if candidate_id in expected_revisions:
                            mapped_ids.append(str(candidate_id))
                    if mapped_ids and len(mapped_ids) == len(blocked):
                        for session_id in sorted(set(mapped_ids)):
                            set_hold_state(
                                conn,
                                session_id,
                                "pending_review",
                                changed_by="auto_upload",
                                reason="Automatic share finding requires review",
                            )
                        if isinstance(share_id, str):
                            _cleanup_unsent_draft(conn, share_id)
                        _record_cycle_result(
                            conn,
                            generation=generation,
                            code="review_attention",
                            count=len(mapped_ids),
                            retryable=True,
                        )
                        return {
                            "ok": False,
                            "code": "review_attention",
                            "message": "Selected revisions were moved to pending review.",
                            "retryable": True,
                            "count": len(mapped_ids),
                        }
                    if isinstance(share_id, str):
                        _cleanup_unsent_draft(conn, share_id)
                    block_reason = packaged.get("block_reason")
                    if block_reason == "ai-pii-incomplete":
                        raise AutoUploadError(
                            "ai_pii_incomplete",
                            "AI-PII coverage was incomplete; no artifact was submitted.",
                            retryable=True,
                        )
                    if block_reason == "trufflehog-findings" or blocked:
                        raise AutoUploadError(
                            "unmappable_findings",
                            str(
                                packaged.get("error")
                                or "Automatic findings could not be mapped safely."
                            ),
                        )
                    raise AutoUploadError(
                        "package_failed",
                        str(packaged.get("error") or "Automatic packaging failed."),
                        retryable=True,
                    )

                assert isinstance(share_id, str)
                manifest = packaged["manifest"]
                if manifest.get("session_count") != len(selected) or not (
                    1 <= len(selected) <= MAX_SESSIONS
                ):
                    _cleanup_unsent_draft(conn, share_id)
                    raise AutoUploadError(
                        "package_scope_mismatch",
                        "The finalized artifact no longer contains the exact selected trace set.",
                    )
                coverage_ok, coverage_message = verify_coverage(
                    manifest,
                    bool(config.get("ai_pii_review_enabled")),
                    expected_backend=ai_backend,
                )
                if not coverage_ok:
                    _cleanup_unsent_draft(conn, share_id)
                    raise AutoUploadError(
                        "ai_pii_incomplete", coverage_message, retryable=True
                    )
                if _raw_fingerprints(selected) != fingerprints:
                    _cleanup_unsent_draft(conn, share_id)
                    raise AutoUploadError(
                        "raw_source_changed",
                        "A selected raw trace changed during packaging.",
                        retryable=True,
                    )
                _assert_control_state(
                    expected_generation=generation,
                    expected_profile_hash=expected_profile,
                    expected_revisions=expected_revisions,
                    api_origin=str(credentials["api_origin"]),
                    ai_backend=ai_backend,
                )
                zip_bytes = build_zip(Path(packaged["export_dir"]))
                if len(zip_bytes) == 0:
                    _cleanup_unsent_draft(conn, share_id)
                    raise AutoUploadError("empty_artifact", "The final recurring ZIP is empty.")
                if len(zip_bytes) > int(capabilities["maximum_bundle_size"]):
                    _cleanup_unsent_draft(conn, share_id)
                    raise AutoUploadError(
                        "payload_too_large",
                        "The final recurring ZIP exceeds the hosted size limit.",
                    )
                # Seal and the post-seal rechecks share one cleanup guard: a
                # failure while writing the ZIP or committing the seal ledger
                # (e.g. a generation/profile change racing this boundary) would
                # otherwise orphan the draft share row and the on-disk sealed
                # ZIP, which nothing later reclaims.
                try:
                    artifact_path, artifact_hash = _write_sealed_zip(
                        Path(packaged["export_dir"]), zip_bytes
                    )
                    client_submission_id = _seal_share_ledger(
                        conn,
                        share_id=share_id,
                        enrollment=enrollment,
                        artifact_path=artifact_path,
                        artifact_sha256=artifact_hash,
                        raw_fingerprints=fingerprints,
                    )
                    if _raw_fingerprints(selected) != fingerprints:
                        raise ControlChanged("A selected raw trace changed before egress.")
                    _assert_control_state(
                        expected_generation=generation,
                        expected_profile_hash=expected_profile,
                        expected_revisions=expected_revisions,
                        api_origin=str(credentials["api_origin"]),
                        ai_backend=ai_backend,
                    )
                except Exception:
                    _cleanup_unsent_draft(conn, share_id, allow_sealed=True)
                    raise
                _set_run_stage(conn, generation=generation, run_id=run_id, stage="submitting")
                sealed_share = get_share(conn, share_id)
                if sealed_share is None:
                    raise AutoUploadError("ledger_missing", "The sealed share ledger disappeared.")
                # get_share returns compatibility aliases plus sessions; merge
                # the ledger-only columns from the direct row.
                ledger = conn.execute(
                    "SELECT * FROM shares WHERE share_id = ?", (share_id,)
                ).fetchone()
                assert ledger is not None
                result = _submit_pending_artifact(
                    conn,
                    share=dict(ledger),
                    enrollment=enrollment,
                    credentials=credentials,
                    capabilities=capabilities,
                    ai_backend=ai_backend,
                )
                assert result["receipt_reference"]
                _record_cycle_result(
                    conn,
                    generation=generation,
                    code="uploaded",
                    count=int(result["count"]),
                    receipt_reference=str(result["receipt_reference"]),
                    success=True,
                )
                return {
                    **result,
                    "client_submission_id": client_submission_id,
                    "artifact_sha256": artifact_hash,
                    "deferred_by_size": deferred_by_size,
                }
            except ControlChanged as exc:
                # A control change voids this cycle. Most causes (pause,
                # disable, profile edit) stop the next cycle at the mode check,
                # but a persistent mismatch would otherwise raise here every
                # SessionStart with health='ready' and no next_retry_at, i.e. a
                # full-cycle storm. Record backoff so a lasting control mismatch
                # cannot relaunch on every wake-up.
                _record_cycle_result(
                    conn,
                    generation=generation,
                    code=exc.code,
                    retryable=True,
                )
                return exc.as_result()
            except RecurringServiceError as exc:
                _record_cycle_result(
                    conn,
                    generation=generation,
                    code=exc.code,
                    retryable=exc.retryable or exc.ambiguous,
                    retry_after=exc.retry_after,
                    action_required=not (exc.retryable or exc.ambiguous),
                )
                return exc.as_result()
            except (AutoUploadError, CredentialStoreError) as exc:
                error = (
                    exc
                    if isinstance(exc, AutoUploadError)
                    else AutoUploadError("credential_store_failed", str(exc))
                )
                _record_cycle_result(
                    conn,
                    generation=generation,
                    code=error.code,
                    retryable=error.retryable,
                    retry_after=error.retry_after,
                    action_required=not error.retryable,
                )
                return error.as_result()
        finally:
            conn.close()
            if generation is not None:
                _clear_run_overlay(run_id)


def _record_crash_backoff() -> None:
    """Best-effort backoff stamp so a hard crash cannot retry every SessionStart.

    The crashed cycle never reached ``_record_cycle_result``, so without this
    the enrollment keeps ``next_retry_at`` unset and every subsequent hook
    would relaunch the failing runner immediately. Must never raise: this runs
    inside the crash handler, possibly with the index unavailable.
    """

    try:
        conn = open_index()
    except Exception:
        return
    try:
        enrollment = get_auto_upload_enrollment(conn)
        if enrollment is None or enrollment.get("mode") != "enabled":
            return
        _record_cycle_result(
            conn,
            generation=int(enrollment["generation"]),
            code="runner_crash",
            retryable=True,
        )
    except Exception:
        pass
    finally:
        conn.close()


def run_cycle(
    *,
    force: bool = False,
    scheduled_client: AgentName | None = None,
) -> dict[str, Any]:
    """Run one cycle and emit only bounded, non-content telemetry."""

    started = time.monotonic()
    try:
        result = _run_cycle_impl(force=force, scheduled_client=scheduled_client)
    except Exception:
        _write_telemetry(
            "cycle_complete",
            code="runner_crash",
            count=None,
            duration_ms=int((time.monotonic() - started) * 1000),
            scheduled_client=scheduled_client,
        )
        _record_crash_backoff()
        return AutoUploadError(
            "runner_crash",
            "The automatic-upload cycle stopped safely before completion.",
            retryable=True,
        ).as_result()
    _write_telemetry(
        "cycle_complete",
        code=_stable_telemetry_code(
            result.get("code") or ("ok" if result.get("ok") else "error")
        ),
        count=result.get("count") if isinstance(result.get("count"), int) else None,
        duration_ms=int((time.monotonic() - started) * 1000),
        scheduled_client=scheduled_client,
    )
    return result


def _open_existing_hook_index() -> sqlite3.Connection | None:
    """Open the existing index without bootstrap, migrations, or long waits."""

    from .workbench import index as index_module

    path = Path(index_module.INDEX_DB)
    try:
        if not path.is_file():
            return None
        conn = sqlite3.connect(
            path.resolve().as_uri() + "?mode=rw",
            uri=True,
            timeout=HOOK_DB_BUSY_TIMEOUT_MS / 1000,
            isolation_level=None,
        )
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout={HOOK_DB_BUSY_TIMEOUT_MS}")
        return conn
    except (OSError, sqlite3.Error):
        return None


def _record_hook_observed_on_connection(
    conn: sqlite3.Connection,
    client: AgentName,
    observed_at: datetime,
) -> bool:
    field = f"{client}_hook_observed_at"
    observed = _iso(observed_at)
    cursor = conn.execute(
        f"UPDATE auto_upload_enrollment SET {field} = ?, updated_at = ? "
        "WHERE singleton_id = 1",
        (observed, observed),
    )
    return cursor.rowcount == 1


def _hook_due_check_on_connection(
    conn: sqlite3.Connection,
    now: datetime,
) -> DueDecision:
    row = conn.execute(
        "WITH enrollment AS ("
        "  SELECT singleton_id, mode, enrolled_at, last_completed_at, next_retry_at "
        "  FROM auto_upload_enrollment WHERE singleton_id = 1"
        ") "
        "SELECT enrollment.singleton_id, enrollment.mode, enrollment.enrolled_at, "
        "enrollment.last_completed_at, enrollment.next_retry_at, "
        "EXISTS(SELECT 1 FROM shares WHERE submission_channel = 'auto_weekly' "
        "  AND submission_state = 'submitting') AS submitting_pending "
        "FROM (SELECT 1) AS anchor LEFT JOIN enrollment ON 1 = 1"
    ).fetchone()
    if row is None:
        return DueDecision(False, "index-unavailable")
    # A 'submitting' artifact may already have crossed egress; its receipt must
    # be reconciled promptly regardless of cadence, so this is due immediately
    # after a crash (the runner's recovery step can only look up receipts). But
    # a *persistently failing* receipt lookup stamps next_retry_at (see the
    # recovery branch in _run_cycle_impl); honor it here so a stuck 'submitting'
    # artifact cannot relaunch a network-calling runner on every SessionStart.
    if row["submitting_pending"]:
        retry_at = _parse_time(row["next_retry_at"])
        if retry_at is not None and now < retry_at:
            return DueDecision(False, "receipt-recovery-backoff")
        return DueDecision(True, "receipt-recovery-pending")
    # A merely 'sealed' artifact is a retryable submit failure with a stamped
    # next_retry_at. It must honor that backoff instead of relaunching a full
    # cycle on every SessionStart, so fall through to due_decision rather than
    # forcing due=True on recovery_pending.
    enrollment = dict(row) if row["singleton_id"] is not None else None
    return due_decision(enrollment, now=now)


def record_hook_observed(client: AgentName, observed_at: datetime) -> bool:
    """Record hook diagnostics only when the current index is immediately writable."""

    conn = _open_existing_hook_index()
    if conn is None:
        return False
    try:
        conn.execute("BEGIN IMMEDIATE")
        recorded = _record_hook_observed_on_connection(conn, client, observed_at)
        conn.commit()
        return recorded
    except sqlite3.Error:
        conn.rollback()
        return False
    finally:
        conn.close()


def hook_due_check(_client: AgentName, now: datetime) -> DueDecision:
    """Read scheduler state only when the current index is immediately stable."""

    conn = _open_existing_hook_index()
    if conn is None:
        return DueDecision(False, "index-unavailable")
    try:
        # A write-intent transaction makes a concurrent writer fail closed even
        # in WAL mode, where an ordinary reader would otherwise see stale state
        # and could launch a runner while controls are changing.
        conn.execute("BEGIN IMMEDIATE")
        decision = _hook_due_check_on_connection(conn, now)
        conn.rollback()
        return decision
    except sqlite3.Error:
        conn.rollback()
        return DueDecision(False, "index-busy")
    finally:
        conn.close()


def hook_session_start_check(client: AgentName, now: datetime) -> DueDecision:
    """Record observation and decide due state in one bounded transaction."""

    conn = _open_existing_hook_index()
    if conn is None:
        return DueDecision(False, "index-unavailable")
    try:
        conn.execute("BEGIN IMMEDIATE")
        _record_hook_observed_on_connection(conn, client, now)
        decision = _hook_due_check_on_connection(conn, now)
        conn.commit()
        return decision
    except sqlite3.Error:
        conn.rollback()
        return DueDecision(False, "index-busy")
    finally:
        conn.close()


def spawn_scheduled_runner(client: AgentName) -> bool:
    spawn_detached(
        [
            sys.executable,
            "-m",
            "clawjournal.auto_upload",
            "run",
            "--scheduled",
            "--client",
            client,
        ]
    )
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m clawjournal.auto_upload")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--scheduled", action="store_true")
    run_parser.add_argument("--client", choices=SUPPORTED_HOOK_TARGETS)
    args = parser.parse_args(argv)
    if args.command == "run":
        result = run_cycle(
            force=not args.scheduled,
            scheduled_client=args.client,
        )
        # Detached scheduled runs are intentionally quiet.  Direct internal
        # invocation remains machine-readable for diagnostics.
        if not args.scheduled:
            print(json.dumps(result, sort_keys=True))
        return 0 if result.get("ok") else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
