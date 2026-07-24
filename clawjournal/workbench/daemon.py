"""Local daemon for the scientist workbench — scanner + HTTP API."""

import hashlib
import io
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import zipfile
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from functools import partial
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterator
from urllib.parse import parse_qs, unquote, urlparse

from .. import __version__
from ..auto_upload_client import (
    RECURRING_CADENCE_DAYS,
    RECURRING_UPLOAD_API_VERSION,
    comparable_origin,
)
from ..redaction.anonymizer import Anonymizer
from ..scoring.badges import compute_all_badges
from ..scoring.backends import (
    PERMANENT_BACKEND_FAILURE_MARKERS,
    SUPPORTED_BACKENDS,
    detect_available_backend,
    installed_fallback_chain,
    is_backend_unavailable_error,
    require_backend_command,
    resolve_backend,
)
from ..scoring.overrides import (
    failure_evidence_from_detail,
    merge_failure_evidence,
    normalize_failure_evidence,
    requires_failure_evidence,
)
from ..config import (
    CONFIG_DIR,
    RECURRING_ENROLLMENT_GRANT_CONFIG_KEYS,
    load_config,
    save_config,
)
from .findings_pipeline import (
    drain_findings_backfill,
    run_findings_pipeline,
)
from .frontend_snapshot import DEFAULT_FRONTEND_DIST, FrontendSnapshot
from .index import (
    add_policy,
    already_shared_revision_blockers,
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
    release_gate_blockers,
    RevisionConflictError,
    revision_review_blockers,
    remove_policy,
    search_fts,
    SCORE_SETTLE_SECONDS,
    session_matches_excluded_projects,
    share_predecessor_blockers,
    share_revision_blockers,
    source_scope_blockers,
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
    CLAUDE_SCIENCE_SOURCE,
    CODEX_SOURCE,
    COPILOT_SOURCE,
    CURSOR_SOURCE,
    GEMINI_SOURCE,
    KIMI_SOURCE,
    OPENCODE_SOURCE,
    OPENCLAW_SOURCE,
    WORKBUDDY_SOURCE,
    discover_projects,
    parse_project_sessions,
)

logger = logging.getLogger(__name__)


class _StrictScanLogFilter(logging.Filter):
    """Suppress unbounded log records emitted by one strict-scan thread."""

    def __init__(self, thread_id: int):
        super().__init__()
        self.thread_id = thread_id

    def filter(self, record: logging.LogRecord) -> bool:
        return (
            record.thread != self.thread_id
            or getattr(record, "strict_scan_safe", False) is True
        )


# Serializes scan passes ACROSS processes (CLI commands vs the serve daemon):
# SQLite allows one writer at a time, and a daemon background pass can hold
# write transactions long enough that a concurrent CLI scan's upserts blow
# through the busy timeout and fail. The lock file lives next to the index
# database — the contended resource — which also isolates monkeypatched test
# indexes from each other.
SCAN_LOCK_FILENAME = "scan.lock"
# Longer than a full background pass on a large corpus, so a strict scan
# waiting on the lock outlives the daemon tick that holds it.
SCAN_LOCK_WAIT_SECONDS = 300.0
# Normal scans fail closed after this wait instead of bypassing the lock.
# Their interactive callers (`clawjournal recent`, the share preflight) have
# limited wait feedback, so keep the ceiling short.
SCAN_ONCE_LOCK_WAIT_SECONDS = 15.0
_SCAN_LOCK_POLL_SECONDS = 0.5


class ScanBusyError(RuntimeError):
    """Raised when a normal scan cannot acquire the cross-process scan lock."""


@contextmanager
def _scan_process_lock(
    *,
    wait_seconds: float | None,
    on_wait: Callable[[], None] | None = None,
) -> Iterator[bool]:
    """Acquire the cross-process scan lock; yields whether it was acquired.

    Mirrors ``auto_upload.whole_run_lock``: an OS-level lock the kernel
    releases on process death, so a crashed scan can never wedge future
    scans. ``wait_seconds=None`` makes a single non-blocking attempt;
    otherwise attempts are polled until the deadline passes. ``on_wait``
    fires once if the first attempt fails, before any waiting starts.
    """

    from .index import INDEX_DB

    path = Path(str(INDEX_DB)).parent / SCAN_LOCK_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    file = path.open("a+b")
    acquired = False

    def _try_acquire() -> bool:
        if os.name == "nt":
            import msvcrt

            try:
                # Byte-range locking needs at least one byte; unlike the
                # single-attempt whole_run_lock this is retried in a poll
                # loop, so guard on the real size instead of appending a
                # byte per attempt.
                if os.fstat(file.fileno()).st_size == 0:
                    file.write(b"0")
                    file.flush()
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
                return True
            except OSError:
                return False
        import fcntl

        try:
            fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except BlockingIOError:
            return False

    try:
        acquired = _try_acquire()
        if not acquired and wait_seconds is not None:
            if on_wait is not None:
                try:
                    on_wait()
                except Exception:
                    # A broken wait notice (e.g. a closed stderr pipe) must
                    # not abort the scan it merely narrates.
                    pass
            deadline = time.monotonic() + wait_seconds
            while time.monotonic() < deadline:
                time.sleep(_SCAN_LOCK_POLL_SECONDS)
                acquired = _try_acquire()
                if acquired:
                    break
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


@contextmanager
def _strict_scan_log_guard() -> Iterator[None]:
    """Allow only explicitly bounded logs from the current strict scan.

    Parser and storage helpers predate strict automatic-upload scans and may
    log project paths or session identifiers internally. Attach a thread-bound
    filter to every configured handler while the strict scan runs so those
    lower-level records cannot escape, without muting ordinary scans or other
    concurrent threads.
    """

    log_filter = _StrictScanLogFilter(threading.get_ident())
    handlers: list[logging.Handler] = []
    seen: set[int] = set()
    loggers = [logging.getLogger()]
    # Snapshot the registry under the logging module lock: a concurrent
    # first-time getLogger() in another daemon thread mutates loggerDict, and an
    # unlocked values() iteration would raise "dictionary changed size during
    # iteration" and abort the strict scan (spurious runner_crash + backoff).
    with logging._lock:
        registered = list(logging.Logger.manager.loggerDict.values())
    loggers.extend(
        candidate
        for candidate in registered
        if isinstance(candidate, logging.Logger)
    )
    for configured_logger in loggers:
        for handler in configured_logger.handlers:
            if id(handler) in seen:
                continue
            seen.add(id(handler))
            handlers.append(handler)
            handler.addFilter(log_filter)
    if logging.lastResort is not None and id(logging.lastResort) not in seen:
        handlers.append(logging.lastResort)
        logging.lastResort.addFilter(log_filter)
    try:
        yield
    finally:
        for handler in handlers:
            handler.removeFilter(log_filter)


def _log_strict_scan_failure(*, source: str, stage: str, code: str) -> None:
    """Emit only allowlisted telemetry fields for a strict scan failure."""

    logger.warning(
        "Strict scan failure source=%s stage=%s code=%s",
        source,
        stage,
        code,
        extra={"strict_scan_safe": True},
    )

DEFAULT_PORT = 8384
SCAN_INTERVAL = 60  # seconds
# A scan pass over a large corpus can take longer than SCAN_INTERVAL. Waiting a
# flat interval after such a tick leaves the scanner running nearly
# back-to-back, which pins a core and churns the session list the workbench is
# rendering. Back off to at least the tick's own duration, which holds scanning
# to half the loop's wall-clock for any pass up to MAX_SCAN_BACKOFF. Past that
# the cap wins and the duty cycle climbs again — a deliberate trade so a
# pathologically slow corpus still gets rechecked a few times an hour rather
# than falling arbitrarily far behind.
MAX_SCAN_BACKOFF = 900  # seconds
AUTO_SCORE_BATCH_SIZE = 20
_NO_MATCHING_WARMUP_SOURCE = "__clawjournal_no_matching_warmup_source__"
SCORING_DISPLAY_NAMES = {
    "claude": "Claude Code",
    "codex": "Codex",
    "hermes": "Hermes Agent",
    "openclaw": "OpenClaw",
}

_SHARE_MAX_FILE_SIZE = 500 * 1024 * 1024  # 500 MB
_SHARE_COOLDOWN_SECONDS = 10
_UPLOAD_PII_DEFAULT_WORKERS = 3
_UPLOAD_PII_MAX_WORKERS = 4
_UPLOAD_PII_DEFAULT_TIMEOUT_SECONDS = 60
_UPLOAD_PII_MIN_TIMEOUT_SECONDS = 10
_UPLOAD_PII_MAX_TIMEOUT_SECONDS = 180
_SHARE_INGEST_URL = os.environ.get("CLAWJOURNAL_INGEST_URL", "")
# The hosted research submission page. Self-hosters can override via the
# CLAWJOURNAL_SHARE_URL env var; explicitly setting it to an empty value
# disables the workbench's "Submit to ClawJournal Research" button.
_HOSTED_SHARE_URL_DEFAULT = "https://data.rayward.ai/share"
_HOSTED_SHARE_URL = os.environ.get("CLAWJOURNAL_SHARE_URL", _HOSTED_SHARE_URL_DEFAULT).strip()
_SHARE_GCS_BUCKET = os.environ.get("CLAWJOURNAL_GCS_BUCKET", "clawjournal-traces")
_SHARE_GCS_PREFIX = os.environ.get("CLAWJOURNAL_GCS_PREFIX", "clawjournal")
_SHARE_UPLOAD_TIMEOUT = 120
_share_rate_lock = threading.Lock()
_auto_upload_run_lock = threading.Lock()
_auto_upload_run_thread: threading.Thread | None = None
_HOSTED_EMAIL_SUFFIXES_DEFAULT = (".edu", ".ac.uk", ".edu.au", ".edu.cn", "ac.jp", "rayward.ai")
_hosted_capabilities_cache: tuple[str, float, dict[str, Any]] | None = None

# Sources supported in the workbench (scientist-facing subset)
WORKBENCH_SOURCES = {
    CLAUDE_SOURCE, CLAUDE_SCIENCE_SOURCE, CODEX_SOURCE, OPENCLAW_SOURCE,
    CURSOR_SOURCE, COPILOT_SOURCE, AIDER_SOURCE,
    GEMINI_SOURCE, OPENCODE_SOURCE, KIMI_SOURCE, WORKBUDDY_SOURCE,
}

# Path to the built frontend dist directory.
FRONTEND_DIST = DEFAULT_FRONTEND_DIST
_FRONTEND_BUILD_INPUT_DIRS = ("src", "public")
_FRONTEND_BUILD_INPUT_FILES = (
    "index.html",
    "package.json",
    "package-lock.json",
    "tsconfig.app.json",
    "tsconfig.json",
    "tsconfig.node.json",
    "vite.config.ts",
)


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


def _env_scoring_backend() -> str | None:
    backend = os.environ.get("CLAWJOURNAL_SCORER_BACKEND", "").strip().lower()
    return backend if backend in SUPPORTED_BACKENDS else None


def _confirmed_scoring_backend() -> str | None:
    env_backend = _env_scoring_backend()
    if env_backend:
        return env_backend
    config = load_config()
    backend = str(config.get("scorer_backend") or "").strip().lower()
    if backend in SUPPORTED_BACKENDS:
        return backend
    return None


def _suggest_scoring_backend() -> str | None:
    return detect_available_backend()


def _save_confirmed_scoring_backend(backend: str) -> None:
    config = load_config()
    config["scorer_backend"] = backend
    config["scorer_backend_confirmed_at"] = datetime.now(timezone.utc).isoformat()
    if save_config(config) is False:
        raise OSError("Scoring backend selection could not be saved safely.")


def _scoring_backend_payload(backend: str | None) -> dict[str, Any]:
    return {
        "backend": backend,
        "display_name": SCORING_DISPLAY_NAMES.get(backend or "", backend),
    }


def _fallback_chain_has_installed_backend(backend: str) -> bool:
    """True when `backend` or one of its fallback backends is runnable."""
    try:
        chain = installed_fallback_chain(resolve_backend(backend))
    except Exception:  # noqa: BLE001
        chain = [backend]
    for candidate in chain:
        if candidate not in SUPPORTED_BACKENDS:
            continue
        try:
            require_backend_command(candidate)
        except RuntimeError:
            continue
        return True
    return False


def trigger_scoring_warmup(
    scanner: "Scanner | None",
    *,
    confirm_backend: bool = False,
    requested_backend: str | None = None,
    limit: int = AUTO_SCORE_BATCH_SIZE,
) -> dict[str, Any]:
    """Start the share-readiness scoring warmup if it is allowed."""
    if scanner is None:
        return {"status": "disabled", "reason": "Background scanner is not running."}

    # A user decline must gate EVERY entry point (scanner loop, initial scan,
    # manual scan, HTTP handler) — not just the browser prompt. Once a backend
    # is confirmed (env var or prior CLI/Settings), scoring would otherwise
    # auto-start on every scan regardless of the UI choice.
    if load_config().get("scoring_warmup_declined"):
        return {"status": "declined", "reason": "Background auto-scoring is turned off in settings."}

    backend = _confirmed_scoring_backend()
    suggested = (requested_backend or "").strip().lower() or None
    if suggested is not None and suggested not in SUPPORTED_BACKENDS:
        return {"status": "disabled", "reason": f"Unsupported scoring backend: {suggested}"}

    if backend is None:
        backend = suggested or _suggest_scoring_backend()
        if backend is None:
            return {
                "status": "disabled",
                "reason": "No supported scoring backend CLI was detected.",
            }
        try:
            require_backend_command(backend)
        except RuntimeError as exc:
            if not _fallback_chain_has_installed_backend(backend):
                return {"status": "disabled", "reason": str(exc), **_scoring_backend_payload(backend)}
        if not confirm_backend and _env_scoring_backend() is None:
            return {
                "status": "needs_confirmation",
                "reason": "Confirm the detected AI scoring backend before background scoring starts.",
                **_scoring_backend_payload(backend),
            }
        _save_confirmed_scoring_backend(backend)

    try:
        require_backend_command(backend)
    except RuntimeError as exc:
        if not _fallback_chain_has_installed_backend(backend):
            return {"status": "disabled", "reason": str(exc), **_scoring_backend_payload(backend)}

    return scanner.trigger_auto_score(limit=limit, backend=backend)


def _score_redaction_settings(settings: dict[str, Any]) -> dict[str, Any] | None:
    """Return only the policy fields that affect scoring-prompt redaction."""
    scoped = {
        "custom_strings": list(settings.get("custom_strings", []) or []),
        "extra_usernames": list(settings.get("extra_usernames", []) or []),
        "blocked_domains": list(settings.get("blocked_domains", []) or []),
    }
    return scoped if any(scoped.values()) else None


def _warmup_source_filter(settings: dict[str, Any]) -> str | tuple[str, ...]:
    """Return the sources background scoring may egress.

    Warmup scoring is a background AI call, so it must honor the same confirmed
    source scope that share/skill paths use. Keep the unrestricted case on the
    failure-value corpus, because not every indexed source has a scoring rollout.
    """
    allowed = settings.get("source_filter")
    if allowed is None:
        return FAILURE_VALUE_SOURCE_SCOPE
    if isinstance(allowed, str):
        allowed_values = {allowed}
    else:
        allowed_values = {str(source) for source in allowed if source}
    scoped = tuple(source for source in FAILURE_VALUE_SOURCE_SCOPE if source in allowed_values)
    return scoped or _NO_MATCHING_WARMUP_SOURCE


def _filter_scoreable_warmup_sessions(
    conn: sqlite3.Connection,
    sessions: list[dict[str, Any]],
    *,
    excluded_projects: list[str],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Apply the same egress gates before background AI scoring."""
    if not sessions:
        return []
    blocker_ids = {
        b["session_id"]
        for b in release_gate_blockers(
            conn,
            [s["session_id"] for s in sessions],
            now=now,
        )
    }
    return [
        s for s in sessions
        if s["session_id"] not in blocker_ids
        and not session_matches_excluded_projects(s, excluded_projects)
    ]


def _query_scoreable_warmup_sessions(
    conn: sqlite3.Connection,
    *,
    limit: int,
    since: str | None,
    source_filter: str | tuple[str, ...],
    excluded_projects: list[str],
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Return latest scorable warmup rows without letting gated rows starve them."""
    if limit <= 0:
        return []

    fetch_limit = limit
    max_fetch = 10_000
    while True:
        fetched = query_unscored_sessions(
            conn,
            limit=fetch_limit,
            source=source_filter,
            since=since,
            include_stale_scored=True,
            settle_seconds=SCORE_SETTLE_SECONDS,
            now=now,
        )
        scoreable = _filter_scoreable_warmup_sessions(
            conn,
            fetched,
            excluded_projects=excluded_projects,
            now=now,
        )
        if len(scoreable) >= limit or len(fetched) < fetch_limit or fetch_limit >= max_fetch:
            return scoreable[:limit]
        fetch_limit = min(max_fetch, max(fetch_limit * 2, fetch_limit + 1))


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


def _next_scan_delay(elapsed_seconds: float) -> float:
    """Return how long the background scanner should idle after a pass.

    A pass that finishes within ``SCAN_INTERVAL`` keeps the normal cadence. A
    slower pass — large corpora routinely exceed a minute — idles for at least
    its own duration so scanning never occupies more than half the loop's
    wall-clock, bounded by ``MAX_SCAN_BACKOFF``.
    """
    if not elapsed_seconds or elapsed_seconds <= SCAN_INTERVAL:
        return SCAN_INTERVAL
    return min(elapsed_seconds, MAX_SCAN_BACKOFF)


class Scanner:
    """Periodically scans source directories and indexes new or changed sessions."""

    def __init__(self, source_filter: str | None = None):
        self.source_filter = source_filter
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_scan_mtimes: dict[str, float] = {}
        self.last_linked_count = 0
        self.last_updated_count = 0
        self.last_unchanged_count = 0
        self.last_updated_by_source: dict[str, int] = {}
        self.last_unchanged_by_source: dict[str, int] = {}
        self.last_scored_count = 0
        self._scan_lock = threading.Lock()
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

    def scan_once(
        self,
        *,
        lock_wait_seconds: float | None = SCAN_ONCE_LOCK_WAIT_SECONDS,
    ) -> dict[str, int]:
        """Run a scan pass and return ``{source: new_session_count}``.

        The return shape is retained for existing callers. Counts for traces
        whose content changed (or remained unchanged) are exposed on the
        ``last_updated_*`` and ``last_unchanged_*`` attributes. Waits
        briefly for a scan in another process to finish; if the wait
        expires, raises :class:`ScanBusyError` without touching the index.
        Normal scans must never bypass the process lock because doing so
        could disrupt a strict scan that already holds it.
        """
        with self._scan_lock:
            with _scan_process_lock(wait_seconds=lock_wait_seconds) as acquired:
                if not acquired:
                    raise ScanBusyError(
                        "Another scan is still refreshing the index; try again shortly."
                    )
                return self._scan_once_report(required_sources=None)["new_by_source"]

    def _scan_tick(self) -> dict[str, int] | None:
        """One background-loop pass; skips when another process is scanning.

        The loop reruns within a minute, so a skipped tick just catches up on
        the next one instead of contending with a CLI scan for SQLite writes.
        """
        with self._scan_lock:
            with _scan_process_lock(wait_seconds=None) as acquired:
                if not acquired:
                    logger.info(
                        "Skipping background scan: another process holds the scan lock"
                    )
                    return None
                return self._scan_once_report(required_sources=None)["new_by_source"]

    def scan_once_if_idle(self) -> dict[str, int] | None:
        """Run a normal scan only when no scan is already active.

        HTTP refresh requests use this non-blocking entry point so repeated
        desktop launches coalesce instead of running concurrent SQLite and
        findings passes on the same Scanner instance — or against a scan in
        another process.
        """
        if not self._scan_lock.acquire(blocking=False):
            return None
        try:
            with _scan_process_lock(wait_seconds=None) as acquired:
                if not acquired:
                    return None
                return self._scan_once_report(required_sources=None)["new_by_source"]
        finally:
            self._scan_lock.release()

    def scan_once_strict(
        self,
        required_sources: list[str],
        *,
        progress: Callable[[str, int, int], None] | None = None,
        on_wait: Callable[[], None] | None = None,
        lock_wait_seconds: float | None = SCAN_LOCK_WAIT_SECONDS,
    ) -> dict[str, Any]:
        """Run a fail-closed refresh for an automatic-upload source scope.

        Unlike :meth:`scan_once`, this returns a structured report and never
        turns a partial parse/findings pass into success.  Project names,
        paths, session IDs, and exception text are intentionally omitted so
        the report is safe to persist as scheduler telemetry.  ``progress``
        receives ``(source, position, total)`` per project under the same
        discipline — sources and counters only, never names or paths.
        ``on_wait`` fires once if a scan in another process holds the lock;
        if the lock is still held after ``lock_wait_seconds``, the refresh
        fails closed with a ``busy`` report instead of contending for SQLite
        writes it would lose.
        """
        required = sorted({source.strip() for source in required_sources if source.strip()})
        if not required:
            raise ValueError("required_sources must not be empty")
        unsupported = [source for source in required if source not in WORKBENCH_SOURCES]
        if unsupported:
            return {
                "ok": False,
                "required_sources": required,
                "discovered_sources": [],
                "missing_sources": unsupported,
                "new_by_source": {},
                "updated_by_source": {},
                "unchanged_by_source": {},
                "failures": [
                    {"source": source, "stage": "source", "code": "unsupported_source"}
                    for source in unsupported
                ],
            }
        with self._scan_lock:
            with _scan_process_lock(
                wait_seconds=lock_wait_seconds, on_wait=on_wait
            ) as acquired:
                if not acquired:
                    return {
                        "ok": False,
                        "busy": True,
                        "required_sources": required,
                        "discovered_sources": [],
                        "missing_sources": [],
                        "new_by_source": {},
                        "updated_by_source": {},
                        "unchanged_by_source": {},
                        "failures": [
                            {"source": "*", "stage": "lock", "code": "scanner_busy"}
                        ],
                    }
                return self._scan_once_report(
                    required_sources=set(required), progress=progress
                )

    def _scan_once_report(
        self,
        *,
        required_sources: set[str] | None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        if required_sources is None:
            return self._scan_once_report_impl(required_sources=None)
        with _strict_scan_log_guard():
            return self._scan_once_report_impl(
                required_sources=required_sources, progress=progress
            )

    def _scan_once_report_impl(
        self,
        *,
        required_sources: set[str] | None,
        progress: Callable[[str, int, int], None] | None = None,
    ) -> dict[str, Any]:
        strict = required_sources is not None
        self.last_updated_count = 0
        self.last_unchanged_count = 0
        self.last_updated_by_source = {}
        self.last_unchanged_by_source = {}
        results: dict[str, int] = {}
        discovered_sources: set[str] = set()
        failures: list[dict[str, str]] = []
        strict_raw_fingerprints: dict[str, list[Any]] = {}
        conn = open_index()
        try:
            config = load_config()
            # Ingest stores raw content; anonymization happens at egress
            # (apply_share_redactions, score_session).
            anonymizer = Anonymizer(enabled=False)

            # Drain any sessions flagged by the security-refactor migration
            # before running the normal parse/scan loop. Per-row updates so
            # a crash mid-drain leaves remaining rows for the next tick.
            try:
                drain_findings_backfill(
                    conn, config=config, safe_logging=strict
                )
            except Exception:
                if strict:
                    _log_strict_scan_failure(
                        source="*", stage="findings", code="backfill_failed"
                    )
                else:
                    logger.exception("Findings backfill failed during scan")
                failures.append(
                    {"source": "*", "stage": "findings", "code": "backfill_failed"}
                )

            try:
                projects = discover_projects(source_filter=self.source_filter)
            except Exception:
                if strict:
                    _log_strict_scan_failure(
                        source="*", stage="discovery", code="discovery_failed"
                    )
                else:
                    logger.exception("Source discovery failed during scan")
                projects = []
                failures.append(
                    {"source": "*", "stage": "discovery", "code": "discovery_failed"}
                )

            relevant_projects = []
            for project in projects:
                source = project.get("source", "")
                if source not in WORKBENCH_SOURCES:
                    continue
                if required_sources is not None and source not in required_sources:
                    continue
                if self.source_filter and source != self.source_filter:
                    continue
                relevant_projects.append(project)

            total_projects = len(relevant_projects)
            for position, project in enumerate(relevant_projects, start=1):
                source = project.get("source", "")
                discovered_sources.add(source)
                if progress is not None:
                    try:
                        progress(source, position, total_projects)
                    except Exception:
                        # A broken progress callback must not fail the
                        # fail-closed scan or leak into its failure report.
                        progress = None

                try:
                    sessions = parse_project_sessions(
                        project["dir_name"],
                        anonymizer=anonymizer,
                        include_thinking=True,
                        source=source,
                        locator=project.get("locator"),
                        strict_jsonl=strict,
                    )
                    if sessions and strict:
                        for session in sessions:
                            snapshot = session.pop(
                                "_raw_source_fingerprint", None
                            )
                            session_id = session.get("session_id")
                            if (
                                source in {"claude", "codex"}
                                and (
                                    not session_id
                                    or not isinstance(snapshot, tuple)
                                    or len(snapshot) != 5
                                )
                            ):
                                raise ValueError(
                                    "strict parser did not return a raw-source snapshot"
                                )
                            if session_id and snapshot is not None:
                                strict_raw_fingerprints[str(session_id)] = list(
                                    snapshot
                                )
                except Exception:
                    if strict:
                        _log_strict_scan_failure(
                            source=source, stage="parse", code="parse_failed"
                        )
                    else:
                        logger.exception(
                            "Error parsing project %s", project["dir_name"]
                        )
                    failures.append(
                        {"source": source, "stage": "parse", "code": "parse_failed"}
                    )
                    continue
                if not sessions:
                    continue
                try:
                    upsert_stats: dict[str, int] = {}
                    new_count = upsert_sessions(conn, sessions, stats=upsert_stats)
                    results[source] = results.get(source, 0) + new_count
                    updated_count = upsert_stats.get("updated", 0)
                    unchanged_count = upsert_stats.get("unchanged", 0)
                    self.last_updated_count += updated_count
                    self.last_unchanged_count += unchanged_count
                    self.last_updated_by_source[source] = (
                        self.last_updated_by_source.get(source, 0) + updated_count
                    )
                    self.last_unchanged_by_source[source] = (
                        self.last_unchanged_by_source.get(source, 0) + unchanged_count
                    )
                except Exception as store_error:
                    # Not a parse problem: the sessions parsed cleanly and the
                    # index write failed. A lock timeout gets its own code so
                    # contention is diagnosable as contention — but only a
                    # locked/busy OperationalError: the same exception type
                    # also covers corruption and full disks, which must not
                    # masquerade as transient contention.
                    contention = isinstance(
                        store_error, sqlite3.OperationalError
                    ) and any(
                        marker in str(store_error).lower()
                        for marker in ("locked", "busy")
                    )
                    code = "index_busy" if contention else "store_failed"
                    if strict:
                        _log_strict_scan_failure(
                            source=source, stage="store", code=code
                        )
                    else:
                        logger.exception(
                            "Error storing project %s", project["dir_name"]
                        )
                    failures.append(
                        {"source": source, "stage": "store", "code": code}
                    )
                    continue
                # Drive each freshly-upserted session through the findings
                # pipeline. Settle-threshold + revision check inside the
                # driver keep this cheap on steady state; errors per session
                # don't abort the loop.
                for session in sessions:
                    sid = session.get("session_id")
                    if not sid:
                        continue
                    try:
                        run_findings_pipeline(
                            conn,
                            sid,
                            session,
                            config=config,
                            safe_logging=strict,
                        )
                    except Exception:
                        if strict:
                            _log_strict_scan_failure(
                                source=source,
                                stage="findings",
                                code="findings_failed",
                            )
                        else:
                            logger.exception(
                                "Findings pipeline failed for %s", sid
                            )
                        failures.append(
                            {
                                "source": source,
                                "stage": "findings",
                                "code": "findings_failed",
                            }
                        )

            try:
                self.last_linked_count = link_subagent_hierarchy(conn)
            except Exception:
                if strict:
                    _log_strict_scan_failure(
                        source="*", stage="link", code="link_failed"
                    )
                else:
                    logger.exception("Subagent hierarchy linking failed during scan")
                self.last_linked_count = 0
                failures.append(
                    {"source": "*", "stage": "link", "code": "link_failed"}
                )

            required = sorted(required_sources or ())
            missing_sources = sorted(set(required) - discovered_sources)
            for source in missing_sources:
                failures.append(
                    {"source": source, "stage": "discovery", "code": "source_not_discovered"}
                )
            relevant_failures = [
                failure
                for failure in failures
                if required_sources is None
                or failure["source"] == "*"
                or failure["source"] in required_sources
            ]
            return {
                "ok": not relevant_failures,
                "required_sources": required,
                "discovered_sources": sorted(discovered_sources),
                "missing_sources": missing_sources,
                "new_by_source": results,
                "updated_by_source": dict(self.last_updated_by_source),
                "unchanged_by_source": dict(self.last_unchanged_by_source),
                "failures": relevant_failures,
                "raw_fingerprints": strict_raw_fingerprints if strict else {},
            }
        finally:
            conn.close()

    def score_unscored_once(
        self,
        *,
        limit: int = AUTO_SCORE_BATCH_SIZE,
        since: str | None = None,
        backend: str = "auto",
    ) -> int:
        """Score the latest failure-corpus traces using the selected backend."""
        if self._auto_score_disabled_reason:
            return 0
        if not self._score_lock.acquire(blocking=False):
            return 0

        from ..scoring.scoring import score_session

        try:
            conn = open_index()
            try:
                effective_settings = get_effective_share_settings(conn, load_config())
                excluded_projects = list(effective_settings.get("excluded_projects") or [])
                redaction_settings = _score_redaction_settings(effective_settings)
                # Warmup deliberately scores the latest `limit` unscored
                # failure-corpus traces ordered by start_time DESC with no
                # age cap (`since` is None for the background path). The
                # `limit` bounds cost; callers that want a rolling window
                # (CLI `--window`) pass `since` explicitly.
                #
                # `include_stale_scored` also re-selects sessions that were
                # graded mid-flight and then grew (end_time advanced past
                # ai_scored_at); `settle_seconds` defers sessions that are still
                # active so we don't grade them prematurely in the first place.
                sessions = _query_scoreable_warmup_sessions(
                    conn,
                    limit=limit,
                    since=since,
                    source_filter=_warmup_source_filter(effective_settings),
                    excluded_projects=excluded_projects,
                )
                if not sessions:
                    return 0

                # Build a fallback chain so a backend that runs out mid-batch
                # (e.g. codex out of credits / not logged in) is replaced by the
                # next installed backend instead of failing every remaining trace.
                # If nothing resolves up front, fall through to the per-session
                # handling below (which arms the permanent-failure breaker).
                try:
                    chain = installed_fallback_chain(resolve_backend(backend))
                except Exception:  # noqa: BLE001
                    chain = [backend]
                dead: set[str] = set()

                scored = 0
                for s in sessions:
                    # Honor a mid-batch opt-out: if the user turns off background
                    # scoring (Settings / decline) while this batch is running,
                    # stop before egressing the next trace. Re-read config each
                    # iteration — it's the source of truth and cheap relative to a
                    # scoring call.
                    if load_config().get("scoring_warmup_declined"):
                        logger.info("Automatic scoring stopped: background scoring turned off")
                        break
                    sid = s["session_id"]
                    while True:
                        active = next((b for b in chain if b not in dead), None)
                        if active is None:
                            self._auto_score_disabled_reason = (
                                self._auto_score_disabled_reason
                                or "All scoring backends are unavailable")
                            logger.info("Automatic scoring disabled: %s",
                                        self._auto_score_disabled_reason)
                            break
                        try:
                            score_kwargs: dict[str, Any] = {"backend": active}
                            if redaction_settings is not None:
                                score_kwargs["redaction_settings"] = redaction_settings
                            result = score_session(conn, sid, **score_kwargs)
                        except RuntimeError as exc:
                            message = str(exc)
                            backend_dead = is_backend_unavailable_error(message) or any(
                                m in message for m in PERMANENT_BACKEND_FAILURE_MARKERS)
                            if backend_dead:
                                dead.add(active)
                                if next((b for b in chain if b not in dead), None) is not None:
                                    logger.info("Scoring backend '%s' unavailable (%s); "
                                                "switching to '%s'", active, message,
                                                next(b for b in chain if b not in dead))
                                    continue  # retry this session on the next backend
                                self._auto_score_disabled_reason = message
                                logger.info("Automatic scoring disabled: %s", message)
                                break
                            logger.warning("Automatic scoring failed for %s: %s", sid, message)
                            break
                        except Exception:
                            logger.exception("Automatic scoring crashed for %s", sid)
                            break
                        else:
                            if _persist_scoring_result(conn, sid, result):
                                scored += 1
                                _maybe_create_trace_note(conn, sid)
                            break

                    if self._auto_score_disabled_reason:
                        break  # stop the batch — no usable backend remains

                return scored
            finally:
                conn.close()
        finally:
            self._score_lock.release()

    def trigger_auto_score(
        self,
        *,
        limit: int = AUTO_SCORE_BATCH_SIZE,
        since: str | None = None,
        backend: str = "auto",
    ) -> dict[str, Any]:
        """Start background scoring for the latest failure-corpus sessions if idle."""
        if self._auto_score_disabled_reason:
            return {"status": "disabled", "reason": self._auto_score_disabled_reason}
        if self._score_thread and self._score_thread.is_alive():
            return {"status": "already_running"}

        def _run() -> None:
            scored = self.score_unscored_once(limit=limit, since=since, backend=backend)
            self.last_scored_count = scored
            if scored > 0:
                logger.info("Auto-scored %d recent sessions", scored)

        self._score_thread = threading.Thread(target=_run, daemon=True)
        self._score_thread.start()
        return {"status": "started", "limit": limit, "backend": backend}

    def _run(self) -> None:
        while not self._stop_event.is_set():
            tick_started = time.monotonic()
            try:
                results = self._scan_tick()
                if results is None:
                    self._stop_event.wait(
                        _next_scan_delay(time.monotonic() - tick_started)
                    )
                    continue
                if self._stop_event.is_set():
                    break
                trigger_scoring_warmup(self)
                total_new = sum(results.values())
                if (
                    total_new > 0
                    or self.last_updated_count > 0
                    or self.last_linked_count > 0
                ):
                    logger.info(
                        "Indexed %d new sessions, updated %d existing traces, "
                        "linked %d subagent relationships: new=%s updated=%s",
                        total_new,
                        self.last_updated_count,
                        self.last_linked_count,
                        results,
                        self.last_updated_by_source,
                    )
            except Exception:
                logger.exception("Scanner error")
            elapsed = time.monotonic() - tick_started
            delay = _next_scan_delay(elapsed)
            if delay > SCAN_INTERVAL:
                logger.info(
                    "Scan pass took %.0fs (longer than the %ds interval); "
                    "waiting %.0fs before the next pass",
                    elapsed,
                    SCAN_INTERVAL,
                    delay,
                )
            self._stop_event.wait(delay)


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


def _api_session_id(path: str, *, suffix: str = "") -> str:
    """Extract and decode a session id from a `/api/sessions/<id>` route."""
    session_id = path[len("/api/sessions/"):]
    if suffix:
        session_id = session_id[:-len(suffix)]
    return unquote(session_id)


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


# Serializes benchmark generation: the deep pipeline is minutes-long and
# expensive, so only one runs at a time (the row's `generating` status is the
# durable state; this lock just rejects concurrent kicks).
_BENCHMARK_GEN_LOCK = threading.Lock()
_BENCHMARK_STALE_DAYS = 7


def _benchmark_is_stale(benchmark: dict | None, *, days: int = _BENCHMARK_STALE_DAYS) -> bool:
    """True when there's no benchmark, or the latest one is older than ``days``."""
    if not benchmark or not benchmark.get("generated_at"):
        return True
    try:
        gen = datetime.fromisoformat(str(benchmark["generated_at"]).replace("Z", "+00:00"))
    except ValueError:
        return True
    if gen.tzinfo is None:
        gen = gen.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - gen).total_seconds() > days * 86400


def _run_benchmark_generation(
    benchmark_id: str, week_slice, backend: str = "auto", model: str | None = None,
) -> None:
    """Background worker: run the deep pipeline against a pre-selected slice and
    finalize (or mark failed) the placeholder row. Always releases the gen lock.

    The same ``week_slice`` used to insert the placeholder is passed through, so
    the finalized window matches the placeholder (``finalize_benchmark`` rejects
    a window mismatch).
    """
    from ..benchmark import store
    from ..benchmark.generate import generate_benchmark

    conn = open_index()
    try:
        def progress(msg: str) -> None:
            try:
                store.update_status(conn, benchmark_id, stage=msg)
            except Exception:  # progress is best-effort
                pass

        try:
            benchmark = generate_benchmark(
                conn, week_slice=week_slice, backend=backend, model=model, progress=progress)
            store.finalize_benchmark(conn, benchmark_id, benchmark)
        except Exception as exc:
            logger.warning("benchmark generation failed for %s: %s", benchmark_id, exc)
            # Discard any partial writes from a half-applied finalize before the
            # failed-status commit, so we don't persist orphan/inconsistent rows.
            try:
                conn.rollback()
            except Exception:
                pass
            store.update_status(conn, benchmark_id, status="failed", error=str(exc))
    finally:
        conn.close()
        _BENCHMARK_GEN_LOCK.release()


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



def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _email_domain_allowed(
    email: str,
    capabilities: dict[str, Any] | None = None,
) -> bool:
    normalized = _normalize_email(email)
    if normalized.count("@") != 1:
        return False
    local_part, domain = normalized.rsplit("@", 1)
    if not local_part or not domain or any(character.isspace() for character in normalized):
        return False
    policy = (capabilities or {}).get("supported_institution_email_policy")
    suffixes = _HOSTED_EMAIL_SUFFIXES_DEFAULT
    if isinstance(policy, dict) and isinstance(policy.get("domain_suffixes"), list):
        suffixes = tuple(str(item).lower() for item in policy["domain_suffixes"] if item)
    for suffix in suffixes:
        normalized_suffix = suffix.strip().lower()
        if not normalized_suffix:
            continue
        bare_suffix = normalized_suffix[1:] if normalized_suffix.startswith(".") else normalized_suffix
        if domain == bare_suffix or domain.endswith(f".{bare_suffix}"):
            return True
    return bool(
        isinstance(policy, dict)
        and policy.get("explicit_collaborators_supported") is True
    )


def _expiry_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        raw = value.strip()
        try:
            return float(raw)
        except ValueError:
            pass
        try:
            return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


def _expiry_is_valid(value: Any, *, grace_seconds: int = 60) -> bool:
    timestamp = _expiry_timestamp(value)
    return timestamp is not None and time.time() < (timestamp - grace_seconds)


def _is_edu_email(email: str) -> bool:
    """Check if an email address matches the hosted academic-email policy."""
    return _email_domain_allowed(email)


def _missing_ingest_url_error() -> str:
    return (
        "CLI ingest upload is not configured in this build. "
        "Use the workbench Share tab's Submit step when hosted submissions are "
        "open, or use Download zip / "
        "`clawjournal bundle-export <bundle_id> --zip` for manual browser upload. "
        "Self-hosters can set CLAWJOURNAL_INGEST_URL to "
        "point at their own ingest backend."
    )


def _validated_hosted_share_url() -> tuple[str | None, str]:
    """Return a configured hosted share URL, or a user-facing disabled reason."""
    if not _HOSTED_SHARE_URL:
        return None, "Hosted submission is not configured for this install."
    try:
        parsed = urlparse(_HOSTED_SHARE_URL)
        parsed.port
    except ValueError:
        return None, "CLAWJOURNAL_SHARE_URL must be a valid HTTPS URL, or localhost."
    hostname = (parsed.hostname or "").lower()
    has_credentials = parsed.username is not None or parsed.password is not None
    is_https = parsed.scheme == "https" and bool(hostname) and not has_credentials
    is_local_dev = (
        parsed.scheme == "http"
        and hostname in {"localhost", "127.0.0.1", "::1"}
        and not has_credentials
    )
    if is_https or is_local_dev:
        return _HOSTED_SHARE_URL, "Hosted submission is configured for browser zip upload."
    return None, "CLAWJOURNAL_SHARE_URL must use HTTPS, or localhost for development."


def _hosted_api_base() -> str:
    share_url, message = _validated_hosted_share_url()
    if not share_url:
        raise RuntimeError(message)
    parsed = urlparse(share_url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _hosted_is_local_dev() -> bool:
    """True when the hosted submission API points at a local/dev host.

    Used to gate developer-only fields (e.g. ``dev_code``) so they are never
    forwarded to the browser when talking to a production deployment, even if a
    misconfigured server were to return them.
    """
    try:
        parsed = urlparse(_hosted_api_base())
    except RuntimeError:
        return False
    host = (parsed.hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or parsed.scheme == "http"


def _json_request(
    url: str,
    *,
    method: str = "GET",
    payload: dict[str, Any] | None = None,
    timeout: int = 30,
) -> dict[str, Any]:
    data = None
    headers = {"User-Agent": f"clawjournal/{__version__}"}
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    if not body:
        return {}
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("Hosted service returned an invalid response.")
    return parsed


def _fetch_hosted_share_capabilities(*, force: bool = False) -> dict[str, Any]:
    """Fetch and daemon-cache the hosted submission capability document."""
    global _hosted_capabilities_cache
    now = time.time()
    api_base = _hosted_api_base()
    if not force and _hosted_capabilities_cache is not None:
        cached_base, expires_at, cached = _hosted_capabilities_cache
        if cached_base == api_base and now < expires_at:
            return dict(cached)

    capabilities = _json_request(
        f"{api_base}/.well-known/clawjournal-share.json",
        timeout=15,
    )
    cache_seconds = capabilities.get("cache_seconds", 300)
    try:
        ttl = max(0, min(86400, int(cache_seconds)))
    except (TypeError, ValueError):
        ttl = 300
    _hosted_capabilities_cache = (api_base, now + ttl, dict(capabilities))
    return capabilities


def _recurring_offer_available(capabilities: dict[str, Any]) -> bool:
    """Return whether the live capability document offers this protocol."""

    return bool(
        capabilities.get("recurring_upload_api_version")
        == RECURRING_UPLOAD_API_VERSION
        and capabilities.get("recurring_cadence_days")
        == RECURRING_CADENCE_DAYS
        and capabilities.get("recurring_enrollment_open") is True
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


def _ensure_hosted_upload_token() -> tuple[str, str]:
    """Ensure the user has a valid, non-expired upload token.

    Returns (verified_email, upload_token).
    """
    _hosted_api_base()

    config = load_config()
    verified_email = (config.get("verified_email") or "").strip().lower()
    upload_token = (config.get("verified_email_token") or "").strip()
    expires_at = config.get("verified_email_token_expires_at", 0)

    if verified_email and upload_token:
        # Check expiry with 60-second grace period
        if _expiry_is_valid(expires_at):
            return verified_email, upload_token
        raise RuntimeError(
            "Upload token has expired. "
            "Verify your academic email again before submitting."
        )
    if verified_email:
        raise RuntimeError(
            "Email verification needs to be refreshed before sharing data. "
            "Verify your academic email again before submitting."
        )
    raise RuntimeError(
        "Email verification required before sharing data. "
        "Verify your academic email before submitting."
    )


def _ensure_self_hosted_upload_credentials() -> tuple[str, str]:
    """Ensure the legacy self-hosted ingest service has a token to send."""
    _validate_ingest_url()
    config = load_config()
    verified_email = (config.get("verified_email") or "").strip().lower()
    upload_token = (config.get("verified_email_token") or "").strip()
    expires_at = config.get("verified_email_token_expires_at", 0)
    if verified_email and upload_token:
        if _expiry_is_valid(expires_at):
            return verified_email, upload_token
        raise RuntimeError("Upload token has expired. Verify your email again before sharing.")
    if verified_email:
        raise RuntimeError("Email verification needs to be refreshed before sharing data.")
    raise RuntimeError("Email verification required before sharing data.")


def ensure_share_upload_ready() -> None:
    """Fail fast if the current environment cannot upload shared data."""
    _ensure_hosted_upload_token()


def _clear_stored_upload_token() -> None:
    """Remove any cached upload token so the next share re-verifies."""
    config = load_config()
    changed = False
    for key in ("verified_email_token", "verified_email_token_expires_at"):
        if key in config:
            del config[key]
            changed = True
    if changed:
        if save_config(config) is False:
            raise OSError("Stored upload authority could not be cleared safely.")


def _store_recurring_enrollment_grant(
    hosted_result: dict[str, Any],
    *,
    receipt_id: str,
) -> bool:
    grant = hosted_result.get("recurring_enrollment_grant")
    expires_at = hosted_result.get("recurring_enrollment_grant_expires_at")
    grant_receipt_id = hosted_result.get("recurring_enrollment_grant_receipt_id")
    if (
        not isinstance(grant, str)
        or not grant
        or not _expiry_is_valid(expires_at, grace_seconds=0)
        or grant_receipt_id != receipt_id
    ):
        return False
    try:
        config = load_config()
        config["recurring_enrollment_grant"] = grant
        config["recurring_enrollment_grant_expires_at"] = expires_at
        config["recurring_enrollment_grant_receipt_id"] = receipt_id
        # Store the issuer in the same normalized form the enrollment path
        # compares against, so an operator's casing or explicit :443 in
        # CLAWJOURNAL_SHARE_URL cannot make a valid grant permanently unusable.
        config["recurring_enrollment_grant_issuer"] = comparable_origin(
            _hosted_api_base()
        )
        if save_config(config) is False:
            raise OSError("config save returned false")
    except (OSError, RuntimeError):
        logger.warning(
            "Manual share succeeded, but its recurring-enrollment grant "
            "could not be cached; email verification remains available."
        )
        return False
    return True


def _http_error_message(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict) and data.get("error"):
        return str(data["error"])
    return body or str(exc)


class HostedServiceError(ValueError):
    """User-facing hosted API error with the originating HTTP status."""

    def __init__(self, message: str, status: int):
        super().__init__(message)
        self.status = status


def _hosted_user_status(status: int) -> int:
    return status if status in (400, 401, 403, 409, 413, 429) else 502


def request_email_verification(email: str) -> dict:
    """Send a verification request to the hosted submission service.

    Returns the response dict from the server (contains status info).
    """
    normalized = _normalize_email(email)
    try:
        capabilities = _fetch_hosted_share_capabilities()
    except (OSError, ValueError, RuntimeError, urllib.error.URLError):
        # The capabilities doc is only a best-effort source for the institution
        # email policy. If it is momentarily unreachable, fall back to the
        # built-in default suffixes rather than blocking the student from
        # requesting a code; a genuine outage still surfaces on the verify-email
        # POST below.
        capabilities = None
    if not _email_domain_allowed(normalized, capabilities):
        raise ValueError("Enter a valid academic email address.")

    try:
        result = _json_request(
            f"{_hosted_api_base()}/api/verify-email",
            method="POST",
            payload={"email": normalized},
            timeout=30,
        )
    except urllib.error.HTTPError as exc:
        raise HostedServiceError(_http_error_message(exc), exc.code) from exc

    verification_id = result.get("verification_id")
    if not isinstance(verification_id, str) or not verification_id:
        raise ValueError("Verification service did not return a verification id.")
    config = load_config()
    prior_verified = (config.get("verified_email") or "").strip().lower()
    if prior_verified and prior_verified != normalized:
        # Switching identities: drop the previously verified email's upload
        # token so a later submit cannot upload under the old identity while
        # the UI shows the new email being verified.
        for key in (
            "verified_email",
            "verified_email_token",
            "verified_email_token_expires_at",
            *RECURRING_ENROLLMENT_GRANT_CONFIG_KEYS,
        ):
            config.pop(key, None)
    config["pending_verification_id"] = verification_id
    config["pending_verification_email"] = normalized
    config["pending_verification_expires_at"] = result.get("expires_at")
    if save_config(config) is False:
        raise OSError("Email verification state could not be saved safely.")
    return result


def confirm_pending_email_verification(code: str) -> dict:
    """Confirm the pending hosted email verification and persist its token."""
    config = load_config()
    verification_id = (config.get("pending_verification_id") or "").strip()
    pending_email = (config.get("pending_verification_email") or "").strip().lower()
    if not verification_id or not pending_email:
        raise ValueError("No pending email verification. Request a new verification code first.")

    try:
        result = _json_request(
            f"{_hosted_api_base()}/api/verify-email/confirm",
            method="POST",
            payload={"verification_id": verification_id, "code": code.strip()},
            timeout=30,
        )
    except urllib.error.HTTPError as exc:
        raise HostedServiceError(_http_error_message(exc), exc.code) from exc

    upload_token = result.get("upload_token")
    if not isinstance(upload_token, str) or not upload_token:
        raise ValueError("Verification succeeded but no upload token was returned.")
    expires_at = result.get("upload_token_expires_at", 0)
    config = load_config()
    config["verified_email"] = pending_email
    config["verified_email_token"] = upload_token
    config["verified_email_token_expires_at"] = expires_at
    for key in (
        "pending_verification_id",
        "pending_verification_email",
        "pending_verification_expires_at",
    ):
        config.pop(key, None)
    if save_config(config) is False:
        raise OSError("Verified upload authority could not be saved safely.")

    return result


def confirm_email_verification(email: str, code: str) -> dict:
    """CLI-compatible wrapper around pending hosted verification."""
    normalized = _normalize_email(email)
    config = load_config()
    pending_email = (config.get("pending_verification_email") or "").strip().lower()
    if pending_email and normalized != pending_email:
        raise ValueError(
            f"Verification code was requested for {pending_email}; request a new code for {normalized}."
        )
    return confirm_pending_email_verification(code)


def hosted_upload_status() -> dict[str, Any]:
    config = load_config()
    verified_email = (config.get("verified_email") or "").strip().lower() or None
    upload_token = (config.get("verified_email_token") or "").strip()
    expires_at = config.get("verified_email_token_expires_at")
    token_valid = False
    if upload_token:
        token_valid = _expiry_is_valid(expires_at)
    return {
        "verified_email": verified_email,
        "token_valid": token_valid,
        "expires_at": expires_at,
        "pending_email": (config.get("pending_verification_email") or "").strip().lower() or None,
    }


def fetch_hosted_consent() -> dict[str, Any]:
    return _json_request(f"{_hosted_api_base()}/api/consent", timeout=30)


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


def _transport_manifest_bytes(manifest_file: Path) -> bytes:
    """Return a manifest safe to place on an egress transport.

    The persisted manifest includes ``export_path`` so the local workbench can
    reopen custom exports.  That absolute path is local control metadata, not
    bundle provenance, and can reveal the participant's home directory or
    username.  Keep the on-disk manifest intact while stripping the field from
    every uploaded/downloaded transport artifact.
    """

    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict):
        raise ValueError("Finalized share manifest must be a JSON object")
    transport_manifest = dict(manifest)
    transport_manifest.pop("export_path", None)
    return (
        json.dumps(transport_manifest, indent=2, default=str) + "\n"
    ).encode("utf-8")


def _build_share_zip(export_dir: Path) -> bytes:
    """Build the finalized share zip expected by hosted submission.

    `secret-scan.post-pii.json` (the tiered gate's proof marker) is
    required alongside the legacy `trufflehog.post-pii.json` so a zip
    can never be built from an export that skipped the combined gate.
    """
    required = [
        "sessions.jsonl",
        "manifest.json",
        "trufflehog.post-pii.json",
        "secret-scan.post-pii.json",
    ]
    missing = [name for name in required if not (export_dir / name).exists()]
    if missing:
        raise FileNotFoundError(f"Finalized share is missing {', '.join(missing)}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in (
            "sessions.jsonl",
            "manifest.json",
            "trufflehog.json",
            "trufflehog.post-pii.json",
            "secret-scan.json",
            "secret-scan.post-pii.json",
        ):
            path = export_dir / name
            if path.exists():
                payload = (
                    _transport_manifest_bytes(path)
                    if name == "manifest.json"
                    else path.read_bytes()
                )
                zf.writestr(name, payload)
    return buf.getvalue()


def _jsonl_row_count(path: Path) -> int:
    count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                count += 1
    return count


def _body_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return _body_bool(value)


def _with_legacy_bundle_alias(payload: dict[str, Any]) -> dict[str, Any]:
    """Expose bundle_id as a compatibility alias for share_id."""
    if "share_id" in payload and "bundle_id" not in payload:
        payload["bundle_id"] = payload["share_id"]
    return payload


def _bounded_int_env(name: str, default: int, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ignoring invalid %s=%r; using %d", name, raw, default)
        return default
    return max(minimum, min(maximum, value))


def _upload_pii_worker_count(session_count: int) -> int:
    if session_count <= 0:
        return 0
    requested = _bounded_int_env(
        "CLAWJOURNAL_UPLOAD_PII_WORKERS",
        _UPLOAD_PII_DEFAULT_WORKERS,
        1,
        _UPLOAD_PII_MAX_WORKERS,
    )
    return min(session_count, requested)


def _upload_pii_timeout_seconds() -> int:
    return _bounded_int_env(
        "CLAWJOURNAL_UPLOAD_PII_TIMEOUT_SECONDS",
        _UPLOAD_PII_DEFAULT_TIMEOUT_SECONDS,
        _UPLOAD_PII_MIN_TIMEOUT_SECONDS,
        _UPLOAD_PII_MAX_TIMEOUT_SECONDS,
    )


def _apply_upload_pii_redactions(
    sessions_file: Path,
    *,
    ai_pii: bool = False,
    backend: str = "auto",
    before_ai_call: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Run upload-time PII review over a JSONL export, preserving row order."""
    from ..redaction.pii import (
        apply_findings_to_session,
        review_session_pii,
        review_session_pii_hybrid,
    )

    sessions: list[dict[str, Any]] = []
    with open(sessions_file, encoding="utf-8") as f:
        for line in f:
            if line.strip():
                sessions.append(json.loads(line))

    workers = _upload_pii_worker_count(len(sessions)) if ai_pii else 0
    if ai_pii and before_ai_call is not None:
        # Auto-upload controls must be rechecked in a deterministic sequence
        # before every provider call.  Parallel sessions could otherwise all
        # pass the gate before a pause or hold applied during the first call.
        workers = 1
    timeout_seconds = _upload_pii_timeout_seconds() if ai_pii else 0
    coverage = {"full": 0, "rules_only": 0}
    if not sessions:
        return {
            "session_count": 0,
            "finding_count": 0,
            "replacement_count": 0,
            "coverage": coverage,
            "workers": 0,
            "agent_timeout_seconds": timeout_seconds,
            "ai_enabled": ai_pii,
            # No session was reviewed, so no AI backend ran. Callers read
            # pii_summary["backend"] unconditionally; omitting it raises
            # KeyError('backend') on an all-excluded (zero-row) share.
            "backend": None,
        }

    def redact_one(index: int, session: dict[str, Any]) -> tuple[int, dict[str, Any], int, int, str]:
        if ai_pii:
            findings, cov = review_session_pii_hybrid(
                session,
                ignore_llm_errors=True,
                backend=backend,
                return_coverage=True,
                timeout_seconds=timeout_seconds,
                before_agent_call=before_ai_call,
            )
        else:
            findings = review_session_pii(session)
            cov = "rules_only"
        replacement_count = 0
        if findings:
            session, replacement_count = apply_findings_to_session(session, findings)
        coverage_bucket = cov if cov in coverage else "rules_only"
        return index, session, len(findings), replacement_count, coverage_bucket

    results: list[dict[str, Any] | None] = [None] * len(sessions)
    finding_count = 0
    replacement_count = 0

    if workers <= 1:
        for index, session in enumerate(sessions):
            idx, redacted, findings, replacements, cov = redact_one(index, session)
            results[idx] = redacted
            finding_count += findings
            replacement_count += replacements
            coverage[cov] += 1
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(redact_one, index, session): index
                for index, session in enumerate(sessions)
            }
            for future in as_completed(futures):
                idx, redacted, findings, replacements, cov = future.result()
                results[idx] = redacted
                finding_count += findings
                replacement_count += replacements
                coverage[cov] += 1

    with open(sessions_file, "w", encoding="utf-8") as f:
        for session in results:
            if session is None:
                raise RuntimeError("PII redaction did not produce all session rows")
            f.write(json.dumps(session, default=str) + "\n")

    return {
        "session_count": len(sessions),
        "finding_count": finding_count,
        "replacement_count": replacement_count,
        "coverage": coverage,
        "workers": workers,
        "agent_timeout_seconds": timeout_seconds,
        "ai_enabled": ai_pii,
        "backend": backend if ai_pii else None,
    }


def finalize_share_export_for_upload(
    export_dir: Path,
    manifest: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    ai_pii: bool = False,
    ai_backend: str = "auto",
    before_ai_call: Callable[[], None] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Apply final local-only gates before an export becomes an upload zip.

    `export_share_to_disk()` performs deterministic redaction and a first
    secret-scan gate. Hosted/browser upload needs the same extra local PII
    pass that the legacy ingest path used, then a fresh gate run over the
    rewritten `sessions.jsonl` (`conn` feeds the tier policy's findings-table
    decisions).
    """
    sessions_file = export_dir / "sessions.jsonl"
    manifest_file = export_dir / "manifest.json"

    if not sessions_file.exists():
        return {"error": "Export failed — no sessions file.", "status": 500}, manifest

    if manifest.get("blocked"):
        return {
            "error": manifest.get("block_message") or "Share blocked by the secret scan",
            "block_reason": manifest.get("block_reason"),
            "blocked_sessions": manifest.get("blocked_sessions", []),
            "trufflehog_summary": manifest.get("redaction_summary", {}).get("trufflehog"),
            "secret_scan_summary": manifest.get("redaction_summary", {}).get("secret_scan"),
            "status": 422,
        }, manifest

    try:
        pii_summary = _apply_upload_pii_redactions(
            sessions_file,
            ai_pii=ai_pii,
            backend=ai_backend,
            before_ai_call=before_ai_call,
        )
        if pii_summary["finding_count"]:
            logger.info(
                "PII redaction applied: %d findings / %d replacements across %d sessions "
                "(ai=%s, workers=%d, timeout=%ss)",
                pii_summary["finding_count"],
                pii_summary["replacement_count"],
                pii_summary["session_count"],
                "on" if ai_pii else "off",
                pii_summary["workers"],
                pii_summary["agent_timeout_seconds"],
            )
    except Exception as exc:
        from ..auto_upload import ControlChanged
        from ..redaction.pii import _AgentCallGateError

        # A before_ai_call control gate (pause/disable/profile/revision/
        # generation change) fired during AI-PII review. review_session_pii_hybrid
        # UNWRAPS _AgentCallGateError to its .cause before it reaches here, so the
        # exception that actually propagates is a bare ControlChanged; catch that
        # (and the wrapper defensively) and re-propagate the original control
        # exception to the runner's dedicated handler instead of collapsing a
        # deliberate control stop into a retryable "packaging failed".
        if isinstance(exc, _AgentCallGateError):
            raise exc.cause
        if isinstance(exc, ControlChanged):
            raise
        logger.warning("PII redaction pass failed: %s", exc)
        return {
            "error": "PII redaction failed — upload aborted. Try again or report this issue.",
            "status": 500,
        }, manifest

    redaction_summary = manifest.setdefault("redaction_summary", {})
    if isinstance(redaction_summary, dict):
        redaction_summary["coverage"] = dict(pii_summary["coverage"])
        redaction_summary["pii_review"] = {
            "session_count": pii_summary["session_count"],
            "finding_count": pii_summary["finding_count"],
            "replacement_count": pii_summary["replacement_count"],
            "workers": pii_summary["workers"],
            "agent_timeout_seconds": pii_summary["agent_timeout_seconds"],
            "ai_enabled": pii_summary["ai_enabled"],
            "backend": pii_summary["backend"],
            "coverage": dict(pii_summary["coverage"]),
        }

    if ai_pii and pii_summary["coverage"].get("rules_only", 0):
        with open(manifest_file, "w", encoding="utf-8") as f:
            json.dump(manifest, f, indent=2, default=str)
        return {
            "error": (
                "AI-assisted PII review was configured but did not cover every "
                "trace. Upload remains blocked; retry when the configured backend "
                "is available."
            ),
            "block_reason": "ai-pii-incomplete",
            "coverage": dict(pii_summary["coverage"]),
            "status": 503,
        }, manifest

    try:
        from ..redaction import scan_policy
        from ..redaction import trufflehog as trufflehog_scanner
        from .share_gate import build_blocked_sessions, run_share_gate

        post_pii_gate = run_share_gate(sessions_file, manifest, conn=conn)
    except Exception as exc:
        logger.warning("Post-PII secret-scan gate failed: %s", exc)
        return {
            "error": "Post-redaction scan failed — upload aborted.",
            "detail": str(exc),
            "status": 500,
        }, manifest

    # `secret-scan.json` is the authoritative combined report shipped in
    # the zip; `secret-scan.post-pii.json` proves the final artifact
    # passed the post-PII gate. The TruffleHog sub-report keeps its
    # legacy artifact names (all-clean on every shippable bundle, since
    # verified findings are block-tier) for pre-tier consumers.
    scan_policy.write_report(export_dir / "secret-scan.json", post_pii_gate)
    scan_policy.write_report(export_dir / "secret-scan.post-pii.json", post_pii_gate)
    th_sub_report = post_pii_gate.trufflehog_report
    if th_sub_report is not None:
        trufflehog_scanner.write_report(export_dir / "trufflehog.json", th_sub_report)
        trufflehog_scanner.write_report(
            export_dir / "trufflehog.post-pii.json", th_sub_report
        )
    if isinstance(redaction_summary, dict):
        gate_summary = post_pii_gate.summary()
        redaction_summary["secret_scan"] = gate_summary
        redaction_summary["secret_scan_post_pii"] = gate_summary
        th_summary = (
            th_sub_report.summary()
            if th_sub_report is not None
            else {
                "findings": 0,
                "bypassed": post_pii_gate.bypassed,
                "binary_missing": post_pii_gate.binary_missing,
                "scan_error": post_pii_gate.scan_error,
                "engine": post_pii_gate.engine,
            }
        )
        redaction_summary["trufflehog"] = th_summary
        redaction_summary["trufflehog_post_pii"] = th_summary

    if post_pii_gate.blocking or post_pii_gate.bypassed:
        manifest["blocked"] = True
        manifest["block_reason"] = (
            post_pii_gate.block_reason
            or ("secret-scan-bypassed" if post_pii_gate.bypassed else None)
        )
        manifest["block_message"] = scan_policy.format_block_message(post_pii_gate)
        blocked_sessions = build_blocked_sessions(
            manifest, post_pii_gate.block_review_findings
        )
        if blocked_sessions:
            manifest["blocked_sessions"] = blocked_sessions
        with open(manifest_file, "w") as f:
            json.dump(manifest, f, indent=2, default=str)
        if post_pii_gate.bypassed:
            return {
                "error": (
                    "Refusing to prepare upload zip: the secret scanners were "
                    "bypassed via CLAWJOURNAL_SKIP_BETTERLEAKS/"
                    "CLAWJOURNAL_SKIP_TRUFFLEHOG. Unset the variable(s) and retry."
                ),
                "block_reason": "secret-scan-bypassed",
                "status": 422,
            }, manifest
        return {
            "error": scan_policy.format_block_message(post_pii_gate),
            "block_reason": post_pii_gate.block_reason,
            "blocked_sessions": manifest.get("blocked_sessions", []),
            "trufflehog_summary": redaction_summary.get("trufflehog")
            if isinstance(redaction_summary, dict) else None,
            "secret_scan_summary": post_pii_gate.summary(),
            "status": 422,
        }, manifest

    # Gate-time span redactions from the post-PII pass fold into the
    # manifest counters like the export-time ones do.
    if post_pii_gate.gate_redactions and isinstance(redaction_summary, dict):
        redaction_summary["total_redactions"] = (
            redaction_summary.get("total_redactions", 0)
            + post_pii_gate.gate_redactions
        )
        by_type = redaction_summary.setdefault("by_type", {})
        by_type["gate_secret_scan"] = (
            by_type.get("gate_secret_scan", 0) + post_pii_gate.gate_redactions
        )

    with open(manifest_file, "w") as f:
        json.dump(manifest, f, indent=2, default=str)
    return None, manifest


def _redaction_settings_fingerprint(settings: dict[str, Any]) -> str:
    """Stable hash of the settings that shape an exported bundle.

    A finalized export is a point-in-time artifact, but it must only be reused
    while these inputs are unchanged. Editing the allowlist, custom redaction
    strings/usernames, excluded projects, blocked domains, or confirmed source
    scope must force a rebuild so a later seal/submit/download can never ship
    content prepared under stale settings. `ai_pii` is intentionally excluded —
    it is gated separately by ``_manifest_is_finalized_for_upload``.

    Order-independent: each list is normalized (handles str and dict entries)
    and sorted, so config/policy ordering differences do not change the hash.

    MAINTENANCE: these keys must stay in sync with the redaction-affecting
    inputs produced by ``get_effective_share_settings`` (index.py). If a new
    redaction input is added there, add it here too — otherwise a change to it
    would not invalidate the cache and a stale-redacted bundle could be reused.
    """

    def _norm(values: Any) -> list[str]:
        return sorted(
            json.dumps(item, sort_keys=True, default=str) for item in (values or [])
        )

    payload = {
        "custom_strings": _norm(settings.get("custom_strings")),
        "extra_usernames": _norm(settings.get("extra_usernames")),
        "excluded_projects": _norm(settings.get("excluded_projects")),
        "blocked_domains": _norm(settings.get("blocked_domains")),
        "allowlist_entries": _norm(settings.get("allowlist_entries")),
        "source_filter": _norm(settings.get("source_filter")),
        "enabled_findings_engines": _norm(settings.get("enabled_findings_engines")),
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _manifest_is_finalized_for_upload(
    manifest: dict[str, Any],
    *,
    ai_pii: bool | None = None,
    ai_backend: str | None = None,
) -> bool:
    if manifest.get("blocked"):
        return False
    summary = manifest.get("redaction_summary")
    if not isinstance(summary, dict):
        return False
    pii_review = summary.get("pii_review")
    post_pii_scan = summary.get("trufflehog_post_pii")
    if not isinstance(pii_review, dict) or not isinstance(post_pii_scan, dict):
        return False
    if ai_pii is not None and pii_review.get("ai_enabled") is not ai_pii:
        return False
    if ai_backend is not None and pii_review.get("backend") != ai_backend:
        return False
    if pii_review.get("ai_enabled") is True:
        coverage = pii_review.get("coverage")
        if not isinstance(coverage, dict):
            return False
        if coverage.get("rules_only") != 0:
            return False
        if coverage.get("full") != pii_review.get("session_count"):
            return False
    gate = summary.get("secret_scan_post_pii")
    if isinstance(gate, dict):
        # Tier-aware finalized check: warn-tier findings (and gate-time
        # redactions) ship; block/review tiers, non-convergence, bypass,
        # and scanner failure do not.
        tier_counts = gate.get("tier_counts")
        if not isinstance(tier_counts, dict):
            return False
        return (
            tier_counts.get("block", 0) == 0
            and tier_counts.get("review", 0) == 0
            and gate.get("converged") is True
            and gate.get("bypassed") is False
            and gate.get("binary_missing") is False
            and not gate.get("scan_error")
        )
    # Legacy manifests (sealed before the tiered gate existed) fall back
    # to the all-or-nothing TruffleHog check.
    return (
        post_pii_scan.get("findings") == 0
        and post_pii_scan.get("bypassed") is False
        and post_pii_scan.get("binary_missing") is False
        and not post_pii_scan.get("scan_error")
    )


def _load_finalized_share_export(
    share_id: str,
    *,
    ai_pii: bool | None = None,
    ai_backend: str | None = None,
    expected_fingerprint: str | None = None,
) -> tuple[Path, dict[str, Any]] | None:
    # Finalized exports are point-in-time artifacts: a later config change
    # creates a new share/seal operation rather than mutating this cached zip.
    export_dir = CONFIG_DIR / "shares" / share_id
    manifest_file = export_dir / "manifest.json"
    sessions_file = export_dir / "sessions.jsonl"
    trufflehog_file = export_dir / "trufflehog.json"
    post_pii_file = export_dir / "trufflehog.post-pii.json"
    gate_post_pii_file = export_dir / "secret-scan.post-pii.json"
    if not (
        manifest_file.exists()
        and sessions_file.exists()
        and trufflehog_file.exists()
        and post_pii_file.exists()
    ):
        return None
    # Exports finalized by the tiered gate carry its post-PII report
    # both in the manifest summary and on disk. A legacy export (sealed
    # before the tiered gate existed) has neither — reject it here so it
    # rebuilds once through the current gate rather than shipping under
    # retired semantics.
    if not gate_post_pii_file.exists():
        return None
    try:
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    summary_probe = manifest.get("redaction_summary")
    if not (
        isinstance(summary_probe, dict)
        and isinstance(summary_probe.get("secret_scan_post_pii"), dict)
    ):
        return None
    if manifest.get("share_id") != share_id and manifest.get("bundle_id") != share_id:
        return None
    if not _manifest_is_finalized_for_upload(
        manifest,
        ai_pii=ai_pii,
        ai_backend=ai_backend,
    ):
        return None
    if (
        expected_fingerprint is not None
        and manifest.get("redaction_settings_fingerprint") != expected_fingerprint
    ):
        # Settings (allowlist / custom strings / excluded projects / blocked
        # domains / source scope) changed since this export was sealed. Force a
        # rebuild rather than reuse a stale-redacted artifact. Exports sealed
        # before this field existed have no fingerprint and so also rebuild once.
        return None
    return export_dir, manifest


def _prepare_share_export_for_upload(
    conn: sqlite3.Connection,
    share_id: str,
    share: dict[str, Any],
    settings: dict[str, Any],
    *,
    reuse_finalized: bool = False,
    ai_pii_review_enabled: bool | None = None,
    ai_pii_backend: str = "auto",
    before_ai_call: Callable[[], None] | None = None,
) -> tuple[Path | None, dict[str, Any], dict[str, Any] | None]:
    effective_ai_pii = (
        bool(settings.get("ai_pii_review_enabled", False))
        if ai_pii_review_enabled is None
        else ai_pii_review_enabled
    )
    session_ids = [s["session_id"] for s in share.get("sessions") or []]
    source_blockers = source_scope_blockers(conn, session_ids, settings.get("source_filter"))
    if source_blockers:
        return None, {}, {
            "error": "Share contains sessions outside the confirmed source scope",
            "blockers": source_blockers,
            "status": 409,
        }
    settings_fingerprint = _redaction_settings_fingerprint(settings)
    if reuse_finalized:
        # Pass the RAW override (not effective_ai_pii) on purpose: a finalized
        # export is a point-in-time artifact. With an explicit ai_pii override
        # the cache is only reused when its recorded ai_enabled matches; with no
        # override (None) we reuse whatever was sealed (history re-download),
        # rather than letting a later config-default flip rebuild the artifact.
        # The fingerprint additionally forces a rebuild when the redaction
        # settings changed since seal, so we never reuse stale-redacted bytes.
        # A cache miss still finalizes with effective_ai_pii below.
        cached = _load_finalized_share_export(
            share_id,
            ai_pii=ai_pii_review_enabled,
            ai_backend=(ai_pii_backend if ai_pii_review_enabled else None),
            expected_fingerprint=settings_fingerprint,
        )
        if cached is not None:
            export_dir, manifest = cached
            return export_dir, manifest, None

    # Existing participants can receive a new required scanner through a
    # ClawJournal fast-forward without re-running the installer. Repair that
    # dependency gap only when building a new artifact: a finalized cached
    # export has already passed the current gate and is reused byte-for-byte.
    # Failures stay fail-closed before any export or AI work and carry a stable
    # reason the workbench can recover from explicitly.
    from ..redaction.scanner_install import ensure_share_scanners

    scanner_setup = ensure_share_scanners()
    if not scanner_setup["ok"]:
        return None, {}, {
            "error": scanner_setup.get("error") or "Required secret scanners are not installed.",
            "block_reason": "scanner-not-installed",
            "scanner_install": scanner_setup,
            "status": 503,
        }

    export_dir, manifest = export_share_to_disk(
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
        return None, manifest, {"error": "Failed to prepare upload zip", "status": 500}
    if manifest.get("block_reason") == "revision_conflict":
        return export_dir, manifest, {
            "error": manifest.get("block_message") or "Trace revisions changed after review.",
            "block_reason": "revision_conflict",
            "blocked_sessions": manifest.get("blocked_sessions", []),
            "status": 409,
        }

    # Stamp the redaction-settings fingerprint so a later reuse can detect when
    # the allowlist / custom redactions changed and rebuild instead of shipping
    # stale bytes. Set before finalize so it is persisted to manifest.json.
    if isinstance(manifest, dict):
        manifest["redaction_settings_fingerprint"] = settings_fingerprint

    error, manifest = finalize_share_export_for_upload(
        export_dir,
        manifest,
        conn=conn,
        ai_pii=effective_ai_pii,
        ai_backend=ai_pii_backend,
        before_ai_call=before_ai_call,
    )
    if error:
        return export_dir, manifest, error
    return export_dir, manifest, None


def _final_manual_share_egress_gate(
    conn: sqlite3.Connection,
    share_id: str,
    session_ids: list[str],
    source_filter: str | list[str] | tuple[str, ...] | None,
) -> dict[str, Any] | None:
    """Re-check mutable share gates immediately before manual upload egress."""
    release_blockers = release_gate_blockers(conn, session_ids)
    if release_blockers:
        return {
            "error": "Share contains sessions that are not released",
            "blockers": release_blockers,
            "status": 409,
        }

    source_blockers = source_scope_blockers(conn, session_ids, source_filter)
    if source_blockers:
        return {
            "error": "Share contains sessions outside the confirmed source scope",
            "blockers": source_blockers,
            "status": 409,
        }

    revision_blockers = share_revision_blockers(conn, share_id)
    if revision_blockers:
        return {
            "error": "Trace revisions changed after review.",
            "block_reason": "revision_conflict",
            "blocked_sessions": revision_blockers,
            "status": 409,
        }

    # Predecessor/duplicate-revision state is mutable during the minutes-long
    # AI-PII pass between the pre-flight check and this gate (e.g. a sibling
    # share in the same revision chain lands first), so re-check it here too —
    # otherwise a stale-predecessor duplicate that #109 added this gate to
    # refuse could still cross egress.
    predecessor_blockers = share_predecessor_blockers(conn, share_id)
    if predecessor_blockers:
        return {
            "error": "Share revisions are duplicate or based on a stale predecessor",
            "blockers": predecessor_blockers,
            "status": 409,
        }
    return None


def upload_share_to_self_hosted_ingest(
    conn: sqlite3.Connection,
    share_id: str,
    *,
    force: bool = False,
    custom_strings: list[str] | None = None,
    extra_usernames: list[str] | None = None,
    excluded_projects: list[str] | None = None,
    blocked_domains: list[str] | None = None,
    allowlist_entries: list[dict[str, Any]] | None = None,
    source_filter: str | list[str] | tuple[str, ...] | None = None,
    ai_pii_review_enabled: bool = False,
) -> dict[str, Any]:
    """Upload a share to the legacy self-hosted ingest service.

    Returns a result dict with keys: ok, shared_at, session_count,
    bundle_hash, redaction_summary.  On error, returns: error (str) and
    optionally status (int).  gcs_uri is stored in DB but not returned to
    callers to avoid leaking internal infrastructure details.
    """
    # Require verified upload credentials before uploading.
    try:
        verified_email, verified_email_token = _ensure_self_hosted_upload_credentials()
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
    source_blockers = source_scope_blockers(conn, session_ids, source_filter)
    if source_blockers:
        return {
            "error": "Share contains sessions outside the confirmed source scope",
            "blockers": source_blockers,
            "status": 409,
        }
    predecessor_blockers = share_predecessor_blockers(conn, share_id)
    if predecessor_blockers:
        return {
            "error": "Share revisions are duplicate or based on a stale predecessor",
            "blockers": predecessor_blockers,
            "status": 409,
        }

    # Reuse the immutable artifact the user reviewed whenever one exists.
    # Re-exporting live blobs here could silently include appended content that
    # was never part of the bundle review. On a cache miss the export path
    # verifies the create-time revision snapshot before reading current blobs.
    export_dir, manifest, error = _prepare_share_export_for_upload(
        conn,
        share_id,
        share,
        {
            "custom_strings": custom_strings or [],
            "extra_usernames": extra_usernames or [],
            "excluded_projects": excluded_projects or [],
            "blocked_domains": blocked_domains or [],
            "allowlist_entries": allowlist_entries or [],
            "source_filter": source_filter,
            "ai_pii_review_enabled": ai_pii_review_enabled,
        },
        reuse_finalized=True,
        ai_pii_review_enabled=ai_pii_review_enabled,
    )
    if error:
        return error
    if export_dir is None:
        return {"error": "Export failed.", "status": 500}

    sessions_file = export_dir / "sessions.jsonl"
    manifest_file = export_dir / "manifest.json"

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
        files["manifest"] = (
            "manifest.json",
            _transport_manifest_bytes(manifest_file),
            "application/json",
        )

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

    final_gate_error = _final_manual_share_egress_gate(
        conn, share_id, session_ids, source_filter
    )
    if final_gate_error:
        return final_gate_error

    try:
        with urllib.request.urlopen(req, timeout=_SHARE_UPLOAD_TIMEOUT) as resp:
            upload_result = json.loads(resp.read())
        gcs_uri_from_server = upload_result.get("gcs_uri", "")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        error_data: dict[str, Any] = {}
        try:
            parsed_error = json.loads(error_body)
            if isinstance(parsed_error, dict):
                error_data = parsed_error
            error_msg = error_data.get("error", error_body)
        except json.JSONDecodeError:
            error_msg = error_body
        confirmed_hash = (
            error_data.get("bundle_hash")
            or error_data.get("existing_bundle_hash")
        )
        if (
            exc.code == 409
            and error_data.get("idempotent") is True
            and confirmed_hash == bundle_hash
        ):
            # The service explicitly proved that the same immutable bundle is
            # already stored. This is a safe retry after an ambiguous client
            # timeout, not a stale-revision conflict.
            gcs_uri_from_server = str(error_data.get("gcs_uri") or "")
        elif exc.code in (400, 401, 403, 409, 429):
            if exc.code in (401, 403):
                _clear_stored_upload_token()
            return {"error": error_msg, "status": exc.code}
        else:
            return {"error": error_msg, "status": 502}
    except (urllib.error.URLError, TimeoutError, OSError):
        return {"error": "Could not reach upload service. Please try again.", "status": 502}

    # Count sessions
    session_count = _jsonl_row_count(sessions_file)

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


def _hosted_error_message(exc: urllib.error.HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace")
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        data = None
    if isinstance(data, dict):
        if data.get("error"):
            return str(data["error"])
        detail = data.get("detail")
        if isinstance(detail, str):
            return detail
        if isinstance(detail, list) and detail:
            messages = [
                str(item.get("msg"))
                for item in detail
                if isinstance(item, dict) and item.get("msg")
            ]
            if messages:
                return "; ".join(messages)
    return body or "Hosted submission failed."


def _hosted_http_error_result(exc: urllib.error.HTTPError) -> dict[str, Any]:
    message = _hosted_error_message(exc)
    if exc.code in (401, 403):
        _clear_stored_upload_token()
    if exc.code in (400, 401, 403, 409, 413, 429):
        return {"error": message, "status": exc.code}
    return {"error": message, "status": 502}


def submit_share_to_hosted(
    conn: sqlite3.Connection,
    share_id: str,
    *,
    accept_terms: bool,
    ownership_certification: bool,
    consent_version: str,
    retention_policy_version: str,
    settings: dict[str, Any],
    ai_pii_review_enabled: bool | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Submit a finalized share zip to the hosted research API."""
    # The HTTP handler already checks for missing keys; these checks keep
    # in-process callers from submitting without the exact displayed terms.
    if not accept_terms or not ownership_certification:
        return {
            "error": "You must accept the terms and certify ownership before submitting.",
            "status": 400,
        }
    if not consent_version or not retention_policy_version:
        return {"error": "Consent and retention versions are required.", "status": 400}

    share = get_share(conn, share_id)
    if share is None:
        return {"error": "Share not found", "status": 404}

    # `force` is only meaningful when the share has already been submitted;
    # for hosted research it surfaces a clearer "cannot overwrite" message.
    # On a fresh share, `force` is ignored so defensive clients can pass it
    # without failing the submission.
    hosted_receipt_id = share.get("hosted_receipt_id")
    prior_shared_at = share.get("shared_at")
    if hosted_receipt_id:
        return {
            "error": (
                "Hosted submissions cannot be overwritten. Create a new share to submit again."
                if force
                else "Share already submitted"
            ),
            "receipt_id": hosted_receipt_id,
            "hosted_status": share.get("hosted_status"),
            "shared_at": prior_shared_at,
            "status": 409,
        }
    if prior_shared_at:
        # Legacy self-hosted ingest upload; hosted research won't accept a
        # re-submit. Differentiating the message lets the user know why.
        return {
            "error": (
                "This share was uploaded via self-hosted ingest. "
                "Create a new share to submit it to hosted research."
            ),
            "shared_at": prior_shared_at,
            "status": 409,
        }

    from .index import release_gate_blockers
    session_ids = [s["session_id"] for s in share.get("sessions") or []]
    blockers = release_gate_blockers(conn, session_ids)
    if blockers:
        return {
            "error": "Share contains sessions that are not released",
            "blockers": blockers,
            "status": 409,
        }
    predecessor_blockers = share_predecessor_blockers(conn, share_id)
    if predecessor_blockers:
        return {
            "error": "Share revisions are duplicate or based on a stale predecessor",
            "blockers": predecessor_blockers,
            "status": 409,
        }

    try:
        _verified_email, upload_token = _ensure_hosted_upload_token()
    except RuntimeError as exc:
        return {"error": str(exc), "status": 403}

    try:
        capabilities = _fetch_hosted_share_capabilities()
    except (OSError, ValueError, RuntimeError, urllib.error.URLError) as exc:
        return {"error": f"Could not reach hosted submission service: {exc}", "status": 502}
    if capabilities.get("submissions_open") is False:
        return {
            "error": "Hosted submissions are currently closed.",
            "support_contact": capabilities.get("contact_email"),
            "status": 403,
        }

    export_dir, manifest, error = _prepare_share_export_for_upload(
        conn,
        share_id,
        share,
        settings,
        reuse_finalized=True,
        ai_pii_review_enabled=ai_pii_review_enabled,
    )
    if error:
        return error
    if export_dir is None:
        return {"error": "Failed to prepare upload zip", "status": 500}

    try:
        zip_bytes = _build_share_zip(export_dir)
    except OSError as exc:
        return {"error": f"Failed to build upload zip: {exc}", "status": 500}

    max_bundle_size = capabilities.get("maximum_bundle_size", 52_428_800)
    try:
        max_bundle_size_int = int(max_bundle_size)
    except (TypeError, ValueError):
        max_bundle_size_int = 52_428_800
    if len(zip_bytes) > max_bundle_size_int:
        return {
            "error": (
                f"Upload zip is {len(zip_bytes) / (1024 * 1024):.1f} MB, "
                f"which exceeds the hosted limit of {max_bundle_size_int / (1024 * 1024):.1f} MB."
            ),
            "status": 413,
        }

    sessions_file = export_dir / "sessions.jsonl"
    sha = hashlib.sha256()
    with open(sessions_file, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha.update(chunk)
    bundle_hash = sha.hexdigest()

    upload_body, content_type = _build_multipart_body(
        fields={
            "upload_token": upload_token,
            "consent_version": consent_version,
            "retention_policy_version": retention_policy_version,
            "accept_terms": "true" if accept_terms else "false",
            "ownership_certification": "true" if ownership_certification else "false",
        },
        files={
            "bundle": (
                f"clawjournal-share-{share_id[:8]}.zip",
                zip_bytes,
                "application/zip",
            ),
        },
    )
    req = urllib.request.Request(
        f"{_hosted_api_base()}/api/submissions",
        data=upload_body,
        headers={
            "Content-Type": content_type,
            "User-Agent": f"clawjournal/{__version__}",
        },
        method="POST",
    )

    ambiguous_timeout_error = {
        "error": (
            "Your bundle was uploaded but the server did not confirm before the "
            "connection timed out. It may already have been received — check for a "
            "confirmation email before retrying, since re-submitting could create a "
            "duplicate."
        ),
        "ambiguous": True,
        "status": 504,
    }
    final_gate_error = _final_manual_share_egress_gate(
        conn, share_id, session_ids, settings.get("source_filter")
    )
    if final_gate_error:
        return final_gate_error

    try:
        with urllib.request.urlopen(req, timeout=_SHARE_UPLOAD_TIMEOUT) as resp:
            hosted_result = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return _hosted_http_error_result(exc)
    except TimeoutError:
        # The bundle bytes were already sent; a timeout means we never learned
        # whether the server accepted it. Do NOT clear the token or mark the
        # share shared, and warn against a blind retry that could duplicate a
        # submission that actually landed.
        return dict(ambiguous_timeout_error)
    except (urllib.error.URLError, OSError) as exc:
        # A URLError may wrap a timeout raised while writing the request body.
        if isinstance(getattr(exc, "reason", None), TimeoutError):
            return dict(ambiguous_timeout_error)
        # Connection refused / DNS failure: the request never reached the
        # server, so an immediate retry is safe.
        return {"error": "Could not reach hosted submission service. Please try again.", "status": 502}

    receipt_id = hosted_result.get("receipt_id")
    if not isinstance(receipt_id, str) or not receipt_id:
        return {"error": "Hosted submission succeeded but no receipt was returned.", "status": 502}

    hosted_status = hosted_result.get("status")
    hosted_submission_url = hosted_result.get("submission_url")
    shared_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE shares SET status = 'shared', shared_at = ?, bundle_hash = ?, "
        "hosted_receipt_id = ?, hosted_status = ?, hosted_submission_url = ? "
        "WHERE share_id = ?",
        (
            shared_at,
            bundle_hash,
            receipt_id,
            str(hosted_status) if hosted_status is not None else None,
            str(hosted_submission_url) if hosted_submission_url is not None else None,
            share_id,
        ),
    )
    conn.commit()

    _store_recurring_enrollment_grant(hosted_result, receipt_id=receipt_id)
    _clear_stored_upload_token()
    redaction_summary = manifest.get("redaction_summary", {}) if manifest else {}
    return {
        "ok": True,
        "receipt_id": receipt_id,
        "hosted_status": hosted_status,
        "hosted_submission_url": hosted_submission_url,
        "shared_at": shared_at,
        "session_count": _jsonl_row_count(sessions_file),
        "bundle_hash": bundle_hash,
        "zip_size_bytes": len(zip_bytes),
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

    def handle_one_request(self) -> None:
        # In-flight/mutation accounting for the update self-restart monitor:
        # a restart only happens when nothing is being served and no
        # mutating request landed recently.
        if not _note_request_start():
            # The update watcher has atomically frozen admission before
            # stopping the listening loops.  A connection accepted in that
            # narrow hand-off window must not begin work in the old process.
            self.close_connection = True
            return
        try:
            super().handle_one_request()
        finally:
            _note_request_end(getattr(self, "command", None))

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

        # The launcher needs to identify this daemon before it is safe to send
        # the bearer token. Keep the challenge response outside `/api/*` so
        # the rule that every API endpoint requires bearer auth stays intact.
        if path == "/.well-known/clawjournal":
            challenge = params.get("challenge", [""])[0]
            if len(challenge) != 32 or any(
                character not in "0123456789abcdefABCDEF" for character in challenge
            ):
                _json_response(self, {"error": "invalid challenge"}, status=400)
                return
            try:
                from pathlib import Path as _Path
                from ..paths import api_health_proof, ensure_api_token
                from .index import INDEX_DB as _INDEX_DB

                token = ensure_api_token(_Path(str(_INDEX_DB)).parent)
            except Exception:
                logger.exception("Could not resolve api_token for health check")
                _json_response(self, {"error": "health check unavailable"}, status=500)
                return
            _json_response(
                self,
                {
                    "ok": True,
                    "service": "clawjournal",
                    "proof": api_health_proof(token, challenge),
                },
            )
        # API routes
        elif path == "/api/sessions":
            self._handle_list_sessions(params)
        elif path.startswith("/api/sessions/") and path.endswith("/redaction-report"):
            session_id = _api_session_id(path, suffix="/redaction-report")
            ai_pii = params.get("ai_pii", [""])[0] == "1"
            self._handle_redaction_report(session_id, ai_pii=ai_pii)
        elif path.startswith("/api/sessions/") and path.endswith("/findings"):
            session_id = _api_session_id(path, suffix="/findings")
            self._handle_list_session_findings(session_id, params)
        elif path.startswith("/api/sessions/") and path.endswith("/hold-history"):
            session_id = _api_session_id(path, suffix="/hold-history")
            self._handle_hold_history(session_id)
        elif path.startswith("/api/sessions/") and path.endswith("/redacted"):
            session_id = _api_session_id(path, suffix="/redacted")
            self._handle_session_redacted(session_id)
        elif path.startswith("/api/sessions/"):
            session_id = _api_session_id(path)
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
        elif path == "/api/share-destination":
            self._handle_share_destination()
        elif path == "/api/share/consent":
            self._handle_share_consent()
        elif path == "/api/share/upload-status":
            self._handle_share_upload_status()
        elif path == "/api/auto-upload/status":
            self._handle_auto_upload_status()
        elif path == "/api/auto-upload/preview":
            self._handle_auto_upload_preview(refresh=False)
        elif path == "/api/scoring/backend":
            self._handle_scoring_backend()
        elif path == "/api/bundles":
            self._handle_list_shares()
        elif path.startswith("/api/bundles/") and path.endswith("/preview"):
            share_id = path[len("/api/bundles/"):-len("/preview")]
            self._handle_preview_share(share_id)
        elif path.startswith("/api/bundles/") and path.endswith("/download"):
            share_id = path[len("/api/bundles/"):-len("/download")]
            self._handle_download_share(share_id, params)
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
            self._handle_download_share(share_id, params)
        elif path.startswith("/api/shares/"):
            share_id = path[len("/api/shares/"):]
            self._handle_get_share(share_id)
        elif path == "/api/policies":
            self._handle_list_policies()
        elif path == "/api/allowlist":
            self._handle_list_allowlist()
        elif path == "/api/findings/allowlist":
            self._handle_list_findings_allowlist()
        elif path == "/api/config":
            self._handle_get_config()
        elif path == "/api/features":
            self._handle_features()
        elif path == "/api/benchmarks":
            self._handle_benchmarks_list()
        elif path == "/api/benchmarks/latest":
            self._handle_benchmark_latest()
        elif path == "/api/benchmarks/trend":
            self._handle_benchmark_trend()
        elif path.startswith("/api/benchmarks/") and path.endswith("/status"):
            self._handle_benchmark_status(path[len("/api/benchmarks/"):-len("/status")])
        elif path.startswith("/api/benchmarks/"):
            self._handle_benchmark_get(path[len("/api/benchmarks/"):])
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
            session_id = _api_session_id(path, suffix="/score")
            self._handle_score_session(session_id)
        elif path.startswith("/api/sessions/") and path.endswith("/scan"):
            session_id = _api_session_id(path, suffix="/scan")
            self._handle_force_scan_session(session_id)
        elif path.startswith("/api/sessions/"):
            session_id = _api_session_id(path)
            self._handle_update_session(session_id)
        elif path == "/api/quick-share":
            self._handle_quick_share()
        elif path == "/api/share/verify-email":
            self._handle_share_verify_email()
        elif path == "/api/share/verify-confirm":
            self._handle_share_verify_confirm()
        elif path == "/api/share/scanners/install":
            self._handle_install_share_scanners()
        elif path == "/api/auto-upload/preview":
            self._handle_auto_upload_preview(refresh=True)
        elif path == "/api/auto-upload/enable":
            self._handle_auto_upload_enable()
        elif path == "/api/auto-upload/run":
            self._handle_auto_upload_run()
        elif path == "/api/auto-upload/pause":
            self._handle_auto_upload_pause()
        elif path == "/api/auto-upload/resume":
            self._handle_auto_upload_resume()
        elif path == "/api/auto-upload/disable":
            self._handle_auto_upload_disable()
        elif path == "/api/bundles":
            self._handle_create_share()
        elif path.startswith("/api/bundles/") and path.endswith("/export"):
            share_id = path[len("/api/bundles/"):-len("/export")]
            self._handle_export_share(share_id)
        elif path.startswith("/api/bundles/") and path.endswith("/seal"):
            share_id = path[len("/api/bundles/"):-len("/seal")]
            self._handle_seal_share(share_id)
        elif path.startswith("/api/bundles/") and path.endswith("/share"):
            share_id = path[len("/api/bundles/"):-len("/share")]
            self._handle_upload_share(share_id)
        elif path == "/api/shares":
            self._handle_create_share()
        elif path.startswith("/api/shares/") and path.endswith("/export"):
            share_id = path[len("/api/shares/"):-len("/export")]
            self._handle_export_share(share_id)
        elif path.startswith("/api/shares/") and path.endswith("/seal"):
            share_id = path[len("/api/shares/"):-len("/seal")]
            self._handle_seal_share(share_id)
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
        elif path == "/api/scoring/warmup":
            self._handle_scoring_warmup()
        elif path == "/api/config":
            self._handle_update_config()
        elif path == "/api/desktop/opened":
            self._handle_desktop_opened()
        elif path == "/api/scan":
            force = parse_qs(parsed.query).get("force", [""])[0] in ("1", "true")
            self._handle_trigger_scan(force=force)
        elif path == "/api/benchmarks/generate":
            self._handle_benchmark_generate()
        elif path.startswith("/api/benchmarks/") and path.endswith("/export"):
            self._handle_benchmark_export(path[len("/api/benchmarks/"):-len("/export")])
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
        status_values = [
            value.strip()
            for raw in params.get("status", [])
            for value in raw.split(",")
            if value.strip()
        ]
        status_filter: str | list[str] | None
        if len(status_values) == 1:
            status_filter = status_values[0]
        elif status_values:
            status_filter = status_values
        else:
            status_filter = None
        conn = open_index()
        try:
            result = query_sessions(
                conn,
                status=status_filter,
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
            if "ai_failure_value_score" in body or "ai_failure_evidence" in body:
                detail = get_session_detail(conn, session_id)
                if detail is None:
                    _json_response(self, {"error": "Session not found"}, 404)
                    return

                failure_value = body.get("ai_failure_value_score")
                if failure_value is not None:
                    try:
                        failure_value = int(failure_value)
                    except (TypeError, ValueError):
                        _json_response(self, {"error": "Invalid failure value"}, 400)
                        return
                    body["ai_failure_value_score"] = failure_value

                provided_evidence = normalize_failure_evidence(
                    body.get("ai_failure_evidence")
                )
                raw_detail = body.get(
                    "ai_scoring_detail",
                    detail.get("ai_scoring_detail"),
                )
                if requires_failure_evidence(failure_value):
                    existing_evidence = failure_evidence_from_detail(raw_detail)
                    if not provided_evidence and not existing_evidence:
                        _json_response(
                            self,
                            {"error": "Failure-value 4-5 overrides require evidence."},
                            400,
                        )
                        return
                if provided_evidence:
                    body["ai_scoring_detail"] = merge_failure_evidence(
                        raw_detail,
                        provided_evidence,
                    )

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
            # Manual scoring is AI egress too: scrub configured redaction
            # strings/usernames/blocked-domains before the prompt leaves the
            # machine, matching the background warmup path. Unlike warmup we do
            # not gate on hold/embargo/excluded-project here — scoring a
            # specific session is an explicit user request for that session.
            redaction_settings = _score_redaction_settings(
                get_effective_share_settings(conn, load_config())
            )
            score_kwargs: dict[str, Any] = {"model": model, "backend": backend}
            if redaction_settings is not None:
                score_kwargs["redaction_settings"] = redaction_settings
            try:
                result = score_session(conn, session_id, **score_kwargs)
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

    # ------------------------------------------------------------------
    # Personalized benchmark API
    # ------------------------------------------------------------------
    def _handle_benchmarks_list(self) -> None:
        from ..benchmark import store
        conn = open_index()
        try:
            _json_response(self, {"benchmarks": store.list_benchmarks(conn)})
        finally:
            conn.close()

    def _handle_benchmark_latest(self) -> None:
        from ..benchmark import store
        conn = open_index()
        try:
            latest = store.get_latest_benchmark(conn)
            _json_response(self, {"benchmark": latest, "stale": _benchmark_is_stale(latest)})
        finally:
            conn.close()

    def _handle_benchmark_trend(self) -> None:
        from ..benchmark import store
        conn = open_index()
        try:
            _json_response(self, store.get_theme_trend(conn))
        finally:
            conn.close()

    def _handle_benchmark_get(self, benchmark_id: str) -> None:
        from ..benchmark import store
        conn = open_index()
        try:
            got = store.get_benchmark(conn, benchmark_id)
            if got is None:
                _json_response(self, {"error": "benchmark not found"}, 404)
                return
            _json_response(self, got)
        finally:
            conn.close()

    def _handle_benchmark_status(self, benchmark_id: str) -> None:
        from ..benchmark import store
        conn = open_index()
        try:
            got = store.get_benchmark(conn, benchmark_id)
            if got is None:
                _json_response(self, {"error": "benchmark not found"}, 404)
                return
            _json_response(self, {
                "benchmark_id": benchmark_id,
                "status": got.get("status"),
                "stage": got.get("stage"),
                "error": got.get("error"),
            })
        finally:
            conn.close()

    def _handle_benchmark_generate(self) -> None:
        from ..benchmark import store
        from ..benchmark.select import select_week_failures

        body = _read_body(self) or {}
        # window/cap/backend/model now come from the UI controls — clamp the
        # numerics to sane bounds and default backend to auto-detect.
        window = max(1, min(int(body.get("window_days", 7) or 7), 90))
        cap = max(1, min(int(body.get("cap", 15) or 15), 50))
        backend = str(body.get("backend") or "auto").strip().lower() or "auto"
        model = (str(body.get("model")).strip() or None) if body.get("model") else None
        if backend != "auto" and backend not in SUPPORTED_BACKENDS:
            _json_response(self, {"error": f"Unsupported benchmark backend: {backend}"}, 400)
            return

        if not _BENCHMARK_GEN_LOCK.acquire(blocking=False):
            _json_response(self, {"status": "busy",
                                  "error": "a benchmark generation is already running"}, 409)
            return
        released = False
        try:
            conn = open_index()
            try:
                sl = select_week_failures(conn, window_days=window, cap=cap)
                if not sl.candidates:
                    _json_response(
                        self, {"error": "no failure-signal sessions in the selected window"}, 400)
                    return
                bid = store.insert_generating(
                    conn, window_start=sl.window_start, window_end=sl.window_end)
            finally:
                conn.close()
            try:
                threading.Thread(
                    target=_run_benchmark_generation, args=(bid, sl, backend, model),
                    daemon=True).start()
            except Exception as exc:
                # Thread didn't start, so the worker won't run (and won't release
                # the lock or finalize the row) — mark it failed here and bail.
                conn2 = open_index()
                try:
                    store.update_status(conn2, bid, status="failed",
                                        error=f"failed to start worker: {exc}")
                finally:
                    conn2.close()
                _json_response(self, {"error": "could not start generation"}, 500)
                return
            released = True  # the worker owns the lock now
            _json_response(self, {"status": "generating", "benchmark_id": bid}, 202)
        finally:
            if not released:
                _BENCHMARK_GEN_LOCK.release()

    def _handle_benchmark_export(self, benchmark_id: str) -> None:
        from ..benchmark import render, store
        from ..benchmark import schema as bm
        from ..benchmark.render import EXPORT_KINDS

        body = _read_body(self) or {}
        kind = body.get("kind", "authoring_md")
        if kind not in EXPORT_KINDS:
            _json_response(self, {"error": f"unknown export kind {kind!r}",
                                  "kinds": list(EXPORT_KINDS)}, 400)
            return
        conn = open_index()
        try:
            got = store.get_benchmark(conn, benchmark_id)
            if got is None:
                _json_response(self, {"error": "benchmark not found"}, 404)
                return
            if got.get("status") != "ready":
                _json_response(self, {"error": f"benchmark is {got.get('status')!r}, not ready to export"}, 409)
                return
            content = render.render(got, kind)
            pii_hits = len(bm.find_pii(content))
            ext = "json" if kind.endswith("json") else "md"
            out_dir = CONFIG_DIR / "benchmark_exports"
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{benchmark_id}-{kind}.{ext}"
            out_path.write_text(content, encoding="utf-8")
            summary = {"kind": kind, "pii_scan_hits": pii_hits, "deterministic_redaction": "deferred"}
            store.record_export(
                conn, benchmark_id, kind=kind, path=str(out_path), redaction_summary=summary)
            _json_response(self, {
                "benchmark_id": benchmark_id, "kind": kind, "path": str(out_path),
                "pii_scan_hits": pii_hits, "content": content,
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
        min_failure_value = _int_param("min_failure_value", min_quality, 1, 5)

        conn = open_index()
        try:
            data = get_highlights(
                conn,
                days=days,
                top_n=top_n,
                min_quality=min_quality,
                min_failure_value=min_failure_value,
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
            settings = get_effective_share_settings(conn, load_config())
            excluded_projects = settings["excluded_projects"]
            projects = []
            for row in rows:
                project = dict(row)
                project["excluded"] = session_matches_excluded_projects(
                    project,
                    excluded_projects,
                )
                projects.append(project)
            _json_response(self, projects)
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

        When *ai_pii* is True, also runs agent-based PII detection and
        applies the findings on top of the deterministic share redaction.
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

            # Agent-based PII detection is opt-in for the preview. The
            # deterministic findings-backed pass above always runs.
            ai_pii_count = 0
            ai_pii_findings: list[dict] = []
            ai_coverage = "disabled"
            if ai_pii:
                ai_coverage = "rules_only"
                try:
                    from ..redaction.pii import review_session_pii_with_agent, apply_findings_to_session
                    # Use AI-only detection (skip redundant rule-based PII scan
                    # since redact_session() already handles regex patterns)
                    findings = review_session_pii_with_agent(
                        detail,
                        ignore_errors=False,
                        backend="auto",
                        timeout_seconds=_upload_pii_timeout_seconds(),
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

    def _handle_features(self) -> None:
        """Return UI feature flags read fresh from config (no DB, no restart needed
        to pick up a toggle — the browser just reloads)."""
        from ..config import load_config
        config = load_config()
        _json_response(self, {
            "benchmark_tab_enabled": bool(config.get("benchmark_tab_enabled", True)),
            "scoring_warmup_declined": bool(config.get("scoring_warmup_declined", False)),
        })

    def _handle_get_config(self) -> None:
        """Return the UI-editable config subset plus the valid option lists.

        Only non-sensitive knobs are exposed — never tokens, attestations, or
        verification state.
        """
        from ..config import load_config
        from ..cli import EXPLICIT_SOURCE_CHOICES
        from ..scoring.scoring import SUPPORTED_SCORING_BACKENDS
        config = load_config()
        # 'both' is hidden from the manual picker ('claude' + 'codex' cover it
        # more explicitly), but the Auto Upload guided scope setup writes it as
        # the recurring claude+codex pair — so surface it whenever it is the
        # currently-stored value, keeping the select on the real value rather
        # than a misleading "Select a source…" placeholder.
        stored_source = config.get("source")
        source_choices = sorted(
            c for c in EXPLICIT_SOURCE_CHOICES if c != "both" or stored_source == "both"
        )
        _json_response(self, {
            "source": config.get("source"),
            "projects_confirmed": bool(config.get("projects_confirmed", False)),
            "ai_pii_review_enabled": bool(config.get("ai_pii_review_enabled", False)),
            "scorer_backend": config.get("scorer_backend"),
            "scorer_backend_confirmed_at": config.get("scorer_backend_confirmed_at"),
            "benchmark_tab_enabled": bool(config.get("benchmark_tab_enabled", True)),
            "scoring_warmup_declined": bool(config.get("scoring_warmup_declined", False)),
            "source_choices": source_choices,
            "scorer_backend_choices": [b for b in SUPPORTED_SCORING_BACKENDS if b != "auto"],
            "scorer_backend_detected": _suggest_scoring_backend(),
        })

    def _handle_update_config(self) -> None:
        """Write a whitelisted config subset via cli.configure() so all the
        append/merge/confirm invariants stay identical to the CLI path."""
        from ..cli import configure, EXPLICIT_SOURCE_CHOICES
        from ..scoring.scoring import SUPPORTED_SCORING_BACKENDS
        body = _read_body(self) or {}

        kwargs: dict[str, Any] = {}
        if body.get("source") is not None:
            src = str(body["source"]).strip().lower()
            if src not in EXPLICIT_SOURCE_CHOICES:
                _json_response(self, {"error": f"Invalid source: {src}"}, 400)
                return
            kwargs["source"] = src
        if body.get("scorer_backend") is not None:
            sb = str(body["scorer_backend"]).strip().lower()
            if sb != "none" and sb not in SUPPORTED_SCORING_BACKENDS:
                _json_response(self, {"error": f"Invalid scorer backend: {sb}"}, 400)
                return
            kwargs["scorer_backend"] = sb
        if body.get("confirm_projects"):
            kwargs["confirm_projects"] = True
        if body.get("ai_pii_review_enabled") is not None:
            kwargs["ai_pii_review"] = bool(body["ai_pii_review_enabled"])
        if body.get("benchmark_tab_enabled") is not None:
            kwargs["benchmark_tab_enabled"] = bool(body["benchmark_tab_enabled"])
        if body.get("scoring_warmup_declined") is not None:
            kwargs["scoring_warmup_declined"] = bool(body["scoring_warmup_declined"])

        if not kwargs:
            _json_response(self, {"error": "No recognized config fields"}, 400)
            return
        try:
            configure(quiet=True, **kwargs)
        except Exception as exc:
            _json_response(self, {"error": str(exc)}, 500)
            return
        self._handle_get_config()  # echo back the new state

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
        if save_config(config) is False:
            _json_response(
                self,
                {"error": "Allowlist persistence could not be confirmed; review automatic-upload status."},
                500,
            )
            return
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
        if save_config(config) is False:
            _json_response(
                self,
                {"error": "Allowlist persistence could not be confirmed; review automatic-upload status."},
                500,
            )
            return
        _json_response(self, {"ok": True})

    def _handle_scoring_backend(self) -> None:
        """Return the default AI scoring backend detected for this daemon."""
        backend = _confirmed_scoring_backend()
        suggested = None if backend else _suggest_scoring_backend()
        _json_response(self, {
            **_scoring_backend_payload(backend or suggested),
            "confirmed": backend is not None,
            "needs_confirmation": backend is None and suggested is not None,
        })

    def _handle_scoring_warmup(self) -> None:
        """Start background scoring for share-ready recommendations."""
        from ..config import load_config, save_config
        body = _read_body(self) or {}

        # Persist an explicit decline server-side so it sticks across reloads,
        # browsers, and the background scanner (not just localStorage).
        if body.get("decline"):
            cfg = load_config()
            cfg["scoring_warmup_declined"] = True
            save_config(cfg)
            _json_response(self, {"status": "declined"})
            return

        confirm = bool(body.get("confirm_backend") or body.get("confirm"))
        if confirm:
            # Confirm must win over a stale decline — clear the flag BEFORE
            # trigger_scoring_warmup, whose top-of-function gate would otherwise
            # swallow the very confirm that should turn scoring on.
            cfg = load_config()
            if cfg.pop("scoring_warmup_declined", None) is not None:
                save_config(cfg)

        scanner = getattr(self.server, "_scanner", None)
        payload = trigger_scoring_warmup(
            scanner,
            confirm_backend=confirm,
            requested_backend=body.get("backend"),
        )
        _json_response(self, payload)

    def _handle_share_destination(self) -> None:
        """Return the optional hosted research-submission destination."""
        share_url, message = _validated_hosted_share_url()
        payload: dict[str, Any] = {
            "configured": bool(share_url),
            "daemon_upload_supported": False,
            "submissions_open": False,
            "preferred_upload_flow": "browser_zip",
            "cli_ingest_supported": False,
            "share_page_url": share_url,
            "submit_page_url": share_url,
            "maximum_bundle_size": None,
            "accepted_manifest_schema_versions": [],
            "supported_institution_email_policy": None,
            "support_contact": None,
            "message": message,
        }
        if not share_url:
            _json_response(self, payload)
            return
        try:
            capabilities = _fetch_hosted_share_capabilities()
        except Exception as exc:
            payload["message"] = f"Hosted submission is configured, but capabilities could not be loaded: {exc}"
            _json_response(self, payload)
            return

        submissions_open = bool(capabilities.get("submissions_open"))
        email_policy = capabilities.get("supported_institution_email_policy")
        if not isinstance(email_policy, dict):
            email_policy = None
        payload.update({
            "preferred_upload_flow": capabilities.get("preferred_upload_flow", "browser_zip"),
            "cli_ingest_supported": bool(capabilities.get("cli_ingest_supported")),
            "share_page_url": capabilities.get("share_page_url") or share_url,
            "submit_page_url": capabilities.get("submit_page_url") or capabilities.get("share_page_url") or share_url,
            "daemon_upload_supported": True,
            "submissions_open": submissions_open,
            "maximum_bundle_size": capabilities.get("maximum_bundle_size"),
            "accepted_manifest_schema_versions": capabilities.get("accepted_manifest_schema_versions", []),
            "supported_institution_email_policy": email_policy,
            "support_contact": capabilities.get("contact_email") or capabilities.get("support_contact"),
            "message": "Hosted research submissions are open." if submissions_open else "Hosted research submissions are currently closed.",
        })
        _json_response(self, payload)

    def _handle_share_consent(self) -> None:
        try:
            _json_response(self, fetch_hosted_consent())
        except urllib.error.HTTPError as exc:
            _json_response(self, {"error": _hosted_error_message(exc)}, exc.code)
        except Exception as exc:
            _json_response(self, {"error": f"Could not load hosted consent text: {exc}"}, 502)

    def _handle_share_verify_email(self) -> None:
        body = _read_body(self)
        email = body.get("email")
        if not isinstance(email, str) or not email.strip():
            _json_response(self, {"error": "email required"}, 400)
            return
        try:
            result = request_email_verification(email)
        except HostedServiceError as exc:
            _json_response(self, {"error": str(exc)}, _hosted_user_status(exc.status))
            return
        except ValueError as exc:
            _json_response(self, {"error": str(exc)}, 400)
            return
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            _json_response(self, {"error": str(exc)}, 502)
            return
        response = {
            "ok": True,
            "email": _normalize_email(email),
            "expires_at": result.get("expires_at"),
        }
        # `dev_code` is a local-development convenience only. Never forward it to
        # the browser when pointed at a production hosted deployment, even if the
        # server returns it.
        if result.get("dev_code") and _hosted_is_local_dev():
            response["dev_code"] = result["dev_code"]
        _json_response(self, response)

    def _handle_share_verify_confirm(self) -> None:
        body = _read_body(self)
        code = body.get("code")
        if not isinstance(code, str) or not code.strip():
            _json_response(self, {"error": "code required"}, 400)
            return
        try:
            result = confirm_pending_email_verification(code)
        except HostedServiceError as exc:
            _json_response(self, {"error": str(exc)}, _hosted_user_status(exc.status))
            return
        except ValueError as exc:
            _json_response(self, {"error": str(exc)}, 400)
            return
        except (OSError, RuntimeError, urllib.error.URLError) as exc:
            _json_response(self, {"error": str(exc)}, 502)
            return
        status = hosted_upload_status()
        _json_response(self, {
            "verified": True,
            "verified_email": status["verified_email"],
            "expires_at": result.get("upload_token_expires_at"),
        })

    def _handle_share_upload_status(self) -> None:
        _json_response(self, hosted_upload_status())

    def _handle_install_share_scanners(self) -> None:
        """Install pinned local scanner binaries and report readiness."""
        from ..redaction.scanner_install import ensure_share_scanners

        result = ensure_share_scanners(prefer_managed=True)
        if result["ok"]:
            _json_response(self, result)
            return
        _json_response(self, result, 503)

    @staticmethod
    def _auto_upload_http_status(result: dict[str, Any]) -> int:
        if result.get("ok") is not False:
            return 200
        if result.get("status") == 409 or result.get("code") in {
            "action_required",
            "authorization_required",
            "already_running",
            "control_conflict",
        }:
            return 409
        if result.get("code") in {"not_enabled", "paused", "not_enrolled"}:
            return 409
        if result.get("code") in {"credential_invalid", "credential_revoked"}:
            return 401
        if result.get("retryable"):
            return 503
        return 400

    def _send_auto_upload_result(self, result: dict[str, Any]) -> None:
        payload = dict(result)
        payload.pop("status", None)
        _json_response(self, payload, self._auto_upload_http_status(result))

    def _handle_auto_upload_status(self) -> None:
        from ..auto_upload import status

        self._send_auto_upload_result(status())

    def _handle_auto_upload_preview(self, *, refresh: bool) -> None:
        from ..auto_upload import preview

        self._send_auto_upload_result(preview(refresh=refresh))

    def _handle_auto_upload_enable(self) -> None:
        from ..auto_upload import enable

        body = _read_body(self) or {}
        self._send_auto_upload_result(
            enable(
                agent=str(body.get("agent") or "all"),
                accepted_authorization_version=(
                    str(body["accepted_authorization_version"])
                    if body.get("accepted_authorization_version") is not None
                    else None
                ),
                accepted_retention_version=(
                    str(body["accepted_retention_version"])
                    if body.get("accepted_retention_version") is not None
                    else None
                ),
                accepted_ownership_certification_version=(
                    str(body["accepted_ownership_certification_version"])
                    if body.get("accepted_ownership_certification_version") is not None
                    else None
                ),
                accepted_authorization_profile_hash=(
                    str(body["accepted_authorization_profile_hash"])
                    if body.get("accepted_authorization_profile_hash") is not None
                    else None
                ),
                challenge_only=bool(body.get("challenge_only")),
            )
        )

    def _handle_auto_upload_run(self) -> None:
        global _auto_upload_run_thread

        from ..auto_upload import run_cycle, status, whole_run_lock

        with _auto_upload_run_lock:
            current = status()
            mode = current.get("mode")
            if mode == "off":
                self._send_auto_upload_result(
                    {
                        "ok": False,
                        "code": "not_enabled",
                        "message": "Automatic upload is not enabled.",
                        "retryable": False,
                    }
                )
                return
            if mode == "paused":
                self._send_auto_upload_result(
                    {
                        "ok": False,
                        "code": "paused",
                        "message": "Automatic upload is paused.",
                        "retryable": False,
                    }
                )
                return
            if (
                current.get("health") == "action_required"
                and current.get("run_now_allowed") is not True
            ):
                self._send_auto_upload_result(
                    {
                        "ok": False,
                        "code": "action_required",
                        "message": "Review the automatic-upload status before running again.",
                        "retryable": False,
                    }
                )
                return
            if _auto_upload_run_thread is not None and _auto_upload_run_thread.is_alive():
                self._send_auto_upload_result(
                    {
                        "ok": False,
                        "code": "already_running",
                        "message": "An automatic-upload cycle is already running.",
                        "retryable": True,
                    }
                )
                return
            with whole_run_lock(blocking=False) as acquired:
                if not acquired:
                    self._send_auto_upload_result(
                        {
                            "ok": False,
                            "code": "already_running",
                            "message": "An automatic-upload cycle is already running.",
                            "retryable": True,
                        }
                    )
                    return

            def run_background() -> None:
                try:
                    run_cycle(force=True)
                except Exception:
                    logger.exception("Explicit automatic-upload cycle crashed")

            _auto_upload_run_thread = threading.Thread(
                target=run_background,
                name="clawjournal-auto-upload",
                daemon=True,
            )
            _auto_upload_run_thread.start()
        payload = dict(current)
        payload["overlay"] = "running"
        self._send_auto_upload_result(payload)

    def _handle_auto_upload_pause(self) -> None:
        from ..auto_upload import pause

        self._send_auto_upload_result(pause())

    def _handle_auto_upload_resume(self) -> None:
        from ..auto_upload import resume

        self._send_auto_upload_result(resume())

    def _handle_auto_upload_disable(self) -> None:
        from ..auto_upload import disable

        self._send_auto_upload_result(disable())

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
                source_filter=settings.get("source_filter"),
                include_unapproved=include_unapproved,
            )
            _json_response(self, stats)
        finally:
            conn.close()

    def _handle_quick_share(self) -> None:
        """Create and package a share; hosted submission needs consent first."""
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
            ai_pii_override = _optional_bool(body.get("ai_pii")) if "ai_pii" in body else None
            # Fail fast on hold-state BEFORE creating the share row: quick-share
            # leads straight to submit, so there is no point creating/packaging a
            # share for sessions the release gate would later block — and a gate
            # placed after create_share would orphan the draft share on rejection.
            # submit_share_to_hosted re-checks this at upload time.
            from .index import release_gate_blockers
            blockers = release_gate_blockers(conn, session_ids)
            if blockers:
                _json_response(self, {
                    "error": "Some selected sessions are on hold and cannot be shared.",
                    "blockers": blockers,
                }, 409)
                return
            source_blockers = source_scope_blockers(conn, session_ids, settings.get("source_filter"))
            if source_blockers:
                _json_response(self, {
                    "error": "Some selected sessions are outside the confirmed source scope.",
                    "blockers": source_blockers,
                }, 409)
                return
            review_blockers = revision_review_blockers(conn, session_ids)
            if review_blockers:
                _json_response(self, {
                    "error": "Updated traces require fresh approval before re-upload.",
                    "blockers": review_blockers,
                }, 409)
                return
            duplicate_blockers = already_shared_revision_blockers(conn, session_ids)
            if duplicate_blockers:
                _json_response(self, {
                    "error": "One or more selected trace revisions were already shared.",
                    "blockers": duplicate_blockers,
                }, 409)
                return
            current_rows = conn.execute(
                f"SELECT session_id, content_revision FROM sessions WHERE session_id IN "
                f"({','.join('?' for _ in session_ids)})",
                session_ids,
            ).fetchall()
            expected_revisions = {
                row["session_id"]: row["content_revision"] for row in current_rows
            }
            try:
                share_id = create_share(
                    conn,
                    session_ids,
                    note=note,
                    source_filter=settings.get("source_filter"),
                    expected_revisions=expected_revisions,
                )
            except RevisionConflictError as exc:
                _json_response(self, {
                    "error": str(exc),
                    "block_reason": "revision_conflict",
                    "blockers": exc.blockers,
                }, 409)
                return
            share = get_share(conn, share_id)
            if share is None:
                _json_response(self, {"error": "Share not found"}, 404)
                return
            export_dir, manifest, error = _prepare_share_export_for_upload(
                conn,
                share_id,
                share,
                settings,
                reuse_finalized=True,
                ai_pii_review_enabled=ai_pii_override,
            )
            if error:
                status_code = int(error.get("status", 500))
                _json_response(self, error, status_code)
                return
            if export_dir is None:
                _json_response(self, {"error": "Failed to prepare upload zip"}, 500)
                return
            with _share_rate_lock:
                WorkbenchHandler._last_share_time = time.time()
            _json_response(self, {
                "ok": True,
                "share_id": share_id,
                "bundle_id": share_id,
                "next_step": "submit",
                "export_path": str(export_dir),
                "session_count": len(manifest.get("sessions", [])),
                "redaction_summary": manifest.get("redaction_summary", {}),
            })
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
            cached = _load_finalized_share_export(share_id)
            if cached is not None:
                export_dir, finalized_manifest = cached
                share["manifest"] = finalized_manifest
                try:
                    share["zip_size_bytes"] = len(_build_share_zip(export_dir))
                except OSError:
                    pass
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
        expected_revisions = body.get("expected_revisions")
        if expected_revisions is not None and not isinstance(expected_revisions, dict):
            _json_response(self, {"error": "expected_revisions must be an object"}, 400)
            return
        conn = open_index()
        try:
            settings = get_effective_share_settings(conn, load_config())
            source_blockers = source_scope_blockers(conn, session_ids, settings.get("source_filter"))
            if source_blockers:
                _json_response(self, {
                    "error": "Some selected sessions are outside the confirmed source scope.",
                    "blockers": source_blockers,
                }, 409)
                return
            review_blockers = revision_review_blockers(conn, session_ids)
            if review_blockers:
                _json_response(self, {
                    "error": "Updated traces require fresh approval before re-upload.",
                    "blockers": review_blockers,
                }, 409)
                return
            duplicate_blockers = already_shared_revision_blockers(conn, session_ids)
            if duplicate_blockers:
                _json_response(self, {
                    "error": "One or more selected trace revisions were already shared.",
                    "blockers": duplicate_blockers,
                }, 409)
                return
            try:
                share_id = create_share(
                    conn, session_ids,
                    attestation=body.get("attestation"),
                    note=body.get("note"),
                    source_filter=settings.get("source_filter"),
                    expected_revisions=expected_revisions,
                )
            except RevisionConflictError as exc:
                _json_response(self, {
                    "error": str(exc),
                    "block_reason": "revision_conflict",
                    "blockers": exc.blockers,
                }, 409)
                return
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
            with open(sessions_file, encoding="utf-8") as f:
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

        from ..redaction.scanner_install import ensure_share_scanners

        scanner_setup = ensure_share_scanners()
        if not scanner_setup["ok"]:
            _json_response(self, {
                "error": scanner_setup.get("error") or "Required secret scanners are not installed.",
                "block_reason": "scanner-not-installed",
                "scanner_install": scanner_setup,
            }, 503)
            return

        conn = open_index()
        try:
            share = get_share(conn, share_id)
            if share is None:
                _json_response(self, {"error": "Share not found"}, 404)
                return

            settings = get_effective_share_settings(conn, load_config())
            source_blockers = source_scope_blockers(
                conn,
                [s["session_id"] for s in share.get("sessions") or []],
                settings.get("source_filter"),
            )
            if source_blockers:
                _json_response(self, {
                    "error": "Share contains sessions outside the confirmed source scope",
                    "blockers": source_blockers,
                }, 409)
                return
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
                status_code = 409 if manifest.get("block_reason") == "revision_conflict" else 422
                _json_response(self, {
                    "error": manifest.get("block_message") or "Share blocked by TruffleHog",
                    "block_reason": manifest.get("block_reason"),
                    "export_path": str(export_dir),
                    "blocked_sessions": manifest.get("blocked_sessions", []),
                    "trufflehog_summary": manifest.get("redaction_summary", {}).get("trufflehog"),
                }, status_code)
                return

            _json_response(self, {
                "ok": True,
                "export_path": str(export_dir),
                "session_count": len(manifest["sessions"]),
            })
        finally:
            conn.close()

    def _handle_seal_share(self, share_id: str) -> None:
        """Finalize a share for browser upload without returning zip bytes."""
        body = _read_body(self)
        ai_pii_override = _optional_bool(body.get("ai_pii")) if "ai_pii" in body else None
        conn = open_index()
        try:
            share = get_share(conn, share_id)
            if share is None:
                _json_response(self, {"error": "Share not found"}, 404)
                return

            settings = get_effective_share_settings(conn, load_config())
            export_dir, manifest, error = _prepare_share_export_for_upload(
                conn,
                share_id,
                share,
                settings,
                reuse_finalized=True,
                ai_pii_review_enabled=ai_pii_override,
            )
            if error:
                _json_response(self, error, int(error.get("status", 500)))
                return
            if export_dir is None:
                _json_response(self, {"error": "Failed to prepare upload zip"}, 500)
                return
            try:
                zip_size_bytes = len(_build_share_zip(export_dir))
            except OSError:
                zip_size_bytes = None

            _json_response(self, {
                "ok": True,
                "export_path": str(export_dir),
                "session_count": len(manifest.get("sessions", [])),
                "zip_size_bytes": zip_size_bytes,
                "redaction_summary": manifest.get("redaction_summary", {}),
            })
        finally:
            conn.close()

    def _handle_download_share(
        self,
        share_id: str,
        params: dict[str, list[str]] | None = None,
    ) -> None:
        """Generate a zip of the share and serve it as a browser download."""
        ai_pii_override = None
        if params and "ai_pii" in params:
            ai_pii_override = _body_bool(params.get("ai_pii", [""])[0])
        conn = open_index()
        try:
            share = get_share(conn, share_id)
            if share is None:
                _json_response(self, {"error": "Share not found"}, 404)
                return

            settings = get_effective_share_settings(conn, load_config())
            export_dir, _manifest, error = _prepare_share_export_for_upload(
                conn,
                share_id,
                share,
                settings,
                reuse_finalized=True,
                ai_pii_review_enabled=ai_pii_override,
            )
            if error:
                _json_response(self, error, int(error.get("status", 500)))
                return
            if export_dir is None:
                _json_response(self, {"error": "Failed to prepare download"}, 500)
                return

            zip_bytes = _build_share_zip(export_dir)

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
        """Submit a share to hosted research after consent."""
        # Validate the request BEFORE the rate-limit gate so a malformed request
        # (missing consent fields) doesn't consume the 10s cooldown — only a real
        # submission attempt should start it.
        body = _read_body(self)
        force = _body_bool(body.get("force", False))
        ai_pii_override = _optional_bool(body.get("ai_pii")) if "ai_pii" in body else None
        required = [
            "accept_terms",
            "ownership_certification",
            "consent_version",
            "retention_policy_version",
        ]
        missing = [key for key in required if key not in body]
        if missing:
            _json_response(self, {
                "error": (
                    "Hosted submission requires consent fields. "
                    "Use the Share tab Submit step and review the current terms."
                ),
                "missing": missing,
            }, 400)
            return

        with _share_rate_lock:
            now = time.time()
            elapsed = now - WorkbenchHandler._last_share_time
            if elapsed < _SHARE_COOLDOWN_SECONDS:
                _json_response(self, {
                    "error": f"Rate limited. Try again in {int(_SHARE_COOLDOWN_SECONDS - elapsed)}s.",
                }, 429)
                return
            # Mark in-flight atomically (like _handle_quick_share) so a second
            # tab / double-click cannot also pass this check and reach the
            # hosted POST concurrently, and so a failed attempt still starts the
            # cooldown instead of allowing an immediate retry loop.
            WorkbenchHandler._last_share_time = now

        conn = open_index()
        try:
            settings = get_effective_share_settings(conn, load_config())
            result = submit_share_to_hosted(
                conn,
                share_id,
                force=force,
                settings=settings,
                ai_pii_review_enabled=ai_pii_override,
                accept_terms=_body_bool(body.get("accept_terms")),
                ownership_certification=_body_bool(body.get("ownership_certification")),
                consent_version=str(body.get("consent_version") or ""),
                retention_policy_version=str(body.get("retention_policy_version") or ""),
            )
            if result.get("ok"):
                # Cache only the non-authoritative offer bit after a successful
                # manual receipt.  Automatic Enable still performs a fresh,
                # fail-closed capability validation before changing state.
                try:
                    capabilities = _fetch_hosted_share_capabilities()
                    config = load_config()
                    config["auto_upload_capability_available"] = (
                        _recurring_offer_available(capabilities)
                    )
                    save_config(config)
                except Exception:
                    pass
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
            results = scanner.scan_once_if_idle()
            if results is None:
                _json_response(
                    self,
                    {
                        "ok": True,
                        "status": "already_running",
                        "new_sessions": {},
                        "updated_sessions": {},
                        "unchanged_sessions": {},
                    },
                    202,
                )
                return
            warmup = trigger_scoring_warmup(scanner)
            payload: dict[str, Any] = {
                "ok": True,
                "new_sessions": results,
                "updated_sessions": scanner.last_updated_by_source,
                "unchanged_sessions": scanner.last_unchanged_by_source,
            }
            payload["scoring_warmup"] = warmup
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

    def _handle_desktop_opened(self) -> None:
        """Record a real SPA mount without blocking the request on OS tools."""
        from ..desktop import note_opened_async

        _json_response(self, {"ok": True, "scheduled": note_opened_async()})

    # --- Static file serving ---

    def _serve_static(self, path: str) -> None:
        """Serve frontend static files, falling back to index.html for SPA routing."""
        # Backward compatibility for older bookmarks/openers that prefixed SPA
        # routes with /traces.
        if path == "/traces" or path.startswith("/traces/"):
            path = path[len("/traces"):] or "/"

        if path == "/" or path == "":
            path = "/index.html"

        relative_path = path.lstrip("/")
        file_path = (FRONTEND_DIST / relative_path).resolve()
        if not file_path.is_relative_to(FRONTEND_DIST.resolve()):
            self.send_error(403)
            return

        snapshot: FrontendSnapshot | None = getattr(
            self.server, "_frontend_snapshot", None
        )
        served_path = relative_path
        try:
            if snapshot is not None:
                data = snapshot.read(served_path)
                if data is None:
                    served_path = "index.html"
                    data = snapshot.read(served_path)
                if data is None:
                    self._serve_placeholder()
                    return
            else:
                # Direct run_server callers and development tests retain the
                # historical disk-backed behavior. The CLI supplies an
                # immutable startup snapshot before its updater can touch dist.
                if not file_path.exists() or not file_path.is_file():
                    file_path = FRONTEND_DIST / "index.html"
                if not file_path.exists():
                    self._serve_placeholder()
                    return
                served_path = file_path.name
                data = file_path.read_bytes()
        except OSError:
            self.send_error(404)
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
        ext = Path(served_path).suffix.lower()
        content_type = content_types.get(ext, "application/octet-stream")

        try:
            if content_type == "text/html":
                data = self._inject_api_token(data)
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            if content_type == "text/html":
                # index.html references content-hashed asset filenames, so it must
                # never be cached: a stale copy pins the browser to an old bundle
                # after a rebuild (the dev-staleness trap that `--reload` targets).
                # Hashed assets under /assets/* stay implicitly cacheable.
                self.send_header("Cache-Control", "no-store, must-revalidate")
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


def _warn_if_frontend_stale() -> None:
    """In a dev checkout, warn when the served bundle is older than build inputs.

    The editable install serves the gitignored ``dist/`` straight off disk, so
    frontend edits (or a freshly-merged feature) are invisible until someone runs
    a build. We only have a ``src/`` tree in a dev checkout -- the shipped wheel
    packages only ``dist/`` -- so its presence is what distinguishes "developer
    who should rebuild" from "user running the released package". Silent on the
    latter.
    """
    frontend_root = FRONTEND_DIST.parent
    src_dir = frontend_root / "src"
    if not src_dir.is_dir():
        return  # installed wheel, not a dev checkout -- nothing to compare against

    index_html = FRONTEND_DIST / "index.html"
    # Absolute --prefix so the hint is copy-pasteable regardless of the cwd the
    # daemon was launched from (`clawjournal serve` is a global entry point).
    build_cmd = f"npm --prefix {frontend_root} run build"

    def _banner(lines: list[str]) -> None:
        width = max(len(line) for line in lines) + 2
        bar = "=" * width
        print(f"\n{bar}", file=sys.stderr)
        for line in lines:
            print(f" {line}", file=sys.stderr)
        print(f"{bar}\n", file=sys.stderr)

    if not index_html.exists():
        _banner([
            "⚠  frontend bundle is MISSING — the workbench will serve a placeholder.",
            f"   build it:  {build_cmd}",
        ])
        return

    try:
        dist_mtime = index_html.stat().st_mtime
        newest_input = _newest_frontend_build_input_mtime(frontend_root)
    except OSError:
        return  # can't stat -- don't block startup over a warning

    if newest_input > dist_mtime:
        _banner([
            "⚠  frontend bundle is STALE — build inputs are newer than dist/.",
            "   You are seeing an OLD UI. Rebuild to pick up frontend changes:",
            f"     {build_cmd}",
        ])


def _newest_frontend_build_input_mtime(frontend_root: Path) -> float:
    newest = 0.0
    for rel in _FRONTEND_BUILD_INPUT_FILES:
        path = frontend_root / rel
        if not path.is_file():
            continue
        newest = max(newest, path.stat().st_mtime)

    for rel in _FRONTEND_BUILD_INPUT_DIRS:
        root = frontend_root / rel
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file():
                newest = max(newest, path.stat().st_mtime)
    return newest


# ---------- update self-restart ----------------------------------------------
#
# The background auto-update (clawjournal/selfupdate.py) fast-forwards the
# checkout and reruns the installer, but a running daemon keeps executing the
# Python it imported at startup. The CLI pins an immutable frontend snapshot
# before starting that updater, so the old process continues serving one
# compatible frontend/backend pair while ``dist/`` is rebuilt. When HEAD has
# moved AND the install is fully reconciled (no pending reinstall, workbench
# build current), the daemon re-execs itself at a quiet moment and the new
# process captures the new pair together. Restarting is equivalent to the
# user's Ctrl-C + rerun, which the daemon already supports; the SQLite index
# and the upload ledger are built to survive it.

RESTART_CHILD_ENV = "CLAWJOURNAL_RESTART_CHILD"  # set on the re-exec'd process: don't reopen the browser
_RESTART_POLL_SECONDS = 60.0
# Don't restart within this window of a mutating request — a user mid-flow
# (queueing a share, changing hold state) shouldn't have the rug moved.
_RESTART_MUTATION_IDLE_SECONDS = 600.0
_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_activity_lock = threading.Lock()
_activity = {"in_flight": 0, "last_mutation": 0.0}
_request_admission_open = True


def _note_request_start() -> bool:
    with _activity_lock:
        if not _request_admission_open:
            return False
        _activity["in_flight"] += 1
        return True


def _note_request_end(method: str | None) -> None:
    with _activity_lock:
        _activity["in_flight"] = max(0, _activity["in_flight"] - 1)
        if method in _MUTATING_METHODS:
            _activity["last_mutation"] = time.time()


def _snapshot_activity() -> dict[str, float]:
    with _activity_lock:
        return dict(_activity)


def _open_request_admission() -> None:
    """Allow handlers to begin requests for a newly started server."""
    global _request_admission_open
    with _activity_lock:
        _request_admission_open = True


def _freeze_request_admission(*, now: float | None = None) -> bool:
    """Atomically close admission if the daemon is still restart-safe.

    ``_update_restart_due`` is a pre-flight check.  A request can start or
    finish after that snapshot, so the watcher must repeat the request and
    mutation gates while holding the same lock used by handlers.  Once this
    succeeds, no new handler can enter the old process.
    """
    global _request_admission_open
    with _activity_lock:
        if not _request_admission_open or _activity["in_flight"] > 0:
            return False
        t = time.time() if now is None else now
        last_mutation = _activity["last_mutation"]
        if (
            last_mutation
            and t - last_mutation < _RESTART_MUTATION_IDLE_SECONDS
        ):
            return False
        _request_admission_open = False
        return True


def _resume_request_admission() -> None:
    """Undo a tentative freeze when a background worker wins the race."""
    global _request_admission_open
    with _activity_lock:
        _request_admission_open = True


def _background_workers_active(scanner: Scanner | None = None) -> bool:
    """Whether re-exec would interrupt durable or expensive background work."""
    if _BENCHMARK_GEN_LOCK.locked():
        return True
    upload_thread = _auto_upload_run_thread
    if upload_thread is not None and upload_thread.is_alive():
        return True
    if scanner is not None:
        score_thread = scanner._score_thread
        if score_thread is not None and score_thread.is_alive():
            return True
        if scanner._scan_lock.locked():
            return True
    return False


def _update_restart_due(
    repo: Path,
    startup_head: str,
    *,
    now: float | None = None,
    activity: dict[str, float] | None = None,
    scanner: Scanner | None = None,
) -> str | None:
    """Return the new HEAD when a graceful restart should happen, else None.

    Deliberately conservative: any doubt (can't read HEAD, install not yet
    reconciled, requests in flight, recent mutation) defers to the next poll.
    """
    from .. import selfupdate

    if os.environ.get(RELOAD_CHILD_ENV) == "1":
        return None  # the --reload supervisor owns restarts in dev
    head = selfupdate._rev_parse(repo, "HEAD")
    if not head or head == startup_head:
        return None
    if selfupdate.reinstall_in_progress():
        return None  # HEAD may have moved before the pending record was written
    if selfupdate.reinstall_needed(repo):
        return None  # wait for the background reinstall to finish the job
    snap = activity if activity is not None else _snapshot_activity()
    if snap["in_flight"] > 0:
        return None
    if _background_workers_active(scanner):
        return None
    t = time.time() if now is None else now
    if snap["last_mutation"] and t - snap["last_mutation"] < _RESTART_MUTATION_IDLE_SECONDS:
        return None
    return head


def _exec_restart(server: ThreadingHTTPServer,
                  v6_server: ThreadingHTTPServer | None) -> None:
    """Replace this process with a fresh `clawjournal serve`. Never returns
    on success — argv is preserved, so port/source/remote flags carry over.

    The listening sockets are closed *before* the exec so the new process
    can rebind the same port (on Windows, where exec is emulated as
    spawn+exit, this matters even more).
    """
    for srv in (server, v6_server):
        if srv is None:
            continue
        try:
            srv.server_close()
        except OSError:
            pass
    os.environ[RESTART_CHILD_ENV] = "1"
    try:
        os.execv(sys.executable, _reload_child_command())
    except OSError:
        logger.error(
            "Could not restart after update — run `clawjournal serve` again manually.",
            exc_info=True,
        )


# Env vars that coordinate the --reload supervisor with its server child.
RELOAD_CHILD_ENV = "CLAWJOURNAL_RELOAD_CHILD"  # set on the child: "run the server, don't supervise"
RELOAD_OPEN_BROWSER_ENV = "CLAWJOURNAL_RELOAD_OPEN_BROWSER"  # set only on the first child
_RELOAD_POLL_SECONDS = 1.0
_RELOAD_DEBOUNCE_SECONDS = 0.3
# Directories with no backend source (or huge trees) we never want to walk.
_RELOAD_SKIP_DIRS = {"node_modules", ".git", "__pycache__", "dist", ".mypy_cache", ".pytest_cache"}


def _python_source_signature(root: Path) -> dict[str, float]:
    """Map every ``*.py`` path under ``root`` to its mtime, pruning noise dirs.

    Pruning is not optional: ``node_modules`` lives *inside* the package tree
    (``web/frontend/``), so a naive ``rglob`` would traverse thousands of JS
    dirs on every poll.
    """
    sig: dict[str, float] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _RELOAD_SKIP_DIRS]
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = os.path.join(dirpath, name)
            try:
                sig[path] = os.stat(path).st_mtime
            except OSError:
                continue
    return sig


def _terminate_child(proc: subprocess.Popen) -> None:
    """SIGTERM the server child, escalating to SIGKILL if it doesn't exit."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _reload_child_command() -> list[str]:
    """Run the CLI as a module so reload works for console scripts and -m."""
    return [sys.executable, "-m", "clawjournal.cli", *sys.argv[1:]]


def run_with_reload(open_browser: bool = True) -> None:
    """Dev supervisor: run the daemon as a child and restart it on ``*.py`` edits.

    Python imports each module once per process, so a long-running ``clawjournal
    serve`` keeps executing the code it loaded at startup — backend edits stay
    invisible until a manual restart. This parent watches the package's Python
    files and respawns the server child whenever they change.

    We restart the whole child rather than reload modules in-process: the daemon
    owns a bound HTTP socket and background scanner threads, so in-process
    ``importlib.reload`` would leave half-swapped state. This is the same
    full-restart strategy uvicorn/flask ``--reload`` use. On a child crash (e.g.
    a syntax error in a just-saved file) we hold instead of hot-looping, and
    respawn on the next edit.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    watch_root = Path(__file__).resolve().parent.parent  # the clawjournal/ package

    # Install explicit handlers rather than relying on KeyboardInterrupt: a
    # process backgrounded with `&` inherits SIGINT=SIG_IGN (Python keeps it
    # ignored), so Ctrl-C alone wouldn't fire. Handling SIGTERM too means
    # `kill`/`pkill` of the supervisor tears the child down instead of orphaning
    # it. (SIGKILL of the supervisor can still orphan the child — unavoidable.)
    stopping = threading.Event()

    def _on_signal(_signum: int, _frame: object) -> None:
        stopping.set()

    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _on_signal)
        except (ValueError, OSError):
            pass  # not the main thread / platform doesn't support it

    def _spawn(first: bool) -> subprocess.Popen:
        env = dict(os.environ)
        env[RELOAD_CHILD_ENV] = "1"
        # Only the first child opens a browser tab; restarts must not spawn more.
        env.pop(RELOAD_OPEN_BROWSER_ENV, None)
        if first and open_browser:
            env[RELOAD_OPEN_BROWSER_ENV] = "1"
        return subprocess.Popen(_reload_child_command(), env=env)

    logger.info(
        "reloader: watching %s/**/*.py — save a backend file to restart the server",
        watch_root,
    )
    sig = _python_source_signature(watch_root)
    proc = _spawn(first=True)

    try:
        while not stopping.is_set():
            stopping.wait(_RELOAD_POLL_SECONDS)
            if stopping.is_set():
                break

            if proc.poll() is not None:
                # Child exited on its own — almost always a crash in edited code.
                # Hold (don't hot-loop respawning) until the next save, then retry.
                logger.error(
                    "reloader: server exited (code %s) — fix it and save to retry",
                    proc.returncode,
                )
                while proc.poll() is not None and not stopping.is_set():
                    stopping.wait(_RELOAD_POLL_SECONDS)
                    new_sig = _python_source_signature(watch_root)
                    if new_sig != sig:
                        sig = new_sig
                        logger.info("reloader: change detected — restarting server")
                        proc = _spawn(first=False)
                continue

            new_sig = _python_source_signature(watch_root)
            if new_sig != sig:
                stopping.wait(_RELOAD_DEBOUNCE_SECONDS)  # let a burst of saves settle
                sig = _python_source_signature(watch_root)
                logger.info("reloader: change detected — restarting server")
                _terminate_child(proc)
                proc = _spawn(first=False)
    finally:
        logger.info("reloader: shutting down")
        _terminate_child(proc)


def _try_serve_ipv6_loopback(
    port: int,
    scanner: "Scanner",
    frontend_snapshot: FrontendSnapshot | None = None,
) -> ThreadingHTTPServer | None:
    """Start a companion IPv6 (``::1``) loopback server on ``port``, serving in a
    daemon thread with the same handler/scanner as the primary IPv4 server.

    Returns the server, or ``None`` if IPv6 loopback isn't available (no IPv6
    stack, or the port is already taken on ``::1``) — the IPv4 socket still
    serves, so this is best-effort.
    """
    import socket as _socket

    class _IPv6ThreadingHTTPServer(ThreadingHTTPServer):
        address_family = _socket.AF_INET6

    try:
        v6 = _IPv6ThreadingHTTPServer(("::1", port), WorkbenchHandler)
    except OSError as exc:
        logger.info(
            "IPv6 loopback (::1) bind skipped (%s); reach the workbench via 127.0.0.1",
            exc,
        )
        return None
    v6._scanner = scanner  # type: ignore[attr-defined]
    v6._frontend_snapshot = frontend_snapshot  # type: ignore[attr-defined]
    threading.Thread(target=v6.serve_forever, daemon=True).start()
    return v6


def run_server(
    port: int = DEFAULT_PORT,
    open_browser: bool = True,
    source_filter: str | None = None,
    remote: bool = False,
    allow_port_fallback: bool = True,
    startup_head: str | None = None,
    frontend_snapshot: FrontendSnapshot | None = None,
) -> None:
    """Start the workbench daemon — scanner + HTTP server.

    `allow_port_fallback` keeps the historical behaviour for `clawjournal
    serve`: if the requested port is taken, quietly bind an ephemeral one. The
    desktop launcher passes False, because there a busy port almost always
    means our own daemon already won the race — silently starting a second one
    would put two scanners on the same SQLite index and strand the browser on
    a port that won't be there next time. ``startup_head`` and
    ``frontend_snapshot`` are captured by the CLI before its detached updater
    can move the checkout or rebuild the workbench.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )

    scanner = Scanner(source_filter=source_filter)
    _open_request_admission()

    # Start HTTP server first so it's responsive immediately. The primary socket
    # is IPv4 127.0.0.1 — what the CLI health probe, curl, and SSH `-L` tunnels
    # expect.
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), WorkbenchHandler)
    except OSError:
        if not allow_port_fallback:
            raise
        server = ThreadingHTTPServer(("127.0.0.1", 0), WorkbenchHandler)
        port = server.server_address[1]
    server._scanner = scanner  # type: ignore[attr-defined]
    server._frontend_snapshot = frontend_snapshot  # type: ignore[attr-defined]

    # Companion IPv6 loopback socket on the same port. Browsers resolve
    # `localhost` to ::1 (IPv6) first and don't all fall back to IPv4, so an
    # IPv4-only daemon leaves the workbench unreachable via localhost on
    # IPv6-preferring systems. Each family is its own ::1 / 127.0.0.1 loopback
    # socket — nothing is exposed beyond the local host.
    v6_server = _try_serve_ipv6_loopback(port, scanner, frontend_snapshot)

    url = f"http://localhost:{port}/"
    logger.info("Workbench running at %s", url)

    _warn_if_frontend_stale()

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
        try:
            results = scanner.scan_once()
        except ScanBusyError:
            logger.info(
                "Initial scan skipped: another process is refreshing the index"
            )
        else:
            if not scanner._stop_event.is_set():
                trigger_scoring_warmup(scanner)
            total = sum(results.values())
            logger.info(
                "Initial scan complete: %d new sessions indexed, "
                "%d existing traces updated, "
                "%d subagent relationships linked",
                total,
                scanner.last_updated_count,
                scanner.last_linked_count,
            )
        if not scanner._stop_event.is_set():
            scanner.start()
            logger.info("Background scanner started (interval: %ds)", SCAN_INTERVAL)

    threading.Thread(target=_initial_scan, daemon=True).start()

    # Watch the editable checkout: once the background auto-update has both
    # moved HEAD and reconciled the install, restart at a quiet moment so the
    # new frontend/backend pair becomes visible together. No-op for wheel
    # installs and under the --reload supervisor.
    restart_to: dict[str, str | None] = {"head": None}

    def _watch_for_update() -> None:
        from .. import selfupdate

        repo = selfupdate._package_repo_root()
        if repo is None:
            return  # wheel install — nothing to watch
        initial_head = startup_head or selfupdate._rev_parse(repo, "HEAD")
        if not initial_head:
            return
        while True:
            time.sleep(_RESTART_POLL_SECONDS)
            try:
                head = _update_restart_due(repo, initial_head, scanner=scanner)
            except Exception:
                logger.debug("update-restart check failed", exc_info=True)
                continue
            if head:
                # The earlier activity snapshot is only advisory.  Atomically
                # close handler admission before committing so a request
                # cannot enter between the quietness check and shutdown.
                if not _freeze_request_admission():
                    continue
                if _background_workers_active(scanner):
                    _resume_request_admission()
                    continue
                restart_to["head"] = head
                # Prevent the periodic/initial scanner from starting another
                # pass or scoring batch while the listening loops stop.
                scanner._stop_event.set()
                logger.info(
                    "ClawJournal updated (%s -> %s) — restarting the workbench "
                    "to serve the new version",
                    initial_head[:7], head[:7],
                )
                if v6_server is not None:
                    v6_server.shutdown()
                server.shutdown()
                return

    threading.Thread(target=_watch_for_update, daemon=True,
                     name="update-restart").start()

    # Reconcile benchmark rows orphaned in 'generating' by a previous crash/restart
    # (the only normal exit from 'generating' is the in-process worker).
    try:
        from ..benchmark import store as _bstore
        _bconn = open_index()
        try:
            n = _bstore.reconcile_stale_generating(_bconn)
            if n:
                logger.info("Reconciled %d stale 'generating' benchmark row(s) -> failed", n)
        finally:
            _bconn.close()
    except Exception:
        logger.warning("benchmark stale-row reconcile skipped", exc_info=True)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        scanner.stop()
        server.shutdown()
        if v6_server is not None:
            v6_server.shutdown()
        if restart_to["head"]:
            # A scan that began just before admission froze may outlive
            # Scanner.stop()'s bounded join.  Never exec over any durable or
            # expensive worker; with admission frozen, no new one can start.
            while _background_workers_active(scanner):
                time.sleep(0.05)
            _exec_restart(server, v6_server)
