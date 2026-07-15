"""Recurring hosted sharing enrollment, selection, and one-shot execution."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from .config import CONFIG_DIR, load_config, save_config, source_scope_sources
from .share_flow import package, submit
from .workbench.index import (
    get_effective_share_settings,
    get_share,
    get_share_ready_stats,
    open_index,
    set_hold_state,
)

DEFAULT_CADENCE_DAYS = 7
MAX_CANDIDATES = 5
QUIET_HOURS = 24
_ACTIVE_STATES = {"enabled", "paused", "off"}
RUN_LOCK = Path.home() / ".clawjournal" / "auto-upload.lock"


@contextmanager
def _run_lock():
    RUN_LOCK.parent.mkdir(parents=True, exist_ok=True)
    handle = open(RUN_LOCK, "a+b")
    try:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        if os.name == "nt":
            import msvcrt
            try:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("An automatic upload cycle is already running.") from exc
        else:
            import fcntl
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise RuntimeError("An automatic upload cycle is already running.") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()).encode("ascii"))
        handle.flush()
        yield
    finally:
        try:
            if os.name == "nt":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        handle.close()


def _now(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def get_enrollment(conn) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM auto_upload_enrollment WHERE enrollment_id = 1"
    ).fetchone()
    if row is None:
        return None
    result = dict(row)
    try:
        result["excluded_projects"] = json.loads(result["excluded_projects"])
    except (TypeError, json.JSONDecodeError):
        result["excluded_projects"] = []
    try:
        result["included_projects"] = json.loads(result.get("included_projects") or "[]")
    except (TypeError, json.JSONDecodeError):
        result["included_projects"] = []
    result["sources"] = source_scope_sources(result.get("source_scope"))
    return result


def recurring_terms() -> dict[str, Any]:
    from .workbench.daemon import _fetch_hosted_share_capabilities, _hosted_api_base, _json_request

    capabilities = _fetch_hosted_share_capabilities(force=True)
    if int(capabilities.get("recurring_upload_api_version") or 0) != 1:
        raise ValueError("Hosted recurring upload capability is unavailable.")
    return _json_request(f"{_hosted_api_base()}/api/recurring-upload/terms")


def _profile_action(enrollment: dict[str, Any], capabilities: dict[str, Any]) -> str | None:
    config = load_config()
    if not config.get("projects_confirmed"):
        return "review_project_scope"
    profile = {
        "source_scope": str(config.get("source") or ""),
        "included_projects": enrollment.get("included_projects") or [],
        "excluded_projects": sorted(config.get("excluded_projects", []) or []),
        "ai_pii_review_enabled": bool(config.get("ai_pii_review_enabled")),
        "destination_origin": str(capabilities.get("share_page_url") or ""),
    }
    current_hash = hashlib.sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    if current_hash != enrollment.get("profile_hash"):
        return "review_enrollment_profile"
    advertised_consent = capabilities.get("recurring_consent_version")
    advertised_retention = capabilities.get("recurring_retention_policy_version")
    if advertised_consent and advertised_consent != enrollment.get("consent_version"):
        return "review_updated_recurring_terms"
    if advertised_retention and advertised_retention != enrollment.get("retention_policy_version"):
        return "review_updated_retention_policy"
    return None


def enable_enrollment(
    conn,
    *,
    consent_version: str,
    retention_policy_version: str,
    cadence_days: int = DEFAULT_CADENCE_DAYS,
    now: datetime | None = None,
    authorization_token: str | None = None,
    capabilities_fn: Callable[[], dict[str, Any]] | None = None,
    enroll_fn: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if not consent_version.strip() or not retention_policy_version.strip():
        raise ValueError("Consent and retention policy versions are required.")
    if cadence_days < 1:
        raise ValueError("cadence_days must be at least 1")
    config = load_config()
    if not config.get("projects_confirmed") or not config.get("source"):
        raise ValueError("Source and project scope must be confirmed before enrollment.")
    token = authorization_token or config.get("verified_email_token")
    if not token:
        raise ValueError("Fresh verified identity is required to create recurring authorization.")

    manual_share = conn.execute(
        "SELECT 1 FROM shares WHERE hosted_receipt_id IS NOT NULL LIMIT 1"
    ).fetchone()
    if manual_share is None:
        raise ValueError("Complete one successful hosted manual share before enrollment.")
    if capabilities_fn is None:
        from .workbench.daemon import _fetch_hosted_share_capabilities
        capabilities_fn = lambda: _fetch_hosted_share_capabilities(force=True)
    capabilities = capabilities_fn()
    if int(capabilities.get("recurring_upload_api_version") or 0) != 1:
        raise ValueError("Hosted recurring upload capability is unavailable.")
    if int(capabilities.get("recurring_upload_max_sessions") or MAX_CANDIDATES) != MAX_CANDIDATES:
        raise ValueError("Hosted recurring upload limits are incompatible with this client.")

    clock = _now(now)
    stamp = _iso(clock)
    sources = source_scope_sources(str(config["source"]))
    query = "SELECT DISTINCT project FROM sessions WHERE project IS NOT NULL"
    params: list[Any] = []
    if sources:
        query += f" AND source IN ({','.join('?' for _ in sources)})"
        params.extend(sources)
    excluded = set(config.get("excluded_projects", []) or [])
    projects = sorted(
        row["project"] for row in conn.execute(query, params).fetchall()
        if row["project"] not in excluded
    )
    if not projects:
        raise ValueError("The confirmed scope contains no indexed projects.")
    client_enrollment_id = str(uuid.uuid4())
    profile = {
        "source_scope": str(config["source"]),
        "included_projects": projects,
        "excluded_projects": sorted(excluded),
        "ai_pii_review_enabled": bool(config.get("ai_pii_review_enabled")),
        "destination_origin": str(capabilities.get("share_page_url") or ""),
    }
    profile_hash = hashlib.sha256(
        json.dumps(profile, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    request = {
        "client_enrollment_id": client_enrollment_id,
        "identity_token": str(token),
        "consent_version": consent_version.strip(),
        "retention_policy_version": retention_policy_version.strip(),
        "scope": profile,
        "cadence_days": cadence_days,
        "max_sessions_per_cycle": MAX_CANDIDATES,
    }
    if enroll_fn is None:
        from .workbench.daemon import _hosted_api_base, _json_request
        enroll_fn = lambda payload: _json_request(
            f"{_hosted_api_base()}/api/recurring-upload/enroll",
            method="POST", payload=payload,
        )
    hosted = enroll_fn(request)
    active_token = hosted.get("active_token")
    recovery_token = hosted.get("recovery_token")
    server_enrollment_id = hosted.get("enrollment_id")
    authorization_revision = hosted.get("authorization_revision")
    server_accepted_at = hosted.get("accepted_at")
    if not all(isinstance(value, str) and value for value in (
        active_token, recovery_token, server_enrollment_id,
        authorization_revision, server_accepted_at,
    )):
        raise ValueError("Hosted enrollment did not return complete recurring authorization.")
    existing = get_enrollment(conn)
    enrolled_at = existing["enrolled_at"] if existing else stamp
    baseline_at = str(server_accepted_at)
    next_due = _iso(clock + timedelta(days=cadence_days))
    conn.execute(
        """INSERT INTO auto_upload_enrollment (
            enrollment_id, state, consent_version, retention_policy_version,
            source_scope, excluded_projects, cadence_days, enrolled_at,
            baseline_at, updated_at, next_due_at, last_trace_count,
            client_enrollment_id, server_enrollment_id, authorization_revision,
            recurring_auth_version, included_projects, profile_hash,
            server_accepted_at, generation, health, revocation_pending
        ) VALUES (1, 'enabled', ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, '1', ?, ?, ?, 1, 'ready', 0)
        ON CONFLICT(enrollment_id) DO UPDATE SET
            state = 'enabled', consent_version = excluded.consent_version,
            retention_policy_version = excluded.retention_policy_version,
            source_scope = excluded.source_scope,
            excluded_projects = excluded.excluded_projects,
            cadence_days = excluded.cadence_days, updated_at = excluded.updated_at,
            next_due_at = excluded.next_due_at, last_error = NULL,
            required_action = NULL, client_enrollment_id = excluded.client_enrollment_id,
            server_enrollment_id = excluded.server_enrollment_id,
            authorization_revision = excluded.authorization_revision,
            recurring_auth_version = excluded.recurring_auth_version,
            included_projects = excluded.included_projects,
            profile_hash = excluded.profile_hash,
            server_accepted_at = excluded.server_accepted_at,
            generation = auto_upload_enrollment.generation + 1,
            health = 'ready', revocation_pending = 0""",
        (
            consent_version.strip(), retention_policy_version.strip(),
            str(config["source"]), json.dumps(sorted(excluded)), cadence_days,
            enrolled_at, baseline_at, stamp, next_due, client_enrollment_id,
            server_enrollment_id, authorization_revision, json.dumps(projects),
            profile_hash, server_accepted_at,
        ),
    )
    conn.commit()
    config["recurring_upload_token"] = str(active_token)
    config["recurring_upload_recovery_token"] = str(recovery_token)
    config.pop("recurring_upload_token_expires_at", None)
    save_config(config)
    return get_enrollment(conn) or {}


def set_enrollment_state(conn, state: str, *, now: datetime | None = None) -> dict[str, Any]:
    if state not in _ACTIVE_STATES:
        raise ValueError(f"Invalid automatic sharing state: {state}")
    current = get_enrollment(conn)
    if current is None:
        raise ValueError("Automatic sharing is not enrolled.")
    if state == "enabled":
        if current.get("state") != "paused":
            raise ValueError("Only a paused enrollment can be resumed.")
        if not load_config().get("recurring_upload_token"):
            raise ValueError("Active recurring authorization is missing.")
    conn.execute(
        "UPDATE auto_upload_enrollment SET state = ?, updated_at = ?, generation = generation + 1 "
        "WHERE enrollment_id = 1",
        (state, _iso(_now(now))),
    )
    conn.commit()
    if state == "off":
        config = load_config()
        config.pop("recurring_upload_token", None)
        config.pop("recurring_upload_token_expires_at", None)
        save_config(config)
        recovery = config.get("recurring_upload_recovery_token")
        enrollment_id = current.get("server_enrollment_id")
        if recovery and enrollment_id:
            try:
                from .workbench.daemon import _hosted_api_base, _json_request
                _json_request(
                    f"{_hosted_api_base()}/api/recurring-upload/revoke",
                    method="POST",
                    payload={"enrollment_id": enrollment_id, "recovery_token": recovery},
                )
            except Exception as exc:
                conn.execute(
                    "UPDATE auto_upload_enrollment SET revocation_pending = 1, health = 'action_required', "
                    "required_action = ?, last_error = ? WHERE enrollment_id = 1",
                    ("retry_revocation", str(exc)),
                )
                conn.commit()
            else:
                config.pop("recurring_upload_recovery_token", None)
                save_config(config)
    return get_enrollment(conn) or {}


def select_pending_sessions(
    conn, enrollment: dict[str, Any] | None = None, *, now: datetime | None = None,
) -> list[dict[str, Any]]:
    enrollment = enrollment or get_enrollment(conn)
    if enrollment is None:
        return []
    stats = get_share_ready_stats(
        conn,
        excluded_projects=enrollment.get("excluded_projects") or [],
        source_filter=enrollment.get("sources"),
        include_unapproved=True,
    )
    sessions = list(stats.get("sessions") or [])
    baseline = _parse_iso(enrollment.get("server_accepted_at") or enrollment.get("baseline_at"))
    clock = _now(now)
    quiet_cutoff = clock - timedelta(hours=QUIET_HOURS)
    projects = set(enrollment.get("included_projects") or [])
    selected: list[dict[str, Any]] = []
    for session in sessions:
        row = conn.execute(
            "SELECT end_time, updated_at, indexed_at, review_status, hold_state, "
            "blob_path, ai_failure_value_score FROM sessions WHERE session_id = ?",
            (session["session_id"],),
        ).fetchone()
        if row is None or session.get("project") not in projects:
            continue
        end_time = _parse_iso(row["end_time"])
        stable_at = _parse_iso(row["updated_at"] or row["indexed_at"])
        if baseline is None or end_time is None or end_time <= baseline:
            continue
        if end_time > quiet_cutoff or stable_at is None or stable_at > quiet_cutoff:
            continue
        if row["review_status"] not in ("new", "shortlisted", "approved"):
            continue
        if row["hold_state"] not in (None, "auto_redacted", "released"):
            continue
        if not row["blob_path"] or not os.path.isfile(row["blob_path"]):
            continue
        item = dict(session)
        item["end_time"] = row["end_time"]
        item["ai_failure_value_score"] = row["ai_failure_value_score"]
        selected.append(item)
    def rank(item: dict[str, Any]) -> tuple[Any, ...]:
        score = item.get("ai_failure_value_score")
        ended = _parse_iso(item.get("end_time")) or datetime.min.replace(tzinfo=timezone.utc)
        return (score is None, -(float(score) if score is not None else 0.0), -ended.timestamp(), item["session_id"])
    return sorted(selected, key=rank)[:MAX_CANDIDATES]


def strict_refresh(
    enrollment: dict[str, Any], scanner_factory: Callable[[], Any] | None = None,
) -> list[dict[str, str]]:
    enrolled_sources = set(enrollment.get("sources") or [])
    if scanner_factory is None:
        from .workbench.daemon import Scanner
        source_filter = next(iter(enrolled_sources)) if len(enrolled_sources) == 1 else None
        scanner_factory = lambda: Scanner(source_filter=source_filter)
    scanner = scanner_factory()
    scanner.scan_once()
    return [
        error for error in getattr(scanner, "last_scan_errors", [])
        if not enrolled_sources or error.get("source") in enrolled_sources
    ]


def candidate_report(
    conn, enrollment: dict[str, Any] | None = None, *, now: datetime | None = None,
) -> dict[str, Any]:
    enrollment = enrollment or get_enrollment(conn)
    if enrollment is None:
        return {"sessions": [], "count": 0, "deferred_count": 0}
    sessions = select_pending_sessions(conn, enrollment, now=now)
    broad = get_share_ready_stats(
        conn,
        excluded_projects=enrollment.get("excluded_projects") or [],
        source_filter=enrollment.get("sources"),
        include_unapproved=True,
    ).get("sessions") or []
    return {
        "sessions": sessions,
        "count": len(sessions),
        "deferred_count": max(0, len(broad) - len(sessions)),
        "limit": MAX_CANDIDATES,
        "order": "scored first, failure-value desc, end_time desc, session_id",
        "eligibility": [
            "exact enrolled source/project snapshot",
            "server-accepted future-only boundary",
            "24 hours unchanged for append-only traces",
            "new, shortlisted, or approved; never blocked or segmented",
            "auto_redacted or released hold state",
            "raw blob present, current revision, and not already shared",
        ],
    }


def enrollment_status(conn, *, now: datetime | None = None) -> dict[str, Any]:
    enrollment = get_enrollment(conn)
    manual_share_completed = conn.execute(
        "SELECT 1 FROM shares WHERE hosted_receipt_id IS NOT NULL LIMIT 1"
    ).fetchone() is not None
    try:
        from .workbench.daemon import _fetch_hosted_share_capabilities
        capabilities = _fetch_hosted_share_capabilities()
        capability_available = int(capabilities.get("recurring_upload_api_version") or 0) == 1
    except Exception:
        capability_available = False
    if enrollment is None:
        return {"state": "off", "enrolled": False, "pending_count": 0, "due": False,
                "capability_available": capability_available,
                "manual_share_completed": manual_share_completed}
    clock = _now(now)
    next_due = _parse_iso(enrollment.get("next_due_at"))
    report = candidate_report(conn, enrollment, now=clock)
    result = dict(enrollment)
    result.update({
        "enrolled": True,
        "pending_count": report["count"],
        "deferred_count": report["deferred_count"],
        "due": enrollment["state"] == "enabled" and (next_due is None or clock >= next_due),
        "capability_available": capability_available,
        "manual_share_completed": manual_share_completed,
    })
    latest_run = conn.execute(
        "SELECT status FROM auto_upload_runs ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    result["running"] = bool(latest_run and latest_run["status"] == "running")
    if latest_run and latest_run["status"] in ("retrying", "failed"):
        result["health"] = "retrying"
    action = _profile_action(enrollment, capabilities) if capability_available else "recurring_capability_unavailable"
    if action:
        result["health"] = "action_required"
        result["required_action"] = action
    if enrollment["state"] == "enabled":
        from .auto_upload_scheduler import hook_status
        hooks = hook_status()
        result["hooks"] = hooks
        if not all(hooks.values()):
            result["health"] = "action_required"
            result["required_action"] = "repair_session_start_hooks"
    return result


def is_due_local(conn, *, now: datetime | None = None) -> bool:
    """Fast, network-free SessionStart predicate."""
    enrollment = get_enrollment(conn)
    if enrollment is None or enrollment.get("state") != "enabled":
        return False
    due_at = _parse_iso(enrollment.get("next_due_at"))
    return due_at is None or _now(now) >= due_at


def _record_run(conn, *, now: datetime, due_at: str | None) -> str:
    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO auto_upload_runs (run_id, started_at, status, due_at) "
        "VALUES (?, ?, 'running', ?)",
        (run_id, _iso(now), due_at),
    )
    conn.execute(
        "UPDATE auto_upload_enrollment SET last_attempt_at = ?, updated_at = ? "
        "WHERE enrollment_id = 1",
        (_iso(now), _iso(now)),
    )
    conn.commit()
    return run_id


def _seal_artifact(export_dir: Path, client_submission_id: str) -> tuple[str, str]:
    from .workbench.daemon import _build_share_zip

    payload = _build_share_zip(export_dir)
    directory = CONFIG_DIR / "auto-upload" / "artifacts"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{client_submission_id}.zip"
    temporary = path.with_suffix(".tmp")
    with open(temporary, "wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)
    try:
        directory_handle = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(directory_handle)
        finally:
            os.close(directory_handle)
    except OSError:
        pass
    return str(path), hashlib.sha256(payload).hexdigest()


def _lookup_receipt(enrollment: dict[str, Any], client_submission_id: str) -> dict[str, Any]:
    from .workbench.daemon import _hosted_api_base, _json_request

    recovery = load_config().get("recurring_upload_recovery_token")
    if not recovery:
        return {}
    return _json_request(
        f"{_hosted_api_base()}/api/recurring-upload/receipt",
        method="POST",
        payload={
            "enrollment_id": enrollment.get("server_enrollment_id"),
            "client_submission_id": client_submission_id,
            "recovery_token": recovery,
        },
    )


def _finish_run(
    conn,
    run_id: str,
    *,
    status: str,
    now: datetime,
    share_id: str | None = None,
    trace_count: int = 0,
    receipt_id: str | None = None,
    error: str | None = None,
    required_action: str | None = None,
    success: bool = False,
    clear_pending: bool = False,
) -> None:
    conn.execute(
        "UPDATE auto_upload_runs SET finished_at = ?, status = ?, share_id = ?, "
        "trace_count = ?, receipt_id = ?, error = ? WHERE run_id = ?",
        (_iso(now), status, share_id, trace_count, receipt_id, error, run_id),
    )
    fields = ["updated_at = ?", "last_error = ?", "required_action = ?"]
    params: list[Any] = [_iso(now), error, required_action]
    if success or status == "no_work":
        enrollment = get_enrollment(conn) or {}
        fields.append("next_due_at = ?")
        params.append(_iso(now + timedelta(days=int(enrollment.get("cadence_days", 7)))))
    if success:
        fields.extend([
            "last_success_at = ?", "last_trace_count = ?", "last_share_id = ?",
            "last_receipt_id = ?",
        ])
        params.extend([_iso(now), trace_count, share_id, receipt_id])
    if clear_pending:
        fields.append("pending_share_id = NULL")
    params.append(1)
    conn.execute(
        f"UPDATE auto_upload_enrollment SET {', '.join(fields)} WHERE enrollment_id = ?",
        params,
    )
    conn.commit()


def run_once(
    *,
    force: bool = False,
    scan: bool = True,
    now: datetime | None = None,
    scanner_factory: Callable[[], Any] | None = None,
    package_fn: Callable[..., dict] = package,
    submit_fn: Callable[..., dict] = submit,
) -> dict[str, Any]:
    try:
        with _run_lock():
            return _run_once_locked(
                force=force, scan=scan, now=now, scanner_factory=scanner_factory,
                package_fn=package_fn, submit_fn=submit_fn,
            )
    except RuntimeError as exc:
        return {"ok": False, "status": "retrying", "error": str(exc)}


def _run_once_locked(
    *, force: bool, scan: bool, now: datetime | None,
    scanner_factory: Callable[[], Any] | None,
    package_fn: Callable[..., dict], submit_fn: Callable[..., dict],
) -> dict[str, Any]:
    clock = _now(now)
    conn = open_index()
    try:
        enrollment = get_enrollment(conn)
        if enrollment is None or enrollment["state"] == "off":
            return {"ok": False, "status": "off", "error": "Automatic sharing is off."}
        if enrollment["state"] == "paused":
            return {"ok": False, "status": "paused", "error": "Automatic sharing is paused."}
        try:
            from .workbench.daemon import _fetch_hosted_share_capabilities
            capabilities = _fetch_hosted_share_capabilities(force=True)
        except Exception as exc:
            return {"ok": False, "status": "retrying", "error": str(exc)}
        action = _profile_action(enrollment, capabilities)
        if action:
            return {"ok": False, "status": "action_required", "required_action": action,
                    "error": "Recurring sharing profile or terms require review."}
        due_at = _parse_iso(enrollment.get("next_due_at"))
        if not force and due_at is not None and clock < due_at:
            return {"ok": True, "status": "not_due", "next_due_at": enrollment["next_due_at"]}

        run_id = _record_run(conn, now=clock, due_at=enrollment.get("next_due_at"))
        config = load_config()
        token = config.get("recurring_upload_token")
        if not token:
            error = "Recurring upload authorization is missing. Verify your email and enable again."
            _finish_run(
                conn, run_id, status="action_required", now=clock,
                error=error, required_action="renew_authentication",
            )
            return {"ok": False, "status": "action_required", "error": error}

        generation = int(enrollment.get("generation") or 0)
        if scan:
            errors = strict_refresh(enrollment, scanner_factory)
            if errors:
                error = "Strict enrolled-source refresh was incomplete."
                _finish_run(conn, run_id, status="retrying", now=clock, error=error)
                return {"ok": False, "status": "retrying", "error": error, "scan_errors": errors}

        enrollment = get_enrollment(conn) or enrollment
        settings = get_effective_share_settings(conn, config)
        settings["source_filter"] = enrollment.get("sources")
        settings["excluded_projects"] = enrollment.get("excluded_projects") or []
        share_id = enrollment.get("pending_share_id")
        if share_id:
            share = get_share(conn, share_id)
            if share is None or share.get("shared_at"):
                share_id = None
                conn.execute(
                    "UPDATE auto_upload_enrollment SET pending_share_id = NULL "
                    "WHERE enrollment_id = 1"
                )
                conn.commit()

        if share_id:
            share = get_share(conn, share_id) or {}
            trace_count = len(share.get("sessions") or [])
            pending = conn.execute(
                "SELECT client_submission_id, artifact_path, artifact_sha256, revisions_json "
                "FROM auto_upload_runs WHERE share_id = ? AND artifact_path IS NOT NULL "
                "ORDER BY started_at DESC LIMIT 1",
                (share_id,),
            ).fetchone()
            if pending is None or not os.path.isfile(pending["artifact_path"]):
                error = "Pending exact artifact is missing; refusing a non-identical retry."
                _finish_run(conn, run_id, status="action_required", now=clock, share_id=share_id,
                            trace_count=trace_count, error=error,
                            required_action="review_pending_artifact")
                return {"ok": False, "status": "action_required", "error": error}
            client_submission_id = pending["client_submission_id"]
            artifact_path = pending["artifact_path"]
            revision_keys = json.loads(pending["revisions_json"])
        else:
            candidates = select_pending_sessions(conn, enrollment, now=clock)
            if not candidates:
                _finish_run(conn, run_id, status="no_work", now=clock)
                return {"ok": True, "status": "no_work", "trace_count": 0}
            session_ids = [item["session_id"] for item in candidates]
            revisions = {item["session_id"]: item["revision_hash"] for item in candidates}
            packaged = package_fn(
                conn, session_ids, settings,
                ai_pii=bool(settings.get("ai_pii_review_enabled")),
                note="Automatic weekly sharing",
                expected_revisions=revisions,
            )
            if not packaged.get("ok"):
                error = str(packaged.get("error") or "Automatic packaging failed.")
                blocked = packaged.get("blocked_sessions") or []
                mapped_ids = [item.get("session_id") for item in blocked if item.get("session_id")]
                if blocked and len(mapped_ids) == len(blocked):
                    for session_id in mapped_ids:
                        set_hold_state(
                            conn, str(session_id), "pending_review", changed_by="auto",
                            reason="Automatic sharing findings require review",
                        )
                    _finish_run(
                        conn, run_id, status="action_required", now=clock, error=error,
                        required_action="review_findings_in_share",
                    )
                    return {"ok": False, "status": "action_required", "error": error,
                            "required_action": "review_findings_in_share",
                            "blocked_sessions": blocked}
                action = "review_oversized_bundle" if "size" in error.lower() else None
                status = "action_required" if action else "retrying"
                _finish_run(conn, run_id, status=status, now=clock, error=error,
                            required_action=action)
                return {"ok": False, "status": status, "error": error,
                        "required_action": action}
            share_id = str(packaged["share_id"])
            trace_count = len(session_ids)
            client_submission_id = str(uuid.uuid4())
            artifact_path, artifact_sha256 = _seal_artifact(
                Path(packaged["export_dir"]), client_submission_id
            )
            revision_keys = [
                hashlib.sha256(
                    f"{session_id}:{revisions[session_id]}".encode("utf-8")
                ).hexdigest()
                for session_id in session_ids
            ]
            conn.execute(
                "UPDATE auto_upload_enrollment SET pending_share_id = ? "
                "WHERE enrollment_id = 1",
                (share_id,),
            )
            conn.execute(
                "UPDATE auto_upload_runs SET share_id = ?, trace_count = ?, "
                "client_submission_id = ?, artifact_path = ?, artifact_sha256 = ?, "
                "revisions_json = ?, generation = ? WHERE run_id = ?",
                (share_id, trace_count, client_submission_id, artifact_path,
                 artifact_sha256, json.dumps(revision_keys), generation, run_id),
            )
            conn.commit()

        current = get_enrollment(conn) or {}
        if current.get("state") != "enabled" or int(current.get("generation") or 0) != generation:
            error = "Enrollment changed before submission; packaged artifact was not uploaded."
            _finish_run(conn, run_id, status="action_required", now=clock, share_id=share_id,
                        trace_count=trace_count, error=error, required_action="review_enrollment")
            return {"ok": False, "status": "action_required", "error": error}
        result = submit_fn(
            conn, share_id,
            accept_terms=True,
            ownership_certification=True,
            consent_version=enrollment["consent_version"],
            retention_policy_version=enrollment["retention_policy_version"],
            settings=settings,
            ai_pii=bool(settings.get("ai_pii_review_enabled")),
            upload_token_override=str(token),
            artifact_path=artifact_path,
            client_submission_id=client_submission_id,
            recurring_enrollment_id=enrollment.get("server_enrollment_id"),
            authorization_revision=enrollment.get("authorization_revision"),
            revision_keys=revision_keys,
            expected_enrollment_generation=generation,
        )
        if result.get("ok"):
            receipt_id = str(result.get("receipt_id") or "") or None
            _finish_run(
                conn, run_id, status="succeeded", now=clock, share_id=share_id,
                trace_count=int(result.get("session_count") or trace_count),
                receipt_id=receipt_id, success=True, clear_pending=True,
            )
            return {
                "ok": True, "status": "succeeded", "share_id": share_id,
                "trace_count": int(result.get("session_count") or trace_count),
                "receipt_id": receipt_id,
            }

        error = str(result.get("error") or "Automatic upload failed.")
        required_action = None
        status_code = int(result.get("status") or 0)
        if result.get("ambiguous"):
            try:
                receipt = _lookup_receipt(enrollment, client_submission_id)
            except Exception:
                receipt = {}
            receipt_id = receipt.get("receipt_id")
            if isinstance(receipt_id, str) and receipt_id:
                conn.execute(
                    "UPDATE shares SET status = 'shared', shared_at = ?, hosted_receipt_id = ?, "
                    "hosted_status = ? WHERE share_id = ?",
                    (_iso(clock), receipt_id, str(receipt.get("status") or "received"), share_id),
                )
                conn.commit()
                _finish_run(conn, run_id, status="succeeded", now=clock, share_id=share_id,
                            trace_count=trace_count, receipt_id=receipt_id,
                            success=True, clear_pending=True)
                return {"ok": True, "status": "succeeded", "share_id": share_id,
                        "trace_count": trace_count, "receipt_id": receipt_id,
                        "reconciled": True}
            required_action = "reconcile_submission_receipt"
        elif status_code in (401, 403):
            required_action = "renew_authentication"
        retryable = status_code == 429 or status_code >= 500 or status_code == 0
        final_status = "action_required" if required_action else ("retrying" if retryable else "action_required")
        if final_status == "action_required" and not required_action:
            required_action = "review_hosted_conflict"
        _finish_run(
            conn, run_id,
            status=final_status,
            now=clock, share_id=share_id, trace_count=trace_count,
            error=error, required_action=required_action,
        )
        return {
            "ok": False,
            "status": final_status,
            "share_id": share_id,
            "trace_count": trace_count,
            "error": error,
            "required_action": required_action,
        }
    finally:
        conn.close()
