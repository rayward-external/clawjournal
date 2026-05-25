"""Local daemon for the scientist workbench — scanner + HTTP API."""

import hashlib
import io
import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import zipfile
from datetime import datetime, timedelta, timezone
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..redaction.anonymizer import Anonymizer
from ..scoring.badges import compute_all_badges
from ..config import CONFIG_DIR, load_config, save_config
from .findings_pipeline import (
    drain_findings_backfill,
    run_findings_pipeline,
)
from .index import (
    add_policy,
    apply_share_redactions,
    create_share,
    export_share_to_disk,
    FAILURE_VALUE_SOURCE_SCOPE,
    get_effective_share_settings,
    get_share,
    get_shares,
    get_dashboard_analytics,
    get_highlights,
    get_policies,
    get_session_detail,
    get_share_ready_stats,
    get_stats,
    link_subagent_hierarchy,
    open_index,
    query_sessions,
    query_unscored_sessions,
    remove_policy,
    search_fts,
    update_session,
    upsert_sessions,
)
from .timeline import (
    canonical_session_path,
    load_timeline_page,
    render_not_found_html,
    render_timeline_html,
)
from ..parsing.parser import (
    AIDER_SOURCE,
    CLAUDE_SOURCE,
    CODEX_SOURCE,
    COPILOT_SOURCE,
    CURSOR_SOURCE,
    GEMINI_SOURCE,
    KIMI_SOURCE,
    OPENCODE_SOURCE,
    OPENCLAW_SOURCE,
    discover_projects,
    parse_project_sessions,
)

logger = logging.getLogger(__name__)

DEFAULT_PORT = 8384
SCAN_INTERVAL = 60  # seconds
AUTO_SCORE_BATCH_SIZE = 10

_SHARE_MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB
_SHARE_COOLDOWN_SECONDS = 10
_SHARE_INGEST_URL = os.environ.get("CLAWJOURNAL_INGEST_URL", "")
_SHARE_GCS_BUCKET = os.environ.get("CLAWJOURNAL_GCS_BUCKET", "clawjournal-traces")
_SHARE_GCS_PREFIX = os.environ.get("CLAWJOURNAL_GCS_PREFIX", "clawjournal")
_SHARE_UPLOAD_TIMEOUT = 120
_share_rate_lock = threading.Lock()

# Sources supported in the workbench (scientist-facing subset)
WORKBENCH_SOURCES = {
    CLAUDE_SOURCE, CODEX_SOURCE, OPENCLAW_SOURCE,
    CURSOR_SOURCE, COPILOT_SOURCE, AIDER_SOURCE,
    GEMINI_SOURCE, OPENCODE_SOURCE, KIMI_SOURCE,
}

# Path to the built frontend dist directory.
FRONTEND_DIST = Path(__file__).resolve().parent.parent / "web" / "frontend" / "dist"


def _persist_scoring_result(conn: sqlite3.Connection, session_id: str, result: Any) -> bool:
    """Persist a scoring result into the sessions table."""
    return update_session(
        conn, session_id,
        ai_quality_score=result.quality,
        ai_score_reason=result.reason,
        ai_scoring_detail=result.detail_json,
        ai_task_type=result.task_type,
        ai_outcome_badge=result.outcome_label or None,
        ai_value_badges=json.dumps(result.value_labels),
        ai_risk_badges=json.dumps(result.risk_level),
        ai_display_title=result.display_title or None,
        ai_effort_estimate=result.effort_estimate,
        ai_summary=result.summary or None,
        ai_failure_value_score=getattr(result, "failure_value_score", None),
        ai_recovery_labels=json.dumps(getattr(result, "recovery_labels", [])),
        ai_failure_attribution=getattr(result, "failure_attribution", "") or None,
        ai_failure_modes=json.dumps(getattr(result, "failure_modes", [])),
        ai_learning_summary=getattr(result, "learning_summary", "") or None,
        ai_scorer_backend=getattr(result, "scorer_backend", "") or None,
        ai_scorer_model=getattr(result, "scorer_model", "") or None,
        ai_rubric_git_sha=getattr(result, "rubric_git_sha", "") or None,
        ai_scored_at=getattr(result, "scored_at", "") or None,
    )


def _maybe_create_trace_note(conn: sqlite3.Connection, session_id: str) -> None:
    """Create `notes/{session_id}.md` if it does not already exist.

    Called from both score paths (auto-scoring in `score_unscored_once` and
    manual scoring in `_handle_score_session`) after the DB is updated, so
    the freshly-written `ai_summary` is what lands in the file. Strictly
    create-if-missing — never overwrite existing notes in the scoring hook,
    because they may carry unsynced user edits.

    Errors are logged but never raised: note creation is a best-effort
    side effect of scoring, not a requirement for scoring to succeed.
    """
    try:
        from ..workbench.trace_note import create_note_if_missing
        row = conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if row is None:
            return
        created = create_note_if_missing(dict(row))
        if created is not None:
            logger.debug("created trace note at %s", created)
    except Exception:
        logger.exception("Failed to create trace note for %s", session_id)


class Scanner:
    """Periodically scans source directories and indexes new sessions."""

    def __init__(self, source_filter: str | None = None):
        self.source_filter = source_filter
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_scan_mtimes: dict[str, float] = {}
        self.last_linked_count = 0
        self.last_scored_count = 0
        self._score_thread: threading.Thread | None = None
        self._score_lock = threading.Lock()
        self._auto_score_disabled_reason: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)

    def scan_once(self) -> dict[str, int]:
        """Run a single scan pass. Returns {source: new_session_count}."""
        conn = open_index()
        try:
            config = load_config()
            # Ingest stores raw content; anonymization happens at egress
            # (apply_share_redactions, score_session).
            anonymizer = Anonymizer(enabled=False)

            # Drain any sessions flagged by the security-refactor migration
            # before running the normal parse/scan loop. Per-row updates so
            # a crash mid-drain leaves remaining rows for the next tick.
            drain_findings_backfill(conn, config=config)

            results: dict[str, int] = {}
            projects = discover_projects(source_filter=self.source_filter)

            for project in projects:
                source = project.get("source", "")
                if source not in WORKBENCH_SOURCES:
                    continue
                if self.source_filter and source != self.source_filter:
                    continue

                try:
                    sessions = parse_project_sessions(
                        project["dir_name"],
                        anonymizer=anonymizer,
                        include_thinking=True,
                        source=source,
                        locator=project.get("locator"),
                    )
                    if sessions:
                        new_count = upsert_sessions(conn, sessions)
                        results[source] = results.get(source, 0) + new_count
                        # Drive each freshly-upserted session through the
                        # findings pipeline. Settle-threshold + revision
                        # check inside the driver keep this cheap on steady
                        # state; errors per session don't abort the loop.
                        for session in sessions:
                            sid = session.get("session_id")
                            if not sid:
                                continue
                            try:
                                run_findings_pipeline(conn, sid, session, config=config)
                            except Exception:
                                logger.exception("Findings pipeline failed for %s", sid)
                except Exception:
                    logger.exception("Error parsing project %s", project["dir_name"])

            self.last_linked_count = link_subagent_hierarchy(conn)
            return results
        finally:
            conn.close()

    def score_unscored_once(self, *, limit: int = AUTO_SCORE_BATCH_SIZE) -> int:
        """Score a recent batch of unscored traces using the current agent."""
        if self._auto_score_disabled_reason:
            return 0
        if not self._score_lock.acquire(blocking=False):
            return 0

        from ..scoring.scoring import score_session

        try:
            conn = open_index()
            try:
                source_scope = self.source_filter or FAILURE_VALUE_SOURCE_SCOPE
                recent_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
                sessions = query_unscored_sessions(
                    conn,
                    limit=limit,
                    source=source_scope,
                    since=recent_cutoff,
                )
                if not sessions:
                    return 0

                scored = 0
                for s in sessions:
                    sid = s["session_id"]
                    try:
                        result = score_session(conn, sid, backend="auto")
                    except RuntimeError as exc:
                        message = str(exc)
                        if (
                            "Could not detect the current agent" in message
                            or "CLI not found" in message
                            or "Unsupported CLAWJOURNAL_SCORER_BACKEND" in message
                        ):
                            self._auto_score_disabled_reason = message
                            logger.info("Automatic scoring disabled: %s", message)
                            break
                        logger.warning("Automatic scoring failed for %s: %s", sid, message)
                        continue
                    except Exception:
                        logger.exception("Automatic scoring crashed for %s", sid)
                        continue

                    if _persist_scoring_result(conn, sid, result):
                        scored += 1
                        _maybe_create_trace_note(conn, sid)

                return scored
            finally:
                conn.close()
        finally:
            self._score_lock.release()

    def trigger_auto_score(self, *, limit: int = AUTO_SCORE_BATCH_SIZE) -> None:
        """Start background scoring for recent unscored sessions if idle."""
        if self._auto_score_disabled_reason:
            return
        if self._score_thread and self._score_thread.is_alive():
            return

        def _run() -> None:
            scored = self.score_unscored_once(limit=limit)
            self.last_scored_count = scored
            if scored > 0:
                logger.info("Auto-scored %d recent sessions", scored)

        self._score_thread = threading.Thread(target=_run, daemon=True)
        self._score_thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                results = self.scan_once()
                self.trigger_auto_score()
                total_new = sum(results.values())
                if total_new > 0 or self.last_linked_count > 0:
                    logger.info(
                        "Indexed %d new sessions, linked %d subagent relationships: %s",
                        total_new,
                        self.last_linked_count,
                        results,
                    )
            except Exception:
                logger.exception("Scanner error")
            self._stop_event.wait(SCAN_INTERVAL)


_LOCALHOST_ORIGINS = re.compile(r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$")


_API_TOKEN_COOKIE_NAME = "clawjournal_token"


def _cors_origin(handler: BaseHTTPRequestHandler) -> str | None:
    """Return the request Origin if it's a localhost address, else None."""
    origin = handler.headers.get("Origin", "")
    if _LOCALHOST_ORIGINS.match(origin):
        return origin
    return None


def _parse_cookie_token(cookie_header: str | None) -> str | None:
    """Extract the per-install api_token from the `Cookie` request header.

    Returns None when the header is absent, unparseable, or does not
    include the expected cookie. Never raises — malformed cookies just
    fall through to the 401 path.
    """
    if not cookie_header:
        return None
    try:
        from http.cookies import SimpleCookie

        jar = SimpleCookie()
        jar.load(cookie_header)
    except Exception:
        return None
    morsel = jar.get(_API_TOKEN_COOKIE_NAME)
    if morsel is None:
        return None
    return morsel.value or None


def _api_token_cookie_header(token: str) -> str:
    """Build the `Set-Cookie` value that carries the api_token.

    HttpOnly prevents XSS from reading the token (stricter than the
    existing `window.__CLAWJOURNAL_API_TOKEN__` injection, which we keep
    for the SPA's fetch-based API access). SameSite=Strict prevents
    cross-site navigation from leaking the cookie. The cookie is scoped
    to `/timeline` so it cannot authorize the broader `/api/*` surface.
    No Secure flag — the daemon is loopback HTTP only.
    """
    return (
        f"{_API_TOKEN_COOKIE_NAME}={token}; Path=/timeline; HttpOnly; SameSite=Strict"
    )


def _json_response(handler: BaseHTTPRequestHandler, data: Any, status: int = 200) -> None:
    """Send a JSON response."""
    body = json.dumps(data, default=str).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    origin = _cors_origin(handler)
    if origin:
        handler.send_header("Access-Control-Allow-Origin", origin)
    handler.end_headers()
    handler.wfile.write(body)


def _read_body(handler: BaseHTTPRequestHandler) -> dict:
    """Read and parse JSON body from request."""
    length = int(handler.headers.get("Content-Length", 0))
    if length == 0:
        return {}
    raw = handler.rfile.read(length)
    return json.loads(raw)


def _parse_json_fields(rows: list[dict]) -> None:
    """Parse JSON string fields in session rows into Python objects.

    Also resolves LLM-classified badges: prefers ai_* values when present,
    falls back to heuristic values, then removes the ai_* keys from the dict.
    """
    for row in rows:
        for field in (
            "value_badges", "risk_badges", "files_touched", "commands_run",
            "ai_value_badges", "ai_risk_badges", "ai_recovery_labels",
            "ai_failure_modes",
        ):
            if isinstance(row.get(field), str):
                try:
                    row[field] = json.loads(row[field])
                except (json.JSONDecodeError, ValueError):
                    pass

        # Resolve: prefer LLM classification over heuristic
        if row.get("ai_task_type"):
            row["task_type"] = row["ai_task_type"]
        if row.get("ai_outcome_badge"):
            row["outcome_badge"] = row["ai_outcome_badge"]
        if row.get("ai_value_badges"):
            row["value_badges"] = row["ai_value_badges"]
        if row.get("ai_risk_badges"):
            row["risk_badges"] = row["ai_risk_badges"]

        # Remove ai_* fields from API response (frontend doesn't need them)
        for k in ("ai_task_type", "ai_outcome_badge", "ai_value_badges", "ai_risk_badges"):
            row.pop(k, None)

        # Rename DB column names → user-facing API names
        if "outcome_badge" in row:
            row["outcome_label"] = row.pop("outcome_badge")
        if "value_badges" in row:
            row["value_labels"] = row.pop("value_badges")
        if "risk_badges" in row:
            row["risk_level"] = row.pop("risk_badges")



def _is_edu_email(email: str) -> bool:
    """Check if an email address has a .edu domain."""
    return bool(email and email.strip().lower().endswith(".edu"))


def _missing_ingest_url_error() -> str:
    return (
        "Hosted sharing is not configured in this build. "
        "Use `clawjournal bundle-export` to produce a local zip you can share "
        "any way you like. Self-hosters can set CLAWJOURNAL_INGEST_URL to point "
        "at their own ingest backend."
    )


def _validate_ingest_url() -> None:
    """Verify the ingest URL is configured and uses HTTPS."""
    if not _SHARE_INGEST_URL:
        raise RuntimeError(_missing_ingest_url_error())
    if not _SHARE_INGEST_URL.startswith("https://"):
        # Allow http://localhost and http://127.0.0.1 for local development
        if _SHARE_INGEST_URL.startswith(("http://localhost", "http://127.0.0.1")):
            return
        raise RuntimeError(
            "CLAWJOURNAL_INGEST_URL must use HTTPS to protect credentials in transit."
        )


def _ensure_verified_email_credentials() -> tuple[str, str]:
    """Ensure the user has a valid, non-expired upload token.

    Returns (verified_email, upload_token).
    """
    _validate_ingest_url()

    config = load_config()
    verified_email = (config.get("verified_email") or "").strip().lower()
    upload_token = (config.get("verified_email_token") or "").strip()
    expires_at = config.get("verified_email_token_expires_at", 0)

    if verified_email and upload_token:
        # Check expiry with 60-second grace period
        if isinstance(expires_at, (int, float)) and time.time() < (expires_at - 60):
            return verified_email, upload_token
        raise RuntimeError(
            "Upload token has expired. "
            "Run `clawjournal verify-email <your-email@university.edu>` to get a fresh token."
        )
    if verified_email:
        raise RuntimeError(
            "Email verification needs to be refreshed before sharing data. "
            "Run `clawjournal verify-email <your-email@university.edu>` again."
        )
    raise RuntimeError(
        "Email verification required before sharing data. "
        "Run `clawjournal verify-email <your-email@university.edu>` to verify your .edu email."
    )


def ensure_share_upload_ready() -> None:
    """Fail fast if the current environment cannot upload shared data."""
    _ensure_verified_email_credentials()


def _clear_stored_upload_token() -> None:
    """Remove any cached upload token so the next share re-verifies."""
    config = load_config()
    changed = False
    for key in ("verified_email_token", "verified_email_token_expires_at"):
        if key in config:
            del config[key]
            changed = True
    if changed:
        save_config(config)


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return body or str(exc)


def request_email_verification(email: str) -> dict:
    """Send a verification request to the ingest service for a .edu email.

    Returns the response dict from the server (contains status info).
    """
    _validate_ingest_url()
    if not _is_edu_email(email):
        raise ValueError("Only .edu email addresses are accepted for data sharing.")

    url = f"{_SHARE_INGEST_URL}/verify-email"
    req = urllib.request.Request(
        url,
        data=json.dumps({"email": email.strip().lower()}).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"clawjournal/{__version__}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ValueError(_http_error_message(exc)) from exc


def confirm_email_verification(email: str, code: str) -> dict:
    """Confirm email verification with the code received via email.

    On success, stores the verified email in config and returns the response.
    """
    _validate_ingest_url()
    url = f"{_SHARE_INGEST_URL}/verify-confirm"
    req = urllib.request.Request(
        url,
        data=json.dumps({
            "email": email.strip().lower(),
            "code": code.strip(),
        }).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "User-Agent": f"clawjournal/{__version__}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        raise ValueError(_http_error_message(exc)) from exc

    if result.get("verified"):
        upload_token = result.get("upload_token")
        if not isinstance(upload_token, str) or not upload_token:
            raise ValueError(
                "Verification succeeded but the ingest service did not return an upload token."
            )
        expires_at = result.get("upload_token_expires_at", 0)
        config = load_config()
        config["verified_email"] = email.strip().lower()
        config["verified_email_token"] = upload_token
        config["verified_email_token_expires_at"] = expires_at
        save_config(config)

    return result


def _build_multipart_body(
    fields: dict[str, str],
    files: dict[str, tuple[str, bytes, str]],
) -> tuple[bytes, str]:
    """Build a multipart/form-data body using only stdlib.

    Args:
        fields: name -> value for text fields
        files: name -> (filename, data, content_type) for file parts

    Returns:
        (body_bytes, content_type_header)
    """
    boundary = uuid.uuid4().hex
    parts: list[bytes] = []

    for name, value in fields.items():
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode("utf-8")
        )

    for name, (filename, data, content_type) in files.items():
        header = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        )
        parts.append(header.encode("utf-8") + data + b"\r\n")

    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    body = b"".join(parts)
    content_type = f"multipart/form-data; boundary={boundary}"
    return body, content_type


def _with_legacy_bundle_alias(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose bundle_id as a compatibility alias for share_id."""
    if "share_id" in payload and "bundle_id" not in payload:
        payload["bundle_id"] = payload["share_id"]
    return payload


def upload_share(
    conn: sqlite3.Connection,
    share_id: str,
    *,
    force: bool = False,
    custom_strings: list[str] | None = None,
    extra_usernames: list[str] | None = None,
    excluded_projects: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    allowlist_entries: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Upload a share to the GCS ingest service.

    Returns a result dict with keys: ok, shared_at, session_count,
    bundle_hash, redaction_summary.  On error, returns: error (str) and
    optionally status (int).  gcs_uri is stored in DB but not returned to
    callers to avoid leaking internal infrastructure details.
    """
    # Require verified upload credentials before uploading.
    try:
        verified_email, verified_email_token = _ensure_verified_email_credentials()
    except RuntimeError as e:
        return {"error": str(e), "status": 403}

    share = get_share(conn, share_id)
    if share is None:
        return {"error": "Share not found", "status": 404}

    if share.get("shared_at") and not force:
        return {
            "error": "Share already uploaded",
            "shared_at": share.get("shared_at"),
            "status": 409,
        }

    # Centralized release gate — every hosted-upload path reaches this
    # helper, so CLI, quick-share, and direct upload endpoints cannot
    # diverge (Decision 24). Non-`released` sessions are refused with
    # a structured list of offending IDs and their effective state.
    from .index import release_gate_blockers
    session_ids = [s["session_id"] for s in share.get("sessions") or []]
    blockers = release_gate_blockers(conn, session_ids)
    if blockers:
        return {
            "error": "Share contains sessions that are not released",
            "blockers": blockers,
            "status": 409,
        }

    # Re-export to ensure latest field filtering and secret redaction
    export_dir, manifest = export_share_to_disk(
        conn,
        share_id,
        share,
        custom_strings=custom_strings,
        extra_usernames=extra_usernames,
        excluded_projects=excluded_projects,
        blocked_domains=blocked_domains,
        allowlist_entries=allowlist_entries,
    )
    if export_dir is None:
        return {"error": "Export failed.", "status": 500}
    sessions_file = export_dir / "sessions.jsonl"
    manifest_file = export_dir / "manifest.json"

    if not sessions_file.exists():
        return {"error": "Export failed — no sessions file.", "status": 500}

    # Pre-PII TruffleHog scan already ran inside export_share_to_disk —
    # if that stage blocked, fail closed before we do any additional work.
    if manifest.get("blocked"):
        return {
            "error": manifest.get("block_message") or "Share blocked by TruffleHog",
            "block_reason": manifest.get("block_reason"),
            "trufflehog_summary": manifest.get("redaction_summary", {}).get("trufflehog"),
            "status": 422,
        }

    # Mandatory PII redaction pass — applied to ALL sessions before upload.
    # This catches names, orgs, usernames, etc. that regex-based secret
    # redaction misses.  No session cap: every session gets reviewed.
    coverage_full = 0
    coverage_rules_only = 0
    try:
        from ..redaction.pii import apply_findings_to_session, review_session_pii_hybrid
        import json as _json

        redacted_lines: list[str] = []
        pii_total = 0
        with open(sessions_file) as f:
            for line in f:
                if not line.strip():
                    continue
                session = _json.loads(line)
                findings, cov = review_session_pii_hybrid(
                    session, ignore_llm_errors=True, return_coverage=True,
                )
                if cov == "full":
                    coverage_full += 1
                else:
                    coverage_rules_only += 1
                if findings:
                    session, _ = apply_findings_to_session(session, findings)
                    pii_total += len(findings)
                redacted_lines.append(_json.dumps(session, default=str))
        with open(sessions_file, "w") as f:
            for rline in redacted_lines:
                f.write(rline + "\n")
        if pii_total:
            logger.info("PII redaction applied: %d findings across %d sessions", pii_total, len(redacted_lines))
    except Exception as exc:
        logger.warning("PII redaction pass failed: %s", exc)
        return {"error": "PII redaction failed — upload aborted. Try again or report this issue.", "status": 500}

    # Update manifest with PII coverage metadata
    if manifest and isinstance(manifest.get("redaction_summary"), dict):
        manifest["redaction_summary"]["coverage"] = {
            "full": coverage_full,
            "rules_only": coverage_rules_only,
        }

    # Post-PII TruffleHog re-scan: the PII pass rewrote sessions.jsonl,
    # so the scan embedded in the manifest by export_share_to_disk is
    # stale. Run the gate again on the file that's actually about to
    # leave this machine.
    try:
        from ..redaction import trufflehog as trufflehog_scanner

        post_pii_report = trufflehog_scanner.scan_file(sessions_file)
    except Exception as exc:
        logger.warning("Post-PII TruffleHog scan failed: %s", exc)
        return {
            "error": "Post-redaction scan failed — upload aborted.",
            "detail": str(exc),
            "status": 500,
        }
    trufflehog_scanner.write_report(export_dir / "trufflehog.post-pii.json", post_pii_report)
    if manifest and isinstance(manifest.get("redaction_summary"), dict):
        manifest["redaction_summary"]["trufflehog_post_pii"] = post_pii_report.summary()
    if post_pii_report.blocking:
        if manifest:
            manifest["blocked"] = True
            manifest["block_reason"] = post_pii_report.block_reason
            manifest["block_message"] = trufflehog_scanner.format_block_message(post_pii_report)
            with open(manifest_file, "w") as f:
                json.dump(manifest, f, indent=2, default=str)
        return {
            "error": trufflehog_scanner.format_block_message(post_pii_report),
            "block_reason": post_pii_report.block_reason,
            "trufflehog_summary": post_pii_report.summary(),
            "status": 422,
        }
    # Bypass is allowed locally (bundle-export with CLAWJOURNAL_SKIP_
    # TRUFFLEHOG=1 still writes files), but uploading an unscanned
    # share to a remote endpoint should fail closed — the gate exists
    # specifically to catch surviving secrets before they leave the
    # machine.
    if post_pii_report.bypassed:
        return {
            "error": (
                "Refusing to upload: TruffleHog was bypassed via "
                "CLAWJOURNAL_SKIP_TRUFFLEHOG. Unset the variable and "
                "retry, or use bundle-export for local-only output."
            ),
            "block_reason": "trufflehog-bypassed",
            "status": 422,
        }

    if manifest and isinstance(manifest.get("redaction_summary"), dict):
        with open(manifest_file, "w") as f:
            json.dump(manifest, f, indent=2, default=str)

    file_size = sessions_file.stat().st_size
    if file_size > _SHARE_MAX_FILE_SIZE:
        return {
            "error": f"sessions.jsonl is {file_size / (1024*1024):.1f} MB, exceeds 500 MB limit.",
            "status": 400,
        }

    # Compute SHA-256
    sha = hashlib.sha256()
    with open(sessions_file, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    bundle_hash = sha.hexdigest()

    # Read files into memory
    sessions_bytes = sessions_file.read_bytes()
    files: dict[str, tuple[str, bytes, str]] = {
        "sessions": ("sessions.jsonl", sessions_bytes, "application/jsonl"),
    }
    if manifest_file.exists():
        files["manifest"] = ("manifest.json", manifest_file.read_bytes(), "application/json")

    upload_body, content_type = _build_multipart_body(
        fields={
            "share_id": share_id,
            "bundle_id": share_id,
            "bundle_hash": bundle_hash,
            "upload_token": verified_email_token,
        },
        files=files,
    )

    upload_url = f"{_SHARE_INGEST_URL}/upload"
    req = urllib.request.Request(
        upload_url,
        data=upload_body,
        headers={
            "Content-Type": content_type,
            "User-Agent": f"clawjournal/{__version__}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=_SHARE_UPLOAD_TIMEOUT) as resp:
            upload_result = json.loads(resp.read())
        gcs_uri_from_server = upload_result.get("gcs_uri", "")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        try:
            error_data = json.loads(error_body)
            error_msg = error_data.get("error", error_body)
        except json.JSONDecodeError:
            error_msg = error_body
        if exc.code == 409:
            gcs_uri_from_server = ""
        elif exc.code in (400, 401, 403, 429):
            if exc.code in (401, 403):
                _clear_stored_upload_token()
            return {"error": error_msg, "status": exc.code}
        else:
            return {"error": error_msg, "status": 502}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"error": "Could not reach upload service. Please try again.", "status": 502}

    # Count sessions
    session_count = 0
    with open(sessions_file) as f:
        for line in f:
            if line.strip():
                session_count += 1

    gcs_uri = gcs_uri_from_server or f"gs://{_SHARE_GCS_BUCKET}/{_SHARE_GCS_PREFIX}/{share_id}/sessions.jsonl"
    shared_at = datetime.now(timezone.utc).isoformat()

    conn.execute(
        "UPDATE shares SET status = 'shared', shared_at = ?, gcs_uri = ?, bundle_hash = ? WHERE share_id = ?",
        (shared_at, gcs_uri, bundle_hash, share_id),
    )
    conn.commit()

    redaction_summary = manifest.get("redaction_summary", {}) if manifest else {}
    _clear_stored_upload_token()

    return {
        "ok": True,
        "shared_at": shared_at,
        "session_count": session_count,
        "bundle_hash": bundle_hash,
        "redaction_summary": redaction_summary,
    }


class WorkbenchHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the workbench API + static files.

    Auth: every `/api/*` request requires an `Authorization: Bearer <token>`
    header where `<token>` matches `~/.clawjournal/api_token`. `/timeline/*`
    accepts the same bearer token and, for browser navigations only, a
    `clawjournal_token` cookie scoped to `/timeline`. Missing or wrong
    credentials get a 401 with an empty body — no hint about what was wrong.
    Static/SPA shell paths bypass auth. See docs/security-refactor.md §Daemon
    API surface.

    Access logs go to `logger.debug` and receive only the format string
    plus the request line; bodies, query strings, and the `Authorization`
    header are never passed to the logger. If we ever need to log them
    for debugging, scrub them first.
    """

    _last_share_time: float = 0.0

    def log_message(self, format: str, *args: Any) -> None:
        logger.debug(format, *args)

    def _check_api_auth(self) -> bool:
        """Return True if the request is authorized for protected routes.

        Static assets and the SPA shell bypass auth. Transcript-bearing
        routes under `/api/*` and `/timeline/*` require the per-install
        `api_token`. `/api/*` accepts only the `Authorization: Bearer`
        header; `/timeline/*` accepts that header plus a
        `clawjournal_token` cookie for browser navigations. The cookie is
        set by `_serve_static` on SPA HTML responses so a user who has
        opened the workbench can follow `/timeline/*` links with no extra
        handling. Uses `secrets.compare_digest` for constant-time
        comparison.
        """
        from pathlib import Path as _Path
        import secrets as _secrets

        parsed = urlparse(self.path)
        is_api_path = parsed.path.startswith("/api/")
        is_timeline_path = (
            parsed.path == "/timeline" or parsed.path.startswith("/timeline/")
        )
        if not (is_api_path or is_timeline_path):
            return True

        try:
            from ..paths import ensure_api_token
            from .index import INDEX_DB as _INDEX_DB
            expected = ensure_api_token(_Path(str(_INDEX_DB)).parent)
        except Exception:
            logger.exception("Could not resolve api_token for auth check")
            return False

        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            supplied = header[len("Bearer "):].strip()
            if _secrets.compare_digest(supplied, expected):
                return True

        if is_timeline_path:
            cookie_token = _parse_cookie_token(self.headers.get("Cookie"))
            if cookie_token is not None and _secrets.compare_digest(
                cookie_token, expected
            ):
                return True

        return False

    def _reject_unauthenticated(self) -> None:
        """Send a 401 with no body — never reveal what the auth state is."""
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_OPTIONS(self) -> None:
        self.send_response(200)
        origin = _cors_origin(self)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.end_headers()

    def do_GET(self) -> None:
        if not self._check_api_auth():
            self._reject_unauthenticated()
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        params = parse_qs(parsed.query)

        # API routes
        if path == "/api/sessions":
            self._handle_list_sessions(params)
        elif path.startswith("/api/sessions/") and path.endswith("/redaction-report"):
            session_id = path[len("/api/sessions/"):-len("/redaction-report")]
            ai_pii = params.get("ai_pii", [""])[0] == "1"
            self._handle_redaction_report(session_id, ai_pii=ai_pii)
        elif path.startswith("/api/sessions/") and path.endswith("/findings"):
            session_id = path[len("/api/sessions/"):-len("/findings")]
            self._handle_list_session_findings(session_id, params)
        elif path.startswith("/api/sessions/") and path.endswith("/hold-history"):
            session_id = path[len("/api/sessions/"):-len("/hold-history")]
            self._handle_hold_history(session_id)
        elif path.startswith("/api/sessions/") and path.endswith("/redacted"):
            session_id = path[len("/api/sessions/"):-len("/redacted")]
            self._handle_session_redacted(session_id)
        elif path.startswith("/api/sessions/"):
            session_id = path[len("/api/sessions/"):]
            self._handle_get_session(session_id)
        elif path == "/api/search":
            self._handle_search(params)
        elif path == "/api/stats":
            self._handle_stats(params)
        elif path == "/api/dashboard":
            self._handle_dashboard(params)
        elif path == "/api/dashboard/highlights":
            self._handle_highlights(params)
        elif path == "/api/insights":
            self._handle_insights(params)
        elif path == "/api/advisor":
            self._handle_advisor(params)
        elif path == "/api/projects":
            self._handle_projects()
        elif path == "/api/share-ready":
            self._handle_share_ready(params)
        elif path == "/api/scoring/backend":
            self._handle_scoring_backend()
        elif path == "/api/bundles":
            self._handle_list_shares()
        elif path.startswith("/api/bundles/") and path.endswith("/preview"):
            share_id = path[len("/api/bundles/"):-len("/preview")]
            self._handle_preview_share(share_id)
        elif path.startswith("/api/bundles/") and path.endswith("/download"):
            share_id = path[len("/api/bundles/"):-len("/download")]
            self._handle_download_share(share_id)
        elif path.startswith("/api/bundles/"):
            share_id = path[len("/api/bundles/"):]
            self._handle_get_share(share_id)
        elif path == "/api/shares":
            self._handle_list_shares()
        elif path.startswith("/api/shares/") and path.endswith("/preview"):
            share_id = path[len("/api/shares/"):-len("/preview")]
            self._handle_preview_share(share_id)
        elif path.startswith("/api/shares/") and path.endswith("/download"):
            share_id = path[len("/api/shares/"):-len("/download")]
            self._handle_download_share(share_id)
        elif path.startswith("/api/shares/"):
            share_id = path[len("/api/shares/"):]
            self._handle_get_share(share_id)
        elif path == "/api/policies":
            self._handle_list_policies()
        elif path == "/api/allowlist":
            self._handle_list_allowlist()
        elif path == "/api/findings/allowlist":
            self._handle_list_findings_allowlist()
        elif path.startswith("/timeline/"):
            if self._handle_session_timeline(path):
                return
            self._serve_static(parsed.path)
            return
        else:
            self._serve_static(parsed.path)

    def do_POST(self) -> None:
        if not self._check_api_auth():
            self._reject_unauthenticated()
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/sessions/") and path.endswith("/score"):
            session_id = path[len("/api/sessions/"):-len("/score")]
            self._handle_score_session(session_id)
        elif path.startswith("/api/sessions/") and path.endswith("/scan"):
            session_id = path[len("/api/sessions/"):-len("/scan")]
            self._handle_force_scan_session(session_id)
        elif path.startswith("/api/sessions/"):
            session_id = path[len("/api/sessions/"):]
            self._handle_update_session(session_id)
        elif path == "/api/quick-share":
            self._handle_quick_share()
        elif path == "/api/bundles":
            self._handle_create_share()
        elif path.startswith("/api/bundles/") and path.endswith("/export"):
            share_id = path[len("/api/bundles/"):-len("/export")]
            self._handle_export_share(share_id)
        elif path.startswith("/api/bundles/") and path.endswith("/share"):
            share_id = path[len("/api/bundles/"):-len("/share")]
            self._handle_upload_share(share_id)
        elif path == "/api/shares":
            self._handle_create_share()
        elif path.startswith("/api/shares/") and path.endswith("/export"):
            share_id = path[len("/api/shares/"):-len("/export")]
            self._handle_export_share(share_id)
        elif path.startswith("/api/shares/") and path.endswith("/share"):
            share_id = path[len("/api/shares/"):-len("/share")]
            self._handle_upload_share(share_id)
        elif path.startswith("/api/shares/") and path.endswith("/upload"):
            share_id = path[len("/api/shares/"):-len("/upload")]
            self._handle_upload_share(share_id)
        elif path == "/api/policies":
            self._handle_add_policy()
        elif path == "/api/allowlist":
            self._handle_add_allowlist()
        elif path == "/api/findings/allowlist":
            self._handle_add_findings_allowlist()
        elif path == "/api/scan":
            force = parse_qs(parsed.query).get("force", [""])[0] in ("1", "true")
            self._handle_trigger_scan(force=force)
        else:
            _json_response(self, {"error": "Not found"}, 404)

    def do_PATCH(self) -> None:
        if not self._check_api_auth():
            self._reject_unauthenticated()
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        if path == "/api/findings":
            self._handle_patch_findings()
        else:
            _json_response(self, {"error": "Not found"}, 404)

    def do_DELETE(self) -> None:
        if not self._check_api_auth():
            self._reject_unauthenticated()
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path.startswith("/api/policies/"):
            policy_id = path[len("/api/policies/"):]
            self._handle_remove_policy(policy_id)
        elif path.startswith("/api/findings/allowlist/"):
            allowlist_id = path[len("/api/findings/allowlist/"):]
            self._handle_remove_findings_allowlist(allowlist_id)
        elif path.startswith("/api/allowlist/"):
            entry_id = path[len("/api/allowlist/"):]
            self._handle_remove_allowlist(entry_id)
        else:
            _json_response(self, {"error": "Not found"}, 404)

    # --- API handlers ---

    def _handle_list_sessions(self, params: dict[str, list[str]]) -> None:
        conn = open_index()
        try:
            result = query_sessions(
                conn,
                status=params.get("status", [None])[0],
                source=params.get("source", [None])[0],
                project=params.get("project", [None])[0],
                task_type=params.get("task_type", [None])[0],
                recovery_label=params.get("recovery_label", [None])[0],
                failure_attribution=params.get("failure_attribution", [None])[0],
                failure_mode=params.get("failure_mode", [None])[0],
                search_text=params.get("q", [None])[0],
                sort=params.get("sort", ["start_time"])[0],
                order=params.get("order", ["desc"])[0],
                limit=int(params.get("limit", ["50"])[0]),
                offset=int(params.get("offset", ["0"])[0]),
            )
            _parse_json_fields(result)
            _json_response(self, result)
        finally:
            conn.close()

    def _handle_get_session(self, session_id: str) -> None:
        conn = open_index()
        try:
            detail = get_session_detail(conn, session_id)
            if detail is None:
                _json_response(self, {"error": "Session not found"}, 404)
                return
            _parse_json_fields([detail])
            _json_response(self, detail)
        finally:
            conn.close()

    def _handle_update_session(self, session_id: str) -> None:
        body = _read_body(self)
        conn = open_index()
        try:
            ok = update_session(
                conn, session_id,
                status=body.get("status"),
                notes=body.get("notes"),
                reason=body.get("reason"),
                ai_quality_score=body.get("ai_quality_score"),
                ai_score_reason=body.get("ai_score_reason"),
                ai_effort_estimate=body.get("ai_effort_estimate"),
                ai_summary=body.get("ai_summary"),
                ai_scoring_detail=body.get("ai_scoring_detail"),
                ai_task_type=body.get("ai_task_type"),
                ai_outcome_badge=body.get("ai_outcome_badge"),
                ai_value_badges=json.dumps(body["ai_value_badges"]) if isinstance(body.get("ai_value_badges"), list) else body.get("ai_value_badges"),
                ai_risk_badges=json.dumps(body["ai_risk_badges"]) if isinstance(body.get("ai_risk_badges"), list) else body.get("ai_risk_badges"),
                ai_failure_value_score=body.get("ai_failure_value_score"),
                ai_recovery_labels=json.dumps(body["ai_recovery_labels"]) if isinstance(body.get("ai_recovery_labels"), list) else body.get("ai_recovery_labels"),
                ai_failure_attribution=body.get("ai_failure_attribution"),
                ai_failure_modes=json.dumps(body["ai_failure_modes"]) if isinstance(body.get("ai_failure_modes"), list) else body.get("ai_failure_modes"),
                ai_learning_summary=body.get("ai_learning_summary"),
                ai_scorer_backend=body.get("ai_scorer_backend"),
                ai_scorer_model=body.get("ai_scorer_model"),
                ai_rubric_git_sha=body.get("ai_rubric_git_sha"),
                ai_scored_at=body.get("ai_scored_at"),
            )
            # Hold-state transitions are separate from review-status updates
            # — they pass through `set_hold_state` so the audit log and
            # validation stay in one place.
            hold_state = body.get("hold_state")
            if hold_state is not None:
                from .index import set_hold_state
                try:
                    ok = set_hold_state(
                        conn, session_id, hold_state,
                        changed_by="user",
                        reason=body.get("reason"),
                        embargo_until=body.get("embargo_until"),
                    ) and ok
                except ValueError as exc:
                    _json_response(self, {"error": str(exc)}, 400)
                    return
            if ok:
                _json_response(self, {"ok": True})
            else:
                _json_response(self, {"error": "Session not found"}, 404)
        finally:
            conn.close()

    # --- Findings endpoints ---

    def _handle_list_session_findings(self, session_id: str, params: dict) -> None:
        from ..findings import (
            dedupe_findings_by_entity,
            derive_preview,
            load_findings_from_db,
        )
        from .index import read_blob

        group_by = params.get("group_by", [""])[0] == "entity"
        status_filter_raw = params.get("status", [""])[0]
        status_filter = {status_filter_raw} if status_filter_raw else None

        conn = open_index()
        try:
            findings = load_findings_from_db(conn, session_id, status_filter=status_filter)
            blob = read_blob(session_id)
            if group_by:
                groups = dedupe_findings_by_entity(findings)
                # Attach a masked preview per group, derived from the blob —
                # never persisted, never carries the matched text.
                for group in groups:
                    sample_id = group["finding_ids"][0] if group["finding_ids"] else None
                    sample_finding = next(
                        (f for f in findings if f.finding_id == sample_id),
                        None,
                    )
                    if blob is not None and sample_finding is not None:
                        group["sample_preview"] = derive_preview(blob, sample_finding)
                    else:
                        group["sample_preview"] = {
                            "before": "", "after": "", "match_placeholder": "[...]",
                        }
                _json_response(self, {"total": len(groups), "entities": groups})
                return

            out: list[dict[str, Any]] = []
            for finding in findings:
                entry = {
                    "finding_id": finding.finding_id,
                    "engine": finding.engine,
                    "rule": finding.rule,
                    "entity_type": finding.entity_type,
                    "entity_hash": finding.entity_hash,
                    "entity_length": finding.entity_length,
                    "field": finding.field,
                    "message_index": finding.message_index,
                    "tool_field": finding.tool_field,
                    "offset": finding.offset,
                    "length": finding.length,
                    "confidence": finding.confidence,
                    "status": finding.status,
                    "decided_by": finding.decided_by,
                    "decided_at": finding.decided_at,
                    "decision_reason": finding.decision_reason,
                }
                if blob is not None:
                    entry["preview"] = derive_preview(blob, finding)
                out.append(entry)
            _json_response(self, {"total": len(out), "findings": out})
        finally:
            conn.close()

    def _handle_patch_findings(self) -> None:
        from ..findings import set_finding_status

        body = _read_body(self) or {}
        finding_ids = body.get("finding_ids") or []
        status = body.get("status")
        if status not in ("accepted", "ignored"):
            _json_response(self, {"error": "status must be 'accepted' or 'ignored'"}, 400)
            return
        if not isinstance(finding_ids, list) or not finding_ids:
            _json_response(self, {"error": "finding_ids must be a non-empty list"}, 400)
            return

        reason = body.get("reason")
        make_global = bool(body.get("global", False)) and status == "ignored"

        conn = open_index()
        try:
            updated = set_finding_status(
                conn, finding_ids, status,
                reason=reason, also_allowlist=make_global,
            )
            conn.commit()
            _json_response(self, {"updated": updated, "allowlisted": bool(make_global)})
        finally:
            conn.close()

    def _handle_hold_history(self, session_id: str) -> None:
        from .index import get_hold_history

        conn = open_index()
        try:
            history = get_hold_history(conn, session_id)
            _json_response(self, {"total": len(history), "history": history})
        finally:
            conn.close()

    def _handle_force_scan_session(self, session_id: str) -> None:
        from ..config import load_config
        from .findings_pipeline import run_findings_pipeline
        from .index import read_blob

        blob = read_blob(session_id)
        if blob is None:
            _json_response(self, {"error": "Session blob not available"}, 404)
            return
        conn = open_index()
        try:
            result = run_findings_pipeline(
                conn, session_id, blob, config=dict(load_config()), force=True,
            )
            _json_response(self, result)
        finally:
            conn.close()

    # --- Findings allowlist endpoints ---

    def _handle_list_findings_allowlist(self) -> None:
        from ..findings import allowlist_list

        conn = open_index()
        try:
            entries = list(allowlist_list(conn))
            _json_response(self, {"total": len(entries), "entries": entries})
        finally:
            conn.close()

    def _handle_add_findings_allowlist(self) -> None:
        from ..findings import allowlist_add

        body = _read_body(self) or {}
        entity_text = body.get("entity_text")
        if not isinstance(entity_text, str) or not entity_text:
            _json_response(self, {"error": "entity_text is required"}, 400)
            return
        conn = open_index()
        try:
            entry, retro, retro_sessions = allowlist_add(
                conn,
                entity_text=entity_text,
                entity_type=body.get("entity_type"),
                entity_label=body.get("entity_label"),
                reason=body.get("reason"),
            )
            conn.commit()
            _json_response(self, {
                "entry": dict(entry),
                "retroactive_updates": retro,
                "retroactive_sessions": retro_sessions,
            })
        finally:
            conn.close()

    def _handle_remove_findings_allowlist(self, allowlist_id: str) -> None:
        from ..findings import allowlist_remove

        conn = open_index()
        try:
            removed, reverted, reassigned = allowlist_remove(conn, allowlist_id)
            if not removed:
                _json_response(self, {"error": "allowlist entry not found"}, 404)
                return
            conn.commit()
            _json_response(self, {
                "removed": True,
                "reverted": reverted,
                "reassigned": reassigned,
            })
        finally:
            conn.close()

    def _handle_score_session(self, session_id: str) -> None:
        body = _read_body(self) or {}
        backend = body.get("backend", "auto")
        model = body.get("model")

        from ..scoring.scoring import score_session

        conn = open_index()
        try:
            try:
                result = score_session(conn, session_id, model=model, backend=backend)
            except RuntimeError as e:
                _json_response(self, {"error": str(e)}, 503)
                return

            ok = _persist_scoring_result(conn, session_id, result)
            if not ok:
                _json_response(self, {"error": "Session not found"}, 404)
                return

            _maybe_create_trace_note(conn, session_id)

            _json_response(self, {
                "ok": True,
                "ai_quality_score": result.quality,
                "ai_failure_value_score": getattr(result, "failure_value_score", None),
                "ai_recovery_labels": getattr(result, "recovery_labels", []),
                "ai_failure_attribution": getattr(result, "failure_attribution", ""),
                "ai_failure_modes": getattr(result, "failure_modes", []),
                "ai_learning_summary": getattr(result, "learning_summary", ""),
                "reason": result.reason,
                "task_type": result.task_type,
                "outcome": result.outcome_label,
                "summary": result.summary,
            })
        finally:
            conn.close()

    def _handle_search(self, params: dict[str, list[str]]) -> None:
        q = params.get("q", [""])[0]
        if not q:
            _json_response(self, [])
            return
        conn = open_index()
        try:
            results = search_fts(
                conn, q,
                limit=int(params.get("limit", ["50"])[0]),
                offset=int(params.get("offset", ["0"])[0]),
            )
            _parse_json_fields(results)
            _json_response(self, results)
        finally:
            conn.close()

    def _handle_stats(self, params: dict[str, list[str]]) -> None:
        start = params.get("start", [None])[0]
        end = params.get("end", [None])[0]
        conn = open_index()
        try:
            stats = get_stats(conn, start=start, end=end)
            _json_response(self, stats)
        finally:
            conn.close()

    def _handle_dashboard(self, params: dict[str, list[str]]) -> None:
        start = params.get("start", [None])[0]
        end = params.get("end", [None])[0]
        conn = open_index()
        try:
            data = get_dashboard_analytics(conn, start=start, end=end)
            _json_response(self, data)
        finally:
            conn.close()

    def _handle_highlights(self, params: dict[str, list[str]]) -> None:
        def _int_param(name: str, default: int, lo: int, hi: int) -> int:
            raw = params.get(name, [str(default)])[0]
            try:
                value = int(raw)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, value))

        days = _int_param("days", 7, 1, 90)
        top_n = _int_param("top", 3, 1, 12)
        min_quality = _int_param("min_quality", 4, 1, 5)

        conn = open_index()
        try:
            data = get_highlights(
                conn, days=days, top_n=top_n, min_quality=min_quality
            )
            _json_response(self, data)
        finally:
            conn.close()

    def _handle_insights(self, params: dict) -> None:
        from .index import get_insights
        start = params.get("start", [None])[0]
        end = params.get("end", [None])[0]
        conn = open_index()
        try:
            data = get_insights(conn, start=start, end=end)
            _json_response(self, data)
        finally:
            conn.close()

    def _handle_advisor(self, params: dict) -> None:
        from ..scoring.insights import collect_advisor_stats, generate_recommendations
        try:
            days = int(params.get("days", ["7"])[0])
        except (ValueError, TypeError):
            days = 7
        conn = open_index()
        try:
            stats = collect_advisor_stats(conn, days=days)
            advisor = generate_recommendations(stats)
            _json_response(self, advisor)
        finally:
            conn.close()

    def _handle_projects(self) -> None:
        conn = open_index()
        try:
            rows = conn.execute(
                "SELECT project, source, COUNT(*) as session_count, "
                "SUM(input_tokens + output_tokens) as total_tokens "
                "FROM sessions GROUP BY project, source ORDER BY project"
            ).fetchall()
            _json_response(self, [dict(r) for r in rows])
        finally:
            conn.close()

    def _handle_session_redacted(self, session_id: str) -> None:
        """Return session with secrets redacted — for pre-share review."""
        conn = open_index()
        try:
            detail = get_session_detail(conn, session_id)
            if detail is None:
                _json_response(self, {"error": "Session not found"}, 404)
                return
            settings = get_effective_share_settings(conn, load_config())
            detail, _, _ = apply_share_redactions(
                conn,
                detail,
                custom_strings=settings["custom_strings"],
                user_allowlist=settings["allowlist_entries"],
                extra_usernames=settings["extra_usernames"],
                blocked_domains=settings["blocked_domains"],
            )
            _json_response(self, detail)
        finally:
            conn.close()

    def _handle_redaction_report(self, session_id: str, *, ai_pii: bool = False) -> None:
        """Return redacted session WITH the full redaction log for review.

        When *ai_pii* is True, also runs AI-based PII detection (hybrid:
        rule-based + LLM agent) and applies the findings on top of the
        regex-based secret redaction.
        """
        conn = open_index()
        try:
            detail = get_session_detail(conn, session_id)
            if detail is None:
                _json_response(self, {"error": "Session not found"}, 404)
                return
            settings = get_effective_share_settings(conn, load_config())
            detail, redaction_count, redaction_log = apply_share_redactions(
                conn,
                detail,
                custom_strings=settings["custom_strings"],
                user_allowlist=settings["allowlist_entries"],
                extra_usernames=settings["extra_usernames"],
                blocked_domains=settings["blocked_domains"],
            )

            # AI-based PII detection (hybrid: rule-based + LLM agent)
            ai_pii_count = 0
            ai_pii_findings: list[dict] = []
            ai_coverage = "rules_only"
            if ai_pii:
                try:
                    from ..redaction.pii import review_session_pii_with_agent, apply_findings_to_session
                    # Use AI-only detection (skip redundant rule-based PII scan
                    # since redact_session() already handles regex patterns)
                    findings = review_session_pii_with_agent(
                        detail, ignore_errors=False, backend="auto",
                    )
                    ai_coverage = "full"
                    if findings:
                        detail, ai_pii_count = apply_findings_to_session(detail, findings)
                        ai_pii_findings = [
                            {
                                "entity_type": f.get("entity_type", ""),
                                "entity_text": f.get("entity_text", ""),
                                "confidence": f.get("confidence", 0),
                                "field": f.get("field", ""),
                                "source": f.get("source", ""),
                            }
                            for f in findings
                        ]
                except Exception as exc:
                    logger.warning("AI PII detection failed for %s: %s", session_id, exc)
                    ai_coverage = "rules_only"

            _json_response(self, {
                "session_id": session_id,
                "redaction_count": redaction_count + ai_pii_count,
                "redaction_log": redaction_log,
                "ai_pii_findings": ai_pii_findings,
                "ai_coverage": ai_coverage,
                "redacted_session": detail,
            })
        finally:
            conn.close()

    def _handle_list_allowlist(self) -> None:
        """Return current allowlist entries from config."""
        from ..config import load_config
        config = load_config()
        entries = config.get("allowlist_entries", [])
        _json_response(self, entries)

    def _handle_add_allowlist(self) -> None:
        """Add a new allowlist entry to config."""
        import uuid
        from ..config import load_config, save_config
        body = _read_body(self)

        entry_type = body.get("type")
        if entry_type not in ("exact", "pattern", "category"):
            _json_response(self, {"error": "type must be exact, pattern, or category"}, 400)
            return

        entry: dict[str, Any] = {
            "id": uuid.uuid4().hex[:12],
            "type": entry_type,
            "added": datetime.now(timezone.utc).isoformat(),
        }
        if entry_type == "exact":
            if not body.get("text"):
                _json_response(self, {"error": "text required for exact type"}, 400)
                return
            entry["text"] = body["text"]
        elif entry_type == "pattern":
            if not body.get("regex"):
                _json_response(self, {"error": "regex required for pattern type"}, 400)
                return
            entry["regex"] = body["regex"]
        elif entry_type == "category":
            if not body.get("match_type"):
                _json_response(self, {"error": "match_type required for category type"}, 400)
                return
            entry["match_type"] = body["match_type"]

        if body.get("reason"):
            entry["reason"] = body["reason"]

        config = load_config()
        entries = config.get("allowlist_entries", [])
        entries.append(entry)
        config["allowlist_entries"] = entries
        save_config(config)
        _json_response(self, {"ok": True, "entry": entry})

    def _handle_remove_allowlist(self, entry_id: str) -> None:
        """Remove an allowlist entry by ID."""
        from ..config import load_config, save_config
        config = load_config()
        entries = config.get("allowlist_entries", [])
        new_entries = [e for e in entries if e.get("id") != entry_id]
        if len(new_entries) == len(entries):
            _json_response(self, {"error": "Entry not found"}, 404)
            return
        config["allowlist_entries"] = new_entries
        save_config(config)
        _json_response(self, {"ok": True})

    def _handle_scoring_backend(self) -> None:
        """Return the default AI scoring backend detected for this daemon."""
        from ..scoring.backends import resolve_backend
        display_names = {"claude": "Claude Code", "codex": "Codex", "openclaw": "OpenClaw"}
        try:
            backend = resolve_backend(backend="auto")
        except RuntimeError:
            _json_response(self, {"backend": None, "display_name": None})
            return
        _json_response(self, {
            "backend": backend,
            "display_name": display_names.get(backend, backend),
        })

    def _handle_share_ready(self, params: dict[str, list[str]]) -> None:
        """Return stats for sessions ready to share.

        By default only `review_status='approved'` sessions are returned.
        Pass `?include_unapproved=1` to also return non-approved sessions
        so the Share Preview can offer a broader pool to pick from.
        """
        include_unapproved = params.get("include_unapproved", [""])[0] == "1"
        conn = open_index()
        try:
            settings = get_effective_share_settings(conn, load_config())
            stats = get_share_ready_stats(
                conn,
                excluded_projects=settings["excluded_projects"],
                include_unapproved=include_unapproved,
            )
            _json_response(self, stats)
        finally:
            conn.close()

    def _handle_quick_share(self) -> None:
        """Combined create + share in one call."""
        with _share_rate_lock:
            now = time.time()
            elapsed = now - WorkbenchHandler._last_share_time
            if elapsed < _SHARE_COOLDOWN_SECONDS:
                _json_response(self, {
                    "error": f"Rate limited. Try again in {int(_SHARE_COOLDOWN_SECONDS - elapsed)}s.",
                }, 429)
                return
            # Mark as in-flight to prevent concurrent requests passing the check
            WorkbenchHandler._last_share_time = now

        body = _read_body(self)
        session_ids = body.get("session_ids", [])
        note = body.get("note")
        if not session_ids:
            _json_response(self, {"error": "session_ids required"}, 400)
            return

        conn = open_index()
        try:
            settings = get_effective_share_settings(conn, load_config())
            share_id = create_share(conn, session_ids, note=note)
            result = upload_share(
                conn,
                share_id,
                force=True,
                custom_strings=settings["custom_strings"],
                extra_usernames=settings["extra_usernames"],
                excluded_projects=settings["excluded_projects"],
                blocked_domains=settings["blocked_domains"],
                allowlist_entries=settings["allowlist_entries"],
            )
            result["share_id"] = share_id
            result["bundle_id"] = share_id
            if result.get("ok"):
                with _share_rate_lock:
                    WorkbenchHandler._last_share_time = time.time()
                _json_response(self, result)
            else:
                status_code = result.pop("status", 500)
                _json_response(self, result, status_code)
        except Exception as exc:
            logger.exception("Quick share failed")
            _json_response(self, {"error": str(exc)}, 500)
        finally:
            conn.close()

    def _handle_list_shares(self) -> None:
        conn = open_index()
        try:
            shares = get_shares(conn)
            for b in shares:
                b.pop("gcs_uri", None)
            _json_response(self, shares)
        finally:
            conn.close()

    def _handle_get_share(self, share_id: str) -> None:
        conn = open_index()
        try:
            share = get_share(conn, share_id)
            if share is None:
                _json_response(self, {"error": "Share not found"}, 404)
                return
            share.pop("gcs_uri", None)
            _with_legacy_bundle_alias(share)
            _json_response(self, share)
        finally:
            conn.close()

    def _handle_create_share(self) -> None:
        body = _read_body(self)
        session_ids = body.get("session_ids", [])
        if not session_ids:
            _json_response(self, {"error": "session_ids required"}, 400)
            return
        conn = open_index()
        try:
            share_id = create_share(
                conn, session_ids,
                attestation=body.get("attestation"),
                note=body.get("note"),
            )
            _json_response(self, {"share_id": share_id, "bundle_id": share_id}, 201)
        finally:
            conn.close()

    def _handle_preview_share(self, share_id: str) -> None:
        """Return a readable summary of an exported share."""
        conn = open_index()
        try:
            share = get_share(conn, share_id)
            if share is None:
                _json_response(self, {"error": "Share not found"}, 404)
                return

            # Check both default and custom export paths
            export_dir = CONFIG_DIR / "shares" / share_id

            # If manifest stored an export_path, try that first
            manifest_data = share.get("manifest")
            if isinstance(manifest_data, dict) and manifest_data.get("export_path"):
                custom_dir = Path(manifest_data["export_path"])
                if (custom_dir / "sessions.jsonl").exists():
                    export_dir = custom_dir

            sessions_file = export_dir / "sessions.jsonl"
            manifest_file = export_dir / "manifest.json"

            # Check if exported
            if not sessions_file.exists():
                _json_response(self, {"error": "Share not exported yet. Export first."}, 400)
                return

            # Read manifest
            manifest = {}
            if manifest_file.exists():
                with open(manifest_file) as f:
                    manifest = json.load(f)

            # Build session previews from the JSONL
            previews = []
            total_tokens = 0
            total_messages = 0
            with open(sessions_file) as f:
                for line in f:
                    if not line.strip():
                        continue
                    session = json.loads(line)
                    msgs = session.get("messages", [])
                    input_tok = session.get("input_tokens", 0) or 0
                    output_tok = session.get("output_tokens", 0) or 0
                    total_tokens += input_tok + output_tok
                    total_messages += len(msgs)

                    # First user message as preview
                    first_user_msg = ""
                    for m in msgs:
                        if m.get("role") == "user":
                            content = m.get("content", "")
                            if isinstance(content, str):
                                first_user_msg = content[:200]
                            elif isinstance(content, list):
                                for block in content:
                                    if isinstance(block, str):
                                        first_user_msg = block[:200]
                                        break
                                    if isinstance(block, dict) and block.get("text"):
                                        first_user_msg = block["text"][:200]
                                        break
                            break

                    previews.append({
                        "session_id": session.get("session_id"),
                        "project": session.get("project"),
                        "source": session.get("source"),
                        "model": session.get("model"),
                        "display_title": session.get("display_title", ""),
                        "message_count": len(msgs),
                        "input_tokens": input_tok,
                        "output_tokens": output_tok,
                        "first_user_message": first_user_msg,
                        "ai_quality_score": session.get("ai_quality_score"),
                        "ai_failure_value_score": session.get("ai_failure_value_score"),
                        "ai_failure_attribution": session.get("ai_failure_attribution"),
                        "ai_recovery_labels": session.get("ai_recovery_labels"),
                        "ai_failure_modes": session.get("ai_failure_modes"),
                    })

            file_size = sessions_file.stat().st_size

            _json_response(self, {
                "share_id": share_id,
                "bundle_id": share_id,
                "status": share.get("status"),
                "session_count": len(previews),
                "total_tokens": total_tokens,
                "total_messages": total_messages,
                "file_size_bytes": file_size,
                "export_path": str(export_dir),
                "manifest": manifest,
                "sessions": previews,
            })
        finally:
            conn.close()

    def _handle_export_share(self, share_id: str) -> None:
        body = _read_body(self)
        output_path = body.get("output_path")

        conn = open_index()
        try:
            share = get_share(conn, share_id)
            if share is None:
                _json_response(self, {"error": "Share not found"}, 404)
                return

            settings = get_effective_share_settings(conn, load_config())
            export_dir, manifest = export_share_to_disk(
                conn,
                share_id,
                share,
                output_path=output_path,
                custom_strings=settings["custom_strings"],
                extra_usernames=settings["extra_usernames"],
                excluded_projects=settings["excluded_projects"],
                blocked_domains=settings["blocked_domains"],
                allowlist_entries=settings["allowlist_entries"],
            )
            if export_dir is None:
                _json_response(self, {"error": "output_path must be under home directory or /tmp"}, 400)
                return

            if manifest.get("blocked"):
                _json_response(self, {
                    "error": manifest.get("block_message") or "Share blocked by TruffleHog",
                    "block_reason": manifest.get("block_reason"),
                    "export_path": str(export_dir),
                    "trufflehog_summary": manifest.get("redaction_summary", {}).get("trufflehog"),
                }, 422)
                return

            _json_response(self, {
                "ok": True,
                "export_path": str(export_dir),
                "session_count": len(manifest["sessions"]),
            })
        finally:
            conn.close()

    def _handle_download_share(self, share_id: str) -> None:
        """Generate a zip of the share and serve it as a browser download."""
        conn = open_index()
        try:
            share = get_share(conn, share_id)
            if share is None:
                _json_response(self, {"error": "Share not found"}, 404)
                return

            settings = get_effective_share_settings(conn, load_config())
            export_dir, _manifest = export_share_to_disk(
                conn,
                share_id,
                share,
                custom_strings=settings["custom_strings"],
                extra_usernames=settings["extra_usernames"],
                excluded_projects=settings["excluded_projects"],
                blocked_domains=settings["blocked_domains"],
                allowlist_entries=settings["allowlist_entries"],
            )
            if export_dir is None:
                _json_response(self, {"error": "Failed to prepare download"}, 500)
                return

            if _manifest.get("blocked"):
                _json_response(self, {
                    "error": _manifest.get("block_message") or "Share blocked by TruffleHog",
                    "block_reason": _manifest.get("block_reason"),
                    "trufflehog_summary": _manifest.get("redaction_summary", {}).get("trufflehog"),
                }, 422)
                return

            sessions_content = (export_dir / "sessions.jsonl").read_bytes()
            manifest_content = (export_dir / "manifest.json").read_bytes()
            trufflehog_path = export_dir / "trufflehog.json"
            trufflehog_content = (
                trufflehog_path.read_bytes() if trufflehog_path.exists() else None
            )

            # Create in-memory zip
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.writestr("sessions.jsonl", sessions_content)
                zf.writestr("manifest.json", manifest_content)
                if trufflehog_content is not None:
                    zf.writestr("trufflehog.json", trufflehog_content)
            zip_bytes = buf.getvalue()

            # Serve the zip
            date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
            filename = f"clawjournal-share-{share_id[:8]}-{date_str}.zip"
            self.send_response(200)
            self.send_header("Content-Type", "application/zip")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(zip_bytes)))
            origin = _cors_origin(self)
            if origin:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(zip_bytes)
        finally:
            conn.close()

    def _handle_upload_share(self, share_id: str) -> None:
        """Upload a share to the hosted ingest service."""
        with _share_rate_lock:
            now = time.time()
            elapsed = now - WorkbenchHandler._last_share_time
            if elapsed < _SHARE_COOLDOWN_SECONDS:
                _json_response(self, {
                    "error": f"Rate limited. Try again in {int(_SHARE_COOLDOWN_SECONDS - elapsed)}s.",
                }, 429)
                return

        body = _read_body(self)
        force = body.get("force", False)

        conn = open_index()
        try:
            settings = get_effective_share_settings(conn, load_config())
            result = upload_share(
                conn,
                share_id,
                force=force,
                custom_strings=settings["custom_strings"],
                extra_usernames=settings["extra_usernames"],
                excluded_projects=settings["excluded_projects"],
                blocked_domains=settings["blocked_domains"],
                allowlist_entries=settings["allowlist_entries"],
            )
            if result.get("ok"):
                with _share_rate_lock:
                    WorkbenchHandler._last_share_time = time.time()
                _json_response(self, result)
            else:
                status_code = result.pop("status", 500)
                _json_response(self, result, status_code)
        except Exception as exc:
            logger.exception("Upload failed for share %s", share_id)
            _json_response(self, {"error": str(exc)}, 500)
        finally:
            conn.close()

    def _handle_list_policies(self) -> None:
        conn = open_index()
        try:
            policies = get_policies(conn)
            _json_response(self, policies)
        finally:
            conn.close()

    def _handle_add_policy(self) -> None:
        body = _read_body(self)
        policy_type = body.get("policy_type")
        value = body.get("value")
        if not policy_type or not value:
            _json_response(self, {"error": "policy_type and value required"}, 400)
            return
        conn = open_index()
        try:
            policy_id = add_policy(conn, policy_type, value, reason=body.get("reason"))
            _json_response(self, {"policy_id": policy_id}, 201)
        finally:
            conn.close()

    def _handle_remove_policy(self, policy_id: str) -> None:
        conn = open_index()
        try:
            ok = remove_policy(conn, policy_id)
            if ok:
                _json_response(self, {"ok": True})
            else:
                _json_response(self, {"error": "Policy not found"}, 404)
        finally:
            conn.close()

    def _handle_trigger_scan(self, *, force: bool = False) -> None:
        """Trigger an immediate scan (used by the UI refresh button).

        With `force=true`, rebuilds findings for every session in the DB
        after the normal scan pass. Functionally equivalent to
        `clawjournal scan --force --all` — useful when the frontend needs
        to pick up an engine/allowlist change without shelling out.
        """
        scanner = getattr(self.server, "_scanner", None)
        if scanner:
            results = scanner.scan_once()
            scanner.trigger_auto_score()
            payload: dict[str, Any] = {"ok": True, "new_sessions": results}
            if force:
                from ..config import load_config as _load_config
                from .findings_pipeline import run_findings_pipeline
                from .index import read_blob
                conn = open_index()
                processed = 0
                errored: list[dict[str, Any]] = []
                try:
                    rows = conn.execute("SELECT session_id FROM sessions").fetchall()
                    cfg = dict(_load_config())
                    for row in rows:
                        sid = row["session_id"]
                        blob = read_blob(sid)
                        if blob is None:
                            continue
                        try:
                            run_findings_pipeline(conn, sid, blob, config=cfg, force=True)
                            processed += 1
                        except Exception as exc:  # noqa: BLE001
                            errored.append({"session_id": sid, "error": str(exc)})
                finally:
                    conn.close()
                payload["force_rescan"] = {"processed": processed, "errored": errored}
            _json_response(self, payload)
        else:
            _json_response(self, {"error": "Scanner not available"}, 503)

    # --- Static file serving ---

    def _serve_static(self, path: str) -> None:
        """Serve frontend static files, falling back to index.html for SPA routing."""
        # Backward compatibility for older bookmarks/openers that prefixed SPA
        # routes with /traces.
        if path == "/traces" or path.startswith("/traces/"):
            path = path[len("/traces"):] or "/"

        if path == "/" or path == "":
            path = "/index.html"

        file_path = (FRONTEND_DIST / path.lstrip("/")).resolve()
        if not file_path.is_relative_to(FRONTEND_DIST.resolve()):
            self.send_error(403)
            return

        # SPA fallback: if file doesn't exist, serve index.html
        if not file_path.exists() or not file_path.is_file():
            file_path = FRONTEND_DIST / "index.html"

        if not file_path.exists():
            # No frontend built yet — serve a placeholder
            self._serve_placeholder()
            return

        content_types = {
            ".html": "text/html",
            ".js": "application/javascript",
            ".css": "text/css",
            ".json": "application/json",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".map": "application/json",
        }
        ext = file_path.suffix.lower()
        content_type = content_types.get(ext, "application/octet-stream")

        try:
            data = file_path.read_bytes()
            if content_type == "text/html":
                data = self._inject_api_token(data)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if content_type == "text/html":
                self._maybe_set_api_token_cookie()
            self.end_headers()
            self.wfile.write(data)
        except OSError:
            self.send_error(404)

    def _maybe_set_api_token_cookie(self) -> None:
        """Set the `clawjournal_token` cookie on SPA HTML responses.

        The cookie is what lets a browser that has opened the workbench
        follow `/timeline/<key>` links without manually attaching an
        `Authorization` header. The cookie is intentionally scoped to
        `/timeline` so it cannot unlock the wider `/api/*` surface.
        Silent fall-through on any failure — worst case, the browser
        falls back to the existing 401 flow.
        """
        try:
            from pathlib import Path as _Path
            from ..paths import ensure_api_token
            from .index import INDEX_DB as _INDEX_DB

            token = ensure_api_token(_Path(str(_INDEX_DB)).parent)
        except Exception:
            logger.exception("Could not resolve api_token for cookie set")
            return
        self.send_header("Set-Cookie", _api_token_cookie_header(token))

    def _handle_session_timeline(self, path: str) -> bool:
        requested = unquote(path[len("/timeline/"):])
        if not requested:
            return False

        conn = open_index()
        try:
            legacy_row = conn.execute(
                "SELECT session_key FROM sessions WHERE session_id = ? LIMIT 1",
                (requested,),
            ).fetchone()
            if legacy_row is not None:
                session_key = legacy_row["session_key"]
                if session_key:
                    self._redirect(canonical_session_path(str(session_key)))
                    return True
                # Legacy workbench row exists but has no `session_key`
                # yet — most likely a pre-ADR-001 session that hasn't been
                # re-scanned through `events ingest`. Surface the
                # pending-ingest page with a 404 rather than falling
                # through to the SPA shell (the SPA has no /timeline/
                # route).
                body = render_not_found_html(requested).encode("utf-8")
                self.send_response(404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return True

            page = load_timeline_page(conn, requested)
        finally:
            conn.close()

        if page.root is None and page.workbench_row is None:
            body = render_not_found_html(requested).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True

        if page.redirect_session_key:
            self._redirect(canonical_session_path(page.redirect_session_key))
            return True

        body = render_timeline_html(page).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _inject_api_token(self, data: bytes) -> bytes:
        # Inject the per-install API token so same-origin frontend fetches
        # can reach `/api/*` without the user handling it. Loopback-only, so
        # it never leaves the local machine. JS reads `window.__CLAWJOURNAL_API_TOKEN__`.
        if b"__CLAWJOURNAL_API_TOKEN__" in data:
            return data
        try:
            from pathlib import Path as _Path
            from ..paths import ensure_api_token
            from .index import INDEX_DB as _INDEX_DB
            token = ensure_api_token(_Path(str(_INDEX_DB)).parent)
            safe = token.replace("\\", "\\\\").replace('"', '\\"')
            injection = (
                f'<script>window.__CLAWJOURNAL_API_TOKEN__="{safe}";</script>'
            ).encode()
            if b"</head>" in data:
                return data.replace(b"</head>", injection + b"</head>", 1)
            return injection + data
        except Exception:
            logger.exception("Failed to inject API token into index.html")
            return data

    def _serve_placeholder(self) -> None:
        """Serve a minimal HTML page when the frontend isn't built yet."""
        html = """<!DOCTYPE html>
<html>
<head><title>ClawJournal Workbench</title>
<style>
body { font-family: system-ui, sans-serif; max-width: 600px; margin: 80px auto; padding: 0 20px; color: #333; }
h1 { font-size: 1.4em; }
code { background: #f0f0f0; padding: 2px 6px; border-radius: 3px; }
pre { background: #f0f0f0; padding: 12px; border-radius: 6px; overflow-x: auto; }
.api-link { color: #0066cc; }
</style>
</head>
<body>
<h1>ClawJournal Workbench</h1>
<p>The API is running. The frontend hasn't been built yet.</p>
<p>To build the frontend:</p>
<pre>cd clawjournal/web/frontend
npm install
npm run build</pre>
<p>API endpoints available:</p>
<ul>
<li><a class="api-link" href="/api/stats">/api/stats</a> — Index statistics</li>
<li><a class="api-link" href="/api/sessions">/api/sessions</a> — Session list</li>
<li><a class="api-link" href="/api/projects">/api/projects</a> — Projects</li>
<li><a class="api-link" href="/api/shares">/api/shares</a> — Shares</li>
<li><a class="api-link" href="/api/policies">/api/policies</a> — Policies</li>
</ul>
</body>
</html>"""
        data = self._inject_api_token(html.encode("utf-8"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(data)))
        self._maybe_set_api_token_cookie()
        self.end_headers()
        self.wfile.write(data)


def run_server(
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    source_filter: str | None = None,
    remote: bool = False,
) -> None:
    """Start the workbench daemon — scanner + HTTP server."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    scanner = Scanner(source_filter=source_filter)

    # Start HTTP server first so it's responsive immediately
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), WorkbenchHandler)
    except OSError:
        server = ThreadingHTTPServer(("127.0.0.1", 0), WorkbenchHandler)
        port = server.server_address[1]
    server._scanner = scanner  # type: ignore[attr-defined]

    url = f"http://localhost:{port}/"
    logger.info("Workbench running at %s", url)

    if remote:
        import socket
        hostname = socket.gethostname()
        print(f"\nRemote access — run this on your local machine:")
        print(f"  ssh -L {port}:localhost:{port} <user>@{hostname}")
        print(f"Then open {url}\n")

    if open_browser and not remote:
        webbrowser.open(url)

    # Run initial scan in background, then start periodic scanner
    def _initial_scan() -> None:
        logger.info("Running initial scan...")
        results = scanner.scan_once()
        scanner.trigger_auto_score()
        total = sum(results.values())
        logger.info(
            "Initial scan complete: %d sessions indexed, %d subagent relationships linked",
            total,
            scanner.last_linked_count,
        )
        scanner.start()
        logger.info("Background scanner started (interval: %ds)", SCAN_INTERVAL)

    threading.Thread(target=_initial_scan, daemon=True).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        scanner.stop()
        server.shutdown()
